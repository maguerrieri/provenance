"""Cross-claim disagreement detection.

For a voter guide, sources disagreeing on a date or a dollar figure is the most
valuable thing the pipeline can surface — so this flags rather than smooths.
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from .models import Claim

# A unit after a dollar figure is one of these forms as a whole word, and nothing else. Read
# as the first letter of the next word, "$5,000 more" was five billion dollars and "$20 by"
# twenty billion. Every form is listed, since that loose read caught the abbreviations by
# accident, and a form missing here reads as a bare figure. A hyphen may follow ("$1.5M-2M",
# "$7M-a-year"); the cost is "$500 K-12", read as thousands.
_MULT = {**dict.fromkeys(("k", "thousand", "thousands"), 10**3),
         **dict.fromkeys(("m", "mm", "mn", "mln", "mil", "million", "millions"), 10**6),
         **dict.fromkeys(("b", "bn", "bln", "bil", "billion", "billions"), 10**9)}
MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:(" + "|".join(_MULT) + r")(?!\w))?", re.I)
YEAR = re.compile(r"\b(19|20)\d{2}\b")
DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d{2}\b"
    r"|\b(?:19|20)\d{2}-\d{2}-\d{2}\b", re.I)


def money_values(text: str) -> set[float]:
    vals = set()
    for amt, unit in MONEY.findall(text):
        # Scaled in decimal, not binary: 8.2 * 1e6 is 8199999.999999999 as floats, so
        # "$8.2 million" and "$8,200,000" were two different figures.
        try:
            v = Decimal(amt.replace(",", "")) * _MULT.get((unit or "").lower(), 1)
        except ArithmeticError:   # a bare "$," leaves nothing to parse
            continue
        vals.add(float(v))
    return vals


def money(v: float) -> str:
    """A dollar figure as a reader should see it: whole dollars from $10 up, cents below $10
    and on any fractional amount. Whole dollars everywhere printed $0.40 and $0.25 both as
    "$0", so a disagreement read as "$0 vs $0". An amount finer than a cent keeps every place
    it has, or cents would do the same to $1.1045 and $1.1012, written out in full: a float's
    own format prints $0.00001 as "$1e-05", which no one can find on the page."""
    if v >= 10 and v.is_integer():
        return f"${v:,.0f}"
    cents = f"{v:,.2f}"
    return f"${cents}" if float(cents.replace(",", "")) == v else f"${Decimal(repr(v)):,f}"


def _amounts(vals: set[float]) -> str:
    return ", ".join(money(v) for v in sorted(vals))


def date_values(text: str) -> set[str]:
    return {d.lower().replace(".", "").replace(",", "") for d in DATE.findall(text)}


def year_values(text: str) -> set[str]:
    return {m.group(0) for m in YEAR.finditer(text)}


def unsourced_figures(c: Claim) -> list[str]:
    """Conflict kind (2): the answer states a dollar figure or a year, its snippets state some,
    and none of the answer's is among them. The answer asserts what none of its evidence says.

    `Claim.status` sends such a claim to review, and `detect()` lists it, both from this one
    function, so the status and the conflicts section cannot disagree about it. Why this kind
    gates and (1) does not: CLAUDE.md, "The judgment pass is not advisory"."""
    snip = " ".join(s.snippet for s in c.sources)
    found = []
    a_money, s_money = money_values(c.answer), money_values(snip)
    if a_money and s_money and not (a_money & s_money):
        found.append(f"dollar figure in the answer ({_amounts(a_money)}) does not appear in any "
                     f"cited snippet ({_amounts(s_money)})")
    a_year, s_year = year_values(c.answer), year_values(snip)
    if a_year and s_year and not (a_year & s_year):
        found.append(f"year in the answer ({', '.join(sorted(a_year))}) does not appear in any "
                     f"cited snippet ({', '.join(sorted(s_year))})")
    return found


def detect(claims: list[Claim]) -> list[Claim]:
    """Flag four kinds of conflict:
    1. sources within one claim disagreeing with each other;
    2. the answer's figures/dates vs. those in its own snippets;
    3. two claims asserting near-miss amounts;
    4. a source a verifier judged to contradict its claim, still cited or since dropped.

    (1) is the case a voter guide most needs surfaced — two outlets reporting different
    numbers for the same settlement is a finding, not noise to average away — and it is
    invisible to (2), which passes as long as the answer matches *one* of them. (4) is the
    same finding made by the judgment pass rather than by comparing figures.

    (2) and (4) also send the claim to review (`Claim.status`); (1) and (3) are flags for the
    reviewer. Why the line falls there: CLAUDE.md, "The judgment pass is not advisory".

    (4) reads the support verdicts, so run this after they are settled (`cli._settle()` does):
    on a claim file as loaded, `support` is whatever the file says, recorded or not.
    """
    for c in claims:
        c.conflicts = []
        # (4) Before the snippet test below, which skips a claim whose snippets are all empty:
        # a verdict is listed whatever the snippets hold.
        for s in c.sources:
            if s.contradicts:
                who = f"{s.publisher.strip()} ({s.url})" if s.publisher.strip() else s.url
                note = s.verification.support_note
                c.conflicts.append(f"a verifier judged {who} contradicts the claim"
                                   + (f": {note}" if note else ""))
        # A verdict records no URL, so a source no longer cited is named by its id: the URL or
        # the quote changed, and which one is not recorded. What clears it goes beside it: the
        # reviewer is who decides.
        for d in c.dropped_contradictions:
            when = f", judged {d.judged_at}" if d.judged_at else ""
            c.conflicts.append(
                f"a verifier judged a source this claim no longer cites as it did (source id "
                f"{d.sid}{when}) contradicts the claim" + (f": {d.note}" if d.note else "")
                + f". Changing the citation did not resolve that: the claim stays in review "
                f"until the source is cited again, or a human clears it at a terminal with `provenance "
                f"clear-contradiction {c.question_id} {d.sid}`")
        snip = " ".join(s.snippet for s in c.sources)
        if not snip:
            continue
        # (1) sources contradicting each other
        by_source_money = [(s.publisher, money_values(s.snippet)) for s in c.sources]
        cited = [(pub, vals) for pub, vals in by_source_money if vals]
        if len(cited) > 1:
            for i, (pub_a, vals_a) in enumerate(cited):
                for pub_b, vals_b in cited[i + 1:]:
                    if not (vals_a & vals_b):
                        c.conflicts.append(
                            f"sources disagree on a dollar figure: {pub_a} "
                            f"({_amounts(vals_a)}) vs {pub_b} ({_amounts(vals_b)})")
        by_source_year = [(s.publisher, year_values(s.snippet)) for s in c.sources]
        cited_y = [(pub, vals) for pub, vals in by_source_year if vals]
        if len(cited_y) > 1:
            for i, (pub_a, vals_a) in enumerate(cited_y):
                for pub_b, vals_b in cited_y[i + 1:]:
                    if not (vals_a & vals_b):
                        c.conflicts.append(
                            f"sources disagree on a year: {pub_a} ({', '.join(sorted(vals_a))}) "
                            f"vs {pub_b} ({', '.join(sorted(vals_b))})")

        # (2) the answer against its own citations
        c.conflicts += unsourced_figures(c)

    by_money: dict[float, set[str]] = defaultdict(set)
    for c in claims:
        for v in money_values(c.answer):
            by_money[v].add(c.question_id)
    # Near-miss amounts across claims (e.g. $120K vs $102K) are worth a human look.
    amounts = sorted(by_money)
    for i, a in enumerate(amounts):
        for b in amounts[i + 1:]:
            if a and 1.0 < b / a < 1.15:
                ids = by_money[a] | by_money[b]
                for c in claims:
                    if c.question_id in ids:
                        c.conflicts.append(
                            f"near-miss dollar figures across claims: {money(a)} vs {money(b)} "
                            f"({', '.join(sorted(ids))}) — confirm which is right")
    return claims
