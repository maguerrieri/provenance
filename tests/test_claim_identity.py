"""A verdict records which claim it judged (#30).

A verdict is keyed by sid, which covers the quote, not what the claim says about it. A retry
that rewrites the answer and keeps the quote kept the verdict too, about words the claim no
longer says. `provenance judge` now stamps each verdict with the judged claim's fingerprint, so that
can be seen (#74 reads it as stale). Every host and name here is synthetic.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance import judgments
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source

HOST = "https://ledger.example"


def src(slug: str) -> Source:
    return Source(url=f"{HOST}/{slug}", publisher="The Example Ledger", author="R. Writer",
                  date="2030-05-14", source_type="bylined_journalism",
                  snippet=f"the board approved appeal {slug} on Tuesday")


def claim(qid: str, *sources: Source, question: str = "", answer: str = "") -> Claim:
    return Claim(question_id=qid, question=question or f"What did the board decide ({qid})?",
                 answer=answer or f"It approved the appeal ({qid}).", sources=list(sources))


def test_a_claims_identity_is_its_question_and_answer():
    """It must change when a retry rewrites what the claim says, and not when only its sources
    change: a verdict judges one of them, and adding another must not lapse it."""
    c = claim("q1", src("a"))
    assert c.fingerprint == c.model_copy(update={"sources": [src("a"), src("b")]}).fingerprint
    assert c.fingerprint == c.model_copy(update={"question_id": "q7"}).fingerprint
    assert c.fingerprint != c.model_copy(update={"answer": "It denied the appeal."}).fingerprint
    assert c.fingerprint != c.model_copy(update={"question": "Who decided it?"}).fingerprint
    # exact text: a one-character edit can change what the claim says
    assert c.fingerprint != c.model_copy(update={"answer": c.answer + " "}).fingerprint
    # "Yes." to two different questions is two claims
    assert claim("q1", question="Did it approve A?", answer="Yes.").fingerprint != \
        claim("q2", question="Did it approve B?", answer="Yes.").fingerprint
    assert re.fullmatch(r"[0-9a-f]{12}", c.fingerprint)


def test_a_fingerprint_and_a_sid_are_made_one_way():
    """judgments checks both against one shape, so they share one recipe (`models.short_id`).
    The sid's input is unchanged, since changing it would lapse every recorded verdict."""
    from provenance.models import short_id

    s, c = src("a"), claim("q1")
    assert s.sid == short_id(s.url, s.snippet)
    assert c.fingerprint == short_id(json.dumps([c.question, c.answer]))


def test_moving_a_nul_between_question_and_answer_changes_the_fingerprint():
    """Both halves are free agent-written text. NUL-joined, ("Q", "A\\0B") and ("Q\\0A", "B")
    hashed alike, so a rewrite of both at once could keep the stamp."""
    one = claim("q1", question="Q", answer="A\x00B")
    assert one.fingerprint != one.model_copy(
        update={"question": "Q\x00A", "answer": "B"}).fingerprint


# --- recording ----------------------------------------------------------------------------


def _verified_run(root: Path, *claims: Claim) -> Path:
    """A run whose claims `provenance verify` has checked offline against cached pages, so `provenance judge`
    records verdicts on them."""
    from typer.testing import CliRunner

    from provenance import cli
    from provenance.fetch import cache_path

    (root / "claims").mkdir(parents=True)
    (root / "questions.json").write_text(json.dumps(
        [{"id": c.question_id, "text": c.question, "claim_type": "mechanical"} for c in claims]))
    for c in claims:
        (root / "claims" / f"{c.question_id}.json").write_text(c.model_dump_json())
        for s in c.sources:
            cache_path(root, s.url).write_text(PageCache(
                url=s.url, final_url=s.url, status=200, content_type="text/html", title="T",
                text=f"Minutes. In a 4-1 vote, {s.snippet}, after a long hearing.",
                fetched_at=datetime.now(UTC) - timedelta(hours=1),
                extractor_version=EXTRACTOR_VERSION).model_dump_json())
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(root)])
    assert res.exit_code == 0, res.output
    return root


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


