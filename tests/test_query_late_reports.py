"""Late contributions that no schedule A restates yet.

A contribution received in the weeks before an election is reported within 24 hours on a late
report: Form 497 Part 1, or Form 496 Part 3 for a committee making independent expenditures. It
reaches schedule A only when the Form 460 covering its date is filed. The contribution queries
count schedule A, so until then the gift was left out without a word: `top_contributor` could
name the wrong donor "the largest contributor", and every total came out short, green.

They never add a late entry to a total. With form_type unset they refuse while one could change
the answer. form_type=A gives the schedule-A figure alone, and while a late entry could change
it the figure goes to human_review, naming each late report to open: a note alone rendered
green.
"""

from __future__ import annotations

import zipfile

import pytest

from provenance import calaccess, queries
from provenance.models import QueryCitation, Source
from provenance.verify import revalidate_from_cache, verify_query_source

FILER = "9990661"
OTHER_FILER = "9990670"
DAY = " 12:00:00 AM"

RCPT_HEAD = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP"
             "\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n")
# The real S497_CD carries many more columns; the build keeps only the ones it wants.
S497_HEAD = ("FILING_ID\tAMEND_ID\tLINE_ITEM\tREC_TYPE\tFORM_TYPE\tTRAN_ID\tENTITY_CD"
             "\tENTY_NAML\tENTY_NAMF\tENTY_CITY\tCTRIB_EMP\tCTRIB_OCC\tELEC_DATE\tCTRIB_DATE"
             "\tDATE_THRU\tAMOUNT\tCMTE_ID\n")
CVR_HEAD = ("FILING_ID\tAMEND_ID\tREC_TYPE\tFORM_TYPE\tFILER_ID\tFILER_NAML\tFROM_DATE"
            "\tTHRU_DATE\tELECT_DATE\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD\n")

# The filer's Form 460 covers the first half of 2026. Its late reports come after.
F460, F496, F497 = "9990662", "9990663", "9990664"
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{F460}\tF460\t7/31/2026{DAY}\n"
           f"{FILER}\t{F496}\tF496\t9/20/2026{DAY}\n"
           f"{FILER}\t{F497}\tF497\t10/5/2026{DAY}\n"
           f"{OTHER_FILER}\t9990671\tF460\t1/31/2027{DAY}\n")
# another filer's statement covering the late period: it restates nothing of ours
OTHER_460 = (f"9990671\t0\tCVR\tF460\t{OTHER_FILER}\tExample Hills Committee\t7/1/2026{DAY}"
             f"\t12/31/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")
# Our Form 496 has a cover too: a filing with rows and none is unsettled (#141), and these
# tests are about late reports, not that.
COVERS = (CVR_HEAD
          + f"{F460}\t0\tCVR\tF460\t{FILER}\tNeighbors for Example Valley\t1/1/2026{DAY}"
            f"\t6/30/2026{DAY}\t11/3/2026{DAY}\t\t\t\n"
          + f"{F496}\t0\tCVR\tF496\t{FILER}\tNeighbors for Example Valley\t\t\t11/3/2026{DAY}"
            f"\t\t\t\n"
          + OTHER_460)


# A cover table with no 460 of this filer's: every citable query refuses a database with no
# cover table at all (#31), so "no period" has to be a table without one.
NO_460_OF_OURS = CVR_HEAD + OTHER_460


def rcpt(tran, last, first, amount, form, filing=F460, date="3/2/2026", amend="0"):
    return (f"{filing}\t{amend}\t{tran}\t1\t{last}\t{first}\t\t\t{date}{DAY}"
            f"\t{amount}\t{form}\n")


def s497(tran, last, first, amount, date="10/2/2026", part="F497P1", filing=F497, amend="0",
         thru=""):
    when = f"{date}{DAY}" if date else ""
    until = f"{thru}{DAY}" if thru else ""
    return (f"{filing}\t{amend}\t1\tS497\t{part}\t{tran}\tIND\t{last}\t{first}\tExampleville"
            f"\t\t\t11/3/2026{DAY}\t{when}\t{until}\t{amount}\t\n")


# Two schedule-A gifts on the 460, the largest from the PAC.
ON_SCHEDULE_A = (rcpt("A-1", "Marwick", "Odile", "3000", "A")
                 + rcpt("A-2", "Fernhollow Growers PAC", "", "5000", "A", date="4/9/2026"))


def build(root, receipts, late="", covers=COVERS, filings=FILINGS):
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RCPT_HEAD + receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
        if covers:
            zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
        if late is not None:
            zf.writestr("CalAccess/DATA/S497_CD.TSV", S497_HEAD + late)
    calaccess.build(root)
    return root


def run(root, name, **params):
    return queries.run(f"calaccess.{name}", {"filer_id": FILER, **params}, root)


