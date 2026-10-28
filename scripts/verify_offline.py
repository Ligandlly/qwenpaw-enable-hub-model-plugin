#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate that a built plugin ZIP can be installed on an offline host.

The archive is the only thing an air-gapped deployment receives, so this check
refuses anything that would need the network or a package index at install
time:

* a single top-level directory matching ``plugin.json`` ``id``,
* manifest, entry backend, README and LICENSE all present,
* no wheels / sdists / vendored binaries / byte-code / lock files,
* every ``.py`` module imports under ``python -I -S`` (isolated: no
  ``site-packages``, no user site, no ``PYTHON*`` env), which is what proves the
  plugin is standard-library only,
* the ZIP is not corrupt, and its SHA-256 is printed for the checksum file.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dylib",
    ".dll",
    ".whl",
    ".egg",
)
FORBIDDEN_NAMES = (
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "environment.yml",
)
REQUIRED_FILES = ("plugin.json", "README.md", "LICENSE")


def fail(message):
    return SystemExit("verify-offline: FAIL " + message)


def dotted_name(relative):
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def check_members(names):
    forbidden = [
        name
        for name in names
        if name.endswith(FORBIDDEN_SUFFIXES)
        or name.rsplit("/", 1)[-1] in FORBIDDEN_NAMES
    ]
    if forbidden:
        raise fail("members that break offline install: " + ", ".join(sorted(forbidden)))

    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) != 1:
        raise fail("expected a single top-level directory, got %r" % (sorted(tops),))
    return tops.pop()


def check_manifest(top, manifest, archive):
    for key in ("id", "version", "entry"):
        if not manifest.get(key):
            raise fail("plugin.json is missing %r" % (key,))
    if manifest["id"] != top:
        raise fail("plugin.json id %r != archive dir %r" % (manifest["id"], top))

    entry = manifest["entry"]
    backend = entry.get("backend") if isinstance(entry, dict) else None
    if not backend:
        raise fail("plugin.json entry.backend is missing")

    expected = "%s-%s" % (manifest["id"], manifest["version"])
    if archive.stem != expected:
        raise fail("archive name %r != %r" % (archive.name, expected + ".zip"))
    return str(backend)


def check_imports(top_dir):
    modules = sorted(path for path in top_dir.rglob("*.py"))
    if not modules:
        raise fail("archive contains no Python modules")
    for path in modules:
        name = dotted_name(path.relative_to(top_dir))
        code = (
            "import importlib, sys\n"
            "sys.path.insert(0, %r)\n"
            "importlib.import_module(%r)\n" % (str(top_dir), name)
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            capture_output=True,
            text=True,
            cwd=str(top_dir),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise fail("%s needs site-packages: %s" % (name, " | ".join(detail)))


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: verify_offline.py <plugin.zip>")
    archive = Path(argv[1]).resolve()
    if not archive.is_file():
        raise fail("missing archive %s" % (archive,))

    with zipfile.ZipFile(archive) as zf:
        broken = zf.testzip()
        if broken:
            raise fail("corrupt member %s" % (broken,))
        names = [name for name in zf.namelist() if not name.endswith("/")]
        top = check_members(names)
        for required in REQUIRED_FILES:
            if "%s/%s" % (top, required) not in names:
                raise fail("missing %s/%s" % (top, required))
        manifest = json.loads(zf.read("%s/plugin.json" % top).decode("utf-8"))

    backend = check_manifest(top, manifest, archive)

    with tempfile.TemporaryDirectory() as workdir:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(workdir)
        top_dir = Path(workdir) / top
        if not (top_dir / backend).is_file():
            raise fail("entry backend %r is missing from the archive" % (backend,))
        check_imports(top_dir)

    print("verify-offline: OK %s (%d bytes)" % (archive.name, archive.stat().st_size))
    print("verify-offline: sha256 %s" % hashlib.sha256(archive.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
