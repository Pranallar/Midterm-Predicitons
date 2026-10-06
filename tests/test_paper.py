"""Package C (paper): fill model, maker fills, exits, settlement, valuation, accounting, verdict, persistence, runner budget. See docs/PAPER_TRADING.md §6.16.

Everything here is offline and deterministic: hand-built observations, ``MemoryPaperPersistence`` and a
fixed-units sizing policy (``FixedPolicy``) so the engine is tested without package B. One test at the end
drives the real ``strategy.generate_signals`` and ``sizing`` policies.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import paper as P
from supermarket_bot.models import (
    HUMAN_LATENCY_S,
    HUMAN_MAX_FILL_DELAY_S,
    BetShape,
    BookObservation,
    ExchangeInfo,
    ExitPlan,
    FairValue,
    MarketObservation,
    Opportunity,
    PaperConfig,
    PaperTrade,
    PortfolioSpec,
    Quote,
    RaceRef,
    SettlementInfo,
    SizeDecision,
    StrategyInputs,
    StrategyParams,
    TradeRecord,
    default_portfolios,
)

T = 1_790_000_000.0  # 2026-09-21 (UTC), well before the Cup end
CUP = 1_793_811_600.0  # 2026-11-04T17:00:00Z


# --------------------------------------------------------------------------- helpers


class FixedPolicy:
    """A sizing policy that buys a fixed number of units of every idea (per-idea overrides possible)."""

    name = "fixed"
    label = "Fixed units"

    def __init__(self, units: int = 100, per_idea: Optional[Dict[str, int]] = None,
                 replaces: Optional[Dict[str, str]] = None, mode: Optional[str] = None) -> None:
        self.units = units
        self.per_idea = dict(per_idea or {})
        self.replaces = dict(replaces or {})
        self.mode = mode
        self.calls: List[Tuple[List[str], Any]] = []

    def size(self, opp: Opportunity, ctx: Any) -> SizeDecision:
        units = self.per_idea.get(str(opp.idea_id), self.units)
        return SizeDecision(idea_id=str(opp.idea_id), policy=self.name, units=int(units), stake=0.0, mode=self.mode,
                            replaces=self.replaces.get(str(opp.idea_id)))

    def size_all(self, opps: Sequence[Opportunity], ctx: Any) -> Dict[str, SizeDecision]:
        self.calls.append(([str(o.idea_id) for o in opps], ctx))
        return {str(o.idea_id): self.size(o, ctx) for o in opps}

    def explain(self, ctx: Any) -> Dict[str, Any]:
        return {"policy": self.name, "label": self.label, "mode": self.mode, "lines": []}


def bot(pid: str = "bot", kinds: Optional[List[str]] = None, policy: str = "fixed") -> PortfolioSpec:
    return PortfolioSpec(pid, pid, policy, kinds, None, 2.0, 120.0)


def human(pid: str = "human:fixed", kinds: Optional[List[str]] = None) -> PortfolioSpec:
    return PortfolioSpec(pid, pid, "fixed", kinds, None, HUMAN_LATENCY_S, HUMAN_MAX_FILL_DELAY_S)


def make_engine(specs: Optional[Sequence[PortfolioSpec]] = None, policy: Optional[FixedPolicy] = None,
                persistence: Any = None, code_version: str = "test", **kw: Any) -> Tuple[P.PaperEngine, FixedPolicy]:
    pol = policy or FixedPolicy()
    cfg = PaperConfig(portfolios=list(specs or [bot()]), **kw)
    eng = P.PaperEngine(cfg, persistence=persistence if persistence is not None else P.MemoryPaperPersistence(),
                        policies={"fixed": pol, "conservative": pol, "chaser": pol}, clock=lambda: T,
                        code_version=code_version)
    return eng, pol


def bk(eid: str, at: float, bids: Sequence[Tuple[float, float]] = (), asks: Sequence[Tuple[float, float]] = (),
       source: str = "paper") -> BookObservation:
    return BookObservation(eid, at, bids=list(bids), asks=list(asks), source=source)


def mobs(t: float, quotes: Optional[Dict[str, Any]] = None, books: Sequence[BookObservation] = (),
         trades: Optional[Dict[str, List[TradeRecord]]] = None, open_ids: Optional[Sequence[str]] = None,
         settlements: Optional[Dict[str, SettlementInfo]] = None, fair_values: Optional[Dict[str, FairValue]] = None,
         races: Optional[Dict[str, RaceRef]] = None, cup_end: float = CUP) -> MarketObservation:
    """A MarketObservation at ``t``; quotes are ``eid -> (bid, ask)`` or ``(bid, ask, ts)`` (default "1": 0.50/0.52)."""
    q: Dict[str, Quote] = {}
    for eid, v in (quotes if quotes is not None else {"1": (0.50, 0.52)}).items():
        if isinstance(v, Quote):
            q[eid] = v
        else:
            ts = v[2] if len(v) > 2 else t
            q[eid] = Quote(eid, v[0], v[1], None, ts)
    return MarketObservation(
        now=t, cup_end=cup_end, quotes=q, books={b.exchange_id: b for b in books}, trades=dict(trades or {}),
        settlements=dict(settlements or {}), open_ids=set(open_ids) if open_ids is not None else set(q),
        infos={e: ExchangeInfo(e, "m" + e, None, f"Will {e} happen?") for e in q},
        fair_values=dict(fair_values or {}), races=dict(races or {}))


def opp(eid: str = "1", side: str = "yes", limit: float = 0.535, idea: Optional[str] = None, kind: str = "value",
        plan: Optional[ExitPlan] = None, edge: float = 0.05, score: float = 1.0, factor_delta: Optional[float] = 0.05,
        race_key: Optional[str] = None, order_type: str = "taker", expires_at: Optional[float] = None,
        fair_value: Optional[float] = None, bet_kind: str = "binary", entry: float = 0.52) -> Opportunity:
    idea = idea or f"{kind}:x{eid}:{side}"
    return Opportunity(
        kind=kind, exchange_id=eid, market_id="m" + eid, title=f"Will {eid} happen?", option=None, side=side,
        entry_price=entry, target_price=None, stop_price=None, prob_win=0.6, edge=edge, expected_return=0.1,
        horizon_hours=1.0, suggested_shares=0, suggested_cost=0.0, score=score, confidence=0.6,
        rationale=[f"Reason for {idea}", "second line"], idea_id=idea, race_key=race_key,
        bet=BetShape(bet_kind, 0.6, 0.4, 0.5, entry), exit_plan=plan if plan is not None else ExitPlan(kind=kind),
        order_type=order_type, limit_price=limit, expires_at=expires_at, fair_value=fair_value,
        factor_delta=factor_delta)


def hole(eid: str = "1", limit: float = 0.62, expires_at: Optional[float] = None, side: str = "yes",
         idea: Optional[str] = None, plan: Optional[ExitPlan] = None) -> Opportunity:
    return opp(eid, side=side, limit=limit, idea=idea or f"hole:x{eid}:{side}", kind="hole", order_type="maker",
               expires_at=expires_at if expires_at is not None else T + 6 * 3600, bet_kind="fixed", entry=limit,
               plan=plan if plan is not None else ExitPlan(kind="hole"))


def basket(legs: Sequence[Tuple[str, str, float]] = (("1", "no", 0.48), ("2", "no", 0.47)), idea: str = "basket:1+2:nn",
           edge: float = 0.04, min_set_profit: float = 0.005, floor: float = 1.0, bet_kind: str = "riskless",
           plan: Optional[ExitPlan] = None) -> Opportunity:
    total = round(sum(p for _, _, p in legs), 6)
    leg_dicts = [{"exchange_id": e, "market_id": "m" + e, "title": f"Will {e} happen?", "option": None, "side": s,
                  "price": p, "limit": p, "race_key": "2026:GOVERNOR:NV", "party": None, "yes_bid": None, "yes_ask": None,
                  "quote_ts": None} for e, s, p in legs]
    return Opportunity(
        kind="basket", exchange_id=None, market_id="m" + legs[0][0], title="NV Governor NO basket", option=None,
        side=legs[0][1], entry_price=total, target_price=None, stop_price=None, prob_win=1.0, edge=edge,
        expected_return=edge / total, horizon_hours=24.0, suggested_shares=0, suggested_cost=0.0, score=2.0,
        confidence=0.9, rationale=["YES bids sum above 1"], legs=leg_dicts, unit="sets", idea_id=idea,
        race_key="2026:GOVERNOR:NV", bet=BetShape(bet_kind, 1.0, floor - total, 0.0, total, floor=floor),
        exit_plan=plan if plan is not None else ExitPlan(kind="basket", min_set_profit=min_set_profit),
        limit_price=total, factor_delta=0.0)


def book_of(eng: P.PaperEngine, pid: str = "bot") -> Any:
    return eng._books[pid]


def all_orders(eng: P.PaperEngine) -> List[Any]:
    return eng.orders(open_only=False)


def fills_of(eng: P.PaperEngine, pid: Optional[str] = None) -> List[Any]:
    return list(reversed(eng.fills(pid, limit=1000)))  # oldest first


# --------------------------------------------------------------------------- 1. taker fills


def test_taker_book_before_bot_latency_never_fills() -> None:
    eng, _ = make_engine()
    r = eng.step(T, mobs(T), [opp()])
    assert r.orders_created == 1
    (order,) = eng.orders()
    assert order.status == "pending" and order.latency_s == 2.0 and order.decision_touch == 0.52
    # observed 1 s after the decision: earlier than created_at + latency
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 1, asks=[(0.52, 500)])]), [])
    assert eng.fills() == [] and eng.orders()[0].status == "pending"
    eng.step(T + 60, mobs(T + 60, books=[bk("1", T + 59, asks=[(0.52, 500)])]), [])
    (fill,) = eng.fills()
    assert fill.qty == 100 and fill.price == 0.52 and fill.book_at == T + 59 and fill.latency_s == 59


def test_taker_human_latency_waits_four_minutes() -> None:
    eng, _ = make_engine([human()])
    eng.step(T, mobs(T), [opp()])
    assert eng.orders()[0].latency_s == HUMAN_LATENCY_S
    eng.step(T + 210, mobs(T + 210, books=[bk("1", T + 200, asks=[(0.52, 500)])]), [])
    assert eng.fills() == []
    eng.step(T + 250, mobs(T + 250, books=[bk("1", T + 245, asks=[(0.52, 500)])]), [])
    (fill,) = eng.fills()
    assert fill.book_at == T + 245 and fill.latency_s == pytest.approx(245)


def test_taker_first_eligible_book_is_used_even_if_a_later_one_is_better() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.53, 500)])]), [])
    eng.step(T + 60, mobs(T + 60, books=[bk("1", T + 59, asks=[(0.50, 500)])]), [])
    (fill,) = eng.fills()
    assert fill.price == 0.53 and fill.book_at == T + 29
    assert eng.positions()[0].avg_cost == 0.53


def test_taker_walks_depth_to_the_limit_and_cancels_the_rest() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=200))
    eng.step(T, mobs(T), [opp(limit=0.535)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 60), (0.53, 100), (0.54, 1000)])]), [])
    (fill,) = eng.fills()
    assert fill.qty == 160
    assert fill.price == pytest.approx((0.52 * 60 + 0.53 * 100) / 160)
    assert fill.levels == [(0.52, 60.0), (0.53, 100.0)]
    assert fill.slippage == pytest.approx((0.52 * 60 + 0.53 * 100) / 160 - 0.52)
    (order,) = all_orders(eng)
    assert order.status == "cancelled" and order.filled_qty == 160
    assert order.close_reason == "The book had only 160 of 200 shares at 0.535 or better"
    b = book_of(eng)
    assert b.unfilled[-1]["qty"] == 40 and b.unfilled[-1]["decision_touch"] == 0.52
    assert b.reserved_cash == 0.0
    ex = eng.portfolios()[0].execution["value"]
    assert ex["entries"] == 1 and ex["filled"] == 1 and ex["fill_rate"] == 1.0 and ex["unfilled"] == 0
    # what the 40 shares that did NOT fill would show now: 40 x (bid 0.50 - decision touch 0.52)
    assert ex["unfilled_pnl_now"] == pytest.approx(-0.8)
    assert ex["avg_slippage"] == pytest.approx(fill.slippage)


def test_taker_limit_below_the_book_fills_nothing() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp(limit=0.51)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    assert eng.fills() == []
    (order,) = all_orders(eng)
    assert order.status == "cancelled" and order.close_reason.startswith("The book had only 0 of 100 shares at 0.510")
    assert eng.portfolios()[0].execution["value"]["unfilled"] == 1


def _round_trip(eng: P.PaperEngine, t0: float) -> None:
    """Idea A buys 100 (60 @ 0.52 + 40 @ 0.53), then exits at the target."""
    plan = ExitPlan(kind="value", target_bid=0.55)
    eng.step(t0, mobs(t0), [opp(limit=0.53, idea="A", plan=plan)])
    eng.step(t0 + 30, mobs(t0 + 30, books=[bk("1", t0 + 29, asks=[(0.52, 60), (0.53, 100)])]), [])
    eng.step(t0 + 60, mobs(t0 + 60, quotes={"1": (0.56, 0.57)}), [])
    eng.step(t0 + 90, mobs(t0 + 90, quotes={"1": (0.56, 0.57)}, books=[bk("1", t0 + 89, bids=[(0.56, 500)])]), [])
    assert eng.positions() == [] and len(eng.trades()) == 1


def test_consumed_liquidity_is_not_reused_by_a_later_order() -> None:
    eng, _ = make_engine()
    _round_trip(eng, T)
    eng.step(T + 120, mobs(T + 120), [opp(limit=0.54, idea="B")])
    eng.step(T + 150, mobs(T + 150, books=[bk("1", T + 149, asks=[(0.52, 60), (0.53, 100), (0.54, 500)])]), [])
    fill = fills_of(eng)[-1]
    assert fill.idea_id == "B"
    # the 60 at 0.52 and 40 of the 0.53 rung were ours: B gets the other 60 at 0.53, then 0.54
    assert fill.levels == [(0.53, 60.0), (0.54, 40.0)]


def test_consumed_liquidity_matches_a_one_tick_requote() -> None:
    eng, _ = make_engine()
    _round_trip(eng, T)
    eng.step(T + 120, mobs(T + 120), [opp(limit=0.55, idea="B")])
    # the house re-quoted its rungs one tick up: still the liquidity we already took
    eng.step(T + 150, mobs(T + 150, books=[bk("1", T + 149, asks=[(0.525, 60), (0.535, 100), (0.545, 500)])]), [])
    fill = fills_of(eng)[-1]
    taken = dict(fill.levels)
    assert 0.525 not in taken
    assert sum(taken.values()) == 100 and taken.get(0.535, 0) <= 60


def test_consumed_liquidity_is_forgotten_after_the_memory_window() -> None:
    eng, _ = make_engine()
    _round_trip(eng, T)
    t1 = T + 1900  # consumption at T + 30 is older than consumed_memory_s (1800 s)
    eng.step(t1, mobs(t1), [opp(limit=0.54, idea="B")])
    eng.step(t1 + 30, mobs(t1 + 30, books=[bk("1", t1 + 29, asks=[(0.52, 60), (0.53, 100), (0.54, 500)])]), [])
    fill = fills_of(eng)[-1]
    assert fill.levels == [(0.52, 60.0), (0.53, 40.0)]


def test_consumed_memory_is_never_shorter_than_the_cooldown() -> None:
    eng, _ = make_engine(consumed_memory_s=60.0, reentry_cooldown_s=1800.0)
    _round_trip(eng, T)
    eng.step(T + 300, mobs(T + 300), [opp(limit=0.54, idea="B")])
    eng.step(T + 330, mobs(T + 330, books=[bk("1", T + 329, asks=[(0.52, 60), (0.53, 100), (0.54, 500)])]), [])
    assert fills_of(eng)[-1].levels == [(0.53, 60.0), (0.54, 40.0)]


def test_stale_quote_blocks_the_fill_then_the_order_expires() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, quotes={"1": (0.50, 0.52, T - 200)}, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    assert eng.fills() == [] and eng.orders()[0].status == "pending"
    eng.step(T + 130, mobs(T + 130, quotes={"1": (0.50, 0.52, T - 200)}), [])
    assert eng.orders() == []
    (order,) = all_orders(eng)
    assert order.status == "expired"
    assert order.close_reason == "No order book read within 120 s of the decision: not filled"


def test_outcome_missing_from_open_ids_never_fills() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, quotes={"1": (0.50, 0.52), "2": (0.4, 0.5)}, open_ids=["2"],
                         books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    assert eng.fills() == []


def test_expiry_uses_the_portfolios_own_fill_delay() -> None:
    eng, _ = make_engine([bot(), human()])
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 130, mobs(T + 130), [])
    statuses = {o.portfolio_id: o.status for o in all_orders(eng)}
    assert statuses == {"bot": "expired", "human:fixed": "pending"}
    eng.step(T + 300, mobs(T + 300), [])
    assert {o.portfolio_id: o.status for o in all_orders(eng)}["human:fixed"] == "pending"
    eng.step(T + 370, mobs(T + 370), [])
    expired = [o for o in all_orders(eng) if o.portfolio_id == "human:fixed"][0]
    assert expired.status == "expired"
    assert expired.close_reason == "No order book read within 360 s of the decision: not filled"


def test_orders_never_fill_in_the_step_that_created_them() -> None:
    eng, _ = make_engine()
    # a fresh book is in the very observation the decision was made on
    eng.step(T, mobs(T, books=[bk("1", T, asks=[(0.52, 500)])]), [opp()])
    assert eng.fills() == []


def test_partly_filled_exit_never_reuses_its_observation_and_asks_for_a_newer_one() -> None:
    eng, _ = make_engine()
    plan = ExitPlan(kind="value", target_bid=0.55)
    eng.step(T, mobs(T), [opp(plan=plan)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.56, 0.57)}), [])
    q = {"1": (0.56, 0.57)}
    b1 = bk("1", T + 89, bids=[(0.56, 40)])
    eng.step(T + 90, mobs(T + 90, quotes=q, books=[b1]), [])
    assert eng.positions()[0].qty == 60
    plan_reads = eng.wanted_reads(T + 120, mobs(T + 120, quotes=q, books=[b1]))
    (req,) = [r for r in plan_reads.requests if r.priority == 1]
    assert req.after > T + 89  # strictly after the observation it already used
    eng.step(T + 120, mobs(T + 120, quotes=q, books=[b1]), [])  # the same observation again: nothing
    assert eng.positions()[0].qty == 60
    eng.step(T + 150, mobs(T + 150, quotes=q, books=[bk("1", T + 149, bids=[(0.56, 500)])]), [])
    assert eng.positions() == []
    (trade,) = eng.trades()
    assert trade.qty == 100 and trade.exit_reason == "target"


# --------------------------------------------------------------------------- 2. maker fills


def test_queue_ahead_at_cases() -> None:
    cfg = PaperConfig()
    book = bk("1", T - 10, bids=[(0.93, 200), (0.92, 300), (0.90, 100)], asks=[(0.94, 100), (0.95, 50), (0.97, 10)])
    assert P.queue_ahead_at(book, "yes", 0.92, T, cfg) == 300  # the level's quantity at our price
    assert P.queue_ahead_at(book, "yes", 0.91, T, cfg) == 0  # inside the displayed range, no level there
    assert P.queue_ahead_at(book, "yes", 0.94, T, cfg) == 0  # better than the best bid: first in line
    assert P.queue_ahead_at(book, "yes", 0.62, T, cfg) is None  # beyond the last displayed level: unknown
    assert P.queue_ahead_at(book, "no", 0.05, T, cfg) == 50  # NO at 0.05 rests as a YES ask at 0.95
    assert P.queue_ahead_at(book, "no", 0.04, T, cfg) == 0
    assert P.queue_ahead_at(book, "no", 0.02, T, cfg) is None
    assert P.queue_ahead_at(book, "yes", 0.92, T + 200, cfg) is None  # the book is older than 120 s
    assert P.queue_ahead_at(None, "yes", 0.92, T, cfg) is None
    synthetic = bk("1", T - 10, bids=[(0.92, 300)], source="synthetic")
    assert P.queue_ahead_at(synthetic, "yes", 0.92, T, cfg) is None


def _resting(eng: P.PaperEngine, limit: float = 0.62, books: Sequence[BookObservation] = (), **kw: Any) -> Any:
    eng.step(T, mobs(T, quotes={"1": (0.93, 0.94)}, books=books), [hole(limit=limit, **kw)])
    (order,) = eng.orders()
    assert order.status == "resting" and order.order_type == "maker"
    return order


def tr(tid: str, ts: float, price: float, size: float, side: Optional[str] = "NO") -> TradeRecord:
    return TradeRecord(tid, "1", ts, price, size, side)


def test_maker_trade_through_fills_at_the_limit_capped_by_size() -> None:
    eng, _ = make_engine()
    order = _resting(eng)
    assert order.queue_ahead is None and order.reserved_cash == pytest.approx(62.0)
    tape = [tr("a", T + 30, 0.61, 30)]
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.93, 0.94)}, trades={"1": tape}), [hole()])
    (fill,) = eng.fills()
    assert fill.qty == 30 and fill.price == 0.62 and fill.liquidity == "maker" and fill.trade_ids == ["a"]
    assert fill.book_at == T + 30
    tape2 = tape + [tr("b", T + 70, 0.60, 500)]
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.93, 0.94)}, trades={"1": tape2}), [hole()])
    fills = fills_of(eng)
    assert [f.qty for f in fills] == [30, 70]  # dedup by id: "a" is not counted again
    assert all(f.price == 0.62 for f in fills)
    assert all_orders(eng)[0].status == "filled"


def test_maker_ignores_prints_before_placement_and_at_better_prices() -> None:
    eng, _ = make_engine()
    _resting(eng)
    tape = [tr("old", T - 5, 0.50, 100), tr("lat", T + 1, 0.50, 100), tr("above", T + 20, 0.63, 100),
            tr("at", T + 21, 0.62, 100)]  # at the price but the queue is unknown
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.93, 0.94)}, trades={"1": tape}), [hole()])
    assert eng.fills() == []
    order = eng.orders()[0]
    assert set(order.seen_trade_ids) == {"above", "at"} and order.last_trade_ts == T + 21


def test_maker_at_price_prints_reduce_a_known_queue_across_prints_and_steps() -> None:
    eng, _ = make_engine()
    book = bk("1", T - 5, bids=[(0.93, 200), (0.92, 300)], asks=[(0.94, 100)])
    order = _resting(eng, limit=0.92, books=[book])
    assert order.queue_ahead == 300
    q = {"1": (0.93, 0.94)}
    tape = [tr("p1", T + 10, 0.92, 200, "NO"),  # queue 300 -> 100
            tr("p2", T + 11, 0.92, 500, "YES"),  # a buyer lifted an ask: not our side
            tr("p3", T + 12, 0.92, 500, None),  # unknown taker side: does not count
            tr("p4", T + 13, 0.92, 150, "NO")]  # queue 100 -> 0, 50 left, half fills
    eng.step(T + 30, mobs(T + 30, quotes=q, trades={"1": tape}), [hole(limit=0.92)])
    (fill,) = eng.fills()
    assert fill.qty == 25 and fill.trade_ids == ["p4"]
    assert eng.orders()[0].queue_ahead == 0
    eng.step(T + 60, mobs(T + 60, quotes=q, trades={"1": tape + [tr("p5", T + 40, 0.92, 40, "NO")]}), [hole(limit=0.92)])
    assert [f.qty for f in fills_of(eng)] == [25, 20]


def test_maker_unknown_queue_fills_only_on_trade_throughs() -> None:
    eng, _ = make_engine()
    _resting(eng, limit=0.92)  # no book: queue unknown
    tape = [tr("at", T + 10, 0.92, 500, "NO"), tr("thru", T + 11, 0.915, 40, "NO")]
    eng.step(T + 30, mobs(T + 30, quotes={"1": (0.93, 0.94)}, trades={"1": tape}), [hole(limit=0.92)])
    (fill,) = eng.fills()
    assert fill.qty == 40 and fill.trade_ids == ["thru"]


def test_maker_no_order_rests_as_a_yes_ask_and_fills_on_prints_above() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T, quotes={"1": (0.04, 0.06)}), [hole(limit=0.90, side="no")])  # YES ask at 0.10
    tape = [tr("lo", T + 10, 0.09, 50, "YES"), tr("hi", T + 11, 0.11, 30, "YES")]
    eng.step(T + 30, mobs(T + 30, quotes={"1": (0.04, 0.06)}, trades={"1": tape}), [hole(limit=0.90, side="no")])
    (fill,) = eng.fills()
    assert fill.qty == 30 and fill.price == 0.90 and fill.side == "no"


def test_maker_cancel_after_two_missing_steps_still_fills_until_cancel_at() -> None:
    eng, _ = make_engine()
    _resting(eng)
    q = {"1": (0.93, 0.94)}
    eng.step(T + 30, mobs(T + 30, quotes=q), [])
    assert eng.orders()[0].status == "resting" and eng.orders()[0].missing_steps == 1
    eng.step(T + 60, mobs(T + 60, quotes=q), [])
    order = eng.orders()[0]
    assert order.status == "cancelling" and order.cancel_at == T + 60 + 2.0  # the cancel takes the bot's 2-s latency
    # the next tape read (after cancel_at): a print before cancel_at still fills, one after it does not
    tape = [tr("in", T + 50, 0.60, 30), tr("after", T + 70, 0.60, 30)]
    eng.step(T + 90, mobs(T + 90, quotes=q, trades={"1": tape}), [])
    (fill,) = eng.fills()
    assert fill.qty == 30 and fill.trade_ids == ["in"]
    (closed,) = all_orders(eng)
    assert closed.status == "cancelled" and closed.close_reason == "The conditions for this resting order no longer hold"
    assert eng.positions()[0].qty == 30


def test_maker_expiry_closes_after_a_tape_read_past_expires_at() -> None:
    eng, _ = make_engine()
    _resting(eng, expires_at=T + 100)
    q = {"1": (0.93, 0.94)}
    eng.step(T + 120, mobs(T + 120, quotes=q, trades={"1": [tr("late", T + 110, 0.50, 100)]}), [])
    assert eng.fills() == []
    (order,) = all_orders(eng)
    assert order.status == "expired" and order.close_reason == "Expired unfilled" and order.cancel_at == T + 100


def test_maker_cancelling_order_times_out_without_a_tape_read() -> None:
    eng, _ = make_engine()
    _resting(eng, expires_at=T + 100)
    q = {"1": (0.93, 0.94)}
    eng.step(T + 120, mobs(T + 120, quotes=q), [])
    assert eng.orders()[0].status == "cancelling"
    eng.step(T + 230, mobs(T + 230, quotes=q), [])  # cancel_at + max_fill_delay_s (120) passed
    assert eng.orders() == [] and all_orders(eng)[0].status == "expired"


def test_maker_failed_tape_read_gives_no_fill_and_truncation_flags_a_gap() -> None:
    eng, _ = make_engine()
    _resting(eng)
    q = {"1": (0.93, 0.94)}
    eng.step(T + 30, mobs(T + 30, quotes=q), [hole()])  # no tape in the observation: a failed or skipped read
    assert eng.fills() == [] and eng.orders()[0].tape_gap is False
    r = eng.step(T + 60, mobs(T + 60, quotes=q, trades={"1": [tr("x", T + 40, 0.70, 10)]}), [hole()],
                 tape_truncated=["1"])
    assert r.tape_gaps == 1 and eng.orders()[0].tape_gap is True


def test_resting_orders_request_tape_and_refresh_books() -> None:
    eng, _ = make_engine()
    _resting(eng)
    plan = eng.wanted_reads(T + 70, mobs(T + 70, quotes={"1": (0.93, 0.94)}))
    kinds = [(r.kind, r.priority) for r in plan.requests]
    assert ("trades", 3) in kinds and ("book", 5) in kinds
    tape_req = [r for r in plan.requests if r.kind == "trades"][0]
    assert tape_req.since == T
    assert eng.resting_exchanges() == {"1"}


# --------------------------------------------------------------------------- 3. sets


NV = {"1": (0.52, 0.54), "2": (0.53, 0.55)}  # YES bids sum to 1.05: NO asks 0.48 + 0.47 = 0.95


def _set_books(t: float, b1: float = 300, b2: float = 500) -> List[BookObservation]:
    return [bk("1", t, bids=[(0.52, b1)], asks=[(0.54, 1000)]), bk("2", t, bids=[(0.53, b2)], asks=[(0.55, 1000)])]


def test_set_entry_is_deferred_until_every_leg_has_a_fresh_book() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=1000))
    r = eng.step(T, mobs(T, quotes=NV), [basket()])
    assert r.sets_deferred == 1 and r.orders_created == 0
    assert book_of(eng).deferred == ["basket:1+2:nn"]
    plan = eng.wanted_reads(T + 30, mobs(T + 30, quotes=NV))
    reqs = [r for r in plan.requests if r.priority == 2]
    assert {r.exchange_id for r in reqs} == {"1", "2"} and len({r.group for r in reqs}) == 1 and reqs[0].group
    # one leg fresh, the other 2 minutes old: still deferred
    stale = [bk("1", T + 29, bids=[(0.52, 300)]), bk("2", T - 90, bids=[(0.53, 500)])]
    assert eng.step(T + 30, mobs(T + 30, quotes=NV, books=stale), [basket()]).sets_deferred == 1
    # both fresh: entered, units capped by the thinner leg (300 sets on offer up to the limits)
    r = eng.step(T + 60, mobs(T + 60, quotes=NV, books=_set_books(T + 59)), [basket()])
    assert r.orders_created == 2 and r.sets_deferred == 0
    orders = eng.orders()
    assert {o.exchange_id: o.qty for o in orders} == {"1": 300, "2": 300}
    assert len({o.group_id for o in orders}) == 1 and orders[0].group_id == "bot:b1"
    assert {o.exchange_id: o.limit_price for o in orders} == {"1": 0.48, "2": 0.47}
    assert sum(o.reserved_cash for o in orders) == pytest.approx(300 * 0.95)


def _enter_set(eng: P.PaperEngine, b2_fill: float = 300) -> None:
    eng.step(T, mobs(T, quotes=NV, books=_set_books(T - 1)), [basket()])
    eng.step(T + 30, mobs(T + 30, quotes=NV, books=_set_books(T + 29, b2=b2_fill)), [])


def test_set_both_legs_fill() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    pos = {p.exchange_id: p for p in eng.positions()}
    assert {e: p.qty for e, p in pos.items()} == {"1": 300, "2": 300}
    assert pos["1"].basket_id == pos["2"].basket_id == "bot:b1"
    assert pos["1"].avg_cost == 0.48 and pos["2"].avg_cost == 0.47
    assert pos["1"].edge_per_unit == pytest.approx(0.02)  # the set's edge 0.04 split over 2 legs
    assert pos["1"].floor_per_unit == pytest.approx(0.5)
    assert eng.orders() == []


def test_set_legging_residue_is_sold_and_recorded_as_a_legging_trade() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng, b2_fill=200)
    pos = {p.exchange_id: p.qty for p in eng.positions()}
    assert pos == {"1": 300, "2": 200}
    (exit_order,) = eng.orders()
    assert exit_order.exchange_id == "1" and exit_order.purpose == "exit" and exit_order.qty == 100
    assert exit_order.limit_price == 0.005 and exit_order.reason == "Legging: the other legs filled only 200 sets"
    # selling NO hits the YES asks (NO bid = 1 - YES ask = 0.46)
    eng.step(T + 60, mobs(T + 60, quotes=NV, books=[bk("1", T + 59, bids=[(0.52, 1)], asks=[(0.54, 1000)])]), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "legging" and trade.qty == 100
    assert trade.cost == pytest.approx(48.0) and trade.proceeds == pytest.approx(46.0) and trade.pnl == pytest.approx(-2.0)
    assert {p.exchange_id: p.qty for p in eng.positions()} == {"1": 200, "2": 200}


def test_basket_exit_needs_depth_walked_proceeds_not_just_the_touch() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    q = {"1": (0.495, 0.505), "2": (0.495, 0.505)}  # touch: NO bids 0.495 + 0.495 = 0.99 per set vs cost 0.95
    thin = [bk("1", T + 59, bids=[(0.495, 1000)], asks=[(0.505, 100), (0.60, 1000)]),
            bk("2", T + 59, bids=[(0.495, 1000)], asks=[(0.505, 1000)])]
    r = eng.step(T + 60, mobs(T + 60, quotes=q, books=thin), [])
    assert r.exits_started == 0 and eng.orders() == []  # depth: 278 vs cost 285
    deep = [bk("1", T + 89, asks=[(0.505, 1000)]), bk("2", T + 89, asks=[(0.505, 1000)])]
    r = eng.step(T + 90, mobs(T + 90, quotes=q, books=deep), [])
    orders = eng.orders()
    assert r.exits_started == 1 and len(orders) == 2 and {o.limit_price for o in orders} == {0.495}
    assert all(o.reason == "The price converged: selling banks the edge" for o in orders)
    eng.step(T + 120, mobs(T + 120, quotes=q, books=[bk("1", T + 119, asks=[(0.505, 1000)]),
                                                      bk("2", T + 119, asks=[(0.505, 1000)])]), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "converged" and trade.qty == 300 and len(trade.legs) == 2
    assert trade.pnl == pytest.approx(300 * (0.99 - 0.95))
    assert trade.direction == "N"


def test_basket_exit_without_books_requests_the_group_and_decides_next_step() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    q = {"1": (0.495, 0.505), "2": (0.495, 0.505)}
    eng.step(T + 2000, mobs(T + 2000, quotes=q), [])  # last books are > 900 s old
    assert eng.orders() == []
    plan = eng.wanted_reads(T + 2030, mobs(T + 2030, quotes=q))
    reqs = [r for r in plan.requests if r.priority == 2]
    assert {r.exchange_id for r in reqs} == {"1", "2"} and len({r.group for r in reqs}) == 1


def test_basket_legging_out_force_sells_the_rest() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    q = {"1": (0.495, 0.505), "2": (0.495, 0.505)}
    deep = [bk("1", T + 89, asks=[(0.505, 1000)]), bk("2", T + 89, asks=[(0.505, 1000)])]
    eng.step(T + 90, mobs(T + 90, quotes=q, books=deep), [])
    assert len(eng.orders()) == 2
    eng.step(T + 120, mobs(T + 120, quotes=q, books=[bk("1", T + 119, asks=[(0.505, 1000)])]), [])  # leg 1 sells
    assert {p.exchange_id for p in eng.positions()} == {"2"}
    eng.step(T + 240, mobs(T + 240, quotes=q), [])  # leg 2's exit expired: force it out
    (forced,) = eng.orders()
    assert forced.exchange_id == "2" and forced.limit_price == 0.005
    assert forced.reason == "Legging out: selling every remaining leg"
    eng.step(T + 270, mobs(T + 270, quotes=q, books=[bk("2", T + 269, asks=[(0.60, 1000)])]), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "legging" and trade.pnl == pytest.approx(300 * (0.495 + 0.40 - 0.95))


def test_basket_settlement_pays_every_leg() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    settle = {"1": SettlementInfo("1", "m1", "YES", T + 50, 1.0, False, T + 55),
              "2": SettlementInfo("2", "m2", "NO", T + 50, 0.0, False, T + 55)}
    r = eng.step(T + 60, mobs(T + 60, quotes={}, open_ids=["x"], settlements=settle), [])
    assert r.settled == 2 and eng.positions() == []
    (trade,) = eng.trades()
    assert trade.exit_reason == "settled" and trade.proceeds == pytest.approx(300.0)
    assert trade.pnl == pytest.approx(300 * 0.05)
    settles = [f for f in eng.fills() if f.action == "settle"]
    assert {f.exchange_id: f.price for f in settles} == {"1": 0.0, "2": 1.0}
    assert all(f.liquidity == "settlement" and f.purpose == "settlement" for f in settles)


# --------------------------------------------------------------------------- 4. exits


def _hold(eng: P.PaperEngine, plan: ExitPlan, side: str = "yes", fair_value: Optional[float] = None,
          idea: Optional[str] = None) -> None:
    """Enter 100 shares of exchange "1" (YES at 0.52, or NO at 0.48) and fill at T + 30."""
    limit = 0.535 if side == "yes" else 0.49
    eng.step(T, mobs(T), [opp(plan=plan, side=side, limit=limit, fair_value=fair_value, idea=idea)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.52, 500)], asks=[(0.52, 500)])]), [])
    (pos,) = eng.positions()
    assert pos.qty == 100


def _exit_after(eng: P.PaperEngine, t: float, quotes: Dict[str, Any], **kw: Any) -> Any:
    r = eng.step(t, mobs(t, quotes=quotes, **kw), [])
    exits = [o for o in eng.orders() if o.purpose == "exit"]
    return r, exits


def test_exit_regime_rule_first_with_a_floor_limit() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", exit_before_ts=T + 50, stop_bid=0.45, target_bid=0.55))
    r, (o,) = _exit_after(eng, T + 60, {"1": (0.44, 0.46)})  # the stop would fire too: regime wins
    assert r.exits_started == 1
    assert o.limit_price == 0.005 and o.planned_price == 0.44 and o.qty == 100 and o.decision_touch == 0.44
    assert o.reason == "Flat before the Cup's closeout window (settlement rule)"
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.44, 0.46)}, books=[bk("1", T + 89, bids=[(0.44, 500)])]), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "regime"


def test_exit_stop_rule() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", stop_bid=0.45, target_bid=0.55, time_stop_ts=T + 50))
    _, (o,) = _exit_after(eng, T + 60, {"1": (0.44, 0.46)})  # stop before time
    assert o.limit_price == 0.005 and o.planned_price == 0.45 and o.reason == "The bid fell to the stop"


def test_exit_target_rule_limit_one_tick_below_and_exit_slippage() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", stop_bid=0.45, target_bid=0.55))
    _, (o,) = _exit_after(eng, T + 60, {"1": (0.56, 0.57)})
    assert o.limit_price == 0.545 and o.planned_price == 0.55 and o.reason == "The bid reached the target"
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.56, 0.57)}, books=[bk("1", T + 89, bids=[(0.555, 30), (0.545, 500)])]), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "target"
    avg_exit = (0.555 * 30 + 0.545 * 70) / 100
    ex = eng.portfolios()[0].execution["value"]
    assert ex["exits"] == 1 and ex["avg_exit_slippage"] == pytest.approx(0.55 - avg_exit)


def test_exit_time_rules_and_stale_quotes() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", stop_bid=0.45, time_stop_ts=T + 3600))
    # stale quote: the stop cannot fire (the bid is unknown) ...
    _, exits = _exit_after(eng, T + 60, {"1": (0.40, 0.46, T - 300)})
    assert exits == []
    # ... but the time rule still does
    _, (o,) = _exit_after(eng, T + 3600, {"1": (0.40, 0.46, T - 300)})
    assert o.reason == "Time stop" and o.limit_price == 0.005 and o.planned_price is None
    eng2, _ = make_engine()
    _hold(eng2, ExitPlan(kind="hole", time_stop_after_fill_s=100))
    assert _exit_after(eng2, T + 120, {"1": (0.50, 0.52)})[1] == []
    _, (o2,) = _exit_after(eng2, T + 130, {"1": (0.50, 0.52)})  # first fill at T + 30
    assert o2.reason == "Time stop"


def test_exit_dynamic_fair_value_target() -> None:
    plan = ExitPlan(kind="value", dynamic_fv_target=True, exit_buffer=0.005, target_bid=0.70)

    def run(fv_now: float, as_of: float) -> Any:
        eng, _ = make_engine()
        _hold(eng, plan, fair_value=0.58)
        fv = {"1": FairValue("1", fv_now, "polymarket", usable=True, as_of=as_of)}
        return _exit_after(eng, T + 60, {"1": (0.59, 0.61)}, fair_values=fv)[1]

    (moved,) = run(0.60, T + 55)  # target = floor_tick(0.60 - 0.01 - 0.005) = 0.585
    assert moved.planned_price == 0.585 and moved.limit_price == 0.58
    assert moved.reason == "The bid reached the fair-value target (the fair value moved)"
    (same,) = run(0.58, T + 55)  # target 0.565, the fair value did not move: the Cup converged
    assert same.planned_price == 0.565 and same.reason == "The price converged: selling banks the edge"
    assert run(0.60, T - 100) == []  # a stale fair value is not used: the plan's own target (0.70) applies


def test_exit_one_order_only() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", target_bid=0.55))
    _exit_after(eng, T + 60, {"1": (0.56, 0.57)})
    r, exits = _exit_after(eng, T + 90, {"1": (0.56, 0.57)})
    assert r.exits_started == 0 and len(exits) == 1


def test_exit_replaced_by_a_chaser_decision() -> None:
    pol = FixedPolicy(replaces={"value:x2:yes": "value:x1:yes"})
    eng, _ = make_engine(policy=pol)
    _hold(eng, ExitPlan(kind="value"))
    q = {"1": (0.50, 0.52), "2": (0.40, 0.42)}
    r = eng.step(T + 60, mobs(T + 60, quotes=q), [opp("2")])
    assert r.orders_created == 0  # the new idea waits until the held one is sold
    assert book_of(eng).replace == ["value:x1:yes"]
    assert book_of(eng).diagnostics["kinds"]["value"]["blocked"]["replace"] == 1
    eng.step(T + 90, mobs(T + 90, quotes=q), [])
    (o,) = eng.orders()
    assert o.exchange_id == "1" and o.purpose == "exit" and o.limit_price == 0.495
    assert o.reason == "Replaced by a better idea (chaser top-k)"
    eng.step(T + 120, mobs(T + 120, quotes=q, books=[bk("1", T + 119, bids=[(0.50, 500)])]), [])
    assert eng.trades()[0].exit_reason == "replaced" and book_of(eng).replace == []


# --------------------------------------------------------------------------- 5. settlement, frozen outcomes, Cup end


def _settle(payout: Optional[float], refund: bool = False) -> Dict[str, SettlementInfo]:
    return {"1": SettlementInfo("1", "m1", "YES" if payout == 1 else "NO", T + 40, payout, refund, T + 50)}


@pytest.mark.parametrize("side,payout,price,pnl", [
    ("yes", 1.0, 1.0, 48.0), ("yes", 0.0, 0.0, -52.0), ("no", 0.0, 1.0, 52.0), ("no", 1.0, 0.0, -48.0)])
def test_settlement_pays_yes_and_no(side: str, payout: float, price: float, pnl: float) -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="settle", hold_to_resolution=True), side=side)
    r = eng.step(T + 60, mobs(T + 60, quotes={}, open_ids=["x"], settlements=_settle(payout)), [])
    assert r.settled == 1 and eng.positions() == []
    (trade,) = eng.trades()
    assert trade.exit_reason == "settled" and trade.pnl == pytest.approx(pnl)
    settle = [f for f in eng.fills() if f.action == "settle"][0]
    assert settle.price == price and settle.liquidity == "settlement" and settle.purpose == "settlement"
    assert eng.portfolios()[0].cash == pytest.approx(100_000 + pnl)


def test_settlement_refund_returns_the_cost() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="settle"))
    eng.step(T + 60, mobs(T + 60, quotes={}, open_ids=["x"], settlements=_settle(None, refund=True)), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "refund" and trade.pnl == pytest.approx(0.0)
    assert eng.portfolios()[0].cash == pytest.approx(100_000)


def test_closed_without_a_ruling_freezes_counts_zero_and_is_named() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", target_bid=0.55))
    last = eng.positions()[0].liq_value
    assert last == pytest.approx(50.0)  # 100 shares at the touch 0.50 (no bid depth seen: "unknown")
    unknown = {"1": SettlementInfo("1", "m1", "SOMETHING ODD", T + 40, None, False, T + 50)}
    eng.step(T + 60, mobs(T + 60, quotes={"2": (0.4, 0.5)}, open_ids=["2"], settlements=unknown), [])
    (pos,) = eng.positions()
    assert pos.status == "frozen" and "closed_no_ruling" in pos.flags
    s = eng.portfolios()[0]
    assert s.equity_liq == pytest.approx(100_000 - 52.0) and s.unvalued == pytest.approx(last)
    assert s.positions_liq == 0.0
    assert s.verdict.unvalued_positions == 1
    assert " 1 position whose market closed without a ruling is left out (last value 50)." in s.verdict.sentence
    assert eng.ideas("bot")[0].frozen is True
    # frozen: no exits even when the target would fire; it unfreezes when the outcome reappears
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.56, 0.57)}), [])
    assert eng.positions()[0].status == "open" and "closed_no_ruling" not in eng.positions()[0].flags


def test_settlements_run_before_fills() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])], settlements=_settle(1.0)), [])
    assert eng.fills() == [] and eng.positions() == []
    (order,) = all_orders(eng)
    assert order.status == "cancelled" and order.close_reason == "The outcome settled"


def test_cup_end_stops_entries_exits_and_orders() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", target_bid=0.55))
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.50, 0.52), "2": (0.4, 0.42)}), [opp("2")])
    assert len(eng.orders()) == 1  # an entry on "2" is pending
    late = CUP + 10
    r = eng.step(late, mobs(late, quotes={"1": (0.60, 0.61), "2": (0.4, 0.42)}, cup_end=CUP), [opp("3")])
    assert r.orders_created == 0 and r.exits_started == 0 and eng.orders() == []
    (cancelled,) = [o for o in all_orders(eng) if o.exchange_id == "2"]
    assert cancelled.status == "cancelled" and cancelled.close_reason == "The Cup ended"
    (pos,) = eng.positions()
    assert "post_cup" in pos.flags and pos.liq_value == pytest.approx(50.0)  # keeps its last value
    assert book_of(eng).diagnostics["kinds"]["value"]["blocked"]["cup_end"] == 1


# --------------------------------------------------------------------------- 6. valuation


def test_liquidation_value_fresh_walk_beyond_depth_is_worthless() -> None:
    q = Quote("1", 0.50, 0.52, None, T)
    book = bk("1", T - 10, bids=[(0.50, 60), (0.49, 30)])
    value, state = P.liquidation_value("yes", 100, book, q, now=T)
    assert state == "fresh" and value == pytest.approx(60 * 0.50 + 30 * 0.49)


def test_liquidation_value_stale_book_shifted_to_the_touch() -> None:
    q = Quote("1", 0.50, 0.52, None, T)
    old = bk("1", T - 1200, bids=[(0.55, 60), (0.54, 100)])
    value, state = P.liquidation_value("yes", 100, None, q, last_real_book=old, now=T)
    assert state == "stale" and value == pytest.approx(60 * 0.50 + 40 * 0.49)
    # an inconsistent fresh book (best bid 5 ticks above the touch) is not "fresh"
    inconsistent = bk("1", T - 10, bids=[(0.525, 60), (0.52, 100)])
    value2, state2 = P.liquidation_value("yes", 100, inconsistent, q, now=T)
    assert state2 == "stale" and value2 == pytest.approx(60 * 0.50 + 40 * 0.495)


def test_liquidation_value_unknown_depth_rule_and_no_bid() -> None:
    q = Quote("1", 0.50, 0.52, None, T)
    value, state = P.liquidation_value("yes", 1000, None, q, now=T)
    assert state == "unknown" and value == pytest.approx(100 * 0.50 + 900 * 0.25)
    too_old = bk("1", T - 4000, bids=[(0.50, 5000)])
    assert P.liquidation_value("yes", 1000, None, q, last_real_book=too_old, now=T) == (pytest.approx(275.0), "unknown")
    synthetic = bk("1", T - 10, bids=[(0.50, 5000)], source="synthetic")
    assert P.liquidation_value("yes", 1000, synthetic, q, now=T)[1] == "unknown"  # never marks
    assert P.liquidation_value("yes", 10, None, Quote("1", None, 0.52, None, T), now=T) == (0.0, "unknown")
    fresh = bk("1", T - 10, bids=[(0.50, 5000)])
    assert P.liquidation_value("yes", 10, fresh, Quote("1", None, 0.52, None, T), now=T) == (0.0, "unknown")
    assert P.liquidation_value("yes", 10, fresh, None, now=T) == (pytest.approx(5.0), "fresh")  # no quote at all


def test_liquidation_value_no_side_and_mid_value() -> None:
    q = Quote("1", 0.50, 0.52, None, T)
    book = bk("1", T - 10, asks=[(0.52, 50)])
    assert P.liquidation_value("no", 50, book, q, now=T) == (pytest.approx(24.0), "fresh")
    assert P.mid_value("yes", 100, q) == pytest.approx(51.0)
    assert P.mid_value("no", 100, q) == pytest.approx(49.0)
    assert P.mid_value("yes", 100, None) is None


def test_engine_marks_depth_shares_references_and_drawdown() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])  # no bid depth seen yet
    fv = {"1": FairValue("1", 0.60, "polymarket", usable=True, as_of=T + 60)}
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.40, 0.42)}, fair_values=fv), [])
    (pos,) = eng.positions()
    assert pos.depth_state == "unknown" and "depth_unknown" in pos.flags
    assert pos.liq_value == pytest.approx(40.0) and pos.mark_value == pytest.approx(41.0)
    assert pos.fv_value == pytest.approx(60.0)
    s = eng.portfolios()[0]
    assert s.equity_liq == pytest.approx(100_000 - 52 + 40)
    assert s.equity_mark == pytest.approx(100_000 - 52 + 41) and s.equity_fv == pytest.approx(100_000 - 52 + 60)
    assert s.depth_unknown_share == pytest.approx(40 / s.equity_liq, abs=1e-6)
    assert s.unrealized_pnl_liq == pytest.approx(40 - 52)
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.50, 0.52)},
                          books=[bk("1", T + 89, bids=[(0.50, 50), (0.49, 50)], asks=[(0.52, 50)])]), [])
    pos = eng.positions()[0]
    assert pos.depth_state == "fresh" and pos.liq_value == pytest.approx(25 + 24.5)
    s = eng.portfolios()[0]
    assert s.max_drawdown_abs == pytest.approx(12.0) and s.max_drawdown == pytest.approx(12.0 / 100_000)
    assert s.depth_unknown_share == 0.0
    # a stale book (15-60 min old) shifted to the touch
    eng.step(T + 1200, mobs(T + 1200, quotes={"1": (0.48, 0.50)}), [])
    pos = eng.positions()[0]
    assert pos.depth_state == "stale" and pos.liq_value == pytest.approx(50 * 0.48 + 50 * 0.47)
    assert eng.portfolios()[0].depth_stale_share == pytest.approx(pos.liq_value / eng.portfolios()[0].equity_liq, abs=1e-6)


# --------------------------------------------------------------------------- 7. accounting


def test_reservation_at_the_limit_and_release_on_fill() -> None:
    eng, _ = make_engine()
    eng.step(T, mobs(T), [opp(limit=0.535)])
    b = book_of(eng)
    assert b.reserved_cash == pytest.approx(53.5) and b.free_cash() == pytest.approx(100_000 - 53.5)
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    assert b.reserved_cash == 0.0 and b.cash == pytest.approx(100_000 - 52.0)


def test_free_cash_and_cash_never_go_negative() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=10 ** 7))
    q = {"1": (0.50, 0.52), "2": (0.40, 0.42)}
    eng.step(T, mobs(T, quotes=q), [opp("1", limit=0.535, score=2.0), opp("2", limit=0.50)])
    b = book_of(eng)
    (order,) = eng.orders()
    assert order.exchange_id == "1" and order.qty == math.floor(100_000 / 0.535)
    assert b.free_cash() >= 0 and b.diagnostics["kinds"]["value"]["blocked"]["cash"] == 1
    eng.step(T + 30, mobs(T + 30, quotes=q, books=[bk("1", T + 29, asks=[(0.52, 10 ** 7)])]), [])
    assert b.cash >= 0 and b.reserved_cash == 0.0
    assert b.cash == pytest.approx(100_000 - math.floor(100_000 / 0.535) * 0.52)


def test_all_collateral_advance_and_repayment() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300), all_collateral=True)
    eng.step(T, mobs(T, quotes=NV, books=_set_books(T - 1)), [basket()])
    b = book_of(eng)
    assert b.reserved_cash == 0.0  # a riskless set below its floor needs no cash with ALL collateral
    eng.step(T + 30, mobs(T + 30, quotes=NV, books=_set_books(T + 29)), [])
    pos = {p.exchange_id: p for p in eng.positions()}
    assert b.cash == pytest.approx(100_000.0)
    assert pos["1"].collateral_advance == pytest.approx(144.0) and pos["2"].collateral_advance == pytest.approx(141.0)
    s = eng.portfolios()[0]
    assert s.equity_liq == pytest.approx(b.cash + s.positions_liq - 285.0)
    settle = {"1": SettlementInfo("1", "m1", "YES", T + 50, 1.0, False, T + 55),
              "2": SettlementInfo("2", "m2", "NO", T + 50, 0.0, False, T + 55)}
    eng.step(T + 60, mobs(T + 60, quotes={}, open_ids=["x"], settlements=settle), [])
    assert b.cash == pytest.approx(100_000 + 15.0)  # 300 paid, the 285 advance repaid
    assert eng.trades()[0].pnl == pytest.approx(15.0)


def test_partial_exit_realizes_pnl_and_keeps_the_average_cost() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value", target_bid=0.55))
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.56, 0.57)}), [])
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.56, 0.57)}, books=[bk("1", T + 89, bids=[(0.56, 40)])]), [])
    (pos,) = eng.positions()
    assert pos.qty == 60 and pos.avg_cost == 0.52 and pos.cost == pytest.approx(31.2)
    assert pos.realized_pnl == pytest.approx(1.6)
    s = eng.portfolios()[0]
    assert s.realized_pnl == pytest.approx(1.6)
    assert s.unrealized_pnl_liq == pytest.approx(pos.liq_value - 31.2)
    assert s.pnl_liq == pytest.approx(s.equity_liq - 100_000)


def test_closed_trade_fields_and_profit_per_capital_day() -> None:
    eng, _ = make_engine()
    _round_trip(eng, T)
    (t,) = eng.trades()
    assert t.trade_id == "bot:t1" and t.cost == pytest.approx(0.52 * 60 + 0.53 * 40)
    assert t.proceeds == pytest.approx(56.0) and t.pnl == pytest.approx(56.0 - t.cost)
    assert t.return_pct == pytest.approx(t.pnl / t.cost, abs=1e-6)
    assert t.hold_hours == pytest.approx(60 / 3600, abs=1e-6)
    assert t.profit_per_capital_day == pytest.approx(t.pnl / (t.cost / 24.0), rel=1e-4)
    assert t.entered_at == T and t.opened_at == T + 30 and t.closed_at == T + 90
    assert t.direction == "D" and t.race_key == "market:m1"
    assert t.legs[0]["avg_exit"] == pytest.approx(0.56)


def test_sizing_context_expected_profit_and_delta_weighted_tilt() -> None:
    pol = FixedPolicy(units=300, per_idea={"value:x3:yes": 200})
    eng, _ = make_engine(policy=pol)
    q = dict(NV, **{"3": (0.40, 0.42), "4": (0.30, 0.32)})
    eng.step(T, mobs(T, quotes=q, books=_set_books(T - 1)), [basket(), opp("3", limit=0.43, factor_delta=-0.02)])
    eng.step(T + 30, mobs(T + 30, quotes=q, books=_set_books(T + 29)), [])
    # a single-outcome value position on "1" is impossible (the basket holds it): use a pending entry on 3 only
    eng.step(T + 60, mobs(T + 60, quotes=q), [opp("4", limit=0.33)])
    ctx = pol.calls[-1][1]
    # the basket legs carry edge / 2 each: 300 x 0.02 x 2 legs = 12 (a basket's edge is counted once)
    assert ctx.expected_profit == pytest.approx(12.0)
    exp = ctx.exposure
    # set legs are excluded from the tilt; the pending entry on "3" counts qty x delta (200 x -0.02)
    assert exp.national_tilt_d == pytest.approx(-4.0)
    assert exp.gross_cost == pytest.approx(285.0 + 200 * 0.43)
    assert exp.by_race["2026:GOVERNOR:NV"] == pytest.approx(285.0)
    assert ctx.equity == pytest.approx(eng.portfolios()[0].equity_liq) and ctx.free_cash >= 0


def test_open_position_tilt_counts_qty_times_delta() -> None:
    pol = FixedPolicy()
    eng, _ = make_engine(policy=pol)
    _hold(eng, ExitPlan(kind="value"))
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.50, 0.52), "2": (0.4, 0.42)}), [opp("2", limit=0.43)])
    assert pol.calls[-1][1].exposure.national_tilt_d == pytest.approx(100 * 0.05)
    assert eng.portfolios()[0].swing_risk == pytest.approx(abs(100 * 0.05 + 100 * 0.05) * 3.0)


# --------------------------------------------------------------------------- 8. portfolios


def test_portfolios_are_independent_on_the_same_book() -> None:
    eng, _ = make_engine([bot("a"), bot("b")])
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 100)])]), [])
    assert sorted((f.portfolio_id, f.qty) for f in eng.fills()) == [("a", 100), ("b", 100)]


def test_per_portfolio_latency_fills_from_different_books() -> None:
    eng, _ = make_engine([human(), bot()])
    eng.step(T, mobs(T), [opp()])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    eng.step(T + 250, mobs(T + 250, books=[bk("1", T + 245, asks=[(0.53, 500)])]), [])
    fills = {f.portfolio_id: f for f in eng.fills()}
    assert fills["bot"].price == 0.52 and fills["bot"].book_at == T + 29
    assert fills["human:fixed"].price == 0.53 and fills["human:fixed"].book_at == T + 245


def test_kind_filters() -> None:
    eng, _ = make_engine([bot("v", kinds=["value"]), bot("h", kinds=["hole"])])
    q = {"1": (0.50, 0.52), "2": (0.93, 0.94)}
    eng.step(T, mobs(T, quotes=q), [opp("1"), hole("2"), opp("1", kind="watch", idea="watch:1")])
    assert {(o.portfolio_id, o.kind) for o in eng.orders()} == {("v", "value"), ("h", "hole")}


def test_one_net_position_per_exchange_and_held_ideas() -> None:
    eng, _ = make_engine()
    signals = [opp("1", score=2.0), opp("1", side="no", kind="fade", idea="fade:x1:no", limit=0.49)]
    r = eng.step(T, mobs(T), signals)
    assert r.orders_created == 1 and eng.orders()[0].idea_id == "value:x1:yes"
    assert book_of(eng).diagnostics["kinds"]["fade"]["blocked"]["exchange"] == 1
    eng.step(T + 10, mobs(T + 10), [opp("1", score=2.0)])
    assert book_of(eng).diagnostics["kinds"]["value"]["blocked"]["held"] == 1


def test_cooldown_after_an_idea_closes() -> None:
    eng, _ = make_engine()
    _round_trip(eng, T)  # idea "A" closed at T + 90
    plan = ExitPlan(kind="value", target_bid=0.55)
    assert eng.step(T + 120, mobs(T + 120), [opp(limit=0.53, idea="A", plan=plan)]).orders_created == 0
    assert book_of(eng).diagnostics["kinds"]["value"]["blocked"]["cooldown"] == 1
    assert eng.step(T + 90 + 1801, mobs(T + 90 + 1801), [opp(limit=0.53, idea="A", plan=plan)]).orders_created == 1


def test_caps_on_open_positions_and_new_orders() -> None:
    q = {"1": (0.50, 0.52), "2": (0.40, 0.42), "3": (0.30, 0.32)}
    eng, _ = make_engine(max_new_orders_per_step=1)
    r = eng.step(T, mobs(T, quotes=q), [opp("1", score=1.0), opp("2", score=3.0, limit=0.43)])
    assert r.orders_created == 1 and eng.orders()[0].exchange_id == "2"  # best score first
    assert book_of(eng).diagnostics["kinds"]["value"]["blocked"]["order_cap"] == 1
    eng2, _ = make_engine(max_open_positions=1)
    eng2.step(T, mobs(T, quotes=q), [opp("1")])
    eng2.step(T + 30, mobs(T + 30, quotes=q, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [])
    assert eng2.step(T + 60, mobs(T + 60, quotes=q), [opp("2", limit=0.43)]).orders_created == 0
    assert book_of(eng2).diagnostics["kinds"]["value"]["blocked"]["max_positions"] == 1


def test_chaser_mode_memory_reaches_the_next_sizing_and_the_state() -> None:
    pol = FixedPolicy(mode="chase")
    eng, _ = make_engine(policy=pol)
    q = {"1": (0.50, 0.52), "2": (0.40, 0.42)}
    eng.step(T, mobs(T, quotes=q), [opp("1")])
    assert pol.calls[0][1].prev_mode is None
    assert book_of(eng).mode == {"prev_mode": "chase", "mode_since": T}
    eng.step(T + 30, mobs(T + 30, quotes=q), [opp("2", limit=0.43)])
    ctx = pol.calls[-1][1]
    assert ctx.prev_mode == "chase" and ctx.mode_since == T
    assert eng.state()["portfolios"]["bot"]["mode"] == {"prev_mode": "chase", "mode_since": T}


# --------------------------------------------------------------------------- 9. verdict


def _ideas(n: int, pnl: Any, races: int = 25, blocks: int = 25, closed: bool = True, cost: float = 100.0,
           start: float = T) -> List[P.IdeaOutcome]:
    out = []
    for i in range(n):
        out.append(P.IdeaOutcome(idea_id=f"i{i}", kind="value", race_key=f"R{i % races}",
                                 entered_at=start + (i % blocks) * P.TIME_BLOCK_S, direction="D" if i % 2 == 0 else "R",
                                 closed=closed, pnl=float(pnl(i)), cost=cost))
    return out


def _noise(i: int) -> float:
    return ((i * 37) % 11 - 5) * 4.0


def _verdict(ideas: Sequence[P.IdeaOutcome], pnl_liq: Optional[float] = None, hours: float = 24.0,
             days: Optional[Dict[str, float]] = None, **kw: Any) -> Any:
    args: Dict[str, Any] = dict(
        pnl_liq=sum(i.pnl for i in ideas) if pnl_liq is None else pnl_liq, covered_hours=hours, wall_hours=hours,
        day_hours=days if days is not None else {"2026-09-21": 10.0, "2026-09-22": 14.0}, max_drawdown=0.01,
        closed_trades=[], exploratory=False, n_portfolios=9, depth_unknown_share=0.0, swing_share=0.0,
        unvalued_positions=0, unvalued_value=0.0)
    args.update(kw)
    return P.compute_verdict("human:conservative", ideas, **args)


def test_t_quantile_matches_table_values() -> None:
    assert P.t_quantile(0.95, 7) == pytest.approx(1.895, abs=1e-3)
    assert P.t_quantile(0.95, 19) == pytest.approx(1.729, abs=1e-3)
    assert P.t_quantile(0.99375, 19) == pytest.approx(2.759, abs=1e-3)
    assert P.t_quantile(0.975, 1) == pytest.approx(12.706, abs=1e-3)
    assert P.t_quantile(0.05, 7) == pytest.approx(-1.895, abs=1e-3)
    assert P.t_quantile(0.5, 3) == 0.0


def test_cluster_t_interval_two_way_formula_on_a_hand_example() -> None:
    # e = [-1.5, -0.5, 0.5, 1.5]; V_a = (4 + 4) / 16; V_b = (1 + 1) / 16; V_ab = 5 / 16 -> V = 0.3125
    mean, lo, hi, g = P.cluster_t_interval([1, 2, 3, 4], ["A", "A", "B", "B"], ["X", "Y", "X", "Y"], level=0.90)
    se = math.sqrt(0.3125 * 2 / 1)
    assert mean == 2.5 and g == 2
    assert hi - mean == pytest.approx(P.t_quantile(0.95, 1) * se, rel=1e-6) and mean - lo == pytest.approx(hi - mean)
    # V <= 0 falls back to max(V_a, V_b) (here 0: no spread between the clusters)
    m2, lo2, hi2, _ = P.cluster_t_interval([1, -1, 1, -1], ["A", "A", "B", "B"], ["X", "X", "Y", "Y"])
    assert m2 == 0 and lo2 == hi2 == 0
    # G = min(#a, #b) < 2: no interval
    assert P.cluster_t_interval([1, 2, 3], ["A", "A", "A"], ["X", "Y", "Z"]) is None
    assert P.cluster_t_interval([], [], []) is None


def test_wilson_interval() -> None:
    lo, hi = P.wilson_interval(5, 5)
    z = 1.645
    denom = 1 + z * z / 5
    center = (1 + z * z / 10) / denom
    half = z * math.sqrt(z * z / 100) / denom
    assert lo == pytest.approx(center - half) and hi == pytest.approx(1.0)
    lo2, hi2 = P.wilson_interval(3, 10)
    assert lo2 < 0.3 < hi2
    assert P.wilson_interval(0, 0) is None


def test_verdict_insufficient_exact_sentence_and_reasons() -> None:
    ideas = _ideas(7, lambda i: 100.0)
    v = _verdict(ideas, hours=3.0)
    assert v.level == "insufficient" and v.n_ideas == 7 and v.clusters == 7
    assert v.sentence == ("Not enough evidence yet: 7 ideas entered (7 closed, 0 open) across 7 races in 3.0 h observed. "
                          "A verdict needs at least 30 ideas, 20 independent groups of races and 2-hour periods, and 12 "
                          "hours; P&L so far +700 SUSQies at liquidation value.")
    assert v.reasons[:3] == ["only 3.0 h observed (needs 12)", "only 7 ideas entered (needs 30)",
                             "only 7 independent groups (needs 20): 7 races, 7 time blocks"]
    assert "Closed: 7 ideas, +700 realised; open: 0 ideas, +0 at liquidation value" in v.reasons
    assert v.thresholds == {"min_hours": 12, "min_ideas": 30, "min_clusters": 20, "positive_hours": 48, "positive_days": 2}
    assert v.caveats == list(P.CAVEATS)
    one = _verdict(_ideas(1, lambda i: -5.0), hours=1.0)
    assert one.sentence.startswith("Not enough evidence yet: 1 idea entered (1 closed, 0 open) across 1 race in 1.0 h")


def test_verdict_counts_open_losers_not_only_closed_winners() -> None:
    winners = [P.IdeaOutcome(f"w{i}", "value", f"R{i}", T + i * 7200, "D", True, 250.0, 100.0) for i in range(5)]
    losers = [P.IdeaOutcome(f"l{i}", "value", f"S{i}", T + i * 7200, "R", False, -200.0, 100.0) for i in range(5)]
    trades = [PaperTrade(f"t{i}", "h", f"w{i}", "value", ["1"], T, T + 60, 1, 100, 350, 250.0, "target") for i in range(5)]
    v = _verdict(winners + losers, hours=20.0, closed_trades=trades)
    assert v.n_ideas == 10 and v.open_ideas == 5 and v.closed_trades == 5
    assert v.mean_trade_pnl == pytest.approx(25.0)
    assert v.closed_pnl == pytest.approx(1250.0) and v.open_pnl_liq == pytest.approx(-1000.0)
    assert v.win_rate == 1.0 and v.win_rate_low == pytest.approx(P.wilson_interval(5, 5)[0], abs=1e-6)
    assert "Closed: 5 ideas, +1,250 realised; open: 5 ideas, -1,000 at liquidation value" in v.reasons
    assert ("Win rate 100% over 5 closed ideas (90% interval 65% to 100%): closed trades only: biased toward quick "
            "winners") in v.reasons


def test_verdict_promising_is_the_most_one_day_can_say() -> None:
    ideas = _ideas(40, lambda i: 100.0 + _noise(i))
    v = _verdict(ideas)
    assert v.level == "promising" and v.clusters == 25 and v.df == 24
    ci = P.cluster_t_interval([i.pnl for i in ideas], [i.race_key for i in ideas],
                              [(int(i.entered_at // P.TIME_BLOCK_S), i.direction) for i in ideas], level=0.90)
    assert v.ci_low == pytest.approx(ci[1], abs=1e-6) and v.ci_high == pytest.approx(ci[2], abs=1e-6)
    pnl = sum(i.pnl for i in ideas)
    ex = pnl - max(i.pnl for i in ideas)
    assert v.sentence == (
        f"Promising, not proof: {pnl:+,.0f} SUSQies at liquidation value over 24.0 h observed and 40 ideas (40 closed, 0 "
        f"open); the 90% interval for the average idea is {v.ci_low:+,.1f} to {v.ci_high:+,.1f} SUSQies, and the result "
        f"stays positive without the best idea ({ex:+,.0f}). One day cannot separate an edge from one national swing: "
        "confirm it on a second day before acting.")
    assert v.return_ci_low > 0 and v.exploratory is False


def test_verdict_positive_needs_48_hours_on_two_days_each_positive() -> None:
    days = {"2026-09-21": 10.0, "2026-09-22": 24.0, "2026-09-23": 14.0}
    ideas = _ideas(40, lambda i: 100.0 + _noise(i))
    v = _verdict(ideas, hours=48.0, days=days)
    assert v.level == "positive" and v.days_covered == 3
    pnl = sum(i.pnl for i in ideas)
    ex = pnl - max(i.pnl for i in ideas)
    assert v.sentence == (
        f"Profitable so far: {pnl:+,.0f} SUSQies at liquidation value over 48.0 h observed on 3 days and 40 ideas; the 90% "
        f"interval for the average idea is {v.ci_low:+,.1f} to {v.ci_high:+,.1f} SUSQies, positive on each of the last two "
        f"days and without the best idea ({ex:+,.0f}).")
    assert _verdict(ideas, hours=47.0, days=days).level == "promising"
    assert _verdict(ideas, hours=60.0, days={"2026-09-21": 50.0, "2026-09-22": 5.0}).level == "promising"
    # the most recent day lost money: still only promising
    day23 = [i for i in range(40) if P._utc_day(T + (i % 25) * P.TIME_BLOCK_S) == "2026-09-23"]
    assert day23
    mixed = _ideas(40, lambda i: -50.0 if i in day23 else 100.0 + _noise(i))
    mv = _verdict(mixed, hours=48.0, days=days)
    assert mv.level == "promising"
    assert any(r.startswith('"Profitable so far" needs 48 covered hours on 2 UTC days') for r in mv.reasons)


def test_verdict_negative_and_inconclusive_sentences() -> None:
    neg = _ideas(40, lambda i: -100.0 + _noise(i))
    v = _verdict(neg)
    pnl = sum(i.pnl for i in neg)
    assert v.level == "negative"
    assert v.sentence == (f"Losing so far: {pnl:+,.0f} SUSQies at liquidation value over 24.0 h observed and 40 ideas (40 "
                          f"closed, 0 open); the 90% interval for the average idea is {v.ci_low:+,.1f} to "
                          f"{v.ci_high:+,.1f} SUSQies, entirely below zero.")
    # the guards never soften a loss
    assert _verdict(neg, depth_unknown_share=0.9, swing_share=0.9).level == "negative"
    zero = _ideas(40, lambda i: 100.0 if i % 2 else -100.0)
    z = _verdict(zero)
    zp = sum(i.pnl for i in zero)
    assert z.level == "inconclusive"
    assert z.sentence == (f"Inconclusive: {zp:+,.0f} SUSQies at liquidation value over 24.0 h observed and 40 ideas (40 "
                          f"closed, 0 open), but the 90% interval for the average idea ({z.ci_low:+,.1f} to "
                          f"{z.ci_high:+,.1f} SUSQies) includes zero.")


@pytest.mark.parametrize("kw,clause", [
    ({"pnl_liq": 100.0}, "without the best idea the result is -20."),
    ({"depth_unknown_share": 0.30}, "30% of the value is marked without a recent order book."),
    ({"swing_share": 0.15}, "a 3-point national swing would move this portfolio by 15% of its value, so the result may be "
                            "one swing, not an edge."),
    ({"pnl_liq": 300.0}, "one race supplies 67% of the P&L."),
])
def test_verdict_guards_downgrade_a_favourable_result(kw: Dict[str, Any], clause: str) -> None:
    ideas = _ideas(40, lambda i: 100.0 + _noise(i))
    if kw.get("pnl_liq") == 100.0:
        ideas = _ideas(40, lambda i: 120.0 if i == 0 else 100.0)  # best idea 120 > the whole P&L 100
    if kw.get("pnl_liq") == 300.0:
        ideas = _ideas(40, lambda i: 100.0)  # race R0 holds 2 ideas (+200) of a 300 P&L
    v = _verdict(ideas, **kw)
    assert v.level == "inconclusive"
    assert v.sentence.endswith(", but " + clause)
    assert len(v.reasons) >= 3


def test_verdict_exploratory_level_and_cap() -> None:
    days = {"2026-09-21": 10.0, "2026-09-22": 24.0, "2026-09-23": 14.0}
    ideas = _ideas(40, lambda i: 100.0 + _noise(i))
    v = _verdict(ideas, hours=48.0, days=days, exploratory=True, n_portfolios=9)
    assert v.level == "promising" and v.exploratory is True
    assert v.ci_level == pytest.approx(1 - 0.10 / 9, abs=1e-6)
    assert v.sentence.startswith("Exploratory (one of 9 portfolios, judged at the stricter 99% level): Promising, not proof:")
    assert "the 99% interval for the average idea" in v.sentence
    head = _verdict(ideas, hours=48.0, days=days)
    assert v.ci_high - v.ci_low > head.ci_high - head.ci_low  # stricter: wider


def test_verdict_drops_synthetic_and_frozen_ideas() -> None:
    ideas = _ideas(10, lambda i: 10.0)
    ideas[0].synthetic = True
    ideas[1].frozen = True
    v = _verdict(ideas, unvalued_positions=1, unvalued_value=1234.4)
    assert v.n_ideas == 8 and v.synthetic_excluded == 1 and v.unvalued_positions == 1
    assert v.sentence.endswith(" 1 position whose market closed without a ruling is left out (last value 1,234).")
    assert any("candle or synthetic-book fills left out" in r for r in v.reasons)
    two = _verdict(ideas, unvalued_positions=2, unvalued_value=10.0)
    assert " 2 positions whose market closed without a ruling are left out" in two.sentence
    demo = _verdict(ideas, demo=True)
    assert demo.caveats[-1] == P.DEMO_CAVEAT


def test_engine_verdicts_headline_and_exploratory() -> None:
    eng, _ = make_engine([human(), bot()], headline_portfolio="human:fixed")
    eng.step(T, mobs(T), [opp()])
    v = eng.verdicts()
    assert v["human:fixed"].exploratory is False and v["bot"].exploratory is True
    assert v["bot"].sentence.startswith("Exploratory (one of 2 portfolios, judged at the stricter 95% level): ")
    s = {p.portfolio_id: p for p in eng.portfolios()}
    assert s["human:fixed"].headline is True and s["bot"].headline is False and s["bot"].exploratory is True
    assert P.headline_id(PaperConfig(sizing="chaser", portfolios=default_portfolios("chaser"))) == "human:chaser"
    assert P.headline_id(PaperConfig(portfolios=[bot("x")])) == "x"


# --------------------------------------------------------------------------- 10. persistence, coverage, reset


def _busy_engine(persistence: Any, **kw: Any) -> P.PaperEngine:
    eng, _ = make_engine(persistence=persistence, **kw)
    q = {"1": (0.50, 0.52), "2": (0.93, 0.94)}
    eng.step(T, mobs(T, quotes=q), [opp("1"), hole("2")])
    eng.step(T + 30, mobs(T + 30, quotes=q, books=[bk("1", T + 29, bids=[(0.50, 80)], asks=[(0.52, 500)])]), [hole("2")])
    return eng


def test_a_new_engine_on_the_same_persistence_continues_the_run() -> None:
    store = P.MemoryPaperPersistence()
    eng = _busy_engine(store)
    before = eng.state()
    eng2, _ = make_engine(persistence=store)
    after = eng2.state()
    assert eng2.run_id == eng.run_id and after["steps"] == before["steps"] == 2
    for key in ("cash", "reserved_cash", "positions", "orders", "consumed", "fill_obs", "cooldowns"):
        assert after["portfolios"]["bot"][key] == before["portfolios"]["bot"][key], key
    assert after["counters"] == before["counters"] == {"bot": {"o": 2, "f": 1, "t": 0, "b": 0}}
    assert after["coverage"] == before["coverage"]
    assert [f.fill_id for f in eng2.fills()] == ["bot:f1"]
    q = {"1": (0.50, 0.52), "2": (0.93, 0.94), "3": (0.3, 0.32)}
    eng2.step(T + 60, mobs(T + 60, quotes=q), [hole("2"), opp("3", limit=0.33)])
    # o1 is the resting hole order (ideas are ordered by score, then id), o2 the filled value entry
    assert {o.order_id for o in eng2.orders()} == {"bot:o1", "bot:o3"}
    assert eng2.state()["steps"] == 3


def test_changed_settings_end_the_old_run_and_start_a_new_one() -> None:
    store = P.MemoryPaperPersistence()
    eng = _busy_engine(store)
    old_id = eng.run_id
    eng2, _ = make_engine(persistence=store, regime="resolved_outcomes")
    assert eng2.run_id is None
    prev = store.paper_last_ended_run()
    assert prev["run_id"] == old_id and prev["ended_at"] is not None
    final = prev["state"]["final"]
    assert final["reason"] == "settings changed" and set(final) == {"at", "reason", "verdicts", "portfolios", "study"}
    old_trades = store.paper_trades(old_id)
    assert [t["exit_reason"] for t in old_trades] == ["settings changed"]
    assert store.paper_orders(old_id, statuses=["resting", "pending"]) == []
    eng2.step(T + 500, mobs(T + 500), [])
    assert eng2.run_id == "run-1790000500" and eng2.summary()["has_previous"] is True
    assert eng2.positions() == [] and eng2.config.regime == "resolved_outcomes"
    assert P.config_fingerprint(eng.config, "test") != P.config_fingerprint(eng2.config, "test")
    assert P.config_fingerprint(eng.config, "test") != P.config_fingerprint(eng.config, "other code")
    cfg = PaperConfig(portfolios=[bot()])
    same = PaperConfig(portfolios=[bot()], target_hours=48.0, demo=True)
    assert P.config_fingerprint(cfg, "x") == P.config_fingerprint(same, "x")


def test_reset_writes_the_final_snapshot_and_the_previous_run_answers() -> None:
    store = P.MemoryPaperPersistence()
    eng = _busy_engine(store)
    old_id = eng.run_id
    new_id = eng.reset(T + 60, start_capital=50_000, capital_source="set by you", target_hours=12)
    assert new_id != old_id and eng.run_id == new_id
    s = eng.summary()
    assert s["run"]["start_capital"] == 50_000 and s["run"]["capital_source"] == "set by you"
    assert s["run"]["target_hours"] == 12 and s["has_previous"] is True
    assert eng.portfolios()[0].equity_liq == pytest.approx(50_000)
    prev = eng.previous_summary()
    assert prev["run"]["run_id"] == old_id and prev["run"]["end_reason"] == "reset"
    assert prev["run"]["final"]["reason"] == "reset" and prev["run"]["ended_at"] == T + 60
    assert prev["headline"] is not None and prev["portfolios"][0]["portfolio_id"] == "bot"
    assert {t["exit_reason"] for t in prev["trades"]} == {"reset"}
    assert store.paper_last_ended_run()["run_id"] == old_id
    json.dumps(prev)


def test_runs_ended_on_completion_and_run_ids_never_collide() -> None:
    store = P.MemoryPaperPersistence()
    eng, _ = make_engine(persistence=store)
    eng.step(T, mobs(T), [])
    first = eng.run_id
    snap = eng.end_run(T, "completed")
    assert snap["reason"] == "completed" and eng.run_id is None
    eng.step(T, mobs(T), [])
    assert eng.run_id == first + "-2"
    assert eng.end_run(T + 1, "completed") is not None and eng.end_run(T + 2, "completed") is None


def test_coverage_clock_caps_gaps_and_splits_days() -> None:
    c = P.CoverageClock(interval_s=30.0)
    assert c.tick(T) is None and c.tick(T + 30) is None and c.tick(T + 60) is None
    assert c.tick(T + 60 + 36000) == pytest.approx(36000)  # a 10-hour gap counts 90 s
    assert c.covered_s == pytest.approx(150.0) and c.gaps == [{"from": T + 60, "to": T + 36060}]
    assert c.wall_hours(T + 36060) == pytest.approx(36060 / 3600)
    midnight = 1_790_035_200.0  # 2026-09-22T00:00:00Z
    d = P.CoverageClock(interval_s=60.0)
    d.tick(midnight - 30)
    d.tick(midnight + 30)
    assert d.day_hours() == {"2026-09-21": pytest.approx(30 / 3600), "2026-09-22": pytest.approx(30 / 3600)}
    assert P.CoverageClock.from_dict(c.to_dict()) == c


def test_downtime_cancels_resting_orders_at_the_last_step_before_the_gap() -> None:
    eng, _ = make_engine()
    _resting(eng)
    q = {"1": (0.93, 0.94)}
    eng.step(T + 30, mobs(T + 30, quotes=q), [hole()])
    gap_end = T + 30 + 10 * 3600
    tape = [tr("during", T + 3600, 0.50, 100), tr("before", T + 20, 0.61, 10)]
    r = eng.step(gap_end, mobs(gap_end, quotes={"1": (0.93, 0.94)}, trades={"1": tape}), [hole()])
    assert r.gap_s == pytest.approx(36000)
    assert [f.trade_ids for f in eng.fills()] == [["before"]]  # the print from the downtime never fills it
    closed = all_orders(eng)[0]
    assert closed.status == "cancelled" and closed.cancel_at == T + 30  # D48: at the last step before the gap
    assert closed.close_reason == "The bot was not running: a resting order cannot be managed through downtime"
    s = eng.summary()["run"]
    assert s["hours_run"] == pytest.approx((30 + 90) / 3600, abs=1e-6) and len(s["gaps"]) == 1
    assert s["wall_hours"] == pytest.approx((gap_end - T) / 3600, abs=1e-6)


def test_equity_points_are_spaced_by_a_minute() -> None:
    store = P.MemoryPaperPersistence()
    eng, _ = make_engine(persistence=store)
    for k in range(11):
        eng.step(T + 30 * k, mobs(T + 30 * k), [])
    pts = [p.ts for p in eng.equity()["bot"]]
    assert pts == [T + 60 * k for k in range(6)]
    assert [r["ts"] for r in store.paper_equity(eng.run_id, "bot")] == pts


def test_final_snapshot_when_covered_hours_reach_the_target() -> None:
    eng, _ = make_engine(target_hours=24.0)
    t = T
    for _ in range(960):
        eng.step(t, mobs(t), [])
        t += 90.0  # 3 x interval: still fully covered
    assert eng.state()["final"] is None and eng.summary()["run"]["complete"] is False
    eng.step(t, mobs(t), [])
    final = eng.state()["final"]
    assert final is not None and final["reason"] == P.TARGET_REACHED and final["at"] == t
    run = eng.summary()["run"]
    assert run["complete"] is True and run["progress"] == 1.0 and run["hours_run"] == pytest.approx(24.0)
    eng.step(t + 30, mobs(t + 30), [])  # the run goes on; the snapshot stays the first one
    assert eng.state()["final"]["at"] == t and eng.run_id is not None


def test_memory_persistence_orderings_and_copies() -> None:
    store = P.MemoryPaperPersistence()
    store.paper_save_run("r1", T, {"x": 1}, {"s": [1]}, T)
    store.paper_save_run("r2", T + 10, {}, {}, T + 10)
    assert store.paper_load_run(None)["run_id"] == "r2"
    store.paper_end_run("r2", T + 20)
    assert store.paper_load_run(None)["run_id"] == "r1" and store.paper_last_ended_run()["run_id"] == "r2"
    store.paper_add_fills("r1", [{"fill_id": "a", "ts": 1.0}, {"fill_id": "b", "ts": 2.0}, {"fill_id": "c", "ts": 2.0}])
    assert [f["fill_id"] for f in store.paper_fills("r1")] == ["c", "b", "a"]
    store.paper_put_orders("r1", [{"order_id": "o1", "created_at": 2.0, "status": "pending"},
                                  {"order_id": "o2", "created_at": 1.0, "status": "pending"}])
    store.paper_put_orders("r1", [{"order_id": "o1", "created_at": 2.0, "status": "filled"}])
    assert [o["order_id"] for o in store.paper_orders("r1")] == ["o2", "o1"]
    assert [o["order_id"] for o in store.paper_orders("r1", statuses=["filled"])] == ["o1"]
    store.paper_put_events("r1", [{"event_id": "e2", "t0": 2.0, "kind": "value"}, {"event_id": "e1", "t0": 1.0, "kind": "basket"}])
    assert [e["event_id"] for e in store.paper_events("r1")] == ["e1", "e2"]
    assert [e["event_id"] for e in store.paper_events("r1", kind="value")] == ["e2"]
    rec = store.paper_load_run("r1")
    rec["state"]["s"].append(2)
    assert store.paper_load_run("r1")["state"] == {"s": [1]}  # deep copies


# --------------------------------------------------------------------------- 11. runner and read plan


class SpyReader:
    def __init__(self, books: Optional[Dict[str, Any]] = None, tapes: Optional[Dict[str, Any]] = None,
                 limiter: Any = None, clock: Any = None) -> None:
        self.books = dict(books or {})
        self.tapes = dict(tapes or {})
        self.calls: List[Tuple[Any, ...]] = []
        self.limiter = limiter
        self.clock = clock

    def _spend(self, n: int = 1) -> None:
        if self.limiter is not None:
            self.limiter.used += n
        if self.clock is not None:
            self.clock.t += 1.0

    def book(self, exchange_id: str, depth: int) -> Optional[BookObservation]:
        self.calls.append(("book", exchange_id, depth))
        self._spend()
        b = self.books.get(exchange_id)
        return b(self.clock.t if self.clock else T) if callable(b) else b

    def trades(self, exchange_id: str, since: float, max_pages: int = 3) -> Optional[P.TapeRead]:
        self.calls.append(("trades", exchange_id, since, max_pages))
        tape = self.tapes.get(exchange_id)
        self._spend(tape.pages if tape is not None else 1)
        return tape


class Strict:
    """Records every attribute the runner touches on the reader."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "touched", [])

    def __getattribute__(self, name: str) -> Any:
        if name in ("_inner", "touched"):
            return object.__getattribute__(self, name)
        object.__getattribute__(self, "touched").append(name)
        return getattr(object.__getattribute__(self, "_inner"), name)


