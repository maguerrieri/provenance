"""What a source list's records are like reaches researchers and verifiers through the brief.

Which filings come in series, which portals answer only through a bulk export, and how one
state's campaign-finance export counts: that is domain content, and the core skill and agents
carry none of it. It ships beside the source list it belongs to, as `<name>-notes.md`, and
`provenance brief` appends the notes of every list a project names. So a project that names
`ca` gets the California notes, and one that doesn't never sees them, with no prose deciding
when the work "touches the domain".
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import EXAMPLE_CHECK, write_project
from typer.testing import CliRunner

from provenance import cli, project, sources

HEADING = "Notes that ship with the `ca` source list (the tool's, not this project's):"


def _brief(root: Path, *args: str) -> str:
    here = Path.cwd()
    os.chdir(root)
    try:
        res = CliRunner().invoke(cli.app, ["brief", *args])
    finally:
        os.chdir(here)
    assert res.exit_code == 0, res.output
    return res.stdout


def test_a_project_naming_ca_gets_the_ca_notes_in_its_brief(tmp_path):
    root = write_project(tmp_path / "p", sources=("us", "ca"), subjects=["lind"])
    for args in ((), ("--subject", "lind")):
        out = _brief(root, *args)
        assert HEADING in out, out
        assert "provenance form700" in out and "billStatusClient.xhtml" in out, out
        # after the project's own context, never before it
        assert out.index("appeals board") < out.index(HEADING), out


def test_a_project_not_naming_ca_gets_no_ca_notes(tmp_path):
    root = write_project(tmp_path / "p", sources=("us",))
    out = _brief(root)
    assert "Notes that ship with" not in out, out
    assert "provenance form700" not in out and "billStatusClient.xhtml" not in out, out


def test_the_notes_leave_out_the_maintainers_comment():
    [(name, text)] = sources.notes(("us", "ca"))
    assert name == "ca"
    assert not text.startswith("<!--") and "-->" not in text, text[:200]
    assert "#22" not in text, "the packs note is for the tool's maintainers, not a researcher"
    assert "<!--" in (sources.SOURCES_DIR / "ca-notes.md").read_text(), "the comment is there"


def test_notes_are_read_in_the_projects_order_once_each(tmp_path):
    (tmp_path / "a-notes.md").write_text("<!-- for maintainers -->\n\nAbout a.\n")
    (tmp_path / "b-notes.md").write_text("About b.\n")
    (tmp_path / "c-notes.md").write_text("<!-- only a comment -->\n")
    got = sources.notes(("b", "a", "b", "c", "d"), sources_dir=tmp_path)
    assert got == [("b", "About b."), ("a", "About a.")]


def test_the_completeness_check_stays_out_of_a_brief_with_notes(tmp_path):
    """The notes are the tool's text, appended after the project's: adding them must not open a
    way for the project's known answers into a prompt."""
    sentinel = "SENTINEL-KNOWN-ANSWER"
    root = write_project(tmp_path / "p", sources=("us", "ca"),
                         completeness_check=f"{EXAMPLE_CHECK}\n- {sentinel}\n")
    out = _brief(root)
    assert HEADING in out and sentinel not in out, out
    assert sentinel not in cli.researcher_brief(project.load(root), None)


def test_every_notes_file_belongs_to_a_source_list():
    """A notes file with no list beside it is one no project can name, so nobody is told it."""
    listed = set(sources.available())
    for p in sources.SOURCES_DIR.glob("*-notes.md"):
        assert p.name.removesuffix("-notes.md") in listed, p.name
