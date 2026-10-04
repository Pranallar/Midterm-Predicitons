"""Tests for supermarket_bot/strategy.py (sizing, risk mode, ideas, report, fade backtest)."""

from __future__ import annotations

import copy
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest

from supermarket_bot import strategy as S
from supermarket_bot.analytics import SURGE_WINDOWS, _Series, detect_surges, high_band, update_surge_status, window_tolerance
from supermarket_bot.models import (
    SURGE_OPEN,
    SURGE_REVERTED,
    Attribution,
    BacktestResult,
    ExchangeInfo,
    HighBand,
    Opportunity,
    PricePoint,
    StrategyReport,
    Surge,
)
from supermarket_bot.strategy import (
    arbitrage_opportunities,
    backtest_fade,
    build_report,
    carry_opportunity,
    cup_end_ts,
    fade_opportunity,
    kelly_fraction,
    risk_mode,
    size_position,
    watch_opportunity,
)

H = 3600.0
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()
CUP_END = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc).timestamp()
BAL = 100_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def att(verdict: str = "participants", odds: float = 0.66, conf: float = 0.64, depth: Optional[float] = None,
        reasons: Sequence[str] = ("2 trades made 90% of the volume",)) -> Attribution:
    return Attribution(verdict=verdict, confidence=conf, reversion_odds=odds, summary="", reasons=list(reasons),
                       book_depth=depth)


def mk_surge(start: float = 0.51, peak: float = 0.69, direction: str = "up", verdict: Optional[str] = "participants",
             odds: float = 0.66, conf: float = 0.64, depth: Optional[float] = None, eid: str = "e1",
             detected_at: float = NOW - 600, status: str = SURGE_OPEN, sid: Optional[int] = 7) -> Surge:
    return Surge(exchange_id=eid, market_id="m1", window="1h", window_s=3600, start_ts=detected_at - 1800,
                 end_ts=detected_at, start_price=start, end_price=peak, change=round(peak - start, 6),
                 direction=direction, peak_price=peak, detected_at=detected_at, status=status, current_price=peak,
                 attribution=att(verdict, odds, conf, depth) if verdict else None, id=sid)


def info(eid: str = "e1", title: str = "Will Republicans win Ohio House District 9?", option: Optional[str] = None,
         settle: Optional[float] = NOW + 30 * 86400) -> ExchangeInfo:
    return ExchangeInfo(exchange_id=eid, market_id="m1", option=option, market_title=title,
                        settlement_date=iso(settle) if settle is not None else None)


def quote(bid: Optional[float], ask: Optional[float], price: Optional[float] = None) -> PricePoint:
    if price is None and bid is not None and ask is not None:
        price = round((bid + ask) / 2, 6)
    return PricePoint(ts=NOW, price=price, bid=bid, ask=ask)


def band(side: str = "YES", fav: float = 0.9725, stable: bool = True, eid: str = "e2", settle: Optional[float] = NOW + 40 * H,
         tib: float = 1.0) -> HighBand:
    return HighBand(exchange_id=eid, market_id="m2", side=side, favorite_price=fav, time_in_band=tib, mean=fav,
                    low=fav - 0.005, high=fav + 0.005, lookback_s=6 * H, stable=stable,
                    settlement_date=iso(settle) if settle is not None else None,
                    hours_to_settlement=(settle - NOW) / H if settle is not None else None)


# --------------------------------------------------------------------------- cup end


