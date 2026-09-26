"""`provenance new` and `provenance ask`: the two ways a project starts.

`new` writes a project for a template, and `ask` a project holding one question. Each prints
the command that hands the project to the plugin's skill, which does the research. Neither
changes a project that exists: `new` refuses a directory holding a project file or a run's
files, and `ask` any directory that exists. And neither writes a project file any command
would refuse: each is read back as every command reads it, and a refused one leaves nothing
behind.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import write_project
from typer.testing import CliRunner

from provenance import cli, project
from provenance.fetch import cache_path
from provenance.models import EXTRACTOR_VERSION, Claim, PageCache, Source

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "What does the record show about the county's road-repair bond?"
URL = "https://harbor-ledger.example/road-bond"
SNIPPET = "approved the road-repair bond by a vote of 4-1"
STORY = f"On Monday the supervisors {SNIPPET}, sending it to the ballot.\n"
TEMPLATE = "# Questions\n\n1. What does the record show about the road-repair bond?\n"


@pytest.fixture(autouse=True)
def wide(monkeypatch):
    """rich folds a long tmp path at 80 columns. `_width` itself, so COLUMNS still reaches the
    console in later tests (CLAUDE.md, "A test that widens the console must not pin it")."""
    monkeypatch.setattr(cli.con, "_width", 10_000)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home directory of the test's own, for `ask`'s cache under ~."""
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("HOME", str(d))
    return d


def _provenance(*args, cwd: Path | None = None, monkeypatch=None) -> tuple[int, str]:
    if cwd is not None:
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("PWD", str(cwd))
    res = CliRunner().invoke(cli.app, [str(a) for a in args])
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        raise res.exception   # a crash also exits 1, so it must not pass for a refusal
    return res.exit_code, re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _template(tmp_path) -> Path:
    t = tmp_path / "tpl.md"
    t.write_text(TEMPLATE)
    return t


# --- new ---------------------------------------------------------------------------------------


def test_new_writes_a_project_every_command_reads(tmp_path):
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us", "--source", "ca")
    assert code == 0, out
    p = project.load(tmp_path / "p")
    assert (p.name, p.sources, p.subjects) == ("p", ("us", "ca"), ())
    assert p.cache == (tmp_path / "p").resolve()
    assert (p.context, p.completeness_check) == ("", "")
    assert (tmp_path / "p" / "template.md").read_text() == TEMPLATE
    # The hand-off: the skill, invoked in the project, quoted for a shell.
    assert f"cd {p.root} && claude {cli.SKILL}" in out, out


def test_new_lists_each_source_once_and_takes_a_name(tmp_path):
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us", "--source", "us", "--name", "Road bond review")
    assert code == 0, out
    p = project.load(tmp_path / "p")
    assert (p.name, p.sources) == ("Road bond review", ("us",))


@pytest.mark.parametrize(("args", "said"), [
    ((), "--source is required"),
    (("--source", "zz"), "--source names no such source list: zz"),
], ids=["none", "unknown"])
def test_new_requires_the_source_lists_and_names_the_ones_there_are(tmp_path, args, said):
    """Never defaulted: a project checked against lists it didn't choose is checked against the
    wrong ones, silently."""
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path), *args)
    assert code == 1 and said in out, out
    assert "ca, us" in out, out
    assert not (tmp_path / "p").exists()


def test_new_refuses_a_project_and_writes_over_nothing(tmp_path):
    root = write_project(tmp_path / "p", name="kept")
    before = (root / "provenance.toml").read_text()
    code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us")
    assert code == 1 and "holds a provenance.toml already" in out, out
    assert (root / "provenance.toml").read_text() == before
    assert not (root / "template.md").exists()


@pytest.mark.parametrize("held", ["questions.json", "claims", "cache"])
def test_new_refuses_a_directory_holding_a_runs_files(tmp_path, held):
    """A project from before project files: writing a project file beside them would adopt it,
    which is a reviewed change, not a command's."""
    root = tmp_path / "old"
    (root / held).mkdir(parents=True) if "." not in held else (
        root.mkdir(), (root / held).write_text("[]"))
    code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us")
    assert code == 1 and f"holds a run's files already ({held})" in out, out
    assert "Moving a project laid out the old way" in out, out
    assert not (root / "provenance.toml").exists()


