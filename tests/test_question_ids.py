"""Question ids are stable and never reused, and `vg build` and `vg status` are the rule's gate:
each claim is checked against the question the run's questions.json holds for its id. A claim
on an id the set no longer lists, or answering another question than its id names, fails
both commands, and build renders nothing. A `maps_from` nothing will ever apply is reported."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from vgpipe import cli
from vgpipe.models import Claim

VOTE = "How did the member vote on the harbor levy?"
FUNDS = "Who are the largest donors to the member's campaign?"


def _vg(*args):
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _run(root, questions, claims):
    """A run at `root`: its question set (a list, or raw text for a malformed one; None for
    none) and one claim file per (id, question) pair. No sources: the gate reads only the id
    and the question, and build renders a claim without them."""
    (root / "claims").mkdir(parents=True)
    if questions is not None:
        text = questions if isinstance(questions, str) else json.dumps(questions)
        (root / "questions.json").write_text(text)
    for qid, question in claims:
        (root / "claims" / f"{qid}.json").write_text(
            Claim(question_id=qid, question=question, answer="a").model_dump_json())
    return root


def _rendered(run) -> bool:
    return (run / "out" / "review.html").exists()


def test_a_run_whose_claims_answer_their_ids_questions_builds(tmp_path):
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}],
               [("q1", VOTE), ("q2", FUNDS)])
    code, out = _vg("build", "--data", run)
    assert code == 0, out
    assert _rendered(run)
    assert "questions.json" not in out, out
    code, out = _vg("status", "--data", run)
    assert code == 0, out


def test_a_claim_on_an_id_the_question_set_does_not_list_fails(tmp_path):
    """A retired id's claim left in claims/ rendered beside its replacement, answering a
    question the run no longer asks, and every command exited 0."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q3", "text": FUNDS}],
               [("q1", VOTE), ("q2", FUNDS), ("q3", FUNDS)])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert f"1 claim(s) sit on an id {run / 'questions.json'} does not list: q2." in out, out
    assert "claims-archive/" in out and "derives_from" in out, out
    assert "review app not rendered" in out and not _rendered(run), out

    code, out = _vg("status", "--data", run)
    assert code == 1, out
    assert "does not list: q2." in out, out


def test_a_claim_answering_another_question_than_its_id_names_fails(tmp_path):
    """A question reworded or replaced at its id: the claim researched for the old one sits
    under the new one, and the shard's verdicts with it."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}],
               [("q1", VOTE), ("q2", "Which committees spent against the member?")])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert "1 claim(s) answer another question than" in out, out
    assert ("q2: the claim answers 'Which committees spent against the member?' "
            f"questions.json asks \"{FUNDS}\"") in out, out
    assert "give the new question a new id" in out, out
    assert not _rendered(run)

    code, out = _vg("status", "--data", run)
    assert code == 1 and "answer another question than" in out, out


def test_whitespace_alone_is_not_another_question(tmp_path):
    run = _run(tmp_path / "data", [{"id": "q1", "text": "How did the member\nvote on the levy? "}],
               [("q1", "How did the  member vote on the levy?")])
    code, out = _vg("build", "--data", run)
    assert code == 0 and _rendered(run), out


def test_any_other_difference_is_another_question(tmp_path):
    """A misquote and a rewording look the same on disk, so both fail, and the message offers
    the fix for each."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": "How did the member vote on the levy?"}],
               [("q1", "How did the member vote on the levy")])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert "copy the exact text into its `question`" in out, out


def test_a_pending_maps_from_is_reported_and_does_not_fail(tmp_path):
    """Nothing applies a `maps_from` now that `vg remap` is retired, so no claim moves: each
    is still checked against the question at the id it sits on, which is what fails."""
    run = _run(tmp_path / "data",
               [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS, "maps_from": "q5"},
                {"id": "q3", "text": "Who endorsed them?", "maps_from": ""}],
               [("q1", VOTE), ("q2", FUNDS)])
    code, out = _vg("build", "--data", run)
    assert code == 0 and _rendered(run), out
    assert "still declares maps_from (q2 from q5)" in out, out
    assert "never applied" in out and "Delete the key" in out, out
    code, out = _vg("status", "--data", run)
    assert code == 0 and "still declares maps_from (q2 from q5)" in out, out


