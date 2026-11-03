#!/usr/bin/env python3
"""Run Tasks 2+3: raw battles in MinIO -> filter, dedupe -> Postgres star schema.

Examples (inside the stack):
    make load                          # current ISO week
    make load args="--year 2026 --week 45"

Exit codes: 0 ok, 1 failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decksupreme.load.pipeline import TransformLoadError, run_transform_and_load  # noqa: E402

log = logging.getLogger("run_load")


def main(argv: list[str] | None = None) -> int:
    year, week, _ = datetime.now(UTC).isocalendar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--year", type=int, default=year, help=f"ISO year (default {year})")
    ap.add_argument("--week", type=int, default=week, help=f"ISO week (default {week})")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    from decksupreme.db.session import init_db

    init_db()  # idempotent; makes a fresh stack loadable without `make seed` first
    try:
        stats = run_transform_and_load(args.year, args.week)
    except TransformLoadError as exc:
        log.error("%s", exc)
        return 1
    print(json.dumps(stats, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