class Limiter:
    def __init__(self, limit: int = 20, used: int = 0) -> None:
        self.limit, self.used = limit, used

    def room(self) -> int:
        return self.limit - self.used


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _inputs_fn(quotes: Dict[str, Any], books: Sequence[BookObservation] = ()) -> Any:
    def fn(t: float) -> Tuple[StrategyInputs, MarketObservation]:
        o = mobs(t, quotes=quotes, books=books)
        from supermarket_bot.models import PricePoint
        latest = {e: PricePoint(ts=q.ts, price=None, bid=q.bid, ask=q.ask) for e, q in o.quotes.items()}
        return StrategyInputs(now=t, cup_end=CUP, infos=dict(o.infos), latest=latest), o
    return fn


def test_read_plan_priorities_and_groups() -> None:
    eng, _ = make_engine()
    q = {"1": (0.50, 0.52), "2": (0.52, 0.54), "3": (0.53, 0.55), "4": (0.93, 0.94), "5": (0.50, 0.52)}
    eng.step(T, mobs(T, quotes=q), [opp("5")])
    eng.step(T + 30, mobs(T + 30, quotes=q, books=[bk("5", T + 29, bids=[(0.50, 50)], asks=[(0.52, 500)])]), [])
    eng.step(T + 60, mobs(T + 60, quotes=q), [opp("1"), basket(legs=(("2", "no", 0.48), ("3", "no", 0.47))), hole("4")])
    plan = eng.wanted_reads(T + 400, mobs(T + 400, quotes=q), candidates=["6", "4"])
    got = [(r.priority, r.kind, r.exchange_id) for r in plan.requests]
    assert got == [(1, "book", "1"), (2, "book", "2"), (2, "book", "3"), (3, "trades", "4"), (4, "book", "5"),
                   (5, "book", "4"), (6, "book", "6")]
    by = {(r.kind, r.exchange_id): r for r in plan.requests}
    assert by[("book", "1")].after == T + 62 and by[("book", "1")].group is None
    assert by[("book", "2")].group == by[("book", "3")].group == "g:2+3"
    assert by[("trades", "4")].since == T + 60
    # a pending order whose fill window has not opened yet is not read; one already satisfied neither
    early = eng.wanted_reads(T + 61, mobs(T + 61, quotes=q))
    assert ("book", "1") not in {(r.kind, r.exchange_id) for r in early.requests if r.priority == 1}
    done = eng.wanted_reads(T + 90, mobs(T + 90, quotes=q, books=[bk("1", T + 70, asks=[(0.52, 5)])]))
    assert ("book", "1") not in {(r.kind, r.exchange_id) for r in done.requests if r.priority == 1}


