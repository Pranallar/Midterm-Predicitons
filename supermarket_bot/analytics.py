"""Pure price analytics: marks, changes, volatility, surges, high band, trade flow.

No I/O. All thresholds are documented in docs/DESIGN.md (Analytics section).

Float care: prices sit on a 0.005 tick, so differences such as ``0.41 - 0.40`` come out as
``0.00999…``. Every threshold comparison allows ``1e-9`` of slack, and volatility is computed
from prices quantised to integer nano-units so a perfectly flat (or perfectly steady) history
gives a volatility of exactly 0 instead of float noise.
"""

from __future__ import annotations

import bisect
import logging
import math
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .books import parse_time
from .models import SURGE_HELD, SURGE_OPEN, SURGE_REVERTED, HighBand, PricePoint, Surge, TradeFlow, TradeRecord

log = logging.getLogger("supermarket_bot")

# window name -> (seconds, minimum absolute move)
SURGE_WINDOWS: Dict[str, Tuple[float, float]] = {
    "5m": (300.0, 0.05),
    "1h": (3600.0, 0.08),
    "6h": (21600.0, 0.10),
    "24h": (86400.0, 0.12),
}
Z_THRESHOLD = 3.0
SHORTEST_SHARE = 0.5  # pick the shortest qualifying window holding >= half of the largest move
MAX_STALENESS_S = 900.0

VOL_STEP_S = 300.0  # volatility resampling grid
VOL_LOOKBACK_S = 86400.0  # history used for volatility, ending where the window under test starts
MIN_VOL_STEPS = 12  # fewer step differences than this -> volatility is unknown (None)
MIN_TOLERANCE_S = 120.0
HELD_AFTER_S = 86400.0  # a surge that has not reverted 24h after its end is "held"
REVERT_FRACTION = 0.5
STABLE_RANGE = 0.03

_EPS = 1e-9  # price/threshold slack (0.005 tick arithmetic)
_TS_EPS = 1e-6  # timestamp slack in seconds
_NANO = 1e9  # quantisation for volatility: integer nano-price units


# --------------------------------------------------------------------------- helpers


