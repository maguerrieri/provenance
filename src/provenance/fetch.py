"""Fetch + on-disk page cache + text extraction.

Cached deliberately: the verifier and the review app must see the *same* bytes. If the
app re-fetched at render time, a page could change between "verified" and the human's
read, and the highlighted context would silently stop matching the recorded offsets.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .models import EXTRACTOR_VERSION, PageCache, RefetchFailure
from .queries import unprintable
from .terminal import printable

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# A line that is HTML source rendered as text, not prose.
MARKUP_LINE = re.compile(r"</?(?:p|div|h[1-6]|a|img|span|br)\b[^>]*>|&lt;/?(?:p|h[1-6]|a)\b")

PAYWALL_MARKERS = re.compile(
    r"(subscribe to (?:continue|read)|already a subscriber|this article is for subscribers|"
    r"create an account to (?:keep )?read|you've reached your (?:free )?(?:article )?limit|"
    r"metered paywall|subscription required)", re.I)

# The page locator _extract_pdf() inserts before each page's text. It is ours, not the
# document's: no PDF viewer shows it, so a snippet touching one fails the human's Cmd-F.
PDF_PAGE_MARKER = re.compile(r"\[\[page (\d+)\]\]")


def _pages_dir(root: Path) -> Path:
    return root / "cache" / "pages"


def cache_dir(root: Path) -> Path:
    d = _pages_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _page_file(root: Path, url: str) -> Path:
    return _pages_dir(root) / f"{cache_key(url)}.json"


def cache_path(root: Path, url: str) -> Path:
    """Where to WRITE a page: creates the cache directory. Readers use `load_cached()`."""
    cache_dir(root)
    return _page_file(root, url)


def load_cached(root: Path, url: str) -> PageCache | None:
    # Read-only: a lookup must not create the directory it looks in. Going through
    # cache_path() here mkdir'd data/<candidate>/cache/pages on every staleness check, and
    # `_cache_root()` preferred a data dir's own cache/ once one existed — so merely reading
    # forked the candidate off the shared cache.
    p = _page_file(root, url)
    if not p.exists():
        return None
    try:
        return PageCache.model_validate_json(p.read_text())
    except Exception:
        return None


def _drop_escaped_markup(text: str) -> str:
    """Remove lines that are raw HTML rendered as text, and exactly-repeated paragraphs.

    Pages embed escaped copies of themselves in ways no tag list fully covers, and a duplicate
    is worse than useless here: uniqueness is what makes a snippet Cmd-F-able, so a second copy
    turns every true citation on that page into a false `snippet_not_unique`.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        stripped = line.strip()
        if MARKUP_LINE.search(stripped):
            continue
        # Only dedupe substantial lines: short ones ("Share", a date) repeat legitimately.
        if len(stripped) > 60:
            key = " ".join(stripped.split())
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def _extract_html(html: str, url: str) -> tuple[str, str | None]:
    """Readable body text + title. trafilatura first (drops nav/boilerplate, which
    otherwise creates false 'not unique' hits from repeated nav strings); falls back
    to a full-DOM text dump so we never fail closed on an odd page."""
    title = None
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        if tree.css_first("title"):
            title = tree.css_first("title").text(strip=True)
    except Exception:
        tree = None

    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=True,
            favor_recall=True, output_format="txt") or ""
    except Exception:
        pass

    # Fall back not only when extraction returns almost nothing, but when it returns far less
    # than the document holds. On a single-page document of anchor-linked sections,
    # trafilatura kept the short introduction and dropped every section after it, so every
    # snippet from those sections would have failed verification while the page looked fine.
    if tree is not None:
        # textarea/template/pre carry "republish this story" widgets: an HTML-ESCAPED copy of
        # the whole article, which lands in the dump as a second copy of every sentence. That
        # made uniqueness fail on 20 good citations at once — and falsely, since a human
        # pressing Cmd-F on the real page gets one hit.
        for tag in ("script", "style", "noscript", "textarea", "template", "pre"):
            for node in tree.css(tag):
                node.decompose()
        body = tree.body or tree.root
        dump = body.text(separator="\n", strip=True) if body else ""
        dump = _drop_escaped_markup(dump)
        # Prefer the readable extraction, but take the full dump when it holds materially more:
        # boilerplate noise costs a little uniqueness, whereas dropped content makes a true
        # snippet unverifiable, which is the failure that actually matters.
        if len(dump) > max(400, int(len(text) * 1.5)):
            text = dump
    return text, title


