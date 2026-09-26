"""A project: the directory holding a provenance.toml, and what that file declares.

The file names the project, the source lists its citations are checked against, where its
shared cache lives and its subjects (each a subdirectory holding a run of its own). It also holds
what every researcher is told (`context`) and what no researcher is told (`completeness_check`).
Every command finds it the same way: the nearest provenance.toml at or above the run directory
(`--data`, else the working directory), or the directory `--project` names.

This replaced inference. The pipeline used to decide where the cache lived by asking whether
a parent directory held a question set, and whether a cache/ directory existed here or there.
A rule keyed on what happens to exist fulfils itself, since whatever first creates the
directory gets to pick: one stray fetch forked a subject's cache, and a staleness check that
read the wrong cache created that stray as a side effect. And some layouts looked alike to it:
a self-contained data root nested under another, and a subject scaffolded before its parent
had a question set. Now nothing is resolved from which cache or output directories exist. A
directory with no project file above it is refused, never adopted: moving a project laid out
the old way is a reviewed change (README, "Projects"), not something a command does silently.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

FILE = "provenance.toml"
KEYS = ("name", "title", "sources", "cache", "subjects", "context", "completeness_check")
SUBJECT_KEYS = ("id", "name")

# The heading that opened a race file's completeness check. Pasted into `context` with the rest
# of a race file's body, it would carry the known answers into every researcher's prompt.
_CHECK_HEADING = re.compile(r"^#+[ \t]*completeness check\b", re.IGNORECASE | re.MULTILINE)

# A subject is a directory name under the project root: no separator, no leading dot, so it
# is always a direct child. The names a run keeps inside itself are refused, since a subject
# called "claims" would be the root run's claims directory, and so are a cache directory's,
# since with `cache = "."` a subject called "pages" would read as the cache itself.
SUBJECT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
RESERVED = frozenset({"archives.json", "cache", "calaccess", "claims", "claims-archive",
                      "judgments", "judgments-archive", "judgments-backup",
                      "judgments-backup.partial", "judgments-backup.discard", "out", "pages",
                      "provenance.toml", "questions.json"})


class ProjectError(ValueError):
    """No project to be found, a project file that can't be read, or a run the project does
    not declare. Never read as "no project, so infer one": that is the rule this replaced."""


class UnreadableProject(ProjectError):
    """A project file that exists and can't be read as a project. `root` is the directory
    holding it, for a caller that has to act on the project's runs anyway (`lists_run()`)."""

    def __init__(self, message: str, root: Path):
        super().__init__(message)
        self.root = root


@dataclass(frozen=True)
class Subject:
    """One of a project's separate runs. Not necessarily a person: a proposal, a document, or
    one version of either. Two versions of one proposal are two subjects, each with its own id,
    run and review progress."""
    id: str      # its directory under the project root, and what --subject names
    name: str    # what the questions call it, and what its review page is titled by


@dataclass(frozen=True)
class Project:
    root: Path                     # the directory holding provenance.toml, resolved
    name: str
    title: str                     # the review page's title: `name` unless the file gives one
    sources: tuple[str, ...]       # source list names: sources.load_rules() reads them
    cache: Path                    # the directory that holds cache/, as --cache names it
    subjects: tuple[Subject, ...]  # each a run in root/<id>
    context: str = ""              # prompt-safe: `provenance brief` gives it to every researcher
    completeness_check: str = ""   # the orchestrator's and the reviewer's: never in a brief

    @property
    def file(self) -> Path:
        return self.root / FILE

    @property
    def subject_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.subjects)

    def subject(self, subject_id: str) -> Subject | None:
        return next((s for s in self.subjects if s.id == subject_id), None)

    def subject_dir(self, subject: str) -> Path:
        return self.root / subject

    def subject_of(self, run: Path) -> str | None:
        """The declared subject whose run `run` is, or None for the root (or any other
        directory). By the name the project declares, not the directory's own, which differs
        for a subject that is a symlink."""
        at = _real(run)
        return None if at is None else _subject_at(self.root, self.subject_ids, at)


