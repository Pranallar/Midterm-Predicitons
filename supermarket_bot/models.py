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
from typing import Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple

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
    if isinstance(value, (set, frozenset)):  # e.g. MarketObservation.open_ids: a sorted list in JSON
        return sorted((_plain(v) for v in value), key=str)
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
    # Only on ``TrackerStore.latest()``: the newest stored order-book read for the exchange,
    # ``{"at": epoch s, "bids": [[price, qty], ...] best first, "asks": [...]}`` (YES prices),
    # used to size trade ideas by the depth they can really fill. Not part of equality.
    book: Optional[Dict[str, Any]] = field(default=None, compare=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        if out.get("book") is None:
            out.pop("book", None)  # the usual point has no book: keep its shape unchanged
        return out


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
    # When this print was fetched by us (store column trades.fetched_at, schema v2; None for older rows).
    # Replays use a print at decision time t only if fetched_at <= t (§6.12.2).
    fetched_at: Optional[float] = None


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
    book_depth: Optional[float] = None  # resting shares within 5 cents of the mid (both sides)
    method: str = "heuristic"  # "heuristic" | "llm" | "heuristic+llm"
    llm: Optional[Dict[str, Any]] = None
    analyzed_at: Optional[float] = None
    news_status: Optional[str] = None  # NEWS_OK | NEWS_UNAVAILABLE | NEWS_DISABLED (None: analysed before this existed)
    # The same depth per side: YES bids within 5 cents below the mid (what a NO buy or a YES sale
    # hits) and YES asks within 5 cents above it (what a YES buy hits). None when unknown.
    bid_depth: Optional[float] = None
    ask_depth: Optional[float] = None


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

    def to_dict(self) -> Dict[str, Any]:
        out = super().to_dict()
        # The largest move seen (start -> peak), next to ``change`` (start -> end of the frame).
        peak, start = self.peak_price, self.start_price
        ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v for v in (peak, start))
        out["peak_change"] = round(peak - start, 6) if ok else None
        return out


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
    # Whether suggested_shares was checked against the order book on the side the idea buys
    # (None: not applicable, e.g. a watch idea or nothing to size). When True, fill_price is the
    # average price (per share, or per set) of filling the suggested size from the book.
    depth_checked: Optional[bool] = None
    fill_price: Optional[float] = None
    # ---- added for docs/PAPER_TRADING.md; all optional, so older payloads keep their shape ----
    idea_id: Optional[str] = None  # stable key (see strategy.idea_id_for): the same idea keeps its id across builds
    race_key: Optional[str] = None  # RaceRef.race_key of the outcome, or of the set's race
    bet: Optional["BetShape"] = None  # payoff shape the sizing policies size from
    exit_plan: Optional["ExitPlan"] = None  # when and how the position is closed
    order_type: str = "taker"  # "taker": take the book now (IOC) | "maker": rest a limit order until expires_at
    limit_price: Optional[float] = None  # worst CONTRACT price accepted per share (per set for set ideas)
    expires_at: Optional[float] = None  # maker orders only
    max_units: Optional[float] = None  # depth cap (shares or sets) at limit_price in the decision-time book; None = unknown
    fair_value: Optional[float] = None  # fair value of the CONTRACT bought (YES or NO) when one was used
    fair_source: Optional[str] = None  # FairValue.source of that value
    settlement_regime: Optional[str] = None  # the regime the edge was computed under (REGIMES)
    profit_per_capital_day: Optional[float] = None  # edge / (cost x days the capital stays tied up)
    sizing: Optional[Dict[str, Any]] = None  # SizeDecision.to_dict() under the report's sizing policy
    alt_sizing: Optional[Dict[str, Any]] = None  # SizeDecision.to_dict() under the other policy (chaser <-> conservative)
    # ---- added in spec revision 2 (docs/PAPER_TRADING.md, "Design decisions") ----
    bet_limit: Optional["BetShape"] = None  # the same bet priced at limit_price (the worst fill the order accepts)
    # (CONTRACT price per unit, units) on offer up to limit_price in the decision-time book, best first;
    # sets: (marginal set cost, sets) from depth.set_levels. Empty = depth unknown (sizing then assumes the touch).
    levels: List[Tuple[float, float]] = field(default_factory=list)
    fv_uncertainty: Optional[float] = None  # +/- uncertainty of the fair value behind the edge (FairValue.uncertainty or a kind default)
    factor_delta: Optional[float] = None  # SUSQies gained per unit for a 1-point national swing toward Democrats (0: sets, no race)
    called_prob: Optional[float] = None  # strategy.called_prob behind the regime EV (None: not used)
    priced_from: str = "tick"  # "tick" | "candle": where the decision quote came from (candle only in replays)


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
    # ---- added in spec revision 2 ----
    net_of_spread: bool = True  # entry at the ask (or 1 - bid), exit at the horizon's bid
    n_participant: int = 0  # surges whose attribution verdict was "participants"
    participant_reversion_rate: Optional[float] = None  # net-of-spread reversion rate of those surges only (fade p)


@dataclass
class StrategyReport(_Serializable):
    generated_at: float
    risk_mode: str  # "protect" | "balanced" | "aggressive"
    headline: str
    balance: Optional[float] = None  # cash (the tournament's myBalance)
    initial_balance: Optional[float] = None
    # Cash plus the market value of open positions: what the leaderboard's value compares with.
    account_value: Optional[float] = None
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
    # ---- added for docs/PAPER_TRADING.md ----
    sizing_policy: str = "conservative"  # POLICIES: which policy sized suggested_shares
    sizing: Optional[Dict[str, Any]] = None  # SizingPolicy.explain() for the report policy (+ "alternative")
    settlement_regime: str = "unknown"  # REGIMES
    fair_value: Optional[Dict[str, Any]] = None  # {"mode", "usable", "total", "providers": {...}} or None


# =========================================================================== paper trading
# Shared types for fair value, new idea kinds, sizing policies, the paper-trading engine and the
# backtest. The spec is docs/PAPER_TRADING.md; field meanings there are normative. Conventions on
# top of the module docstring:
#
# * "YES price" = a YES-denominated price (books, quotes, tape). "Contract price" = the price of the
#   contract named by ``side`` ("yes" -> YES price, "no" -> 1 - YES price). Orders, fills, positions
#   and trades are in contract prices; books and quotes are always YES prices.
# * "units" are shares for single-outcome ideas and sets (one share of every leg) for set ideas.
# * Every list/dict field defaults to empty; every Optional defaults to None (unknown).

REGIME_UNKNOWN = "unknown"
REGIME_RESOLVED = "resolved_outcomes"  # unresolved Cup markets wait for real outcomes and pay 1/0
REGIME_VWAP = "vwap_closeout"  # unresolved markets are cashed at an admin-reviewed ~5h VWAP at the Cup end
REGIMES = (REGIME_UNKNOWN, REGIME_RESOLVED, REGIME_VWAP)

