"""A filing's latest amendment in a table can have rows on some schedules and none on another.

A table holds several schedules, told apart by FORM_TYPE, and the per-table "latest amendment"
rule takes that amendment whole, so the other schedule's earlier rows are left out of every
figure. The export cannot say whether that is right: the amendment withdrew the schedule, or
did not restate it because nothing on it changed. A figure that leaves such rows out must not
verify, the listing must show and mark them, and a table that cannot tell its schedules apart
must say so.

Every filing here is synthetic, in the export's own formats: "M/D/YYYY 12:00:00 AM" dates and
integer amendment ids.
"""

from __future__ import annotations

import re
import sqlite3
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vgpipe import calaccess, cli, queries
from vgpipe.models import QueryCitation, Source, Verification
from vgpipe.verify import revalidate_from_cache, verify_source

FILER = "8884100"
GAP_460 = "8884101"      # schedules A and I at amendment 0; amendment 1 restates only C
SETTLED_460 = "8884102"  # schedule A at amendments 0 and 1
F496 = "8884103"         # one amendment: the Form 496 copy of the schedule A gift A-300001
EXP_GAP = "8884201"      # expenditures: schedule F at amendment 0; amendment 1 has only E
QUILL = "8884500"        # a second filer, whose every readable gift is left out
QUILL_GAP = "8884501"    # schedule A at amendment 0; amendment 1 has one C with no amount

RECEIPTS = (
    "FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP\tCTRIB_OCC"
    "\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n"
    # the same gift as F496P3-300001 below, reported on both forms
    f"{GAP_460}\t0\tA-300001\t1\tMarrow Creek PAC\t\t\t\t1/8/2026 12:00:00 AM\t3000\tA\n"
    f"{GAP_460}\t0\tA-300002\t2\tVeldt\tAnsel\t\t\t1/15/2026 12:00:00 AM\t400\tA\n"
    # reported nowhere else, so a figure that would count it finds nothing counted
    f"{GAP_460}\t0\tI-300005\t1\tLarkspur Savings\t\t\t\t1/12/2026 12:00:00 AM\t700\tI\n"
    f"{GAP_460}\t0\tC-300003\t1\tOyelaran\tTamsin\t\t\t1/18/2026 12:00:00 AM\t150\tC\n"
    f"{GAP_460}\t1\tC-300003\t1\tOyelaran\tTamsin\t\t\t1/18/2026 12:00:00 AM\t150\tC\n"
    f"{SETTLED_460}\t0\tA-300004\t1\tVeldt\tAnsel\t\t\t2/3/2026 12:00:00 AM\t250\tA\n"
    f"{SETTLED_460}\t1\tA-300004\t1\tVeldt\tAnsel\t\t\t2/3/2026 12:00:00 AM\t250\tA\n"
    f"{F496}\t0\tF496P3-300001\t1\tMarrow Creek PAC\t\t\t\t1/8/2026 12:00:00 AM\t3000"
    "\tF496P3\n"
    f"{QUILL_GAP}\t0\tA-500001\t1\tQuillon\tMaren\t\t\t1/5/2026 12:00:00 AM\t900\tA\n"
    f"{QUILL_GAP}\t1\tC-500002\t1\tQuillon\tMaren\t\t\t1/6/2026 12:00:00 AM\t\tC\n"
)
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{GAP_460}\tF460\t1/31/2026 12:00:00 AM\n"
           f"{FILER}\t{GAP_460}\tF460\t2/20/2026 12:00:00 AM\n"
           f"{FILER}\t{SETTLED_460}\tF460\t2/28/2026 12:00:00 AM\n"
           f"{FILER}\t{SETTLED_460}\tF460\t3/9/2026 12:00:00 AM\n"
           f"{FILER}\t{F496}\tF496\t1/9/2026 12:00:00 AM\n"
           f"{QUILL}\t{QUILL_GAP}\tF460\t1/31/2026 12:00:00 AM\n"
           f"{QUILL}\t{QUILL_GAP}\tF460\t2/20/2026 12:00:00 AM\n")
