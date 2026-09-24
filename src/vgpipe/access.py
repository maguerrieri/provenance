"""Source access registry: how to actually query a site whose UI is a JS app.

The pattern this exists for: a researcher cannot reach an authoritative index, quietly
cites something else, and every downstream check passes because the substitute is a real
document. Detecting the substitution (`verify.secondary_host`) is half the answer. The other
half is making the source reachable — and then *writing that down*, so the next researcher
inherits the finding instead of rediscovering it or giving up.

A negative result is worth recording too: "probed, needs a session, retrieve by hand" saves
the next run from re-litigating it and from substituting silently.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from urllib.parse import unquote_plus, urlparse, urlsplit, urlunsplit

import httpx
import yaml

REGISTRY = Path(__file__).resolve().parents[2] / "sources" / "access"

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


def _has_login(url: str) -> bool:
    """A username or password in the URL itself: basic auth by another route."""
    return "@" in urlparse(url).netloc


# Parameter names need a rule of their own. The header pattern reads `sess` as a session, but
# in public-records APIs `?session=2025-2026` is a legislative session, and `auth` or `pass`
# anywhere in a name would take `author`, `authority` and `passed` with them. So a name is
# split into words (`apiKey`, `api_key` and `X-Api-Key` all hold the word `key`) and matched
# by word: these words whole, these parts anywhere in a word (for names with no case or
# separator to split on, like `csrfmiddlewaretoken` or `PHPSESSID`), and `session` followed by
# `id` or `ids`. Bare `session` is not a credential, and neither is `pin`, which is a parcel
# number. `key` is a whole word, or ends one of a few compounds, so `turkey` is not a key.
_CREDENTIAL_PARAM_WORDS = frozenset({
    "auth", "oauth", "authorization", "authcode", "authkey", "pass", "pw", "pwd", "passcode",
    "passphrase", "cred", "creds", "key", "keys", "sig", "cookie", "cookies", "sess", "sid",
    "otp",
})
_CREDENTIAL_PARAM_PART = re.compile(
    r"token|csrf|xsrf|passw|secret(?!ar)|sessid|sessionid|credential|bearer|jwt|hmac|signature"
    r"|(?:api|access|app|client|consumer|private|session|signing|subscription)keys?$")

# The characters of a parameter name. A name slot can hold a value (a JSON map keyed by
# session id, a pair that lost its `=`), so a message repeats only a name made of these whose
# words are short and not digit-laden, and describes any other.
_PARAM_NAME = re.compile(r"[\w.\-\[\]$:~*@]{1,64}")


def _param_words(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)    # apiKey
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)  # APIKey
    return re.findall(r"[a-z0-9]+", name.lower())


def credential_param(name: str) -> bool:
    """A query parameter, path parameter or body field whose name says it is a credential."""
    words = _param_words(name)
    return (any(w in _CREDENTIAL_PARAM_WORDS or _CREDENTIAL_PARAM_PART.search(w) for w in words)
            or any(pair in (("session", "id"), ("session", "ids")) for pair in pairwise(words)))


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
        return _value_names(value)
    if isinstance(value, dict):
        return [n for k, v in value.items() for n in (k, *_json_names(v))]
    if isinstance(value, list):
        return [n for v in value for n in _json_names(v)]
    return []


def _value_names(value: str) -> list[str]:
    """The names a parameter's value holds: the keys of JSON, or the parameters of a URL (a
    `next` or `callback` link carries a query of its own)."""
    if (fields := _json_container(value)) is not None:
        return _json_names(fields)
    try:
        if value.startswith("/") or urlsplit(value).scheme in ("http", "https"):
            return _url_param_names(value)
    except ValueError:
        raise ValueError("a parameter holds a URL that can't be parsed, so it can't be "
                         "checked for a credential") from None
    return []


def _pair_names(text: str) -> list[str]:
    """The names in a query string or form body, and the names in their values.

    Read twice, split on `&` alone and on `&` and `;`, since servers differ: splitting on `;`
    alone would cut up a JSON value holding one. A segment with no `=` is a flag or a bare
    value, not a named parameter."""
    names = []
    for separators in ("[&]", "[&;]"):
        for segment in re.split(separators, text):
            name, eq, value = segment.partition("=")
            if eq:
                names += [unquote_plus(name), *_value_names(unquote_plus(value))]
    return names


def _url_param_names(url: str) -> list[str]:
    """The query, the fragment, and the `;` parameters of each path segment."""
    parts = urlsplit(url)
    names = _pair_names(parts.query) + _pair_names(parts.fragment)
    for segment in parts.path.split("/"):
        names += _pair_names(segment.partition(";")[2])
    return names


def _body_param_names(body: str, content_type: str) -> list[str]:
    """The field names of a body, read every way the server might: as JSON if it parses, and
    as a form unless a content type says otherwise (curl sends `-d` as a form by default).
    A body that is neither can't be checked, so it is refused: a multipart form carries its
    CSRF token in a part this can't read."""
    names = None
    try:
        names = _json_names(json.loads(body))
    except ValueError:
        pass
    if content_type.partition(";")[0].strip().lower() in ("", "application/x-www-form-urlencoded"):
        names = (names or []) + _pair_names(body)
    if names is None:
        raise ValueError("the body is neither JSON nor form-encoded, so its field names can't "
                         "be checked for a credential")
    return names


