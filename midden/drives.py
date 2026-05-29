"""Stable drive IDs.

A drive's identity is a UUID written to `.midden_drive.json` at its root.
This survives the drive being remounted at a different letter/path, and lets
us cross-reference the same physical drive across ingests.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

MARKER = ".midden_drive.json"


@dataclass
class DriveInfo:
    id: str
    label: str
    root_path: str


def get_or_create(root: Path, label: str | None = None) -> DriveInfo:
    """Return drive info, writing a marker if none exists."""
    root = Path(root).resolve()
    marker = root / MARKER
    if marker.exists():
        data = json.loads(marker.read_text())
        return DriveInfo(
            id=data["id"],
            label=data.get("label") or label or root.name,
            root_path=str(root),
        )
    drive_id = str(uuid.uuid4())
    info = DriveInfo(id=drive_id, label=label or root.name, root_path=str(root))
    marker.write_text(json.dumps({"id": info.id, "label": info.label}, indent=2))
    return info
