"""A verdict names what the verifier was handed, and `vg judge` refuses any other.

`vg judge` checks the claim file as it is when judge runs. It already refuses when the copy of
the page `vg verify` built the context from is no longer the one cached. What that check
passes is a re-verify that lands while a verifier works: another run re-fetches the page, this
one rebuilds the context from the new copy, and the claim file names a copy that is cached and
a context no verifier has read. The verdict used to be recorded, and the row rendered green on
that context. `vg handoff` now prints the claim and each source's context with a token, `vg
judge --context` hands the token back, and a token for anything else writes nothing. A query
citation's token also names the run that produced its context, since a re-run under another
definition can print exactly the same text.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vgpipe import cli, judgments, queries
from vgpipe.fetch import cache_path
from vgpipe.models import (
    EXTRACTOR_VERSION,
    Claim,
    PageCache,
    QueryCitation,
    QueryRun,
    Source,
)

URL = "https://bay-courier.example/tideland-lease"
SNIPPET = "voted 5-2 to adopt the tideland lease"
STORY = f"On Tuesday the council {SNIPPET}, over the harbor board's objection [sic].\n"
# The other run's re-fetch: same quote, a changed clause beside it inside the context window.
RESTORY = STORY.replace("over the harbor board's objection", "and repealed it a week later")


def _source(**kw) -> Source:
    d = dict(url=URL, publisher="Bay Courier", author="M. Reporter", date="2030-03-04",
             source_type="bylined_journalism", snippet=SNIPPET)
    d.update(kw)
    return Source(**d)


def _cache(run, text: str, fetched_at: datetime, url: str = URL) -> None:
    page = PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=text, fetched_at=fetched_at, extractor_version=EXTRACTOR_VERSION)
    cache_path(run, url).write_text(page.model_dump_json())


def _vg(*args) -> tuple[int, str]:
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception   # a crash also exits 1, so it must not pass for a refusal
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)   # when FORCE_COLOR is set


def _run(tmp_path, *sources: Source, story: str = STORY):
    """A run with one claim, its page cached six hours ago and verified offline."""
    _cache(tmp_path, story, datetime.now(UTC) - timedelta(hours=6))
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="How did the council vote on the tideland lease?",
        answer="It adopted the lease 5-2.", sources=list(sources or [_source()]),
    ).model_dump_json())
    code, out = _vg("verify", "--data", tmp_path)
    assert code == 0, out
    return tmp_path


def _handed(run) -> dict[str, tuple[str, str]]:
    """{sid: (token, context)} as `vg handoff q1` prints them: what a verifier is given."""
    code, out = _vg("handoff", "q1", "--data", run)
    assert code == 0, out
    return _handed_from(out)


def _handed_from(out: str) -> dict[str, tuple[str, str]]:
    handed = {}
    for m in re.finditer(r"sid (\w+)  context token (\w+)\n(?:  (?!context:).*\n)*"
                         r"  context:\n((?:  \| .*\n)+)", out):
        text = "\n".join(ln.removeprefix("  | ") for ln in m.group(3).splitlines())
        handed[m.group(1)] = (m.group(2), text)
    return handed


def _claim(run) -> Claim:
    return cli.load_claims(run / "claims", trust_machine_fields=True)[0]


def _token(run) -> str:
    claim = _claim(run)
    return judgments.context_token(claim, claim.sources[0])


def _edit(run, **fields) -> None:
    """Rewrite claim q1 in place, as a retry or a hand edit would, without re-verifying."""
    p = run / "claims" / "q1.json"
    raw = json.loads(p.read_text())
    for key, value in fields.items():
        *path, last = key.split("__")
        node = raw
        for part in path:
            node = node[int(part)] if part.isdigit() else node[part]
        node[last] = value
    p.write_text(json.dumps(raw))


def _shards(run) -> dict[str, bytes]:
    d = run / "judgments"
    return {p.name: p.read_bytes() for p in d.iterdir()} if d.exists() else {}


def _unreviewed_after_build(run) -> list[str]:
    code, out = _vg("build", "--data", run)
    assert code == 0, out
    built = json.loads((run / "out" / "claims.json").read_text())
    return [s["url"] for c in built for s in c["sources"]
            if s["verification"]["support"] == "unreviewed"]


def test_a_verdict_on_a_context_rebuilt_since_the_hand_off_is_refused(tmp_path):
    """The reported sequence. Handed C1, the verifier works while another run re-fetches the
    page and a re-verify rebuilds the context as C2 from the new copy, which is what is cached.
    The copy check passes that, so only the token can tell the verdict was about C1."""
    run, s = _run(tmp_path), _source()
    [(token1, handed1)] = _handed(run).values()
    assert handed1 == STORY.removesuffix("\n") and "[sic]" in handed1, (
        "the hand-off prints the context verbatim, brackets and all")
    assert token1 == _token(run)

    _cache(run, RESTORY, datetime.now(UTC))           # another run's re-fetch
    assert _vg("verify", "--data", run)[0] == 0       # and a re-verify of this claim
    rebuilt = _claim(run).sources[0].verification.context
    assert "repealed" in rebuilt and "repealed" not in handed1
    token2 = _token(run)

    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token1, "--data", run)
    assert code == 1 and "not recorded" in out and f"vg handoff q1 --data {run}" in out, out
    assert token2 not in out, "a refusal that printed the current token invites a blind retry"
    assert _shards(run) == {}, "refused, writing nothing"
    assert _unreviewed_after_build(run) == [URL], "nothing renders green on text no one read"

    # what the hand-off gives now is what can be judged
    assert _handed(run) == {s.sid: (token2, rebuilt.removesuffix("\n"))}
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", f" {token2.upper()} ",
                    "--data", run)
    assert code == 0 and "supports recorded for q1" in out, out
    assert _unreviewed_after_build(run) == []


@pytest.mark.parametrize("edit", [
    {"answer": "It adopted the lease unanimously."},
    {"sources__0__date": "2029-03-04"},            # `superseded` turns on a filing's date
    {"sources__0__publisher": "Bay Courier Weekly"},
], ids=["answer", "date", "publisher"])
def test_a_verdict_on_a_claim_rewritten_since_the_hand_off_is_refused(tmp_path, edit):
    """A retry that rewrites only the answer, or only a citation's date, keeps the sid (url and
    snippet) and the context, so the verdict would carry over to what no verifier read. The
    token covers everything the hand-off shows the verifier to judge from."""
    run, s = _run(tmp_path), _source()
    [(token1, _)] = _handed(run).values()
    _edit(run, **edit)
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token1, "--data", run)
    assert code == 1 and "different claim, citation or context" in out, out
    assert _shards(run) == {}


def test_a_page_verdict_without_a_token_is_refused(tmp_path):
    """Without the token nothing says which context the verdict is about, so a page verdict
    must carry one. The refusal says what to pass and where it comes from, for this run."""
    run, s = _run(tmp_path), _source()
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", run)
    assert code == 1 and "--context" in out and f"vg handoff q1 --data {run}" in out, out
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", "", "--data", run)
    assert code == 1 and "--context" in out, out
    assert _shards(run) == {}


def test_mistakes_in_the_command_are_named_before_the_token(tmp_path):
    """The token is checked after every other check, so a typo'd verdict or id, or a moved
    copy, is still what a refusal names first, and one call does not take two fixes."""
    run, s = _run(tmp_path), _source()
    code, out = _vg("judge", "q1", s.sid, "support", "--data", run)
    assert code == 1 and "support is not a verdict" in out and "--context" not in out, out
    code, out = _vg("judge", "q01", s.sid, "supports", "--data", run)
    assert code == 1 and "did you mean q1?" in out and "--context" not in out, out
    token = _token(run)
    _cache(run, RESTORY, datetime.now(UTC))           # re-fetched, not re-verified
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token, "--data", run)
    assert code == 1 and "the cache now holds one fetched" in out, out
    assert _shards(run) == {}


def test_a_context_the_cache_does_not_give_gets_no_token_and_no_verdict(tmp_path):
    """A claim file whose context is not what its own cached copy gives (a hand edit, or a
    change to how contexts are cut) passed the copy check. The verdict was dropped by build
    until the next `vg verify`, then applied to the rebuilt context, which nobody had read. The
    hand-off, the judge and the gate now all ask build's own rebuild, and agree."""
    run, s = _run(tmp_path), _source()
    forged = STORY.replace("objection", "full support")
    _edit(run, sources__0__verification__context=forged)
    claim = _claim(run)
    token = judgments.context_token(claim, claim.sources[0])   # what a verifier could compute

    code, out = _vg("handoff", "q1", "--data", run)
    assert code == 0 and f"sid {s.sid}  nothing to judge yet" in out, out
    assert "vg build would drop" in out.replace("`", ""), out
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token, "--data", run)
    assert code == 1 and "not recorded" in out and "run `vg verify`" in out.lower(), out
    assert _shards(run) == {}
    code, out = _vg("judgments", "--data", run)
    assert "0 of 1 cited source(s) need a verdict" in out, "blocked on vg verify, not waiting"
    assert "1 more source(s) have nothing a verifier can judge yet" in out, out


