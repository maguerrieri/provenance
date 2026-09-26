"""Source access registry: how to actually query a site whose UI is a JS app.

The pattern this exists for: a researcher cannot reach an authoritative index, quietly
cites something else, and every downstream check passes because the substitute is a real
document. Detecting the substitution (`verify.secondary_host`) is half the answer. The other
half is making the source reachable — and then *writing that down*, so the next researcher
inherits the finding instead of rediscovering it or giving up.

A negative result is worth recording too: "probed, needs a session, retrieve by hand" saves
the next run from re-litigating it and from substituting silently.

A credential never enters the registry, and nothing here prints one. That is two checks, one
each way, and every path goes through them rather than a check of its own:
- **In:** `check_entry()`. `save()` is the only write under `sources/access/` and
  `dump_entry()` the only YAML an entry becomes, and both call it. `load_all()` calls it on
  every file too, since a person with an editor is a writer as well.
- **Out:** `redact()`. Every exception this module raises is a `Refused`, whose message has been
  through it, and `_refusing` makes one of a library's error at every function the CLI calls.
"""

from __future__ import annotations

import functools
import html
import json
import os
import re
import shlex
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from typing import NoReturn
from urllib.parse import SplitResult, unquote, unquote_plus, urlsplit, urlunsplit

import httpx
import yaml

# Package data, so an installed copy (uv tool install) has it. Only a checkout writes to it:
# see installed_copy().
REGISTRY = Path(files(__package__) / "source_access")


def installed_copy() -> bool:
    """Whether this provenance is an installed copy, not a checkout of its repo.

    The registry ships inside the package. In a checkout (an editable install, which `uv sync`
    makes) an entry written there is a file in the repo, committed like any other, and that is
    how a finding outlives the session that made it. In an installed copy (`uv tool install`) it
    is a file in the tool's own environment, which the next install replaces without a word. So
    `source-note` and `source-import-curl` write only in a checkout. The install records which
    it is (PEP 610's direct_url.json). Anything it can't read counts as installed: refusing a
    write costs a paste, and a write that is silently lost costs the finding.
    """
    try:
        raw = distribution(__package__).read_text("direct_url.json")
        info = json.loads(raw) if raw else {}
    except (PackageNotFoundError, OSError, ValueError):
        return True
    dir_info = info.get("dir_info") if isinstance(info, dict) else None
    return not (isinstance(dir_info, dict) and dir_info.get("editable") is True)

# Never persisted, never replayed. An endpoint that only works with your session is a manual
# retrieval, not a pipeline capability, and recording the credential would be both a leak and
# a lie about what the pipeline can do.
#
# Matched by pattern, not by a list of names: a session travels as cookie and authorization,
# but also as x-csrf-token, x-xsrf-token, x-session-id, phpsessid,
# __requestverificationtoken, x-goog-authuser, api_key, ocp-apim-subscription-key, and
# whatever else a site calls it. `key` is matched as the end of a word, so it catches every
# `*-key` and `*_keys` without reading `keyword` as one; short words (sid, otp, pin) only
# whole. No pattern is complete, which is why an import also keeps only SAFE_HEADERS.
_CREDENTIAL_HEADER = re.compile(
    r"auth|token|csrf|xsrf|sess|secret|pass|cred|cookie|bearer|jwt|signature|hmac"
    r"|keys?(?:$|[-_])|(?:^|[-_])(?:sid|otp|pin)(?:$|[-_])")

# What an import keeps. A deny pattern can't anticipate every name a session header goes by,
# so an import keeps only headers that describe the request rather than the person, and
# drops and names the rest. A human can add one an endpoint needs to the entry after looking
# at it; `run()` still refuses it if it looks like a credential.
SAFE_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control", "content-type", "dnt",
    "origin", "pragma", "priority", "referer", "sec-ch-ua", "sec-ch-ua-mobile",
    "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-fetch-user", "sec-gpc", "te", "upgrade-insecure-requests", "user-agent",
    "x-requested-with",
})
# Safe names whose value is a URL, which can carry a login like any other.
_URL_HEADERS = frozenset({"origin", "referer"})


# An HTTP header name (RFC 9110's token). Anything else in a name slot, such as
# `Authorization Bearer abc` with its colon lost, is a value that has lost its separator.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def credential_header(name: str) -> bool:
    return bool(_CREDENTIAL_HEADER.search(name.strip().lower()))


class Refused(ValueError):
    """Why the registry code won't do something. Its message has been through `redact()`, so a
    refusal can be printed whatever it quotes. Every exception this module raises is one (a test
    fails on any other), and `_refusing` makes one of a library's error."""

    def __init__(self, message: str):
        super().__init__(redact(message))


