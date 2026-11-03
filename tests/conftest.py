"""Shared fixtures. Loads .env (without overriding real env vars) so host-side
integration tests hit the stack's published Postgres port on localhost."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(REPO_ROOT / ".env")
os.environ.setdefault("POSTGRES_HOST", "localhost")
