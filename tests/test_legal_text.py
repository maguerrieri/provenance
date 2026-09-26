"""Legal text is a series, like a filing (#41): the version of a code section before its last
amendment verifies exactly like the one in force. So a citation to legal text must carry the
effective date or version it quotes, which hosts publish legal text is source-list data, and
the verifier is told to check for a newer version and to read what a quoted term is defined as."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import write_project
from typer.testing import CliRunner

from provenance import cli, sources
from provenance.fetch import cache_path
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source
from provenance.verify import missing_legal_version

SECTION = "https://codes.example.gov/title-4/section-120"
SNIPPET = "no permit shall issue for a structure within the setback"


def _source(url=SECTION, date=None, **kw):
    d = dict(url=url, publisher="Example County", author="Example County Board",
             source_type="primary_document", snippet=SNIPPET, date=date)
    d.update(kw)
    return Source(**d)


def _lists(tmp_path, text: str, name="x"):
    d = tmp_path / "lists"
    d.mkdir(exist_ok=True)
    (d / f"{name}-sources.yaml").write_text(text)
    return str(d)


def test_an_undated_citation_on_a_legal_text_host_is_missing_its_version(tmp_path):
    rules = sources.load_rules(("x",), _lists(tmp_path, "legal_text:\n  - codes.example.gov\n"))
    assert missing_legal_version(_source(), rules)
    assert missing_legal_version(_source(date="  "), rules), "blank is no version"
    assert missing_legal_version(_source(url="https://www.codes.example.gov/s/1"), rules)
    assert missing_legal_version(_source(url="https://library.codes.example.gov/s/1"), rules)
    for version in ("operative 2025-01-01", "as amended by Ordinance 12-24, effective 2024-07-01",
                    "2025-01-01"):
        assert not missing_legal_version(_source(date=version), rules), version
    # A placeholder fills the field and names no version, as "staff" names no author.
    for placeholder in ("n/a", "N/A", "none", "Unknown", "undated", "-", "\u2014", "?", "TBD"):
        assert missing_legal_version(_source(date=placeholder), rules), placeholder
    # Not every host is legal text: an undated article is untidy, not a series defect.
    assert not missing_legal_version(_source(url="https://news.example/a"), rules)
    assert not missing_legal_version(_source(url="https://notcodes.example.gov/a"), rules)


def test_a_placeholder_is_no_date_for_a_filing_either():
    """The two series checks read `date` one way: a filing dated "n/a" is as undated as one
    with no date at all."""
    from provenance.verify import missing_filing_date

    filing = dict(url="https://www.fppc.ca.gov/documents/700.pdf", publisher="FPPC",
                  author="FPPC", source_type="primary_document")
    assert missing_filing_date(_source(**filing, date="n/a"))
    assert not missing_filing_date(_source(**filing, date="2025-04-01"))


def test_which_hosts_publish_legal_text_is_list_data_not_code(tmp_path):
    """A project elsewhere names its own jurisdiction's hosts in its source lists, and nothing
    in the core knows one: with no list naming it, no host is legal text."""
    none = sources.load_rules(("x",), _lists(tmp_path, "bylined_journalism: []\n"))
    for url in (SECTION, "https://uscode.house.gov/view.xhtml?req=x",
                "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml"):
        assert not missing_legal_version(_source(url=url), none), url
    shipped = sources.load_rules(("us", "ca"))
    assert missing_legal_version(_source(url="https://uscode.house.gov/view.xhtml?req=x"),
                                 shipped)
    assert not missing_legal_version(_source(), shipped), "codes.example.gov is on no list"


def test_a_legal_text_host_keeps_the_class_another_key_gives_it(tmp_path):
    """`legal_text` is not a class: a multi-jurisdiction code publisher listed there is not
    thereby the issuing authority for every code it hosts (#40)."""
    rules = sources.load_rules(("x",), _lists(tmp_path, (
        "primary_document:\n  - legislature.example.gov\n"
        "legal_text:\n  - legislature.example.gov\n  - codes.example.com\n")))
    assert sources.classify("https://legislature.example.gov/s", rules) == "primary_document"
    assert sources.classify("https://codes.example.com/s", rules) == "unknown"


def test_every_shipped_source_list_loads():
    """`load_rules()` refuses a malformed list, so a typo in a shipped one fails here, not in a
    run."""
    for name in sources.available():
        rules = sources.load_rules((name,))
        assert set(rules) == set(sources.KEYS), name


NOT_A_LIST = "'legal_text' must be a list of host names"
NOT_A_HOST = "'legal_text' holds what is not a bare host name"


@pytest.mark.parametrize("text, says", [
    ("legal_texts:\n  - codes.example.gov\n", "'legal_texts'"),
    ("legal_text:\n  - codes.example.gov\nlegal_text:\n  - more.example.gov\n",
     "'legal_text' is given twice"),
    ("legal_text: codes.example.gov\n", NOT_A_LIST),
    ("legal_text: ''\n", NOT_A_LIST),
    ("legal_text: 0\n", NOT_A_LIST),
    ("legal_text: {}\n", NOT_A_LIST),
    ("legal_text:\n  - 12\n", NOT_A_HOST),
    ("legal_text:\n  - ''\n", NOT_A_HOST),
    ("legal_text:\n  - https://codes.example.gov\n", NOT_A_HOST),
    ("legal_text:\n  - codes.example.gov/\n", NOT_A_HOST),
    ("legal_text:\n  - codes.example.gov/title-4\n", NOT_A_HOST),
    ("legal_text:\n  - codes.example.gov:443\n", NOT_A_HOST),
    ("legal_text:\n  - www.codes.example.gov\n", NOT_A_HOST),
    ("legal_text:\n  - localhost\n", NOT_A_HOST),
    ("legal_text: [codes.example.gov\n", "can't be read"),
    ("- codes.example.gov\n", "is not a mapping"),
], ids=["misspelled key", "repeated key", "bare host", "empty string", "zero", "empty mapping",
        "number", "empty host", "scheme", "trailing slash", "path", "port", "www", "no dot",
        "not yaml", "not a mapping"])
def test_a_malformed_source_list_is_refused(tmp_path, text, says):
    """Each of these fails open if read past: a misspelled or repeated `legal_text` lists no
    host, or only some, and every undated statute on the rest passes; a host written without
    the list's `-` is read one letter at a time; and a host in any shape `domain()` never
    returns matches no URL."""
    with pytest.raises(ValueError, match=re.escape(says)):
        sources.load_rules(("x",), _lists(tmp_path, text))


def test_an_empty_key_lists_no_host(tmp_path):
    rules = sources.load_rules(("x",), _lists(tmp_path, "legal_text:\nprimary_document: []\n"))
    assert rules[sources.LEGAL_TEXT] == () and rules["primary_document"] == ()


def test_a_malformed_list_is_a_problem_with_the_project(tmp_path, monkeypatch):
    """Every command loads the project before its rules, so a list that can't be read is named
    there, with the project's other problems, and not as a traceback from whichever command
    reads the rules first."""
    from provenance import project

    monkeypatch.setattr(sources, "SOURCES_DIR", Path(_lists(tmp_path, "legal_texts: []\n")))
    sources.load_rules.cache_clear()
    root = write_project(tmp_path / "p", sources=("x",))
    with pytest.raises(project.UnreadableProject, match=re.escape("key(s) it can't use: "
                                                                  "'legal_texts'")):
        project.load(root)
    sources.load_rules.cache_clear()


def _provenance(monkeypatch, *args):
    monkeypatch.setattr(cli.con, "_width", 10_000)   # rich folds a long tmp path at 80 columns
    res = CliRunner().invoke(cli.app, [str(a) for a in args])
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


@pytest.mark.parametrize("url, ack", [
    ("https://uscode.house.gov/view.xhtml?req=granuleid:USC-prelim-title99-section1", None),
    # A publisher serving many jurisdictions is not listed as primary (#40), so citing a code
    # there is acknowledged as a copy: that check is not the one under test.
    ("https://library.municode.com/xx/example_county/codes/code_of_ordinances",
     "the county publishes its code only through this host"),
], ids=["federal code", "code publisher"])
def test_check_claim_fails_legal_text_with_no_effective_date_or_version(tmp_path, monkeypatch,
                                                                         url, ack):
    """The acceptance test: a code citation that passes every mechanical check still fails
    `check-claim` until it says which version it quotes."""
    root = write_project(tmp_path / "data")
    page = PageCache(url=url, final_url=url, status=200, content_type="text/html", title="T",
                     text=f"(b) Except as provided in Section 3, {SNIPPET} line.",
                     fetched_at=datetime.now(UTC) - timedelta(hours=1),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(root, url).write_text(page.model_dump_json())
    path = root / "claims" / "q1.json"
    path.parent.mkdir()

    def check(date):
        cited = _source(url=url, date=date, author="Code office", secondary_host_ack=ack)
        claim = Claim(question_id="q1", question="What does the code say about setbacks?",
                      answer="It bars a permit within the setback.", sources=[cited])
        path.write_text(claim.model_dump_json())
        return _provenance(monkeypatch, "check-claim", path, "--data", root)

    code, out = check(None)
    assert code == 1, out
    assert "verified" in out, "the quote is on the page: only the version is missing"
    assert f"no effective date or version {url}" in out, out
    assert "set `date` to the effective date or version of what you quoted" in out, out
    assert "for a bill, its version, or the date of the action you cite" in out, out

    code, out = check("n/a")
    assert code == 1 and f"no effective date or version {url}" in out, out

    code, out = check("current through 2026-01-15")
    assert code == 0 and "All sources check out." in out, out

