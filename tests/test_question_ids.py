"""Question ids are stable and never reused, and `provenance build` and `provenance status` are the rule's gate:
each claim is checked against the question the run's questions.json holds for its id. A claim
on an id the set no longer lists, or answering another question than its id names, is left out
of the review app and fails both commands. A `maps_from` nothing will ever apply is reported.
`provenance check-claim` runs the same check on the one claim a researcher is handing on."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import write_project
from typer.testing import CliRunner

from provenance import cli
from provenance.fetch import cache_path
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source

VOTE = "How did the member vote on the harbor levy?"
FUNDS = "Who are the largest donors to the member's campaign?"


def _provenance(*args):
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _run(root, questions, claims, *, subject=False):
    """A run at `root`: its question set (a list, or raw text for a malformed one; None for
    none) and one claim file per (id, question) pair. No sources: the gate reads only the id
    and the question, and build renders a claim without them. The root of a project of its
    own, unless it is a `subject` of the one its parent's project file declares."""
    if not subject:
        write_project(root)
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


def _built(run) -> dict[str, dict]:
    """The claims the review app was rendered with, by id."""
    return {c["question_id"]: c for c in json.loads((run / "out" / "claims.json").read_text())}


def test_a_run_whose_claims_answer_their_ids_questions_builds(tmp_path):
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}],
               [("q1", VOTE), ("q2", FUNDS)])
    code, out = _provenance("build", "--data", run)
    assert code == 0, out
    assert set(_built(run)) == {"q1", "q2"}
    assert "questions.json" not in out and "left out" not in out, out
    code, out = _provenance("status", "--data", run)
    assert code == 0, out


def test_a_claim_on_an_id_the_question_set_does_not_list_is_left_out_and_fails(tmp_path):
    """A retired id's claim left in claims/ rendered beside its replacement, answering a
    question the run no longer asks, and every command exited 0. It is left out now, and the
    command fails; the rest still render, since one mis-filed claim must not cost the run
    every other one."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q3", "text": FUNDS}],
               [("q1", VOTE), ("q2", FUNDS), ("q3", FUNDS)])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert f"1 claim(s) sit on an id {run / 'questions.json'} does not list: q2." in out, out
    assert "claim from claims/ to claims-archive/" in out and "derives_from" in out, out
    assert "shard from judgments/ to judgments-archive/" in out, out
    assert out.endswith("1 claim(s) left out of the review app until they answer the question "
                        "their id names (above): q2"), out
    assert set(_built(run)) == {"q1", "q3"}

    code, out = _provenance("status", "--data", run)
    assert code == 1, out
    assert "does not list: q2." in out and "left out of this summary" in out, out
    table = out.split("does not list")[1]
    assert " q1 " in table and " q2 " not in table.split("left out")[0], out


def test_a_claim_answering_another_question_than_its_id_names_is_left_out_and_fails(tmp_path):
    """A question reworded or replaced at its id: the claim researched for the old one sits
    under the new one, and the shard's verdicts with it."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}],
               [("q1", VOTE), ("q2", "Which committees spent against the member?")])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert "1 claim(s) answer another question than" in out, out
    assert ("q2: the claim answers 'Which committees spent against the member?' "
            f"questions.json asks \"{FUNDS}\"") in out, out
    assert "give the new question a new id" in out, out
    assert set(_built(run)) == {"q1"}

    code, out = _provenance("status", "--data", run)
    assert code == 1 and "answer another question than" in out, out


def test_a_claim_deriving_from_one_left_out_reads_its_input_as_missing(tmp_path):
    run = _run(tmp_path / "data", [{"id": "q1", "text": VOTE}, {"id": "q3", "text": FUNDS}],
               [("q2", VOTE)])
    (run / "claims" / "q3.json").write_text(Claim(
        question_id="q3", question=FUNDS, answer="a", confidence="not_found",
        derives_from=["q2"]).model_dump_json())
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    built = _built(run)
    assert "q2" not in built
    assert built["q3"]["unmet_inputs"] and built["q3"]["status"] == "human_review", built


def test_an_id_differing_only_in_case_is_named(tmp_path):
    """The fix there is the claim's id, not archiving its research as a retired id's."""
    run = _run(tmp_path / "data", [{"id": "q3", "text": VOTE}], [("Q3", VOTE)])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert "does not list: Q3 (the set has q3, which differs only in case)." in out, out


