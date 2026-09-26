"""A project names the hosts that issue its own records (#179).

`secondary_host()` reads "not listed as a primary-document host" as "not the issuing authority".
The lists ship with the tool and cover a few jurisdictions, so a project anywhere else met it on
its first primary source, the issuing body's own site included. Both ways past it were wrong: an
acknowledgment badged the authority's own record as a copy, and relabelling it `own_statement`
left the source class saying something false. `primary_hosts` in the project file names them.
The rule itself stays: a host neither the lists nor the project name reads as a copy.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import write_project
from selectolax.parser import HTMLParser
from typer.testing import CliRunner

from provenance import cli, project, sources
from provenance.fetch import cache_path
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source
from provenance.report import render, store_id
from provenance.sources import classify, load_rules, tier
from provenance.verify import check_corroboration, secondary_host, unacked_copy

AUTHORITY = "records.example.gov"
URL = f"https://{AUTHORITY}/decisions/2030-14"
MIRROR = "https://mirror.example.com/decisions/2030-14"
SNIPPET = "the board adopted the revised fee schedule by resolution"
LISTS = """\
primary_document:
  - agency.example.gov
bylined_journalism:
  - news.example.com
lead_generator_only:
  - wiki.example.org
excluded:
  - farm.example.net
campaign_statement_only:
  - campaign.example.org
"""


@pytest.fixture
def synthetic_lists(tmp_path, monkeypatch):
    """The tool's lists replaced by one synthetic list, `xx`, with a host in every class."""
    d = tmp_path / "lists"
    d.mkdir()
    (d / "xx-sources.yaml").write_text(LISTS)
    monkeypatch.setattr(sources, "SOURCES_DIR", d)
    load_rules.cache_clear()   # keyed by list names, so it would still hold the real `xx`-less lists
    yield
    load_rules.cache_clear()


def _load(tmp_path: Path, hosts, source_lists=("us",)) -> project.Project:
    root = write_project(tmp_path / "p", sources=source_lists,
                         extra=f"primary_hosts = {hosts}\n")
    return project.load(root)


def _refusal(tmp_path: Path, hosts, source_lists=("us",)) -> str:
    with pytest.raises(project.UnreadableProject) as e:
        _load(tmp_path, hosts, source_lists)
    return str(e.value)


def _official(url: str = URL, source_type: str = "official_record", **kw) -> Source:
    return Source(url=url, publisher="Example Records Office", author="Example Records Office",
                  date="2030-03-02", source_type=source_type, snippet=SNIPPET, **kw)


# --- the project file ---------------------------------------------------------------------------


def test_a_project_without_primary_hosts_names_none(tmp_path):
    assert project.load(write_project(tmp_path / "p")).primary_hosts == ()


def test_primary_hosts_are_read_as_hosts_classify_matches(tmp_path):
    p = _load(tmp_path, f'[" Records.Example.GOV ", "{AUTHORITY}", "tracker.example.org"]')
    assert p.primary_hosts == (AUTHORITY, "tracker.example.org")


@pytest.mark.parametrize("value", ['"records.example.gov"', "[1]", '[["a.example.gov"]]'])
def test_primary_hosts_must_be_a_list_of_host_names(tmp_path, value):
    assert "`primary_hosts` must list the hosts" in _refusal(tmp_path, value)


def test_every_entry_that_is_not_a_host_is_named_at_once(tmp_path):
    """Each would fail open: a scheme, path, port or `www.` matches no URL, so the real host's
    records still read as copies, and one label makes a whole top-level domain an authority."""
    bad = ["https://a.example.gov", "a.example.gov/records", "a.example.gov:8443",
           "www.a.example.gov", "gov", "", "a .example.gov"]
    msg = _refusal(tmp_path, str(bad).replace("'", '"'))
    for entry in bad:
        assert repr(entry) in msg, (entry, msg)
    assert "more than a top-level domain" in msg, msg


