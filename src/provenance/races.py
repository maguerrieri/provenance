"""A project's race.

A race is one markdown file, which the project's provenance.toml names (`race = ...`): YAML
frontmatter (what the review app is titled, the candidates) plus prose context that goes
verbatim into researcher prompts. It is project data, so it lives with the project, not in the
tool: a races/ directory inside the tool resolved into the tool's own install once it was
installed with `uv tool install`. #9 folds the race into the project file, and the key is the
bridge until then.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Candidate:
    id: str
    name: str
    party: str | None = None


@dataclass
class Race:
    name: str
    title: str
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


def load(path: Path) -> Race:
    """Load the race file at `path`: the one the project names."""
    if not path.is_file():
        raise FileNotFoundError(f"no race file at {path}")
    meta, body = _split_frontmatter(path.read_text())
    if "sources" in meta:
        # Refused, not ignored: a race still listing `us, ca` beside a project listing `us`
        # alone would lose its California lists without a word.
        raise ValueError(f"{path} names source lists (sources: {meta['sources']}), and they "
                         f"are the project's now: list them under `sources` in its "
                         f"provenance.toml, and delete the key from the race file")
    context, check = _split_context(body)
    return Race(
        name=meta.get("name", path.stem),
        title=meta.get("title", path.stem),
        candidates=[Candidate(**c) for c in (meta.get("candidates") or [])],
        election_date=meta.get("election_date"),
        context=context,
        completeness_check=check,
        path=path,
    )
