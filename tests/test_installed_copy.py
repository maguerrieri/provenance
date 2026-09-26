"""An installed copy (`uv tool install`) runs from outside the repo, so what it reads ships in
the package.

The review page's template and the source lists and access registry were found beside the
checkout (`parents[2]`), which a wheel doesn't have. They are package data now, found through
importlib.resources.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from provenance import access, report, sources

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "provenance"
DATA = ("templates", "source_lists", "source_access")


def test_the_data_is_found_in_the_package():
    assert report.TEMPLATES == PACKAGE / "templates"
    assert sources.SOURCES_DIR == PACKAGE / "source_lists"
    assert access.REGISTRY == PACKAGE / "source_access"
    assert (report.TEMPLATES / "review.html.j2").is_file()
    assert {"us", "ca"} <= set(sources.available())
    assert access.load_all(), "the shipped access registry reads as empty"


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

