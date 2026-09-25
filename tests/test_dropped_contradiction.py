"""A `contradicts` verdict does not lapse when a retry drops its source.

A verdict is keyed by source id, so when a retry rewrites a claim without the source a verifier
judged `contradicts`, the verdict stops matching anything the claim cites. It used to lapse there
like any other: the conflict line went, and the claim rendered `verified` on the sources that
agree, with no trace that a record argued against it. Dropping the source takes the disagreement
off the review page without resolving it. So the verdict holds its claim in review until the
source is cited again or a human clears it on the record.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vgpipe import cli, judgments
from vgpipe.conflicts import detect
from vgpipe.fetch import cache_path
from vgpipe.models import (EXTRACTOR_VERSION, Claim, DroppedContradiction, PageCache, Source,
                           strip_machine_fields)
from vgpipe.verify import check_corroboration, check_inputs

LEDGER = "https://daily-ledger.example/levy-vote"
WEEKLY = "https://harbor-weekly.example/levy-vote"
GAZETTE = "https://tide-gazette.example/levy-vote"
SNIPPET = "voted against the harbor levy twice"
STORY = f"At both hearings the councilmember {SNIPPET}, citing the port budget.\n"
NOTE = "says the councilmember voted for the levy"


def _source(url: str, publisher: str) -> Source:
    return Source(url=url, publisher=publisher, author="R. Writer", date="2026-05-14",
                  source_type="bylined_journalism", snippet=SNIPPET)


def _ledger() -> Source:
    return _source(LEDGER, "Daily Ledger")


def _weekly() -> Source:
    return _source(WEEKLY, "Harbor Weekly")


def _gazette() -> Source:
    return _source(GAZETTE, "Tide Gazette")


def _cache(root: Path, url: str) -> None:
    page = PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=STORY, fetched_at=datetime.now(UTC) - timedelta(days=1),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(root, url).write_text(page.model_dump_json())


def _vg(*args, input: str | None = None) -> tuple[int, str]:
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args], input=input)
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _write_claim(data: Path, *sources: Source, qid: str = "q1") -> None:
    (data / "claims").mkdir(parents=True, exist_ok=True)
    (data / "claims" / f"{qid}.json").write_text(
        Claim(question_id=qid, question="How did the councilmember vote on the levy?",
              answer="Against it, twice.", sources=list(sources)).model_dump_json())


def _built(data: Path) -> dict:
    code, out = _vg("build", "--data", data)
    assert code == 0, out
    [claim] = json.loads((data / "out" / "claims.json").read_text())
    return claim


def _judge(data: Path, sid: str, verdict: str, note: str, qid: str = "q1") -> None:
    code, out = _vg("judge", qid, sid, verdict, "--note", note, "--data", data)
    assert code == 0, out


def _retried_run(tmp_path: Path) -> Path:
    """The issue's repro, up to its last build: q1 cites the Ledger (judged `supports`) and the
    Weekly (judged `contradicts`); a retry for some other failure rewrites it citing the Ledger
    and the Gazette, which agrees; the Gazette is judged `supports`."""
    data = tmp_path / "data"
    for url in (LEDGER, WEEKLY, GAZETTE):
        _cache(data, url)
    _write_claim(data, _ledger(), _weekly())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    _judge(data, _ledger().sid, "supports", "states the vote")
    _judge(data, _weekly().sid, "contradicts", NOTE)
    assert _built(data)["status"] == "human_review"

    _write_claim(data, _ledger(), _gazette())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    _judge(data, _gazette().sid, "supports", "states the vote too")
    return data


def test_a_contradiction_a_retry_drops_still_holds_the_claim(tmp_path):
    """The regression: every cited source judged `supports`, and the contradicting one gone
    from the claim, so it rendered `verified` with no trace of the contradiction."""
    data = _retried_run(tmp_path)
    weekly = _weekly().sid

    built = _built(data)
    assert built["corroboration_ok"] is True, "the sources that agree still corroborate"
    assert built["status"] == "human_review"
    assert built["dropped_contradictions"] == [
        {"sid": weekly, "note": NOTE, "judged_at": built["dropped_contradictions"][0]["judged_at"]}]
    [line] = built["conflicts"]
    assert f"no longer cites as it did (source id {weekly}, judged " in line
    assert f"contradicts the claim: {NOTE}. " in line
    assert line.endswith(f"at a terminal with `vg clear-contradiction q1 {weekly}`")
    assert "Conflicts (1)" in (data / "out" / "review.html").read_text()

    code, out = _vg("status", "--data", data)
    assert code == 0, out
    row = next(ln for ln in out.splitlines() if ln.split()[:1] == ["q1"])
    assert "human_review" in row and row.split()[-1] == "1", row

    # Not in the gate: no verifier can close it, since nothing cites the source to judge.
    code, out = _vg("judgments", "--data", data)
    assert code == 0, out
    assert "0 of 2 cited source(s) need a verdict" in out
    assert f"1 contradicts verdict(s) are on a source their claim no longer cites (q1/{weekly})" \
        in out
    assert "correctly lapsed" not in out


def test_citing_the_source_again_applies_the_verdict_as_usual(tmp_path):
    """The verdict is the same one: back on a cited source, it is listed as that source's."""
    data = _retried_run(tmp_path)
    _write_claim(data, _ledger(), _gazette(), _weekly())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out

    built = _built(data)
    assert built["status"] == "human_review"
    assert built["dropped_contradictions"] == []
    assert built["conflicts"] == [f"a verifier judged Harbor Weekly ({WEEKLY}) contradicts the "
                                  f"claim: {NOTE}"]


