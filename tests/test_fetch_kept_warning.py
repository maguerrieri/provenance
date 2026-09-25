"""The warning `fetch()` logs for a kept page is printed outside `cli.py` (#147).

When a re-fetch fails, `fetch._warn_kept()` logs the page it kept, and Python's last-resort
handler writes that line to stderr as it is. Two parts of it come from outside the pipeline:

- the URL, which a claim cited or `vg fetch` was given. `_http_only()` lets a C1 control and a
  bidi override through, and `vg fetch`'s argument is not checked at all;
- the failure reason, which is exception text from the failed fetch.

So an ESC or C1 sequence in either reached the operator's terminal, as it did from `cli.py`'s
prints before #142. Both now show such characters as escapes (`terminal.printable()`). The
retry command at the end is `shlex.quote`d. It used to put the URL in single quotes by hand,
so a `'` in the URL broke it. It is printed only for a URL that shows as itself, since a command
with an escape in it names another URL.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vgpipe import fetch as fetch_mod
from vgpipe.models import EXTRACTOR_VERSION, PageCache, RefetchFailure

# What acts on a terminal or makes a write raise: the categories `terminal.printable()` escapes.
_ACTING = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

TEXT = "The board approved the Example Levy on a 4-1 vote after a long hearing on Tuesday."
RETRY = "Retry: vg fetch --refresh "
WITHHELD = "no command is printed"


@pytest.fixture
def refused(monkeypatch):
    """Every request fails, with exception text that erases the line it is printed on."""
    real = httpx.Client

    def handler(request):
        raise httpx.ConnectError("connection reset\x1b[2K", request=request)

    monkeypatch.setattr(fetch_mod.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(fetch_mod, "_warned", set())   # a fresh run: every kept page warns


def _page(url: str, **kw) -> PageCache:
    """A good page at `url`, cached under an older extractor, so the next fetch re-fetches it."""
    d = dict(url=url, final_url=url, status=200, content_type="text/html", title="T", text=TEXT,
             fetched_at=datetime.now(UTC) - timedelta(days=2),
             extractor_version=EXTRACTOR_VERSION - 1)
    d.update(kw)
    return PageCache(**d)


def _kept_warning(root, url, caplog) -> str:
    """Cache a good page at `url`, fail its re-fetch, and return the one warning logged."""
    fetch_mod.cache_path(root, url).write_text(_page(url).model_dump_json())
    caplog.clear()
    page = fetch_mod.fetch(url, root)
    assert page.text == TEXT and page.refetch_failure is not None, "the good page is kept"
    (msg,) = [r.getMessage() for r in caplog.records if r.name == fetch_mod.log.name]
    return msg


def _assert_inert(msg: str) -> None:
    """Nothing in `msg` acts on a terminal, and it can be written to a UTF-8 stream."""
    # A tab is the one such character `printable()` leaves as it is: it moves nothing.
    acting = sorted({f"U+{ord(c):04X}" for c in msg
                     if c != "\t" and unicodedata.category(c) in _ACTING})
    assert not acting, f"reached the terminal: {acting} in {msg!r}"
    msg.encode()


def _argv_in_a_real_shell(cmd: str) -> list[str]:
    """The arguments `sh` would pass to `cmd`, with `vg` swapped for an argv printer. shlex
    expands neither `$(...)` nor backticks, so it round-trips an unquoted substitution as one
    argument; a real shell shows what running the command would do. Payloads are harmless."""
    show_argv = (f"{shlex.quote(sys.executable)} -c "
                 "'import json, sys; print(json.dumps(sys.argv[1:]))'")
    assert cmd.startswith("vg ")
    out = subprocess.run(["sh", "-c", f"{show_argv} {cmd[len('vg '):]}"],
                         capture_output=True, check=True, text=True).stdout
    return json.loads(out)


def test_a_kept_page_logs_its_url_and_failure_reason_as_escapes(tmp_path, refused, caplog):
    """The issue's case: a URL holding a C1 CSI erase-line, and a failure reason holding an ANSI
    one. A retry command with the URL shown as escapes would name another URL, and with the URL
    as it is would erase the line, so the warning prints none and says why."""
    url = "https://news.example/levy\x9b2K"
    msg = _kept_warning(tmp_path, url, caplog)
    _assert_inert(msg)
    assert msg.startswith("https://news.example/levy\\x9b2K: re-fetch failed "
                          "(ConnectError: connection reset\\x1b[2K, "), msg
    assert f"under extractor v{EXTRACTOR_VERSION - 1}" in msg, msg
    assert RETRY not in msg and WITHHELD in msg, msg


@pytest.mark.parametrize("url", [
    "https://news.example/the-levy's-vote",
    "https://news.example/levy?q=1&r=$(printf${IFS}INJECTED)",
    "https://news.example/levy#`echo INJECTED`;x",
], ids=["quote", "substitution", "backticks"])
def test_the_retry_command_runs_as_printed(tmp_path, refused, caplog, url):
    """A URL that shows as itself gets a retry command, quoted so that the shell passes it back
    as one argument, whatever it holds. The hand-written `'{url}'` broke on a `'`."""
    msg = _kept_warning(tmp_path, url, caplog)
    _assert_inert(msg)
    assert msg.startswith(f"{url}: re-fetch failed (ConnectError: connection reset\\x1b[2K, ")
    cmd = msg.split("Retry: ", 1)[1]
    assert shlex.split(cmd) == ["vg", "fetch", "--refresh", url]
    assert _argv_in_a_real_shell(cmd) == ["fetch", "--refresh", url]


@pytest.mark.parametrize(("url", "shown"), [
    ("https://news.example/\u202elevy", "https://news.example/\\u202elevy"),    # bidi override
    ("https://news.example/co\xadop", "https://news.example/co\\xadop"),       # soft hyphen
    ("https://news.example/\udcff", "https://news.example/\\udcff"),           # undecodable argv
    ("https://news.example/a\u2028b", "https://news.example/a\\u2028b"),       # line separator
    ("https://news.example/a\tb", "https://news.example/a\tb"),                # shown as it is
    ("https://news.example/a\u034fb", "https://news.example/a\u034fb"),        # renders as nothing
], ids=["bidi", "soft-hyphen", "surrogate", "line-separator", "tab", "invisible"])
def test_a_url_a_pasted_command_cannot_carry_gets_no_command(caplog, monkeypatch, url, shown):
    """A URL with an escape in it, or a tab or a character that renders as nothing (which
    `printable()` leaves as they are), would be pasted as another URL: no command, by the rule
    `queries.human_command()` uses. Called directly: a lone surrogate cannot reach this through
    `fetch()`, whose cache key encodes the URL first, but the warning must not depend on its
    callers for that. Written as it is, one made the log call fail in place of the warning."""
    monkeypatch.setattr(fetch_mod, "_warned", set())
    page = _page(url, refetch_failure=RefetchFailure(
        attempted_at=datetime.now(UTC), extractor_version=EXTRACTOR_VERSION, status=0,
        reason="ConnectError: reset\u202e\udcff"))
    fetch_mod._warn_kept(page)
    (msg,) = [r.getMessage() for r in caplog.records if r.name == fetch_mod.log.name]
    _assert_inert(msg)
    assert msg.startswith(f"{shown}: re-fetch failed (ConnectError: reset\\u202e\\udcff, "), msg
    assert RETRY not in msg and WITHHELD in msg, msg
