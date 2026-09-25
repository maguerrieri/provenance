"""Deterministic verification. No model in this loop.

Order matters: literal match first, because the human's Cmd-F is literal. Normalized
matching is a documented downgrade (flagged in the output), never a silent one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .archive import snapshot_of, wayback_target
from .fetch import (
    PDF_PAGE_MARKER,
    fetch,
    has_text,
    kept_copy_note,
    load_cached,
    no_text_layer,
    pages_without_text,
    unreadable_reason,
)
from .models import (
    ArchiveStatus,
    Claim,
    PageCache,
    PageCopy,
    QueryRun,
    Source,
    Verification,
    check_archive_url,
)
from .normalize import context_window, dehyphenate, find_all, normalize
from .sources import check_source_class, classify, domain

# Hosts that publish filings as a SERIES. A superseded filing verifies perfectly — same
# host, same institutional author, snippet genuinely present — so the only mechanical grip
# on recency is requiring the filing's own date. Without it, neither the verifier agent nor
# the human can tell which form they are looking at.
FILING_HOSTS = (
    "fppc.ca.gov", "cal-access.sos.ca.gov", "powersearch.sos.ca.gov", "sos.ca.gov",
    "fec.gov", "sec.gov",
)

MIN_SNIPPET_WORDS = 3
# A word count is a proxy for distinctiveness, and it was the wrong one. It rejected the
# parcel number "000-111-222-000" — unique on the page and exactly what a human would Cmd-F —
# and the retry came back with a span running across a line break, which passes the pipeline
# and fails a human in Preview. What actually matters is that the span is unambiguous, and
# uniqueness (checked below, against the real page) measures that directly. So a short
# snippet is allowed when it is long enough to be a real identifier.
MIN_SNIPPET_CHARS = 8
MAX_SNIPPET_WORDS = 25


def _locate(page: PageCache, snippet: str) -> tuple[str | None, int, int, int]:
    """Return (mode, count, start, end) in RAW page-text offsets.

    mode: 'exact' | 'normalized' | None

    On a PDF, a hit that touches one of our [[page N]] markers is not a hit: that text is
    ours, not the document's, and Cmd-F in a PDF viewer will never find it. It doesn't count
    toward uniqueness either, for the same reason.
    """
    marks = [m.span() for m in PDF_PAGE_MARKER.finditer(page.text)] if page.is_pdf else []

    def in_document(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(s, e) for s, e in spans if not any(ms < e and s < me for ms, me in marks)]

    hits = in_document([(h, h + len(snippet)) for h in find_all(page.text, snippet)])
    if hits:
        return "exact", len(hits), *hits[0]

    norm_text, idx = normalize(page.text)
    norm_snip, _ = normalize(snippet)
    if not norm_snip:
        return None, 0, -1, -1

    def raw_spans(starts: list[int]) -> list[tuple[int, int]]:
        return in_document([(idx[s], idx[s + len(norm_snip) - 1] + 1) for s in starts])

    nhits = raw_spans(find_all(norm_text, norm_snip))
    if not nhits and page.is_pdf:
        # Last resort for PDFs: rejoin words the layout broke across a line.
        norm_text, idx = dehyphenate(norm_text, idx)
        nhits = raw_spans(find_all(norm_text, norm_snip))
    if not nhits:
        return None, 0, -1, -1
    return "normalized", len(nhits), *nhits[0]


def verify_query_source(src: Source, root: Path) -> Source:
    """Verify a citation by re-running its query and comparing the result.

    Deterministic and reproducible — no page to fetch, nothing to go stale between the run and
    the review. A mismatch is a hard failure: the number in the claim is not the number the
    data gives.
    """
    from . import queries

    v = Verification(checked_at=datetime.now(UTC), attempts=src.verification.attempts + 1)
    q = src.query
    try:
        result = queries.run(q.name, dict(q.params), root)
        run = _query_run(result, root)
    except Exception as e:  # noqa: BLE001
        v.status = "fetch_failed"
        v.reason = f"query {q.name!r} failed: {e}"
        src.verification = v
        return src

    if not result.found and (why := result.unsettled):
        # Nothing counted, and every match with a readable amount is on a schedule a later
        # amendment left out. A retry of these parameters cannot reproduce it, and a retry
        # invites a mirror; only the filing says whether the rows stand, so a person opens it.
        # The claimed figure is named, since nothing here checks it against the filing.
        v.query_run = run
        v.status = "human_review"
        v.reason = (f"the query counts nothing ({result.note}), so nothing here reproduces the "
                    f"claimed {q.expected!r}, but {why} Re-run: "
                    f"{queries.human_command(q.name, dict(q.params), run.cache_root)}")
        src.verification = v
        return src
    if not result.found or not queries.matches(q.expected, result.value):
        # Stamped on a mismatch too, for the re-run command in the reason and for `vg judge`.
        # The review page does not show it: build does not re-run a failed row, so it drops a
        # stamp it cannot vouch for (revalidate_from_cache) and prints its own root.
        v.query_run = run
        v.status = "snippet_not_found"
        v.reason = (f"query returned {result.value!r} ({result.note}); the claim says "
                    f"{q.expected!r}. Re-run: "
                    f"{queries.human_command(q.name, dict(q.params), run.cache_root)}")
        src.verification = v
        return src
    if why := result.unsettled:
        # The number reproduces, but the record it comes from is not settled: it counts rows a
        # later amendment may have withdrawn, leaves out rows one may have, or leaves out a late
        # report no schedule A restates yet. Only the filings it names say which, so a person
        # opens them; stamped as for a mismatch.
        v.query_run = run
        v.status = "human_review"
        v.reason = (f"the query reproduces {result.value!r}, but {why} Re-run: "
                    f"{queries.human_command(q.name, dict(q.params), run.cache_root)}")
        src.verification = v
        return src

    _mark_query_verified(v, q, result, run)
    src.verification = v
    return src


def _query_run(result, root: Path) -> QueryRun:
    """Which definition, export and database produced `result` — from `run()` and the root it
    was handed, never from the claim file. Raises ValueError for a root no pasted command could
    name, which the caller turns into a failed row rather than a crashed run."""
    from . import queries

    if queries.unprintable(str(root)):
        raise ValueError("the cache root contains a control or invisible character, so no "
                         "command a reviewer pastes could name it; rename the directory")
    return QueryRun(version=result.version, export_date=result.export_date,
                    cache_root=str(root))


def _mark_query_verified(v: Verification, q, result, run: QueryRun) -> None:
    """Write the evidence a reproduced query vouches for. Every field the review app draws is
    set here, so nothing a claim file said about this row survives into it."""
    from . import queries

    v.status = "verified"
    v.query_run = run
    v.reason = ("verified by re-running the query, not by text search — check it yourself: "
                f"{queries.human_command(q.name, dict(q.params), v.query_run.cache_root)}")
    v.context = (f"{q.name}({', '.join(f'{k}={val}' for k, val in q.params.items())}) "
                 f"= {result.value}  [{result.note}]")
    v.context_offset = (0, len(str(q.name)))
    v.matched_offset = None
    v.match_count = None
    v.context_page = None      # no page: the context is the query's own result


# --- The offline check ---------------------------------------------------------------------
# `vg verify` and `vg build` must agree on what a citation needs. Build's re-check used to ask
# only whether the snippet was uniquely on the cached page, so a forged `verified` rode through
# on an excluded aggregator, a blog, or a soft 404 echoing the quote — every rule verify
# enforces beyond presence was one build never re-ran. Both now call these two functions, and
# neither touches the network: verify fetches the page before check_page(), build reads the
# cache. A rule added anywhere else is a rule build will not enforce.


def citation_problem(src: Source,
                     rules: dict[str, tuple[str, ...]] | None = None) -> tuple[str, str] | None:
    """(status, reason) for a rule the citation breaks before any page is consulted, or None.

    Source class and snippet shape. Checked first so verify never spends a fetch on a
    citation it would refuse anyway."""
    ok, reason = check_source_class(src, rules)
    if not ok:
        return "bad_source_class", reason
    return snippet_problem(src.snippet)


def snippet_problem(snippet: str) -> tuple[str, str] | None:
    """citation_problem()'s snippet-shape rules, which need neither a page nor the rest of the
    citation — so `vg check` runs them on a bare snippet too."""
    # Normalized first, so "[[PAGE 7]]" and a marker with odd spacing are caught too. A part of
    # one ("page 7]]") is caught at search time instead: _locate() drops hits on a marker.
    if m := PDF_PAGE_MARKER.search(normalize(snippet)[0]):
        return "snippet_not_found", (
            f"snippet contains {m.group()}, a page locator the pipeline inserts into PDF text — "
            "it is not in the document, and Cmd-F will not find it. Quote the document's own "
            "words, on one page, and put the page number in `page`")
    words = len(snippet.split())
    if words < MIN_SNIPPET_WORDS and len(snippet.strip()) < MIN_SNIPPET_CHARS:
        return "snippet_too_short", (
            f"snippet is {words} word(s) and {len(snippet.strip())} characters — too "
            "short to be distinctive. An identifier (a case number, a parcel number, a bill "
            "number) is fine; a common word is not")
    if words > MAX_SNIPPET_WORDS:
        return "human_review", (
            f"snippet is {words} words; long quotes break Cmd-F on smart quotes and line "
            "breaks — use two short snippets instead")

    if "\n" in snippet or "  " in snippet:
        return "human_review", (
            "snippet spans a line break or doubled space — it may match our extracted text and "
            "still fail the human's Cmd-F in a PDF viewer or browser. Pick a span from a single "
            "line")
    return None


MATCHED = ("verified", "pdf_normalized_match", "normalized_match")

# A scanned filing serves fine and extracts to nothing but page markers. Searching that finds
# nothing, and "snippet not found" would tell the researcher a real citation to a real filing
# was fabricated — which is exactly what pushes them to substitute some copy they can read.
# Not fetch_failed either: the fetch worked, and that label invites the same swap.
_TEXT_LAYER_COPY = ("cite a copy that has a text layer, with `secondary_host_ack` saying why it "
                    "is the same document")


def no_text_layer_reason(on_page: int | None = None) -> str:
    """Why a scan is `human_review`: the whole PDF, or just the cited page of a partly scanned
    one. Both open with "PDF has no text layer", which the orchestration keys on.

    A single page with no text may be blank rather than scanned, and nothing here can tell
    which. So the per-page reason tells the human reading it what a blank page means: pointing
    `page` at one turns a miss into this row, and the human is the check on that."""
    if on_page is None:
        return ("PDF has no text layer (image-only or scanned), so there is nothing to search "
                "— this is not evidence the citation is wrong. Open the PDF and confirm the "
                "quote by eye, then either keep this citation with a `page` locator so a human "
                f"can check it there, or {_TEXT_LAYER_COPY}")
    return (f"PDF has no text layer on page {on_page} (scanned, or blank), so there is nothing "
            "to search there — this is not evidence the citation is wrong. Open the PDF at page "
            f"{on_page}: if it is a scan, confirm the quote by eye and keep this citation, or "
            f"{_TEXT_LAYER_COPY}; if the page is blank, the quote is not on it")


NO_TEXT_LAYER = no_text_layer_reason()


def page_list(pages: list[int], limit: int = 8) -> str:
    """[2, 3, 4, 6] -> "pages 2–4, 6". At most `limit` runs, then a count: a duplex scan with
    every back page blank would otherwise put 150 numbers into every miss reason."""
    runs: list[list[int]] = []
    for n in pages:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    body = ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs[:limit])
    if rest := sum(b - a + 1 for a, b in runs[limit:]):
        body += f" and {rest} more"
    return f"page{'s' if len(pages) > 1 else ''} {body}"


def _scanned_pages_hint(pages: list[int]) -> str:
    """For a miss on a PDF with text-less pages and no `page` pointing at one: where a scan
    could be hiding the quote. The miss stands — a blank page is common, and downgrading every
    miss on one would weaken the fabricated-quote check — but the researcher learns what to do
    if the quote really is on one of them."""
    many = len(pages) > 1
    return (f"; {page_list(pages)} {'have' if many else 'has'} no text layer — if you read the "
            f"quote on {'one of them' if many else 'it'}, set `page` to that page and a human "
            "will check it there")


@dataclass
class PageCheck:
    """What check_page() found. `start`/`end` are raw page-text offsets, set only on a match."""

    status: str
    reason: str | None = None
    paywall: bool = False
    mode: str | None = None          # 'exact' | 'normalized' | None
    match_count: int | None = None   # None when the page never got as far as a search
    start: int = -1
    end: int = -1


def check_page(src: Source, page: PageCache) -> PageCheck:
    """Served status, presence and uniqueness of the snippet on a page already in hand."""
    found = check_snippet(src.snippet, page, pdf_page=src.page)
    # A page kept after a failed re-fetch says so on the row. The review app and the retry
    # loop read the reason, not the log, and a miss against an older extraction would
    # otherwise read as a research failure rather than a stale page. First, not last:
    # `vg verify` prints only the head of a reason.
    if note := kept_copy_note(page):
        found.reason = f"{note} — {found.reason}" if found.reason else note
    return found


def check_snippet(snippet: str, page: PageCache, *, pdf_page: int | None = None) -> PageCheck:
    """check_page() without the kept-copy note: everything it decides needs only the snippet,
    and the `page` locator a source may carry. `vg check` calls this too, so a researcher's
    self-check and the verifier cannot triage a page differently."""
    # A citation must point at a page that actually served. An error page can carry the
    # snippet (a soft 404 echoing the query, a "not found" page quoting the article title)
    # and would otherwise verify green. 401/402/403 still route to the paywall path below,
    # which is a different thing: the page exists, we just can't read it.
    served = 200 <= page.status < 400 or page.status in (401, 402, 403)
    if page.error or not served or not has_text(page):
        if page.paywall_suspected or page.status in (401, 402, 403):
            return PageCheck("could_not_verify_paywall",
                             f"HTTP {page.status}; page body not retrievable — check via "
                             "archive snapshot", paywall=True)
        if no_text_layer(page):
            return PageCheck("human_review", NO_TEXT_LAYER)
        return PageCheck("fetch_failed",
                         page.error or f"HTTP {page.status}, {len(page.text)} chars extracted")

    mode, count, start, end = _locate(page, snippet)
    if mode is None:
        gated = page.status in (401, 402, 403)
        # has_text() is whole-document: a typed cover page makes a partly scanned PDF
        # searchable, and a quote on one of its scanned pages is simply not in the text. Ahead
        # of the paywall heuristic, which on a PDF that served is only a phrase in its text: a
        # scanned cited page is the more specific answer, and a snapshot can't help with it.
        blank = pages_without_text(page)
        if pdf_page in blank and not gated:
            return PageCheck("human_review", no_text_layer_reason(pdf_page), match_count=count)
        # A 401/402/403 that still returned a body is a gate, not a bad citation — the
        # served text is the "subscribe" page, not the article.
        if page.paywall_suspected or gated:
            return PageCheck("could_not_verify_paywall",
                             "snippet not in retrievable text; page appears paywalled or gated",
                             paywall=True, match_count=count)
        return PageCheck("snippet_not_found",
                         "snippet does not appear on the page — the citation is wrong, the "
                         "page changed, or the quote was reconstructed rather than copied"
                         + (_scanned_pages_hint(blank) if blank else ""),
                         match_count=count)

    if count > 1:
        return PageCheck("snippet_not_unique",
                         f"snippet appears {count} times; Cmd-F would be ambiguous — pick a "
                         "more distinctive span", mode=mode, match_count=count)

    if mode == "exact":
        return PageCheck("verified", mode=mode, match_count=count, start=start, end=end)
    return PageCheck("pdf_normalized_match" if page.is_pdf else "normalized_match",
                     "matched only after normalizing whitespace/quotes/dashes — Cmd-F may need "
                     "a shorter fragment", mode=mode, match_count=count, start=start, end=end)


def verify_source(src: Source, root: Path, *, refresh: bool = False,
                  rules: dict[str, tuple[str, ...]] | None = None) -> Source:
    prior = src.verification
    v = Verification(checked_at=datetime.now(UTC), attempts=prior.attempts + 1)
    # Recomputed from scratch each run: a stale flag from a previous attempt would keep
    # routing this source to extra-care handling long after the paywall came down.
    src.paywall = False

    if src.query is not None:
        return verify_query_source(src, root)

    problem = citation_problem(src, rules)
    if problem:
        v.status, v.reason = problem
        src.verification = v
        return src

    page = fetch(src.url, root, refresh=refresh)
    found = check_page(src, page)
    v.status, v.reason, v.match_count = found.status, found.reason, found.match_count
    # Deliberately NOT set from the fetch heuristic alone: a thin page trips the
    # short-body check, and badging a readable source as paywalled sends the human off to
    # an archive snapshot for no reason. check_page() sets it only where the text really was
    # unreadable — locating the snippet is itself proof that it wasn't.
    src.paywall = found.paywall
    if found.status not in MATCHED:
        src.verification = v
        return src

    start, end = found.start, found.end
    excerpt, rs, re_ = context_window(page.text, start, end)
    v.matched_offset = start
    v.context = excerpt
    v.context_offset = (rs, re_)
    v.context_page = _copy_of(src.url, page)
    same_place = (prior.context == excerpt
                  and prior.matched_offset == start
                  and prior.context_offset == (rs, re_))
    if prior.support != "unreviewed" and same_place:
        # Carry the verifier agent's judgment forward: it's the expensive half to produce
        # and it's still about the same words in the same place. Matching the excerpt
        # alone isn't enough — two nearby phrases share a context window, so a retry that
        # swapped the snippet for its neighbour would inherit a verdict about the other
        # one. Require the offsets to agree too.
        v.support = prior.support
        v.support_note = prior.support_note
    if page.is_pdf and src.page is None:
        # Recover the page locator so the human can jump straight there: the last marker before
        # the match, parsed by the one pattern that defines a marker.
        before = list(PDF_PAGE_MARKER.finditer(page.text, 0, start))
        if before:
            src.page = int(before[-1].group(1))
    src.verification = v
    return src


def _copy_of(url: str, page: PageCache) -> PageCopy:
    """Name the copy of `url` a context is being built from, as `vg judge` will look it up."""
    return PageCopy(url=url, fetched_at=page.fetched_at, extractor_version=page.extractor_version)


GOOD = MATCHED + ("verified_via_archive",)
# What check_corroboration() counts as evidence, and so what revalidate_from_cache() must
# re-derive: one list, or a status could count toward corroboration without being re-checked.
USABLE = GOOD + ("could_not_verify_paywall",)


def _archive_reason(mode: str | None, snapshot: PageCache) -> str:
    reason = ("live page not readable (paywall); snippet confirmed in the archive snapshot — "
              "read the context here, Cmd-F on the live page will not work")
    if mode != "exact":
        # The invariant holds here too: a normalized-only hit is never presented as if
        # Cmd-F would find it verbatim.
        reason += (" (matched only after normalizing whitespace/quotes/dashes — Cmd-F in the "
                   "snapshot may need a shorter fragment)")
    # Built here rather than taken from check_page(), so the kept-copy note goes on here too.
    if note := kept_copy_note(snapshot):
        reason = f"{note} — {reason}"
    return reason

# Bot checks that Save Page Now captures in place of the page. Named, because "the snapshot
# is a Cloudflare challenge" tells the reviewer what happened and "snapshot unusable" does
# not. cal-access serves the Incapsula one to every non-browser client, and SPN reports
# success on it. Only matched on short pages: an interstitial is a few lines, and a long
# article that merely mentions one of these phrases is not one.
BOT_CHECKS = (
    ("an Incapsula bot check",
     r"incapsula incident id|request unsuccessful\. incapsula|_incapsula_resource"),
    ("a Cloudflare challenge",
     r"just a moment\.\.\.|attention required! \| cloudflare|cf-browser-verification|"
     r"cloudflare ray id|why have i been blocked|enable javascript and cookies to continue|"
     r"checking if the site connection is secure"),
    ("an Imperva/Distil bot check", r"pardon our interruption"),
    ("a browser check",
     r"checking your browser|verify(?:ing)? (?:that )?you are (?:a )?human|"
     r"are you a robot|please complete the security check|ddos protection by"),
)
_BOT_CHECK_MAX_CHARS = 3000


def bot_check(page: PageCache) -> str | None:
    """Name the bot-check interstitial this page is, or None."""
    text = page.text or ""
    if len(text) > _BOT_CHECK_MAX_CHARS:
        return None   # too long to be an interstitial, whatever its title says
    hay = f"{page.title or ''}\n{text}".lower()
    for name, pattern in BOT_CHECKS:
        if re.search(pattern, hay):
            return name
    return None


def _is_capture_of(root: Path, url: str, snapshot: str) -> bool:
    """`archive.snapshot_of`, also admitting a redirect the pipeline's own live fetch of `url`
    followed. A capture of a redirecting URL is filed under the page it redirects to, and that
    is still the cited page — the pipeline saw the redirect itself, so no agent chose it. The
    cached live page is only read when the direct match fails: it can be megabytes."""
    if snapshot_of(url, snapshot):
        return True
    live = load_cached(root, url)
    return bool(live is not None and live.final_url
                and snapshot_of(url, snapshot, also=(live.final_url,)))


def _landed_elsewhere(root: Path, src: Source, page: PageCache) -> str | None:
    """Why a fetched snapshot, after its redirects, is not a capture of the cited page — or
    None. Fetches follow redirects, and the Wayback Machine redirects to the nearest capture,
    which replays whatever that capture recorded: a redirect to some other page included. The
    target check on the requested URL says nothing about where the fetch ended up."""
    final = page.final_url or page.url
    if final == page.url or _is_capture_of(root, src.url, final):
        return None
    return f"it redirected to {final}, which is not a capture of the cited URL"


def _live_gate(root: Path, src: Source) -> str | None:
    """None when the cached live page is gated — the one condition an archive verification
    stands on — else why it isn't. Asked of the page, never of a status a claim file carried."""
    live = load_cached(root, src.url)
    if live is None:
        return "no live page is cached, so nothing shows it is gated"
    found = check_page(src, live)
    if found.status != "could_not_verify_paywall":
        return f"the cached live page is not gated (it checks as {found.status})"
    return None


