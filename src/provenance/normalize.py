"""Text normalization shared by the verifier and the review app.

The whole pipeline rests on one promise: if we say a snippet is on a page, a human
pressing Cmd-F will find it. So matching is *literal* first. Normalization is the
fallback for cases where the promise is still kept in spirit but not in bytes —
PDF reflow, smart quotes, ligatures, collapsed whitespace.

`normalize()` returns the normalized text plus an index map back into the raw text,
so a normalized match can still be shown to the human with real surrounding context.
"""

from __future__ import annotations

import unicodedata

# Characters that render identically (or near enough) but differ in bytes.
_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "", "­": "",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
}


def normalize(text: str, *, casefold: bool = True) -> tuple[str, list[int]]:
    """Return (normalized_text, idx_map) where idx_map[i] is the raw offset of
    normalized character i.

    Normalization: NFKC-ish char folding, whitespace collapsed to single spaces,
    casefolded unless `casefold` is False. Leading whitespace is dropped rather than emitted.
    """
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # suppress leading whitespace
    for raw_i, ch in enumerate(text):
        mapped = _CHAR_MAP.get(ch)
        if mapped is None:
            mapped = unicodedata.normalize("NFKC", ch)
        if mapped.isspace() or (len(mapped) == 1 and mapped in "\t\r\n\f\v"):
            if prev_space:
                continue
            out.append(" ")
            idx.append(raw_i)
            prev_space = True
            continue
        if not mapped:
            continue
        folded = mapped.casefold() if casefold else mapped
        # "--" is a common ASCII rendering of an em dash; collapse hyphen runs so it
        # folds onto the same normalized form.
        if folded == "-" and out and out[-1] == "-":
            continue
        for c in folded:
            out.append(c)
            idx.append(raw_i)
        prev_space = False
    # trailing space
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def find_all(haystack: str, needle: str) -> list[int]:
    """Start offsets of every NON-OVERLAPPING occurrence of needle.

    The verifier's uniqueness check counts these, so the non-overlap matters: a snippet
    like "aa" in "aaa" counts once, not twice.
    """
    if not needle:
        return []
    hits: list[int] = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            return hits
        hits.append(i)
        start = i + len(needle)


def dehyphenate(text: str, idx: list[int]) -> tuple[str, list[int]]:
    """Drop `- ` between letters from already-normalized text, carrying the index map.

    PDFs break words across lines ("settle-\nment"), which normalizes to "settle- ment"
    and can never match a snippet containing the whole word. Joining is a guess — it also
    joins a genuine hyphenate broken at its hyphen ("cost- sharing") — so this is a
    match-time fallback only: the stored page text is never mutated, the excerpt the human
    reads stays faithful to the document, and any hit found this way is reported as a
    normalized (not exact) match.
    """
    out: list[str] = []
    out_idx: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        if (text[i] == "-" and i + 2 < n and text[i + 1] == " "
                and text[i + 2].isalpha() and out and out[-1].isalpha()):
            i += 2
            continue
        out.append(text[i])
        out_idx.append(idx[i])
        i += 1
    return "".join(out), out_idx


def context_window(text: str, start: int, end: int, radius: int = 600) -> tuple[str, int, int]:
    """Slice `radius` chars either side of [start, end), snapped to whitespace so the
    excerpt starts and ends on word boundaries. Returns (excerpt, rel_start, rel_end).
    """
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    if lo > 0:
        sp = text.find(" ", lo, start)
        if sp != -1:
            lo = sp + 1
    if hi < len(text):
        sp = text.rfind(" ", end, hi)
        if sp != -1:
            hi = sp
    return text[lo:hi], start - lo, end - lo