def test_a_candidate_run_reads_its_own_question_set_before_the_data_roots(tmp_path):
    """`vg new-candidate` gives a run its own copy, retargeted to the candidate. Until a run
    declares its question set (#8), that copy wins, and a run without one reads the root's."""
    root = tmp_path / "data"
    root.mkdir()
    (root / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "How did Alex Placeholder vote on the levy?"}]))
    run = _run(root / "cand", [{"id": "q1", "text": "How did Sam Sample vote on the levy?"}],
               [("q1", "How did Sam Sample vote on the levy?")])
    code, out = _vg("build", "--data", run)
    assert code == 0, out

    (run / "questions.json").unlink()
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert f"another question than {root / 'questions.json'} asks" in out, out


def test_an_unreadable_own_question_set_does_not_fall_back_to_the_roots(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": VOTE}]))
    run = _run(root / "cand", "{", [("q1", VOTE)])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert f"unreadable question set {run / 'questions.json'}" in out, out


def test_no_question_set_is_said_and_does_not_fail(tmp_path):
    """Nothing to check against is not a pass: it is said, so silence never stands for a
    check that did not run."""
    run = _run(tmp_path / "data", None, [("q1", VOTE)])
    code, out = _vg("build", "--data", run)
    assert code == 0 and _rendered(run), out
    assert "no questions.json in" in out and "no claim was checked" in out, out


@pytest.mark.parametrize("text,expect", [
    pytest.param("[{", "unreadable question set", id="not-json"),
    pytest.param('{"q1": "?"}', "is not a list of questions", id="not-a-list"),
    pytest.param(json.dumps([{"id": "q1", "text": VOTE}, "q2", {"text": FUNDS},
                             {"id": "q4"}, {"id": "q1", "text": FUNDS}]),
                 "entry 1 is not an object; entry 2 has no id; entry 3 (q4) has no text; "
                 "entry 4 reuses id q1", id="bad-entries"),
])
def test_an_unreadable_question_set_fails_rather_than_reading_as_empty(tmp_path, text, expect):
    """Read as empty, every claim would sit on an unlisted id; read as missing, nothing would
    be checked. Either way the operator is told the wrong thing, so it fails, naming every
    problem at once. A reused id is one: a claim on it answers one of two questions."""
    run = _run(tmp_path / "data", text, [("q1", VOTE)])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert expect in out and str(run / "questions.json") in out, out
    assert not _rendered(run)
    code, out = _vg("status", "--data", run)
    assert code == 1 and expect in out, out


def test_status_with_no_claims_still_reads_the_question_set(tmp_path):
    """A migration nothing applies is worth settling before anyone researches on its ids."""
    run = _run(tmp_path / "data", [{"id": "q2", "text": FUNDS, "maps_from": "q1"}], [])
    code, out = _vg("status", "--data", run)
    assert code == 0 and "No claims yet." in out, out
    assert "still declares maps_from (q2 from q1)" in out, out

    (run / "questions.json").write_text("[")
    code, out = _vg("status", "--data", run)
    assert code == 1 and "unreadable question set" in out, out


def test_question_text_prints_as_text_not_markup(tmp_path):
    """Both questions are agent-authored, and the difference may be exactly what markup eats:
    a bracketed aside, an emoji code, or a closing tag that raises."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": "Did they vote [sic] :smile: yes?"}],
               [("q1", "Did they vote [/] :smile: no?")])
    code, out = _vg("build", "--data", run)
    assert code == 1, out
    assert "'Did they vote [/] :smile: no?'" in out, out
    assert "'Did they vote [sic] :smile: yes?'" in out, out
