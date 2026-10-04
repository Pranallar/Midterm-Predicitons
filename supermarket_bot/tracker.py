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

:meth:`Tracker.start` runs that loop in a daemon thread next to a candle-backfill worker and
an attribution worker; each worker has its own read budget on top of the client's limiter.
Read-only: nothing here places orders.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Set, Tuple

from . import analytics
from .books import books_from_market_orderbook
from .bot import Context, MarketDataBot, is_fatal
from .client import SuperMarketClient
from .errors import ApiError, NetworkError, RequestCancelled, SuperMarketError
from .models import SURGE_OPEN, ExchangeInfo, PricePoint, Surge
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
        self._bot = MarketDataBot(client, context)

        self._stop = threading.Event()
        self._backfill_wake = threading.Event()
        self._analysis_wake = threading.Event()
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
        self._series: Dict[str, List[PricePoint]] = {}
        self._series_loaded: Dict[str, float] = {}
        self._dirty: Set[str] = set()  # exchanges whose stored history changed under the cache

        # backfill
        self._bf_queues: Dict[str, Deque[str]] = {res: deque() for res, _ in BACKFILL_PLAN}
        self._bf_known: Set[Tuple[str, str]] = set()
        self._bf_done: Set[Tuple[str, str]] = set()
        self._bf_failed: Set[Tuple[str, str]] = set()
        self._bf_attempts: Dict[Tuple[str, str], int] = {}

        # attribution queue
        self._queue: Deque[int] = deque()
        self._queued: Set[int] = set()
        self._analyzed_change: Dict[int, float] = self._load_analyzed()
        self._analyses = 0

        # context
        self._context: Dict[str, Any] = {
            "tournament_id": context.tournament_id,
            "slug": context.slug,
            "name": context.name,
            "currency": context.currency,
            "tournament": None,
            "balance": None,
            "initial_balance": None,
            "leaderboard": None,
            "constraints": None,
            "overround": [],
            "updated_at": None,
        }
        self._context_at = float("-inf")
        self._overround_offset = 0

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

    # ------------------------------------------------------------------ plumbing

    def _wait_or_stop(self, seconds: float) -> None:
        """Limiter sleep that ends at once (raising :class:`_Stopping`) when stop() is called."""
        if self._stop.wait(max(0.0, seconds)):
            raise _Stopping()

    def _record_error(self, where: str, exc: Any) -> None:
        message = str(exc)
        with self._lock:
            self._errors += 1
            self._recent_errors.append({"at": self._clock(), "where": where, "error": message})
        log.warning("tracker: %s failed: %s", where, message)

    def _set_fatal(self, where: str, exc: BaseException) -> None:
        with self._lock:
            self._fatal_error = str(exc)
            self._recent_errors.append({"at": self._clock(), "where": where, "error": str(exc)})
        log.error("tracker stopped: %s failed: %s", where, exc)
        self._signal_stop()

    def _signal_stop(self) -> None:
        self._stop.set()
        self._backfill_wake.set()
        self._analysis_wake.set()

    def _api_error(self, where: str, exc: SuperMarketError) -> None:
        """Count a transient error; re-raise fatal ones (after stopping) and shutdown cancels."""
        if isinstance(exc, RequestCancelled) and self._stop.is_set():
            raise _Stopping() from exc
        if is_fatal(exc):
            self._set_fatal(where, exc)
            raise exc
        self._record_error(where, exc)

    def _call(self, where: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except SuperMarketError as exc:
            self._api_error(where, exc)
            return None

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

        Fatal API errors (``bot.is_fatal``) set ``status()["fatal_error"]``, stop the
        tracker and are re-raised.
        """
        try:
            return self._run_once()
        except _Stopping:
            return {"at": self._clock(), "stopped": True}

    def _run_once(self) -> Dict[str, Any]:
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

            # 1. market list
            if self._markets is None or now - self._markets_at >= self.market_refresh:
                markets = self._call("market list", lambda: self._bot.list_markets(status="open"))
                if markets is not None:
                    self.store.upsert_markets(markets)
                    self._set_markets(markets, now)
                    summary["markets_refreshed"] = True

            # 2. snapshot
            if self._markets:
                markets_now = self._markets
                snap = self._call("price snapshot", lambda: self._bot.snapshot(markets_now))
                if snap is not None:
                    now = self._clock()
                    added = self.store.add_ticks(now, snap.rows)
                    with self._lock:
                        self._rows = snap.by_exchange()
                        self._last_snapshot_at = now
                        self._ticks_recorded += added
                    summary["ticks"] = added

            # 3 + 4. analytics, surges, attribution queue
            results = self._analyze_all(now, summary)

            # 5. context
            if now - self._context_at >= self.context_refresh:
                self._refresh_context(now)
                summary["context_refreshed"] = True

            self._build_view(now, results)
            with self._lock:
                self._cycles += 1
                self._last_cycle_at = now
                self._last_cycle_s = round(time.monotonic() - started, 4)
                summary["errors"] = self._errors - errors_before
            summary.update(at=now, markets=len(self._markets or []), exchanges=len(self._infos))
            summary["high_band"] = sum(1 for r in results.values() if r.get("band") is not None)
            return summary

    def _set_markets(self, markets: List[Dict[str, Any]], now: float) -> None:
        all_infos = {info.exchange_id: info for info in self.store.exchanges()}
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
        results: Dict[str, Dict[str, Any]] = {}
        failed: List[str] = []
        first_error: Optional[BaseException] = None
        for eid, info in infos.items():
            result: Dict[str, Any] = {"last": None, "band": None, "changes": {}, "sparkline": []}
            results[eid] = result
            try:
                points = self._points(eid, now)
                result["last"] = points[-1] if points else None
                self._analyze_exchange(eid, info, points, now, result, summary)
            except Exception as exc:  # one broken series must not stop the others
                failed.append(eid)
                first_error = first_error or exc
        if failed:
            log.debug("analytics failure detail", exc_info=first_error)
            self._record_error("analytics", f"{len(failed)} outcome(s), e.g. {failed[0]}: {first_error!r}")
        self._update_open_surges(now, results)
        return results

    def _analyze_exchange(
        self, eid: str, info: ExchangeInfo, points: List[PricePoint], now: float,
        result: Dict[str, Any], summary: Dict[str, Any],
    ) -> None:
        recent = [p for p in points if p.ts >= now - SPARKLINE_WINDOW_S and p.ts <= now and p.price is not None]
        result["sparkline"] = [[p.ts, p.price] for p in analytics.downsample(recent, SPARKLINE_POINTS)]
        result["changes"] = {name: analytics.change_over(points, now, secs) for name, secs in CHANGE_WINDOWS}
        result["band"] = analytics.high_band(points, now, eid, info.market_id, settlement_date=info.settlement_date)
        for surge in analytics.detect_surges(points, now, eid, info.market_id):
            recorded = self._record_surge(surge)
            if recorded is None:
                continue
            stored, is_new = recorded
            summary["surges"].append(stored.id)
            if is_new:
                summary["new_surges"].append(stored.id)
            if self._maybe_queue(stored):
                summary["queued"].append(stored.id)

    def _record_surge(self, surge: Surge) -> Optional[Tuple[Surge, bool]]:
        """Store a detection, unless it only re-detects a move that already reverted or held.

        Returns ``(stored surge, created)``, or None when the detection was ignored.
        """
        recent = self.store.surges(exchange_id=surge.exchange_id, limit=10)
        same_dir = [s for s in recent if s.direction == surge.direction]
        if not any(s.status == SURGE_OPEN for s in same_dir):
            for old in same_dir:
                if surge.start_ts > old.end_ts:
                    continue  # the window starts after the old move ended: a new move
                if surge.direction == "down":
                    beyond = surge.end_price < old.peak_price - _EPS
                else:
                    beyond = surge.end_price > old.peak_price + _EPS
                if not beyond:
                    return None  # the same (reverted or held) move seen again through a longer window
        known = {s.id for s in recent}
        stored = self.store.record_surge(surge)
        return stored, stored.id not in known

    def _update_open_surges(self, now: float, results: Mapping[str, Mapping[str, Any]]) -> None:
        try:
            open_surges = self.store.surges(status=SURGE_OPEN, limit=1000)
        except Exception as exc:
            self._record_error("surge status", exc)
            return
        failures = 0
        for surge in open_surges:
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
            if (surge.status, surge.current_price, surge.reverted_fraction, surge.peak_price) != before:
                surge.attribution = None  # never overwrite an analysis that finished meanwhile
                self.store.update_surge(surge)

    # ------------------------------------------------------------------ attribution queue

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
                return False
            if baseline is not None and abs(stored.change) - baseline < REANALYZE_GROWTH - _EPS:
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
        self._analysis_wake.set()
        return True

    def analyze_pending(self, max_items: int = 1) -> int:
        """Run attribution for up to ``max_items`` queued surges (synchronous)."""
        if self.attributor is None:
            return 0
        done = 0
        while done < max_items:
            with self._lock:
                if not self._queue:
                    break
                sid = self._queue.popleft()
                self._queued.discard(sid)
            surge = self.store.get_surge(sid)
            if surge is None:
                continue
            try:
                attribution = self.attributor.analyze(surge)
            except _Stopping:
                with self._lock:
                    if sid not in self._queued:
                        self._queue.appendleft(sid)
                        self._queued.add(sid)
                raise
            except SuperMarketError as exc:
                self._api_error(f"attribution of surge {sid}", exc)
                continue
            except Exception as exc:
                log.debug("attribution failure detail", exc_info=True)
                self._record_error(f"attribution of surge {sid}", repr(exc))
                continue
            if attribution is None:
                continue
            if attribution.analyzed_at is None:
                attribution.analyzed_at = self._clock()
            self.store.set_attribution(sid, attribution)
            with self._lock:
                self._analyzed_change[sid] = abs(surge.change)
                self._analyses += 1
                if len(self._analyzed_change) > MAX_ANALYZED_REMEMBERED:
                    for old in sorted(self._analyzed_change)[: len(self._analyzed_change) - MAX_ANALYZED_REMEMBERED]:
                        del self._analyzed_change[old]
                remembered = {str(k): v for k, v in self._analyzed_change.items()}
                self._patch_view_attribution(sid, attribution.to_dict())
            self.store.set_state(ANALYZED_STATE_KEY, remembered)
            done += 1
        return done

    def _patch_view_attribution(self, sid: int, attribution: Dict[str, Any]) -> None:
        """Show a finished analysis right away instead of at the next cycle (caller holds the lock)."""
        if not self._view:
            return
        for row in self._view.get("surges") or []:
            if row.get("id") == sid:
                row["attribution"] = attribution

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
        with self._lock:
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
        return None

    def backfill_step(self, max_reads: int = 1) -> int:
        """Backfill candles for up to ``max_reads`` exchanges (synchronous; used by the worker and tests).

        Each read is one ``price-history`` request (1h x 168 for every outcome first, then
        5m x 288). Returns the number of reads made.
        """
        if not self.backfill_enabled:
            return 0
        reads = 0
        while reads < max_reads:
            task = self._next_backfill()
            if task is None:
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
                resp = self.client.get_price_history(
                    eid, tournament_id=self.context.tournament_id, resolution=res, limit=limit
                )
            except SuperMarketError as exc:
                self._api_error(f"backfill {res} for exchange {eid}", exc)
                self._backfill_failed(eid, res, exc)
                continue
            candles = resp.get("candles") if isinstance(resp, Mapping) else None
            written = self.store.add_candles(eid, res, candles or [])
            self.store.set_state(_backfill_key(eid, res), {"at": self._clock(), "candles": written})
            with self._lock:
                self._bf_done.add((eid, res))
                if written:
                    self._dirty.add(eid)
        return reads

    def _backfill_failed(self, eid: str, res: str, exc: SuperMarketError) -> None:
        key = (eid, res)
        with self._lock:
            attempts = self._bf_attempts.get(key, 0) + 1
            self._bf_attempts[key] = attempts
            if _transient(exc) and attempts < BACKFILL_MAX_ATTEMPTS:
                self._bf_queues[res].append(eid)  # try again after the rest of the queue
            else:
                self._bf_failed.add(key)

    # ------------------------------------------------------------------ context

    def _refresh_context(self, now: float) -> None:
        """Constraints, balance, leaderboard and overround; each piece fails on its own."""
        tid, slug = self.context.tournament_id, self.context.slug
        updates: Dict[str, Any] = {}

        constraints = self._call(
            "constraints", lambda: self.client.get_relationship_constraints(violations_only=True, tournament_id=tid)
        )
        if isinstance(constraints, Mapping):
            violations = [v for v in constraints.get("data") or [] if isinstance(v, Mapping)]
            count = constraints.get("violationsCount")
            updates["constraints"] = {
                "data": violations,
                "violationsCount": count if isinstance(count, int) else len(violations),
                "computedAt": constraints.get("computedAt"),
            }

        initial = self._context.get("initial_balance")
        if slug:
            info = self._call("tournament", lambda: self.client.get_tournament(slug))
            if isinstance(info, Mapping):
                updates["tournament"] = dict(info)
                balance, start = _num(info.get("myBalance")), _num(info.get("initialBalance"))
                if balance is not None:
                    updates["balance"] = balance
                if start is not None:
                    updates["initial_balance"] = initial = start
            board = self._call("leaderboard", lambda: self.client.get_tournament_leaderboard(slug, limit=3))
            if isinstance(board, Mapping):
                updates["leaderboard"] = self._leaderboard(board, initial)

        if self.max_overround_markets:
            rows = self._overround_rows()
            if rows is not None:
                updates["overround"] = rows

        with self._lock:
            self._context.update(updates)
            self._context["updated_at"] = now
            self._context_at = now

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

    def _overround_rows(self) -> Optional[List[Dict[str, Any]]]:
        """Up to ``max_overround_markets`` multi-outcome books per refresh, rotating through all."""
        multi = [m for m in self._markets or [] if m.get("isMultiOutcome") and m.get("id") is not None]
        if not multi:
            return [] if self._markets is not None else None
        tid = self.context.tournament_id
        with self._lock:
            previous = {r["market_id"]: r for r in self._context.get("overround") or []}
            start = self._overround_offset % len(multi)
            self._overround_offset = start + self.max_overround_markets
        batch = (multi[start:] + multi[:start])[: self.max_overround_markets]
        fetched: Dict[str, Dict[str, Any]] = {}
        for market in batch:
            mid = str(market["id"])
            resp = self._call(
                f"orderbook for market {mid}",
                lambda mid=mid: self.client.get_market_orderbook(mid, tournament_id=tid, depth=1),
            )
            if not isinstance(resp, Mapping):
                continue
            _, ob = books_from_market_orderbook(resp, tid)
            fetched[mid] = {
                "market_id": mid,
                "title": market.get("title"),
                "overround": _num(ob.get("overround")),
                "arbitrage": bool(ob.get("hasArbitrageOpportunity")),
                "outcomes": len(ob.get("exchanges") or []),
            }
        rows = []
        for market in multi:
            mid = str(market["id"])
            row = fetched.get(mid) or previous.get(mid)
            if row is not None:
                rows.append(row)
        return rows

    # ------------------------------------------------------------------ view

    def _info(self, eid: str) -> Optional[ExchangeInfo]:
        return self._infos.get(eid) or self._all_infos.get(eid)

    def _surge_row(self, surge: Surge) -> Dict[str, Any]:
        row = surge.to_dict()
        info = self._info(surge.exchange_id)
        row["title"] = info.market_title if info else ""
        row["option"] = info.option if info else None
        return row

    def _build_view(self, now: float, results: Mapping[str, Mapping[str, Any]]) -> None:
        try:
            surges = self.store.surges(since=now - SURGE_VIEW_LOOKBACK_S, limit=SURGE_VIEW_LIMIT)
        except Exception as exc:
            self._record_error("surge list", exc)
            surges = []
        with self._lock:
            infos = dict(self._infos)
            rows = dict(self._rows)
            context = _copy_json(self._context)
        open_by_exchange: Dict[str, int] = {}
        for surge in surges:  # newest first
            if surge.status == SURGE_OPEN and surge.id is not None:
                open_by_exchange.setdefault(surge.exchange_id, surge.id)

        exchanges: List[Dict[str, Any]] = []
        bands: List[Dict[str, Any]] = []
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
                "updated_at": last_point.ts if last_point is not None else None,
            }
            exchanges.append(row)
            if band_dict is not None:
                bands.append({**band_dict, "title": info.market_title, "option": info.option})

        view = {
            "exchanges": exchanges,
            "surges": [self._surge_row(s) for s in surges],
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
                },
                "markets_updated_at": self._markets_at if self._markets is not None else None,
                "context_updated_at": self._context.get("updated_at"),
            }
        client_limiter = getattr(self.client, "read_limiter", None)
        status["read_budget"] = {
            "client": {"used": client_limiter.used, "limit": client_limiter.limit} if client_limiter is not None else None,
            "backfill": {"used": self.backfill_limiter.used, "limit": self.backfill_limiter.limit},
            "analysis": {"used": self.analyze_limiter.used, "limit": self.analyze_limiter.limit},
            "requests_sent": getattr(self.client, "requests_sent", None),
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
            self._fatal_error = None
            if getattr(self.client, "cancelled", False):
                self.client.reset_cancel()  # a previous stop() cancelled the client
            self._started_at = self._clock()
            threads = [threading.Thread(target=self._loop, name="tracker-loop", daemon=True)]
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
                self._run_once()
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break  # fatal: already recorded in status
            except Exception as exc:  # a bug in one cycle should not end tracking
                log.exception("tracker cycle failed")
                self._record_error("cycle", repr(exc))
            next_at += self.interval
            now = time.monotonic()
            if next_at <= now:  # fell behind: skip the missed slots instead of bursting
                next_at += (int((now - next_at) // self.interval) + 1) * self.interval
            if self._stop.wait(next_at - now):
                break

    def _worker(self, step: Callable[[int], int], wake: threading.Event, name: str) -> None:
        while not self._stop.is_set():
            try:
                worked = step(1)
            except _Stopping:
                break
            except SuperMarketError:
                if self._stop.is_set():
                    break
                worked = 0
            except Exception as exc:
                log.exception("tracker %s worker failed", name)
                self._record_error(f"{name} worker", repr(exc))
                worked = 0
            if not worked and wake.wait(WORKER_IDLE_S):
                wake.clear()

    def _backfill_worker(self) -> None:
        self._worker(self.backfill_step, self._backfill_wake, "backfill")

    def _analysis_worker(self) -> None:
        self._worker(self.analyze_pending, self._analysis_wake, "analysis")
