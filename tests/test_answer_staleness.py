"""A verdict applies only to the question and answer it judged (#74).

A verdict says whether a page supports one claim's answer, but it was keyed by source id alone
(url + snippet). A retry that rewrote the answer and kept the source kept the verdict too: a
`supports` about one answer rendered green on the opposite answer, and a `contradicts` held a
corrected claim in review. Each verdict now names the claim it judged (`claim_fingerprint`,
#30), and one naming another question or answer, or none, is stale. Every host and name here
is synthetic.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import write_project

from provenance import judgments
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source

# A listed news host: reporting on an unlisted one reads as an unlisted outlet, which a
# claim can cite only as what the outlet reports (sources.tier()).
HOST = "https://calmatters.org"
QUESTION = "How did the member for District 9 vote on Measure Q-7?"
FOR, AGAINST = "They voted for it.", "They voted against it."


def src(slug: str = "q-7") -> Source:
    return Source(url=f"{HOST}/{slug}", publisher="The Example Gazette", author="R. Writer",
                  date="2030-05-14", source_type="bylined_journalism",
                  snippet=f"the member for District 9 voted yes on {slug}")


def claim(qid: str, *sources: Source, answer: str = FOR, question: str = QUESTION) -> Claim:
    return Claim(question_id=qid, question=question, answer=answer,
                 sources=list(sources) or [src()])


def _provenance(*args) -> tuple[int, str]:
    from typer.testing import CliRunner

    from provenance import cli

    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _write(run: Path, *claims: Claim) -> None:
    """Write claim files as a researcher does: research fields only, `provenance verify` next."""
    for c in claims:
        (run / "claims" / f"{c.question_id}.json").write_text(c.model_dump_json())


def _verified_run(run: Path, *claims: Claim) -> Path:
    """A run whose claims `provenance verify` has checked offline against cached pages."""
    from provenance.fetch import cache_path

    write_project(run)
    (run / "claims").mkdir(parents=True)
    (run / "questions.json").write_text(json.dumps(
        [{"id": c.question_id, "text": c.question, "claim_type": "mechanical"} for c in claims]))
    for c in claims:
        for s in c.sources:
            cache_path(run, s.url).write_text(PageCache(
                url=s.url, final_url=s.url, status=200, content_type="text/html", title="T",
                text=f"Minutes. In a 4-1 vote, {s.snippet}, after a long hearing.",
                fetched_at=datetime.now(UTC) - timedelta(hours=1),
                extractor_version=EXTRACTOR_VERSION).model_dump_json())
    _write(run, *claims)
    _verify(run)
    return run


def _verify(run: Path) -> str:
    code, out = _provenance("verify", "--data", run)
    assert code == 0, out
    return out


def _handed(data, qid: str, sid: str) -> list[str]:
    """`--context` and the token `provenance handoff` prints beside `sid`, as a verifier passes it on."""
    code, out = _provenance("handoff", qid, "--data", data)
    assert code == 0, out
    token = re.search(rf"sid {re.escape(sid)}\s+context token (\w+)", out)
    assert token, out
    return ["--context", token.group(1)]


def _judge(run: Path, qid: str, s: Source, verdict: str, note: str = "") -> None:
    code, out = _provenance("judge", qid, s.sid, verdict, "--data", run, "--note", note or verdict,
                            *_handed(run, qid, s.sid))
    assert code == 0, out


def _built(run: Path) -> dict[str, dict]:
    """{question id: claim} as `provenance build` writes it out, status included."""
    code, out = _provenance("build", "--data", run)
    assert code == 0, out
    return {c["question_id"]: c for c in json.loads((run / "out" / "claims.json").read_text())}


def _gate(run: Path) -> tuple[int, int, int, str]:
    """(exit code, need a verdict, stale, output) of `provenance judgments`."""
    code, out = _provenance("judgments", "--data", run)
    m = re.search(r"(\d+) of \d+ cited source\(s\) need a verdict \((\d+) stale\)", out)
    assert m, out
    return code, int(m.group(1)), int(m.group(2)), out


def test_a_supports_about_one_answer_does_not_vouch_for_the_opposite_one(tmp_path):
    """The issue's repro. q1 says the member voted for the measure, and its page was judged
    `supports`. A retry rewrites the answer to "voted against", keeping the url and snippet,
    so the sid and the verdict survived: q1 rendered verified on a verdict about the opposite
    answer."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports", "the minutes record a yes vote")
    assert _built(run)["q1"]["status"] == "verified"

    _write(run, claim("q1", src(), answer=AGAINST))          # the retry
    _verify(run)
    built = _built(run)["q1"]
    assert built["status"] == "pending"
    assert built["sources"][0]["verification"]["support"] == "unreviewed"
    assert built["sources"][0]["verification"]["support_note"] is None

    # the gate lists it for re-judging, so the judgment pass is not done
    code, need, stale, out = _gate(run)
    assert (code, need, stale) == (1, 1, 1), out
    assert "judged another question or answer than its claim gives now" in out, out

    # and a verifier's verdict on the answer as it reads now is what renders
    _judge(run, "q1", s, "contradicts", "the minutes record a yes vote")
    assert _built(run)["q1"]["status"] == "human_review"
    assert _gate(run)[:3] == (0, 0, 0)


