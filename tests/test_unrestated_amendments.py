"""A filing's latest amendment can carry a cover and no rows in a fact table.

The per-table "latest amendment" rule then counts an earlier amendment's rows, and the export
cannot say whether that is right: the later amendment withdrew them, or did not restate that
schedule. A figure counting such a row must not verify, a listing must mark it, and a database
that cannot check must say so.

Every filing here is synthetic, in the export's own formats: "M/D/YYYY 12:00:00 AM" dates and
integer amendment ids.
"""

from __future__ import annotations

import re
import sqlite3
import zipfile

import pytest
from typer.testing import CliRunner

from vgpipe import calaccess, cli, queries
from vgpipe.models import QueryCitation, Source, Verification
from vgpipe.verify import revalidate_from_cache, verify_source

FILER = "8880100"
SETTLED_460 = "8880101"      # amendments 0 and 1, and amendment 1 restates every row
DROPPED_496 = "8880102"      # rows only at amendment 0; the cover's latest amendment is 1
IE_DROPPED = "8880201"       # an expenditure only at amendment 0; the cover goes to 1
IE_SETTLED = "8880202"       # restated at amendment 1, the cover's latest
IE_UNREAD = "8880203"        # dropped the same way, but its amount is blank: never counted

RECEIPTS = (
    "FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP\tCTRIB_OCC"
    "\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n"
    f"{SETTLED_460}\t0\tT1\t1\tQuill Harbor PAC\t\t\t\t1/5/2026 12:00:00 AM\t1500\tA\n"
    f"{SETTLED_460}\t1\tT1\t1\tQuill Harbor PAC\t\t\t\t1/5/2026 12:00:00 AM\t1500\tA\n"
    f"{SETTLED_460}\t1\tT2\t2\tOstrander\tMina\t\t\t1/12/2026 12:00:00 AM\t250\tA\n"
    # the same gift as F496P3-200001 below: one gift, two filings
    f"{SETTLED_460}\t1\tA-200001\t3\tLindqvist Farms\t\t\t\t1/20/2026 12:00:00 AM\t2000\tA\n"
    f"{DROPPED_496}\t0\tF496P3-200001\t1\tLindqvist Farms\t\t\t\t1/20/2026 12:00:00 AM"
    "\t2000\tF496P3\n"
    f"{DROPPED_496}\t0\tF496P3-200002\t2\tQuill Harbor PAC\t\t\t\t1/22/2026 12:00:00 AM"
    "\t4000\tF496P3\n"
)
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{SETTLED_460}\tF460\t1/31/2026 12:00:00 AM\n"
           f"{FILER}\t{SETTLED_460}\tF460\t2/14/2026 12:00:00 AM\n"
           f"{FILER}\t{DROPPED_496}\tF496\t1/23/2026 12:00:00 AM\n"
           f"{FILER}\t{DROPPED_496}\tF496\t2/2/2026 12:00:00 AM\n")
IES = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n"
       f"{IE_DROPPED}\t0\tIE1\t1\t3000\t3/2/2026 12:00:00 AM\tmailer\n"
       f"{IE_SETTLED}\t0\tIE2\t1\t1200\t3/9/2026 12:00:00 AM\tdigital ads\n"
       f"{IE_SETTLED}\t1\tIE2\t1\t1200\t3/9/2026 12:00:00 AM\tdigital ads\n"
       f"{IE_UNREAD}\t0\tIE3\t1\t\t3/12/2026 12:00:00 AM\tphone bank\n")
