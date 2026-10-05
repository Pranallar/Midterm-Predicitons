"""Tests for supermarket_bot/store.py (TrackerStore: SQLite persistence for the tracker).

Offline and fast: in-memory databases except where file behaviour (WAL, reopening,
migrations) is the point.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Dict, List, Optional

import pytest

from conftest import market

import supermarket_bot.analytics as analytics_mod
import supermarket_bot.store as store_mod
from supermarket_bot.models import (
    SURGE_HELD,
    SURGE_OPEN,
    SURGE_REVERTED,
    Article,
    Attribution,
    ExchangeInfo,
    PricePoint,
    Surge,
    TradeFlow,
    iso_ts,
)
from supermarket_bot.store import SCHEMA_VERSION, TrackerStore, fallback_mark_price

T0 = 1_790_000_000.0 - (1_790_000_000.0 % 3600)  # an hour boundary in Sept 2026
H = 3600.0
DAY = 86400.0


@pytest.fixture
def store() -> TrackerStore:
    s = TrackerStore()
    yield s
    s.close()


def tick(eid: str, last: Optional[float], bid: Optional[float] = None, ask: Optional[float] = None) -> Dict[str, Any]:
    return {"exchange_id": eid, "latest_price": last, "best_bid": bid, "best_ask": ask}


def candle(start: float, close: Optional[float], **extra: Any) -> Dict[str, Any]:
    out = {"time": iso_ts(start), "open": close, "high": close, "low": close, "close": close, "vwap": close, "volume": 10, "tradeCount": 2}
    out.update(extra)
    return out


def make_surge(
    eid: str = "e1",
    direction: str = "up",
    start_ts: float = T0,
    end_ts: float = T0 + 300,
    start: float = 0.40,
    end: float = 0.55,
    detected_at: Optional[float] = None,
    window: str = "5m",
    window_s: float = 300.0,
    peak: Optional[float] = None,
    zscore: Optional[float] = None,
    status: str = SURGE_OPEN,
    current: Optional[float] = None,
) -> Surge:
    return Surge(
        exchange_id=eid,
        market_id="m1",
        window=window,
        window_s=window_s,
        start_ts=start_ts,
        end_ts=end_ts,
        start_price=start,
        end_price=end,
        change=round(end - start, 6),
        direction=direction,
        peak_price=peak if peak is not None else end,
        detected_at=end_ts if detected_at is None else detected_at,
        zscore=zscore,
        status=status,
        current_price=current,
    )


def sample_attribution() -> Attribution:
    return Attribution(
        verdict="participants",
        confidence=0.64,
        reversion_odds=0.66,
        summary="Two large trades moved a thin book",
        reasons=["2 trades only", "top trade 80% of size"],
        articles=[
            Article(title="Poll shows tight race", url="https://example.test/a", source="Example", published_at=T0 - 600,
                    summary="A new poll", provider="gdelt", relevance=0.5),
        ],
        flow=TradeFlow(n_trades=2, total_size=900.0, max_trade_size=720.0, top_trade_share=0.8, hhi=0.68,
                       yes_share=1.0, vwap=0.52, first_ts=T0, last_ts=T0 + 120, price_impact=0.05),
        book_depth=320.0,
        method="heuristic+llm",
        llm={"verdict": "participants", "key_article_indexes": [0]},
        analyzed_at=T0 + 400,
    )


# --------------------------------------------------------------------------- schema / lifecycle


def test_schema_created_and_versioned(store: TrackerStore) -> None:
    assert store.schema_version == SCHEMA_VERSION == 2  # schema v2: docs/PAPER_TRADING.md §7.1
    tables = {r[0] for r in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"markets", "exchanges", "ticks", "candles", "trades", "surges", "news_cache", "state"} <= tables
    indexes = {r[0] for r in store._query("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"candles_exchange_ts", "trades_exchange_ts", "surges_exchange"} <= indexes


def test_file_database_uses_wal_and_persists(tmp_path) -> None:
    path = tmp_path / "nested" / "dir" / "tracker.db"
    s = TrackerStore(path)
    assert path.exists()
    assert s._query("PRAGMA journal_mode")[0][0] == "wal"
    assert s._query("PRAGMA synchronous")[0][0] == 1  # NORMAL
    s.add_ticks(T0, [tick("e1", 0.4)])
    s.set_state("k", {"a": 1})
    s.close()
    s.close()  # idempotent

    again = TrackerStore(str(path))
    try:
        assert again.schema_version == 2
        assert again.tick_count() == 1
        assert again.get_state("k") == {"a": 1}
    finally:
        again.close()


def test_newer_schema_version_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        TrackerStore(path)


def test_context_manager_closes(tmp_path) -> None:
    with TrackerStore(tmp_path / "x.db") as s:
        s.set_state("a", 1)
    with pytest.raises(sqlite3.ProgrammingError):
        s.get_state("a")


# --------------------------------------------------------------------------- metadata


def test_upsert_markets_and_exchanges(store: TrackerStore) -> None:
    markets = [
        market("m1", "Will Republicans win the Pennsylvania Senate race?", [("e1", None, 0.4)]),
        market("m3", "Who will win the Arizona Governor race?", [("e3a", "Alice", 0.5), ("e3b", "Bob", 0.3)]),
        {"title": "no id: skipped"},
    ]
    store.upsert_markets(markets)
    infos = store.markets()
    assert [m.market_id for m in infos] == ["m1", "m3"]
    m3 = infos[1]
    assert m3.title == "Who will win the Arizona Governor race?"
    assert m3.is_multi is True and m3.exchange_ids == ["e3a", "e3b"]
    assert m3.status == "open" and m3.settlement_date == "2026-11-04T04:59:00.000Z"
    assert m3.categories == ["Election Outcome"]
    assert infos[0].is_multi is False

    assert [e.exchange_id for e in store.exchanges()] == ["e1", "e3a", "e3b"]
    assert store.exchange("e3b") == ExchangeInfo(
        exchange_id="e3b", market_id="m3", option="Bob", market_title="Who will win the Arizona Governor race?",
        settlement_date="2026-11-04T04:59:00.000Z", initial_price=0.5,
    )
    assert store.exchange("nope") is None

    # upsert again with a changed title: updated in place, no duplicates
    renamed = market("m1", "Will the GOP win the Pennsylvania Senate race?", [("e1", None, 0.42)])
    store.upsert_markets([renamed])
    assert len(store.markets()) == 2
    assert store.exchange("e1").market_title == "Will the GOP win the Pennsylvania Senate race?"


def test_is_multi_falls_back_to_exchange_count(store: TrackerStore) -> None:
    raw = market("m9", "Who wins?", [("a", "A", 0.5), ("b", "B", 0.5)])
    del raw["isMultiOutcome"]
    store.upsert_markets([raw])
    assert store.markets()[0].is_multi is True


# --------------------------------------------------------------------------- ticks


def test_add_ticks_skips_rows_without_prices_and_replaces_same_ts(store: TrackerStore) -> None:
    rows = [tick("e1", 0.40, 0.39, 0.41), tick("e2", None), {"latest_price": 0.5}, tick("e3", None, 0.2, None)]
    assert store.add_ticks(T0, rows) == 2
    assert store.tick_count() == 2
    assert store.add_ticks(T0, [tick("e1", 0.45, 0.44, 0.46)]) == 1
    assert store.tick_count() == 2
    assert store.latest("e1").last == 0.45
    assert store.add_ticks(T0, []) == 0
    # bool / NaN are not prices
    assert store.add_ticks(T0 + 1, [tick("e4", True), tick("e5", float("nan"))]) == 0


def test_old_ticks_are_pruned(monkeypatch) -> None:
    monkeypatch.setattr(store_mod, "PRUNE_EVERY", 2)
    s = TrackerStore()
    try:
        s.upsert_markets([market("m1", "M", [("e1", None, 0.4)])])
        s.add_ticks(0.0, [tick("e1", 0.40)])  # first insert prunes (nothing old yet)
        s.add_ticks(2 * DAY, [tick("e1", 0.41)])
        s.add_ticks(11 * DAY, [tick("e1", 0.42), tick("other", 0.1)])  # cutoff = day 1
        assert [p.ts for p in s.series("e1", 0.0)] == [2 * DAY, 11 * DAY]
        assert s.tick_count() == 3
    finally:
        s.close()


def test_prune_runs_every_500_inserts_by_default() -> None:
    s = TrackerStore()
    try:
        s.add_ticks(0.0, [tick("e1", 0.40)])
        s.add_ticks(20 * DAY, [tick(f"x{i}", 0.5) for i in range(10)])
        assert s.tick_count() == 11  # not yet: only 11 inserts since the first prune
        s.add_ticks(20 * DAY + 1, [tick(f"y{i}", 0.5) for i in range(500)])
        assert s.series("e1", -1.0) == []
    finally:
        s.close()


# --------------------------------------------------------------------------- candles / series


def test_add_candles_idempotent_and_has_candles(store: TrackerStore) -> None:
    candles = [candle(T0, 0.40), candle(T0 + H, 0.41), {"time": "garbage", "close": 0.5}, "not a dict"]
    assert store.add_candles("e1", "1h", candles) == 2
    assert store.add_candles("e1", "1h", [candle(T0 + H, 0.43)]) == 1  # forming candle re-read
    assert store.has_candles("e1", "1h") and not store.has_candles("e1", "5m") and not store.has_candles("e2", "1h")
    rows = store._query("SELECT ts, close, trade_count FROM candles ORDER BY ts")
    assert [(r["ts"], r["close"], r["trade_count"]) for r in rows] == [(T0, 0.40, 2), (T0 + H, 0.43, 2)]
    assert store.add_candles("e1", "1h", []) == 0


def test_series_uses_candle_close_times_before_the_first_tick(store: TrackerStore) -> None:
    store.add_candles("e1", "1h", [candle(T0 + i * H, 0.40 + i / 100) for i in range(6)])
    store.add_ticks(T0 + 3.5 * H, [tick("e1", 0.50, 0.49, 0.51)])
    store.add_ticks(T0 + 3.6 * H, [tick("e1", 0.52, 0.51, 0.53)])
    pts = store.series("e1", T0)
    assert [(p.ts, p.source) for p in pts] == [
        (T0 + 1 * H, "candle"),
        (T0 + 2 * H, "candle"),
        (T0 + 3 * H, "candle"),  # closes before the first tick; T0+4h would close after it
        (T0 + 3.5 * H, "tick"),
        (T0 + 3.6 * H, "tick"),
    ]
    c = pts[0]
    assert c.price == c.last == 0.40 and c.bid is None and c.ask is None
    assert pts[3].price == 0.50 and pts[3].bid == 0.49 and pts[3].ask == 0.51


def test_series_candles_fill_a_window_that_starts_before_tracking(store: TrackerStore) -> None:
    store.add_candles("e1", "1h", [candle(T0 + i * H, 0.40) for i in range(10)])
    store.add_ticks(T0, [tick("e1", 0.30)])  # an old tick outside the window
    store.add_ticks(T0 + 8.5 * H, [tick("e1", 0.45)])
    pts = store.series("e1", T0 + 5 * H)
    assert [p.ts for p in pts] == [T0 + 5 * H, T0 + 6 * H, T0 + 7 * H, T0 + 8 * H, T0 + 8.5 * H]


def test_series_prefers_5m_over_1h_where_both_exist(store: TrackerStore) -> None:
    store.add_candles("e1", "1h", [candle(T0 + i * H, 0.30) for i in range(6)])  # closes T0+1h..T0+6h
    store.add_candles("e1", "5m", [candle(T0 + 4 * H + i * 300, 0.35) for i in range(12)])  # T0+4h..T0+5h
    pts = store.series("e1", T0)
    hourly = [p.ts for p in pts if p.price == 0.30]
    fine = [p.ts for p in pts if p.price == 0.35]
    assert hourly == [T0 + 1 * H, T0 + 2 * H, T0 + 3 * H, T0 + 4 * H, T0 + 6 * H]  # T0+5h is inside the 5m span
    assert fine == [T0 + 4 * H + (i + 1) * 300 for i in range(12)]
    assert [p.ts for p in pts] == sorted(p.ts for p in pts)
    assert all(p.source == "candle" for p in pts)


def test_series_since_until_and_null_closes(store: TrackerStore) -> None:
    store.add_candles("e1", "5m", [candle(T0 + i * 300, None if i == 2 else 0.4) for i in range(6)])
    store.add_candles("e1", "weird", [candle(T0, 0.9)])  # unknown resolution: ignored
    store.add_ticks(T0 + 3000, [tick("e1", 0.5)])
    store.add_ticks(T0 + 3300, [tick("e1", 0.6)])
    pts = store.series("e1", T0 + 600, T0 + 3000)
    assert [p.ts for p in pts] == [T0 + 600, T0 + 1200, T0 + 1500, T0 + 1800, T0 + 3000]  # 900 is a null close
    assert [p.ts for p in store.series("e1", T0 + 3100)] == [T0 + 3300]
    assert store.series("e1", T0 + 4000) == []
    assert [p.source for p in store.series("e1", T0, include_candles=False)] == ["tick", "tick"]
    # no ticks at all in [since, until]: every candle close in range is used
    assert [p.ts for p in store.series("e1", T0, T0 + 1500)] == [T0 + 300, T0 + 600, T0 + 1200, T0 + 1500]


def test_series_marks_follow_the_design_rule(store: TrackerStore) -> None:
    store.add_ticks(T0, [tick("e1", 0.40, 0.39, 0.41)])  # tight: mid
    store.add_ticks(T0 + 1, [tick("e1", 0.40, 0.20, 0.70)])  # wide: last
    store.add_ticks(T0 + 2, [tick("e1", None, 0.20, 0.70)])  # wide, no last: mid
    store.add_ticks(T0 + 3, [tick("e1", None, 0.20, None)])  # one-sided, no last: None
    store.add_ticks(T0 + 4, [tick("e1", 0.33, None, None)])  # last only
    assert [p.price for p in store.series("e1", T0)] == [0.40, 0.40, 0.45, None, 0.33]


def test_series_marks_use_analytics_mark_price(store: TrackerStore, monkeypatch) -> None:
    store.add_ticks(T0, [tick("e1", 0.40, 0.39, 0.41)])
    monkeypatch.setattr(analytics_mod, "mark_price", lambda last, bid, ask, max_spread=0.10: 0.123)
    assert store.series("e1", T0)[0].price == 0.123

    def broken(*args: Any, **kwargs: Any) -> Optional[float]:
        raise NotImplementedError

    monkeypatch.setattr(analytics_mod, "mark_price", broken)
    assert store.series("e1", T0)[0].price == 0.40  # standalone fallback
    assert store.latest("e1").price == 0.40


def test_fallback_mark_price() -> None:
    assert fallback_mark_price(0.4, 0.39, 0.41) == 0.40
    assert fallback_mark_price(0.4, 0.45, 0.55) == 0.50  # a 0.10 spread is still tight (float slack)
    assert fallback_mark_price(0.4, 0.2, 0.7) == 0.4
    assert fallback_mark_price(None, 0.2, 0.7) == 0.45
    assert fallback_mark_price(None, 0.2, None) is None
    assert fallback_mark_price(0.3, None, 0.7) == 0.3
    assert fallback_mark_price(None, None, None) is None


def test_latest(store: TrackerStore) -> None:
    assert store.latest("e1") is None
    store.add_candles("e1", "1h", [candle(T0, 0.40), candle(T0 + H, 0.41)])
    store.add_candles("e1", "5m", [candle(T0 + H + 1800, 0.42)])  # closes T0+1h35m, before the 1h close
    latest = store.latest("e1")
    assert latest == PricePoint(ts=T0 + 2 * H, price=0.41, last=0.41, source="candle")
    store.add_ticks(T0 + 5, [tick("e1", 0.39, 0.385, 0.395)])
    assert store.latest("e1") == PricePoint(ts=T0 + 5, price=0.39, last=0.39, bid=0.385, ask=0.395, source="tick")


# --------------------------------------------------------------------------- trades


def test_trades_idempotent_and_ordered(store: TrackerStore) -> None:
    tape = [
        {"id": "102", "createdAt": iso_ts(T0 + 20), "price": 0.55, "size": 500, "side": "YES", "volume": 275},
        {"id": "101", "createdAt": iso_ts(T0 + 10), "price": 0.50, "size": 100, "side": "no", "volume": 50},
        {"id": None, "createdAt": iso_ts(T0), "price": 0.5, "size": 1},
        {"id": "bad", "createdAt": "not a time", "price": 0.5, "size": 1},
    ]
    assert store.add_trades("e1", tape) == 2
    assert store.add_trades("e1", tape) == 0
    assert store.add_trades("e2", tape[:1]) == 1  # same id on another exchange is another trade
    trades = store.trades("e1", T0)
    assert [(t.trade_id, t.ts, t.price, t.size, t.side) for t in trades] == [
        ("101", T0 + 10, 0.50, 100.0, "NO"),
        ("102", T0 + 20, 0.55, 500.0, "YES"),
    ]
    assert [t.trade_id for t in store.trades("e1", T0, T0 + 15)] == ["101"]
    assert store.trades("e1", T0 + 30) == []
    assert store.add_trades("e1", []) == 0


# --------------------------------------------------------------------------- surges


def test_record_surge_inserts_then_merges(store: TrackerStore) -> None:
    first = make_surge(start_ts=T0, end_ts=T0 + 300, start=0.40, end=0.55, window="5m", zscore=4.0)
    stored = store.record_surge(first)
    assert stored.id == 1 and first.id is None  # the caller's object is untouched
    assert store.get_surge(1) == stored

    later = make_surge(start_ts=T0 - 3000, end_ts=T0 + 600, start=0.38, end=0.60, window="1h", window_s=3600.0,
                       detected_at=T0 + 600, zscore=5.5, peak=0.61)
    merged = store.record_surge(later)
    assert merged.id == 1
    assert (merged.start_ts, merged.start_price) == (T0 - 3000, 0.38)  # earliest start
    assert (merged.end_ts, merged.end_price) == (T0 + 600, 0.60)  # latest end
    assert merged.change == 0.22 and merged.window == "1h" and merged.window_s == 3600.0 and merged.zscore == 5.5
    assert merged.peak_price == 0.61 and merged.detected_at == T0 + 300  # first detection kept
    assert store.get_surge(1) == merged
    assert len(store.surges()) == 1

    smaller = make_surge(start_ts=T0 + 400, end_ts=T0 + 900, start=0.50, end=0.57, detected_at=T0 + 900, current=0.57)
    merged = store.record_surge(smaller)
    assert merged.window == "1h"  # the 1h frame still describes the move (0.07 is less than half of it)
    # the price fell back to 0.57: the frame stays at the spike (r2-fixcheck-3), only the live price moves
    assert (merged.start_ts, merged.end_ts, merged.end_price) == (T0 - 3000, T0 + 600, 0.60)
    assert merged.change == 0.22 and merged.zscore == 5.5
    assert merged.peak_price == 0.61 and merged.current_price == 0.57
    assert merged.reverted_fraction == pytest.approx((0.61 - 0.57) / (0.61 - 0.38), abs=1e-6)

    higher = make_surge(start_ts=T0 - 2400, end_ts=T0 + 1200, start=0.39, end=0.63, window="1h", window_s=3600.0,
                        detected_at=T0 + 1200, peak=0.63)
    merged = store.record_surge(higher)
    # a new high inside the frame's window: the end follows it, z scales with the change
    assert (merged.start_ts, merged.end_ts, merged.end_price, merged.window) == (T0 - 3000, T0 + 1200, 0.63, "1h")
    assert merged.change == 0.25 and merged.zscore == pytest.approx(5.5 * 0.25 / 0.22, abs=1e-4)
    assert merged.peak_price == 0.63


def test_record_surge_down_moves_keep_the_lowest_peak(store: TrackerStore) -> None:
    store.record_surge(make_surge(direction="down", start=0.60, end=0.45, peak=0.44))
    merged = store.record_surge(make_surge(direction="down", start=0.60, end=0.47, peak=0.46, end_ts=T0 + 600))
    # the bounce to 0.47 does not move the frame (end - start of the drop), the peak is kept apart
    assert merged.peak_price == 0.44 and merged.change == -0.15 and merged.end_price == 0.45
    lower = store.record_surge(make_surge(direction="down", start=0.60, end=0.43, peak=0.43, end_ts=T0 + 330))
    assert (lower.end_price, lower.change, lower.peak_price) == (0.43, -0.17, 0.43)  # a new low extends it


def test_functional_1_longer_window_redetection_keeps_the_start(store: TrackerStore) -> None:
    """A 1h spike seen again 12 min later through the 24h window keeps its 1h start (not a day earlier)."""
    now = T0 + 10 * DAY
    first = store.record_surge(make_surge(start_ts=now - H, end_ts=now, start=0.38, end=0.575, peak=0.59,
                                          window="1h", window_s=H, detected_at=now, zscore=7.7))
    later = now + 720
    merged = store.record_surge(make_surge(start_ts=later - 28.4 * H, end_ts=later, start=0.39, end=0.545, peak=0.59,
                                           window="24h", window_s=DAY, detected_at=later, zscore=3.4, current=0.545))
    assert merged.id == first.id
    assert (merged.window, merged.window_s, merged.start_ts, merged.start_price) == ("1h", H, now - H, 0.38)
    # the price is lower now (0.545): the frame stays at the spike's end (r2-fixcheck-3)
    assert (merged.end_ts, merged.end_price) == (now, 0.575)
    assert merged.change == 0.195 and merged.zscore == 7.7
    assert merged.current_price == 0.545
    assert store.get_surge(first.id) == merged


def test_functional_1_shorter_window_reframes_a_partial_history_detection(store: TrackerStore) -> None:
    """A first sighting over 24h (partial history) is re-framed by a later 1h detection of the same move."""
    now = T0 + 10 * DAY
    store.record_surge(make_surge(start_ts=now - 20 * H, end_ts=now, start=0.40, end=0.55, window="24h",
                                  window_s=DAY, zscore=3.1))
    merged = store.record_surge(make_surge(start_ts=now - H + 300, end_ts=now + 300, start=0.42, end=0.56,
                                           window="1h", window_s=H, detected_at=now + 300, zscore=6.0))
    assert (merged.window, merged.start_ts, merged.start_price, merged.zscore) == ("1h", now - H + 300, 0.42, 6.0)
    assert merged.change == 0.14


def test_functional_2_change_always_equals_end_minus_start(store: TrackerStore) -> None:
    """Whatever order the windows re-detect a move in, the card's start → end adds up to its change."""
    now = T0 + 10 * DAY
    detections = [
        dict(start_ts=now - 300, end_ts=now, start=0.54, end=0.675, window="5m", window_s=300.0, zscore=5.0),
        dict(start_ts=now - H, end_ts=now + 60, start=0.535, end=0.66, peak=0.675, window="1h", window_s=H, zscore=4.0),
        dict(start_ts=now - 6 * H, end_ts=now + 600, start=0.50, end=0.64, peak=0.675, window="6h", window_s=6 * H),
        dict(start_ts=now + 300, end_ts=now + 900, start=0.60, end=0.655, peak=0.675, window="5m", window_s=300.0),
        dict(start_ts=now - 23 * H, end_ts=now + 1800, start=0.51, end=0.70, window="24h", window_s=DAY, zscore=3.5),
    ]
    for d in detections:
        merged = store.record_surge(make_surge(detected_at=d["end_ts"], **d))
        assert merged.change == pytest.approx(merged.end_price - merged.start_price, abs=1e-9)
        assert merged.end_ts - merged.start_ts <= 1.25 * merged.window_s + (merged.end_ts - now) + 1e-6
    assert len(store.surges()) == 1


