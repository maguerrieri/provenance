"""Cross-claim disagreement detection.

For a voter guide, sources disagreeing on a date or a dollar figure is the most
valuable thing the pipeline can surface — so this flags rather than smooths.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .models import Claim

MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(k|thousand|m|million|b|billion)?", re.I)
YEAR = re.compile(r"\b(19|20)\d{2}\b")
DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d{2}\b"
    r"|\b(?:19|20)\d{2}-\d{2}-\d{2}\b", re.I)

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def money_values(text: str) -> set[float]:
    vals = set()
    for amt, unit in MONEY.findall(text):
        try:
            v = float(amt.replace(",", ""))
        except ValueError:
            continue
        vals.add(v * _MULT.get((unit or "").lower(), 1.0))
    return vals


def date_values(text: str) -> set[str]:
    return {d.lower().replace(".", "").replace(",", "") for d in DATE.findall(text)}


def year_values(text: str) -> set[str]:
    return {m.group(0) for m in YEAR.finditer(text)}


def detect(claims: list[Claim]) -> list[Claim]:
    """Flag four kinds of conflict:
    1. sources within one claim disagreeing with each other;
    2. the answer's figures/dates vs. those in its own snippets;
    3. two claims asserting near-miss amounts;
    4. a source a verifier judged to contradict its claim.

    (1) is the case a voter guide most needs surfaced — two outlets reporting different
    numbers for the same settlement is a finding, not noise to average away — and it is
    invisible to (2), which passes as long as the answer matches *one* of them. (4) is the
    same finding made by the judgment pass rather than by comparing figures.

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
                            f"({', '.join(f'${v:,.0f}' for v in sorted(vals_a))}) vs {pub_b} "
                            f"({', '.join(f'${v:,.0f}' for v in sorted(vals_b))})")
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
        a_money, s_money = money_values(c.answer), money_values(snip)
        if a_money and s_money and not (a_money & s_money):
            c.conflicts.append(
                f"dollar figure in the answer ({sorted(a_money)}) does not appear in any cited "
                f"snippet ({sorted(s_money)})")
        a_year, s_year = year_values(c.answer), year_values(snip)
        if a_year and s_year and not (a_year & s_year):
            c.conflicts.append(
                f"year in the answer ({sorted(a_year)}) does not appear in any cited snippet "
                f"({sorted(s_year)})")

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
                            f"near-miss dollar figures across claims: ${a:,.0f} vs ${b:,.0f} "
                            f"({', '.join(sorted(ids))}) — confirm which is right")
    return claims
