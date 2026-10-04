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
"""

from __future__ import annotations

import copy
import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import analytics
from .books import books_from_market_orderbook
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
    ExchangeInfo,
    PricePoint,
    Surge,
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


class _WorkerReadLimiter:
    """The client's read limiter as seen by a worker's single-attempt reads.

    Every request still takes a slot from the shared per-account budget. A server-requested
    wait is *not* applied to every caller here (the client would pause snapshots too for an
    optional read's ``503 Retry-After``); the tracker forwards 429 waits to the shared limiter
    itself and keeps 503 waits local to the worker that hit them.
    """

    def __init__(self, shared: Any) -> None:
        self.shared = shared

    def acquire(self) -> float:
        return self.shared.acquire()

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
    ) -> None:
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
        self._quick = single_attempt_client(client)  # backfill and context: one attempt per read

        self._stop = threading.Event()
        self._backfill_wake = threading.Event()
        self._analysis_wake = threading.Event()
        self._first_cycle = threading.Event()  # the context worker starts after the first cycle
        self.backfill_limiter = SlidingWindowLimiter(max(1, int(backfill_reads_per_min)), sleep=self._wait_or_stop)
        self.analyze_limiter = SlidingWindowLimiter(max(1, int(analyze_reads_per_min)), sleep=self._wait_or_stop)
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
        self._first_cycle.set()  # ends the context worker's wait for the first cycle

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

            self._build_view(now, results)
            with self._lock:
                self._cycles += 1
                self._last_cycle_at = now
                self._last_cycle_s = round(time.monotonic() - started, 4)
                summary["errors"] = self._errors - errors_before
            self._resolve("tracker")  # a whole cycle ran: an earlier crash is over
            self._first_cycle.set()
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
            if attribution.analyzed_at is None:
                attribution.analyzed_at = self._clock()
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
                board = self._call("leaderboard", lambda: quick.get_tournament_leaderboard(slug, limit=3), "leaderboard")
                if isinstance(board, Mapping):
                    updates["leaderboard"] = self._leaderboard(board, initial)

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
        top = []
        for entry in [e for e in board.get("leaderboard") or [] if isinstance(e, Mapping)][:3]:
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
        rank = board.get("myRank")
        return {
            "my_rank": rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            "top": top,
            "leader_value": top[0]["value"] if top else None,
            "total": board.get("total"),
            "period": board.get("period"),
            "value_assumption": "value = initialBalance + pnl (the leaderboard reports PnL only)",
        }

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
        client_limiter = getattr(self.client, "read_limiter", None)
        sent = getattr(self.client, "requests_sent", None)
        if isinstance(sent, int) and self._quick is not self.client:
            sent += getattr(self._quick, "requests_sent", 0) or 0
        status["read_budget"] = {
            "client": {"used": client_limiter.used, "limit": client_limiter.limit} if client_limiter is not None else None,
            "backfill": {"used": self.backfill_limiter.used, "limit": self.backfill_limiter.limit},
            "analysis": {"used": self.analyze_limiter.used, "limit": self.analyze_limiter.limit},
            "requests_sent": sent,
        }
        return status

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
            if getattr(self.client, "cancelled", False):
                self.client.reset_cancel()  # a previous stop() cancelled the client
            self._started_at = self._clock()
            threads = [
                threading.Thread(target=self._loop, name="tracker-loop", daemon=True),
                threading.Thread(target=self._context_worker, name="tracker-context", daemon=True),
            ]
            if self.backfill_enabled:
                threads.append(threading.Thread(target=self._backfill_worker, name="tracker-backfill", daemon=True))
            if self.attributor is not None:
                threads.append(threading.Thread(target=self._analysis_worker, name="tracker-analysis", daemon=True))
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
            next_at += self.interval
            now = time.monotonic()
            if next_at <= now:  # fell behind: skip the missed slots instead of bursting
                next_at += (int((now - next_at) // self.interval) + 1) * self.interval
            if self._stop.wait(next_at - now):
                break

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