def _refusing(fn):
    """`fn`, with every exception that leaves it a `Refused`. A library's error can quote what it
    was handed: `urlsplit()` quoted a whole netloc, login included (#158), httpx quotes a header
    value it can't send, and PyYAML a snippet of the file around its error. Every function the
    CLI calls carries this, and a test fails on one that doesn't."""
    @functools.wraps(fn)
    def refusing(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001
            raise Refused(f"{type(e).__name__}: {e}") from None
    return refusing


def _split(url: str, where: str) -> SplitResult:
    """`urlsplit(url)` for a URL from a paste or a recipe, refused without repeating it.

    The stdlib's error for a host it can't read quotes the whole netloc, login included: a
    fullwidth `@` (U+FF20) in a password reads as `@` once normalized, and the refusal of
    `user:pass<U+FF20>x@host` printed all of it (#158). `where` names the URL instead ("the
    referer header's URL"). Every split of such a URL is made here, and the `try` holds the
    split alone: what a caller reads from the parts raises refusals of its own."""
    try:
        return urlsplit(url)
    except ValueError:
        raise Refused(f"{where} can't be parsed, so it can't be checked for a "
                      "credential") from None


_BACKSLASH_ESCAPE = re.compile(
    r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|U([0-9a-fA-F]{8})|([/\\\"']))")


def _unbackslash(text: str) -> str:
    def one(m: re.Match) -> str:
        if code := m.group(1) or m.group(2) or m.group(3):
            return chr(n) if (n := int(code, 16)) <= 0x10FFFF else m.group(0)
        return m.group(4)
    return _BACKSLASH_ESCAPE.sub(one, text)


def _readings(text: str) -> list[str]:
    """`text`, then `text` as each round of decoding reads it: NFKC (a fullwidth `＠` is an `@`
    to `urlsplit()`), percent-encoding (`%40`, and `%2540` in a second round), HTML entities and
    backslash escapes (`\\u0040`, JSON's `\\/`). Every check for a login looks at every reading,
    so none hides behind an encoding some reader decodes and the check doesn't."""
    out = [text]
    for _ in range(4):
        t = _unbackslash(html.unescape(unquote(unicodedata.normalize("NFKC", out[-1]))))
        if t == out[-1]:
            break
        out.append(t)
    return out


# Where a URL's authority starts, and what it runs to: an `@` in it follows a login.
_AUTHORITY = re.compile(r"//([^/?#\s\"'<>`\\]*)")
# `name:secret@host` with no scheme: a host argument, a proxy setting, curl's `-u` value pasted
# whole. The colon is what tells it from an email address, and `mailto:` is one.
_BARE_LOGIN = re.compile(r"(?<![^\s\"'<>`(=,;&?])(?!mailto:)[^\s/?#@\"'<>`:]+:"
                         r"[^\s/?#@\"'<>`]*@[^\s/?#@\"'<>`]", re.IGNORECASE)


def _login_in(text: str) -> bool:
    """Whether a URL or host anywhere in `text`, in any reading of it, carries a username or
    password: an `@` in an authority (`https://user:x@host/`, `//user@host`), or a bare
    `name:secret@host`. It fails toward refusing: a URL nested in a parameter, a body field or a
    note is a login wherever it sits and whatever the field is called. An `@` in a path
    (`https://host/@user`) or in an email address is not one."""
    readings = _readings(text)
    return (any("@" in m.group(1) for r in readings for m in _AUTHORITY.finditer(r))
            or any(_BARE_LOGIN.search(r) for r in readings))


def _has_login(url: str, where: str) -> bool:
    """A username or password in the URL itself, basic auth by another route: an `@` in its
    netloc, in any reading. `%40` is a port to httpx and an `@` to a person."""
    return any("@" in r for r in _readings(_split(url, where).netloc))


# Parameter names need a rule of their own. In a header name, `sess`, `auth`, `pass` and `pin`
# anywhere are credentials. In a parameter name they are usually something else:
# `?session=2025-2026` and `?sess=CUR` are legislative sessions, `author` and `authority` are
# people and bodies, `passed` is a bill's status, `pin` is a parcel and `keys` is a site
# search box. So a name is split into words (`apiKey`, `api_key` and `X-Api-Key` all hold the
# word `key`), and only words with no ordinary public-records meaning count:
# - these whole words;
# - these parts inside a word, for names with no case or separator to split on
#   (`csrfmiddlewaretoken`, `PHPSESSID`, `_wpnonce`), including `key` at the end of a few
#   compounds (`apikey`, `sesskey`), so `turkey` is not a key;
# - those compounds split in two (`api_keys`), and `session` followed by `id` or `ids`.
# Left out on purpose: `session`, `sess`, `keys`, `pin`, `ticket` (a citation number) and
# `sign`. No list of names is complete: a credential under any other name gets through.
_CREDENTIAL_PARAM_WORD = re.compile(
    r"o?auth|authori[sz]ation|authentication|authcode|pass|pwd?|pswd|passcode|passphrase"
    r"|creds?|key\d*|sig|cookies?|sid|otp|saml|appid")
_KEY_COMPOUND = (r"(?:api|access|app|auth|client|consumer|dev|developer|licen[cs]e|master"
                 r"|private|sess|session|signing|subscription|user)")
_CREDENTIAL_PARAM_PART = re.compile(
    r"token|csrf|xsrf|passw|secret(?!ar)|sessid|sessionid|credential|bearer|jwt|hmac|signature"
    rf"|nonce$|{_KEY_COMPOUND}keys?\d*$")
_CREDENTIAL_PARAM_PAIR = re.compile(rf"{_KEY_COMPOUND} keys?\d*|session ids?")

# A name slot can hold a value (a JSON map keyed by session id, a pair that lost its `=`), so
# a message repeats a flagged name only if it reads like one a person wrote: short, made of
# these characters, each word letters with at most one trailing digit, not a run of short
# words (what a mixed-case token splits into) and not a long run of hex. Anything else is
# described instead. A value made of ordinary lowercase letters still reads as a name.
_PARAM_NAME = re.compile(r"[\w.\-\[\]$:~*@]{1,40}")


def _param_words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)    # apiKey
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)  # APIKey
    return re.findall(r"[a-z0-9]+", name.lower())


def credential_param(name: str) -> bool:
    """A query parameter, path parameter or body field whose name says it is a credential."""
    words = _param_words(name)
    return (any(_CREDENTIAL_PARAM_WORD.fullmatch(w) or _CREDENTIAL_PARAM_PART.search(w)
                for w in words)
            or any(_CREDENTIAL_PARAM_PAIR.fullmatch(f"{a} {b}") for a, b in pairwise(words)))


def _json_container(text: str) -> dict | list | None:
    # RecursionError is not caught: JSON nested too deeply to read is refused, not skipped.
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict | list) else None


