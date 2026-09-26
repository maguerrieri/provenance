"""Verifier judgments, stored apart from the claims they judge.

They cannot live in the claim file. `provenance verify` reloads claim files with
`strip_machine_fields()` on — which is what stops a researcher marking its own citation
verified — so a verdict written there is destroyed by the next verify run. In the smoke test
the orchestrator had to hand-sequence every verify before every writeback, and the verdicts
still had no effect on status.

One shard per question, keyed within it by source id (url + snippet), so a judgment follows
the citation it was about and lapses on its own when a retry changes the quote. The answer is
the other half of what was judged, so a verdict applies only while its claim still asks and
answers what it judged (`claim_fingerprint`; see `_claim_stale()`). A verdict is
about one question's claim, not about the source alone: two questions citing the same page
each get their own verdict, and the two can differ — a snippet can support one claim and be
`topic_only` for another. Nothing here pools verdicts across questions by sid.

A sid covers the quote, not what the claim says about it, so each verdict also names the claim
it judged (`claim_fingerprint`): a retry that later rewrites that claim's question or answer
leaves the verdict naming words the claim no longer says.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import time
from contextlib import contextmanager
from dataclasses import MISSING, asdict, dataclass, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints

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
    # Which claim it judged: `Claim.fingerprint` of the claim `provenance judge` was given, its question
    # and answer. A retry that rewrites either keeps the sid, and so the verdict, while the
    # words it judged are gone; this is what shows it, and a verdict whose fingerprint is not
    # the claim's is stale (`_claim_stale()`). Empty on a verdict recorded before it existed,
    # which is stale too.
    claim_fingerprint: str = ""


class UnreadableJudgments(ValueError):
    """A verdict file, or the judgments directory, exists but cannot be read as verdicts.

    A ValueError, so `provenance judge`'s existing handler already reports it. A subclass, so every
    other command can stop on exactly this without also catching unrelated ValueErrors (a
    pydantic ValidationError is one) and presenting a bug as a data problem.
    """


class Unjudgeable(ValueError):
    """A verdict on this source cannot be recorded now, because nothing would tie it to the
    copy of the page the verifier read. `provenance judge` refuses with the message and writes nothing."""


_TYPES = get_type_hints(Judgment)
_REQUIRED = {f.name for f in fields(Judgment)
             if f.default is MISSING and f.default_factory is MISSING}
if not all(isinstance(t, type) for t in _TYPES.values()):
    # _entry_problems checks each field against an exact class. An Optional, union or generic
    # field would fail every entry, or crash on `t.__name__` inside load(): give it its own
    # check there before adding it here.
    raise TypeError(f"Judgment fields must be plain classes, got {_TYPES}")
# What Source.sid makes: the first 12 hex characters of a sha1. Anything else — empty,
# padded, a typo — can never match a cited source, so it would read as a lapsed verdict: a
# verdict someone recorded, silently judging nothing.
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
    # One shape for both: each is `models.short_id()`. A malformed fingerprint would never match
    # its claim, and read as a claim rewritten since it was judged. It may be empty (unstamped).
    for key, what in (("sid", "source id"), ("claim_fingerprint", "claim fingerprint")):
        value = item.get(key)
        if isinstance(value, str) and (value or key == "sid") and not _SID.fullmatch(value):
            problems.append(f"{key} {value!r} is not a {what} (12 lowercase hex characters)")
    if isinstance(verdict, str) and verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    if isinstance(sid, str) and sid in seen:
        # Keyed by sid, so one of the two would be dropped — and deleted by the next rewrite.
        problems.append("a second verdict for a source this file already judged")
    return problems


def path_for(root: Path, question_id: str) -> Path:
    """The shard for a question id — refusing any id that would name a file elsewhere.

    The claim schema constrains question ids, but only for values that pass through it, and
    `provenance judge` takes its id straight from the command line, where verifier agents (which have
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
    """Where a re-home by the retired `vg remap` kept every shard as it was while it rewrote
    them. Nothing writes it now, so one that exists was left by an interrupted re-home."""
    return root / "judgments-backup"


def archive_dir(root: Path) -> Path:
    """Where verdicts taken out of the live shards are kept, one directory per run: clear()
    keeps a contradiction a human cleared here, with the reason."""
    return root / "judgments-archive"


# Every holder keeps the lock for one shard read or one rewrite: milliseconds. Waiting longer
# than this means a holder is stuck, and verifier agents blocked on it forever would report
# nothing — which the judgment pass reads as nothing to report.
LOCK_TIMEOUT = 60.0


@contextmanager
def _lock(root: Path, *, shared: bool = False):
    """Hold the judgments directory while reading or rewriting it.

    record() reads a shard and then rewrites it, and verifier agents record in parallel, so a
    second `provenance judge` that landed in between was never read, and the rewrite deleted it. Writers
    take the lock exclusively. Readers take it shared, so they read after a write in progress
    rather than before it; each shard is replaced whole (_write()), so none reads half of one.
    It is an flock on the directory itself: no lock file to litter a tracked data dir, and a
    holder that dies releases it with its process.
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
                            f"{d} has been locked by another provenance process for over "
                            f"{LOCK_TIMEOUT:.0f}s. A verdict write holds it for "
                            f"milliseconds, so that process is stuck: stop it and re-run."
                        ) from None
                    time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            os.close(fd)


# A commit on main whose `vg judgments --rollback` undoes an interrupted re-home. Named outright,
# and in full: a lookup such as `git log -S'def rollback('` finds whatever commit last touched
# that text, and an abbreviated id can become ambiguous as the repository grows. It predates the
# rename to `provenance` (#6), so its command is `vg`, and refuse_if_interrupted() names every
# command that runs there as `vg`.
LAST_WITH_ROLLBACK = "6f73eac879f5ce54a196436c781a8939217c5154"


def leftovers(root: Path) -> list[Path]:
    """Scratch an interrupted re-home by the retired `vg remap` left that no reader uses: a
    backup still being built, which had touched no shard, or one already retired after its
    re-home finished. The next re-home used to clear them. Now nothing does, so `provenance judgments`
    names them, and neither is ever to be restored from."""
    return [d for d in (root / "judgments-backup.partial", root / "judgments-backup.discard")
            if d.exists()]


def refuse_if_interrupted(root: Path) -> None:
    """Raise if a re-home by the retired `vg remap` or `vg judgments --repair` was interrupted.
    A backup left behind means a rewrite stopped partway: some shards may already hold their new
    contents and others not. Reading that as the verdicts would render whatever is missing as
    unreviewed, which is the silent loss the backup exists to prevent. The command that undoes
    it retired with them, so the message names a commit that still has it, and the run by its
    absolute path, since the command runs from that other checkout. The path is quoted for the
    shell: an operator pastes it, and a space in it would split `--data` in two."""
    b = backup_dir(root)
    try:
        b.stat()
    except FileNotFoundError:
        return
    except OSError as e:
        # exists() would read this as "no backup" (3.13+), and the shards as whole.
        raise UnreadableJudgments(f"cannot tell whether {b} exists: {e}.") from None
    raise UnreadableJudgments(
        f"{b} exists, so a re-home of {root / 'judgments'} by the retired `vg remap` or `vg "
        f"judgments --repair` was interrupted, and its shards may be half-rewritten. This "
        f"version cannot undo it: run `vg judgments --rollback --data "
        f"{shlex.quote(str(root.resolve()))}` from a checkout of commit {LAST_WITH_ROLLBACK}, "
        f"which still has it. It puts back everything the re-home changed: the verdicts, and the "
        f"claim files and questions.json if it was `vg remap --apply`. Then, from that checkout, "
        f"re-run the command that was interrupted to finish it, since this version cannot apply a "
        f"migration either.")


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
    # and record() rewrites the file from what this returns, so it is also deleted.
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

    Reads every shard before returning anything, so an unreadable one stops the caller before
    it reports on any, and a caller that also needs the verdicts (`provenance judgments`) gets them
    from the same read.
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
# same way: `provenance judge` sets it only for a snapshot, which older code would check against the
# paywall stub, and leaves it empty for the cited page, which older code checks right.
# `claim_fingerprint` is on every verdict `provenance judge` records now, so older code refuses those
# shards, which is the safe side: it could not tell that the claim was rewritten since. A shard
# holding only older verdicts stays one older code reads.
_WRITTEN_WHEN_SET = ("query_version", "export_date", "page_url", "claim_fingerprint")


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
           query_version: int = 0, export_date: str = "", page_url: str = "",
           claim_fingerprint: str = "") -> Judgment:
    """Write one verdict into `question_id`'s shard, replacing any earlier one for `sid`.

    `claim_fingerprint` is the judged claim's `Claim.fingerprint`. `provenance judge` always passes it;
    left empty, the verdict reads like one recorded before fingerprints existed."""
    j = Judgment(sid=sid, verdict=verdict, note=note,
                 page_fetched_at=page_fetched_at, extractor_version=extractor_version,
                 query_version=query_version, export_date=export_date, page_url=page_url,
                 claim_fingerprint=claim_fingerprint,
                 judged_at=datetime.now(UTC).isoformat(timespec="seconds"))
    # The writer holds itself to the reader's rule: an entry load() refuses would stop every
    # command that reads this file until someone repaired it by hand.
    if why := _entry_problems(asdict(j), set()):
        raise ValueError(f"refusing to record a verdict that could not be read back: "
                         f"{' and '.join(why)}")
    p = path_for(root, question_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Read and rewrite under one lock: another record() between the two would otherwise be
    # undone.
    with _lock(root):
        refuse_if_interrupted(root)
        existing = _read(p)
        existing[sid] = j
        _write(p, existing.values())
    return j


def dropped(claim, judged: dict[str, Judgment]) -> list:
    """The `contradicts` verdicts in a claim's shard on a source the claim no longer cites.

    Such a verdict does not lapse with its sid as the others do. It says the record argues
    against the claim, and a retry that drops the source takes that off the review page
    without resolving it: the claim rendered `verified` on the sources that agree. So it holds
    the claim (`Claim.dropped_contradictions`) until the source is cited again, which applies
    it as usual, or a human clears it (`clear()`).

    "No longer cites" is by sid, which covers url and snippet: a retry that re-quotes the same
    page holds the claim too, since a new quote can be a more agreeable passage of a page that
    argued against it. A verdict filed under the claim before `provenance judge` checked that it cites
    the source is held the same way. Each fails toward review, where a human can clear it.

    Never checked for staleness: that compares a verdict with the source it judged, which the
    claim no longer carries, and `provenance judge` cannot re-judge a source nothing cites. Holding the
    claim in review is the direction to fail in."""
    from .models import DroppedContradiction

    cited = {s.sid for s in claim.sources}
    return [DroppedContradiction(sid=sid, note=j.note or "", judged_at=j.judged_at)
            for sid, j in judged.items() if j.verdict == "contradicts" and sid not in cited]


def clear(root: Path, question_id: str, sid: str, reason: str, *, shown=None) -> Path:
    """Take one `contradicts` verdict out of the live shard on a human's word, and keep it under
    archive_dir() with the reason beside it. Returns the directory it went to.

    For a contradiction a retry dropped (`dropped()`): the caller checks that the claim no
    longer cites the source. One on a cited source is the claim's own evidence disagreeing, and
    stays live. `shown` is the `DroppedContradiction` the person was shown: a verdict re-judged
    since then is refused, since the reason was given for the one they read.

    The archive is complete or absent: built beside its name with the verdict and the reason,
    made durable, renamed into place, and only then is the shard rewritten. If that rewrite
    fails, the shard still holds the verdict and the archive is taken back out, so a re-run
    leaves one copy. Only a kill between the two leaves the verdict in both places, where the
    live copy still holds the claim. Never in neither."""
    if not reason.strip():
        raise ValueError("a clearance needs a reason: it is what the record keeps in place of "
                         "the verdict")
    p = path_for(root, question_id)
    with _lock(root):
        refuse_if_interrupted(root)
        shard = _read(p)
        j = shard.get(sid)
        if j is None:
            raise ValueError(f"{p} holds no verdict for source {sid}")
        if j.verdict != "contradicts":
            raise ValueError(f"the verdict for source {sid} in {p} is {j.verdict}, not "
                             f"contradicts: a lapsed {j.verdict} holds nothing to clear")
        if shown is not None and (j.note or "", j.judged_at) != (shown.note, shown.judged_at):
            raise ValueError(f"the verdict for source {sid} in {p} was judged again (at "
                             f"{j.judged_at}) after it was shown: run the command again to read "
                             f"it before clearing it")
        stamp = _stamp()
        dest = archive_dir(root) / stamp
        building = dest.with_name(f".{stamp}.partial")
        placed = False
        try:
            _archive(building, {question_id: {sid: j}})
            now = datetime.now(UTC).isoformat(timespec="seconds")
            _durable_text(building / _CLEARED_NAME,
                          f"{question_id}/{sid} cleared {now}: {reason.strip()}\n")
            _fsync_dir(building)
            os.replace(building, dest)
            placed = True
            # The whole path to it, since mkdir may have made judgments-archive/ just now: a
            # power loss that kept the shard rewrite and not the archive's entry would lose it.
            for d in (dest.parent, root):
                _fsync_dir(d)
        except BaseException:
            # The shard is untouched, so the archive goes, wherever it had got to.
            shutil.rmtree(dest if placed else building, ignore_errors=True)
            raise
        del shard[sid]
        try:
            if shard:
                _write(p, shard.values())
            else:
                p.unlink()          # an empty shard is no file, as for a claim never judged
        except BaseException:
            # Asked of the shard, not assumed: the archive goes only if the verdict is still live.
            try:
                live = sid in _read(p)
            except Exception:   # noqa: BLE001 — unreadable: keep the archive
                live = False
            if live:
                shutil.rmtree(dest, ignore_errors=True)
            raise
        _fsync_dir(p.parent)
    return dest


def _stamp() -> str:
    """A fresh archive run directory's name: the time, to the microsecond."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _archive(dest: Path, orphans: dict[str, dict[str, Judgment]]) -> None:
    """Keep verdicts no current claim cites, under the shard name they had. A fresh directory
    per run, so an earlier run's archive is never merged into or overwritten."""
    dest.mkdir(parents=True)
    for qid, items in sorted(orphans.items()):
        _write(dest / f"{qid}.json", items.values())
    _fsync_dir(dest)


# Inside an archive run directory clear() writes: which verdict a human cleared, and why.
_CLEARED_NAME = "CLEARED"


def _durable_text(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(d: Path) -> None:
    """Make a directory's entries durable. A rename or unlink is only on disk once its directory
    is, and every order this module keeps on disk depends on that, such as clear()'s archive
    before its shard rewrite."""
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _on_disk(root: Path, stem: str) -> Path:
    """A shard found by listing judgments/, addressed by the name it has. It is inside the
    directory by construction, and path_for() would refuse a name from before ids were checked,
    stopping a command over a file that escapes nothing."""
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
    `provenance judgments` asks it, so it counts a shard for the claim `provenance build` opens it as.
    """
    same = [q for q in question_ids
            if q != stem and q.casefold() == stem.casefold() and _same_file(root, stem, q)]
    return same[0] if len(same) == 1 else None


def _judged_page(cache_root: Path, url: str):
    # The one lookup both sides of a verdict go through: `judged_copy()` when it is recorded
    # and `is_stale()` when it is applied. They used to resolve their roots separately, and did so
    # differently — `provenance judge` stamped from the shared cache (data/cache) while the check looked
    # under the subject's dir (data/<subject>/cache), found no page, and so compared nothing and
    # passed the verdict.
    from .fetch import load_cached

    return load_cached(cache_root, url)


def query_stamp(cache_root: Path, query) -> tuple[int, str]:
    """(query_version, export_date) now: the registry's current definition and the export
    under `cache_root`. (0, "") for a name the registry does not know, which reads as stale.
    What a claim's run must match (`unjudgeable_query()`) before `provenance judge` stamps that run:
    judge stamps the run it checked, never a second read of this, which a rebuild between the
    two could move."""
    from . import queries

    registered = queries.REGISTRY.get(query.name)
    if registered is None:
        return 0, ""
    return registered.version, queries.export_date(query.name, cache_root)


def unjudgeable_query(query, ran, cache_root: Path) -> str:
    """Why a verdict on this query citation cannot be recorded now, or "". `ran` is the
    `QueryRun` the claim file carries: the run behind the context it holds now. That is not
    always the one the verifier read, since a re-verify can replace it while the verifier
    works; the context token (`context_token()`) is what ties the verdict to that one.

    `provenance judge` stamps the verdict with this run, so it must match the registry and this root.
    A context an older definition produced, stamped with the current one, would make a verdict
    about the old calculation read as current — the exact case versioning exists to catch —
    and one from another database's export would hide that the data differs. So when the run,
    the registry and this root disagree, re-verify first.

    This reads a claim file, and on the trusted side: a forged `query_run` can only get past
    the refusal, and past it is exactly the stamp `provenance judge` wrote before this check existed —
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
    return (f"{why}. Run `provenance verify`, then judge it again, with the same --cache for both "
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
    the copy of the page `provenance verify` built the verifier's context from. `page_url` is empty when
    that is the cited page (see `_WRITTEN_WHEN_SET`). Raises Unjudgeable when that copy cannot
    be named, is not where the context comes from now, or is no longer the one cached.

    `seen` is that copy as the claim file records it, `source.verification.context_page` by
    default; pass it (None included) for a source whose verification something has rewritten
    since (see `unjudgeable_page()`).

    Stamping whatever was cached at judge time let another run's re-fetch land between
    `provenance verify` and `provenance judge`: the verdict was stamped from the new copy, read fresh against
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
            f"`provenance verify` has recorded no page this source's context came from: it has no "
            f"context to judge (it is {source.verification.status}), or it was verified before "
            f"pages were recorded. Run `provenance verify` for this run, then judge the context it gives.")
    if why := _moved(seen.url, source):
        raise Unjudgeable(f"the context you were given no longer describes this source: {why}. "
                          f"Run `provenance verify` for this run, then judge the context it gives.")
    page = _judged_page(cache_root, seen.url)
    if page is None:
        # An empty stamp could never go stale, however often the page is re-fetched after.
        raise Unjudgeable(f"{seen.url} is not in the page cache at {cache_root}, so this verdict "
                          f"could not name the copy it judged. Run `provenance verify` for this run, or "
                          f"pass the --cache it uses.")
    if (_instant(page.fetched_at), page.extractor_version) != (_instant(seen.fetched_at),
                                                               seen.extractor_version):
        raise Unjudgeable(
            f"the context you were given was built from the copy of {_which(seen.url, source)} "
            f"fetched {seen.fetched_at} (extractor v{seen.extractor_version}), and the cache now "
            f"holds one fetched {page.fetched_at} (v{page.extractor_version}). A verdict stamped "
            f"now would describe text you did not read. Run `provenance verify` for this run, then judge "
            f"the context it gives.")
    page_url = "" if seen.url == source.url else seen.url
    return page_url, str(page.fetched_at), page.extractor_version


def unjudgeable_page(source, seen, cache_root: Path) -> str:
    """Why `provenance judge` would refuse a verdict on this page citation now, or "": the page
    counterpart of `unjudgeable_query()`. `seen` is the claim file's `context_page`, captured
    before anything rebuilt the verification — `provenance judge` reads the file, not a revalidated
    copy. `provenance judgments` asks this so its gate never waits on a verdict that cannot be recorded."""
    try:
        judged_copy(source, cache_root, seen=seen)
    except Unjudgeable as e:
        return str(e)
    return ""


@dataclass(frozen=True)
class HandedRun:
    """The run a query citation's context came from, as `provenance handoff` prints it."""
    name: str
    version: int
    dataset: str       # "" for a query whose data has no exports
    export_date: str   # "" for an undated database
    cache_root: str    # resolved, as the run check (`unjudgeable_query()`) compares it


@dataclass(frozen=True)
class HandedContext:
    """What `provenance handoff` prints for a source there is something to judge on."""
    text: str
    query_run: HandedRun | None   # a query citation's run; None for a page


@dataclass(frozen=True)
class HandedSource:
    """One source as `provenance handoff` prints it."""
    sid: str
    status: str
    publisher: str
    author: str
    date: str | None
    source_type: str
    tier: str                       # `sources.TIER_LABEL`: what the type carries on this host
    url: str
    page: int | None
    snippet: str
    context: HandedContext | None   # None: nothing to judge on it yet
    unjudgeable: str                # why, when `context` is None


@dataclass(frozen=True)
class Handoff:
    """Everything `provenance handoff` prints for one claim, as one value (`cli._handed()` builds it).
    The printer reads nothing else and `context_token()` hashes it, so a field the hand-off
    prints is one the token covers without anyone listing it."""
    question_id: str
    claim_type: str
    required_sources: int
    question: str
    answer: str
    sources: tuple[HandedSource, ...]


# What the token leaves out of the hand-off, by name; everything else is in it. The ids `provenance judge`
# takes as arguments and checks itself: the question id here, and the judged source's sid in
# `context_token()`. The other sources' sids are in it: a query citation's covers the figure it
# asserts, which nothing else printed does. The status and the reason a source has nothing to
# judge are the pipeline's account of the source, not evidence; whether it has a context to
# judge is in the token, as its `context`. The reason also names the cache root as spelled, so
# one database under two spellings would read as two.
_NOT_HASHED = {Handoff: {"question_id"}, HandedSource: {"status", "unjudgeable"}}


def _hashed(value):
    """`value` as plain JSON data, minus `_NOT_HASHED`."""
    if isinstance(value, tuple):
        return [_hashed(v) for v in value]
    if not is_dataclass(value):
        return value
    left_out = _NOT_HASHED.get(type(value), set())
    return {f.name: _hashed(getattr(value, f.name)) for f in fields(value)
            if f.name not in left_out}


def context_token(handed: Handoff, sid: str) -> str:
    """A short fingerprint of the hand-off a verifier was given to judge source `sid` from: all
    of `handed` but what `_NOT_HASHED` names and that source's own sid, and which source's block
    it was printed beside (`[n/N]`). "" when the hand-off has nothing to judge on that source.
    `provenance judge --context` must hand it back.

    `provenance judge` reads the claim file as it is when judge runs, and nothing from the verifier said
    what it had read. So a re-verify landing while a verifier worked (another run re-fetched the
    page, and this one rebuilt the context from the new copy) left judge a copy it could stamp
    and a context no verifier had seen, and the row rendered green on it. The token is how the
    verifier says which hand-off it read. Longer than a sid, so the two are not mistaken for
    each other.

    It covers the hand-off as a whole, never a list of its fields. It used to list them, and
    four fixes each found one the list had missed: the claim and the citation (a retry that
    rewrote only the answer, or only a filing's date, kept the sid and the context), a query's
    run (a re-run under a new definition prints a context that reads the same), the claim type
    (it sets what the verifier is asked), and the claim's other sources (an adversarial claim's
    verifier judges them together, so a retry that swapped one for a reprint of the other carried
    an independence verdict onto a pair no verifier saw). Every source's block is in every
    token, so any change to one refuses an outstanding verdict on each.

    Serialized as JSON with sorted keys, where every string is quoted and escaped and every list
    bracketed, so each field's extent is explicit. The fields used to be joined with NUL, which
    json.loads keeps inside a string: text moved from a publisher into its author, across the
    separator, left the token as it was. ensure_ascii also escapes a lone surrogate, which
    json.loads keeps and strict UTF-8 refuses to encode."""
    n = next((n for n, s in enumerate(handed.sources, 1) if s.sid == sid), None)
    if n is None or handed.sources[n - 1].context is None:
        return ""
    hashed = _hashed(handed)
    del hashed["sources"][n - 1]["sid"]   # the argument `provenance judge` found this block by
    shown = json.dumps({"handoff": hashed, "judged": n}, sort_keys=True, ensure_ascii=True,
                       allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(shown.encode("ascii")).hexdigest()[:16]


def wrong_context(handed: Handoff, sid: str, token: str, *, handoff: str) -> str:
    """Why a verdict on `sid` handed back with `token` is not about the hand-off `handed` gives
    now, or "". Every verdict must carry a token: a query citation's too, since the run check
    (`unjudgeable_query()`) ties it only to the run on disk when judge runs, not to the one the
    verifier read. `handoff` is the `provenance handoff` command, with the run's --data and --cache,
    that prints what to judge.

    The token is agent-supplied and the hand-off is built from a claim file loaded trusted, and
    both are safe for the same reason: this can refuse, never grant. A forged token matching
    the current hand-off gets exactly what `provenance judge` recorded before tokens existed, and any
    other blocks the verdict. So the refusal never prints the current token: a verifier handed
    one could retry with it and record a verdict about text it has not read."""
    token = token.strip().lower()
    if not token:
        return (f"a verdict must name the context it judged: pass --context with the context "
                f"token `{handoff}` printed beside this source")
    source = next(s for s in handed.sources if s.sid == sid)
    if source.context is None:   # no token matches, and re-reading the hand-off won't give one
        return f"`{handoff}` has nothing to judge on this source: {source.unjudgeable}"
    if token != context_token(handed, sid):
        run = ("" if source.context.query_run is None else
               ", its query run (a re-run under another definition, export or database changes "
               "the token even where its result reads the same)")
        return (f"you were handed a different hand-off (token {token}) from the one this claim "
                f"gives now: the claim, this source's citation or context{run}, or another "
                f"source printed with it has changed since, so your verdict is about what the "
                f"pipeline no longer shows. Run `{handoff}` again, read what it prints, and "
                f"judge that")
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
    verdicts live in: for a subject's run those differ, and checking the wrong one found no
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
        # Stamped from the cited page, on a row whose live page is gated now. `provenance judge` only
        # stamps a copy a context came from, and a paywalled row has none, so this verdict is
        # either from before verdicts named their page — possibly about a snapshot the row was
        # verified against then and is not now — or about a page since re-fetched behind a
        # paywall. The stub never changes, so no time comparison can tell the first case.
        # Not re-judgeable as it stands (`provenance judge` refuses a row with no context), so the
        # reason says what is: the row needs a context back first.
        return ("judged against the cited page, which is now paywalled: it may be about a "
                "snapshot that no longer backs this row, and nothing can show which. Nothing "
                "can be judged here until `provenance archive` or `provenance verify` gives the row a context")
    # Empty for the cited page: `provenance judge` names only a snapshot (see _WRITTEN_WHEN_SET), and
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


def _claim_stale(j: "Judgment", fingerprint: str) -> str:
    """Why this verdict is not about the claim's question and answer as they read now, or "".
    `fingerprint` is that claim's `Claim.fingerprint`.

    A verdict judges a page against one answer, and the sid covers only the page's half: a retry
    that rewrote the answer and kept the url and snippet kept the verdict too. A `supports` about
    "voted for" then vouched for "voted against", and a `contradicts` held a corrected claim in
    review over an answer it no longer gives.

    A legacy verdict, recorded before verdicts named their claim, is stale. The page rule's
    exception does not carry over: it bounds a legacy verdict by `judged_at` against the time
    every fetch stamps on its page, and nothing records when an answer was written.
    `checked_at` moves on every `provenance verify`, and a claim file's mtime on every write-back and
    checkout. With no bound, it is the query rule instead: unknown fails toward re-checking."""
    if not j.claim_fingerprint:
        return ("recorded before verdicts named the claim they judged, so nothing shows it is "
                "about this question and answer")
    if j.claim_fingerprint != fingerprint:
        # Or a misfiled verdict, one another claim earned, which says as little about this
        # claim's answer.
        return ("judged another question or answer than this claim gives now: a retry or hand "
                "edit rewrote the claim since, or the verdict is filed under another claim's id")
    return ""


def verdicts_for(claim, root: Path, judged: dict[str, Judgment] | None = None, *,
                 cache_root: Path):
    """Yield (source, recorded judgment or None, why it is stale or "") for each source.

    Pass `judged` to reuse a question's already-loaded verdicts.

    `root` is where the verdicts live (the subject's data dir); `cache_root` is where the
    pages they judged live. Required, and keyword-only, because defaulting it to `root` is
    exactly the bug that hid every stale verdict in a subject's run.

    A verdict is stale when it judged another question or answer (`_claim_stale()`, checked
    here because this is where the claim is in hand) or another copy of the page or query
    (`is_stale()`). Both halves are reported: re-judging needs the page half settled too, and
    `provenance judge` refuses a page it cannot stamp.
    """
    if judged is None:
        judged = load(root, claim.question_id)
    fingerprint = claim.fingerprint
    for s in claim.sources:
        j = judged.get(s.sid)
        why = ("; ".join(filter(None, (_claim_stale(j, fingerprint), is_stale(j, cache_root, s))))
               if j is not None else "")
        yield s, j, why


def apply_to(claim, root: Path, *, cache_root: Path) -> list[str]:
    """Merge recorded judgments onto a claim's sources. Returns reasons for any dropped as
    stale — a verdict about text that no longer exists is not a verdict about this source."""
    return merge(verdicts_for(claim, root, cache_root=cache_root))


def merge(verdicts) -> list[str]:
    """`apply_to()`'s body, for a caller that also needs the `verdicts_for()` rows it merged.

    `provenance judgments` merges through here and then runs build's other offline checks, so its
    count is what `provenance build` renders rather than a re-derivation of it: an earlier count
    trusted any recorded verdict, and read 0 while build still showed stale ones as pending.
    """
    stale: list[str] = []
    for s, j, why in verdicts:
        if j is None or why:
            # Reset to unreviewed, not merely skipped: `provenance build` loads claim files trusting
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
