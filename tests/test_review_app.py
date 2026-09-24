"""The review app's saved progress, driven through the page's own script.

The reviewer's check is the last layer of verification, so these run the real `<script>` from
a rendered page (tests/review_app_harness.js, under node) and act on it as a reviewer does,
rather than asserting on the script's text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from vgpipe.models import Claim, QueryCitation, QueryRun, Source
from vgpipe.report import render, review_fingerprint

HARNESS = Path(__file__).parent / "review_app_harness.js"
NODE = shutil.which("node")
LEGACY = "vgpipe:t"          # what earlier versions of the page saved, per source
STORE = "vgpipe:t:v2"

# CI must run these: a skip there would read as a pass.
pytestmark = pytest.mark.skipif(not NODE and not os.environ.get("CI"),
                                reason="the review app tests run its script under node")

CONTEXT = "At its March meeting the council approved the levy by a vote of four to one."
SNIPPET = "the council approved the levy"


def cited(context: str | None = CONTEXT, **kw) -> Source:
    """One synthetic source, identical wherever it is cited, so every claim gets the same sid."""
    s = Source(url="https://ledger.example/levy-vote", publisher="Example Ledger",
               author="A. Writer", source_type="bylined_journalism", snippet=SNIPPET, **kw)
    s.verification.status = "verified"
    s.verification.support = "supports"
    if context is not None:
        s.verification.context = context
        at = context.index(SNIPPET)
        s.verification.context_offset = (at, at + len(SNIPPET))
    return s


def claim(qid: str, answer: str, *sources: Source, question: str = "") -> Claim:
    return Claim(question_id=qid, question=question or "What did the council decide?",
                 answer=answer, sources=list(sources) or [cited()], corroboration_ok=True)


def _tree(node) -> dict:
    return {"tag": node.tag,
            "attrs": {k: v or "" for k, v in node.attributes.items()},
            "children": [_tree(c) for c in node.iter()
                         if c.tag not in ("script", "style") and c.tag[0] not in "-_"]}


def run(tmp_path: Path, claims: list[Claim], *, storage: dict | None = None,
        actions: list | None = None) -> dict:
    """Render `claims`, load the page with `storage` as its localStorage, perform `actions`,
    and return what the page shows and stores."""
    if not NODE:
        pytest.fail("node is not installed, and CI must run the review app tests")
    page = HTMLParser(render(claims, tmp_path, title="T")[0].read_text())
    payload = {"tree": _tree(page.body), "script": page.css_first("script").text(),
               "storage": storage or {}, "actions": actions or []}
    out = subprocess.run([NODE, str(HARNESS)], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def rows(result: dict) -> dict:
    """Rows by key; a key two rows share maps to a list of both."""
    by: dict = {}
    for r in result["rows"]:
        by.setdefault(r["key"], []).append(r)
    return {k: v[0] if len(v) == 1 else v for k, v in by.items()}


def stored(result: dict) -> dict:
    return json.loads(result["storage"][STORE])


def test_two_questions_citing_one_source_are_checked_separately(tmp_path):
    """The check says a source supports ONE claim. Keyed by source id alone, ticking it under
    one question ticked it under every question citing it, and could mark that other claim
    done though nobody had read the source against it."""
    claims = [claim("q1", "The council approved the levy."),
              claim("q2.b", "The levy passed by a wide margin.",
                    question="By what margin did the levy pass?")]
    sid = claims[0].sources[0].sid
    assert claims[1].sources[0].sid == sid, "the test needs one source cited twice"
    one, two = f"q1/{sid}", f"q2.b/{sid}"

    first = run(tmp_path, claims, actions=[{"do": "tick", "row": one, "checked": True}])
    assert rows(first)[one]["checked"] and not rows(first)[two]["checked"]
    assert [c["done"] for c in first["claims"]] == [True, False]
    assert first["progress"].startswith("1/2")

    # It survives a reload, and the other row stays its own.
    again = run(tmp_path, claims, storage=first["storage"])
    assert rows(again)[one]["checked"] and not rows(again)[two]["checked"]

    # The keyboard path keys by row too.
    keyed = run(tmp_path, claims, storage=first["storage"],
                actions=[{"do": "key", "row": two, "key": " "},
                         {"do": "key", "row": one, "key": " "}])
    assert not rows(keyed)[one]["checked"] and rows(keyed)[two]["checked"]

    # A flag is a warning about the source, not an attestation, so it shows wherever the
    # source is cited: sharing it fails toward a second look, never toward green.
    flagged = run(tmp_path, claims, actions=[{"do": "flag", "row": two}])
    assert rows(flagged)[two]["flagged"] and rows(flagged)[one]["flagged"]


def test_a_changed_excerpt_clears_the_check(tmp_path):
    """A check is about the excerpt the reviewer read. The source id covers only the url and
    snippet, so a re-fetch or new snapshot could change the text around the snippet, and the
    check carried over to text nobody had read."""
    key = f"q1/{cited().sid}"
    answer = "The council approved the levy."
    before = run(tmp_path, [claim("q1", answer)],
                 actions=[{"do": "tick", "row": key, "checked": True}])
    assert rows(before)[key]["checked"]

    changed = cited("In a later session the council approved the levy after a hearing.")
    after = run(tmp_path, [claim("q1", answer, changed)], storage=before["storage"])
    row = rows(after)[key]
    assert not row["checked"], "a check on another excerpt must not count"
    assert row["stale"], "the reviewer is told why the row reads unchecked"
    assert after["progress"].startswith("0/1") and not after["claims"][0]["done"]

    # Rewording the claim is the same: the reviewer judged the source against other words.
    reworded = run(tmp_path, [claim("q1", "The council rejected the levy.")],
                   storage=before["storage"])
    assert not rows(reworded)[key]["checked"] and rows(reworded)[key]["stale"]

    # An unchanged rebuild keeps it: nothing the check was about moved.
    same = run(tmp_path, [claim("q1", answer)], storage=after["storage"])
    assert rows(same)[key]["checked"] and not rows(same)[key]["stale"]

    # Checking it again records the new excerpt, and unchecking settles the warning.
    rechecked = run(tmp_path, [claim("q1", answer, changed)], storage=before["storage"],
                    actions=[{"do": "tick", "row": key, "checked": True}])
    assert rows(rechecked)[key]["checked"] and not rows(rechecked)[key]["stale"]
    dismissed = run(tmp_path, [claim("q1", answer, changed)], storage=before["storage"],
                    actions=[{"do": "tick", "row": key, "checked": True},
                             {"do": "tick", "row": key, "checked": False}])
    assert not rows(dismissed)[key]["checked"] and not rows(dismissed)[key]["stale"]


def test_a_renumbered_claim_keeps_its_check_and_flags(tmp_path):
    """`vg remap` moves a claim to another question id. The claim and its evidence are what the
    check attested, so an unchanged claim keeps its check wherever it now sits, and a flag
    stays with its source."""
    sid = cited().sid
    answer = "The council approved the levy."
    before = run(tmp_path, [claim("q18", answer)],
                 actions=[{"do": "tick", "row": f"q18/{sid}", "checked": True},
                          {"do": "flag", "row": f"q18/{sid}"}])
    moved = run(tmp_path, [claim("q20", answer)], storage=before["storage"])
    row = rows(moved)[f"q20/{sid}"]
    assert row["checked"] and row["flagged"] and not row["stale"]

    # A different claim now on the old id does not inherit it.
    other = claim("q18", "The levy failed.", question="Did the levy fail?")
    reused = run(tmp_path, [other, claim("q20", answer)], storage=before["storage"])
    assert not rows(reused)[f"q18/{sid}"]["checked"] and rows(reused)[f"q20/{sid}"]["checked"]

    # The check now names the row it moved to, so when that row's excerpt later changes, the
    # warning lands there, not on whatever claim took the old id.
    changed = cited("In a later session the council approved the levy after a hearing.")
    later = run(tmp_path, [other, claim("q20", answer, changed)], storage=moved["storage"])
    assert rows(later)[f"q20/{sid}"]["stale"] and not rows(later)[f"q18/{sid}"]["stale"]


def test_one_claim_citing_one_snippet_twice_checks_each_locator(tmp_path):
    """The same url and snippet cited twice in one claim, on different pages, are two rows.
    Each check is its own: looking at page 3 is not looking at page 7."""
    p3, p7 = cited(page=3), cited(page=7)
    c = claim("q1", "The council approved the levy.", p3, p7)
    k3 = f"q1/{p3.sid}"
    k7 = k3 + "/2"
    ticked = run(tmp_path, [c], actions=[{"do": "tick", "row": k3, "checked": True}])
    assert rows(ticked)[k3]["checked"] and not rows(ticked)[k7]["checked"]
    assert not ticked["claims"][0]["done"]

    # When page 3's excerpt changes, the warning is page 3's alone, and checking page 7
    # doesn't settle it.
    moved = cited("The minutes record that the council approved the levy in closed session.",
                  page=3)
    after = run(tmp_path, [claim("q1", c.answer, moved, cited(page=7))],
                storage=ticked["storage"], actions=[{"do": "tick", "row": k7, "checked": True}])
    assert rows(after)[k3]["stale"] and not rows(after)[k3]["checked"]
    assert rows(after)[k7]["checked"] and not rows(after)[k7]["stale"]


def test_the_fingerprint_covers_what_the_row_attests():
    """Every part the reviewer judged moves the fingerprint: the claim, the citation as asserted
    and shown, and the evidence (the excerpt; the snapshot where there is none; for a query row,
    the definition and export). Nothing else does, so a check doesn't go stale for no reason."""
    c = claim("q1", "The council approved the levy.")
    changed = "In a later session the council approved the levy after a hearing."

    def fp(claim_=c, source=None, **kw):
        s = source or cited()
        for k, v in kw.items():
            setattr(s, k, v)
        return review_fingerprint(claim_, s)

    base = fp()
    assert fp() == base, "stable across rebuilds"
    assert fp(claim("q9", c.answer)) == base, "content, not position"
    assert fp(claim("q1", "The council rejected the levy.")) != base
    assert fp(claim("q1", c.answer, question="Who voted?")) != base
    assert fp(source=cited(changed)) != base, "the excerpt"
    moved = cited()
    moved.verification.context_offset = (0, 6)
    assert fp(source=moved) != base, "the highlight"
    for field, value in (("page", 4), ("date", "2030-03-04"), ("publisher", "Other Ledger"),
                         ("author", "B. Writer"), ("secondary_host_ack", "portal is script-only"),
                         ("snippet", "approved the levy by a vote")):
        assert fp(**{field: value}) != base, field

    passed = cited()
    passed.verification.status = "normalized_match"
    passed.verification.support = "topic_only"
    assert fp(source=passed) == base, "a pipeline verdict isn't something the reviewer read"

    snap = "https://web.archive.org/web/2030/https://ledger.example/levy-vote"
    other = "https://web.archive.org/web/2031/https://ledger.example/levy-vote"
    assert fp(archive_url=snap) == base, "with an excerpt shown, the excerpt is the evidence"
    assert fp(source=cited(None), archive_url=snap) != fp(source=cited(None), archive_url=other), \
        "with none, the snapshot is the reviewer's route to the text"


