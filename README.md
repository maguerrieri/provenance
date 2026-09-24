# provenance

A research pipeline where every claim is traceable to a human-written source, and a human can check each one in about fifteen seconds.

Agents find sources and make judgment calls. Deterministic code decides whether a citation is real:

- **The quote is literally on the page, exactly once.** Normalized matching exists for PDFs and copy-paste drift, but it is always reported as such and never passes silently as a match.
- **Structured data is checked by re-running the query.** A figure from a bulk dataset cites a named, parameterized query and the value it returned; verification runs the query again.
- **A fresh verifier judges support.** A second agent, which never wrote the claim, decides whether each source supports the specific claim or only touches the topic. An unjudged source is never shown as verified.
- **Everything ends at a local review page.** It shows each quote highlighted in its surrounding text, with links to the live page and an archived copy, so the human does the final check.

## Status

Imported from the private project where it was built, and still shaped by it: the package is
`vgpipe`, the command is `vg`, and research is organized as voter-guide races. The rename and the
generalization are tracked as epics in this repo's issues. The tool ships with no race file: add
one (see [Adding a race](#adding-a-race)) before `vg verify` or `vg build`.

Citation verification pipeline for voter guides. Nothing race-specific lives in the pipeline
itself: each race is one file in `races/`.

**The premise:** every claim must be traceable to a human-written source, and a human must
be able to confirm any citation in ~15 seconds. Fabricated and misattributed citations are
the dominant failure mode of LLM research, so the mechanical checks — *is this string
literally on that page, exactly once?* — are deterministic Python. Models find sources and
judge whether context supports a claim. Models never set a verification status.

## Quick start

```bash
uv sync
uv run vg races                                                            # what's defined
uv run vg check "https://example.org/article" "a short verbatim snippet"   # ad-hoc
uv run vg verify        # deterministic checks over data/claims/*.json
uv run vg archive       # web.archive.org snapshots
uv run vg build         # conflicts + render the review app
uv run vg serve         # http://127.0.0.1:8765/review.html
```

## The review app

`vg serve` opens a local checklist. Per source: the cached page context with your snippet
**highlighted in place**, plus the live link, the archive link, a copy-snippet button, and
a persistent checkbox. Keyboard: `j`/`k` move, `space` check, `f` flag, `o` open, `a`
archive, `c` copy. Filters: unchecked, adversarial, paywalled, conflicts, needs-review.

Serve it rather than opening `review.html` directly — several browsers disable
`localStorage` on `file://` origins, which silently loses your progress. Export/import
buttons cover the standalone case.

Reading the context is the point. ⌘F proves the words are on the page; only you can tell
whether they support the claim.

## Pipeline

| Phase | Who | What |
|---|---|---|
| 0 | main session | Split the template into atomic questions; a human approves the split |
| 1 | `researcher` agents, parallel | One question each → `data/claims/<qid>.json` |
| 2 | `vg verify` + `verifier` agents | Mechanical checks, then the judgment half; ≤2 retries, then `human_review` |
| 3 | `vg build` / `vg serve` | Conflicts + review app + `claims.json` |

Orchestration lives in `.claude/skills/voter-guide-research/SKILL.md`; agent definitions in
`.claude/agents/`.

## Checks

| Check | Failure |
|---|---|
| URL resolves | `fetch_failed` — including a PDF that can't be opened, with the cause |
| Page has text to search | a scanned PDF (no text layer), or a scanned page of one that `page` points at, is `human_review`, with what to do — never `snippet_not_found` |
| Snippet appears literally | falls back to normalized, else `snippet_not_found`; our `[[page N]]` locators are never document text |
| Normalized fallback (whitespace/quotes/dashes/ligatures) | flagged `normalized_match` / `pdf_normalized_match` — never silent |
| Snippet appears exactly once | `snippet_not_unique` |
| Source class (`sources.yaml`) | `bad_source_class` |
| Paywall | `could_not_verify_paywall` — flagged, never failed; `vg archive` then re-checks the snippet against the snapshot and upgrades to `verified_via_archive` when it's readable |
| Snapshot holds the cited page | a capture of another URL, a bot check, or one missing the snippet is `archive_unusable` (badged, no link); one that won't load, has nothing to compare against, or misses the snippet but has scanned or blank pages it could be on is `archive_unconfirmed` — warnings, never failures |
| Corroboration: 1 mechanical, 2 independent publishers adversarial | `human_review` |

`uv run pytest` covers all of them offline, including the four failure modes that matter:
fabricated quote, repeated snippet, smart-quote drift, excluded aggregator.

## Adding a race

A race is one file in `races/`: frontmatter naming its title and which source lists apply,
then prose context that goes verbatim into researcher prompts.

```yaml
---
name: example
title: 2030 Example County Assessor
election_date: 2030-11-05
sources: [us, ca]          # sources/us-sources.yaml + sources/ca-sources.yaml
---
```

`sources/us-sources.yaml` holds national outlets and the structural rules that apply
everywhere — lead-generators (Ballotpedia, Wikipedia) and excluded AI aggregators.
Regional lists (`ca-sources.yaml`, and any county or city list you add) hold local outlets and
primary-document hosts. Lists merge, most-restrictive category wins, and unlisted domains
still pass if they carry a named or institutional author — so an unlisted local paper
degrades gracefully instead of being rejected.

New race → new file. No pipeline or skill edits.

## License

MIT. See [LICENSE](LICENSE).