class TestCupEnd:
    def test_tournament_end_date(self) -> None:
        assert cup_end_ts({"endDate": "2026-11-04T00:00:00.000Z"}) == datetime(2026, 11, 4, tzinfo=timezone.utc).timestamp()

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERMARKET_CUP_END", "2026-11-05T12:00:00Z")
        expected = datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp()
        assert cup_end_ts({"endDate": None}) == expected
        assert cup_end_ts(None) == expected

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPERMARKET_CUP_END", raising=False)
        assert cup_end_ts() == CUP_END  # noon ET on Nov 4, 2026 (EST)

    def test_bad_env_warns_and_uses_default(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        monkeypatch.setenv("SUPERMARKET_CUP_END", "next tuesday")
        with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
            assert cup_end_ts({"endDate": "garbage"}) == CUP_END
        assert "SUPERMARKET_CUP_END" in caplog.text


# --------------------------------------------------------------------------- sizing


class TestKelly:
    def test_formula(self) -> None:
        assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
        assert kelly_fraction(0.978, 0.975) == pytest.approx(0.12)
        assert kelly_fraction(0.5, 0.0) == pytest.approx(0.5)

    @pytest.mark.parametrize("p,c", [(0.5, 0.5), (0.4, 0.5), (0.0, 0.1), (0.9, 1.0), (0.9, 1.2), (0.9, -0.1)])
    def test_no_edge_or_bad_price_is_zero(self, p: float, c: float) -> None:
        assert kelly_fraction(p, c) == 0.0

    def test_float_tie_is_zero(self) -> None:
        assert kelly_fraction(0.1 + 0.2, 0.3) == 0.0  # 0.30000000000000004 vs 0.3

    def test_clamps_and_none(self) -> None:
        assert kelly_fraction(1.5, 0.5) == 1.0
        assert kelly_fraction(None, 0.5) == 0.0  # type: ignore[arg-type]
        assert kelly_fraction(0.5, None) == 0.0  # type: ignore[arg-type]


class TestSizePosition:
    def test_kelly_limited(self) -> None:
        # f = 0.1, quarter-Kelly stake 2,500 at 0.50 -> 5,000 shares
        assert size_position(0.55, 0.5, BAL) == 5000

    def test_pct_cap(self) -> None:
        # f = 0.8 -> 20,000 stake, capped at 8% = 8,000 -> 16,000 shares
        assert size_position(0.9, 0.5, BAL) == 16000
        assert size_position(0.9, 0.5, BAL, max_position_pct=0.02) == 4000

    def test_depth_cap(self) -> None:
        assert size_position(0.9, 0.5, BAL, depth_limit=1200) == 1200
        assert size_position(0.9, 0.5, BAL, depth_limit=0) == 0
        assert size_position(0.9, 0.5, BAL, depth_limit=10**9) == 16000

    def test_kelly_multiplier(self) -> None:
        assert size_position(0.55, 0.5, BAL, kelly_mult=0.5) == 10000

    def test_zero_cases(self) -> None:
        assert size_position(0.5, 0.5, BAL) == 0
        assert size_position(0.9, 0.5, 0) == 0
        assert size_position(0.9, 0.5, None) == 0  # type: ignore[arg-type]
        assert size_position(0.9, 0.0, BAL) == 0

    def test_floor_absorbs_float_error(self) -> None:
        # 70 / 0.07 == 999.9999999999999 in floats
        assert size_position(0.99, 0.07, 1000, max_position_pct=0.07) == 1000

    @pytest.mark.parametrize("p,c", [(0.6, 0.5), (0.99, 0.31), (0.978, 0.975), (0.3, 0.05), (0.55, 0.52)])
    def test_bracket_sizing_matches_binary_kelly(self, p: float, c: float) -> None:
        # a contract held to settlement is a bracket with gain 1 - c and loss c
        assert S._bracket_shares(p, 1 - c, c, c, BAL, 0.25, 0.08, None) == size_position(p, c, BAL)


class TestRiskMode:
    @pytest.mark.parametrize("balance,leader,rank,days,mode", [
        (100_000, 101_000, 2, 5, "protect"),
        (50_000, 100_000, 3, 7, "protect"),  # protect wins over being behind
        (100_000, 101_000, 4, 5, "aggressive"),  # late and outside the top 3 ... and top 10? rank 4 -> balanced? see below
        (100_000, 101_000, 2, 8, "balanced"),
        (80_000, 100_000, 5, 30, "aggressive"),  # more than 10% behind the leader
        (95_000, 100_000, 5, 30, "balanced"),
        (90_000, 100_000, 5, 30, "balanced"),  # exactly 90% is not "behind"
        (80_000, None, 5, 30, "balanced"),
        (80_000, 0, 5, 30, "balanced"),
        (None, 100_000, 5, 30, "balanced"),
        (100_000, 100_000, 11, 10, "aggressive"),
        (100_000, 100_000, 10, 10, "balanced"),
        (100_000, 100_000, None, 10, "aggressive"),
        (100_000, 100_000, 50, 11, "balanced"),
        (100_000, None, None, None, "balanced"),
    ])
    def test_modes(self, balance: Any, leader: Any, rank: Any, days: Any, mode: str) -> None:
        if (rank, days) == (4, 5):
            mode = "balanced"  # rank 4 is outside the top 3 but inside the top 10, and not behind
        assert risk_mode(balance, 100_000, leader, rank, days) == mode


# --------------------------------------------------------------------------- fade


class TestFade:
    def test_up_move_buys_no_at_one_minus_bid(self) -> None:
        o = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(), NOW, BAL)
        assert o is not None
        assert (o.kind, o.side, o.exchange_id, o.market_id, o.surge_id) == ("fade", "no", "e1", "m1", 7)
        assert (o.entry_price, o.target_price, o.stop_price) == (0.31, 0.40, 0.22)
        p, gain, loss = 0.66, 0.09, 0.09
        assert o.prob_win == pytest.approx(p)
        assert o.edge == pytest.approx(p * gain - (1 - p) * loss)
        assert o.expected_return == pytest.approx(o.edge / 0.31, abs=1e-6)
        assert o.score == pytest.approx(o.expected_return * 0.64, abs=1e-6)
        assert o.confidence == 0.64 and o.horizon_hours == 6.0
        # bracket Kelly risks 8,000 (way above the cap) -> 8% cap: floor(8000 / 0.31)
        assert o.suggested_shares == math.floor(8000 / 0.31)
        assert o.suggested_cost == pytest.approx(o.suggested_shares * 0.31, abs=0.01)
        text = " | ".join(o.rationale)
        assert "Buy NO at 0.31 (1 - YES bid 0.69): if YES gives back half its 0.18 jump to 0.60, NO is worth 0.40 (+0.09/share)" in text
        assert "0.78" in text and "0.22" in text  # stop levels
        assert "Win chance 66%" in text and "Evidence: 2 trades made 90% of the volume" in text
        assert o.risks and all(isinstance(r, str) and r for r in o.risks)
        assert o.title == "Will Republicans win Ohio House District 9?"

    def test_down_move_buys_yes_at_ask(self) -> None:
        o = fade_opportunity(mk_surge(start=0.60, peak=0.40, direction="down"), quote(0.39, 0.40), info(), NOW, BAL)
        assert o is not None and o.side == "yes"
        assert (o.entry_price, o.target_price, o.stop_price) == (0.40, 0.50, 0.30)
        assert "Buy YES at 0.40 (the YES ask)" in o.rationale[1]

    def test_without_book_uses_mark(self) -> None:
        o = fade_opportunity(mk_surge(), None, None, NOW, BAL)
        assert o is not None and o.entry_price == 0.31  # 1 - surge.current_price
        assert o.title == "Market m1" and o.option is None
        assert any("No live book" in r for r in o.risks)
        mark_only = fade_opportunity(mk_surge(), PricePoint(ts=NOW, price=0.68), None, NOW, BAL)
        assert mark_only is not None and mark_only.entry_price == 0.32

    def test_backtest_blend_needs_ten_surges(self) -> None:
        ten = BacktestResult(n_surges=10, n_reverted=4, reversion_rate=0.4)
        o = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(), NOW, BAL, ten)
        assert o is not None and o.prob_win == pytest.approx(0.5 * 0.66 + 0.5 * 0.4)
        assert any("blended 50/50" in r and "10 surges" in r for r in o.rationale)
        nine = BacktestResult(n_surges=9, n_reverted=4, reversion_rate=0.44)
        o = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(), NOW, BAL, nine)
        assert o is not None and o.prob_win == pytest.approx(0.66)

    def test_negative_edge_suppressed(self) -> None:
        assert fade_opportunity(mk_surge(odds=0.3), quote(0.69, 0.70), info(), NOW, BAL) is None
        assert fade_opportunity(mk_surge(odds=0.5), quote(0.69, 0.70), info(), NOW, BAL) is None  # zero edge
        low = BacktestResult(n_surges=20, n_reverted=2, reversion_rate=0.1)
        assert fade_opportunity(mk_surge(odds=0.6), quote(0.69, 0.70), info(), NOW, BAL, low) is None

    @pytest.mark.parametrize("verdict", ["news", "unclear", None])
    def test_only_participant_surges(self, verdict: Optional[str]) -> None:
        assert fade_opportunity(mk_surge(verdict=verdict), quote(0.69, 0.70), info(), NOW, BAL) is None

    def test_only_open_surges(self) -> None:
        assert fade_opportunity(mk_surge(status=SURGE_REVERTED), quote(0.69, 0.70), info(), NOW, BAL) is None

    def test_past_target_or_through_stop(self) -> None:
        assert fade_opportunity(mk_surge(), quote(0.59, 0.60), info(), NOW, BAL) is None  # NO at 0.41 > target 0.40
        assert fade_opportunity(mk_surge(), quote(0.79, 0.80), info(), NOW, BAL) is None  # beyond the 0.78 stop

    def test_stop_clamped_at_certainty(self) -> None:
        o = fade_opportunity(mk_surge(start=0.70, peak=0.90), quote(0.89, 0.91), info(), NOW, BAL)
        assert o is not None and o.stop_price == 0.0  # YES stop 1.00 -> NO worthless; loss = entry

    def test_depth_caps_size(self) -> None:
        o = fade_opportunity(mk_surge(depth=300), quote(0.69, 0.70), info(), NOW, BAL)
        assert o is not None and o.suggested_shares == 300
        assert any("Thin book" in r for r in o.risks)
        assert "and by book depth" in o.rationale[4]

    def test_settles_before_cup_end(self) -> None:
        before = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(settle=CUP_END - H), NOW, BAL, cup_end=CUP_END)
        after = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(settle=CUP_END + H), NOW, BAL, cup_end=CUP_END)
        unknown = fade_opportunity(mk_surge(), quote(0.69, 0.70), info(settle=None), NOW, BAL, cup_end=CUP_END)
        assert (before.settles_before_cup_end, after.settles_before_cup_end, unknown.settles_before_cup_end) == (True, False, None)  # type: ignore[union-attr]


