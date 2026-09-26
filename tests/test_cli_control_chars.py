"""Text the pipeline does not control can do more to a terminal than markup can (#142).

- An ESC or C1 sequence acts on the terminal. `\\x1b[2K` erases the line it lands on, and the
  rest can move the cursor and overwrite what the command printed. rich's `Text` strips only
  BEL, BS, VT, FF and CR, so a filer name, a recipe response or a claim's publisher could erase
  lines or fake output in the operator's terminal. A bidi override (U+202E) reorders what
  follows it, and a line separator (U+2028) starts a new line in some readers.
- A lone surrogate makes the print raise `UnicodeEncodeError`. `json.loads` keeps one, and
  Python surrogate-escapes an undecodable byte in argv, so the command died with a traceback
  instead of printing, and a refusal exited with the wrong code.

`cli._printable()` shows each as an escape. Each test here puts such text where a print site
prints it, and `_provenance()` checks the whole output of every run: nothing that acts on a terminal,
and no exception in place of the command's usual exit code.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta

import pytest
from conftest import write_project

from provenance.models import Claim, PageCache, Source

# An erase-line sequence, a C1 CSI with its own erase-line, a right-to-left override and a line
# separator, and how `_printable()` shows them.
CTRL = "\x1b[2K\x9b2K\u202e\u2028"
SHOWN = "\\x1b[2K\\x9b2K\\u202e\\u2028"
LONE = "\ud800"     # a lone surrogate as json.loads keeps it
ARGV = "\udcff"     # an undecodable byte in argv, as Python decodes it

# A listed news host: reporting on an unlisted one reads as an unlisted outlet, which a
# claim can cite only as what the outlet reports (sources.tier()).
URL = "https://calmatters.org/levy"
TEXT = "The board approved the Example Levy on a 4-1 vote after a long hearing on Tuesday."
SNIPPET = "approved the Example Levy on a 4-1 vote"

# What acts on a terminal or makes a print raise: the categories `_printable()` escapes.
_ACTING = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# What a command says under data a reader copies from, once any of it shows as an escape.
NOTE = "The source holds the character, not the escape, so a copy with one in it matches nothing"


def _provenance(*args, lines: bool = False):
    """Run `provenance`, and return (exit code, output): whitespace-joined, or as its lines. Fails if
    any character in the output acts on a terminal, or if the command raised."""
    from typer.testing import CliRunner

    from provenance import cli

    # rich folds a long tmp path at 80 columns. Put back `_width` itself, not the width it
    # computed, which would pin it (CLAUDE.md, "Rich reads brackets as markup").
    width, colour = cli.con._width, cli.con._color_system
    err_colour = cli.err._color_system
    cli.con._width = 10_000
    # No colour, even where the environment forces it (FORCE_COLOR): then rich writes no escape
    # sequence of its own, and any in the output came from the data. Stripping rich's instead
    # would strip a leaked `\x1b[8m` (conceal) with them. The same for standard error's console.
    cli.con._color_system = cli.err._color_system = None
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con._width, cli.con._color_system = width, colour
        cli.err._color_system = err_colour
        # A print that raised leaves its text in rich's buffer, and every later print in the
        # process tries to write it again: one regression would fail every test after it.
        del cli.con._buffer[:]
        del cli.err._buffer[:]
    # The runner catches the exception a surrogate raises in the print: say so plainly.
    assert res.exception is None or isinstance(res.exception, SystemExit), repr(res.exception)
    out = res.output
    acting = sorted({f"U+{ord(c):04X}" for c in out
                     if c != "\n" and unicodedata.category(c) in _ACTING})
    assert not acting, f"reached the terminal: {acting} in {out!r}"
    return res.exit_code, (out.splitlines() if lines else " ".join(out.split()))


def _cache_page(data, url=URL, *, title="T", text=TEXT):
    from provenance.fetch import cache_path
    from provenance.models import EXTRACTOR_VERSION

    cache_path(data, url).write_text(PageCache(
        url=url, final_url=url, status=200, content_type="text/html", title=title, text=text,
        fetched_at=datetime.now(UTC) - timedelta(hours=6),
        extractor_version=EXTRACTOR_VERSION).model_dump_json())


def _source(**kw) -> Source:
    d = dict(url=URL, publisher="Example Gazette", author="A. Reporter", date="2030-05-14",
             source_type="bylined_journalism", snippet=SNIPPET)
    d.update(kw)
    return Source(**d)


# --- the issue's acceptance cases ------------------------------------------------------------


def test_a_filer_name_that_erases_the_line_prints_as_text(tmp_path, monkeypatch):
    from provenance import calaccess

    monkeypatch.setattr(calaccess, "find_filers", lambda root, name, limit: [
        {"filer_id": f"1001{CTRL}", "first": "Pat", "last": f"Doe{CTRL}"}])
    code, out = _provenance("calaccess", "filer", "Doe", "--data", tmp_path)
    assert code == 0 and f"1001{SHOWN} Pat Doe{SHOWN}" in out, out
    assert calaccess.committee_url("1001") + SHOWN in out


ENTRY = """\
name: "portal\\e[2K"
access: api
verified: 2030-01-02
naive_fetch: "a shell page\\e[2K\\nand a\\L second line"
limits: "twenty per minute\\x9b2K"
manual_steps: "log in\\u202e"
recipes:
  - id: search
    method: GET
    url: "https://api.news.example/q?term={q}"
    params: [q]
    summary: "search\\e[2K"
    notes: "returns JSON\\e[2K\\nsecond line"
