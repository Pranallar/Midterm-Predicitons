"""Replay stored history through the live signal and paper code (package C of docs/PAPER_TRADING.md §6.12).

:func:`run_backtest` walks a decision grid over a stored window. The window's history is loaded ONCE
into a :class:`PreloadedHistory` (one query per table and exchange; for the dashboard on a separate
read-only SQLite connection), and every decision-time read goes through a :class:`GuardedHistory`
view that returns only rows known at ``t`` (post-filtering what it returns, not only checking the
call's arguments). At every decision time ``t`` a :class:`HistoricalMarket` builds the same
:class:`~supermarket_bot.models.StrategyInputs` and :class:`~supermarket_bot.models.MarketObservation`
the live tracker would have had, and the same :func:`strategy.generate_signals` and
:class:`paper.PaperEngine` turn them into simulated orders and fills. Fills use the first
observation at least ``latency_s`` after the decision.

Honesty rules (revision 2): set ideas (baskets, arbitrage) are priced only from tick quotes of one
bulk snapshot, never from candles; synthetic books are never used for marks; candle fills come only
from 5-minute candles lying entirely inside the order's live interval and are flagged synthetic
(left out of verdict statistics); settlements become known at max(settled_on, detected_at); the
report opens with a per-kind testability table and says plainly when a window overlaps the live
paper run (agreement between the two is then not evidence) and that the backtest cannot evaluate
sizing policies.
"""

from __future__ import annotations

import bisect
import copy
import dataclasses
import itertools
import logging
import math
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from . import depth as _depth
from . import paper as _paper
from .models import (
    SURGE_CLOSED,
    SURGE_OPEN,
    BacktestConfig,
    BacktestReport,
    BookObservation,
    ExchangeInfo,
    FairValue,
    FairValueRecord,
    FairValueRefresh,
    LeaderboardSnapshot,
    MarketObservation,
    Opportunity,
    PaperConfig,
    PricePoint,
    Quote,
    SettlementInfo,
    StrategyInputs,
    StrategyParams,
    Surge,
    TradeRecord,
)

log = logging.getLogger("supermarket_bot")

MAX_SWEEP_RUNS = 50
SURGE_LOOKBACK_S = 30 * 3600.0  # detection reads this much history before t (like the tracker)
QUOTE_MAX_AGE_FACTOR = 2.0  # a tick older than this x step_s is not a current quote at t
CANDLE_QUOTE_MAX_AGE_S = 3600.0  # single-outcome ideas only: a candle closed at most this long ago
SYNTHETIC_DEPTH_CANDLE_S = 3600.0  # synthetic depth from the candles CLOSED by the observation time, this far back
CANDLE_FILL_RESOLUTION = "5m"
PERFORMANCE_TARGET_S = 60.0  # 24 h x 237 outcomes at step 300 s must finish within this (tests use a scaled store)
DASHBOARD_TIME_BUDGET_S = 120.0
DECISION_BOOK_MAX_AGE_S = 900.0  # decisions and marks: a real snapshot at most this old
SURGE_KEEP_S = 2 * 86400.0  # surges older than this are not strategy inputs (like pipeline.assemble_inputs)
BAND_REFRESH_S = 900.0  # high bands (6-h lookback) are recomputed at most this often per outcome
FV_LOOKBACK_S = 2 * 3600.0  # the newest fair-value record is searched this far back
LEADERBOARD_HISTORY_S = 7 * 86400.0
RESOLUTION_SECONDS: Dict[str, float] = {"1m": 60.0, "5m": 300.0, "1h": 3600.0, "1d": 86400.0, "1w": 604800.0}
REPORT_TRADES_MAX = 500
_EPS = 1e-9

SWEEP_WARNING = (
    "The best of {n} settings is an optimistic estimate (it was picked after seeing the results); "
    "prefer settings whose neighbours also do well, and confirm them on data recorded later."
)
OVERLAP_WARNING = (
    "{h:.1f} h of this window were also seen by the live paper run: a backtest over the same data is not "
    "an independent check, so agreement between the two is not evidence."
)
SIZING_ASSUMPTION = (
    "Depth before the bot started recording is synthetic (one level, a share of recent volume), so the "
    "backtest cannot tell the sizing policies apart: compare them in the live run."
)
NOT_REPLAYABLE_ARBITRAGE = (
    "Engine-reported arbitrage cannot be replayed: the engine's constraints and overround rows are not "
    "stored historically."
)


class LookAheadError(RuntimeError):
    """A replay asked for data newer than its current decision time."""


class HistorySource(Protocol):
    """Stored history (TrackerStore implements every method, §7.1; MemoryHistory for tests).
    Every range is inclusive and times are epoch seconds."""

    def exchanges(self) -> List[ExchangeInfo]: ...
    def series(self, exchange_id: str, since: float, until: Optional[float] = None, *, include_candles: bool = True) -> List[PricePoint]: ...
    def candles(self, exchange_id: str, resolution: str, since: float, until: Optional[float] = None) -> List[Dict[str, Any]]: ...
    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]: ...
    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None,
               limit: int = 200, until: Optional[float] = None) -> List[Surge]: ...
    def fair_value_history(self, since: float, until: Optional[float] = None, exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]: ...
    def fair_value_refreshes(self, since: float, until: Optional[float] = None) -> List[FairValueRefresh]: ...
    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]: ...
    def settlements(self) -> Dict[str, SettlementInfo]: ...
    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]: ...
    def tick_bounds(self) -> Optional[Tuple[float, float]]: ...




def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _candle(row: Mapping[str, Any], resolution: str) -> Dict[str, Any]:
    """A candle dict with ``close_ts`` (bucket start + length) filled in."""
    d = dict(row)
    ts = float(d.get("ts", d.get("time", 0.0)))
    d["ts"] = ts
    if d.get("close_ts") is None:
        d["close_ts"] = ts + RESOLUTION_SECONDS.get(resolution, 0.0)
    return d


def _between(keys: Sequence[float], lo: Optional[float], hi: Optional[float]) -> Tuple[int, int]:
    """Index range of sorted ``keys`` inside [lo, hi] (None = unbounded)."""
    i = 0 if lo is None else bisect.bisect_left(keys, lo - 1e-9)
    j = len(keys) if hi is None else bisect.bisect_right(keys, hi + 1e-9)
    return i, max(i, j)


def _settled_known_at(info: SettlementInfo) -> Optional[float]:
    """When a replay may know a settlement: max(settled_on, detected_at or settled_on) (D23)."""
    on = _num(info.settled_on)
    det = _num(info.detected_at)
    if on is None and det is None:
        return None
    if on is None:
        return det
    return max(on, det if det is not None else on)