# --------------------------------------------------------------------------- carry


class TestCarry:
    def test_yes_favourite(self) -> None:
        o = carry_opportunity(band(), quote(0.97, 0.975), info("e2", "Will Democrats win the Maryland Senate race?"),
                              NOW, CUP_END, BAL)
        assert o is not None
        assert (o.kind, o.side, o.entry_price, o.target_price) == ("carry", "yes", 0.975, 1.0)
        fav_mid = 0.9725
        p_true = fav_mid + min(0.01, 0.2 * (1 - fav_mid))
        assert o.prob_win == pytest.approx(p_true)
        assert o.edge == pytest.approx(p_true * (1 - 0.975) - (1 - p_true) * 0.975)
        assert o.suggested_shares == size_position(p_true, 0.975, BAL) == math.floor(0.25 * 0.12 * BAL / 0.975)
        assert o.settles_before_cup_end is True and o.horizon_hours == pytest.approx(40.0)
        assert o.score == pytest.approx(o.expected_return * o.confidence, abs=1e-6)
        text = " | ".join(o.rationale)
        assert "favourite-longshot adjustment" in text and "before the Cup ends" in text

    def test_no_favourite_buys_no_at_one_minus_bid(self) -> None:
        o = carry_opportunity(band(side="NO"), quote(0.025, 0.03), info("e2"), NOW, CUP_END, BAL)
        assert o is not None and o.side == "no" and o.entry_price == 0.975
        assert o.prob_win == pytest.approx(0.9725 + 0.0055)
        assert "Buy NO at 0.975 (1 - YES bid 0.025)" in o.rationale[1]

    def test_requires_stable_band(self) -> None:
        assert carry_opportunity(band(stable=False), quote(0.97, 0.975), info("e2"), NOW, CUP_END, BAL) is None

    def test_no_room_at_995(self) -> None:
        assert carry_opportunity(band(fav=0.995), quote(0.99, 0.995), info("e2"), NOW, CUP_END, BAL) is None

    def test_negative_edge_suppressed(self) -> None:
        # mid 0.975 -> p_true 0.98 < ask 0.99
        assert carry_opportunity(band(), quote(0.96, 0.99), info("e2"), NOW, CUP_END, BAL) is None

    def test_settles_after_cup_end_halves_score(self) -> None:
        early = carry_opportunity(band(settle=CUP_END - H), quote(0.97, 0.975), None, NOW, CUP_END, BAL)
        late = carry_opportunity(band(settle=CUP_END + 30 * 86400), quote(0.97, 0.975), None, NOW, CUP_END, BAL)
        assert early is not None and late is not None
        assert late.settles_before_cup_end is False
        assert late.score == pytest.approx(early.score / 2, abs=1e-6)
        assert any("valued at market price at cup end, not paid out" in r.lower() for r in late.risks)
        assert not any("not paid out" in r for r in early.risks)
        assert late.horizon_hours == pytest.approx((CUP_END - NOW) / H, abs=0.01)

    def test_unknown_settlement(self) -> None:
        o = carry_opportunity(band(settle=None), quote(0.97, 0.975), None, NOW, CUP_END, BAL)
        assert o is not None and o.settles_before_cup_end is None and o.horizon_hours is None
        assert any("Settlement date unknown" in r for r in o.risks)

    def test_settlement_from_info_when_band_has_none(self) -> None:
        o = carry_opportunity(band(settle=None), quote(0.97, 0.975), info("e2", settle=CUP_END + H), NOW, CUP_END, BAL)
        assert o is not None and o.settles_before_cup_end is False

    def test_without_book_uses_favourite_mark(self) -> None:
        o = carry_opportunity(band(fav=0.97), None, None, NOW, CUP_END, BAL)
        assert o is not None and o.entry_price == 0.97
        assert o.edge == pytest.approx(0.006)  # the adjustment alone: min(0.01, 0.2 * 0.03)
        assert any("No live book" in r for r in o.risks)


