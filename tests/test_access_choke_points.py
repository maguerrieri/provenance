"""The source-access registry's two choke points (#161): one check every entry passes on its way
in, whatever wrote it, and one redactor every refusal passes on its way out.

Seven holes were closed one entry point at a time (#46, #52, #69's neighbours, #155, #158,
#161, #163), and each review found another. So these tests pin the choke points, not the
commands: a new writer that skips the check, or a new message that skips the redactor, fails
here before anyone has to think of the command it came in through.

The fake credentials are low-entropy placeholders, so the secret scan passes them without an
entry in .gitleaks.toml.
"""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from provenance import access, cli
from provenance.access import Recipe, Refused, check_entry, parse_curl, redact, run

CANARY = "fakecanary"
LOGIN = f"canary-user:{CANARY}"
LEAK = re.compile(f"{CANARY}|canary-user|fake\uff20|fake%40")
SRC = Path(access.__file__).parent


def _clean(text: str) -> None:
    assert not LEAK.search(text), text


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "access"
    monkeypatch.setattr(access, "REGISTRY", reg)
    monkeypatch.setattr(access.httpx, "request", lambda *a, **kw: pytest.fail("sent"))
    return reg


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*"))
            if p.is_file()}


def _invoke(*args) -> tuple[int, str]:
    r = CliRunner().invoke(cli.app, [str(a) for a in args])
    # A refusal, never a traceback: typer's pretty traceback can show the command's locals.
    assert isinstance(r.exception, SystemExit) or r.exception is None, r.exception
    return r.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", r.output)


# --- in: a login in a URL nested anywhere in a request (#163) --------------------------------

NESTED_LOGINS = [
    f"curl 'https://x.example/api?next=https://{LOGIN}@y.example/'",
    f"curl -H 'content-type: application/json' -d '{{\"next\":\"https://{LOGIN}@y.example/\"}}' "
    "https://x.example/api",
    f"curl 'https://x.example/api?next=//{LOGIN}@y.example/'",          # protocol-relative
    "curl -d '" + json.dumps({"a": {"b": [{"next": f"https://{LOGIN}@y.example/"}]}})
    + "' https://x.example/api",                                        # deeper in JSON
    f"curl -d 'next=https%3A%2F%2Fcanary-user%3A{CANARY}%40y.example%2F&q=1' "
    "https://x.example/api",                                            # a form field, encoded
    f"curl 'https://x.example/api#next=https://{LOGIN}@y.example/'",    # the fragment
    f"curl 'https://x.example/api?q=see%20https://{LOGIN}@y.example/%20first'",  # inside text
    "curl 'https://x.example/api?next=https://canary-user:fake\uff20canary.example/'",
    # An encoded `/` before an encoded `@`: decoded, the authority ends before the `@`.
    f"curl 'https://x.example/api?next=https://canary-user:x%2F{CANARY}%40y.example/'",
    f"curl -d 'next=https://canary-user:x%252F{CANARY}%2540y.example/' https://x.example/api",
    # Encoded seven times over: read to the end, however deep.
    f"curl 'https://x.example/api?next=https://canary-user:{CANARY}%25252525252540y.example/'",
    f"curl -H 'Origin: https://x.example/?next=https://{LOGIN}@y.example/' https://x.example/api",
]


@pytest.mark.parametrize("curl", NESTED_LOGINS)
def test_a_login_in_a_nested_url_is_refused_on_import(curl, registry, tmp_path):
    """#163: the nested URL was read for its parameter names and never for its netloc."""
    with pytest.raises(Refused, match="carries a username or password") as e:
        parse_curl(curl)
    _clean(str(e.value))
    paste = tmp_path / "paste.sh"
    paste.write_text(curl)
    code, out = _invoke("source-import-curl", paste)
    assert code == 1, out
    _clean(out)
    assert not registry.exists()