def test_record_surge_does_not_merge_across_direction_status_or_time(store: TrackerStore) -> None:
    a = store.record_surge(make_surge())
    b = store.record_surge(make_surge(direction="down", start=0.55, end=0.40))
    c = store.record_surge(make_surge(eid="e2"))
    assert len({a.id, b.id, c.id}) == 3

    a.status = SURGE_REVERTED
    store.update_surge(a)
    d = store.record_surge(make_surge(end_ts=T0 + 900))
    assert d.id not in (a.id, b.id, c.id)

    # open, but last seen more than 6 h before the new detection: a new surge
    e = store.record_surge(make_surge(start_ts=T0 + 7 * H, end_ts=T0 + 7 * H + 300))
    assert e.id != d.id
    # within a custom merge window it would have merged
    f = store.record_surge(make_surge(start_ts=T0 + 20 * H, end_ts=T0 + 20 * H + 300), merge_window_s=24 * H)
    assert f.id == e.id


def test_record_surge_merges_while_the_move_keeps_extending(store: TrackerStore) -> None:
    """A move that keeps going stays one surge: each longer window that takes it further takes
    over the frame, so the frame's end follows the move."""
    sid = store.record_surge(make_surge(end_ts=T0 + 300)).id  # 5m: 0.40 -> 0.55
    # the price keeps climbing for hours: the 6h window (0.40 -> 0.62) and later the 24h window
    # (0.40 -> 0.70) take over the frame
    for t, name, length, end in ((T0 + 300 + 4 * H, "6h", 6 * H, 0.62), (T0 + 300 + 10 * H, "24h", DAY, 0.70)):
        merged = store.record_surge(make_surge(start_ts=t - length, end_ts=t, end=end, window=name, window_s=length,
                                               detected_at=t))
        assert merged.id == sid and (merged.end_ts, merged.window, merged.end_price) == (t, name, end)
        assert merged.change == pytest.approx(end - 0.40) and merged.detected_at == T0 + 300


