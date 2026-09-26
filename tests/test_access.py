"""The access registry must never hold a credential: an endpoint that only works with
someone's session is a manual retrieval, not a pipeline capability.

The fake credentials here are gitleaks placeholders on purpose. `changeme` is on its own
allowlist for curl's `--user`, and a header value with spaces in it is not token-shaped, so
none of them needs an entry in .gitleaks.toml.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from provenance import access
from provenance.access import Recipe, credential_header, parse_curl, run

URL = "https://portal.example/api/search"
USER = "canary-user"
PASSWORD = "changeme"


def _recipe(res: dict) -> dict:
    return res["entry"]["recipes"][0]


# USER and PASSWORD spelled out, so the secret scan sees its own placeholder.
@pytest.mark.parametrize("curl", [
    f"curl --user 'canary-user:changeme' {URL}",
    f"curl -u canary-user:changeme {URL}",
    f"curl -ucanary-user:changeme {URL}",
    f"curl -su canary-user:changeme {URL}",
    f"curl {URL} --user canary-user:changeme",
    f"curl --user=canary-user:changeme {URL}",
])
def test_user_option_is_refused_and_never_recorded(curl):
    """The importer skipped options it didn't know without skipping their values, so
    `curl --user name:password URL` recorded `name:password` as the recipe's URL, and the entry's
    file name. `--user` is always a credential, so the import is refused, and the refusal
    doesn't repeat it."""
    with pytest.raises(ValueError, match="credential") as e:
        parse_curl(curl)
    assert USER not in str(e.value) and PASSWORD not in str(e.value)


def test_a_value_taking_option_consumes_its_value():
    """What made `--user` land in the URL, for any option: its value read as the URL."""
    res = parse_curl(f"curl -A 'Probe/1.0' -o out.json --max-time 5 "
                     f"-e https://portal.example/ {URL}")
    r = _recipe(res)
    assert r["url"] == URL
    assert res["entry"]["host"] == "portal.example"
    assert r["headers"] == {"user-agent": "Probe/1.0", "referer": "https://portal.example/"}


def test_short_options_unbundle_as_curl_reads_them():
    res = parse_curl(f"curl -sSL -XPOST -d 'q=x' {URL}")
    r = _recipe(res)
    assert (r["url"], r["method"], r["body"]) == (URL, "POST", "q=x")


@pytest.mark.parametrize("option", [
    "--form 'upload=@notes.txt'", "--frobnicate=notes.txt", "-Z notes.txt",
])
def test_an_unknown_option_is_refused_not_skipped(option):
    """Skipping an option whose arity we don't know is how a value became the URL. The
    message names the option alone: its value may be the credential."""
    with pytest.raises(ValueError, match="unsupported curl option") as e:
        parse_curl(f"curl {option} {URL}")
    assert "notes.txt" not in str(e.value)


@pytest.mark.parametrize("url", [
    f"https://{USER}:{PASSWORD}@portal.example/api/search",
    f"https://{USER}@portal.example/api/search",
])
def test_a_url_carrying_a_login_is_refused(url):
    with pytest.raises(ValueError, match="username or password") as e:
        parse_curl(f"curl '{url}'")
    assert USER not in str(e.value) and PASSWORD not in str(e.value)


@pytest.mark.parametrize("curl", [
    f"curl {URL} {URL}/second",
    f"curl --url {URL} {URL}",
    "curl portal.example/api/search",
])
def test_the_url_must_be_one_absolute_http_url(curl):
    """A stray positional is the sign of an option read with the wrong arity, and a URL
    without a scheme is what a credential looks like when it lands there."""
    with pytest.raises(ValueError):
        parse_curl(curl)


CREDENTIAL_HEADERS = [
    "X-CSRF-Token", "X-XSRF-TOKEN", "x-csrftoken", "X-Session-Id", "x-sid", "X-PHPSESSID",
    "__RequestVerificationToken", "X-Goog-AuthUser", "Cookie", "Authorization", "api_key",
    "X-Api-Key", "Ocp-Apim-Subscription-Key", "x-functions-key", "X-Access-Key", "x-api-keys",
    "X-Passcode", "x-otp", "X-HMAC", "x-pin",
]


