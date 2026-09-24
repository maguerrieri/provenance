"""A tie for largest contributor is every contributor at the top, not the first four.

`top_contributor` fetched the grouped totals with `LIMIT 4` and built its tie set from those
rows. Five or more contributors tied at the top came back as a `4-WAY TIE` naming four of them,
and nothing ordered equal totals, so which four was SQLite's choice. The value reproduced for as
long as that choice held, so a citation recorded from it verified while naming an incomplete set.
Ties at the top are common: every donor who gave the contribution limit shares one total.
"""

from __future__ import annotations

import zipfile

from vgpipe import calaccess, queries
from vgpipe.models import QueryCitation, Source
from vgpipe.verify import verify_query_source

FILER = "9990790"
F460, F497 = "9990791", "9990792"
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
          f"{F460}\t0\tCVR\tF460\t{FILER}\tExample Harbor Committee\t1/1/2026{DAY}"
          f"\t6/30/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")

# Five givers at the limit, one below it.
AT_LIMIT = ["Quillon", "Brasket", "Oddvar", "Tamsley", "Eskerby"]
LIMIT = "4750"


def gift(i, last, amount, first="Rue"):
    return (f"{F460}\t0\tA-{i}\t{i}\t{last}\t{first}\t\t\t3/{i % 28 + 1}/2026{DAY}"
            f"\t{amount}\tA\n")


def late_gift(last, amount, first="Rue", tran="L-1"):
    return (f"{F497}\t0\t1\tS497\tF497P1\t{tran}\tIND\t{last}\t{first}\tExampleville\t\t"
            f"\t11/3/2026{DAY}\t10/2/2026{DAY}\t\t{amount}\t\n")


def build(root, receipts, late=None):
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RCPT_HEAD + "".join(receipts))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        if late is not None:
            zf.writestr("CalAccess/DATA/S497_CD.TSV", S497_HEAD + late)
    calaccess.build(root)
    return root


def top(root, **params):
    return queries.run("calaccess.top_contributor", {"filer_id": FILER, **params}, root)


def five_way(root, late=None):
    return build(root, [gift(i, last, LIMIT) for i, last in enumerate(AT_LIMIT, 1)]
                 + [gift(9, "Pellow", "250")], late)


FIVE = " | ".join(sorted(f"Rue {last}" for last in AT_LIMIT))


def test_a_five_way_tie_names_all_five(tmp_path):
    got = top(five_way(tmp_path))
    assert got.value == FIVE, f"the tie set was cut short: {got.value!r}"
    assert got.detail.startswith("5-WAY TIE at $4,750"), got.detail
    assert got.rows == 5


def test_a_tie_reads_the_same_whatever_order_the_rows_come_in(tmp_path):
    """Ties among more rows than the old LIMIT, filed in several orders: one value."""
    givers = [f"Tiedrow{c}" for c in "ABCDEFG"]
    orders = [givers, givers[::-1], givers[3:] + givers[:3], givers[1::2] + givers[::2]]
    values = set()
    for n, order in enumerate(orders):
        rows = [gift(i, last, LIMIT) for i, last in enumerate(order, 1)]
        got = top(build(tmp_path / f"order{n}", [gift(20, "Pellow", "250")] + rows))
        assert got.detail.startswith("7-WAY TIE"), got.detail
        values.add(got.value)
    assert values == {" | ".join(sorted(f"Rue {last}" for last in givers))}, values


def test_a_tie_is_within_half_a_cent_and_no_wider(tmp_path):
    """A figure is exact to the half cent (TOLERANCE), so a tie is too: a total a fifth of a
    cent short ties, and one a cent short does not. Not float noise: SQLite's SUM is
    compensated, so gifts adding to the limit come out exact."""
    root = build(tmp_path, [gift(i, last, LIMIT) for i, last in enumerate(AT_LIMIT, 1)]
                 + [gift(10, "Vantner", "4749.7"), gift(11, "Vantner", "0.298")]
                 + [gift(12, "Corriby", "4749.99")])

    got = top(root)
    assert got.detail.startswith("6-WAY TIE"), got.detail
    assert "Rue Vantner" in got.value and "Rue Corriby" not in got.value, got.value


def test_a_large_tie_is_listed_whole(tmp_path):
    """Not refused: every name in the tie is a true, reproducible answer, and the detail
    already says it is no single largest contributor. A cap would be an arbitrary number that
    turns that answer into a miss."""
    givers = [f"Manyway{i:02d}" for i in range(40)]
    got = top(build(tmp_path, [gift(i, last, LIMIT) for i, last in enumerate(givers, 1)]))
    assert got.value.split(" | ") == sorted(f"Rue {last}" for last in givers)
    assert got.detail.startswith("40-WAY TIE"), got.detail


def test_a_citation_of_four_of_five_tied_does_not_verify(tmp_path):
    """The false green: a value naming four of the five reproduced while SQLite kept picking
    the same four."""
    root = five_way(tmp_path)

    def cite(expected):
        s = Source(url=calaccess.filing_url(F460), publisher="California Secretary of State",
                   author="California Secretary of State", source_type="official_record",
                   date="2026-07-31", snippet="monetary contributions received",
                   query=QueryCitation(name="calaccess.top_contributor",
                                       params={"filer_id": FILER}, expected=expected))
        return verify_query_source(s, root).verification

    four = " | ".join(FIVE.split(" | ")[:4])
    assert cite(four).status != "verified"
    assert cite(FIVE).status == "verified", cite(FIVE).reason