def test_r2_fixcheck_3_frame_stays_at_the_spike(store: TrackerStore) -> None:
    """A 1h spike followed by re-detections at lower prices (1h, then 24h windows) keeps its frame:
    the card's move, its z-score and the analysis describe the spike; the peak and the live
    price move on their own."""
    now = T0 + 10 * DAY
    first = store.record_surge(make_surge(start_ts=now - H, end_ts=now, start=0.385, end=0.585, peak=0.585,
                                          window="1h", window_s=H, detected_at=now, zscore=7.9))
    peak = store.record_surge(make_surge(start_ts=now - H + 120, end_ts=now + 120, start=0.39, end=0.595, peak=0.595,
                                         window="1h", window_s=H, detected_at=now + 120, zscore=7.6))
    assert (peak.end_price, peak.change, peak.zscore) == (0.595, 0.21, pytest.approx(7.9 * 0.21 / 0.2, abs=1e-4))
    falling = [
        dict(start_ts=now - H + 900, end_ts=now + 900, start=0.40, end=0.52, peak=0.595, window="1h", window_s=H),
        dict(start_ts=now - 23 * H, end_ts=now + 1800, start=0.38, end=0.49, peak=0.595, window="24h", window_s=DAY),
        dict(start_ts=now - 22 * H, end_ts=now + 5400, start=0.37, end=0.50, peak=0.595, window="24h", window_s=DAY),
    ]
    for d in falling:
        merged = store.record_surge(make_surge(detected_at=d["end_ts"], zscore=4.1, current=d["end"], **d))
        assert merged.id == first.id
        assert (merged.start_ts, merged.start_price, merged.window) == (now - H, 0.385, "1h")
        assert (merged.end_ts, merged.end_price, merged.change) == (now + 120, 0.595, 0.21)
        assert merged.zscore == peak.zscore and merged.peak_price == 0.595
        assert merged.current_price == d["end"]
        assert merged.end_ts - merged.start_ts <= 1.25 * merged.window_s
    assert merged.reverted_fraction == pytest.approx((0.595 - 0.50) / 0.21, abs=1e-6)
    assert merged.to_dict()["peak_change"] == 0.21  # sent next to change for the card's "Peak move"


