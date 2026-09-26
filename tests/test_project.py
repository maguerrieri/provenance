"""A project is declared, never inferred.

The cache root used to be inferred from the filesystem: whether a parent directory held a
question set, whether a cache/ existed here or there. That produced the bugs these tests pin:
a stray cache in one candidate's directory forked the shared cache; a staleness check read the
wrong cache and created that stray as a side effect; and some layouts could not be told apart,
a self-contained data root nested under another, and a candidate scaffolded before its parent
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
from conftest import FIXTURE_RACE, write_project
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
    root = write_project(tmp_path / "p", subjects=["lind", "ng"], cache="~/research-cache",
                         race="race.md")
    p = project.load(root)
    assert p.root == root.resolve() and p.name == "example"
    assert p.sources == ("us", "ca")
    assert p.cache == (Path.home() / "research-cache").resolve(), "~ is the user's home"
    assert p.subjects == ("lind", "ng")
    assert p.race == (root / "race.md").resolve(), "relative to the file, not the cwd"


def test_every_problem_in_a_project_file_is_named_at_once(tmp_path):
    (tmp_path / "provenance.toml").write_text(
        'subject = ["lind"]\n'          # a typo'd key would read as no subjects
        'sources = ["us", "atlantis"]\n'
        'subjects = ["../up", "claims", "Ng", "ng", 3]\n')
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "unknown key(s) 'subject'" in msg
    assert "`name` must name the project" in msg
    assert "no such source list: 'atlantis'" in msg
    assert "`cache` must name the directory that holds the shared cache/" in msg
    assert "`subjects` must be a list of subdirectory names" in msg


def test_subjects_must_be_plain_distinct_directories(tmp_path):
    write_project(tmp_path, subjects=["../up", "claims", "Ng", "ng"])
    (tmp_path / "lind").mkdir()
    with pytest.raises(project.ProjectError) as e:
        project.load(tmp_path)
    msg = str(e.value)
    assert "subject '../up' is not a plain directory name" in msg
    assert "subject 'claims' is a name a run keeps its own files under" in msg
    assert "subject 'ng' repeats 'Ng'" in msg, "one directory on a case-insensitive disk"


def test_two_subjects_are_never_one_directory(tmp_path):
    """A subject may be a symlink (to another disk, say), so names alone don't keep runs apart:
    one linked to another subject, or to the root, would share its claims, verdicts and
    review output under a second name."""
    (tmp_path / "lind").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "lind")
    (tmp_path / "self").symlink_to(tmp_path)
    # nor a file, or a link to nothing: neither can hold a run. An absent one can, once
    # new-candidate creates it.
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
    assert "'ng'" not in msg, "an absent directory is a subject new-candidate has yet to create"


def test_a_cache_naming_a_cache_directory_itself_is_refused(tmp_path):
    """As with --cache: `cache` names the directory that HOLDS cache/, and pointed at cache/
    itself a reader looked in cache/cache and a fetch created a second cache there."""
    (tmp_path / "cache" / "pages").mkdir(parents=True)
    write_project(tmp_path, cache="cache")
    with pytest.raises(project.ProjectError, match=r"which is a cache directory itself"):
        project.load(tmp_path)


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
    """One fetch with --data data/<candidate> created data/<candidate>/cache, and under the rule
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


def test_a_self_contained_root_nested_under_another_is_its_own_project(tmp_path, quiet):
    """Inference sent a child of a directory holding a question set to its parent's cache, so a
    scratch root in data/scratch rebuilt the live CAL-ACCESS database. A nested root with its
    own project file is its own project, whatever its parent holds."""
    outer = write_project(tmp_path / "data")
    (outer / "questions.json").write_text("[]")
    (outer / "cache" / "calaccess").mkdir(parents=True)
    scratch = write_project(outer / "scratch")
    assert cli._cache_root(scratch, None) == scratch.resolve()
    assert cli._cache_root(outer, None) == outer.resolve()

    built = []
    from provenance import calaccess

    orig = calaccess.build
    try:
        calaccess.build = lambda root, progress=None: built.append(root) or root
        cli.calaccess_build(data=scratch)
    finally:
        calaccess.build = orig
    assert built == [scratch.resolve()], "the live database is the outer project's"

    # And the outer project can't also claim it as a subject: which one is meant is not on disk.
    write_project(outer, subjects=["scratch"])
    with pytest.raises(project.ProjectError, match="holds a provenance.toml of its own"):
        project.load(outer)


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
    assert (f"this is the project root's run, not lind's, though the working directory is "
            f"inside {(root / 'lind').resolve()}: pass --data") in out, out
    code, out = _provenance("status", cwd=root)
    assert "project root's run, not" not in out, out
    code, out = _provenance("status", "--data", "lind", cwd=root)
    assert "project root's run, not" not in out, out


def test_a_subject_scaffolded_before_its_template_shares_the_projects_cache(tmp_path):
    """With no question set in the parent, inference read a candidate's directory as a root of
    its own, with its own cache. The cache is the project's whatever question sets exist, and
    new-candidate refuses to scaffold a subject with no set to give it."""
    root = write_project(tmp_path / "data", subjects=["ng"])
    code, out = _provenance("new-candidate", "ng", "--data", root)
    assert code == 1 and f"no question set to copy to {root.resolve() / 'ng' / 'questions.json'}" \
        in out, out
    assert not (root / "ng").exists(), "a refused scaffold writes nothing"

    (root / "ng" / "claims").mkdir(parents=True)   # scaffolded by hand, before any template
    assert cli._cache_root(root / "ng", None) == root.resolve()


def test_new_candidate_refuses_a_subject_the_project_does_not_declare(tmp_path):
    """The project file says what the project is, and no command edits it."""
    root = write_project(tmp_path / "data")
    (root / "questions.json").write_text(json.dumps([{"id": "q1", "text": "?"}]))
    code, out = _provenance("new-candidate", "ng", "--data", root)
    assert code == 1 and "ng is not one of the project's subjects: add it to `subjects`" in out
    assert not (root / "ng").exists()


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


# --- races are the project's --------------------------------------------------------------


def test_the_race_is_the_file_the_project_names(tmp_path):
    root = write_project(tmp_path / "data", race=None)
    (root / "claims").mkdir()
    code, out = _provenance("build", "--data", root)
    assert code == 1 and "names no race file: add race = \"<path>\"" in out, out

    write_project(root, race="elsewhere/race.md")
    code, out = _provenance("build", "--data", root)
    assert code == 1 and f"no race file at {(root / 'elsewhere/race.md').resolve()}" in out, out

    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "race.md").write_text(FIXTURE_RACE.read_text())
    code, out = _provenance("build", "--data", root)
    assert code == 0, out


def test_the_races_listing_is_retired(tmp_path):
    code, out = _provenance("races")
    assert code == 1 and "`provenance races` is retired" in out, out
    assert "race = " in out, out
    _, out = _provenance("--help")
    assert "races" not in out, out
