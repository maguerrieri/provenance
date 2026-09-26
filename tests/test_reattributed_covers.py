"""A row's own amendment can name a candidate its filing's latest cover does not.

`ie_total` attributes a Form 496 row through CVR_LATEST, the filing's latest cover. When a
later amendment has a cover and no rows, and that cover names another candidate, the other
stance or no candidate at all, the row never reaches the total its own amendment's cover
named. Nothing counts it, so nothing flagged it: a total short by a row nobody mentions. Such
a total must not verify, it must name the filing and the amount left out, and the listing
must show the row under the candidate its own cover named.

Every filing here is synthetic, in the export's own formats: "M/D/YYYY 12:00:00 AM" dates and
integer amendment ids.
"""

from __future__ import annotations

import re
import zipfile

import pytest
from typer.testing import CliRunner

from provenance import calaccess, cli, queries
from provenance.models import QueryCitation, Source, Verification
from provenance.verify import revalidate_from_cache, verify_source

MOVED = "8890301"      # a0 names Ondine Fairweather (support), twice; a1 moves it to another
UNNAMED = "8890302"    # a0 names her; a1's cover names no candidate: its rows reach no total
SETTLED = "8890303"    # restated at a1, whose cover still names her
STANCE = "8890304"     # a0 supports her; a1's cover opposes her
UNREAD = "8890305"     # moved like MOVED, but its amount is blank: never counted
ORPHAN = "8890306"     # the only filing naming Wren Larkspur, and a1 names no candidate
BACK = "8890307"       # a0 names Tamsin Juniper, a1 another candidate, a2 her again
NOSIDE = "8890308"     # a0 names Rook Ashdown with a blank stance; a1 supports them

IES = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n"
       f"{MOVED}\t0\tIE1\t1\t3000\t3/2/2026 12:00:00 AM\tmailer\n"
       f"{UNNAMED}\t0\tIE2\t1\t700\t3/4/2026 12:00:00 AM\tdigital ads\n"
       f"{SETTLED}\t0\tIE3\t1\t1200\t3/9/2026 12:00:00 AM\tradio\n"
       f"{SETTLED}\t1\tIE3\t1\t1200\t3/9/2026 12:00:00 AM\tradio\n"
       f"{STANCE}\t0\tIE4\t1\t500\t3/6/2026 12:00:00 AM\tdoor hangers\n"
       f"{UNREAD}\t0\tIE5\t1\t\t3/12/2026 12:00:00 AM\tphone bank\n"
       f"{ORPHAN}\t0\tIE6\t1\t900\t3/10/2026 12:00:00 AM\tyard signs\n"
       f"{BACK}\t0\tIE7\t1\t250\t3/11/2026 12:00:00 AM\tpostcards\n"
       f"{NOSIDE}\t0\tIE8\t1\t400\t3/13/2026 12:00:00 AM\tbanners\n")

ONDINE = ("Fairweather", "Ondine")
CASPIAN = ("Brightwater", "Caspian")
WREN = ("Larkspur", "Wren")
TAMSIN = ("Juniper", "Tamsin")
ROOK = ("Ashdown", "Rook")
NOBODY = ("", "")


def _cover(filing, amend, who, stance):
    return (filing, amend, "8890900", "Committee for Example", *who, stance, "F496")


COVER_ROWS = [
    _cover(MOVED, "0", ONDINE, "S"), _cover(MOVED, "0", ONDINE, "S"),   # a duplicate record
    _cover(MOVED, "1", CASPIAN, "S"),
    _cover(UNNAMED, "0", ONDINE, "S"), _cover(UNNAMED, "1", NOBODY, ""),
    _cover(SETTLED, "0", ONDINE, "S"), _cover(SETTLED, "1", ONDINE, "S"),
    _cover(STANCE, "0", ONDINE, "S"), _cover(STANCE, "1", ONDINE, "O"),
    _cover(UNREAD, "0", ONDINE, "S"), _cover(UNREAD, "1", CASPIAN, "S"),
    _cover(ORPHAN, "0", WREN, "S"), _cover(ORPHAN, "1", NOBODY, ""),
    _cover(BACK, "0", TAMSIN, "S"), _cover(BACK, "1", CASPIAN, "S"),
    _cover(BACK, "2", TAMSIN, "S"),
    _cover(NOSIDE, "0", ROOK, ""), _cover(NOSIDE, "1", ROOK, "S"),
]