EXPENDITURES = (
    "FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tPAYEE_NAML\tPAYEE_NAMF\tEXPN_DATE\tAMOUNT"
    "\tEXPN_DSCR\tCAND_NAML\tSUP_OPP_CD\tFORM_TYPE\n"
    f"{EXP_GAP}\t0\tE-400001\t1\tHollis Print Shop\t\t1/20/2026 12:00:00 AM\t900\tmailers\t\t\tE\n"
    f"{EXP_GAP}\t0\tF-400002\t1\tWren Hall\t\t1/25/2026 12:00:00 AM\t300\tvenue, unpaid\t\t\tF\n"
    f"{EXP_GAP}\t1\tE-400001\t1\tHollis Print Shop\t\t1/20/2026 12:00:00 AM\t900\tmailers\t\t\tE\n"
)
# Each cover's latest amendment matches its table's, so the per-table flag has nothing to say
# and every flag here is the per-schedule one.
# The 460s' periods cover the Form 496's gift, so it is not a pending late report: a 460 for
# its dates restated it, whichever amendment the export kept.
JANUARY = ("1/1/2026 12:00:00 AM", "1/31/2026 12:00:00 AM")
FEBRUARY = ("2/1/2026 12:00:00 AM", "2/28/2026 12:00:00 AM")
COVERS = ("FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tFORM_TYPE\tFROM_DATE\tTHRU_DATE\n"
          + "".join(f"{f}\t{a}\t{FILER}\tFriends of Example\t{form}\t{start}\t{end}\n"
                    for f, a, form, (start, end) in (
                        (GAP_460, 0, "F460", JANUARY), (GAP_460, 1, "F460", JANUARY),
                        (SETTLED_460, 0, "F460", FEBRUARY), (SETTLED_460, 1, "F460", FEBRUARY),
                        (F496, 0, "F496", ("", "")), (EXP_GAP, 0, "F460", JANUARY),
                        (EXP_GAP, 1, "F460", JANUARY), (QUILL_GAP, 0, "F460", JANUARY),
                        (QUILL_GAP, 1, "F460", JANUARY))))

# Every citable CAL-ACCESS query over a table with schedules, with parameters that reach the
# left-out schedule. The rest read a table with a single schedule. A new query fails
# test_every_citable_query_is_checked_for_left_out_schedules until it is in one or the other.
PLANTED = {
    "calaccess.contributor_total": {"filer_id": FILER, "contributor": "Veldt",
                                    "contributor_first": "Ansel"},
    "calaccess.filer_total": {"filer_id": FILER},
    "calaccess.top_contributor": {"filer_id": FILER},
}
SINGLE_SCHEDULE = {"calaccess.ie_total": "S496_CD"}
VELDT = PLANTED["calaccess.contributor_total"]
PHRASE = "rows a later amendment may have withdrawn"


def _export(root: Path, *, expn_form_type: bool = True) -> Path:
    """The export above, with or without EXPN_CD.FORM_TYPE (a database built before that
    column was loaded)."""
    expenditures = EXPENDITURES
    if not expn_form_type:
        expenditures = "".join(line.rsplit("\t", 1)[0] + "\n"
                               for line in EXPENDITURES.splitlines())
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RECEIPTS)
        zf.writestr("CalAccess/DATA/EXPN_CD.TSV", expenditures)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
    calaccess.build(root)
    return root


@pytest.fixture
def root(tmp_path):
    return _export(tmp_path)


def cited(name, params, expected):
    return Source(url="https://committee.example/report", publisher="Example Committee",
                  author="", date="2026-03-09", source_type="primary_document",
                  snippet="itemized contributions",
                  query=QueryCitation(name=name, params=params, expected=expected))


def plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def shares(found):
    return [(u.filing_id, u.schedule, u.amount, u.rows) for u in found]


def test_only_a_schedule_the_latest_amendment_left_out_is_found(root):
    """Schedule C is restated at amendment 1, the settled filing restates schedule A, and a
    filing with one amendment has nothing to leave out."""
    con = calaccess.connect(root)
    try:
        got = calaccess.unrestated_schedules(con, "RCPT_CD",
                                             [GAP_460, SETTLED_460, F496, "8884999"])
    finally:
        con.close()
    assert got == [calaccess.UnrestatedSchedule(GAP_460, "A", 0, 1),
                   calaccess.UnrestatedSchedule(GAP_460, "I", 0, 1)]


