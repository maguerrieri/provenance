"""CAL-ACCESS bulk export: the only programmatically reachable route to California
campaign finance.

Both UIs over this data are closed to us — powersearch.sos.ca.gov drops non-browser
connections, cal-access.sos.ca.gov sits behind bot protection — while the nightly export of
the same database is a plain public download. So donor and independent-expenditure questions
are answerable, but only through here.

**A row in this database is not a citation.** It is a local copy of a filing, with no URL a
human can open and no snippet they can Cmd-F. Use it to find *which* filing says a thing,
then cite that filing's own CAL-ACCESS page — `filing_url()` builds it. Every query result
carries the filing id for exactly that reason.
"""

from __future__ import annotations

import csv
import datetime
import functools
import re
import sqlite3
import sys
import uuid
import warnings
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

EXPORT_URL = "https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip"

# Only the tables the voter-guide questions need. The full export has ~80.
# Table -> the columns we actually query. RCPT_CD alone has 62 columns and tens of millions
# of rows; loading all of them produced a 7.1 GB database for data we never read. Selecting
# columns is the difference between a working local index and one nobody will keep on disk.
WANTED = {
    "FILERNAME_CD": ["FILER_ID", "NAML", "NAMF", "FILER_TYPE", "STATUS"],
    # AMEND_ID / TRAN_ID / LINE_ITEM are NOT optional. These tables hold every amendment of
    # every filing, so one contribution appeared 72 times in a single filing and every
    # total came out ~72x too high. Dropping these keys to save disk made the data unusable
    # while still looking plausible — real rows, real amounts, fabricated sums.
    # CTRIB_CITY and CTRIB_ZIP4 are for people, not figures: a query that flags names which
    # could be one giver's lists where each one's gifts came from (queries._identity).
    "RCPT_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "CTRIB_NAML", "CTRIB_NAMF",
                "CTRIB_EMP", "CTRIB_OCC", "CTRIB_CITY", "CTRIB_ZIP4", "RCPT_DATE", "AMOUNT",
                "FORM_TYPE", "CAND_NAML", "SUP_OPP_CD"],
    # FORM_TYPE tells the schedules apart, in EXPN_CD as in RCPT_CD, and without it nothing can
    # find a schedule a filing's latest amendment left out (unrestated_schedules()).
    "EXPN_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "PAYEE_NAML", "PAYEE_NAMF",
                "EXPN_DATE", "AMOUNT", "EXPN_DSCR", "CAND_NAML", "SUP_OPP_CD", "FORM_TYPE"],
    "S496_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "AMOUNT", "EXP_DATE",
                "EXPN_DSCR"],
    # Form 497 late contribution reports. Not counted in any total: a gift here is restated on
    # a later Form 460 schedule A, and until then the contribution queries refuse rather than
    # leave it out (queries._pending_late). FORM_TYPE tells Part 1 (received) from Part 2 (made).
    "S497_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "FORM_TYPE", "ENTY_NAML",
                "ENTY_NAMF", "CTRIB_DATE", "DATE_THRU", "AMOUNT"],
    # AMEND_ID here for the same reason as the fact tables: without it nothing dedupes the
    # cover records, and 61,088 filings carry duplicate rows. A clean fact table joined to a
    # multiplying cover table still double-counts — the amendment guard on S496/RCPT does not
    # help, because it is the JOIN that fans out.
    "CVR_CAMPAIGN_DISCLOSURE_CD": ["FILING_ID", "AMEND_ID", "FILER_ID", "FILER_NAML",
                                   "CAND_NAML", "CAND_NAMF", "SUP_OPP_CD", "FORM_TYPE",
                                   "ELECT_DATE", "FROM_DATE", "THRU_DATE"],
    "FILER_FILINGS_CD": ["FILER_ID", "FILING_ID", "FORM_ID", "FILING_DATE"],
}

# Schema notes, learned by reading the real export rather than assuming:
#   RCPT_CD has FILING_ID but NO FILER_ID, and its date column is RCPT_DATE. Getting from a
#   filer to their contributions means joining through a cover record.
#   S496_CD is amounts only — the candidate name and support/oppose live on
#   CVR_CAMPAIGN_DISCLOSURE_CD for the same FILING_ID.

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def filing_url(filing_id: str | int) -> str:
    """The page a human can open for a filing. This is what gets cited, not our row."""
    return f"https://cal-access.sos.ca.gov/PDFGen/pdfgen.prg?filingid={filing_id}&amendid=0"


def committee_url(filer_id: str | int) -> str:
    return ("https://cal-access.sos.ca.gov/Campaign/Committees/Detail.aspx"
            f"?id={filer_id}")


@dataclass
class Contribution:
    filing_id: str          # earliest filing in the restatement chain — what to cite
    filer_id: str
    contributor: str
    employer: str
    occupation: str
    amount: float | None    # None: no readable amount (queries.amount_sql), never $0
    date: str
    committee: str = ""
    filings: int = 1        # how many filings restated this one gift
    amount_filed: str = ""  # the AMOUNT text as filed, for showing one that did not read
    # This gift's filings whose latest amendment has no receipt rows (Unrestated); () when
    # checked and none, None when not checked -- the default, so a row nobody checked never
    # reads as settled.
    unrestated: tuple[Unrestated, ...] | None = None
    # The schedules this gift is on when a later amendment of its filing has no rows there, so
    # no figure counts it (UnrestatedSchedule); () when it is counted, None when not checked.
    omitted: tuple[UnrestatedSchedule, ...] | None = None

    @property
    def cite_url(self) -> str:
        return filing_url(self.filing_id)


# The phrases citable_snapshot() opens its not-citable notes with. Kept beside the notes they
# match: `vg calaccess cite` colours by them, and a new note missing here reads green.
_UNUSABLE = ("WRONG CYCLE", "no dollar figures", "could not be read")


def unusable(note: str) -> bool:
    """Whether a citable_snapshot() note says the snapshot must not be cited as it stands."""
    return any(m in note for m in _UNUSABLE)


