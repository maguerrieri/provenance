"""A project is declared, never inferred.

The cache root used to be inferred from the filesystem: whether a parent directory held a
question set, whether a cache/ existed here or there. That produced the bugs these tests pin:
a stray cache in one subject's directory forked the shared cache; a staleness check read the
wrong cache and created that stray as a side effect; and some layouts could not be told apart,
a self-contained data root nested under another, and a subject scaffolded before its parent
had a question set. Now every command reads the nearest provenance.toml (`project.py`), and no
directory that happens to exist changes where anything resolves."""

from __future__ import annotations

import io
import itertools
import json
import os
import re
from pathlib import Path

import pytest
from conftest import EXAMPLE_CHECK, EXAMPLE_CONTEXT, EXAMPLE_TITLE, write_project
from rich.console import Console
from typer.testing import CliRunner

from provenance import cli, project
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source

URL = "https://example.org/minutes"
SNIPPET = "voted 4-1 to approve the lease"


def _cache_page(root: Path) -> None:
    """URL's page, cached under `root` (the directory holding cache/)."""
    from datetime import UTC, datetime, timedelta

    from provenance.fetch import cache_path

    cache_path(root, URL).write_text(PageCache(
        url=URL, final_url=URL, status=200, content_type="text/html", title="Minutes",
        text=f"The board {SNIPPET} after a long hearing.",
        fetched_at=datetime.now(UTC) - timedelta(hours=1),
        extractor_version=EXTRACTOR_VERSION).model_dump_json())


def _provenance(*args, cwd: Path | None = None):
    # rich folds a long tmp path mid-word at 80 columns. Put back `_width` itself, not the width
    # it computed: assigning that would pin it, and COLUMNS would stop reaching the console.
    width = cli.con._width
    cli.con._width = 10_000
    here = Path.cwd()
    try:
        if cwd is not None:
            os.chdir(cwd)
        res = CliRunner().invoke(cli.app, [str(a) for a in args])
    finally:
        os.chdir(here)
        cli.con._width = width
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", " ".join(res.output.split()))


@pytest.fixture
def quiet(monkeypatch):
    """cli's console, captured, and a fresh record of the strays already named."""
    out = io.StringIO()
    monkeypatch.setattr(cli, "con", Console(file=out, width=10_000, color_system=None))
    monkeypatch.setattr(cli, "_warned_strays", set())
    return out


def _no_project_above(tmp_path):
    """Take away the example project every test's tmp_path is, for a test about having none."""
    (tmp_path / "provenance.toml").unlink()
    assert project.find(tmp_path) is None, "a provenance.toml above the test's tmp dir"


# --- the project file ------------------------------------------------------------------------


def test_a_project_file_says_everything_resolution_needs(tmp_path):
    root = write_project(tmp_path / "p", subjects=["lind", "ng"], cache="~/research-cache")
    p = project.load(root)
    assert p.root == root.resolve() and p.name == "example"
    assert p.sources == ("us", "ca")
    assert p.cache == (Path.home() / "research-cache").resolve(), "~ is the user's home"
    assert p.subjects == (project.Subject("lind", "Avery Lind"),
                          project.Subject("ng", "Jordan Ng"))
    assert p.subject_ids == ("lind", "ng") and p.subject("ng").name == "Jordan Ng"
    assert p.title == EXAMPLE_TITLE
    assert p.context == EXAMPLE_CONTEXT and p.completeness_check == EXAMPLE_CHECK

    # All three are optional: a title defaults to the name, and the rest to nothing.
    (root / "provenance.toml").write_text('name = "bare"\nsources = ["us"]\ncache = "."\n')
    p = project.load(root)
    assert (p.title, p.subjects, p.context, p.completeness_check) == ("bare", (), "", "")


def test_every_problem_in_a_project_file_is_named_at_once(tmp_path):
    (tmp_path / "provenance.toml").write_text(
        'subject = ["lind"]\n'          # a typo'd key would read as no subjects
        'sources = ["us", "atlantis"]\n'
        'subjects = "lind"\n'
        'title = ""\n'
        'context = 3\n'
        'completeness_check = ["known"]\n')
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "unknown key(s) 'subject'" in msg
    assert "`name` must name the project" in msg
    assert "no such source list: 'atlantis'" in msg
    assert "`cache` must name the directory that holds the shared cache/" in msg
    assert "`subjects` must be a list of {id = " in msg
    assert "`title` must be what the review page is titled" in msg
    assert "`context` must be text" in msg and "`completeness_check` must be text" in msg