def test_new_keeps_a_template_already_in_place_and_refuses_another(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "template.md").write_text(TEMPLATE)
    code, out = _provenance("new", root, "--from", root / "template.md", "--source", "us")
    assert code == 0 and "already there" in out, out

    other = tmp_path / "q"
    other.mkdir()
    (other / "template.md").write_text("# Other questions\n")
    code, out = _provenance("new", other, "--from", _template(tmp_path), "--source", "us")
    assert code == 1 and "writes over no file" in out, out
    assert (other / "template.md").read_text() == "# Other questions\n"
    assert not (other / "provenance.toml").exists()


@pytest.mark.parametrize("text", ["", "  \n"], ids=["empty", "blank"])
def test_new_refuses_an_empty_template(tmp_path, text):
    t = tmp_path / "tpl.md"
    t.write_text(text)
    code, out = _provenance("new", tmp_path / "p", "--from", t, "--source", "us")
    assert code == 1 and "is empty" in out, out
    assert not (tmp_path / "p").exists()


def test_new_refuses_a_template_it_cannot_read(tmp_path):
    code, out = _provenance("new", tmp_path / "p", "--from", tmp_path / "missing.md",
                            "--source", "us")
    assert code == 1 and "can't be read as a template" in out, out
    assert not (tmp_path / "p").exists()


def test_a_project_file_any_command_would_refuse_is_taken_back_whole(tmp_path):
    """Read back as every command reads it. Refused, the file goes, and so does every directory
    created for it, so a retry meets nothing left over."""
    (tmp_path / "f").write_text("a file where the cache would go")
    code, out = _provenance("new", tmp_path / "a" / "b", "--from", _template(tmp_path),
                            "--source", "us", "--cache", tmp_path / "f")
    assert code == 1 and "`cache` needs" in out and "Nothing was written" in out, out
    assert not (tmp_path / "a").exists()


def test_new_refuses_a_subject_of_another_project(tmp_path):
    """The nearest project file wins, so one written into another project's subject would take
    that subject's run over."""
    parent = write_project(tmp_path / "parent", subjects=["lind"])
    code, out = _provenance("new", parent / "lind", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 1 and "is a subject of the project in" in out, out
    assert not (parent / "lind").exists()


def test_what_new_writes_into_the_project_file_reads_back_as_given(tmp_path):
    name = 'A "quoted" \\ name, and one astral character: \U0001d4d0'
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us", "--name", name)
    assert code == 0, out
    assert project.load(tmp_path / "p").name == name


@pytest.mark.parametrize(("flag", "value"), [("--name", "bell\x07"), ("--cache", "a\x1b[2Kb")])
def test_new_refuses_a_value_it_cannot_write_as_given(tmp_path, flag, value):
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us", flag, value)
    assert code == 1 and "can't be written as it is" in out, out
    assert "\x07" not in out and "\x1b" not in out
    assert not (tmp_path / "p").exists()


def test_ask_refuses_a_directory_whose_name_it_cannot_write(tmp_path, home):
    """The directory's name is the project's, written into its project file."""
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "a\x7fb")
    assert code == 1 and "can't be written as it is" in out and "a\\x7fb" in out, out
    assert not (tmp_path / "a\x7fb").exists()


def test_what_new_and_ask_print_comes_out_as_written(tmp_path, home):
    """Paths and the question are data: printed as they are, never read as markup (CLAUDE.md,
    "Rich reads brackets as markup")."""
    odd = tmp_path / "[/] [sic] [bold]x"
    code, out = _provenance("new", odd, "--from", _template(tmp_path), "--source", "us")
    assert code == 0, out
    assert f"created {odd.resolve()}" in out, out
    question = "What does [/] the [sic] record show?"
    code, out = _provenance("ask", question, "--source", "us", "--dir", tmp_path / "q")
    assert code == 0 and f"q1 (mechanical): {question}" in out, out


def test_new_in_the_working_directory_names_the_project_for_it(tmp_path, monkeypatch):
    root = tmp_path / "road-bond"
    root.mkdir()
    (root / "template.md").write_text(TEMPLATE)
    code, out = _provenance("new", ".", "--from", "template.md", "--source", "us",
                            cwd=root, monkeypatch=monkeypatch)
    assert code == 0, out
    assert project.load(root).name == "road-bond"


