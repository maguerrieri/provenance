"""A curl import must not record a credential in the recipe URL or body either: an endpoint
that only works with one is a manual retrieval, not a pipeline capability.

Parameters get a rule of their own, not the header pattern: in public-records APIs
`?session=2025-2026` is a legislative session. The fake value is low-entropy on purpose, so
the secret scan passes it without an entry in .gitleaks.toml.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from provenance import access
from provenance.access import Recipe, credential_param, parse_curl, run

URL = "https://portal.example/api/search"
FAKE = "fake-canary"


def _recipe(res: dict) -> dict:
    return res["entry"]["recipes"][0]


def _refused(curl: str, match: str) -> str:
    with pytest.raises(ValueError, match=match) as e:
        parse_curl(curl)
    assert FAKE not in str(e.value)
    return str(e.value)


CREDENTIAL_PARAMS = [
    "api_key", "apiKey", "APIKey", "apikey", "key", "x-api-key", "subscription-key",
    "access_token", "accessToken", "token", "$$app_token", "csrf_token", "_csrf",
    "csrfmiddlewaretoken", "__RequestVerificationToken", "authenticity_token", "auth",
    "oauth_signature", "X-Amz-Signature", "X-Amz-Credential", "sig", "client_secret",
    "password", "passwd", "pass", "sessionid", "session_id", "SessionId", "jsessionid",
    "PHPSESSID", "sid", "otp", "auth[token]", "user[password]", "pw", "cred", "creds",
    "session_ids", "sessionIds", "authcode", "authkey", "key1", "apiKey2", "api_keys",
    "userkey", "devkey", "licensekey", "sesskey", "appid", "_wpnonce", "nonce",
    "SAMLResponse", "authentication", "pswd",
]
ORDINARY_PARAMS = [
    "session", "Session", "legislative_session", "session_year", "sessionYear", "author",
    "authorId", "authority", "passed", "passage", "secretary", "credit", "keyword", "pin",
    "parcel_pin", "q", "page", "per_page", "jurisdiction", "queryField", "filterValue",
    "turkey", "hockey", "turnkey", "ticket_number", "sess", "keys", "sort_keys", "sign",
    "noncertified",
]


@pytest.mark.parametrize("name", CREDENTIAL_PARAMS)
def test_credential_parameter_names(name):
    assert credential_param(name)


@pytest.mark.parametrize("name", ORDINARY_PARAMS)
def test_ordinary_parameter_names(name):
    """`sess`, `auth`, `pass` and `pin` anywhere in a header name are credentials. In a
    parameter's they are a legislative session, a byline, a bill's passage and a parcel, and
    `keys` is a site search box. `ticket` is left out on purpose: in public records it is a
    citation, and a single-sign-on ticket is spent by the time the browser shows the page."""
    assert not credential_param(name)


@pytest.mark.parametrize("name", ["api_key", "token", "sig", "access_token", "PHPSESSID"])
def test_a_credential_in_the_query_string_is_refused_and_named(name):
    msg = _refused(f"curl '{URL}?q=x&{name}={FAKE}'", "credentials in its URL")
    assert f"({name})" in msg


@pytest.mark.parametrize("url", [
    f"{URL}?q=x;token={FAKE}",                       # `;` separates pairs on some servers
    f"{URL}?api%5Fkey={FAKE}",                        # the name as the server decodes it
    f"https://portal.example/app;jsessionid={FAKE}/search",
    f"{URL}#access_token={FAKE}",
    f"{URL}?filter=" + '{"apiKey":"' + FAKE + '"}',  # JSON in a parameter's value
    f"{URL}?filter=" + '{"q":"a;b","apiKey":"' + FAKE + '"}',  # a `;` inside that JSON
    f"{URL}?next=https%3A%2F%2Fportal.example%2Fcb%3Faccess_token%3D{FAKE}",  # a URL in a value
    f"{URL}?next=/search?token={FAKE}",
    f"{URL}?next=search%3Ftoken%3D{FAKE}",                    # a relative link
    f"{URL}?q=%3Ftoken%3D{FAKE}",                             # a bare query
])
def test_every_parameter_the_url_carries_is_checked(url):
    _refused(f"curl '{url}'", "credentials in its URL")


@pytest.mark.parametrize("body", [
    f"q=x&csrf_token={FAKE}",
    f"__RequestVerificationToken={FAKE}&q=x",
    f"user=canary&password={FAKE}",
    "data=" + '{"token":"' + FAKE + '"}',
    f"callback=https%3A%2F%2Fportal.example%2Fcb%3Fsig%3D{FAKE}",
])
def test_a_credential_in_a_form_body_is_refused(body):
    _refused(f"curl -H 'content-type: application/x-www-form-urlencoded' -d '{body}' {URL}",
             "credentials in its body")


@pytest.mark.parametrize("fields", [
    {"q": "x", "token": FAKE},
    {"query": {"filters": [{"field": "name", "apiKey": FAKE}]}},
    {"payload": json.dumps({"session_id": FAKE})},
    {"callback": f"https://portal.example/cb?token={FAKE}"},
    {"data": f"csrf_token={FAKE}&q=1"},
])
def test_a_credential_in_a_json_body_is_refused_at_any_depth(fields):
    _refused(f"curl -H 'content-type: application/json' -d '{json.dumps(fields)}' {URL}",
             "credentials in its body")


def test_a_body_without_a_content_type_is_read_as_curl_sends_it():
    """curl sends `-d` as a form unless told otherwise, and JSON is recognised by parsing."""
    _refused(f"curl -d 'q=x&token={FAKE}' {URL}", "in its body")
    _refused(f"""curl -d '{{"token": "{FAKE}"}}' {URL}""", "in its body")


@pytest.mark.parametrize("content_type, body", [
    ("multipart/form-data; boundary=x",
     f'--x\\r\\nContent-Disposition: form-data; name="csrf_token"\\r\\n\\r\\n{FAKE}\\r\\n--x--'),
    ("text/xml", f"<request><token>{FAKE}</token></request>"),
    ("application/json", f"not json {FAKE}"),
])
def test_a_body_whose_fields_cant_be_read_is_refused(content_type, body):
    """A multipart form carries its CSRF token in a part the importer can't read."""
    _refused(f"curl -H 'content-type: {content_type}' --data-raw '{body}' {URL}",
             "neither JSON nor form-encoded")


