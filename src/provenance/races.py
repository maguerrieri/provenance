"""Race definitions.

A race is one markdown file in `races/`: YAML frontmatter (which source lists apply,
what the review app is titled) plus prose context that goes verbatim into researcher
prompts. Everything race-specific lives here, so a new race is a new file rather than an
edit to the pipeline or the skill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

RACES_DIR = Path(__file__).resolve().parents[2] / "races"


@dataclass
class Candidate:
    id: str
    name: str
    party: str | None = None


@dataclass
class Race:
    name: str
    title: str
    sources: list[str] = field(default_factory=lambda: ["us"])
    election_date: str | None = None
    candidates: list["Candidate"] = field(default_factory=list)
    context: str = ""            # prompt-safe: goes into researcher prompts verbatim
    completeness_check: str = ""  # orchestrator/reviewer only — never into a prompt
    path: Path | None = None


# Everything from this heading to the next top-level one is withheld from researcher
# prompts. A researcher told what it is looking for confirms that item instead of
# searching, and whatever is not on the list never surfaces.
_CHECK_HEADING = "# Completeness check"


def candidate(race: "Race", cid: str) -> "Candidate":
    for c in race.candidates:
        if c.id == cid:
            return c
    have = ", ".join(c.id for c in race.candidates) or "none"
    raise ValueError(f"no candidate {cid!r} in race {race.name} (have: {have})")


def _split_context(body: str) -> tuple[str, str]:
    """Return (prompt_safe_context, completeness_check)."""
    i = body.find(_CHECK_HEADING)
    if i == -1:
        return body, ""
    rest = body[i:]
    end = rest.find("\n# ", len(_CHECK_HEADING))
    if end == -1:
        return body[:i].rstrip() + "\n", rest.strip()
    return (body[:i] + rest[end + 1:]).rstrip() + "\n", rest[:end].strip()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return yaml.safe_load(parts[1]) or {}, parts[2].lstrip("\n")


def available(races_dir: Path | None = None) -> list[str]:
    d = races_dir or RACES_DIR
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


def load(name: str | None = None, races_dir: Path | None = None) -> Race:
    """Load a race by name. With no name, load the only race if there is exactly one —
    the common case is a single active race, and making that implicit keeps every CLI
    call from carrying a --race flag."""
    d = races_dir or RACES_DIR
    names = available(d)
    if name is None:
        if len(names) == 1:
            name = names[0]
        elif not names:
            raise FileNotFoundError(f"no race files in {d}")
        else:
            raise ValueError(f"multiple races ({', '.join(names)}); pass --race")
    path = d / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"no race {name!r} in {d} (have: {', '.join(names) or 'none'})")
    meta, body = _split_frontmatter(path.read_text())
    context, check = _split_context(body)
    return Race(
        name=meta.get("name", name),
        title=meta.get("title", name),
        sources=list(meta.get("sources", ["us"])),
        candidates=[Candidate(**c) for c in (meta.get("candidates") or [])],
        election_date=meta.get("election_date"),
        context=context,
        completeness_check=check,
        path=path,
    )