def _json_names(value) -> list[str]:
    """Every key in a JSON value, at any depth, and the names in any of its strings."""
    if isinstance(value, str):
        return list(_value_names(value))
    if isinstance(value, dict):
        return [n for k, v in value.items() for n in (k, *_json_names(v))]
    if isinstance(value, list):
        return [n for v in value for n in _json_names(v)]
    return []


@lru_cache(maxsize=4096)
def _value_names(value: str) -> tuple[str, ...]:
    """The names a parameter's value holds: the keys of JSON, the parameters of a URL (a
    `next` or `callback` link carries a query of its own, relative or not), or form pairs.

    Cached, and cleared after each request, and `_pair_names` keeps each name once: every pair
    is read two ways, so without both a URL nested in a URL was read twice at every level, and
    its names listed twice, and a 130-character paste ran for minutes.

    Every value is checked for a login here too, and every value a request holds passes here:
    each query, fragment, `;` and form value, and each JSON string at any depth. A nested URL
    was read for its parameter names and never for its netloc, so `?next=https://user:x@host/`
    imported with the login in it (#163)."""
    if _login_in(value):
        raise Refused("a URL in a parameter carries a username or password")
    if (fields := _json_container(value)) is not None:
        return tuple(_json_names(fields))
    where = "a URL in a parameter"
    if ("?" in value or "#" in value or value.startswith("/")
            or _split(value, where).scheme in ("http", "https")):
        return tuple(_url_param_names(value, where))
    # Pairs only with an `&`: a lone `name=value` is too often base64 with its padding.
    if "&" in value and "=" in value:
        return tuple(_pair_names(value))
    return ()


def _pair_names(text: str) -> list[str]:
    """The names in a query string or form body, and the names in their values.

    Read twice, split on `&` alone and on `&` and `;`, since servers differ: splitting on `;`
    alone would cut up a JSON value holding one. A segment with no `=` is a flag or a bare
    value, not a named parameter."""
    names: dict[str, None] = {}  # ordered and unique: both readings mostly find the same names
    for separators in ("[&]", "[&;]"):
        for segment in re.split(separators, text):
            name, eq, value = segment.partition("=")
            if eq:
                names |= dict.fromkeys((unquote_plus(name), *_value_names(unquote_plus(value))))
    return list(names)


def _url_param_names(url: str, where: str) -> list[str]:
    """The query, the fragment, and the `;` parameters of each path segment."""
    parts = _split(url, where)
    names = _pair_names(parts.query) + _pair_names(parts.fragment)
    for segment in parts.path.split("/"):
        names += _pair_names(segment.partition(";")[2])
    return names


def _body_param_names(body: str, content_type: str) -> list[str]:
    """The field names of a body, read every way the server might: as JSON if it parses, and
    as a form unless a content type says otherwise (curl sends `-d` as a form by default).
    A body that is neither can't be checked, so it is refused: a multipart form carries its
    CSRF token in a part this can't read.

    Only the parse is in the `try`, and the reading is in its `else`. Reading a parsed body
    raises refusals of its own, as `ValueError` too (a value holding a URL that can't be
    parsed), and caught with the parse failure each one read as "not JSON" and dropped the
    names already read: a body holding `api_key` beside such a URL imported (#155)."""
    names = None
    try:
        parsed = json.loads(body)
    except ValueError:
        pass
    else:
        names = _json_names(parsed)
    if content_type.partition(";")[0].strip().lower() in ("", "application/x-www-form-urlencoded"):
        names = (names or []) + _pair_names(body)
    if names is None:
        raise Refused("the body is neither JSON nor form-encoded, so its field names can't "
                      "be checked for a credential")
    return names