@pytest.mark.parametrize("name", CREDENTIAL_HEADERS)
def test_session_and_csrf_headers_are_dropped(name):
    """A browser's copied request carries the page's session under many names, not only
    Cookie and Authorization."""
    res = parse_curl(f"curl -H '{name}: fake session value' -H 'accept: */*' {URL}")
    assert "fake session value" not in json.dumps(res)
    assert res["dropped_credentials"] == [name.lower()]
    assert _recipe(res)["headers"] == {"accept": "*/*"}


def test_a_header_not_known_to_be_safe_is_dropped_and_named():
    """A deny-list can't anticipate every name a session header goes by, so an import keeps
    only headers known to describe the request, and names the rest for a human to check."""
    res = parse_curl(f"curl -H 'X-Client-Data: fake client value' "
                     f"-H 'content-type: application/json' -H 'Origin: https://portal.example' {URL}")
    assert "fake client value" not in json.dumps(res)
    assert res["dropped_headers"] == ["x-client-data"]
    assert res["dropped_credentials"] == []
    assert _recipe(res)["headers"] == {"content-type": "application/json",
                                       "origin": "https://portal.example"}


@pytest.mark.parametrize("value", ["fakesessiontoken", "Authorization Bearer fakesessiontoken: x"])
def test_a_header_that_is_not_name_colon_value_is_refused_without_repeating_it(value):
    """With its colon lost, a header is all value, and the whole of it would have been
    reported as a dropped header's name."""
    with pytest.raises(ValueError) as e:
        parse_curl(f"curl -H '{value}' {URL}")
    assert "fakesessiontoken" not in str(e.value)


def test_an_empty_header_is_read_as_curl_reads_it():
    res = parse_curl(f"curl -H 'X-Empty;' -H 'Accept;' {URL}")
    assert _recipe(res)["headers"] == {"accept": ""}
    assert res["dropped_headers"] == ["x-empty"]


def test_the_referer_is_kept_as_far_as_its_page():
    """A browser's referer is the full URL of the page the request came from, and a session
    id can ride in its query or its `;jsessionid=` parameters."""
    res = parse_curl(f"curl -H 'Referer: https://portal.example/app;jsessionid=fakesession/"
                     f"search;v=fakesession?sessionId=fakesession&tab=2#results' {URL}")
    assert "fakesession" not in json.dumps(res)
    assert _recipe(res)["headers"] == {"referer": "https://portal.example/app/search"}


@pytest.mark.parametrize("option", [
    f"-e 'https://{USER}:{PASSWORD}@portal.example/'",
    f"-H 'Origin: https://{USER}:{PASSWORD}@portal.example'",
])
def test_a_url_header_carrying_a_login_is_refused(option):
    with pytest.raises(ValueError, match="username or password") as e:
        parse_curl(f"curl {option} {URL}")
    assert PASSWORD not in str(e.value)


def test_an_emptied_header_is_removed_as_curl_removes_it():
    """curl's `Name:` removes the header; recorded as empty, `run()` would send it empty."""
    assert _recipe(parse_curl(f"curl -H 'User-Agent:' -H 'Accept: */*' {URL}"))["headers"] == {
        "accept": "*/*"}


@pytest.mark.parametrize("option, referer", [
    ("-e 'https://portal.example/;auto'", {"referer": "https://portal.example/"}),
    ("-e ';auto'", {}),
])
def test_curls_auto_referer_directive_is_not_part_of_the_referer(option, referer):
    assert _recipe(parse_curl(f"curl {option} {URL}"))["headers"] == referer


def test_no_safe_header_looks_like_a_credential():
    assert not [h for h in access.SAFE_HEADERS if credential_header(h)]


@pytest.mark.parametrize("name", CREDENTIAL_HEADERS)
def test_run_refuses_what_the_importer_drops(name):
    """A registry entry is edited by hand, so `run()` checks it again."""
    r = Recipe(id="bad", method="GET", url=URL, headers={name: "fake session value"})
    with pytest.raises(ValueError, match="credential headers"):
        run(r, {})


def test_run_refuses_a_login_in_the_url():
    r = Recipe(id="bad", method="GET", url=f"https://{USER}:{PASSWORD}@portal.example/api")
    with pytest.raises(ValueError, match="username or password") as e:
        run(r, {})
    assert PASSWORD not in str(e.value)