def test_a_total_leaving_out_a_gift_goes_to_human_review_naming_the_filing(root):
    """The acceptance case: $250 reproduces and would have rendered green, but the filing's
    amendment 1 may only have left schedule A unchanged, and then the donor gave $650."""
    result = queries.run("calaccess.contributor_total", VELDT, root)
    assert result.value == 250.0, "the flag must never change the number"
    assert shares(result.omitted) == [(GAP_460, "A", 400.0, 1)]
    assert not result.unrestated

    v = verify_source(cited("calaccess.contributor_total", VELDT, "250"), root).verification
    assert v.status == "human_review", "a note alone would still render green"
    assert f"filing {GAP_460}'s schedule A rows are from amendment 0" in v.reason
    assert "later amendment 1 has rows on other schedules but none on that one" in v.reason
    assert "$400.00 in 1 row(s) this result leaves out" in v.reason
    assert calaccess.filing_url(GAP_460) in v.reason
    assert "they belong in this figure" in v.reason
    assert SETTLED_460 not in v.reason, "a settled filing is not one to open"
    assert "more schedule(s)" not in v.reason, "no count of more when there are none"
    assert "vg query" in v.reason and v.query_run is not None   # the re-run command


def test_a_gift_a_counted_row_also_reports_is_not_left_out(root):
    """Marrow Creek's schedule A copy is left out, but the same gift is counted from the Form
    496, so a figure over every schedule is the same whichever the amendment meant."""
    marrow = {"filer_id": FILER, "contributor": "Marrow Creek PAC", "form_type": ""}
    result = queries.run("calaccess.contributor_total", marrow, root)
    assert result.value == 3000.0 and result.omitted == []
    assert verify_source(cited("calaccess.contributor_total", marrow, "3000"),
                         root).verification.status == "verified"
    p496 = {"filer_id": FILER, "form_type": "F496P3"}
    assert verify_source(cited("calaccess.filer_total", p496, "3000"),
                         root).verification.status == "verified"


def test_filer_total_names_only_the_schedule_it_sums(root):
    """A schedule A total leaves out both schedule A gifts: its filter drops the Form 496
    copy, so nothing counted reports Marrow Creek's gift. Schedule C was restated."""
    result = queries.run("calaccess.filer_total", {"filer_id": FILER}, root)
    assert result.value == 250.0
    assert shares(result.omitted) == [(GAP_460, "A", 3400.0, 2)]

    sched_c = {"filer_id": FILER, "form_type": "C"}
    assert queries.run("calaccess.filer_total", sched_c, root).omitted == []
    assert verify_source(cited("calaccess.filer_total", sched_c, "150"),
                         root).verification.status == "verified"


def test_a_ranking_is_unsettled_by_any_gift_it_leaves_out(root):
    """Left out, Veldt's $400 and Larkspur's $700 are not in a ranking over every schedule, and
    whoever they belong to, a gift the ranking leaves out can change who is at the top."""
    every = {"filer_id": FILER, "form_type": ""}
    result = queries.run("calaccess.top_contributor", every, root)
    assert result.value == "Marrow Creek PAC"
    assert shares(result.omitted) == [(GAP_460, "I", 700.0, 1), (GAP_460, "A", 400.0, 1)]
    v = verify_source(cited("calaccess.top_contributor", every, "Marrow Creek PAC"),
                      root).verification
    assert v.status == "human_review" and GAP_460 in v.reason

    # Ranked on schedule A, the default, the Form 496 copy is not counted, so Marrow Creek's
    # gift is left out whole, and a $250 leader stands over $3,400 the ranking leaves out.
    result = queries.run("calaccess.top_contributor", {"filer_id": FILER}, root)
    assert result.value == "Ansel Veldt"
    assert shares(result.omitted) == [(GAP_460, "A", 3400.0, 2)]


def test_build_downgrades_a_row_verified_before_it_asked(root):
    """A claim file carrying `verified` from before this check re-runs at build and goes to
    human_review, context and verdict dropped."""
    s = cited("calaccess.contributor_total", VELDT, "250")
    s.verification = Verification(status="verified", reason="verified earlier",
                                  context="contributor_total = 250", support="supports")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review"
    assert GAP_460 in v.reason and "claimed 'verified'" in v.reason
    assert v.context is None and v.support == "unreviewed"


