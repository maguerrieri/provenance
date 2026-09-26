"""provenance — cited-research pipeline CLI."""

from __future__ import annotations

import json
import re
import shlex
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import archive as arch
from . import project as proj
from .fetch import fetch as fetch_url
from .fetch import kept_copy_note, no_text_layer, pages_without_text
from .models import (
    QID_PATTERN,
    Claim,
    DroppedContradiction,
    check_archive_url,
    strip_machine_fields,
)
from .report import clear_render, render, store_id
from .sources import TIER_LABEL, domain, load_rules, notes, speakers, tier
from .terminal import printable as _printable
from .verify import (
    GOOD,
    NO_TEXT_LAYER,
    apply_archive,
    check_corroboration,
    check_inputs,
    check_snippet,
    junk_capture,
    missing_filing_date,
    missing_legal_version,
    page_list,
    revalidate_from_cache,
    snippet_problem,
    unacked_copy,
    unattributed,
    verify_against_archive,
    verify_source,
)

app = typer.Typer(add_completion=False, help="Cited-research pipeline")
# No emoji: escape() leaves ":ok:" alone, so claim text, notes and filer names would print with
# a shortcode turned into an emoji. Nothing the code itself prints uses one.
con = Console(emoji=False)
err = Console(emoji=False, stderr=True)   # notes beside output that is copied whole


def _print_version(value: bool) -> None:
    # The plugin's skill checks this before a run: its agents name the commands and flags of
    # one version, and pyproject's version is the plugin's (tests/test_plugin.py holds them
    # equal).
    if not value:
        return
    try:
        installed = package_version("provenance")
    except PackageNotFoundError:
        # Run from source without installing: there is no version to compare, so say so
        # rather than print a traceback the skill would read as a broken install.
        typer.echo("provenance: no installed version (run from source without installing it)",
                   err=True)
        raise typer.Exit(1) from None
    typer.echo(f"provenance {installed}")
    raise typer.Exit()


@app.callback()
def _main(version: Annotated[bool, typer.Option(
        "--version", callback=_print_version, is_eager=True,
        help="Print the installed version and exit.")] = False) -> None:
    pass


def qid_sort_key(qid: str) -> tuple:
    """Sort q1, q2, q10 in that order — not q1, q10, q2.

    Question ids are number-then-optional-letter (q2a, q2b), so compare the number
    numerically and the suffix as text. A plain string sort scatters a 31-question run in the
    review app and makes it hard to tell what is missing.
    """
    m = re.match(r"^([A-Za-z]*)(\d+)([A-Za-z0-9._-]*)$", qid.strip())
    if not m:
        return (1, 0, qid)
    prefix, num, suffix = m.groups()
    return (0, int(num), suffix, prefix)


def _load_or_exit(claims_dir: Path, **kw) -> list[Claim]:
    """Load claims, turning expected data problems into a readable message rather than a
    traceback — these are things the operator fixes in the data directory, not bugs."""
    try:
        return load_claims(claims_dir, **kw)
    except ValueError as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None


@contextmanager
def _judgments_or_exit():
    """Stop on an unreadable verdict file, naming it. Like a malformed claim, it is something
    the operator fixes in the data directory — and carrying on without it would render every
    source it judged as unreviewed, or rewrite the file without its verdicts."""
    from .judgments import UnreadableJudgments

    try:
        yield
    except UnreadableJudgments as e:
        # Escaped: the message quotes the file's own text, and rich reads "[/]" in a verdict as
        # markup — crashing the refusal into a traceback, or silently eating "[supports]".
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None

DATA = Path("data")   # the retired commands' old default run; every other command resolves one
_warned_strays: set[Path] = set()   # run dirs whose stray cache/ was already reported
_noted_defaults: set[Path] = set()  # subject dirs the root run was defaulted from, already said


def _project(data: Path | None, project: Path | None, *, for_run: bool = True,
             subject: str | None = None) -> tuple[proj.Project, Path]:
    """The project and the run a command's `--data`, `--subject` and `--project` name, or a
    refusal.

    `project.resolve()` is the one rule: the project `--project` names, else the nearest
    provenance.toml at or above the run (`--data`, else the working directory); and the run is
    the project root or a subject the project declares, named by its directory (`--data`) or
    its id (`--subject`). With neither, the run is the root, even from inside a subject's
    directory, so a command that works on the run (`for_run`) says so there: `cd` into a
    subject and run a command, and it was the root's run."""
    try:
        p, run = proj.resolve(data, project, subject=subject)
    except proj.ProjectError as e:
        _refuse(str(e))
    if for_run and data is None and subject is None:
        _note_root_run(p)
    return p, run


def _note_root_run(p: proj.Project, out: Console | None = None) -> None:
    """Say, once, that a run defaulted from inside a subject's directory is the root's. The
    subject is named by the path the project declares it at, which a symlinked subject's real
    directory is not: `--data` naming that would find no project above it."""
    here = Path.cwd().resolve()
    for s in p.subject_ids:
        d = p.subject_dir(s)
        real = d.resolve()
        if (here == real or real in here.parents) and real not in _noted_defaults:
            _noted_defaults.add(real)
            (out or con).print("[yellow]" + escape(_printable(
                f"this is the project root's run, not {s}'s, though the working directory "
                f"is inside {d}: pass --subject {s}, or --data {shlex.quote(str(d))} where a "
                f"command has no --subject, for {s}'s")) + "[/]")


def _clear_render_or_exit(out: Path) -> None:
    """Remove the last render from `out`, or stop: `provenance serve` must not show one a
    build did not produce."""
    try:
        clear_render(out)
    except OSError as e:
        con.print("[red]" + escape(_printable(f"could not clear the previous render from "
                                              f"{out}: {e}", lines=True))
                  + ". Remove it by hand: `provenance serve` must not show a render this build did not "
                  "produce.[/]")
        raise typer.Exit(1) from None


# Each subject gets its own run (`<project>/<subject>`) so their claims, retries and review
# progress never mix. The page cache is deliberately NOT per-subject: the same article,
# filing, or roll call routinely covers more than one, and re-fetching it per run would cost
# time and, worse, could hand two runs different bytes for the same URL.
def _explicit_cache(cache: Path | None) -> Path | None:
    """`--cache`, refused if it names a cache directory itself. Asked first by every command
    that takes it, so a bad one is named before anything reads the project."""
    if cache is not None and any((cache / d).is_dir() for d in ("pages", "calaccess")):
        # --cache names the directory that HOLDS cache/. Pointed at cache/ itself — the natural
        # `--cache data/cache` — a reader looked in data/cache/cache and found nothing, and
        # fetch or verify quietly created a second cache there. A cache directory holds pages/
        # or calaccess/; a root holds cache/. Through _printable(): an undecodable byte in argv
        # arrives as a lone surrogate.
        con.print("[red]" + escape(_printable(
            f"--cache {cache} is a cache directory itself. --cache names the directory that "
            f"holds cache/, so this looks like --cache {cache.resolve().parent}.")) + "[/]")
        raise typer.Exit(1)
    return cache


def _cache_root(data: Path | None, cache: Path | None, project: Path | None = None, *,
                resolved: tuple[proj.Project, Path] | None = None) -> Path:
    """Where the shared page cache and CAL-ACCESS database live: `--cache`, else the directory
    the project's provenance.toml names under `cache`. A command that has already resolved its
    project passes it as `resolved`, so the project file is read once.

    It used to be inferred. First from whether `data.parent/cache` existed, so an unrelated
    stray `./cache/` at the repo root silently redirected the whole pipeline to a root with no
    CAL-ACCESS database, and 14 query citations failed with "not found" on a file that plainly
    existed. Then from whether the parent held a question set, which still could not tell a
    self-contained root nested in another from a subject of it, or a subject scaffolded
    before its parent had a question set from a root. Now it is declared (`project.py`), and
    no directory that happens to exist changes the answer.
    """
    if cache is not None:
        return _explicit_cache(cache)
    p, run = resolved or _project(data, project, for_run=False)
    # A stray is never used, however it got there: an earlier rule ("the run's own cache wins
    # if it exists") was self-fulfilling, so one fetch with --data data/<subject> created
    # data/<subject>/cache, the stray became authoritative, and it hid the CAL-ACCESS
    # database, failing all 14 query citations at once. It is named, once per run in a process
    # (a warning repeated wherever the root is asked for scrolls away the output it annotates),
    # so someone moves what it holds.
    stray, shared = run / "cache", p.cache / "cache"
    if (stray.exists() and stray.resolve() != shared.resolve()
            and (key := run.resolve()) not in _warned_strays):
        _warned_strays.add(key)
        state = "" if shared.exists() else " (not created yet)"
        con.print("[yellow]" + escape(_printable(f"{stray} is a stray: this project's cache is "
                                                 f"{shared}"))
                  + f"{state}, as its provenance.toml says, so that is what is used. Move or "
                  f"merge anything the stray holds (pages, a CAL-ACCESS database) into it, or "
                  f"pass --cache.[/]")
    return p.cache


def _archive_records(data: Path) -> dict[str, dict]:
    """The run's archive records, or a readable exit if the file is damaged."""
    try:
        return arch.load_records(data)
    except ValueError as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None


def _warn_unrecorded_snapshots(claims_dir: Path, records: dict[str, dict]) -> None:
    """Say how many claim-file archive_urls no `provenance archive` run recorded.

    None is ever used, and the first command to write claims back drops them, so this is
    the one moment the loss is visible — a run archived before the records existed would
    otherwise lose every snapshot link without a word. Reads the raw files: loading strips
    the field.
    """
    n = 0
    for p in claims_dir.glob("*.json"):
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError):
            continue   # load_claims reports unreadable files
        for item in raw if isinstance(raw, list) else [raw]:
            for s in (item.get("sources") or []) if isinstance(item, dict) else []:
                if (isinstance(s, dict) and s.get("archive_url")
                        and s["archive_url"] != (records.get(s.get("url")) or {}).get("snapshot")):
                    n += 1
    if n:
        con.print(f"[yellow]{n} source(s) carry an archive_url no `provenance archive` run recorded; "
                  f"it is ignored. Run `provenance archive` to snapshot them.[/]")


def _verdict_cache_root(data: Path | None, cache: Path | None, project: Path | None = None, *,
                        resolved: tuple[proj.Project, Path] | None = None) -> Path:
    """`_cache_root()` for a command that only READS pages to check verdicts against them:
    judge, judgments, build, status.

    Nothing checked `--cache`: a root with no cache found no page, and every verdict passed. A
    verdict with no page now reads stale instead, which is safe but no more useful — a whole
    run reported as needing re-judging because of a typo. These commands never create a cache,
    so an explicit root without one can only be the wrong root: refuse it. (`_cache_root()`
    refuses `--cache` naming a cache directory itself, for every command.) fetch and verify
    create the cache on first use and do not come through here. Only `cache/` is required,
    not `cache/pages`: a run citing only queries needs the CAL-ACCESS database and no pages.
    """
    root = _cache_root(data, cache, project, resolved=resolved)
    if (root / "cache").is_dir():
        return root
    if cache is not None:
        con.print(f"[red]--cache {escape(_printable(str(cache)))} holds no cache/ directory, so "
                  f"every verdict checked against it would read stale.[/]")
        raise typer.Exit(1)
    con.print(f"[yellow]no cache at {escape(_printable(str(root / 'cache')))}: every verdict on a "
              f"cited page will read stale until `provenance verify` fetches the pages, or --cache names "
              f"the cache.[/]")
    return root


def _aliased_shards(data: Path, every: dict, claims: list[Claim]) -> dict[str, str]:
    """{claim id: shard stem} for shards the disk opens under a claim's id though they are named
    otherwise — Q1.json for claim q1 on a case-insensitive disk.

    Asked of the disk, not guessed from the names: on a case-sensitive one Q1.json is simply a
    shard no claim has, which `provenance build` never reads. Where the disk opens it as q1.json, it is
    q1's shard, and `provenance build` applies it to q1.
    """
    from . import judgments

    held = {c.question_id for c in claims}
    out: dict[str, str] = {}
    for stem in every:
        if stem not in held and (qid := judgments.opened_as(data, stem, held)):
            out[qid] = stem
    return out


def _shard_names(stems) -> str:
    """Shard file names, for a message. Through `_printable()`: they come from listing
    judgments/, not from the schema, so a name can hold anything a file name can."""
    return _printable(", ".join(f"{q}.json" for q in sorted(stems, key=qid_sort_key)))


def load_claims(claims_dir: Path, *, trust_machine_fields: bool = False,
                skipped: list[str] | None = None) -> list[Claim]:
    """Load claim files. Discards agent-writable verification data unless asked not to.

    Defaults to NOT trusting, so a new call site has to say out loud that it wants
    pipeline-owned fields — defaulting to trust is how this invariant erodes. Only pass
    `trust_machine_fields=True` where the data is re-checked (build) or merely displayed
    (status), never where it decides whether a citation counts as verified.

    Pass `skipped` to collect the question ids of unreadable claims, for a caller whose
    result would otherwise silently leave them out.
    """
    out: list[Claim] = []
    origin: dict[str, tuple[str, str]] = {}   # casefolded id -> (id, file it came from)
    skipped = [] if skipped is None else skipped
    for p in sorted(claims_dir.glob("*.json")):
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError, RecursionError) as e:
            # JSONDecodeError and UnicodeDecodeError are ValueErrors whose text names neither
            # file nor directory, so the operator was told "Expecting value" and nothing to open.
            raise ValueError(f"unreadable claim file {p}: {e}") from e
        for item in (raw if isinstance(raw, list) else [raw]):
            # A trusted load keeps verification (re-checked or displayed) but never the archive
            # fields: those come only from the run's records, via apply_archive().
            if isinstance(item, dict):
                item = strip_machine_fields(item, archive_only=trust_machine_fields)
            try:
                claim = Claim.model_validate(item)
            except Exception as e:  # noqa: BLE001  pydantic
                # One malformed row must not cost the run every other verified claim. Surface
                # it as work for the human instead of dying on the whole build. A row that is
                # not even an object (a bare string, a number) is skipped the same way.
                named = item.get("question_id") if isinstance(item, dict) else None
                # Through _printable() here, so every later print of `skipped` is safe too:
                # json.loads keeps a lone surrogate, and printing one raises.
                qid = _printable(str(named or p.stem)[:64]) or "unreadable"
                # Escaped: the id and pydantic's echo of the input are agent-authored text, and
                # so is a dict key in the field path it names. Line by line: its message has
                # line breaks of its own.
                con.print(f"[yellow]skipping {escape(f'{qid} in {_printable(p.name)}')}:[/] "
                          f"{escape(_printable(str(e), lines=True))}")
                skipped.append(qid)
                continue
            key = claim.question_id.casefold()
            if key in origin:
                first, where = origin[key]
                if first == claim.question_id:
                    # save_claims() writes <qid>.json, so a claim first written under another
                    # filename leaves a duplicate behind. Silently loading both double-counts
                    # the question in every status total and renders it twice for review.
                    raise ValueError(
                        f"duplicate question_id {first!r} in {where} and {p.name} — delete "
                        "the stale file (claims are stored as <qid>.json)")
                # An id is a filename too, here and in judgments/, so two ids are one id
                # wherever the disk folds case. On macOS's default disk the verdicts for Q1 and
                # q1 are one file: judging either writes it, and build applies it to both
                # claims. Refused on every disk, not only where it folds case: a run travels
                # through git, and a pair a case-sensitive checkout holds breaks on the first
                # Mac that clones it. The fix is by hand, and a new id must be one no question
                # has had: verdicts left under a reused id apply to the claim that takes it,
                # wherever it cites the same source.
                raise ValueError(
                    f"question_ids {first!r} in {where} and {claim.question_id!r} in {p.name} "
                    "differ only in case, and a case-insensitive disk stores both claims (and "
                    "their verdicts) as one file. Keep one id and fix the other by hand. If "
                    "its claim is a stale copy, take it out of claims/ (just its entry, where "
                    "one file holds both). If it is a question of its own, give it a new id "
                    "that no question has had: change its question_id, its question in "
                    "questions.json and any derives_from naming it, and store it as "
                    "<new id>.json, since claims are stored as <qid>.json. On a case-sensitive "
                    "disk its verdicts stay filed under the old id, where nothing reads them: "
                    "`provenance judgments` names that shard, to move out of judgments/ or rename with "
                    "its claim. Where the disk folds case, both ids' verdicts are already in "
                    "one shard: split it by hand.")
            origin[key] = (claim.question_id, p.name)
            out.append(claim)
    if skipped:
        con.print(f"[yellow]{len(skipped)} claim(s) skipped as unreadable: "
                  f"{escape(', '.join(skipped))} — fix or re-run those questions[/]")
    return sorted(out, key=lambda c: qid_sort_key(c.question_id))