# --- ask ---------------------------------------------------------------------------------------


def test_ask_writes_a_project_holding_the_one_question(tmp_path, home, monkeypatch):
    code, out = _provenance("ask", QUESTION, "--source", "us", cwd=tmp_path,
                            monkeypatch=monkeypatch)
    assert code == 0, out
    [root] = tmp_path.glob("ask-*")
    assert re.fullmatch(r"ask-county-road-repair-bond-[0-9a-f]{16}", root.name), root.name
    assert json.loads((root / "questions.json").read_text()) == [
        {"id": "q1", "text": QUESTION, "claim_type": "mechanical", "parent": None,
         "rationale": "asked with provenance ask"}]
    p = project.load(root)
    assert (p.name, p.sources) == (root.name, ("us",))
    # The shared cache, declared in the file as it is written, never inferred.
    assert 'cache = "~/.cache/provenance"' in (root / "provenance.toml").read_text()
    assert p.cache == (home / ".cache" / "provenance").resolve()
    assert not (home / ".cache").exists(), "writing the project creates no cache"
    assert not (root / "template.md").exists()
    assert f"cd {p.root} && claude '{cli.SKILL} ask'" in out, out


def test_ask_takes_a_directory_a_cache_and_an_adversarial_question(tmp_path, home):
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "mine",
                            "--cache", tmp_path / "c", "--adversarial")
    assert code == 0, out
    assert json.loads((tmp_path / "mine" / "questions.json").read_text())[0]["claim_type"] \
        == "adversarial"
    assert project.load(tmp_path / "mine").cache == (tmp_path / "c").resolve()
    assert "two independent sources" in out, out


def test_record_questions_about_different_things_get_different_directories(tmp_path, home,
                                                                          monkeypatch):
    """Named by their first words, every question asking what the record shows was one
    directory, and one project name, so their reviews shared progress."""
    for q in ("What does the record show about the levy?",
              "What does the record show about the bond?"):
        code, out = _provenance("ask", q, "--source", "us", cwd=tmp_path,
                                monkeypatch=monkeypatch)
        assert code == 0, out
    names = sorted(p.name for p in tmp_path.glob("ask-*"))
    assert [re.sub(r"-[0-9a-f]{16}$", "", n) for n in names] == ["ask-bond", "ask-levy"], names


def test_questions_sharing_their_first_words_get_different_directories(tmp_path, home,
                                                                       monkeypatch):
    """The words are cut at six, so two questions can share them: the hash of the whole
    question keeps each its own directory and project name."""
    stem = "What does the record show about the county road repair bond vote"
    for q in (f"{stem} in 2029?", f"{stem} in 2030?"):
        code, out = _provenance("ask", q, "--source", "us", cwd=tmp_path,
                                monkeypatch=monkeypatch)
        assert code == 0, out
    names = [p.name for p in tmp_path.glob("ask-*")]
    assert len(names) == 2 and all(n.startswith("ask-county-road-repair-bond-vote-")
                                   for n in names), names


def test_the_hash_is_long_enough_that_questions_do_not_collide_on_it():
    """Cut to 24 bits, the hash put these two on one directory: their digests share their first
    six hex digits."""
    stem = "What does the record show about levy candidate county road repair bond vote"
    assert cli._ask_dir(f"{stem} 1198?") != cli._ask_dir(f"{stem} 9253?")


@pytest.mark.parametrize("kind", ["new", "ask"])
def test_a_relative_cache_is_read_from_the_working_directory(tmp_path, home, monkeypatch,
                                                            kind):
    """As every command's --cache is, and written relative to the project file, which is how
    the file reads it. Written as typed, one shared cache named from the directory holding the
    asks became a cache inside each one."""
    args = (("new", "p", "--from", _template(tmp_path)) if kind == "new"
            else ("ask", QUESTION, "--dir", "p"))
    code, out = _provenance(*args, "--source", "us", "--cache", "shared", cwd=tmp_path,
                            monkeypatch=monkeypatch)
    assert code == 0, out
    assert 'cache = "../shared"' in (tmp_path / "p" / "provenance.toml").read_text()
    assert project.load(tmp_path / "p").cache == (tmp_path / "shared").resolve()