def test_whitespace_alone_is_not_another_question(tmp_path):
    run = _run(tmp_path / "data", [{"id": "q1", "text": "How did the member\nvote on the levy? "}],
               [("q1", "How did the  member vote on the levy?")])
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _rendered(run), out


def test_typographic_drift_is_not_another_question(tmp_path):
    """Curly quotes and dashes, and case, are what copy-paste drifts on, and they print almost
    alike: the pipeline's own `normalize()` folds them, as it does for a snippet."""
    run = _run(tmp_path / "data",
               [{"id": "q1", "text": "What is the member’s record — on the levy?"}],
               [("q1", "what is the member's record -- on the levy?")])
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _rendered(run), out


def test_unicode_composition_alone_is_not_another_question(tmp_path):
    """"é" as one code point and as "e" plus a combining accent print identically, so a
    failure on it would be one nobody could see to fix."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": "How did Ren\u00e9 Sample vote?"}],
               [("q1", "How did Rene\u0301 Sample vote?")])
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _rendered(run), out


def test_any_other_difference_is_another_question(tmp_path):
    """A misquote and a rewording look the same on disk, so both fail, and the message offers
    the fix for each."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": "How did the member vote on the levy?"}],
               [("q1", "How did the member vote on the levy")])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert "copy the exact text into its `question`" in out, out


def test_a_pending_maps_from_is_reported_and_does_not_fail(tmp_path):
    """Nothing applies a `maps_from` now that `provenance remap` is retired, so no claim moves: each
    is still checked against the question at the id it sits on, which is what fails. An
    identity pair only adopted a rewording and never moved anything, so it is not reported."""
    run = _run(tmp_path / "data",
               [{"id": "q1", "text": VOTE, "maps_from": "q1"},
                {"id": "q2", "text": FUNDS, "maps_from": "q5"},
                {"id": "q3", "text": "Who endorsed them?", "maps_from": ""}],
               [("q1", VOTE), ("q2", FUNDS)])
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _rendered(run), out
    assert "still declares maps_from (q2 from q5), a migration for the retired `vg remap`" in out
    assert "Delete the key" in out and "q1 from q1" not in out, out
    code, out = _provenance("status", "--data", run)
    assert code == 0 and "still declares maps_from (q2 from q5)" in out, out


def test_an_unlisted_id_a_pending_maps_from_names_says_so(tmp_path):
    """The migration meant q5's research for q2. The advice is still to archive it, which keeps
    it, but the operator reads that advice knowing which question it answered."""
    run = _run(tmp_path / "data", [{"id": "q2", "text": FUNDS, "maps_from": "q5"}],
               [("q5", FUNDS), ("q7", VOTE)])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert "does not list: q5 (maps_from of q2), q7." in out, out


def test_a_subject_run_reads_its_own_question_set_and_never_the_projects(tmp_path):
    """`provenance new-candidate` gives a subject's run its own copy, retargeted to the subject.
    A run without one used to read the project root's, which is worded for another subject; now
    it says it has none, as a run with no set does."""
    root = write_project(tmp_path / "data", subjects=["cand"])
    (root / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "How did Alex Placeholder vote on the levy?"}]))
    run = _run(root / "cand", [{"id": "q1", "text": "How did Sam Sample vote on the levy?"}],
               [("q1", "How did Sam Sample vote on the levy?")], subject=True)
    code, out = _provenance("build", "--data", run)
    assert code == 0, out

    (run / "questions.json").unlink()
    code, out = _provenance("build", "--data", run)
    assert code == 0 and f"no questions.json in {run}, so no claim was checked" in out, out
    assert "another question than" not in out, out


def test_an_unreadable_own_question_set_does_not_fall_back_to_the_roots(tmp_path):
    """Falling back would check a candidate's claims against a template not retargeted to it,
    or pass them against the wrong set. A dangling symlink is unreadable too, though `exists()`
    reads it as absent."""
    root = write_project(tmp_path / "data", subjects=["cand"])
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": VOTE}]))
    run = _run(root / "cand", "{", [("q1", VOTE)], subject=True)
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert f"unreadable question set {run / 'questions.json'}" in out, out

    (run / "questions.json").unlink()
    (run / "questions.json").symlink_to(tmp_path / "moved.json")
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert f"unreadable question set {run / 'questions.json'}" in out, out