def _shown(name: str) -> bool:
    return bool(_PARAM_NAME.fullmatch(name)) and not any(
        len(w) > 24 or (w.isalnum() and not w.isdigit() and sum(c.isdigit() for c in w) > 1)
        for w in _param_words(name))


def _credential_names(names: list[str]) -> str:
    """The names that look like credentials, for a message, or "" if none does."""
    bad = {n for n in names if credential_param(n)}
    shown = sorted(n for n in bad if _shown(n))
    if len(shown) < len(bad):
        shown.append("a name that may hold a value")
    return ", ".join(shown)


def _credential_params(url: str, headers: dict[str, str], body: str | None) -> list[str]:
    """Where a request carries parameters that look like credentials, one message fragment per
    place ("its URL (api_key)"), or none. The URL is read, and so are the URLs in an `origin`
    or `referer` header, and the body.

    Refused and named, never dropped: unlike a header, a parameter is part of what the request
    asks, so a recipe without it can run and answer a different question. Raises ValueError
    for a body or value that can't be read."""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    try:
        found = [("its URL", _url_param_names(url))]
        found += [(f"its {k.lower()} header", _url_param_names(v))
                  for k, v in headers.items() if k.lower() in _URL_HEADERS]
        if body:
            found.append(("its body", _body_param_names(body, content_type)))
    except RecursionError:
        raise ValueError("a parameter nests too deeply to be checked for a credential") from None
    return [f"{where} ({shown})" for where, names in found if (shown := _credential_names(names))]


def _page_only(url: str) -> str:
    """A URL without its query, fragment, or `;` parameters in any path segment
    (`/app;jsessionid=…/search`), which is where a session id rides in a page's URL."""
    parts = urlsplit(url)
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
    h = host_or_url.strip()
    if "://" in h:
        h = urlparse(h).hostname or h
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def load_all(registry: Path | None = None) -> dict[str, SourceAccess]:
    d = registry or REGISTRY
    out: dict[str, SourceAccess] = {}
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        raw = yaml.safe_load(p.read_text()) or {}
        recipes = [Recipe(**{**r, "headers": r.get("headers") or {}}) for r in (raw.pop("recipes", None) or [])]
        raw.pop("host", None)
        host = _norm_host(p.stem)
        out[host] = SourceAccess(host=host, recipes=recipes,
                                 **{k: v for k, v in raw.items()
                                    if k in SourceAccess.__dataclass_fields__})
    return out


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