class MemoryHistory:
    """A HistorySource over plain lists (tests, synthetic scenarios)."""

    def __init__(self, *, exchanges: Sequence[ExchangeInfo] = (), points: Optional[Mapping[str, Sequence[PricePoint]]] = None,
                 candles: Optional[Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]]] = None,
                 trades: Optional[Mapping[str, Sequence[TradeRecord]]] = None, surges: Sequence[Surge] = (),
                 fair_values: Sequence[FairValueRecord] = (), refreshes: Sequence[FairValueRefresh] = (),
                 books: Optional[Mapping[str, Sequence[BookObservation]]] = None,
                 settlements: Optional[Mapping[str, SettlementInfo]] = None,
                 leaderboards: Sequence[LeaderboardSnapshot] = ()) -> None:
        self._exchanges = list(exchanges)
        self._points = {str(k): sorted(v, key=lambda p: p.ts) for k, v in (points or {}).items()}
        self._candles: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for (eid, res), rows in (candles or {}).items():
            self._candles[(str(eid), str(res))] = sorted((_candle(r, str(res)) for r in rows), key=lambda c: c["ts"])
        self._trades = {str(k): sorted(v, key=lambda t: (t.ts, str(t.trade_id))) for k, v in (trades or {}).items()}
        self._surges = list(surges)
        self._fair_values = sorted(fair_values, key=lambda r: (r.ts, r.exchange_id))
        self._refreshes = sorted(refreshes, key=lambda r: r.ts)
        self._books = {str(k): sorted(v, key=lambda b: b.observed_at) for k, v in (books or {}).items()}
        self._settlements = {str(k): v for k, v in (settlements or {}).items()}
        self._leaderboards = sorted(leaderboards, key=lambda s: s.at)

    def exchanges(self) -> List[ExchangeInfo]:
        return list(self._exchanges)

    def series(self, exchange_id: str, since: float, until: Optional[float] = None, *,
               include_candles: bool = True) -> List[PricePoint]:
        hi = float("inf") if until is None else until
        return [copy.copy(p) for p in self._points.get(str(exchange_id), [])
                if since - 1e-9 <= p.ts <= hi + 1e-9 and (include_candles or p.source == "tick")]

    def candles(self, exchange_id: str, resolution: str, since: float, until: Optional[float] = None) -> List[Dict[str, Any]]:
        hi = float("inf") if until is None else until
        return [dict(c) for c in self._candles.get((str(exchange_id), str(resolution)), [])
                if since - 1e-9 <= c["ts"] <= hi + 1e-9]

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        hi = float("inf") if until is None else until
        return [copy.copy(t) for t in self._trades.get(str(exchange_id), []) if since - 1e-9 <= t.ts <= hi + 1e-9]

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None,
               limit: int = 200, until: Optional[float] = None) -> List[Surge]:
        rows = [s for s in self._surges
                if (since is None or s.detected_at >= since - 1e-9 or s.end_ts >= since - 1e-9)
                and (until is None or s.detected_at <= until + 1e-9)
                and (status is None or s.status == status)
                and (exchange_id is None or str(s.exchange_id) == str(exchange_id))]
        rows.sort(key=lambda s: (s.detected_at, s.id or 0), reverse=True)
        return [copy.deepcopy(s) for s in rows[: int(limit)]]

    def fair_value_history(self, since: float, until: Optional[float] = None,
                           exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]:
        hi = float("inf") if until is None else until
        wanted = {str(e) for e in exchange_ids} if exchange_ids is not None else None
        return [copy.deepcopy(r) for r in self._fair_values
                if since - 1e-9 <= r.ts <= hi + 1e-9 and (wanted is None or r.exchange_id in wanted)]

    def fair_value_refreshes(self, since: float, until: Optional[float] = None) -> List[FairValueRefresh]:
        hi = float("inf") if until is None else until
        return [copy.deepcopy(r) for r in self._refreshes if since - 1e-9 <= r.ts <= hi + 1e-9]

    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]:
        hi = float("inf") if until is None else until
        return [copy.copy(b) for b in self._books.get(str(exchange_id), []) if since - 1e-9 <= b.observed_at <= hi + 1e-9]

    def settlements(self) -> Dict[str, SettlementInfo]:
        return dict(self._settlements)

    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]:
        hi = float("inf") if until is None else until
        return [copy.deepcopy(s) for s in self._leaderboards if since - 1e-9 <= s.at <= hi + 1e-9]

    def tick_bounds(self) -> Optional[Tuple[float, float]]:
        ts = [p.ts for pts in self._points.values() for p in pts if p.source == "tick"]
        return (min(ts), max(ts)) if ts else None


def _safe(errors: List[str], label: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        out = fn()
        return default if out is None else out
    except (NotImplementedError, AttributeError) as exc:
        errors.append(f"The store cannot provide {label} ({type(exc).__name__}): replayed without it.")
        return default
    except Exception as exc:  # a broken table never stops the replay
        errors.append(f"Reading {label} failed ({exc}): replayed without it.")
        return default


class PreloadedHistory:
    """Everything a replay of ``[start - warmup, end]`` needs, read once from a HistorySource: per exchange
    the series (ticks and candles), 5m/1h candles, tape (with fetched_at), book snapshots; fair-value records
    and refreshes; surges (``until=end``, limit 100,000); settlements; leaderboards. Sorted lists with
    bisect access. Loading reads the whole window (that is not a decision); decisions read only through
    GuardedHistory."""

    def __init__(self, source: HistorySource, start: float, end: float, *, warmup_s: float = 6 * 3600.0,
                 exchange_ids: Optional[Sequence[str]] = None) -> None:
        self.start = float(start)
        self.end = float(end)
        self.warmup_s = float(warmup_s)
        self.lo = self.start - max(self.warmup_s, SURGE_LOOKBACK_S)
        self.errors: List[str] = []
        errs = self.errors
        infos = _safe(errs, "the outcome list", source.exchanges, [])
        if exchange_ids is not None:
            wanted = {str(e) for e in exchange_ids}
            infos = [i for i in infos if str(i.exchange_id) in wanted]
        self._exchanges: List[ExchangeInfo] = list(infos)
        self.exchange_ids: List[str] = [str(i.exchange_id) for i in self._exchanges]
        self._series: Dict[str, List[PricePoint]] = {}
        self._series_ts: Dict[str, List[float]] = {}
        self._ticks: Dict[str, List[PricePoint]] = {}
        self._ticks_ts: Dict[str, List[float]] = {}
        self._candles: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._candles_ts: Dict[Tuple[str, str], List[float]] = {}
        self._trades: Dict[str, List[TradeRecord]] = {}
        self._trades_ts: Dict[str, List[float]] = {}
        self._books: Dict[str, List[BookObservation]] = {}
        self._books_ts: Dict[str, List[float]] = {}
        for eid in self.exchange_ids:
            pts = sorted(_safe(errs, "price series", lambda: source.series(eid, self.lo, self.end), []),
                         key=lambda p: p.ts)
            self._series[eid] = pts
            self._series_ts[eid] = [p.ts for p in pts]
            ticks = [p for p in pts if getattr(p, "source", "tick") == "tick"]
            self._ticks[eid] = ticks
            self._ticks_ts[eid] = [p.ts for p in ticks]
            for res in ("5m", "1h"):
                rows = _safe(errs, "candles", lambda: source.candles(eid, res, self.lo - RESOLUTION_SECONDS[res], self.end), [])
                cs = sorted((_candle(r, res) for r in rows), key=lambda c: c["ts"])
                self._candles[(eid, res)] = cs
                self._candles_ts[(eid, res)] = [c["ts"] for c in cs]
            tr = sorted(_safe(errs, "stored trades", lambda: source.trades(eid, self.lo, self.end), []),
                        key=lambda t: (t.ts, str(t.trade_id)))
            self._trades[eid] = tr
            self._trades_ts[eid] = [t.ts for t in tr]
            bks = sorted(_safe(errs, "order-book snapshots", lambda: source.book_snapshots(eid, self.lo, self.end), []),
                         key=lambda b: b.observed_at)
            self._books[eid] = bks
            self._books_ts[eid] = [b.observed_at for b in bks]
        fv = _safe(errs, "fair-value history",
                   lambda: source.fair_value_history(self.lo - FV_LOOKBACK_S, self.end, exchange_ids=self.exchange_ids), [])
        self._fv: Dict[str, List[FairValueRecord]] = {}
        for r in sorted(fv, key=lambda r: (r.ts, r.exchange_id)):
            self._fv.setdefault(str(r.exchange_id), []).append(r)
        self._fv_ts = {k: [r.ts for r in v] for k, v in self._fv.items()}
        self._refreshes = sorted(_safe(errs, "fair-value refreshes",
                                       lambda: source.fair_value_refreshes(self.lo - FV_LOOKBACK_S, self.end), []),
                                 key=lambda r: r.ts)
        self._refresh_ts = [r.ts for r in self._refreshes]
        self._surges = list(_safe(errs, "surges", lambda: source.surges(since=self.lo - SURGE_KEEP_S, limit=100_000,
                                                                       until=self.end), []))
        self._settlements = dict(_safe(errs, "settlements", source.settlements, {}))
        lbs = _safe(errs, "leaderboard snapshots",
                    lambda: source.leaderboard_snapshots(self.lo - LEADERBOARD_HISTORY_S, self.end), [])
        self._leaderboards = sorted(lbs, key=lambda s: s.at)
        self._leaderboard_ts = [s.at for s in self._leaderboards]
        all_ticks = [ts for v in self._ticks_ts.values() for ts in v]
        self._tick_bounds = (min(all_ticks), max(all_ticks)) if all_ticks else None
        self._first_fetch_cache: Dict[str, Optional[float]] = {}
        self.errors = list(dict.fromkeys(errs))

    def covers(self, start: float, end: float, warmup_s: float) -> bool:
        lo = float(start) - max(float(warmup_s), SURGE_LOOKBACK_S)
        return self.lo <= lo + 1e-6 and self.end >= float(end) - 1e-6

    def has_tape(self, exchange_id: str, by: Optional[float] = None) -> bool:
        """Whether the store holds tape for this outcome (prints with a fetched_at), fetched by ``by`` when given
        (decision-time use must pass ``by=t``: tape stored later in the window is not known at t)."""
        first = self._first_fetch(str(exchange_id))
        if first is None:
            return False
        return by is None or first <= float(by) + 1e-9

    def _first_fetch(self, eid: str) -> Optional[float]:
        cache = self._first_fetch_cache
        if eid not in cache:
            stamps = [float(t.fetched_at) for t in self._trades.get(eid, []) if t.fetched_at is not None]
            cache[eid] = min(stamps) if stamps else None
        return cache[eid]

    # the HistorySource interface, over memory
    def exchanges(self) -> List[ExchangeInfo]:
        return list(self._exchanges)

    def series(self, exchange_id: str, since: float, until: Optional[float] = None, *,
               include_candles: bool = True) -> List[PricePoint]:
        eid = str(exchange_id)
        if include_candles:
            pts, keys = self._series.get(eid, []), self._series_ts.get(eid, [])
        else:
            pts, keys = self._ticks.get(eid, []), self._ticks_ts.get(eid, [])
        i, j = _between(keys, since, until)
        return pts[i:j]

    def candles(self, exchange_id: str, resolution: str, since: float, until: Optional[float] = None) -> List[Dict[str, Any]]:
        key = (str(exchange_id), str(resolution))
        rows, keys = self._candles.get(key, []), self._candles_ts.get(key, [])
        i, j = _between(keys, since, until)
        return rows[i:j]

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        eid = str(exchange_id)
        i, j = _between(self._trades_ts.get(eid, []), since, until)
        return self._trades.get(eid, [])[i:j]

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None,
               limit: int = 200, until: Optional[float] = None) -> List[Surge]:
        rows = [s for s in self._surges
                if (since is None or s.detected_at >= since - 1e-9 or s.end_ts >= since - 1e-9)
                and (until is None or s.detected_at <= until + 1e-9)
                and (status is None or s.status == status)
                and (exchange_id is None or str(s.exchange_id) == str(exchange_id))]
        rows.sort(key=lambda s: (s.detected_at, s.id or 0), reverse=True)
        return rows[: int(limit)]

    def fair_value_history(self, since: float, until: Optional[float] = None,
                           exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]:
        eids = [str(e) for e in exchange_ids] if exchange_ids is not None else sorted(self._fv)
        out: List[FairValueRecord] = []
        for eid in eids:
            i, j = _between(self._fv_ts.get(eid, []), since, until)
            out.extend(self._fv.get(eid, [])[i:j])
        out.sort(key=lambda r: (r.ts, r.exchange_id))
        return out

    def latest_fair_value(self, exchange_id: str, until: float, since: float) -> Optional[FairValueRecord]:
        eid = str(exchange_id)
        keys = self._fv_ts.get(eid, [])
        j = bisect.bisect_right(keys, until + 1e-9)
        if j <= 0 or keys[j - 1] < since - 1e-9:
            return None
        return self._fv[eid][j - 1]

    def fair_value_refreshes(self, since: float, until: Optional[float] = None) -> List[FairValueRefresh]:
        i, j = _between(self._refresh_ts, since, until)
        return self._refreshes[i:j]

    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]:
        eid = str(exchange_id)
        i, j = _between(self._books_ts.get(eid, []), since, until)
        return self._books.get(eid, [])[i:j]

    def settlements(self) -> Dict[str, SettlementInfo]:
        return dict(self._settlements)

    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]:
        i, j = _between(self._leaderboard_ts, since, until)
        return self._leaderboards[i:j]

    def tick_bounds(self) -> Optional[Tuple[float, float]]:
        return self._tick_bounds


