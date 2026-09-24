"""A run's question set, and whether its claims still answer it.

Question ids are stable and never reused (CLAUDE.md, "Question ids are stable and never
reused"). A claim and its verdicts are filed under the id they were researched for, and nothing
moves them, so what can go wrong is the id coming to mean something else, or nothing: a
question reworded or replaced in place, or an id retired while its claim stays in claims/.
`check()` compares each claim with the question the run's questions.json holds for its id. It
is the rule's gate: `vg build` and `vg status` run it on every claim, and `vg check-claim` on
the one a researcher is handing on.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .models import Claim

FILE = "questions.json"


class UnreadableQuestions(ValueError):
    """The question set cannot be read as one question per id.

    Never read as an empty set, which would report every claim as sitting on a retired id, nor
    as a missing file, which would check nothing."""


@dataclass
class QuestionSet:
    text: dict[str, str]        # id -> the question asked at it
    maps_from: dict[str, str]   # id -> another id a `vg remap` migration declared it maps from


@dataclass
class Findings:
    unlisted: list[str] = field(default_factory=list)   # claim ids the set asks nothing at
    # (id, the question the claim answers, the question the set asks at that id)
    reworded: list[tuple[str, str, str]] = field(default_factory=list)
    pending: list[tuple[str, str]] = field(default_factory=list)   # (id, maps_from) still declared

    @property
    def failed(self) -> bool:
        """A pending `maps_from` alone does not fail: nothing applies it, so no claim moves, and
        each claim is still checked against the question at the id it sits on."""
        return bool(self.unlisted or self.reworded)


def find(data: Path) -> Path | None:
    """The run's questions.json: its own, else the data root's. None when neither exists.

    Which question set belongs to a run is #8. Until a run declares it, a candidate run
    (`data/<candidate>`, which `vg new-candidate` gives its own retargeted copy) reads its own,
    and one without falls back to its parent's template. A file that exists but can't be read
    is `load()`'s to refuse, never a reason to fall back: a dangling symlink included, which
    `exists()` alone reads as absent.
    """
    for p in (data / FILE, data.parent / FILE):
        if p.exists() or p.is_symlink():
            return p
    return None


def load(path: Path) -> QuestionSet:
    """Read a question set, refusing anything it can't read as one question per id. Every
    problem is named in one message, so a repair is not a loop of re-runs."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError) as e:
        raise UnreadableQuestions(f"unreadable question set {path}: {e}") from e
    if not isinstance(raw, list):
        raise UnreadableQuestions(f"{path} is not a list of questions")
    text: dict[str, str] = {}
    maps_from: dict[str, str] = {}
    folded: dict[str, str] = {}   # casefolded id -> the id as first given
    problems: list[str] = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            problems.append(f"entry {i} is not an object")
            continue
        qid, asked = q.get("id"), q.get("text")
        if not isinstance(qid, str) or not qid:
            problems.append(f"entry {i} has no id")
            continue
        if not isinstance(asked, str):
            problems.append(f"entry {i} ({qid}) has no text")
            continue
        if (first := folded.get(qid.casefold())) is not None:
            # An id names one question: with two, a claim on it answers one of them, and which
            # one it was researched for is not on disk. Ids differing only in case are one id,
            # since they name one claim file and one shard on a case-insensitive disk (macOS's
            # default), where the second question's research overwrites the first's.
            problems.append(f"entry {i} reuses id {first}"
                            + (f" as {qid}" if qid != first else ""))
            continue
        folded[qid.casefold()] = qid
        text[qid] = asked
        # An identity pair only adopted a rewording, and moved nothing.
        if q.get("maps_from") not in (None, "", qid):
            maps_from[qid] = str(q["maps_from"])
    if problems:
        raise UnreadableQuestions(f"{path} can't be read as one question per id: "
                                  + "; ".join(problems))
    return QuestionSet(text, maps_from)


def _words(s: str) -> list[str]:
    return unicodedata.normalize("NFC", s).split()


def _same(a: str, b: str) -> bool:
    """The same question, whitespace and Unicode composition aside: an "é" typed as one code
    point or as "e" plus an accent prints identically, and a failure nobody can see is one
    nobody can fix. Any other difference is a rewording, or a misquote the claim's `question`
    should correct: the gate can't tell which, so both fail."""
    return _words(a) == _words(b)


def check(claims: list[Claim], questions: QuestionSet) -> Findings:
    """What in `claims` breaks the stable-id rule against `questions`. Reports only: nothing
    here moves or re-files anything."""
    found = Findings(pending=list(questions.maps_from.items()))
    for c in claims:
        asked = questions.text.get(c.question_id)
        if asked is None:
            found.unlisted.append(c.question_id)
        elif not _same(c.question, asked):
            found.reworded.append((c.question_id, c.question, asked))
    return found
