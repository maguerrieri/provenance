"""A paywalled source is flagged for the human, never left waiting on a verdict.

`could_not_verify_paywall` has no confirmed context: the live page is gated and no snapshot
confirmed the quote. The judgment pass cannot cover it. `vg judge` refuses it, and a verdict
recorded on it reads stale. So a roll-up that waited on its verdict read `pending` forever,
and the review page never showed the paywall.

The one route to a verdict on a paywalled source is its snapshot. Once `vg verify` or
`vg archive` confirms the quote there, the row is `verified_via_archive`, its context is the
snapshot, and it waits on a verdict like any other row.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from vgpipe import archive, cli, judgments
from vgpipe.fetch import cache_path
from vgpipe.models import EXTRACTOR_VERSION, Claim, PageCache, Source
from vgpipe.verify import check_corroboration

PAYWALLED = "https://daily-ledger.example/levy-vote"
SNAPSHOT = f"https://web.archive.org/web/20260901000000/{PAYWALLED}"
READABLE = "https://harbor-weekly.example/levy-vote"
SNIPPET = "voted against the harbor levy twice"
STORY = f"At both hearings the councilmember {SNIPPET}, citing the port budget.\n"


def _source(url=PAYWALLED, status="could_not_verify_paywall", support="unreviewed"):
    s = Source(url=url, publisher="Daily Ledger", author="R. Writer", date="2026-05-14",
               source_type="bylined_journalism", snippet=SNIPPET)
    s.verification.status = status
    s.verification.support = support
    s.paywall = status == "could_not_verify_paywall"
    return s


def _claim(*sources, **kw):
    return check_corroboration(Claim(question_id="q1", question="?", answer="a",
                                     sources=list(sources), **kw))


def test_a_claim_whose_only_source_is_paywalled_is_flagged_not_pending():
    """The regression: nothing can record a verdict on this source, so waiting on one read
    `pending` for good, and the reviewer never saw that the page is paywalled."""
    only = _claim(_source())
    assert only.corroboration_ok is True
    assert only.status == "could_not_verify_paywall"
    assert _claim(_source(), _source(url=READABLE)).status == "could_not_verify_paywall"


def test_the_exemption_covers_the_paywalled_source_and_nothing_beside_it():
    """Only the paywalled row is outside the judgment pass. A readable source beside it still
    waits on its verdict, and one `vg verify` has not reached yet is still pending. A verdict
    on the readable source never lifts the claim to verified: the paywalled row keeps it
    yellow."""
    waiting = _claim(_source(url=READABLE, status="verified"), _source())
    assert waiting.status == "pending", "the readable source has no verdict yet"
    waiting.sources[0].verification.support = "supports"
    assert waiting.status == "could_not_verify_paywall"

    not_yet_verified = _claim(_source(url=READABLE, status="pending"), _source())
    assert not_yet_verified.status == "pending"

    # A rejected source beside a paywalled one is not "every source rejected": the paywalled
    # source may still carry the claim, and only the human can read it.
    rejected = _claim(_source(url=READABLE, status="verified", support="topic_only"), _source())
    assert rejected.status == "could_not_verify_paywall"


def test_a_paywalled_claim_short_of_corroboration_goes_to_review_not_yellow():
    """The flag does not stand in for corroboration. An adversarial claim needs two independent
    documents whatever their status; with every source verified, one short is `human_review`,
    and the paywall must not move it behind a yellow badge outside the review filter."""
    alone = _claim(_source(), claim_type="adversarial")
    assert alone.corroboration_ok is False
    assert alone.status == "human_review"

    other = _source(url=READABLE, status="verified", support="supports")
    other.publisher = "Harbor Weekly"
    two = _claim(other, _source(), claim_type="adversarial")
    assert two.corroboration_ok is True
    assert two.status == "could_not_verify_paywall"

    other.publisher = "Daily Ledger"   # one outlet twice: not independent
    assert _claim(other, _source(), claim_type="adversarial").status == "human_review"


def test_the_statuses_outside_the_judgment_pass_are_exactly_the_usable_ones_without_context():
    """`vg judgments` gates on GOOD: a verifier can judge only a row with confirmed context.
    A usable status outside GOOD has none, so the roll-up must not wait on its verdict, and a
    new status of that kind has to be classified here rather than read `pending` forever."""
    from vgpipe.models import NOT_JUDGED
    from vgpipe.verify import GOOD, USABLE

    assert NOT_JUDGED == set(USABLE) - set(GOOD)


def test_a_failed_citation_still_outranks_the_paywall_flag():
    """The exemption must not put a broken citation behind the yellow badge, which sits outside
    the review filter."""
    broken = _claim(_source(url=READABLE, status="snippet_not_found"), _source())
    assert broken.status == "human_review"


# --- end to end: what `vg verify`, `vg judge`, `vg judgments` and `vg build` do with one ---


def _cache(root: Path, url: str, fetched_at: datetime, **kw) -> None:
    page = PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=STORY, fetched_at=fetched_at, extractor_version=EXTRACTOR_VERSION)
    cache_path(root, url).write_text(page.model_copy(update=kw).model_dump_json())


def _vg(*args) -> tuple[int, str]:
    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _paywalled_run(tmp_path: Path, *, snapshot: bool) -> tuple[Path, Source]:
    """A run whose one source is gated live, verified offline as `vg verify` leaves it. With
    `snapshot`, the run's archive records hold a capture that contains the quote."""
    data, now = tmp_path / "data", datetime.now(UTC)
    _cache(data, PAYWALLED, now - timedelta(days=3), status=403, text="",
           paywall_suspected=True)
    if snapshot:
        _cache(data, SNAPSHOT, now - timedelta(days=2))
        archive.save_records(data, {PAYWALLED: {"snapshot": SNAPSHOT, "error": None}})
    s = Source(url=PAYWALLED, publisher="Daily Ledger", author="R. Writer",
               date="2026-05-14", source_type="bylined_journalism", snippet=SNIPPET)
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    return data, s


