"""One giver filed under two names is not two contributors, and is not merged into one either.

Filers do not reliably split a name. One giver can be filed with the whole name in the
last-name field on one row ('Rue Quillon'/'') and split on the next ('Quillon'/'Rue'). The
ranking grouped by (last, first), so those were two contributors, each short of what the giver
gave: someone smaller could be named "the largest contributor", a tie could list "Rue Quillon |
Rue Quillon", and `contributor_total` asked for Quillon, Rue left the whole-name rows out. Each
value reproduced, so a citation of it verified green.

Merging the names would be a definition change that can join two different people (a bare
surname, a middle initial that marks a son). So another name that could be the giver's is held
against the result, as a pending late gift is: with form_type unset, a miss while it could
change the answer; form_type=A, the figure as filed, naming the other names.
"""

from __future__ import annotations

import sqlite3
import zipfile

from vgpipe import calaccess, queries
from vgpipe.models import QueryCitation, Source
from vgpipe.verify import verify_query_source

FILER = "9991140"
F460, F497 = "9991141", "9991142"
DAY = " 12:00:00 AM"

RCPT_HEAD = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP"
             "\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n")
S497_HEAD = ("FILING_ID\tAMEND_ID\tLINE_ITEM\tREC_TYPE\tFORM_TYPE\tTRAN_ID\tENTITY_CD"
             "\tENTY_NAML\tENTY_NAMF\tENTY_CITY\tCTRIB_EMP\tCTRIB_OCC\tELEC_DATE\tCTRIB_DATE"
             "\tDATE_THRU\tAMOUNT\tCMTE_ID\n")
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{F460}\tF460\t7/31/2026{DAY}\n"
           f"{FILER}\t{F497}\tF497\t10/5/2026{DAY}\n")
# The 460 covers the first half of 2026, so a late report after it is pending.
COVERS = ("FILING_ID\tAMEND_ID\tREC_TYPE\tFORM_TYPE\tFILER_ID\tFILER_NAML\tFROM_DATE\tTHRU_DATE"
          "\tELECT_DATE\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD\n"
          f"{F460}\t0\tCVR\tF460\t{FILER}\tExample Ridge Committee\t1/1/2026{DAY}"
          f"\t6/30/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")

WHOLE = ("Rue Quillon", "")     # the whole name in the last-name field
SPLIT = ("Quillon", "Rue")      # the same name, split
VARDLE = ("Vardle", "Tamsin")


def gift(i, name, amount):
    last, first = name
    return (f"{F460}\t0\tA-{i}\t{i}\t{last}\t{first}\t\t\t3/{i % 28 + 1}/2026{DAY}"
            f"\t{amount}\tA\n")


def late_gift(i, name, amount):
    last, first = name
    return (f"{F497}\t0\t1\tS497\tF497P1\tL-{i}\tIND\t{last}\t{first}\tExampleville\t\t"
            f"\t11/3/2026{DAY}\t10/2/2026{DAY}\t\t{amount}\t\n")


def build(root, gifts, late=None):
    """`gifts` and `late`: (name, amount) pairs."""
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV",
                    RCPT_HEAD + "".join(gift(i, n, a) for i, (n, a) in enumerate(gifts, 1)))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", S497_HEAD + "".join(
            late_gift(i, n, a) for i, (n, a) in enumerate(late or [], 1)))
    calaccess.build(root)
    return root


def run(root, name, **params):
    return queries.run(f"calaccess.{name}", {"filer_id": FILER, **params}, root)


def cite(root, name, expected, **params):
    s = Source(url=calaccess.filing_url(F460), publisher="California Secretary of State",
               author="California Secretary of State", source_type="official_record",
               date="2026-07-31", snippet="monetary contributions received",
               query=QueryCitation(name=f"calaccess.{name}",
                                   params={"filer_id": FILER, **params}, expected=expected))
    return verify_query_source(s, root).verification


# --- top_contributor ---------------------------------------------------------------------


