"""Source-class enforcement.

Rules live in `source_lists/<name>-sources.yaml` and are selected per project, so a California
project loads `us` + `ca`, and a future city project could add a city list. The lists merge; a
domain in any loaded list counts. Besides the classes, a list names the hosts that publish legal
text (`legal_text`), whose citations must carry the version they quote. A list can ship notes
beside it (`<name>-notes.md`), which `provenance brief` hands to researchers and verifiers
(`notes()`).
"""

from __future__ import annotations

import re
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
# Not a category: a host that publishes the text of law (statutes, codes, regulations), whatever
# its class. Legal text is a series, like a filing: the version before the last amendment
# verifies exactly like the one in force, so a citation there must say which version it quotes
# (`verify.missing_legal_version()`). Which hosts those are is list data, per jurisdiction.
LEGAL_TEXT = "legal_text"
KEYS = (*CATEGORIES, LEGAL_TEXT)
# What `domain()` can return for a URL: a bare, lowercase host of two labels or more, never
# starting with `www.`, which it strips. A label is letters and digits, Unicode ones included
# (`domain()` returns a host as the URL spells it), with hyphens inside. An entry of any other
# shape (a scheme, a path, a port) matches nothing.
_LABEL = r"[^\W_](?:[\w-]*[^\W_])?"
_HOST = re.compile(rf"(?!www\.){_LABEL}(?:\.{_LABEL})+")


class _OneOfEachKey(yaml.SafeLoader):
    """`yaml.safe_load()`, except that a repeated key is refused. PyYAML keeps the last of two
    equal keys, so a second `legal_text:` block appended to a list would replace the first one's
    hosts without a word. A SafeLoader still: it constructs no Python object a tag names."""

    def construct_mapping(self, node, deep=False):
        seen: set = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode):
                continue   # not a key a list can use; the unknown-key check names it
            if key.value in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"{key.value!r} is given twice", key.start_mark)
            seen.add(key.value)
        return super().construct_mapping(node, deep)


def available(sources_dir: Path | None = None) -> list[str]:
    d = sources_dir or SOURCES_DIR
    return sorted(p.stem.removesuffix("-sources") for p in d.glob("*-sources.yaml"))


@lru_cache(maxsize=8)
def load_rules(names: tuple[str, ...] = ("us",), sources_dir: str | None = None) -> dict[str, tuple[str, ...]]:
    """Merge the named source lists. Order doesn't matter — classification checks the
    most restrictive category first, so a domain listed as excluded stays excluded even
    if another list also names it.

    A list that can't be read as exactly these keys, each a list of hosts, is refused with a
    ValueError naming it (`project.load()` reports it as a problem with the project). Read past,
    each of these fails open: a misspelled or repeated `legal_text` lists no host, or only some,
    and every undated statute on the rest passes; a host written without the list's `-` is read
    one letter at a time; and a host with a scheme, a path or `www.` matches no URL."""
    d = Path(sources_dir) if sources_dir else SOURCES_DIR
    merged: dict[str, list[str]] = {k: [] for k in KEYS}
    for name in names:
        p = d / f"{name}-sources.yaml"
        if not p.exists():
            raise FileNotFoundError(
                f"no source list {name!r} in {d} (have: {', '.join(available(d)) or 'none'})")
        try:
            data = yaml.load(p.read_text(encoding="utf-8"), Loader=_OneOfEachKey)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise ValueError(f"source list {p} can't be read: {e}") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"source list {p} is not a mapping of {', '.join(KEYS)}")
        if unknown := [k for k in data if k not in KEYS]:
            raise ValueError(f"source list {p} has key(s) it can't use: "
                             f"{', '.join(map(repr, unknown))} (known: {', '.join(KEYS)})")
        for k in KEYS:
            hosts = data.get(k)
            hosts = [] if hosts is None else hosts   # `key:` with nothing under it lists none
            if not isinstance(hosts, list):
                raise ValueError(f"source list {p}: {k!r} must be a list of host names")
            if bad := [h for h in hosts
                       if not isinstance(h, str) or not _HOST.fullmatch(h.strip().lower())]:
                raise ValueError(f"source list {p}: {k!r} holds what is not a bare host name: "
                                 f"{', '.join(map(repr, bad))} (write `example.gov`: no "
                                 f"scheme, path, port or `www.`)")
            merged[k].extend(h.strip().lower() for h in hosts)
    return {k: tuple(dict.fromkeys(v)) for k, v in merged.items()}


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


def publishes_legal_text(url: str, rules: dict[str, tuple[str, ...]] | None = None) -> bool:
    """True when `url` is on a host a loaded list names under `legal_text`."""
    r = rules if rules is not None else default_rules()
    return _matches(domain(url), r.get(LEGAL_TEXT, ()))


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