class GuardedHistory:
    """A time-guarded view of a PreloadedHistory (or any HistorySource). ``set_time(t)`` moves the guard.
    Per method (§6.12.2):

    * ``series`` / ``candles`` / ``trades`` / ``book_snapshots`` / ``fair_value_history`` /
      ``fair_value_refreshes`` / ``leaderboard_snapshots``: ``until`` is required and must be <= t, else
      LookAheadError; returned rows are ALSO filtered: candles with ``close_ts > t`` dropped (a partial
      bucket), trades with ``ts > t`` or ``fetched_at`` None or > t dropped, fair-value records with
      ``ts > t`` dropped;
    * ``surges(since, until)``: ``until`` <= t required; rows with ``detected_at > t`` dropped; every
      returned surge is a copy with peak/status/current_price/reverted_fraction reset to detection-time
      values and ``attribution`` removed unless ``attribution.analyzed_at <= t``;
    * ``settlements()``: only those with ``max(settled_on, detected_at or settled_on) <= t``;
    * ``exchanges()``: allowed (metadata);
    * ``tick_bounds()``: allowed only before the first ``set_time`` (else LookAheadError).
    ``filtered`` counts rows dropped by post-filters (reported in coverage)."""

    def __init__(self, source: Any) -> None:
        self._src = source
        self._t: Optional[float] = None
        self._filtered = 0

    @property
    def source(self) -> Any:
        return self._src

    @property
    def time(self) -> Optional[float]:
        return self._t

    def set_time(self, t: float) -> None:
        self._t = float(t)

    @property
    def filtered(self) -> int:
        return self._filtered

    def _check(self, method: str, until: Optional[float]) -> float:
        if self._t is None:
            raise LookAheadError(f"{method}: no decision time set (call set_time first)")
        if until is None:
            raise LookAheadError(f"{method}: 'until' is required during a replay (decision time {self._t:.0f})")
        if float(until) > self._t + 1e-9:
            raise LookAheadError(f"{method}: until={float(until):.0f} is after the decision time {self._t:.0f}")
        return self._t

    def _keep(self, rows: Sequence[Any], ok: Callable[[Any], bool]) -> List[Any]:
        out = [r for r in rows if ok(r)]
        self._filtered += len(rows) - len(out)
        return out

    def exchanges(self) -> List[ExchangeInfo]:
        return self._src.exchanges()

    def tick_bounds(self) -> Optional[Tuple[float, float]]:
        if self._t is not None:
            raise LookAheadError("tick_bounds: the store's newest tick is not known at a decision time")
        return self._src.tick_bounds()

    def series(self, exchange_id: str, since: float, until: Optional[float] = None, *,
               include_candles: bool = True) -> List[PricePoint]:
        t = self._check("series", until)
        rows = self._src.series(exchange_id, since, until, include_candles=include_candles)
        return self._keep(rows, lambda p: p.ts <= t + 1e-9)

    def candles(self, exchange_id: str, resolution: str, since: float, until: Optional[float] = None) -> List[Dict[str, Any]]:
        t = self._check("candles", until)
        rows = [_candle(r, resolution) for r in self._src.candles(exchange_id, resolution, since, until)]
        return self._keep(rows, lambda c: float(c["close_ts"]) <= t + 1e-9)

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        t = self._check("trades", until)
        rows = self._src.trades(exchange_id, since, until)
        return self._keep(rows, lambda r: r.ts <= t + 1e-9 and r.fetched_at is not None and r.fetched_at <= t + 1e-9)

    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]:
        t = self._check("book_snapshots", until)
        rows = self._src.book_snapshots(exchange_id, since, until)
        return self._keep(rows, lambda b: b.observed_at <= t + 1e-9)

    def fair_value_history(self, since: float, until: Optional[float] = None,
                           exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]:
        t = self._check("fair_value_history", until)
        rows = self._src.fair_value_history(since, until, exchange_ids=exchange_ids)
        return self._keep(rows, lambda r: r.ts <= t + 1e-9)

    def latest_fair_value(self, exchange_id: str, until: float, since: float) -> Optional[FairValueRecord]:
        """The newest record with since <= ts <= until (until <= t), guarded like fair_value_history."""
        t = self._check("fair_value_history", until)
        fast = getattr(self._src, "latest_fair_value", None)
        if fast is not None:
            rec = fast(exchange_id, until, since)
        else:
            rows = self._src.fair_value_history(since, until, exchange_ids=[exchange_id])
            rec = rows[-1] if rows else None
        if rec is not None and rec.ts > t + 1e-9:
            self._filtered += 1
            return None
        return rec

    def fair_value_refreshes(self, since: float, until: Optional[float] = None) -> List[FairValueRefresh]:
        t = self._check("fair_value_refreshes", until)
        rows = self._src.fair_value_refreshes(since, until)
        return self._keep(rows, lambda r: r.ts <= t + 1e-9)

    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]:
        t = self._check("leaderboard_snapshots", until)
        rows = self._src.leaderboard_snapshots(since, until)
        return self._keep(rows, lambda s: s.at <= t + 1e-9)

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None,
               limit: int = 200, until: Optional[float] = None) -> List[Surge]:
        t = self._check("surges", until)
        rows = self._src.surges(since=since, status=None, exchange_id=exchange_id, limit=100_000, until=until)
        rows = self._keep(rows, lambda s: s.detected_at <= t + 1e-9)
        out: List[Surge] = []
        for s in rows:
            c = copy.deepcopy(s)
            c.peak_price = c.end_price
            c.current_price = c.end_price
            c.reverted_fraction = 0.0
            c.status = SURGE_OPEN
            att = c.attribution
            if att is not None and (att.analyzed_at is None or att.analyzed_at > t + 1e-9):
                c.attribution = None
                self._filtered += 1
            if status is not None and c.status != status:
                continue
            out.append(c)
        return out[: int(limit)]

    def settlements(self) -> Dict[str, SettlementInfo]:
        if self._t is None:
            raise LookAheadError("settlements: no decision time set (call set_time first)")
        t = self._t
        out: Dict[str, SettlementInfo] = {}
        for eid, info in self._src.settlements().items():
            known = _settled_known_at(info)
            if known is not None and known <= t + 1e-9:
                out[str(eid)] = info
            else:
                self._filtered += 1
        return out


