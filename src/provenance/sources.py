"""Source-class enforcement.

Rules live in `source_lists/<name>-sources.yaml` and are selected per project, so a California
project loads `us` + `ca`, and a future city project could add a city list. The lists merge; a
domain in any loaded list counts. A list can ship notes beside it (`<name>-notes.md`), which
`provenance brief` hands to researchers and verifiers (`notes()`).
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .models import Source

# Package data, so an installed copy (uv tool install) has it.
SOURCES_DIR = Path(files(__package__) / "source_lists")
CATEGORIES = ("excluded", "lead_generator_only", "campaign_statement_only",
              "primary_document", "bylined_journalism")


def available(sources_dir: Path | None = None) -> list[str]:
    d = sources_dir or SOURCES_DIR
    return sorted(p.stem.removesuffix("-sources") for p in d.glob("*-sources.yaml"))


@lru_cache(maxsize=8)
def load_rules(names: tuple[str, ...] = ("us",), sources_dir: str | None = None) -> dict[str, tuple[str, ...]]:
    """Merge the named source lists. Order doesn't matter — classification checks the
    most restrictive category first, so a domain listed as excluded stays excluded even
    if another list also names it."""
    d = Path(sources_dir) if sources_dir else SOURCES_DIR
    merged: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for name in names:
        p = d / f"{name}-sources.yaml"
        if not p.exists():
            raise FileNotFoundError(
                f"no source list {name!r} in {d} (have: {', '.join(available(d)) or 'none'})")
        data = yaml.safe_load(p.read_text()) or {}
        for c in CATEGORIES:
            merged[c].extend(data.get(c) or [])
    return {c: tuple(dict.fromkeys(v)) for c, v in merged.items()}


def notes(names: tuple[str, ...], sources_dir: str | Path | None = None) -> list[tuple[str, str]]:
    """Each named list's notes, as `(name, text)` in the order the project names them:
    `<name>-notes.md` beside `<name>-sources.yaml`, on how that list's records behave (which
    filings come in series, which portals answer only through a bulk export). A list with no
    notes file has none. `provenance brief` hands them to every researcher and verifier, so the
    core agents and skill carry no domain content. A leading `<!-- ... -->` is a note to the
    tool's maintainers and is left out. Only a listed source list has notes: a name is joined
    into a path, so one that `available()` doesn't list (`../x`, a typo) reads nothing, though
    `project.load()` already refuses a project naming one."""
    d = Path(sources_dir) if sources_dir else SOURCES_DIR
    listed = set(available(d))
    found = []
    for name in dict.fromkeys(names):
        if name not in listed:
            continue
        p = d / f"{name}-notes.md"
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8").strip()
        if text.startswith("<!--") and "-->" in text:
            text = text.split("-->", 1)[1].strip()
        if text:
            found.append((name, text))
    return found


def default_rules() -> dict[str, tuple[str, ...]]:
    """`us` alone: the lists for a caller that has no project. Every command passes the lists
    its project names (`sources = ["us", "ca"]` in provenance.toml), so a California project
    sees CalMatters and LegInfo while a project elsewhere doesn't inherit them. This used to
    read a file kept in the tool itself, which a command checking another project's citations
    could silently disagree with."""
    return load_rules(("us",))


def domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, entries: tuple[str, ...]) -> bool:
    return any(host == e or host.endswith("." + e) for e in entries)


def classify(url: str, rules: dict[str, tuple[str, ...]] | None = None) -> str:
    """One of: bylined_journalism, primary_document, lead_generator_only,
    excluded, campaign_statement_only, unknown."""
    r = rules if rules is not None else default_rules()
    host = domain(url)
    for key in CATEGORIES:  # most restrictive first
        if _matches(host, r.get(key, ())):
            return key
    return "unknown"


def check_source_class(src: Source, rules: dict[str, tuple[str, ...]] | None = None) -> tuple[bool, str | None]:
    """(ok, reason). Unknown domains are allowed but must carry a named or institutional
    author — the rule is 'human-written', not 'on our list'. That keeps a good local
    paper or an agency we haven't listed from being rejected out of hand."""
    cls = classify(src.url, rules)
    if cls == "excluded":
        return False, f"{domain(src.url)} is an excluded AI aggregator / content farm"
    if cls == "lead_generator_only":
        return False, (f"{domain(src.url)} is a lead-generator only; cite the underlying "
                       "primary source it references")
    if cls == "campaign_statement_only" and src.source_type != "campaign_statement":
        return False, ("campaign material is citable only for the claim form 'the campaign "
                       "says X' (source_type must be campaign_statement)")
    if not src.author or not src.author.strip():
        return False, "no named or institutional author-of-record"
    if src.author.strip().lower() in {"staff", "unknown", "n/a", "none", "editorial board"}:
        return False, f"author {src.author!r} is not a named or institutional author-of-record"
    return True, None
