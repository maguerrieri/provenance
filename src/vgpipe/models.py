"""The claim schema. Agents own the research fields; the pipeline owns verification
fields. Keeping that boundary explicit is deliberate — an agent that can write its own
`verification.status` can mark its own fabrication verified.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# `own_statement` covers an organization speaking for itself on its own site: a party's
# endorsement, a union's own announcement. It is a primary source for the fact that the
# entity said it, which is exactly what an endorsement claim asserts. Distinct from
# `campaign_statement` (the candidate's own campaign) only in whose statement it is.
SourceType = Literal["bylined_journalism", "primary_document", "official_record",
                     "campaign_statement", "own_statement"]
ClaimType = Literal["mechanical", "adversarial"]
Confidence = Literal["direct", "inferred", "not_found"]

VerifyStatus = Literal[
    "verified",
    "pdf_normalized_match",
    "normalized_match",
    "could_not_verify_paywall",
    "verified_via_archive",
    "snippet_not_found",
    "snippet_not_unique",
    "snippet_too_short",
    "fetch_failed",
    "bad_source_class",
    "human_review",
    "pending",
]

SupportVerdict = Literal["supports", "topic_only", "contradicts", "superseded", "unreviewed"]

# The citation itself did not hold up. No verdict can make a claim resting on one verified, so
# in Claim.status these outrank everything else a source can say, "waiting for a verdict" too.
MECHANICAL_FAILURES = frozenset({"snippet_not_found", "snippet_not_unique", "snippet_too_short",
                                 "fetch_failed", "bad_source_class", "human_review"})

# Whether the snapshot `vg archive` saved actually holds the cited page. A successful save is
# not a usable snapshot: SPN reports success on bot-protected pages and captures the bot check.
#   archived             — checked: the snippet is in it, or it matches the live page
#   archive_unconfirmed  — a capture of the cited URL that could not be checked: nothing to
#                          compare it with (live page unreadable, no snippet), or it would
#                          not load. Offered, not vouched for
#   archive_unusable     — checked, and it is not the cited page; no link is offered
#   archive_failed       — nothing could be saved
ArchiveStatus = Literal["archived", "archive_unconfirmed", "archive_unusable", "archive_failed"]


class QueryRun(BaseModel):
    """What a query citation was last checked against. Machine-owned, inside `Verification`.

    A query citation promises that the reviewer can reproduce the figure exactly as cited. That
    takes three facts the citation itself does not carry: which definition of the query ran
    (definitions have changed under the same name), which export the database was built from
    (it refreshes nightly), and which database was read (`--cache` moves it).
    """

    version: int
    export_date: str = ""     # YYYY-MM-DD; "" when the database predates dated exports
    cache_root: str = ""      # the root the database was resolved from, as the run was given it

    @field_validator("cache_root")
    @classmethod
    def _printable(cls, v: str) -> str:
        """It is printed into a command a human pastes. Checked on load too: a trusted load
        (`vg status`, `vg judgments`) reads a stamp back from a claim file anyone can edit."""
        from .queries import unprintable

        if unprintable(v):
            raise ValueError("cache_root contains a control or invisible character")
        return v


class PageCopy(BaseModel):
    """Which cached copy of a page a source's context was built from. Machine-owned, inside
    `Verification`.

    `url` is the cache key the copy sits under: the cited page, or the snapshot for a
    `verified_via_archive` row, whose context comes from the Wayback capture and not from the
    paywall stub at the cited URL. A verdict is about the copy the verifier read, so `vg judge`
    stamps this one rather than whatever is cached when it runs, and refuses when the
    cache no longer holds it.
    """

    url: str
    fetched_at: datetime
    extractor_version: int


class Verification(BaseModel):
    """Machine-owned. Researcher agents must not write this."""

    status: VerifyStatus = "pending"
    reason: str | None = None
    match_count: int | None = None
    matched_offset: int | None = None
    context: str | None = None
    context_offset: tuple[int, int] | None = None
    checked_at: datetime | None = None
    attempts: int = 0
    # Judgment half, from a fresh verifier agent — never the authoring agent.
    support: SupportVerdict = "unreviewed"
    support_note: str | None = None
    # Query citations only: set whenever the query ran, matched or not.
    query_run: QueryRun | None = None
    # Set wherever `context` is set from a page, and cleared with it.
    context_page: PageCopy | None = None


class QueryCitation(BaseModel):
    """A citation verified by re-running a query, not by finding text on a page.

    For structured data this is stronger than a snippet: it is reproducible on demand, and a
    reviewer runs the same one-liner the pipeline ran instead of hunting for a string that may
    not appear as text anywhere. `name` must be a registered query — agents do not write SQL.
    """

    name: str
    params: dict[str, str] = Field(default_factory=dict)
    expected: str
    detail: str = ""

    @model_validator(mode="after")
    def _printable_as_a_command(self) -> QueryCitation:
        """Refuse a query the review page cannot print as a safe command to paste.

        The name, keys and values all end up in a shell command a human copies and runs.
        Quoting makes printable text inert, but not a control character, a name read as an
        option, or a key `vg query` splits differently — see `queries.unsafe_reason`. Checked
        here, like `_http_only`, so no consumer of a loaded claim has to remember to.
        """
        from .queries import unsafe_reason

        if reason := unsafe_reason(self.name, self.params):
            raise ValueError(reason)
        return self


class Source(BaseModel):
    url: str
    archive_url: str | None = None

    @field_validator("archive_url")
    @classmethod
    def _wayback_only(cls, v: str | None) -> str | None:
        """archive_url must point at the Wayback Machine.

        verify_against_archive() FETCHES this URL and treats a snippet found there as
        evidence, so an arbitrary host would let an agent stand up a page containing its
        own fabricated quote and earn `verified_via_archive` from it.

        The host is necessary, not sufficient: anyone can Save Page Now a page they control,
        so the capture must also be OF the cited URL (`archive.snapshot_of`, checked where
        the snapshot is used), and the value comes only from the run's archive records,
        never from a claim file.

        Specifically `web.archive.org/web/` — NOT archive.org generally, whose hosts serve
        user-uploaded items. A bare `archive.org` allowance would hand the attacker back
        the page-control this check exists to deny.
        """
        if v is None:
            return v
        if not re.match(r"^https://web\.archive\.org/web/", v.strip(), re.I):
            raise ValueError(
                f"archive_url must be a web.archive.org/web/ snapshot: {v!r}")
        return v

    @field_validator("url", "archive_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        """Reject non-HTTP schemes.

        These strings arrive in agent-authored JSON and are rendered straight into
        `<a href="...">` in the review app, where a `javascript:` or `data:` URL would
        execute when the reviewer clicks "open". Escaping doesn't help — the scheme is the
        payload — so the check belongs here, before anything can hold such a value.
        """
        if v is None:
            return v
        v = v.strip()
        if not re.match(r"^https?://", v, re.I):
            raise ValueError(f"url must be http(s): {v!r}")
        # Quotes, angle brackets, whitespace and control characters can't appear in a
        # legitimate URL and are exactly what an attribute breakout needs
        # (`https://x/" onmouseover="...`). Template autoescaping now covers this too, but
        # this docstring promises the value is safe to put in an href, so enforce it here
        # rather than depend on a caller getting the escaping right.
        if re.search(r'[\s"\'<>\\`]|[\x00-\x1f\x7f]', v):
            raise ValueError(f"url contains characters not valid in a URL: {v!r}")
        return v
    publisher: str
    author: str
    date: str | None = None
    source_type: SourceType
    snippet: str = Field(description="verbatim 5-10 word span, character-exact from the page")
    page: int | None = None
    paywall: bool = False
    # Set ONLY when citing a primary document from somewhere other than the issuing
    # authority's own host (a copy on a third-party site). Must say what you could not
    # reach and why this copy is the same document. Silence here is the bug: an
    # unreachable authority plus a silent substitution passes every other check.
    secondary_host_ack: str | None = None
    # Set instead of relying on `snippet` when the claim rests on a local dataset. The snippet
    # then describes what was looked up; the query is what actually gets checked.
    query: QueryCitation | None = None
    verification: Verification = Field(default_factory=Verification)
    # Pipeline-owned, like `verification` and `archive_url`: set from the run's archive
    # records by `verify.apply_archive()`, never from the claim file.
    archive_status: ArchiveStatus | None = None
    archive_note: str | None = None

    @property
    def context_url(self) -> str | None:
        """The page this source's context comes from: the snapshot for a `verified_via_archive`
        row, the cited URL for any other. None for an archive-verified row with no snapshot.

        The one place that choice is made: revalidation rebuilds the context from this page and
        a verdict is checked against it, and two copies of the rule could drift apart. Set
        `archive_url` from the run's records first (`verify.apply_archive()`); without it an
        archive row has no page, which reads as unverified and every verdict on it as stale."""
        if self.verification.status == "verified_via_archive":
            return self.archive_url
        return self.url

    @property
    def judged_bad(self) -> bool:
        """The verifier looked at this and said it does not support the claim."""
        return self.verification.support in ("topic_only", "contradicts", "superseded")

    @property
    def sid(self) -> str:
        """Identity of the thing a verifier judged.

        It must cover everything the verdict was about, because a judgment is keyed by sid and
        lapses only when the sid changes. For a text citation that is the url and the snippet.
        For a QUERY citation the assertion IS the query name, its parameters and the expected
        value — leaving those out let a `supports` verdict carry over when a query citation's
        expected value changed from one contributor to another, with url and snippet untouched:
        a verifier had checked one number and the row then vouched for another.
        """
        import hashlib

        parts = [self.url, self.snippet]
        if self.query is not None:
            parts += [self.query.name, self.query.expected,
                      ";".join(f"{k}={v}" for k, v in sorted(self.query.params.items()))]
        return hashlib.sha1("\x00".join(parts).encode()).hexdigest()[:12]


# Question IDs become filenames (`data/claims/<qid>.json`, `data/judgments/<qid>.json`),
# and agents supply them. Constrain the shape at the schema boundary so a traversal or
# absolute path in a claim file never reaches the filesystem layer. An id that never
# passes through the schema (`vg judge` takes one from its command line) is checked
# against this same pattern by `judgments.path_for()`.
QID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def is_question_id(v: object) -> bool:
    """Whether a value has the shape every claim's question_id has, for a value that never
    passes through the schema. No separator and no leading dot, so `<dir>/<id>.json` is always
    a direct child of `<dir>`. `fullmatch`, because `$` in a `match` also accepts a trailing
    newline."""
    return isinstance(v, str) and re.fullmatch(QID_PATTERN, v) is not None


class Claim(BaseModel):
    question_id: str = Field(pattern=QID_PATTERN)
    question: str
    answer: str
    claim_type: ClaimType = "mechanical"
    sources: list[Source] = Field(default_factory=list)
    confidence: Confidence = "direct"
    notes: str | None = None
    # Question ids this claim reasons FROM. A comparison ("how does their housing position
    # compare to an organization's platform?") is not a retrieval: its inputs are two other
    # claims. Naming them makes the dependency checkable — a conclusion cannot be verified
    # while an input it rests on is not.
    derives_from: list[str] = Field(default_factory=list)
    # Pipeline-owned:
    corroboration_ok: bool | None = None
    unmet_inputs: list[str] = Field(default_factory=list)
    corroboration_note: str | None = None
    conflicts: list[str] = Field(default_factory=list)

    @property
    def required_sources(self) -> int:
        return 2 if self.claim_type == "adversarial" else 1

    @property
    def status(self) -> str:
        """Roll-up used by the review app's filters and summary counts."""
        # An input that isn't verified poisons the conclusion drawn from it, however well
        # cited the conclusion's own sources are.
        if self.unmet_inputs:
            return "human_review"
        sts = [s.verification.status for s in self.sources]
        # A citation that failed needs fixing, and no verdict or other source changes that —
        # so it outranks everything below, `pending` included. Checked after the unjudged
        # test, it hid the failure: the verifier does not judge a quote that isn't on the
        # page, so a claim whose only source was snippet_not_found read `pending` forever.
        # It outranks `not_found` too: an absence claim needs no citation, but one it does
        # carry is shown to the reviewer, and a broken one must not hide under a muted badge.
        if any(st in MECHANICAL_FAILURES for st in sts):
            return "human_review"
        if self.confidence == "not_found":
            return "not_found"
        if not self.sources:
            return "human_review"
        # Mechanics say the quote is on the page; judgment says the page doesn't support the
        # claim. Rendering that green is the failure the whole pipeline exists to prevent —
        # it just moves the fabrication one layer out, from the quote to the argument.
        if all(s.judged_bad for s in self.sources):
            return "human_review"
        # An unjudged source is not a verified one. The judgment pass is where "the quote is
        # on the page" becomes "the page supports the claim", so a claim nobody has judged
        # has not been checked in the way that matters — and in testing the verifier silently
        # never ran, which rendered everything green.
        if any(s.verification.support == "unreviewed" for s in self.sources):
            return "pending"
        good = {"verified", "pdf_normalized_match", "normalized_match",
                "verified_via_archive"}
        if all(s in good for s in sts):
            # Only True counts. None means check_corroboration() hasn't run, and an
            # adversarial claim reading "verified" before its second source is checked is
            # exactly the false green this pipeline exists to prevent.
            if self.corroboration_ok is True:
                return "verified"
            return "pending" if self.corroboration_ok is None else "human_review"
        if any(s == "could_not_verify_paywall" for s in sts):
            return "could_not_verify_paywall"
        if any(s == "pending" for s in sts):
            return "pending"
        return "human_review"


