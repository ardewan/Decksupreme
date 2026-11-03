#!/usr/bin/env python3
"""Run Task 6: commit a week's rankings/ artifacts to GitHub.

Examples (inside the stack):
    make publish                           # current ISO week
    make publish args="--year 2026 --week 45"

Without GITHUB_TOKEN/GITHUB_REPO this logs a warning and exits 0 (skipped).
Exit codes: 0 committed/unchanged/skipped, 1 failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decksupreme.publish.github import GitHubPublishError, publish_rankings  # noqa: E402

log = logging.getLogger("run_publish")


def main(argv: list[str] | None = None) -> int:
    year, week, _ = datetime.now(UTC).isocalendar()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--year", type=int, default=year)
    ap.add_argument("--week", type=int, default=week)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        result = publish_rankings(args.year, args.week)
    except GitHubPublishError as exc:
        log.error("%s", exc)
        return 1
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
