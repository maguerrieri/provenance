"""Source-class enforcement.

Rules live in `source_lists/<name>-sources.yaml` and are selected per project, so a California
project loads `us` + `ca`, and a future city project could add a city list. The lists merge; a
domain in any loaded list counts. Besides the classes, a list names the hosts that publish legal
text (`legal_text`), whose citations must carry the version they quote. A list can ship notes
beside it (`<name>-notes.md`), which `provenance brief` hands to researchers and verifiers
(`notes()`). A project adds the hosts that issue its own records (`primary_hosts` in its
provenance.toml), which the tool's lists can't know.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

import idna
import yaml

from .models import Source
from .normalize import normalize

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
_LABEL = r"[^\W_](?:(?:[^\W_]|-)*[^\W_])?"
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
def load_rules(names: tuple[str, ...] = ("us",), sources_dir: str | None = None, *,
               primary_hosts: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """Merge the named source lists. Order doesn't matter — classification checks the
    most restrictive category first, so a domain listed as excluded stays excluded even
    if another list also names it.

    A list that can't be read as exactly these keys, each a list of hosts, is refused with a
    ValueError naming it (`project.load()` reports it as a problem with the project). Read past,
    each of these fails open: a misspelled or repeated `legal_text` lists no host, or only some,
    and every undated statute on the rest passes; a host written without the list's `-` is read
    one letter at a time; and a host with a scheme, a path or `www.` matches no URL.

    `primary_hosts` are the hosts a project names as the issuing authorities for its own
    records, added to the lists' `primary_document` hosts. A command reads its rules through
    `Project.rules()`, never by passing the project's lists here alone: checked against the
    lists alone, every official record from a host they don't name reads as a copy."""
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
            # One test for a host, the one a project's `primary_hosts` passes (`bare_host()`),
            # so a list and a project can't disagree about what a host is.
            if bad := [h for h in hosts if not isinstance(h, str) or bare_host(h) is None]:
                raise ValueError(f"source list {p}: {k!r} holds what is not a bare host name: "
                                 f"{', '.join(map(repr, bad))} (write `example.gov`: no "
                                 f"scheme, path, port or `www.`, and not an IP address)")
            merged[k].extend(map(bare_host, hosts))
    # Read here, where they join the rules, not only in project.load(): one given any other way
    # (a Project built in code) would otherwise be compared as written, and match nothing.
    merged["primary_document"].extend(h for h in map(bare_host, primary_hosts) if h)
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


def bare_host(value: str) -> str | None:
    """`value` as a host `classify()` can match, trimmed and lowercased, or None when it is not
    one: a scheme, a path, a port or a leading `www.` (which `domain()` strips from every URL)
    would match nothing, and a single label would match a whole top-level domain. So would an
    IP address's tail (`0.1` matches every address ending in it): no top-level domain is all
    digits, so an entry whose last label is all digits is refused."""
    host = _ascii(value.strip().lower().rstrip("."))   # a trailing dot names the same host
    if not _HOST.fullmatch(host) or host.rsplit(".", 1)[-1].isdigit():
        return None
    return host


def _ascii(host: str) -> str:
    """`host` in its ASCII form, the one form list entries, a project's hosts and a URL's host
    are compared in. `domain()` gives a URL's host as the URL spells it, and an entry can be
    written either way, so compared as spelled a host matched only an entry spelled like it.
    UTS 46 without the transitional mappings, as browsers and httpx resolve a host: Python's own
    `idna` codec is IDNA 2003, which folds `ß` into `ss`, so `straße.example` would have matched
    `strasse.example`, a different registrant. A host the encoder refuses is compared as is."""
    try:
        return idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except (idna.IDNAError, UnicodeError):
        return host