def test_pending_set_legs_are_requested_as_one_group() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    eng.step(T, mobs(T, quotes=NV, books=_set_books(T - 1)), [basket()])
    plan = eng.wanted_reads(T + 30, mobs(T + 30, quotes=NV))
    reqs = [r for r in plan.requests if r.priority == 1]
    assert {r.exchange_id for r in reqs} == {"1", "2"} and len({r.group for r in reqs}) == 1


def test_fulfil_plan_caps_groups_and_room() -> None:
    reqs = [P.ReadRequest("book", str(i), 4, "mark") for i in range(12)]
    reader = SpyReader(books={str(i): bk(str(i), T) for i in range(12)})
    f = P.fulfil_plan(P.ReadPlan(reqs), read_book=lambda r: reader.book(r.exchange_id, 20),
                      read_tape=lambda r, n: None, max_books=8, max_pages=2)
    assert f.book_reads == 8 and f.skipped == 4 and len(f.books) == 8
    group = [P.ReadRequest("book", e, 1, "fill", group="g") for e in ("a", "b", "c")]
    f2 = P.fulfil_plan(P.ReadPlan([P.ReadRequest("book", "x", 1, "fill")] * 1 + group),
                       read_book=lambda r: bk(r.exchange_id, T), read_tape=lambda r, n: None, max_books=3, max_pages=2)
    assert f2.book_reads == 1 and f2.skipped == 3 and set(f2.books) == {"x"}  # the group did not fit: none of it
    lim = Limiter(limit=3)

    def counted(r: P.ReadRequest) -> BookObservation:
        lim.used += 1
        return bk(r.exchange_id, T)

    f3 = P.fulfil_plan(P.ReadPlan(reqs), read_book=counted, read_tape=lambda r, n: None, max_books=8, max_pages=2,
                       room=lim.room)
    assert f3.book_reads == 3 and f3.skipped == 9
    tapes = [P.ReadRequest("trades", "t1", 3, "tape"), P.ReadRequest("trades", "t2", 3, "tape")]
    seen: List[int] = []

    def tape(r: P.ReadRequest, pages: int) -> P.TapeRead:
        seen.append(pages)
        return P.TapeRead(trades=[], truncated=True, pages=pages)

    f4 = P.fulfil_plan(P.ReadPlan(tapes), read_book=lambda r: None, read_tape=tape, max_books=8, max_pages=2,
                       max_tape_pages=3)
    assert seen == [2] and f4.trade_reads == 2 and f4.skipped == 1 and f4.tape["t1"].truncated
    f5 = P.fulfil_plan(P.ReadPlan(reqs[:2]), read_book=lambda r: None, read_tape=tape, max_books=8, max_pages=2)
    assert f5.book_reads == 2 and f5.books == {}  # failed reads are counted, not retried