# The 460 covers January, so the Form 496's January gifts are ones it had to restate: without
# a period they are late gifts no schedule A restates yet, and the schedule-A default refuses
# while one is pending (#66).
JANUARY = ("1/1/2026 12:00:00 AM", "1/31/2026 12:00:00 AM")
COVER_ROWS = [
    (SETTLED_460, "0", FILER, "Friends of Example", "", "", "", "F460", *JANUARY),
    (SETTLED_460, "1", FILER, "Friends of Example", "", "", "", "F460", *JANUARY),
    (DROPPED_496, "0", FILER, "Friends of Example", "", "", "", "F496", "", ""),
    (DROPPED_496, "1", FILER, "Friends of Example", "", "", "", "F496", "", ""),
    *[(f, a, "8880900", "Committee for Example", "Fairweather", "Ondine", "S", "F496", "", "")
      for f in (IE_DROPPED, IE_SETTLED, IE_UNREAD) for a in ("0", "1")],
]
IE_PARAMS = {"candidate_last": "Fairweather", "first": "Ondine", "stance": "support"}
# Every citable CAL-ACCESS query, with parameters that reach an unrestated filing, and which.
# A new query fails test_every_citable_query_flags_and_refuses until it is added here.
# Every schedule ("") where the default, schedule A, would leave out the Form 496 Part 3 rows.
PLANTED = {
    "calaccess.contributor_total": ({"filer_id": FILER, "contributor": "Quill Harbor PAC",
                                     "form_type": ""}, DROPPED_496),
    "calaccess.filer_total": ({"filer_id": FILER, "form_type": "F496P3"}, DROPPED_496),
    "calaccess.top_contributor": ({"filer_id": FILER, "form_type": ""}, DROPPED_496),
    "calaccess.ie_total": (IE_PARAMS, IE_DROPPED),
}


def _export(root, *, cover_amend_ids=True, covers=True):
    """The export above, with or without CVR_CAMPAIGN_DISCLOSURE_CD.AMEND_ID (a database built
    before that column was loaded), or with no cover table at all."""
    cols = ["FILING_ID", "AMEND_ID", "FILER_ID", "FILER_NAML", "CAND_NAML", "CAND_NAMF",
            "SUP_OPP_CD", "FORM_TYPE", "FROM_DATE", "THRU_DATE"]
    rows = COVER_ROWS
    if not cover_amend_ids:
        cols.remove("AMEND_ID")
        rows = [r[:1] + r[2:] for r in rows]
    cover_tsv = "\t".join(cols) + "\n" + "".join("\t".join(r) + "\n" for r in rows)
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RECEIPTS)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/S496_CD.TSV", IES)
        if covers:
            zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", cover_tsv)
    if cover_amend_ids or not covers:
        calaccess.build(root)
    else:
        with pytest.warns(calaccess.DegradedDatabaseWarning):
            calaccess.build(root)
    return root


@pytest.fixture
def root(tmp_path):
    return _export(tmp_path)


def cited(name, params, expected):
    return Source(url="https://committee.example/report", publisher="Example Committee",
                  author="", date="2026-02-14", source_type="primary_document",
                  snippet="itemized contributions",
                  query=QueryCitation(name=name, params=params, expected=expected))


def plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_only_a_filing_whose_latest_amendment_has_no_rows_is_unrestated(root):
    con = calaccess.connect(root)
    try:
        got = calaccess.unrestated_filings(con, "RCPT_CD", [SETTLED_460, DROPPED_496, "8880999"])
        ies = calaccess.unrestated_filings(con, "S496_CD", [IE_DROPPED, IE_SETTLED])
    finally:
        con.close()
    assert got == {DROPPED_496: calaccess.Unrestated(DROPPED_496, 0, 1)}, (
        "a filing restated at its latest amendment, or with no cover to compare, is settled")
    assert set(ies) == {IE_DROPPED}


def test_a_total_counting_a_dropped_row_goes_to_human_review_naming_the_filing(root):
    """The acceptance case: the number reproduces, and would have rendered green. It is still
    unknown whether the later amendment withdrew the $4,000 gift, so a person opens the
    filing, and the reason says which one and how much of the figure it is."""
    # every schedule (""): the default is schedule A, and the dropped gift is Form 496 Part 3
    params = {"filer_id": FILER, "contributor": "Quill Harbor PAC", "form_type": ""}
    result = queries.run("calaccess.contributor_total", params, root)
    assert result.value == 5500.0, "the flag must never change the number"
    assert [(u.filing_id, u.amount, u.rows) for u in result.unrestated] == [
        (DROPPED_496, 4000.0, 1)]

    v = verify_source(cited("calaccess.contributor_total", params, "5500"), root).verification
    assert v.status == "human_review", "a note alone would still render green"
    assert DROPPED_496 in v.reason and "$4,000.00" in v.reason
    assert calaccess.filing_url(DROPPED_496) in v.reason
    assert "amendment 0" in v.reason and "latest amendment (1) has none" in v.reason
    assert SETTLED_460 not in v.reason, "a settled filing is not one to open"
    assert "vg query" in v.reason and v.query_run is not None   # the re-run command