KIND_FADE = "fade"
KIND_CARRY = "carry"
KIND_ARBITRAGE = "arbitrage"
KIND_WATCH = "watch"
KIND_VALUE = "value"
KIND_BASKET = "basket"
KIND_HOLE = "hole"
IDEA_KINDS = (KIND_VALUE, KIND_BASKET, KIND_HOLE, KIND_FADE, KIND_CARRY, KIND_ARBITRAGE, KIND_WATCH)
TRADE_KINDS = (KIND_VALUE, KIND_BASKET, KIND_HOLE, KIND_FADE, KIND_CARRY, KIND_ARBITRAGE)  # watch never trades
SET_KINDS = (KIND_ARBITRAGE, KIND_BASKET)  # multi-leg ideas: legs + unit "sets"

POLICY_CONSERVATIVE = "conservative"
POLICY_CHASER = "chaser"
POLICIES = (POLICY_CONSERVATIVE, POLICY_CHASER)

FV_MANUAL = "manual"
FV_POLYMARKET = "polymarket"
FV_KALSHI = "kalshi"
FV_BLEND = "blend"
FV_DEMO = "demo"
FV_SOURCES = (FV_MANUAL, FV_POLYMARKET, FV_KALSHI, FV_BLEND, FV_DEMO)
FV_CONF_HIGH = "high"
FV_CONF_MEDIUM = "medium"
FV_CONF_LOW = "low"


# --------------------------------------------------------------------------- fair value


@dataclass(frozen=True)
class RaceRef(_Serializable):
    """One outcome's place in a race, parsed from its title (see fairvalue.parse_race)."""

    race_key: str  # "2026:SENATE:NH" | "2026:GOVERNOR:GA" | "2026:HOUSE:AZ-01" | "2026:SENATE_CONTROL:US" | "2026:HOUSE_CONTROL:US"
    office: str  # "SENATE" | "GOVERNOR" | "HOUSE" | "SENATE_CONTROL" | "HOUSE_CONTROL"
    state: str  # USPS code ("NH"), "US" for chamber control
    district: Optional[str] = None  # "AZ-01" for House seats, else None
    party: str = "O"  # "D" | "R" | "I" | "L" | "G" | "O" (other / any other candidate)
    candidate: Optional[str] = None  # a named candidate when the outcome names one
    source: str = "title"  # "title" | "option" | "override"


@dataclass
class FairValueQuote(_Serializable):
    """One external venue's (or the manual file's) view of one Super Market outcome (YES prices)."""

    venue: str  # FV_SOURCES member
    external_id: str  # Polymarket market id, Kalshi ticker, "manual:<line>", "demo:<eid>"
    label: str = ""  # human-readable: "Chris Pappas (D) - New Hampshire Senate Election Winner"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    mid: Optional[float] = None
    raw_value: Optional[float] = None  # venue probability before race normalisation
    value: Optional[float] = None  # after race normalisation; None = not usable
    spread: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    fetched_at: Optional[float] = None  # our clock when the response arrived
    venue_updated_at: Optional[float] = None  # the venue's own timestamp, if any (not a quote time on Gamma)
    match_kind: str = "EXACT"  # "EXACT" | "NEAR" | "MANUAL"
    match_confidence: float = 0.0  # 0..1
    flags: List[str] = field(default_factory=list)  # "last-trade-only", "placeholder", "stale", "suspect-sum", "wide"


@dataclass
class FairValue(_Serializable):
    """The combined fair value of one Super Market outcome (probability that YES wins)."""

    exchange_id: str
    value: Optional[float]  # YES probability; None when nothing usable exists
    source: str  # FV_SOURCES member ("blend" when several venues were combined)
    confidence: str = FV_CONF_LOW  # FV_CONF_*
    usable: bool = False  # safe to trade on (fresh, matched, venues agree)
    reason: str = ""  # one sentence: why usable / why not
    bid: Optional[float] = None  # best external executable YES bid (for display)
    ask: Optional[float] = None
    as_of: Optional[float] = None  # the oldest fetched_at among the quotes used
    agreement: Optional[float] = None  # max - min of venue values (None with one venue)
    sources: List[FairValueQuote] = field(default_factory=list)
    race_key: Optional[str] = None
    party: Optional[str] = None
    match_kind: str = "EXACT"  # "EXACT" | "NEAR" | "MANUAL"
    match_confidence: float = 0.0
    note: Optional[str] = None  # the manual entry's note
    manual_updated_at: Optional[float] = None
    # ---- added in spec revision 2 ----
    # +/- uncertainty of ``value`` as a probability: max(agreement, tightest venue half-spread, venue fee
    # band) (fairvalue.uncertainty_of). Strategy needs gap >= min_value_edge + this.
    uncertainty: Optional[float] = None
    prev_value: Optional[float] = None  # the value at the previous refresh (jump check, §5.2)
    prev_as_of: Optional[float] = None
    suspect: bool = False  # |value - Cup mid| > SUSPECT_GAP and the match is not confirmed by the user
    history: bool = False  # rebuilt from imported outside price history ("indicative"), replays only


@dataclass
class FairValueRecord(_Serializable):
    """One stored fair-value observation (store table ``fair_values``) for replays.

    Written on a change of at least one tick (0.005) in value / bid / ask, a change of usable or
    confidence, or a heartbeat every FV_RECORD_HEARTBEAT_S; confirmations in between are proven by
    the ``fair_value_refreshes`` rows (FairValueRefresh)."""

    ts: float  # when it was recorded (our clock); a replay may use it at decision time t only if ts <= t - latency
    exchange_id: str
    value: Optional[float]
    source: str  # FV_SOURCES member, or "history" for imported outside price history
    bid: Optional[float] = None
    ask: Optional[float] = None
    confidence: str = FV_CONF_LOW
    usable: bool = False
    agreement: Optional[float] = None
    # compact: {"as_of", "venues", "match_kind", "match_confidence", "uncertainty", "prev_value", "suspect"}
    # (no reason text, no "sources")
    detail: Dict[str, Any] = field(default_factory=dict)
    as_of: Optional[float] = None  # the live FairValue.as_of (oldest venue fetched_at) when recorded
    venues: List[str] = field(default_factory=list)  # venues whose quotes were used ("polymarket", "kalshi", ...)


@dataclass
class FairValueRefresh(_Serializable):
    """One fair-value refresh (store table ``fair_value_refreshes``): proves which venues answered when,
    so a replay knows an unchanged value was re-confirmed between FairValueRecords."""

    ts: float
    venues: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # venue -> {"status", "fetched_at"}


# --------------------------------------------------------------------------- idea shapes