def test_a_value_encoded_past_reading_is_refused_not_read_no_further(registry):
    """Four rounds of decoding, then a stop, let a login one layer deeper through. A value that
    still decodes after the last round can't be shown to hold no login, so it is refused, and
    the redactor removes it."""
    deep = "%" + "25" * 11 + "40"   # an `@` percent-encoded twelve times over
    url = f"https://x.example/api?next=https://canary-user:{CANARY}{deep}y.example/"
    with pytest.raises(Refused, match="encoded more than 8 times over") as e:
        parse_curl(f"curl '{url}'")
    _clean(str(e.value))
    assert redact(f"see https://x.example/?n={CANARY}{deep}") == "see [redacted]"


@pytest.mark.parametrize("recipe", [
    Recipe(id="r", method="GET", url=f"https://x.example/api?next=https://{LOGIN}@y.example/"),
    Recipe(id="r", method="POST", url="https://x.example/api",
           headers={"content-type": "application/json"},
           body=json.dumps({"q": [{"next": f"//{LOGIN}@y.example/"}]})),
    Recipe(id="r", method="POST", url="https://x.example/api", body="next={next}&q=1",
           params=["next"]),
    # Any header, not only origin and referer: the send path holds what the registry does.
    Recipe(id="r", method="GET", url="https://x.example/api",
           headers={"X-Endpoint": f"https://{LOGIN}@y.example/"}),
    Recipe(id="r", method="GET", url="https://x.example/api", notes=f"//{LOGIN}@y.example"),
])
def test_run_refuses_a_login_in_a_nested_url(recipe, registry):
    """`run()` checks a recipe as `check_entry()` does, since one built by hand reaches it too."""
    with pytest.raises(Refused, match="username or password") as e:
        run(recipe, {"next": f"https://{LOGIN}@y.example/"})
    _clean(str(e.value))


@pytest.mark.parametrize("query", ["next=https://y.example/@canary", "q=records@agency.example",
                                   "next=mailto:records@agency.example",
                                   "q=from:alice@agency.example"])
def test_an_at_sign_that_is_not_a_login_still_imports_and_runs(query, registry, monkeypatch):
    """A login is an `@` in a URL's authority. A scheme-less `name:x@host` is refused only where
    a host is expected: in a query it reads the same as a search (`from:alice@…`)."""
    url = f"https://x.example/api?{query}"
    assert parse_curl(f"curl '{url}'")["entry"]["recipes"][0]["url"] == url
    sent = []
    monkeypatch.setattr(access.httpx, "request", lambda *a, **kw: sent.append(a[1]))
    name, _, value = query.partition("=")
    run(Recipe(id="r", method="GET", url=f"https://x.example/api?{name}={{v}}", params=["v"]),
        {"v": value})
    assert sent == [url]


# --- in: a host argument, and every other value a command writes (#161) ----------------------

HOST_LOGINS = [
    f"https://{LOGIN}@portal.example/",
    f"{LOGIN}@portal.example",                        # no scheme: `.hostname` never dropped it
    "https://canary-user:fake\uff20canary@portal.example/",  # #158's, which urlsplit refuses
    "canary-user:fake%40canary.example",
    f"http://{LOGIN}@portal.example:8080/path",
]


@pytest.mark.parametrize("host", HOST_LOGINS)
def test_a_host_argument_holding_a_login_is_refused_and_nothing_is_written(host, registry):
    for args in (("source-note", host, "a finding"), ("source-access", host),
                 ("source-access", host, "--run-recipe", "r")):
        code, out = _invoke(*args)
        assert code == 1, (args, out)
        assert "username or password" in out or "can't be parsed" in out, out
        _clean(out)
    assert not registry.exists()


@pytest.mark.parametrize("host", ["../outside", "portal.example/../../outside", "a b.example",
                                  "portal.example/x", "", "."])
def test_a_host_that_is_not_a_host_name_names_no_file(host, registry, tmp_path):
    """The host is the file's name, so a path in it wrote outside the registry."""
    code, out = _invoke("source-note", host, "a finding")
    assert code == 1 and "not a host name" in out, out
    assert _files(tmp_path) == {}