def _served(page: PageCache) -> bool:
    # The fetch layer's own test, not `page.text.strip()`: a scan's bare page markers are not
    # a served page.
    return not unreadable_reason(page)


def _squash(s: str | None) -> str:
    return " ".join((s or "").split()).casefold()


def _same_page(live: PageCache, snap: PageCache) -> tuple[bool | None, str]:
    """Does the snapshot hold the page we fetched live? None when there is too little on the
    live page to tell. By text, never by title: cal-access gives every page the same title,
    so a title match says nothing about which filing or cycle a capture holds. Nor does one
    shared line, which is as likely a footer as content: it takes a few to vouch."""
    lines = [x for x in (_squash(ln) for ln in live.text.splitlines()) if len(x) >= 40][:40]
    if len(lines) < 3:
        return None, "the live page has too little text to compare the snapshot with"
    body = _squash(snap.text)
    hits = sum(1 for x in lines if x in body)
    if hits * 2 >= len(lines):
        return True, f"{hits} of {len(lines)} lines of the live page appear in it"
    return False, f"only {hits} of {len(lines)} lines of the live page appear in it"


def _scanned_snapshot(snapshot: str, on_page: int | None = None) -> str:
    """The note for a capture with nothing to search: the whole PDF, or the cited page — which
    may be blank rather than scanned, as no_text_layer_reason() says too."""
    if on_page is None:
        return (f"{snapshot} is a PDF with no text layer (image-only or scanned) — open it and "
                "confirm it shows the cited page")
    return (f"{snapshot} is a PDF with no text layer on page {on_page} (scanned, or blank) — "
            "open it and confirm it shows the cited page; if the page is blank, the quote is "
            "not on it")