def test_a_subject_is_an_id_and_a_name(tmp_path):
    """A subject's name is what the questions call it and what its review page is titled by.
    A bare id, the form before subjects had names, is refused with the form to write: read as
    its own name, a short id would retarget questions by a word inside other words."""
    write_project(tmp_path)
    toml = (tmp_path / "provenance.toml").read_text().replace(
        "subjects = []",
        'subjects = ["lind", 3, {id = "ng"}, {id = "a", name = "Measure A"}, '
        '{id = "b", name = " measure a "}, {name = "No id"}, '
        '{id = "c", name = "Measure C", version = 2}]')
    (tmp_path / "provenance.toml").write_text(toml)
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert ("subject 'lind' needs a name: write it as {id = \"lind\", name = \"<what the "
            "questions call it>\"}") in msg
    assert "subject 3 is not {id = ..., name = ...}" in msg
    assert "subject 'ng' needs a `name`: what the questions call it" in msg
    assert "subject 'b' has the name of subject 'a'" in msg
    assert "needs an `id`: the name of its directory" in msg
    assert "subject 'c' has unknown key(s) 'version' (a subject has id, name)" in msg


def test_the_race_key_is_refused_with_where_its_content_goes(tmp_path):
    """`race` named a race file, a bridge until its content moved into the project file.
    Ignored, the context and completeness check would drop out without a word."""
    write_project(tmp_path, extra='race = "race.md"\n')
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "`race` is retired: the race file's title, context and completeness check are keys " \
        "of this file now (`title`, `context`, `completeness_check`)" in msg
    assert "unknown key" not in msg, "named once, by what to do"


@pytest.mark.parametrize("heading", ["# Completeness check", "## completeness check",
                                     "#Completeness Check:", "# Completeness checks",
                                     "**Completeness check**", "Completeness check\n---"])
def test_a_context_holding_the_completeness_check_is_refused(tmp_path, heading):
    """A race file's body pasted whole into `context` would carry its completeness check, the
    answers already known, into every researcher's prompt."""
    write_project(tmp_path, context=f"Where records are.\n\n{heading}\n\n- A known claim.\n")
    with pytest.raises(project.ProjectError, match="`context` holds a completeness check heading"):
        project.load(tmp_path)


def test_subjects_must_be_plain_distinct_directories(tmp_path):
    write_project(tmp_path, subjects=["../up", "claims", "Ng", "ng"])
    (tmp_path / "lind").mkdir()
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "subject '../up' is not a plain directory name" in msg
    assert "subject 'claims' is a name a run keeps its own files under" in msg
    assert "subject 'ng' repeats 'Ng'" in msg, "one directory on a case-insensitive disk"


@pytest.mark.parametrize("name", ["pages", "calaccess"])
def test_a_subject_named_like_the_cache_is_refused_as_a_subject(tmp_path, name):
    """With cache = ".", a subject called "pages" made the root read as a cache directory,
    and the refusal blamed `cache`. It is refused by its own name."""
    write_project(tmp_path, subjects=[name])
    (tmp_path / name).mkdir()
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    assert f"subject {name!r} is a name a run keeps its own files under" in str(e.value)


def test_two_subjects_are_never_one_directory(tmp_path):
    """A subject may be a symlink (to another disk, say), so names alone don't keep runs apart:
    one linked to another subject, or to the root, would share its claims, verdicts and
    review output under a second name."""
    (tmp_path / "lind").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "lind")
    (tmp_path / "self").symlink_to(tmp_path)
    # nor a file, or a link to nothing: neither can hold a run. An absent one can, once
    # new-subject creates it.
    (tmp_path / "notes").write_text("")
    (tmp_path / "gone").symlink_to(tmp_path / "unmounted")
    write_project(tmp_path, subjects=["lind", "alias", "self", "notes", "gone", "ng"])
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "subject 'alias' is the same directory as subject 'lind'" in msg
    assert "subject 'self' is the project root itself" in msg
    assert "subject 'notes' is not a directory" in msg
    assert "subject 'gone' is not a directory" in msg
    assert "'ng'" not in msg, "an absent directory is a subject new-subject has yet to create"


def test_a_cache_naming_a_cache_directory_itself_is_refused(tmp_path):
    """As with --cache: `cache` names the directory that HOLDS cache/, and pointed at cache/
    itself a reader looked in cache/cache and a fetch created a second cache there."""
    (tmp_path / "cache" / "pages").mkdir(parents=True)
    write_project(tmp_path, cache="cache")
    with pytest.raises(project.ProjectError, match=r"which is a cache directory itself"):
        project.load(tmp_path)


def test_a_symlink_loop_is_refused_by_name_never_a_traceback(tmp_path):
    """`Path.resolve()` raises on a symlink loop, and a project file with one in a subject or
    the cache escaped load() as a traceback. So does a --data in one."""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    write_project(tmp_path, subjects=["a"], cache="b/x")
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "subject 'a' can't be resolved (a symlink loop)" in msg
    assert "`cache` is 'b/x', which can't be resolved (a symlink loop)" in msg

    write_project(tmp_path)
    with pytest.raises(project.ProjectError, match="can't be resolved"):
        project.resolve(tmp_path / "a", None)
    assert not project.lists_run(tmp_path, tmp_path / "a")