def test_json_nested_too_deeply_to_read_is_refused_not_skipped():
    deep = '{"a": ' * 5000 + f'"{FAKE}"' + "}" * 5000
    _refused(f"curl -H 'content-type: application/json' -d '{deep}' {URL}", "too deeply")
    _refused(f"curl '{URL}?filter={deep}'", "too deeply")


# #155's reproduction. Reading this body as JSON raised the nested URL's refusal, and the
# `except` meant for a body that isn't JSON swallowed it along with `api_key`. The body then
# went to the form reading, which finds no `=` in a JSON object, and it was recorded.
UNPARSEABLE_NEXT = '{"api_key":"SECRET","next":"http://[bad"}'


@pytest.mark.parametrize("content_type", ["", "application/x-www-form-urlencoded",
                                          "application/json"])
def test_a_json_body_holding_a_url_that_cant_be_parsed_is_refused(content_type):
    header = f" -H 'Content-Type: {content_type}'" if content_type else ""
    curl = f"curl 'https://example.gov/api'{header} --data-raw '{UNPARSEABLE_NEXT}'"
    with pytest.raises(ValueError, match="a URL in a parameter can't be parsed") as e:
        parse_curl(curl)
    assert "SECRET" not in str(e.value)
    # The same body without the URL is refused for the credential it names.
    with pytest.raises(ValueError, match=r"in its body \(api_key\)"):
        parse_curl(curl.replace(',"next":"http://[bad"', ""))


