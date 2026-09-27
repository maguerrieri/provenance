"""A claim's one-sentence summary, and its answer laid out behind it (#204).

In a first trial every answer rendered as one long paragraph, and the operator writing from the
review page had to read each to find the sentence that answered the question. A claim now
carries a `summary`: one sentence, written from the answer, which the page shows first. It is the
sentence a reader is most likely to copy, so it is evidence-bearing text, checked like the
answer. Every name, host and figure here is synthetic.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from provenance import cli
from provenance.answers import SUMMARY_MAX, Block, blocks, format_problems, summary_problems
from provenance.conflicts import detect, unsourced_figures, unsourced_summary_figures
from provenance.models import Claim, Source, short_id

QUESTION = "What did the council decide about the harbor levy?"
ANSWER = ("The council approved the harbor levy on March 3, 2030, by a 5-2 vote, according to "
          "the minutes.\n\n"
          "Findings:\n"
          "- The Example Ledger reported the vote the next day.\n"
          "- The minutes record J. Doe voting no,\n"
          "  citing the $1.2 million cost.\n\n"
          "No later vote was found.")


def _source(snippet: str = "approved the harbor levy by a 5-2 vote") -> Source:
    s = Source(url="https://ledger.example/levy", publisher="Example Ledger", author="A. Writer",
               date="2030-03-04", source_type="bylined_journalism", snippet=snippet)
    s.verification.status = "verified"
    s.verification.support = "supports"
    return s


def _claim(summary: str | None = "The council approved the harbor levy 5-2 in 2030.",
           answer: str = ANSWER, *sources: Source) -> Claim:
    return Claim(question_id="q1", question=QUESTION, answer=answer, summary=summary,
                 sources=list(sources) or [_source()], corroboration_ok=True)


# --- the model -----------------------------------------------------------------------------

def test_a_blank_summary_is_no_summary():
    """One value for "none", so the fingerprints and the page cannot disagree about it."""
    for blank in ("", "  ", "\n\t"):
        assert _claim(blank).summary is None
    assert _claim(" Yes. ").summary == " Yes. ", "kept exactly as written otherwise"


def test_the_verdict_fingerprint_covers_the_summary_only_where_there_is_one():
    """A verdict is stamped with the claim it judged, and the hand-off gives the verifier the
    summary beside the answer, so a rewritten summary lapses the verdict. A claim without one
    hashes as it did before summaries existed, so every verdict already recorded still applies."""
    plain = _claim(None)
    assert plain.fingerprint == short_id(json.dumps([QUESTION, ANSWER])), \
        "unchanged for a claim with no summary"
    with_one = _claim("The council approved the harbor levy 5-2.")
    assert with_one.fingerprint != plain.fingerprint
    assert _claim("The council approved the harbor levy 5-2 in 2030.").fingerprint \
        != with_one.fingerprint
    assert _claim("").fingerprint == plain.fingerprint, "blank is none"
    # Hashed as one JSON list: text moved from the answer into the summary moves it.
    assert (Claim(question_id="q1", question="Q?", answer="A. B.", summary="C.").fingerprint
            != Claim(question_id="q1", question="Q?", answer="A.", summary="B. C.").fingerprint)


# --- the figure gate reads the summary too --------------------------------------------------

def test_a_summary_whose_figure_no_snippet_carries_is_flagged_apart_from_its_answer():
    """The answer has a figure a snippet carries (2030), so it passes. The summary states only
    the answer's other year, which no snippet carries. It is the sentence a reader copies, so it
    is asked on its own, not passed on the answer's figure. A flag, not a gate: the gate's cost
    was measured on answers alone, and a sound summary can give a figure its snippets state only
    in parts (CLAUDE.md, "A claim's summary is evidence-bearing text")."""
    s = _source("in 2030 the council approved the harbor levy")
    answer = "In 2030 the council approved the harbor levy, first proposed in 2027."
    fine = _claim("The council approved the harbor levy in 2030.", answer, s)
    assert unsourced_summary_figures(fine) == [] and detect([fine])[0].conflicts == []
    off = _claim("The harbor levy was first proposed in 2027.", answer, s)
    line = "year in the summary (2027) does not appear in any cited snippet (2030)"
    assert unsourced_summary_figures(off) == [line] and unsourced_figures(off) == []
    assert detect([off])[0].conflicts == [line], "listed for the reviewer"
    assert off.status == "verified", "a flag: the status is the answer's to decide"


def test_the_summary_flag_reads_money_however_check_claim_does():
    """check-claim takes "5,000 dollars" as a dollar figure, so the flag does too, in the summary
    and in the snippets: read with `$` forms only, a summary could state a written-out figure no
    snippet carries and nothing was flagged."""
    answer = "The fund received $5,000 in 2030, after $4,000 in 2029."
    off = _claim("The fund received 5,000 dollars.", answer, _source("received $4,000 in 2029"))
    assert unsourced_summary_figures(off) == [
        "dollar figure in the summary ($5,000) does not appear in any cited snippet ($4,000)"]
    fine = _claim("The fund received $5,000.", answer, _source("received 5,000 dollars in 2030"))
    assert unsourced_summary_figures(fine) == []
    assert unsourced_figures(fine) == [], "the answer's gate is as measured: $ forms only"


def test_a_summary_line_that_repeats_the_answers_is_left_out():
    """The summary's figures are the answer's, so where the answer's gate names a kind, the
    summary's line says the same thing, and a researcher handed both reads two things to fix.
    A kind the answer's gate does not name is still flagged."""
    s = _source("the fund received $4,000 in 2030")
    both = _claim("The fund received $5,000.", "The fund received $5,000 in 2030.", s)
    assert detect([both])[0].conflicts == [
        "dollar figure in the answer ($5,000) does not appear in any cited snippet ($4,000)"]
    year = _claim("The fund was set up in 2027.",
                  "The fund, set up in 2027, received $5,000 in 2030.", _source(
                      "the fund received $4,000 in 2030"))
    assert detect([year])[0].conflicts == [
        "dollar figure in the answer ($5,000) does not appear in any cited snippet ($4,000)",
        "year in the summary (2027) does not appear in any cited snippet (2030)"]


