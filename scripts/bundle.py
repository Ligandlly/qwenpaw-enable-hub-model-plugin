#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the uploadable plugin ZIP without third-party tooling."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Only the files a runtime needs; tests and packaging stay out of the archive.
FILES = ("plugin.json", "enable_hub_model.py", "README.md", "LICENSE")


def main() -> int:
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    plugin_id = str(manifest["id"])
    version = str(manifest["version"])
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"missing plugin files: {', '.join(missing)}")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"{plugin_id}-{version}.zip"
    prefix = f"{plugin_id}/"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(ROOT / name, prefix + name)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