def save_claims(claims: list[Claim], claims_dir: Path) -> None:
    """Write claim files back. `question_id` is schema-constrained to a safe filename
    shape (`models.QID_PATTERN`); this re-checks the resolved path anyway, because the
    cost of being wrong is overwriting a file outside the data directory."""
    base = claims_dir.resolve()
    for c in claims:
        path = (base / f"{c.question_id}.json").resolve()
        if path.parent != base:
            raise ValueError(f"refusing to write claim {c.question_id!r} outside {base}")
        path.write_text(c.model_dump_json(indent=2))


@app.command()
def fetch(url: str, data: Path = None, cache: Path = None, refresh: bool = False,
          project: Path = None):
    """Fetch and cache a page; print what the verifier will see."""
    data = _cache_root(data, cache, project)
    p = fetch_url(url, data, refresh=refresh)
    # Nothing from the page is printed as markup: Rich reads "[[page 1]]" as a tag and prints
    # "[]", and drops "[sic]" — so the page breaks a researcher must cite by were invisible,
    # and a snippet copied from here would not be on the page.
    con.print(f"[bold]{p.status}[/] {escape(_printable(p.final_url))}")
    con.print(f"title: {escape(_printable(str(p.title)))}  pdf: {p.is_pdf}  "
              f"paywall: {p.paywall_suspected}  chars: {len(p.text)}")
    if p.error:
        con.print(f"[red]error:[/] {escape(_printable(p.error, lines=True))}")
    # A kept page carries its OLD status and text, so without this line a failed
    # `provenance fetch --refresh` prints exactly like a successful one.
    if kept := kept_copy_note(p):
        con.print(f"[red]{escape(_printable(kept))}[/]")
    # Its markers print below like text, so a scan otherwise reads as an ordinary page.
    if no_text_layer(p):
        con.print(f"[yellow]{escape(NO_TEXT_LAYER)}[/]")
    elif blank := pages_without_text(p):
        # A partly scanned PDF: the text below is the typed pages only.
        con.print(f"[yellow]no text layer on {escape(page_list(blank))} "
                  "(scanned or blank) — a quote there needs `page` set to it[/]")
    # As Text, like a query value: a plain string wraps at 80 columns in an agent's shell,
    # putting line breaks in a snippet. Line by line, as a researcher copies from it.
    if p.text:
        _print_copied(p.text, 1200)
    else:
        con.print("[dim](no text)[/]")


@app.command()
def check(url: str, snippet: str, data: Path = None, cache: Path = None, refresh: bool = False,
          page: int = None, project: Path = None):
    """Ad-hoc: is this snippet on this page, exactly once? (Researchers self-check with this.)

    Runs the verifier's own snippet rules and page check, so it cannot pass a snippet or a page
    that `provenance verify` fails, nor fail what it only flags: a paywall or a scan is flagged, a dead
    URL or a soft 404 is a failure. --page is the source's `page` locator: on a partly scanned
    PDF it is what tells a quote on a scanned page from one that isn't there. Source class
    needs the whole citation, so only `provenance check-claim` checks that. Exits non-zero on a
    failure."""
    if problem := snippet_problem(snippet):
        con.print(f"[red]{problem[0]}: {escape(_printable(problem[1]))}[/]")
        raise typer.Exit(1)
    data = _cache_root(data, cache, project)
    p = fetch_url(url, data, refresh=refresh)
    # Before the verdict: on a page kept under an older extraction, a miss may be the
    # extraction, and a researcher told only "not found" drops or swaps a real citation.
    if kept := kept_copy_note(p):
        con.print(f"[yellow]{escape(_printable(kept))}[/]")
    found = check_snippet(snippet, p, pdf_page=page)
    if found.match_count is not None:
        con.print(f"matches: [bold]{found.match_count}[/] ({found.mode or 'none'})")
    if found.status == "verified":
        con.print("[green]OK — literal and unique.[/]")
    elif found.status in ("pdf_normalized_match", "normalized_match"):
        con.print("[yellow]Only matches after normalization; Cmd-F may need a shorter fragment.[/]")
    elif found.status in ("could_not_verify_paywall", "human_review"):
        # Flagged, not failed — a paywall, or a scan with no text layer. "Do not cite this"
        # about either sends the researcher off to substitute a copy they can read.
        con.print(f"[yellow]{found.status}: {escape(_printable(found.reason))}[/]")
    else:
        con.print(f"[red]{found.status}: {escape(_printable(found.reason))}[/]")
        raise typer.Exit(1)


@app.command(hidden=True)
def races():
    """Retired: a project's context is part of its provenance.toml."""
    # Hidden, and still answering, so an old habit learns why instead of meeting "No such
    # command". There was a list because races lived in the tool; that context is project data.
    con.print("[red]`provenance races` is retired:[/] a project's title, context and "
              "completeness check are keys of its provenance.toml, not a file kept in the tool. "
              "`provenance brief` prints what a researcher is told.")
    raise typer.Exit(1)


@app.command()
def brief(data: Path = None, project: Path = None, subject: str = None):
    """Print what every researcher and verifier on the run is told: its subject, the project's
    context, and the notes that ship with the project's source lists.

    Paste it into each researcher's and verifier's prompt verbatim. It never holds the project's
    completeness check: those are the answers already known, and a researcher told what it is
    looking for confirms that instead of searching, so nothing off the list ever surfaces."""
    p, run = _project(data, project, subject=subject, for_run=False)
    # Standard output is the brief and nothing else, since it is pasted whole: the notes about
    # it (which run it is, what was escaped) go to standard error.
    if data is None and subject is None:
        _note_root_run(p, out=err)
    _print_copied(researcher_brief(p, p.subject_of(run)), notes=err)


def researcher_brief(p: proj.Project, subject: str | None) -> str:
    """The text `provenance brief` prints: the one place a researcher's context is put together,
    and a verifier's. Built from the fields a researcher may read, by name, so nothing added to
    the project file later reaches a prompt without someone adding it here. After the project's
    own context come the notes of each source list it names (`sources.notes()`): the tool's
    text, never the project's, headed as such."""
    lines = [f"Project: {p.title}"]
    if subject is not None and (s := p.subject(subject)) is not None:
        lines.append(f"Subject: {s.name}")
    if p.context.strip():
        lines += ["", p.context.strip()]
    for name, text in notes(p.sources):
        lines += ["", f"Notes that ship with the `{name}` source list (the tool's, not this "
                      "project's):", "", text]
    return "\n".join(lines)


@app.command()
def verify(data: Path = None, cache: Path = None, refresh: bool = False, qid: str = "",
           project: Path = None, subject: str = None):
    """Run deterministic verification over all claims."""
    _explicit_cache(cache)
    p, data = _project(data, project, subject=subject)
    cache_root = _cache_root(data, cache, resolved=(p, data))
    rules = load_rules(p.sources)
    claims_dir = data / "claims"
    claims = [c for c in _load_or_exit(claims_dir)   # never trust: this run decides status
              if not qid or c.question_id == qid]
    if not claims:
        con.print("[yellow]No claims found in[/] " + escape(_printable(str(claims_dir))))
        raise typer.Exit(1)
    from . import judgments

    # Read every claim's verdicts before fetching anything: applied claim by claim below, a
    # malformed shard for a later claim would stop the run only after the earlier claims'
    # pages had been fetched.
    with _judgments_or_exit():
        recorded = {c.question_id: judgments.load(data, c.question_id) for c in claims}

    # Stripping on load dropped every archive_url; put the pipeline's own snapshots back so
    # this write-back doesn't delete them.
    records = _archive_records(data)
    _warn_unrecorded_snapshots(claims_dir, records)
    t = Table("qid", "status", "publisher", "detail", box=None)
    stale_verdicts: list[str] = []
    for c in claims:
        judgments.merge(judgments.verdicts_for(c, data, recorded[c.question_id],
                                               cache_root=cache_root))
        for s in c.sources:
            verify_source(s, cache_root, refresh=refresh, rules=rules)
            apply_archive(s, records, cache_root)
            # verify_source just reset a paywalled row; its snapshot is already cached, so
            # re-earn verified_via_archive here instead of losing it until `provenance archive`.
            verify_against_archive(s, cache_root)
            st = s.verification.status
            if st in ("verified", "verified_via_archive"):
                color = "green"
            elif "paywall" in st or "normalized" in st:
                color = "yellow"
            elif st == "pending":
                color = "cyan"
            else:
                color = "red"
            t.add_row(c.question_id, f"[{color}]{st}[/]", escape(_printable(s.publisher)),
                      escape(_cut(s.verification.reason or "", 70)))
        # Checked after this run's fetches, not before: a --refresh or an extractor re-fetch
        # is exactly what makes a verdict stale, and a list from before it would omit those.
        stale_verdicts += [f"{c.question_id}/{s.sid}: {why}" for s, _j, why in
                           judgments.verdicts_for(c, data, recorded[c.question_id],
                                                  cache_root=cache_root) if why]
        check_corroboration(c, rules=rules)
        if c.corroboration_ok is False:
            t.add_row(c.question_id, "[red]corroboration[/]", "-",
                      escape(_cut(c.corroboration_note, 70)))
    save_claims(claims, claims_dir)
    con.print(t)
    # The re-check a changed query definition calls for: the figure was just re-run under the
    # new one, but the verdict on it was formed about the old calculation.
    _report_stale(stale_verdicts)


def _report_stale(stale: list[str]) -> None:
    if not stale:
        return
    con.print(f"[yellow]{len(stale)} verdict(s) no longer describe what they judged, and were NOT "
              f"applied. Each predates its page, or has no cached page to check against, or "
              f"judged a copy its context no longer comes from (a replaced snapshot), or was "
              f"formed under another query definition, or judged another question or answer "
              f"than its claim gives now (or names none) — re-judge those sources (after `provenance "
              f"verify`, where the page is missing):[/]")
    for x in stale[:8]:
        # Escaped: a reason can name a snapshot, whose URL embeds the agent-authored one.
        con.print(f"  {escape(_printable(x))}")


def _report_older_exports(claims: list[Claim], cache_root: Path,
                          recorded_by_qid: dict) -> None:
    """Say how many applied query verdicts were formed against an older CAL-ACCESS export
    (`judgments.older_export()` says why they stand), so the reviewer knows the data moved
    under them. Call it once statuses are settled: it counts only a verdict that was applied
    and survived revalidation on a row whose figure reproduced — a stale verdict is already
    reported as such, and a figure that moved is a failed row, not a standing verdict."""
    from . import judgments

    older = []
    for c in claims:
        for s in c.sources:
            j = recorded_by_qid.get(c.question_id, {}).get(s.sid)
            if (s.query is None or j is None or s.verification.support == "unreviewed"
                    or s.verification.status not in GOOD):
                continue
            if why := judgments.older_export(j, cache_root, s.query):
                older.append(f"{c.question_id}/{s.sid}: {why}")
    if older:
        con.print(f"[dim]{len(older)} query verdict(s) were formed against an older CAL-ACCESS "
                  f"export. Their figures still reproduce on this one, so they stand; e.g. "
                  f"{escape(_printable(older[0]))}[/]")


@app.command()
def archive(data: Path = None, cache: Path = None, delay: float = 3.0, project: Path = None,
            subject: str = None):
    """Snapshot every cited URL to web.archive.org."""
    _explicit_cache(cache)
    p, data = _project(data, project, subject=subject)
    claims_dir = data / "claims"
    # Trusted only to carry existing verification statuses through. No archive field is read
    # from a claim file even so: one may be agent-authored, and keeping it where Save Page Now
    # failed is how a snapshot of a page the agent controls came to verify its quote. They come
    # from the run's own records below.
    claims = _load_or_exit(claims_dir, trust_machine_fields=True)
    try:
        records = arch.load_records(data)
    except ValueError as e:
        # This command is what rebuilds the records, so a damaged file must not stop it.
        aside = data / f"{arch.RECORDS}.damaged-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
        (data / arch.RECORDS).rename(aside)
        con.print("[yellow]" + escape(f"{_printable(str(e), lines=True)}\n  moved it to "
                                      f"{_printable(str(aside))}") + "; starting fresh[/]")
        records = {}
    # Every cited URL, even one already carrying a snapshot, with one source citing it.
    first = {}
    for c in claims:
        for s in c.sources:
            first.setdefault(s.url, s)
    urls = list(first)
    if not urls:
        con.print("Nothing to archive.")
        return
    if arch.have_credentials():
        con.print(f"Archiving {len(urls)} URL(s) [green](authenticated)[/]…")
    else:
        con.print(f"Archiving {len(urls)} URL(s) [yellow](anonymous — Save Page Now "
                  f"rate-limits hard; a full run left 58 of ~150 URLs unarchived)[/]\n"
                  f"  For a higher quota, get keys at https://archive.org/account/s3.php and "
                  f"export {arch.ACCESS_KEY_ENV} and {arch.SECRET_KEY_ENV}.")
    cache_root = _cache_root(data, cache, resolved=(p, data))

    def unusable(u: str, snapshot: str) -> bool:
        """Whether an earlier snapshot of ours is already known not to be the cited page — a
        bot check, a capture of somewhere else, nothing readable. Never because a snippet is
        missing from it: that can be the citation's fault, and a good capture would be thrown
        away for it. Offline: `provenance archive` fetched it when it was recorded."""
        try:
            snapshot = check_archive_url(snapshot)
        except ValueError:
            return True
        return junk_capture(first[u], snapshot, cache_root)   # asks only of the URL

    def progress(u: str, snap: str | None, err: str | None) -> None:
        prev = records.get(u) or {}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if snap and not err:
            records[u] = {"snapshot": snap, "error": None, "saved_at": now}
        elif prev.get("snapshot") and not (snap and unusable(u, prev["snapshot"])):
            # The save failed: keep the pipeline's OWN earlier snapshot, never one from a claim
            # file — and not the closest capture save() fell back to either, which nothing has
            # checked and which can be another URL's (a www. variant) where ours was good. It
            # is re-checked below like a fresh one, so a stale capture still has to hold the page.
            # Unless ours is known junk and there is a fallback to try: on a URL where Save
            # Page Now always fails (cal-access), keeping it would mean a bot-check capture
            # could never be replaced by a good one. The fallback is checked below too.
            records[u] = {"snapshot": prev["snapshot"], "error": err or "save failed",
                          "saved_at": prev.get("saved_at")}
        else:
            # `err` alongside a snapshot means an older capture stood in for the failed save;
            # it stays on the record so that is never mistaken for a fresh one.
            records[u] = {"snapshot": snap, "error": err or "save failed",
                          "saved_at": now if snap else None}
        # After every URL: a run takes seconds a URL, and an interrupted one should keep
        # what it saved.
        arch.save_records(data, records)
        con.print(f"  {'[green]saved[/]' if snap else '[red]fail[/]'} "
                  + escape(_printable(f"{u} {err or ''}")))

    arch.archive_all(urls, delay=delay, progress=progress)

    upgraded = 0
    tally: dict[str, int] = {}
    for c in claims:
        for s in c.sources:
            # Fetches each new snapshot, which caches it: later commands repeat this check
            # offline against the same copy.
            apply_archive(s, records, cache_root, fetch_missing=True)
            key = s.archive_status or "not archived"
            tally[key] = tally.get(key, 0) + 1
            if s.archive_status in ("archive_unusable", "archive_unconfirmed"):
                color = "red" if s.archive_status == "archive_unusable" else "yellow"
                con.print(f"  [{color}]{s.archive_status}[/] " + escape(_printable(
                    f"{c.question_id} {s.publisher}: {s.archive_note or ''}")))
            before = s.verification.status
            verify_against_archive(s, cache_root)
            if s.verification.status == before:
                continue
            if s.verification.status == "verified_via_archive":
                upgraded += 1
                con.print(f"  [green]snippet confirmed in snapshot[/] "
                          + escape(_printable(f"{c.question_id} {s.publisher}")))
            else:
                # It was verified_via_archive against a snapshot this run replaced.
                con.print(f"  [yellow]snapshot no longer confirms the snippet[/] "
                          + escape(_printable(f"{c.question_id} {s.publisher}")))
    save_claims(claims, claims_dir)
    con.print("sources: " + ", ".join(f"{n} {k}" for k, n in sorted(tally.items())))
    if upgraded:
        con.print(f"{upgraded} paywalled source(s) verified against their snapshot.")
    _report_rearchived_verdicts(claims, data, cache_root)


def _report_rearchived_verdicts(claims: list[Claim], data: Path, cache_root: Path) -> None:
    """Say how many verdicts on archive rows this run left stale. A fresh snapshot is a new
    copy of the page, so every verdict on a row whose context comes from a replaced one goes
    stale — and `provenance archive` runs after the judgment pass, so without this nothing says
    the pass has to run again for those rows. Called with the archives this run applied."""
    from . import judgments

    try:
        # The page half only (`is_stale()`), on archive rows only: a verdict about another answer
        # is stale however the snapshot went, and this message says the snapshot is why.
        stale = []
        for c in claims:
            rows = [s for s in c.sources if s.verification.status == "verified_via_archive"]
            judged = judgments.load(data, c.question_id) if rows else {}
            stale += [f"{c.question_id}/{s.sid}" for s in rows
                      if (j := judged.get(s.sid)) is not None
                      and judgments.is_stale(j, cache_root, s)]
    except judgments.UnreadableJudgments as e:
        # The archive itself is done and written; an unreadable shard is every reader's to stop on.
        con.print(f"[yellow]could not check verdicts against the new snapshots: "
                  f"{escape(_printable(str(e), lines=True))}[/]")
        return
    if stale:
        con.print(f"[yellow]{len(stale)} verdict(s) on archive-verified sources were about a "
                  f"snapshot this run replaced, so they are stale: run `provenance judgments` and judge "
                  f"them again before `provenance build` — e.g. {escape(', '.join(stale[:4]))}[/]")