def find(start: Path) -> Path | None:
    """The nearest directory at or above `start` that holds a provenance.toml, or None.

    Walks the path as given (`absolute()`), not the one its symlinks resolve to: a subject that
    is a symlink to another disk is declared in the project it sits in, and its real directory
    has no project above it. (A run reached through a symlink from outside its project finds
    no project, or one that does not declare it, and is refused: never a wrong answer.)
    Anything by that name counts, a dangling symlink or a directory included, and `load()`
    refuses it: skipping it would hand the run to a project further up."""
    here = absolute(start)
    for d in (here, *here.parents):
        if os.path.lexists(d / FILE):
            return d
    return None


def load(root: Path) -> Project:
    """Read `root`/provenance.toml, refusing anything it can't read as a project. Every problem
    is named in one message, so a repair is not a loop of re-runs."""
    from .sources import available

    root = _real(root) or absolute(root)
    path = root / FILE
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise UnreadableProject(f"unreadable project file {path}: {e}", root) from e
    problems: list[str] = []
    if "race" in raw:
        # Refused, not ignored: ignored, the race file's context and completeness check would
        # drop out without a word, and so would the review page's title.
        problems.append("`race` is retired: the race file's title, context and completeness "
                        "check are keys of this file now (`title`, `context`, "
                        "`completeness_check`), and its candidates are `subjects` (README, "
                        "\"Projects\")")
    if unknown := sorted(set(raw) - set(KEYS) - {"race"}):
        # A typo'd key would otherwise read as the key left out: `subject = [...]` as a
        # project with no subjects, which refuses every subject run with the wrong reason.
        problems.append(f"unknown key(s) {', '.join(map(repr, unknown))} "
                        f"(a project file has {', '.join(KEYS)})")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("`name` must name the project")
        name = ""

    title = raw.get("title", name)
    if not isinstance(title, str) or ("title" in raw and not title.strip()):
        problems.append("`title` must be what the review page is titled, or be left out to "
                        "use `name`")

    sources = raw.get("sources")
    if (not isinstance(sources, list) or not sources
            or not all(isinstance(s, str) and s for s in sources)):
        # Required, and never defaulted: a project checked against lists it didn't choose is
        # checked against the wrong ones, silently.
        problems.append("`sources` must list the source lists citations are checked against, "
                        "e.g. [\"us\"]")
        sources = []
    elif missing := [s for s in sources if s not in available()]:
        problems.append(f"`sources` names no such source list: {', '.join(map(repr, missing))} "
                        f"(there are {', '.join(available()) or 'none'})")

    cache = raw.get("cache")
    cache_path = None
    if not isinstance(cache, str) or not cache.strip():
        # Required too. A default is what the old rule was: where the cache goes decided by
        # something other than a line someone wrote.
        problems.append("`cache` must name the directory that holds the shared cache/ "
                        "(\".\" for one beside this file)")
    elif (cache_path := _path(root, cache, "cache", problems)) is not None:
        if bad := next((p for p in (cache_path / "cache", cache_path, *cache_path.parents)
                        if os.path.lexists(p) and not p.is_dir()), None):
            # A file anywhere on the way to cache/pages, the declared directory, its cache/ or
            # a directory above it: the first fetch would fail creating cache/pages, with an
            # OSError naming neither the project file nor the key. Every part that exists has
            # to be a directory; one not created yet is fine.
            problems.append(f"`cache` needs {bad} to be a directory, and it is a file")
        elif any((cache_path / d).is_dir() for d in ("pages", "calaccess")):
            # As --cache: it names the directory that HOLDS cache/, and pointed at cache/ itself a
            # reader looked in cache/cache, found nothing, and a fetch created a second cache there.
            problems.append(f"`cache` names {cache_path}, which is a cache directory itself: "
                            f"`cache` names the directory that holds cache/, so this looks like "
                            f"{cache_path.parent}")

    subjects = raw.get("subjects", [])
    if not isinstance(subjects, list):
        problems.append("`subjects` must be a list of {id = \"<its directory>\", name = \"<what "
                        "the questions call it>\"}")
        subjects = []
    found: list[Subject] = []
    folded: dict[str, str] = {}
    dirs: dict[Path, str] = {}   # resolved directory -> the subject first found there
    names: dict[str, str] = {}   # casefolded name -> the subject first found with it
    for entry in subjects:
        if isinstance(entry, str):
            # The form from before subjects had names. Refused with the form to write: a subject
            # named by its bare id would retarget questions by that id, a word inside others.
            problems.append(f"subject {entry!r} needs a name: write it as {{id = "
                            f"{json.dumps(entry)}, name = \"<what the questions call it>\"}}")
            continue
        if not isinstance(entry, dict):
            problems.append(f"subject {entry!r} is not {{id = ..., name = ...}}")
            continue
        s, label = entry.get("id"), entry.get("name")
        if unknown := sorted(set(entry) - set(SUBJECT_KEYS)):
            problems.append(f"subject {s!r} has unknown key(s) {', '.join(map(repr, unknown))} "
                            f"(a subject has {', '.join(SUBJECT_KEYS)})")
        if not isinstance(label, str) or not label.strip():
            problems.append(f"subject {s!r} needs a `name`: what the questions call it")
            label = None
        elif (other := names.get(label.strip().casefold())) is not None:
            # The name is what the questions and the review page tell subjects apart by.
            problems.append(f"subject {s!r} has the name of subject {other!r}")
        if not isinstance(s, str):
            problems.append(f"subject {entry!r} needs an `id`: the name of its directory")
        elif not re.fullmatch(SUBJECT_PATTERN, s):
            problems.append(f"subject {s!r} is not a plain directory name "
                            f"({SUBJECT_PATTERN})")
        elif s.casefold() in RESERVED:
            problems.append(f"subject {s!r} is a name a run keeps its own files under")
        elif (first := folded.get(s.casefold())) is not None:
            # One directory on a case-insensitive disk (macOS's default), so one run.
            problems.append(f"subject {s!r} repeats {first!r}")
        elif (d := _real(root / s)) is None:
            problems.append(f"subject {s!r} can't be resolved (a symlink loop)")
        elif d == root:
            # A symlink to the root: its run would be the root's, claims and verdicts shared.
            problems.append(f"subject {s!r} is the project root itself")
        elif os.path.lexists(root / s) and not (root / s).is_dir():
            # A file, or a link to one or to nothing, can hold no run. An absent directory is
            # fine: `provenance new-subject` creates it.
            problems.append(f"subject {s!r} is not a directory")
        elif os.path.lexists(root / s / FILE):
            # The nearest project file wins, so its commands would never read this one's
            # declaration: two projects claiming one directory, and which is meant is not on disk.
            problems.append(f"subject {s!r} holds a {FILE} of its own, so it is a project, not "
                            f"a subject of this one")
        elif (other := dirs.get(d)) is not None:
            # Two names for one directory (a symlink to another subject) would be one run under
            # two names: shared claims, verdicts and review output.
            problems.append(f"subject {s!r} is the same directory as subject {other!r}")
        else:
            folded[s.casefold()] = s
            dirs[d] = s
            if label is not None:
                names.setdefault(label.strip().casefold(), s)
                found.append(Subject(id=s, name=label.strip()))

    context, check = raw.get("context", ""), raw.get("completeness_check", "")
    if not isinstance(context, str):
        problems.append("`context` must be text: what every researcher is told")
    elif _CHECK_HEADING.search(context):
        # A race file's body pasted in whole. Its completeness check lists the known answers,
        # and a researcher told what it is looking for confirms that instead of searching.
        problems.append("`context` holds a completeness check heading: move that section to "
                        "`completeness_check`, which no researcher is shown")
    if not isinstance(check, str):
        problems.append("`completeness_check` must be text: the answers already known, for the "
                        "orchestrator and the reviewer")

    if problems:
        raise UnreadableProject(f"{path} can't be read as a project: " + "; ".join(problems),
                                root)
    return Project(root=root, name=name.strip(), title=title.strip(), sources=tuple(sources),
                   cache=cache_path, subjects=tuple(found), context=context,
                   completeness_check=check)