def _extract_pdf(data: bytes) -> tuple[str, str | None]:
    """(page-tagged text, why the PDF could not be opened, or None). Page numbers matter: PDF
    snippets get a `page` locator because reflowed PDF text often can't be matched exactly.

    The failure is an error, never a title. It used to come back in the title slot, where the
    review app showed the diagnosis as the page's name while the row read "empty pdf text"."""
    try:
        import pdfplumber

        chunks = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                chunks.append(f"\n\n[[page {i}]]\n" + (page.extract_text() or ""))
        return "".join(chunks), None
    except Exception as first:  # noqa: BLE001
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            return "".join(
                f"\n\n[[page {i}]]\n" + (p.extract_text() or "")
                for i, p in enumerate(reader.pages, 1)), None
        except Exception as e:  # noqa: BLE001
            # Both, since either may be the informative one: pdfplumber's is often the precise
            # syntax error, pypdf's the generic fallback.
            return "", (f"pdfplumber: {type(first).__name__}: {first}; "
                        f"pypdf: {type(e).__name__}: {e}")


# One of our page markers, or any other non-space character. The first match that is not a
# marker is readable text, so has_text() can stop there rather than copy the whole page.
_MARKER_OR_CHAR = re.compile(PDF_PAGE_MARKER.pattern + r"|\S")


def _readable(text: str) -> bool:
    return any(len(m.group()) == 1 for m in _MARKER_OR_CHAR.finditer(text))


def has_text(page: PageCache) -> bool:
    """Something readable was extracted — not just whitespace, nor the bare [[page N]]
    markers an image-only PDF yields.

    The one test for "is there anything to search". The cache uses it to refuse a re-fetch
    that would replace a good page; every reader of page text uses it to tell a page with
    nothing to search from a citation that is not on it. Deriving it again anywhere, as
    `not page.text`, misses the marker-only case that motivated it. pages_without_text() is
    the same test, page by page."""
    return _readable(page.text)


def pages_without_text(page: PageCache) -> list[int]:
    """The pages of a PDF that extracted to no text — scanned, or blank — by has_text()'s own
    test applied between markers. [] for a page that is not a PDF.

    has_text() is whole-document, so a typed cover page in front of scanned schedules reads as
    searchable, and a real quote on a scanned page would otherwise read as fabricated."""
    if not page.is_pdf:
        return []
    marks = list(PDF_PAGE_MARKER.finditer(page.text))
    ends = [m.start() for m in marks[1:]] + [len(page.text)]
    return [int(m.group(1)) for m, end in zip(marks, ends, strict=True)
            if not _readable(page.text[m.end():end])]


def _request_failure(page: PageCache) -> str:
    """Why the request itself failed — an exception, a non-2xx/3xx — or "" if it worked."""
    if page.error:
        return page.error
    if not 200 <= page.status < 400:
        return f"HTTP {page.status}"
    return ""


def unreadable_reason(page: PageCache) -> str:
    """Why `page` served nothing readable — an exception, a non-2xx/3xx, no text — or "" if
    it did. One wording for every reader that has to say so."""
    if why := _request_failure(page):
        return why
    return "" if has_text(page) else f"HTTP {page.status} with no extractable text"


def _served(page: PageCache) -> bool:
    """The page actually served readable text: no error, a 2xx/3xx, something extracted."""
    return not unreadable_reason(page)


def no_text_layer(page: PageCache) -> bool:
    """A PDF that fetched fine but extracted to nothing a snippet could match: a scan."""
    return page.is_pdf and not _request_failure(page) and not has_text(page)


def _why_not_replace(fresh: PageCache, prior: PageCache | None) -> str:
    """Why `fresh` must not overwrite `prior` in the cache, or "" if it may.

    Only a good prior is protected. With nothing cached, or only an earlier failure, the new
    result is recorded exactly as it always was.

    Failure here is objective — an exception, a non-2xx/3xx, no text — and deliberately not
    the paywall heuristic. That heuristic flags any short page with a title, and extractor
    fixes shorten pages (dropping escaped duplicates did): gating on it would block a bump
    from the very pages it was meant to fix, and --refresh could not override it."""
    if prior is None or not _served(prior):
        return ""
    return unreadable_reason(fresh)


def kept_copy_note(page: PageCache) -> str:
    """What a reader of this page must know if it was kept after a failed re-fetch, else ""."""
    f = page.refetch_failure
    if f is None:
        return ""
    note = (f"re-fetch failed ({f.reason}, {f.attempted_at:%Y-%m-%d}); kept the copy fetched "
            f"{page.fetched_at:%Y-%m-%d} under extractor v{page.extractor_version}")
    if page.extractor_version < EXTRACTOR_VERSION:
        note += (f", older than the current v{EXTRACTOR_VERSION} — a miss on it may be the "
                 "older extraction, not the citation")
    return note


# (url, attempt) pairs already warned about in this process: one line per kept page per run,
# not one per source citing it.
_warned: set[tuple[str, datetime]] = set()