def _apply_archives(claims: list[Claim], records: dict[str, dict], cache_root: Path) -> None:
    """Put the run's own snapshots on every source. Before any verdict is checked, too: one on
    a verified_via_archive row is about the snapshot, and without it the check has no page to
    compare, so the verdict reads stale."""
    for c in claims:
        for s in c.sources:
            apply_archive(s, records, cache_root)   # snapshots come from the run's records, not the file


def _settle(claims: list[Claim], data: Path, cache_root: Path,
            rules: dict[str, tuple[str, ...]], records: dict[str, dict]) -> tuple[list[str], dict]:
    """Settle every claim's status in the one order that works. Returns the stale verdicts, as
    `question/sid: why`, and the verdicts read, by question.

    Each step reads what the one before it produced: the run's snapshots go on first, since an
    archive row's verdict is checked against its snapshot and so is its context; then
    the recorded verdicts, each checked against the page it judged, and any `contradicts` on a
    source the claim has since dropped; revalidation checks every
    row against the cache, a verified_via_archive one against that snapshot; corroboration
    counts the sources revalidation left standing; conflicts come after the verdicts, since a
    `contradicts` verdict is one; and check_inputs() reads every input's finished status. The
    conflicts are settled before that first status read, because two of their kinds decide the
    status and the list is what explains it. Run out of order, a step reads what the claim
    file said instead — q36 once rendered verified on an input build downgraded two steps
    later. Build and status both call this so the order lives in one place.
    """
    from . import judgments
    from .conflicts import detect

    _apply_archives(claims, records, cache_root)
    with _judgments_or_exit():
        recorded = {c.question_id: judgments.load(data, c.question_id) for c in claims}
    stale = []
    for c in claims:   # apply_to(), on verdicts read once and reused for the export report
        stale += [f"{c.question_id}/{x}" for x in judgments.merge(
            judgments.verdicts_for(c, data, recorded[c.question_id], cache_root=cache_root))]
        # Before check_inputs(), which reads the status this sets. Reset from the shard every
        # time, never read from the claim file.
        c.dropped_contradictions = judgments.dropped(c, recorded[c.question_id])
    for c in claims:
        for s in c.sources:
            revalidate_from_cache(s, cache_root, rules=rules)   # a status the pipeline didn't produce won't render green
        check_corroboration(c, rules=rules)
    detect(claims)   # lists `contradicts` verdicts, so only the recorded ones
    for cycle in check_inputs(claims):   # a conclusion is not verified while an input isn't
        con.print(f"[yellow]derives_from cycle: {', '.join(sorted(cycle, key=qid_sort_key))} — "
                  f"none of these can be verified until one stops deriving from the others[/]")
    return stale, recorded


def _no_own_set(p: proj.Project, run: Path) -> str:
    """Why a subject's run has no question set to be checked against, or "".

    A subject's run never reads another run's set, each worded for its own subject. So where
    any run in the project has one and this subject has none, its claims can't be checked, and
    that fails like a set that can't be read. Checking nothing and saying so would be a prose
    rule with no gate: a subject moved from the old layout without its copy, or one whose copy
    was lost, would render claims on retired or reworded ids. Only a project with no set
    anywhere (before its questions are written) checks nothing, and says so."""
    from . import questions

    # By the name the project declares: a symlinked subject's directory has another, and a
    # command naming that one would be refused.
    subject = p.subject_of(run)
    if subject is None or questions.find(run) is not None:
        return ""
    others = [p.root] + [p.subject_dir(s) for s in p.subject_ids if s != subject]
    if not any(questions.find(d) is not None for d in others):
        return ""
    fix = (f"`provenance new-subject {subject}` copies the project's, retargeted to it"
           if questions.find(p.root) is not None else
           f"restore it: it is the set {subject}'s claims were researched against")
    return (f"{run} is {subject}'s run and has no {questions.FILE} of its own, so no claim in it "
            f"can be checked against the question its id names. No other run's set is read "
            f"instead: each is worded for its own subject. {fix[0].upper()}{fix[1:]}.")


def _question_ids(data: Path, claims: list[Claim], p: proj.Project) -> set[str] | None:
    """Check every claim against the question its id names in the run's questions.json, and
    print what breaks the stable-id rule (CLAUDE.md, "Question ids are stable and never
    reused"). Returns the ids of the claims that break it, or None when the question set can't
    be read, so that no claim could be checked.

    `provenance build` never read questions.json, so a claim on a retired id rendered beside its
    replacement, and one on a reworded or reused id rendered under the new question, while every
    command exited 0. No question set at all is said out loud, since then nothing was checked.
    """
    from . import questions

    if why := _no_own_set(p, data):
        con.print(Text(_printable(why), style="red"), soft_wrap=True)
        return None
    path = questions.find(data)
    if path is None:
        con.print("[yellow]" + escape(_printable(f"no {questions.FILE} in {data}"))
                  + ", so no claim was checked against the question its id names[/]")
        return set()
    try:
        found = questions.check(claims, questions.load(path))
    except questions.UnreadableQuestions as e:
        # Escaped: it quotes the file's own ids.
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        return None
    where = escape(_printable(str(path)))
    if found.pending:
        pairs = ", ".join(f"{q} from {old}" for q, old in found.pending)
        # Not "never applied": an older remap applied some without retiring them, and the file
        # can't say which.
        con.print("[yellow]" + escape(_printable(f"{path} still declares maps_from ({pairs})"))
                  + ", a migration for the retired `vg remap`. Nothing applies it now, and no "
                  "claim moves, so each is checked against the question at the id it sits on. "
                  "Delete the key once that is settled.[/]")
    if found.unlisted:
        # Each is named with what the advice below should be read against: the migration that
        # meant to move it (its research answered the mapped question), or the listed id it
        # differs from only in case (the fix is the id, not archiving the research).
        mapped = {old: q for q, old in found.pending}

        def named(q: str) -> str:
            if q in found.case_of:
                return f"{q} (the set has {found.case_of[q]}, which differs only in case)"
            return f"{q} (maps_from of {mapped[q]})" if q in mapped else q

        # One run from the path to the path again: "[/" in the first and "]" in the second
        # are one tag to rich, however much plain text sits between them.
        con.print(f"[red]{len(found.unlisted)} claim(s) sit on an id "
                  + escape(_printable(
                      f"{path} does not list: {', '.join(named(q) for q in found.unlisted)}. "
                      f"If the id was retired, move its claim from claims/ to "
                      f"claims-archive/ and its shard from judgments/ to judgments-archive/, "
                      f"and point any derives_from naming it at the new id. If the question "
                      f"is still asked, add it to {path}"))
                  + " under that id: a subject's own copy is not updated when the template "
                  "gains a question.[/]")
    if found.reworded:
        con.print(f"[red]{len(found.reworded)} claim(s) answer another question than {where} asks "
                  f"at their id. Ids are never reused or reworded: give the new question a new "
                  f"id, and retire this one as above. If the claim only misquotes its question, "
                  f"copy the exact text into its `question`.[/]")
        for qid, answered, asked in found.reworded:
            # As Text: both are agent-authored, and the difference may be a bracket or an emoji
            # code that markup would eat.
            con.print(Text(f"  {qid}: the claim answers {answered!r}\n"
                           f"  {' ' * len(qid)}  {questions.FILE} asks {asked!r}"),
                      soft_wrap=True)
    return found.failing


def _left_out(failing: set[str], where: str) -> None:
    """The gate's last word, after everything else a command prints, so it is not scrolled
    away: which claims were left out, and a failing exit."""
    con.print(f"[red]{len(failing)} claim(s) left out of {where} until they answer the question "
              f"their id names (above): {escape(', '.join(sorted(failing, key=qid_sort_key)))}[/]")
    raise typer.Exit(1)


@app.command()
def build(data: Path = None, cache: Path = None, title: str = "", project: Path = None,
          subject: str = None,
          candidate: Annotated[str, typer.Option(hidden=True)] = ""):
    """Detect conflicts and render the review app."""
    if candidate:
        # Retired, hidden and still answering, as `new-candidate` is: the command it printed
        # named the run and titled its page with this, and --subject does both now.
        _refuse(f"build --candidate is retired: pass --subject {candidate} for that subject's "
                f"run, whose page is titled by the subject")
    # The run's last render goes first, so that every way this build can stop short (a refusal
    # below, a crash, a kill) leaves no earlier render for `provenance serve` to show as if it
    # were this one. Only a run's, though: which directories are runs is the project file's to
    # say, and one it doesn't name, or one in no project, keeps its out/. A project file that
    # can't be read as a project is one of the refusals, and says as much as it still can: the
    # run is cleared if it is the root, or a subject the file lists (`project.lists_run()`).
    given = data
    try:
        p, data = proj.resolve(data, project, subject=subject)
    except proj.ProjectError as e:
        if isinstance(e, proj.UnreadableProject):
            run = proj.named_run(e.root, given, subject)
            if run is not None and proj.lists_run(e.root, run):
                _clear_render_or_exit(run / "out")
        _refuse(str(e))
    if given is None and subject is None:
        _note_root_run(p)
    _clear_render_or_exit(data / "out")
    _explicit_cache(cache)
    # Titled by the run's subject, so two subjects' pages never read alike. The title is only
    # what the page shows: its saved progress is keyed by the project and the subject
    # (`report.store_id()`), which a --title can't change.
    run_subject = p.subject(sid) if (sid := p.subject_of(data)) is not None else None
    if not title:
        title = f"{run_subject.name} — {p.title}" if run_subject else p.title
    cache_root = _verdict_cache_root(data, cache, resolved=(p, data))
    rules = load_rules(p.sources)

    # Trusted, then immediately re-checked: _settle() discards any status that cannot be
    # reproduced from the cached page, and replaces every support verdict with the recorded
    # one (or `unreviewed`).
    claims = _load_or_exit(data / "claims", trust_machine_fields=True)
    failing = _question_ids(data, claims, p)
    if failing is None:
        con.print("[red]review app not rendered: no claim can be checked against a question set "
                  "that can't be read, or that the run does not have.[/]")
        raise typer.Exit(1)
    # Left out before anything reads them: rendered, such a claim reads as an answer to a
    # question the run does not ask, or to one it was never researched for. The rest still
    # render, as load_claims() skips an unreadable claim: one mis-filed claim must not cost the
    # run every other one. A claim deriving from one left out reads its input as missing.
    claims = [c for c in claims if c.question_id not in failing]
    records = _archive_records(data)
    _warn_unrecorded_snapshots(data / "claims", records)
    stale_verdicts, recorded = _settle(claims, data, cache_root, rules, records)
    _report_stale(stale_verdicts)

    # A verdict recorded after the last verify has nothing to attach to yet, and the row reads
    # `unreviewed` — indistinguishable from never having been judged. Say so instead.
    jdir = data / "judgments"
    if jdir.exists():
        newest_j = max((f.stat().st_mtime for f in jdir.glob("*.json")), default=0)
        newest_c = max((f.stat().st_mtime for f in (data / "claims").glob("*.json")), default=0)
        if newest_j > newest_c:
            con.print("[yellow]judgments are newer than the claim files: run `provenance verify` first, "
                      "or recent verdicts will render as unreviewed[/]")   # verdicts live outside the claim file; merge them in
    _report_older_exports(claims, cache_root, recorded)
    html, js = render(claims, data / "out", title=title, cache_root=cache_root, rules=rules,
                      store=store_id(p.name, sid))
    con.print(f"[green]wrote[/] {escape(_printable(str(html)))}\n"
              f"[green]wrote[/] {escape(_printable(str(js)))}")
    if failing:
        _left_out(failing, "the review app")