def replay_fair_value(record: FairValueRecord, refresh: Optional[FairValueRefresh], t: float, latency_s: float) -> FairValue:
    """The FairValue a live decision at ``t`` would have seen from the newest record with ``ts <= t - latency``
    (§6.12.2): value/bid/ask/usable/confidence from the record; ``as_of`` = the record's live ``as_of``,
    advanced to the oldest ``fetched_at`` of the record's venues in ``refresh`` (the newest refresh row with
    ``ts <= t - latency``) when that refresh is newer than the record and every one of those venues was "ok"
    in it; ``uncertainty`` / ``prev_value`` / ``suspect`` from ``detail``; source "history" records get
    ``history=True`` and ``as_of`` = the bar end. Manual values: availability is the record ts, never the
    user's updated_at."""
    detail = dict(record.detail or {})
    history = str(record.source) == "history"
    as_of = _num(record.as_of)
    if as_of is None:
        as_of = _num(detail.get("as_of"))
    if as_of is None:
        as_of = float(record.ts)
    venues = [str(v) for v in (record.venues or detail.get("venues") or [])]
    confirmed = False
    if not history and refresh is not None and venues and float(refresh.ts) > float(record.ts) + 1e-9:
        rows = refresh.venues or {}
        ok = all(str((rows.get(v) or {}).get("status")) == "ok" for v in venues)
        if ok:
            stamps = [_num((rows.get(v) or {}).get("fetched_at")) for v in venues]
            fetched = [s if s is not None else float(refresh.ts) for s in stamps]
            if fetched:
                newer = min(fetched)
                if newer > as_of:
                    as_of = newer
                    confirmed = True
    match_conf = _num(detail.get("match_confidence"))
    if match_conf is None:
        match_conf = 1.0 if record.usable else 0.0
    reason = ("replayed from imported outside history (indicative)" if history else
              ("replayed: re-confirmed by the refresh at " if confirmed else "replayed from the record at ")
              + f"{float(record.ts):.0f}")
    return FairValue(
        exchange_id=str(record.exchange_id), value=_num(record.value), source=str(record.source),
        confidence=str(record.confidence or "low"), usable=bool(record.usable), reason=reason,
        bid=_num(record.bid), ask=_num(record.ask), as_of=as_of, agreement=_num(record.agreement),
        match_kind=str(detail.get("match_kind") or ("MANUAL" if record.source == "manual" else "EXACT")),
        match_confidence=float(match_conf), uncertainty=_num(detail.get("uncertainty")),
        prev_value=_num(detail.get("prev_value")), prev_as_of=_num(detail.get("prev_as_of")),
        suspect=bool(detail.get("suspect", False)), history=history,
    )


def _stored_book(book: BookObservation) -> Dict[str, Any]:
    return {"at": book.observed_at, "bids": [[p, q] for p, q in book.bids], "asks": [[p, q] for p, q in book.asks]}


