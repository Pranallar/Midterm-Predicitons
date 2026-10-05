"""Package C (backtest): no look-ahead, candle-only assumptions, same engine as live, sweeps, report. See docs/PAPER_TRADING.md §6.16.

Offline and deterministic: every history is a ``MemoryHistory`` built here. Most replays inject the signal
function and a fixed-units sizing policy (monkeypatched ``sizing.get_policy``) so the adapter and the engine
are tested without depending on package B's thresholds; one test runs the real strategy.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import analytics
from supermarket_bot import backtest as B
from supermarket_bot import paper as P
from supermarket_bot import sizing
from supermarket_bot.models import (
    Attribution,
    BacktestConfig,
    BetShape,
    BookObservation,
    ExchangeInfo,
    ExitPlan,
    FairValueRecord,
    FairValueRefresh,
    LeaderboardSnapshot,
    Opportunity,
    PaperConfig,
    PortfolioSpec,
    PricePoint,
    SettlementInfo,
    SizeDecision,
    StrategyInputs,
    Surge,
    TradeRecord,
)

T0 = 1_790_006_400.0  # on the hour and on the 300-s grid
END = "2026-11-04T17:00:00Z"
NV_D = ExchangeInfo("101", "m101", None, "Will the Democratic Party win the Nevada Governor?", END)
NV_R = ExchangeInfo("102", "m102", None, "Will the Republican Party win the Nevada Governor?", END)
TX_D = ExchangeInfo("103", "m103", None, "Will the Democratic Party win the Texas Senate?", END)
WY_R = ExchangeInfo("104", "m104", None, "Will the Republican Party win the Wyoming Governor?", END)


# --------------------------------------------------------------------------- helpers


def pt(ts: float, bid: float, ask: float) -> PricePoint:
    return PricePoint(ts=ts, price=round((bid + ask) / 2.0, 6), bid=bid, ask=ask, source="tick")


def ticks(start: float, end: float, bid: float, ask: float, every: float = 30.0) -> List[PricePoint]:
    n = int((end - start) // every) + 1
    return [pt(start + k * every, bid, ask) for k in range(n)]


def books(eid: str, start: float, end: float, bids: Sequence[Tuple[float, float]], asks: Sequence[Tuple[float, float]],
          every: float = 120.0) -> List[BookObservation]:
    n = int((end - start) // every) + 1
    return [BookObservation(eid, start + k * every, bids=list(bids), asks=list(asks), source="paper") for k in range(n)]


def fv_records(eid: str, value: float, start: float, end: float, every: float = 600.0, source: str = "polymarket",
               usable: bool = True) -> List[FairValueRecord]:
    n = int((end - start) // every) + 1
    out = []
    for k in range(n):
        ts = start + k * every
        out.append(FairValueRecord(ts=ts, exchange_id=eid, value=value, source=source, bid=value - 0.005,
                                   ask=value + 0.005, confidence="high", usable=usable,
                                   detail={"uncertainty": 0.005, "match_kind": "EXACT", "match_confidence": 1.0,
                                           "prev_value": value},
                                   as_of=ts, venues=[] if source == "history" else [source]))
    return out


def refreshes(start: float, end: float, status: str = "ok", venue: str = "polymarket", every: float = 60.0) -> List[FairValueRefresh]:
    n = int((end - start) // every) + 1
    return [FairValueRefresh(ts=start + k * every, venues={venue: {"status": status, "fetched_at": start + k * every}})
            for k in range(n)]


class Fixed:
    name = "fixed"
    label = "fixed"

    def __init__(self, units: int = 100) -> None:
        self.units = units

    def size(self, opp: Opportunity, ctx: Any) -> SizeDecision:
        return SizeDecision(idea_id=str(opp.idea_id), policy="fixed", units=self.units, stake=0.0)

    def size_all(self, opps: Sequence[Opportunity], ctx: Any) -> Dict[str, SizeDecision]:
        return {str(o.idea_id): self.size(o, ctx) for o in opps}

    def explain(self, ctx: Any) -> Dict[str, Any]:
        return {"policy": "fixed", "label": "fixed", "mode": None, "lines": []}


@pytest.fixture
def fixed_sizing(monkeypatch: pytest.MonkeyPatch) -> Fixed:
    pol = Fixed()
    monkeypatch.setattr(sizing, "get_policy", lambda name: pol)
    return pol


PORTS = [PortfolioSpec("human:conservative", "you", "conservative", None, None, 240.0, 360.0),
         PortfolioSpec("policy:conservative", "bot", "conservative")]


def cfg(start: float = T0, hours: float = 1.0, step: float = 300.0, **kw: Any) -> BacktestConfig:
    paper = kw.pop("paper", PaperConfig(portfolios=list(PORTS)))
    return BacktestConfig(start=start, end=start + hours * 3600.0, step_s=step, paper=paper, **kw)


def value_signal(eid: str = "103", limit: float = 0.52, side: str = "yes", kind: str = "value") -> Any:
    def fn(inputs: StrategyInputs, params: Any = None) -> List[Opportunity]:
        p = inputs.latest.get(eid)
        if p is None or p.ask is None:
            return []
        return [Opportunity(kind=kind, exchange_id=eid, market_id="m" + eid, title="x", option=None, side=side,
                            entry_price=p.ask, target_price=None, stop_price=None, prob_win=0.6, edge=0.05,
                            expected_return=0.1, horizon_hours=1.0, suggested_shares=0, suggested_cost=0.0, score=1.0,
                            confidence=0.6, rationale=["test"], idea_id=f"{kind}:x{eid}:{side}", limit_price=limit,
                            exit_plan=ExitPlan(kind="value"), bet=BetShape("binary", 0.6, 0.4, 0.5, p.ask),
                            factor_delta=0.05, fair_value=0.60 if kind == "value" else None)]
    return fn


def hole_signal(eid: str = "104", limit: float = 0.62, ttl: float = 3 * 3600.0) -> Any:
    def fn(inputs: StrategyInputs, params: Any = None) -> List[Opportunity]:
        if eid not in inputs.latest:
            return []
        return [Opportunity(kind="hole", exchange_id=eid, market_id="m" + eid, title="x", option=None, side="yes",
                            entry_price=limit, target_price=None, stop_price=None, prob_win=0.1, edge=0.2,
                            expected_return=0.3, horizon_hours=6.0, suggested_shares=0, suggested_cost=0.0, score=1.0,
                            confidence=0.5, rationale=["hole"], idea_id=f"hole:x{eid}:yes", order_type="maker",
                            limit_price=limit, expires_at=T0 + ttl, exit_plan=ExitPlan(kind="hole"),
                            bet=BetShape("fixed", 0.1, 0.3, 0.62, 0.62, cap_pct=0.01))]
    return fn


def tx_history(with_books: bool = True, with_fv: bool = True, **extra: Any) -> B.MemoryHistory:
    kw: Dict[str, Any] = dict(exchanges=[TX_D], points={"103": ticks(T0 - 7 * 3600, T0 + 7 * 3600, 0.50, 0.51)})
    if with_books:
        kw["books"] = {"103": books("103", T0 - 3600, T0 + 7 * 3600, [(0.50, 500)], [(0.51, 500), (0.52, 800)])}
    if with_fv:
        kw["fair_values"] = fv_records("103", 0.60, T0 - 3 * 3600, T0 + 7 * 3600)
        kw["refreshes"] = refreshes(T0 - 3 * 3600, T0 + 7 * 3600)
    kw.update(extra)
    return B.MemoryHistory(**kw)


def market_at(src: Any, config: BacktestConfig, t: Optional[float] = None) -> Tuple[B.HistoricalMarket, B.GuardedHistory]:
    pre = B.PreloadedHistory(src, config.start, config.end, warmup_s=config.warmup_s)
    guard = B.GuardedHistory(pre)
    market = B.HistoricalMarket(guard, config, pre)
    if t is not None:
        guard.set_time(t)
    return market, guard


# --------------------------------------------------------------------------- decision grid, report shape


def test_decision_grid_and_window_defaults() -> None:
    src = tx_history()
    c = BacktestConfig(start=T0 + 10, end=T0 + 3600, step_s=300.0)
    market, _ = market_at(src, c)
    times = market.decision_times()
    assert times[0] == T0 + 300 and times[-1] == T0 + 3600 and len(times) == 12
    assert all(b - a == 300.0 for a, b in zip(times, times[1:]))
    default = B.HistoricalMarket(B.GuardedHistory(src), BacktestConfig(hours=2.0))
    newest = T0 + 7 * 3600
    assert default.window() == (newest - 7200.0, newest)
    empty = B.run_backtest(B.MemoryHistory(exchanges=[TX_D]), BacktestConfig())
    assert empty.window["steps"] == 0 and empty.warnings == ["No stored ticks: there is nothing to replay yet."]


def test_report_shape(fixed_sizing: Fixed) -> None:
    rep = B.run_backtest(tx_history(), cfg(hours=1.0), signal_fn=value_signal())
    d = rep.to_dict()
    for key in ("generated_at", "config", "window", "coverage", "assumptions", "portfolios", "equity", "trades",
                "verdicts", "sweep", "warnings", "testability", "study", "overlap_hours", "stopped_early"):
        assert key in d
    assert {"start", "end", "hours", "steps"} <= set(rep.window) and rep.window["steps"] == 13
    for key in ("exchanges", "decision_steps", "tick_share", "candle_share", "book_snapshot_share",
                "synthetic_book_share", "fair_value_share", "tape_share", "guard_filtered"):
        assert key in rep.coverage
    assert list(rep.testability) == ["value", "basket", "hole", "fade", "carry", "arbitrage"]
    assert [p.portfolio_id for p in rep.portfolios] == ["human:conservative", "policy:conservative"]
    assert set(rep.verdicts) == {"human:conservative", "policy:conservative"}
    assert rep.study is not None and rep.study["can_show"]
    assert B.SIZING_ASSUMPTION in rep.assumptions and B.NOT_REPLAYABLE_ARBITRAGE in rep.assumptions
    assert any("waits 240 s" in a for a in rep.assumptions)
    assert "Only 1.0 hours of data in this window: too little for a verdict." in rep.warnings
    assert rep.stopped_early is False and rep.overlap_hours is None
    json.dumps(d)


# --------------------------------------------------------------------------- the look-ahead guard


def test_guard_requires_until_at_or_before_t_for_every_method() -> None:
    src = tx_history(trades={"103": [TradeRecord("a", "103", T0 - 100, 0.5, 1, "NO", T0 - 50)]})
    pre = B.PreloadedHistory(src, T0, T0 + 3600)
    guard = B.GuardedHistory(pre)
    assert guard.tick_bounds() is not None  # allowed before the first set_time
    guard.set_time(T0)
    with pytest.raises(B.LookAheadError):
        guard.tick_bounds()
    calls = [
        lambda u: guard.series("103", T0 - 600, u),
        lambda u: guard.candles("103", "1h", T0 - 7200, u),
        lambda u: guard.trades("103", T0 - 600, u),
        lambda u: guard.book_snapshots("103", T0 - 600, u),
        lambda u: guard.fair_value_history(T0 - 600, u),
        lambda u: guard.fair_value_refreshes(T0 - 600, u),
        lambda u: guard.leaderboard_snapshots(T0 - 600, u),
        lambda u: guard.surges(since=T0 - 600, until=u),
    ]
    for call in calls:
        call(T0)  # a correct call never raises
        with pytest.raises(B.LookAheadError):
            call(T0 + 1)
        with pytest.raises(B.LookAheadError):
            call(None)
    assert [i.exchange_id for i in guard.exchanges()] == ["103"]
    fresh = B.GuardedHistory(pre)
    with pytest.raises(B.LookAheadError):
        fresh.series("103", T0 - 600, T0)  # no decision time yet


def test_guard_drops_a_leaky_partial_candle_and_tape_fetched_later() -> None:
    t = T0 + 3600
    leaky = {"ts": t - 600, "open": 0.5, "high": 0.95, "low": 0.5, "close": 0.9, "volume": 100, "trade_count": 3}
    closed = {"ts": t - 4200, "open": 0.5, "high": 0.52, "low": 0.49, "close": 0.5, "volume": 50, "trade_count": 2}
    tape = [TradeRecord("ok", "103", t - 100, 0.5, 1, "NO", t - 90),
            TradeRecord("late", "103", t - 100, 0.5, 1, "NO", t + 60),  # printed before t, fetched after it
            TradeRecord("old", "103", t - 100, 0.5, 1, "NO", None)]  # stored before schema v2: never replayed
    src = tx_history(candles={("103", "1h"): [leaky, closed]}, trades={"103": tape})
    pre = B.PreloadedHistory(src, T0, T0 + 7200)
    guard = B.GuardedHistory(pre)
    guard.set_time(t)
    got = guard.candles("103", "1h", t - 7200, t)
    assert [c["ts"] for c in got] == [t - 4200] and got[0]["close_ts"] == t - 600
    assert [x.trade_id for x in guard.trades("103", t - 3600, t)] == ["ok"]
    assert guard.filtered >= 3
    guard.set_time(t + 3000)  # the bucket has closed: now it is history
    assert [c["ts"] for c in guard.candles("103", "1h", t - 7200, t + 3000)] == [t - 4200, t - 600]
    assert {x.trade_id for x in guard.trades("103", t - 3600, t + 3000)} == {"ok", "late"}


def _surge(detected: float, analyzed: Optional[float], sid: int = 7) -> Surge:
    att = Attribution(verdict="participants", confidence=0.7, reversion_odds=0.6, summary="one account",
                      analyzed_at=analyzed) if analyzed is not None else None
    return Surge(exchange_id="103", market_id="m103", window="5m", window_s=300.0, start_ts=detected - 300,
                 end_ts=detected, start_price=0.50, end_price=0.60, change=0.10, direction="up", peak_price=0.70,
                 detected_at=detected, status="reverted", current_price=0.52, reverted_fraction=0.9, attribution=att,
                 id=sid)


def test_guard_hides_future_surges_and_attributions_and_later_fields() -> None:
    src = tx_history(surges=[_surge(T0 + 600, T0 + 1500), _surge(T0 + 3000, T0 + 3100, sid=8)])
    pre = B.PreloadedHistory(src, T0, T0 + 7200)
    guard = B.GuardedHistory(pre)
    guard.set_time(T0 + 900)
    (s,) = guard.surges(since=T0, until=T0 + 900)
    assert s.id == 7 and s.attribution is None  # analysed after t
    assert s.status == "open" and s.peak_price == 0.60 and s.current_price == 0.60 and s.reverted_fraction == 0.0
    guard.set_time(T0 + 1500)
    (s2,) = guard.surges(since=T0, until=T0 + 1500)
    assert s2.attribution is not None and s2.attribution.verdict == "participants"
    assert pre.surges(since=T0, until=T0 + 7200)[0].status == "reverted"  # the stored row itself is untouched
    guard.set_time(T0 + 3200)
    assert [x.id for x in guard.surges(since=T0, until=T0 + 3200)] == [8, 7]


def test_guard_settlements_only_once_known() -> None:
    late = SettlementInfo("103", "m103", "YES", T0 + 600, 1.0, False, T0 + 3000)  # settledOn backdated
    src = tx_history(settlements={"103": late})
    pre = B.PreloadedHistory(src, T0, T0 + 7200)
    guard = B.GuardedHistory(pre)
    guard.set_time(T0 + 1800)
    assert guard.settlements() == {}
    guard.set_time(T0 + 3000)
    assert set(guard.settlements()) == {"103"}
    assert B._settled_known_at(SettlementInfo("1", "m", "NO", T0, 0.0, False, None)) == T0


# --------------------------------------------------------------------------- quotes, books, fills


def test_candle_only_history_gives_single_quotes_but_never_sets() -> None:
    from supermarket_bot import strategy

    def candle(ts: float, close: float) -> Dict[str, Any]:
        return {"ts": ts, "open": close, "high": close, "low": close, "close": close, "volume": 500, "trade_count": 5}

    t = T0 + 3600
    candle_src = B.MemoryHistory(exchanges=[NV_D, NV_R], candles={
        ("101", "5m"): [candle(t - 900, 0.56)], ("102", "5m"): [candle(t - 1500, 0.55)]})
    c = cfg(start=T0, hours=2.0)
    market, _ = market_at(candle_src, c, t)
    inputs = market.inputs(t)
    p = inputs.latest["101"]
    assert p.source == "candle" and p.bid == 0.55 and p.ask == 0.57 and p.ts == t - 600
    assert inputs.latest["102"].source == "candle"
    assert [o for o in strategy.generate_signals(inputs, inputs.params) if o.kind == "basket"] == []
    # the same prices as one bulk snapshot of ticks DO form the NO basket (YES bids 0.55 + 0.54 = 1.09)
    tick_src = B.MemoryHistory(exchanges=[NV_D, NV_R], points={
        "101": ticks(T0, t, 0.55, 0.57), "102": ticks(T0, t, 0.54, 0.56)})
    market2, _ = market_at(tick_src, c, t)
    inputs2 = market2.inputs(t)
    assert inputs2.latest["101"].source == "tick"
    baskets = [o for o in strategy.generate_signals(inputs2, inputs2.params) if o.kind == "basket"]
    assert baskets and baskets[0].unit == "sets"
    # a candle that closed more than an hour ago is no quote at all
    old_src = B.MemoryHistory(exchanges=[NV_D], candles={("101", "5m"): [candle(t - 4800, 0.56)]})
    market3, _ = market_at(old_src, c, t)
    assert market3.inputs(t).latest == {}


def test_synthetic_books_have_no_depth_without_a_closed_candle() -> None:
    t = T0 + 3600
    hour = {"ts": t - 3900, "open": 0.5, "high": 0.52, "low": 0.49, "close": 0.5, "volume": 2000, "trade_count": 9}
    with_candle = tx_history(with_books=False, candles={("103", "1h"): [hour]})
    market, _ = market_at(with_candle, cfg(hours=2.0), t)
    book = market._first_tick_book("103", t - 200, t)
    assert book.source == "synthetic" and book.observed_at == t - 180
    assert book.bids == [(0.50, 100.0)] and book.asks == [(0.51, 100.0)]  # min(100, 10% of 2,000)
    market2, _ = market_at(tx_history(with_books=False), cfg(hours=2.0), t)
    assert market2._first_tick_book("103", t - 200, t).bids == [(0.50, 0.0)]  # no candle: no depth, no fill


def test_book_snapshots_fill_orders_after_the_latency(fixed_sizing: Fixed) -> None:
    rep = B.run_backtest(tx_history(), cfg(hours=1.0), signal_fn=value_signal())
    assert rep.coverage["book_snapshot_share"] == 1.0 and rep.coverage["synthetic_book_share"] == 0.0
    fills = {p.portfolio_id: p for p in rep.portfolios}
    assert fills["policy:conservative"].fills == 1 and fills["human:conservative"].fills == 1
    pos = rep.portfolios[1]
    assert pos.positions_open == 1 and pos.depth_unknown_share == 0.0


def test_fills_use_the_first_observation_after_each_portfolios_latency(fixed_sizing: Fixed,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []
    real = P.PaperEngine.step

    def spy(self: P.PaperEngine, now: float, obs: Any, signals: Any, inputs: Any = None, **kw: Any) -> Any:
        r = real(self, now, obs, signals, inputs, **kw)
        seen.extend(self.fills(limit=1000))
        return r

    monkeypatch.setattr(P.PaperEngine, "step", spy)
    B.run_backtest(tx_history(), cfg(hours=1.0, latency_s=60.0), signal_fn=value_signal())
    first = {}
    for f in seen:
        first.setdefault(f.portfolio_id, f)
    # decisions at T0; snapshots every 120 s: the bot (60 s) gets the one at T0 + 120, you (240 s) the one at T0 + 240
    assert first["policy:conservative"].book_at == T0 + 120 and first["human:conservative"].book_at == T0 + 240
    assert all(not f.synthetic for f in first.values())


def test_synthetic_fills_are_flagged_and_left_out_of_the_verdict(fixed_sizing: Fixed) -> None:
    hours = [{"ts": T0 - 3600 + k * 3600, "open": 0.5, "high": 0.52, "low": 0.49, "close": 0.5, "volume": 5000,
              "trade_count": 9} for k in range(4)]
    src = tx_history(with_books=False, candles={("103", "1h"): hours})
    rep = B.run_backtest(src, cfg(hours=1.0), signal_fn=value_signal())
    assert rep.coverage["synthetic_book_share"] == 1.0
    bot = rep.portfolios[1]
    assert bot.fills == 1 and bot.verdict.synthetic_excluded == 1 and bot.verdict.n_ideas == 0
    # synthetic books never mark: the position is valued by the depth-unknown rule
    assert bot.depth_unknown_share and bot.depth_unknown_share > 0


def test_candle_trade_throughs_fill_only_inside_the_orders_life(fixed_sizing: Fixed) -> None:
    def c5(start: float, low: float, high: float, volume: float) -> Dict[str, Any]:
        return {"ts": start, "open": high, "high": high, "low": low, "close": high, "volume": volume, "trade_count": 4}

    candles5 = [c5(T0 - 300, 0.50, 0.70, 10_000),  # before the order: never
                c5(T0 + 300, 0.60, 0.70, 1000),  # inside: 10% x 1,000 x (0.62 - 0.60) / 0.10 = 20
                c5(T0 + 900, 0.55, 0.65, 10_000),  # inside: capped at 50
                c5(T0 + 1500, 0.63, 0.70, 10_000)]  # never traded through 0.62
    hour = [{"ts": T0, "open": 0.9, "high": 0.95, "low": 0.40, "close": 0.9, "volume": 10 ** 6, "trade_count": 99}]
    src = B.MemoryHistory(exchanges=[WY_R], points={"104": ticks(T0 - 7 * 3600, T0 + 3 * 3600, 0.93, 0.94)},
                          candles={("104", "5m"): candles5, ("104", "1h"): hour})
    bot_only = PaperConfig(portfolios=[PortfolioSpec("policy:conservative", "bot", "conservative")])
    rep = B.run_backtest(src, cfg(hours=0.5, step=300.0, latency_s=60.0, paper=bot_only), signal_fn=hole_signal())
    (port,) = rep.portfolios
    assert port.fills == 2 and rep.coverage["maker_fills_candle"] == 2 and rep.coverage["maker_fills_tape"] == 0
    assert port.positions_open == 1 and port.verdict.synthetic_excluded == 1
    assert port.cash == pytest.approx(100_000 - (20 + 50) * 0.62)  # 20 from the first candle, the cap (50) from the second
    assert rep.coverage["tape_share"] == 0.0
    assert any("candle" in a and "flagged as assumed" in a for a in rep.assumptions)


def test_stored_tape_fetched_by_t_fills_resting_orders(fixed_sizing: Fixed) -> None:
    tape = [TradeRecord("p1", "104", T0 + 400, 0.61, 30, "NO", T0 + 450),
            TradeRecord("p2", "104", T0 + 500, 0.60, 40, "NO", T0 + 1300)]  # fetched only at T0 + 1300
    src = B.MemoryHistory(exchanges=[WY_R], points={"104": ticks(T0 - 7 * 3600, T0 + 3 * 3600, 0.93, 0.94)},
                          trades={"104": tape})
    bot_only = PaperConfig(portfolios=[PortfolioSpec("policy:conservative", "bot", "conservative")])
    seen: List[Tuple[float, float]] = []

    def progress(i: int, n: int) -> None:
        seen.append((i, n))

    rep = B.run_backtest(src, cfg(hours=0.5, paper=bot_only), signal_fn=hole_signal(), progress=progress)
    (port,) = rep.portfolios
    assert port.fills == 2 and rep.coverage["maker_fills_tape"] == 2 and port.verdict.synthetic_excluded == 0
    assert port.cash == pytest.approx(100_000 - (30 + 40) * 0.62)  # p2 counts once it was fetched (T0 + 1300)
    assert seen[-1] == (7, 7)


# --------------------------------------------------------------------------- fair-value replay


def test_constant_fair_value_recorded_live_is_fresh_at_every_step() -> None:
    src = tx_history()
    c = cfg(hours=3.0, latency_s=60.0)
    market, guard = market_at(src, c)
    for t in market.decision_times():
        guard.set_time(t)
        fv = market.inputs(t).fair_values["103"]
        assert fv.usable and fv.value == 0.60 and t - fv.as_of <= 90.0, (t, fv.as_of)
        assert fv.uncertainty == 0.005 and fv.prev_value == 0.60 and fv.history is False


def test_fair_value_goes_stale_after_a_provider_outage() -> None:
    outage = T0 + 1800
    refs = refreshes(T0 - 3 * 3600, outage) + refreshes(outage + 60, T0 + 7 * 3600, status="error")
    src = tx_history(refreshes=refs)
    c = cfg(hours=3.0, latency_s=60.0)
    market, guard = market_at(src, c)
    ages = {}
    for t in market.decision_times():
        guard.set_time(t)
        ages[t] = t - market.inputs(t).fair_values["103"].as_of
    assert ages[T0 + 1800] <= 90 and ages[T0 + 3600] > 90  # the record's own as_of ages: stale as it was live
    rec = fv_records("103", 0.6, T0, T0)[0]
    replayed = B.replay_fair_value(rec, FairValueRefresh(T0 + 300, {"polymarket": {"status": "ok", "fetched_at": T0 + 295}}),
                                   T0 + 360, 60.0)
    assert replayed.as_of == T0 + 295
    failed = B.replay_fair_value(rec, FairValueRefresh(T0 + 300, {"polymarket": {"status": "error"}}), T0 + 360, 60.0)
    assert failed.as_of == T0


def test_fair_values_are_available_only_after_the_latency_and_history_records_are_flagged() -> None:
    late = [FairValueRecord(ts=T0 + 1000, exchange_id="103", value=0.6, source="manual", usable=True,
                            confidence="high", as_of=T0 + 1000)]
    market, guard = market_at(tx_history(fair_values=late, refreshes=[]), cfg(hours=1.0, latency_s=60.0))
    guard.set_time(T0 + 1050)
    assert market.inputs(T0 + 1050).fair_values == {}  # recorded 50 s ago: not available with a 60-s latency
    guard.set_time(T0 + 1060)
    fv = market.inputs(T0 + 1060).fair_values["103"]
    assert fv.source == "manual" and fv.match_kind == "MANUAL"
    hist = fv_records("103", 0.58, T0 - 3600, T0 + 3600, every=300.0, source="history")
    m2, g2 = market_at(tx_history(fair_values=hist, refreshes=[]), cfg(hours=1.0))
    g2.set_time(T0 + 600)
    hv = m2.inputs(T0 + 600).fair_values["103"]
    assert hv.history is True and hv.source == "history"
    m3, g3 = market_at(tx_history(fair_values=hist, refreshes=[]), cfg(hours=1.0, use_history_fair_values=False))
    g3.set_time(T0 + 600)
    assert m3.inputs(T0 + 600).fair_values == {}
    m4, g4 = market_at(tx_history(), cfg(hours=1.0, use_fair_values=False))
    g4.set_time(T0 + 600)
    assert m4.inputs(T0 + 600).fair_values == {}


# --------------------------------------------------------------------------- settlements, surges, scanner


def test_settlement_is_known_at_max_of_settled_on_and_detected_at(fixed_sizing: Fixed) -> None:
    info = SettlementInfo("103", "m103", "YES", T0 + 600, 1.0, False, T0 + 2400)
    src = tx_history(settlements={"103": info})
    market, guard = market_at(src, cfg(hours=1.0))
    guard.set_time(T0 + 1800)
    assert market.observation(T0 + 1800).settlements == {} and "103" in market.inputs(T0 + 1800).infos
    guard.set_time(T0 + 2400)
    o = market.observation(T0 + 2400)
    assert set(o.settlements) == {"103"} and "103" not in o.open_ids
    rep = B.run_backtest(src, cfg(hours=1.0), signal_fn=value_signal())
    trades = [t for t in rep.trades if t.exit_reason == "settled"]
    assert trades and all(t.closed_at == T0 + 2400 for t in trades)


def _surge_series(eid: str, jump_at: float) -> List[PricePoint]:
    pts = []
    k = 0
    t = T0 - 31 * 3600
    while t <= T0 + 2 * 3600:
        base = 0.60 if t >= jump_at else 0.50
        mid = base + (0.005 if k % 2 else 0.0)
        pts.append(pt(t, round(mid - 0.005, 3), round(mid + 0.005, 3)))
        k += 1
        t += 30.0
    return pts


def test_surges_are_redetected_and_attributed_only_after_analysis() -> None:
    jump = T0 + 600
    stored = _surge(jump, jump + 900)
    stored.direction = "up"
    src = B.MemoryHistory(exchanges=[TX_D], points={"103": _surge_series("103", jump)}, surges=[stored])
    c = cfg(hours=1.0)
    market, guard = market_at(src, c)
    found: Dict[float, List[Surge]] = {}
    for t in market.decision_times():
        guard.set_time(t)
        found[t] = market.inputs(t).surges
    assert found[T0 + 300] == []
    (s,) = found[jump]
    assert s.direction == "up" and s.detected_at == jump and s.attribution is None
    (s2,) = found[jump + 900]
    assert s2.attribution is not None and s2.id == 7


def test_incremental_scanner_matches_detect_surges_on_the_cut_series() -> None:
    jump = T0 + 1200
    pts = _surge_series("103", jump)
    src = B.MemoryHistory(exchanges=[TX_D], points={"103": pts})
    c = cfg(hours=1.0)
    market, guard = market_at(src, c)
    scanner = market._scanner("103")
    for t in market.decision_times():
        cut = [p for p in pts if p.ts <= t]
        expect = analytics.detect_surges(cut, t, "103", "m103")
        got = scanner.detect(t, "103", "m103")
        assert [(s.window, s.start_ts, s.change, s.zscore) for s in got] == \
               [(s.window, s.start_ts, s.change, s.zscore) for s in expect], t


# --------------------------------------------------------------------------- engine, testability, warnings, sweeps


def test_the_live_paper_engine_class_is_used(fixed_sizing: Fixed, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[float] = []
    real = P.PaperEngine.step

    def spy(self: P.PaperEngine, now: float, *a: Any, **kw: Any) -> Any:
        calls.append(now)
        return real(self, now, *a, **kw)

    monkeypatch.setattr(P.PaperEngine, "step", spy)
    rep = B.run_backtest(tx_history(), cfg(hours=1.0), signal_fn=value_signal())
    assert calls == [T0 + 300 * k for k in range(13)] and rep.window["steps"] == 13


def test_backtest_paper_config_latency_and_delays() -> None:
    c = cfg(hours=1.0, latency_s=60.0)
    pc = B.backtest_paper_config(c)
    lat = {p.portfolio_id: (p.min_latency_s, p.max_fill_delay_s) for p in pc.portfolios}
    assert lat["human:conservative"] == (240.0, 540.0) and lat["policy:conservative"] == (60.0, 360.0)
    assert pc.interval_s == 300.0
    with pytest.raises(ValueError):
        B.backtest_paper_config(cfg(params_overrides={"no_such_param": 1}))
    assert B.backtest_paper_config(cfg(params_overrides={"min_value_edge": 0.05})).params.min_value_edge == 0.05


def test_testability_table() -> None:
    half = fv_records("103", 0.60, T0 + 1800, T0 + 3600)
    src = tx_history(fair_values=half, refreshes=refreshes(T0, T0 + 7200))
    rep = B.run_backtest(src, cfg(hours=1.0, step=300.0), signal_fn=lambda i, p: [])
    tb = rep.testability
    assert tb["value"]["status"] == "partial" and 0 < tb["value"]["hours"] < 1.1
    assert tb["arbitrage"] == {"status": "not_replayable", "hours": 0.0, "sentence": B.NOT_REPLAYABLE_ARBITRAGE}
    assert tb["carry"]["status"] == "testable" and "pays at resolution" in tb["carry"]["sentence"]
    assert tb["basket"]["sentence"].startswith("basket: ") and "candle-only hours cannot price a set" in tb["basket"]["sentence"]
    none = B.run_backtest(tx_history(with_fv=False), cfg(hours=1.0), signal_fn=lambda i, p: [])
    assert none.testability["value"] == {
        "status": "not_testable", "hours": 0.0,
        "sentence": ("value: not testable yet (0 h of outside prices recorded or imported in this window); run "
                     "`fairvalue --import-history` or let the bot record for a day")}
    assert "No fair values were recorded in this window: value ideas could not trade." in none.warnings
    full = B.run_backtest(tx_history(), cfg(hours=1.0), signal_fn=lambda i, p: [])
    assert full.testability["value"]["status"] == "testable" and full.testability["hole"]["status"] == "testable"


def test_candle_share_warning() -> None:
    def candle(ts: float) -> Dict[str, Any]:
        return {"ts": ts, "open": 0.5, "high": 0.5, "low": 0.5, "close": 0.5, "volume": 10, "trade_count": 1}

    src = B.MemoryHistory(exchanges=[TX_D], candles={("103", "5m"): [candle(T0 - 3600 + 300 * k) for k in range(30)]})
    rep = B.run_backtest(src, cfg(hours=1.0), signal_fn=lambda i, p: [])
    assert rep.coverage["candle_share"] == 1.0
    assert "Most quotes are candle-based: spreads and depth are assumptions." in rep.warnings
    assert rep.testability["basket"]["status"] == "not_testable"


def test_overlap_warning() -> None:
    rep = B.run_backtest(tx_history(), cfg(hours=2.0, live_run_started_at=T0 + 1800), signal_fn=lambda i, p: [])
    assert rep.overlap_hours == pytest.approx(1.5)
    assert B.OVERLAP_WARNING.format(h=1.5) in rep.warnings
    assert "not an independent check" in B.OVERLAP_WARNING
    clean = B.run_backtest(tx_history(), cfg(hours=2.0, live_run_started_at=T0 + 9000), signal_fn=lambda i, p: [])
    assert clean.overlap_hours == 0.0 and not any("independent check" in w for w in clean.warnings)


def test_time_budget_stops_early() -> None:
    ticker = iter(range(10 ** 6))
    rep = B.run_backtest(tx_history(), cfg(hours=1.0, time_budget_s=5.0), signal_fn=lambda i, p: [],
                         clock=lambda: float(next(ticker)))
    assert rep.stopped_early is True and 0 < rep.window["steps"] < 13
    assert any(w.startswith("Stopped early after 5 s of wall time") for w in rep.warnings)


def test_parse_sweep() -> None:
    grid = B.parse_sweep(["min_value_edge=0.01,0.02", "latency_s=30,60", "use_book_snapshots=true,false",
                          "regime=unknown,resolved_outcomes"])
    assert grid == {"min_value_edge": [0.01, 0.02], "latency_s": [30, 60], "use_book_snapshots": [True, False],
                    "regime": ["unknown", "resolved_outcomes"]}
    with pytest.raises(ValueError) as err:
        B.parse_sweep(["nonsense=1,2"])
    assert "valid names" in str(err.value) and "min_value_edge" in str(err.value)
    with pytest.raises(ValueError):
        B.parse_sweep(["min_value_edge"])


def test_sweep_rows_limit_and_warning(fixed_sizing: Fixed) -> None:
    src = tx_history()
    grid = B.parse_sweep(["latency_s=30,120,300"])  # the CLI's --latency-sweep
    rep = B.sweep(src, cfg(hours=1.0), grid, signal_fn=value_signal())
    assert len(rep.sweep) == 3
    assert {r["label"] for r in rep.sweep} == {"latency_s=30", "latency_s=120", "latency_s=300"}
    for row in rep.sweep:
        assert set(row) == {"params", "label", "pnl_liq", "trades_closed", "verdict_level", "max_drawdown"}
    pnls = [r["pnl_liq"] for r in rep.sweep]
    assert pnls == sorted(pnls, reverse=True)
    assert B.SWEEP_WARNING.format(n=3) in rep.warnings
    two = B.sweep(src, cfg(hours=1.0), {"min_value_edge": [0.01, 0.02], "latency_s": [30, 60]}, signal_fn=value_signal())
    assert {r["label"] for r in two.sweep} == {"min_value_edge=0.01, latency_s=30", "min_value_edge=0.01, latency_s=60",
                                              "min_value_edge=0.02, latency_s=30", "min_value_edge=0.02, latency_s=60"}
    with pytest.raises(ValueError):
        B.sweep(src, cfg(hours=1.0), {"latency_s": list(range(51))})


def test_memory_history_contract() -> None:
    src = tx_history(trades={"103": [TradeRecord("b", "103", T0 + 5, 0.5, 1), TradeRecord("a", "103", T0 + 1, 0.5, 1)]},
                     leaderboards=[LeaderboardSnapshot(at=T0 + 10), LeaderboardSnapshot(at=T0 - 10)])
    assert [t.trade_id for t in src.trades("103", T0, T0 + 10)] == ["a", "b"]
    assert [s.at for s in src.leaderboard_snapshots(T0 - 100, T0 + 100)] == [T0 - 10, T0 + 10]
    assert src.tick_bounds() == (T0 - 7 * 3600, T0 + 7 * 3600)
    assert len(src.series("103", T0, T0 + 60)) == 3 and src.series("103", T0, T0 + 60)[0].source == "tick"
    assert src.fair_value_history(T0, T0, exchange_ids=["103"])[0].value == 0.60


# --------------------------------------------------------------------------- with the real strategy, and performance


def _many_outcomes(n: int, hours: float) -> B.MemoryHistory:
    infos, points, fvs = [], {}, []
    states = ["AZ", "GA", "MI", "NV", "PA", "WI", "NC", "OH", "TX", "FL", "ME", "NH", "IA", "MN", "VA", "CO", "NM", "AK",
              "NE", "KS"]
    start = T0 - 7 * 3600
    end = T0 + hours * 3600
    for k in range(n):
        eid = str(1000 + k)
        party = "Democratic" if k % 2 == 0 else "Republican"
        infos.append(ExchangeInfo(eid, f"m{eid}", None, f"Will the {party} Party win the {_STATE[states[(k // 2) % 20]]} Senate?",
                                  END))
        base = 0.30 + 0.4 * ((k * 7) % 10) / 10.0
        pts = []
        steps = int((end - start) // 30) + 1
        for j in range(steps):
            ts = start + j * 30
            mid = base + 0.01 * math.sin(j / 40.0 + k)
            bid = round(math.floor(mid * 200) / 200, 3)
            pts.append(pt(ts, bid, round(bid + 0.01, 3)))
        points[eid] = pts
        if k % 4 == 0:
            fvs.extend(fv_records(eid, round(base + 0.08, 3), start, end))
    return B.MemoryHistory(exchanges=infos, points=points, fair_values=fvs, refreshes=refreshes(start, end))


_STATE = {"AZ": "Arizona", "GA": "Georgia", "MI": "Michigan", "NV": "Nevada", "PA": "Pennsylvania", "WI": "Wisconsin",
          "NC": "North Carolina", "OH": "Ohio", "TX": "Texas", "FL": "Florida", "ME": "Maine", "NH": "New Hampshire",
          "IA": "Iowa", "MN": "Minnesota", "VA": "Virginia", "CO": "Colorado", "NM": "New Mexico", "AK": "Alaska",
          "NE": "Nebraska", "KS": "Kansas"}


def test_real_strategy_replay_runs_cleanly() -> None:
    src = _many_outcomes(8, 2.0)
    rep = B.run_backtest(src, BacktestConfig(start=T0, end=T0 + 2 * 3600, step_s=300.0))
    assert rep.window["steps"] == 25
    assert [p.portfolio_id for p in rep.portfolios][:3] == ["human:conservative", "policy:conservative", "policy:chaser"]
    assert all(p.verdict is not None and p.verdict.level == "insufficient" for p in rep.portfolios)
    assert rep.study is not None
    json.dumps(rep.to_dict())


def test_scaled_performance() -> None:
    src = _many_outcomes(40, 6.0)
    t0 = time.monotonic()
    rep = B.run_backtest(src, BacktestConfig(start=T0, end=T0 + 6 * 3600, step_s=300.0))
    took = time.monotonic() - t0
    assert rep.window["steps"] == 73 and took < 10.0, took


@pytest.mark.skipif(os.environ.get("SUPERMARKET_PERF") != "1", reason="full-size performance run: set SUPERMARKET_PERF=1")
def test_full_size_performance() -> None:
    src = _many_outcomes(237, 24.0)
    t0 = time.monotonic()
    rep = B.run_backtest(src, BacktestConfig(start=T0, end=T0 + 24 * 3600, step_s=300.0))
    took = time.monotonic() - t0
    assert rep.window["steps"] == 289 and took < B.PERFORMANCE_TARGET_S, took