@app.command()
def serve(data: Path = None, port: int = 8765, open_browser: bool = True,
          project: Path = None, subject: str = None):
    """Serve the review app on localhost (localStorage is unreliable on file:// origins)."""
    import functools
    import http.server
    import webbrowser

    _, data = _project(data, project, subject=subject)
    out = (data / "out").resolve()
    if not (out / "review.html").exists():
        # A build removes the last render before it can refuse, so this is also what a refused
        # build leaves: say so, or the next person runs serve, not build, and never sees why.
        con.print(f"[red]No review.html in {escape(_printable(str(out)))}: `provenance build` has not "
                  f"rendered one, is still rendering one, or its last run refused and rendered "
                  f"nothing. Run `provenance build` and fix what it reports.[/]")
        raise typer.Exit(1)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    url = f"http://127.0.0.1:{port}/review.html"
    con.print(f"Serving {escape(_printable(str(out)))} at [bold]{url}[/]  (ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


calaccess_app = typer.Typer(help="CAL-ACCESS bulk export: California campaign finance")
app.add_typer(calaccess_app, name="calaccess")


@calaccess_app.command("build")
def calaccess_build(data: Path = None, cache: Path = None, project: Path = None):
    """Load the downloaded export into SQLite (a few minutes).

    Download it first — it is ~1.5 GB, so the pipeline never fetches it implicitly:
      curl -L -o <cache>/cache/calaccess/dbwebexport.zip \
        https://campaignfinance.cdn.sos.ca.gov/dbwebexport.zip
    where <cache> is the project's `cache` in its provenance.toml, or --cache.
    """
    from . import calaccess

    root = _cache_root(data, cache, project)
    try:
        dbp = calaccess.build(root, progress=lambda t, n, note: con.print(
            f"  {t:32} {n:>9,} rows {note}"))
    except FileNotFoundError as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None
    con.print("[green]built[/] " + escape(_printable(f"{dbp} ({_export_line(root)})")))


def _export_line(root: Path) -> str:
    """Which export the database under `root` holds, in the review page's words."""
    from . import calaccess, queries

    info = calaccess.export_info(root)
    how = ", dated by download time" if info.get("export_date_from") == "download" else ""
    return queries.describe_export(info.get("export_date", "")) + how


@calaccess_app.command("filer")
def calaccess_filer(name: str, data: Path = None, cache: Path = None, limit: int = 25,
                    project: Path = None):
    """Find filer ids by name — the id every other query needs."""
    from . import calaccess

    try:
        rows = calaccess.find_filers(_cache_root(data, cache, project), name, limit)
    except FileNotFoundError as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None
    if not rows:
        con.print(f"No filer matching {escape(repr(name))}.")
        return
    t = Table("filer id", "name", "committee page", box=None)
    for r in rows:
        # Filer text from the export, as Text: rich reads "[/]" in a table cell as markup. And
        # through _printable(): the export is latin-1, so a name can carry ESC or a C1 control.
        who = " ".join(x for x in (r.get("first"), r.get("last")) if x)
        t.add_row(Text(_printable(str(r["filer_id"]))), Text(_printable(who)),
                  Text(_printable(calaccess.committee_url(r["filer_id"]))))
    con.print(t)


@calaccess_app.command("cite")
def calaccess_cite(filer_id: str, filing_id: str = "", data: Path = None, cache: Path = None,
                   session: str = "", year: str = "", project: Path = None):
    """Find a fetchable citation for a CAL-ACCESS page.

    The live pages are bot-protected, so a direct citation fails verification and researchers
    fall back to third-party mirrors. Many CAL-ACCESS pages have been snapshotted by broad
    crawls, and a snapshot is fetchable — so it can carry the official record instead.
    """
    from . import calaccess

    base = calaccess.committee_url(filer_id)
    if session:
        base = f"{base}&session={session}"
    targets = [("committee page", base)]
    if filing_id:
        targets.append(("filing", calaccess.filing_url(filing_id)))
    for label, url in targets:
        snap, note = calaccess.citable_snapshot(url, root=_cache_root(data, cache, project),
                                                expect_year=year)
        if snap:
            bad = calaccess.unusable(note)
            con.print(f"[{'red' if bad else 'green'}]{label}[/]: "
                      + escape(f"{_printable(snap)}\n  {_printable(note)}\n"
                               f"  live URL for the human: {_printable(url)}"))
        else:
            con.print(f"[yellow]{label}[/]: "
                      + escape(f"{_printable(note)}\n  live URL: {_printable(url)}"))


@calaccess_app.command("contributions")
def calaccess_contributions(filer_id: str, data: Path = None, cache: Path = None, top: int = 25,
                            since: str = "", project: Path = None):
    """Largest contributions received by a filer, then up to --top with no readable amount.

    The URL column is the point: cite the filing page, never this table. A row here is a
    local copy with nothing a human can open or Cmd-F. A gift whose amount is not a number is
    not counted by the query totals, so it is listed after the ranked ones, as filed, for
    checking by hand.
    """
    from . import calaccess

    try:
        # refuses a bad --since before it opens the database
        root = _cache_root(data, cache, project)
        rows = calaccess.contributions_to(root, filer_id, top=top, since=since)
    except (FileNotFoundError, ValueError) as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None
    t = Table("amount", "contributor", "employer", "date", "restated", "cite this URL",
              "latest amendment", box=None)
    for c in rows:
        # An amount that did not read is not "$0". A blank says so, and anything else is shown
        # as filed, cut to fit. Every cell is export text: as Text, since rich would read "[/]"
        # as markup, and through _printable(), since a control character acts on the terminal
        # before anything is shown (`_cut()` where it is cut to fit).
        amt = (f"${c.amount:,.0f}" if c.amount is not None
               else (_cut(c.amount_filed, 14) or "blank"))
        t.add_row(Text(amt), Text(_cut(c.contributor, 30)),
                  Text(_cut(c.occupation or c.employer, 18)),
                  Text(_printable(c.date)), (f"{c.filings}x" if c.filings > 1 else ""),
                  Text(_printable(c.cite_url)), _amendment_cell(c.unrestated, c.omitted))
    con.print(t)
    unread = sum(c.amount is None for c in rows)
    con.print(f"\n[yellow]{len(rows)} rows. Cite the filing page, not this table — a row here "
              f"has no URL a human can check.[/]")
    if unread:
        con.print(f"[yellow]The last {unread} have no readable amount, so no query total counts "
                  "them" + ("; there may be more: raise --top" if unread >= top else "")
                  + ".[/]")
    _amendment_footer([c.unrestated for c in rows], [c.omitted for c in rows],
                      problem=calaccess.cover_problem_at(root))


def _amendment_cell(unrestated, omitted=(), reattributed=None) -> Text:
    """A listing row's 'latest amendment' cell: each of its filings whose latest amendment has
    no such rows, or that has no cover record (calaccess.Unrestated), each schedule of it a
    later amendment left out (calaccess.UnrestatedSchedule), and, for an expenditure listed under
    the candidate its own amendment's cover named, who the latest cover names instead
    (calaccess.Reattributed), or nothing. As Text, through `_printable()`: filing ids, schedules
    and names are export text."""
    cells = ([f"{u.filing_id}: " + ("no cover" if u.cover_amend is None
                                    else f"a{u.cover_amend} has none") for u in unrestated or ()]
             + [f"{u.filing_id}: a{u.table_amend} has no schedule {u.schedule or '(blank)'}, "
                f"not counted" for u in omitted or ()])
    if reattributed:
        cells.append(reattributed.mark() if cells
                     else f"{reattributed.filing_id}: {reattributed.mark()}")
    return Text(_printable("; ".join(cells)))


def _amendment_footer(marks: list, omitted: list = (), reattributed: list | None = None,
                      problem: str | None = None) -> None:
    """Say what a marked row means, or that this database cannot mark any, and why: `problem`
    is calaccess.cover_problem_at(), whose message names the case and its fix. Said even for
    an empty listing, which would otherwise read as nothing to list. `omitted` is each row's
    left-out schedules, for a listing that has them (receipts); `reattributed` is the
    independent-expenditure listing's, one per row."""
    if problem or any(m is None for m in marks):
        also = (", or list rows an earlier amendment's cover gave this candidate"
                if reattributed is not None else "")
        why = problem or ("its covers cannot say which amendment of a filing is its latest. "
                          "Rebuild it from a complete export: provenance calaccess build")
        con.print(Text("This database cannot tell whether a filing's latest amendment dropped "
                       f"any of these rows{also}: {why}", style="yellow"), soft_wrap=True)
    else:
        # Counted by kind, so a row with one of each is in both lines: each says what to open.
        if flagged := sum(1 for m in marks if any(u.cover_amend is not None for u in m)):
            con.print(f"[yellow]{flagged} row(s) come from an amendment a later one did not "
                      f"restate (the 'latest amendment' column). That later amendment withdrew "
                      f"them, or left the schedule unchanged, and only the filing says which: "
                      f"open it before using the row. A figure counting one goes to "
                      f"human_review.[/]")
        if uncovered := sum(1 for m in marks if any(u.cover_amend is None for u in m)):
            con.print(f"[yellow]{uncovered} row(s) come from a filing with no cover record "
                      f"('no cover' in the 'latest amendment' column), so nothing here says "
                      f"whether a later amendment dropped them: open it before using the row. A "
                      f"figure counting one goes to human_review.[/]")
    if any(m is None for m in omitted):
        con.print("[yellow]This database cannot tell whether a later amendment left out a "
                  "schedule these rows are on: they carry no FORM_TYPE. Rebuild it: provenance "
                  "calaccess build[/]")
    elif left_out := sum(1 for m in omitted if m):
        con.print(f"[yellow]{left_out} row(s) are in no figure: a later amendment of their "
                  f"filing has rows on other schedules and none on theirs (the 'latest "
                  f"amendment' column). It withdrew them, or left that schedule unchanged, and "
                  f"only the filing says which: open it before using the row. A figure that "
                  f"leaves one out goes to human_review.[/]")
    if moved := sum(1 for r in reattributed or () if r):
        con.print(f"[yellow]{moved} row(s) have an earlier cover naming this candidate where the "
                  f"latest cover, which decides whose money a row is, names another candidate, "
                  f"the other stance or none (the 'latest amendment' column). A total asking for "
                  f"what the earlier cover says leaves the row out and goes to human_review. "
                  f"Only the filing says which cover is right: open it before using the row.[/]")


@calaccess_app.command("independent-expenditures")
def calaccess_ie(candidate_last: str, data: Path = None, cache: Path = None, first: str = "",
                 top: int = 50, loose: bool = False, project: Path = None):
    """Late independent expenditures naming a candidate, with support/oppose.

    The surname matches exactly. --loose does a substring search, which can return committees
    for a different candidate whose surname merely contains this one — check every hit if you
    use it.
    """
    from . import calaccess

    try:
        root = _cache_root(data, cache, project)
        rows = calaccess.independent_expenditures(root, candidate_last,
                                                  first=first, top=top, loose=loose)
    except FileNotFoundError as e:
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")
        raise typer.Exit(1) from None
    t = Table("amount", "stance", "spender", "candidate", "date", "cite this URL",
              "latest amendment", box=None)
    for r in rows:
        # A blank amount is money nobody stated: printed "$0" it read as a stated zero, which
        # ie_total refuses to report. Anything else that is not a number is shown as filed, cut
        # to fit. Every cell is filer text, as in the contributions listing: as Text, since rich
        # would read "[/]" as markup, and through _printable() (`_cut()` where it is cut).
        raw = (r.get("AMOUNT") or "").strip()
        try:
            amt = f"${float(raw):,.0f}" if raw else "blank"
        except ValueError:
            amt = _cut(raw, 14)
        t.add_row(Text(amt), Text(_printable(r["stance"])),
                  Text(_cut(r.get("FILER_NAML") or "", 28)),
                  Text(_cut(" ".join(x for x in (r.get("CAND_NAMF"), r.get("CAND_NAML")) if x),
                            22)),
                  Text(_printable(r.get("EXP_DATE") or "")), Text(_printable(r["cite_url"])),
                  _amendment_cell(r["unrestated"], reattributed=r["reattributed"]))
    con.print(t)
    _amendment_footer([r["unrestated"] for r in rows],
                      reattributed=[r["reattributed"] for r in rows],
                      problem=calaccess.cover_problem_at(root))


def _unsettled_rest(result) -> None:
    """Every filing, schedule, late report or other name past the few a reason names
    (UNSETTLED_SHOWN, LATE_SHOWN). The reason says `provenance query` lists them, so this is where they are."""
    from . import queries

    for u in (result.unrestated[queries.UNSETTLED_SHOWN:]
              + result.omitted[queries.UNSETTLED_SHOWN:]):
        con.print(Text(_printable(f"  {queries.share_text(u)}"), style="yellow"), soft_wrap=True)
    for u in result.reattributed[queries.UNSETTLED_SHOWN:]:
        con.print(Text(_printable(f"  {queries.left_out_text(u)}"), style="yellow"),
                  soft_wrap=True)
    for r in result.late[queries.LATE_SHOWN:]:
        con.print(Text(_printable(f"  {queries.late_text(r)}"), style="yellow"), soft_wrap=True)
    for line in result.names[queries.LATE_SHOWN:]:
        con.print(Text(_printable(f"  {line}"), style="yellow"), soft_wrap=True)


@app.command(name="query")
def run_query(name: str = typer.Argument(""), param: list[str] = None, data: Path = None,
              cache: Path = None, project: Path = None):
    """Run a named data query — the direct alternative to hunting for text on a page.

    With no name, lists what can be asked. This is the same command the review app prints
    next to a query-backed citation, so a reviewer checks a number by re-running it rather
    than searching a web page for a string that may not appear there at all. The printed
    command carries --cache, so it reads the database the figure was verified against.
    """
    from . import queries

    if not name:
        t = Table("query", "version", "required params", "what it answers", box=None)
        for qname, q in sorted(queries.REGISTRY.items()):
            t.add_row(qname, f"v{q.version}", ", ".join(q.required), q.description)
        con.print(t)
        return
    params = dict(p.split("=", 1) for p in (param or []))
    root = _cache_root(data, cache, project)
    try:
        result = queries.run(name, params, root)
    except TypeError as e:
        required = q.required if (q := queries.REGISTRY.get(name)) else ()
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]\n"
                  f"  required: {', '.join(required)}")
        raise typer.Exit(1) from None
    except Exception as e:  # noqa: BLE001
        con.print(f"[red]{escape(_printable(str(e), lines=True))}[/]")   # it may quote the params
        raise typer.Exit(1) from None
    if not result.found:
        # Escaped: the note lists near-matches, which are filer names from the export.
        what = "nothing counted" if result.omitted or result.reattributed else "no match"
        con.print(f"[yellow]{what}[/] — {escape(_printable(result.note))}")
        if result.unsettled:
            # every match was left out: name the filings, or the miss reads as "gave nothing"
            con.print(Text(_printable(f"Not a finding: {result.unsettled}"), style="yellow"),
                      soft_wrap=True)
            _unsettled_rest(result)
        raise typer.Exit(1)
    # Printed as Text, never as a markup string: researchers copy this value into `expected`
    # verbatim, and a str would lose "[b]" to markup and wrap at 80 columns when stdout is not
    # a terminal (an agent's shell). Through _printable(): a value or note can be export text.
    value = str(result.value)
    con.print(Text(shown := _printable(value), style="bold"),
              Text(_printable(f"({result.note})"), style="dim"), sep="  ", soft_wrap=True)
    _say_if_escaped(value, shown)
    if why := result.unsettled:
        # Before a researcher records it: this value goes to human_review however it is cited.
        # The reason names the largest few; the rest are listed here, where there is room.
        con.print(Text(_printable(f"Will not verify: {why}"), style="yellow"), soft_wrap=True)
        _unsettled_rest(result)
    # What the review page prints beside the command, so a reviewer can see they reproduced
    # the figure under the same definition and against the same export — or that they didn't.
    where = f"{_export_line(root)}, " if queries.dataset(name) == "CAL-ACCESS" else ""
    con.print(Text(_printable(f"{name} v{result.version}; {where}root {root}"), style="dim"),
              soft_wrap=True)


def _refuse(msg: str) -> NoReturn:
    """Print `msg` in red and exit 1. As one Text, never markup: a refusal quotes ids, paths
    and agent-written values, and escape() only neutralises a tag complete inside the one value
    it is given, so `[/` escaped in one piece and `]` in the next still made a closing tag, and
    the refusal raised MarkupError instead. Through `_printable()`, since an argument can carry
    a lone surrogate (an undecodable byte) that makes the print raise, or an escape sequence;
    and unwrapped, since a refusal can name a command to run next."""
    con.print(Text(_printable(msg), style="red"), soft_wrap=True)
    raise typer.Exit(1)


def _qid_or_exit(question_id: str) -> None:
    """Stop unless `question_id` has the shape every claim's has — before anything prints it.

    `judgments.path_for()` is the check, the one every read and write of a verdict file by id
    goes through. A command given a malformed id has nothing to find, and each message quoting
    it is one more place it can combine with the text around it. The id is quoted as `repr()`,
    which shows a space or a control character for what it is."""
    from . import judgments

    try:
        judgments.path_for(Path("."), question_id)   # asks only of the id, before any run is read
    except ValueError:
        near = question_id.strip()
        _refuse(f"refusing question id {question_id!r}: no claim can have it, since every "
                f"claim's question_id has the shape {QID_PATTERN} — no spaces, path separators "
                f"or leading dot"
                + (f". Did you mean {near}?" if near != question_id
                   and re.fullmatch(QID_PATTERN, near) else ""))


def _claim_or_exit(data: Path, question_id: str, needed: str) -> tuple[Claim, list[Claim]]:
    """The claim with exactly this question id, loaded as `provenance judge` reads it (trusted), and
    every readable claim; or stop, naming why. `needed` finishes the sentence for a claim that
    could not be read: what that leaves unknown. Plain text, not markup."""
    skipped: list[str] = []
    claims = _load_or_exit(data / "claims", trust_machine_fields=True, skipped=skipped)
    claim = next((c for c in claims if c.question_id == question_id), None)
    if claim is not None:
        return claim, claims
    if question_id in skipped:
        _refuse(f"claim {question_id} could not be read, {needed} — fix it first")
    # q07 for q7, Q7 for q7: the same question to a reader, a different shard to the code.
    # On a case-insensitive disk Q7.json even IS q7.json, which `provenance judgments` then misses.
    near = [c.question_id for c in claims
            if qid_sort_key(c.question_id.lower()) == qid_sort_key(question_id.lower())]
    _refuse(f"no {'readable ' if skipped else ''}claim has question id {question_id} in "
            f"{data / 'claims'}"
            + (f" — did you mean {', '.join(near)}?" if near else "")
            + (f" {len(skipped)} could not be read ({', '.join(skipped)}), and it may be one of "
               f"those: fix them first." if skipped else ""))


def _run_args(data: Path, cache: Path | None) -> str:
    """` --data <run> [--cache <root>]`, quoted for a shell: printed into every command this run's
    output tells an agent to run, since without it that command reads the default run."""
    return f" --data {shlex.quote(str(data))}" + (
        f" --cache {shlex.quote(str(cache))}" if cache is not None else "")


def _apply_archive_rows(data: Path, sources, cache_root: Path, *,
                        records: dict[str, dict] | None = None) -> None:
    """Put the run's snapshots on the sources that are `verified_via_archive`, whose context and
    verdict both come from one. Only there, and the records read only if there is one: no other
    row depends on them, and a damaged records file should not stop every verdict in the run.
    Pass `records` already read, to have a damaged file stop the command before anything else."""
    rows = [s for s in sources if s.verification.status == "verified_via_archive"]
    if rows:
        records = _archive_records(data) if records is None else records
        for s in rows:
            apply_archive(s, records, cache_root)


def _rebuild_problem(s, cache_root: Path, rules: dict[str, tuple[str, ...]]) -> str:
    """Why `provenance build` would not keep a verdict on `s` as the claim file has it now, or "".

    Build rebuilds every row from the cache, and drops a verdict whose context that changes
    (`revalidate_from_cache()`). A verdict on the claim file's context was then kept only until
    the next `provenance verify` rewrote the file, and from then on it applied to the rebuilt context,
    which no verifier had read. Asked by running that code on a copy, not by re-deriving its
    rule. Apply the run's snapshots first (`_apply_archive_rows()`). `rules` are the project's
    source lists, the ones build checks against."""
    rebuilt = s.model_copy(deep=True)
    rebuilt.verification.support = "supports"   # would build keep a verdict on this row?
    revalidate_from_cache(rebuilt, cache_root, rules=rules)
    v = rebuilt.verification
    if v.status not in GOOD:
        return (f"the cache does not confirm this citation as the claim file has it ({v.status}: "
                f"{v.reason or 'no reason given'}), so there is no context to judge")
    if v.support == "unreviewed":
        return ("its context is not what the cached page gives now, so `provenance build` would drop a "
                "verdict on it. Run `provenance verify` for this run, then judge the context it gives")
    return ""


def _unjudgeable(s, cache_root: Path, *, seen, last_run,
                 rules: dict[str, tuple[str, ...]]) -> str:
    """Why `provenance judge` would refuse a verdict on `s` now, or "": one answer for `provenance handoff`,
    `provenance judge` and `provenance judgments`, so a hand-off never offers what judge refuses or the gate
    waits on. `seen` and `last_run` are the claim file's `context_page` and `query_run`,
    captured before anything rebuilt the verification: `provenance judge` reads the file."""
    from . import judgments

    if s.query is None:
        why = judgments.unjudgeable_page(s, seen, cache_root)
    else:
        why = judgments.unjudgeable_query(s.query, last_run, cache_root)
    return why or _rebuild_problem(s, cache_root, rules)


