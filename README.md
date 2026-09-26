# provenance

A research pipeline where every claim is traceable to a human-written source, and a human can check each one in about fifteen seconds.

Agents find sources and make judgment calls. Deterministic code decides whether a citation is real:

- **The quote is literally on the page, exactly once.** Normalized matching exists for PDFs and copy-paste drift, but it is always reported as such and never passes silently as a match.
- **Structured data is checked by re-running the query.** A figure from a bulk dataset cites a named, parameterized query and the value it returned; verification runs the query again.
- **A fresh verifier judges support.** A second agent, which never wrote the claim, decides whether each source supports the specific claim or only touches the topic. An unjudged source is never shown as verified.
- **Everything ends at a local review page.** It shows each quote highlighted in its surrounding text, with links to the live page and an archived copy, so the human does the final check.

## Status

Imported from the private project where it was built, for one kind of research: candidates in
an election. Its core no longer assumes that. A project researches one or more subjects, and a
subject can be a person, a pending proposal or a document. The rest of the generalization is
tracked as an epic in this repo's issues. The tool ships with no project: write one (see
[Projects](#projects)) before running a command in it.

Nothing project-specific lives in the pipeline itself: a project's subjects, and what its
researchers are told, are in its own `provenance.toml`.

**The premise:** every claim must be traceable to a human-written source, and a human must
be able to confirm any citation in ~15 seconds. Fabricated and misattributed citations are
the dominant failure mode of LLM research, so the mechanical checks — *is this string
literally on that page, exactly once?* — are deterministic Python. Models find sources and
judge whether context supports a claim. Models never set a verification status.

### Renamed from `vgpipe`

The package was `vgpipe` and the command `vg`. Both are now `provenance`: `uv run vg verify` is
`provenance verify`, and so on for every command.

The orchestration skill was `voter-guide-research`. It is now `research`, invoked as
`provenance:research`.

A run verified before the rename still holds the reasons `vg verify` wrote into its claim files,
and some of them tell you or a researcher to run a `vg` command. Run `provenance verify`
on it once to rewrite them.

Two things keep the old name on purpose:

- **Review progress.** Checks made before the rename are under keys that start with `vgpipe:`,
  and the review page reads them to carry them over (see "The review app").
- **Undoing an interrupted `vg remap`.** The rollback runs from a checkout from before the
  rename, so the message that sends you there names its commands as `vg`, the command that
  checkout has.

## Quick start

Install the command, `provenance`, once, from git at a release tag. The name `provenance` on
PyPI belongs to an unrelated project, so don't install it by name.

```bash
uv tool install git+https://github.com/maguerrieri/provenance@v0.1.0
```

Then run it from your research project's directory, the one holding its `provenance.toml`
(write one first: see [Projects](#projects)):

```bash
provenance --version                                                    # provenance 0.1.0
provenance check "https://example.org/article" "a short verbatim snippet" # ad-hoc
provenance verify        # deterministic checks over the run's claims/*.json
provenance archive       # web.archive.org snapshots
provenance build         # conflicts + render the review app
provenance serve         # http://127.0.0.1:8765/review.html
```

A command that works on a run, or on the cache, runs in a project: from inside one, or with
`--project <dir>` (see [Projects](#projects)). Given `--cache`, the ones that only read the
cache (`check`, `fetch`, `query` and the `calaccess` commands) need none, and `form700` and the
`source-*` commands never read a project at all.

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
Installed, the agents are `provenance:researcher` and `provenance:verifier`, and the skill that
runs a project is `provenance:research`.

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
`provenance archive`). A researcher's note added, changed or removed after you checked a
claim clears your checks on it too: each row you had checked says the note is why, and so
does the claim. Checks made before the page recorded notes lapse once, the same way, on each
claim that has a note.
Your flags and notes are about the source, and show wherever it is cited.

Serve it rather than opening `review.html` directly — several browsers disable
`localStorage` on `file://` origins, which silently loses your progress. Export/import
buttons cover the standalone case.

`provenance build` removes the previous `review.html` and `claims.json` from `out/` before anything
else, so a build that refuses or fails leaves nothing for `provenance serve` to show, and a reload
while any build runs gets a 404 until it finishes. If it can't remove them, it says so and
stops. Your checkmarks live in the browser and come back with the next build that succeeds. A
tab you already have open keeps its page until you reload it.

They are kept per project and subject: by the project's `name` and the subject's id, never by
the page's title, so two subjects never share them, nor two versions of an amended proposal
(two subjects). Renaming either starts that review over, so export your progress first. Two
projects with the same `name` share progress between their subjects of one id, since
`provenance serve` serves every project from one origin: give each project its own `name`.
Progress saved before it was kept this way was kept by the page's title. It carries over once,
to the first run whose page has the title it was saved under. For a run built under another
title, build once with `--title '<that title>'`. Another run whose page has that title (or
this one, after the project is renamed) starts over and says so: export the progress from the
page it went to, and import it where it belongs.

Reading the context is the point. ⌘F proves the words are on the page; only you can tell
whether they support the claim.

## Pipeline

| Phase | Who | What |
|---|---|---|
| 0 | main session | Split the template into atomic questions; a human approves the split |
| 1 | `provenance:researcher` agents, parallel | One question each → `claims/<qid>.json` in the run |
| 2 | `provenance verify` + `provenance:verifier` agents | Mechanical checks, then the judgment half; ≤2 retries, then `human_review` |
| 3 | `provenance build` / `provenance serve` | Conflicts + review app + `claims.json` |

Orchestration lives in the plugin's skill, `skills/research/SKILL.md`; agent
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

## Projects

A project is a directory holding a `provenance.toml`. Every command finds it the same way: the
nearest one at or above the run it is given (`--data`, else the working directory), or the one
`--project` names. Nothing is inferred from which directories happen to exist.

```toml
name = "example"
title = "Example County Assessor"   # optional: the review page's title, `name` if left out
sources = ["us", "ca"]              # the source lists citations are checked against
primary_hosts = ["records.example.gov"]   # optional: hosts that issue this project's records
cache = "."                         # the directory that holds cache/, as --cache names it
subjects = [                        # optional: a run for each, in its own subdirectory
  {id = "lind", name = "Avery Lind"},
  {id = "ng", name = "Jordan Ng"},
]
context = """
Where the records are: appeals board decisions are PDFs on the county site, one per hearing.
"""
completeness_check = """
- A settlement the research should surface on its own.
"""
```

- **`primary_hosts`** names the issuing authorities for the project's own records that its
  source lists leave out: an agency's records site, a standards body's document tracker. A
  `primary_document`, `official_record` or `official_analysis` cited from a host neither names
  is a copy: `provenance check-claim` fails it, and `provenance build` fails its claim's
  corroboration, until it carries `secondary_host_ack`. Each entry is a bare
  host (`records.example.gov`, no scheme, path or `www.`) and covers its subdomains, so name the
  authority's own host, never a suffix shared with others. A host the lists already class
  (excluded, a lead generator, campaign material, a news outlet) is refused: the project adds
  authorities, and can't reclass a host. The project file is a person's to edit: a researcher
  that names its own issuing authorities can pass off any copy as the record, so one that meets
  an unlisted authority hands the claim on with a note instead. Once a person adds the host,
  the next `provenance build` releases the claim.
- **`cache`** is where the shared page cache and the CAL-ACCESS database live, relative to the
  file, with `~` expanded. `"."` keeps them beside it. The same path in several projects, such
  as `"~/.cache/provenance"`, shares one cache between them. `--cache` overrides it.
- **`subjects`** lists the project's separate runs, each in its own subdirectory with its own
  claims, verdicts, question set and review progress. A subject is an `id`, its directory, and
  a `name`, what the questions call it. It need not be a person: a pending proposal or a
  document works the same way, and two versions of an amended proposal are two subjects, so
  their reviews never mix. `provenance new-subject <id>` scaffolds one, but only for a subject
  listed here: no command edits the project file. Without subjects, the project root is the
  only run.
- **`context`** is what every researcher is told, verbatim. See [What researchers are
  told](#what-researchers-are-told).
- **`completeness_check`** is what no researcher is told: the answers already known.

A run is the project root or a subject's directory: `provenance verify` works on the root
(from anywhere in the project, a subject's directory included, which it then says), and
`provenance verify --subject lind` (or `--data lind`) on a subject. Any other `--data`, or a
`--subject` the project doesn't list, is refused. Each run holds its own `questions.json`,
`claims/`, `judgments/`, `archives.json` and `out/`. A subject's `questions.json` is its own
copy, retargeted to it: `new-subject` replaces the other subjects' names in the template's
questions with this one's. It never falls back to the root's, and a subject without one, in a
project that has one, fails the question check until it gets its copy.

The review page is titled by the run: a subject's by its name and the project's title, the
root's by the project's title. `provenance build --title` changes what it shows, not where its
progress is kept.

### Moving a project laid out the old way

Before project files, a project lived in `data/`: its question set, claims and cache there, and
a run per subject in `data/<subject>/`, with the cache found by inference, and a race file in
the tool's `races/` holding its title, context and completeness check. No command adopts that
layout: each one refuses until a project file exists. To move one, write
`data/provenance.toml`:

```toml
name = "<the race's name>"
title = "<the race file's title>"
sources = ["us", "ca"]                 # moved from the race file's `sources:`
cache = "."                            # data/cache, as before
subjects = [{id = "<id>", name = "<name>"}]   # each data/<id>/, named as the race file's candidates
context = """<the race file's prose, every section but its completeness check>"""
completeness_check = """<the race file's completeness check section>"""
```

The race file's completeness check ran from its `# Completeness check` heading to the next
top-level heading. A section after it (a list of places to look, say) was context, and goes
into `context` with the rest.

Give any subject without a `questions.json` its own copy. Commit the project file in the
project's own repository, so the move is reviewed like any other change. Commands then run in
`data/`, with `--subject <id>` for a subject's run. Nothing else moves. A project file written
before subjects had names, or naming its race file with `race`, is refused with what to write
instead.

## What researchers are told

`provenance brief` prints what goes into every researcher's and verifier's prompt, verbatim:
the project, the run's subject, `context`, the issuing authorities (the source lists'
primary-document hosts and `primary_hosts`), and the notes that ship with each source list the
project names (see [Source lists](#source-lists)). It never prints `completeness_check`.

Keep `context` thin. It reaches every researcher and verifier unverified: no snippet, no source, nothing
checks it, so a factual claim placed there is believed by every researcher and checked by
none. It earns its place by helping researchers find records (which bodies keep minutes, which
host covers which years). Anything else is a question with a citation.

`completeness_check` holds the answers already known. A researcher told what it is looking for
confirms that item instead of searching, so nothing off the list surfaces. Check it *after*
the results are in, and treat a gap as a finding. A `context` holding a completeness check
heading, as a race file's pasted body would, is refused.

## Source lists

The source lists are the project's (`sources`).
`src/provenance/source_lists/us-sources.yaml` holds national outlets and the structural rules that apply
everywhere — lead-generators (Ballotpedia, Wikipedia) and excluded AI aggregators.
Regional lists (`ca-sources.yaml`, and any county or city list added to the tool) hold local outlets and
primary-document hosts. Lists merge, most-restrictive category wins, and unlisted domains
still pass if they carry a named or institutional author — so an unlisted local paper
degrades gracefully instead of being rejected. Passing is not counting as reporting, though:
each citation's tier comes from its own `source_type` (primary text, official analysis,
reporting, opinion, advocacy), and reporting on a host no list names as a news outlet is an
unlisted outlet. Opinion, advocacy and an unlisted outlet support only "X argues Y": the answer
must name the source's author or publisher, and together they count as one source toward
corroboration. A list also names, under `legal_text`, the hosts that publish the text of law
(statutes, codes, regulations), whatever their class. Legal text is
a series, so `provenance check-claim` fails a citation on one of them that doesn't give the
effective date or version it quotes in `date` (a placeholder such as `n/a` or `current` gives
none). A list that isn't exactly these keys, each a list of bare host names (`example.gov`: no
scheme, path, port or `www.`), is refused, and every command names it as a problem with the
project. A new list is a change to the tool: add it under
`src/provenance/source_lists/` in a clone, since an installed copy's lists are replaced on the
next install. The hosts that issue one project's own records go in its `primary_hosts` instead.

A list can ship notes beside it, `<name>-notes.md`: how its records behave, such as which
filings come in series and which portals answer only through a bulk export. `provenance brief`
appends the notes of every list the project names, so a project that names the list gets them
and one that doesn't never sees them. The `ca` list's notes hold the California election,
legislative and campaign-finance guidance; the skill and agents carry none.

A new project is a new `provenance.toml`. No pipeline or skill edits.

## License

MIT. See [LICENSE](LICENSE).