def _num(value: object) -> Optional[float]:
    """``value`` as a finite float, or None (bools and NaN/inf are not numbers here)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _quantize(price: float) -> int:
    return int(round(price * _NANO))


def window_tolerance(window_s: float) -> float:
    """How old the reference point of a window may be: ``max(0.25 * window, 120 s)``."""
    return max(0.25 * window_s, MIN_TOLERANCE_S)


def parse_iso_ts(value: object) -> Optional[float]:
    """Epoch seconds from an ISO-8601 string (``Z``/offset/naive-as-UTC/date-only) or a number."""
    num = _num(value)
    if num is not None:
        return num
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        dt = parse_time(text)
        if dt is not None:
            return dt.timestamp()
    return None


class _Series:
    """Time-ordered points with a usable price, plus parallel ``ts``/``prices`` lists for bisecting."""

    __slots__ = ("points", "ts", "prices", "held", "_q")

    def __init__(self, points: Optional[Sequence[PricePoint]]) -> None:
        usable = [p for p in (points or ()) if p is not None and _num(p.ts) is not None and _num(p.price) is not None]
        usable.sort(key=lambda p: p.ts)  # stable, and O(n) when already sorted
        self.points: List[PricePoint] = usable
        self.ts: List[float] = [float(p.ts) for p in usable]
        self.prices: List[float] = [float(p.price) for p in usable]  # type: ignore[arg-type]
        # Candles exist only for buckets with trades: a candle close holds until the next point.
        self.held: List[bool] = [getattr(p, "source", "tick") == "candle" for p in usable]
        self._q: Optional[List[int]] = None

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def q(self) -> List[int]:
        if self._q is None:
            self._q = [_quantize(p) for p in self.prices]
        return self._q

    def idx_at(self, t: float) -> Optional[int]:
        """Index of the latest point with ``ts <= t``."""
        i = bisect.bisect_right(self.ts, t + _TS_EPS) - 1
        return i if i >= 0 else None

    def value_idx(self, t: float, tolerance_s: float) -> Optional[int]:
        """Like :meth:`idx_at`, but None when that point is older than ``t - tolerance_s``.

        A *candle* point is the exception when a later point exists: the price-history API only
        has candles for buckets with trades, so a gap after a candle means nobody traded and its
        close still held at ``t`` (forward fill). A gap between ticks is a tracking outage, and a
        candle with nothing after it may be stale, so those keep the tolerance rule.
        """
        i = self.idx_at(t)
        if i is None:
            return None
        if self.ts[i] < t - tolerance_s - _TS_EPS and not (self.held[i] and i + 1 < len(self.ts)):
            return None
        return i

    def window_ref(self, t: float, tolerance_s: float, partial: bool = False) -> Optional[Tuple[int, float]]:
        """``(index, start time)`` of the reference for a window starting at ``t``, or None.

        The start time is the point's own time, except for a held candle (its close carried
        forward), which starts the window at ``t`` itself. With ``partial``, a series that only
        begins after ``t`` uses its first point: the move since tracking started, which is shorter
        than the window, so a qualifying change is a real move within the window.
        """
        i = self.value_idx(t, tolerance_s)
        if i is not None:
            start = self.ts[i]
            return i, (t if start < t - tolerance_s - _TS_EPS else start)
        if partial and len(self.ts) and self.ts[0] > t + _TS_EPS:
            return 0, self.ts[0]
        return None


def _sigma_from_sums(n: int, s1: int, s2: int) -> float:
    """Population std-dev of ``n`` integer nano-unit diffs with exact sums ``s1`` and ``s2``."""
    var_num = n * s2 - s1 * s1  # exact integer, >= 0
    if var_num <= 0:
        return 0.0
    return math.sqrt(var_num) / n / _NANO


def _grid_steps(lookback_s: float, step_s: float) -> int:
    return int(math.floor(lookback_s / step_s + 1e-9))


def _volatility(series: _Series, end: float, lookback_s: float, step_s: float) -> Optional[float]:
    if step_s <= 0 or lookback_s <= 0 or not len(series):
        return None
    k = _grid_steps(lookback_s, step_s)
    if k < MIN_VOL_STEPS:
        return None
    ts, q = series.ts, series.q
    # grid g_i = end - (k - i) * step_s for i = 0..k (the last grid point is ``end`` itself)
    first = end - k * step_s
    j = bisect.bisect_right(ts, first + _TS_EPS) - 1
    last_n = len(ts) - 1
    prev: Optional[int] = None
    n = s1 = s2 = 0
    for i in range(k + 1):
        g = end - (k - i) * step_s
        while j < last_n and ts[j + 1] <= g + _TS_EPS:
            j += 1
        if j < 0:
            continue
        cur = q[j]
        if prev is not None:
            d = cur - prev
            n += 1
            s1 += d
            s2 += d * d
        prev = cur
    if n < MIN_VOL_STEPS:
        return None
    return _sigma_from_sums(n, s1, s2)


# --------------------------------------------------------------------------- public API


def mark_price(last: Optional[float], bid: Optional[float], ask: Optional[float], max_spread: float = 0.10) -> Optional[float]:
    """Mid of a tight two-sided book, else the last trade, else the mid of a wide book, else None."""
    last, bid, ask = _num(last), _num(bid), _num(ask)
    mid = round((bid + ask) / 2, 6) if bid is not None and ask is not None else None
    if mid is not None and ask - bid <= max_spread + _EPS:  # type: ignore[operator]
        return mid
    if last is not None:
        return last
    return mid


def value_at(points: Sequence[PricePoint], t: float, tolerance_s: float) -> Optional[PricePoint]:
    """Latest point with ``ts <= t`` and a non-None price, if not older than ``t - tolerance_s``
    (a candle followed by a later point holds through the gap, see ``_Series.value_idx``)."""
    series = _Series(points)
    i = series.value_idx(t, tolerance_s)
    return series.points[i] if i is not None else None


def change_over(points: Sequence[PricePoint], now: float, window_s: float) -> Optional[float]:
    """``p_now - p_then`` over ``window_s`` (None when either mark is missing or stale).

    ``p_now`` must be no older than 15 minutes; ``p_then`` uses the window tolerance, except that
    a candle close holds until the next point (trade-less buckets have no candle).
    """
    series = _Series(points)
    i_now = series.value_idx(now, MAX_STALENESS_S)
    if i_now is None:
        return None
    i_then = series.value_idx(now - window_s, window_tolerance(window_s))
    if i_then is None:
        return None
    return round(series.prices[i_now] - series.prices[i_then], 6)


def volatility(points: Sequence[PricePoint], end: float, lookback_s: float = 86400.0, step_s: float = 300.0) -> Optional[float]:
    """Std-dev of step changes on a forward-filled ``step_s`` grid over ``[end - lookback_s, end]``.

    None with fewer than 12 step differences. Exactly 0.0 for a flat or perfectly steady series.
    """
    return _volatility(_Series(points), end, lookback_s, step_s)


SigmaFn = Callable[[float, float], Optional[float]]  # (window_s, end) -> sigma


def _detect(
    series: _Series,
    now: float,
    exchange_id: str,
    market_id: str,
    windows: Dict[str, Tuple[float, float]],
    z_threshold: float,
    sigma_fn: SigmaFn,
    partial: bool = False,
) -> List[Surge]:
    """Shared decision logic for :func:`detect_surges` and the backtest's incremental scanner."""
    if not len(series):
        return []
    i_now = series.value_idx(now, MAX_STALENESS_S)
    if i_now is None:
        return []
    prices = series.prices
    p_now = prices[i_now]
    candidates: List[Tuple[str, float, int, float, float, Optional[float]]] = []
    for name, (window_s, min_change) in sorted(windows.items(), key=lambda kv: kv[1][0]):
        ref = series.window_ref(now - window_s, window_tolerance(window_s), partial)
        if ref is None:
            continue
        i_then, start_ts = ref
        if i_then >= i_now:
            continue
        change = round(p_now - prices[i_then], 6)
        if abs(change) < _EPS or abs(change) + _EPS < min_change:
            continue
        sigma = sigma_fn(window_s, now - window_s)
        z = change / (sigma * math.sqrt(window_s / VOL_STEP_S)) if sigma is not None and sigma > 0 else None
        if z is not None and abs(z) + _EPS < z_threshold:
            continue
        candidates.append((name, window_s, i_then, start_ts, change, z))
    if not candidates:
        return []
    # Describe the move by the SHORTEST window that still captures most of it: a 20-minute
    # spike is a 1h surge, not a 24h one, so attribution looks at the trades that moved it.
    biggest = max(abs(c[4]) for c in candidates)
    name, window_s, i_then, start_ts, change, z = next(
        c for c in candidates if abs(c[4]) + _EPS >= SHORTEST_SHARE * biggest
    )
    span = prices[i_then : i_now + 1]
    up = change > 0
    surge = Surge(
        exchange_id=exchange_id,
        market_id=market_id,
        window=name,
        window_s=window_s,
        start_ts=start_ts,
        end_ts=series.ts[i_now],
        start_price=prices[i_then],
        end_price=p_now,
        change=change,
        direction="up" if up else "down",
        peak_price=max(span) if up else min(span),
        detected_at=now,
        zscore=round(z, 4) if z is not None else None,
    )
    return [update_surge_status(surge, p_now, now)]


