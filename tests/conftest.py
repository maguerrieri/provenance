"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from vgpipe import races

FIXTURE_RACES = Path(__file__).parent / "fixtures" / "races"


@pytest.fixture(autouse=True)
def example_race(monkeypatch):
    """Every test sees one race: the synthetic example. `vg verify`, `build`, `check-claim` and
    `new-candidate` each load the race, and races/ holds a project's own races, which this repo
    does not ship. races.load() reads RACES_DIR at call time, so patching the global is enough."""
    monkeypatch.setattr(races, "RACES_DIR", FIXTURE_RACES)