class HistoricalMarket:
    """Builds inputs and observations at decision times from a GuardedHistory (§6.12.2). Keeps one
    incremental ``analytics._SurgeScanner`` per exchange over the preloaded series (prefix-safe: the
    decision at t uses only points <= t) instead of re-reading 30 h of series per step."""

    def __init__(self, history: GuardedHistory, config: BacktestConfig, preloaded: Optional[PreloadedHistory] = None) -> None:
        self.history = history
        self.config = config
        self.pre = preloaded
        self.engine: Any = None  # set by run_backtest: candle fills need the engine's resting orders
        base = (config.paper.params if config.paper is not None and config.paper.params is not None else StrategyParams())
        self.params = _apply_params(base, config.params_overrides or {})
        self.regime = config.paper.regime if config.paper is not None else "unknown"
        self._infos: Dict[str, ExchangeInfo] = {str(i.exchange_id): i for i in history.exchanges()}
        self._eids = sorted(self._infos, key=_eid_key)
        self._races: Dict[str, Any] = {}
        try:
            from .fairvalue import races_for

            self._races = dict(races_for(list(self._infos.values())))
        except Exception as exc:  # fair value module unavailable: baskets need race groups, so say so
            log.warning("backtest: race keys unavailable: %s", exc)
        try:
            from .strategy import cup_end_ts

            self._cup_end = float(cup_end_ts(None))
        except Exception:
            self._cup_end = 1793811600.0  # 2026-11-04T17:00:00Z
        self._window: Optional[Tuple[float, float]] = None
        self._cache_t: Optional[float] = None
        self._cache: Dict[str, Any] = {}
        self._scanners: Dict[str, Any] = {}
        self._surges: Dict[str, List[Surge]] = {}
        self._surge_seq = 0
        self._bands: Dict[str, Tuple[float, Any]] = {}
        self._steps = 0
        self.counters: Dict[str, int] = {
            "quote_pairs": 0, "tick_quotes": 0, "candle_quotes": 0, "book_fills_real": 0, "book_fills_synthetic": 0,
            "book_fills_none": 0, "tape_stored": 0, "tape_candle": 0, "fv_steps": 0, "fv_recorded_steps": 0,
            "fv_history_steps": 0, "tick_steps": 0, "depth_steps": 0, "candle_only_steps": 0, "quote_steps": 0,
            "set_tick_steps": 0,
        }

    # ------------------------------------------------------------------ window and grid
    def window(self) -> Tuple[float, float]:
        """(start, end) of the replay: config values, else the store's tick bounds (end) and end - hours."""
        if self._window is not None:
            return self._window
        cfg = self.config
        end = cfg.end
        if end is None:
            bounds = self.history.tick_bounds()
            if bounds is None:
                raise ValueError("No stored ticks: there is nothing to replay.")
            end = bounds[1]
        start = cfg.start if cfg.start is not None else float(end) - float(cfg.hours) * 3600.0
        self._window = (float(start), float(end))
        return self._window

    def decision_times(self) -> List[float]:
        start, end = self.window()
        step = float(self.config.step_s)
        if step <= 0:
            raise ValueError("step_s must be positive")
        first = math.ceil(start / step - 1e-9) * step  # on the step grid: the surge scanner's grid (prefix-exact)
        n = int(math.floor((end - first) / step + 1e-9)) + 1
        return [first + i * step for i in range(max(0, n))]

    # ------------------------------------------------------------------ per-decision snapshot
    def _snapshot(self, t: float) -> Dict[str, Any]:
        if self._cache_t is not None and abs(self._cache_t - t) < 1e-9:
            return self._cache
        cfg = self.config
        h = self.history
        step = float(cfg.step_s)
        quotes: Dict[str, PricePoint] = {}
        n_ticks = n_candles = 0
        depth_any = False
        tick_ts: List[float] = []
        for eid in self._eids:
            ticks = h.series(eid, t - QUOTE_MAX_AGE_FACTOR * step, t, include_candles=False)
            point: Optional[PricePoint] = None
            for p in reversed(ticks):
                if p.bid is not None or p.ask is not None or p.price is not None:
                    point = PricePoint(ts=p.ts, price=p.price, last=p.last, bid=p.bid, ask=p.ask, source="tick")
                    break
            if point is not None:
                n_ticks += 1
                tick_ts.append(point.ts)
            else:
                point = self._candle_quote(eid, t)
                if point is not None:
                    n_candles += 1
            if point is None:
                continue
            if cfg.use_book_snapshots:
                books = h.book_snapshots(eid, t - DECISION_BOOK_MAX_AGE_S, t)
                if books:
                    point.book = _stored_book(books[-1])
                    depth_any = True
            quotes[eid] = point
        c = self.counters
        c["quote_pairs"] += n_ticks + n_candles
        c["tick_quotes"] += n_ticks
        c["candle_quotes"] += n_candles
        if quotes:
            c["quote_steps"] += 1
        if n_ticks >= 2:
            c["tick_steps"] += 1
            tick_ts.sort()
            if any(tick_ts[i + 1] - tick_ts[i] <= self.params.same_snapshot_s + 1e-9 for i in range(len(tick_ts) - 1)):
                c["set_tick_steps"] += 1
        if not depth_any and self.pre is not None:
            depth_any = any(self.pre.has_tape(e, by=t) for e in quotes)
        if depth_any:
            c["depth_steps"] += 1
        elif quotes:
            c["candle_only_steps"] += 1
        fvs = self._fair_values(t, quotes)
        settled = h.settlements()
        self._cache_t = t
        self._cache = {"quotes": quotes, "fair_values": fvs, "settlements": settled}
        self._steps += 1
        return self._cache

    def _candle_quote(self, eid: str, t: float) -> Optional[PricePoint]:
        cfg = self.config
        h = self.history
        best: Optional[Dict[str, Any]] = None
        for res in ("5m", "1h"):
            length = RESOLUTION_SECONDS[res]
            rows = h.candles(eid, res, t - CANDLE_QUOTE_MAX_AGE_S - length, t)
            for row in reversed(rows):
                if _num(row.get("close")) is None:
                    continue
                if float(row["close_ts"]) >= t - CANDLE_QUOTE_MAX_AGE_S - 1e-9:
                    if best is None or float(row["close_ts"]) > float(best["close_ts"]):
                        best = row
                break
        if best is None:
            return None
        close = float(best["close"])
        half = float(cfg.assumed_spread) / 2.0
        bid = _depth.clamp_price(_depth.floor_tick(close - half))
        ask = _depth.clamp_price(_depth.ceil_tick(close + half))
        return PricePoint(ts=float(best["close_ts"]), price=close, last=close, bid=bid, ask=ask, source="candle")

    def _fair_values(self, t: float, quotes: Mapping[str, PricePoint]) -> Dict[str, FairValue]:
        cfg = self.config
        out: Dict[str, FairValue] = {}
        if not cfg.use_fair_values:
            return out
        h = self.history
        lat = float(cfg.latency_s)
        until = t - lat
        refresh_rows = h.fair_value_refreshes(until - FV_LOOKBACK_S, until)
        refresh = refresh_rows[-1] if refresh_rows else None
        rec_any = hist_any = False
        for eid in self._eids:
            rec = h.latest_fair_value(eid, until, until - FV_LOOKBACK_S)
            if rec is None:
                continue
            if str(rec.source) == "history" and not cfg.use_history_fair_values:
                continue
            fv = replay_fair_value(rec, refresh, t, lat)
            race = self._races.get(eid)
            if race is not None:
                fv.race_key, fv.party = race.race_key, race.party
            out[eid] = fv
            if fv.usable and fv.value is not None:
                if fv.history:
                    hist_any = True
                else:
                    rec_any = True
        if rec_any or hist_any:
            self.counters["fv_steps"] += 1
        if rec_any:
            self.counters["fv_recorded_steps"] += 1
        if hist_any:
            self.counters["fv_history_steps"] += 1
        return out

    # ------------------------------------------------------------------ surges and bands
    def _scanner(self, eid: str) -> Any:
        sc = self._scanners.get(eid)
        if sc is None:
            from .analytics import SURGE_WINDOWS, _Series, _SurgeScanner

            pts = self.pre.series(eid, -float("inf"), None) if self.pre is not None else self.history.source.series(
                eid, -float("inf"), None)
            sc = _SurgeScanner(_Series(pts), float(self.config.step_s), SURGE_WINDOWS)
            self._scanners[eid] = sc
        return sc

    def _detect(self, t: float, quotes: Mapping[str, PricePoint]) -> List[Surge]:
        from .analytics import update_surge_status

        out: List[Surge] = []
        for eid in self._eids:
            info = self._infos[eid]
            kept = self._surges.setdefault(eid, [])
            if eid in quotes:
                found = self._scanner(eid).detect(t, eid, str(info.market_id))
                for s in found:
                    self._record(eid, s, t)
            mark = quotes[eid].price if eid in quotes else None
            alive: List[Surge] = []
            for s in kept:
                if t - s.detected_at > SURGE_KEEP_S:
                    continue
                if eid in quotes:
                    update_surge_status(s, mark, t)
                elif s.status != SURGE_CLOSED:
                    s.status = SURGE_CLOSED
                if s.attribution is None and s.status == SURGE_OPEN:
                    att = self._stored_attribution(eid, s, t)  # analysed live after our detection: usable from now on
                    if att is not None:
                        s.id, s.attribution = att
                alive.append(s)
            self._surges[eid] = alive
            out.extend(copy.copy(s) for s in alive if s.status != SURGE_CLOSED)
        return out

    def _record(self, eid: str, surge: Surge, t: float) -> None:
        kept = self._surges.setdefault(eid, [])
        for old in reversed(kept):
            if old.direction != surge.direction and old.status == SURGE_OPEN and surge.start_ts >= old.start_ts - 1e-9:
                lo, hi = sorted((old.start_price, old.peak_price))
                if lo - 1e-9 <= surge.end_price <= hi + 1e-9:
                    return  # the old move giving itself back: its status reports that
        for old in reversed(kept):
            if old.direction != surge.direction or surge.start_ts > old.end_ts + 1e-9:
                continue
            if old.status != SURGE_OPEN:
                return
            up = surge.direction != "down"
            if (surge.end_price > old.peak_price + 1e-9) if up else (surge.end_price < old.peak_price - 1e-9):
                old.end_ts, old.end_price = surge.end_ts, surge.end_price
                old.change = round(old.end_price - old.start_price, 6)
                old.peak_price = surge.end_price
            return
        self._surge_seq += 1
        surge.id = 1_000_000_000 + self._surge_seq
        att = self._stored_attribution(eid, surge, t)
        if att is not None:
            surge.id, surge.attribution = att
        kept.append(surge)

    def _stored_attribution(self, eid: str, surge: Surge, t: float) -> Optional[Tuple[int, Any]]:
        """An attribution from a stored surge on the same exchange and direction detected inside this
        detection's window and analysed by ``t`` (never the stored surge's later fields)."""
        rows = self.history.surges(since=surge.start_ts - float(self.config.step_s), exchange_id=eid, until=t)
        for s in rows:  # newest first
            if s.direction != surge.direction or s.attribution is None:
                continue
            if surge.start_ts - float(self.config.step_s) - 1e-9 <= s.detected_at <= t + 1e-9:
                return (int(s.id) if s.id is not None else surge.id or 0), s.attribution
        return None

    def _bands_at(self, t: float, quotes: Mapping[str, PricePoint]) -> List[Any]:
        from .analytics import high_band

        out: List[Any] = []
        for eid, point in quotes.items():
            mark = _num(point.price)
            if mark is None or max(mark, 1.0 - mark) < 0.95 - 1e-9:
                self._bands.pop(eid, None)
                continue
            cached = self._bands.get(eid)
            if cached is not None and t - cached[0] < BAND_REFRESH_S - 1e-9:
                band = cached[1]
            else:
                pts = self.history.series(eid, t - 6 * 3600.0 - 1200.0, t)
                info = self._infos[eid]
                band = high_band(pts, t, eid, str(info.market_id), settlement_date=info.settlement_date)
                self._bands[eid] = (t, band)
            if band is not None:
                out.append(band)
        return out

    # ------------------------------------------------------------------ builders
    def _leaderboard(self, t: float) -> Tuple[Optional[LeaderboardSnapshot], List[LeaderboardSnapshot]]:
        rows = self.history.leaderboard_snapshots(t - LEADERBOARD_HISTORY_S, t)
        return (rows[-1] if rows else None), rows

    def inputs(self, t: float) -> StrategyInputs:
        snap = self._snapshot(t)
        quotes: Dict[str, PricePoint] = snap["quotes"]
        settled = snap["settlements"]
        open_quotes = {e: copy.copy(p) for e, p in quotes.items() if e not in settled}
        infos = {e: self._infos[e] for e in open_quotes}
        surges = self._detect(t, open_quotes)
        bands = self._bands_at(t, open_quotes)
        lb, history = self._leaderboard(t)
        initial = _num(lb.initial_balance) if lb is not None else None
        leader = None
        if lb is not None and lb.entries:
            top = lb.entries[0]
            leader = _num(top.get("value"))
            if leader is None and initial is not None and _num(top.get("pnl")) is not None:
                leader = initial + float(top["pnl"])
        bar = None
        try:
            from .sizing import estimate_bar

            bar = estimate_bar(lb, history, initial, max(0.0, (self._cup_end - t) / 86400.0))
        except Exception as exc:
            log.debug("backtest: bar estimate failed: %s", exc)
        mids: Dict[str, List[Tuple[float, float]]] = {}
        for eid in open_quotes:
            rows = self.history.series(eid, t - 300.0 + 1e-6, t, include_candles=False)
            pts = [(p.ts, (float(p.bid) + float(p.ask)) / 2.0) for p in rows if p.bid is not None and p.ask is not None]
            if pts:
                mids[eid] = pts
        return StrategyInputs(
            now=float(t), cup_end=self._cup_end, infos=infos, latest=open_quotes, surges=surges, bands=bands,
            constraints=None, overround_rows=[], fair_values={e: v for e, v in snap["fair_values"].items() if e in infos},
            races={e: r for e, r in self._races.items() if e in infos}, backtest=None, balance=None,
            initial_balance=initial, account_value=None, leader_value=leader,
            my_rank=lb.my_rank if lb is not None else None, leaderboard=lb, bar=bar, settlement_regime=self.regime,
            params=self.params, recent_mids=mids, resting_exchanges=set(),
        )

    def observation(self, t: float) -> MarketObservation:
        """Quotes, books and tape known at ``t`` (books/tape requested by the engine's read plan are
        provided by ``fulfil``)."""
        snap = self._snapshot(t)
        quotes: Dict[str, PricePoint] = snap["quotes"]
        settled = snap["settlements"]
        pending = self._pending_exchanges()
        books: Dict[str, BookObservation] = {}
        if self.config.use_book_snapshots:
            for eid in quotes:
                if eid in pending:
                    continue  # a pending order fills from the FIRST observation after its decision (via fulfil)
                rows = self.history.book_snapshots(eid, t - DECISION_BOOK_MAX_AGE_S, t)
                if rows:
                    books[eid] = rows[-1]
        open_ids = {e for e in quotes if e not in settled}
        return MarketObservation(
            now=float(t), cup_end=self._cup_end,
            quotes={e: Quote(e, p.bid, p.ask, p.last, float(p.ts)) for e, p in quotes.items()}, books=books, trades={},
            settlements=dict(settled), open_ids=open_ids, infos={e: self._infos[e] for e in quotes},
            fair_values={e: v for e, v in snap["fair_values"].items() if e in quotes},
            races={e: r for e, r in self._races.items() if e in quotes},
        )

    def _pending_exchanges(self) -> Set[str]:
        out: Set[str] = set()
        eng = self.engine
        if eng is None:
            return out
        for o in eng.orders():
            if o.status == "pending":
                out.add(o.exchange_id)
        return out

    def _first_tick_book(self, eid: str, after: float, t: float) -> Optional[BookObservation]:
        cfg = self.config
        rows = self.history.series(eid, after, t, include_candles=False)
        tick = next((p for p in rows if p.ts >= after - 1e-9 and p.bid is not None and p.ask is not None), None)
        if tick is None:
            return None
        volume = 0.0
        found = False
        for c in self.history.candles(eid, "1h", tick.ts - SYNTHETIC_DEPTH_CANDLE_S - 3600.0, tick.ts):
            close_ts = float(c["close_ts"])
            if tick.ts - SYNTHETIC_DEPTH_CANDLE_S - 1e-9 <= close_ts <= tick.ts + 1e-9:
                volume += float(_num(c.get("volume")) or 0.0)
                found = True
        qty = math.floor(min(float(cfg.assumed_touch_qty), float(cfg.volume_share) * volume) + 1e-9) if found else 0.0
        return BookObservation(exchange_id=eid, observed_at=float(tick.ts), bids=[(float(tick.bid), float(qty))],
                               asks=[(float(tick.ask), float(qty))], source="synthetic")

    def _candle_prints(self, eid: str, t: float) -> List[TradeRecord]:
        """Synthetic trade-through prints from 5m candles lying inside each resting order's live interval
        (D21), one per (candle, order), flagged by their id (``candle:<eid>:<bucket>:<order id>``)."""
        cfg = self.config
        eng = self.engine
        if eng is None:
            return []
        out: List[TradeRecord] = []
        for o in eng.orders():
            if o.exchange_id != eid or o.status not in ("resting", "cancelling"):
                continue
            lat = float(o.latency_s or 0.0)
            start = o.created_at + lat
            if o.last_trade_ts is not None:  # never re-cover a period whose prints were already processed
                start = max(start, float(o.last_trade_ts))
            end = t
            if o.status == "cancelling" and o.cancel_at is not None:
                end = min(end, float(o.cancel_at))
            if o.expires_at is not None:
                end = min(end, float(o.expires_at))
            if end <= start:
                continue
            p = o.limit_price if o.side == "yes" else 1.0 - o.limit_price
            for c in self.history.candles(eid, CANDLE_FILL_RESOLUTION, start, end):
                if float(c["ts"]) < start - 1e-9 or float(c["close_ts"]) > end + 1e-9:
                    continue
                low, high, volume = _num(c.get("low")), _num(c.get("high")), _num(c.get("volume")) or 0.0
                if low is None or high is None:
                    continue
                span = high - low
                if o.side == "yes":
                    if not low < p - 1e-9:
                        continue
                    frac = 1.0 if span <= 1e-9 else min(1.0, (p - low) / span)
                    price = low
                else:
                    if not high > p + 1e-9:
                        continue
                    frac = 1.0 if span <= 1e-9 else min(1.0, (high - p) / span)
                    price = high
                qty = math.floor(min(float(cfg.candle_fill_cap), float(cfg.volume_share) * volume * frac) + 1e-9)
                if qty < 1:
                    continue
                out.append(TradeRecord(trade_id=f"candle:{eid}:{int(c['ts'])}:{o.order_id}", exchange_id=eid,
                                       ts=float(c["close_ts"]), price=price, size=float(qty), side=None,
                                       fetched_at=float(c["close_ts"])))
        out.sort(key=lambda r: (r.ts, r.trade_id))
        return out

    def fulfil(self, plan: Any, t: float) -> Any:
        """The backtest's ``paper.FulfilFn`` (returns a paper.Fulfilment): for each book request the first real
        snapshot with ``observed_at >= request.after`` and ``<= t``, else a synthetic book built from the first
        tick at or after ``after`` with depth from candles closed by that tick's time; marks and candidates:
        the newest real snapshot at most 900 s old (synthetic books are never returned for marks); for each
        tape request the stored prints in ``(since, t]`` fetched by t, else candle trade-throughs (§6.12.2,
        flagged synthetic). Applies the same per-step read caps and group rule as the live runner."""
        cfg = self.config
        pcfg = self.engine.config if self.engine is not None else cfg.paper
        h = self.history
        counters = self.counters

        def read_book(r: Any) -> Optional[BookObservation]:
            eid = r.exchange_id
            if r.after is not None:
                if cfg.use_book_snapshots:
                    rows = h.book_snapshots(eid, float(r.after), t)
                    if rows:
                        counters["book_fills_real"] += 1
                        return rows[0]
                book = self._first_tick_book(eid, float(r.after), t)
                counters["book_fills_synthetic" if book is not None else "book_fills_none"] += 1
                return book
            if not cfg.use_book_snapshots:
                return None
            rows = h.book_snapshots(eid, t - DECISION_BOOK_MAX_AGE_S, t)
            return rows[-1] if rows else None

        def read_tape(r: Any, pages: int) -> Optional[Any]:
            eid = r.exchange_id
            since = float(r.since) if r.since is not None else t - float(pcfg.trade_poll_s)
            rows = [x for x in h.trades(eid, since, t) if x.ts > since + 1e-9]  # guarded: fetched by t
            if rows:
                counters["tape_stored"] += 1
                return _paper.TapeRead(trades=rows, truncated=False, pages=1)
            # nothing stored for (since, t]: either no trades or not recorded; candle trade-throughs are
            # possible only where trades happened, and they are flagged synthetic (D21)
            counters["tape_candle"] += 1
            return _paper.TapeRead(trades=self._candle_prints(eid, t), truncated=False, pages=1)

        return _paper.fulfil_plan(plan, read_book=read_book, read_tape=read_tape,
                                  max_books=int(pcfg.max_book_reads_per_step), max_pages=int(pcfg.max_trade_reads_per_step),
                                  max_tape_pages=int(pcfg.max_tape_pages), room=None)

    # ------------------------------------------------------------------ report parts
    def coverage(self) -> Dict[str, Any]:
        c = self.counters
        pairs = max(1, c["quote_pairs"])
        book_fills = c["book_fills_real"] + c["book_fills_synthetic"]
        tape = c["tape_stored"] + c["tape_candle"]
        steps = max(1, self._steps)
        maker_tape = maker_candle = 0
        if self.engine is not None:
            rows: List[Any] = []
            try:  # every fill of the replay (the engine keeps only the newest in memory)
                if self.engine.run_id is not None:
                    rows = [_paper._fill_from(d) for d in self.engine.persistence.paper_fills(self.engine.run_id, limit=None)]
            except Exception:
                rows = []
            if not rows:
                rows = self.engine.fills(limit=None)
            for f in rows:
                if f.liquidity != "maker":
                    continue
                if any(str(i).startswith("candle:") for i in f.trade_ids):
                    maker_candle += 1
                else:
                    maker_tape += 1
        return {
            "exchanges": len(self._eids), "decision_steps": self._steps,
            "tick_share": round(c["tick_quotes"] / pairs, 6) if c["quote_pairs"] else 0.0,
            "candle_share": round(c["candle_quotes"] / pairs, 6) if c["quote_pairs"] else 0.0,
            "book_snapshot_share": round(c["book_fills_real"] / book_fills, 6) if book_fills else None,
            "synthetic_book_share": round(c["book_fills_synthetic"] / book_fills, 6) if book_fills else None,
            "fair_value_share": round(c["fv_steps"] / steps, 6),
            "fair_value_recorded_share": round(c["fv_recorded_steps"] / steps, 6),
            "fair_value_imported_share": round(c["fv_history_steps"] / steps, 6),
            "tape_share": round(c["tape_stored"] / tape, 6) if tape else None,
            "maker_fills_tape": maker_tape, "maker_fills_candle": maker_candle,
            "set_tick_share": round(c["set_tick_steps"] / steps, 6),
            "guard_filtered": self.history.filtered,
        }

    def assumptions(self) -> List[str]:
        cfg = self.config
        pcfg = self.engine.config if self.engine is not None else cfg.paper
        human = [p for p in pcfg.portfolios if p.portfolio_id.startswith("human:")]
        human_lat = (human[0].min_latency_s if human and human[0].min_latency_s is not None else None)
        out = [
            f"Decisions every {cfg.step_s:.0f} s; fills use the first order book observed at least {cfg.latency_s:.0f} s "
            "after a decision" + (f" (the headline portfolio, you acting by hand, waits {human_lat:.0f} s)"
                                  if human_lat is not None else "") + ".",
            f"Quotes come from stored ticks no older than {QUOTE_MAX_AGE_FACTOR * cfg.step_s:.0f} s; where only candles "
            f"exist, single-outcome ideas use the candle close -/+ half an assumed {cfg.assumed_spread:.3f} spread "
            "(candle quotes never price a basket or arbitrage set).",
            ("Depth comes from stored order-book snapshots where they exist; otherwise a synthetic one-level book at the "
             f"tick's bid and ask holds min({cfg.assumed_touch_qty:.0f}, {cfg.volume_share:.0%} of the last hour's volume) "
             "shares, and none without a closed candle. Synthetic books fill orders but never value positions."
             if cfg.use_book_snapshots else
             f"Stored order books are ignored (--no-books): fills use a synthetic one-level book of min({cfg.assumed_touch_qty:.0f}, "
             f"{cfg.volume_share:.0%} of the last hour's volume) shares at the tick's bid and ask."),
            "Resting orders fill from stored tape fetched by the decision time; where none was stored, from 5-minute "
            f"candles trading through the price inside the order's life (at most {cfg.candle_fill_cap:.0f} shares and "
            f"{cfg.volume_share:.0%} of the candle's volume), flagged as assumed and left out of the verdict.",
            SIZING_ASSUMPTION,
            ("Fair values replay only what the bot recorded (or imported: imported Polymarket history is indicative, "
             "not executable), each available only after the decision latency." if cfg.use_fair_values else
             "Fair values are not replayed (--no-fair-values): value ideas cannot trade."),
            "The stored tape is sparse: the tracker stores trades only for surging outcomes and paper orders.",
            "Surges are re-detected at each decision time; a stored attribution is used only once it was analysed.",
            NOT_REPLAYABLE_ARBITRAGE,
        ]
        return out

    def testability(self) -> Dict[str, Dict[str, Any]]:
        """Per trade kind: ``{"status", "hours", "sentence"}`` (§6.12.3), e.g. value "not_testable" with
        "value: not testable yet (0 h of outside prices recorded or imported in this window)"."""
        c = self.counters
        steps = self._steps
        per = float(self.config.step_s) / 3600.0
        total_h = steps * per

        def status(hours: float) -> str:
            if steps and hours >= 0.9 * total_h - 1e-9 and hours > 0:
                return "testable"
            return "partial" if hours > 0 else "not_testable"

        fv_h = c["fv_steps"] * per
        hist_h = c["fv_history_steps"] * per
        if fv_h <= 0:
            value_sentence = ("value: not testable yet (0 h of outside prices recorded or imported in this window); run "
                              "`fairvalue --import-history` or let the bot record for a day")
        else:
            value_sentence = f"value: {fv_h:.1f} h of outside prices recorded or imported in this {total_h:.1f}-hour window"
            if hist_h > 0:
                value_sentence += f" ({hist_h:.1f} h from imported history, indicative)"
        tick_h = c["tick_steps"] * per
        depth_h = c["depth_steps"] * per
        candle_h = c["candle_only_steps"] * per
        quote_h = c["quote_steps"] * per
        depth_status = "testable" if steps and depth_h >= 0.9 * total_h - 1e-9 and depth_h > 0 else (
            "partial" if depth_h > 0 or candle_h > 0 else "not_testable")
        return {
            "value": {"status": status(fv_h), "hours": round(fv_h, 3), "sentence": value_sentence},
            "basket": {"status": status(tick_h), "hours": round(tick_h, 3),
                       "sentence": f"basket: {tick_h:.1f} h of tick quotes; candle-only hours cannot price a set"},
            "hole": {"status": depth_status, "hours": round(depth_h, 3),
                     "sentence": f"hole: {depth_h:.1f} h with stored tape or real order books; {candle_h:.1f} h "
                                 "candle-only (passive fills there are assumed from candles)"},
            "fade": {"status": depth_status, "hours": round(depth_h, 3),
                     "sentence": f"fade: {depth_h:.1f} h with stored tape or real order books; {candle_h:.1f} h "
                                 "candle-only (fills there use synthetic books)"},
            "carry": {"status": "testable" if quote_h > 0 else "not_testable", "hours": round(quote_h, 3),
                      "sentence": "carry: testable from quotes, but it pays at resolution: a one-day replay shows only "
                                  "its liquidation value"},
            "arbitrage": {"status": "not_replayable", "hours": 0.0, "sentence": NOT_REPLAYABLE_ARBITRAGE},
        }