def detect_surges(
    points: Sequence[PricePoint],
    now: float,
    exchange_id: str,
    market_id: str,
    windows: Optional[Dict[str, Tuple[float, float]]] = None,
    z_threshold: float = Z_THRESHOLD,
    *,
    partial: bool = False,
) -> List[Surge]:
    """At most one Surge (the most significant qualifying window). Empty list when none.

    A window qualifies when ``|change| >= min_change`` and (``z`` is None or ``|z| >= 3``),
    where ``z = change / (sigma * sqrt(window / 300))`` with ``sigma`` the volatility of the
    24 h *before* the window. Among qualifying windows the shortest one whose ``|change|`` is at
    least half of the largest qualifying ``|change|`` is reported. The current mark must be
    no older than 15 minutes. Only points with ``ts <= now`` are used (no look-ahead).

    ``partial=True`` is for live-only tracking (no candle history): a window reaching back
    before the first point measures the move since that point instead of being skipped, so a
    jump seen live is reported without waiting a full window of ticks.
    """
    series = _Series(points)

    def sigma(window_s: float, end: float) -> Optional[float]:
        return _volatility(series, end, VOL_LOOKBACK_S, VOL_STEP_S)

    return _detect(series, now, exchange_id, market_id, windows or SURGE_WINDOWS, z_threshold, sigma, partial)