def test_only_a_person_at_a_terminal_can_clear_it(tmp_path):
    """"Clearing is the human's call" was a sentence, and the command is named wherever the
    contradiction is: in `vg judgments`, which agents run, and in the conflicts. Agents run
    commands with no terminal, so the command asks for its reason at one and refuses without."""
    data = _retried_run(tmp_path)
    weekly = _weekly().sid
    before = judgments.path_for(data, "q1").read_bytes()

    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data,
                    input="the verifier was wrong\n")
    assert code == 1 and "a person's decision" in out and "Nothing was cleared" in out
    assert judgments.path_for(data, "q1").read_bytes() == before
    assert not judgments.archive_dir(data).exists()
    assert _built(data)["status"] == "human_review"


def test_a_person_clears_it_on_the_record(tmp_path, monkeypatch):
    """Dropping the source can be right — the verifier was wrong, or the claim was re-scoped —
    so a person can clear it. The verdict is kept, with the reason, and the claim is released."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    weekly = _weekly().sid
    shard = judgments.path_for(data, "q1")
    before = shard.read_bytes()

    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data, input="  \n")
    assert code == 1 and "a reason is required" in out and "Nothing was cleared" in out
    assert f"q1/{weekly}: a verifier judged it contradicts the claim" in out and NOTE in out
    assert shard.read_bytes() == before
    assert not judgments.archive_dir(data).exists()

    reason = "the Weekly story was about an earlier levy, not this one"
    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data, input=f"{reason}\n")
    assert code == 0, out
    assert f"cleared the contradicts verdict on q1/{weekly}" in out

    assert set(judgments.load(data, "q1")) == {_ledger().sid, _gazette().sid}
    (kept,) = judgments.archive_dir(data).iterdir()
    [archived] = json.loads((kept / "q1.json").read_text())
    assert (archived["sid"], archived["verdict"], archived["note"]) == (weekly, "contradicts",
                                                                        NOTE)
    assert re.fullmatch(rf"q1/{weekly} cleared \S+: {re.escape(reason)}\n",
                        (kept / "CLEARED").read_text())

    built = _built(data)
    assert built["status"] == "verified"
    assert built["conflicts"] == [] and built["dropped_contradictions"] == []


def test_clearing_refuses_anything_but_a_dropped_contradiction(tmp_path, monkeypatch):
    """It is for a verdict that holds a claim only because its source was dropped. A live one
    is the claim's own evidence disagreeing; a lapsed `supports` holds nothing. Each is refused
    before the reason is asked for."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    lapsed = "0123456789ab"
    judgments.record(data, "q1", lapsed, "supports", "about a citation since changed")
    before = judgments.path_for(data, "q1").read_bytes()

    def refused(*args) -> str:
        code, out = _vg("clear-contradiction", *args, "--data", data, input="because\n")
        assert code == 1 and "Nothing was cleared" in out and "Why" not in out, out
        return " ".join(out.split())

    assert "claim q1 still cites" in refused("q1", _ledger().sid)
    assert f"q1 has no contradicts verdict on {lapsed} to clear" in refused("q1", lapsed)
    assert "no contradicts verdict on ffffffffffff" in refused("q1", "ffffffffffff")
    assert "no claim has question id q2" in refused("q2", _weekly().sid)
    assert "refusing question id '../claims/q1'" in refused("../claims/q1", _weekly().sid)
    assert judgments.path_for(data, "q1").read_bytes() == before
    assert not judgments.archive_dir(data).exists()

    # clear() holds itself to the same rules, under the lock.
    with pytest.raises(ValueError, match="is supports, not contradicts"):
        judgments.clear(data, "q1", lapsed, "because")
    with pytest.raises(ValueError, match="needs a reason"):
        judgments.clear(data, "q1", _weekly().sid, " ")
    assert judgments.path_for(data, "q1").read_bytes() == before