def _handed(data, qid: str, sid: str) -> list[str]:
    """`--context` and the token `provenance handoff` prints beside `sid`, as a verifier passes it on."""
    code, out = _provenance("handoff", qid, "--data", data)
    assert code == 0, out
    token = re.search(rf"sid {re.escape(sid)}\s+context token (\w+)", out)
    assert token, out
    return ["--context", token.group(1)]


def _rewrite_answer(run: Path, qid: str, answer: str) -> None:
    """A retry that rewrites the answer and keeps every source, quote and all."""
    p = run / "claims" / f"{qid}.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "answer": answer}))


def test_judge_stamps_the_claim_it_judged(tmp_path):
    shared = src("shared")
    run = _verified_run(tmp_path / "run", c := claim("q1", shared))
    code, out = _provenance("judge", "q1", shared.sid, "supports", "--data", run,
                    *_handed(run, "q1", shared.sid))
    assert code == 0, out
    assert judgments.load(run, "q1")[shared.sid].claim_fingerprint == c.fingerprint
    entry = json.loads(judgments.path_for(run, "q1").read_text())[0]
    assert entry["claim_fingerprint"] == c.fingerprint


def test_a_verdict_shows_that_a_retry_rewrote_its_claim(tmp_path):
    """The repro #74 builds on: the answer flips, the quote stays, so the sid and the verdict
    stay. The stamp is what no longer matches."""
    from provenance import cli

    s = src("a")
    run = _verified_run(tmp_path / "run", claim("q1", s, answer="It approved the appeal."))
    assert _provenance("judge", "q1", s.sid, "supports", "--data", run,
               *_handed(run, "q1", s.sid))[0] == 0
    _rewrite_answer(run, "q1", "It denied the appeal.")

    (now,) = cli.load_claims(run / "claims")
    j = judgments.load(run, "q1")[s.sid]
    assert now.sources[0].sid == s.sid, "the retry kept the quote, so the verdict still matches"
    assert j.claim_fingerprint and j.claim_fingerprint != now.fingerprint


def test_judging_again_after_a_retry_stamps_what_the_claim_says_now(tmp_path):
    from provenance import cli

    s = src("a")
    run = _verified_run(tmp_path / "run", claim("q1", s, answer="It approved the appeal."))
    assert _provenance("judge", "q1", s.sid, "supports", "--data", run,
               *_handed(run, "q1", s.sid))[0] == 0
    _rewrite_answer(run, "q1", "It denied the appeal.")

    code, out = _provenance("judge", "q1", s.sid, "topic_only", "--data", run,
                    *_handed(run, "q1", s.sid))
    assert code == 0, out
    (now,) = cli.load_claims(run / "claims")
    j = judgments.load(run, "q1")[s.sid]
    assert (j.verdict, j.claim_fingerprint) == ("topic_only", now.fingerprint)


def test_a_verdict_without_a_fingerprint_is_written_as_older_code_reads_it(tmp_path):
    """load() refuses an unknown key, so a shard of verdicts recorded without a stamp must stay
    readable by a checkout from before this: the key is written only when set."""
    s = src("a")
    judgments.record(tmp_path, "q1", s.sid, "supports", "recorded before stamping")
    (entry,) = json.loads(judgments.path_for(tmp_path, "q1").read_text())
    assert "claim_fingerprint" not in entry
    assert judgments.load(tmp_path, "q1")[s.sid].claim_fingerprint == ""


@pytest.mark.parametrize("bad", ["not-hex-at-all", "ABCDEF123456", "0123456789abc", 7])
def test_a_malformed_fingerprint_is_refused_on_read_and_on_write(tmp_path, bad):
    """Read as absent, a malformed stamp would pass for a verdict from before stamping; read as a
    fingerprint, it never matches its claim. Neither is what it says, so load() refuses it like
    any other unreadable entry, and record() will not write one."""
    s = src("a")
    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir()
    p.write_text(json.dumps([{"sid": s.sid, "verdict": "supports", "claim_fingerprint": bad}]))
    with pytest.raises(judgments.UnreadableJudgments, match="claim_fingerprint"):
        judgments.load(tmp_path, "q1")
    p.unlink()
    with pytest.raises(ValueError, match="claim_fingerprint"):
        judgments.record(tmp_path, "q1", s.sid, "supports", claim_fingerprint=bad)
    assert not p.exists()
