"""Package B (strategy): generate_signals and the value, basket and hole ideas, regime-aware edges, fixed assumptions. See docs/PAPER_TRADING.md §5.8."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import depth as D
from supermarket_bot import sizing as Z
from supermarket_bot import strategy as S
from supermarket_bot.models import (
    SURGE_OPEN,
    Attribution,
    BacktestResult,
    ExchangeInfo,
    FairValue,
    HighBand,
    Opportunity,
    PricePoint,
    RaceRef,
    SizingContext,
    StrategyInputs,
    StrategyParams,
    Surge,
)

H = 3600.0
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()
CUP_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc).timestamp()
P = StrategyParams()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def race(state: str = "TX", party: str = "D", office: str = "SENATE") -> RaceRef:
    return RaceRef(f"2026:{office}:{state}", office, state, party=party)


def info(eid: str, mid: str, title: str = "Will the Democratic Party win the Texas Senate?",
         option: Optional[str] = None, settle: Optional[float] = CUP_END) -> ExchangeInfo:
    return ExchangeInfo(exchange_id=eid, market_id=mid, option=option, market_title=title,
                        settlement_date=iso(settle) if settle is not None else None)


def tick(bid: float, ask: float, ts: float = NOW, source: str = "tick", book: Optional[Dict[str, Any]] = None) -> PricePoint:
    point = PricePoint(ts=ts, price=round((bid + ask) / 2, 6), bid=bid, ask=ask, source=source)
    point.book = book
    return point


def book(bids: Sequence[Tuple[float, float]] = (), asks: Sequence[Tuple[float, float]] = (), at: float = NOW - 30) -> Dict[str, Any]:
    return {"at": at, "bids": [list(lv) for lv in bids], "asks": [list(lv) for lv in asks]}


def fv(value: float, *, eid: str = "9026", unc: Optional[float] = 0.01, age: float = 30.0, usable: bool = True,
       suspect: bool = False, kind: str = "EXACT", source: str = "polymarket", conf: str = "high",
       prev: Optional[float] = None, mc: float = 0.95) -> FairValue:
    return FairValue(exchange_id=eid, value=value, source=source, confidence=conf, usable=usable, as_of=NOW - age,
                     uncertainty=unc, match_kind=kind, match_confidence=mc, suspect=suspect, prev_value=prev)


def value_inputs(fair: FairValue, bid: float = 0.51, ask: float = 0.52, *, st: str = "TX", regime: str = "unknown",
                 settle: Optional[float] = CUP_END, recent: Optional[List[Tuple[float, float]]] = None,
                 point_book: Optional[Dict[str, Any]] = None) -> Tuple[StrategyInputs, ExchangeInfo, PricePoint, RaceRef]:
    r = race(st)
    i = info("9026", "318", settle=settle)
    p = tick(bid, ask, book=point_book)
    inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos={"9026": i}, latest={"9026": p}, fair_values={"9026": fair},
                         races={"9026": r}, settlement_regime=regime,
                         recent_mids={"9026": recent} if recent is not None else {})
    return inp, i, p, r


def value_idea(fair: FairValue, bid: float = 0.51, ask: float = 0.52, **kw: Any) -> Optional[Opportunity]:
    inp, i, p, r = value_inputs(fair, bid, ask, **kw)
    return S.value_opportunity("9026", fair, p, i, r, inp, P)


# --------------------------------------------------------------------------- value ideas


class TestValueHandExample:
    """ask 0.52, bid 0.51, fv 0.58 (one venue, uncertainty 0.01), fast-count state, regime unknown (§5.2)."""

    def test_numbers(self) -> None:
        o = value_idea(fv(0.58))
        assert o is not None and o.kind == "value" and o.side == "yes"
        assert o.entry_price == 0.52
        assert o.target_price == 0.57 and o.exit_plan is not None and o.exit_plan.target_bid == 0.57
        assert o.edge == pytest.approx(0.053175, abs=1e-6)  # the vwap branch is the smaller one
        assert o.limit_price == 0.535  # 0.545 fails: marginal ev 0.028175 < required 0.03
        assert o.called_prob == pytest.approx(0.85)
        assert o.fv_uncertainty == 0.01 and o.fair_value == pytest.approx(0.58) and o.fair_source == "polymarket"
        assert o.order_type == "taker" and o.settlement_regime == "unknown" and o.settles_before_cup_end is False
        text = " | ".join(o.rationale)
        assert "0.570" in text and "+0.045/share" in text  # target, and what the exit order at 0.565 banks
        assert "needs 0.030" in text
        assert "heuristic for how an uncalled race closes" in text
        assert o.exit_plan.dynamic_fv_target is True and o.exit_plan.exit_buffer == 0.005
        assert o.exit_plan.note.startswith("Sell when the YES bid reaches 0.570 (fair value 0.58 less half the spread")
        assert o.exit_plan.hold_to_resolution is False and o.exit_plan.exit_before_ts is None  # TX is a fast count
        assert o.bet is not None and o.bet.kind == "binary" and o.bet.p == pytest.approx(0.52 + 0.053175)
        assert o.bet_limit is not None and o.bet_limit.cost == 0.535 and o.bet_limit.loss == 0.535

    def test_branch_payoffs(self) -> None:
        ev, text, d = S.regime_ev(0.58, 0.52, 0.515, "unknown", 0.85, P)
        assert ev == pytest.approx(0.053175, abs=1e-6)
        assert d["q_called"] == pytest.approx(0.58988, abs=1e-5)
        assert d["q_uncalled"] == pytest.approx(0.524)
        assert d["called_payoff"] == pytest.approx(0.06988, abs=1e-5)
        assert d["uncalled_payoff"] == pytest.approx(-0.0415, abs=1e-6)
        assert S.regime_ev(0.58, 0.52, 0.515, "resolved_outcomes", 0.85, P)[0] == pytest.approx(0.06)
        assert "smaller of two" in text

    def test_limit_without_uncertainty(self) -> None:
        o = value_idea(fv(0.58, unc=0.0))
        assert o is not None and o.limit_price == 0.545

    def test_value_limit_function(self) -> None:
        assert S.value_limit(0.58, 0.52, 0.515, 0.565, "unknown", 0.85, 0.03, P) == 0.535
        assert S.value_limit(0.58, 0.52, 0.515, 0.565, "unknown", 0.85, 0.02, P) == 0.545
        assert S.value_limit(0.58, 0.52, 0.515, 0.565, "unknown", 0.85, 0.06, P) is None

    def test_depth_at_the_limit(self) -> None:
        b = book(bids=[(0.51, 500)], asks=[(0.52, 2000), (0.53, 3000), (0.535, 5000), (0.54, 9000)])
        o = value_idea(fv(0.58), point_book=b)
        assert o is not None and o.levels == [(0.52, 2000.0), (0.53, 3000.0), (0.535, 5000.0)]
        assert o.max_units == 10000 and o.depth_checked is True


class TestValueRules:
    def test_no_side(self) -> None:
        o = value_idea(fv(0.42), bid=0.48, ask=0.49)
        assert o is not None and o.side == "no" and o.entry_price == 0.52
        assert o.fair_value == pytest.approx(0.58) and o.factor_delta is not None and o.factor_delta < 0

    def test_spread_buffer_and_exit_tick_are_charged(self) -> None:
        # resolution gap 0.06 at the ask, but a 0.04 spread: target 0.58 - 0.02 - 0.005 = 0.555, exit order 0.55,
        # convergence gain 0.55 - 0.52 = 0.03 >= 0.03 just clears; one tick wider spread does not
        assert value_idea(fv(0.58), bid=0.48, ask=0.52) is not None
        assert value_idea(fv(0.58), bid=0.47, ask=0.52) is None

    def test_required_edge_terms(self) -> None:
        # min 0.02 + uncertainty: ev 0.053 clears 0.03 but not 0.06
        assert value_idea(fv(0.58, unc=0.01)) is not None
        assert value_idea(fv(0.58, unc=0.04)) is None
        # a NEAR match the user enabled (usable) needs +0.03 more: 0.05 in all
        near = value_idea(fv(0.60, unc=0.0, kind="NEAR"))
        assert near is not None and any("Near match" in r for r in near.risks)

    def test_near_extra_edge_boundary(self) -> None:
        # fv 0.58, no uncertainty: conv gain 0.045, so a NEAR match (required 0.05) is refused
        o = value_idea(fv(0.58, unc=0.0, kind="NEAR"))
        assert o is None or o.edge >= 0.05  # never traded below the NEAR bar
        assert value_idea(fv(0.58, unc=0.0, kind="NEAR")) is None

    def test_longshot_shrink_and_multiplier(self) -> None:
        # YES fair value 0.12 (< 0.15): shrunk 10% to 0.108, and the minimum edge doubles to 0.04
        o = value_idea(fv(0.12, unc=0.0), bid=0.05, ask=0.055)
        assert o is not None and o.side == "yes"
        assert o.prob_win == pytest.approx(0.108)
        assert any("Page & Clemen" in line for line in o.rationale)
        assert any("x2 (longshot)" in line for line in o.rationale)
        # 0.095 fair value at 0.055: shrunk 0.0855, gap 0.0305 < 0.04 -> nothing
        assert value_idea(fv(0.095, unc=0.0), bid=0.05, ask=0.055) is None

    @pytest.mark.parametrize("kw", [dict(age=91.0), dict(usable=False), dict(suspect=True)])
    def test_unusable_values_give_nothing(self, kw: Dict[str, Any]) -> None:
        assert value_idea(fv(0.58, **kw)) is None

    def test_age_boundary_and_manual_age(self) -> None:
        assert value_idea(fv(0.58, age=90.0)) is not None
        assert value_idea(fv(0.58, age=3600.0, source="manual")) is not None  # manual: 72 h
        assert value_idea(fv(0.58, age=73 * H, source="manual")) is None

    def test_near_display_only(self) -> None:
        # what fairvalue publishes for a NEAR match the user did not enable: usable False
        assert value_idea(fv(0.70, kind="NEAR", usable=False)) is None

    def test_jump_check(self) -> None:
        # gap |0.58 - 0.515| = 0.065; a 0.04 jump since the previous refresh is more than half of it
        assert value_idea(fv(0.58, prev=0.54)) is None
        assert value_idea(fv(0.58, prev=0.57)) is not None
        assert value_idea(fv(0.58, prev=0.54, source="manual")) is not None  # manual values: no freshness checks

    def test_cup_moved_check_both_directions(self) -> None:
        as_of = NOW - 30
        # the Cup moved AWAY from fair value since it was read (0.56 -> 0.515): skip
        away = [(as_of - 60, 0.55), (as_of - 1, 0.56), (NOW - 5, 0.515)]
        assert value_idea(fv(0.58), recent=away) is None
        # it moved TOWARD fair value (0.45 -> 0.515): not blocked
        toward = [(as_of - 1, 0.45), (NOW - 5, 0.515)]
        assert value_idea(fv(0.58), recent=toward) is not None
        # no mid at or before as_of: the check is skipped
        assert value_idea(fv(0.58), recent=[(NOW - 5, 0.40)]) is not None
        # a small move away (under half the gap) does not block
        assert value_idea(fv(0.58), recent=[(as_of - 1, 0.53)]) is not None

    def test_min_edge_boundary(self) -> None:
        # resolved regime, no uncertainty: ev = fv - ask; conv = floor_tick(fv - 0.005 - 0.005) - 0.005 - 0.52
        assert value_idea(fv(0.55, unc=0.0), regime="resolved_outcomes") is None  # conv 0.015 < 0.02
        o = value_idea(fv(0.555, unc=0.0), regime="resolved_outcomes")
        assert o is not None and o.edge == pytest.approx(0.035)  # conv exactly 0.02 clears
        assert o.exit_plan.hold_to_resolution is True

    def test_settling_before_the_end_uses_the_resolution(self) -> None:
        o = value_idea(fv(0.58), settle=NOW + 2 * H)
        assert o is not None and o.settles_before_cup_end is True and o.edge == pytest.approx(0.06)
        assert any("settlement date is not a payout date" in r for r in o.risks)

    def test_one_sided_quote(self) -> None:
        inp, i, _, r = value_inputs(fv(0.58))
        p = PricePoint(ts=NOW, price=0.52, bid=None, ask=0.52)
        assert S.value_opportunity("9026", fv(0.58), p, i, r, inp, P) is None


# --------------------------------------------------------------------------- regime, called prob, factor delta, score


class TestRegimeHelpers:
    def test_regimes(self) -> None:
        res = S.regime_ev(0.7, 0.6, 0.62, "resolved_outcomes", 0.85, P)
        assert res[0] == pytest.approx(0.1) and res[2] == {}
        before = S.regime_ev(0.7, 0.6, 0.62, "vwap_closeout", 0.85, P, settles_before_end=True)
        assert before[0] == pytest.approx(0.1) and "settles before the Cup ends" in before[1]
        vwap, text, d = S.regime_ev(0.7, 0.6, 0.62, "vwap_closeout", 0.85, P)
        assert vwap == pytest.approx(0.85 * d["called_payoff"] + 0.15 * d["uncalled_payoff"])
        assert "5-hour VWAP closeout" in text
        unknown = S.regime_ev(0.7, 0.6, 0.62, "unknown", 0.85, P)[0]
        assert unknown == pytest.approx(min(0.1, vwap))

    def test_carry_favourite_numbers(self) -> None:
        """§5.5: a 0.97 favourite, q 0.98, mid 0.965, called 0.97."""
        ev, _, d = S.regime_ev(0.98, 0.97, 0.965, "vwap_closeout", 0.97, P)
        assert ev == pytest.approx(0.0097, abs=1e-4)
        assert d["uncalled_payoff"] == pytest.approx(-0.3365, abs=1e-4)
        assert 0.03 * d["uncalled_payoff"] == pytest.approx(-0.0101, abs=1e-4)

    def test_q_called_clipping(self) -> None:
        _, _, d = S.regime_ev(0.99, 0.9, 0.95, "vwap_closeout", 0.5, P)
        assert d["q_called"] == 1.0  # (0.99 - 0.5 x 0.647) / 0.5 = 1.33, clipped

    def test_called_prob(self) -> None:
        assert S.called_prob(race("GA"), 0.96, P) == 0.97
        assert S.called_prob(race("ME"), 0.96, P) == 0.80  # ranked choice
        assert S.called_prob(race("NV"), 0.6, P) == 0.40
        assert S.called_prob(race("GA"), 0.6, P) == 0.85
        assert S.called_prob(None, 0.6, P) == pytest.approx((0.85 + 0.40) / 2)

    def test_factor_delta(self) -> None:
        d = S.factor_delta(race(party="D"), "yes", 0.5, P)
        assert d == pytest.approx(0.057, abs=1e-3)
        assert S.factor_delta(race(party="D"), "no", 0.5, P) == pytest.approx(-d)
        assert S.factor_delta(race(party="R"), "yes", 0.5, P) == pytest.approx(-d)
        assert S.factor_delta(race(party="R"), "no", 0.5, P) == pytest.approx(d)
        assert S.factor_delta(race(party="D"), "yes", 0.95, P) == pytest.approx(0.0147, abs=1e-3)
        assert S.factor_delta(race(party="I"), "yes", 0.5, P) == 0.0
        assert S.factor_delta(None, "yes", 0.5, P) == 0.0
        assert S.factor_delta(race(), "yes", None, P) == 0.0

    def test_growth_score_ranks_toss_up_above_longshot(self) -> None:
        def binary(c: float, ev: float) -> Any:
            from supermarket_bot.models import BetShape
            return BetShape("binary", p=c + ev, gain=1 - c, loss=c, cost=c)

        toss = S.growth_score(binary(0.50, 0.05), 0.05, 1.0, 0.50)
        longshot = S.growth_score(binary(0.10, 0.02), 0.02, 1.0, 0.10)
        assert toss == pytest.approx(0.25) and longshot == pytest.approx(0.1111, abs=1e-4)
        assert S.growth_score(None, 0.05, 1.0, 0.5) == 0.0
        assert S.growth_score(binary(0.5, 0.05), -0.01, 1.0, 0.5) == 0.0

    def test_growth_score_set_and_fixed(self) -> None:
        from supermarket_bot.models import BetShape

        riskless = BetShape("riskless", p=1.0, gain=0.05, loss=0.0, cost=0.95, floor=1.0)
        assert S.growth_score(riskless, 0.05, 0.9, 0.95) == pytest.approx(100 * 0.9 * 0.05 * 0.08 / 0.95, abs=1e-6)
        hole = BetShape("fixed", p=0.02, gain=0.2, loss=0.7, cost=0.7, cap_pct=0.01)
        assert S.growth_score(hole, 0.004, 0.5, 0.7) == pytest.approx(100 * 0.5 * 0.004 * 0.01 / 0.7, abs=1e-6)
        # a bounded set is scored on what it is expected to make, not on its floor edge
        bounded = BetShape("bounded", p=0.99, gain=0.02, loss=0.55, cost=0.95, floor=1.0, tail_prob=0.01)
        expected = 0.99 * 0.02 - 0.01 * 0.55
        u = min(0.25 * (0.99 / 0.55 - 0.01 / 0.02), 0.08 / 0.95)
        assert S.growth_score(bounded, 0.05, 0.9, 0.95) == pytest.approx(100 * 0.9 * expected * u, abs=1e-6)

    def test_idea_ids(self) -> None:
        assert S.idea_id_for("value", exchange_id="9026", side="yes") == "value:x9026:yes"
        assert S.idea_id_for("fade", exchange_id="e1", side="no", surge_id=7) == "fade:xe1:no:s7"
        legs = [{"exchange_id": "9025", "side": "no"}, {"exchange_id": "9024", "side": "no"}]
        assert S.idea_id_for("basket", legs=legs) == "basket:set:9024+9025:nn"
        assert S.idea_id_for("arbitrage", legs=[{"exchange_id": "12", "side": "no"}, {"exchange_id": "11", "side": "yes"}]) \
            == "arbitrage:set:11+12:yn"
        assert S.idea_id_for("value", exchange_id="1", side="no", extra="x") == "value:x1:no:x"

    def test_settles_before(self) -> None:
        assert S.settles_before(iso(CUP_END), CUP_END) is False  # the Cup end itself is the closeout
        assert S.settles_before(iso(CUP_END - 1800), CUP_END) is False  # inside the 1 h margin
        assert S.settles_before(iso(CUP_END - H), CUP_END) is True
        assert S.settles_before(None, CUP_END) is None and S.settles_before(iso(NOW), None) is None


# --------------------------------------------------------------------------- baskets


def nv_inputs(bid_d: float = 0.60, bid_r: float = 0.45, *, regime: str = "unknown", st: str = "NV",
              dts: float = 2.0, source_r: str = "tick", books: bool = False, races_on: bool = True,
              constraints: Optional[Dict[str, Any]] = None) -> StrategyInputs:
    title_d = f"Will the Democratic Party win the {st} Governor?"
    title_r = f"Will the Republican Party win the {st} Governor?"
    infos = {"9024": info("9024", "316", title_d), "9025": info("9025", "317", title_r)}
    bd = book(bids=[(bid_d, 3000), (bid_d - 0.01, 5000)], asks=[(bid_d + 0.01, 3000)]) if books else None
    br = book(bids=[(bid_r, 1000), (bid_r - 0.01, 5000)], asks=[(bid_r + 0.01, 3000)]) if books else None
    latest = {"9024": tick(bid_d, bid_d + 0.01, book=bd), "9025": tick(bid_r, bid_r + 0.01, ts=NOW + dts, source=source_r, book=br)}
    races = {"9024": race(st, "D", "GOVERNOR"), "9025": race(st, "R", "GOVERNOR")} if races_on else {}
    return StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races=races, settlement_regime=regime,
                          constraints=constraints)


class TestBaskets:
    def test_no_basket_hand_example(self) -> None:
        [o] = S.basket_opportunities(nv_inputs(), P)
        assert (o.kind, o.side, o.unit, o.exchange_id) == ("basket", "no", "sets", None)
        assert o.idea_id == "basket:set:9024+9025:nn" and o.race_key == "2026:GOVERNOR:NV"
        assert o.entry_price == pytest.approx(0.95) and o.edge == pytest.approx(0.05)
        assert [leg["limit"] for leg in o.legs] == [0.41, 0.56] and o.limit_price == pytest.approx(0.97)
        assert [(leg["side"], leg["price"], leg["yes_bid"], leg["yes_ask"]) for leg in o.legs] == [
            ("no", 0.40, 0.60, 0.61), ("no", 0.55, 0.45, 0.46)]
        assert {leg["party"] for leg in o.legs} == {"D", "R"} and all(leg["quote_ts"] for leg in o.legs)
        assert o.bet is not None and o.bet.kind == "bounded"  # NV counts slowly, regime unknown
        assert (o.bet.p, o.bet.gain, o.bet.loss, o.bet.cost, o.bet.floor) == (0.99, 0.02, 0.55, 0.95, 1.0)
        assert o.called_prob == pytest.approx(0.40) and o.prob_win == pytest.approx(0.99)
        assert o.target_price is None  # not exhaustive: pays at least 1.00
        assert o.factor_delta == 0.0 and o.max_units is None and o.levels == []
        text = " | ".join(o.rationale)
        assert "sum to 1.050" in text and "pays even if a third candidate wins" in text
        assert "about 0.93 per set against 0.95" in text and "not a loss of the locked edge" in text
        assert any("REFUND" in r for r in o.risks) and any("Legging" in r for r in o.risks)
        assert o.exit_plan is not None and o.exit_plan.kind == "basket"
        assert o.exit_plan.min_set_profit == pytest.approx(0.025)  # max(0.005, 0.5 x 0.05)
        assert o.exit_plan.exit_before_ts == pytest.approx(CUP_END - 6 * H)  # slow-count state, unknown regime
        days = (CUP_END - NOW) / 86400
        assert o.profit_per_capital_day == pytest.approx(0.05 / (0.95 * days), rel=1e-4)
        assert o.horizon_hours == pytest.approx(days * 24, abs=0.01)

    def test_riskless_only_when_it_is(self) -> None:
        [resolved] = S.basket_opportunities(nv_inputs(regime="resolved_outcomes"), P)
        assert resolved.bet.kind == "riskless" and resolved.bet.loss == 0 and resolved.prob_win == 1.0
        assert resolved.exit_plan.hold_to_resolution is True
        [fast] = S.basket_opportunities(nv_inputs(st="GA"), P)  # a fast count, regime unknown
        assert fast.bet.kind == "riskless" and fast.exit_plan.exit_before_ts is None
        [vwap_fast] = S.basket_opportunities(nv_inputs(st="GA", regime="vwap_closeout"), P)
        assert vwap_fast.bet.kind == "riskless" and vwap_fast.exit_plan.exit_before_ts == pytest.approx(CUP_END - 6 * H)
        # no race known: a market group, but called is unknown -> bounded

    def test_below_min_edge(self) -> None:
        assert S.basket_opportunities(nv_inputs(0.55, 0.455), P) == []  # bids sum 1.005
        assert len(S.basket_opportunities(nv_inputs(0.55, 0.46), P)) == 1  # exactly 1.01

    def test_same_snapshot_and_tick_only(self) -> None:
        assert len(S.basket_opportunities(nv_inputs(dts=5.0), P)) == 1
        assert S.basket_opportunities(nv_inputs(dts=6.0), P) == []
        assert S.basket_opportunities(nv_inputs(source_r="candle"), P) == []

    def test_depth_from_set_levels(self) -> None:
        [o] = S.basket_opportunities(nv_inputs(books=True), P)
        d_levels = D.contract_levels([(0.60, 3000), (0.59, 5000)], [], "no", "buy")
        r_levels = D.contract_levels([(0.45, 1000), (0.44, 5000)], [], "no", "buy")
        expect = D.set_levels([[lv for lv in d_levels if lv[0] <= 0.41], [lv for lv in r_levels if lv[0] <= 0.56]], 0.97)
        assert o.levels == expect == [(0.95, 1000.0), (0.96, 2000.0), (0.97, 3000.0)]
        assert o.max_units == 6000 and o.depth_checked is True

    def test_triple(self) -> None:
        titles = {"9030": ("D", 0.03), "9031": ("R", 0.66), "9032": ("I", 0.34)}
        infos = {e: info(e, str(300 + i), f"Will the {p} win the Nebraska Senate?") for i, (e, (p, _)) in enumerate(titles.items())}
        latest = {e: tick(b, b + 0.01) for e, (_, b) in titles.items()}
        races = {e: race("NE", p) for e, (p, _) in titles.items()}
        inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races=races)
        [o] = S.basket_opportunities(inp, P)
        assert len(o.legs) == 3 and o.side == "no" and o.idea_id == "basket:set:9030+9031+9032:nnn"
        assert o.edge == pytest.approx(0.03) and o.entry_price == pytest.approx(3 - 1.03)
        assert o.bet.floor == 2.0  # n - 1 NO shares pay

    def test_yes_basket_only_for_exhaustive_groups(self) -> None:
        # two separate binaries of one race: not exhaustive (a third candidate could win) -> never a YES basket
        assert S.basket_opportunities(nv_inputs(0.47, 0.47), P) == []  # asks 0.48 + 0.48 = 0.96
        az = {"9011": ("Democratic nominee", 0.505), "9012": ("Republican nominee", 0.405), "9013": ("Any other candidate", 0.03)}
        infos = {e: info(e, "m-az", "Who will win the Arizona Governor race?", option=o) for e, (o, _) in az.items()}
        latest = {e: tick(b, b + 0.01) for e, (_, b) in az.items()}
        [o] = S.basket_opportunities(StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest), P)
        assert o.side == "yes" and o.entry_price == pytest.approx(0.97) and o.edge == pytest.approx(0.03)
        assert o.bet.floor == 1.0 and o.confidence == S.BASKET_CONF_EXHAUSTIVE and o.target_price == 1.0
        assert "catches every other result" in o.rationale[0]
        # without the catch-all option: not exhaustive ...
        plain = {e: info(e, "m-az", "Who will win the Arizona Governor race?", option=o) for e, (o, _) in
                 list(az.items())[:2]}
        two = {e: latest[e] for e in plain}
        assert S.basket_opportunities(StrategyInputs(now=NOW, cup_end=CUP_END, infos=plain, latest=two), P) == []
        # ... unless an engine complementary relationship covers every outcome
        rel = {"data": [{"type": "complementary", "observedPrices": [{"exchangeId": "9011"}, {"exchangeId": "9012"}]}]}
        latest2 = {"9011": tick(0.50, 0.51), "9012": tick(0.47, 0.48)}
        [y] = S.basket_opportunities(StrategyInputs(now=NOW, cup_end=CUP_END, infos=plain, latest=latest2,
                                                    constraints=rel), P)
        assert y.side == "yes" and "complementary relationship" in y.rationale[0]

    def test_group_races_dedupes_by_member_set(self) -> None:
        infos = {"1": info("1", "m", option="Democrats"), "2": info("2", "m", option="Republicans"),
                 "3": info("3", "x"), "4": info("4", "y")}
        races = {"1": race("US", "D", "SENATE_CONTROL"), "2": race("US", "R", "SENATE_CONTROL"),
                 "3": race("TX", "D"), "4": race("TX", "R")}
        groups = S.group_races(infos, races)
        assert [(g.key, g.exchange_ids, g.source) for g in groups] == [
            ("market:m", ["1", "2"], "market"), ("2026:SENATE:TX", ["3", "4"], "race")]
        assert not any(g.exhaustive for g in groups)
        # two outcomes of the same party are not a race group
        races["4"] = race("TX", "D")
        assert [g.key for g in S.group_races(infos, races)] == ["market:m"]

    def test_dedupe_with_identical_engine_arbitrage(self) -> None:
        violation = {"data": [{
            "relationshipId": "r", "type": "mutually_exclusive", "violationAmount": 0.05, "reason": "sum above 1",
            "suggestedCorrectiveTrades": [
                {"exchangeId": "9024", "outcomeSide": "YES", "action": "sell", "marketId": "316", "currentPrice": 0.605},
                {"exchangeId": "9025", "outcomeSide": "YES", "action": "sell", "marketId": "317", "currentPrice": 0.455}],
        }]}
        signals = S.generate_signals(nv_inputs(constraints=violation))
        kinds = [o.kind for o in signals]
        assert kinds == ["basket"]


# --------------------------------------------------------------------------- holes


def wy_inputs(bid: float = 0.925, ask: float = 0.935, *, touch: float = 400.0, book_age: float = 60.0,
              fair: Optional[FairValue] = None, surge: bool = False, resting: Sequence[str] = (),
              with_book: bool = True) -> StrategyInputs:
    b = book(bids=[(bid, touch), (bid - 0.005, 900)], asks=[(ask, 800)], at=NOW - book_age) if with_book else None
    infos = {"9033": info("9033", "325", "Will the Republican Party win the Wyoming Governor?")}
    latest = {"9033": tick(bid, ask, book=b)}
    surges: List[Surge] = []
    if surge:
        surges.append(Surge(exchange_id="9033", market_id="325", window="1h", window_s=3600, start_ts=NOW - 1800,
                            end_ts=NOW - 60, start_price=0.9, end_price=0.93, change=0.03, direction="up",
                            peak_price=0.93, detected_at=NOW - 60, status=SURGE_OPEN,
                            attribution=Attribution(verdict="news", confidence=0.8, reversion_odds=0.2, summary="")))
    return StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races={"9033": race("WY", "R", "GOVERNOR")},
                          fair_values={"9033": fair} if fair is not None else {}, surges=surges,
                          resting_exchanges=set(resting))


class TestHoles:
    def test_hole_idea(self) -> None:
        [o] = S.hole_opportunities(wy_inputs(fair=fv(0.94, eid="9033", source="demo", unc=0.005)), P)
        assert (o.kind, o.side, o.order_type) == ("hole", "yes", "maker")
        assert o.limit_price == 0.705 == o.entry_price  # floor_tick(0.94 x 0.75)
        assert o.expires_at == pytest.approx(NOW + 6 * H) and o.max_units == 500 and o.levels == []
        assert o.exit_plan is not None and o.exit_plan.kind == "hole"
        assert o.exit_plan.target_bid == 0.91 and o.exit_plan.time_stop_after_fill_s == 12 * H
        assert o.exit_plan.note == "If it fills, sell when the bid recovers to 0.910, or after 12 h"
        assert o.bet.kind == "fixed" and o.bet.cap_pct == 0.01 and o.bet.cost == 0.705
        assert o.edge == pytest.approx(0.02 * (0.91 - 0.705)) and o.prob_win == 0.02 and o.confidence == 0.5
        assert o.idea_id == "hole:x9033:yes" and o.fair_value == pytest.approx(0.94)
        assert o.factor_delta is not None and o.factor_delta < 0  # YES on the Republican leg
        assert any("never fill" in line for line in o.rationale) and any("Adverse selection" in r for r in o.risks)

    def test_reference_is_the_mid_without_fair_value(self) -> None:
        [o] = S.hole_opportunities(wy_inputs(), P)
        assert o.limit_price == D.floor_tick(0.93 * 0.75) == 0.695 and o.fair_value is None

    def test_no_side_for_a_longshot(self) -> None:
        b = book(bids=[(0.065, 900)], asks=[(0.075, 500), (0.08, 300)])
        inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos={"7": info("7", "m")}, latest={"7": tick(0.065, 0.075, book=b)})
        [o] = S.hole_opportunities(inp, P)
        assert o.side == "no" and o.limit_price == D.floor_tick(0.93 * 0.75)

    @pytest.mark.parametrize("kw", [dict(bid=0.90, ask=0.93), dict(bid=0.60, ask=0.61), dict(touch=150.0),
                                    dict(surge=True), dict(book_age=1000.0), dict(with_book=False)])
    def test_eligibility(self, kw: Dict[str, Any]) -> None:
        assert S.hole_opportunities(wy_inputs(**kw), P) == []

    def test_price_rule_needs_ticks_below_the_bid_and_five_cents(self) -> None:
        # a fair value far below the bid: still far below; a reference so low the price is under 0.05: none
        inp = wy_inputs(fair=fv(0.94, eid="9033", source="demo"))
        params = StrategyParams(hole_depth_fraction=0.02)  # 0.94 x 0.98 = 0.921: only 1 tick under the 0.925 bid
        assert S.hole_opportunities(inp, params) == []
        b = book(bids=[(0.02, 900)], asks=[(0.03, 900)])
        cheap = StrategyInputs(now=NOW, cup_end=CUP_END, infos={"7": info("7", "m")}, latest={"7": tick(0.02, 0.03, book=b)})
        assert S.hole_opportunities(cheap, StrategyParams(hole_depth_fraction=0.95)) == []

    def test_candidates_without_books(self) -> None:
        infos, latest = {}, {}
        for i, spread in enumerate([0.02, 0.005, 0.01, 0.015, 0.005, 0.01, 0.02, 0.005, 0.01, 0.015]):
            eid = str(100 + i)
            infos[eid] = info(eid, f"m{i}")
            latest[eid] = tick(0.9, round(0.9 + spread, 3))
        latest["109"].book = book(bids=[(0.9, 900)], asks=[(0.915, 900)])  # a fresh book: decided on it, not a candidate
        inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest)
        cands = S.hole_candidates(inp, P)
        assert len(cands) == 8 and "109" not in cands
        assert cands[:3] == ["101", "104", "107"]  # tightest spread first, then exchange id
        assert S.hole_opportunities(inp, P) == [] or all(o.exchange_id == "109" for o in S.hole_opportunities(inp, P))

    def test_survival_via_resting_exchanges(self) -> None:
        assert S.hole_opportunities(wy_inputs(book_age=1200.0), P) == []
        [kept] = S.hole_opportunities(wy_inputs(book_age=1200.0, resting=["9033"]), P)
        assert kept.idea_id == "hole:x9033:yes" and any("Kept alive" in line for line in kept.rationale)
        assert S.hole_opportunities(wy_inputs(book_age=1900.0, resting=["9033"]), P) == []
        # the quote must still qualify
        assert S.hole_opportunities(wy_inputs(bid=0.90, ask=0.93, book_age=1200.0, resting=["9033"]), P) == []

    def test_at_most_four(self) -> None:
        infos, latest = {}, {}
        for i in range(6):
            eid = str(200 + i)
            infos[eid] = info(eid, f"m{i}")
            latest[eid] = tick(0.92, 0.93, book=book(bids=[(0.92, 300 + 100 * i)], asks=[(0.93, 500)]))
        ideas = S.hole_opportunities(StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest), P)
        assert [o.exchange_id for o in ideas] == ["205", "204", "203", "202"]  # most touch quantity first


# --------------------------------------------------------------------------- the fixed assumptions


def att(odds: float = 0.66, conf: float = 0.64) -> Attribution:
    return Attribution(verdict="participants", confidence=conf, reversion_odds=odds, summary="", reasons=["2 trades"])


def spike(start: float = 0.51, peak: float = 0.69, direction: str = "up", eid: str = "9026") -> Surge:
    return Surge(exchange_id=eid, market_id="318", window="1h", window_s=3600, start_ts=NOW - 2400, end_ts=NOW - 600,
                 start_price=start, end_price=peak, change=round(peak - start, 6), direction=direction, peak_price=peak,
                 detected_at=NOW - 600, status=SURGE_OPEN, current_price=peak, attribution=att(), id=3)


class TestFixedAssumptions:
    def test_fade_exit_spread_and_bets(self) -> None:
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000)
        assert o is not None and o.exit_plan.kind == "bracket"
        assert (o.exit_plan.target_bid, o.exit_plan.stop_bid) == (0.395, 0.215)
        assert o.bet.kind == "bracket" and (o.bet.gain, o.bet.loss, o.bet.cost) == (0.085, 0.095, 0.31)
        assert o.exit_plan.time_stop_ts == pytest.approx(NOW + 6 * H)
        assert o.limit_price == D.floor_tick(0.31 + 0.5 * o.edge) == 0.32
        assert o.fv_uncertainty == 0.03
        assert any("Exits sell at the NO bid" in line for line in o.rationale)

    def test_fade_target_never_beyond_fair_value(self) -> None:
        # fair value YES 0.62: NO 0.38 is nearer than the half-reversion NO 0.40
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, fair_value=fv(0.62))
        assert o is not None and o.target_price == pytest.approx(0.38) and o.exit_plan.target_bid == pytest.approx(0.375)
        assert o.bet.gain == pytest.approx(0.065)
        assert any("caps the target" in line for line in o.rationale)
        assert o.fair_value == pytest.approx(0.38) and o.fair_source == "polymarket"
        # fair value YES 0.66 caps the target at NO 0.34: the edge is gone after the stop, so no fade
        assert S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, fair_value=fv(0.66)) is None
        # a fair value beyond the target does not cap it
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, fair_value=fv(0.55))
        assert o is not None and o.target_price == pytest.approx(0.40)
        # the move went TOWARD fair value (fair value beyond the peak): no fade
        assert S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, fair_value=fv(0.75)) is None
        # a stale fair value is ignored
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, fair_value=fv(0.75, age=200))
        assert o is not None

    def test_fade_min_gain(self) -> None:
        # a small jump (0.05): the half-reversion gain 0.025 less half a 0.02 spread is 0.015 < 0.02
        assert S.fade_opportunity(spike(0.64, 0.69), tick(0.68, 0.70), info("9026", "318"), NOW, 100_000) is None
        assert S.fade_opportunity(spike(0.64, 0.69), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000,
                                  params=StrategyParams(min_fade_gain=0.0)) is not None

    def test_fade_measured_participant_p(self) -> None:
        bt = BacktestResult(n_surges=40, n_reverted=30, reversion_rate=0.75, n_participant=12,
                            participant_reversion_rate=0.62)
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, bt)
        assert o is not None and o.prob_win == pytest.approx(0.62)  # min(0.66, measured 0.62); never raised to 0.75
        high = BacktestResult(n_surges=40, n_participant=12, participant_reversion_rate=0.9)
        o = S.fade_opportunity(spike(), tick(0.69, 0.70), info("9026", "318"), NOW, 100_000, high)
        assert o is not None and o.prob_win == pytest.approx(0.66)  # a cap, not a floor

    def test_carry_with_fair_value(self) -> None:
        band = HighBand(exchange_id="9026", market_id="318", side="YES", favorite_price=0.9725, time_in_band=1.0,
                        mean=0.9725, low=0.97, high=0.975, lookback_s=6 * H, stable=True,
                        settlement_date=iso(NOW + 40 * H))
        o = S.carry_opportunity(band, tick(0.97, 0.975), None, NOW, CUP_END, 100_000, fair_value=fv(0.99, unc=0.005))
        assert o is not None and o.prob_win == pytest.approx(0.99) and o.edge == pytest.approx(0.015)
        assert o.fv_uncertainty == 0.005 and o.fair_source == "polymarket"
        assert any("the outside price, not a rule of thumb" in line for line in o.rationale)
        assert not any("rule of thumb, not a measured edge" in r for r in o.risks)
        plain = S.carry_opportunity(band, tick(0.97, 0.975), None, NOW, CUP_END, 100_000)
        assert plain is not None and plain.fv_uncertainty == 0.02
        assert any("rule of thumb, not a measured edge" in r for r in plain.risks)
        assert plain.exit_plan.kind == "settle" and plain.exit_plan.hold_to_resolution is True
        assert any("settlement date is not a payout date" in r for r in plain.risks)

    def test_carry_settling_at_the_cup_end_is_not_before(self) -> None:
        band = HighBand(exchange_id="9026", market_id="318", side="YES", favorite_price=0.9725, time_in_band=1.0,
                        mean=0.9725, low=0.97, high=0.975, lookback_s=6 * H, stable=True, settlement_date=iso(CUP_END))
        o = S.carry_opportunity(band, tick(0.97, 0.975), None, NOW, CUP_END, 100_000, regime="vwap_closeout",
                                race=race("NV"))
        assert o is None or o.settles_before_cup_end is False
        o = S.carry_opportunity(band, tick(0.97, 0.975), None, NOW, CUP_END, 100_000, regime="resolved_outcomes")
        assert o is not None and o.settles_before_cup_end is False and o.edge == pytest.approx(0.003)

    def test_arbitrage_sets_get_a_bet_and_exit_plan(self) -> None:
        latest = {"9016": tick(0.705, 0.715), "9017": tick(0.325, 0.335)}
        v = {"type": "complementary", "violationAmount": 0.03, "reason": "sum 1.03",
             "suggestedCorrectiveTrades": [
                 {"exchangeId": "9016", "outcomeSide": "YES", "action": "sell", "marketId": "316", "currentPrice": 0.71},
                 {"exchangeId": "9017", "outcomeSide": "YES", "action": "sell", "marketId": "316", "currentPrice": 0.32}],
             "direction": {"fromExchangeIds": ["9016", "9017"], "toExchangeIds": ["9016", "9017"]}}
        [o] = S.arbitrage_opportunities({"data": [v]}, [], 100_000, latest=latest, now=NOW, cup_end=CUP_END)
        assert o.bet is not None and o.bet.kind == "bounded" and o.bet.floor == 1.0
        assert o.exit_plan is not None and o.exit_plan.kind == "basket" and o.idea_id == "arbitrage:set:9016+9017:nn"
        # leg limits floor_tick(price + 0.5 x 0.03 / 2): 0.3025 -> 0.30, 0.6825 -> 0.68 (keeping 0.02 of the 0.03 edge)
        assert [leg["limit"] for leg in o.legs] == [0.30, 0.68] and o.limit_price == pytest.approx(0.98)
        [r] = S.arbitrage_opportunities({"data": [v]}, [], 100_000, latest=latest, now=NOW, cup_end=CUP_END,
                                        regime="resolved_outcomes")
        assert r.bet.kind == "riskless" and r.exit_plan.hold_to_resolution is True

    def test_backtest_fade_net_of_spread(self) -> None:
        t0 = 1_789_999_800.0
        pts: List[PricePoint] = []
        for i in range(int(40 * H / 60) + 1):
            t = t0 + i * 60.0
            p = 0.50 + (0.005 if int(t // 300) % 2 else 0.0)
            if t >= t0 + 30 * H:
                back = min(1.0, max(0.0, (t - t0 - 31 * H) / (2 * H)))
                p += 0.18 - 0.8 * 0.18 * back
            pts.append(PricePoint(ts=t, price=round(p, 6), bid=round(p - 0.01, 6), ask=round(p + 0.01, 6)))
        gross = S.backtest_fade({"e1": pts}, net_of_spread=False)
        net = S.backtest_fade({"e1": pts})
        assert net.net_of_spread is True and gross.net_of_spread is False
        assert gross.n_surges == net.n_surges >= 1
        # half the 0.02 spread on the way in and half on the way out come off both returns
        assert net.avg_fade_return == pytest.approx(gross.avg_fade_return - 0.02, abs=1e-6)
        assert net.avg_hold_return == pytest.approx(-gross.avg_fade_return - 0.02, abs=1e-6)
        assert any("net of the spread" in n for n in net.notes)
        # candle-like points (no bid/ask): an assumed 0.02 spread, and a note says so
        bare = [PricePoint(ts=p.ts, price=p.price) for p in pts]
        assumed = S.backtest_fade({"e1": bare})
        assert assumed.avg_fade_return == pytest.approx(gross.avg_fade_return - 0.02, abs=1e-6)
        assert any("assumed 0.02 spread" in n for n in assumed.notes)
        # participant-only stats need stored attributions
        assert net.n_participant == 0 and net.participant_reversion_rate is None
        detected = t0 + 30 * H + 300
        stored = Surge(exchange_id="e1", market_id="", window="5m", window_s=300, start_ts=detected - 300,
                       end_ts=detected, start_price=0.5, end_price=0.68, change=0.18, direction="up", peak_price=0.68,
                       detected_at=detected, attribution=att())
        with_part = S.backtest_fade({"e1": pts}, surges=[stored])
        assert with_part.n_participant == 1 and with_part.participant_reversion_rate in (0.0, 1.0)
        assert any("participant-driven" in n for n in with_part.notes)


# --------------------------------------------------------------------------- generate_signals and the report


def full_inputs() -> StrategyInputs:
    """Value (TX), a NO basket (NV pair), a hole (WY), a fade and a carry, all on one snapshot."""
    nv = nv_inputs()
    wy = wy_inputs(fair=fv(0.94, eid="9033", source="demo", unc=0.005))
    infos = {**nv.infos, **wy.infos, "9026": info("9026", "318"), "9027": info("9027", "319",
                                                                               "Will the Republican Party win the Texas Senate?")}
    latest = {**nv.latest, **wy.latest, "9026": tick(0.51, 0.52), "9027": tick(0.48, 0.49), "e9": tick(0.69, 0.70),
              "e2": tick(0.97, 0.975)}
    infos["e9"] = info("e9", "m9", "Will Republicans win Ohio House District 9?")
    infos["e2"] = info("e2", "m2", "Will Democrats win the Maryland Senate race?", settle=NOW + 40 * H)
    races = {**nv.races, **wy.races, "9026": race("TX", "D"), "9027": race("TX", "R")}
    fvs = {"9026": fv(0.58, eid="9026"), "9027": fv(0.42, eid="9027"), "9033": wy.fair_values["9033"]}
    band = HighBand(exchange_id="e2", market_id="m2", side="YES", favorite_price=0.9725, time_in_band=1.0, mean=0.9725,
                    low=0.97, high=0.975, lookback_s=6 * H, stable=True, settlement_date=iso(NOW + 40 * H))
    return StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races=races, fair_values=fvs,
                          surges=[spike(eid="e9")], bands=[band], balance=100_000.0, initial_balance=100_000.0)


class TestGenerateSignals:
    def test_every_kind_unsized_and_complete(self) -> None:
        signals = S.generate_signals(full_inputs())
        kinds = {o.kind for o in signals}
        assert kinds == {"value", "basket", "hole", "fade", "carry"}
        for o in signals:
            assert o.suggested_shares == 0 and o.suggested_cost == 0.0 and o.sizing is None
            assert o.idea_id and o.bet is not None and o.bet_limit is not None and o.exit_plan is not None
            assert o.limit_price is not None and o.factor_delta is not None and o.settlement_regime == "unknown"
            if o.unit == "sets":
                assert all(leg["limit"] == D.floor_tick(leg["limit"]) for leg in o.legs)
                assert o.limit_price == pytest.approx(sum(leg["limit"] for leg in o.legs))
            else:
                assert o.limit_price == D.floor_tick(o.limit_price) or o.limit_price == o.entry_price
        # [updated, strategy-2] both TX ideas bet on the Democrat winning (YES-D and NO-R): one bet on one result, so
        # only the better-scored one is kept (a tie here: the first idea id)
        assert sorted(o.idea_id for o in signals if o.kind == "value") == ["value:x9026:yes"]
        json.dumps([o.to_dict() for o in signals], allow_nan=False)

    def test_deterministic_and_stable_ids(self) -> None:
        a = S.generate_signals(full_inputs())
        b = S.generate_signals(copy.deepcopy(full_inputs()))
        assert [o.idea_id for o in a] == [o.idea_id for o in b]
        assert [o.to_dict() for o in a] == [o.to_dict() for o in b]
        assert [(-o.score, o.idea_id) for o in a] == sorted((-o.score, o.idea_id) for o in a)

    def test_stale_quotes_give_nothing(self) -> None:
        inp = full_inputs()
        inp.latest = {}
        assert [o for o in S.generate_signals(inp) if o.kind != "watch"] == []

    def test_opposite_legs_of_one_race_keep_one_value_idea(self) -> None:
        inp = full_inputs()
        # fair values that say BOTH legs are cheap (inconsistent): YES on D and YES on R bet on different winners
        inp.fair_values["9027"] = fv(0.56, eid="9027")
        inp.latest["9027"] = tick(0.47, 0.48)
        values = [o for o in S.generate_signals(inp) if o.kind == "value"]
        assert len(values) == 1

    def test_growth_ranking_toss_up_above_longshot(self) -> None:
        infos = {"1": info("1", "a"), "2": info("2", "b")}
        latest = {"1": tick(0.49, 0.50), "2": tick(0.195, 0.20)}
        fvs = {"1": fv(0.56, eid="1", unc=0.0), "2": fv(0.235, eid="2", unc=0.0)}
        inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, fair_values=fvs,
                             settlement_regime="resolved_outcomes")
        ideas = S.generate_signals(inp)
        assert [o.exchange_id for o in ideas] == ["1", "2"]
        assert ideas[0].expected_return < ideas[1].expected_return  # the old score would have ranked them the other way

    def test_report_from_inputs(self) -> None:
        inp = full_inputs()
        r = S.report_from_inputs(inp, sizing="conservative", fair_value_status={"mode": "auto", "usable": 3, "total": 4})
        assert r.sizing_policy == "conservative" and r.settlement_regime == "unknown"
        assert r.fair_value == {"mode": "auto", "usable": 3, "total": 4}
        assert r.sizing["policy"] == "conservative" and r.sizing["alternative"]["policy"] == "chaser"
        assert "value" in r.headline and "basket" in r.headline and "hole" in r.headline
        for o in r.opportunities:
            assert o.sizing is not None and o.alt_sizing is not None
            assert o.suggested_shares == o.sizing["units"]
            if o.kind in ("value", "basket", "hole"):
                assert o.rationale[-1] == o.sizing["lines"][0]
        fade = next(o for o in r.opportunities if o.kind == "fade")
        assert "size" in fade.rationale[4] and f"{fade.suggested_shares:,} shares" in fade.rationale[4]
        hole = next(o for o in r.opportunities if o.kind == "hole")
        assert hole.suggested_shares == 500  # 1% of 100,000 at 0.705 allows 1,418; the order holds at most 500
        chaser = S.report_from_inputs(inp, sizing="chaser")
        assert chaser.sizing_policy == "chaser" and chaser.sizing["mode"] == "unknown_bar"
        json.dumps(r.to_dict(), allow_nan=False)
        json.dumps(chaser.to_dict(), allow_nan=False)
        with pytest.raises(ValueError):
            S.report_from_inputs(inp, sizing="yolo")

    def test_build_report_passes_the_new_inputs(self) -> None:
        inp = full_inputs()
        r = S.build_report(now=inp.now, surges=inp.surges, bands=inp.bands, latest=inp.latest, infos=inp.infos,
                           balance=100_000.0, initial_balance=100_000.0, leader_value=None, my_rank=None,
                           cup_end=inp.cup_end, fair_values=inp.fair_values, races=inp.races,
                           settlement_regime="resolved_outcomes", sizing="chaser")
        assert r.settlement_regime == "resolved_outcomes" and r.sizing_policy == "chaser"
        basket = next(o for o in r.opportunities if o.kind == "basket")
        assert basket.bet.kind == "riskless"


class TestBasketWording:
    """[integration] A basket's reason names binary legs by party and the race in words, not "NO YES" or a race key."""

    def test_binary_legs_are_named_by_party(self) -> None:
        legs = [{"exchange_id": "9024", "option": "YES", "party": "D", "side": "no", "price": 0.475},
                {"exchange_id": "9025", "option": "YES", "party": "R", "side": "no", "price": 0.475},
                {"exchange_id": "9013", "option": "Any other candidate", "party": "O", "side": "yes", "price": 0.04}]
        assert S._legs_text(legs) == "NO Democratic @ 0.475 + NO Republican @ 0.475 + YES Any other candidate @ 0.04"
        assert S._leg_name({"exchange_id": "1", "option": "YES", "party": None, "title": "Will X happen?"}) == "Will X happen?"

    def test_race_in_words(self) -> None:
        assert S._race_words(RaceRef("2026:GOVERNOR:NV", "GOVERNOR", "NV", party="D"), "k") == "the Nevada Governor race"
        assert S._race_words(RaceRef("2026:SENATE_CONTROL:US", "SENATE_CONTROL", "US"), "k") == "control of the U.S. Senate"
        assert S._race_words(RaceRef("2026:HOUSE:AZ-01", "HOUSE", "AZ", district="AZ-01"), "k") == "the AZ-01 House race"
        assert S._race_words(None, "2026:SENATE:XX") == "2026:SENATE:XX"


