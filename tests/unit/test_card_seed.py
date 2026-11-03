"""Seed dataset + validator tests. Pure Python: no database or SQLAlchemy needed."""

import copy
import json

import pytest

from decksupreme.seeds.cards import (
    DEFAULT_SEED_PATH,
    CardSeed,
    SeedValidationError,
    load_card_seed,
    reconcile_with_api,
    validate_cards,
)

GOOD = {
    "card_id": 26000000, "name": "Knight", "card_type": "troop", "rarity": "common",
    "elixir": 3, "base_hp": 1766, "base_dps": 168, "unit_count": 1, "speed_score": 2,
}


@pytest.fixture(scope="module")
def seed() -> list[CardSeed]:
    return load_card_seed()


def test_committed_seed_is_valid(seed):
    assert len(seed) >= 100


def test_seed_metadata_declares_level_11():
    meta = json.loads(DEFAULT_SEED_PATH.read_text())["_meta"]
    assert meta["level"] == 11
    assert meta["speed_scale"] == {
        "stationary_or_spell": 0, "slow": 1, "medium": 2, "fast": 3, "very_fast": 4,
    }


def test_seed_covers_every_card_type(seed):
    assert {c.card_type for c in seed} == {"troop", "building", "spell"}


def test_ids_and_names_unique(seed):
    assert len({c.card_id for c in seed}) == len(seed)
    assert len({c.name.casefold() for c in seed}) == len(seed)


def test_speed_mapping_rules(seed):
    for c in seed:
        if c.card_type == "troop":
            assert 1 <= c.speed_score <= 4, c.name
        else:
            assert c.speed_score == 0, c.name


def test_known_anchor_cards(seed):
    by_name = {c.name: c for c in seed}
    assert by_name["Hog Rider"].speed_score == 4          # very fast
    assert by_name["Golem"].speed_score == 1              # slow
    assert by_name["Skeleton Army"].unit_count == 15      # swarm
    assert by_name["Fireball"].base_hp == 0               # spell
    assert by_name["Cannon"].card_type == "building"


def test_db_row_matches_table_columns(seed):
    assert set(seed[0].db_row()) == {
        "card_id", "name", "elixir", "base_hp", "base_dps",
        "speed_score", "card_type", "rarity", "unit_count",
    }


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"elixir": 11}, "elixir 11 outside"),
        ({"speed_score": 5}, "speed_score 5 outside"),
        ({"card_type": "hero"}, "card_type 'hero'"),
        ({"rarity": "mythic"}, "rarity 'mythic'"),
        ({"base_hp": "1766"}, "base_hp must be an integer"),
        ({"speed_score": 0}, "troops need a speed_score"),
        ({"card_type": "spell", "speed_score": 0}, "spells must have base_hp 0"),
        ({"card_type": "building"}, "buildings must have speed_score 0"),
    ],
)
def test_validator_rejects_bad_rows(patch, message):
    bad = {**GOOD, **patch}
    with pytest.raises(SeedValidationError, match=message):
        validate_cards([bad])


def test_validator_rejects_duplicates():
    dup_id = {**GOOD, "name": "Not Knight"}
    dup_name = {**GOOD, "card_id": 1, "name": "KNIGHT"}
    with pytest.raises(SeedValidationError) as exc:
        validate_cards([GOOD, dup_id, dup_name])
    text = str(exc.value)
    assert "duplicate card_id 26000000" in text
    assert "duplicate name 'KNIGHT'" in text


def test_validator_reports_all_problems_at_once():
    with pytest.raises(SeedValidationError) as exc:
        validate_cards([{**GOOD, "elixir": 0}, {**GOOD, "card_id": 2, "name": "X", "rarity": "?"}])
    assert len(exc.value.problems) == 2


def test_note_is_not_a_db_column():
    card = validate_cards([{**GOOD, "note": "hello"}])[0]
    assert card.note == "hello"
    assert "note" not in card.db_row()


def test_reconcile_detects_each_kind_of_drift():
    seed = validate_cards([GOOD, {**GOOD, "card_id": 26000001, "name": "Archers"}])
    api = [
        {"id": 26000000, "name": "Knight", "elixirCost": 3},
        {"id": 26000099, "name": "Archers", "elixirCost": 4},  # wrong id + elixir
        {"id": 26000100, "name": "Brand New Card", "elixirCost": 5},
        {"id": 26000101, "name": "Some Champion"},  # no elixirCost: skipped, not an error
    ]
    report = reconcile_with_api(seed, api)
    assert report.id_mismatch == [("Archers", 26000001, 26000099)]
    assert report.elixir_mismatch == [("Archers", 3, 4)]
    assert report.missing_from_seed == [(26000100, "Brand New Card"), (26000101, "Some Champion")]
    assert not report.ok


def test_reconcile_new_cards_alone_are_ok():
    seed = validate_cards([GOOD])
    api = [{"id": 26000000, "name": "Knight", "elixirCost": 3}, {"id": 9, "name": "New"}]
    report = reconcile_with_api(seed, api)
    assert report.ok  # new cards get placeholder rows from the Phase 3 loader
    assert report.missing_from_seed == [(9, "New")]


def test_reconcile_flags_cards_missing_from_api():
    report = reconcile_with_api(validate_cards([copy.deepcopy(GOOD)]), [])
    assert report.missing_from_api == ["Knight"] and not report.ok