def _warn_kept(page: PageCache) -> None:
    """Log a page kept after a failed re-fetch, once per run. The URL is one a claim cited or
    `provenance fetch` was given, and the note quotes exception text, so both go through `printable()`:
    Python's last-resort handler writes the line as it is, and an ESC or C1 sequence in either
    reached the operator's terminal. The retry command is `shlex.quote`d, and printed only for a
    URL a pasted copy carries as it is (`unprintable()`, the rule for every command printed for
    a human): one with an escape, a tab or an invisible character in it names another URL."""
    f = page.refetch_failure
    if f is None or (page.url, f.attempted_at) in _warned:
        return
    _warned.add((page.url, f.attempted_at))
    # Withheld, the line still says how to retry: from where the URL is held as it is, since
    # the operator cannot copy or type it from here.
    retry = (f"Retry: provenance fetch --refresh {shlex.quote(page.url)}" if not unprintable(page.url)
             else "No retry command: the URL holds a control or invisible character a pasted "
                  "command would not carry. Retry with provenance verify --refresh, which reads it from "
                  "the claim citing it, or provenance fetch --refresh with the URL copied from its "
                  "source, not from this line")
    log.warning(f"{printable(page.url)}: {printable(kept_copy_note(page))}. {retry}")


def fetch(url: str, root: Path, *, refresh: bool = False, timeout: float = 30.0) -> PageCache:
    cached = load_cached(root, url)
    if cached is not None and not refresh:
        failed = cached.refetch_failure
        # Re-fetch automatically when the cached copy was extracted by an older extractor —
        # otherwise a fix silently fails to reach the pages that motivated it — unless that
        # re-fetch was already tried under this extractor and failed. Then serve the kept copy
        # as-is, loudly, rather than hit a host that blocks us on every run.
        if (cached.extractor_version >= EXTRACTOR_VERSION
                or (failed is not None and failed.extractor_version >= EXTRACTOR_VERSION)):
            _warn_kept(cached)
            return cached

    fresh = _fetch_live(url, timeout)
    # A failed re-fetch never replaces a good page. Caching the failure would send every
    # source citing it to fetch_failed — and the re-fetches that get here are extractor bumps
    # and --refresh, meant to improve pages, not to destroy the ones whose hosts now block us.
    why = _why_not_replace(fresh, cached)
    if why:
        # Parallel runs share this cache, and ours just spent up to `timeout` on the network.
        # If another run re-fetched meanwhile, its page wins: writing our copy back would undo
        # its fix and mark the page failed under the current extractor, so never retried.
        latest = load_cached(root, url)
        if latest is not None and latest.fetched_at != cached.fetched_at:
            return latest
        cached.refetch_failure = RefetchFailure(
            attempted_at=fresh.fetched_at, extractor_version=EXTRACTOR_VERSION,
            status=fresh.status, reason=why)
        _store(root, url, cached)
        _warn_kept(cached)
        return cached

    _store(root, url, fresh)
    return fresh


def _store(root: Path, url: str, page: PageCache) -> None:
    """Write through a temp file and a rename. load_cached() reads a half-written file as no
    page at all, and a concurrent re-fetch that sees no page has nothing to protect — so it
    would overwrite the good page it could not read."""
    p = cache_path(root, url)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(page.model_dump_json(indent=2))
    os.replace(tmp, p)


def _fetch_live(url: str, timeout: float) -> PageCache:
    now = datetime.now(UTC)
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}) as c:
            r = c.get(url)
        ctype = r.headers.get("content-type", "")
        is_pdf = "pdf" in ctype.lower() or url.lower().split("?")[0].endswith(".pdf")
        error = None
        if is_pdf:
            text, failure = _extract_pdf(r.content)
            title = None
            # A scan's marker-only text is NOT an error here: the fetch worked. check_page()
            # reports it as a missing text layer at read time, which also reaches every page
            # cached before it did. An extraction failure is, but only on a response that
            # served: a 404's body is an error page, not the PDF, and the 404 is the cause.
            # Recording the failure there hid it behind "empty pdf text".
            if 200 <= r.status_code < 400:
                if failure:
                    error = (f"the PDF could not be opened ({failure}; served as "
                             f"{ctype or 'no content type'})")
                elif not text:
                    error = "the PDF has no pages"
        else:
            text, title = _extract_html(r.text, url)
        paywalled = bool(
            r.status_code in (401, 402, 403)
            or PAYWALL_MARKERS.search(text[:6000])
            or (r.status_code == 200 and not is_pdf and len(text) < 500 and bool(title)))
        page = PageCache(
            extractor_version=EXTRACTOR_VERSION,
            url=url, final_url=str(r.url), status=r.status_code, content_type=ctype,
            title=title, text=text, fetched_at=now, is_pdf=is_pdf,
            paywall_suspected=paywalled, error=error)
    except Exception as e:  # noqa: BLE001
        page = PageCache(extractor_version=EXTRACTOR_VERSION, url=url, final_url=url,
                         status=0, content_type="", text="", fetched_at=now,
                         error=f"{type(e).__name__}: {e}")
    return page