@dataclass
class BetShape(_Serializable):
    """What one unit of an idea pays, for sizing.

    * ``binary``: pays ``gain`` with probability ``p`` and loses ``loss`` otherwise (a contract
      held to 1/0: gain = 1 - cost, loss = cost; ``p`` is the *effective* win probability,
      entry + expected edge, so that p - cost = EV per share).
    * ``bracket``: a target/stop trade, same fields (gain to target, loss to stop).
    * ``riskless``: a set paying at least ``floor`` per unit for ``cost``; ``gain`` = floor - cost.
      Only when the regime is ``resolved_outcomes`` (every leg pays 1/0) and no refund signal exists.
    * ``bounded``: a set that is riskless at settlement but not under the other regimes or a REFUND
      on one leg (§5.3): pays ``gain`` (expected edge) with ``p = 1 - tail_prob`` and loses ``loss``
      (the refund scenario: set cost - cheapest leg) with ``tail_prob``.
    * ``fixed``: small passive orders (holes): sized by ``cap_pct`` of equity, no Kelly.
    """

    kind: str  # "binary" | "bracket" | "riskless" | "bounded" | "fixed"
    p: float  # win probability (1.0 for riskless)
    gain: float  # per unit if it wins (> 0)
    loss: float  # per unit at risk if it loses (0 for riskless)
    cost: float  # cash per unit at the entry price (contract price, or set cost)
    floor: Optional[float] = None  # riskless / bounded: guaranteed payoff per unit at a 1/0 settlement
    cap_pct: Optional[float] = None  # fixed: stake cap as a share of equity
    tail_prob: Optional[float] = None  # bounded: probability of the loss branch


@dataclass
class ExitPlan(_Serializable):
    """When a position opened from an idea is closed (evaluated by the paper engine every step).

    Prices are the CONTRACT's executable bid (what selling fetches), per share; for baskets the
    per-set proceeds of selling every leg. Rules are checked in this order: settlement,
    ``exit_before_ts``, ``stop_bid``, ``target_bid`` (or the dynamic fair-value target), time stops.
    """

    kind: str  # "bracket" (fade) | "settle" (carry, arbitrage held) | "value" | "basket" | "hole"
    target_bid: Optional[float] = None  # exit when the contract bid >= this
    stop_bid: Optional[float] = None  # exit when the contract bid <= this
    time_stop_ts: Optional[float] = None  # exit at the bid at/after this time
    time_stop_after_fill_s: Optional[float] = None  # hole: exit this long after the entry fill
    exit_before_ts: Optional[float] = None  # be flat before this (settlement regime / closeout window)
    hold_to_resolution: bool = False  # otherwise keep it until one of the rules fires
    dynamic_fv_target: bool = False  # value: target = current fair value (contract) - half spread - buffer
    exit_buffer: float = 0.0  # value: the buffer in that formula
    min_set_profit: Optional[float] = None  # basket: exit when set proceeds - set cost >= this
    note: str = ""  # one sentence for the UI: "Exit when the YES bid reaches 0.565, or hold to resolution"


@dataclass
class StrategyParams(_Serializable):
    """Every tunable of the idea generators (backtest sweeps set these by name)."""

    # value (external fair value vs Super Market)
    min_value_edge: float = 0.02  # per share, after spread, exit cost and the regime adjustment, ON TOP of the fair value's uncertainty
    value_exit_buffer: float = 0.005  # exit when the contract bid >= fv - half spread - this
    fv_max_age_s: float = 90.0  # external fair values older than this are not traded on (competitors react in seconds)
    history_fv_max_age_s: float = 600.0  # imported outside price history (5-60 min bars): replays only
    manual_fv_max_age_s: float = 72 * 3600.0  # manual entries older than this are not traded on
    longshot_shrink: float = 0.10  # buying a contract whose fair value is < 0.15: use fv x (1 - this)
    longshot_min_edge_mult: float = 2.0  # ... and require this x min_value_edge
    near_extra_edge: float = 0.03  # NEAR matches the user enabled for trading need this much more edge
    fv_jump_fraction: float = 0.5  # skip when the outside value moved by more than this x the gap between the last two refreshes
    cup_move_fraction: float = 0.5  # skip when the Cup mid moved away from fair value by more than this x the gap since fv.as_of
    called_prob_fast: float = 0.85  # P(race called before the closeout window), fast-count states
    called_prob_slow: float = 0.40  # slow-count / ranked-choice states (strategy.SLOW_COUNT_STATES)
    called_prob_safe: float = 0.97  # favourite side >= 0.95 (safe seats are called at poll close)
    # Uncalled branch under a VWAP closeout (§5.2): E[p | uncalled] = 0.5 + (q - 0.5) x uncalled_shrink,
    # and the closeout VWAP still carries (1 - unconverged_share) of the Cup's mispricing.
    uncalled_shrink: float = 0.30
    unconverged_share: float = 0.30  # share of the Cup's mispricing (fair - Cup mid) a VWAP closeout of an uncalled race realises
    # basket (pair / set arbitrage)
    basket_min_edge: float = 0.01  # per set: sum(YES bids) - 1 (NO basket), 1 - sum(YES asks) (YES basket)
    basket_exit_fraction: float = 0.5  # exit when set profit now >= max(min profit, this x entry edge)
    basket_exit_min_profit: float = 0.005  # per set
    basket_refund_prob: float = 0.01  # tail probability of a REFUND / ruling on one leg (bounded bet)
    same_snapshot_s: float = 5.0  # every leg of a set idea needs a tick quote from the same bulk snapshot (|dts| <= this)
    # hole (deep resting orders on liquid favourites)
    hole_min_mid: float = 0.70  # favourite side's mid
    hole_min_touch_qty: float = 200.0  # shares at the favourite's best bid in a recent book
    hole_max_spread: float = 0.02
    hole_depth_fraction: float = 0.25  # rest at floor_tick(ref x (1 - this))
    hole_min_ticks_below: int = 6  # and at least this many ticks under the best bid
    hole_ttl_s: float = 6 * 3600.0
    hole_max_stake_pct: float = 0.01
    hole_max_shares: int = 500
    hole_max_exchanges: int = 4
    hole_exit_buffer: float = 0.03  # exit when the bid recovers to ref - this
    hole_time_stop_s: float = 12 * 3600.0
    hole_fill_prob: float = 0.02  # prior, only for the score
    # fade / carry
    fade_horizon_h: float = 6.0
    min_fade_gain: float = 0.02  # per share after both spreads, else no fade
    carry_max_entry: float = 0.995
    # national factor (tilt, chaser slate): P(D) = Phi(margin / race_margin_sd_pts); a national swing moves every margin
    race_margin_sd_pts: float = 7.0
    national_swing_sd_pts: float = 3.0  # one standard deviation of a national polling miss, in points
    # common
    book_max_age_s: float = 900.0  # a stored book older than this does not cap max_units
    settle_margin_s: float = 3600.0  # "settles before the Cup end" needs settlement this much earlier
    closeout_buffer_s: float = 6 * 3600.0  # flat this long before the Cup end when the regime requires it


