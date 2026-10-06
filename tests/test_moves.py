"""Outside-move watcher, package "core" (docs/OUTSIDE_MOVES.md §22 core): offline, fast.

Hand numbers come from the spec's worked examples (§5.2, §6.1, Example L of §6.3 / §8.2 / §9 / §10.1, §10.2).
The watcher runs against fakes (scripted venues, a scripted Cup feed, MemoryMovesPersistence) and, for the GET-only
guarantee, against the real Polymarket / Kalshi providers over the offline fixtures
``tests/fixtures/external/moves_polymarket.json`` / ``moves_kalshi.json`` (answers served in order for the same
request, D57 key) plus ``rate_limits.json`` (429s)."""

from __future__ import annotations

import copy
import dataclasses
import json
import math
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx
import pytest

from supermarket_bot import fairvalue as fv
from supermarket_bot import moves as mv
from supermarket_bot.fairvalue import MatchTarget, ProviderResult, VenueMatch
from supermarket_bot.models import BookObservation, ExchangeInfo, FairValueQuote, RaceRef
from supermarket_bot.moves import CupQuote, MoveParams, VenueMove
from supermarket_bot.readonly import ReadOnlyViolation

FIXTURES = Path(__file__).parent / "fixtures" / "external"
T = 1791208800.0  # 2026-10-05T14:00:00Z: Example L's t_base (a multiple of 30: the fake Cup snapshot grid)
P = MoveParams()
NC = RaceRef(race_key="2026:SENATE:NC", office="SENATE", state="NC", party="D")
NC_R = RaceRef(race_key="2026:SENATE:NC", office="SENATE", state="NC", party="R")


def hms(h: int, m: int, s: float = 0.0) -> float:
    """Epoch seconds of HH:MM:SS UTC on Example L's day."""
    return T + (h - 14) * 3600.0 + m * 60.0 + s


# --------------------------------------------------------------------------- builders


def quote(t: float, value: Optional[float], *, venue: str = "polymarket", spread: float = 0.01,
          bid: Any = "auto", ask: Any = "auto", bid_size: Optional[float] = None, ask_size: Optional[float] = None,
          kind: str = "EXACT", conf: float = 0.95, flags: Optional[List[str]] = None) -> FairValueQuote:
    if bid == "auto":
        bid = None if value is None else round(value - spread / 2.0, 6)
    if ask == "auto":
        ask = None if value is None else round(value + spread / 2.0, 6)
    return FairValueQuote(venue=venue, external_id=f"{venue}:x", bid=bid, ask=ask, fetched_at=t, bid_size=bid_size,
                          ask_size=ask_size, match_kind=kind, match_confidence=conf, flags=list(flags or []))


def sample(t: float, value: Optional[float], *, liquidity: Optional[float] = None, eid: str = "1104",
           **kw: Any) -> mv.OutsideSample:
    return mv.sample_from_quote(eid, quote(t, value, **kw), liquidity=liquidity)


def series_of(points: Sequence[Tuple[float, Optional[float]]], *, venue: str = "polymarket", sigma: Any = "auto",
              params: MoveParams = P, **kw: Any) -> mv.SeriesBuffer:
    s = mv.SeriesBuffer("1104", venue, params)
    for t, v in points:
        assert s.add(sample(t, v, venue=venue, **kw))
    if sigma != "auto":
        s.sigma = lambda w, now, _s=sigma: _s  # type: ignore[assignment]
    return s


def vmove(venue: str, *, base: float, after: float, threshold: float, now: Optional[float] = None, spread: float = 0.01,
          base_ts: float = T, t_half: Optional[float] = None, window_s: float = 300.0, **kw: Any) -> VenueMove:
    move = round(after - base, 6)
    fields: Dict[str, Any] = dict(
        venue=venue, label=mv.venue_label(venue), window_s=window_s, base_ts=base_ts, base_value=base,
        after_ts=base_ts + window_s, after_value=after, now_value=after if now is None else now, move=move,
        threshold=threshold, sigma=kw.pop("sigma", 0.01), vol_known=True, ratio=round(abs(move) / threshold, 6),
        spread_base=spread, spread_now=spread, t_half=t_half if t_half is not None else base_ts + window_s / 2)
    fields.update(kw)
    return VenueMove(**fields)


def example_l_venues(**kalshi: Any) -> Dict[str, VenueMove]:
    """§6.3 Example L: Polymarket 0.520 -> 0.580 (threshold 0.04), Kalshi 0.515 -> 0.565 (threshold 0.044)."""
    pm = vmove("polymarket", base=0.52, after=0.58, threshold=0.04, sigma=0.008, spread=0.01, t_half=T + 150)
    ks = vmove("kalshi", base=0.515, after=0.565, threshold=0.044, sigma=0.011, spread=0.02, base_ts=T - 15,
               t_half=T + 170)
    if kalshi:
        ks = dataclasses.replace(ks, **kalshi)
    return {"polymarket": pm, "kalshi": ks}


def flat_cup(start: float, end: float, bid: float = 0.505, ask: float = 0.52, step: float = 30.0) -> List[CupQuote]:
    out = []
    t = start
    while t <= end + 1e-9:
        out.append(CupQuote(ts=t, bid=bid, ask=ask, mid=round((bid + ask) / 2, 6), last=0.51, spread=round(ask - bid, 6)))
        t += step
    return out


def cq(ts: float, mid: float, spread: float = 0.015) -> CupQuote:
    return CupQuote(ts=ts, bid=round(mid - spread / 2, 6), ask=round(mid + spread / 2, 6), mid=mid, spread=spread)


def example_l_alert(cup: Sequence[CupQuote], now: float = hms(14, 5, 15)) -> mv.MoveAlert:
    """Example L as a MoveAlert built the way the watcher builds one (classify at detection)."""
    move, reason = mv.combine_venues("1104", 300.0, example_l_venues(), P)
    assert move is not None and reason is None
    status, sentence, ctx, exclusion = mv.classify(move, cup, now, P)
    d = move.direction
    return mv.MoveAlert(
        alert_id=f"mv-1104-{int(move.t_base)}", exchange_id="1104", market_id="552",
        title="Will the Democratic Party win the North Carolina Senate?", option="YES", race_key=NC.race_key, party="D",
        race_label="North Carolina Senate (D)", direction=d, window_s=300.0, windows=[300.0],
        venues=list(move.venues), confirmation=move.confirmation, venue_moves=list(move.venue_moves),
        t_base=move.t_base, t_move=move.t_move, outside_before=move.before, outside_after=move.after,
        outside_now=move.now, move=move.move, peak_move=move.move, threshold=move.threshold, sigma=move.sigma,
        vol_known=move.vol_known, uncertainty=move.uncertainty, detected_at=now, updated_at=now, grown_at=now,
        state="open", status=status, status_label=mv.MOVE_STATUS_LABELS[status], reason=sentence, cup=ctx,
        lag_gap=round(move.move - (ctx.move_same_window or 0.0), 6), lag=mv.MoveLag(t_move=move.t_move),
        cup_now=ctx.now, opened_status=status)


# --------------------------------------------------------------------------- 1. thresholds, sigma, bars


@pytest.mark.parametrize("window, sigma, expected", [
    (60.0, 0.003, 0.03), (60.0, 0.010, 0.04), (300.0, 0.006, 0.04), (300.0, 0.013, 0.052),
    (900.0, 0.010, 0.05), (900.0, 0.016, 0.064), (3600.0, 0.015, 0.07), (3600.0, 0.030, 0.12),
    (60.0, None, 0.045), (300.0, None, 0.06), (900.0, None, 0.075), (3600.0, None, 0.105),
])
def test_threshold_table_of_5_3(window: float, sigma: Optional[float], expected: float) -> None:
    assert mv.threshold_for(window, sigma, P) == pytest.approx(expected)


def test_window_sigma_hand_series_of_5_2() -> None:
    params = MoveParams(vol_min_pairs=5)
    bars: List[Optional[float]] = [0.50, 0.50, 0.51, 0.51, 0.50, 0.52]  # changes 0, +0.01, 0, -0.01, +0.02
    assert mv.window_sigma(bars, 0.0, 60.0, 420.0, params) == pytest.approx(math.sqrt(1.2e-4), abs=1e-9)
    assert round(mv.window_sigma(bars, 0.0, 60.0, 420.0, params), 5) == 0.01095
    # the move under test and the minute before it never set their own bar: at now=360 the last pair is excluded
    assert mv.window_sigma(bars, 0.0, 60.0, 360.0, MoveParams(vol_min_pairs=4)) == pytest.approx(math.sqrt(0.0002 / 4))
    assert mv.window_sigma(bars, 0.0, 60.0, 420.0, MoveParams(vol_min_pairs=6)) is None  # too few pairs: unknown
    assert mv.window_sigma([0.5] * 40, 0.0, 60.0, 2400.0, P) == P.vol_floor  # never below one tick
    jump = [0.5] * 20 + [0.8] * 20  # one 30-pt jump is clipped to 10 pts
    s = mv.window_sigma(jump, 0.0, 60.0, 2400.0, P)
    assert s == pytest.approx(math.sqrt(0.1 ** 2 / 38))  # 38 pairs (minutes 1..38 <= now - 120)


def test_minute_bars_forward_fill_and_the_360_s_cap() -> None:
    samples = [sample(T + 5, 0.50), sample(T + 125, 0.52), sample(T + 130, 0.53)]
    assert mv.minute_bars(samples, T, T + 180) == [0.50, 0.50, 0.53]  # last sample of the minute; minute 1 filled
    long_gap = [sample(T + 5, 0.50), sample(T + 605, 0.55)]
    bars = mv.minute_bars(long_gap, T, T + 660)
    assert bars[:6] == [0.50] * 6  # filled while the last sample is at most 360 s old (minute 5 ends at T+360)
    assert bars[6:10] == [None] * 4 and bars[10] == 0.55
    # unpriced quotes never make a bar
    assert mv.minute_bars([sample(T + 5, None)], T, T + 60) == [None]
    # the SeriesBuffer's compact bars equal minute_bars over its samples
    s = series_of([(T + 5, 0.50), (T + 605, 0.55), (T + 650, 0.56)])
    start, got = s.bars(T + 720)
    assert start == T and got == mv.minute_bars([sample(T + 5, 0.50), sample(T + 605, 0.55), sample(T + 650, 0.56)],
                                                T, T + 720)


def test_series_ignores_old_and_duplicate_samples_and_prunes() -> None:
    s = series_of([(T, 0.5), (T + 15, 0.51)])
    assert not s.add(sample(T + 15, 0.6)) and not s.add(sample(T + 5, 0.6))
    assert [x.value for x in s.samples(T)] == [0.5, 0.51]
    s.prune(T + mv.MOVES_RAW_KEEP_S + 100)
    assert len(s) == 1 and s.latest().ts == T + 15  # the newest sample always stays


def test_sample_value_rule_and_thin() -> None:
    assert sample(T, 0.5).value == 0.5
    assert sample(T, None).value is None
    assert sample(T, 0.5, spread=0.12).value is None  # wider than 0.10: not a price
    assert sample(T, 0.5, bid=None, ask=0.52).value is None  # one-sided
    assert sample(T, 0.5, flags=["placeholder"]).value is None
    with pytest.raises(ValueError):
        mv.sample_from_quote("1", FairValueQuote(venue="kalshi", external_id="x", bid=0.4, ask=0.5))
    assert not mv.is_thin(sample(T, 0.5, spread=0.05), P)
    assert mv.is_thin(sample(T, 0.5, spread=0.06), P)  # spread
    assert mv.is_thin(sample(T, 0.5, liquidity=4999.0), P)  # Polymarket liquidity
    assert not mv.is_thin(sample(T, 0.5, liquidity=5000.0), P)
    assert mv.is_thin(sample(T, 0.5, bid_size=99.0, ask_size=500.0, venue="kalshi"), P)  # touch size
    assert not mv.is_thin(sample(T, 0.5, bid_size=100.0, ask_size=100.0, venue="kalshi"), P)


# --------------------------------------------------------------------------- 2. one venue over one window


SPEC_61 = [(0.0, 0.520), (15.0, 0.520), (30.0, 0.521), (45.0, 0.519), (60.0, 0.540), (75.0, 0.580), (90.0, 0.578)]


def test_evaluate_window_worked_example_of_6_1() -> None:
    s = series_of([(T + t, v) for t, v in SPEC_61], sigma=0.003)
    vm = mv.evaluate_window(s, T + 90, 60.0, P)
    assert vm is not None
    assert (vm.base_value, vm.base_ts, vm.after_value, vm.after_ts) == (0.52, T + 30, 0.578, T + 90)
    assert vm.move == pytest.approx(0.058) and vm.threshold == pytest.approx(0.03)
    assert vm.ratio == pytest.approx(1.9333, abs=1e-4) and vm.vol_known
    assert vm.t_half - T == pytest.approx(63.375)  # 60 + 15 x 0.009 / 0.040
    early = mv.evaluate_window(s, T + 75, 60.0, P)  # one moved sample alone is never enough
    assert early.base_ts == T + 15 and early.after_value == 0.54
    assert early.move == pytest.approx(0.02) and early.ratio == pytest.approx(0.6667, abs=1e-4)


def test_evaluate_window_base_median_tolerance_and_evaluability() -> None:
    pts = [(T + 0, 0.40), (T + 10, 0.50), (T + 20, 0.52), (T + 30, 0.51), (T + 80, 0.60), (T + 90, 0.60)]
    vm = mv.evaluate_window(series_of(pts, sigma=None), T + 90, 60.0, P)
    assert vm.base_value == 0.51 and vm.base_ts == T + 30  # median of the 3 latest in [0, 30], the 0.40 ignored
    assert vm.threshold == pytest.approx(0.045) and "warm-up thresholds" in vm.flags and not vm.vol_known
    assert [P.base_tolerance_s(w) for w in P.windows_s] == [30.0, 75.0, 225.0, 900.0]
    # nothing in the base band: not evaluable
    assert mv.evaluate_window(series_of([(T + 0, 0.5), (T + 80, 0.6), (T + 95, 0.6)]), T + 95, 60.0, P) is None
    # fewer than 2 confirming samples 10 s apart after the base: not evaluable
    assert mv.evaluate_window(series_of([(T + 0, 0.5), (T + 60, 0.6)]), T + 60, 60.0, P) is None
    assert mv.evaluate_window(series_of([(T + 0, 0.5), (T + 55, 0.6), (T + 60, 0.6)]), T + 60, 60.0, P) is None
    # the 1-hour window's base band is 15 minutes wide
    hour = series_of([(T + 0, 0.5), (T + 900, 0.5)] + [(T + 1800 + 15 * k, 0.5) for k in range(121)], sigma=0.01)
    assert mv.evaluate_window(hour, T + 4500, 3600.0, P).base_ts == T + 900
    # only samples fetched by `now` are used (no look-ahead)
    s = series_of([(T + t, v) for t, v in SPEC_61], sigma=0.003)
    assert mv.evaluate_window(s, T + 75, 60.0, P).now_value == 0.58