@pytest.mark.parametrize("args", [
    ("portal.example", f"works at https://{LOGIN}@portal.example/api"),
    ("portal.example", f"api at https://portal.example/api?api_key={CANARY}"),
    ("portal.example", "fine", "--access", f"https://{LOGIN}@portal.example/"),
    ("portal.example", "fine", "--verified", f"https://{LOGIN}@portal.example/"),
])
def test_source_note_refuses_a_credential_in_any_value_it_writes(args, registry):
    code, out = _invoke("source-note", *args)
    assert code == 1, out
    _clean(out)
    assert not registry.exists()


def test_source_note_checks_the_entry_it_appends_to(registry):
    """What was there already is written back too, so it is checked again."""
    registry.mkdir()
    (registry / "portal.example.yaml").write_text(
        f"host: portal.example\nnotes: see https://{LOGIN}@portal.example/\n")
    before = _files(registry)
    code, out = _invoke("source-note", "portal.example", "a finding")
    assert code == 1 and "field notes holds a username or password" in out, out
    _clean(out)
    assert _files(registry) == before


def test_source_import_curl_checks_its_name_and_what_it_prints(registry, tmp_path):
    paste = tmp_path / "paste.sh"
    paste.write_text("curl 'https://portal.example/api?q=1'")
    for extra in ((), ("--no-write",)):
        code, out = _invoke("source-import-curl", paste, "--name",
                            f"portal at https://{LOGIN}@portal.example/", *extra)
        assert code == 1, out
        _clean(out)
    assert not registry.exists()


# --- in: check_entry(), the one check, for each shape and in every field ---------------------

def _entry(**fields) -> dict:
    return {"host": "portal.example", "name": "", "access": "api", **fields}


def _recipe(**fields) -> dict:
    return {"recipes": [{"id": "search", "method": "GET", "url": "https://portal.example/api",
                         **fields}]}


@pytest.mark.parametrize("entry, match", [
    (_entry(host=f"{LOGIN}@portal.example"), "username or password"),
    (_entry(host="../portal.example"), "not a host name"),
    (_entry(notes=f"see https://{LOGIN}@portal.example/"), "field notes holds a username"),
    (_entry(limits=f"proxy http://{LOGIN}@proxy.example works"), "field limits holds a username"),
    (_entry(extra=[{"deep": f"//{LOGIN}@portal.example/"}]), "field extra.deep holds"),
    (_entry(notes=f"see https:\\/\\/{LOGIN}@portal.example\\/"), "holds a username"),  # escaped
    (_entry(ui_url=f"https://portal.example/?api_key={CANARY}"), r"credentials in its URL"),
    (_entry(api_key=CANARY), "field named like a credential"),
    (_entry(extra={"session_token": CANARY}), "field named like a credential"),
    (_entry(cookie=f"session={CANARY}"), r"field named like a credential \(cookie\)"),
    (_entry(extra={"Set-Cookie": CANARY}), "field named like a credential"),
    (_entry(**_recipe(url=f"https://{LOGIN}@portal.example/api")), "its URL carries a username"),
    (_entry(**_recipe(url=f"https://portal.example/api?token={CANARY}")), r"its URL \(token\)"),
    (_entry(**_recipe(headers={"Authorization": f"Basic {CANARY}"})), "credential headers"),
    (_entry(**_recipe(headers={"Origin": f"https://{LOGIN}@portal.example"})),
     "origin header's URL carries"),
    (_entry(**_recipe(headers={"user-agent": f"probe https://{LOGIN}@portal.example/"})),
     "its user-agent header holds a username"),
    (_entry(**_recipe(headers={"x-endpoint": f"https://portal.example/?api_key={CANARY}"})),
     r"its x-endpoint header holds a URL carrying .* \(api_key\)"),
    (_entry(**_recipe(notes=f"see https://{LOGIN}@portal.example/")),
     "in recipe 'search', its field notes holds a username"),
    (_entry(**_recipe(body=json.dumps({"q": {"next": f"https://{LOGIN}@y.example/"}}))),
     "carries a username"),
    (_entry(**_recipe(body=f"q=1&csrf_token={CANARY}")), r"its body \(csrf_token\)"),
    (_entry(**_recipe(params=["token"])), "asks for what look like credentials"),
    (_entry(recipes="not a list"), "not a list"),
    ([], "a mapping of fields"),
])
def test_check_entry_refuses_each_shape_naming_where_not_what(entry, match):
    with pytest.raises(Refused, match=match) as e:
        check_entry(entry)
    _clean(str(e.value))


