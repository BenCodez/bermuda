"""Verify the actual HACS archive contract without publishing a release."""

import ast
import json
from pathlib import Path
import runpy
from zipfile import ZipFile

import pytest

PACKAGE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"))


@pytest.mark.parametrize("tag,version", [("v9.8.7", "9.8.7"), ("9.8", "9.8"), ("9.8.7-beta.1", "9.8.7-beta.1")])
def test_release_archive_matches_tag_and_preserves_checkout(tmp_path, tag, version):
    component = PACKAGE["COMPONENT"]
    before = (component / "manifest.json").read_bytes()
    runtime_before = (component / "const.py").read_bytes()
    output = tmp_path / "integration.zip"
    PACKAGE["build_release"](tag, output)
    with ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == version
        assert manifest["domain"] == PACKAGE["DOMAIN"]
        assert "__init__.py" in archive.namelist()
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in archive.namelist())
        if PACKAGE["DOMAIN"] == "bermuda":
            tree = ast.parse(archive.read("const.py"))
            values = [
                ast.literal_eval(node.value)
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)
            ]
            assert values == [version]
        assert archive.testzip() is None
    assert (component / "manifest.json").read_bytes() == before
    assert (component / "const.py").read_bytes() == runtime_before


@pytest.mark.parametrize("tag", ["main", "", "v1.2.3;command", "v1.2.3\n", "../1.2.3"])
def test_nonversion_tags_cannot_publish_an_archive(tmp_path, tag):
    output = tmp_path / "integration.zip"
    with pytest.raises(ValueError):
        PACKAGE["build_release"](tag, output)
    assert not output.exists()