def test_runner_reads_within_budget_only_book_and_trades_and_decides_after_reads() -> None:
    clock = Clock(T)
    lim = Limiter(limit=20)
    q = {"1": (0.50, 0.52)}
    inner = SpyReader(books={"1": lambda t: bk("1", t, asks=[(0.52, 500)])}, limiter=lim, clock=clock)
    reader = Strict(inner)
    eng, _ = make_engine()
    runner = P.PaperRunner(eng, reader, _inputs_fn(q), signal_fn=lambda inputs, params: [opp()], limiter=lim,
                           clock=clock, interval=30.0, candidates_fn=lambda inputs: [])
    r1 = runner.step()
    (order,) = eng.orders()
    assert order.created_at == T and r1.book_reads == 0
    clock.t = T + 30
    r2 = runner.step()
    assert r2.book_reads == 1 and r2.reads_skipped == 0
    # the order was decided at T; the book was read at T + 30 and the decision clock moved on after the read
    assert eng.fills()[0].book_at == T + 31
    assert set(reader.touched) <= {"book", "trades"} and "book" in reader.touched
    body = runner.summary()
    assert body["budget"]["reads_used"] == 1 and body["budget"]["reads_limit"] == 20
    assert body["budget"]["book_reads_last_step"] == 1 and body["run"]["interval"] == 30.0
    assert body["last_step"]["book_reads"] == 1
    # no room left: nothing is read, everything wanted is counted as skipped
    lim.used = 20
    eng.step(T + 40, mobs(T + 40, quotes={"1": (0.50, 0.52), "2": (0.4, 0.42)}), [opp("2", limit=0.43)])
    clock.t = T + 70
    r3 = runner.step()
    assert r3.book_reads == 0 and r3.reads_skipped >= 1 and runner.summary()["budget"]["reads_skipped_last_step"] >= 1