def citable_snapshot(url: str, *, root: Path | None = None,
                     expect_year: str = "") -> tuple[str | None, str]:
    """A fetchable archived copy of a CAL-ACCESS page — checked, not just found.

    The live pages are bot-protected, so a direct citation fails verification, which pushes
    researchers onto third-party mirrors. Broad crawls did snapshot many CAL-ACCESS pages and
    a snapshot IS fetchable, so it can carry the official record.

    But finding *a* snapshot is not enough, and an earlier version of this function stopped
    there: asked about a 2026 committee it returned a 2023 landing page, which is the same
    wrong-document trap `superseded` exists to catch. So this reports what the snapshot
    actually is — its capture date, the election cycle it covers, and whether it carries
    dollar figures at all — and refuses to imply more than that.

    Returns (snapshot_url, note). Read the note; it is the whole point.
    """
    from .archive import existing_snapshot
    from .fetch import fetch, unreadable_reason

    snap = existing_snapshot(url)
    if not snap:
        return None, ("no archived copy exists, and Save Page Now cannot make one (the "
                      "crawler is blocked as we are) — cite the live URL for the human and "
                      "expect human_review, or cite a mirror WITH secondary_host_ack")

    captured = ""
    m = re.search(r"/web/(\d{4})(\d{2})", snap)
    if m:
        captured = f"{m.group(1)}-{m.group(2)}"

    detail = ""
    if root is not None:
        page = fetch(snap, root)
        # An unread snapshot is not a landing page: "no dollar figures" below would describe a
        # page nobody saw — and a Wayback error page has text, so an empty body is not the test.
        if why := unreadable_reason(page):
            return snap, (f"captured {captured}, but the snapshot could not be read ({why}), so "
                          "nothing is known about what it covers. Retry later or open it by "
                          "hand; do not cite it on the strength of this note.")
        text = page.text
        cycle = re.search(r"Election Cycle:\s*([\d]{4}\s*through\s*[\d]{4})", text)
        has_money = "$" in text
        detail = (f" Snapshot covers {cycle.group(1).strip()}." if cycle else "")
        if expect_year and cycle and expect_year not in cycle.group(1):
            return snap, (f"WRONG CYCLE: captured {captured}, covers {cycle.group(1).strip()},"
                          f" but the claim is about {expect_year}. Do not cite this."
                          " Try the same URL with &session=<year>.")
        if not has_money:
            return snap, (f"captured {captured}; this page carries no dollar figures at all"
                          f" (it is a landing page).{detail} Usable only to establish that the"
                          " committee exists and its id.")

    return snap, (f"captured {captured}.{detail} Committee pages carry period TOTALS, not"
                  " per-donor itemization — a per-donor amount cannot be cited from here."
                  " Use the export to find it, and cite this only for what it actually says.")


def db_path(root: Path) -> Path:
    return root / "cache" / "calaccess" / "calaccess.sqlite"


def zip_path(root: Path) -> Path:
    return root / "cache" / "calaccess" / "dbwebexport.zip"


# Which export the database was built from. The export is refreshed nightly, so a figure can
# change because new filings arrived, and nothing recorded which export a query citation was
# checked against — a mismatch could not be told apart from a query fix or a wrong citation.
EXPORT_META = "EXPORT_META"


def _export_date(zf: zipfile.ZipFile, zp: Path) -> tuple[str, str]:
    """(YYYY-MM-DD the export was produced, where that date came from).

    The newest timestamp among the export's own TSVs: it travels with the file, so it survives
    a copy or a re-download of the same export. A zip entry with no timestamp reads as
    1980-01-01, the format's zero, so a zip carrying only those falls back to the file's
    modification time, which is when it was downloaded — and says so.

    Zip timestamps are the producer's local time, so the fallback reads the modification time
    as local too: in UTC, a late-evening download dates to the next day, and two databases
    built from one export would carry different dates.
    """
    stamps = []
    for zi in zf.infolist():
        if zi.filename.upper().endswith(".TSV") and zi.date_time[0] > 1980:
            try:
                stamps.append(datetime.datetime(*zi.date_time))
            except ValueError:
                continue    # a malformed stamp (month 13, say) is no date; the others still are
    if stamps:
        return max(stamps).date().isoformat(), "export"
    return datetime.datetime.fromtimestamp(zp.stat().st_mtime).date().isoformat(), "download"


def export_info(root: Path) -> dict[str, str]:
    """What `build()` recorded about the export: export_date, export_date_from, built_at,
    not_in_export.

    Empty when there is no database, or one built before exports were dated. Opened read-only,
    so asking never creates a database where there was none. Remembered per file state, since
    every query run asks and a build re-asks for every query citation.
    """
    dbp = db_path(root)
    try:
        st = dbp.stat()
    except OSError:
        return {}
    return dict(_read_export_info(str(dbp.resolve()), st.st_mtime_ns, st.st_size))


@functools.lru_cache(maxsize=8)
def _read_export_info(path: str, _mtime_ns: int, _size: int) -> tuple[tuple[str, str], ...]:
    # Keyed on mtime and size as well as the path, so a rebuild mid-process is read afresh.
    try:
        con = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return ()
    try:
        return tuple(con.execute(f'SELECT "key", "value" FROM "{EXPORT_META}"').fetchall())
    except sqlite3.DatabaseError:
        # No table — built before exports were dated, or interrupted before build() wrote it
        # last — or not a database at all. Unknown either way: this only annotates, so it must
        # not stop `vg judge` or `vg build`.
        return ()
    finally:
        con.close()


# The dedup views. Every aggregate must go through them; querying a raw table sums each
# contribution once per amendment that restated it. Each view carries a {temp} slot: see
# install_views() for why connect() redefines them.

# One cover record per filing. Built from the latest amendment when AMEND_ID is present, and
# DISTINCT otherwise so an older database still behaves.
CVR_LATEST_SQL = """
    CREATE {temp}VIEW IF NOT EXISTS CVR_LATEST AS
    SELECT c.* FROM CVR_CAMPAIGN_DISCLOSURE_CD c
    JOIN (SELECT FILING_ID, MAX(CAST(AMEND_ID AS INTEGER)) a
          FROM CVR_CAMPAIGN_DISCLOSURE_CD GROUP BY FILING_ID) m
      ON m.FILING_ID = c.FILING_ID AND CAST(c.AMEND_ID AS INTEGER) = m.a
    GROUP BY c.FILING_ID
"""

# Column-agnostic on purpose: naming columns here breaks against any export or fixture that
# selected a different subset.
CVR_DISTINCT_SQL = """
    CREATE {temp}VIEW IF NOT EXISTS CVR_LATEST AS
    SELECT DISTINCT * FROM CVR_CAMPAIGN_DISCLOSURE_CD
"""

# FILER_FILINGS_CD carries one row per filing SEQUENCE, so a filing amended five times
# appears six times. Joining contributions to it therefore multiplies every amount by the
# amendment count -- which produced a dozen donors tied at one inflated total, each a single
# maximum gift multiplied by the amendment count. Join through this instead.
FILER_FILING_SQL = """
    CREATE {temp}VIEW IF NOT EXISTS FILER_FILING AS
    SELECT DISTINCT FILER_ID, FILING_ID FROM FILER_FILINGS_CD
"""

