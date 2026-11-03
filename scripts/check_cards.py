#!/usr/bin/env python3
"""Compare dim_cards with the official card list and report what the seed needs.

    make check-cards

Exit codes: 0 nothing to do, 2 something needs attention, 1 check failed.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decksupreme.config import MissingSettingError  # noqa: E402
from decksupreme.extract.card_check import run_card_check  # noqa: E402
from decksupreme.extract.client import ClashApiError  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        report = run_card_check()
    except (ClashApiError, MissingSettingError) as exc:
        logging.getLogger("check_cards").error("%s", exc)
        return 1
    print(json.dumps(report.to_dict(), indent=2))
    return 2 if report.needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
