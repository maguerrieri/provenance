"""Shared fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The synthetic example project: no person, office, proposal or record in it is real.
EXAMPLE_TITLE = "Example County Assessor"
EXAMPLE_SUBJECTS = {"lind": "Avery Lind", "ng": "Jordan Ng"}
EXAMPLE_CONTEXT = """\
A fictional project for the test suite: no person, office or record in it is real.

Avery Lind, the county's deputy assessor, and Jordan Ng, a former county auditor, are the
project's subjects.

Where the records are:
- Assessment appeals board decisions are PDFs on the county site, one per hearing date. The
  board's agenda page links each one; search engines index few of them.
- The assessor's annual reports carry the office's own figures on roll growth and backlog.
- Filings for county offices are with the county registrar, not CAL-ACCESS.
"""
# Answers already known, which no researcher is ever told: the leak tests look for these.
EXAMPLE_CHECK = """\
Known claims the research should surface on its own. Never pasted into a researcher prompt.

- The Doe settlement: the county paid $120,000 in 2019 to settle a suit over an appeal
  decision. One outlet reported $102,000.
- Avery Lind's position on the proposed parcel tax for flood control.
- Facts once drafted into the context and moved here, since context is checked by nobody:
  Lind took 58.3% of the June vote to Ng's 41.7%; Ng's office is run by Rowan Pike and Ellis
  Marlowe; Lind re-registered in another party in 2024.
"""


def _toml(value) -> str:
    """`value` as TOML: strings as JSON, which TOML reads as the same basic string."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value)


def write_project(root: Path, *, subjects=(), cache=".", sources=("us", "ca"),
                  name: str = "example", title: str | None = EXAMPLE_TITLE,
                  context: str = EXAMPLE_CONTEXT, completeness_check: str = EXAMPLE_CHECK,
                  extra: str = "") -> Path:
    """Make `root` a project by writing its provenance.toml, and return `root`.

    The synthetic example unless told otherwise, with its cache beside the file, as a project
    laid out the old way had it in data/cache. A subject given as a bare id is named as the
    example names it, or "Subject <id>"; one given as a dict is written as it is. `extra` is
    appended as it is, for a line a test wants verbatim."""
    root.mkdir(parents=True, exist_ok=True)
    listed = [s if isinstance(s, dict)
              else {"id": s, "name": EXAMPLE_SUBJECTS.get(s, f"Subject {s}")}
              for s in subjects]
    lines = [f"name = {_toml(name)}",
             f"sources = {_toml(list(sources))}",
             f"cache = {_toml(str(cache))}",
             f"subjects = {_toml(listed)}",
             f"context = {_toml(context)}",
             f"completeness_check = {_toml(completeness_check)}"]
    if title is not None:
        lines.append(f"title = {_toml(title)}")
    (root / "provenance.toml").write_text("\n".join(lines) + "\n" + extra)
    return root


@pytest.fixture(autouse=True)
def example_project(tmp_path):
    """Every test's tmp_path is a project: the synthetic example, with no subjects. Commands
    find their project by walking up from the run, so a run in tmp_path itself needs nothing
    more, and a test laying a run out elsewhere writes its own with `write_project()` (the
    nearest provenance.toml wins). The repo ships no project: a project's context and data are
    its own."""
    write_project(tmp_path)
