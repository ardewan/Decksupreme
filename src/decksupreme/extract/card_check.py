"""Weekly check of dim_cards against the official card list (GET /v1/cards).

The API publishes ids, names, elixir and rarity but no HP or DPS, so it can't
fill in stats. What it can do is tell you when data/cards_seed.json needs work:

* new_in_api       released cards that dim_cards doesn't know yet
* stats_pending    cards in dim_cards (usually placeholders from the loader)
                   with NULL HP/DPS/speed: these stay unranked until the seed has them
* elixir_changed   a balance change moved a card's cost
* name_changed     the same id under a new name
* missing_from_api dim_cards rows the API no longer lists

Mirror has a variable cost, so it is excluded from stats_pending and elixir checks.
Report only: nothing is written, and the weekly run never fails because of it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from decksupreme.db.models import DimCard

log = logging.getLogger(__name__)

VARIABLE_COST_NAMES = {"mirror"}


@dataclass
class CardCheckReport:
    api_cards: int = 0
    warehouse_cards: int = 0
    new_in_api: list[dict[str, Any]] = field(default_factory=list)
    stats_pending: list[dict[str, Any]] = field(default_factory=list)
    elixir_changed: list[dict[str, Any]] = field(default_factory=list)
    name_changed: list[dict[str, Any]] = field(default_factory=list)
    missing_from_api: list[dict[str, Any]] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.new_in_api or self.stats_pending or self.elixir_changed
            or self.name_changed or self.missing_from_api
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "needs_attention": self.needs_attention}

    def lines(self) -> list[str]:
        out = [
            f"new card (not in dim_cards): {c['name']} ({c['card_id']})" for c in self.new_in_api
        ]
        out += [f"stats pending in seed: {c['name']} ({c['card_id']})" for c in self.stats_pending]
        out += [
            f"elixir changed: {c['name']} warehouse={c['warehouse']} api={c['api']}"
            for c in self.elixir_changed
        ]
        out += [
            f"renamed: {c['card_id']} warehouse={c['warehouse']!r} api={c['api']!r}"
            for c in self.name_changed
        ]
        out += [f"not in API: {c['name']} ({c['card_id']})" for c in self.missing_from_api]
        return out


def compare_cards(
    api_items: Iterable[Mapping[str, Any]], warehouse: Iterable[Mapping[str, Any]]
) -> CardCheckReport:
    """Pure comparison. warehouse rows: card_id, name, elixir, base_hp, base_dps, speed_score."""
    api = {int(i["id"]): i for i in api_items}
    wh = {int(r["card_id"]): r for r in warehouse}
    report = CardCheckReport(api_cards=len(api), warehouse_cards=len(wh))

    for cid, item in sorted(api.items()):
        row = wh.get(cid)
        if row is None:
            report.new_in_api.append({"card_id": cid, "name": item.get("name")})
            continue
        if item.get("name") and item["name"] != row["name"]:
            report.name_changed.append(
                {"card_id": cid, "warehouse": row["name"], "api": item["name"]}
            )
        variable = row["name"].casefold() in VARIABLE_COST_NAMES
        api_elixir = item.get("elixirCost")
        if not variable and api_elixir is not None and row["elixir"] not in (None, api_elixir):
            report.elixir_changed.append(
                {"card_id": cid, "name": row["name"], "warehouse": row["elixir"], "api": api_elixir}
            )

    for cid, row in sorted(wh.items()):
        if cid not in api:
            report.missing_from_api.append({"card_id": cid, "name": row["name"]})
        if row["name"].casefold() in VARIABLE_COST_NAMES:
            continue
        if any(row.get(k) is None for k in ("base_hp", "base_dps", "speed_score")):
            report.stats_pending.append({"card_id": cid, "name": row["name"]})
    return report


def warehouse_cards(engine: Engine) -> list[dict[str, Any]]:
    t = DimCard.__table__
    cols = [t.c.card_id, t.c.name, t.c.elixir, t.c.base_hp, t.c.base_dps, t.c.speed_score]
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(*cols)).mappings()]


def run_card_check(*, client: Any | None = None, engine: Engine | None = None) -> CardCheckReport:
    if client is None:
        from decksupreme.extract.client import ClashRoyaleClient

        client = ClashRoyaleClient()
    if engine is None:
        from decksupreme.db.session import get_engine

        engine = get_engine()
    report = compare_cards(client.get("/cards").get("items", []), warehouse_cards(engine))
    for line in report.lines():
        log.warning("card check: %s", line)
    if not report.needs_attention:
        log.info("card check: dim_cards matches the API (%d cards)", report.api_cards)
    return report
