"""Tests for supermarket_bot/tracker.py (Tracker: snapshots → store → surges → attribution).

Everything is offline: REST goes through the FakeAPI MockTransport from conftest, data time
comes from a FakeClock, and threads run with tiny intervals.

Most tests swap ``tracker.analytics`` for a small deterministic fake (see ``FAKE_ANALYTICS``)
so they exercise the tracker's plumbing on their own. ``TestRealAnalytics`` runs the same
pipeline against the real analytics module and the real Attributor.
"""

from __future__ import annotations

import json
import math
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest

from conftest import (
    TOURNAMENT_ID,
    TOURNAMENT_SLUG,
    FakeClock,
    book_exchange,
    error_body,
    market,
    market_orderbook,
    market_page,
    price,
    tournament,
)

import supermarket_bot.analytics as real_analytics
import supermarket_bot.tracker as tracker_mod
from supermarket_bot.bot import Context
from supermarket_bot.errors import ApiError
from supermarket_bot.models import (
    SURGE_HELD,
    SURGE_OPEN,
    SURGE_REVERTED,
    Article,
    Attribution,
    HighBand,
    PricePoint,
    Surge,
    TradeFlow,
    iso_ts,
)
from supermarket_bot.store import TrackerStore, fallback_mark_price
from supermarket_bot.tracker import ANALYZED_STATE_KEY, Tracker, leaderboard_value

T0 = 1_790_000_000.0 - (1_790_000_000.0 % 3600)  # an hour boundary in Sept 2026
SLUG = TOURNAMENT_SLUG
CTX = Context(TOURNAMENT_ID, SLUG, "SIG Predictions Cup", "SIG Coins", "active")
VIEW_KEYS = {"exchanges", "surges", "high_band", "context", "status"}
ROW_KEYS = {
    "exchange_id", "market_id", "title", "option", "settlement_date", "mark", "last", "bid", "ask", "spread",
    "change_5m", "change_1h", "change_24h", "sparkline", "high_band", "surge_id", "updated_at", "stale",
}

TOURNAMENT_INFO = {**tournament(), "initialBalance": 100000, "myBalance": 101250.5, "endDate": "2026-11-04T17:00:00.000Z"}
LEADERBOARD = {
    "leaderboard": [
        {"rank": 1, "profileId": "p1", "username": "alice", "pnl": 5000.0, "tradesCount": 40, "volume": 9000, "winRate": 60.0, "roi": 55.5},
        {"rank": 2, "profileId": "p2", "username": "bob", "pnl": 3000.0, "tradesCount": 12, "volume": 4000, "winRate": None, "roi": 75.0},
        {"rank": 3, "profileId": "p3", "username": None, "pnl": 2500.0, "tradesCount": 3, "volume": 800, "winRate": None, "roi": 312.5},
    ],
    "total": 120, "period": "all", "limit": 3, "offset": 0, "sort": "pnl", "myRank": 17,
    "season": None, "boundGroups": [], "activeGroupId": None,
}
PNL = {"period": "all", "periodStart": None, "periodEnd": "2026-10-03T12:00:00.000Z", "periodPnl": None,
       "unrealizedPnl": 30.0, "totalAccountValue": 103_750.5, "totalHoldingsValue": 2_500.0, "totalCostBasis": 2_470.0,
       "roi": 3.75, "sharpe": None}
CONSTRAINTS = {
    "data": [
        {
            "relationshipId": "r1", "type": "ALL", "violationAmount": 0.06, "reason": "Outcomes sum above 1",
            "suggestedCorrectiveTrades": [{"exchangeId": "e3a", "side": "sell"}], "evaluationStatus": "violated",
        }
    ],
    "violationsCount": 1,
    "computedAt": "2026-10-03T12:00:00.000Z",
}


# --------------------------------------------------------------------------- fake analytics


def _usable(points: List[PricePoint], now: float) -> List[PricePoint]:
    return [p for p in points if p.price is not None and p.ts <= now]


def _fake_detect(points: List[PricePoint], now: float, exchange_id: str, market_id: str, windows: Any = None,
                 z_threshold: float = 3.0, *, partial: bool = False) -> List[Surge]:
    """A 5-minute window only: latest mark vs the latest point at least 300 s older."""
    pts = _usable(points, now)
    if not pts:
        return []
    cur = pts[-1]
    then = None
    for p in pts:
        if p.ts <= now - 300:
            then = p
    if then is None:
        return []
    change = round(cur.price - then.price, 6)
    if abs(change) + 1e-9 < 0.05:
        return []
    span = [p.price for p in pts if then.ts <= p.ts <= cur.ts]
    up = change > 0
    return [Surge(exchange_id, market_id, "5m", 300.0, then.ts, cur.ts, then.price, cur.price, change,
                  "up" if up else "down", max(span) if up else min(span), now)]


def _fake_update(surge: Surge, current: Optional[float], now: float) -> Surge:
    if current is not None:
        surge.current_price = current
    cur = surge.current_price
    up = surge.direction == "up"
    den = (surge.peak_price - surge.start_price) if up else (surge.start_price - surge.peak_price)
    frac = None
    if cur is not None and den > 0:
        frac = round(((surge.peak_price - cur) if up else (cur - surge.peak_price)) / den, 6)
        surge.reverted_fraction = frac
    if frac is not None and frac >= 0.5 - 1e-9:
        surge.status = SURGE_REVERTED
    elif now - surge.end_ts >= 86400:
        surge.status = SURGE_HELD
    else:
        surge.status = SURGE_OPEN
    return surge


def _fake_band(points: List[PricePoint], now: float, exchange_id: str, market_id: str, threshold: float = 0.95,
               lookback_s: float = 21600.0, min_fraction: float = 0.8, settlement_date: Optional[str] = None, **_: Any) -> Optional[HighBand]:
    pts = _usable(points, now)
    if not pts:
        return None
    p = pts[-1].price
    fav, side = (p, "YES") if p >= 0.5 else (round(1 - p, 6), "NO")
    if fav + 1e-9 < threshold:
        return None
    return HighBand(exchange_id, market_id, side, fav, 1.0, fav, fav, fav, lookback_s, True, settlement_date, None)


def _fake_change(points: List[PricePoint], now: float, window_s: float) -> Optional[float]:
    pts = _usable(points, now)
    then = [p for p in pts if p.ts <= now - window_s]
    if not pts or not then:
        return None
    return round(pts[-1].price - then[-1].price, 6)


def _fake_downsample(points: List[PricePoint], max_points: int) -> List[PricePoint]:
    pts = list(points)
    if len(pts) <= max_points:
        return pts
    step = (len(pts) - 1) / (max_points - 1)
    return [pts[int(round(i * step))] for i in range(max_points)]


def _fake_downsample_by_time(points: List[PricePoint], max_points: int, start: Any = None, end: Any = None) -> List[PricePoint]:
    return _fake_downsample(points, max_points)


FAKE_ANALYTICS = SimpleNamespace(
    mark_price=fallback_mark_price,
    detect_surges=_fake_detect,
    update_surge_status=_fake_update,
    high_band=_fake_band,
    change_over=_fake_change,
    downsample=_fake_downsample,
    downsample_by_time=_fake_downsample_by_time,
)


def _real_analytics_available() -> bool:
    try:
        real_analytics.mark_price(0.5, None, None)
        real_analytics.detect_surges([], 0.0, "e", "m")
        real_analytics.high_band([], 0.0, "e", "m")
        real_analytics.downsample([], 2)
    except NotImplementedError:
        return False
    return True


@pytest.fixture(autouse=True)
def _analytics(request: Any, monkeypatch: Any) -> None:
    if request.cls is not None and getattr(request.cls, "real_analytics", False):
        if not _real_analytics_available():
            pytest.skip("supermarket_bot.analytics is not implemented yet")
        return
    monkeypatch.setattr(tracker_mod, "analytics", FAKE_ANALYTICS)


# --------------------------------------------------------------------------- fake world


class World:
    """Mutable market state served through FakeAPI routes."""

    def __init__(self, fake: Any) -> None:
        self.fake = fake
        self.markets: List[Dict[str, Any]] = [
            market("m1", "Will Republicans win the Pennsylvania Senate race?", [("e1", None, 0.40)]),
            market("m2", "Will Democrats win the Ohio Senate race?", [("e2", None, 0.97)]),
            market("m3", "Who will win the Arizona Governor race?", [("e3a", "Alice", 0.5), ("e3b", "Bob", 0.3), ("e3c", "Carol", 0.2)]),
        ]
        self.quotes: Dict[str, List[Optional[float]]] = {}
        for eid, px in (("e1", 0.40), ("e2", 0.97), ("e3a", 0.50), ("e3b", 0.30), ("e3c", 0.20)):
            self.set(eid, px)
        self.history: Dict[str, float] = {"e1": 0.40, "e2": 0.97, "e3a": 0.50, "e3b": 0.30, "e3c": 0.20}
        fake.add("GET", f"/tournaments/{SLUG}/markets", lambda r: httpx.Response(200, json=market_page(self.markets)))
        fake.add("GET", "/exchanges/prices", self._prices)
        fake.add("GET", "/relationships/constraints", CONSTRAINTS)
        fake.add("GET", f"/tournaments/{SLUG}", TOURNAMENT_INFO)
        fake.add("GET", f"/tournaments/{SLUG}/leaderboard", LEADERBOARD)
        fake.add("GET", "/markets/m3/orderbook", market_orderbook(
            [book_exchange("e3a", [(0.49, 100)], [(0.51, 100)], option="Alice"),
             book_exchange("e3b", [(0.29, 100)], [(0.31, 100)], option="Bob"),
             book_exchange("e3c", [(0.24, 100)], [(0.26, 100)], option="Carol")],
            overround=1.04, arb=True,
        ))
        fake.add("GET", f"/tournaments/{SLUG}/portfolio/pnl", PNL)
        for eid in ("e1", "e2", "e3a", "e3b", "e3c"):
            fake.add("GET", f"/exchanges/{eid}/orderbook", self._book)

    def _book(self, request: httpx.Request) -> httpx.Response:
        eid = request.url.path.split("/")[-2]
        last, bid, ask = self.quotes.get(eid) or [None, None, None]
        book = book_exchange(eid, [(bid, 150), (round(bid - 0.01, 6), 300)] if bid is not None else [],
                             [(ask, 120), (round(ask + 0.01, 6), 250)] if ask is not None else [])
        return httpx.Response(200, json={**book, "marketId": self.market_of(eid)})

    def market_of(self, eid: str) -> str:
        for m in self.markets:
            if any(ex["id"] == eid for ex in m["exchanges"]):
                return m["id"]
        raise KeyError(eid)

    def set(self, eid: str, last: float, spread: float = 0.02) -> None:
        self.quotes[eid] = [last, round(last - spread / 2, 6), round(last + spread / 2, 6)]

    def _prices(self, request: httpx.Request) -> httpx.Response:
        ids = [i for i in (request.url.params.get("ids") or "").split(",") if i]
        data = [price(eid, self.market_of(eid), *self.quotes[eid]) for eid in ids if eid in self.quotes]
        return httpx.Response(200, json={"data": data, "missingIds": [eid for eid in ids if eid not in self.quotes]})

    def add_history(self, clock: FakeClock, eids: Optional[List[str]] = None) -> None:
        """Flat candles at each exchange's ``history`` price, ending at the data clock."""
        lengths = {"1h": 3600.0, "5m": 300.0}

        def handler(request: httpx.Request) -> httpx.Response:
            eid = request.url.path.split("/")[-2]
            res = request.url.params.get("resolution", "1h")
            n = int(request.url.params.get("limit", "10"))
            length = lengths[res]
            last_start = math.floor(clock.now / length) * length
            close = self.history[eid]
            candles = [
                {"time": iso_ts(last_start - i * length), "open": close, "high": close, "low": close, "close": close,
                 "vwap": close, "volume": 50, "tradeCount": 3}
                for i in reversed(range(n))
            ]
            return httpx.Response(200, json={
                "exchangeId": eid, "marketId": self.market_of(eid), "resolution": res,
                "from": candles[0]["time"], "to": iso_ts(clock.now), "candles": candles,
                "coverage": {"complete": True, "projectedThroughSequence": 1},
            })

        for eid in eids or list(self.quotes):
            self.fake.add("GET", f"/exchanges/{eid}/price-history", handler)


class FakeAttributor:
    def __init__(self, verdict: str = "participants", fail: Optional[BaseException] = None) -> None:
        self.limiter: Any = None
        self.verdict = verdict
        self.fail = fail
        self.calls: List[Surge] = []

    def analyze(self, surge: Surge) -> Attribution:
        self.calls.append(surge)
        if self.fail is not None:
            raise self.fail
        return Attribution(
            verdict=self.verdict, confidence=0.7, reversion_odds=0.6, summary=f"surge {surge.id} by a few traders",
            reasons=["2 trades only"], articles=[Article(title="Unrelated", url="https://example.test/x")],
            flow=TradeFlow(n_trades=2, total_size=900.0),
        )


@pytest.fixture
def data_clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
def world(fake: Any) -> World:
    return World(fake)


@pytest.fixture
def make_tracker(client: Any, world: World, data_clock: FakeClock) -> Callable[..., Tracker]:
    made: List[Tracker] = []

    def factory(**kwargs: Any) -> Tracker:
        opts: Dict[str, Any] = dict(clock=data_clock, backfill=False)
        opts.update(kwargs)
        store = opts.pop("store", None) or TrackerStore()
        context = opts.pop("context", CTX)
        tracker = Tracker(opts.pop("client", client), context, store, **opts)
        made.append(tracker)
        return tracker

    yield factory
    for tracker in made:
        tracker.stop(timeout=2)


def list_calls(fake: Any) -> List[Any]:
    """Market-list reads (status=open). Since schema v2 the context refresh also reads settled markets
    (status=settled) on the same path (docs/PAPER_TRADING.md §7.2): those are not market-list reads."""
    return [c for c in fake.calls_to(f"/tournaments/{SLUG}/markets") if c.params.get("status") == "open"]


def step(tracker: Tracker, clock: FakeClock, seconds: float = 60.0) -> Dict[str, Any]:
    clock.now += seconds
    return tracker.run_once()


def jump_scenario(tracker: Tracker, world: World, clock: FakeClock, to: float = 0.60) -> Dict[str, Any]:
    """Six flat cycles at 0.40 (T0 .. T0+300), then e1 jumps; returns the jump cycle's summary."""
    tracker.run_once()
    for _ in range(5):
        step(tracker, clock)
    world.set("e1", to)
    return step(tracker, clock)


