"""Package C (study): signal-level event study: episodes, horizons, converged/reversed, summary intervals, state. See docs/PAPER_TRADING.md §6.14 and §6.16."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import paper as P
from supermarket_bot import study as S
from supermarket_bot.models import (
    BetShape,
    ExchangeInfo,
    ExitPlan,
    FairValue,
    MarketObservation,
    Opportunity,
    PaperConfig,
    PortfolioSpec,
    Quote,
    SizeDecision,
)

T = 1_790_000_000.0
CUP = 1_793_811_600.0


def obs(t: float, quotes: Dict[str, Tuple[float, float]], fv: Optional[Dict[str, float]] = None,
        ts: Optional[float] = None) -> MarketObservation:
    q = {e: Quote(e, b, a, None, t if ts is None else ts) for e, (b, a) in quotes.items()}
    fvs = {e: FairValue(e, v, "polymarket", usable=True, as_of=t) for e, v in (fv or {}).items()}
    return MarketObservation(now=t, cup_end=CUP, quotes=q, open_ids=set(q), fair_values=fvs,
                             infos={e: ExchangeInfo(e, "m" + e, None, "x") for e in q})


def value(eid: str = "1", side: str = "yes", fv: float = 0.60, idea: Optional[str] = None,
          race: Optional[str] = "2026:SENATE:TX", delta: float = 0.05) -> Opportunity:
    return Opportunity(kind="value", exchange_id=eid, market_id="m" + eid, title="x", option=None, side=side,
                       entry_price=0.52, target_price=None, stop_price=None, prob_win=0.6, edge=0.05,
                       expected_return=0.1, horizon_hours=1.0, suggested_shares=0, suggested_cost=0.0, score=1.0,
                       confidence=0.6, idea_id=idea or f"value:x{eid}:{side}", race_key=race, fair_value=fv,
                       limit_price=0.535, factor_delta=delta, bet=BetShape("binary", 0.6, 0.48, 0.52, 0.52),
                       exit_plan=ExitPlan(kind="value"))


def basket(side: str = "no", idea: str = "basket:1+2") -> Opportunity:
    legs = [{"exchange_id": e, "market_id": "m" + e, "side": side, "price": 0.0, "limit": 0.0} for e in ("1", "2")]
    return Opportunity(kind="basket", exchange_id=None, market_id="m1", title="set", option=None, side=side,
                       entry_price=0.95, target_price=None, stop_price=None, prob_win=1.0, edge=0.05,
                       expected_return=0.05, horizon_hours=1.0, suggested_shares=0, suggested_cost=0.0, score=1.0,
                       confidence=0.9, legs=legs, unit="sets", idea_id=idea, race_key="2026:GOVERNOR:NV",
                       limit_price=0.95)


def test_value_event_entry_horizons_converged() -> None:
    st = S.SignalStudy(interval_s=30.0)
    assert st.observe(T, obs(T, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], set()) == []
    (ev,) = st.pending()
    assert ev.event_id == f"value:x1:yes@{int(T)}" and ev.kind == "value" and ev.entry == 0.52
    assert ev.fair_value == 0.60 and ev.gap == pytest.approx(0.08) and ev.direction == "D"
    assert ev.race_key == "2026:SENATE:TX" and ev.traded is False
    # +5 min: the first quote at or after t0 + 300; bid 0.56 - entry 0.52; the mid moved 0.06 of a 0.09 gap
    st.observe(T + 300, obs(T + 300, {"1": (0.56, 0.58)}, fv={"1": 0.60}), [value()], set())
    ev = st.pending()[0]
    assert ev.outcomes == {"5m": pytest.approx(0.04)} and ev.converged == {"5m": True} and ev.reversed == {"5m": False}
    # +30 min: the Cup fell back, and the outside price came down to the Cup (the gap closed by "reversal")
    st.observe(T + 1800, obs(T + 1800, {"1": (0.50, 0.52)}, fv={"1": 0.54}), [], set())
    ev = st.pending()[0]
    assert ev.outcomes["30m"] == pytest.approx(-0.02) and ev.converged["30m"] is False and ev.reversed["30m"] is True
    st.observe(T + 7200, obs(T + 7200, {"1": (0.58, 0.60)}), [], set())
    done = st.observe(T + 21600, obs(T + 21600, {"1": (0.59, 0.61)}), [], set())
    assert [e.event_id for e in done] == [ev.event_id] and done[0].done is True
    assert done[0].outcomes["6h"] == pytest.approx(0.07) and done[0].reversed["2h"] is None  # no fair value then
    assert st.pending() == [] and len(st.completed()) == 1


def test_value_event_for_a_no_contract() -> None:
    st = S.SignalStudy(interval_s=30.0)
    st.observe(T, obs(T, {"1": (0.48, 0.50)}, fv={"1": 0.40}), [value(side="no", fv=0.60, delta=-0.05)], set())
    ev = st.pending()[0]
    assert ev.entry == pytest.approx(0.52) and ev.sides == ["no"] and ev.direction == "R"
    st.observe(T + 300, obs(T + 300, {"1": (0.42, 0.44)}, fv={"1": 0.40}), [], set())
    ev = st.pending()[0]
    assert ev.outcomes["5m"] == pytest.approx(0.56 - 0.52) and ev.converged["5m"] is True


def test_basket_outcomes_for_no_and_yes_baskets() -> None:
    st = S.SignalStudy(interval_s=30.0)
    st.observe(T, obs(T, {"1": (0.52, 0.54), "2": (0.53, 0.55)}), [basket("no")], set())
    ev = st.pending()[0]
    assert ev.kind == "basket" and ev.entry == pytest.approx(0.95) and ev.gap == pytest.approx(0.05)
    assert ev.direction == "N" and ev.fair_value is None
    st.observe(T + 300, obs(T + 300, {"1": (0.49, 0.50), "2": (0.49, 0.50)}), [], set())
    ev = st.pending()[0]
    assert ev.outcomes["5m"] == pytest.approx(1.0 - 0.95) and ev.converged["5m"] is True and ev.reversed == {}
    st2 = S.SignalStudy(interval_s=30.0)
    st2.observe(T, obs(T, {"1": (0.43, 0.45), "2": (0.48, 0.50)}), [basket("yes", idea="basket:1+2:yy")], set())
    ev2 = st2.pending()[0]
    assert ev2.entry == pytest.approx(0.95) and ev2.gap == pytest.approx(0.05)
    st2.observe(T + 300, obs(T + 300, {"1": (0.44, 0.46), "2": (0.48, 0.50)}), [], set())
    ev2 = st2.pending()[0]
    assert ev2.outcomes["5m"] == pytest.approx(0.92 - 0.95) and ev2.converged["5m"] is False


def test_episodes_and_the_episode_gap() -> None:
    st = S.SignalStudy(interval_s=30.0)
    q = {"1": (0.50, 0.52)}
    for k in range(5):
        st.observe(T + 30 * k, obs(T + 30 * k, q, fv={"1": 0.60}), [value()], set())
    assert len(st.pending()) == 1  # one episode
    t2 = T + 120 + S.EPISODE_GAP_S + 1
    st.observe(t2, obs(t2, q, fv={"1": 0.60}), [value()], set())
    assert [e.event_id for e in st.pending()][-1] == f"value:x1:yes@{int(t2)}"
    assert len(st.pending()) == 2


def test_a_signal_without_a_quote_opens_its_event_when_measurable() -> None:
    st = S.SignalStudy(interval_s=30.0)
    st.observe(T, obs(T, {"2": (0.4, 0.5)}), [value()], set())  # no quote for "1": nothing to measure yet
    assert st.pending() == []
    st.observe(T + 30, obs(T + 30, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], set())
    assert [e.t0 for e in st.pending()] == [T + 30]


def test_horizon_without_a_quote_in_its_window_is_none() -> None:
    st = S.SignalStudy(interval_s=30.0)
    st.observe(T, obs(T, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], set())
    # at t0 + 300 only an old quote (ts before the horizon): not yet observed
    st.observe(T + 300, obs(T + 300, {"1": (0.56, 0.58)}, ts=T + 200), [], set())
    assert "5m" not in st.pending()[0].outcomes
    # a quote far past the window (more than 2 x interval late) does not count either
    st.observe(T + 600, obs(T + 600, {"1": (0.56, 0.58)}), [], set())
    ev = st.pending()[0]
    assert ev.outcomes["5m"] is None and ev.converged["5m"] is None


def test_traded_flag() -> None:
    st = S.SignalStudy(interval_s=30.0)
    st.observe(T, obs(T, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], {"value:x1:yes"})
    assert st.pending()[0].traded is True
    st2 = S.SignalStudy(interval_s=30.0)
    st2.observe(T, obs(T, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], set())
    st2.observe(T + 30, obs(T + 30, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], {"value:x1:yes"})
    assert st2.pending()[0].traded is True and st2.summary()["kinds"]["value"]["traded"] == 1


def _many(st: S.SignalStudy, n: int, races: int, outcome: Any = lambda k: 0.01 * (k % 5 - 1)) -> None:
    for k in range(n):
        eid = f"e{k}"
        t0 = T + 3600 * k
        st.observe(t0, obs(t0, {eid: (0.50, 0.52)}, fv={eid: 0.60}),
                   [value(eid, race=f"R{k % races}", idea=f"value:{eid}")], set())
        bid = 0.52 + outcome(k)
        st.observe(t0 + 300, obs(t0 + 300, {eid: (bid, bid + 0.02)}, fv={eid: 0.60}), [], set())


def test_summary_intervals_need_twenty_events_and_ten_clusters() -> None:
    st = S.SignalStudy(interval_s=30.0)
    _many(st, 24, races=12)
    row = st.summary()["kinds"]["value"]["horizons"]["5m"]
    assert row["n"] == 24 and row["clusters"] == 12 and row["ci_level"] == 0.9
    assert row["ci_low"] is not None and row["ci_low"] <= row["mean"] <= row["ci_high"]
    values = [0.01 * (k % 5 - 1) for k in range(24)]
    assert row["mean"] == pytest.approx(sum(values) / 24, abs=1e-6)
    assert row["share_positive"] == pytest.approx(sum(1 for v in values if v > 1e-9) / 24, abs=1e-6)
    few = S.SignalStudy(interval_s=30.0)
    _many(few, 19, races=12)
    assert few.summary()["kinds"]["value"]["horizons"]["5m"]["ci_low"] is None
    clustered = S.SignalStudy(interval_s=30.0)
    _many(clustered, 24, races=9)
    r = clustered.summary()["kinds"]["value"]["horizons"]["5m"]
    assert r["clusters"] == 9 and r["ci_low"] is None
    summary = st.summary()
    assert summary["can_show"] == S.CAN_SHOW and summary["cannot_show"] == S.CANNOT_SHOW
    assert summary["kinds"]["basket"]["events"] == 0 and summary["kinds"]["basket"]["horizons"]["5m"]["n"] == 0
    assert set(summary) == {"kinds", "pending", "dropped", "can_show", "cannot_show"}


def test_fixed_texts() -> None:
    assert S.CAN_SHOW == ("What one day can show: how often, and how fast, Cup prices move toward the outside fair value "
                          "after a signal, net of the spread, over hundreds of signals.")
    assert S.CANNOT_SHOW == ("What it cannot show: whether the outside fair value is right about who wins (that pays only "
                             "when races are decided, November 3-4), how prices behave on election night, or how the "
                             "Cup's end-of-tournament settlement will work.")


def test_state_round_trip() -> None:
    st = S.SignalStudy(interval_s=30.0)
    _many(st, 3, races=3)
    st.observe(T + 50_000, obs(T + 50_000, {"1": (0.50, 0.52), "2": (0.53, 0.55)}, fv={"1": 0.60}),
               [value(), basket()], set())
    state = json.loads(json.dumps(st.state()))
    completed = [e.to_dict() for e in st.completed()]
    again = S.SignalStudy()
    again.load(state, completed)
    assert again.interval_s == 30.0
    assert [e.to_dict() for e in again.pending()] == [e.to_dict() for e in st.pending()]
    assert again.summary() == st.summary()
    # the episode memory survives: the same idea right after the restart is not a new event
    again.observe(T + 50_030, obs(T + 50_030, {"1": (0.50, 0.52)}, fv={"1": 0.60}), [value()], set())
    assert len(again.pending()) == len(st.pending())
    empty = S.SignalStudy()
    empty.load(None, ())
    assert empty.pending() == [] and empty.summary()["pending"] == 0


class _Fixed:
    name = "fixed"
    label = "fixed"

    def size(self, opp: Opportunity, ctx: Any) -> SizeDecision:
        return SizeDecision(idea_id=str(opp.idea_id), policy="fixed", units=10, stake=0.0)

    def size_all(self, opps: Sequence[Opportunity], ctx: Any) -> Dict[str, SizeDecision]:
        return {str(o.idea_id): self.size(o, ctx) for o in opps}

    def explain(self, ctx: Any) -> Dict[str, Any]:
        return {"policy": "fixed", "label": "fixed", "mode": None, "lines": []}


def test_engine_runs_the_study_once_per_step_and_persists_completed_events() -> None:
    store = P.MemoryPaperPersistence()
    specs = [PortfolioSpec("a", "a", "fixed", ["value"], None, 2.0, 120.0), PortfolioSpec("b", "b", "fixed", None, None, 2.0, 120.0)]
    eng = P.PaperEngine(PaperConfig(portfolios=specs), persistence=store, policies={"fixed": _Fixed()}, clock=lambda: T)
    q = {"1": (0.50, 0.52)}
    eng.step(T, obs(T, q, fv={"1": 0.60}), [value()])
    (ev,) = eng.events()
    assert ev.traded is True  # a portfolio placed an order for it
    for h in (300, 1800, 7200, 21600):
        eng.step(T + h, obs(T + h, q, fv={"1": 0.60}), [])
    rows = store.paper_events(eng.run_id)
    assert [r["event_id"] for r in rows] == [ev.event_id] and rows[0]["done"] is True
    assert eng.summary()["study"]["kinds"]["value"]["events"] == 1
    assert eng.state()["study"]["pending"] == []
    # a restarted engine restores the completed events from the persistence
    eng2 = P.PaperEngine(PaperConfig(portfolios=specs), persistence=store, policies={"fixed": _Fixed()}, clock=lambda: T)
    assert [e.event_id for e in eng2.events()] == [ev.event_id]