def _capture_problem(root: Path, src: Source, snapshot: str,
                     page: PageCache | None) -> tuple[ArchiveStatus, str] | None:
    """check_snapshot()'s verdict on the capture itself, whoever cites it, or None when the
    capture loaded as a readable page and only the citation's own check is left."""
    if page is None:
        return "archive_unconfirmed", (
            f"{snapshot} has not been checked yet — run `vg archive` to check it")
    if elsewhere := _landed_elsewhere(root, src, page):
        return "archive_unusable", f"{snapshot}: {elsewhere}"
    name = bot_check(page)
    if name:
        return "archive_unusable", f"{snapshot} holds {name}, not the cited page"
    if page.error or not 200 <= page.status < 400:
        # Not loading is not evidence of junk: a timeout, a rate limit, or a fresh capture
        # the Wayback Machine hasn't indexed yet all look like this. Unusable needs evidence
        # the capture isn't the page; this is unchecked, and never vouches.
        why = page.error or f"HTTP {page.status}"
        return "archive_unconfirmed", (
            f"{snapshot} could not be loaded to check it ({why}) — open it and confirm it "
            "shows the cited page")
    if no_text_layer(page):
        # A scan. Nothing to search is no evidence the capture isn't the page, so it is
        # unchecked, never unusable — and never "the snippet is not in it".
        return "archive_unconfirmed", _scanned_snapshot(snapshot)
    if not has_text(page):
        return "archive_unusable", (
            f"{snapshot} loaded with no readable text — often a bot check that renders "
            "nothing without JavaScript")
    return None


