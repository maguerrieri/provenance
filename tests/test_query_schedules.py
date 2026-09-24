"""Which receipts count as contributions.

RCPT_CD holds every receipt schedule, not only gifts: monetary contributions (A), in-kind
contributions (C), and miscellaneous receipts such as a vendor's refund or interest income (I).
`filer_total` already defaulted to schedule A after mixing them made a total come out high.
`contributor_total` and `top_contributor` did not, so a refund could name a non-contributor
"the largest contributor", and the citation would still reproduce green.
"""

from __future__ import annotations

import inspect
import zipfile

import pytest

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
        # an empty cover table: every citable query refuses a database without one
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", "FILING_ID\tAMEND_ID\n")
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


def test_a_miss_on_another_schedule_names_the_schedules_as_filed(tmp_path):
    """Asked for in-kind items, a donor with only monetary gifts misses. The note must point at
    schedule A, not call the gifts non-contributions. The gift was also reported on Form 496
    Part 3; the dedup's collapsed row carries only that label, and read from it the hint named
    F496P3 alone."""
    root = _export(tmp_path, _row("A-100001", "Brightwater PAC", "", "2500", "A")
                   + _row("F496P3-100001", "Brightwater PAC", "", "2500", "F496P3",
                          filing="9990033"))

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Brightwater PAC", "form_type": "C"},
                      root)
    assert not got.found
    assert "form_type=A" in got.note and "form_type=F496P3" in got.note, got.note
    assert "not counted as contributions" not in got.note


def test_a_schedule_hint_leaves_room_for_the_near_names(tmp_path):
    """The note shows four suggestions. One hint per schedule pushed the schedule-A spelling
    the researcher needed out of view."""
    root = _export(tmp_path, _row("A-1", "Brightwater PAC SCC", "", "2500", "A")
                   + "".join(_row(f"{f}-{i}", "Brightwater PAC", "", "100", f)
                             for i, f in enumerate(("C", "I", "F401A", "F496P3"), 2)))

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Brightwater PAC"}, root)
    assert not got.found
    assert "Brightwater PAC SCC" in got.note and "form_type=F496P3" in got.note, got.note


def test_a_receipt_with_no_schedule_is_never_suggested_as_one(tmp_path):
    """"form_type=" is how to ask for every schedule, so suggesting it for a blank schedule
    would count refunds and interest back in."""
    root = _export(tmp_path, _row("X-1", "Example Print Shop", "", "7200", ""))

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": FILER, "contributor": "Example Print Shop"}, root)
    assert not got.found and "form_type" not in got.note, got.note


# Every query over the receipts, with what each needs besides filer_id and form_type.
RECEIPT_QUERIES = (("calaccess.contributor_total", {"contributor": "Brightwater PAC"}),
                   ("calaccess.filer_total", {}),
                   ("calaccess.top_contributor", {}))


def test_every_receipt_query_is_tested_for_its_schedule_handling():
    """RECEIPT_QUERIES is a hand-kept list, and filer_total was the query a hand-kept fix
    missed. So the list is checked against the registry: a new query over the receipts must be
    added here, where the tests below hold it to the guards."""
    reads_receipts = {name for name, q in queries.REGISTRY.items()
                      if "DEDUPED_RECEIPTS" in inspect.getsource(q.fn)
                      or "RCPT_LATEST" in inspect.getsource(q.fn)}
    assert reads_receipts == {name for name, _ in RECEIPT_QUERIES}


def test_the_label_upper_cases_as_the_filter_matches():
    """SQLite's UPPER() folds ASCII only. Python folds "ß" to "SS", naming a schedule the
    filter never matched."""
    assert queries._schedule_label("f401a") == "schedule-F401A"
    assert queries._schedule_label("aß") == "schedule-Aß"


def test_a_padded_schedule_is_refused(tmp_path):
    """" " is truthy: it filtered to the receipts with no schedule, under a label naming none.
    filer_total ran it and returned that sum as found, after the other two were fixed."""
    root = _export(tmp_path, RECEIPTS + _row("X-6", "Example Print Shop", "", "300", ""))
    for name, params in RECEIPT_QUERIES:
        for form_type in (" ", " A"):
            with pytest.raises(ValueError, match="form_type must be a schedule code"):
                queries.run(name, {"filer_id": FILER, "form_type": form_type, **params}, root)


def test_a_filer_with_no_schedule_a_is_not_read_as_receiving_nothing(tmp_path):
    """A slate mailer's receipts are all Form 401 payments. The ranking and the total miss on
    schedule A, and say where the receipts are."""
    root = _export(tmp_path, _row("P-1", "Brightwater PAC", "", "2500", "F401A"))

    for name, slate_value in (("calaccess.top_contributor", "Brightwater PAC"),
                              ("calaccess.filer_total", 2500.0)):
        got = queries.run(name, {"filer_id": FILER}, root)
        assert not got.found and got.value is None, f"{name}: {got.value!r}"
        assert "form_type=F401A" in got.note, f"{name}: {got.note}"
        assert got.suggestions == ["form_type=F401A"], f"{name}: {got.suggestions}"

        slate = queries.run(name, {"filer_id": FILER, "form_type": "F401A"}, root)
        assert slate.value == slate_value, f"{name}: {slate.value!r}"

        # Matched case-insensitively, so named as filed: "schedule-f401a" read as a schedule
        # other than the F401A a miss suggests.
        lower = queries.run(name, {"filer_id": FILER, "form_type": "f401a"}, root)
        assert lower.value == slate_value and "schedule-F401A" in lower.detail, lower.detail


@pytest.mark.parametrize("name,params", RECEIPT_QUERIES)
def test_every_schedule_is_named_as_what_was_counted(tmp_path, name, params):
    """form_type="" counts every schedule. filer_total's detail read "schedule- gift(s)", naming
    no schedule at all, where the other two said "every-schedule"."""
    root = _export(tmp_path, RECEIPTS)

    got = queries.run(name, {"filer_id": FILER, "form_type": "", **params}, root)
    assert got.found and "every-schedule" in got.detail, got.detail
    assert "schedule- " not in got.detail, got.detail

    miss = queries.run(name, {"filer_id": "9990099", "form_type": "", **params}, root)
    assert not miss.found and "every-schedule" in miss.detail, miss.detail
    assert "schedule- " not in miss.detail and "form_type" not in miss.note, miss.note


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


def test_an_unreadable_receipt_on_another_schedule_does_not_block_the_ranking(tmp_path):
    """A rank names no largest contributor while a gift it ranks has no amount. A refund with
    no amount is not a gift, so by default it leaves the schedule-A ranking alone; ranked over
    every schedule, it still blocks it."""
    root = _export(tmp_path, RECEIPTS + _row("I-6", "Example Print Shop", "", "", "I"))

    got = queries.run("calaccess.top_contributor", {"filer_id": FILER}, root)
    assert got.found and got.value == "Ines Quillfeather", got

    every = queries.run("calaccess.top_contributor", {"filer_id": FILER, "form_type": ""}, root)
    assert not every.found and "1 more gift(s) to this filer with no readable amount" in every.note
