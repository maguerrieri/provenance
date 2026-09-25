"""The schedule a receipt figure counts must not hide a gift's other reports from #31's flag.

A gift over the 24-hour threshold is reported twice: on a Form 460 schedule A and on a Form 496
Part 3, and DEDUPED_RECEIPTS collapses the two into one gift. The receipt figures count schedule
A by default (#47), and filtered rows by schedule BEFORE that grouping, so the Form 496 copy
never reached it. When that copy's filing was the one whose latest amendment dropped its rows
(`calaccess.Unrestated`), the flag never saw the filing, and the figure verified green under the
default and under form_type=A, while every schedule ("") sent the same figure to human_review.

The schedule decides which gifts a figure counts; every filing a counted gift was reported on
decides the flag. Every filing here is synthetic, in the export's own formats: "M/D/YYYY
12:00:00 AM" dates and integer amendment ids.
"""

from __future__ import annotations

import zipfile

import pytest

from vgpipe import calaccess, queries
from vgpipe.models import QueryCitation, Source, Verification
from vgpipe.verify import revalidate_from_cache, verify_source

FILER = "7770100"
SETTLED_460 = "7770101"      # amendments 0 and 1, and amendment 1 restates every row
DROPPED_496 = "7770102"      # rows only at amendment 0; the cover's latest amendment is 1

RECEIPTS = (
    "FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP\tCTRIB_OCC"
    "\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n"
    # One gift, two reports: schedule A on the settled 460, Part 3 on the unrestated 496.
    f"{SETTLED_460}\t0\tA-300001\t1\tPellworth Orchards\t\t\t\t1/20/2026 12:00:00 AM\t2000\tA\n"
    f"{SETTLED_460}\t1\tA-300001\t1\tPellworth Orchards\t\t\t\t1/20/2026 12:00:00 AM\t2000\tA\n"
    f"{DROPPED_496}\t0\tF496P3-300001\t1\tPellworth Orchards\t\t\t\t1/20/2026 12:00:00 AM"
    "\t2000\tF496P3\n"
    # A settled schedule-A gift from someone else, and one from a contributor whose other gift
    # is reported only on the unrestated 496: no report of the schedule-A gift is unrestated.
    f"{SETTLED_460}\t1\tA-300002\t2\tHarrowgate\tSelma\t\t\t1/12/2026 12:00:00 AM\t250\tA\n"
    f"{SETTLED_460}\t1\tA-300003\t3\tTamsin Vale Trust\t\t\t\t1/14/2026 12:00:00 AM\t1500\tA\n"
    f"{DROPPED_496}\t0\tF496P3-300009\t2\tTamsin Vale Trust\t\t\t\t1/22/2026 12:00:00 AM"
    "\t3000\tF496P3\n"
)
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{SETTLED_460}\tF460\t1/31/2026 12:00:00 AM\n"
           f"{FILER}\t{SETTLED_460}\tF460\t2/14/2026 12:00:00 AM\n"
           f"{FILER}\t{DROPPED_496}\tF496\t1/23/2026 12:00:00 AM\n"
           f"{FILER}\t{DROPPED_496}\tF496\t2/2/2026 12:00:00 AM\n")
# The 460 covers January, so the Part-3-only gift is not a pending late report (#66): these
# tests are about which reports decide the unrestated flag, not about late gifts.
COVERS = ("FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD"
          "\tFORM_TYPE\tFROM_DATE\tTHRU_DATE\n"
          + "".join(f"{f}\t{a}\t{FILER}\tCommittee for Example\t\t\t\t{form}\t{period}\n"
                    for f, form, period in (
                        (SETTLED_460, "F460", "1/1/2026 12:00:00 AM\t1/31/2026 12:00:00 AM"),
                        (DROPPED_496, "F496", "\t"))
                    for a in ("0", "1")))