def junk_capture(src: Source, snapshot: str, root: Path) -> bool:
    """Whether a cached capture is evidently not the cited page, whatever the citation says: a
    capture of another URL, a fetch that landed elsewhere, a bot check, or nothing readable.
    Offline. Unlike check_snapshot() it asks nothing of the snippet, since a snippet missing
    from a real capture can be the citation's fault, and a good capture must not be thrown
    away for it."""
    if not _is_capture_of(root, src.url, snapshot):
        return True
    problem = _capture_problem(root, src, snapshot, load_cached(root, snapshot))
    return problem is not None and problem[0] == "archive_unusable"


def check_snapshot(src: Source, snapshot: str, root: Path, *, fetch_missing: bool = False,
                   earlier: bool = False) -> tuple[ArchiveStatus, str]:
    """Is this snapshot the page the source cites?

    A successful Save Page Now is not a usable snapshot. cal-access is behind bot
    protection: SPN reported success on at least one of its pages and captured the bot
    check. On a row whose live page can't be read, the snapshot is the reviewer's only route
    to the text, so a junk one is worse than none — it looks like evidence.

    `earlier` marks a capture that is not a fresh save (the save failed and an older one
    stood in). Offline unless `fetch_missing`: `vg archive` fetches each snapshot (which
    caches it), and every later command re-derives the same answer from that cached copy.
    """
    if not _is_capture_of(root, src.url, snapshot):
        return "archive_unusable", (
            f"{snapshot} is a capture of {wayback_target(snapshot) or 'no page'}, not of the "
            "cited URL")
    page = load_cached(root, snapshot)
    if fetch_missing:
        # A cached failure may have been the Wayback Machine, not the capture: try it again
        # rather than let one timeout decide this snapshot for good.
        page = fetch(snapshot, root, refresh=page is not None and not _served(page))
    if problem := _capture_problem(root, src, snapshot, page):
        return problem

    if src.query is None and src.snippet.strip():
        mode, _count, _s, _e = _locate(page, src.snippet)
        blank = pages_without_text(page) if mode is None else []
        if src.page in blank:
            # The cited page of a partly scanned capture: unchecked, like a whole scan.
            return "archive_unconfirmed", _scanned_snapshot(snapshot, src.page)
        if blank and src.page is None:
            # Unlike check_page(), a miss here does not stand. There, keeping snippet_not_found
            # protects the fabricated-quote check; here the alternative is unconfirmed, which
            # vouches for nothing, while unusable drops the link — on a paywalled row, the
            # human's only route to the text.
            return "archive_unconfirmed", (
                f"the snippet is not in the text of {snapshot}, but {page_list(blank)} "
                f"{'have' if len(blank) > 1 else 'has'} no text layer (scanned, or blank) — "
                "open it and confirm the quote is on one of them")
        if mode is None:
            return "archive_unusable", (
                f"the snippet is not in {snapshot} — the capture may predate the quote, or "
                "hold a different version of the page")
        return "archived", "snippet found in the snapshot" + (
            " after normalizing whitespace/quotes/dashes" if mode != "exact" else "")

    # A query citation's snippet describes the lookup and need not be on the page, so
    # compare the snapshot with the page as the pipeline fetched it live.
    live = load_cached(root, src.url)
    if live is None or not _served(live) or bot_check(live):
        return "archive_unconfirmed", (
            "the live page is unreadable, so there is nothing to compare the snapshot with — "
            "open it and confirm it shows the cited record")
    same, why = _same_page(live, page)
    if same is None:
        return "archive_unconfirmed", why
    if not same:
        return "archive_unusable", f"{snapshot}: {why}"
    if earlier:
        # Shared page furniture can match while the figures differ: an older capture of a
        # filing index can hold another cycle. With no snippet to find, don't vouch for it.
        return "archive_unconfirmed", (
            f"{why}, but it is an older capture — check it covers the cited period")
    return "archived", why


