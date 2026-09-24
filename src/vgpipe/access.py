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
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
# `*-key` and `*_key` without reading `keyword` as one.
_CREDENTIAL_HEADER = re.compile(
    r"auth|token|csrf|xsrf|sess|secret|passw|cred|cookie|bearer|jwt|signature"
    r"|key(?:$|[-_])|(?:^|[-_])sid(?:$|[-_])")

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


# An HTTP header name (RFC 9110's token). Anything else in a name slot, such as
# `Authorization Bearer abc` with its colon lost, is a value that has lost its separator.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def credential_header(name: str) -> bool:
    return bool(_CREDENTIAL_HEADER.search(name.strip().lower()))


def _has_login(url: str) -> bool:
    """A username or password in the URL itself: basic auth by another route."""
    return "@" in urlparse(url).netloc


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
    """Execute a recipe. Credential headers are refused, not stripped: a recipe that needs
    one is describing a manual retrieval and should be recorded as such."""
    bad = sorted(k for k in recipe.headers if credential_header(k))
    if bad:
        raise ValueError(f"recipe {recipe.id!r} carries credential headers ({', '.join(bad)}); "
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
    the rest of the word is the value. curl has no `--option=value` form.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if not arg.startswith("-") or arg == "-":
            yield None, arg
            continue
        long = arg.startswith("--")
        for k, option in enumerate([arg] if long else ["-" + c for c in arg[1:]]):
            if option not in _CURL_FLAGS and option not in _CURL_VALUE_OPTIONS:
                raise _refuse_option(option)
            if option in _CURL_FLAGS:
                yield option, None
                continue
            attached = "" if long else arg[k + 2:]
            if attached:
                yield option, attached
            elif i < len(args):
                yield option, args[i]
                i += 1
            else:
                raise ValueError(f"curl {option} needs a value")
            break


def parse_curl(text: str) -> dict:
    """Turn a browser 'copy as cURL' into a registry entry, minus credentials.

    This is how these endpoints get discovered in practice — open dev tools, do the search,
    copy the request. The cookies and session headers in that paste are the human's live
    session: they are dropped here and never written to disk, along with any header not
    known to be safe, and the referer loses its query. A login passed any other way
    (`--user`, a username in the URL) means the endpoint needs one, so the import is refused.
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
        elif name == "referer":
            # The URL of the page the request came from, and a session id can ride in that
            # page's query or `;jsessionid=` parameters. The page itself is what a site checks.
            if _has_login(value):
                raise ValueError(f"the referer carries a username or password. {_MANUAL}")
            headers[name] = urlunparse(urlparse(value)._replace(params="", query="", fragment=""))
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
