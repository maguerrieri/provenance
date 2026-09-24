"""Which of a claim's own conflicts hold it out of green, and how their figures read.

An answer whose dollar figures or years are none of those its snippets state asserts what
its quoted evidence does not carry. That claim is `human_review`, by the same check that
sends a `contradicts` verdict there. Sources disagreeing with each other stay a flag: measured
on real runs, that detector fired mostly on sound claims whose sources describe different
things, such as a total beside one gift.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from vgpipe import cli
from vgpipe.conflicts import detect, money, money_values, unsourced_figures
from vgpipe.fetch import cache_path
from vgpipe.models import EXTRACTOR_VERSION, Claim, PageCache, Source
from vgpipe.verify import check_corroboration, check_inputs

LEDGER = "https://daily-ledger.example/pier-settlement"
WEEKLY = "https://harbor-weekly.example/pier-settlement"
SNIPPET = "approved a pier settlement of $120,000 in 2019"
STORY = f"After two hearings the council {SNIPPET}, the clerk said.\n"


def _source(url=LEDGER, publisher="Daily Ledger", snippet=SNIPPET, status="verified",
            support="supports"):
    s = Source(url=url, publisher=publisher, author="R. Writer", date="2026-05-14",
               source_type="bylined_journalism", snippet=snippet)
    s.verification.status = status
    s.verification.support = support
    return s


def _claim(answer, *sources, qid="q1", **kw):
    return check_corroboration(Claim(question_id=qid, question="?", answer=answer,
                                     sources=list(sources or [_source()]), **kw))


def test_an_answer_stating_a_figure_no_snippet_carries_is_never_green():
    """The gate. Every source is verified and judged `supports`, and one document is all the
    claim needs, so without it the claim rendered `verified` with only a conflict line saying
    the answer's figure is in none of its evidence."""
    money_claim = _claim("The council paid $150,000.")
    assert money_claim.corroboration_ok is True
    assert money_claim.status == "human_review"

    year_claim = _claim("The council settled in 2021.")
    assert year_claim.status == "human_review"

    # A paywalled source beside it: the yellow flag is outside the review filter too.
    paywalled = _claim("The council paid $150,000.",
                       _source(url=WEEKLY, publisher="Harbor Weekly",
                               status="could_not_verify_paywall", support="unreviewed"))
    assert paywalled.status == "human_review"

    # The figure and the year are both in a snippet: nothing to hold.
    assert _claim("The council paid $120,000 in 2019.").status == "verified"


def test_status_reads_it_without_detect_having_run():
    """Worked out from the claim, not read from the conflicts list, so a status read before
    `detect()` (or on a claim file whose `conflicts` says otherwise) still sees it."""
    c = _claim("The council paid $150,000.", conflicts=[])
    assert c.conflicts == []
    assert c.status == "human_review"
    assert unsourced_figures(c) == [
        "dollar figure in the answer ($150,000) does not appear in any cited snippet "
        "($120,000)"]


def test_it_outranks_pending_and_not_found():
    """In the one check a `contradicts` verdict has: no verdict still to come changes what the
    snippets say, and an absence claim that names a year its evidence does not is not a muted
    `not_found`."""
    unjudged = _claim("The council paid $150,000.", _source(support="unreviewed"))
    assert unjudged.status == "human_review"
    assert _claim("The council paid $120,000.", _source(support="unreviewed")).status == "pending"

    absent = _claim("No vote on the pier was recorded in 2021.", confidence="not_found")
    assert absent.status == "human_review"
    assert _claim("No vote on the pier was recorded in 2019.",
                  confidence="not_found").status == "not_found"


def test_the_detector_keeps_its_measured_scope():
    """The measured cost holds only for the detector as measured. It fires when none of the
    answer's figures is in a snippet and the snippets state some: an answer with one figure
    from its evidence, or evidence with no figure at all, is not flagged."""
    assert _claim("The council paid $120,000, then $150,000 more.").status == "verified"
    quiet = _source(snippet="approved the pier settlement after two hearings")
    assert _claim("The council paid $150,000 in 2021.", quiet).status == "verified"


def test_sources_disagreeing_with_each_other_stay_a_flag():
    """The issue's repro, and the decision not to gate on it: two outlets, both verified and
    judged `supports`, one saying $120,000 and the other $102,000. It is listed with the
    conflicts and the status stays green."""
    other = _source(url=WEEKLY, publisher="Harbor Weekly",
                    snippet="the $102,000 pier settlement the council approved")
    [c] = detect([_claim("The council paid $120,000 in 2019.", _source(), other)])
    assert c.conflicts == ["sources disagree on a dollar figure: Daily Ledger ($120,000) vs "
                           "Harbor Weekly ($102,000)"]
    assert c.status == "verified"


def test_a_derived_claim_sees_it():
    """The status is what `derives_from` reads, so a conclusion drawn from the claim goes to
    review with it."""
    base = _claim("The council paid $150,000.")
    conclusion = _claim("The pier cost more than the dock.", qid="q2", derives_from=["q1"])
    check_inputs([base, conclusion])
    assert conclusion.unmet_inputs == ["q1 (human_review)"]
    assert conclusion.status == "human_review"


# --- how a figure reads ---