def apply_archive(src: Source, records: dict[str, dict], root: Path, *,
                  fetch_missing: bool = False) -> Source:
    """Set the source's archive fields from the run's records — never from the claim file.

    Every command that writes claims back, or renders them, calls this: it is what makes
    `archive_url` a machine field. An agent-authored value is replaced by the pipeline's own
    snapshot, or by nothing.
    """
    src.archive_url = src.archive_status = src.archive_note = None
    rec = records.get(src.url)
    if not rec:
        return src
    snap, err = rec.get("snapshot"), rec.get("error")
    if not snap:
        src.archive_status = "archive_failed"
        src.archive_note = err or "no snapshot could be saved"
        return src
    try:
        snap = check_archive_url(snap)
    except ValueError as e:
        src.archive_status, src.archive_note = "archive_unusable", f"invalid snapshot URL: {e}"
        return src
    status, note = check_snapshot(src, snap, root, fetch_missing=fetch_missing,
                                  earlier=bool(err))
    if err:
        note = f"not a fresh capture ({err}); {note}"
    src.archive_status, src.archive_note = status, note
    if status != "archive_unusable":
        src.archive_url = snap
    return src


def verify_against_archive(src: Source, root: Path, *, refresh: bool = False) -> Source:
    """For a source we couldn't read live, check the snippet against its archive snapshot.

    A paywalled row is the one place the human has no cheap way to confirm anything, so
    if the snapshot is readable the pipeline should do the check rather than hand over an
    unverified row with a link. Only ever upgrades a paywall status — never downgrades a
    live verification, and never invents one where no snapshot exists.

    The snapshot must be a capture of the cited URL. The host pin on `archive_url` only says
    the Wayback Machine serves it, and anyone can Save Page Now a page holding their own quote.
    And it must have served a real page: an error page or bot check can echo the snippet.

    A row already `verified_via_archive` is re-checked, not kept: it was earned against
    whatever snapshot was recorded then, and `vg archive` may have replaced it since.
    """
    v = src.verification
    if v.status == "verified_via_archive":
        # Drop everything the old status vouched for, verdict included: it was about an
        # excerpt this snapshot may no longer hold (recorded verdicts are re-applied by sid).
        v.status = "could_not_verify_paywall"
        v.reason = ("page body not retrievable (paywall); the archive snapshot no longer "
                    "confirms the snippet — check via a subscription")
        v.checked_at = datetime.now(UTC)
        v.match_count = v.matched_offset = v.context = v.context_offset = None
        v.context_page = None
        v.support, v.support_note = "unreviewed", None
    if v.status != "could_not_verify_paywall" or not src.archive_url:
        return src
    # The status may have come from a claim file (`vg archive` loads trusted): upgrade only
    # where the live page, as cached, really is gated.
    if _live_gate(root, src) is not None:
        return src
    if not _is_capture_of(root, src.url, src.archive_url):
        return src
    page = fetch(src.archive_url, root, refresh=refresh)
    if not _served(page) or bot_check(page) or _landed_elsewhere(root, src, page):
        return src
    mode, count, start, end = _locate(page, src.snippet)
    if mode is None or count != 1:
        return src
    excerpt, rs, re_ = context_window(page.text, start, end)
    v.checked_at = datetime.now(UTC)   # this check happened now, not at the live attempt
    v.status = "verified_via_archive"
    v.reason = _archive_reason(mode, page)
    v.match_count = count
    v.matched_offset = start
    v.context = excerpt
    v.context_offset = (rs, re_)
    # The snapshot, not the cited URL: that is the copy the context came from, and the one a
    # verdict on it describes. The paywall stub at `src.url` never changes, so a verdict
    # stamped from it survived every re-archive.
    v.context_page = _copy_of(src.archive_url, page)
    return src


