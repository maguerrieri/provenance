"""`vg remap` moves every `derives_from` that names a claim along with the claim.

`check_inputs()` reads a conclusion's inputs by question id. A remap that moved claims and left
those ids alone pointed a conclusion at whatever claim took the id next — ids overlap in every
renumbering remap handles — and rendered it green on an input nobody checked.

Every name, host and figure here is made up.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

from vgpipe import cli, judgments
from vgpipe.models import Claim, PageCache, Source
from vgpipe.normalize import context_window
from vgpipe.verify import check_inputs

MARK = " (dropped by remap)"


def _question(qid, maps_from=None):
    q = {"id": qid, "text": f"new {qid}", "claim_type": "mechanical"}
    if maps_from:
        q["maps_from"] = maps_from
    return q


def _write_questions(root, questions):
    (root / "questions.json").write_text(json.dumps(questions, indent=1))


def _write_claim(root, qid, derives_from=(), **extra):
    (root / "claims").mkdir(parents=True, exist_ok=True)
    body = {"question_id": qid, "question": f"old {qid}", "answer": f"answer {qid}",
            "sources": [], "derives_from": list(derives_from), **extra}
    (root / "claims" / f"{qid}.json").write_text(json.dumps(body))


def _inputs(root):
    """Each claim file's derives_from, by the id its claim carries."""
    out = {}
    for p in sorted((root / "claims").glob("*.json")):
        raw = json.loads(p.read_text())
        out[raw["question_id"]] = raw.get("derives_from", [])
    return out


def _snapshot(root):
    return {p.name: p.read_bytes() for p in sorted((root / "claims").iterdir())}


def _said(capsys):
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())


def _retired_run(root, claims):
    """A run that has already been through one migration (identity pairs), so its claims sit on
    the current ids and a new pair moves only what it names. `claims` is {qid: derives_from},
    written after that migration, so every entry names a current id."""
    for qid in claims:
        _write_claim(root, qid)
    _write_questions(root, [_question(q, q) for q in claims])
    cli.remap(data=root, apply=True)
    for qid, inputs in claims.items():
        p = root / "claims" / f"{qid}.json"
        p.write_text(json.dumps({**json.loads(p.read_text()), "derives_from": list(inputs)}))
    return json.loads((root / "questions.json").read_text())


# --- the false green -------------------------------------------------------------------------

def _page(url, text):
    return PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=text, fetched_at=datetime.now(UTC))


def _genuine(qid):
    """A source that reproduces on its cached page, stamped as `vg verify` would stamp it."""
    url = f"https://ledger.example/{qid}"
    snippet = f"the council approved item {qid} on a split vote"
    page = _page(url, f"At Tuesday's meeting {snippet}, after an hour of comment.\n")
    s = Source(url=url, publisher="Example Ledger", author="A. Writer", date="2030-03-02",
               source_type="bylined_journalism", snippet=snippet)
    start = page.text.index(snippet)
    excerpt, rs, re_ = context_window(page.text, start, start + len(snippet))
    s.verification.status = "verified"
    s.verification.match_count = 1
    s.verification.matched_offset = start
    s.verification.context = excerpt
    s.verification.context_offset = (rs, re_)
    return s, page


def _build(root, monkeypatch, pages):
    """Run `vg build` over the claim files on disk; return the claims as they reached render."""
    for where in ("vgpipe.verify.load_cached", "vgpipe.fetch.load_cached"):
        monkeypatch.setattr(where, lambda _root, url: pages.get(url))
    rendered = {}

    def _render(claims, out, title, **_kw):
        rendered.update((c.question_id, Claim.model_validate_json(c.model_dump_json()))
                        for c in claims)
        return "review.html", "review.js"

    monkeypatch.setattr(cli, "render", _render)
    cli.build(data=root)
    return rendered


def _false_green_run(root):
    """q1 reasons from q2, whose claim a verifier rejected; q3's claim is verified. The mapping
    moves q2's claim to q4 and q3's onto q2, so the id q1 names changes hands."""
    pages = {}
    for qid, verdict in (("q1", "supports"), ("q2", "topic_only"), ("q3", "supports")):
        s, page = _genuine(qid)
        pages[s.url] = page
        c = Claim(question_id=qid, question=f"old {qid}", answer=f"answer {qid}", sources=[s],
                  derives_from=["q2"] if qid == "q1" else [])
        (root / "claims").mkdir(parents=True, exist_ok=True)
        (root / "claims" / f"{qid}.json").write_text(c.model_dump_json())
        judgments.record(root, qid, s.sid, verdict, f"judged for old {qid}")
    _write_questions(root, [_question("q1", "q1"), _question("q2", "q3"),
                            _question("q4", "q2")])
    return pages


