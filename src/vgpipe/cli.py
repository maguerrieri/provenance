"""vg — voter guide pipeline CLI."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import archive as arch
from .fetch import fetch as fetch_url
from .fetch import kept_copy_note, no_text_layer, pages_without_text
from .models import Claim, check_archive_url, strip_machine_fields
from .races import available as available_races
from .races import load as load_race
from .report import clear_render, render
from .sources import domain, load_rules
from .verify import (
    GOOD,
    NO_TEXT_LAYER,
    apply_archive,
    check_corroboration,
    check_inputs,
    check_snippet,
    junk_capture,
    missing_filing_date,
    page_list,
    revalidate_from_cache,
    secondary_host,
    snippet_problem,
    verify_against_archive,
    verify_source,
)

app = typer.Typer(add_completion=False, help="Voter guide research pipeline")
con = Console()


def qid_sort_key(qid: str) -> tuple:
    """Sort q1, q2, q10 in that order — not q1, q10, q2.

    Question ids are number-then-optional-letter (q2a, q2b), so compare the number
    numerically and the suffix as text. A plain string sort scatters a 31-question run in the
    review app and makes it hard to tell what is missing.
    """
    m = re.match(r"^([A-Za-z]*)(\d+)([A-Za-z0-9._-]*)$", qid.strip())
    if not m:
        return (1, 0, qid)
    prefix, num, suffix = m.groups()
    return (0, int(num), suffix, prefix)


def _load_or_exit(claims_dir: Path, **kw) -> list[Claim]:
    """Load claims, turning expected data problems into a readable message rather than a
    traceback — these are things the operator fixes in the data directory, not bugs."""
    try:
        return load_claims(claims_dir, **kw)
    except ValueError as e:
        con.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None


@contextmanager
def _judgments_or_exit():
    """Stop on an unreadable verdict file, naming it. Like a malformed claim, it is something
    the operator fixes in the data directory — and carrying on without it would render every
    source it judged as unreviewed, or rewrite the file without its verdicts."""
    from .judgments import UnreadableJudgments

    try:
        yield
    except UnreadableJudgments as e:
        # Escaped: the message quotes the file's own text, and rich reads "[/]" in a verdict as
        # markup — crashing the refusal into a traceback, or silently eating "[supports]".
        con.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None

DATA = Path("data")
_warned_strays: set[Path] = set()   # candidate dirs whose stray cache/ was already reported

# Each candidate gets its own data dir (`data/<candidate>`) so their claims, retries and
# review progress never mix. The page cache is deliberately NOT per-candidate: the same
# article, filing, or roll call routinely covers more than one, and re-fetching it per run
# would cost time and, worse, could hand two runs different bytes for the same URL.
def _cache_root(data: Path, cache: Path | None) -> Path:
    """Where the shared page cache and CAL-ACCESS database live.

    This used to infer the root by asking whether `data.parent/cache` existed — so an unrelated
    stray `./cache/` at the repo root silently redirected the whole pipeline to a root with no
    CAL-ACCESS database, and 14 query citations failed with "not found" on a file that plainly
    existed. Inferring a root from a directory's existence fails open and gives a symptom that
    points nowhere near the cause.

    Rule now: an explicit --cache always wins; a candidate SUBDIR (data/<candidate>) uses its
    parent's shared cache whenever the parent is a data root (has a questions.json template),
    whether or not any cache/ exists yet on either side; anything else is its own root.
    """
    if cache is not None:
        # --cache names the directory that HOLDS cache/. Pointed at cache/ itself — the natural
        # `--cache data/cache` — a reader looked in data/cache/cache and found nothing, and
        # fetch or verify quietly created a second cache there. A cache directory holds pages/
        # or calaccess/; a root holds cache/.
        if any((cache / d).is_dir() for d in ("pages", "calaccess")):
            con.print(f"[red]--cache {escape(str(cache))} is a cache directory itself. --cache "
                      f"names the directory that holds cache/, so this looks like --cache "
                      f"{escape(str(cache.resolve().parent))}.[/]")
            raise typer.Exit(1)
        return cache
    # A candidate subdir shares its parent's cache BY DESIGN (data/<candidate> -> data/cache),
    # so the choice must not depend on any cache/ existing. The previous rule ("own cache wins
    # if it exists") was self-fulfilling: one fetch with --data data/<candidate> created
    # data/<candidate>/cache, the stray became authoritative, and it hid the CAL-ACCESS
    # database, failing all 14 query citations at once. Requiring the PARENT's cache/ would be
    # the same trap on a fresh clone, where data/cache is gitignored and absent, so the first
    # run forks before it exists.
    # questions.json is the race's question template, written in Phase 0 before any candidate
    # exists (new-candidate reads it); no fetch, query or build writes it, so no run flips this.
    parent = data.parent
    if data.name and (parent / "questions.json").exists():
        # Once per stray: `status` and `archive` resolve the root per source, and a warning
        # repeated 150 times scrolls away the output it was meant to annotate.
        if (data / "cache").exists() and data not in _warned_strays:
            _warned_strays.add(data)
            shared = parent / "cache"
            state = "" if shared.exists() else " (not created yet)"
            con.print(f"[yellow]{escape(str(data / 'cache'))} is a stray: this candidate "
                      f"shares {escape(str(shared))}{state}, so that is what is used. Move or "
                      f"merge anything the stray holds (pages, a CAL-ACCESS database) into it, "
                      f"or pass --cache.[/]")
        return parent
    return data


def _archive_records(data: Path) -> dict[str, dict]:
    """The run's archive records, or a readable exit if the file is damaged."""
    try:
        return arch.load_records(data)
    except ValueError as e:
        con.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None


def _warn_unrecorded_snapshots(claims_dir: Path, records: dict[str, dict]) -> None:
    """Say how many claim-file archive_urls no `vg archive` run recorded.

    None is ever used, and the first command to write claims back drops them, so this is
    the one moment the loss is visible — a run archived before the records existed would
    otherwise lose every snapshot link without a word. Reads the raw files: loading strips
    the field.
    """
    n = 0
    for p in claims_dir.glob("*.json"):
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            continue   # load_claims reports unreadable files
        for item in raw if isinstance(raw, list) else [raw]:
            for s in (item.get("sources") or []) if isinstance(item, dict) else []:
                if (isinstance(s, dict) and s.get("archive_url")
                        and s["archive_url"] != (records.get(s.get("url")) or {}).get("snapshot")):
                    n += 1
    if n:
        con.print(f"[yellow]{n} source(s) carry an archive_url no `vg archive` run recorded; "
                  f"it is ignored. Run `vg archive` to snapshot them.[/]")


def _verdict_cache_root(data: Path, cache: Path | None) -> Path:
    """`_cache_root()` for a command that only READS pages to check verdicts against them:
    judge, judgments, build, status.

    Nothing checked `--cache`: a root with no cache found no page, and every verdict passed. A
    verdict with no page now reads stale instead, which is safe but no more useful — a whole
    run reported as needing re-judging because of a typo. These commands never create a cache,
    so an explicit root without one can only be the wrong root: refuse it. (`_cache_root()`
    refuses `--cache` naming a cache directory itself, for every command.) fetch and verify
    create the cache on first use and do not come through here. Only `cache/` is required,
    not `cache/pages`: a run citing only queries needs the CAL-ACCESS database and no pages.
    """
    root = _cache_root(data, cache)
    if (root / "cache").is_dir():
        return root
    if cache is not None:
        con.print(f"[red]--cache {escape(str(cache))} holds no cache/ directory, so every "
                  f"verdict checked against it would read stale.[/]")
        raise typer.Exit(1)
    con.print(f"[yellow]no cache at {escape(str(root / 'cache'))}: every verdict on a cited page "
              f"will read stale until `vg verify` fetches the pages, or --cache names the "
              f"cache.[/]")
    return root


def _aliased_shards(data: Path, every: dict, claims: list[Claim]) -> dict[str, str]:
    """{claim id: shard stem} for shards the disk opens under a claim's id though they are named
    otherwise — Q1.json for claim q1 on a case-insensitive disk.

    Asked of the disk, not guessed from the names: on a case-sensitive one Q1.json is simply a
    shard no claim has, which `vg build` never reads. Where the disk opens it as q1.json, it is
    q1's shard, and `vg build` applies it to q1.
    """
    from . import judgments

    held = {c.question_id for c in claims}
    out: dict[str, str] = {}
    for stem in every:
        if stem not in held and (qid := judgments.opened_as(data, stem, held)):
            out[qid] = stem
    return out


def _shard_names(stems) -> str:
    return ", ".join(f"{q}.json" for q in sorted(stems, key=qid_sort_key))