def _shown(name: str) -> bool:
    words = _param_words(name)
    return (bool(_PARAM_NAME.fullmatch(name))
            and all(re.fullmatch(r"[a-z]{1,24}\d?", w) and not re.fullmatch(r"[a-f]{8,}", w)
                    for w in words)
            and sum(len(w) <= 2 for w in words) <= 2)


def _names_shown(names) -> str:
    """Names for a message: each that reads like a name a person wrote, and a description in
    place of any that may hold a value."""
    names = set(names)
    shown = sorted(n for n in names if _shown(n))
    if len(shown) < len(names):
        shown.append("a name that may hold a value")
    return ", ".join(shown)


def _credential_names(names: list[str]) -> str:
    """The names that look like credentials, for a message, or "" if none does."""
    bad = {n for n in names if credential_param(n)}
    return _names_shown(bad) if bad else ""


def _credential_params(url: str, headers: dict[str, str], body: str | None) -> list[str]:
    """Where a request carries parameters that look like credentials, one message fragment per
    place ("its URL (api_key)"), or none. The URL is read, and so are the URLs in an `origin`
    or `referer` header, and the body.

    Refused and named, never dropped: unlike a header, a parameter is part of what the request
    asks, so a recipe without it can run and answer a different question. Raises `Refused` for
    a body or value that can't be read, and for a login in any value or name it reads."""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    try:
        found = [("its URL", _url_param_names(url, "its URL"))]
        found += [(f"its {k.lower()} header",
                   _url_param_names(v, f"its {k.lower()} header's URL"))
                  for k, v in headers.items() if k.lower() in _URL_HEADERS]
        if body:
            found.append(("its body", _body_param_names(body, content_type)))
    except RecursionError:
        raise Refused("a parameter nests too deeply to be checked for a credential") from None
    finally:
        _value_names.cache_clear()
    if any(_login_in(n) for _, names in found for n in names):
        raise Refused("a parameter's name carries a username or password")
    return [f"{where} ({shown})" for where, names in found if (shown := _credential_names(names))]


def _check_request(url: str, headers: dict[str, str], body: str | None) -> None:
    """The check a request passes wherever it is recorded or sent: `check_entry()` for every
    recipe, `run()` after filling one, and `parse_curl()` for the request a paste becomes.
    Refuses credential headers, a login in the URL or in an `origin` or `referer` header,
    and, through `_credential_params()`, credential parameters and a login anywhere in them.
    Names what it found, never a value."""
    if bad := [k for k in headers if credential_header(k)]:
        raise Refused(f"it carries credential headers ({_names_shown(bad)})")
    for k, v in headers.items():
        if k.lower() in _URL_HEADERS and _has_login(v, f"its {k.lower()} header's URL"):
            raise Refused(f"its {k.lower()} header's URL carries a username or password")
    if _has_login(url, "its URL"):
        raise Refused("its URL carries a username or password")
    if found := _credential_params(url, headers, body):
        raise Refused(f"it carries what look like credentials in {'; '.join(found)}")


_REDACTED = "[redacted]"
# A name and its value in a message: a header line or a YAML or JSON field (`Authorization:
# Bearer x`, `"token": "x"`), whose value runs to its closing quote or the end of the line.
_COLON_PAIR = re.compile(r"""(?P<name>[A-Za-z_$][\w.\-\[\]$]*)["']?[ \t]*:[ \t]*"""
                         r"""(?P<value>"[^"\n]*"|'[^'\n]*'|[^"'\s][^"'\n]*)""")
# ... and a query or form pair (`api_key=x`), whose value runs to the next separator.
_EQUALS_PAIR = re.compile(r"""(?P<name>[^\s&;?#=/"'<>`]+)=(?P<value>[^\s&;#"'<>`]+)""")
_AUTH_SCHEME = re.compile(r"\b(Bearer|Basic|Digest|Negotiate)[ \t]+[^\s\"',;]+", re.IGNORECASE)
_TOKEN_EDGE = re.compile(r"(\s+|[\"'`<>])")
# `KeyError: 'x'` names an exception, not a field.
_EXCEPTION_NAME = re.compile(r"[A-Z]\w*(?:Error|Exception|Warning)")


def _named_credential(name: str) -> bool:
    return (not _EXCEPTION_NAME.fullmatch(name)
            and any(credential_param(r) or credential_header(r) for r in _readings(name)))


