#!/usr/bin/env python3
"""Run Tasks 4+5: rank cards for a snapshot week and write rankings/ artifacts.

Examples (inside the stack):
    make rank                              # current ISO week
    make rank args="--year 2026 --week 45"

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

from decksupreme.scoring.pipeline import ScoringError, run_scoring_and_artifacts  # noqa: E402

log = logging.getLogger("run_scoring")


def main(argv: list[str] | None = None) -> int:
    year, week, _ = datetime.now(UTC).isocalendar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--year", type=int, default=year, help=f"ISO year (default {year})")
    ap.add_argument("--week", type=int, default=week, help=f"ISO week (default {week})")
    ap.add_argument("--out", type=Path, help="rankings directory (default: repo rankings/)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        summary = run_scoring_and_artifacts(args.year, args.week, out_dir=args.out)
    except ScoringError as exc:
        log.error("%s", exc)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