def test_both_paths_write_the_phrase_the_skill_matches(root):
    """The research skill leaves an unsettled figure for a person rather than retrying it,
    and tells one by this phrase, which both kinds of reason carry."""
    s = cited("calaccess.contributor_total", VELDT, "250")
    assert PHRASE in verify_source(s, root).verification.reason
    s.verification = Verification(status="verified")
    assert PHRASE in revalidate_from_cache(s, root).verification.reason
    skill = Path(__file__).parents[1] / ".claude" / "skills" / "voter-guide-research" / "SKILL.md"
    assert f'"{PHRASE}"' in skill.read_text(), "the skill must match what the code writes"


def test_every_citable_query_is_checked_for_left_out_schedules(root):
    """The check is written into each query, so this is the gate that a new one has it: every
    registered CAL-ACCESS query names the left-out schedule, or reads a table with one."""
    citable = {n for n in queries.REGISTRY if queries.dataset(n) == "CAL-ACCESS"}
    assert set(PLANTED) | set(SINGLE_SCHEDULE) == citable
    assert not set(PLANTED) & set(SINGLE_SCHEDULE)
    for name, params in PLANTED.items():
        assert GAP_460 in [u.filing_id for u in queries.run(name, params, root).omitted], name


def test_staging_twice_on_one_connection_keeps_each_grouping_its_own(root):
    """Each call stages its own table, so SQL built on the first still reads the first's rows
    and `gap` indexes after a second call."""
    con = calaccess.connect(root)
    try:
        first, a = queries.left_out_gifts(con, FILER, extra="",
                                          counted="UPPER(TRIM(x.FORM_TYPE)) = 'A'")
        queries.left_out_gifts(con, FILER, extra="", counted="1")
        staged = con.execute("SELECT name FROM sqlite_temp_master WHERE type = 'table'")
        assert len(staged.fetchall()) == 2, "the second call must not replace the first's rows"
        got = [r["GAPS"] for r in con.execute(f"SELECT d.GAPS FROM ({a}) d", [FILER, FILER])]
        assert sorted(first[int(g)].schedule for g in got) == ["A", "A"]
    finally:
        con.close()


def test_the_check_writes_nothing_to_the_database(root):
    """The left-out rows are staged in a TEMP table, so a query leaves the database as it was."""
    queries.run("calaccess.filer_total", {"filer_id": FILER}, root)
    con = sqlite3.connect(calaccess.db_path(root))
    try:
        assert not con.execute("SELECT name FROM sqlite_master WHERE name LIKE "
                               "'%UNRESTATED%'").fetchall()
    finally:
        con.close()


def test_the_listing_adds_a_left_out_gift_marked_and_lists_counted_ones_as_before(root):
    gifts = {c.amount: c for c in calaccess.contributions_to(root, FILER)}
    assert set(gifts) == {3000.0, 700.0, 400.0, 250.0, 150.0}, "one row per gift"
    veldt = gifts[400.0]
    assert veldt.filing_id == GAP_460 and veldt.unrestated == ()
    assert veldt.omitted == (calaccess.UnrestatedSchedule(GAP_460, "A", 0, 1),)
    marrow = gifts[3000.0]
    assert marrow.omitted == ()
    assert (marrow.filing_id, marrow.filings) == (F496, 1), (
        "a counted gift cites what the figures count, not the left-out copy")
    assert all(gifts[a].omitted == () for a in (250.0, 150.0))
    assert [u.schedule for u in gifts[700.0].omitted] == ["I"]

    assert [c.amount for c in calaccess.contributions_to(root, FILER, top=2)] == [3000.0, 700.0]
    later = [c.amount for c in calaccess.contributions_to(root, FILER, since="2026-01-16")]
    assert later == [250.0, 150.0], "the date window applies to left-out gifts too"


def test_the_cli_marks_the_row_and_says_what_it_means(root, monkeypatch):
    monkeypatch.setattr(cli.con, "_width", 250)   # not COLUMNS: see CLAUDE.md on widening
    out = plain(CliRunner().invoke(cli.app, ["calaccess", "contributions", FILER,
                                             "--cache", str(root)]).output)
    assert f"{GAP_460}: a1 has no schedule A, not counted" in out
    assert f"{GAP_460}: a1 has no schedule I, not counted" in out
    assert "2 row(s) are in no figure" in out and "leaves one out goes to human_review" in out