def _export(root, *, cover_amend_ids=True):
    cols = ["FILING_ID", "AMEND_ID", "FILER_ID", "FILER_NAML", "CAND_NAML", "CAND_NAMF",
            "SUP_OPP_CD", "FORM_TYPE"]
    rows = COVER_ROWS
    if not cover_amend_ids:
        cols.remove("AMEND_ID")
        rows = [r[:1] + r[2:] for r in rows]
    cover_tsv = "\t".join(cols) + "\n" + "".join("\t".join(r) + "\n" for r in rows)
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", IES)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", cover_tsv)
    if cover_amend_ids:
        calaccess.build(root)
    else:
        with pytest.warns(calaccess.DegradedDatabaseWarning):
            calaccess.build(root)
    return root


@pytest.fixture
def root(tmp_path):
    return _export(tmp_path)


def ie(who, stance="support", **window):
    return {"candidate_last": who[0], "first": who[1], "stance": stance, **window}


def cited(params, expected):
    return Source(url="https://committee.example/ie-report", publisher="Example Committee",
                  author="", date="2026-03-15", source_type="primary_document",
                  snippet="independent expenditures",
                  query=QueryCitation(name="calaccess.ie_total", params=params,
                                      expected=expected))


@pytest.fixture(autouse=True)
def wide(monkeypatch):
    """A listing row is wider than 80 columns. Widened on rich's console, not with COLUMNS: a
    test that pinned the width earlier in the run keeps COLUMNS from reaching it (CLAUDE.md)."""
    monkeypatch.setattr(cli.con, "_width", 400)


def left_out(result):
    return [(u.filing_id, u.amount, u.rows) for u in result.reattributed]


def plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_a_total_missing_rows_its_own_cover_named_goes_to_human_review(root):
    """The acceptance case. The total reproduces and would have rendered green, while three
    filings whose own amendment named her are in no total of hers: one moved to another
    candidate, one to no candidate, one to the other stance."""
    params = ie(ONDINE)
    result = queries.run("calaccess.ie_total", params, root)
    assert result.value == 1200.0, "the flag must never change the number"
    assert left_out(result) == [(MOVED, 3000.0, 1), (UNNAMED, 700.0, 1), (STANCE, 500.0, 1)], (
        "each once, largest first: a duplicate cover record must not fan the row out, and "
        "the blank-amount row was never going to be counted")
    assert not result.unrestated, "nothing this total counts is unsettled"

    v = verify_source(cited(params, "1200"), root).verification
    assert v.status == "human_review", "a note alone would still render green"
    for fid, amount in ((MOVED, "$3,000.00"), (UNNAMED, "$700.00"), (STANCE, "$500.00")):
        assert fid in v.reason and amount in v.reason and calaccess.filing_url(fid) in v.reason
    assert "'Caspian Brightwater' (support)" in v.reason
    assert "names no candidate" in v.reason
    assert "'Ondine Fairweather' (oppose)" in v.reason
    assert SETTLED not in v.reason and UNREAD not in v.reason
    assert "provenance query" in v.reason and v.query_run is not None


def test_only_rows_the_window_would_count_are_named(root):
    later = ie(ONDINE, since="2026-03-05")
    assert left_out(queries.run("calaccess.ie_total", later, root)) == [(STANCE, 500.0, 1)]


def test_a_settled_total_still_verifies(root):
    """Known-good totals are unchanged: nothing their own covers named is left out."""
    params = ie(ONDINE, since="2026-03-07")
    assert not queries.run("calaccess.ie_total", params, root).reattributed
    assert verify_source(cited(params, "1200"), root).verification.status == "verified"