def test_a_settled_total_still_verifies(root):
    """Known-good totals are unchanged: nothing they count comes from such a filing."""
    mina = cited("calaccess.contributor_total",
                 {"filer_id": FILER, "contributor": "Ostrander", "contributor_first": "Mina"},
                 "250")
    assert verify_source(mina, root).verification.status == "verified"
    # schedule A only: the Form 496's rows are F496P3, so none of them is in this total
    sched_a = cited("calaccess.filer_total", {"filer_id": FILER}, "3750")
    assert verify_source(sched_a, root).verification.status == "verified"


def test_a_gift_restated_across_filings_is_flagged_by_its_unrestated_filing(root):
    """A cross-form gift counts once, and still rests on a filing whose latest amendment
    dropped it. The filing to open is that one, not the earliest the gift is cited by."""
    # every schedule: only there does the cross-form dedup meet both filings' rows
    result = queries.run("calaccess.contributor_total",
                         {"filer_id": FILER, "contributor": "Lindqvist Farms", "form_type": ""},
                         root)
    assert result.value == 2000.0
    assert [(u.filing_id, u.amount) for u in result.unrestated] == [(DROPPED_496, 2000.0)]

    [row] = [c for c in calaccess.contributions_to(root, FILER) if "Lindqvist" in c.contributor]
    assert row.filing_id == SETTLED_460 and row.filings == 2
    assert [u.filing_id for u in row.unrestated] == [DROPPED_496]


def test_filer_total_names_every_gift_it_took_from_the_filing(root):
    result = queries.run("calaccess.filer_total", {"filer_id": FILER, "form_type": "F496P3"},
                         root)
    assert result.value == 6000.0
    assert [(u.filing_id, u.amount, u.rows) for u in result.unrestated] == [
        (DROPPED_496, 6000.0, 2)]


def test_a_ranking_is_unsettled_by_any_gift_in_it(root):
    """The ranking is made of every gift to the filer: a dropped one can put a contributor at
    the top or keep one off it, whoever it belongs to."""
    params = {"filer_id": FILER, "form_type": ""}    # every schedule, as above
    result = queries.run("calaccess.top_contributor", params, root)
    assert result.value == "Quill Harbor PAC"
    assert [(u.filing_id, u.amount) for u in result.unrestated] == [(DROPPED_496, 6000.0)]
    v = verify_source(cited("calaccess.top_contributor", params,
                            "Quill Harbor PAC"), root).verification
    assert v.status == "human_review" and DROPPED_496 in v.reason


def test_a_receipt_total_flags_only_gifts_it_counts(root):
    """A gift with no readable amount is in no total, so the filing it came from has no share
    in one, as for ie_total below. Named as a share, it would send a settled figure to
    human_review over money the figure never counted."""
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE RCPT_CD SET AMOUNT = '' WHERE TRAN_ID = 'F496P3-200002'")
    con.commit()
    con.close()
    params = {"filer_id": FILER, "contributor": "Quill Harbor PAC", "form_type": ""}
    result = queries.run("calaccess.contributor_total", params, root)
    assert result.value == 1500.0 and "1 more gift(s) with no readable amount" in result.note
    assert result.unrestated == [], "the blank-amount gift was not counted, so it is not named"
    assert verify_source(cited("calaccess.contributor_total", params, "1500"),
                         root).verification.status == "verified"


def test_ie_total_flags_only_rows_it_counts(root):
    """A dropped row outside the window, or with no readable amount, has no share in the
    total, so it cannot unsettle it."""
    everything = queries.run("calaccess.ie_total", IE_PARAMS, root)
    assert everything.value == 4200.0
    assert [(u.filing_id, u.amount, u.rows) for u in everything.unrestated] == [
        (IE_DROPPED, 3000.0, 1)], "the blank-amount row was not counted, so it is not named"
    v = verify_source(cited("calaccess.ie_total", IE_PARAMS, "4200"), root).verification
    assert v.status == "human_review" and IE_DROPPED in v.reason and "$3,000.00" in v.reason

    later = {**IE_PARAMS, "since": "2026-03-05"}
    assert not queries.run("calaccess.ie_total", later, root).unrestated
    assert verify_source(cited("calaccess.ie_total", later, "1200"),
                         root).verification.status == "verified"