def run(recipe: Recipe, params: dict[str, str], *, timeout: float = 45.0) -> httpx.Response:
    """Execute a recipe. Credential headers and parameters are refused, not stripped: a
    recipe that needs one is describing a manual retrieval and should be recorded as such."""
    bad = sorted(k for k in recipe.headers if credential_header(k))
    if bad:
        raise ValueError(f"recipe {recipe.id!r} carries credential headers ({', '.join(bad)}); "
                         "record it as access: manual instead")
    if any(_has_login(v) for k, v in recipe.headers.items() if k.lower() in _URL_HEADERS):
        raise ValueError(f"recipe {recipe.id!r} puts a username or password in a header's URL; "
                         "record it as access: manual instead")
    missing = [p for p in recipe.params if p not in params]
    if missing:
        raise ValueError(f"recipe {recipe.id!r} needs {', '.join(missing)}")
    # Substitute only the declared params. str.format() would choke on the JSON braces in
    # a body — which is most of them, since these are XHR endpoints.
    def fill(text: str) -> str:
        for k in recipe.params:
            text = text.replace("{" + k + "}", params[k])
        return text

    url = fill(recipe.url)
    # Checked after filling: a param can land in the host part too.
    if _has_login(url):
        raise ValueError(f"recipe {recipe.id!r} puts a username or password in its URL; "
                         "record it as access: manual instead")
    body = fill(recipe.body) if recipe.body else None
    # Also checked after filling, since a param can hold a whole `name=value` pair.
    try:
        found = _credential_params(url, recipe.headers, body)
    except ValueError as e:
        raise ValueError(f"recipe {recipe.id!r}: {e}") from None
    if found:
        raise ValueError(f"recipe {recipe.id!r} carries what look like credentials in "
                         f"{'; '.join(found)}; record it as access: manual instead")
    headers = {"user-agent": "Mozilla/5.0", **recipe.headers}
    return httpx.request(recipe.method.upper(), url, timeout=timeout, follow_redirects=True,
                         headers=headers,
                         content=body.encode() if body else None)


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
           "`vg source-note <host> <finding> --access manual`")


def _refuse_option(option: str) -> ValueError:
    # Name the option alone: its value may be the credential.
    name = option.partition("=")[0]
    if name in _CURL_CREDENTIAL_OPTIONS:
        return ValueError(f"curl {name} passes a credential. {_MANUAL}")
    return ValueError(f"unsupported curl option {name}: remove it if the request works "
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
                raise _refuse_option(option)
            elif rest:
                yield option, rest
                break
            elif i < len(args):
                yield option, args[i]
                i += 1
                break
            else:
                raise ValueError(f"curl {option} needs a value")


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
        raise ValueError("not a curl command")

    urls: list[str] = []
    method, body = None, None
    headers: dict[str, str] = {}
    dropped: list[str] = []
    unknown: list[str] = []

    def header(name: str, value: str) -> None:
        name = name.strip().lower()
        if not _HEADER_NAME.fullmatch(name):
            # Not a name, so perhaps a value that lost its colon. Named nowhere.
            raise ValueError("a curl header's name is not a header name; fix it or remove it")
        value = value.strip()
        if credential_header(name):
            dropped.append(name)
        elif name not in SAFE_HEADERS:
            unknown.append(name)
        elif name in _URL_HEADERS and _has_login(value):
            raise ValueError(f"the {name} header carries a username or password. {_MANUAL}")
        elif name == "referer":
            # The URL of the page the request came from, and a session id can ride in that
            # page's query or `;jsessionid=` parameters. The page itself is what a site checks.
            headers[name] = _page_only(value)
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
                    raise ValueError("a curl -H has no colon; write it `Name: value`")
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
        raise ValueError("no URL in curl command")
    if len(urls) > 1:
        raise ValueError(f"{len(urls)} URLs in the curl command; import one request at a time")
    url = urls[0]
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the curl command's URL is not an absolute http(s) URL")
    if _has_login(url):
        raise ValueError(f"the URL carries a username or password. {_MANUAL}")
    try:
        found = _credential_params(url, headers, body)
    except ValueError as e:
        raise ValueError(f"{e}. Record the endpoint by hand with "
                         "`vg source-note <host> <finding>`") from None
    if found:
        raise ValueError(f"the request carries what look like credentials in "
                         f"{'; '.join(found)}. Remove them from the paste if the request works "
                         f"without them. {_MANUAL}")
    host = _norm_host(url)
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
    return {"entry": entry, "dropped_credentials": sorted(set(dropped)),
            "dropped_headers": sorted(set(unknown))}