def _built(data: Path) -> dict:
    code, out = _vg("build", "--data", data)
    assert code == 0, out
    [claim] = json.loads((data / "out" / "claims.json").read_text())
    return claim


def _gate(data: Path) -> tuple[int, int, int]:
    """(need a verdict, total, nothing to judge yet), read off what `vg judgments` prints."""
    code, out = _vg("judgments", "--data", data)
    m = re.search(r"(\d+) of (\d+) cited source\(s\) need a verdict", out)
    assert m, out
    assert code == (1 if int(m.group(1)) else 0), out
    b = re.search(r"(\d+) more source\(s\) have nothing a verifier can judge yet", out)
    return int(m.group(1)), int(m.group(2)), int(b.group(1)) if b else 0


def test_a_paywalled_claim_builds_flagged_and_the_gate_does_not_wait_on_it(tmp_path):
    """No snapshot confirms the quote, so the source is outside the judgment pass: `vg judge`
    refuses it, the gate reads 0, and the review app shows the paywall rather than a claim
    waiting on a judgment pass that can never record anything."""
    data, s = _paywalled_run(tmp_path, snapshot=False)
    built = _built(data)
    assert built["sources"][0]["verification"]["status"] == "could_not_verify_paywall"
    assert built["status"] == "could_not_verify_paywall"

    assert _gate(data) == (0, 1, 1)
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", data)
    assert code == 1 and "it is could_not_verify_paywall" in out, out


def test_a_verdict_left_on_a_paywalled_source_is_not_applied(tmp_path):
    """A verdict recorded before the row fell back to its paywall (the snapshot that confirmed
    it was replaced, say) may be about a copy the row no longer has. It is not applied, and the
    claim is flagged either way: neither a `supports` nor a `topic_only` decides it."""
    data, s = _paywalled_run(tmp_path, snapshot=False)
    for verdict in ("supports", "topic_only"):
        judgments.record(data, "q1", s.sid, verdict, "left from an earlier snapshot")
        built = _built(data)
        assert built["sources"][0]["verification"]["support"] == "unreviewed", verdict
        assert built["status"] == "could_not_verify_paywall", verdict


def test_a_paywalled_source_is_judged_through_its_snapshot(tmp_path):
    """The chosen path: where the run's own snapshot confirms the quote, the row is
    `verified_via_archive` and is judged like any other. It waits on a verdict (`pending`, and
    counted by the gate) until a verifier judges the snapshot's context, then renders
    verified."""
    data, s = _paywalled_run(tmp_path, snapshot=True)
    built = _built(data)
    assert built["sources"][0]["verification"]["status"] == "verified_via_archive"
    assert built["status"] == "pending"
    assert _gate(data) == (1, 1, 0)

    code, out = _vg("judge", "q1", s.sid, "supports", "--note", "the snapshot quotes the vote",
                    "--data", data)
    assert code == 0, out
    assert _gate(data) == (0, 1, 0)
    assert _built(data)["status"] == "verified"