def test_a_gift_only_on_form_496_part_3_is_never_silently_dropped(tmp_path):
    """The acceptance case: a gift reported on Form 496 Part 3, after the last 460's period."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-7", "Saltmarsh", "Tobiah", "2000", "F496P3", filing=F496,
                        date="9/18/2026"))

    late = run(root, "contributor_total", contributor="Saltmarsh", contributor_first="Tobiah")
    assert not late.found and late.value is None, late.note
    assert "1 late-report entry ($2,000.00; filing 9990663)" in late.note, late.note

    total = run(root, "filer_total")
    assert not total.found and total.value is None, f"a short total was given: {total.value}"
    assert "$8,000.00 across 2 itemized schedule-A gift(s)" in total.note
    assert "not a complete total" in total.note and "form_type=A" in total.note

    # Asked for schedule A by name, the figure is schedule A's, says what it leaves out, and
    # is held for a person with the late report to open.
    a = run(root, "filer_total", form_type="A")
    assert a.value == 8000.0
    assert "filing 9990663) not yet restated on a Form 460 schedule A, not counted" in a.detail
    assert "filing 9990663 (Form 496 Part 3): $2,000.00 in 1 entry" in a.unsettled, a.unsettled
    assert calaccess.filing_url(F496) in a.unsettled
    # Every schedule counts the Form 496 Part 3 row, once.
    assert run(root, "filer_total", form_type="").value == 10000.0

    # A donor with no late gift is unaffected.
    assert run(root, "contributor_total", contributor="Marwick",
               contributor_first="Odile").value == 3000.0


def test_a_late_gift_too_small_to_change_the_ranking_leaves_it_standing(tmp_path):
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-7", "Saltmarsh", "Tobiah", "2000", "F496P3", filing=F496,
                        date="9/18/2026"))

    got = run(root, "top_contributor")
    assert got.value == "Fernhollow Growers PAC", got.note
    assert "not counted — they cannot change the ranking" in got.detail
    # A ranking no late gift can move is settled, by name too.
    assert not got.unsettled and not run(root, "top_contributor", form_type="A").unsettled


def test_a_late_gift_that_could_change_the_ranking_is_not_a_settled_answer(tmp_path):
    """$3,000 on schedule A and $4,000 on a Form 497: she may well be the largest contributor,
    and the ranking would have named the PAC."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Marwick", "Odile", "4000"))

    got = run(root, "top_contributor")
    assert not got.found and got.value is None, f"a ranking a late gift could change: {got.value}"
    assert "Odile Marwick ($3,000 on schedule-A, $4,000 late)" in got.note, got.note
    assert "not a settled ranking" in got.note and "form_type=A" in got.note

    a = run(root, "top_contributor", form_type="A")
    assert a.value == "Fernhollow Growers PAC"
    assert "they could change the ranking" in a.detail
    assert "filing 9990664 (Form 497): $4,000.00" in a.unsettled, a.unsettled

    her = run(root, "contributor_total", contributor="Marwick", contributor_first="Odile")
    assert not her.found and "$3,000.00 across 1 itemized schedule-A gift(s)" in her.note
    assert "filing 9990664" in her.note, her.note
    by_name = run(root, "contributor_total", contributor="Marwick", contributor_first="Odile",
                  form_type="A")
    assert by_name.value == 3000.0 and "filing 9990664 (Form 497)" in by_name.unsettled
    # A contributor with no late gift of their own is settled, though the filer has one.
    assert not run(root, "contributor_total", contributor="Fernhollow Growers PAC",
                   form_type="A").unsettled


def test_a_late_gift_breaks_a_tie(tmp_path):
    root = build(tmp_path, rcpt("A-1", "Marwick", "Odile", "5000", "A")
                 + rcpt("A-2", "Fernhollow Growers PAC", "", "5000", "A"),
                 late=s497("L-1", "Marwick", "Odile", "100"))

    got = run(root, "top_contributor")
    assert not got.found, f"a tie a late gift breaks was reported as a tie: {got.value}"


def test_a_late_amount_of_nothing_breaks_no_tie(tmp_path):
    root = build(tmp_path, rcpt("A-1", "Marwick", "Odile", "5000", "A")
                 + rcpt("A-2", "Fernhollow Growers PAC", "", "5000", "A"),
                 late=s497("L-1", "Marwick", "Odile", "0"))

    got = run(root, "top_contributor")
    assert got.value == "Fernhollow Growers PAC | Odile Marwick", got.note


def test_a_negative_late_amount_can_unseat_the_leader(tmp_path):
    """The PAC leads $5,000 to $3,000, and a late entry takes $2,500 off the PAC: it could end
    at $2,500, behind a contributor who has no late gift at all."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("L-1", "Fernhollow Growers PAC", "", "-2500"))

    got = run(root, "top_contributor")
    assert not got.found, f"a leader who could drop to second was named: {got.value}"
    assert "Odile Marwick ($3,000 on schedule-A, no late gift)" in got.note, got.note
    assert "Fernhollow Growers PAC ($5,000 on schedule-A, $-2,500 late)" in got.note, got.note


def test_a_late_report_with_the_whole_name_in_one_field_is_still_hers(tmp_path):
    """Filers do not reliably split names. A Form 497 with "Odile Marwick" in the last-name
    field is her gift: it gates her total and adds to her schedule-A sum in the ranking."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Odile Marwick", "", "2500"))

    her = run(root, "contributor_total", contributor="Marwick", contributor_first="Odile")
    assert not her.found, f"her late gift escaped the gate: {her.value}"
    got = run(root, "top_contributor")
    assert not got.found and "Odile Marwick ($3,000 on schedule-A, $2,500 late)" in got.note, (
        got.note)


