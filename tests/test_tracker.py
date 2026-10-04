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
    "change_5m", "change_1h", "change_24h", "sparkline", "high_band", "surge_id", "updated_at",
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


def _fake_detect(points: List[PricePoint], now: float, exchange_id: str, market_id: str, windows: Any = None, z_threshold: float = 3.0) -> List[Surge]:
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


FAKE_ANALYTICS = SimpleNamespace(
    mark_price=fallback_mark_price,
    detect_surges=_fake_detect,
    update_surge_status=_fake_update,
    high_band=_fake_band,
    change_over=_fake_change,
    downsample=_fake_downsample,
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
    assert len(fake.calls_to(f"/tournaments/{SLUG}/markets")) == 1
    assert "e4" not in [r["exchange_id"] for r in tracker.view()["exchanges"]]
    summary = step(tracker, data_clock, 180)  # 300 s since the first refresh
    assert summary["markets_refreshed"] and summary["exchanges"] == 6
    assert len(fake.calls_to(f"/tournaments/{SLUG}/markets")) == 2
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
    assert merged.start_ts == T0 + 60 and merged.end_ts == T0 + 420 and merged.detected_at == T0 + 360
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
        {"market_id": "m3", "title": "Who will win the Arizona Governor race?", "overround": 1.04, "arbitrage": True, "outcomes": 3}
    ]
    assert ctx["updated_at"] == T0
    assert fake.calls_to("/relationships/constraints")[0].params == {"violationsOnly": "true", "tournamentId": TOURNAMENT_ID}
    assert fake.calls_to(f"/tournaments/{SLUG}/leaderboard")[0].params["limit"] == "3"
    assert fake.calls_to("/markets/m3/orderbook")[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "1"}
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
    step(tracker, data_clock, 300)
    assert [r["market_id"] for r in tracker.view()["context"]["overround"]] == ["m3", "m4", "m5", "m6"]
    step(tracker, data_clock, 300)
    assert len(fake.calls_to("/markets/m3/orderbook")) == 2
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
        fake.add("GET", "/exchanges/e1/orderbook", book_exchange("e1", [(0.59, 50)], [(0.61, 60)]))

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
