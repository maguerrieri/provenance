"""The CLI prints through rich, which reads "[...]" in a printed string as markup. Much of what
it prints is text the pipeline does not control: agent-written claims and verdict notes, pages
and responses it fetched, filer names from the CAL-ACCESS export, race and registry files, the
arguments an agent passed, and exception text quoting any of those.

Unescaped, a "[/]" in any of it raised rich.errors.MarkupError, so the command died with a
traceback, and a "[b]" or "[sic]" vanished from what the operator saw. escape() leaves emoji
shortcodes alone, so ":ok:" printed as an emoji until the console stopped reading them. Each
test here puts such text where one print site interpolates it and checks that it comes out as
written.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from rich.errors import MarkupError

from provenance.models import Claim, PageCache, Source

# "[/]" raises when nothing is open; "[sic]" opens a style rich drops without a word. Which
# of the two a site shows depends on the tags around it, so every test asserts the literal.
# ":ok:" is an emoji shortcode, which escape() does not touch.
MARK = "[/] [sic] :ok:"

URL = "https://news.example/levy"
TEXT = "The board approved the Example Levy on a 4-1 vote after a long hearing on Tuesday."
SNIPPET = "approved the Example Levy on a 4-1 vote"


def _provenance(*args):
    """Run `provenance`, returning (exit code, output with ANSI codes and wrapping removed)."""
    from typer.testing import CliRunner

    from provenance import cli

    # rich folds a long tmp path mid-word at 80 columns. Put back `_width` itself, not the width
    # it computed: assigning that would pin it, and COLUMNS would stop reaching the console.
    width = cli.con._width
    cli.con._width = 10_000
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con._width = width
    # The runner catches the exception: say so plainly rather than as a wrong exit code.
    assert not isinstance(res.exception, MarkupError), f"printed as markup: {res.exception}"
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _source(**kw) -> Source:
    d = dict(url=URL, publisher="Example Gazette", author="A. Reporter", date="2030-05-14",
             source_type="bylined_journalism", snippet=SNIPPET)
    d.update(kw)
    return Source(**d)


def _run(tmp_path, s: Source):
    """A candidate run as `provenance new-candidate` lays it out, with one cited page cached and
    verified, so `provenance judge` accepts a verdict on it and `provenance judgments` lists it."""
    from provenance.fetch import cache_path
    from provenance.models import EXTRACTOR_VERSION

    data, cand = tmp_path / "data", tmp_path / "data" / "cand"
    cache_path(data, s.url).write_text(PageCache(
        url=s.url, final_url=s.url, status=200, content_type="text/html", title="T", text=TEXT,
        fetched_at=datetime.now(UTC) - timedelta(hours=6),
        extractor_version=EXTRACTOR_VERSION).model_dump_json())
    (data / "questions.json").write_text("[]")
    (cand / "claims").mkdir(parents=True)
    (cand / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    code, out = _provenance("verify", "--data", cand)   # offline: the page is cached
    assert code == 0 and "verified" in out, out
    return cand


def _bad_claim(claims_dir, qid=MARK):
    """A claim file that parses as JSON but not as a claim, under an id that is markup."""
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "bad.json").write_text(json.dumps({"question_id": qid, "question": "?"}))


# --- the issue's acceptance cases ------------------------------------------------------------


def test_a_verdict_note_in_markup_is_recorded_and_listed_as_written(tmp_path):
    """`provenance judge` printed its success line after the verdict was on disk, so a `[/]` in the note
    exited non-zero for a write that happened. And one such note made `provenance judgments`, the
    listing every verifier reads, unusable for the whole run."""
    cand = _run(tmp_path, _source(publisher=f"{MARK} Gazette"))
    sid = _source(publisher=f"{MARK} Gazette").sid
    note = f"roll call only {MARK}"

    # The token the verifier hands back, from the hand-off it reads (which names the source too).
    code, out = _provenance("handoff", "q1", "--data", cand)
    assert code == 0 and f"{MARK} Gazette" in out, out
    token = re.search(rf"sid {re.escape(sid)}\s+context token (\w+)", out)
    assert token, out

    code, out = _provenance("judge", "q1", sid, "topic_only", "--note", note, "--context",
                            token.group(1), "--data", cand)
    assert code == 0 and f"recorded for q1/{sid}: {note}" in out, out

    code, out = _provenance("judgments", "--data", cand)
    assert code == 0, out
    assert f"{MARK} Gazette {sid}" in out and note in out
    assert out.endswith("0 of 1 cited source(s) need a verdict (0 stale)")


def test_a_claim_skipped_as_unreadable_is_named_as_written(tmp_path):
    """The per-claim line escaped the id, and the summary line under it did not: a malformed
    claim whose id holds `[/]` turned the skip into a traceback, for every command that loads
    claims."""
    _bad_claim(tmp_path / "claims")
    code, out = _provenance("status", "--data", tmp_path)
    assert code == 0, out
    assert f"skipping {MARK} in bad.json" in out
    assert f"1 claim(s) skipped as unreadable: {MARK} — fix or re-run" in out


def test_the_judgment_gate_names_an_unreadable_claim_as_written(tmp_path):
    """`provenance judgments` refuses to print its gate while a claim is unreadable, naming each one.
    With `[/]` in the id the refusal was a traceback instead."""
    _bad_claim(tmp_path / "claims")
    code, out = _provenance("judgments", "--data", tmp_path)
    assert code == 1
    assert f"1 claim(s) could not be read, so nothing here counts as done: {MARK}" in out


def test_the_judgment_gate_names_a_missing_question_id_as_given(tmp_path):
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a").model_dump_json())
    code, out = _provenance("judgments", "--data", tmp_path, "--question-id", MARK)
    assert code == 1 and f"no claim with question id {MARK!r} in" in out, out


# --- CAL-ACCESS: filer text from the export, and the ids an agent passes ---------------------


def test_calaccess_filer_prints_filer_names_and_arguments_as_written(tmp_path, monkeypatch):
    from provenance import calaccess

    monkeypatch.setattr(calaccess, "find_filers", lambda root, name, limit: [
        {"filer_id": "1001", "first": "Pat", "last": MARK}])
    code, out = _provenance("calaccess", "filer", "Pat", "--data", tmp_path)
    assert code == 0 and f"1001 Pat {MARK}" in out, out

    monkeypatch.setattr(calaccess, "find_filers", lambda root, name, limit: [])
    code, out = _provenance("calaccess", "filer", MARK, "--data", tmp_path)
    assert code == 0 and f"No filer matching {MARK!r}." in out, out

    def missing(root, name, limit):
        raise FileNotFoundError(f"no database under {root}/[b]")

    monkeypatch.setattr(calaccess, "find_filers", missing)
    code, out = _provenance("calaccess", "filer", "Pat", "--data", tmp_path)
    assert code == 1 and "/[b]" in out, out


def test_calaccess_contributions_print_donor_text_as_filed(tmp_path, monkeypatch):
    from provenance import calaccess

    monkeypatch.setattr(calaccess, "contributions_to", lambda root, filer_id, top, since: [
        calaccess.Contribution(filing_id="2002", filer_id="1001", contributor=f"{MARK} Doe",
                               employer="[b]Acme", occupation="", amount=500.0,
                               date="1/2/2030 12:00:00 AM", filings=2)])
    code, out = _provenance("calaccess", "contributions", "1001", "--data", tmp_path)
    assert code == 0, out
    assert f"$500 {MARK} Doe [b]Acme 1/2/2030 12:00:00 AM 2x" in out

    # escape() adds a backslash to a value ending in one, which only a following tag takes
    # back: at the end of a cell it printed. A truncated name can end anywhere.
    monkeypatch.setattr(calaccess, "contributions_to", lambda root, filer_id, top, since: [
        calaccess.Contribution(filing_id="2002", filer_id="1001", contributor="Doe\\",
                               employer="Acme", occupation="", amount=5.0, date="")])
    code, out = _provenance("calaccess", "contributions", "1001", "--data", tmp_path)
    assert code == 0 and "$5 Doe\\ Acme" in out, out

    def refused(root, filer_id, top, since):
        raise ValueError(f"--since {since!r} is not a date")

    monkeypatch.setattr(calaccess, "contributions_to", refused)
    code, out = _provenance("calaccess", "contributions", "1001", "--since", MARK, "--data", tmp_path)
    assert code == 1 and f"--since {MARK!r} is not a date" in out, out


def test_calaccess_independent_expenditures_print_names_as_filed(tmp_path, monkeypatch):
    from provenance import calaccess

    monkeypatch.setattr(calaccess, "independent_expenditures", lambda root, last, **kw: [
        {"AMOUNT": "250", "stance": "support", "FILER_NAML": f"{MARK} PAC",
         "CAND_NAMF": "[b]Pat", "CAND_NAML": "Doe", "EXP_DATE": "1/2/2030",
         "cite_url": calaccess.filing_url("2002"),
         "unrestated": (calaccess.Unrestated("2002[/]", rows_amend=0, cover_amend=1),),
         "reattributed": None}])   # every listed row carries one (#89)
    code, out = _provenance("calaccess", "independent-expenditures", "Doe", "--data", tmp_path)
    assert code == 0, out
    assert f"$250 support {MARK} PAC [b]Pat Doe 1/2/2030" in out
    assert "2002[/]: a1 has none" in out

    def missing(root, last, **kw):
        raise FileNotFoundError(f"no database under {root}/[b]")

    monkeypatch.setattr(calaccess, "independent_expenditures", missing)
    code, out = _provenance("calaccess", "independent-expenditures", "Doe", "--data", tmp_path)
    assert code == 1 and "/[b]" in out, out


def test_calaccess_cite_prints_snapshots_notes_and_urls_as_written(tmp_path, monkeypatch):
    from provenance import calaccess

    answers = iter([("https://web.archive.org/web/2030/x[b]", f"checked {MARK}"),
                    (None, f"no capture {MARK}")])
    monkeypatch.setattr(calaccess, "citable_snapshot", lambda url, **kw: next(answers))
    code, out = _provenance("calaccess", "cite", "1001[b]", "--filing-id", "2002", "--data", tmp_path)
    assert code == 0, out
    assert f"committee page: https://web.archive.org/web/2030/x[b] checked {MARK}" in out
    assert "live URL for the human: " + calaccess.committee_url("1001[b]") in out
    assert f"filing: no capture {MARK}" in out


def test_calaccess_build_names_its_paths_as_given(tmp_path):
    """The operator's --data path, in the refusal and in the success line."""
    import zipfile

    root = tmp_path / "[b]"
    code, out = _provenance("calaccess", "build", "--data", root)
    assert code == 1 and "/[b]/" in out, out

    (root / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/FILERNAME_CD.TSV", "FILER_ID\tNAML\tNAMF\n1001\tDoe\tPat\n")
    code, out = _provenance("calaccess", "build", "--data", root)
    assert code == 0 and f"built {root}/" in out, out


# --- provenance query: near-matches come from the export, errors quote the params -------------------


def test_query_prints_its_no_match_note_and_errors_as_written(tmp_path, monkeypatch):
    from provenance import queries

    monkeypatch.setattr(queries, "run", lambda name, params, root: queries.QueryResult(
        value=None, found=False, detail=f"no rows for {params['last']}"))
    code, out = _provenance("query", "calaccess.ie_total", "--param", f"last={MARK}", "--data", tmp_path)
    assert code == 1 and f"no match — no rows for {MARK}" in out, out

    def refused(exc):
        def run(name, params, root):
            raise exc(f"bad value {params['last']!r}")
        return run

    for exc in (ValueError, TypeError):
        monkeypatch.setattr(queries, "run", refused(exc))
        code, out = _provenance("query", "calaccess.ie_total", "--param", f"last={MARK}",
                                "--data", tmp_path)
        assert code == 1 and f"bad value {MARK!r}" in out, (exc, out)


# --- the source-access registry: YAML files, pasted cURL, and what a recipe fetched ----------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from provenance import access

    reg = tmp_path / "reg[b]"
    reg.mkdir()
    monkeypatch.setattr(access, "REGISTRY", reg)
    return reg


# `verified` is unquoted, so YAML reads it as a date. The second recipe's summary is blank, which
# YAML reads as None, and its params and notes are numbers. Neither escape() nor .strip() takes
# anything but a str.
ENTRY = f"""\
name: "{MARK} portal"
access: api
verified: 2030-01-02
naive_fetch: "a shell page {MARK}"
limits: "twenty per minute {MARK}"
manual_steps: "log in {MARK}"
recipes:
  - id: search
    method: GET
    url: "https://api.news.example/q?term={{q}}&x=[b]"
    params: [q]
    summary: "search {MARK}"
    notes: "returns JSON {MARK}"
  - id: 7
    method: GET
    url: "https://api.news.example/7"
    summary:
    params: [2030]
    notes: 404
"""


def test_source_access_prints_registry_entries_as_written(registry):
    (registry / "news.example.yaml").write_text(ENTRY)

    code, out = _provenance("source-access")
    assert code == 0 and f"news.example api search, 7 {MARK} portal" in out, out

    code, out = _provenance("source-access", "news.example")
    assert code == 0, out
    for text in (f"news.example — {MARK} portal (api, verified 2030-01-02)",
                 f"a plain fetch gets: a shell page {MARK}",
                 f"search search {MARK} GET https://api.news.example/q?term={{q}}&x=[b]",
                 f"returns JSON {MARK}", f"limits: twenty per minute {MARK}",
                 f"manual retrieval: log in {MARK}",
                 "GET https://api.news.example/7 params: 2030 404"):
        assert text in out, text

    code, out = _provenance("source-access", "nothing[b].example")
    assert code == 1 and "Nothing recorded for nothing[b].example." in out, out

    code, out = _provenance("source-access", "news.example", "--run-recipe", MARK)
    assert code == 1 and f"no recipe {MARK!r}" in out, out


def _unwrapped(*args):
    """Run `provenance` at rich's 80-column default for a shell that is not a terminal, and return its
    lines as printed: what an agent copies from."""
    from typer.testing import CliRunner

    from provenance import cli

    res = CliRunner().invoke(cli.app, [str(a) for a in args])
    assert res.exit_code == 0, res.output
    return re.sub(r"\x1b\[[0-9;]*m", "", res.output).splitlines()


def test_source_access_prints_a_recipe_response_as_fetched(registry, monkeypatch):
    """A response body is fetched text, and a researcher copies values from it: printed as
    markup it lost "[b]", and at 80 columns rich broke its lines, mid-value too."""
    import httpx

    from provenance import access

    (registry / "news.example.yaml").write_text(ENTRY)
    body = '{"hits": [' + ", ".join(f'"{MARK} {i}"' for i in range(8)) + "]}"
    monkeypatch.setattr(access, "run", lambda recipe, params: httpx.Response(200, text=body))
    lines = _unwrapped("source-access", "news.example", "--run-recipe", "search",
                       "--param", "q=levy")
    assert body in lines, lines

    def refused(recipe, params):
        raise ValueError(f"recipe needs {MARK}")

    monkeypatch.setattr(access, "run", refused)
    code, out = _provenance("source-access", "news.example", "--run-recipe", "search")
    assert code == 1 and f"ValueError: recipe needs {MARK}" in out, out


def test_source_note_names_the_file_it_wrote_as_it_is(registry):
    code, out = _provenance("source-note", "news.example", "probed; needs a session")
    assert code == 0 and f"recorded {registry}/news.example.yaml" in out, out


def test_source_import_curl_prints_the_pasted_request_as_written(tmp_path, registry):
    """Everything here comes from a browser's "copy as cURL": the URL, the header names it
    drops, and the entry it prints."""
    paste = tmp_path / "req.sh"
    url = "https://api.news.example/records/by-name?term=[b]&page=1&per_page=100&order=desc"
    paste.write_text(f"curl '{url}' -H 'Cookie: a=1' -H 'Accept: {MARK}'")
    code, out = _provenance("source-import-curl", paste, "--no-write")
    assert code == 0, out
    assert "dropped credentials: cookie" in out and MARK in out
    # YAML to be copied into a file, so not broken at 80 columns
    assert f"  url: {url}" in _unwrapped("source-import-curl", paste, "--no-write")

    code, out = _provenance("source-import-curl", paste)
    assert code == 0 and f"wrote {registry}/api.news.example.yaml" in out, out
    code, out = _provenance("source-import-curl", paste)
    assert code == 0 and f"{registry}/api.news.example.yaml exists" in out, out
    assert f"url: {url}" in out

    code, out = _provenance("source-import-curl", tmp_path / "[b]" / "missing.sh")
    assert code == 1 and "/[b]/missing.sh" in out, out


# --- Form 700: the FPPC index's own text, and the names an agent searched for ---------------


def test_form700_prints_the_index_and_the_search_as_written(monkeypatch):
    from provenance import fppc

    monkeypatch.setattr(fppc, "search", lambda first, last: [fppc.Filing(
        filer="Pat Doe", filed_date="2030-03-01 [b]", filing_years=[2029],
        agencies=[f"{MARK} Board"], index_id="idx[b]")])
    code, out = _provenance("form700", "Pat", "[b]Doe")
    assert code == 0, out
    assert f"2030-03-01 [b] 2029 {MARK} Board idx[b]" in out
    assert "Most recent: filed 2030-03-01 [b], covering 2029." in out
    assert "(search Pat [b]Doe)" in out

    monkeypatch.setattr(fppc, "search", lambda first, last: [])
    code, out = _provenance("form700", "Pat", "[b]Doe")
    assert code == 0 and "No Form 700 filings found for Pat [b]Doe." in out, out

    def failed(first, last):
        raise RuntimeError(f"HTTP 500 {MARK}")

    monkeypatch.setattr(fppc, "search", failed)
    code, out = _provenance("form700", "Pat", "Doe")
    assert code == 1 and f"FPPC search failed: RuntimeError: HTTP 500 {MARK}" in out, out


# --- a tag split across two values -----------------------------------------------------------


def test_a_tag_split_across_two_values_prints_as_written(tmp_path, monkeypatch):
    """escape() neutralises only a tag complete inside the one value it is given. "[/" in one
    value and "x]" in the next, with plain text between, still made the closing tag "[/ x]":
    a MarkupError, or with "[b" a silently dropped run of text. A run of data is escaped as one
    string."""
    from provenance import calaccess, fppc, races

    monkeypatch.setattr(fppc, "search", lambda first, last: [])
    code, out = _provenance("form700", "[/", "x]")
    assert code == 0 and "No Form 700 filings found for [/ x]." in out, out
    code, out = _provenance("form700", "Pat [b", "x] Doe")
    assert code == 0 and "No Form 700 filings found for Pat [b x] Doe." in out, out

    monkeypatch.setattr(calaccess, "citable_snapshot", lambda url, **kw: (None, "no capture [/"))
    code, out = _provenance("calaccess", "cite", "x]", "--data", tmp_path)
    assert code == 0 and "committee page: no capture [/ live URL: " in out, out
    assert calaccess.committee_url("x]") in out

    d = tmp_path / "races"
    d.mkdir()
    (d / "split.md").write_text('---\nname: split\ntitle: "[b"\nsources: ["x]"]\n---\n')
    monkeypatch.setattr(races, "RACES_DIR", d)
    code, out = _provenance("races")
    assert code == 0 and "split [b sources: x]" in out, out


def test_a_path_beside_another_value_prints_as_written(tmp_path, monkeypatch):
    """The same split with the operator's --data path as one half: a directory named "[" puts
    "[/" in it, and the other half is the path's own parent, a question set's maps_from (which
    nothing checks), or the error that names the path."""
    from provenance import cli

    data = tmp_path / "x]" / "[" / "c"
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a").model_dump_json())
    code, out = _provenance("status", "--data", data)
    assert code == 0 and f"no questions.json in {data} or {data.parent}, so" in out, out

    (data / "questions.json").write_text(
        json.dumps([{"id": "q1", "text": "?", "maps_from": "x]"}]))
    code, out = _provenance("status", "--data", data)
    assert code == 0, out
    assert f"{data}/questions.json still declares maps_from (q1 from x])" in out

    # The path twice in one line, "[/" in the first and "x]" in the second.
    (data / "questions.json").write_text(json.dumps([{"id": "q2", "text": "?"}]))
    code, out = _provenance("status", "--data", data)
    assert code == 1, out
    assert f"sit on an id {data}/questions.json does not list: q1. If" in out
    assert f"add it to {data}/questions.json under that id" in out

    def refused(out):
        raise OSError("permission denied: x]")

    monkeypatch.setattr(cli, "clear_render", refused)
    code, out = _provenance("build", "--data", data)
    assert code == 1, out
    assert f"from {data}/out: permission denied: x]. Remove it by hand" in out


# --- a lone surrogate at the shared helpers ---------------------------------------------------


def test_a_lone_surrogate_in_a_skipped_claim_id_prints_as_an_escape(tmp_path):
    """json.loads keeps a lone surrogate, and printing one raises UnicodeEncodeError: the skip
    line and every later list of skipped ids died on it, in every command that loads claims."""
    _bad_claim(tmp_path / "claims", qid="q\ud800")
    code, out = _provenance("status", "--data", tmp_path)
    assert code == 0, out
    assert "skipping q\\ud800 in bad.json" in out
    assert "1 claim(s) skipped as unreadable: q\\ud800 — fix" in out

    code, out = _provenance("judgments", "--data", tmp_path)
    assert code == 1 and "nothing here counts as done: q\\ud800" in out, out


def test_a_lone_surrogate_in_cache_is_named_as_an_escape(tmp_path, monkeypatch):
    """An undecodable byte in argv arrives as a lone surrogate. `_cache_root()` and
    `_verdict_cache_root()` refuse such a --cache for `provenance judge` and `provenance handoff` too, and the
    refusal raised instead of printing."""
    from pathlib import Path

    (tmp_path / "claims").mkdir()
    code, out = _provenance("judgments", "--data", tmp_path, "--cache", "x\udcff")
    assert code == 1 and "--cache x\\udcff holds no cache/ directory" in out, out

    # A cache directory itself: a disk that refuses the name can't hold one, so say it does.
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    code, out = _provenance("judgments", "--data", tmp_path, "--cache", "x\udcff")
    assert code == 1 and "--cache x\\udcff is a cache directory itself" in out, out


# --- paths, race files and exception text elsewhere ------------------------------------------


def test_check_claim_names_an_unreadable_file_as_given(tmp_path):
    code, out = _provenance("check-claim", tmp_path / "[b]" / "q1.json", "--data", tmp_path)
    assert code == 1 and f"cannot read {tmp_path}/[b]/q1.json" in out, out


def test_verify_names_an_empty_claims_directory_as_given(tmp_path):
    code, out = _provenance("verify", "--data", tmp_path / "[b]")
    assert code == 1 and f"No claims found in {tmp_path}/[b]/claims" in out, out


def test_build_names_what_it_wrote_as_it_is(tmp_path):
    data = tmp_path / "[b]"
    (data / "claims").mkdir(parents=True)
    code, out = _provenance("build", "--data", data)
    assert code == 0 and f"wrote {data}/out/review.html" in out, out


def test_serve_names_what_it_serves_as_it_is(tmp_path, monkeypatch):
    import http.server

    class NoServer:
        def __init__(self, address, handler):
            pass

        def serve_forever(self):
            pass

    data = tmp_path / "[b]"
    (data / "out").mkdir(parents=True)
    (data / "out" / "review.html").write_text("")
    monkeypatch.setattr(http.server, "ThreadingHTTPServer", NoServer)
    code, out = _provenance("serve", "--data", data, "--no-open-browser")
    assert code == 0 and f"Serving {data.resolve()}/out at" in out, out


def test_status_prints_a_race_error_as_written(tmp_path):
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a").model_dump_json())
    code, out = _provenance("status", "--data", tmp_path, "--race", MARK)
    assert code == 0 and f"no race {MARK!r} in" in out, out


@pytest.fixture
def marked_race(tmp_path, monkeypatch):
    from provenance import races

    d = tmp_path / "races"
    d.mkdir()
    (d / "marked.md").write_text(
        "---\n"
        "name: marked\n"
        f'title: "{MARK} Assessor"\n'
        "sources: [us]\n"
        "candidates:\n"
        '  - {id: pd, name: "[b]Pat Doe"}\n'
        "---\n\nA fictional race.\n")
    # YAML reads this title as a number, and escape() takes only a str.
    (d / "numbered.md").write_text("---\nname: numbered\ntitle: 2030\nsources: [us]\n---\n")
    monkeypatch.setattr(races, "RACES_DIR", d)


def test_races_prints_a_race_file_as_written(marked_race):
    code, out = _provenance("races")
    assert code == 0 and f"marked {MARK} Assessor sources: us" in out, out
    assert "numbered 2030 sources: us" in out


def test_new_candidate_prints_the_candidate_and_its_paths_as_written(tmp_path, marked_race):
    data = tmp_path / "[b]data"
    data.mkdir()
    (data / "questions.json").write_text(json.dumps([{"id": "q1", "text": "What has he said?"}]))
    code, out = _provenance("new-candidate", "pd", "--data", data, "--race", "marked")
    assert code == 0, out
    assert f"wrote {data}/pd/questions.json (1 questions retargeted to [b]Pat Doe)" in out
    assert f"ready {data}/pd" in out and f"provenance build --data {data}/pd --candidate pd" in out

    code, out = _provenance("new-candidate", "pd", "--data", data, "--race", "marked")
    assert code == 0 and f"{data}/pd/questions.json already exists" in out, out