def test_a_path_that_cannot_be_pasted_gets_no_command(tmp_path):
    """A command printed for a value is printed only when a pasted copy carries the value as it
    is (`queries.unprintable()`): a tab in the path would paste as another path, or as zsh's
    completion key."""
    code, out = _provenance("new", tmp_path / "a\tb" / "p", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 0, out
    assert "open Claude Code in that directory" in out and f"run {cli.SKILL}" in out, out
    assert "&& claude" not in out, out


def test_ask_takes_back_the_whole_project_when_it_is_refused(tmp_path, home):
    """The question set is written before the project is read back, so a refused project takes
    it back too, and a retry does not meet a directory holding half a project."""
    (tmp_path / "f").write_text("a file where the cache would go")
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "a" / "b",
                            "--cache", tmp_path / "f")
    assert code == 1 and "Nothing was written" in out, out
    assert not (tmp_path / "a").exists()
    assert not list(tmp_path.glob(".*")), "nothing is left beside it either"


def test_what_ask_leaves_is_the_project_and_nothing_beside_it(tmp_path, home):
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "q")
    assert code == 0, out
    assert sorted(p.name for p in (tmp_path / "q").iterdir()) == ["provenance.toml",
                                                                   "questions.json"]
    assert not list(tmp_path.glob(".*"))


def test_a_directory_made_meanwhile_is_refused_and_left_alone(tmp_path, home, monkeypatch):
    """ask's directory is created exclusively, so one another process makes between the check
    and the write is refused, never written into, and what it holds is kept."""
    real = cli._missing_dirs

    def racing(path):
        missing = real(path)
        (tmp_path / "q").mkdir()
        (tmp_path / "q" / "theirs.txt").write_text("another writer's file")
        return missing

    monkeypatch.setattr(cli, "_missing_dirs", racing)
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "q")
    assert code == 1 and "could not create" in out, out
    assert sorted(p.name for p in (tmp_path / "q").iterdir()) == ["theirs.txt"]


def _interrupted_at_the_project_file(monkeypatch, *args) -> None:
    """Run a command killed as it installs provenance.toml, the last file it writes: its
    content is written out, and the link into place never happens."""
    import os

    real = os.link

    def killed(src, dst, *a, **kw):
        if Path(dst).name == "provenance.toml":
            raise KeyboardInterrupt
        return real(src, dst, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr(os, "link", killed)
        CliRunner().invoke(cli.app, [str(a) for a in args])


def test_an_interrupted_new_leaves_no_project_and_runs_again(tmp_path, monkeypatch):
    """The project file is written last, so an interrupted run leaves none, and the retry finds
    the same template and finishes."""
    root = tmp_path / "p"
    args = ("new", root, "--from", _template(tmp_path), "--source", "us")
    _interrupted_at_the_project_file(monkeypatch, *args)
    assert not (root / "provenance.toml").exists()
    code, out = _provenance(*args)
    assert code == 0 and "already there" in out, out
    assert project.load(root).name == "p"


def test_a_partial_file_a_killed_run_left_is_never_read_or_in_the_way(tmp_path):
    """Files are written beside their names and linked into place whole, so a run killed while
    writing leaves only a temporary dotfile: never a partial provenance.toml that every command
    would read as the project, nor a partial template that refuses the retry."""
    root = tmp_path / "p"
    root.mkdir()
    (root / ".provenance.toml.abc123.tmp").write_text('name = "half')
    (root / ".template.md.def456.tmp").write_text("# Quest")
    code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us")
    assert code == 0, out
    assert (root / "template.md").read_text() == TEMPLATE
    assert project.load(root).name == "p"


def test_a_filesystem_without_hard_links_still_gets_its_project(tmp_path, monkeypatch):
    import errno
    import os

    def no_links(src, dst, *a, **kw):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(os, "link", no_links)
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 0, out
    assert project.load(tmp_path / "p").name == "p"
    assert sorted(p.name for p in (tmp_path / "p").iterdir()) == ["provenance.toml",
                                                                   "template.md"]


def test_a_failed_write_on_a_filesystem_without_hard_links_leaves_no_partial_file(
        tmp_path, monkeypatch):
    import errno
    import os

    real_fsync, calls = os.fsync, []

    def no_links(src, dst, *a, **kw):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    def fsync(fd):
        calls.append(fd)
        if len(calls) == 2:   # the temporary file's goes through; the fallback's fails
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "link", no_links)
    monkeypatch.setattr(os, "fsync", fsync)
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 1 and "could not write" in out, out
    assert not (tmp_path / "p").exists(), "the partial template and the directory are gone"