# --------------------------------------------------------------------------- strategy fixer regressions


def tx_pair(*, r_bid: float = 0.495, books: bool = True) -> StrategyInputs:
    """YES on the Texas Democrat and NO on the Texas Republican: both pay only if the Democrat wins."""
    d_eid, r_eid = "9026", "9027"
    infos = {d_eid: info(d_eid, "318"), r_eid: info(r_eid, "319", "Will the Republican Party win the Texas Senate?")}
    db = book([(0.495, 50000)], [(0.505, 50000), (0.51, 50000)]) if books else None
    rb = book([(r_bid, 50000), (r_bid - 0.005, 50000)], [(r_bid + 0.01, 50000)]) if books else None
    latest = {d_eid: tick(0.495, 0.505, book=db), r_eid: tick(r_bid, r_bid + 0.01, book=rb)}
    fvs = {d_eid: fv(0.58, eid=d_eid, unc=0.005), r_eid: fv(0.42, eid=r_eid, unc=0.005)}
    races = {d_eid: race("TX", "D"), r_eid: race("TX", "R")}
    return StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, fair_values=fvs, races=races)


class TestStrategyFixerRegressions:
    def test_strategy_2_same_winner_legs_are_one_value_idea(self) -> None:
        inp = tx_pair()
        values = [o for o in S.generate_signals(inp) if o.kind == "value"]
        assert [o.idea_id for o in values] == ["value:x9026:yes"]  # NO on x9027 is the same bet: dropped
        # alone, the NO-R leg is an idea of its own (the rule removes only the duplicate)
        alone = copy.deepcopy(inp)
        alone.fair_values.pop("9026")
        assert [o.idea_id for o in S.generate_signals(alone) if o.kind == "value"] == ["value:x9027:no"]
        # one quarter-Kelly bet on "D wins TX", not two (13,246 shares, half-Kelly, before)
        c = SizingContext(now=NOW, equity=100_000.0, cash=100_000.0, free_cash=100_000.0, start_capital=100_000.0,
                          cup_end=CUP_END, days_left=(CUP_END - NOW) / 86400, params=P)
        decs = Z.ConservativePolicy().size_all(values, c)
        assert sum(d.units for d in decs.values()) == Z.ConservativePolicy().size(values[0], c).units == 6623
        # the better-scored leg is the one kept: NO-R at 0.495 beats YES-D at 0.505
        cheaper = [o for o in S.generate_signals(tx_pair(r_bid=0.505, books=False)) if o.kind == "value"]
        assert [o.idea_id for o in cheaper] == ["value:x9027:no"]
        # different winners (D38) are still one idea too
        both_yes = tx_pair(books=False)
        both_yes.latest["9027"] = tick(0.34, 0.35)
        both_yes.fair_values["9027"] = fv(0.42, eid="9027", unc=0.005)
        kept = [o for o in S.generate_signals(both_yes) if o.kind == "value"]
        assert len(kept) == 1

    @pytest.mark.parametrize("office,title", [
        ("HOUSE_CONTROL", "Which party will control the House after the midterms?"),
        ("SENATE_CONTROL", "Which party will control the Senate after the midterms?")])
    def test_strategy_6_chamber_control_is_a_slow_count(self, office: str, title: str) -> None:
        d_race = RaceRef(f"2026:{office}:US", office, "US", party="D")
        r_race = RaceRef(f"2026:{office}:US", office, "US", party="R")
        assert S.is_chamber(d_race) and not S.is_chamber(race("TX"))
        assert S.called_prob(d_race, 0.69, P) == P.called_prob_slow  # was called_prob_fast (0.85)
        assert S.called_prob(d_race, 0.97, P) == S.CHAMBER_SAFE_CALLED_PROB < P.called_prob_safe
        assert S.called_prob(race("TX"), 0.69, P) == P.called_prob_fast  # state races keep their rule
        assert S._exit_before("unknown", ["US"], None, CUP_END, P) == pytest.approx(CUP_END - P.closeout_buffer_s)
        assert S._exit_before("unknown", ["TX"], None, CUP_END, P) is None
        infos = {"9014": info("9014", "700", title, option="Democrats"),
                 "9015": info("9015", "700", title, option="Republicans")}
        latest = {"9014": tick(0.68, 0.69, book=book([(0.68, 5000)], [(0.69, 5000)])),
                  "9015": tick(0.34, 0.35, book=book([(0.34, 5000)], [(0.35, 5000)]))}
        races = {"9014": d_race, "9015": r_race}
        for regime in ("vwap_closeout", "unknown"):
            inp = StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races=races,
                                 settlement_regime=regime)
            [b] = [o for o in S.basket_opportunities(inp, P) if o.side == "no"]
            assert b.bet is not None and b.bet.kind == "bounded" and b.called_prob == P.called_prob_slow
            assert b.exit_plan is not None and b.exit_plan.exit_before_ts == pytest.approx(CUP_END - P.closeout_buffer_s)
            assert not any("fast count" in line for line in b.rationale)
            assert any("chamber control is decided by the last seats counted" in line for line in b.rationale)
        resolved = StrategyInputs(now=NOW, cup_end=CUP_END, infos=infos, latest=latest, races=races,
                                  settlement_regime="resolved_outcomes")
        [b] = [o for o in S.basket_opportunities(resolved, P) if o.side == "no"]
        assert b.bet is not None and b.bet.kind == "riskless"  # a real 1/0 resolution: riskless again
