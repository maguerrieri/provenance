"""web.archive.org snapshots.

Runs last and never blocks verification: SPN is rate-limited and frequently slow, and a
missing snapshot is a warning, not a reason to discard a good citation.

We save a *fresh* snapshot by default and fall back to an existing one only when saving
fails. An old snapshot can predate the sentence being cited, and for a paywalled source
the snapshot is the human's only route to the text — handing them a 2019 capture of a 2026
article is worse than handing them nothing, because it looks like verification.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SAVE = "https://web.archive.org/save/"
SAVE_API = "https://web.archive.org/save"
AVAIL = "https://archive.org/wayback/available"

# The pipeline's own record of what `vg archive` saved, one file per run. It exists so that
# `archive_url` can be a machine field like `verification`: stripped from agent-authored
# claim files on ingest, and re-applied from here by every command that writes claims back.
# Keeping the value in the claim file instead meant `vg verify` could not strip it (that
# would delete every snapshot), so an agent-authored one survived — and on a URL where Save
# Page Now fails, `vg archive` kept it and verified against it.
RECORDS = "archives.json"

# `web/<timestamp>[<modifier>_]/<target>`. The modifier (`id_`, `if_`, …) selects a replay
# mode of the same capture, so it does not change which page was archived.
_WAYBACK = re.compile(r"^https?://web\.archive\.org/web/(\d{1,14})(?:[a-z]{2}_)?/(.+)$", re.I)

# Save Page Now rate-limits and 500s hard for anonymous clients: a full 31-question run left
# 58 of ~150 URLs with no snapshot. Authenticated SPN gets a much higher quota.
#
# Keys come from https://archive.org/account/s3.php (requires an archive.org login) and are
# read from the environment ONLY — never a file this code reads, never a value it logs.
ACCESS_KEY_ENV = "ARCHIVE_ORG_ACCESS_KEY"
SECRET_KEY_ENV = "ARCHIVE_ORG_SECRET_KEY"


def credentials() -> tuple[str, str] | None:
    """(access, secret) from the environment, or None. Never returns a partial pair."""
    access = (os.environ.get(ACCESS_KEY_ENV) or "").strip()
    secret = (os.environ.get(SECRET_KEY_ENV) or "").strip()
    return (access, secret) if access and secret else None


def auth_header() -> dict[str, str]:
    creds = credentials()
    return {"Authorization": f"LOW {creds[0]}:{creds[1]}"} if creds else {}


def have_credentials() -> bool:
    return credentials() is not None


def _https(snapshot_url: str) -> str:
    """Upgrade only the Wayback prefix to https.

    The availability API answers with an http:// snapshot URL, and the cited URL is
    embedded in the path — so a blanket replace would rewrite the archived target too
    (`…/web/2019/http://example.com` → `…/https://example.com`), naming a snapshot that
    doesn't exist. Only the leading scheme is ours to change.
    """
    if snapshot_url.startswith("http://web.archive.org/"):
        return "https://" + snapshot_url[len("http://"):]
    return snapshot_url


def existing_snapshot(url: str, timeout: float = 20.0) -> str | None:
    try:
        r = httpx.get(AVAIL, params={"url": url}, timeout=timeout,
                      follow_redirects=True)
        snap = r.json().get("archived_snapshots", {}).get("closest", {})
        if snap.get("available"):
            return _https(snap.get("url", "")) or None
    except Exception:  # noqa: BLE001
        pass
    return None


def is_snapshot(url: str) -> bool:
    """A Wayback URL is already the snapshot. Submitting it to SPN yields
    web.archive.org/save/https://web.archive.org/web/… — a URL that is not a snapshot, which
    the model then rejects, which took down an entire build in testing."""
    return url.lower().startswith(("https://web.archive.org/web/", "http://web.archive.org/web/"))


def wayback_target(snapshot_url: str) -> str | None:
    """The page a Wayback snapshot URL is a capture of, or None if it names no page."""
    m = _WAYBACK.match(snapshot_url.strip())
    if not m or not re.match(r"^https?://", m.group(2), re.I):
        return None
    return m.group(2)


def _page_key(url: str) -> tuple[str, str, str, str] | None:
    """Identity of a page for snapshot matching.

    Folds only what does not change which page is meant: the scheme, the scheme's own default
    port, one trailing slash, host case, and the fragment (never sent to the server, so never
    part of a capture). Everything else must match exactly — a looser rule is room for a
    snapshot of some other page to pass as this one.
    """
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        return None
    scheme = p.scheme.lower()
    if scheme not in ("http", "https") or not p.hostname:
        return None
    default = 80 if scheme == "http" else 443
    host = p.hostname if port in (None, default) else f"{p.hostname}:{port}"
    userinfo = p.netloc.rpartition("@")[0]
    path = p.path[:-1] if p.path.endswith("/") else p.path
    return userinfo, host, path, p.query


def cited_page(url: str) -> str | None:
    """The page a citation points at. A citation may itself be a snapshot — `vg calaccess
    cite` recommends one for bot-protected pages — and then it points at that snapshot's
    target, so a snapshot of the same target is a snapshot of the cited page."""
    return wayback_target(url) if is_snapshot(url) else url


def snapshot_of(url: str, snapshot_url: str, *, also: tuple[str, ...] = ()) -> bool:
    """True when `snapshot_url` is a Wayback capture of the page `url` cites.

    The `web.archive.org/web/` host pin says who SERVES a snapshot, not which page it is a
    capture of — anyone can Save Page Now a page they control and cite the result. `also`
    admits further pages the pipeline itself saw the cited URL resolve to (the live fetch's
    final URL after redirects), which a capture of a redirecting URL lands on.
    """
    target = wayback_target(snapshot_url)
    key = _page_key(target) if target else None
    if key is None:
        return False
    wanted = [cited_page(url), *also]
    return any(w and _page_key(w) == key for w in wanted)


def load_records(data: Path) -> dict[str, dict]:
    """The snapshots `vg archive` saved for this run, keyed by cited URL.

    An unreadable file raises rather than reading as empty: empty means "nothing archived",
    so every snapshot link would vanish from the review app with no sign of why.
    """
    p = data / RECORDS
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{p} is unreadable ({e}) — `vg archive` sets it aside and "
                         "rebuilds it") from None
    # A record that isn't one is damage too: dropping it would lose that URL's snapshot
    # without a word.
    if not isinstance(raw, dict) or not all(isinstance(r, dict) for r in raw.values()):
        raise ValueError(f"{p} is not a URL → snapshot mapping — `vg archive` sets it aside "
                         "and rebuilds it")
    return raw


def save_records(data: Path, records: dict[str, dict]) -> None:
    """Write atomically: `vg archive` saves after every URL, and an interrupted write must
    not leave a half file that every later command refuses to read."""
    data.mkdir(parents=True, exist_ok=True)
    tmp = data / f".{RECORDS}.tmp"
    tmp.write_text(json.dumps(records, indent=1, sort_keys=True))
    os.replace(tmp, data / RECORDS)


def save(url: str, *, timeout: float = 60.0, prefer_existing: bool = False) -> tuple[str | None, str | None]:
    """Return (archive_url, error). Saves fresh unless `prefer_existing` is set (cheap
    re-runs), and falls back to any existing snapshot if the save fails — returning the
    failure alongside it, so an older capture is never recorded as a fresh one."""
    if is_snapshot(url):
        return _https(url), None
    if prefer_existing:
        snap = existing_snapshot(url)
        if snap:
            return snap, None
    auth = auth_header()
    ua = {"User-Agent": "vgpipe/0.1 (voter guide citation archiver)"}
    try:
        if auth:
            # Authenticated SPN takes a POST and answers with JSON: a job id to poll, or the
            # snapshot outright. Much higher quota than the anonymous GET path.
            r = httpx.post(SAVE_API, timeout=timeout, follow_redirects=True,
                           headers={**ua, **auth, "Accept": "application/json"},
                           data={"url": url, "skip_first_archive": "1"})
            if r.status_code == 429:
                return None, "rate limited by web.archive.org (authenticated)"
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = {}
            if isinstance(payload, dict):
                if payload.get("message") and not payload.get("job_id"):
                    return None, f"SPN: {str(payload['message'])[:120]}"
                job = payload.get("job_id")
                if job:
                    snap = _poll_job(job, timeout=timeout)
                    if snap:
                        return snap, None
        else:
            r = httpx.get(SAVE + url, timeout=timeout, follow_redirects=True, headers=ua)
            if r.status_code == 429:
                return None, ("rate limited by web.archive.org — set "
                              f"{ACCESS_KEY_ENV}/{SECRET_KEY_ENV} for a higher quota")
            final = _https(str(r.url))   # SPN can land on http://; the model requires https
            if "web.archive.org/web/" in final:
                return final, None
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    else:
        err = f"save returned HTTP {r.status_code}"
    # Last resort: any older snapshot beats none — but say it is one. Checking it still
    # decides whether it holds the page.
    snap = existing_snapshot(url)
    return (snap, f"{err}; using an earlier capture") if snap else (None, err)


def _poll_job(job_id: str, *, timeout: float = 60.0, tries: int = 12) -> str | None:
    """Poll an authenticated SPN job until it reports a timestamp."""
    for _ in range(tries):
        try:
            r = httpx.get(f"https://web.archive.org/save/status/{job_id}", timeout=timeout,
                          headers={**auth_header(), "Accept": "application/json"})
            st = r.json() if r.content else {}
        except Exception:  # noqa: BLE001
            return None
        status = (st or {}).get("status")
        if status == "success":
            ts, original = st.get("timestamp"), st.get("original_url")
            if ts and original:
                return f"https://web.archive.org/web/{ts}/{original}"
            return None
        if status == "error":
            return None
        time.sleep(3)
    return None


def archive_all(urls: list[str], *, delay: float | None = None, progress=None) -> dict[str, str | None]:
    """Snapshot each URL. Anonymous runs pace themselves; authenticated ones need less."""
    if delay is None:
        delay = 1.0 if have_credentials() else 3.0
    out: dict[str, str | None] = {}
    for i, u in enumerate(urls):
        out[u], err = save(u)
        if progress:
            progress(u, out[u], err)
        if i < len(urls) - 1:
            time.sleep(delay)  # SPN is aggressively rate-limited
    return out
