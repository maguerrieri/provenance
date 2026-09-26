"""Each citation carries its own tier, and opinion and advocacy hold a claim to "X argues Y" (#39).

One news site runs reporting and op-eds, and one article holds reported fact beside its writer's
opinion, so a tier read off the host list said nothing about the citation. And an advocacy site
missing from the lists passed as journalism as long as it named an author. Every name and
unlisted host here is synthetic; the news hosts are ones the example project's lists name.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from conftest import write_project

from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source, SourceType
from provenance.sources import (
    ARGUED,
    TIER_LABEL,
    TIER_OF,
    attributes,
    check_source_class,
    load_rules,
    speakers,
    tier,
)
from provenance.verify import check_corroboration, secondary_host, unattributed

RULES = load_rules(("us", "ca"))

# Listed as news outlets in the `ca` list.
NEWS = "https://calmatters.org/harbor-levy-analysis"
OTHER_NEWS = "https://sacbee.com/harbor-levy"
# On no list.
ADVOCACY = "https://levy-watch.example/why-the-levy-fails"
ADVOCACY_2 = "https://port-forward.example/the-levy-question"

FACT = "the harbor levy passed on its second reading"
OPINION = "the levy is a mistake the council will come to regret"
QUESTION = "What does the record show about the harbor levy?"
# The analysis piece holds both: a reported fact, then its writer's view of it.
ARTICLE = (f"After a long hearing, {FACT}, with the board split. In this writer's view, "
           f"{OPINION}, and the port will pay for it.")


def _source(url=NEWS, snippet=FACT, source_type="bylined_journalism", publisher="CalMatters",
            author="R. Writer", status="verified", support="supports") -> Source:
    s = Source(url=url, publisher=publisher, author=author, date="2030-05-14",
               source_type=source_type, snippet=snippet)
    s.verification.status = status
    s.verification.support = support
    return s


def _claim(answer, *sources, claim_type="mechanical") -> Claim:
    return check_corroboration(Claim(question_id="q1", question=QUESTION, answer=answer,
                                     claim_type=claim_type, sources=list(sources)), rules=RULES)


def test_every_source_type_has_a_tier():
    """A source_type added without one would raise wherever a tier is read. Adding one (#41 may)
    means deciding what it can carry, here, in a diff a reviewer reads."""
    assert set(get_args(SourceType)) == set(TIER_OF)
    assert set(TIER_OF.values()) | {"unlisted_outlet"} == set(TIER_LABEL)
    assert ARGUED <= set(TIER_LABEL)


def test_the_tier_is_the_citations_not_the_hosts():
    """One news host, three tiers: the citation's own type decides. The host can only lower a
    tier: reporting on a host no list names as a news outlet is an unlisted outlet."""
    assert tier(_source(), RULES) == "reporting"
    assert tier(_source(source_type="opinion"), RULES) == "opinion"
    assert tier(_source(source_type="advocacy"), RULES) == "advocacy"
    assert tier(_source(url=ADVOCACY), RULES) == "unlisted_outlet"
    # The project's lists decide: a regional outlet is unlisted to a project without its list.
    assert tier(_source(), load_rules(("us",))) == "unlisted_outlet"
    # A label never raises a page past what it is: opinion stays opinion on any host.
    assert tier(_source(url=ADVOCACY, source_type="opinion"), RULES) == "opinion"
    # And the lists are the caller's to give: defaulted to `us`, every regional outlet would be
    # unlisted.
    with pytest.raises(TypeError):
        tier(_source())


def test_an_answer_attributes_a_source_by_its_author_or_publisher_as_whole_words():
    s = _source(source_type="opinion", author="Pat O'Rourke", publisher="The Harbor Ledger")
    assert attributes("Pat O'Rourke argues the levy is a mistake.", s)
    assert attributes("Pat O’Rourke  argues it.", s), "quote- and space-blind, as a question is"
    assert attributes("The Harbor Ledger's editorial says so.", s)
    assert attributes("Harbor Ledger argues so.", s), "with or without its leading 'The'"
    assert not attributes("O'Rourke argues the levy is a mistake.", s), "a surname is not a name"
    assert not attributes("The levy is a mistake.", s)
    assert not attributes("Harbor Ledgers argue so.", s), "inside a longer word is not the name"


def test_a_name_is_told_from_a_word_by_its_capitals():
    """Case-blind, an ordinary phrase named a publisher whose name is one: the bare opinion
    passed as attributed. And a byline that names nobody names no one in the answer either."""
    record = _source(source_type="advocacy", author="Levy Watch", publisher="The Record")
    assert not attributes("The record shows the levy is a mistake.", record)
    assert attributes("The Record argues the levy is a mistake.", record)
    times = _source(source_type="opinion", author="A. Columnist", publisher="The Times")
    assert not attributes("The council voted three times; the levy is a mistake.", times)
    staff = _source(source_type="opinion", author="Staff", publisher="")
    assert speakers(staff) == []
    assert not attributes("City staff say the levy is a mistake.", staff)
    # One word left of "The Record" starts sentences, so the publisher is named whole.
    assert not attributes("Record turnout shows the levy is a mistake.", record)
    assert "Record" not in speakers(record)
    # A name of a mark or one letter matched any answer holding it. Counted over the whole name,
    # so one written as single characters with spaces between is still a name.
    for mark in ("-", "A"):
        unnamed = _source(source_type="advocacy", author=mark, publisher="")
        assert speakers(unnamed) == []
        assert not attributes("A levy fails voters - it costs too much.", unnamed)
    assert speakers(_source(author="\u674e \u660e", publisher="")) == ["\u674e \u660e"]
    # Folded as a question is: a fullwidth "Staff" and a no-break space print the same.
    for nobody in ("\uff33\uff54\uff41\uff46\uff46", "Editorial\u00a0Board", " N/A ",
                   "The Editorial Board"):
        assert speakers(_source(source_type="opinion", author=nobody, publisher="")) == []
        ok, why = check_source_class(_source(author=nobody), RULES)
        assert not ok and "is not a named or institutional author-of-record" in why


def test_a_mixed_tier_article_is_tiered_per_citation():
    """The issue's case: one article, cited once for its reported fact and once for its writer's
    opinion. Each citation keeps its own tier, the opinion one alone needs attributing, and the
    page is one document however it is cited."""
    fact, opinion = _source(), _source(snippet=OPINION, source_type="opinion")
    assert [tier(s, RULES) for s in (fact, opinion)] == ["reporting", "opinion"]

    bare = Claim(question_id="q1", question=QUESTION, sources=[fact, opinion],
                 answer="The levy passed on its second reading, and it is a mistake.")
    assert unattributed(bare, RULES) == [opinion]

    said = _claim("The levy passed on its second reading; R. Writer argues it is a mistake.",
                  fact, opinion)
    assert said.corroboration_ok is True and said.status == "verified"
    assert said.corroboration_note.startswith("1 document(s) from 1 publisher(s), 2 snippet(s)")

    # One document, so an adversarial claim needs another; a second outlet's report is one.
    alone = _claim(said.answer, fact, opinion, claim_type="adversarial")
    assert alone.corroboration_ok is False and "has 1 document(s)" in alone.corroboration_note
    other = _source(url=OTHER_NEWS, publisher="Sacramento Bee", author="A. Reporter")
    assert _claim(said.answer, fact, opinion, other,
                  claim_type="adversarial").corroboration_ok is True


def test_an_opinion_source_stated_bare_fails_corroboration_whatever_backs_it():
    """`build` enforces the claim form as `check-claim` does. A report backing the fact beside it
    does not help: the answer still states the opinion as fact."""
    bare = _claim("The levy passed and it is a mistake.",
                  _source(), _source(snippet=OPINION, source_type="opinion"))
    assert bare.corroboration_ok is False and bare.status == "human_review"
    assert bare.corroboration_note.startswith("1 source(s) cited for a claim they cannot carry")
    assert "'X argues Y'" in bare.corroboration_note

    # Every problem at once: fixing the attribution alone would still leave this one short.
    both = _claim(bare.answer, _source(), _source(snippet=OPINION, source_type="opinion"),
                  claim_type="adversarial")
    assert both.corroboration_note.startswith("1 source(s) cited for a claim they cannot carry")
    assert ("; and adversarial claim needs 2 independent document(s); has 1 document(s) "
            "(1 snippet(s))") in both.corroboration_note

    # A source the verifier rejected is not evidence at all, so it holds nothing.
    rejected = _claim("The levy passed and it is a mistake.", _source(),
                      _source(snippet=OPINION, source_type="opinion", support="topic_only"))
    assert rejected.corroboration_ok is True


def test_two_advocacy_pieces_are_one_source():
    """Each is one side's say-so. Attributed, each supports what its author argues, and together
    they are one document: an adversarial claim needs a source beside them."""
    answer = "Levy Watch and Port Forward both argue the levy will fail."
    first = _source(url=ADVOCACY, snippet=OPINION, source_type="advocacy",
                    publisher="Levy Watch", author="Levy Watch")
    second = _source(url=ADVOCACY_2, snippet=OPINION, source_type="advocacy",
                     publisher="Port Forward", author="Port Forward")
    both = _claim(answer, first, second, claim_type="adversarial")
    assert both.corroboration_ok is False
    assert ("has 1 document(s) (2 snippet(s)), 2 opinion, advocacy or unlisted-outlet "
            "document(s) counted as one") in both.corroboration_note

    report = _source(url=OTHER_NEWS, publisher="Sacramento Bee", author="A. Reporter")
    with_report = _claim(answer, first, second, report, claim_type="adversarial")
    assert with_report.corroboration_ok is True
    assert "counted as one" in with_report.corroboration_note


def test_an_unlisted_advocacy_host_does_not_count_as_reporting():
    """The issue's case: an advocacy site on no list, labeled reporting, with a byline. It is an
    unlisted outlet, so it carries only what it says, and counts with the advocacy."""
    labeled = _source(url=ADVOCACY, publisher="Levy Watch", author="J. Advocate")
    assert tier(labeled, RULES) == "unlisted_outlet"
    assert _claim("The levy will fail.", labeled).corroboration_ok is False
    assert _claim("Levy Watch reports the levy will fail.", labeled).corroboration_ok is True

    # Beside an advocacy piece it is not a second, independent source.
    advocacy = _source(url=ADVOCACY_2, snippet=OPINION, source_type="advocacy",
                       publisher="Port Forward", author="Port Forward")
    pair = _claim("Levy Watch and Port Forward say the levy will fail.", labeled, advocacy,
                  claim_type="adversarial")
    assert pair.corroboration_ok is False and "counted as one" in pair.corroboration_note


def test_an_official_analysis_off_its_authoritys_host_is_a_copy():
    """The label alone must not make an advocacy site's "analysis" carry a bare fact: off an
    issuing authority's host it is a secondary host, as a primary document is."""
    at_home = _source(url="https://lao.ca.gov/analysis/levy", source_type="official_analysis",
                      publisher="Legislative Analyst", author="Legislative Analyst's Office")
    assert tier(at_home, RULES) == "official_analysis" and not secondary_host(at_home, RULES)
    assert secondary_host(_source(url=ADVOCACY, source_type="official_analysis"), RULES)


def test_relabeling_an_unlisted_page_as_a_primary_text_does_not_carry_a_bare_fact():
    """The bypass: the advocacy page that cannot carry "the levy will fail" as reporting,
    relabeled a primary text or an analysis. Off an issuing authority's host with no ack it is
    no evidence, so `build` holds it as `check-claim` does. With an ack it is a declared copy,
    badged on the review page: the substitution is loud, not silent."""
    for relabeled in ("primary_document", "official_record", "official_analysis"):
        page = _source(url=ADVOCACY, source_type=relabeled, publisher="Levy Watch",
                       author="Levy Watch")
        bare = _claim("The levy will fail.", page)
        assert bare.corroboration_ok is False and bare.status == "human_review", relabeled
        assert "cited from a host that does not issue them, with no secondary_host_ack" in \
            bare.corroboration_note
        page.secondary_host_ack = "The agency's portal is down; this is its filed PDF."
        assert _claim("The levy will fail.", page).corroboration_ok is True


# --- through the commands ---------------------------------------------------------------------


def _provenance(*args) -> tuple[int, str]:
    from typer.testing import CliRunner

    from provenance import cli

    # rich folds a long tmp path mid-word at 80 columns. Put back `_width` itself, not the
    # width rich computed, which would pin it (CLAUDE.md, "Rich reads brackets as markup").
    width = cli.con._width
    cli.con._width = 10_000
    try:
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        cli.con._width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


def _run(root: Path, answer: str, *sources: Source) -> Path:
    """A run whose one claim cites `sources`, each page cached holding `ARTICLE`. Returns the
    claim file."""
    from provenance.fetch import cache_path

    write_project(root)
    (root / "claims").mkdir(parents=True, exist_ok=True)
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": QUESTION}]))
    for s in sources:
        cache_path(root, s.url).write_text(PageCache(
            url=s.url, final_url=s.url, status=200, content_type="text/html", title="T",
            text=ARTICLE, fetched_at=datetime.now(UTC) - timedelta(hours=1),
            extractor_version=EXTRACTOR_VERSION).model_dump_json())
    path = root / "claims" / "q1.json"
    path.write_text(Claim(question_id="q1", question=QUESTION, answer=answer,
                          sources=list(sources)).model_dump_json(exclude={"sources": {
                              "__all__": {"verification"}}}))
    return path


def test_check_claim_fails_an_opinion_citation_for_a_bare_factual_claim(tmp_path):
    """The acceptance case, and the names it takes are in the message."""
    opinion = _source(snippet=OPINION, source_type="opinion")
    path = _run(tmp_path, "The levy is a mistake the council will regret.", opinion)
    code, out = _provenance("check-claim", path)
    assert code == 1, out
    assert f"not attributed opinion CalMatters: '{OPINION}'" in out, out
    assert ("This supports only 'X argues Y', never Y on its own. Name who argues it in "
            "`answer`: 'R. Writer' or 'CalMatters'.") in out, out
    assert "corroboration 1 source(s) cited for a claim they cannot carry" in out, out

    path = _run(tmp_path, "R. Writer argues the levy is a mistake the council will regret.",
                opinion)
    code, out = _provenance("check-claim", path)
    assert code == 0 and "All sources check out." in out, out

    # A byline that names nobody gives the answer nothing to name: say so, not "`answer`: .".
    unnamed = _source(snippet=OPINION, source_type="opinion", author="Staff", publisher="")
    code, out = _provenance("check-claim", _run(tmp_path, "The levy is a mistake.", unnamed))
    assert code == 1, out
    assert ("its `author` and `publisher` name nobody the answer could say argues it") in out, out


def test_check_claim_says_an_unlisted_outlet_is_not_reporting(tmp_path):
    labeled = _source(url=ADVOCACY, publisher="Levy Watch", author="J. Advocate")
    path = _run(tmp_path, "The harbor levy passed on its second reading.", labeled)
    code, out = _provenance("check-claim", path)
    assert code == 1, out
    assert "not attributed unlisted outlet Levy Watch" in out, out
    assert ("levy-watch.example is not on this project's source lists as a news outlet, so "
            "nothing but the label says this is reporting.") in out, out

    path = _run(tmp_path, "Levy Watch reports the harbor levy passed on its second reading.",
                labeled)
    code, out = _provenance("check-claim", path)
    assert code == 0 and "All sources check out." in out, out


def test_the_review_page_and_claims_json_carry_each_rows_tier(tmp_path):
    """An argued row says what it supports, so the reviewer checks the claim's form, not the
    fact; an unlisted outlet says why it is not reporting."""
    fact = _source(status="pending", support="unreviewed")
    opinion = _source(snippet=OPINION, source_type="opinion", status="pending",
                      support="unreviewed")
    unlisted = _source(url=ADVOCACY, publisher="Levy Watch", author="J. Advocate",
                       status="pending", support="unreviewed")
    _run(tmp_path, "The levy passed; R. Writer argues it is a mistake; Levy Watch reports it.",
         fact, opinion, unlisted)
    code, out = _provenance("verify", "--data", tmp_path)
    assert code == 0, out
    code, out = _provenance("build", "--data", tmp_path)
    assert code == 0, out

    built = json.loads((tmp_path / "out" / "claims.json").read_text())
    assert [s["tier"] for s in built[0]["sources"]] == ["reporting", "opinion", "unlisted_outlet"]
    page = (tmp_path / "out" / "review.html").read_text()
    assert page.count("This supports only what its author argues") == 2
    assert ("levy-watch.example is not on this project's source lists as a news outlet, so "
            "this is not counted as reporting.") in " ".join(page.split())

    # The verifier sees the tier too: labeled reporting, it is an unlisted outlet, and the
    # verifier judges an argued tier's claim form.
    code, out = _provenance("handoff", "q1", "--data", tmp_path)
    assert code == 0, out
    assert "bylined_journalism · tier: reporting" in out, out
    assert "opinion · tier: opinion" in out, out
    assert "bylined_journalism · tier: unlisted outlet" in out, out