@pytest.mark.parametrize("entry", [
    # Documentation names a key parameter with a relative link and a placeholder.
    _entry(manual_steps="POST /Home/Pdf -> {url: .../DownloadPdf?key=<hex>&fileName=..}"),
    _entry(notes="write to records@agency.example; see https://portal.example/@records"),
    _entry(**_recipe(url="https://{host}/api?session={session}", params=["host", "session"])),
    _entry(verified=__import__("datetime").date(2030, 1, 2), notes=404, recipes=None),
    # A placeholder for a whole JSON value is no JSON until it is filled.
    _entry(**_recipe(headers={"content-type": "application/json"}, body='{"year": {year}}',
                     params=["year"])),
    _entry(notes="search from:alice@agency.example to find the filings"),
])
def test_check_entry_accepts_what_is_not_a_credential(entry):
    check_entry(entry)


def test_a_recipe_is_checked_as_it_will_be_sent(registry, monkeypatch):
    """Checked as recorded, a template refused `{"year": {year}}` as neither JSON nor a form,
    though it runs: each placeholder is read as a neutral value first, then as filled."""
    sent = []
    monkeypatch.setattr(access.httpx, "request", lambda *a, **kw: sent.append(kw["content"]))
    r = Recipe(id="r", method="POST", url="https://portal.example/api",
               headers={"content-type": "application/json"}, body='{"year": {year}}',
               params=["year"])
    run(r, {"year": "2030"})
    assert sent == [b'{"year": 2030}']
    with pytest.raises(Refused, match=r"its body \(token\)"):
        run(r, {"year": f'1, "token": "{CANARY}"'})


def test_a_host_argument_with_a_port_names_the_host(registry):
    """The registry is kept by host, and a URL's port was always dropped: so is a bare one."""
    code, out = _invoke("source-note", "portal.example:8443", "a finding")
    assert code == 0, out
    assert list(_files(registry)) == ["portal.example.yaml"]


def test_the_committed_registry_passes_the_entry_check():
    """`load_all()` checks every file, so a committed credential breaks every command that
    reads the registry, and this test, before anything reads it."""
    entries = access.load_all()
    assert entries
    for p in sorted(access.REGISTRY.glob("*.yaml")):
        check_entry(access._read_entry(p), p.stem)


@pytest.mark.parametrize("text", [
    f"host: portal.example\nnotes: https://{LOGIN}@portal.example/\n",
    "host: portal.example\nrecipes:\n  - id: r\n    method: GET\n    url: https://portal.example/\n"
    "    headers:\n      Cookie: session=fakecanary\n",
    f"host: portal.example\napi_key: {CANARY}\n",
])
def test_a_hand_edited_registry_file_is_refused_on_every_read(text, registry):
    """A person with an editor is a writer too: the check runs on every read."""
    registry.mkdir()
    (registry / "portal.example.yaml").write_text(text)
    for args in (("source-access",), ("source-access", "portal.example"),
                 ("source-access", "sub.portal.example", "--run-recipe", "r")):
        code, out = _invoke(*args)
        assert code == 1 and "registry's entry for portal.example can't be used" in out, out
        _clean(out)


def test_an_entry_whose_host_field_names_another_host_is_refused(registry):
    """`load_all()` serves an entry under its file's name, so a `host:` naming another host
    served that host's recipes under this one."""
    with pytest.raises(Refused, match="names another host than the file it is in"):
        check_entry(_entry(host="other.example"), "portal.example")
    check_entry(_entry(host="WWW.Portal.example"), "portal.example")
    registry.mkdir()
    (registry / "portal.example.yaml").write_text("host: other.example\n")
    code, out = _invoke("source-access", "portal.example")
    assert code == 1 and "names another host" in out, out
    code, out = _invoke("source-note", "portal.example", "a finding")
    assert code == 1 and "names another host" in out, out


