"""Minimal .env loader — no python-dotenv dependency.

Reads `KEY=VALUE` lines from a `.env` file in the repo root and populates
os.environ for any name not already set (a real environment variable wins). This
keeps secrets — notably the LLM API key — out of source and out of git: the
key lives in the gitignored `.env`, with `.env.example` as the tracked template.

Loaded once on `import midden` so every entry point (CLI, server, tools) sees the
values without an explicit call.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo root = the directory that holds the `midden/` package (and `.env`,
# `README.md`). `parents[1]` from midden/env.py.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Parse a `.env` file and load it into os.environ; return the parsed dict.

    A missing file is fine (returns {}). Handles `export KEY=val`, single/double
    quoted values, inline `#` comments on unquoted values, and blank lines.
    Existing os.environ entries are preserved unless `override=True`.
    """
    p = path or DEFAULT_ENV_PATH
    parsed: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return parsed
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]  # quoted: keep inner content verbatim
        else:
            cut = val.find(" #")  # unquoted: drop an inline comment
            if cut != -1:
                val = val[:cut].rstrip()
        parsed[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return parsed
