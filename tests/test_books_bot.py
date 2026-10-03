"""Tests for supermarket_bot/books.py (order-book model) and supermarket_bot/bot.py
(context discovery, snapshots, watch loop, scan, formatting).

Everything is offline: REST goes through the FakeAPI MockTransport from conftest and
time is faked (FakeClock / Sleeper), so nothing really sleeps.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest

from conftest import (
    TOURNAMENT_ID,
    TOURNAMENT_SLUG,
    FakeClock,
    Sleeper,
    book_exchange,
    error_body,
    market,
    market_orderbook,
    market_page,
    price,
    tournament,
    tournament_page,
)

from supermarket_bot.books import (
    Book,
    BookStore,
    BookVersion,
    Level,
    books_from_market_orderbook,
    format_level,
    is_newer,
    parse_time,
    select_context,
)
from supermarket_bot.bot import (
    ROW_FIELDS,
    Context,
    ContextError,
    MarketDataBot,
    Snapshot,
    SnapshotWriter,
    build_rows,
    diff_rows,
    iso,
    market_rows,
    price_table,
    resolve_context,
)
from supermarket_bot.errors import SuperMarketError

UTC = timezone.utc
OTHER_TID = "11111111-2222-3333-4444-555555555555"
MARKETS_PATH = f"/tournaments/{TOURNAMENT_SLUG}/markets"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def v(seq: int, at: Optional[str]) -> BookVersion:
    version = BookVersion.parse({"sequence": seq, "at": at})
    assert version is not None
    return version


def tournament_ctx() -> Context:
    return Context.from_tournament(tournament())


# =========================================================================== books.py
# --------------------------------------------------------------------------- parse_time


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-10-03T12:00:00Z", utc(2026, 10, 3, 12)),
        ("2026-10-03T12:00:00z", utc(2026, 10, 3, 12)),
        ("2026-10-03T12:00:00+00:00", utc(2026, 10, 3, 12)),
        ("2026-10-03T14:30:00+02:30", utc(2026, 10, 3, 12)),
        ("2026-10-03T07:00:00-05:00", utc(2026, 10, 3, 12)),
        ("  2026-10-03T12:00:00Z \n", utc(2026, 10, 3, 12)),
        # naive timestamps are taken as UTC
        ("2026-10-03T12:00:00", utc(2026, 10, 3, 12)),
        ("2026-10-03 12:00:00", utc(2026, 10, 3, 12)),
    ],
)
def test_parse_time_offsets_and_naive(text: str, expected: datetime) -> None:
    parsed = parse_time(text)
    assert parsed == expected
    assert parsed.tzinfo == UTC
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "fraction, micros",
    [
        ("1", 100000),
        ("12", 120000),
        ("125", 125000),
        ("1234", 123400),
        ("12345", 123450),
        ("123456", 123456),
        ("1234567", 123456),
        ("12345678", 123456),
        ("123456789", 123456),
    ],
)
@pytest.mark.parametrize("suffix", ["Z", "+00:00"])
def test_parse_time_fraction_lengths(fraction: str, micros: int, suffix: str) -> None:
    parsed = parse_time(f"2026-09-28T09:15:02.{fraction}{suffix}")
    assert parsed == utc(2026, 9, 28, 9, 15, 2, micros)


def test_parse_time_fraction_with_non_utc_offset_converted_to_utc() -> None:
    assert parse_time("2026-09-28T11:15:02.125+02:00") == utc(2026, 9, 28, 9, 15, 2, 125000)


@pytest.mark.parametrize("garbage", [None, "", "   ", "not a time", "2026-13-45T99:00:00Z", "yesterday", 1727950000, 17.5, {}, []])
def test_parse_time_garbage_returns_none(garbage: Any) -> None:
    assert parse_time(garbage) is None


# --------------------------------------------------------------------------- BookVersion


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "seq 5",
        [5, "2026-10-03T12:00:00Z"],
        {},
        {"at": "2026-10-03T12:00:00Z"},
        {"sequence": None, "at": "2026-10-03T12:00:00Z"},
        {"sequence": True, "at": "2026-10-03T12:00:00Z"},
        {"sequence": False},
        {"sequence": "4817", "at": "2026-10-03T12:00:00Z"},
    ],
)
def test_book_version_parse_rejects_unversioned(raw: Any) -> None:
    assert BookVersion.parse(raw) is None


def test_book_version_parse_valid() -> None:
    version = BookVersion.parse({"sequence": 4817, "at": "2026-09-28T09:15:02.125+00:00"})
    assert version == BookVersion(4817, utc(2026, 9, 28, 9, 15, 2, 125000))
    assert str(version) == "seq 4817"
    # a float sequence is accepted as an integer; a missing/garbage "at" leaves at=None
    assert BookVersion.parse({"sequence": 12.0, "at": "garbage"}) == BookVersion(12, None)
    assert BookVersion.parse({"sequence": 0}) == BookVersion(0, None)


def test_versions_compare_by_sequence_first() -> None:
    later_seq_earlier_clock = v(101, "2026-10-03T11:00:00Z")
    earlier_seq_later_clock = v(100, "2026-10-03T13:00:00Z")
    assert is_newer(later_seq_earlier_clock, earlier_seq_later_clock)
    assert not is_newer(earlier_seq_later_clock, later_seq_earlier_clock)


def test_versions_with_same_sequence_compare_by_at() -> None:
    assert is_newer(v(100, "2026-10-03T12:00:00.002Z"), v(100, "2026-10-03T12:00:00.001Z"))
    assert not is_newer(v(100, "2026-10-03T12:00:00.001Z"), v(100, "2026-10-03T12:00:00.002Z"))


def test_same_instant_in_different_offsets_is_not_newer() -> None:
    a = v(100, "2026-10-03T12:00:00.000+00:00")
    b = v(100, "2026-10-03T14:00:00.000+02:00")
    assert a == b
    assert not is_newer(a, b)
    assert not is_newer(b, a)


def test_at_compared_as_instant_not_as_text() -> None:
    # "13:00+02:00" is 11:00Z: lexically larger, but an earlier instant than "12:00Z".
    earlier = v(100, "2026-10-03T13:00:00+02:00")
    later = v(100, "2026-10-03T12:00:00Z")
    assert is_newer(later, earlier)
    assert not is_newer(earlier, later)


def test_version_without_at_is_older_than_same_sequence_with_at() -> None:
    no_clock = v(100, None)
    with_clock = v(100, "2026-10-03T12:00:00Z")
    assert is_newer(with_clock, no_clock)
    assert not is_newer(no_clock, with_clock)
    assert not is_newer(no_clock, v(100, None))


def test_is_newer_null_handling() -> None:
    held = v(100, "2026-10-03T12:00:00Z")
    # A held book with a null asOf is replaced by anything.
    assert is_newer(held, None)
    assert is_newer(None, None)
    # An unversioned candidate never replaces a versioned book.
    assert not is_newer(None, held)
    # An identical version is not newer.
    assert not is_newer(v(100, "2026-10-03T12:00:00Z"), held)


# --------------------------------------------------------------------------- levels / Book


def test_levels_skip_malformed_and_sort() -> None:
    raw = {
        "exchangeId": "36",
        "bids": [
            {"price": 0.40, "quantity": 10},
            "garbage",
            None,
            {"price": "0.45", "quantity": 5},
            {"price": True, "quantity": 5},
            {"quantity": 5},
            {"price": 0.42, "quantity": None},
            {"price": 0.44, "quantity": 7},
            {"price": 0.41, "quantity": 3},
        ],
        "asks": [
            {"price": 0.60, "quantity": 1},
            {"price": 0.55, "quantity": 2},
            {"price": 0.58, "quantity": "lots"},
            {"price": 0.57, "quantity": 4},
        ],
    }
    book = Book.from_payload(raw)
    assert book.bids == [Level(0.44, 7.0), Level(0.41, 3.0), Level(0.40, 10.0)]
    assert book.asks == [Level(0.55, 2.0), Level(0.57, 4.0), Level(0.60, 1.0)]
    assert all(isinstance(lv.price, float) and isinstance(lv.quantity, float) for lv in book.bids + book.asks)


@pytest.mark.xfail(strict=True, reason="BUG: _levels rejects bool prices but accepts a bool quantity as 1.0")
def test_levels_skip_bool_quantity() -> None:
    book = Book.from_payload({"exchangeId": "36", "bids": [{"price": 0.4, "quantity": True}], "asks": []})
    assert book.bids == []


def test_levels_missing_or_null_sides_are_empty() -> None:
    book = Book.from_payload({"exchangeId": "36", "bids": None})
    assert book.bids == [] and book.asks == []
    assert book.best_bid is None and book.best_ask is None
    assert book.mid is None and book.spread is None
    assert book.top() == (None, None)


def test_from_payload_exchange_orderbook_response() -> None:
    # GET /exchanges/{id}/orderbook
    raw = {
        "exchangeId": "36",
        "marketId": "26",
        "asOf": {"sequence": 4817, "at": "2026-09-28T09:15:02.125+00:00"},
        "depth": 20,
        "bids": [{"price": 0.39, "quantity": 50}, {"price": 0.41, "quantity": 100}],
        "asks": [{"price": 0.60, "quantity": 10}, {"price": 0.55, "quantity": 300}],
        "bestBid": 0.41,
        "bestAsk": 0.55,
        "spread": 0.14,
    }
    book = Book.from_payload(raw, market_id="ignored-because-payload-has-one")
    assert book.exchange_id == "36"
    assert book.market_id == "26"
    assert book.option is None
    assert book.as_of == BookVersion(4817, utc(2026, 9, 28, 9, 15, 2, 125000))
    assert book.next_expiry_at is None
    assert book.best_bid == Level(0.41, 100.0)
    assert book.best_ask == Level(0.55, 300.0)
    assert book.spread == 0.14
    assert book.mid == 0.48


def test_from_payload_market_orderbook_exchange_item() -> None:
    # one exchanges[] item of GET /markets/{id}/orderbook: no marketId, has option
    raw = book_exchange("37", bids=[(0.30, 5)], asks=[(0.35, 8)], seq=None, option="Candidate B")
    book = Book.from_payload(raw, market_id="26")
    assert book.exchange_id == "37"
    assert book.option == "Candidate B"
    assert book.market_id == "26"
    assert book.as_of is None
    assert book.top() == (Level(0.30, 5.0), Level(0.35, 8.0))


def test_from_payload_realtime_pushed_book() -> None:
    # pushed books: numeric exchangeId, nextExpiryAt, no option/marketId
    raw = {
        "exchangeId": 36,
        "asOf": {"sequence": 4820, "at": "2026-09-28T09:15:03.5+00:00"},
        "nextExpiryAt": "2026-09-28T10:00:00Z",
        "bids": [{"price": 0.4, "quantity": 1}],
        "asks": [{"price": 0.5, "quantity": 2}],
    }
    book = Book.from_payload(raw, market_id="26")
    assert book.exchange_id == "36"
    assert book.market_id == "26"
    assert book.option is None
    assert book.as_of == BookVersion(4820, utc(2026, 9, 28, 9, 15, 3, 500000))
    assert book.next_expiry_at == utc(2026, 9, 28, 10)


def test_from_payload_id_fallback_and_numeric_market_id() -> None:
    book = Book.from_payload({"id": 99, "marketId": 26, "nextExpiryAt": None})
    assert book.exchange_id == "99"
    assert book.market_id == "26"
    assert book.next_expiry_at is None


def test_mid_and_spread_are_rounded() -> None:
    book = Book("1", bids=[Level(0.1, 1)], asks=[Level(0.3, 1)])
    # 0.3 - 0.1 == 0.19999999999999998 in floating point
    assert book.spread == 0.2
    assert book.mid == 0.2
    one_sided = Book("1", bids=[Level(0.1, 1)])
    assert one_sided.spread is None and one_sided.mid is None


def test_format_level() -> None:
    assert format_level(None) == "—"
    assert format_level(Level(0.415, 300.0)) == "0.415 x 300"
    assert format_level(Level(0.5, 12.5)) == "0.500 x 12.5"


# --------------------------------------------------------------------------- BookStore


def mkbook(eid: str = "36", seq: Optional[int] = 100, at: str = "2026-10-03T12:00:00Z", bid: float = 0.4, **kw: Any) -> Book:
    as_of = None if seq is None else v(seq, at)
    return Book(eid, bids=[Level(bid, 1)], asks=[Level(0.6, 1)], as_of=as_of, **kw)


def test_store_first_pushed_book_applies() -> None:
    store = BookStore()
    book = mkbook()
    assert store.apply_pushed(book) is True
    assert store.get("36") is book
    assert store.get(36) is book  # ids are normalised to strings
    assert store.get("37") is None


def test_store_pushed_older_or_equal_ignored_newer_applied() -> None:
    store = BookStore()
    held = mkbook(seq=100, bid=0.40)
    store.apply_pushed(held)

    assert store.apply_pushed(mkbook(seq=99, at="2026-10-03T13:00:00Z", bid=0.10)) is False
    assert store.apply_pushed(mkbook(seq=100, bid=0.20)) is False  # equal version: duplicate
    assert store.apply_pushed(mkbook(seq=100, at="2026-10-03T14:00:00+02:00", bid=0.20)) is False  # same instant
    assert store.get("36") is held

    newer = mkbook(seq=100, at="2026-10-03T12:00:00.001Z", bid=0.45)
    assert store.apply_pushed(newer) is True
    assert store.get("36") is newer
    newest = mkbook(seq=101, at="2026-10-03T11:00:00Z", bid=0.46)
    assert store.apply_pushed(newest) is True
    assert store.get("36").best_bid == Level(0.46, 1.0)


def test_store_held_null_asof_replaced_by_any_pushed() -> None:
    store = BookStore()
    store.apply_rest(mkbook(seq=None, bid=0.30))
    pushed = mkbook(seq=1, at="2000-01-01T00:00:00Z", bid=0.31)
    assert store.apply_pushed(pushed) is True
    assert store.get("36") is pushed


def test_store_unversioned_push_never_replaces_versioned_book() -> None:
    store = BookStore()
    held = mkbook(seq=5)
    store.apply_pushed(held)
    assert store.apply_pushed(mkbook(seq=None, bid=0.1)) is False
    assert store.get("36") is held


def test_store_rest_applies_unless_held_strictly_newer() -> None:
    store = BookStore()
    store.apply_pushed(mkbook(seq=200))
    # held is strictly newer: a stale REST read (in flight while a push landed) loses
    assert store.apply_rest(mkbook(seq=199, at="2026-10-03T23:00:00Z", bid=0.1)) is False
    assert store.get("36").as_of.sequence == 200
    # an equal version is an authoritative resync and applies
    equal = mkbook(seq=200, bid=0.33)
    assert store.apply_rest(equal) is True
    assert store.get("36") is equal
    # a newer REST read applies
    assert store.apply_rest(mkbook(seq=201, bid=0.34)) is True
    assert store.get("36").best_bid.price == 0.34


def test_store_rest_with_null_asof_applies() -> None:
    store = BookStore()
    store.apply_pushed(mkbook(seq=500))
    unversioned = mkbook(seq=None, bid=0.25)
    assert store.apply_rest(unversioned) is True
    assert store.get("36") is unversioned
    # and a REST read over a held null-asOf book applies too
    assert store.apply_rest(mkbook(seq=1, bid=0.26)) is True


def test_store_carries_option_and_market_id_over() -> None:
    store = BookStore()
    store.apply_rest(mkbook(seq=10, option="YES", market_id="26"))
    pushed = mkbook(seq=11)  # pushed books carry neither option nor marketId
    assert pushed.option is None and pushed.market_id is None
    assert store.apply_pushed(pushed)
    held = store.get("36")
    assert (held.option, held.market_id) == ("YES", "26")
    # values on the incoming book win over the held ones
    store.apply_rest(mkbook(seq=12, option="Candidate A", market_id="27"))
    assert (store.get("36").option, store.get("36").market_id) == ("Candidate A", "27")


def test_store_expired() -> None:
    store = BookStore()
    now = utc(2026, 10, 3, 12)
    store.apply_pushed(mkbook("1", next_expiry_at=now - timedelta(seconds=1)))
    store.apply_pushed(mkbook("2", next_expiry_at=now))
    store.apply_pushed(mkbook("3", next_expiry_at=now + timedelta(seconds=1)))
    store.apply_pushed(mkbook("4", next_expiry_at=None))
    assert sorted(store.expired(now)) == ["1", "2"]
    assert store.expired(now - timedelta(hours=1)) == []


# --------------------------------------------------------------------------- contexts


def two_context_orderbook() -> Dict[str, Any]:
    """Market orderbook whose legacy top-level mirrors OTHER_TID; our tournament is contexts[1]."""
    theirs = {
        "exchanges": [book_exchange("36", [(0.10, 1)], [(0.90, 1)], seq=1), book_exchange("37", [], [], seq=1)],
        "overround": 1.20,
        "hasArbitrageOpportunity": False,
    }
    ours = {
        "exchanges": [
            book_exchange("36", [(0.40, 5), (0.42, 3)], [(0.47, 2)], seq=900, option="A"),
            book_exchange("37", [(0.45, 1)], [(0.48, 1), (0.50, 9)], seq=901, option="B"),
        ],
        "overround": 0.95,
        "hasArbitrageOpportunity": True,
    }
    return {
        **theirs,
        "contexts": [
            {"type": "tournament", "tournament": {"id": OTHER_TID, "slug": "other"}, "orderbook": theirs},
            {"type": "tournament", "tournament": {"id": TOURNAMENT_ID, "slug": TOURNAMENT_SLUG}, "orderbook": ours},
        ],
    }


def test_select_context_matches_tournament() -> None:
    resp = two_context_orderbook()
    assert select_context(resp, TOURNAMENT_ID)["overround"] == 0.95
    assert select_context(resp, OTHER_TID)["overround"] == 1.20


def test_select_context_public() -> None:
    resp = market_orderbook([book_exchange("36", [], [])], overround=1.03, tid=None)
    resp["overround"] = 9.9  # make sure the context, not the legacy field, is used
    assert select_context(resp, None)["overround"] == 1.03


def test_select_context_falls_back_to_top_level() -> None:
    no_contexts = {"exchanges": [], "overround": 1.1, "hasArbitrageOpportunity": False}
    assert select_context(no_contexts, TOURNAMENT_ID) is no_contexts
    assert select_context({**no_contexts, "contexts": None}, None)["overround"] == 1.1
    resp = two_context_orderbook()
    assert select_context(resp, "99999999-0000-0000-0000-000000000000") is resp
    # no public context among tournament-only contexts -> top level
    assert select_context(resp, None) is resp


def test_books_from_market_orderbook_tournament_context() -> None:
    books, ob = books_from_market_orderbook(two_context_orderbook(), TOURNAMENT_ID, market_id="26")
    assert ob["overround"] == 0.95 and ob["hasArbitrageOpportunity"] is True
    assert [b.exchange_id for b in books] == ["36", "37"]
    assert [b.option for b in books] == ["A", "B"]
    assert all(b.market_id == "26" for b in books)
    assert books[0].bids == [Level(0.42, 3.0), Level(0.40, 5.0)]
    assert books[1].asks == [Level(0.48, 1.0), Level(0.50, 9.0)]
    assert [b.as_of.sequence for b in books] == [900, 901]


def test_books_from_market_orderbook_public_and_no_contexts() -> None:
    public = market_orderbook([book_exchange("36", [(0.2, 1)], [(0.3, 1)], seq=7)], tid=None)
    books, ob = books_from_market_orderbook(public, None)
    assert [b.exchange_id for b in books] == ["36"] and books[0].market_id is None
    assert ob is public["contexts"][0]["orderbook"]

    legacy = {"exchanges": [book_exchange("40", [], [(0.7, 1)])], "overround": None, "hasArbitrageOpportunity": False}
    books, ob = books_from_market_orderbook(legacy, TOURNAMENT_ID, market_id="30")
    assert ob is legacy
    assert [(b.exchange_id, b.market_id) for b in books] == [("40", "30")]


def test_books_from_market_orderbook_context_without_exchanges_uses_top_level() -> None:
    resp = {
        "exchanges": [book_exchange("36", [], [])],
        "contexts": [{"type": "tournament", "tournament": {"id": TOURNAMENT_ID}}],  # no orderbook/exchanges
    }
    books, ob = books_from_market_orderbook(resp, TOURNAMENT_ID)
    assert ob is resp
    assert [b.exchange_id for b in books] == ["36"]


# =========================================================================== bot.py
# --------------------------------------------------------------------------- resolve_context


def tournaments_route(active: List[Dict[str, Any]], every: List[Dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        items = active if request.url.params.get("status") == "active" else every
        return httpx.Response(200, json=tournament_page(items))

    return handler


def test_resolve_context_explicit_slug(fake, client) -> None:
    fake.add("GET", "/tournaments/my cup", tournament(slug="my cup", name="My Cup"))
    ctx = resolve_context(client, slug="my cup")
    assert ctx == Context(TOURNAMENT_ID, "my cup", "My Cup", "SIG Coins", "active")
    assert [c.path for c in fake.calls] == ["/tournaments/my cup"]
    assert "/tournaments/my%20cup" in str(fake.calls[0].request.url)  # slug is path-quoted


def test_resolve_context_public_makes_no_request(fake, client) -> None:
    ctx = resolve_context(client, public=True)
    assert ctx == Context.public()
    assert ctx.is_public and ctx.label == "public" and ctx.tournament_id is None
    assert fake.calls == []


def test_resolve_context_single_active(fake, client) -> None:
    fake.add("GET", "/tournaments", tournaments_route([tournament()], []))
    ctx = resolve_context(client)
    assert ctx.tournament_id == TOURNAMENT_ID and ctx.slug == TOURNAMENT_SLUG
    assert ctx.currency == "SIG Coins" and ctx.status == "active"
    assert len(fake.calls) == 1
    assert fake.calls[0].params["status"] == "active"


def test_resolve_context_several_active_raises_listing_slugs(fake, client) -> None:
    active = [tournament(), tournament(slug="fall-classic", tid=OTHER_TID, name="Fall Classic")]
    fake.add("GET", "/tournaments", tournaments_route(active, active))
    with pytest.raises(ContextError) as info:
        resolve_context(client)
    msg = str(info.value)
    assert "2 active tournaments" in msg
    assert TOURNAMENT_SLUG in msg and "fall-classic" in msg and "--tournament" in msg
    assert isinstance(info.value, SuperMarketError)
    # did not go on to list every status
    assert [c.params["status"] for c in fake.calls] == ["active"]


def test_resolve_context_several_active_across_pages(fake, client) -> None:
    page1 = {"data": [tournament()], "pagination": {"limit": 100, "offset": 0, "hasMore": True, "total": 2}}
    page2 = {"data": [tournament(slug="fall-classic", tid=OTHER_TID)], "pagination": {"limit": 100, "offset": 1, "hasMore": False, "total": 2}}
    fake.add("GET", "/tournaments", page1, page2)
    with pytest.raises(ContextError, match="fall-classic"):
        resolve_context(client)
    assert [c.params["offset"] for c in fake.calls] == ["0", "1"]


def test_resolve_context_zero_active_one_any_status(fake, client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="supermarket_bot")
    ended = tournament(status="ended")
    fake.add("GET", "/tournaments", tournaments_route([], [ended]))
    ctx = resolve_context(client)
    assert ctx.tournament_id == TOURNAMENT_ID and ctx.status == "ended"
    assert [c.params["status"] for c in fake.calls] == ["active", "any"]
    assert "No active tournament" in caplog.text


def test_resolve_context_zero_active_several_any_raises(fake, client) -> None:
    every = [tournament(status="ended"), tournament(slug="next-cup", tid=OTHER_TID, status="draft")]
    fake.add("GET", "/tournaments", tournaments_route([], every))
    with pytest.raises(ContextError) as info:
        resolve_context(client)
    msg = str(info.value)
    assert "No active tournament" in msg
    assert f"{TOURNAMENT_SLUG} (ended)" in msg and "next-cup (draft)" in msg


def test_resolve_context_no_tournaments_falls_back_to_public(fake, client) -> None:
    fake.add("GET", "/tournaments", tournaments_route([], []))
    assert resolve_context(client) == Context.public()
    assert len(fake.calls) == 2


def test_context_helpers() -> None:
    ctx = Context.from_tournament({"id": 42, "slug": "s", "name": None, "currencyName": "coins", "status": "active"})
    assert ctx.tournament_id == "42" and ctx.name == "s" and ctx.label == "s" and not ctx.is_public
    assert Context.from_tournament({"id": "abc"}).name == "abc"
    unslugged = Context(TOURNAMENT_ID, None, "Cup")
    assert unslugged.label == "public" and not unslugged.is_public
    pub = Context.public()
    assert pub.name == "Public global markets" and pub.slug is None


# --------------------------------------------------------------------------- list_markets


def test_list_markets_tournament_slug_paginates_with_cursor(fake, client) -> None:
    fake.add(
        "GET",
        MARKETS_PATH,
        market_page([market("26", "A?", [("36", "YES", 0.4)])], cursor="cur-2"),
        market_page([market("27", "B?", [("37", "YES", 0.5)])]),
    )
    bot = MarketDataBot(client, tournament_ctx())
    markets = bot.list_markets(status="open", search="senate")
    assert [m["id"] for m in markets] == ["26", "27"]
    calls = fake.calls_to(MARKETS_PATH)
    assert calls[0].params == {"limit": "100", "status": "open", "search": "senate"}
    assert calls[1].params == {"limit": "100", "status": "open", "search": "senate", "cursor": "cur-2"}
    assert fake.calls_to("/markets") == []


def test_list_markets_default_status_and_max_items(fake, client) -> None:
    fake.add("GET", MARKETS_PATH, market_page([market(str(i), "m", []) for i in range(5)], cursor="more"))
    bot = MarketDataBot(client, tournament_ctx())
    assert len(bot.list_markets(max_items=3)) == 3
    assert len(fake.calls) == 1
    assert fake.calls[0].params == {"limit": "100", "status": "open"}


def test_list_markets_without_slug_uses_markets_with_tournament_id(fake, client) -> None:
    fake.add("GET", "/markets", market_page([market("26", "A?", [])]))
    bot = MarketDataBot(client, Context(TOURNAMENT_ID, None, "Cup"))
    assert [m["id"] for m in bot.list_markets(status="any")] == ["26"]
    assert fake.calls_to("/markets")[0].params == {"limit": "100", "tournamentId": TOURNAMENT_ID, "status": "any"}


def test_list_markets_public_omits_tournament_id(fake, client) -> None:
    fake.add("GET", "/markets", market_page([market("26", "A?", [])], cursor="n2"), market_page([market("27", "B?", [])]))
    bot = MarketDataBot(client, Context.public())
    assert [m["id"] for m in bot.list_markets(search="cpi")] == ["26", "27"]
    calls = fake.calls_to("/markets")
    assert calls[0].params == {"limit": "100", "status": "open", "search": "cpi"}
    assert calls[1].params["cursor"] == "n2" and "tournamentId" not in calls[1].params


# --------------------------------------------------------------------------- snapshot


def two_markets() -> List[Dict[str, Any]]:
    return [
        market("26", "Who wins?", [("36", "A", 0.40), ("37", "B", 0.60)]),
        market("27", "CPI above 3%?", [("38", None, 0.25)]),
    ]


def test_snapshot_requests_bulk_prices_and_joins_rows(fake, client) -> None:
    fake.add("GET", MARKETS_PATH, market_page(two_markets()))
    fake.add(
        "GET",
        "/exchanges/prices",
        {
            "data": [
                price("36", "26", 0.41, 0.40, 0.42, "A"),
                price("37", "26", 0.59, 0.58, 0.61, "B"),
                price("38", "27", 0.26, 0.25, 0.27, "YES"),
            ],
            "missingIds": [],
        },
    )
    bot = MarketDataBot(client, tournament_ctx())
    snap = bot.snapshot()
    call = fake.calls_to("/exchanges/prices")[0]
    assert call.params == {"ids": "36,37,38", "tournamentId": TOURNAMENT_ID}
    assert snap.context.tournament_id == TOURNAMENT_ID and snap.missing_ids == []
    assert [m["id"] for m in snap.markets] == ["26", "27"]
    assert set(snap.rows[0]) == set(ROW_FIELDS)
    by = snap.by_exchange()
    assert by["37"] == {
        "taken_at": iso(snap.taken_at),
        "tournament": TOURNAMENT_SLUG,
        "market_id": "26",
        "market_title": "Who wins?",
        "market_status": "open",
        "settlement_date": "2026-11-04T04:59:00.000Z",
        "exchange_id": "37",
        "option": "B",
        "latest_price": 0.59,
        "best_bid": 0.58,
        "best_ask": 0.61,
        "spread": 0.03,
        "mid": 0.595,
    }
    # exchange option is null in the market listing -> taken from the quote
    assert by["38"]["option"] == "YES" and by["38"]["market_id"] == "27"


def test_snapshot_missing_quote_falls_back_to_market_price(fake, client, caplog) -> None:
    fake.add("GET", MARKETS_PATH, market_page(two_markets()))
    fake.add(
        "GET",
        "/exchanges/prices",
        {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("38", "27", None, None, 0.30)], "missingIds": ["37"]},
    )
    snap = MarketDataBot(client, tournament_ctx()).snapshot()
    assert snap.missing_ids == ["37"]
    row = snap.by_exchange()["37"]
    assert row["latest_price"] == 0.60  # market listing's latestPrice
    assert row["best_bid"] is None and row["best_ask"] is None
    assert row["spread"] is None and row["mid"] is None
    assert row["option"] == "B"
    # a quote that exists with a null latestPrice is authoritative: no fallback
    row38 = snap.by_exchange()["38"]
    assert row38["latest_price"] is None and row38["best_ask"] == 0.30 and row38["mid"] is None
    assert "missing from the price snapshot" in caplog.text and "37" in caplog.text


def test_snapshot_spread_computed_when_absent(fake, client) -> None:
    quote = price("38", "27", 0.26, 0.1, 0.3)
    quote["spread"] = None
    fake.add("GET", "/exchanges/prices", {"data": [quote], "missingIds": []})
    snap = MarketDataBot(client, tournament_ctx()).snapshot(markets=[two_markets()[1]])
    assert snap.rows[0]["spread"] == 0.2  # rounded, not 0.19999999999999998
    assert snap.rows[0]["mid"] == 0.2
    assert fake.calls_to(MARKETS_PATH) == []  # explicit markets: no listing


def test_snapshot_without_exchanges_makes_no_price_request(fake, client) -> None:
    fake.add("GET", MARKETS_PATH, market_page([market("26", "Empty", [])]))
    snap = MarketDataBot(client, tournament_ctx()).snapshot()
    assert snap.rows == [] and snap.missing_ids == []
    assert fake.calls_to("/exchanges/prices") == []


def test_snapshot_public_context_omits_tournament_id(fake, client) -> None:
    fake.add("GET", "/markets", market_page([two_markets()[1]]))
    fake.add("GET", "/exchanges/prices", {"data": [price("38", "27", 0.26, 0.25, 0.27)], "missingIds": []})
    snap = MarketDataBot(client, Context.public()).snapshot(search="cpi")
    assert fake.calls_to("/exchanges/prices")[0].params == {"ids": "38"}
    assert fake.calls_to("/markets")[0].params["search"] == "cpi"
    assert snap.rows[0]["tournament"] == "public"


def test_snapshot_chunks_large_markets_and_joins_all(fake, client) -> None:
    exchanges = [(str(1000 + i), f"opt{i}", 0.5) for i in range(150)]
    big = market("26", "Big", exchanges)

    def prices(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        return httpx.Response(200, json={"data": [price(i, "26", 0.01, 0.0, 0.02) for i in ids], "missingIds": []})

    fake.add("GET", "/exchanges/prices", prices)
    snap = MarketDataBot(client, tournament_ctx()).snapshot(markets=[big])
    calls = fake.calls_to("/exchanges/prices")
    assert [len(c.params["ids"].split(",")) for c in calls] == [100, 50]
    assert all(c.params["tournamentId"] == TOURNAMENT_ID for c in calls)
    assert len(snap.rows) == 150 and all(r["latest_price"] == 0.01 for r in snap.rows)


# --------------------------------------------------------------------------- build_rows / diff_rows


def row(eid: str = "36", latest: Any = 0.40, bid: Any = 0.39, ask: Any = 0.41) -> Dict[str, Any]:
    return {"exchange_id": eid, "latest_price": latest, "best_bid": bid, "best_ask": ask}


def test_build_rows_stamp_and_label() -> None:
    taken = datetime(2026, 10, 3, 8, 30, 15, 123456, tzinfo=timezone(timedelta(hours=-4)))
    rows = build_rows([market("26", "T", [("36", "YES", 0.5)])], [], Context.public(), taken)
    assert rows[0]["taken_at"] == "2026-10-03T12:30:15.123Z"
    assert rows[0]["tournament"] == "public"
    assert rows[0]["latest_price"] == 0.5 and rows[0]["best_bid"] is None


def test_diff_first_snapshot_and_unchanged_yield_no_changes() -> None:
    assert diff_rows({}, [row("36"), row("37")]) == []
    assert diff_rows({"36": row("36")}, [row("36")]) == []


def test_diff_latest_price_move_has_delta() -> None:
    changes = diff_rows({"36": row(latest=0.40)}, [row(latest=0.43)])
    assert len(changes) == 1
    assert changes[0]["change"] == "latest_price"
    assert changes[0]["delta"] == 0.03
    assert changes[0]["exchange_id"] == "36"


def test_diff_bid_or_ask_only_changes() -> None:
    bid_only = diff_rows({"36": row(bid=0.39)}, [row(bid=0.38)])
    assert [(c["change"], c["delta"]) for c in bid_only] == [("best_bid", 0.0)]
    both = diff_rows({"36": row()}, [row(bid=0.38, ask=0.44)])
    assert both[0]["change"] == "best_bid,best_ask"
    side_gone = diff_rows({"36": row(ask=0.41)}, [row(ask=None)])
    assert side_gone[0]["change"] == "best_ask"


def test_diff_new_rows_only_after_first_snapshot() -> None:
    changes = diff_rows({"36": row("36")}, [row("36"), row("99", latest=0.7)])
    assert [(c["exchange_id"], c["change"], c["delta"]) for c in changes] == [("99", "new", None)]
    # rows that disappeared are not reported
    assert diff_rows({"36": row("36"), "37": row("37")}, [row("36")]) == []


def test_diff_min_move_filter() -> None:
    prev = {"36": row(latest=0.40, bid=0.39, ask=0.41)}
    assert diff_rows(prev, [row(latest=0.403, bid=0.39, ask=0.41)], min_move=0.005) == []
    big = diff_rows(prev, [row(latest=0.43, bid=0.391, ask=0.41)], min_move=0.005)
    assert [c["change"] for c in big] == ["latest_price"]  # the 0.001 bid move is filtered
    # a value appearing or disappearing always counts, whatever min_move is
    appeared = diff_rows({"36": row(latest=None)}, [row(latest=0.40)], min_move=0.5)
    assert appeared[0]["change"] == "latest_price" and appeared[0]["delta"] is None


@pytest.mark.xfail(strict=True, reason="BUG: diff_rows compares raw float differences, so an exact min_move-sized move (e.g. 0.40->0.41 with 0.01) is dropped")
@pytest.mark.parametrize("old, new, min_move", [(0.40, 0.41, 0.01), (0.41, 0.40, 0.01), (0.405, 0.41, 0.005)])
def test_diff_move_equal_to_min_move_is_reported(old: float, new: float, min_move: float) -> None:
    # --min-move is documented as "ignore price moves smaller than this"; a move of
    # exactly min_move (one 0.005 tick, or one cent) is not smaller.
    changes = diff_rows({"36": row(latest=old)}, [row(latest=new)], min_move=min_move)
    assert [c["change"] for c in changes] == ["latest_price"]


# --------------------------------------------------------------------------- SnapshotWriter


def fixed_snapshot(ctx: Context, latest: float, taken: datetime) -> Snapshot:
    markets = [market("26", "Café ✓", [("36", "Oui", latest)])]
    rows = build_rows(markets, [price("36", "26", latest, 0.40, 0.42, "Oui")], ctx, taken)
    rows[0]["extra"] = "ignored by the CSV"
    return Snapshot(taken, ctx, markets, rows, [])


def test_snapshot_writer_files(tmp_path) -> None:
    ctx = tournament_ctx()
    writer = SnapshotWriter(tmp_path, ctx)
    assert writer.dir == tmp_path / TOURNAMENT_SLUG and writer.dir.is_dir()

    taken = utc(2026, 10, 3, 23, 59, 59)
    paths = writer.write(fixed_snapshot(ctx, 0.41, taken))
    writer.write(fixed_snapshot(ctx, 0.45, taken + timedelta(seconds=0.5)))

    assert paths["jsonl"] == tmp_path / TOURNAMENT_SLUG / "prices-2026-10-03.jsonl"
    lines = paths["jsonl"].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["latest_price"] for line in lines] == [0.41, 0.45]  # appended
    assert "Café ✓" in lines[0]  # ensure_ascii=False

    with paths["csv"].open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        body = list(reader)
    assert paths["csv"] == tmp_path / TOURNAMENT_SLUG / "latest.csv"
    assert header == ROW_FIELDS
    assert len(body) == 1 and body[0][ROW_FIELDS.index("latest_price")] == "0.45"  # rewritten, latest only
    assert not (tmp_path / TOURNAMENT_SLUG / "latest.csv.tmp").exists()

    markets = json.loads(paths["markets"].read_text(encoding="utf-8"))
    assert paths["markets"] == tmp_path / TOURNAMENT_SLUG / "markets.json"
    assert markets[0]["id"] == "26" and markets[0]["exchanges"][0]["latestPrice"] == 0.45

    # the next UTC day starts a new JSONL file
    writer.write(fixed_snapshot(ctx, 0.5, utc(2026, 10, 4, 0, 0, 1)))
    next_day = tmp_path / TOURNAMENT_SLUG / "prices-2026-10-04.jsonl"
    assert len(next_day.read_text(encoding="utf-8").splitlines()) == 1
    assert len(paths["jsonl"].read_text(encoding="utf-8").splitlines()) == 2


def test_bot_snapshot_writes_under_public_label(tmp_path, fake, client) -> None:
    fake.add("GET", "/exchanges/prices", {"data": [price("38", "27", 0.26, 0.25, 0.27)], "missingIds": []})
    bot = MarketDataBot(client, Context.public(), data_dir=tmp_path)
    snap = bot.snapshot(markets=[two_markets()[1]])
    bot.snapshot(markets=[two_markets()[1]])
    out_dir = tmp_path / "public"
    assert (out_dir / f"prices-{snap.taken_at.strftime('%Y-%m-%d')}.jsonl").exists()
    # two snapshots of one row each, appended (summed over files in case UTC midnight passed)
    rows = [json.loads(line) for f in out_dir.glob("prices-*.jsonl") for line in f.read_text(encoding="utf-8").splitlines()]
    assert [r["exchange_id"] for r in rows] == ["38", "38"]
    assert json.loads((out_dir / "markets.json").read_text(encoding="utf-8"))[0]["id"] == "27"
    with (out_dir / "latest.csv").open(newline="", encoding="utf-8") as fh:
        assert [r["exchange_id"] for r in csv.DictReader(fh)] == ["38"]


# --------------------------------------------------------------------------- watch


def make_bot(client, clock: FakeClock, **kw: Any):
    bot_sleep = Sleeper(clock)
    out = io.StringIO()
    bot = MarketDataBot(client, tournament_ctx(), out=out, sleep=bot_sleep, clock=clock, **kw)
    return bot, bot_sleep, out


def prices_reply(latest36: float, bid36: float = 0.40) -> Dict[str, Any]:
    return {"data": [price("36", "26", latest36, bid36, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []}


def test_watch_three_iterations_reports_changes(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, market_page([two_markets()[0]]))
    fake.add("GET", "/exchanges/prices", prices_reply(0.41), prices_reply(0.41), prices_reply(0.45))
    bot, bot_sleep, out = make_bot(client, clock)
    seen = []
    bot.watch(interval=30, iterations=3, on_snapshot=lambda snap, changes: seen.append((snap, changes)))

    assert len(seen) == 3
    assert [len(changes) for _, changes in seen] == [0, 0, 1]
    change = seen[2][1][0]
    assert change["exchange_id"] == "36" and change["change"] == "latest_price" and change["delta"] == 0.04
    assert bot_sleep.calls == [30.0, 30.0]  # no sleep after the last iteration
    assert len(fake.calls_to(MARKETS_PATH)) == 1  # default refresh is every 300s
    assert len(fake.calls_to("/exchanges/prices")) == 3

    text = out.getvalue()
    assert "SIG Predictions Cup: 1 markets, 2 outcomes" in text
    assert "no changes" in text
    assert "1 change(s)" in text and "ΔLAST" in text and "+0.040" in text


def test_watch_refreshes_market_list_on_schedule(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, market_page([two_markets()[0]]))
    fake.add("GET", "/exchanges/prices", prices_reply(0.41))
    bot, bot_sleep, _ = make_bot(client, clock)
    # iterations start at t=1000, 1030, 1060, 1090 -> list at 1000 and 1060
    bot.watch(interval=30, iterations=4, refresh_markets_every=60)
    assert len(fake.calls_to(MARKETS_PATH)) == 2
    assert len(fake.calls_to("/exchanges/prices")) == 4
    assert bot_sleep.calls == [30.0, 30.0, 30.0]


def test_watch_new_market_after_refresh_reported_as_new(fake, client, clock) -> None:
    first, second = two_markets()
    fake.add("GET", MARKETS_PATH, market_page([first]), market_page([first, second]))

    def prices(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        return httpx.Response(200, json={"data": [price(i, "x", 0.5, 0.49, 0.51) for i in ids], "missingIds": []})

    fake.add("GET", "/exchanges/prices", prices)
    bot, _, _ = make_bot(client, clock)
    seen = []
    bot.watch(interval=10, iterations=2, refresh_markets_every=0, on_snapshot=lambda s, c: seen.append(c))
    assert seen[0] == []
    assert [(c["exchange_id"], c["change"]) for c in seen[1]] == [("38", "new")]


def test_watch_sleep_accounts_for_elapsed_time(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, market_page([two_markets()[0]]))
    durations = iter([12, 45, 0])

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += next(durations)
        return httpx.Response(200, json=prices_reply(0.41))

    fake.add("GET", "/exchanges/prices", slow)
    bot, bot_sleep, _ = make_bot(client, clock)
    bot.watch(interval=30, iterations=3)
    assert bot_sleep.calls == [18.0, 0.0]


def test_watch_failed_snapshot_logged_and_loop_continues(fake, client, clock, caplog) -> None:
    fake.add("GET", MARKETS_PATH, market_page([two_markets()[0]]))
    fake.add(
        "GET",
        "/exchanges/prices",
        prices_reply(0.41),
        (400, error_body("VALIDATION_ERROR", "ids is required")),
        prices_reply(0.41, bid36=0.39),
    )
    bot, bot_sleep, out = make_bot(client, clock)
    seen = []
    bot.watch(interval=5, iterations=3, on_snapshot=lambda s, c: seen.append(c))

    assert len(fake.calls_to("/exchanges/prices")) == 3
    assert len(seen) == 2  # the failed iteration reports nothing
    assert [(c["exchange_id"], c["change"]) for c in seen[1]] == [("36", "best_bid")]  # diffed vs iteration 1
    assert bot_sleep.calls == [5.0, 5.0]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "snapshot failed" in errors[0].getMessage() and "VALIDATION_ERROR" in errors[0].getMessage()
    assert "1 change(s)" in out.getvalue()


def test_watch_failed_market_listing_is_retried_next_iteration(fake, client, clock, caplog) -> None:
    fake.add("GET", MARKETS_PATH, (403, error_body("FORBIDDEN", "not a member")), market_page([two_markets()[0]]))
    fake.add("GET", "/exchanges/prices", prices_reply(0.41))
    bot, _, _ = make_bot(client, clock)
    seen = []
    bot.watch(interval=1, iterations=2, refresh_markets_every=10_000, on_snapshot=lambda s, c: seen.append(s))
    assert len(fake.calls_to(MARKETS_PATH)) == 2
    assert len(seen) == 1 and len(seen[0].rows) == 2
    assert "snapshot failed" in caplog.text


def test_watch_single_iteration_never_sleeps(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, market_page([]))
    bot, bot_sleep, out = make_bot(client, clock)
    bot.watch(interval=30, iterations=1)
    assert bot_sleep.calls == []
    assert fake.calls_to("/exchanges/prices") == []
    assert "0 markets, 0 outcomes" in out.getvalue()


# --------------------------------------------------------------------------- scan


CONSTRAINTS = {
    "data": [{"relationshipId": "r1", "evaluationStatus": "violated", "violationAmount": 0.05, "suggestedCorrectiveTrades": []}],
    "violationsCount": 1,
    "computedAt": "2026-10-03T12:00:00.000Z",
}


def test_scan_reads_violations_and_multi_outcome_books(fake, client, caplog) -> None:
    fake.add("GET", "/relationships/constraints", CONSTRAINTS)
    fake.add(
        "GET",
        MARKETS_PATH,
        market_page(
            [
                market("26", "Who wins?", [("36", "A", 0.4), ("37", "B", 0.6)]),
                market("27", "Binary", [("38", "YES", 0.3)]),
                market("28", "Forbidden multi", [("39", "X", 0.5), ("40", "Y", 0.5)]),
            ]
        ),
    )
    fake.add("GET", "/markets/26/orderbook", two_context_orderbook())
    fake.add("GET", "/markets/28/orderbook", (403, error_body("FORBIDDEN", "nope")))

    result = MarketDataBot(client, tournament_ctx()).scan()

    assert fake.calls_to("/relationships/constraints")[0].params == {"violationsOnly": "true", "tournamentId": TOURNAMENT_ID}
    assert fake.calls_to(MARKETS_PATH)[0].params["status"] == "open"
    assert fake.calls_to("/markets/27/orderbook") == []  # single-outcome markets are not fetched
    assert fake.calls_to("/markets/26/orderbook")[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "1"}
    assert len(fake.calls_to("/markets/28/orderbook")) == 1  # 403 is not retried

    assert result["violations"] == CONSTRAINTS["data"]
    assert result["violationsCount"] == 1 and result["computedAt"] == "2026-10-03T12:00:00.000Z"
    assert result["multiOutcomeMarkets"] == 2 and result["scannedMarkets"] == 1
    # overround / arbitrage come from OUR tournament's context, not the legacy top level (1.20 / False)
    assert result["markets"] == [
        {"market_id": "26", "market_title": "Who wins?", "outcomes": 2, "overround": 0.95, "arbitrage": True}
    ]
    assert "orderbook for market 28 failed" in caplog.text


def test_scan_caps_markets(fake, client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="supermarket_bot")
    fake.add("GET", "/relationships/constraints", {"data": [], "violationsCount": 0, "computedAt": "2026-10-03T12:00:00Z"})
    multis = [market(str(26 + i), f"M{i}", [(f"{i}a", "A", 0.5), (f"{i}b", "B", 0.5)]) for i in range(3)]
    fake.add("GET", MARKETS_PATH, market_page(multis))
    for m in multis:
        fake.add("GET", f"/markets/{m['id']}/orderbook", market_orderbook([book_exchange("1", [], []), book_exchange("2", [], [])], overround=None))
    result = MarketDataBot(client, tournament_ctx()).scan(max_markets=2, status="any")
    assert fake.calls_to(MARKETS_PATH)[0].params["status"] == "any"
    assert [m["market_id"] for m in result["markets"]] == ["26", "27"]
    assert fake.calls_to("/markets/28/orderbook") == []
    assert result["scannedMarkets"] == 2 and result["multiOutcomeMarkets"] == 3
    assert result["markets"][0]["overround"] is None and result["markets"][0]["arbitrage"] is False
    assert result["violations"] == [] and result["violationsCount"] == 0
    assert "scanning 2 of 3" in caplog.text


def test_scan_public_context(fake, client) -> None:
    fake.add("GET", "/relationships/constraints", {"data": None, "computedAt": None})
    fake.add("GET", "/markets", market_page([market("26", "Multi", [("36", "A", 0.4), ("37", "B", 0.5)])]))
    public_book = market_orderbook([book_exchange("36", [], []), book_exchange("37", [], [])], overround=1.04, arb=False, tid=None)
    public_book["overround"] = 7.0  # the public context must win over the legacy field
    fake.add("GET", "/markets/26/orderbook", public_book)
    result = MarketDataBot(client, Context.public()).scan()
    assert fake.calls_to("/relationships/constraints")[0].params == {"violationsOnly": "true"}
    assert "tournamentId" not in fake.calls_to("/markets")[0].params
    assert fake.calls_to("/markets/26/orderbook")[0].params == {"depth": "1"}
    assert result["markets"][0]["overround"] == 1.04
    assert result["violations"] == [] and result["violationsCount"] == 0


# --------------------------------------------------------------------------- formatting


def test_market_rows() -> None:
    long_title = "Will the unemployment rate printed for October be strictly below four point one percent?"
    single = market("27", long_title, [("38", "YES", 0.4213)])
    multi = market("26", "Who wins?", [("36", "A", 0.4), ("37", "B", 0.6)])
    bare = {"id": "30", "title": None, "status": "closed", "settlementDate": None, "exchanges": None}
    rows = market_rows([single, multi, bare])
    assert rows[0] == {
        "id": "27",
        "title": long_title[:59] + "…",
        "status": "open",
        "closes": "2026-11-04 04:59",
        "last": "0.421",
        "categories": "Election Outcome",
    }
    assert len(rows[0]["title"]) == 60
    assert rows[1]["last"] == "2 outcomes" and rows[1]["title"] == "Who wins?"
    assert rows[2] == {"id": "30", "title": "", "status": "closed", "closes": "", "last": "0 outcomes", "categories": ""}


def test_market_rows_single_exchange_without_price() -> None:
    assert market_rows([market("27", "T", [("38", "YES", None)])])[0]["last"] == "—"


def test_price_table_formatting() -> None:
    rows = [
        {"market_id": "26", "market_title": "Who wins?", "exchange_id": "36", "option": "A", "latest_price": 0.41, "best_bid": 0.4, "best_ask": None, "spread": None},
        {"market_id": "27", "market_title": "x" * 80, "exchange_id": "38", "option": None, "latest_price": None, "best_bid": 0.25, "best_ask": 0.27, "spread": 0.02},
    ]
    lines = price_table(rows).splitlines()
    assert lines[0].split() == ["MKT", "MARKET", "EXCH", "OPTION", "LAST", "BID", "ASK", "SPREAD"]
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert lines[2].split() == ["26", "Who", "wins?", "36", "A", "0.410", "0.400", "—", "—"]
    assert ("x" * 47 + "…") in lines[3] and ("x" * 48) not in lines[3]  # titles capped at 48
    assert lines[3].split()[-4:] == ["—", "0.250", "0.270", "0.020"]
    assert "ΔLAST" not in lines[0]


def test_price_table_with_delta() -> None:
    changes = diff_rows(
        {"36": {"market_id": "26", "market_title": "M", "exchange_id": "36", "option": "A", "latest_price": 0.41, "best_bid": 0.4, "best_ask": 0.42}},
        [{"market_id": "26", "market_title": "M", "exchange_id": "36", "option": "A", "latest_price": 0.38, "best_bid": 0.37, "best_ask": 0.42, "spread": 0.05}],
    )
    lines = price_table(changes, with_delta=True).splitlines()
    assert lines[0].split()[-2:] == ["ΔLAST", "CHANGED"]
    assert lines[2].split()[-2:] == ["-0.030", "latest_price,best_bid"]
    new_row = diff_rows({"1": {}}, [{"exchange_id": "36", "latest_price": 0.5}])
    assert price_table(new_row, with_delta=True).splitlines()[2].split()[-1] == "new"
