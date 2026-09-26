"""Render the review app + the raw claims JSON."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from . import queries
from .models import QID_PATTERN, Claim, Source
from .verify import secondary_host

# Package data, so an installed copy (uv tool install) has it. files() gives a Path for a
# package on disk, which Jinja's FileSystemLoader needs; anything else fails loudly here.
TEMPLATES = Path(files(__package__) / "templates")

# What a build leaves in out/. review.html is what `provenance serve` serves, so it goes first here and
# is written last: while it is there, a build finished writing it.
REVIEW_HTML, CLAIMS_JSON = "review.html", "claims.json"


def _temp_for(p: Path, pid: int | str) -> Path:
    return p.with_name(f".{p.name}.{pid}.tmp")


def clear_render(out_dir: Path) -> None:
    """Remove the review app an earlier build rendered, before this build can refuse.

    A build that exits early writes nothing, so the last render stayed in out/ and `provenance serve`
    served it: a reviewer ticked a page the pipeline had just refused to produce. It is
    regenerable, and the checkboxes live in the browser, keyed by title, so nothing is lost.
    A temp file a killed build left goes too: serve lists out/, dotfiles included, and one
    can hold a whole page no build finished.
    """
    for name in (REVIEW_HTML, CLAIMS_JSON):
        (out_dir / name).unlink(missing_ok=True)
        for tmp in out_dir.glob(_temp_for(Path(name), "*").name):
            tmp.unlink(missing_ok=True)


def _write_whole(p: Path, text: str) -> None:
    """Replace `p` whole, as `judgments._write()` does: a temp file beside it, on disk, then
    renamed over it. `provenance serve` may be reading review.html while a build writes it, and a
    build killed mid-write must not leave half a page."""
    tmp = _temp_for(p, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())   # otherwise a power loss can keep the rename and lose the text
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


BADGE = {
    "verified": "ok",
    "verified_via_archive": "ok",
    "pdf_normalized_match": "warn",
    "normalized_match": "warn",
    "could_not_verify_paywall": "warn",
    "pending": "mut",
}

# The review app records which ROW each check was made on, to say when that row's evidence has
# changed since: `<question id>/<source id>`, plus `/<n>` on the n-th citation of one source in
# one claim, so every row has its own. The page whitelists stored values to this shape; `/` is
# outside QID_PATTERN, so it is unambiguous.
ROW_KEY_RE = QID_PATTERN.removesuffix("$") + "/[0-9a-f]{12}(/[1-9][0-9]{0,3})?$"


def shown_notes(claim: Claim) -> str:
    """The claim's researcher notes as the review page shows them, or "" for none.

    What doesn't show is folded, so a retry that changes only that clears no check: a line
    ending (the HTML parser reads CR and CRLF as LF), and white space at the end of a line or
    around the whole, both read as `str.strip()` reads it."""
    lines = (claim.notes or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def review_fingerprint(claim: Claim, source: Source) -> str:
    """What a reviewer's "verified by me" on one row attests: this source, as cited and shown,
    supports this claim, as the claim reads with its researcher notes.

    The page counts a row as checked only while some recorded check carries this fingerprint.
    The source id alone covers just the url and snippet, so a check keyed by it counted for
    every question citing the source, and survived a re-fetch or new snapshot that changed the
    text around the snippet. So this hashes the claim, the citation as the researcher asserted
    it and the row shows it, and the evidence: the highlighted excerpt; where a row shows none
    (a paywall, a scan), the snapshot offered in its place; for a query row, the definition and
    export its figure was checked against.

    It hashes identity, never display: the excerpt's own text and offsets, not the markup around
    them, and not the printed command or the query's note, which move with a `--cache` spelling
    or a reworded message while the evidence stays put. And it is content, not position: the
    question id is left out, so a claim moved to another id without changing keeps its check,
    while a reworded one loses it. Pipeline verdicts (status, support) are left out: they don't
    change what was read.

    The claim's notes are shown above its rows and carry what only a person can act on, so a
    check covers them too, and a note added, changed or removed since clears it. They are a
    part of their own, after a dot, so the page can tell a changed note from changed evidence
    and say which. And only when the claim has notes: a claim without them hashes exactly as it
    did before notes were hashed, so checks saved then still stand.
    """
    v = source.verification
    if source.query is not None:
        run = v.query_run
        evidence = [str(run.version), run.export_date] if run else [""]
    elif context_html(source) is not None:
        evidence = [v.context, *map(str, v.context_offset)]
    else:
        evidence = [source.archive_url or ""]
    parts = [source.sid, claim.question, claim.answer, source.publisher, source.author,
             source.date or "", str(source.page or ""), source.secondary_host_ack or "",
             *evidence]
    # JSON, not a join: these fields are agent-authored, and a separator one of them contains
    # would let two different rows hash alike.
    fp = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]
    if notes := shown_notes(claim):
        fp += "." + hashlib.sha256(json.dumps(notes).encode()).hexdigest()[:16]
    return fp


def context_html(claim_source) -> Markup | None:
    v = claim_source.verification
    if not v.context or not v.context_offset:
        return None
    s, e = v.context_offset
    t = v.context
    return Markup(
        f"…{escape(t[:s])}<mark>{escape(t[s:e])}</mark>{escape(t[e:])}…")


def query_provenance(claim_source) -> str:
    """Which definition, export and database the printed command reproduces, or "".

    Printed beside the command so a reviewer whose re-run disagrees can tell a changed
    definition or a newer export from a wrong citation."""
    run = claim_source.verification.query_run
    q = claim_source.query
    if q is None or run is None:
        return ""
    parts = [f"{q.name} v{run.version}"]
    if data := queries.dataset(q.name):
        parts.append(queries.describe_export(run.export_date, data))
    parts.append(f"database root {run.cache_root}")
    return " · ".join(parts)


def render(claims: list[Claim], out_dir: Path, *, title: str = "voter guide",
           cache_root: Path | None = None,
           rules: dict[str, tuple[str, ...]] | None = None) -> tuple[Path, Path]:
    """`cache_root` is the root this build resolved: the `--cache` for a query row that carries
    no stamp of its own (one build did not re-run, whose file stamp revalidation dropped).
    `rules` are the project's source lists, which the "copy, not the issuing authority" badge
    is decided by, as `provenance check-claim` decides it."""
    from .cli import qid_sort_key

    claims = sorted(claims, key=lambda c: qid_sort_key(c.question_id))
    out_dir.mkdir(parents=True, exist_ok=True)

    # autoescape=True, NOT select_autoescape(): that helper matches on the filename
    # suffix, and this template is "review.html.j2" — it ends in .j2, so the predicate
    # fell through to default=False and every {{ }} in the review app rendered raw.
    # Everything in here (claim text, publisher, snippets) is agent-authored and derived
    # from fetched pages, so escaping is the whole defense. context_html() returns Markup
    # and is unaffected; it escapes its own interpolations.
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    tpl = env.get_template("review.html.j2")

    def source_views(c: Claim) -> list[SimpleNamespace]:
        views, seen = [], Counter()
        for s in c.sources:
            seen[s.sid] += 1
            row_key = f"{c.question_id}/{s.sid}" + (f"/{seen[s.sid]}" if seen[s.sid] > 1 else "")
            command = (queries.human_command(
                s.query.name, dict(s.query.params),
                s.verification.query_run.cache_root if s.verification.query_run
                else (str(cache_root) if cache_root else None)) if s.query else "")
            views.append(SimpleNamespace(
                **s.model_dump(), sid=s.sid, row_key=row_key,
                fingerprint=review_fingerprint(c, s), context_html=context_html(s),
                badge_class=BADGE.get(s.verification.status, "bad"),
                secondary=secondary_host(s, rules), query_command=command,
                query_provenance=query_provenance(s)))
        return views

    # Build explicit view objects rather than writing render-only attributes onto the
    # models: assigning into a pydantic instance's __dict__ shadows computed properties
    # like Source.sid and leaves the model in a state nothing else can trust.
    view = [
        SimpleNamespace(
            question_id=c.question_id, question=c.question, answer=c.answer,
            claim_type=c.claim_type, confidence=c.confidence, status=c.status,
            corroboration_ok=c.corroboration_ok, corroboration_note=c.corroboration_note,
            conflicts=c.conflicts, sources=source_views(c),
            # The researcher's caveats for the person checking this claim: a scan to read by
            # eye, a filing that may not be the newest, a figure a query would not settle.
            # Agent-authored, so autoescaped like the rest. What is shown is what a check
            # covers (review_fingerprint), so both read it through shown_notes().
            notes=shown_notes(c),
        )
        for c in claims
    ]

    # Keyed by the race, NOT by a hash of the question set. Checks carry stable fingerprints
    # (review_fingerprint) and flags stable source ids, so keying storage by the question set
    # would silently discard every checkbox the moment a question is added, split, or dropped
    # — which happens constantly during a research run, and mid-review is exactly when losing
    # it hurts. A row whose claim or evidence changed loses only its own check.
    store_key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "voter-guide"

    html = tpl.render(
        claims=view,
        title=title,
        run_id=store_key,
        row_key_re=ROW_KEY_RE,
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        n_sources=sum(len(c.sources) for c in claims),
        status_counts=dict(Counter(c.status for c in claims).most_common()),
        conflict_claims=[c for c in claims if c.conflicts],
        # By settled status, not confidence: a not_found claim carrying a broken citation is
        # human_review, and must not be listed under "deliberate, not failure".
        not_found_claims=[c for c in claims if c.status == "not_found"],
    )

    json_path = out_dir / CLAIMS_JSON
    # `status` is a computed property, so model_dump_json drops it — and any consumer of
    # claims.json then cannot see the roll-up the HTML displays.
    export = []
    for c in claims:
        d = json.loads(c.model_dump_json())
        d["status"] = c.status
        for src_json, src_obj in zip(d.get("sources", []), c.sources):
            src_json["sid"] = src_obj.sid
            src_json["secondary_host"] = secondary_host(src_obj, rules)
        export.append(d)
    _write_whole(json_path, json.dumps(export, indent=2))

    html_path = out_dir / REVIEW_HTML
    _write_whole(html_path, html)   # last: `provenance serve` serves it, so it means the rest is written
    return html_path, json_path
