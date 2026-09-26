<!--
Notes for the `ca` source list. `provenance brief` appends them, after the project's context,
to what every researcher and verifier on a project whose `sources` names `ca` is told, and the
orchestrating skill reads them there. This comment is left out.

It stays one `ca` file for now. Splitting the jurisdiction (the legislature, the state's
outlets and portals) from the domain (CAL-ACCESS, Form 700, candidate statements) is for the
packs, #22 and #23.
-->

# California: elections, the legislature and campaign finance

## Sources

- **Bylined journalism:** besides the national outlets, the `ca` list names CalMatters, the LA
  Times, the Sacramento Bee, KQED, LAist and Capitol Weekly.
- **Primary documents and official records** here include certified election results and
  filings, legislative roll calls, campaign finance and conflict-of-interest forms, ballot
  designation worksheets, and legislative-analyst and controller reports.
- **Lead-generators only:** Ballotpedia, like Wikipedia. Use it to find the underlying
  document, then cite *that*.

## What comes in series here

Each of these is a series, and an older one verifies exactly as well as the current one:
same host, same institutional author, snippet genuinely present. Cite the newest, and a
verifier returns `superseded` for an older one when a newer one exists.

- **Form 700s** (statements of economic interests).
- **Campaign finance forms:** Form 460s, and the Form 497 and Form 496 late reports.
- **Candidate statements:** one for the primary and another for the general.

## Portals that answer only through a bulk export or the pipeline's own query

The FPPC Form 700 portal returns a welcome screen to a fetcher, and CAL-ACCESS returns zero
characters. Neither is a reason to cite a copy from another site.

### Form 700: find it with `provenance form700`, and you cannot cite it

The filer's index is a JS portal you cannot fetch, so use the pipeline's own query instead: it
calls the same endpoint the portal does.

```
provenance form700 "<first>" "<last>"
```

It prints every filing newest-first with its filed date, the year it covers, and the agency.

**You cannot cite a Form 700.** The PDF is only served behind a session-bound link, so there is
no URL the pipeline or the human can fetch. That makes a Form 700 question `not_found`: answer
it that way, and give the human a precise retrieval instruction (portal URL, name, filing year,
agency, filing type) plus what to look for. Do not fill the gap with a copy hosted somewhere
else: it verifies perfectly and is the wrong document, which is worse than an honest gap.