def _path(root: Path, value: str, key: str, problems: list[str]) -> Path | None:
    """A path from the project file's `key`: `~` expanded, relative to the file, resolved. None,
    with the problem named, for a `~user` that names no user or a path in a symlink loop:
    `expanduser()` and `resolve()` raise there, and the traceback named neither the file nor
    the key."""
    try:
        expanded = Path(value).expanduser()
    except RuntimeError:
        problems.append(f"`{key}` is {value!r}, whose ~ names no user on this machine")
        return None
    if (path := _real(root / expanded)) is None:
        problems.append(f"`{key}` is {value!r}, which can't be resolved (a symlink loop)")
    return path


def _real(path: Path) -> Path | None:
    """`path.resolve()`, or None where it can't be resolved: a symlink loop raises. Every
    resolve in this module goes through it, so a loop is a refusal naming the path, never a
    traceback."""
    try:
        return path.resolve()
    except (RuntimeError, OSError):
        return None


def working_dir() -> Path:
    """The working directory as the shell names it ($PWD) where that is the same directory as
    the process's, else the process's. `os.getcwd()` resolves symlinks, so from inside a
    subject that is a symlink it names the real directory, which has no project above it."""
    pwd = os.environ.get("PWD")
    if pwd and os.path.isabs(pwd):
        try:
            if os.path.samefile(pwd, os.getcwd()):
                return Path(pwd)
        except OSError:
            pass
    return Path.cwd()