def test_a_listing_that_cannot_check_says_so(root, monkeypatch):
    """No receipts table this code builds lacks FORM_TYPE, but the contract holds: a row
    nobody checked reads as not checked, never as counted."""
    def cannot(con, table, ids):
        raise calaccess.DegradedDatabase("no FORM_TYPE")

    monkeypatch.setattr(calaccess, "unrestated_schedules", cannot)
    listed = calaccess.contributions_to(root, FILER)
    assert listed and all(c.omitted is None for c in listed)
    monkeypatch.setattr(cli.con, "_width", 250)
    out = plain(CliRunner().invoke(cli.app, ["calaccess", "contributions", FILER,
                                             "--cache", str(root)]).output)
    assert "cannot tell whether a later amendment left out a schedule" in out


def test_vg_query_warns_before_the_value_is_recorded(root, monkeypatch):
    args = ["query", "calaccess.contributor_total", "--cache", str(root)]
    for k, v in VELDT.items():
        args += ["--param", f"{k}={v}"]
    monkeypatch.setattr(cli.con, "_width", 250)
    res = CliRunner().invoke(cli.app, args)
    out = plain(res.output)
    assert res.exit_code == 0, out
    assert "250.0" in out and "Will not verify" in out and f"filing {GAP_460}'s" in out


def test_a_miss_whose_every_match_was_left_out_says_so(root, monkeypatch):
    """Larkspur's only gift is on a left-out schedule. "NO MATCH for that name" would read as
    "gave nothing"; the miss names the filing instead, and still never verifies."""
    larkspur = {"filer_id": FILER, "contributor": "Larkspur Savings", "form_type": "I"}
    result = queries.run("calaccess.contributor_total", larkspur, root)
    assert (result.found, result.value) == (False, None)
    assert shares(result.omitted) == [(GAP_460, "I", 700.0, 1)]
    assert ("every match with a readable amount is on a schedule a later amendment left out"
            in result.note)
    assert "NO MATCH" not in result.note
    s = cited("calaccess.contributor_total", larkspur, "700")
    v = verify_source(s, root).verification
    assert v.status == "human_review", "no parameters can make it reproduce; a person opens it"
    assert v.reason.startswith("the query counts nothing") and PHRASE in v.reason
    assert "nothing here reproduces the claimed '700'" in v.reason, "the claim is named too"
    assert calaccess.filing_url(GAP_460) in v.reason and "vg query" in v.reason
    s.verification = Verification(status="verified")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review" and PHRASE in v.reason and GAP_460 in v.reason

    sched_i = queries.run("calaccess.filer_total", {"filer_id": FILER, "form_type": "I"}, root)
    assert not sched_i.found and shares(sched_i.omitted) == [(GAP_460, "I", 700.0, 1)]
    assert "NO MATCH" not in sched_i.note

    args = ["query", "calaccess.contributor_total", "--cache", str(root)]
    for k, val in larkspur.items():
        args += ["--param", f"{k}={val}"]
    monkeypatch.setattr(cli.con, "_width", 250)
    res = CliRunner().invoke(cli.app, args)
    out = plain(res.output)
    assert res.exit_code == 1, out
    assert "nothing counted" in out and "no match" not in out
    assert f"Not a finding: it leaves out {PHRASE}" in out
    assert calaccess.filing_url(GAP_460) in out


def test_a_miss_carries_what_a_left_out_readable_gift_would_change(root):
    """Quillon's only readable gift is on left-out schedule A. top_contributor ranks schedule
    A by default, so it finds nothing to rank; and a total over every schedule counts only a
    gift with no amount. A restated schedule A would turn either miss into a figure. Ranked
    over every schedule, the gift with no amount names no leader whatever the left-out rows
    hold, so that miss is not flagged."""
    top = queries.run("calaccess.top_contributor", {"filer_id": QUILL}, root)
    assert not top.found and shares(top.omitted) == [(QUILL_GAP, "A", 900.0, 1)]
    assert "NO MATCH" not in top.note and "left out" in top.note

    quillon = {"filer_id": QUILL, "contributor": "Quillon", "contributor_first": "Maren",
               "form_type": ""}
    total = queries.run("calaccess.contributor_total", quillon, root)
    assert not total.found and "no readable amount" in total.detail
    assert shares(total.omitted) == [(QUILL_GAP, "A", 900.0, 1)]
    assert "every match with a readable amount" in total.note

    every = queries.run("calaccess.top_contributor", {"filer_id": QUILL, "form_type": ""}, root)
    assert not every.found and every.omitted == []