# --------------------------------------------------------------------------- watch / arbitrage


class TestWatch:
    def test_unclear_open_surge(self) -> None:
        o = watch_opportunity(mk_surge(verdict="unclear", odds=0.4, conf=0.35), info())
        assert o is not None
        assert (o.kind, o.side, o.suggested_shares, o.suggested_cost, o.score, o.edge) == ("watch", "no", 0, 0.0, 0.0, None)
        assert o.entry_price == 0.31 and o.prob_win == 0.4 and o.confidence == 0.35
        assert any("Wait for confirmation" in r for r in o.rationale)

    def test_down_watch_side(self) -> None:
        o = watch_opportunity(mk_surge(start=0.6, peak=0.4, direction="down", verdict="unclear"), None)
        assert o is not None and o.side == "yes" and o.entry_price == 0.40

    @pytest.mark.parametrize("verdict", ["participants", "news", None])
    def test_other_verdicts(self, verdict: Optional[str]) -> None:
        assert watch_opportunity(mk_surge(verdict=verdict), info()) is None

    def test_closed_surge(self) -> None:
        assert watch_opportunity(mk_surge(verdict="unclear", status=SURGE_REVERTED), info()) is None


def violation(amount: float = 0.05) -> Dict[str, Any]:
    return {
        "relationshipId": "r1", "type": "monotonic", "violationAmount": amount,
        "reason": "Above 160K cannot be likelier than above 150K",
        "suggestedCorrectiveTrades": [
            {"exchangeId": "11", "outcomeSide": "Yes", "action": "Buy", "rationale": "underpriced", "marketId": "5",
             "marketTitle": "BTC above 150K", "outcome": None, "currentPrice": 0.50},
            {"exchangeId": "12", "outcomeSide": "Yes", "action": "Sell", "rationale": "overpriced", "marketId": "6",
             "marketTitle": "BTC above 160K", "outcome": None, "currentPrice": 0.55},
        ],
        "observedPrices": [], "evaluationStatus": "violated", "constraint": {"priceRule": "P(160K) <= P(150K)"},
    }


class TestArbitrage:
    def test_constraint_violation(self) -> None:
        [o] = arbitrage_opportunities({"data": [violation()], "violationsCount": 1}, [], BAL)
        # a two-leg set: no single exchange/option describes it (the legs do)
        assert (o.kind, o.exchange_id, o.option, o.market_id, o.side, o.unit) == ("arbitrage", None, None, "5", "yes", "sets")
        assert [(leg["exchange_id"], leg["side"], leg["price"]) for leg in o.legs] == [("11", "yes", 0.50), ("12", "no", 0.45)]
        assert o.entry_price == 0.95  # buy YES 0.50 + buy NO (sell YES at 0.55) 0.45
        assert o.edge == 0.05 and o.expected_return == pytest.approx(0.05 / 0.95, abs=1e-6)  # rounded to 6 dp
        assert o.suggested_shares == math.floor(8000 / 0.95)
        assert o.score == pytest.approx(o.expected_return * S.CONSTRAINT_CONFIDENCE, abs=1e-6)
        text = " | ".join(o.rationale)
        assert "monotonic violation of 0.05" in text and "Rule: P(160K) <= P(150K)" in text
        assert "buy NO at 0.45" in text and "overpriced" in text

    def test_satisfied_constraints_and_scan_shape(self) -> None:
        assert arbitrage_opportunities({"data": [violation(0.0)]}, [], BAL) == []
        assert len(arbitrage_opportunities({"violations": [violation()]}, [], BAL)) == 1
        assert arbitrage_opportunities(None, [], BAL) == []

    def test_book_flags(self) -> None:
        rows = [
            {"market_id": "9", "market_title": "Who will win the Arizona Governor race?", "outcomes": 3, "overround": 0.97, "arbitrage": True},
            {"market_id": "10", "market_title": "Fair book", "outcomes": 2, "overround": 1.02, "arbitrage": False},
            {"marketId": "11", "title": "API shape", "overround": None, "hasArbitrageOpportunity": True},
        ]
        a, b = arbitrage_opportunities(None, rows, BAL)
        assert (a.exchange_id, a.market_id, a.entry_price, a.edge, a.target_price) == (None, "9", 0.97, 0.03, 1.0)
        assert a.suggested_shares == math.floor(8000 / 0.97)
        assert "sum to 0.97" in a.rationale[0] and "3 outcomes" in a.rationale[0]
        assert (b.market_id, b.edge, b.score, b.suggested_shares) == ("11", None, 0.0, 0)


