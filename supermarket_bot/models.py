"""Shared data types for the tracker, analytics, news attribution, strategy and dashboard.

Conventions (all modules follow these):

* Timestamps are Unix epoch **seconds** (``float``, UTC). Convert to ISO only at the edges
  (JSON for the dashboard uses :func:`iso_ts`).
* Prices are YES-denominated probabilities in ``[0, 1]``. A NO share costs ``1 - price``.
* Every dataclass has ``to_dict()`` producing JSON-safe values (nested dataclasses become
  dicts, timestamps stay floats, plus ``*_iso`` companions where useful for the UI).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VERDICT_NEWS = "news"
VERDICT_PARTICIPANTS = "participants"
VERDICT_UNCLEAR = "unclear"
VERDICTS = (VERDICT_NEWS, VERDICT_PARTICIPANTS, VERDICT_UNCLEAR)

SURGE_OPEN = "open"  # still near its peak
SURGE_REVERTED = "reverted"  # gave back at least half of the move
SURGE_HELD = "held"  # older than 24h and did not revert
SURGE_CLOSED = "closed"  # its market left the open-market list (closed or settled): no trade ideas

NEWS_OK = "ok"  # at least one news provider answered
NEWS_UNAVAILABLE = "unavailable"  # every news provider failed: missing headlines are not evidence
NEWS_DISABLED = "disabled"  # no news searcher configured


def iso_ts(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, float) and value != value:  # NaN is not valid JSON
        return None
    return value


class _Serializable:
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            out[f.name] = _plain(getattr(self, f.name))
        return out


@dataclass
class PricePoint(_Serializable):
    """One observation of an exchange's price.

    ``price`` is the *mark* used by analytics (see ``analytics.mark_price``): the mid when
    the book is two-sided and tight, else the last trade. ``source`` is ``"tick"`` (our own
    bulk-price snapshot) or ``"candle"`` (backfilled ``GET /exchanges/{id}/price-history``
    close; bid/ask are then None).
    """

    ts: float
    price: Optional[float]
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    source: str = "tick"


@dataclass
class MarketInfo(_Serializable):
    market_id: str
    title: str
    status: Optional[str] = None
    settlement_date: Optional[str] = None  # ISO string as returned by the API
    categories: List[str] = field(default_factory=list)
    is_multi: bool = False
    exchange_ids: List[str] = field(default_factory=list)


@dataclass
class ExchangeInfo(_Serializable):
    exchange_id: str
    market_id: str
    option: Optional[str] = None
    market_title: str = ""
    settlement_date: Optional[str] = None
    initial_price: Optional[float] = None


@dataclass
class TradeRecord(_Serializable):
    """One execution from ``GET /exchanges/{id}/trades`` (the tape has no trader identity)."""

    trade_id: str
    exchange_id: str
    ts: float
    price: Optional[float]
    size: float
    side: Optional[str] = None  # "YES" | "NO" (the taker side as reported by the tape)


@dataclass
class Article(_Serializable):
    title: str
    url: str
    source: str = ""
    published_at: Optional[float] = None
    summary: Optional[str] = None
    provider: str = ""
    relevance: float = 0.0  # 0..1, set by news.relevance()


@dataclass
class TradeFlow(_Serializable):
    """Who-moved-the-price statistics for the trades inside a surge window."""

    n_trades: int = 0
    total_size: float = 0.0
    max_trade_size: float = 0.0
    top_trade_share: float = 0.0  # largest single trade / total size
    hhi: float = 0.0  # Herfindahl index of trade sizes (1.0 = one trade did everything)
    yes_share: Optional[float] = None  # share of size whose side is YES
    vwap: Optional[float] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    price_impact: Optional[float] = None  # |price move| per 100 shares traded


@dataclass
class Attribution(_Serializable):
    verdict: str  # one of VERDICTS
    confidence: float  # 0..1
    reversion_odds: float  # 0..1 estimated chance the move gives back >= half
    summary: str
    reasons: List[str] = field(default_factory=list)
    articles: List[Article] = field(default_factory=list)
    flow: Optional[TradeFlow] = None
    book_depth: Optional[float] = None  # resting shares within 5 cents of the mid
    method: str = "heuristic"  # "heuristic" | "llm" | "heuristic+llm"
    llm: Optional[Dict[str, Any]] = None
    analyzed_at: Optional[float] = None
    news_status: Optional[str] = None  # NEWS_OK | NEWS_UNAVAILABLE | NEWS_DISABLED (None: analysed before this existed)


@dataclass
class Surge(_Serializable):
    exchange_id: str
    market_id: str
    window: str  # "5m" | "1h" | "6h" | "24h"
    window_s: float
    start_ts: float
    end_ts: float
    start_price: float
    end_price: float
    change: float  # end - start (signed)
    direction: str  # "up" | "down"
    peak_price: float
    detected_at: float
    zscore: Optional[float] = None
    status: str = SURGE_OPEN
    current_price: Optional[float] = None
    reverted_fraction: Optional[float] = None  # share of the move given back since the peak
    attribution: Optional[Attribution] = None
    id: Optional[int] = None


@dataclass
class HighBand(_Serializable):
    """An outcome whose favourite side has sat at or above the threshold (e.g. 0.95)."""

    exchange_id: str
    market_id: str
    side: str  # "YES" when YES is the favourite, "NO" when NO is (YES <= 1 - threshold)
    favorite_price: float  # current favourite-side mark
    time_in_band: float  # 0..1 share of the lookback spent >= threshold
    mean: float
    low: float
    high: float
    lookback_s: float
    stable: bool  # range within the lookback <= 0.03
    settlement_date: Optional[str] = None
    hours_to_settlement: Optional[float] = None


@dataclass
class Opportunity(_Serializable):
    kind: str  # "fade" | "carry" | "arbitrage" | "watch"
    exchange_id: Optional[str]
    market_id: str
    title: str
    option: Optional[str]
    side: str  # "yes" | "no": the contract to BUY (selling YES == buying NO)
    entry_price: Optional[float]  # price of the contract being bought
    target_price: Optional[float]
    stop_price: Optional[float]
    prob_win: Optional[float]
    edge: Optional[float]  # expected profit per share (after the stop), in SUSQies
    expected_return: Optional[float]  # edge / entry_price
    horizon_hours: Optional[float]
    suggested_shares: int
    suggested_cost: float
    score: float
    confidence: float
    rationale: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    settles_before_cup_end: Optional[bool] = None
    surge_id: Optional[int] = None
    # Multi-leg ideas (constraint arbitrage, full sets): one entry per contract to buy, each with
    # its own price: {"exchange_id", "market_id", "title", "option", "side", "price"}. Empty for
    # single-outcome ideas, whose exchange_id / option / side / entry_price describe the order.
    legs: List[Dict[str, Any]] = field(default_factory=list)
    unit: str = "shares"  # what suggested_shares counts: "shares", or "sets" for multi-leg ideas


@dataclass
class BacktestResult(_Serializable):
    n_surges: int = 0
    n_reverted: int = 0
    reversion_rate: Optional[float] = None
    avg_fade_return: Optional[float] = None  # per share, fading at detection, exit at horizon
    avg_hold_return: Optional[float] = None  # per share, following the move instead
    horizon_hours: float = 6.0
    by_window: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class StrategyReport(_Serializable):
    generated_at: float
    risk_mode: str  # "protect" | "balanced" | "aggressive"
    headline: str
    balance: Optional[float] = None
    initial_balance: Optional[float] = None
    cup_end: Optional[float] = None
    days_left: Optional[float] = None
    leader_value: Optional[float] = None
    my_rank: Optional[int] = None
    principles: List[str] = field(default_factory=list)
    opportunities: List[Opportunity] = field(default_factory=list)
    backtest: Optional[BacktestResult] = None
    # What the report had to assume because an input was unknown, e.g. "Balance unknown: sized on
    # the 100,000 starting balance" or "Leaderboard unavailable: risk mode assumes balanced".
    assumptions: List[str] = field(default_factory=list)
    disclaimer: str = (
        "Read-only analysis. Nothing is traded automatically; estimates are heuristics, "
        "not guarantees."
    )