def _query_run_line(run) -> str:
    """The run a query citation's context came from, as `provenance handoff` shows it. Facts only: the
    verifier acts on what the hand-off prints, so it carries no instruction (`describe_export()`
    tells an operator to rebuild an undated database, which is not a verifier's step)."""
    data = run.dataset
    export = (f", {data} export of {run.export_date}" if run.export_date
              else f", undated {data} database") if data else ""
    return _printable(f"{run.name} v{run.version}{export}, under {run.cache_root}")


def _handed(claim, cache_root: Path, *, rules: dict[str, tuple[str, ...]], judged=None):
    """What `provenance handoff` shows a verifier for `claim`, as one value (`judgments.Handoff`): the
    printer reads nothing else, and the context token hashes all of it but what it names as left
    out, so nothing else can be printed that the token does not cover. `provenance handoff` and `provenance
    judge` both build it here, from one read of the claim file. Apply the run's snapshots first
    (`_apply_archive_rows()`).

    `judged` is a source `provenance judge` has already found judgeable, and is not asked again: that
    would read a query's database a second time, and a rebuild landing between the two reads
    would refuse a verdict whose stamp comes from the first."""
    from . import judgments, queries

    first: dict[str, int] = {}
    sources = []
    for n, s in enumerate(claim.sources, 1):
        v = s.verification
        # `provenance judge` finds a source by its sid, so a second citation of the same url and snippet
        # is judged as the first: one verdict, keyed by that sid, and one token, printed there.
        k = first.setdefault(s.sid, n)
        why = (f"the same source id as [{k}/{len(claim.sources)}]: a verdict is recorded per "
               f"source id, and `provenance judge` takes that one for it" if k != n else
               "" if s is judged else
               _unjudgeable(s, cache_root, seen=v.context_page, last_run=v.query_run,
                            rules=rules)
               ) or ("" if v.context else "it has no context")
        context = None
        if not why:
            # A query context has a run: `unjudgeable_query()` refuses one without. The root
            # resolved, as the run check compares it: one database spelled two ways is one run.
            run = v.query_run if s.query is not None else None
            context = judgments.HandedContext(v.context, run and judgments.HandedRun(
                s.query.name, run.version, queries.dataset(s.query.name), run.export_date,
                str(Path(run.cache_root).resolve())))
        # The tier as well as the type: a type labeled reporting is an unlisted outlet on a host
        # the lists don't name, and the verifier judges an argued tier's claim form.
        sources.append(judgments.HandedSource(
            s.sid, v.status, s.publisher, s.author, s.date, s.source_type,
            TIER_LABEL[tier(s, rules)], s.url, s.page, s.snippet, context, why))
    return judgments.Handoff(claim.question_id, claim.claim_type, claim.required_sources,
                             claim.question, claim.answer, tuple(sources))


def _cut(text: str, n: int) -> str:
    """`text` through `_printable()`, cut to at most `n` characters as shown: a cell's width.
    Cut after the escaping, or a cell of control bytes shows four times as wide and folds the
    table; and between escapes, never inside one, so none reads as another character."""
    out, used = [], 0
    for ch in text:
        used += len(shown := _printable(ch))
        if used > n:
            break
        out.append(shown)
    return "".join(out)


def _say_if_escaped(text: str, shown: str, out: Console | None = None) -> None:
    """Under data a reader copies from, say so if `_printable()` showed any of it as an escape.
    An escape is text the pipeline inserted: a snippet or an `expected` copied with one in it
    (`co\\xadoperate` for a soft hyphen, which page text often holds) matches nothing."""
    if shown != text:
        (out or con).print("[yellow]Characters above that act on a terminal, and format "
                           "characters such as a soft hyphen, are shown as escapes (\\x.., "
                           "\\u....). The source holds the character, not the escape, so a copy "
                           "with one in it matches nothing: quote around it.[/]")


def _print_copied(text: str, limit: int | None = None, notes: Console | None = None) -> None:
    """Multi-line data a reader copies from (page text, a response body, a YAML entry): as Text
    and unwrapped, so no line break is the terminal's; line by line through `_printable()`; and
    `_say_if_escaped()` under it. A CRLF is joined before the cut, so the cut cannot leave a CR
    without its LF, to show as `\\x0d` and set off the note over nothing."""
    text = text.replace("\r\n", "\n")[:limit]
    con.print(Text(shown := _printable(text, lines=True)), soft_wrap=True)
    _say_if_escaped(text, shown, notes)


@app.command()
def handoff(question_id: str, data: Path = None, cache: Path = None, project: Path = None,
            subject: str = None):
    """Print what a verifier judges for one claim: the claim, and each source's context with
    the context token `provenance judge --context` must hand back.

    Claim, contexts and tokens come from one read of the claim file, the one `provenance judge` checks,
    built into one value (`_handed()`) that this prints and each token hashes, so a token names
    everything printed with it: the claim, and every source, not only its own. A source `provenance
    judge` would refuse now gets its reason instead of a token. Read-only.
    """
    _qid_or_exit(question_id)
    _explicit_cache(cache)
    p, data = _project(data, project, subject=subject)
    cache_root = _verdict_cache_root(data, cache, resolved=(p, data))
    claim, _ = _claim_or_exit(data, question_id, "so what it cites cannot be shown")
    _apply_archive_rows(data, claim.sources, cache_root)
    _print_handoff(_handed(claim, cache_root, rules=load_rules(p.sources)),
                   _run_args(data, cache))


def _print_handoff(h, run_args: str) -> None:
    """Print hand-off `h`. Reads nothing but `h` (and the run's --data and --cache, for the
    command it ends with): a field printed from anywhere else is one the token does not cover."""
    from . import judgments

    def line(text: str, style: str = "") -> None:
        # Everything here is agent- or page-authored: as Text, so no bracket reads as markup,
        # and unwrapped, so a line break is the page's and never the terminal's.
        con.print(Text(text, style=style), soft_wrap=True)

    line(f"{h.question_id} ({h.claim_type}, needs {h.required_sources} "
         f"source(s)): {_printable(h.question)}", "bold")
    line(f"claim: {_printable(h.answer)}")
    for n, s in enumerate(h.sources, 1):
        token = judgments.context_token(h, s.sid) if s.context is not None else ""
        line("")
        line(f"[{n}/{len(h.sources)}] sid {s.sid}  "
             + (f"context token {token}" if token else "nothing to judge yet"), "bold")
        byline = _printable(" · ".join((s.publisher, s.author, s.date or "undated")))
        line(f"  {byline} · {s.source_type} · tier: {s.tier}")
        # `_http_only()` refuses C0 controls and whitespace in a URL, not a C1 control or a
        # bidi override.
        line(f"  {_printable(s.url)}" + (f"  (page {s.page})" if s.page else ""))
        line(f"  status: {s.status}")
        line(f"  snippet: {_printable(s.snippet)}")
        if s.context is None:
            line(f"  {_printable(s.unjudgeable)}", "yellow")
            continue
        if s.context.query_run is not None:   # the token names the run too, so show which
            line(f"  query run: {_query_run_line(s.context.query_run)}")
        # Every line of it prefixed, so page text can't end the block early and go on to print
        # what reads as this command's own output. splitlines(), not split("\n"): a \r, \x85
        # or U+2028 is a line break to some reader, and each one starts a prefixed line here.
        line("  context:")
        for text in s.context.text.splitlines():
            line(f"  | {_printable(text)}")
    line("")
    if all(s.context is None for s in h.sources):
        line("Nothing in this claim can be judged yet.", "yellow")
        return
    line(f"Record each verdict: provenance judge {shlex.quote(h.question_id)} <sid> "
         f"supports|topic_only|contradicts|superseded --context <token> --note \"<one line>\""
         f"{run_args}")


@app.command()
def judge(question_id: str, sid: str, verdict: str, note: str = "", context: str = "",
          data: Path = None, cache: Path = None, project: Path = None, subject: str = None):
    """Record a verifier agent's verdict on one source.

    Judgments live in the run's judgments/, not in the claim file: `provenance verify` reloads claims with
    stripping on — which is what stops a researcher self-certifying — so a verdict written
    into the claim is destroyed by the next verify run. Keyed by source id, so it follows the
    citation and lapses automatically when a retry changes the quote. A contradicts does not:
    it holds its claim in review until the source is cited again or a person clears it.

    verdict: supports | topic_only | contradicts | superseded

    --context is the context token `provenance handoff` printed beside the source. Required: without
    it nothing says which hand-off the verdict is about.

    Refuses, writing nothing, unless the named claim cites the source, the copy of the page
    `provenance verify` built its context from is still the one cached, and the hand-off `provenance handoff`
    would print now is the one the token names. A verdict filed anywhere else is read by
    nothing — `q07` for `q7`, or a sid another claim cites — while the command reported success
    and the judgment pass looked done; one stamped from another copy, or on a claim, context or
    query run changed since the verifier was handed it, or beside other sources than it was
    handed, describes what the verifier never read.

    The verdict also records the claim's fingerprint (its question and answer as the claim file
    reads now), so a retry that rewrites the claim after this can be told apart from one that
    did not: `provenance build` applies the verdict only while the claim still asks and answers that.
    """
    from . import judgments

    # Before reading anything, so an unrelated claims error can't stop the command first and
    # hide the refusal.
    _qid_or_exit(question_id)
    if verdict not in judgments.VERDICTS:
        # First, with the id: a mistake in the command itself is named before any check of what
        # it refers to, so one call with two mistakes does not take two refusals to fix.
        _refuse(f"{verdict} is not a verdict: use one of {', '.join(judgments.VERDICTS)}")
    _explicit_cache(cache)
    p, data = _project(data, project, subject=subject)
    cache_root = _verdict_cache_root(data, cache, resolved=(p, data))
    rules = load_rules(p.sources)
    claim, claims = _claim_or_exit(data, question_id,
                                   f"so whether it cites {sid} cannot be checked")
    source = next((s for s in claim.sources if s.sid == sid), None)
    if source is None:
        citing = [c.question_id for c in claims if any(s.sid == sid for s in c.sources)]
        _refuse(f"claim {question_id} does not cite source {sid}"
                + (f"; {', '.join(citing)} does" if citing else
                   "; no current claim cites it — its citation may have changed since you were "
                   "given it"))
    with _judgments_or_exit():
        # Before the page check: an unreadable shard is what to fix first, since nothing can be
        # recorded into it whatever else is right.
        judgments.load(data, question_id)

    # Stamp the copy of the page the verifier read — the one `provenance verify` built the context from
    # — so a later re-fetch can invalidate this verdict instead of leaving it to describe text
    # that no longer exists. Same root, same lookup as the check in `apply_to()`, or the stamp
    # describes a page the check never looks at.
    page_url, page_at, ver, query_ver, export = "", "", 0, 0, ""
    # An archive-verified context comes from the snapshot the run's records name.
    _apply_archive_rows(data, [source], cache_root)
    if source.query is None:
        try:
            page_url, page_at, ver = judgments.judged_copy(source, cache_root)
        except judgments.Unjudgeable as e:
            _refuse(str(e))
    else:
        # A query citation has no page: its evidence is the query, so stamp its definition and
        # export instead — and only if that is what produced the context the verifier read.
        # `source` is this question's own citation (above): two questions citing one query
        # share its sid while each carries its own last run.
        if why := judgments.unjudgeable_query(source.query, source.verification.query_run,
                                              cache_root):
            _refuse(f"not recorded: {why}")
        # The run the check just matched, which the token names too, not a second read of the
        # registry and database: a rebuild landing between the two would stamp an export the
        # verifier's context never came from.
        run = source.verification.query_run
        query_ver, export = run.version, run.export_date
    # A context build would not keep, the claim file's own copy notwithstanding: a verdict on it
    # would outlive the next `provenance verify` and apply to the context that one gives.
    if why := _rebuild_problem(source, cache_root, rules):
        _refuse(f"not recorded: {why}")
    # Last, so a wrong id, sid or copy is still what a refusal names first. The copy check above
    # passes a re-verify that rebuilt the context from a newer cached copy; this is what doesn't.
    # The hand-off as `provenance handoff` would print it now, from the claim file judge just read. It
    # prints every source, so the others need their snapshots too, applied only here: a damaged
    # records file is not what a refusal about this source's own id, sid or copy names first.
    _apply_archive_rows(data, [s for s in claim.sources if s is not source], cache_root)
    if why := judgments.wrong_context(
            _handed(claim, cache_root, rules=rules, judged=source), sid, context,
            handoff=f"provenance handoff {shlex.quote(question_id)}{_run_args(data, cache)}"):
        _refuse(f"not recorded: {why}")
    try:
        # And stamp the claim it judged, so a retry that rewrites the claim later can be seen to
        # have left the verdict about words it no longer says.
        j = judgments.record(data, question_id, sid, verdict, note,
                             page_fetched_at=page_at, extractor_version=ver,
                             query_version=query_ver, export_date=export, page_url=page_url,
                             claim_fingerprint=claim.fingerprint)
    except ValueError as e:
        _refuse(str(e))   # it may quote the verdict file's own text
    # As Text, through `_printable()`: the note is agent-written, and a `[/]` or a lone
    # surrogate in it raised after the verdict was on disk — a non-zero exit that verifiers are
    # told means nothing was recorded. A line break in it could also print a second line.
    con.print(Text.assemble((j.verdict, "green" if j.verdict == "supports" else "red"),
                            _printable(f" recorded for {question_id}/{sid}"
                                       + (f": {note}" if note else ""))), soft_wrap=True)


# A retired flag: still parsed, so an old invocation reaches _retired(), but not in --help. Given
# through Annotated so the Python default stays a plain value: tests call commands as functions,
# and a `= typer.Option(...)` default is an OptionInfo there, which is truthy. (Typer does not
# resolve a `type` alias, so each parameter spells out its Annotated.)
_HIDDEN = typer.Option(hidden=True)


