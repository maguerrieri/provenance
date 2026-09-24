"""`vg remap` and the verdict re-homing it needed are retired: question ids are stable and never
reused, so a claim never moves between ids. What stays is that an old invocation is told why,
that a backup an interrupted re-home left behind still stops every reader, and that files
carrying the fields remap wrote still load and build."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from vgpipe import cli, judgments
from vgpipe.fetch import cache_path
from vgpipe.models import EXTRACTOR_VERSION, Claim, PageCache, Question, Source

URL = "https://news.example/council-vote"
SNIPPET = "voted against the harbor levy on its second reading"


def _vg(*args):
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _source() -> Source:
    return Source(url=URL, publisher="Example News", author="A. Reporter", date="2026-05-14",
                  source_type="bylined_journalism", snippet=SNIPPET)


def _legacy_run(tmp_path):
    """A candidate run whose files carry what `vg remap` wrote: `maps_from` and `mapped_from`
    in the question template, `previous_question` in the claim. The cited page is cached, so
    everything below runs offline."""
    data, run = tmp_path / "data", tmp_path / "data" / "cand"
    page = PageCache(url=URL, final_url=URL, status=200, content_type="text/html", title="T",
                     text=f"At the meeting the member {SNIPPET}, the minutes show.",
                     fetched_at=datetime.now(UTC) - timedelta(hours=6),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(data, URL).write_text(page.model_dump_json())
    questions = [
        {"id": "q1", "text": "How did they vote on the levy?", "claim_type": "mechanical",
         "mapped_from": "q3"},
        {"id": "q2", "text": "Who funds them?", "claim_type": "mechanical", "maps_from": "q1"}]
    (data / "questions.json").write_text(json.dumps(questions))
    (run / "claims").mkdir(parents=True)
    claim = json.loads(Claim(question_id="q1", question="How did they vote on the levy?",
                             answer="Against.", sources=[_source()]).model_dump_json())
    claim["previous_question"] = "What was their levy vote?"
    (run / "claims" / "q1.json").write_text(json.dumps(claim))
    return data, run, questions


def test_files_carrying_remaps_fields_still_load_and_build(tmp_path):
    """Nothing writes `maps_from`, `mapped_from` or `previous_question` any more, but runs made
    before remap was retired carry them, and every command still has to read those runs."""
    data, run, questions = _legacy_run(tmp_path)
    assert [Question.model_validate(q).id for q in questions] == ["q1", "q2"]

    code, out = _vg("verify", "--data", run)
    assert code == 0, out
    (claim,) = cli.load_claims(run / "claims", trust_machine_fields=True)
    assert claim.sources[0].verification.status == "verified"
    code, out = _vg("judge", "q1", claim.sources[0].sid, "supports", "--data", run)
    assert code == 0 and "supports recorded" in out, out
    code, out = _vg("judgments", "--data", run)
    assert code == 0 and "0 of 1 cited source(s) need a verdict" in out, out
    code, out = _vg("build", "--data", run)
    assert code == 0, out
    (built,) = json.loads((run / "out" / "claims.json").read_text())
    assert built["question_id"] == "q1"
    assert built["sources"][0]["verification"]["support"] == "supports"

    # A new run copies neither half of a migration: it has no earlier id space.
    code, out = _vg("new-candidate", "ng", "--data", data)
    assert code == 0, out
    copied = json.loads((data / "ng" / "questions.json").read_text())
    assert [q["id"] for q in copied] == ["q1", "q2"]
    assert not [q for q in copied if "maps_from" in q or "mapped_from" in q], copied


def _tree(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("args, named", [
    (["remap"], "`vg remap`"),
    (["remap", "--apply"], "`vg remap`"),
    (["remap", "--apply", "--archive-stranded"], "`vg remap`"),
    (["remap", "--mark-applied"], "`vg remap`"),
    (["judgments", "--repair"], "`vg judgments --repair`"),
    (["judgments", "--repair", "--moved", "q1:q2", "--gone", "q3"],
     "`vg judgments --repair --moved --gone`"),
    (["judgments", "--moved", "q1:q2"], "`vg judgments --moved`"),
    (["judgments", "--gone", "q3"], "`vg judgments --gone`"),
    (["judgments", "--rollback"], "`vg judgments --rollback`"),
], ids=lambda v: " ".join(v) if isinstance(v, list) else "")
def test_a_retired_command_says_why_and_changes_nothing(tmp_path, args, named):
    """An old script or habit must meet the rule that replaced these commands, not "No such
    command" — and must not have anything moved, re-homed or rolled back on its behalf. The
    refusal names the flags given, not one the caller never typed."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "judged before the retirement")
    before = _tree(data)
    code, out = _vg(*args, "--data", run)
    assert code == 1, out
    assert f"{named} is retired: question ids are stable and never reused" in out, out
    assert "A split or reworded question gets a new id, and the old id is retired" in out, out
    assert _tree(data) == before