def test_a_clearance_is_never_half_done(tmp_path, monkeypatch):
    """The archive is complete before the shard is rewritten. If the rewrite fails, the shard
    still holds the verdict and the archive is taken back out, so a re-run leaves one copy:
    never in neither, and not in two."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    weekly = _weekly().sid

    def fail(*_a, **_k):
        raise OSError("disk full")

    with monkeypatch.context() as m, pytest.raises(OSError):
        m.setattr(judgments, "_write", fail)
        judgments.clear(data, "q1", weekly, "re-scoped")
    assert judgments.load(data, "q1")[weekly].verdict == "contradicts"
    assert list(judgments.archive_dir(data).iterdir()) == []
    assert _built(data)["status"] == "human_review"

    # Through the command, a failure is a refusal, not a traceback.
    with monkeypatch.context() as m:
        m.setattr(judgments, "_write", fail)
        code, out = _vg("clear-contradiction", "q1", weekly, "--data", data, input="re-scoped\n")
    assert code == 1 and "disk full. Nothing was cleared" in out, out
    assert list(judgments.archive_dir(data).iterdir()) == []

    judgments.clear(data, "q1", weekly, "re-scoped")    # a re-run finishes it
    assert weekly not in judgments.load(data, "q1")
    assert len(list(judgments.archive_dir(data).iterdir())) == 1
    assert _built(data)["status"] == "verified"


def test_a_clearance_that_fails_after_placing_its_archive_takes_it_back(tmp_path, monkeypatch):
    """The archive is renamed into place and then made durable. A failure there leaves the shard
    untouched, so the placed archive goes too, not only the half-built one it was renamed from:
    a verdict still live must not also sit in the archive as cleared."""
    data = _retried_run(tmp_path)
    weekly = _weekly().sid
    real = judgments._fsync_dir

    def fail_above(d: Path) -> None:
        if d == judgments.archive_dir(data):
            raise OSError("I/O error")
        real(d)

    with monkeypatch.context() as m, pytest.raises(OSError, match="I/O error"):
        m.setattr(judgments, "_fsync_dir", fail_above)
        judgments.clear(data, "q1", weekly, "re-scoped")
    assert judgments.load(data, "q1")[weekly].verdict == "contradicts"
    assert list(judgments.archive_dir(data).iterdir()) == []


def test_a_verdict_judged_again_while_the_prompt_waits_is_not_cleared(tmp_path, monkeypatch):
    """The reason is for the verdict the person read. A retry that cites the source, a verifier
    judging it again, and a retry that drops it again, all while the prompt waits, leave a
    dropped contradiction on the same source that nobody was shown: it is refused."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    weekly = _weekly().sid

    def meanwhile(*_a, **_k) -> str:
        entries = json.loads(judgments.path_for(data, "q1").read_text())
        for e in entries:
            if e["sid"] == weekly:
                e["note"], e["judged_at"] = "a later reading", "2031-01-02T03:04:05+00:00"
        judgments.path_for(data, "q1").write_text(json.dumps(entries))
        return "the verifier was wrong"

    monkeypatch.setattr(cli.typer, "prompt", meanwhile)
    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data)
    assert code == 1 and "was judged again (at 2031-01-02T03:04:05+00:00) after it was " \
                         "shown" in " ".join(out.split()), out
    assert judgments.load(data, "q1")[weekly].note == "a later reading"
    assert not judgments.archive_dir(data).exists()


def test_vg_judgments_names_every_held_verdict(tmp_path):
    """The line is the only list of them, so none is cut off."""
    data = tmp_path / "data"
    _cache(data, LEDGER)
    _write_claim(data, _ledger())
    sids = [f"{n:012x}" for n in range(1, 11)]
    for sid in sids:
        judgments.record(data, "q1", sid, "contradicts", NOTE)
    code, out = _vg("judgments", "--data", data)
    flat = " ".join(out.split())
    assert "no cache" not in flat and "10 contradicts verdict(s)" in flat, out
    assert f"({', '.join(f'q1/{sid}' for sid in sids)})" in flat, out