def test_evaluate_window_stale_gap_and_thin_flags() -> None:
    base = [(T + 15 * k, 0.5) for k in range(5)]  # 0..60
    s = series_of(base + [(T + 75, 0.56), (T + 90, 0.56)], sigma=0.003)
    vm = mv.evaluate_window(s, T + 90, 60.0, P)
    assert not vm.stale and not vm.gap and not vm.thin
    late = mv.evaluate_window(s, T + 90 + 46, 120.0, MoveParams())
    assert late is not None and late.stale  # the latest sample is 46 s old (> max(3 x 15, 45))
    gappy = series_of([(T, 0.5), (T + 30, 0.5), (T + 95, 0.56), (T + 110, 0.56)], sigma=0.003)
    assert mv.evaluate_window(gappy, T + 110, 80.0, P).gap  # 65 s between two samples inside the window
    wide = series_of(base + [(T + 75, 0.56), (T + 90, 0.56)], spread=0.06, sigma=0.003)
    assert mv.evaluate_window(wide, T + 90, 60.0, P).thin
    s2 = mv.SeriesBuffer("1104", "polymarket", P)
    for t, v in base:
        s2.add(sample(t, v, liquidity=4000.0 if t == T + 30 else 50000.0))  # a thin print inside the base band
    s2.add(sample(T + 75, 0.56, liquidity=50000.0))
    s2.add(sample(T + 90, 0.56, liquidity=50000.0))
    s2.sigma = lambda w, now: 0.003  # type: ignore[assignment]
    assert mv.evaluate_window(s2, T + 90, 60.0, P).thin


# --------------------------------------------------------------------------- 3. combining venues


def test_combine_venues_example_l() -> None:
    move, reason = mv.combine_venues("1104", 300.0, example_l_venues(), P)
    assert reason is None and move is not None
    assert move.confirmation == "two venues" and move.venues == ["polymarket", "kalshi"] and move.direction == 1
    assert (move.before, move.after, move.now) == (0.5175, 0.5725, 0.5725)
    assert move.move == pytest.approx(0.055) and move.threshold == pytest.approx(0.044) and move.sigma == 0.011
    assert move.uncertainty == pytest.approx(0.02)  # max(0.015 apart, 0.005 half-spread, Kalshi's 0.02 fee band)
    assert move.t_base == T and move.t_move == T + 160  # 14:02:40


def test_combine_venues_disagreement_one_venue_and_opposite() -> None:
    # Kalshi +0.01 (ratio 0.23): mean 0.86 < 1 and Polymarket alone has ratio 1.5 -> disagree
    assert mv.combine_venues("1104", 300.0, example_l_venues(after_value=0.525, now_value=0.525, move=0.01,
                                                             ratio=round(0.01 / 0.044, 6)), P) == (None, "disagree")
    # the other way
    assert mv.combine_venues("1104", 300.0, example_l_venues(after_value=0.465, now_value=0.465, move=-0.05,
                                                             ratio=1.136364), P) == (None, "disagree")
    # both the same way, each >= 0.5 of its threshold, but together under the bar: not a move, nothing filtered
    weak = example_l_venues(after_value=0.545, now_value=0.545, move=0.03, ratio=round(0.03 / 0.044, 6))
    weak["polymarket"] = dataclasses.replace(weak["polymarket"], after_value=0.56, now_value=0.56, move=0.04, ratio=1.0)
    assert mv.combine_venues("1104", 300.0, weak, P) == (None, None)
    # one usable venue (the other unmatched): ratio >= 1 triggers
    one, reason = mv.combine_venues("1104", 300.0, {"polymarket": example_l_venues()["polymarket"], "kalshi": None}, P)
    assert reason is None and one.confirmation == "one venue" and one.venues == ["polymarket"] and one.uncertainty == 0.01
    # a filtered venue neither confirms nor disagrees
    thin_ks = example_l_venues(thin=True, after_value=0.515, now_value=0.515, move=0.0, ratio=0.0)
    move, reason = mv.combine_venues("1104", 300.0, thin_ks, P)
    assert reason is None and move.confirmation == "one venue"
    # nothing moved
    flat = example_l_venues(after_value=0.52, now_value=0.52, move=0.005, ratio=0.1)
    flat["polymarket"] = dataclasses.replace(flat["polymarket"], after_value=0.525, now_value=0.525, move=0.005, ratio=0.1)
    assert mv.combine_venues("1104", 300.0, flat, P) == (None, None)


@pytest.mark.parametrize("change, reason", [
    ({"flags": ["placeholder"]}, "placeholder"), ({"flags": ["match"]}, "match"), ({"flags": ["near"]}, "near"),
    ({"stale": True}, "stale"), ({"gap": True}, "stale"), ({"thin": True}, "thin"), ({"flags": ["single_tick"]}, "single_tick"),
    ({"flags": ["thin", "match"]}, "match"),  # the first reason in the order of §6.2 is reported
])
def test_per_venue_filters_name_their_reason(change: Dict[str, Any], reason: str) -> None:
    pm = dataclasses.replace(example_l_venues()["polymarket"], **change)
    assert mv.venue_filter(pm) == reason
    assert mv.combine_venues("1104", 300.0, {"polymarket": pm}, P) == (None, reason)


# --------------------------------------------------------------------------- 4. classification


def test_classify_lagging_example_l() -> None:
    cup = flat_cup(T - 1200, hms(14, 5, 0))
    alert = example_l_alert(cup)
    assert alert.status == "lagging" and alert.lag_gap == pytest.approx(0.055)
    assert alert.reason == ("The outside price moved +5.5 pts in 5 min; the Cup has moved +0.0 pts over the same time "
                            "(now 0.512).")
    assert alert.cup.move_same_window == 0.0 and alert.cup.move_lookback == 0.0 and not alert.cup.short_history
    assert alert.cup.now.mid == 0.5125 and alert.cup.base.ts == T


def test_classify_already_moved_moved_first_converging_and_short_history() -> None:
    move, _ = mv.combine_venues("1104", 300.0, example_l_venues(), P)
    now = hms(14, 5, 15)
    # the Cup moved at 14:02:30, within 60 s of the outside's half-way time (14:02:40): not "first", already moved
    moved = flat_cup(T - 1200, T + 120) + [cq(T + 30 * k, 0.545) for k in range(5, 11)]
    status, reason, ctx, exclusion = mv.classify(move, moved, now, P)
    assert status == "already_moved" and exclusion is None and ctx.move_same_window == pytest.approx(0.0325)
    assert reason == "The Cup has already moved +3.2 of the 5.5 pts (now 0.545): no edge left to chase."
    # the Cup rose 0.515 -> 0.55 between 13:50 and 13:53, half-way (0.5425) confirmed at 13:51:30
    first = [cq(hms(13, 40) + 30 * k, 0.515) for k in range(21)]  # 13:40 .. 13:50
    first += [cq(hms(13, 50, 30), 0.525), cq(hms(13, 51), 0.535), cq(hms(13, 51, 30), 0.545)]
    first += [cq(hms(13, 52) + 30 * k, 0.55) for k in range(27)]  # 13:52 .. 14:05
    status, reason, ctx, exclusion = mv.classify(move, first, now, P)
    assert status == "moved_first" and ctx.half_cross_ts == hms(13, 51, 30) and exclusion is None
    assert ctx.half_cross_ts - move.t_move == -670.0
    assert reason == ("The Cup moved +3.5 pts about 11.2 min before the outside price: the outside followed the Cup, "
                      "not a lag.")
    # the outside moved to where the Cup already was
    status, reason, ctx, exclusion = mv.classify(move, flat_cup(T - 1200, now, 0.57, 0.58), now, P)
    assert status == "already_moved" and exclusion == "converging: the outside moved to the Cup's price"
    assert reason == "The outside price moved to where the Cup already was (0.575): no edge."
    # less Cup history than the 15-min look-back: the earliest snapshot, flagged
    status, _r, ctx, _e = mv.classify(move, flat_cup(T - 120, now), now, P)
    assert status == "lagging" and ctx.short_history and ctx.ref.ts == T - 120
    # no Cup snapshot within 2 minutes of now
    assert mv.classify(move, flat_cup(T - 1200, now - 150), now, P)[0] == "no_cup"
    # no Cup snapshot at the start of the move: compared with the earliest after it, excluded from the lag study
    status, _r, ctx, exclusion = mv.classify(move, flat_cup(T + 60, now), now, P)
    assert exclusion == "no Cup price at the start of the move" and ctx.base.ts == T + 60


def test_classify_down_move_sentence_signs() -> None:
    down = {v: dataclasses.replace(m, base_value=round(1 - m.base_value, 6), after_value=round(1 - m.after_value, 6),
                                   now_value=round(1 - m.now_value, 6), move=-m.move)
            for v, m in example_l_venues().items()}
    move, _ = mv.combine_venues("1104", 300.0, down, P)
    assert move.direction == -1 and move.move == pytest.approx(0.055)
    cup = flat_cup(T - 1200, T + 120, 0.48, 0.495) + [cq(T + 30 * k, 0.455) for k in range(5, 11)]
    status, reason, _ctx, _e = mv.classify(move, cup, hms(14, 5, 15), P)
    assert status == "already_moved"
    assert reason == "The Cup has already moved -3.2 of the 5.5 pts (now 0.455): no edge left to chase."


# --------------------------------------------------------------------------- 5. the hand trade


def book(at: float, bids: Sequence[Tuple[float, float]], asks: Sequence[Tuple[float, float]],
         source: str = "moves") -> BookObservation:
    return BookObservation(exchange_id="1104", observed_at=at, bids=list(bids), asks=list(asks), source=source)


def test_hand_trade_yes_example_l() -> None:
    now = hms(14, 5, 15)
    cup = CupQuote(ts=now - 15, bid=0.505, ask=0.52, mid=0.5125, spread=0.015)
    b = book(now - 2, [(0.505, 200), (0.50, 500)], [(0.52, 300), (0.525, 450), (0.53, 800)])
    trade, note = mv.hand_trade(1, [0.58, 0.565], 0.02, cup, b, "read", now, P)
    assert note is None and trade.side == "yes" and trade.text == "Buy YES at 0.520" and trade.note == mv.MOVES_TRADE_NOTE
    assert (trade.limit, trade.max_limit, trade.outside_value, trade.cup_half_spread) == (0.52, 0.525, 0.565, 0.0075)
    assert trade.edge_per_share == pytest.approx(0.0375) and trade.edge_after_uncertainty == pytest.approx(0.0175)
    assert trade.edge_at_resolution == pytest.approx(0.045)
    assert (trade.shares_at_limit, trade.shares_to_max, trade.book_source, trade.book_age_s) == (300.0, 750.0, "read", 2.0)
    # (integration) a book read just after the step's evaluation time has age 0, never a negative age
    later = book(now + 0.003, [(0.505, 200), (0.50, 500)], [(0.52, 300), (0.525, 450), (0.53, 800)])
    trade, _note = mv.hand_trade(1, [0.58, 0.565], 0.02, cup, later, "read", now, P)
    assert trade.book_age_s == 0.0 and trade.shares_at_limit == 300.0


def test_hand_trade_no_side_lead_long_numbers() -> None:
    now = T + 600
    cup = CupQuote(ts=now - 10, bid=0.57, ask=0.59, mid=0.58, spread=0.02)
    b = book(now - 5, [(0.57, 200), (0.565, 300), (0.56, 500)], [(0.59, 400)], source="tracker")
    trade, note = mv.hand_trade(-1, [0.52, 0.525], 0.005, cup, b, "stored", now, P)
    assert note is None and trade.side == "no" and trade.text == "Buy NO at 0.430"
    assert (trade.limit, trade.max_limit, trade.outside_value) == (0.43, 0.45, 0.475)
    assert trade.edge_per_share == pytest.approx(0.035) and trade.edge_after_uncertainty == pytest.approx(0.03)
    assert trade.shares_at_limit == 200.0 and trade.shares_to_max == 1000.0 and trade.book_source == "stored"


def test_hand_trade_no_edge_no_quote_unknown_and_stale_book() -> None:
    now = hms(14, 5, 15)
    trade, note = mv.hand_trade(1, [0.58, 0.565], 0.02, CupQuote(ts=now, bid=0.52, ask=0.535, mid=0.5275, spread=0.015),
                                None, None, now, P)
    assert trade is None and note == ("The Cup's ask (0.535) leaves less than 1 cent per share after the spread and the "
                                      "outside price's uncertainty: no trade suggested.")
    assert mv.hand_trade(1, [0.58], 0.02, CupQuote(ts=now, bid=None, ask=0.52, mid=0.52), None, None, now, P) == (
        None, "The Cup has no two-sided quote right now.")
    cup = CupQuote(ts=now - 15, bid=0.505, ask=0.52, mid=0.5125, spread=0.015)
    trade, _ = mv.hand_trade(1, [0.58, 0.565], 0.02, cup, None, None, now, P)
    assert trade.shares_at_limit is None and trade.shares_to_max is None and trade.book_source is None
    old = book(now - 121, [(0.505, 200)], [(0.52, 300)])
    trade, _ = mv.hand_trade(1, [0.58, 0.565], 0.02, cup, old, "read", now, P)
    assert trade.shares_at_limit is None  # a book older than 120 s gives no depth
    # a fresh book newer than the snapshot sets the touch (here a tighter ask)
    fresher = book(now - 1, [(0.51, 100)], [(0.515, 50), (0.52, 300)])
    trade, _ = mv.hand_trade(1, [0.58, 0.565], 0.02, cup, fresher, "read", now, P)
    assert trade.limit == 0.515 and trade.cup_half_spread == pytest.approx(0.0025) and trade.shares_at_limit == 50.0
    # max_limit is floored on the tick
    trade, _ = mv.hand_trade(1, [0.5689], 0.0, cup, None, None, now, P)
    assert trade.max_limit == 0.55  # floor_tick(0.5689 - 0.0075 - 0 - 0.01 = 0.5514)