# --------------------------------------------------------------------------- sizing


@dataclass
class LeaderboardSnapshot(_Serializable):
    """Up to 100 leaderboard rows at one time (the tracker reads ``limit=100``, period all)."""

    at: float
    period: str = "all"
    total: Optional[int] = None
    my_rank: Optional[int] = None
    initial_balance: Optional[float] = None
    # [{"rank": int|None, "username": str|None, "pnl": float|None, "value": float|None}], best first
    entries: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BarEstimate(_Serializable):
    """The estimated final top-3 balance (the bar a chaser must clear)."""

    value: float  # the bar used for sizing = ``low`` (sized from the lower end of the range)
    third_value: Optional[float] = None  # 3rd place now (initial + pnl, mid-Cup marks)
    tenth_value: Optional[float] = None
    hundredth_value: Optional[float] = None
    growth_per_day: Optional[float] = None  # LINEAR growth per day (share of today's value), median over ranks 3-10, clipped
    projected: bool = False  # kept for compatibility: always False (value is never projected; see ``high``)
    method: str = "floor"  # "third" | "floor"
    explanation: List[str] = field(default_factory=list)
    low: Optional[float] = None  # max(third, floor): what sizing uses
    high: Optional[float] = None  # third x (1 + growth x days_left), capped: shown, never sized from
    history_days: Optional[float] = None  # span of leaderboard history behind growth_per_day (needs >= 3 days)


@dataclass
class ExposureSummary(_Serializable):
    """What a portfolio already holds or has reserved, in cash at cost."""

    gross_cost: float = 0.0  # open positions at cost + cash reserved by pending entry orders
    by_race: Dict[str, float] = field(default_factory=dict)  # race_key (or "market:<id>") -> cost
    by_idea: Dict[str, float] = field(default_factory=dict)  # idea_id -> cost
    # SUSQies gained by open positions + pending entries for a 1-point national swing toward Democrats:
    # sum(qty x factor_delta) (delta-weighted, §5.6.1); set legs (basket / arbitrage) are left out.
    national_tilt_d: float = 0.0
    open_positions: int = 0
    # directional (binary / bracket) ideas held or pending: idea_id -> score at entry (chaser top_k slots)
    directional: Dict[str, float] = field(default_factory=dict)
    directional_sign: Dict[str, int] = field(default_factory=dict)  # idea_id -> sign of factor_delta (+1 D, -1 R, 0 none)
    bold_idea: Optional[str] = None  # the chaser's one open bold-to-goal bet, if any


@dataclass
class SizingContext(_Serializable):
    """Everything a SizingPolicy may use (built by build_report or by the paper engine)."""

    now: float
    equity: float  # liquidation value of the portfolio (cash + positions sold into the bids)
    cash: float  # all cash (reserved included)
    free_cash: float  # cash not reserved by pending orders: a size never needs more than this
    start_capital: float
    cup_end: float
    days_left: float
    exposure: ExposureSummary = field(default_factory=ExposureSummary)
    bar: Optional[BarEstimate] = None
    expected_profit: float = 0.0  # sum over open positions of qty x edge per unit at entry
    my_rank: Optional[int] = None
    regime: str = REGIME_UNKNOWN
    all_collateral: bool = False  # riskless sets need only cost - floor in cash (ALL collateral)
    races: Dict[str, "RaceRef"] = field(default_factory=dict)  # exchange_id -> race (for race caps and tilt)
    # ---- added in spec revision 2 ----
    prev_mode: Optional[str] = None  # the chaser mode at the previous sizing (hysteresis, §5.6.4)
    mode_since: Optional[float] = None  # when prev_mode began
    params: Optional["StrategyParams"] = None  # national_swing_sd_pts etc. (None = StrategyParams())


@dataclass
class SizeDecision(_Serializable):
    """How many units a policy buys of one idea, and why (every number explained in text)."""

    idea_id: str
    policy: str
    units: int  # 0 = do not trade
    stake: float  # cash committed (units x cash per unit)
    kelly_fraction: Optional[float] = None  # of equity at risk, before caps
    # "kelly", "idea_cap", "race_cap", "total_cap", "tilt_cap", "swing_cap", "cash", "depth", "top_k",
    # "goal", "zero_edge", "basket_cap", "factor", "certainty"
    capped_by: List[str] = field(default_factory=list)
    mode: Optional[str] = None  # chaser mode: "near" | "chase" | "swing" | "out_of_reach" | "unknown_bar"
    lines: List[str] = field(default_factory=list)  # plain-English explanation, one sentence each
    # ---- added in spec revision 2 ----
    avg_cost: Optional[float] = None  # expected depth-walked average cost per unit at this size (None: touch assumed)
    certainty: Optional[float] = None  # edge^2 / (edge^2 + uncertainty^2) applied to the Kelly multiplier
    factor_scale: Optional[float] = None  # 1 / (1 + (n_same - 1) x FACTOR_RHO): correlated-slate scaling (chaser)
    bold: bool = False  # the chaser's bold-to-goal bet
    replaces: Optional[str] = None  # chaser top_k: sell this held idea first (the new one enters in a later step)


class SizingPolicy(Protocol):
    """Implemented by sizing.ConservativePolicy and sizing.ChaserPolicy (docs/PAPER_TRADING.md §5.6)."""

    name: str
    label: str

    def size(self, opp: Opportunity, ctx: SizingContext) -> SizeDecision:
        """Size one idea on its own (no other new ideas counted): what the Strategy view shows."""
        ...

    def size_all(self, opps: Sequence[Opportunity], ctx: SizingContext) -> Dict[str, SizeDecision]:
        """Size new ideas together, best score first, each seeing the exposure of those before it
        (what the paper engine uses). Keys are idea ids."""
        ...

    def explain(self, ctx: SizingContext) -> Dict[str, Any]:
        """Portfolio-level explanation: {"policy", "label", "mode", "lines", "bar", "M", "markov_ceiling", "ev_multiple"}."""
        ...


# --------------------------------------------------------------------------- inputs and observations


