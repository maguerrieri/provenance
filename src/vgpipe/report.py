"""Render the review app + the raw claims JSON."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from html import escape
from types import SimpleNamespace
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from .models import Claim
from . import queries
from .verify import secondary_host

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

# What a build leaves in out/. review.html is what `vg serve` serves, so it goes first here and
# is written last: while it is there, a build finished writing it.
HTML, JSON = "review.html", "claims.json"


def clear_render(out_dir: Path) -> None:
    """Remove the review app an earlier build rendered, before this build can refuse.

    A build that exits early writes nothing, so the last render stayed in out/ and `vg serve`
    served it: a reviewer ticked a page the pipeline had just refused to produce. It is
    regenerable, and the checkboxes live in the browser, keyed by title, so nothing is lost.
    """
    for name in (HTML, JSON):
        (out_dir / name).unlink(missing_ok=True)


def _write_whole(p: Path, text: str) -> None:
    """Replace `p` whole, as `judgments._write()` does: a temp file beside it, on disk, then
    renamed over it. `vg serve` may be reading review.html while a build writes it, and a
    build killed mid-write must not leave half a page. The temp name carries the pid, so two
    builds of one run can't take each other's."""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
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
           cache_root: Path | None = None) -> tuple[Path, Path]:
    """`cache_root` is the root this build resolved: the `--cache` for a query row that carries
    no stamp of its own (one build did not re-run, whose file stamp revalidation dropped)."""
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

    # Build explicit view objects rather than writing render-only attributes onto the
    # models: assigning into a pydantic instance's __dict__ shadows computed properties
    # like Source.sid and leaves the model in a state nothing else can trust.
    view = [
        SimpleNamespace(
            question_id=c.question_id, question=c.question, answer=c.answer,
            claim_type=c.claim_type, confidence=c.confidence, status=c.status,
            corroboration_ok=c.corroboration_ok, corroboration_note=c.corroboration_note,
            conflicts=c.conflicts,
            sources=[
                SimpleNamespace(
                    **s.model_dump(), sid=s.sid, context_html=context_html(s),
                    badge_class=BADGE.get(s.verification.status, "bad"),
                    secondary=secondary_host(s),
                    query_command=(queries.human_command(
                        s.query.name, dict(s.query.params),
                        s.verification.query_run.cache_root if s.verification.query_run
                        else (str(cache_root) if cache_root else None)) if s.query else ""),
                    query_provenance=query_provenance(s))
                for s in c.sources
            ],
        )
        for c in claims
    ]

    # Keyed by the race, NOT by a hash of the question set. Sources carry stable ids
    # (url + snippet), so keying storage by the question set would silently discard every
    # checkbox the moment a question is added, split, or dropped — which happens
    # constantly during a research run, and mid-review is exactly when losing it hurts.
    store_key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "voter-guide"

    html = tpl.render(
        claims=view,
        title=title,
        run_id=store_key,
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        n_sources=sum(len(c.sources) for c in claims),
        status_counts=dict(Counter(c.status for c in claims).most_common()),
        conflict_claims=[c for c in claims if c.conflicts],
        # By settled status, not confidence: a not_found claim carrying a broken citation is
        # human_review, and must not be listed under "deliberate, not failure".
        not_found_claims=[c for c in claims if c.status == "not_found"],
    )

    json_path = out_dir / JSON
    # `status` is a computed property, so model_dump_json drops it — and any consumer of
    # claims.json then cannot see the roll-up the HTML displays.
    export = []
    for c in claims:
        d = json.loads(c.model_dump_json())
        d["status"] = c.status
        for src_json, src_obj in zip(d.get("sources", []), c.sources):
            src_json["sid"] = src_obj.sid
            src_json["secondary_host"] = secondary_host(src_obj)
        export.append(d)
    _write_whole(json_path, json.dumps(export, indent=2))

    html_path = out_dir / HTML
    _write_whole(html_path, html)   # last: `vg serve` serves it, so it means the rest is written
    return html_path, json_path