def test_every_schedule_names_the_form_497_gifts_it_leaves_out(tmp_path):
    """Every schedule counts Form 496 Part 3 rows, which are in the receipts table. A Form 497
    is not, so that figure is short by it too, and says so."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-7", "Saltmarsh", "Tobiah", "2000", "F496P3", filing=F496,
                        date="9/18/2026"),
                 late=s497("L-1", "Quennell", "Ada", "700"))

    every = run(root, "filer_total", form_type="")
    assert every.value == 10000.0
    assert "1 late-report entry ($700.00; filing 9990664)" in every.detail, every.detail
    assert "NOT a complete total" in every.detail
    # held for the Form 497 only: the Form 496 Part 3 row is in the figure
    assert [r.filing_id for r in every.late] == [int(F497)], every.unsettled


def test_every_schedule_holds_a_form_496_row_her_total_did_not_sum(tmp_path):
    """Every schedule sums the Form 496 Part 3 rows, but one contributor's total sums only
    those filed under the name it matches. Her $9,000 gift with the whole name in the
    last-name field was dropped as "already summed", and her $3,000 verified green.

    It is held as the name it was filed under: that name is on every schedule, in her group,
    so the name check holds it (`_groups`), one rule with no second check for Form 496 rows."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-8", "Odile Marwick", "", "9000", "F496P3", filing=F496,
                        date="9/18/2026")
                 + rcpt("F496P3-9", "Marwick", "Odile", "400", "F496P3", filing=F496,
                        date="9/19/2026"))

    every = dict(contributor="Marwick", contributor_first="Odile", form_type="")
    her = run(root, "contributor_total", **every)
    assert not her.found and "'Odile Marwick'/'' $9,000.00 in 1 gift(s)" in her.note, her.note
    assert her.suggestions == ["names=as_filed"], her.suggestions
    as_filed = run(root, "contributor_total", names="as_filed", **every)
    # the row filed as the sum matches it is in the figure; the other is held for a person
    assert as_filed.value == 3400.0, as_filed.note
    assert [n.split(" $")[0] for n in as_filed.names] == ["'Odile Marwick'/''"], as_filed.names
    assert "'Odile Marwick'/'' $9,000.00" in as_filed.unsettled, as_filed.unsettled
    assert not run(root, "contributor_total", contributor="Marwick",
                   contributor_first="Odile").found
    # a filer's figure sums both rows, so neither is held
    total = run(root, "filer_total", form_type="")
    assert total.value == 17400.0 and not total.late, total.unsettled


def test_a_blank_late_amount_is_not_restated_by_a_zero(tmp_path):
    """Grouped by CAST, a blank reads as 0 and paired with a schedule-A row of "0"."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("A-9", "Saltmarsh", "Tobiah", "0", "A", date="10/2/2026"),
                 late=s497("P-9", "Saltmarsh", "Tobiah", ""))

    assert not run(root, "filer_total").found


@pytest.mark.parametrize("head", [
    S497_HEAD.replace("\tAMEND_ID", "\tAMENDMENT"),   # no latest-amendment view
    "RECORD\tKIND\n",                                  # none of the wanted columns
])
def test_a_form_497_table_the_database_cannot_read_is_not_an_empty_one(tmp_path, head):
    """The build keeps what columns it finds, or skips a file with none it wants. Either way
    the database holds no readable Form 497, which is not the same as there being none."""
    (tmp_path / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(calaccess.zip_path(tmp_path), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RCPT_HEAD + ON_SCHEDULE_A)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", head + s497("L-1", "Marwick", "Odile", "4000"))
    calaccess.build(tmp_path)

    for form_type in ({}, {"form_type": "A"}, {"form_type": ""}):
        with pytest.raises(calaccess.DegradedDatabase, match="lacks a column"):
            run(tmp_path, "filer_total", **form_type)


def test_a_surname_is_not_one_contributor_across_forms_either(tmp_path):
    """Schedule A has one Marwick, and a late report another. Asked by surname alone, the
    total would be hers and the late gift his, reported as hers not yet counted."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Marwick", "Jonah", "700"))

    for form_type in ({}, {"form_type": "A"}):
        got = run(root, "contributor_total", contributor="Marwick", **form_type)
        assert not got.found and "DIFFERENT first names" in got.note, got.note
    assert run(root, "contributor_total", contributor="Marwick", contributor_first="Odile",
               ).value == 3000.0