def absolute(path: Path) -> Path:
    """`path` made absolute against `working_dir()`, with `..` folded and its symlinks left as
    they are: the path as given, which is where the walk to a project file starts."""
    return Path(os.path.normpath(os.path.join(working_dir(), path)))


def _subject_at(root: Path, subjects, at: Path) -> str | None:
    """The one of `subjects` whose directory under `root` is `at` (resolved), or None. The one
    rule for which directory a subject is: `Project.subject_of()`, `resolve()` and the check
    on a parent's declaration all ask it."""
    return next((s for s in subjects if isinstance(s, str) and re.fullmatch(SUBJECT_PATTERN, s)
                 and _real(root / s) == at), None)


def _raw_subjects(root: Path) -> list:
    """The ids of the `subjects` root's project file lists, parsed but not validated, or []
    where it can't be parsed. For what has to be known of a project file `load()` refuses. A
    bare string, the form from before subjects had names, is read as an id too: `load()` refuses
    it, and it still says which directory the file means."""
    try:
        subjects = tomllib.loads((root / FILE).read_text()).get("subjects", [])
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(subjects, list):
        return []
    return [s.get("id") if isinstance(s, dict) else s for s in subjects]


def _reached_from_inside(root_real: Path, run: Path) -> bool:
    """Whether the walk from `run` (`find()`, on the path as given) finds the project whose
    resolved root is `root_real`. A run is named by its path in the project: a path matched only
    by where it resolves would take an outside alias of a subject for that subject."""
    found = find(run)
    return found is not None and _real(found) == root_real


def lists_run(root: Path, run: Path) -> bool:
    """Whether `run` is one of the runs of the project in `root`, for a project file `load()`
    refuses, as far as that file can say: reached from inside the project, as `resolve()`
    requires of every run, and the root or a subject the file lists. A file that can't be
    parsed at all lists only its root. A run that can't be resolved is none."""
    at, root_real = _real(run), _real(root)
    if at is None or root_real is None or not _reached_from_inside(root_real, run):
        return False
    return at == root_real or _subject_at(root, _raw_subjects(root), at) is not None


