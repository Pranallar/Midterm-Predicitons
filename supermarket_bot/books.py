"""Order-book model shared by the REST commands and the realtime stream.

All prices are YES-normalised probabilities in ``[0, 1]``. Bids are best (highest)
first, asks best (lowest) first.

Books carry an engine version ``asOf = {sequence, at}``. Versions compare by
``sequence`` first, then ``at`` as an instant. A book with a null ``asOf`` was read
without an engine version, so any versioned book replaces it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

_FRACTION = re.compile(r"\.(\d+)")


def parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``Z`` or offset, any fraction length) to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class BookVersion:
    sequence: int
    at: Optional[datetime]

    @classmethod
    def parse(cls, raw: Any) -> Optional["BookVersion"]:
        if not isinstance(raw, Mapping):
            return None
        seq = raw.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, (int, float)):
            return None
        return cls(int(seq), parse_time(raw.get("at")))

    def key(self) -> Tuple[int, float]:
        return (self.sequence, self.at.timestamp() if self.at else float("-inf"))

    def __str__(self) -> str:
        return f"seq {self.sequence}"


def is_newer(candidate: Optional[BookVersion], held: Optional[BookVersion]) -> bool:
    """True when a book at ``candidate`` should replace one held at ``held``."""
    if held is None:
        return True
    if candidate is None:
        return False
    return candidate.key() > held.key()


@dataclass(frozen=True)
class Level:
    price: float
    quantity: float


def _levels(raw: Any, descending: bool) -> List[Level]:
    levels: List[Level] = []
    for item in raw or []:
        if not isinstance(item, Mapping):
            continue
        price, qty = item.get("price"), item.get("quantity")
        if isinstance(price, (int, float)) and isinstance(qty, (int, float)) and not isinstance(price, bool):
            levels.append(Level(float(price), float(qty)))
    levels.sort(key=lambda lv: lv.price, reverse=descending)
    return levels


@dataclass
class Book:
    exchange_id: str
    bids: List[Level] = field(default_factory=list)
    asks: List[Level] = field(default_factory=list)
    as_of: Optional[BookVersion] = None
    next_expiry_at: Optional[datetime] = None
    option: Optional[str] = None
    market_id: Optional[str] = None

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any], market_id: Optional[str] = None) -> "Book":
        """Build from any book shape: exchange/market REST orderbooks or a pushed book."""
        exchange_id = raw.get("exchangeId", raw.get("id"))
        return cls(
            exchange_id=str(exchange_id),
            bids=_levels(raw.get("bids"), descending=True),
            asks=_levels(raw.get("asks"), descending=False),
            as_of=BookVersion.parse(raw.get("asOf")),
            next_expiry_at=parse_time(raw.get("nextExpiryAt")),
            option=raw.get("option"),
            market_id=str(raw["marketId"]) if raw.get("marketId") is not None else market_id,
        )

    @property
    def best_bid(self) -> Optional[Level]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Level]:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round(self.best_ask.price - self.best_bid.price, 6)
        return None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return round((self.best_bid.price + self.best_ask.price) / 2, 6)
        return None

    def top(self) -> Tuple[Optional[Level], Optional[Level]]:
        return self.best_bid, self.best_ask


class BookStore:
    """Holds the newest known book per exchange.

    * Pushed (realtime) books apply only when newer than the held one.
    * REST books are an authoritative resync: they apply unless the held book is
      strictly newer (a push can land while the REST read is in flight).
    """

    def __init__(self) -> None:
        self.books: Dict[str, Book] = {}

    def get(self, exchange_id: Any) -> Optional[Book]:
        return self.books.get(str(exchange_id))

    def apply_pushed(self, book: Book) -> bool:
        held = self.books.get(book.exchange_id)
        if held is not None and not is_newer(book.as_of, held.as_of):
            return False
        self._store(book, held)
        return True

    def apply_rest(self, book: Book) -> bool:
        held = self.books.get(book.exchange_id)
        if held is not None and held.as_of is not None and book.as_of is not None:
            if held.as_of.key() > book.as_of.key():
                return False
        self._store(book, held)
        return True

    def _store(self, book: Book, held: Optional[Book]) -> None:
        if held is not None:
            book.option = book.option or held.option
            book.market_id = book.market_id or held.market_id
        self.books[book.exchange_id] = book

    def expired(self, now: datetime) -> List[str]:
        """Exchanges whose soonest resting-order expiry has passed (refetch them)."""
        return [eid for eid, b in self.books.items() if b.next_expiry_at is not None and b.next_expiry_at <= now]


def select_context(response: Mapping[str, Any], tournament_id: Optional[str]) -> Mapping[str, Any]:
    """Pick the ``contexts[]`` entry for ``tournament_id`` from a multi-context response.

    Market and market-orderbook responses carry legacy top-level fields plus one
    context per accessible tournament. When a context matches the tournament we
    asked for, use it; otherwise fall back to the top-level object.
    """
    for ctx in response.get("contexts") or []:
        tournament = ctx.get("tournament") if isinstance(ctx, Mapping) else None
        if tournament_id is None:
            if ctx.get("type") == "public" or tournament is None:
                return ctx.get("orderbook", ctx)
        elif isinstance(tournament, Mapping) and tournament.get("id") == tournament_id:
            return ctx.get("orderbook", ctx)
    return response


def books_from_market_orderbook(
    response: Mapping[str, Any], tournament_id: Optional[str], market_id: Optional[str] = None
) -> Tuple[List[Book], Mapping[str, Any]]:
    """Books for every exchange in ``GET /markets/{id}/orderbook`` for one context."""
    ob = select_context(response, tournament_id)
    if "exchanges" not in ob:
        ob = response
    books = [Book.from_payload(ex, market_id=market_id) for ex in ob.get("exchanges") or []]
    return books, ob


def format_level(level: Optional[Level]) -> str:
    if level is None:
        return "—"
    return f"{level.price:.3f} x {level.quantity:g}"