def test_a_query_rows_fingerprint_is_its_run_not_its_printout():
    """A query row is checked by re-running it, so what it attests is the definition and the
    export the figure came from. The printed command carries the `--cache` path, and the context
    carries the query's note, a message; neither is evidence."""
    def row(run: QueryRun, note: str) -> Source:
        s = cited(None)
        s.query = QueryCitation(name="contributor_total", expected="1200.00",
                                params={"committee": "Example Committee"})
        s.verification.query_run = run
        s.verification.context = f"contributor_total(committee=Example Committee) = 1200.00  [{note}]"
        s.verification.context_offset = (0, len("contributor_total"))
        return s

    c = claim("q1", "The committee raised 1,200 dollars from one donor.")
    v1 = QueryRun(version=1, export_date="2030-01-02", cache_root="data")
    base = review_fingerprint(c, row(v1, "1 filing"))
    assert review_fingerprint(c, row(v1, "one filing, deduplicated")) == base
    assert review_fingerprint(c, row(v1.model_copy(update={"cache_root": "/abs/data"}),
                                     "1 filing")) == base
    assert review_fingerprint(c, row(v1.model_copy(update={"version": 2}), "1 filing")) != base
    assert review_fingerprint(c, row(v1.model_copy(update={"export_date": "2030-02-02"}),
                                     "1 filing")) != base