def test_merge_keeps_attribution_and_update_never_erases_it(store: TrackerStore) -> None:
    sid = store.record_surge(make_surge()).id
    snapshot = store.get_surge(sid)  # read before the analysis finished
    store.set_attribution(sid, sample_attribution())
    merged = store.record_surge(make_surge(end_ts=T0 + 600, end=0.60))
    assert merged.attribution is not None  # returned with the stored analysis
    assert store.get_surge(sid).attribution == sample_attribution()

    snapshot.status = SURGE_HELD
    snapshot.current_price = 0.58
    store.update_surge(snapshot)  # attribution None on the stale copy
    again = store.get_surge(sid)
    assert again.status == SURGE_HELD and again.current_price == 0.58
    assert again.attribution == sample_attribution()

    replaced = sample_attribution()
    replaced.verdict = "news"
    again.attribution = replaced
    store.update_surge(again)
    assert store.get_surge(sid).attribution.verdict == "news"


def test_attribution_round_trips_to_dataclasses(store: TrackerStore) -> None:
    sid = store.record_surge(make_surge()).id
    store.set_attribution(sid, sample_attribution())
    got = store.get_surge(sid).attribution
    assert got == sample_attribution()
    assert isinstance(got.articles[0], Article) and isinstance(got.flow, TradeFlow)

    minimal = Attribution(verdict="unclear", confidence=0.35, reversion_odds=0.4, summary="?")
    store.set_attribution(sid, minimal)
    assert store.get_surge(sid).attribution == minimal

    s = make_surge(eid="e9")
    s.attribution = sample_attribution()
    assert store.get_surge(store.record_surge(s).id).attribution == sample_attribution()


