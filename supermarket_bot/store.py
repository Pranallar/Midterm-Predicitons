"""SQLite persistence for the tracker (ticks, candles, trades, surges, news cache, state).

Thread-safe: one connection guarded by a lock (WAL mode on file databases). Pass ``":memory:"``
for tests. See docs/DESIGN.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .models import Article, Attribution, ExchangeInfo, MarketInfo, PricePoint, Surge, TradeRecord


class TrackerStore:
    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # metadata -------------------------------------------------------------
    def upsert_markets(self, markets: Sequence[Mapping[str, Any]]) -> None:
        """Store API market dicts (``GET /tournaments/{slug}/markets`` rows) and their exchanges."""
        raise NotImplementedError

    def markets(self) -> List[MarketInfo]:
        raise NotImplementedError

    def exchanges(self) -> List[ExchangeInfo]:
        raise NotImplementedError

    def exchange(self, exchange_id: str) -> Optional[ExchangeInfo]:
        raise NotImplementedError

    # prices -----------------------------------------------------------------
    def add_ticks(self, ts: float, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert snapshot rows (``bot.build_rows`` shape: exchange_id, latest_price, best_bid, best_ask)."""
        raise NotImplementedError

    def add_candles(self, exchange_id: str, resolution: str, candles: Sequence[Mapping[str, Any]]) -> int:
        """Insert ``price-history`` candles (``time`` ISO, ``close`` …). Idempotent."""
        raise NotImplementedError

    def has_candles(self, exchange_id: str, resolution: str) -> bool:
        raise NotImplementedError

    def series(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[PricePoint]:
        """Merged, time-ordered points: candle closes (``source="candle"``, at candle end time)
        for the period before the first tick, then ticks. Marks via ``analytics.mark_price``."""
        raise NotImplementedError

    def latest(self, exchange_id: str) -> Optional[PricePoint]:
        raise NotImplementedError

    def tick_count(self) -> int:
        raise NotImplementedError

    # trades -----------------------------------------------------------------
    def add_trades(self, exchange_id: str, trades: Sequence[Mapping[str, Any]]) -> int:
        """Insert trade-tape dicts (``id, createdAt, price, size, side``). Idempotent by id."""
        raise NotImplementedError

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        raise NotImplementedError

    # surges -----------------------------------------------------------------
    def record_surge(self, surge: Surge, merge_window_s: float = 6 * 3600) -> Surge:
        """Insert, or merge into the open surge for the same exchange+direction detected within
        ``merge_window_s`` (keep earliest start, latest end, larger |change|). Returns the stored surge with id."""
        raise NotImplementedError

    def update_surge(self, surge: Surge) -> None:
        raise NotImplementedError

    def set_attribution(self, surge_id: int, attribution: Attribution) -> None:
        raise NotImplementedError

    def get_surge(self, surge_id: int) -> Optional[Surge]:
        raise NotImplementedError

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None, limit: int = 200) -> List[Surge]:
        """Newest first (by detected_at)."""
        raise NotImplementedError

    # news cache / state -------------------------------------------------------
    def cached_news(self, key: str, max_age_s: float, now: float) -> Optional[List[Article]]:
        raise NotImplementedError

    def put_news(self, key: str, articles: Sequence[Article], now: float) -> None:
        raise NotImplementedError

    def get_state(self, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    def set_state(self, key: str, value: Any) -> None:
        raise NotImplementedError