def test_a_corrected_answer_lifts_an_old_contradicts(tmp_path):
    """The other direction. A `contradicts` sends its claim to review, and once a retry
    corrected the answer so the page agrees, the old verdict (with its note, about an answer
    that no longer exists) kept it there until someone thought to re-judge it."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s, answer=AGAINST))
    _judge(run, "q1", s, "contradicts", "the minutes record a yes vote, not a no")
    assert _built(run)["q1"]["status"] == "human_review"

    _write(run, claim("q1", src(), answer=FOR))              # the correction
    _verify(run)
    built = _built(run)["q1"]
    assert built["status"] == "pending", "waiting on a verdict about the corrected answer"
    assert built["sources"][0]["verification"]["support"] == "unreviewed"
    assert built["sources"][0]["verification"]["support_note"] is None
    assert built["conflicts"] == []
    assert _gate(run)[1:3] == (1, 1)

    _judge(run, "q1", s, "supports")
    assert _built(run)["q1"]["status"] == "verified"


def test_rewording_the_question_makes_a_verdict_stale(tmp_path):
    """A verdict judges one answer to one question: "They voted for it." answers a different
    question once the measure it names changes. The id is reworded in place here, question set
    and claim together, which the question-id gate cannot see: the stamp is what catches it."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports")
    reworded = claim("q1", src(), question="How did the member for District 9 vote on Q-8?")
    (run / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": reworded.question, "claim_type": "mechanical"}]))
    _write(run, reworded)
    _verify(run)
    assert _built(run)["q1"]["status"] == "pending"


def test_a_verdict_recorded_before_verdicts_named_their_claim_is_stale(tmp_path):
    """Nothing shows which answer an unstamped verdict judged, and nothing bounds when an answer
    was written the way a page's fetch time bounds a legacy page verdict. So it fails toward
    re-checking, as an unversioned query verdict does, and a verifier can record over it."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports")
    shard = judgments.path_for(run, "q1")
    raw = json.loads(shard.read_text())
    del raw[0]["claim_fingerprint"]                          # as a verdict from before #30
    shard.write_text(json.dumps(raw))

    c = Claim.model_validate_json((run / "claims" / "q1.json").read_text())
    [(_s, j, why)] = judgments.verdicts_for(c, run, cache_root=run)
    assert j.verdict == "supports"
    assert "recorded before verdicts named the claim they judged" in why, why
    assert _built(run)["q1"]["status"] == "pending"
    code, need, stale, out = _gate(run)
    assert (code, need, stale) == (1, 1, 1), "waiting on a verifier, not blocked"

    _judge(run, "q1", s, "supports")
    assert judgments.load(run, "q1")[s.sid].claim_fingerprint == c.fingerprint
    assert _built(run)["q1"]["status"] == "verified"


def test_a_verdict_filed_under_another_claims_id_does_not_apply_there(tmp_path):
    """Two claims cite one page, and q2's verdict on it sits in q1's shard, left there by an
    older remap or a hand move. It judged q2's answer, so it says nothing about q1's, whatever
    the sid says."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s),
                        claim("q2", src(), question="Did the member for District 9 vote?",
                              answer="Yes."))
    q2 = Claim.model_validate_json((run / "claims" / "q2.json").read_text())
    judgments.record(run, "q1", s.sid, "supports", "judged for q2",
                     claim_fingerprint=q2.fingerprint)

    c = Claim.model_validate_json((run / "claims" / "q1.json").read_text())
    [(_s, _j, why)] = judgments.verdicts_for(c, run, cache_root=run)
    assert "filed under another claim's id" in why, why
    assert _built(run)["q1"]["status"] == "pending"
    # and the gate counts it as a verifier's to close, which `provenance judge` lets it do
    assert _gate(run)[1:3] == (2, 1)
    _judge(run, "q1", s, "supports")
    assert _built(run)["q1"]["status"] == "verified"