def test_per_source_progress_is_not_spread_to_every_question(tmp_path):
    """Progress saved before this version holds one tick per source, tied to no claim and no
    excerpt. Applying it to every row citing the source would be the false green itself, so
    ticks are dropped and the reviewer is told. Flags and notes are per source still, so they
    carry over whole, cited or not, and the old progress is left where it was."""
    claims = [claim("q1", "The council approved the levy."),
              claim("q2", "The levy passed.", question="Did the levy pass?")]
    sid = claims[0].sources[0].sid
    legacy = json.dumps({sid: {"done": True, "flag": True, "note": "check the vote count"},
                         "0123456789ab": {"done": True, "flag": False, "note": "since dropped"},
                         "ba9876543210": {"done": False, "flag": False, "note": ""}})

    loaded = run(tmp_path, claims, storage={LEGACY: legacy})
    for r in loaded["rows"]:
        assert not r["checked"] and not r["stale"]
        assert r["flagged"] and r["note"] == "check the vote count"
    assert "cleared: 1 on sources cited here" in loaded["notice"]
    assert "and 1 on sources no longer cited" in loaded["notice"]
    new = stored(loaded)
    assert new["checked"] == {}
    assert new["sources"]["0123456789ab"]["note"] == "since dropped", "kept, though uncited"
    assert loaded["storage"][LEGACY] == legacy, "the old progress is read, never rewritten"

    # The notice stays until the reviewer dismisses it: a reload or a second tab still shows it.
    reloaded = run(tmp_path, claims, storage=loaded["storage"])
    assert reloaded["notice"] == loaded["notice"]
    dismissed = run(tmp_path, claims, storage=loaded["storage"], actions=[{"do": "dismiss"}])
    assert dismissed["notice"] == ""
    assert run(tmp_path, claims, storage=dismissed["storage"])["notice"] == ""
    assert all(r["flagged"] for r in rows(dismissed).values()), "the flags are unaffected"

    # An exported progress file from before the change is read the same way.
    imported = run(tmp_path, claims, actions=[{"do": "import", "text": legacy}])
    assert not any(r["checked"] for r in imported["rows"])
    assert "cleared: 1 on sources cited here" in imported["notice"]


