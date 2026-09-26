"""A build that stops short leaves no review app behind for `provenance serve`.

`provenance build` renders nothing when it can't read what it would check or render (the question set,
a claim, verdict or archive file), and a crash or a kill stops it too. Each used to leave the
previous build's review.html and claims.json in out/, and `provenance serve` served that render as if
nothing had happened. Build now removes the last render before any step that can stop it, so
whatever stops it leaves nothing to serve, and serve says why."""

from __future__ import annotations

import json
import re

import pytest
from conftest import write_project
from typer.testing import CliRunner

from provenance import cli, report
from provenance.models import Claim

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


def _claim(run, qid, question, answer="a"):
    (run / "claims" / f"{qid}.json").write_text(
        Claim(question_id=qid, question=question, answer=answer).model_dump_json())


@pytest.fixture
def run(tmp_path):
    """A run that builds, already built once: the render a later refusal must not leave."""
    run = write_project(tmp_path / "data")
    (run / "claims").mkdir(parents=True)
    (run / "cache").mkdir()
    (run / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}]))
    _claim(run, "q1", VOTE)
    _claim(run, "q2", FUNDS)
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _left(run) == {report.REVIEW_HTML, report.CLAIMS_JSON}, out
    return run


def _left(run) -> set[str]:
    out = run / "out"
    return {p.name for p in out.iterdir()} if out.is_dir() else set()


def _unreadable_questions(run):
    (run / "questions.json").write_text(json.dumps({"q1": VOTE}))   # a set is a list


def _unreadable_claim(run):
    (run / "claims" / "q2.json").write_text("{not json")


def _unreadable_verdicts(run):
    (run / "judgments").mkdir()
    (run / "judgments" / "q1.json").write_text("{}")   # a shard is a list


def _unreadable_archives(run):
    (run / "archives.json").write_text("[]")


def _missing_race(run):
    write_project(run, race=run / "no-such-race.md")


REFUSALS = {
    "question set unreadable": ((), _unreadable_questions),
    "claim file unreadable": ((), _unreadable_claim),
    "verdict shard unreadable": ((), _unreadable_verdicts),
    "archive records unreadable": ((), _unreadable_archives),
    "cache root with no cache": (("--cache", "{tmp}"), None),
    "race file missing": ((), _missing_race),
}


@pytest.mark.parametrize("args, damage", REFUSALS.values(), ids=REFUSALS.keys())
def test_a_build_that_stops_short_leaves_no_review_app_to_serve(run, tmp_path, args, damage):
    if damage:
        damage(run)
    code, out = _provenance("build", "--data", run, *(a.format(tmp=tmp_path) for a in args))
    assert code != 0, out
    assert _left(run) == set(), out

    code, out = _provenance("serve", "--data", run, "--no-open-browser")
    assert code == 1, out
    assert f"No review.html in {(run / 'out').resolve()}" in out, out
    assert "its last run refused and rendered nothing. Run `provenance build`" in out, out


@pytest.mark.parametrize("how", ["run given", "run found from the working directory"])
def test_a_project_file_that_cannot_be_read_leaves_no_review_app(run, monkeypatch, how):
    """The run is found without reading the project file, so a project file that can't be read
    stops the build like any other refusal, after the last render is gone."""
    (run / "provenance.toml").write_text("name = ")
    if how == "run given":
        code, out = _provenance("build", "--data", run)
    else:
        monkeypatch.chdir(run / "claims")
        code, out = _provenance("build")
    assert code == 1 and "unreadable project file" in out, out
    assert _left(run) == set(), out


def test_a_project_file_it_cannot_load_still_says_which_directories_are_runs(run):
    """A project file that parses but isn't a valid project still lists its subjects: a listed
    subject's render goes, and a directory it doesn't list keeps its out/."""
    listed, other = run / "cand", run / "site"
    for d in (listed, other):
        (d / "out").mkdir(parents=True)
        (d / "out" / report.REVIEW_HTML).write_text("an earlier page")
    (run / "provenance.toml").write_text(
        (run / "provenance.toml").read_text().replace("subjects = []", 'subjects = ["cand"]')
        + "typo = 1\n")
    code, out = _provenance("build", "--data", listed)
    assert code == 1 and "unknown key(s) 'typo'" in out, out
    assert _left(listed) == set(), out
    code, out = _provenance("build", "--data", other)
    assert code == 1 and "unknown key(s) 'typo'" in out, out
    assert _left(other) == {report.REVIEW_HTML}, "no run of the project, as far as it can say"