def revalidate_from_cache(src: Source, root: Path, *,
                          rules: dict[str, tuple[str, ...]] | None = None) -> Source:
    """Re-derive a usable source's verification from the page cache, offline.

    Stripping machine-owned fields on ingest stops an agent from *declaring* its citation
    verified, but only for commands that re-verify. This closes the rest of the gap: every
    row that counts as evidence is rebuilt from the cached page it was supposedly checked
    against, by the same offline check `vg verify` runs — source class, snippet rules, served
    status, presence, uniqueness. What renders is what that check gives now, never what the
    claim file said: a row that cannot be reproduced is downgraded to `human_review`, and one
    that can gets its status, reason, excerpt and offsets from the check. No network, so it
    is cheap enough to run on every build.
    """
    v = src.verification
    # USABLE, not GOOD: a paywall row is not green, but check_corroboration() counts it, so a
    # forged one on an excluded aggregator could stand as an adversarial claim's second source.
    if v.status not in USABLE:
        if src.query is not None:
            # Not re-run, so nothing this build did says what it was checked against — and
            # the file's stamp becomes the --cache of a command the reviewer pastes. A
            # hand-edited root pointing at a crafted database would "reproduce" a figure the
            # red row says is wrong. The review page prints the build's own root instead.
            v.query_run = None
        return src
    claimed = v.status

    def _discard(reason: str) -> Source:
        """Downgrade AND drop everything the status vouched for.

        Clearing the status alone isn't enough: the review app renders
        `verification.context` as the highlighted excerpt and shows the verifier agent's
        support verdict. Leaving those in place would show the human a fabricated excerpt
        and a judgment nothing produced — a way to launder a citation past the one check
        that matters, with only a badge to contradict it.
        """
        v.status = "human_review"
        v.reason = reason
        v.checked_at = datetime.now(UTC)   # this check is what produced the downgrade
        v.context = None
        v.context_offset = None
        v.matched_offset = None
        v.match_count = None
        v.context_page = None
        v.support = "unreviewed"
        v.support_note = None
        v.query_run = None
        return src
    # A query citation has no page by design — that is the whole point of it, and cal-access
    # is unfetchable. Re-run the query instead: deterministic, offline, and it satisfies the
    # same "cannot be reproduced => downgrade" contract this function exists to enforce.
    # Without this branch every query citation was discarded at build, and _discard() also
    # wiped the fresh verifier's judgment each time.
    if src.query is not None:
        from . import queries

        try:
            result = queries.run(src.query.name, dict(src.query.params), root)
            run = _query_run(result, root)
        except Exception as e:  # noqa: BLE001
            return _discard(f"claimed {claimed!r} but the query could not be re-run: {e}")
        if not result.found or not queries.matches(src.query.expected, result.value):
            # with what it leaves out, if anything: the filing to open, not a retry
            _discard(f"claimed {claimed!r} but re-running the query gives {result.value!r} "
                     f"({result.note}), not {src.query.expected!r}"
                     + (f"; {why}" if (why := result.unsettled) else ""))
            v.query_run = run   # what this re-run read: build's own run, so it can vouch for it
            return src
        if why := result.unsettled:
            # verify_query_source()'s rule, applied here too: a row verified before a filing's
            # later amendment or a late report reached the export, or by a version that did not
            # ask, is not green
            _discard(f"claimed {claimed!r}; re-running the query gives {result.value!r}, but "
                     f"{why}")
            v.query_run = run
            return src
        # A query that reproduces vouches for its number, not for whatever status, excerpt or
        # reason the claim file paired with it: rebuild them exactly as `vg verify` would.
        _mark_query_verified(v, src.query, result, run)
        return src

    problem = citation_problem(src, rules)
    if problem:
        return _discard(f"claimed {claimed!r} but the citation fails a rule `vg verify` "
                        f"enforces ({problem[0]}): {problem[1]}")

    # verified_via_archive means the LIVE page was unreadable, so checking src.url would
    # fail by design and wrongly discard a status the pipeline itself produced. Check the
    # snapshot it was actually verified against.
    if claimed == "verified_via_archive":
        if not src.archive_url:
            return _discard("claimed verified_via_archive but no snapshot is recorded — "
                            "re-run `vg archive`")
        # The status means the LIVE page couldn't be read. Where it reads fine, an earlier
        # capture holding a quote the page has since dropped would otherwise reproduce it.
        if gate := _live_gate(root, src):
            return _discard(f"claimed verified_via_archive but {gate} — re-run `vg verify`")
        if not _is_capture_of(root, src.url, src.archive_url):
            return _discard(
                f"claimed verified_via_archive against {src.archive_url}, which is not a "
                "capture of the cited URL — re-run `vg archive`")
    target = src.context_url   # the page a verdict on this row is checked against, too

    # fetch() caches every response, gates and error pages included, so any row the pipeline
    # produced has a page here — a paywall row as much as a verified one.
    page = load_cached(root, target)
    if page is None:
        return _discard(
            f"claimed {claimed!r} but no cached page exists for {target} — run "
            "`vg verify`; never trust a status the pipeline did not produce")
    if claimed == "verified_via_archive" and (elsewhere := _landed_elsewhere(root, src, page)):
        return _discard(f"claimed verified_via_archive against {target}, but {elsewhere} — "
                        "re-run `vg archive`")
    found = check_page(src, page)
    if claimed == "could_not_verify_paywall":
        if found.status != claimed:
            # Build refines a row that already matched; it never promotes one into a match, the
            # same way a 'verified' that no longer matches exactly is sent back rather than
            # relabelled. A gate the cached page doesn't have is `vg verify`'s to re-derive.
            return _discard(f"claimed {claimed!r} but the cached page gives {found.status!r} "
                            "— re-run `vg verify`")
        # Still gated. Its evidence is the gate itself, so there is no excerpt to draw: take
        # the check's reason and clear anything page-derived the claim file carried.
        v.reason, v.match_count = found.reason, found.match_count
        v.context = v.context_offset = v.matched_offset = v.context_page = None
        src.paywall = True
        return src
    if found.status not in MATCHED:
        return _discard(f"claimed {claimed!r} but the cached page fails the check `vg verify` "
                        f"runs ({found.status}): {found.reason} — re-run `vg verify`")
    start, end = found.start, found.end
    if claimed == "verified" and found.mode != "exact":
        # "verified" is a promise that the human's literal Cmd-F will hit. A normalized-only
        # match doesn't keep that promise, so a forged (or stale) "verified" must not ride
        # through on one.
        return _discard(
            "claimed 'verified' but the snippet only matches after normalization, which "
            "does not guarantee Cmd-F will find it — re-run `vg verify`")
    if claimed == "verified_via_archive":
        # The snapshot matched, so the status stands. The reason is still rebuilt from how it
        # matched: a normalized-only hit must say so, whatever the claim file wrote.
        v.reason = _archive_reason(found.mode, page)
    else:
        # The status the check gives for this page, not the one the file claimed: the cache is
        # shared, so another run's re-fetch can turn a normalized match exact.
        v.status, v.reason = found.status, found.reason
        src.paywall = found.paywall

    # Confirming the status is not enough: context and the offsets are what the review app
    # actually draws, so a file could pair a reproducible status with a fabricated excerpt
    # and the human would read the fabrication under a green badge. Recompute them from
    # the page we just checked, and drop the support verdict if the excerpt it was formed
    # about has changed.
    excerpt, rs, re_ = context_window(page.text, start, end)
    if v.context != excerpt or v.context_offset != (rs, re_) or v.matched_offset != start:
        v.support = "unreviewed"
        v.support_note = None
    v.match_count = found.match_count
    v.matched_offset = start
    v.context = excerpt
    v.context_offset = (rs, re_)
    v.context_page = _copy_of(target, page)
    return src