"""


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from provenance import access

    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "news.example.yaml").write_text(ENTRY)
    monkeypatch.setattr(access, "REGISTRY", reg)
    return reg


def test_a_recipe_response_prints_its_escapes_and_keeps_its_lines(registry, monkeypatch):
    """A response body is fetched text, and a researcher copies from it: each line is shown
    through `_printable()`, and a line break stays one. A CR before it is part of the break;
    NEL is a line break to some readers, so it shows."""
    import httpx

    from provenance import access

    body = f'{{"a": "one{CTRL}",\r\n "b": "two\x85"}}\n'
    monkeypatch.setattr(access, "run", lambda recipe, params: httpx.Response(200, text=body))
    code, lines = _provenance("source-access", "news.example", "--run-recipe", "search", "--param",
                              "q=levy", lines=True)
    assert code == 0
    assert f'{{"a": "one{SHOWN}",' in lines and ' "b": "two\\x85"}' in lines, lines
    assert NOTE in " ".join(lines), lines

    monkeypatch.setattr(access, "run", lambda recipe, params: httpx.Response(200, text="{}\r\n"))
    code, out = _provenance("source-access", "news.example", "--run-recipe", "search")
    assert code == 0 and NOTE not in out, out

    def refused(recipe, params):
        raise ValueError(f"recipe needs{CTRL}")

    monkeypatch.setattr(access, "run", refused)
    code, out = _provenance("source-access", "news.example", "--run-recipe", "search")
    assert code == 1 and f"ValueError: recipe needs{SHOWN}" in out, out


@pytest.mark.parametrize("args, code, said", [
    pytest.param(("form700", "Pat", f"Doe{ARGV}"), 0,
                 "No Form 700 filings found for Pat Doe\\udcff.", id="form700"),
    pytest.param(("source-access", f"news{ARGV}.example"), 1,
                 "Nothing recorded for news\\udcff.example.", id="source-access"),
    # A directory named so can't be a project's, so a command given one as its run refuses it.
    pytest.param(("verify", "--data", f"x{ARGV}"), 1, "x\\udcff is neither the root of the",
                 id="verify"),
    pytest.param(("serve", "--data", f"x{ARGV}", "--no-open-browser"), 1,
                 "x\\udcff is neither the root of the", id="serve"),
    pytest.param(("check-claim", f"x{ARGV}/q1.json"), 1, "cannot read x\\udcff/q1.json",
                 id="check-claim"),
    pytest.param(("calaccess", "cite", f"1001{ARGV}"), 0, "id=1001\\udcff", id="cite"),
    pytest.param(("calaccess", "filer", f"Doe{ARGV}"), 0, "No filer matching 'Doe\\udcff'.",
                 id="filer"),
    pytest.param(("query", "calaccess.ie_total", "--param", f"last=Doe{ARGV}"), 1,
                 "bad value Doe\\udcff", id="query"),
])
def test_a_lone_surrogate_in_an_argument_prints_as_an_escape(args, code, said, tmp_path,
                                                             monkeypatch):
    """An undecodable byte in argv reaches the command as a lone surrogate, and each command
    echoes its arguments back somewhere: a refusal, a "nothing found", a path."""
    from provenance import calaccess, fppc, queries

    monkeypatch.chdir(tmp_path)   # a relative --data stays inside the test's directory
    monkeypatch.setattr(fppc, "search", lambda first, last: [])
    monkeypatch.setattr(calaccess, "find_filers", lambda root, name, limit: [])
    monkeypatch.setattr(calaccess, "citable_snapshot", lambda url, **kw: (None, "no capture"))

    def refused(name, params, root):
        raise ValueError(f"bad value {params['last']}")

    monkeypatch.setattr(queries, "run", refused)
    got, out = _provenance(*args)
    assert got == code and said in out, out


# --- text from the CAL-ACCESS export and the FPPC index ---------------------------------------


def test_contributions_print_donor_text_through_printable(tmp_path, monkeypatch):
    from provenance import calaccess

    # The cut cells get what fits their width as shown: `\x1b[2K` shows as 7 characters.
    monkeypatch.setattr(calaccess, "contributions_to", lambda root, filer_id, top, since: [
        calaccess.Contribution(filing_id=f"2002{CTRL}", filer_id="1001",
                               contributor=f"Doe{CTRL}", employer=f"Acme{LONE}",
                               occupation="", amount=None, amount_filed=f"1{LONE}",
                               date=f"2030-01-02{CTRL}", filings=2,
                               unrestated=(calaccess.Unrestated(f"2002{CTRL}", rows_amend=0,
                                                                cover_amend=1),))])
    code, out = _provenance("calaccess", "contributions", "1001", "--data", tmp_path)
    assert code == 0, out
    assert f"1\\ud800 Doe{SHOWN} Acme\\ud800 2030-01-02{SHOWN} 2x" in out
    assert f"2002{SHOWN}: a1 has none" in out


def test_independent_expenditures_print_filer_text_through_printable(tmp_path, monkeypatch):
    """The unreadable-amount cell had no guard at all here, where the contributions listing
    already showed a control character in the same kind of field."""
    from provenance import calaccess

    monkeypatch.setattr(calaccess, "independent_expenditures", lambda root, last, **kw: [
        {"AMOUNT": "n/a\x9b", "stance": f"X{CTRL}", "FILER_NAML": f"PAC{LONE}\x1b[2K",
         "CAND_NAMF": "Pat", "CAND_NAML": "Doe\x1b[2K", "EXP_DATE": f"2030-01-02{CTRL}",
         "cite_url": calaccess.filing_url("2002") + CTRL,
         "unrestated": (calaccess.Unrestated(f"2002{CTRL}", rows_amend=0, cover_amend=1),),
         "reattributed": None}])   # every listed row carries one (#89)
    code, out = _provenance("calaccess", "independent-expenditures", "Doe", "--data", tmp_path)
    assert code == 0, out
    assert (f"n/a\\x9b X{SHOWN} PAC\\ud800\\x1b[2K Pat Doe\\x1b[2K 2030-01-02{SHOWN} "
            f"{calaccess.filing_url('2002')}{SHOWN} 2002{SHOWN}: a1 has none") in out


def test_a_cell_is_cut_to_its_width_as_shown_and_never_inside_an_escape(tmp_path, monkeypatch):
    """Cut before escaping, a cell of control bytes showed four times as wide as its column and
    folded the table. Cut after, it could end inside an escape, which then reads as another
    character. Each escape here shows as 4 characters: 3 fit an amount's 14, 7 a name's 30."""
    from provenance import calaccess

    soh, stx = "\\x01", "\\x02"   # as shown
    monkeypatch.setattr(calaccess, "contributions_to", lambda root, filer_id, top, since: [
        calaccess.Contribution(filing_id="2002", filer_id="1001", contributor="\x02" * 40,
                               employer="Acme", occupation="", amount=None,
                               amount_filed="\x01" * 14, date="2030-01-02")])
    code, out = _provenance("calaccess", "contributions", "1001", "--data", tmp_path)
    assert code == 0 and f"{soh * 3} {stx * 7} Acme" in out, out

    monkeypatch.setattr(calaccess, "independent_expenditures", lambda root, last, **kw: [
        {"AMOUNT": "\x01" * 14, "stance": "support", "FILER_NAML": "PAC", "CAND_NAMF": "Pat",
         "CAND_NAML": "Doe", "EXP_DATE": "2030-01-02", "cite_url": calaccess.filing_url("2002"),
         "unrestated": (), "reattributed": None}])
    code, out = _provenance("calaccess", "independent-expenditures", "Doe", "--data", tmp_path)
    assert code == 0 and f"{soh * 3} support PAC" in out, out


def test_exception_text_keeps_its_own_line_breaks(tmp_path, monkeypatch):
    """Shown whole through `_printable()`, the download command a missing export names, and the
    link an HTTP error ends with, landed on the line before them after a `\\x0a`."""
    from provenance import fppc

    code, lines = _provenance("calaccess", "build", "--data", tmp_path, lines=True)
    assert code == 1 and any(x.startswith("  curl -L -o ") for x in lines), lines

    def failed(first, last):
        raise RuntimeError(f"HTTP 500{LONE}\nFor more information check: https://err.example/")

    monkeypatch.setattr(fppc, "search", failed)
    code, lines = _provenance("form700", "Pat", "Doe", lines=True)
    assert code == 1 and "For more information check: https://err.example/" in lines, lines
    assert "FPPC search failed: RuntimeError: HTTP 500\\ud800" in lines


def test_calaccess_cite_prints_snapshot_notes_through_printable(tmp_path, monkeypatch):
    from provenance import calaccess

    answers = iter([("https://web.archive.org/web/2030/x", f"checked{CTRL}"),
                    (None, f"no capture{LONE}")])
    monkeypatch.setattr(calaccess, "citable_snapshot", lambda url, **kw: next(answers))
    code, lines = _provenance("calaccess", "cite", "1001", "--filing-id", "2002", "--data", tmp_path,
                              lines=True)
    assert code == 0, lines
    # Its own line breaks stay: the snapshot, the note and the live URL are one line each.
    assert f"  checked{SHOWN}" in lines and "filing: no capture\\ud800" in lines, lines


def test_query_prints_its_value_and_notes_through_printable(tmp_path, monkeypatch):
    from provenance import queries

    monkeypatch.setattr(queries, "run", lambda name, params, root: queries.QueryResult(
        value=None, found=False, detail=f"no rows{CTRL}", suggestions=[f"Doe{LONE}"]))
    code, out = _provenance("query", "calaccess.ie_total", "--param", "last=Doe", "--data", tmp_path)
    assert code == 1 and f"no match — no rows{SHOWN} — NO MATCH. Did you mean: Doe\\ud800" in out

    monkeypatch.setattr(queries, "run", lambda name, params, root: queries.QueryResult(
        value=f"Doe{CTRL}", detail=f"top donor{LONE}", version=1))
    code, out = _provenance("query", "calaccess.ie_total", "--param", "last=Doe", "--data", tmp_path)
    assert code == 0 and f"Doe{SHOWN} (top donor\\ud800)" in out, out
    assert NOTE in out   # a researcher copies the value into `expected` as printed

    monkeypatch.setattr(queries, "run", lambda name, params, root: queries.QueryResult(
        value=1250.0, detail=f"total{CTRL}", version=1))
    code, out = _provenance("query", "calaccess.ie_total", "--param", "last=Doe", "--data", tmp_path)
    # Only the note has an escape, and nobody copies the note.
    assert code == 0 and "1250.0" in out and NOTE not in out, out


def test_form700_prints_the_index_through_printable(monkeypatch):
    """The FPPC index is JSON, which can carry a lone surrogate as an escape."""
    from provenance import fppc

    monkeypatch.setattr(fppc, "search", lambda first, last: [fppc.Filing(
        filer="Pat Doe", filed_date=f"2030-03-01{LONE}", filing_years=[2029],
        agencies=[f"Board{CTRL}"], index_id=f"idx{CTRL}")])
    code, out = _provenance("form700", "Pat", f"Doe{CTRL}")
    assert code == 0, out
    assert f"2030-03-01\\ud800 2029 Board{SHOWN} idx{SHOWN}" in out
    assert "Most recent: filed 2030-03-01\\ud800, covering 2029." in out
    assert f"(search Pat Doe{SHOWN})" in out

    def failed(first, last):
        raise RuntimeError(f"HTTP 500{LONE}")

    monkeypatch.setattr(fppc, "search", failed)
    code, out = _provenance("form700", "Pat", "Doe")
    assert code == 1 and "FPPC search failed: RuntimeError: HTTP 500\\ud800" in out, out


# --- fetched pages, and the source-access registry ---------------------------------------------


def test_fetch_and_check_print_page_text_through_printable(tmp_path):
    """Page text keeps its lines, as a researcher copies a snippet from it. An escape in it is
    text the pipeline inserted, and a snippet copied with one is on no page, so `provenance fetch`
    says so wherever it showed one."""
    data = write_project(tmp_path / "data")
    _cache_page(data, title=f"Title{CTRL}", text=f"{TEXT}\nsecond line{CTRL}")
    code, lines = _provenance("fetch", URL, "--data", data, lines=True)
    assert code == 0 and TEXT in lines and f"second line{SHOWN}" in lines, lines
    assert any(f"title: Title{SHOWN}" in x for x in lines), lines
    assert NOTE in " ".join(lines), lines

    _cache_page(data, f"{URL}/plain", text=f"{TEXT}\r\n{TEXT}")   # a CRLF is a line break
    code, out = _provenance("fetch", f"{URL}/plain", "--data", data)
    assert code == 0 and TEXT in out and NOTE not in out, out

    # A cut at 1200 characters between a CR and its LF left the CR to show as an escape.
    _cache_page(data, f"{URL}/crlf", text="a" * 1199 + "\r\nb")
    code, out = _provenance("fetch", f"{URL}/crlf", "--data", data)
    assert code == 0 and "\\x0d" not in out and NOTE not in out, out

    code, out = _provenance("check", URL, "approved the Example Levy on a 4-1 vote", "--data", data)
    assert code == 0 and "OK — literal and unique." in out, out
    code, out = _provenance("check", URL, f"approved the Example Levy{CTRL} on a vote", "--data", data)
    assert code == 1, out


def test_source_access_prints_registry_entries_through_printable(registry):
    code, out = _provenance("source-access")
    assert code == 0 and "news.example api search portal\\x1b[2K" in out, out

    code, lines = _provenance("source-access", "news.example", lines=True)
    assert code == 0, lines
    text = "\n".join(lines)
    for said in ("news.example — portal\\x1b[2K  (api, verified 2030-01-02)",
                 "a plain fetch gets: a shell page\\x1b[2K\nand a\\u2028 second line",
                 "search search\\x1b[2K\n  GET https://api.news.example/q?term={q}",
                 "  returns JSON\\x1b[2K\nsecond line",
                 "limits: twenty per minute\\x9b2K", "manual retrieval: log in\\u202e"):
        assert said in text, said


def test_source_import_curl_and_source_note_print_through_printable(tmp_path, registry,
                                                                    monkeypatch):
    from provenance import access

    monkeypatch.setattr(access, "parse_curl", lambda text: {
        "entry": {"host": "api.news.example", "name": f"portal\u202e", "access": "api",
                  "recipes": [{"id": "r1", "method": "GET",
                               "url": "https://api.news.example/records"}]},
        "dropped_credentials": [f"cookie{CTRL}"], "dropped_headers": [f"x-trace{CTRL}"]})
    paste = tmp_path / "req.sh"
    paste.write_text("curl 'https://api.news.example/records'")
    code, lines = _provenance("source-import-curl", paste, "--no-write", lines=True)
    assert code == 0, lines
    text = "\n".join(lines)
    assert f"dropped credentials: cookie{SHOWN}" in text
    assert f"dropped headers not known to be safe: x-trace{SHOWN}" in text
    assert "name: portal\\u202e\n" in text   # YAML, a line at a time
    assert NOTE in " ".join(lines)

    code, out = _provenance("source-note", "new.example", f"probed; needs a session{CTRL}")
    assert code == 0 and "recorded " in out, out
    # A host names a file, so one with a control character in it is refused, not written.
    code, out = _provenance("source-note", f"new{CTRL}.example", "probed; needs a session")
    assert code == 1 and "not a host name" in out, out
    assert sorted(p.name for p in registry.iterdir()) == ["new.example.yaml", "news.example.yaml"]


# --- claims, verdicts and the files a run keeps -------------------------------------------------


def _run(tmp_path, s: Source):
    """A subject's run, as `provenance new-subject` lays it out, with its one page cached and
    verified, so `provenance judge` accepts a verdict on it."""
    data, cand = write_project(tmp_path / "data", subjects=["cand"]), tmp_path / "data" / "cand"
    _cache_page(data, s.url)
    (cand / "claims").mkdir(parents=True)
    for run in (data, cand):
        (run / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    (cand / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    return cand


def test_agent_written_claims_and_verdicts_print_through_printable(tmp_path):
    """A publisher, a URL and a verdict note are agent-written. The URL passes `_http_only()`,
    which refuses C0 controls and whitespace but not a C1 control or a bidi override."""
    s = _source(url=f"{URL}\x9b2K\u202e", publisher=f"Gazette{CTRL}")
    cand = _run(tmp_path, s)

    code, out = _provenance("verify", "--data", cand)
    assert code == 0 and f"q1 verified Gazette{SHOWN}" in out, out

    code, out = _provenance("handoff", "q1", "--data", cand)
    assert code == 0 and f"{URL}\\x9b2K\\u202e" in out, out
    token = re.search(rf"sid {re.escape(s.sid)}\s+context token (\w+)", out).group(1)
    code, out = _provenance("judge", "q1", s.sid, "topic_only", "--note", f"roll call{CTRL}",
                            "--context", token, "--data", cand)
    assert code == 0 and f"recorded for q1/{s.sid}: roll call{SHOWN}" in out, out

    code, out = _provenance("judgments", "--data", cand)
    assert code == 0 and f"Gazette{SHOWN} {s.sid} topic_only roll call{SHOWN}" in out, out

    code, out = _provenance("check-claim", cand / "claims" / "q1.json", "--data", cand)
    assert code == 0 and f"verified Gazette{SHOWN}: " in out, out


def test_an_unattributed_citation_names_its_arguer_through_printable(tmp_path):
    """The refusal quotes the publisher and the names the answer could take, both
    agent-written. Inside each name, since a name is taken stripped, and U+2028 strips. And
    as written, brackets included: split across the two names, `[/` and `x]` would close a tag."""
    s = _source(source_type="opinion", publisher=f"Ex{CTRL}Gazette x]",
                author=f"A.{CTRL}Writer [/")
    cand = _run(tmp_path, s)
    code, out = _provenance("check-claim", cand / "claims" / "q1.json", "--data", cand)
    assert code == 1 and f"not attributed opinion Ex{SHOWN}Gazette x]: " in out, out
    assert f"'A.{SHOWN}Writer [/' or 'Ex{SHOWN}Gazette x]'" in out, out


def test_a_shard_or_claim_file_named_with_control_characters_prints_as_an_escape(tmp_path):
    """File names come from a listing, not from the schema: a shard no claim has is named by its
    file name, and so is a claim file that duplicates another."""
    cand = _run(tmp_path, _source())
    (cand / "judgments").mkdir()
    (cand / "judgments" / f"x{CTRL}.json").write_text(json.dumps([{
        "sid": _source().sid, "verdict": "supports", "note": "",
        "judged_at": "2030-01-01T00:00:00+00:00"}]))
    code, out = _provenance("judgments", "--data", cand)
    assert f"under an id no claim has (x{SHOWN}.json)" in out, out

    (cand / "claims" / f"q1{CTRL}.json").write_text((cand / "claims" / "q1.json").read_text())
    code, out = _provenance("status", "--data", cand)
    assert code == 1 and f"in q1{SHOWN}.json and q1.json" in out, out


def test_a_claim_that_does_not_parse_is_skipped_with_its_error_through_printable(tmp_path):
    """pydantic's message names each field it refused by its path, and a dict key in that path
    is agent-written. Its message has line breaks of its own, and they stay."""
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a",
        "sources": [{**_source().model_dump(mode="json"),
                     "query": {"name": "calaccess.ie_total", "params": {f"k{CTRL}": 5}}}]}))
    code, out = _provenance("status", "--data", tmp_path)
    assert code == 0 and "skipping q1 in q1.json" in out, out


def test_project_and_subject_names_print_through_printable(tmp_path):
    data = write_project(tmp_path / "data", title="Assessor\x1b[2K",
                         subjects=[{"id": "pd", "name": "Pat Doe\u202e"}],
                         context="Where records are\x1b[2K kept.\n")
    (data / "questions.json").write_text(json.dumps([{"id": "q1", "text": "What?"}]))
    code, out = _provenance("new-subject", "pd", "--data", data)
    assert code == 0 and "retargeted to Pat Doe\\u202e)" in out, out
    code, out = _provenance("brief", "--data", data / "pd")
    assert code == 0 and "Project: Assessor\\x1b[2K" in out and "Pat Doe\\u202e" in out, out
    assert "records are\\x1b[2K kept" in out, out

    # A project file refused for a subject with no name quotes the subject it names.
    toml = data / "provenance.toml"
    toml.write_text(re.sub(r"(?m)^subjects = .*$", lambda _: 'subjects = ["us\\u001b[2K"]',
                           toml.read_text()))
    code, out = _provenance("build", "--data", data)
    assert code == 1 and "subject 'us\\x1b[2K' needs a name" in out, out


def test_a_run_directory_named_with_control_characters_prints_as_an_escape(tmp_path):
    """The operator's --data path is printed wherever a command names what it read or wrote."""
    data = write_project(tmp_path / f"run{CTRL}")
    (data / "claims").mkdir(parents=True)
    code, out = _provenance("build", "--data", data)
    assert code == 0, out
    assert f"no questions.json in {tmp_path}/run{SHOWN}" in out
    assert f"wrote {tmp_path}/run{SHOWN}/out/review.html" in out


def test_archive_prints_what_save_page_now_said_through_printable(tmp_path, monkeypatch):
    from provenance import archive as arch

    cand = _run(tmp_path, _source(publisher=f"Gazette{CTRL}"))
    monkeypatch.setattr(arch, "have_credentials", lambda: True)
    monkeypatch.setattr(arch, "archive_all", lambda urls, delay, progress: [
        progress(u, None, f"save failed{CTRL}") for u in urls])
    code, out = _provenance("archive", "--data", cand, "--delay", "0")
    assert code == 0 and f"fail {URL} save failed{SHOWN}" in out, out


def test_clear_contradiction_names_a_source_id_argument_as_an_escape(tmp_path):
    cand = _run(tmp_path, _source())
    code, out = _provenance("clear-contradiction", "q1", f"abc{ARGV}{CTRL}", "--data", cand)
    assert code == 1 and f"no contradicts verdict on abc\\udcff{SHOWN} to clear" in out, out
