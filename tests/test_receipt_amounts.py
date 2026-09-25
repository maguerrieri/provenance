"""A receipt whose AMOUNT is not a number is money nobody stated, never a found $0.00.

The receipt queries summed `CAST(AMOUNT AS REAL)` and counted rows, so a blank AMOUNT added 0.0
and still counted as a gift: a contributor whose only gift had one came back found, "$0.00, 1
itemized gift(s)", and a recorded `expected` of "0" verified against it. `ie_total` got the same
fix first; these hold the receipt side to it.
"""

from __future__ import annotations

import re
import sqlite3
import zipfile

import pytest

from vgpipe import calaccess, cli, queries
from vgpipe.models import QueryCitation, Source
from vgpipe.verify import verify_source

# What CAST reads as a number nobody filed: "" and "N/A" as 0.0, "1,000" as 1.0, "$100" as 0.0.
UNREADABLE = ["", "   ", "N/A", "-", "1,000", "$100", "1.2.3", "[/]"]

MIXED = "8000001"      # a filer with readable gifts and one unreadable one
ONLY = "8000002"       # a filer whose one gift has no readable amount


def _receipts(tmp_path):
    """Two filers' schedule-A gifts, dated and keyed as the export carries them. T2 and T9 are
    the rows each test makes unreadable."""
    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '8100001\t0\tT1\t1\tQuill PAC\t\t\t\t1/5/2026 12:00:00 AM\t500\tA\n'
                '8100001\t0\tT2\t2\tQuill PAC\t\t\t\t1/6/2026 12:00:00 AM\t250\tA\n'
                '8100001\t0\tT3\t3\tMarlow\tTess\t\t\t1/7/2026 12:00:00 AM\t300\tA\n'
                '8100002\t0\tT9\t1\tCedar Trust\t\t\t\t1/10/2026 12:00:00 AM\t700\tA\n')
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               f'{MIXED}\t8100001\tF460\t2/1/2026 12:00:00 AM\n'
               f'{ONLY}\t8100002\tF460\t2/1/2026 12:00:00 AM\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)
    return tmp_path


def _set(root, sql, *args):
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute(sql, args)
    con.commit()
    con.close()


def _set_amount(root, tran_id, amount):
    _set(root, "UPDATE RCPT_CD SET AMOUNT = ? WHERE TRAN_ID = ?", amount, tran_id)


def _verifies(root, name, params, expected):
    cited = Source(url="https://cal-access.example/filing", publisher="CAL-ACCESS",
                   author="CAL-ACCESS", source_type="primary_document", snippet="",
                   query=QueryCitation(name=name, params=params, expected=expected))
    return verify_source(cited, root).verification.status == "verified"


ONLY_QUERIES = [
    ("calaccess.contributor_total", {"filer_id": ONLY, "contributor": "Cedar Trust"}),
    ("calaccess.filer_total", {"filer_id": ONLY}),
    ("calaccess.top_contributor", {"filer_id": ONLY}),
]


@pytest.mark.parametrize("unread", UNREADABLE)
@pytest.mark.parametrize(("name", "params"), ONLY_QUERIES)
def test_only_unreadable_amounts_are_not_a_found_zero(tmp_path, name, params, unread):
    """A total built only from amounts nobody stated is a miss that names them, not "$0.00"."""
    root = _receipts(tmp_path)
    _set_amount(root, "T9", unread)

    got = queries.run(name, params, root)
    assert got.value is None and not got.found and got.rows == 0, got
    assert "1 more gift(s) with no readable amount, not counted" in got.note
    for expected in ("0", "0.00", "700", "Cedar Trust"):
        assert not _verifies(root, name, params, expected), expected


@pytest.mark.parametrize("unread", UNREADABLE)
def test_a_mixed_total_sums_only_the_stated_amounts_and_says_how_many_it_left_out(
        tmp_path, unread):
    root = _receipts(tmp_path)
    _set_amount(root, "T2", unread)   # one of Quill PAC's two gifts

    quill = queries.run("calaccess.contributor_total",
                        {"filer_id": MIXED, "contributor": "Quill PAC"}, root)
    assert quill.found and quill.value == 500.0 and quill.rows == 1
    assert quill.note == ("1 itemized schedule-A gift(s); 1 more gift(s) with no readable "
                          "amount, not counted")

    total = queries.run("calaccess.filer_total", {"filer_id": MIXED}, root)
    assert total.found and total.value == 800.0 and total.rows == 2
    assert total.note == ("2 itemized schedule-A gift(s); 1 more gift(s) with no readable "
                          "amount, not counted")

    # the figure the unreadable row used to inflate or zero no longer reproduces
    for name, params in (("calaccess.contributor_total",
                          {"filer_id": MIXED, "contributor": "Quill PAC"}),
                         ("calaccess.filer_total", {"filer_id": MIXED})):
        assert _verifies(root, name, params, "500" if "contributor" in params else "800")
        wrong = "750" if "contributor" in params else "1050"
        assert not _verifies(root, name, params, wrong)