@dataclass
class StrategyInputs(_Serializable):
    """Everything the idea generators read at one decision time (live: pipeline.assemble_inputs;
    replay: backtest.HistoricalMarket.inputs). Nothing in here may be newer than ``now``."""

    now: float
    cup_end: float
    infos: Dict[str, ExchangeInfo] = field(default_factory=dict)  # open outcomes only
    latest: Dict[str, PricePoint] = field(default_factory=dict)  # newest quote per outcome, .book attached when stored
    surges: List[Surge] = field(default_factory=list)  # not closed, last 2 days, attribution analysed <= now
    bands: List[HighBand] = field(default_factory=list)
    constraints: Optional[Dict[str, Any]] = None
    overround_rows: List[Dict[str, Any]] = field(default_factory=list)
    fair_values: Dict[str, FairValue] = field(default_factory=dict)
    races: Dict[str, RaceRef] = field(default_factory=dict)  # exchange_id -> race
    backtest: Optional[BacktestResult] = None
    balance: Optional[float] = None  # real account cash (sizing for the Strategy view)
    initial_balance: Optional[float] = None
    account_value: Optional[float] = None
    leader_value: Optional[float] = None
    my_rank: Optional[int] = None
    leaderboard: Optional[LeaderboardSnapshot] = None
    bar: Optional[BarEstimate] = None
    settlement_regime: str = REGIME_UNKNOWN
    params: Optional[StrategyParams] = None  # None = StrategyParams()
    # ---- added in spec revision 2 ----
    # exchange_id -> [(ts, YES mid)] over the last ~5 minutes, oldest first (the fair-value staleness check, §5.2)
    recent_mids: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    # exchanges where a paper portfolio has a resting hole order: hole ideas there survive a missing book (§5.4)
    resting_exchanges: Set[str] = field(default_factory=set)


@dataclass
class Quote(_Serializable):
    """The bulk-price touch of one outcome at one snapshot (YES prices, no sizes)."""

    exchange_id: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    ts: float  # when the snapshot was taken


@dataclass
class BookObservation(_Serializable):
    """One order-book read (YES prices, best first) and when it was observed."""

    exchange_id: str
    observed_at: float  # our clock when the response arrived
    bids: List[Tuple[float, float]] = field(default_factory=list)  # (YES price, quantity), highest first
    asks: List[Tuple[float, float]] = field(default_factory=list)  # lowest first
    source: str = "paper"  # "paper" | "tracker" | "attribution" | "overround" | "synthetic" (backtest)
    sequence: Optional[int] = None  # asOf.sequence when the API gave one


@dataclass
class SettlementInfo(_Serializable):
    """How one outcome resolved in the tournament scope."""

    exchange_id: str
    market_id: str
    settled_with: Optional[str]  # the API's settledWith, verbatim ("YES", "NO", an option label, "REFUND")
    settled_on: Optional[float] = None
    payout_yes: Optional[float] = None  # 1.0 YES won, 0.0 YES lost, None = refund or unknown
    refund: bool = False
    detected_at: Optional[float] = None


@dataclass
class MarketObservation(_Serializable):
    """What the paper engine may know at ``now`` (live: built each step; replay: by the adapter)."""

    now: float
    cup_end: float
    quotes: Dict[str, Quote] = field(default_factory=dict)
    books: Dict[str, BookObservation] = field(default_factory=dict)  # the newest observation per exchange
    trades: Dict[str, List[TradeRecord]] = field(default_factory=dict)  # tape read since the paper cursor, oldest first
    settlements: Dict[str, SettlementInfo] = field(default_factory=dict)
    open_ids: Set[str] = field(default_factory=set)  # outcomes on the open list AND quoted by the last bulk read
    infos: Dict[str, ExchangeInfo] = field(default_factory=dict)
    fair_values: Dict[str, FairValue] = field(default_factory=dict)
    races: Dict[str, RaceRef] = field(default_factory=dict)


# --------------------------------------------------------------------------- paper configuration


HUMAN_LATENCY_S = 240.0  # "human speed": a person reads the dashboard and places the order by hand ~4 minutes later
HUMAN_MAX_FILL_DELAY_S = 360.0


@dataclass
class PortfolioSpec(_Serializable):
    portfolio_id: str  # "policy:conservative", "human:conservative", "kind:value", ...
    label: str
    policy: str  # POLICIES member
    kinds: Optional[List[str]] = None  # None = every TRADE_KINDS member
    start_capital: Optional[float] = None  # None = the run's start capital
    # ---- added in spec revision 2 ----
    min_latency_s: Optional[float] = None  # None = PaperConfig.min_latency_s; "human:*" portfolios use HUMAN_LATENCY_S
    max_fill_delay_s: Optional[float] = None  # None = PaperConfig.max_fill_delay_s


def default_portfolios(sizing: str = POLICY_CONSERVATIVE) -> List[PortfolioSpec]:
    """The pre-registered headline first: the user's sizing policy at human speed (``human:<sizing>``), then
    both policies at bot speed, then one conservative portfolio per kind (equal capital, exploratory)."""
    if sizing not in POLICIES:
        raise ValueError(f"unknown sizing policy {sizing!r}")
    human_label = {
        POLICY_CONSERVATIVE: "You, acting by hand ~4 min late: all ideas, conservative sizing",
        POLICY_CHASER: "You, acting by hand ~4 min late: all ideas, chaser sizing",
    }[sizing]
    return [
        PortfolioSpec(f"human:{sizing}", human_label, sizing, None, None, HUMAN_LATENCY_S, HUMAN_MAX_FILL_DELAY_S),
        PortfolioSpec("policy:conservative", "Bot speed (30 s): all ideas, conservative sizing (quarter-Kelly, 8% cap)", POLICY_CONSERVATIVE),
        PortfolioSpec("policy:chaser", "Bot speed (30 s): all ideas, chaser sizing (goal-based)", POLICY_CHASER),
        PortfolioSpec("kind:value", "Value vs outside fair value only", POLICY_CONSERVATIVE, [KIND_VALUE]),
        PortfolioSpec("kind:basket", "Pair and set baskets only", POLICY_CONSERVATIVE, [KIND_BASKET]),
        PortfolioSpec("kind:hole", "Liquidity-hole resting orders only", POLICY_CONSERVATIVE, [KIND_HOLE]),
        PortfolioSpec("kind:fade", "Fades of participant spikes only", POLICY_CONSERVATIVE, [KIND_FADE]),
        PortfolioSpec("kind:carry", "High-90s carry only", POLICY_CONSERVATIVE, [KIND_CARRY]),
        PortfolioSpec("kind:arbitrage", "Engine-reported arbitrage only", POLICY_CONSERVATIVE, [KIND_ARBITRAGE]),
    ]