def _eid_key(eid: Any) -> Tuple[int, int, str]:
    s = str(eid)
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


def _apply_params(base: StrategyParams, overrides: Mapping[str, Any]) -> StrategyParams:
    names = {f.name for f in dataclasses.fields(StrategyParams)}
    bad = sorted(k for k in overrides if k not in names)
    if bad:
        raise ValueError(f"unknown strategy parameter(s) {', '.join(bad)}")
    return dataclasses.replace(base, **dict(overrides)) if overrides else copy.deepcopy(base)


def backtest_paper_config(config: BacktestConfig) -> PaperConfig:
    """The PaperConfig a replay runs with (§6.12.1): regime/params overrides applied; every portfolio's latency is
    max(its own, config.latency_s) (the human headline keeps 240 s); max_fill_delay_s = max(its own, step_s +
    latency); interval_s = step_s; set entries accept books up to one step old; the engine's quote staleness rule
    is relaxed to the adapter's own quote rules (ticks <= 2 x step_s, candles <= 1 h)."""
    base = copy.deepcopy(config.paper if config.paper is not None else PaperConfig())
    params = _apply_params(base.params or StrategyParams(), config.params_overrides or {})
    lat = float(config.latency_s)
    step = float(config.step_s)
    ports = []
    for spec in base.portfolios:
        own_lat = float(spec.min_latency_s if spec.min_latency_s is not None else base.min_latency_s)
        new_lat = max(own_lat, lat)
        own_delay = float(spec.max_fill_delay_s if spec.max_fill_delay_s is not None else base.max_fill_delay_s)
        ports.append(dataclasses.replace(spec, min_latency_s=new_lat, max_fill_delay_s=max(own_delay, step + new_lat)))
    return dataclasses.replace(
        base, portfolios=ports, params=params, interval_s=step, min_latency_s=max(float(base.min_latency_s), lat),
        max_fill_delay_s=max(float(base.max_fill_delay_s), step + lat),
        set_book_max_age_s=max(float(base.set_book_max_age_s), step),
        stale_quote_s=max(float(base.stale_quote_s), QUOTE_MAX_AGE_FACTOR * step, CANDLE_QUOTE_MAX_AGE_S),
        target_hours=max(float(config.hours), 1.0 / 60.0),
    )