# --------------------------------------------------------------------------- 6. the lag


def lag_alert(**cup_extra: Any) -> mv.MoveAlert:
    return example_l_alert(flat_cup(T - 1200, hms(14, 5, 0)))


def follow_series() -> List[CupQuote]:
    cup = flat_cup(T - 1200, hms(14, 5, 0))
    return cup + [cq(hms(14, 5, 30), 0.5125), cq(hms(14, 6), 0.52), cq(hms(14, 6, 30), 0.53), cq(hms(14, 7), 0.545),
                  cq(hms(14, 7, 30), 0.55)]


def test_update_lag_followed_example_l() -> None:
    alert = lag_alert()
    cup = follow_series()
    outside = [(hms(14, 5, 30) + 15 * k, 0.5725) for k in range(10)]
    assert not mv.update_lag(alert, cup, outside, hms(14, 7, 15), P)  # one snapshot over half is not a follow yet
    assert mv.update_lag(alert, cup, outside, hms(14, 7, 30), P)
    lag = alert.lag
    assert lag.outcome == "followed" and lag.followed_at == hms(14, 7)
    assert lag.lag_s == 260.0 and lag.lag_after_alert_s == 105.0
    assert alert.status == "already_moved" and alert.status_label == "Cup already moved" and alert.opened_status == "lagging"
    assert lag.reason == "The Cup moved +3.2 pts, 4.3 min after the outside move (1.8 min after the alert)."


def test_update_lag_one_snapshot_blip_is_not_a_follow() -> None:
    alert = lag_alert()
    cup = flat_cup(T - 1200, hms(14, 5, 0)) + [cq(hms(14, 6), 0.55), cq(hms(14, 6, 30), 0.52), cq(hms(14, 7), 0.55),
                                                cq(hms(14, 7, 30), 0.52)]
    mv.update_lag(alert, cup, [], hms(14, 8), P)
    assert alert.lag.outcome == "pending" and alert.status == "lagging"


def test_update_lag_reverted_and_the_earlier_of_follow_and_reversion() -> None:
    alert = lag_alert()
    cup = flat_cup(T - 1200, hms(14, 10))
    outside = [(hms(14, 6), 0.5725), (hms(14, 6, 15), 0.545), (hms(14, 6, 30), 0.5375)]  # <= 0.5175 + 0.0275 twice
    assert mv.update_lag(alert, cup, outside, hms(14, 6, 30), P)
    assert alert.lag.outcome == "reverted" and alert.status == "reverted"
    assert alert.lag.reason == "The outside price came back 3.5 of its 5.5 pts before the Cup moved."
    both = lag_alert()
    mv.update_lag(both, follow_series(), [(hms(14, 7, 15), 0.54), (hms(14, 7, 20), 0.54)], hms(14, 7, 30), P)
    assert both.lag.outcome == "followed"  # the follow (14:07:00) is earlier than the reversion (14:07:15)
    other = lag_alert()
    mv.update_lag(other, follow_series(), [(hms(14, 6, 45), 0.54), (hms(14, 6, 50), 0.54)], hms(14, 7, 30), P)
    assert other.lag.outcome == "reverted"


def test_update_lag_not_followed_and_censored_without_cup() -> None:
    alert = lag_alert()
    cup = flat_cup(T - 1200, alert.t_move + 3600)
    mv.update_lag(alert, cup, [(alert.t_move + 3590, 0.5725)], alert.t_move + 3599, P)
    assert alert.lag.outcome == "pending"
    assert mv.update_lag(alert, cup, [(alert.t_move + 3590, 0.5725)], alert.t_move + 3600, P)
    assert alert.lag.outcome == "not_followed" and alert.status == "lagging"
    assert alert.lag.reason == "The Cup did not follow within 60 min; the gap is 5.5 pts now."
    quiet = lag_alert()
    assert mv.update_lag(quiet, flat_cup(T - 1200, hms(14, 5)), [], hms(14, 15, 1), P)
    assert quiet.lag.outcome == "censored" and quiet.lag.reason == "No Cup price for 10 min."


def test_update_lag_captures_example_l_and_the_no_side_exit() -> None:
    alert = lag_alert()
    cup = follow_series() + [CupQuote(ts=hms(14, 9), bid=0.55, ask=0.56, mid=0.555, spread=0.01),
                             CupQuote(ts=hms(14, 39), bid=0.565, ask=0.575, mid=0.57, spread=0.01)]
    outside = [(hms(14, 5, 30) + 15 * k, 0.5725) for k in range(20)]
    mv.update_lag(alert, cup, outside, hms(14, 9, 14), P)
    assert alert.lag.capture_at is None
    # (integration) the capture waits for the Cup snapshot after t_h, or MOVES_CUP_MAX_AGE_S (none here)
    mv.update_lag(alert, cup, outside, hms(14, 9, 15), P)
    assert alert.lag.capture_at is None
    mv.update_lag(alert, cup, outside, hms(14, 11, 15), P)
    assert alert.lag.capture_at == hms(14, 9, 15) and alert.lag.capture_4m == pytest.approx(0.0075)
    mv.update_lag(alert, cup, outside, hms(14, 39, 15), P)
    assert alert.lag.exit_at is None
    mv.update_lag(alert, cup, outside, hms(14, 41, 15), P)
    assert alert.lag.exit_at == hms(14, 39, 15) and alert.lag.exit_30m == pytest.approx(0.005)
    # NO side: buy NO at 1 - YES bid at t_h, sell at 1 - YES ask at t_e -> bid(t_h) - ask(t_e)
    down = lag_alert()
    down.direction = -1
    mv.update_lag(down, cup, outside, hms(14, 41, 15), P)
    assert down.lag.exit_30m == pytest.approx(0.55 - 0.575)
    assert down.lag.capture_4m == pytest.approx(-(0.5725 - 0.555) - 0.01)
    # only alerts that were lagging at detection get captures
    first = lag_alert()
    first.opened_status = first.status = "already_moved"
    mv.update_lag(first, cup, outside, hms(14, 41, 15), P)
    assert first.lag.capture_4m is None and first.lag.exit_30m is None


def test_update_lag_captures_take_the_less_favourable_bracketing_cup_snapshot() -> None:
    """(integration) The Cup is sampled every ~30 s: the snapshot at or before t_h may predate a catch-up that
    already happened by t_h. The capture and the entry price use the worse of it and the first snapshot after."""
    cup = follow_series() + [CupQuote(ts=hms(14, 9), bid=0.55, ask=0.56, mid=0.555, spread=0.01),
                             CupQuote(ts=hms(14, 9, 30), bid=0.56, ask=0.57, mid=0.565, spread=0.01),
                             CupQuote(ts=hms(14, 39), bid=0.565, ask=0.575, mid=0.57, spread=0.01),
                             CupQuote(ts=hms(14, 39, 30), bid=0.56, ask=0.57, mid=0.565, spread=0.01)]
    outside = [(hms(14, 5, 30) + 15 * k, 0.5725) for k in range(20)]
    alert = lag_alert()
    mv.update_lag(alert, cup[:-3], outside, hms(14, 9, 29), P)  # the snapshot after t_h is not in yet: wait
    assert alert.lag.capture_at is None
    mv.update_lag(alert, cup[:-2], outside, hms(14, 9, 30), P)
    # 14:09:00 leaves 0.5725 - 0.555 - 0.01 = +0.0075, 14:09:30 only 0.5725 - 0.565 - 0.01 = -0.0025: the worse
    assert alert.lag.capture_at == hms(14, 9, 15) and alert.lag.capture_4m == pytest.approx(-0.0025)
    mv.update_lag(alert, cup, outside, hms(14, 39, 30), P)
    # bought at the higher ask around t_h (0.57), sold at the lower bid around t_e (0.56)
    assert alert.lag.exit_at == hms(14, 39, 15) and alert.lag.exit_30m == pytest.approx(0.56 - 0.57)
    down = lag_alert()
    down.direction = -1
    mv.update_lag(down, cup, outside, hms(14, 39, 30), P)
    # NO: bought at 1 - the lower YES bid around t_h (0.55), sold at 1 - the higher YES ask around t_e (0.575)
    assert down.lag.exit_30m == pytest.approx(0.55 - 0.575)
    assert down.lag.capture_4m == pytest.approx(min(-(0.5725 - 0.555) - 0.01, -(0.5725 - 0.565) - 0.01))


# --------------------------------------------------------------------------- 7. the summary


def summary_alert(i: int, outcome: str, *, race: str, lag_s: Optional[float] = None, after: Optional[float] = None,
                  capture: Optional[float] = None, exit_: Optional[float] = None, eligible: bool = True) -> Dict[str, Any]:
    return {"alert_id": f"mv-{i}", "exchange_id": str(i), "race_key": race, "detected_at": T + i,
            "lag": {"eligible": eligible, "outcome": outcome, "lag_s": lag_s, "lag_after_alert_s": after,
                    "capture_4m": capture, "exit_30m": exit_}}


def spec_summary_alerts() -> List[Dict[str, Any]]:
    """§10.2's example: 46 outside-led moves on 21 races, 19 followed within 5 min, 33 within 60, median lag 210 s
    (90 s after the alert), capture mean +0.004 (n=40), exit mean -0.002 (n=37)."""
    lags = [30.0 + 10 * k for k in range(16)] + [210.0, 250.0, 280.0] + [600.0 + 200 * k for k in range(14)]
    assert len(lags) == 33 and sorted(lags)[16] == 210.0
    out = []
    for i in range(46):
        race = f"2026:SENATE:R{i % 21}"
        capture = (0.010 if i % 2 else -0.002) if i < 40 else None
        exit_ = -0.002 if i < 37 else None
        if i < 33:
            out.append(summary_alert(i, "followed", race=race, lag_s=lags[i], after=lags[i] - 120, capture=capture,
                                     exit_=exit_))
        else:
            out.append(summary_alert(i, "reverted" if i < 41 else "not_followed", race=race, capture=capture, exit_=exit_))
    return out


def test_summarise_the_spec_example_sentence_verbatim() -> None:
    alerts = spec_summary_alerts()
    alerts.append(summary_alert(100, "excluded", race="2026:SENATE:R0", eligible=False))
    alerts.append(summary_alert(101, "pending", race="2026:SENATE:R1"))
    alerts.append(summary_alert(102, "censored", race="2026:SENATE:R2"))
    s = mv.summarise(alerts, T + 1000)
    assert (s.moves, s.races, s.resolved, s.pending, s.censored, s.excluded, s.outside_led) == (48, 21, 46, 1, 1, 1, 46)
    assert s.followed == 33 and s.followed_within_n == {"60": 4, "300": 19, "900": 21, "3600": 33}
    assert s.followed_within["300"] == pytest.approx(19 / 46, abs=1e-6)
    assert s.followed_within["3600"] == pytest.approx(33 / 46, abs=1e-6)
    assert (s.reverted, s.not_followed, s.never_followed) == (8, 5, 13)
    assert s.never_followed_share == pytest.approx(13 / 46, abs=1e-6) and s.cup_first_share == 0.0
    assert s.median_lag_s == 210.0 and s.median_lag_after_alert_s == 90.0
    assert s.capture_4m["n"] == 40 and s.capture_4m["mean"] == pytest.approx(0.004)
    assert s.exit_30m["n"] == 37 and s.exit_30m["mean"] == pytest.approx(-0.002) and s.exit_30m["positive_share"] == 0.0
    assert not s.small_sample
    assert s.sentence == (
        "Over 46 outside moves on 21 races, the Cup followed within 5 minutes 41% of the time and within an hour 72%; "
        "the median lag was 3.5 min after the outside move (1.5 min after the alert). Acting 4 minutes after an alert "
        "left an average gap of +0.004 per share net of the spread (n=40); buying then and selling 30 minutes later "
        "averaged -0.002 per share (n=37). Moves cluster on news days and races share national swings, so treat this "
        "as a hint, not proof.")


def test_summarise_small_sample_no_data_quartiles_and_ci90_over_race_means() -> None:
    assert mv.summarise([], T).sentence == mv.MOVES_NO_DATA_SENTENCE == "No outside move has been measured yet."
    small = [summary_alert(1, "followed", race="A", lag_s=200, after=100), summary_alert(2, "followed", race="B", lag_s=400, after=300),
             summary_alert(3, "reverted", race="C"), summary_alert(4, "cup_first", race="D", lag_s=-200),
             summary_alert(5, "excluded", race="E", eligible=False)]
    s = mv.summarise(small, T)
    assert s.small_sample and s.cup_first == 1 and s.outside_led == 3 and s.cup_first_share == pytest.approx(0.25)
    assert s.sentence == ("Only 4 outside moves on 4 races so far (2 followed by the Cup, 1 never, 1 where the Cup moved "
                          "first): far too few to say whether the Cup lags. Wait for at least 20 moves on 10 races.")
    assert s.lag_quartiles_s == [250.0, 350.0] and s.median_lag_s == 300.0
    # ci90: a t-interval over PER-RACE means (race A's two alerts count as one mean of 0.01)
    values = {"A": [0.0, 0.02], "B": [0.02], "C": [0.03], "D": [0.04]}
    rows = [summary_alert(10 + i, "followed", race=r, lag_s=60, after=0, capture=v)
            for i, (r, vs) in enumerate(values.items()) for v in vs]
    assert mv.summarise(rows, T).capture_4m["ci90"] is None  # 4 races: no interval
    rows.append(summary_alert(30, "followed", race="E", lag_s=60, after=0, capture=0.05))
    ci = mv.summarise(rows, T).capture_4m["ci90"]
    half = 2.131847 * math.sqrt(2.5e-4) / math.sqrt(5)  # race means 0.01..0.05: sd 0.0158, t(0.95, 4) = 2.1318
    assert ci == pytest.approx([0.03 - half, 0.03 + half], abs=2e-6)
    assert mv.summarise(rows, T).capture_4m["mean"] == pytest.approx(0.16 / 6, abs=1e-6)  # the alert mean stays per alert