BLANK_FILER = "8884600"
BLANK_SETTLED = "8884601"  # schedule A at amendment 0: Veldt's $500
BLANK_GAP = "8884602"      # schedule A at amendment 0, Oyelaran's; amendment 1 has only C
BLANK_MORE = "8884603"     # schedule A at amendment 0, Hollis's $900; amendment 1 has only C


def _blank_export(root: Path, *, amount: str = "", amended: bool = True, settled: bool = True,
                  more: bool = False) -> Path:
    """A filer whose one left-out schedule-A gift has `amount` as filed: blank by default.
    `amended=False` drops the amendment that leaves it out, so it is counted; `settled=False`
    drops the only counted gift; `more=True` adds a second left-out filing with a readable
    $900."""
    gifts = [(BLANK_GAP, 0, "A-600002", "Oyelaran", "Tamsin", "2/5/2026", amount, "A")]
    if settled:
        gifts.append((BLANK_SETTLED, 0, "A-600001", "Veldt", "Ansel", "1/5/2026", "500", "A"))
    if amended:
        gifts.append((BLANK_GAP, 1, "C-600003", "Oyelaran", "Tamsin", "2/6/2026", "80", "C"))
    if more:
        gifts += [(BLANK_MORE, 0, "A-600004", "Hollis", "Wren", "2/9/2026", "900", "A"),
                  (BLANK_MORE, 1, "C-600005", "Hollis", "Wren", "2/10/2026", "60", "C")]
    receipts = RECEIPTS.splitlines(keepends=True)[0] + "".join(
        f"{f}\t{a}\t{t}\t1\t{last}\t{first}\t\t\t{day} 12:00:00 AM\t{amt}\t{form}\n"
        for f, a, t, last, first, day, amt, form in gifts)
    filings = {(f, a) for f, a, *_ in gifts}
    covers = COVERS.splitlines(keepends=True)[0] + "".join(
        f"{f}\t{a}\t{BLANK_FILER}\tFriends of Example\tF460\t{start}\t{end}\n"
        for f, a in sorted(filings)
        for start, end in [JANUARY if f == BLANK_SETTLED else FEBRUARY])
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS.splitlines(keepends=True)[0]
                    + "".join(f"{BLANK_FILER}\t{f}\tF460\t3/1/2026 12:00:00 AM\n"
                              for f in sorted({f for f, _ in filings})))
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    calaccess.build(root)
    return root


def unread(found):
    return [(u.filing_id, u.schedule, u.amount, u.rows, u.unread) for u in found]


def test_a_ranking_is_unsettled_by_a_left_out_gift_with_no_readable_amount(tmp_path):
    """Oyelaran's gift has no amount, and a later amendment left its schedule out. Counted, it
    would name no largest contributor, since a gift of unknown size could make anyone largest.
    Left out, the $500 leader stood with nothing flagged, and a citation of it verified green
    (#156). The ranking now leaves the gift out as it leaves out one with an amount: held for a
    person, naming the filing."""
    root = _blank_export(tmp_path)
    top = {"filer_id": BLANK_FILER}
    result = queries.run("calaccess.top_contributor", top, root)
    assert (result.found, result.value) == (True, "Ansel Veldt"), "the flag never moves a value"
    assert unread(result.omitted) == [(BLANK_GAP, "A", 0.0, 0, 1)]
    v = verify_source(cited("calaccess.top_contributor", top, "Ansel Veldt"), root).verification
    assert v.status == "human_review", "a gift of unknown size left out must not verify green"
    assert PHRASE in v.reason and calaccess.filing_url(BLANK_GAP) in v.reason
    assert (f"filing {BLANK_GAP}'s schedule A rows are from amendment 0, and its later "
            "amendment 1 has rows on other schedules but none on that one: 1 row(s) with no "
            "readable amount this result leaves out") in v.reason
    assert "$0.00" not in v.reason, "a blank amount is not zero"
    assert "no largest contributor can be named" in v.reason

    s = cited("calaccess.top_contributor", top, "Ansel Veldt")
    s.verification = Verification(status="verified")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review" and BLANK_GAP in v.reason

    # A total keeps leaving it out unflagged: a blank amount changes no total.
    total = queries.run("calaccess.filer_total", top, root)
    assert (total.value, total.omitted) == (500.0, [])
    assert verify_source(cited("calaccess.filer_total", top, "500"),
                         root).verification.status == "verified"