def test_a_derived_claim_still_reads_its_real_input_after_a_renumbering(tmp_path, monkeypatch):
    """The issue's repro. Left as "q2", q1's input read the claim that moved onto q2 (the old
    q3, verified), so q1 rendered verified while its real input sat at q4 in human_review."""
    pages = _false_green_run(tmp_path)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q4"]

    out = _build(tmp_path, monkeypatch, pages)
    assert out["q2"].status == "verified", "the old q3's claim, now on q2"
    assert out["q4"].status == "human_review", "the old q2's claim, now on q4"
    assert out["q1"].unmet_inputs == ["q4 (human_review)"]
    assert out["q1"].status == "human_review"


def test_the_dry_run_lists_each_rewrite_and_writes_nothing(tmp_path, capsys):
    _false_green_run(tmp_path)
    before = _snapshot(tmp_path)
    capsys.readouterr()
    cli.remap(data=tmp_path)
    said = _said(capsys)
    assert "q1: q2 → q4" in said, said
    assert _snapshot(tmp_path) == before


# --- claims that stay put ----------------------------------------------------------------------

def test_a_claim_that_stays_put_follows_an_input_that_moves(tmp_path, capsys):
    """q1 keeps its id and is not moved, so its file is rewritten where it is, inside the
    transaction; a claim whose inputs don't move is not touched at all."""
    questions = _retired_run(tmp_path, {"q1": ["q2"], "q2": [], "q3": ["q1"]})
    untouched = (tmp_path / "claims" / "q3.json").read_bytes()
    questions.append(_question("q5", "q2"))
    _write_questions(tmp_path, questions)
    capsys.readouterr()
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path) == {"q1": ["q5"], "q3": ["q1"], "q5": []}
    assert (tmp_path / "claims" / "q3.json").read_bytes() == untouched
    assert "q1: q2 → q5" in _said(capsys)


def test_a_claim_rewritten_in_place_keeps_the_format_it_was_saved_in(tmp_path):
    """vg verify writes claim files through save_claims(). Reformatted wholesale, a tracked file's
    diff would bury the one entry that moved."""
    questions = _retired_run(tmp_path, {"q1": [], "q2": []})
    kept = Claim(question_id="q1", question="old q1", answer="Sí — the café's lease",
                 derives_from=["q2", "q1"])
    cli.save_claims([kept], tmp_path / "claims")
    questions.append(_question("q5", "q2"))
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    expected = kept.model_copy(update={"derives_from": ["q5", "q1"]}).model_dump_json(indent=2)
    assert (tmp_path / "claims" / "q1.json").read_text(encoding="utf-8") == expected


def test_every_claim_in_a_file_of_several_follows_its_inputs(tmp_path):
    """A claim file may hold a list. Both claims in it name an input that moves, and finding the
    first one changed must not stop the second from being rewritten."""
    questions = _retired_run(tmp_path, {"q1": [], "q2": []})
    (tmp_path / "claims" / "bundle.json").write_text(json.dumps([
        {"question_id": "q8", "question": "old q8", "answer": "a", "derives_from": ["q2"]},
        {"question_id": "q9", "question": "old q9", "answer": "a", "derives_from": ["q1", "q2"]},
    ]))
    questions.append(_question("q5", "q2"))
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    bundle = json.loads((tmp_path / "claims" / "bundle.json").read_text())
    assert [c["derives_from"] for c in bundle] == [["q5"], ["q1", "q5"]]


def test_a_claim_that_moves_takes_its_rewritten_inputs_with_it(tmp_path):
    """Both ends move: q1's claim goes to q7 and names q2's claim, which goes to q8. A claim that
    names itself still does after the move."""
    for qid, inputs in (("q1", ["q2", "q1"]), ("q2", [])):
        _write_claim(tmp_path, qid, inputs)
    _write_questions(tmp_path, [_question("q7", "q1"), _question("q8", "q2")])
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path) == {"q7": ["q8", "q7"], "q8": []}


def test_an_input_filed_under_another_name_follows_the_claim_it_names(tmp_path):
    """derives_from names a claim by the id it carries, not by its file name, and remap moves a
    claim by the id it carries too."""
    _write_claim(tmp_path, "q1", ["q2"])
    (tmp_path / "claims" / "stray-name.json").write_text(json.dumps(
        {"question_id": "q2", "question": "old q2", "answer": "a", "sources": []}))
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q6", "stray-name")])
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q6"]


# --- inputs the mapping cannot carry -----------------------------------------------------------