def test_run_refuses_a_login_in_a_url_header():
    r = Recipe(id="bad", method="GET", url=URL,
               headers={"Origin": f"https://{USER}:{PASSWORD}@portal.example"})
    with pytest.raises(ValueError, match="username or password"):
        run(r, {})


def test_run_refuses_a_login_that_a_param_puts_in_the_url():
    r = Recipe(id="bad", method="GET", url="https://{host}/api", params=["host"])
    with pytest.raises(ValueError, match="username or password"):
        run(r, {"host": f"{USER}:{PASSWORD}@portal.example"})


# #158. `\uff20` is the fullwidth `@`, which NFKC reads as `@`, so `urlsplit()` refuses this
# host, and its error quotes the whole netloc, login included.
UNREADABLE_LOGIN = "user:fake\uff20canary@portal.example"


def _refused_unrepeated(call, where: str) -> str:
    with pytest.raises(ValueError, match=f"{re.escape(where)} can't be parsed") as e:
        call()
    assert not re.search("fake|canary|\uff20|portal", str(e.value)), str(e.value)
    return str(e.value)


@pytest.mark.parametrize("curl, where", [
    (f"curl 'https://{UNREADABLE_LOGIN}/api'", "its URL"),
    (f"curl --url 'https://{UNREADABLE_LOGIN}/api'", "its URL"),
    # The stdlib has other errors for a host it can't read, and one quotes the host.
    ("curl 'https://user:fake@[canary]/api'", "its URL"),
    (f"curl -H 'Referer: https://{UNREADABLE_LOGIN}/' https://x.example/",
     "its referer header's URL"),
    (f"curl -e 'https://{UNREADABLE_LOGIN}/' https://x.example/", "its referer header's URL"),
    (f"curl -H 'Origin: https://{UNREADABLE_LOGIN}' https://x.example/", "its origin header's URL"),
])
def test_a_url_whose_host_cant_be_parsed_is_refused_without_repeating_it(curl, where):
    msg = _refused_unrepeated(lambda: parse_curl(curl), f"in the pasted request, {where}")
    assert "record the endpoint by hand" in msg


@pytest.mark.parametrize("recipe, params, where", [
    (Recipe(id="bad", method="GET", url=f"https://{UNREADABLE_LOGIN}/api"), {}, "its URL"),
    (Recipe(id="bad", method="GET", url="https://{host}/api", params=["host"]),
     {"host": UNREADABLE_LOGIN}, "its URL"),
    (Recipe(id="bad", method="GET", url="https://x.example/",
            headers={"Referer": f"https://{UNREADABLE_LOGIN}/"}), {}, "its referer header's URL"),
    (Recipe(id="bad", method="GET", url="https://x.example/",
            headers={"Origin": f"https://{UNREADABLE_LOGIN}"}), {}, "its origin header's URL"),
])
def test_run_refuses_a_url_whose_host_cant_be_parsed_without_repeating_it(recipe, params, where,
                                                                          monkeypatch):
    sent = []
    monkeypatch.setattr(access.httpx, "request", lambda *a, **kw: sent.append(a))
    msg = _refused_unrepeated(lambda: run(recipe, params), f"in recipe 'bad', {where}")
    assert msg.endswith("record it as access: manual instead") and not sent


def test_run_refuses_a_url_httpx_cant_read_without_repeating_it(monkeypatch):
    """httpx parses the URL again, and its error quoted the part it couldn't read: a port, and
    a password written with `%40` for its `@`, which is a port to httpx. That one is a login to
    the check now, which reads every decoding of a netloc, so it never reaches httpx."""
    monkeypatch.setattr(access.httpx.Client, "send", lambda *a, **kw: pytest.fail("sent"))
    r = Recipe(id="bad", method="GET", url="https://{host}/api", params=["host"])
    with pytest.raises(ValueError, match="its URL can't be sent as it is") as e:
        run(r, {"host": "portal.example/fakecanary\x00"})
    assert not re.search("fake|canary", str(e.value)), str(e.value)
    # A port that isn't one (`user:pw` before a `/` its encoding became) reads as a login now,
    # and so does `%40`, so neither reaches httpx.
    for host in ("portal.example:fakecanary", "user:fake%40canary.example"):
        with pytest.raises(ValueError, match="username or password") as e:
            run(r, {"host": host})
        assert not re.search("fake|canary", str(e.value)), str(e.value)