@pytest.mark.parametrize("last, first", [
    ("Marwick, Odile", ""),     # "LAST, FIRST" in one field
    ("Marwick", ""),            # a bare surname could be hers
    ("Marwick", "Odile J"),     # a middle initial
])
def test_a_late_name_filed_another_way_still_counts_against_her(tmp_path, last, first):
    """A name the ranking could not place was a new giver holding only the late amount, and a
    $2,500 gift that could lift her past the PAC let the ranking stand."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", last, first, "2500"))

    got = run(root, "top_contributor")
    assert not got.found and "Odile Marwick ($3,000 on schedule-A, $2,500 late)" in got.note, (
        got.note)
    her = run(root, "contributor_total", contributor="Marwick", contributor_first="Odile")
    assert not her.found and "late-report entry" in her.note, her.note


@pytest.mark.parametrize("also", [
    "",
    # seen first, "Ada Quennell" joined "Ada C Quennell" and never met "Ada B Quennell"
    s497("L-0", "Quennell", "Ada C", "100", date="10/1/2026"),
], ids=["two-names", "after-a-third-name"])
def test_a_late_giver_filed_two_ways_is_held_as_one(tmp_path, also):
    """Two $3,000 late gifts, as "Ada Quennell" and "Ada B Quennell", could be one giver's
    $6,000, past the $5,000 leader. Neither name is on schedule A, so each was held only against
    itself, and the ranking was said to be one they "cannot change"."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=also + s497("L-1", "Quennell", "Ada", "3000")
                 + s497("L-2", "Quennell", "Ada B", "3000", date="10/3/2026"))

    got = run(root, "top_contributor")
    assert not got.found, got.note
    # One group of late names, held as one giver: every late entry in it, each once.
    assert f"$0 on schedule-A, ${'6,100' if also else '6,000'} late)" in got.note, got.note
    named = run(root, "top_contributor", form_type="A")
    assert named.value == "Fernhollow Growers PAC" and named.unsettled, named.note


def test_one_giver_filed_two_ways_is_not_two_people(tmp_path):
    """Schedule A files her whole name in the last-name field, the late report splits it."""
    root = build(tmp_path, rcpt("A-1", "Odile Marwick", "", "3000", "A"),
                 late=s497("L-1", "Marwick", "Odile", "400"))

    got = run(root, "contributor_total", contributor="Odile Marwick")
    assert not got.found and "DIFFERENT first names" not in got.note, got.note
    assert "late-report entry" in got.note
    a = run(root, "contributor_total", contributor="Odile Marwick", form_type="A")
    assert a.value == 3000.0, a.note


def test_an_amount_with_a_dollar_sign_is_not_a_zero(tmp_path):
    """CAST reads "$5,000" as 0.0, which paired it with a schedule-A row of "0"."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("A-9", "Saltmarsh", "Tobiah", "0", "A", date="10/2/2026"),
                 late=s497("P-9", "Saltmarsh", "Tobiah", "$5,000"))

    assert not run(root, "filer_total").found


def test_contributions_the_filer_made_are_not_late_contributions_to_it(tmp_path):
    """Form 497 Part 2 lists the filer's own late contributions to others."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("M-1", "Example Neighbors Committee", "", "9000", part="F497P2"))

    assert run(root, "filer_total").value == 8000.0
    assert run(root, "top_contributor").value == "Fernhollow Growers PAC"
    miss = run(root, "contributor_total", contributor="Example Neighbors Committee")
    assert not miss.found and "late-report" not in miss.note, miss.note