def test_a_parent_another_process_made_meanwhile_is_left_to_it(tmp_path, home, monkeypatch):
    """Only the directories this run made are taken back when it is refused."""
    real = cli._missing_dirs

    def racing(path):
        missing = real(path)
        if path == tmp_path / "theirs":
            (tmp_path / "theirs").mkdir()
        return missing

    (tmp_path / "f").write_text("a file where the cache would go")
    monkeypatch.setattr(cli, "_missing_dirs", racing)
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir",
                            tmp_path / "theirs" / "q", "--cache", tmp_path / "f")
    assert code == 1 and "Nothing was written" in out, out
    assert (tmp_path / "theirs").is_dir() and not (tmp_path / "theirs" / "q").exists()


def test_run_files_that_appear_before_the_project_file_are_not_adopted(tmp_path, monkeypatch):
    """A run's files that appeared after `new` looked, by the time its project file did, take
    the project back: writing it beside them would adopt them."""
    root = tmp_path / "p"
    root.mkdir()
    real = cli._install

    def racing(path, body):
        ident = real(path, body)
        if path.name == "template.md":
            (root / "claims").mkdir()
        return ident

    monkeypatch.setattr(cli, "_install", racing)
    code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us")
    assert code == 1 and "holds a run's files already (claims)" in out, out
    assert sorted(p.name for p in root.iterdir()) == ["claims"]


def test_a_refused_scaffold_leaves_a_file_another_process_put_in_its_place(tmp_path,
                                                                           monkeypatch):
    """Cleanup removes a file only while it is still the one this run installed."""
    root = tmp_path / "p"
    root.mkdir()
    real = cli._install

    def racing(path, body):
        ident = real(path, body)
        if path.name == "template.md":
            theirs = root / ".theirs"
            theirs.write_text("another writer's template")
            theirs.replace(path)
            (root / "claims").mkdir()
        return ident

    monkeypatch.setattr(cli, "_install", racing)
    code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us")
    assert code == 1 and "holds a run's files already (claims)" in out, out
    assert (root / "template.md").read_text() == "another writer's template"
    assert not (root / "provenance.toml").exists()