@pytest.mark.parametrize("zero", ["0", "0.00", "-0", " 0 "])
def test_a_stated_zero_is_the_filers_figure_and_counts(tmp_path, zero):
    """Only an amount that is not a number is unknown money. A "0" was filed, and counts."""
    root = _receipts(tmp_path)
    _set_amount(root, "T9", zero)

    one = queries.run("calaccess.contributor_total",
                      {"filer_id": ONLY, "contributor": "Cedar Trust"}, root)
    assert one.found and one.value == 0.0 and one.rows == 1
    assert "no readable amount" not in one.note
    total = queries.run("calaccess.filer_total", {"filer_id": ONLY}, root)
    assert total.found and total.value == 0.0 and total.rows == 1
    top = queries.run("calaccess.top_contributor", {"filer_id": ONLY}, root)
    assert top.found and top.value == "Cedar Trust"


@pytest.mark.parametrize("unread", UNREADABLE)
def test_no_largest_contributor_is_named_while_a_gift_has_no_amount(tmp_path, unread):
    """A total can say what the stated gifts come to and name what it left out. A rank cannot:
    a gift of unknown size could make anyone the largest, and "the largest contributor" is a
    publishable sentence. So the stated leader is named as a lead, and nothing verifies."""
    root = _receipts(tmp_path)
    _set_amount(root, "T3", unread)   # Tess Marlow's only gift, not the leader's

    top = queries.run("calaccess.top_contributor", {"filer_id": MIXED}, root)
    assert top.value is None and not top.found, top
    assert top.note.startswith("Quill PAC leads the stated amounts at $750 in schedule-A gifts, "
                               "but no largest contributor can be named; 1 more gift(s) to this "
                               "filer with no readable amount, not counted")
    for expected in ("Quill PAC", "Tess Marlow"):
        assert not _verifies(root, "calaccess.top_contributor", {"filer_id": MIXED}, expected)


def test_a_contributor_with_only_unreadable_gifts_never_ties_for_top(tmp_path):
    """Read as 0.0, a contributor whose gifts all had blank amounts tied one whose stated total
    was $0, and the tie was reported as a finding: "2-WAY TIE at $0". A tie among STATED
    totals is still reported as one."""
    root = _receipts(tmp_path)
    _set_amount(root, "T1", "")
    _set_amount(root, "T2", "")
    _set_amount(root, "T3", "0")

    top = queries.run("calaccess.top_contributor", {"filer_id": MIXED}, root)
    assert not top.found and "TIE" not in top.detail and "tie" not in top.detail, top
    assert top.detail.startswith("Tess Marlow leads the stated amounts at $0")

    _set_amount(root, "T1", "300")   # Quill PAC now matches Tess Marlow's stated $300
    _set_amount(root, "T3", "300")
    top = queries.run("calaccess.top_contributor", {"filer_id": MIXED}, root)
    assert not top.found
    assert top.detail.startswith("a 2-way tie (Quill PAC | Tess Marlow) leads the stated "
                                 "amounts at $300")


def test_top_contributor_counts_unreadable_gifts_beyond_the_ranked_few(tmp_path):
    """Only four contributors are fetched to rank. The unreadable count is over every gift to
    the filer, not those four, and contributors with no readable gift never crowd out one
    with a stated amount."""
    root = _receipts(tmp_path)
    for i in range(6):
        _set(root, "INSERT INTO RCPT_CD (FILING_ID, AMEND_ID, TRAN_ID, LINE_ITEM, CTRIB_NAML,"
             " RCPT_DATE, AMOUNT, FORM_TYPE) VALUES ('8100001', '0', ?, ?, ?,"
             " '1/9/2026 12:00:00 AM', '', 'A')", f"U{i}", str(10 + i), f"Unstated Fund {i}")

    top = queries.run("calaccess.top_contributor", {"filer_id": MIXED}, root)
    assert not top.found, top
    assert top.detail == ("Quill PAC leads the stated amounts at $750 in schedule-A gifts, "
                          "but no largest contributor can be named; 6 more gift(s) to this "
                          "filer with no readable amount, not counted")

    # with every amount readable, the largest is named as before
    _set(root, "DELETE FROM RCPT_CD WHERE TRAN_ID LIKE 'U%'")
    top = queries.run("calaccess.top_contributor", {"filer_id": MIXED}, root)
    assert top.found and top.value == "Quill PAC"
    assert top.detail == "$750 across 2 schedule-A gift(s)"


