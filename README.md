# provenance

A research pipeline where every claim is traceable to a human-written source, and a human can check each one in about fifteen seconds.

Agents find sources and make judgment calls. Deterministic code decides whether a citation is real:

- **The quote is literally on the page, exactly once.** Normalized matching exists for PDFs and copy-paste drift, but it is always reported as such and never passes silently as a match.
- **Structured data is checked by re-running the query.** A figure from a bulk dataset cites a named, parameterized query and the value it returned; verification runs the query again.
- **A fresh verifier judges support.** A second agent, which never wrote the claim, decides whether each source supports the specific claim or only touches the topic. An unjudged source is never shown as verified.
- **Everything ends at a local review page.** It shows each quote highlighted in its surrounding text, with links to the live page and an archived copy, so the human does the final check.

## Status

Imported from the private project where it was built, and still shaped by it: research is
organized as voter-guide races. The generalization is tracked as an epic in this repo's issues.
The tool ships with no race file: add one (see [Adding a race](#adding-a-race)) before
`provenance verify` or `provenance build`.

Citation verification pipeline for voter guides. Nothing race-specific lives in the pipeline
itself: each race is one file in `races/`.

**The premise:** every claim must be traceable to a human-written source, and a human must
be able to confirm any citation in ~15 seconds. Fabricated and misattributed citations are
the dominant failure mode of LLM research, so the mechanical checks — *is this string
literally on that page, exactly once?* — are deterministic Python. Models find sources and
judge whether context supports a claim. Models never set a verification status.

### Renamed from `vgpipe`

The package was `vgpipe` and the command `vg`. Both are now `provenance`: `uv run vg verify` is
`provenance verify`, and so on for every command.

A run verified before the rename still holds the reasons `vg verify` wrote into its claim files,
and some of them tell you or a researcher to run a `vg` command. Run `provenance verify`
on it once to rewrite them.

Two things keep the old name on purpose:

- **Review progress.** The review page stores your checks under a key that starts with `vgpipe:`,
  so checks made before the rename carry over.
- **Undoing an interrupted `vg remap`.** The rollback runs from a checkout from before the
  rename, so the message that sends you there names its commands as `vg`, the command that
  checkout has.

## Quick start

Install the command, `provenance`, once, from git at a release tag. The name `provenance` on
PyPI belongs to an unrelated project, so don't install it by name.

```bash
uv tool install git+https://github.com/maguerrieri/provenance@v0.1.0
```

Then run it from your research project's directory:

```bash
provenance --version                                                    # provenance 0.1.0
provenance races                                                        # what's defined
provenance check "https://example.org/article" "a short verbatim snippet" # ad-hoc
provenance verify        # deterministic checks over data/claims/*.json
provenance archive       # web.archive.org snapshots
provenance build         # conflicts + render the review app
provenance serve         # http://127.0.0.1:8765/review.html
```

To move to another release, install over it:

```bash
uv tool install --force git+https://github.com/maguerrieri/provenance@v<version>
```

Without installing, this runs one command:

```bash
uvx --from git+https://github.com/maguerrieri/provenance@v0.1.0 provenance <command>
```

It resolves the git source on every call, and floats with the default branch unless pinned,
so it suits a try-out, not an agent running hundreds of commands.

### The Claude Code plugin

The researcher and verifier agents and the orchestration skill are a Claude Code plugin,
`provenance`, listed in the `maguerrieri-toolbox` marketplace from its first release, v0.1.0:

```bash
claude plugin marketplace add maguerrieri/claude-toolbox
claude plugin install provenance@maguerrieri-toolbox
```

Until the marketplace lists a release, load it from a clone with `claude --plugin-dir <clone>`
(see "Developing the tool" below).

The plugin and the command are released together, at one version. Before a run the skill
checks `provenance --version` against the plugin's, and gives the install command for the
plugin's version when they differ: the agents run the commands and flags of that version.
Installed, the agents are `provenance:researcher` and `provenance:verifier`.

### Developing the tool

In a clone, `uv sync`, then `uv run provenance <command>` runs the clone's code and
`uv run pytest` the tests. `claude --plugin-dir <clone>` loads the clone's agents and skill
for one session; they run `provenance` from PATH, so `uv tool install --force --editable <clone>`
puts the clone's code there. A release bumps `version` in `pyproject.toml` and
`.claude-plugin/plugin.json` together (a test holds them equal, and every version the skill
and this README name to both), then tags the commit `v<version>` and pushes the tag.

## The review app

`provenance serve` opens a local checklist. Per source: the cached page context with your snippet
**highlighted in place**, plus the live link, the archive link, a copy-snippet button, and
a persistent checkbox. Keyboard: `j`/`k` move, `space` check, `f` flag, `o` open, `a`
archive, `c` copy. Filters: unchecked, adversarial, paywalled, conflicts, needs-review,
researcher notes.

A claim whose researcher left notes shows them above its sources: the caveats written for the
person checking it, such as a scan to read by eye, a filing that may not be the newest, or a
figure a query would not settle. The pipeline doesn't check them, so they are marked
unverified.

A check belongs to one claim: a source cited by two questions is checked under each
separately. It also clears itself when what you checked changes, and the row says so: the
claim (a reworded answer), the excerpt you read (a re-fetch that changes its text or
highlight), or, on a row with no excerpt, the snapshot offered instead (a new one from
`provenance archive`). Your flags and notes are about the source, and show wherever it is cited.

Serve it rather than opening `review.html` directly — several browsers disable
`localStorage` on `file://` origins, which silently loses your progress. Export/import
buttons cover the standalone case.

`provenance build` removes the previous `review.html` and `claims.json` from `out/` before anything
else, so a build that refuses or fails leaves nothing for `provenance serve` to show, and a reload
while any build runs gets a 404 until it finishes. If it can't remove them, it says so and
stops. Your checkmarks live in the browser, keyed by the title, and come back with the next
build that succeeds. A tab you already have open keeps its page until you reload it.

Reading the context is the point. ⌘F proves the words are on the page; only you can tell
whether they support the claim.

## Pipeline

| Phase | Who | What |
|---|---|---|
| 0 | main session | Split the template into atomic questions; a human approves the split |
| 1 | `provenance:researcher` agents, parallel | One question each → `data/claims/<qid>.json` |
| 2 | `provenance verify` + `provenance:verifier` agents | Mechanical checks, then the judgment half; ≤2 retries, then `human_review` |
| 3 | `provenance build` / `provenance serve` | Conflicts + review app + `claims.json` |

Orchestration lives in the plugin's skill, `skills/voter-guide-research/SKILL.md`; agent
definitions in `agents/`.

Question ids (`q1`, `q2a`) are **stable and never reused**. They name each question's claim and
verdict files, so a split or reworded question gets a new id and the old id is retired. To
retire one, move its claim out of `claims/` (to `claims-archive/`) and its verdict shard out of
`judgments/` (to `judgments-archive/`), and point any `derives_from` that names it at the new id. Nothing re-files a claim
onto another id: `vg remap`, which used to, is retired. `provenance build` and `provenance status` are the rule's
gate: a claim whose id `questions.json` no longer lists, or whose question differs from the one
its id names, is left out of the review app, and the command exits 1.

## Checks

| Check | Failure |
|---|---|
| URL resolves | `fetch_failed` — including a PDF that can't be opened, with the cause |
| Page has text to search | a scanned PDF (no text layer), or a scanned page of one that `page` points at, is `human_review`, with what to do — never `snippet_not_found` |
| Snippet appears literally | falls back to normalized, else `snippet_not_found`; our `[[page N]]` locators are never document text |
| Normalized fallback (whitespace/quotes/dashes/ligatures) | flagged `normalized_match` / `pdf_normalized_match` — never silent |
| Snippet appears exactly once | `snippet_not_unique` |
| Source class (`sources.yaml`) | `bad_source_class` |
| Paywall | `could_not_verify_paywall` — flagged, never failed; `provenance archive` then re-checks the snippet against the snapshot and upgrades to `verified_via_archive` when it's readable |
| Snapshot holds the cited page | a capture of another URL, a bot check, or one missing the snippet is `archive_unusable` (badged, no link); one that won't load, has nothing to compare against, or misses the snippet but has scanned or blank pages it could be on is `archive_unconfirmed` — warnings, never failures |
| Corroboration: 1 mechanical, 2 independent publishers adversarial | `human_review` |
| Claim answers the question its id names in `questions.json` | left out of the review app, and `provenance build` and `provenance status` exit 1; `provenance check-claim` fails the one claim |

`uv run pytest` covers all of them offline, including the four failure modes that matter:
fabricated quote, repeated snippet, smart-quote drift, excluded aggregator. The review-app
tests run the page's own script under Node, so they need `node` on the PATH; without it they
skip, except in CI, where they fail.

## Adding a race

A race is one file in `races/`: frontmatter naming its title and which source lists apply,
then prose context that goes verbatim into researcher prompts.

```yaml
---
name: example
title: 2030 Example County Assessor
election_date: 2030-11-05
sources: [us, ca]          # source_lists/us-sources.yaml + source_lists/ca-sources.yaml
---
```

`src/provenance/source_lists/us-sources.yaml` holds national outlets and the structural rules that apply
everywhere — lead-generators (Ballotpedia, Wikipedia) and excluded AI aggregators.
Regional lists (`ca-sources.yaml`, and any county or city list added to the tool) hold local outlets and
primary-document hosts. Lists merge, most-restrictive category wins, and unlisted domains
still pass if they carry a named or institutional author — so an unlisted local paper
degrades gracefully instead of being rejected. A new list is a change to the tool: add it under
`src/provenance/source_lists/` in a clone, since an installed copy's lists are replaced on the
next install.

New race → new file. No pipeline or skill edits.

## License

MIT. See [LICENSE](LICENSE).