@pytest.mark.parametrize("host, cls", [
    ("farm.example.net", "excluded"),
    ("sub.farm.example.net", "excluded"),
    ("wiki.example.org", "lead_generator_only"),
    ("campaign.example.org", "campaign_statement_only"),
    ("news.example.com", "bylined_journalism"),
])
def test_a_host_the_lists_class_cannot_be_named_an_authority(tmp_path, synthetic_lists, host,
                                                              cls):
    """The most restrictive class wins, so an excluded host named here would stay excluded while
    every brief called it an issuing authority. A news outlet would stop being one, and its copy
    of a record would pass as the record."""
    msg = _refusal(tmp_path, f'["{host}"]', ("xx",))
    assert f"`primary_hosts` names {host!r}, which the project's source lists class as {cls}" \
        in msg, msg


def test_a_host_above_a_news_outlet_cannot_be_named_an_authority(tmp_path, synthetic_lists):
    """Primary documents are classed before journalism, so naming `example.com` would make every
    story on `news.example.com` a primary record, its copies of records included."""
    msg = _refusal(tmp_path, '["example.com"]', ("xx",))
    assert ("`primary_hosts` names 'example.com', which covers 'news.example.com', a news outlet "
            "on the project's source lists") in msg, msg
    # A host above an excluded one changes nothing there: the most restrictive class still wins.
    p = _load(tmp_path, '["example.net"]', ("xx",))
    assert classify("https://farm.example.net/x", p.rules()) == "excluded"


def test_a_host_the_lists_already_name_as_an_authority_is_allowed(tmp_path, synthetic_lists):
    p = _load(tmp_path, '["agency.example.gov", "records.example.gov"]', ("xx",))
    assert p.primary_hosts == ("agency.example.gov", AUTHORITY)


def test_a_bad_entry_is_named_beside_a_bad_list(tmp_path):
    """Every problem in one message, so a repair is not a loop of re-runs."""
    msg = _refusal(tmp_path, '["gov"]', ("nosuchlist",))
    assert "no such source list: 'nosuchlist'" in msg and "'gov'" in msg, msg


# --- the rules every command reads ------------------------------------------------------------


def test_the_projects_rules_count_its_hosts_as_issuing_authorities(tmp_path):
    p = _load(tmp_path, f'["{AUTHORITY}"]')
    rules = p.rules()
    assert classify(URL, rules) == "primary_document"
    assert classify(f"https://www.{AUTHORITY}/x", rules) == "primary_document"
    assert classify(f"https://archive.{AUTHORITY}/x", rules) == "primary_document"
    assert classify(MIRROR, rules) == "unknown"
    # Everything the lists hold is still there.
    assert set(load_rules(p.sources)["excluded"]) <= set(rules["excluded"])
    assert classify("https://grokipedia.com/x", rules) == "excluded"


def test_the_authoritys_own_record_is_no_copy_and_a_copy_still_is(tmp_path):
    """The rule stays: a primary record from a host nobody names as its issuer needs its ack."""
    p = _load(tmp_path, f'["{AUTHORITY}"]')
    assert not secondary_host(_official(), p.rules())
    assert secondary_host(_official(MIRROR), p.rules())
    # What the lists alone said, and what check-claim said before this.
    assert secondary_host(_official(), load_rules(p.sources))


def test_no_command_reads_the_lists_without_the_projects_hosts():
    """`Project.rules()` is the one way a command gets its rules. A command that passed the
    project's lists to `load_rules()` itself would check against the lists alone, and call every
    record from a host the project names a copy."""
    src = Path(cli.__file__).parent
    for f in sorted(src.glob("*.py")):
        if f.name in ("sources.py", "project.py"):
            continue
        assert "load_rules(" not in f.read_text(), f.name


# --- what the researcher is told, and what check-claim says ------------------------------------


def test_the_brief_names_every_issuing_authority(tmp_path, synthetic_lists):
    p = _load(tmp_path, f'["{AUTHORITY}"]', ("xx",))
    brief = cli.researcher_brief(p, None)
    assert brief.endswith("Issuing authorities, each with its subdomains: agency.example.gov, "
                          f"{AUTHORITY}\n{cli.ISSUING_AUTHORITIES}"), brief