@app.command(name="judgments")
def show_judgments(data: Path = None, question_id: str = "",
                   repair: Annotated[bool, _HIDDEN] = False, cache: Path = None,
                   rollback: Annotated[bool, _HIDDEN] = False,
                   moved: Annotated[list[str], _HIDDEN] = None,
                   gone: Annotated[list[str], _HIDDEN] = None, project: Path = None,
                   subject: str = None):
    """Show recorded verdicts, and which cited sources still need one."""
    from . import judgments

    if repair or rollback or moved or gone:
        # Retired with `vg remap`: they re-homed verdicts after claims moved, and claims no longer
        # move. A backup an interrupted re-home left behind still stops every reader, naming the
        # checkout that undoes it, so say that first. In the run as given, with no project to
        # resolve: these flags come from before project files, as `provenance remap` does.
        with _judgments_or_exit():
            judgments.refuse_if_interrupted(DATA if data is None else data)
        given = [flag for flag, on in (("--repair", repair), ("--rollback", rollback),
                                       ("--moved", moved), ("--gone", gone)) if on]
        _retired(f"`provenance judgments {' '.join(given)}`")

    _explicit_cache(cache)
    p, data = _project(data, project, subject=subject)
    cache_root = _verdict_cache_root(data, cache, resolved=(p, data))
    rules = load_rules(p.sources)
    skipped: list[str] = []
    claims = _load_or_exit(data / "claims", trust_machine_fields=True, skipped=skipped)
    # As build does, before the verdicts: an archive-verified row is checked against its
    # snapshot, both its verdict and its context, and without the record it has neither. Only
    # read where one exists, as `provenance judge` does, so a damaged records file stops a run with
    # archive rows but not one without.
    records = (_archive_records(data) if any(s.verification.status == "verified_via_archive"
                                             for c in claims for s in c.sources) else {})
    with _judgments_or_exit():
        # Every shard, not only those named after current claims: this is the command that
        # shows the gaps, and a malformed shard left under an old id is one.
        every = judgments.load_every(data)
    for d in judgments.leftovers(data):
        con.print(f"[yellow]{escape(_printable(str(d)))} is scratch an interrupted re-home by "
                  f"the retired `vg remap` left behind. Nothing reads it, and it holds at most an "
                  f"older copy of the run's verdicts: delete it, and never restore from it.[/]")
    selected = [c for c in claims if not question_id or c.question_id == question_id]
    # Checking a snapshot reads it (and at times the live page) from the cache: archive rows only.
    _apply_archive_rows(data, [s for c in selected for s in c.sources], cache_root,
                        records=records)
    unread = [q for q in skipped if not question_id or q == question_id]
    if not selected and not unread:
        # Nothing to count would print a green "0 of 0" — the done signal — for a typo'd
        # --question-id or --data.
        what = f"with question id {question_id!r}" if question_id else "at all"
        con.print("[red]" + escape(_printable(f"no claim {what} in {data / 'claims'}")) + "[/]")
        raise typer.Exit(1)
    # A shard the disk opens under a claim's id is that claim's, as `provenance build` reads it. On a
    # case-insensitive disk q1 opens Q1.json, and counting what build applies as unjudged sent
    # verifiers round a loop: re-judging as q1 writes into Q1.json, whose name never changes.
    aliased = _aliased_shards(data, every, claims)
    by_question = {c.question_id: every.get(c.question_id, every.get(aliased.get(c.question_id),
                                                                    {}))
                   for c in selected}
    # A shard named for no current claim is read by nothing — not build, not this count. A
    # mistyped `provenance judge` id used to write one and report success.
    current = {c.question_id for c in claims} | set(skipped)
    unowned = {q: len(v) for q, v in every.items()
               if v and q not in current and q not in aliased.values()}
    t = Table("qid", "source", "verdict", "note", box=None)
    total = waiting = stale_waiting = stale = blocked = orphans = 0
    holding: list[str] = []
    for c in selected:
        recorded = by_question[c.question_id]
        # A judgment whose sid no longer matches any cited source is dead weight: it was about
        # a citation that has since changed. Harmless, but invisible without saying so. Except
        # a `contradicts`, which holds its claim in review (`judgments.dropped()`).
        live = {s.sid for s in c.sources}
        dropped = judgments.dropped(c, recorded)
        holding += [f"{c.question_id}/{d.sid}" for d in dropped]
        orphans += sum(1 for sid in recorded if sid not in live) - len(dropped)
        # Run what `provenance build` runs on each source, read-only as `provenance status` does, so the count
        # is what the review app will show rather than a re-derivation of it — each of three
        # re-derivations disagreed with build somewhere.
        rows = list(judgments.verdicts_for(c, data, recorded, cache_root=cache_root))
        stale += len(judgments.merge(rows))
        for s, j, why in rows:
            # Asked of the claim file, before build's rebuild below, since that is what
            # `provenance judge` reads: the same answer `provenance handoff` gives. Only where the answer is used
            # (no verdict applied, a status with a context), since it rebuilds a copy and re-runs
            # a query citation's query.
            filed = s.verification
            refused = (_unjudgeable(s, cache_root, seen=filed.context_page,
                                    last_run=filed.query_run, rules=rules)
                       if filed.support == "unreviewed" and filed.status in GOOD else "")
            drawn = (filed.context, filed.context_offset, filed.matched_offset)
            revalidate_from_cache(s, cache_root, rules=rules)
            # Revalidation redrew a page citation's excerpt: it drops any verdict on the one
            # `provenance verify` wrote, stale or not, so one recorded now would be dropped too.
            redrawn = s.query is None and drawn != (s.verification.context,
                                                    s.verification.context_offset,
                                                    s.verification.matched_offset)
            total += 1
            unjudgeable = s.verification.support == "unreviewed" and refused
            if s.verification.support != "unreviewed":
                v = f"[{'green' if j.verdict == 'supports' else 'red'}]{j.verdict}[/]"
                note = j.note
            elif s.verification.status not in GOOD:
                # No confirmed context to judge (failed, paywalled, never verified). The gate
                # counts only what the judgment pass can close, or it never reaches 0.
                blocked += 1
                v = f"[dim]unreviewed ({s.verification.status})[/]"
                note = s.verification.reason
            elif redrawn:
                # The context moved since `provenance verify` wrote the claim file, and revalidation
                # drops any verdict on the old one. Re-judging can't fix that; re-verifying does.
                # Asked of the context, not of the verdict: a stale one (another answer, or none
                # named) never applies, and with no verdict at all, `provenance judge` would stamp one
                # on the claim file's outdated excerpt and build would drop it the same way.
                blocked += 1
                v = "[dim]unreviewed (run provenance verify)[/]"
                note = (f"{'verdict recorded, but the' if j else 'the'} context changed since "
                        f"provenance verify")
            elif unjudgeable:
                # `provenance judge` would refuse it: the claim file's run predates this definition,
                # export or root, or the copy of the page its context came from is gone or
                # unnamed. Counted as needing a verdict, the gate could never reach 0 by
                # judging; re-verifying is what closes it.
                blocked += 1
                v = "[dim]unreviewed (run provenance verify)[/]"
                note = unjudgeable
            else:
                waiting += 1
                stale_waiting += bool(why)
                v = f"[yellow]stale (was {j.verdict})[/]" if why else "[dim]unreviewed[/]"
                # Why it is stale, not its old note: a verifier reads this table, and a stale
                # note can be about another answer, or another claim's.
                note = why or (j.note if j else "")
            t.add_row(c.question_id, escape(_printable(f"{s.publisher} {s.sid}")), v,
                      escape(_cut(note or "", 60)))
    con.print(t)

    if orphans:
        con.print(f"[dim]{orphans} recorded verdict(s) no longer match any cited source — "
                  f"their citation changed, so the judgment correctly lapsed.[/]")
    if holding:
        # Not in the count below: no verifier can close it, since `provenance judge` refuses a source
        # nothing cites. It is the human's, so every one is named: this line is the only list.
        con.print(f"[yellow]{len(holding)} contradicts verdict(s) are on a source their claim no "
                  f"longer cites ({escape(', '.join(holding))}). A contradiction does not "
                  f"lapse: each holds its claim in review until the source is cited again, or a "
                  f"person clears it at a terminal with `provenance clear-contradiction QID SID`.[/]")
    if aliased:
        # Which claim they belong to is not inferred from a source id but is what this disk
        # already does (judgments.opened_as()).
        con.print("[yellow]" + escape(_printable(
                      f"{_shard_names(aliased.values())} differ from a claim's id only in case, "
                      f"and this disk opens them under that id, so they count as that claim's — "
                      f"but a case-sensitive checkout of {data}"))
                  + " reads nothing from them. Rename each to its claim's exact id by hand, "
                  "through a temporary name.[/]")
    if unowned:
        # Question ids are stable, so a claim never moves off its shard: a shard no claim has
        # judged a claim that is gone, or was written under an id no claim ever had.
        con.print(f"[yellow]{sum(unowned.values())} verdict(s) sit in judgments/ under an id no "
                  f"claim has ({escape(_shard_names(unowned))}), so nothing reads them. Their "
                  f"claim is gone, or never had that id. Move each to judgments-archive/ to "
                  f"keep it, or put back the claim it judged. Do not delete one: that discards "
                  f"its verdicts.[/]")
    if blocked:
        con.print(f"[dim]{blocked} more source(s) have nothing a verifier can judge yet: the "
                  f"citation failed, is paywalled, was never verified, or changed since "
                  f"`provenance verify`. That is the retry loop's, `provenance archive`'s or `provenance verify`'s job.[/]")
    _report_older_exports(selected, cache_root, by_question)
    if stale:
        # Not "all need judging again": one with no page to check against has no context to
        # judge either, so it sits with the blocked sources, and `provenance judge` refuses it.
        con.print(f"[yellow]{stale} verdict(s) no longer describe what they judged, so `provenance build` "
                  f"will not apply them. Each predates its page, or has no cached page to check "
                  f"against, or judged a copy its context no longer comes from (a replaced "
                  f"snapshot), or was formed under another query definition, or judged another "
                  f"question or answer than its claim gives now (or names none). "
                  + (f"{stale_waiting} are in the count below and need judging again. "
                     if stale_waiting else "")
                  + (f"{stale - stale_waiting} have nothing a verifier can judge yet (above)."
                     if stale > stale_waiting else "") + "[/]")
    if unread:
        # No gate line at all: "0 of M" as the last line reads as done to anyone taking
        # `tail -1` through a pipe, which discards the exit status.
        con.print(f"\n[red]{len(unread)} claim(s) could not be read, so nothing here counts as "
                  f"done: {escape(', '.join(unread))}[/]")
        raise typer.Exit(1)
    # Printed as one number rather than left to be counted off a rich table: that table wraps,
    # so `grep -c unreviewed` under-reported twice and read as "done" when a source genuinely
    # had no verdict. Last line, so it is also what `tail -1` finds.
    style = "green" if waiting == 0 else "yellow"
    con.print(f"\n[{style}]{waiting} of {total} cited source(s) need a verdict "
              f"({stale_waiting} stale)[/]")
    if waiting:
        # And a gate that fails, not only one that prints: exit 0 means the judgment pass is
        # done, for anyone checking `&&` rather than reading the number.
        raise typer.Exit(1)


def _at_a_terminal() -> bool:
    """Whether a person can be asked something: stdin is a terminal. An agent's shell tool runs
    commands with stdin from a pipe or nothing, so this is the one step it cannot take."""
    return sys.stdin.isatty()


@app.command(name="clear-contradiction")
def clear_contradiction(question_id: str, sid: str, data: Path = None, project: Path = None,
                        subject: str = None):
    """Clear, on the record, a contradicts verdict on a source its claim no longer cites.

    Such a verdict holds its claim in human_review, and `provenance build` lists it with the conflicts:
    a retry that drops the source takes the disagreement off the review page without resolving
    it. Sometimes dropping it was right: the verifier was wrong, or the claim was re-scoped.
    This is for the person who has looked and decided so. It runs only at a terminal and asks
    why, and the verdict moves to judgments-archive/ with that reason beside it.
    """
    from . import judgments

    def refuse(msg: str) -> NoReturn:
        con.print(f"[red]{msg.rstrip('.')}. Nothing was cleared.[/]")
        raise typer.Exit(1)

    try:
        judgments.path_for(Path("."), question_id)   # asks only of the id
    except ValueError as e:
        refuse(escape(_printable(str(e), lines=True)))
    _, data = _project(data, project, subject=subject)
    # The id was just checked; the source id is the argument as given.
    qid, s = escape(question_id), escape(_printable(sid))

    def held_here() -> tuple[DroppedContradiction, list[str]]:
        """The dropped contradiction to clear, read from the claim files and the shard as they
        are now, and the other claims that cite its source; or refuse."""
        skipped: list[str] = []
        # Stripped: it reads only which sources each claim cites, none of which is machine-owned.
        claims = _load_or_exit(data / "claims", skipped=skipped)
        claim = next((c for c in claims if c.question_id == question_id), None)
        if claim is None:
            if question_id in skipped:
                refuse(f"claim {qid} could not be read, so whether it still cites {s} cannot be "
                       f"checked — fix it first.")
            # q07 for q7, Q7 for q7, as `provenance judge` says.
            near = [c.question_id for c in claims
                    if qid_sort_key(c.question_id.lower()) == qid_sort_key(question_id.lower())]
            refuse(f"no claim has question id {qid} in "
                   f"{escape(_printable(str(data / 'claims')))}"
                   + (f" — did you mean {escape(', '.join(near))}?" if near else
                      ", so no claim is held by that verdict. `provenance judgments` names a shard no "
                      "claim has."))
        if any(x.sid == sid for x in claim.sources):
            refuse(f"claim {qid} still cites {s}, so its verdict is the claim's own evidence "
                   f"disagreeing, and it stays live. This clears only a source the claim has "
                   f"dropped.")
        with _judgments_or_exit():
            held = next((d for d in judgments.dropped(claim, judgments.load(data, question_id))
                         if d.sid == sid), None)
        if held is None:
            refuse(f"{qid} has no contradicts verdict on {s} to clear: `provenance judgments "
                   f"--question-id {qid}` lists the ones holding it.")
        return held, sorted((c.question_id for c in claims
                             if any(x.sid == sid for x in c.sources)), key=qid_sort_key)

    held, citing = held_here()
    if not _at_a_terminal():
        # The rule that it is a person's call has to be a step that fails, not a sentence: the
        # command is named wherever the contradiction is, and agents run commands.
        refuse("clearing a contradiction is a person's decision, so this asks for the reason at "
               "a terminal, and there is none here. An agent that reached this should leave the "
               "claim in review and report it to the operator.")
    con.print(Text(_printable(f"{question_id}/{sid}: a verifier judged it contradicts the claim"
                              + (f" ({held.judged_at})" if held.judged_at else "")
                              + (f": {held.note}" if held.note else "")), style="yellow"),
              soft_wrap=True)
    if citing:
        # A verdict names its source and a hash of the claim's words, never the claim's id (#30),
        # so the shard it sits in is all that says which claim it judged. Clearing touches this
        # claim's shard alone: a claim citing the source keeps the verdict in its own shard, or
        # waits for a verifier's.
        con.print(Text(f"{', '.join(citing)} also cite{'s' if len(citing) == 1 else ''} this "
                       f"source, each judged by the verdicts in its own shard: clearing this one "
                       f"leaves those as they are.", style="yellow"), soft_wrap=True)
    reason = typer.prompt("Why does it no longer apply? (kept with the verdict in the archive)",
                          default="", show_default=False)
    if not reason.strip():
        refuse("a reason is required: it is what the record keeps in place of the verdict.")
    # Again, now: a retry can cite the source again while the prompt waits for a person.
    held_here()
    try:
        with _judgments_or_exit():
            dest = judgments.clear(data, question_id, sid, reason, shown=held)
    except (ValueError, OSError) as e:
        refuse(escape(_printable(str(e), lines=True)))
    con.print(f"[green]cleared[/] the contradicts verdict on {qid}/{s}: kept, with the reason, in "
              f"{escape(_printable(str(dest)))}")


def _access_refused(message: str, style: str = "red") -> NoReturn:
    """How a source-access command refuses: `message` through `access.redact()`, the one
    redactor for messages, then printed as `_refuse()` prints, and exit 1. Every access
    command's refusal and error leaves through here (`_access_refusals()`), and a test fails on
    an access command that exits or catches any other way. A refusal quotes what it was handed
    (a host argument, a recipe name, a library's error quoting a file), and one that echoed a
    credential would print it to a terminal an agent's log keeps."""
    from . import access

    con.print(Text(_printable(access.redact(message), lines=True), style=style), soft_wrap=True)
    raise typer.Exit(1)


@contextmanager
def _access_refusals():
    """Around the whole of an access command: whatever it raises is printed by
    `_access_refused()`, never as a traceback. A `Refused` is the registry code's own refusal,
    printed as it is; anything else is printed with its type, as a bug to report."""
    from . import access

    try:
        yield
    except typer.Exit:
        raise
    except access.Refused as e:
        _access_refused(str(e))
    except Exception as e:  # noqa: BLE001
        _access_refused(f"{type(e).__name__}: {e}")


@app.command(name="source-access")
def source_access(host: str = typer.Argument(""), run_recipe: str = "",
                  param: list[str] = None):
    """What we know about querying a site whose UI is a JS app.

    With no host, lists the registry. With a host, shows what a naive fetch gets, any
    working endpoint, and the known limits. `--run-recipe <id> --param k=v` executes one.

    Check here before concluding a source is unreachable — and before citing a copy from
    somewhere else, which is the failure this registry exists to prevent.
    """
    from . import access

    with _access_refusals():
        if not host:
            entries = access.load_all()
            if not entries:
                con.print("Registry is empty.")
                return
            t = Table("host", "access", "recipes", "what it is", box=None)
            for h, e in sorted(entries.items()):
                # Registry files are data, and `source-import-curl` writes them from a paste.
                t.add_row(Text(_printable(h)), Text(_printable(str(e.access))),
                          Text(_printable(", ".join(str(r.id) for r in e.recipes) or "-")),
                          Text(_printable(str(e.name))))
            con.print(t)
            return

        entry = access.find(host)
        if entry is None:
            _access_refused(f"Nothing recorded for {host}.\n"
                            f"If you find a way in, record it: `provenance source-import-curl "
                            f"<file>` after copying the request from dev tools. If it needs a "
                            f"login, record that too — a known dead end saves the next run from "
                            f"substituting silently.", style="yellow")

        # YAML reads an unquoted `verified: 2026-08-21` as a date, hence str() before escape().
        # The prose fields are often block scalars, so they keep their line breaks.
        def prose(value) -> str:
            return _printable(str(value).strip(), lines=True)

        con.print(f"[bold]{escape(_printable(entry.host))}[/] — "
                  f"{escape(_printable(str(entry.name)))}  "
                  f"([bold]{escape(_printable(str(entry.access)))}[/], "
                  f"verified {escape(_printable(str(entry.verified or 'unknown')))})")
        if entry.naive_fetch:
            con.print(f"\n[dim]a plain fetch gets:[/] {escape(prose(entry.naive_fetch))}")
        for r in entry.recipes:
            con.print(f"\n[bold]{escape(_printable(str(r.id)))}[/] "
                      + escape(_printable(str(r.summary)) + "\n  "
                               + _printable(f"{r.method} {r.url}")
                               + (f"\n  params: {_printable(', '.join(map(str, r.params)))}"
                                  if r.params else "")
                               + (f"\n  {prose(r.notes)}" if r.notes else "")))
        if entry.limits:
            con.print(f"\n[yellow]limits:[/] {escape(prose(entry.limits))}")
        if entry.manual_steps:
            con.print(f"\n[yellow]manual retrieval:[/] {escape(prose(entry.manual_steps))}")

        if run_recipe:
            recipe = entry.recipe(run_recipe)
            if recipe is None:
                _access_refused(f"no recipe {run_recipe!r}")
            params = dict(p.split("=", 1) for p in (param or []))
            resp = access.run(recipe, params)
            con.print(f"\n[bold]HTTP {resp.status_code}[/] {len(resp.text)} chars")
            _print_copied(resp.text, 1500)   # fetched, and copied from


