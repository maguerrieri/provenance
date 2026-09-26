---
name: research
description: Use when running the provenance research pipeline for any project — fanning out research questions to subagents, verifying citations, and building the human review app. Covers the full template-to-review-app run.
---

# Cited research run

Three phases. The design principle throughout: **agents find and judge; Python decides
whether a citation is real.** Never let a model set a verification status.

## First — the `provenance` command, at this plugin's version

Every step below, and every command the agents run, is the `provenance` command. Check it
before anything else:

```
provenance --version
```

It must print `provenance 0.1.0`, this plugin's version. If the command is not found, or it
prints another version, stop and give the operator the fix:

```
uv tool install --force git+https://github.com/maguerrieri/provenance@v0.1.0
```

The agents' instructions name the commands and flags of this version, and a CLI of another
version can lack one or refuse what they write. Don't fall back to running it through `uv run`:
that works only inside a clone of the tool's own repo.

Run every `provenance` command from the project root, the directory holding its
`provenance.toml` (or pass `--project <root>`). Outside a project, a command that works on a
run, or on the cache without `--cache`, is refused.
Name a subject's run with `--subject <id>` (or `--data <its directory>`): with neither, a
command works on the root's run, wherever in the project it runs from. A subject need not be a
person: a proposal or a document works the same way, and two versions of an amended proposal
are two subjects.

## Where a run starts: `provenance new` or `provenance ask`

The operator starts a project with one of two commands, and each prints the command that opens
Claude Code in it with this skill invoked. Neither command researches anything: this skill does.

**`provenance new <dir> --from <template> --source <list>`** writes the project's
`provenance.toml` and copies the template to `template.md`, at the project root. Run every
phase below from there. Before Phase 0, read `provenance.toml` yourself: if its `context` is
empty, ask the operator where the records are (which bodies keep minutes, which hosts serve
which documents), since that is what every researcher and verifier is told. The operator edits
the file: no command does, and nor do you.

If `provenance.toml` lists `subjects`, each is a separate run. Once the operator approves the
split and Phase 0 has written the root's `questions.json`, run `provenance new-subject <id>`
for each subject. It copies the questions into the subject's directory, retargeted to the
subject's name. Then research each subject's run: its researchers get the subject's own
questions, its researchers and verifiers get `provenance brief --subject <id>`, and every
command after that takes `--subject <id>`. With no `subjects`, the root is the only run.

