"""An installed copy (`uv tool install`) runs from outside the repo, so what it reads ships in
the package, and what the access registry commands would write into it is printed instead.

The review page's template and the source lists and access registry were found beside the
checkout (`parents[2]`), which a wheel doesn't have. They are package data now, found through
importlib.resources. The registry is also written to, by `source-note` and
`source-import-curl`, and in an installed copy that write would land in the tool's own
environment, where the next install deletes it without a word.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from provenance import access, cli, report, sources

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "provenance"
DATA = ("templates", "source_lists", "source_access")
URL = "https://portal.example/api/search?name=Doe"


def test_the_data_is_found_in_the_package():
    # Resolved on both sides: a clone under a symlinked path (macOS /tmp) is found unresolved.
    assert report.TEMPLATES.resolve() == PACKAGE / "templates"
    assert sources.SOURCES_DIR.resolve() == PACKAGE / "source_lists"
    assert access.REGISTRY.resolve() == PACKAGE / "source_access"
    assert (report.TEMPLATES / "review.html.j2").is_file()
    assert {"us", "ca"} <= set(sources.available())
    assert access.load_all(), "the shipped access registry reads as empty"


def test_nothing_the_package_reads_is_found_beside_the_checkout():
    """A path built from `__file__` up past the package (`parents[2]`) is the repo root in a
    checkout and a directory inside the tool's environment in an installed copy, and every test
    passes, because tests run from the checkout. Package data goes through importlib.resources.
    races/ is the one left, until #8 replaces RACES_DIR with the project file's race key."""
    found = {p.name for p in PACKAGE.glob("*.py") if "__file__" in p.read_text()}
    assert found == {"races.py"}, found


def test_the_wheel_carries_the_data(tmp_path):
    """uv_build puts only the module root in the wheel, which is why the data moved under it.
    Built for real, since a file left outside src/provenance/ is exactly what a check of the
    source tree can't see."""
    uv = shutil.which("uv")
    if not uv:
        if os.environ.get("CI"):
            pytest.fail("uv is not installed, and CI must build the wheel")
        pytest.skip("uv is not installed")
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
                   capture_output=True, check=True)
    [wheel] = tmp_path.glob("provenance-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    want = {f"provenance/{p.relative_to(PACKAGE).as_posix()}"
            for d in DATA for p in (PACKAGE / d).rglob("*") if p.is_file()}
    assert want and want <= names, sorted(want - names)


def _install(monkeypatch, direct_url):
    class Dist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return direct_url
    monkeypatch.setattr(access, "distribution", lambda name: Dist())


@pytest.mark.parametrize("direct_url, installed", [
    ('{"url": "file:///src/provenance", "dir_info": {"editable": true}}', False),
    ('{"url": "https://github.com/maguerrieri/provenance", "vcs_info": {"vcs": "git"}}', True),
    ('{"url": "file:///src/provenance", "dir_info": {}}', True),
    ('{"url": "file:///src/provenance", "dir_info": {"editable": "true"}}', True),
    (None, True),
    ("not json", True),
    ("[]", True),
])
def test_only_an_editable_install_is_a_checkout(monkeypatch, direct_url, installed):
    """`uv sync` installs the checkout editable; `uv tool install git+…` records the git
    source. Anything the install can't say reads as installed: a refused write costs a paste,
    and a lost one costs the finding."""
    _install(monkeypatch, direct_url)
    assert access.installed_copy() is installed


def test_this_checkout_is_one():
    assert access.installed_copy() is False, "uv sync installs the checkout editable"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    reg = tmp_path / "access"
    monkeypatch.setattr(access, "REGISTRY", reg)
    monkeypatch.setattr(access, "installed_copy", lambda: True)
    return reg


def test_an_installed_copy_prints_a_note_instead_of_writing_it(installed):
    r = CliRunner().invoke(cli.app, ["source-note", "portal.example", "needs a session"],
                           terminal_width=200)
    assert r.exit_code == 1, r.output
    assert not installed.exists()
    assert "not written: this provenance is an installed copy" in r.output
    assert "src/provenance/source_access/portal.example.yaml" in r.output
    assert "host: portal.example" in r.output and "findings: needs a session" in r.output


def test_a_host_with_an_entry_is_merged_not_replaced(installed):
    """Printed from an installed copy, the entry is that install's copy with the note added. The
    repo's may have findings the install lacks, so pasting it over the file would drop them."""
    installed.mkdir()
    (installed / "portal.example.yaml").write_text("host: portal.example\nfindings: older\n")
    r = CliRunner().invoke(cli.app, ["source-note", "portal.example", "newer"],
                           terminal_width=200)
    assert r.exit_code == 1, r.output
    assert "merge what is new into src/provenance/source_access/portal.example.yaml" in r.output
    assert "older" in r.output and "newer" in r.output
    assert (installed / "portal.example.yaml").read_text().endswith("findings: older\n")


@pytest.mark.parametrize("host", ["../outside", "../../outside", "sub/outside", "/tmp/outside"])
def test_a_host_that_is_a_path_names_no_entry(installed, tmp_path, host):
    """source-note joins the host into a file name. With a "/" in it, the file was outside the
    registry: read, printed in full by the installed-copy refusal, or rewritten in a checkout."""
    outside = tmp_path / "outside.yaml"
    outside.write_text("secret: canary-value\n")
    for installed_copy in (True, False):
        with pytest.MonkeyPatch.context() as m:
            m.setattr(access, "installed_copy", lambda: installed_copy)
            r = CliRunner().invoke(cli.app, ["source-note", host, "x"], terminal_width=200)
        assert r.exit_code == 1, r.output
        assert "is not a host name" in r.output
        assert "canary-value" not in r.output
        assert outside.read_text() == "secret: canary-value\n"


def test_an_installed_copy_prints_an_import_instead_of_writing_it(installed, tmp_path):
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl '{URL}' -H 'X-CSRF-Token: fake session value' -H 'accept: */*'\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)], terminal_width=200)
    assert r.exit_code == 1, r.output
    assert not installed.exists()
    assert "src/provenance/source_access/portal.example.yaml" in r.output
    assert "https://portal.example/api/search" in r.output
    # printed only after every check a write passes: the session header is dropped from it
    assert "fake session value" not in r.output


def test_printing_is_no_way_past_a_refused_paste(installed, tmp_path):
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl --user canary-user:changeme {URL}\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste)], terminal_width=200)
    assert r.exit_code == 1, r.output
    assert "changeme" not in r.output
    assert "not written" not in r.output, "refused by the import's own check, before any print"
    assert not installed.exists()


def test_no_write_still_prints_in_an_installed_copy(installed, tmp_path):
    paste = tmp_path / "paste.txt"
    paste.write_text(f"curl '{URL}'\n")
    r = CliRunner().invoke(cli.app, ["source-import-curl", str(paste), "--no-write"],
                           terminal_width=200)
    assert r.exit_code == 0, r.output
    assert "not written" not in r.output
    assert "https://portal.example/api/search" in r.output


def test_the_refusal_prints_the_host_as_data(installed, monkeypatch):
    """The host is typed by a person or an agent, and the refusal names the file it would be."""
    monkeypatch.setattr(cli.con, "_color_system", None)   # rich's own escapes, not the host's
    r = CliRunner().invoke(cli.app, ["source-note", "portal\x1b[2K.example", "x"],
                           terminal_width=200)
    assert r.exit_code == 1, r.output
    assert "\x1b" not in r.output
    assert "source_access/portal\\x1b[2k.example.yaml" in r.output
