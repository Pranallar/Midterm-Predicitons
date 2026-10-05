"""Replay stored history through the live signal and paper code (package C of docs/PAPER_TRADING.md §6.12).

:func:`run_backtest` walks a decision grid over a stored window. At every decision time ``t`` a
:class:`HistoricalMarket` builds the same :class:`~supermarket_bot.models.StrategyInputs` and
:class:`~supermarket_bot.models.MarketObservation` the live tracker would have had, from data
with timestamps ``<= t`` only, and the same :func:`strategy.generate_signals` and
:class:`paper.PaperEngine` turn them into simulated orders and fills. Fills use the first
observation at least ``latency_s`` after the decision.

Where the store holds only candles (no bid/ask, no depth), quotes and depth are synthesised with
the documented assumptions (``assumed_spread``, ``assumed_touch_qty``, ``volume_share``) and the
report says so in ``assumptions`` and ``coverage``. A look-ahead guard wraps the history source:
any read reaching past the current decision time raises :class:`LookAheadError`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import (
    BacktestConfig,
    BacktestReport,
    BookObservation,
    ExchangeInfo,
    FairValueRecord,
    LeaderboardSnapshot,
    MarketObservation,
    Opportunity,
    PricePoint,
    SettlementInfo,
    StrategyInputs,
    Surge,
    TradeRecord,
)

log = logging.getLogger("supermarket_bot")

MAX_SWEEP_RUNS = 50
SURGE_LOOKBACK_S = 30 * 3600.0  # detection reads this much history before t (like the tracker)
QUOTE_MAX_AGE_FACTOR = 2.0  # a tick older than this x step_s is not a current quote at t

SWEEP_WARNING = (
    "The best of {n} settings is an optimistic estimate (it was picked after seeing the results); "
    "prefer settings whose neighbours also do well, and confirm them on data recorded later."
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
    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None, limit: int = 200) -> List[Surge]: ...
    def fair_value_history(self, since: float, until: Optional[float] = None, exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]: ...
    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]: ...
    def settlements(self) -> Dict[str, SettlementInfo]: ...
    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]: ...
    def tick_bounds(self) -> Optional[Tuple[float, float]]: ...


class MemoryHistory:
    """A HistorySource over plain lists (tests, synthetic scenarios)."""

    def __init__(self, *, exchanges: Sequence[ExchangeInfo] = (), points: Optional[Mapping[str, Sequence[PricePoint]]] = None,
                 candles: Optional[Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]]] = None,
                 trades: Optional[Mapping[str, Sequence[TradeRecord]]] = None, surges: Sequence[Surge] = (),
                 fair_values: Sequence[FairValueRecord] = (), books: Optional[Mapping[str, Sequence[BookObservation]]] = None,
                 settlements: Optional[Mapping[str, SettlementInfo]] = None,
                 leaderboards: Sequence[LeaderboardSnapshot] = ()) -> None:
        raise NotImplementedError


class GuardedHistory:
    """Wraps a HistorySource; ``set_time(t)`` moves the guard; a read with ``until`` (or ``since``)
    after ``t``, or without ``until`` at all, raises LookAheadError. ``settlements()`` is filtered to
    ``settled_on <= t``; ``exchanges()`` is allowed (metadata)."""

    def __init__(self, source: HistorySource) -> None:
        raise NotImplementedError

    def set_time(self, t: float) -> None:
        raise NotImplementedError


class HistoricalMarket:
    """Builds inputs and observations at decision times from history (§6.12.2)."""

    def __init__(self, source: HistorySource, config: BacktestConfig) -> None:
        raise NotImplementedError

    def window(self) -> Tuple[float, float]:
        """(start, end) of the replay: config values, else the store's tick bounds (end) and end - hours."""
        raise NotImplementedError

    def decision_times(self) -> List[float]:
        raise NotImplementedError

    def inputs(self, t: float) -> StrategyInputs:
        raise NotImplementedError

    def observation(self, t: float) -> MarketObservation:
        """Quotes, books and tape known at ``t`` (books/tape requested by the engine's read plan are
        provided as the first observation at or after the pending decision + latency, never after ``t``)."""
        raise NotImplementedError

    def fulfil(self, plan: Any, t: float) -> Tuple[Dict[str, BookObservation], Dict[str, List[TradeRecord]], int]:
        """The backtest's ``paper.FulfilFn``: for each book request the first observation with
        ``observed_at >= request.after`` (marks and candidates: the newest at most 900 s old), never
        later than ``t``; for each tape request the stored prints in ``(since, t]``, else candle
        trade-throughs (§6.12.2). Applies the same per-step read caps as the live runner."""
        raise NotImplementedError

    def coverage(self) -> Dict[str, Any]:
        raise NotImplementedError

    def assumptions(self) -> List[str]:
        raise NotImplementedError


def run_backtest(source: HistorySource, config: Optional[BacktestConfig] = None, *,
                 signal_fn: Optional[Callable[..., List[Opportunity]]] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 clock: Callable[[], float] = time.time) -> BacktestReport:
    """Replay ``config``'s window through generate_signals + PaperEngine (MemoryPaperPersistence:
    a backtest never writes to the live store) and report the same metrics and verdicts as the
    paper trader."""
    raise NotImplementedError


def parse_sweep(specs: Sequence[str]) -> Dict[str, List[Any]]:
    """``["min_value_edge=0.01,0.02", "latency_s=30,60"]`` -> grid. Names are StrategyParams fields,
    then BacktestConfig fields, then PaperConfig fields; unknown names raise ValueError."""
    raise NotImplementedError


def sweep(source: HistorySource, base: BacktestConfig, grid: Mapping[str, Sequence[Any]], *,
          signal_fn: Optional[Callable[..., List[Opportunity]]] = None) -> BacktestReport:
    """Run every combination (at most MAX_SWEEP_RUNS); the returned report is the base config's run
    with ``sweep`` rows for every combination (sorted by the headline portfolio's pnl_liq) and the
    SWEEP_WARNING in ``warnings``."""
    raise NotImplementedError