# The latest amendment of each FILING, not of each transaction. An amendment restates the
# whole filing, so a transaction missing from it was withdrawn or re-keyed. Keyed per
# (FILING_ID, TRAN_ID), the older amendment's copy survived and was counted: 387,606 receipt
# rows across 4,722 filings in the real export, and a Form 496 that zeroed one transaction
# and re-reported it under a new id counted both.
# A join, not a correlated subquery: that re-scanned the filing once per row, and took 13.7s
# on a 45,000-gift committee where this takes 1.4s.
# The maximum is taken within each table, not from the cover, and that is deliberate. An
# amendment with a cover and no rows in this table is sometimes a withdrawal and sometimes an
# amendment that did not restate the schedule ("A missing address was added"), and the export
# cannot tell them apart. The cover's maximum would turn the second kind into a silent
# undercount, so this keeps the rows, and `unrestated_filings()` flags every figure and
# listing row that counts one. The measurements are in CLAUDE.md, under "CAL-ACCESS
# double-counts four ways, and all are silent".
# The same ambiguity recurs one level down, and there this rule takes the other side. A table
# holds several schedules (FORM_TYPE), and an amendment with rows on one schedule and none on
# another is taken whole, so the other schedule's earlier rows are left out. Choosing the
# schedule's own latest amendment instead would count a withdrawn schedule, so the rule stays
# and `unrestated_schedules()` flags every figure that leaves such rows out.
LATEST_SQL = """
    CREATE {temp}VIEW IF NOT EXISTS {view} AS
    SELECT t.* FROM {table} t
    JOIN (SELECT FILING_ID, MAX(CAST(AMEND_ID AS INTEGER)) AS a
          FROM {table} GROUP BY FILING_ID) m
      ON m.FILING_ID = t.FILING_ID AND CAST(t.AMEND_ID AS INTEGER) = m.a
"""

LATEST_VIEWS = (("RCPT_CD", "RCPT_LATEST"), ("EXPN_CD", "EXPN_LATEST"),
                ("S496_CD", "S496_LATEST"), ("S497_CD", "S497_LATEST"))


class DegradedDatabaseWarning(UserWarning):
    """The database lacks a column a dedup view needs, so some results can be wrong until
    it is rebuilt with `vg calaccess build`."""


class DegradedDatabase(RuntimeError):
    """A citable query refused to answer from a degraded database. `vg verify` and `vg build`
    then record the citation as not reproduced, never as verified."""


def covers_by_amendment(con: sqlite3.Connection) -> bool:
    """Whether this database can narrow cover records to the latest amendment."""
    return "AMEND_ID" in {r[1] for r in con.execute(
        'PRAGMA main.table_info("CVR_CAMPAIGN_DISCLOSURE_CD")')}


COVER_FALLBACK = (
    "this CAL-ACCESS database has no CVR_CAMPAIGN_DISCLOSURE_CD.AMEND_ID (it was built before "
    "that column was loaded), so cover records cannot be narrowed to the latest amendment. "
    "An independent expenditure can be attributed to a candidate or stance that a later "
    "amendment replaced, and no figure can be checked for rows a later amendment dropped. "
    "Rebuild the database: uv run vg calaccess build")


def connect_citable(root: Path) -> sqlite3.Connection:
    """connect() for a citable query: refuses (DegradedDatabase) a database whose covers carry
    no amendment ids.

    Without them nothing can find a filing's latest amendment, so no figure can be checked
    for rows that amendment dropped (`unrestated_filings()`), and an independent expenditure
    can be credited to a candidate a later amendment replaced. A total that renders green is a
    finding, and a warning printed once to stderr still let one re-run and render green.
    """
    con = connect(root)
    if not covers_by_amendment(con):
        has_covers = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                                 "'CVR_CAMPAIGN_DISCLOSURE_CD'").fetchone()
        con.close()
        # No cover table at all is not the old-build case, and a rebuild from the same zip
        # would not fix it, so say which it is.
        raise DegradedDatabase(COVER_FALLBACK if has_covers else NO_COVERS)
    return con


NO_COVERS = (
    "this CAL-ACCESS database has no CVR_CAMPAIGN_DISCLOSURE_CD table, so no figure can be "
    "checked for rows a filing's later amendment dropped. The export it was built from lacked "
    "the cover table: download a complete export, then uv run vg calaccess build")


@dataclass(frozen=True)
class Unrestated:
    """A filing whose latest amendment, by its cover, has no rows in a fact table where an
    earlier amendment has some. LATEST_SQL keeps the earlier amendment's rows, and the export
    cannot say whether that is right: the later amendment withdrew them, or did not restate
    that schedule (a fixed address, a signature). Only the filing settles it.

    `amount` and `rows` are what a figure took from the filing; a listing leaves them 0.
    """
    filing_id: str
    rows_amend: int      # the latest amendment with rows in the table: the rows counted
    cover_amend: int     # the filing's latest amendment, which has none there
    amount: float = 0.0
    rows: int = 0
    does = "rests on"    # what a figure does with its rows (queries.share_text)

    @property
    def cite_url(self) -> str:
        return filing_url(self.filing_id)

    def describe(self) -> str:
        return (f"filing {self.filing_id}'s rows are from amendment {self.rows_amend}, and its "
                f"latest amendment ({self.cover_amend}) has none")


def unrestated_filings(con: sqlite3.Connection, table: str,
                       filing_ids) -> dict[str, Unrestated]:
    """The filings among `filing_ids` whose highest cover AMEND_ID is greater than the highest
    AMEND_ID `table` has for them, by filing id.

    Asked per filing, through the FILING_ID indexes, not over the whole table: only the
    filings a result touched matter. A filing with no cover at all has nothing to compare, so
    it is not reported. Needs covers_by_amendment(con); a caller without it says it cannot
    check (connect_citable() refuses).
    """
    ids = sorted({str(f) for f in filing_ids})
    out: dict[str, Unrestated] = {}
    for i in range(0, len(ids), 500):     # under every SQLite build's limit on parameters
        chunk = ids[i:i + 500]
        for r in con.execute(f"""
            SELECT t.FILING_ID fid, MAX(CAST(t.AMEND_ID AS INTEGER)) rows_a,
                   (SELECT MAX(CAST(c.AMEND_ID AS INTEGER)) FROM CVR_CAMPAIGN_DISCLOSURE_CD c
                    WHERE c.FILING_ID = t.FILING_ID) cover_a
            FROM "{table}" t WHERE t.FILING_ID IN ({", ".join("?" * len(chunk))})
            GROUP BY t.FILING_ID
        """, chunk):
            if r["cover_a"] is not None and r["cover_a"] > r["rows_a"]:
                out[str(r["fid"])] = Unrestated(str(r["fid"]), r["rows_a"], r["cover_a"])
    return out