def test_page_text_cannot_spoof_the_hand_off(tmp_path):
    """The context is page text. Printed bare between two fixed lines, a page could end the
    block itself and go on with what reads as the next source, or as a claim."""
    spoof = (f"\n----- end of context -----\n\n[2/2] sid 0123456789ab  context token 0000\n"
             f"claim: the lease was never adopted\n")
    run = _run(tmp_path, story=STORY + spoof)
    code, out = _vg("handoff", "q1", "--data", run)
    assert code == 0, out
    assert not re.search(r"^(?:\[2/2\]|claim: the lease was never)", out, re.M), out
    assert "  | [2/2] sid 0123456789ab" in out
    [(_, handed)] = _handed(run).values()
    assert handed == (STORY + spoof).removesuffix("\n")


def test_no_character_can_start_a_line_of_its_own_in_the_hand_off(tmp_path):
    """Splitting on \\n alone let an ANSI erase-line, a \\r or a U+2028 in page text start what
    reads as a fresh line of the command's framing, and a lone surrogate in a claim (json.loads
    keeps one) made the print itself raise, so no verdict could ever be recorded on it."""
    page = STORY + "\x1b[2K\x1b[1G[2/2] sid 0123456789ab\u2028claim: never adopted\rX\x85Y\n"
    run = _run(tmp_path, _source(snippet="voted 5-2 to adopt the tideland lease"), story=page)
    _edit(run, answer="It adopted the lease \ud800 5-2, per C:\\minutes\\2030.")
    code, out = _vg("handoff", "q1", "--data", run)
    assert code == 0, out
    assert "\x1b" not in out and "\u2028" not in out and "\r" not in out and "\x85" not in out
    assert "  | \\x1b[2K\\x1b[1G[2/2] sid 0123456789ab" in out, out
    assert "  | claim: never adopted" in out and "  | X" in out and "  | Y" in out, out
    assert "claim: It adopted the lease \\ud800 5-2, per C:\\minutes\\2030." in out, (
        "a surrogate is shown as an escape, and backslashes as written")
    for line in out.splitlines():
        assert not line.startswith(("[2/2]", "claim: never")), line