def test_a_claim_without_a_summary_is_gated_as_before():
    s = _source("the levy raised $40,000 in 2030")
    assert unsourced_figures(_claim(None, "It raised $40,000.", s)) == []
    assert unsourced_figures(_claim(None, "It raised $4,000.", s)) == [
        "dollar figure in the answer ($4,000) does not appear in any cited snippet ($40,000)"]


def test_the_summary_and_answer_are_never_read_as_one_text():
    """"$5" ending the answer and "million" starting the summary would read as one figure."""
    c = _claim("Million-dollar pledges were not found.", "It pledged $5", _source("pledged $5"))
    assert unsourced_figures(c) == []


# --- laying out the answer ------------------------------------------------------------------

def test_the_answer_is_laid_out_as_paragraphs_and_bullets():
    assert blocks(ANSWER) == [
        Block("p", ("The council approved the harbor levy on March 3, 2030, by a 5-2 vote, "
                    "according to the minutes.",)),
        Block("p", ("Findings:",)),
        Block("ul", ("The Example Ledger reported the vote the next day.",
                     "The minutes record J. Doe voting no,\n  citing the $1.2 million cost.")),
        Block("p", ("No later vote was found.",)),
    ]


def test_the_layout_keeps_the_text_as_written():
    """The fingerprints hash the answer as written and the hand-off prints it so, so the page
    shows it so too, white space included: folded on the page alone, a spacing change cleared a
    check with nothing on the page to say what changed. Only the structure comes out: the "- "
    that starts a bullet, and the blank lines between blocks."""
    assert blocks("  Two  spaces, indented. \n-  a bullet  \n\tits tab") == [
        Block("p", ("  Two  spaces, indented. ",)), Block("ul", (" a bullet  \n\tits tab",))]


def test_line_endings_and_blank_lines_read_as_one_break():
    assert blocks("One.\r\nStill one.\r\n\r\n\r\nTwo.\r- a\r- b") == [
        Block("p", ("One.\nStill one.",)), Block("p", ("Two.",)), Block("ul", ("a", "b"))]
    assert blocks("") == [] and blocks(" \n ") == []