def test_verify_reports_a_verdict_about_another_answer_as_not_applied(tmp_path):
    """`provenance verify` is where a retry's claim is checked first, so it says which verdicts no
    longer apply and why, as it does for a page re-fetched since."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports")
    _write(run, claim("q1", src(), answer=AGAINST))
    out = _verify(run)
    assert "1 verdict(s) no longer describe what they judged" in out, out
    assert f"q1/{s.sid}: judged another question or answer than this claim gives now" in out, out


def test_a_verdict_stale_on_both_halves_names_both(tmp_path):
    """Re-judging a rewritten claim whose page was also re-fetched needs `provenance verify` first,
    or `provenance judge` refuses to stamp a copy the verifier did not read. Naming only the answer
    would hide that step."""
    from provenance.fetch import cache_path

    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports")
    page = PageCache.model_validate_json(cache_path(run, s.url).read_text())
    page.fetched_at = datetime.now(UTC)                     # re-fetched since the verdict
    cache_path(run, s.url).write_text(page.model_dump_json())

    c = claim("q1", src(), answer=AGAINST)
    [(_s, _j, why)] = judgments.verdicts_for(c, run, cache_root=run)
    assert "judged another question or answer" in why and "re-fetched since" in why, why


def test_a_stale_verdict_on_a_redrawn_context_waits_on_verify_not_a_verifier(tmp_path):
    """Revalidation redraws an excerpt that moved since `provenance verify`, and drops any verdict on
    the old one. The gate sent such a row to `provenance verify` only when its verdict had applied, so
    once a rewritten answer made the verdict stale, the row counted as a verifier's to close:
    `provenance judge` accepted a verdict on the claim file's outdated excerpt, and build dropped it."""
    from provenance.fetch import cache_path

    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports")
    p = run / "claims" / "q1.json"
    raw = json.loads(p.read_text())
    raw["answer"] = AGAINST                    # edited in place: the verification stays
    p.write_text(json.dumps(raw))
    page = PageCache.model_validate_json(cache_path(run, s.url).read_text())
    page.text = f"A different lead-in. {s.snippet}."     # same copy, fetch time unchanged
    cache_path(run, s.url).write_text(page.model_dump_json())

    code, need, stale, out = _gate(run)
    assert (code, need, stale) == (0, 0, 0), out       # not waiting on a verifier...
    assert "1 more source(s) have nothing a verifier can judge yet" in out, out
    assert "unreviewed (run provenance verify)" in out, out     # ...but on `provenance verify`


def test_an_unjudged_source_on_a_redrawn_context_waits_on_verify_not_a_verifier(tmp_path):
    """The same row with no verdict yet. It counted as a verifier's to close, and `provenance judge`
    stamped a verdict on the claim file's outdated excerpt that build then dropped."""
    from provenance.fetch import cache_path

    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    page = PageCache.model_validate_json(cache_path(run, s.url).read_text())
    page.text = f"A different lead-in. {s.snippet}."     # same copy, fetch time unchanged
    cache_path(run, s.url).write_text(page.model_dump_json())

    code, need, stale, out = _gate(run)
    assert (code, need, stale) == (0, 0, 0), out
    assert "1 more source(s) have nothing a verifier can judge yet" in out, out
    assert "unreviewed (run provenance verify) the context changed since provenance verify" in out, out

    _verify(run)                                            # redraws the claim file's excerpt
    code, need, stale, out = _gate(run)
    assert (code, need) == (1, 1), out                      # now a verifier's to close


def test_a_stale_row_shows_why_not_the_note_about_the_old_answer(tmp_path):
    """A verifier re-judging a stale row reads this table. The old note was about words the
    claim no longer says, so the row shows why the verdict is stale instead."""
    s = src()
    run = _verified_run(tmp_path / "run", claim("q1", s))
    _judge(run, "q1", s, "supports", "the minutes record a yes vote")
    _write(run, claim("q1", src(), answer=AGAINST))
    _verify(run)

    code, need, stale, out = _gate(run)
    assert (code, need, stale) == (1, 1, 1), out
    assert "stale (was supports) judged another question or answer" in out, out
    assert "the minutes record a yes vote" not in out, out