def test_a_source_cited_again_while_the_prompt_waits_is_not_cleared(tmp_path, monkeypatch):
    """The check that the claim no longer cites the source runs again after the person answers:
    a retry can put the source back meanwhile, and then its verdict is live again."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    weekly = _weekly().sid
    before = judgments.path_for(data, "q1").read_bytes()

    def meanwhile(*_a, **_k) -> str:
        _write_claim(data, _ledger(), _gazette(), _weekly())
        return "the verifier was wrong"

    monkeypatch.setattr(cli.typer, "prompt", meanwhile)
    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data)
    assert code == 1 and "claim q1 still cites" in out, out
    assert judgments.path_for(data, "q1").read_bytes() == before
    assert not judgments.archive_dir(data).exists()


def test_clearing_says_when_another_claim_cites_the_source(tmp_path, monkeypatch):
    """Another claim citing the source is named before the person gives a reason, and keeps its
    own verdict on it: clearing touches q1's shard alone. And a typo'd id is a typo, not a
    shard no claim has."""
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    data = _retried_run(tmp_path)
    weekly = _weekly().sid
    _write_claim(data, _weekly(), qid="q2")
    judgments.record(data, "q2", weekly, "supports", "states the vote")
    theirs = judgments.path_for(data, "q2").read_bytes()

    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data, input="\n")
    assert code == 1, out
    flat = " ".join(out.split())
    assert "q2 also cites this source, each judged by the verdicts in its own shard: clearing " \
           "this one leaves those as they are." in flat

    code, out = _vg("clear-contradiction", "q1", weekly, "--data", data,
                    input="the claim was narrowed\n")
    assert code == 0, out
    assert weekly not in judgments.load(data, "q1")
    assert judgments.path_for(data, "q2").read_bytes() == theirs

    code, out = _vg("clear-contradiction", "q01", weekly, "--data", data, input="because\n")
    assert code == 1 and "did you mean q1?" in out and "names a shard" not in out, out


# --- the status, as every triage surface reads it ---


def _claim(*sources, qid="q1", **kw) -> Claim:
    for s in sources:
        s.verification.status = "verified"
        s.verification.support = "supports"
    return check_corroboration(Claim(question_id=qid, question="?", answer="a",
                                     sources=list(sources), **kw))


def test_the_claim_is_in_review_and_so_is_a_conclusion_drawn_from_it():
    held = _claim(_ledger(), dropped_contradictions=[DroppedContradiction(sid="0123456789ab")])
    assert held.corroboration_ok is True
    assert held.status == "human_review"
    # As with a cited one, an absence claim is no exception: the record it could not find is
    # what the verifier said is there.
    absent = Claim(question_id="q3", question="?", answer="none found", confidence="not_found",
                   dropped_contradictions=[DroppedContradiction(sid="0123456789ab")])
    assert absent.status == "human_review"

    conclusion = _claim(_gazette(), qid="q2", derives_from=["q1"])
    check_inputs([held, conclusion])
    assert conclusion.unmet_inputs == ["q1 (human_review)"]


def test_only_the_shard_says_a_contradiction_was_dropped(tmp_path):
    """Pipeline-owned: stripped from an agent's claim file, and rebuilt from the shard on every
    build, so one written into a claim file neither holds the claim nor survives."""
    raw = json.loads(Claim(question_id="q1", question="?", answer="a").model_dump_json())
    raw["dropped_contradictions"] = [{"sid": "0123456789ab"}]
    assert "dropped_contradictions" not in strip_machine_fields(raw)

    data = tmp_path / "data"
    _cache(data, LEDGER)
    _write_claim(data, _ledger())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    _judge(data, _ledger().sid, "supports", "states the vote")
    path = data / "claims" / "q1.json"
    raw = json.loads(path.read_text())
    raw["dropped_contradictions"] = [{"sid": "0123456789ab", "note": "typed in by hand"}]
    path.write_text(json.dumps(raw))

    built = _built(data)
    assert built["status"] == "verified"
    assert built["dropped_contradictions"] == [] and built["conflicts"] == []


def test_detect_lists_one_without_a_note_or_a_time():
    [c] = detect([_claim(_ledger(), dropped_contradictions=[
        DroppedContradiction(sid="0123456789ab")])])
    assert c.conflicts == [
        "a verifier judged a source this claim no longer cites as it did (source id "
        "0123456789ab) contradicts the claim. Changing the citation did not resolve that: the "
        "claim stays in review until the source is cited again, or a human clears it at a "
        "terminal with `vg clear-contradiction q1 0123456789ab`"]
