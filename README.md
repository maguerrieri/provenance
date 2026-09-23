# provenance

A research pipeline where every claim is traceable to a human-written source, and a human can check each one in about fifteen seconds.

Agents find sources and make judgment calls. Deterministic code decides whether a citation is real:

- **The quote is literally on the page, exactly once.** Normalized matching exists for PDFs and copy-paste drift, but it is always reported as such and never passes silently as a match.
- **Structured data is checked by re-running the query.** A figure from a bulk dataset cites a named, parameterized query and the value it returned; verification runs the query again.
- **A fresh verifier judges support.** A second agent, which never wrote the claim, decides whether each source supports the specific claim or only touches the topic. An unjudged source is never shown as verified.
- **Everything ends at a local review page.** It shows each quote highlighted in its surrounding text, with links to the live page and an archived copy, so the human does the final check.

## Status

Not usable yet. The tool is being imported from the private project where it was built. See the epics in this repo's issues for the plan.