def test_the_other_side_is_flagged_as_unrestated_not_as_left_out(root):
    """The candidate a later cover moved the row to counts it, and that total is #31's case: it
    counts rows a later amendment may have withdrawn. It is not also a left-out row."""
    result = queries.run("calaccess.ie_total", ie(CASPIAN), root)
    assert result.value == 3000.0
    assert [u.filing_id for u in result.unrestated] == [MOVED]
    assert not result.reattributed, "her own cover named someone else, not him"


def test_stance_decides_which_side_a_row_is_on(root):
    """With no stance asked, the latest cover still names her, so the row is counted, not left
    out. Asked for the stance only the latest cover took, it is counted and unsettled."""
    anyway = queries.run("calaccess.ie_total", ie(ONDINE, stance=""), root)
    assert anyway.value == 1700.0
    assert [u.filing_id for u in anyway.reattributed] == [MOVED, UNNAMED]
    assert [u.filing_id for u in anyway.unrestated] == [STANCE]

    oppose = queries.run("calaccess.ie_total", ie(ONDINE, stance="oppose"), root)
    assert oppose.value == 500.0 and not oppose.reattributed
    assert [u.filing_id for u in oppose.unrestated] == [STANCE]


def test_a_candidate_whose_only_rows_were_left_out_is_a_miss_naming_them(root):
    """The update whose later cover names no candidate: its rows reach no total, so the
    candidate it was about gets a miss. A miss never verifies, and it must say why, or it
    reads as "nobody spent on this candidate". It goes to a person, as a receipt query's miss
    on a left-out schedule does: no retry of these parameters reproduces a figure, and only
    the filing says which cover is right."""
    result = queries.run("calaccess.ie_total", ie(WREN), root)
    assert not result.found and result.value is None
    assert "1 more whose filing's latest cover names another candidate" in result.note
    assert "($900.00 between them)" in result.note, "the figure a researcher would cite"
    assert "NO MATCH" not in result.note, "there was a match: it was left out"
    assert left_out(result) == [(ORPHAN, 900.0, 1)], "carried, so `provenance query` can list them all"
    assert ORPHAN in result.unsettled and "$900.00" in result.unsettled
    assert calaccess.filing_url(ORPHAN) in result.unsettled

    v = verify_source(cited(ie(WREN), "900"), root).verification
    assert v.status == "human_review" and ORPHAN in v.reason
    assert v.reason.count(calaccess.filing_url(ORPHAN)) == 1, "named once, not in the note too"


def test_latest_means_the_last_amendment_not_any_later_one(root):
    """A middle amendment's cover named someone else, and the last one names her again: the
    total counts the row, so it is not left out, and the middle candidate never had it."""
    tamsin = queries.run("calaccess.ie_total", ie(TAMSIN), root)
    assert tamsin.value == 250.0 and not tamsin.reattributed
    assert [u.filing_id for u in tamsin.unrestated] == [BACK]
    caspian = queries.run("calaccess.ie_total", ie(CASPIAN), root)
    assert not caspian.reattributed and BACK not in caspian.note


def test_build_downgrades_a_row_verified_before_it_asked(root):
    s = cited(ie(ONDINE), "1200")
    s.verification = Verification(status="verified", reason="verified earlier",
                                  context="ie_total = 1200", support="supports")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review" and MOVED in v.reason
    assert v.context is None and v.support == "unreviewed"


def test_both_paths_write_the_phrase_the_skill_matches(root):
    """The research skill does not retry this either, and the `ca` notes quote this phrase for
    it (tests/test_source_notes.py)."""
    phrase = "leaves out rows that a filing's own amendment attributed to this candidate"
    s = cited(ie(ONDINE), "1200")
    assert phrase in verify_source(s, root).verification.reason
    s.verification = Verification(status="verified")
    assert phrase in revalidate_from_cache(s, root).verification.reason