def secondary_host(src: Source, rules: dict[str, tuple[str, ...]] | None = None) -> bool:
    """True when a primary document is cited from somewhere other than its issuing authority.

    The generalized form of a real failure: the FPPC Form 700 portal is a JS app our fetcher
    cannot search, so a researcher took a copy of the form hosted by another agency and cited
    it as the filing. Every downstream check passed — real PDF, institutional author, snippet
    present — because none of them ask whether this host is the one that issues the record.

    A copy is often the only reachable version and may be perfectly faithful. The rule is
    not "never cite a copy", it is "never cite one silently".
    """
    # own_statement/campaign_statement are BY the host about itself, so "not the issuing
    # authority" cannot apply: for an endorsement, the endorsing organization IS the
    # authority. Flagging those told one run to "repair" five genuine primary sources.
    if src.source_type not in ("primary_document", "official_record"):
        return False
    return classify(src.url, rules) != "primary_document"


def missing_filing_date(src: Source) -> bool:
    """True when a periodic-filing citation carries no date.

    Not a verification failure — the snippet is really there — but it makes the recency
    question unanswerable, which is how a 2024 Form 700 gets cited while a 2025 one sits in
    the same search results.
    """
    host = domain(src.url)
    on_filing_host = any(host == h or host.endswith("." + h) for h in FILING_HOSTS)
    return on_filing_host and not (src.date or "").strip()