def wait_for(cond: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def row(view: Dict[str, Any], eid: str) -> Dict[str, Any]:
    return next(r for r in view["exchanges"] if r["exchange_id"] == eid)


# --------------------------------------------------------------------------- cycle basics


def test_run_once_records_markets_ticks_and_builds_view(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker()
    summary = tracker.run_once()
    assert summary["markets"] == 3 and summary["exchanges"] == 5 and summary["ticks"] == 5
    assert summary["markets_refreshed"] and summary["context_refreshed"] and not summary["stopped"]
    assert summary["errors"] == 0 and summary["at"] == T0
    assert [m.market_id for m in tracker.store.markets()] == ["m1", "m2", "m3"]
    assert tracker.store.tick_count() == 5
    assert fake.calls_to(f"/tournaments/{SLUG}/markets")[0].params["status"] == "open"
    assert fake.calls_to("/exchanges/prices")[0].params["tournamentId"] == TOURNAMENT_ID

    view = tracker.view()
    json.dumps(view)
    assert set(view) == VIEW_KEYS
    assert [r["exchange_id"] for r in view["exchanges"]] == ["e1", "e2", "e3a", "e3b", "e3c"]
    e1 = row(view, "e1")
    assert set(e1) == ROW_KEYS
    assert e1["market_id"] == "m1" and e1["title"] == "Will Republicans win the Pennsylvania Senate race?"
    assert e1["option"] is None and e1["settlement_date"] == "2026-11-04T04:59:00.000Z"
    assert (e1["mark"], e1["last"], e1["bid"], e1["ask"], e1["spread"]) == (0.40, 0.40, 0.39, 0.41, 0.02)
    assert e1["sparkline"] == [[T0, 0.40]] and e1["updated_at"] == T0
    assert e1["surge_id"] is None and e1["high_band"] is None and e1["change_5m"] is None
    assert row(view, "e3b")["option"] == "Bob"

    # the 0.97 outcome is in the high band
    assert row(view, "e2")["high_band"]["side"] == "YES"
    assert [b["exchange_id"] for b in view["high_band"]] == ["e2"]
    assert view["high_band"][0]["title"] == "Will Democrats win the Ohio Senate race?"

    status = view["status"]
    assert status["cycles"] == 1 and status["last_snapshot_at"] == T0 and status["last_cycle_at"] == T0
    assert status["markets"] == 3 and status["exchanges"] == 5 and status["ticks_recorded"] == 5
    assert status["errors"] == 0 and status["fatal_error"] is None and status["running"] is False
    assert status["read_budget"]["client"]["limit"] == 10_000
    assert status["read_budget"]["backfill"]["limit"] == 30 and status["read_budget"]["analysis"]["limit"] == 20
    assert status["queues"] == {"analysis": 0, "backfill": 0}


def test_view_before_the_first_cycle(make_tracker: Any) -> None:
    view = make_tracker().view()
    json.dumps(view)
    assert set(view) == VIEW_KEYS
    assert view["exchanges"] == [] and view["surges"] == [] and view["high_band"] == []
    assert view["context"]["slug"] == SLUG and view["context"]["balance"] is None
    assert view["status"]["cycles"] == 0 and view["status"]["last_snapshot_at"] is None


def test_view_returns_a_copy(make_tracker: Any) -> None:
    tracker = make_tracker()
    tracker.run_once()
    view = tracker.view()
    view["exchanges"][0]["sparkline"].append([0, 0])
    view["exchanges"].clear()
    view["context"]["overround"].clear()
    again = tracker.view()
    assert len(again["exchanges"]) == 5 and again["exchanges"][0]["sparkline"] == [[T0, 0.40]]
    assert again["context"]["overround"]


def test_market_list_refreshes_only_when_due(make_tracker: Any, world: World, fake: Any, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    tracker.run_once()
    world.markets.append(market("m4", "Will Democrats win the Maine Governor race?", [("e4", None, 0.62)]))
    world.set("e4", 0.62)
    step(tracker, data_clock, 120)
    assert len(list_calls(fake)) == 1
    assert "e4" not in [r["exchange_id"] for r in tracker.view()["exchanges"]]
    summary = step(tracker, data_clock, 180)  # 300 s since the first refresh
    assert summary["markets_refreshed"] and summary["exchanges"] == 6
    assert len(list_calls(fake)) == 2
    assert row(tracker.view(), "e4")["mark"] == 0.62
    assert len(fake.calls_to("/exchanges/prices")) == 3


def test_closed_markets_leave_the_view(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker(market_refresh=60)
    tracker.run_once()
    world.markets = world.markets[:1]
    step(tracker, data_clock)
    assert [r["exchange_id"] for r in tracker.view()["exchanges"]] == ["e1"]
    assert tracker.store.exchange("e2") is not None  # metadata is kept for history


# --------------------------------------------------------------------------- surges


def test_surge_detected_queued_and_shown(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    attributor = FakeAttributor()
    tracker = make_tracker(attributor=attributor)
    summary = jump_scenario(tracker, world, data_clock)
    surges = tracker.store.surges()
    assert len(surges) == 1
    s = surges[0]
    assert summary["new_surges"] == [s.id] and summary["surges"] == [s.id] and summary["queued"] == [s.id]
    assert (s.exchange_id, s.market_id, s.direction, s.change, s.start_price, s.peak_price) == ("e1", "m1", "up", 0.2, 0.40, 0.60)
    assert (s.start_ts, s.end_ts, s.detected_at) == (T0 + 60, T0 + 360, T0 + 360)
    assert s.status == SURGE_OPEN and s.current_price == 0.60 and s.reverted_fraction == 0.0

    view = tracker.view()
    json.dumps(view)
    assert row(view, "e1")["surge_id"] == s.id and row(view, "e1")["change_5m"] == 0.2
    card = view["surges"][0]
    assert card["id"] == s.id and card["title"] == "Will Republicans win the Pennsylvania Senate race?"
    assert card["option"] is None and card["attribution"] is None
    assert view["status"]["queues"]["analysis"] == 1
    assert attributor.limiter is tracker.analyze_limiter  # the attributor shares the analysis budget


def test_redetections_merge_into_one_surge(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker(attributor=FakeAttributor())
    first = jump_scenario(tracker, world, data_clock)
    sid = first["new_surges"][0]
    again = step(tracker, data_clock)  # still 0.60: re-detected through the window
    assert again["surges"] == [sid] and again["new_surges"] == [] and again["queued"] == []
    merged = tracker.store.get_surge(sid)
    # the same move seen again: the frame stays at the spike (r2-fixcheck-3)
    assert merged.start_ts == T0 + 60 and merged.end_ts == T0 + 360 and merged.detected_at == T0 + 360
    assert len(tracker.store.surges()) == 1
    assert tracker.status()["queues"]["analysis"] == 1  # no duplicate queue entry


def test_grown_surge_is_requeued_after_analysis(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    attributor = FakeAttributor()
    tracker = make_tracker(attributor=attributor)
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    assert tracker.analyze_pending(5) == 1
    world.set("e1", 0.62)  # |change| 0.22: grew 0.02 since the analysis
    assert step(tracker, data_clock)["queued"] == []
    world.set("e1", 0.64)  # |change| 0.24: grew 0.04
    assert step(tracker, data_clock)["queued"] == [sid]
    assert tracker.analyze_pending(5) == 1
    assert [c.change for c in attributor.calls] == [0.2, 0.24]
    assert tracker.store.get_state(ANALYZED_STATE_KEY) == {str(sid): 0.24}


def test_analysis_baseline_survives_a_restart(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    first = make_tracker(attributor=FakeAttributor())
    sid = jump_scenario(first, world, data_clock)["new_surges"][0]
    first.analyze_pending(1)

    second = make_tracker(store=first.store, attributor=FakeAttributor())
    world.set("e1", 0.62)
    assert step(second, data_clock)["queued"] == []  # baseline 0.20 loaded from the store
    world.set("e1", 0.65)
    assert step(second, data_clock)["queued"] == [sid]


def test_reverted_surge_is_not_recorded_again(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]  # 0.40 -> 0.60 at T0+360
    world.set("e1", 0.47)
    step(tracker, data_clock)  # merged, then 65% given back: reverted
    assert tracker.store.get_surge(sid).status == SURGE_REVERTED
    for _ in range(2):  # the window still shows +0.07 from 0.40, but that is the same move
        assert step(tracker, data_clock)["surges"] == []
    assert len(tracker.store.surges()) == 1
    assert tracker.view()["exchanges"][0]["surge_id"] is None  # not open any more

    world.set("e1", 0.66)  # beyond the old peak: a new move
    summary = step(tracker, data_clock)
    assert len(summary["new_surges"]) == 1 and summary["new_surges"][0] != sid
    assert len(tracker.store.surges()) == 2


def test_surges_are_recorded_without_attributor_or_with_analysis_off(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    plain = make_tracker()
    summary = jump_scenario(plain, world, data_clock)
    assert summary["new_surges"] and summary["queued"] == []
    assert plain.analyze_pending(5) == 0 and plain.request_analysis(summary["new_surges"][0]) is False

    world.set("e1", 0.40)
    data_clock.now = T0
    off = make_tracker(attributor=FakeAttributor(), analyze=False)
    summary = jump_scenario(off, world, data_clock)
    assert summary["new_surges"] and summary["queued"] == []
    assert off.request_analysis(summary["new_surges"][0]) is True  # manual requests still work
    assert off.analyze_pending(5) == 1


def test_open_surges_get_status_updates(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    world.set("e1", 0.54)
    for _ in range(5):  # p_then catches up with the jump: no more re-detection
        step(tracker, data_clock)
    s = tracker.store.get_surge(sid)
    assert s.status == SURGE_OPEN and s.current_price == 0.54 and s.reverted_fraction == 0.3
    step(tracker, data_clock, 86400)
    assert tracker.store.get_surge(sid).status == SURGE_HELD


def test_analytics_failures_do_not_break_the_cycle(make_tracker: Any, monkeypatch: Any) -> None:
    def broken(*args: Any, **kwargs: Any) -> List[Surge]:
        raise ValueError("boom")

    monkeypatch.setattr(FAKE_ANALYTICS, "detect_surges", broken)
    tracker = make_tracker()
    summary = tracker.run_once()
    assert summary["errors"] == 1
    status = tracker.status()
    assert status["last_error"]["where"] == "analytics" and "5 outcome(s)" in status["last_error"]["error"]
    view = tracker.view()
    assert row(view, "e1")["mark"] == 0.40  # prices still shown
    json.dumps(view)


# --------------------------------------------------------------------------- attribution


def test_analyze_pending_persists_attribution_and_updates_view(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    attributor = FakeAttributor()
    tracker = make_tracker(attributor=attributor)
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    assert tracker.analyze_pending(5) == 1
    assert tracker.analyze_pending(5) == 0
    assert attributor.calls[0].id == sid
    stored = tracker.store.get_surge(sid).attribution
    assert stored.verdict == "participants" and isinstance(stored.articles[0], Article)
    assert stored.flow == TradeFlow(n_trades=2, total_size=900.0)
    assert stored.analyzed_at == data_clock.now  # stamped by the tracker when the attributor did not
    card = tracker.view()["surges"][0]
    assert card["attribution"]["verdict"] == "participants"  # visible before the next cycle
    status = tracker.status()
    assert status["analysis"]["analyzed"] == 1 and status["queues"]["analysis"] == 0
    # the next cycle's status update must not erase the analysis
    step(tracker, data_clock)
    assert tracker.store.get_surge(sid).attribution.verdict == "participants"
    assert tracker.view()["surges"][0]["attribution"]["summary"] == f"surge {sid} by a few traders"


def test_attributor_errors_are_counted_and_fatal_errors_stop(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    attributor = FakeAttributor(fail=RuntimeError("news down"))
    tracker = make_tracker(attributor=attributor)
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    assert tracker.analyze_pending(1) == 0
    status = tracker.status()
    assert status["errors"] == 1 and "news down" in status["last_error"]["error"]
    assert status["queues"]["analysis"] == 0 and tracker.store.get_surge(sid).attribution is None

    attributor.fail = ApiError(401, "INVALID_API_KEY", "bad key")
    assert tracker.request_analysis(sid)
    with pytest.raises(ApiError):
        tracker.analyze_pending(1)
    assert "INVALID_API_KEY" in tracker.status()["fatal_error"]


def test_request_analysis(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker(attributor=FakeAttributor(), analyze=False)
    first = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    other = tracker.store.record_surge(Surge("e2", "m2", "1h", 3600.0, T0, T0 + 60, 0.9, 0.97, 0.07, "up", 0.97, T0 + 60)).id
    assert tracker.request_analysis(999) is False
    assert tracker.request_analysis("abc") is False  # type: ignore[arg-type]
    assert tracker.request_analysis(first) is True
    assert tracker.request_analysis(str(other)) is True  # type: ignore[arg-type]
    assert tracker.request_analysis(first) is True  # already queued: no duplicate
    assert tracker.status()["queues"]["analysis"] == 2
    tracker.analyze_pending(1)
    assert tracker.attributor.calls[0].id == other  # manual requests jump the queue


# --------------------------------------------------------------------------- backfill


def test_backfill_step_plan_order_and_storage(make_tracker: Any, world: World, fake: Any, data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
    assert tracker.backfill_step(5) == 0  # nothing planned before the market list
    tracker.run_once()
    assert tracker.status()["backfill"] == {"enabled": True, "total": 10, "done": 0, "failed": 0, "pending": 10}
    assert tracker.backfill_step(3) == 3
    order = [(c.path.split("/")[2], c.params["resolution"]) for c in fake.calls if c.path.endswith("/price-history")]
    assert order == [("e1", "1h"), ("e2", "1h"), ("e3a", "1h")]
    assert fake.calls_to("/exchanges/e1/price-history")[0].params == {"tournamentId": TOURNAMENT_ID, "resolution": "1h", "limit": "168"}
    assert tracker.backfill_step(100) == 7
    assert tracker.backfill_step(100) == 0
    order = [(c.path.split("/")[2], c.params["resolution"]) for c in fake.calls if c.path.endswith("/price-history")]
    assert order[5:] == [("e1", "5m"), ("e2", "5m"), ("e3a", "5m"), ("e3b", "5m"), ("e3c", "5m")]
    assert fake.calls_to("/exchanges/e1/price-history")[1].params["limit"] == "288"
    assert tracker.store.has_candles("e1", "1h") and tracker.store.has_candles("e3c", "5m")
    assert tracker.store.get_state("backfill:e1:5m") == {"at": T0, "candles": 288}
    status = tracker.status()
    assert status["backfill"] == {"enabled": True, "total": 10, "done": 10, "failed": 0, "pending": 0}
    assert status["read_budget"]["backfill"]["used"] == 10


def test_backfilled_candles_reach_the_series_cache(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
    tracker.run_once()
    step(tracker, data_clock)
    assert len(row(tracker.view(), "e1")["sparkline"]) == 2
    tracker.backfill_step(100)
    step(tracker, data_clock)
    spark = row(tracker.view(), "e1")["sparkline"]
    assert len(spark) == 96  # 24 h of 5m candle closes + ticks, downsampled
    assert spark[0][0] >= data_clock.now - 86400 and spark[-1] == [data_clock.now, 0.40]
    assert row(tracker.view(), "e1")["change_24h"] == 0.0


def test_backfill_after_restart_only_when_tracking_had_a_gap(make_tracker: Any, world: World, fake: Any, data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    store = TrackerStore()
    first = make_tracker(store=store, backfill=True, backfill_reads_per_min=1000)
    first.run_once()
    first.backfill_step(100)
    for _ in range(3):
        step(first, data_clock)
    calls = len([c for c in fake.calls if c.path.endswith("/price-history")])

    data_clock.now += 30  # quick restart: data right up to the restart
    quick = make_tracker(store=store, backfill=True, backfill_reads_per_min=1000)
    quick.run_once()
    assert quick.status()["backfill"] == {"enabled": True, "total": 10, "done": 10, "failed": 0, "pending": 0}
    assert quick.backfill_step(100) == 0
    assert len([c for c in fake.calls if c.path.endswith("/price-history")]) == calls

    data_clock.now += 2 * 86400  # restart after two days off: refetch
    later = make_tracker(store=store, backfill=True, backfill_reads_per_min=1000)
    later.run_once()
    assert later.status()["backfill"]["pending"] == 10
    assert later.backfill_step(100) == 10


def test_backfill_failures_retry_then_give_up(make_client: Any, make_tracker: Any, world: World, fake: Any, data_clock: FakeClock) -> None:
    world.add_history(data_clock, ["e1", "e3a", "e3b", "e3c"])
    fake.add("GET", "/exchanges/e2/price-history", (503, error_body("SERVICE_UNAVAILABLE", "busy")))
    world.markets = world.markets[:2]
    tracker = make_tracker(client=make_client(max_retries=0), backfill=True, backfill_reads_per_min=1000)
    tracker.run_once()
    fake.add("GET", "/exchanges/e1/price-history", (404, error_body("NOT_FOUND", "gone")))
    fake.routes[("GET", "/exchanges/e1/price-history")].popleft()  # e1 now answers 404 (permanent)
    # e1 1h (404, permanent), e2 1h x3 (503, transient: re-queued until 3 attempts), then the same for 5m
    assert tracker.backfill_step(100) == 8
    assert tracker.backfill_step(100) == 0  # nothing left to try
    status = tracker.status()
    assert status["backfill"] == {"enabled": True, "total": 4, "done": 0, "failed": 4, "pending": 0}
    assert len(fake.calls_to("/exchanges/e2/price-history")) == 6  # 3 attempts per resolution
    assert len(fake.calls_to("/exchanges/e1/price-history")) == 2  # 404 is not retried
    assert status["errors"] == 8


def test_backfill_disabled(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker(backfill=False)
    tracker.run_once()
    assert tracker.backfill_step(10) == 0
    assert tracker.status()["backfill"] == {"enabled": False, "total": 0, "done": 0, "failed": 0, "pending": 0}
    assert not [c for c in fake.calls if c.path.endswith("/price-history")]


# --------------------------------------------------------------------------- context


def test_context_refresh(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker()
    tracker.run_once()
    ctx = tracker.view()["context"]
    assert ctx["tournament_id"] == TOURNAMENT_ID and ctx["slug"] == SLUG and ctx["name"] == "SIG Predictions Cup"
    assert ctx["balance"] == 101250.5 and ctx["initial_balance"] == 100000.0
    assert ctx["tournament"]["endDate"] == "2026-11-04T17:00:00.000Z"
    board = ctx["leaderboard"]
    assert board["my_rank"] == 17 and board["total"] == 120
    assert board["leader_value"] == 105000.0  # initialBalance + pnl
    assert [(e["rank"], e["username"], e["value"]) for e in board["top"]] == [
        (1, "alice", 105000.0), (2, "bob", 103000.0), (3, None, 102500.0),
    ]
    assert ctx["constraints"]["violationsCount"] == 1 and ctx["constraints"]["data"][0]["relationshipId"] == "r1"
    assert ctx["overround"] == [
        {"market_id": "m3", "title": "Who will win the Arizona Governor race?", "overround": 1.04, "arbitrage": True,
         "outcomes": 3, "at": T0}
    ]
    assert (ctx["account_value"], ctx["positions_value"]) == (103_750.5, 2_500.0)  # cash 101,250.5 + positions
    assert ctx["updated_at"] == T0
    assert fake.calls_to("/relationships/constraints")[0].params == {"violationsOnly": "true", "tournamentId": TOURNAMENT_ID}
    assert fake.calls_to(f"/tournaments/{SLUG}/leaderboard")[0].params["limit"] == "100"  # §7.2: 100 rows for the bar
    assert fake.calls_to("/markets/m3/orderbook")[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "20"}
    assert len(fake.calls_to("/markets/m1/orderbook")) == 0  # binary markets are not read


def test_context_refreshes_only_when_due_and_pieces_fail_independently(make_tracker: Any, fake: Any, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    tracker.run_once()
    step(tracker, data_clock, 120)
    assert len(fake.calls_to("/relationships/constraints")) == 1

    fake.add("GET", "/relationships/constraints", (400, error_body("VALIDATION_ERROR", "nope")))
    fake.routes[("GET", "/relationships/constraints")].popleft()
    fake.add("GET", f"/tournaments/{SLUG}/leaderboard", (500, error_body("INTERNAL", "oops")))
    fake.routes[("GET", f"/tournaments/{SLUG}/leaderboard")].popleft()
    fake.add("GET", "/markets/m3/orderbook", (404, error_body("NOT_FOUND", "gone")))
    fake.routes[("GET", "/markets/m3/orderbook")].popleft()
    fake.add("GET", f"/tournaments/{SLUG}", {**TOURNAMENT_INFO, "myBalance": 99000.0})
    fake.routes[("GET", f"/tournaments/{SLUG}")].popleft()
    summary = step(tracker, data_clock, 180)
    assert summary["context_refreshed"] and summary["errors"] == 3
    ctx = tracker.view()["context"]
    assert ctx["balance"] == 99000.0  # this piece worked
    assert ctx["constraints"]["violationsCount"] == 1  # previous values kept
    assert ctx["leaderboard"]["leader_value"] == 105000.0
    assert ctx["overround"][0]["overround"] == 1.04
    assert ctx["updated_at"] == T0 + 300
    assert {e["where"] for e in tracker.status()["recent_errors"]} == {"constraints", "leaderboard", "orderbook for market m3"}


def test_overround_rotates_through_multi_outcome_markets(make_tracker: Any, world: World, fake: Any, data_clock: FakeClock) -> None:
    for k in range(4, 7):
        world.markets.append(market(f"m{k}", f"Who wins race {k}?", [(f"x{k}a", "A", 0.5), (f"x{k}b", "B", 0.5)]))
        world.set(f"x{k}a", 0.5)
        world.set(f"x{k}b", 0.5)
        fake.add("GET", f"/markets/m{k}/orderbook", market_orderbook([], overround=1.0 + k / 100))
    tracker = make_tracker(max_overround_markets=2)
    tracker.run_once()
    assert [r["market_id"] for r in tracker.view()["context"]["overround"]] == ["m3", "m4"]
    # m3 is flagged for arbitrage: it is re-read at every refresh, the others take turns
    step(tracker, data_clock, 300)
    assert [r["market_id"] for r in tracker.view()["context"]["overround"]] == ["m3", "m4", "m5"]
    step(tracker, data_clock, 300)
    assert [r["market_id"] for r in tracker.view()["context"]["overround"]] == ["m3", "m4", "m5", "m6"]
    assert len(fake.calls_to("/markets/m3/orderbook")) == 3
    assert tracker.view()["context"]["overround"][3]["overround"] == 1.06


def test_public_context_skips_tournament_reads(make_tracker: Any, world: World, fake: Any) -> None:
    fake.add("GET", "/markets", lambda r: httpx.Response(200, json=market_page(world.markets)))
    tracker = make_tracker(context=Context.public())
    tracker.run_once()
    ctx = tracker.view()["context"]
    assert ctx["balance"] is None and ctx["leaderboard"] is None and ctx["constraints"]["violationsCount"] == 1
    assert fake.calls_to(f"/tournaments/{SLUG}") == [] and fake.calls_to(f"/tournaments/{SLUG}/leaderboard") == []
    assert "tournamentId" not in fake.calls_to("/exchanges/prices")[0].params


def test_leaderboard_value() -> None:
    assert leaderboard_value({"pnl": 250.0}, 100000.0) == 100250.0
    assert leaderboard_value({"pnl": 250.0, "portfolioValue": 123.0}, 100000.0) == 123.0
    assert leaderboard_value({"pnl": 250.0}, None) is None
    assert leaderboard_value({}, 100000.0) is None


# --------------------------------------------------------------------------- errors


def test_fatal_error_in_run_once(make_tracker: Any, fake: Any) -> None:
    fake.add("GET", "/exchanges/prices", (401, error_body("INVALID_API_KEY", "bad key")))
    fake.routes[("GET", "/exchanges/prices")].popleft()
    tracker = make_tracker()
    with pytest.raises(ApiError) as info:
        tracker.run_once()
    assert info.value.code == "INVALID_API_KEY"
    status = tracker.status()
    assert "INVALID_API_KEY" in status["fatal_error"] and status["running"] is False
    assert status["errors"] == 0  # fatal errors are reported separately
    assert tracker.view()["status"]["fatal_error"] == status["fatal_error"]


def test_transient_errors_are_counted_and_the_next_cycle_retries(make_tracker: Any, fake: Any, world: World, data_clock: FakeClock) -> None:
    fake.routes[("GET", f"/tournaments/{SLUG}/markets")].appendleft((400, error_body("VALIDATION_ERROR", "bad cursor")))
    tracker = make_tracker()
    summary = tracker.run_once()
    assert summary["errors"] == 1 and summary["markets"] == 0 and summary["ticks"] == 0
    assert tracker.status()["last_error"]["where"] == "market list"
    summary = step(tracker, data_clock)
    assert summary["markets_refreshed"] and summary["ticks"] == 5 and summary["errors"] == 0
    assert tracker.status()["errors"] == 1


def test_cancel_during_stop_is_not_fatal(make_tracker: Any, client: Any) -> None:
    tracker = make_tracker()
    tracker.stop()
    client.cancel()
    summary = tracker.run_once()
    assert summary["stopped"] is True
    assert tracker.status()["fatal_error"] is None


def test_invalid_interval() -> None:
    with pytest.raises(ValueError):
        Tracker(None, CTX, TrackerStore(), interval=0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- threads


def test_start_stop_quickly_and_restart(make_tracker: Any, world: World, client: Any, data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    attributor = FakeAttributor()
    tracker = make_tracker(interval=0.05, backfill=True, attributor=attributor)
    tracker.start()
    tracker.start()  # idempotent while running
    assert wait_for(lambda: tracker.status()["cycles"] >= 3)
    assert wait_for(lambda: tracker.status()["backfill"]["done"] == 10)
    assert tracker.status()["running"] is True
    sid = tracker.store.record_surge(Surge("e1", "m1", "5m", 300.0, T0, T0 + 60, 0.4, 0.6, 0.2, "up", 0.6, T0 + 60)).id
    assert tracker.request_analysis(sid)
    assert wait_for(lambda: tracker.store.get_surge(sid).attribution is not None)

    started = time.monotonic()
    tracker.stop(timeout=2)
    assert time.monotonic() - started < 2
    assert not any(t.is_alive() for t in tracker._threads)
    assert tracker.status()["running"] is False and client.cancelled

    cycles = tracker.status()["cycles"]
    tracker.start()  # restart resets the client's cancel flag
    assert not client.cancelled
    assert wait_for(lambda: tracker.status()["cycles"] > cycles)
    tracker.stop(timeout=2)
    assert tracker.status()["fatal_error"] is None
    json.dumps(tracker.view())


def test_stop_interrupts_budget_and_interval_waits(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    tracker = make_tracker(interval=60, backfill=True, backfill_reads_per_min=1)
    tracker.start()
    assert wait_for(lambda: tracker.status()["backfill"]["done"] == 1)
    time.sleep(0.05)  # the backfill worker is now waiting ~60 s for budget
    started = time.monotonic()
    tracker.stop(timeout=5)
    assert time.monotonic() - started < 1.5
    assert not any(t.is_alive() for t in tracker._threads)
    assert tracker.status()["backfill"]["pending"] == 9  # the interrupted task went back to the queue


def test_fatal_error_stops_the_threads(make_tracker: Any, fake: Any) -> None:
    fake.add("GET", "/exchanges/prices", (401, error_body("API_KEY_REVOKED", "revoked")))
    fake.routes[("GET", "/exchanges/prices")].popleft()
    tracker = make_tracker(interval=0.05, attributor=FakeAttributor())
    tracker.start()
    assert wait_for(lambda: tracker.status()["fatal_error"] is not None)
    assert wait_for(lambda: not any(t.is_alive() for t in tracker._threads))
    status = tracker.status()
    assert "API_KEY_REVOKED" in status["fatal_error"] and status["running"] is False and status["cycles"] == 0


def test_loop_keeps_going_after_an_unexpected_error(make_tracker: Any, monkeypatch: Any) -> None:
    tracker = make_tracker(interval=0.02)
    calls = {"n": 0}
    original = tracker.store.add_ticks

    def flaky(ts: float, rows: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        return original(ts, rows)

    monkeypatch.setattr(tracker.store, "add_ticks", flaky)
    tracker.start()
    assert wait_for(lambda: tracker.status()["cycles"] >= 2)
    tracker.stop(timeout=2)
    status = tracker.status()
    assert status["recent_errors"][0]["where"] == "cycle" and "disk hiccup" in status["recent_errors"][0]["error"]
    assert status["fatal_error"] is None


def test_concurrent_view_reads_during_cycles(make_tracker: Any, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    errors: List[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                json.dumps(tracker.view())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for _ in range(20):
            step(tracker, data_clock)
    finally:
        stop.set()
        for t in readers:
            t.join(5)
    assert not errors
    assert tracker.status()["cycles"] == 20


# --------------------------------------------------------------------------- integration: real analytics + attribution


class TestRealAnalytics:
    real_analytics = True

    def test_backfill_surge_and_high_band(self, make_tracker: Any, world: World, data_clock: FakeClock) -> None:
        world.add_history(data_clock)
        attributor = FakeAttributor()
        tracker = make_tracker(backfill=True, backfill_reads_per_min=1000, attributor=attributor)
        tracker.run_once()
        assert tracker.backfill_step(100) == 10
        for _ in range(9):
            step(tracker, data_clock)  # flat at the candle level
        assert tracker.store.surges() == []
        view = tracker.view()
        band = row(view, "e2")["high_band"]
        assert band is not None and band["side"] == "YES" and band["stable"] is True and band["time_in_band"] == 1.0
        assert [b["exchange_id"] for b in view["high_band"]] == ["e2"]
        assert row(view, "e1")["change_24h"] == 0.0 and len(row(view, "e1")["sparkline"]) == 96

        world.set("e1", 0.60)
        summary = step(tracker, data_clock)
        assert len(summary["new_surges"]) == 1 and summary["queued"] == summary["new_surges"]
        s = tracker.store.get_surge(summary["new_surges"][0])
        assert (s.window, s.direction, s.change, s.start_price, s.end_price) == ("5m", "up", 0.2, 0.40, 0.60)
        assert s.zscore is None  # flat history: no volatility to scale by
        view = tracker.view()
        json.dumps(view)
        e1 = row(view, "e1")
        assert e1["surge_id"] == s.id and e1["change_5m"] == 0.2 and e1["change_1h"] == 0.2 and e1["change_24h"] == 0.2
        assert tracker.analyze_pending(1) == 1 and tracker.view()["surges"][0]["attribution"]["verdict"] == "participants"

    def test_real_attributor_end_to_end(self, make_tracker: Any, world: World, fake: Any, client: Any, data_clock: FakeClock) -> None:
        from supermarket_bot.attribution import Attributor

        store = TrackerStore()
        attributor = Attributor(client, CTX, store, news=None, clock=data_clock)
        tracker = make_tracker(store=store, attributor=attributor)
        jump_at = T0 + 600
        fake.add("GET", "/exchanges/e1/trades", {
            "exchangeId": "e1", "marketId": "m1", "from": iso_ts(T0), "to": iso_ts(jump_at),
            "data": [
                {"id": "902", "createdAt": iso_ts(jump_at - 30), "price": 0.60, "size": 600, "side": "YES", "volume": 360},
                {"id": "901", "createdAt": iso_ts(jump_at - 90), "price": 0.52, "size": 300, "side": "YES", "volume": 156},
            ],
            "pagination": {"limit": 200, "hasMore": False, "nextCursor": None},
            "coverage": {"complete": True, "projectedThroughSequence": 1},
        })
        replace_route(fake, "/exchanges/e1/orderbook", book_exchange("e1", [(0.59, 50)], [(0.61, 60)]))

        tracker.run_once()
        for _ in range(9):
            step(tracker, data_clock)
        world.set("e1", 0.60)
        sid = step(tracker, data_clock)["new_surges"][0]
        assert attributor.limiter is tracker.analyze_limiter
        assert tracker.analyze_pending(1) == 1

        surge = store.get_surge(sid)
        attribution = surge.attribution
        assert attribution is not None and attribution.verdict == "participants"
        assert attribution.flow.n_trades == 2 and attribution.book_depth == 110.0
        assert [t.trade_id for t in store.trades("e1", T0)] == ["901", "902"]
        trade_call = fake.calls_to("/exchanges/e1/trades")[0]
        assert trade_call.params["from"] == iso_ts(surge.start_ts - 3600) and trade_call.params["tournamentId"] == TOURNAMENT_ID
        assert fake.calls_to("/exchanges/e1/orderbook")[0].params == {"depth": "20", "tournamentId": TOURNAMENT_ID}
        assert tracker.status()["read_budget"]["analysis"]["used"] == 2
        card = tracker.view()["surges"][0]
        assert card["attribution"]["verdict"] == "participants"
        json.dumps(tracker.view())


# --------------------------------------------------------------------------- round-1 QA regressions


def problems(tracker: Tracker) -> Dict[str, Dict[str, Any]]:
    return {p["source"]: p for p in tracker.status()["problems"]}


def replace_route(fake: Any, path: str, *replies: Any) -> None:
    fake.routes.pop(("GET", path), None)
    fake.add("GET", path, *replies)


FORBIDDEN_MEMBER = (403, error_body("FORBIDDEN", "You are not a member of this tournament."))
STANDINGS_UPDATING = (
    503,
    error_body("SERVICE_UNAVAILABLE", "Standings are updating.", {"reason": "STANDINGS_UPDATING", "retryAfterSeconds": 10}),
    {"Retry-After": "10"},
)


def test_live_api_1_leaderboard_403_is_not_fatal(make_tracker: Any, fake: Any, world: World, data_clock: FakeClock) -> None:
    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", FORBIDDEN_MEMBER)
    tracker = make_tracker(context_refresh=60)
    summary = tracker.run_once()
    status = tracker.status()
    assert status["fatal_error"] is None and summary["ticks"] == 5 and summary["context_refreshed"]
    view = tracker.view()
    assert len(view["exchanges"]) == 5
    assert view["context"]["leaderboard"] is None and view["context"]["balance"] == 101250.5  # the rest still loads
    p = problems(tracker)["leaderboard"]
    assert p["severity"] == "warning" and p["count"] == 1 and p["since"] == p["last"] == T0
    assert "403 FORBIDDEN: You are not a member of this tournament." in p["message"]
    assert set(p) == {"source", "message", "since", "last", "count", "severity"}

    step(tracker, data_clock)
    p = problems(tracker)["leaderboard"]
    assert p["count"] == 2 and p["since"] == T0 and p["last"] == T0 + 60
    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", LEADERBOARD)
    step(tracker, data_clock)
    assert "leaderboard" not in problems(tracker)
    assert tracker.view()["context"]["leaderboard"]["my_rank"] == 17


def test_live_api_1_other_optional_403s_are_problems_not_stops(make_tracker: Any, fake: Any) -> None:
    replace_route(fake, "/relationships/constraints", FORBIDDEN_MEMBER)
    replace_route(fake, f"/tournaments/{SLUG}", FORBIDDEN_MEMBER)
    replace_route(fake, "/markets/m3/orderbook", FORBIDDEN_MEMBER)
    tracker = make_tracker()
    tracker.run_once()
    assert tracker.status()["fatal_error"] is None
    assert {"constraints", "balance", "order books"} <= set(problems(tracker))
    assert "order book read(s) failed: HTTP 403 FORBIDDEN" in problems(tracker)["order books"]["message"]


def test_live_api_1_threaded_tracker_keeps_running_after_a_leaderboard_403(make_tracker: Any, fake: Any) -> None:
    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", FORBIDDEN_MEMBER)
    tracker = make_tracker(interval=0.02)
    tracker.start()
    try:
        assert wait_for(lambda: "leaderboard" in problems(tracker))
        assert wait_for(lambda: tracker.status()["cycles"] >= 3)
        assert tracker.status()["running"] is True and tracker.status()["fatal_error"] is None
        assert len(tracker.view()["exchanges"]) == 5
    finally:
        tracker.stop(timeout=2)


def test_live_api_9_context_reads_are_single_attempts(make_tracker: Any, fake: Any, client: Any, clock: FakeClock,
                                                      sleeper: Any) -> None:
    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", STANDINGS_UPDATING)
    tracker = make_tracker()
    tracker.run_once()
    assert len(fake.calls_to(f"/tournaments/{SLUG}/leaderboard")) == 1  # no inline retries
    assert sleeper.calls == []  # and no Retry-After wait inside the cycle
    assert client.read_limiter._paused_until <= clock.now  # the optional read does not hold snapshots back
    assert client.max_retries == 4  # snapshots keep the client's retries
    assert "STANDINGS_UPDATING" not in problems(tracker)["leaderboard"]["message"]  # the message, not the details
    assert "Standings are updating." in problems(tracker)["leaderboard"]["message"]
    assert tracker.status()["read_budget"]["requests_sent"] == len(fake.calls)


def test_live_api_9_slow_context_reads_do_not_stall_price_snapshots(make_tracker: Any, fake: Any) -> None:
    gate = threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        gate.wait(5)
        return httpx.Response(200, json=LEADERBOARD)

    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", slow)
    tracker = make_tracker(interval=0.02)
    tracker.start()
    try:
        assert wait_for(lambda: len(fake.calls_to(f"/tournaments/{SLUG}/leaderboard")) == 1)
        before = tracker.status()["cycles"]
        assert wait_for(lambda: tracker.status()["cycles"] >= before + 5)  # snapshots go on meanwhile
        assert {t.name for t in tracker._threads} >= {"tracker-loop", "tracker-context"}
    finally:
        gate.set()
        tracker.stop(timeout=2)
    assert tracker.view()["context"]["leaderboard"]["my_rank"] == 17


def test_live_api_9_rate_limits_still_pause_every_caller(make_tracker: Any, fake: Any, client: Any, clock: FakeClock) -> None:
    replace_route(fake, f"/tournaments/{SLUG}/leaderboard", (429, error_body("RATE_LIMITED", "slow down"), {"Retry-After": "7"}))
    tracker = make_tracker()
    start = clock.now
    tracker.run_once()
    assert client.read_limiter._paused_until == pytest.approx(start + 7)  # the account budget is shared
    assert clock.now >= start + 7  # the next read (an order book) waited it out


def test_live_api_6_backfill_stays_inside_its_budget_when_history_503s(make_tracker: Any, world: World, fake: Any,
                                                                      data_clock: FakeClock, sleeper: Any) -> None:
    for eid in world.quotes:
        fake.add("GET", f"/exchanges/{eid}/price-history", (503, error_body("SERVICE_UNAVAILABLE", "busy")))
    tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)  # the client retries 503s 4 times
    tracker.run_once()
    reads = tracker.backfill_step(100)
    calls = [c for c in fake.calls if c.path.endswith("/price-history")]
    # one request per attempt, charged to the backfill budget; then the whole queue rests
    assert reads == len(calls) == tracker_mod.BACKFILL_FAIL_STREAK
    assert tracker.status()["read_budget"]["backfill"]["used"] == reads
    assert sleeper.calls == []
    assert tracker.backfill_step(100) == 0  # resting: no more reads for now
    p = problems(tracker)["price history"]
    assert p["count"] == reads and "503 SERVICE_UNAVAILABLE" in p["message"]
    assert tracker.status()["backfill"]["pending"] == 10 and tracker.status()["backfill"]["failed"] == 0

    # price history comes back: after the rest the queue drains and the problem clears
    world.add_history(data_clock)
    for eid in world.quotes:
        fake.routes[("GET", f"/exchanges/{eid}/price-history")].popleft()
    tracker._bf_pause_until = 0.0
    assert tracker.backfill_step(100) == 10
    assert "price history" not in problems(tracker)
    assert tracker.status()["backfill"]["done"] == 10


def test_live_api_6_single_attempt_client_shares_budget_and_cancel(client: Any) -> None:
    view = tracker_mod.single_attempt_client(client)
    assert view is not client and view.max_retries == 0 and client.max_retries == 4
    before = client.read_limiter.used
    view.read_limiter.acquire()
    assert client.read_limiter.used == before + 1 and view.read_limiter.limit == client.read_limiter.limit
    view.read_limiter.pause(30)  # server waits stay with the worker
    assert client.read_limiter._paused_until == 0.0
    client.cancel()
    assert view.cancelled
    client.reset_cancel()
    sentinel = object()
    assert tracker_mod.single_attempt_client(sentinel) is sentinel


def test_live_api_7_surges_of_closed_markets_are_closed(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    from supermarket_bot.models import SURGE_CLOSED

    tracker = make_tracker(market_refresh=60)
    summary = jump_scenario(tracker, world, data_clock)
    [sid] = summary["new_surges"]
    assert tracker.store.get_surge(sid).status == SURGE_OPEN
    everything = list(world.markets)
    world.markets = [m for m in world.markets if m["id"] != "m1"]  # Pennsylvania closes
    step(tracker, data_clock)
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_CLOSED and surge.current_price == 0.60
    view = tracker.view()
    assert all(r["exchange_id"] != "e1" for r in view["exchanges"])
    card = next(r for r in view["surges"] if r["id"] == sid)
    assert card["status"] == "closed" and card["title"]
    assert tracker.store.surges(status=SURGE_OPEN) == []

    world.markets = everything  # listed again: the surge is evaluated like any open one
    step(tracker, data_clock)
    assert tracker.store.get_surge(sid).status == SURGE_OPEN


def test_live_api_7_market_list_failure_does_not_close_surges(make_tracker: Any, world: World, fake: Any,
                                                              data_clock: FakeClock) -> None:
    tracker = make_tracker(market_refresh=60)
    [sid] = jump_scenario(tracker, world, data_clock)["new_surges"]
    for _ in range(5):  # the first try and the client's 4 retries
        fake.routes[("GET", f"/tournaments/{SLUG}/markets")].appendleft((503, error_body("SERVICE_UNAVAILABLE", "down")))
    step(tracker, data_clock)
    assert tracker.store.get_surge(sid).status == SURGE_OPEN  # the last good list still applies
    p = problems(tracker)["markets"]
    assert p["severity"] == "error" and "503" in p["message"]
    step(tracker, data_clock)
    assert "markets" not in problems(tracker)


def test_live_api_11_unanalysed_surges_are_requeued_after_a_restart(make_tracker: Any, world: World,
                                                                     data_clock: FakeClock) -> None:
    store = TrackerStore()
    first = make_tracker(store=store, attributor=FakeAttributor())
    [sid] = jump_scenario(first, world, data_clock)["new_surges"]
    assert store.get_surge(sid).attribution is None
    assert first.view()["surges"][0]["analysis_pending"] is True
    assert first.status()["analysis"]["queued_ids"] == [sid]
    first.stop()
    world.set("e1", 0.40)  # the spike reverts while the dashboard is down

    second = make_tracker(store=store, attributor=FakeAttributor())
    summary = step(second, data_clock)
    assert sid in summary["queued"] and second.status()["queues"]["analysis"] == 1
    assert store.get_surge(sid).status == SURGE_REVERTED
    assert second.view()["surges"][0]["analysis_pending"] is True
    assert second.analyze_pending(5) == 1 and store.get_surge(sid).attribution is not None
    card = second.view()["surges"][0]
    assert card["analysis_pending"] is False and card["attribution"]["verdict"] == "participants"
    step(second, data_clock)
    assert second.status()["queues"]["analysis"] == 0  # analysed: not queued again

    # without an attributor nothing is pending, so the card can say "not analysed yet"
    third = make_tracker(store=TrackerStore())
    [sid3] = jump_scenario(third, world, data_clock, to=0.70)["new_surges"]
    assert third.view()["surges"][0]["id"] == sid3 and third.view()["surges"][0]["analysis_pending"] is False


def test_live_api_8_news_outage_is_a_problem(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    class OutageAttributor(FakeAttributor):
        status = "unavailable"

        def analyze(self, surge: Surge) -> Attribution:
            out = super().analyze(surge)
            out.news_status = self.status
            out.reasons.append("News search failed (gdelt: HTTP 503): missing headlines are not evidence that there was no news")
            return out

    attributor = OutageAttributor()
    tracker = make_tracker(attributor=attributor)
    jump_scenario(tracker, world, data_clock)
    tracker.analyze_pending(1)
    p = problems(tracker)["news"]
    assert p["message"] == "News search failed (gdelt: HTTP 503)" and p["severity"] == "warning"
    attributor.status = "ok"
    sid = tracker.store.surges()[0].id
    assert tracker.request_analysis(sid) and tracker.analyze_pending(1) == 1
    assert "news" not in problems(tracker)


def test_live_api_2_3_5_status_shapes(make_tracker: Any, fake: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    status = tracker.status()
    assert status["problems"] == []
    assert status["detection"] == {"enabled": True, "waiting_for_history": 0, "live_only": 0, "reason": None}
    fake.routes[("GET", f"/tournaments/{SLUG}/markets")].appendleft((400, error_body("VALIDATION_ERROR", "bad cursor")))
    tracker.run_once()
    status = tracker.status()
    assert [p["source"] for p in status["problems"]] == ["markets"]
    assert status["detection"]["enabled"] is False and "market list" in status["detection"]["reason"]
    step(tracker, data_clock)
    status = tracker.status()
    assert status["problems"] == []
    det = status["detection"]  # backfill is off in this tracker: live prices only
    assert det["enabled"] and det["live_only"] == 5 and "backfill is off" in det["reason"]
    json.dumps(tracker.view())


class TestRound1RealAnalytics:
    real_analytics = True

    def test_live_api_4_history_outage_does_not_switch_detection_off(self, make_tracker: Any, world: World, fake: Any,
                                                                     data_clock: FakeClock) -> None:
        for eid in world.quotes:
            fake.add("GET", f"/exchanges/{eid}/price-history", (503, error_body("SERVICE_UNAVAILABLE", "down")))
        tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
        tracker.run_once()
        det = tracker.status()["detection"]
        assert det["waiting_for_history"] == 5 and det["enabled"] is False and "Waiting for price history" in det["reason"]
        tracker.backfill_step(100)  # every read fails: price history is down
        for _ in range(4):
            step(tracker, data_clock, 5)
        world.set("e1", 0.56)
        summary = step(tracker, data_clock, 5)
        assert len(summary["new_surges"]) == 1
        s = tracker.store.get_surge(summary["new_surges"][0])
        assert (s.window, s.direction, s.start_ts, s.start_price, s.end_price) == ("5m", "up", T0, 0.40, 0.56)
        det = tracker.status()["detection"]
        assert det["enabled"] and det["waiting_for_history"] == 0 and det["live_only"] == 5
        assert "price history is unavailable" in det["reason"] and "503" in det["reason"]

    def test_live_api_4_waiting_for_history_is_capped(self, make_tracker: Any, world: World, data_clock: FakeClock) -> None:
        tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
        tracker.run_once()  # backfill planned, the worker has not read anything yet
        assert tracker.status()["detection"]["waiting_for_history"] == 5
        step(tracker, data_clock, tracker_mod.HISTORY_WAIT_MAX_S)
        det = tracker.status()["detection"]
        assert det["waiting_for_history"] == 0 and det["enabled"] is True

    def test_visual_9_sparkline_spreads_over_the_whole_day(self, make_tracker: Any, world: World,
                                                           data_clock: FakeClock) -> None:
        world.add_history(data_clock)
        tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
        tracker.run_once()
        tracker.backfill_step(100)
        for _ in range(240):  # 20 minutes of 5 s live ticks
            step(tracker, data_clock, 5)
        spark = row(tracker.view(), "e1")["sparkline"]
        now = data_clock.now
        assert 2 <= len(spark) <= 96 and spark[-1][0] == now and spark[0][0] >= now - 86400
        assert sum(1 for ts, _ in spark if ts > now - 3600) <= 6  # not most of the points in the last hour
        gaps = [b[0] - a[0] for a, b in zip(spark, spark[1:])]
        assert max(gaps) <= 2 * 86400 / 95

    def test_functional_3_change_1h_on_trade_bucket_candles(self, make_tracker: Any, world: World, fake: Any,
                                                            data_clock: FakeClock) -> None:
        """Candles only for buckets that had trades: the 1h change still has a reference."""

        def sparse(request: httpx.Request) -> httpx.Response:
            res = request.url.params.get("resolution", "1h")
            length = 3600.0 if res == "1h" else 300.0
            last_start = math.floor(data_clock.now / length) * length
            starts = [last_start - 95 * 60 - length] if res == "5m" else [last_start - 26 * 3600]
            candles = [{"time": iso_ts(t), "open": 0.40, "high": 0.40, "low": 0.40, "close": 0.40, "vwap": 0.40,
                        "volume": 5, "tradeCount": 1} for t in starts]
            return httpx.Response(200, json={"exchangeId": "e1", "marketId": "m1", "resolution": res, "candles": candles})

        fake.add("GET", "/exchanges/e1/price-history", sparse)
        tracker = make_tracker(backfill=True, backfill_reads_per_min=1000)
        tracker.run_once()
        tracker.backfill_step(2)  # e1's 1h then e2's 1h; the rest is not needed for e1's row
        tracker.backfill_step(100)
        world.set("e1", 0.55)
        step(tracker, data_clock, 60)
        e1 = row(tracker.view(), "e1")
        assert e1["change_1h"] == pytest.approx(0.15) and e1["change_5m"] == pytest.approx(0.15)


# --------------------------------------------------------------------------- round 2 regressions


def mk(eid: str, window: str, start_ts: float, end_ts: float, start: float, end: float, direction: str,
       peak: Optional[float] = None, z: Optional[float] = None) -> Surge:
    """A detection as analytics.detect_surges returns it (current price = its end)."""
    seconds = {"5m": 300.0, "1h": 3600.0, "6h": 21600.0, "24h": 86400.0}[window]
    return Surge(eid, "m1", window, seconds, start_ts, end_ts, start, end, round(end - start, 6), direction,
                 end if peak is None else peak, end_ts, zscore=z, current_price=end)


def test_r2_fixcheck_3_a_plateau_seen_through_the_24h_window_stays_one_surge(make_tracker: Any) -> None:
    """With the frame anchored at the spike, a 24h re-detection hours later is merged into the
    spike's surge (not stored as a second surge), and the frame does not move."""
    tracker = make_tracker()
    spike = mk("e1", "1h", T0, T0 + 3600, 0.40, 0.60, "up", z=6.0)
    stored, created = tracker._record_surge(spike)  # type: ignore[misc]
    assert created
    later = T0 + 3600 + 8 * 3600
    plateau = mk("e1", "24h", later - 86400, later, 0.40, 0.60, "up", z=3.2)
    again, created = tracker._record_surge(plateau)  # type: ignore[misc]
    assert not created and again.id == stored.id
    assert (again.window, again.start_ts, again.end_ts, again.change, again.zscore) == ("1h", T0, T0 + 3600, 0.2, 6.0)
    assert len(tracker.store.surges()) == 1


def test_r2_fixcheck_5_the_reversion_of_a_spike_is_not_a_new_surge(make_tracker: Any) -> None:
    tracker = make_tracker()
    spike = mk("e1", "1h", T0, T0 + 1800, 0.385, 0.595, "up", z=7.9)
    tracker._record_surge(spike)
    # the fall back (0.595 -> 0.47) stays inside the spike's 0.385 -> 0.595 range: its reversion
    back = mk("e1", "1h", T0 + 1800, T0 + 5400, 0.595, 0.47, "down", z=-3.1)
    assert tracker._record_surge(back) is None
    assert [s.direction for s in tracker.store.surges()] == ["up"]
    # a drop below where the spike started is a move of its own
    deeper = mk("e1", "1h", T0 + 1800, T0 + 5400, 0.595, 0.30, "down", z=-6.0)
    stored, created = tracker._record_surge(deeper)  # type: ignore[misc]
    assert created and stored.direction == "down"
    # so is an opposite move long after the spike (window + 6 h)
    tracker2 = make_tracker()
    tracker2._record_surge(spike)
    much_later = T0 + 1800 + 3600 + 6 * 3600 + 3600 + 600  # starts 10 min past the spike's end + window + 6 h
    late = mk("e1", "1h", much_later - 3600, much_later, 0.595, 0.47, "down", z=-3.5)
    assert tracker2._record_surge(late) is not None


def test_r2_fixcheck_14_reverted_surges_follow_the_live_price(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    from supermarket_bot.models import SURGE_CLOSED

    tracker = make_tracker()
    [sid] = jump_scenario(tracker, world, data_clock)["new_surges"]  # 0.40 -> 0.60
    world.set("e1", 0.48)
    step(tracker, data_clock)
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_REVERTED and surge.current_price == 0.48
    world.set("e1", 0.45)
    step(tracker, data_clock)
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_REVERTED  # final
    assert surge.current_price == 0.45 and surge.reverted_fraction == pytest.approx(0.75)  # "Now" is now
    card = next(r for r in tracker.view()["surges"] if r["id"] == sid)
    assert card["current_price"] == 0.45 and card["reverted_fraction"] == pytest.approx(0.75)
    world.set("e1", 0.58)
    step(tracker, data_clock)
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_REVERTED and surge.reverted_fraction == pytest.approx(0.1)
    # the market settles: the reverted surge reads "Market closed" with its last price
    world.markets = [m for m in world.markets if m["id"] != "m1"]
    step(tracker, data_clock, 300)
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_CLOSED and surge.current_price == 0.58


def test_r2_live_api_1_a_missing_quote_records_no_tick_and_closes_the_surge(make_tracker: Any, world: World,
                                                                         fake: Any, data_clock: FakeClock) -> None:
    from supermarket_bot.models import SURGE_CLOSED

    tracker = make_tracker()
    [sid] = jump_scenario(tracker, world, data_clock)["new_surges"]  # 0.40 -> 0.60; the market list says 0.40
    lists = len(list_calls(fake))
    world.quotes.pop("e1")  # m1 settles: the bulk read lists e1 in missingIds
    summary = step(tracker, data_clock, 5)
    assert summary["ticks"] == 4  # nothing stored for e1 (no stale 0.40 from the market list)
    assert [p.price for p in tracker.store.series("e1", T0)][-1] == 0.60
    surge = tracker.store.get_surge(sid)
    assert surge.status == SURGE_CLOSED and surge.current_price == 0.60 and surge.reverted_fraction == 0.0
    e1 = row(tracker.view(), "e1")
    assert (e1["last"], e1["mark"], e1["surge_id"], e1["high_band"]) == (0.60, 0.60, None, None)
    assert tracker._bot is not None and summary["markets_refreshed"] is False
    # the next cycle re-reads the market list early (the outcome may have settled)
    world.markets = [m for m in world.markets if m["id"] != "m1"]
    summary = step(tracker, data_clock, 40)
    assert summary["markets_refreshed"] and len(list_calls(fake)) == lists + 1
    assert all(r["exchange_id"] != "e1" for r in tracker.view()["exchanges"])
    assert tracker.store.get_surge(sid).status == SURGE_CLOSED
    # a persistently missing outcome does not re-read the list every cycle
    world.quotes.pop("e2")
    step(tracker, data_clock, 40)
    step(tracker, data_clock, 40)
    step(tracker, data_clock, 40)
    assert len(list_calls(fake)) == lists + 2


def test_r2_live_api_9_news_is_rechecked_until_it_works_again(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    class OutageAttributor(FakeAttributor):
        status = "unavailable"

        def analyze(self, surge: Surge) -> Attribution:
            out = super().analyze(surge)
            out.news_status = self.status
            if self.status == "unavailable":
                out.reasons.append("News search failed (gdelt: HTTP 503): missing headlines are not evidence")
            return out

    attributor = OutageAttributor()
    tracker = make_tracker(attributor=attributor)
    tracker.run_once()
    for _ in range(5):
        step(tracker, data_clock)
    world.set("e1", 0.60)
    world.set("e3a", 0.70)
    first = sorted(step(tracker, data_clock)["new_surges"])
    assert len(first) == 2 and tracker.analyze_pending(5) == 2
    p = problems(tracker)["news"]
    assert p["count"] == 2
    step(tracker, data_clock, 60)
    assert tracker.status()["analysis"]["queued_ids"] == []  # not due yet
    step(tracker, data_clock, 300)  # 5 min later: one surge is re-analysed to re-check news search
    [probe] = tracker.status()["analysis"]["queued_ids"]
    assert tracker.analyze_pending(5) == 1
    p = problems(tracker)["news"]
    assert p["count"] == 3 and p["last"] == data_clock.now  # the retry failed: its time is shown
    step(tracker, data_clock, 300)
    assert tracker.status()["analysis"]["queued_ids"] == []  # the next re-check waits 10 min
    attributor.status = "ok"
    step(tracker, data_clock, 300)
    assert tracker.status()["analysis"]["queued_ids"] == [probe]
    assert tracker.analyze_pending(1) == 1
    assert "news" not in problems(tracker)
    # the other surge analysed without headlines is re-analysed too
    assert tracker.status()["analysis"]["queued_ids"] == [i for i in first if i != probe]
    assert tracker.analyze_pending(5) == 1
    assert all(s.attribution.news_status == "ok" for s in tracker.store.surges())


def test_r2_live_api_9_stale_news_problem_without_candidates_clears(make_tracker: Any, data_clock: FakeClock) -> None:
    tracker = make_tracker(attributor=FakeAttributor())
    tracker.run_once()
    tracker._problem("news", "News search failed (gdelt: HTTP 503)")
    tracker._news_recheck_at = data_clock.now + 300
    step(tracker, data_clock, 301)
    assert "news" not in problems(tracker)


def test_r2_live_api_10_no_live_history_problem_after_the_backfill_gave_up(make_client: Any, make_tracker: Any,
                                                                        world: World, fake: Any,
                                                                        data_clock: FakeClock) -> None:
    world.add_history(data_clock, ["e1", "e3a", "e3b", "e3c"])
    fake.add("GET", "/exchanges/e2/price-history", (503, error_body("SERVICE_UNAVAILABLE", "busy")))
    tracker = make_tracker(client=make_client(max_retries=0), backfill=True, backfill_reads_per_min=1000)
    tracker.run_once()
    assert tracker.backfill_step(2) == 2  # e1 1h loads, e2 1h fails (queued again)
    p = problems(tracker)["price history"]
    assert p["count"] == 1 and "503" in p["message"]
    assert tracker.backfill_step(3) == 3  # e3a, e3b, e3c load: the e2 problem stays
    assert problems(tracker)["price history"]["count"] == 1
    assert tracker.backfill_step(1) == 1  # e2 fails again
    assert problems(tracker)["price history"]["count"] == 2  # every failed read is counted
    tracker.backfill_step(100)  # e2 1h gives up; the 5m pass does the same
    assert tracker.backfill_step(100) == 0
    status = tracker.status()
    assert status["backfill"]["pending"] == 0 and status["backfill"]["failed"] == 2
    assert "price history" not in problems(tracker)  # nothing is retrying any more
    step(tracker, data_clock)
    reason = tracker.status()["detection"]["reason"]
    assert "price history could not be loaded (HTTP 503" in reason and "1 of 5" in reason


def test_r2_robustness_1_storage_failures_are_a_problem(make_tracker: Any, world: World, data_clock: FakeClock,
                                                       monkeypatch: Any) -> None:
    import sqlite3

    tracker = make_tracker()
    tracker.run_once()
    original = tracker.store.add_ticks

    def full(ts: float, rows: Any) -> int:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(tracker.store, "add_ticks", full)
    world.set("e1", 0.45)
    summary = step(tracker, data_clock)
    p = problems(tracker)["storage"]
    assert p["severity"] == "error" and "database or disk is full" in p["message"]
    assert p["message"].startswith("Saving price snapshot failed: database or disk is full")
    # the cycle went on: live prices and the view are current, the snapshot time too
    assert summary["ticks"] == 0 and row(tracker.view(), "e1")["last"] == 0.45
    assert tracker.status()["last_snapshot_at"] == data_clock.now and tracker.status()["cycles"] == 2
    monkeypatch.setattr(tracker.store, "add_ticks", original)
    step(tracker, data_clock)
    assert "storage" not in problems(tracker)


def test_r2_robustness_1_a_crashing_cycle_is_a_problem(make_tracker: Any, monkeypatch: Any) -> None:
    tracker = make_tracker(interval=0.02)

    def broken(now: float, results: Any) -> None:
        raise RuntimeError("view exploded")

    monkeypatch.setattr(tracker, "_build_view", broken)
    tracker.start()
    assert wait_for(lambda: "tracker" in problems(tracker))
    tracker.stop(timeout=2)
    p = problems(tracker)["tracker"]
    assert p["severity"] == "error" and "view exploded" in p["message"] and "retried automatically" in p["message"]


def test_r2_robustness_8_problem_messages_never_carry_part_of_the_key(make_tracker: Any) -> None:
    from conftest import API_KEY

    tracker = make_tracker()
    for pad in (0, 260, 275, 280, 290):
        exc = ApiError(503, "SERVICE_UNAVAILABLE", "x" * pad + f" Request with credentials {API_KEY} could not be served")
        tracker._problem("leaderboard", exc)
        message = problems(tracker)["leaderboard"]["message"]
        assert len(message) <= tracker_mod.PROBLEM_MESSAGE_MAX
        assert not any(API_KEY[i:i + 8] in message for i in range(len(API_KEY) - 7)), (pad, message)
    tracker._problem("order books", f"1 of 2 read(s) failed: Bearer {API_KEY}")
    assert API_KEY[:8] not in problems(tracker)["order books"]["message"]


def test_r2_functional_1_idea_books_are_read_and_stored(make_tracker: Any, world: World, fake: Any,
                                                       data_clock: FakeClock) -> None:
    tracker = make_tracker()
    tracker.run_once()
    # the multi-outcome market's book (20 levels) keeps every outcome's book
    assert fake.calls_to("/markets/m3/orderbook")[0].params["depth"] == "20"
    assert tracker.store.latest("e3a").book["asks"] == [[0.51, 100.0]]
    step(tracker, data_clock, 300)  # the next refresh reads the carry candidate's own book (e2, a stable band)
    call = fake.calls_to("/exchanges/e2/orderbook")[0]
    assert call.params == {"depth": "20", "tournamentId": TOURNAMENT_ID}
    book = tracker.store.latest("e2").book
    assert book["at"] == data_clock.now and book["asks"] == [[0.98, 120.0], [0.99, 250.0]]
    assert book["bids"] == [[0.96, 150.0], [0.95, 300.0]]
    step(tracker, data_clock, 300)  # read again at each refresh while it is a candidate
    assert len(fake.calls_to("/exchanges/e2/orderbook")) == 2


def test_r2_functional_3_account_value_is_cash_plus_positions(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker()
    tracker.run_once()
    ctx = tracker.view()["context"]
    assert ctx["balance"] == 101250.5  # cash
    assert ctx["account_value"] == 103_750.5 and ctx["positions_value"] == 2_500.0
    # the P&L read answers 409 (a holding without a valuation price): unknown, not a banner problem
    other = make_tracker()
    replace_route(fake, f"/tournaments/{SLUG}/portfolio/pnl", (409, error_body("CONFLICT", "no valuation price")))
    other.run_once()
    assert other.view()["context"]["account_value"] is None
    assert not any(p["source"] in ("balance", "account value") for p in other.status()["problems"])


class TestRound2RealAnalytics:
    real_analytics = True

    def test_r2_live_api_4_old_prices_after_a_restart_are_not_a_high_band(self, make_client: Any, make_tracker: Any,
                                                                          world: World, fake: Any,
                                                                          data_clock: FakeClock) -> None:
        store = TrackerStore()
        for k in range(60):  # an earlier run: e2 at 0.975 for an hour, ending 8 h ago
            store.add_ticks(T0 - 9 * 3600 + 60 * k, [{"exchange_id": "e2", "latest_price": 0.975, "best_bid": 0.965,
                                                      "best_ask": 0.985}])
        replace_route(fake, "/exchanges/prices", (503, error_body("SERVICE_UNAVAILABLE", "down")))
        tracker = make_tracker(store=store, client=make_client(max_retries=0))
        tracker.run_once()
        view = tracker.view()
        e2 = row(view, "e2")
        assert e2["last"] == 0.975 and e2["stale"] is True and e2["updated_at"] == T0 - 9 * 3600 + 59 * 60
        assert view["high_band"] == [] and e2["high_band"] is None
        # prices work again: a few live ticks are not "100% of the last 6 h"
        replace_route(fake, "/exchanges/prices", world._prices)
        world.set("e2", 0.975)
        for _ in range(5):
            step(tracker, data_clock, 5)
        view = tracker.view()
        assert view["high_band"] == [] and row(view, "e2")["stale"] is False


# --------------------------------------------------------------------------- paper trading and fair values (§7.2)


class StubRunner:
    """Stands in for paper.PaperRunner (package C): records what the tracker hands it and how it is called."""

    def __init__(self, engine: Any, reader: Any, inputs_fn: Callable[[float], Any], *, limiter: Any = None,
                 clock: Any = None, interval: Any = None, extras_fn: Any = None, **kwargs: Any) -> None:
        self.engine, self.reader, self.inputs_fn, self.limiter = engine, reader, inputs_fn, limiter
        self.clock, self.interval, self.extras_fn = clock, interval, extras_fn
        self.steps: List[Optional[float]] = []
        self.calls: List[Any] = []
        self.inputs: List[Any] = []
        self.errors: List[str] = []
        self.lock_held: List[bool] = []
        self.tracker: Any = None
        self._summary: Dict[str, Any] = {"run": {"run_id": "run-1", "steps": 0, "last_step_at": None,
                                                 "last_step_seconds": None, "interval": None}, "portfolios": []}
        self._previous: Optional[Dict[str, Any]] = None

    def _held(self) -> None:
        if self.tracker is not None:
            self.lock_held.append(self.tracker._lock._is_owned())

    def step(self, now: Optional[float] = None) -> Any:
        self._held()
        when = self.clock() if now is None else now
        self.steps.append(now)
        self.inputs.append(self.inputs_fn(when))
        extras = dict(self.extras_fn() or {}) if self.extras_fn is not None else {}
        self._summary = {"run": {"run_id": "run-1", "steps": len(self.steps), "last_step_at": when,
                                 "last_step_seconds": 0.01, "interval": None}, "portfolios": [], **extras}
        return SimpleNamespace(errors=list(self.errors))

    def summary(self) -> Dict[str, Any]:
        return self._summary

    def previous(self) -> Optional[Dict[str, Any]]:
        return self._previous

    def reset(self, now: Any = None, start_capital: Any = None, target_hours: Any = None, capital_source: Any = None) -> str:
        self._held()
        self.calls.append(("reset", now, start_capital, target_hours, capital_source))
        self._previous = {"run": {"run_id": "run-1", "end_reason": "reset"}, "portfolios": []}
        return "run-2"

    def end_run(self, now: Any = None, reason: str = "completed") -> Dict[str, Any]:
        self._held()
        self.calls.append(("end", now, reason))
        return {"at": now, "reason": reason}


class StubFairValues:
    """Stands in for fairvalue.FairValueService (package A)."""

    def __init__(self, tracker_box: Optional[List[Any]] = None, gate: Optional[threading.Event] = None) -> None:
        self.box = tracker_box if tracker_box is not None else []
        self.gate = gate
        self.targets: List[List[Any]] = []
        self.refreshes: List[float] = []
        self.started = threading.Event()
        self.provider: Dict[str, Any] = {"status": "ok", "last_ok_at": None, "last_error": None, "requests": 1,
                                         "matched": 2, "quoted": 2, "next_try_at": None}
        self.lock_held: List[bool] = []

    def _held(self) -> None:
        if self.box:
            self.lock_held.append(self.box[0]._lock._is_owned())

    def set_targets(self, infos: Any) -> None:
        self._held()
        self.targets.append(list(infos))

    def refresh(self, now: float) -> Any:
        self._held()
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        self.refreshes.append(now)

    def status(self) -> Dict[str, Any]:
        return {"mode": "auto", "enabled": True, "last_refresh_at": self.refreshes[-1] if self.refreshes else None,
                "usable": 2, "total": 5, "providers": {"polymarket": dict(self.provider)}, "manual": None, "map": None}

    def current(self) -> Any:
        return SimpleNamespace(enabled=True, values={})

    def races(self) -> Dict[str, Any]:
        return {}


@pytest.fixture
def stub_runner(monkeypatch: Any) -> List[StubRunner]:
    import supermarket_bot.paper as paper_mod

    made: List[StubRunner] = []

    def factory(*args: Any, **kwargs: Any) -> StubRunner:
        runner = StubRunner(*args, **kwargs)
        made.append(runner)
        return runner

    monkeypatch.setattr(paper_mod, "PaperRunner", factory)
    return made


ENGINE = SimpleNamespace(config=SimpleNamespace(reads_per_min=20))


def test_paper_and_fair_value_workers_start_only_when_configured(make_tracker: Any, stub_runner: List[StubRunner]) -> None:
    plain = make_tracker(interval=0.05)
    plain.start()
    names = {t.name for t in plain._threads}
    assert "tracker-paper" not in names and "tracker-fairvalue" not in names
    status = plain.status()
    assert status["paper"] is None and status["fair_value"] is None and status["read_budget"]["paper"] is None
    assert plain.paper_step() is None and plain.fair_value_step() is False
    assert plain.paper_view() is None and plain.paper_reset() is None and plain.paper_end() is None
    plain.stop(timeout=2)

    fv = StubFairValues()
    tracker = make_tracker(interval=0.05, paper=ENGINE, fair_values=fv)
    [runner] = stub_runner
    assert runner.engine is ENGINE and isinstance(runner.reader, tracker_mod.TrackerMarketReader)
    assert runner.limiter is tracker.paper_limiter and runner.interval == 0.05 and runner.clock is tracker._clock
    tracker.start()
    assert {"tracker-paper", "tracker-fairvalue"} <= {t.name for t in tracker._threads}
    assert wait_for(lambda: len(runner.steps) >= 2 and fv.refreshes, timeout=5)
    cycles = tracker.status()["cycles"]
    assert len(runner.steps) <= cycles + 1  # at most one paper step per tracker cycle
    tracker.stop(timeout=2)
    assert not any(t.is_alive() for t in tracker._threads)


def test_paper_step_and_fair_value_step_run_synchronously(make_tracker: Any, stub_runner: List[StubRunner],
                                                          data_clock: FakeClock) -> None:
    box: List[Any] = []
    fv = StubFairValues(box)
    tracker = make_tracker(paper=ENGINE, fair_values=fv)
    box.append(tracker)
    runner = stub_runner[0]
    runner.tracker = tracker
    tracker.run_once()
    assert [i.exchange_id for i in fv.targets[-1]] == ["e1", "e2", "e3a", "e3b", "e3c"]  # open outcomes, after a list read
    assert tracker.fair_value_step(T0 + 5) is True and fv.refreshes == [T0 + 5]
    assert tracker.fair_value_step() is True and fv.refreshes[-1] == T0  # defaults to the tracker clock
    report = tracker.paper_step(T0 + 7)
    assert report.errors == [] and runner.steps == [T0 + 7]
    inputs, obs = runner.inputs[-1]
    assert set(inputs.infos) == {"e1", "e2", "e3a", "e3b", "e3c"} and set(obs.open_ids) == set(inputs.infos)
    assert inputs.latest["e1"].bid == 0.39 and obs.quotes["e1"].ask == 0.41
    assert inputs.settlement_regime == "unknown"
    view = tracker.paper_view()
    assert view["run"]["interval"] == tracker.interval and view["run"]["steps"] == 1
    assert view["fair_value"] == {"mode": "auto", "enabled": True, "usable": 2, "total": 5}  # the tracker's extras
    assert runner.summary()["run"]["interval"] is None  # paper_view copies; the published summary is untouched
    assert tracker.status()["paper"] == {"enabled": True, "run_id": "run-1", "steps": 1, "last_step_at": T0 + 7,
                                         "last_step_seconds": 0.01}
    assert tracker.status()["fair_value"]["providers"]["polymarket"]["status"] == "ok"
    assert tracker.paper_view("previous") is None
    assert tracker.paper_reset(start_capital=5000.0, target_hours=12) == "run-2"
    assert runner.calls[-1] == ("reset", T0, 5000.0, 12, "set by you")
    assert tracker.paper_view("previous")["run"]["end_reason"] == "reset"
    tracker.paper_reset()
    assert runner.calls[-1] == ("reset", T0, None, None, "")
    assert tracker.paper_end("completed") == {"at": T0, "reason": "completed"}
    # lock rule (D43): neither the runner nor the fair-value service is ever called under the tracker's lock
    assert runner.lock_held and not any(runner.lock_held)
    assert fv.lock_held and not any(fv.lock_held)


def test_recent_and_cup_mids_come_from_the_published_snapshot(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    tracker = make_tracker()
    assert tracker.recent_mids() == {} and tracker.cup_mids() == {}
    tracker.run_once()
    world.set("e1", 0.44)
    step(tracker, data_clock, 30)
    assert tracker.cup_mids()["e1"] == pytest.approx(0.44) and tracker.cup_mids()["e2"] == pytest.approx(0.97)
    mids = tracker.recent_mids(300)
    assert mids["e1"] == [(T0, pytest.approx(0.40)), (T0 + 30, pytest.approx(0.44))]
    assert tracker.recent_mids(10)["e1"] == [(T0 + 30, pytest.approx(0.44))]


def test_paper_budget_in_the_status_is_6_per_min_while_the_backfill_runs(make_tracker: Any, world: World,
                                                                         stub_runner: List[StubRunner],
                                                                         data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    tracker = make_tracker(backfill=True, paper=ENGINE, paper_reads_per_min=20, paper_reads_per_min_during_backfill=6)
    tracker.run_once()
    budget = tracker.status()["read_budget"]
    assert tracker.status()["backfill"]["pending"] > 0
    assert budget["paper"] == {"used": 0, "limit": 6, "room": 6}
    assert budget["reserve"] == 12 and isinstance(budget["projected_per_min"], float) and budget["projected_per_min"] > 0
    assert budget["warning"] is None  # the test client is not capped like a real account
    tracker.paper_limiter.acquire()
    assert tracker.status()["read_budget"]["paper"] == {"used": 1, "limit": 6, "room": 5}
    while tracker.backfill_step(10):
        pass
    assert tracker.status()["backfill"]["pending"] == 0
    assert tracker.status()["read_budget"]["paper"] == {"used": 1, "limit": 20, "room": 19}


def test_projected_reads_warn_close_to_the_account_limit(make_client: Any, make_tracker: Any, world: World,
                                                         stub_runner: List[StubRunner], data_clock: FakeClock) -> None:
    world.add_history(data_clock)
    tracker = make_tracker(client=make_client(reads_per_min=90), backfill=True, backfill_reads_per_min=60,
                           attributor=FakeAttributor(), paper=ENGINE)
    tracker.run_once()
    budget = tracker.status()["read_budget"]
    assert budget["projected_per_min"] > 75
    assert budget["warning"] == (f"Projected reads ({budget['projected_per_min']:.0f}/min) are close to the account limit of 100 "
                                 "per minute shared by all your keys: do not run other scripts on this account.")
    problem = next(p for p in tracker.status()["problems"] if p["source"] == "read budget")
    assert problem["severity"] == "warning"
    while tracker.backfill_step(20):
        pass
    step(tracker, data_clock, 300)  # the market list is re-read: the projection follows the finished backfill
    assert tracker.status()["read_budget"]["warning"] is None
    assert not any(p["source"] == "read budget" for p in tracker.status()["problems"])


def test_new_problem_sources_are_warnings() -> None:
    for source in ("paper", "fair value", "settlements", "read budget"):
        assert tracker_mod.PROBLEM_SEVERITY[source] == "warning"


def _settled_route(world: World, settled: List[Dict[str, Any]], calls: List[Dict[str, str]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("status") == "settled":
            calls.append(params)
            return httpx.Response(200, json=market_page(settled))
        return httpx.Response(200, json=market_page(world.markets))

    return handler


def test_settlements_are_read_and_stored_with_their_detection_time(make_tracker: Any, world: World, fake: Any,
                                                                  data_clock: FakeClock) -> None:
    done = market("m9", "Will Democrats win the Iowa Senate race?", [("e9", None, 1.0)], status="settled")
    done.update(settledWith="YES", settledOn="2026-09-30T00:00:00.000Z")
    refund = market("m8", "Will the debate happen?", [("e8", None, None)], status="settled")
    refund["settledWith"] = "REFUND"
    calls: List[Dict[str, str]] = []
    replace_route(fake, f"/tournaments/{SLUG}/markets", _settled_route(world, [done, refund], calls))
    tracker = make_tracker()
    tracker.run_once()
    assert len(calls) == 1 and calls[0]["limit"] == "100"
    stored = tracker.store.settlements()
    assert stored["e9"].payout_yes == 1.0 and stored["e9"].detected_at == T0 and stored["e9"].market_id == "m9"
    assert stored["e9"].settled_on == pytest.approx(iso_epoch("2026-09-30T00:00:00.000Z"))
    assert stored["e8"].refund is True and stored["e8"].payout_yes is None
    assert set(tracker.settlements()) == {"e8", "e9"}
    step(tracker, data_clock, 120)
    assert len(calls) == 1  # every settlement_refresh (600 s) ...
    world.quotes.pop("e1")  # ... and early when an outcome goes missing from the bulk prices
    step(tracker, data_clock, 30)
    step(tracker, data_clock, 30)
    assert len(calls) == 2
    assert tracker.store.settlements()["e9"].detected_at == T0  # the first detection is kept
    replace_route(fake, f"/tournaments/{SLUG}/markets", lambda r: (
        httpx.Response(500, json=error_body("INTERNAL", "boom")) if r.url.params.get("status") == "settled"
        else httpx.Response(200, json=market_page(world.markets))))
    step(tracker, data_clock, 600)
    problem = next(p for p in tracker.status()["problems"] if p["source"] == "settlements")
    assert problem["severity"] == "warning"


def iso_epoch(text: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def test_leaderboard_reads_100_rows_and_keeps_entries(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker()
    tracker.run_once()
    assert fake.calls_to(f"/tournaments/{SLUG}/leaderboard")[0].params["limit"] == "100"
    board = tracker.view()["context"]["leaderboard"]
    assert [e["username"] for e in board["top"]] == ["alice", "bob", None]  # the existing keys stay
    assert {"rank", "username", "pnl", "roi", "trades", "value"} <= set(board["top"][0])
    assert board["entries"] == [
        {"rank": 1, "username": "alice", "pnl": 5000.0, "value": 105000.0},
        {"rank": 2, "username": "bob", "pnl": 3000.0, "value": 103000.0},
        {"rank": 3, "username": None, "pnl": 2500.0, "value": 102500.0},
    ]
    [snap] = tracker.store.leaderboard_snapshots(T0 - 1, T0 + 1)
    assert snap.at == T0 and snap.my_rank == 17 and snap.initial_balance == 100000 and len(snap.entries) == 3


def test_paper_and_fair_value_problems(make_tracker: Any, stub_runner: List[StubRunner]) -> None:
    fv = StubFairValues()
    tracker = make_tracker(paper=ENGINE, fair_values=fv)
    runner = stub_runner[0]
    tracker.run_once()
    offline = "Polymarket is unreachable from this machine (connection refused by the network proxy): no outside fair value from Polymarket."
    fv.provider.update(status="offline", last_error=offline)
    tracker.fair_value_step()
    problems = [p for p in tracker.status()["problems"] if p["source"] == "fair value"]
    assert len(problems) == 1 and problems[0]["message"] == offline and problems[0]["severity"] == "warning"
    fv.provider.update(status="ok", last_error=None)
    tracker.fair_value_step()
    assert not any(p["source"] == "fair value" for p in tracker.status()["problems"])
    runner.errors = ["no order book"]
    tracker.paper_step()
    problem = next(p for p in tracker.status()["problems"] if p["source"] == "paper")
    assert problem["severity"] == "warning" and "no order book" in problem["message"]
    runner.errors = []
    tracker.paper_step()
    assert not any(p["source"] == "paper" for p in tracker.status()["problems"])


def test_a_slow_fair_value_provider_never_blocks_the_snapshot_loop(make_tracker: Any) -> None:
    gate = threading.Event()
    fv = StubFairValues(gate=gate)
    tracker = make_tracker(interval=0.05, fair_values=fv)
    try:
        tracker.start()
        assert fv.started.wait(5)  # the refresh is stuck in its (outside) HTTP call
        cycles = tracker.status()["cycles"]
        assert wait_for(lambda: tracker.status()["cycles"] >= cycles + 5, timeout=5)
        started = time.monotonic()
        tracker.status()
        tracker.view()
        assert time.monotonic() - started < 1.0
        assert fv.refreshes == []
    finally:
        gate.set()
        tracker.stop(timeout=2)
    assert fv.refreshes


def test_a_saturated_background_workload_does_not_delay_the_snapshot_loop(make_client: Any, make_tracker: Any,
                                                                          stub_runner: List[StubRunner], world: World,
                                                                          sleeper: Any, clock: FakeClock,
                                                                          data_clock: FakeClock) -> None:
    client = make_client(reads_per_min=90)
    waits: List[float] = []

    def limiter_sleep(seconds: float) -> None:  # a background read waiting for room above the reserve
        waits.append(seconds)
        clock.now += 61  # ...until the client's window has moved on

    world.add_history(data_clock)
    tracker = make_tracker(client=client, backfill=True, paper=ENGINE, attributor=FakeAttributor(),
                           limiter_sleep=limiter_sleep)
    tracker.run_once()  # first cycle: market list, prices and the (gated) context, on an idle budget
    clock.now += 61  # a minute later the client's window is empty again
    while client.read_limiter.used < 80:  # background reads (backfill, analysis, paper) have used 80 of the 90 slots
        client.read_limiter.acquire()
    assert tracker.read_reserve.room() == 90 - 12 - 80
    assert tracker.paper_limiter.room() <= 0  # the paper runner skips its reads
    for _ in range(2):  # snapshot cycles (the context is not due): the loop's own reads are never gated
        summary = step(tracker, data_clock, 30)
        assert summary["ticks"] == 5 and summary["errors"] == 0
    assert sleeper.calls == [] and waits == []  # it never waited: no delay at all, let alone one interval
    assert client.read_limiter.used <= 90
    tracker.backfill_step(1)  # a background read waits for room above the reserve instead
    assert waits and waits[0] == tracker.read_reserve.poll_s


def _trade(i: int, ts: float) -> Dict[str, Any]:
    return {"id": f"t{i}", "createdAt": iso_ts(ts), "price": 0.41, "size": 10, "side": "YES", "volume": 4.1}


def test_the_tape_reader_pages_and_flags_truncation(make_tracker: Any, fake: Any, data_clock: FakeClock) -> None:
    pages = {None: ([_trade(i, T0 - 1000 + i) for i in range(600, 400, -1)], "c1"),
             "c1": ([_trade(i, T0 - 1000 + i) for i in range(400, 200, -1)], "c2"),
             "c2": ([_trade(i, T0 - 1000 + i) for i in range(200, 150, -1)], None)}
    seen: List[Dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        data, nxt = pages[params.get("cursor")]
        return httpx.Response(200, json={"data": data, "pagination": {"limit": 200, "hasMore": nxt is not None, "nextCursor": nxt}})

    fake.add("GET", "/exchanges/e1/trades", handler)
    tracker = make_tracker()
    reader = tracker_mod.TrackerMarketReader(tracker)
    tape = reader.trades("e1", T0 - 1000, max_pages=2)
    assert tape.pages == 2 and tape.truncated is True and len(tape.trades) == 400
    assert [t.ts for t in tape.trades] == sorted(t.ts for t in tape.trades)  # oldest first
    assert tape.trades[0].trade_id == "t201" and tape.trades[-1].trade_id == "t600"
    assert seen[0] == {"tournamentId": TOURNAMENT_ID, "from": iso_ts(T0 - 1000), "limit": "200"}
    assert seen[1]["cursor"] == "c1"
    stored = tracker.store.trades("e1", T0 - 2000)
    assert len(stored) == 400 and all(t.fetched_at == T0 for t in stored)
    data_clock.now += 5
    full = reader.trades("e1", T0 - 1000, max_pages=3)
    assert full.pages == 3 and full.truncated is False and len(full.trades) == 450
    assert tracker.paper_limiter.used == 5  # one budget slot per page
    fake.routes.pop(("GET", "/exchanges/e1/trades"))
    fake.add("GET", "/exchanges/e1/trades", (500, error_body("INTERNAL", "boom")))
    assert reader.trades("e1", T0 - 1000) is None
    assert any(p["source"] == "paper" for p in tracker.status()["problems"])


def test_the_book_reader_stores_what_it_read(make_tracker: Any, fake: Any) -> None:
    tracker = make_tracker()
    tracker.run_once()
    reader = tracker_mod.TrackerMarketReader(tracker)
    obs = reader.book("e1", 20)
    assert obs.source == "paper" and obs.observed_at == T0 and obs.sequence == 100
    assert obs.bids == [(0.39, 150.0), (0.38, 300.0)] and obs.asks == [(0.41, 120.0), (0.42, 250.0)]
    assert fake.calls_to("/exchanges/e1/orderbook")[-1].params == {"depth": "20", "tournamentId": TOURNAMENT_ID}
    assert tracker.store.book("e1")["at"] == T0  # ideas size by it
    [snap] = tracker.store.book_snapshots("e1", T0 - 1)
    assert snap.bids == obs.bids and snap.source == "paper"
    assert tracker.paper_limiter.used == 1
    fake.routes.pop(("GET", "/exchanges/e1/orderbook"))
    fake.add("GET", "/exchanges/e1/orderbook", (500, error_body("INTERNAL", "boom")))
    assert reader.book("e1", 20) is None
    assert any(p["source"] == "paper" for p in tracker.status()["problems"])


def test_the_lease_refuses_a_second_tracker_on_the_same_store(make_tracker: Any, tmp_path: Any, data_clock: FakeClock) -> None:
    path = tmp_path / "tracker.sqlite3"
    first = make_tracker(store=TrackerStore(path), lease=True)
    second = make_tracker(store=TrackerStore(path), lease=True)
    first.run_once()
    with pytest.raises(tracker_mod.TrackerBusy) as caught:
        second.run_once()
    message = str(caught.value)
    assert message.startswith(f"Another process (pid {os_pid()} on ")
    # this very process: its liveness cannot be told apart, so the heartbeat rule and its wait are explained
    assert f"is already running the tracker on {path}: stop it first. If it has already ended, the lock frees " \
           f"itself within {3 * second.interval:.0f} s of its last heartbeat" in message
    assert "separate copy" not in message  # live-5: a second copy would start a new run and re-download history
    assert message.endswith("Running two would double the API reads; the account allows 100 per minute across all keys.")
    with pytest.raises(tracker_mod.TrackerBusy):
        second.start()
    with pytest.raises(tracker_mod.TrackerBusy):
        second.paper_step() if second.paper_runner is not None else second._ensure_lease()
    first.stop(timeout=2)  # releases the lease
    second.run_once()
    data_clock.now += 3 * second.interval + 1  # a lease whose heartbeat is older than 3 x interval is taken over
    first.run_once()


def os_pid() -> int:
    import os

    return os.getpid()


# --------------------------------------------------------------------------- integration regressions (round 3)


def test_live_1_the_lease_heartbeat_survives_a_cycle_stuck_in_retries(make_tracker: Any, fake: Any, world: World,
                                                                      tmp_path: Any) -> None:
    """live-1: the snapshot loop renews the lease only between cycles; a cycle that sits in the client's retries
    (503 Retry-After) for longer than the 3 x interval TTL must not let a second tracker on the same store take
    the lease, and the first one must not go fatal afterwards."""
    path = tmp_path / "tracker.sqlite3"
    gate, blocked = threading.Event(), threading.Event()
    first = make_tracker(store=TrackerStore(path), lease=True, clock=time.time, interval=0.2)
    first.run_once()  # the market list is known: the next cycles go straight to the (blocked) price read

    def slow_prices(request: httpx.Request) -> httpx.Response:
        blocked.set()
        gate.wait(15)
        return world._prices(request)

    fake.routes.pop(("GET", "/exchanges/prices"))
    fake.add("GET", "/exchanges/prices", slow_prices)
    try:
        first.start()
        assert blocked.wait(5)
        time.sleep(4 * first.interval)  # longer than the TTL (3 x interval) with the cycle still stuck
        second = make_tracker(store=TrackerStore(path), lease=True, clock=time.time, interval=0.2)
        with pytest.raises(tracker_mod.TrackerBusy):
            second.start()
    finally:
        gate.set()
    assert wait_for(lambda: first.status()["cycles"] >= 3, timeout=5)
    assert first.status()["fatal_error"] is None and first.running
    first.stop(timeout=2)
    assert TrackerStore(path).lease("tracker") is None  # released on stop


def test_accounting_3_a_threaded_run_starts_on_the_account_value_not_the_default(make_tracker: Any, fake: Any) -> None:
    """accounting-3 (§6.1): in threaded mode the first paper step runs right after the first cycle, while the
    context worker is still reading the balance; it must wait for that read, so the run starts on the account
    value (103,750.50 here) rather than the "default 100,000"."""
    from supermarket_bot.models import PaperConfig
    from supermarket_bot.paper import MemoryPaperPersistence, PaperEngine

    def slow_tournament(request: httpx.Request) -> httpx.Response:
        time.sleep(0.6)  # the first context refresh is still in flight when the first cycle ends
        return httpx.Response(200, json=TOURNAMENT_INFO)

    fake.routes.pop(("GET", f"/tournaments/{SLUG}"))
    fake.add("GET", f"/tournaments/{SLUG}", slow_tournament)
    # without the default-capital re-base, so this tests that the tracker's first step already knows the account
    engine = PaperEngine(PaperConfig(), persistence=MemoryPaperPersistence(), clock=time.time,
                         rebase_default_capital=False)
    tracker = make_tracker(interval=0.05, paper=engine, clock=time.time)
    tracker.start()
    assert wait_for(lambda: ((tracker.paper_view() or {}).get("run") or {}).get("steps", 0) >= 1, timeout=10)
    run = tracker.paper_view()["run"]
    assert run["capital_source"] == "account value" and run["start_capital"] == pytest.approx(103_750.5)
    tracker.stop(timeout=2)


def test_lookahead_3_an_attribution_is_stamped_when_the_analysis_returned(make_tracker: Any, world: World,
                                                                         data_clock: FakeClock) -> None:
    """lookahead-3: the attributor stamps analyzed_at with the clock when its analysis STARTED (then reads the
    market, the tape, the book, the news and the LLM judge: 59 s here). The verdict exists for the paper step only
    once analyze() returned, so that is the stored analyzed_at, and a replay (analyzed_at <= t) cannot use it
    earlier."""
    from supermarket_bot.backtest import GuardedHistory

    class SlowAttributor(FakeAttributor):
        def analyze(self, surge: Surge) -> Attribution:
            started = data_clock.now
            result = super().analyze(surge)
            result.analyzed_at = started  # what Attributor.analyze does
            data_clock.now += 59.0  # reads, news search, LLM judge
            return result

    tracker = make_tracker(attributor=SlowAttributor())
    sid = jump_scenario(tracker, world, data_clock)["new_surges"][0]
    t_start = data_clock.now
    assert tracker.analyze_pending(1) == 1
    stored = tracker.store.get_surge(sid).attribution
    assert stored.analyzed_at == t_start + 59.0
    guard = GuardedHistory(tracker.store)
    for t, visible in ((t_start, False), (t_start + 30.0, False), (t_start + 59.0, True)):
        guard.set_time(t)
        [s] = [x for x in guard.surges(since=t_start - 7200, until=t) if x.id == sid]
        assert (s.attribution is not None) is visible, t


@pytest.mark.skipif(__import__("os").name != "posix", reason="pid liveness is checked on POSIX only")
def test_live_5_a_lease_left_by_a_dead_process_on_this_machine_is_continued(make_tracker: Any, tmp_path: Any,
                                                                            caplog: Any) -> None:
    """live-5: after a crash or a closed terminal the lease names this host and a pid that no longer exists; the
    restart takes it over at once (and says the previous run ended without shutting down) instead of refusing."""
    import logging
    import socket
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    path = tmp_path / "tracker.sqlite3"
    store = TrackerStore(path)
    host = socket.gethostname()
    assert store.acquire_lease("tracker", {"owner_id": f"{host}:{proc.pid}:{time.time() - 60:.6f}:1", "pid": proc.pid,
                                           "host": host}, T0, ttl_s=90.0) is None
    tracker = make_tracker(store=store, lease=True)
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        tracker.run_once()  # the heartbeat is fresh (same data time), but its process is gone
    assert store.lease("tracker")["owner_id"] == tracker._owner["owner_id"]
    assert f"the previous run (pid {proc.pid} on {host}) ended without shutting down; continuing it" in caplog.text


def test_live_5_tracker_busy_says_to_stop_a_running_process_and_never_suggests_a_separate_copy() -> None:
    running = tracker_mod.TrackerBusy({"pid": 42, "host": "vm", "alive": True}, "data/cup/tracker.sqlite3")
    assert str(running).startswith("Another process (pid 42 on vm) is already running the tracker on "
                                   "data/cup/tracker.sqlite3: stop it first (close that dashboard, or press Ctrl-C in "
                                   "its terminal), then start again.")
    remote = tracker_mod.TrackerBusy({"pid": 42, "host": "laptop", "heartbeat_at": 1000.0}, "db", ttl_s=90.0, now=1030.0)
    assert ("If it has already ended, the lock frees itself within 90 s of its last heartbeat (about 60 s from now): "
            "start again then.") in str(remote)
    for exc in (running, remote):
        assert "separate copy" not in str(exc) and "--data-dir" not in str(exc)


# --------------------------------------------------------------------------- outside-move alerts (OUTSIDE_MOVES.md §13)


class StubWatcher:
    """Stands in for moves.OutsideMoveWatcher (package core): records how the tracker binds and steps it, and checks
    the lock rule (the tracker's lock is never held while the watcher runs)."""

    def __init__(self, poll_s: float = 15.0, gate: Optional[threading.Event] = None, use_feed: bool = False) -> None:
        self.poll_s = poll_s
        self.cup: Any = None
        self.tracker: Any = None
        self.gate = gate
        self.use_feed = use_feed
        self.steps: List[Optional[float]] = []
        self.lock_held: List[bool] = []
        self.started = threading.Event()
        self.last_error: Optional[str] = None
        self.books: List[Any] = []
        self.book_ids: List[str] = []
        self._status: Dict[str, Any] = {"enabled": True, "poll_s": poll_s, "last_step_at": None, "steps": 0, "open": 0,
                                        "actionable": 0, "actionable_alerts": [], "venues": {"ok": 0, "total": 2},
                                        "last_error": None}
        self._summary: Dict[str, Any] = {"alerts": [], "steps": 0}

    def bind(self, cup: Any) -> None:
        self.cup = cup

    def add_listener(self, fn: Any) -> None:
        pass

    def step(self, now: Optional[float] = None) -> Any:
        if self.tracker is not None:
            self.lock_held.append(self.tracker._lock._is_owned())
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        self.steps.append(now)
        if self.use_feed:
            self.quotes = self.cup.quotes()
            self.history = self.cup.history(7200)
            for eid in self.book_ids:
                self.books.append(self.cup.book(eid))
        self._status = dict(self._status, steps=len(self.steps), last_step_at=now, last_error=self.last_error)
        self._summary = {"alerts": [], "steps": len(self.steps)}
        return SimpleNamespace(at=now, errors=[])

    def status(self) -> Dict[str, Any]:
        return self._status

    def summary(self) -> Dict[str, Any]:
        return self._summary


def test_moves_no_watcher_no_thread_and_none_everywhere(make_tracker: Any) -> None:
    plain = make_tracker(interval=0.05)
    plain.start()
    assert "tracker-moves" not in {t.name for t in plain._threads}
    status = plain.status()
    assert status["moves"] is None and status["read_budget"]["moves"] is None
    assert plain.moves_step() is None and plain.moves_view() is None and plain.moves_budget is None
    plain.stop(timeout=2)


def test_moves_watcher_is_bound_and_stepped_by_its_own_worker(make_tracker: Any) -> None:
    watcher = StubWatcher(poll_s=0.05)
    tracker = make_tracker(interval=0.05, moves=watcher)
    watcher.tracker = tracker
    assert isinstance(watcher.cup, tracker_mod.TrackerCupFeed) and watcher.cup.tracker is tracker
    tracker.start()
    try:
        assert "tracker-moves" in {t.name for t in tracker._threads}
        assert wait_for(lambda: len(watcher.steps) >= 3, timeout=5)
        assert tracker.status()["cycles"] >= 1  # the worker starts after the first cycle (the Cup series is loaded)
    finally:
        tracker.stop(timeout=2)
    assert not any(t.is_alive() for t in tracker._threads)
    assert watcher.lock_held and not any(watcher.lock_held)  # lock rule (§12.3)


def test_moves_step_view_and_status(make_tracker: Any, data_clock: FakeClock) -> None:
    watcher = StubWatcher()
    tracker = make_tracker(moves=watcher)
    watcher.tracker = tracker
    tracker.run_once()
    report = tracker.moves_step(T0 + 15)
    assert report.at == T0 + 15 and watcher.steps == [T0 + 15]
    assert tracker.moves_step() is not None and watcher.steps[-1] is None  # the watcher reads its own clock
    assert tracker.moves_view() == {"alerts": [], "steps": 2}
    status = tracker.status()
    assert status["moves"] == watcher.status() and status["moves"] is not watcher.status()  # a copy
    assert status["read_budget"]["moves"] == {"used": 0, "limit": 4, "room": 4}  # MOVES_READS_PER_MIN
    assert not any(watcher.lock_held)


def test_moves_problem_follows_the_watchers_last_error(make_tracker: Any) -> None:
    watcher = StubWatcher()
    tracker = make_tracker(moves=watcher)
    tracker.run_once()
    watcher.last_error = "Kalshi is unreachable from this machine (could not connect): no outside quotes from Kalshi."
    tracker.moves_step()
    [problem] = [p for p in tracker.status()["problems"] if p["source"] == tracker_mod.MOVES_PROBLEM]
    assert problem["message"] == watcher.last_error and problem["severity"] == "warning"
    watcher.last_error = None
    tracker.moves_step()
    assert not any(p["source"] == "outside moves" for p in tracker.status()["problems"])
    assert tracker_mod.PROBLEM_SEVERITY["outside moves"] == "warning"


def test_cup_quotes_come_from_the_published_snapshot(make_tracker: Any, world: World, data_clock: FakeClock) -> None:
    from supermarket_bot.moves import CupQuote

    tracker = make_tracker()
    assert tracker.cup_quotes() == {} and tracker.cup_history(3600) == {}
    tracker.run_once()
    quotes = tracker.cup_quotes()
    assert set(quotes) == {"e1", "e2", "e3a", "e3b", "e3c"}
    assert quotes["e1"] == CupQuote(ts=T0, bid=0.39, ask=0.41, mid=pytest.approx(0.40), last=0.40, spread=pytest.approx(0.02))
    world.set("e1", 0.44, spread=0.04)
    data_clock.now += 30
    tracker.run_once()
    q = tracker.cup_quotes()["e1"]
    assert q.ts == T0 + 30 and (q.bid, q.ask) == (0.42, 0.46) and q.spread == pytest.approx(0.04)
    # the series cache: the last 2 h of tick snapshots, oldest first, ts = each snapshot's completion
    history = tracker.cup_history(7200)
    assert [(h.ts, h.mid) for h in history["e1"]] == [(T0, pytest.approx(0.40)), (T0 + 30, pytest.approx(0.44))]
    assert [h.ts for h in tracker.cup_history(10)["e1"]] == [T0 + 30]
    # an outcome the bulk price read stops returning is stale after 3 intervals: no quote (no look-ahead, no old price)
    del world.quotes["e2"]
    for _ in range(4):
        data_clock.now += 30
        tracker.run_once()
    assert "e2" not in tracker.cup_quotes() and "e1" in tracker.cup_quotes()


def test_the_cup_feed_book_uses_the_moves_budget_and_is_never_stored(make_tracker: Any, fake: Any) -> None:
    watcher = StubWatcher()
    tracker = make_tracker(moves=watcher, moves_reads_per_min=2)
    tracker.run_once()
    feed = watcher.cup
    obs = feed.book("e1")
    assert obs.source == "moves" and obs.observed_at == T0 and obs.exchange_id == "e1"
    assert obs.bids == [(0.39, 150.0), (0.38, 300.0)] and obs.asks == [(0.41, 120.0), (0.42, 250.0)]
    assert fake.calls_to("/exchanges/e1/orderbook")[-1].params == {"depth": "20", "tournamentId": TOURNAMENT_ID}
    assert tracker.store.book("e1") is None and tracker.store.book_snapshots("e1", 0.0) == []  # M12: not stored
    assert feed.book("e2") is not None
    reads = len(fake.calls_to("/exchanges/e1/orderbook"))
    assert tracker.moves_budget.status() == {"used": 2, "limit": 2, "room": 0}
    assert feed.book("e1") is None and len(fake.calls_to("/exchanges/e1/orderbook")) == reads  # no room: no read
    assert tracker.status()["read_budget"]["moves"] == {"used": 2, "limit": 2, "room": 0}


def test_the_cup_feed_book_stays_behind_the_read_reserve(make_client: Any, make_tracker: Any, fake: Any) -> None:
    client = make_client(reads_per_min=40)
    watcher = StubWatcher()
    tracker = make_tracker(client=client, moves=watcher)
    tracker.run_once()
    while client.read_limiter.used < 40 - 12:
        client.read_limiter.acquire()
    assert tracker.read_reserve.room() == 0 and tracker.moves_budget.room() == 0
    before = len(fake.calls_to("/exchanges/e1/orderbook"))
    assert watcher.cup.book("e1") is None and len(fake.calls_to("/exchanges/e1/orderbook")) == before
    off = StubWatcher()
    disabled = make_tracker(moves=off, moves_reads_per_min=0)
    assert disabled.moves_budget.status() == {"used": 0, "limit": 0, "room": 0}
    assert off.cup.book("e1") is None


def test_a_failed_cup_book_read_is_the_outside_moves_problem(make_tracker: Any, fake: Any) -> None:
    watcher = StubWatcher(use_feed=True)
    tracker = make_tracker(moves=watcher)
    tracker.run_once()
    fake.routes.pop(("GET", "/exchanges/e1/orderbook"))
    fake.add("GET", "/exchanges/e1/orderbook", (500, error_body("INTERNAL", "boom")))
    watcher.book_ids = ["e1"]
    tracker.moves_step()
    assert watcher.books == [None]
    [problem] = [p for p in tracker.status()["problems"] if p["source"] == "outside moves"]
    assert problem["severity"] == "warning" and problem["count"] == 1
    assert tracker.status()["fatal_error"] is None  # a 500 is not fatal
    assert tracker.status()["recent_errors"][-1]["where"] == "outside-move order book for exchange e1"
    watcher.book_ids = []
    tracker.moves_step()  # a clean step clears it
    assert not any(p["source"] == "outside moves" for p in tracker.status()["problems"])


def test_the_cup_feed_stored_book_history_and_quotes(make_tracker: Any) -> None:
    watcher = StubWatcher(use_feed=True)
    tracker = make_tracker(moves=watcher)
    feed = watcher.cup
    assert feed.stored_book("e1") is None
    tracker.store.put_book("e1", T0 - 30, [(0.39, 100)], [(0.41, 80), (0.42, 10)])
    obs = feed.stored_book("e1")
    assert (obs.exchange_id, obs.observed_at, obs.bids, obs.asks) == ("e1", T0 - 30, [(0.39, 100.0)], [(0.41, 80.0), (0.42, 10.0)])
    tracker.run_once()
    tracker.moves_step()
    assert set(watcher.quotes) == {"e1", "e2", "e3a", "e3b", "e3c"} and watcher.history["e1"][0].ts == T0


def test_the_projection_adds_the_moves_book_reads(make_tracker: Any) -> None:
    plain = make_tracker()
    with_moves = make_tracker(moves=StubWatcher())
    base = plain.status()["read_budget"]["projected_per_min"]
    assert with_moves.status()["read_budget"]["projected_per_min"] == pytest.approx(base + 4.0)
    none = make_tracker(moves=StubWatcher(), moves_reads_per_min=0)
    assert none.status()["read_budget"]["projected_per_min"] == pytest.approx(base)


def test_status_returns_quickly_while_a_slow_watcher_step_runs(make_tracker: Any) -> None:
    gate = threading.Event()
    watcher = StubWatcher(poll_s=0.05, gate=gate)
    tracker = make_tracker(interval=0.05, moves=watcher)
    watcher.tracker = tracker
    try:
        tracker.start()
        assert watcher.started.wait(5)  # the step is stuck (an outside host is slow)
        cycles = tracker.status()["cycles"]
        assert wait_for(lambda: tracker.status()["cycles"] >= cycles + 3, timeout=5)  # the snapshot loop goes on
        started = time.monotonic()
        tracker.status()
        tracker.view()
        tracker.moves_view()
        assert time.monotonic() - started < 1.0
    finally:
        gate.set()
        tracker.stop(timeout=2)
    assert watcher.lock_held and not any(watcher.lock_held)