# Fields the pipeline owns. Anything here that arrives in an agent-authored file is
# discarded on ingest — see `strip_machine_fields`.
MACHINE_CLAIM_FIELDS = ("corroboration_ok", "unmet_inputs", "corroboration_note", "conflicts")
# archive_url is evidence — verify_against_archive() fetches it and a snippet found there
# upgrades the source — so it is stripped like `verification`. It used to be kept, because
# `vg verify` strips and writes back and would have deleted every snapshot; its safety then
# rested on `vg archive` overwriting every recorded value, which it did not do on a URL where
# Save Page Now failed. The pipeline's own snapshots now live in the run's archive records
# (`archive.RECORDS`) and `verify.apply_archive()` puts them back, so nothing an agent
# writes here survives.
#
# The archive fields go further than `verification`: they are never read from a claim file in
# ANY load mode, trusted included. Nothing there is ever the authority for them, so a command
# that forgets `apply_archive()` shows no snapshot rather than whatever the file held.
ARCHIVE_SOURCE_FIELDS = ("archive_url", "archive_status", "archive_note")
MACHINE_SOURCE_FIELDS = ("verification", *ARCHIVE_SOURCE_FIELDS)


def check_archive_url(v: str) -> str:
    """Both of Source's archive_url validators, for a value assigned rather than parsed —
    assignment doesn't run them, and the host pin and href safety must hold regardless."""
    return Source._http_only(Source._wayback_only(v))


