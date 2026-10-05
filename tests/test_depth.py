"""Tests for the shared depth helpers (supermarket_bot/depth.py) and their agreement with the legacy
strategy walk (docs/PAPER_TRADING.md, Design decision D28)."""

from __future__ import annotations

import random

import pytest

from supermarket_bot import depth as D
from supermarket_bot import strategy as S
from supermarket_bot.models import BookObservation


def test_ticks() -> None:
    assert D.floor_tick(0.5466) == 0.545
    assert D.ceil_tick(0.5466) == 0.55
    assert D.floor_tick(0.545) == 0.545 and D.ceil_tick(0.545) == 0.545
    assert D.floor_tick(0.4125) == 0.41 and D.floor_tick(0.5625) == 0.56
    assert D.clamp_price(0.0) == 0.005 and D.clamp_price(1.2) == 0.995


def test_contract_levels_all_four_directions() -> None:
    bids = [(0.50, 100), (0.52, 50), (0.51, 10)]
    asks = [(0.56, 30), (0.54, 20)]
    assert D.contract_levels(bids, asks, "yes", "buy") == [(0.54, 20), (0.56, 30)]
    assert D.contract_levels(bids, asks, "no", "buy") == [(0.48, 50), (0.49, 10), (0.5, 100)]
    assert D.contract_levels(bids, asks, "yes", "sell") == [(0.52, 50), (0.51, 10), (0.5, 100)]
    assert D.contract_levels(bids, asks, "no", "sell") == [(0.46, 20), (0.44, 30)]


def test_contract_levels_drop_bad_rows_and_merge() -> None:
    bids = [(0.5, 10), (0.5, 5), (1.2, 3), (0.4, 0), ("x", 1), (0.3, None)]
    assert D.contract_levels(bids, [], "yes", "sell") == [(0.5, 15)]
    book = BookObservation("1", 0.0, bids=[(0.5, 10)], asks=[(0.6, 4)])
    assert D.book_sides(book) == ([(0.5, 10.0)], [(0.6, 4.0)])
    assert D.book_sides({"bids": [{"price": 0.5, "quantity": 2}], "asks": [[0.6, 1]]}) == ([(0.5, 2.0)], [(0.6, 1.0)])
    assert D.book_sides(None) == ([], [])


def test_walk_limit_whole_shares_and_average() -> None:
    levels = [(0.31, 300.0), (0.315, 200.0), (0.32, 100.0), (0.33, 1000.0)]
    r = D.walk(levels, 650, 0.32, buy=True)
    assert r.filled == 600 and r.avg_price == pytest.approx((300 * 0.31 + 200 * 0.315 + 100 * 0.32) / 600)
    r = D.walk([(0.5, 10.6)], 20, 0.5, buy=True)
    assert r.filled == 10 and r.levels == [(0.5, pytest.approx(10.0))]
    assert D.walk(levels, 0.4, 0.5, buy=True).filled == 0
    sells = [(0.6, 5.0), (0.55, 5.0)]
    r = D.walk(sells, 10, 0.58, buy=False)
    assert r.filled == 5 and r.avg_price == pytest.approx(0.6)


def test_available_avg_cost_liquidation_shift() -> None:
    levels = [(0.4, 10.0), (0.45, 5.0)]
    assert D.available(levels, 0.42, buy=True) == 10
    assert D.avg_cost(levels, 15) == pytest.approx((4 + 2.25) / 15)
    assert D.avg_cost([], 1) is None and D.avg_cost(levels, 0) is None
    assert D.liquidation_proceeds([(0.6, 5.0), (0.5, 5.0)], 20) == (pytest.approx(5.5), 10.0)
    assert D.shift_levels([(0.6, 5.0), (0.55, 3.0)], 0.5) == [(0.5, 5.0), (0.45, 3.0)]


def test_set_levels_marginal_costs() -> None:
    legs = [[(0.40, 100.0), (0.41, 50.0)], [(0.55, 60.0), (0.56, 200.0)]]
    assert D.set_levels(legs) == [(0.95, 60.0), (0.96, 40.0), (0.97, 50.0)]
    assert D.set_levels(legs, limit=0.96) == [(0.95, 60.0), (0.96, 40.0)]
    assert D.walk_set(legs, 0.96) == 100.0
    assert D.set_levels([[(0.4, 1.0)], []]) == []


def test_walk_set_matches_legacy_strategy_walk() -> None:
    rng = random.Random(42)
    for _ in range(300):
        legs = []
        for _ in range(rng.randint(2, 3)):
            price = rng.uniform(0.05, 0.6)
            lv = []
            for _ in range(rng.randint(1, 5)):
                lv.append((round(price, 3), float(rng.randint(1, 400))))
                price += rng.choice((0.005, 0.01, 0.02))
            legs.append(lv)
        limit = sum(lv[0][0] for lv in legs) + rng.uniform(0.0, 0.08)
        assert D.walk_set(legs, limit) == pytest.approx(S._walk_set(legs, limit))


def test_ticks_survive_float_noise() -> None:
    assert D.floor_tick(0.60 - 0.01 - 0.005) == 0.585  # 0.5849999999999999
    assert D.ceil_tick(0.1 + 0.2) == 0.3  # 0.30000000000000004
    assert D.floor_tick(0.995) == 0.995 and D.ceil_tick(0.0050000001) == 0.01
    assert D.floor_tick(0.5466 - 0.0) == 0.545 and D.ceil_tick(0.5401) == 0.545


def test_sell_walk_and_liquidation_with_fractional_levels() -> None:
    bids = [(0.60, 2.5), (0.59, 2.5), (0.50, 100.0)]
    r = D.walk(bids, 4.9, 0.59, buy=False)
    assert r.filled == 4 and r.levels == [(0.60, 2.5), (0.59, 1.5)]
    assert r.avg_price == pytest.approx((0.60 * 2.5 + 0.59 * 1.5) / 4)
    assert D.walk(bids, 10, 0.70, buy=False).filled == 0  # nothing at or above the limit
    cash, sold = D.liquidation_proceeds(bids, 3.0)
    assert sold == 3.0 and cash == pytest.approx(0.60 * 2.5 + 0.59 * 0.5)
    assert D.liquidation_proceeds([], 10) == (0.0, 0.0)


def test_shift_levels_clips_outside_the_price_range() -> None:
    assert D.shift_levels([(0.02, 5.0), (0.01, 3.0)], 0.005) == [(0.005, 5.0)]
    assert D.shift_levels([(0.97, 5.0), (0.96, 3.0)], 0.995) == [(0.995, 5.0), (0.985, 3.0)]
    assert D.shift_levels([], 0.5) == []


def test_book_sides_accepts_size_and_qty_keys_and_merges_no_levels() -> None:
    book = {"bids": [{"price": 0.4, "size": 3}, {"price": 0.4, "qty": 2}], "asks": [{"price": 0.6, "quantity": 1}]}
    bids, asks = D.book_sides(book)
    assert bids == [(0.4, 3.0), (0.4, 2.0)] and asks == [(0.6, 1.0)]
    assert D.contract_levels(bids, asks, "no", "buy") == [(0.6, 5.0)]
    assert D.contract_levels(bids, asks, "no", "sell") == [(0.4, 1.0)]