def redact(text: str) -> str:
    """`text` with whatever in it may be a credential replaced by `[redacted]`: a login in any
    URL or host, and the value of a header, parameter or field named like a credential. Each word
    is read in every decoding `_readings()` knows, so a login percent-encoded into a parameter
    is found too.

    The one redactor for messages: a `Refused` is made through it, and the CLI prints every
    refusal of an access command through it. It fails toward removing, as the checks going in
    fail toward refusing: a message that says too little costs a look at the file, and one that
    says too much prints the credential it refused. Names are left alone, since they say what to
    fix, and a message repeats only names that read like ones a person wrote."""
    def colon_pair(m: re.Match) -> str:
        if m.group("value").startswith(_REDACTED) or not _named_credential(m.group("name")):
            return m.group(0)
        return m.group(0)[:m.start("value") - m.start()] + _REDACTED

    def word(w: str) -> str:
        if not w or _TOKEN_EDGE.fullmatch(w) or w == _REDACTED:
            return w
        if _login_in(w) or any(credential_param(unquote_plus(p.group("name")))
                               for r in _readings(w) for p in _EQUALS_PAIR.finditer(r)):
            return _REDACTED
        return w

    text = _COLON_PAIR.sub(colon_pair, text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)
    return "".join(word(w) for w in _TOKEN_EDGE.split(text))


def _page_only(url: str, where: str) -> str:
    """A URL without its query, fragment, or `;` parameters in any path segment
    (`/app;jsessionid=…/search`), which is where a session id rides in a page's URL."""
    parts = _split(url, where)
    return urlunsplit((parts.scheme, parts.netloc, re.sub(r";[^/]*", "", parts.path), "", ""))


@dataclass
class Recipe:
    id: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    params: list[str] = field(default_factory=list)
    summary: str = ""
    notes: str = ""


@dataclass
class SourceAccess:
    host: str
    name: str = ""
    access: str = "unknown"        # api | manual | unknown
    ui_url: str = ""
    naive_fetch: str = ""
    limits: str = ""
    verified: str = ""
    manual_steps: str = ""
    recipes: list[Recipe] = field(default_factory=list)

    def recipe(self, rid: str | None = None) -> Recipe | None:
        if not self.recipes:
            return None
        if rid is None:
            return self.recipes[0]
        return next((r for r in self.recipes if r.id == rid), None)


def _norm_host(host_or_url: str) -> str:
    """The host a command's host argument names, or a URL's. Refused if it holds a login, with
    or without a scheme: `user:x@host` has no `://` for `.hostname` to drop it from, and
    `provenance source-note` wrote it into the registry's file name and `host:` field (#161)."""
    h = host_or_url.strip()
    authority = _split(h, "the host").netloc if "://" in h else re.split(r"[/?#]", h, 1)[0]
    if any("@" in r for r in _readings(authority)):
        raise Refused("the host holds a username or password; name the host alone")
    if "://" in h:
        h = _split(h, "the host").hostname or h
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


# A host name, which is also a registry file's name: labels of letters, digits, `-` and `_`.
# Nothing else is one, so none can name a path out of the registry, or a file with a control
# character in its name.
_HOST = re.compile(r"[\w-]+(?:\.[\w-]+)*")


def _registry_host(host_or_url: str) -> str:
    h = _norm_host(host_or_url)
    if not _HOST.fullmatch(h):
        raise Refused("the host is not a host name, so it can't name a registry entry")
    return h


def _field(path: str, name) -> str:
    """A field's path for a message (`recipes.notes`): each name if it reads like one a person
    wrote, else a stand-in, since a name can hold a value."""
    name = str(name) if _shown(str(name)) else "<a field>"
    return f"{path}.{name}" if path else name


def _check_text(text: str, where: str) -> None:
    """Prose is read for the URLs in it: any login, and the parameters of any URL a reader
    would open (one with a scheme, or `//`). A relative link is not read for parameters,
    since documentation names a parameter that way (`/DownloadPdf?key=<hex>`)."""
    if _login_in(text):
        raise Refused(f"{where} holds a username or password in a URL or host")
    for url in re.findall(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s\"'<>`]+", text):
        if found := _credential_params(url, {}, None):
            raise Refused(f"{where} holds a URL carrying what look like credentials in "
                          f"{'; '.join(found)}")


def _check_fields(value, path: str = "") -> None:
    """Every field of an entry, at any depth: a name like a credential's, and every string."""
    if isinstance(value, dict):
        for k, v in value.items():
            if credential_param(str(k)) or _login_in(str(k)):
                raise Refused(f"it has a field named like a credential "
                              f"({_names_shown([str(k)])})")
            _check_fields(v, _field(path, k))
    elif isinstance(value, list):
        for v in value:
            _check_fields(v, path)
    elif isinstance(value, str):
        _check_text(value, f"its field {path}")