def test_small_and_fractional_amounts_show_cents():
    """Whole dollars everywhere made a disagreement read as agreement: $0.40 against $0.25
    printed "$0 vs $0", and $4.40 against $4.25 "$4 vs $4"."""
    assert [money(v) for v in (0, 0.25, 0.4, 4, 4.4, 9.99)] == [
        "$0.00", "$0.25", "$0.40", "$4.00", "$4.40", "$9.99"]
    assert [money(v) for v in (10, 120_000, 1_000.5, 8_200_000)] == [
        "$10", "$120,000", "$1,000.50", "$8,200,000"]
    # Finer than a cent keeps its places, or cents would merge these two the same way.
    assert (money(1.1045), money(1.1012)) == ("$1.1045", "$1.1012")

    fare = _source(snippet="raised the ferry fare to $4.40 a ride")
    other = _source(url=WEEKLY, publisher="Harbor Weekly",
                    snippet="the ferry fare, now $4.25 a ride")
    [c] = detect([_claim("The fare is $4.40.", fare, other)])
    assert c.conflicts == ["sources disagree on a dollar figure: Daily Ledger ($4.40) vs "
                           "Harbor Weekly ($4.25)"]

    [c] = detect([_claim("The fare rose to $4.60 in 2021.", fare,
                         _source(snippet="set the fare in 2019"))])
    assert c.conflicts == [
        "dollar figure in the answer ($4.60) does not appear in any cited snippet ($4.40)",
        "year in the answer (2021) does not appear in any cited snippet (2019)"]

    a = _claim("The fare is $4.40.", fare)
    b = _claim("The fare is $4.60.", _source(snippet="raised the ferry fare to $4.60 a ride"),
               qid="q2")
    assert "near-miss dollar figures across claims: $4.40 vs $4.60 (q1, q2)" in "".join(
        detect([a, b])[0].conflicts)


def test_a_scaled_figure_equals_the_same_figure_written_out():
    """8.2 * 1e6 is 8199999.999999999 as floats, so "$8.2 million" and "$8,200,000" were two
    figures: a disagreement between sources, a near miss across claims, and an answer whose
    figure no snippet carried, all over one number."""
    assert money_values("$8.2 million") == money_values("$8,200,000") == {8_200_000.0}
    assert money_values("$1.005k and $1.15 million") == {1_005.0, 1_150_000.0}
    assert money_values("$, and $,,") == set()

    written = _source(snippet="approved a pier settlement of $8,200,000 in 2019")
    scaled = _source(url=WEEKLY, publisher="Harbor Weekly",
                     snippet="the $8.2 million pier settlement from 2019")
    [c] = detect([_claim("The council paid $8.2 million in 2019.", written, scaled)])
    assert c.conflicts == []
    assert c.status == "verified"


def test_a_unit_is_a_whole_word():
    """The unit used to be read off the next word's first letter, so "$5,000 more" was five
    billion dollars. An answer phrased that way had a figure no snippet could carry."""
    assert money_values("$5,000 more by $20 between $3 before $7 kids") == {5_000.0, 20.0,
                                                                          3.0, 7.0}
    assert money_values("$2bn, $3 mn, $4m. $5K $6 thousand $1.5 billion") == {
        2e9, 3e6, 4e6, 5e3, 6e3, 1.5e9}
    # Abbreviations the first-letter read caught by accident, which a whole word must list.
    assert money_values("$5MM, $6 mil, $2.5 bil, a $7M-a-year deal") == {5e6, 6e6, 2.5e9, 7e6}
    # A letter that starts a designation, or is followed by a digit, is not a unit.
    assert money_values("a $500 K-12 increase, $10 m2") == {500.0, 10.0}
    assert _claim("The council paid $120,000 more than planned in 2019.").status == "verified"


# --- end to end: build and status read it, and so does a claim built on it ---


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


def test_build_and_status_hold_it_and_a_claim_built_on_it(tmp_path):
    data = tmp_path / "data"
    _cache(data, LEDGER)
    _cache(data, WEEKLY)
    base = _source(status="pending", support="unreviewed")
    built_on = _source(url=WEEKLY, publisher="Harbor Weekly", status="pending",
                       support="unreviewed")
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="The council paid $150,000.",
              sources=[base]).model_dump_json())
    (data / "claims" / "q2.json").write_text(
        Claim(question_id="q2", question="?", answer="The settlement came after two hearings.",
              sources=[built_on], derives_from=["q1"]).model_dump_json())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    for qid, sid in (("q1", base.sid), ("q2", built_on.sid)):
        code, out = _vg("judge", qid, sid, "supports", "--note", "states it", "--data", data)
        assert code == 0, out

    code, out = _vg("build", "--data", data)
    assert code == 0, out
    q1, q2 = json.loads((data / "out" / "claims.json").read_text())
    assert q1["corroboration_ok"] is True
    assert q1["status"] == "human_review"
    line = "dollar figure in the answer ($150,000) does not appear in any cited snippet ($120,000)"
    assert q1["conflicts"] == [line]
    assert line in (data / "out" / "review.html").read_text()
    assert q2["unmet_inputs"] == ["q1 (human_review)"]
    assert q2["status"] == "human_review"

    code, out = _vg("status", "--data", data)
    assert code == 0, out
    rows = {ln.split()[0]: ln for ln in out.splitlines() if ln.split()[:1] in (["q1"], ["q2"])}
    assert "human_review" in rows["q1"] and rows["q1"].split()[-1] == "1", rows
    assert "human_review" in rows["q2"], rows