def test_a_registry_file_not_named_for_a_host_is_refused_without_its_name(registry):
    registry.mkdir()
    (registry / f"{LOGIN}@portal.example.yaml").write_text("host: portal.example\n")
    code, out = _invoke("source-access")
    assert code == 1 and "not named for a host" in out, out
    _clean(out)


# --- in: pins. Every write under the registry goes through save(), and save() checks ---------

def _tree(module: str) -> ast.Module:
    return ast.parse((SRC / f"{module}.py").read_text())


def _enclosing_functions(tree: ast.AST):
    """(function name, node) for every node, the innermost enclosing def naming it."""
    def visit(node, where):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            where = node.name
        yield where, node
        for child in ast.iter_child_nodes(node):
            yield from visit(child, where)
    yield from visit(tree, "<module>")


def _calls(fn: ast.FunctionDef) -> set[str]:
    return {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            for n in ast.walk(fn) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name | ast.Attribute)}


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_only_save_writes_under_the_registry_and_only_dump_entry_makes_its_yaml():
    """A new writer can't skip the check: the registry's path comes only from `entry_path()`
    (and `load_all()`, which reads), YAML only from `dump_entry()`, which checks, and a file
    only from `save()`, which calls it. Anything else in the package that names the registry,
    dumps YAML or writes in `access.py` fails here until it goes through them."""
    registry, dumps, writes = set(), set(), set()
    for path in sorted(SRC.glob("*.py")):
        module, tree = path.stem, ast.parse(path.read_text())
        # What names the access module here, or its REGISTRY: `queries.REGISTRY` is another.
        aliases = _access_names(tree)
        for where, node in _enclosing_functions(tree):
            name = (node.id if isinstance(node, ast.Name) else
                    node.attr if isinstance(node, ast.Attribute) else None)
            if name == "REGISTRY" and (
                    (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                     and (module == "access" or "REGISTRY" in aliases))
                    or (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in aliases)):
                registry.add((module, where))
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "yaml"
                    and node.attr in {"dump", "safe_dump", "dump_all", "safe_dump_all"}):
                dumps.add((module, where))
            if module == "access" and (
                    name in {"write_text", "write_bytes", "open", "rename", "unlink", "mkdir"}
                    or (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id in {"os", "shutil"})):
                writes.add(where)
    assert registry == {("access", "entry_path"), ("access", "load_all")}
    assert dumps == {("access", "dump_entry")}
    assert writes == {"save"}
    fns = _functions(_tree("access"))
    assert {"entry_path", "dump_entry"} <= _calls(fns["save"])
    assert "check_entry" in _calls(fns["dump_entry"])
    assert "check_entry" in _calls(fns["load_all"])
    assert "save" in _calls(fns["note"])


def _access_names(node: ast.AST) -> set[str]:
    """The names `node` (a function, or a whole module) binds to the access module, or imports
    from it."""
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.ImportFrom) and n.module in (None, "access") and n.level == 1:
            for alias in n.names:
                if n.module == "access" or alias.name == "access":
                    names.add(alias.asname or alias.name)
    return names


def _cli_access_functions() -> dict[str, ast.FunctionDef]:
    return {name: fn for name, fn in _functions(_tree("cli")).items() if _access_names(fn)}


# Every command that writes the registry, with invocations putting a synthetic credential in
# each value it writes. A new writer fails the test below until it is listed here.
WRITERS = {
    "source_note": lambda tmp: [
        ("source-note", f"{LOGIN}@portal.example", "a finding"),
        ("source-note", "portal.example", f"see https://{LOGIN}@portal.example/"),
        ("source-note", "portal.example", "x", "--access", f"//{LOGIN}@portal.example"),
        ("source-note", "portal.example", "x", "--verified", f"https://{LOGIN}@x.example/"),
    ],
    "source_import_curl": lambda tmp: [
        ("source-import-curl", _paste(tmp, f"curl 'https://{LOGIN}@portal.example/api'")),
        ("source-import-curl",
         _paste(tmp, f"curl 'https://portal.example/?n=//{LOGIN}@y.example'")),
        ("source-import-curl", _paste(tmp, "curl 'https://portal.example/api'"),
         "--name", f"https://{LOGIN}@portal.example/"),
    ],
}