def test_an_input_whose_claim_is_archived_never_reads_the_claim_that_takes_its_id(tmp_path,
                                                                                  capsys):
    """q1 names q3, a claim nothing maps to. --archive-stranded archives it and q2's claim moves
    onto q3. Left as "q3", q1 read the new occupant. It now names the archived claim's old id in
    a form no question id can take, so it reads as missing, whatever lands on q3 later."""
    for qid, inputs in (("q1", ["q3"]), ("q2", []), ("q3", [])):
        _write_claim(tmp_path, qid, inputs)
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q3", "q2")])
    capsys.readouterr()
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    said = _said(capsys)
    assert _inputs(tmp_path) == {"q1": ["q3" + MARK], "q3": []}
    assert "q1: q3 → q3" + MARK in said and "archived" in said, said

    claims = cli.load_claims(tmp_path / "claims")
    check_inputs(claims)
    q1 = next(c for c in claims if c.question_id == "q1")
    assert q1.unmet_inputs == ["q3" + MARK + " (missing)"]


def test_the_dry_run_names_an_input_it_cannot_carry(tmp_path, capsys):
    _write_claim(tmp_path, "q1", ["q4"])
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q4")])
    before = _snapshot(tmp_path)
    capsys.readouterr()
    cli.remap(data=tmp_path)
    said = _said(capsys)
    assert "q1: q4 → q4" + MARK in said, said
    assert "missing" in said
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("new_pairs, inputs, expected", [
    # The question q9 moved to q5 with no research yet: its research, when it comes, is q5's.
    pytest.param([("q7", "q2"), ("q5", "q9")], ["q9"], ["q5"], id="question-moved-unresearched"),
    # q6 now names the question moved from q2, and the old q6 is not in the template.
    pytest.param([("q6", "q2")], ["q6"], ["q6" + MARK], id="id-taken-by-another-question"),
    # The mapping says nothing about q4, a question that keeps its id: it still means q4.
    pytest.param([("q7", "q2")], ["q4"], ["q4"], id="unresearched-question-keeps-its-id"),
    # No question has q88 — dropped from the template, or never in it. Left as it is, a question
    # added at q88 later would satisfy q1 with research on something else.
    pytest.param([("q7", "q2")], ["q88"], ["q88" + MARK], id="id-no-question-has"),
])
def test_an_input_no_claim_held_follows_its_question(tmp_path, new_pairs, inputs, expected):
    """Every case moves q2's claim, so the apply really migrates the run."""
    questions = _retired_run(tmp_path, {"q1": inputs, "q2": []})
    questions.append(_question("q4"))
    questions += [_question(new, old) for new, old in new_pairs]
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == expected


def test_an_apply_that_moves_no_claim_rewrites_no_input(tmp_path):
    """With no claim to move, the apply retires nothing and the run stays in its old id space,
    so the mapping is still pending. Following it now would follow it again on the next apply,
    and q9 → q5 would become q5 → dropped."""
    questions = _retired_run(tmp_path, {"q1": ["q9"]})
    questions.append(_question("q5", "q9"))
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q9"]
    assert any(q.get("maps_from") == "q9" for q in
               json.loads((tmp_path / "questions.json").read_text())), "still pending"


def test_in_a_first_migration_an_input_no_pair_maps_from_reads_as_missing(tmp_path):
    """Until a run has migrated once, its ids are the old ones, and a question no pair maps from
    is a new question. q1 names q4, an old id the template dropped: the new q4 is another
    question, and research landing on it would satisfy q1."""
    _write_claim(tmp_path, "q1", ["q4"])
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q4")])
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q4" + MARK]


def test_an_input_moved_onto_an_id_a_stranded_claim_holds_reads_as_missing(tmp_path):
    """Question q9, unresearched, moves to q5 — but q5.json holds a claim nothing maps to, left in
    place. Pointed at q5, q1 would read that claim, which answers another question."""
    questions = _retired_run(tmp_path, {"q1": ["q9"], "q2": []})
    _write_claim(tmp_path, "q5")
    questions += [_question("q5", "q9"), _question("q7", "q2")]
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q9" + MARK]


def test_the_reason_for_a_drop_does_not_blame_a_claim_the_mapping_moved(tmp_path, capsys):
    """q3.json holds a claim carrying q9, and q2 maps from q3, so that claim moves to q2. An
    entry naming "q3" named no claim (none carries q3) and its question's new id will hold a
    claim it did not name, so it drops — but not as "a claim nothing maps to": one does."""
    _write_claim(tmp_path, "q1", ["q3"])
    (tmp_path / "claims" / "q3.json").write_text(json.dumps(
        {"question_id": "q9", "question": "old q9", "answer": "a", "sources": []}))
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q2", "q3")])
    capsys.readouterr()
    cli.remap(data=tmp_path, apply=True)
    said = _said(capsys)
    assert _inputs(tmp_path)["q1"] == ["q3" + MARK]
    assert "its question moved to q2, which will hold another claim" in said, said
    assert "nothing maps to" not in said