def test_a_line_break_is_one_wherever_it_is_read():
    """The hand-off splits an answer with splitlines(), so the page does too: read two ways, a
    bullet the verifier was handed on a line of its own reached the page inside a paragraph."""
    assert blocks("It passed.\u2028- The minutes say so.\x85- So does the Ledger.") == [
        Block("p", ("It passed.",)), Block("ul", ("The minutes say so.", "So does the Ledger."))]


def test_markup_is_text_to_the_layout():
    """The page lays out paragraphs and bullets itself: anything else is text, escaped where it
    is shown (tests/test_review_app.py checks the escaping)."""
    assert blocks("<b>x</b>\n\n* y") == [Block("p", ("<b>x</b>",)), Block("p", ("* y",))]


def test_an_answer_outside_the_subset_is_named_line_by_line():
    assert format_problems(ANSWER) == []
    assert format_problems(
        "See [the minutes](https://ledger.example/m). **Bold**, <b>tag</b> &amp; `code`\n"
        "1. one\n  - nested\n# heading\n* star\n-") == [
        "an HTML tag ('<b>')", "an HTML entity ('&amp;')",
        "a markdown link ('[the minutes](https://ledger.example/m)')",
        "markdown emphasis or code ('**')", "a markdown heading on line 4",
        "a list marker other than '- ' on line 2",
        "an indented bullet (lists don't nest) on line 3", "an empty bullet on line 6"]


def test_markdown_that_spans_lines_is_named_too():
    """A link or an emphasis can run over a line break inside a paragraph, and some markdown is a
    line of its own: a heading's underline, a rule, a quote. None of it crosses a blank line."""
    assert format_problems("See [the board's\nminutes](https://ledger.example/m).") == [
        "a markdown link (\"[the board's\\nminutes](https://ledger.example/m)\")"]
    assert format_problems("It *passed\nunanimously* in 2030.") == [
        "markdown emphasis or code ('*passed\\nunanimously*')"]
    assert format_problems("Fees rose 3 *\n\nper year*.") == [], "a blank line ends a paragraph"
    assert format_problems("Findings\n========\n\n---\n\n> quoted") == [
        "a markdown rule or heading underline on line 2", "a markdown quote on line 6"]


def test_a_bullet_needs_a_space_after_its_dash():
    """Only "- " starts a bullet, so "-" and a tab, or a dash of another kind, would run on as
    text of the line above."""
    assert format_problems("It passed.\n-\tThe minutes say so.") == [
        "a bullet whose '-' is followed by a tab, not a space on line 2"]
    assert format_problems("It passed.\n\u2013 The minutes say so.") == [
        "a list marker other than '- ' on line 2"]


def test_ordinary_prose_is_not_read_as_markup():
    for prose in ("Fewer than <5 filings, and 3 > 2.", "It rose 4*5 times.", "A & B Co.",
                  "Per rule 12(b), the levy stands.", "The bill (AB 12) passed.",
                  "Its file is levy_final_2030.pdf.", "- a bullet\n- another"):
        assert format_problems(prose) == [], prose


# --- the summary's rules ------------------------------------------------------------------

def test_a_summary_stating_only_what_the_answer_does_passes():
    for ok in ("The council approved the harbor levy 5-2 in 2030.",
               "Yes: the council approved it on March 3, 2030, by a 5-2 vote.",
               "J. Doe voted against the $1.2 million levy.",
               "J. Doe voted against the $1,200,000 levy.",
               "Doe's vote was against; the Example Ledger reported it.",
               'The minutes say "voting no," of J. Doe.',
               "The council approved the harbor levy."):
        assert summary_problems(ok, ANSWER) == [], ok


def test_no_summary_is_named():
    assert summary_problems(None, ANSWER) == ["the claim has no summary"]


def test_one_sentence_one_line_and_short():
    assert summary_problems("The council approved it.\nDoe voted no.", ANSWER)[0] \
        == "it runs over more than one line"
    for trailing in ("The council approved it.\n", "\nThe council approved it.",
                     "The council approved it.\u2029"):
        assert summary_problems(trailing, ANSWER) == ["it runs over more than one line"], \
            "a break at either end is one too"
    assert summary_problems("The council approved it\u2028in 2030.", ANSWER) == [
        "it runs over more than one line"], "a line break to some reader"
    [two] = summary_problems("The council approved it. Doe voted no.", ANSWER)
    assert two.startswith("it goes on to a second sentence"), two
    assert summary_problems("Was it approved? Yes, 5-2.", ANSWER)[0].startswith(
        "it goes on to a second sentence")
    long = "The council approved the harbor levy " + "and the council approved it " * 9
    assert summary_problems(long.strip() + ".", ANSWER) == [
        f"it is {len(long.strip()) + 1} characters, over {SUMMARY_MAX}"]


