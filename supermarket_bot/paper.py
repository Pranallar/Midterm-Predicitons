"""Read-only paper trading: simulated portfolios on live signals (package C of docs/PAPER_TRADING.md §6).

Nothing here talks to the API directly and nothing can place an order: the engine books
simulated orders and fills *locally* from what the bot observed. :class:`PaperEngine` is pure
(inputs in, state out) so the live tracker and the backtest drive the very same code;
:class:`PaperRunner` adds the read plan (which books and tapes to read, inside its own budget)
on top of an injected :class:`MarketReader` that only has ``book`` and ``trades`` methods.

Honesty rules the engine enforces (§6.3-6.8):

* a taker order fills only against an order book observed at least ``min_latency_s`` after the
  decision, the first such observation, walking its real depth up to the order's limit, never
  re-using liquidity the same portfolio already took (until ``consumed_memory_s`` passes);
* a resting (maker) order fills only on tape prints strictly through its price after placement
  (or at its price behind the displayed queue, times ``queue_haircut``), capped by the printed size;
* no fills, decisions or exits on an outcome whose bulk quote is older than ``stale_quote_s``;
* positions are valued at liquidation value (selling into the bids actually seen), with the mid
  mark and the fair value only as references; settlement pays 1/0 (refunds return the cost);
  an outcome that closes without a ruling is frozen and flagged;
* the verdict reports closed-trade counts, a cluster-bootstrap interval, the P&L without the
  single best trade and says "not enough evidence yet" below explicit thresholds.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import (
    BookObservation,
    EquityPoint,
    MarketObservation,
    Opportunity,
    PaperConfig,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperTrade,
    PortfolioSummary,
    Quote,
    SizingPolicy,
    StepReport,
    StrategyInputs,
    TradeRecord,
    Verdict,
    _Serializable,
)

log = logging.getLogger("supermarket_bot")

TICK = 0.005
DEFAULT_START_CAPITAL = 100_000.0
STATE_VERSION = 1

# verdict (§6.9)
MIN_HOURS_FOR_VERDICT = 12.0
MIN_CLOSED_TRADES = 20
MIN_CLUSTERS = 8
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 12345
CI_LEVEL = 0.90
WILSON_Z = 1.645  # two-sided 90%

# summary payload limits (§10.1)
EQUITY_POINTS_MAX = 300
FILLS_MAX = 100
TRADES_MAX = 100
ORDERS_MAX = 100
POSITIONS_MAX = 200
SEEN_TRADE_IDS_MAX = 500

CAVEATS: Tuple[str, ...] = (
    "A day is a small sample, and trades on different races share one national polling factor, so they are not independent: treat any result as a hint, not proof.",
    "Fills are simulated from order books read after each decision; real fills can be worse (other bots get there first, queue position) or occasionally better.",
    "Profit is shown at liquidation value: what selling every position into the bids you could see would fetch. Marks at the mid are higher and are shown only for reference.",
    "The Cup's end-of-tournament settlement rule is unknown, so positions still open at the end are valued conservatively rather than at 1.00.",
    "Portfolios are separate what-ifs that run on the same signals; they do not compete with each other for the same liquidity.",
    "Nothing was traded: the bot is read-only and every order here is simulated.",
)
DEMO_CAVEAT = "Demo data: the market is simulated and its mispricings are scripted, so these results say nothing about real profitability."


# --------------------------------------------------------------------------- injected interfaces


class PaperPersistence(Protocol):
    """Where a run lives (TrackerStore implements it with SQL tables, §7.1; MemoryPaperPersistence
    in memory for tests and backtests). Dict payloads are the dataclasses' ``to_dict()``."""

    def paper_save_run(self, run_id: str, started_at: float, config: Dict[str, Any], state: Dict[str, Any],
                       updated_at: float) -> None: ...
    def paper_load_run(self, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...
    def paper_end_run(self, run_id: str, ended_at: float) -> None: ...
    def paper_put_orders(self, run_id: str, orders: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_fills(self, run_id: str, fills: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_trades(self, run_id: str, trades: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_equity(self, run_id: str, points: Sequence[Dict[str, Any]]) -> None: ...
    def paper_fills(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]: ...
    def paper_trades(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...
    def paper_equity(self, run_id: str, portfolio_id: Optional[str] = None, since: Optional[float] = None) -> List[Dict[str, Any]]: ...
    def paper_orders(self, run_id: str, statuses: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]: ...
    def add_book_snapshots(self, rows: Sequence[Dict[str, Any]]) -> int: ...


class MarketReader(Protocol):
    """The only way the runner reads the market: one GET each, None on any failure (the reader
    reports problems itself). The tracker's implementation also stores what it read."""

    def book(self, exchange_id: str, depth: int) -> Optional[BookObservation]: ...
    def trades(self, exchange_id: str, since: float) -> Optional[List[TradeRecord]]: ...


class MemoryPaperPersistence:
    """In-memory PaperPersistence with the same semantics as the SQL tables (newest-first fills,
    oldest-first equity, upsert orders by id, ``paper_load_run(None)`` = newest run not ended)."""

    def __init__(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- read plan


@dataclass
class ReadRequest(_Serializable):
    kind: str  # "book" | "trades"
    exchange_id: str
    priority: int  # 1 pending taker fills, 2 resting-order tape, 3 position marks, 4 candidate depth
    reason: str
    since: Optional[float] = None  # trades: tape wanted after this time (the oldest resting order's cursor)
    after: Optional[float] = None  # books for pending taker orders: earliest acceptable observed_at (decision + min latency)


@dataclass
class ReadPlan(_Serializable):
    requests: List[ReadRequest] = field(default_factory=list)  # priority order, one per (kind, exchange)


# --------------------------------------------------------------------------- pure helpers


def contract_levels(book: BookObservation, side: str, action: str) -> List[Tuple[float, float]]:
    """The levels an order trades against, in CONTRACT prices, best first:
    buy YES -> YES asks; buy NO -> YES bids as 1 - bid; sell YES -> YES bids; sell NO -> YES asks as 1 - ask."""
    raise NotImplementedError


@dataclass
class WalkResult(_Serializable):
    filled: float
    avg_price: Optional[float]
    levels: List[Tuple[float, float]] = field(default_factory=list)  # (contract price, qty) taken


def walk(levels: Sequence[Tuple[float, float]], qty: float, limit: float, buy: bool) -> WalkResult:
    """Take up to ``qty`` from ``levels`` (best first) at prices no worse than ``limit``
    (<= for buys, >= for sells). Whole shares only (floor)."""
    raise NotImplementedError


def liquidation_value(side: str, qty: float, book: Optional[BookObservation], quote: Optional[Quote]) -> Tuple[float, bool]:
    """(value, depth_known) of selling ``qty`` of ``side`` now: walk the book's bids (YES) / asks
    (NO, at 1 - ask); shares beyond the book's depth are worth 0. Without a book: qty x the touch,
    depth_known False. No touch at all: 0."""
    raise NotImplementedError


def mid_value(side: str, qty: float, quote: Optional[Quote]) -> Optional[float]:
    """qty x the contract's mark (analytics.mark_price of the quote, 1 - mark for NO)."""
    raise NotImplementedError


def maker_fills(order: PaperOrder, trades: Sequence[TradeRecord], *, min_latency_s: float,
                queue_haircut: float, now: float) -> Tuple[float, List[str], Optional[float]]:
    """(qty filled, trade ids used, newest print ts used) for a resting order (§6.4): prints after
    ``created_at + min_latency_s`` and ``<= now`` not seen before; strictly through the YES-equivalent
    price -> fill min(remaining, size); exactly at it -> first consume ``queue_ahead``, then fill
    ``queue_haircut`` x the rest; ``queue_ahead`` None -> at-price prints never fill."""
    raise NotImplementedError


def bootstrap_ci(values: Sequence[float], clusters: Sequence[str], *, resamples: int = BOOTSTRAP_RESAMPLES,
                 level: float = CI_LEVEL, seed: int = BOOTSTRAP_SEED) -> Optional[Tuple[float, float]]:
    """Cluster bootstrap of the mean: resample clusters (races) with replacement, pool their values,
    take the mean; percentile interval. None with fewer than 2 clusters. Deterministic (seeded)."""
    raise NotImplementedError


def wilson_interval(wins: int, n: int, z: float = WILSON_Z) -> Optional[Tuple[float, float]]:
    raise NotImplementedError


def compute_verdict(portfolio_id: str, trades: Sequence[PaperTrade], pnl_liq: float, hours_run: float,
                    max_drawdown: Optional[float], *, demo: bool = False) -> Verdict:
    """The level and sentence of §6.9 (exact templates there)."""
    raise NotImplementedError


def downsample_equity(points: Sequence[Tuple[float, float]], max_points: int = EQUITY_POINTS_MAX) -> List[List[float]]:
    """Keep the first and last point and evenly spaced points by time in between."""
    raise NotImplementedError


# --------------------------------------------------------------------------- engine


class PaperEngine:
    """Simulated portfolios side by side on one stream of signals (§6.2-6.8).

    ``policies`` maps policy names to SizingPolicy objects (default: sizing.get_policy for each
    name the portfolios use). ``persistence`` defaults to MemoryPaperPersistence. On construction
    the newest unfinished run is loaded and continued; otherwise a run starts at the first step.
    """

    def __init__(self, config: Optional[PaperConfig] = None, *, persistence: Optional[PaperPersistence] = None,
                 policies: Optional[Mapping[str, SizingPolicy]] = None, clock: Callable[[], float] = time.time) -> None:
        raise NotImplementedError

    # lifecycle
    @property
    def run_id(self) -> Optional[str]:
        raise NotImplementedError

    def start(self, now: float, start_capital: Optional[float] = None, capital_source: str = "") -> str:
        """Begin a new run (ending the current one) with every portfolio at ``start_capital``."""
        raise NotImplementedError

    def reset(self, now: float, start_capital: Optional[float] = None, capital_source: str = "",
              target_hours: Optional[float] = None) -> str:
        """End the current run (open positions are recorded as closed trades with exit_reason
        "reset" at liquidation value) and start a fresh one. Returns the new run id."""
        raise NotImplementedError

    # one step
    def wanted_reads(self, now: float, obs: MarketObservation, candidates: Sequence[str] = ()) -> ReadPlan:
        """What this step needs read, by priority (§6.10): 1 books for pending taker orders, 2 tape for
        resting orders, 3 books for open positions' marks, 4 books for ``candidates`` (hole candidates)
        and for last step's sized signals that had no recent book. The caller trims it to the budget."""
        raise NotImplementedError

    def step(self, now: float, obs: MarketObservation, signals: Sequence[Opportunity],
             inputs: Optional[StrategyInputs] = None) -> StepReport:
        """Phases (§6.2): 1 taker fills, 2 maker fills, 3 settlements and frozen outcomes, 4 marks,
        5 exits, 6 entries (sized per portfolio), 7 equity points, 8 persistence."""
        raise NotImplementedError

    # reading
    def portfolios(self, now: Optional[float] = None) -> List[PortfolioSummary]:
        raise NotImplementedError

    def positions(self, portfolio_id: Optional[str] = None) -> List[PaperPosition]:
        raise NotImplementedError

    def orders(self, portfolio_id: Optional[str] = None, open_only: bool = True) -> List[PaperOrder]:
        raise NotImplementedError

    def fills(self, portfolio_id: Optional[str] = None, limit: int = FILLS_MAX) -> List[PaperFill]:
        raise NotImplementedError

    def trades(self, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[PaperTrade]:
        raise NotImplementedError

    def equity(self, portfolio_id: Optional[str] = None) -> Dict[str, List[EquityPoint]]:
        raise NotImplementedError

    def verdicts(self, now: Optional[float] = None) -> Dict[str, Verdict]:
        raise NotImplementedError

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        """The ``/api/paper`` body minus the runner's ``budget`` and ``fair_value`` parts (§10.1)."""
        raise NotImplementedError

    def state(self) -> Dict[str, Any]:
        """The JSON state persisted after every step (§6.11)."""
        raise NotImplementedError


# --------------------------------------------------------------------------- runner


InputsFn = Callable[[float], Tuple[StrategyInputs, MarketObservation]]
SignalFn = Callable[..., List[Opportunity]]
# (plan, now) -> (fresh books by exchange id, fresh tape by exchange id, requests skipped)
FulfilFn = Callable[[ReadPlan, float], Tuple[Dict[str, BookObservation], Dict[str, List[TradeRecord]], int]]
CandidatesFn = Callable[[StrategyInputs], List[str]]


def run_step(engine: PaperEngine, inputs: StrategyInputs, obs: MarketObservation, fulfil: FulfilFn, *,
             signal_fn: Optional[SignalFn] = None, candidates_fn: Optional[CandidatesFn] = None,
             decided_at: Optional[Callable[[], float]] = None) -> StepReport:
    """The one step sequence shared by the live runner and the backtest (§6.2, §6.12):

    1. ``plan = engine.wanted_reads(obs.now, obs, candidates_fn(inputs))`` (default candidates:
       strategy.hole_candidates with ``inputs.params``);
    2. ``books, tape, skipped = fulfil(plan, obs.now)`` merged into ``obs`` (a fresh book replaces an
       older one; tape is appended);
    3. ``signals = signal_fn(inputs, inputs.params)`` (default strategy.generate_signals);
    4. ``engine.step(now, obs, signals, inputs)`` with ``now = decided_at()`` when given (live: the
       clock after the reads, so every book read in this step predates the decisions), else ``obs.now``.
    """
    raise NotImplementedError


class PaperRunner:
    """One live paper step = inputs -> read plan -> reads (budgeted) -> signals -> engine.step.

    ``inputs_fn(now)`` (package E, pipeline.assemble_inputs) builds the strategy inputs and the base
    observation from what the tracker already holds, without reads. ``signal_fn`` defaults to
    strategy.generate_signals. ``limiter`` is the tracker's paper SlidingWindowLimiter: a read is made
    only while ``limiter.used < limiter.limit`` (never blocks), at most ``max_book_reads_per_step``
    books and ``max_trade_reads_per_step`` tapes per step.
    """

    def __init__(self, engine: PaperEngine, reader: MarketReader, inputs_fn: InputsFn, *,
                 signal_fn: Optional[SignalFn] = None, limiter: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        raise NotImplementedError

    def step(self, now: Optional[float] = None) -> StepReport:
        """Never raises for data problems (they go to StepReport.errors); re-raises only shutdown."""
        raise NotImplementedError

    def summary(self) -> Dict[str, Any]:
        """The full ``/api/paper`` body (§10.1), with ``budget``."""
        raise NotImplementedError

    def reset(self, now: Optional[float] = None, start_capital: Optional[float] = None,
              target_hours: Optional[float] = None, capital_source: str = "") -> str:
        raise NotImplementedError

    @property
    def last_report(self) -> Optional[StepReport]:
        raise NotImplementedError

