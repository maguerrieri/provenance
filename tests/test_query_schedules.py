"""Which receipts count as contributions.

RCPT_CD holds every receipt schedule, not only gifts: monetary contributions (A), in-kind
contributions (C), and miscellaneous receipts such as a vendor's refund or interest income (I).
`filer_total` already defaulted to schedule A after mixing them made a total come out high.
`contributor_total` and `top_contributor` did not, so a refund could name a non-contributor
"the largest contributor", and the citation would still reproduce green.
"""

from __future__ import annotations

import zipfile

from vgpipe import calaccess, queries

FILER = "9990031"
HEAD = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
        '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n')


def _export(root, receipts):
    """Build a database from one filing's receipts, all filed by FILER."""
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               f'{FILER}\t9990032\tF460\t2/1/2026 12:00:00 AM\n'
               f'{FILER}\t9990033\tF496\t1/31/2026 12:00:00 AM\n')
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", HEAD + receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(root)
    return root


def _row(tran, last, first, amount, form, filing="9990032", date="1/5/2026"):
    return (f'{filing}\t0\t{tran}\t1\t{last}\t{first}\t\t\t{date} 12:00:00 AM'
            f'\t{amount}\t{form}\n')


# A donor's gift, then the same donor's in-kind item, and two receipts from businesses that
# never gave anything: a vendor's refund and a bank's interest, each larger than any gift.
RECEIPTS = (_row("A-1", "Quillfeather", "Ines", "4000", "A")
            + _row("C-2", "Quillfeather", "Ines", "1500", "C")
            + _row("A-3", "Brightwater PAC", "", "2500", "A")
            + _row("I-4", "Example Print Shop", "", "7200", "I")
            + _row("I-5", "Example Credit Union", "", "6100", "I"))


def test_a_refund_or_interest_is_not_the_largest_contributor(tmp_path):
    """Ranked over every schedule, the print shop's refund came out on top."""
    root = _export(tmp_path, RECEIPTS)

    got = queries.run("calaccess.top_contributor", {"filer_id": FILER}, root)
    assert got.value == "Ines Quillfeather", f"a non-contributor ranked first: {got.value!r}"
    assert "schedule-A" in got.detail, "the result must say which schedule it ranked"


def test_an_in_kind_item_does_not_change_the_ranking_by_default(tmp_path):
    """Without the in-kind item the two donors are $1,500 apart; with it, it would decide."""
    root = _export(tmp_path, _row("A-1", "Quillfeather", "Ines", "2500", "A")
                   + _row("C-2", "Quillfeather", "Ines", "1500", "C")
                   + _row("A-3", "Brightwater PAC", "", "3000", "A"))

    got = queries.run("calaccess.top_contributor", {"filer_id": FILER}, root)
    assert got.value == "Brightwater PAC", f"an in-kind item decided the ranking: {got.value!r}"

    every = queries.run("calaccess.top_contributor", {"filer_id": FILER, "form_type": ""}, root)
    assert every.value == "Ines Quillfeather", "form_type='' still ranks every schedule"


def test_a_contributors_in_kind_item_is_not_in_their_total_by_default(tmp_path):
    root = _export(tmp_path, RECEIPTS)

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Quillfeather",
                       "contributor_first": "Ines"}, root)
    assert got.value == 4000.0, f"the in-kind item was summed as a gift: {got.value}"
    assert "schedule-A" in got.detail

    inkind = queries.run("calaccess.contributor_total",
                         {"filer_id": FILER, "contributor": "Quillfeather",
                          "contributor_first": "Ines", "form_type": "C"}, root)
    assert inkind.value == 1500.0, "another schedule is counted when asked for by name"

    every = queries.run("calaccess.contributor_total",
                        {"filer_id": FILER, "contributor": "Quillfeather",
                         "contributor_first": "Ines", "form_type": ""}, root)
    assert every.value == 5500.0


def test_a_refund_is_not_a_contribution_and_the_miss_says_where_it_is(tmp_path):
    """The print shop gave nothing. A miss, never a total, and never a zero: the note names
    the schedule its receipt is on, so a researcher sees why rather than retyping the name."""
    root = _export(tmp_path, RECEIPTS)

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Example Print Shop"}, root)
    assert got.value is None and not got.found, f"a refund counted as a gift: {got.value}"
    assert "form_type=I" in got.note, got.note

    refund = queries.run("calaccess.contributor_total",
                         {"filer_id": FILER, "contributor": "Example Print Shop",
                          "form_type": "I"}, root)
    assert refund.value == 7200.0


def test_the_miss_suggests_names_from_the_schedule_it_searched(tmp_path):
    """A near-match from another schedule is a receipt, not the contributor that was meant."""
    root = _export(tmp_path, RECEIPTS + _row("I-6", "Brightwater Bank", "", "90", "I"))

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Brightwater"}, root)
    assert not got.found
    assert "Brightwater PAC" in got.note and "Brightwater Bank" not in got.note, got.note


def test_a_gift_on_two_forms_still_counts_once_across_every_schedule(tmp_path):
    """Cross-form dedup collapses a Schedule A gift and its Form 496 Part 3 report. Counting
    every schedule on request must still count it once."""
    root = _export(tmp_path, _row("A-100001", "Brightwater PAC", "", "2500", "A")
                   + _row("F496P3-100001", "Brightwater PAC", "", "2500", "F496P3",
                          filing="9990033"))

    for form_type in ("A", ""):
        got = queries.run("calaccess.contributor_total",
                          {"filer_id": FILER, "contributor": "Brightwater PAC",
                           "form_type": form_type}, root)
        assert got.value == 2500.0, f"form_type={form_type!r} counted it twice: {got.value}"