class _SurgeScanner:
    """Runs the :func:`detect_surges` decision at ``t0, t0 + step, …`` in near-linear time.

    Volatility normally re-resamples 24 h of history at every call. Here the forward-filled
    300 s grid and exact integer prefix sums of its step differences are built once, so each
    window's sigma is O(1). Grid times are integer-valued floats, so they coincide exactly with
    the grids :func:`detect_surges` builds at the same ``now``; the decisions are identical
    (tests check this). Falls back to the plain computation when ``step_s`` or a window is
    not a multiple of 300 s.
    """

    def __init__(self, series: _Series, step_s: float, windows: Dict[str, Tuple[float, float]]) -> None:
        self.series = series
        self.step_s = float(step_s)
        self.windows = windows
        base = VOL_STEP_S
        self.k = _grid_steps(VOL_LOOKBACK_S, base)
        ok = len(series) > 0 and step_s > 0 and _is_multiple(step_s, base) and _is_multiple(VOL_LOOKBACK_S, base)
        ok = ok and all(_is_multiple(w, base) for w, _ in windows.values())
        self.fast = ok
        if not len(series):
            self.t0 = 0.0
            self.t_end = -1.0
            return
        # first grid time that has data under the same timestamp slack the plain path uses
        self.t0 = math.ceil((series.ts[0] - _TS_EPS) / base) * base
        self.t_end = series.ts[-1]
        if not ok:
            return
        n_grid = int(math.floor((self.t_end - self.t0) / base + 1e-9)) + 1
        ts, q = series.ts, series.q
        p1 = [0] * max(n_grid, 1)
        p2 = [0] * max(n_grid, 1)
        j = -1
        last_n = len(ts) - 1
        prev: Optional[int] = None
        for g_i in range(n_grid):
            g = self.t0 + g_i * base
            while j < last_n and ts[j + 1] <= g + _TS_EPS:
                j += 1
            cur = q[j]  # j >= 0: t0 is at or after the first point (with slack)
            if prev is None:
                p1[g_i], p2[g_i] = 0, 0
            else:
                d = cur - prev
                p1[g_i] = p1[g_i - 1] + d
                p2[g_i] = p2[g_i - 1] + d * d
            prev = cur
        self._p1, self._p2 = p1, p2
        self._n_grid = n_grid

    def times(self) -> Iterator[float]:
        if self.t_end < self.t0:
            return
        i = 0
        while True:
            t = self.t0 + i * self.step_s
            if t > self.t_end + _TS_EPS:
                return
            yield t
            i += 1

    def _sigma_fast(self, window_s: float, end: float) -> Optional[float]:
        e = int(round((end - self.t0) / VOL_STEP_S))
        if e < 0:
            return None
        e = min(e, self._n_grid - 1)
        a = max(0, e - self.k)
        n = e - a
        if n < MIN_VOL_STEPS:
            return None
        return _sigma_from_sums(n, self._p1[e] - self._p1[a], self._p2[e] - self._p2[a])

    def _sigma_plain(self, window_s: float, end: float) -> Optional[float]:
        return _volatility(self.series, end, VOL_LOOKBACK_S, VOL_STEP_S)

    def detect(self, now: float, exchange_id: str, market_id: str, z_threshold: float = Z_THRESHOLD) -> List[Surge]:
        sigma = self._sigma_fast if self.fast else self._sigma_plain
        return _detect(self.series, now, exchange_id, market_id, self.windows, z_threshold, sigma)


def _is_multiple(value: float, base: float) -> bool:
    ratio = value / base
    return abs(ratio - round(ratio)) < 1e-9 and float(value).is_integer()


def update_surge_status(surge: Surge, current_price: Optional[float], now: float) -> Surge:
    """Set ``current_price``, ``reverted_fraction`` and ``status`` (open/reverted/held) in place; return it.

    ``reverted_fraction = (peak - current) / (peak - start)`` for up-moves (mirrored for down),
    clamped to [-1, 2]: negative means the move extended beyond the old peak. A fraction of 0.5
    or more is ``reverted``; otherwise the surge is ``held`` once 24 h have passed since its
    end, else ``open``. With no price the previous ``current_price`` is reused.
    """
    cur = _num(current_price)
    if cur is not None:
        surge.current_price = cur
    else:
        cur = _num(surge.current_price)
    frac: Optional[float] = None
    if cur is not None:
        peak, start = surge.peak_price, surge.start_price
        if surge.direction == "down":
            num, den = cur - peak, start - peak
        else:
            num, den = peak - cur, peak - start
        if den > _EPS:
            frac = max(-1.0, min(2.0, num / den))
    if frac is not None:
        surge.reverted_fraction = round(frac, 6)
    if frac is not None and frac + _EPS >= REVERT_FRACTION:
        surge.status = SURGE_REVERTED
    elif now - surge.end_ts >= HELD_AFTER_S - _TS_EPS:
        surge.status = SURGE_HELD
    else:
        surge.status = SURGE_OPEN
    return surge