@dataclass(frozen=True)
class Reattributed:
    """A filing whose Form 496 rows its own amendment's cover gave to the candidate and stance
    asked about, while no cover of the filing's latest amendment, which decides whose money a
    row is (CVR_LATEST), does: it names another candidate, the other stance or none. No total
    for the first candidate counts those rows, and the export cannot say which cover is right:
    the later amendment withdrew or moved them, or its cover was an update that dropped the
    candidate.

    `amount` and `rows` are what a total left out; a listing leaves them 0 and sets `own`.
    """
    filing_id: str
    own_amend: int       # the amendment the rows are from, whose cover named the candidate
    latest_amend: int    # the filing's latest amendment, whose cover decides
    latest: str          # who that cover names (cover_names()), "" for no candidate
    amount: float = 0.0
    rows: int = 0
    own: str = ""        # who the own amendment's cover names, for the listing's mark

    @property
    def cite_url(self) -> str:
        return filing_url(self.filing_id)

    def describe(self) -> str:
        return (f"filing {self.filing_id}'s rows are from amendment {self.own_amend}, whose "
                f"cover matches this total, and the cover of its latest amendment "
                f"({self.latest_amend}) names {self.latest or 'no candidate'}")

    def mark(self) -> str:
        """The listing's note on the row: both covers, since the row is listed under its own
        cover's candidate when the latest names someone else, and under the latest when only
        the stance moved."""
        return (f"a{self.own_amend}'s cover names {self.own}; a{self.latest_amend}'s names "
                f"{self.latest or 'no candidate'}")


def cover_names(last, first, stance) -> str:
    """Who and which stance a cover names, as Reattributed.latest has it: "" for no candidate.
    Quoted with repr, as ie_total's near matches are: the names are export text, and a control
    byte in one would otherwise reach a terminal or a claim file raw. A stance code is named
    only when it is exactly what ie_total's test matches: "S " fails that test, so calling it
    support would make a flagged cover read as the one asked for."""
    name = " ".join(x.strip() for x in (first, last) if x and x.strip())
    if not name:
        return ""
    side = {"S": "support", "O": "oppose"}.get((stance or "").upper())
    if side is None:
        side = f"stance {stance!r}" if (stance or "").strip() else "no stance"
    return f"{name!r} ({side})"


def latest_cover(con: sqlite3.Connection, filing_id: str) -> tuple[int, str]:
    """A filing's latest cover amendment and who its cover names (cover_names()), for
    Reattributed. An amendment has one cover record; rowid only breaks a tie, should one ever
    carry two, so the text is stable."""
    r = con.execute("""
        SELECT CAST(AMEND_ID AS INTEGER) a, CAND_NAML, CAND_NAMF, SUP_OPP_CD
        FROM CVR_CAMPAIGN_DISCLOSURE_CD WHERE FILING_ID = ?
        ORDER BY CAST(AMEND_ID AS INTEGER) DESC, rowid LIMIT 1""", [str(filing_id)]).fetchone()
    return r["a"], cover_names(r["CAND_NAML"], r["CAND_NAMF"], r["SUP_OPP_CD"])


def left_out_sql(asked) -> str:
    """SQL true when S496 row `s` is one the latest-cover rule keeps out of every total asking
    for `asked` (Reattributed): a cover of the row's own amendment matches, and no cover of the
    filing's latest amendment does. `asked(alias)` is the total's condition on a cover alias;
    it appears twice, so its arguments go in twice. Needs covers_by_amendment(con).

    EXISTS, not a join, so a second cover record of one amendment could never count the row
    twice. And through the FILING_ID index, not a join to CVR_LATEST, which rebuilt that view
    on every call: on a synthetic export of 1.5 million covers that took 5s, twice the total
    itself, where the check built on this takes 0.6s.

    These are exactly the rows the total leaves out while their own cover matched, because an
    amendment has one cover record, so CVR_LATEST's cover is the latest amendment's only one.
    In the export measured for #111, no Form 496 filing's latest amendment carried two.
    """
    return f"""(EXISTS (SELECT 1 FROM CVR_CAMPAIGN_DISCLOSURE_CD o
                        WHERE o.FILING_ID = s.FILING_ID
                          AND CAST(o.AMEND_ID AS INTEGER) = CAST(s.AMEND_ID AS INTEGER)
                          AND {asked("o")})
            AND NOT EXISTS (SELECT 1 FROM CVR_CAMPAIGN_DISCLOSURE_CD l
                            WHERE l.FILING_ID = s.FILING_ID
                              AND CAST(l.AMEND_ID AS INTEGER) = (
                                  SELECT MAX(CAST(m.AMEND_ID AS INTEGER))
                                  FROM CVR_CAMPAIGN_DISCLOSURE_CD m
                                  WHERE m.FILING_ID = s.FILING_ID)
                              AND {asked("l")}))"""


def unrestated_shares(con: sqlite3.Connection, table: str, counted,
                      gaps: dict[str, Unrestated] | None = None) -> list[Unrestated]:
    """What each unrestated filing accounts for in a figure, largest first.

    `counted` holds the figure's counted units as (filing ids, amount, rows): one per
    deduplicated gift, which can span filings, or one per filing. A unit counts toward every
    unrestated filing it touches, so two filings' shares can overlap. `gaps` is
    unrestated_filings() over those ids, when the caller already asked.
    """
    counted = [(set(ids), amount, n) for ids, amount, n in counted]
    if gaps is None:
        gaps = unrestated_filings(con, table, set().union(*(ids for ids, _, _ in counted)))
    amounts: dict[str, float] = {}
    rows: dict[str, int] = {}
    for ids, amount, n in counted:
        for f in ids & gaps.keys():
            amounts[f] = amounts.get(f, 0.0) + (amount or 0.0)
            rows[f] = rows.get(f, 0) + n
    return sorted((replace(gaps[f], amount=amounts[f], rows=rows[f])
                   for f in amounts), key=lambda u: (-u.amount, u.filing_id))


LATE_FALLBACK = (
    "this CAL-ACCESS database cannot read Form 497 late contribution reports (S497_CD): it was "
    "built before they were loaded, or the export's table lacks a column they need. So a "
    "contribution total cannot tell whether a late contribution is missing from it, whether "
    "or not it names schedule A. Rebuild the database: uv run vg calaccess build")

# What queries._pending_late reads from S497_CD. DATE_THRU is optional.
LATE_COLUMNS = ("FILING_ID", "AMEND_ID", "TRAN_ID", "FORM_TYPE", "ENTY_NAML", "ENTY_NAMF",
                "CTRIB_DATE", "AMOUNT")


def late_reports_loaded(con: sqlite3.Connection) -> bool:
    """Whether this database can say which Form 497 late contribution reports there are.

    Yes when S497_CD holds every column queries read (LATE_COLUMNS): without AMEND_ID its
    latest-amendment view is not even defined. Also yes when the export had no S497_CD.TSV at
    all, since then it has no late reports to miss, which `build()` records in EXPORT_META.
    Not when a present file was skipped (no header, none of the wanted columns): that is an
    unreadable table, not an empty one. And not for a database built before either record.
    """
    cols = {r[1] for r in con.execute('PRAGMA main.table_info("S497_CD")')}
    if cols:
        return set(LATE_COLUMNS) <= cols
    try:
        row = con.execute(f'SELECT "value" FROM main."{EXPORT_META}" WHERE "key" = ?',
                          ("not_in_export",)).fetchone()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and "S497_CD" in str(row[0]).split(",")