# --------------------------------------------------------------------------- report


def scenario(**overrides: Any) -> Dict[str, Any]:
    surges = [
        mk_surge(eid="e1"),  # participants -> fade
        mk_surge(eid="e3", verdict="unclear", odds=0.4, conf=0.35),  # -> watch
        mk_surge(eid="e4", verdict="news", odds=0.2, conf=0.8),  # nothing
        mk_surge(eid="e5", status=SURGE_REVERTED),  # nothing
    ]
    latest = {"e1": quote(0.69, 0.70), "e2": quote(0.97, 0.975), "e3": quote(0.68, 0.70)}
    infos = {e: info(e) for e in ("e1", "e3", "e4", "e5")}
    infos["e2"] = info("e2", "Will Democrats win the Maryland Senate race?")
    args: Dict[str, Any] = dict(
        now=NOW, surges=surges, bands=[band(), band(eid="e6", stable=False)], latest=latest, infos=infos,
        balance=BAL, initial_balance=BAL, leader_value=105_000, my_rank=8, cup_end=CUP_END,
        constraints={"data": [violation()]},
        overround_rows=[{"market_id": "9", "market_title": "AZ Governor", "overround": 0.97, "arbitrage": True}],
    )
    args.update(overrides)
    return args


class TestBuildReport:
    def test_contents_and_ordering(self) -> None:
        r = build_report(**scenario())
        assert isinstance(r, StrategyReport) and r.risk_mode == "balanced"
        kinds = [o.kind for o in r.opportunities]
        assert sorted(kinds) == ["arbitrage", "arbitrage", "carry", "fade", "watch"]
        scores = [o.score for o in r.opportunities]
        assert scores == sorted(scores, reverse=True)
        assert kinds[-1] == "watch"
        assert r.days_left == pytest.approx((CUP_END - NOW) / 86400, abs=0.01)
        assert (r.balance, r.initial_balance, r.leader_value, r.my_rank, r.cup_end) == (BAL, BAL, 105_000, 8, CUP_END)
        assert "Balanced mode" in r.headline and "32 days left" in r.headline and "Top idea" in r.headline

    def test_json_serializable(self) -> None:
        r = build_report(**scenario(backtest=BacktestResult(n_surges=12, n_reverted=7, reversion_rate=0.5833)))
        data = json.loads(json.dumps(r.to_dict(), allow_nan=False))
        assert data["risk_mode"] == "balanced" and data["backtest"]["n_surges"] == 12
        assert all(isinstance(o["rationale"], list) for o in data["opportunities"])

    def test_principles(self) -> None:
        r = build_report(**scenario())
        assert 4 <= len(r.principles) <= 6
        text = " ".join(r.principles)
        for needle in ("top 3", "convex", "variance is your friend", "compound slowly", "participant-driven",
                       "settlement dates", "Diversify", "uncorrelated"):
            assert needle in text

    def test_aggressive_multipliers(self) -> None:
        base = {o.kind: o.score for o in build_report(**scenario()).opportunities if o.kind != "arbitrage"}
        r = build_report(**scenario(leader_value=200_000))
        assert r.risk_mode == "aggressive"
        got = {o.kind: o.score for o in r.opportunities if o.kind != "arbitrage"}
        assert got["fade"] == pytest.approx(base["fade"] * 1.3, abs=1e-6)
        assert got["carry"] == pytest.approx(base["carry"] * 0.7, abs=1e-6)
        assert "Mode: aggressive" in r.principles[-1] and "behind the leader" in r.principles[-1]

    def test_protect_multipliers(self) -> None:
        base = {o.kind: o.score for o in build_report(**scenario()).opportunities}
        r = build_report(**scenario(my_rank=1, leader_value=None, now=CUP_END - 5 * 86400))
        assert r.risk_mode == "protect"
        got = {o.kind: o.score for o in r.opportunities}
        assert got["fade"] == pytest.approx(base["fade"] * 0.6, abs=1e-6)
        assert got["arbitrage"] == pytest.approx(base["arbitrage"], abs=1e-6)
        assert "Mode: protect" in r.principles[-1]

    def test_top_50(self) -> None:
        bands = [band(eid=f"b{i}", fav=0.9725) for i in range(60)]
        latest = {f"b{i}": quote(0.97, 0.975) for i in range(60)}
        r = build_report(**scenario(bands=bands, latest=latest, surges=[], constraints=None, overround_rows=()))
        assert len(r.opportunities) == 50
        assert [o.exchange_id for o in r.opportunities] == sorted(o.exchange_id for o in r.opportunities)  # stable ties

    def test_newest_open_surge_per_exchange(self) -> None:
        old = mk_surge(eid="e1", detected_at=NOW - 3 * H, sid=1)
        new = mk_surge(eid="e1", start=0.6, peak=0.4, direction="down", detected_at=NOW - 600, sid=2)
        r = build_report(**scenario(surges=[old, new], latest={"e1": quote(0.39, 0.40)}, bands=[], constraints=None,
                                    overround_rows=()))
        [o] = r.opportunities
        assert (o.surge_id, o.side) == (2, "yes")

    def test_missing_balance_uses_initial_then_default(self) -> None:
        r = build_report(**scenario(balance=None, initial_balance=None))
        assert r.balance is None and r.initial_balance == S.DEFAULT_INITIAL_BALANCE
        fade = next(o for o in r.opportunities if o.kind == "fade")
        assert fade.suggested_shares == math.floor(8000 / 0.31)

    def test_empty(self) -> None:
        r = build_report(now=NOW, surges=[], bands=[], latest={}, infos={}, balance=None, initial_balance=None,
                         leader_value=None, my_rank=None, cup_end=CUP_END)
        assert r.opportunities == [] and "0 trade ideas" in r.headline
        json.dumps(r.to_dict(), allow_nan=False)