def test_no_question_set_is_said_and_does_not_fail(tmp_path):
    """Nothing to check against is not a pass: it is said, so silence never stands for a
    check that did not run."""
    run = _run(tmp_path / "data", None, [("q1", VOTE)])
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _rendered(run), out
    assert "no questions.json in" in out and "no claim was checked" in out, out


@pytest.mark.parametrize("text,expect", [
    pytest.param("[{", "unreadable question set", id="not-json"),
    pytest.param('{"q1": "?"}', "is not a list of questions", id="not-a-list"),
    pytest.param(json.dumps([{"id": "q1", "text": VOTE}, "q2", {"text": FUNDS},
                             {"id": "q4"}, {"id": "q1", "text": FUNDS},
                             {"id": "Q1", "text": "Who endorsed them?"},
                             {"id": " q6", "text": "Who endorsed them?"},
                             {"id": "q7", "text": " "}]),
                 "entry 1 is not an object; entry 2 has no id; entry 3 (q4) has no text; "
                 "entry 4 reuses id q1; entry 5 reuses id q1 as Q1; "
                 "entry 6 has id ' q6', which no claim can carry; entry 7 (q7) has no text",
                 id="bad-entries"),
])
def test_an_unreadable_question_set_fails_rather_than_reading_as_empty(tmp_path, text, expect):
    """Read as empty, every claim would sit on an unlisted id; read as missing, nothing would
    be checked. Either way the operator is told the wrong thing, so it fails, naming every
    problem at once, and renders nothing, since no claim could be checked. A reused id is one:
    a claim on it answers one of two questions. So are ids differing only in case, which share
    one claim file and one shard on macOS's default disk, and an id no claim can carry, whose
    claims would otherwise read as unlisted with nothing naming the entry."""
    run = _run(tmp_path / "data", text, [("q1", VOTE)])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert expect in out and str(run / "questions.json") in out, out
    assert "review app not rendered" in out and not _rendered(run), out
    code, out = _provenance("status", "--data", run)
    assert code == 1 and expect in out, out


def test_status_with_no_claims_still_reads_the_question_set(tmp_path):
    """A migration nothing applies is worth settling before anyone researches on its ids."""
    run = _run(tmp_path / "data", [{"id": "q2", "text": FUNDS, "maps_from": "q1"}], [])
    code, out = _provenance("status", "--data", run)
    assert code == 0 and "No claims yet." in out, out
    assert "still declares maps_from (q2 from q1)" in out, out

    (run / "questions.json").write_text("[")
    code, out = _provenance("status", "--data", run)
    assert code == 1 and "unreadable question set" in out, out


def test_question_text_prints_as_text_not_markup(tmp_path):
    """Both questions are agent-authored, and the difference may be exactly what markup eats:
    a bracketed aside, an emoji code, or a closing tag that raises."""
    run = _run(tmp_path / "data", [{"id": "q1", "text": "Did they vote [sic] :smile: yes?"}],
               [("q1", "Did they vote [/] :smile: no?")])
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert "'Did they vote [/] :smile: no?'" in out, out
    assert "'Did they vote [sic] :smile: yes?'" in out, out


URL = "https://news.example/council-vote"
SNIPPET = "voted against the harbor levy on its second reading"


def _cited(root, run, qid, question):
    """`run/claims/<qid>.json`, citing a page cached under `root`, so `provenance check-claim` passes it
    offline on everything but its question."""
    page = PageCache(url=URL, final_url=URL, status=200, content_type="text/html", title="T",
                     text=f"At the meeting the member {SNIPPET}, the minutes show.",
                     fetched_at=datetime.now(UTC) - timedelta(hours=6),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(root, URL).write_text(page.model_dump_json())
    source = Source(url=URL, publisher="Example News", author="A. Reporter", date="2026-05-14",
                    source_type="bylined_journalism", snippet=SNIPPET)
    path = run / "claims" / f"{qid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(Claim(question_id=qid, question=question, answer="Against.",
                          sources=[source]).model_dump_json())
    return path