def test_unreadable_progress_is_kept_and_not_replaced_by_older(tmp_path):
    """Progress that can't be parsed is not a cue to migrate the per-source progress again,
    which would bring back flags cleared since. The page starts over, says so, and keeps the
    unreadable text where a person can recover it, since its first save replaces it."""
    c = claim("q1", "The council approved the levy.")
    sid = c.sources[0].sid
    legacy = json.dumps({sid: {"done": False, "flag": True, "note": "cleared long ago"}})
    result = run(tmp_path, [c], storage={STORE: '{"v": 2, "checked": {', LEGACY: legacy})
    assert not rows(result)[f"q1/{sid}"]["flagged"]
    assert "could not be read" in result["notice"] and STORE + ":unreadable" in result["notice"]
    assert result["storage"][STORE + ":unreadable"] == '{"v": 2, "checked": {'


def test_stored_progress_is_sanitized(tmp_path):
    """Progress comes from localStorage or a file someone hands you, so the app coerces it into
    a known shape: keys and values whitelisted to their formats (a `__proto__` key is dropped,
    not assigned), annotations reduced to the two fields it uses."""
    c = claim("q1", "The council approved the levy.")
    sid = c.sources[0].sid
    hostile = {"v": 2,
               "checked": {"__proto__": f"q1/{sid}", "0" * 16: "../q1/" + sid,
                           "1" * 16: f"q1/{sid}", "not a fingerprint": f"q1/{sid}"},
               "sources": {"__proto__": {"flag": True},
                           sid: {"flag": "yes", "note": 7, "extra": "dropped"}},
               "notice": {"cleared": "<b>9</b>", "uncited": -1, "unreadable": "yes"},
               "extra": {"dropped": True}}
    for result in (run(tmp_path, [c], storage={STORE: json.dumps(hostile)}),
                   run(tmp_path, [c], actions=[{"do": "import", "text": json.dumps(hostile)}])):
        assert stored(result) == {"v": 2, "checked": {"1" * 16: f"q1/{sid}"},
                                  "sources": {sid: {"flag": False, "note": ""}}}
        assert not rows(result)[f"q1/{sid}"]["checked"]
        assert not rows(result)[f"q1/{sid}"]["flagged"]

    # A file that isn't JSON is refused, not half-applied.
    bad = run(tmp_path, [c], actions=[{"do": "import", "text": "{not json"}])
    assert bad["alerts"] == ["Bad progress file"]
    assert bad["fileInput"] == "", "the file input resets, so importing the same file works"