SCHEDULE_FALLBACK = (
    "this CAL-ACCESS database has no {table}.FORM_TYPE (it was built before that column was "
    "loaded), so its schedules cannot be told apart, and no figure from that table can be "
    "checked for a schedule a filing's later amendment left out. Rebuild the database: "
    "uv run vg calaccess build")


@dataclass(frozen=True)
class UnrestatedSchedule:
    """A schedule (FORM_TYPE) with rows in an earlier amendment of a filing, and none in the
    filing's latest amendment in the same table, which has rows on other schedules. LATEST_SQL
    takes that amendment whole, so it leaves this schedule's rows out, and the export cannot
    say whether that is right: the amendment withdrew them, or did not restate a schedule it
    did not change. Only the filing settles it.

    The other side of Unrestated, one level down: there a figure counts rows the cover's
    latest amendment lacks; here it leaves out rows the table's own latest amendment lacks.
    `amount` and `rows` are what a figure leaves out; a listing leaves them 0.
    """
    filing_id: str
    schedule: str        # its FORM_TYPE, trimmed and upper-cased
    rows_amend: int      # the schedule's latest amendment with rows: the rows left out
    table_amend: int     # the filing's latest amendment in the table, with none on it
    amount: float = 0.0
    rows: int = 0
    does = "leaves out"  # what a figure does with its rows (queries.share_text)

    @property
    def cite_url(self) -> str:
        return filing_url(self.filing_id)

    def describe(self) -> str:
        return (f"filing {self.filing_id}'s schedule {self.schedule or '(blank)'} rows are from "
                f"amendment {self.rows_amend}, and its later amendment {self.table_amend} has "
                f"rows on other schedules but none on that one")


def unrestated_schedules(con: sqlite3.Connection, table: str,
                         filing_ids) -> list[UnrestatedSchedule]:
    """The schedules in `table` that a filing among `filing_ids` has rows on in an earlier
    amendment and none on in its latest amendment there, by filing and schedule.

    Asked per filing, through the FILING_ID indexes, as unrestated_filings() is: first which
    filings have more than one amendment in the table, from the (FILING_ID, AMEND_ID) index
    alone, then the schedules of only those. A table that is not there has no rows to leave
    out. One without FORM_TYPE is refused (DegradedDatabase): an EXPN_CD built before that
    column was loaded cannot tell its schedules apart, and a check that cannot run must not
    read as settled.
    """
    cols = {r[1] for r in con.execute(f'PRAGMA main.table_info("{table}")')}
    if not cols:
        return []
    if "FORM_TYPE" not in cols:
        raise DegradedDatabase(SCHEDULE_FALLBACK.format(table=table))
    ids = sorted({str(f) for f in filing_ids})
    latest: dict[str, int] = {}
    for i in range(0, len(ids), 500):     # under every SQLite build's limit on parameters
        chunk = ids[i:i + 500]
        latest.update((str(r[0]), r[1]) for r in con.execute(f"""
            SELECT FILING_ID, MAX(CAST(AMEND_ID AS INTEGER)) FROM "{table}"
            WHERE FILING_ID IN ({", ".join("?" * len(chunk))}) GROUP BY FILING_ID
            HAVING MIN(CAST(AMEND_ID AS INTEGER)) < MAX(CAST(AMEND_ID AS INTEGER))
        """, chunk))
    out = []
    amended = sorted(latest)
    for i in range(0, len(amended), 500):
        chunk = amended[i:i + 500]
        for fid, schedule, a in con.execute(f"""
            SELECT FILING_ID, UPPER(TRIM(COALESCE(FORM_TYPE, ''))) s,
                   MAX(CAST(AMEND_ID AS INTEGER))
            FROM "{table}" WHERE FILING_ID IN ({", ".join("?" * len(chunk))})
            GROUP BY FILING_ID, s
        """, chunk):
            if a < latest[str(fid)]:
                out.append(UnrestatedSchedule(str(fid), schedule, a, latest[str(fid)]))
    return sorted(out, key=lambda u: (u.filing_id, u.schedule))


def stage_unrestated(con: sqlite3.Connection, table: str,
                     schedules: list[UnrestatedSchedule]) -> str:
    """Copy the rows `schedules` leave out of `table` into a TEMP table, and return its name.

    Each row carries `gap`, the index in `schedules` of the one it came from. A figure groups
    these rows with the rows it counts (`queries.left_out_gifts()`), so a gift a counted row
    also reports is in the figure either way, and only the rest are what it leaves out.
    A new table per call, so SQL built on an earlier one keeps reading its own rows and `gap`
    indexes. Commits nothing: the insert leaves the connection in a transaction, and the
    table goes when it closes.
    """
    name = f"{table}_UNRESTATED_{uuid.uuid4().hex}"
    con.execute(f'CREATE TEMP TABLE "{name}" AS SELECT *, 0 AS gap FROM main."{table}" WHERE 0')
    con.executemany(f"""
        INSERT INTO temp."{name}" SELECT t.*, ? FROM main."{table}" t
        WHERE t.FILING_ID = ? AND CAST(t.AMEND_ID AS INTEGER) = ?
          AND UPPER(TRIM(COALESCE(t.FORM_TYPE, ''))) = ?
    """, [(i, u.filing_id, u.rows_amend, u.schedule) for i, u in enumerate(schedules)])
    return name


