#!/usr/bin/env python3
"""Seed dim_cards from data/cards_seed.json.

Examples (inside the stack):
    make seed                                   # validate, create tables if needed, upsert
    docker compose exec airflow-scheduler python /opt/airflow/scripts/seed_dim_cards.py --dry-run
    ... seed_dim_cards.py --reconcile           # also check ids/names/elixir vs the official API

Exit codes: 0 ok, 1 invalid seed or DB error, 2 reconcile found mismatches.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decksupreme.config import get_settings  # noqa: E402
from decksupreme.seeds.cards import (  # noqa: E402
    DEFAULT_SEED_PATH,
    SeedValidationError,
    load_card_seed,
    reconcile_with_api,
)

log = logging.getLogger("seed_dim_cards")


def fetch_api_cards() -> list[dict]:
    import requests

    clash = get_settings().clash
    resp = requests.get(
        f"{clash.base_url}/cards",
        headers={"Authorization": f"Bearer {clash.require_api_key()}"},
        timeout=30,
    )
    if resp.status_code == 403:
        raise SystemExit(
            "Clash Royale API returned 403: the key is missing, invalid, or not "
            "whitelisted for this machine's public IP."
        )
    resp.raise_for_status()
    return resp.json()["items"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--file", type=Path, default=DEFAULT_SEED_PATH, help="seed JSON path")
    ap.add_argument("--dry-run", action="store_true", help="validate only; no DB writes")
    ap.add_argument("--no-init", action="store_true", help="don't create missing tables first")
    ap.add_argument("--reconcile", action="store_true", help="compare with GET /v1/cards first")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        cards = load_card_seed(args.file)
    except SeedValidationError as exc:
        log.error("%s", exc)
        return 1
    log.info("validated %d cards from %s", len(cards), args.file)

    if args.reconcile:
        report = reconcile_with_api(cards, fetch_api_cards())
        for line in report.lines():
            log.warning("reconcile: %s", line)
        if not report.ok:
            log.error("seed disagrees with the official API; fix data/cards_seed.json")
            return 2
        log.info("reconcile: ids, names and elixir match the API")

    if args.dry_run:
        return 0

    from decksupreme.db.session import get_engine, init_db
    from decksupreme.db.upserts import upsert_cards

    engine = get_engine()
    if not args.no_init:
        init_db(engine)
    result = upsert_cards(engine, cards)
    log.info(
        "dim_cards upsert: %d inserted, %d updated, %d unchanged",
        result.inserted, result.updated, result.unchanged,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
