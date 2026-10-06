"""Background market tracker: snapshots → SQLite → surge/high-band analytics → attribution.

See docs/DESIGN.md (Tracker section).

One cycle (:meth:`Tracker.run_once`):

1. Refresh the open-market list when due (``market_refresh``) and store it.
2. Snapshot every outcome's price (bulk reads) and store the ticks.
3. For every outcome, run surge detection and the high-band check on the last 30 hours,
   merge surges into the store and update the status of every open surge.
4. Queue new surges (or ones whose ``|change|`` grew by 0.03 since their last analysis) for
   attribution.
5. Refresh the tournament context (constraints, balance, leaderboard, overround) when due.

:meth:`Tracker.start` runs that loop in a daemon thread next to a candle-backfill worker, an
attribution worker and a context worker (so slow optional reads never delay price snapshots);
each worker has its own read budget on top of the client's limiter. Backfill and context reads
are single attempts: a failure is retried by the worker later, never inline by the client.

Non-fatal failures are reported in ``status()["problems"]`` (one entry per failing source,
cleared when it works again) and ``status()["detection"]`` says whether surge detection is
waiting for history or running on live prices only. Read-only: nothing here places orders.

Paper trading and outside fair values (docs/PAPER_TRADING.md §7.2):

* a **read reserve** (D44) keeps 12 of the client's slots for the snapshot loop: every background read
  (context, backfill, analysis, paper) waits, or is skipped, while fewer are free;
* ``fair_values`` (a :class:`~supermarket_bot.fairvalue.FairValueService`) refreshes in its own
  ``tracker-fairvalue`` worker, ``paper`` (a :class:`~supermarket_bot.paper.PaperEngine`) steps in its
  own ``tracker-paper`` worker after each cycle, within :class:`PaperBudget` (20 reads/min, 6 while the
  backfill runs); its reads go through :class:`TrackerMarketReader` (GET only, stored for replays);
* settlements of resolved markets are read every 600 s (or when outcomes go missing) and stored;
* a store **lease** (D46) refuses a second tracker on the same database;
* **lock rule** (D43): nothing calls the paper runner, the engine or the fair-value service while holding
  ``self._lock``.

Outside-move alerts (docs/OUTSIDE_MOVES.md §13): ``moves`` (a :class:`~supermarket_bot.moves.OutsideMoveWatcher`)
is bound to a :class:`TrackerCupFeed` (the published snapshot quotes, the series cache, stored books and at most
``moves_reads_per_min`` fresh Cup books a minute inside :class:`MovesBudget`, behind the read reserve and never
waited for) and steps every ``moves.poll_s`` in its own ``tracker-moves`` worker. Its outside polls use the
fair-value providers' own per-host budgets, never the Super Market client. The watcher is never called while
``self._lock`` is held, and ``status()`` reads its published status lock-free.
"""

from __future__ import annotations

import copy
import itertools
import logging
import math
import os
import socket
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import analytics, pipeline
from .books import Book, books_from_market_orderbook, parse_time
from .bot import Context, MarketDataBot, is_fatal
from .client import SuperMarketClient
from .errors import ApiError, NetworkError, RequestCancelled, SuperMarketError, redact
from .models import (
    NEWS_OK,
    NEWS_UNAVAILABLE,
    SURGE_CLOSED,
    SURGE_HELD,
    SURGE_OPEN,
    SURGE_REVERTED,
    VERDICT_PARTICIPANTS,
    BookObservation,
    ExchangeInfo,
    PricePoint,
    SettlementInfo,
    StrategyParams,
    Surge,
    TradeRecord,
    iso_ts,
)
from .ratelimit import SlidingWindowLimiter
from .store import TrackerStore

log = logging.getLogger("supermarket_bot")

SERIES_LOOKBACK_S = 30 * 3600.0  # history analysed per cycle (24h windows + their volatility lead-in)
SERIES_RELOAD_S = 600.0  # re-read the full series from the store at least this often
SPARKLINE_WINDOW_S = 24 * 3600.0
SPARKLINE_POINTS = 96
CHANGE_WINDOWS: Tuple[Tuple[str, float], ...] = (("change_5m", 300.0), ("change_1h", 3600.0), ("change_24h", 86400.0))
BACKFILL_PLAN: Tuple[Tuple[str, int], ...] = (("1h", 168), ("5m", 288))  # 7 days hourly, then 24 hours of 5m
BACKFILL_MAX_ATTEMPTS = 3
RESUME_GAP_S = 900.0  # data this close to a restart means the earlier backfill still covers us
REANALYZE_GROWTH = 0.03
MAX_OVERROUND_MARKETS = 10
SURGE_VIEW_LOOKBACK_S = 48 * 3600.0
SURGE_VIEW_LIMIT = 100
MAX_RECENT_ERRORS = 20
ANALYZED_STATE_KEY = "tracker:analyzed_change"
MAX_ANALYZED_REMEMBERED = 500
WORKER_IDLE_S = 5.0
HISTORY_WAIT_MAX_S = 600.0  # detect anyway once an outcome has waited this long for its backfill
BACKFILL_FAIL_STREAK = 3  # transient failures on this many different series in a row: history is down
BACKFILL_BACKOFF_S = 60.0  # then pause the whole backfill queue (doubling, up to the max)
BACKFILL_BACKOFF_MAX_S = 600.0
PROBLEM_MESSAGE_MAX = 300
BAND_MAX_STALENESS_S = 600.0  # a high band needs a current mark at most this old
REVERSION_LINK_S = 6 * 3600.0  # an opposite move this soon after a surge's window may be its reversion
NEWS_RECHECK_S = 300.0  # while news search is failing, re-try it this often (doubling) ...
NEWS_RECHECK_MAX_S = 1800.0  # ... up to this
MISSING_REFRESH_MIN_S = 30.0  # outcomes missing from the bulk prices: re-read the market list at most this often
IDEA_BOOK_DEPTH = 20  # order-book levels read to size trade ideas
IDEA_BOOKS_PER_REFRESH = 12  # single-exchange order books read per context refresh for trade ideas
IDEA_BOOK_MAX_AGE_S = 900.0  # a stored idea book younger than this is not re-read for another candidate
READ_RESERVE = 12  # client slots kept for the snapshot loop: background reads wait (or skip) below this (D44)
RESERVE_POLL_S = 1.0  # a background read waiting for room above the reserve re-checks this often
SETTLEMENT_PAGES_MAX = 5  # GET /tournaments/{slug}/markets?status=settled pages per read (100 markets each)
TAPE_PAGE_LIMIT = 200  # trades per tape page (the API maximum)
LEASE_NAME = "tracker"
LEASE_TTL_INTERVALS = 3.0  # a lease whose heartbeat is older than this many intervals has lapsed (D46)
LEASE_HEARTBEATS_PER_TTL = 9.0  # the heartbeat thread renews this often per TTL (every interval / 3)
LEASE_HEARTBEAT_MAX_S = 30.0
CONTEXT_WAIT_MAX_S = 60.0  # the first paper step waits at most this long for the first context refresh (§6.1)
PROJECTED_WARN_PER_MIN = 75.0  # warn above this projected read rate (the account allows 100/min across all keys)
ACCOUNT_READS_PER_MIN = 100  # the Super Market account limit, shared by every key
DRAWER_READS_PER_MIN = 4.0  # one open exchange drawer re-reads its book every 15 s
ANALYSIS_READS_PER_MIN = 2.0  # ~2 reads per new or grown surge (bursts up to the analysis budget)
# docs/OUTSIDE_MOVES.md §13 (package "wiring"): Cup order-book reads for lagging outside-move alerts (the hand
# trade's "shares available"), behind the read reserve, skipped (never waited for) when there is no room
MOVES_READS_PER_MIN = 4
MOVES_PROBLEM = "outside moves"  # status()["problems"] source of the outside-move watcher (severity "warning")
_OWNER_SEQ = itertools.count(1)
# status()["problems"] sources and how bad a current failure of each is
PROBLEM_SEVERITY: Dict[str, str] = {
    "markets": "error",
    "prices": "error",
    "storage": "error",
    "tracker": "error",
    "analytics": "warning",
    "price history": "warning",
    "constraints": "warning",
    "balance": "warning",
    "leaderboard": "warning",
    "order books": "warning",
    "attribution": "warning",
    "news": "warning",
    "paper": "warning",
    "fair value": "warning",
    "settlements": "warning",
    "read budget": "warning",
    MOVES_PROBLEM: "warning",
}
_EPS = 1e-9


class _Stopping(BaseException):
    """Raised from worker waits once :meth:`Tracker.stop` was requested.

    A ``BaseException`` so that ``except Exception`` blocks in collaborators (for example
    an attributor degrading gracefully) do not swallow the shutdown.
    """


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _copy_json(value: Any) -> Any:
    """Copy nested dicts/lists (JSON-shaped data) so callers cannot mutate shared state."""
    if isinstance(value, dict):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_json(v) for v in value]
    return value


def _backfill_key(exchange_id: str, resolution: str) -> str:
    return f"backfill:{exchange_id}:{resolution}"


def _transient(exc: SuperMarketError) -> bool:
    """Worth retrying later: network trouble, rate limits, timeouts and server errors."""
    if isinstance(exc, NetworkError):
        return True
    return isinstance(exc, ApiError) and (exc.status >= 500 or exc.status in (408, 429))