def test_a_late_gift_restated_on_a_filed_460_is_counted_once(tmp_path):
    """Dated inside the 460's period, so the 460 had to restate it: it is counted once, from
    schedule A, even though its TRAN_ID shares no base with the schedule-A copy."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("A-3", "Saltmarsh", "Tobiah", "1500", "A", date="5/20/2026")
                 + rcpt("F496P3-900", "Saltmarsh", "Tobiah", "1500", "F496P3", filing=F496,
                        date="5/20/2026"),
                 late=s497("L-44", "Saltmarsh", "Tobiah", "1500", date="5/20/2026"))

    assert run(root, "contributor_total", contributor="Saltmarsh",
               contributor_first="Tobiah").value == 1500.0
    assert run(root, "filer_total").value == 9500.0


def test_a_late_entry_spanning_past_the_460s_period_is_not_covered_by_it(tmp_path):
    """A Form 497 entry can cover a range of dates. The 460 ending June 30 restates the part
    received by then, not what came in on July 3."""
    covered = build(tmp_path / "covered", ON_SCHEDULE_A
                    + rcpt("A-3", "Saltmarsh", "Tobiah", "900", "A", date="6/28/2026"),
                    late=s497("L-1", "Saltmarsh", "Tobiah", "900", date="6/28/2026"))
    assert run(covered, "filer_total").value == 8900.0

    spans = build(tmp_path / "spans", ON_SCHEDULE_A
                  + rcpt("A-3", "Saltmarsh", "Tobiah", "900", "A", date="6/28/2026"),
                  late=s497("L-1", "Saltmarsh", "Tobiah", "900", date="6/28/2026",
                            thru="7/3/2026"))
    assert not run(spans, "filer_total").found


def test_the_same_gift_on_both_forms_still_counts_once_without_a_460(tmp_path):
    """No cover record says what our 460 covers, but its schedule-A row carries the same
    transaction as the Form 496 Part 3 row (the cross-form key): that is a restatement."""
    root = build(tmp_path, rcpt("A-100001", "Fernhollow Growers PAC", "", "2500", "A")
                 + rcpt("F496P3-100001", "Fernhollow Growers PAC", "", "2500", "F496P3",
                        filing=F496),
                 covers=NO_460_OF_OURS)

    for name, params in (("contributor_total", {"contributor": "Fernhollow Growers PAC"}),
                         ("filer_total", {})):
        got = run(root, name, **params)
        assert got.value == 2500.0, f"{name}: {got.note}"
        assert "late-report" not in got.detail
    assert run(root, "top_contributor").value == "Fernhollow Growers PAC"


def test_a_late_gift_pairs_with_a_schedule_a_copy_only_as_the_dedup_reads_it(tmp_path):
    """The restatement key reads amounts through amount_sql(), as the sums and DEDUPED_RECEIPTS
    do. Its own Python copy of that rule stripped whitespace that TRIM keeps (here a vertical
    tab), so a schedule-A copy the sum cannot read still "restated" the late gift: every
    schedule counted the two as different gifts, while the default counted the late one as on
    schedule A."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("A-100001", "Saltmarsh", "Tobiah", "2500\x0b", "A", date="9/18/2026")
                 + rcpt("F496P3-100001", "Saltmarsh", "Tobiah", "2500", "F496P3", filing=F496,
                        date="9/18/2026"))

    every = run(root, "filer_total", form_type="")
    assert every.value == 10500.0 and "1 more gift(s) with no readable amount" in every.note
    total = run(root, "filer_total")
    assert not total.found and total.value is None, total.note
    assert "1 late-report entry ($2,500.00; filing 9990663)" in total.note, total.note


def test_another_filers_460_restates_nothing_of_ours(tmp_path):
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("L-1", "Saltmarsh", "Tobiah", "700", date="11/1/2026"))
    assert not run(root, "filer_total").found


def test_a_late_entry_nobody_can_place_or_add_up_stays_pending(tmp_path):
    """A date that cannot be read cannot be shown to fall in a filed 460's period, and an
    amount nobody stated could be anything."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("L-1", "Saltmarsh", "Tobiah", "300", date="")
                 + s497("L-2", "Quennell", "Ada", "", date="10/3/2026"))

    undated = run(root, "contributor_total", contributor="Saltmarsh", contributor_first="Tobiah")
    assert not undated.found and "late-report entry" in undated.note, undated.note

    got = run(root, "top_contributor")
    assert not got.found, f"an unstated late amount could not change the ranking? {got.value}"
    assert "Ada Quennell ($0 on schedule-A, a late amount nobody stated)" in got.note, got.note
    assert "1 with no readable amount" in got.note


def test_an_amended_late_report_counts_its_latest_amendment(tmp_path):
    """Amendment 1 corrects the amount. The superseded entry is not a second late gift."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("L-1", "Saltmarsh", "Tobiah", "2000")
                 + s497("L-1", "Saltmarsh", "Tobiah", "200", amend="1"))

    got = run(root, "filer_total", form_type="A")
    assert "1 late-report entry ($200.00; filing 9990664)" in got.detail, got.detail


def test_the_three_queries_agree(tmp_path):
    """A sum of contributor totals is the filer total, whether late gifts are pending (by
    schedule A, asked for by name) or restated (by default)."""
    pending = build(tmp_path / "pending", ON_SCHEDULE_A,
                    late=s497("L-1", "Saltmarsh", "Tobiah", "700"))
    restated = build(tmp_path / "restated", ON_SCHEDULE_A
                     + rcpt("A-3", "Saltmarsh", "Tobiah", "700", "A", date="6/1/2026"),
                     late=s497("L-1", "Saltmarsh", "Tobiah", "700", date="6/1/2026"))
    donors = [("Marwick", "Odile"), ("Fernhollow Growers PAC", ""), ("Saltmarsh", "Tobiah")]

    for root, extra in ((pending, {"form_type": "A"}), (restated, {})):
        found = [run(root, "contributor_total", contributor=last, contributor_first=first,
                     **extra) for last, first in donors]
        total = sum(r.value for r in found if r.found)
        assert total == run(root, "filer_total", **extra).value


