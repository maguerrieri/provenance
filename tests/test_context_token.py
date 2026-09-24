"""A verdict names what the verifier was handed, and `vg judge` refuses any other.

`vg judge` checks the claim file as it is when judge runs. It already refuses when the copy of
the page `vg verify` built the context from is no longer the one cached. What that check
passes is a re-verify that lands while a verifier works: another run re-fetches the page, this
one rebuilds the context from the new copy, and the claim file names a copy that is cached and
a context no verifier has read. The verdict used to be recorded, and the row rendered green on
that context. `vg handoff` now prints the claim and each source's context with a token, `vg
judge --context` hands the token back, and a token for anything else writes nothing.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from vgpipe import cli, judgments, queries
from vgpipe.fetch import cache_path
from vgpipe.models import EXTRACTOR_VERSION, Claim, PageCache, QueryCitation, Source

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


def test_a_verdict_on_a_claim_rewritten_since_the_hand_off_is_refused(tmp_path):
    """A retry that rewrites only the answer keeps the sid (url and snippet) and the context,
    so the verdict would carry over to an answer no verifier read. The token covers the claim."""
    run, s = _run(tmp_path), _source()
    [(token1, _)] = _handed(run).values()
    _edit(run, answer="It adopted the lease unanimously.")
    code, out = _vg("judge", "q1", s.sid, "supports", "--context", token1, "--data", run)
    assert code == 1 and "different claim or context" in out, out
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


def test_a_lone_surrogate_does_not_stop_the_token():
    """json.loads keeps a lone surrogate from a claim file, and strict UTF-8 refuses to encode
    it: every verdict on that source would crash instead of recording."""
    s = _source()
    s.verification.context = "the lease \ud800 was adopted"
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    assert len(judgments.context_token(claim, s)) == 16


@pytest.fixture
def total(monkeypatch):
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=4321.0), ("filer_id",), "test", 1))
    return _source(query=QueryCitation(name="test.total", params={"filer_id": "7"},
                                       expected="4321"))


def test_a_query_verdict_needs_no_token_but_a_wrong_one_is_refused(tmp_path, total):
    """A query citation is already tied to its run (`unjudgeable_query()`), so it needs no
    token. One that is given is still checked: it can only refuse."""
    (tmp_path / "cache").mkdir()
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a", sources=[total]).model_dump_json())
    assert _vg("verify", "--data", tmp_path)[0] == 0
    [(token, _)] = _handed(tmp_path).values()

    code, out = _vg("judge", "q1", total.sid, "supports", "--context", "0" * 16,
                    "--data", tmp_path)
    assert code == 1 and "not recorded" in out, out
    assert _shards(tmp_path) == {}
    for given in ("", token):
        code, out = _vg("judge", "q1", total.sid, "supports", "--context", given,
                        "--data", tmp_path)
        assert code == 0, out