def high_band(
    points: Sequence[PricePoint],
    now: float,
    exchange_id: str,
    market_id: str,
    threshold: float = 0.95,
    lookback_s: float = 6 * 3600.0,
    min_fraction: float = 0.8,
    settlement_date: Optional[str] = None,
    *,
    max_staleness_s: Optional[float] = None,
) -> Optional[HighBand]:
    """The favourite side's band, when it sits at or above ``threshold`` now and for
    ``min_fraction`` of the lookback (time-weighted; time without data counts as outside).

    The favourite is YES when the current mark is >= 0.5, else NO (``1 - p``). The value in
    effect at the start of the lookback (the latest earlier point) is carried in.
    ``max_staleness_s`` optionally rejects a current mark older than that.
    """
    if lookback_s <= 0:
        raise ValueError("lookback_s must be positive")
    series = _Series(points)
    i_now = series.idx_at(now)
    if i_now is None:
        return None
    ts, prices = series.ts, series.prices
    if max_staleness_s is not None and ts[i_now] < now - max_staleness_s - _TS_EPS:
        return None
    yes = prices[i_now] >= 0.5

    def fav(p: float) -> float:
        return round(p if yes else 1.0 - p, 9)

    current = fav(prices[i_now])
    if current + _EPS < threshold:
        return None
    start = now - lookback_s
    k0 = series.idx_at(start)
    if k0 is None:
        k0 = bisect.bisect_right(ts, start + _TS_EPS)
    in_band = covered = weighted = 0.0
    low = high = current
    for k in range(k0, i_now + 1):
        v = fav(prices[k])
        low, high = min(low, v), max(high, v)
        seg_start = max(start, ts[k])
        seg_end = min(now, ts[k + 1]) if k < i_now else now
        dur = seg_end - seg_start
        if dur <= 0:
            continue
        covered += dur
        weighted += v * dur
        if v + _EPS >= threshold:
            in_band += dur
    time_in_band = min(1.0, in_band / lookback_s)
    if time_in_band + _EPS < min_fraction:
        return None
    mean = weighted / covered if covered > 0 else current
    hours = hours_until(settlement_date, now)
    return HighBand(
        exchange_id=exchange_id,
        market_id=market_id,
        side="YES" if yes else "NO",
        favorite_price=round(current, 6),
        time_in_band=round(time_in_band, 4),
        mean=round(mean, 6),
        low=round(low, 6),
        high=round(high, 6),
        lookback_s=float(lookback_s),
        stable=high - low <= STABLE_RANGE + _EPS,
        settlement_date=settlement_date,
        hours_to_settlement=round(hours, 3) if hours is not None else None,
    )