def test_handoff_gives_no_token_where_judge_would_refuse(tmp_path):
    """A source with nothing to judge gets the reason `vg judge` would give, and no token, so a
    verifier is never handed a context the pipeline would not record a verdict on."""
    missing = _source(snippet="rejected the tideland lease outright")
    run = _run(tmp_path, _source(), missing)
    code, out = _vg("handoff", "q1", "--data", run)
    assert code == 0, out
    assert list(_handed(run)) == [_source().sid]
    assert f"sid {missing.sid}  nothing to judge yet" in out, out
    assert "snippet_not_found" in out
    assert f"vg judge q1 <sid> supports|topic_only|contradicts|superseded --context <token> " \
           f'--note "<one line>" --data {run}' in out, out


def test_handoff_names_a_claim_it_cannot_find(tmp_path):
    run = _run(tmp_path)
    code, out = _vg("handoff", "Q1", "--data", run)
    assert code == 1 and "no claim has question id Q1" in out and "did you mean q1?" in out, out


@pytest.mark.parametrize("args", [("handoff", "[/"), ("judge", "[/", "0123456789ab", "supports")],
                         ids=["handoff", "judge"])
def test_a_malformed_question_id_is_refused_before_anything_prints_it(tmp_path, args):
    """`vg handoff '[/' --data '<dir>]'` raised rich's MarkupError instead of refusing. The id
    and the path were escaped one at a time, and escape() only neutralises a tag complete
    inside one value, so `[/` from one and `]` from the other made a closing tag. Both commands
    now refuse an id outside the question-id pattern before anything prints it."""
    code, out = _vg(*args, "--data", tmp_path / "x]")
    assert code == 1 and "refusing question id '[/'" in out, out