def _server_wait(exc: BaseException) -> Optional[float]:
    """The wait a 429/503 asked for (``Retry-After`` or ``details.retryAfterSeconds``)."""
    if not isinstance(exc, ApiError):
        return None
    if exc.retry_after is not None:
        return max(0.0, float(exc.retry_after))
    seconds = (exc.details or {}).get("retryAfterSeconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return max(0.0, float(seconds))
    return None


def _clip_message(text: str, secrets: Sequence[str] = ()) -> str:
    """One line of at most ``PROBLEM_MESSAGE_MAX`` characters, secrets masked *before* cutting
    (a key cut at the boundary would otherwise leak its start)."""
    text = " ".join(redact(text, secrets).split())
    return text if len(text) <= PROBLEM_MESSAGE_MAX else text[: PROBLEM_MESSAGE_MAX - 1].rstrip() + "…"


def _problem_text(exc: Any, secrets: Sequence[str] = ()) -> str:
    text = str(exc).split(" — ")[0].strip() or type(exc).__name__  # drop the long hint ApiError adds
    if isinstance(exc, BaseException) and not isinstance(exc, SuperMarketError):
        text = f"{type(exc).__name__}: {text}"
    return _clip_message(text, secrets)


def _storage_error(exc: BaseException) -> bool:
    """A failure of the local database or disk (full disk, locked or broken SQLite file)."""
    return isinstance(exc, (sqlite3.Error, OSError)) and not isinstance(exc, SuperMarketError)


class TrackerBusy(RuntimeError):
    """Another live process holds this store's tracker lease (D46): running two would double the reads.

    The message depends on what is known about the holder (live-5): a process that is running on this machine
    (``holder["alive"]`` True) is to be stopped first; for one on another machine (or one whose state cannot be
    checked) it says that the lock frees itself ``ttl_s`` after that process's last heartbeat if it has already
    ended. It never suggests a second copy of the data (a new paper run and a full history download)."""

    def __init__(self, holder: Mapping[str, Any], path: str = "", *, ttl_s: Optional[float] = None,
                 now: Optional[float] = None) -> None:
        self.holder = dict(holder or {})
        self.path = str(path or "")
        pid = self.holder.get("pid") if self.holder.get("pid") is not None else "?"
        host = self.holder.get("host") or "another machine"
        where = self.path or "this database"
        tail = "Running two would double the API reads; the account allows 100 per minute across all keys."
        if self.holder.get("alive") is True:
            text = (f"Another process (pid {pid} on {host}) is already running the tracker on {where}: stop it first "
                    "(close that dashboard, or press Ctrl-C in its terminal), then start again. " + tail)
        else:
            ttl, beat, at = _num(ttl_s), _num(self.holder.get("heartbeat_at")), _num(now)
            wait = "after a short wait"
            if ttl is not None and ttl > 0:
                left = ttl - (at - beat) if beat is not None and at is not None else None
                wait = f"within {ttl:.0f} s of its last heartbeat" + (
                    f" (about {max(1.0, left):.0f} s from now)" if left is not None and left > 0 else "")
            text = (f"Another process (pid {pid} on {host}) is already running the tracker on {where}: stop it first. "
                    f"If it has already ended, the lock frees itself {wait}: start again then. " + tail)
        super().__init__(text)


class ReadReserve:
    """Keeps ``reserve`` slots of the shared client limiter free for the snapshot loop (D44).

    ``room() = shared.limit - reserve - shared.used``. Background readers call :meth:`wait` (blocks, polling
    every ``poll_s`` with the stop-aware ``sleep``) or check :meth:`room` and skip; the snapshot loop's own
    reads never look at it."""

    def __init__(self, shared: Any, reserve: int = READ_RESERVE, *, sleep: Optional[Callable[[float], None]] = None,
                 poll_s: float = RESERVE_POLL_S) -> None:
        self.shared = shared
        self.reserve = max(0, int(reserve))
        self._sleep = sleep or time.sleep
        self.poll_s = float(poll_s)

    @property
    def limit(self) -> int:
        return int(getattr(self.shared, "limit", 0) or 0)

    def room(self) -> int:
        if self.shared is None:
            return 1_000_000
        try:
            return int(self.shared.limit) - self.reserve - int(self.shared.used)
        except Exception:  # a limiter without the usual attributes: never gate
            return 1_000_000

    def wait(self) -> float:
        """Block until there is room above the reserve. Returns the seconds waited."""
        waited = 0.0
        while self.room() <= 0:
            self._sleep(self.poll_s)
            waited += self.poll_s
        return waited


class _GatedLimiter:
    """A worker's own budget behind the read reserve: ``acquire`` waits for room above the reserve, then
    takes its own slot. Exposes the limiter attributes the status and the attributor use."""

    def __init__(self, own: SlidingWindowLimiter, reserve: ReadReserve) -> None:
        self.own = own
        self.reserve = reserve

    def acquire(self) -> float:
        waited = self.reserve.wait()
        return waited + self.own.acquire()

    def pause(self, seconds: float) -> None:
        self.own.pause(seconds)

    @property
    def limit(self) -> int:
        return self.own.limit

    @property
    def used(self) -> int:
        return self.own.used

    @property
    def window(self) -> float:
        return self.own.window

    @property
    def _paused_until(self) -> float:
        return self.own._paused_until

    @property
    def _clock(self) -> Callable[[], float]:
        return self.own._clock


class PaperBudget:
    """The paper worker's read budget (§7.2): its own sliding window (``reads_per_min``, or
    ``during_backfill`` while the tracker's backfill queue is not empty) AND room above the read reserve.
    The runner reads only while ``room() > 0``, so :meth:`acquire` never needs to wait."""

    def __init__(self, own: SlidingWindowLimiter, reserve: ReadReserve, backfill_pending: Callable[[], bool],
                 during_backfill: int) -> None:
        self.own = own
        self.reserve = reserve
        self._backfill_pending = backfill_pending
        self.during_backfill = max(1, int(during_backfill))

    @property
    def limit(self) -> int:
        try:
            pending = bool(self._backfill_pending())
        except Exception:
            pending = False
        return min(self.own.limit, self.during_backfill) if pending else self.own.limit

    @property
    def used(self) -> int:
        return self.own.used

    def room(self) -> int:
        return min(self.limit - self.own.used, self.reserve.room())

    def acquire(self) -> float:
        return self.own.acquire()

    def status(self) -> Dict[str, int]:
        return {"used": int(self.used), "limit": int(self.limit), "room": max(0, int(self.room()))}


class _WorkerReadLimiter:
    """The client's read limiter as seen by a worker's single-attempt reads.

    Every request still takes a slot from the shared per-account budget, after waiting for room above
    the snapshot loop's read reserve (when one is set). A server-requested wait is *not* applied to
    every caller here (the client would pause snapshots too for an optional read's ``503 Retry-After``);
    the tracker forwards 429 waits to the shared limiter itself and keeps 503 waits local to the worker
    that hit them.
    """

    def __init__(self, shared: Any, reserve: Optional[ReadReserve] = None) -> None:
        self.shared = shared
        self.reserve = reserve

    def acquire(self) -> float:
        waited = self.reserve.wait() if self.reserve is not None else 0.0
        return waited + self.shared.acquire()

    def pause(self, seconds: float) -> None:
        pass

    @property
    def limit(self) -> int:
        return self.shared.limit

    @property
    def used(self) -> int:
        return self.shared.used


def single_attempt_client(client: Any) -> Any:
    """A view of ``client`` that sends each request once (no inline retries or waits).

    It shares the HTTP pool, the per-account read budget and the cancel flag with ``client``.
    Background workers use it so a failing optional read costs one request and is retried by
    the worker on its own schedule. Objects that are not a ``SuperMarketClient`` are returned as is.
    """
    if not isinstance(client, SuperMarketClient):
        return client
    view = copy.copy(client)
    view.max_retries = 0
    view.read_limiter = _WorkerReadLimiter(client.read_limiter)  # type: ignore[assignment]
    view.requests_sent = 0
    return view


def leaderboard_value(entry: Mapping[str, Any], initial_balance: Optional[float]) -> Optional[float]:
    """Portfolio value of a leaderboard entry.

    The leaderboard reports PnL, not portfolio value. We assume ``value = initialBalance + pnl``
    (all-time period) unless the entry carries an explicit value field.
    """
    for key in ("portfolioValue", "value", "totalValue", "balance", "equity"):
        explicit = _num(entry.get(key))
        if explicit is not None:
            return explicit
    pnl = _num(entry.get("pnl"))
    if pnl is None or initial_balance is None:
        return None
    return round(initial_balance + pnl, 6)


class Tracker:
    def __init__(
        self,
        client: SuperMarketClient,
        context: Context,
        store: TrackerStore,
        *,
        interval: float = 30.0,
        market_refresh: float = 300.0,
        backfill: bool = True,
        attributor: Any = None,
        analyze: bool = True,
        backfill_reads_per_min: int = 30,
        analyze_reads_per_min: int = 20,
        context_refresh: float = 300.0,
        clock: Callable[[], float] = time.time,
        max_overround_markets: int = MAX_OVERROUND_MARKETS,
        fair_values: Any = None,
        paper: Any = None,
        paper_reads_per_min: int = 20,
        paper_reads_per_min_during_backfill: int = 6,
        settlement_refresh: float = 600.0,
        leaderboard_limit: int = 100,
        regime: str = "unknown",
        strategy_params: Optional[StrategyParams] = None,
        read_reserve: int = READ_RESERVE,
        limiter_clock: Optional[Callable[[], float]] = None,
        limiter_sleep: Optional[Callable[[float], None]] = None,
        lease: bool = False,
        moves: Any = None,
        moves_reads_per_min: int = MOVES_READS_PER_MIN,
    ) -> None:
        """New in docs/PAPER_TRADING.md §7.2 (the defaults keep the old behaviour): ``fair_values`` (a
        FairValueService, refreshed in its own worker), ``paper`` (a PaperEngine, stepped after each cycle
        within ``paper_reads_per_min``, ``paper_reads_per_min_during_backfill`` while the backfill queue is not
        empty), ``settlement_refresh`` (seconds between reads of settled markets), ``leaderboard_limit``
        (rows read per context refresh), ``regime`` / ``strategy_params`` (the paper trader's inputs),
        ``read_reserve`` (client slots kept for the snapshot loop), ``limiter_clock`` / ``limiter_sleep``
        (used by every limiter the tracker builds; the fast demo passes its SimClock and ``demo.no_wait``)
        and ``lease`` (one tracker per store).

        docs/OUTSIDE_MOVES.md §13 (package "wiring"): ``moves`` (an ``moves.OutsideMoveWatcher``; the tracker binds
        it to a :class:`TrackerCupFeed` and steps it every ``moves.poll_s`` in its own ``tracker-moves`` worker) and
        ``moves_reads_per_min`` (its Cup order-book budget, :class:`MovesBudget`; 0 disables those reads)."""
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self.client = client
        self.context = context
        self.store = store
        self.interval = float(interval)
        self.market_refresh = float(market_refresh)
        self.context_refresh = float(context_refresh)
        self.backfill_enabled = bool(backfill)
        self.attributor = attributor
        self.analyze_enabled = bool(analyze)
        self.max_overround_markets = max(0, int(max_overround_markets))
        self._clock = clock
        # The client's API key: masked in every problem message before it is cut to length.
        self._secrets: Tuple[str, ...] = tuple(s for s in getattr(client, "_secrets", ()) or () if isinstance(s, str))
        self._bot = MarketDataBot(client, context)  # snapshots keep the client's retries
        self._stop = threading.Event()
        self._backfill_wake = threading.Event()
        self._analysis_wake = threading.Event()
        self._paper_wake = threading.Event()  # set at the end of every cycle: the paper worker steps once per cycle
        self._first_cycle = threading.Event()  # the context worker starts after the first cycle
        # set once the first context refresh (balance, account value) has finished, whatever its outcome: the
        # first paper step waits for it so a run starts on the account value, not the default (accounting-3)
        self._context_ready = threading.Event()
        self._limiter_clock: Callable[[], float] = limiter_clock or time.monotonic
        self._limiter_sleep: Callable[[float], None] = limiter_sleep or self._wait_or_stop
        # The snapshot loop's reserve on the shared client budget (D44): background reads wait for room above it.
        self.read_reserve = ReadReserve(getattr(client, "read_limiter", None), read_reserve, sleep=self._limiter_sleep)
        self._quick = single_attempt_client(client)  # backfill and context: one attempt per read
        if isinstance(getattr(self._quick, "read_limiter", None), _WorkerReadLimiter):
            self._quick.read_limiter.reserve = self.read_reserve
        self.backfill_limiter = _GatedLimiter(
            SlidingWindowLimiter(max(1, int(backfill_reads_per_min)), clock=self._limiter_clock, sleep=self._limiter_sleep),
            self.read_reserve,
        )
        self.analyze_limiter = _GatedLimiter(
            SlidingWindowLimiter(max(1, int(analyze_reads_per_min)), clock=self._limiter_clock, sleep=self._limiter_sleep),
            self.read_reserve,
        )
        if attributor is not None and getattr(attributor, "limiter", None) is None:
            # Share the analysis budget with the attributor, which makes the client calls.
            try:
                attributor.limiter = self.analyze_limiter
            except AttributeError:
                log.debug("attributor does not accept a limiter; analysis reads use the client budget only")

        self._lock = threading.RLock()  # everything below is shared with the workers and readers
        self._cycle_lock = threading.Lock()  # one run_once at a time
        self._threads: List[threading.Thread] = []
        self._session_start = clock()  # backfills older than this belong to an earlier run
        self._started_at: Optional[float] = None

        # paper trading, fair values, settlements (docs/PAPER_TRADING.md §7.2)
        self.fair_values = fair_values
        self.regime = str(regime or "unknown")
        self.strategy_params = strategy_params
        self.settlement_refresh = float(settlement_refresh)
        self.leaderboard_limit = max(1, min(100, int(leaderboard_limit)))
        self._settlements_at = float("-inf")
        self._settled_missing: Set[str] = set()  # the missing set the last settlement read covered
        self.paper_limiter = PaperBudget(
            SlidingWindowLimiter(max(1, int(paper_reads_per_min)), clock=self._limiter_clock, sleep=self._limiter_sleep),
            self.read_reserve,
            backfill_pending=self._backfill_pending,
            during_backfill=paper_reads_per_min_during_backfill,
        )
        self.paper = paper
        self.paper_runner: Any = None
        self._paper_error: Optional[str] = None
        self._paper_read_failures = 0  # reads that failed during the current paper step
        self._projected: Dict[str, Any] = {"per_min": 0.0, "warning": None}
        self.lease_enabled = bool(lease)
        self._lease_held = False
        self._lease_ttl = LEASE_TTL_INTERVALS * self.interval
        # renewed by its own thread (live-1): a cycle stuck in client retries must not let the lease lapse
        self._lease_heartbeat_s = max(0.05, min(LEASE_HEARTBEAT_MAX_S, self._lease_ttl / LEASE_HEARTBEATS_PER_TTL))
        self._owner: Dict[str, Any] = {
            "owner_id": f"{socket.gethostname()}:{os.getpid()}:{time.time():.6f}:{next(_OWNER_SEQ)}",
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        self._published: Dict[str, Any] = {"mids": {}, "recent": {}, "at": None}  # replaced, never mutated

        # markets and prices
        self._markets: Optional[List[Dict[str, Any]]] = None
        self._markets_at = float("-inf")
        self._infos: Dict[str, ExchangeInfo] = {}  # currently open outcomes, in market order
        self._all_infos: Dict[str, ExchangeInfo] = {}  # every stored outcome (titles for old surges)
        self._rows: Dict[str, Dict[str, Any]] = {}  # latest snapshot row per exchange
        # Outcomes the latest bulk price read did not return (missingIds: settled, closed or out
        # of scope since the market list was read): no ticks are stored and they count as closed.
        self._missing: Set[str] = set()
        self._missing_listed: Set[str] = set()  # the missing set the last early market-list read was for
        self._missing_refresh_at = float("-inf")
        self._series: Dict[str, List[PricePoint]] = {}
        self._series_loaded: Dict[str, float] = {}
        self._dirty: Set[str] = set()  # exchanges whose stored history changed under the cache

        # backfill
        self._bf_queues: Dict[str, Deque[str]] = {res: deque() for res, _ in BACKFILL_PLAN}
        self._bf_known: Set[Tuple[str, str]] = set()
        self._bf_done: Set[Tuple[str, str]] = set()
        self._bf_failed: Set[Tuple[str, str]] = set()
        self._bf_attempts: Dict[Tuple[str, str], int] = {}
        self._bf_planned_at: Dict[str, float] = {}  # data-clock time each outcome's backfill was planned
        self._bf_streak: List[str] = []  # series ids of the current run of transient failures
        self._bf_pause_until = 0.0  # time.monotonic() before which the backfill queue rests
        self._bf_backoffs = 0
        # Series that failed and are queued to be tried again: the "price history" problem stays
        # while any is left (a success on another series does not clear it) and goes away once
        # each one has loaded or been given up (then detection.reason explains the gap).
        self._bf_retrying: Set[Tuple[str, str]] = set()
        self._bf_last_error: Optional[str] = None  # why the last backfill read failed

        # attribution queue
        self._queue: Deque[int] = deque()
        self._queued: Set[int] = set()
        self._analyzed_change: Dict[int, float] = self._load_analyzed()
        self._analyzed_window: Dict[int, str] = {}  # window each surge was last analysed over
        self._analyses = 0
        self._requeued = False  # stored surges without an analysis are queued on the first cycle
        self._analyzing: Optional[int] = None  # the surge the analysis worker is working on
        # While the "news" problem is set, a surge analysed without headlines is re-analysed on a
        # backoff (5 min doubling to 30 min) to find out whether news search works again.
        self._news_recheck_at: Optional[float] = None
        self._news_recheck_wait = NEWS_RECHECK_S

        # context
        self._context: Dict[str, Any] = {
            "tournament_id": context.tournament_id,
            "slug": context.slug,
            "name": context.name,
            "currency": context.currency,
            "tournament": None,
            "balance": None,  # cash (the tournament's myBalance)
            "account_value": None,  # cash + open positions at market value (like the leaderboard's value)
            "positions_value": None,
            "initial_balance": None,
            "leaderboard": None,
            "constraints": None,
            "overround": [],
            "updated_at": None,
        }
        self._context_at = float("-inf")
        self._overround_read: Dict[str, int] = {}  # market id -> refresh round its book was last read in
        self._overround_round = 0

        # status
        self._cycles = 0
        self._errors = 0
        self._recent_errors: Deque[Dict[str, Any]] = deque(maxlen=MAX_RECENT_ERRORS)
        self._fatal_error: Optional[str] = None
        self._last_snapshot_at: Optional[float] = None
        self._last_cycle_at: Optional[float] = None
        self._last_cycle_s: Optional[float] = None
        self._ticks_recorded = 0
        self._view: Optional[Dict[str, Any]] = None
        self._problems: Dict[str, Dict[str, Any]] = {}  # source -> current non-fatal failure
        self._detection: Dict[str, Any] = {"enabled": True, "waiting_for_history": 0, "live_only": 0, "reason": None}

        # outside-move alerts (docs/OUTSIDE_MOVES.md §13): the watcher sees the Cup only through a TrackerCupFeed, and
        # its optional Cup order-book reads have their own small budget behind the read reserve (never waited for)
        self.moves = moves
        self.moves_reads_per_min = max(0, int(moves_reads_per_min))
        self.moves_budget: Optional[MovesBudget] = None
        self._moves_book_failures = 0  # Cup book reads that failed during the current watcher step
        if moves is not None:
            self.moves_budget = MovesBudget(
                SlidingWindowLimiter(self.moves_reads_per_min, clock=self._limiter_clock, sleep=self._limiter_sleep)
                if self.moves_reads_per_min > 0 else None,
                self.read_reserve,
            )
            moves.bind(TrackerCupFeed(self))

        if paper is not None:
            self._make_paper_runner(paper)
        self._update_projection()

    # ------------------------------------------------------------------ plumbing

    def _wait_or_stop(self, seconds: float) -> None:
        """Limiter sleep that ends at once (raising :class:`_Stopping`) when stop() is called."""
        if self._stop.wait(max(0.0, seconds)):
            raise _Stopping()

    def _record_error(self, where: str, exc: Any, source: Optional[str] = None) -> None:
        message = redact(str(exc), self._secrets)
        with self._lock:
            self._errors += 1
            self._recent_errors.append({"at": self._clock(), "where": where, "error": message})
        log.warning("tracker: %s failed: %s", where, message)
        if source is not None:
            self._problem(source, exc)

    def _storage_failed(self, where: str, exc: BaseException) -> None:
        """A local database/disk failure: counted and shown as the "storage" problem."""
        self._record_error(where, f"{type(exc).__name__}: {exc}", None)
        self._problem("storage", f"{where[:1].upper()}{where[1:]} failed: {exc} ({type(exc).__name__}). Check the free "
                                 "disk space, and that no other bot or dashboard uses the same data folder")

    def _problem(self, source: str, exc: Any, severity: Optional[str] = None) -> None:
        """Record (or extend) the current failure of ``source`` for ``status()["problems"]``."""
        now = self._clock()
        message = _clip_message(exc, self._secrets) if isinstance(exc, str) else _problem_text(exc, self._secrets)
        with self._lock:
            entry = self._problems.get(source)
            if entry is None:
                entry = {"source": source, "message": message, "since": now, "last": now, "count": 0,
                         "severity": severity or PROBLEM_SEVERITY.get(source, "warning")}
                self._problems[source] = entry
            entry["message"] = message
            entry["last"] = now
            entry["count"] += 1
            if severity:
                entry["severity"] = severity

    def _resolve(self, source: str) -> None:
        """``source`` works again: drop its problem."""
        with self._lock:
            self._problems.pop(source, None)

    def _forward_rate_limit(self, exc: BaseException) -> None:
        """A 429 is about the whole account's budget: make every caller wait it out."""
        if isinstance(exc, ApiError) and exc.status == 429:
            limiter = getattr(self.client, "read_limiter", None)
            if limiter is not None and hasattr(limiter, "pause"):
                limiter.pause(_server_wait(exc) or 1.0)

    def _set_fatal(self, where: str, exc: BaseException) -> None:
        message = redact(str(exc), self._secrets)
        with self._lock:
            self._fatal_error = message
            self._recent_errors.append({"at": self._clock(), "where": where, "error": message})
        log.error("tracker stopped: %s failed: %s", where, message)
        self._signal_stop()

    def _signal_stop(self) -> None:
        self._stop.set()
        self._backfill_wake.set()
        self._analysis_wake.set()
        self._paper_wake.set()
        self._first_cycle.set()  # ends the context worker's wait for the first cycle
        self._context_ready.set()  # ends the paper worker's wait for the first context refresh

    def _api_error(self, where: str, exc: SuperMarketError, source: Optional[str] = None) -> None:
        """Count a non-fatal error (and report it under ``source``); re-raise fatal ones (after
        stopping) and shutdown cancels. Only a problem with the key itself is fatal."""
        if isinstance(exc, RequestCancelled) and self._stop.is_set():
            raise _Stopping() from exc
        if is_fatal(exc):
            self._set_fatal(where, exc)
            raise exc
        self._forward_rate_limit(exc)
        self._record_error(where, exc, source)

    def _call(self, where: str, fn: Callable[[], Any], source: Optional[str] = None) -> Any:
        """``fn()``; on a non-fatal API error, report it under ``source`` and return None.
        A success clears that source's problem."""
        try:
            result = fn()
        except SuperMarketError as exc:
            self._api_error(where, exc, source)
            return None
        if source is not None:
            self._resolve(source)
        return result

    def _load_analyzed(self) -> Dict[int, float]:
        raw = self.store.get_state(ANALYZED_STATE_KEY, {})
        out: Dict[int, float] = {}
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                try:
                    out[int(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        return out

    # ------------------------------------------------------------------ paper trading / fair values plumbing

    def _make_paper_runner(self, engine: Any) -> None:
        """``PaperRunner(engine, TrackerMarketReader(self), self._paper_inputs, ...)`` (§7.2). A paper module
        that is not available in this build turns the paper trader off with a plain reason (never a crash)."""
        from . import paper as paper_mod

        try:
            self.paper_runner = paper_mod.PaperRunner(
                engine, TrackerMarketReader(self), self._paper_inputs, limiter=self.paper_limiter, clock=self._clock,
                interval=self.interval, extras_fn=self._paper_extras,
            )
        except NotImplementedError:
            self.paper_runner = None
            self._paper_error = "The paper trader is not available in this build."
            log.warning("paper trading disabled: %s", self._paper_error)

    @property
    def paper_error(self) -> Optional[str]:
        """Why a configured paper trader is not running (None when it runs, or when none was configured)."""
        return self._paper_error

    def _backfill_pending(self) -> bool:
        with self._lock:
            return any(len(q) for q in self._bf_queues.values())

    def _paper_extras(self) -> Dict[str, Any]:
        """Keys the runner adds to its published summary (it calls this under the runner lock: no tracker lock)."""
        return {"fair_value": self._fair_value_compact()}

    def _fair_value_compact(self) -> Optional[Dict[str, Any]]:
        if self.fair_values is None:
            return None
        try:
            st = self.fair_values.status()
        except Exception as exc:
            log.debug("fair value status failed: %s", exc)
            return None
        return {"mode": st.get("mode"), "enabled": bool(st.get("enabled")), "usable": int(st.get("usable") or 0),
                "total": int(st.get("total") or 0)}

    def _view_copy(self) -> Dict[str, Any]:
        """What the paper inputs need from the cached view (takes the lock briefly). Rows are copied shallowly
        (each cycle publishes new row dicts; only the surge rows are patched in place, and they are left out),
        without their sparklines; the context is copied deeply."""
        with self._lock:
            if self._view is not None:
                view = {
                    "exchanges": [{k: v for k, v in r.items() if k != "sparkline"} for r in self._view.get("exchanges") or []],
                    "high_band": [dict(b) for b in self._view.get("high_band") or []],
                    "surges": [],
                    "context": _copy_json(self._context),
                }
            else:
                view = {"exchanges": [], "surges": [], "high_band": [], "context": _copy_json(self._context), "status": {}}
            markets_at = self._markets_at if self._markets is not None else None
        view["status"] = {"markets_updated_at": markets_at}
        return view

    def _paper_inputs(self, now: float) -> Tuple[Any, Any]:
        """``pipeline.assemble_inputs`` + ``pipeline.observation`` from the cached view and the store (no reads)."""
        view = self._view_copy()
        inputs = pipeline.assemble_inputs(
            now=now, view=view, store=self.store, fair_values=self.fair_values, regime=self.regime,
            params=self.strategy_params, recent_mids=self.recent_mids(pipeline.RECENT_MIDS_S),
        )
        obs = pipeline.observation(now=now, inputs=inputs, view=view, store=self.store, open_ids=sorted(self._open_ids()))
        return inputs, obs

    def recent_mids(self, seconds: float = pipeline.RECENT_MIDS_S) -> Dict[str, List[Tuple[float, float]]]:
        """exchange id -> [(ts, YES mid)] over the last ``seconds`` before the latest cycle, oldest first, from the
        in-memory series cache (no SQL). Lock-free: reads the snapshot the loop published at its last cycle."""
        published = self._published
        at = published.get("at")
        if at is None:
            return {}
        cutoff = float(at) - float(seconds)
        return {eid: [p for p in pts if p[0] >= cutoff - 1e-9] for eid, pts in published.get("recent", {}).items()}

    def cup_mids(self) -> Dict[str, float]:
        """exchange id -> the Cup's current YES mid from the last published view rows (lock-free)."""
        return dict(self._published.get("mids") or {})

    def cup_quotes(self) -> Dict[str, Any]:
        """exchange id -> ``moves.CupQuote`` (ts = the snapshot read's completion, bid, ask, mid, last, spread) of every
        open, non-stale outcome in the last published snapshot; lock-free (docs/OUTSIDE_MOVES.md §13.2)."""
        from .moves import CupQuote

        quotes = self._published.get("quotes") or {}
        return {eid: CupQuote(ts=q["ts"], bid=q["bid"], ask=q["ask"], mid=q["mid"], last=q["last"], spread=q["spread"])
                for eid, q in quotes.items()}

    def cup_history(self, seconds: float) -> Dict[str, List[Any]]:
        """exchange id -> ``moves.CupQuote`` tick snapshots of the last ``seconds`` (oldest first) from the in-memory
        series cache, taking ``self._lock`` briefly (never called while the caller holds another lock; §13.2)."""
        from .moves import CupQuote

        now = float(self._clock())
        cutoff = now - float(seconds)
        with self._lock:  # copy plain tuples only; the CupQuotes are built after the lock is released
            # list(...) copies the items atomically: the loop thread sets self._series[eid] in _points without
            # this lock, and a new key during the iteration would raise "dictionary changed size"
            raw = {
                eid: [(p.ts, p.bid, p.ask, p.price, p.last) for p in pts
                      if p.source == "tick" and cutoff - 1e-9 <= p.ts <= now + 1e-9]
                for eid, pts in list(self._series.items()) if eid in self._infos
            }
        out: Dict[str, List[Any]] = {}
        for eid, pts in raw.items():
            quotes: List[Any] = []
            for ts, bid, ask, price, last in pts:
                two_sided = bid is not None and ask is not None
                mid = round((bid + ask) / 2, 6) if two_sided else price
                if mid is None:
                    continue
                quotes.append(CupQuote(ts=float(ts), bid=bid, ask=ask, mid=float(mid), last=last,
                                       spread=round(ask - bid, 6) if two_sided else None))
            if quotes:
                out[eid] = quotes
        return out

    def _publish(self, now: float, rows: Sequence[Mapping[str, Any]]) -> None:
        """Replace the lock-free snapshot behind :meth:`cup_mids` and :meth:`recent_mids` (end of each cycle)."""
        mids: Dict[str, float] = {}
        # the outside-move watcher's Cup quotes (§13.2): ts = the row's updated_at, the snapshot read's completion
        quotes: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            bid, ask, mark = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("mark"))
            mid = round((bid + ask) / 2, 6) if bid is not None and ask is not None else mark
            if mid is not None and not row.get("stale"):
                eid = str(row["exchange_id"])
                mids[eid] = mid
                updated = _num(row.get("updated_at"))
                if updated is not None:
                    quotes[eid] = {"ts": updated, "bid": bid, "ask": ask, "mid": mid, "last": _num(row.get("last")),
                                   "spread": round(ask - bid, 6) if bid is not None and ask is not None else None}
        recent: Dict[str, List[Tuple[float, float]]] = {}
        cutoff = now - pipeline.RECENT_MIDS_S
        for eid in mids:
            pts = self._series.get(eid) or []
            out: List[Tuple[float, float]] = []
            for p in reversed(pts):
                if p.ts < cutoff:
                    break
                if p.ts > now or p.source != "tick":
                    continue
                mid = round((p.bid + p.ask) / 2, 6) if p.bid is not None and p.ask is not None else p.price
                if mid is not None:
                    out.append((p.ts, float(mid)))
            recent[eid] = list(reversed(out))
        self._published = {"mids": mids, "recent": recent, "at": now, "quotes": quotes}

    def _update_projection(self) -> None:
        """The §9 read projection from the live outcome count (start-up and after each market-list refresh)."""
        with self._lock:
            n = len(self._infos)
            markets = list(self._markets or [])
            backfill_pending = any(len(q) for q in self._bf_queues.values())
        m = len(markets)
        multi = sum(1 for mk in markets if isinstance(mk, Mapping) and mk.get("isMultiOutcome"))
        bulk = math.ceil(n / 100) * 60.0 / self.interval if n else 0.0
        lists = math.ceil(m / 100) * 60.0 / max(1.0, self.market_refresh) if m else 0.0
        context = (4 + IDEA_BOOKS_PER_REFRESH + min(self.max_overround_markets, multi)) * 60.0 / max(1.0, self.context_refresh)
        settlements = 60.0 / max(1.0, self.settlement_refresh)
        backfill = float(self.backfill_limiter.limit) if self.backfill_enabled and backfill_pending else 0.0
        analysis = ANALYSIS_READS_PER_MIN if self.analyze_enabled and self.attributor is not None else 0.0
        paper = float(self.paper_limiter.limit) if self.paper_runner is not None else 0.0
        # the outside-move watcher's Cup book reads (§13.3, its worst case; the outside polls never read the Cup)
        moves = float(self.moves_reads_per_min) if getattr(self, "moves", None) is not None else 0.0
        total = round(bulk + lists + context + settlements + backfill + analysis + paper + moves + DRAWER_READS_PER_MIN, 1)
        warning = None
        # Only a client capped like a real account (<= 100/min) shares the account limit; the demo's simulated
        # API has budgets far above it, and warning about the account there would be wrong.
        client_limit = getattr(getattr(self.client, "read_limiter", None), "limit", None)
        real_account = not isinstance(client_limit, (int, float)) or client_limit <= ACCOUNT_READS_PER_MIN
        if real_account and total > PROJECTED_WARN_PER_MIN:
            warning = (f"Projected reads ({total:.0f}/min) are close to the account limit of 100 per minute shared by all "
                       "your keys: do not run other scripts on this account.")
        with self._lock:
            self._projected = {"per_min": total, "warning": warning}
        if warning:
            self._problem("read budget", warning)
        else:
            self._resolve("read budget")

    # ------------------------------------------------------------------ lease (one tracker per store, D46)

    def _ensure_lease(self) -> None:
        """Take or renew the store lease; raise TrackerBusy when another live process holds it."""
        if not self.lease_enabled:
            return
        acquire = getattr(self.store, "acquire_lease", None)
        if not callable(acquire):
            return
        now = self._clock()
        holder = acquire(LEASE_NAME, self._owner, now, ttl_s=self._lease_ttl)
        if holder is not None:
            raise TrackerBusy(holder, getattr(self.store, "path", ""), ttl_s=self._lease_ttl, now=now)
        taken = getattr(self.store, "last_takeover", None)
        if isinstance(taken, Mapping) and not self._lease_held:
            if taken.get("alive") is False:
                log.warning("tracker: the previous run (pid %s on %s) ended without shutting down; continuing it",
                            taken.get("pid"), taken.get("host"))
            else:
                log.warning("tracker: took over the lease of pid %s on %s (no heartbeat for %.0f s)",
                            taken.get("pid"), taken.get("host"), float(_num(taken.get("age_s")) or 0.0))
        self._lease_held = True

    def _renew_lease(self) -> None:
        if not self.lease_enabled or not self._lease_held or self._stop.is_set():
            return  # (after stop() the lease is released: a late heartbeat must not take it back)
        try:
            ok = self.store.renew_lease(LEASE_NAME, self._owner["owner_id"], self._clock())
        except Exception as exc:
            if not _storage_error(exc):
                raise
            self._storage_failed("renewing the tracker lease", exc)
            return
        if not ok:
            try:
                self._ensure_lease()  # our heartbeat lapsed (the machine slept): take it back if nobody else did
            except TrackerBusy as exc:
                self._set_fatal("tracker lease", exc)

    def _release_lease(self) -> None:
        if not self._lease_held:
            return
        try:
            self.store.release_lease(LEASE_NAME, self._owner["owner_id"])
        except Exception as exc:
            log.debug("could not release the tracker lease: %s", exc)
        self._lease_held = False

    # ------------------------------------------------------------------ cycle

    def run_once(self) -> Dict[str, Any]:
        """Run one cycle synchronously and return a summary of what it did.

        Summary keys: ``at``, ``markets``, ``exchanges``, ``ticks`` (written this cycle),
        ``surges`` (ids recorded), ``new_surges`` (ids created), ``queued`` (ids queued for
        attribution), ``high_band`` (count), ``errors`` (new errors this cycle),
        ``markets_refreshed``, ``context_refreshed``, ``stopped``.

        Fatal API errors (``bot.is_fatal``: only problems with the key itself) set
        ``status()["fatal_error"]``, stop the tracker and are re-raised. Everything else is
        counted and reported in ``status()["problems"]``.
        """
        self._ensure_lease()
        try:
            return self._run_once()
        except _Stopping:
            return {"at": self._clock(), "stopped": True}

    def _run_once(self, refresh_context: bool = True) -> Dict[str, Any]:
        """One cycle; ``refresh_context=False`` when the context worker thread does that part."""
        with self._cycle_lock:
            started = time.monotonic()
            with self._lock:
                errors_before = self._errors
            summary: Dict[str, Any] = {
                "at": None, "markets": 0, "exchanges": 0, "ticks": 0, "surges": [], "new_surges": [],
                "queued": [], "high_band": 0, "errors": 0, "markets_refreshed": False,
                "context_refreshed": False, "stopped": False,
            }
            now = self._clock()
            if not self._requeued:
                self._requeued = True
                summary["queued"].extend(self._requeue_unanalyzed(now))

            # 1. market list (early when outcomes went missing from the bulk prices: they may have settled)
            if self._markets is None or now - self._markets_at >= self.market_refresh or self._missing_changed(now):
                markets = self._call("market list", lambda: self._bot.list_markets(status="open"), "markets")
                if markets is not None:
                    try:
                        self.store.upsert_markets(markets)
                    except Exception as exc:
                        if not _storage_error(exc):
                            raise
                        self._storage_failed("storing the market list", exc)
                    self._set_markets(markets, now)
                    summary["markets_refreshed"] = True
                with self._lock:
                    self._missing_listed = set(self._missing)
                    self._missing_refresh_at = now

            # 2. snapshot
            if self._markets:
                markets_now = self._markets
                snap = self._call("price snapshot", lambda: self._bot.snapshot(markets_now), "prices")
                if snap is not None:
                    now = self._clock()
                    missing = {str(eid) for eid in snap.missing_ids or []}
                    missing |= {str(r.get("exchange_id")) for r in snap.rows if r.get("missing")}
                    rows = [r for r in snap.rows if str(r.get("exchange_id")) not in missing]
                    added = 0
                    try:
                        added = self.store.add_ticks(now, rows)
                    except Exception as exc:
                        if not _storage_error(exc):
                            raise
                        # Keep going on the in-memory snapshot: prices, surges and the view stay
                        # live, and the banner says why history stopped growing.
                        self._storage_failed("saving price snapshot", exc)
                    else:
                        self._resolve("storage")
                    with self._lock:
                        self._rows = {str(r["exchange_id"]): r for r in rows if r.get("exchange_id") is not None}
                        self._missing = missing
                        self._last_snapshot_at = now
                        self._ticks_recorded += added
                    summary["ticks"] = added

            # 3 + 4. analytics, surges, attribution queue
            results = self._analyze_all(now, summary)
            self._maybe_recheck_news(now)

            # 5. context (inline only in synchronous use; the threaded tracker has a worker for it)
            if refresh_context and self._context_due(now):
                self._refresh_context(now)
                summary["context_refreshed"] = True
            if refresh_context and self._settlements_due(now):
                self._refresh_settlements(now)

            self._build_view(now, results)
            with self._lock:
                self._cycles += 1
                self._last_cycle_at = now
                self._last_cycle_s = round(time.monotonic() - started, 4)
                summary["errors"] = self._errors - errors_before
            self._resolve("tracker")  # a whole cycle ran: an earlier crash is over
            self._first_cycle.set()
            self._paper_wake.set()
            summary.update(at=now, markets=len(self._markets or []), exchanges=len(self._infos))
            summary["high_band"] = sum(1 for r in results.values() if r.get("band") is not None)
            return summary

    def _missing_changed(self, now: float) -> bool:
        """Outcomes went missing from the bulk prices since the last market-list read (and that
        read is at least ``MISSING_REFRESH_MIN_S`` old): re-read the list now, so a settled
        market leaves the open list within a cycle instead of after ``market_refresh``."""
        with self._lock:
            new = bool(self._missing - self._missing_listed)
            return new and now - self._missing_refresh_at >= MISSING_REFRESH_MIN_S

    def _open_ids(self) -> Set[str]:
        """Outcomes that count as open: on the market list and quoted by the latest bulk read."""
        with self._lock:
            return set(self._infos) - self._missing

    def _set_markets(self, markets: List[Dict[str, Any]], now: float) -> None:
        try:
            stored = self.store.exchanges()
        except Exception as exc:
            if not _storage_error(exc):
                raise
            self._storage_failed("reading the stored outcomes", exc)
            stored = []
        all_infos = {info.exchange_id: info for info in stored}
        infos: Dict[str, ExchangeInfo] = {}
        for market in markets:
            for ex in market.get("exchanges") or []:
                if ex.get("id") is None:
                    continue
                eid = str(ex["id"])
                infos[eid] = all_infos.get(eid) or ExchangeInfo(
                    exchange_id=eid,
                    market_id=str(market.get("id")),
                    option=ex.get("option"),
                    market_title=market.get("title") or "",
                    settlement_date=market.get("settlementDate"),
                    initial_price=_num(ex.get("initialPrice")),
                )
        with self._lock:
            self._markets = list(markets)
            self._markets_at = now
            self._infos = infos
            self._all_infos = all_infos
            for eid in list(self._series):
                if eid not in infos:
                    self._series.pop(eid, None)
                    self._series_loaded.pop(eid, None)
        if self.backfill_enabled:
            self._plan_backfill(list(infos))
        # Lock rule (D43): the fair-value service is called only after the tracker's lock is released.
        if self.fair_values is not None:
            try:
                self.fair_values.set_targets(list(infos.values()))
            except Exception as exc:
                self._record_error("fair value targets", repr(exc), "fair value")
        self._update_projection()

    # ------------------------------------------------------------------ analytics

    def _points(self, eid: str, now: float) -> List[PricePoint]:
        """The last 30 h of points, cached in memory and topped up with new ticks each cycle."""
        since = now - SERIES_LOOKBACK_S
        with self._lock:
            dirty = eid in self._dirty
            self._dirty.discard(eid)
        cached = self._series.get(eid)
        stale = now - self._series_loaded.get(eid, float("-inf")) >= SERIES_RELOAD_S
        if not cached or dirty or stale or cached[-1].ts > now:
            points = self.store.series(eid, since)
            self._series_loaded[eid] = now
        else:
            after = cached[-1].ts
            fresh = [p for p in self.store.series(eid, after, include_candles=False) if p.ts > after]
            drop = 0
            while drop < len(cached) and cached[drop].ts < since:
                drop += 1
            points = cached[drop:] + fresh
        self._series[eid] = points
        return points

    def _analyze_all(self, now: float, summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            infos = dict(self._infos)
        with self._lock:
            missing = set(self._missing)
        results: Dict[str, Dict[str, Any]] = {}
        failed: List[str] = []
        first_error: Optional[BaseException] = None
        storage_error: Optional[BaseException] = None
        states: Dict[str, int] = {"ready": 0, "pending": 0, "unavailable": 0}
        for eid, info in infos.items():
            result: Dict[str, Any] = {"last": None, "band": None, "changes": {}, "sparkline": []}
            results[eid] = result
            try:
                points = self._points(eid, now)
                result["last"] = points[-1] if points else None
                state = self._history_state(eid, now)
                states[state] += 1
                self._analyze_exchange(eid, info, points, now, result, summary, state, quoted=eid not in missing)
            except Exception as exc:  # one broken series must not stop the others
                if _storage_error(exc):
                    storage_error = storage_error or exc
                    continue
                failed.append(eid)
                first_error = first_error or exc
        if storage_error is not None:
            self._storage_failed("recording surges", storage_error)
        if failed:
            log.debug("analytics failure detail", exc_info=first_error)
            self._record_error("analytics", f"{len(failed)} outcome(s), e.g. {failed[0]}: {first_error!r}", "analytics")
        else:
            self._resolve("analytics")
        self._set_detection(len(infos), states)
        try:
            self._update_open_surges(now, results)
        except Exception as exc:
            if not _storage_error(exc):
                raise
            self._storage_failed("updating surge status", exc)
        return results

    def _analyze_exchange(
        self, eid: str, info: ExchangeInfo, points: List[PricePoint], now: float,
        result: Dict[str, Any], summary: Dict[str, Any], state: str = "ready", quoted: bool = True,
    ) -> None:
        recent = [p for p in points if p.ts >= now - SPARKLINE_WINDOW_S and p.ts <= now and p.price is not None]
        spark = analytics.downsample_by_time(recent, SPARKLINE_POINTS, now - SPARKLINE_WINDOW_S, now)
        result["sparkline"] = [[p.ts, p.price] for p in spark]
        result["changes"] = {name: analytics.change_over(points, now, secs) for name, secs in CHANGE_WINDOWS}
        if not quoted:
            # Missing from the bulk prices (settled, closed or out of scope): no fresh mark, so no
            # high band (no carry idea) and no new surges on its last stored price.
            return
        # A band needs a current mark (an 8-hour-old price after a restart is not "in the band now").
        result["band"] = analytics.high_band(points, now, eid, info.market_id, settlement_date=info.settlement_date,
                                             max_staleness_s=BAND_MAX_STALENESS_S)
        if state == "pending":
            # Detecting on partial history mis-sizes the window (e.g. a 20-minute spike read as a
            # 24h move); wait, briefly, for this outcome's candle backfill (see _history_state).
            return
        if state == "unavailable":
            # No candle history (backfill off, failing or slow): detect from live prices, and let a
            # window that reaches back before tracking started measure the move since then.
            found = analytics.detect_surges(points, now, eid, info.market_id, partial=True)
        else:
            found = analytics.detect_surges(points, now, eid, info.market_id)
        for surge in found:
            recorded = self._record_surge(surge)
            if recorded is None:
                continue
            stored, is_new = recorded
            summary["surges"].append(stored.id)
            if is_new:
                summary["new_surges"].append(stored.id)
            if self._maybe_queue(stored):
                summary["queued"].append(stored.id)

    def _history_pending(self, eid: str) -> bool:
        return self._history_state(eid, self._clock()) == "pending"

    def _history_state(self, eid: str, now: float) -> str:
        """``"ready"`` (candle backfill done), ``"pending"`` (detection waits for it) or
        ``"unavailable"`` (detect from live prices).

        Waiting is short on purpose: only while this outcome's backfill is still queued, has not
        failed yet, the backfill as a whole is healthy and at most ``HISTORY_WAIT_MAX_S`` have
        passed since it was planned. A price-history outage must not switch detection off.
        """
        if not self.backfill_enabled:
            return "unavailable"
        with self._lock:
            keys = [(eid, res) for res, _ in BACKFILL_PLAN]
            if all(k in self._bf_done for k in keys):
                return "ready"
            failed = any(k in self._bf_failed or self._bf_attempts.get(k, 0) > 0 for k in keys)
            planned = self._bf_planned_at.get(eid)
            healthy = not self._backfill_down()
        if failed or not healthy:
            return "unavailable"
        if planned is not None and now - planned >= HISTORY_WAIT_MAX_S - _EPS:
            return "unavailable"
        return "pending"

    def _set_detection(self, total: int, states: Mapping[str, int]) -> None:
        waiting, live = int(states.get("pending", 0)), int(states.get("unavailable", 0))
        reason: Optional[str] = None
        if total == 0:
            with self._lock:
                loaded = self._markets is not None
            reason = ("No open outcomes to watch in this tournament" if loaded
                      else "Waiting for the market list before surge detection can start")
        elif waiting:
            reason = (f"Waiting for price history for {waiting} of {total} outcome(s) before detecting surges there "
                      f"(at most {HISTORY_WAIT_MAX_S / 60:.0f} min)")
        elif live:
            if not self.backfill_enabled:
                why = "price-history backfill is off"
            else:
                with self._lock:
                    problem = self._problems.get("price history")
                    last_error = self._bf_last_error
                    gave_up = bool(self._bf_failed)
                if problem:
                    why = f"price history is unavailable ({problem['message']})"
                elif gave_up and last_error:
                    why = f"price history could not be loaded ({last_error})"
                else:
                    why = "price history is not loaded yet"
            reason = (f"For {live} of {total} outcome(s) {why}: surges are detected from live prices only, so moves "
                      "from before tracking started are not seen, and the high-90s check needs 6 h of prices")
        with self._lock:
            self._detection = {
                "enabled": total > 0 and waiting < total,
                "waiting_for_history": waiting,
                "live_only": live,
                "reason": reason,
            }

    def _record_surge(self, surge: Surge) -> Optional[Tuple[Surge, bool]]:
        """Store a detection, unless it only re-detects a known move or is a known move's reversion.

        * The detection's window reaches back over a stored move of the same direction (it
          starts before that move's frame ended) and goes no further than its peak: the same
          move seen again (for example a plateau seen through the 24h window hours later). An
          open surge absorbs it, whatever its age; a reverted, held or closed one ignores it.
        * The detection runs against a recent surge (opposite direction, starting after that
          surge started) and stays inside its start -> peak range: it is that surge giving its
          move back, which the surge's own status reports ("reverted"). Recording it as a new
          surge would turn the reversion of a participant spike into a "surge" to fade.

        Returns ``(stored surge, created)``, or None when the detection was ignored.
        """
        recent = self.store.surges(exchange_id=surge.exchange_id, limit=10)
        if any(self._is_reversion(surge, old) for old in recent):
            return None
        into: Optional[int] = None
        for old in recent:  # newest first
            if old.direction != surge.direction or surge.start_ts > old.end_ts + _EPS:
                continue  # another direction, or the window starts after the old move ended: a new move
            if surge.direction == "down":
                beyond = surge.end_price < old.peak_price - _EPS
            else:
                beyond = surge.end_price > old.peak_price + _EPS
            if old.status == SURGE_OPEN:
                into = old.id  # the same (or a growing) move: merge into it however old it is
                break
            if not beyond:
                return None  # the same (reverted, held or closed) move seen again through a longer window
        known = {s.id for s in recent}
        stored = self.store.record_surge(surge, into=into)
        return stored, stored.id not in known

    @staticmethod
    def _is_reversion(surge: Surge, old: Surge) -> bool:
        """``surge`` is ``old`` giving its move back (see :meth:`_record_surge`)."""
        if old.direction == surge.direction or old.id is None:
            return False
        if surge.start_ts < old.start_ts - _EPS:
            return False  # it started before the old move: not a reaction to it
        if surge.start_ts > old.end_ts + max(old.window_s, 0.0) + REVERSION_LINK_S:
            return False  # long after: a move of its own
        if old.direction == "down":
            within = surge.end_price <= old.start_price + _EPS and surge.end_price >= old.peak_price - _EPS
        else:
            within = surge.end_price >= old.start_price - _EPS and surge.end_price <= old.peak_price + _EPS
        return within

    def _update_open_surges(self, now: float, results: Mapping[str, Mapping[str, Any]]) -> None:
        """Refresh every open surge's status; close the surges of markets that left the open list.

        A surge whose outcome is no longer open (it left the open-market list, or the bulk price
        read stopped returning it: closed or settled) becomes ``closed`` with its last real
        price as the final one: it no longer counts as open and gets no trade ideas. That holds
        for reverted and held surges of the last 48 h too, so their card reads "Market closed".
        If the market shows up again, its closed surges reopen and are re-evaluated.

        Reverted and held surges of the last 48 h on open markets keep their status but follow
        the live price: their ``current_price`` ("Now") and ``reverted_fraction`` stay current.
        """
        try:
            open_surges = self.store.surges(status=SURGE_OPEN, limit=1000)
        except Exception as exc:
            self._record_error("surge status", exc, "storage")
            return
        with self._lock:
            listed = self._markets is not None
        open_ids = self._open_ids()
        reopened: Set[Optional[int]] = set()
        settled: List[Surge] = []
        if listed:
            since = now - SURGE_VIEW_LOOKBACK_S
            try:
                closed = self.store.surges(since=since, status=SURGE_CLOSED, limit=200)
                settled = [s for status in (SURGE_REVERTED, SURGE_HELD)
                           for s in self.store.surges(since=since, status=status, limit=200)]
            except Exception as exc:
                self._record_error("surge status", exc, "storage")
                closed, settled = [], []
            for surge in closed:
                if surge.exchange_id in open_ids:
                    surge.status = SURGE_OPEN  # re-evaluated below like any open surge
                    surge.attribution = None
                    reopened.add(surge.id)
                    open_surges.append(surge)
        failures = 0
        for surge in open_surges:
            if listed and surge.exchange_id not in open_ids:
                surge.status = SURGE_CLOSED
                surge.attribution = None  # never overwrite an analysis that finished meanwhile
                self.store.update_surge(surge)
                continue
            last = (results.get(surge.exchange_id) or {}).get("last")
            current = last.price if last is not None and last.price is not None else surge.current_price
            before = (surge.status, surge.current_price, surge.reverted_fraction, surge.peak_price)
            try:
                analytics.update_surge_status(surge, current, now)
            except Exception as exc:
                failures += 1
                if failures == 1:
                    self._record_error("surge status", repr(exc))
                continue
            if (surge.status, surge.current_price, surge.reverted_fraction, surge.peak_price) != before or \
                    surge.id in reopened:
                surge.attribution = None  # never overwrite an analysis that finished meanwhile
                self.store.update_surge(surge)
        for surge in settled:
            if surge.exchange_id not in open_ids:
                surge.status = SURGE_CLOSED  # the market closed or settled after the move resolved
                surge.attribution = None
                self.store.update_surge(surge)
                continue
            last = (results.get(surge.exchange_id) or {}).get("last")
            if last is None or last.price is None:
                continue
            status = surge.status
            before = (surge.current_price, surge.reverted_fraction)
            try:
                analytics.update_surge_status(surge, last.price, now)
            except Exception as exc:
                failures += 1
                if failures == 1:
                    self._record_error("surge status", repr(exc))
                continue
            surge.status = status  # reverted and held are final: only the live numbers move
            if (surge.current_price, surge.reverted_fraction) != before:
                surge.attribution = None
                self.store.update_surge(surge)

    # ------------------------------------------------------------------ attribution queue

    def _requeue_unanalyzed(self, now: float) -> List[int]:
        """Queue stored surges from the last 48 h that have no analysis yet (e.g. the dashboard
        stopped before the analysis worker reached them). Returns the ids queued."""
        if not self.analyze_enabled or self.attributor is None:
            return []
        try:
            stored = self.store.surges(since=now - SURGE_VIEW_LOOKBACK_S, limit=SURGE_VIEW_LIMIT)
        except Exception as exc:
            self._record_error("surge list", exc, "storage")
            return []
        queued: List[int] = []
        with self._lock:
            for surge in stored:  # newest first
                if surge.attribution is not None or surge.id is None:
                    continue
                sid = int(surge.id)
                if sid in self._queued:
                    continue
                self._queue.append(sid)
                self._queued.add(sid)
                queued.append(sid)
        if queued:
            log.info("tracker: %d stored surge(s) had no analysis yet; queued them", len(queued))
            self._analysis_wake.set()
        return queued

    def _maybe_queue(self, stored: Surge) -> bool:
        if not self.analyze_enabled or self.attributor is None or stored.id is None:
            return False
        sid = int(stored.id)
        with self._lock:
            if sid in self._queued:
                return False
            baseline = self._analyzed_change.get(sid)
            if baseline is None and stored.attribution is not None:
                # analysed in an earlier run whose baseline was not saved: start from today's size
                self._analyzed_change[sid] = abs(stored.change)
                self._analyzed_window[sid] = stored.window
                return False
            reframed = sid in self._analyzed_window and self._analyzed_window[sid] != stored.window
            if baseline is not None and not reframed and abs(stored.change) - baseline < REANALYZE_GROWTH - _EPS:
                return False
            self._queue.append(sid)
            self._queued.add(sid)
        self._analysis_wake.set()
        return True

    def request_analysis(self, surge_id: int) -> bool:
        """Queue a surge for (re-)attribution ahead of automatic ones.

        True when the surge is queued (now or already); False without an attributor or for
        an unknown surge.
        """
        if self.attributor is None:
            return False
        try:
            sid = int(surge_id)
        except (TypeError, ValueError):
            return False
        if self.store.get_surge(sid) is None:
            return False
        with self._lock:
            if sid not in self._queued:
                self._queue.appendleft(sid)
                self._queued.add(sid)
            self._patch_view_pending(sid, True)
        self._analysis_wake.set()
        return True

    def analyze_pending(self, max_items: int = 1) -> int:
        """Run attribution for up to ``max_items`` queued surges (synchronous)."""
        if self.attributor is None:
            return 0
        try:
            return self._analyze_pending(max_items)
        finally:
            with self._lock:
                self._analyzing = None

    def _analyze_pending(self, max_items: int) -> int:
        done = 0
        while done < max_items:
            with self._lock:
                self._analyzing = None
                if not self._queue:
                    break
                sid = self._queue.popleft()
                self._queued.discard(sid)
                self._analyzing = sid
            try:
                surge = self.store.get_surge(sid)
                if surge is None:
                    continue
                attribution = self.attributor.analyze(surge)
            except _Stopping:
                with self._lock:
                    if sid not in self._queued:
                        self._queue.appendleft(sid)
                        self._queued.add(sid)
                raise
            except SuperMarketError as exc:
                self._api_error(f"attribution of surge {sid}", exc, "attribution")
                self._mark_not_pending(sid)
                continue
            except Exception as exc:
                log.debug("attribution failure detail", exc_info=True)
                self._record_error(f"attribution of surge {sid}", repr(exc), "attribution")
                self._mark_not_pending(sid)
                continue
            if attribution is None:
                self._mark_not_pending(sid)
                continue
            # lookahead-3: the verdict exists for a reader (the paper step, a replay's analyzed_at <= t) only from
            # now, when analyze() returned -- not from the clock when the analysis started (reads, the news search
            # and the LLM judge can take a minute)
            done_at = self._clock()
            started_at = _num(attribution.analyzed_at)
            attribution.analyzed_at = max(done_at, started_at) if started_at is not None else done_at
            self._resolve("attribution")
            news_status = getattr(attribution, "news_status", None)
            if news_status == NEWS_UNAVAILABLE:
                failed = next((r for r in attribution.reasons if r.startswith("News search failed")), "News search failed")
                self._problem("news", failed.split(": missing headlines")[0])
                with self._lock:
                    if self._news_recheck_at is None:
                        self._news_recheck_at = self._clock() + self._news_recheck_wait
            elif news_status == NEWS_OK:
                self._news_recovered(sid)
            self.store.set_attribution(sid, attribution)
            with self._lock:
                self._analyzed_change[sid] = abs(surge.change)
                self._analyzed_window[sid] = surge.window
                self._analyses += 1
                if len(self._analyzed_change) > MAX_ANALYZED_REMEMBERED:
                    for old in sorted(self._analyzed_change)[: len(self._analyzed_change) - MAX_ANALYZED_REMEMBERED]:
                        del self._analyzed_change[old]
                remembered = {str(k): v for k, v in self._analyzed_change.items()}
                self._patch_view_attribution(sid, attribution.to_dict())
            self.store.set_state(ANALYZED_STATE_KEY, remembered)
            done += 1
        return done

    def _queue_back(self, ids: Sequence[int]) -> List[int]:
        """Queue surges for (re-)analysis behind the ones already waiting. Returns those queued."""
        queued: List[int] = []
        with self._lock:
            for sid in ids:
                if sid in self._queued or sid == self._analyzing:
                    continue
                self._queue.append(sid)
                self._queued.add(sid)
                queued.append(sid)
                self._patch_view_pending(sid, True)
        if queued:
            self._analysis_wake.set()
        return queued

    def _news_less(self, now: float) -> List[int]:
        """Ids of recent surges (newest first) whose analysis ran while news search was down."""
        try:
            recent = self.store.surges(since=now - SURGE_VIEW_LOOKBACK_S, limit=SURGE_VIEW_LIMIT)
        except Exception as exc:
            self._record_error("surge list", exc, "storage")
            return []
        return [int(s.id) for s in recent
                if s.id is not None and s.attribution is not None and s.attribution.news_status == NEWS_UNAVAILABLE]

    def _maybe_recheck_news(self, now: float) -> None:
        """While news search is reported failing, re-analyse one surge that was analysed without
        headlines, on a backoff (5 min, doubling to 30 min): its news search tells whether the
        outage is over (then the problem clears and the other such surges are re-analysed), and
        every failed retry updates the problem's "last failed" time and count."""
        if not self.analyze_enabled or self.attributor is None:
            return
        with self._lock:
            due = "news" in self._problems and self._news_recheck_at is not None and now >= self._news_recheck_at
        if not due:
            return
        candidates = self._news_less(now)
        with self._lock:
            self._news_recheck_wait = min(NEWS_RECHECK_MAX_S, self._news_recheck_wait * 2)
            self._news_recheck_at = now + self._news_recheck_wait
        if not candidates:
            # Nothing left that was analysed without news (re-analysed meanwhile, or older than 48 h):
            # there is no current evidence of an outage.
            self._news_recovered(None)
            return
        if self._queue_back(candidates[:1]):
            log.info("tracker: news search failed earlier; re-checking it with surge %s", candidates[0])

    def _news_recovered(self, sid: Optional[int]) -> None:
        """News search works (again): clear the problem, stop re-checking and re-analyse the recent
        surges whose verdict was made without headlines."""
        with self._lock:
            was_down = "news" in self._problems
            self._news_recheck_at = None
            self._news_recheck_wait = NEWS_RECHECK_S
        self._resolve("news")
        if was_down and sid is not None:
            others = [i for i in self._news_less(self._clock()) if i != sid]
            if others:
                self._queue_back(others)

    def _patch_view_attribution(self, sid: int, attribution: Dict[str, Any]) -> None:
        """Show a finished analysis right away instead of at the next cycle (caller holds the lock)."""
        if not self._view:
            return
        for row in self._view.get("surges") or []:
            if row.get("id") == sid:
                row["attribution"] = attribution
                row["analysis_pending"] = sid in self._queued

    def _patch_view_pending(self, sid: int, pending: bool) -> None:
        """Keep the view's ``analysis_pending`` flag current between cycles (caller holds the lock)."""
        if not self._view:
            return
        for row in self._view.get("surges") or []:
            if row.get("id") == sid:
                row["analysis_pending"] = pending

    def _mark_not_pending(self, sid: int) -> None:
        with self._lock:
            if sid not in self._queued:
                self._patch_view_pending(sid, False)

    # ------------------------------------------------------------------ backfill

    def _backfill_needed(self, eid: str, resolution: str) -> bool:
        record = self.store.get_state(_backfill_key(eid, resolution))
        at = _num(record.get("at")) if isinstance(record, Mapping) else None
        if at is None:
            return True
        if at >= self._session_start:
            return False
        # Backfilled in an earlier run: refetch only if tracking stopped for a while since.
        return not self.store.series(eid, self._session_start - RESUME_GAP_S, self._session_start)

    def _plan_backfill(self, eids: List[str]) -> None:
        with self._lock:
            new = [(eid, res) for res, _ in BACKFILL_PLAN for eid in eids if (eid, res) not in self._bf_known]
            self._bf_known.update(new)
        if not new:
            return
        needed = [(eid, res, self._backfill_needed(eid, res)) for eid, res in new]
        queued = 0
        planned_at = self._clock()
        with self._lock:
            for eid, _ in new:
                self._bf_planned_at.setdefault(eid, planned_at)
            for eid, res, need in needed:
                if need:
                    self._bf_queues[res].append(eid)
                    queued += 1
                else:
                    self._bf_done.add((eid, res))
        if queued:
            self._backfill_wake.set()

    def _next_backfill(self) -> Optional[Tuple[str, str, int]]:
        with self._lock:
            for res, limit in BACKFILL_PLAN:
                queue = self._bf_queues[res]
                while queue:
                    eid = queue.popleft()
                    if eid in self._infos:
                        return eid, res, limit
                    self._bf_done.add((eid, res))  # market closed meanwhile: nothing to chart
                    self._bf_retrying.discard((eid, res))
        return None

    def backfill_step(self, max_reads: int = 1) -> int:
        """Backfill candles for up to ``max_reads`` exchanges (synchronous; used by the worker and tests).

        Each read is one ``price-history`` request (1h x 168 for every outcome first, then
        5m x 288), sent once: the client does not retry it inline, so every attempt is one
        slot of the backfill budget. A failed series is retried later from the back of the
        queue (up to ``BACKFILL_MAX_ATTEMPTS``). When transient failures hit several different
        series in a row, price history is down: the whole queue rests (60 s, doubling up to
        10 min, or longer if the server asked) so snapshots keep their share of the budget.
        Returns the number of reads made.
        """
        if not self.backfill_enabled:
            return 0
        reads = 0
        while reads < max_reads:
            if self._backfill_resting():
                break
            task = self._next_backfill()
            if task is None:
                self._update_history_problem()  # nothing left to retry: no live problem
                break
            eid, res, limit = task
            try:
                self.backfill_limiter.acquire()
            except _Stopping:
                with self._lock:
                    self._bf_queues[res].appendleft(eid)
                raise
            reads += 1
            try:
                resp = self._quick.get_price_history(
                    eid, tournament_id=self.context.tournament_id, resolution=res, limit=limit
                )
            except SuperMarketError as exc:
                self._api_error(f"backfill {res} for exchange {eid}", exc)
                self._backfill_failed(eid, res, exc)
                continue
            candles = resp.get("candles") if isinstance(resp, Mapping) else None
            try:
                written = self.store.add_candles(eid, res, candles or [])
                self.store.set_state(_backfill_key(eid, res), {"at": self._clock(), "candles": written})
            except Exception as exc:
                if not _storage_error(exc):
                    raise
                self._storage_failed("saving price history", exc)
                with self._lock:
                    self._bf_queues[res].append(eid)  # the read worked: try it again later
                continue
            with self._lock:
                self._bf_done.add((eid, res))
                self._bf_retrying.discard((eid, res))
                self._bf_streak = []
                self._bf_backoffs = 0
                self._bf_pause_until = 0.0
                if written:
                    self._dirty.add(eid)
            self._update_history_problem()
        return reads

    def _update_history_problem(self, exc: Optional[BaseException] = None) -> None:
        """Keep the "price history" problem while a failed series is queued to be retried.

        A failure (``exc``) records or extends it, counting every failed read; a success on
        another series does not clear it. It goes away once every failed series has loaded or
        been given up: nothing is retrying then, and ``detection.reason`` says which outcomes
        are watched from live prices only and why.
        """
        if exc is not None:
            with self._lock:
                self._bf_last_error = _problem_text(exc, self._secrets)
                retrying = bool(self._bf_retrying)
            if retrying:
                self._problem("price history", exc)
                return
        with self._lock:
            retrying = bool(self._bf_retrying)
        if not retrying:
            self._resolve("price history")

    def _backfill_resting(self) -> bool:
        with self._lock:
            return time.monotonic() < self._bf_pause_until

    def _backfill_down(self) -> bool:
        """Price history looks down: the latest reads failed transiently on several different
        series and none has worked since (caller may hold the lock)."""
        with self._lock:
            return len(set(self._bf_streak)) >= BACKFILL_FAIL_STREAK

    def _backfill_failed(self, eid: str, res: str, exc: SuperMarketError) -> None:
        key = (eid, res)
        wait = _server_wait(exc)
        if isinstance(exc, ApiError) and exc.status == 429:
            with self._lock:
                # The account budget, not this series: try it again first, after the wait.
                self._bf_queues[res].appendleft(eid)
                self._bf_retrying.add(key)
                self._bf_pause_until = max(self._bf_pause_until, time.monotonic() + (wait or 1.0))
            self._update_history_problem(exc)
            return
        transient = _transient(exc)
        with self._lock:
            attempts = self._bf_attempts.get(key, 0) + 1
            self._bf_attempts[key] = attempts
            if transient and attempts < BACKFILL_MAX_ATTEMPTS:
                self._bf_queues[res].append(eid)  # try again after the rest of the queue
                self._bf_retrying.add(key)
            else:
                self._bf_failed.add(key)
                self._bf_retrying.discard(key)
        self._update_history_problem(exc)
        rest = 0.0
        with self._lock:
            if not transient:
                self._bf_streak = []  # the server answered: price history itself is up
                return
            self._bf_streak.append(eid)
            rest = wait or 0.0
            if len(set(self._bf_streak)) >= BACKFILL_FAIL_STREAK:
                rest = max(rest, min(BACKFILL_BACKOFF_MAX_S, BACKFILL_BACKOFF_S * (2 ** self._bf_backoffs)))
                self._bf_backoffs += 1
                self._bf_streak = self._bf_streak[-BACKFILL_FAIL_STREAK:]
            if rest > 0:
                self._bf_pause_until = max(self._bf_pause_until, time.monotonic() + rest)
        if rest > 0:
            log.info("tracker: price history failing (%s); backfill rests %.0fs", _problem_text(exc, self._secrets), rest)

    # ------------------------------------------------------------------ context

    def _context_due(self, now: float) -> bool:
        with self._lock:
            return now - self._context_at >= self.context_refresh

    def _refresh_context(self, now: float) -> None:
        """Constraints, balance, leaderboard and overround; each piece fails on its own.

        Every read is a single attempt (no inline retries or ``Retry-After`` waits): a failure,
        including a ``403 FORBIDDEN`` from a leaderboard the key cannot see yet or a ``503
        STANDINGS_UPDATING``, is reported in ``status()["problems"]`` and tried again at the next
        refresh. Only a problem with the key itself stops the tracker.
        """
        tid, slug = self.context.tournament_id, self.context.slug
        updates: Dict[str, Any] = {}
        quick = self._quick
        complete = False
        try:
            constraints = self._call(
                "constraints", lambda: quick.get_relationship_constraints(violations_only=True, tournament_id=tid),
                "constraints",
            )
            if isinstance(constraints, Mapping):
                violations = [v for v in constraints.get("data") or [] if isinstance(v, Mapping)]
                count = constraints.get("violationsCount")
                updates["constraints"] = {
                    "data": violations,
                    "violationsCount": count if isinstance(count, int) else len(violations),
                    "computedAt": constraints.get("computedAt"),
                }

            with self._lock:
                initial = self._context.get("initial_balance")
            if slug:
                info = self._call("tournament", lambda: quick.get_tournament(slug), "balance")
                if isinstance(info, Mapping):
                    updates["tournament"] = dict(info)
                    balance, start = _num(info.get("myBalance")), _num(info.get("initialBalance"))
                    if balance is not None:
                        updates["balance"] = balance
                    if start is not None:
                        updates["initial_balance"] = initial = start
                    updates.update(self._account_value(slug, balance))
                limit = self.leaderboard_limit
                board = self._call("leaderboard", lambda: quick.get_tournament_leaderboard(slug, limit=limit), "leaderboard")
                if isinstance(board, Mapping):
                    updates["leaderboard"] = self._leaderboard(board, initial)
                    self._store_leaderboard(updates["leaderboard"], initial, now)
            # the account part is known: publish it before the (slower) order-book reads below, so the first paper
            # step, which waits for it, starts the run on the account value (accounting-3)
            with self._lock:
                self._context.update(updates)
            self._context_ready.set()

            book_failures: List[str] = []
            book_reads = 0
            if self.max_overround_markets:
                rows, reads, failed = self._overround_rows(now)
                book_reads += reads
                book_failures += failed
                if rows is not None:
                    updates["overround"] = rows
            reads, failed = self._refresh_idea_books(now, updates.get("constraints"))
            book_reads += reads
            book_failures += failed
            if book_failures:
                self._problem("order books", f"{len(book_failures)} of {book_reads} order book read(s) failed: "
                                             f"{book_failures[-1]}")
            elif book_reads:
                self._resolve("order books")
            complete = True
        finally:
            with self._lock:
                self._context.update(updates)  # keep what was read even if a shutdown cut the refresh short
                if complete:
                    self._context["updated_at"] = now
                    self._context_at = now
            self._context_ready.set()  # the first attempt is over (read or not): the paper worker may start

    def _account_value(self, slug: str, cash: Optional[float]) -> Dict[str, Any]:
        """``account_value`` (cash + open positions at market value) and ``positions_value``.

        ``myBalance`` is cash only, while the leaderboard's value (initial + P&L) counts
        positions too: comparing the two would call a player with money in positions "far
        behind". Read from ``GET /tournaments/{slug}/portfolio/pnl`` (``totalAccountValue``);
        a failed read (e.g. ``409`` while a holding has no valuation price) leaves both None,
        and the strategy then says it compared cash. Not a banner problem: it is optional.
        """
        pnl = self._call("portfolio value", lambda: self._quick.get_tournament_pnl(slug, period="all"))
        if not isinstance(pnl, Mapping):
            return {}
        total, holdings = _num(pnl.get("totalAccountValue")), _num(pnl.get("totalHoldingsValue"))
        if total is None and holdings is not None and cash is not None:
            total = round(cash + holdings, 6)
        if holdings is None and total is not None and cash is not None:
            holdings = round(total - cash, 6)
        out: Dict[str, Any] = {}
        if total is not None:
            out["account_value"] = total
        if holdings is not None:
            out["positions_value"] = holdings
        return out

    def _save_book(self, eid: str, payload: Any, now: float, market_id: Optional[str] = None) -> bool:
        """Store one exchange's order book (``books.Book`` or an API book dict) for idea sizing."""
        from .books import Book

        try:
            book = payload if isinstance(payload, Book) else Book.from_payload(payload, market_id=market_id)
            self.store.put_book(eid, now, book.bids, book.asks)
        except Exception as exc:
            if not _storage_error(exc):
                log.debug("could not keep the order book of %s", eid, exc_info=True)
                return False
            self._storage_failed("saving an order book", exc)
            return False
        return True

    def _idea_candidates(self, constraints: Any) -> List[str]:
        """Outcomes a trade idea may size right now, most important first: the legs of
        engine-reported violations, open participant-driven surges (fades) and stable high
        bands (carry). Their books are read so the strategy caps sizes by real depth."""
        open_ids = self._open_ids()
        out: List[str] = []
        data = constraints.get("data") if isinstance(constraints, Mapping) else None
        if data is None:
            with self._lock:
                held = self._context.get("constraints")
            data = held.get("data") if isinstance(held, Mapping) else None
        for violation in data or []:
            if not isinstance(violation, Mapping):
                continue
            for trade in violation.get("suggestedCorrectiveTrades") or []:
                if isinstance(trade, Mapping) and trade.get("exchangeId") is not None:
                    out.append(str(trade["exchangeId"]))
        try:
            surges = self.store.surges(status=SURGE_OPEN, limit=200)
        except Exception:
            surges = []
        for surge in surges:
            att = surge.attribution
            if att is not None and att.verdict == VERDICT_PARTICIPANTS:
                out.append(str(surge.exchange_id))
        with self._lock:
            view = self._view or {}
            bands = [dict(b) for b in view.get("high_band") or [] if isinstance(b, Mapping)]
            spreads = {str(r.get("exchange_id")): _num(r.get("spread")) for r in view.get("exchanges") or []
                       if isinstance(r, Mapping)}

        def carry_edge(band: Mapping[str, Any]) -> float:
            # the carry rule's edge at the ask: min(0.01, 0.2 x room) less half the spread
            fav = _num(band.get("favorite_price")) or 1.0
            spread = spreads.get(str(band.get("exchange_id"))) or 0.0
            return min(0.01, 0.2 * (1.0 - fav)) - spread / 2

        stable = [b for b in bands if b.get("stable") and b.get("exchange_id") is not None]
        for band in sorted(stable, key=carry_edge, reverse=True):  # the ideas most likely to rank first
            out.append(str(band["exchange_id"]))
        return [eid for eid in dict.fromkeys(out) if eid in open_ids]

    def _refresh_idea_books(self, now: float, constraints: Any) -> Tuple[int, List[str]]:
        """Read the 20-level books of up to ``IDEA_BOOKS_PER_REFRESH`` idea candidates (skipping
        books stored less than half a refresh ago). Returns (reads, failure messages)."""
        tid = self.context.tournament_id
        reads = 0
        failures: List[str] = []
        for eid in self._idea_candidates(constraints):
            if reads >= IDEA_BOOKS_PER_REFRESH:
                break
            try:
                held = self.store.book(eid)
            except Exception:
                held = None
            if held is not None and now - held["at"] < min(IDEA_BOOK_MAX_AGE_S, 0.5 * self.context_refresh):
                continue
            reads += 1
            try:
                payload = self._quick.get_exchange_orderbook(eid, depth=IDEA_BOOK_DEPTH, tournament_id=tid)
            except SuperMarketError as exc:
                self._api_error(f"order book for exchange {eid}", exc)
                failures.append(_problem_text(exc, self._secrets))
                continue
            if isinstance(payload, Mapping):
                self._save_book(eid, payload, now)
        return reads, failures

    @staticmethod
    def _leaderboard(board: Mapping[str, Any], initial: Optional[float]) -> Dict[str, Any]:
        rows = [e for e in board.get("leaderboard") or [] if isinstance(e, Mapping)]
        top = []
        for entry in rows[:3]:
            top.append(
                {
                    "rank": entry.get("rank"),
                    "username": entry.get("username"),
                    "pnl": _num(entry.get("pnl")),
                    "roi": _num(entry.get("roi")),
                    "trades": entry.get("tradesCount"),
                    "value": leaderboard_value(entry, initial),
                }
            )
        # Up to 100 compact rows for the top-3 bar estimate (sizing.estimate_bar).
        entries = [
            {"rank": entry.get("rank"), "username": entry.get("username"), "pnl": _num(entry.get("pnl")),
             "value": leaderboard_value(entry, initial)}
            for entry in rows[:100]
        ]
        rank = board.get("myRank")
        return {
            "my_rank": rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            "top": top,
            "leader_value": top[0]["value"] if top else None,
            "total": board.get("total"),
            "period": board.get("period"),
            "value_assumption": "value = initialBalance + pnl (the leaderboard reports PnL only)",
            "entries": entries,
        }

    def _store_leaderboard(self, board: Mapping[str, Any], initial: Optional[float], now: float) -> None:
        snap = pipeline.leaderboard_snapshot({"leaderboard": board, "initial_balance": initial}, now)
        if snap is None:
            return
        try:
            self.store.add_leaderboard_snapshot(snap)
        except Exception as exc:
            if not _storage_error(exc):
                raise
            self._storage_failed("saving the leaderboard", exc)

    # ------------------------------------------------------------------ settlements

    def _settlements_due(self, now: float) -> bool:
        """Settled markets are read every ``settlement_refresh`` s, and early (at most every
        ``MISSING_REFRESH_MIN_S``) when outcomes went missing from the bulk prices since the last read."""
        if not self.context.slug:
            return False
        with self._lock:
            if now - self._settlements_at >= self.settlement_refresh:
                return True
            new_missing = bool(self._missing - self._settled_missing)
            return new_missing and now - self._settlements_at >= MISSING_REFRESH_MIN_S

    def _refresh_settlements(self, now: float) -> int:
        """``GET /tournaments/{slug}/markets?status=settled`` (cursor pages, at most 5), stored with
        ``detected_at = now`` (the first detection is kept). Returns the outcomes recorded."""
        slug = self.context.slug
        with self._lock:
            missing = set(self._missing)
        if not slug:
            return 0
        rows: List[SettlementInfo] = []
        cursor: Optional[str] = None
        failed = False
        for _ in range(SETTLEMENT_PAGES_MAX):
            page_cursor = cursor
            try:
                resp = self._quick.list_tournament_markets(slug, status="settled", limit=100, cursor=page_cursor)
            except SuperMarketError as exc:
                self._api_error("settled markets", exc, "settlements")
                failed = True
                break
            data = resp.get("data") if isinstance(resp, Mapping) else None
            for market in data or []:
                if isinstance(market, Mapping):
                    rows.extend(pipeline.settlements_from_market(market, self._clock()))
            pagination = resp.get("pagination") if isinstance(resp, Mapping) else None
            cursor = pagination.get("nextCursor") if isinstance(pagination, Mapping) else None
            if not cursor or not (isinstance(pagination, Mapping) and pagination.get("hasMore", True)):
                break
        with self._lock:
            self._settlements_at = now
            self._settled_missing = missing
        if failed:
            return 0
        self._resolve("settlements")
        if not rows:
            return 0
        try:
            return int(self.store.record_settlements(rows))
        except Exception as exc:
            if not _storage_error(exc):
                raise
            self._storage_failed("saving settlements", exc)
            return 0

    def settlements(self) -> Dict[str, SettlementInfo]:
        """Every settlement the tracker has read (from the store)."""
        getter = getattr(self.store, "settlements", None)
        return dict(getter()) if callable(getter) else {}

    def _overround_rows(self, now: Optional[float] = None) -> Tuple[Optional[List[Dict[str, Any]]], int, List[str]]:
        """Up to ``max_overround_markets`` multi-outcome books per refresh: the markets flagged
        for arbitrage last time first (their ideas need fresh numbers), then the rest in
        rotation. Each read takes 20 levels per outcome and keeps every outcome's book for
        idea sizing. Returns (rows, reads, failure messages); rows is None before the market
        list is known."""
        multi = [m for m in self._markets or [] if m.get("isMultiOutcome") and m.get("id") is not None]
        if not multi:
            return ([] if self._markets is not None else None), 0, []
        tid = self.context.tournament_id
        stamp = self._clock() if now is None else now
        with self._lock:
            previous = {r["market_id"]: r for r in self._context.get("overround") or []}
            flagged = [m for m in multi if (previous.get(str(m["id"])) or {}).get("arbitrage")][: self.max_overround_markets]
            rest = [m for m in multi if m not in flagged]
            take = max(0, self.max_overround_markets - len(flagged))
            # the rest take turns: never read first, then the longest ago (market order breaks ties)
            order = {str(m["id"]): i for i, m in enumerate(multi)}
            rest.sort(key=lambda m: (self._overround_read.get(str(m["id"]), float("-inf")), order[str(m["id"])]))
        batch = flagged + rest[:take]
        with self._lock:
            self._overround_round += 1
            for m in batch:
                self._overround_read[str(m["id"])] = self._overround_round
        fetched: Dict[str, Dict[str, Any]] = {}
        failures: List[str] = []
        for market in batch:
            mid = str(market["id"])
            try:
                resp = self._quick.get_market_orderbook(mid, tournament_id=tid, depth=IDEA_BOOK_DEPTH)
            except SuperMarketError as exc:
                self._api_error(f"orderbook for market {mid}", exc)
                failures.append(_problem_text(exc, self._secrets))
                continue
            if not isinstance(resp, Mapping):
                failures.append("unreadable response")
                continue
            books, ob = books_from_market_orderbook(resp, tid, market_id=mid)
            for book in books:
                if book.exchange_id and book.exchange_id != "None":
                    self._save_book(book.exchange_id, book, stamp)
            fetched[mid] = {
                "market_id": mid,
                "title": market.get("title"),
                "overround": _num(ob.get("overround")),
                "arbitrage": bool(ob.get("hasArbitrageOpportunity")),
                "outcomes": len(ob.get("exchanges") or []),
                "at": stamp,
            }
        rows = []
        for market in multi:
            mid = str(market["id"])
            row = fetched.get(mid) or previous.get(mid)
            if row is not None:
                rows.append(row)
        return rows, len(batch), failures

    # ------------------------------------------------------------------ view

    def _info(self, eid: str) -> Optional[ExchangeInfo]:
        return self._infos.get(eid) or self._all_infos.get(eid)

    def _surge_row(self, surge: Surge, queued: Optional[Set[int]] = None) -> Dict[str, Any]:
        row = surge.to_dict()
        info = self._info(surge.exchange_id)
        row["title"] = info.market_title if info else ""
        row["option"] = info.option if info else None
        # True while the surge waits in (or is in) the analysis queue: a surge with no attribution
        # and no pending analysis is "not analysed yet", not "analysing".
        row["analysis_pending"] = surge.id is not None and surge.id in (queued or set())
        return row

    def _build_view(self, now: float, results: Mapping[str, Mapping[str, Any]]) -> None:
        try:
            surges = self.store.surges(since=now - SURGE_VIEW_LOOKBACK_S, limit=SURGE_VIEW_LIMIT)
        except Exception as exc:
            self._record_error("surge list", exc, "storage")
            surges = []
        with self._lock:
            infos = dict(self._infos)
            rows = dict(self._rows)
            context = _copy_json(self._context)
            queued = set(self._queued) | ({self._analyzing} if self._analyzing is not None else set())
        open_by_exchange: Dict[str, int] = {}
        for surge in surges:  # newest first
            if surge.status == SURGE_OPEN and surge.id is not None:
                open_by_exchange.setdefault(surge.exchange_id, surge.id)

        exchanges: List[Dict[str, Any]] = []
        bands: List[Dict[str, Any]] = []
        stale_after = max(3 * self.interval, 60.0)
        for eid, info in infos.items():
            result = results.get(eid) or {}
            last_point: Optional[PricePoint] = result.get("last")
            snap = rows.get(eid)
            if snap is not None:
                last, bid, ask = _num(snap.get("latest_price")), _num(snap.get("best_bid")), _num(snap.get("best_ask"))
            elif last_point is not None:
                last, bid, ask = last_point.last, last_point.bid, last_point.ask
            else:
                last = bid = ask = None
            try:
                mark = analytics.mark_price(last, bid, ask)
            except Exception:
                mark = last_point.price if last_point is not None else None
            band = result.get("band")
            band_dict = band.to_dict() if band is not None else None
            changes = result.get("changes") or {}
            updated_at = last_point.ts if last_point is not None else None
            if snap is not None and self._last_snapshot_at is not None:
                updated_at = max(updated_at or 0.0, self._last_snapshot_at)
            row = {
                "exchange_id": eid,
                "market_id": info.market_id,
                "title": info.market_title,
                "option": info.option,
                "settlement_date": info.settlement_date,
                "mark": mark,
                "last": last,
                "bid": bid,
                "ask": ask,
                "spread": round(ask - bid, 6) if bid is not None and ask is not None else None,
                "change_5m": changes.get("change_5m"),
                "change_1h": changes.get("change_1h"),
                "change_24h": changes.get("change_24h"),
                "sparkline": result.get("sparkline") or [],
                "high_band": band_dict,
                "surge_id": open_by_exchange.get(eid),
                "updated_at": updated_at,
                # The shown price is not from a recent snapshot (e.g. stored by an earlier run
                # before a restart, or the bulk price read stopped returning this outcome).
                "stale": updated_at is None or now - updated_at > stale_after,
            }
            exchanges.append(row)
            if band_dict is not None:
                bands.append({**band_dict, "title": info.market_title, "option": info.option})

        view = {
            "exchanges": exchanges,
            "surges": [self._surge_row(s, queued) for s in surges],
            "high_band": bands,
            "context": context,
            "status": {},
        }
        with self._lock:
            self._view = view
        self._publish(now, exchanges)

    def view(self) -> Dict[str, Any]:
        """Thread-safe snapshot for the dashboard (a copy; safe to mutate or ``json.dumps``).

        Keys: ``exchanges`` (one row per open outcome), ``surges`` (recent, newest first, with
        attribution), ``high_band``, ``context`` and ``status``. Built at the end of each
        cycle; before the first cycle the lists are empty.
        """
        with self._lock:
            if self._view is not None:
                view = _copy_json(self._view)
                view["context"] = _copy_json(self._context)  # the context worker updates it between cycles
            else:
                view = {"exchanges": [], "surges": [], "high_band": [], "context": _copy_json(self._context), "status": {}}
        view["status"] = self.status()
        return view

    def status(self) -> Dict[str, Any]:
        with self._lock:
            pending = sum(len(q) for q in self._bf_queues.values())
            status: Dict[str, Any] = {
                "running": self.running,
                "started_at": self._started_at,
                "last_snapshot_at": self._last_snapshot_at,
                "last_cycle_at": self._last_cycle_at,
                "last_cycle_seconds": self._last_cycle_s,
                "cycles": self._cycles,
                "interval": self.interval,
                "markets": len(self._markets or []),
                "exchanges": len(self._infos),
                "ticks_recorded": self._ticks_recorded,
                "errors": self._errors,
                "last_error": dict(self._recent_errors[-1]) if self._recent_errors else None,
                "recent_errors": [dict(e) for e in self._recent_errors],
                "fatal_error": self._fatal_error,
                "backfill": {
                    "enabled": self.backfill_enabled,
                    "total": len(self._bf_known),
                    "done": len(self._bf_done),
                    "failed": len(self._bf_failed),
                    "pending": pending,
                },
                "queues": {"analysis": len(self._queue), "backfill": pending},
                "analysis": {
                    "enabled": self.analyze_enabled and self.attributor is not None,
                    "attributor": self.attributor is not None,
                    "analyzed": self._analyses,
                    "queued_ids": list(self._queue),
                    "analyzing": self._analyzing,
                },
                "markets_updated_at": self._markets_at if self._markets is not None else None,
                "context_updated_at": self._context.get("updated_at"),
                # Current non-fatal failures, errors first, then oldest first. Each entry is
                # {"source", "message", "since", "last", "count", "severity"} and goes away
                # when that source works again.
                "problems": sorted(
                    (dict(p) for p in self._problems.values()),
                    key=lambda p: (p["severity"] != "error", p["since"], p["source"]),
                ),
                # Whether surge detection runs: enabled (for at least one outcome), outcomes
                # waiting for price history, outcomes detected from live prices only, and why.
                "detection": dict(self._detection),
            }
            projected = dict(self._projected)
        client_limiter = getattr(self.client, "read_limiter", None)
        sent = getattr(self.client, "requests_sent", None)
        if isinstance(sent, int) and self._quick is not self.client:
            sent += getattr(self._quick, "requests_sent", 0) or 0
        status["read_budget"] = {
            "client": {"used": client_limiter.used, "limit": client_limiter.limit} if client_limiter is not None else None,
            "backfill": {"used": self.backfill_limiter.used, "limit": self.backfill_limiter.limit},
            "analysis": {"used": self.analyze_limiter.used, "limit": self.analyze_limiter.limit},
            "requests_sent": sent,
            "paper": self.paper_limiter.status() if self.paper_runner is not None else None,
            "moves": self.moves_budget.status() if self.moves_budget is not None else None,
            "reserve": self.read_reserve.reserve,
            "projected_per_min": projected.get("per_min"),
            "warning": projected.get("warning"),
        }
        # Outside self._lock (lock rule, D43): the runner's published summary and the fair-value service.
        status["paper"] = self._paper_status()
        status["fair_value"] = self._fair_value_status()
        status["moves"] = self._moves_status()
        return status

    def _moves_status(self) -> Optional[Dict[str, Any]]:
        """The watcher's published compact status (lock-free; §12.4), or None without a watcher."""
        if self.moves is None:
            return None
        try:
            raw = self.moves.status()
        except Exception as exc:
            log.debug("outside-move status failed: %s", exc)
            return None
        return _copy_json(dict(raw)) if isinstance(raw, Mapping) else None

    def _paper_status(self) -> Optional[Dict[str, Any]]:
        if self.paper_runner is None:
            return None
        try:
            summary = self.paper_runner.summary() or {}
        except Exception as exc:
            log.debug("paper summary failed: %s", exc)
            summary = {}
        run = summary.get("run") if isinstance(summary, Mapping) else None
        run = run if isinstance(run, Mapping) else {}
        return {"enabled": True, "run_id": run.get("run_id"), "steps": run.get("steps"),
                "last_step_at": run.get("last_step_at"), "last_step_seconds": run.get("last_step_seconds")}

    def _fair_value_status(self) -> Optional[Dict[str, Any]]:
        if self.fair_values is None:
            return None
        try:
            return _copy_json(dict(self.fair_values.status()))
        except Exception as exc:
            log.debug("fair value status failed: %s", exc)
            return None

    # ------------------------------------------------------------------ paper / fair value (synchronous)

    def fair_value_step(self, now: Optional[float] = None) -> bool:
        """One fair-value refresh (the worker calls this every FV_REFRESH_S). False without a service."""
        if self.fair_values is None:
            return False
        when = self._clock() if now is None else float(now)
        self.fair_values.refresh(when)
        self._update_fair_value_problem()
        return True

    def _update_fair_value_problem(self) -> None:
        try:
            st = self.fair_values.status() if self.fair_values is not None else {}
        except Exception:
            return
        messages: List[str] = []
        for name, info in sorted((st.get("providers") or {}).items()):
            if not isinstance(info, Mapping):
                continue
            if info.get("status") in ("offline", "error", "backoff") and info.get("last_error"):
                text = str(info["last_error"])
                if text not in messages:
                    messages.append(text)
        if messages:
            self._problem("fair value", " ".join(messages))
        else:
            self._resolve("fair value")

    def paper_step(self, now: Optional[float] = None) -> Any:
        """One paper step (the worker calls this after each cycle). None without a paper trader."""
        if self.paper_runner is None:
            return None
        self._ensure_lease()
        with self._lock:
            self._paper_read_failures = 0
        report = self.paper_runner.step(now)
        errors = list(getattr(report, "errors", None) or [])
        with self._lock:
            failures = self._paper_read_failures
        if errors:
            self._problem("paper", f"The paper step reported a problem: {errors[0]}")
        elif not failures:
            self._resolve("paper")
        return report

    def moves_step(self, now: Optional[float] = None) -> Any:
        """One outside-move watcher step (the ``tracker-moves`` worker calls this every ``moves.poll_s``; the fast demo
        calls it from ``pipeline.run_simulation``). None without a watcher. Never called under ``self._lock``;
        reports failures as the MOVES_PROBLEM problem (docs/OUTSIDE_MOVES.md §13.3)."""
        if self.moves is None:
            return None
        with self._lock:
            self._moves_book_failures = 0
        # Lock rule (§12.3, D43): the watcher calls back into this tracker (TrackerCupFeed) and the store, so it is
        # never called while self._lock is held.
        report = self.moves.step(now)
        try:
            last_error = (self.moves.status() or {}).get("last_error")
        except Exception:
            last_error = None
        with self._lock:
            failures = self._moves_book_failures
        if last_error:
            self._problem(MOVES_PROBLEM, str(last_error))
        elif not failures:
            self._resolve(MOVES_PROBLEM)  # (a failed Cup book read of this step keeps the problem it reported)
        return report

    def moves_view(self) -> Optional[Dict[str, Any]]:
        """The watcher's published summary (lock-free), or None without a watcher (§13.3)."""
        if self.moves is None:
            return None
        summary = self.moves.summary()
        return dict(summary) if isinstance(summary, Mapping) else None

    def paper_view(self, run: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The runner's published ``/api/paper`` body (a shallow copy, ``run.interval`` set), or for
        ``run="previous"`` the newest ended run's final snapshot. None without a paper trader."""
        if self.paper_runner is None:
            return None
        if run == "previous":
            prev = self.paper_runner.previous()
            return dict(prev) if isinstance(prev, Mapping) else None
        summary = self.paper_runner.summary()
        if not isinstance(summary, Mapping):
            return None
        out = dict(summary)
        run_info = out.get("run")
        if isinstance(run_info, Mapping):
            run_info = dict(run_info)
            run_info["interval"] = self.interval
            out["run"] = run_info
        return out

    def paper_reset(self, start_capital: Optional[float] = None, target_hours: Optional[float] = None) -> Optional[str]:
        """End the current run (reason "reset", final snapshot kept) and start a new one. Returns its id."""
        if self.paper_runner is None:
            return None
        self._ensure_lease()  # a second process must not end this store's run (D46)
        if start_capital is None and self.running and not self._context_ready.is_set():
            # `paper --reset` right after start(): the new run takes its capital from the account value, which the
            # first context refresh is still reading (accounting-3); wait for it (bounded) like the first step
            self._context_ready.wait(max(1.0, min(self.context_refresh, CONTEXT_WAIT_MAX_S)))
        source = "set by you" if start_capital is not None else ""
        return self.paper_runner.reset(self._clock(), start_capital=start_capital, target_hours=target_hours,
                                       capital_source=source)

    def paper_end(self, reason: str = "completed") -> Optional[Dict[str, Any]]:
        """End the current run with ``reason`` (``paper --hours N`` when done) and keep its final snapshot."""
        if self.paper_runner is None:
            return None
        return self.paper_runner.end_run(self._clock(), reason)

    # ------------------------------------------------------------------ threads

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and any(t.is_alive() for t in self._threads)

    def start(self) -> None:
        """Run the loop, the backfill worker and the attribution worker in daemon threads."""
        with self._lock:
            if any(t.is_alive() for t in self._threads):
                return
            self._stop.clear()
            self._backfill_wake.clear()
            self._analysis_wake.clear()
            if not self._cycles:
                self._first_cycle.clear()
            self._fatal_error = None
            self._ensure_lease()  # raises TrackerBusy when another live process tracks this store
            if getattr(self.client, "cancelled", False):
                self.client.reset_cancel()  # a previous stop() cancelled the client
            self._started_at = self._clock()
            self._paper_wake.clear()
            threads = [
                threading.Thread(target=self._loop, name="tracker-loop", daemon=True),
                threading.Thread(target=self._context_worker, name="tracker-context", daemon=True),
            ]
            if self.backfill_enabled:
                threads.append(threading.Thread(target=self._backfill_worker, name="tracker-backfill", daemon=True))
            if self.attributor is not None:
                threads.append(threading.Thread(target=self._analysis_worker, name="tracker-analysis", daemon=True))
            if self.fair_values is not None:
                threads.append(threading.Thread(target=self._fair_value_worker, name="tracker-fairvalue", daemon=True))
            if self.paper_runner is not None:
                threads.append(threading.Thread(target=self._paper_worker, name="tracker-paper", daemon=True))
            if self.moves is not None:
                threads.append(threading.Thread(target=self._moves_worker, name="tracker-moves", daemon=True))
            if self.lease_enabled and self._lease_held:
                threads.append(threading.Thread(target=self._lease_worker, name="tracker-lease", daemon=True))
            self._threads = threads
        for thread in threads:
            thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal every worker, cancel in-flight client waits and join the threads."""
        with self._lock:
            threads = list(self._threads)
        self._signal_stop()
        if threads:
            cancel = getattr(self.client, "cancel", None)
            if callable(cancel):
                cancel()
        deadline = time.monotonic() + max(0.0, timeout)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(max(0.0, deadline - time.monotonic()))
        alive = [t.name for t in threads if t.is_alive() and t is not current]
        if alive:
            log.warning("tracker threads still running after %.1fs: %s", timeout, ", ".join(alive))
        self._release_lease()

    def _loop(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._run_once(refresh_context=False)  # the context worker refreshes the context
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break  # fatal: already recorded in status
            except Exception as exc:  # a bug in one cycle should not end tracking
                self._cycle_failed("cycle", exc, label="tracker cycle")
            try:
                self._renew_lease()
            except Exception as exc:
                self._cycle_failed("lease renewal", exc, label="tracker lease renewal")
            next_at += self.interval
            now = time.monotonic()
            if next_at <= now:  # fell behind: skip the missed slots instead of bursting
                next_at += (int((now - next_at) // self.interval) + 1) * self.interval
            if self._stop.wait(next_at - now):
                break

    def _lease_worker(self) -> None:
        """Renew the store lease every interval / 3 for as long as the tracker runs (live-1, D46). The snapshot
        loop also renews after each cycle, but a cycle can sit in the client's retries (Retry-After waits) for
        longer than the 3 x interval TTL; without this a second dashboard would take the lease meanwhile and this
        tracker would stop with "Another process is already running"."""
        while not self._stop.wait(self._lease_heartbeat_s):
            try:
                self._renew_lease()
            except _Stopping:
                break
            except Exception as exc:
                self._cycle_failed("lease renewal", exc, label="tracker lease renewal")

    def _cycle_failed(self, where: str, exc: BaseException, source: str = "tracker", label: Optional[str] = None) -> None:
        """An unexpected exception ended a cycle or a worker step: count it and show it.

        A database or disk failure is the "storage" problem; anything else is reported under
        ``source`` ("tracker" for the snapshot loop, "<name> worker" for a worker), so the banner
        explains why data stopped updating. The loop and the workers retry on their own; the
        next step that completes clears the problem.
        """
        if _storage_error(exc):
            log.debug("%s failed on storage", where, exc_info=True)
            self._storage_failed(where, exc)
            return
        log.exception("%s failed", label or where)
        self._record_error(where, repr(exc))
        self._problem(source, f"The {label or where} failed ({type(exc).__name__}: {exc}); it is retried automatically",
                      severity="error")

    def _worker(self, step: Callable[[int], int], wake: threading.Event, name: str) -> None:
        source = f"{name} worker"
        while not self._stop.is_set():
            try:
                worked = step(1)
                self._resolve(source)
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break
                worked = 0
            except Exception as exc:
                self._cycle_failed(f"{name} worker", exc, source)
                worked = 0
            if not worked and wake.wait(WORKER_IDLE_S):
                wake.clear()

    def _backfill_worker(self) -> None:
        self._worker(self.backfill_step, self._backfill_wake, "backfill")

    def _context_worker(self) -> None:
        """Refresh the tournament context off the snapshot path (after the first cycle, so the
        market list for the overround books is known)."""
        while not self._stop.is_set() and not self._first_cycle.wait(min(WORKER_IDLE_S, self.interval)):
            pass
        while not self._stop.is_set():
            try:
                now = self._clock()
                if self._context_due(now):
                    self._refresh_context(now)
                    self._resolve("context worker")
                if self._settlements_due(now):
                    self._refresh_settlements(now)
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break  # fatal: already recorded in status
            except Exception as exc:
                self._cycle_failed("context refresh", exc, "context worker")
                with self._lock:
                    self._context_at = self._clock()  # do not spin on a bug: try again next period
            if self._stop.wait(min(WORKER_IDLE_S, max(0.05, self.context_refresh))):
                break

    def _analysis_worker(self) -> None:
        self._worker(self.analyze_pending, self._analysis_wake, "analysis")

    def _fair_value_worker(self) -> None:
        """Refresh outside fair values every FV_REFRESH_S (its own HTTP budget: never the snapshot loop's)."""
        from .fairvalue import FV_REFRESH_S

        while not self._stop.is_set():
            try:
                self.fair_value_step()
                self._resolve("fair value worker")
            except _Stopping:
                break
            except Exception as exc:
                self._cycle_failed("fair value refresh", exc, "fair value worker")
            if self._stop.wait(FV_REFRESH_S):
                break

    def _moves_worker(self) -> None:
        """One outside-move watcher step every ``moves.poll_s`` (docs/OUTSIDE_MOVES.md §13.1), after the first cycle
        (so the Cup series the watcher loads once at its first step is there). Its outside polls use the providers'
        own per-host budgets, never the Super Market client; missed slots are skipped, not burst."""
        while not self._stop.is_set() and not self._first_cycle.wait(min(WORKER_IDLE_S, self.interval)):
            pass
        try:
            period = max(0.05, float(getattr(self.moves, "poll_s", 15.0) or 15.0))
        except (TypeError, ValueError):
            period = 15.0
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self.moves_step()
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break  # fatal: already recorded in status
            except Exception as exc:  # a bug in one step should not end the alerts
                self._cycle_failed("outside-move step", exc, MOVES_PROBLEM, label="outside-move step")
            next_at += period
            now = time.monotonic()
            if next_at <= now:  # fell behind (a slow outside host): skip the missed slots
                next_at += (int((now - next_at) // period) + 1) * period
            if self._stop.wait(next_at - now):
                break

    def _paper_worker(self) -> None:
        """One paper step per tracker cycle (woken at the end of each cycle). The first step waits (at most
        min(context_refresh, CONTEXT_WAIT_MAX_S)) for the first context refresh, which the context worker starts
        after the first cycle too: a run must start on the account value (§6.1: set by you > account value > cash
        > initial balance > default), not on the default 100,000 because the balance read was still in flight
        (accounting-3)."""
        first = True
        while not self._stop.is_set():
            if not self._paper_wake.wait(max(WORKER_IDLE_S, self.interval)):
                continue
            if first:
                first = False
                if not self._context_ready.is_set():
                    self._context_ready.wait(max(1.0, min(self.context_refresh, CONTEXT_WAIT_MAX_S)))
            self._paper_wake.clear()
            if self._stop.is_set():
                break
            try:
                self.paper_step()
                self._resolve("paper worker")
            except _Stopping:
                break
            except TrackerBusy as exc:
                self._set_fatal("paper step", exc)
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break
            except Exception as exc:
                self._cycle_failed("paper step", exc, "paper")


class MovesBudget:
    """The outside-move watcher's Cup order-book budget (docs/OUTSIDE_MOVES.md §13.4): its own sliding window
    (``Tracker.moves_reads_per_min``) AND room above the read reserve, like :class:`PaperBudget` without the backfill
    rule. Readers call :meth:`acquire` only when ``room() > 0``, so it never waits."""

    def __init__(self, own: Optional[SlidingWindowLimiter], reserve: ReadReserve) -> None:
        self.own = own  # None when moves_reads_per_min is 0 (no book reads at all)
        self.reserve = reserve

    @property
    def limit(self) -> int:
        return int(self.own.limit) if self.own is not None else 0

    @property
    def used(self) -> int:
        return int(self.own.used) if self.own is not None else 0

    def room(self) -> int:
        if self.own is None:
            return 0
        return min(int(self.own.limit) - int(self.own.used), int(self.reserve.room()))

    def acquire(self) -> float:
        if self.own is None:
            return 0.0
        return self.own.acquire()

    def status(self) -> Dict[str, int]:
        """``{"used", "limit", "room"}`` (limit 0 when disabled)."""
        return {"used": self.used, "limit": self.limit, "room": max(0, int(self.room()))}


class TrackerCupFeed:
    """The watcher's only view of the Cup (``moves.CupFeed``, docs/OUTSIDE_MOVES.md §13.2): the published snapshot
    quotes, the series cache, stored books and, inside :class:`MovesBudget`, fresh order-book reads through the
    tracker's single-attempt client (GET only). Book reads are kept by the watcher and NOT stored in the tracker's
    book tables, so the paper trader's inputs never depend on the alerts. Never raises: a SuperMarketError is
    reported through ``tracker._api_error(..., MOVES_PROBLEM)`` and returns None (a fatal key error still stops the
    tracker as everywhere else)."""

    def __init__(self, tracker: "Tracker") -> None:
        self.tracker = tracker

    def quotes(self) -> Dict[str, Any]:
        return self.tracker.cup_quotes()

    def history(self, seconds: float) -> Dict[str, List[Any]]:
        return self.tracker.cup_history(seconds)

    def _failed(self) -> None:
        with self.tracker._lock:
            self.tracker._moves_book_failures += 1

    def book(self, exchange_id: str) -> Optional[BookObservation]:
        from .moves import MOVES_BOOK_DEPTH

        t = self.tracker
        eid = str(exchange_id)
        budget = t.moves_budget
        if budget is None or budget.room() <= 0:
            return None  # no room (or reads disabled): shares stay unknown, never a wait
        where = f"outside-move order book for exchange {eid}"
        try:
            budget.acquire()
            payload = t._quick.get_exchange_orderbook(eid, depth=MOVES_BOOK_DEPTH, tournament_id=t.context.tournament_id)
        except SuperMarketError as exc:
            self._failed()
            try:
                t._api_error(where, exc, MOVES_PROBLEM)
            except SuperMarketError:
                pass  # a fatal key error: the tracker has stopped and recorded it
            return None
        observed = t._clock()  # stamped when the response arrived (lookahead-2)
        try:
            parsed = Book.from_payload(payload) if isinstance(payload, Mapping) else None
        except Exception as exc:
            parsed = None
            log.debug("outside-move book parse failed: %s", exc)
        if parsed is None:
            self._failed()
            t._record_error(where, "the order book could not be read", MOVES_PROBLEM)
            return None
        sequence = parsed.as_of.sequence if parsed.as_of is not None else None
        return BookObservation(
            exchange_id=eid, observed_at=float(observed),
            bids=[(float(lv.price), float(lv.quantity)) for lv in parsed.bids],
            asks=[(float(lv.price), float(lv.quantity)) for lv in parsed.asks],
            source="moves", sequence=sequence if isinstance(sequence, int) else None,
        )

    def stored_book(self, exchange_id: str) -> Optional[BookObservation]:
        eid = str(exchange_id)
        try:
            raw = self.tracker.store.book(eid)
        except Exception as exc:  # a storage problem: no stored book, the watcher may read one
            log.debug("stored book for %s unavailable: %s", eid, exc)
            return None
        if not isinstance(raw, Mapping) or _num(raw.get("at")) is None:
            return None
        try:
            bids = [(float(p), float(q)) for p, q in raw.get("bids") or []]
            asks = [(float(p), float(q)) for p, q in raw.get("asks") or []]
        except (TypeError, ValueError):
            return None
        return BookObservation(exchange_id=eid, observed_at=float(raw["at"]), bids=bids, asks=asks, source="tracker")


class TrackerMarketReader:
    """The paper runner's only way to read the market (``paper.MarketReader``): GET reads through the
    tracker's single-attempt client inside its paper budget, stored for replays (books in ``book_snapshots``
    and as the idea book, prints with ``fetched_at``). Returns None on any failure (reported as the
    "paper" problem)."""

    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker

    def _failed(self) -> None:
        with self.tracker._lock:
            self.tracker._paper_read_failures += 1

    def book(self, exchange_id: str, depth: int) -> Optional[BookObservation]:
        t = self.tracker
        eid = str(exchange_id)
        try:
            t.paper_limiter.acquire()
            payload = t._quick.get_exchange_orderbook(eid, depth=int(depth), tournament_id=t.context.tournament_id)
        except SuperMarketError as exc:
            self._failed()
            t._api_error(f"paper order book for exchange {eid}", exc, "paper")
            return None
        if not isinstance(payload, Mapping):
            self._failed()
            return None
        try:
            parsed = Book.from_payload(payload)
        except Exception as exc:
            self._failed()
            t._record_error(f"paper order book for exchange {eid}", repr(exc), "paper")
            return None
        observed = t._clock()
        bids = [(float(lv.price), float(lv.quantity)) for lv in parsed.bids]
        asks = [(float(lv.price), float(lv.quantity)) for lv in parsed.asks]
        sequence = parsed.as_of.sequence if parsed.as_of is not None else None
        obs = BookObservation(exchange_id=eid, observed_at=observed, bids=bids, asks=asks, source="paper",
                              sequence=sequence if isinstance(sequence, int) else None)
        try:
            t.store.put_book(eid, observed, bids, asks)  # the strategy sizes ideas by it
            t.store.add_book_snapshots([obs])  # replays and audits
        except Exception as exc:
            if not _storage_error(exc):
                raise
            t._storage_failed("saving a paper order book", exc)
        return obs

    def trades(self, exchange_id: str, since: float, max_pages: int = 3) -> Any:
        """Prints after ``since`` (oldest first), following the cursor while a page is full (200) and pages
        remain in the budget; ``truncated`` when the last page read was still full."""
        from .paper import TapeRead

        t = self.tracker
        eid = str(exchange_id)
        start = iso_ts(float(since))
        cursor: Optional[str] = None
        pages = 0
        truncated = False
        seen: Set[str] = set()
        records: List[TradeRecord] = []
        while pages < max(1, int(max_pages)):
            if pages and t.paper_limiter.room() <= 0:
                truncated = True  # more prints exist but the budget is spent: say so (D40)
                break
            try:
                t.paper_limiter.acquire()
                resp = t._quick.get_trades(eid, tournament_id=t.context.tournament_id, start=start, limit=TAPE_PAGE_LIMIT,
                                           cursor=cursor)
            except SuperMarketError as exc:
                self._failed()
                t._api_error(f"paper trade tape for exchange {eid}", exc, "paper")
                return None
            pages += 1
            data = [d for d in (resp.get("data") if isinstance(resp, Mapping) else None) or [] if isinstance(d, Mapping)]
            fetched = t._clock()
            try:
                t.store.add_trades(eid, data, fetched_at=fetched)
            except Exception as exc:
                if not _storage_error(exc):
                    raise
                t._storage_failed("saving paper trades", exc)
            for raw in data:
                tid = raw.get("id")
                when = parse_time(raw.get("createdAt"))
                if tid is None or when is None or str(tid) in seen:
                    continue
                seen.add(str(tid))
                side = raw.get("side")
                records.append(TradeRecord(trade_id=str(tid), exchange_id=eid, ts=when.timestamp(), price=_num(raw.get("price")),
                                           size=_num(raw.get("size")) or 0.0, side=side.upper() if isinstance(side, str) else None,
                                           fetched_at=fetched))
            pagination = resp.get("pagination") if isinstance(resp, Mapping) else None
            nxt = pagination.get("nextCursor") if isinstance(pagination, Mapping) else None
            full = len(data) >= TAPE_PAGE_LIMIT
            truncated = bool(full and nxt)
            if not truncated:
                break
            cursor = str(nxt)
        records.sort(key=lambda r: (r.ts, len(r.trade_id), r.trade_id))
        return TapeRead(trades=records, truncated=truncated, pages=pages)
