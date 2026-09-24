"""The access registry must never hold a credential: an endpoint that only works with
someone's session is a manual retrieval, not a pipeline capability.

The fake credentials here are gitleaks placeholders on purpose. `changeme` is on its own
allowlist for curl's `--user`, and a header value with spaces in it is not token-shaped, so
none of them needs an entry in .gitleaks.toml.
"""

from __future__ import annotations

import json

import pytest

from vgpipe import access
from vgpipe.access import Recipe, credential_header, parse_curl, run

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
    "X-Api-Key", "Ocp-Apim-Subscription-Key", "x-functions-key", "X-Access-Key",
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
    res = parse_curl(f"curl -H 'Referer: https://portal.example/search;jsessionid=fakesession"
                     f"?sessionId=fakesession&tab=2#results' {URL}")
    assert "fakesession" not in json.dumps(res)
    assert _recipe(res)["headers"] == {"referer": "https://portal.example/search"}


def test_a_referer_carrying_a_login_is_refused():
    with pytest.raises(ValueError, match="username or password") as e:
        parse_curl(f"curl -e 'https://{USER}:{PASSWORD}@portal.example/' {URL}")
    assert PASSWORD not in str(e.value)


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


def test_run_refuses_a_login_that_a_param_puts_in_the_url():
    r = Recipe(id="bad", method="GET", url="https://{host}/api", params=["host"])
    with pytest.raises(ValueError, match="username or password"):
        run(r, {"host": f"{USER}:{PASSWORD}@portal.example"})


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

    from vgpipe import cli

    monkeypatch.setattr(access, "REGISTRY", tmp_path / "access")
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl --user canary-user:changeme {URL}\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)])
    assert r.exit_code == 1
    assert PASSWORD not in r.output
    assert not (tmp_path / "access").exists()


def test_import_command_writes_the_entry_without_session_headers(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from vgpipe import cli

    monkeypatch.setattr(access, "REGISTRY", tmp_path / "access")
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl '{URL}' \\\n  -H 'X-CSRF-Token: fake session value' \\\n"
                     f"  -H 'X-Client-Data: fake client value' \\\n  -H 'accept: */*'\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)])
    assert r.exit_code == 0, r.output
    written = (tmp_path / "access" / "portal.example.yaml").read_text()
    assert "fake session value" not in written and "fake client value" not in written
    assert "x-csrf-token" in r.output and "x-client-data" in r.output
