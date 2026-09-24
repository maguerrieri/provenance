"""A source judged `contradicts` sends its claim to a human, whatever the other sources say.

`topic_only` and `superseded` say a page does not support the claim, and another source can
still carry it: those sources are left out of the corroboration count and nothing more.
`contradicts` says the record argues against the claim. A supporting source beside it does not
settle that. It is a disagreement between the claim's own evidence, and only a human can
resolve it. So the claim is `human_review`, in the review filter, and its contradiction is
listed with the conflicts the review app says to resolve first.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from vgpipe import cli
from vgpipe.conflicts import detect
from vgpipe.fetch import cache_path
from vgpipe.models import EXTRACTOR_VERSION, Claim, PageCache, Source
from vgpipe.verify import check_corroboration, check_inputs

LEDGER = "https://daily-ledger.example/levy-vote"
WEEKLY = "https://harbor-weekly.example/levy-vote"
SNIPPET = "voted against the harbor levy twice"
STORY = f"At both hearings the councilmember {SNIPPET}, citing the port budget.\n"


def _source(url=LEDGER, publisher="Daily Ledger", status="verified", support="unreviewed",
            note=None):
    s = Source(url=url, publisher=publisher, author="R. Writer", date="2026-05-14",
               source_type="bylined_journalism", snippet=SNIPPET)
    s.verification.status = status
    s.verification.support = support
    s.verification.support_note = note
    s.paywall = status == "could_not_verify_paywall"
    return s


def _weekly(**kw):
    return _source(url=WEEKLY, publisher="Harbor Weekly", **kw)


def _claim(*sources, qid="q1", **kw):
    return check_corroboration(Claim(question_id=qid, question="?", answer="a",
                                     sources=list(sources), **kw))


def test_a_contradicted_claim_is_never_green_beside_a_supporting_source():
    """The regression: the contradicting source was left out of the count, one supporting
    document met the one required, and the claim rendered `verified`. The red badge sat on
    one source while the claim, the status counts and the review filter all said green."""
    c = _claim(_source(support="supports"), _weekly(support="contradicts"))
    assert c.corroboration_ok is True, "the supporting source alone still corroborates"
    assert c.status == "human_review"

    # Adversarial, with two supporting outlets beside it: still a disagreement to resolve.
    third = _source(url="https://tide-gazette.example/levy", publisher="Tide Gazette",
                    support="supports")
    adv = _claim(_source(support="supports"), third, _weekly(support="contradicts"),
                 claim_type="adversarial")
    assert adv.corroboration_ok is True
    assert adv.status == "human_review"


def test_a_contradicted_claim_is_never_behind_the_paywall_flag():
    """The paywall branch reads the evidence as the verified branch does, so it had the same
    hole: the yellow flag, which is also outside the review filter."""
    c = _claim(_weekly(support="contradicts"), _source(status="could_not_verify_paywall"))
    assert c.corroboration_ok is True
    assert c.status == "human_review"


def test_only_contradicts_does_this():
    """`topic_only` and `superseded` take a source's support away, and another source can
    supply it. They stay out of the count and nothing more."""
    for verdict in ("topic_only", "superseded"):
        beside_verified = _claim(_source(support="supports"), _weekly(support=verdict))
        assert beside_verified.status == "verified", verdict
        beside_paywall = _claim(_weekly(support=verdict),
                                _source(status="could_not_verify_paywall"))
        assert beside_paywall.status == "could_not_verify_paywall", verdict


def test_a_contradiction_outranks_pending_and_not_found():
    """`pending` means a verdict could still clear the claim, and none can clear this one:
    whatever the unjudged source turns out to say, the contradicting one still says it. It
    outranks `not_found` as a failed citation does. A contradicted absence claim says the
    record it could not find is there, and that must not sit under a muted badge."""
    unjudged = _claim(_source(), _weekly(support="contradicts"))
    assert unjudged.status == "human_review"
    unverified = _claim(_source(status="pending"), _weekly(support="contradicts"))
    assert unverified.status == "human_review"

    absent = _claim(_weekly(support="contradicts"), confidence="not_found")
    assert absent.status == "human_review"
    # A page that is only on the subject is what an absence claim's citation usually is.
    assert _claim(_weekly(support="topic_only"), confidence="not_found").status == "not_found"


def test_a_contradicted_input_is_an_unmet_one():
    """The status is what `derives_from` reads, so a conclusion drawn from a contradicted
    claim goes to review with it, which a line in the conflicts section alone would not do."""
    base = _claim(_source(support="supports"), _weekly(support="contradicts"))
    conclusion = _claim(_source(support="supports"), qid="q2", derives_from=["q1"])
    check_inputs([base, conclusion])
    assert conclusion.unmet_inputs == ["q1 (human_review)"]
    assert conclusion.status == "human_review"


def test_the_contradiction_is_listed_as_a_conflict():
    """Listed where the review app says to resolve things first, naming the source and the
    verifier's reason, so the reviewer sees why the claim is in review. A query citation has
    no snippet to compare, and its verdict is listed all the same."""
    [c] = detect([_claim(_source(support="supports"),
                         _weekly(support="contradicts", note="the story says he voted for it"))])
    assert c.conflicts == [f"a verifier judged Harbor Weekly ({WEEKLY}) contradicts the "
                           f"claim: the story says he voted for it"]

    no_snippet = _weekly(support="contradicts")
    no_snippet.snippet = ""
    assert detect([_claim(no_snippet)])[0].conflicts == [
        f"a verifier judged Harbor Weekly ({WEEKLY}) contradicts the claim"]

    unnamed = _weekly(support="contradicts")
    unnamed.publisher = " "
    assert detect([_claim(unnamed)])[0].conflicts == [
        f"a verifier judged {WEEKLY} contradicts the claim"]

    for verdict in ("supports", "topic_only", "superseded", "unreviewed"):
        assert detect([_claim(_weekly(support=verdict))])[0].conflicts == [], verdict


# --- end to end: only a recorded verdict counts, and build and status read it ---


def _cache(root: Path, url: str) -> None:
    page = PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=STORY, fetched_at=datetime.now(UTC) - timedelta(days=1),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(root, url).write_text(page.model_dump_json())


def _vg(*args) -> tuple[int, str]:
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _handed(data, qid: str, sid: str) -> list[str]:
    """`--context` and the token `vg handoff` prints beside `sid`, as a verifier passes it on."""
    code, out = _vg("handoff", qid, "--data", data)
    assert code == 0, out
    token = re.search(rf"sid {re.escape(sid)}\s+context token (\w+)", out)
    assert token, out
    return ["--context", token.group(1)]


def _built(data: Path) -> dict:
    code, out = _vg("build", "--data", data)
    assert code == 0, out
    [claim] = json.loads((data / "out" / "claims.json").read_text())
    return claim


def test_build_and_status_list_a_recorded_contradiction_and_only_that(tmp_path):
    """Conflicts are detected after the recorded verdicts are applied. They used to be detected
    first, on the claim file as loaded, whose `support` is exactly what must never render: a
    `contradicts` written into the file would have been listed, and a recorded one missed."""
    data = tmp_path / "data"
    _cache(data, LEDGER)
    _cache(data, WEEKLY)
    ledger, weekly = _source(status="pending"), _weekly(status="pending")
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a",
              sources=[ledger, weekly]).model_dump_json())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out

    # Written into the claim file by hand: no verdict was recorded, so none is listed.
    path = data / "claims" / "q1.json"
    raw = json.loads(path.read_text())
    raw["sources"][1]["verification"]["support"] = "contradicts"
    path.write_text(json.dumps(raw))
    built = _built(data)
    assert built["status"] == "pending"
    assert built["conflicts"] == []

    for sid, verdict, note in ((ledger.sid, "supports", "states the vote"),
                               (weekly.sid, "contradicts", "says the vote went the other way")):
        code, out = _vg("judge", "q1", sid, verdict, "--note", note, "--data", data,
                        *_handed(data, "q1", sid))
        assert code == 0, out
    built = _built(data)
    assert built["corroboration_ok"] is True
    assert built["status"] == "human_review"
    line = (f"a verifier judged Harbor Weekly ({WEEKLY}) contradicts the claim: says the vote "
            f"went the other way")
    assert built["conflicts"] == [line]
    html = (data / "out" / "review.html").read_text()
    assert "Conflicts (1)" in html and line in html

    code, out = _vg("status", "--data", data)
    assert code == 0, out
    row = next(ln for ln in out.splitlines() if ln.split()[:1] == ["q1"])
    assert "human_review" in row and row.split()[-1] == "1", row