def test_one_giver_filed_two_ways_is_not_ranked_below_a_smaller_one(tmp_path):
    """The acceptance case: $3,000 and $2,500 under two filings of one name, behind $5,000."""
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (SPLIT, "2500")])

    got = run(root, "top_contributor")
    assert not got.found and got.value is None, f"ranked as two contributors: {got.value}"
    assert ("names ranked apart that could be one giver's could change the ranking: Rue "
            "Quillon as 'Quillon'/'Rue' ($2,500 on schedule-A, no late gift; could be one giver "
            "with 'Rue Quillon'/'' ($3,000))") in got.note, got.note
    assert "pass form_type=A to rank each name on schedule A as filed" in got.note

    a = run(root, "top_contributor", form_type="A")
    assert a.value == "Tamsin Vardle", a.note
    assert "not counted as one, could change the ranking: Rue Quillon as" in a.detail, a.detail


def test_a_ranking_names_filed_two_ways_could_change_does_not_verify_green(tmp_path):
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (SPLIT, "2500")])

    assert cite(root, "top_contributor", "Tamsin Vardle").status != "verified"
    as_filed = cite(root, "top_contributor", "Tamsin Vardle", form_type="A")
    assert as_filed.status == "verified", as_filed.reason
    assert "could be one giver with 'Rue Quillon'/''" in as_filed.context, as_filed.context


def test_a_tie_never_lists_one_name_twice(tmp_path):
    """Two filings of one name, tied: listed as "Rue Quillon | Rue Quillon", they read as one
    donor, and together they are no tie at all."""
    root = build(tmp_path, [(WHOLE, "2000"), (SPLIT, "2000"), (VARDLE, "1000")])

    got = run(root, "top_contributor")
    assert not got.found, f"a tie of one name filed two ways stood: {got.value}"
    assert "could be one giver with" in got.note, got.note

    a = run(root, "top_contributor", form_type="A")
    assert a.value == ("Rue Quillon (filed 'Quillon'/'Rue') | "
                       "Rue Quillon (filed 'Rue Quillon'/'')"), a.value
    assert a.detail.startswith("2-WAY TIE"), a.detail


def test_another_name_for_a_tied_leader_breaks_the_tie(tmp_path):
    """$100 under a middle initial: counted as the same giver, they lead alone."""
    root = build(tmp_path, [(SPLIT, "4750"), (VARDLE, "4750"), (("Quillon", "Rue M"), "100")])

    got = run(root, "top_contributor")
    assert not got.found, f"a tie another name could break stood: {got.value}"
    assert ("Rue Quillon as 'Quillon'/'Rue' ($4,750 on schedule-A, no late gift; could be one "
            "giver with 'Quillon'/'Rue M' ($100))") in got.note, got.note


def test_a_lone_leaders_other_names_are_named_when_they_cannot_change_it(tmp_path):
    root = build(tmp_path, [(SPLIT, "5000"), (("Quillon", "Rue M"), "100"), (VARDLE, "3000")])

    got = run(root, "top_contributor")
    assert got.value == "Rue Quillon", got.note
    assert got.detail == ("$5,000 across 1 schedule-A gift(s); Rue Quillon could also be filed "
                          "as 'Quillon'/'Rue M' ($100), not added — they cannot change the "
                          "ranking"), got.detail


def test_a_giver_filed_two_ways_far_below_the_top_changes_nothing(tmp_path):
    root = build(tmp_path, [(VARDLE, "10000"), (WHOLE, "2000"), (SPLIT, "2000")])

    got = run(root, "top_contributor")
    assert got.value == "Tamsin Vardle" and got.detail == "$10,000 across 1 schedule-A gift(s)"


