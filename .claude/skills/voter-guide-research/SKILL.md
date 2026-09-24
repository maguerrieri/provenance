---
name: voter-guide-research
description: Use when running the voter guide research pipeline for any race — fanning out research questions to subagents, verifying citations, and building the human review app. Covers the full template-to-review-app run.
---

# Voter guide research run

Three phases. The design principle throughout: **agents find and judge; Python decides
whether a citation is real.** Never let a model set a verification status.

## Phase 0 — split the template

1. Read `data/template.md`.
2. Split compound questions ("record on X and Y") into **atomic** ones — one claim per
   question — with stable IDs: `q1`, `q2a`, `q2b`. A subagent handling two claims at once
   produces citations that support one of them. IDs are **never reused or renumbered**: claims
   and verdicts are filed by id, and nothing moves them. If a question is split or reworded
   later in a run, give each new question a new id and retire the old one. Move its claim
   file out of `claims/` (to `claims-archive/`) and its verdict shard out of `judgments/` (to
   `judgments-archive/`), and point any `derives_from` that names it at the new id. A reused id hands its old verdicts to
   the new claim. `vg build` and `vg status` are the rule's gate: a claim whose id
   `questions.json` no longer lists, or whose `question` differs from the text at its id, is
   left out of review, and the command exits 1. Once the new question's research has replaced
   the old claim, a reused id no longer shows, so give the new question its new id before
   anyone researches it.
3. Write `data/questions.json` as `[{"id", "text", "claim_type", "parent", "rationale"}]`.
4. Mark `claim_type: "adversarial"` for anything negative or contested about a candidate
   (settlements, donor influence, opposition to a popular measure). Adversarial claims
   need two independent sources, so this classification changes what gets researched.
5. **Show the operator the split and get approval before fanning out.** A bad split silently
   changes the question being answered — cheapest possible place to catch it.

## Phase 1 — research fan-out, in dependency order

Some questions are **comparisons**, not retrievals: "how does their housing position compare
to the platform the guide measures against?" reasons from two other claims. Those declare `derives_from` in
questions.json, and the rule is simple — **research the inputs first, in a wave, then the
conclusions.** A comparison written before its inputs exist is guesswork wearing citations.

`check_inputs()` enforces it mechanically: a claim whose declared inputs are not themselves
verified is `human_review`, however well its own sources check out. So spawning the comparison
early does not save time; it just produces a row that cannot go green.

For the platform comparison, cite the reference document's section by its anchor URL.
Where it has no section on a topic, say so: "the platform takes no position here" is a real
finding, and stretching a nearby section to cover it is not.



Spawn one `researcher` subagent per atomic question, **in parallel** (they're
independent). Give each:

- the question text and its `claim_type`, verbatim from the run's own `questions.json`: for a
  candidate run that is `data/<candidate>/questions.json`, retargeted to the candidate, not
  the template. The gate checks each claim's `question` against it,
- the race context block from `races/<race>.md`, verbatim,
- its `question_id` and the instruction to write `data/claims/<qid>.json`.

Do not summarize the source rules for them — the agent definition carries them in full.

## Phase 2 — verification

1. `uv run vg verify --race <race>` — deterministic checks on every source: URL resolves, snippet is
   literally on the page, snippet appears exactly once, source class is allowed, paywall
   detection. No model in this loop.
2. Spawn one `verifier` subagent per claim that passed the mechanical checks, for the
   judgment half: does the cached context actually support *this* claim, and (for
   adversarial claims) are the two sources genuinely independent? Feed it the claim text
   and each source's `verification.context`. Record each verdict with:

   ```
   uv run vg judge <qid> <sid> supports|topic_only|contradicts|superseded --note "..."
   ```

   `vg judge` refuses, writing nothing, unless that claim (exact id, case included) cites
   that sid and the cache still holds the copy of the page `vg verify` built its context from
   (the snapshot, for a `verified_via_archive` source) — so run it after `vg verify`, and
   re-verify if another run re-fetched the page or `vg archive` replaced the snapshot since.
   A refusal names a wrong id, sid or `--data`, or the copy that moved; it is never a cue to
   file the verdict under some other claim.

   Judgments are stored in `data/judgments/<qid>.json`, keyed by source id — **not** in the
   claim file, which `vg verify` reloads with stripping on. A verdict written into the claim is
   destroyed by the next verify run. `vg judgments` shows what has been recorded and ends
   with one line, `N of M cited source(s) need a verdict (K stale)`. That line is the gate:
   the pass is finished when `N` is 0. **Read the number; don't count rows off the table.**
   The table wraps, and `grep -c unreviewed` has under-reported twice. A stale verdict
   predates the page it judged (or, for a query citation, the query definition), and
   `vg build` won't apply it, so it counts in `N` until the source is judged again. A source a verifier has nothing to judge on is not in `N`. That
   means its citation failed, is paywalled, was never verified, or changed since the last
   `vg verify`. Those are counted on a separate line, and each one's table row names its
   status (`unreviewed (<status>)`, or `unreviewed (run vg verify)`). They go to the retry
   loop (step 3), `vg archive` or `vg verify`, not to a verifier. The count runs the same
   offline checks `vg build` does, so the two counts together are what the review app will
   show without a verdict. The command exits 0 only when the pass is done: it exits 1 while
   `N` is above 0, and also when a claim file couldn't be read.
   For periodic filings (Form 700, campaign finance forms, annual reports), the verifier
   also checks the filer's index for a **newer** filing and returns `superseded` if one
   exists — a stale form verifies perfectly, so nothing mechanical can catch it.

   **Never** route a claim to the same agent that authored it.
