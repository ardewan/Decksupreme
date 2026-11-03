"""Load, validate and reconcile the static card dataset (data/cards_seed.json).

Pure Python on purpose: no SQLAlchemy or network imports, so validation runs
anywhere (CI, pre-commit) and the DB/API layers stay thin.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "cards_seed.json"

CARD_TYPES = {"troop", "building", "spell"}
RARITIES = {"common", "rare", "epic", "legendary", "champion"}
DB_FIELDS = (
    "card_id", "name", "elixir", "base_hp", "base_dps",
    "speed_score", "card_type", "rarity", "unit_count",
)


class SeedValidationError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s) in card seed:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class CardSeed:
    card_id: int
    name: str
    card_type: str
    rarity: str
    elixir: int
    base_hp: int
    base_dps: int
    unit_count: int
    speed_score: int
    note: str | None = field(default=None, compare=False)

    def db_row(self) -> dict:
        data = asdict(self)
        return {k: data[k] for k in DB_FIELDS}


def _check_card(c: dict) -> list[str]:
    label = f"{c.get('name', '?')} ({c.get('card_id', '?')})"
    p: list[str] = []
    ints = ("card_id", "elixir", "base_hp", "base_dps", "unit_count", "speed_score")
    for key in ints:
        if not isinstance(c.get(key), int) or isinstance(c.get(key), bool):
            p.append(f"{label}: {key} must be an integer, got {c.get(key)!r}")
    if not isinstance(c.get("name"), str) or not c.get("name", "").strip():
        p.append(f"{label}: name is required")
    if p:
        return p  # type errors make the range checks below meaningless

    if c["card_type"] not in CARD_TYPES:
        p.append(f"{label}: card_type {c['card_type']!r} not in {sorted(CARD_TYPES)}")
    if c["rarity"] not in RARITIES:
        p.append(f"{label}: rarity {c['rarity']!r} not in {sorted(RARITIES)}")
    if not 1 <= c["elixir"] <= 10:
        p.append(f"{label}: elixir {c['elixir']} outside 1..10")
    if c["base_hp"] < 0 or c["base_dps"] < 0:
        p.append(f"{label}: base_hp/base_dps must be >= 0")
    if c["unit_count"] < 1:
        p.append(f"{label}: unit_count must be >= 1")
    if not 0 <= c["speed_score"] <= 4:
        p.append(f"{label}: speed_score {c['speed_score']} outside 0..4")

    # Spec 3.1 speed mapping: buildings and spells are 0, moving troops 1..4.
    if c["card_type"] in {"building", "spell"} and c["speed_score"] != 0:
        p.append(f"{label}: {c['card_type']}s must have speed_score 0")
    if c["card_type"] == "troop" and c["speed_score"] == 0:
        p.append(f"{label}: troops need a speed_score of 1..4")
    if c["card_type"] == "spell" and c["base_hp"] != 0:
        p.append(f"{label}: spells must have base_hp 0")
    if c["card_type"] in {"troop", "building"} and c["base_hp"] == 0:
        p.append(f"{label}: {c['card_type']}s need base_hp > 0")
    return p


def validate_cards(raw_cards: Iterable[dict]) -> list[CardSeed]:
    """Validate raw dicts and return CardSeed objects, or raise with every problem found."""
    raw_cards = list(raw_cards)
    problems: list[str] = []
    seen_ids: dict[int, str] = {}
    seen_names: dict[str, int] = {}

    for c in raw_cards:
        problems += _check_card(c)
        cid, name = c.get("card_id"), c.get("name")
        if cid in seen_ids:
            problems.append(f"duplicate card_id {cid}: {seen_ids[cid]!r} and {name!r}")
        if isinstance(name, str):
            key = name.casefold()
            if key in seen_names:
                problems.append(f"duplicate name {name!r} (ids {seen_names[key]} and {cid})")
            seen_names[key] = cid
        seen_ids[cid] = name

    if problems:
        raise SeedValidationError(problems)
    allowed = set(CardSeed.__dataclass_fields__)
    return [CardSeed(**{k: v for k, v in c.items() if k in allowed}) for c in raw_cards]


def load_card_seed(path: str | Path = DEFAULT_SEED_PATH) -> list[CardSeed]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "cards" not in doc:
        raise SeedValidationError([f"{path}: missing top-level 'cards' array"])
    return validate_cards(doc["cards"])


@dataclass
class ReconcileReport:
    """Differences between the seed and the official /v1/cards listing."""

    id_mismatch: list[tuple[str, int, int]] = field(default_factory=list)  # name, seed, api
    elixir_mismatch: list[tuple[str, int, int]] = field(default_factory=list)
    missing_from_api: list[str] = field(default_factory=list)  # removed/renamed cards
    missing_from_seed: list[tuple[int, str]] = field(default_factory=list)  # new cards

    @property
    def ok(self) -> bool:
        return not (self.id_mismatch or self.elixir_mismatch or self.missing_from_api)

    def lines(self) -> list[str]:
        out = [f"id mismatch   {n}: seed={s} api={a}" for n, s, a in self.id_mismatch]
        out += [f"elixir diff   {n}: seed={s} api={a}" for n, s, a in self.elixir_mismatch]
        out += [f"not in API    {n}" for n in self.missing_from_api]
        out += [f"not in seed   {n} ({i})" for i, n in self.missing_from_seed]
        return out


def reconcile_with_api(seed: Iterable[CardSeed], api_items: Iterable[dict]) -> ReconcileReport:
    """Compare seed cards with the `items` array of GET /v1/cards, matching by name.

    Champions and evolutions may omit `elixirCost` in the API; those are skipped
    for the elixir comparison only.
    """
    api_by_name = {i["name"].casefold(): i for i in api_items}
    report = ReconcileReport()
    seed = list(seed)
    seed_names = set()
    for card in seed:
        key = card.name.casefold()
        seed_names.add(key)
        api = api_by_name.get(key)
        if api is None:
            report.missing_from_api.append(card.name)
            continue
        if api["id"] != card.card_id:
            report.id_mismatch.append((card.name, card.card_id, api["id"]))
        api_elixir = api.get("elixirCost")
        if api_elixir is not None and api_elixir != card.elixir:
            report.elixir_mismatch.append((card.name, card.elixir, api_elixir))
    report.missing_from_seed = sorted(
        (i["id"], i["name"]) for k, i in api_by_name.items()
        if k not in seed_names and i.get("name", "").casefold() != "mirror"
    )
    return report