def test_a_tilde_naming_no_user_is_refused_by_key(tmp_path):
    """`expanduser()` raises for an unknown ~user, and the traceback named neither the project
    file nor the key."""
    write_project(tmp_path, cache="~no-such-user-here/cache")
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    assert "`cache` is '~no-such-user-here/cache', whose ~ names no user" in str(e.value)


@pytest.mark.parametrize("where", ["the directory named", "its cache/", "a directory above it"])
def test_a_file_where_the_cache_goes_is_refused(tmp_path, where):
    """The first fetch would fail creating cache/pages under it, with an error naming neither
    the project file nor the key. A directory not created yet is fine."""
    write_project(tmp_path, cache="above/store")
    assert project.load(tmp_path).cache == (tmp_path / "above" / "store").resolve()
    bad = {"the directory named": tmp_path / "above" / "store",
           "its cache/": tmp_path / "above" / "store" / "cache",
           "a directory above it": tmp_path / "above"}[where]
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("")
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    assert f"`cache` needs {bad.resolve()} to be a directory, and it is a file" in str(e.value)


def test_an_unreadable_project_file_is_refused_not_skipped(tmp_path):
    """A walk that skipped a broken project file would hand the run to a project further up."""
    inner = write_project(tmp_path / "inner")
    (inner / "provenance.toml").write_text("name = ")
    code, out = _provenance("status", "--data", inner)
    assert code == 1 and f"unreadable project file {inner / 'provenance.toml'}" in out, out

    # A dangling symlink is a project file that can't be read, not an absent one.
    (inner / "provenance.toml").unlink()
    (inner / "provenance.toml").symlink_to(tmp_path / "moved.toml")
    assert project.find(inner) == inner.resolve()
    code, out = _provenance("status", "--data", inner)
    assert code == 1 and "unreadable project file" in out, out


# --- finding it --------------------------------------------------------------------------------


def test_the_nearest_project_file_wins(tmp_path):
    outer = write_project(tmp_path / "outer", subjects=["cand"])
    inner = write_project(outer / "cand" / "deep" / "inner")
    assert project.find(inner / "claims") == inner.resolve()
    assert project.find(outer / "cand" / "deep") == outer.resolve()


def test_with_no_run_given_the_run_is_the_root_found_from_the_working_directory(tmp_path):
    root = write_project(tmp_path / "p", subjects=["lind"])
    (root / "lind" / "claims").mkdir(parents=True)
    p, run = project.resolve(None, None, cwd=root / "lind" / "claims")
    assert p.root == root.resolve() and run == root.resolve()
    # --project from anywhere, and a run given beside it must be one of its runs
    p, run = project.resolve(root / "lind", root, cwd=tmp_path)
    assert run == root / "lind"
    with pytest.raises(project.ProjectError, match="holds no provenance.toml"):
        project.resolve(None, tmp_path / "nowhere")


def test_a_run_the_project_does_not_declare_is_refused(tmp_path):
    """A mistyped --data used to become a run of its own, with a cache of its own."""
    root = write_project(tmp_path / "p", subjects=["lind"])
    code, out = _provenance("verify", "--data", root / "lnid")
    assert code == 1, out
    assert (f"{root / 'lnid'} is neither the root of the project in {root.resolve()} nor one of "
            f"its subjects (lind)") in out, out


def test_no_project_is_refused_with_how_to_write_one(tmp_path):
    """No command adopts a directory laid out the old way: moving one to a project file is a
    reviewed change, not something a command does silently."""
    _no_project_above(tmp_path)
    data = tmp_path / "data"
    (data / "claims").mkdir(parents=True)
    (data / "questions.json").write_text("[]")
    (data / "cache" / "pages").mkdir(parents=True)
    for args in (["verify", "--data", data], ["build", "--data", data], ["status"],
                 ["fetch", "https://example.org/x"], ["query", "calaccess.ie_total"]):
        code, out = _provenance(*args, cwd=data)
        assert code == 1, (args, out)
        assert f"no provenance.toml at or above {data.resolve()}" in out, (args, out)
        assert "gets one in data/ with cache = \".\"" in out, (args, out)
    assert not (data / "out").exists()


def test_a_command_given_its_cache_needs_no_project(tmp_path):
    """--cache names everything a cache-only command reads: a researcher checking a quote from
    anywhere needs no project around it."""
    _no_project_above(tmp_path)
    _cache_page(tmp_path)
    code, out = _provenance("check", URL, SNIPPET, "--cache", tmp_path, cwd=tmp_path)
    assert code == 0 and "OK" in out, out


# --- the known cases -------------------------------------------------------------------------