def test_a_bare_surname_is_one_givers_not_every_givers(tmp_path):
    """A bare 'Quillon' could be Rue's or Ada's, not both, and an unnamed gift could be
    anyone's, not everyone's. Summing every name that could be a giver's would put Rue at
    $6,700 and refuse; the most one giver could hold is $3,700, below $4,000."""
    names = [(VARDLE, "4000"), (SPLIT, "3000"), (("Quillon", "Ada"), "3000")]
    root = build(tmp_path / "short", names + [(("Quillon", ""), "500"), (("", ""), "200")])
    got = run(root, "top_contributor")
    assert got.value == "Tamsin Vardle", got.note

    # $1,500 bare could lift either one past the top, and the refusal says which name does it.
    root = build(tmp_path / "reach", names + [(("Quillon", ""), "1500")])
    got = run(root, "top_contributor")
    assert not got.found, got.value
    assert ("Ada Quillon as 'Quillon'/'Ada' ($3,000 on schedule-A, no late gift; could be one "
            "giver with 'Quillon'/'' ($1,500))") in got.note, got.note
    assert "Rue Quillon as 'Quillon'/'Rue'" in got.note, got.note


def test_a_late_giver_filed_two_ways_is_one_giver(tmp_path):
    """No schedule-A row for them, and two late reports: a bare surname and a split name. Each
    was a new giver short of the top, so the ranking stood."""
    root = build(tmp_path, [(VARDLE, "5000")],
                 late=[(("Quillon", ""), "3000"), (SPLIT, "2500")])

    got = run(root, "top_contributor")
    assert not got.found, f"a late giver filed two ways was two givers: {got.value}"
    assert ("Quillon as 'Quillon'/'' ($0 on schedule-A, $3,000 late; could be one giver with "
            "'Quillon'/'Rue' ($0, $2,500 late))") in got.note, got.note
    # Neither name is on schedule A: the late reports are the cause, and so is the remedy.
    assert "2 late-report entries" in got.note and "names ranked apart" not in got.note
    assert "pass form_type=A to rank schedule A alone" in got.note, got.note


def test_a_late_gift_that_cannot_change_it_is_not_blamed(tmp_path):
    """The names could change the ranking and a $10 late gift could not. Both were blamed."""
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (SPLIT, "2500")],
                 late=[(("Zed", "Ada"), "10")])

    got = run(root, "top_contributor")
    assert not got.found and "late-report" not in got.note, got.note
    assert "names ranked apart that could be one giver's could change" in got.note
    a = run(root, "top_contributor", form_type="A")
    assert "not counted — they cannot change the ranking" in a.detail, a.detail
    assert "not counted as one, could change the ranking" in a.detail, a.detail


def test_a_late_gift_and_a_name_that_can_only_change_it_together_are_both_named(tmp_path):
    """$3,000 + $1,000 filed another way + $1,500 late passes $5,000; no two of them do."""
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (SPLIT, "1000")],
                 late=[(SPLIT, "1500")])

    got = run(root, "top_contributor")
    assert not got.found, got.value
    assert "1 late-report entry" in got.note and "names ranked apart" in got.note, got.note


def test_one_chain_is_one_line_in_a_refusal(tmp_path):
    """A name holding nothing is still on its giver's chain: listed apart, it read as a second
    giver, and "and N more" counted it."""
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "4000"), (SPLIT, "0"),
                            (("Quillon", ""), "1500")])

    got = run(root, "top_contributor")
    assert not got.found, got.value
    assert got.note.count("could be one giver with") == 1, got.note


def test_a_missing_last_name_groups_with_a_blank_one(tmp_path):
    """A NULL name part read back as None, which cannot be sorted against text. It is no name,
    as a blank is, so the two are one name as filed."""
    root = build(tmp_path, [(VARDLE, "5000"), (("", "Rue"), "3000"), (("", "Rue"), "2500")])
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE RCPT_CD SET CTRIB_NAML = NULL WHERE TRAN_ID = 'A-3'")
    con.commit()
    con.close()

    got = run(root, "top_contributor")
    assert got.value == "Rue" and got.detail.startswith("$5,500 across 2"), got.note