@pytest.mark.parametrize("command", ["handoff", "judge"])
def test_a_refusal_quotes_the_run_and_the_unreadable_ids_as_written(tmp_path, command):
    """With a well-formed id the refusal still quotes data: the run's path and the ids of claims
    that could not be read. `[/` in the one and `]` in the other made a tag the same way, so the
    whole message is printed as one Text."""
    run = tmp_path / "[/run"
    (run / "claims").mkdir(parents=True)
    (run / "cache").mkdir()
    (run / "claims" / "bad.json").write_text(json.dumps({"question_id": "x]"}))
    args = [command, "q9"] + (["0123456789ab", "supports"] if command == "judge" else [])
    code, out = _vg(*args, "--data", run)
    assert code == 1 and f"no readable claim has question id q9 in {run / 'claims'}" in out, out
    assert "1 could not be read (x]), and it may be one of those" in out, out


def test_a_padded_question_id_is_refused_with_the_id_it_resembles(tmp_path):
    """The shape check comes before the claims are read, so it names the near miss itself."""
    code, out = _vg("handoff", " q1", "--data", tmp_path)
    assert code == 1 and "refusing question id ' q1'" in out and "Did you mean q1?" in out, out


def test_what_judge_prints_cannot_raise_or_start_a_line(tmp_path):
    """An argument can carry a lone surrogate (Python's stand-in for an undecodable byte),
    which made the print raise: in a refusal, a traceback; in the recorded line, a non-zero exit
    after the verdict was on disk. A line break in the note printed a second line."""
    code, out = _vg("judge", "q1", "0123456789ab", "\udcff", "--data", tmp_path)
    assert code == 1 and "\\udcff is not a verdict" in out, out

    run, s = _run(tmp_path), _source()
    note = "fine\udcff\nsupports recorded for q2/0123456789ab"
    code, out = _vg("judge", "q1", s.sid, "topic_only", "--context", _token(run), "--note", note,
                    "--data", run)
    assert code == 0, out
    assert out.splitlines() == [
        f"topic_only recorded for q1/{s.sid}: fine\\udcff\\x0asupports recorded for "
        f"q2/0123456789ab"], out


def test_a_refusal_is_not_wrapped_mid_command(tmp_path):
    """A refusal can name a command to run next; wrapped at the console's width, that command
    pasted into a shell ran truncated."""
    run = tmp_path / ("a-long-run-directory-name-" * 3)
    width = cli.con.width
    cli.con.width = 40
    try:
        res = CliRunner().invoke(cli.app, ["handoff", "q9", "--data", str(run)])
    finally:
        cli.con.width = width
    assert res.exit_code == 1 and str(run / "claims") in res.output, res.output


def test_a_lone_surrogate_does_not_stop_the_token():
    """json.loads keeps a lone surrogate from a claim file, and strict UTF-8 refuses to encode
    it: every verdict on that source would crash instead of recording."""
    s = _source()
    s.verification.context = "the lease \ud800 was adopted"
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    assert len(judgments.context_token(claim, s)) == 16