def test_lag_table_lines_give_every_number_with_its_sample_size() -> None:
    s = mv.summarise(spec_summary_alerts(), T + 1000).to_dict()
    lines = mv.lag_table_lines(s)
    assert lines[0] == ("    lag study: 46 resolved outside-led moves on 21 races; 0 where the Cup moved first; not counted: "
                        "0 excluded (converging or no Cup base), 0 censored, 0 still being measured")
    assert lines[1] == ("      followed within 1 min 4/46 (9%), 5 min 19/46 (41%), 15 min 21/46 (46%), "
                        "1 h 33/46 (72%)")
    assert lines[2] == "      never followed 13/46 (28%): 8 came back, 5 did not follow within 60 min"
    assert lines[3] == "      median lag 3.5 min after the outside move (1.5 min after the alert; n=33 followed)"
    assert lines[4].startswith("      gap left 4 min after the alert, net of the spread: mean +0.004, median ")
    assert "(n=40; 90% interval " in lines[4] and "over per-race means; no price impact or queue)" in lines[4]
    assert lines[5].startswith("      bought 4 min after the alert, sold 30 min later: mean -0.002, median -0.002 a share, "
                               "positive 0% (n=37; 90% interval ")
    empty = mv.lag_table_lines(mv.summarise([summary_alert(1, "pending", race="A")], T).to_dict())
    assert empty == ["    lag study: 0 resolved outside-led moves on 0 races; 0 where the Cup moved first; not counted: "
                     "0 excluded (converging or no Cup base), 0 censored, 1 still being measured"]


def test_summarise_counts_races_over_the_same_moves_as_its_numbers() -> None:
    """(integration) Races are counted over the resolved moves the sentence counts, not pending ones; the
    sample-size bar uses the outside-led races the shares are over; singular words read right; nothing resolved
    yet says so instead of "Only 0 outside moves on 1 races"."""
    one = mv.summarise([summary_alert(1, "pending", race="A")], T)
    assert one.moves == 1 and one.races == 0 and one.small_sample
    assert one.sentence == ("No outside move has been resolved yet (1 still being measured): far too few to say whether "
                            "the Cup lags. Wait for at least 20 moves on 10 races.")
    cut = mv.summarise([summary_alert(1, "pending", race="A"), summary_alert(2, "censored", race="B")], T)
    assert cut.sentence.startswith("No outside move has been resolved yet (1 still being measured, 1 cut short, not counted)")
    single = mv.summarise([summary_alert(1, "followed", race="A", lag_s=60, after=30),
                           summary_alert(2, "pending", race="B")], T)
    assert single.races == 1 and single.sentence.startswith("Only 1 outside move on 1 race so far (1 followed by the Cup")
    # 20 resolved outside-led moves on 3 races plus pending ones on 8 other races: still a small sample
    rows = [summary_alert(i, "followed", race=f"R{i % 3}", lag_s=60, after=30) for i in range(20)]
    rows += [summary_alert(100 + i, "pending", race=f"P{i}") for i in range(8)]
    s = mv.summarise(rows, T)
    assert (s.outside_led, s.outside_led_races, s.races) == (20, 3, 3)
    assert s.small_sample and s.sentence.startswith("Only 20 outside moves on 3 races so far")
    # cup-first moves count in the resolved races but not in the outside-led ones
    mixed = [summary_alert(i, "followed", race=f"R{i % 10}", lag_s=60, after=30) for i in range(20)]
    mixed.append(summary_alert(50, "cup_first", race="CF", lag_s=-300))
    m = mv.summarise(mixed, T)
    assert (m.races, m.outside_led_races, m.small_sample) == (11, 10, False)
    assert m.sentence.startswith("Over 20 outside moves on 10 races, ")


# --------------------------------------------------------------------------- the watcher with fakes


class Clock:
    def __init__(self, t: float = T) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def set(self, t: float) -> None:
        self.t = float(t)


def target(eid: str = "1104", race: RaceRef = NC, **kw: Any) -> MatchTarget:
    party = {"D": "Democratic", "R": "Republican"}[race.party]
    title = kw.pop("title", f"Will the {party} Party win the North Carolina Senate?")
    return MatchTarget(exchange_id=eid, market_id=kw.pop("market_id", "552"), title=title, option="YES", race=race, **kw)


class FakeFairValues:
    mode = "auto"

    def __init__(self, targets: Sequence[MatchTarget], providers: Sequence[Any] = ()) -> None:
        self._targets = list(targets)
        self.providers = list(providers)

    def targets(self) -> List[MatchTarget]:
        return list(self._targets)


Script = Callable[[str, str, float], Optional[Dict[str, Any]]]  # (venue, eid, t) -> quote fields | None