def load_claims(claims_dir: Path, *, trust_machine_fields: bool = False,
                skipped: list[str] | None = None) -> list[Claim]:
    """Load claim files. Discards agent-writable verification data unless asked not to.

    Defaults to NOT trusting, so a new call site has to say out loud that it wants
    pipeline-owned fields — defaulting to trust is how this invariant erodes. Only pass
    `trust_machine_fields=True` where the data is re-checked (build) or merely displayed
    (status), never where it decides whether a citation counts as verified.

    Pass `skipped` to collect the question ids of unreadable claims, for a caller whose
    result would otherwise silently leave them out.
    """
    out: list[Claim] = []
    origin: dict[str, str] = {}
    skipped = [] if skipped is None else skipped
    for p in sorted(claims_dir.glob("*.json")):
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError, RecursionError) as e:
            # JSONDecodeError and UnicodeDecodeError are ValueErrors whose text names neither
            # file nor directory, so the operator was told "Expecting value" and nothing to open.
            raise ValueError(f"unreadable claim file {p}: {e}") from e
        for item in (raw if isinstance(raw, list) else [raw]):
            # A trusted load keeps verification (re-checked or displayed) but never the archive
            # fields: those come only from the run's records, via apply_archive().
            if isinstance(item, dict):
                item = strip_machine_fields(item, archive_only=trust_machine_fields)
            try:
                claim = Claim.model_validate(item)
            except Exception as e:  # noqa: BLE001  pydantic
                # One malformed row must not cost the run every other verified claim. Surface
                # it as work for the human instead of dying on the whole build. A row that is
                # not even an object (a bare string, a number) is skipped the same way.
                named = item.get("question_id") if isinstance(item, dict) else None
                qid = str(named or p.stem)[:64] or "unreadable"
                # Escaped: the id and pydantic's echo of the input are agent-authored text.
                con.print(f"[yellow]skipping {escape(qid)} in {escape(p.name)}:[/] "
                          f"{escape(str(e))}")
                skipped.append(qid)
                continue
            if claim.question_id in origin:
                # save_claims() writes <qid>.json, so a claim first written under another
                # filename leaves a duplicate behind. Silently loading both double-counts
                # the question in every status total and renders it twice for review.
                raise ValueError(
                    f"duplicate question_id {claim.question_id!r} in {origin[claim.question_id]} "
                    f"and {p.name} — delete the stale file (claims are stored as <qid>.json)")
            origin[claim.question_id] = p.name
            out.append(claim)
    if skipped:
        con.print(f"[yellow]{len(skipped)} claim(s) skipped as unreadable: "
                  f"{', '.join(skipped)} — fix or re-run those questions[/]")
    return sorted(out, key=lambda c: qid_sort_key(c.question_id))


def save_claims(claims: list[Claim], claims_dir: Path) -> None:
    """Write claim files back. `question_id` is schema-constrained to a safe filename
    shape (`models.QID_PATTERN`); this re-checks the resolved path anyway, because the
    cost of being wrong is overwriting a file outside the data directory."""
    base = claims_dir.resolve()
    for c in claims:
        path = (base / f"{c.question_id}.json").resolve()
        if path.parent != base:
            raise ValueError(f"refusing to write claim {c.question_id!r} outside {base}")
        path.write_text(c.model_dump_json(indent=2))


@app.command()
def fetch(url: str, data: Path = DATA, cache: Path = None, refresh: bool = False):
    """Fetch and cache a page; print what the verifier will see."""
    data = _cache_root(data, cache)
    p = fetch_url(url, data, refresh=refresh)
    # Nothing from the page is printed as markup: Rich reads "[[page 1]]" as a tag and prints
    # "[]", and drops "[sic]" — so the page breaks a researcher must cite by were invisible,
    # and a snippet copied from here would not be on the page.
    con.print(f"[bold]{p.status}[/] {escape(p.final_url)}")
    con.print(f"title: {escape(str(p.title))}  pdf: {p.is_pdf}  "
              f"paywall: {p.paywall_suspected}  chars: {len(p.text)}")
    if p.error:
        con.print(f"[red]error:[/] {escape(p.error)}")
    # A kept page carries its OLD status and text, so without this line a failed
    # `vg fetch --refresh` prints exactly like a successful one.
    if kept := kept_copy_note(p):
        con.print(f"[red]{escape(kept)}[/]")
    # Its markers print below like text, so a scan otherwise reads as an ordinary page.
    if no_text_layer(p):
        con.print(f"[yellow]{escape(NO_TEXT_LAYER)}[/]")
    elif blank := pages_without_text(p):
        # A partly scanned PDF: the text below is the typed pages only.
        con.print(f"[yellow]no text layer on {escape(page_list(blank))} "
                  "(scanned or blank) — a quote there needs `page` set to it[/]")
    # As Text, like a query value: escape() alone still lets ":ok:" become an emoji, and a
    # plain string wraps at 80 columns in an agent's shell, putting line breaks in a snippet.
    if p.text:
        con.print(Text(p.text[:1200]), soft_wrap=True)
    else:
        con.print("[dim](no text)[/]")


@app.command()
def check(url: str, snippet: str, data: Path = DATA, cache: Path = None, refresh: bool = False,
          page: int = None):
    """Ad-hoc: is this snippet on this page, exactly once? (Researchers self-check with this.)

    Runs the verifier's own snippet rules and page check, so it cannot pass a snippet or a page
    that `vg verify` fails, nor fail what it only flags: a paywall or a scan is flagged, a dead
    URL or a soft 404 is a failure. --page is the source's `page` locator: on a partly scanned
    PDF it is what tells a quote on a scanned page from one that isn't there. Source class
    needs the whole citation, so only `vg check-claim` checks that. Exits non-zero on a
    failure."""
    if problem := snippet_problem(snippet):
        con.print(f"[red]{problem[0]}: {escape(problem[1])}[/]")
        raise typer.Exit(1)
    data = _cache_root(data, cache)
    p = fetch_url(url, data, refresh=refresh)
    # Before the verdict: on a page kept under an older extraction, a miss may be the
    # extraction, and a researcher told only "not found" drops or swaps a real citation.
    if kept := kept_copy_note(p):
        con.print(f"[yellow]{escape(kept)}[/]")
    found = check_snippet(snippet, p, pdf_page=page)
    if found.match_count is not None:
        con.print(f"matches: [bold]{found.match_count}[/] ({found.mode or 'none'})")
    if found.status == "verified":
        con.print("[green]OK — literal and unique.[/]")
    elif found.status in ("pdf_normalized_match", "normalized_match"):
        con.print("[yellow]Only matches after normalization; Cmd-F may need a shorter fragment.[/]")
    elif found.status in ("could_not_verify_paywall", "human_review"):
        # Flagged, not failed — a paywall, or a scan with no text layer. "Do not cite this"
        # about either sends the researcher off to substitute a copy they can read.
        con.print(f"[yellow]{found.status}: {escape(found.reason)}[/]")
    else:
        con.print(f"[red]{found.status}: {escape(found.reason)}[/]")
        raise typer.Exit(1)


@app.command()
def races():
    """List available races."""
    for name in available_races():
        r = load_race(name)
        con.print(f"[bold]{name}[/]  {r.title}  sources: {', '.join(r.sources)}")


@app.command()
def verify(data: Path = DATA, cache: Path = None, refresh: bool = False, qid: str = "",
           race: str = ""):
    """Run deterministic verification over all claims."""
    cache_root = _cache_root(data, cache)
    r = load_race(race or None)
    rules = load_rules(tuple(r.sources))
    claims_dir = data / "claims"
    claims = [c for c in _load_or_exit(claims_dir)   # never trust: this run decides status
              if not qid or c.question_id == qid]
    if not claims:
        con.print("[yellow]No claims found in[/] " + str(claims_dir))
        raise typer.Exit(1)
    from . import judgments

    # Read every claim's verdicts before fetching anything: applied claim by claim below, a
    # malformed shard for a later claim would stop the run only after the earlier claims'
    # pages had been fetched.
    with _judgments_or_exit():
        recorded = {c.question_id: judgments.load(data, c.question_id) for c in claims}

    # Stripping on load dropped every archive_url; put the pipeline's own snapshots back so
    # this write-back doesn't delete them.
    records = _archive_records(data)
    _warn_unrecorded_snapshots(claims_dir, records)
    t = Table("qid", "status", "publisher", "detail", box=None)
    stale_verdicts: list[str] = []
    for c in claims:
        judgments.merge(judgments.verdicts_for(c, data, recorded[c.question_id],
                                               cache_root=cache_root))
        for s in c.sources:
            verify_source(s, cache_root, refresh=refresh, rules=rules)
            apply_archive(s, records, cache_root)
            # verify_source just reset a paywalled row; its snapshot is already cached, so
            # re-earn verified_via_archive here instead of losing it until `vg archive`.
            verify_against_archive(s, cache_root)
            st = s.verification.status
            if st in ("verified", "verified_via_archive"):
                color = "green"
            elif "paywall" in st or "normalized" in st:
                color = "yellow"
            elif st == "pending":
                color = "cyan"
            else:
                color = "red"
            t.add_row(c.question_id, f"[{color}]{st}[/]", escape(s.publisher),
                      escape((s.verification.reason or "")[:70]))
        # Checked after this run's fetches, not before: a --refresh or an extractor re-fetch
        # is exactly what makes a verdict stale, and a list from before it would omit those.
        stale_verdicts += [f"{c.question_id}/{s.sid}: {why}" for s, _j, why in
                           judgments.verdicts_for(c, data, recorded[c.question_id],
                                                  cache_root=cache_root) if why]
        check_corroboration(c)
        if c.corroboration_ok is False:
            t.add_row(c.question_id, "[red]corroboration[/]", "-",
                      escape(c.corroboration_note[:70]))
    save_claims(claims, claims_dir)
    con.print(t)
    # The re-check a changed query definition calls for: the figure was just re-run under the
    # new one, but the verdict on it was formed about the old calculation.
    _report_stale(stale_verdicts)