def test_a_directory_that_is_no_run_keeps_its_out(run, tmp_path):
    """Only a run's render is removed. A directory the project doesn't declare is no run, and
    what its out/ holds is none of the project's: a mistyped --data must not delete it."""
    other = tmp_path / "data" / "site"
    (other / "out").mkdir(parents=True)
    (other / "out" / report.REVIEW_HTML).write_text("someone else's page")
    code, out = _provenance("build", "--data", other)
    assert code == 1 and "is neither the root of the project" in out, out
    assert _left(other) == {report.REVIEW_HTML}, out
    assert _left(run) == {report.REVIEW_HTML, report.CLAIMS_JSON}, "nor the project's own"


def test_the_question_id_refusal_says_the_app_was_not_rendered(run):
    _unreadable_questions(run)
    code, out = _provenance("build", "--data", run)
    assert code == 1 and "review app not rendered" in out, out
    assert _left(run) == set(), out


def test_a_build_after_the_fix_renders_again(run):
    _unreadable_questions(run)
    assert _provenance("build", "--data", run)[0] == 1
    (run / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": VOTE}, {"id": "q2", "text": FUNDS}]))
    code, out = _provenance("build", "--data", run)
    assert code == 0 and _left(run) == {report.REVIEW_HTML, report.CLAIMS_JSON}, out


def test_a_build_that_leaves_a_claim_out_serves_its_own_render(run):
    """A claim on an id the set does not list fails the build but not the render: the rest
    render, so what is served after that exit 1 is this build's, without the claim."""
    _claim(run, "q1", VOTE, answer="the newer answer")
    _claim(run, "q3", FUNDS)   # an id questions.json does not list
    code, out = _provenance("build", "--data", run)
    assert code == 1 and "left out of the review app" in out, out
    assert _left(run) == {report.REVIEW_HTML, report.CLAIMS_JSON}, out
    written = {c["question_id"]: c["answer"]
               for c in json.loads((run / "out" / report.CLAIMS_JSON).read_text())}
    assert written == {"q1": "the newer answer", "q2": "a"}


def test_a_build_that_dies_writing_the_page_leaves_none(run, monkeypatch):
    """review.html is written last, whole, by rename: a build killed just before the rename
    leaves this build's claims.json and no page, not half of one or the last build's."""
    replace = report.os.replace

    def killed_at_the_page(src, dst):
        if report.Path(dst).name == report.REVIEW_HTML:
            raise KeyboardInterrupt
        replace(src, dst)

    _claim(run, "q1", VOTE, answer="the newer answer")
    monkeypatch.setattr(report.os, "replace", killed_at_the_page)
    code, out = _provenance("build", "--data", run)
    assert code != 0, out
    assert _left(run) == {report.CLAIMS_JSON}, out   # nor the temp file
    written = json.loads((run / "out" / report.CLAIMS_JSON).read_text())
    assert [c["answer"] for c in written if c["question_id"] == "q1"] == ["the newer answer"]

    code, out = _provenance("serve", "--data", run, "--no-open-browser")
    assert code == 1, out


def test_a_build_removes_the_temp_files_a_killed_build_left(run):
    """A kill skips the cleanup, and serve lists out/, dotfiles included: a leftover temp can
    hold a whole page no build finished."""
    for name in (report.REVIEW_HTML, report.CLAIMS_JSON):
        (run / "out" / f".{name}.4242.tmp").write_text("left by a killed build")
    _unreadable_questions(run)
    assert _provenance("build", "--data", run)[0] == 1
    assert _left(run) == set()


def test_a_build_removes_only_what_it_writes(run):
    (run / "out" / "notes.txt").write_text("the reviewer's own file")
    _unreadable_questions(run)
    assert _provenance("build", "--data", run)[0] == 1
    assert _left(run) == {"notes.txt"}


def test_a_render_it_cannot_remove_stops_the_build(run, monkeypatch):
    def locked(self, missing_ok=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(report.Path, "unlink", locked)
    code, out = _provenance("build", "--data", run)
    assert code == 1, out
    assert f"could not clear the previous render from {run / 'out'}: [Errno 13]" in out, out
    assert "Remove it by hand" in out and "wrote" not in out, out
