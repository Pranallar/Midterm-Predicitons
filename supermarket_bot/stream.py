"""Live market feed over Supabase Realtime, kept honest with REST resyncs.

Channels (all private broadcast channels):

* ``tournament:{tournament_id}:market:{market_id}`` — tournament markets. Needs the
  minted token (``set_auth``). ``market_batch`` events carry trades and the full,
  versioned book of every exchange they name.
* ``market:{market_id}:combined`` — public markets. Carries trades and
  ``bookDirty`` hints but no books, so dirty books are refetched over REST.

Realtime is best-effort with no replay, so per the API docs this stream reads the
REST order book after every (re)subscribe, revision gap, ``resyncRequired`` batch,
trade with a null ``sequence``, token refresh, book expiry (``nextExpiryAt``), and
periodically (every ``resync_interval`` seconds). When trades may have been missed
(a gap, a rejoin, or a periodic resync that finds a changed book) it also backfills
the REST trade tape since the stream started, de-duplicated by trade id.

Requires the optional ``realtime`` package: ``pip install realtime``.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .books import Book, BookStore, Level, books_from_market_orderbook, format_level, parse_time
from .bot import Context, iso
from .client import SuperMarketClient
from .errors import ApiError, RequestCancelled, SuperMarketError
from .ratelimit import SlidingWindowLimiter

log = logging.getLogger("supermarket_bot")

# Resync reasons after which trades may have been missed, so the tape is backfilled.
BACKFILL_REASONS = {"gap", "resync_required", "unreadable", "null_sequence", "resubscribed", "reconnect"}
MAX_SEEN_TRADES = 50_000


class Verdict(enum.Enum):
    ACCEPT = "accept"  # new batch: process its arrays
    DUPLICATE = "duplicate"  # revision already accepted: ignore arrays (books still apply)
    GAP = "gap"  # at least one batch was missed: resync over REST
    RESYNC = "resync_required"  # batch flagged resyncRequired: resync over REST
    UNREADABLE = "unreadable"  # no usable delivery.revision: resync to be safe


class RevisionTracker:
    """Tracks the last accepted ``delivery.revision`` for one topic (see API docs)."""

    def __init__(self) -> None:
        self.last: Optional[int] = None

    def observe(self, payload: Mapping[str, Any]) -> Verdict:
        delivery = payload.get("delivery")
        if not isinstance(delivery, Mapping):
            return Verdict.UNREADABLE
        revision = delivery.get("revision")
        previous = delivery.get("previousRevision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            return Verdict.UNREADABLE
        # Check resyncRequired first: a compact one carries no arrays at all.
        if payload.get("resyncRequired"):
            if self.last is None or revision > self.last:
                self.last = revision
            return Verdict.RESYNC
        if self.last is not None and revision <= self.last:
            return Verdict.DUPLICATE
        missed = self.last is not None and isinstance(previous, int) and previous > self.last
        self.last = revision
        return Verdict.GAP if missed else Verdict.ACCEPT


@dataclass
class StreamEvent:
    kind: str  # "trade" | "book" | "settled" | "resync" | "status"
    market_id: Optional[str]
    data: Dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "marketId": self.market_id, "at": iso(self.at), "data": self.data}


def topic_for(context: Context, market_id: str) -> str:
    if context.tournament_id:
        return f"tournament:{context.tournament_id}:market:{market_id}"
    return f"market:{market_id}:combined"


def channel_params() -> Dict[str, Any]:
    return {
        "config": {
            "private": True,
            "broadcast": {"ack": False, "self": False},
            "presence": {"key": "", "enabled": False},
        }
    }


RealtimeFactory = Callable[[str, str], Any]


def default_realtime_factory(supabase_url: str, anon_key: str) -> Any:
    try:
        from realtime import AsyncRealtimeClient
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SuperMarketError("The live stream needs the optional 'realtime' package: pip install realtime") from exc
    return AsyncRealtimeClient(f"{supabase_url.rstrip('/')}/realtime/v1", anon_key, auto_reconnect=True)


def safe_resync_interval(markets: int, reads_per_min: int, requested: float = 90.0, share: float = 0.5) -> float:
    """Smallest periodic-resync interval that keeps resyncs within ``share`` of the read budget."""
    floor = markets * 60.0 / max(1.0, reads_per_min * share)
    return max(requested, floor)


class MarketStream:
    """Subscribe to live market batches for a set of markets and keep books current.

    Broadcasts are handled on the event loop as they arrive; REST resyncs and trade
    backfills run in a separate worker so a slow or rate-limited REST read never delays
    the live feed. REST reads share ``rest_share`` of the client's read budget.
    """

    def __init__(
        self,
        client: SuperMarketClient,
        context: Context,
        market_ids: Sequence[str],
        *,
        on_event: Optional[Callable[[StreamEvent], None]] = None,
        resync_interval: float = 90.0,
        token_refresh_margin: float = 600.0,
        depth: int = 200,
        backfill_trades: bool = True,
        log_path: Optional[Path] = None,
        realtime_factory: RealtimeFactory = default_realtime_factory,
        tick: float = 1.0,
        reconnect_after: float = 30.0,
        rest_share: float = 0.5,
        min_dirty_interval: float = 5.0,
        max_backfill_pages: int = 5,
        cancel_client_on_exit: bool = True,
    ) -> None:
        if not market_ids:
            raise ValueError("at least one market id is required")
        self.client = client
        self.context = context
        self.market_ids = [str(m) for m in dict.fromkeys(str(m) for m in market_ids)]
        self.on_event = on_event
        self.resync_interval = resync_interval
        self.token_refresh_margin = token_refresh_margin
        self.depth = depth
        self.backfill_trades = backfill_trades
        self.log_path = log_path
        self.realtime_factory = realtime_factory
        self.tick = tick
        self.reconnect_after = reconnect_after
        self.min_dirty_interval = min_dirty_interval
        self.max_backfill_pages = max_backfill_pages
        self.cancel_client_on_exit = cancel_client_on_exit

        self.books = BookStore()
        self.trackers: Dict[str, RevisionTracker] = {m: RevisionTracker() for m in self.market_ids}
        self.seen_trades: Dict[str, None] = {}
        self.last_trade_ts: Dict[str, float] = {}
        self.market_exchanges: Dict[str, List[str]] = {}
        self.exchange_market: Dict[str, str] = {}
        self.next_periodic: Dict[str, float] = {}
        self.last_rest: Dict[str, float] = {}
        self.started_at = datetime.now(timezone.utc)
        self.resyncs = 0
        self.batches = 0

        self._pending: Dict[str, Set[str]] = {}
        self._retry_delay: Dict[str, float] = {}
        self._subscribed_once: Set[str] = set()
        self._rt: Any = None
        self._token: Optional[Dict[str, Any]] = None
        self._token_expires: Optional[datetime] = None
        self._want_connected = False
        self._down_since: Optional[float] = None
        self._channel_error = False
        self._reconnect_failures = 0
        self._reconnect_pending = False
        self._fatal: Optional[BaseException] = None
        # asyncio objects are created lazily inside the running loop (Python 3.9 binds
        # them to the loop current at construction time).
        self._queue_obj: Optional["asyncio.Queue[Tuple[str, Any]]"] = None
        self._rest_queue_obj: Optional["asyncio.Queue[str]"] = None
        self._stop_obj: Optional[asyncio.Event] = None
        self._stop_requested = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._shutdown = threading.Event()
        share = max(1, int(getattr(client.read_limiter, "limit", 90) * rest_share))
        self._rest_limiter = SlidingWindowLimiter(share, sleep=self._interruptible_sleep)

    # ------------------------------------------------------------ helpers

    @property
    def _queue(self) -> "asyncio.Queue[Tuple[str, Any]]":
        if self._queue_obj is None:
            self._queue_obj = asyncio.Queue()
        return self._queue_obj

    @property
    def _rest_queue(self) -> "asyncio.Queue[str]":
        if self._rest_queue_obj is None:
            self._rest_queue_obj = asyncio.Queue()
        return self._rest_queue_obj

    @property
    def _stop(self) -> asyncio.Event:
        if self._stop_obj is None:
            self._stop_obj = asyncio.Event()
            if self._stop_requested:
                self._stop_obj.set()
        return self._stop_obj

    def _interruptible_sleep(self, seconds: float) -> None:
        if self._shutdown.wait(max(0.0, seconds)):
            raise RequestCancelled("stream is shutting down")

    async def _call(self, fn: Callable[..., Any], *args: Any, _budget: bool = True, **kwargs: Any) -> Any:
        """Run a blocking REST call off the loop, inside the stream's share of the read budget."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="supermarket-stream")

        def call() -> Any:
            if _budget:
                self._rest_limiter.acquire()
            return fn(*args, **kwargs)

        return await asyncio.get_running_loop().run_in_executor(self._executor, call)

    def _emit(self, event: StreamEvent) -> None:
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event.to_json(), separators=(",", ":"), default=str) + "\n")
            except OSError as exc:  # a locked/full log must not kill the stream
                log.warning("could not write stream log: %s", exc)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # never let a display bug kill the stream
                log.exception("on_event handler failed")

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop_obj is not None:
            self._stop_obj.set()

    def request_resync(self, market_id: str, reason: str) -> None:
        """Queue one REST resync per market; reasons accumulate while it is pending."""
        reasons = self._pending.get(market_id)
        if reasons is None:
            self._pending[market_id] = {reason}
            self._queue.put_nowait(("resync", market_id))
        else:
            reasons.add(reason)

    def _requeue(self, market_id: str) -> None:
        if not self._stop_requested and market_id in self._pending:
            self._queue.put_nowait(("resync", market_id))

    # ------------------------------------------------------------ connection

    async def _mint_token(self) -> Dict[str, Any]:
        token = await self._call(self.client.mint_realtime_token, _budget=False)
        self._token = token
        expires = parse_time(token.get("expiresAt"))
        self._token_expires = expires or (datetime.now(timezone.utc) + timedelta(hours=3))
        return token

    async def _connect(self) -> None:
        token = self._token or await self._mint_token()
        rt = self.realtime_factory(token["supabaseUrl"], token["anonKey"])
        try:
            await rt.connect()
            # Authorize before subscribing: tournament channels need the minted token.
            await rt.set_auth(token["token"])
            self._rt = rt
            self._down_since = None
            self._channel_error = False
            for market_id in self.market_ids:
                await self._subscribe(rt, market_id)
        except BaseException:
            self._rt = None
            try:
                await rt.close()
            except Exception:  # pragma: no cover - best effort
                log.debug("closing a half-built realtime client failed", exc_info=True)
            raise

    async def _subscribe(self, rt: Any, market_id: str) -> None:
        topic = topic_for(self.context, market_id)
        channel = rt.channel(topic, channel_params())

        def on_broadcast(message: Mapping[str, Any]) -> None:
            self._queue.put_nowait(("broadcast", (market_id, message)))

        for event in ("market_batch", "book_dirty", "market_settled"):
            channel.on_broadcast(event, on_broadcast)

        def on_state(state: Any, error: Optional[Exception]) -> None:
            name = getattr(state, "value", str(state))
            self._queue.put_nowait(("status", (market_id, name, str(error) if error else None)))
            if name == "SUBSCRIBED":
                # Fires on the first join and again on every rejoin (reconnect/socket error).
                first = market_id not in self._subscribed_once
                self._subscribed_once.add(market_id)
                self._reconnect_failures = 0
                self.request_resync(market_id, "subscribed" if first else "resubscribed")
            else:
                log.warning("channel %s: %s %s", topic, name, error or "")
                if name == "CHANNEL_ERROR":
                    # A refused join is never retried by the library: rebuild the connection
                    # (with a fresh token) after the reconnect back-off.
                    self._channel_error = True
                    self._reconnect_failures += 1

        await channel.subscribe(on_state)

    def _update_join_tokens(self, token: str) -> None:
        """Make library rejoins carry the current token (realtime-py keeps the join payload)."""
        channels = getattr(self._rt, "channels", None)
        if not isinstance(channels, Mapping):
            return
        for channel in list(channels.values()):
            push = getattr(channel, "join_push", None)
            if push is not None and hasattr(push, "update_payload"):
                push.update_payload({"access_token": token})

    async def _close_realtime(self) -> None:
        rt, self._rt = self._rt, None
        if rt is None:
            return
        try:
            await rt.close()
        except Exception:  # pragma: no cover - best effort
            log.debug("closing realtime client failed", exc_info=True)

    def _socket_alive(self) -> bool:
        rt = self._rt
        if rt is None or self._channel_error or not getattr(rt, "is_connected", False):
            return False
        task = getattr(rt, "_listen_task", None)  # realtime-py exposes no public liveness flag
        return not (task is not None and task.done())

    def _reconnect_delay(self) -> float:
        return min(300.0, self.reconnect_after * (2 ** min(self._reconnect_failures, 6)))

    # ------------------------------------------------------------ REST resync

    async def _resync(self, market_id: str) -> None:
        reasons = self._pending.pop(market_id, set())
        try:
            resp = await self._call(
                self.client.get_market_orderbook, market_id, tournament_id=self.context.tournament_id, depth=self.depth
            )
        except RequestCancelled:
            return
        except SuperMarketError as exc:
            log.warning("resync of market %s failed: %s", market_id, exc)
            # Keep the reasons (a gap still needs its backfill) and retry with back-off.
            self._pending.setdefault(market_id, set()).update(reasons)
            delay = min(120.0, max(2.0, self._retry_delay.get(market_id, 1.0) * 2))
            self._retry_delay[market_id] = delay
            asyncio.get_running_loop().call_later(delay, self._requeue, market_id)
            return
        self._retry_delay.pop(market_id, None)
        self.last_rest[market_id] = asyncio.get_running_loop().time()
        self.resyncs += 1
        books, _ = books_from_market_orderbook(resp, self.context.tournament_id, market_id=market_id)
        self.market_exchanges[market_id] = [b.exchange_id for b in books]
        changed = False
        for book in books:
            self.exchange_market[book.exchange_id] = market_id
            before = self.books.get(book.exchange_id)
            if self.books.apply_rest(book):
                changed |= self._emit_book_if_changed(market_id, before, book, source="rest")
        self._emit(StreamEvent("resync", market_id, {"reasons": sorted(reasons), "exchanges": len(books)}))
        if self.backfill_trades and (reasons & BACKFILL_REASONS or ("periodic" in reasons and changed)):
            await self._backfill(market_id)

    async def _backfill(self, market_id: str) -> None:
        """Emit trades from the REST tape that the stream has not seen.

        Pages newest-first from just before the last trade we saw (or the stream start),
        stopping once a page holds only known trades, with a page cap per exchange.
        """
        floor = self.started_at.timestamp()
        for exchange_id in self.market_exchanges.get(market_id, []):
            last = self.last_trade_ts.get(exchange_id)
            since = max(floor, last - 60.0) if last is not None else floor
            collected: List[Mapping[str, Any]] = []
            cursor: Optional[str] = None
            try:
                for _ in range(max(1, self.max_backfill_pages)):
                    kwargs: Dict[str, Any] = {"tournament_id": self.context.tournament_id, "start": iso(datetime.fromtimestamp(since, timezone.utc)), "limit": 200}
                    if cursor:
                        kwargs["cursor"] = cursor
                    page = await self._call(self.client.get_trades, exchange_id, **kwargs)
                    data = [t for t in page.get("data") or [] if isinstance(t, Mapping)]
                    collected.extend(data)
                    pagination = page.get("pagination") or {}
                    cursor = pagination.get("nextCursor")
                    if not pagination.get("hasMore") or not cursor:
                        break
                    if data and all(str(t.get("id")) in self.seen_trades for t in data):
                        break
            except RequestCancelled:
                return
            except SuperMarketError as exc:
                log.warning("trade backfill for exchange %s failed: %s", exchange_id, exc)
            for trade in reversed(collected):  # the tape is newest first
                self._emit_trade(
                    market_id,
                    {
                        "id": trade.get("id"),
                        "exchangeId": exchange_id,
                        "marketId": market_id,
                        "price": trade.get("price"),
                        "quantity": trade.get("size"),
                        "side": trade.get("side"),
                        "executedAt": trade.get("createdAt"),
                        "source": "rest",
                    },
                )

    def _emit_trade(self, market_id: str, trade: Mapping[str, Any]) -> None:
        trade_id = trade.get("id")
        if trade_id is not None:
            key = str(trade_id)
            if key in self.seen_trades:
                return
            self.seen_trades[key] = None
            if len(self.seen_trades) > MAX_SEEN_TRADES:
                for old in list(self.seen_trades)[: MAX_SEEN_TRADES // 5]:
                    del self.seen_trades[old]
        executed = parse_time(trade.get("executedAt"))
        exchange_id = trade.get("exchangeId")
        if executed is not None and exchange_id is not None:
            key = str(exchange_id)
            self.last_trade_ts[key] = max(self.last_trade_ts.get(key, 0.0), executed.timestamp())
        self._emit(StreamEvent("trade", market_id, dict(trade)))

    def _emit_book_if_changed(self, market_id: str, before: Optional[Book], after: Book, source: str) -> bool:
        if before is not None and before.bids == after.bids and before.asks == after.asks:
            return False
        if before is None or before.top() != after.top():
            self._emit(
                StreamEvent(
                    "book",
                    market_id,
                    {
                        "exchangeId": after.exchange_id,
                        "option": after.option,
                        "bestBid": after.best_bid.price if after.best_bid else None,
                        "bidQty": after.best_bid.quantity if after.best_bid else None,
                        "bestAsk": after.best_ask.price if after.best_ask else None,
                        "askQty": after.best_ask.quantity if after.best_ask else None,
                        "sequence": after.as_of.sequence if after.as_of else None,
                        "source": source,
                    },
                )
            )
        return True

    # ------------------------------------------------------------ broadcasts

    async def handle_broadcast(self, market_id: str, message: Mapping[str, Any]) -> None:
        event = message.get("event")
        payload = message.get("payload") if isinstance(message.get("payload"), Mapping) else {}
        if event == "market_batch":
            self.handle_market_batch(market_id, payload)
        elif event == "book_dirty":
            self.request_resync(market_id, "book_dirty")
        elif event == "market_settled":
            self._emit(StreamEvent("settled", market_id, dict(payload)))
            self.request_resync(market_id, "market_settled")

    def handle_market_batch(self, market_id: str, payload: Mapping[str, Any]) -> None:
        self.batches += 1
        # Books apply by version even on a duplicate or resyncRequired batch.
        for raw in payload.get("books") or []:
            if not isinstance(raw, Mapping):
                continue
            book = Book.from_payload(raw, market_id=market_id)
            self.exchange_market[book.exchange_id] = market_id
            before = self.books.get(book.exchange_id)
            if self.books.apply_pushed(book):
                self._emit_book_if_changed(market_id, before, book, source="push")

        verdict = self.trackers.setdefault(market_id, RevisionTracker()).observe(payload)
        if verdict is Verdict.DUPLICATE:
            return

        # Trades are immutable executions keyed by id, so showing the ones a gap or
        # resyncRequired batch still carries is safe; the REST resync fixes state.
        reasons: Set[str] = set()
        if verdict is not Verdict.ACCEPT:
            reasons.add(verdict.value)
        for trade in payload.get("trades") or []:
            if not isinstance(trade, Mapping):
                continue
            if trade.get("sequence") is None:
                reasons.add("null_sequence")  # cannot be ordered; resync price and tape
                continue
            self._emit_trade(market_id, trade)
        for item in payload.get("marketSettled") or []:
            if isinstance(item, Mapping):
                self._emit(StreamEvent("settled", market_id, dict(item)))
                reasons.add("market_settled")
        if payload.get("bookDirty") and not payload.get("books"):
            reasons.add("book_dirty")  # market:* topics carry hints, never books
        for reason in reasons:
            self.request_resync(market_id, reason)

    # ------------------------------------------------------------ workers & timers

    async def _rest_worker(self) -> None:
        """Process queued resyncs one at a time, off the broadcast path."""
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            market_id = await self._rest_queue.get()
            reasons = self._pending.get(market_id)
            if not reasons:
                continue  # already handled by an earlier entry
            if reasons == {"book_dirty"}:
                wait = self.min_dirty_interval - (loop.time() - self.last_rest.get(market_id, float("-inf")))
                if wait > 0:  # coalesce bursts of dirty hints into one refetch
                    loop.call_later(wait, self._requeue, market_id)
                    continue
            try:
                await self._resync(market_id)
            except Exception:
                log.exception("resync of market %s crashed", market_id)

    async def _timers(self) -> None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        stagger = self.resync_interval / max(1, len(self.market_ids))
        for i, market_id in enumerate(self.market_ids):
            # Spread periodic resyncs out so they never burst the read budget.
            self.next_periodic.setdefault(market_id, start + (i + 1) * stagger)
        while not self._stop.is_set():
            await asyncio.sleep(self.tick)
            now = loop.time()
            for market_id in self.market_ids:
                if now >= self.next_periodic[market_id]:
                    self.next_periodic[market_id] = now + self.resync_interval
                    self.request_resync(market_id, "periodic")
            wall = datetime.now(timezone.utc)
            for exchange_id in self.books.expired(wall, grace=1.0):
                book = self.books.get(exchange_id)
                if book is not None:
                    book.next_expiry_at = None  # one refetch per expiry
                market_id = self.exchange_market.get(exchange_id)
                if market_id:
                    self.request_resync(market_id, "expiry")
            if self._token_expires is not None and wall >= self._token_expires - timedelta(seconds=self.token_refresh_margin):
                self._token_expires = None  # set again by the refresh
                self._queue.put_nowait(("refresh_token", None))
            if self._want_connected and not self._reconnect_pending:
                if self._socket_alive():
                    self._down_since = None
                elif self._down_since is None:
                    self._down_since = now
                elif now - self._down_since >= self._reconnect_delay():
                    self._reconnect_pending = True
                    self._queue.put_nowait(("reconnect", None))

    async def _refresh_token(self) -> None:
        try:
            token = await self._mint_token()
        except RequestCancelled:
            return
        except ApiError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                # Revoked key, missing scope…: retrying every minute only burns writes.
                log.error("realtime token refresh refused: %s", exc)
                self._fatal = exc
                self._emit(StreamEvent("status", None, {"status": "fatal", "error": str(exc)}))
                self.stop()
                return
            self._schedule_token_retry(exc)
            return
        except SuperMarketError as exc:
            self._schedule_token_retry(exc)
            return
        if self._rt is not None:
            await self._rt.set_auth(token["token"])
            self._update_join_tokens(token["token"])
        self._emit(StreamEvent("status", None, {"status": "token_refreshed", "expiresAt": token.get("expiresAt")}))
        for market_id in self.market_ids:
            self.request_resync(market_id, "token_refresh")

    def _schedule_token_retry(self, exc: BaseException) -> None:
        log.error("realtime token refresh failed: %s; retrying in 60s", exc)
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        self._token_expires = retry_at + timedelta(seconds=self.token_refresh_margin)

    async def _reconnect(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self._socket_alive():
                self._down_since = None  # recovered while the request was queued
                return
            log.warning("realtime socket is down; reconnecting")
            self._emit(StreamEvent("status", None, {"status": "reconnecting"}))
            await self._close_realtime()
            try:
                await self._connect()
            except Exception as exc:
                self._reconnect_failures += 1
                self._down_since = loop.time()  # try again after a longer back-off
                log.error("reconnect failed: %s (next attempt in %.0fs)", exc, self._reconnect_delay())
                self._emit(StreamEvent("status", None, {"status": "reconnect_failed", "error": str(exc)}))
                return
            for market_id in self.market_ids:
                self.request_resync(market_id, "reconnect")
        finally:
            self._reconnect_pending = False

    # ------------------------------------------------------------ main loop

    async def run(self, duration: Optional[float] = None) -> None:
        """Run until :meth:`stop` is called, ``duration`` seconds pass, or the task is cancelled.

        Raises the fatal :class:`ApiError` (e.g. a revoked key) that stopped the stream, if any.
        """
        loop = asyncio.get_running_loop()
        self.started_at = datetime.now(timezone.utc)
        deadline = None if duration is None else loop.time() + duration
        stop_event = self._stop
        tasks: List["asyncio.Task[Any]"] = []
        client_was_cancelled = self.client.cancelled
        try:
            await self._connect()
            self._want_connected = True
            tasks = [asyncio.create_task(self._timers()), asyncio.create_task(self._rest_worker())]
            stopper = asyncio.create_task(stop_event.wait())
            tasks.append(stopper)
            while not stop_event.is_set():
                timeout = None if deadline is None else deadline - loop.time()
                if timeout is not None and timeout <= 0:
                    break
                getter = asyncio.create_task(self._queue.get())
                done, _ = await asyncio.wait({getter, stopper}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                    continue
                try:
                    kind, item = getter.result()
                    await self._dispatch(kind, item)
                except Exception:
                    log.exception("failed to process a stream event")
        finally:
            self._shutdown.set()  # wake the REST budget waits
            if self.cancel_client_on_exit and not client_was_cancelled:
                self.client.cancel()  # wake 429/503 retry waits inside the client
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_realtime()
            executor, self._executor = self._executor, None
            if executor is not None:
                await loop.run_in_executor(None, executor.shutdown, True)
            if self.cancel_client_on_exit and not client_was_cancelled:
                self.client.reset_cancel()
            self._shutdown.clear()
            self._want_connected = False
        if self._fatal is not None:
            raise self._fatal

    async def _dispatch(self, kind: str, item: Any) -> None:
        if kind == "broadcast":
            market_id, message = item
            await self.handle_broadcast(market_id, message)
        elif kind == "resync":
            self._rest_queue.put_nowait(item)
        elif kind == "status":
            market_id, state, error = item
            self._emit(StreamEvent("status", market_id, {"status": state, "error": error}))
        elif kind == "refresh_token":
            await self._refresh_token()
        elif kind == "reconnect":
            await self._reconnect()


def describe_event(event: StreamEvent) -> str:
    """One human-readable line per event, for the CLI."""
    stamp = event.at.astimezone().strftime("%H:%M:%S")
    d = event.data
    if event.kind == "trade":
        side = f" {d['side']}" if d.get("side") else ""
        return f"[{stamp}] TRADE   mkt {event.market_id} exch {d.get('exchangeId')}  {d.get('price')} x {d.get('quantity')}{side}"
    if event.kind == "book":
        bid = format_level(_level(d.get("bestBid"), d.get("bidQty")))
        ask = format_level(_level(d.get("bestAsk"), d.get("askQty")))
        label = f"exch {d.get('exchangeId')}" + (f" ({d['option']})" if d.get("option") else "")
        return f"[{stamp}] BOOK    mkt {event.market_id} {label}  bid {bid} | ask {ask}  [{d.get('source')}]"
    if event.kind == "settled":
        return f"[{stamp}] SETTLED mkt {event.market_id} -> {d.get('settledWith')}"
    if event.kind == "resync":
        return f"[{stamp}] resync  mkt {event.market_id} ({', '.join(d.get('reasons') or [])})"
    status = d.get("status")
    extra = f" {d['error']}" if d.get("error") else ""
    return f"[{stamp}] {status} {event.market_id or ''}{extra}".rstrip()


def _level(price: Any, qty: Any) -> Optional[Level]:
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        return Level(float(price), float(qty or 0))
    return None