@_refusing
def check_entry(entry, host: str | None = None) -> None:
    """The one check every registry entry passes, whatever wrote it. `save()`, `dump_entry()` and
    `load_all()` call it, so an entry a command writes, one printed to be pasted into a file,
    and one a person edited by hand are all held to it. Refuses, naming where and never what:
    - a host that holds a login, or is not a host name: `host` (the file's), and its `host:`;
    - each recipe's request as `run()` would send it (`_check_request()`): credential headers,
      a login in its URL or its `origin` or `referer`, and credential parameters or a login in
      its URL, those headers and its body, a URL nested in any of them included;
    - a recipe param named like a credential, since a recipe that asks for one at run time is
      a manual retrieval;
    - a field named like a credential, at any depth, and a login in any string, prose included.
    """
    if not isinstance(entry, dict):
        raise Refused("an entry is a mapping of fields")
    for h in ([host] if host is not None else []) + ([entry["host"]] if "host" in entry else []):
        _registry_host(str(h))
    recipes = entry.get("recipes") or []
    if not isinstance(recipes, list):
        raise Refused("its recipes are not a list")
    for n, r in enumerate(recipes, 1):
        if not isinstance(r, dict):
            raise Refused(f"its recipe {n} is not a mapping")
        rid = r.get("id")
        where = f"recipe {rid!r}" if isinstance(rid, str) and _shown(rid) else f"recipe {n}"
        headers = r.get("headers") or {}
        if not isinstance(headers, dict):
            raise Refused(f"{where}'s headers are not a mapping")
        body = r.get("body")
        try:
            _check_request(str(r.get("url") or ""), {str(k): str(v) for k, v in headers.items()},
                           None if body is None else str(body))
        except Refused as e:
            # Never `name: reason`: to the redactor that reads as a field and its value, and a
            # recipe called `token` would lose its reason.
            raise Refused(f"in {where}, {e}") from None
        params = r.get("params") or []
        if bad := [str(p) for p in (params if isinstance(params, list) else [params])
                   if credential_param(str(p))]:
            raise Refused(f"{where} asks for what look like credentials ({_names_shown(bad)})")
    _check_fields(entry)


@_refusing
def dump_entry(entry: dict, host: str | None = None) -> str:
    """An entry as the YAML the registry holds, checked first: the only way one becomes YAML,
    whether it is written or printed for a person to paste into a file."""
    check_entry(entry, host)
    return yaml.safe_dump(entry, sort_keys=False, allow_unicode=True, width=100)


@_refusing
def entry_path(host: str) -> Path:
    """Where the registry keeps `host`'s entry, for both commands that write it. The host is
    checked (`_registry_host()`): it names the file, and a `/`, `..` or, on Windows, a `\\`
    in it named a file outside the registry, to be read, printed and rewritten."""
    path = REGISTRY / f"{_registry_host(host)}.yaml"
    if path.parent != REGISTRY:
        raise Refused("the host is not a host name, so it can't name a registry entry")
    return path


