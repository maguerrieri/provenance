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
    root = tmp_path / "ask-county-road-repair-bond"
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
    assert sorted(p.name for p in tmp_path.glob("ask-*")) == ["ask-bond", "ask-levy"]


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
    assert (tmp_path / "ask-que-muestra-el-registro-sobre-la").is_dir()


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
    claim.write_text(Claim(
        question_id="q1", question=QUESTION,
        answer="The supervisors approved it 4-1 and sent it to the ballot.",
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
