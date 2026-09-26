"""`vg remap` and the verdict re-homing it needed are retired: question ids are stable and never
reused, so a claim never moves between ids. What stays is that an old invocation is told why,
that a backup an interrupted re-home left behind still stops every reader, and that files
carrying the fields remap wrote still load and build."""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime, timedelta

import pytest
from conftest import write_project
from typer.testing import CliRunner

from provenance import cli, judgments
from provenance.fetch import cache_path
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Question, Source

URL = "https://news.example/council-vote"
SNIPPET = "voted against the harbor levy on its second reading"


def _provenance(*args):
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


def _source() -> Source:
    return Source(url=URL, publisher="Example News", author="A. Reporter", date="2026-05-14",
                  source_type="bylined_journalism", snippet=SNIPPET)


def _legacy_run(tmp_path):
    """A subject's run whose files carry what `vg remap` wrote: `maps_from` and `mapped_from`
    in the question template, `previous_question` in the claim. The cited page is cached, so
    everything below runs offline."""
    data = write_project(tmp_path / "data", subjects=["cand", "ng"])
    run = data / "cand"
    page = PageCache(url=URL, final_url=URL, status=200, content_type="text/html", title="T",
                     text=f"At the meeting the member {SNIPPET}, the minutes show.",
                     fetched_at=datetime.now(UTC) - timedelta(hours=6),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(data, URL).write_text(page.model_dump_json())
    questions = [
        {"id": "q1", "text": "How did they vote on the levy?", "claim_type": "mechanical",
         "mapped_from": "q3"},
        {"id": "q2", "text": "Who funds them?", "claim_type": "mechanical", "maps_from": "q1"}]
    (run / "claims").mkdir(parents=True)
    for d in (data, run):
        (d / "questions.json").write_text(json.dumps(questions))
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

    code, out = _provenance("verify", "--data", run)
    assert code == 0, out
    (claim,) = cli.load_claims(run / "claims", trust_machine_fields=True)
    assert claim.sources[0].verification.status == "verified"
    code, out = _provenance("judge", "q1", claim.sources[0].sid, "supports", "--data", run,
                            *_handed(run, "q1", claim.sources[0].sid))
    assert code == 0 and "supports recorded" in out, out
    code, out = _provenance("judgments", "--data", run)
    assert code == 0 and "0 of 1 cited source(s) need a verdict" in out, out
    code, out = _provenance("build", "--data", run)
    assert code == 0, out
    (built,) = json.loads((run / "out" / "claims.json").read_text())
    assert built["question_id"] == "q1"
    assert built["sources"][0]["verification"]["support"] == "supports"

    # A new run copies neither half of a migration: it has no earlier id space.
    code, out = _provenance("new-subject", "ng", "--data", data)
    assert code == 0, out
    copied = json.loads((data / "ng" / "questions.json").read_text())
    assert [q["id"] for q in copied] == ["q1", "q2"]
    assert not [q for q in copied if "maps_from" in q or "mapped_from" in q], copied


def _tree(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("args, named", [
    (["remap"], "`provenance remap`"),
    (["remap", "--apply"], "`provenance remap`"),
    (["remap", "--apply", "--archive-stranded"], "`provenance remap`"),
    (["remap", "--mark-applied"], "`provenance remap`"),
    (["judgments", "--repair"], "`provenance judgments --repair`"),
    (["judgments", "--repair", "--moved", "q1:q2", "--gone", "q3"],
     "`provenance judgments --repair --moved --gone`"),
    (["judgments", "--moved", "q1:q2"], "`provenance judgments --moved`"),
    (["judgments", "--gone", "q3"], "`provenance judgments --gone`"),
    (["judgments", "--rollback"], "`provenance judgments --rollback`"),
], ids=lambda v: " ".join(v) if isinstance(v, list) else "")
def test_a_retired_command_says_why_and_changes_nothing(tmp_path, args, named):
    """An old script or habit must meet the rule that replaced these commands, not "No such
    command" — and must not have anything moved, re-homed or rolled back on its behalf. The
    refusal names the flags given, not one the caller never typed."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "judged before the retirement")
    before = _tree(data)
    code, out = _provenance(*args, "--data", run)
    assert code == 1, out
    assert f"{named} is retired: question ids are stable and never reused" in out, out
    assert "A split or reworded question gets a new id, and the old id is retired" in out, out
    assert _tree(data) == before


def test_retired_commands_are_not_offered(tmp_path):
    _, out = _provenance("--help")
    assert "remap" not in out, out
    _, out = _provenance("judgments", "--help")
    assert not re.search(r"--(repair|rollback|moved|gone)\b", out), out


def test_a_backup_an_interrupted_re_home_left_still_stops_every_reader(tmp_path, monkeypatch):
    """No re-home runs any more, but one an older version was running when it died left
    judgments-backup/ behind, with shards that may be half-rewritten. Reading them as the
    verdicts would render whatever is missing as unreviewed, so every reader still refuses,
    and names a commit whose `--rollback` undoes it. `--rollback`, `--repair` and `provenance remap`
    say the same rather than that they are retired, since that is what an operator retrying the
    interrupted command needs.

    The command runs from that other checkout, so the run is named by its absolute path: a
    relative `--data data` there names that checkout's own data/, which holds no backup, and
    the old rollback reports nothing to undo."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "half-rewritten")
    (judgments.backup_dir(run) / "q1.json").parent.mkdir()
    (judgments.backup_dir(run) / "q1.json").write_text("[]")
    before = _tree(data)
    monkeypatch.chdir(tmp_path)
    # `provenance judge` too, the one writer: a verdict recorded into half-rewritten shards is one the
    # old rollback then overwrites.
    for args in (["judgments"], ["judgments", "--rollback"], ["judgments", "--repair"],
                 ["remap", "--apply"], ["build"], ["status"], ["verify"],
                 ["judge", "q1", _source().sid, "supports"]):
        code, out = _provenance(*args, "--data", run.relative_to(tmp_path))
        assert code == 1, (args, out)
        assert "was interrupted, and its shards may be half-rewritten" in out, (args, out)
        assert (f"run `vg judgments --rollback --data {shlex.quote(str(run.resolve()))}` from "
                f"a checkout of commit {judgments.LAST_WITH_ROLLBACK}, which still has it"
                ) in out, (args, out)
        # after the rollback, the migration is still pending, and only that checkout can apply it
        assert "Then, from that checkout, re-run the command that was interrupted" in out, out
        assert "is retired" not in out, (args, out)
    assert _tree(data) == before


def test_the_rollback_command_quotes_a_run_path_with_a_space(tmp_path):
    """The operator pastes the command into another checkout's shell. Unquoted, a space in the
    run's path split `--data` in two: the old rollback looked in the first half, found no backup
    and reported nothing to undo, while every reader here kept refusing."""
    data, run, _ = _legacy_run(tmp_path / "my runs")
    judgments.backup_dir(run).mkdir()
    code, out = _provenance("judgments", "--data", run)
    assert code == 1, out
    cmd = re.search(r"run `(vg judgments --rollback --data .*?)` from a checkout", out)
    assert cmd, out
    assert shlex.split(cmd.group(1))[-2:] == ["--data", str(run.resolve())], cmd.group(1)


def test_the_commit_named_for_rollback_is_on_main_and_has_it():
    """The refusal above sends an operator to this commit. It must be named in full, be on
    main, where the operator's clone has it (a commit only on a feature branch can be rebased
    away), and its `vg judgments` must still take `--rollback`. CI's checkout is shallow, so
    the history half runs where the history is."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    commit = judgments.LAST_WITH_ROLLBACK
    assert re.fullmatch(r"[0-9a-f]{40}", commit), "an abbreviated id can become ambiguous"
    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        pytest.skip(f"no git history with {commit} here")
    # It predates the rename (#6): its package is vgpipe and its command vg. Skipped only for a
    # missing commit: the rename first made this path wrong, and a skip on any failure hid it.
    old = git("show", f"{commit}:src/vgpipe/judgments.py")
    assert old.returncode == 0, old.stderr
    # HEAD only where there is no origin/main to ask, as in a fork's checkout.
    main = ("origin/main" if git("rev-parse", "--verify", "--quiet", "origin/main").returncode == 0
            else "HEAD")
    assert git("merge-base", "--is-ancestor", commit, main).returncode == 0, main
    assert "\ndef rollback(root: Path)" in old.stdout
    assert "rollback: bool = False" in git("show", f"{commit}:src/vgpipe/cli.py").stdout


def test_provenance_judgments_names_the_scratch_an_interrupted_re_home_left(tmp_path):
    """A re-home killed while building its backup left judgments-backup.partial/, and one killed
    while deleting the retired backup left judgments-backup.discard/. The next re-home cleared
    them. Nothing does now, so `provenance judgments` says what they are, before anyone mistakes one
    for the backup and restores stale verdicts from it."""
    data, run, _ = _legacy_run(tmp_path)
    code, out = _provenance("verify", "--data", run)
    assert code == 0, out
    for name in ("judgments-backup.partial", "judgments-backup.discard"):
        (run / name).mkdir()
        (run / name / "q1.json").write_text("[]")
    code, out = _provenance("judgments", "--data", run)
    for name in ("judgments-backup.partial", "judgments-backup.discard"):
        assert (f"{run / name} is scratch an interrupted re-home by the retired `vg remap` left "
                f"behind") in out, out
    # A .partial copied the shards before any was touched, and a .discard is the shards before a
    # re-home that finished: neither is the run's verdicts now.
    assert ("holds at most an older copy of the run's verdicts: delete it, and never restore "
            "from it") in out, out
    assert "was interrupted, and its shards may be half-rewritten" not in out, "not the backup"


def test_provenance_judgments_counts_a_shard_a_case_folding_disk_opens_as_the_claims(tmp_path,
                                                                           monkeypatch):
    """On macOS's default disk `provenance build` opens Q1.json for claim q1 and applies its verdicts,
    so `provenance judgments` must count them as q1's, or its gate never reaches 0. CI's disk keeps the
    two names apart, so that branch never ran there once the re-home tests that simulated a
    folding disk went: this simulates one by answering `_same_file()` as such a disk would."""
    data, run, _ = _legacy_run(tmp_path)
    code, out = _provenance("verify", "--data", run)
    assert code == 0, out
    (claim,) = cli.load_claims(run / "claims", trust_machine_fields=True)
    page_url, page_at, ver = judgments.judged_copy(claim.sources[0], data)
    judgments.record(run, "Q1", claim.sources[0].sid, "supports", "judged as Q1",
                     page_fetched_at=page_at, extractor_version=ver, page_url=page_url,
                     claim_fingerprint=claim.fingerprint)
    code, out = _provenance("judgments", "--data", run)
    if not (run / "judgments" / "q1.json").exists():   # this disk keeps Q1 and q1 apart
        assert code == 1 and "under an id no claim has (Q1.json)" in out, out
        monkeypatch.setattr(judgments, "_same_file", lambda root, stem, qid: (
            stem.casefold() == qid.casefold() and judgments._on_disk(root, stem).exists()))
        code, out = _provenance("judgments", "--data", run)
    assert code == 0 and "0 of 1 cited source(s) need a verdict" in out, out
    assert "Q1.json differ from a claim's id only in case, and this disk opens them" in out, out
    assert "Rename each to its claim's exact id by hand" in out, out


def test_a_verdict_recorded_while_another_is_being_written_is_not_lost(tmp_path, monkeypatch):
    """record() reads a shard and rewrites it, and verifier agents record in parallel. A second
    `provenance judge` landing between the first one's read and its rewrite was never read, so the
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