def test_a_refusal_reads_the_same_whatever_order_the_rows_come_in(tmp_path):
    """Two givers each filed two ways reach as high: their order in the refusal is by name,
    not by which rows SQLite read first."""
    rows = [(VARDLE, "5000"), (WHOLE, "3000"), (SPLIT, "3000"),
            (("Bo Ennis", ""), "3000"), (("Ennis", "Bo"), "3000")]
    notes = {run(build(tmp_path / str(n), order), "top_contributor").note
             for n, order in enumerate((rows, rows[::-1], rows[2:] + rows[:2]))}
    assert len(notes) == 1, notes
    note = notes.pop()
    assert note.index("Bo Ennis as") < note.index("Rue Quillon as"), note


# --- contributor_total -------------------------------------------------------------------


def test_a_total_asked_for_one_filing_names_the_other(tmp_path):
    """The mirror image: asked for Quillon, Rue, it missed the rows filed whole."""
    root = build(tmp_path, [(SPLIT, "2500"), (WHOLE, "3000"), (VARDLE, "5000")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert not got.found and got.value is None, f"a short total was given: {got.value}"
    assert ("$2,500.00 across 1 itemized schedule-A gift(s), but 1 other name this giver could "
            "be filed under ('Rue Quillon'/'' $3,000.00 in 1 gift(s)) — not a complete total; "
            "pass form_type=A for the schedule-A figure under this name alone") in got.note

    a = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue",
            form_type="A")
    assert a.value == 2500.0, a.note
    assert a.detail.endswith("('Rue Quillon'/'' $3,000.00 in 1 gift(s)), not counted — a figure "
                             "for this name as filed, NOT a complete total; do not word it as "
                             "one"), a.detail

    whole = run(root, "contributor_total", contributor="Rue Quillon")
    assert not whole.found and "'Quillon'/'Rue' $2,500.00" in whole.note, whole.note


def test_a_short_total_under_one_name_does_not_verify_green(tmp_path):
    root = build(tmp_path, [(SPLIT, "2500"), (WHOLE, "3000")])

    short = cite(root, "contributor_total", "2500", contributor="Quillon",
                 contributor_first="Rue")
    assert short.status != "verified", short.reason
    as_filed = cite(root, "contributor_total", "2500", contributor="Quillon",
                    contributor_first="Rue", form_type="A")
    assert as_filed.status == "verified", as_filed.reason
    assert "a figure for this name as filed" in as_filed.context


def test_a_different_person_sharing_the_surname_is_not_another_name(tmp_path):
    root = build(tmp_path, [(SPLIT, "2500"), (("Quillon", "Ada"), "3000")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert got.value == 2500.0 and "other name" not in got.detail, got.note


def test_a_miss_names_the_name_the_giver_is_filed_under(tmp_path):
    root = build(tmp_path, [(WHOLE, "3000"), (VARDLE, "5000")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert not got.found and got.rows == 0
    assert "1 other name this giver could be filed under ('Rue Quillon'/''" in got.note, got.note


def test_more_other_names_than_one_lookup_holds_are_all_named(tmp_path):
    """The other names are summed in chunks, since each is two SQL variables."""
    middles = [(f"Rue X{i:03d} Quillon", "") for i in range(600)]
    root = build(tmp_path, [(SPLIT, "2500")] + [(m, "10") for m in middles])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue",
              form_type="A")
    assert got.value == 2500.0
    assert "600 other names this giver could be filed under ('Rue X000 Quillon'/''" in got.detail


def test_an_unnamed_gift_could_be_anyones(tmp_path):
    """As an unnamed late gift is: it could be this giver's, so the default total is not
    complete, and the as-filed figure says so."""
    root = build(tmp_path, [(VARDLE, "5000"), (("", ""), "200")])

    assert not run(root, "contributor_total", contributor="Vardle",
                   contributor_first="Tamsin").found
    a = run(root, "contributor_total", contributor="Vardle", contributor_first="Tamsin",
            form_type="A")
    assert a.value == 5000.0 and "''/'' $200.00" in a.detail, a.detail