def test_a_stray_cache_in_a_subject_does_not_fork_the_shared_one(tmp_path, quiet):
    """One fetch with --data data/<subject> created data/<subject>/cache, and under the rule
    "the run's own cache wins if it exists" that stray WAS the cache from then on: the pages
    forked and the CAL-ACCESS database was hidden, so every query citation failed at once."""
    root = write_project(tmp_path / "data", subjects=["cand"])
    cand = root / "cand"
    cand.mkdir()
    shared = root.resolve()

    # a fresh clone: the declared cache doesn't exist yet, and nothing forks before it does
    assert cli._cache_root(cand, None) == shared
    assert quiet.getvalue() == ""

    (root / "cache").mkdir()
    assert cli._cache_root(cand, None) == shared
    assert quiet.getvalue() == ""

    # with a stray: STILL the declared cache, and the stray is named, once
    (cand / "cache" / "pages").mkdir(parents=True)
    assert cli._cache_root(cand, None) == shared
    warning = quiet.getvalue()
    assert str(cand / "cache") in warning and str(shared / "cache") in warning, warning
    assert "--cache" in warning
    assert cli._cache_root(cand, None) == shared
    assert quiet.getvalue() == warning, "named once, not once per source"

    # an explicit --cache overrides everything, stray included, and needs no warning
    assert cli._cache_root(cand, tmp_path / "x") == tmp_path / "x"
    assert quiet.getvalue() == warning


def test_a_stray_is_named_as_written_and_says_when_the_cache_is_not_there_yet(tmp_path, quiet):
    root = write_project(tmp_path / "[old]" / "data", subjects=["cand"])
    (root / "cand" / "cache").mkdir(parents=True)
    assert cli._cache_root(root / "cand", None) == root.resolve()
    said = quiet.getvalue()
    assert str(root / "cand" / "cache") in said, "a bracketed path segment is text, not markup"
    assert f"{root.resolve() / 'cache'} (not created yet)" in said, said


def test_an_unrelated_cache_beside_the_project_changes_nothing(tmp_path, quiet):
    """An unrelated ./cache at the repo root once redirected the whole pipeline to a root with
    no CAL-ACCESS database, and 14 query citations failed on a file that plainly existed."""
    root = write_project(tmp_path / "data")
    (tmp_path / "cache" / "pages").mkdir(parents=True)
    assert cli._cache_root(root, None) == root.resolve()
    assert quiet.getvalue() == ""


