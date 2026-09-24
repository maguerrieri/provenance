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


@pytest.mark.parametrize("args", [
    ["remap"],
    ["remap", "--apply"],
    ["remap", "--apply", "--archive-stranded"],
    ["remap", "--mark-applied"],
    ["judgments", "--repair"],
    ["judgments", "--repair", "--moved", "q1:q2", "--gone", "q3"],
    ["judgments", "--moved", "q1:q2"],
    ["judgments", "--rollback"],
], ids=" ".join)
def test_a_retired_command_says_why_and_changes_nothing(tmp_path, args):
    """An old script or habit must meet the rule that replaced these commands, not "No such
    command" — and must not have anything moved, re-homed or rolled back on its behalf."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "judged before the retirement")
    before = _tree(data)
    code, out = _vg(*args, "--data", run)
    assert code == 1, out
    assert "is retired: question ids are stable and never reused" in out, out
    assert "A split or reworded question gets a new id, and the old id is retired" in out, out
    assert _tree(data) == before


def test_retired_commands_are_not_offered(tmp_path):
    _, out = _vg("--help")
    assert "remap" not in out, out
    _, out = _vg("judgments", "--help")
    assert not re.search(r"--(repair|rollback|moved|gone)\b", out), out


def test_a_backup_an_interrupted_re_home_left_still_stops_every_reader(tmp_path):
    """No re-home runs any more, but one an older version was running when it died left
    judgments-backup/ behind, with shards that may be half-rewritten. Reading them as the
    verdicts would render whatever is missing as unreviewed, so every reader still refuses,
    and names the checkout whose `--rollback` undoes it. `--rollback` here says the same rather
    than that it is retired, since that is what the operator asking for it needs."""
    data, run, _ = _legacy_run(tmp_path)
    judgments.record(run, "q1", _source().sid, "supports", "half-rewritten")
    (judgments.backup_dir(run) / "q1.json").parent.mkdir()
    (judgments.backup_dir(run) / "q1.json").write_text("[]")
    before = _tree(data)
    for args in (["judgments"], ["judgments", "--rollback"], ["judgments", "--repair"],
                 ["build"], ["status"], ["verify"]):
        code, out = _vg(*args, "--data", run)
        assert code == 1, (args, out)
        assert "was interrupted, and its shards may be half-rewritten" in out, (args, out)
        assert f"run `vg judgments --rollback --data {run}` from a checkout" in out, (args, out)
        assert "git log -1 -S'def rollback(' -- src/vgpipe/judgments.py" in out, (args, out)
        assert "is retired" not in out, (args, out)
    assert _tree(data) == before
