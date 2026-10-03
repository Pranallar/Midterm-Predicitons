"""Tests for ``supermarket_bot.stream``: revision tracking, batch handling, REST resyncs,
token refresh and reconnects.

Semantics under test come from the ``POST /realtime/token`` operation description in
``docs/supermarket-openapi.json`` (delivery/revision rules, versioned ``books``,
``resyncRequired``, null trade ``sequence``, ``nextExpiryAt`` and the resync list).

Part 3 runs the real ``realtime`` (supabase realtime-py) client against a tiny fake
Supabase Realtime server (Phoenix v1 JSON protocol) bound to 127.0.0.1. No other
network is touched: REST goes through ``httpx.MockTransport`` (see conftest).
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import TOURNAMENT_ID, TOURNAMENT_SLUG, book_exchange, error_body, market_orderbook
from realtime.types import RealtimeSubscribeStates
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from supermarket_bot import stream as stream_mod
from supermarket_bot.books import Book, BookVersion, Level, parse_time
from supermarket_bot.bot import Context, iso
from supermarket_bot.stream import (
    BACKFILL_REASONS,
    MarketStream,
    RevisionTracker,
    StreamEvent,
    Verdict,
    channel_params,
    default_realtime_factory,
    describe_event,
    safe_resync_interval,
    topic_for,
)

CTX = Context(TOURNAMENT_ID, TOURNAMENT_SLUG, "SIG Predictions Cup")
PUBLIC = Context.public()
MID = "26"
TOPIC = f"tournament:{TOURNAMENT_ID}:market:{MID}"
OB_PATH = f"/markets/{MID}/orderbook"
TAPE_36 = "/exchanges/36/trades"
TIMEOUT = 8.0


# --------------------------------------------------------------------------- helpers


def async_test(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run an ``async def`` test body in a fresh loop (pytest-asyncio is not installed)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), TIMEOUT))

    return wrapper


async def until(pred: Callable[[], bool], timeout: float = 2.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def delivery(rev: Any, prev: Any = None) -> Dict[str, Any]:
    return {
        "model": "best-effort-authoritative-resync",
        "correlationId": f"corr-{rev}",
        "revision": rev,
        "previousRevision": prev,
        "sourceSequenceFrom": 1,
        "sourceSequenceThrough": 2,
    }


def batch(
    rev: Any,
    prev: Any = None,
    *,
    trades: Optional[List[Dict[str, Any]]] = None,
    books: Optional[List[Dict[str, Any]]] = None,
    book_dirty: Optional[List[Dict[str, Any]]] = None,
    settled: Optional[List[Dict[str, Any]]] = None,
    resync: bool = False,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "trades": trades or [],
        "bookDirty": book_dirty or [],
        "marketSettled": settled or [],
        "delivery": delivery(rev, prev),
    }
    if books is not None:
        body["books"] = books
    if resync:
        body["resyncRequired"] = True
    return body


def trade(tid: Optional[str], seq: Optional[int], eid: str = "36", px: float = 0.42, qty: float = 10) -> Dict[str, Any]:
    return {
        "id": tid,
        "sequence": seq,
        "exchangeId": eid,
        "marketId": MID,
        "price": px,
        "quantity": qty,
        "executedAt": "2026-10-03T12:00:01.000Z",
        "tournamentId": TOURNAMENT_ID,
    }


def pushed_book(
    eid: int,
    bids: Sequence[Tuple[float, float]],
    asks: Sequence[Tuple[float, float]],
    seq: int,
    at: str = "2026-10-03T12:00:00.000Z",
    expiry: Optional[str] = None,
) -> Dict[str, Any]:
    # Pushed books carry exchangeId as a number and no option label.
    return {
        "exchangeId": eid,
        "asOf": {"sequence": seq, "at": at},
        "nextExpiryAt": expiry,
        "bids": [{"price": p, "quantity": q} for p, q in bids],
        "asks": [{"price": p, "quantity": q} for p, q in asks],
    }


def rest_trade(tid: str, px: float, size: int, side: str = "YES", at: str = "2026-10-03T12:00:02.000Z") -> Dict[str, Any]:
    return {"id": tid, "createdAt": at, "price": px, "size": size, "side": side, "volume": px * size}


def tape(eid: str, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "exchangeId": eid,
        "marketId": MID,
        "from": "2026-10-03T00:00:00.000Z",
        "to": "2026-10-03T23:00:00.000Z",
        "data": trades,
        "pagination": {"limit": 200, "hasMore": False, "nextCursor": None},
        "coverage": {"complete": True, "projectedThroughSequence": None},
    }


def ob(*books: Dict[str, Any], tid: Optional[str] = TOURNAMENT_ID) -> Dict[str, Any]:
    return market_orderbook(list(books), tid=tid)


OB_36 = ob(book_exchange("36", [(0.40, 10)], [(0.45, 5)], seq=100))


def token_body(url: str, token: str = "jwt-token", expires_in: float = 3 * 3600) -> Dict[str, Any]:
    expires = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return {
        "token": token,
        "expiresAt": iso(expires),
        "supabaseUrl": url,
        "anonKey": "anon",
        "channels": {"user": "user:00000000-0000-0000-0000-000000000001"},
    }


def make_stream(client: Any, ctx: Context = CTX, markets: Sequence[str] = (MID,), **kwargs: Any) -> Tuple[MarketStream, List[StreamEvent]]:
    events: List[StreamEvent] = []
    kwargs.setdefault("on_event", events.append)
    kwargs.setdefault("resync_interval", 1000.0)
    kwargs.setdefault("tick", 0.02)
    return MarketStream(client, ctx, list(markets), **kwargs), events


def of_kind(events: List[StreamEvent], kind: str) -> List[StreamEvent]:
    return [e for e in events if e.kind == kind]


def statuses(events: List[StreamEvent]) -> List[Any]:
    return [e.data.get("status") for e in of_kind(events, "status")]


def resync_reasons(events: List[StreamEvent]) -> List[List[str]]:
    return [e.data["reasons"] for e in of_kind(events, "resync")]


def reasons_after_first(events: List[StreamEvent]) -> set:
    return set().union(*map(set, resync_reasons(events)[1:]))


def queued(stream: MarketStream) -> List[Tuple[str, Any]]:
    items = []
    while not stream._queue.empty():
        items.append(stream._queue.get_nowait())
    return items


# --------------------------------------------------------------------------- Part 1: pure logic


class TestRevisionTracker:
    def test_first_batch_is_accepted_whatever_previous_revision_says(self):
        t = RevisionTracker()
        # No revision accepted yet: nothing can have been "missed" relative to it.
        assert t.observe(batch(57, 40)) is Verdict.ACCEPT
        assert t.last == 57

    def test_consecutive_batches_advance_the_last_revision(self):
        t = RevisionTracker()
        for rev in (1, 2, 3):
            assert t.observe(batch(rev, rev - 1)) is Verdict.ACCEPT
        assert t.last == 3

    @pytest.mark.parametrize("rev", [10, 9, 1])
    def test_revision_at_or_below_last_is_a_duplicate(self, rev):
        t = RevisionTracker()
        t.observe(batch(10, 9))
        assert t.observe(batch(rev, rev - 1)) is Verdict.DUPLICATE
        assert t.last == 10  # never goes backwards

    def test_gap_when_previous_revision_exceeds_last_accepted(self):
        t = RevisionTracker()
        t.observe(batch(5, 4))
        assert t.observe(batch(8, 7)) is Verdict.GAP
        assert t.last == 8  # advanced so the next consecutive batch is accepted
        assert t.observe(batch(9, 8)) is Verdict.ACCEPT

    def test_previous_revision_below_last_is_not_a_gap(self):
        # A batch may cover several revisions after previousRevision; overlap is fine.
        t = RevisionTracker()
        t.observe(batch(5, 4))
        assert t.observe(batch(7, 3)) is Verdict.ACCEPT
        assert t.last == 7

    @pytest.mark.parametrize("prev", [None, "4", 4.5])
    def test_non_integer_previous_revision_is_not_treated_as_gap(self, prev):
        t = RevisionTracker()
        t.observe(batch(3, 2))
        assert t.observe(batch(6, prev)) is Verdict.ACCEPT
        assert t.last == 6

    def test_resync_required_on_first_batch_sets_last(self):
        t = RevisionTracker()
        assert t.observe(batch(4, 3, resync=True)) is Verdict.RESYNC
        assert t.last == 4

    def test_resync_required_advances_last_only_when_higher(self):
        t = RevisionTracker()
        t.observe(batch(10, 9))
        assert t.observe(batch(12, 11, resync=True)) is Verdict.RESYNC
        assert t.last == 12
        # A stale or repeated resyncRequired batch still asks for a resync but never lowers last.
        assert t.observe(batch(11, 10, resync=True)) is Verdict.RESYNC
        assert t.observe(batch(12, 11, resync=True)) is Verdict.RESYNC
        assert t.last == 12

    def test_resync_required_wins_over_gap(self):
        t = RevisionTracker()
        t.observe(batch(1, 0))
        assert t.observe(batch(9, 8, resync=True)) is Verdict.RESYNC
        assert t.last == 9
        assert t.observe(batch(10, 9)) is Verdict.ACCEPT

    def test_compact_resync_required_without_arrays(self):
        t = RevisionTracker()
        t.observe(batch(2, 1))
        assert t.observe({"resyncRequired": True, "delivery": delivery(3, 2)}) is Verdict.RESYNC
        assert t.last == 3
        assert t.observe(batch(3, 2)) is Verdict.DUPLICATE

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"trades": []},
            {"delivery": None},
            {"delivery": "rev-5"},
            {"delivery": [5]},
            {"delivery": {"previousRevision": 4}},
            {"delivery": {"revision": "5", "previousRevision": 4}},
            {"delivery": {"revision": 5.5, "previousRevision": 4}},
            {"delivery": {"revision": True, "previousRevision": 0}},
            {"delivery": {"revision": None}, "resyncRequired": True},
        ],
        ids=["empty", "no-delivery", "null", "string", "list", "no-revision", "str-revision", "float-revision", "bool-revision", "resync-no-revision"],
    )
    def test_missing_or_garbage_delivery_is_unreadable_and_keeps_state(self, payload):
        t = RevisionTracker()
        t.observe(batch(3, 2))
        assert t.observe(payload) is Verdict.UNREADABLE
        assert t.last == 3


def test_topic_for_tournament_and_public_contexts():
    assert topic_for(CTX, "26") == f"tournament:{TOURNAMENT_ID}:market:26"
    assert topic_for(PUBLIC, "26") == "market:26:combined"


def test_channel_params_are_private_broadcast_without_presence():
    params = channel_params()
    assert params["config"]["private"] is True
    assert params["config"]["broadcast"] == {"ack": False, "self": False}
    assert params["config"]["presence"]["enabled"] is False
    # Fresh dict each call, so realtime-py mutating one channel's config cannot leak.
    params["config"]["presence"]["enabled"] = True
    assert channel_params()["config"]["presence"]["enabled"] is False


@pytest.mark.parametrize(
    "markets, rpm, kwargs, expected",
    [
        (5, 600, {}, 90.0),  # budget allows the requested interval
        (100, 60, {}, 200.0),  # 100 reads per interval within 30/min -> 200s
        (100, 60, {"share": 1.0}, 100.0),
        (10, 600, {"requested": 30.0}, 30.0),
        (3, 0, {"requested": 1.0}, 180.0),  # zero budget does not divide by zero
    ],
)
def test_safe_resync_interval(markets, rpm, kwargs, expected):
    interval = safe_resync_interval(markets, rpm, **kwargs)
    assert interval == pytest.approx(expected)
    share = kwargs.get("share", 0.5)
    if rpm:
        # Periodic resyncs (one read per market per interval) stay within the share.
        assert markets * 60.0 / interval <= rpm * share + 1e-9


def test_default_realtime_factory_builds_socket_url_from_supabase_url():
    rt = default_realtime_factory("https://abc.supabase.co/", "anon-key")
    assert rt.url == "wss://abc.supabase.co/realtime/v1/websocket?apikey=anon-key"
    assert rt.auto_reconnect is True
    assert not rt.is_connected  # constructing does not connect
    assert default_realtime_factory("http://127.0.0.1:5000", "k").url == "ws://127.0.0.1:5000/realtime/v1/websocket?apikey=k"


def test_stream_event_to_json():
    at = datetime(2026, 10, 3, 12, 0, 0, 123000, tzinfo=timezone.utc)
    ev = StreamEvent("trade", "26", {"id": "1"}, at=at)
    assert ev.to_json() == {"kind": "trade", "marketId": "26", "at": "2026-10-03T12:00:00.123Z", "data": {"id": "1"}}


def test_describe_event_lines():
    at = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
    trade_line = describe_event(StreamEvent("trade", "26", {"exchangeId": "36", "price": 0.42, "quantity": 10, "side": "NO"}, at=at))
    assert "TRADE" in trade_line and "mkt 26 exch 36" in trade_line and "0.42 x 10 NO" in trade_line
    book_line = describe_event(
        StreamEvent("book", "26", {"exchangeId": "36", "option": "YES", "bestBid": 0.4, "bidQty": 10, "bestAsk": None, "source": "push"}, at=at)
    )
    assert "exch 36 (YES)" in book_line and "bid 0.400 x 10" in book_line and "ask —" in book_line and "[push]" in book_line
    assert describe_event(StreamEvent("settled", "26", {"settledWith": "REFUND"}, at=at)).endswith("SETTLED mkt 26 -> REFUND")
    assert describe_event(StreamEvent("resync", "26", {"reasons": ["gap", "periodic"]}, at=at)).endswith("resync  mkt 26 (gap, periodic)")
    assert describe_event(StreamEvent("status", None, {"status": "token_refreshed"}, at=at)).endswith("token_refreshed")
    assert describe_event(StreamEvent("status", "26", {"status": "CHANNEL_ERROR", "error": "denied"}, at=at)).endswith("CHANNEL_ERROR 26 denied")


def test_constructor_requires_markets_and_dedupes_ids(client):
    async def body():
        with pytest.raises(ValueError):
            MarketStream(client, CTX, [])
        s = MarketStream(client, CTX, [26, "26", "27", 27, "25"])
        assert s.market_ids == ["26", "27", "25"]
        assert set(s.trackers) == {"26", "27", "25"}

    asyncio.run(body())


# --------------------------------------------------------------------------- Part 2: handlers without a socket


class TestMarketBatch:
    @async_test
    async def test_accepted_batch_emits_trades_in_order_and_book(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(
            MID,
            batch(
                1,
                0,
                trades=[trade("1001", 1001, px=0.41, qty=3), trade("1002", 1002, px=0.42, qty=4)],
                books=[pushed_book(36, [(0.40, 10), (0.38, 2)], [(0.45, 5)], seq=1002)],
            ),
        )
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["1001", "1002"]
        assert of_kind(events, "trade")[0].data["price"] == 0.41
        (book_ev,) = of_kind(events, "book")
        assert book_ev.market_id == MID
        assert book_ev.data == {
            "exchangeId": "36",
            "option": None,
            "bestBid": 0.40,
            "bidQty": 10.0,
            "bestAsk": 0.45,
            "askQty": 5.0,
            "sequence": 1002,
            "source": "push",
        }
        held = s.books.get("36")
        assert held.bids == [Level(0.40, 10.0), Level(0.38, 2.0)]
        assert s.exchange_market["36"] == MID
        assert s.trackers[MID].last == 1
        assert s.batches == 1
        assert s._pending == {} and queued(s) == []  # a clean batch needs no resync

    @async_test
    async def test_duplicate_revision_still_applies_newer_book_but_ignores_trades(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(5, 4, trades=[trade("1", 1)], books=[pushed_book(36, [(0.40, 10)], [(0.45, 5)], seq=100)]))
        events.clear()
        s.handle_market_batch(MID, batch(5, 4, trades=[trade("2", 2)], books=[pushed_book(36, [(0.41, 1)], [(0.45, 5)], seq=101)]))
        assert of_kind(events, "trade") == []  # arrays of a duplicate are ignored...
        assert s.books.get("36").as_of.sequence == 101  # ...but its books apply by version
        assert [e.data["bestBid"] for e in of_kind(events, "book")] == [0.41]
        assert s._pending == {}
        # The ignored trade id was not marked seen: it shows if it later arrives legitimately.
        s.handle_market_batch(MID, batch(6, 5, trades=[trade("2", 2)]))
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["2"]

    @async_test
    async def test_older_pushed_book_is_ignored(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.40, 10)], [(0.45, 5)], seq=105)]))
        events.clear()
        s.handle_market_batch(MID, batch(2, 1, books=[pushed_book(36, [(0.10, 1)], [(0.90, 1)], seq=104)]))
        assert s.books.get("36").as_of.sequence == 105
        assert s.books.get("36").best_bid == Level(0.40, 10.0)
        assert of_kind(events, "book") == []

    @async_test
    async def test_same_sequence_later_at_is_newer(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.40, 10)], [(0.45, 5)], seq=7, at="2026-10-03T12:00:00.000Z")]))
        s.handle_market_batch(MID, batch(2, 1, books=[pushed_book(36, [(0.41, 10)], [(0.45, 5)], seq=7, at="2026-10-03T12:00:00.500+00:00")]))
        assert s.books.get("36").best_bid.price == 0.41
        assert len(of_kind(events, "book")) == 2

    @async_test
    async def test_book_event_only_when_top_of_book_changes(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.40, 10), (0.30, 1)], [(0.45, 5)], seq=1)]))
        # Deeper level changes: stored, but the top is the same, so no event.
        s.handle_market_batch(MID, batch(2, 1, books=[pushed_book(36, [(0.40, 10), (0.35, 7)], [(0.45, 5)], seq=2)]))
        assert s.books.get("36").bids[1] == Level(0.35, 7.0)
        assert len(of_kind(events, "book")) == 1
        # Quantity change at the top is a top-of-book change.
        s.handle_market_batch(MID, batch(3, 2, books=[pushed_book(36, [(0.40, 4), (0.35, 7)], [(0.45, 5)], seq=3)]))
        assert [e.data["bidQty"] for e in of_kind(events, "book")] == [10.0, 4.0]
        # Book emptied on one side.
        s.handle_market_batch(MID, batch(4, 3, books=[pushed_book(36, [(0.40, 4)], [], seq=4)]))
        last = of_kind(events, "book")[-1].data
        assert last["bestAsk"] is None and last["askQty"] is None

    @async_test
    async def test_multi_exchange_books_are_demuxed_by_exchange_id(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(
            MID,
            batch(1, 0, books=[pushed_book(36, [(0.40, 1)], [(0.45, 1)], seq=10), pushed_book(37, [(0.55, 2)], [(0.60, 2)], seq=11)]),
        )
        assert {e.data["exchangeId"] for e in of_kind(events, "book")} == {"36", "37"}
        assert s.exchange_market == {"36": MID, "37": MID}

    @async_test
    async def test_each_trade_is_emitted_once_across_batches(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, trades=[trade("1", 1), trade("2", 2)]))
        s.handle_market_batch(MID, batch(2, 1, trades=[trade("2", 2), trade("3", 3)]))
        s.handle_market_batch(MID, batch(3, 2, trades=[trade("3", 3), trade("1", 1)]))
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["1", "2", "3"]

    @async_test
    async def test_null_sequence_trade_triggers_resync_and_is_not_emitted(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, trades=[trade(None, None, px=0.5), trade("7", 7)]))
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["7"]
        assert s._pending == {MID: {"null_sequence"}}
        assert queued(s) == [("resync", MID)]
        assert "null_sequence" in BACKFILL_REASONS  # the REST tape will supply it

    @async_test
    async def test_gap_requests_resync_but_still_shows_carried_trades(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0))
        s.handle_market_batch(MID, batch(4, 3, trades=[trade("40", 40)]))
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["40"]
        assert s._pending == {MID: {"gap"}}
        assert queued(s) == [("resync", MID)]

    @async_test
    async def test_resync_required_applies_books_and_requests_resync(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0))
        s.handle_market_batch(MID, batch(2, 1, resync=True, trades=[trade("5", 5)], books=[pushed_book(36, [(0.40, 1)], [(0.50, 1)], seq=50)]))
        assert s.books.get("36").as_of.sequence == 50
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["5"]
        assert s._pending == {MID: {"resync_required"}}
        assert s.trackers[MID].last == 2

    @async_test
    async def test_compact_resync_required_has_no_arrays(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0))
        s.handle_market_batch(MID, {"resyncRequired": True, "delivery": delivery(2, 1)})
        assert s._pending == {MID: {"resync_required"}}
        assert [e.kind for e in events] == []

    @async_test
    async def test_unreadable_delivery_requests_resync(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, {"trades": [trade("9", 9)]})
        assert s._pending == {MID: {"unreadable"}}
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["9"]

    @async_test
    async def test_market_settled_emits_settled_and_requests_resync(self, client):
        s, events = make_stream(client)
        item = {"marketId": MID, "tournamentId": TOURNAMENT_ID, "settledWith": "Candidate A", "at": "2026-11-04T05:00:00.000Z"}
        s.handle_market_batch(MID, batch(1, 0, settled=[item]))
        (settled,) = of_kind(events, "settled")
        assert settled.market_id == MID and settled.data == item
        assert s._pending == {MID: {"market_settled"}}

    @async_test
    async def test_book_dirty_without_books_requests_resync_on_public_topic(self, client):
        s, events = make_stream(client, ctx=PUBLIC)
        dirty = [{"exchangeId": "36", "marketId": MID, "tournamentId": None, "at": "2026-10-03T12:00:00.000Z"}]
        s.handle_market_batch(MID, batch(1, 0, book_dirty=dirty))
        assert s._pending == {MID: {"book_dirty"}}

    @async_test
    async def test_book_dirty_with_books_needs_no_resync(self, client):
        s, events = make_stream(client)
        dirty = [{"exchangeId": "36", "marketId": MID, "tournamentId": TOURNAMENT_ID, "at": "2026-10-03T12:00:00.000Z"}]
        s.handle_market_batch(MID, batch(1, 0, book_dirty=dirty, books=[pushed_book(36, [(0.4, 1)], [(0.5, 1)], seq=3)]))
        assert s._pending == {}
        assert len(of_kind(events, "book")) == 1

    @async_test
    async def test_reasons_from_one_batch_coalesce_into_one_resync(self, client):
        s, _ = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0))
        settled = [{"marketId": MID, "settledWith": "A"}, {"marketId": MID, "settledWith": "A"}]
        s.handle_market_batch(MID, batch(5, 4, trades=[trade(None, None)], settled=settled))
        assert s._pending == {MID: {"gap", "null_sequence", "market_settled"}}
        assert queued(s) == [("resync", MID)]

    @async_test
    async def test_garbage_items_are_skipped(self, client):
        s, events = make_stream(client)
        s.handle_market_batch(MID, {"delivery": delivery(1, 0), "trades": ["x", None, trade("1", 1)], "books": [3, None], "marketSettled": ["y"]})
        assert [e.kind for e in events] == ["trade"]
        assert s._pending == {}


class TestBroadcastDispatch:
    @async_test
    async def test_individual_book_dirty_and_market_settled_events(self, client):
        s, events = make_stream(client)
        await s.handle_broadcast(MID, {"event": "book_dirty", "payload": {"exchangeId": "36"}})
        await s.handle_broadcast(MID, {"event": "market_settled", "payload": {"marketId": MID, "settledWith": "REFUND"}})
        assert s._pending == {MID: {"book_dirty", "market_settled"}}
        assert [e.data for e in of_kind(events, "settled")] == [{"marketId": MID, "settledWith": "REFUND"}]

    @async_test
    async def test_market_batch_with_non_mapping_payload_is_unreadable(self, client):
        s, _ = make_stream(client)
        await s.handle_broadcast(MID, {"event": "market_batch", "payload": None})
        assert s.batches == 1
        assert s._pending == {MID: {"unreadable"}}

    @async_test
    async def test_unknown_event_is_ignored(self, client):
        s, events = make_stream(client)
        await s.handle_broadcast(MID, {"event": "tick", "payload": {"at": "x"}})
        assert events == [] and s._pending == {}

    @async_test
    async def test_request_resync_coalesces_while_pending(self, client):
        s, _ = make_stream(client, markets=(MID, "27"))
        s.request_resync(MID, "gap")
        s.request_resync(MID, "periodic")
        s.request_resync("27", "expiry")
        s.request_resync(MID, "gap")
        assert s._pending == {MID: {"gap", "periodic"}, "27": {"expiry"}}
        assert queued(s) == [("resync", MID), ("resync", "27")]


class TestEmit:
    @async_test
    async def test_on_event_errors_are_swallowed_and_log_file_written(self, client, tmp_path):
        log_path = tmp_path / "nested" / "events.jsonl"

        def boom(event: StreamEvent) -> None:
            raise RuntimeError("display bug")

        s, _ = make_stream(client, on_event=boom, log_path=log_path)
        s.handle_market_batch(MID, batch(1, 0, trades=[trade("1", 1)], books=[pushed_book(36, [(0.4, 1)], [(0.5, 1)], seq=1)]))
        lines = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert [line["kind"] for line in lines] == ["book", "trade"]
        assert lines[1]["marketId"] == MID and lines[1]["data"]["id"] == "1"

    @async_test
    async def test_seen_trade_ids_are_bounded(self, client, monkeypatch):
        monkeypatch.setattr(stream_mod, "MAX_SEEN_TRADES", 10)
        s, events = make_stream(client)
        for i in range(11):
            s._emit_trade(MID, {"id": str(i)})
        assert len(s.seen_trades) == 9  # the oldest fifth was evicted
        assert "0" not in s.seen_trades and "10" in s.seen_trades
        s._emit_trade(MID, {"id": "10"})
        assert len(of_kind(events, "trade")) == 11


class TestResync:
    @async_test
    async def test_subscribed_resync_reads_rest_book_without_backfill(self, client, fake):
        fake.add("GET", OB_PATH, ob(book_exchange("36", [(0.40, 10)], [(0.45, 5)], seq=100), book_exchange("37", [(0.55, 1)], [(0.60, 2)], seq=99, option="NO")))
        s, events = make_stream(client)
        s.request_resync(MID, "subscribed")
        await s._resync(MID)
        (call,) = fake.calls_to(OB_PATH)
        assert call.method == "GET"
        assert call.params == {"tournamentId": TOURNAMENT_ID, "depth": "200"}
        assert fake.calls_to(TAPE_36) == []  # a first subscription has nothing to backfill
        assert s.books.get("36").as_of == BookVersion(100, parse_time("2026-10-03T12:00:00.000+00:00"))
        assert s.market_exchanges[MID] == ["36", "37"]
        assert s.exchange_market == {"36": MID, "37": MID}
        books = of_kind(events, "book")
        assert [(e.data["exchangeId"], e.data["option"], e.data["source"]) for e in books] == [("36", "YES", "rest"), ("37", "NO", "rest")]
        (rs,) = of_kind(events, "resync")
        assert rs.data == {"reasons": ["subscribed"], "exchanges": 2}
        assert s.resyncs == 1
        assert s._pending == {}

    @pytest.mark.parametrize("reason", sorted(BACKFILL_REASONS))
    @async_test
    async def test_backfill_reasons_fetch_trade_tape_since_stream_start(self, client, fake, reason):
        fake.add("GET", OB_PATH, OB_36)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("12", 0.43, 3, "NO", "2026-10-03T12:00:03.000Z"), rest_trade("11", 0.42, 5)]))
        s, events = make_stream(client)
        s.started_at = datetime(2026, 10, 3, 11, 0, 0, tzinfo=timezone.utc)
        s.request_resync(MID, reason)
        await s._resync(MID)
        (call,) = fake.calls_to(TAPE_36)
        assert call.params == {"tournamentId": TOURNAMENT_ID, "from": "2026-10-03T11:00:00.000Z", "limit": "200"}
        trades = of_kind(events, "trade")
        assert [t.data["id"] for t in trades] == ["11", "12"]  # tape is newest first; emitted oldest first
        assert trades[1].data == {
            "id": "12",
            "exchangeId": "36",
            "marketId": MID,
            "price": 0.43,
            "quantity": 3,
            "side": "NO",
            "executedAt": "2026-10-03T12:00:03.000Z",
            "source": "rest",
        }

    @pytest.mark.parametrize("reason", ["subscribed", "token_refresh", "expiry", "book_dirty", "market_settled"])
    @async_test
    async def test_other_reasons_do_not_backfill(self, client, fake, reason):
        fake.add("GET", OB_PATH, OB_36)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("11", 0.42, 5)]))
        s, _ = make_stream(client)
        s.request_resync(MID, reason)
        await s._resync(MID)
        assert len(fake.calls_to(OB_PATH)) == 1
        assert fake.calls_to(TAPE_36) == []

    @async_test
    async def test_backfill_dedupes_against_streamed_trades(self, client, fake):
        fake.add("GET", OB_PATH, OB_36)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("3", 0.44, 1), rest_trade("2", 0.43, 1), rest_trade("1", 0.42, 1)]))
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, trades=[trade("1", 1)]))
        s.handle_market_batch(MID, batch(5, 4, trades=[trade("3", 3)]))  # gap: trade 2 was missed
        assert s._pending == {MID: {"gap"}}
        await s._resync(MID)
        trades = of_kind(events, "trade")
        assert [(t.data["id"], t.data.get("source")) for t in trades] == [("1", None), ("3", None), ("2", "rest")]
        # A second backfill re-reads the same tape but emits nothing new.
        s.request_resync(MID, "reconnect")
        await s._resync(MID)
        assert len(of_kind(events, "trade")) == 3

    @async_test
    async def test_periodic_resync_backfills_only_when_book_changed(self, client, fake):
        changed = ob(book_exchange("36", [(0.41, 10)], [(0.45, 5)], seq=103))
        fake.add("GET", OB_PATH, OB_36, OB_36, changed)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("103", 0.41, 2)]))
        s, events = make_stream(client)
        s.request_resync(MID, "subscribed")
        await s._resync(MID)
        s.request_resync(MID, "periodic")
        await s._resync(MID)  # same book: nothing could have been missed
        assert fake.calls_to(TAPE_36) == []
        s.request_resync(MID, "periodic")
        await s._resync(MID)  # changed book: a trade may have been missed
        assert len(fake.calls_to(TAPE_36)) == 1
        assert [t.data["id"] for t in of_kind(events, "trade")] == ["103"]
        assert [e.data["bestBid"] for e in of_kind(events, "book")] == [0.40, 0.41]

    @async_test
    async def test_periodic_resync_with_depth_only_change_backfills_without_book_event(self, client, fake):
        deeper = ob(book_exchange("36", [(0.40, 10), (0.30, 9)], [(0.45, 5)], seq=101))
        fake.add("GET", OB_PATH, OB_36, deeper)
        fake.add("GET", TAPE_36, tape("36", []))
        s, events = make_stream(client)
        s.request_resync(MID, "subscribed")
        await s._resync(MID)
        s.request_resync(MID, "periodic")
        await s._resync(MID)
        assert len(of_kind(events, "book")) == 1  # top unchanged
        assert len(fake.calls_to(TAPE_36)) == 1  # but depth changed, so the tape is checked

    @async_test
    async def test_backfill_disabled(self, client, fake):
        fake.add("GET", OB_PATH, OB_36)
        s, _ = make_stream(client, backfill_trades=False)
        s.request_resync(MID, "gap")
        await s._resync(MID)
        assert fake.calls_to(TAPE_36) == []

    @async_test
    async def test_rest_book_does_not_replace_newer_pushed_book(self, client, fake):
        fake.add("GET", OB_PATH, OB_36)  # seq 100
        s, events = make_stream(client)
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.47, 1)], [(0.49, 1)], seq=150)]))
        events.clear()
        s.request_resync(MID, "periodic")
        await s._resync(MID)
        assert s.books.get("36").as_of.sequence == 150
        assert s.books.get("36").best_bid.price == 0.47
        assert of_kind(events, "book") == []
        assert fake.calls_to(TAPE_36) == []  # unchanged -> periodic does not backfill

    @async_test
    async def test_pushed_book_after_rest_keeps_option_label(self, client, fake):
        fake.add("GET", OB_PATH, ob(book_exchange("36", [(0.40, 10)], [(0.45, 5)], seq=100, option="Candidate A")))
        s, events = make_stream(client)
        s.request_resync(MID, "subscribed")
        await s._resync(MID)
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.42, 1)], [(0.45, 5)], seq=101)]))
        assert of_kind(events, "book")[-1].data["option"] == "Candidate A"

    @async_test
    async def test_public_context_resync_omits_tournament_id(self, client, fake):
        fake.add("GET", OB_PATH, ob(book_exchange("36", [(0.40, 10)], [(0.45, 5)], seq=None), tid=None))
        fake.add("GET", TAPE_36, tape("36", []))
        s, events = make_stream(client, ctx=PUBLIC)
        s.request_resync(MID, "gap")
        await s._resync(MID)
        assert fake.calls_to(OB_PATH)[0].params == {"depth": "200"}
        assert "tournamentId" not in fake.calls_to(TAPE_36)[0].params
        assert s.books.get("36").as_of is None
        assert len(of_kind(events, "book")) == 1

    @async_test
    async def test_failed_rest_read_is_logged_not_raised(self, client, fake):
        fake.add("GET", OB_PATH, (404, error_body("NOT_FOUND", "no such market")))
        s, events = make_stream(client)
        s.request_resync(MID, "gap")
        await s._resync(MID)
        assert s.resyncs == 0
        assert events == []
        assert fake.calls_to(TAPE_36) == []

    @async_test
    async def test_backfill_failure_on_one_exchange_continues_with_the_next(self, client, fake):
        fake.add("GET", OB_PATH, ob(book_exchange("36", [(0.4, 1)], [(0.5, 1)], seq=1), book_exchange("37", [(0.4, 1)], [(0.5, 1)], seq=1)))
        fake.add("GET", TAPE_36, (400, error_body("BAD_REQUEST")))
        fake.add("GET", "/exchanges/37/trades", tape("37", [rest_trade("70", 0.5, 1)]))
        s, events = make_stream(client)
        s.request_resync(MID, "gap")
        await s._resync(MID)
        assert [(t.data["id"], t.data["exchangeId"]) for t in of_kind(events, "trade")] == [("70", "37")]

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: _resync pops the pending reasons before the REST read; on failure a gap's resync/backfill is dropped for good",
    )
    @async_test
    async def test_failed_gap_resync_is_not_forgotten(self, client, fake):
        fake.add("GET", OB_PATH, OB_36, (404, error_body("NOT_FOUND", "transient")), OB_36)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("2", 0.43, 1)]))
        s, events = make_stream(client)
        s.request_resync(MID, "subscribed")
        await s._resync(MID)
        s.handle_market_batch(MID, batch(1, 0))
        s.handle_market_batch(MID, batch(5, 4))  # gap: trade 2 was missed
        await s._resync(MID)  # the REST read fails
        # The next successful resync (here a periodic one, book unchanged) must still
        # honour the unresolved gap and backfill the tape.
        s.request_resync(MID, "periodic")
        await s._resync(MID)
        assert [t.data["id"] for t in of_kind(events, "trade")] == ["2"]


class TestTimers:
    @async_test
    async def test_periodic_resyncs_are_staggered_across_markets(self, client):
        s, _ = make_stream(client, markets=("26", "27"), resync_interval=0.2, tick=0.01)
        loop = asyncio.get_running_loop()
        start = loop.time()
        task = asyncio.create_task(s._timers())
        await asyncio.sleep(0.01)
        assert s.next_periodic["27"] - s.next_periodic["26"] == pytest.approx(0.1, abs=0.02)
        assert s.next_periodic["26"] - start == pytest.approx(0.1, abs=0.05)
        await until(lambda: set(s._pending) == {"26", "27"}, what="periodic resyncs")
        assert s._pending == {"26": {"periodic"}, "27": {"periodic"}}
        s.stop()
        await task

    @async_test
    async def test_book_expiry_requests_one_resync(self, client):
        s, _ = make_stream(client, tick=0.01)
        past = iso(datetime.now(timezone.utc) - timedelta(seconds=1))
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.4, 1)], [(0.5, 1)], seq=1, expiry=past)]))
        assert s.books.get("36").next_expiry_at is not None
        task = asyncio.create_task(s._timers())
        await until(lambda: MID in s._pending, what="expiry resync")
        await asyncio.sleep(0.05)  # several more ticks
        s.stop()
        await task
        assert s._pending == {MID: {"expiry"}}
        assert queued(s) == [("resync", MID)]
        assert s.books.get("36").next_expiry_at is None  # one refetch per expiry

    @async_test
    async def test_future_expiry_does_not_resync(self, client):
        s, _ = make_stream(client, tick=0.01)
        future = iso(datetime.now(timezone.utc) + timedelta(hours=1))
        s.handle_market_batch(MID, batch(1, 0, books=[pushed_book(36, [(0.4, 1)], [(0.5, 1)], seq=1, expiry=future)]))
        task = asyncio.create_task(s._timers())
        await asyncio.sleep(0.05)
        s.stop()
        await task
        assert s._pending == {}

    @async_test
    async def test_token_refresh_is_queued_once_inside_margin(self, client):
        s, _ = make_stream(client, tick=0.01, token_refresh_margin=60)
        s._token_expires = datetime.now(timezone.utc) + timedelta(seconds=30)
        task = asyncio.create_task(s._timers())
        await asyncio.sleep(0.06)
        s.stop()
        await task
        assert queued(s) == [("refresh_token", None)]
        assert s._token_expires is None

    @async_test
    async def test_token_outside_margin_is_left_alone(self, client):
        s, _ = make_stream(client, tick=0.01, token_refresh_margin=60)
        s._token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        task = asyncio.create_task(s._timers())
        await asyncio.sleep(0.05)
        s.stop()
        await task
        assert queued(s) == []

    @async_test
    async def test_refresh_token_sets_auth_and_requests_resyncs(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("http://rt.invalid", token="jwt-2"))
        rt = FakeRealtime("http://rt.invalid", "anon", log=[])
        s, events = make_stream(client, markets=("26", "27"))
        s._rt = rt
        await s._refresh_token()
        assert rt.auth == ["jwt-2"]
        assert statuses(events) == ["token_refreshed"]
        assert s._pending == {"26": {"token_refresh"}, "27": {"token_refresh"}}
        assert s._token["token"] == "jwt-2"
        assert s._token_expires > datetime.now(timezone.utc) + timedelta(hours=2)

    @async_test
    async def test_failed_token_refresh_retries_later(self, client, fake):
        fake.add("POST", "/realtime/token", (403, error_body("FORBIDDEN")))
        rt = FakeRealtime("http://rt.invalid", "anon", log=[])
        s, events = make_stream(client, token_refresh_margin=600)
        s._rt = rt
        before = datetime.now(timezone.utc)
        await s._refresh_token()
        assert rt.auth == [] and events == [] and s._pending == {}
        # Next attempt is due ~60s from now (expiry is stored margin-adjusted).
        due = s._token_expires - timedelta(seconds=600)
        assert before + timedelta(seconds=59) <= due <= datetime.now(timezone.utc) + timedelta(seconds=61)

    def test_socket_alive_checks(self, client):
        async def body():
            s, _ = make_stream(client)
            assert s._socket_alive() is False  # no client
            rt = FakeRealtime("u", "k", log=[])
            s._rt = rt
            assert s._socket_alive() is False  # not connected
            rt.connected = True
            assert s._socket_alive() is True
            loop = asyncio.get_running_loop()
            rt._listen_task = loop.create_future()
            assert s._socket_alive() is True  # listener still running
            rt._listen_task.set_result(None)
            assert s._socket_alive() is False  # listener ended: socket is dead

        asyncio.run(body())


# --------------------------------------------------------------------------- fake realtime client (in-memory)


class FakeChannel:
    def __init__(self, rt: "FakeRealtime", topic: str, params: Any) -> None:
        self.rt = rt
        self.topic = topic
        self.params = params
        self.handlers: List[Tuple[str, Callable[[Any], None]]] = []
        self.state_cb: Optional[Callable[..., None]] = None

    def on_broadcast(self, event: str, cb: Callable[[Any], None]) -> "FakeChannel":
        self.handlers.append((event, cb))
        return self

    async def subscribe(self, cb: Optional[Callable[..., None]] = None) -> "FakeChannel":
        self.rt.log.append(("subscribe", self.topic))
        self.state_cb = cb
        if cb is not None:
            asyncio.get_running_loop().call_soon(cb, RealtimeSubscribeStates.SUBSCRIBED, None)
        return self

    def broadcast(self, event: str, payload: Any) -> None:
        for name, cb in self.handlers:
            if name == event:
                cb({"event": event, "payload": payload})


class FakeRealtime:
    def __init__(self, url: str, key: str, *, log: List[Any], fail_connect: bool = False) -> None:
        self.url = url
        self.key = key
        self.log = log
        self.fail_connect = fail_connect
        self.connected = False
        self.closed = False
        self.auth: List[str] = []
        self.channels: Dict[str, FakeChannel] = {}
        self._listen_task: Any = None

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.log.append(("connect", self.url))
        if self.fail_connect:
            raise ConnectionError("realtime unreachable")
        self.connected = True

    async def set_auth(self, token: str) -> None:
        self.log.append(("set_auth", token))
        self.auth.append(token)

    def channel(self, topic: str, params: Any = None) -> FakeChannel:
        self.log.append(("channel", topic))
        ch = FakeChannel(self, topic, params)
        self.channels[topic] = ch
        return ch

    async def close(self) -> None:
        self.log.append(("close", self.url))
        self.closed = True
        self.connected = False


class FakeFactory:
    def __init__(self, fail_on: Sequence[int] = ()) -> None:
        self.instances: List[FakeRealtime] = []
        self.args: List[Tuple[str, str]] = []
        self.fail_on = set(fail_on)
        self.log: List[Any] = []

    def __call__(self, url: str, key: str) -> FakeRealtime:
        rt = FakeRealtime(url, key, log=self.log, fail_connect=len(self.instances) in self.fail_on)
        self.args.append((url, key))
        self.instances.append(rt)
        return rt


class TestRunWithFakeRealtime:
    @async_test
    async def test_connect_authorizes_before_subscribing_and_resyncs(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, OB_36)
        factory = FakeFactory()
        s, events = make_stream(client, realtime_factory=factory)
        task = asyncio.create_task(s.run())
        await until(lambda: of_kind(events, "resync"), what="initial resync")
        assert factory.args == [("https://rt.example.test", "anon")]
        assert factory.log[:3] == [("connect", "https://rt.example.test"), ("set_auth", "jwt-token"), ("channel", TOPIC)]
        ch = factory.instances[0].channels[TOPIC]
        assert ch.params == channel_params()
        assert sorted(name for name, _ in ch.handlers) == ["book_dirty", "market_batch", "market_settled"]
        assert {"status": "SUBSCRIBED", "error": None} in [e.data for e in of_kind(events, "status")]
        assert resync_reasons(events) == [["subscribed"]]
        # Broadcasts flow through the queue to the handlers.
        ch.broadcast("market_batch", batch(1, 0, trades=[trade("1", 1)]))
        ch.broadcast("market_settled", {"marketId": MID, "settledWith": "A"})
        await until(lambda: of_kind(events, "settled"), what="settled event")
        assert [e.data["id"] for e in of_kind(events, "trade")] == ["1"]
        s.stop()
        await asyncio.wait_for(task, 2)
        assert factory.instances[0].closed and s._rt is None

    @pytest.mark.parametrize("failure", ["disconnected", "listener_finished"])
    @async_test
    async def test_dead_socket_reconnects_after_grace_period(self, client, fake, failure):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, OB_36)
        fake.add("GET", TAPE_36, tape("36", [rest_trade("5", 0.4, 1)]))
        factory = FakeFactory()
        s, events = make_stream(client, realtime_factory=factory, reconnect_after=0.1, tick=0.02)
        task = asyncio.create_task(s.run())
        await until(lambda: of_kind(events, "resync"), what="initial resync")
        first = factory.instances[0]
        if failure == "disconnected":
            first.connected = False
        else:
            done = asyncio.get_running_loop().create_future()
            done.set_result(None)
            first._listen_task = done
        await until(lambda: any("reconnect" in r for r in resync_reasons(events)), what="reconnect resync")
        assert "reconnecting" in statuses(events)
        assert first.closed
        assert len(factory.instances) == 2
        second = factory.instances[1]
        assert second.auth == ["jwt-token"] and TOPIC in second.channels  # token reused, channel rejoined
        assert len(fake.calls_to("/realtime/token")) == 1
        # The rejoin's SUBSCRIBED may land in the same resync or a following one.
        await until(lambda: {"reconnect", "resubscribed"} <= reasons_after_first(events), what="reconnect + resubscribed")
        await until(lambda: fake.calls_to(TAPE_36), what="trade backfill")
        await until(lambda: of_kind(events, "trade"), what="backfilled trade")
        s.stop()
        await asyncio.wait_for(task, 2)
        assert second.closed

    @async_test
    async def test_short_blip_does_not_reconnect(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, OB_36)
        factory = FakeFactory()
        s, events = make_stream(client, realtime_factory=factory, reconnect_after=0.3, tick=0.02)
        task = asyncio.create_task(s.run())
        await until(lambda: of_kind(events, "resync"), what="initial resync")
        factory.instances[0].connected = False
        await asyncio.sleep(0.1)
        factory.instances[0].connected = True
        await asyncio.sleep(0.35)
        s.stop()
        await asyncio.wait_for(task, 2)
        assert len(factory.instances) == 1
        assert "reconnecting" not in statuses(events)

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: a failed reconnect leaves _rt None, and _timers only checks liveness when _rt is set, so it never retries",
    )
    @async_test
    async def test_failed_reconnect_is_retried(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, OB_36)
        fake.add("GET", TAPE_36, tape("36", []))
        factory = FakeFactory(fail_on=[1])  # the first reconnect attempt fails
        s, events = make_stream(client, realtime_factory=factory, reconnect_after=0.05, tick=0.01)
        task = asyncio.create_task(s.run())
        try:
            await until(lambda: of_kind(events, "resync"), what="initial resync")
            factory.instances[0].connected = False
            await until(lambda: len(factory.instances) >= 2, what="first reconnect attempt")
            await until(lambda: len(factory.instances) >= 3, timeout=0.8, what="a retry after the failed reconnect")
        finally:
            s.stop()
            await asyncio.wait_for(task, 2)

    @async_test
    async def test_run_duration_returns_and_closes(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, OB_36)
        factory = FakeFactory()
        s, _ = make_stream(client, realtime_factory=factory)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.wait_for(s.run(duration=0.2), 2)
        assert 0.15 <= loop.time() - t0 < 1.5
        assert factory.instances[0].closed

    @async_test
    async def test_handler_errors_do_not_stop_the_loop(self, client, fake):
        fake.add("POST", "/realtime/token", token_body("https://rt.example.test"))
        fake.add("GET", OB_PATH, {"exchanges": "not-a-list-of-books", "contexts": "garbage"}, OB_36)
        factory = FakeFactory()
        s, events = make_stream(client, realtime_factory=factory)
        task = asyncio.create_task(s.run())
        await until(lambda: len(fake.calls_to(OB_PATH)) == 1, what="first resync attempt")
        ch = factory.instances[0].channels[TOPIC]
        ch.broadcast("market_batch", batch(1, 0, trades=[trade("1", 1)]))
        await until(lambda: of_kind(events, "trade"), what="trade after a failing resync")
        s.stop()
        await asyncio.wait_for(task, 2)


# --------------------------------------------------------------------------- Part 3: real websocket


class PhoenixServer:
    """Minimal Supabase Realtime (Phoenix v1 JSON) server for the realtime-py client."""

    def __init__(self, join_status: str = "ok") -> None:
        self.join_status = join_status
        self.requests: List[Tuple[str, Dict[str, List[str]]]] = []
        self.received: List[Dict[str, Any]] = []
        self.connections: List[Any] = []
        self.port: Optional[int] = None

    def events(self, name: str) -> List[Dict[str, Any]]:
        return [m for m in self.received if m.get("event") == name]

    @property
    def joins(self) -> List[Dict[str, Any]]:
        return self.events("phx_join")

    async def handler(self, ws: Any) -> None:
        parts = urlsplit(ws.request.path)
        self.requests.append((parts.path, parse_qs(parts.query)))
        if parts.path != "/realtime/v1/websocket":
            await ws.close(1008, "unknown path")
            return
        self.connections.append(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                event = msg.get("event")
                if event == "phx_join":
                    if self.join_status == "ok":
                        reply = {"status": "ok", "response": {"postgres_changes": []}}
                    else:
                        reply = {"status": "error", "response": {"reason": "Unauthorized"}}
                    await ws.send(json.dumps({"event": "phx_reply", "topic": msg["topic"], "ref": msg["ref"], "join_ref": msg["ref"], "payload": reply}))
                elif event == "heartbeat":
                    await ws.send(json.dumps({"event": "phx_reply", "topic": "phoenix", "ref": msg.get("ref"), "payload": {"status": "ok", "response": {}}}))
                elif event in ("access_token", "phx_leave"):
                    await ws.send(
                        json.dumps({"event": "phx_reply", "topic": msg["topic"], "ref": msg["ref"], "join_ref": msg.get("join_ref"), "payload": {"status": "ok", "response": {}}})
                    )
        except ConnectionClosed:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)

    async def broadcast(self, topic: str, event: str, payload: Dict[str, Any]) -> None:
        msg = json.dumps({"event": "broadcast", "topic": f"realtime:{topic}", "ref": None, "payload": {"event": event, "type": "broadcast", "payload": payload}})
        for ws in list(self.connections):
            await ws.send(msg)

    async def drop_all(self, code: int = 1000) -> None:
        for ws in list(self.connections):
            await ws.close(code)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Serving:
    def __init__(self, server: PhoenixServer) -> None:
        self.server = server
        self._cm: Any = None

    async def __aenter__(self) -> PhoenixServer:
        self._cm = serve(self.server.handler, "127.0.0.1", 0)
        ws_server = await self._cm.__aenter__()
        self.server.port = ws_server.sockets[0].getsockname()[1]
        return self.server

    async def __aexit__(self, *exc: Any) -> None:
        await self._cm.__aexit__(*exc)


@pytest.fixture(autouse=True)
def _no_proxy_for_localhost(monkeypatch):
    # websockets honours proxy env vars; make sure the loopback server is reached directly.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


class TestRealWebsocket:
    @async_test
    async def test_live_stream_join_batches_gap_and_stop(self, client, fake):
        async with _Serving(PhoenixServer()) as server:
            fake.add("POST", "/realtime/token", token_body(server.url))
            fake.add("GET", OB_PATH, OB_36, ob(book_exchange("36", [(0.42, 2)], [(0.45, 5)], seq=1004)))
            fake.add("GET", TAPE_36, tape("36", [rest_trade("1004", 0.42, 2), rest_trade("1003", 0.42, 1), rest_trade("1002", 0.41, 1), rest_trade("1001", 0.41, 3)]))
            s, events = make_stream(client, tick=0.05)
            task = asyncio.create_task(s.run())

            await until(lambda: of_kind(events, "resync"), what="resync after SUBSCRIBED")
            assert server.requests[0][0] == "/realtime/v1/websocket"
            assert server.requests[0][1]["apikey"] == ["anon"]
            (join,) = server.joins
            assert join["topic"] == f"realtime:{TOPIC}"
            assert join["payload"]["config"]["private"] is True
            assert join["payload"]["access_token"] == "jwt-token"
            assert resync_reasons(events) == [["subscribed"]]
            assert fake.calls_to(OB_PATH)[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "200"}
            assert fake.calls_to(TAPE_36) == []
            assert [e.data["source"] for e in of_kind(events, "book")] == ["rest"]

            await server.broadcast(
                TOPIC,
                "market_batch",
                batch(1, 0, trades=[trade("1001", 1001, px=0.41, qty=3)], books=[pushed_book(36, [(0.41, 3)], [(0.45, 5)], seq=1001)]),
            )
            await until(lambda: of_kind(events, "trade"), what="streamed trade")
            await until(lambda: len(of_kind(events, "book")) == 2, what="pushed book event")
            assert of_kind(events, "book")[-1].data["bestBid"] == 0.41
            assert of_kind(events, "book")[-1].data["source"] == "push"

            # Revisions 2-3 never arrive: the next batch reveals the gap.
            await server.broadcast(TOPIC, "market_batch", batch(4, 3, trades=[trade("1004", 1004, px=0.42, qty=2)]))
            await until(lambda: len(of_kind(events, "resync")) == 2, what="gap resync")
            await until(lambda: len(of_kind(events, "trade")) == 4, what="backfilled trades")
            assert resync_reasons(events)[1] == ["gap"]
            (backfill,) = fake.calls_to(TAPE_36)
            assert backfill.params["from"] == iso(s.started_at)
            trades = of_kind(events, "trade")
            assert [(t.data["id"], t.data.get("source")) for t in trades] == [("1001", None), ("1004", None), ("1002", "rest"), ("1003", "rest")]

            s.stop()
            await asyncio.wait_for(task, 2)
            assert s._rt is None
            await until(lambda: not server.connections, what="socket closed")

    @async_test
    async def test_run_duration_on_public_topic(self, client, fake):
        async with _Serving(PhoenixServer()) as server:
            fake.add("POST", "/realtime/token", token_body(server.url))
            fake.add("GET", OB_PATH, ob(book_exchange("36", [(0.40, 10)], [(0.45, 5)], seq=None), tid=None))
            s, events = make_stream(client, ctx=PUBLIC, tick=0.05)
            await asyncio.wait_for(s.run(duration=0.4), 3)
            assert [j["topic"] for j in server.joins] == [f"realtime:market:{MID}:combined"]
            assert server.joins[0]["payload"]["config"]["private"] is True
            assert fake.calls_to(OB_PATH)[0].params == {"depth": "200"}
            assert resync_reasons(events) == [["subscribed"]]
            await until(lambda: not server.connections, what="socket closed")

    @async_test
    async def test_token_refresh_reauthorizes_joined_channels(self, client, fake):
        async with _Serving(PhoenixServer()) as server:
            # First token is inside the refresh margin ~0.4s after start; the second is not.
            fake.add("POST", "/realtime/token", token_body(server.url, "jwt-token", expires_in=10), token_body(server.url, "jwt-token-2"))
            fake.add("GET", OB_PATH, OB_36)
            s, events = make_stream(client, tick=0.05, token_refresh_margin=9.6)
            task = asyncio.create_task(s.run())
            await until(lambda: server.events("access_token"), timeout=3, what="access_token push")
            (push,) = server.events("access_token")
            assert push["topic"] == f"realtime:{TOPIC}"
            assert push["payload"] == {"access_token": "jwt-token-2"}
            await until(lambda: any("token_refresh" in r for r in resync_reasons(events)), what="token_refresh resync")
            assert "token_refreshed" in statuses(events)
            assert len(fake.calls_to("/realtime/token", "POST")) == 2
            await asyncio.sleep(0.15)
            assert len(fake.calls_to("/realtime/token", "POST")) == 2  # new token is outside the margin
            assert fake.calls_to(TAPE_36) == []  # a token refresh resyncs books only
            s.stop()
            await asyncio.wait_for(task, 2)

    @async_test
    async def test_server_closing_socket_triggers_reconnect_and_rejoin(self, client, fake):
        async with _Serving(PhoenixServer()) as server:
            fake.add("POST", "/realtime/token", token_body(server.url))
            fake.add("GET", OB_PATH, OB_36)
            fake.add("GET", TAPE_36, tape("36", []))
            s, events = make_stream(client, tick=0.02, reconnect_after=0.1)
            task = asyncio.create_task(s.run())
            await until(lambda: of_kind(events, "resync"), what="initial resync")
            await server.drop_all(1000)
            await until(lambda: len(server.joins) == 2, timeout=3, what="rejoin on a new socket")
            assert len(server.requests) == 2
            assert server.joins[1]["payload"]["access_token"] == "jwt-token"
            await until(lambda: fake.calls_to(TAPE_36), what="backfill after reconnect")
            assert "reconnecting" in statuses(events)
            await until(lambda: {"reconnect", "resubscribed"} <= reasons_after_first(events), what="reconnect + resubscribed")
            s.stop()
            await asyncio.wait_for(task, 2)

    @async_test
    async def test_rejected_join_is_reported_and_not_resynced(self, client, fake):
        async with _Serving(PhoenixServer(join_status="error")) as server:
            fake.add("POST", "/realtime/token", token_body(server.url))
            s, events = make_stream(client, tick=0.05)
            task = asyncio.create_task(s.run())
            await until(lambda: "CHANNEL_ERROR" in statuses(events), what="channel error status")
            (err,) = [e for e in of_kind(events, "status") if e.data["status"] == "CHANNEL_ERROR"]
            assert err.market_id == MID and "Unauthorized" in err.data["error"]
            await asyncio.sleep(0.1)
            assert fake.calls_to(OB_PATH) == []
            s.stop()
            await asyncio.wait_for(task, 2)