class ScriptVenue:
    """A pollable venue: ``world.outside(venue, eid, t)`` gives {"v" | "bid"/"ask", "spread", sizes, "liquidity", "kind",
    "conf"} or None; ``fetched_at`` is the clock when poll returns (``poll_cost_s`` is spent inside)."""

    def __init__(self, name: str, world: "World") -> None:
        self.name = name
        self.world = world
        self.calls: List[float] = []
        self.raise_exc: Optional[BaseException] = None
        self.status = "ok"
        self.poll_cost_s = 0.0
        self.locked_seen: List[bool] = []

    def poll(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        self.locked_seen.append(self.world.w._lock.locked())
        self.calls.append(now)
        assert deadline == pytest.approx(now + 10.0)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.status != "ok":
            return ProviderResult(venue=self.name, status=self.status, errors=[f"{self.name} is {self.status}."])
        self.world.clock.set(self.world.clock.t + self.poll_cost_s)
        fetched = self.world.clock()
        res = ProviderResult(venue=self.name, status="ok", requests=1)
        for t in targets:
            spec = self.world.outside(self.name, t.exchange_id, now)
            if spec is None:
                continue
            spread = spec.get("spread", 0.01)
            v = spec.get("v")
            bid = spec.get("bid", None if v is None else round(v - spread / 2, 6))
            ask = spec.get("ask", None if v is None else round(v + spread / 2, 6))
            res.quotes[t.exchange_id] = FairValueQuote(
                venue=self.name, external_id=f"{self.name}:{t.exchange_id}", bid=bid, ask=ask,
                bid_size=spec.get("bid_size"), ask_size=spec.get("ask_size"), fetched_at=fetched,
                match_kind=spec.get("kind", "EXACT"), match_confidence=spec.get("conf", 0.95))
            res.matches[t.exchange_id] = VenueMatch(venue=self.name, exchange_id=t.exchange_id,
                                                    external_id=f"{self.name}:{t.exchange_id}")
            if spec.get("liquidity") is not None:
                res.liquidity[t.exchange_id] = spec["liquidity"]
        return res

    def budget(self) -> Dict[str, int]:
        return {"used": 4, "limit": 45}


class FakeCup:
    """Cup snapshots on a 30-s grid from ``world.cup(eid, ts) -> (bid, ask) | None``; books on request."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.books: Dict[str, BookObservation] = {}
        self.stored: Dict[str, BookObservation] = {}
        self.book_calls: List[str] = []
        self.history_calls = 0
        self.locked_seen: List[bool] = []

    def _snap(self, eid: str, ts: float) -> Optional[CupQuote]:
        got = self.world.cup(eid, ts)
        if got is None:
            return None
        bid, ask = got
        return CupQuote(ts=ts, bid=bid, ask=ask, mid=round((bid + ask) / 2, 6), last=None, spread=round(ask - bid, 6))

    def quotes(self) -> Dict[str, CupQuote]:
        self.locked_seen.append(self.world.w._lock.locked())
        ts = math.floor(self.world.clock() / 30.0) * 30.0
        out = {}
        for t in self.world.targets:
            q = self._snap(t.exchange_id, ts)
            if q is not None:
                out[t.exchange_id] = q
        return out

    def history(self, seconds: float) -> Dict[str, List[CupQuote]]:
        self.history_calls += 1
        now = math.floor(self.world.clock() / 30.0) * 30.0
        out: Dict[str, List[CupQuote]] = {}
        for t in self.world.targets:
            pts = [self._snap(t.exchange_id, now - 30.0 * k) for k in range(int(seconds // 30), 0, -1)]
            pts = [p for p in pts if p is not None and p.ts >= self.world.history_from]
            if pts:
                out[t.exchange_id] = pts
        return out

    def book(self, eid: str) -> Optional[BookObservation]:
        self.locked_seen.append(self.world.w._lock.locked())
        self.book_calls.append(eid)
        b = self.books.get(eid)
        return dataclasses.replace(b, observed_at=self.world.clock()) if b is not None else None

    def stored_book(self, eid: str) -> Optional[BookObservation]:
        return self.stored.get(eid)


class CheckedPersistence(mv.MemoryMovesPersistence):
    """MemoryMovesPersistence that asserts the watcher lock is never held while it is called."""

    def __init__(self, watcher_ref: Callable[[], Any]) -> None:
        super().__init__()
        self.watcher_ref = watcher_ref
        self.quote_calls = 0
        self.alert_calls = 0

    def add_outside_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        assert not self.watcher_ref()._lock.locked()
        self.quote_calls += 1
        return super().add_outside_quotes(rows)

    def put_move_alerts(self, rows: Sequence[Mapping[str, Any]]) -> int:
        assert not self.watcher_ref()._lock.locked()
        self.alert_calls += 1
        return super().put_move_alerts(rows)


def ramp(t: float, a: float, b: float, x: float) -> float:
    return 0.0 if t < a else x if t >= b else x * (t - a) / (b - a)


class World:
    """A watcher over scripted venues and a scripted Cup. Defaults: one NC (D) target, Polymarket + Kalshi flat at 0.50,
    the Cup flat at 0.495 / 0.505."""

    def __init__(self, *, targets: Optional[Sequence[MatchTarget]] = None, venues: Sequence[str] = ("polymarket", "kalshi"),
                 outside: Optional[Script] = None, cup: Optional[Callable[[str, float], Optional[Tuple[float, float]]]] = None,
                 params: Optional[MoveParams] = None, persistence: Any = "checked", alerts_path: Optional[Path] = None,
                 start: float = T, demo: bool = False) -> None:
        self.clock = Clock(start)
        self.targets = list(targets or [target()])
        self.outside: Script = outside or (lambda venue, eid, t: {"v": 0.50})
        self.cup = cup or (lambda eid, t: (0.495, 0.505))
        self.history_from = start
        self.venues = [ScriptVenue(n, self) for n in venues]
        self.fv = FakeFairValues(self.targets)
        self.feed = FakeCup(self)
        if persistence == "checked":
            persistence = CheckedPersistence(lambda: self.w)
        self.persistence = persistence
        self.w = mv.OutsideMoveWatcher(self.fv, providers=self.venues, cup=self.feed, persistence=persistence,
                                       alerts_path=alerts_path, clock=self.clock, params=params or MoveParams(), demo=demo)
        self.events: List[mv.MoveEvent] = []
        self.w.add_listener(self.events.append)
        self.last: Optional[float] = None
        self.reports: List[mv.MovesStepReport] = []

    def run_to(self, end: float, step: float = 15.0) -> "World":
        t = self.clock.t if self.last is None else self.last + step
        while t <= end + 1e-9:
            self.clock.set(t)
            self.reports.append(self.w.step(t))
            self.last = t
            t += step
        return self

    def kinds(self, eid: Optional[str] = None) -> List[Tuple[str, str]]:
        return [(e.kind, e.alert_id) for e in self.events if eid is None or e.alert["exchange_id"] == eid]

    def alerts(self) -> List[Dict[str, Any]]:
        return self.w.summary()["alerts"]

    def suppressed(self) -> List[Dict[str, Any]]:
        return self.w.summary()["suppressed"]


def lead(at: float = T + 300, size: float = 0.06, cup_at_: Optional[float] = T + 600, cup_size: float = 0.04,
         base: float = 0.50, venues: Sequence[str] = ("polymarket", "kalshi")) -> Tuple[Script, Callable[[str, float], Any]]:
    """The outside steps by ``size`` at ``at`` (both venues); the Cup follows by ``cup_size`` at ``cup_at_``."""
    def outside(venue: str, eid: str, t: float) -> Optional[Dict[str, Any]]:
        if venue not in venues:
            return {"v": base}
        return {"v": round(base + (size if t >= at else 0.0), 6)}

    def cup(eid: str, t: float) -> Tuple[float, float]:
        off = cup_size if cup_at_ is not None and t >= cup_at_ else 0.0
        return round(base - 0.005 + off, 6), round(base + 0.005 + off, 6)
    return outside, cup


def test_watcher_lagging_alert_follow_and_close_events_once_each(tmp_path: Path) -> None:
    outside, cup = lead()
    world = World(outside=outside, cup=cup, alerts_path=tmp_path / "alerts.jsonl")
    world.feed.books["1104"] = book(0.0, [(0.495, 120), (0.49, 300)], [(0.505, 250), (0.51, 400), (0.53, 900)])
    world.run_to(T + 315)
    (alert,) = world.alerts()
    aid = f"mv-1104-{int(T + 255)}"  # the base band at 14:05:15 is [T+225, T+255]
    assert alert["alert_id"] == aid and alert["status"] == "lagging" and alert["state"] == "open"
    assert alert["window_s"] == 60.0 and alert["windows"] == [60.0, 300.0] and alert["confirmation"] == "two venues"
    assert alert["venues"] == ["polymarket", "kalshi"] and alert["direction"] == 1
    assert alert["detected_at"] == T + 315 and alert["t_move"] == pytest.approx(T + 292.5)
    assert alert["move"] == pytest.approx(0.06) and alert["lag_gap"] == pytest.approx(0.06)
    assert "warm-up thresholds" in alert["flags"] and alert["opened_status"] == "lagging"
    trade = alert["trade"]
    assert alert["actionable"] and trade["text"] == "Buy YES at 0.505" and trade["book_source"] == "read"
    assert trade["shares_at_limit"] == 250.0 and trade["max_limit"] == 0.525 and trade["shares_to_max"] == 650.0
    assert trade["edge_per_share"] == pytest.approx(0.05) and trade["uncertainty"] == pytest.approx(0.02)
    assert world.feed.book_calls == ["1104"]
    assert world.w.summary()["actionable_ids"] == [aid]
    world.run_to(T + 2400)
    assert world.kinds() == [("opened", aid), ("status", aid), ("closed", aid)]
    (alert,) = world.alerts()
    assert alert["state"] == "closed" and alert["status"] == "already_moved" and alert["lag"]["outcome"] == "followed"
    assert alert["lag"]["followed_at"] == T + 600 and alert["lag"]["lag_s"] == pytest.approx(307.5)
    assert alert["lag"]["lag_after_alert_s"] == 285.0 and alert["closed_at"] == T + 630
    assert alert["lag"]["capture_4m"] == pytest.approx(0.05)  # 0.56 - 0.50 - 0.01 at detection + 4 min
    assert alert["lag"]["exit_30m"] == pytest.approx(0.535 - 0.505)  # captures keep arriving after the close
    assert not alert["actionable"] and alert["trade"] is None
    # the stored (closed) alert still says what the opening suggested: a replay prints the same block
    assert alert["opened_trade"]["text"] == "Buy YES at 0.505" and alert["opened_trade"]["shares_at_limit"] == 250.0
    stored = world.persistence.move_alerts()[0]
    replay = mv.format_event_lines(mv.MoveEvent(kind="opened", at=stored["detected_at"], alert_id=aid,
                                                status=stored["status"], lag_outcome="followed", alert=stored))
    live = mv.format_event_lines(world.events[0])
    assert replay == live and replay[0].startswith("[moves 14:05:15] CUP LAGGING  North Carolina Senate (D)")
    assert replay[2].startswith("    suggestion (not a sure thing): Buy YES at 0.505, 250 shares at that price")
    # book reads every 120 s while lagging only, never once the alert stopped lagging
    assert world.feed.book_calls == ["1104", "1104", "1104"]
    # alerts.jsonl: one line per event, each a JSON object with the alert
    lines = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["event"] for x in lines] == ["opened", "status", "closed"]
    first = json.loads(lines[0])
    assert first["at"] == T + 315 and first["at_iso"] == "2026-10-05T14:05:15Z" and first["alert"]["alert_id"] == aid
    assert first["alert"]["status"] == "lagging"
    # the lock rule: no provider, Cup feed or persistence call ever saw the watcher lock held
    assert not any(v for venue in world.venues for v in venue.locked_seen)
    assert not any(world.feed.locked_seen)


def test_watcher_status_published_dicts_listeners_and_notification_text() -> None:
    outside, cup = lead(cup_at_=None)
    world = World(outside=outside, cup=cup)
    seen: List[Tuple[bool, Dict[str, Any], Dict[str, Any]]] = []

    def listener(event: mv.MoveEvent) -> None:  # called outside the lock: reading the published dicts never blocks
        seen.append((world.w._lock.locked(), world.w.summary(), world.w.status()))

    world.w.add_listener(listener)
    world.run_to(T + 300)
    before_summary, before_status = world.w.summary(), world.w.status()
    frozen = copy.deepcopy(before_summary), copy.deepcopy(before_status)
    world.run_to(T + 345)
    assert world.w.summary() is not before_summary and world.w.status() is not before_status
    assert (before_summary, before_status) == frozen  # replaced, never mutated
    assert seen and not any(locked for locked, _s, _t in seen)
    status = world.w.status()
    assert set(status) == {"enabled", "poll_s", "last_step_at", "steps", "open", "actionable", "actionable_alerts",
                           "venues", "last_error"}
    assert status["enabled"] and status["poll_s"] == 15.0 and status["steps"] == 24 and status["last_step_at"] == T + 345
    assert status["open"] == 1 and status["actionable"] == 1 and status["venues"] == {"ok": 2, "total": 2}
    (note,) = status["actionable_alerts"]
    assert note["headline"] == "Cup lagging: North Carolina Senate (D)"
    assert note["body"] == ("Outside +6.0 pts in 1 min (0.500 → 0.560); Cup 0.500. Suggestion: Buy YES at 0.505. "
                            "Not a sure thing.")
    assert set(note) == {"alert_id", "exchange_id", "headline", "body", "detected_at"}
    summary = world.w.summary()
    assert set(summary) == {"poll_s", "last_step_at", "steps", "watching", "venues", "counts", "actionable_ids", "alerts",
                            "suppressed", "summary", "thresholds", "caveats"}
    assert summary["watching"] == {"outcomes": 1, "matched": 1, "venues": 2}
    assert summary["counts"]["open"] == 1 and summary["counts"]["lagging"] == 1 and summary["counts"]["today"] == 1
    assert set(summary["counts"]["suppressed_today"]) == set(mv.SUPPRESS_REASONS)
    assert summary["thresholds"]["abs_min"] == {"60": 0.03, "300": 0.04, "900": 0.05, "3600": 0.07}
    assert summary["caveats"] == list(mv.MOVES_CAVEATS)
    v0 = summary["venues"][0]
    assert v0["venue"] == "polymarket" and v0["status"] == "ok" and v0["matched"] == 1 and v0["budget_limit"] == 45
    assert world.w.open_alert_for("1104") == {
        "alert_id": f"mv-1104-{int(T + 255)}", "status": "lagging", "status_label": "Cup lagging", "state": "open",
        "direction": 1, "move": 0.06, "lag_gap_now": 0.06, "detected_at": T + 315, "actionable": True}
    assert world.w.open_alert_for("999") is None
    assert [a["alert_id"] for a in world.w.alerts(state="open")] == [f"mv-1104-{int(T + 255)}"]
    assert world.w.alerts(state="closed") == [] and world.w.alerts(since=T + 400) == []


def test_watcher_never_raises_for_a_failing_provider_or_listener() -> None:
    outside, cup = lead(cup_at_=None)
    world = World(outside=outside, cup=cup)
    world.venues[1].raise_exc = RuntimeError("boom")
    world.w.add_listener(lambda event: 1 / 0)
    world.run_to(T + 330)
    report = world.reports[-1]
    assert report.polled["kalshi"]["status"] == "error" and report.polled["polymarket"]["status"] == "ok"
    venues = {v["venue"]: v for v in world.w.summary()["venues"]}
    assert venues["kalshi"]["status"] == "error" and "could not be polled (RuntimeError)" in venues["kalshi"]["last_error"]
    (alert,) = world.alerts()  # Polymarket alone: a one-venue alert
    assert alert["confirmation"] == "one venue" and "one venue" in alert["flags"] and alert["venues"] == ["polymarket"]
    assert world.w.status()["venues"] == {"ok": 1, "total": 2}
    world.venues[1].raise_exc = ReadOnlyViolation("POST blocked")
    with pytest.raises(ReadOnlyViolation):
        world.run_to(T + 345)


def test_detection_time_is_after_every_poll_and_samples_are_stamped_on_arrival() -> None:
    outside, cup = lead(cup_at_=None)
    world = World(outside=outside, cup=cup)
    for v in world.venues:
        v.poll_cost_s = 2.0
    world.run_to(T + 315)
    (alert,) = world.alerts()
    # each step: polymarket returns at +2, kalshi at +4; the decision time is the clock after both
    assert alert["detected_at"] == T + 315 + 4.0
    pm = next(vm for vm in alert["venue_moves"] if vm["venue"] == "polymarket")
    ks = next(vm for vm in alert["venue_moves"] if vm["venue"] == "kalshi")
    assert pm["base_ts"] == T + 255 + 2.0 and ks["base_ts"] == T + 255 + 4.0
    assert alert["t_base"] == T + 259.0
    first = {r["venue"]: r["ts"] for r in world.persistence.outside_quotes(T, T + 4)}
    assert first == {"polymarket": T + 2.0, "kalshi": T + 4.0}


def test_debounce_one_alert_per_move_peak_growth_and_re_arm() -> None:
    def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
        return {"v": round(0.50 + ramp(t, T + 300, T + 330, 0.08) + ramp(t, T + 420, T + 450, 0.03), 6)}
    world = World(outside=outside)
    world.run_to(T + 600)  # 20 steps after the move
    opened = [k for k, _a in world.kinds() if k == "opened"]
    assert opened == ["opened"]
    (alert,) = world.alerts()
    assert alert["windows"] == [60.0, 300.0] and alert["window_s"] == 60.0 and alert["detected_at"] == T + 345
    assert alert["t_base"] == T + 285 and alert["outside_before"] == 0.5  # the base never changes
    assert alert["move"] == pytest.approx(0.08) and alert["peak_move"] == pytest.approx(0.11)
    assert alert["grown_at"] == T + 465  # the step the last growth was confirmed (two samples at the new level)
    world.run_to(T + 1200)  # the 15-min window now sees the same move: the same alert gains it
    (alert,) = world.alerts()
    assert alert["windows"] == [60.0, 300.0, 900.0] and alert["t_base"] == T + 285
    world.run_to(T + 4000)  # Cup never follows: not followed after 60 min, then the 1-h window would see the old move
    assert [k for k, _a in world.kinds()] == ["opened", "status", "closed"]
    (alert,) = world.alerts()
    assert alert["lag"]["outcome"] == "not_followed" and alert["state"] == "closed"


def test_re_arm_a_distinct_later_move_opens_a_second_alert() -> None:
    def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
        return {"v": round(0.50 + ramp(t, T + 300, T + 315, 0.06) + ramp(t, T + 1500, T + 1515, 0.06), 6)}

    def cup(eid: str, t: float) -> Tuple[float, float]:
        off = 0.04 if t >= T + 600 else 0.0
        return round(0.495 + off, 6), round(0.505 + off, 6)
    world = World(outside=outside, cup=cup).run_to(T + 1800)
    ids = [a for k, a in world.kinds() if k == "opened"]
    assert ids == [f"mv-1104-{int(T + 270)}", f"mv-1104-{int(T + 1470)}"]


def test_opposite_trigger_after_a_reversion_and_linking_two_legs() -> None:
    def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
        up = ramp(t, T + 300, T + 315, 0.06) - ramp(t, T + 600, T + 615, 0.06)
        if eid == "1105":  # the Republican leg mirrors the Democratic one
            return {"v": round(0.50 - up, 6)}
        return {"v": round(0.50 + up, 6)}
    world = World(outside=outside, targets=[target("1104"), target("1105", race=NC_R, market_id="553")])
    world.run_to(T + 330)
    a, b = sorted(world.alerts(), key=lambda x: x["exchange_id"])
    assert a["linked"] == [b["alert_id"]] and b["linked"] == [a["alert_id"]] and b["direction"] == -1
    world.run_to(T + 700)
    kinds = world.kinds("1104")
    assert ("status", a["alert_id"]) in kinds and ("closed", a["alert_id"]) in kinds
    first = next(x for x in world.w.alerts() if x["alert_id"] == a["alert_id"])
    assert first["lag"]["outcome"] == "reverted" and first["status"] == "reverted"
    back = [x for x in world.w.alerts() if x["exchange_id"] == "1104" and x["alert_id"] != a["alert_id"]]
    assert len(back) == 1 and back[0]["direction"] == -1 and back[0]["t_base"] >= first["grown_at"]
    assert back[0]["status"] == "already_moved" and back[0]["lag"]["outcome"] == "excluded"  # converging back to the Cup


# --------------------------------------------------------------------------- 3b. filters through the watcher


def one_venue(world_kw: Dict[str, Any], venue_spec: Callable[[float], Optional[Dict[str, Any]]],
              until: float = T + 360) -> World:
    world = World(venues=("polymarket",), outside=lambda venue, eid, t: venue_spec(t), **world_kw)
    return world.run_to(until)


def moved(t: float, **kw: Any) -> Dict[str, Any]:
    return dict({"v": 0.56 if t >= T + 300 else 0.50}, **kw)


def reasons(world: World) -> Dict[str, str]:
    return {s["reason"]: s["detail"] for s in world.suppressed()}


def test_filters_match_near_thin_stale_and_their_sentences() -> None:
    w = one_venue({}, lambda t: moved(t, conf=0.75))
    assert not w.alerts() and reasons(w) == {"match": "Polymarket match with confidence 0.75: too uncertain to alert on."}
    w = one_venue({}, lambda t: moved(t, kind="NEAR"))
    assert not w.alerts() and reasons(w) == {"near": "Near match (different settlement wording): not alerted."}
    w = one_venue({"targets": [target(trade_near=True)]}, lambda t: moved(t, kind="NEAR"))
    assert len(w.alerts()) == 1  # trade_near: a NEAR match may alert
    w = one_venue({}, lambda t: moved(t, spread=0.06))
    assert not w.alerts() and reasons(w) == {"thin": "Polymarket's book was 6 pts wide during the move."}
    w = one_venue({}, lambda t: moved(t, liquidity=4000.0))
    assert reasons(w) == {"thin": "Polymarket had only $4,000 of liquidity during the move."}
    w = World(venues=("kalshi",), outside=lambda venue, eid, t: moved(t, bid_size=50.0, ask_size=900.0)).run_to(T + 360)
    assert reasons(w) == {"thin": "Kalshi had only 50 contracts at the touch during the move."}
    w = one_venue({}, lambda t: None if T + 225 < t < T + 300 else moved(t))  # not read for 75 s inside the window
    assert not w.alerts() and reasons(w)["stale"] == "Polymarket was not read for 1.2 min inside this window."
    counts = w.w.summary()["counts"]["suppressed_today"]
    assert counts["stale"] == 1 and sum(counts.values()) == 1  # the same (outcome, reason) counts once


def test_filters_single_tick_disagree_suspect_confirmed_no_cup_and_placeholder() -> None:
    w = World(outside=lambda venue, eid, t: {"v": 0.59 if (venue == "polymarket" and t == T + 300) else 0.50}).run_to(T + 400)
    assert not w.alerts() and reasons(w) == {"single_tick": "One print of +9.0 pts that did not hold."}
    w = World(outside=lambda venue, eid, t: moved(t) if venue == "polymarket" else {"v": 0.50}).run_to(T + 400)
    assert not w.alerts() and reasons(w) == {"disagree": "Polymarket +6.0 pts, Kalshi +0.0 pts: the venues disagree."}
    far = {"outside": lambda venue, eid, t: {"v": 0.86 if t >= T + 300 else 0.80}, "cup": lambda eid, t: (0.415, 0.425)}
    w = World(**far).run_to(T + 400)
    assert not w.alerts() and reasons(w) == {"suspect": "Outside 0.86 vs Cup 0.42: check the match."}
    w = World(targets=[target(confirmed=True)], **far).run_to(T + 400)
    assert len(w.alerts()) == 1  # the user confirmed the match: the suspect-gap guard is off
    # no Cup price until T+330 (no_cup), then the venue's quote turns one-sided (placeholder): never an alert
    w = World(venues=("polymarket",),
              outside=lambda venue, eid, t: ({"bid": None, "ask": 0.57} if t >= T + 330 else moved(t)),
              cup=lambda eid, t: None if t < T + 330 else (0.495, 0.505)).run_to(T + 400)
    assert not w.alerts()
    assert reasons(w) == {"no_cup": "No Cup price in the last 2 minutes to compare with.",
                          "placeholder": "Polymarket's quote turned one-sided: not a price."}
    assert [s["reason"] for s in w.suppressed()] == ["placeholder", "no_cup"]  # newest first
    first = w.suppressed()[1]
    assert first["race_label"] == "North Carolina Senate (D)" and first["move"] == pytest.approx(0.06)
    assert first["reason_label"] == mv.SUPPRESS_LABELS["no_cup"] and first["window_s"] == 60.0


def test_censored_when_the_outcome_leaves_the_targets_or_the_watcher_stalls() -> None:
    outside, cup = lead(cup_at_=None)
    world = World(outside=outside, cup=cup).run_to(T + 330)
    world.fv._targets = []  # nothing to watch at all (e.g. the market list failed): nothing is censored
    world.run_to(T + 360)
    assert world.alerts()[0]["lag"]["outcome"] == "pending"
    world.fv._targets = [target("777", title="Another")]  # the outcome closed: it left the targets
    world.targets = list(world.fv._targets)
    world.run_to(T + 375)
    (alert,) = world.w.alerts()
    assert alert["lag"]["outcome"] == "censored" and alert["state"] == "closed"
    assert alert["lag"]["reason"] == "The market closed before the Cup followed."
    stall = World(outside=outside, cup=cup).run_to(T + 330)
    stall.last = T + 330 + 1500 - 15  # the next step comes 25 min later
    stall.run_to(T + 330 + 1500)
    alert = stall.w.alerts()[0]
    assert alert["lag"]["outcome"] == "censored" and alert["state"] == "closed"
    assert alert["lag"]["reason"] == "The bot was not running for 25 min while this was measured."
    assert [k for k, _a in stall.kinds()] == ["opened", "status", "closed"]


# --------------------------------------------------------------------------- 10. persistence and restart


def test_memory_persistence_semantics() -> None:
    p = mv.MemoryMovesPersistence()
    row = sample(T, 0.5, liquidity=1e5).to_dict()
    assert p.add_outside_quotes([row, dict(row, bid=0.48), dict(row, ts=T + 15, exchange_id="9")]) == 3
    got = p.outside_quotes(T, T)
    assert len(got) == 1 and got[0]["bid"] == 0.48  # upsert by (exchange_id, venue, ts)
    assert [r["ts"] for r in p.outside_quotes(T)] == [T, T + 15]
    assert [r["exchange_id"] for r in p.outside_quotes(T, exchange_ids=["9"])] == ["9"]
    got[0]["bid"] = 0.0
    assert p.outside_quotes(T, T)[0]["bid"] == 0.48  # copies out
    alerts = [{"alert_id": f"a{i}", "detected_at": T + i, "state": "open" if i % 2 else "closed", "windows": (60.0,)}
              for i in range(5)]
    assert p.put_move_alerts(alerts + [{"detected_at": T}]) == 5  # a row without an id is skipped
    assert [a["alert_id"] for a in p.move_alerts()] == ["a0", "a1", "a2", "a3", "a4"]
    assert [a["alert_id"] for a in p.move_alerts(since=T + 1, until=T + 3)] == ["a1", "a2", "a3"]
    assert [a["alert_id"] for a in p.move_alerts(state="open")] == ["a1", "a3"]
    assert [a["alert_id"] for a in p.move_alerts(limit=2)] == ["a3", "a4"]  # the newest
    assert p.move_alerts()[0]["windows"] == [60.0]  # JSON types, like the store


def test_samples_are_recorded_on_change_and_by_heartbeat() -> None:
    def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
        v = 0.50 + (0.003 if t >= T + 60 else 0.0) + (0.005 if t >= T + 120 else 0.0)
        return {"v": round(v, 6), "spread": 0.02 if t >= T + 700 else 0.01}
    world = World(venues=("polymarket",), outside=outside).run_to(T + 900)
    stored = [r["ts"] - T for r in world.persistence.outside_quotes(T)]
    # first sample, the >= 0.005 change at 120 (the 0.003 at 60 is below a tick), the heartbeat at 420, the spread
    # change first polled at 705 (bid and ask moved by 0.005)
    assert stored == [0.0, 120.0, 420.0, 705.0]


def test_restart_within_600_s_continues_the_same_alert(tmp_path: Path) -> None:
    outside, cup = lead(cup_at_=T + 900)
    path = tmp_path / "alerts.jsonl"
    store = mv.MemoryMovesPersistence()
    first = World(outside=outside, cup=cup, persistence=store, alerts_path=path).run_to(T + 360)
    (alert,) = first.alerts()
    first.w.close()
    assert first.w.step(T + 375).errors == ["The outside-move watcher is closed."]
    text = path.read_text(encoding="utf-8")
    again = World(outside=outside, cup=cup, persistence=store, alerts_path=path, start=T + 600)
    again.history_from = T
    again.run_to(T + 1300)
    assert path.read_text(encoding="utf-8").startswith(text)  # appended, never rewritten
    events = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert [(e["event"], e["alert"]["alert_id"]) for e in events] == [
        ("opened", alert["alert_id"]), ("status", alert["alert_id"]), ("closed", alert["alert_id"])]
    (after,) = again.w.alerts()
    assert after["alert_id"] == alert["alert_id"] and after["t_base"] == alert["t_base"]
    assert after["outside_before"] == alert["outside_before"] and after["detected_at"] == alert["detected_at"]
    assert after["lag"]["outcome"] == "followed" and after["lag"]["followed_at"] == T + 900
    assert again.feed.history_calls == 1
    assert [k for k, _a in again.kinds()] == ["status", "closed"]  # never a second "opened"


def test_restart_after_more_than_600_s_censors_and_never_reopens(tmp_path: Path) -> None:
    outside, cup = lead(cup_at_=None)
    store = mv.MemoryMovesPersistence()
    path = tmp_path / "alerts.jsonl"
    first = World(outside=outside, cup=cup, persistence=store, alerts_path=path).run_to(T + 360)
    aid = first.alerts()[0]["alert_id"]
    again = World(outside=outside, cup=cup, persistence=store, alerts_path=path, start=T + 360 + 900)
    again.run_to(T + 360 + 1200)
    (alert,) = again.w.alerts()
    assert alert["alert_id"] == aid and alert["state"] == "closed" and alert["lag"]["outcome"] == "censored"
    assert alert["lag"]["reason"] == "The bot was not running for 15 min while this was measured."
    assert again.kinds() == [("status", aid), ("closed", aid)]
    stored = store.move_alerts()
    assert len(stored) == 1 and stored[0]["state"] == "closed"
    assert mv.MoveAlert.from_dict(stored[0]).to_dict() == stored[0]  # the stored dict round-trips


@pytest.mark.parametrize("downtime, alerted", [(30.0, True), (90.0, False)])
def test_restored_samples_are_not_a_gap_but_a_real_downtime_is(downtime: float, alerted: bool) -> None:
    """Restored series are sparse by design (a sample is stored on a one-tick change or the 300-s heartbeat): their
    spacing is not an interruption. The bot's own downtime after the restart is (here 90 s > 60 s)."""
    def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
        return {"v": round(0.50 + ramp(t, T + 1830, T + 2550, 0.08), 6)}  # 8 pts in 12 min: only the 15-min window
    store = mv.MemoryMovesPersistence()
    World(outside=outside, persistence=store).run_to(T + 1800)
    assert [r["ts"] - T for r in store.outside_quotes(T) if r["venue"] == "polymarket"] == [0, 300, 600, 900, 1200,
                                                                                            1500, 1800]
    again = World(outside=outside, persistence=store, start=T + 1800 + downtime)
    again.run_to(T + 2580)
    if alerted:
        (alert,) = again.alerts()
        assert alert["window_s"] == 900.0 and alert["t_base"] == T + 1500  # a restored base
    else:
        assert not again.alerts()
        assert reasons(again)["stale"] == "Polymarket was not read for 1.5 min inside this window."


def test_an_alerts_file_that_cannot_be_written_is_reported_and_tracking_continues(tmp_path: Path) -> None:
    outside, cup = lead(cup_at_=None)
    bad = tmp_path / "is-a-directory"
    bad.mkdir()
    world = World(outside=outside, cup=cup, alerts_path=bad).run_to(T + 330)
    assert len(world.alerts()) == 1
    assert "could not be written" in (world.w.status()["last_error"] or "")
    assert world.reports[-1].errors and "could not be written" in world.reports[-1].errors[-1]


# --------------------------------------------------------------------------- 12. determinism and CLI text


def test_two_identical_runs_publish_identical_summaries() -> None:
    def run() -> str:
        def outside(venue: str, eid: str, t: float) -> Dict[str, Any]:
            jitter = 0.005 if (venue == "kalshi" and int(t // 15) % 4 == 0) else 0.0
            return {"v": round(0.50 + ramp(t, T + 300, T + 330, 0.07) + jitter, 6)}
        outside_cup = lead(cup_at_=T + 700)[1]
        w = World(outside=outside, cup=outside_cup, demo=True).run_to(T + 2400)
        assert w.w.summary()["caveats"][-1] == mv.MOVES_DEMO_CAVEAT and w.alerts()[0]["demo"] is True
        return json.dumps(w.w.summary(), sort_keys=True)
    assert run() == run()


def example_event(kind: str, alert: mv.MoveAlert, at: float) -> mv.MoveEvent:
    d = alert.to_dict()
    return mv.MoveEvent(kind=kind, at=at, alert_id=alert.alert_id, status=alert.status, lag_outcome=alert.lag.outcome,
                        alert=d)


def test_format_event_lines_exact_text_of_17_2() -> None:
    cup = flat_cup(T - 1200, hms(14, 5, 0))
    alert = example_l_alert(cup)
    alert.trade, _ = mv.hand_trade(1, [0.58, 0.565], 0.02, alert.cup.now,
                                   book(hms(14, 5, 13), [(0.505, 200)], [(0.52, 300), (0.525, 450), (0.53, 800)]), "read",
                                   hms(14, 5, 15), P)
    alert.actionable = True
    lines = mv.format_event_lines(example_event("opened", alert, hms(14, 5, 15)))
    assert lines == [
        "[moves 14:05:15] CUP LAGGING  North Carolina Senate (D)  Will the Democratic Party win the North Carolina Senate?",
        "    outside 0.517 -> 0.573 (+5.5 pts in 5 min; Polymarket, Kalshi); Cup 0.512 (bid 0.505 / ask 0.520), +0.0 pts; "
        "lag gap 5.5 pts",
        "    suggestion (not a sure thing): Buy YES at 0.520, 300 shares at that price (750 up to 0.525); +0.037/share net "
        "of the spread, +0.018 after the outside price's uncertainty",
    ]
    belled = mv.format_event_lines(example_event("opened", alert, hms(14, 5, 15)), bell=True)
    assert belled[0].endswith("\a") and belled[1:] == lines[1:]
    alert.actionable = False
    assert not mv.format_event_lines(example_event("opened", alert, hms(14, 5, 15)), bell=True)[0].endswith("\a")
    no_book = copy.deepcopy(alert)
    no_book.trade = dataclasses.replace(alert.trade, shares_at_limit=None, shares_to_max=None)
    assert "shares at that price unknown (no recent Cup order book)" in mv.format_event_lines(
        example_event("opened", no_book, hms(14, 5, 15)))[2]
    no_trade = copy.deepcopy(alert)
    no_trade.trade, no_trade.trade_note = None, "The Cup's ask (0.535) leaves less than 1 cent per share."
    assert mv.format_event_lines(example_event("opened", no_trade, hms(14, 5, 15)))[2] == (
        "    no trade suggested: The Cup's ask (0.535) leaves less than 1 cent per share.")
    mv.update_lag(alert, follow_series(), [], hms(14, 7, 30), P)
    assert mv.format_event_lines(example_event("status", alert, hms(14, 7, 30))) == [
        "[moves 14:07:30] FOLLOWED  North Carolina Senate (D)  Will the Democratic Party win the North Carolina Senate?",
        "    the Cup moved +3.2 pts, 4.3 min after the outside move (1.8 min after the alert)",
    ]
    assert mv.format_event_lines(example_event("closed", alert, hms(14, 12, 30))) == []
    # the other tags (an alert's reason / lag reason sentence, first letter lower-cased, final period dropped)
    for kind, status, outcome, reason, tag, detail in [
        ("opened", "moved_first", "cup_first",
         "The Cup moved +4.5 pts about 3.8 min before the outside price: the outside followed the Cup, not a lag.",
         "CUP MOVED FIRST", "the Cup moved +4.5 pts about 3.8 min before the outside price: the outside followed the Cup, "
                            "not a lag"),
        ("opened", "already_moved", "pending", "The Cup has already moved -3.0 of the 6.0 pts (now 0.550): no edge left "
                                               "to chase.",
         "CUP ALREADY MOVED", "the Cup has already moved -3.0 of the 6.0 pts (now 0.550): no edge left to chase"),
        ("status", "reverted", "reverted", "The outside price came back 4.5 of its 6.0 pts before the Cup moved.",
         "REVERTED", "the outside price came back 4.5 of its 6.0 pts before the Cup moved"),
        ("status", "lagging", "not_followed", "The Cup did not follow within 60 min; the gap is 7.0 pts now.",
         "NOT FOLLOWED", "the Cup did not follow within 60 min; the gap is 7.0 pts now"),
        ("status", "lagging", "censored", "The bot was not running for 25 min while this was measured.",
         "CENSORED", "the bot was not running for 25 min while this was measured"),
    ]:
        a = copy.deepcopy(alert)
        a.status, a.opened_status, a.lag.outcome = status, status, outcome
        a.reason = a.lag.reason = reason
        assert mv.format_event_lines(example_event(kind, a, hms(15, 20))) == [
            f"[moves 15:20:00] {tag}  North Carolina Senate (D)  Will the Democratic Party win the North Carolina Senate?",
            f"    {detail}"]
    opt = copy.deepcopy(alert)
    opt.option = "Chris Pappas"
    assert mv.format_event_lines(example_event("status", opt, hms(14, 7, 30)))[0].endswith(
        "Will the Democratic Party win the North Carolina Senate? — Chris Pappas")


def test_summary_lines_exact_text() -> None:
    venues = [mv.VenueStatus(venue="polymarket", label="Polymarket", status="ok", matched=231, budget_used=25,
                             budget_limit=45).to_dict(),
              mv.VenueStatus(venue="kalshi", label="Kalshi", status="offline",
                             last_error="Kalshi is unreachable from this machine (could not connect): no outside fair "
                                        "value from Kalshi.").to_dict()]
    body = {"summary": mv.summarise([], T).to_dict(),
            "counts": {"suppressed_today": dict({r: 0 for r in mv.SUPPRESS_REASONS}, thin=3, disagree=1)},
            "caveats": list(mv.MOVES_CAVEATS) + [mv.MOVES_DEMO_CAVEAT]}
    lines = mv.summary_lines(body, venues, hms(14, 20))
    assert lines == [
        "[moves 14:20:00] summary: No outside move has been measured yet.",
        "    venues: Polymarket ok (231 matched, 25/45 reads a minute); Kalshi offline (Kalshi is unreachable from this "
        "machine (could not connect): no outside fair value from Kalshi.)",
        "    filtered out today: 3 thin or wide outside book, 1 the other venue did not move",
    ]
    final = mv.summary_lines(body, venues, hms(14, 20), final=True)
    assert final[:3] == lines and final[3:] == [f"  - {c}" for c in body["caveats"]]
    live = mv.summary_lines(body, venues, hms(14, 20), final=True, demo=False)
    assert f"  - {mv.MOVES_DEMO_CAVEAT}" not in live
    assert mv.summary_lines({}, [], hms(14, 20))[1:] == ["    venues: none configured", "    filtered out today: nothing"]


def test_formatting_rules() -> None:
    assert (mv.fmt_pts(0.055), mv.fmt_pts(-0.03), mv.fmt_pts(0.055, signed=False), mv.fmt_pts(None)) == (
        "+5.5 pts", "-3.0 pts", "5.5 pts", "n/a")
    # plain float formatting (the exact binary value, as JavaScript's toFixed does): 0.0075 -> +0.007
    assert (mv.fmt_price(0.5125), mv.fmt_money(0.0075), mv.fmt_money(-0.002), mv.fmt_money(None)) == (
        "0.512", "+0.007", "-0.002", "n/a")
    assert [mv.fmt_duration(s) for s in (40, 210, 670, 5400, None)] == ["40 s", "3.5 min", "11.2 min", "1.5 h", "n/a"]
    assert [mv.window_text(w) for w in (60, 300, 900, 3600)] == ["1 min", "5 min", "15 min", "1 h"]
    assert mv.fmt_share(19 / 46) == "41%" and mv.fmt_share(None) == "n/a"
    assert mv.race_label(NC) == "North Carolina Senate (D)" and mv.race_label(None, "A title") == "A title"


# --------------------------------------------------------------------------- 11. poll against the fixtures

T0 = 1791144000.0  # 2026-10-04T20:00:00Z, the external fixtures' snapshot time
GAMMA = "gamma-api.polymarket.com"
KALSHI_HOSTS = ("api.elections.kalshi.com", "external-api.kalshi.com")
NH, ME, NCK = "2026:SENATE:NH", "2026:SENATE:ME", "2026:SENATE:NC"


def _req_key(url: httpx.URL) -> Tuple[str, str, Tuple[Tuple[str, str], ...]]:
    """D57: host family, path and the query as a multiset (``limit`` ignored, comma lists as sets)."""
    items = []
    for name, value in url.params.multi_items():
        if name == "limit":
            continue
        if name == "tickers":
            value = ",".join(sorted(value.split(",")))
        items.append((name, value))
    host = "kalshi" if url.host in KALSHI_HOSTS else url.host
    return host, url.path, tuple(sorted(items))


class SeqTransport:
    """Serves the fixture answers IN ORDER for the same request (the last one repeats); 404 for anything else. Every
    request is recorded; ``before`` runs inside the handler (e.g. to advance a fake clock: the response arrives
    later), ``fail`` may raise a transport error."""

    def __init__(self, *files: str) -> None:
        self.queues: Dict[Any, List[Tuple[int, Any, Dict[str, str]]]] = {}
        self.pos: Dict[Any, int] = {}
        self.calls: List[httpx.Request] = []
        self.before: Optional[Callable[[httpx.Request], None]] = None
        self.fail: Optional[Callable[[httpx.Request], Optional[BaseException]]] = None
        self.dynamic: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None
        for name in files:
            self.add_file(name)

    def add_file(self, name: str) -> None:
        for r in json.loads((FIXTURES / name).read_text(encoding="utf-8"))["responses"]:
            self.push(r["request"], r["status"], r["body"], r.get("headers") or {})

    def push(self, request: str, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        method, url = request.split(" ", 1)
        assert method == "GET"
        self.queues.setdefault(_req_key(httpx.URL(url)), []).append((status, body, dict(headers or {})))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.before is not None:
            self.before(request)
        if self.fail is not None:
            exc = self.fail(request)
            if exc is not None:
                raise exc
        if self.dynamic is not None:
            got = self.dynamic(request)
            if got is not None:
                return got
        key = _req_key(request.url)
        queue = self.queues.get(key)
        if not queue:
            return httpx.Response(404, json={"error": f"no fixture for {request.url}"})
        i = self.pos.get(key, 0)
        self.pos[key] = i + 1
        status, body, headers = queue[min(i, len(queue) - 1)]
        return httpx.Response(status, json=body, headers=headers)

    @property
    def mock(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def assert_anonymous_gets(*transports: SeqTransport) -> None:
    calls = [c for t in transports for c in t.calls]
    assert calls
    for request in calls:
        assert request.method == "GET"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.url.host in (GAMMA,) + KALSHI_HOSTS


def _cup_rows() -> List[Dict[str, str]]:
    lines = [line for line in (FIXTURES / "supermarket_titles.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


def cup_targets(*race_keys: str) -> List[MatchTarget]:
    infos = [ExchangeInfo(exchange_id=r["sig_exchange_id"], market_id=r["sig_market_id"], option="YES",
                          market_title=r["title"]) for r in _cup_rows() if r["race_key"] in race_keys]
    return fv.build_targets(infos, fv.races_for(infos, None), None)


def providers(clock: Clock, pm_t: SeqTransport, ks_t: SeqTransport, **kw: Any) -> Tuple[fv.PolymarketProvider, fv.KalshiProvider]:
    pm = fv.PolymarketProvider(transport=pm_t.mock, clock=clock, sleep=lambda s: None, **kw)
    ks = fv.KalshiProvider(transport=ks_t.mock, clock=clock, sleep=lambda s: None, **kw)
    return pm, ks


def test_poll_is_pending_before_validation_then_reads_only_validated_matches() -> None:
    clock = Clock(T0)
    pm_t, ks_t = SeqTransport("moves_polymarket.json"), SeqTransport("moves_kalshi.json")
    pm, ks = providers(clock, pm_t, ks_t)
    targets = cup_targets(NH, ME, NCK)
    for p, label in ((pm, "Polymarket"), (ks, "Kalshi")):
        res = p.poll(targets, T0)
        assert (res.status, res.requests, res.errors) == (
            "pending", 0, [f"No validated {label} match yet: waiting for the first fair-value refresh."])
    assert not pm_t.calls and not ks_t.calls
    assert pm.refresh(targets, T0).status == "ok" and ks.refresh(targets, T0).status == "ok"
    clock.set(T0 + 15)
    res = pm.poll(targets, T0 + 15)
    assert res.status == "ok" and res.requests == 1 and sorted(res.quotes) == ["1070", "1071", "959", "960", "966", "967"]
    q = res.quotes["966"]
    assert (q.bid, q.ask, q.fetched_at, q.match_kind) == (0.515, 0.525, T0 + 15, "EXACT")
    assert q.venue_updated_at is not None and q.venue_updated_at < T0  # the venue's updatedAt stays informational
    assert res.matches["966"].external_id == "630883" and res.liquidity["966"] == 210000.0
    assert pm_t.calls[-1].url.params["limit"] == "6" and pm_t.calls[-1].url.params.get_list("id") == [
        m.external_id for m in pm._poll_matches(targets).values()]
    # a polled quote equals a refreshed one for the same JSON (the provider's own _quote)
    fresh = pm._quote(pm_t.queues[_req_key(pm_t.calls[-1].url)][1][1][4], pm._matches["966"], T0 + 15)
    assert fresh.to_dict() == q.to_dict()
    res = ks.poll(targets, T0 + 15)
    assert res.status == "ok" and res.quotes["966"].bid_size == 900.0 and res.quotes["966"].ask_size == 1400.0
    assert res.liquidity == {} and ks_t.calls[-1].url.params["limit"] == "100"
    # only validated matches: a new target, or a pin changed since the refresh, is not polled
    extra = cup_targets("2026:SENATE:GA")[:1]
    changed = [dataclasses.replace(t, pins=dict(t.pins, polymarket="999999")) if t.exchange_id == "966" else t
               for t in targets]
    res = pm.poll(changed + extra, T0 + 30)
    assert "966" not in res.quotes and extra[0].exchange_id not in res.quotes
    assert "630883" not in pm_t.calls[-1].url.params.get_list("id") and len(pm_t.calls[-1].url.params.get_list("id")) == 5
    disabled = [dataclasses.replace(t, disabled=True) if t.exchange_id == "1070" else t for t in targets]
    assert "1070" not in pm.poll(disabled, T0 + 45).quotes
    assert_anonymous_gets(pm_t, ks_t)
    assert set(pm._limiters) == {GAMMA} and set(ks._limiters) == {"api.elections.kalshi.com"}  # never the Cup's


def test_poll_light_checks_missing_ids_and_liquidity() -> None:
    clock = Clock(T0)
    pm_t, ks_t = SeqTransport("moves_polymarket.json"), SeqTransport("moves_kalshi.json")
    pm, ks = providers(clock, pm_t, ks_t)
    targets = cup_targets(NH, ME, NCK)
    pm.refresh(targets, T0)
    ks.refresh(targets, T0)
    for k in range(1, 5):
        pm.poll(targets, T0 + 15 * k)
        ks.poll(targets, T0 + 15 * k)
    res = pm.poll(targets, T0 + 75)  # answer 5: ME (D) closed, NH (R) not returned
    assert res.status == "ok" and res.rejected == {"959": "Polymarket market 630772 is closed: not polled."}
    assert "959" not in res.quotes and "1071" not in res.quotes and "1071" not in res.rejected
    assert "959" in pm._matches  # the match is kept: the next refresh decides
    res = ks.poll(targets, T0 + 75)
    assert res.rejected == {"960": "Kalshi ticker SENATEME-26-R is closed, not open: not polled."}
    assert "960" not in res.quotes and "1071" not in res.quotes and "960" in ks._matches
    res = pm.poll(targets, T0 + 90)  # answer 6: liquidityNum as a numeric string
    assert res.liquidity["966"] == 4321.5 and res.quotes["959"].bid == 0.58
    assert ks.poll(targets, T0 + 90).quotes["966"].bid_size == 20.0
    inactive = {"id": "630844", "active": False, "closed": False, "outcomes": "[\"Yes\", \"No\"]"}
    assert pm._poll_reject(inactive, "630844") == "Polymarket market 630844 is not active: not polled."
    odd = dict(inactive, active=True, outcomes="[\"Pappas\", \"Sununu\"]")
    assert pm._poll_reject(odd, "630844").startswith("Polymarket market 630844 has outcomes")


def synthetic_targets(n: int, venue: str) -> List[MatchTarget]:
    out = []
    for i in range(n):
        race = RaceRef(race_key=f"2026:HOUSE:ZZ-{i:02d}", office="HOUSE", state="ZZ", district=f"ZZ-{i:02d}", party="D")
        ext = str(700000 + i) if venue == "polymarket" else f"KXTEST-{i:03d}"
        out.append(MatchTarget(exchange_id=str(5000 + i), market_id=str(4000 + i), title=f"Seat {i}", race=race,
                               pins={venue: ext}))
    return out


def validate_all(provider: Any, targets: Sequence[MatchTarget]) -> None:
    for t in targets:
        ext = next(iter(t.pins.values()))
        provider._matches[t.exchange_id] = VenueMatch(venue=provider.name, exchange_id=t.exchange_id, external_id=ext,
                                                      kind="EXACT", confidence=0.95)


def synthetic_answers(request: httpx.Request) -> Optional[httpx.Response]:
    u = request.url
    if u.host == GAMMA and u.path == "/markets":
        return httpx.Response(200, json=[{"id": i, "active": True, "closed": False, "outcomes": "[\"Yes\", \"No\"]",
                                          "bestBid": 0.40, "bestAsk": 0.42, "liquidityNum": 9000}
                                         for i in u.params.get_list("id")])
    if u.host in KALSHI_HOSTS and u.path.endswith("/markets"):
        return httpx.Response(200, json={"markets": [{"ticker": t, "status": "active", "yes_bid_dollars": "0.4000",
                                                      "yes_ask_dollars": "0.4200", "yes_bid_size_fp": "500.00",
                                                      "yes_ask_size_fp": "500.00"}
                                                     for t in u.params["tickers"].split(",")], "cursor": ""})
    return None


def test_poll_batches_50_ids_and_100_tickers_and_stamps_each_batch_on_arrival() -> None:
    clock = Clock(T0)
    pm_t, ks_t = SeqTransport(), SeqTransport()
    pm_t.dynamic = ks_t.dynamic = synthetic_answers
    pm_t.before = ks_t.before = lambda request: clock.set(clock.t + 1.5)  # each response arrives 1.5 s later
    pm, ks = providers(clock, pm_t, ks_t, reads_per_min=45)
    pm_targets, ks_targets = synthetic_targets(51, "polymarket"), synthetic_targets(101, "kalshi")
    validate_all(pm, pm_targets)
    validate_all(ks, ks_targets)
    res = pm.poll(pm_targets, T0)
    assert res.status == "ok" and res.requests == 2 and len(res.quotes) == 51
    assert [len(c.url.params.get_list("id")) for c in pm_t.calls] == [50, 1]
    assert [c.url.params["limit"] for c in pm_t.calls] == ["50", "1"]  # limit = the number of ids (D57)
    assert {q.fetched_at for e, q in res.quotes.items() if e != "5050"} == {T0 + 1.5}
    assert res.quotes["5050"].fetched_at == T0 + 3.0  # the second batch is stamped when ITS response arrived
    assert res.liquidity["5000"] == 9000.0
    res = ks.poll(ks_targets, clock())
    assert res.requests == 2 and len(res.quotes) == 101
    assert [len(c.url.params["tickers"].split(",")) for c in ks_t.calls] == [100, 1]
    assert [c.url.params["limit"] for c in ks_t.calls] == ["100", "100"]
    assert pm.budget() == {"used": 2, "limit": 45} and ks.budget() == {"used": 2, "limit": 45}
    assert_anonymous_gets(pm_t, ks_t)


def test_poll_deadline_and_headroom_keep_the_refresh_its_room() -> None:
    clock = Clock(T0)
    pm_t = SeqTransport()
    pm_t.dynamic = synthetic_answers
    pm_t.before = lambda request: clock.set(clock.t + 6.0)
    pm = fv.PolymarketProvider(transport=pm_t.mock, clock=clock, sleep=lambda s: None)
    targets = synthetic_targets(120, "polymarket")
    validate_all(pm, targets)
    res = pm.poll(targets, T0, deadline=T0 + mv.MOVES_POLL_DEADLINE_S)
    assert res.status == "partial" and res.requests == 2 and len(res.quotes) == 100  # no third batch after 10 s
    assert res.errors[0] == "Polymarket poll stopped at the 10 s deadline: some outcomes were not read this time."
    # headroom: a batch starts only while >= 9 of the host's minute slots are free
    clock2 = Clock(T0)
    t2 = SeqTransport()
    t2.dynamic = synthetic_answers
    small = fv.PolymarketProvider(transport=t2.mock, clock=clock2, sleep=lambda s: None, reads_per_min=10)
    few = synthetic_targets(3, "polymarket")
    validate_all(small, few)
    assert small.poll(few, T0).status == "ok"  # 10 - 0 free
    assert small.poll(few, T0 + 1).status == "ok"  # 10 - 1 = 9 free
    res = small.poll(few, T0 + 2)  # 10 - 2 = 8 free: kept for the fair-value refresh
    assert res.status == "partial" and res.requests == 0 and len(t2.calls) == 2
    assert res.errors[0] == ("Polymarket's per-minute budget is kept for the fair-value refresh: some outcomes were not "
                             "polled.")
    assert small.budget() == {"used": 2, "limit": 10}


def test_poll_429_retry_after_is_capped_and_shared_with_refresh() -> None:
    clock = Clock(T0)
    targets = cup_targets(NH)
    base_pm = json.loads((FIXTURES / "moves_polymarket.json").read_text(encoding="utf-8"))["responses"][0]["body"]
    base_ks = json.loads((FIXTURES / "moves_kalshi.json").read_text(encoding="utf-8"))["responses"][0]["body"]
    pm_t, ks_t = SeqTransport(), SeqTransport()
    pm_t.push("GET https://gamma-api.polymarket.com/markets?id=630844&id=630845&limit=2", 200,
              [m for m in base_pm if m["id"] in ("630844", "630845")])
    ks_t.push("GET https://api.elections.kalshi.com/trade-api/v2/markets?tickers=SENATENH-26-D,SENATENH-26-R&limit=100", 200,
              {"markets": [m for m in base_ks["markets"] if m["ticker"].startswith("SENATENH")], "cursor": ""})
    pm_t.add_file("rate_limits.json")  # then the 429s: Gamma Retry-After 120, Kalshi Retry-After 900
    ks_t.add_file("rate_limits.json")
    pm, ks = providers(clock, pm_t, ks_t)
    assert pm.refresh(targets, T0).status == "ok" and ks.refresh(targets, T0).status == "ok"
    res = ks.poll(targets, T0 + 15)
    assert res.status == "backoff" and res.next_try_at == T0 + 15 + 600.0  # 900 capped at MAX_RETRY_AFTER_S
    assert res.errors[0] == "Kalshi asked us to slow down (HTTP 429): next try in 600 s."
    res = pm.poll(targets, T0 + 15)
    assert res.status == "backoff" and res.next_try_at == T0 + 15 + 120.0
    calls = len(pm_t.calls), len(ks_t.calls)
    # one host, one backoff: the refresh waits too, and so does the next poll (no request)
    assert pm.refresh(targets, T0 + 60).status == "backoff" and ks.refresh(targets, T0 + 60).status == "backoff"
    assert pm.poll(targets, T0 + 60).status == "backoff" and ks.poll(targets, T0 + 600).status == "backoff"
    assert (len(pm_t.calls), len(ks_t.calls)) == calls
    assert pm.refresh(targets, T0 + 15 + 120.0).requests >= 1  # after the wait the refresh reads again
    assert_anonymous_gets(pm_t, ks_t)


def test_poll_offline_after_the_first_failure_and_the_kalshi_fallback() -> None:
    clock = Clock(T0)
    pm_t = SeqTransport()
    pm_t.dynamic = synthetic_answers
    pm_t.fail = lambda request: httpx.ConnectError("refused", request=request)
    pm = fv.PolymarketProvider(transport=pm_t.mock, clock=clock, sleep=lambda s: None)
    targets = synthetic_targets(60, "polymarket")
    validate_all(pm, targets)
    res = pm.poll(targets, T0)
    assert res.status == "offline" and res.requests == 1 and len(pm_t.calls) == 1  # the first failure ends the poll
    assert res.errors[0] == ("Polymarket is unreachable from this machine (could not connect): no outside fair value "
                             "from Polymarket.")
    early = pm.refresh(targets, T0 + 5)  # no request while waiting; the real cause first (live-4)
    assert early.status == "offline" and early.errors[0] == res.errors[0] and len(pm_t.calls) == 1
    # Kalshi: a connect error on the first host tries the other host once per poll
    ks_t = SeqTransport()
    ks_t.dynamic = synthetic_answers
    ks_t.fail = lambda request: (httpx.ConnectError("dns", request=request)
                                 if request.url.host == "api.elections.kalshi.com" else None)
    ks = fv.KalshiProvider(transport=ks_t.mock, clock=clock, sleep=lambda s: None)
    kt = synthetic_targets(3, "kalshi")
    validate_all(ks, kt)
    res = ks.poll(kt, T0)
    assert res.status == "ok" and len(res.quotes) == 3
    assert [c.url.host for c in ks_t.calls] == ["api.elections.kalshi.com", "external-api.kalshi.com"]
    ks_t.fail = lambda request: httpx.ConnectError("dns", request=request)
    res = ks.poll(kt, T0 + 15)
    assert res.status == "offline" and res.requests == 2  # the other host once, then stop
    # an unexpected exception is an "error" sentence, never raised; ReadOnlyViolation is raised
    bad = SeqTransport()
    bad.fail = lambda request: ValueError("odd")
    broken = fv.PolymarketProvider(transport=bad.mock, clock=clock, sleep=lambda s: None)
    validate_all(broken, targets[:2])
    res = broken.poll(targets[:2], T0)
    assert res.status == "error" and res.errors[0] == ("Polymarket data could not be read (ValueError): no outside "
                                                       "quotes from Polymarket this time.")
    guarded = SeqTransport()
    guarded.fail = lambda request: ReadOnlyViolation("blocked")
    strict = fv.PolymarketProvider(transport=guarded.mock, clock=clock, sleep=lambda s: None)
    validate_all(strict, targets[:2])
    with pytest.raises(ReadOnlyViolation):
        strict.poll(targets[:2], T0)


def test_poll_is_busy_while_a_refresh_holds_the_io_lock() -> None:
    clock = Clock(T0)
    entered, release = threading.Event(), threading.Event()
    pm_t = SeqTransport("moves_polymarket.json")

    def slow(request: httpx.Request) -> None:
        entered.set()
        assert release.wait(10)
    pm_t.before = slow
    pm = fv.PolymarketProvider(transport=pm_t.mock, clock=clock, sleep=lambda s: None)
    pm.poll_lock_wait_s = 0.2
    targets = cup_targets(NH, ME, NCK)
    results: List[ProviderResult] = []
    worker = threading.Thread(target=lambda: results.append(pm.refresh(targets, T0)))
    worker.start()
    try:
        assert entered.wait(10)
        res = pm.poll(targets, T0 + 15)
        assert (res.status, res.requests) == ("busy", 0) and len(pm_t.calls) == 1
    finally:
        release.set()
        worker.join(10)
    assert results and results[0].status == "ok"
    pm_t.before = None
    assert pm.poll(targets, T0 + 30).status == "ok"  # once the refresh returned


def test_watcher_over_the_real_providers_alerts_and_sends_only_anonymous_gets(tmp_path: Path) -> None:
    clock = Clock(T0)
    pm_t, ks_t = SeqTransport("moves_polymarket.json"), SeqTransport("moves_kalshi.json")
    pm, ks = providers(clock, pm_t, ks_t, reads_per_min=fv.OUTSIDE_READS_PER_MIN_WITH_MOVES)
    targets = cup_targets(NH, ME, NCK)
    pm.refresh(targets, T0)  # the fair-value refresh validates the pins (the watcher never does)
    ks.refresh(targets, T0)
    service = FakeFairValues(targets, providers=[pm, ks])
    cups = {"966": (0.515, 0.525), "967": (0.475, 0.485)}

    class Feed:
        def quotes(self) -> Dict[str, CupQuote]:
            ts = math.floor(clock() / 30) * 30
            return {e: CupQuote(ts=ts, bid=b, ask=a, mid=(a + b) / 2, spread=round(a - b, 6)) for e, (b, a) in cups.items()}

        def history(self, seconds: float) -> Dict[str, List[CupQuote]]:
            return {}

        def book(self, eid: str) -> Optional[BookObservation]:
            return None

        def stored_book(self, eid: str) -> Optional[BookObservation]:
            return None
    w = mv.OutsideMoveWatcher(service, cup=Feed(), persistence=mv.MemoryMovesPersistence(),
                              alerts_path=tmp_path / "alerts.jsonl", clock=clock)
    for k in range(1, 8):
        clock.set(T0 + 15 * k)
        w.step()
    alerts = {a["exchange_id"]: a for a in w.alerts()}
    assert set(alerts) == {"966", "967"}  # both NC legs; nothing for NH or ME (flat)
    nc = alerts["966"]
    assert nc["detected_at"] == T0 + 75 and nc["confirmation"] == "two venues" and nc["venues"] == ["polymarket", "kalshi"]
    assert nc["status"] == "lagging" and nc["uncertainty"] == pytest.approx(0.02) and nc["linked"] == [alerts["967"]["alert_id"]]
    assert nc["trade"]["text"] == "Buy YES at 0.525" and nc["trade"]["shares_at_limit"] is None
    assert alerts["967"]["trade"]["text"] == "Buy NO at 0.525"
    venues = {v["venue"]: v for v in w.summary()["venues"]}
    assert venues["polymarket"]["status"] == "ok" and venues["polymarket"]["matched"] == 6
    # the host limiter's last minute (a sliding window): the polls at +60 .. +105 s
    assert venues["polymarket"]["budget_limit"] == 45 and venues["polymarket"]["budget_used"] == 4
    assert len(pm_t.calls) == 8 and len(ks_t.calls) == 8
    assert_anonymous_gets(pm_t, ks_t)


def test_moves_module_is_get_only_by_construction() -> None:
    """The static scan of tests/test_readonly.py covers moves.py with no allowlist entry; this is the same check
    in miniature so a core change fails here first: no write-shaped call, no non-GET method literal."""
    import ast

    tree = ast.parse(Path(mv.__file__).read_text(encoding="utf-8"))
    bad = {"post", "put", "patch", "delete", "send", "stream", "build_request", "mint_realtime_token"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in bad, f"moves.py line {node.lineno}: .{node.func.attr}("
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in ("POST", "PUT", "PATCH", "DELETE") and "/orders" not in node.value


def test_a_failing_step_is_reported_and_the_next_step_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    outside, cup = lead(cup_at_=None)
    world = World(outside=outside, cup=cup).run_to(T + 30)
    real = world.w._compute
    monkeypatch.setattr(world.w, "_compute", lambda *a, **k: (_ for _ in ()).throw(KeyError("bug")))
    report = world.w.step(T + 45)
    assert report.errors == ["The outside-move watcher failed this step (KeyError): alerts may be missing until it "
                             "recovers."]
    assert world.w.status()["last_error"] == report.errors[0]
    monkeypatch.setattr(world.w, "_compute", real)
    world.last = T + 45
    world.run_to(T + 330)
    assert world.w.status()["last_error"] is None and len(world.alerts()) == 1


def test_a_refresh_429_pauses_the_poll_too() -> None:
    clock = Clock(T0)
    targets = cup_targets(NH)
    pm_t = SeqTransport("rate_limits.json")  # the very first Gamma answer is a 429 with Retry-After 120
    pm = fv.PolymarketProvider(transport=pm_t.mock, clock=clock, sleep=lambda s: None)
    res = pm.refresh(targets, T0)
    assert res.status == "backoff" and res.next_try_at == T0 + 120.0 and len(pm_t.calls) == 1
    polled = pm.poll(targets, T0 + 15)
    assert polled.status == "backoff" and polled.requests == 0 and len(pm_t.calls) == 1
    assert polled.errors[0] == "Polymarket asked us to slow down (HTTP 429): next try in 120 s."
