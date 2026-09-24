"""How a conflict reads a dollar figure out of text, and how it prints one.

A misread figure or a misleading print makes two numbers that agree look like a disagreement,
or two that disagree look alike.
"""

from __future__ import annotations

from vgpipe.conflicts import detect, money_values
from vgpipe.models import Claim, Source
from vgpipe.verify import check_corroboration

LEDGER = "https://daily-ledger.example/pier-settlement"
WEEKLY = "https://harbor-weekly.example/pier-settlement"
SNIPPET = "approved a pier settlement of $120,000 in 2019"


def _source(url=LEDGER, publisher="Daily Ledger", snippet=SNIPPET, status="verified",
            support="supports"):
    s = Source(url=url, publisher=publisher, author="R. Writer", date="2026-05-14",
               source_type="bylined_journalism", snippet=snippet)
    s.verification.status = status
    s.verification.support = support
    return s


def _claim(answer, *sources, qid="q1", **kw):
    return check_corroboration(Claim(question_id=qid, question="?", answer=answer,
                                     sources=list(sources or [_source()]), **kw))


# --- how a figure reads ---


def test_a_scaled_figure_equals_the_same_figure_written_out():
    """8.2 * 1e6 is 8199999.999999999 as floats, so "$8.2 million" and "$8,200,000" were two
    figures: a disagreement between sources, a near miss across claims, and an answer whose
    figure no snippet carried, all over one number."""
    assert money_values("$8.2 million") == money_values("$8,200,000") == {8_200_000.0}
    assert money_values("$1.005k and $1.15 million") == {1_005.0, 1_150_000.0}
    assert money_values("$, and $,,") == set()

    written = _source(snippet="approved a pier settlement of $8,200,000 in 2019")
    scaled = _source(url=WEEKLY, publisher="Harbor Weekly",
                     snippet="the $8.2 million pier settlement from 2019")
    [c] = detect([_claim("The council paid $8.2 million in 2019.", written, scaled)])
    assert c.conflicts == []
    assert c.status == "verified"


def test_a_unit_is_a_whole_word():
    """The unit used to be read off the next word's first letter, so "$5,000 more" was five
    billion dollars. An answer phrased that way had a figure no snippet could carry."""
    assert money_values("$5,000 more by $20 between $3 before $7 kids") == {5_000.0, 20.0,
                                                                          3.0, 7.0}
    assert money_values("$2bn, $3 mn, $4m. $5K $6 thousand $1.5 billion") == {
        2e9, 3e6, 4e6, 5e3, 6e3, 1.5e9}
    assert _claim("The council paid $120,000 more than planned in 2019.").status == "verified"
