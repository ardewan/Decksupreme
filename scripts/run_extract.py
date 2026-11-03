#!/usr/bin/env python3
"""Run Task 1: fetch the top-N leaderboard and their battle logs, write raw JSON to MinIO.

Examples (inside the stack):
    make extract                                 # top 1,000 -> s3://decksupreme-raw/year=YYYY/week=WW/
    make extract args="--limit 20"               # quick smoke run
    make extract args="--limit 20 --no-upload --out /tmp/raw.json"

Exit codes: 0 ok, 1 extraction failed, 3 missing/invalid API key or IP not whitelisted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decksupreme.config import MissingSettingError  # noqa: E402
from decksupreme.extract.client import ClashApiError, ClashAuthError  # noqa: E402
from decksupreme.extract.pipeline import ExtractionError, run_extraction  # noqa: E402

log = logging.getLogger("run_extract")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--limit", type=int, default=1000, help="leaderboard size (default 1000)")
    ap.add_argument("--workers", type=int, default=8, help="concurrent requests (rate is capped)")
    ap.add_argument("--no-upload", action="store_true", help="skip MinIO; use with --out")
    ap.add_argument("--out", type=Path, help="also write the payload to this local file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    try:
        summary = run_extraction(
            limit=args.limit,
            max_workers=args.workers,
            upload=not args.no_upload,
        )
    except (ClashAuthError, MissingSettingError) as exc:
        log.error("%s", exc)
        return 3
    except (ExtractionError, ClashApiError) as exc:
        log.error("%s", exc)
        return 1

    payload = summary.pop("payload", None)
    if args.out and payload is not None:
        args.out.write_text(json.dumps(payload, indent=2))
        log.info("payload written to %s", args.out)
    elif args.out:
        log.warning("--out only applies with --no-upload; payload is in MinIO")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