# Every receipt figure over the cross-form gift, under the default, schedule A named, and every
# schedule, with the value each must still return. #31's tests were adapted to "" during
# integration, and "" is the one spelling that never hid the Form 496 copy.
FORM_TYPES = ({}, {"form_type": "A"}, {"form_type": ""})
CROSS_FORM = (
    ("calaccess.contributor_total", {"contributor": "Pellworth Orchards"},
     {"A": 2000.0, "": 2000.0}),
    ("calaccess.filer_total", {}, {"A": 3750.0, "": 6750.0}),
    ("calaccess.top_contributor", {}, {"A": "Pellworth Orchards", "": "Tamsin Vale Trust"}),
)


def _build(root, receipts=RECEIPTS, filings=FILINGS, covers=COVERS):
    (root / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    calaccess.build(root)
    return root


@pytest.fixture
def root(tmp_path):
    return _build(tmp_path)


def cited(name, params, expected):
    return Source(url="https://committee.example/report", publisher="Example Committee",
                  author="", date="2026-02-14", source_type="primary_document",
                  snippet="itemized contributions",
                  query=QueryCitation(name=name, params=params, expected=expected))


def _params(extra, schedule):
    return {"filer_id": FILER, **extra, **schedule}


@pytest.mark.parametrize("name,extra,values", CROSS_FORM, ids=[n for n, _, _ in CROSS_FORM])
@pytest.mark.parametrize("schedule", FORM_TYPES, ids=["default", "A", "every"])
def test_a_gift_whose_form_496_report_is_unrestated_goes_to_human_review(root, name, extra,
                                                                        values, schedule):
    """The acceptance case. The schedule-A report is settled; the Part 3 report of the same
    gift is on a filing whose latest amendment has no receipt rows. Which report stands is the
    open question #31 flags, whichever schedule the figure counts."""
    params = _params(extra, schedule)
    expected = values[schedule.get("form_type", "A")]
    result = queries.run(name, params, root)
    assert result.value == expected, "the flag must never change the number"
    assert DROPPED_496 in [u.filing_id for u in result.unrestated], result.unrestated

    v = verify_source(cited(name, params, str(expected)), root).verification
    assert v.status == "human_review", f"{name} {schedule}: {v.status}: {v.reason}"
    assert DROPPED_496 in v.reason and calaccess.filing_url(DROPPED_496) in v.reason


def test_the_share_named_is_the_gift_the_figure_counts(root):
    """Under schedule A the figure rests on the unrestated filing for the one gift it counts,
    not for the Part-3-only gift it leaves out."""
    result = queries.run("calaccess.filer_total", {"filer_id": FILER}, root)
    assert [(u.filing_id, u.amount, u.rows) for u in result.unrestated] == [
        (DROPPED_496, 2000.0, 1)]


def test_build_downgrades_the_schedule_a_figure_verified_before(root):
    """A claim file carrying `verified` from before the fix re-runs at build and goes to
    human_review, like any row that does not reproduce clean."""
    s = cited("calaccess.contributor_total",
              {"filer_id": FILER, "contributor": "Pellworth Orchards"}, "2000")
    s.verification = Verification(status="verified", reason="verified earlier",
                                  context="contributor_total = 2000", support="supports")
    v = revalidate_from_cache(s, root).verification
    assert v.status == "human_review" and DROPPED_496 in v.reason


@pytest.mark.parametrize("schedule", FORM_TYPES[:2], ids=["default", "A"])
def test_a_gift_not_counted_does_not_unsettle_one_that_is(root, schedule):
    """Only the counted gift's own reports decide the flag. The trust's schedule-A gift was
    reported once and is settled; its other gift, reported only on the unrestated 496, is not
    on schedule A, so the figure neither counts it nor rests on its filing."""
    params = {"filer_id": FILER, "contributor": "Tamsin Vale Trust", **schedule}
    result = queries.run("calaccess.contributor_total", params, root)
    assert result.value == 1500.0 and result.unrestated == []
    assert verify_source(cited("calaccess.contributor_total", params, "1500"),
                         root).verification.status == "verified"

    individual = {"filer_id": FILER, "contributor": "Harrowgate", "contributor_first": "Selma",
                  **schedule}
    assert verify_source(cited("calaccess.contributor_total", individual, "250"),
                         root).verification.status == "verified"


def test_every_schedule_still_counts_what_it_did(root):
    """The fix moves the schedule test after the grouping; which gifts each schedule counts,
    and so every value, must not move with it. A gift reported only on Form 496 Part 3 is still
    left out of schedule A (#66), and a cross-form gift still counts once."""
    got = {ft: queries.run("calaccess.filer_total", {"filer_id": FILER, "form_type": ft},
                           root).value
           for ft in ("A", "F496P3", "C", "")}
    assert got == {"A": 3750.0, "F496P3": 5000.0, "C": None, "": 6750.0}

    trust = queries.run("calaccess.contributor_total",
                        {"filer_id": FILER, "contributor": "Tamsin Vale Trust", "form_type": ""},
                        root)
    assert trust.value == 4500.0
    assert [u.filing_id for u in trust.unrestated] == [DROPPED_496]


def test_a_gift_counted_from_form_496_is_flagged_by_its_schedule_a_report(tmp_path):
    """The other direction: the gift is counted as Form 496 Part 3, from a settled 496, and its
    schedule-A report is on a 460 whose latest amendment has no receipt rows."""
    settled_496, dropped_460 = "7770103", "7770104"
    gift = "\tBrackwater Mills\t\t\t\t2/3/2026 12:00:00 AM\t5000\t"
    receipts = (RECEIPTS.splitlines(keepends=True)[0]
                + f"{dropped_460}\t0\tA-500001\t1{gift}A\n"
                + "".join(f"{settled_496}\t{a}\tF496P3-500001\t1{gift}F496P3\n"
                          for a in ("0", "1")))
    filings = (FILINGS.splitlines(keepends=True)[0]
               + f"{FILER}\t{dropped_460}\tF460\t2/20/2026 12:00:00 AM\n"
               + f"{FILER}\t{settled_496}\tF496\t2/4/2026 12:00:00 AM\n")
    covers = (COVERS.splitlines(keepends=True)[0]
              + "".join(f"{f}\t{a}\t{FILER}\tCommittee for Example\t\t\t\t{form}\n"
                        for f, form in ((dropped_460, "F460"), (settled_496, "F496"))
                        for a in ("0", "1")))
    root = _build(tmp_path, receipts, filings, covers)

    for form_type in ("F496P3", "A", ""):
        params = {"filer_id": FILER, "form_type": form_type}
        result = queries.run("calaccess.filer_total", params, root)
        assert result.value == 5000.0, form_type
        assert [(u.filing_id, u.amount) for u in result.unrestated] == [(dropped_460, 5000.0)]
        v = verify_source(cited("calaccess.filer_total", params, "5000"), root).verification
        assert v.status == "human_review" and dropped_460 in v.reason, form_type


def test_the_gift_test_counts_exactly_what_the_row_filter_did(tmp_path):
    """Moving the schedule after the grouping is only safe if it counts the same gifts. Over
    seeded random receipts (three filings across two amendments, one of them another filer's;
    shared and unshared transaction bases; names differing in case and padding; blank and
    unreadable amounts), the new placement must give the old one's sum, count, gifts and first
    names for every schedule, filer-wide and narrowed to a contributor as contributor_total
    narrows it, and the same per-contributor totals top_contributor ranks. The old placement is
    the same view with the schedule as a row filter, written out as v4 had it, and every gift
    kept."""
    import random

    other_filer, other_filing = "7770199", "7770105"
    rng = random.Random(131)
    rows = []
    for i in range(600):
        base = rng.randrange(60)
        form = rng.choice(["A", "A", "F496P3", "C", "I", ""])
        prefix = {"A": "A-", "F496P3": "F496P3-", "C": "C-", "I": "I-", "": ""}[form]
        rows.append("\t".join([
            rng.choice([SETTLED_460, DROPPED_496, other_filing]), rng.choice(["0", "1"]),
            f"{prefix}{base}", str(i),
            rng.choice(["Pellworth Orchards", "PELLWORTH ORCHARDS ", "Harrowgate"]),
            rng.choice(["", "Selma", "selma "]), "", "",
            f"1/{base % 28 + 1}/2026 12:00:00 AM",
            rng.choice(["2000", "250", "1500", "", "N/A", "1,000", str(base * 10)]), form]))
    _build(tmp_path, RECEIPTS.splitlines(keepends=True)[0] + "\n".join(rows) + "\n",
           FILINGS + f"{other_filer}\t{other_filing}\tF460\t1/31/2026 12:00:00 AM\n")

    whose = {"everyone": ("", []),
             "Pellworth": (" AND UPPER(TRIM(r.CTRIB_NAML)) = UPPER(TRIM(?))",
                           ["Pellworth Orchards"]),
             "Selma Harrowgate": (" AND UPPER(TRIM(r.CTRIB_NAML)) = UPPER(TRIM(?))"
                                  " AND UPPER(TRIM(COALESCE(r.CTRIB_NAMF,''))) = UPPER(TRIM(?))",
                                  ["Harrowgate", "Selma"])}
    stats = ("SELECT SUM(d.AMT), COUNT(d.AMT), COUNT(*),"
             " COUNT(DISTINCT UPPER(TRIM(COALESCE(d.CTRIB_NAMF,'')))) FROM ({}) d")
    ranked = ("SELECT UPPER(TRIM(d.CTRIB_NAML)) nm, UPPER(TRIM(COALESCE(d.CTRIB_NAMF,''))) nf,"
              " SUM(d.AMT), COUNT(d.AMT), COUNT(*) FROM ({}) d GROUP BY nm, nf ORDER BY nm, nf")
    con = calaccess.connect(tmp_path)
    try:
        for form_type in ("A", "F496P3", "C", "I", ""):
            counted, sched_args, _ = queries._schedule(form_type)
            row_filter = " AND UPPER(TRIM(r.FORM_TYPE)) = UPPER(TRIM(?))" if form_type else ""
            for who, (who_sql, who_args) in whose.items():
                args = [FILER, *who_args, *sched_args]
                new = queries.DEDUPED_RECEIPTS.format(extra=who_sql, counted=counted)
                old = queries.DEDUPED_RECEIPTS.format(extra=who_sql + row_filter, counted="1")
                for sql in (stats, ranked):
                    assert (list(map(tuple, con.execute(sql.format(new), args)))
                            == list(map(tuple, con.execute(sql.format(old), args)))), (
                        form_type, who, sql)
                gifts = con.execute(stats.format(new), args).fetchone()[2]
                assert gifts > 0, f"no {who} gifts on {form_type!r}: nothing proved there"
    finally:
        con.close()


def test_a_gift_left_out_on_its_schedule_is_flagged_though_another_report_counts(tmp_path):
    """#92's left-out check groups the same way, and tests the schedule per report too. Here a
    460's later amendment has rows on schedule C and none on A, so its schedule-A report of a
    gift is left out, while the same gift's Form 496 report is counted. No schedule-A figure
    counts the gift, so it is left out of one; over every schedule the 496 report counts it.
    Tested on the grouped rows as a whole, the schedule-A report would have made the gift read
    as counted, in no figure and flagged by none."""
    left_460, counted_496 = "7770106", "7770107"
    gift = "\tQuenby Hollis Fund\t\t\t\t2/10/2026 12:00:00 AM\t7000\t"
    receipts = (RECEIPTS.splitlines(keepends=True)[0]
                + f"{left_460}\t0\tA-600001\t1{gift}A\n"
                + f"{left_460}\t1\tC-600002\t1\tQuenby Hollis Fund\t\t\t\t2/12/2026 12:00:00 AM"
                  "\t300\tC\n"
                + f"{counted_496}\t0\tF496P3-600001\t1{gift}F496P3\n")
    filings = (FILINGS.splitlines(keepends=True)[0]
               + f"{FILER}\t{left_460}\tF460\t3/1/2026 12:00:00 AM\n"
               + f"{FILER}\t{counted_496}\tF496\t2/11/2026 12:00:00 AM\n")
    # the 460 covers February, so the 496 report is not a pending late report (#66)
    covers = (COVERS.splitlines(keepends=True)[0]
              + "".join(f"{left_460}\t{a}\t{FILER}\tCommittee for Example\t\t\t\tF460"
                        "\t2/1/2026 12:00:00 AM\t2/28/2026 12:00:00 AM\n" for a in ("0", "1"))
              + f"{counted_496}\t0\t{FILER}\tCommittee for Example\t\t\t\tF496\t\t\n")
    root = _build(tmp_path, receipts, filings, covers)

    who = {"contributor": "Quenby Hollis Fund"}
    for name, extra in (("calaccess.contributor_total", who), ("calaccess.filer_total", {})):
        for schedule in FORM_TYPES[:2]:
            params = _params(extra, schedule)
            result = queries.run(name, params, root)
            assert result.value is None, (name, schedule, result.value)
            assert [(u.filing_id, u.schedule, u.amount) for u in result.omitted] == [
                (left_460, "A", 7000.0)], (name, schedule)
            v = verify_source(cited(name, params, "7000"), root).verification
            assert v.status == "human_review" and left_460 in v.reason, (name, schedule)

    every = queries.run("calaccess.contributor_total",
                        {"filer_id": FILER, **who, "form_type": ""}, root)
    assert every.value == 7300.0 and every.omitted == [], "the 496 report counts it"


def test_a_left_out_gift_names_only_the_schedule_the_figure_counts(tmp_path):
    """A gift reported on schedules A and C of one 460, both left out by a later amendment with
    rows only on schedule I. A schedule-A figure leaves it out of schedule A; its C report is
    not what that figure leaves out, and naming it put a $7,000 schedule-C share on a
    schedule-A figure, the one gift counted twice across the two. Over every schedule, both."""
    left_460 = "7770108"
    gift = "\tQuenby Hollis Fund\t\t\t\t2/10/2026 12:00:00 AM\t7000\t"
    receipts = (RECEIPTS.splitlines(keepends=True)[0]
                + f"{left_460}\t0\tA-700001\t1{gift}A\n"
                + f"{left_460}\t0\tC-700001\t2{gift}C\n"
                + f"{left_460}\t1\tI-700002\t1\tExample Credit Union\t\t\t\t2/12/2026 12:00:00 AM"
                  "\t40\tI\n")
    filings = (FILINGS.splitlines(keepends=True)[0]
               + f"{FILER}\t{left_460}\tF460\t3/1/2026 12:00:00 AM\n")
    covers = (COVERS.splitlines(keepends=True)[0]
              + "".join(f"{left_460}\t{a}\t{FILER}\tCommittee for Example\t\t\t\tF460"
                        "\t2/1/2026 12:00:00 AM\t2/28/2026 12:00:00 AM\n" for a in ("0", "1")))
    root = _build(tmp_path, receipts, filings, covers)

    for form_type, named in (("A", ["A"]), ("C", ["C"]), ("", ["A", "C"])):
        result = queries.run("calaccess.filer_total",
                             {"filer_id": FILER, "form_type": form_type}, root)
        assert sorted((u.schedule, u.amount) for u in result.omitted) == [
            (s, 7000.0) for s in named], form_type
