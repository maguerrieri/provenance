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
from urllib.parse import urlparse

import httpx
import yaml

REGISTRY = Path(__file__).resolve().parents[2] / "sources" / "access"

# Never persisted, never replayed. An endpoint that only works with your session is a manual
# retrieval, not a pipeline capability, and recording the credential would be both a leak and
# a lie about what the pipeline can do.
CREDENTIAL_HEADERS = {"cookie", "authorization", "proxy-authorization", "x-api-key",
                      "x-auth-token", "api-key", "authentication"}


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
    bad = sorted(k for k in recipe.headers if k.lower() in CREDENTIAL_HEADERS)
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
    body = fill(recipe.body) if recipe.body else None
    headers = {"user-agent": "Mozilla/5.0", **recipe.headers}
    return httpx.request(recipe.method.upper(), url, timeout=timeout, follow_redirects=True,
                         headers=headers,
                         content=body.encode() if body else None)


def parse_curl(text: str) -> dict:
    """Turn a browser 'copy as cURL' into a registry entry, minus credentials.

    This is how these endpoints get discovered in practice — open dev tools, do the search,
    copy the request. The cookies in that paste are the human's live session: they are
    dropped here and never written to disk.
    """
    text = re.sub(r"\\\s*\n", " ", text).strip()
    tokens = shlex.split(text)
    if not tokens or tokens[0] != "curl":
        raise ValueError("not a curl command")

    url, method, body = None, None, None
    headers: dict[str, str] = {}
    dropped: list[str] = []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-H", "--header"):
            i += 1
            k, _, v = tokens[i].partition(":")
            (dropped if k.strip().lower() in CREDENTIAL_HEADERS else None)
            if k.strip().lower() in CREDENTIAL_HEADERS:
                dropped.append(k.strip().lower())
            else:
                headers[k.strip().lower()] = v.strip()
        elif t in ("-b", "--cookie"):
            i += 1
            dropped.append("cookie")
        elif t in ("--data-raw", "--data", "-d", "--data-binary"):
            i += 1
            body = tokens[i]
            method = method or "POST"
        elif t in ("-X", "--request"):
            i += 1
            method = tokens[i]
        elif t in ("--url",):
            i += 1
            url = tokens[i]
        elif t.startswith("-"):
            pass
        elif url is None:
            url = t
        i += 1

    if not url:
        raise ValueError("no URL in curl command")
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
            "notes": "Imported from a browser request. Credentials were dropped — confirm it "
                     "still works without them before relying on it.",
        }],
    }
    return {"entry": entry, "dropped_credentials": sorted(set(dropped))}