def install_views(con: sqlite3.Connection, *, temp: bool = True) -> None:
    """Define the dedup views on this connection, for whichever tables exist.

    A view is written into the database when it is built, so a database built before a view
    was corrected keeps the old definition, and nothing says so -- the same trap
    EXTRACTOR_VERSION closes for cached pages. The real export database still carried the
    per-transaction amendment view after it was fixed here. So connect() installs these as
    TEMP views on every connection: SQLite resolves an unqualified name in `temp` before
    `main`, which makes the definitions in this file the ones every query runs, whatever the
    database was built with. TEMP also leaves a read-only connection read-only.

    That only reaches a view whose columns exist. A fix that needs a column the database
    never loaded needs a rebuild, and says so (DegradedDatabaseWarning) rather than degrading
    quietly.
    """
    kw = "TEMP " if temp else ""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    # Decide by inspecting each table: CREATE VIEW does not validate column names, so a view
    # referencing a missing AMEND_ID is created happily and then fails at query time.
    def cols(table: str) -> set[str]:
        return {r[1] for r in con.execute(f'PRAGMA main.table_info("{table}")')}

    for table, view in LATEST_VIEWS:
        # No AMEND_ID means no way to pick the latest amendment: leave the view undefined, so
        # a query fails on the missing view instead of summing every amendment.
        if table in tables and {"FILING_ID", "AMEND_ID"} <= cols(table):
            con.execute(LATEST_SQL.format(temp=kw, view=view, table=table))
    if "FILER_FILINGS_CD" in tables:
        con.execute(FILER_FILING_SQL.format(temp=kw))
    if "CVR_CAMPAIGN_DISCLOSURE_CD" in tables:
        if covers_by_amendment(con):
            con.execute(CVR_LATEST_SQL.format(temp=kw))
        else:
            # DISTINCT keeps every amendment's cover, and an amendment can change who the
            # filing is about: in the real export one moves a filing from supporting one
            # candidate to supporting another, and 597 Form 496 filings carry more than one
            # cover. Joined to all of them, the latest amendment's expenditures counted for
            # every candidate any amendment named, putting another candidate's money in a
            # candidate's all-years total. DISTINCT also collapses only byte-identical
            # duplicates. So ie_total, the one citable query over covers, refuses on this path
            # (DegradedDatabase); the listing keeps working, as a finding aid, under this
            # warning.
            warnings.warn(COVER_FALLBACK, DegradedDatabaseWarning, stacklevel=2)
            con.execute(CVR_DISTINCT_SQL.format(temp=kw))


def _rows(zf: zipfile.ZipFile, member: str):
    """Yield rows from one TSV inside the export.

    CAL-ACCESS TSVs are dirty — stray quotes, embedded tabs, rows with the wrong field
    count. Quoting is disabled (a lone " in a donor's name would otherwise swallow the rest
    of the file) and short/long rows are padded or trimmed rather than dropped: a skipped
    row here is a missing donation, which is exactly the kind of silent gap this project
    exists to avoid.
    """
    with zf.open(member) as fh:
        # Neutralize every CR/LF in the line, not just the trailing ones: these files carry
        # bare \r *inside* fields (addresses, expenditure descriptions), which makes csv raise
        # "new-line character seen in unquoted field" partway through a multi-million-row
        # table. Splitting on \n and blanking the rest keeps one physical line per record.
        text = (line.decode("latin-1").replace("\r", " ").replace("\n", " ") for line in fh)
        reader = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader, None)
        if not header:
            return
        width = len(header)
        yield header
        for row in reader:
            if len(row) < width:
                row = row + [""] * (width - len(row))
            elif len(row) > width:
                row = row[:width]
            yield row