def why_not_nameable(host: str | None, rules: dict[str, tuple[str, ...]]) -> str | None:
    """Why `host` (as `bare_host()` gives it) can't be named in a project's `primary_hosts`
    under `rules`, or None when it can. The one rule for it: `project.load()` refuses an entry
    by it, and check-claim and the review page offer `primary_hosts` only where it allows, so
    none of them tells a person to add a host the project file would refuse.

    Classification takes the most restrictive class first, so an excluded or lead-generator host
    named there would stay what it is while every brief called it an issuing authority. And a
    news outlet would stop being one, named itself or through a host above it (primary_document
    is checked before journalism): its copy of a record would pass as the record."""
    if host is None:
        return ("it is no host name a project can list (an IP address, a single label, or a "
                "character no host name holds)")
    if (cls := classify(f"https://{host}/", rules)) not in ("unknown", "primary_document"):
        return f"the project's source lists class it as {cls}"
    if outlets := [j for j in rules.get("bylined_journalism", ()) if j.endswith("." + host)]:
        return f"it covers {', '.join(map(repr, outlets))}, a news outlet on the project's " \
               f"source lists"
    return None


def host_key(url: str) -> str:
    """`url`'s host as hosts are compared: `domain()`'s, in the ASCII form list entries are
    stored in (`_ascii()`). Everything that asks whether two hosts are one uses it, the
    corroboration check's "different outlets" included, so `bücher.example` and its `xn--`
    spelling are one host there as they are to `classify()`. `domain()` stays the form shown."""
    return _ascii(domain(url))


def display_host(host: str) -> str:
    """`host` as a URL would spell it for a reader: the Unicode form of an internationalized
    host, which is stored in its ASCII form (`_ascii()`). The brief lists hosts this way, so a
    researcher on a page whose address bar reads `bücher.example` sees it named."""
    try:
        return idna.decode(host)
    except (idna.IDNAError, UnicodeError):
        return host


def default_rules() -> dict[str, tuple[str, ...]]:
    """`us` alone: the lists for a caller that has no project. Every command passes the lists
    its project names (`sources = ["us", "ca"]` in provenance.toml), so a California project
    sees CalMatters and LegInfo while a project elsewhere doesn't inherit them. This used to
    read a file kept in the tool itself, which a command checking another project's citations
    could silently disagree with."""
    return load_rules(("us",))


def domain(url: str) -> str:
    # A trailing dot names the same host (`example.gov.` is fully qualified `example.gov`), and
    # left on, it matched no entry: an excluded host's page passed as unlisted.
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, entries: tuple[str, ...]) -> bool:
    return any(host == e or host.endswith("." + e) for e in entries)


def classify(url: str, rules: dict[str, tuple[str, ...]] | None = None) -> str:
    """One of: bylined_journalism, primary_document, lead_generator_only,
    excluded, campaign_statement_only, unknown."""
    r = rules if rules is not None else default_rules()
    host = host_key(url)
    for key in CATEGORIES:  # most restrictive first
        if _matches(host, r.get(key, ())):
            return key
    return "unknown"


def publishes_legal_text(url: str, rules: dict[str, tuple[str, ...]] | None = None) -> bool:
    """True when `url` is on a host a loaded list names under `legal_text`."""
    r = rules if rules is not None else default_rules()
    return _matches(host_key(url), r.get(LEGAL_TEXT, ()))


# Bylines that name nobody. Refused as an author, and never taken as naming who argues something.
NOT_A_NAME = frozenset({"staff", "unknown", "n/a", "none", "editorial board"})


def names_nobody(name: str) -> bool:
    """Whether `name` is one of `NOT_A_NAME`, with or without a leading "The" ("The Editorial
    Board" is the usual byline). Compared folded as a question is, since a fullwidth "Staff" or
    an "Editorial Board" spaced with a no-break space prints the same, and compared by
    `.lower()` alone it passed as a name."""
    folded = normalize(unicodedata.normalize("NFC", name))[0]
    return (folded[4:] if folded.startswith("the ") else folded) in NOT_A_NAME


def check_source_class(src: Source, rules: dict[str, tuple[str, ...]] | None = None) -> tuple[bool, str | None]:
    """(ok, reason). Unknown domains are allowed but must carry a named or institutional
    author — the rule is 'human-written', not 'on our list'. That keeps a good local
    paper or an agency we haven't listed from being rejected out of hand. Allowed is not
    trusted, though: what an unlisted host's citation can carry is `tier()`'s question."""
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
    if names_nobody(src.author):
        return False, f"author {src.author!r} is not a named or institutional author-of-record"
    return True, None