A form filed in 2025 may cover 2024: put the period in the `answer` ("per the 2025 Form 700,
covering calendar 2024").

### Campaign finance: CAL-ACCESS through its nightly bulk export

Both CAL-ACCESS UIs are closed to you (one drops the connection, the other is behind bot
protection), so donor and independent-expenditure questions go through the nightly bulk export
instead:

```
provenance calaccess filer "<committee name>"                       # find the filer id
provenance calaccess contributions <filer_id> --top 25              # who gave, how much
provenance calaccess independent-expenditures <last> --first <first> # who spent for/against
```

Every result prints the filing's own CAL-ACCESS page: cite *that*, with a snippet from it. If
it won't load or won't yield a snippet, "CAL-ACCESS export shows X; filing page unreachable at
time of research" is honest and useful. Before falling back to a mirror, try:

```
provenance calaccess cite <filer_id> [--filing-id <id>]
```

Pass `--session <year>` and `--year <claim year>`: a bare lookup returned a 2023 landing page
for a 2026 committee, and the command will now tell you so instead of letting you cite it.

**Read the note it prints: it bounds what the snapshot can support.** These pages carry period
*totals* ("CONTRIBUTIONS FROM THIS PERIOD $<total>"), not per-donor itemization. So a
committee-level total is citable from the official record; an individual donor's amount is
**not**. For per-donor figures the export is your finding aid and the honest options are: cite
a mirror **with `secondary_host_ack`**, cite a query (below), or state the figure as coming
from the CAL-ACCESS export and let it go to `human_review`. Never dress up a total as
itemization, and never cite a snapshot for a number that isn't on it.

### CAL-ACCESS query citations

A query citation for a donor's total looks like this:

```json
"query": {"name": "calaccess.contributor_total",
          "params": {"filer_id": "<filer id>",
                     "contributor": "<exact name as provenance calaccess contributions prints it>"},
          "expected": "<the figure exactly as provenance query prints it>"}
```

Get the exact parameter values from `provenance calaccess contributions` first.

The contribution queries count **schedule A (monetary contributions) only** unless you pass
`form_type`. The listing shows every receipt: in-kind items, refunds, interest. So a name from it
can come back a miss whose note names the schedule it is on (`form_type=C`, say). A refund or
interest is not a contribution. Cite another schedule only when the claim says so ("in-kind
contributions"), and word the claim to match the schedule you passed.

A late contribution (Form 497, or Form 496 Part 3) reaches schedule A only when the next Form
460 is filed. Until then, a total or a ranking it could change comes back a miss, and the note
names the late report's filing. Don't record the schedule-A figure from that note as the
total. Either cite the late report's filing page for the gift, or pass `form_type=A` and word
the claim as a schedule-A figure ("reported on its campaign statements through <date>").

**"Will not verify"** here means one of these, and in each case the export cannot say what a
later amendment did: the figure counts rows from a filing whose latest amendment has none; it
leaves out a schedule an earlier amendment reported and a later one has no rows on; (`ie_total`)
it leaves out rows a filing's own amendment gave this candidate because its latest cover names
someone else; or (with `form_type=A`) it leaves out late reports that a person checks your
wording against. The warning can name a filing that is not on the schedule you counted, most
often a Form 496 on a schedule-A figure: a large late gift is reported on both forms, the
figure counts it once, and either report may be the one a later amendment withdrew.

**"nothing counted"** with **"Not a finding"** means every row matching your parameters that
states an amount is on such a left-out schedule, or (`ie_total`) in a filing whose latest cover
names someone else. The export has the money; only the filing says whether it stands. If the
note also names another `form_type` as "not counted here", that schedule's figure is a
different one: cite it only when the claim is about that schedule.

One donor can be filed under more than one name: the whole name in the last-name field on one
filing, split on another, with initials for a first or middle name, or as initials alone. The
queries never add those together, because two such names can be two people. A total or a
ranking another name could change comes back a miss. Its note lists each other name on the
schedule as filed (`'last'/'first'`) with its figure and the city, ZIP and employer on its
filings, and each late report by filing. Open those filings and decide whether they are the
same giver. Don't add the figures yourself, and don't call anyone "the largest contributor"
from that note. `names=as_filed` gives the figure under the name you asked for, or ranks each
name as filed. As with `form_type=A`, `provenance query` then says **"Will not verify"** and
lists the other names: word the claim as that filing name's figure, not the donor's whole
total, and hand it on with `notes` naming the other names. The two switches are separate: each
lifts only its own check.

### For the orchestrator: CAL-ACCESS figures that are not a retry

A `human_review` on a CAL-ACCESS query citation whose reason says it counts or leaves out
"rows a later amendment may have withdrawn", says it "leaves out rows that a filing's own
amendment attributed to this candidate", says "it leaves out late-reported contributions", or
says it "is for names exactly as filed" (from `provenance verify` or `provenance build`), is a
correct citation of an unsettled record. Either the export cannot say whether a later amendment
withdrew or moved those rows, or a late gift is on file that no Form 460 has restated yet, or
other names on file could be the same giver's. Leave it for the person, as the skill says for
any unsettled query figure.

## Roll calls: the vote page identifies nobody, cite the bill page

`leginfo.legislature.ca.gov/faces/billVotesClient.xhtml` renders a bare Ayes/Noes roster. The
same list of names appears on every vote in a session, so a snippet from it is both
non-distinctive AND unable to establish *what* was voted on: a verifier will correctly reject
it as `topic_only`, however real the vote.

**Status, not history.** `billHistoryClient.xhtml` is bare dated action rows (SB 4242's is a
date, then "Chaptered by Secretary of State") with the bill number nowhere in the extracted
text. It is the wrong one of the two. Cite `billStatusClient.xhtml`: it carries the bill
number, title and action dates in extractable text. Use the votes page as the finding aid (it
tells you how the member voted) and the status page as the citation. Same pattern as the
campaign-finance database.

Roll calls are the strongest evidence there is of a record, because they are what someone *did*
rather than said. Do not skip them because the obvious page won't verify.

## Candidate statements are a series too

A candidate files a statement for the primary and another for the general, and search engines
keep returning the primary one months after the general is out. A stale statement verifies
perfectly. Cite the general-election statement once it exists, from the host that actually
serves the current document; your brief says where it is published. A position dropped or
added between the two is a finding in itself, and a claim about what a campaign emphasises
*now* cannot rest on the primary statement.