def test_build_downgrades_a_row_verified_before_it_asked(root):
    """A claim file carrying `verified` (from an older version, or before the later amendment
    reached the export) re-runs at build and goes to human_review, context and verdict
    dropped, like any row that does not reproduce clean."""
    s = cited("calaccess.ie_total", IE_PARAMS, "4200")
    s.verification = Verification(status="verified", reason="verified earlier",
                                  context="ie_total = 4200", support="supports")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review"
    assert IE_DROPPED in v.reason and "claimed 'verified'" in v.reason
    assert v.context is None and v.support == "unreviewed"
    assert v.query_run is not None and v.query_run.cache_root == str(root)


def test_both_paths_write_the_phrase_the_skill_matches(root):
    """The research skill does not retry an unsettled figure, and tells one by this phrase in
    its reason. `vg verify` and `vg build` word the rest of their reasons differently."""
    phrase = "it counts rows a later amendment may have withdrawn"
    s = cited("calaccess.ie_total", IE_PARAMS, "4200")
    assert phrase in verify_source(s, root).verification.reason
    s.verification = Verification(status="verified")
    assert phrase in revalidate_from_cache(s, root).verification.reason


def test_every_citable_query_flags_and_refuses(tmp_path):
    """The flag and the refusal are written into each query, so this is the gate that a new
    one has them: every registered CAL-ACCESS query must be in PLANTED, name its unrestated
    filing, and refuse a database without cover amendment ids."""
    assert set(PLANTED) == {n for n in queries.REGISTRY if queries.dataset(n) == "CAL-ACCESS"}
    root = _export(tmp_path / "full")
    for name, (params, planted) in PLANTED.items():
        assert planted in [u.filing_id for u in queries.run(name, params, root).unrestated], name

    degraded = _export(tmp_path / "degraded", cover_amend_ids=False)
    for name, (params, _) in PLANTED.items():
        with pytest.warns(calaccess.DegradedDatabaseWarning), \
                pytest.raises(calaccess.DegradedDatabase,
                              match="rows a later amendment dropped.*vg calaccess build"):
            queries.run(name, params, degraded)
        with pytest.warns(calaccess.DegradedDatabaseWarning):
            v = verify_source(cited(name, params, "1"), degraded).verification
        assert v.status != "verified" and "vg calaccess build" in v.reason, name


def test_no_cover_table_is_refused_for_what_it_is(tmp_path):
    """Not the old-build case, which a rebuild from the same zip fixes: the export lacked the
    table, so the message says so."""
    root = _export(tmp_path, covers=False)
    for name in ("calaccess.contributor_total", "calaccess.filer_total",
                 "calaccess.top_contributor"):
        with pytest.raises(calaccess.DegradedDatabase, match="no CVR_CAMPAIGN_DISCLOSURE_CD "
                                                             "table.*complete export"):
            queries.run(name, PLANTED[name][0], root)


def test_a_database_without_cover_amend_ids_says_the_listings_cannot_check(tmp_path):
    """The listings keep working as finding aids under the refusal, and say they cannot
    check; a row nobody checked never reads as settled."""
    root = _export(tmp_path, cover_amend_ids=False)
    assert calaccess.Contribution("1", "2", "x", "", "", 1.0, "").unrestated is None
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        assert all(c.unrestated is None for c in calaccess.contributions_to(root, FILER))
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        ies = calaccess.independent_expenditures(root, "Fairweather", first="Ondine")
    assert ies and all(r["unrestated"] is None for r in ies)