def _report_stale(stale: list[str]) -> None:
    if not stale:
        return
    con.print(f"[yellow]{len(stale)} verdict(s) predate the page they judged, or have no cached "
              f"page to check against, or judged a copy their context no longer comes from (a "
              f"replaced snapshot), or were formed under another query definition, and were "
              f"NOT applied — re-judge those sources (after `vg verify`, where the page is "
              f"missing):[/]")
    for x in stale[:8]:
        # Escaped: a reason can name a snapshot, whose URL embeds the agent-authored one.
        con.print(f"  {escape(x)}")


def _report_older_exports(claims: list[Claim], cache_root: Path,
                          recorded_by_qid: dict) -> None:
    """Say how many applied query verdicts were formed against an older CAL-ACCESS export
    (`judgments.older_export()` says why they stand), so the reviewer knows the data moved
    under them. Call it once statuses are settled: it counts only a verdict that was applied
    and survived revalidation on a row whose figure reproduced — a stale verdict is already
    reported as such, and a figure that moved is a failed row, not a standing verdict."""
    from . import judgments

    older = []
    for c in claims:
        for s in c.sources:
            j = recorded_by_qid.get(c.question_id, {}).get(s.sid)
            if (s.query is None or j is None or s.verification.support == "unreviewed"
                    or s.verification.status not in GOOD):
                continue
            if why := judgments.older_export(j, cache_root, s.query):
                older.append(f"{c.question_id}/{s.sid}: {why}")
    if older:
        con.print(f"[dim]{len(older)} query verdict(s) were formed against an older CAL-ACCESS "
                  f"export. Their figures still reproduce on this one, so they stand; e.g. "
                  f"{escape(older[0])}[/]")


@app.command()
def archive(data: Path = DATA, cache: Path = None, delay: float = 3.0):
    """Snapshot every cited URL to web.archive.org."""
    claims_dir = data / "claims"
    # Trusted only to carry existing verification statuses through. No archive field is read
    # from a claim file even so: one may be agent-authored, and keeping it where Save Page Now
    # failed is how a snapshot of a page the agent controls came to verify its quote. They come
    # from the run's own records below.
    claims = _load_or_exit(claims_dir, trust_machine_fields=True)
    try:
        records = arch.load_records(data)
    except ValueError as e:
        # This command is what rebuilds the records, so a damaged file must not stop it.
        aside = data / f"{arch.RECORDS}.damaged-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        (data / arch.RECORDS).rename(aside)
        con.print(f"[yellow]{escape(str(e))}\n  moved it to {escape(str(aside))}; "
                  "starting fresh[/]")
        records = {}
    # Every cited URL, even one already carrying a snapshot, with one source citing it.
    first = {}
    for c in claims:
        for s in c.sources:
            first.setdefault(s.url, s)
    urls = list(first)
    if not urls:
        con.print("Nothing to archive.")
        return
    if arch.have_credentials():
        con.print(f"Archiving {len(urls)} URL(s) [green](authenticated)[/]…")
    else:
        con.print(f"Archiving {len(urls)} URL(s) [yellow](anonymous — Save Page Now "
                  f"rate-limits hard; a full run left 58 of ~150 URLs unarchived)[/]\n"
                  f"  For a higher quota, get keys at https://archive.org/account/s3.php and "
                  f"export {arch.ACCESS_KEY_ENV} and {arch.SECRET_KEY_ENV}.")
    cache_root = _cache_root(data, cache)

    def unusable(u: str, snapshot: str) -> bool:
        """Whether an earlier snapshot of ours is already known not to be the cited page — a
        bot check, a capture of somewhere else, nothing readable. Never because a snippet is
        missing from it: that can be the citation's fault, and a good capture would be thrown
        away for it. Offline: `vg archive` fetched it when it was recorded."""
        try:
            snapshot = check_archive_url(snapshot)
        except ValueError:
            return True
        return junk_capture(first[u], snapshot, cache_root)   # asks only of the URL

    def progress(u: str, snap: str | None, err: str | None) -> None:
        prev = records.get(u) or {}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if snap and not err:
            records[u] = {"snapshot": snap, "error": None, "saved_at": now}
        elif prev.get("snapshot") and not (snap and unusable(u, prev["snapshot"])):
            # The save failed: keep the pipeline's OWN earlier snapshot, never one from a claim
            # file — and not the closest capture save() fell back to either, which nothing has
            # checked and which can be another URL's (a www. variant) where ours was good. It
            # is re-checked below like a fresh one, so a stale capture still has to hold the page.
            # Unless ours is known junk and there is a fallback to try: on a URL where Save
            # Page Now always fails (cal-access), keeping it would mean a bot-check capture
            # could never be replaced by a good one. The fallback is checked below too.
            records[u] = {"snapshot": prev["snapshot"], "error": err or "save failed",
                          "saved_at": prev.get("saved_at")}
        else:
            # `err` alongside a snapshot means an older capture stood in for the failed save;
            # it stays on the record so that is never mistaken for a fresh one.
            records[u] = {"snapshot": snap, "error": err or "save failed",
                          "saved_at": now if snap else None}
        # After every URL: a run takes seconds a URL, and an interrupted one should keep
        # what it saved.
        arch.save_records(data, records)
        con.print(f"  {'[green]saved[/]' if snap else '[red]fail[/]'} {escape(u)} "
                  f"{escape(err or '')}")

    arch.archive_all(urls, delay=delay, progress=progress)

    upgraded = 0
    tally: dict[str, int] = {}
    for c in claims:
        for s in c.sources:
            # Fetches each new snapshot, which caches it: later commands repeat this check
            # offline against the same copy.
            apply_archive(s, records, cache_root, fetch_missing=True)
            key = s.archive_status or "not archived"
            tally[key] = tally.get(key, 0) + 1
            if s.archive_status in ("archive_unusable", "archive_unconfirmed"):
                color = "red" if s.archive_status == "archive_unusable" else "yellow"
                con.print(f"  [{color}]{s.archive_status}[/] {escape(c.question_id)} "
                          f"{escape(s.publisher)}: {escape(s.archive_note or '')}")
            before = s.verification.status
            verify_against_archive(s, cache_root)
            if s.verification.status == before:
                continue
            if s.verification.status == "verified_via_archive":
                upgraded += 1
                con.print(f"  [green]snippet confirmed in snapshot[/] {escape(c.question_id)} "
                          f"{escape(s.publisher)}")
            else:
                # It was verified_via_archive against a snapshot this run replaced.
                con.print(f"  [yellow]snapshot no longer confirms the snippet[/] "
                          f"{escape(c.question_id)} {escape(s.publisher)}")
    save_claims(claims, claims_dir)
    con.print("sources: " + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())))
    if upgraded:
        con.print(f"{upgraded} paywalled source(s) verified against their snapshot.")
    _report_rearchived_verdicts(claims, data, cache_root)


def _report_rearchived_verdicts(claims: list[Claim], data: Path, cache_root: Path) -> None:
    """Say how many verdicts on archive rows this run left stale. A fresh snapshot is a new
    copy of the page, so every verdict on a row whose context comes from a replaced one goes
    stale — and `vg archive` runs after the judgment pass, so without this nothing says
    the pass has to run again for those rows. Called with the archives this run applied."""
    from . import judgments

    try:
        stale = [f"{c.question_id}/{s.sid}" for c in claims
                 for s, _j, why in judgments.verdicts_for(c, data, cache_root=cache_root)
                 if why and s.verification.status == "verified_via_archive"]
    except judgments.UnreadableJudgments as e:
        # The archive itself is done and written; an unreadable shard is every reader's to stop on.
        con.print(f"[yellow]could not check verdicts against the new snapshots: "
                  f"{escape(str(e))}[/]")
        return
    if stale:
        con.print(f"[yellow]{len(stale)} verdict(s) on archive-verified sources were about a "
                  f"snapshot this run replaced, so they are stale: run `vg judgments` and judge "
                  f"them again before `vg build` — e.g. {escape(', '.join(stale[:4]))}[/]")


def _apply_archives(claims: list[Claim], records: dict[str, dict], cache_root: Path) -> None:
    """Put the run's own snapshots on every source. Before any verdict is checked, too: one on
    a verified_via_archive row is about the snapshot, and without it the check has no page to
    compare, so the verdict reads stale."""
    for c in claims:
        for s in c.sources:
            apply_archive(s, records, cache_root)   # snapshots come from the run's records, not the file