def _empty_report(config: BacktestConfig, generated_at: float, warnings: List[str]) -> BacktestReport:
    return BacktestReport(generated_at=generated_at, config=config, window={"start": None, "end": None, "hours": 0.0,
                                                                            "steps": 0},
                          warnings=warnings, testability={}, study=None)


def run_backtest(source: HistorySource, config: Optional[BacktestConfig] = None, *,
                 signal_fn: Optional[Callable[..., List[Opportunity]]] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 clock: Callable[[], float] = time.time) -> BacktestReport:
    """Replay ``config``'s window through generate_signals + PaperEngine (MemoryPaperPersistence:
    a backtest never writes to the live store) and report the same metrics, verdicts and event study as
    the paper trader, plus testability, coverage, assumptions and warnings. Stops early (``stopped_early``)
    when ``config.time_budget_s`` of wall time (``clock``) is used."""
    config = config if config is not None else BacktestConfig()
    wall0 = clock()
    warnings: List[str] = []
    try:
        if config.end is not None and config.start is not None:
            start, end = float(config.start), float(config.end)
        else:
            bounds = source.tick_bounds()
            if bounds is None and config.end is None:
                return _empty_report(config, clock(), ["No stored ticks: there is nothing to replay yet."])
            end = float(config.end) if config.end is not None else float(bounds[1])  # type: ignore[index]
            start = float(config.start) if config.start is not None else end - float(config.hours) * 3600.0
    except (NotImplementedError, AttributeError) as exc:
        return _empty_report(config, clock(), [f"The store cannot be replayed ({type(exc).__name__})."])
    cfg = dataclasses.replace(config, start=start, end=end)
    if isinstance(source, PreloadedHistory) and source.covers(start, end, cfg.warmup_s):
        pre = source
    else:
        pre = PreloadedHistory(source, start, end, warmup_s=cfg.warmup_s)
    warnings.extend(pre.errors)
    guard = GuardedHistory(pre)
    market = HistoricalMarket(guard, cfg, pre)
    times = market.decision_times()
    paper_cfg = backtest_paper_config(cfg)
    current = [times[0] if times else start]
    engine = _paper.PaperEngine(paper_cfg, persistence=_paper.MemoryPaperPersistence(), clock=lambda: current[0],
                                code_version="backtest")
    market.engine = engine
    capital = float(paper_cfg.start_capital) if paper_cfg.start_capital else _paper.DEFAULT_START_CAPITAL
    source_label = "set by you" if paper_cfg.start_capital else "default 100,000"
    stopped = False
    steps_run = 0
    if times:
        engine.start(times[0], capital, source_label)
    for i, t in enumerate(times):
        if cfg.time_budget_s is not None and clock() - wall0 >= float(cfg.time_budget_s):
            stopped = True
            break
        guard.set_time(t)
        current[0] = t
        inputs = market.inputs(t)
        obs = market.observation(t)
        _paper.run_step(engine, inputs, obs, market.fulfil, signal_fn=signal_fn)
        steps_run += 1
        if progress is not None:
            progress(i + 1, len(times))
    last_t = times[steps_run - 1] if steps_run else start
    covered_h = engine._coverage.covered_hours
    coverage = market.coverage()
    if steps_run and covered_h < 12.0 - 1e-9:
        warnings.append(f"Only {covered_h:.1f} hours of data in this window: too little for a verdict.")
    if coverage.get("candle_share", 0.0) > 0.5:
        warnings.append("Most quotes are candle-based: spreads and depth are assumptions.")
    if cfg.use_fair_values and market.counters["fv_steps"] == 0:
        warnings.append("No fair values were recorded in this window: value ideas could not trade.")
    overlap = None
    if cfg.live_run_started_at is not None:
        live = float(cfg.live_run_started_at)
        overlap = max(0.0, end - max(start, live)) / 3600.0
        if overlap > 0:
            warnings.append(OVERLAP_WARNING.format(h=overlap))
    if stopped:
        warnings.append(f"Stopped early after {float(cfg.time_budget_s or 0):.0f} s of wall time: the replay covers "
                        f"{steps_run} of {len(times)} decision steps.")
    summaries = engine.portfolios(last_t)
    try:
        study = engine.study.summary()
    except Exception:
        study = None
    return BacktestReport(
        generated_at=clock(), config=cfg,
        window={"start": start, "end": end, "hours": round((end - start) / 3600.0, 6), "steps": steps_run,
                "first_decision": times[0] if times else None, "last_decision": last_t if steps_run else None},
        coverage=coverage, assumptions=market.assumptions(), portfolios=summaries,
        equity={pid: _paper.downsample_equity([(p.ts, p.liq_value) for p in pts])
                for pid, pts in engine.equity().items()},
        trades=engine.trades(limit=REPORT_TRADES_MAX),
        verdicts={s.portfolio_id: s.verdict for s in summaries if s.verdict is not None},
        warnings=list(dict.fromkeys(warnings)), testability=market.testability(), study=study,
        overlap_hours=round(overlap, 6) if overlap is not None else None, stopped_early=stopped,
    )


