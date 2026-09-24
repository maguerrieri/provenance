"""`vg remap --archive-stranded`: where a stranded claim goes, and that it goes inside the re-home
transaction.

It used to be a plain rename into one shared claims-archive/, done before the transaction began:
a later migration stranding a file of the same name replaced the earlier archived copy, and a
re-home that refused or failed after it left the stranded files moved while the error said
nothing had moved.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import typer

from vgpipe import cli, judgments
from vgpipe.models import Claim, Source


def _src(**kw) -> Source:
    d = dict(url="https://news.example/story", publisher="Example News", author="A. Writer",
             date="2026-05-14", source_type="bylined_journalism",
             snippet="the council voted to adopt the example ordinance")
    d.update(kw)
    return Source(**d)


def _claim(root: Path, qid: str, answer: str, *sources: Source) -> None:
    (root / "claims").mkdir(exist_ok=True)
    (root / "claims" / f"{qid}.json").write_text(
        Claim(question_id=qid, question=f"old {qid}", answer=answer,
              sources=list(sources)).model_dump_json())


def _questions(root: Path, *questions: dict) -> None:
    """questions.json, keeping any mapped_from an earlier apply retired onto a question."""
    qpath = root / "questions.json"
    retired = ({q["id"]: q["mapped_from"] for q in json.loads(qpath.read_text())
                if q.get("mapped_from")} if qpath.exists() else {})
    qpath.write_text(json.dumps([
        {"text": f"new {q['id']}", "claim_type": "mechanical",
         **({"mapped_from": retired[q["id"]]} if q["id"] in retired else {}), **q}
        for q in questions]))


def _files(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.json"))} if d.exists() else {}


def _archived(root: Path) -> dict[str, dict[str, bytes]]:
    """Every archived claim file, by run directory."""
    arch = root / "claims-archive"
    return {run.name: _files(run) for run in sorted(arch.iterdir())} if arch.exists() else {}


def _stranding_run(root: Path) -> Source:
    """q1 moves to q2, and q9 answers a question the template no longer asks, with a verdict."""
    s = _src()
    _claim(root, "q1", "moves to q2")
    _claim(root, "q9", "stranded research", s)
    judgments.record(root, "q9", s.sid, "supports", "judged for the stranded q9")
    _questions(root, {"id": "q2", "maps_from": "q1"})
    return s


def _plain(out: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(out.splitlines()))


def test_two_remaps_stranding_the_same_name_keep_both_archived_copies(tmp_path):
    """Both migrations strand a q9.json. One shared claims-archive/ kept the second over the
    first: the first run's research, gone, with nothing to say so."""
    _claim(tmp_path, "q1", "moves to q2")
    _claim(tmp_path, "q9", "the first q9")
    _questions(tmp_path, {"id": "q2", "maps_from": "q1"})
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)

    # new research lands on q9 again, and the next migration strands it too
    _claim(tmp_path, "q9", "the second q9")
    _questions(tmp_path, {"id": "q3", "maps_from": "q2"})
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)

    runs = _archived(tmp_path)
    assert len(runs) == 2, "one directory per run"
    kept = sorted(json.loads(files["q9.json"])["answer"] for files in runs.values())
    assert kept == ["the first q9", "the second q9"]
    assert json.loads((tmp_path / "claims" / "q3.json").read_text())["answer"] == "moves to q2"
    assert not (tmp_path / "claims" / "q9.json").exists()


def test_an_archived_claim_and_its_verdicts_share_a_run_directory(tmp_path):
    """The verdicts of an archived claim are archived with it, so restoring the claim means
    finding them: the same run name under claims-archive/ and judgments-archive/."""
    s = _stranding_run(tmp_path)
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)

    (run,) = _archived(tmp_path)
    assert json.loads((tmp_path / "claims-archive" / run / "q9.json").read_text())["answer"] \
        == "stranded research"
    (verdicts,) = judgments.archive_dir(tmp_path).iterdir()
    assert verdicts.name == run
    assert json.loads((verdicts / "q9.json").read_text())[0]["sid"] == s.sid


def _refuse_the_plan(monkeypatch):
    def refuse(*a, **kw):
        raise judgments.CannotRehome("cannot tell which question 1 verdict(s) belong to")

    monkeypatch.setattr(judgments, "_plan", refuse)


def test_a_refused_rehome_leaves_the_stranded_files_where_they_were(tmp_path, monkeypatch,
                                                                    capsys):
    """The stranded files were archived before the re-home began, so a re-home that then
    refused left them moved, under an error saying nothing had changed."""
    _stranding_run(tmp_path)
    claims_before = _files(tmp_path / "claims")
    shards_before = _files(tmp_path / "judgments")
    _refuse_the_plan(monkeypatch)

    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert exc.value.exit_code == 1
    assert "cannot tell which question" in _plain(capsys.readouterr().out)
    assert _files(tmp_path / "claims") == claims_before
    assert _files(tmp_path / "judgments") == shards_before
    assert not (tmp_path / "claims-archive").exists(), "nothing archived"
    assert not (tmp_path / ".remap-applied").exists()


