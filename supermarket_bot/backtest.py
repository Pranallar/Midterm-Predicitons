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

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import (
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
CANDLE_QUOTE_MAX_AGE_S = 3600.0  # single-outcome ideas only: a candle closed at most this long ago
SYNTHETIC_DEPTH_CANDLE_S = 3600.0  # synthetic depth from the candles CLOSED by the observation time, this far back
CANDLE_FILL_RESOLUTION = "5m"
PERFORMANCE_TARGET_S = 60.0  # 24 h x 237 outcomes at step 300 s must finish within this (tests use a scaled store)
DASHBOARD_TIME_BUDGET_S = 120.0

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


class MemoryHistory:
    """A HistorySource over plain lists (tests, synthetic scenarios)."""

    def __init__(self, *, exchanges: Sequence[ExchangeInfo] = (), points: Optional[Mapping[str, Sequence[PricePoint]]] = None,
                 candles: Optional[Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]]] = None,
                 trades: Optional[Mapping[str, Sequence[TradeRecord]]] = None, surges: Sequence[Surge] = (),
                 fair_values: Sequence[FairValueRecord] = (), refreshes: Sequence[FairValueRefresh] = (),
                 books: Optional[Mapping[str, Sequence[BookObservation]]] = None,
                 settlements: Optional[Mapping[str, SettlementInfo]] = None,
                 leaderboards: Sequence[LeaderboardSnapshot] = ()) -> None:
        raise NotImplementedError


class PreloadedHistory:
    """Everything a replay of ``[start - warmup, end]`` needs, read once from a HistorySource: per exchange
    the series (ticks and candles), 5m/1h candles, tape (with fetched_at), book snapshots; fair-value records
    and refreshes; surges (``until=end``, limit 100,000); settlements; leaderboards. Sorted lists with
    bisect access. Loading reads the whole window (that is not a decision); decisions read only through
    GuardedHistory."""

    def __init__(self, source: HistorySource, start: float, end: float, *, warmup_s: float = 6 * 3600.0,
                 exchange_ids: Optional[Sequence[str]] = None) -> None:
        raise NotImplementedError


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
        raise NotImplementedError

    def set_time(self, t: float) -> None:
        raise NotImplementedError

    @property
    def filtered(self) -> int:
        raise NotImplementedError


def replay_fair_value(record: FairValueRecord, refresh: Optional[FairValueRefresh], t: float, latency_s: float) -> FairValue:
    """The FairValue a live decision at ``t`` would have seen from the newest record with ``ts <= t - latency``
    (§6.12.2): value/bid/ask/usable/confidence from the record; ``as_of`` = the record's live ``as_of``,
    advanced to the oldest ``fetched_at`` of the record's venues in ``refresh`` (the newest refresh row with
    ``ts <= t - latency``) when that refresh is newer than the record and every one of those venues was "ok"
    in it; ``uncertainty`` / ``prev_value`` / ``suspect`` from ``detail``; source "history" records get
    ``history=True`` and ``as_of`` = the bar end. Manual values: availability is the record ts, never the
    user's updated_at."""
    raise NotImplementedError


class HistoricalMarket:
    """Builds inputs and observations at decision times from a GuardedHistory (§6.12.2). Keeps one
    incremental ``analytics._SurgeScanner`` per exchange over the preloaded series (prefix-safe: the
    decision at t uses only points <= t) instead of re-reading 30 h of series per step."""

    def __init__(self, history: GuardedHistory, config: BacktestConfig, preloaded: Optional[PreloadedHistory] = None) -> None:
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
        provided by ``fulfil``)."""
        raise NotImplementedError

    def fulfil(self, plan: Any, t: float) -> Any:
        """The backtest's ``paper.FulfilFn`` (returns a paper.Fulfilment): for each book request the first real
        snapshot with ``observed_at >= request.after`` and ``<= t``, else a synthetic book built from the first
        tick at or after ``after`` with depth from candles closed by that tick's time; marks and candidates:
        the newest real snapshot at most 900 s old (synthetic books are never returned for marks); for each
        tape request the stored prints in ``(since, t]`` fetched by t, else candle trade-throughs (§6.12.2,
        flagged synthetic). Applies the same per-step read caps and group rule as the live runner."""
        raise NotImplementedError

    def coverage(self) -> Dict[str, Any]:
        raise NotImplementedError

    def assumptions(self) -> List[str]:
        raise NotImplementedError

    def testability(self) -> Dict[str, Dict[str, Any]]:
        """Per trade kind: ``{"status", "hours", "sentence"}`` (§6.12.3), e.g. value "not_testable" with
        "value: not testable yet (0 h of outside prices recorded or imported in this window)"."""
        raise NotImplementedError


def run_backtest(source: HistorySource, config: Optional[BacktestConfig] = None, *,
                 signal_fn: Optional[Callable[..., List[Opportunity]]] = None,
                 progress: Optional[Callable[[int, int], None]] = None,
                 clock: Callable[[], float] = time.time) -> BacktestReport:
    """Replay ``config``'s window through generate_signals + PaperEngine (MemoryPaperPersistence:
    a backtest never writes to the live store) and report the same metrics, verdicts and event study as
    the paper trader, plus testability, coverage, assumptions and warnings. Stops early (``stopped_early``)
    when ``config.time_budget_s`` of wall time (``clock``) is used."""
    raise NotImplementedError


def parse_sweep(specs: Sequence[str]) -> Dict[str, List[Any]]:
    """``["min_value_edge=0.01,0.02", "latency_s=30,60"]`` -> grid. Names are StrategyParams fields,
    then BacktestConfig fields, then PaperConfig fields; unknown names raise ValueError."""
    raise NotImplementedError


def sweep(source: HistorySource, base: BacktestConfig, grid: Mapping[str, Sequence[Any]], *,
          signal_fn: Optional[Callable[..., List[Opportunity]]] = None) -> BacktestReport:
    """Run every combination (at most MAX_SWEEP_RUNS); the returned report is the base config's run
    with ``sweep`` rows for every combination (sorted by the headline portfolio's pnl_liq) and the
    SWEEP_WARNING in ``warnings``. ``latency_s=30,120,300`` is the latency-sensitivity sweep the CLI
    offers as ``--latency-sweep``."""
    raise NotImplementedError
