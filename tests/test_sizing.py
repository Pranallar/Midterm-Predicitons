"""Package B (sizing): conservative and chaser policies, bar estimate, Markov ceiling, explanations. See docs/PAPER_TRADING.md §5.8."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import sizing as Z
from supermarket_bot import strategy as S
from supermarket_bot.models import (
    BarEstimate,
    BetShape,
    ExchangeInfo,
    ExitPlan,
    ExposureSummary,
    HighBand,
    LeaderboardSnapshot,
    Opportunity,
    PricePoint,
    SizingContext,
    StrategyInputs,
)

H = 3600.0
DAY = 86400.0
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()
CUP_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc).timestamp()
W = 100_000.0
HAND_EV = 0.053175
HAND_LEVELS = [(0.52, 2000.0), (0.53, 3000.0), (0.535, 5000.0)]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def bar(value: float = 221_500.0) -> BarEstimate:
    est = BarEstimate(value=value, low=value, high=value, third_value=value, explanation=["bar line"])
    est.method = "third"
    return est


def ctx(*, equity: float = W, free: Optional[float] = None, exposure: Optional[ExposureSummary] = None,
        b: Optional[BarEstimate] = None, days_left: float = 30.0, prev_mode: Optional[str] = None,
        mode_since: Optional[float] = None, all_collateral: bool = False, expected_profit: float = 0.0) -> SizingContext:
    return SizingContext(now=NOW, equity=equity, cash=equity, free_cash=equity if free is None else free,
                         start_capital=W, cup_end=NOW + days_left * DAY, days_left=days_left,
                         exposure=exposure or ExposureSummary(), bar=b, prev_mode=prev_mode, mode_since=mode_since,
                         all_collateral=all_collateral, expected_profit=expected_profit)


def value_idea(*, entry: float = 0.52, ev: float = HAND_EV, levels: Sequence[Tuple[float, float]] = (),
               max_units: Optional[float] = None, limit: Optional[float] = 0.535, sigma: Optional[float] = 0.01,
               delta: float = 0.0, score: float = 0.3, race_key: str = "2026:SENATE:TX", idea_id: str = "value:x1:yes",
               kind: str = "value", settles: Optional[bool] = False, called: Optional[float] = 0.85,
               side: str = "yes") -> Opportunity:
    bet = BetShape("binary", p=entry + ev, gain=1 - entry, loss=entry, cost=entry)
    lv = list(levels)
    return Opportunity(kind=kind, exchange_id=idea_id.split(":")[1][1:], market_id="m", title="t", option=None, side=side,
                       entry_price=entry, target_price=None, stop_price=None, prob_win=entry + ev, edge=ev,
                       expected_return=ev / entry, horizon_hours=None, suggested_shares=0, suggested_cost=0.0, score=score,
                       confidence=0.6, idea_id=idea_id, race_key=race_key, bet=bet, limit_price=limit, levels=lv,
                       max_units=max_units if max_units is not None else (sum(q for _, q in lv) if lv else None),
                       fv_uncertainty=sigma, factor_delta=delta, fair_value=entry + ev,
                       settles_before_cup_end=settles, called_prob=called)


def set_idea(kind: str = "bounded", *, cost: float = 0.95, edge: float = 0.05, gain: Optional[float] = None,
             loss: float = 0.55, limit: float = 0.97, idea_id: str = "basket:set:1+2:nn",
             levels: Sequence[Tuple[float, float]] = (), max_units: Optional[float] = None) -> Opportunity:
    if kind == "riskless":
        bet = BetShape("riskless", p=1.0, gain=edge, loss=0.0, cost=cost, floor=1.0)
    else:
        bet = BetShape("bounded", p=0.99, gain=0.02 if gain is None else gain, loss=loss, cost=cost, floor=1.0,
                       tail_prob=0.01)
    return Opportunity(kind="basket", exchange_id=None, market_id="m", title="NV", option=None, side="no",
                       entry_price=cost, target_price=None, stop_price=None, prob_win=bet.p, edge=edge,
                       expected_return=edge / cost, horizon_hours=None, suggested_shares=0, suggested_cost=0.0, score=0.1,
                       confidence=0.9, legs=[{"exchange_id": "1", "side": "no", "price": 0.40},
                                             {"exchange_id": "2", "side": "no", "price": 0.55}],
                       unit="sets", idea_id=idea_id, race_key="2026:GOVERNOR:NV", bet=bet, limit_price=limit,
                       levels=list(levels), max_units=max_units, factor_delta=0.0)


# --------------------------------------------------------------------------- Kelly mechanics


class TestKellyMechanics:
    @pytest.mark.parametrize("p,c", [(0.6, 0.5), (0.99, 0.31), (0.978, 0.975), (0.3, 0.05), (0.55, 0.52)])
    def test_binary_equals_size_position(self, p: float, c: float) -> None:
        units = Z.kelly_units(p, 1 - c, c, W, 0.25)
        assert math.floor(min(units, 0.08 * W / c) + 1e-9) == S.size_position(p, c, W)

    @pytest.mark.parametrize("p,gain,loss,entry", [(0.66, 0.085, 0.095, 0.31), (0.6, 0.1, 0.05, 0.4), (0.7, 0.03, 0.06, 0.5)])
    def test_bracket_equals_bracket_shares(self, p: float, gain: float, loss: float, entry: float) -> None:
        units = Z.kelly_units(p, gain, loss, W, 0.25)
        assert math.floor(min(units, 0.08 * W / entry) + 1e-9) == S._bracket_shares(p, gain, loss, entry, W, 0.25, 0.08, None)

    def test_kelly_zero_cases(self) -> None:
        assert Z.kelly_units(0.5, 0.5, 0.5, W, 0.25) == 0.0
        assert Z.kelly_units(0.6, 0.0, 0.5, W, 0.25) == 0.0 and Z.kelly_units(0.6, 0.5, 0.5, 0, 0.25) == 0.0

    def test_bet_at_cost(self) -> None:
        b = Z.bet_at_cost(BetShape("binary", p=0.57, gain=0.48, loss=0.52, cost=0.52), 0.535)
        assert (b.p, b.gain, b.loss, b.cost) == (0.57, 0.465, 0.535, 0.535)
        br = Z.bet_at_cost(BetShape("bracket", p=0.66, gain=0.085, loss=0.095, cost=0.31), 0.32)
        assert (br.gain, br.loss) == (pytest.approx(0.075), pytest.approx(0.105))
        r = Z.bet_at_cost(BetShape("riskless", p=1.0, gain=0.05, loss=0.0, cost=0.95, floor=1.0), 0.97)
        assert (r.gain, r.loss) == (pytest.approx(0.03), 0.0)
        bd = Z.bet_at_cost(BetShape("bounded", p=0.99, gain=0.02, loss=0.55, cost=0.95, floor=1.0, tail_prob=0.01), 0.96)
        assert (bd.gain, bd.loss, bd.tail_prob) == (pytest.approx(0.01), pytest.approx(0.56), 0.01)

    def test_hand_example_touch_certainty_and_depth(self) -> None:
        bet = BetShape("binary", p=0.52 + HAND_EV, gain=0.48, loss=0.52, cost=0.52)
        assert Z.depth_kelly_units(bet, [], W, 0.25) == (5326, None)
        cert = Z.certainty(HAND_EV, 0.01)
        assert cert == pytest.approx(0.9658, abs=1e-4)
        assert Z.depth_kelly_units(bet, [], W, 0.25 * cert)[0] == 5144
        units, avg = Z.depth_kelly_units(bet, HAND_LEVELS, W, 0.25 * cert)
        assert units == 4601 and avg == pytest.approx(0.52565, abs=1e-5)
        assert Z.depth_kelly_units(bet, HAND_LEVELS, W, 0.25 * cert, upper=1000)[0] == 1000
        assert Z.depth_kelly_units(bet, [(0.52, 300.0)], W, 0.25)[0] == 300  # never beyond the book

    def test_policy_reproduces_the_hand_example(self) -> None:
        d = Z.ConservativePolicy().size(value_idea(levels=HAND_LEVELS), ctx())
        assert d.units == 4601 and d.avg_cost == pytest.approx(0.52565, abs=1e-5)
        assert d.certainty == pytest.approx(0.9658, abs=1e-4) and "kelly" in d.capped_by and "certainty" in d.capped_by
        assert d.stake == pytest.approx(4601 * 0.52565, abs=1.0)
        text = " | ".join(d.lines)
        assert "uncertainty 0.01: 97% certainty" in text
        assert "expected average fill of 0.5257 across the book, not the 0.52 touch" in text
        touch = Z.ConservativePolicy().size(value_idea(levels=(), max_units=None), ctx())
        assert touch.units == 5144 and touch.avg_cost is None and any("Depth unknown" in line for line in touch.lines)

    def test_small_helpers(self) -> None:
        assert Z.certainty(0.0, 0.01) == 0.0 and Z.certainty(0.05, None) == 1.0 and Z.certainty(0.05, 0.0) == 1.0
        assert [round(Z.factor_scale(n), 4) for n in (1, 2, 3, 5)] == [1.0, 0.6667, 0.5, 0.3333]
        assert Z.bold_max_cost(221_500, W) == pytest.approx(0.3306, abs=1e-4)
        assert Z.markov_ceiling(1.04, 2.215) == pytest.approx(0.4695, abs=1e-4) and Z.markov_ceiling(3.0, 2.0) == 1.0
        assert Z.markov_sentence(1.04, 2.215) == ("A strategy whose expected multiple is 1.04x reaches 2.2x with probability "
                                                  "at most 47%; with no edge (fair prices) the bound is 1/M = 45%.")


# --------------------------------------------------------------------------- conservative policy


def legacy_scenario() -> Dict[str, Any]:
    """The legacy no-book scenario of tests/test_strategy.py: a fade, a carry, a constraint arbitrage."""
    from supermarket_bot.models import Attribution, Surge

    surge = Surge(exchange_id="e1", market_id="m1", window="1h", window_s=3600, start_ts=NOW - 2400, end_ts=NOW - 600,
                  start_price=0.51, end_price=0.69, change=0.18, direction="up", peak_price=0.69, detected_at=NOW - 600,
                  current_price=0.69, id=7,
                  attribution=Attribution(verdict="participants", confidence=0.64, reversion_odds=0.66, summary=""))
    band = HighBand(exchange_id="e2", market_id="m2", side="YES", favorite_price=0.9725, time_in_band=1.0, mean=0.9725,
                    low=0.9675, high=0.9775, lookback_s=6 * H, stable=True, settlement_date=iso(NOW + 40 * H))
    latest = {"e1": PricePoint(ts=NOW, price=0.695, bid=0.69, ask=0.70), "e2": PricePoint(ts=NOW, price=0.9725, bid=0.97, ask=0.975)}
    infos = {"e1": ExchangeInfo("e1", "m1", None, "Will Republicans win Ohio House District 9?", iso(NOW + 30 * DAY)),
             "e2": ExchangeInfo("e2", "m2", None, "Will Democrats win the Maryland Senate race?", iso(NOW + 40 * H))}
    violation = {"type": "monotonic", "violationAmount": 0.05, "reason": "x",
                 "suggestedCorrectiveTrades": [
                     {"exchangeId": "11", "outcomeSide": "Yes", "action": "Buy", "marketId": "5", "currentPrice": 0.50},
                     {"exchangeId": "12", "outcomeSide": "Yes", "action": "Sell", "marketId": "6", "currentPrice": 0.55}]}
    return dict(surge=surge, band=band, latest=latest, infos=infos, constraints={"data": [violation]})


class TestConservative:
    @pytest.mark.parametrize("balance", [100_000.0, 60_000.0, 7_500.0])
    def test_reproduces_legacy_sizes_without_books(self, balance: float) -> None:
        sc = legacy_scenario()
        fade = S.fade_opportunity(sc["surge"], sc["latest"]["e1"], sc["infos"]["e1"], NOW, balance, cup_end=CUP_END)
        carry = S.carry_opportunity(sc["band"], sc["latest"]["e2"], sc["infos"]["e2"], NOW, CUP_END, balance)
        [arb] = S.arbitrage_opportunities(sc["constraints"], [], balance)
        r = S.build_report(now=NOW, surges=[sc["surge"]], bands=[sc["band"]], latest=sc["latest"], infos=sc["infos"],
                           balance=balance, initial_balance=100_000.0, leader_value=None, my_rank=None, cup_end=CUP_END,
                           constraints=sc["constraints"])
        got = {o.kind: o.suggested_shares for o in r.opportunities}
        assert got == {"fade": fade.suggested_shares, "carry": carry.suggested_shares, "arbitrage": arb.suggested_shares}
        assert got["fade"] == math.floor(0.08 * balance / 0.31) and got["arbitrage"] == math.floor(0.08 * balance / 0.95)
        for o in r.opportunities:
            assert o.sizing["policy"] == "conservative" and o.alt_sizing["policy"] == "chaser"
            assert o.suggested_cost == pytest.approx(o.suggested_shares * o.entry_price, abs=0.01)

    def test_every_cap_fires_and_is_named(self) -> None:
        pol = Z.ConservativePolicy()
        # idea cap: a big, certain edge
        d = pol.size(value_idea(ev=0.2, sigma=0.0, levels=()), ctx())
        assert d.capped_by == ["idea_cap"] and d.units == math.floor(8000 / 0.52)
        assert "Capped at 8% of equity per idea" in d.lines
        # race cap: 11,000 already in the race
        d = pol.size(value_idea(ev=0.2, sigma=0.0), ctx(exposure=ExposureSummary(by_race={"2026:SENATE:TX": 11_000.0})))
        assert d.capped_by == ["race_cap"] and d.units == math.floor(1000 / 0.52)
        assert any("per race (2026:SENATE:TX already holds 11,000 SUSQies)" in line for line in d.lines)
        # swing (tilt) cap: +3,000 SUSQies per point already; a toss-up D share adds 0.057
        tilt = ExposureSummary(national_tilt_d=3000.0)
        d = pol.size(value_idea(ev=0.2, sigma=0.0, delta=0.057), ctx(exposure=tilt))
        assert d.capped_by == ["tilt_cap"] and d.units == math.floor((W * 0.10 / 3 - 3000) / 0.057 + 1e-9)
        assert any("3-point national swing toward the Republicans would cost this portfolio" in line for line in d.lines)
        # total cap
        d = pol.size(value_idea(ev=0.2, sigma=0.0), ctx(exposure=ExposureSummary(gross_cost=59_500.0)))
        assert d.capped_by == ["total_cap"] and d.units == math.floor(500 / 0.52)
        # cash (reserved at the 0.535 limit)
        d = pol.size(value_idea(ev=0.2, sigma=0.0), ctx(free=1000.0))
        assert d.capped_by == ["cash"] and d.units == math.floor(1000 / 0.535)
        assert any("Capped by the free cash (1,000 SUSQies left after pending orders)" == line for line in d.lines)
        # depth
        d = pol.size(value_idea(ev=0.2, sigma=0.0, levels=[(0.52, 300.0)]), ctx())
        assert d.capped_by == ["depth"] and d.units == 300
        assert any("The book offers 300 shares up to 0.535" == line for line in d.lines)
        # Kelly binds
        d = pol.size(value_idea(sigma=0.0), ctx())
        assert d.capped_by == ["kelly"] and d.units == 5326

    def test_swing_cap_only_blocks_increases_and_ignores_sets(self) -> None:
        pol = Z.ConservativePolicy()
        full = ExposureSummary(national_tilt_d=5000.0)  # 15,000 at risk for a 3-point swing: over the 10,000 cap
        assert pol.size(value_idea(ev=0.2, sigma=0.0, delta=0.057), ctx(exposure=full)).units == 0
        reduce = pol.size(value_idea(ev=0.2, sigma=0.0, delta=-0.057), ctx(exposure=full))
        assert reduce.units == math.floor(8000 / 0.52) and "tilt_cap" not in reduce.capped_by
        sets = pol.size(set_idea(), ctx(exposure=full))
        assert sets.units == 8421 and "tilt_cap" not in sets.capped_by

    def test_size_all_never_exceeds_the_free_cash(self) -> None:
        pol = Z.ConservativePolicy()
        opps = [value_idea(ev=0.2, sigma=0.0, idea_id=f"value:x{i}:yes", race_key=f"r{i}", score=1.0 - i / 100)
                for i in range(8)]
        out = pol.size_all(opps, ctx(free=12_000.0))
        reserved = sum(out[o.idea_id].units * o.limit_price for o in opps)
        assert reserved <= 12_000.0 + 1e-6
        assert [out[o.idea_id].units for o in opps][:1] == [math.floor(8000 / 0.52)]
        assert "cash" in out[opps[1].idea_id].capped_by
        assert all(out[o.idea_id].units == 0 for o in opps[2:])
        assert all(out[o.idea_id].lines for o in opps)

    def test_size_all_counts_earlier_ideas_against_the_caps(self) -> None:
        pol = Z.ConservativePolicy()
        opps = [value_idea(ev=0.2, sigma=0.0, idea_id=f"value:x{i}:yes", score=1.0 - i / 10) for i in range(3)]
        out = pol.size_all(opps, ctx())
        first, second, third = (out[o.idea_id] for o in opps)
        assert first.units == math.floor(8000 / 0.52)
        assert second.capped_by == ["race_cap"] and second.units == math.floor((12_000 - first.units * 0.52) / 0.52 + 1e-9)
        assert third.units == 0

    def test_riskless_bounded_and_all_collateral(self) -> None:
        pol = Z.ConservativePolicy()
        riskless = pol.size(set_idea("riskless"), ctx())
        assert riskless.units == 8421 and riskless.capped_by == ["basket_cap"]
        bounded = pol.size(set_idea("bounded"), ctx())
        assert bounded.units == 8421 and bounded.capped_by == ["basket_cap"]  # quarter-Kelly would allow ~32,500
        assert any("quarter-Kelly on the bounded bet allows 32," in line for line in bounded.lines)
        # a bounded bet whose Kelly is small is sized by Kelly
        thin = pol.size(set_idea("bounded", gain=0.006, edge=0.006), ctx())
        assert thin.capped_by == ["kelly"] and 0 < thin.units < 8421
        # free cash binds without ALL collateral, not with it (a riskless set then needs max(0, 0.97 - 1.00) = 0)
        assert pol.size(set_idea("riskless"), ctx(free=100.0)).units == math.floor(100 / 0.97)
        coll = pol.size(set_idea("riskless"), ctx(free=100.0, all_collateral=True))
        assert coll.units == 8421 and coll.stake == 0.0 and any("ALL collateral" in line for line in coll.lines)
        # no all-collateral credit for a bounded set
        assert pol.size(set_idea("bounded"), ctx(free=100.0, all_collateral=True)).units == math.floor(100 / 0.97)

    def test_set_depth_and_hole(self) -> None:
        pol = Z.ConservativePolicy()
        d = pol.size(set_idea("riskless", levels=[(0.95, 1000.0), (0.96, 2000.0)], max_units=3000.0), ctx())
        assert d.units == 3000 and d.capped_by == ["depth"] and d.avg_cost == pytest.approx((950 + 1920) / 3000)
        hole_bet = BetShape("fixed", p=0.02, gain=0.205, loss=0.705, cost=0.705, cap_pct=0.01)
        hole = Opportunity(kind="hole", exchange_id="9033", market_id="325", title="WY", option=None, side="yes",
                           entry_price=0.705, target_price=0.91, stop_price=None, prob_win=0.02, edge=0.0041,
                           expected_return=0.0058, horizon_hours=6, suggested_shares=0, suggested_cost=0.0, score=0.003,
                           confidence=0.5, idea_id="hole:x9033:yes", bet=hole_bet, order_type="maker", limit_price=0.705,
                           max_units=500.0, factor_delta=-0.02)
        d = pol.size(hole, ctx())
        assert d.units == 500 and d.capped_by == ["depth"] and any("rest in one order" in line for line in d.lines)
        assert pol.size(hole, ctx(equity=20_000.0)).units == math.floor(200 / 0.705)

    def test_zero_decisions_have_lines(self) -> None:
        pol = Z.ConservativePolicy()
        watch = value_idea()
        watch.kind, watch.bet = "watch", None
        for opp in (watch, value_idea(ev=-0.01)):
            d = pol.size(opp, ctx())
            assert d.units == 0 and d.lines and d.capped_by == ["zero_edge"]
        assert pol.size(value_idea(), ctx(equity=0.0)).units == 0

    def test_explain(self) -> None:
        e = Z.ConservativePolicy().explain(ctx())
        assert e["policy"] == "conservative" and e["mode"] is None and e["bar"] is None and e["M"] is None
        assert e["lines"] == list(Z.CONSERVATIVE_LINES)
        e = Z.ConservativePolicy().explain(ctx(b=bar(), expected_profit=4000.0))
        assert e["M"] == pytest.approx(2.215) and e["ev_multiple"] == pytest.approx(1.04)
        assert e["markov_ceiling"] == pytest.approx(0.4695, abs=1e-4) and e["bar"]["value"] == 221_500
        json.dumps(e, allow_nan=False)


# --------------------------------------------------------------------------- chaser policy


class TestChaserModes:
    @pytest.mark.parametrize("value,days,mode", [(120_000.0, 30, "near"), (221_500.0, 30, "chase"),
                                                 (1_200_000.0, 30, "out_of_reach"), (None, 30, "unknown_bar"),
                                                 (221_500.0, 1, "swing"), (1_200_000.0, 1, "out_of_reach")])
    def test_modes(self, value: Optional[float], days: float, mode: str) -> None:
        b = bar(value) if value is not None else None
        assert Z.ChaserPolicy().mode(ctx(b=b, days_left=days)) == mode

    def test_hysteresis(self) -> None:
        pol = Z.ChaserPolicy()
        long_ago, recent = NOW - 7 * H, NOW - 1 * H
        assert pol.mode(ctx(b=bar(950_000.0), prev_mode="chase", mode_since=long_ago)) == "chase"  # M 9.5
        assert pol.mode(ctx(b=bar(950_000.0), prev_mode="out_of_reach", mode_since=long_ago)) == "out_of_reach"
        assert pol.mode(ctx(b=bar(850_000.0), prev_mode="out_of_reach", mode_since=long_ago)) == "chase"  # M 8.5 < 9
        assert pol.mode(ctx(b=bar(1_150_000.0), prev_mode="chase", mode_since=long_ago)) == "out_of_reach"  # 11.5, 7 h
        assert pol.mode(ctx(b=bar(1_150_000.0), prev_mode="chase", mode_since=recent)) == "chase"  # 11.5 after 1 h
        assert pol.mode(ctx(b=bar(1_050_000.0), prev_mode="chase", mode_since=long_ago)) == "chase"  # 10.5 < 11
        assert pol.mode(ctx(b=bar(125_000.0), prev_mode="chase", mode_since=long_ago)) == "chase"  # 1.25 > 1.17
        assert pol.mode(ctx(b=bar(115_000.0), prev_mode="chase", mode_since=long_ago)) == "near"
        assert pol.mode(ctx(b=bar(140_000.0), prev_mode="near", mode_since=long_ago)) == "near"  # 1.40 < 1.43
        assert pol.mode(ctx(b=bar(150_000.0), prev_mode="near", mode_since=long_ago)) == "chase"
        # swing and unknown_bar switch at once
        assert pol.mode(ctx(b=bar(221_500.0), days_left=1, prev_mode="chase", mode_since=recent)) == "swing"
        assert pol.mode(ctx(b=None, prev_mode="chase", mode_since=recent)) == "unknown_bar"
        assert pol.mode(ctx(b=bar(221_500.0), prev_mode="unknown_bar", mode_since=recent)) == "chase"
        kept = pol.explain(ctx(b=bar(1_150_000.0), prev_mode="chase", mode_since=recent))
        assert kept["kept_by_hysteresis"] is True and any("kept by hysteresis" in line for line in kept["lines"])

    def test_explain_lines(self) -> None:
        e = Z.ChaserPolicy().explain(ctx(b=bar(), expected_profit=4000.0))
        text = " | ".join(e["lines"])
        assert e["policy"] == "chaser" and e["mode"] == "chase" and e["M"] == pytest.approx(2.215)
        assert "bar line" in text and "M = the bar / your value = 221,500 / 100,000 = 2.21" in text
        assert Z.markov_sentence(1.04, 2.215) in e["lines"]
        assert "Mode: chase" in text and "full Kelly" in text and "at most 5 directional ideas" in text
        assert Z.SLATE_LINE in e["lines"] and Z.CASH_LINE in e["lines"]
        far = Z.ChaserPolicy().explain(ctx(b=bar(1_200_000.0)))
        assert Z.OUT_OF_REACH_LINE in far["lines"]
        unknown = Z.ChaserPolicy().explain(ctx())
        assert unknown["mode"] == "unknown_bar" and unknown["bar"] is None and unknown["M"] is None
        json.dumps(e, allow_nan=False)


class TestChaserSizing:
    def test_full_kelly_with_depth_half_kelly_without(self) -> None:
        pol = Z.ChaserPolicy()
        c = ctx(b=bar())
        with_depth = pol.size(value_idea(sigma=0.0, levels=[(0.52, 100_000.0)], limit=0.52), c)
        assert with_depth.mode == "chase"
        assert with_depth.units == math.floor(min(Z.kelly_units(0.52 + HAND_EV, 0.48, 0.52, W, 1.0), 0.25 * W / 0.52) + 1e-9)
        blind = pol.size(value_idea(sigma=0.0), c)
        assert blind.units == math.floor(Z.kelly_units(0.52 + HAND_EV, 0.48, 0.52, W, 0.5) + 1e-9)
        assert any("Depth unknown: at most half-Kelly" in line for line in blind.lines)

    def test_certainty_on_every_directional_idea(self) -> None:
        pol = Z.ChaserPolicy()
        fade = value_idea(kind="fade", sigma=None, idea_id="fade:x1:no:s1", levels=[(0.52, 100_000.0)], limit=0.52)
        d = pol.size(fade, ctx(b=bar()))
        assert d.certainty == pytest.approx(Z.certainty(HAND_EV, Z.DEFAULT_SIGMA["fade"]))
        assert Z.ConservativePolicy().size(fade, ctx()).certainty is None  # conservative: fades unscaled

    def test_out_of_reach_is_conservative(self) -> None:
        opp = value_idea(levels=HAND_LEVELS)
        far = Z.ChaserPolicy().size(opp, ctx(b=bar(1_200_000.0)))
        cons = Z.ConservativePolicy().size(opp, ctx())
        assert far.units == cons.units == 4601 and far.mode == "out_of_reach" and Z.OUT_OF_REACH_LINE in far.lines

    def test_slate_of_five_same_sign_ideas(self) -> None:
        opps = [value_idea(sigma=0.0, delta=0.057, idea_id=f"value:x{i}:yes", race_key=f"r{i}", score=1 - i / 10,
                           levels=[(0.52, 1e6)], limit=0.52) for i in range(5)]
        out = Z.ChaserPolicy().size_all(opps, ctx(b=bar(), exposure=ExposureSummary()))
        for o in opps:
            d = out[o.idea_id]
            assert d.factor_scale == pytest.approx(1 / 3)
            assert any("5 Democratic-leaning ideas share one national polling error: each gets 33%" in line for line in d.lines)
        single = Z.ChaserPolicy().size(opps[0], ctx(b=bar()))
        assert single.factor_scale == pytest.approx(1.0)

    def test_top_k_counts_held_positions(self) -> None:
        held = ExposureSummary(directional={"a": 0.5, "b": 0.6, "c": 0.7}, directional_sign={"a": 1, "b": 1, "c": -1})
        opps = [value_idea(sigma=0.0, idea_id=f"value:x{i}:yes", race_key=f"r{i}", score=1 - i / 10) for i in range(4)]
        out = Z.ChaserPolicy().size_all(opps, ctx(b=bar(), exposure=held))
        units = [out[o.idea_id].units for o in opps]
        assert units[0] > 0 and units[1] > 0 and units[2:] == [0, 0]
        assert out[opps[2].idea_id].capped_by == ["top_k"] and out[opps[3].idea_id].capped_by == ["top_k"]

    def test_replacement_when_full(self) -> None:
        held = ExposureSummary(directional={f"h{i}": 0.3 + i / 10 for i in range(5)})
        best = value_idea(sigma=0.0, idea_id="value:x9:yes", score=0.5)
        out = Z.ChaserPolicy().size_all([best, value_idea(sigma=0.0, idea_id="value:x8:yes", score=0.44)],
                                        ctx(b=bar(), exposure=held))
        d = out["value:x9:yes"]
        assert d.units == 0 and d.replaces == "h0"
        assert "Better than the weakest held idea (score 0.50 vs 0.30): sell that one first" in d.lines
        assert out["value:x8:yes"].replaces is None and out["value:x8:yes"].capped_by == ["top_k"]
        # not 50% better: no replacement
        out = Z.ChaserPolicy().size_all([value_idea(sigma=0.0, idea_id="value:x9:yes", score=0.44)],
                                        ctx(b=bar(), exposure=held))
        assert out["value:x9:yes"].replaces is None and out["value:x9:yes"].units == 0

    def test_one_bold_bet(self) -> None:
        pol = Z.ChaserPolicy()
        cheap = value_idea(entry=0.30, ev=0.05, sigma=0.0, idea_id="value:x1:yes", score=0.9, settles=True, limit=0.30)
        other = value_idea(entry=0.25, ev=0.04, sigma=0.0, idea_id="value:x2:yes", score=0.5, settles=True,
                           race_key="r2", limit=0.25)
        out = pol.size_all([cheap, other], ctx(b=bar(), days_left=1.0))
        bold = out["value:x1:yes"]
        assert bold.bold is True and bold.mode == "swing" and bold.units == math.floor(52_071.43 / 0.30)
        assert "Bold bet: if it wins, your value reaches the 221,500 bar; it loses 52,071 otherwise" in bold.lines
        assert out["value:x2:yes"].bold is False  # never two
        # an open bold bet already exists: no second one
        again = pol.size_all([cheap], ctx(b=bar(), days_left=1.0, exposure=ExposureSummary(bold_idea="value:x7:yes")))
        assert again["value:x1:yes"].bold is False

    def test_no_bold_bet_above_the_threshold(self) -> None:
        pricey = value_idea(entry=0.45, ev=0.05, sigma=0.0, idea_id="value:x1:yes", score=0.9, settles=True, limit=0.45)
        out = Z.ChaserPolicy().size_all([pricey], ctx(b=bar(), days_left=1.0))
        d = out["value:x1:yes"]
        assert d.bold is False and d.units > 0
        assert ("No bold bet: the cheapest qualifying contract costs 0.45; above 0.33 even a 60% stake cannot reach the "
                "bar (it would win to 173,333), so a swing would waste the variance") in d.lines
        # a market neither settling before the end nor likely called: not a bold candidate
        slow = value_idea(entry=0.30, ev=0.05, sigma=0.0, idea_id="value:x2:yes", settles=False, called=0.4, limit=0.30)
        d = Z.ChaserPolicy().size_all([slow], ctx(b=bar(), days_left=1.0))["value:x2:yes"]
        assert d.bold is False and any(line.startswith("No bold bet") for line in d.lines)

    def test_chaser_sets_cap_and_never_more_than_cash(self) -> None:
        pol = Z.ChaserPolicy()
        assert pol.size(set_idea("bounded"), ctx(b=bar())).units == 21052  # 20% per set idea
        assert pol.size(set_idea("riskless"), ctx(b=bar())).units == 21052
        assert pol.size(set_idea("riskless"), ctx(b=bar(), free=500.0)).units == math.floor(500 / 0.97)
        opps = [value_idea(ev=0.3, sigma=0.0, idea_id=f"value:x{i}:yes", race_key=f"r{i}", score=1 - i / 10) for i in range(5)]
        out = pol.size_all(opps, ctx(b=bar(), free=30_000.0))
        assert sum(out[o.idea_id].units * o.limit_price for o in opps) <= 30_000 + 1e-6

    def test_get_policy(self) -> None:
        a, b = Z.get_policy("conservative"), Z.get_policy("conservative")
        assert isinstance(a, Z.ConservativePolicy) and a is not b
        assert isinstance(Z.get_policy("chaser"), Z.ChaserPolicy)
        with pytest.raises(ValueError):
            Z.get_policy("yolo")


# --------------------------------------------------------------------------- bar estimate


def board(values: Sequence[float], at: float = NOW, pnl_only: bool = False) -> LeaderboardSnapshot:
    entries = []
    for i, v in enumerate(values):
        entries.append({"rank": i + 1, "username": f"u{i}", "pnl": v - W, "value": None if pnl_only else v})
    return LeaderboardSnapshot(at=at, entries=entries, initial_balance=W)


TOP = [285_000.0, 242_000.0, 221_500.0, 200_000.0, 190_000.0, 180_000.0, 170_000.0, 160_000.0, 150_000.0, 140_000.0]


class TestEstimateBar:
    def test_third_and_value_is_low(self) -> None:
        b = Z.estimate_bar(board(TOP), [], W, 30)
        assert (b.value, b.low, b.high, b.method, b.third_value, b.tenth_value) == (221_500, 221_500, 221_500, "third",
                                                                                   221_500, 140_000)
        assert b.growth_per_day is None and b.projected is False and b.hundredth_value is None
        assert b.explanation[0] == Z.MARKS_CAVEAT and "Sizing uses the lower end" in b.explanation[1]
        assert Z.estimate_bar(board(TOP, pnl_only=True), [], W, 30).value == 221_500  # value = initial + pnl

    def test_floor(self) -> None:
        low = Z.estimate_bar(board([150_000, 140_000, 130_000]), [], W, 30)
        assert (low.value, low.method, low.third_value) == (200_000, "floor", 130_000)
        assert any("below the floor" in line for line in low.explanation)
        none = Z.estimate_bar(None, [], W, 30)
        assert (none.value, none.method) == (200_000, "floor") and any("No leaderboard" in l for l in none.explanation)

    def test_linear_median_growth_from_three_days(self) -> None:
        then = board([v / 1.04 for v in TOP], at=NOW - 4 * DAY)
        newer = board([v / 1.50 for v in TOP], at=NOW - 1 * DAY)  # too recent to be the growth base
        b = Z.estimate_bar(board(TOP), [newer, then], W, 30)
        assert b.growth_per_day == pytest.approx(0.01, abs=1e-6)  # 4% over 4 days, linear
        assert b.high == pytest.approx(221_500 * 1.3) == pytest.approx(287_950)
        assert b.value == b.low == 221_500 and b.history_days == pytest.approx(4.0)
        assert ("Top-3 bar: 221,500 now; at the recent pace of +1.0% a day it could reach 287,950 by the Cup end. "
                "Sizing uses the lower end.") in b.explanation

    def test_growth_needs_three_days_and_is_clipped_and_capped(self) -> None:
        short = Z.estimate_bar(board(TOP), [board([v / 1.04 for v in TOP], at=NOW - 2 * DAY)], W, 30)
        assert short.growth_per_day is None and short.high == short.low
        fast = Z.estimate_bar(board(TOP), [board([v / 1.4 for v in TOP], at=NOW - 4 * DAY)], W, 30)
        assert fast.growth_per_day == pytest.approx(0.02)  # 10%/day clipped to 2%
        shrinking = Z.estimate_bar(board(TOP), [board([v * 1.2 for v in TOP], at=NOW - 4 * DAY)], W, 30)
        assert shrinking.growth_per_day == 0.0 and shrinking.high == shrinking.low
        big = [900_000.0, 850_000.0, 800_000.0] + TOP[3:]
        capped = Z.estimate_bar(board(big), [board([v / 1.4 for v in big], at=NOW - 4 * DAY)], W, 30)
        assert capped.high == pytest.approx(1_000_000.0)  # 10x the initial balance
        # one inflated mark does not move the median
        now_values = list(TOP)
        now_values[4] = 199_000.0  # one inflated mark (rank 5 grew 2.2%/day): the median ignores it
        robust = Z.estimate_bar(board(now_values), [board([v / 1.04 for v in TOP], at=NOW - 4 * DAY)], W, 30)
        assert robust.growth_per_day == pytest.approx(0.01, abs=1e-6)


# --------------------------------------------------------------------------- strategy fixer regressions


def _plain_swing(opp: Opportunity, c: SizingContext) -> int:
    """What the chaser buys of ``opp`` in swing mode without a bold bet (chase rules, one idea)."""
    return Z._size_one(opp, c, Z._chaser_rules("swing"), Z._State.of(c), "chaser", n_same=1).units


class TestStrategyFixerRegressions:
    def test_strategy_3_bold_bet_only_on_a_binary_held_to_settlement(self) -> None:
        pol = Z.ChaserPolicy()
        c = ctx(b=bar(), days_left=1.0)
        # a fade (a target/stop bracket) settling before the Cup end: its "win" is the target, +0.065 a share, never
        # the bar, so it is not a bold candidate and is sized as in chase mode
        fade = value_idea(entry=0.28, ev=0.015, sigma=0.0, idea_id="fade:x2:no:s7", kind="fade", settles=True,
                          limit=0.285, side="no")
        fade.bet = BetShape("bracket", p=0.7, gain=0.065, loss=0.135, cost=0.28)
        d = pol.size_all([fade], c)[fade.idea_id]
        assert d.bold is False and d.units == _plain_swing(fade, c) and d.units < 60_000  # the bold stake was 168,750
        assert any(line.startswith("No bold bet: the ideas that settle in time exit before settlement") for line in d.lines)
        assert not any(line.startswith("Bold bet") for line in d.lines)
        # a value idea is a binary contract: bold, and the bold position must be held to its 1/0 result
        val = value_idea(entry=0.25, ev=0.08, sigma=0.0, idea_id="value:x1:yes", settles=True, limit=0.25)
        val.exit_plan = ExitPlan(kind="value", target_bid=0.32, dynamic_fv_target=True, exit_buffer=0.005,
                                 note="Sell when the YES bid reaches 0.320")
        b = pol.size_all([val], c)[val.idea_id]
        assert b.bold is True and Z.BOLD_HOLD_LINE in b.lines and "held to settlement" in b.lines[0]
        plan = Z.bold_exit_plan(val)
        assert (plan.kind, plan.hold_to_resolution, plan.target_bid, plan.stop_bid, plan.dynamic_fv_target,
                plan.time_stop_ts, plan.time_stop_after_fill_s, plan.exit_before_ts) == ("settle", True, None, None, False,
                                                                                         None, None, None)
        assert plan.note.startswith(Z.BOLD_HOLD_NOTE)
        assert val.exit_plan.target_bid == 0.32  # the shared idea is not changed
        # only the settlement regime's exit survives (flat before a VWAP closeout window)
        val.exit_plan.exit_before_ts = NOW + 5 * H
        assert Z.bold_exit_plan(val).exit_before_ts == NOW + 5 * H

    def test_strategy_4_no_bold_bet_unless_the_fundable_stake_reaches_the_bar(self) -> None:
        pol = Z.ChaserPolicy()
        val = value_idea(entry=0.25, ev=0.08, sigma=0.0, idea_id="value:x1:yes", settles=True, limit=0.285)
        # depth unknown: priced at the 0.285 limit, so the win reaches the bar even if every share fills there
        full = pol.size_all([val], ctx(b=bar(), days_left=1.0))[val.idea_id]
        assert full.bold is True and full.avg_cost == 0.285 and full.units == math.floor(121_500 / 0.715)
        assert W + full.units * (1 - 0.285) >= 221_500 - 1 and full.stake <= 0.60 * W
        assert any("priced at the 0.285 limit" in line for line in full.lines)
        assert "Bold bet: if it wins, your value reaches the 221,500 bar; it loses 48,430 otherwise" in full.lines
        # 20,000 free cash: no stake it can fund reaches the bar -> no bold bet, plain chase sizing, and why
        c20 = ctx(b=bar(), days_left=1.0, free=20_000.0)
        short = pol.size_all([val], c20)[val.idea_id]
        assert short.bold is False and short.units == _plain_swing(val, c20)
        assert ("No bold bet: the free cash (20,000 SUSQies, reserved at the 0.285 limit) cannot fund a stake that reaches "
                "the bar (the best candidate would win only to 150,175, below the 221,500 bar), so a swing would waste "
                "the variance; every idea is sized as in chase mode") in short.lines
        assert not any("below the 221,500 bar); it loses" in line for line in short.lines)  # the old shortfall bet
        # the 95% total cap: 60,000 already committed leaves 35,000, less than the 48,430 the goal needs
        capped = pol.size_all([val], ctx(b=bar(), days_left=1.0, exposure=ExposureSummary(gross_cost=60_000.0)))
        assert capped[val.idea_id].bold is False
        assert any("the 95% total cap leaves room for only 35,000 SUSQies" in line for line in capped[val.idea_id].lines)
        # the book holds only 50,000 shares up to the limit
        thin = value_idea(entry=0.25, ev=0.08, sigma=0.0, idea_id="value:x1:yes", settles=True, limit=0.285,
                          levels=[(0.25, 30_000.0), (0.27, 20_000.0)])
        d = pol.size_all([thin], ctx(b=bar(), days_left=1.0))[thin.idea_id]
        assert d.bold is False and any("the book offers only 50,000 shares up to the 0.285 limit" in line for line in d.lines)

    def test_strategy_4_bold_bet_is_funded_before_better_scored_ideas(self) -> None:
        pol = Z.ChaserPolicy()
        best = [value_idea(entry=0.5, ev=0.08, sigma=0.0, idea_id=f"value:x{i}:yes", race_key=f"r{i}", score=2.0 - i / 10,
                           settles=False, called=0.4, limit=0.5) for i in range(1, 4)]
        bold = value_idea(entry=0.25, ev=0.08, sigma=0.0, idea_id="value:x9:yes", race_key="r9", score=0.5, settles=True,
                          limit=0.25)
        ideas = best + [bold]
        out = pol.size_all(ideas, ctx(b=bar(), days_left=1.0, free=60_000.0))
        d = out[bold.idea_id]
        assert d.bold is True and W + d.units * 0.75 >= 221_500 - 1  # not cut by the better-scored ideas' cash
        assert sum(x.units * (o.limit_price or 0.0) for o, x in zip(ideas, (out[o.idea_id] for o in ideas))) <= 60_000 + 1e-6
        assert sum(1 for x in out.values() if x.bold) == 1
        assert list(out) == [o.idea_id for o in sorted(ideas, key=lambda o: -o.score)]  # still best score first

    def test_strategy_7_swing_cap_never_flips_an_over_cap_tilt_to_the_same_size(self) -> None:
        cap = 0.10 * W / 3.0  # 3,333: a 3-point swing may cost at most 10% of equity
        # the helper: against an over-cap tilt of -9,000, a D position may go through zero and up to +cap, not +9,000
        u = Z._swing_units(-9_000.0, 0.057, cap)
        assert -9_000.0 + u * 0.057 == pytest.approx(cap)
        assert Z._swing_units(-9_000.0, -0.057, cap) == 0.0  # adding to the over-cap side: nothing
        assert -2_000.0 + Z._swing_units(-2_000.0, 0.057, cap) * 0.057 == pytest.approx(cap)  # under the cap: unchanged
        assert Z._swing_units(2_000.0, 0.057, cap) * 0.057 == pytest.approx(cap - 2_000.0)
        # through the policy: a cheap contract far below fair value (ask 0.02, fair 0.32) on a -9,000 tilt used to buy
        # 352,340 shares and move the tilt to +9,000 (a 3-point swing toward R costing 27% of equity, cap 10%); the
        # chaser without a bar (unknown_bar mode) has a 30% swing cap: a 10,000 tilt
        cases = [(Z.ConservativePolicy(), cap, -9_000.0), (Z.ConservativePolicy(), cap, -3_400.0),
                 (Z.ChaserPolicy(), 0.30 * W / 3.0, -27_000.0)]
        for pol, limit, tilt in cases:
            idea = value_idea(entry=0.02, ev=0.30, sigma=0.0, limit=0.02, delta=0.057, idea_id="value:x7:yes",
                              race_key="2026:SENATE:PA", levels=[(0.02, 10_000_000.0)])
            d = pol.size_all([idea], ctx(exposure=ExposureSummary(national_tilt_d=tilt)))[idea.idea_id]
            after = tilt + d.units * 0.057
            assert d.units > 0 and after > 0 and after <= limit + 0.06, (pol.name, tilt, d.units, after)
            assert ("tilt_cap" if pol.name == "conservative" else "swing_cap") in d.capped_by
            swing_line = next(line for line in d.lines if line.startswith("A 3-point national swing"))
            loss = float(swing_line.split("cost this portfolio ")[1].split(" SUSQies")[0].replace(",", ""))
            assert loss <= 3 * limit + 1  # the line's own number is within the cap it states