def test_listings_mark_the_row_and_keep_listing_it(root):
    gifts = {c.amount: c for c in calaccess.contributions_to(root, FILER)}
    assert set(gifts) == {4000.0, 2000.0, 1500.0, 250.0}, "a marked row is still listed"
    assert [u.filing_id for u in gifts[4000.0].unrestated] == [DROPPED_496]
    assert gifts[1500.0].unrestated == () and gifts[250.0].unrestated == ()

    ies = {r["FILING_ID"]: r for r in calaccess.independent_expenditures(
        root, "Fairweather", first="Ondine")}
    assert set(ies) == {IE_DROPPED, IE_SETTLED, IE_UNREAD}
    assert [u.filing_id for u in ies[IE_DROPPED]["unrestated"]] == [IE_DROPPED]
    assert ies[IE_SETTLED]["unrestated"] == ()


def test_the_cli_tables_mark_the_row_and_say_what_it_means(root, monkeypatch):
    # COLUMNS does not reach `cli.con` once another test has set its width outright
    monkeypatch.setattr(cli.con, "_width", 250)
    run = CliRunner()
    out = plain(run.invoke(cli.app, ["calaccess", "contributions", FILER, "--cache", str(root)],
                           env={"COLUMNS": "250"}).output)
    assert f"{DROPPED_496}: a1 has none" in out
    assert "2 row(s) come from an amendment a later one did not restate" in out

    out = plain(run.invoke(cli.app, ["calaccess", "independent-expenditures", "Fairweather",
                                     "--first", "Ondine", "--cache", str(root)],
                           env={"COLUMNS": "250"}).output)
    assert f"{IE_DROPPED}: a1 has none" in out and f"{IE_UNREAD}: a1 has none" in out
    assert "2 row(s) come from an amendment" in out


def test_the_cli_says_a_database_cannot_check(tmp_path):
    root = _export(tmp_path, cover_amend_ids=False)
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        res = CliRunner().invoke(cli.app, ["calaccess", "contributions", FILER,
                                           "--cache", str(root)], env={"COLUMNS": "250"})
    assert res.exit_code == 0, res.output
    assert "cannot tell whether a filing's latest amendment dropped" in plain(res.output)


def test_vg_query_warns_before_the_value_is_recorded(root):
    """The researcher copies `expected` from this output, so it says here, not only at
    check-claim, that the value will go to human_review however it is cited."""
    res = CliRunner().invoke(cli.app, ["query", "calaccess.contributor_total",
                                       "--param", f"filer_id={FILER}",
                                       "--param", "contributor=Quill Harbor PAC",
                                       "--param", "form_type=",
                                       "--cache", str(root)], env={"COLUMNS": "250"})
    out = plain(res.output)
    assert res.exit_code == 0, out
    assert "5500.0" in out and "Will not verify" in out
    assert out.count(DROPPED_496) == 2, "its explanation and its URL, not again in the note"


def test_a_reason_names_the_largest_shares_and_vg_query_lists_the_rest(tmp_path, monkeypatch):
    """A committee's whole history can name dozens of filings, and the reason is written into
    the claim file and rendered on the review page. It names the largest few, and `vg query`,
    which a reviewer re-runs, lists every one."""
    shares = [calaccess.Unrestated(f"888030{i}", 0, 1, amount=1000.0 - i, rows=1)
              for i in range(queries.UNSETTLED_SHOWN + 2)]
    result = queries.QueryResult(value=1.0, unrestated=shares)
    reason = result.unsettled
    assert all(u.filing_id in reason for u in shares[:queries.UNSETTLED_SHOWN])
    assert not any(u.filing_id in reason for u in shares[queries.UNSETTLED_SHOWN:])
    assert "and 2 more filing(s) with smaller shares, which `vg query` lists" in reason
    for n in (1, queries.UNSETTLED_SHOWN):    # nothing left over is never "and -4 more"
        assert "more filing(s)" not in queries.QueryResult(value=1.0,
                                                           unrestated=shares[:n]).unsettled

    monkeypatch.setattr(queries, "run", lambda name, params, root: result)
    out = plain(CliRunner().invoke(cli.app, ["query", "calaccess.filer_total", "--param",
                                             f"filer_id={FILER}", "--cache", str(tmp_path)],
                                   env={"COLUMNS": "250"}).output)
    assert all(out.count(f"filing {u.filing_id}'s rows") == 1 for u in shares), (
        "every filing, each once")