def test_runner_decided_at_is_the_clock_after_the_reads() -> None:
    clock = Clock(T)
    inner = SpyReader(books={"9": lambda t: bk("9", t)}, clock=clock)
    eng, _ = make_engine()
    runner = P.PaperRunner(eng, inner, _inputs_fn({"1": (0.50, 0.52), "9": (0.4, 0.5)}),
                           signal_fn=lambda inputs, params: [opp()], clock=clock, candidates_fn=lambda inputs: ["9"])
    runner.step()
    (order,) = eng.orders()
    assert order.created_at == T + 1  # one candidate book read moved the clock by 1 s before the decision


def test_runner_with_an_explicit_now_still_decides_after_its_reads() -> None:
    clock = Clock(T + 1000)  # the injected clock is ahead of the step time the tracker passes
    inner = SpyReader(books={"9": lambda t: bk("9", t)}, clock=clock)
    eng, _ = make_engine()
    runner = P.PaperRunner(eng, inner, _inputs_fn({"1": (0.50, 0.52), "9": (0.4, 0.5)}),
                           signal_fn=lambda inputs, params: [opp()], clock=clock, candidates_fn=lambda inputs: ["9"])
    runner.step(T)
    assert eng.orders()[0].created_at == T + 1  # the step time plus the 1 s the read took on the clock


def test_runner_tape_paging_and_truncation() -> None:
    clock = Clock(T)
    tape = P.TapeRead(trades=[tr("p", T + 10, 0.60, 5)], truncated=True, pages=2)
    inner = SpyReader(tapes={"1": tape}, clock=clock)
    eng, _ = make_engine()
    runner = P.PaperRunner(eng, inner, _inputs_fn({"1": (0.93, 0.94)}), signal_fn=lambda i, p: [hole()], clock=clock,
                           candidates_fn=lambda inputs: [])
    runner.step()
    clock.t = T + 70
    r = runner.step()
    assert ("trades", "1", T, 2) in inner.calls  # max_tape_pages 3, but only 2 pages per step
    assert r.trade_reads == 2 and r.tape_gaps == 1
    order = eng.orders()[0]
    assert order.tape_gap is True and eng.fills()[0].qty == 5