def strip_machine_fields(raw: dict, *, archive_only: bool = False) -> dict:
    """Drop pipeline-owned fields from an agent-authored claim before validation.

    Without this, a researcher agent could write `verification.status: "verified"` into
    its own claim file and `vg build` would render it as verified without `vg verify`
    ever having fetched the page — precisely the failure this pipeline exists to catch.
    Claim files are rewritten by the pipeline after each verify run, so real verification
    data survives via that path, not via whatever the file happened to contain.

    `archive_only` drops just the archive fields, for a trusted load: see
    ARCHIVE_SOURCE_FIELDS.
    """
    claim_fields = () if archive_only else MACHINE_CLAIM_FIELDS
    source_fields = ARCHIVE_SOURCE_FIELDS if archive_only else MACHINE_SOURCE_FIELDS
    claim = {k: v for k, v in raw.items() if k not in claim_fields}
    sources = raw.get("sources") or []
    if isinstance(sources, list):
        claim["sources"] = [
            {k: v for k, v in s.items() if k not in source_fields}
            if isinstance(s, dict) else s
            for s in sources
        ]
    return claim


class Question(BaseModel):
    id: str = Field(pattern=QID_PATTERN)
    text: str
    claim_type: ClaimType = "mechanical"
    parent: str | None = None
    rationale: str | None = None


# Bump when extraction changes in a way that alters what a page yields. The cache is
# authoritative by design, so a fetch-layer fix does NOT reach pages already cached — and
# nothing else signals that a cached page predates it. A roll-call page cached before the
# full-DOM fallback held only a name roster, so citations to it could not identify their own
# bill and were rejected as topic_only, which reads as a research failure rather than a stale
# cache.
EXTRACTOR_VERSION = 3


class RefetchFailure(BaseModel):
    """A re-fetch that failed, and so was not allowed to replace a good cached page.

    Kept on the page it spared, for two reasons: the failure stays visible after the run that
    hit it, and a page kept under an older extractor is not re-fetched on every run — the
    attempt already happened under the current extractor, and the host refused it."""
    attempted_at: datetime
    extractor_version: int
    status: int
    reason: str


class PageCache(BaseModel):
    extractor_version: int = 1
    url: str
    final_url: str
    status: int
    content_type: str
    title: str | None = None
    text: str = ""
    fetched_at: datetime
    is_pdf: bool = False
    paywall_suspected: bool = False
    error: str | None = None
    refetch_failure: RefetchFailure | None = None