def test_the_commands_refuse_a_url_whose_host_cant_be_parsed_without_repeating_it(tmp_path,
                                                                                   monkeypatch):
    """What a person sees: the refusal is printed, and a terminal can be logged."""
    from typer.testing import CliRunner

    from provenance import cli

    reg = tmp_path / "access"
    reg.mkdir()
    (reg / "x.example.yaml").write_text(
        "recipes:\n  - {id: r, method: GET, url: 'https://{host}/api', params: [host]}\n")
    monkeypatch.setattr(access, "REGISTRY", reg)
    monkeypatch.setattr(access.httpx, "request", lambda *a, **kw: pytest.fail("sent"))
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl 'https://{UNREADABLE_LOGIN}/api'\n")
    for args in (["source-import-curl", str(paste)],
                 ["source-access", "x.example", "--run-recipe", "r",
                  "--param", f"host={UNREADABLE_LOGIN}"]):
        r = CliRunner().invoke(cli.app, args)
        assert r.exit_code == 1 and "can't be parsed" in r.output, r.output
        assert not re.search("fake|canary|\uff20", r.output), r.output
    # A host argument is split too, and the refusal is printed, not a traceback (#161).
    for args in (["source-access", f"https://{UNREADABLE_LOGIN}/"],
                 ["source-note", f"https://{UNREADABLE_LOGIN}/", "a finding"]):
        r = CliRunner().invoke(cli.app, args)
        assert r.exit_code == 1 and "the host can't be parsed" in r.output, r.output
        assert isinstance(r.exception, SystemExit), r.exception
        assert not re.search("fake|canary|\uff20", r.output), r.output
    assert [p.name for p in reg.iterdir()] == ["x.example.yaml"]


def test_every_url_from_a_paste_or_a_recipe_is_split_where_its_error_is_replaced():
    """A split anywhere else raises the stdlib's error, which quotes the netloc (#158). Every
    reference to a function that splits counts, called or not, bare or as an attribute, and
    so does an import that renames one. A command's host argument goes through it too."""
    splitters = {"urlsplit", "urlparse", "urljoin", "urldefrag"}
    found = []

    def visit(node, where):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            where = getattr(node, "name", "<lambda>")
        if isinstance(node, ast.Name) and node.id in splitters:
            found.append((where, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in splitters:
            found.append((where, node.attr))
        elif isinstance(node, ast.alias) and node.name in splitters and node.asname:
            found.append((where, f"{node.name} as {node.asname}"))
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(ast.parse(Path(access.__file__).read_text()), "<module>")
    assert found == [("_split", "urlsplit")]


def test_run_allows_a_hand_added_header_that_is_not_a_credential(monkeypatch):
    """The import drops headers nobody has looked at. One a human added to the entry runs,
    unless it looks like a credential."""
    sent = {}
    monkeypatch.setattr(access.httpx, "request",
                        lambda method, url, **kw: sent.update(method=method, url=url, **kw))
    run(Recipe(id="ok", method="GET", url=URL, headers={"X-Api-Version": "2"}), {})
    assert sent["headers"]["X-Api-Version"] == "2"


def test_import_command_writes_nothing_for_a_refused_paste(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from provenance import cli

    monkeypatch.setattr(access, "REGISTRY", tmp_path / "access")
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl --user canary-user:changeme {URL}\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)])
    assert r.exit_code == 1
    assert PASSWORD not in r.output
    assert not (tmp_path / "access").exists()


def test_import_command_writes_the_entry_without_session_headers(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from provenance import cli

    monkeypatch.setattr(access, "REGISTRY", tmp_path / "access")
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl '{URL}' \\\n  -H 'X-CSRF-Token: fake session value' \\\n"
                     f"  -H 'X-Client-Data: fake client value' \\\n  -H 'accept: */*'\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)])
    assert r.exit_code == 0, r.output
    written = (tmp_path / "access" / "portal.example.yaml").read_text()
    assert "fake session value" not in written and "fake client value" not in written
    assert "x-csrf-token" in r.output and "x-client-data" in r.output