def _settle(claims: list[Claim], data: Path, cache_root: Path,
            rules: dict[str, tuple[str, ...]], records: dict[str, dict]) -> tuple[list[str], dict]:
    """Settle every claim's status in the one order that works. Returns the stale verdicts, as
    `question/sid: why`, and the verdicts read, by question.

    Each step reads what the one before it produced: the run's snapshots go on first, since an
    archive row's verdict is checked against its snapshot and so is its context; then
    the recorded verdicts, each checked against the page it judged; revalidation checks every
    row against the cache, a verified_via_archive one against that snapshot; corroboration
    counts the sources revalidation left standing; and check_inputs() reads every input's
    finished status. Run out of order, a step reads what the claim file said instead — q36
    once rendered verified on an input build downgraded two steps later. Build and status both
    call this so the order lives in one place.
    """
    from . import judgments

    _apply_archives(claims, records, cache_root)
    with _judgments_or_exit():
        recorded = {c.question_id: judgments.load(data, c.question_id) for c in claims}
    stale = []
    for c in claims:   # apply_to(), on verdicts read once and reused for the export report
        stale += [f"{c.question_id}/{x}" for x in judgments.merge(
            judgments.verdicts_for(c, data, recorded[c.question_id], cache_root=cache_root))]
    for c in claims:
        for s in c.sources:
            revalidate_from_cache(s, cache_root, rules=rules)   # a status the pipeline didn't produce won't render green
        check_corroboration(c)
    for cycle in check_inputs(claims):   # a conclusion is not verified while an input isn't
        con.print(f"[yellow]derives_from cycle: {', '.join(sorted(cycle, key=qid_sort_key))} — "
                  f"none of these can be verified until one stops deriving from the others[/]")
    return stale, recorded


def _question_ids(data: Path, claims: list[Claim]) -> set[str] | None:
    """Check every claim against the question its id names in the run's questions.json, and
    print what breaks the stable-id rule (CLAUDE.md, "Question ids are stable and never
    reused"). Returns the ids of the claims that break it, or None when the question set can't
    be read, so that no claim could be checked.

    `vg build` never read questions.json, so a claim on a retired id rendered beside its
    replacement, and one on a reworded or reused id rendered under the new question, while every
    command exited 0. No question set at all is said out loud, since then nothing was checked.
    """
    from . import questions

    path = questions.find(data)
    if path is None:
        con.print(f"[yellow]no {questions.FILE} in {escape(str(data))} or "
                  f"{escape(str(data.parent))}, so no claim was checked against the question its "
                  f"id names[/]")
        return set()
    try:
        found = questions.check(claims, questions.load(path))
    except questions.UnreadableQuestions as e:
        con.print(f"[red]{escape(str(e))}[/]")   # escaped: it quotes the file's own ids
        return None
    where = escape(str(path))
    if found.pending:
        pairs = ", ".join(f"{q} from {old}" for q, old in found.pending)
        # Not "never applied": an older remap applied some without retiring them, and the file
        # can't say which.
        con.print(f"[yellow]{where} still declares maps_from ({escape(pairs)}), a migration for "
                  f"the retired `vg remap`. Nothing applies it now, and no claim moves, so each is "
                  f"checked against the question at the id it sits on. Delete the key once that "
                  f"is settled.[/]")
    if found.unlisted:
        # Each is named with what the advice below should be read against: the migration that
        # meant to move it (its research answered the mapped question), or the listed id it
        # differs from only in case (the fix is the id, not archiving the research).
        mapped = {old: q for q, old in found.pending}

        def named(q: str) -> str:
            if q in found.case_of:
                return f"{q} (the set has {found.case_of[q]}, which differs only in case)"
            return f"{q} (maps_from of {mapped[q]})" if q in mapped else q

        con.print(f"[red]{len(found.unlisted)} claim(s) sit on an id {where} does not list: "
                  f"{escape(', '.join(named(q) for q in found.unlisted))}. If the id was retired, "
                  f"move its claim from claims/ to claims-archive/ and its shard from "
                  f"judgments/ to judgments-archive/, and point any derives_from naming it at "
                  f"the new id. If the question is still asked, add it to {where} under that "
                  f"id: a candidate run's own copy is not updated when the template gains a "
                  f"question.[/]")
    if found.reworded:
        con.print(f"[red]{len(found.reworded)} claim(s) answer another question than {where} asks "
                  f"at their id. Ids are never reused or reworded: give the new question a new "
                  f"id, and retire this one as above. If the claim only misquotes its question, "
                  f"copy the exact text into its `question`.[/]")
        for qid, answered, asked in found.reworded:
            # As Text: both are agent-authored, and the difference may be a bracket or an emoji
            # code that markup would eat.
            con.print(Text(f"  {qid}: the claim answers {answered!r}\n"
                           f"  {' ' * len(qid)}  {questions.FILE} asks {asked!r}"),
                      soft_wrap=True)
    return found.failing


def _left_out(failing: set[str], where: str) -> None:
    """The gate's last word, after everything else a command prints, so it is not scrolled
    away: which claims were left out, and a failing exit."""
    con.print(f"[red]{len(failing)} claim(s) left out of {where} until they answer the question "
              f"their id names (above): {escape(', '.join(sorted(failing, key=qid_sort_key)))}[/]")
    raise typer.Exit(1)


@app.command()
def build(data: Path = DATA, cache: Path = None, race: str = "", candidate: str = "",
          title: str = ""):
    """Detect conflicts and render the review app."""
    from .conflicts import detect
    from .races import candidate as find_candidate

    # First, so that every way this build can stop short (a refusal below, a crash, a kill)
    # leaves no earlier render for `vg serve` to show as if it were this one.
    try:
        clear_render(data / "out")
    except OSError as e:
        con.print(f"[red]could not clear the previous render from {escape(str(data / 'out'))}: "
                  f"{escape(str(e))}. Remove it by hand: `vg serve` must not show a render this "
                  f"build did not produce.[/]")
        raise typer.Exit(1) from None
    r = load_race(race or None)
    # The title also keys the review app's saved progress, so it must name the candidate:
    # two candidates sharing a key would show each other's checkmarks.
    if not title:
        title = f"{find_candidate(r, candidate).name} — {r.title}" if candidate else r.title
    cache_root = _verdict_cache_root(data, cache)
    rules = load_rules(tuple(r.sources))

    # Trusted, then immediately re-checked: _settle() discards any status that cannot be
    # reproduced from the cached page, and replaces every support verdict with the recorded
    # one (or `unreviewed`).
    claims = _load_or_exit(data / "claims", trust_machine_fields=True)
    failing = _question_ids(data, claims)
    if failing is None:
        con.print("[red]review app not rendered: no claim can be checked against a question set "
                  "that can't be read.[/]")
        raise typer.Exit(1)
    # Left out before anything reads them: rendered, such a claim reads as an answer to a
    # question the run does not ask, or to one it was never researched for. The rest still
    # render, as load_claims() skips an unreadable claim: one mis-filed claim must not cost the
    # run every other one. A claim deriving from one left out reads its input as missing.
    claims = [c for c in claims if c.question_id not in failing]
    detect(claims)
    records = _archive_records(data)
    _warn_unrecorded_snapshots(data / "claims", records)
    stale_verdicts, recorded = _settle(claims, data, cache_root, rules, records)
    _report_stale(stale_verdicts)

    # A verdict recorded after the last verify has nothing to attach to yet, and the row reads
    # `unreviewed` — indistinguishable from never having been judged. Say so instead.
    jdir = data / "judgments"
    if jdir.exists():
        newest_j = max((f.stat().st_mtime for f in jdir.glob("*.json")), default=0)
        newest_c = max((f.stat().st_mtime for f in (data / "claims").glob("*.json")), default=0)
        if newest_j > newest_c:
            con.print("[yellow]judgments are newer than the claim files: run `vg verify` first, "
                      "or recent verdicts will render as unreviewed[/]")   # verdicts live outside the claim file; merge them in
    _report_older_exports(claims, cache_root, recorded)
    html, js = render(claims, data / "out", title=title, cache_root=cache_root)
    con.print(f"[green]wrote[/] {html}\n[green]wrote[/] {js}")
    if failing:
        _left_out(failing, "the review app")