def _declared_by_parent(root: Path) -> Path | None:
    """The directory above `root` if its project file declares `root` as a subject, else None.

    The nearest project file wins, so from inside such a subject the parent's declaration
    would never be read: a provenance.toml dropped into a subject's directory would take its
    run over, with a cache and source lists of its own, and the parent project's `load()`
    refuses it only when something runs at the parent. Only the parent can declare it, since a
    subject is a direct child. A parent file that can't be parsed declares nothing: its own
    commands refuse it. Parsed, not `load()`ed, since `load()` refuses the parent for exactly
    this, and for anything else wrong with it, which is not this run's to fix."""
    parent, at = root.parent, _real(root)
    return parent if at is not None and _subject_at(parent, _raw_subjects(parent), at) else None


def resolve(run: Path | None, project: Path | None, cwd: Path | None = None,
            subject: str | None = None) -> tuple[Project, Path]:
    """(project, run directory) for a command's `--data`, `--subject` and `--project`.

    The project is the one `project` names, else the nearest one at or above `run`, else at or
    above the working directory. The run is `run` as given, else the directory of the subject
    `subject` names, else the project root. A `run` must be the root or one of the subjects the
    project declares: a mistyped `--data` is refused, where it used to become a run of its own
    with a cache of its own. It must also be reached from inside the project, as the walk from
    it would reach it, with `--project` too: a symlink outside the project to one of its runs is
    refused, not taken as that run. A `subject` must be one the project declares.
    """
    if subject is not None and run is not None:
        # Two names for the run, which can disagree: neither is taken over the other.
        raise ProjectError(f"--subject {subject} and --data {run} both name the run: pass one")
    if project is not None:
        root = absolute(project)
        if not os.path.lexists(root / FILE):
            raise ProjectError(f"--project {project} holds no {FILE}")
    else:
        start = run if run is not None else (cwd or working_dir())
        root = find(start)
        if root is None:
            raise ProjectError(
                f"no {FILE} at or above {absolute(start)}. Every command reads its project from "
                f"one: write one at the project's root, or pass --project. It names the project, "
                f"its source lists, where the cache lives, its subjects and what researchers are "
                f"told (README, \"Projects\"). A directory laid out the old way, with the question "
                f"set, claims and cache in data/ and a run per subject in data/<subject>/, gets "
                f"one in data/ with cache = \".\" and each subject's directory under subjects.")
    if (parent := _declared_by_parent(root)) is not None:
        raise ProjectError(f"{root} is a subject of the project in {parent}, and holds a {FILE} "
                           f"of its own: which project it belongs to is not on disk. Remove "
                           f"one: its own {FILE}, or its entry under `subjects` in "
                           f"{parent / FILE}.")
    p = load(root)
    if subject is not None:
        if p.subject(subject) is None:
            subjects = ", ".join(p.subject_ids) or "none"
            raise ProjectError(f"--subject {subject} is not one of the subjects of the project "
                               f"in {p.root} ({subjects}): a subject is one its {FILE} lists "
                               f"under `subjects`, by id")
        return p, p.subject_dir(subject)
    if run is None:
        return p, p.root
    if (at := _real(run)) is None:
        raise ProjectError(f"{run} can't be resolved (a symlink loop), so it is no run")
    if not _reached_from_inside(p.root, run):
        # Without --project the walk from the run found this project, so it holds already.
        # With it, a run matched only by where it resolves would take an outside alias of a
        # subject as that subject, where the walk from the alias finds no project, or another.
        raise ProjectError(f"{run} is not inside the project in {p.root} as given: it reaches "
                           f"one of its runs only through a symlink from outside. Name the run "
                           f"by its path in the project, the root or <root>/<subject>.")
    if at == p.root or p.subject_of(run) is not None:
        return p, run
    subjects = ", ".join(p.subject_ids) or "none"
    raise ProjectError(f"{run} is neither the root of the project in {p.root} nor one of its "
                       f"subjects ({subjects}). A subject's run is a directory the project's "
                       f"{FILE} lists under `subjects`.")
