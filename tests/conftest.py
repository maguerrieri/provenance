"""Shared fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_RACE = Path(__file__).parent / "fixtures" / "races" / "example.md"


def write_project(root: Path, *, subjects=(), cache=".", sources=("us", "ca"),
                  race: Path | None = FIXTURE_RACE, name: str = "example") -> Path:
    """Make `root` a project by writing its provenance.toml, and return `root`.

    Its race is the synthetic example unless `race` says otherwise, and its cache sits beside
    the file, as a project laid out the old way had it in data/cache. Strings go in as JSON,
    which TOML reads as the same basic string."""
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"name = {json.dumps(name)}",
             f"sources = {json.dumps(list(sources))}",
             f"cache = {json.dumps(str(cache))}",
             f"subjects = {json.dumps(list(subjects))}"]
    if race is not None:
        lines.append(f"race = {json.dumps(str(race))}")
    (root / "provenance.toml").write_text("\n".join(lines) + "\n")
    return root


@pytest.fixture(autouse=True)
def example_project(tmp_path):
    """Every test's tmp_path is a project: the synthetic example, with no subjects. Commands
    find their project by walking up from the run, so a run in tmp_path itself needs nothing
    more, and a test laying a run out elsewhere writes its own with `write_project()` (the
    nearest provenance.toml wins). The repo ships no project: a project's race and data are
    its own."""
    write_project(tmp_path)
