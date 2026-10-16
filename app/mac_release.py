"""The VALR for Mac download: downloads/VALR-<version>.dmg plus downloads/mac.json (version,
file name, size, SHA-256), both written by scripts/release_mac.sh and committed to the repo,
so `git archive` (deploy/deploy-all.sh) and the Dockerfile's `COPY downloads` ship them.

Settings › Phone call recordings shows the version, size and SHA-256; GET /valr/downloads/mac
(owner only, app/phone_web.py) serves the file as an attachment.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DOWNLOADS = Path(__file__).resolve().parent.parent / "downloads"
MANIFEST = DOWNLOADS / "mac.json"
FILE_NAME = re.compile(r"^VALR-\d+\.\d+\.\d+\.dmg$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def size_label(size: int) -> str:
    return f"{size / 1_000_000:.1f} MB" if size >= 1_000_000 else f"{max(1, round(size / 1000))} KB"


def info(downloads: Path | None = None) -> dict[str, Any] | None:
    """The current release, or None when there is none in this build (or the manifest is off)."""
    folder = downloads or DOWNLOADS
    try:
        data = json.loads((folder / "mac.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name, sha, version = data.get("file"), data.get("sha256"), data.get("version")
    if not (isinstance(name, str) and FILE_NAME.match(name) and isinstance(sha, str) and SHA256.match(sha)
            and isinstance(version, str) and name == f"VALR-{version}.dmg"):
        return None
    path = folder / name
    if not path.is_file():
        return None
    size = path.stat().st_size
    return {"version": version, "file": name, "path": path, "size": size, "size_label": size_label(size),
            "sha256": sha}