def _registry_write_refused(dest: Path, text: str) -> NoReturn:
    """Print the entry instead of writing it, in an installed copy: the registry ships inside
    the package there, and the next install would delete the file (access.installed_copy()).
    Called where the write would be, with the text `access.dump_entry()` made, so the entry has
    passed every check a write does, and `dest` is `access.entry_path()`'s, a checked host. For a
    host that already has an entry, what is printed is this install's copy with the change
    applied, and the repo's may be newer, so it says to merge rather than replace."""
    where = f"src/provenance/source_access/{_printable(dest.name)}"
    how = (f"The tool already has an entry for this host: merge what is new into {where} by "
           "hand rather than replacing the file, since the repo's copy may be newer than this "
           "install's." if dest.exists() else f"Add it as {where}.")
    con.print(Text("not written: this provenance is an installed copy, and the access registry "
                   "ships inside it, so the next install would delete the entry. Record it in the "
                   "tool's repo instead, from a clone of https://github.com/maguerrieri/provenance. "
                   + how, style="yellow"), soft_wrap=True)
    _print_copied(text)
    raise typer.Exit(1)


@app.command(name="source-note")
def source_note(host: str, note: str, access: str = "", verified: str = ""):
    """Record an access finding that didn't come from a browser request.

    The Form 700 download flow was found by reading the portal's own script bundle, not by
    copying a cURL — so there was nowhere to put it and it nearly stayed in one session's
    head. Appends to the host's entry, creating a stub if there is none. The note and the
    host are checked as every registry write is (`access.check_entry()`): a login in either,
    or in a URL the note quotes, is refused and nothing is written.
    """
    from . import access as access_mod

    with _access_refusals():
        h, entry = access_mod.with_note(host, note, access=access, verified=verified)
        if access_mod.installed_copy():
            _registry_write_refused(access_mod.entry_path(h), access_mod.dump_entry(entry, h))
        path = access_mod.save(h, entry)
        con.print(f"[green]recorded[/] {escape(_printable(str(path)))}")


@app.command(name="source-import-curl")
def source_import_curl(path: Path, name: str = "", write: bool = True):
    """Turn a browser 'copy as cURL' into a registry entry, minus credentials.

    Cookies, auth and session headers in the paste are your live session. They are dropped
    here and never written to disk, and so is any header not known to be safe (each is
    named, to add back by hand if it is not a credential). `--user`, a login in the URL, a
    URL parameter or body field named like a credential (`api_key`, `csrf_token`), a body
    that is neither JSON nor form-encoded, or a curl option the importer doesn't know refuses
    the import. If the endpoint only works with a credential, it is a manual retrieval, not a
    pipeline capability: record it as `access: manual`. The entry is checked as every
    registry write is (`access.check_entry()`), printed or written, `--name` included.
    """
    from . import access

    with _access_refusals():
        parsed = access.parse_curl(path.read_text())
        entry = parsed["entry"]
        entry["name"] = name or entry["name"]
        dropped = parsed["dropped_credentials"]
        if dropped:
            con.print(f"[yellow]dropped credentials:[/] {escape(_printable(', '.join(dropped)))}"
                      f" — confirm the endpoint still works without them before relying on it")
        unknown = parsed["dropped_headers"]
        if unknown:
            con.print(f"[yellow]dropped headers not known to be safe:[/] "
                      f"{escape(_printable(', '.join(unknown)))}"
                      f" — if the endpoint needs one and it is not a credential, add it by hand")

        # The entry is the pasted request, and YAML to be copied into a file: allow_unicode
        # leaves a bidi override or NEL in it as it was pasted. Printed or written, it is
        # checked once, `--name` included (`dump_entry()`, or `save()` through it).
        if not write:
            _print_copied(access.dump_entry(entry))
            return
        dest = access.entry_path(entry["host"])
        if access.installed_copy():
            _registry_write_refused(dest, access.dump_entry(entry))
        if dest.exists():
            text = access.dump_entry(entry)
            con.print(f"[yellow]{escape(_printable(str(dest)))} exists — printing instead of "
                      f"overwriting[/]\n")
            _print_copied(text)
            return
        dest = access.save(entry["host"], entry)
        con.print(f"[green]wrote[/] {escape(_printable(str(dest)))}\n"
                  f"Fill in `summary`, `params`, and `notes`, then verify it works cookieless.")


@app.command(name="form700")
def form700(first: str, last: str):
    """List a filer's Form 700 filings, newest first, from the FPPC's own index.

    Use this before citing a Form 700. The portal is a JS app, so a fetcher cannot run the
    search a human runs — which is how a 2024 filing got cited while the 2025 one sat at the
    top of the results. This asks the same endpoint the portal calls.
    """
    from . import fppc

    try:
        filings = fppc.search(first, last)
    except Exception as e:  # noqa: BLE001
        con.print(f"[red]FPPC search failed: {type(e).__name__}: "
                  f"{escape(_printable(str(e), lines=True))}[/]\n"
                  f"Do not substitute a copy silently — say the index was unreachable.")
        raise typer.Exit(1) from None
    # The names as given: an undecodable byte in argv is a lone surrogate.
    searched = escape(_printable(f"{first} {last}"))
    if not filings:
        con.print(f"No Form 700 filings found for {searched}.")
        return
    t = Table("filed", "covers", "agency", "index id", box=None)
    for f in filings:
        # The FPPC index's own text, as Text: rich reads "[/]" in a table cell as markup. It is
        # JSON, which can carry a lone surrogate as an escape.
        t.add_row(Text(_printable(f.filed_date)),
                  Text(_printable(", ".join(str(y) for y in f.filing_years))),
                  Text(_printable("; ".join(f.agencies[:2])
                                  + (" …" if len(f.agencies) > 2 else ""))),
                  Text(_printable(str(f.index_id))))
    con.print(t)
    newest = filings[0]
    con.print("\n[bold]Most recent:[/] " + escape(_printable(
        f"filed {newest.filed_date}, covering {', '.join(str(y) for y in newest.filing_years)}.")))
    con.print(f"Retrieve it at {fppc.PORTAL} (search {searched}); cite "
              f"that one, and set the source `date` to its filed date.")


@app.command(name="check-claim")
def check_claim(path: Path, data: Path = None, cache: Path = None, project: Path = None):
    """Validate one claim file before handing it on. Exits non-zero if anything fails.

    Researchers run this as their last step. The prose rules about snippet length and
    distinctiveness are necessary but demonstrably not sufficient — a one-word quote got
    written twice in testing — so this makes the check mechanical instead of a request.
    Every failure here is one the verifier would have raised anyway, minus a round trip.
    """
    from . import questions

    # The run `provenance build` will check this claim in: the directory holding the claim's
    # claims/, and the project that run is in. Not --data alone: a researcher on a subject's run
    # checks with no --data, which is the project root, whose set is the template, not the
    # subject's retargeted copy. Made absolute, so `provenance check-claim q1.json` from inside
    # claims/ has a parent name, but not resolved: a subject that is a symlink has no project
    # above its real directory (`project.find()`).
    at = proj.absolute(path)
    _explicit_cache(cache)
    p, run = _project(at.parent.parent if at.parent.name == "claims" else data, project)
    rules = load_rules(p.sources)
    cache_root = _cache_root(run, cache, resolved=(p, run))
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        con.print(f"[red]cannot read {escape(_printable(f'{path}: {e}', lines=True))}[/]")
        raise typer.Exit(1) from None

    ok = True
    asked_in, asked = questions.find(run), None
    if why := _no_own_set(p, run):
        ok = False
        con.print(Text(_printable(why), style="red"), soft_wrap=True)
    elif asked_in is None:
        con.print(f"[dim]no {questions.FILE} for {escape(_printable(str(run)))}, so the "
                  f"question was not checked[/]")
    else:
        try:
            asked = questions.load(asked_in)
        except questions.UnreadableQuestions as e:
            ok = False
            con.print(f"[red]cannot check the question:[/] "
                      f"{escape(_printable(str(e), lines=True))}")
    for item in (raw if isinstance(raw, list) else [raw]):
        try:
            claim = Claim.model_validate(strip_machine_fields(item))
        except Exception as e:  # noqa: BLE001
            con.print(f"[red]schema:[/] {escape(_printable(str(e), lines=True))}")
            raise typer.Exit(1) from None
        # `provenance build` refuses a claim whose question isn't the one its id names, so a researcher
        # who misquotes it hears here rather than stopping the whole run's review app.
        found = questions.check([claim], asked) if asked is not None else questions.Findings()
        qid = escape(claim.question_id)
        where = escape(_printable(str(asked_in)))
        if listed := found.case_of.get(claim.question_id):
            ok = False
            con.print(f"  [red]question id[/] {qid} is not in {where}, which "
                      f"has {escape(_printable(listed))}: they differ only in case\n"
                      f"      Use the question_id you were given, exactly.")
        elif found.unlisted:
            ok = False
            con.print(f"  [red]question id[/] {qid} is not in {where}\n"
                      f"      Use the question_id you were given, exactly. If you did, the "
                      f"question set changed under you: report that, and do not edit it.")
        for _qid, answered, text in found.reworded:
            ok = False
            con.print(f"  [red]question[/] is not the one {where} asks at {qid}\n"
                      f"      Copy it exactly as you were given it, into `question`:")
            con.print(Text(f"      yours: {answered!r}\n      asked: {text!r}"), soft_wrap=True)
        for src_ in claim.sources:
            verify_source(src_, cache_root, rules=rules)
            st = src_.verification.status
            cited = _printable(f"{src_.publisher}: {src_.snippet!r}")
            if st in ("verified", "verified_via_archive", "could_not_verify_paywall"):
                con.print(f"  [green]{st}[/] {escape(cited)}")
            elif st in ("normalized_match", "pdf_normalized_match"):
                con.print(f"  [yellow]{st}[/] {escape(cited)}")
            else:
                ok = False
                # One run with the reason, which can quote the snippet or the page.
                con.print(f"  [red]{st}[/] " + escape(
                    f"{cited}\n      {_printable(src_.verification.reason or '')}"))
        for src_ in claim.sources:
            if unacked_copy(src_, rules):
                ok = False
                con.print(f"  [red]secondary host[/] {escape(_printable(domain(src_.url)))} is "
                          f"not the "
                          f"authority that issues this record\n"
                          f"      Cite the issuing authority's own copy. If you genuinely "
                          f"cannot reach it, set `secondary_host_ack` saying what you could "
                          f"not reach and why this copy is the same document — but never "
                          f"substitute silently.")
            if missing_filing_date(src_):
                ok = False
                con.print(f"  [red]no filing date[/] {escape(_printable(src_.url))}\n"
                          f"      periodic filings are a series — set `date` to the filing's "
                          f"own date, and make sure it is the most recent one")
            if missing_legal_version(src_, rules):
                ok = False
                con.print(f"  [red]no effective date or version[/] "
                          f"{escape(_printable(src_.url))}\n"
                          f"      this host publishes legal text, which is a series — set "
                          f"`date` to the effective date or version of what you quoted (a "
                          f"code section's history or currency note says which; for a bill, "
                          f"its version, or the date of the action you cite), and make sure "
                          f"it is the version the claim is about")
        for src_ in unattributed(claim, rules):
            ok = False
            t = tier(src_, rules)
            con.print(f"  [red]not attributed[/] {TIER_LABEL[t]} " + escape(_printable(
                f"{src_.publisher}: {src_.snippet!r}")))
            if t == "unlisted_outlet":
                # One run: the host is data, and so is every name below.
                con.print("      " + escape(_printable(
                    f"{domain(src_.url)} is not on this project's source lists as a news "
                    f"outlet, so nothing but the label says this is reporting. If it is an "
                    f"outlet doing its own reporting, a source list can name it (a change to "
                    f"the tool, reviewed like code); until then, cite the record itself, or "
                    f"state the claim as what this outlet reports.")))
            names = speakers(src_)
            con.print("      " + escape(_printable(
                f"This supports only 'X argues Y', never Y on its own. Name who argues it in "
                f"`answer`: {' or '.join(repr(n) for n in names)}." if names else
                "This supports only 'X argues Y', never Y on its own, and its `author` and "
                "`publisher` name nobody the answer could say argues it: set them to who "
                "wrote or published it.")))
            con.print("      If the claim is a fact, cite a source that states it: a primary "
                      "text, an official analysis, or reporting.")
        check_corroboration(claim, rules=rules)
        if claim.corroboration_ok is False:
            ok = False
            con.print(f"  [red]corroboration[/] {escape(_printable(claim.corroboration_note))}")
    if not ok:
        con.print("[red]Not ready. Fix these and re-run — do not hand this on.[/]")
        raise typer.Exit(1)
    con.print("[green]All sources check out.[/]")


# Question ids are stable and never reused, so a claim never moves between ids and nothing has
# to re-file it or its verdicts (CLAUDE.md, "Question ids are stable and never reused"). The
# commands that did are retired. Each still answers, hidden, so an old script or habit learns why
# instead of meeting "No such command".
_STABLE_IDS = ("question ids are stable and never reused. A split or reworded question gets a "
               "new id, and the old id is retired, so claims never move between ids and "
               "nothing re-files them or their verdicts. See \"Question ids are stable and "
               "never reused\" in CLAUDE.md.")


def _retired(what: str) -> NoReturn:
    con.print(f"[red]{what} is retired:[/] {_STABLE_IDS}")
    raise typer.Exit(1)


@app.command(name="remap", hidden=True,
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def remap(data: Path = DATA):
    """Retired: question ids are stable, so there is no numbering to migrate."""
    from . import judgments

    # The run may be one an interrupted `vg remap --apply` left half-moved, and whoever retries
    # it needs the way back first, as `vg judgments --rollback` gives it.
    with _judgments_or_exit():
        judgments.refuse_if_interrupted(data)
    _retired("`provenance remap`")


@app.command(name="new-subject")
def new_subject(subject: str, data: Path = None, questions: Path = None,
                project: Path = None):
    """Scaffold <project>/<subject>/ for a separate run.

    Each subject is its own run: own claims, own retries, own review progress. A subject need
    not be a person: a proposal, a document, or one version of either. Only the page cache is
    shared (the project's `cache`), because the same filing or article routinely covers more
    than one subject. The subject must be one of the project's `subjects` already: this command
    writes the run, never the project file.
    """
    p, _ = _project(data, project, for_run=False)   # scaffolds a run; works on none
    s = p.subject(subject)
    if s is None:
        _refuse(f"{subject} is not one of the project's subjects: add it to `subjects` in "
                f"{p.file} as {{id = {json.dumps(subject)}, name = \"<what the questions call "
                f"it>\"}}, then run this again. Commands find a subject's run by that list, and "
                f"no command edits the project file.")
    root = p.subject_dir(s.id)
    src_q = questions or (p.root / "questions.json")
    dest_q = root / "questions.json"
    if not dest_q.exists() and not src_q.exists():
        # A subject's run is checked against its own copy and never falls back to the
        # template, so one scaffolded without it has nothing to check its claims against.
        _refuse(f"no question set to copy to {dest_q}: write {src_q} first (the template's "
                f"questions), or pass --questions")
    (root / "claims").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)

    if not dest_q.exists():
        qs = json.loads(src_q.read_text())
        for q in qs:
            # A new run has no earlier id space: a maps_from or mapped_from left from the
            # retired `vg remap` is another run's history, so it is not copied.
            q.pop("maps_from", None)
            q.pop("mapped_from", None)
            # The question set is written about a subject; retarget it rather than making
            # the researcher infer which subject it is about.
            q["text"] = _retarget(q["text"], p.subjects, s)
            q["subject"] = s.id
        dest_q.write_text(json.dumps(qs, indent=1))
        con.print("[green]wrote[/] "
                  + escape(_printable(f"{dest_q} ({len(qs)} questions retargeted to {s.name})")))
    elif dest_q.exists():
        con.print(f"{escape(_printable(str(dest_q)))} already exists — left alone")

    # Quoted for a shell, as every printed command is. They name the subject by id, and the
    # project too where the working directory is not in it: --subject finds the project from
    # there otherwise.
    at = _printable(str(root))
    here = proj.find(proj.working_dir())
    where = ("" if here is not None and here.resolve() == p.root
             else f" --project {shlex.quote(str(p.root))}")
    sid = _printable(shlex.quote(s.id) + where)
    con.print("[green]ready[/] " + escape(f"{at}\n"
                                          f"  provenance verify --subject {sid}\n"
                                          f"  provenance build  --subject {sid}"))


def _retarget(text: str, subjects, to: proj.Subject) -> str:
    """`text` with every subject's name made `to`'s. In one pass, longest name first, so a name
    inside another ("Measure A" in "Measure A (amended)") is never replaced within it, and `to`'s
    own name is left as it is: replaced one at a time, a question already naming the amended
    version read "Measure A (amended) (amended)". Whole names only, never inside a word."""
    names = sorted({s.name for s in subjects} | {to.name}, key=len, reverse=True)
    alternation = "|".join(re.escape(n) for n in names)
    return re.sub(rf"(?<!\w)(?:{alternation})(?!\w)", lambda _: to.name, text)


