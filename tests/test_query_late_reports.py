"""Late contributions that no schedule A restates yet.

A contribution received in the weeks before an election is reported within 24 hours on a late
report: Form 497 Part 1, or Form 496 Part 3 for a committee making independent expenditures. It
reaches schedule A only when the Form 460 covering its date is filed. The contribution queries
count schedule A, so until then the gift was left out without a word: `top_contributor` could
name the wrong donor "the largest contributor", and every total came out short, green.

They never add a late entry to a total. With form_type unset they refuse while one could change
the answer, and form_type=A gives the schedule-A figure alone, naming what it leaves out.
"""

from __future__ import annotations

import zipfile

import pytest

from vgpipe import calaccess, queries
from vgpipe.models import QueryCitation, Source
from vgpipe.verify import verify_query_source

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
COVERS = (CVR_HEAD
          + f"{F460}\t0\tCVR\tF460\t{FILER}\tNeighbors for Example Valley\t1/1/2026{DAY}"
            f"\t6/30/2026{DAY}\t11/3/2026{DAY}\t\t\t\n"
          # another filer's statement covering the late period: it restates nothing of ours
          + f"9990671\t0\tCVR\tF460\t{OTHER_FILER}\tExample Hills Committee\t7/1/2026{DAY}"
            f"\t12/31/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")


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


def build(root, receipts, late="", covers=COVERS):
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RCPT_HEAD + receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
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

    # Asked for schedule A by name, the figure is schedule A's, and says what it leaves out.
    a = run(root, "filer_total", form_type="A")
    assert a.value == 8000.0
    assert "filing 9990663) not yet restated on a Form 460 schedule A, not counted" in a.detail
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

    her = run(root, "contributor_total", contributor="Marwick", contributor_first="Odile")
    assert not her.found and "$3,000.00 across 1 itemized schedule-A gift(s)" in her.note
    assert "filing 9990664" in her.note, her.note
    assert run(root, "contributor_total", contributor="Marwick", contributor_first="Odile",
               form_type="A").value == 3000.0


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
        zf.writestr("CalAccess/DATA/S497_CD.TSV", head + s497("L-1", "Marwick", "Odile", "4000"))
    calaccess.build(tmp_path)

    with pytest.raises(calaccess.DegradedDatabase, match="lacks a column"):
        run(tmp_path, "filer_total")
    a = run(tmp_path, "filer_total", form_type="A")
    assert a.value == 8000.0 and "Form 497 late reports not checked" in a.detail


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
    """No cover record says what the 460 covers, but its schedule-A row carries the same
    transaction as the Form 496 Part 3 row (the cross-form key): that is a restatement."""
    root = build(tmp_path, rcpt("A-100001", "Fernhollow Growers PAC", "", "2500", "A")
                 + rcpt("F496P3-100001", "Fernhollow Growers PAC", "", "2500", "F496P3",
                        filing=F496),
                 covers="")

    for name, params in (("contributor_total", {"contributor": "Fernhollow Growers PAC"}),
                         ("filer_total", {})):
        got = run(root, name, **params)
        assert got.value == 2500.0, f"{name}: {got.note}"
        assert "late-report" not in got.detail
    assert run(root, "top_contributor").value == "Fernhollow Growers PAC"


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
    by_name = cite("8000", form_type="A")
    assert by_name.status == "verified", by_name.reason
    assert "not yet restated on a Form 460 schedule A, not counted" in by_name.context


def test_a_database_built_before_late_reports_were_loaded_refuses_the_default(tmp_path,
                                                                              monkeypatch):
    """It cannot see a Form 497, so it cannot say none is pending. A warning would let the
    short total render green (the ie_total lesson), so the default refuses until a rebuild."""
    wanted = {k: v for k, v in calaccess.WANTED.items() if k != "S497_CD"}
    monkeypatch.setattr(calaccess, "WANTED", wanted)
    root = build(tmp_path, ON_SCHEDULE_A, late=s497("L-1", "Marwick", "Odile", "4000"))
    monkeypatch.undo()

    for name, params in (("contributor_total", {"contributor": "Fernhollow Growers PAC"}),
                         ("filer_total", {}), ("top_contributor", {})):
        with pytest.raises(calaccess.DegradedDatabase, match="uv run vg calaccess build"):
            run(root, name, **params)
    a = run(root, "filer_total", form_type="A")
    assert a.value == 8000.0 and "Form 497 late reports not checked" in a.detail
    assert a.detail.endswith("(uv run vg calaccess build)"), a.detail
    top = run(root, "top_contributor", form_type="A")
    assert top.detail.endswith("(uv run vg calaccess build)"), top.detail
    # other schedules never depended on late reports
    assert run(root, "filer_total", form_type="").value == 8000.0


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