**`provenance ask "<question>" --source <list>`** creates a new project holding one question,
`q1`, in `questions.json`, with no template. Its hand-off invokes this skill with `ask`. A
project with a `questions.json` and no `template.md` is one too. For it, skip Phase 0, and
check two things before any researcher runs:
- **The question asks what the record shows.** One that names the answer it expects ("confirm
  that…") gets that answer back, and nothing else surfaces. Propose a rewording that asks what
  the record shows.
- **Its `claim_type`.** A question negative or contested about someone or something is
  `adversarial` (Phase 0, step 4). If it was not asked with `--adversarial`, say so.

Don't edit `questions.json` to fix either: `q1` names one question for the life of the
project. Give the operator the `provenance ask` command for the question as it should be
(with `--adversarial` if it should be), and stop. The first project's directory holds no
research yet, so they can remove it.

Then run Phases 1 to 3 as written, for the one question: one `provenance:researcher`, which
must pass `provenance check-claim`, and after `provenance verify`, one fresh
`provenance:verifier`, never the researcher's own session. The guarantees are the template
run's: the question asks what the record shows, the claim passes the check-claim gate, and a
fresh verifier judges every source.

## Phase 0 — split the template

1. Read the project's `template.md`.
2. Split compound questions ("record on X and Y") into **atomic** ones — one claim per
   question — with stable IDs: `q1`, `q2a`, `q2b`. A subagent handling two claims at once
   produces citations that support one of them. IDs are **never reused or renumbered**: claims
   and verdicts are filed by id, and nothing moves them. If a question is split or reworded
   later in a run, give each new question a new id and retire the old one. Move its claim
   file out of `claims/` (to `claims-archive/`) and its verdict shard out of `judgments/` (to
   `judgments-archive/`), and point any `derives_from` that names it at the new id. A reused id hands its old verdicts to
   the new claim. `provenance build` and `provenance status` are the rule's gate: a claim whose id
   `questions.json` no longer lists, or whose `question` differs from the text at its id, is
   left out of review, and the command exits 1. Once the new question's research has replaced
   the old claim, a reused id no longer shows, so give the new question its new id before
   anyone researches it.
3. Write the project root's `questions.json` as `[{"id", "text", "claim_type", "parent", "rationale"}]`.
4. Mark `claim_type: "adversarial"` for anything negative or contested about a subject
   (an allegation, a settlement, a conflict of interest). Adversarial claims
   need two independent sources, so this classification changes what gets researched.
5. **Show the operator the split and get approval before fanning out.** A bad split silently
   changes the question being answered — cheapest possible place to catch it.

## Phase 1 — research fan-out, in dependency order

Some questions are **comparisons**, not retrievals: "how does the subject's position on a topic
compare to the reference document the project measures against?" reasons from two other claims. Those declare `derives_from` in
questions.json, and the rule is simple — **research the inputs first, in a wave, then the
conclusions.** A comparison written before its inputs exist is guesswork wearing citations.

`check_inputs()` enforces it mechanically: a claim whose declared inputs are not themselves
verified is `human_review`, however well its own sources check out. So spawning the comparison
early does not save time; it just produces a row that cannot go green.

For a comparison with a reference document, cite its section by its anchor URL.
Where it has no section on a topic, say so: "the document takes no position here" is a real
finding, and stretching a nearby section to cover it is not.



Spawn one `provenance:researcher` subagent per atomic question, **in parallel** (they're
independent). Give each:

- the question text and its `claim_type`, verbatim from the run's own `questions.json`: for a
  subject's run that is the subject's own `<subject>/questions.json`, retargeted to the subject, not
  the template. The gate checks each claim's `question` against it,
- the run's brief, verbatim: what `provenance brief` prints (with `--subject <id>` for a
  subject's run),
- its `question_id` and the instruction to write `claims/<qid>.json` in its run (the project root, or the subject's directory).

Do not summarize the source rules for them — the agent definition carries them in full.

## Phase 2 — verification

1. `provenance verify` — deterministic checks on every source: URL resolves, snippet is
   literally on the page, snippet appears exactly once, source class is allowed, paywall
   detection. No model in this loop.
2. Spawn one `provenance:verifier` subagent per claim that passed the mechanical checks, for the
   judgment half: does the cached context actually support *this* claim, and (for
   adversarial claims) are the two sources genuinely independent? Give it the run's brief,
   verbatim, as for a researcher (its source lists' notes say which records come in series),
   and the question id and the run dir, not a copy of the claim: it reads the claim and each
   source's context
   from `provenance handoff <qid> --data <run>`, which prints every context with its sid and a
   **context token** naming the whole hand-off: the claim (its type included), and every
   source printed with it, each with its citation, its context and, for a query citation, the
   query run that produced it. Record each verdict with:

   ```
   provenance judge <qid> <sid> supports|topic_only|contradicts|superseded --context <token> --note "..."
   ```

   `provenance judge` refuses, writing nothing, unless that claim (exact id, case included) cites
   that sid, the cache still holds the copy of the page `provenance verify` built its context from
   (the snapshot, for a `verified_via_archive` source), that copy still gives the context the
   claim file holds, and the token names the hand-off `provenance handoff` would print now —
   so run it after `provenance verify`, and re-verify if another run re-fetched the page or
   `provenance archive` replaced the snapshot since. A verdict without a token is refused, a query
   citation's included. A token for an older hand-off means something in it changed while the
   verifier worked (a re-verify, a retry that rewrote the claim or swapped, added or dropped a
   source, a query re-run under a new definition, export or database, even one printing the
   same figure): the verifier re-reads what `provenance handoff` prints and judges that. A refusal names a wrong id, sid or `--data`, the copy
   that moved, or what changed; it is never a cue to file the verdict under some other claim.

   Judgments are stored in the run's `judgments/<qid>.json`, keyed by source id — **not** in the
   claim file, which `provenance verify` reloads with stripping on. A verdict written into the claim is
   destroyed by the next verify run. `provenance judgments` shows what has been recorded and ends
   with one line, `N of M cited source(s) need a verdict (K stale)`. That line is the gate:
   the pass is finished when `N` is 0. **Read the number; don't count rows off the table.**
   The table wraps, and `grep -c unreviewed` has under-reported twice. A stale verdict
   predates the page it judged (or, for a query citation, the query definition), or judged
   another question or answer than the claim gives now (a retry rewrote it), or was recorded
   before verdicts named their claim (a one-time re-judge of every such verdict), and
   `provenance build` won't apply it, so it counts in `N` until the source is judged again. A source a verifier has nothing to judge on is not in `N`. That
   means its citation failed, is paywalled, was never verified, or changed since the last
   `provenance verify`. Those are counted on a separate line, and each one's table row names its
   status (`unreviewed (<status>)`, or `unreviewed (run provenance verify)`). They go to the retry
   loop (step 3), `provenance archive` or `provenance verify`, not to a verifier. The count runs the same
   offline checks `provenance build` does, so the two counts together are what the review app will
   show without a verdict. The command exits 0 only when the pass is done: it exits 1 while
   `N` is above 0, and also when a claim file couldn't be read.
   For periodic filings (disclosure forms, annual reports, and the series the brief's notes
   name), the verifier also checks the filer's index for a **newer** filing and returns
   `superseded` if one exists — a stale form verifies perfectly, so nothing mechanical can
   catch it. Statute, code and regulation text is a series the same way: the verifier checks
   for a **newer version** than the one the citation's `date` names, and reads the definitions
   a quoted term points to, since a quote can match exactly and still mean something else.

   **Never** route a claim to the same agent that authored it.
3. **Retry loop.** Retry on the *verifier agent's* verdicts too, not only mechanical
   failures — `topic_only` and `superseded` mean the citation is wrong even though every
   mechanical check passed. A retry that only ever hears "snippet not found" fixes the
   quote and keeps the wrong document: that is how a superseded filing survives a retry.
   Say which kind of failure it was. A `topic_only` whose note says the claim states an
   argument as fact (an opinion or advocacy source) is not a wrong citation. Ask for the claim
   as "X argues Y", naming the source's author or publisher, or for a source that states the
   fact. Swapping in an opinion piece that agrees fixes nothing.

   **Not a retry: a `contradicts`.** It says the record argues against the claim, not that
   the citation is wrong. The claim is `human_review` whatever its other sources say, and
   the contradiction is listed with the conflicts. A retry that swaps the source for one
   that agrees resolves nothing: the claim stays `human_review`, with the contradiction
   still listed but only by source id, and only a human can clear it. So leave it for the
   human. When a claim carrying one is retried for another failure, tell the researcher to
   keep the contradicting source as it is, so the reviewer can still open it.

   For each failing source, hand the failure reason back to a fresh
   `provenance:researcher` for that question — "your snippet was not on the page", "your snippet
   appears 3 times, pick a distinctive span", "wikipedia is a lead-generator, cite the
   underlying document". **Max 2 retries**, then leave it as `human_review` with the
   reason attached. Serialize verify→retry per question; questions run in parallel.

   **Not a retry: a scan that already has its `page`.** A `human_review` whose reason is
   "PDF has no text layer" means nothing mechanical can check it, not that it is wrong. If
   the source carries a `page` locator it is already where it belongs, with a person reading
   that page. Retrying it only invites a researcher to swap in a readable copy. Retry it only
   to ask for the missing `page`.

   **Not a retry: an issuing authority the project doesn't name.** A researcher whose
   `notes` say its primary text or official analysis comes from the body that issues it, though
   its claim fails corroboration because the project doesn't name that host as an issuing
   authority, has done what its instructions say. Retrying it only invites it to acknowledge a
   copy that isn't one, or to relabel the source. Hand the host to the human: whether it is the
   issuing authority is theirs to decide, and if it is, they add it to `primary_hosts` in the
   project's `provenance.toml` and `provenance build` runs again. Never
   add it yourself: an agent that names its own issuing authorities can pass off any copy as the
   record.

   **Not a retry either: a query figure whose record isn't settled.** A `human_review` on a
   query citation whose reason (from `provenance verify` or `provenance build`) says "the
   record is unsettled" is a correct citation of an unsettled record, whatever the dataset.
   What follows those words says why, and names each filing or name to open, and a person
   checks the claim against them. Retrying it only invites a researcher to change the
   parameters or cite a mirror until the filings or names drop out.

   **A retry with no failing source: an answer whose figures no snippet carries.** The claim
   is `human_review` with every source green, and its conflict line names the dollar figures
   or years the answer states that none of its snippets do. Hand that line back to a
   researcher: quote the span that carries the figure, or correct the answer. `provenance
   check-claim` does not catch this yet (#82), so a clean exit there does not rule it out.
   A claim held by a contradiction a retry dropped is also `human_review` with every source
   green, but its conflict line names a source id and a verifier's verdict, not figures: that
   one is the human's (above), not a retry.
4. `provenance archive` — saves a fresh snapshot of every cited URL, checks each one
   actually holds the cited page (a bot check or a capture missing the snippet is
   `archive_unusable`), then re-checks any paywalled snippet against its snapshot and
   upgrades it to `verified_via_archive` when the text is readable there. Runs last, never
   blocks; a missing or unusable snapshot is a warning. Snapshots are recorded in the run's
   `archives.json`, never taken from a claim file.

   **Then run `provenance judgments` again, and judge what it lists.** An archive-verified row's
   context comes from its snapshot, and a fresh snapshot is a different copy: every verdict
   on such a row goes stale when `provenance archive` replaces it, and a row it newly upgrades has no
   verdict yet. `provenance archive` says how many verdicts it made stale. Skipping this step leaves
   the claim behind every archive-verified row `pending` in the review app. A row still
   paywalled needs no verdict, and it never holds its claim at `pending`.

## Phase 3 — review surface

1. `provenance build` — detects conflicts and renders the run's `out/review.html` +
   `out/claims.json`. It first checks every claim against the question its id names in
   `questions.json`. A claim on an id the set no longer lists, or answering another question,
   is left out of the app, and build exits 1, naming it last. Retire the id (Phase 0, step 2),
   or, if the claim only misquotes its question, copy the exact text into its `question`.
   Build removes the previous render before anything can stop it. After one that renders
   nothing, a fresh `provenance serve` refuses and a running one answers a reload with a 404, until a
   build succeeds. A tab already open keeps its page, so tell its reviewer to stop and reload
   once one does.
2. `provenance serve` — opens the app on `127.0.0.1:8765`. Serve rather than opening the
   file directly: browsers disable `localStorage` on `file://` origins, and the checkbox
   state is what makes a long review session survivable.
3. Report to the operator: counts by status, the conflict list, the `not_found` list, which
   rows need the most care (adversarial + paywalled), and every access-registry entry a
   researcher reported `not written`, whole, for the operator to add to the tool's repo.

Claim files are stored as `claims/<qid>.json` in their run. If a researcher writes one under any
other name, delete the stale file — `provenance` refuses to load two files carrying the same
`question_id` rather than silently double-counting the question.

A claim whose every source the verifier rejected is `human_review`, and rejected sources do
not count toward corroboration. One source judged `contradicts` sends its claim to
`human_review` whatever the others say, and is listed with the conflicts. That is the point of the judgment pass: mechanical checks
prove the quote is on the page, and only this pass asks whether the page supports the claim.
A run that skips it produces green rows nobody has actually checked.

## Definition of done

- Every question has either a verified, corroboration-compliant, ⌘F-able citation, or an
  explicit `not_found` / `human_review` status **with a reason**.
- Every cited source that passed its mechanical checks has a usable verdict: `provenance judgments`
  ends with `0 of M cited source(s) need a verdict`. An unreviewed or stale source is not a
  verified one.
- Every verified snippet passed exact (or PDF-normalized) substring + uniqueness checks.
- Every URL has a checked archive snapshot recorded, or a logged reason it doesn't
  (`archive_failed` / `archive_unusable` / `archive_unconfirmed` carry the reason).
- The review app opens, persists checkboxes, and surfaces conflicts and paywall rows.

## Project context

**Run `provenance brief` for the run** (with `--subject <id>` for a subject's run) **and paste
its output verbatim into every researcher and verifier prompt.** It prints the project, the
run's subject, the project's `context` from its `provenance.toml`, and the hosts that issue its
records: everything project-specific a researcher needs, such as which bodies keep minutes and
which hosts serve which records. Do not summarize it. The project's `sources` names which of the
source lists that ship with the tool apply, and its `primary_hosts` the issuing authorities
those lists leave out. The brief ends with the notes that ship beside each list: how that
list's records behave, such as which filings come in series and which portals answer only
through a bulk export. The notes are yours too. Read them in the brief before Phase 0, and again
before deciding a retry: some name failures that are not one.

**Never paste the project's `completeness_check` into a researcher prompt**, and never paste
`provenance.toml` itself or tell a researcher to read it: the researcher's instructions say to
take its context from the brief alone. The check lists claims already known to exist, and a researcher told
what it is looking for confirms that item instead of searching — so anything not on the list
(a second incident, a larger figure, a more recent filing) never surfaces, and the
"corroboration" you get back is the pipeline agreeing with itself. `provenance brief` never
prints it; read it yourself, from the project file, for the check below.

Write the questions the same way: **ask what the record shows, not whether a known thing
happened.** "What documented allegations of misconduct exist?" is research. "Confirm the
Doe settlement" is transcription. Use the completeness check *after* results are in:
if the research didn't independently surface a known item, that is itself a finding — hand
the gap to the human rather than topping up a claim with the answer.

A new project is a new `provenance.toml`, not an edit to this skill.

## Style for any prose output

Verdict-first, information-dense, mobile-friendly. No padding, no congratulatory framing.
Distinguish enacted law from proposals, primary sources from advocacy framing.