def test_a_short_total_does_not_verify_green(tmp_path):
    """The false green: a figure recorded from the schedule-A total before the late gift was
    known reproduced exactly, and rendered verified."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Saltmarsh", "Tobiah", "700"))

    def cite(expected, **params):
        s = Source(url=calaccess.filing_url(F460), publisher="California Secretary of State",
                   author="California Secretary of State", source_type="official_record",
                   date="2026-07-31", snippet="monetary contributions received",
                   query=QueryCitation(name="calaccess.filer_total",
                                       params={"filer_id": FILER, **params}, expected=expected))
        return verify_query_source(s, root).verification

    short = cite("8000")
    assert short.status != "verified", short.reason
    assert "not a complete total" in short.reason
    # Named, the figure reproduces, and still goes to a person: the note alone rendered green.
    by_name = cite("8000", form_type="A")
    assert by_name.status == "human_review", by_name.reason
    assert "the query reproduces 8000.0, but it leaves out late-reported" in by_name.reason
    assert "filing 9990664 (Form 497): $700.00 in 1 entry" in by_name.reason, by_name.reason
    assert f"open {calaccess.filing_url(F497)}" in by_name.reason
    assert "Re-run: provenance query calaccess.filer_total" in by_name.reason
    assert by_name.query_run is not None, "stamped, as a mismatch is, for provenance judge"
    # A mismatch is still a mismatch, not a question for a person.
    assert cite("9000", form_type="A").status == "snippet_not_found"


def test_a_database_built_before_late_reports_were_loaded_refuses_the_default(tmp_path,
                                                                              monkeypatch):
    """It cannot see a Form 497, so it cannot say none is pending. A warning would let the
    short total render green (the ie_total lesson), so the default refuses until a rebuild."""
    wanted = {k: v for k, v in calaccess.WANTED.items() if k != "S497_CD"}
    monkeypatch.setattr(calaccess, "WANTED", wanted)
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Marwick", "Odile", "4000"))
    monkeypatch.undo()

    # Named or not: a figure that cannot be checked for late reports would render green.
    for name, params in (("contributor_total", {"contributor": "Fernhollow Growers PAC"}),
                         ("filer_total", {}), ("top_contributor", {})):
        for form_type in ({}, {"form_type": "A"}, {"form_type": ""}):
            with pytest.raises(calaccess.DegradedDatabase, match="database: provenance calaccess build"):
                run(root, name, **params, **form_type)
    # another schedule never depended on late reports: a miss, not a refusal
    assert not run(root, "filer_total", form_type="C").found


def test_an_export_without_late_reports_is_not_a_degraded_database(tmp_path):
    """The build looked for S497_CD and the export had none: nothing is pending, and nothing
    was missed."""
    root = build(tmp_path, ON_SCHEDULE_A, late=None)
    con = calaccess.connect(root)
    try:
        assert calaccess.late_reports_loaded(con)
    finally:
        con.close()
    assert run(root, "filer_total").value == 8000.0


def test_build_does_not_keep_a_named_figure_green_while_a_late_report_is_pending(tmp_path):
    """A claim file verified before the late report reached the export, or under a version that
    did not ask, re-runs at every build: the same rule `provenance verify` applies, applied there too."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Saltmarsh", "Tobiah", "700"))

    def claimed_green(root, **params):
        s = Source(url=calaccess.filing_url(F460), publisher="California Secretary of State",
                   author="California Secretary of State", source_type="official_record",
                   date="2026-07-31", snippet="monetary contributions received",
                   query=QueryCitation(name="calaccess.filer_total",
                                       params={"filer_id": FILER, **params}, expected="8000"))
        s.verification.status = "verified"
        s.verification.support = "supports"
        return revalidate_from_cache(s, root).verification

    held = claimed_green(root, form_type="A")
    assert held.status == "human_review", held.reason
    assert "re-running the query gives 8000.0, but it leaves out late-reported" in held.reason
    assert "filing 9990664 (Form 497): $700.00" in held.reason, held.reason
    assert held.support == "unreviewed", "a verdict about a settled figure must not ride along"

    # Once a 460 covering the late gift is on file, the same citation is green again.
    later = COVERS + (f"9990665\t0\tCVR\tF460\t{FILER}\tNeighbors for Example Valley"
                      f"\t7/1/2026{DAY}\t12/31/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")
    restated = build(tmp_path / "restated", ON_SCHEDULE_A,
                     late=s497("L-1", "Saltmarsh", "Tobiah", "700"), covers=later,
                     filings=FILINGS + f"{FILER}\t9990665\tF460\t1/31/2027{DAY}\n")
    assert claimed_green(restated, form_type="A").status == "verified"


# Late reports on filings of their own.
MANY = [str(f) for f in range(9990680, 9990687)]
MANY_FILINGS = FILINGS + "".join(f"{FILER}\t{f}\tF497\t10/5/2026{DAY}\n" for f in MANY)