def test_a_second_sentence_is_found_however_it_starts():
    """After a period, a lowercase letter or a digit starts a sentence as a capital does. A ? or
    ! inside a sentence is a quoted question more often than an end, so only a capital or a
    digit after one does."""
    for two in ("The council approved it in 2030. it was the only vote.",
                "The council approved it in 2030. 2031 saw no vote.",
                "The council approved fees, fines, etc. The minutes record it."):
        [problem] = summary_problems(two, "The council approved fees, fines, etc. in 2030 and "
                                          "2031: it was the only vote. The minutes record it.")
        assert problem.startswith("it goes on to a second sentence"), two
    for one in ('The council asked "why?" and approved it.',
                "The council approved fees, fines, etc. in 2030."):
        assert summary_problems(one, "The council asked \"why?\" and approved fees, fines, "
                                     "etc. in 2030.") == [], one


def test_initials_and_abbreviations_end_no_sentence():
    for one in ("J. Doe voted no.", "The Example Ledger reported it at 9 a.m. on March 3, 2030.",
                "Mr. Doe voted no.", "The vote was 5-2 vs. a 7-0 plan.",
                "The E.G. Doe committee voted no.", "It cost approx. five times more.",
                "Prop. 12 filings show it passed.", "It amends Sec. 4 of the charter.",
                "Art. II of the charter governs it."):
        assert not any(p.startswith("it goes on") for p in summary_problems(one, ANSWER)), one


def test_a_figure_a_quotation_or_a_name_the_answer_lacks_fails():
    assert summary_problems("The council approved the $1.5 million harbor levy.", ANSWER) == [
        "dollar figure(s) the answer doesn't state: $1,500,000"]
    assert summary_problems("The council approved the harbor levy 6-1.", ANSWER) == [
        "number(s) the answer doesn't state: 1, 6"]
    assert summary_problems('The council called it "a bargain".', ANSWER) == [
        "quotation(s) the answer doesn't hold: 'a bargain'"]
    assert summary_problems("The council, led by R. Roe, approved it.", ANSWER) == [
        "name(s) the answer doesn't use (a capitalized word is read as one): R, Roe"]


def test_names_compare_case_blind_and_the_first_word_is_one_too():
    """Names compare case-blind: "the Council" names what "the council" does. The first word is
    capitalized because it starts the sentence, but the subject usually comes first, so it is a
    name too unless it is a word that starts sentences: a wrong name there is the likeliest."""
    assert summary_problems("Council members approved the harbor levy.", ANSWER) == []
    assert summary_problems("Its Council approved the Harbor Levy.", ANSWER) == []
    assert summary_problems("Doe voted against the $1.2 million levy.", ANSWER) == []
    assert summary_problems("Roe voted against the $1.2 million levy.", ANSWER) == [
        "name(s) the answer doesn't use (a capitalized word is read as one): Roe"]
    for opener in ("Yes, the council approved it.", "No later vote was found.",
                   "According to the minutes, the council approved it."):
        assert summary_problems(opener, ANSWER) == [], opener


def test_money_is_one_figure_however_written_and_never_a_plain_number():
    """"$5,000" and "5,000 dollars" are one dollar figure. A dollar figure and a plain number
    are never one, though: merged, an answer's "$2,030" let a summary say "in 2030"."""
    answer = "Doe gave 5,000 dollars in 2030, Roe gave $250, and the fund holds $8.2 million."
    assert summary_problems("Doe gave $5,000 in 2030.", answer) == []
    assert summary_problems("Roe gave 250 Dollars.", answer) == []
    assert summary_problems("The fund holds 8.2 million dollars.", answer) == []
    assert summary_problems("Doe gave $6,000 in 2030.", answer) == [
        "dollar figure(s) the answer doesn't state: $6,000"]
    assert summary_problems("It was approved in 2030.", "It cost $2,030.") == [
        "number(s) the answer doesn't state: 2030"]
    assert summary_problems("It cost $2,030.", "It was approved in 2030.") == [
        "dollar figure(s) the answer doesn't state: $2,030"]