def test_the_controls_left_out_with_an_amount_and_counted_blank(tmp_path):
    """With an amount, the same gift was already flagged. Not left out, it is counted, and a
    ranking holding a gift of unknown size names no leader at all."""
    top = {"filer_id": BLANK_FILER}
    stated = _blank_export(tmp_path / "stated", amount="700")
    result = queries.run("calaccess.top_contributor", top, stated)
    assert result.value == "Ansel Veldt"
    assert unread(result.omitted) == [(BLANK_GAP, "A", 700.0, 1, 0)]
    assert verify_source(cited("calaccess.top_contributor", top, "Ansel Veldt"),
                         stated).verification.status == "human_review"

    counted = _blank_export(tmp_path / "counted", amended=False)
    result = queries.run("calaccess.top_contributor", top, counted)
    assert not result.found and result.omitted == []
    assert "no largest contributor can be named" in result.detail


def test_a_share_with_no_readable_amount_comes_first(tmp_path):
    """A gift of unknown size could outweigh any stated share, so the reason, which names only
    the first few, names it before larger stated ones, as a late report with no amount is."""
    root = _blank_export(tmp_path, more=True)
    result = queries.run("calaccess.top_contributor", {"filer_id": BLANK_FILER}, root)
    assert unread(result.omitted) == [(BLANK_GAP, "A", 0.0, 0, 1),
                                      (BLANK_MORE, "A", 900.0, 1, 0)]
    both = calaccess.UnrestatedSchedule(BLANK_GAP, "A", 0, 1, amount=900.0, rows=1, unread=2)
    assert ": $900.00 in 1 row(s), plus 2 row(s) with no readable amount, this result leaves " \
           "out (open" in queries.share_text(both)


def test_a_ranking_with_nothing_counted_is_a_miss_whatever_its_blank_gift_holds(tmp_path):
    """With no counted gift, the only match is the left-out gift with no amount. Kept by the
    amendment, it names no largest contributor; withdrawn, nothing is counted. Either way the
    ranking names nobody, so the filing is not one a person need open, and the miss stays one
    to retry rather than going to human_review."""
    root = _blank_export(tmp_path / "alone", settled=False)
    result = queries.run("calaccess.top_contributor", {"filer_id": BLANK_FILER}, root)
    assert not result.found and result.omitted == [] and not result.unsettled
    v = verify_source(cited("calaccess.top_contributor", {"filer_id": BLANK_FILER},
                            "Tamsin Oyelaran"), root).verification
    assert v.status == "snippet_not_found"

    # Beside a left-out gift with an amount it is named too: a person who restores Hollis's
    # $900 as the leader has to see the gift of unknown size that could overturn it.
    root = _blank_export(tmp_path / "beside", settled=False, more=True)
    result = queries.run("calaccess.top_contributor", {"filer_id": BLANK_FILER}, root)
    assert not result.found
    assert unread(result.omitted) == [(BLANK_GAP, "A", 0.0, 0, 1),
                                      (BLANK_MORE, "A", 900.0, 1, 0)]
    v = verify_source(cited("calaccess.top_contributor", {"filer_id": BLANK_FILER},
                            "Wren Hollis"), root).verification
    assert v.status == "human_review"
    assert BLANK_GAP in v.reason and BLANK_MORE in v.reason