def test_the_brief_says_what_to_do_about_an_unnamed_authority():
    """The one place a researcher is told, beside the lists' notes (researcher.md points here),
    so the two can't drift."""
    rule = " ".join(cli.ISSUING_AUTHORITIES.split())
    for said in ("cited from any other host is a copy", "set `secondary_host_ack`",
                 "don't acknowledge a copy, don't relabel the source, and don't edit the "
                 "project file", "Hand the claim on with `notes` naming the host",
                 "a person adds it to the project's `primary_hosts`"):
        assert said in rule, said
    researcher = " ".join((Path(cli.__file__).parents[2] / "agents" / "researcher.md")
                          .read_text().split())
    assert "Hand the claim on with `notes` naming the host" not in researcher
    assert "an issuing authority the brief doesn't name (the brief says how)" in researcher


def test_the_brief_says_when_none_are_named(tmp_path, synthetic_lists):
    (sources.SOURCES_DIR / "yy-sources.yaml").write_text("bylined_journalism:\n  - paper.example\n")
    p = project.load(write_project(tmp_path / "p", sources=("yy",)))
    assert "Issuing authorities, each with its subdomains: none named\n" \
        in cli.researcher_brief(p, None)


def _provenance(monkeypatch, *args):
    monkeypatch.setattr(cli.con, "_width", 10_000)   # rich folds a long tmp path mid-word
    res = CliRunner().invoke(cli.app, [str(a) for a in args])
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _page(root: Path, url: str) -> None:
    """A page at `url`, cached under `root`, holding SNIPPET once."""
    cache_path(root, url).write_text(PageCache(
        url=url, final_url=url, status=200, content_type="text/html", title="Decision 2030-14",
        text=f"At its March meeting {SNIPPET}, effective in April.",
        fetched_at=datetime.now(UTC) - timedelta(hours=6),
        extractor_version=EXTRACTOR_VERSION).model_dump_json())


def _claim_citing(root: Path, url: str, snippet: str = SNIPPET) -> Path:
    """`root/claims/q1.json`, citing an official record at `url` cached under `root`, so
    `provenance check-claim` passes it offline on everything but where it is from."""
    _page(root, url)
    path = root / "claims" / "q1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    source = _official(url)
    source.snippet = snippet
    path.write_text(Claim(question_id="q1", question="What fee schedule did the board adopt?",
                          answer="The revised one.", sources=[source]).model_dump_json())
    return path


def test_check_claim_takes_a_named_authoritys_record_as_its_own(tmp_path, monkeypatch):
    root = write_project(tmp_path / "p", sources=("us",))
    path = _claim_citing(root, URL)
    code, out = _provenance(monkeypatch, "check-claim", path, "--data", root)
    assert code == 1, out
    assert (f"secondary host {AUTHORITY} is not one of the project's issuing authorities "
            f"(`provenance brief` lists them)") in out, out
    # Both ways on: acknowledge a real copy, or, for the authority itself, don't relabel it.
    assert "set `secondary_host_ack` saying what you could not reach" in out, out
    assert (f"If {AUTHORITY} is itself the body that issues this record, do neither, and "
            f"don't relabel the source: hand the claim on with `notes` naming the host") \
        in out, out
    # The copy is the only failure: the close gives the hand-on path, never "do not hand this
    # on", and the claim's corroboration failing on that copy alone isn't a second thing to fix.
    assert ("Only the issuing-authority check failed. If the host is the body that issues the "
            "record, hand this on with `notes` naming it") in out, out
    assert "do not hand this on" not in out and "corroboration" not in out, out

    write_project(root, sources=("us",), extra=f'primary_hosts = ["{AUTHORITY}"]\n')
    code, out = _provenance(monkeypatch, "check-claim", path, "--data", root)
    assert code == 0 and "issuing authorit" not in out, out


def test_check_claim_still_fails_a_copy_on_a_host_nobody_names(tmp_path, monkeypatch):
    root = write_project(tmp_path / "p", sources=("us",),
                         extra=f'primary_hosts = ["{AUTHORITY}"]\n')
    path = _claim_citing(root, MIRROR)
    code, out = _provenance(monkeypatch, "check-claim", path, "--data", root)
    assert code == 1, out
    assert "secondary host mirror.example.com is not one of the project's issuing" in out, out


def test_another_failure_beside_the_copy_is_the_researchers_to_fix(tmp_path, monkeypatch):
    """Only an unnamed authority is handed on. Anything else wrong keeps the old close."""
    root = write_project(tmp_path / "p", sources=("us",))
    path = _claim_citing(root, MIRROR, snippet="the board rejected the fee schedule outright")
    code, out = _provenance(monkeypatch, "check-claim", path, "--data", root)
    assert code == 1 and "snippet_not_found" in out, out
    assert "secondary host mirror.example.com is not one of" in out, out
    assert "do not hand this on" in out and "Only the issuing-authority" not in out, out