def test_the_late_report_check_weighs_every_tied_contributor(tmp_path):
    """A late gift too small to reach the top leaves a five-way tie standing. With four in the
    tie set, the fifth tied contributor was judged as an outsider who could reach it, and with
    form_type=A the tie was held for a person over a late report that cannot move it."""
    root = five_way(tmp_path, late=late_gift("Pellow", "300"))

    got = top(root)
    assert got.value == FIVE, got.note
    assert "not counted — they cannot change the ranking" in got.detail, got.detail
    a = top(root, form_type="A")
    assert a.value == FIVE and "they cannot change the ranking" in a.detail, a.detail
    assert not a.late, a.unsettled


def test_a_late_gift_to_any_tied_contributor_breaks_the_tie(tmp_path):
    """By default a miss; with form_type=A the tie as filed, held for a person, naming the late
    report that breaks it."""
    for last in AT_LIMIT:
        root = five_way(tmp_path / last, late=late_gift(last, "100"))
        got = top(root)
        assert not got.found, f"a tie {last}'s late gift breaks was reported: {got.value}"
        assert f"Rue {last} ($4,750 on schedule-A, $100 late)" in got.note, got.note
        assert FIVE in got.note, got.note
        a = top(root, form_type="A")
        assert a.value == FIVE, a.note
        assert [r.filing_id for r in a.late] == [int(F497)], a.unsettled


def test_a_tie_is_in_name_order_whatever_case_a_name_is_filed_in(tmp_path):
    """A group's name is spelled as whichever of its rows SQLite reads. Sorted with case, one
    tie read "Rue ABBOT | Rue Aaron" or "Rue Aaron | Rue Abbot" depending on that row, and a
    recorded value stopped reproducing."""
    for n, abbot in enumerate(("ABBOT", "Abbot", "abbot")):
        got = top(build(tmp_path / str(n), [gift(1, abbot, LIMIT), gift(2, "Aaron", LIMIT)]))
        assert got.value == f"Rue Aaron | Rue {abbot}", got.value


def test_a_tie_reads_the_same_whatever_padding_a_name_is_filed_with(tmp_path):
    """The ranking groups on trimmed names, but a group's name was shown from whichever of its
    rows SQLite read: "Rue  Abbot" (a padded first name) on one load, "Rue Abbot" on another."""
    padded = gift(1, "Abbot", "2000", first="Rue ")
    rows = [padded, gift(2, "Abbot", "2750"), gift(3, "Aaron", LIMIT)]
    for n, order in enumerate((rows, rows[::-1])):
        got = top(build(tmp_path / str(n), order))
        assert got.value == "Rue Aaron | Rue Abbot", got.value


# First names in the opposite order to the surnames, so name order as shown is not the SQL's.
CROSSED = [("Zollern", "Ansel"), ("Mortlake", "Birch"), ("Kestle", "Cato"),
           ("Dunmore", "Delphine"), ("Abernay", "Edda")]


def test_a_refusal_names_the_leaders_who_could_move_in_name_order(tmp_path):
    """A late gift with no name could be any of the five. They were a set, so which three the
    refusal named, and in what order, changed with the hash seed; and the SQL's order is by
    surname, not by the name as the tie shows it."""
    root = build(tmp_path, [gift(i, last, LIMIT, first=first)
                            for i, (last, first) in enumerate(CROSSED, 1)],
                 late=late_gift("", "100", first=""))

    got = top(root)
    assert not got.found, got.value
    shown = [f"{first} {last} ($4,750 on schedule-A, $100 late)" for last, first in CROSSED[:3]]
    assert f"ranking: {'; '.join(shown)}; and 2 more — not a settled ranking" in got.note, (
        got.note)


def test_a_refusal_names_late_givers_in_name_order_whatever_order_they_were_filed_in(tmp_path):
    """Two late givers on one report, the same day, each able to pass the top: filed in load
    order, they swapped places in the refusal from one export to the next."""
    for n, order in enumerate((("Wexley", "Brume"), ("Brume", "Wexley"))):
        late = "".join(late_gift(last, "6000", tran=f"L-{i}") for i, last in enumerate(order))
        got = top(five_way(tmp_path / str(n), late=late))
        assert not got.found, got.value
        assert ("ranking: Rue Brume ($0 on schedule-A, $6,000 late); Rue Wexley ($0 on "
                "schedule-A, $6,000 late)") in got.note, got.note


def test_a_refusal_orders_equal_reaches_by_name_to_the_cent(tmp_path):
    """Summed as floats, $6,000.01 and $0.02 reach 6000.030000000001: ranked on that, a giver
    whose name comes second went ahead of one who reached $6,000.03 exactly."""
    late = (late_gift("Wexley", "6000.01", tran="L-1") + late_gift("Wexley", "0.02", tran="L-2")
            + late_gift("Brume", "6000.03", tran="L-3"))
    got = top(five_way(tmp_path, late=late))
    assert not got.found, got.value
    assert ("ranking: Rue Brume ($0 on schedule-A, $6,000 late); Rue Wexley ($0 on "
            "schedule-A, $6,000 late)") in got.note, got.note