def test_a_stranded_claim_left_in_place_is_still_its_input(tmp_path):
    """Not archived, the claim stays on its id and nothing can move onto it (that is a
    collision), so the entry naming it is left alone."""
    questions = _retired_run(tmp_path, {"q1": ["q7"], "q2": []})
    _write_claim(tmp_path, "q7")
    questions.append(_question("q5", "q2"))
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q7"]


def test_a_dropped_input_and_a_non_id_entry_are_left_as_they_are(tmp_path):
    """Neither can name a claim, so neither is rewritten again: a second remap that moves the id
    the marker quotes must not move the marker."""
    questions = _retired_run(tmp_path, {"q1": ["q3" + MARK, "not an id"], "q3": []})
    questions.append(_question("q5", "q3"))
    _write_questions(tmp_path, questions)
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path)["q1"] == ["q3" + MARK, "not an id"]


# --- the transaction -------------------------------------------------------------------------

def _two_rewrites(root):
    """q1 stays put and is rewritten in place; q2's claim moves to q5; q3 moves onto q6 and
    names q2. So one apply writes a moved file and rewrites a kept one."""
    questions = _retired_run(root, {"q1": ["q2"], "q2": [], "q3": ["q2"]})
    questions += [_question("q5", "q2"), _question("q6", "q3")]
    _write_questions(root, questions)


def test_a_remap_that_fails_rewriting_an_input_puts_every_claim_back(tmp_path, monkeypatch):
    _two_rewrites(tmp_path)
    before = _snapshot(tmp_path)
    questions = (tmp_path / "questions.json").read_bytes()
    real = Path.write_text

    def fails(self, *a, **kw):
        if self.parent.name == "claims" and self.name == "q1.json":
            raise OSError("disk full")      # the moved files were already written
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", fails)
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True)
    monkeypatch.undo()
    assert _snapshot(tmp_path) == before
    assert (tmp_path / "questions.json").read_bytes() == questions
    assert not judgments.backup_dir(tmp_path).exists()


def test_a_remap_killed_after_rewriting_inputs_rolls_them_back_with_the_claims(tmp_path,
                                                                              monkeypatch):
    _two_rewrites(tmp_path)
    before = _snapshot(tmp_path)
    real_replace = judgments.os.replace

    def killed_at_commit(a, b):
        if Path(b).name == "judgments-backup.discard":
            raise KeyboardInterrupt
        real_replace(a, b)

    monkeypatch.setattr(judgments.os, "replace", killed_at_commit)
    with pytest.raises(KeyboardInterrupt):
        cli.remap(data=tmp_path, apply=True)
    monkeypatch.undo()
    assert _inputs(tmp_path)["q1"] == ["q5"], "the rewrite really had happened"

    cli.show_judgments(data=tmp_path, rollback=True)
    assert _snapshot(tmp_path) == before
    cli.remap(data=tmp_path, apply=True)
    assert _inputs(tmp_path) == {"q1": ["q5"], "q5": [], "q6": ["q5"]}


def test_every_rewritten_input_is_on_disk_before_the_commit(tmp_path, monkeypatch):
    import os

    _two_rewrites(tmp_path)
    events: list[tuple[str, int]] = []
    real_fsync, real_replace = os.fsync, judgments.os.replace

    def fsync(fd):
        events.append(("fsync", os.fstat(fd).st_ino))
        real_fsync(fd)

    def replace(a, b):
        if Path(b).name == "judgments-backup.discard":
            events.append(("commit", 0))
        real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(judgments.os, "replace", replace)
    cli.remap(data=tmp_path, apply=True)

    commit = events.index(("commit", 0))
    durable = {ino for kind, ino in events[:commit] if kind == "fsync"}
    written = [tmp_path / "claims" / n for n in ("q1.json", "q5.json", "q6.json")]
    assert [p.name for p in written if p.stat().st_ino not in durable] == []


# --- a run an older remap migrated -----------------------------------------------------------

def test_mark_applied_names_inputs_an_older_remap_left_on_old_ids(tmp_path, capsys):
    """An older remap moved the claims and not their inputs, and nothing shows which entries were
    fixed by hand since. --mark-applied moves nothing, so it lists each entry the mapping it
    retires moved away from, for a person to check."""
    _write_claim(tmp_path, "q4")
    _write_claim(tmp_path, "q1", ["q2"])
    _write_questions(tmp_path, [_question("q1", "q1"), _question("q4", "q2")])
    capsys.readouterr()
    cli.remap(data=tmp_path, mark_applied=True)
    said = _said(capsys)
    assert "q1 names q2" in said and "q4" in said, said
    assert _inputs(tmp_path)["q1"] == ["q2"], "listed, never rewritten"