def test_a_parent_that_turns_up_as_a_symlink_is_refused(tmp_path, home, monkeypatch):
    """A parent found missing that another process makes meanwhile is left to it, but not
    followed if it is a link: the project would land wherever it points."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = cli._missing_dirs

    def racing(path):
        missing = real(path)
        if path == tmp_path / "parent":
            (tmp_path / "parent").symlink_to(elsewhere)
        return missing

    monkeypatch.setattr(cli, "_missing_dirs", racing)
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir",
                            tmp_path / "parent" / "q")
    assert code == 1 and "as a symlink" in out, out
    assert list(elsewhere.iterdir()) == []


def test_a_new_directory_made_meanwhile_is_refused_not_written_into(tmp_path, monkeypatch):
    """`new` found no directory, so it creates one, exclusively: one made since, holding a run's
    files, is not a directory `new` checked, and a project file beside them would adopt them."""
    real = cli._missing_dirs

    def racing(path):
        missing = real(path)
        (tmp_path / "p" / "claims").mkdir(parents=True)
        return missing

    monkeypatch.setattr(cli, "_missing_dirs", racing)
    code, out = _provenance("new", tmp_path / "p", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 1 and "could not create" in out, out
    assert sorted(p.name for p in (tmp_path / "p").iterdir()) == ["claims"]


def test_a_crlf_template_is_copied_as_given_and_a_retry_finds_it_the_same(tmp_path,
                                                                          monkeypatch):
    """Read as text, CRLF came back as LF, so the copy differed from --from, and after an
    interrupted run the retry refused its own template as not the one given."""
    t = tmp_path / "tpl.md"
    t.write_bytes(b"# Questions\r\n\r\n1. What does the record show?\r\n")
    root = tmp_path / "p"
    args = ("new", root, "--from", t, "--source", "us")
    _interrupted_at_the_project_file(monkeypatch, *args)
    assert (root / "template.md").read_bytes() == t.read_bytes()
    code, out = _provenance(*args)
    assert code == 0 and "already there" in out, out


def test_an_interrupted_ask_leaves_no_project_and_says_so(tmp_path, home, monkeypatch):
    root = tmp_path / "q"
    args = ("ask", QUESTION, "--source", "us", "--dir", root)
    _interrupted_at_the_project_file(monkeypatch, *args)
    assert not (root / "provenance.toml").exists()
    assert project.find(root) == tmp_path, "what it left is no project of its own"
    code, out = _provenance(*args)
    assert code == 1 and "if an interrupted `provenance ask` left it, remove it" in out, out


def test_new_refuses_a_symlink_to_another_projects_subject(tmp_path):
    """Written through the link, the project file landed in the other project's subject, which
    then refused every command at the other project. Whether a project declares a directory is
    asked of the path as given, and the link's parent declares nothing."""
    parent = write_project(tmp_path / "parent", subjects=["lind"])
    (parent / "lind").mkdir()
    (tmp_path / "alias").symlink_to(parent / "lind")
    code, out = _provenance("new", tmp_path / "alias", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 1 and "is a symlink" in out, out
    assert list((parent / "lind").iterdir()) == []


def test_new_through_a_symlinked_parent_is_asked_of_the_directory_it_names(tmp_path):
    """A subject is declared by the directory above it, and the path as given reads that
    directory through the link: so a project written through a link to another project's
    root, at one of its subjects, is refused. Below a subject, it is a project nested in that
    directory, as `new` there without the link would make."""
    parent = write_project(tmp_path / "parent", subjects=["lind"])
    (tmp_path / "alias").symlink_to(parent)
    code, out = _provenance("new", tmp_path / "alias" / "lind", "--from", _template(tmp_path),
                            "--source", "us")
    assert code == 1 and "is a subject of the project in" in out, out
    assert not (parent / "lind").exists()


def test_a_cache_in_a_symlink_loop_is_refused_not_a_traceback(tmp_path, home, monkeypatch):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", "q", "--cache", "loop",
                            cwd=tmp_path, monkeypatch=monkeypatch)
    assert code == 1 and "--cache loop can't be resolved" in out, out
    assert not (tmp_path / "q").exists()


def test_a_directory_that_cannot_be_made_is_refused_as_such(tmp_path, home):
    (tmp_path / "f").write_text("a file, not a directory")
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "f" / "x")
    assert code == 1 and "could not create" in out, out
    assert "provenance.toml already" not in out, out
    assert (tmp_path / "f").read_text() == "a file, not a directory"


def test_ask_never_adds_to_a_directory_that_exists(tmp_path, home):
    """Always a new project: never a question added to one that exists, nor a project written
    into a directory holding anything, an empty one included."""
    root = write_project(tmp_path / "p")
    before = sorted(root.iterdir())
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", root)
    assert code == 1 and "exists" in out, out
    assert sorted(root.iterdir()) == before

    (tmp_path / "empty").mkdir()
    code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", tmp_path / "empty")
    assert code == 1 and "exists" in out, out
    assert list((tmp_path / "empty").iterdir()) == []


def test_asking_again_refuses_the_first_asks_directory(tmp_path, home, monkeypatch):
    assert _provenance("ask", QUESTION, "--source", "us", cwd=tmp_path,
                       monkeypatch=monkeypatch)[0] == 0
    code, out = _provenance("ask", QUESTION, "--source", "us")
    assert code == 1 and "Pass another --dir" in out, out


def test_ask_needs_a_directory_when_the_question_names_none(tmp_path, home, monkeypatch):
    code, out = _provenance("ask", "記録は何を示すか", "--source", "us", cwd=tmp_path,
                            monkeypatch=monkeypatch)
    assert code == 1 and "pass --dir" in out, out


def test_asks_default_directory_folds_accents(tmp_path, home, monkeypatch):
    code, out = _provenance("ask", "¿Qué muestra el registro sobre la ordenanza?", "--source",
                            "us", cwd=tmp_path, monkeypatch=monkeypatch)
    assert code == 0, out
    assert len(list(tmp_path.glob("ask-que-muestra-el-registro-sobre-la-*"))) == 1


