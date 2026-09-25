"""Text for a terminal: what the pipeline does not control, shown so it does nothing but print.

Its own module so that every module that prints or logs such text shares one definition:
`cli.py` imports it as `_printable()`, and `fetch.py` logs through it. `fetch.py` cannot
import `cli.py`, which imports it.
"""

from __future__ import annotations

import re
import unicodedata

# Control, format, surrogate and line/paragraph-separator characters: what can move the cursor,
# erase a line, reorder text or start a new line in a reader that is not this terminal.
UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def printable(text: str, *, lines: bool = False) -> str:
    """`text` with every character in `UNPRINTABLE` but a tab shown as an escape (`\\x1b`,
    `\\u2028`), so text the pipeline does not control prints on the line it is given and does
    nothing else to the terminal. Every print or log of such text goes through here. rich's
    `Text` strips only BEL, BS, VT, FF and CR, so an ESC or C1 sequence in a filer name or a
    response body erased lines or faked output. Splitting on `\\n` alone left an ANSI erase-line
    or a U+2028 to fake a line of `vg handoff`'s own framing, and a lone surrogate (which
    json.loads keeps, and which an undecodable byte in argv becomes) made the print raise.
    Backslashes are left alone: the text a verifier compares, snippet against context, must read
    as written.

    With `lines`, a line break (`\\n`, or `\\r\\n`) stays one and each line is shown as above:
    for multi-line data a reader copies from (page text, a response body, a YAML entry), and a
    message whose line breaks are its own (pydantic's). Any other line break (a lone CR, NEL,
    U+2028) still shows as an escape."""
    if lines:
        return "\n".join(printable(line) for line in re.split(r"\r?\n", text))
    return "".join(ch if ch == "\t" or unicodedata.category(ch) not in UNPRINTABLE
                   else (f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else
                         f"\\u{ord(ch):04x}" if ord(ch) < 0x10000 else f"\\U{ord(ch):08x}")
                   for ch in text)