def _archive_before(root: Path, earlier: bool) -> dict[str, dict[str, bytes]]:
    """An earlier run's claims archive, or none: a rollback removes only what its own run made."""
    if earlier:
        run = root / "claims-archive" / "20200101T000000.000000Z"
        run.mkdir(parents=True)
        (run / "q9.json").write_text(json.dumps({"question_id": "q9", "answer": "an older q9"}))
    return _archived(root)


def _archive_unchanged(root: Path, before: dict[str, dict[str, bytes]]) -> bool:
    return _archived(root) == before and (bool(before) or not (root / "claims-archive").exists())


PRIOR = pytest.mark.parametrize("earlier", [pytest.param(False, id="first-archive"),
                                            pytest.param(True, id="earlier-archive")])


def _fail_writing(monkeypatch, name: str, exc: BaseException) -> None:
    """Fail writing claims/<name>, after the stranded files are archived."""
    real = Path.write_text

    def flaky(self, *a, **kw):
        if self.parent.name == "claims" and self.name == name:
            raise exc
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", flaky)


@PRIOR
def test_a_remap_that_fails_after_archiving_puts_the_stranded_files_back(tmp_path, monkeypatch,
                                                                         capsys, earlier):
    """"Nothing moved: the verdicts, claim files and questions.json were put back" — except the
    stranded files, which moved before the transaction and stayed moved."""
    _stranding_run(tmp_path)
    archive_before = _archive_before(tmp_path, earlier)
    claims_before = _files(tmp_path / "claims")
    shards_before = _files(tmp_path / "judgments")
    questions_before = (tmp_path / "questions.json").read_bytes()
    _fail_writing(monkeypatch, "q2.json", OSError(28, "No space left on device"))

    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert exc.value.exit_code == 1
    assert "Nothing moved" in _plain(capsys.readouterr().out)
    monkeypatch.undo()
    assert _files(tmp_path / "claims") == claims_before
    assert _files(tmp_path / "judgments") == shards_before
    assert (tmp_path / "questions.json").read_bytes() == questions_before
    assert _archive_unchanged(tmp_path, archive_before), \
        "the begun archive is removed, not left as a second copy"
    assert not judgments.backup_dir(tmp_path).exists()

    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    runs = _archived(tmp_path)
    assert len(runs) == len(archive_before) + 1
    assert sorted(json.loads(files["q9.json"])["answer"] for files in runs.values()) \
        == ["an older q9"] * earlier + ["stranded research"]


@PRIOR
def test_a_remap_killed_after_archiving_is_undone_by_rollback(tmp_path, monkeypatch, capsys,
                                                              earlier):
    """A kill can't restore anything, so the backup stays and `vg judgments --rollback` undoes
    the re-home: that has to include the stranded files, and the archive run they went to."""
    _stranding_run(tmp_path)
    archive_before = _archive_before(tmp_path, earlier)
    claims_before = _files(tmp_path / "claims")
    shards_before = _files(tmp_path / "judgments")
    _fail_writing(monkeypatch, "q2.json", KeyboardInterrupt())

    def die(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(judgments, "_restore", die)
    with pytest.raises(KeyboardInterrupt):
        cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    monkeypatch.undo()
    assert not (tmp_path / "claims" / "q9.json").exists(), "the archive really was under way"
    (begun,) = set(_archived(tmp_path)) - set(archive_before)
    capsys.readouterr()

    cli.show_judgments(data=tmp_path, rollback=True)
    out = _plain(capsys.readouterr().out)
    assert "put back" in out
    # the operator is told the archive run went: a directory they may already have opened. A
    # claims-archive/ the run made goes with it.
    removed = [tmp_path / "claims-archive" / begun] + [tmp_path / "claims-archive"] * (not earlier)
    assert f"removed {', '.join(map(str, removed))}, which it had begun" in out
    assert _files(tmp_path / "claims") == claims_before
    assert _files(tmp_path / "judgments") == shards_before
    assert _archive_unchanged(tmp_path, archive_before)
    assert not judgments.backup_dir(tmp_path).exists()


def test_a_dry_run_does_not_refuse_a_collision_archiving_would_clear(tmp_path, capsys):
    """q3 holds research nothing maps away from, and q4's claim is moving onto it. With
    --archive-stranded the apply archives q3 first, so the dry run of that same command must
    not refuse where the apply goes through."""
    _claim(tmp_path, "q3", "nothing maps away from this")
    _claim(tmp_path, "q4", "moves to q3")
    _questions(tmp_path, {"id": "q3", "maps_from": "q4"})
    claims_before = _files(tmp_path / "claims")

    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path)
    assert "would be overwritten" in _plain(capsys.readouterr().out)

    cli.remap(data=tmp_path, archive_stranded=True)
    assert "would be overwritten" not in _plain(capsys.readouterr().out)
    assert _files(tmp_path / "claims") == claims_before, "a dry run moves nothing"
    assert not _archived(tmp_path)

    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    (run,) = _archived(tmp_path).values()
    assert json.loads(run["q3.json"])["answer"] == "nothing maps away from this"
    assert json.loads((tmp_path / "claims" / "q3.json").read_text())["answer"] == "moves to q3"