def test_runner_summary_is_published_and_lock_free() -> None:
    eng, _ = make_engine()
    runner = P.PaperRunner(eng, SpyReader(), _inputs_fn({"1": (0.50, 0.52)}), signal_fn=lambda i, p: [],
                           clock=Clock(T), candidates_fn=lambda inputs: [])
    first = runner.summary()
    assert first is runner.summary() and first["run"]["run_id"] is None
    runner.step(T)
    published = runner.summary()
    assert published is not first and published["run"]["run_id"] == eng.run_id
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with runner.lock:
            held.set()
            release.wait(5)

    th = threading.Thread(target=hold)
    th.start()
    assert held.wait(5)
    t0 = time.monotonic()
    assert runner.summary() is published  # never waits for the lock
    assert time.monotonic() - t0 < 1.0
    release.set()
    th.join(5)


def test_runner_never_raises_on_bad_inputs_and_reset_publishes() -> None:
    def broken(t: float) -> Any:
        raise ValueError("no view yet")

    eng, _ = make_engine()
    runner = P.PaperRunner(eng, SpyReader(), broken, clock=Clock(T))
    r = runner.step(T)
    assert r.errors == ["inputs: no view yet"]
    good = P.PaperRunner(eng, SpyReader(), _inputs_fn({"1": (0.50, 0.52)}), signal_fn=lambda i, p: [], clock=Clock(T),
                         candidates_fn=lambda inputs: [])
    good.step(T)
    rid = good.reset(T + 10, start_capital=1234.0)
    assert good.summary()["run"]["run_id"] == rid and good.summary()["run"]["capital_source"] == "set by you"
    assert good.summary()["has_previous"] is True and good.previous()["run"]["end_reason"] == "reset"
    snap = good.end_run(T + 20)
    assert snap["reason"] == "completed" and good.summary()["run"]["end_reason"] == "completed"


def test_runner_propagates_shutdown_errors() -> None:
    class SimClockStall(Exception):
        pass

    def stalls(t: float) -> Any:
        raise SimClockStall("would wait")

    eng, _ = make_engine()
    with pytest.raises(SimClockStall):
        P.PaperRunner(eng, SpyReader(), stalls, clock=Clock(T)).step(T)


def test_start_capital_sources() -> None:
    eng, _ = make_engine()
    inputs = StrategyInputs(now=T, cup_end=CUP, account_value=123_456.0, balance=99_000.0)
    eng.step(T, mobs(T), [], inputs)
    s = eng.summary()["run"]
    assert s["start_capital"] == 123_456.0 and s["capital_source"] == "account value"
    eng2, _ = make_engine()
    eng2.step(T, mobs(T), [], StrategyInputs(now=T, cup_end=CUP, balance=99_000.0))
    assert eng2.summary()["run"]["capital_source"] == "cash"
    eng3, _ = make_engine()
    eng3.step(T, mobs(T), [], StrategyInputs(now=T, cup_end=CUP, initial_balance=100_000.0))
    assert eng3.summary()["run"]["capital_source"] == "initial balance"
    eng4, _ = make_engine()
    eng4.step(T, mobs(T), [])
    assert eng4.summary()["run"]["capital_source"] == "default 100,000"
    eng5, _ = make_engine(start_capital=5_000.0)
    eng5.step(T, mobs(T), [], inputs)
    assert eng5.summary()["run"]["capital_source"] == "set by you" and eng5.portfolios()[0].start_capital == 5_000


# --------------------------------------------------------------------------- 12. summary JSON


RUN_KEYS = {"run_id", "started_at", "hours_run", "wall_hours", "gaps", "target_hours", "progress", "complete", "steps",
            "last_step_at", "last_step_seconds", "interval", "regime", "all_collateral", "sizing", "start_capital",
            "capital_source", "fingerprint", "code_version", "settings", "ended_at", "end_reason", "final"}
TOP_KEYS = {"now", "enabled", "available", "error", "demo", "run", "has_previous", "headline", "table_warning",
            "model_label", "portfolios", "equity", "positions", "baskets", "orders", "fills", "trades", "signals", "study",
            "budget", "fair_value", "caveats", "last_step"}
BUDGET_KEYS = {"reads_used", "reads_limit", "reads_room", "book_reads_last_step", "trade_reads_last_step",
               "reads_skipped_last_step", "max_book_reads_per_step", "max_trade_reads_per_step",
               "sets_deferred_last_step", "tape_gaps_last_step"}


def test_summary_json_shape() -> None:
    eng, _ = make_engine([human(), bot()], policy=FixedPolicy(units=300), headline_portfolio="human:fixed", demo=True)
    q = dict(NV, **{"3": (0.50, 0.52)})
    runner = P.PaperRunner(eng, SpyReader(), _inputs_fn(q, books=_set_books(T - 1)),
                           signal_fn=lambda i, p: [basket(), opp("3")], clock=Clock(T), interval=30.0,
                           candidates_fn=lambda inputs: [], extras_fn=lambda: {"fair_value": {"mode": "auto"}})
    runner.step(T)
    eng.step(T + 30, mobs(T + 30, quotes=q, books=_set_books(T + 29) + [bk("3", T + 29, asks=[(0.52, 500)])]), [])
    runner.step(T + 60)
    body = runner.summary()
    assert set(body) == TOP_KEYS
    assert set(body["run"]) == RUN_KEYS and set(body["budget"]) == BUDGET_KEYS
    assert set(body["run"]["settings"]) == {"sizing", "regime", "all_collateral", "start_capital", "params_changed"}
    assert body["enabled"] is True and body["available"] is True and body["error"] is None and body["demo"] is True
    assert body["fair_value"] == {"mode": "auto"} and body["caveats"][-1] == P.DEMO_CAVEAT
    assert body["table_warning"] == P.TABLE_WARNING.format(n=2) and body["model_label"] == P.FV_MODEL_LABEL
    head = body["headline"]
    assert set(head) == {"portfolio_id", "label", "latency_s", "pnl_liq", "pnl_liq_pct", "pnl_mark", "equity_liq",
                         "unvalued", "depth_unknown_share", "verdict", "verdict_caveats", "untested"}
    assert head["portfolio_id"] == "human:fixed" and head["latency_s"] == HUMAN_LATENCY_S
    assert head["verdict_caveats"] == [P.CAVEATS[i] for i in P.VERDICT_CAVEATS]
    assert [p["portfolio_id"] for p in body["portfolios"]] == ["human:fixed", "bot"]
    port = body["portfolios"][1]
    for key in ("verdict", "execution", "no_trade_reason", "sizing", "by_kind", "exposure", "headline", "exploratory"):
        assert key in port
    pos = body["positions"][0]
    assert {"unrealized_liq", "exit_note", "age_hours", "depth_state", "liq_value"} <= set(pos)
    (basket_row,) = body["baskets"]
    assert set(basket_row) == {"portfolio_id", "basket_id", "idea_id", "sets", "cost", "floor_value", "liq_value", "legs",
                               "naked_qty", "legs_total"}
    assert basket_row["sets"] == 300 and basket_row["floor_value"] == pytest.approx(300.0)
    assert basket_row["naked_qty"] == 0 and basket_row["legs_total"] == 2
    assert set(basket_row["legs"][0]) == {"exchange_id", "side", "qty", "liq_value", "naked_qty"}
    assert body["signals"] == {"count": 2, "by_kind": {"basket": 1, "value": 1}, "at": T + 60}
    assert body["study"]["can_show"] and body["study"]["cannot_show"]
    assert isinstance(body["equity"]["bot"], list) and body["equity"]["bot"][0] == [T, 100_000.0]
    assert body["fills"][0]["ts"] >= body["fills"][-1]["ts"]  # newest first
    json.dumps(body)


def test_summary_without_a_run() -> None:
    eng, _ = make_engine()
    body = eng.summary()
    assert set(body) == TOP_KEYS and body["run"]["run_id"] is None and body["portfolios"] == []
    assert body["headline"] is None and body["study"] is None and body["has_previous"] is False
    assert eng.previous_summary() is None


def test_summary_limits() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=10 ** 6), equity_min_spacing_s=0.0)
    q = {"1": (0.93, 0.94)}
    eng.step(T, mobs(T, quotes=q), [hole()])
    tape: List[TradeRecord] = []
    for k in range(1, 151):
        tape.append(tr(f"p{k}", T + 10 * k - 5, 0.61, 1))
        eng.step(T + 10 * k, mobs(T + 10 * k, quotes=q, trades={"1": tape[-3:]}), [hole()])
    for k in range(151, 451):
        eng.step(T + 10 * k, mobs(T + 10 * k, quotes=q), [hole()])
    body = eng.summary()
    assert len(eng.fills(limit=1000)) == 150 and len(body["fills"]) == P.FILLS_MAX
    assert body["fills"][0]["fill_id"] == "bot:f150"
    assert len(eng.equity()["bot"]) > 300 and len(body["equity"]["bot"]) <= P.EQUITY_POINTS_MAX
    pts = [p.ts for p in eng.equity()["bot"]]
    assert body["equity"]["bot"][0][0] == pts[0] and body["equity"]["bot"][-1][0] == pts[-1]
    assert P.downsample_equity([(float(i), 1.0) for i in range(1000)], 300)[-1] == [999.0, 1.0]
    assert len(P.downsample_equity([(float(i), 1.0) for i in range(1000)], 300)) <= 300


def test_execution_statistics_and_no_trade_reasons() -> None:
    specs = [bot("kind:value", kinds=["value"]), bot("kind:basket", kinds=["basket"]), bot("kind:fade", kinds=["fade"])]
    eng, _ = make_engine(specs, interval_s=3600.0)
    for k in range(19):
        t = T + 3600 * k
        eng.step(t, mobs(t, quotes={"1": (0.52, 0.54, t), "2": (0.53, 0.55, t)}), [basket()] if k < 12 else [])
    reasons = {p.portfolio_id: p.no_trade_reason for p in eng.portfolios()}
    assert reasons["kind:value"] == "No value trades yet: usable outside fair values on 0% of steps (see the fair-value status)."
    assert reasons["kind:basket"] == "12 basket signals, none entered: waiting for fresh books of every leg (budget)."
    assert reasons["kind:fade"] == ("No fade signals in 18.0 h: the market offered no such setup (that is not evidence of "
                                    "no edge).")


# --------------------------------------------------------------------------- with the real strategy and sizing


def test_real_strategy_and_sizing_run_through_run_step() -> None:
    from supermarket_bot.models import PricePoint

    eid = "9026"
    info = ExchangeInfo(eid, "318", None, "Will the Democratic Party win the Texas Senate?", "2026-11-04T17:00:00Z")
    race = RaceRef("2026:SENATE:TX", "SENATE", "TX", party="D")
    eng = P.PaperEngine(PaperConfig(), persistence=P.MemoryPaperPersistence(), clock=lambda: T)  # real policies

    def step(t: float, books: Sequence[BookObservation] = ()) -> Any:
        fv = FairValue(eid, 0.60, "polymarket", confidence="high", usable=True, as_of=t - 5, uncertainty=0.005,
                       prev_value=0.60, match_kind="EXACT", match_confidence=1.0, race_key=race.race_key, party="D")
        point = PricePoint(ts=t, price=0.505, bid=0.50, ask=0.51)
        inputs = StrategyInputs(now=t, cup_end=CUP, infos={eid: info}, latest={eid: point}, fair_values={eid: fv},
                                races={eid: race}, recent_mids={eid: [(t - 60, 0.505), (t, 0.505)]})
        o = MarketObservation(now=t, cup_end=CUP, quotes={eid: Quote(eid, 0.50, 0.51, None, t)},
                              books={b.exchange_id: b for b in books}, open_ids={eid}, infos={eid: info},
                              fair_values={eid: fv}, races={eid: race})
        return P.run_step(eng, inputs, o, lambda plan, now: P.Fulfilment())

    r = step(T, [bk(eid, T - 5, bids=[(0.50, 3000)], asks=[(0.51, 3000), (0.52, 5000)])])
    assert r.errors == [], r.errors
    assert r.signals >= 1, "the strategy should see a value idea (Cup 0.51 vs outside 0.60)"
    kinds = {o.portfolio_id: o.kind for o in eng.orders()}
    assert "kind:value" in kinds and "human:conservative" in kinds, kinds
    assert all(o.qty >= 1 and o.limit_price <= 0.6 for o in eng.orders())
    r2 = step(T + 30, [bk(eid, T + 29, bids=[(0.50, 3000)], asks=[(0.51, 3000), (0.52, 5000)])])
    assert r2.errors == [] and any(f.portfolio_id == "kind:value" for f in eng.fills())
    body = eng.summary()
    assert body["headline"]["portfolio_id"] == "human:conservative"
    assert body["portfolios"][0]["sizing"] is not None
    json.dumps(body)


# --------------------------------------------------------------------------- state shape and bookkeeping details


def test_state_json_version_two_shape() -> None:
    eng = _busy_engine(P.MemoryPaperPersistence())
    st = json.loads(json.dumps(eng.state()))
    for key in ("version", "run_id", "started_at", "start_capital", "capital_source", "target_hours", "steps",
                "last_step_at", "last_step_seconds", "coverage", "counters", "portfolios", "last_real_books", "study",
                "final", "last_signals"):
        assert key in st, key
    assert st["version"] == 2 and st["final"] is None
    port = st["portfolios"]["bot"]
    for key in ("cash", "reserved_cash", "peak_equity", "max_drawdown", "max_drawdown_abs", "realized_pnl", "fills",
                "positions", "orders", "consumed", "cooldowns", "missing", "fill_obs", "mode", "unfilled", "deferred",
                "diagnostics"):
        assert key in port, key
    assert port["missing"] == {"bot:o1": 0}  # the resting hole order
    assert set(st["last_real_books"]) == {"1"}  # exchanges with open positions only
    assert set(st["last_signals"]) == {"count", "by_kind", "at", "top_bookless"}
    assert st["coverage"]["interval_s"] == 30.0 and st["coverage"]["covered_s"] == 30.0


def test_fill_counts_per_kind_are_not_capped_by_the_in_memory_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "RECENT_FILLS_KEPT", 2)
    eng, _ = make_engine(policy=FixedPolicy(units=10))
    q = {"1": (0.93, 0.94)}
    eng.step(T, mobs(T, quotes=q), [hole()])
    for k in range(1, 6):
        eng.step(T + 30 * k, mobs(T + 30 * k, quotes=q, trades={"1": [tr(f"p{k}", T + 30 * k - 5, 0.61, 1)]}), [hole()])
    assert len(eng.fills(limit=None)) == 2
    assert eng.portfolios()[0].by_kind["hole"]["fills"] == 5 and eng.portfolios()[0].fills == 5


def test_a_basket_stops_asking_for_exit_books_once_the_touch_moved_away() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng)
    good = {"1": (0.495, 0.505), "2": (0.495, 0.505)}
    eng.step(T + 2000, mobs(T + 2000, quotes=good), [])
    assert any(r.priority == 2 for r in eng.wanted_reads(T + 2030, mobs(T + 2030, quotes=good)).requests)
    gone = {"1": (0.52, 0.54), "2": (0.53, 0.55)}  # NO bids 0.46 + 0.45 = 0.91 < cost: no exit to check
    eng.step(T + 2030, mobs(T + 2030, quotes=gone), [])
    assert not any(r.priority == 2 for r in eng.wanted_reads(T + 2060, mobs(T + 2060, quotes=gone)).requests)


# --------------------------------------------------------------------------- regressions: engine fixes (paper-verify QA)


def _exit_150_of_300(eng: P.PaperEngine) -> Any:
    """Hold 300 YES @0.52; the bid reaches the 0.60 target; the exit (limit 0.595) sells 150 into 0.60x100 + 0.595x50."""
    plan = ExitPlan(kind="value", target_bid=0.60)
    eng.step(T, mobs(T), [opp(limit=0.535, plan=plan)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.50, 2000)], asks=[(0.52, 1000)])]), [])
    eng.step(T + 60, mobs(T + 60, quotes={"1": (0.60, 0.61)}), [])
    (o,) = [o for o in eng.orders() if o.purpose == "exit"]
    assert o.limit_price == 0.595

    def book(t: float) -> BookObservation:
        return bk("1", t, bids=[(0.60, 100), (0.595, 50), (0.55, 1000)], asks=[(0.61, 1000)])

    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.60, 0.61)}, books=[book(T + 89)]), [])
    return book


def test_fills_1_marks_never_reuse_bids_this_portfolio_sold_into() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    book = _exit_150_of_300(eng)
    assert sum(f.qty for f in eng.fills() if f.action == "sell") == 150
    (pos,) = eng.positions()
    # the 150 left can only sell into what we did NOT take: 150 x 0.55 (not 100 x 0.60 + 50 x 0.595)
    assert pos.qty == 150 and pos.liq_value == pytest.approx(82.5) and pos.depth_state == "fresh"
    assert eng.portfolios()[0].equity_liq == pytest.approx(100_000 - 300 * 0.52 + 100 * 0.60 + 50 * 0.595 + 82.5)
    # an identical later book: the exit cannot re-take the consumed bids, and neither can the mark
    eng.step(T + 120, mobs(T + 120, quotes={"1": (0.60, 0.61)}, books=[book(T + 119)]), [])
    assert sum(f.qty for f in eng.fills() if f.action == "sell") == 150
    assert eng.positions()[0].liq_value == pytest.approx(82.5)


def test_fills_1_basket_leg_after_its_residue_exit_is_marked_without_reuse() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng, b2_fill=200)  # leg 1 300, leg 2 200: residue exit of 100 on leg 1
    # the residue sells 60 at NO 0.46 and 40 at NO 0.40 (YES asks 0.54 x 60, 0.60 x 1000)
    eng.step(T + 60, mobs(T + 60, quotes=NV, books=[bk("1", T + 59, bids=[(0.52, 1)], asks=[(0.54, 60), (0.60, 1000)])]), [])
    (residue,) = eng.trades()
    assert residue.proceeds == pytest.approx(60 * 0.46 + 40 * 0.40)
    leg1 = {p.exchange_id: p for p in eng.positions()}["1"]
    assert leg1.qty == 200 and leg1.liq_value == pytest.approx(200 * 0.40)  # not 60 x 0.46 + 140 x 0.40 = 83.60


