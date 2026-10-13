"""Build the HACS release ZIP with the published tag as its installed version."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

DOMAIN = "bermuda"
COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
VERSION_PATTERN = re.compile(r"[vV]?(\d+\.\d+(?:\.\d+)?(?:(?:a|b|rc)\d+|-(?:alpha|beta|rc)\.?\d+)?)")


def release_version(tag: str) -> str:
    """Accept numeric release tags and conventional alpha/beta/RC versions."""
    match = VERSION_PATTERN.fullmatch(tag)
    if match is None:
        msg = "Use a version tag such as v1.2.3 or v1.2.3-beta.1"
        raise ValueError(msg)
    return match.group(1)


def build_release(tag: str, output: Path) -> None:
    """Stamp archive contents without changing the checkout or integration domain."""
    version = release_version(tag)
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    if manifest.get("domain") != DOMAIN:
        msg = "Release component domain does not match its HACS package"
        raise ValueError(msg)
    manifest["version"] = version
    files = sorted(
        path
        for path in COMPONENT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.resolve() != output.resolve()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            relative = path.relative_to(COMPONENT).as_posix()
            data = path.read_bytes()
            if relative == "manifest.json":
                data = (json.dumps(manifest, indent=2) + "\n").encode()
            if DOMAIN == "bermuda" and relative == "const.py":
                text, count = re.subn(r"^VERSION\s*=.*$", f'VERSION = "{version}"', data.decode(), flags=re.MULTILINE)
                if count != 1:
                    msg = "Bermuda runtime version declaration is missing or ambiguous"
                    raise ValueError(msg)
                data = text.encode()
            archive.writestr(relative, data)
    with ZipFile(output) as archive:
        if json.loads(archive.read("manifest.json"))["version"] != version or archive.testzip() is not None:
            msg = "Release archive failed its version or integrity check"
            raise ValueError(msg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_release(args.tag, args.output)