@_refusing
def save(host: str, entry: dict) -> Path:
    """Write `entry` as the registry's entry for `host`: the only write under `sources/access/`,
    and a checked one (`dump_entry()`). Whole or not at all: a temp file, then a rename."""
    path = entry_path(host)
    text = dump_entry(entry, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def _read_entry(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise Refused("it is not a mapping of fields")
    return raw


@_refusing
def with_note(host: str, finding: str, *, access: str = "",
              verified: str = "") -> tuple[str, dict]:
    """`host` as the registry names it, and its entry with `finding` appended, or a stub with it
    if there is none. Nothing is written: `save()` writes it, checking the whole entry again,
    what was there already included, and in an installed copy the CLI prints it instead."""
    h = _registry_host(host)
    path = entry_path(h)
    data = (_read_entry(path) if path.exists()
            else {"host": h, "name": "", "access": access or "unknown"})
    if access:
        data["access"] = access
    if verified:
        data["verified"] = verified
    data["findings"] = ((data.get("findings") or "") + ("\n" if data.get("findings") else "")
                        + finding)
    return h, data


@_refusing
def load_all(registry: Path | None = None) -> dict[str, SourceAccess]:
    """Every entry in the registry, each checked as `save()` checks one: a file is also written
    by hand, and one holding a credential is refused, naming its host, before anything reads it."""
    d = registry or REGISTRY
    out: dict[str, SourceAccess] = {}
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        try:
            host = _registry_host(p.stem)
        except Refused as e:
            raise Refused(f"a file in the registry is not named for a host ({e}); rename it "
                          "to the host it records") from None
        try:
            raw = _read_entry(p)
            check_entry(raw, host)
            recipes = [Recipe(**{**r, "headers": r.get("headers") or {}})
                       for r in (raw.pop("recipes", None) or [])]
            raw.pop("host", None)
            out[host] = SourceAccess(host=host, recipes=recipes,
                                     **{k: v for k, v in raw.items()
                                        if k in SourceAccess.__dataclass_fields__})
        except Exception as e:  # noqa: BLE001
            why = str(e) if isinstance(e, Refused) else f"{type(e).__name__}: {e}"
            raise Refused(f"the registry's entry for {host} can't be used, {why}") from None
    return out


@_refusing
def find(host_or_url: str, registry: Path | None = None) -> SourceAccess | None:
    """Registry entry for a host, matching parent domains too."""
    host = _norm_host(host_or_url)
    entries = load_all(registry)
    if host in entries:
        return entries[host]
    for h, entry in entries.items():
        if host.endswith("." + h) or h.endswith("." + host):
            return entry
    return None


_MANUAL_RECIPE = "record it as access: manual instead"


@_refusing
def run(recipe: Recipe, params: dict[str, str], *, timeout: float = 45.0) -> httpx.Response:
    """Execute a recipe. Credential headers and parameters are refused, not stripped: a
    recipe that needs one is describing a manual retrieval and should be recorded as such.
    Checked with `_check_request()`, as `check_entry()` checks it, before filling and again
    after: a param can put a login in the host, or a whole `name=value` pair in the query."""
    def checked(url: str, body: str | None) -> None:
        try:
            _check_request(url, recipe.headers, body)
        except Refused as e:
            raise Refused(f"in recipe {recipe.id!r}, {e}; {_MANUAL_RECIPE}") from None

    checked(recipe.url, recipe.body)
    missing = [p for p in recipe.params if p not in params]
    if missing:
        raise Refused(f"recipe {recipe.id!r} needs {', '.join(missing)}")
    # Substitute only the declared params. str.format() would choke on the JSON braces in
    # a body — which is most of them, since these are XHR endpoints.
    def fill(text: str) -> str:
        for k in recipe.params:
            text = text.replace("{" + k + "}", params[k])
        return text

    url = fill(recipe.url)
    body = fill(recipe.body) if recipe.body else None
    checked(url, body)
    headers = {"user-agent": "Mozilla/5.0", **recipe.headers}
    try:
        return httpx.request(recipe.method.upper(), url, timeout=timeout, follow_redirects=True,
                             headers=headers,
                             content=body.encode() if body else None)
    except httpx.InvalidURL:
        # httpx reads the URL again, and its error quotes the part it can't read: a port it
        # can't read, and `Invalid port: '...'` printed it.
        raise Refused(f"in recipe {recipe.id!r}, its URL can't be sent as it is; "
                      f"{_MANUAL_RECIPE}") from None
    except httpx.LocalProtocolError:
        # And a header value it can't send is quoted whole, whatever the header is called.
        raise Refused(f"in recipe {recipe.id!r}, a header can't be sent as it is; "
                      f"{_MANUAL_RECIPE}") from None


# curl's options, by what an import does with them. Every option's arity has to be known: one
# skipped without its value leaves the value behind as an argument, which is how
# `curl --user name:password URL` recorded `name:password` as the recipe's URL. An option not
# listed here is refused, never skipped.
_CURL_CREDENTIAL_OPTIONS = frozenset({
    "-u", "--user", "-U", "--proxy-user", "--oauth2-bearer", "-E", "--cert", "--key", "--pass",
    "--proxy-pass", "-n", "--netrc", "--netrc-file", "--netrc-optional", "--aws-sigv4",
})
_CURL_FLAGS = frozenset({  # no value, and nothing to record: how curl runs, not what it asks
    "--compressed", "-k", "--insecure", "-s", "--silent", "-S", "--show-error", "-L",
    "--location", "-i", "--include", "-v", "--verbose", "-g", "--globoff", "-f", "--fail",
    "--http1.0", "--http1.1", "--http2", "--http2-prior-knowledge", "--http3",
})
_CURL_VALUE_OPTIONS = frozenset({
    "-H", "--header", "-b", "--cookie", "-d", "--data", "--data-raw", "--data-binary",
    "--data-ascii", "-X", "--request", "--url", "-A", "--user-agent", "-e", "--referer",
    "-o", "--output", "-m", "--max-time", "--connect-timeout",
})


_MANUAL = ("An endpoint that needs one is a manual retrieval: record it with "
           "`provenance source-note <host> <finding> --access manual`")
_PASTE_REFUSED = ("Remove it from the paste if the request works without it. Otherwise record "
                  "the endpoint by hand, as a manual retrieval: "
                  "`provenance source-note <host> <finding> --access manual`")


def _refuse_option(option: str) -> NoReturn:
    # Name the option alone: its value may be the credential.
    name = option.partition("=")[0]
    if name in _CURL_CREDENTIAL_OPTIONS:
        raise Refused(f"curl {name} passes a credential. {_MANUAL}")
    raise Refused(f"unsupported curl option {name}: remove it if the request works "
                  "without it, or record the endpoint by hand")


def _curl_arguments(args: list[str]):
    """Yield `(option, value)` for each of curl's options (value None for a flag), and
    `(None, arg)` for each argument that is not one.

    Short options are read the way curl reads them: `-sSL` is three flags, and in `-XPOST`
    the rest of the word is the value. A long option is one word: curl has no
    `--option=value` form.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg.startswith("-") or arg == "-":
            yield None, arg
            continue
        # Each option in the word, with what follows it in the word.
        if arg.startswith("--"):
            word = [(arg, "")]
        else:
            word = [("-" + c, arg[k + 2:]) for k, c in enumerate(arg[1:])]
        for option, rest in word:
            if option in _CURL_FLAGS:
                yield option, None
            elif option not in _CURL_VALUE_OPTIONS:
                _refuse_option(option)
            elif rest:
                yield option, rest
                break
            elif i < len(args):
                yield option, args[i]
                i += 1
                break
            else:
                raise Refused(f"curl {option} needs a value")


@_refusing
def parse_curl(text: str) -> dict:
    """Turn a browser 'copy as cURL' into a registry entry, minus credentials.

    This is how these endpoints get discovered in practice — open dev tools, do the search,
    copy the request. The cookies and session headers in that paste are the human's live
    session: they are dropped here and never written to disk, along with any header not
    known to be safe, and the referer loses its query. A login passed any other way
    (`--user`, a username in the URL) means the endpoint needs one, so the import is refused.
    So does a URL parameter or body field named like a credential, and a body whose fields
    can't be read: dropping a parameter would change what the request asks.
    """
    text = re.sub(r"\\\s*\n", " ", text).strip()
    tokens = shlex.split(text)
    if not tokens or tokens[0] != "curl":
        raise Refused("not a curl command")

    urls: list[str] = []
    method, body = None, None
    headers: dict[str, str] = {}
    dropped: list[str] = []
    unknown: list[str] = []

    def checked(check, *args):
        """A check whose refusal says what to do instead."""
        try:
            return check(*args)
        except Refused as e:
            raise Refused(f"the pasted request: {e}. {_PASTE_REFUSED}") from None

    def header(name: str, value: str) -> None:
        name = name.strip().lower()
        if not _HEADER_NAME.fullmatch(name):
            # Not a name, so perhaps a value that lost its colon. Named nowhere.
            raise Refused("a curl header's name is not a header name; fix it or remove it")
        value = value.strip()
        if credential_header(name):
            dropped.append(name)
        elif name not in SAFE_HEADERS:
            unknown.append(name)
        elif name == "referer":
            # The URL of the page the request came from, and a session id can ride in that
            # page's query or `;jsessionid=` parameters. The page itself is what a site checks.
            headers[name] = checked(_page_only, value, "its referer header's URL")
        else:
            headers[name] = value

    for option, value in _curl_arguments(tokens[1:]):
        if option in (None, "--url"):
            urls.append(value)
        elif option in ("-H", "--header"):
            name, colon, v = value.partition(":")
            if not colon:
                # curl's `Name;` sends the header empty. Anything else without a colon is not
                # a header, and may be a value: refused without repeating it.
                if not value.rstrip().endswith(";"):
                    raise Refused("a curl -H has no colon; write it `Name: value`")
                name, v = value.rstrip()[:-1], ""
            elif not v.strip():
                continue  # curl's `Name:` removes the header rather than sending it empty
            header(name, v)
        elif option in ("-A", "--user-agent"):
            header("user-agent", value)
        elif option in ("-e", "--referer"):
            # `;auto` tells curl to update the referer on redirects; it is not part of it.
            if referer := value.removesuffix(";auto"):
                header("referer", referer)
        elif option in ("-b", "--cookie"):
            dropped.append("cookie")
        elif option in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            body = value
            method = method or "POST"
        elif option in ("-X", "--request"):
            method = value

    # A second argument is what a misread option's value looks like, so it is refused rather
    # than ignored. None of these messages repeats the URL: it may hold the credential.
    if not urls:
        raise Refused("no URL in curl command")
    if len(urls) > 1:
        raise Refused(f"{len(urls)} URLs in the curl command; import one request at a time")
    url = urls[0]
    parts = checked(_split, url, "its URL")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("the curl command's URL is not an absolute http(s) URL")
    # The check `run()` and `check_entry()` make, on the request as it will be recorded: the
    # headers left once credentials and unknown ones are dropped, and the referer's page.
    checked(_check_request, url, headers, body)
    host = checked(_registry_host, url)
    entry = {
        "host": host,
        "name": "",
        "access": "api",
        "ui_url": f"https://{host}/",
        "recipes": [{
            "id": "imported",
            "summary": "",
            "method": (method or "GET").upper(),
            "url": url,
            "headers": headers,
            **({"body": body} if body else {}),
            "params": [],
            "notes": "Imported from a browser request. Credentials, and headers not known to "
                     "be safe, were dropped — confirm it still works without them before "
                     "relying on it.",
        }],
    }
    checked(check_entry, entry)
    return {"entry": entry, "dropped_credentials": sorted(set(dropped)),
            "dropped_headers": sorted(set(unknown))}