def test_attribution_json_tolerates_unknown_and_missing_fields(store: TrackerStore, caplog) -> None:
    sid = store.record_surge(make_surge()).id
    raw = sample_attribution().to_dict()
    raw["future_field"] = 1
    raw["articles"][0]["extra"] = "x"
    raw["flow"]["new_stat"] = 2
    del raw["summary"]
    with store._write() as conn:
        conn.execute("UPDATE surges SET attribution = ? WHERE id = ?", (json.dumps(raw), sid))
    got = store.get_surge(sid).attribution
    assert got.summary == "" and got.articles[0].title == "Poll shows tight race" and got.flow.n_trades == 2

    with store._write() as conn:
        conn.execute("UPDATE surges SET attribution = ? WHERE id = ?", ("{not json", sid))
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        assert store.get_surge(sid).attribution is None
    assert "unreadable" in caplog.text


def test_update_surge_requires_an_id(store: TrackerStore) -> None:
    with pytest.raises(ValueError):
        store.update_surge(make_surge())


def test_surges_filters_and_order(store: TrackerStore) -> None:
    a = store.record_surge(make_surge(eid="e1", end_ts=T0 + 100))
    b = store.record_surge(make_surge(eid="e2", end_ts=T0 + 200))
    c = store.record_surge(make_surge(eid="e1", direction="down", start=0.6, end=0.5, end_ts=T0 + 300))
    b.status = SURGE_REVERTED
    store.update_surge(b)
    assert [s.id for s in store.surges()] == [c.id, b.id, a.id]
    assert [s.id for s in store.surges(status=SURGE_OPEN)] == [c.id, a.id]
    assert [s.id for s in store.surges(exchange_id="e1")] == [c.id, a.id]
    assert [s.id for s in store.surges(since=T0 + 150)] == [c.id, b.id]
    assert [s.id for s in store.surges(limit=1)] == [c.id]
    assert store.get_surge(999) is None

    # a surge first detected long ago but still extending counts as recent
    old = make_surge(eid="e7", start_ts=T0 - 4 * H - 300, end_ts=T0 - 4 * H, detected_at=T0 - 4 * H)
    sid = store.record_surge(old).id
    store.record_surge(make_surge(eid="e7", start_ts=T0 - 4.5 * H, end_ts=T0 + 500, end=0.62, window="6h",
                                  window_s=6 * H, detected_at=T0 + 500))
    assert sid in [s.id for s in store.surges(since=T0 + 400)]