def test_a_total_both_counting_and_missing_such_rows_names_both(root):
    """Two open questions, one reason: the skill's two phrases both appear."""
    result = queries.run("calaccess.ie_total", ie(ONDINE, stance=""), root)
    why = result.unsettled
    assert "it counts rows a later amendment may have withdrawn" in why
    assert "It also leaves out rows that a filing's own amendment attributed" in why
    assert STANCE in why and MOVED in why and UNNAMED in why


def test_the_listing_shows_the_row_under_the_candidate_its_own_cover_named(root):
    """A finding aid that hid the row would hide the filing to open."""
    rows = calaccess.independent_expenditures(root, "Fairweather", first="Ondine")
    by_id = {r["FILING_ID"]: r for r in rows}
    assert sorted(by_id) == sorted([MOVED, UNNAMED, SETTLED, STANCE, UNREAD])
    assert len(rows) == 5, "a duplicate cover record must not list the row twice"

    moved = by_id[MOVED]
    assert (moved["CAND_NAMF"], moved["CAND_NAML"], moved["stance"]) == (
        "Ondine", "Fairweather", "support"), "listed as its own amendment's cover has it"
    assert moved["reattributed"].latest == "'Caspian Brightwater' (support)"
    assert moved["reattributed"].own == "'Ondine Fairweather' (support)"
    assert by_id[UNNAMED]["reattributed"].latest == ""
    assert by_id[SETTLED]["reattributed"] is None
    # Listed once, under the latest cover, since the oppose total counts it. Still marked:
    # ie_total(stance=support) flags this filing, and the listing must agree about it.
    stance = by_id[STANCE]
    assert stance["stance"] == "oppose"
    assert (stance["reattributed"].own, stance["reattributed"].latest) == (
        "'Ondine Fairweather' (support)", "'Ondine Fairweather' (oppose)")

    [orphan] = calaccess.independent_expenditures(root, "Larkspur", first="Wren")
    assert orphan["FILING_ID"] == ORPHAN and orphan["reattributed"] is not None
    assert {r["FILING_ID"] for r in calaccess.independent_expenditures(
        root, "Brightwater", first="Caspian")} == {MOVED, UNREAD}


def test_a_stance_no_total_asks_for_is_not_a_flip(root):
    """The listing marks a stance flip only where ie_total leaves the row out. A blank own
    stance is none a total asks for, so every total that could count the row does, and a mark
    would name a filing no flag holds open."""
    [row] = calaccess.independent_expenditures(root, "Ashdown", first="Rook")
    assert row["FILING_ID"] == NOSIDE and row["reattributed"] is None
    for stance in ("", "support", "oppose"):
        assert not queries.run("calaccess.ie_total", ie(ROOK, stance=stance), root).reattributed


def test_the_cli_listing_marks_the_row_and_says_what_it_means(root):
    out = plain(CliRunner().invoke(cli.app, [
        "calaccess", "independent-expenditures", "Fairweather", "--first", "Ondine",
        "--cache", str(root)]).output)
    own = "a0's cover names 'Ondine Fairweather' (support)"
    assert f"{MOVED}: a1 has none; {own}; a1's names 'Caspian Brightwater' (support)" in out
    assert f"{UNNAMED}: a1 has none; {own}; a1's names no candidate" in out
    assert f"{STANCE}: a1 has none; {own}; a1's names 'Ondine Fairweather' (oppose)" in out
    assert "4 row(s) have an earlier cover naming this candidate" in out
    assert "No total for this candidate counts them" not in out, (
        "not true of the stance flip, which the oppose total counts")


def test_a_database_without_cover_amend_ids_says_the_listing_cannot_check(tmp_path):
    """Without amendment ids nothing can say which cover is a row's own, so no row is marked
    as left out, and the footer says the listing could not look."""
    root = _export(tmp_path, cover_amend_ids=False)
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        rows = calaccess.independent_expenditures(root, "Fairweather", first="Ondine")
    assert rows and all(r["reattributed"] is None for r in rows)
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        res = CliRunner().invoke(cli.app, ["calaccess", "independent-expenditures", "Larkspur",
                                           "--first", "Wren", "--cache", str(root)])
    assert res.exit_code == 0, res.output
    assert "or list rows an earlier amendment's cover gave this candidate" in plain(res.output)