def _paste(tmp: Path, text: str) -> Path:
    p = tmp / f"paste{len(list(tmp.glob('paste*')))}.sh"
    p.write_text(text)
    return p


def test_every_command_that_writes_the_registry_is_held_to_the_check(registry, tmp_path):
    writers = {name for name, fn in _cli_access_functions().items()
               if _calls(fn) & {"save", "note", "dump_entry"}}
    assert writers == set(WRITERS), "list a new writer's invocations in WRITERS"
    pastes = tmp_path / "pastes"
    pastes.mkdir()
    for name, invocations in WRITERS.items():
        for args in invocations(pastes):
            code, out = _invoke(*args)
            assert code == 1, (name, args, out)
            _clean(out)
    assert not registry.exists()


# --- out: redact(), the one redactor, and the pins that every message passes it --------------

@pytest.mark.parametrize("message", [
    f"Nothing recorded for https://{LOGIN}@portal.example/.",
    f"Nothing recorded for {LOGIN}@portal.example.",
    "host https://canary-user:fake\uff20canary@portal.example/",
    "while scanning\n  in \"<unicode string>\", line 3\n"
    f"    url: https://portal.example/api?api_key={CANARY}&q=1\n    ^",
    f"headers:\n  Authorization: Bearer {CANARY}\n  accept: */*",
    f'{{"token": "{CANARY}", "q": "x"}}',
    f"{{'password': '{CANARY}'}}",
    f"Illegal header value b'Bearer {CANARY}'",
    f"next=https%3A%2F%2Fcanary-user%3A{CANARY}%40y.example%2F",
    f"https://portal.example/?next=https%253A%252F%252Fcanary-user%253A{CANARY}%2540y.example",
    f"session_id={CANARY}",
    f"-H 'X-CSRF-Token: {CANARY}'",
    f"see https://canary-user:x%2F{CANARY}%40y.example/ first",
    # A pair whose name is not a credential's gives up its name, not the rest of the line.
    f"ValueError: bad; api_key: {CANARY}",
    f"note: see token: {CANARY}",
    # A bare login's password may hold `=` or `:`, as base64 does.
    f"failed for canary-user:{CANARY}==@portal.example",
    f"failed for canary-user:{CANARY}:x=y@portal.example",
])
def test_redact_removes_every_credential_shape(message):
    out = redact(message)
    _clean(out)
    assert "[redacted]" in out and redact(out) == out


@pytest.mark.parametrize("word", ["x" * 20000, "a:" * 10000, "a=" * 10000, "/" * 20000])
def test_redact_takes_time_in_proportion_to_a_long_word(word):
    """A name pattern tried at every position in a word backtracked over the rest of it: 3.7s
    for 16,000 characters, four times as long for twice as many."""
    start = time.monotonic()
    redact(word)
    assert time.monotonic() - start < 1


@pytest.mark.parametrize("message", [
    "in recipe 'token', it carries what look like credentials in its URL (api_key); "
    "record it as access: manual instead",
    "the registry's entry for auth.example can't be used, in recipe 'search', its URL carries "
    "a username or password",
    "the pasted request: its URL can't be parsed, so it can't be checked for a credential",
    "KeyError: 'token'",
    "unsupported curl option --cookie-jar; remove it if the request works without it, or "
    "record the endpoint by hand",
    "no recipe '[/] [sic] :ok:'",
    "Nothing recorded for session.example.",
    "write to records@agency.example, or see https://portal.example/@records",
])
def test_redact_leaves_the_reason_in_a_refusal(message):
    """Names say what to fix, and a refusal's own words are never a field and its value."""
    assert redact(message) == message