# --------------------------------------------------------------------------- news cache / state


def test_news_cache_ttl(store: TrackerStore) -> None:
    arts = [Article(title="A", url="https://a.test", published_at=T0, relevance=0.7), Article(title="B", url="https://b.test")]
    assert store.cached_news("q", 1200, T0) is None
    store.put_news("q", arts, T0)
    assert store.cached_news("q", 1200, T0 + 1200) == arts
    assert store.cached_news("q", 1200, T0 + 1201) is None
    store.put_news("empty", [], T0)
    assert store.cached_news("empty", 60, T0) == []
    # entries older than a week are dropped on the next put
    store.put_news("new", arts, T0 + 8 * DAY)
    assert store.cached_news("q", 1e9, T0 + 8 * DAY) is None


def test_state_round_trip(store: TrackerStore) -> None:
    assert store.get_state("missing") is None
    assert store.get_state("missing", {"d": 1}) == {"d": 1}
    for value in ({"a": [1, 2.5, None]}, [1, "x"], "text", 3, None, True):
        store.set_state("k", value)
        assert store.get_state("k") == value
    store.set_state("dc", PricePoint(ts=1.0, price=0.5))
    assert store.get_state("dc") == {"ts": 1.0, "price": 0.5, "last": None, "bid": None, "ask": None, "source": "tick"}
    store.set_state("tuple", (1, 2))
    assert store.get_state("tuple") == [1, 2]
    with pytest.raises(TypeError):
        store.set_state("bad", object())