def test_check_claim_fails_a_question_build_would_refuse(tmp_path):
    """A researcher's own gate. Checked only at build, one misquoted question stopped the whole
    run's review app, a retry round after the researcher had reported done."""
    root = write_project(tmp_path / "data")
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": VOTE}]))

    path = _cited(root, root, "q1", VOTE)
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 0 and "All sources check out." in out, out

    path = _cited(root, root, "q1", VOTE.rstrip("?"))
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 1, out
    assert f"question is not the one {root / 'questions.json'} asks at q1" in out, out
    assert f"yours: '{VOTE.rstrip('?')}' asked: '{VOTE}'" in out, out

    path = _cited(root, root, "q9", VOTE)
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 1, out
    assert f"question id q9 is not in {root / 'questions.json'}" in out, out
    assert "do not edit it" in out, out

    # A miscased id is the researcher's to fix, not a changed question set to report.
    path = _cited(root, root, "Q1", VOTE)
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 1, out
    assert (f"question id Q1 is not in {root / 'questions.json'}, which has q1: they differ "
            f"only in case") in out, out
    assert "changed under you" not in out, out


def test_check_claim_finds_the_run_from_inside_claims(tmp_path, monkeypatch):
    """`provenance check-claim q1.json` from inside claims/ gave a parent name of "", fell back to
    --data, found no question set there, and passed without checking the question."""
    root = write_project(tmp_path / "data")
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": VOTE}]))
    _cited(root, root, "q1", VOTE.rstrip("?"))
    monkeypatch.chdir(root / "claims")
    # No --data, as a researcher runs it: from here the run is the claim's, whose question set
    # is the project's. --cache only points at the cached page.
    code, out = _provenance("check-claim", "q1.json", "--cache", root)
    assert code == 1 and "question is not the one" in out, out


def test_check_claim_reads_the_question_set_of_the_run_the_claim_is_in(tmp_path):
    """A candidate run's claim is checked with --data naming the project root, whose template
    is not retargeted to the candidate. The run is the directory holding the claim's claims/."""
    root = write_project(tmp_path / "data", subjects=["cand"])
    (root / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "How did Alex Placeholder vote on the levy?"}]))
    run = root / "cand"
    run.mkdir()
    (run / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "How did Sam Sample vote on the levy?"}]))
    path = _cited(root, run, "q1", "How did Sam Sample vote on the levy?")
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 0 and "All sources check out." in out, out


def test_check_claim_with_no_question_set_says_so_and_one_it_cannot_read_fails(tmp_path):
    root = write_project(tmp_path / "data")
    path = _cited(root, root, "q1", VOTE)
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 0, out
    assert "no questions.json for" in out and "the question was not checked" in out, out

    (root / "questions.json").write_text('{"q1": "?"}')
    code, out = _provenance("check-claim", path, "--data", root)
    assert code == 1, out
    assert "cannot check the question:" in out and "is not a list of questions" in out, out


def test_an_id_retired_as_the_gate_says_leaves_nothing_to_report(tmp_path):
    """The retire procedure and the unowned-shard message said where a retired claim goes
    (claims-archive/) but only that its shard leaves judgments/. Both now name
    judgments-archive/, and a run retired that way passes the gate with nothing left over."""
    from provenance import judgments

    root = write_project(tmp_path / "data")
    (root / "questions.json").write_text(json.dumps([{"id": "q2", "text": VOTE}]))
    _cited(root, root, "q2", VOTE)
    old = Claim.model_validate_json(_cited(root, root, "q1", VOTE).read_text())
    judgments.record(root, "q1", old.sources[0].sid, "supports", "judged under the old id")

    code, out = _provenance("build", "--data", root)
    assert code == 1 and "shard from judgments/ to judgments-archive/" in out, out

    (root / "claims-archive").mkdir()
    (root / "claims" / "q1.json").rename(root / "claims-archive" / "q1.json")
    _, out = _provenance("judgments", "--data", root)
    assert "1 verdict(s) sit in judgments/ under an id no claim has (q1.json)" in out, out
    assert "Move each to judgments-archive/ to keep it" in out, out

    (root / "judgments-archive").mkdir()
    judgments.path_for(root, "q1").rename(root / "judgments-archive" / "q1.json")
    _, out = _provenance("judgments", "--data", root)
    assert "no claim has" not in out and "judgments-archive" not in out, out
    code, out = _provenance("build", "--data", root)
    assert code == 0 and "left out" not in out, out
    assert set(_built(root)) == {"q2"}