def test_a_quotation_is_a_whole_phrase_of_the_answer():
    """Letters inside another word are not a quotation: an answer saying "urban growth plan"
    does not hold "ban"."""
    answer = "The minutes call it the urban growth plan."
    assert summary_problems('It was called a "ban".', answer) == [
        "quotation(s) the answer doesn't hold: 'ban'"]
    assert summary_problems('It was called the "urban growth plan".', answer) == []


def test_numbers_are_read_by_their_thousands_groups():
    """"1,2" is two numbers and "1,000" one, and "5,000-dollar" is money."""
    assert summary_problems("It amends sections 1,2 of the code.",
                            "It amends sections 1, 2 of the code.") == []
    assert summary_problems("Doe gave a 5,000-dollar gift.", "Doe gave a $5,000 gift.") == []


def test_a_quotation_is_compared_composed():
    """An accent written as a combining mark prints as the precomposed letter, so the two are
    one quotation, as they are one question (`questions.same_question()`)."""
    composed, combining = "caf\u00e9 policy", "cafe\u0301 policy"
    assert summary_problems(f'It adopted a "{composed}".', f'It adopted a "{combining}".') == []
    assert summary_problems(f'It adopted a {combining}.', f"It adopted a {composed}.") == []


def test_markup_in_a_summary_fails():
    assert summary_problems("The council **approved** the levy.", ANSWER) == [
        "markdown emphasis or code ('**')"]


# --- check-claim ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    """Wide enough that rich folds no long tmp path, set through `_width` so the width rich
    computed is put back after: assigning `con.width` pins it for every later test (#132)."""
    monkeypatch.setattr(cli.con, "_width", 10_000)


def _provenance(*args) -> tuple[int, str]:
    res = CliRunner().invoke(cli.app, [str(a) for a in args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _check(tmp_path, **fields) -> tuple[int, str]:
    """check-claim on a not_found claim, which needs no fetch: only its summary and answer."""
    (tmp_path / "claims").mkdir(exist_ok=True)
    raw = {"question_id": "q1", "question": QUESTION, "confidence": "not_found",
           "answer": "No record of a levy vote was found in the council's minutes for 2030."}
    raw.update(fields)
    path = tmp_path / "claims" / "q1.json"
    path.write_text(json.dumps({k: v for k, v in raw.items() if v is not None}))
    return _provenance("check-claim", path)


def test_check_claim_fails_a_claim_with_no_summary_and_says_what_to_write(tmp_path):
    code, out = _check(tmp_path)
    assert code == 1, out
    assert ("summary the claim has no summary Write `summary` as one sentence, at most "
            f"{SUMMARY_MAX} characters, that answers the question from the answer") in out, out
    assert "Not ready. Fix these and re-run" in out, out


def test_check_claim_passes_a_summary_stating_only_what_the_answer_does(tmp_path):
    code, out = _check(tmp_path, summary="No levy vote was found in the 2030 minutes.")
    assert code == 0 and "All sources check out." in out, out


def test_check_claim_names_every_problem_with_a_summary_at_once(tmp_path):
    code, out = _check(tmp_path, summary="No levy vote was found in 2031. Roe says so.")
    assert code == 1, out
    assert ("it goes on to a second sentence" in out and "number(s) the answer doesn't state: "
            "2031" in out and "name(s) the answer doesn't use (a capitalized word is read as "
            "one): Roe" in out), out


def test_check_claim_fails_an_answer_outside_the_subset(tmp_path):
    code, out = _check(tmp_path, summary="No levy vote was found.",
                       answer="No levy vote was found.\n\n1. **Minutes** searched")
    assert code == 1, out
    assert ("answer format markdown emphasis or code ('**'); a list marker other than '- ' on "
            "line 3 The answer may hold paragraphs (a blank line between them) and bullet "
            "lists (lines starting '- '), and nothing else") in out, out