# --------------------------------------------------------------------------- threads


def test_concurrent_writers_and_readers(tmp_path) -> None:
    s = TrackerStore(tmp_path / "threads.db")
    errors: List[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(150):
                s.add_ticks(T0 + n * 1000 + i, [tick(f"e{n}", 0.5)])
                if i % 50 == 0:
                    s.record_surge(make_surge(eid=f"e{n}", end_ts=T0 + i))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(100):
                s.series("e0", T0)
                s.surges(limit=5)
                s.tick_count()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    try:
        assert not errors
        assert s.tick_count() == 6 * 150
        assert len(s.surges(limit=100)) == 6  # one merged open surge per exchange
    finally:
        s.close()


# --------------------------------------------------------------------------- round 2


def test_r2_live_api_4_candles_fill_a_gap_between_ticks(store: TrackerStore) -> None:
    """The dashboard was off for 8 h: once price history is backfilled, its candles fill the gap
    instead of the old tick standing in for the whole of it."""
    store.add_ticks(T0, [tick("e1", 0.975)])
    store.add_ticks(T0 + 60, [tick("e1", 0.975)])
    restart = T0 + 8 * H
    store.add_candles("e1", "1h", [candle(T0 + k * H, 0.90 + k / 100) for k in range(1, 8)])
    store.add_ticks(restart, [tick("e1", 0.97)])
    pts = store.series("e1", T0)
    assert [(p.ts, p.source) for p in pts] == [(T0, "tick"), (T0 + 60, "tick")] + \
        [(T0 + (k + 1) * H, "candle") for k in range(1, 7)] + [(restart, "tick")]
    assert [p.price for p in pts][2:4] == [0.91, 0.92]
    # short pauses (under 15 min) are not gaps: no candles slip in between live ticks
    store.add_ticks(restart + 600, [tick("e1", 0.97)])
    store.add_candles("e1", "5m", [candle(restart + 100, 0.5)])
    assert [p.source for p in store.series("e1", restart)] == ["tick", "tick"]
    assert [p.source for p in store.series("e1", T0, include_candles=False)] == ["tick"] * 4


def test_r2_functional_1_order_books_are_stored_with_the_latest_point(store: TrackerStore) -> None:
    assert store.book("e1") is None
    store.add_ticks(T0, [tick("e1", 0.50, 0.49, 0.51)])
    assert store.latest("e1").book is None
    store.put_book("e1", T0 + 5, [{"price": 0.49, "quantity": 100}, (0.48, 50), (0.47, 0)], [[0.52, 30], [0.51, 70]])
    book = {"at": T0 + 5, "bids": [[0.49, 100.0], [0.48, 50.0]], "asks": [[0.51, 70.0], [0.52, 30.0]]}
    assert store.book("e1") == book
    latest = store.latest("e1")
    assert latest.book == book and latest == PricePoint(ts=T0, price=0.50, last=0.50, bid=0.49, ask=0.51, source="tick")
    assert "book" not in PricePoint(ts=1.0, price=0.5).to_dict()  # the usual point keeps its shape
    store.put_book("e1", T0 + 9, [], [(0.53, 10)])
    assert store.book("e1") == {"at": T0 + 9, "bids": [], "asks": [[0.53, 10.0]]}


def test_r2_fixcheck_3_record_surge_into_a_named_open_surge(store: TrackerStore) -> None:
    first = store.record_surge(make_surge(start_ts=T0, end_ts=T0 + H, end=0.60, window="1h", window_s=H))
    # 10 h later (outside the 6 h merge window) the tracker names the surge the detection re-sees
    later = T0 + 11 * H
    detection = make_surge(start_ts=later - DAY, end_ts=later, end=0.60, window="24h", window_s=DAY, detected_at=later)
    assert store.record_surge(detection).id != first.id  # without a target: a new surge
    merged = store.record_surge(detection, into=first.id)
    assert merged.id == first.id and (merged.window, merged.end_ts) == ("1h", T0 + H)
    closed = make_surge(eid="e9")
    sid = store.record_surge(closed).id
    reverted = store.get_surge(sid)
    reverted.status = SURGE_REVERTED
    store.update_surge(reverted)
    assert store.record_surge(make_surge(eid="e9", end_ts=T0 + 400), into=sid).id != sid  # only open surges absorb