# --- Tiers -------------------------------------------------------------------------------------
# What a citation can carry. The tier is the citation's own, from its source_type, never its
# host's: one news site runs reporting and op-eds, and one article holds reported fact beside
# its writer's opinion. The host can only lower a tier, never raise one. A citation labeled
# reporting is reporting only on a host a source list names as a news outlet: anywhere else
# nothing but the label says it is journalism, and an advocacy site with a byline passed as a
# newspaper. See CLAUDE.md, "A citation's tier is its own, and the host can only lower it".
TIER_OF = {
    "primary_document": "primary_text",
    "official_record": "primary_text",
    "official_analysis": "official_analysis",
    "bylined_journalism": "reporting",
    "opinion": "opinion",
    "advocacy": "advocacy",
    # Each already limited to its own claim form, "the campaign says X" or "the organization
    # says X", by the host lists and the researcher's rules. Unchanged here.
    "campaign_statement": "campaign_statement",
    "own_statement": "own_statement",
}
# Tiers citable only as what their author argues, "X argues Y", never for a bare fact: a claim
# resting on one names whose argument it is (`attributes()`), and corroboration counts them all
# as one document. `unlisted_outlet` is a citation labeled reporting on a host no source list
# names as a news outlet.
ARGUED = frozenset({"opinion", "advocacy", "unlisted_outlet"})
TIER_LABEL = {
    "primary_text": "primary text",
    "official_analysis": "official analysis",
    "reporting": "reporting",
    "unlisted_outlet": "unlisted outlet",
    "opinion": "opinion",
    "advocacy": "advocacy",
    "campaign_statement": "campaign statement",
    "own_statement": "own statement",
}


def tier(src: Source, rules: dict[str, tuple[str, ...]]) -> str:
    """The tier `src` carries: its source_type's, lowered to `unlisted_outlet` for reporting on a
    host the lists don't name as a news outlet. `rules` is required: defaulted to `us`, every
    regional outlet would read as unlisted."""
    t = TIER_OF[src.source_type]
    if t == "reporting" and classify(src.url, rules) != "bylined_journalism":
        return "unlisted_outlet"
    return t


def _folded(text: str) -> str:
    # Composed first: normalize() folds one character at a time, so a decomposed accent would
    # never meet the composed one it prints as (the same reason `questions` composes). Not
    # casefolded, unlike a question: a name is told from a word by its capitals, and case-blind,
    # a publisher called "The Record" was named by "the record shows".
    return normalize(unicodedata.normalize("NFC", text), casefold=False)[0]


def speakers(src: Source) -> list[str]:
    """What an answer can name `src`'s arguer by: its author or its publisher, the publisher with
    or without a leading "The" where that leaves two words or more. Whole names only: a surname
    alone also names everyone else who has it, and one word left of "The Record" is a word that
    starts sentences ("Record turnout shows ..."). The check tells the researcher exactly which
    names it takes. A byline that names nobody (`names_nobody()`) is not one, and nor is one of
    fewer than two letters ("-", "A"): it matched any answer holding that mark or letter, so a
    bare fact read as attributed (CLAUDE.md: "No name means no words, not an empty string").
    Letters are counted over the whole name, so one written as single characters with spaces
    between still counts."""
    names = [src.author.strip(), src.publisher.strip()]
    if re.match(r"(?i)the\s", names[1]) and len(rest := names[1][4:].split()) >= 2:
        names.append(" ".join(rest))
    return [n for n in dict.fromkeys(names)
            if len(re.findall(r"[^\W\d_]", _folded(n))) >= 2 and not names_nobody(n)]


def attributes(answer: str, src: Source) -> bool:
    """Whether `answer` names who argues what `src` says: its author or its publisher, as whole
    words, as written. Whitespace, quote and dash styles are folded as a question's are, but case
    is not (`_folded()`).

    The mechanical half of "X argues Y": it shows the claim says whose argument this is. Whether
    the claim then states the argument as the arguer's, or as established fact, is the verifier's
    to judge (verifier.md)."""
    said = _folded(answer)
    return any(re.search(rf"(?<!\w){re.escape(_folded(n))}(?!\w)", said) for n in speakers(src))