def _side(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text in ("YES", "Y"):
        return "YES"
    if text in ("NO", "N"):
        return "NO"
    return None


def trade_flow(trades: Sequence[TradeRecord]) -> TradeFlow:
    """Who-moved-the-price statistics for a set of trades (sorted by time internally).

    ``yes_share`` is over trades whose side is known; ``vwap`` over priced trades;
    ``price_impact = |last_price - first_price| / max(total_size / 100, 1)``.
    """
    rows = [t for t in (trades or ()) if t is not None and _num(t.ts) is not None]
    rows.sort(key=lambda t: t.ts)
    if not rows:
        return TradeFlow()
    sizes = [max(0.0, _num(t.size) or 0.0) for t in rows]
    total = sum(sizes)
    biggest = max(sizes)
    yes_size = sum(s for t, s in zip(rows, sizes) if _side(t.side) == "YES")
    sided = sum(s for t, s in zip(rows, sizes) if _side(t.side) is not None)
    priced = [(_num(t.price), s) for t, s in zip(rows, sizes) if _num(t.price) is not None]
    priced_size = sum(s for _, s in priced)
    vwap = sum(p * s for p, s in priced) / priced_size if priced_size > 0 else None  # type: ignore[operator]
    impact = None
    if priced:
        impact = abs(priced[-1][0] - priced[0][0]) / max(total / 100.0, 1.0)  # type: ignore[operator]
    return TradeFlow(
        n_trades=len(rows),
        total_size=round(total, 6),
        max_trade_size=round(biggest, 6),
        top_trade_share=round(biggest / total, 6) if total > 0 else 0.0,
        hhi=round(sum((s / total) ** 2 for s in sizes), 6) if total > 0 else 0.0,
        yes_share=round(yes_size / sided, 6) if sided > 0 else None,
        vwap=round(vwap, 6) if vwap is not None else None,
        first_ts=float(rows[0].ts),
        last_ts=float(rows[-1].ts),
        price_impact=round(impact, 6) if impact is not None else None,
    )


def downsample(points: Sequence[PricePoint], max_points: int) -> List[PricePoint]:
    """At most ``max_points`` points: the first, the last and evenly spaced ones in between.

    With ``max_points == 1`` only the latest point is kept. Input is sorted by time first.
    """
    pts = sorted((p for p in (points or ()) if p is not None), key=lambda p: p.ts)
    n = len(pts)
    if max_points <= 0:
        return []
    if n <= max_points:
        return pts
    if max_points == 1:
        return [pts[-1]]
    m = max_points - 1
    # round-half-up of i * (n - 1) / m in integer arithmetic; distinct because n - 1 > m
    return [pts[(2 * i * (n - 1) + m) // (2 * m)] for i in range(max_points)]


def downsample_by_time(points: Sequence[PricePoint], max_points: int, start: Optional[float] = None,
                       end: Optional[float] = None) -> List[PricePoint]:
    """At most ``max_points`` points spread evenly over *time* (for sparklines and charts).

    ``[start, end]`` (default: the first and last point) is cut into ``max_points - 1`` equal
    slots; each slot keeps its last point, and an empty slot after data repeats the previous
    price at the slot's end (a price holds until it changes). The first and last points are
    always kept. Unlike :func:`downsample`, a burst of dense live ticks cannot crowd out the
    rest of the window. Input is sorted by time first.
    """
    pts = sorted((p for p in (points or ()) if p is not None and _num(p.ts) is not None), key=lambda p: p.ts)
    if max_points <= 0 or not pts:
        return []
    if len(pts) <= max_points:
        return pts
    if max_points == 1:
        return [pts[-1]]
    lo = float(start) if start is not None else float(pts[0].ts)
    hi = float(end) if end is not None else float(pts[-1].ts)
    lo, hi = min(lo, float(pts[0].ts)), max(hi, float(pts[-1].ts))
    slots = max_points - 1
    width = (hi - lo) / slots
    if width <= 0:
        return [pts[0], pts[-1]] if len(pts) > 1 else [pts[0]]
    last_in: List[Optional[PricePoint]] = [None] * slots
    for p in pts[1:]:
        k = min(slots - 1, max(0, int((float(p.ts) - lo) / width)))
        last_in[k] = p  # sorted input: the last write is the slot's last point
    out: List[PricePoint] = [pts[0]]
    prev = pts[0]
    first_slot = min(slots - 1, max(0, int((float(pts[0].ts) - lo) / width)))
    for k in range(first_slot, slots):
        kept = last_in[k]
        if kept is not None:
            out.append(kept)
            prev = kept
        elif k < slots - 1:
            slot_end = lo + (k + 1) * width
            if slot_end > float(prev.ts) and slot_end < float(pts[-1].ts):
                out.append(PricePoint(ts=slot_end, price=prev.price, last=prev.last, bid=prev.bid, ask=prev.ask,
                                      source=prev.source))
    return out  # the first point plus at most one per slot; the last slot holds the last point


def hours_until(iso_date: Optional[str], now: float) -> Optional[float]:
    """Hours from ``now`` until an ISO-8601 timestamp (None if missing/unparseable)."""
    ts = parse_iso_ts(iso_date) if iso_date is not None else None
    if ts is None:
        return None
    return (ts - now) / 3600.0
