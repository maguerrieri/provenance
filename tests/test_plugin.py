"""The Claude Code plugin: its manifest, its agents and skill, and the version it shares with
the CLI.

`claude plugin install` reads `.claude-plugin/plugin.json` and finds `agents/*.md` and
`skills/<name>/SKILL.md` beside it. Nothing in CI runs Claude Code, so these tests hold the
files to what it needs. They also hold the plugin's version to the package's: the skill checks
`provenance --version` against it before a run, and tells the operator to install the CLI at
that version, since a CLI of another version can lack a command or flag the agents run.
"""

import json
import re
import tomllib
from pathlib import Path

import yaml
from typer.testing import CliRunner

from provenance import cli

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
AGENTS = sorted((ROOT / "agents").glob("*.md"))
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path}'s frontmatter is never closed"
    return yaml.safe_load(head), body


def test_the_plugin_is_named_for_the_command_and_versioned_with_the_package():
    assert PLUGIN["name"] == "provenance"
    assert PLUGIN["version"] == VERSION, (
        "a release bumps .claude-plugin/plugin.json and pyproject.toml together")


def test_provenance_version_prints_the_package_version():
    res = CliRunner().invoke(cli.app, ["--version"])
    assert res.exit_code == 0, res.output
    assert res.output == f"provenance {VERSION}\n"


def test_provenance_version_without_an_install_says_so(monkeypatch):
    def missing(name):
        raise cli.PackageNotFoundError(name)
    monkeypatch.setattr(cli, "package_version", missing)
    res = CliRunner().invoke(cli.app, ["--version"])
    assert res.exit_code == 1
    assert "no installed version" in res.output and "Traceback" not in res.output


def test_the_skill_checks_the_cli_is_at_the_plugins_version_first():
    """The check, what it must print, and the install command it gives name one version, the
    plugin's, and so does the README's install command. A version left behind by a release
    would send every operator to install a CLI whose commands the agents' instructions no
    longer match."""
    checks = [p for p in SKILLS if "provenance --version" in p.read_text()]
    assert checks, "no skill checks provenance --version before a run"
    named = [(p.relative_to(ROOT), v) for p in [*SKILLS, *AGENTS, ROOT / "README.md"]
             for v in re.findall(r"provenance(?: |@v)(\d+(?:\.\d+)+)", p.read_text())]
    assert named, "the check names no version"
    assert {v for _, v in named} == {VERSION}, named
    for path in checks:
        assert (f"uv tool install --force git+https://github.com/maguerrieri/provenance@v{VERSION}"
                in path.read_text()), f"{path} gives no install command for v{VERSION}"


def test_every_agent_resolves_and_can_run_what_it_is_told_to():
    """An agent's name is its file's, and one told to run `provenance` has Bash to run it with:
    a verifier without Bash once went idle having recorded nothing (CLAUDE.md, "An agent's
    declared tools must match what you told it to do")."""
    assert AGENTS, "the plugin ships no agents"
    for path in AGENTS:
        meta, body = _frontmatter(path)
        assert meta["name"] == path.stem, path
        assert meta.get("description"), path
        tools = {t.strip() for t in str(meta.get("tools", "")).split(",")}
        if re.search(r"^\s*provenance [a-z]", body, re.MULTILINE):
            assert "Bash" in tools, f"{path} runs provenance without Bash"


def test_every_skill_resolves():
    assert SKILLS, "the plugin ships no skills"
    for path in SKILLS:
        meta, _ = _frontmatter(path)
        assert meta["name"] == path.parent.name, path
        assert meta.get("description"), path


def test_the_skill_spawns_the_plugins_agents_by_their_plugin_names():
    """Installed, a plugin's agents are `provenance:<name>`. A bare `researcher` names no agent
    outside this repo, or a user's own agent that happens to have the name."""
    names = {p.stem for p in AGENTS}
    spawned = set()
    for path in SKILLS:
        text = path.read_text()
        spawned |= set(re.findall(r"`provenance:([\w-]+)`", text))
        for name in names:
            assert f"`{name}`" not in text, f"{path} names the {name} agent without its plugin"
    assert spawned, "the skill spawns none of the plugin's agents"
    assert spawned <= names, f"the skill spawns agents the plugin lacks: {spawned - names}"


def test_nothing_a_user_or_agent_reads_says_uv_run_provenance():
    """`uv run provenance` works only inside a clone of this repo. A research project lives
    anywhere else, with the command installed on PATH, so everything a user or agent reads or
    runs says `provenance`: the agents and skill, the CLI's messages, and the commands the review
    page prints. `uv run` is for developing the tool: tests, CI and contributor docs."""
    shipped = [*AGENTS, *SKILLS, *(ROOT / "src" / "provenance").rglob("*")]
    said = [p.relative_to(ROOT) for p in shipped
            if p.is_file() and "__pycache__" not in p.parts
            and "uv run provenance" in p.read_text(errors="replace")]
    assert not said, said


# Terms of the one domain the tool has shipped with: its jurisdiction's portals and outlets,
# its campaign-finance export and its election records.
DOMAIN = ("CAL-ACCESS", "calaccess", "FPPC", "Form 700", "form700", "Form 460", "Form 496",
          "Form 497", "leginfo", "California", "CalMatters", "Ballotpedia", "candidate",
          "election")


def test_the_core_skill_and_agents_carry_no_domain_content():
    """Which filings come in series, which portals answer only through a bulk export, one
    state's campaign-finance export: that ships beside its source list (`<name>-notes.md`) and
    reaches researchers and verifiers through `provenance brief`, so a project that doesn't name
    the list never reads it (#11). One of its terms back in the skill or an agent is domain
    content in the core."""
    for path in [*SKILLS, *AGENTS]:
        text = path.read_text()
        # whole words, so "selection" or "candidates for a retry" is not a domain term
        found = [t for t in DOMAIN
                 if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)]
        assert not found, f"{path.relative_to(ROOT)} names {found}: put it in a list's notes"


def test_the_skill_hands_the_brief_to_researchers_and_verifiers():
    """The brief is the one channel the notes travel by, so a verifier without it judges a
    series filing without being told the series exists."""
    [skill] = [p for p in SKILLS if p.parent.name == "research"]
    text = " ".join(skill.read_text().split())
    assert "paste its output verbatim into every researcher and verifier prompt" in text
    assert "Give it the run's brief, verbatim, as for a researcher" in text
    for path in AGENTS:
        assert "notes that ship with the project's source lists" in " ".join(
            path.read_text().split()), f"{path.relative_to(ROOT)} is not told its brief has notes"