def _input_order(by_id: dict[str, Claim]) -> list[list[Claim]]:
    """The strongly connected components of the `derives_from` graph, inputs first.

    Tarjan's algorithm emits a component only after every component it can reach, so walking
    the result in order settles each claim's inputs before the claim itself. A component of
    more than one claim, or of one claim naming itself, is a cycle. Iterative, because
    `derives_from` is agent-authored and a long enough chain would hit the recursion limit.
    """
    deps = {q: [d for d in c.derives_from if d in by_id] for q, c in by_id.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[Claim]] = []
    for root in by_id:
        if root in index:
            continue
        work = [(root, 0)]   # (claim, how many of its inputs have been explored)
        while work:
            q, i = work[-1]
            if i == 0:
                index[q] = low[q] = len(index)
                stack.append(q)
                on_stack.add(q)
            if i < len(deps[q]):
                work[-1] = (q, i + 1)
                d = deps[q][i]
                if d not in index:
                    work.append((d, 0))
                elif d in on_stack:
                    low[q] = min(low[q], index[d])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[q])
            if low[q] == index[q]:
                component: list[Claim] = []
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    component.append(by_id[m])
                    if m == q:
                        break
                out.append(component)
    return out


def check_inputs(claims: list[Claim]) -> list[list[str]]:
    """Mark any claim whose declared inputs are not themselves verified. Returns the
    `derives_from` cycles found, as lists of question ids, so the caller can report them.

    A comparison inherits the weakness of what it compares. If the candidate's housing
    position is `human_review`, then "their housing position falls short of the platform" is
    not verified either — no matter how well the platform's own text is cited. This is a
    cheap mechanical check on something the judgment layer would otherwise have to notice.

    Run it only once every claim's own status is final — after revalidate_from_cache() and
    check_corroboration() — and it works in dependency order, so a downgrade reaches every
    claim derived from it, however many steps away. A single pass in file order let q36 read
    verified while q17, which it derives from, was downgraded later in the same build.

    Claims in a cycle rest on each other, so none of them is a foundation to reason from:
    each gets its in-cycle inputs marked unmet instead of being looped over.
    """
    by_id = {c.question_id: c for c in claims}
    good = {"verified", "not_found"}
    cycles: list[list[str]] = []
    for component in _input_order(by_id):
        ids = {c.question_id for c in component}
        cyclic = len(component) > 1 or any(c.question_id in c.derives_from for c in component)
        if cyclic:
            cycles.append(sorted(ids))
        for c in component:
            unmet = []
            for dep in c.derives_from:
                other = by_id.get(dep)
                if other is None:
                    unmet.append(f"{dep} (missing)")
                elif dep in ids:   # only possible in a cycle
                    # Never its status: that reads the member's own unmet_inputs, which may
                    # not be settled yet — or be the stale value the claim file carried.
                    unmet.append(f"{dep} (cycle)")
                elif (st := other.status) not in good:
                    unmet.append(f"{dep} ({st})")
            c.unmet_inputs = unmet
    return cycles


def check_corroboration(claim: Claim) -> Claim:
    """Adversarial claims need 2 *independent* sources: different publishers, and not
    the same wire story reprinted."""
    # A source judged topic_only/contradicts/superseded is not corroboration. Counting it
    # let a claim whose every source a verifier rejected report "15/1 usable, corroborated".
    usable = [s for s in claim.sources
              if s.verification.status in USABLE and not s.judged_bad]
    need = claim.required_sources
    if claim.confidence == "not_found":
        claim.corroboration_ok = True
        claim.corroboration_note = "not_found — no citation required"
        return claim
    n_docs = len({s.url for s in usable})
    if n_docs < need:
        rejected = sum(1 for s in claim.sources if s.judged_bad)
        claim.corroboration_ok = False
        claim.corroboration_note = (
            f"{claim.claim_type} claim needs {need} independent document(s); has "
            f"{n_docs} document(s) ({len(usable)} snippet(s))"
            + (f", {rejected} rejected by the verifier" if rejected else ""))
        return claim
    docs = {s.url for s in usable}
    if need >= 2:
        pubs = {domain(s.url) for s in usable}
        names = {s.publisher.strip().lower() for s in usable}
        if len(pubs) < 2 or len(names) < 2:
            claim.corroboration_ok = False
            claim.corroboration_note = (
                "sources are not independent — same publisher; adversarial claims need two "
                "different outlets doing their own reporting")
            return claim
    claim.corroboration_ok = True
    # Report documents, not snippets: "9 usable" was 9 snippets across 4 articles from 2
    # outlets, three of them from one URL. The independence checks still held, but the count
    # overstated the evidence base to a human deciding how much weight it carries.
    pubs = {s.publisher.strip() for s in usable if s.publisher.strip()}
    claim.corroboration_note = (
        f"{len(docs)} document(s) from {len(pubs)} publisher(s), {len(usable)} snippet(s); "
        f"needs {need}" + (", independent publishers" if need >= 2 else ""))
    return claim