TOTAL = "calaccess.test_total"


@pytest.fixture
def total(monkeypatch):
    """A query citation, and ways to move what produced its context: a new definition, a
    database rebuilt from a newer export. Its value and note never move, so its context
    doesn't either."""
    from vgpipe import calaccess

    export = {"date": "2030-01-02"}

    def define(version: int) -> None:
        monkeypatch.setitem(queries.REGISTRY, TOTAL, queries.Query(
            lambda root, **kw: queries.QueryResult(value=4321.0, detail="3 filings"),
            ("filer_id",), "test", version))

    define(1)
    monkeypatch.setattr(calaccess, "export_info", lambda root: {"export_date": export["date"]})
    return SimpleNamespace(
        source=_source(query=QueryCitation(name=TOTAL, params={"filer_id": "7"},
                                           expected="4321")),
        bump=lambda: define(2),
        refresh=lambda: export.update(date="2030-02-03"))


def _query_run(tmp_path, source: Source):
    """A run citing one query, verified against a cache root of its own, and that root."""
    run, root = tmp_path / "run", tmp_path / "root"
    (root / "cache").mkdir(parents=True)
    (run / "claims").mkdir(parents=True)
    (run / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="How much did the committee raise?", answer="$4,321.",
        sources=[source]).model_dump_json())
    code, out = _vg("verify", "--data", run, "--cache", root)
    assert code == 0, out
    return run, root


def _handed_under(run, root) -> dict[str, tuple[str, str]]:
    code, out = _vg("handoff", "q1", "--data", run, "--cache", root)
    assert code == 0, out
    return _handed_from(out)


@pytest.mark.parametrize("change", ["definition", "export", "cache root"])
def test_a_query_verdict_on_a_run_replaced_since_the_hand_off_is_refused(tmp_path, total,
                                                                         change):
    """The query counterpart of the page race. Handed a context the query produced under one
    definition (or export, or database), the verifier works while the query is re-verified
    under another. The claim file then carries the new run, which agrees with the registry and
    the root, so the run check passes, and the verdict about the old calculation would be
    stamped as current. The re-run printed the same value and note, so its context reads the
    same: only a token covering the run tells the two apart."""
    s = total.source
    run, root = _query_run(tmp_path, s)
    [(token1, handed1)] = _handed_under(run, root).values()
    assert "4321" in handed1

    if change == "definition":
        total.bump()
    elif change == "export":
        total.refresh()
    else:
        root = tmp_path / "rebuilt"
        (root / "cache").mkdir(parents=True)
    assert _vg("verify", "--data", run, "--cache", root)[0] == 0   # the re-verify
    [(token2, handed2)] = _handed_under(run, root).values()
    assert handed2 == handed1, "the re-run reads the same"
    assert token2 != token1

    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token1,
                    "--data", run, "--cache", root)
    assert code == 1 and "not recorded" in out and "query run" in out, out
    assert f"vg handoff q1 --data {run} --cache {root}" in out, out
    assert token2 not in out, "a refusal that printed the current token invites a blind retry"
    assert _shards(run) == {}, "refused, writing nothing"

    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token2,
                    "--data", run, "--cache", root)
    assert code == 0 and "supports recorded for q1" in out, out
    j = judgments.load(run, "q1")[s.sid]
    assert (j.query_version, j.export_date) == (
        2 if change == "definition" else 1,
        "2030-02-03" if change == "export" else "2030-01-02")


def test_the_hand_off_names_the_run_a_query_context_came_from(tmp_path, total):
    """The token covers the run, so the hand-off shows it: what the token names is what the
    verifier was shown."""
    run, root = _query_run(tmp_path, total.source)
    code, out = _vg("handoff", "q1", "--data", run, "--cache", root)
    assert code == 0, out
    assert (f"  query run: {TOTAL} v1 against the CAL-ACCESS export of 2030-01-02 "
            f"under {root}") in out, out