@app.command()
def serve(data: Path = DATA, port: int = 8765, open_browser: bool = True):
    """Serve the review app on localhost (localStorage is unreliable on file:// origins)."""
    import functools
    import http.server
    import webbrowser

    out = (data / "out").resolve()
    if not (out / "review.html").exists():
        # A build removes the last render before it can refuse, so this is also what a refused
        # build leaves: say so, or the next person runs serve, not build, and never sees why.
        con.print(f"[red]No review.html in {escape(str(out))}: `vg build` has not rendered one, "
                  f"is still rendering one, or its last run refused and rendered nothing. Run `vg build` and fix what "
                  f"it reports.[/]")
        raise typer.Exit(1)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    url = f"http://127.0.0.1:{port}/review.html"
    con.print(f"Serving {out} at [bold]{url}[/]  (ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


calaccess_app = typer.Typer(help="CAL-ACCESS bulk export: California campaign finance")
app.add_typer(calaccess_app, name="calaccess")


@calaccess_app.command("build")
def calaccess_build(data: Path = DATA, cache: Path = None):
    """Load the downloaded export into SQLite (a few minutes).

    Download it first — it is ~1.5 GB, so the pipeline never fetches it implicitly:
      curl -L -o data/cache/calaccess/dbwebexport.zip \
        https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip
    (With --cache, the zip goes under <cache>/cache/calaccess/ instead.)
    """
    from . import calaccess

    root = _cache_root(data, cache)
    try:
        dbp = calaccess.build(root, progress=lambda t, n, note: con.print(
            f"  {t:32} {n:>9,} rows {note}"))
    except FileNotFoundError as e:
        con.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    con.print(f"[green]built[/] {dbp} ({_export_line(root)})")


def _export_line(root: Path) -> str:
    """Which export the database under `root` holds, in the review page's words."""
    from . import calaccess, queries

    info = calaccess.export_info(root)
    how = ", dated by download time" if info.get("export_date_from") == "download" else ""
    return queries.describe_export(info.get("export_date", "")) + how


@calaccess_app.command("filer")
def calaccess_filer(name: str, data: Path = DATA, cache: Path = None, limit: int = 25):
    """Find filer ids by name — the id every other query needs."""
    from . import calaccess

    try:
        rows = calaccess.find_filers(_cache_root(data, cache), name, limit)
    except FileNotFoundError as e:
        con.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    if not rows:
        con.print(f"No filer matching {name!r}.")
        return
    t = Table("filer id", "name", "committee page", box=None)
    for r in rows:
        who = " ".join(x for x in (r.get("first"), r.get("last")) if x)
        t.add_row(r["filer_id"], who, calaccess.committee_url(r["filer_id"]))
    con.print(t)


@calaccess_app.command("cite")
def calaccess_cite(filer_id: str, filing_id: str = "", data: Path = DATA, cache: Path = None,
                   session: str = "", year: str = ""):
    """Find a fetchable citation for a CAL-ACCESS page.

    The live pages are bot-protected, so a direct citation fails verification and researchers
    fall back to third-party mirrors. Many CAL-ACCESS pages have been snapshotted by broad
    crawls, and a snapshot is fetchable — so it can carry the official record instead.
    """
    from . import calaccess

    base = calaccess.committee_url(filer_id)
    if session:
        base = f"{base}&session={session}"
    targets = [("committee page", base)]
    if filing_id:
        targets.append(("filing", calaccess.filing_url(filing_id)))
    for label, url in targets:
        snap, note = calaccess.citable_snapshot(url, root=_cache_root(data, cache),
                                                expect_year=year)
        if snap:
            bad = calaccess.unusable(note)
            con.print(f"[{'red' if bad else 'green'}]{label}[/]: {snap}\n  {note}\n"
                      f"  live URL for the human: {url}")
        else:
            con.print(f"[yellow]{label}[/]: {note}\n  live URL: {url}")


@calaccess_app.command("contributions")
def calaccess_contributions(filer_id: str, data: Path = DATA, cache: Path = None, top: int = 25,
                            since: str = ""):
    """Largest contributions received by a filer.

    The URL column is the point: cite the filing page, never this table. A row here is a
    local copy with nothing a human can open or Cmd-F.
    """
    from . import calaccess

    try:
        # refuses a bad --since before it opens the database
        rows = calaccess.contributions_to(_cache_root(data, cache), filer_id, top=top, since=since)
    except (FileNotFoundError, ValueError) as e:
        con.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    t = Table("amount", "contributor", "employer", "date", "restated", "cite this URL",
              box=None)
    for c in rows:
        t.add_row(f"${c.amount:,.0f}", c.contributor[:30], c.occupation[:18] or c.employer[:18],
                  c.date, (f"{c.filings}x" if c.filings > 1 else ""), c.cite_url)
    con.print(t)
    con.print(f"\n[yellow]{len(rows)} rows. Cite the filing page, not this table — a row here "
              f"has no URL a human can check.[/]")


@calaccess_app.command("independent-expenditures")
def calaccess_ie(candidate_last: str, data: Path = DATA, cache: Path = None, first: str = "",
                 top: int = 50, loose: bool = False):
    """Late independent expenditures naming a candidate, with support/oppose.

    The surname matches exactly. --loose does a substring search, which can return committees
    for a different candidate whose surname merely contains this one — check every hit if you
    use it.
    """
    from . import calaccess

    try:
        rows = calaccess.independent_expenditures(_cache_root(data, cache), candidate_last,
                                                  first=first, top=top, loose=loose)
    except FileNotFoundError as e:
        con.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    t = Table("amount", "stance", "spender", "candidate", "date", "cite this URL", box=None)
    for r in rows:
        # A blank amount is money nobody stated: printed "$0" it read as a stated zero, which
        # ie_total refuses to report. Anything else that is not a number is shown as filed,
        # escaped, since it is filer text and rich would read "[/]" as markup.
        raw = (r.get("AMOUNT") or "").strip()
        try:
            amt = f"${float(raw):,.0f}" if raw else "blank"
        except ValueError:
            amt = escape(raw)
        t.add_row(amt, r["stance"], (r.get("FILER_NAML") or "")[:28],
                  " ".join(x for x in (r.get("CAND_NAMF"), r.get("CAND_NAML")) if x)[:22],
                  r.get("EXP_DATE") or "", r["cite_url"])
    con.print(t)


@app.command(name="query")
def run_query(name: str = typer.Argument(""), param: list[str] = None, data: Path = DATA,
              cache: Path = None):
    """Run a named data query — the direct alternative to hunting for text on a page.

    With no name, lists what can be asked. This is the same command the review app prints
    next to a query-backed citation, so a reviewer checks a number by re-running it rather
    than searching a web page for a string that may not appear there at all. The printed
    command carries --cache, so it reads the database the figure was verified against.
    """
    from . import queries

    if not name:
        t = Table("query", "version", "required params", "what it answers", box=None)
        for qname, q in sorted(queries.REGISTRY.items()):
            t.add_row(qname, f"v{q.version}", ", ".join(q.required), q.description)
        con.print(t)
        return
    params = dict(p.split("=", 1) for p in (param or []))
    root = _cache_root(data, cache)
    try:
        result = queries.run(name, params, root)
    except TypeError as e:
        required = q.required if (q := queries.REGISTRY.get(name)) else ()
        con.print(f"[red]{e}[/]\n  required: {', '.join(required)}")
        raise typer.Exit(1) from None
    except Exception as e:  # noqa: BLE001
        con.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    if not result.found:
        con.print(f"[yellow]no match[/] — {result.note}")
        raise typer.Exit(1)
    # Printed as Text, never as a markup string: researchers copy this value into `expected`
    # verbatim, and a str would lose "[b]" to markup and ":smile:" to emoji, and wrap at 80
    # columns when stdout is not a terminal (an agent's shell).
    con.print(Text(str(result.value), style="bold"), Text(f"({result.note})", style="dim"),
              sep="  ", soft_wrap=True)
    # What the review page prints beside the command, so a reviewer can see they reproduced
    # the figure under the same definition and against the same export — or that they didn't.
    where = f"{_export_line(root)}, " if queries.dataset(name) == "CAL-ACCESS" else ""
    con.print(Text(f"{name} v{result.version}; {where}root {root}", style="dim"),
              soft_wrap=True)


@app.command()
def judge(question_id: str, sid: str, verdict: str, note: str = "", data: Path = DATA,
          cache: Path = None):
    """Record a verifier agent's verdict on one source.

    Judgments live in data/judgments/, not in the claim file: `vg verify` reloads claims with
    stripping on — which is what stops a researcher self-certifying — so a verdict written
    into the claim is destroyed by the next verify run. Keyed by source id, so it follows the
    citation and lapses automatically when a retry changes the quote.

    verdict: supports | topic_only | contradicts | superseded

    Refuses, writing nothing, unless the named claim cites the source and the copy of the page
    `vg verify` built its context from is still the one cached. A verdict filed anywhere else
    is read by nothing — `q07` for `q7`, or a sid another claim cites — while the command
    reported success and the judgment pass looked done; one stamped from another copy describes
    text the verifier never read.
    """
    from . import judgments

    try:
        # Before reading anything, so an unrelated claims error can't stop the command first
        # and hide the refusal. Escaped: the message quotes the id, and `[/x]` in it is markup.
        judgments.path_for(data, question_id)
    except ValueError as e:
        con.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None

    def refuse(msg: str) -> NoReturn:
        con.print(f"[red]{msg}[/]")
        raise typer.Exit(1)

    cache_root = _verdict_cache_root(data, cache)
    skipped: list[str] = []
    claims = _load_or_exit(data / "claims", trust_machine_fields=True, skipped=skipped)
    claim = next((c for c in claims if c.question_id == question_id), None)
    qid = escape(question_id)
    if claim is None:
        if question_id in skipped:
            refuse(f"claim {qid} could not be read, so whether it cites {escape(sid)} cannot be "
                   f"checked — fix it first")
        # q07 for q7, Q7 for q7: the same question to a reader, a different shard to the code.
        # On a case-insensitive disk Q7.json even IS q7.json, which `vg judgments` then misses.
        near = [c.question_id for c in claims
                if qid_sort_key(c.question_id.lower()) == qid_sort_key(question_id.lower())]
        refuse(f"no {'readable ' if skipped else ''}claim has question id {qid} in "
               f"{escape(str(data / 'claims'))}"
               + (f" — did you mean {escape(', '.join(near))}?" if near else "")
               + (f" {len(skipped)} could not be read ({escape(', '.join(skipped))}), and it may "
                  f"be one of those: fix them first." if skipped else ""))
    source = next((s for s in claim.sources if s.sid == sid), None)
    if source is None:
        citing = [c.question_id for c in claims if any(s.sid == sid for s in c.sources)]
        refuse(f"claim {qid} does not cite source {escape(sid)}"
               + (f"; {escape(', '.join(citing))} does" if citing else
                  "; no current claim cites it — its citation may have changed since you were "
                  "given it"))
    with _judgments_or_exit():
        # Before the page check: an unreadable shard is what to fix first, since nothing can be
        # recorded into it whatever else is right.
        judgments.load(data, question_id)

    # Stamp the copy of the page the verifier read — the one `vg verify` built the context from
    # — so a later re-fetch can invalidate this verdict instead of leaving it to describe text
    # that no longer exists. Same root, same lookup as the check in `apply_to()`, or the stamp
    # describes a page the check never looks at.
    page_url, page_at, ver, query_ver, export = "", "", 0, 0, ""
    if source.query is None:
        # An archive-verified context comes from the snapshot the run's records name. Only
        # there: no other row's context depends on the records, and a damaged records file
        # should not stop every verdict in the run.
        if source.verification.status == "verified_via_archive":
            apply_archive(source, _archive_records(data), cache_root)
        try:
            page_url, page_at, ver = judgments.judged_copy(source, cache_root)
        except judgments.Unjudgeable as e:
            refuse(escape(str(e)))
    else:
        # A query citation has no page: its evidence is the query, so stamp its definition and
        # export instead — and only if that is what produced the context the verifier read.
        # `source` is this question's own citation (above): two questions citing one query
        # share its sid while each carries its own last run.
        if why := judgments.unjudgeable_query(source.query, source.verification.query_run,
                                              cache_root):
            refuse(f"not recorded: {escape(why)}")
        query_ver, export = judgments.query_stamp(cache_root, source.query)
    try:
        j = judgments.record(data, question_id, sid, verdict, note,
                             page_fetched_at=page_at, extractor_version=ver,
                             query_version=query_ver, export_date=export, page_url=page_url)
    except ValueError as e:
        con.print(f"[red]{escape(str(e))}[/]")   # it may quote the verdict file's own text
        raise typer.Exit(1) from None
    color = "green" if j.verdict == "supports" else "red"
    # Escaped: the note is agent-written, and a `[/]` in it raised after the verdict was on
    # disk — a non-zero exit that verifiers are told means nothing was recorded.
    con.print(f"[{color}]{j.verdict}[/] recorded for {escape(question_id)}/{escape(sid)}"
              + (f": {escape(note)}" if note else ""))


# A retired flag: still parsed, so an old invocation reaches _retired(), but not in --help. Given
# through Annotated so the Python default stays a plain value: tests call commands as functions,
# and a `= typer.Option(...)` default is an OptionInfo there, which is truthy. (Typer does not
# resolve a `type` alias, so each parameter spells out its Annotated.)
_HIDDEN = typer.Option(hidden=True)


@app.command(name="judgments")
def show_judgments(data: Path = DATA, question_id: str = "",
                   repair: Annotated[bool, _HIDDEN] = False, cache: Path = None,
                   rollback: Annotated[bool, _HIDDEN] = False,
                   moved: Annotated[list[str], _HIDDEN] = None,
                   gone: Annotated[list[str], _HIDDEN] = None):
    """Show recorded verdicts, and which cited sources still need one."""
    from . import judgments

    if repair or rollback or moved or gone:
        # Retired with `vg remap`: they re-homed verdicts after claims moved, and claims no longer
        # move. A backup an interrupted re-home left behind still stops every reader, naming the
        # checkout that undoes it, so say that first.
        with _judgments_or_exit():
            judgments.refuse_if_interrupted(data)
        given = [flag for flag, on in (("--repair", repair), ("--rollback", rollback),
                                       ("--moved", moved), ("--gone", gone)) if on]
        _retired(f"`vg judgments {' '.join(given)}`")

    cache_root = _verdict_cache_root(data, cache)
    skipped: list[str] = []
    claims = _load_or_exit(data / "claims", trust_machine_fields=True, skipped=skipped)
    # As build does, before the verdicts: an archive-verified row is checked against its
    # snapshot, both its verdict and its context, and without the record it has neither. Only
    # read where one exists, as `vg judge` does, so a damaged records file stops a run with
    # archive rows but not one without.
    records = (_archive_records(data) if any(s.verification.status == "verified_via_archive"
                                             for c in claims for s in c.sources) else {})
    with _judgments_or_exit():
        # Every shard, not only those named after current claims: this is the command that
        # shows the gaps, and a malformed shard left under an old id is one.
        every = judgments.load_every(data)
    for d in judgments.leftovers(data):
        con.print(f"[yellow]{escape(str(d))} is scratch an interrupted re-home by the retired "
                  f"`vg remap` left behind. Nothing reads it, and it holds at most an older copy "
                  f"of the run's verdicts: delete it, and never restore from it.[/]")
    selected = [c for c in claims if not question_id or c.question_id == question_id]
    for c in selected:
        for s in c.sources:
            # Only an archive row's verdict and context depend on the records, and checking a
            # snapshot reads it (and at times the live page) from the cache: skip the rest.
            if s.verification.status == "verified_via_archive":
                apply_archive(s, records, cache_root)
    unread = [q for q in skipped if not question_id or q == question_id]
    if not selected and not unread:
        # Nothing to count would print a green "0 of 0" — the done signal — for a typo'd
        # --question-id or --data.
        what = f"with question id {question_id!r}" if question_id else "at all"
        con.print(f"[red]no claim {what} in {data / 'claims'}[/]")
        raise typer.Exit(1)
    # A shard the disk opens under a claim's id is that claim's, as `vg build` reads it. On a
    # case-insensitive disk q1 opens Q1.json, and counting what build applies as unjudged sent
    # verifiers round a loop: re-judging as q1 writes into Q1.json, whose name never changes.
    aliased = _aliased_shards(data, every, claims)
    by_question = {c.question_id: every.get(c.question_id, every.get(aliased.get(c.question_id),
                                                                    {}))
                   for c in selected}
    # A shard named for no current claim is read by nothing — not build, not this count. A
    # mistyped `vg judge` id used to write one and report success.
    current = {c.question_id for c in claims} | set(skipped)
    unowned = {q: len(v) for q, v in every.items()
               if v and q not in current and q not in aliased.values()}
    t = Table("qid", "source", "verdict", "note", box=None)
    total = waiting = stale_waiting = stale = blocked = orphans = 0
    for c in selected:
        recorded = by_question[c.question_id]
        # A judgment whose sid no longer matches any cited source is dead weight: it was about
        # a citation that has since changed. Harmless, but invisible without saying so.
        live = {s.sid for s in c.sources}
        orphans += sum(1 for sid in recorded if sid not in live)
        # Run what `vg build` runs on each source, read-only as `vg status` does, so the count
        # is what the review app will show rather than a re-derivation of it — each of three
        # re-derivations disagreed with build somewhere.
        rows = list(judgments.verdicts_for(c, data, recorded, cache_root=cache_root))
        stale += len(judgments.merge(rows))
        for s, j, why in rows:
            last_run = s.verification.query_run   # the run `vg judge` checks, before build's
            seen = s.verification.context_page    # the copy it checks, likewise
            revalidate_from_cache(s, cache_root)
            total += 1
            unjudgeable = s.verification.support == "unreviewed" and (
                judgments.unjudgeable_query(s.query, last_run, cache_root) if s.query is not None
                else judgments.unjudgeable_page(s, seen, cache_root))
            if s.verification.support != "unreviewed":
                v = f"[{'green' if j.verdict == 'supports' else 'red'}]{j.verdict}[/]"
                note = j.note
            elif s.verification.status not in GOOD:
                # No confirmed context to judge (failed, paywalled, never verified). The gate
                # counts only what the judgment pass can close, or it never reaches 0.
                blocked += 1
                v = f"[dim]unreviewed ({s.verification.status})[/]"
                note = s.verification.reason
            elif j is not None and not why:
                # Revalidation dropped a usable verdict: the context moved since `vg verify`
                # wrote the claim file. Re-judging can't fix that; re-verifying does.
                blocked += 1
                v = "[dim]unreviewed (run vg verify)[/]"
                note = "verdict recorded, but the context changed since vg verify"
            elif unjudgeable:
                # `vg judge` would refuse it: the claim file's run predates this definition,
                # export or root, or the copy of the page its context came from is gone or
                # unnamed. Counted as needing a verdict, the gate could never reach 0 by
                # judging; re-verifying is what closes it.
                blocked += 1
                v = "[dim]unreviewed (run vg verify)[/]"
                note = unjudgeable
            else:
                waiting += 1
                stale_waiting += bool(why)
                v = f"[yellow]stale (was {j.verdict})[/]" if why else "[dim]unreviewed[/]"
                note = j.note if j else ""
            t.add_row(c.question_id, escape(f"{s.publisher} {s.sid}"), v,
                      escape((note or "")[:60]))
    con.print(t)

    if orphans:
        con.print(f"[dim]{orphans} recorded verdict(s) no longer match any cited source — "
                  f"their citation changed, so the judgment correctly lapsed.[/]")
    if aliased:
        # Which claim they belong to is not inferred from a source id but is what this disk
        # already does (judgments.opened_as()).
        con.print(f"[yellow]{escape(_shard_names(aliased.values()))} differ from a claim's id only "
                  f"in case, and this disk opens them under that id, so they count as that "
                  f"claim's — but a case-sensitive checkout of {escape(str(data))} reads nothing "
                  f"from them. Rename each to its claim's exact id by hand, through a temporary "
                  f"name.[/]")
    if unowned:
        # Question ids are stable, so a claim never moves off its shard: a shard no claim has
        # judged a claim that is gone, or was written under an id no claim ever had.
        con.print(f"[yellow]{sum(unowned.values())} verdict(s) sit in judgments/ under an id no "
                  f"claim has ({escape(_shard_names(unowned))}), so nothing reads them. Their "
                  f"claim is gone, or never had that id. Move each to judgments-archive/ to "
                  f"keep it, or put back the claim it judged. Do not delete one: that discards "
                  f"its verdicts.[/]")
    if blocked:
        con.print(f"[dim]{blocked} more source(s) have nothing a verifier can judge yet: the "
                  f"citation failed, is paywalled, was never verified, or changed since "
                  f"`vg verify`. That is the retry loop's, `vg archive`'s or `vg verify`'s job.[/]")
    _report_older_exports(selected, cache_root, by_question)
    if stale:
        # Not "all need judging again": one with no page to check against has no context to
        # judge either, so it sits with the blocked sources, and `vg judge` refuses it.
        con.print(f"[yellow]{stale} verdict(s) predate the page they judged, or have no cached "
                  f"page to check against, or judged a copy their context no longer comes from "
                  f"(a replaced snapshot), or were formed under another query definition, so "
                  f"`vg build` will not apply them. "
                  + (f"{stale_waiting} are in the count below and need judging again. "
                     if stale_waiting else "")
                  + (f"{stale - stale_waiting} have nothing a verifier can judge yet (above)."
                     if stale > stale_waiting else "") + "[/]")
    if unread:
        # No gate line at all: "0 of M" as the last line reads as done to anyone taking
        # `tail -1` through a pipe, which discards the exit status.
        con.print(f"\n[red]{len(unread)} claim(s) could not be read, so nothing here counts as "
                  f"done: {', '.join(unread)}[/]")
        raise typer.Exit(1)
    # Printed as one number rather than left to be counted off a rich table: that table wraps,
    # so `grep -c unreviewed` under-reported twice and read as "done" when a source genuinely
    # had no verdict. Last line, so it is also what `tail -1` finds.
    style = "green" if waiting == 0 else "yellow"
    con.print(f"\n[{style}]{waiting} of {total} cited source(s) need a verdict "
              f"({stale_waiting} stale)[/]")
    if waiting:
        # And a gate that fails, not only one that prints: exit 0 means the judgment pass is
        # done, for anyone checking `&&` rather than reading the number.
        raise typer.Exit(1)


@app.command(name="source-access")
def source_access(host: str = typer.Argument(""), run_recipe: str = "",
                  param: list[str] = None):
    """What we know about querying a site whose UI is a JS app.

    With no host, lists the registry. With a host, shows what a naive fetch gets, any
    working endpoint, and the known limits. `--run-recipe <id> --param k=v` executes one.

    Check here before concluding a source is unreachable — and before citing a copy from
    somewhere else, which is the failure this registry exists to prevent.
    """
    from . import access

    if not host:
        entries = access.load_all()
        if not entries:
            con.print("Registry is empty.")
            return
        t = Table("host", "access", "recipes", "what it is", box=None)
        for h, e in sorted(entries.items()):
            t.add_row(h, e.access, ", ".join(r.id for r in e.recipes) or "-", e.name)
        con.print(t)
        return

    entry = access.find(host)
    if entry is None:
        con.print(f"[yellow]Nothing recorded for {host}.[/]\n"
                  f"If you find a way in, record it: `vg source-import-curl <file>` after "
                  f"copying the request from dev tools. If it needs a login, record that too "
                  f"— a known dead end saves the next run from substituting silently.")
        raise typer.Exit(1)

    con.print(f"[bold]{entry.host}[/] — {entry.name}  ([bold]{entry.access}[/], "
              f"verified {entry.verified or 'unknown'})")
    if entry.naive_fetch:
        con.print(f"\n[dim]a plain fetch gets:[/] {entry.naive_fetch.strip()}")
    for r in entry.recipes:
        con.print(f"\n[bold]{r.id}[/] {r.summary}\n  {r.method} {r.url}"
                  + (f"\n  params: {', '.join(r.params)}" if r.params else "")
                  + (f"\n  {r.notes.strip()}" if r.notes else ""))
    if entry.limits:
        con.print(f"\n[yellow]limits:[/] {entry.limits.strip()}")
    if entry.manual_steps:
        con.print(f"\n[yellow]manual retrieval:[/] {entry.manual_steps.strip()}")

    if run_recipe:
        recipe = entry.recipe(run_recipe)
        if recipe is None:
            con.print(f"[red]no recipe {run_recipe!r}[/]")
            raise typer.Exit(1)
        params = dict(p.split("=", 1) for p in (param or []))
        try:
            resp = access.run(recipe, params)
        except Exception as e:  # noqa: BLE001
            con.print(f"[red]{type(e).__name__}: {e}[/]")
            raise typer.Exit(1) from None
        con.print(f"\n[bold]HTTP {resp.status_code}[/] {len(resp.text)} chars")
        con.print(resp.text[:1500])


@app.command(name="source-note")
def source_note(host: str, note: str, access: str = "", verified: str = ""):
    """Record an access finding that didn't come from a browser request.

    The Form 700 download flow was found by reading the portal's own script bundle, not by
    copying a cURL — so there was nowhere to put it and it nearly stayed in one session's
    head. Appends to the host's entry, creating a stub if there is none.
    """
    from . import access as access_mod

    entry_path = access_mod.REGISTRY / f"{access_mod._norm_host(host)}.yaml"
    if entry_path.exists():
        data = yaml.safe_load(entry_path.read_text()) or {}
    else:
        data = {"host": access_mod._norm_host(host), "name": "", "access": access or "unknown"}
    if access:
        data["access"] = access
    if verified:
        data["verified"] = verified
    data["findings"] = (data.get("findings") or "") + ("\n" if data.get("findings") else "") + note
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    con.print(f"[green]recorded[/] {entry_path}")


@app.command(name="source-import-curl")
def source_import_curl(path: Path, name: str = "", write: bool = True):
    """Turn a browser 'copy as cURL' into a registry entry, minus credentials.

    Cookies, auth and session headers in the paste are your live session. They are dropped
    here and never written to disk, and so is any header not known to be safe (each is
    named, to add back by hand if it is not a credential). `--user`, a login in the URL, or
    a curl option the importer doesn't know refuses the import. If the endpoint only works
    with a credential, it is a manual retrieval, not a pipeline capability: record it as
    `access: manual`.
    """
    from . import access

    try:
        parsed = access.parse_curl(path.read_text())
    except (OSError, ValueError) as e:
        con.print(f"[red]{escape(str(e))}[/]")
        raise typer.Exit(1) from None

    entry = parsed["entry"]
    entry["name"] = name or entry["name"]
    dropped = parsed["dropped_credentials"]
    if dropped:
        con.print(f"[yellow]dropped credentials:[/] {escape(', '.join(dropped))} — confirm the "
                  f"endpoint still works without them before relying on it")
    unknown = parsed["dropped_headers"]
    if unknown:
        con.print(f"[yellow]dropped headers not known to be safe:[/] {escape(', '.join(unknown))}"
                  f" — if the endpoint needs one and it is not a credential, add it by hand")

    dest = access.REGISTRY / f"{entry['host']}.yaml"
    text = yaml.safe_dump(entry, sort_keys=False, allow_unicode=True, width=100)
    if not write:
        con.print(text)
        return
    if dest.exists():
        con.print(f"[yellow]{dest} exists — printing instead of overwriting[/]\n")
        con.print(text)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    con.print(f"[green]wrote[/] {dest}\n"
              f"Fill in `summary`, `params`, and `notes`, then verify it works cookieless.")


@app.command(name="form700")
def form700(first: str, last: str):
    """List a filer's Form 700 filings, newest first, from the FPPC's own index.

    Use this before citing a Form 700. The portal is a JS app, so a fetcher cannot run the
    search a human runs — which is how a 2024 filing got cited while the 2025 one sat at the
    top of the results. This asks the same endpoint the portal calls.
    """
    from . import fppc

    try:
        filings = fppc.search(first, last)
    except Exception as e:  # noqa: BLE001
        con.print(f"[red]FPPC search failed: {type(e).__name__}: {e}[/]\n"
                  f"Do not substitute a copy silently — say the index was unreachable.")
        raise typer.Exit(1) from None
    if not filings:
        con.print(f"No Form 700 filings found for {first} {last}.")
        return
    t = Table("filed", "covers", "agency", "index id", box=None)
    for f in filings:
        t.add_row(f.filed_date, ", ".join(str(y) for y in f.filing_years),
                  "; ".join(f.agencies[:2]) + (" …" if len(f.agencies) > 2 else ""),
                  f.index_id)
    con.print(t)
    newest = filings[0]
    con.print(f"\n[bold]Most recent:[/] filed {newest.filed_date}, covering "
              f"{', '.join(str(y) for y in newest.filing_years)}.")
    con.print(f"Retrieve it at {fppc.PORTAL} (search {first} {last}); cite that one, and set "
              f"the source `date` to its filed date.")


@app.command(name="check-claim")
def check_claim(path: Path, data: Path = DATA, cache: Path = None, race: str = ""):
    """Validate one claim file before handing it on. Exits non-zero if anything fails.

    Researchers run this as their last step. The prose rules about snippet length and
    distinctiveness are necessary but demonstrably not sufficient — a one-word quote got
    written twice in testing — so this makes the check mechanical instead of a request.
    Every failure here is one the verifier would have raised anyway, minus a round trip.
    """
    from . import questions

    r = load_race(race or None)
    rules = load_rules(tuple(r.sources))
    cache_root = _cache_root(data, cache)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        con.print(f"[red]cannot read {path}: {e}[/]")
        raise typer.Exit(1) from None

    ok = True
    # The question set `vg build` will check this claim against: its run's, where the run is
    # the directory holding the claim's claims/. Not --data alone: a candidate run's claim is
    # checked with the default --data, whose set is the root template, not the retargeted copy.
    # Resolved: `vg check-claim q1.json` from inside claims/ has "" for a parent name.
    at = path.resolve()
    run = at.parent.parent if at.parent.name == "claims" else data
    asked_in, asked = questions.find(run), None
    if asked_in is None:
        con.print(f"[dim]no {questions.FILE} for {escape(str(run))}, so the question was not "
                  f"checked[/]")
    else:
        try:
            asked = questions.load(asked_in)
        except questions.UnreadableQuestions as e:
            ok = False
            con.print(f"[red]cannot check the question:[/] {escape(str(e))}")
    for item in (raw if isinstance(raw, list) else [raw]):
        try:
            claim = Claim.model_validate(strip_machine_fields(item))
        except Exception as e:  # noqa: BLE001
            con.print(f"[red]schema:[/] {escape(str(e))}")
            raise typer.Exit(1) from None
        # `vg build` refuses a claim whose question isn't the one its id names, so a researcher
        # who misquotes it hears here rather than stopping the whole run's review app.
        found = questions.check([claim], asked) if asked is not None else questions.Findings()
        qid = escape(claim.question_id)
        if listed := found.case_of.get(claim.question_id):
            ok = False
            con.print(f"  [red]question id[/] {qid} is not in {escape(str(asked_in))}, which "
                      f"has {escape(listed)}: they differ only in case\n"
                      f"      Use the question_id you were given, exactly.")
        elif found.unlisted:
            ok = False
            con.print(f"  [red]question id[/] {qid} is not in {escape(str(asked_in))}\n"
                      f"      Use the question_id you were given, exactly. If you did, the "
                      f"question set changed under you: report that, and do not edit it.")
        for _qid, answered, text in found.reworded:
            ok = False
            con.print(f"  [red]question[/] is not the one {escape(str(asked_in))} asks at {qid}\n"
                      f"      Copy it exactly as you were given it, into `question`:")
            con.print(Text(f"      yours: {answered!r}\n      asked: {text!r}"), soft_wrap=True)
        for src_ in claim.sources:
            verify_source(src_, cache_root, rules=rules)
            st = src_.verification.status
            cited = escape(f"{src_.publisher}: {src_.snippet!r}")
            if st in ("verified", "verified_via_archive", "could_not_verify_paywall"):
                con.print(f"  [green]{st}[/] {cited}")
            elif st in ("normalized_match", "pdf_normalized_match"):
                con.print(f"  [yellow]{st}[/] {cited}")
            else:
                ok = False
                con.print(f"  [red]{st}[/] {cited}\n"
                          f"      {escape(src_.verification.reason or '')}")
        for src_ in claim.sources:
            if secondary_host(src_, rules) and not (src_.secondary_host_ack or "").strip():
                ok = False
                con.print(f"  [red]secondary host[/] {domain(src_.url)} is not the authority "
                          f"that issues this record\n"
                          f"      Cite the issuing authority's own copy. If you genuinely "
                          f"cannot reach it, set `secondary_host_ack` saying what you could "
                          f"not reach and why this copy is the same document — but never "
                          f"substitute silently.")
            if missing_filing_date(src_):
                ok = False
                con.print(f"  [red]no filing date[/] {escape(src_.url)}\n"
                          f"      periodic filings are a series — set `date` to the filing's "
                          f"own date, and make sure it is the most recent one")
        check_corroboration(claim)
        if claim.corroboration_ok is False:
            ok = False
            con.print(f"  [red]corroboration[/] {escape(claim.corroboration_note)}")
    if not ok:
        con.print("[red]Not ready. Fix these and re-run — do not hand this on.[/]")
        raise typer.Exit(1)
    con.print("[green]All sources check out.[/]")


# Question ids are stable and never reused, so a claim never moves between ids and nothing has
# to re-file it or its verdicts (CLAUDE.md, "Question ids are stable and never reused"). The
# commands that did are retired. Each still answers, hidden, so an old script or habit learns why
# instead of meeting "No such command".
_STABLE_IDS = ("question ids are stable and never reused. A split or reworded question gets a "
               "new id, and the old id is retired, so claims never move between ids and "
               "nothing re-files them or their verdicts. See \"Question ids are stable and "
               "never reused\" in CLAUDE.md.")


def _retired(what: str) -> NoReturn:
    con.print(f"[red]{what} is retired:[/] {_STABLE_IDS}")
    raise typer.Exit(1)


@app.command(name="remap", hidden=True,
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def remap(data: Path = DATA):
    """Retired: question ids are stable, so there is no numbering to migrate."""
    from . import judgments

    # The run may be one an interrupted `vg remap --apply` left half-moved, and whoever retries
    # it needs the way back first, as `vg judgments --rollback` gives it.
    with _judgments_or_exit():
        judgments.refuse_if_interrupted(data)
    _retired("`vg remap`")


@app.command(name="new-candidate")
def new_candidate(candidate: str, data: Path = DATA, race: str = "",
                  questions: Path = None):
    """Scaffold data/<candidate>/ for a separate run.

    Each candidate is its own run: own claims, own retries, own review progress. Only the
    page cache is shared (data/cache), because the same filing or article routinely covers
    more than one candidate.
    """
    from .races import candidate as find_candidate

    r = load_race(race or None)
    c = find_candidate(r, candidate)
    root = data / c.id
    (root / "claims").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (data / "cache" / "pages").mkdir(parents=True, exist_ok=True)

    src_q = questions or (data / "questions.json")
    dest_q = root / "questions.json"
    if src_q.exists() and not dest_q.exists():
        qs = json.loads(src_q.read_text())
        for q in qs:
            # A new run has no earlier id space: a maps_from or mapped_from left from the
            # retired `vg remap` is another run's history, so it is not copied.
            q.pop("maps_from", None)
            q.pop("mapped_from", None)
            # The question set is written about a subject; retarget it rather than making
            # the researcher infer who "the candidate" is.
            for other in r.candidates:
                if other.id != c.id:
                    q["text"] = q["text"].replace(other.name, c.name)
            q["subject"] = c.id
        dest_q.write_text(json.dumps(qs, indent=1))
        con.print(f"[green]wrote[/] {dest_q} ({len(qs)} questions retargeted to {c.name})")
    elif dest_q.exists():
        con.print(f"{dest_q} already exists — left alone")

    con.print(f"[green]ready[/] {root}\n"
              f"  uv run vg verify --data {root}\n"
              f"  uv run vg build  --data {root} --candidate {c.id}")


@app.command()
def status(data: Path = DATA, cache: Path = None, race: str = ""):
    """Summary of where the run stands."""
    from .conflicts import detect

    claims = _load_or_exit(data / "claims", trust_machine_fields=True)
    # Even with no claims: a pending maps_from, or a question set nothing can read, is worth
    # settling before anyone researches on those ids.
    failing = _question_ids(data, claims)
    if failing is None:
        raise typer.Exit(1)   # as build renders nothing: no claim could be checked
    if not claims:
        con.print("No claims yet.")
        return
    # Run the same offline checks `vg build` runs, so the summary can't disagree with what
    # the review app will actually show. Nothing is written back — this is a read-only view.
    # That starts with leaving out the claims build leaves out.
    claims = [c for c in claims if c.question_id not in failing]

    # The lists build checks against. This is a summary, so an ambiguous races/ falls back
    # rather than raising — but loudly, since a narrower list can pass rows build rejects.
    try:
        rules = load_rules(tuple(load_race(race or None).sources))
    except (FileNotFoundError, ValueError) as e:
        con.print(f"[yellow]{e} — checking against the `us` source list only, which can pass "
                  f"rows `vg build --race` rejects[/]")
        rules = load_rules(("us",))
    cache_root = _verdict_cache_root(data, cache)
    detect(claims)
    _settle(claims, data, cache_root, rules, _archive_records(data))
    t = Table("qid", "type", "status", "sources", "corroboration", "conflicts", box=None)
    for c in claims:
        t.add_row(c.question_id, c.claim_type, c.status, str(len(c.sources)),
                  c.corroboration_note or "-", str(len(c.conflicts)))
    con.print(t)
    if failing:
        _left_out(failing, "this summary, as from the review app")


if __name__ == "__main__":
    app()
