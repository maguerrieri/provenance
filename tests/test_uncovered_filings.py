"""A filing can have rows in a fact table and no cover record at all.

The unrestated-amendment check (#31) compares a filing's latest amendment by its cover with the
latest its table has. With no cover there is nothing to compare, and the check used to read such
a filing as settled, so a figure resting on one verified green. A check that cannot compare
answers "not checked", never "fresh": the filing is named, and the figure goes to a person.

A cover table with no rows at all made every filing that case, and every receipt figure green.
It is refused now, like a database with no cover table.

Every filing here is synthetic, in the export's own formats: "M/D/YYYY 12:00:00 AM" dates and
integer amendment ids.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from provenance import calaccess, cli, queries
from provenance.models import QueryCitation, Source, Verification
from provenance.verify import revalidate_from_cache, verify_source

FILER = "8886100"
COVERED_460 = "8886101"     # schedule A at amendment 0, with its cover
UNCOVERED_460 = "8886102"   # schedule A at amendment 0, and no cover record at all
UNCOVERED_496 = "8886103"   # the Form 496 copy of COVERED_460's A-600001, with no cover
IE_COVERED = "8886201"      # an expenditure whose cover names the candidate
IE_UNCOVERED = "8886202"    # an expenditure with no cover, so it names nobody

RECEIPTS = (
    "FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP\tCTRIB_OCC"
    "\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n"
    f"{COVERED_460}\t0\tA-600001\t1\tBrambleworth PAC\t\t\t\t1/6/2026 12:00:00 AM\t2500\tA\n"
    f"{COVERED_460}\t0\tA-600002\t2\tTessaly\tOren\t\t\t1/9/2026 12:00:00 AM\t300\tA\n"
    f"{UNCOVERED_460}\t0\tA-600003\t1\tBrambleworth PAC\t\t\t\t2/4/2026 12:00:00 AM\t1200\tA\n"
    f"{UNCOVERED_496}\t0\tF496P3-600001\t1\tBrambleworth PAC\t\t\t\t1/6/2026 12:00:00 AM"
    "\t2500\tF496P3\n"
)
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{COVERED_460}\tF460\t1/31/2026 12:00:00 AM\n"
           f"{FILER}\t{UNCOVERED_460}\tF460\t2/28/2026 12:00:00 AM\n"
           f"{FILER}\t{UNCOVERED_496}\tF496\t1/7/2026 12:00:00 AM\n")
IES = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n"
       f"{IE_COVERED}\t0\tIE1\t1\t1800\t3/2/2026 12:00:00 AM\tmailer\n"
       f"{IE_UNCOVERED}\t0\tIE2\t1\t900\t3/4/2026 12:00:00 AM\tdigital ads\n")
COVER_HEAD = ("FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD"
              "\tFORM_TYPE\tFROM_DATE\tTHRU_DATE\n")
# A partial cover table: two of the five filings with rows have a cover.
COVERS = (COVER_HEAD
          + f"{COVERED_460}\t0\t{FILER}\tFriends of Example\t\t\t\tF460"
            "\t1/1/2026 12:00:00 AM\t1/31/2026 12:00:00 AM\n"
          + f"{IE_COVERED}\t0\t8886900\tCommittee for Example\tFairlowe\tLiesel\tS\tF496\t\t\n")
IE_PARAMS = {"candidate_last": "Fairlowe", "first": "Liesel", "stance": "support"}
BRAMBLEWORTH = {"filer_id": FILER, "contributor": "Brambleworth PAC"}
# Every citable CAL-ACCESS query that can count a row from a filing with no cover, with
# parameters that reach UNCOVERED_460. The rest join the covers, so a filing with none reaches
# no figure of theirs. A new query fails
# test_every_citable_query_flags_a_filing_with_no_cover until it is in one or the other.
PLANTED = {
    "calaccess.contributor_total": BRAMBLEWORTH,
    "calaccess.filer_total": {"filer_id": FILER},
    "calaccess.top_contributor": {"filer_id": FILER},
}
JOINS_COVERS = {"calaccess.ie_total": IE_PARAMS}
PHRASE = "it counts rows a later amendment may have withdrawn"


def _export(root: Path, covers: str | None = COVERS) -> Path:
    """The export above; `covers` None leaves the cover table out of it altogether."""
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RECEIPTS)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/S496_CD.TSV", IES)
        if covers is not None:
            zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    calaccess.build(root)
    return root


@pytest.fixture
def root(tmp_path):
    return _export(tmp_path)


@pytest.fixture
def empty(tmp_path):
    """A cover table with its header and no rows: what the test fixtures used to build."""
    return _export(tmp_path / "empty", covers=COVER_HEAD)


def cited(name, params, expected):
    return Source(url="https://committee.example/report", publisher="Example Committee",
                  author="", date="2026-02-28", source_type="primary_document",
                  snippet="itemized contributions",
                  query=QueryCitation(name=name, params=params, expected=expected))


def plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_a_filing_with_rows_and_no_cover_is_unsettled_not_settled(root):
    con = calaccess.connect(root)
    try:
        got = calaccess.unrestated_filings(
            con, "RCPT_CD", [COVERED_460, UNCOVERED_460, UNCOVERED_496, "8886999"])
        ies = calaccess.unrestated_filings(con, "S496_CD", [IE_COVERED, IE_UNCOVERED])
    finally:
        con.close()
    assert got == {UNCOVERED_460: calaccess.Unrestated(UNCOVERED_460, 0, None),
                   UNCOVERED_496: calaccess.Unrestated(UNCOVERED_496, 0, None)}, (
        "a covered filing is settled, one with no rows is not asked about, and one with rows "
        "and no cover has nothing to compare, which is not settled")
    assert set(ies) == {IE_UNCOVERED}


def test_a_total_resting_on_a_filing_with_no_cover_goes_to_human_review_naming_it(root):
    """The acceptance case: the number reproduces, and used to render green."""
    result = queries.run("calaccess.contributor_total", BRAMBLEWORTH, root)
    assert result.value == 3700.0, "the flag must never change the number"
    # the Form 496 copy too: schedule A picks the gift after the cross-form dedup (#131)
    assert [(u.filing_id, u.amount, u.rows) for u in result.unrestated] == [
        (UNCOVERED_496, 2500.0, 1), (UNCOVERED_460, 1200.0, 1)]

    v = verify_source(cited("calaccess.contributor_total", BRAMBLEWORTH, "3700"),
                      root).verification
    assert v.status == "human_review", "a figure resting on an unchecked filing is not green"
    assert PHRASE in v.reason, "the research skill tells an unsettled figure by this phrase"
    assert UNCOVERED_460 in v.reason and "$1,200.00" in v.reason
    assert calaccess.filing_url(UNCOVERED_460) in v.reason
    assert "this export has no cover record for it" in v.reason
    assert COVERED_460 not in v.reason, "a settled filing is not one to open"

    s = cited("calaccess.contributor_total", BRAMBLEWORTH, "3700")
    s.verification = Verification(status="verified", reason="verified earlier",
                                  context="contributor_total = 3700", support="supports")
    rebuilt = revalidate_from_cache(s, root).verification
    assert rebuilt.status == "human_review" and PHRASE in rebuilt.reason
    assert UNCOVERED_460 in rebuilt.reason and rebuilt.support == "unreviewed"


def test_a_total_from_covered_filings_still_verifies(root):
    tessaly = cited("calaccess.contributor_total",
                    {"filer_id": FILER, "contributor": "Tessaly", "contributor_first": "Oren"},
                    "300")
    assert verify_source(tessaly, root).verification.status == "verified"


def test_a_gift_restated_across_filings_is_flagged_by_its_uncovered_filing(root):
    """Counted once, and still resting on a Form 496 with no cover: which report stands is the
    same open question."""
    result = queries.run("calaccess.contributor_total", {**BRAMBLEWORTH, "form_type": ""}, root)
    assert result.value == 3700.0
    assert [(u.filing_id, u.amount) for u in result.unrestated] == [
        (UNCOVERED_496, 2500.0), (UNCOVERED_460, 1200.0)]


def test_every_citable_query_flags_a_filing_with_no_cover(root):
    """The flag is written into each query, so this is the gate that a new one has it: every
    registered CAL-ACCESS query names UNCOVERED_460, or joins the covers and so cannot count a
    row from a filing without one."""
    citable = {n for n in queries.REGISTRY if queries.dataset(n) == "CAL-ACCESS"}
    assert set(PLANTED) | set(JOINS_COVERS) == citable
    assert not set(PLANTED) & set(JOINS_COVERS)
    for name, params in PLANTED.items():
        assert UNCOVERED_460 in [u.filing_id
                                 for u in queries.run(name, params, root).unrestated], name
    for name, params in JOINS_COVERS.items():
        # the exemption's premise: the uncovered expenditure names no candidate, so no total
        got = queries.run(name, params, root)
        assert got.value == 1800.0 and got.unrestated == [], name


def test_a_cover_table_with_no_rows_is_refused_by_every_citable_query(empty):
    """Every filing in it has no cover, so every figure would name every filing it counts: a
    database that cannot check, not records to open. A rebuild from a complete export fixes
    it, as for a database with no cover table."""
    con = calaccess.connect(empty)
    try:
        assert calaccess.cover_problem(con) == calaccess.EMPTY_COVERS
    finally:
        con.close()
    for name, params in {**PLANTED, **JOINS_COVERS}.items():
        with pytest.raises(calaccess.DegradedDatabase,
                           match="has no rows.*rows a filing's later amendment dropped.*"
                                 "complete export"):
            queries.run(name, params, empty)
        v = verify_source(cited(name, params, "1"), empty).verification
        assert v.status != "verified" and "provenance calaccess build" in v.reason, name


def test_the_listings_cannot_check_a_cover_table_with_no_rows(empty, monkeypatch):
    """As for a database with no cover amendment ids: the listing keeps working as a finding
    aid, and a row nobody checked never reads as settled. The footer says which case it is,
    since a rebuild from the same zip fixes only one of them."""
    assert all(c.unrestated is None for c in calaccess.contributions_to(empty, FILER))
    monkeypatch.setattr(cli.con, "_width", 250)
    res = CliRunner().invoke(cli.app, ["calaccess", "contributions", FILER,
                                       "--cache", str(empty)], env={"COLUMNS": "250"})
    assert res.exit_code == 0, res.output
    out = plain(res.output)
    assert "cannot tell whether a filing's latest amendment dropped" in out
    assert "CVR_CAMPAIGN_DISCLOSURE_CD table has no rows" in out

    # Finds each candidate on its cover, so it lists nothing: without the footer that reads as
    # "no committee spent anything".
    assert calaccess.independent_expenditures(empty, "Fairlowe", first="Liesel") == []
    res = CliRunner().invoke(cli.app, ["calaccess", "independent-expenditures", "Fairlowe",
                                       "--first", "Liesel", "--cache", str(empty)],
                             env={"COLUMNS": "250"})
    assert res.exit_code == 0, res.output
    assert "CVR_CAMPAIGN_DISCLOSURE_CD table has no rows" in plain(res.output)


def test_the_listing_marks_a_gift_from_a_filing_with_no_cover(root, monkeypatch):
    gifts = {c.amount: c for c in calaccess.contributions_to(root, FILER)}
    assert set(gifts) == {2500.0, 1200.0, 300.0}, "a marked row is still listed"
    assert [(u.filing_id, u.cover_amend) for u in gifts[1200.0].unrestated] == [
        (UNCOVERED_460, None)]
    assert [u.filing_id for u in gifts[2500.0].unrestated] == [UNCOVERED_496]
    assert gifts[300.0].unrestated == ()

    # COLUMNS does not reach `cli.con` once another test has set its width outright
    monkeypatch.setattr(cli.con, "_width", 250)
    out = plain(CliRunner().invoke(cli.app, ["calaccess", "contributions", FILER,
                                             "--cache", str(root)],
                                   env={"COLUMNS": "250"}).output)
    assert f"{UNCOVERED_460}: no cover" in out and f"{UNCOVERED_496}: no cover" in out
    assert "2 row(s) come from a filing with no cover record" in out
    assert "did not restate" not in out, "no row here comes from an unrestated amendment"


def test_the_expenditure_listing_names_a_missing_cover_table(tmp_path, monkeypatch):
    """It finds each candidate on a cover, so with no cover table it has nothing to list. It
    used to fail on the missing view instead, and never reached the footer that says why."""
    root = _export(tmp_path, covers=None)
    assert calaccess.independent_expenditures(root, "Fairlowe", first="Liesel") == []
    monkeypatch.setattr(cli.con, "_width", 250)
    res = CliRunner().invoke(cli.app, ["calaccess", "independent-expenditures", "Fairlowe",
                                       "--first", "Liesel", "--cache", str(root)],
                             env={"COLUMNS": "250"})
    assert res.exit_code == 0, res.output
    assert "has no CVR_CAMPAIGN_DISCLOSURE_CD table" in plain(res.output)