@dataclass
class PaperConfig(_Serializable):
    portfolios: List[PortfolioSpec] = field(default_factory=default_portfolios)
    # The ONE pre-registered portfolio whose verdict can reach every level; None = f"human:{sizing}"
    # (paper.headline_id). Every other portfolio is exploratory (§6.9).
    headline_portfolio: Optional[str] = None
    sizing: str = POLICY_CONSERVATIVE  # the user's --sizing choice (picks the headline)
    target_hours: float = 24.0
    start_capital: Optional[float] = None  # None: account value at reset, else cash, else the initial balance, else 100,000
    regime: str = REGIME_UNKNOWN
    all_collateral: bool = False
    reads_per_min: int = 20  # the paper worker's own limiter (on top of the client's 90/min)
    reads_per_min_during_backfill: int = 6  # while the tracker's backfill queue is not empty
    max_book_reads_per_step: int = 8
    max_trade_reads_per_step: int = 2
    max_tape_pages: int = 3  # follow the tape cursor this many pages when a page comes back full (200)
    book_depth: int = 20
    interval_s: float = 30.0  # the step interval (covered hours count gaps up to 3 x this, §6.11)
    gap_cancel_s: float = 300.0  # after a gap longer than this, resting orders count as cancelled at the last step before it
    min_latency_s: float = 2.0  # a fill uses only observations at least this long after the decision
    max_fill_delay_s: float = 120.0  # a taker order with no usable observation by then expires unfilled
    stale_quote_s: float = 120.0  # no decisions, fills or exits on an outcome whose bulk quote is older
    set_book_max_age_s: float = 60.0  # a set entry waits until every leg has a book at most this old
    mark_book_max_age_s: float = 900.0  # liquidation marks walk a consistent book at most this old ("fresh")
    mark_depth_max_age_s: float = 3600.0  # else the last real book this old, shifted to today's touch ("stale")
    depth_unknown_full_shares: float = 100.0  # no real book within the hour: this many shares at the touch ...
    depth_unknown_haircut: float = 0.5  # ... and the rest at touch x (1 - this) ("unknown")
    queue_book_max_age_s: float = 120.0  # maker queue_ahead needs a book this fresh showing our price level, else None
    consumed_memory_s: float = 1800.0  # liquidity a portfolio took stays taken this long (>= reentry_cooldown_s)
    consumed_match_ticks: int = 1  # ... matched within this many ticks of the price we took
    queue_haircut: float = 0.5  # maker fills from prints AT the limit price, after the queue ahead
    trade_poll_s: float = 60.0  # tape read per exchange with resting orders at most this often
    mark_refresh_s: float = 300.0  # book re-read per open position at most this often
    resting_book_refresh_s: float = 600.0  # book re-read for exchanges with resting orders (keeps hole ideas alive)
    reentry_cooldown_s: float = 1800.0  # an idea that closed may not re-enter for this long
    maker_cancel_after_missing_steps: int = 2  # cancel a resting order once its idea is absent this many steps
    max_open_positions: int = 40  # per portfolio
    max_new_orders_per_step: int = 10  # per portfolio
    equity_min_spacing_s: float = 60.0  # at most one stored equity point per minute per portfolio
    unfilled_tracked: int = 200  # per portfolio: unfilled entry orders kept to show what they would have made
    params: StrategyParams = field(default_factory=StrategyParams)
    demo: bool = False


# --------------------------------------------------------------------------- paper records


@dataclass
class PaperOrder(_Serializable):
    order_id: str  # "<portfolio_id>:o<counter>"
    portfolio_id: str
    idea_id: str
    kind: str
    purpose: str  # "entry" | "exit"
    order_type: str  # "taker" | "maker"
    exchange_id: str
    market_id: str
    side: str  # "yes" | "no": the contract
    action: str  # "buy" | "sell"
    qty: float  # shares of this leg
    limit_price: float  # contract price: max for buys, min for sells
    created_at: float  # decision time
    expires_at: Optional[float] = None
    # "pending" (taker, waiting for a fill observation) | "resting" (maker) | "cancelling" (maker: still
    # fillable by prints up to cancel_at until a tape read after cancel_at is processed) | "filled" |
    # "cancelled" | "expired"
    status: str = "pending"
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    reserved_cash: float = 0.0  # buy orders: cash held back until filled or closed
    reason: str = ""  # why it was placed (one sentence)
    close_reason: Optional[str] = None  # why it ended unfilled / partly filled
    closed_at: Optional[float] = None
    group_id: Optional[str] = None  # basket legs share one group id (= basket_id)
    queue_ahead: Optional[float] = None  # maker: displayed quantity ahead at placement (None = unknown)
    last_trade_ts: Optional[float] = None  # maker: tape processed up to here
    seen_trade_ids: List[str] = field(default_factory=list)  # maker: tape ids already counted (bounded, newest 500)
    missing_steps: int = 0  # maker: consecutive steps its idea was absent from the signals
    cancel_at: Optional[float] = None  # maker: when the engine decided to cancel (or expires_at); prints up to it still count
    title: str = ""
    option: Optional[str] = None
    # ---- added in spec revision 2 ----
    decision_touch: Optional[float] = None  # contract touch at the decision (ask for buys, bid for sells): slippage reference
    latency_s: Optional[float] = None  # this order's portfolio latency (fills need an observation >= created_at + this)
    planned_price: Optional[float] = None  # exits: the price the exit rule planned (target_bid etc.), for exit slippage
    synthetic: bool = False  # filled (partly) from candles or a synthetic book (replays): left out of verdict statistics
    tape_gap: bool = False  # maker: a tape read was truncated past our cursor (prints may have been missed)


@dataclass
class PaperFill(_Serializable):
    fill_id: str
    order_id: str
    portfolio_id: str
    exchange_id: str
    market_id: str
    side: str
    action: str  # "buy" | "sell" | "settle"
    qty: float
    price: float  # contract price
    ts: float  # when the fill was simulated (the step time)
    liquidity: str  # "taker" | "maker" | "settlement"
    purpose: str  # "entry" | "exit" | "settlement"
    kind: str
    idea_id: str
    book_at: Optional[float] = None  # observed_at of the book (taker) or the newest print used (maker)
    latency_s: Optional[float] = None  # book_at - order.created_at
    levels: List[Tuple[float, float]] = field(default_factory=list)  # (contract price, qty) walked
    trade_ids: List[str] = field(default_factory=list)  # maker: the prints that filled it
    reason: str = ""
    group_id: Optional[str] = None
    title: str = ""
    option: Optional[str] = None
    # ---- added in spec revision 2 ----
    slippage: Optional[float] = None  # buys: price - decision_touch; sells: decision_touch - price (positive = worse)
    synthetic: bool = False  # from candles or a synthetic book (replays only)


