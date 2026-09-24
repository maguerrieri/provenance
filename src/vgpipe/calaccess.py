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
import warnings
import zipfile
from dataclasses import dataclass
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
    "RCPT_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "CTRIB_NAML", "CTRIB_NAMF",
                "CTRIB_EMP", "CTRIB_OCC", "RCPT_DATE", "AMOUNT", "FORM_TYPE", "CAND_NAML",
                "SUP_OPP_CD"],
    "EXPN_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "PAYEE_NAML", "PAYEE_NAMF",
                "EXPN_DATE", "AMOUNT", "EXPN_DSCR", "CAND_NAML", "SUP_OPP_CD"],
    "S496_CD": ["FILING_ID", "AMEND_ID", "TRAN_ID", "LINE_ITEM", "AMOUNT", "EXP_DATE",
                "EXPN_DSCR"],
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
    """What `build()` recorded about the export: export_date, export_date_from, built_at.

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
# undercount, so this keeps the rows. The measurements are in CLAUDE.md, under "CAL-ACCESS
# double-counts four ways, and all are silent".
LATEST_SQL = """
    CREATE {temp}VIEW IF NOT EXISTS {view} AS
    SELECT t.* FROM {table} t
    JOIN (SELECT FILING_ID, MAX(CAST(AMEND_ID AS INTEGER)) AS a
          FROM {table} GROUP BY FILING_ID) m
      ON m.FILING_ID = t.FILING_ID AND CAST(t.AMEND_ID AS INTEGER) = m.a
"""

LATEST_VIEWS = (("RCPT_CD", "RCPT_LATEST"), ("EXPN_CD", "EXPN_LATEST"),
                ("S496_CD", "S496_LATEST"))


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
    "amendment replaced. Rebuild the database: uv run vg calaccess build")


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

    with zipfile.ZipFile(zp) as zf:
        export_date, date_from = _export_date(zf, zp)
        members = {Path(n).stem.upper(): n for n in zf.namelist() if n.upper().endswith(".TSV")}
        for table in WANTED:
            member = members.get(table)
            if member is None:
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
    con.executemany(f'INSERT INTO "{EXPORT_META}" VALUES (?, ?)', [
        ("export_date", export_date), ("export_date_from", date_from),
        ("built_at", datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))])
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
    """Largest contributions received by a filer, on or after `since` if given (YYYY-MM-DD,
    YYYY-MM or YYYY)."""
    # Must use the same dedup as queries.contributor_total, or the listing and the citable
    # figure disagree — and they did. Filers restate one gift under several FILING_IDs while
    # keeping the TRAN_ID stable, so a raw listing showed one donor's single gift four times
    # while the query totalled it correctly. A listing that contradicts the verified number
    # is worse than no listing: it invites hand-summing the duplicates.
    from .queries import DEDUPED_RECEIPTS, date_window_sql, iso_date_sql, real_date_sql

    window, window_args = date_window_sql("CTRIB_DATE", since, "")   # refuses a bad `since`
    con = connect(root)
    inner = DEDUPED_RECEIPTS.format(extra="")
    # RCPT_DATE is "M/D/YYYY 12:00:00 AM" text: compared as-is, 10/14/2025 sorts before
    # 2025-01-01 and 3/1/2010 after it. Normalize and filter in SQL, BEFORE the LIMIT --
    # filtering afterwards spent the top-N slots on old gifts and then discarded them.
    where, args = "", [str(filer_id), str(filer_id)] + window_args
    if window:
        # A gift with no usable date cannot be placed either side of `since`, so it stays in,
        # with its date showing as it was filed: dropping it would be a missing donation.
        where = f"WHERE ({window}) OR NOT {real_date_sql('CTRIB_DATE')}"
    q = f"""
        SELECT * FROM (
            SELECT d.FILING_ID, ? AS FILER_ID, d.CTRIB_NAML, d.CTRIB_NAMF, d.CTRIB_EMP,
                   d.CTRIB_OCC, d.AMOUNT, d.AMT, {iso_date_sql("d.RCPT_DATE")} AS CTRIB_DATE,
                   TRIM(COALESCE(d.RCPT_DATE, '')) AS FILED_DATE,
                   d.FILINGS
            FROM ({inner}) d)
        {where}
        ORDER BY AMT DESC
        LIMIT ?
    """
    out = []
    for r in con.execute(q, args + [top]):
        name = " ".join(x for x in (r["CTRIB_NAMF"], r["CTRIB_NAML"]) if x).strip()
        # Read as the queries read it, so the listing and a citable total agree on which gifts
        # have an amount. A blank here was listed as $0, which reads as a stated zero; an
        # amount that did not read sorts after every stated one (NULL is last in DESC).
        out.append(Contribution(filing_id=str(r["FILING_ID"]),
                                filings=int(r["FILINGS"] or 1), filer_id=r["FILER_ID"],
                                contributor=name, employer=r["CTRIB_EMP"] or "",
                                occupation=r["CTRIB_OCC"] or "",
                                amount=None if r["AMT"] is None else float(r["AMT"]),
                                amount_filed=(r["AMOUNT"] or "").strip(),
                                date=shown_date(r["CTRIB_DATE"], r["FILED_DATE"])))
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

    if loose:
        match, name_arglist = "UPPER(TRIM(c.CAND_NAML)) LIKE UPPER(?)", [f"%{candidate_last}%"]
    else:
        match = name_match_sql("c.CAND_NAML", "c.CAND_NAMF", first)
        name_arglist = _nargs(candidate_last, first)
    first_clause = ""
    q = f"""
        SELECT s.FILING_ID, c.FILER_ID, c.FILER_NAML, c.CAND_NAML, c.CAND_NAMF,
               c.SUP_OPP_CD, s.AMOUNT, {iso_date_sql("s.EXP_DATE")} AS EXP_DATE,
               TRIM(COALESCE(s.EXP_DATE, '')) AS FILED_DATE, s.EXPN_DSCR
        FROM S496_LATEST s
        JOIN CVR_LATEST c ON c.FILING_ID = s.FILING_ID
        WHERE {match}{first_clause}
        ORDER BY CAST(s.AMOUNT AS REAL) DESC
        LIMIT ?
    """
    args = name_arglist + [top]
    rows = [dict(r) for r in con.execute(q, args)]
    con.close()
    for r in rows:
        # ISO, as contributions_to() gives it. Slicing the raw text printed "9/1/2026 1".
        r["EXP_DATE"] = shown_date(r["EXP_DATE"], r["FILED_DATE"])
        r["cite_url"] = filing_url(r["FILING_ID"])
        r["stance"] = {"S": "support", "O": "oppose"}.get((r.get("SUP_OPP_CD") or "").upper(),
                                                          r.get("SUP_OPP_CD") or "?")
    return rows