@pytest.mark.parametrize(("args", "said"), [
    (("  ", "--source", "us"), "the question is empty"),
    (("What is\x1b[2K on record?", "--source", "us"), "can't be written as it is"),
    ((QUESTION,), "--source is required"),
], ids=["empty", "control", "no-source"])
def test_ask_refuses_what_it_cannot_ask(tmp_path, home, monkeypatch, args, said):
    code, out = _provenance("ask", *args, cwd=tmp_path, monkeypatch=monkeypatch)
    assert code == 1 and said in out, out
    assert "\x1b[2K" not in out
    assert not list(tmp_path.glob("ask-*"))


# --- the hand-off ------------------------------------------------------------------------------


def test_the_hand_off_invokes_a_skill_the_plugin_ships():
    """Renaming the skill fails here until `cli.SKILL` follows it."""
    shipped = {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}
    assert cli.SKILL.removeprefix("/provenance:") in shipped, (cli.SKILL, shipped)
    text = (ROOT / "skills" / cli.SKILL.removeprefix("/provenance:") / "SKILL.md").read_text()
    assert "provenance new" in text and "provenance ask" in text
    assert "invokes this skill with `ask`" in text


# --- end to end --------------------------------------------------------------------------------


def _cache(root: Path) -> None:
    page = PageCache(url=URL, final_url=URL, status=200, content_type="text/html", title="Bond",
                     text=STORY, fetched_at=datetime.now(UTC) - timedelta(hours=1),
                     extractor_version=EXTRACTOR_VERSION)
    cache_path(root, URL).write_text(page.model_dump_json())


def _started(kind: str, tmp_path: Path) -> Path:
    """A project as `new` or `ask` leaves it, with the question set the skill works from:
    `ask` writes it, and for `new` the skill's Phase 0 does."""
    root, cache = tmp_path / kind, tmp_path / "shared"
    if kind == "new":
        code, out = _provenance("new", root, "--from", _template(tmp_path), "--source", "us",
                                "--cache", cache)
        assert code == 0, out
        (root / "questions.json").write_text(json.dumps(
            [{"id": "q1", "text": QUESTION, "claim_type": "mechanical"}]))
    else:
        code, out = _provenance("ask", QUESTION, "--source", "us", "--dir", root,
                                "--cache", cache)
        assert code == 0, out
    return root


@pytest.mark.parametrize("kind", ["new", "ask"])
def test_a_started_project_runs_end_to_end(tmp_path, home, kind):
    """The toy question through the whole loop, offline, as the skill drives it: a researcher's
    claim through the check-claim gate, verify, a fresh verifier's verdict from the hand-off,
    the judgments gate at 0, and a review page."""
    root = _started(kind, tmp_path)
    _cache(tmp_path / "shared")
    (root / "claims").mkdir()
    claim = root / "claims" / "q1.json"
    # The outlet is on no source list, so the answer states what it reports, not the fact alone.
    claim.write_text(Claim(
        question_id="q1", question=QUESTION,
        answer="The Harbor Ledger reports the supervisors approved it 4-1, sending it to the "
               "ballot.",
        sources=[Source(url=URL, publisher="Harbor Ledger", author="R. Reporter",
                        date="2030-05-06", source_type="bylined_journalism", snippet=SNIPPET)],
    ).model_dump_json())

    code, out = _provenance("check-claim", claim)
    assert code == 0, out
    code, out = _provenance("verify", "--project", root)
    assert code == 0, out

    code, out = _provenance("handoff", "q1", "--project", root)
    assert code == 0, out
    sid, token = re.search(r"sid (\w+)  context token (\w+)", out).groups()
    code, out = _provenance("judge", "q1", sid, "supports", "--context", token,
                            "--note", "the minutes give the vote", "--project", root)
    assert code == 0, out
    code, out = _provenance("judgments", "--project", root)
    assert code == 0 and "0 of 1 cited source(s) need a verdict" in out, out

    code, out = _provenance("build", "--project", root)
    assert code == 0, out
    assert (root / "out" / "review.html").is_file()
    [built] = json.loads((root / "out" / "claims.json").read_text())
    assert built["status"] == "verified", built