@dataclass
class PaperPosition(_Serializable):
    position_id: str  # "<portfolio_id>:<exchange_id>"
    portfolio_id: str
    exchange_id: str
    market_id: str
    side: str  # the contract held
    qty: float
    avg_cost: float  # contract price
    cost: float  # cost basis of the open qty
    opened_at: float
    idea_id: str
    kind: str
    edge_per_unit: float = 0.0  # the idea's expected edge per share at entry (for expected_profit)
    race_key: Optional[str] = None
    basket_id: Optional[str] = None  # legs of one basket share it
    exit_plan: Optional[ExitPlan] = None
    realized_pnl: float = 0.0  # from partial exits so far
    collateral_advance: float = 0.0  # all_collateral: cash advanced against the set's floor (repaid on close)
    status: str = "open"  # "open" | "frozen" | "closed"
    # "depth_stale", "depth_unknown", "closed_no_ruling", "stale_quote", "post_cup", "synthetic"
    flags: List[str] = field(default_factory=list)
    first_fill_at: Optional[float] = None
    updated_at: Optional[float] = None
    liq_value: Optional[float] = None  # selling qty into the bids now (depth-aware; frozen positions: their last value, "unvalued")
    mark_value: Optional[float] = None  # qty x contract mid mark (reference only)
    fv_value: Optional[float] = None  # qty x contract fair value (the model's own opinion, reference only)
    last_marked_at: Optional[float] = None
    title: str = ""
    option: Optional[str] = None
    # ---- added in spec revision 2 ----
    depth_state: str = "unknown"  # liquidation mark: "fresh" | "stale" | "unknown" (§6.7)
    factor_delta: Optional[float] = None  # per share (Opportunity.factor_delta); set legs 0
    score: Optional[float] = None  # the idea's score at entry (chaser top_k ranking)
    bold: bool = False  # the chaser's bold-to-goal bet
    floor_per_unit: Optional[float] = None  # set legs: the set's floor / n legs (floor value shown next to liquidation)
    synthetic: bool = False  # opened by a candle / synthetic-book fill (replays only)
    entered_at: Optional[float] = None  # decision time of the entry order (time-block clusters, §6.9)


@dataclass
class PaperTrade(_Serializable):
    """One closed round trip (a position, or every leg of a basket, back to zero)."""

    trade_id: str
    portfolio_id: str
    idea_id: str
    kind: str
    exchange_ids: List[str]
    opened_at: float
    closed_at: float
    qty: float  # shares (sets for baskets)
    cost: float
    proceeds: float  # sales + settlement payouts (+ refunds)
    pnl: float  # proceeds - cost
    # "target" | "stop" | "time" | "settled" | "refund" | "converged" | "fair_value" | "regime" | "legging"
    # | "replaced" | "reset" | "settings changed"
    exit_reason: str
    race_key: Optional[str] = None
    title: str = ""
    legs: List[Dict[str, Any]] = field(default_factory=list)  # [{"exchange_id", "side", "qty", "avg_cost", "avg_exit"}]
    return_pct: Optional[float] = None  # pnl / cost
    hold_hours: Optional[float] = None
    profit_per_capital_day: Optional[float] = None  # pnl / (cost x max(hold days, 1/24))
    # ---- added in spec revision 2 ----
    synthetic: bool = False  # any fill was synthetic (replays): left out of verdict statistics
    entered_at: Optional[float] = None  # decision time of the entry (time-block clusters)
    direction: str = "N"  # "D" | "R" | "N": sign of the position's national-swing exposure at entry (clusters)


VERDICT_LEVELS = ("insufficient", "inconclusive", "promising", "positive", "negative")


@dataclass
class EquityPoint(_Serializable):
    portfolio_id: str
    ts: float
    cash: float  # all cash (reserved included)
    reserved_cash: float
    liq_value: float  # cash + positions at liquidation value - outstanding collateral advances
    mark_value: float  # cash + positions at mid marks - advances
    fv_value: Optional[float]  # cash + positions at fair value where known (else liquidation) - advances
    open_positions: int


@dataclass
class Verdict(_Serializable):
    """An honest statement of what a run shows (docs/PAPER_TRADING.md §6.9, revision 2).

    The statistic is over every idea ENTERED (closed ones at realised P&L, open ones at liquidation
    P&L; frozen and synthetic ones left out), with a two-way cluster-robust t-interval (clusters:
    races, and 2-hour blocks x party direction; G = the smaller count; G - 1 degrees of freedom)."""

    portfolio_id: str
    level: str  # VERDICT_LEVELS: "insufficient" | "inconclusive" | "promising" | "positive" | "negative"
    sentence: str
    hours_run: float  # COVERED hours (sum of step gaps, each capped at 3 x interval), not wall-clock time
    closed_trades: int
    clusters: int  # G = min(race clusters, time-block clusters) among the ideas counted
    pnl_liq: float  # realised + unrealised at liquidation value, frozen ("unvalued") positions at 0
    pnl_ex_best: float  # pnl_liq without the single best idea, open or closed
    best_trade_pnl: Optional[float] = None  # the best idea's P&L (open or closed)
    mean_trade_pnl: Optional[float] = None  # mean P&L per idea entered (SUSQies)
    ci_low: Optional[float] = None  # two-way cluster-robust t-interval of mean_trade_pnl
    ci_high: Optional[float] = None
    ci_level: float = 0.90  # 0.90 for the headline; 1 - 0.10 / n_portfolios for exploratory portfolios
    win_rate: Optional[float] = None  # CLOSED trades only (biased toward quick winners: label it so)
    win_rate_low: Optional[float] = None  # Wilson interval
    win_rate_high: Optional[float] = None
    max_drawdown: Optional[float] = None  # fraction of peak liquidation equity
    thresholds: Dict[str, float] = field(default_factory=dict)  # {"min_hours", "min_ideas", "min_clusters", "positive_hours", "positive_days"}
    reasons: List[str] = field(default_factory=list)  # why this level (one sentence each)
    caveats: List[str] = field(default_factory=list)
    # ---- added in spec revision 2 ----
    exploratory: bool = False  # not the pre-registered headline: adjusted level, capped at "promising"
    n_ideas: int = 0  # ideas counted (closed + open, non-synthetic, non-frozen)
    open_ideas: int = 0
    closed_pnl: float = 0.0  # realised P&L of the closed ideas counted
    open_pnl_liq: float = 0.0  # unrealised P&L at liquidation of the open ideas counted
    clusters_race: int = 0
    clusters_time: int = 0
    df: Optional[int] = None  # G - 1
    return_mean: Optional[float] = None  # mean P&L / cost per idea
    return_ci_low: Optional[float] = None
    return_ci_high: Optional[float] = None
    days_covered: int = 0  # UTC days with >= 6 covered hours
    wall_hours: Optional[float] = None
    unvalued_positions: int = 0  # frozen (market closed without a ruling): left out, named in the sentence
    unvalued_value: float = 0.0  # their last liquidation value
    depth_unknown_share: Optional[float] = None  # share of equity marked without a real book in the last hour
    swing_share: Optional[float] = None  # |national tilt| x national_swing_sd_pts / equity: one-swing exposure
    top_race_share: Optional[float] = None  # largest single race's share of |pnl_liq|
    synthetic_excluded: int = 0  # replays: ideas with candle / synthetic fills left out