def test_retired_commands_are_not_offered(tmp_path):
    _, out = _vg("--help")
    assert "remap" not in out, out
    _, out = _vg("judgments", "--help")
    assert not re.search(r"--(repair|rollback|moved|gone)\b", out), out


def test_a_backup_an_interrupted_re_home_left_still_stops_every_reader(tmp_path, monkeypatch):
    """No re-home runs any more, but one an older version was running when it died left
    judgments-backup/ behind, with shards that may be half-rewritten. Reading them as the
    verdicts would render whatever is missing as unreviewed, so every reader still refuses,
    and names a commit whose `--rollback` undoes it. `--rollback` here says the same rather
    than that it is retired, since that is what the operator asking for it needs.

    The command runs from that other checkout, so the run is named by its absolute path: a
    relative `--data data` there names that checkout's own data/, which holds no backup, and
    the old rollback reports nothing to undo."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "half-rewritten")
    (judgments.backup_dir(run) / "q1.json").parent.mkdir()
    (judgments.backup_dir(run) / "q1.json").write_text("[]")
    before = _tree(data)
    monkeypatch.chdir(tmp_path)
    for args in (["judgments"], ["judgments", "--rollback"], ["judgments", "--repair"],
                 ["build"], ["status"], ["verify"]):
        code, out = _vg(*args, "--data", run.relative_to(tmp_path))
        assert code == 1, (args, out)
        assert "was interrupted, and its shards may be half-rewritten" in out, (args, out)
        assert (f"run `vg judgments --rollback --data {run.resolve()}` from a checkout of "
                f"commit {judgments.LAST_WITH_ROLLBACK}, which still has it") in out, (args, out)
        assert "is retired" not in out, (args, out)
    assert _tree(data) == before


def test_the_commit_named_for_rollback_is_on_main_and_has_it():
    """The refusal above sends an operator to this commit. It must stay reachable, and its
    `vg judgments` must still take `--rollback`."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    commit = judgments.LAST_WITH_ROLLBACK
    old = git("show", f"{commit}:src/vgpipe/judgments.py")
    if old.returncode != 0:
        pytest.skip(f"no git history with {commit} here: {old.stderr.strip()}")
    assert "\ndef rollback(root: Path)" in old.stdout
    assert "rollback: bool = False" in git("show", f"{commit}:src/vgpipe/cli.py").stdout


def test_a_verdict_recorded_while_another_is_being_written_is_not_lost(tmp_path, monkeypatch):
    """record() reads a shard and rewrites it, and verifier agents record in parallel. A second
    `vg judge` landing between the first one's read and its rewrite was never read, so the
    rewrite deleted it. The directory lock makes it wait and land on top instead. (The test
    that pinned this lock went with `rehome()`, which shared it.)"""
    import threading

    first, second = _source(), Source(**{**_source().model_dump(), "url": URL + "-2"})
    real_read, waiting = judgments._read, []

    def read_then_judge(p):
        read = real_read(p)
        if not waiting:
            t = threading.Thread(target=judgments.record,
                                 args=(tmp_path, "q1", second.sid, "topic_only", "second"))
            waiting.append(t)
            t.start()
            t.join(timeout=0.3)
            assert t.is_alive(), "the second verdict was written while the first held a stale read"
        return read

    judgments.record(tmp_path, "q1", first.sid, "supports", "warm-up")   # judgments/ exists
    monkeypatch.setattr(judgments, "_read", read_then_judge)
    judgments.record(tmp_path, "q1", first.sid, "supports", "first")
    waiting[0].join(timeout=5)
    assert not waiting[0].is_alive()
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q1").items()} == {
        first.sid: "first", second.sid: "second"}