def test_a_trailing_dot_names_the_same_host(tmp_path):
    """`example.gov.` is fully qualified `example.gov`. Left on, it matched no entry, so an
    excluded host's page passed as unlisted, and a named authority's read as a copy."""
    p = _load(tmp_path, f'["{AUTHORITY}."]')
    assert p.primary_hosts == (AUTHORITY,)
    assert classify(f"https://{AUTHORITY}./x", p.rules()) == "primary_document"
    assert classify("https://grokipedia.com./x", p.rules()) == "excluded"
    assert _refusal(tmp_path, '["gov."]')


def test_the_secondary_host_message_shows_a_hosts_format_character(tmp_path, monkeypatch):
    """The URL check refuses C0 controls, not a bidi override, which would reorder the rest of
    the line in both places the host is printed."""
    rlo = chr(0x202E)
    root = write_project(tmp_path / "p", sources=("us",))
    path = _claim_citing(root, f"https://a{rlo}b.example.com/x")
    code, out = _provenance(monkeypatch, "check-claim", path, "--data", root)
    assert code == 1 and rlo not in out, out
    assert "secondary host a\\u202eb.example.com is not one of" in out, out
    assert "If a\\u202eb.example.com is itself the body" in out, out


# --- build and check-claim agree about a named authority --------------------------------------


def test_a_named_authority_issues_its_own_official_analysis(tmp_path):
    """#188 gates an official analysis like a primary record: a body issues it, and the label
    alone must not raise a page anywhere else to that tier. A host the project names issues its
    analyses as well as its records."""
    p = _load(tmp_path, f'["{AUTHORITY}"]')
    analysis = _official(source_type="official_analysis")
    assert not secondary_host(analysis, p.rules()) and not unacked_copy(analysis, p.rules())
    assert tier(analysis, p.rules()) == "official_analysis"
    assert unacked_copy(_official(MIRROR, source_type="official_analysis"), p.rules())
    # What the lists alone said.
    assert unacked_copy(analysis, load_rules(p.sources))


def test_corroboration_reads_the_projects_hosts(tmp_path):
    """#188 fails a claim's corroboration on an unacknowledged copy, in build as in check-claim,
    whatever path set its row's status (an archive upgrade included). Both read the project's
    rules with its `primary_hosts`, or they would disagree about the same host."""
    unnamed = _load(tmp_path, "[]").rules()
    named = _load(tmp_path, f'["{AUTHORITY}"]').rules()

    def corroborated(src: Source, rules) -> bool | None:
        claim = Claim(question_id="q1", question="Q?", answer="A.", sources=[src])
        return check_corroboration(claim, rules=rules).corroboration_ok

    for status in ("verified", "verified_via_archive", "could_not_verify_paywall"):
        src = _official()
        src.verification.status = status
        assert corroborated(src, unnamed) is False, status
        assert corroborated(src.model_copy(deep=True), named) is True, status

    # Judged, the claim renders green only where the host is named.
    src = _official()
    src.verification.status, src.verification.support = "verified", "supports"
    claim = Claim(question_id="q1", question="Q?", answer="A.", sources=[src])
    assert check_corroboration(claim, rules=unnamed).status == "human_review"
    assert check_corroboration(claim, rules=named).status == "verified"


def test_build_holds_the_claim_until_the_project_names_its_authority(tmp_path, monkeypatch):
    """The end-to-end case #179 is about: the claim a researcher handed on fails corroboration
    in build until a person names the host, and then build alone releases it."""
    import json

    root = write_project(tmp_path / "p", sources=("us",))
    _claim_citing(root, URL)
    _provenance(monkeypatch, "verify", "--data", root)

    def built() -> dict:
        code, out = _provenance(monkeypatch, "build", "--data", root)
        assert code == 0, out
        [claim] = json.loads((root / "out" / "claims.json").read_text())
        return claim

    held = built()
    assert held["corroboration_ok"] is False, held
    assert "cited from a host that does not issue them" in held["corroboration_note"], held
    assert held["sources"][0]["secondary_host"] is True
    write_project(root, sources=("us",), extra=f'primary_hosts = ["{AUTHORITY}"]\n')
    released = built()
    assert released["corroboration_ok"] is True, released
    assert released["sources"][0]["secondary_host"] is False