3. **Retry loop.** Retry on the *verifier agent's* verdicts too, not only mechanical
   failures — `topic_only`, `contradicts` and `superseded` all mean the citation is wrong
   even though every mechanical check passed. A retry that only ever hears "snippet not
   found" fixes the quote and keeps the wrong document: that is how a superseded Form 700
   survives a retry. Say which kind of failure it was.

   For each failing source, hand the failure reason back to a fresh
   `researcher` for that question — "your snippet was not on the page", "your snippet
   appears 3 times, pick a distinctive span", "ballotpedia is a lead-generator, cite the
   underlying document". **Max 2 retries**, then leave it as `human_review` with the
   reason attached. Serialize verify→retry per question; questions run in parallel.

   **Not a retry: a scan that already has its `page`.** A `human_review` whose reason is
   "PDF has no text layer" means nothing mechanical can check it, not that it is wrong. If
   the source carries a `page` locator it is already where it belongs, with a person reading
   that page. Retrying it only invites a researcher to swap in a readable copy. Retry it only
   to ask for the missing `page`.
4. `uv run vg archive` — saves a fresh snapshot of every cited URL, checks each one
   actually holds the cited page (a bot check or a capture missing the snippet is
   `archive_unusable`), then re-checks any paywalled snippet against its snapshot and
   upgrades it to `verified_via_archive` when the text is readable there. Runs last, never
   blocks; a missing or unusable snapshot is a warning. Snapshots are recorded in the run's
   `archives.json`, never taken from a claim file.

   **Then run `vg judgments` again, and judge what it lists.** An archive-verified row's
   context comes from its snapshot, and a fresh snapshot is a different copy: every verdict
   on such a row goes stale when `vg archive` replaces it, and a row it newly upgrades has no
   verdict yet. `vg archive` says how many verdicts it made stale. Skipping this step leaves
   the claim behind every archive-verified row `pending` in the review app. A row still
   paywalled needs no verdict: its claim is flagged for the human, not left waiting.

## Phase 3 — review surface

1. `uv run vg build --race <race>` — detects conflicts and renders `data/out/review.html` +
   `data/out/claims.json`. It first checks every claim against the question its id names in
   `questions.json`. A claim on an id the set no longer lists, or answering another question,
   is left out of the app, and build exits 1, naming it last. Retire the id (Phase 0, step 2),
   or, if the claim only misquotes its question, copy the exact text into its `question`.
   Build removes the previous render before anything can stop it. After one that renders
   nothing, a fresh `vg serve` refuses and a running one answers a reload with a 404, until a
   build succeeds. A tab already open keeps its page, so tell its reviewer to stop and reload
   once one does.
2. `uv run vg serve` — opens the app on `127.0.0.1:8765`. Serve rather than opening the
   file directly: browsers disable `localStorage` on `file://` origins, and the checkbox
   state is what makes a long review session survivable.
3. Report to the operator: counts by status, the conflict list, the `not_found` list, and which
   rows need the most care (adversarial + paywalled).

Claim files are stored as `data/claims/<qid>.json`. If a researcher writes one under any
other name, delete the stale file — `vg` refuses to load two files carrying the same
`question_id` rather than silently double-counting the question.

A claim whose every source the verifier rejected is `human_review`, and rejected sources do
not count toward corroboration. That is the point of the judgment pass: mechanical checks
prove the quote is on the page, and only this pass asks whether the page supports the claim.
A run that skips it produces green rows nobody has actually checked.

## Definition of done

- Every question has either a verified, corroboration-compliant, ⌘F-able citation, or an
  explicit `not_found` / `human_review` status **with a reason**.
- Every cited source that passed its mechanical checks has a usable verdict: `vg judgments`
  ends with `0 of M cited source(s) need a verdict`. An unreviewed or stale source is not a
  verified one.
- Every verified snippet passed exact (or PDF-normalized) substring + uniqueness checks.
- Every URL has a checked archive snapshot recorded, or a logged reason it doesn't
  (`archive_failed` / `archive_unusable` / `archive_unconfirmed` carry the reason).
- The review app opens, persists checkboxes, and surfaces conflicts and paywall rows.

## Race context

**Read `races/<race>.md`** — that file holds everything race-specific: the candidates, the
known adversarial claims, the powers of the office, the primary-source hosts, and which
source lists apply (`sources: [us, ca]` → `sources/us-sources.yaml` +
`sources/ca-sources.yaml`).

Paste its **"Race context"** and **"Primary sources"** sections verbatim into every
researcher prompt. Do not summarize them — the specifics (exact vote shares, which LegInfo
host covers which years) are what keep researchers from guessing.

**Never paste the "Completeness check" section into a researcher prompt.** It lists claims
already known to exist, and a researcher told what it is looking for confirms that item
instead of searching — so anything not on the list (a second incident, a bigger donor, a
more recent filing) never surfaces, and the "corroboration" you get back is the pipeline
agreeing with itself. `races.load()` enforces the split: `race.context` is prompt-safe,
`race.completeness_check` is for you and the human only.

Write the questions the same way: **ask what the record shows, not whether a known thing
happened.** "What documented allegations of misconduct exist?" is research. "Confirm the
Doe settlement" is transcription. Use the completeness check *after* results are in:
if the research didn't independently surface a known item, that is itself a finding — hand
the gap to the human rather than topping up a claim with the answer.

`uv run vg races` lists what's defined. A new race is a new file in `races/`, not an edit
to this skill.

## Style for any prose output

Verdict-first, information-dense, mobile-friendly. No padding, no congratulatory framing.
Distinguish enacted law from proposals, primary sources from advocacy framing.