def build(root: Path, *, progress=None) -> Path:
    """Load the wanted tables into SQLite. Idempotent: rebuilds from the zip each time."""
    zp = zip_path(root)
    if not zp.exists():
        raise FileNotFoundError(
            f"{zp} not found — download it first:\n  curl -L -o {zp} {EXPORT_URL}")
    dbp = db_path(root)
    dbp.parent.mkdir(parents=True, exist_ok=True)
    if dbp.exists():
        dbp.unlink()
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    absent = []     # wanted tables the export does not contain at all
    with zipfile.ZipFile(zp) as zf:
        export_date, date_from = _export_date(zf, zp)
        members = {Path(n).stem.upper(): n for n in zf.namelist() if n.upper().endswith(".TSV")}
        for table in WANTED:
            member = members.get(table)
            if member is None:
                absent.append(table)
                if progress:
                    progress(table, 0, "not in export")
                continue
            it = _rows(zf, member)
            header = next(it, None)
            if not header:
                continue
            all_cols = [c.strip().replace(" ", "_") or f"c{i}" for i, c in enumerate(header)]
            wanted = WANTED[table]
            idx = [all_cols.index(c) for c in wanted if c in all_cols]
            cols = [all_cols[i] for i in idx]
            if not cols:
                if progress:
                    progress(table, 0, "no wanted columns present")
                continue
            coldefs = ", ".join('"%s" TEXT' % c for c in cols)
            con.execute('CREATE TABLE "%s" (%s)' % (table, coldefs))
            ins = f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(cols))})'
            n = 0
            batch = []
            for row in it:
                batch.append([row[i] for i in idx])
                if len(batch) >= 20000:
                    con.executemany(ins, batch)
                    n += len(batch)
                    batch = []
            if batch:
                con.executemany(ins, batch)
                n += len(batch)
            con.commit()
            if progress:
                progress(table, n, "")

    for table, _ in LATEST_VIEWS:
        try:
            con.execute(f'CREATE INDEX "ix_{table}_dedupe" ON "{table}" '
                        f'("FILING_ID", "AMEND_ID")')
        except sqlite3.OperationalError:
            pass
    # Persisted too, for the sqlite3 shell -- but only as current as this build. The pipeline
    # never reads these copies; connect() shadows them (see install_views).
    install_views(con, temp=False)
    con.commit()

    for tbl, col in (("RCPT_CD", "FILING_ID"), ("EXPN_CD", "FILING_ID"),
                     ("FILERNAME_CD", "FILER_ID"), ("S496_CD", "FILING_ID"),
                     ("CVR_CAMPAIGN_DISCLOSURE_CD", "FILER_ID"),
                     ("CVR_CAMPAIGN_DISCLOSURE_CD", "FILING_ID"),
                     ("FILER_FILINGS_CD", "FILER_ID")):
        try:
            con.execute(f'CREATE INDEX "ix_{tbl}_{col}" ON "{tbl}" ("{col}")')
        except sqlite3.OperationalError:
            pass
    # Last, so it certifies a finished build. Written first, it was committed with the first
    # table, and an interrupted rebuild answered queries from missing tables under a full
    # export date. Without it the database reads as undated, which `vg judge` and the review
    # page both say.
    con.execute(f'CREATE TABLE "{EXPORT_META}" ("key" TEXT PRIMARY KEY, "value" TEXT)')
    # not_in_export: wanted tables the export has no file for (late_reports_loaded).
    con.executemany(f'INSERT INTO "{EXPORT_META}" VALUES (?, ?)', [
        ("export_date", export_date), ("export_date_from", date_from),
        ("built_at", datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")),
        ("not_in_export", ",".join(absent))])
    con.commit()
    con.close()
    return dbp


def connect(root: Path) -> sqlite3.Connection:
    dbp = db_path(root)
    if not dbp.exists():
        raise FileNotFoundError(f"{dbp} not found — run `vg calaccess build` first")
    con = sqlite3.connect(dbp)
    con.row_factory = sqlite3.Row
    install_views(con)
    # A name's key, as the queries group, match and dedup names (queries._name_key).
    from .queries import _name_key

    con.create_function("vg_name_key", 1, _name_key, deterministic=True)
    return con


def find_filers(root: Path, name: str, limit: int = 25) -> list[dict]:
    """Filer ids matching a name. Names are messy and repeat across filings, so this
    collapses to distinct (id, name) pairs."""
    con = connect(root)
    q = """
        SELECT DISTINCT FILER_ID AS filer_id, NAML AS last, NAMF AS first
        FROM FILERNAME_CD
        WHERE UPPER(NAML) LIKE UPPER(?) OR UPPER(NAMF || ' ' || NAML) LIKE UPPER(?)
        LIMIT ?
    """
    like = f"%{name}%"
    rows = [dict(r) for r in con.execute(q, (like, like, limit))]
    con.close()
    return rows


# ASCII digits, not \d: that matches any Unicode digit, and "２０２５" sorts after every ASCII
# date, so as a bound it silently widened or emptied the window.
ISO_DATE = re.compile(r"[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?")
ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def check_date(value: str | None, name: str) -> str:
    """`value` if it is empty or a real ISO date (YYYY-MM-DD, YYYY-MM or YYYY), else
    ValueError.

    Compared against normalized dates, "10/14/2025" filters by string order and returns a
    plausible answer built from the wrong rows, so it is refused rather than used. So is a
    date of the right shape that does not exist: until="2025-00" quietly ends the window at
    2024, and "2025-02-31" is no day at all.
    """
    value = value or ""
    if value:
        try:
            if not ISO_DATE.fullmatch(value):
                raise ValueError
            # "2025" -> 2025-01-01, "2025-13" -> 2025-13-01, which raises
            datetime.date.fromisoformat(value + "-01" * (2 - value.count("-")))
        except ValueError:
            raise ValueError(f"{name} must be a date as YYYY-MM-DD, YYYY-MM or YYYY, "
                             f"not {value!r}") from None
    return value


def shown_date(iso: str | None, filed: str) -> str:
    """A listing's date: ISO when `iso_date_sql()` made a real date of it, else as it was
    filed. "09-05-24" from "5/24/09" would pass for a date, and so would "2025-14-10" from a
    day-first "14/10/2025"; the filed text does not pretend to be one. The same test as
    `queries.real_date_sql()`, so a listing and a window agree on which dates are readable.
    """
    try:
        if ISO_DAY.fullmatch(iso or ""):
            datetime.date.fromisoformat(iso)
            return iso
    except ValueError:
        pass
    return filed


def contributions_to(root: Path, filer_id: str, *, top: int = 25,
                     since: str = "") -> list[Contribution]:
    """The `top` largest contributions received by a filer, on or after `since` if given
    (YYYY-MM-DD, YYYY-MM or YYYY), then up to `top` more with no readable amount."""
    # Must use the same dedup as queries.contributor_total, or the listing and the citable
    # figure disagree — and they did. Filers restate one gift under several FILING_IDs while
    # keeping the TRAN_ID stable, so a raw listing showed one donor's single gift four times
    # while the query totalled it correctly. A listing that contradicts the verified number
    # is worse than no listing: it invites hand-summing the duplicates.
    from .queries import (DEDUPED_RECEIPTS, date_window_sql, iso_date_sql, left_out_gifts,
                          real_date_sql)

    window, window_args = date_window_sql("CTRIB_DATE", since, "")   # refuses a bad `since`
    con = connect(root)
    # A gift on a schedule a later amendment left out is in no figure. Listed anyway, and
    # marked: a finding aid that hid it would hide the filing to open. A database that cannot
    # check still lists, and its rows read as not checked (None), as for `unrestated`.
    try:
        left_out, with_left_out = left_out_gifts(con, str(filer_id), extra="")
    except DegradedDatabase:
        left_out = None
    listed = """
            SELECT d.FILING_ID, ? AS FILER_ID, d.CTRIB_NAML, d.CTRIB_NAMF, d.CTRIB_EMP,
                   d.CTRIB_OCC, d.AMOUNT, d.AMT, {date} AS CTRIB_DATE,
                   TRIM(COALESCE(d.RCPT_DATE, '')) AS FILED_DATE,
                   d.FILINGS, d.FILING_IDS, {marks}
            FROM ({inner}) d"""
    date = iso_date_sql("d.RCPT_DATE")
    rows = listed.format(date=date, marks="0 AS OMITTED, NULL AS GAPS",
                         inner=DEDUPED_RECEIPTS.format(extra=""))
    args = [str(filer_id)] * 2
    if left_out:
        # Their own arm, grouped with the counted rows so a gift a counted row also reports is
        # not listed twice. Counted gifts come from the arm above, exactly as the figures count
        # them: here a left-out filing could become the one a counted gift cites.
        rows += " UNION ALL " + listed.format(date=date, marks="d.OMITTED, d.GAPS",
                                              inner=with_left_out) + " WHERE d.OMITTED"
        args += [str(filer_id)] * 3
    # RCPT_DATE is "M/D/YYYY 12:00:00 AM" text: compared as-is, 10/14/2025 sorts before
    # 2025-01-01 and 3/1/2010 after it. Normalize and filter in SQL, BEFORE the LIMIT --
    # filtering afterwards spent the top-N slots on old gifts and then discarded them.
    where = ""
    if window:
        # A gift with no usable date cannot be placed either side of `since`, so it stays in,
        # with its date showing as it was filed: dropping it would be a missing donation.
        where = f"WHERE ({window}) OR NOT {real_date_sql('CTRIB_DATE')}"
    # Amounts are read as the queries read them (AMT), so the listing and a citable total agree
    # on which gifts have one. A gift with none cannot be ranked, but it is exactly what a
    # total names as "not counted", so it gets its own `top` slots after the ranked ones,
    # newest first, rather than being cut by their LIMIT: the researcher needs its filing to
    # check it by hand. NULL sorts last in DESC.
    q = f"""
        WITH g AS (SELECT * FROM ({rows}) {where})
        SELECT * FROM (SELECT * FROM g WHERE AMT IS NOT NULL ORDER BY AMT DESC LIMIT ?)
        UNION ALL
        SELECT * FROM (SELECT * FROM g WHERE AMT IS NULL ORDER BY CTRIB_DATE DESC LIMIT ?)
        ORDER BY AMT DESC, CTRIB_DATE DESC
    """
    found = con.execute(q, args + window_args + [top, top]).fetchall()
    ids = [set((r["FILING_IDS"] or "").split(",")) - {""} for r in found]
    # Marked, and still listed: a finding aid that hid the row would hide the filing to open.
    gaps = (unrestated_filings(con, "RCPT_CD", set().union(*ids))
            if covers_by_amendment(con) else None)
    out = []
    for r, filings in zip(found, ids):
        name = " ".join(x for x in (r["CTRIB_NAMF"], r["CTRIB_NAML"]) if x).strip()
        omitted = (None if left_out is None else () if not r["OMITTED"] else
                   tuple(left_out[int(g)] for g in sorted(r["GAPS"].split(","), key=int)))
        # A blank here was listed as $0, which reads as a stated zero.
        out.append(Contribution(filing_id=str(r["FILING_ID"]),
                                filings=int(r["FILINGS"] or 1), filer_id=r["FILER_ID"],
                                contributor=name, employer=r["CTRIB_EMP"] or "",
                                occupation=r["CTRIB_OCC"] or "",
                                amount=None if r["AMT"] is None else float(r["AMT"]),
                                amount_filed=(r["AMOUNT"] or "").strip(),
                                date=shown_date(r["CTRIB_DATE"], r["FILED_DATE"]),
                                # a left-out gift is from an earlier amendment than the rows
                                # Unrestated describes; its own mark sends a person to it
                                unrestated=None if gaps is None else () if omitted else tuple(
                                    gaps[f] for f in sorted(filings) if f in gaps),
                                omitted=omitted))
    con.close()
    return out


def independent_expenditures(root: Path, candidate_last: str, *, first: str = "",
                             top: int = 50, loose: bool = False) -> list[dict]:
    """Late independent expenditures naming a candidate, with support/oppose.

    Matches the surname EXACTLY by default. A substring match on a two-letter surname pulled
    in committees supporting a different candidate whose name contains it, and reported a
    multimillion-dollar expenditure as the candidate's largest backer. That is a fabricated
    headline finding, produced from real rows by a sloppy query. Pass `loose=True` only when
    you intend a substring search and will check each hit.
    """
    con = connect(root)
    # S496_CD is amounts only; who spent it and for/against whom is on the cover record.
    # Match every spelling a filer might use. Requiring (last, first) separately hid every
    # expenditure from a committee that put the candidate's whole name in the last-name field.
    from .queries import iso_date_sql, name_match_sql
    from .queries import name_args as _nargs

    def named(cover: str) -> str:
        if loose:
            return f"UPPER(TRIM({cover}.CAND_NAML)) LIKE UPPER(?)"
        return name_match_sql(f"{cover}.CAND_NAML", f"{cover}.CAND_NAMF", first)

    name_arglist = [f"%{candidate_last}%"] if loose else _nargs(candidate_last, first)
    checkable = covers_by_amendment(con)

    def exp(s: str) -> str:
        return f"""{s}.AMOUNT, {iso_date_sql(f"{s}.EXP_DATE")} AS EXP_DATE,
                   TRIM(COALESCE({s}.EXP_DATE, '')) AS FILED_DATE, {s}.EXPN_DSCR,
                   CAST({s}.AMEND_ID AS INTEGER) AS OWN_AMEND"""

    # Also every row whose own amendment's cover names this candidate while no cover of the
    # latest amendment, which attribution goes by, does (Reattributed): no total for this
    # candidate counts it, and hiding it here would hide the filing to open. Its cover columns
    # are filled in below, from its own cover. Needs cover amendment ids. MATERIALIZED for
    # the reason ie_total gives: the correlated tests first, the date work after.
    left_out, reattributed = (f"""
        WITH left_out AS MATERIALIZED (
            SELECT s.* FROM S496_LATEST s WHERE {left_out_sql(named)})""", f"""
        UNION ALL
        SELECT l.FILING_ID, NULL, NULL, NULL, NULL, NULL, {exp("l")}, 1
        FROM left_out l""") if checkable else ("", "")
    q = f"""{left_out}
        SELECT * FROM (
            SELECT s.FILING_ID, c.FILER_ID, c.FILER_NAML, c.CAND_NAML, c.CAND_NAMF,
                   c.SUP_OPP_CD, {exp("s")}, 0 AS LEFT_OUT
            FROM S496_LATEST s
            JOIN CVR_LATEST c ON c.FILING_ID = s.FILING_ID
            WHERE {named("c")}
            {reattributed})
        ORDER BY CAST(AMOUNT AS REAL) DESC
        LIMIT ?
    """
    args = name_arglist * (3 if checkable else 1) + [top]
    rows = [dict(r) for r in con.execute(q, args)]
    # Marked, and still listed: a finding aid that hid the row would hide the filing to open.
    gaps = (unrestated_filings(con, "S496_CD", {r["FILING_ID"] for r in rows})
            if checkable else None)
    for r in rows:
        r["reattributed"] = None
        fid = str(r["FILING_ID"])
        # A row listed under its latest cover can still be one ie_total leaves out: its own
        # cover named this candidate with the other stance. Only a filing whose latest
        # amendment has no rows can differ, so only those are asked.
        if gaps is None or not (r["LEFT_OUT"] or fid in gaps):
            continue
        # LIMIT 1: an amendment has one cover record, and should one ever carry two, this
        # takes one that names this candidate.
        own = con.execute(f"""
            SELECT o.FILER_ID, o.FILER_NAML, o.CAND_NAML, o.CAND_NAMF, o.SUP_OPP_CD
            FROM CVR_CAMPAIGN_DISCLOSURE_CD o
            WHERE o.FILING_ID = ? AND CAST(o.AMEND_ID AS INTEGER) = ? AND {named("o")}
            ORDER BY o.rowid LIMIT 1""", [r["FILING_ID"], r["OWN_AMEND"]] + name_arglist
        ).fetchone()
        # A flip only where a total could ask for the own cover's stance: ie_total asks for
        # one code, case aside (UPPER(SUP_OPP_CD) = UPPER(?)), or for none. A blank or longer
        # code is no stance a total asks for, so none leaves the row out on its account, and a
        # mark would name a filing no flag holds open.
        code = (own["SUP_OPP_CD"] or "").upper() if own is not None else ""
        counted = (own is None or len(code) != 1 or code.isspace()
                   or code == (r["SUP_OPP_CD"] or "").upper())
        if not r["LEFT_OUT"] and counted:
            continue   # its own cover named someone else, or no other stance: counted as listed
        if r["LEFT_OUT"]:
            # listed as its own cover has it; a stance flip stays under the latest, which a
            # total of that stance counts
            r.update(dict(own))
        r["reattributed"] = Reattributed(
            fid, r["OWN_AMEND"], *latest_cover(con, fid),
            own=cover_names(own["CAND_NAML"], own["CAND_NAMF"], own["SUP_OPP_CD"]))
    con.close()
    for r in rows:
        # None when the database cannot check, as for Contribution
        fid = str(r["FILING_ID"])
        r["unrestated"] = None if gaps is None else tuple([gaps[fid]] if fid in gaps else [])
        # ISO, as contributions_to() gives it. Slicing the raw text printed "9/1/2026 1".
        r["EXP_DATE"] = shown_date(r["EXP_DATE"], r["FILED_DATE"])
        r["cite_url"] = filing_url(r["FILING_ID"])
        r["stance"] = {"S": "support", "O": "oppose"}.get((r.get("SUP_OPP_CD") or "").upper(),
                                                          r.get("SUP_OPP_CD") or "?")
    return rows
