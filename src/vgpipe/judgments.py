"""Verifier judgments, stored apart from the claims they judge.

They cannot live in the claim file. `vg verify` reloads claim files with
`strip_machine_fields()` on — which is what stops a researcher marking its own citation
verified — so a verdict written there is destroyed by the next verify run. In the smoke test
the orchestrator had to hand-sequence every verify before every writeback, and the verdicts
still had no effect on status.

One shard per question, keyed within it by source id (url + snippet), so a judgment follows
the citation it was about and lapses on its own when a retry changes the quote. A verdict is
about one question's claim, not about the source alone: two questions citing the same page
each get their own verdict, and the two can differ — a snippet can support one claim and be
`topic_only` for another. Nothing here pools verdicts across questions by sid.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, get_type_hints

from .models import QID_PATTERN

VERDICTS = ("supports", "topic_only", "contradicts", "superseded")


@dataclass
class Judgment:
    sid: str
    verdict: str
    note: str = ""
    judged_at: str = ""
    judge: str = "verifier"
    # What the source looked like when it was judged. A verdict is a claim about a source AS
    # CACHED AT JUDGMENT TIME: six were recorded as "roster-only, no bill number" and a later
    # re-fetch put the bill number back, leaving six settled-looking rejections describing text
    # that no longer exists. The direction matters less than the silence — a `supports` that
    # survives a re-fetch removing the supporting text ships a green row nobody checked.
    page_fetched_at: str = ""
    extractor_version: int = 0
    # A QUERY citation has no page; its equivalent is the query definition and the export the
    # verdict was formed against. The sid covers name, params and expected value, but not the
    # definition, so a query fixed to compute something else that happens to return the same
    # number would otherwise keep a verdict about the old calculation.
    query_version: int = 0
    export_date: str = ""
    # Which page that was, when it is not the cited URL: the snapshot an archive-verified
    # context came from. Empty for the cited page, as on every verdict from before it existed.
    page_url: str = ""


class UnreadableJudgments(ValueError):
    """A verdict file, or the judgments directory, exists but cannot be read as verdicts.

    A ValueError, so `vg judge`'s existing handler already reports it. A subclass, so every
    other command can stop on exactly this without also catching unrelated ValueErrors (a
    pydantic ValidationError is one) and presenting a bug as a data problem.
    """


class Rehomed(NamedTuple):
    """What a re-home did. `moved` counts verdicts that now sit in a shard they were not in —
    `filed` counts every verdict kept, so a mapping that matched nothing would read the same as
    one that worked."""
    filed: int
    archived: int
    questions: list[str]
    moved: int


class Unjudgeable(ValueError):
    """A verdict on this source cannot be recorded now, because nothing would tie it to the
    copy of the page the verifier read. `vg judge` refuses with the message and writes nothing."""


class CannotRehome(ValueError):
    """rehome() cannot tell which question a verdict belongs to, so it changed nothing.

    Reported like UnreadableJudgments. A separate class because every file is fine: what is
    missing is the answer — the mapping `vg judgments --repair --moved` takes — and picking
    one would attach a verdict to a claim it never judged.
    """


_TYPES = get_type_hints(Judgment)
_REQUIRED = {f.name for f in fields(Judgment)
             if f.default is MISSING and f.default_factory is MISSING}
if not all(isinstance(t, type) for t in _TYPES.values()):
    # _entry_problems checks each field against an exact class. An Optional, union or generic
    # field would fail every entry, or crash on `t.__name__` inside load(): give it its own
    # check there before adding it here.
    raise TypeError(f"Judgment fields must be plain classes, got {_TYPES}")
# What Source.sid makes: the first 12 hex characters of a sha1. Anything else — empty,
# padded, a typo — can never match a cited source, so it would read as a lapsed verdict, and
# rehome() archives lapsed verdicts out of the live shards.
_SID = re.compile(r"[0-9a-f]{12}")


class _Object(dict):
    """A JSON object that remembers any key it held twice. json.loads keeps only the last
    value, so without this `"verdict": "supports", "verdict": "contradicts"` reads as one
    verdict and the next rewrite deletes the other."""
    repeated: tuple[str, ...] = ()


def _object(pairs: list[tuple[str, object]]) -> _Object:
    o = _Object(pairs)
    if len(o) < len(pairs):
        keys = [k for k, _ in pairs]
        o.repeated = tuple(sorted({k for k in keys if keys.count(k) > 1}))
    return o


def _entry_problems(item, seen: set[str]) -> list[str]:
    """Everything that stops one list entry reading as a verdict — empty if it reads.

    All of them, not the first: a refusal the operator has to re-run to see the rest of turns a
    repair into a loop. `seen` is every sid earlier entries carried, readable or not.
    """
    if not isinstance(item, dict):
        return [f"not an object (got {type(item).__name__})"]
    problems = []
    if repeated := getattr(item, "repeated", ()):
        problems.append(f"repeated key(s) {', '.join(map(repr, repeated))}")
    # Both halves together, so a typo'd key reads as the one mistake it is:
    # "unknown key(s) 'verdit' and missing 'verdict'".
    problems += [f"{label} {', '.join(map(repr, sorted(names)))}"
                 for label, names in (("unknown key(s)", set(item) - _TYPES.keys()),
                                      ("missing", _REQUIRED - set(item))) if names]
    # `type() is`, not isinstance: JSON true is an int to isinstance, and NaN is a float that
    # compares False with everything — an extractor_version of NaN would never go stale.
    # A null note is the one exception: it holds nothing to lose, and every reader already
    # takes it as empty (`support_note` is `str | None`), so refusing it would only stop a run.
    problems += [f"{k} must be {t.__name__}, got {item[k]!r}"
                 for k, t in _TYPES.items() if k in item and type(item[k]) is not t
                 and not (k == "note" and item[k] is None)]
    sid, verdict = item.get("sid"), item.get("verdict")
    if isinstance(sid, str) and not _SID.fullmatch(sid):
        problems.append(f"sid {sid!r} is not a source id (12 lowercase hex characters)")
    if isinstance(verdict, str) and verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    if isinstance(sid, str) and sid in seen:
        # Keyed by sid, so one of the two would be dropped — and deleted by the next rewrite.
        problems.append("a second verdict for a source this file already judged")
    return problems


def path_for(root: Path, question_id: str) -> Path:
    """The shard for a question id — refusing any id that would name a file elsewhere.

    The claim schema constrains question ids, but only for values that pass through it, and
    `vg judge` takes its id straight from the command line, where verifier agents (which have
    Bash) put it. `../claims/q7` made record() create and replace a file outside judgments/,
    and an absolute path discards `root` entirely. So the check lives here, where the path is
    built, and every read or write by question id passes through it. The schema's own shape
    is enough: no separator and no leading dot means the result is always a direct child of
    judgments/. It bounds the id, not `root`: choosing the run directory is what `--data` is for.
    `fullmatch`, because `$` in a `match` also accepts a trailing newline.
    """
    if not re.fullmatch(QID_PATTERN, question_id):
        raise ValueError(f"refusing question id {question_id!r}: a verdict file is named after "
                         f"it, so it must have the shape every claim's question_id has "
                         f"({QID_PATTERN}) — no path separators, no leading dot.")
    return root / "judgments" / f"{question_id}.json"


def backup_dir(root: Path) -> Path:
    """Where rehome() keeps every shard as it was, for exactly as long as it is rewriting them."""
    return root / "judgments-backup"


def archive_dir(root: Path) -> Path:
    """Where rehome() keeps verdicts that no current claim cites, one directory per run."""
    return root / "judgments-archive"


# Every holder keeps the lock for one shard read or one rewrite: milliseconds. Waiting longer
# than this means a holder is stuck, and verifier agents blocked on it forever would report
# nothing — which the judgment pass reads as nothing to report.
LOCK_TIMEOUT = 60.0


@contextmanager
def _lock(root: Path, *, shared: bool = False):
    """Hold the judgments directory while reading or rewriting it.

    rehome() reads every shard and then rewrites them, so a `vg judge` that landed in between
    was never read, and the rewrite deleted it. Writers take the lock exclusively. Readers take
    it shared, so they wait out a rewrite rather than seeing half of one. It is an flock on the
    directory itself: no lock file to litter a tracked data dir, and a holder that dies releases
    it with its process.
    """
    d = root / "judgments"
    try:
        fd = os.open(d, os.O_RDONLY)
    except FileNotFoundError:
        fd = None           # nothing on disk yet to protect
    except OSError as e:
        if not shared:
            raise UnreadableJudgments(f"{d} cannot be locked for writing: {e} — fix its "
                                      f"permissions.") from None
        fd = None           # the read that follows names what it could not read
    try:
        if fd is not None:
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise UnreadableJudgments(
                            f"{d} has been locked by another vg process for over "
                            f"{LOCK_TIMEOUT:.0f}s. A verdict write or re-home holds it for "
                            f"milliseconds, so that process is stuck: stop it and re-run."
                        ) from None
                    time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            os.close(fd)


def refuse_if_interrupted(root: Path) -> None:
    """Raise if a re-home was interrupted. A backup left behind means a rewrite stopped
    partway: some shards may already hold their new contents and others not. Reading that as
    the verdicts would render whatever is missing as unreviewed, which is the silent loss the
    backup exists to prevent."""
    b = backup_dir(root)
    try:
        b.stat()
    except FileNotFoundError:
        return
    except OSError as e:
        # exists() would read this as "no backup" (3.13+), and the shards as whole.
        raise UnreadableJudgments(f"cannot tell whether {b} exists: {e}.") from None
    raise UnreadableJudgments(
        f"{b} exists, so a re-home of {root / 'judgments'} was interrupted and its shards may "
        f"be half-rewritten. Run `vg judgments --rollback --data {root}` to put back "
        f"everything it changed (the verdicts, and the claim files if it was `vg remap "
        f"--apply`), then re-run it.")


def load(root: Path, question_id: str) -> dict[str, Judgment]:
    with _lock(root, shared=True):
        refuse_if_interrupted(root)
        return _read(path_for(root, question_id))


def _read(p: Path) -> dict[str, Judgment]:
    try:
        # UTF-8, not the locale's encoding: a decode error is now fatal, so a valid shard with a
        # smart quote in a note must not be refused on a machine with a non-UTF-8 locale.
        raw = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except FileNotFoundError:
        # Only a file that is really not there means "no verdicts". Asking `exists()` first let
        # a permission error escape as a traceback (3.12) or read as absent (3.13+).
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise UnreadableJudgments(f"{p} is unreadable: {e}. A malformed judgments file must not "
                                  f"read as 'nobody judged this' — repair it. Do not delete it: "
                                  f"that discards every verdict it holds.") from None
    if not isinstance(raw, list):
        # Silently returning {} here made hand-repaired dict-shaped files vanish entirely, and
        # every source read as unreviewed with no error.
        raise UnreadableJudgments(f"{p} must be a LIST of verdicts, got {type(raw).__name__}. "
                                  f"Returning empty would silently discard every verdict in it "
                                  f"— rewrite it as a list. Do not delete it: that discards them "
                                  f"too.")
    # The file-level rule, one level down: an entry skipped here reads as "nobody judged this",
    # and record() and rehome() rewrite the file from what this returns, so it is also deleted.
    out: dict[str, Judgment] = {}
    problems: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        why = _entry_problems(item, seen)
        sid = item.get("sid") if isinstance(item, dict) else None
        if isinstance(sid, str):
            seen.add(sid)
        if why:
            label = f"entry {i}" + (f" (sid {sid})" if isinstance(sid, str)
                                    and _SID.fullmatch(sid) else "")
            problems.append(f"{label}: {' and '.join(why)}")
            continue
        out[sid] = Judgment(**item)
    if problems:
        raise UnreadableJudgments(f"{p} has {len(problems)} unreadable verdict(s): "
                                  f"{'; '.join(problems)}. Skipping one would read its source as "
                                  f"unreviewed, and the next rewrite would delete it — fix each "
                                  f"entry (entries count from 0). Do not delete one: that discards "
                                  f"its verdict.")
    return out


def load_every(root: Path) -> dict[str, dict[str, Judgment]]:
    """Every recorded verdict, per question: {question id: {source id: verdict}}, each shard
    read once.

    Never pooled by source id. Two questions citing the same source each hold their own
    verdict for it, and the pooled `load_all()` this replaces kept only the shard that sorted
    last.

    Reads every shard before returning anything, so a caller about to rewrite the directory
    learns that one is unreadable while nothing has been touched yet — and a caller that
    also needs the verdicts (`vg judgments`) gets them from the same read.
    """
    with _lock(root, shared=True):
        return _load_every(root)


def _load_every(root: Path) -> dict[str, dict[str, Judgment]]:
    refuse_if_interrupted(root)
    d = root / "judgments"
    try:
        # Not glob(): it silently skips a directory it cannot list, which would read a
        # directory full of verdicts as empty.
        shards = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise UnreadableJudgments(f"{d} is unreadable: {e}. Every verdict in it would read as "
                                  f"'nobody judged this' — fix its permissions.") from None
    # Read by path, not by id: a shard found by listing judgments/ is inside it by
    # construction, and one named before path_for() checked ids must still be read rather
    # than stop every command over a file that escapes nothing.
    return {p.stem: _read(p) for p in shards}


# Fields added after verdict files were already shared, written only when they hold something.
# load() refuses an unknown key, so an older checkout reading a shared data/ would stop on every
# shard this code rewrote — including shards of page verdicts, which never use these. A query
# verdict still carries them: older code cannot check it, and refusing says so. `page_url` the
# same way: `vg judge` sets it only for a snapshot, which older code would check against the
# paywall stub, and leaves it empty for the cited page, which older code checks right.
_WRITTEN_WHEN_SET = ("query_version", "export_date", "page_url")


def _entry(j: Judgment) -> dict:
    d = asdict(j)
    for k in _WRITTEN_WHEN_SET:
        if d[k] == Judgment.__dataclass_fields__[k].default:
            del d[k]
    return d


def _write(p: Path, items) -> None:
    """Replace a shard whole, never in place.

    A reader now refuses a partial file instead of reading it as empty, and verifier agents
    write verdicts while other commands read them — so a half-written shard would stop every
    command, and an interrupted write would leave one behind. Write a sibling temp file (named
    so `*.json` never matches it) and rename it over the shard, which is atomic.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps([_entry(j) for j in items], indent=1))
            f.flush()
            # Contents on disk before the rename: otherwise a power loss can keep the rename
            # and lose the data, leaving an empty shard that every command then refuses.
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def record(root: Path, question_id: str, sid: str, verdict: str, note: str = "",
           page_fetched_at: str = "", extractor_version: int = 0,
           query_version: int = 0, export_date: str = "", page_url: str = "") -> Judgment:
    j = Judgment(sid=sid, verdict=verdict, note=note,
                 page_fetched_at=page_fetched_at, extractor_version=extractor_version,
                 query_version=query_version, export_date=export_date, page_url=page_url,
                 judged_at=datetime.now(UTC).isoformat(timespec="seconds"))
    # The writer holds itself to the reader's rule: an entry load() refuses would stop every
    # command that reads this file until someone repaired it by hand.
    if why := _entry_problems(asdict(j), set()):
        raise ValueError(f"refusing to record a verdict that could not be read back: "
                         f"{' and '.join(why)}")
    p = path_for(root, question_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Read and rewrite under one lock: a rehome() between the two would otherwise be undone.
    with _lock(root):
        refuse_if_interrupted(root)
        existing = _read(p)
        existing[sid] = j
        _write(p, existing.values())
    return j


def rehome(root: Path, claims, moved: Mapping[str, str | None] | None = None,
           then: Callable[[], None] | None = None,
           also: Sequence[Path] = (), *, exact: bool = False, creates: Sequence[Path] = (),
           stamp: str | None = None) -> Rehomed:
    """Re-file verdicts under the question whose claim they judged, after claims have moved.

    Entries are keyed by source id, but the FILES are named by question id — so moving a claim
    to a new id leaves its verdicts behind in the old shard, and the new id finds nothing. That
    orphaned verdict renders as `unreviewed`, which the pipeline reads as "not yet judged"
    rather than "lost", so a damaged run looks merely incomplete: the operator either pays for
    the whole judgment pass twice or ships rows whose verdicts exist but aren't attached.

    A verdict belongs to a (question, source) pair, never to a source alone. This used to pool
    every shard by source id and file each verdict under the last claim citing it, so a source
    cited by two questions kept one of their two verdicts, and could hand it to the claim that
    never earned it: a `supports` for one claim rendering green on another that a verifier had
    judged `topic_only`.

    `moved` maps a shard to the question whose claim it judged: {old question id: new question
    id}. A verdict only ever moves along it: a source id is not proof of ownership, so nothing
    here moves one because another question happens to cite its source. A shard named as a key
    follows its claim to the new id; one that a claim moved onto, and that is not a key itself,
    judged the claim there before; any other shard stays on its own id. Each verdict is then
    filed under that question if its claim cites the source, and archived if not — unless that
    would be a guess:

    - `exact=True` is `vg remap`: its mapping names every claim that moves, one per id, and
      they move inside this call, so no verdict can have been recorded against a claim's new
      id. A shard a claim moved onto judged a claim that is gone (archived as stranded), and a
      verdict its claim no longer cites has lapsed, whoever else cites the source.
    - Otherwise (`vg judgments --repair`) the claims moved some time ago, and `moved` is only
      what the operator stated — possibly nothing. It speaks for the shards it names as keys
      and no others, and verdicts may have been judged since the move, so several shards may
      name one claim: the verdicts it earned before it moved, and those judged on its new id
      since (`q21:q18` and `q18:q18`). Where two hold a verdict for one source, the later
      replaces the earlier, as record() would have. A value of None (`--gone OLD`) says the
      claim OLD's shard judged is gone, so its verdicts are archived. A mapping out of an id no
      claim holds has nothing left to contradict it, so the claim it names must cite every
      verdict in that shard: a claim that moved keeps its sources. A verdict another question
      cites is ambiguous when nothing names its
      shard as a key (its claim dropped the source, or moved away, and only the operator knows
      which), or when the mapping sends its shard elsewhere but it is the claim on the shard's
      own id that cites it (judged after the move, or already re-homed: re-applying a mapping
      would move it a second time).

    Anything ambiguous raises CannotRehome naming each verdict, with nothing changed.

    Verdicts no current claim cites are not deleted: they go to a fresh directory under
    archive_dir(), so a claim archived by `vg remap --archive-stranded` can be restored with its
    verdicts. The rewrite is a transaction (see _rewrite()): it completes, is rolled back on a
    failure, or leaves a backup that `vg judgments --rollback` undoes. `then` runs inside it,
    after the shards are written, and `also` names what it changes, each a direct child of
    `root`: a directory (its `*.json` files) or a file (its bytes, or its absence). They are
    snapshotted into the same backup and restored with the shards. remap moves the claim files
    and appends its re-apply marker there, so verdicts, claims and marker roll back together —
    rolling back only the shards put verdicts on old ids under claims on new ones. `creates`
    names what `then` makes inside `root` that is not there yet: remap's claims-archive/<stamp>/,
    where it archives stranded claims. A restore removes each one, and any directory above it
    the re-home made while that is still empty, so the stranded files, put back in claims/ from
    the backup, are not left behind as a second copy too. `stamp` names the verdict archive's
    run directory, so remap files an archived claim and its verdicts under one name. All of it
    happens under the directory lock, so a `vg judge` cannot land between the read and the
    rewrite and be lost. Returns a Rehomed.
    """
    with _lock(root):
        # Every shard is read before anything is written, so an unreadable one raises while
        # the directory is still intact.
        before = _load_every(root)
        after, orphans = _plan(before, claims, moved, exact, root)
        filed = sum(len(v) for v in after.values())
        archived = sum(len(v) for v in orphans.values())
        # Identity, not equality: a verdict that stayed is the very object read from its shard.
        moved_n = sum(1 for q, shard in after.items() for sid, j in shard.items()
                      if before.get(q, {}).get(sid) is not j)
        if after != before or then is not None:
            _rewrite(root, before, after, orphans, then, also, creates, stamp)
    return Rehomed(filed, archived, sorted(after), moved_n)


def new_stamp() -> str:
    """A run directory's name under an archive: the time, to the microsecond."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _plan(before: dict[str, dict[str, Judgment]], claims,
          moved: Mapping[str, str | None] | None, exact: bool, root: Path):
    """Where each verdict goes: ({question: {sid: verdict}} to keep, {shard: {sid: verdict}} to
    archive). Raises CannotRehome if any verdict has no single right home (see rehome())."""
    moved = dict(moved or {})
    if exact and len(set(moved.values())) < len(moved):
        raise ValueError(f"two claims cannot move onto one question: {moved}")
    # `--moved Q1:q1`, where this disk opens Q1.json as q1.json, is no move: it says q1's own
    # shard judged q1, as `--moved q1:q1` does. Read as a move out of a vacant id, the shard had
    # to bear it out alone, and a verdict q1 has since dropped refused the repair.
    said: dict[str, str | None] = {}
    by: dict[str, str] = {}
    for old, new in moved.items():
        key = new if new is not None and old != new and _same_file(root, old, new) else old
        if key in said and said[key] != new:
            raise CannotRehome(f"{by[key]} and {old} are one shard on this disk, and the mapping "
                               f"says two things about it ({by[key]}:{said[key]}, {old}:{new}), "
                               f"so nothing was changed. Say one.")
        said[key], by[key] = new, old
    moved = said
    citers: dict[str, set[str]] = {}
    for c in claims:
        for s in c.sources:
            citers.setdefault(s.sid, set()).add(c.question_id)
    onto: dict[str, list[str]] = {}
    for old, new in moved.items():
        if new is not None and old != new:
            onto.setdefault(new, []).append(old)

    def owner(qid: str) -> str | None:
        """The question whose claim this shard judged; None for one no longer on any id."""
        if qid in moved:
            return moved[qid]
        # A claim moved onto this id, so its shard judged the one there before: gone (remap
        # archived it as stranded), or — for --repair — wherever the operator has not said.
        return None if qid in onto else qid

    held = {c.question_id for c in claims}
    known = held | moved.keys() | {new for new in moved.values() if new is not None}

    def resolve(stem: str) -> str:
        """The id whose shard this is. Where the disk opens Q1.json as q1.json, `vg build`
        applies it to claim q1 and `vg judge q1` writes into it: it is q1's shard, by the disk's
        own identity rather than a guess from a source id. Keyed by its stem, remap archived the
        verdicts of a claim it moved as lapsed. On a case-sensitive disk it is its own shard."""
        return stem if stem in known else opened_as(root, stem, known) or stem

    wanted: dict[tuple[str, str], list[tuple[str, Judgment]]] = {}
    orphans: dict[str, dict[str, Judgment]] = {}
    unsaid: list[str] = []          # nothing says which claim the shard judged
    contradicted: list[str] = []    # the mapping says, and the claims disagree
    unsupported: list[str] = []     # the mapping says, and its shard does not bear it out
    for stem, entries in before.items():
        # `qid` is the id the shard is under, `stem` its file name: they differ only for a
        # shard the disk opens under another id.
        qid = resolve(stem)
        home = owner(qid)
        for sid, j in entries.items():
            cited_by = citers.get(sid, set())
            if home in cited_by:
                wanted.setdefault((home, sid), []).append((stem, j))
            elif not exact and home is not None and home != qid and qid not in held:
                # Out of an id no claim holds, nothing is left to contradict the mapping, so its
                # shard has to bear it out alone. A claim that moved keeps its sources: one the
                # named claim does not cite says the mapping named the wrong claim — a question
                # that merely cites a deleted claim's page, say — or that the claim was retried
                # since, which the operator settles by hand. Without this, `--moved q2:q7` for a
                # hand-deleted q2 filed its co-cited verdict green on q7 and archived the rest.
                unsupported.append(f"{stem}/{sid}: the mapping says {stem}'s shard judged the "
                                   f"claim now at {home}, which does not cite it")
            elif exact or not cited_by:
                # remap's mapping is complete and its own doing, so a verdict its claim no
                # longer cites has lapsed, whoever else cites the source. Cited by nothing, it
                # has lapsed on any reading.
                orphans.setdefault(stem, {})[sid] = j
            elif qid not in moved:
                # Either the claim this shard judged dropped the source (the verdict lapsed) or
                # it moved away. Only the operator can say which — never the source id.
                where = (f"{', '.join(onto[qid])} moved onto {qid}, and nothing says which "
                         f"claim {stem}'s own shard judged" if home is None else
                         f"the claim at {qid} no longer cites it" if qid in held else
                         f"no claim holds {stem}")
                unsaid.append(f"{stem}/{sid}: {where}; also cited by "
                              f"{', '.join(sorted(cited_by))}")
            elif home is not None and home != qid and qid in cited_by:
                # The mapping says this shard judged another claim, and it is the claim on the
                # shard's own id that cites the source. Lapsed when the moved claim dropped it,
                # or judged since the move, or re-homed already: moving it could attach it to a
                # claim it never judged, and archiving it could lose the second reading's.
                contradicted.append(f"{stem}/{sid}: the mapping says {stem}'s shard judged the "
                                    f"claim now at {home}, which does not cite it, but the claim "
                                    f"now at {qid} does")
            else:
                # The mapping names the claim this shard judged, and it no longer cites the
                # source (or is gone): lapsed, as with remap. Who else cites it says nothing.
                orphans.setdefault(stem, {})[sid] = j

    # Only --repair can file two verdicts under one (question, source): the one a claim earned
    # before it moved and the one judged on its new id since. Both judged the same claim, by the
    # operator's word, so the later replaces the earlier exactly as record() would have — the
    # earlier is archived, not deleted. Verdicts that cannot be put in order are refused.
    after: dict[str, dict[str, Judgment]] = {}
    unordered: list[str] = []
    for (home, sid), found in sorted(wanted.items()):
        times = [_instant(j.judged_at) for _, j in found]
        if len(found) > 1 and (None in times or times.count(max(times)) > 1):
            unordered.append(f"{home}/{sid}: {', '.join(q for q, _ in found)} each hold a "
                             f"verdict for it, judged at "
                             f"{', '.join(repr(j.judged_at) for _, j in found)}")
            continue
        keep = found[times.index(max(times))][1] if len(found) > 1 else found[0][1]
        after.setdefault(home, {})[sid] = keep
        for q, j in found:
            if j is not keep:
                orphans.setdefault(q, {})[sid] = j

    # Two ids the disk opens as one file (claims Q1 and q1, where it folds case) would each be
    # written, the second over the first, whose verdicts would vanish with no archive entry.
    # Unlinking before writing only helps a shard that is leaving; both of these stay.
    ids = sorted(after)
    shared = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if _one_file(root, a, b)]

    if unsaid or contradicted or unsupported or unordered or shared:
        why = []
        if unsaid:
            why.append(
                f"{'; '.join(unsaid)}. A source id is not proof of ownership, so a verdict is "
                f"never moved on one — and a question that also cites the source is not a "
                f"candidate for where a claim went: mapping a shard onto it is that same guess, "
                f"made by hand. Say what you know of each shard's claim, from the run's own "
                f"record (the remap that moved it, the claim files' history), to `vg judgments "
                f"--repair --data {root}`: `--moved OLD:NEW` for a claim you know moved from OLD "
                f"to NEW, `--moved OLD:OLD` for one still on OLD, or `--gone OLD` for one that "
                f"no longer exists, even if another claim has since taken its id. A verdict the "
                f"named claim no longer cites has lapsed, and is archived. Several shards may "
                f"name one claim: the verdicts it earned before it moved and those judged on its "
                f"new id since")
        if contradicted:
            why.append(
                f"{'; '.join(contradicted)}. Each has three readings: it lapsed when the moved "
                f"claim dropped the source; it was judged against the claim on the shard's own "
                f"id after the move; or the mapping was applied already and the verdicts "
                f"re-homed. Only the first fits the mapping, so check it — re-applying a "
                f"finished one would move verdicts a second time. If a verdict did lapse, move "
                f"that entry into judgments-archive/ by hand; a shard holding verdicts for two "
                f"claims has to be split by hand")
        if unsupported:
            why.append(
                f"{'; '.join(unsupported)}. No claim holds those ids, so only the shard can bear "
                f"the mapping out, and a claim that moved keeps its sources: a verdict the named "
                f"claim does not cite says it is the wrong claim. If the claim that shard judged "
                f"is gone, say `--gone OLD`. If it did move and has dropped these sources since, "
                f"move each such entry into judgments-archive/ by hand, then re-run")
        if unordered:
            why.append(
                f"{'; '.join(unordered)}. The later one would replace the earlier, as a "
                f"re-judgment does, but these cannot be put in order: fix each `judged_at`, or "
                f"move the one to drop into judgments-archive/ by hand")
        if shared:
            why.append(
                f"{'; '.join(f'{a} and {b}' for a, b in shared)} are one file on this disk, which "
                f"does not tell case apart, so writing both would replace one claim's verdicts "
                f"with the other's and archive nothing. Two claims whose ids differ only in case "
                f"cannot both hold verdicts here: give one of them another id")
        n = len(unsaid) + len(contradicted) + len(unsupported) + len(unordered)
        heads = ([f"cannot tell which question {n} verdict(s) belong to"] if n else []) + (
            [f"{len(shared)} pair(s) of claims would share one verdict file"] if shared else [])
        raise CannotRehome(
            f"{', and '.join(heads)}, so nothing was changed: {'. '.join(why)}. Do not delete "
            f"one: that discards the verdict.")
    return after, orphans


def _archive(dest: Path, orphans: dict[str, dict[str, Judgment]]) -> None:
    """Keep verdicts no current claim cites, under the shard name they had. A fresh directory
    per run, so an earlier run's archive is never merged into or overwritten."""
    dest.mkdir(parents=True)
    for qid, items in sorted(orphans.items()):
        _write(dest / f"{qid}.json", items.values())
    _fsync_dir(dest)


def _rewrite(root: Path, before: dict[str, dict[str, Judgment]],
             after: dict[str, dict[str, Judgment]], orphans: dict[str, dict[str, Judgment]],
             then: Callable[[], None] | None, also: Sequence[Path],
             creates: Sequence[Path] = (), stamp: str | None = None) -> None:
    """Make the shards read `after` and archive `orphans` as one transaction.

    No order of per-file writes is safe on its own: when two claims swap ids, each shard gains
    what the other loses. So the transaction is recorded by backup_dir(), which is complete or
    absent. It is built beside its final name, made durable, and only then renamed into place.
    A process that dies while building it has touched no shard, and the next run discards the
    leftover. Once the backup exists:
    - every write and the archive happen, then `then`, then the commit: _retire() renames the
      backup out of the name readers and rollback trust, in one step. Deleting it in place was
      not atomic — killed partway, it left a backup missing shards that a rollback then trusted,
      unlinking every shard it lacked;
    - if any of it raises, _restore() puts the shards (and everything in `also`) back from the
      backup, and removes the archive and everything in `creates`, which it names;
    - if the process dies, the backup stays, every reader refuses, and `vg judgments --rollback`
      runs the same _restore().
    """
    backup, building = backup_dir(root), _building_dir(root)
    # A restore deletes the archive run it names, so it must not be there yet: one that is holds
    # an earlier run's verdicts. A fresh stamp can't collide; one from the caller could.
    stamp = stamp or new_stamp()
    if not _plain_name(stamp):
        raise ValueError(f"an archive run is named by one path component, not {stamp!r}")
    if orphans and (archive_dir(root) / stamp).exists():
        raise FileExistsError(errno.EEXIST, "a re-home archives here, and it already exists",
                              str(archive_dir(root) / stamp))
    # Asked here, under the lock, rather than by the caller: what a rollback removes is what the
    # transaction makes, so it is decided at the moment the transaction begins.
    made = [m for path in creates for m in _made_by(root, path)]
    shutil.rmtree(building, ignore_errors=True)     # a build that died: no shard was touched
    building.mkdir()
    try:
        for qid in before:
            _copy(_on_disk(root, qid), building / f"{qid}.json")
        if orphans:
            # Named up front, so a rollback can remove an archive this run began: its verdicts
            # go back into their shards, and a copy left behind would be archived again.
            _durable_text(building / _ARCHIVE_NAME, stamp)
        if made:
            # The same for what `then` creates: remap's stranded claims go back into claims/
            # from the snapshot, and a copy left in claims-archive/ would be archived again.
            _durable_text(building / _CREATES_NAME,
                          "\n".join(f"{kind} {name}" for kind, name, _p in made))
        if also:
            _snapshot(root, also, building)
        _fsync_dir(building)
        os.replace(building, backup)
        _fsync_dir(root)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    try:
        # Shards leaving go first. A new shard's name can be the same file as a leaving one:
        # q1.json IS Q1.json on a case-insensitive disk. Written first, it replaced Q1.json
        # under the old name, and the unlink then deleted the verdicts just written.
        # Unlinked first, the name is free and the write creates the claim's exact id. The
        # backup holds every shard either way, so the order costs nothing if a step fails.
        for qid in sorted(before.keys() - after.keys()):
            _on_disk(root, qid).unlink()
        for qid, items in sorted(after.items()):
            if items != before.get(qid):
                # A shard already on disk is rewritten where it is; a new one is named by an id
                # from outside (a claim, the mapping), which path_for() checks.
                _write(_on_disk(root, qid) if qid in before else path_for(root, qid),
                       items.values())
        if orphans:
            _archive(archive_dir(root) / stamp, orphans)
        if (root / "judgments").is_dir():
            _fsync_dir(root / "judgments")
        if then is not None:
            then()
            for path in also:
                _fsync_dir(path if path.is_dir() else root)
            for kind, _name, path in made:
                # Every directory it made — for the created path, with everything under it: the
                # renames into it — then the directory holding each one: its own entry.
                if path.is_dir() and not path.is_symlink():
                    below = [Path(d) for d, _, _ in os.walk(path)] if kind == "tree" else [path]
                    for d in below:
                        _fsync_dir(d)
                _fsync_dir(path.parent)
    except BaseException:
        _restore(root)
        raise
    _retire(root)


def _retire(root: Path) -> None:
    """Remove the backup as one atomic step: rename it to a name nothing reads, make the rename
    durable, and only then delete it. A kill during the delete leaves a leftover that readers and
    rollback ignore and the next run clears."""
    discard = _discard_dir(root)
    shutil.rmtree(discard, ignore_errors=True)
    os.replace(backup_dir(root), discard)
    _fsync_dir(root)
    shutil.rmtree(discard, ignore_errors=True)


def rollback(root: Path) -> tuple[int, list[str], list[str]]:
    """Undo a re-home that was interrupted, from the backup it left. Returns the number of shards
    put back (0 if there was nothing to undo), the names of the other paths it put back —
    remap's claims/ and its marker — and the paths it removed because the re-home created them
    (remap's claims-archive run), relative to `root`.

    The backup is complete by construction (see _rewrite()), so this needs no judgment: it makes
    the shards, and whatever else the re-home was changing, exactly what the backup holds, and
    removes the archive the re-home began.
    """
    with _lock(root):
        try:
            backup_dir(root).stat()
        except FileNotFoundError:
            return 0, [], []
        also = [name for _kind, name in _read_also(backup_dir(root))]
        # Reported only if it was there and now is not: a re-home killed before `then` made
        # nothing, and a removal that failed partway removed nothing a reader should trust.
        there = [(name, p) for _kind, name, p in _read_created(root, backup_dir(root))
                 if p.exists() or p.is_symlink()]
        restored = _restore(root)
        return restored, also, [name for name, p in there if not (p.exists() or p.is_symlink())]


def _restore(root: Path) -> int:
    """Make the shards, and each path the backup's ALSO list names, exactly what backup_dir()
    holds; remove the archive it names; then retire it. Copies rather than moves, so a failure
    partway leaves the backup whole for a retry."""
    backup = backup_dir(root)
    shards = _mirror(backup, root / "judgments")
    for kind, name in _read_also(backup):
        target, saved = root / name, backup / _ALSO_DIR / name
        if kind == "dir":
            _mirror(saved, target)
        elif kind == "file":
            _copy(saved, target)
        else:
            target.unlink(missing_ok=True)      # it did not exist before the re-home
    _fsync_dir(root)
    if stamp := _read_name(backup / _ARCHIVE_NAME):
        shutil.rmtree(archive_dir(root) / stamp, ignore_errors=True)
    for kind, _name, made in _read_created(root, backup):
        # Absent when the re-home began (_rewrite() checked). Best effort, like the archive
        # above: what is left is a second copy of what the restore just put back, and failing
        # here would hide the error that started the restore. A directory above it the re-home
        # made goes only while empty: the created path is its own, but anything put beside it
        # since is not.
        if kind == "dir":
            with suppress(OSError):
                made.rmdir()
        elif made.is_dir() and not made.is_symlink():
            shutil.rmtree(made, ignore_errors=True)
        else:
            with suppress(OSError):
                made.unlink(missing_ok=True)
        if made.parent.is_dir():
            _fsync_dir(made.parent)
    _retire(root)
    return shards


def _mirror(src: Path, dst: Path) -> int:
    """Make dst's *.json files exactly src's. Every file dst has that src lacks was created by
    the interrupted re-home, from contents src already holds.

    Removed before anything is copied, as _rewrite() does: after a re-home renamed Q1.json to
    q1.json, copying Q1.json back on a case-insensitive disk lands in q1.json under that name,
    and unlinking q1.json afterwards deleted what the rollback had just restored."""
    kept = sorted(p.name for p in src.iterdir() if p.suffix == ".json")
    dst.mkdir(exist_ok=True)
    for p in dst.iterdir():
        if p.suffix == ".json" and p.name not in kept:
            p.unlink()
    for name in kept:
        _copy(src / name, dst / name)
    _fsync_dir(dst)
    return len(kept)


def _snapshot(root: Path, also: Sequence[Path], building: Path) -> None:
    """Save each path in `also` into the backup being built, and list them in its ALSO marker as
    `dir NAME`, `file NAME`, or `absent NAME` (restoring that one means removing it)."""
    saved = building / _ALSO_DIR
    saved.mkdir()
    lines = []
    for path in also:
        if path.parent != root:
            raise ValueError(f"{path} must be directly inside {root}")
        if path.is_dir():
            (saved / path.name).mkdir()
            for f in sorted(path.glob("*.json")):
                _copy(f, saved / path.name / f.name)
            _fsync_dir(saved / path.name)
            lines.append(f"dir {path.name}")
        elif path.exists():
            _copy(path, saved / path.name)
            lines.append(f"file {path.name}")
        else:
            lines.append(f"absent {path.name}")
    _fsync_dir(saved)
    _durable_text(building / _ALSO_NAME, "\n".join(lines))


def _read_also(backup: Path) -> list[tuple[str, str]]:
    """The (kind, name) pairs a backup's ALSO marker lists; malformed lines are ignored rather
    than trusted, since each name is joined to the run directory."""
    try:
        text = (backup / _ALSO_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        kind, _, name = line.partition(" ")
        if kind in ("dir", "file", "absent") and _plain_name(name):
            out.append((kind, name))
    return out


def _plain_name(name: str) -> bool:
    """Whether `name` is one path component a backup marker may name: it is joined to the run
    directory and then restored or deleted, so nothing that could reach outside it. The one
    test for it, for every marker."""
    return bool(name) and name not in (".", "..") and "/" not in name


def _made_by(root: Path, path: Path) -> list[tuple[str, str, Path]]:
    """What creating `path` makes, as the backup's CREATES marker lists it, deepest first:
    `path` itself (`tree`, removed whole), then each directory above it, below `root`, that does
    not exist yet (`dir`, removed only while empty). `path` must not exist: whatever is there
    belongs to something else, and the rollback would delete it."""
    name = _created_name(root, path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(errno.EEXIST, "a re-home creates this, and it already exists",
                              str(path))
    made = [("tree", name, path)]
    for d in path.parents:
        if d == root or d.exists() or d.is_symlink():
            break
        made.append(("dir", _created_name(root, d), d))
    return made


def _created_name(root: Path, path: Path) -> str:
    """`path` as the backup's CREATES marker lists it: relative to `root`, one plain name per
    component. A restore deletes what the marker names, so anything that could reach outside the
    run directory is refused here, and again by _read_created() on the way back in."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = ()
    if not parts or not all(_plain_name(p) for p in parts):
        raise ValueError(f"a re-home creates only inside {root}, not {path}")
    return "/".join(parts)


def _read_created(root: Path, backup: Path) -> list[tuple[str, str, Path]]:
    """The (kind, name, path) entries a backup's CREATES marker lists, in its order; malformed
    lines are ignored rather than trusted, since each names something to delete."""
    try:
        text = (backup / _CREATES_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        kind, _, name = line.partition(" ")
        parts = name.split("/")
        if kind in ("tree", "dir") and all(map(_plain_name, parts)):
            out.append((kind, name, root.joinpath(*parts)))
    return out


def _read_name(marker: Path) -> str | None:
    """A marker's single path component, or None. Refuses anything that could reach outside the
    directory it is joined to."""
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return name if _plain_name(name) else None


def _on_disk(root: Path, stem: str) -> Path:
    """A shard found by listing judgments/, addressed by the name it has. It is inside the
    directory by construction, and path_for() would refuse a name from before ids were checked,
    stopping a re-home over a file that escapes nothing."""
    return root / "judgments" / f"{stem}.json"


def _same_file(root: Path, stem: str, question_id: str) -> bool:
    """Whether this disk opens shard `stem` under `question_id`: Q1.json as q1.json on a
    case-insensitive disk. Asked of the disk, never guessed from the names."""
    try:
        return os.path.samefile(_on_disk(root, stem), path_for(root, question_id))
    except (OSError, ValueError):
        return False


def opened_as(root: Path, stem: str, question_ids) -> str | None:
    """The one id among `question_ids`, other than its own name, that this disk opens shard
    `stem` as: q1 for Q1.json where the disk folds case. None if there is none, or more than one.
    The one test for it, so `vg judgments` and a re-home never disagree about whose shard it is.
    """
    same = [q for q in question_ids
            if q != stem and q.casefold() == stem.casefold() and _same_file(root, stem, q)]
    return same[0] if len(same) == 1 else None


def _disk_folds_case(root: Path) -> bool:
    """Whether this disk opens a name in judgments/ under another case, asked of the directory
    itself — for ids with no shard yet to ask about."""
    d = root / "judgments"
    try:
        return os.path.samefile(d, d.with_name(d.name.upper()))
    except OSError:
        return False


def _one_file(root: Path, a: str, b: str) -> bool:
    """Whether the shards for question ids `a` and `b` are one file on this disk. Ids are ASCII
    (QID_PATTERN), so only case can make two of them one name; whether it does is the disk's."""
    if a.casefold() != b.casefold():
        return False
    if _on_disk(root, a).exists() or _on_disk(root, b).exists():
        return _same_file(root, a, b)
    return _disk_folds_case(root)


def _building_dir(root: Path) -> Path:
    """Where the backup is assembled. Readers ignore it: until it is renamed to backup_dir()
    no shard has been touched."""
    return root / "judgments-backup.partial"


def _discard_dir(root: Path) -> Path:
    """Where a committed backup goes to be deleted. Nothing reads it."""
    return root / "judgments-backup.discard"


# Inside the backup: the archive run directory this re-home writes, the other directory it
# changes (remap's claims/) with its snapshot, and what `then` creates (remap's claims archive).
_ARCHIVE_NAME, _ALSO_NAME, _ALSO_DIR = "ARCHIVE", "ALSO", "also-files"
_CREATES_NAME = "CREATES"


def _copy(src: Path, dst: Path) -> None:
    """Copy a shard's exact bytes the way _write() writes one: fsynced, then renamed into place.
    A backup that a power loss left empty would be the only copy of every shard already
    rewritten."""
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(src.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _durable_text(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(d: Path) -> None:
    """Make a directory's entries durable: a rename or unlink is only on disk once its directory
    is, and the transaction's order (backup before rewrite, rewrite before commit) depends on it."""
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _judged_page(cache_root: Path, url: str):
    # The one lookup both sides of a verdict go through: `judged_copy()` when it is recorded
    # and `is_stale()` when it is applied. They used to resolve their roots separately, and did so
    # differently — `vg judge` stamped from the shared cache (data/cache) while the check looked
    # under the candidate dir (data/<candidate>/cache), found no page, and so compared nothing and
    # passed the verdict.
    from .fetch import load_cached

    return load_cached(cache_root, url)


def query_stamp(cache_root: Path, query) -> tuple[int, str]:
    """(query_version, export_date) a verdict on a query citation is about to be recorded
    against: the registry's current definition and the export under `cache_root`. (0, "") for
    a name the registry does not know, which reads as stale."""
    from . import queries

    registered = queries.REGISTRY.get(query.name)
    if registered is None:
        return 0, ""
    return registered.version, queries.export_date(query.name, cache_root)


def unjudgeable_query(query, ran, cache_root: Path) -> str:
    """Why a verdict on this query citation cannot be recorded now, or "". `ran` is the
    `QueryRun` the claim file carries: the run whose context the verifier read.

    The stamp (`query_stamp()`) must describe that run. Stamping the registry's current
    definition over a context an older one produced would make a verdict about the old
    calculation read as current — the exact case versioning exists to catch — and stamping
    another database's export over it would hide that the data differs. So when the run, the
    registry and this root disagree, re-verify first.

    This reads a claim file, and on the trusted side: a forged `query_run` can only get past
    the refusal, and past it is exactly the stamp `vg judge` wrote before this check existed —
    the registry's version and this root's export. It can refuse; it can never grant."""
    version, export = query_stamp(cache_root, query)
    root = Path(cache_root).resolve()
    if (ran is not None and (ran.version, ran.export_date) == (version, export)
            and Path(ran.cache_root).resolve() == root):
        return ""
    now = f"{query.name} is now v{version}{_against(query.name, export)} under {cache_root}"
    if ran is None:
        why = (f"this citation has no recorded query run (it was verified before runs were "
               f"stamped, or its query failed), so nothing says what produced the context you "
               f"judged; {now}")
    else:
        why = (f"this citation was last verified under v{ran.version}"
               f"{_against(query.name, ran.export_date)} under {ran.cache_root}, but {now}, so "
               f"the context you judged is not what it gives here")
    return (f"{why}. Run `vg verify`, then judge it again, with the same --cache for both "
            f"commands.")


def _against(name: str, export_date: str) -> str:
    """ " against <the export>", or "" for a query whose dataset has no exports."""
    from . import queries

    data = queries.dataset(name)
    return f" against {queries.describe_export(export_date, data)}" if data else ""


def _query_stale(j: "Judgment", query) -> str:
    """The query-citation half of `is_stale()`: a verdict about another definition."""
    from . import queries

    registered = queries.REGISTRY.get(query.name)
    if registered is None:
        return f"judged a query ({query.name}) that is no longer registered"
    if j.query_version == registered.version:
        return ""
    if not j.query_version:
        # Recorded before definitions were versioned, so nothing says which calculation it
        # was about — and every registered name has changed meaning at least once. Unknown
        # fails toward re-checking.
        return (f"judged before query definitions were versioned; {query.name} is now "
                f"v{registered.version}")
    # != rather than <: a verdict from a newer checkout is about a definition this one lacks.
    return (f"judged against {query.name} v{j.query_version}; its definition is now "
            f"v{registered.version}")


def older_export(j: "Judgment", cache_root: Path, query) -> str:
    """Why this query verdict was formed against an older export than the database now holds,
    or "". Reported, never applied as staleness. The row can only render green again if the
    query, same definition, still reproduces the claim's `expected` on the new export — the
    same number from the same calculation, which is what the verdict was about. If the figure
    moved, verification fails and the row is not green whatever its verdict; correcting
    `expected` changes the sid, and the verdict lapses with it."""
    from . import queries

    now = queries.export_date(query.name, cache_root)
    if not now or j.export_date >= now:
        return ""
    data = queries.dataset(query.name)   # non-empty: only a dataset with exports has a `now`
    return (f"judged against {queries.describe_export(j.export_date, data)}; the database is "
            f"now {queries.describe_export(now, data)}")


def _which(url: str, source) -> str:
    return "the cited page" if url == source.url else f"the snapshot {url}"


def _moved(judged_url: str, source) -> str:
    """Why `judged_url` is not the page the source's context comes from now, or "".

    That page is `source.context_url`, decided by the run's archive records (apply them
    first), never by the claim file's `context_page`: a hand edit must not be able to choose
    which copy a verdict is checked against."""
    now = source.context_url
    if now is None:
        # apply_archive() leaves no snapshot both where none was recorded and where the
        # recorded one does not hold the cited page (`archive_unusable`).
        return ("its context comes from an archive snapshot, and the run's records name no "
                "usable one, so nothing shows which copy the verdict describes")
    if judged_url != now:
        return (f"judged against {_which(judged_url, source)}; its context now comes from "
                f"{_which(now, source)}")
    return ""


_RECORDED = object()   # judged_copy(): read the copy from the source itself


def judged_copy(source, cache_root: Path, *, seen=_RECORDED) -> tuple[str, str, int]:
    """(page_url, page_fetched_at, extractor_version) to stamp a verdict on `source` with —
    the copy of the page `vg verify` built the verifier's context from. `page_url` is empty when
    that is the cited page (see `_WRITTEN_WHEN_SET`). Raises Unjudgeable when that copy cannot
    be named, is not where the context comes from now, or is no longer the one cached.

    `seen` is that copy as the claim file records it, `source.verification.context_page` by
    default; pass it (None included) for a source whose verification something has rewritten
    since (see `unjudgeable_page()`).

    Stamping whatever was cached at judge time let another run's re-fetch land between
    `vg verify` and `vg judge`: the verdict was stamped from the new copy, read fresh against
    it, and rendered green on text the verifier never saw. And for an archive-verified
    source the context comes from the snapshot, not from the paywall stub at the cited URL,
    which never changes — so a verdict stamped from the stub survived every re-archive.

    `context_page` is read from a claim file loaded trusted. A forged one only gets past the
    refusal to a stamp read off the cached page itself, at a URL `is_stale()` checks against
    where the context comes from: it can refuse, never grant."""
    if seen is _RECORDED:
        seen = source.verification.context_page
    if seen is None:
        raise Unjudgeable(
            f"`vg verify` has recorded no page this source's context came from: it has no "
            f"context to judge (it is {source.verification.status}), or it was verified before "
            f"pages were recorded. Run `vg verify` for this run, then judge the context it gives.")
    if why := _moved(seen.url, source):
        raise Unjudgeable(f"the context you were given no longer describes this source: {why}. "
                          f"Run `vg verify` for this run, then judge the context it gives.")
    page = _judged_page(cache_root, seen.url)
    if page is None:
        # An empty stamp could never go stale, however often the page is re-fetched after.
        raise Unjudgeable(f"{seen.url} is not in the page cache at {cache_root}, so this verdict "
                          f"could not name the copy it judged. Run `vg verify` for this run, or "
                          f"pass the --cache it uses.")
    if (_instant(page.fetched_at), page.extractor_version) != (_instant(seen.fetched_at),
                                                               seen.extractor_version):
        raise Unjudgeable(
            f"the context you were given was built from the copy of {_which(seen.url, source)} "
            f"fetched {seen.fetched_at} (extractor v{seen.extractor_version}), and the cache now "
            f"holds one fetched {page.fetched_at} (v{page.extractor_version}). A verdict stamped "
            f"now would describe text you did not read. Run `vg verify` for this run, then judge "
            f"the context it gives.")
    page_url = "" if seen.url == source.url else seen.url
    return page_url, str(page.fetched_at), page.extractor_version


def unjudgeable_page(source, seen, cache_root: Path) -> str:
    """Why `vg judge` would refuse a verdict on this page citation now, or "": the page
    counterpart of `unjudgeable_query()`. `seen` is the claim file's `context_page`, captured
    before anything rebuilt the verification — `vg judge` reads the file, not a revalidated
    copy. `vg judgments` asks this so its gate never waits on a verdict that cannot be recorded."""
    try:
        judged_copy(source, cache_root, seen=seen)
    except Unjudgeable as e:
        return str(e)
    return ""


def _instant(value) -> datetime | None:
    """A recorded time as an aware datetime, or None if it cannot be read.

    Compared as times, never as text: `str(fetched_at)` writes a space where a hand-written ISO
    time has a `T`, and the two sort wrong against each other. A time with no zone is UTC,
    which is what every writer here uses."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_stale(j: "Judgment", cache_root: Path, source) -> str:
    """Why this verdict no longer describes the cached page, or "" if it still does.

    `cache_root` is the PAGE CACHE root (`_cache_root(data, cache)`), not the data dir the
    verdicts live in: for a candidate run those differ, and checking the wrong one found no
    page and read as fresh.

    Every path that cannot make the comparison answers stale, not fresh: no cached page, an
    unreadable stamp, a legacy verdict with no time at all. Fresh has to be shown.

    The page compared is the one the verdict judged, and it must be the one the source's
    context comes from now (`Source.context_url`): a re-archive that replaces an archive-verified
    row's snapshot, or a row that moves between the cited page and a snapshot, is stale however
    the times compare. Run `verify.apply_archive()` on the source first."""
    if source.query is not None:
        # A query citation has no page by design, so there is nothing here to compare.
        # What it judged is the query: a verdict formed under another definition is stale
        # (_query_stale). Its result is re-run at every build, and a result that no longer
        # matches `expected` discards the source whatever the verdict says.
        return _query_stale(j, source.query)
    if not j.page_url and source.verification.status == "could_not_verify_paywall":
        # Stamped from the cited page, on a row whose live page is gated now. `vg judge` only
        # stamps a copy a context came from, and a paywalled row has none, so this verdict is
        # either from before verdicts named their page — possibly about a snapshot the row was
        # verified against then and is not now — or about a page since re-fetched behind a
        # paywall. The stub never changes, so no time comparison can tell the first case.
        # Not re-judgeable as it stands (`vg judge` refuses a row with no context), so the
        # reason says what is: the row needs a context back first.
        return ("judged against the cited page, which is now paywalled: it may be about a "
                "snapshot that no longer backs this row, and nothing can show which. Nothing "
                "can be judged here until `vg archive` or `vg verify` gives the row a context")
    # Empty for the cited page: `vg judge` names only a snapshot (see _WRITTEN_WHEN_SET), and
    # a verdict from before `page_url` existed was stamped from the cited page too.
    judged_url = j.page_url or source.url
    if why := _moved(judged_url, source):
        return why
    page = _judged_page(cache_root, judged_url)
    if page is None:
        # The page is deleted, unparseable, or under a different root (a mistyped --cache). Any
        # of those used to apply the verdict with no comparison behind it.
        return (f"judged against a page that is not in the cache at {cache_root}, so nothing "
                f"shows the verdict still describes it")
    # Against the page's own version, not the current extractor's: a page kept after a failed
    # re-fetch (fetch.py) stays under its older version with its text unchanged, and a verdict
    # about that text still describes it. Comparing to EXTRACTOR_VERSION dropped such verdicts
    # forever — each new one is stamped with the page's version and was stale on arrival.
    # A backstop, deliberately: a real re-extraction also moves fetched_at, caught below.
    if j.extractor_version and j.extractor_version < page.extractor_version:
        return (f"judged against extractor v{j.extractor_version}; the page has since been "
                f"re-extracted (v{page.extractor_version})")
    cached = _instant(page.fetched_at)
    if j.page_fetched_at:
        judged = _instant(j.page_fetched_at)
        if judged is None:
            return f"its recorded page time {j.page_fetched_at!r} cannot be read"
        if cached > judged:
            return (f"judged against a copy fetched {j.page_fetched_at}; the page was "
                    f"re-fetched since")
        if cached < judged:
            # Only a replaced cache gets here (a merged stray, a restored backup): the bytes
            # judged are gone either way, and an older copy is no more the one judged.
            return (f"judged against a copy fetched {j.page_fetched_at}; the cache now holds an "
                    f"older copy, fetched {page.fetched_at}")
        return ""
    # A legacy verdict, recorded before stamping existed. Its judgment time still bounds what
    # it saw: every fetch stamps its page with the time the fetch started, so a page fetched no
    # later than the verdict is the copy that was cached when it was judged. One fetched later
    # is not. Treating every legacy verdict as stale would re-judge a whole run to learn
    # nothing; treating them all as fresh is the fail-open this replaces. What it cannot see
    # is a re-fetch already in flight when the verdict was recorded (started before, written
    # after). New verdicts are stamped from the page itself, so only old ones carry that gap.
    judged = _instant(j.judged_at)
    if judged is None:
        return "recorded with no page stamp and no readable time, so nothing ties it to a copy"
    # `judged_at` is written to the second, so compare at that precision: a page fetched
    # earlier in the same second would otherwise read as fetched after the verdict.
    if cached.replace(microsecond=0) > judged:
        return (f"judged {j.judged_at}, before verdicts were stamped; the page was re-fetched "
                f"since ({page.fetched_at})")
    return ""


def verdicts_for(claim, root: Path, judged: dict[str, Judgment] | None = None, *,
                 cache_root: Path):
    """Yield (source, recorded judgment or None, why it is stale or "") for each source.

    Pass `judged` to reuse a question's already-loaded verdicts.

    `root` is where the verdicts live (the candidate's data dir); `cache_root` is where the
    pages they judged live. Required, and keyword-only, because defaulting it to `root` is
    exactly the bug that hid every stale verdict in a candidate run.
    """
    if judged is None:
        judged = load(root, claim.question_id)
    for s in claim.sources:
        j = judged.get(s.sid)
        yield s, j, (is_stale(j, cache_root, s) if j is not None else "")


def apply_to(claim, root: Path, *, cache_root: Path) -> list[str]:
    """Merge recorded judgments onto a claim's sources. Returns reasons for any dropped as
    stale — a verdict about text that no longer exists is not a verdict about this source."""
    return merge(verdicts_for(claim, root, cache_root=cache_root))


def merge(verdicts) -> list[str]:
    """`apply_to()`'s body, for a caller that also needs the `verdicts_for()` rows it merged.

    `vg judgments` merges through here and then runs build's other offline checks, so its
    count is what `vg build` renders rather than a re-derivation of it: an earlier count
    trusted any recorded verdict, and read 0 while build still showed stale ones as pending.
    """
    stale: list[str] = []
    for s, j, why in verdicts:
        if j is None or why:
            # Reset to unreviewed, not merely skipped: `vg build` loads claim files trusting
            # machine fields, so a `support` already sitting in the file — hand- or
            # agent-written, or left from before verdicts moved out — would otherwise render
            # as judged. A verdict comes from data/judgments/ or not at all, and unjudged
            # fails toward re-checking instead of toward shipping.
            s.verification.support = "unreviewed"
            s.verification.support_note = None
            if why:
                stale.append(f"{s.sid}: {why}")
            continue
        s.verification.support = j.verdict
        s.verification.support_note = j.note
    return stale
