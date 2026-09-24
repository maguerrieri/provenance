"""`vg remap` builds claim-file paths from the ids in questions.json: a question's own id and the
ids it maps or mapped from. That file is edited by agents and by hand, and remap reads it raw,
never through the Question schema, so an id is untrusted input there."""

from __future__ import annotations

import io
import json

import pytest
import typer
from rich.console import Console

from vgpipe import cli


@pytest.fixture
def out(monkeypatch):
    """What remap prints, unwrapped: rich folds a long tmp path mid-word at 80 columns."""
    buf = io.StringIO()
    monkeypatch.setattr(cli, "con", Console(file=buf, width=10_000, color_system=None))
    return buf


def _tree(root):
    """Every path under root, with each file's bytes: a refusal must change none of them."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def _claim(claims, qid, answer):
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{qid}.json").write_text(json.dumps(
        {"question_id": qid, "question": f"old {qid}", "answer": answer, "sources": []}))


def _question(qid, **extra):
    return {"id": qid, "text": f"new {qid}", "claim_type": "mechanical", **extra}


# Each names a file outside claims/ once joined as claims/<value>.json. Every one of those files
# exists, so a move that reached one would unlink it.
ESCAPING = [
    pytest.param(lambda tmp: "../victim", id="parent"),
    pytest.param(lambda tmp: "../../victim", id="grandparent"),
    pytest.param(lambda tmp: "sub/victim", id="separator"),
    # pathlib drops everything before an absolute component: claims / "/x/victim.json" is that.
    pytest.param(lambda tmp: str(tmp / "outside" / "victim"), id="absolute"),
    pytest.param(lambda tmp: ".victim", id="leading-dot"),
    # `$` in a `match` accepts a trailing newline, so the pattern must be matched in full.
    pytest.param(lambda tmp: "q2\n", id="trailing-newline"),
    pytest.param(lambda tmp: "../[/victim]", id="rich-markup"),
]

MODES = [
    pytest.param({}, id="dry-run"),
    pytest.param({"apply": True}, id="apply"),
    pytest.param({"apply": True, "archive_stranded": True}, id="archive-stranded"),
    pytest.param({"mark_applied": True}, id="mark-applied"),
]


def _victims(tmp_path, run):
    """A research file at every place an escaping value can reach, inside and outside the run."""
    for n, where in enumerate((run / "victim.json", tmp_path / "victim.json",
                               run / "claims" / "sub" / "victim.json",
                               tmp_path / "outside" / "victim.json",
                               run / "claims" / ".victim.json", run / "claims" / "q2\n.json",
                               run / "[" / "victim].json")):
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({"question_id": f"victim{n}", "question": "someone else's",
                                     "answer": "must survive", "sources": []}))


@pytest.mark.parametrize("flags", MODES)
@pytest.mark.parametrize("field", ["id", "maps_from", "mapped_from"])
@pytest.mark.parametrize("escaping", ESCAPING)
def test_remap_refuses_an_id_that_names_a_file_outside_claims(tmp_path, out, escaping, field,
                                                               flags):
    """An apply unlinks claims/<maps_from>.json and writes claims/<id>.json. With `../victim` as a
    maps_from it deleted a file beside claims/; as an id it wrote the moved claim there; an
    absolute path reached anywhere. Refused before any file is touched, in every mode, with the
    entry named, since the caller may be an agent that has to act on the message."""
    run = tmp_path / "run"
    _claim(run / "claims", "q1", "donations")
    _claim(run / "claims", "q2", "votes")
    _victims(tmp_path, run)
    bad = escaping(tmp_path)
    questions = [_question("q1", maps_from="q1"),
                 {"id": "q2", "maps_from": "q2"} | ({"id": bad} if field == "id" else {})
                 | {"text": "new q2", "claim_type": "mechanical"},
                 _question("q3", **({field: bad} if field != "id" else {}))]
    (run / "questions.json").write_text(json.dumps(questions))
    before = _tree(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=run, **flags)

    assert exc.value.exit_code == 1
    assert _tree(tmp_path) == before, "a refused remap touched a file"
    said = out.getvalue()
    assert "refusing to remap" in said
    assert repr(bad) in said, "the refusal names the offending value, printed literally"
    assert "entry 1" in said or "entry 2" in said, "and the entry it sits on"


def test_a_question_that_is_not_an_object_with_an_id_is_refused_not_a_crash(tmp_path, out):
    """Each of these was a traceback (KeyError, AttributeError, TypeError) before any check ran:
    remap read q["id"] and q.get() off every entry without asking what the entry was."""
    run = tmp_path / "run"
    _claim(run / "claims", "q1", "donations")
    for bad, named in ((["q1"], "entry 0"),
                       ([{"text": "no id", "claim_type": "mechanical"}], "entry 0"),
                       ([_question("q1"), {"id": 7, "text": "x"}], "entry 1"),
                       ([_question("q1"), {"id": None, "text": "x"}], "entry 1"),
                       ({"q1": _question("q1")}, "must be a list of questions, and holds a dict")):
        (run / "questions.json").write_text(json.dumps(bad))
        before = _tree(tmp_path)
        for flags in ({}, {"apply": True}, {"mark_applied": True}):
            out.truncate(0)
            out.seek(0)
            with pytest.raises(typer.Exit) as exc:
                cli.remap(data=run, **flags)
            assert exc.value.exit_code == 1, (bad, flags)
            said = out.getvalue()
            assert "refusing to remap" in said and named in said, (bad, flags, said)
            assert _tree(tmp_path) == before, (bad, flags)


def test_an_interrupted_re_home_is_reported_before_a_bad_id(tmp_path, out):
    """`vg judgments --rollback` puts questions.json back as the interrupted apply found it, so a
    fix made to it first is thrown away. The interrupted transaction is named first."""
    run = tmp_path / "run"
    _claim(run / "claims", "q1", "donations")
    (run / "questions.json").write_text(json.dumps([_question("q1", maps_from="../victim")]))
    (run / "judgments-backup").mkdir()

    with pytest.raises(typer.Exit):
        cli.remap(data=run)

    said = out.getvalue()
    assert "--rollback" in said and "refusing to remap" not in said, said


def test_ids_of_the_question_id_shape_still_remap(tmp_path, out):
    """The check is the claim schema's own id shape, nothing tighter: a letter suffix (q2a), a
    capital, and the dot, hyphen and underscore it allows all move, and retire, as before."""
    run = tmp_path / "run"
    for qid, answer in (("q1", "donations"), ("q2", "votes"), ("q3", "filings")):
        _claim(run / "claims", qid, answer)
    (run / "questions.json").write_text(json.dumps([
        _question("q2a", maps_from="q2"), _question("Q10", maps_from="q1"),
        _question("q3.b_c-1", maps_from="q3")]))

    cli.remap(data=run, apply=True)

    assert {p.name for p in (run / "claims").iterdir()} == {
        "q2a.json", "Q10.json", "q3.b_c-1.json"}
    assert json.loads((run / "claims" / "q2a.json").read_text())["answer"] == "votes"
    retired = json.loads((run / "questions.json").read_text())
    assert [q.get("mapped_from") for q in retired] == ["q2", "q1", "q3"]
    out.truncate(0)
    out.seek(0)
    cli.remap(data=run)
    assert "nothing to remap" in out.getvalue()