def test_fills_1_stale_mark_subtracts_consumption_before_the_shift() -> None:
    old = bk("1", T - 1800, bids=[(0.60, 100), (0.55, 1000)])
    q = Quote("1", 0.62, 0.64, None, T)

    def taken(levels: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        return [(p, qty - 100 if abs(p - 0.60) < 1e-9 else qty) for p, qty in levels if not (abs(p - 0.60) < 1e-9 and qty <= 100)]

    v, state = P.liquidation_value("yes", 150, None, q, last_real_book=old, now=T, config=PaperConfig(), adjust_levels=taken)
    # shifted by the RAW book's offset (+0.02): the rung we took is gone, the rest sits at 0.57 (never re-inflated to 0.62)
    assert state == "stale" and v == pytest.approx(150 * 0.57)


def test_fills_2_a_human_cancel_takes_its_latency_and_the_sweep_fills_it() -> None:
    def run(spec: PortfolioSpec) -> Tuple[float, float, float]:
        eng, _ = make_engine([spec], policy=FixedPolicy(units=100))
        q = {"1": (0.93, 0.94)}
        eng.step(T, mobs(T, quotes=q), [hole(limit=0.62)])
        for k in range(1, 11):  # the idea lives until T + 300
            eng.step(T + 30 * k, mobs(T + 30 * k, quotes=q), [hole(limit=0.62)])
        eng.step(T + 330, mobs(T + 330, quotes={"1": (0.80, 0.85)}), [])  # news: the idea disappears ...
        eng.step(T + 360, mobs(T + 360, quotes={"1": (0.60, 0.70)}), [])  # ... for 2 steps: cancel decided at T + 360
        cancel_at = eng.orders()[0].cancel_at
        sweep = [tr("s1", T + 370, 0.55, 60), tr("s2", T + 400, 0.45, 60), tr("s3", T + 420, 0.40, 60)]
        eng.step(T + 450, mobs(T + 450, quotes={"1": (0.40, 0.42)}, trades={"1": sweep}), [])
        filled = sum(f.qty for f in eng.fills() if f.action == "buy")
        return cancel_at, filled, eng.portfolios()[0].pnl_liq

    h_cancel, h_filled, h_pnl = run(human())
    assert h_cancel == T + 360 + HUMAN_LATENCY_S  # a person needs ~4 minutes to cancel by hand
    assert h_filled == 100 and h_pnl < -20  # 100 @0.62 with the market at 0.40
    b_cancel, b_filled, b_pnl = run(bot())
    assert b_cancel == T + 362 and b_filled == 0 and b_pnl == pytest.approx(0.0)


def test_fills_2_a_pending_cancel_is_read_like_a_resting_order_until_it_lands() -> None:
    eng, _ = make_engine([human()])
    q = {"1": (0.93, 0.94)}
    eng.step(T, mobs(T, quotes=q), [hole()])
    eng.step(T + 30, mobs(T + 30, quotes=q), [])
    eng.step(T + 60, mobs(T + 60, quotes=q, trades={"1": []}), [])  # cancel decided now, lands at T + 300
    assert eng.orders()[0].status == "cancelling" and eng.orders()[0].cancel_at == T + 300
    plan = eng.wanted_reads(T + 90, mobs(T + 90, quotes=q))
    assert not any(r.kind == "trades" and r.reason == "tape since a cancelled resting order" for r in plan.requests)
    plan = eng.wanted_reads(T + 300, mobs(T + 300, quotes=q))
    assert any(r.kind == "trades" and r.reason == "tape since a cancelled resting order" for r in plan.requests)
    eng.step(T + 300, mobs(T + 300, quotes=q, trades={"1": []}), [])
    assert eng.orders() == [] and all_orders(eng)[0].status == "cancelled"


def test_fills_3_an_empty_open_list_freezes_every_position() -> None:
    def run(open_ids: List[str]) -> Any:
        eng, _ = make_engine(policy=FixedPolicy(units=300))
        eng.step(T, mobs(T), [opp(limit=0.535)])
        eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.50, 2000)], asks=[(0.52, 1000)])]), [])
        eng.step(T + 60, mobs(T + 60, quotes={"1": (0.55, 0.57), "2": (0.30, 0.32)},
                              books=[bk("1", T + 59, bids=[(0.55, 1000)], asks=[(0.57, 1000)])]), [])
        stale = {"1": (0.55, 0.57, T + 60), "2": (0.30, 0.32, T + 60)}  # the view keeps its last rows
        eng.step(T + 600, mobs(T + 600, quotes=stale, open_ids=open_ids), [])
        return eng.positions()[0], eng.portfolios()[0]

    for open_ids in (["2"], []):
        pos, s = run(open_ids)
        assert pos.status == "frozen" and "closed_no_ruling" in pos.flags, open_ids
        assert s.equity_liq == pytest.approx(100_000 - 156.0) and s.unvalued == pytest.approx(165.0)
        assert " 1 position whose market closed without a ruling is left out (last value 165)." in s.verdict.sentence
    # a bare observation without an open list still treats every quoted outcome as open
    assert MarketObservation(now=T, cup_end=CUP).open_ids is None


def test_fills_4_a_newer_real_book_beats_a_stale_quote() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    eng.step(T, mobs(T), [opp(limit=0.535)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.50, 2000)], asks=[(0.52, 1000)])]), [])
    eng.step(T + 300, mobs(T + 300, quotes={"1": (0.60, 0.62)},
                           books=[bk("1", T + 299, bids=[(0.60, 1000)], asks=[(0.62, 1000)])]), [])
    assert eng.positions()[0].liq_value == pytest.approx(180.0)
    t = T + 1500  # the bulk quote is stuck at the T + 300 one; the paper worker's own book read shows the fall
    eng.step(t, mobs(t, quotes={"1": (0.60, 0.62, T + 300)},
                     books=[bk("1", t - 1, bids=[(0.45, 300), (0.44, 300)], asks=[(0.47, 300)])]), [])
    (pos,) = eng.positions()
    assert pos.liq_value == pytest.approx(135.0) and pos.depth_state == "fresh" and "stale_quote" in pos.flags
    assert eng.portfolios()[0].pnl_liq == pytest.approx(135.0 - 156.0)
    # the newer book shows no bids at all: worth 0, not the unknown rule at the stale 0.60 touch
    t2 = t + 30
    eng.step(t2, mobs(t2, quotes={"1": (0.60, 0.62, T + 300)}, books=[bk("1", t2 - 1, asks=[(0.05, 5000)])]), [])
    assert eng.positions()[0].liq_value == pytest.approx(0.0)


def test_fills_4_liquidation_value_prefers_the_newer_source() -> None:
    cfg = PaperConfig()
    stale_q = Quote("1", 0.60, 0.62, None, T - 1200)
    fresh = bk("1", T - 5, bids=[(0.45, 300), (0.44, 300)], asks=[(0.47, 300)])
    assert P.liquidation_value("yes", 300, fresh, stale_q, now=T, config=cfg) == (pytest.approx(135.0), "fresh")
    # a fresh quote NEWER than the book still judges the book (an out-of-sync book is shifted to the touch)
    new_q = Quote("1", 0.60, 0.62, None, T)
    v, state = P.liquidation_value("yes", 300, fresh, new_q, now=T, config=cfg)
    assert state == "stale" and v == pytest.approx(300 * 0.60)
    # a book read after a (fresh) quote defines the touch
    after = bk("1", T, bids=[(0.45, 300)])
    assert P.liquidation_value("yes", 300, after, Quote("1", 0.60, 0.62, None, T - 10), now=T,
                               config=cfg) == (pytest.approx(135.0), "fresh")


def _depth_unknown_run() -> P.PaperEngine:
    """40 ideas on 40 races in 10 two-hour blocks x {D, R} (G = 20), 20 covered hours, every idea +5..+6 at
    liquidation, but marked without a recent book: 27% of equity "unknown" -> the guard makes it inconclusive."""
    eng, _ = make_engine(policy=FixedPolicy(units=100), start_capital=8_000.0, target_hours=20.0,
                         max_new_orders_per_step=10, max_open_positions=60, interval_s=600.0)
    eids = [f"E{k:02d}" for k in range(40)]
    entry = {e: T + (k // 4) * 7200 + 60 + (k % 4) for k, e in enumerate(eids)}
    touch = {e: 0.55 + 0.005 * (k % 3) for k, e in enumerate(eids)}
    times = sorted({T + 1800 * i for i in range(41)} | {entry[e] for e in eids} | {entry[e] + 30 for e in eids})
    for t in times:
        quotes = {e: ((0.48, 0.50) if t <= entry[e] + 30 + 1e-6 else (touch[e], touch[e] + 0.02)) for e in eids}
        books = [bk(e, t - 5, bids=[(0.48, 500)], asks=[(0.50, 500)]) for e in eids if t == entry[e]]
        books += [bk(e, t - 1, bids=[(0.49, 500)], asks=[(0.50, 500)]) for e in eids if t == entry[e] + 30]
        signals = [opp(e, limit=0.50, idea=f"value:{e}", race_key=f"race-{e}",
                       factor_delta=0.01 if k % 2 == 0 else -0.01) for k, e in enumerate(eids) if t == entry[e]]
        eng.step(t, mobs(t, quotes=quotes, books=books), signals)
    return eng


def test_accounting_1_end_run_keeps_the_verdict_and_the_closed_open_split() -> None:
    eng = _depth_unknown_run()
    before = eng.summary()
    head = before["headline"]["verdict"]
    assert head["level"] == "inconclusive" and head["depth_unknown_share"] > P.MAX_DEPTH_UNKNOWN_SHARE
    assert head["n_ideas"] == 40 and head["clusters"] == 20 and head["open_ideas"] == 40
    port_before = before["portfolios"][0]
    t_end = eng._now + 30
    snap = eng.end_run(t_end, "completed")
    after = eng.summary()
    assert snap["verdicts"]["bot"]["level"] == "inconclusive"
    # what `paper --hours N` prints and /api/paper serves once the run has ended: the snapshot taken before the
    # positions were booked as trades at liquidation value (no "realised" open P&L, the depth guard still applies)
    same = {k: v for k, v in after["headline"]["verdict"].items() if k != "wall_hours"}
    assert same == {k: v for k, v in head.items() if k != "wall_hours"}
    port_after = after["portfolios"][0]
    for key in ("trades_closed", "positions_open", "wins", "win_rate", "realized_pnl", "pnl_liq", "depth_unknown_share"):
        assert port_after[key] == port_before[key], key
    assert after["run"]["end_reason"] == "completed" and after["run"]["final"]["verdicts"]["bot"] == after["headline"]["verdict"]
    assert eng.verdicts()["bot"].level == "inconclusive" and eng.portfolios()[0].trades_closed == 0
    # the liquidation trades exist, but they are not closed ideas
    assert {t.exit_reason for t in eng.trades()} == {"completed"} and len(eng.trades()) == 40
    assert all(not i.closed for i in eng.ideas("bot"))


def _basket_ab(eng: P.PaperEngine) -> float:
    """A 2-leg YES basket (A 100 @0.45 + B 100 @0.50 = 0.95) converges: exit orders at A 0.52 / B 0.49 at T + 60."""
    plan = ExitPlan(kind="basket", min_set_profit=0.02)
    idea = basket(legs=(("A", "yes", 0.46), ("B", "yes", 0.51)), idea="basket:A+B:yy", edge=0.05, plan=plan)
    q0 = {"A": (0.43, 0.45), "B": (0.48, 0.50)}
    eng.step(T, mobs(T, quotes=q0, books=[bk("A", T - 10, bids=[(0.43, 500)], asks=[(0.45, 300)]),
                                          bk("B", T - 10, bids=[(0.48, 500)], asks=[(0.50, 300)])]), [idea])
    eng.step(T + 30, mobs(T + 30, quotes=q0, books=[bk("A", T + 29, bids=[(0.43, 500)], asks=[(0.45, 300)]),
                                                    bk("B", T + 29, bids=[(0.48, 500)], asks=[(0.50, 300)])]), [])
    assert {p.exchange_id: p.qty for p in eng.positions()} == {"A": 100, "B": 100}
    eng.step(T + 60, mobs(T + 60, quotes={"A": (0.52, 0.54), "B": (0.49, 0.51)},
                          books=[bk("A", T + 59, bids=[(0.52, 1000)], asks=[(0.54, 300)]),
                                 bk("B", T + 59, bids=[(0.49, 1000)], asks=[(0.51, 300)])]), [])
    assert {o.exchange_id: o.limit_price for o in eng.orders()} == {"A": 0.52, "B": 0.49}
    return T + 60


def _step_ab(eng: P.PaperEngine, t: float, a_bids: Sequence[Tuple[float, float]], b_bids: Sequence[Tuple[float, float]]) -> None:
    qa, qb = a_bids[0][0], b_bids[0][0]
    eng.step(t, mobs(t, quotes={"A": (qa, qa + 0.02), "B": (qb, qb + 0.02)},
                     books=[bk("A", t - 1, bids=a_bids, asks=[(qa + 0.02, 300)]),
                            bk("B", t - 1, bids=b_bids, asks=[(qb + 0.02, 300)])]), [])


def test_accounting_2_a_partly_filled_lagging_leg_is_legged_out() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=100))
    t = _basket_ab(eng) + 30
    _step_ab(eng, t, [(0.52, 1000)], [(0.49, 40), (0.30, 1000)])  # A sells 100, B only 40 of 100
    assert {p.exchange_id: p.qty for p in eng.positions()} == {"B": 60}
    for _ in range(8):  # B's bid falls to 0.30; its exit order runs out after 120 s
        t += 30
        _step_ab(eng, t, [(0.52, 1000)], [(0.30, 1000)])
    assert eng.positions() == [] and eng.orders() == []
    (trade,) = eng.trades()
    assert trade.exit_reason == "legging" and trade.pnl == pytest.approx(100 * 0.52 + 40 * 0.49 + 60 * 0.30 - 95.0)
    assert any(o.reason == "Legging out: selling every remaining leg" for o in all_orders(eng))


def test_accounting_2_and_6_both_legs_partly_filled() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=100))
    t = _basket_ab(eng) + 30
    _step_ab(eng, t, [(0.52, 50), (0.40, 1000)], [(0.49, 40), (0.30, 1000)])  # A sells 50, B 40
    assert {p.exchange_id: p.qty for p in eng.positions()} == {"A": 50, "B": 60}
    (row,) = eng.summary()["baskets"]
    # the floor is the matched sets' (50 x 1.00 = 50, below the 52.50 cost); the 10 extra B shares are naked
    assert row["sets"] == 50 and row["floor_value"] == pytest.approx(50.0) and row["cost"] == pytest.approx(52.5)
    assert row["naked_qty"] == 10 and {lg["exchange_id"]: lg["naked_qty"] for lg in row["legs"]} == {"A": 0, "B": 10}
    for _ in range(8):
        t += 30
        _step_ab(eng, t, [(0.40, 1000)], [(0.30, 1000)])
    assert eng.positions() == [] and eng.trades()[0].exit_reason == "legging"


def test_accounting_2_equal_partial_fills_stay_a_basket() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=100))
    t = _basket_ab(eng) + 30
    _step_ab(eng, t, [(0.52, 50), (0.40, 1000)], [(0.49, 50), (0.30, 1000)])  # 50 of each: still 50 whole sets
    for _ in range(8):
        t += 30
        _step_ab(eng, t, [(0.40, 1000)], [(0.30, 1000)])  # the touch moved away: no exit rule fires
    assert {p.exchange_id: p.qty for p in eng.positions()} == {"A": 50, "B": 50} and eng.orders() == []
    assert not any(o.reason == "Legging out: selling every remaining leg" for o in all_orders(eng))
    (row,) = eng.summary()["baskets"]
    assert row["sets"] == 50 and row["floor_value"] == pytest.approx(50.0) and row["naked_qty"] == 0


def test_accounting_6_a_basket_with_a_sold_leg_has_no_floor() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=100))
    t = _basket_ab(eng) + 30
    _step_ab(eng, t, [(0.52, 1000)], [(0.49, 40), (0.30, 1000)])
    (row,) = eng.summary()["baskets"]
    assert row["legs_total"] == 2 and row["sets"] == 0 and row["floor_value"] == 0 and row["naked_qty"] == 60


def test_accounting_4_assumed_fills_never_lift_the_verdict() -> None:
    ideas = []
    k = 0
    for blk in range(10):
        for d in ("D", "R"):
            for j in range(2):
                race = "race-BIG" if (blk, d, j) in ((0, "D", 0), (3, "R", 0), (6, "D", 0)) else f"race-{k}"
                pnl = 20.0 if race == "race-BIG" else 1.0 + 0.1 * (k % 3)
                ideas.append(P.IdeaOutcome(f"i{k}", "value", race, T + blk * 7200 + 60 * j, d, False, pnl, 50.0))
                k += 1
    real = sum(i.pnl for i in ideas)
    synth = [P.IdeaOutcome(f"s{j}", "value", f"race-s{j}", T + 100, "D", True, 20.0, 50.0, synthetic=True)
             for j in range(5)]
    a = _verdict(ideas, pnl_liq=real, hours=20.0, days={})
    b = _verdict(ideas + synth, pnl_liq=real + 100.0, hours=20.0, days={})
    assert a.level == b.level == "inconclusive" and b.top_race_share == pytest.approx(a.top_race_share)
    assert b.pnl_liq == pytest.approx(real) and b.synthetic_pnl == pytest.approx(100.0) and b.synthetic_excluded == 5
    assert b.sentence == a.sentence and "one race supplies 60%" in b.sentence
    assert any("their +100 is not in the P&L above" in r for r in b.reasons)


def test_accounting_4_engine_states_the_pnl_of_the_ideas_it_counts() -> None:
    eng, _ = make_engine()
    q = {"R": (0.48, 0.50), "S": (0.48, 0.50)}
    eng.step(T, mobs(T, quotes=q), [opp("R", limit=0.50, idea="value:R"), opp("S", limit=0.50, idea="value:S")])
    eng.step(T + 30, mobs(T + 30, quotes=q, books=[bk("R", T + 29, bids=[(0.48, 500)], asks=[(0.50, 500)]),
                                                   bk("S", T + 29, bids=[(0.48, 500)], asks=[(0.50, 500)], source="synthetic")]), [])
    settle = {"S": SettlementInfo("S", "mS", "YES", T + 60, 1.0, False, T + 60)}
    eng.step(T + 60, mobs(T + 60, quotes={"R": (0.50, 0.52)}, books=[bk("R", T + 59, bids=[(0.50, 500)])],
                          settlements=settle, open_ids=["R"]), [])
    s = eng.portfolios()[0]
    assert s.pnl_liq == pytest.approx(50.0)  # the portfolio: the synthetic fill settled +50
    v = s.verdict
    assert v.n_ideas == 1 and v.synthetic_excluded == 1 and v.pnl_liq == pytest.approx(0.0)
    assert "P&L so far +0 SUSQies" in v.sentence and v.synthetic_pnl == pytest.approx(50.0)


def test_accounting_5_equity_does_not_rise_when_an_exit_sells_into_the_only_rung() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=2000))
    plan = ExitPlan(kind="fade", stop_bid=0.45)
    q = {"X": (0.48, 0.50)}
    eng.step(T, mobs(T, quotes=q, books=[bk("X", T - 5, bids=[(0.48, 3000)], asks=[(0.50, 3000)])]),
             [opp("X", limit=0.50, kind="fade", plan=plan)])
    eng.step(T + 30, mobs(T + 30, quotes=q, books=[bk("X", T + 29, bids=[(0.48, 3000)], asks=[(0.50, 3000)])]), [])
    equity = []
    for k in range(1, 8):  # one thin rung of 500 @0.40, re-quoted by the house every step
        t = T + 30 + 30 * k
        eng.step(t, mobs(t, quotes={"X": (0.40, 0.42)}, books=[bk("X", t - 1, bids=[(0.40, 500)], asks=[(0.42, 3000)])]), [])
        equity.append(eng.portfolios()[0].equity_liq)
    assert sum(f.qty for f in eng.fills() if f.action == "sell") == 500
    assert eng.positions()[0].qty == 1500 and eng.positions()[0].liq_value == pytest.approx(0.0)
    assert equity[0] == pytest.approx(100_000 - 1000 + 200) and max(equity) <= equity[0] + 1e-6