def test_a_cache_the_project_puts_elsewhere_is_the_one_every_command_uses(tmp_path):
    """A user-level cache shared across projects is a path in the project file, and the
    commands that stamp and check verdicts read it and create nothing in the run."""
    shared = tmp_path / "user-cache"
    root = write_project(tmp_path / "p", subjects=["cand"], cache=str(shared))
    cand = root / "cand"
    (cand / "claims").mkdir(parents=True)
    (cand / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    _cache_page(shared)
    s = Source(url=URL, publisher="Example Gazette", author="A. Reporter", date="2030-05-14",
               source_type="bylined_journalism", snippet=SNIPPET)
    (cand / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a", sources=[s]).model_dump_json())
    code, out = _provenance("verify", "--data", cand)
    assert code == 0 and "q1 verified" in out, out
    for args in (["judgments"], ["status"], ["build"]):
        _provenance(*args, "--data", cand)
        assert not (cand / "cache").exists(), f"`provenance {args[0]}` created a stray"
        assert not (root / "cache").exists(), f"`provenance {args[0]}` created a second cache"
    built = json.loads((cand / "out" / "claims.json").read_text())
    assert built[0]["sources"][0]["verification"]["status"] == "verified"


def test_a_self_contained_root_nested_under_another_is_its_own_project(tmp_path, quiet,
                                                                      monkeypatch):
    """Inference sent a child of a directory holding a question set to its parent's cache, so a
    scratch root in data/scratch rebuilt the live CAL-ACCESS database. A nested root with its
    own project file is its own project, whatever its parent holds."""
    from provenance import calaccess

    outer = write_project(tmp_path / "data")
    (outer / "questions.json").write_text("[]")
    (outer / "cache" / "calaccess").mkdir(parents=True)
    scratch = write_project(outer / "scratch")
    assert cli._cache_root(scratch, None) == scratch.resolve()
    assert cli._cache_root(outer, None) == outer.resolve()

    built = []
    monkeypatch.setattr(calaccess, "build", lambda root, progress=None: built.append(root) or root)
    cli.calaccess_build(data=scratch)
    assert built == [scratch.resolve()], "the live database is the outer project's"

    # And the outer project can't also claim it as a subject: which one is meant is not on disk.
    write_project(outer, subjects=["scratch"])
    with pytest.raises(project.ProjectError, match="holds a provenance.toml of its own"):
        project.load(outer)


def test_calaccess_commands_share_the_cache_root_with_queries(tmp_path, monkeypatch):
    """`provenance query` resolves the CAL-ACCESS database through _cache_root, and so must every
    `provenance calaccess` command, from a subject's run as from the root's. Otherwise
    `provenance calaccess build --data <subject>` writes 1.5 GB into a cache that queries then
    ignore: the database hidden again."""
    from types import SimpleNamespace

    from provenance import calaccess, queries

    root = write_project(tmp_path / "data", subjects=["cand"])
    cand = root / "cand"
    cand.mkdir()
    seen = []
    monkeypatch.setattr(calaccess, "build", lambda r, progress=None: seen.append(r) or r)
    monkeypatch.setattr(calaccess, "find_filers", lambda r, *a, **k: seen.append(r) or [])
    monkeypatch.setattr(calaccess, "contributions_to", lambda r, *a, **k: seen.append(r) or [])
    monkeypatch.setattr(calaccess, "independent_expenditures",
                        lambda r, *a, **k: seen.append(r) or [])
    monkeypatch.setattr(calaccess, "citable_snapshot",
                        lambda url, root=None, **k: seen.append(root) or (None, "none"))
    monkeypatch.setattr(queries, "run", lambda name, params, r: seen.append(r) or SimpleNamespace(
        found=True, value=1, note="", version=1, export_date="", unsettled="", unrestated=[],
        late=[]))

    def every_command(**kw):
        cli.calaccess_build(data=cand, **kw)
        cli.calaccess_filer("Ko", data=cand, **kw)
        cli.calaccess_contributions("123", data=cand, **kw)
        cli.calaccess_ie("Ko", data=cand, first="Dana", **kw)
        cli.calaccess_cite("123", data=cand, **kw)
        cli.run_query("contributor_total", param=["filer_id=123"], data=cand, **kw)

    every_command()
    assert seen == [root.resolve()] * 6
    # and every one of them takes --cache: the commands a reviewer re-checks a figure with must
    # reach the database `provenance verify --cache` checked it against
    seen.clear()
    every_command(cache=tmp_path / "shared")
    assert seen == [tmp_path / "shared"] * 6
    # and --project, from outside the project
    seen.clear()
    monkeypatch.chdir(tmp_path)
    every_command(project=root)
    assert seen == [root.resolve()] * 6


def test_a_project_file_dropped_into_a_declared_subject_is_refused_from_inside_too(tmp_path):
    """The nearest project file wins, so from inside the subject the parent's declaration was
    never read: a provenance.toml placed in a subject's directory took its run over, with a
    cache and source lists of its own. Either side of the conflict now refuses."""
    outer = write_project(tmp_path / "data", subjects=["lind"])
    lind = write_project(outer / "lind", sources=("us",), cache=str(tmp_path / "elsewhere"))
    (lind / "claims").mkdir()
    for args in (["build", "--data", lind], ["status", "--data", lind], ["build", "--data", outer],
                 ["fetch", URL, "--data", lind]):
        code, out = _provenance(*args)
        assert code == 1, (args, out)
        assert "provenance.toml of its own" in out, (args, out)
    code, out = _provenance("status", cwd=lind / "claims")
    assert code == 1 and f"{lind} is a subject of the project in {outer}" in out, out
    assert not (tmp_path / "elsewhere").exists()


def test_a_subject_that_is_a_symlink_finds_the_project_it_is_declared_in(tmp_path, quiet):
    """The walk follows the path as given: the subject's real directory, on another disk, has
    no project above it."""
    root = write_project(tmp_path / "data", subjects=["ng"])
    real = tmp_path / "other-disk" / "ng"
    (real / "claims").mkdir(parents=True)
    (root / "ng").symlink_to(real)
    assert project.find(root / "ng") == root
    p, run = project.resolve(root / "ng", None)
    assert p.root == root.resolve() and run == root / "ng"
    assert cli._cache_root(root / "ng", None) == root.resolve()
    # Named by its real directory, it is outside its project: refused, never mistaken. (Here the
    # project it is found in is tmp_path's, which does not declare it.)
    with pytest.raises(project.ProjectError, match="is neither the root of the project"):
        project.resolve(real, None)
    # With --project too: an alias outside the project, or the real directory, reaches the
    # subject only through a symlink, and is refused rather than taken as ng's run.
    alias = tmp_path / "alias"
    alias.symlink_to(root / "ng")
    for outside in (alias, real):
        with pytest.raises(project.ProjectError, match="only through a symlink from outside"):
            project.resolve(outside, root)
        # and, for a build under a project file it can't load, no run whose render to clear
        assert not project.lists_run(root, outside)
    assert project.resolve(root / "ng", root)[1] == root / "ng"
    assert project.lists_run(root, root / "ng") and project.lists_run(root, root)

    # Named by the project's name for it, not its directory's: the command a refusal gives must
    # name a subject the project has. Here its real directory is "ng-2030".
    real2 = tmp_path / "other-disk" / "ng-2030"
    (real2 / "claims").mkdir(parents=True)
    (root / "ng").unlink()
    (root / "ng").symlink_to(real2)
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    code, _ = _provenance("status", "--data", root / "ng")
    out = " ".join(quiet.getvalue().split())   # the console is `quiet`'s here
    assert code == 1 and f"{root / 'ng'} is ng's run and has no questions.json" in out, out
    assert "`provenance new-subject ng` copies the project's, retargeted to it." in out, out
    assert "ng-2030's" not in out and "new-subject ng-2030" not in out, out


def _symlinked_subject(tmp_path) -> tuple[Path, Path]:
    """A project whose subject ng is a symlink to a directory on another disk, with a question
    set and a claim of its own: (project root, ng as the project declares it)."""
    root = write_project(tmp_path / "data", subjects=["ng"])
    real = tmp_path / "other-disk" / "ng"
    (real / "claims").mkdir(parents=True)
    (root / "ng").symlink_to(real)
    (real / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    (real / "claims" / "q1.json").write_text(
        Claim(question_id="q1", question="?", answer="a").model_dump_json())
    return root, root / "ng"


def test_check_claim_finds_a_symlinked_subjects_project(tmp_path):
    """The claim's path is made absolute, not resolved: resolved, it named the real directory,
    with no project above it, and a researcher on that subject could never pass its last step."""
    root, ng = _symlinked_subject(tmp_path)
    code, out = _provenance("check-claim", "ng/claims/q1.json", cwd=root)
    assert "no provenance.toml at or above" not in out, out
    # The claim cites nothing, so corroboration is all that fails: its question, checked
    # against ng's own set, passes.
    assert out.startswith("corroboration") and "question" not in out, out


def test_from_inside_a_symlinked_subject_the_walk_starts_where_the_shell_says(
        tmp_path, monkeypatch):
    """os.getcwd() resolves symlinks, so from inside a symlinked subject it named the real
    directory, and every command was refused for want of a project. The walk starts from $PWD
    when that is the same directory."""
    monkeypatch.setattr(cli, "_noted_defaults", set())
    root, ng = _symlinked_subject(tmp_path)
    monkeypatch.chdir(ng)
    monkeypatch.setenv("PWD", str(ng))
    assert project.working_dir() == ng
    p, run = project.resolve(None, None)
    assert p.root == root.resolve() and run == root.resolve()
    assert project.resolve(Path("."), None)[1] == Path(".")
    res = CliRunner().invoke(cli.app, ["status"])
    assert res.exit_code == 0, res.output
    # A $PWD naming another directory is ignored: the process's own is the one that is right.
    monkeypatch.setenv("PWD", str(tmp_path))
    assert project.working_dir() == Path.cwd()


def test_the_root_run_is_named_when_defaulted_from_inside_a_subject(tmp_path, monkeypatch):
    """With no --data the run is the project root, wherever the working directory is. From
    inside a subject that is easy to mistake for the subject's, so a command working on the
    run says which it is, once, and how to name the subject's."""
    monkeypatch.setattr(cli, "_noted_defaults", set())
    root = write_project(tmp_path / "data", subjects=["lind"])
    (root / "claims").mkdir()
    (root / "lind" / "claims").mkdir(parents=True)
    code, out = _provenance("status", cwd=root / "lind" / "claims")
    assert code == 0, out
    lind = (root / "lind").resolve()
    assert (f"this is the project root's run, not lind's, though the working directory is "
            f"inside {lind}: pass --subject lind, or --data {lind} where a command has no "
            f"--subject, for lind's") in out, out
    code, out = _provenance("status", cwd=root)
    assert "project root's run, not" not in out, out
    code, out = _provenance("status", "--data", "lind", cwd=root)
    assert "project root's run, not" not in out, out
    code, out = _provenance("status", "--subject", "lind", cwd=root / "lind" / "claims")
    assert code == 0 and "project root's run, not" not in out, out


def test_a_subject_scaffolded_before_its_template_shares_the_projects_cache(tmp_path):
    """With no question set in the parent, inference read a subject's directory as a root of
    its own, with its own cache. The cache is the project's whatever question sets exist, and
    new-subject refuses to scaffold a subject with no set to give it."""
    root = write_project(tmp_path / "data", subjects=["ng"])
    code, out = _provenance("new-subject", "ng", "--data", root)
    assert code == 1 and f"no question set to copy to {root.resolve() / 'ng' / 'questions.json'}" \
        in out, out
    assert not (root / "ng").exists(), "a refused scaffold writes nothing"

    (root / "ng" / "claims").mkdir(parents=True)   # scaffolded by hand, before any template
    assert cli._cache_root(root / "ng", None) == root.resolve()


def test_new_subject_refuses_a_subject_the_project_does_not_declare(tmp_path):
    """The project file says what the project is, and no command edits it."""
    root = write_project(tmp_path / "data")
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    code, out = _provenance("new-subject", "ng", "--data", root)
    assert code == 1 and "ng is not one of the project's subjects: add it to `subjects`" in out
    assert '{id = "ng", name = "<what the questions call it>"}' in out, out
    assert not (root / "ng").exists()


def test_new_candidate_is_retired_for_new_subject(tmp_path):
    code, out = _provenance("new-candidate", "ng")
    assert code == 1 and "`provenance new-candidate` is retired" in out, out
    assert "`provenance new-subject <id>`" in out, out
    _, out = _provenance("--help")
    assert "new-subject" in out and "new-candidate" not in out, out

    # And the build command it printed, which named the run with --candidate.
    code, out = _provenance("build", "--data", tmp_path, "--candidate", "ng")
    assert code == 1 and "build --candidate is retired: pass --subject ng" in out, out
    _, out = _provenance("build", "--help")
    assert "--subject" in out and "--candidate" not in out, out


def test_a_subject_need_not_be_a_person(tmp_path):
    """Two versions of an amended proposal are two subjects: each its own run, its questions
    retargeted to its own name, the printed commands naming it by id."""
    root = write_project(tmp_path / "data", subjects=[
        {"id": "measure-a", "name": "Measure A as introduced"},
        {"id": "measure-a-2", "name": "Measure A as amended"}])
    (root / "questions.json").write_text(json.dumps(
        [{"id": "q1", "text": "What would Measure A as introduced fund?"}]))
    code, out = _provenance("new-subject", "measure-a-2", cwd=root)
    assert code == 0, out
    q = json.loads((root / "measure-a-2" / "questions.json").read_text())
    assert q == [{"id": "q1", "text": "What would Measure A as amended fund?",
                  "subject": "measure-a-2"}]
    assert "provenance verify --subject measure-a-2" in out and "--project" not in out, out

    # From outside the project, the printed commands name it too.
    code, out = _provenance("new-subject", "measure-a", "--project", root, cwd=tmp_path.parent)
    assert code == 0, out
    assert f"provenance build --subject measure-a --project {root.resolve()}" in out, out


def test_retargeting_never_replaces_a_name_inside_another():
    """One version's name can be inside another's. Replaced one at a time, a question already
    naming the amended version read "Measure A (amended) (amended)"."""
    a, b = project.Subject("a", "Measure A"), project.Subject("b", "Measure A (amended)")
    both = (a, b)
    assert cli._retarget("What would Measure A fund?", both, b) \
        == "What would Measure A (amended) fund?"
    assert cli._retarget("What would Measure A (amended) fund?", both, b) \
        == "What would Measure A (amended) fund?"
    assert cli._retarget("What would Measure A (amended) fund?", both, a) \
        == "What would Measure A fund?"
    ng = project.Subject("ng", "Jordan Ng")
    assert cli._retarget("Avery Lind's record, not Avery Lindqvist's", (ng,
                         project.Subject("lind", "Avery Lind")), ng) \
        == "Jordan Ng's record, not Avery Lindqvist's", "whole names only"


PRESENT = ("cache", "cand/cache", "out", "cand/out", "questions.json", "cand/questions.json")


@pytest.mark.parametrize("present", [
    combo for n in range(len(PRESENT) + 1) for combo in itertools.combinations(PRESENT, n)],
    ids=lambda c: "+".join(c) or "none")
def test_no_cache_output_or_question_set_that_exists_moves_the_cache(tmp_path, quiet, present):
    """The acceptance, literally: resolution never depends on which cache or output
    directories happen to exist, nor on which question sets do."""
    root = write_project(tmp_path / "data", subjects=["cand"])
    (root / "cand").mkdir()
    for p in present:
        if p.endswith(".json"):
            (root / p).write_text("[]")
        else:
            (root / p).mkdir(parents=True, exist_ok=True)
    assert cli._cache_root(root, None) == root.resolve()
    assert cli._cache_root(root / "cand", None) == root.resolve()


# --- --subject ---------------------------------------------------------------------------


def test_subject_names_a_run_by_its_id(tmp_path):
    """--subject names a declared subject's run from anywhere in the project, as --data names it
    by its directory. The two together are refused: they could disagree."""
    root = write_project(tmp_path / "data", subjects=["lind", "ng"])
    (root / "claims").mkdir()
    assert project.resolve(None, None, cwd=root / "claims", subject="ng") \
        == (project.load(root), root.resolve() / "ng")
    assert project.resolve(None, root, cwd=tmp_path, subject="ng")[1] == root.resolve() / "ng"

    code, out = _provenance("status", "--subject", "lnid", cwd=root)
    assert code == 1 and "--subject lnid is not one of the subjects of the project" in out, out
    assert "(lind, ng)" in out, out
    code, out = _provenance("status", "--subject", "ng", "--data", root / "lind", cwd=root)
    assert code == 1 and "both name the run: pass one" in out, out


@pytest.mark.parametrize("command", ["verify", "archive", "build", "serve", "handoff q1",
                                     "judge q1 x supports", "judgments",
                                     "clear-contradiction q1 x", "status", "brief"])
def test_every_run_command_takes_subject(tmp_path, command):
    """Every command that works on a run names it by --subject too, refused for a subject the
    project doesn't declare, before it touches anything."""
    write_project(tmp_path, subjects=["lind"])
    code, out = _provenance(*command.split(), "--subject", "nope", cwd=tmp_path)
    assert code == 1 and "--subject nope is not one of the subjects" in out, (command, out)


# --- titles --------------------------------------------------------------------------------


def test_the_review_page_is_titled_by_the_runs_subject(tmp_path):
    """#8 left every subject's page titled by the project alone, so two subjects' pages read
    alike. A subject's run is titled by its subject, the root's by the project."""
    root = write_project(tmp_path / "data", subjects=["lind"])
    for run in (root, root / "lind"):
        (run / "claims").mkdir(parents=True)
    assert _provenance("build", cwd=root)[0] == 0
    assert "<h1>Citation review — Example County Assessor</h1>" \
        in (root / "out" / "review.html").read_text()
    assert _provenance("build", "--subject", "lind", cwd=root)[0] == 0
    assert "<h1>Citation review — Avery Lind — Example County Assessor</h1>" \
        in (root / "lind" / "out" / "review.html").read_text()


# --- what researchers are told ----------------------------------------------------------------


def test_the_races_listing_is_retired(tmp_path):
    code, out = _provenance("races")
    assert code == 1 and "`provenance races` is retired" in out, out
    assert "`provenance brief`" in out, out
    _, out = _provenance("--help")
    assert "races" not in out, out


def test_the_brief_is_the_subject_and_the_context(tmp_path):
    root = write_project(tmp_path / "data", subjects=["lind"])
    code, out = _provenance("brief", "--subject", "lind", cwd=root)
    assert code == 0, out
    assert out.startswith("Project: Example County Assessor Subject: Avery Lind "), out
    assert " ".join(EXAMPLE_CONTEXT.split()) in out, out
    code, out = _provenance("brief", cwd=root)
    assert code == 0 and "Subject:" not in out, out


def test_the_briefs_standard_output_is_the_brief_alone(tmp_path, monkeypatch):
    """It is pasted into prompts whole, so what the CLI says about it (that this is the root's
    run though the working directory is a subject's, that a character is shown as an escape)
    goes to standard error, never into the paste."""
    monkeypatch.setattr(cli, "_noted_defaults", set())
    root = write_project(tmp_path / "data", subjects=["lind"], context="Minutes\xadonline.\n")
    (root / "lind").mkdir()
    here = Path.cwd()
    os.chdir(root / "lind")
    try:
        res = CliRunner().invoke(cli.app, ["brief"])
    finally:
        os.chdir(here)
    assert res.exit_code == 0, res.output
    assert res.stdout == "Project: Example County Assessor\n\nMinutes\\xadonline.\n", res.stdout
    err = " ".join(res.stderr.split())
    assert "this is the project root's run, not lind's" in err, err
    assert "are shown as escapes" in err, err


def test_a_researcher_is_told_to_take_its_context_from_the_brief_alone():
    """The brief leaves the completeness check out, and a researcher with Read and Bash could
    still open the project file. Its instructions say not to, and the skill not to send it."""
    root = Path(__file__).parent.parent
    researcher = " ".join((root / "agents" / "researcher.md").read_text().split())
    assert "Take your context from that brief, and only from it." in researcher
    assert "Never read the project's `provenance.toml` yourself." in researcher
    skill = " ".join((root / "skills" / "voter-guide-research" / "SKILL.md").read_text().split())
    assert "never paste `provenance.toml` itself or tell a researcher to read it" in skill


def test_the_completeness_check_never_reaches_a_researcher(tmp_path):
    """The completeness check is the answers already known. A researcher told what it is looking
    for confirms that item instead of searching, so nothing off the list surfaces, and the
    corroboration that comes back is the pipeline agreeing with itself. `provenance brief` is
    where a researcher's context is put together: it must never hold any of it, for any run,
    however the check is worded."""
    sentinel = "SENTINEL-KNOWN-ANSWER"
    root = write_project(tmp_path / "data", subjects=["lind", "ng"],
                         completeness_check=f"{EXAMPLE_CHECK}\n- {sentinel}\n")
    p = project.load(root)
    for run in (None, "lind", "ng"):
        args = ["brief"] + (["--subject", run] if run else [])
        code, out = _provenance(*args, cwd=root)
        assert code == 0, out
        assert sentinel not in out, (run, out)
        for line in EXAMPLE_CHECK.splitlines():
            if line.strip():
                assert " ".join(line.split()) not in out, (run, line)
        assert sentinel not in cli.researcher_brief(p, run)

    # Built from the fields a researcher may read, by name: a field of the project that isn't
    # one of them never reaches a brief, whatever it holds.
    import dataclasses
    readable = {"title", "context", "subjects"}
    for f in dataclasses.fields(project.Project):
        if f.name in readable or f.type not in ("str",):
            continue
        marked = dataclasses.replace(p, **{f.name: sentinel})
        assert sentinel not in cli.researcher_brief(marked, "lind"), f.name
