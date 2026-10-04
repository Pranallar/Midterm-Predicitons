"""Tests for supermarket_bot/analytics.py (pure functions; synthetic series, no I/O)."""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

import pytest

from supermarket_bot import analytics as A
from supermarket_bot.analytics import (
    SURGE_WINDOWS,
    change_over,
    detect_surges,
    downsample,
    high_band,
    hours_until,
    mark_price,
    parse_iso_ts,
    trade_flow,
    update_surge_status,
    value_at,
    volatility,
)
from supermarket_bot.models import SURGE_HELD, SURGE_OPEN, SURGE_REVERTED, PricePoint, Surge, TradeRecord

T0 = 1_789_999_800.0  # 2026-09-21T14:10:00Z, a multiple of 300 s
H = 3600.0
M = 60.0


def P(ts: float, price: Optional[float], source: str = "tick") -> PricePoint:
    return PricePoint(ts=ts, price=price, source=source)


def build(fn: Callable[[float], float], start: float, end: float, step: float, source: str = "tick") -> List[PricePoint]:
    """Points every ``step`` seconds on [start, end] with ``price = fn(t)`` (rounded to 1e-6)."""
    out = []
    n = int(round((end - start) / step))
    for i in range(n + 1):
        t = start + i * step
        out.append(P(t, round(fn(t), 6), source))
    return out


