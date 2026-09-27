"""A claim's answer as the review page lays it out, and the rules `provenance check-claim`
holds its summary and its answer to (#204).

The answer may hold paragraphs (a blank line between them) and bullet lists (lines starting
`- `), and nothing else. The page lays out that subset itself, from `blocks()`, and every piece
of text in it is escaped like all other agent-authored text: nothing here is ever HTML, and
`report.context_html()` stays the only value the template takes as markup.

The summary is one sentence answering the question, written from the answer. It is the sentence
a reader is most likely to copy, so `summary_problems()` refuses one that states what the answer
does not: a figure, a name, or a quotation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from .conflicts import DOLLARS, MONEY, dollar_values, money
from .normalize import normalize

# Long enough for a sentence that answers the question with its figure and its period, short
# enough that it is one.
SUMMARY_MAX = 250

HOW_TO_WRITE = (f"Write `summary` as one sentence, at most {SUMMARY_MAX} characters, that "
                f"answers the question from the answer: every figure, name and quotation in it "
                f"must be in the answer, as the answer writes it.")
FORMAT_RULE = ("The answer may hold paragraphs (a blank line between them) and bullet lists "
               "(lines starting '- '), and nothing else: the review page shows anything else "
               "exactly as typed.")


@dataclass(frozen=True)
class Block:
    """One paragraph (`p`, one item: its text, line breaks kept) or one bullet list (`ul`, one
    item per bullet)."""
    kind: Literal["p", "ul"]
    items: tuple[str, ...]


def _lines(text: str) -> list[str]:
    """`text`'s lines as the hand-off prints them: splitlines(), not "\\n", since a U+2028 or a
    NEL is a line break to some reader. Read two ways, a bullet the verifier was handed on a line
    of its own could reach the page inside a paragraph."""
    return text.splitlines()


def blocks(answer: str) -> list[Block]:
    """`answer` as paragraphs and bullet lists.

    A blank line ends a block. Inside one, a line starting `- ` starts a bullet, a line after a
    bullet continues it, and lines before the first bullet are a paragraph of their own, so
    "Findings:" above a list stays above it.

    Everything else is kept as written, white space included, and the page shows it so
    (`white-space: pre-wrap`): the fingerprints hash the answer as written, and the hand-off
    prints it so, and a page that folded spacing the others keep would show one text while they
    covered another. Only the structure is taken out: the `- ` that starts a bullet, and the
    blank lines between blocks."""
    out: list[Block] = []
    para: list[str] = []
    items: list[str] = []

    def close() -> None:
        if para:
            out.append(Block("p", ("\n".join(para),)))
        if items:
            out.append(Block("ul", tuple(items)))
        para.clear()
        items.clear()

    for line in _lines(answer):
        if not line.strip():
            close()
        elif line.startswith("- "):
            if para:   # the lead-in above a list is its own paragraph
                out.append(Block("p", ("\n".join(para),)))
                para.clear()
            items.append(line[2:])
        elif items:
            items[-1] += "\n" + line
        else:
            para.append(line)
    close()
    return out


# Markup the page would show as typed. Each is named in what check-claim prints. A link or an
# emphasis can run over a line break inside a paragraph (`_ONE_BREAK`), never over a blank line.
_ONE_BREAK = r"\n(?![^\S\n]*\n)"
_INLINE = (
    ("an HTML tag", re.compile(r"</?[A-Za-z][\w-]*(?:\s[^<>]*)?/?>|<!--")),
    ("an HTML entity", re.compile(r"&(?:[A-Za-z][A-Za-z0-9]*|#\d+|#[xX][0-9A-Fa-f]+);")),
    ("a markdown link", re.compile(rf"\[(?:[^\]\n]|{_ONE_BREAK})+\]\([^)\n]*\)")),
    ("markdown emphasis or code", re.compile(
        rf"\*\*|__|`|(?<![\w*])\*(?=[^\s*])(?:[^*\n]|{_ONE_BREAK})*[^\s*]\*(?![\w*])")),
)
_LINE = (
    ("a markdown heading", re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")),
    ("a list marker other than '- '", re.compile(r"^\s*(?:[*+•·‣◦–—]|\d{1,3}[.)])\s")),
    # "-" then a tab starts no bullet either: only "- " does.
    ("a bullet whose '-' is followed by a tab, not a space", re.compile(r"^-[^\S \n]")),
    ("an indented bullet (lists don't nest)", re.compile(r"^\s+-\s")),
    # A bare "-" starts no bullet ("- " does), so it would run on as text of the one above.
    ("an empty bullet", re.compile(r"^\s*-\s*$")),
    # Markdown that spans lines: a heading underlined on the next line, a rule, a quote.
    ("a markdown rule or heading underline",
     re.compile(r"^\s{0,3}(?:=+|(?:[-_*]\s*){3,})\s*$")),
    ("a markdown quote", re.compile(r"^\s{0,3}>")),
)


def _markup(text: str, *, lines: bool) -> list[str]:
    found = []
    for what, rx in _INLINE:
        if m := rx.search(text):
            found.append(f"{what} ({m.group(0)!r})")
    if lines:
        for what, rx in _LINE:
            for n, line in enumerate(_lines(text), 1):
                if rx.search(line):
                    found.append(f"{what} on line {n}")
                    break
    return found


def format_problems(answer: str) -> list[str]:
    """What in `answer` is outside the subset the page lays out: each kind once, with where."""
    return _markup(answer, lines=True)


# A sentence ends at . ! or ?, any closing quote or bracket, and white space, before the letter or
# digit that starts the next. A period after an initial or one of these abbreviations ends
# nothing: "J. Doe", "U.S. law", "Dr. Roe", "No. 12", "approx. five".
_END = re.compile(r"[.!?][\"'”’)\]]*\s+[\"'“‘(\[]*(?=\S)")
_ABBREVIATIONS = frozenset((
    "mr mrs ms dr jr sr st no nos vs inc co corp ltd llc dept govt gov sen rep assn assoc bros "
    "ave blvd hon univ jan feb mar apr jun jul aug sep sept oct nov dec approx est min max "
    "fig vol pp p pct sq ft yr yrs hr hrs e.g i.e al cf ca "
    # A record's or a law's parts: "Prop. 12", "Sec. 4", "Stat. 1990", "Art. II".
    "prop props sec secs art arts stat stats ch chap subd subdiv para div pt pts ord res reg "
    "regs amdt const cl").split())
# After these a period ends a sentence only before a capital: "fees, fines, etc. are listed"
# is one, and "fees, fines, etc. The board" is two.
_ENDERS = frozenset({"etc"})
_OPENING = "\"'“‘(["


def _sentence_break(summary: str) -> str | None:
    """Where `summary` goes on to a second sentence, as the text around the break, or None.
    A heuristic, wrong both ways at the edges: an abbreviation it doesn't list reads as a break,
    which costs a rewrite, and one it lists hides a break after it ("Roe Inc. The board ..."),
    which the verifier, handed the summary, still reads."""
    for m in _END.finditer(summary):
        after = summary[m.end():m.end() + 1]
        mark = summary[m.start()]
        # A ? or ! inside a sentence is a quoted question or exclamation more often than an end.
        if not (after.isalnum() if mark == "." else after.isupper() or after.isdigit()):
            continue
        if mark == ".":
            before = re.search(r"(\S*)$", summary[:m.start()]).group(1).lstrip(_OPENING)
            last = before.rsplit(".", 1)[-1]
            if not last or (len(last) == 1 and last.isalpha()):   # an initial
                continue
            if before.lower() in _ABBREVIATIONS or last.lower() in _ABBREVIATIONS:
                continue
            if last.lower() in _ENDERS and not after.isupper():
                continue
        return summary[max(0, m.start() - 20):m.end() + 20]
    return None


# A number as written: digits in thousands groups or none, and a decimal part, so "1,000" is
# one number and "1,2" two. Money is read as money first (`conflicts.dollar_values`), so "$8.2
# million" and "$8,200,000" are one figure.
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
# A word: letters, with an apostrophe inside (O'Brien). Hyphens split, so "Vice-Mayor" is two.
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_QUOTED = re.compile(r"[“\"]([^”\"\n]+)[”\"]")
# Words that start a sentence without naming anything. A summary's first word is capitalized
# because it starts the sentence, so it is read as a name unless it is one of these: the subject
# usually comes first, and a wrong name there is the one most worth catching.
_OPENERS = frozenset((
    "a an the this that these those there it its he she they their his her we our yes no none "
    "not nothing nobody neither both each every all most some any one two three in on at by for "
    "from of to as since after before during between while when where although though because "
    "if per according under over about with without across among until through").split())


def _amount(tok: str) -> Decimal | None:
    try:
        return Decimal(tok.rstrip(",").replace(",", ""))
    except InvalidOperation:   # a bare run of commas
        return None


def _numbers(text: str) -> set[Decimal]:
    """Every number in `text` that isn't a dollar figure (`conflicts.dollar_values()`, written
    with a $ or with "dollars"). Kept apart from those: "in 2030" is not "$2,030", so neither may
    stand in for the other."""
    plain = DOLLARS.sub(" ", MONEY.sub(" ", text))
    return {v for tok in _NUMBER.findall(plain) if (v := _amount(tok)) is not None}


def _bare(word: str) -> str:
    """A word as compared: case folded, a possessive dropped ("Doe's" is "Doe")."""
    return re.sub(r"['’]s$", "", word, flags=re.I).casefold()


def summary_problems(summary: str | None, answer: str) -> list[str]:
    """Why `summary` is not one sentence of at most `SUMMARY_MAX` characters stating only what
    `answer` states, or [] when it is.

    What it states is read three ways: its figures (dollar amounts as money, whether written
    "$5,000" or "5,000 dollars", and every other number by value, the two kept apart), its
    quotations
    (text in double quotes, compared as a question's text is: `questions.same_question()`), and
    its names (every capitalized word, the first too unless it is a word that starts sentences,
    compared case-blind). Each must be in the answer. It can't tell whether the sentence is true
    to the answer, only that it adds none of these: that is the verifier's, who is handed both.
    """
    if summary is None:
        return ["the claim has no summary"]
    # Composed first, as `questions.same_question()` does: `normalize()` folds one character at a
    # time, and a word splits at a combining accent, so "café" written two ways would differ.
    summary, answer = (unicodedata.normalize("NFC", t) for t in (summary, answer))
    found = []
    # A break anywhere, a trailing one too: splitlines() drops a last one, so ask whether it
    # found anything to split. Not "\\n" alone: a U+2028 or a NEL is a line break to some reader.
    if summary.splitlines() != [summary]:
        found.append("it runs over more than one line")
    if len(summary) > SUMMARY_MAX:
        found.append(f"it is {len(summary)} characters, over {SUMMARY_MAX}")
    if where := _sentence_break(summary):
        found.append(f"it goes on to a second sentence ({where!r})")
    found += _markup(summary, lines=False)
    if extra := dollar_values(summary) - dollar_values(answer):
        found.append("dollar figure(s) the answer doesn't state: "
                     + ", ".join(money(v) for v in sorted(extra)))
    if extra := _numbers(summary) - _numbers(answer):
        found.append("number(s) the answer doesn't state: "
                     + ", ".join(str(v) for v in sorted(extra)))
    # As whole words: "ban" is not quoted by an answer that says "urban".
    said, _ = normalize(answer)
    if missing := [q for q in _QUOTED.findall(summary) if not re.search(
            rf"(?<!\w){re.escape(normalize(q)[0])}(?!\w)", said)]:
        found.append("quotation(s) the answer doesn't hold: "
                     + ", ".join(repr(q) for q in missing))
    words = {_bare(m.group(0)) for m in _WORD.finditer(answer)}
    names = [w for n, m in enumerate(_WORD.finditer(summary)) if (w := m.group(0))[0].isupper()
             and not (n == 0 and _bare(w) in _OPENERS)]
    if missing := list(dict.fromkeys(n for n in names if _bare(n) not in words)):
        found.append("name(s) the answer doesn't use (a capitalized word is read as one): "
                     + ", ".join(missing))
    return found