def test_accounting_7_by_kind_rows_add_up_while_a_basket_is_half_closed() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=100))
    t = _basket_ab(eng) + 30
    _step_ab(eng, t, [(0.52, 1000)], [(0.49, 40), (0.45, 1000)])  # leg A closed (waiting in closing), B 60 open
    assert book_of(eng).closing
    s = eng.portfolios()[0]
    assert sum(r["pnl_liq"] for r in s.by_kind.values()) == pytest.approx(s.pnl_liq)
    assert s.by_kind["basket"]["pnl_liq"] == pytest.approx(s.pnl_liq)
    # a frozen position: realised + unrealised = pnl_liq (its cost is the unrealised loss while it counts 0)
    eng2, _ = make_engine()
    _hold(eng2, ExitPlan(kind="value"))
    eng2.step(T + 60, mobs(T + 60, quotes={}, open_ids=[]), [])
    s2 = eng2.portfolios()[0]
    assert s2.realized_pnl + s2.unrealized_pnl_liq == pytest.approx(s2.pnl_liq) == pytest.approx(-52.0)
    assert s2.by_kind["value"]["pnl_liq"] == pytest.approx(-52.0)


def test_strategy_9_a_longshot_exit_target_uses_the_shrunk_fair_value() -> None:
    plan = ExitPlan(kind="value", dynamic_fv_target=True, exit_buffer=0.005, target_bid=0.70)
    eng, _ = make_engine()
    _hold(eng, plan, fair_value=0.13)
    fv = {"1": FairValue("1", 0.13, "polymarket", usable=True, as_of=T + 55)}
    _, (o,) = _exit_after(eng, T + 60, {"1": (0.105, 0.11)}, fair_values=fv)
    # q = 0.13 x 0.9 = 0.117: target floor_tick(0.117 - 0.0025 - 0.005) = 0.105 (the card's), not 0.12
    assert o.planned_price == 0.105 and o.limit_price == 0.10
    assert o.reason == "The price converged: selling banks the edge"


def test_live_2_steps_without_fresh_quotes_add_no_covered_hours() -> None:
    eng, _ = make_engine()
    for k in range(4):
        eng.step(T + 30 * k, mobs(T + 30 * k), [])
    covered = eng.summary()["run"]["hours_run"]
    assert covered == pytest.approx(90 / 3600, abs=1e-6)
    # an outage: the loop keeps stepping, but every quote is the one from before it (no snapshot succeeded)
    for k in range(4, 124):
        t = T + 30 * k
        eng.step(t, mobs(t, quotes={"1": (0.50, 0.52, T + 90)}), [])
    run = eng.summary()["run"]
    assert run["hours_run"] == pytest.approx(covered + 120 / 3600, abs=1e-6)  # only while the quote was <= 120 s old
    assert run["wall_hours"] == pytest.approx(123 * 30 / 3600)
    (gap,) = run["gaps"]
    assert gap["reason"] == P.NO_DATA_GAP and gap["to"] == T + 30 * 123
    # data again: coverage resumes, the gap stays closed
    eng.step(T + 30 * 124, mobs(T + 30 * 124), [])
    assert eng.summary()["run"]["hours_run"] == pytest.approx(covered + 150 / 3600, abs=1e-6)
    assert len(eng.summary()["run"]["gaps"]) == 1
    c = P.CoverageClock.from_dict(eng.state()["coverage"])
    assert c.gaps[0]["reason"] == P.NO_DATA_GAP and c == eng._coverage


def test_live_2_coverage_clock_no_data_ticks() -> None:
    c = P.CoverageClock(interval_s=30.0)
    c.tick(T)
    c.tick(T + 30)
    assert c.tick(T + 60, fresh=False) is None and c.tick(T + 90, fresh=False) is None
    assert c.covered_s == pytest.approx(30.0) and c.gaps == [{"from": T + 30, "to": T + 90, "reason": P.NO_DATA_GAP}]
    c.tick(T + 120)
    assert c.covered_s == pytest.approx(60.0) and c.no_data_open is False
    c.tick(T + 150, fresh=False)
    assert len(c.gaps) == 2


def _no_fv_basket_run(specs: Sequence[PortfolioSpec]) -> P.PaperEngine:
    eng, _ = make_engine(list(specs), policy=FixedPolicy(units=300), headline_portfolio=specs[0].portfolio_id)
    _enter_set(eng)  # basket fills; no outside fair value on any step
    return eng


def test_live_3_untested_value_ideas_are_named_even_with_fills() -> None:
    eng = _no_fv_basket_run([bot("human:conservative")])
    s = eng.portfolios()[0]
    assert s.fills > 0 and s.no_trade_reason is None
    note = ("Value ideas were not tested: usable outside fair values on 0% of steps (outside prices off, offline or not "
            "received yet), so this result covers basket, hole, fade, carry and arbitrage ideas only.")
    assert s.untested == [note] and s.verdict.untested == [note]
    assert s.verdict.sentence.endswith(" " + note) and note in s.verdict.reasons
    # a portfolio that never trades value ideas has nothing untested; fair values usable on most steps: no note
    eng2 = _no_fv_basket_run([bot("kind:basket", kinds=["basket"])])
    assert eng2.portfolios()[0].untested == []
    eng3, _ = make_engine(policy=FixedPolicy(units=300))
    fv = {"1": FairValue("1", 0.5, "polymarket", usable=True, as_of=T)}
    eng3.step(T, mobs(T, quotes=NV, books=_set_books(T - 1), fair_values=fv), [basket()])
    assert eng3.portfolios()[0].untested == []


def test_ui_3_the_headline_carries_the_untested_note() -> None:
    eng = _no_fv_basket_run([bot("human:conservative"), bot("kind:value", kinds=["value"])])
    body = eng.summary()
    head = body["headline"]
    assert head["portfolio_id"] == "human:conservative" and len(head["untested"]) == 1
    assert head["untested"][0].startswith("Value ideas were not tested") and head["untested"] == head["verdict"]["untested"]
    value_row = [p for p in body["portfolios"] if p["portfolio_id"] == "kind:value"][0]
    assert value_row["untested"] == ["Value ideas were not tested: usable outside fair values on 0% of steps (outside "
                                     "prices off, offline or not received yet), so this portfolio could not test its only kind."]


def test_ui_6_the_served_equity_series_ends_at_the_current_equity() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value"))
    eng.step(T + 60, mobs(T + 60, books=[bk("1", T + 59, bids=[(0.52, 500)])]), [])
    eng.step(T + 90, mobs(T + 90, quotes={"1": (0.40, 0.42)}, books=[bk("1", T + 89, bids=[(0.40, 500)])]), [])
    assert eng.equity()["bot"][-1].ts == T + 60  # stored at most once a minute (unchanged)
    body = eng.summary()
    last = body["equity"]["bot"][-1]
    assert last[0] == T + 90 and last[1] == pytest.approx(body["headline"]["equity_liq"])
    assert last[1] == pytest.approx(100_000 - 52 + 40)


def test_ui_7_the_win_rate_counts_closed_ideas_not_legging_fragments() -> None:
    eng, _ = make_engine(policy=FixedPolicy(units=300))
    _enter_set(eng, b2_fill=200)
    eng.step(T + 60, mobs(T + 60, quotes=NV, books=[bk("1", T + 59, bids=[(0.52, 1)], asks=[(0.54, 1000)])]), [])
    (residue,) = eng.trades()
    assert residue.exit_reason == "legging"  # a fragment of the basket idea, which is still open
    s = eng.portfolios()[0]
    v = s.verdict
    assert v.closed_trades == 0 and v.open_ideas == 1 and s.trades_closed == 0 and s.win_rate is None
    assert s.legging_trades == 1 and s.legging_pnl == pytest.approx(-2.0)
    assert "No closed ideas yet, so no win rate (closed trades only: biased toward quick winners)" in v.reasons
    assert "1 legging exit of sets still held: -2 (part of those open ideas, not closed ideas)" in v.reasons
    assert s.by_kind["basket"]["trades_closed"] == 0


class BoldPolicy(FixedPolicy):
    """FixedPolicy whose every decision is the chaser's bold-to-goal bet."""

    def size(self, opp: Opportunity, ctx: Any) -> SizeDecision:
        d = super().size(opp, ctx)
        d.bold = True
        return d


def test_strategy_3_a_bold_value_position_is_held_to_settlement() -> None:
    from supermarket_bot import sizing

    plan = ExitPlan(kind="value", target_bid=0.55, stop_bid=0.45, dynamic_fv_target=True, exit_buffer=0.005,
                    time_stop_after_fill_s=60.0)
    idea = opp(plan=plan, fair_value=0.60)
    eng, _ = make_engine(policy=BoldPolicy(units=100))
    eng.step(T, mobs(T), [idea])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.52, 500)], asks=[(0.52, 500)])]), [])
    (pos,) = eng.positions()
    assert pos.bold and pos.exit_plan.hold_to_resolution and pos.exit_plan.kind == "settle"
    assert pos.exit_plan.target_bid is None and pos.exit_plan.stop_bid is None and not pos.exit_plan.dynamic_fv_target
    assert idea.exit_plan is plan and plan.target_bid == 0.55 and plan.stop_bid == 0.45  # the shared idea is untouched
    fv = {"1": FairValue("1", 0.60, "polymarket", usable=True, as_of=T + 55)}
    # past the idea's target, its dynamic fair-value target and its time stop: a bold stake is not sold for cents
    _, exits = _exit_after(eng, T + 120, {"1": (0.62, 0.63)}, fair_values=fv)
    assert exits == []
    _, exits = _exit_after(eng, T + 150, {"1": (0.40, 0.42)})  # through the stop: still held
    assert exits == []
    (row,) = eng.summary()["positions"]
    assert row["exit_note"] == sizing.BOLD_HOLD_NOTE and "bold" in row["flags"]
    eng.step(T + 180, mobs(T + 180, quotes={}, open_ids=["x"], settlements=_settle(1.0)), [])
    (trade,) = eng.trades()
    assert trade.exit_reason == "settled" and trade.pnl == pytest.approx(100 * (1.0 - 0.52))
    # the same idea sized without bold keeps its own plan and is sold at its target
    eng2, _ = make_engine()
    _hold(eng2, plan)
    _, (o,) = _exit_after(eng2, T + 60, {"1": (0.62, 0.63)})
    assert o.purpose == "exit" and o.reason != ""


def test_strategy_3_a_bold_position_keeps_the_settlement_regime_exit() -> None:
    plan = ExitPlan(kind="value", target_bid=0.55, exit_before_ts=T + 80)
    eng, _ = make_engine(policy=BoldPolicy(units=100))
    eng.step(T, mobs(T), [opp(plan=plan)])
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, bids=[(0.52, 500)], asks=[(0.52, 500)])]), [])
    assert eng.positions()[0].exit_plan.exit_before_ts == T + 80
    _, exits = _exit_after(eng, T + 60, {"1": (0.60, 0.61)})
    assert exits == []
    _, (o,) = _exit_after(eng, T + 90, {"1": (0.60, 0.61)})
    assert o.reason == "Flat before the Cup's closeout window (settlement rule)" and o.limit_price == 0.005


def test_accounting_8_a_frozen_row_shows_what_the_totals_charge() -> None:
    eng, _ = make_engine()
    _hold(eng, ExitPlan(kind="value"))
    eng.step(T + 60, mobs(T + 60, books=[bk("1", T + 59, bids=[(0.50, 500)])]), [])
    eng.step(T + 90, mobs(T + 90, quotes={}, open_ids=[]), [])  # closed without a ruling
    s = eng.portfolios()[0]
    (row,) = eng.summary()["positions"]
    assert row["status"] == "frozen" and row["unvalued"] is True and "closed_no_ruling" in row["flags"]
    # the row shows what the totals charge (D45: counted 0), the last mark separately
    assert row["liq_value"] == 0.0 and row["last_liq_value"] == pytest.approx(50.0)
    assert row["unrealized_liq"] == pytest.approx(-52.0) == pytest.approx(s.pnl_liq)
    assert s.realized_pnl + s.unrealized_pnl_liq == pytest.approx(s.pnl_liq) and s.unvalued == pytest.approx(50.0)


def _equity_rows(eng: P.PaperEngine, pid: str) -> List[float]:
    rid = eng.summary()["run"]["run_id"]
    return [float(r["liq_value"]) for r in eng.persistence.paper_equity(rid, pid)]


def test_ui_4_a_default_capital_run_is_rebased_before_any_fill() -> None:
    eng, pol = make_engine([bot(), human()])
    eng.step(T, mobs(T), [opp()], StrategyInputs(now=T, cup_end=CUP))  # the account is not known yet
    run = eng.summary()["run"]
    assert run["capital_source"] == P.DEFAULT_CAPITAL_SOURCE and len(eng.orders()) == 2
    account = StrategyInputs(now=T + 30, cup_end=CUP, account_value=60_000.0)
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [opp()], account)
    run = eng.summary()["run"]
    assert run["start_capital"] == 60_000.0 and run["capital_source"] == "account value"
    assert all(p.start_capital == 60_000.0 and p.pnl_liq == 0.0 for p in eng.portfolios())
    withdrawn = [o for o in all_orders(eng) if o.status == "cancelled"]
    assert len(withdrawn) == 2 and all(o.close_reason.startswith("Start capital set from your account value (60,000 "
                                                                 "SUSQies)") for o in withdrawn)
    # the old orders never fill; the idea is placed again, sized on the account
    assert eng.fills() == [] and {o.created_at for o in eng.orders()} == {T + 30}
    assert pol.calls[-1][1].start_capital == 60_000.0 and pol.calls[-1][1].equity == pytest.approx(60_000.0)
    assert set(_equity_rows(eng, "bot")) == {60_000.0} and set(_equity_rows(eng, "human:fixed")) == {60_000.0}
    assert all(pt[1] == 60_000.0 for pt in eng.summary()["equity"]["bot"])
    # persisted: a restart continues the re-based run
    eng2 = P.PaperEngine(eng.config, persistence=eng.persistence, policies={"fixed": pol}, clock=lambda: T,
                         code_version="test")
    assert eng2.summary()["run"]["capital_source"] == "account value"
    assert {o.status for o in eng2.orders(open_only=False) if o.created_at == T} == {"cancelled"}


def test_ui_4_no_rebase_once_anything_filled_or_in_a_replay() -> None:
    eng, _ = make_engine()
    none = StrategyInputs(now=T, cup_end=CUP)
    eng.step(T, mobs(T), [opp()], none)
    eng.step(T + 30, mobs(T + 30, books=[bk("1", T + 29, asks=[(0.52, 500)])]), [], none)
    assert len(eng.fills()) == 1
    eng.step(T + 60, mobs(T + 60), [], StrategyInputs(now=T + 60, cup_end=CUP, account_value=60_000.0))
    assert eng.summary()["run"]["capital_source"] == P.DEFAULT_CAPITAL_SOURCE  # the run is what it is
    assert eng.portfolios()[0].start_capital == 100_000.0
    pol = FixedPolicy()
    replay = P.PaperEngine(PaperConfig(portfolios=[bot()]), persistence=P.MemoryPaperPersistence(),
                           policies={"fixed": pol}, clock=lambda: T, rebase_default_capital=False)
    replay.start(T, P.DEFAULT_START_CAPITAL, P.DEFAULT_CAPITAL_SOURCE)
    replay.step(T, mobs(T), [opp()], StrategyInputs(now=T, cup_end=CUP, initial_balance=50_000.0))
    assert replay.summary()["run"]["start_capital"] == 100_000.0
    # a capital set by you is never replaced
    mine, _ = make_engine(start_capital=5_000.0)
    mine.step(T, mobs(T), [opp()], StrategyInputs(now=T, cup_end=CUP, account_value=60_000.0))
    assert mine.summary()["run"]["capital_source"] == "set by you"


def test_accounting_1_the_cli_end_of_run_text_matches_the_previous_run(tmp_path: Any) -> None:
    import io
    from pathlib import Path

    from supermarket_bot import cli, pipeline, web
    from supermarket_bot.demo import SIM_T0, SimClock

    clock = SimClock(SIM_T0)
    rt = web.build_demo(Path(tmp_path), 30.0, news=False, out=io.StringIO(), clock=clock, seed=7, target_hours=0.75)
    try:
        pipeline.run_simulation(rt, clock, hours=0.75, step_s=30.0, summary_every_s=3600.0, on_summary=None,
                                end_run=True)
        now_body = rt.app.paper()  # what `paper --hours N` prints (text and --json) after the end
        prev_body = rt.app.paper(run="previous")  # the dashboard's "previous run": the persisted final snapshot
        assert now_body["run"]["end_reason"] == "completed" and prev_body["run"]["run_id"] == now_body["run"]["run_id"]
        keys = ("pnl_liq", "trades_closed", "positions_open", "win_rate", "realized_pnl", "verdict")
        prev_rows = {p["portfolio_id"]: p for p in prev_body["portfolios"]}
        for p in now_body["portfolios"]:
            for k in keys:
                if k in prev_rows[p["portfolio_id"]]:
                    assert p[k] == prev_rows[p["portfolio_id"]][k], (p["portfolio_id"], k)
        assert now_body["headline"]["verdict"] == prev_body["headline"]["verdict"]
        now_lines = [x for x in cli.paper_final_lines(now_body) if x.startswith("  ") and "verdict" not in x.lower()]
        verdict_lines = [x for x in cli.paper_final_lines(now_body) if "(headline)" in x or "(exploratory)" in x]
        prev_verdicts = [x for x in cli.paper_final_lines(prev_body) if "(headline)" in x or "(exploratory)" in x]
        assert verdict_lines and verdict_lines == prev_verdicts
        assert now_lines  # the per-portfolio lines exist (closed / open come from the final snapshot)
    finally:
        rt.close()


def test_fills_2_a_downtime_cancel_stays_at_the_last_step_before_the_gap() -> None:
    """Gap-cancel (phase 0, D48, binding): unlike a cancel decided while the bot runs (which lands after the
    portfolio's latency, fills-2), a resting order counts as cancelled at the last step before the gap, for every
    portfolio, so prints from the downtime never fill it."""
    def run(spec: PortfolioSpec) -> Tuple[float, float]:
        eng, _ = make_engine([spec])
        q = {"1": (0.93, 0.94)}
        eng.step(T, mobs(T, quotes=q), [hole()])
        eng.step(T + 300, mobs(T + 300, quotes=q, trades={"1": []}), [hole()])  # live (past 240 s), last step before the gap
        gap_end = T + 300 + 10 * 3600
        tape = [tr("soon", T + 400, 0.55, 60), tr("later", T + 3600, 0.40, 500)]
        eng.step(gap_end, mobs(gap_end, quotes=q, trades={"1": tape}), [hole()])
        closed = [o for o in all_orders(eng) if o.status == "cancelled"][0]
        return closed.cancel_at, sum(f.qty for f in eng.fills() if f.action == "buy")

    h_cancel, h_filled = run(human())
    assert h_cancel == T + 300 and h_filled == 0
    b_cancel, b_filled = run(bot())
    assert b_cancel == T + 300 and b_filled == 0