def test_a_reason_listing_a_share_of_unknown_size_first_does_not_call_the_rest_smaller():
    """Shares with a gift of unknown size come first, so one cut off after them can hold more
    stated money than any listed: the reason must not call it smaller."""
    blank = [calaccess.UnrestatedSchedule(f"888470{i}", "A", 0, 1, unread=1)
             for i in range(queries.UNSETTLED_SHOWN)]
    stated = calaccess.UnrestatedSchedule("8884799", "A", 0, 1, amount=900.0, rows=1)
    reason = queries.QueryResult(value="Ansel Veldt", omitted=blank + [stated]).unsettled
    assert "and 1 more schedule(s), which `vg query` lists" in reason
    assert "smaller shares" not in reason and "8884799" not in reason
    assert "no largest contributor can be named" in reason


def test_a_left_out_miss_still_names_the_schedules_that_count_the_gift(root):
    """On schedule A, Marrow Creek's only copy is left out, and the Form 496 copy that counts
    the same gift is on another schedule. The miss says both: the left-out filing, and the
    form_type that counts it, which is a different figure the claim decides on."""
    marrow = {"filer_id": FILER, "contributor": "Marrow Creek PAC"}
    result = queries.run("calaccess.contributor_total", marrow, root)
    assert not result.found and shares(result.omitted) == [(GAP_460, "A", 3000.0, 1)]
    assert "left out. Not counted here: form_type=F496P3" in result.note


def test_a_reason_with_both_kinds_names_each_and_vg_query_lists_the_rest(tmp_path,
                                                                         monkeypatch):
    """A value can count rows a cover's later amendment lacks and leave out a schedule too.
    The reason says both, each naming its largest few, and `vg query` lists every one."""
    counted = [calaccess.Unrestated("8884301", 0, 1, amount=90.0, rows=1)]
    left_out = [calaccess.UnrestatedSchedule(f"888440{i}", "A", 0, 1, amount=500.0 - i, rows=1)
                for i in range(queries.UNSETTLED_SHOWN + 2)]
    result = queries.QueryResult(value=1.0, unrestated=counted, omitted=left_out)
    reason = result.unsettled
    assert reason.startswith("it counts rows a later amendment may have withdrawn")
    assert ". It also leaves out rows a later amendment may have withdrawn" in reason
    assert "8884301" in reason and "more filing(s)" not in reason
    assert all(u.filing_id in reason for u in left_out[:queries.UNSETTLED_SHOWN])
    assert not any(u.filing_id in reason for u in left_out[queries.UNSETTLED_SHOWN:])
    assert "and 2 more schedule(s) with smaller shares, which `vg query` lists" in reason

    monkeypatch.setattr(queries, "run", lambda name, params, root: result)
    monkeypatch.setattr(cli.con, "_width", 250)
    out = plain(CliRunner().invoke(cli.app, ["query", "calaccess.filer_total", "--param",
                                             f"filer_id={FILER}", "--cache", str(tmp_path)]).output)
    assert all(out.count(f"filing {u.filing_id}'s") == 1 for u in counted + left_out), (
        "every filing, each once")


def test_expenditure_schedules_are_checked_too(root):
    """EXPN_CD holds several schedules as well, so the build loads its FORM_TYPE and the same
    check reads it."""
    assert "FORM_TYPE" in calaccess.WANTED["EXPN_CD"]
    con = calaccess.connect(root)
    try:
        got = calaccess.unrestated_schedules(con, "EXPN_CD", [EXP_GAP])
    finally:
        con.close()
    assert got == [calaccess.UnrestatedSchedule(EXP_GAP, "F", 0, 1)]


def test_a_table_without_form_type_is_refused_until_rebuilt(tmp_path):
    """A database built before EXPN_CD.FORM_TYPE was loaded cannot tell its schedules apart,
    and a check that cannot run must not read as settled."""
    root = _export(tmp_path, expn_form_type=False)
    con = calaccess.connect(root)
    try:
        with pytest.raises(calaccess.DegradedDatabase,
                           match=r"no EXPN_CD\.FORM_TYPE.*uv run vg calaccess build"):
            calaccess.unrestated_schedules(con, "EXPN_CD", [EXP_GAP])
        assert calaccess.unrestated_schedules(con, "NO_SUCH_CD", [EXP_GAP]) == [], (
            "a table that is not there has no rows to leave out")
    finally:
        con.close()
