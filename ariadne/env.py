"""Minimal .env loading.

Zero-dependency on purpose: one more package in `pyproject.toml` to read five lines of
`KEY=value` is not worth it, and the submission is easier to audit without it.

Existing environment variables always win, so `ANTHROPIC_API_KEY=... python -m ariadne.agent`
overrides the file rather than being silently ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT = Path(".env")


def load(path: Path = DEFAULT) -> list[str]:
    """Load `KEY=value` lines into the environment. Returns the names it set, never the values.

    Returning names only is deliberate: callers log what was loaded, and a secret that is never
    returned cannot be logged by accident.
    """
    if not path.exists():
        return []

    loaded: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not key or key in os.environ:  # the real environment wins
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