def noise(amplitude: float) -> Callable[[float], float]:
    """Alternates +0 / +amplitude every 300 s: a calm market with volatility ~amplitude per step."""
    return lambda t: amplitude if int(t // 300) % 2 else 0.0


NOW = T0 + 50 * H  # leaves 50 h of history before "now" in most tests


def calm_then(after: Callable[[float], float], amp: float = 0.005, base: float = 0.50, hist_h: float = 50.0) -> List[PricePoint]:
    """Calm noisy baseline at ``base`` with ``after(t)`` added on top (60 s ticks up to NOW)."""
    wiggle = noise(amp)
    return build(lambda t: base + wiggle(t) + after(t), NOW - hist_h * H, NOW, 60.0)


def step_at(when: float, size: float) -> Callable[[float], float]:
    return lambda t: size if t >= when else 0.0


def ramp(t_start: float, t_end: float, size: float) -> Callable[[float], float]:
    def f(t: float) -> float:
        if t <= t_start:
            return 0.0
        if t >= t_end:
            return size
        return size * (t - t_start) / (t_end - t_start)
    return f


def expected_change(points: Sequence[PricePoint], now: float, window_s: float) -> float:
    then = max((p for p in points if p.ts <= now - window_s), key=lambda p: p.ts)
    last = max((p for p in points if p.ts <= now), key=lambda p: p.ts)
    return round(last.price - then.price, 6)  # type: ignore[operator]


# --------------------------------------------------------------------------- mark price


class TestMarkPrice:
    def test_tight_book_uses_mid(self) -> None:
        assert mark_price(0.30, 0.40, 0.44) == pytest.approx(0.42)

    def test_spread_exactly_max_on_tick_counts_as_tight(self) -> None:
        # 0.45 - 0.35 == 0.10000000000000003 in floats
        assert mark_price(0.30, 0.35, 0.45) == pytest.approx(0.40)

    def test_wide_book_prefers_last(self) -> None:
        assert mark_price(0.52, 0.30, 0.70) == 0.52

    def test_wide_book_without_last_uses_mid(self) -> None:
        assert mark_price(None, 0.30, 0.70) == pytest.approx(0.50)

    def test_one_sided_book(self) -> None:
        assert mark_price(0.61, 0.60, None) == 0.61
        assert mark_price(None, None, 0.60) is None
        assert mark_price(None, None, None) is None

    def test_custom_max_spread(self) -> None:
        assert mark_price(0.5, 0.40, 0.44, max_spread=0.02) == 0.5

    def test_nan_and_bool_are_not_prices(self) -> None:
        assert mark_price(float("nan"), None, None) is None
        assert mark_price(True, None, None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- value_at / change_over


class TestValueAt:
    pts = [P(T0, 0.40), P(T0 + 300, 0.45), P(T0 + 600, None), P(T0 + 900, 0.50)]

    def test_latest_at_or_before(self) -> None:
        assert value_at(self.pts, T0 + 300, 120).price == 0.45  # type: ignore[union-attr]
        assert value_at(self.pts, T0 + 450, 300).price == 0.45  # type: ignore[union-attr]
        assert value_at(self.pts, T0 - 1, 1e9) is None

    def test_tolerance(self) -> None:
        assert value_at(self.pts, T0 + 420, 120).price == 0.45  # type: ignore[union-attr]  # exactly 120 s old
        assert value_at(self.pts, T0 + 421, 120) is None

    def test_skips_none_prices(self) -> None:
        # the point at +600 has no price: +700 falls back to +300 (400 s old)
        assert value_at(self.pts, T0 + 700, 500).price == 0.45  # type: ignore[union-attr]
        assert value_at(self.pts, T0 + 700, 300) is None

    def test_unsorted_input(self) -> None:
        shuffled = list(reversed(self.pts))
        assert value_at(shuffled, T0 + 1000, 200).price == 0.50  # type: ignore[union-attr]

    def test_empty(self) -> None:
        assert value_at([], T0, 1e9) is None


class TestChangeOver:
    def test_tick_float_noise_is_rounded(self) -> None:
        pts = [P(T0, 0.40), P(T0 + H, 0.41)]
        assert change_over(pts, T0 + H, H) == 0.01

    def test_stale_current_is_none(self) -> None:
        pts = [P(T0, 0.40), P(T0 + H, 0.50)]
        assert change_over(pts, T0 + H + 14 * M, H) is not None
        assert change_over(pts, T0 + H + 16 * M, H) is None

    def test_reference_point_tolerance(self) -> None:
        # 1h window: the reference may be up to max(0.25 * 3600, 120) = 900 s older than now - 1h
        now = T0 + 3 * H
        ok = [P(now - H - 900, 0.42), P(now, 0.55)]
        assert change_over(ok, now, H) == pytest.approx(0.13)
        too_old = [P(now - H - 901, 0.42), P(now, 0.55)]
        assert change_over(too_old, now, H) is None

    def test_window_tolerance_floor(self) -> None:
        assert A.window_tolerance(300) == 120.0
        assert A.window_tolerance(3600) == 900.0
        assert A.window_tolerance(86400) == 21600.0


# --------------------------------------------------------------------------- volatility


def reference_vol(points: Sequence[PricePoint], end: float, lookback: float = 86400.0, step: float = 300.0) -> Optional[float]:
    """Independent float implementation of the DESIGN rule (forward fill, std-dev of diffs)."""
    pts = sorted((p for p in points if p.price is not None), key=lambda p: p.ts)
    k = int(lookback // step)
    vals = []
    for i in range(k + 1):
        g = end - (k - i) * step
        prior = [p for p in pts if p.ts <= g]
        if prior:
            vals.append(prior[-1].price)
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    if len(diffs) < 12:
        return None
    mean = sum(diffs) / len(diffs)
    return math.sqrt(sum((d - mean) ** 2 for d in diffs) / len(diffs))


class TestVolatility:
    def test_flat_is_exactly_zero(self) -> None:
        pts = build(lambda t: 0.45, T0, T0 + 30 * H, 60)
        assert volatility(pts, T0 + 30 * H) == 0.0

    def test_steady_drift_is_exactly_zero(self) -> None:
        # +0.005 every 300 s: every step difference is identical (float noise must not leak)
        pts = build(lambda t: 0.10 + 0.005 * int((t - T0) // 300), T0, T0 + 24 * H, 300)
        assert volatility(pts, T0 + 24 * H) == 0.0

    def test_alternating_noise(self) -> None:
        pts = build(lambda t: 0.50 + noise(0.01)(t), T0, T0 + 30 * H, 60)
        assert volatility(pts, T0 + 30 * H) == pytest.approx(0.01, rel=1e-3)

    def test_needs_twelve_steps(self) -> None:
        end = T0 + 10 * H
        twelve = build(lambda t: 0.5 + noise(0.01)(t), end - 3600, end, 300)
        eleven = build(lambda t: 0.5 + noise(0.01)(t), end - 3300, end, 300)
        assert volatility(twelve, end) is not None
        assert volatility(eleven, end) is None
        assert volatility([], end) is None

    def test_only_history_up_to_end_counts(self) -> None:
        pts = build(lambda t: 0.5 + noise(0.01)(t), T0, T0 + 30 * H, 60)
        pts += [P(T0 + 30 * H + 60, 0.99)]  # after ``end``: ignored
        assert volatility(pts, T0 + 30 * H) == pytest.approx(0.01, rel=1e-3)

    def test_candle_only_history_forward_fills(self) -> None:
        # hourly candles alternating +-0.01: on the 5m grid one step in 12 moves
        pts = build(lambda t: 0.50 + (0.01 if int(t // H) % 2 else 0.0), T0, T0 + 48 * H, H, source="candle")
        got = volatility(pts, T0 + 48 * H)
        assert got == pytest.approx(math.sqrt(0.0001 / 12), rel=0.01)

    def test_matches_reference_on_random_data(self) -> None:
        rng = random.Random(11)
        pts, price = [], 0.5
        t = T0
        while t < T0 + 28 * H:
            price = min(0.99, max(0.01, price + rng.choice([-0.005, 0, 0, 0.005])))
            pts.append(P(t, round(price, 3)))
            t += rng.choice([30, 60, 90, 400, 1200])  # irregular spacing with gaps
        rng.shuffle(pts)
        for end in (T0 + 25 * H, T0 + 27 * H + 123.4):
            assert volatility(pts, end) == pytest.approx(reference_vol(pts, end), rel=1e-9, abs=1e-12)


# --------------------------------------------------------------------------- surges


def only(surges: List[Surge]) -> Surge:
    assert len(surges) == 1, surges
    return surges[0]


class TestDetectSurgeWindows:
    def test_5m_jump(self) -> None:
        pts = calm_then(step_at(NOW - 2 * M, 0.06))
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert (s.window, s.window_s, s.direction) == ("5m", 300.0, "up")
        assert s.change == expected_change(pts, NOW, 300)
        assert s.zscore is not None and s.zscore >= 3
        assert (s.exchange_id, s.market_id, s.detected_at, s.status) == ("e1", "m1", NOW, SURGE_OPEN)

    def test_1h_ramp(self) -> None:
        pts = calm_then(ramp(NOW - 40 * M, NOW, 0.10))
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "1h"
        assert s.change == expected_change(pts, NOW, 3600)
        assert s.zscore == pytest.approx(s.change / (volatility(pts, NOW - 3600) * math.sqrt(12)), rel=1e-3)

    def test_6h_ramp(self) -> None:
        pts = calm_then(ramp(NOW - 5 * H, NOW, 0.15))
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "6h"
        assert s.change == expected_change(pts, NOW, 21600)

    def test_24h_ramp_with_zscore(self) -> None:
        pts = calm_then(ramp(NOW - 20 * H, NOW, 0.20), amp=0.002)
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "24h"
        assert s.zscore is not None and s.zscore >= 3

    def test_down_move(self) -> None:
        pts = calm_then(step_at(NOW - 2 * M, -0.08))
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.direction == "down" and s.change < 0
        assert s.peak_price == min(p.price for p in pts if p.ts >= s.start_ts)  # type: ignore[type-var]

    def test_start_and_end_come_from_points(self) -> None:
        pts = calm_then(step_at(NOW - 2 * M, 0.06))
        s = only(detect_surges(pts, NOW + 30, "e1", "m1"))
        then = max((p for p in pts if p.ts <= NOW + 30 - 300), key=lambda p: p.ts)
        assert (s.start_ts, s.start_price) == (then.ts, then.price)
        assert (s.end_ts, s.end_price) == (pts[-1].ts, pts[-1].price)
        assert s.detected_at == NOW + 30

    def test_most_significant_window_only(self) -> None:
        # a 0.15 jump qualifies in 5m, 1h and 6h; 5m has by far the largest |z|
        pts = calm_then(step_at(NOW - 2 * M, 0.15))
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "5m"
        for name in ("1h", "6h"):
            sub = {name: SURGE_WINDOWS[name]}
            other = only(detect_surges(pts, NOW, "e1", "m1", windows=sub))
            assert abs(other.zscore) < abs(s.zscore)  # type: ignore[arg-type]

    def test_no_surge_in_calm_market(self) -> None:
        assert detect_surges(calm_then(lambda t: 0.0), NOW, "e1", "m1") == []
        assert detect_surges([], NOW, "e1", "m1") == []


class TestDetectSurgeGating:
    def test_z_gate_blocks_a_move_in_a_noisy_market(self) -> None:
        noisy = calm_then(ramp(NOW - 40 * M, NOW, 0.10), amp=0.02)
        assert detect_surges(noisy, NOW, "e1", "m1") == []
        calm = calm_then(ramp(NOW - 40 * M, NOW, 0.10), amp=0.005)
        assert only(detect_surges(calm, NOW, "e1", "m1")).window == "1h"

    def test_z_threshold_parameter(self) -> None:
        noisy = calm_then(ramp(NOW - 40 * M, NOW, 0.10), amp=0.02)
        assert only(detect_surges(noisy, NOW, "e1", "m1", z_threshold=1.0)).window == "1h"

    def test_sigma_zero_means_min_change_only(self) -> None:
        pts = build(lambda t: 0.40 + (0.06 if t >= NOW - 2 * M else 0.0), NOW - 30 * H, NOW, 60)
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "5m" and s.zscore is None

    def test_short_history_means_min_change_only(self) -> None:
        wiggle = noise(0.01)
        pts = build(lambda t: 0.40 + wiggle(t) + (0.06 if t >= NOW - 2 * M else 0.0), NOW - 40 * M, NOW, 60)
        assert volatility(pts, NOW - 300) is None
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.zscore is None and s.window == "5m"

    def test_below_min_change(self) -> None:
        pts = build(lambda t: 0.40 + (0.045 if t >= NOW - 2 * M else 0.0), NOW - 30 * H, NOW, 60)
        assert detect_surges(pts, NOW, "e1", "m1") == []

    @pytest.mark.parametrize("before,after,window", [(0.40, 0.45, "5m"), (0.43, 0.48, "5m"), (0.51, 0.59, "1h")])
    def test_min_change_on_tick_float_edges(self, before: float, after: float, window: str) -> None:
        # 0.45 - 0.40 == 0.04999999999999999 and 0.59 - 0.51 == 0.07999999999999996
        sub = {window: SURGE_WINDOWS[window]}
        when = NOW - 2 * M if window == "5m" else NOW - 30 * M
        pts = build(lambda t: after if t >= when else before, NOW - 30 * H, NOW, 60)
        s = only(detect_surges(pts, NOW, "e1", "m1", windows=sub))
        assert s.change == pytest.approx(after - before)

    def test_staleness(self) -> None:
        pts = build(lambda t: 0.40 if t < NOW - 30 * M else 0.55, NOW - 30 * H, NOW, 60)
        assert only(detect_surges(pts, NOW + 14 * M, "e1", "m1")).window == "1h"
        assert detect_surges(pts, NOW + 16 * M, "e1", "m1") == []

    def test_no_look_ahead(self) -> None:
        pts = build(lambda t: 0.40 if t < NOW else 0.60, NOW - 30 * H, NOW + H, 60)
        assert detect_surges(pts, NOW - 60, "e1", "m1") == []

    def test_mixed_zscores_prefer_scored_windows(self) -> None:
        # a step 23 h ago gives the 1h/6h windows a volatility; the 24h window's history is flat
        pts = build(lambda t: 0.30 + (0.10 if t >= NOW - 23 * H else 0) + (0.15 if t >= NOW - 10 * M else 0),
                    NOW - 50 * H, NOW, 60)
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.zscore is not None and s.window in ("1h", "6h")
        flat24 = only(detect_surges(pts, NOW, "e1", "m1", windows={"24h": SURGE_WINDOWS["24h"]}))
        assert flat24.zscore is None and abs(flat24.change) > abs(s.change)

    def test_without_zscores_largest_change_wins(self) -> None:
        windows = {"10m": (600.0, 0.05), "20m": (1200.0, 0.05)}
        # 25 minutes of history: too short for any volatility, so both windows have z = None
        a = [P(NOW - 25 * M, 0.30), P(NOW - 21 * M, 0.30), P(NOW - 15 * M, 0.20), P(NOW - 11 * M, 0.20), P(NOW, 0.40)]
        s = only(detect_surges(a, NOW, "e1", "m1", windows=windows))
        assert (s.window, s.zscore, s.change) == ("10m", None, pytest.approx(0.20))
        b = [P(NOW - 25 * M, 0.10), P(NOW - 21 * M, 0.10), P(NOW - 15 * M, 0.30), P(NOW - 11 * M, 0.30), P(NOW, 0.40)]
        s = only(detect_surges(b, NOW, "e1", "m1", windows=windows))
        assert (s.window, s.change) == ("20m", pytest.approx(0.30))

    def test_gap_skips_window_without_reference(self) -> None:
        pts = [p for p in build(lambda t: 0.40 if t < NOW - 30 * M else 0.55, NOW - 30 * H, NOW, 60)
               if not (NOW - 3 * H < p.ts < NOW - 45 * M)]
        surges = detect_surges(pts, NOW, "e1", "m1")
        assert all(s.window != "1h" for s in surges)
        assert only(surges).window == "6h"


class TestDetectSurgeData:
    def test_unsorted_input_and_none_prices(self) -> None:
        pts = calm_then(step_at(NOW - 2 * M, 0.06))
        expected = only(detect_surges(pts, NOW, "e1", "m1")).to_dict()
        messy = list(pts) + [P(NOW - 30, None), P(NOW - 7 * M, None)]
        random.Random(5).shuffle(messy)
        assert only(detect_surges(messy, NOW, "e1", "m1")).to_dict() == expected

    def test_candle_only_history(self) -> None:
        hourly = build(lambda t: 0.40 + noise(0.005)(t // 12), NOW - 7 * 24 * H, NOW - 24 * H - 300, H, source="candle")
        five = build(lambda t: 0.40 + noise(0.005)(t) + (0.12 if t >= NOW - 40 * M else 0.0), NOW - 24 * H, NOW - 5 * M, 300,
                     source="candle")
        s = only(detect_surges(hourly + five, NOW, "e1", "m1"))
        assert s.direction == "up" and s.window in ("1h", "6h")
        stale = build(lambda t: 0.40, NOW - 48 * H, NOW - 50 * M, H, source="candle")
        assert detect_surges(stale, NOW, "e1", "m1") == []

    def test_peak_and_partial_reversion_inside_window(self) -> None:
        pts = build(lambda t: 0.40, NOW - 30 * H, NOW - 40 * M, 60)
        pts += [P(NOW - 30 * M, 0.55), P(NOW - 20 * M, 0.70), P(NOW - 5 * M, 0.60), P(NOW, 0.60)]
        s = only(detect_surges(pts, NOW, "e1", "m1"))
        assert s.window == "1h" and s.peak_price == 0.70 and s.end_price == 0.60
        assert s.reverted_fraction == pytest.approx(1 / 3, abs=1e-6)
        assert s.status == SURGE_OPEN

    def test_already_reverted_at_detection(self) -> None:
        pts = build(lambda t: 0.40, NOW - 30 * H, NOW - 40 * M, 60)
        pts += [P(NOW - 30 * M, 0.70), P(NOW - 10 * M, 0.52), P(NOW, 0.52)]
        s = only(detect_surges(pts, NOW, "e1", "m1", windows={"1h": SURGE_WINDOWS["1h"]}))
        assert s.status == SURGE_REVERTED and s.reverted_fraction == pytest.approx(0.6)


# --------------------------------------------------------------------------- incremental scanner


def random_walk(seed: int, start: float, hours: float, step: float, spikes=()) -> List[PricePoint]:
    rng = random.Random(seed)
    pts, price, t = [], 0.5, start
    while t <= start + hours * H:
        price += rng.choice([-0.005, 0, 0, 0, 0.005])
        for at, size in spikes:
            if at <= t < at + step:
                price += size
        price = min(0.99, max(0.01, price))
        pts.append(P(t, round(price, 4)))
        t += step
    return pts


class TestSurgeScanner:
    @pytest.mark.parametrize("step", [300.0, 600.0])
    def test_same_decisions_as_detect_surges(self, step: float) -> None:
        start = T0 + 17.3  # not on the grid
        pts = random_walk(3, start, 40, 120, spikes=[(start + 26 * H, 0.15), (start + 33 * H, -0.12)])
        scanner = A._SurgeScanner(A._Series(pts), step, SURGE_WINDOWS)
        assert scanner.fast
        times = list(scanner.times())
        assert times[0] == math.ceil(start / 300) * 300 and times[-1] <= pts[-1].ts
        detections = 0
        for t in times:
            fast = [s.to_dict() for s in scanner.detect(t, "e1", "m1")]
            plain = [s.to_dict() for s in detect_surges(pts, t, "e1", "m1")]
            assert fast == plain, t
            detections += bool(fast)
        assert detections >= 2  # the scripted spikes were seen

    def test_fallback_when_step_is_off_grid(self) -> None:
        pts = random_walk(4, T0, 30, 120, spikes=[(T0 + 26 * H, 0.15)])
        scanner = A._SurgeScanner(A._Series(pts), 420.0, SURGE_WINDOWS)
        assert not scanner.fast
        for t in list(scanner.times())[-40:]:
            assert [s.to_dict() for s in scanner.detect(t, "e1", "m1")] == \
                   [s.to_dict() for s in detect_surges(pts, t, "e1", "m1")]

    def test_empty_series(self) -> None:
        scanner = A._SurgeScanner(A._Series([]), 300.0, SURGE_WINDOWS)
        assert list(scanner.times()) == []


# --------------------------------------------------------------------------- surge lifecycle


def surge(start: float, peak: float, direction: str = "up", end_ts: float = T0) -> Surge:
    return Surge(exchange_id="e1", market_id="m1", window="1h", window_s=3600, start_ts=end_ts - H, end_ts=end_ts,
                 start_price=start, end_price=peak, change=round(peak - start, 6), direction=direction, peak_price=peak,
                 detected_at=end_ts)


class TestUpdateSurgeStatus:
    def test_half_reversion_on_tick_float(self) -> None:
        s = update_surge_status(surge(0.40, 0.60), 0.50, T0 + 60)
        assert s.status == SURGE_REVERTED and s.reverted_fraction == pytest.approx(0.5)
        assert s.current_price == 0.50

    def test_open_below_half(self) -> None:
        s = update_surge_status(surge(0.40, 0.60), 0.55, T0 + 60)
        assert s.status == SURGE_OPEN and s.reverted_fraction == pytest.approx(0.25)

    def test_extension_is_negative_and_clamped(self) -> None:
        assert update_surge_status(surge(0.40, 0.60), 0.70, T0).reverted_fraction == pytest.approx(-0.5)
        assert update_surge_status(surge(0.40, 0.60), 0.95, T0).reverted_fraction == -1.0
        s = update_surge_status(surge(0.40, 0.60), 0.0, T0)
        assert s.reverted_fraction == 2.0 and s.status == SURGE_REVERTED

    def test_down_move_mirrored(self) -> None:
        s = update_surge_status(surge(0.60, 0.40, "down"), 0.50, T0)
        assert s.status == SURGE_REVERTED and s.reverted_fraction == pytest.approx(0.5)
        s = update_surge_status(surge(0.60, 0.40, "down"), 0.35, T0)
        assert s.status == SURGE_OPEN and s.reverted_fraction == pytest.approx(-0.25)

    def test_held_after_24h(self) -> None:
        assert update_surge_status(surge(0.40, 0.60), 0.58, T0 + 24 * H - 1).status == SURGE_OPEN
        assert update_surge_status(surge(0.40, 0.60), 0.58, T0 + 24 * H).status == SURGE_HELD
        # reverting wins over holding
        assert update_surge_status(surge(0.40, 0.60), 0.45, T0 + 48 * H).status == SURGE_REVERTED

    def test_missing_price_reuses_current(self) -> None:
        s = surge(0.40, 0.60)
        s.current_price = 0.48
        update_surge_status(s, None, T0)
        assert s.current_price == 0.48 and s.status == SURGE_REVERTED
        fresh = update_surge_status(surge(0.40, 0.60), None, T0)
        assert fresh.reverted_fraction is None and fresh.status == SURGE_OPEN


# --------------------------------------------------------------------------- high band


class TestHighBand:
    def test_yes_favourite(self) -> None:
        pts = build(lambda t: 0.97, NOW - 8 * H, NOW, 60)
        b = high_band(pts, NOW, "e1", "m1")
        assert b is not None
        assert (b.side, b.favorite_price, b.time_in_band, b.stable) == ("YES", 0.97, 1.0, True)
        assert (b.mean, b.low, b.high, b.lookback_s) == (pytest.approx(0.97), 0.97, 0.97, 6 * H)

    def test_no_favourite(self) -> None:
        pts = build(lambda t: 0.03, NOW - 8 * H, NOW, 60)
        b = high_band(pts, NOW, "e1", "m1")
        assert b is not None and b.side == "NO" and b.favorite_price == pytest.approx(0.97)

    def test_threshold_on_float_edge(self) -> None:
        pts = build(lambda t: 0.05, NOW - 8 * H, NOW, 60)  # NO side: 1 - 0.05
        b = high_band(pts, NOW, "e1", "m1")
        assert b is not None and b.side == "NO" and b.favorite_price == pytest.approx(0.95)

    def test_time_in_band_is_time_weighted(self) -> None:
        mostly = [P(NOW - 6 * H, 0.90), P(NOW - 5 * H, 0.97)]  # 5 of 6 hours in band
        b = high_band(mostly, NOW, "e1", "m1")
        assert b is not None and b.time_in_band == pytest.approx(5 / 6, abs=1e-4)
        assert b.mean == pytest.approx((0.90 + 5 * 0.97) / 6)
        too_little = [P(NOW - 6 * H, 0.90), P(NOW - 4.5 * H, 0.97)]  # 75%
        assert high_band(too_little, NOW, "e1", "m1") is None
        assert high_band(too_little, NOW, "e1", "m1", min_fraction=0.7) is not None

    def test_stable_flag(self) -> None:
        wide = [P(NOW - 7 * H, 0.95), P(NOW - 3 * H, 0.99)]
        b = high_band(wide, NOW, "e1", "m1")
        assert b is not None and b.stable is False and (b.low, b.high) == (0.95, 0.99)
        edge = [P(NOW - 7 * H, 0.96), P(NOW - 3 * H, 0.99)]  # 0.99 - 0.96 == 0.030000000000000027
        assert high_band(edge, NOW, "e1", "m1").stable is True  # type: ignore[union-attr]

    def test_current_below_threshold(self) -> None:
        pts = build(lambda t: 0.97 if t < NOW - 10 * M else 0.94, NOW - 8 * H, NOW, 60)
        assert high_band(pts, NOW, "e1", "m1") is None

    def test_uncovered_lookback_counts_as_outside(self) -> None:
        pts = build(lambda t: 0.98, NOW - 3 * H, NOW, 60)
        assert high_band(pts, NOW, "e1", "m1") is None

    def test_value_before_lookback_is_carried_in(self) -> None:
        pts = [P(NOW - 10 * H, 0.97), P(NOW, 0.97)]
        b = high_band(pts, NOW, "e1", "m1")
        assert b is not None and b.time_in_band == 1.0

    def test_settlement_hours_and_staleness(self) -> None:
        pts = [P(NOW - 10 * H, 0.98), P(NOW - 2 * H, 0.98)]
        settle = datetime.fromtimestamp(NOW + 36 * H, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        b = high_band(pts, NOW, "e1", "m1", settlement_date=settle)
        assert b is not None and b.hours_to_settlement == pytest.approx(36.0) and b.settlement_date == settle
        assert high_band(pts, NOW, "e1", "m1", max_staleness_s=3600) is None

    def test_unsorted_and_empty(self) -> None:
        pts = build(lambda t: 0.97, NOW - 8 * H, NOW, 600)
        assert high_band(list(reversed(pts)), NOW, "e1", "m1") is not None
        assert high_band([], NOW, "e1", "m1") is None
        assert high_band([P(NOW + 60, 0.99)], NOW, "e1", "m1") is None  # only future data


# --------------------------------------------------------------------------- trade flow


def trade(i: int, ts: float, price: Optional[float], size: float, side: Optional[str]) -> TradeRecord:
    return TradeRecord(trade_id=str(i), exchange_id="e1", ts=ts, price=price, size=size, side=side)


class TestTradeFlow:
    def test_numbers(self) -> None:
        trades = [trade(3, T0 + 30, 0.55, 600, "NO"), trade(1, T0, 0.40, 100, "YES"), trade(2, T0 + 10, 0.45, 300, "yes")]
        f = trade_flow(trades)
        assert (f.n_trades, f.total_size, f.max_trade_size) == (3, 1000, 600)
        assert f.top_trade_share == pytest.approx(0.6)
        assert f.hhi == pytest.approx(0.01 + 0.09 + 0.36)
        assert f.yes_share == pytest.approx(0.4)
        assert f.vwap == pytest.approx((0.40 * 100 + 0.45 * 300 + 0.55 * 600) / 1000)
        assert (f.first_ts, f.last_ts) == (T0, T0 + 30)
        assert f.price_impact == pytest.approx(0.15 / 10)  # time order: 0.40 first, 0.55 last

    def test_small_volume_impact_floor(self) -> None:
        f = trade_flow([trade(1, T0, 0.40, 20, "YES"), trade(2, T0 + 5, 0.58, 30, "YES")])
        assert f.price_impact == pytest.approx(0.18)  # divided by max(50/100, 1) = 1
        assert f.yes_share == 1.0 and f.hhi == pytest.approx(0.16 + 0.36)

    def test_single_trade(self) -> None:
        f = trade_flow([trade(1, T0, 0.40, 500, "NO")])
        assert (f.top_trade_share, f.hhi, f.yes_share, f.price_impact) == (1.0, 1.0, 0.0, 0.0)

    def test_unknown_sides_and_prices(self) -> None:
        f = trade_flow([trade(1, T0, None, 100, None), trade(2, T0 + 1, 0.5, 100, "?")])
        assert f.yes_share is None and f.vwap == 0.5 and f.price_impact == 0.0

    def test_empty(self) -> None:
        f = trade_flow([])
        assert f.n_trades == 0 and f.total_size == 0 and f.vwap is None and f.price_impact is None


# --------------------------------------------------------------------------- downsample / time


class TestDownsample:
    def test_keeps_endpoints_and_spacing(self) -> None:
        pts = build(lambda t: 0.5, T0, T0 + 999 * 60, 60)
        out = downsample(pts, 96)
        assert len(out) == 96 and out[0] is pts[0] and out[-1] is pts[-1]
        idx = [pts.index(p) for p in out]
        gaps = {b - a for a, b in zip(idx, idx[1:])}
        assert gaps <= {10, 11}  # evenly spaced: (1000 - 1) / 95 = 10.5

    def test_small_inputs(self) -> None:
        pts = build(lambda t: 0.5, T0, T0 + 9 * 60, 60)
        assert downsample(pts, 96) == pts
        assert downsample(pts, 2) == [pts[0], pts[-1]]
        assert downsample(pts, 1) == [pts[-1]]
        assert downsample(pts, 0) == []
        assert downsample([], 10) == []

    def test_sorts_input(self) -> None:
        pts = build(lambda t: 0.5, T0, T0 + 99 * 60, 60)
        out = downsample(list(reversed(pts)), 10)
        assert out[0].ts == T0 and out[-1].ts == T0 + 99 * 60
        assert [p.ts for p in out] == sorted(p.ts for p in out)


class TestHoursUntil:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc).timestamp()

    @pytest.mark.parametrize("text,hours", [
        ("2026-10-04T00:00:00Z", 12.0),
        ("2026-10-04T00:00:00.000Z", 12.0),
        ("2026-10-04T00:00:00.123456789Z", 12.0 + 0.123456 / 3600),
        ("2026-10-04T02:00:00+02:00", 12.0),
        ("2026-10-04T00:00:00", 12.0),  # naive -> UTC
        ("2026-10-04", 12.0),
        ("2026-10-03T06:00:00Z", -6.0),
    ])
    def test_parses(self, text: str, hours: float) -> None:
        assert hours_until(text, self.now) == pytest.approx(hours)

    @pytest.mark.parametrize("text", [None, "", "soon", "2026-13-01T00:00:00Z"])
    def test_unparseable(self, text: Optional[str]) -> None:
        assert hours_until(text, self.now) is None

    def test_parse_iso_ts_numbers(self) -> None:
        assert parse_iso_ts(1_790_000_000) == 1_790_000_000.0
        assert parse_iso_ts("1790000000") == 1_790_000_000.0
        assert parse_iso_ts(True) is None
