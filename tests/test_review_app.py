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

from provenance.models import Claim, QueryCitation, QueryRun, Source
from provenance.report import render, review_fingerprint

HARNESS = Path(__file__).parent / "review_app_harness.js"
NODE = shutil.which("node")
LEGACY = "vgpipe:t"          # what earlier versions of the page saved, per source
V2 = "vgpipe:t:v2"          # what the page saved before a claim's notes were hashed
STORE = "vgpipe:t:v3"

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
    # Only the tests that run the page need node, so the fingerprint tests run anywhere. And CI
    # must run these: a skip there would read as a pass.
    if not NODE and os.environ.get("CI"):
        pytest.fail("node is not installed, and CI must run the review app tests")
    if not NODE:
        pytest.skip("the review app tests run its script under node")
    page = HTMLParser(render(claims, tmp_path, title="T")[0].read_text())
    payload = {"tree": _tree(page.body), "script": page.css_first("script").text(),
               "storage": storage or {}, "actions": actions or []}
    out = subprocess.run([NODE, str(HARNESS)], input=json.dumps(payload),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def rows(result: dict) -> dict:
    """Rows by key. Every row has its own (report.ROW_KEY_RE), so a shared one fails here."""
    by = {r["key"]: r for r in result["rows"]}
    assert len(by) == len(result["rows"]), "two rows share a key"
    return by


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


def test_a_claim_moved_to_another_id_keeps_its_check_and_flags(tmp_path):
    """A claim can be moved by hand to another question id: say, one filed under the wrong id.
    The claim and its evidence are what the check attested, so an unchanged claim keeps its
    check wherever it now sits, and a flag stays with its source."""
    sid = cited().sid
    answer = "The council approved the levy."
    before = run(tmp_path, [claim("q3", answer)],
                 actions=[{"do": "tick", "row": f"q3/{sid}", "checked": True},
                          {"do": "flag", "row": f"q3/{sid}"}])
    moved = run(tmp_path, [claim("q7", answer)], storage=before["storage"])
    row = rows(moved)[f"q7/{sid}"]
    assert row["checked"] and row["flagged"] and not row["stale"]

    # A different claim on the old id does not inherit it. Question ids are never reused, but
    # nothing enforces that, so the page doesn't rely on it.
    other = claim("q3", "The levy failed.", question="Did the levy fail?")
    reused = run(tmp_path, [other, claim("q7", answer)], storage=before["storage"])
    assert not rows(reused)[f"q3/{sid}"]["checked"] and rows(reused)[f"q7/{sid}"]["checked"]

    # The check now names the row it moved to, so when that row's excerpt later changes, the
    # warning lands there, not on whatever claim sits on the old id.
    changed = cited("In a later session the council approved the levy after a hearing.")
    later = run(tmp_path, [other, claim("q7", answer, changed)], storage=moved["storage"])
    assert rows(later)[f"q7/{sid}"]["stale"] and not rows(later)[f"q3/{sid}"]["stale"]


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

    # The fields are agent-authored, so a separator one of them contains must not move a
    # boundary: joined with NUL, these two rows hashed alike.
    assert (fp(claim("q1", "Yes.\x00Example Ledger"), publisher="")
            != fp(claim("q1", "Yes."), publisher="Example Ledger\x00"))

    passed = cited()
    passed.verification.status = "normalized_match"
    passed.verification.support = "topic_only"
    assert fp(source=passed) == base, "a pipeline verdict isn't something the reviewer read"

    snap = "https://web.archive.org/web/2030/https://ledger.example/levy-vote"
    other = "https://web.archive.org/web/2031/https://ledger.example/levy-vote"
    assert fp(archive_url=snap) == base, "with an excerpt shown, the excerpt is the evidence"
    assert fp(source=cited(None), archive_url=snap) != fp(source=cited(None), archive_url=other), \
        "with none, the snapshot is the reviewer's route to the text"


def test_the_fingerprint_covers_the_claims_researcher_notes():
    """The reviewer reads a claim's researcher notes above its rows, and they carry what a
    person has to act on, so a check covers them too. They get a part of their own, after a dot,
    and only when there are any: the page can then tell a changed note from changed evidence,
    and a claim without notes hashes exactly as it did before notes were hashed, so the checks
    saved then still stand."""
    c = claim("q1", "The council approved the levy.")
    base = review_fingerprint(c, c.sources[0])
    # Pinned: this is what the page saved for this row before notes were hashed. Changing how a
    # claim without notes hashes clears every check saved on one.
    assert base == "284a91bc9c80d54f"

    def with_notes(notes):
        n = claim("q1", c.answer)
        n.notes = notes
        return review_fingerprint(n, n.sources[0])

    assert with_notes(None) == with_notes("") == with_notes(" \n ") == base, \
        "nothing shown, nothing hashed"
    noted = with_notes("The filing cited may not be the newest.")
    head, notes = noted.split(".")
    assert head == base and len(notes) == 16, "the evidence part is unchanged"
    assert with_notes("\n The filing cited may not be the newest.  \n") == noted, \
        "the note as shown, which is stripped"
    two = with_notes("The filing cited may not be the newest.\nPage 3 is a scan.")
    for same in ("The filing cited may not be the newest.\r\nPage 3 is a scan.",
                 "The filing cited may not be the newest.\rPage 3 is a scan.",
                 "The filing cited may not be the newest. \t\nPage 3 is a scan.",
                 "The filing cited may not be the newest." + chr(0xA0) + chr(0x0C)
                 + "\nPage 3 is a scan."):
        assert with_notes(same) == two, "a line ending or trailing white space doesn't show"
    # A lone surrogate survives json.loads; it is hashed as the rest of the fingerprint is.
    assert "." in with_notes("Page 3 is a scan" + chr(0xD800))
    assert with_notes("The filing cited may not be the newest; page 3 is a scan.") != noted
    assert with_notes("The filing cited  may not be the newest.") != noted, \
        "the note shows its spacing, so a change in it is a change in what was read"


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
    """Progress this page can't read is not a cue to migrate the per-source progress again,
    which would bring back flags cleared since. The page starts over, says so, and keeps the
    unreadable text where a person can recover it, since its first save replaces it."""
    c = claim("q1", "The council approved the levy.")
    sid = c.sources[0].sid
    legacy = json.dumps({sid: {"done": False, "flag": True, "note": "cleared long ago"}})
    # Progress that parses but isn't this version's is unreadable too, per-source progress in
    # the new store included. Read as empty, a later version's store or a hand-edited one was
    # saved over with nothing on the first render.
    later = json.dumps({"v": 4, "checked": {"1" * 16: f"q1/{sid}"}})
    before = json.dumps({"v": 2, "checked": {"1" * 16: f"q1/{sid}"}})
    for text in ('{"v": 3, "checked": {', later, before, "null", "[]", legacy):
        result = run(tmp_path, [c], storage={STORE: text, V2: before, LEGACY: legacy})
        assert not rows(result)[f"q1/{sid}"]["flagged"], text
        assert "could not be read" in result["notice"], text
        assert STORE + ":unreadable" in result["notice"], text
        assert result["storage"][STORE + ":unreadable"] == text

    # The same holds for the store from before notes were hashed, read when there is no newer
    # one: it is never written, but falling back past it would bring those flags back too.
    for text in ('{"v": 2, "checked": {', later, json.dumps({"v": 3}), "null", legacy):
        result = run(tmp_path, [c], storage={V2: text, LEGACY: legacy})
        assert not rows(result)[f"q1/{sid}"]["flagged"], text
        assert "could not be read" in result["notice"], text
        assert result["storage"][STORE + ":unreadable"] == text
        assert result["storage"][V2] == text, "only read, never rewritten"

    # Imported, it is refused, and what the page holds is left alone.
    ticked = run(tmp_path, [c], actions=[{"do": "tick", "row": f"q1/{sid}", "checked": True}])
    for text in (later, "null", "[]"):
        refused = run(tmp_path, [c], storage=ticked["storage"],
                      actions=[{"do": "import", "text": text}])
        assert refused["alerts"] == ["Bad progress file"], text
        assert rows(refused)[f"q1/{sid}"]["checked"], text
        assert refused["storage"][STORE] == ticked["storage"][STORE], text


def test_stored_progress_is_sanitized(tmp_path):
    """Progress comes from localStorage or a file someone hands you, so the app coerces it into
    a known shape: keys and values whitelisted to their formats (a `__proto__` key is dropped,
    not assigned), annotations reduced to the two fields it uses."""
    c = claim("q1", "The council approved the levy.")
    sid = c.sources[0].sid
    hostile = {"v": 3,
               "checked": {"__proto__": f"q1/{sid}", "0" * 16: "../q1/" + sid,
                           "1" * 16: f"q1/{sid}", "not a fingerprint": f"q1/{sid}",
                           "2" * 16 + "." + "3" * 16: f"q1/{sid}",
                           "4" * 16 + ".": f"q1/{sid}", "5" * 16 + ".6": f"q1/{sid}",
                           "7" * 16 + "." + "8" * 16 + "." + "9" * 16: f"q1/{sid}"},
               "sources": {"__proto__": {"flag": True},
                           sid: {"flag": "yes", "note": 7, "extra": "dropped"}},
               "notice": {"cleared": "<b>9</b>", "uncited": -1, "unreadable": "yes"},
               "extra": {"dropped": True}}
    two_part = {"2" * 16 + "." + "3" * 16: f"q1/{sid}"}
    older = json.dumps({**hostile, "v": 2})
    for result, kept in ((run(tmp_path, [c], storage={STORE: json.dumps(hostile)}), two_part),
                         (run(tmp_path, [c], actions=[{"do": "import",
                                                       "text": json.dumps(hostile)}]), two_part),
                         # A v2 page wrote only one-part fingerprints, and dropped any other.
                         (run(tmp_path, [c], storage={V2: older}), {}),
                         (run(tmp_path, [c], actions=[{"do": "import", "text": older}]), {})):
        assert stored(result) == {"v": 3, "checked": {"1" * 16: f"q1/{sid}", **kept},
                                  "sources": {sid: {"flag": False, "note": ""}}}
        assert not rows(result)[f"q1/{sid}"]["checked"]
        assert not rows(result)[f"q1/{sid}"]["flagged"]

    # A file that isn't JSON is refused, not half-applied.
    bad = run(tmp_path, [c], actions=[{"do": "import", "text": "{not json"}])
    assert bad["alerts"] == ["Bad progress file"]
    assert bad["fileInput"] == "", "the file input resets, so importing the same file works"


def test_a_claims_researcher_notes_render_escaped_and_only_when_present(tmp_path):
    """The researcher's notes carry what only a person can act on (a scan to read by eye, a
    filing that may not be the newest, a figure a query would not settle), so the page shows
    them on the claim. They are agent-authored, so they render escaped, and a claim with
    nothing to say shows nothing extra."""
    note = ("Page 3 is a scan: read it by eye.\n"
            "<script>alert(1)</script><img src=x onerror=\"alert(2)\">")
    noted, blank = claim("q1", "Approved."), claim("q3", "Approved.")
    noted.notes, blank.notes = note, " \n "
    html = render([noted, claim("q2", "Approved."), blank], tmp_path, title="T")[0].read_text()

    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, "the note renders, escaped"
    by_qid = {c.attributes["data-qid"]: c for c in HTMLParser(html).css(".claim")}
    assert {q: [n.text() for n in c.css(".rnote")] for q, c in by_qid.items()} == {
        "q1": ["Researcher's note (unverified): " + note], "q2": [], "q3": []}
    assert {q: c.attributes["data-notes"] for q, c in by_qid.items()} == {
        "q1": "1", "q2": "0", "q3": "0"}


def test_the_notes_filter_shows_the_claims_with_researcher_notes(tmp_path):
    noted = claim("q1", "Approved.")
    noted.notes = "The filing cited may not be the newest."
    result = run(tmp_path, [noted, claim("q2", "Approved.")],
                 actions=[{"do": "filter", "value": "notes"}])
    assert {c["qid"]: c["shown"] for c in result["claims"]} == {"q1": True, "q2": False}


NOTE = "The filing cited may not be the newest."


def test_a_note_added_after_checking_clears_the_claims_checks(tmp_path):
    """A check covers what the reviewer saw, and a claim's researcher note is part of that: it
    carries what a person has to act on. A note added after every row was checked left the
    claim reading done, dimmed and out of "Unchecked only", so the reviewer who had signed off
    never saw it. Now every check on the claim lapses, each row says the note is why, and the
    note is marked new."""
    answer = "The council approved the levy."
    k3 = f"q1/{cited().sid}"
    k7 = k3 + "/2"

    def build(notes="", *sources):
        c = claim("q1", answer, *(sources or (cited(page=3), cited(page=7))))
        c.notes = notes
        return c

    checked = run(tmp_path, [build()], actions=[{"do": "tick", "row": k3, "checked": True},
                                                {"do": "tick", "row": k7, "checked": True}])
    assert checked["claims"][0]["done"]

    added = run(tmp_path, [build(NOTE)], storage=checked["storage"],
                actions=[{"do": "filter", "value": "unchecked"}])
    for key in (k3, k7):
        row = rows(added)[key]
        assert not row["checked"], key
        assert row["noteStale"] and not row["stale"], "the row names the note, not the evidence"
    c = added["claims"][0]
    assert not c["done"] and c["shown"], "not done, so 'Unchecked only' shows it"
    assert c["noteChanged"], "the note itself is marked new"
    assert added["progress"].startswith("0/2")

    # Reloading changes nothing: the warning stays until the rows are checked again.
    reloaded = run(tmp_path, [build(NOTE)], storage=added["storage"])
    assert reloaded["claims"][0]["noteChanged"]
    assert all(r["noteStale"] for r in reloaded["rows"])

    # Checking the rows again, with the note on the page, records the note with them. The
    # marker stays until the last lapsed row is settled.
    half = run(tmp_path, [build(NOTE)], storage=added["storage"],
               actions=[{"do": "tick", "row": k3, "checked": True}])
    assert rows(half)[k3]["checked"] and rows(half)[k7]["noteStale"]
    assert half["claims"][0]["noteChanged"] and not half["claims"][0]["done"]
    rechecked = run(tmp_path, [build(NOTE)], storage=half["storage"],
                    actions=[{"do": "tick", "row": k7, "checked": True}])
    assert rechecked["claims"][0]["done"] and not rechecked["claims"][0]["noteChanged"]
    assert not any(r["noteStale"] or r["stale"] for r in rechecked["rows"])
    assert run(tmp_path, [build(f"\n  {NOTE}  \n")],
               storage=rechecked["storage"])["claims"][0]["done"], "the note as shown is unchanged"

    # A change to only what the page doesn't show (line endings, blanks at a line's end) is none.
    crlf = run(tmp_path, [build("Page 3 is a scan.  \nRead it by eye.")],
               storage=run(tmp_path, [build("Page 3 is a scan.\r\nRead it by eye.")],
                           actions=[{"do": "tick", "row": k3, "checked": True}])["storage"])
    assert rows(crlf)[k3]["checked"] and not rows(crlf)[k3]["noteStale"]

    # Changing the note lapses the checks again, and so does removing it: the check vouched for
    # the claim as it read with its caveat. The claim says which.
    for notes in ("The filing cited may not be the newest; page 3 is a scan.", ""):
        after = run(tmp_path, [build(notes)], storage=rechecked["storage"])
        assert not after["claims"][0]["done"], notes
        assert all(r["noteStale"] and not r["stale"] and not r["checked"]
                   for r in after["rows"]), notes
        assert after["claims"][0]["noteChanged"], notes
    page = HTMLParser(render([build(), build(NOTE)], tmp_path, title="T")[0].read_text())
    assert ["has been removed" in m.text() for m in page.css(".nchanged")] == [True, False]

    # Unchecking a row settles its warning, as it does for changed evidence.
    dismissed = run(tmp_path, [build("Page 3 is a scan.")], storage=rechecked["storage"],
                    actions=[{"do": "tick", "row": k3, "checked": True},
                             {"do": "tick", "row": k3, "checked": False}])
    assert not rows(dismissed)[k3]["noteStale"] and not rows(dismissed)[k3]["checked"]
    assert rows(dismissed)[k7]["noteStale"]

    # When the evidence changed as well, the row can't say the note is all that changed.
    moved = cited("The minutes record that the council approved the levy in closed session.",
                  page=3)
    both = run(tmp_path, [build("Page 3 is a scan.", moved, cited(page=7))],
               storage=rechecked["storage"])
    assert rows(both)[k3]["stale"] and not rows(both)[k3]["noteStale"]
    assert rows(both)[k7]["noteStale"] and not rows(both)[k7]["stale"]

    # Another lapsed check naming the row (one a claim that sat on this id before left behind)
    # keeps its own warning beside the note's.
    mixed = run(tmp_path, [build("Page 3 is a scan.")], storage={STORE: json.dumps(
        {"v": 3, "checked": {"0123456789abcdef": k3, rows(rechecked)[k3]["fp"]: k3}})})
    assert rows(mixed)[k3]["stale"] and rows(mixed)[k3]["noteStale"]


def test_a_note_change_is_told_by_the_row_the_check_was_made_on(tmp_path):
    """A check names the row it was made on, and says what changed there. A claim moved to
    another id keeps its check (it follows what it attests), so a note changed in a later
    rebuild is told on the new row. Moved and given a new note in one rebuild, it shows nothing
    the check recorded and nothing says where it went: finding it by content sent the warning to
    a twin instead. So it reads unchecked without a reason, and no other row is blamed."""
    answer = "The council approved the levy."
    sid = cited().sid

    def build(qid, notes, *sources):
        c = claim(qid, answer, *(sources or (cited(page=3), cited(page=7))))
        c.notes = notes
        return c

    checked = run(tmp_path, [build("q3", NOTE)],
                  actions=[{"do": "tick", "row": f"q3/{sid}", "checked": True},
                           {"do": "tick", "row": f"q3/{sid}/2", "checked": True}])
    assert checked["claims"][0]["done"]

    moved = run(tmp_path, [build("q7", NOTE)], storage=checked["storage"])
    assert moved["claims"][0]["done"]
    renoted = run(tmp_path, [build("q7", "Page 3 is a scan.")], storage=moved["storage"])
    assert all(r["noteStale"] and not r["stale"] for r in renoted["rows"])

    both = run(tmp_path, [build("q7", "Page 3 is a scan.")], storage=checked["storage"])
    assert not both["claims"][0]["done"]
    assert not any(r["checked"] or r["noteStale"] or r["stale"] for r in both["rows"])

    # Its two citations swapped and the note reworded: each row now shows other evidence than
    # its check was made on, so each says the claim or evidence changed.
    swapped = run(tmp_path, [build("q3", "Page 3 is a scan.", cited(page=7), cited(page=3))],
                  storage=checked["storage"])
    assert all(r["stale"] and not r["noteStale"] for r in swapped["rows"])


def test_a_note_change_on_one_claim_is_not_its_twins(tmp_path):
    """Two claims can ask and answer the same thing from the same citation (twins), so their
    rows' fingerprints match up to the notes. What changes on one is that claim's: its twin's row
    gets no warning, and a tick there settles nothing on the other."""
    answer = "The council approved the levy."
    one, two = f"q1/{cited().sid}", f"q2/{cited().sid}"

    def build(q1_notes, q2_notes="Page 3 is a scan.", q1_answer=answer):
        first, twin = claim("q1", q1_answer), claim("q2", answer)
        first.notes, twin.notes = q1_notes, q2_notes
        return [first, twin]

    checked = run(tmp_path, build(NOTE), actions=[{"do": "tick", "row": one, "checked": True}])
    changed = run(tmp_path, build("The filing cited is superseded."), storage=checked["storage"])
    assert rows(changed)[one]["noteStale"]
    assert not (rows(changed)[two]["noteStale"] or rows(changed)[two]["stale"])
    assert [c["noteChanged"] for c in changed["claims"]] == [True, False]

    ticked = run(tmp_path, build("The filing cited is superseded."), storage=checked["storage"],
                 actions=[{"do": "tick", "row": two, "checked": True}])
    assert rows(ticked)[two]["checked"] and rows(ticked)[one]["noteStale"]

    # A reworded answer on the checked one is its own change, not a note change on the twin.
    reworded = run(tmp_path, build(NOTE, q1_answer="The council rejected the levy."),
                   storage=checked["storage"])
    assert rows(reworded)[one]["stale"] and not rows(reworded)[one]["noteStale"]
    assert not (rows(reworded)[two]["noteStale"] or rows(reworded)[two]["stale"])

    # Twins with the same note share one check. It stays on the row it was ticked on, so when
    # that claim's note changes, that row says so and its twin keeps the check; checking the
    # row again doesn't take the check from the twin.
    shared = run(tmp_path, build(NOTE, NOTE), actions=[{"do": "tick", "row": one, "checked": True}])
    assert all(r["checked"] for r in shared["rows"])
    split = run(tmp_path, build("Page 3 is a scan.", NOTE), storage=shared["storage"])
    assert rows(split)[one]["noteStale"] and rows(split)[two]["checked"]
    again = run(tmp_path, build("Page 3 is a scan.", NOTE), storage=shared["storage"],
                actions=[{"do": "tick", "row": one, "checked": True}])
    assert rows(again)[one]["checked"] and rows(again)[two]["checked"]
    apart = run(tmp_path, build("Page 3 is a scan.", "The filing cited is superseded."),
                storage=shared["storage"])
    assert rows(apart)[one]["noteStale"], "the row it was ticked on says so, whatever row is last"


def test_a_note_change_warns_every_row_one_check_covered(tmp_path):
    """Rows that show exactly the same thing share a fingerprint, so one check covers them all
    and names only one. When the claim's note changes, each of them says so, not just the one
    the check names."""
    c = claim("q1", "The council approved the levy.", cited(), cited())
    key = f"q1/{c.sources[0].sid}"
    checked = run(tmp_path, [c], actions=[{"do": "tick", "row": key, "checked": True}])
    assert all(r["checked"] for r in checked["rows"]), "one fingerprint, one check"
    c.notes = NOTE
    changed = run(tmp_path, [c], storage=checked["storage"])
    assert all(r["noteStale"] and not r["checked"] for r in changed["rows"])


def test_progress_saved_before_notes_were_hashed_lapses_only_on_noted_claims(tmp_path):
    """Checks saved before this change recorded no note. A claim without notes hashes as it did,
    so its check stands and nothing is cleared wholesale. A claim that has notes now reads as
    changed, once, and its rows say the note is why. That progress sits under its own key,
    which this page only reads: a page from before would read two-part checks as its own
    version's, and drop them the next time it saved."""
    plain = claim("q1", "The council approved the levy.")
    noted = claim("q2", "The levy passed.", question="Did the levy pass?")
    sid = plain.sources[0].sid
    # What the page saved for each row before, when a fingerprint carried no notes.
    before = {"v": 2, "checked": {review_fingerprint(c, c.sources[0]): f"{c.question_id}/{sid}"
                                  for c in (plain, noted)},
              "sources": {sid: {"flag": True, "note": "check the vote count"}}}
    assert all("." not in fp for fp in before["checked"])
    noted.notes = NOTE

    result = run(tmp_path, [plain, noted], storage={V2: json.dumps(before)})
    assert result["storage"][V2] == json.dumps(before), "only read, never rewritten"
    assert stored(result)["v"] == 3
    assert rows(result)[f"q1/{sid}"]["checked"] and not rows(result)[f"q1/{sid}"]["stale"]
    row = rows(result)[f"q2/{sid}"]
    assert not row["checked"] and row["noteStale"] and not row["stale"]
    assert [(c["done"], c["noteChanged"]) for c in result["claims"]] == [(True, False),
                                                                         (False, True)]
    assert result["notice"] == "", "nothing was migrated, so there is nothing to announce"
    assert all(r["flagged"] and r["note"] == "check the vote count" for r in result["rows"])