def test_a_readable_gift_is_never_merged_into_an_unreadable_restatement(tmp_path):
    """The cross-form dedup keyed on CAST(AMOUNT AS REAL), which reads "300,000" as 300.0, so a
    row filed as "300,000" under the same transaction base collapsed into the $300 gift, and
    MAX() over the text kept "300,000". Read as unreadable, the stated $300 would have vanished
    with it. The dedup now keys on the amount as the sum reads it."""
    root = _receipts(tmp_path)
    # the same transaction base as T3's $300, the same contributor and date, filed as "300,000"
    _set(root, "INSERT INTO RCPT_CD (FILING_ID, AMEND_ID, TRAN_ID, LINE_ITEM, CTRIB_NAML,"
         " CTRIB_NAMF, RCPT_DATE, AMOUNT, FORM_TYPE) VALUES ('8100001', '0', 'F496P3-T3', '4',"
         " 'Marlow', 'Tess', '1/7/2026 12:00:00 AM', '300,000', 'F496P3')")

    # Only every schedule reaches the dedup: the schedule-A default leaves the Form 496 Part 3
    # row out before it (#66), and counts the $300 alone.
    tess = queries.run("calaccess.contributor_total",
                       {"filer_id": MIXED, "contributor": "Marlow", "contributor_first": "Tess",
                        "form_type": ""}, root)
    assert tess.found and tess.value == 300.0 and tess.rows == 1, tess
    assert "1 more gift(s) with no readable amount" in tess.note
    tess = queries.run("calaccess.contributor_total",
                       {"filer_id": MIXED, "contributor": "Marlow", "contributor_first": "Tess"},
                       root)
    assert tess.found and tess.value == 300.0 and tess.note == "1 itemized schedule-A gift(s)"

    listed = [c for c in calaccess.contributions_to(root, MIXED) if c.contributor == "Tess Marlow"]
    assert sorted((c.amount is None, c.amount_filed) for c in listed) == [
        (False, "300"), (True, "300,000")]


@pytest.mark.parametrize("unread", UNREADABLE)
def test_the_contributions_listing_never_prints_an_unreadable_amount_as_zero(
        tmp_path, capsys, unread):
    """The listing a researcher finds filings with printed a blank as "$0", which reads as a
    stated zero. A blank says so; anything else is shown as filed (escaped, so "[/]" is not
    rich markup). It sorts after every stated amount, and the top-N cut never drops it: it
    is the gift a query total names as not counted, and its filing is how to check it."""
    root = _receipts(tmp_path)
    _set_amount(root, "T2", unread)

    got = calaccess.contributions_to(root, MIXED)
    assert [c.amount for c in got] == [500.0, 300.0, None]
    assert got[-1].amount_filed == unread.strip()
    assert [c.amount for c in calaccess.contributions_to(root, MIXED, top=1)] == [500.0, None]
    # a `since` window still applies to it
    assert [c.amount for c in calaccess.contributions_to(root, MIXED, since="2026-01-07")] == [
        300.0]

    capsys.readouterr()
    cli.calaccess_contributions(MIXED, data=root)
    listed = next(line for line in capsys.readouterr().out.splitlines() if "2026-01-06" in line)
    assert listed.split()[0] == (unread.strip() or "blank")


def test_the_listing_bounds_and_sanitizes_what_it_shows_for_unreadable_amounts(tmp_path, capsys):
    """Unreadable gifts get their own --top slots, not unlimited ones, and the footer says when
    there may be more. What was filed is filer text: a control character in it would act on
    the terminal before anything is shown, so it is made visible, and it is cut to fit."""
    root = _receipts(tmp_path)
    _set_amount(root, "T1", "5\x1b[2J")          # an escape sequence that would clear the screen
    _set_amount(root, "T2", "x" * 40)

    got = calaccess.contributions_to(root, MIXED, top=1)
    assert [c.amount for c in got] == [300.0, None], "one ranked slot, one unreadable slot"

    def plain(out):   # rich's own styling and wrapping, not the text
        return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", out).split())

    capsys.readouterr()
    cli.calaccess_contributions(MIXED, data=root, top=1)
    out = plain(capsys.readouterr().out)
    assert "The last 1 have no readable amount" in out and "raise --top" in out

    cli.calaccess_contributions(MIXED, data=root, top=5)
    out = capsys.readouterr().out
    assert "\x1b[2J" not in out, "the filed escape sequence reached the terminal"
    shown = {plain(line).split()[0] for line in out.splitlines() if "2026-01-0" in line}
    assert shown == {"$300", "5\\x1b[2J", "x" * 14}, shown
    assert "The last 2 have no readable amount" in plain(out) and "raise --top" not in out