@pytest.mark.parametrize("curl", [
    # Deeper in a JSON body, where the refusal passes through more of the reading.
    "curl -d '" + json.dumps({"q": {"filters": [{"api_key": FAKE, "links": ["http://[bad"]}]}})
    + f"' {URL}",
    # JSON in a form field.
    "curl -d 'payload=" + json.dumps({"api_key": FAKE, "next": "http://[bad"}) + f"&a=b' {URL}",
    # A URL in a URL, in the query.
    f"curl '{URL}?api_key={FAKE}&next=/cb?u=http://[bad'",
])
def test_a_url_that_cant_be_parsed_is_refused_wherever_it_sits(curl):
    _refused(curl, "a URL in a parameter can't be parsed")


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/x-www-form-urlencoded"},
                                     {"Content-Type": "application/json"}])
def test_run_refuses_a_json_body_holding_a_url_that_cant_be_parsed(headers, monkeypatch):
    sent = _sent(monkeypatch)
    r = Recipe(id="bad", method="POST", url="https://example.gov/api", headers=headers,
               body=UNPARSEABLE_NEXT)
    with pytest.raises(ValueError, match="a URL in a parameter can't be parsed") as e:
        run(r, {})
    assert "SECRET" not in str(e.value) and not sent


def test_a_json_string_body_sent_as_a_form_is_read_as_a_form_too():
    """The server reads a body the way its content type says, whatever it looks like."""
    _refused(f"""curl -d '"q=x&token={FAKE}"' {URL}""", "in its body")


@pytest.mark.parametrize("key, value", [
    (f"token {FAKE}", FAKE),                        # a pair that lost its `=`
    ("sessid_4f9a1c2e7b", "4f9a1c2e7b"),            # a map keyed by session id
    ("sessid_QhFkZpLmNaBx", "QhFkZpLmNaBx"),        # mixed case splits into short words
    ("sessid_Q2hFk9ZpLmNaBx", "Q2hFk9ZpLmNaBx"),
    ("sessid_deadbeefcafebabe", "deadbeefcafebabe"),
])
def test_a_name_that_may_hold_a_value_is_described_not_repeated(key, value):
    fields = {"sessions": {key: {}}}
    msg = _refused(f"curl -H 'content-type: application/json' -d '{json.dumps(fields)}' {URL}",
                   "may hold a value")
    assert value not in msg


def test_a_url_header_is_checked_too():
    """The referer loses its query on import; an origin carrying one is refused."""
    _refused(f"curl -H 'Origin: https://portal.example/?token={FAKE}' {URL}",
             "credentials in its origin header")


def test_a_legislative_session_still_imports():
    query = "session=2025-2026&sess=CUR&author=Doe&keys=budget"
    assert _recipe(parse_curl(f"curl '{URL}?{query}'"))["url"] == f"{URL}?{query}"


def test_a_lone_padded_value_is_not_read_as_a_pair():
    """Base64 ends in `=`, and read as `name=value` its letters can spell `Key`."""
    body = "state=abcKeyXyz%3D%3D&q=x"
    assert _recipe(parse_curl(f"curl -d '{body}' {URL}"))["body"] == body


def test_a_url_nested_in_urls_is_read_once():
    """Every pair is read two ways, so without a cache each level of nesting doubled the work:
    a 130-character paste ran for minutes."""
    start = time.monotonic()
    _refused(f"curl '{URL}?" + "a=/x?" * 40 + f"token={FAKE}'", "credentials in its URL")
    assert time.monotonic() - start < 5


@pytest.mark.parametrize("content_type, body", [
    ("application/x-www-form-urlencoded", "session=2025-2026&passed=true&pin=000-111-222"),
    ("application/json", json.dumps({"session": "2025-2026", "filters": [{"authority": "x"}]})),
])
def test_ordinary_body_fields_still_import(content_type, body):
    res = parse_curl(f"curl -H 'content-type: {content_type}' -d '{body}' {URL}")
    assert _recipe(res)["body"] == body


