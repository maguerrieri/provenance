"""Question ids as claim loading sees them: each names one claim, whatever the disk."""

from __future__ import annotations

import json
import re

import pytest

from provenance.cli import load_claims


def _claim(qid: str) -> dict:
    return {"question_id": qid, "question": "?", "answer": "a", "confidence": "direct",
            "sources": []}


def _write(path, *qids):
    items = [_claim(q) for q in qids]
    path.write_text(json.dumps(items[0] if len(items) == 1 else items))


@pytest.mark.parametrize("layout", ["two-files", "one-file", "own-names"])
def test_two_ids_differing_only_in_case_are_refused_at_load(tmp_path, layout):
    """Claims Q1 and q1 both loaded, since the duplicate check compared ids exactly. On a
    case-insensitive disk (macOS's default) their verdict shards Q1.json and q1.json are one
    file: `provenance judge` for either wrote into it, `provenance build` applied its verdicts to both claims,
    and a re-home could plan both as destinations, the second write replacing the first with no
    error and no archive entry. So loading refuses the pair on every disk, naming both files.

    The refusal doesn't ask the disk, so there is no case folding to simulate: the layouts that
    both kinds of disk can hold run everywhere, and the one only a case-sensitive disk can hold
    (Q1.json beside q1.json, as a Linux checkout has them) runs where the disk allows it."""
    claims = tmp_path / "claims"
    claims.mkdir()
    if layout == "two-files":
        _write(claims / "Q1.json", "Q1")
        _write(claims / "draft-q1.json", "q1")
        names = ("Q1.json", "draft-q1.json")
    elif layout == "one-file":
        _write(claims / "q1.json", "q1", "Q1")
        names = ("q1.json",)
    else:
        _write(claims / "Q1.json", "Q1")
        if (claims / "q1.json").exists():   # asked of the directory under test
            pytest.skip("this disk opens Q1.json and q1.json as one file")
        _write(claims / "q1.json", "q1")
        names = ("Q1.json", "q1.json")
    with pytest.raises(ValueError, match="differ only in case") as exc:
        load_claims(claims)
    msg = str(exc.value)
    for name in names:
        assert name in msg, msg
    assert "'Q1'" in msg and "'q1'" in msg, msg


def test_an_exact_duplicate_is_still_reported_as_a_duplicate(tmp_path):
    """The case check shares the duplicate check's lookup, and must not swallow it: a stale
    copy under another filename is a different fix (delete it) from a case-only pair."""
    _write(tmp_path / "q1.json", "q1")
    _write(tmp_path / "draft-q1.json", "q1")
    with pytest.raises(ValueError, match="duplicate question_id 'q1'"):
        load_claims(tmp_path)


def test_ids_that_differ_in_more_than_case_still_load(tmp_path):
    """Only a case-only difference is refused: Q1 beside q2, q1a and q10 is an ordinary run."""
    _write(tmp_path / "Q1.json", "Q1")
    _write(tmp_path / "q2.json", "q2")
    _write(tmp_path / "q1a.json", "q1a")
    _write(tmp_path / "q10.json", "q10")
    assert [c.question_id for c in load_claims(tmp_path)] == ["Q1", "q1a", "q2", "q10"]


def test_every_command_stops_on_the_pair_with_both_files_named(tmp_path, monkeypatch):
    """Every command loads claims through one loader, so the refusal reaches them all as a
    readable message and exit 1, not a traceback. `provenance status` stands in for them."""
    from typer.testing import CliRunner

    from provenance import cli

    claims = tmp_path / "claims"
    claims.mkdir()
    _write(claims / "Q1.json", "Q1")
    _write(claims / "draft-q1.json", "q1")
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(cli.con, "width", 10_000)   # rich folds a long tmp path at 80 columns
    res = CliRunner().invoke(cli.app, ["status", "--data", str(tmp_path)])
    # styled segments when FORCE_COLOR is set
    out = re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))
    assert res.exit_code == 1, out
    assert "differ only in case" in out and "Q1.json" in out and "draft-q1.json" in out, out