# --- hosts as a URL spells them ---------------------------------------------------------------


def test_an_internationalized_host_can_be_named(tmp_path):
    """`domain()` gives a host as the URL spells it, and entries are ASCII: compared as spelled,
    a Unicode host could be named in neither form."""
    host = "beh\u00f6rde.example"
    p = _load(tmp_path, f'["{host}.gov"]')
    assert p.primary_hosts == ("xn--behrde-yxa.example.gov",)
    for url in (f"https://{host}.gov/x", "https://xn--behrde-yxa.example.gov/x"):
        assert classify(url, p.rules()) == "primary_document", url


def test_a_misnamed_list_does_not_hide_what_the_others_say(tmp_path, synthetic_lists):
    """Every problem in one message: the host is checked against the lists that exist."""
    msg = _refusal(tmp_path, '["news.example.com"]', ("xx", "nosuchlist"))
    assert "no such source list: 'nosuchlist'" in msg, msg
    assert "names 'news.example.com', which the project's source lists class as " \
           "bylined_journalism" in msg, msg


# --- the review page --------------------------------------------------------------------------


def _row_text(tmp_path: Path, src: Source, rules) -> str:
    src.verification.status = "verified"
    c = Claim(question_id="q1", question="What fee schedule did the board adopt?",
              answer="The revised one.", sources=[src], corroboration_ok=True)
    html = render([c], tmp_path / "out", title="T", rules=rules,
                  store=store_id("example", None))[0].read_text()
    return " ".join(HTMLParser(html).css_first(".src").text().split())


def test_the_review_page_badges_only_what_the_project_does_not_name(tmp_path):
    p = _load(tmp_path, f'["{AUTHORITY}"]')
    assert "issuing authority" not in _row_text(tmp_path, _official(), p.rules())


def test_an_unacknowledged_unnamed_host_is_not_called_a_copy(tmp_path):
    """Without an ack nothing says it is a copy: a researcher is told to hand on an authority
    the project doesn't name, and the reviewer is the one who can add it."""
    p = _load(tmp_path, "[]")
    text = _row_text(tmp_path, _official(), p.rules())
    assert "not a named issuing authority" in text, text
    assert "copy, not the issuing authority" not in text, text
    assert (f"Cited from {AUTHORITY}, which this project does not name as an issuing "
            f"authority, with no explanation. If it is the body that issues this record, add it "
            f"to the project's primary_hosts and rebuild.") in text, text


def test_the_review_page_names_the_host_as_primary_hosts_would(tmp_path):
    """It tells the reviewer to add the host to primary_hosts, so it prints what an entry can
    hold: no `www.`, no port. And inside <bdi>, so a bidi override the URL check lets through
    can't reverse the sentence around it."""
    rlo = chr(0x202E)
    p = _load(tmp_path, "[]")
    src = _official(f"https://www.Mirror.example.com:8443/{rlo}x")
    html = render([Claim(question_id="q1", question="Q?", answer="A.", sources=[src])],
                  tmp_path / "out", title="T", rules=p.rules(),
                  store=store_id("example", None))[0].read_text()
    assert "Cited from\n      <bdi>mirror.example.com</bdi>, which this project" in html, html
    host = f"a{rlo}b.example.com"
    html = render([Claim(question_id="q1", question="Q?", answer="A.",
                         sources=[_official(f"https://{host}/x")])],
                  tmp_path / "out", title="T", rules=p.rules(),
                  store=store_id("example", None))[0].read_text()
    assert f"<bdi>{host}</bdi>" in html


def test_an_acknowledged_copy_is_still_badged_a_copy(tmp_path):
    p = _load(tmp_path, f'["{AUTHORITY}"]')
    text = _row_text(tmp_path, _official(MIRROR, secondary_host_ack="the portal is script-only"),
                     p.rules())
    assert "copy, not the issuing authority" in text, text
    assert ("Cited from mirror.example.com, which is not the body that issues this record. "
            "the portal is script-only Check this copy against the official version.") in text, \
        text