# --------------------------------------------------------------------------- backtest


T0 = 1_789_999_800.0  # a multiple of 300 s


def scripted_series(step: float = 60.0) -> Tuple[List[PricePoint], float, float]:
    """3 days of calm noise; spike A (+0.18) reverts 80% over 3 h, spike B (-0.15) holds."""
    a_at, b_at = T0 + 36 * H + 30, T0 + 60 * H + 30

    def f(t: float) -> float:
        p = 0.50 + (0.005 if int(t // 300) % 2 else 0.0)
        if t >= a_at:
            back = min(1.0, max(0.0, (t - a_at - H) / (3 * H)))  # holds 1 h, then gives back 80% over 3 h
            p += 0.18 - 0.8 * 0.18 * back
        if t >= b_at:
            p -= 0.15
        return round(p, 6)

    pts = [PricePoint(ts=T0 + i * step, price=f(T0 + i * step)) for i in range(int(72 * H / step) + 1)]
    return pts, a_at, b_at


def reference_backtest(series_by: Dict[str, List[PricePoint]], horizon_s: float, step_s: float) -> List[Tuple[str, str, float, bool]]:
    """Plain re-implementation of the DESIGN walk with detect_surges at every step."""
    events = []
    for eid in sorted(series_by):
        pts = series_by[eid]
        ser = _Series(pts)
        t = math.ceil((ser.ts[0] - 1e-6) / 300) * 300
        active: Dict[str, Surge] = {}
        while t <= ser.ts[-1] + 1e-6:
            i = ser.idx_at(t)
            for d in list(active):
                if update_surge_status(active[d], ser.prices[i], t).status != SURGE_OPEN:  # type: ignore[index]
                    del active[d]
            found = detect_surges(pts, t, eid, "")
            if found and found[0].direction not in active and found[0].status == SURGE_OPEN:
                s = found[0]
                active[s.direction] = s
                j = ser.value_idx(t + horizon_s, window_tolerance(horizon_s))
                if t + horizon_s <= ser.ts[-1] + 1e-6 and j is not None:
                    p_h = ser.prices[j]
                    fade = s.end_price - p_h if s.direction == "up" else p_h - s.end_price
                    done = update_surge_status(copy.copy(s), p_h, t + horizon_s)
                    events.append((eid, s.window, round(fade, 6), done.status == SURGE_REVERTED))
            t += step_s
    return events


class TestBacktest:
    def test_two_scripted_spikes(self) -> None:
        pts, a_at, b_at = scripted_series()
        r = backtest_fade({"e1": pts}, market_of={"e1": "m1"})
        assert (r.n_surges, r.n_reverted, r.reversion_rate, r.horizon_hours) == (2, 1, 0.5, 6.0)
        # A: detected at the first 5-minute step after the jump, faded from ~0.68; 6 h later YES is ~0.536
        a_mark = next(p.price for p in reversed(pts) if p.ts <= math.ceil(a_at / 300) * 300)
        a_h = next(p.price for p in reversed(pts) if p.ts <= math.ceil(a_at / 300) * 300 + 6 * H)
        b_mark = next(p.price for p in reversed(pts) if p.ts <= math.ceil(b_at / 300) * 300)
        b_h = next(p.price for p in reversed(pts) if p.ts <= math.ceil(b_at / 300) * 300 + 6 * H)
        fade_a, fade_b = a_mark - a_h, b_h - b_mark  # type: ignore[operator]
        assert fade_a == pytest.approx(0.144, abs=0.006) and abs(fade_b) <= 0.0051
        assert r.avg_fade_return == pytest.approx((fade_a + fade_b) / 2, abs=1e-6)
        assert r.avg_hold_return == pytest.approx(-r.avg_fade_return, abs=1e-12)  # type: ignore[operator]
        assert r.by_window == {"5m": {"n": 2, "n_reverted": 1, "reversion_rate": 0.5,
                                      "avg_fade_return": r.avg_fade_return, "avg_hold_return": r.avg_hold_return}}
        assert any("fewer than 10" in n for n in r.notes)
        json.dumps(r.to_dict(), allow_nan=False)

    def test_matches_plain_walk(self) -> None:
        pts, _, _ = scripted_series(step=120.0)
        rng_pts = []
        price = 0.5
        for i, p in enumerate(pts):  # a second, noisier exchange with its own surges
            price = min(0.95, max(0.05, price + (0.03 if i % 997 == 0 else 0.0) - (0.03 if i % 1499 == 0 else 0.0)))
            rng_pts.append(PricePoint(ts=p.ts + 7, price=round(price + (0.002 if i % 3 else 0.0), 6)))
        series = {"e1": pts, "e2": rng_pts}
        ref = reference_backtest(series, 6 * H, 300.0)
        r = backtest_fade(series)
        assert r.n_surges == len(ref) >= 2
        assert r.n_reverted == sum(1 for e in ref if e[3])
        assert r.avg_fade_return == pytest.approx(sum(e[2] for e in ref) / len(ref), abs=1e-6)
        for name in {e[1] for e in ref}:
            assert r.by_window[name]["n"] == sum(1 for e in ref if e[1] == name)

    def test_recent_detection_is_pending(self) -> None:
        pts, a_at, _ = scripted_series()
        cut = [p for p in pts if p.ts <= a_at + 2 * H]
        r = backtest_fade({"e1": cut})
        assert r.n_surges == 0 and r.reversion_rate is None and r.avg_fade_return is None
        assert any("too recent" in n for n in r.notes)

    def test_other_steps(self) -> None:
        pts, _, _ = scripted_series(step=120.0)
        coarse = backtest_fade({"e1": pts}, step_s=600.0)  # fast path, every other grid point
        odd = backtest_fade({"e1": pts[-1500:]}, step_s=420.0)  # off-grid: plain fallback
        assert coarse.n_surges == 2 and coarse.n_reverted == 1
        assert odd.n_surges >= 0  # runs without error

    def test_empty_inputs(self) -> None:
        r = backtest_fade({})
        assert (r.n_surges, r.reversion_rate, r.by_window) == (0, None, {})
        assert backtest_fade({"e1": [], "e2": [PricePoint(ts=T0, price=0.5)]}).n_surges == 0

    def test_fast_on_a_week_of_ticks(self) -> None:
        week = [PricePoint(ts=T0 + i * 30.0, price=0.5 + (0.005 if (i // 10) % 2 else 0.0)) for i in range(7 * 24 * 120)]
        started = time.perf_counter()
        backtest_fade({f"e{i}": week for i in range(3)})
        assert time.perf_counter() - started < 3.0


# --------------------------------------------------------------------------- end to end with real analytics


def test_report_from_real_analytics() -> None:
    """detect_surges + high_band feed build_report directly."""
    t_now = T0 + 30 * H
    spike = [PricePoint(ts=T0 + i * 60.0, price=0.40 + (0.005 if (i // 5) % 2 else 0.0) + (0.18 if T0 + i * 60 >= t_now - 10 * 60 else 0.0))
             for i in range(int(30 * H / 60) + 1)]
    [s] = detect_surges(spike, t_now, "e1", "m1")
    s.attribution = att(odds=0.66, conf=0.64)
    s.id = 1
    hi = [PricePoint(ts=T0 + i * 60.0, price=0.03) for i in range(int(30 * H / 60) + 1)]
    b = high_band(hi, t_now, "e2", "m2", settlement_date=iso(t_now + 48 * H))
    assert b is not None and b.side == "NO"
    latest = {"e1": PricePoint(ts=t_now, price=s.end_price, bid=s.end_price - 0.005, ask=s.end_price + 0.005),
              "e2": PricePoint(ts=t_now, price=0.03, bid=0.025, ask=0.03)}
    r = build_report(now=t_now, surges=[s], bands=[b], latest=latest, infos={}, balance=BAL, initial_balance=BAL,
                     leader_value=None, my_rank=None, cup_end=CUP_END)
    kinds = {o.kind: o for o in r.opportunities}
    assert set(kinds) == {"fade", "carry"}
    assert kinds["fade"].side == "no" and kinds["carry"].side == "no"
    json.dumps(r.to_dict(), allow_nan=False)


def _unused(*_: Callable[..., Any]) -> None:  # keep imported names referenced for linters
    _ = (Opportunity, SURGE_WINDOWS)


# --------------------------------------------------------------------------- round-1 QA regressions


def senate_violation(amount: float = 0.03, r_price: float = 0.71, d_price: float = 0.32) -> Dict[str, Any]:
    """The demo's complementary Senate-control violation: sell YES on both outcomes (= buy NO on both)."""
    title = "Which party will control the Senate after the midterms?"
    legs = [("9016", "Republicans", r_price), ("9017", "Democrats", d_price)]
    return {
        "relationshipId": "rel-senate", "type": "complementary", "violationAmount": amount,
        "reason": f"{title} outcomes sum to {r_price + d_price:.3f}, {amount:.3f} outside the bound.",
        "suggestedCorrectiveTrades": [
            {"exchangeId": eid, "outcomeSide": "YES", "action": "sell", "rationale": "a full set settles at exactly 1",
             "marketId": "316", "marketTitle": title, "outcome": option, "currentPrice": price}
            for eid, option, price in legs
        ],
        "observedPrices": [{"exchangeId": eid, "price": price, "marketId": "316", "marketTitle": title, "outcome": option,
                            "currentPrice": price} for eid, option, price in legs],
        "direction": {"kind": "symmetric", "fromExchangeIds": ["9016", "9017"], "toExchangeIds": ["9016", "9017"],
                      "description": "Every outcome constrains every other."},
        "evaluationStatus": "violated", "constraint": {"priceRule": "sum(P(outcome)) = 1"},
    }


def senate_infos(settle: Optional[float] = NOW + 30 * 86400) -> Dict[str, ExchangeInfo]:
    title = "Which party will control the Senate after the midterms?"
    return {eid: ExchangeInfo(exchange_id=eid, market_id="316", option=opt, market_title=title,
                              settlement_date=iso(settle) if settle is not None else None)
            for eid, opt in (("9016", "Republicans"), ("9017", "Democrats"))}


class TestRound1Regressions:
    def test_functional_4_constraint_set_lists_every_leg_at_its_own_price(self) -> None:
        latest = {"9016": quote(0.705, 0.715), "9017": quote(0.325, 0.335)}
        [o] = arbitrage_opportunities({"data": [senate_violation()]}, [], BAL, latest=latest, infos=senate_infos(),
                                      cup_end=CUP_END)
        assert (o.exchange_id, o.option, o.side, o.unit) == (None, None, "no", "sets")
        assert o.title == "Which party will control the Senate after the midterms?"
        assert [(leg["exchange_id"], leg["option"], leg["side"], leg["price"]) for leg in o.legs] == [
            ("9016", "Republicans", "no", 0.295), ("9017", "Democrats", "no", 0.675)]
        assert o.entry_price == 0.97 and o.target_price == 1.0  # one set pays exactly 1.00
        assert o.edge == pytest.approx(0.03) and o.suggested_shares == math.floor(8000 / 0.97)
        text = " | ".join(o.rationale)
        assert "NO Republicans @ 0.295 + NO Democrats @ 0.675" in text and "pays 1.00 at settlement" in text
        assert "sets at the 8%-of-balance cap" in text
        assert any("live asks" in r for r in o.risks)

    def test_functional_4_edge_is_the_set_payoff_minus_its_cost(self) -> None:
        # engine prices sum to 1.03 (violation 0.03) but the legs cost 0.96 now: the set gains 0.04
        [o] = arbitrage_opportunities({"data": [senate_violation(0.03, 0.72, 0.32)]}, [], BAL)
        assert [leg["price"] for leg in o.legs] == [0.28, 0.68]
        assert o.entry_price == 0.96 and o.edge == pytest.approx(0.04)
        assert "+0.04 per set" in " | ".join(o.rationale)
        # spread ate the gap: kept as information, but nothing to size or rank
        latest = {"9016": quote(0.69, 0.73), "9017": quote(0.30, 0.34)}
        [gone] = arbitrage_opportunities({"data": [senate_violation()]}, [], BAL, latest=latest)
        assert gone.entry_price == 1.01 and gone.suggested_shares == 0 and gone.score == 0.0
        assert "the gap is gone" in " | ".join(gone.rationale)

    def test_functional_4_single_leg_constraint_is_a_plain_order(self) -> None:
        v = violation()
        v["suggestedCorrectiveTrades"] = v["suggestedCorrectiveTrades"][1:]
        [o] = arbitrage_opportunities({"data": [v]}, [], BAL)
        assert (o.exchange_id, o.side, o.entry_price, o.unit, o.legs) == ("12", "no", 0.45, "shares", [])

    def test_functional_4_headline_describes_the_set(self) -> None:
        r = build_report(**scenario(surges=[], bands=[], overround_rows=(), constraints={"data": [senate_violation()]},
                                    infos=senate_infos()))
        assert "2-leg set" in r.headline and "NO Republicans" in r.headline and "per set" in r.headline

    def test_functional_5_arbitrage_knows_when_every_leg_settles(self) -> None:
        [o] = arbitrage_opportunities({"data": [senate_violation()]}, [], BAL, infos=senate_infos(), cup_end=CUP_END)
        assert o.settles_before_cup_end is True
        assert any("before the Cup ends" in r for r in o.risks)
        assert not any("if that is after the Cup ends" in r for r in o.risks)
        [late] = arbitrage_opportunities({"data": [senate_violation()]}, [], BAL,
                                         infos=senate_infos(CUP_END + 86400), cup_end=CUP_END)
        assert late.settles_before_cup_end is False
        [unknown] = arbitrage_opportunities({"data": [senate_violation()]}, [], BAL, infos=senate_infos(None),
                                            cup_end=CUP_END)
        assert unknown.settles_before_cup_end is None
        # the multi-outcome book flag reads the settlement date of the market's outcomes
        rows = [{"market_id": "316", "market_title": "Senate control", "outcomes": 2, "overround": 0.97, "arbitrage": True}]
        latest = {"9016": quote(0.62, 0.64), "9017": quote(0.32, 0.33)}
        [book] = arbitrage_opportunities(None, rows, BAL, infos=senate_infos(), cup_end=CUP_END, latest=latest)
        assert book.settles_before_cup_end is True and book.unit == "sets"
        assert [(leg["option"], leg["price"]) for leg in book.legs] == [("Republicans", 0.64), ("Democrats", 0.33)]
        # the report passes the infos and the Cup end through
        r = build_report(**scenario(surges=[], bands=[], constraints={"data": [senate_violation()]}, infos=senate_infos(),
                                    overround_rows=()))
        assert [o.settles_before_cup_end for o in r.opportunities] == [True]

    def test_live_api_10_unknown_rank_leader_and_balance_are_stated(self) -> None:
        r = build_report(**scenario(balance=None, initial_balance=None, leader_value=None, my_rank=None))
        assert r.risk_mode == "balanced"
        assert "Balance unknown: sized on the 100,000 starting balance" in r.assumptions
        assert "Leaderboard unavailable: risk mode assumes balanced" in r.assumptions
        assert "neither defending" not in r.principles[-1] and "balanced by default" in r.principles[-1]
        assert "unknown" in r.principles[-1]
        fade = next(o for o in r.opportunities if o.kind == "fade")
        assert any("assumes the 100,000 starting balance" in line for line in fade.rationale)
        watch = next(o for o in r.opportunities if o.kind == "watch")
        assert not any("starting balance" in line for line in watch.rationale)  # nothing sized
        # everything known: no assumptions, the usual wording
        known = build_report(**scenario())
        assert known.assumptions == [] and "neither defending" in known.principles[-1]
        late = build_report(**scenario(leader_value=None, my_rank=None, now=CUP_END - 5 * 86400))
        assert late.risk_mode == "aggressive" and "treated as outside the top 10" in late.assumptions[0]
        assert json.loads(json.dumps(r.to_dict()))["assumptions"] == r.assumptions

    def test_live_api_7_closed_surges_give_no_ideas(self) -> None:
        from supermarket_bot.models import SURGE_CLOSED

        closed = [mk_surge(eid="e1", status=SURGE_CLOSED), mk_surge(eid="e3", verdict="unclear", status=SURGE_CLOSED)]
        r = build_report(**scenario(surges=closed, bands=[], constraints=None, overround_rows=()))
        assert r.opportunities == []