@dataclass
class PortfolioSummary(_Serializable):
    portfolio_id: str
    label: str
    policy: str
    kinds: Optional[List[str]]
    start_capital: float
    cash: float
    reserved_cash: float
    positions_liq: float
    positions_mark: float
    positions_fv: Optional[float]
    equity_liq: float
    equity_mark: float
    equity_fv: Optional[float]
    realized_pnl: float
    unrealized_pnl_liq: float
    pnl_liq: float  # equity_liq - start_capital
    pnl_liq_pct: float  # pnl_liq / start_capital
    pnl_mark: float
    max_drawdown: float  # fraction of peak equity_liq
    max_drawdown_abs: float
    fills: int
    orders_open: int
    positions_open: int
    trades_closed: int
    wins: int
    losses: int
    win_rate: Optional[float]
    # kind -> {"pnl_realized", "pnl_unrealized_liq", "pnl_liq", "trades_closed", "wins", "win_rate", "fills", "positions_open"}
    by_kind: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    exposure: Optional[ExposureSummary] = None
    sizing: Optional[Dict[str, Any]] = None  # SizingPolicy.explain()
    verdict: Optional[Verdict] = None
    last_step_at: Optional[float] = None
    # ---- added in spec revision 2 ----
    headline: bool = False  # the one pre-registered portfolio
    exploratory: bool = True
    latency_s: Optional[float] = None  # this portfolio's fill latency (human speed: 240 s)
    unvalued: float = 0.0  # last liquidation value of frozen positions (left out of equity_liq / pnl_liq)
    depth_unknown_share: Optional[float] = None  # share of equity_liq marked "unknown" (no real book within 1 h)
    depth_stale_share: Optional[float] = None  # share marked from a real book 15-60 min old
    swing_risk: Optional[float] = None  # SUSQies lost by a one-sd (3-point) national swing against the portfolio
    # kind -> {"entries", "filled", "fill_rate", "avg_slippage", "unfilled", "unfilled_pnl_now", "exits", "avg_exit_slippage"}
    execution: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    no_trade_reason: Optional[str] = None  # plain sentence when the portfolio has no fills (why: no data vs no edge)


@dataclass
class StepReport(_Serializable):
    now: float
    step: int
    fills: int = 0
    orders_created: int = 0
    orders_expired: int = 0
    orders_cancelled: int = 0
    settled: int = 0
    exits_started: int = 0
    signals: int = 0
    book_reads: int = 0
    trade_reads: int = 0
    reads_skipped: int = 0  # wanted but not read (budget)
    errors: List[str] = field(default_factory=list)
    duration_s: Optional[float] = None  # measured with the injected clock (0.0 on a SimClock)
    # ---- added in spec revision 2 ----
    sets_deferred: int = 0  # set entries waiting for fresh books of every leg
    tape_gaps: int = 0  # tape reads that came back full and could not be paged within budget
    gap_s: Optional[float] = None  # time since the previous step when it exceeded 3 x interval


@dataclass
class SignalEvent(_Serializable):
    """One signal in the event study (study.py, §6.14): what trading it at the decision touch would
    show at fixed horizons, net of the spread, whether or not any portfolio traded it."""

    event_id: str  # "<idea_id>@<int(t0)>"
    idea_id: str
    kind: str  # "value" | "basket"
    t0: float  # decision time of the first appearance in this episode
    exchange_ids: List[str] = field(default_factory=list)
    sides: List[str] = field(default_factory=list)
    race_key: Optional[str] = None
    direction: str = "N"  # "D" | "R" | "N" (clusters)
    entry: float = 0.0  # cost per unit at the decision touch (contract ask; set: sum of leg asks)
    fair_value: Optional[float] = None  # value: the contract's fair value at t0
    gap: Optional[float] = None  # value: fair value - entry; basket: set edge at t0
    traded: bool = False  # some portfolio placed an order for it
    # horizon label ("5m", "30m", "2h", "6h") -> exit value at the touch minus entry, per unit (None: no quote)
    outcomes: Dict[str, Optional[float]] = field(default_factory=dict)
    converged: Dict[str, Optional[bool]] = field(default_factory=dict)  # half the gap closed by the CUP moving
    reversed: Dict[str, Optional[bool]] = field(default_factory=dict)  # the gap closed because the OUTSIDE value moved toward the Cup
    done: bool = False  # every horizon observed (or past by more than 2 x interval)


# --------------------------------------------------------------------------- backtest


@dataclass
class BacktestConfig(_Serializable):
    start: Optional[float] = None  # default: end - hours
    end: Optional[float] = None  # default: the newest stored tick
    hours: float = 24.0
    step_s: float = 300.0  # decision grid
    latency_s: float = 60.0  # fills use the first observation at least this long after a decision
    warmup_s: float = 6 * 3600.0  # history before start used only for detection lookbacks, never traded
    assumed_spread: float = 0.02  # candle-only quotes: close -/+ half of this, on the tick
    assumed_touch_qty: float = 100.0  # synthetic books: shares at the touch only
    volume_share: float = 0.10  # synthetic depth and candle fills: at most this share of the bucket volume
    use_book_snapshots: bool = True
    use_fair_values: bool = True
    paper: PaperConfig = field(default_factory=PaperConfig)
    params_overrides: Dict[str, Any] = field(default_factory=dict)  # StrategyParams field -> value
    label: str = ""
    # ---- added in spec revision 2 ----
    candle_fill_cap: float = 50.0  # shares per candle trade-through fill (5m candles only), at most
    use_history_fair_values: bool = True  # also replay imported outside history (source "history", indicative)
    time_budget_s: Optional[float] = None  # stop early (report says so) after this much wall time; the dashboard passes 120
    live_run_started_at: Optional[float] = None  # the live paper run's start, for the overlap warning


@dataclass
class BacktestReport(_Serializable):
    generated_at: float
    config: BacktestConfig
    window: Dict[str, Any] = field(default_factory=dict)  # {"start", "end", "hours", "steps"}
    # {"exchanges", "decision_steps", "tick_share", "candle_share", "book_snapshot_share",
    #  "synthetic_book_share", "fair_value_share", "tape_share"}
    coverage: Dict[str, Any] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    portfolios: List[PortfolioSummary] = field(default_factory=list)
    equity: Dict[str, List[List[float]]] = field(default_factory=dict)  # portfolio_id -> [[ts, equity_liq], ...]
    trades: List[PaperTrade] = field(default_factory=list)
    verdicts: Dict[str, Verdict] = field(default_factory=dict)
    sweep: Optional[List[Dict[str, Any]]] = None  # [{"params", "label", "pnl_liq", "trades_closed", "verdict_level", "max_drawdown"}]
    warnings: List[str] = field(default_factory=list)
    # ---- added in spec revision 2 ----
    # kind -> {"status": "testable" | "partial" | "not_testable" | "not_replayable", "hours": float, "sentence": str}
    testability: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    study: Optional[Dict[str, Any]] = None  # SignalStudy.summary() over the replay (§6.14)
    overlap_hours: Optional[float] = None  # hours of the window that the live paper run also saw
    stopped_early: bool = False  # hit time_budget_s