def test_every_exception_access_raises_is_a_refused():
    """`Refused` redacts its message as it is made, so nothing this module raises can quote a
    credential. A `ValueError(...)` or any other exception made here fails this test."""
    bad = []
    for where, node in _enclosing_functions(_tree("access")):
        if isinstance(node, ast.Raise) and node.exc is not None and not (
                isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "Refused"):
            bad.append((where, ast.unparse(node)))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and re.fullmatch(r"\w*(Error|Exception)", node.func.id)):
            bad.append((where, ast.unparse(node)))
    assert bad == []


def test_every_access_function_the_cli_calls_turns_a_library_error_into_a_refused():
    """A library's error can quote what it was handed. `_refusing` makes a `Refused` of it at
    the boundary, so a function the CLI calls without it fails here."""
    fns = _functions(_tree("access"))
    called = {n.attr for fn in _cli_access_functions().values() for n in ast.walk(fn)
              if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
              and n.value.id in _access_names(fn) and n.attr in fns}
    assert {"load_all", "find", "run", "note", "parse_curl", "dump_entry", "entry_path",
            "save"} <= called
    undecorated = sorted(name for name in called - {"redact"}
                         if not any(isinstance(d, ast.Name) and d.id == "_refusing"
                                    for d in fns[name].decorator_list))
    assert undecorated == []


def test_every_access_command_refuses_only_through_the_redactor():
    """Each access command's body is one `with _access_refusals():`, which prints whatever it
    raises through `_access_refused()`, and that prints through `access.redact()`. A command
    that catches, exits or raises on its own could print a message the redactor never saw."""
    fns = _functions(_tree("cli"))
    assert "redact" in _calls(fns["_access_refused"])
    handlers = [h for h in ast.walk(fns["_access_refusals"]) if isinstance(h, ast.ExceptHandler)]
    assert all(isinstance(h.body[-1], ast.Raise) or "_access_refused" in _calls(h)
               for h in handlers) and len(handlers) == 3
    commands = {name: fn for name, fn in _cli_access_functions().items()
                if name not in {"_access_refused", "_access_refusals"}}
    assert set(WRITERS) | {"source_access"} <= set(commands)
    for name, fn in commands.items():
        body = [s for s in fn.body if not (isinstance(s, ast.Expr)
                                           and isinstance(s.value, ast.Constant))
                and not isinstance(s, ast.ImportFrom)]
        assert len(body) == 1 and isinstance(body[0], ast.With), name
        (item,) = body[0].items
        assert ast.unparse(item.context_expr) == "_access_refusals()", name
        assert not [n for n in ast.walk(body[0]) if isinstance(n, ast.Raise | ast.Try)], name


def test_a_library_error_quoting_a_credential_is_redacted(registry, monkeypatch):
    def refused(*a, **kw):
        raise httpx.ConnectError(f"cannot reach https://{LOGIN}@portal.example/api")

    monkeypatch.setattr(access.httpx, "request", refused)
    with pytest.raises(Refused, match="ConnectError") as e:
        run(Recipe(id="r", method="GET", url="https://portal.example/api"), {})
    _clean(str(e.value))


def test_a_yaml_error_quoting_the_file_is_redacted(registry):
    """PyYAML's error quotes the lines around it, and a hand edit can be broken and hold a
    credential at once."""
    registry.mkdir()
    (registry / "portal.example.yaml").write_text(
        f"host: portal.example\nnotes: [see https://{LOGIN}@portal.example/\n  api_key: {CANARY}\n")
    code, out = _invoke("source-access")
    assert code == 1 and "registry's entry for portal.example can't be used" in out, out
    _clean(out)


def test_a_header_value_httpx_cant_send_is_not_quoted(registry, monkeypatch):
    """h11 quotes the whole value of a header it can't send, whatever the header is called, so
    no name tells the redactor it is a credential."""
    def refused(*a, **kw):
        raise httpx.LocalProtocolError(f"Illegal header value b'{CANARY}\\n'")

    monkeypatch.setattr(access.httpx, "request", refused)
    with pytest.raises(Refused, match="a header can't be sent as it is") as e:
        run(Recipe(id="r", method="GET", url="https://portal.example/api",
                   headers={"X-Probe": f"{CANARY}\n"}), {})
    _clean(str(e.value))