@app.command(name="new-candidate", hidden=True,
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def new_candidate():
    """Retired: `provenance new-subject` scaffolds a subject's run."""
    # Hidden, and still answering, so an old habit learns the new name instead of meeting "No
    # such command". A subject need not be a person.
    con.print("[red]`provenance new-candidate` is retired:[/] `provenance new-subject <id>` "
              "scaffolds a subject's run, for a subject the project's provenance.toml lists.")
    raise typer.Exit(1)


# The plugin's orchestration skill, as it is invoked in Claude Code: what `provenance new` and
# `provenance ask` hand a project to. tests/test_new_and_ask.py holds it to a skill the plugin
# ships, so renaming the skill fails there until this follows.
SKILL = "/provenance:voter-guide-research"
# Where `provenance ask` keeps its pages unless told otherwise: one cache for every scratch
# project, so asking again does not fetch again. Written into each project file as it is here,
# so it is declared there, never inferred.
ASK_CACHE = "~/.cache/provenance"

_PROJECT_FILE = '''\
# A provenance project (README, "Projects"). No command edits this file once
# `provenance new` or `provenance ask` has written it: change it by hand.
name = {name}
sources = {sources}
cache = {cache}   # the directory that holds cache/, relative to this file

# title = "<the review page's title>"   # `name` if left out
# subjects = [{{id = "<its directory>", name = "<what the questions call it>"}}]
#   A separate run for each, in its own subdirectory. A subject need not be a person: a
#   proposal or a document works the same way. Leave it out for a project with one subject.

# What every researcher is told, verbatim (`provenance brief` prints it). Keep it thin: where
# the records are, never a finding, since nothing checks it.
context = """
"""

# The answers already known. No researcher is told them: check the results against them.
completeness_check = """
"""
'''

# Words every question asking what the record shows has, left out of `provenance ask`'s default
# directory: named by its first words, every such question was "ask-what-does-the-record-show".
_ASK_FILLER = frozenset("""a about an and are as at be been by did do does for from had has have
how in is it its of on or record records say said says show showed shown shows that the their
this to was were what when where which who whom whose why with""".split())


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string. JSON's escaping is TOML's for text `_unwritable()` passes,
    which holds no control character: kept as UTF-8, since ensure_ascii would write an astral
    character as a surrogate pair, which TOML refuses."""
    return json.dumps(value, ensure_ascii=False)


def _unwritable(what: str, value: str) -> str:
    """A refusal for a value `new` or `ask` can't write into a file as the user sees it, else "":
    one holding a character `_printable()` would escape. A control character is invalid TOML,
    and an undecodable byte from argv (a lone surrogate) can't be written at all. Whether a
    value can go into a printed command is another question, `queries.unprintable()`'s."""
    if _printable(value) != value:
        return f"{what} holds a character that can't be written as it is: {_printable(value)}"
    return ""


def _sources_or_refuse(source: list[str] | None) -> list[str]:
    """The `--source` lists given, or a refusal naming the ones there are. Required, never
    defaulted: a project checked against lists it didn't choose is checked against the wrong
    ones, silently (`project.load()`)."""
    from .sources import available

    there = available()
    if not source:
        _refuse(f"--source is required: name each source list the project's citations are "
                f"checked against, one --source each (there are {', '.join(there)}; `us` holds "
                f"the rules that apply everywhere)")
    if missing := [s for s in source if s not in there]:
        _refuse(f"--source names no such source list: {', '.join(missing)} "
                f"(there are {', '.join(there)})")
    return list(dict.fromkeys(source))


def _cache_setting(cache: str | None, default: str, root: Path) -> str:
    """What the project file's `cache` says: `default` when `--cache` isn't given, else the
    directory `--cache` names. Given, it is read as every command's `--cache` is, from the
    working directory, and written relative to the project file, which is how the file reads
    it. Written as typed, `--cache shared` from the directory holding several asks would have
    named a cache inside each one. Worked out on resolved paths, since `..` from a directory
    reached through a symlink climbs out of its target. A path from ~ or from / is written as
    given."""
    import os

    if cache is None:
        return default
    if problem := _unwritable("--cache", cache):
        _refuse(problem)
    if not cache.strip():
        _refuse("--cache must name a directory")
    if cache.startswith("~") or os.path.isabs(cache):
        return cache
    try:
        return os.path.relpath(proj.absolute(Path(cache)).resolve(), root.resolve())
    except (RuntimeError, OSError) as e:
        # A symlink loop, which resolve() raises on: refused, never a traceback.
        _refuse(f"--cache {cache} can't be resolved from {root}: {e}")


def _missing_dirs(path: Path) -> list[Path]:
    """`path` and each directory above it that does not exist yet, deepest first: what a
    `mkdir(parents=True)` would create, so a scaffold that fails can take back exactly that."""
    import os

    missing = []
    for d in (path, *path.parents):
        if os.path.lexists(d):
            break
        missing.append(d)
    return missing


def _already_a_project(root: Path) -> NoReturn:
    _refuse(f"{root} holds a {proj.FILE} already: `provenance new` and `provenance ask` only "
            f"create a project, and no command edits one")


def _install(path: Path, body: str) -> None:
    """Create `path` holding `body`, whole or not at all, and never over a file already there:
    written to a temporary file beside it, fsynced, then hard-linked into place, which raises
    FileExistsError if `path` exists. A run killed while writing leaves only the temporary file
    (a dotfile nothing reads), never a partial `path`: a partial provenance.toml read as the
    project, a partial template refusing the retry as not the one given. On a filesystem with
    no hard links, `path` is created exclusively and written in place instead."""
    import errno
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                raise
            with open(path, "x", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _scaffold(root: Path, name: str, sources: list[str], cache: str, files: dict[str, str],
              *, create: bool) -> proj.Project:
    """Write `root`/provenance.toml and `files` beside it, and read the project back as every
    command reads it, or refuse and leave nothing behind: what was written removed, and every
    directory created for it that is still empty. Read back through `project.resolve()`, so it
    is refused for anything any command would refuse it for, a subject of another project's
    included.

    Nothing is written into or over anything another process made. With `create` (the caller
    found no `root`), `root` is created exclusively, so one made since the caller looked is
    refused. Each file is installed whole or not at all (`_install()`), and never over a file.
    The project file goes last, so an interrupted run leaves no project, and nothing reads what
    it left as one: `new` run again finds the same template, and `ask` refuses the directory,
    saying what may have left it."""
    import os

    created: list[Path] = []   # directories made for the project, deepest first
    written: list[Path] = []
    text = _PROJECT_FILE.format(name=_toml_string(name), sources=json.dumps(sources),
                                cache=_toml_string(cache))

    def undo() -> None:
        for f in reversed(written):
            try:
                f.unlink()
            except OSError:
                pass
        for d in created:
            try:
                d.rmdir()
            except FileNotFoundError:
                continue
            except OSError:
                break

    if create:
        missing = _missing_dirs(root)
        try:
            created = missing[1:]
            root.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(root)   # exclusive: never a directory made since the caller looked
            created = missing
        except OSError as e:
            # FileExistsError too: `root` made meanwhile, or a part of the path that is a file.
            undo()
            _refuse(f"could not create {root}: {e}")
    for rel, body in {**files, proj.FILE: text}.items():
        path = root / rel
        try:
            _install(path, body)
            written.append(path)
        except FileExistsError:
            undo()
            if rel == proj.FILE:
                _already_a_project(root)
            _refuse(f"{path} exists: `provenance new` and `provenance ask` write over no file")
        except OSError as e:
            undo()
            _refuse(f"could not write {path}: {e}. Nothing was written.")
    try:
        p, _ = proj.resolve(None, root)
    except proj.ProjectError as e:
        undo()
        _refuse(f"{e}. Nothing was written.")
    return p


def _hand_off(root: Path, prompt: str) -> str:
    """How to open Claude Code in `root` with the skill invoked: as a command, quoted for a
    shell, when a pasted copy carries the path as it is (`queries.unprintable()`, the rule for
    every command printed for a value), else in words."""
    from .queries import unprintable

    if unprintable(str(root)):
        return (f"open Claude Code in that directory (its path can't be pasted as printed) and "
                f"run {prompt}")
    return f"cd {shlex.quote(str(root))} && claude {shlex.quote(prompt)}"


@app.command(name="new")
def new(directory: Annotated[Path, typer.Argument(
            help="The project's directory. It may exist already, holding the template.")],
        from_: Annotated[Path, typer.Option(
            "--from", help="The template: the research questions, in prose.")],
        source: Annotated[list[str] | None, typer.Option(
            help="A source list the project's citations are checked against. Repeat for each.")
        ] = None,
        name: Annotated[str, typer.Option(
            help="The project's name: its review progress is kept under it. Defaults to the "
                 "directory's name.")] = "",
        cache: Annotated[str | None, typer.Option(
            help="The directory that holds the shared cache/, from the working directory as "
                 "every command's --cache. Defaults to the project's own directory.")] = None):
    """Start a project from a template, for the plugin's skill to research.

    Writes the project's provenance.toml and template.md, and prints the command that hands it
    to the skill, which splits the template into questions, asks you to approve the split and
    researches them.

    Only a new project: a directory holding a provenance.toml, or a run's files from before
    project files (a question set, claims, a cache), is refused. A project laid out the old way
    gets its provenance.toml by hand, as a reviewed change (README, "Projects")."""
    import os

    sources = _sources_or_refuse(source)
    root = proj.absolute(directory)
    name = name.strip() or root.name
    if problem := _unwritable("the project's name", name):
        _refuse(problem)
    if not name:
        _refuse(f"{root} has no name to give the project: pass --name")
    setting = _cache_setting(cache, ".", root)
    try:
        template = from_.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _refuse(f"--from {from_} can't be read as a template: {e}")
    if not template.strip():
        _refuse(f"--from {from_} is empty: the template is the research questions to split")
    create = not os.path.lexists(root)
    if os.path.islink(root):
        # Written through, the project file would land in the link's target, which can be
        # another project's subject: whether one declares it is asked of the path as given, and
        # a subject is declared by the directory above it, which the link's is not.
        _refuse(f"{root} is a symlink: name the directory it points to")
    if os.path.lexists(root) and not root.is_dir():
        _refuse(f"{root} is not a directory")
    if os.path.lexists(root / proj.FILE):
        _already_a_project(root)
    if held := sorted(n for n in proj.RESERVED - {proj.FILE} if os.path.lexists(root / n)):
        # A run's own files: a project from before project files, or a cache. Writing a project
        # file beside them would adopt them, which is a reviewed change, not a command's.
        _refuse(f"{root} holds a run's files already ({', '.join(held)}): `provenance new` only "
                f"creates a project. A project laid out the old way gets its {proj.FILE} by "
                f"hand, as a reviewed change (README, \"Moving a project laid out the old way\").")
    dest = root / "template.md"
    keep = False
    if os.path.lexists(dest):
        try:
            keep = os.path.samefile(dest, from_) or dest.read_bytes() == from_.read_bytes()
        except OSError:
            keep = False
        if not keep:
            _refuse(f"{dest} exists and is not --from {from_}: `provenance new` writes over no "
                    f"file. Pass it as --from, or move it.")

    p = _scaffold(root, name, sources, setting, {} if keep else {dest.name: template},
                  create=create)
    con.print(Text(_printable(
        f"created {p.root}\n"
        f"  {proj.FILE}  name {name!r}, checked against {', '.join(sources)}, the shared "
        f"cache in {p.cache / 'cache'}\n"
        f"  template.md      {'already there' if keep else f'copied from {from_}'}\n"
        f"Add where the records are to `context` in {proj.FILE}, and list its subjects if it "
        f"has more than one. Then hand it to the skill, which splits the template into "
        f"questions, asks you to approve the split and researches them:\n"
        f"  {_hand_off(p.root, SKILL)}", lines=True)), soft_wrap=True)


def _ask_dir(question: str) -> Path | None:
    """`ask-<the question's first distinctive words>-<a hash of it>`, in the working directory:
    the words every such question shares (`_ASK_FILLER`) are left out. Accents are folded, a
    possessive `'s` dropped, and anything else outside [a-z0-9] separates words, so the name is
    plain on any disk. The words are cut short, and two questions can share them: the hash
    keeps the directory, and the project's name with it, one question's. The same question
    asked again gets the same directory, and is refused. None for a question with no such word:
    it has no default, and `provenance ask` asks for `--dir`."""
    import hashlib
    import unicodedata

    folded = unicodedata.normalize("NFKD", question).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.findall(r"[a-z0-9]+", re.sub(r"'s\b", "", folded))
             if w not in _ASK_FILLER]
    slug = "-".join(words[:6])[:48].rstrip("-")
    digest = hashlib.sha256(question.encode()).hexdigest()[:6]
    return Path(f"ask-{slug}-{digest}") if slug else None


@app.command(name="ask")
def ask(question: Annotated[str, typer.Argument(
            help="One question, asking what the record shows.")],
        source: Annotated[list[str] | None, typer.Option(
            help="A source list the citations are checked against. Repeat for each.")] = None,
        dir_: Annotated[Path | None, typer.Option(
            "--dir", help="The new project's directory, which must not exist. Defaults to "
                          "ask-<the question's first distinctive words>-<a hash of it>, "
                          "here.")] = None,
        name: Annotated[str, typer.Option(
            help="The project's name: its review progress is kept under it. Defaults to the "
                 "directory's name.")] = "",
        cache: Annotated[str | None, typer.Option(
            help=f"The directory that holds the shared cache/, from the working directory as "
                 f"every command's --cache. Defaults to {ASK_CACHE}, which every ask shares.")
        ] = None,
        adversarial: Annotated[bool, typer.Option(
            help="The question is negative or contested: its claim needs two independent "
                 "sources.")] = False):
    """Research one question, without a template, in a new project of its own.

    The project holds just the question, as q1. The command it prints hands it to the plugin's
    skill, which gives it one researcher and one fresh verifier and builds its review page.

    Always a new project, in a directory that does not exist yet: it never adds a question to a
    project that exists. Its pages go in one cache every `provenance ask` shares, unless
    `--cache` names another."""
    import os

    sources = _sources_or_refuse(source)
    question = question.strip()
    if not question:
        _refuse("the question is empty")
    if problem := _unwritable("the question", question):
        _refuse(problem)
    where = dir_ if dir_ is not None else _ask_dir(question)
    if where is None:
        _refuse("no directory name can be made from the question's words: pass --dir")
    root = proj.absolute(where)
    if os.path.lexists(root):
        # The filesystem root included, the one directory with no name to give a project.
        left = ("" if os.path.lexists(root / proj.FILE) else
                f" It holds no {proj.FILE}, so it is no project: if an interrupted "
                f"`provenance ask` left it, remove it.")
        _refuse(f"{root} exists: `provenance ask` starts a new project in a directory of its "
                f"own. Pass another --dir.{left}")
    name = name.strip() or root.name
    if problem := _unwritable("the project's name", name):
        _refuse(problem)
    setting = _cache_setting(cache, ASK_CACHE, root)

    qs = [{"id": "q1", "text": question,
           "claim_type": "adversarial" if adversarial else "mechanical",
           "parent": None, "rationale": "asked with provenance ask"}]
    p = _scaffold(root, name, sources, setting,
                  {"questions.json": json.dumps(qs, indent=1, ensure_ascii=False) + "\n"},
                  create=True)
    kind = "adversarial: two independent sources" if adversarial else "mechanical"
    con.print(Text(_printable(
        f"created {p.root}\n"
        f"  q1 ({kind}): {question}\n"
        f"  checked against {', '.join(sources)}, the shared cache in {p.cache / 'cache'}\n"
        f"Hand it to the skill, which gives it one researcher and a fresh verifier, then builds "
        f"its review page:\n"
        f"  {_hand_off(p.root, SKILL + ' ask')}", lines=True)), soft_wrap=True)


@app.command()
def status(data: Path = None, cache: Path = None, project: Path = None, subject: str = None):
    """Summary of where the run stands."""
    p, data = _project(data, project, subject=subject)
    claims = _load_or_exit(data / "claims", trust_machine_fields=True)
    # Even with no claims: a pending maps_from, or a question set nothing can read, is worth
    # settling before anyone researches on those ids.
    failing = _question_ids(data, claims, p)
    if failing is None:
        raise typer.Exit(1)   # as build renders nothing: no claim could be checked
    if not claims:
        con.print("No claims yet.")
        return
    # Run the same offline checks `provenance build` runs, so the summary can't disagree with what
    # the review app will actually show. Nothing is written back — this is a read-only view.
    # That starts with leaving out the claims build leaves out.
    claims = [c for c in claims if c.question_id not in failing]

    # The lists build checks against: the project's, as build reads them.
    rules = load_rules(p.sources)
    cache_root = _verdict_cache_root(data, cache, resolved=(p, data))
    _settle(claims, data, cache_root, rules, _archive_records(data))
    t = Table("qid", "type", "status", "sources", "corroboration", "conflicts", box=None)
    for c in claims:
        t.add_row(c.question_id, c.claim_type, c.status, str(len(c.sources)),
                  Text(_printable(c.corroboration_note or "-")), str(len(c.conflicts)))
    con.print(t)
    if failing:
        _left_out(failing, "this summary, as from the review app")


if __name__ == "__main__":
    app()