def test_provenance_query_warns_and_lists_every_filing_left_out(root, monkeypatch):
    shares = [calaccess.Reattributed(f"889040{i}", 0, 1, "", amount=1000.0 - i, rows=1)
              for i in range(queries.UNSETTLED_SHOWN + 2)]
    result = queries.QueryResult(value=1.0, reattributed=shares)
    reason = result.unsettled
    assert all(u.filing_id in reason for u in shares[:queries.UNSETTLED_SHOWN])
    assert not any(u.filing_id in reason for u in shares[queries.UNSETTLED_SHOWN:])
    assert "and 2 more filing(s) with smaller amounts, which `provenance query` lists" in reason

    monkeypatch.setattr(queries, "run", lambda name, params, root: result)
    out = plain(CliRunner().invoke(cli.app, ["query", "calaccess.ie_total", "--param",
                                             "candidate_last=Fairweather", "--param",
                                             "first=Ondine", "--cache", str(root)]).output)
    assert "Will not verify" in out
    assert all(out.count(f"filing {u.filing_id}'s rows") == 1 for u in shares), (
        "every filing, each once")


def test_a_miss_lists_every_filing_its_reason_leaves_to_provenance_query(root, monkeypatch):
    """A miss's reason names the largest few and says `provenance query` lists the rest, so the miss
    path of `provenance query` must, or the pointer leads nowhere."""
    shares = [calaccess.Reattributed(f"889050{i}", 0, 1, "", amount=1000.0 - i, rows=1)
              for i in range(queries.UNSETTLED_SHOWN + 2)]
    miss = queries.QueryResult(value=None, found=False, reattributed=shares,
                               detail="no support-stance expenditures counted; 7 more whose "
                                      "filing's latest cover names another candidate, the "
                                      "other stance or none, not counted")
    monkeypatch.setattr(queries, "run", lambda name, params, root: miss)
    res = CliRunner().invoke(cli.app, ["query", "calaccess.ie_total", "--param",
                                       "candidate_last=Larkspur", "--param", "first=Wren",
                                       "--cache", str(root)])
    out = plain(res.output)
    assert res.exit_code == 1 and "which `provenance query` lists" in out
    assert "nothing counted" in out and "Not a finding" in out
    assert all(out.count(f"filing {u.filing_id}'s rows") == 1 for u in shares), (
        "every filing, each once")


def test_a_short_reason_claims_no_more_filings():
    """The count of filings past the shown few was tested for truthiness, and fewer than
    UNSETTLED_SHOWN made it negative: every one-filing reason said "and -4 more filing(s)"."""
    one = calaccess.Unrestated("8890601", 0, 1, amount=10.0, rows=1)
    moved = calaccess.Reattributed("8890602", 0, 1, "", amount=10.0, rows=1)
    for result in (queries.QueryResult(value=1.0, unrestated=[one]),
                   queries.QueryResult(value=1.0, reattributed=[moved])):
        assert "more filing" not in result.unsettled, result.unsettled


def test_a_stance_code_is_named_only_as_the_total_reads_it():
    """ie_total's test does not trim the stance code, so "S " is not support to it. Calling it
    support would make the cover that kept a row out read as the one the total asked for."""
    assert calaccess.cover_names("Fairweather", "Ondine", "S") == "'Ondine Fairweather' (support)"
    assert calaccess.cover_names("Fairweather", "Ondine", "S ") == (
        "'Ondine Fairweather' (stance 'S ')")
    assert calaccess.cover_names("Fairweather", "Ondine", "") == (
        "'Ondine Fairweather' (no stance)")
    assert calaccess.cover_names("", " ", "S") == ""
