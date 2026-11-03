"""Weekly dim_cards vs GET /v1/cards check (pure comparison + runner with fakes)."""

from __future__ import annotations

from decksupreme.extract.card_check import compare_cards, run_card_check


def wh(cid, name, elixir=3, hp=100, dps=50, ss=2):
    return {"card_id": cid, "name": name, "elixir": elixir, "base_hp": hp, "base_dps": dps,
            "speed_score": ss}


def api(cid, name, elixir=3):
    item = {"id": cid, "name": name}
    if elixir is not None:
        item["elixirCost"] = elixir
    return item


def test_all_in_sync():
    r = compare_cards([api(1, "Knight"), api(2, "Archers")], [wh(1, "Knight"), wh(2, "Archers")])
    assert not r.needs_attention
    assert r.lines() == []
    assert (r.api_cards, r.warehouse_cards) == (2, 2)


def test_flags_every_kind_of_drift():
    r = compare_cards(
        [
            api(1, "Knight"),
            api(2, "Goblin Hut", elixir=4),           # cost changed
            api(3, "Archer Queen"),                   # renamed
            api(4, "Brand New", elixir=5),            # not in warehouse
            api(5, "Vines", elixir=3),                # placeholder, stats pending
            api(6, "Mirror", elixir=None),            # variable cost: never flagged
        ],
        [
            wh(1, "Knight"),
            wh(2, "Goblin Hut", elixir=5),
            wh(3, "Archer Princess"),
            wh(5, "Vines", hp=None, dps=None, ss=None),
            wh(6, "Mirror", elixir=None, hp=None, dps=None, ss=None),
            wh(7, "Retired Card"),                    # no longer in the API
        ],
    )
    assert r.needs_attention
    assert r.new_in_api == [{"card_id": 4, "name": "Brand New"}]
    assert r.stats_pending == [{"card_id": 5, "name": "Vines"}]
    assert r.elixir_changed == [{"card_id": 2, "name": "Goblin Hut", "warehouse": 5, "api": 4}]
    assert r.name_changed == [{"card_id": 3, "warehouse": "Archer Princess", "api": "Archer Queen"}]
    assert r.missing_from_api == [{"card_id": 7, "name": "Retired Card"}]
    assert len(r.lines()) == 5
    assert r.to_dict()["needs_attention"] is True


def test_placeholder_with_null_elixir_is_not_an_elixir_change():
    r = compare_cards([api(5, "Vines", elixir=3)], [wh(5, "Vines", elixir=None, hp=None)])
    assert r.elixir_changed == []
    assert r.stats_pending == [{"card_id": 5, "name": "Vines"}]


class FakeClient:
    def get(self, path):
        assert path == "/cards"
        return {"items": [api(1, "Knight"), api(4, "Brand New")]}


def test_run_card_check_logs_warnings(monkeypatch, caplog):
    import decksupreme.extract.card_check as cc

    monkeypatch.setattr(cc, "warehouse_cards", lambda engine: [wh(1, "Knight")])
    with caplog.at_level("WARNING"):
        report = run_card_check(client=FakeClient(), engine=object())
    assert report.new_in_api == [{"card_id": 4, "name": "Brand New"}]
    assert "new card (not in dim_cards): Brand New (4)" in caplog.text