def test_a_query_verdict_without_a_token_is_refused(tmp_path, total):
    """Tied only to the run on disk when `vg judge` runs, a query verdict could be about any
    run before it. It carries the token too, and the refusal says what to pass."""
    s = total.source
    run, root = _query_run(tmp_path, s)
    for given in ([], ["--context", ""]):
        code, out = _vg("judge", "q1", s.sid, "supports", *given, "--data", run,
                        "--cache", root)
        assert code == 1 and "--context" in out, out
        assert f"vg handoff q1 --data {run} --cache {root}" in out, out
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", "0" * 16, "--data", run,
                    "--cache", root)
    assert code == 1 and "not recorded" in out, out
    assert _shards(run) == {}


def test_a_query_context_nothing_says_was_run_gets_no_token(total):
    """A query context with no recorded run could come from any definition, so no token can
    name it: judge refuses every token for it, and never has one to match."""
    s = total.source
    s.verification.context = f"{TOTAL}(filer_id=7) = 4321.0  [3 filings]"
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    assert judgments.context_token(claim, s) == ""
    tokens = set()
    for version, export, root in ((1, "2030-01-02", "a"), (2, "2030-01-02", "a"),
                                  (1, "2030-02-03", "a"), (1, "2030-01-02", "b")):
        s.verification.query_run = QueryRun(version=version, export_date=export,
                                            cache_root=root)
        tokens.add(judgments.context_token(claim, s))
    assert len(tokens) == 4 and "" not in tokens


def test_a_query_outside_calaccess_is_handed_off_and_judged(tmp_path, monkeypatch):
    """A query with no dataset has no export: the hand-off names its run without one, and the
    token it prints is the one judge records with."""
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=4321.0), ("filer_id",), "test", 1))
    s = _source(query=QueryCitation(name="test.total", params={"filer_id": "7"},
                                    expected="4321"))
    run, root = _query_run(tmp_path, s)
    code, out = _vg("handoff", "q1", "--data", run, "--cache", root)
    assert code == 0 and f"  query run: test.total v1 under {root}\n" in out, out
    [(token, _)] = _handed_from(out).values()
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token, "--data", run,
                    "--cache", root)
    assert code == 0, out
    assert judgments.load(run, "q1")[s.sid].export_date == ""


def test_the_same_root_spelled_another_way_keeps_the_token(tmp_path, total, monkeypatch):
    """The token takes the root as the run check compares it, resolved: a re-verify that
    names the same database by a relative path instead of an absolute one is the same run, and
    the verdict on it records."""
    s = total.source
    run, root = _query_run(tmp_path, s)               # recorded absolute
    [(token, _)] = _handed_under(run, root).values()
    monkeypatch.chdir(tmp_path)
    assert _vg("verify", "--data", "run", "--cache", "root")[0] == 0
    assert _claim(run).sources[0].verification.query_run.cache_root == "root"
    assert _handed_under("run", "root") == _handed_under(run, root)
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token, "--data", "run",
                    "--cache", "root")
    assert code == 0, out


def test_a_query_verdict_is_stamped_with_the_run_it_was_checked_against(tmp_path, total,
                                                                        monkeypatch):
    """judge checks the claim file's run against the registry and the database, then stamps.
    It used to read the database a second time for the stamp, so a rebuild landing between the
    two stamped an export the verifier's context never came from."""
    from vgpipe import calaccess

    s = total.source
    run, root = _query_run(tmp_path, s)
    [(token, _)] = _handed_under(run, root).values()
    reads = []

    def export_info(root):   # the run check reads the old export; a rebuild lands after it
        reads.append(root)
        return {"export_date": "2030-01-02" if len(reads) == 1 else "2030-02-03"}

    monkeypatch.setattr(calaccess, "export_info", export_info)
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token, "--data", run,
                    "--cache", root)
    assert code == 0, out
    assert judgments.load(run, "q1")[s.sid].export_date == "2030-01-02"