def test_a_named_figure_names_its_late_reports_largest_first(tmp_path):
    """The reason becomes a claim file's, so it names five and says how many more; `provenance query`
    prints the rest. An amount nobody stated could be anything, so it comes first."""
    amounts = ["100", "", "900", "300", "-2000", "500", "200"]
    late = "".join(s497(f"L-{i}", "Saltmarsh", "Tobiah", a, filing=f)
                   for i, (a, f) in enumerate(zip(amounts, MANY)))
    root = build(tmp_path, ON_SCHEDULE_A, late=late, filings=MANY_FILINGS)

    got = run(root, "filer_total", form_type="A")
    assert [r.filing_id for r in got.late] == [int(MANY[i]) for i in (1, 4, 2, 5, 3, 6, 0)]
    assert ("filing 9990681 (Form 497): $0.00 in 1 entry, 1 with no readable amount"
            in got.unsettled), got.unsettled
    named = [f for f in MANY if f"filing {f} " in got.unsettled]
    assert sorted(named) == sorted(MANY[i] for i in (1, 4, 2, 5, 3)), got.unsettled
    assert "and 2 more late report(s), which `provenance query` lists" in got.unsettled, got.unsettled


def test_a_value_unsettled_two_ways_names_both(tmp_path):
    """#31's unrestated amendments and these late reports are one `unsettled`: a figure can
    count a row a later amendment may have withdrawn and leave out a late gift, and the reason
    names both, each with its own filings. One late report is not "and -4 more"."""
    from types import SimpleNamespace

    share = SimpleNamespace(describe=lambda: "filing 9990601 amendment 0", amount=4000.0,
                            rows=1, cite_url=calaccess.filing_url("9990601"), does="rests on",
                            unread=0)
    report = queries.LateReport(filing_id=int(F497), amend_id="0", forms=frozenset({"F497P1"}),
                                also=frozenset(), amount=2500.0, gross=2500.0, entries=1,
                                unread=0)
    both = queries.QueryResult(value=8000.0, unrestated=[share], late=[report]).unsettled
    assert both.startswith("it counts rows a later amendment may have withdrawn"), both
    assert " It also leaves out late-reported contributions" in both, both
    assert "filing 9990601" in both and f"filing {F497} (Form 497): $2,500.00" in both
    assert "more late report" not in both and "more filing" not in both
    late_only = queries.QueryResult(value=8000.0, late=[report]).unsettled
    assert late_only.startswith("it leaves out late-reported contributions"), late_only