def _parse_value(text: str) -> Any:
    t = text.strip()
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def _sweep_names() -> Tuple[Set[str], Set[str], Set[str]]:
    sp = {f.name for f in dataclasses.fields(StrategyParams)}
    bc = {f.name for f in dataclasses.fields(BacktestConfig)} - {"paper", "params_overrides"}
    pc = {f.name for f in dataclasses.fields(PaperConfig)} - {"portfolios", "params"}
    return sp, bc, pc


def parse_sweep(specs: Sequence[str]) -> Dict[str, List[Any]]:
    """``["min_value_edge=0.01,0.02", "latency_s=30,60"]`` -> grid. Names are StrategyParams fields,
    then BacktestConfig fields, then PaperConfig fields; unknown names raise ValueError."""
    sp, bc, pc = _sweep_names()
    grid: Dict[str, List[Any]] = {}
    for spec in specs or ():
        if "=" not in str(spec):
            raise ValueError(f"sweep {spec!r}: expected NAME=V1,V2")
        name, values = str(spec).split("=", 1)
        name = name.strip()
        if name not in sp and name not in bc and name not in pc:
            valid = ", ".join(sorted(sp) + sorted(bc) + sorted(pc))
            raise ValueError(f"unknown sweep parameter {name!r}; valid names: {valid}")
        parsed = [_parse_value(v) for v in values.split(",") if v.strip() != ""]
        if not parsed:
            raise ValueError(f"sweep {spec!r}: no values")
        grid[name] = parsed
    return grid


def _apply_combo(base: BacktestConfig, combo: Mapping[str, Any]) -> BacktestConfig:
    sp, bc, pc = _sweep_names()
    cfg = copy.deepcopy(base)
    for name, value in combo.items():
        if name in sp:
            cfg.params_overrides = dict(cfg.params_overrides or {})
            cfg.params_overrides[name] = value
        elif name in bc:
            setattr(cfg, name, value)
        elif name in pc:
            setattr(cfg.paper, name, value)
        else:
            raise ValueError(f"unknown sweep parameter {name!r}")
    return cfg


def sweep(source: HistorySource, base: BacktestConfig, grid: Mapping[str, Sequence[Any]], *,
          signal_fn: Optional[Callable[..., List[Opportunity]]] = None) -> BacktestReport:
    """Run every combination (at most MAX_SWEEP_RUNS); the returned report is the base config's run
    with ``sweep`` rows for every combination (sorted by the headline portfolio's pnl_liq) and the
    SWEEP_WARNING in ``warnings``. ``latency_s=30,120,300`` is the latency-sensitivity sweep the CLI
    offers as ``--latency-sweep``."""
    names = list(grid)
    combos = [dict(zip(names, values)) for values in itertools.product(*(list(grid[n]) for n in names))] if names else []
    if len(combos) > MAX_SWEEP_RUNS:
        raise ValueError(f"the sweep has {len(combos)} combinations; at most {MAX_SWEEP_RUNS} are allowed")
    for combo in combos:
        _apply_combo(base, combo)  # validate every name before running anything
    report = run_backtest(source, base, signal_fn=signal_fn)
    shared: Any = source
    if report.window.get("start") is not None and not isinstance(source, PreloadedHistory):
        try:
            hours_max = max([float(c.get("hours", base.hours)) for c in combos] + [float(base.hours)])
            warm_max = max([float(c.get("warmup_s", base.warmup_s)) for c in combos] + [float(base.warmup_s)])
            end = float(report.window["end"])
            start = min(float(report.window["start"]), end - hours_max * 3600.0)
            shared = PreloadedHistory(source, start, end, warmup_s=warm_max)
        except Exception:
            shared = source
    rows: List[Dict[str, Any]] = []
    head = _paper.headline_id(base.paper if base.paper is not None else PaperConfig())
    for combo in combos:
        cfg = _apply_combo(base, combo)
        if base.end is None and report.window.get("end") is not None:
            cfg.end = float(report.window["end"])
        r = run_backtest(shared, cfg, signal_fn=signal_fn)
        port = next((p for p in r.portfolios if p.portfolio_id == head), r.portfolios[0] if r.portfolios else None)
        rows.append({
            "params": dict(combo), "label": ", ".join(f"{k}={v}" for k, v in combo.items()),
            "pnl_liq": port.pnl_liq if port is not None else None,
            "trades_closed": port.trades_closed if port is not None else 0,
            "verdict_level": port.verdict.level if port is not None and port.verdict is not None else None,
            "max_drawdown": port.max_drawdown if port is not None else None,
        })
    rows.sort(key=lambda r: (-(r["pnl_liq"] if r["pnl_liq"] is not None else -float("inf")), r["label"]))
    report.sweep = rows
    report.warnings = list(report.warnings) + [SWEEP_WARNING.format(n=len(rows))]
    return report