def test_the_registry_passes_its_own_parameter_check():
    """The rule must not refuse the recipes already recorded."""
    for entry in access.load_all().values():
        for r in entry.recipes:
            assert access._credential_params(r.url, r.headers, r.body) == [], r.id


def test_import_command_writes_nothing_for_a_credential_parameter(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from provenance import cli

    monkeypatch.setattr(access, "REGISTRY", tmp_path / "access")
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl '{URL}?q=x&api_key={FAKE}'\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)])
    assert r.exit_code == 1
    assert "api_key" in r.output and FAKE not in r.output
    assert not (tmp_path / "access").exists()


# run(): a registry entry is edited by hand, so it is checked again, after filling.

def _sent(monkeypatch) -> dict:
    sent: dict = {}
    monkeypatch.setattr(access.httpx, "request",
                        lambda method, url, **kw: sent.update(method=method, url=url, **kw))
    return sent


@pytest.mark.parametrize("recipe, match", [
    (Recipe(id="bad", method="GET", url=f"{URL}?q=x&api_key={FAKE}"), "in its URL"),
    (Recipe(id="bad", method="POST", url=URL, body=f"q=x&csrf_token={FAKE}"), "in its body"),
    (Recipe(id="bad", method="POST", url=URL, headers={"Content-Type": "application/json"},
            body=json.dumps({"auth": {"token": FAKE}})), "in its body"),
    (Recipe(id="bad", method="POST", url=URL, headers={"Content-Type": "multipart/form-data"},
            body=f"--x\r\n{FAKE}\r\n--x--"), "neither JSON nor form-encoded"),
    (Recipe(id="bad", method="GET", url=URL,
            headers={"Referer": f"https://portal.example/app?csrf_token={FAKE}"}),
     "in its referer header"),
])
def test_run_refuses_what_the_importer_refuses(recipe, match, monkeypatch):
    sent = _sent(monkeypatch)
    with pytest.raises(ValueError, match=match) as e:
        run(recipe, {})
    assert FAKE not in str(e.value) and not sent


def test_run_refuses_a_credential_that_a_param_puts_in_the_url(monkeypatch):
    sent = _sent(monkeypatch)
    r = Recipe(id="bad", method="GET", url=URL + "?q={q}", params=["q"])
    with pytest.raises(ValueError, match="api_key") as e:
        run(r, {"q": f"x&api_key={FAKE}"})
    assert FAKE not in str(e.value) and not sent


def test_run_sends_a_legislative_session_and_a_json_template(monkeypatch):
    sent = _sent(monkeypatch)
    r = Recipe(id="ok", method="POST", url=URL + "?session={session}", params=["session", "last"],
               headers={"content-type": "application/json"},
               body='{"filters": [{"field": "LastName", "value": "{last}"}]}')
    run(r, {"session": "2025-2026", "last": "Doe"})
    assert sent["url"] == f"{URL}?session=2025-2026"
    assert json.loads(sent["content"]) == {"filters": [{"field": "LastName", "value": "Doe"}]}


def test_a_run_refusal_prints_bracketed_names_intact(tmp_path, monkeypatch):
    """Rich reads `[token]` as markup, which showed `auth[token]` as `auth`."""
    from typer.testing import CliRunner

    from provenance import cli

    reg = tmp_path / "access"
    reg.mkdir()
    (reg / "portal.example.yaml").write_text(
        "host: portal.example\naccess: api\nrecipes:\n- id: bad\n  method: GET\n"
        "  url: https://portal.example/api?auth[token]={value}\n  params: [value]\n")
    monkeypatch.setattr(access, "REGISTRY", reg)
    r = CliRunner().invoke(cli.app, ["source-access", "portal.example", "--run-recipe", "bad",
                                     "--param", f"value={FAKE}"])
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", r.output).split())
    assert r.exit_code == 1
    assert "(auth[token])" in out and FAKE not in out