def test_one_late_report_is_named_once_with_every_entry_it_holds(tmp_path):
    """Two gifts on one Form 497, and one gift reported on both late forms under one TRAN_ID
    base: two filings to open, not three entries."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-5", "Quennell", "Ada", "250", "F496P3", filing=F496,
                        date="10/1/2026"),
                 late=s497("L-1", "Saltmarsh", "Tobiah", "700")
                 + s497("L-2", "Marwick", "Odile", "400", date="10/3/2026")
                 + s497("L-5", "Quennell", "Ada", "250", date="10/1/2026", filing="9990666"),
                 filings=FILINGS + f"{FILER}\t9990666\tF497\t10/2/2026{DAY}\n")

    got = run(root, "filer_total", form_type="A")
    assert "filing 9990664 (Form 497): $1,100.00 in 2 entries" in got.unsettled, got.unsettled
    # The earliest filing names the gift reported on both, with its own form, and says where
    # else it is: labelled "Form 496 Part 3 and Form 497", it named a form that filing is not.
    assert ("filing 9990663 (Form 496 Part 3; also on filing 9990666): $250.00 in 1 entry"
            in got.unsettled), got.unsettled
    assert len(got.late) == 2


def test_a_late_report_names_the_amendment_its_entries_come_from(tmp_path):
    """The link opens a filing as first filed; a gift an amendment added is not there."""
    root = build(tmp_path, ON_SCHEDULE_A,
                 late=s497("L-1", "Saltmarsh", "Tobiah", "700")
                 + s497("L-1", "Saltmarsh", "Tobiah", "700", amend="1")
                 + s497("L-2", "Quennell", "Ada", "400", amend="1"))

    got = run(root, "filer_total", form_type="A")
    assert "filing 9990664 amendment 1 (Form 497): $1,100.00 in 2 entries" in got.unsettled, (
        got.unsettled)


def test_a_stated_zero_late_amount_holds_nothing(tmp_path):
    """$0 changes no total. Held, a settled figure sat in human_review until the next 460."""
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Marwick", "Odile", "0"))

    assert run(root, "filer_total").value == 8000.0
    for name, params in (("filer_total", {}), ("contributor_total", {"contributor": "Marwick"})):
        got = run(root, name, form_type="A", **params)
        assert got.found and not got.unsettled, got.note


def test_a_ranking_its_own_detail_calls_unsettled_does_not_verify(tmp_path):
    """Five givers tied, and a late gift nobody in the tie made. While the ranking read only
    four rows (#79), the fifth read as a contender with no late entry of its own, and holding
    only the contenders' late reports held none: green, beside "they could change the
    ranking". The detail and the hold must agree either way."""
    words = ["One", "Two", "Three", "Four", "Five"]
    root = build(tmp_path, "".join(rcpt(f"A-{i}", f"Example Giver {w}", "", "1000", "A")
                                   for i, w in enumerate(words)),
                 late=s497("L-1", "Saltmarsh", "Tobiah", "5"))

    got = run(root, "top_contributor", form_type="A")
    assert ("they could change the ranking" in got.detail) == bool(got.unsettled), got.note


def test_provenance_query_says_a_figure_will_not_verify_before_it_is_recorded(tmp_path):
    from typer.testing import CliRunner

    from provenance import cli

    late = "".join(s497(f"L-{i}", "Saltmarsh", "Tobiah", str(100 * (i + 1)), filing=f)
                   for i, f in enumerate(MANY))
    root = build(tmp_path, ON_SCHEDULE_A, late=late, filings=MANY_FILINGS)

    def query(form_type):
        return CliRunner().invoke(cli.app, [
            "query", "calaccess.filer_total", "--param", f"filer_id={FILER}", "--param",
            f"form_type={form_type}", "--data", str(root), "--cache", str(root)])

    r = query("A")
    assert r.exit_code == 0, r.output
    assert "Will not verify: it leaves out late-reported contributions" in r.output, r.output
    # the two smallest, past the five the reason names, are listed here
    for f, amount in ((MANY[1], "$200.00"), (MANY[0], "$100.00")):
        assert f"  filing {f} (Form 497): {amount} in 1 entry" in r.output, r.output
    assert "Will not verify" not in query("C").output


def test_a_ranking_names_the_late_report_that_could_move_it(tmp_path):
    """Five $1,000 late gifts to $100 givers cannot reach a $50,000 tie; a $100 one to a leader
    breaks it. Held for every late report, largest first, the reason named the five and left
    the one to open as "and 1 more"."""
    words = ["One", "Two", "Three", "Four", "Five"]
    receipts = (rcpt("A-1", "Marwick", "Odile", "50000", "A")
                + rcpt("A-2", "Fernhollow Growers PAC", "", "50000", "A")
                + "".join(rcpt(f"A-{10 + i}", f"Example Giver {w}", "", "100", "A")
                          for i, w in enumerate(words)))
    late = ("".join(s497(f"L-{i}", f"Example Giver {w}", "", "1000", filing=MANY[i])
                    for i, w in enumerate(words))
            + s497("L-9", "Marwick", "Odile", "100", filing=MANY[5]))
    root = build(tmp_path, receipts, late=late, filings=MANY_FILINGS)

    got = run(root, "top_contributor", form_type="A")
    assert got.value == "Fernhollow Growers PAC | Odile Marwick", got.note
    assert [r.filing_id for r in got.late] == [int(MANY[5])], got.unsettled
    assert f"filing {MANY[5]} (Form 497): $100.00 in 1 entry" in got.unsettled


def test_a_gift_and_its_correction_are_not_a_report_to_skip(tmp_path):
    """Netted, a $5,000 gift and a $5,000 correction on one report read $0.00 and sorted
    last, past the five the reason names. Either could be what a 460 restates."""
    late = ("".join(s497(f"L-{i}", "Saltmarsh", "Tobiah", str(100 * (i + 1)), filing=MANY[i])
                    for i in range(5))
            + s497("L-7", "Quennell", "Ada", "5000", filing=MANY[5])
            + s497("L-8", "Quennell", "Ada", "-5000", filing=MANY[5], date="10/4/2026"))
    root = build(tmp_path, ON_SCHEDULE_A, late=late, filings=MANY_FILINGS)

    got = run(root, "filer_total", form_type="A")
    assert got.late[0].filing_id == int(MANY[5]), got.unsettled
    assert (f"filing {MANY[5]} (Form 497): $0.00 in 2 entries, $10,000.00 before netting"
            in got.unsettled), got.unsettled


def test_what_verifies_is_decided_by_the_query_not_by_its_wording(tmp_path):
    """`QueryResult.unsettled` is left out of the definition fingerprint as message text. That
    holds only while it formats `late` and decides nothing, so a result is unsettled exactly
    when the query held a late report against it: the decision stays in the query's own code,
    which the fingerprint gate covers."""
    root = build(tmp_path, ON_SCHEDULE_A
                 + rcpt("F496P3-7", "Saltmarsh", "Tobiah", "2000", "F496P3", filing=F496,
                        date="9/18/2026"),
                 late=s497("L-1", "Marwick", "Odile", "4000") + s497("L-2", "Quennell", "Ada",
                                                                       "0", date="10/3/2026"))
    asked = [("filer_total", {}), ("top_contributor", {})] + [
        ("contributor_total", {"contributor": last, "contributor_first": first})
        for last, first in (("Marwick", "Odile"), ("Saltmarsh", "Tobiah"), ("Quennell", "Ada"),
                            ("Fernhollow Growers PAC", ""))]
    seen = set()
    for name, params in asked:
        for form_type in ({}, {"form_type": "A"}, {"form_type": ""}, {"form_type": "C"}):
            got = run(root, name, **params, **form_type)
            assert bool(got.unsettled) == bool(got.late), (name, params, form_type, got.note)
            seen.add(bool(got.late))
    assert seen == {True, False}