def test_a_rehome_refuses_to_create_what_is_already_there(tmp_path):
    """A path the transaction creates is removed by its rollback, so one that already exists
    would be deleted by a rollback it never belonged to. Refused before anything is written."""
    s = _src()
    _claim(tmp_path, "q1", "a", s)
    judgments.record(tmp_path, "q1", s.sid, "supports")
    taken = tmp_path / "claims-archive" / "run"
    taken.mkdir(parents=True)
    (taken / "q5.json").write_text("{}")
    shards_before = _files(tmp_path / "judgments")
    ran = []

    with pytest.raises(FileExistsError):
        judgments.rehome(tmp_path, [], then=lambda: ran.append(1), creates=(taken,))
    assert not ran
    assert _files(tmp_path / "judgments") == shards_before
    assert (taken / "q5.json").read_text() == "{}"
    assert not judgments.backup_dir(tmp_path).exists()


@pytest.mark.parametrize("outside", ["../elsewhere", "/tmp/elsewhere", "claims/../../elsewhere"])
def test_a_rehome_creates_only_inside_its_run(tmp_path, outside):
    with pytest.raises(ValueError, match="inside"):
        judgments.rehome(tmp_path, [], then=lambda: None, creates=(tmp_path / outside,))


def test_a_rehome_removes_only_the_directories_it_made(tmp_path):
    """Asked when the transaction begins, under the lock: what already exists above a created
    path stays, and what the re-home made above it goes with it."""
    kept = tmp_path / "kept"
    (kept / "older").mkdir(parents=True)
    run = kept / "made" / "run"

    def make_then_fail():
        run.mkdir(parents=True)
        (run / "q5.json").write_text("{}")
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError, match="No space left"):
        judgments.rehome(tmp_path, [], then=make_then_fail, creates=(run,))
    assert sorted(p.name for p in kept.iterdir()) == ["older"]
    assert not judgments.backup_dir(tmp_path).exists()


def _lapsing_run(root: Path) -> None:
    """q1's verdict on a source its claim no longer cites: a re-home archives it."""
    s = _src()
    _claim(root, "q1", "a")
    judgments.record(root, "q1", s.sid, "supports", "lapsed")


def test_a_rehome_refuses_a_stamp_whose_archive_run_exists(tmp_path):
    """A restore deletes the archive run it began. Named by the caller, the run could already
    hold an earlier re-home's verdicts, and a failure would have deleted them."""
    _lapsing_run(tmp_path)
    earlier = judgments.archive_dir(tmp_path) / "20200101T000000.000000Z"
    earlier.mkdir(parents=True)
    (earlier / "q7.json").write_text("[]")
    shards_before = _files(tmp_path / "judgments")
    claims = [Claim(question_id="q1", question="?", answer="a")]

    with pytest.raises(FileExistsError):
        judgments.rehome(tmp_path, claims, stamp=earlier.name)
    assert (earlier / "q7.json").read_text() == "[]"
    assert _files(tmp_path / "judgments") == shards_before
    assert not judgments.backup_dir(tmp_path).exists()
    with pytest.raises(ValueError, match="one path component"):
        judgments.rehome(tmp_path, claims, stamp="../elsewhere")


def _killed(monkeypatch, root: Path, then, creates) -> None:
    """A re-home killed partway through `then`, with no restore: the backup stays."""
    def die(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(judgments, "_restore", die)
    with pytest.raises(KeyboardInterrupt):
        judgments.rehome(root, [], then=then, creates=creates)
    monkeypatch.undo()


def test_a_rollback_reports_only_what_it_removed(tmp_path, monkeypatch):
    """Killed before `then` made anything, the rollback has nothing of it to remove, and saying
    it removed the archive run would send the operator looking for a directory never made."""
    _lapsing_run(tmp_path)

    def killed():
        raise KeyboardInterrupt

    _killed(monkeypatch, tmp_path, killed, (tmp_path / "claims-archive" / "run",))
    assert judgments.rollback(tmp_path)[2] == []


def test_a_rollback_keeps_what_was_put_beside_the_archive_run_since(tmp_path, monkeypatch):
    """The first run's claims-archive/ is the re-home's too, but a file put there after the kill
    is not: the rollback removes the run and leaves the directory holding it."""
    _lapsing_run(tmp_path)
    run = tmp_path / "claims-archive" / "run"

    def killed():
        run.mkdir(parents=True)
        (run / "q9.json").write_text("{}")
        raise KeyboardInterrupt

    _killed(monkeypatch, tmp_path, killed, (run,))
    (tmp_path / "claims-archive" / "by-hand.json").write_text("{}")
    assert judgments.rollback(tmp_path)[2] == ["claims-archive/run"]
    assert sorted(p.name for p in (tmp_path / "claims-archive").iterdir()) == ["by-hand.json"]


def test_load_claims_collects_origins_without_reading_them_as_duplicates(tmp_path):
    _claim(tmp_path, "q1", "a")
    origin = {"q1": "from an earlier load"}
    assert [c.question_id for c in cli.load_claims(tmp_path / "claims", origin=origin)] == ["q1"]
    assert origin == {"q1": "q1.json"}
