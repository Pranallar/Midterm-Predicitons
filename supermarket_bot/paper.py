"""Read-only paper trading: simulated portfolios on live signals (package C of docs/PAPER_TRADING.md §6).

Nothing here talks to the API directly and nothing can place an order: the engine books
simulated orders and fills *locally* from what the bot observed. :class:`PaperEngine` is pure
(inputs in, state out) so the live tracker and the backtest drive the very same code;
:class:`PaperRunner` adds the read plan (which books and tapes to read, inside its own budget)
on top of an injected :class:`MarketReader` that only has ``book`` and ``trades`` methods, owns the
one lock every mutation takes, and publishes an immutable summary after each step (§6.15).

Honesty rules the engine enforces (§6.3-6.9, revision 2):

* a taker order fills only against an order book observed at least its portfolio's latency after
  the decision (2 s at bot speed, 240 s for the "human:" headline), the first such observation,
  walking its real depth up to the order's limit, never re-using liquidity the same portfolio already
  took (for ``consumed_memory_s``, matched within a tick);
* a resting (maker) order fills only on tape prints strictly through its price after placement, or
  at its price behind a KNOWN displayed queue when the print's taker hit our side;
* settlements are applied before fills; nothing fills on an outcome that left the open list;
* positions are valued at liquidation value; without a real book in the last hour, only 100 shares
  count at the touch and the rest at half of it ("depth unknown", reported as a share of equity);
  frozen positions (closed without a ruling) count 0 ("unvalued");
* the verdict counts every idea entered (open ones at liquidation), uses a two-way cluster-robust
  t-interval, never says more than "promising, not proof" within 48 covered hours, and only the
  pre-registered headline portfolio gets the full ladder.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from . import depth as _depth
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
    SignalEvent,
    SizingPolicy,
    StepReport,
    StrategyInputs,
    TradeRecord,
    Verdict,
    _Serializable,
)

log = logging.getLogger("supermarket_bot")

TICK = _depth.TICK
DEFAULT_START_CAPITAL = 100_000.0
STATE_VERSION = 2

# verdict (§6.9)
MIN_COVERED_HOURS = 12.0  # below: "insufficient"
MIN_IDEAS = 30  # ideas entered (closed + open), non-synthetic, non-frozen
MIN_CLUSTERS = 20  # G = min(race clusters, time-block clusters)
POSITIVE_MIN_HOURS = 48.0  # "positive" needs this much covered time ...
POSITIVE_MIN_DAYS = 2  # ... on at least this many UTC days with >= DAY_MIN_HOURS covered each
DAY_MIN_HOURS = 6.0
TIME_BLOCK_S = 2 * 3600.0  # time clusters: 2-hour blocks of the entry time x party direction
CI_LEVEL = 0.90  # two-sided, for the headline portfolio
FAMILY_ALPHA = 0.10  # exploratory portfolios: two-sided level 1 - FAMILY_ALPHA / n_portfolios
MAX_DEPTH_UNKNOWN_SHARE = 0.20  # more equity marked "unknown": at most "inconclusive"
MAX_SWING_SHARE = 0.10  # a one-sd national swing would move equity by more: at most "inconclusive"
MAX_TOP_RACE_SHARE = 0.50  # one race supplies more of |pnl_liq|: at most "inconclusive"
EXPLORATORY_MAX_LEVEL = "promising"
WILSON_Z = 1.645  # two-sided 90%

# summary payload limits (§10.1)
EQUITY_POINTS_MAX = 300
FILLS_MAX = 100
TRADES_MAX = 100
ORDERS_MAX = 100
POSITIONS_MAX = 200
SEEN_TRADE_IDS_MAX = 500
GAPS_MAX = 50

CAVEATS: Tuple[str, ...] = (
    "A day is a small sample, and trades on different races share one national polling factor, so they are not independent: treat any result as a hint, not proof.",
    "Value and carry ideas pay mostly when races are decided (November 3-4). A one-day test shows them at liquidation value, spread included, so it favours ideas that converge fast and understates ideas meant to be held to resolution.",
    "You will act by hand, minutes after a signal, against bots that react in seconds. The headline portfolio waits about 4 minutes before it can fill; the bot-speed portfolios (30 s) are an upper bound for manual copying, especially for baskets and liquidity holes.",
    "Simulated orders have no market impact: a real order of the same size would move the price against you and could be seen by other bots.",
    "Fills are simulated from order books read after each decision; real fills can be worse (other bots get there first, queue position) or occasionally better.",
    "Profit is shown at liquidation value: what selling every position into the bids you could see would fetch. Marks at the mid and the model's own valuation at fair value are higher and are shown only for reference.",
    "The Cup's end-of-tournament settlement rule is unknown, so positions still open at the end are valued conservatively rather than at 1.00.",
    "Portfolios are separate what-ifs that run on the same signals; they do not compete with each other for the same liquidity.",
    "Nothing was traded: the bot is read-only and every order here is simulated.",
)
# Shown by the UI next to the verdict (not only under "How to read this"): indices into CAVEATS.
VERDICT_CAVEATS: Tuple[int, ...] = (0, 1, 2, 3)
DEMO_CAVEAT = (
    "Demo data: the market is simulated and its mispricings are scripted (the demo's outside prices lead the "
    "Cup by design), so these results say nothing about real profitability."
)
TABLE_WARNING = (
    "The best of {n} portfolios looks better than it is by chance: judge the headline, and confirm any single "
    "kind on data recorded later."
)
FV_MODEL_LABEL = "The model's own opinion of these positions (valued at the outside fair value that generated them): not evidence of profit."


# --------------------------------------------------------------------------- injected interfaces


class PaperPersistence(Protocol):
    """Where a run lives (TrackerStore implements it with SQL tables, §7.1; MemoryPaperPersistence
    in memory for tests and backtests). Dict payloads are the dataclasses' ``to_dict()``."""

    def paper_save_run(self, run_id: str, started_at: float, config: Dict[str, Any], state: Dict[str, Any],
                       updated_at: float) -> None: ...
    def paper_load_run(self, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...
    def paper_last_ended_run(self) -> Optional[Dict[str, Any]]: ...
    def paper_end_run(self, run_id: str, ended_at: float) -> None: ...
    def paper_put_orders(self, run_id: str, orders: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_fills(self, run_id: str, fills: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_trades(self, run_id: str, trades: Sequence[Dict[str, Any]]) -> None: ...
    def paper_add_equity(self, run_id: str, points: Sequence[Dict[str, Any]]) -> None: ...
    def paper_put_events(self, run_id: str, events: Sequence[Dict[str, Any]]) -> None: ...
    def paper_fills(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]: ...
    def paper_trades(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...
    def paper_equity(self, run_id: str, portfolio_id: Optional[str] = None, since: Optional[float] = None) -> List[Dict[str, Any]]: ...
    def paper_orders(self, run_id: str, statuses: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]: ...
    def paper_events(self, run_id: str, kind: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...
    def add_book_snapshots(self, rows: Sequence[Dict[str, Any]]) -> int: ...


@dataclass
class TapeRead(_Serializable):
    """One tape read (up to ``max_pages`` pages of 200 newest-first prints from ``since``)."""

    trades: List[TradeRecord] = field(default_factory=list)  # oldest first, deduplicated by id
    truncated: bool = False  # the last page read was still full: prints between ``since`` and the oldest returned may be missing
    pages: int = 0


class MarketReader(Protocol):
    """The only way the runner reads the market: GETs only, None on any failure (the reader reports
    problems itself). The tracker's implementation also stores what it read."""

    def book(self, exchange_id: str, depth: int) -> Optional[BookObservation]: ...
    def trades(self, exchange_id: str, since: float, max_pages: int = 3) -> Optional[TapeRead]: ...


class MemoryPaperPersistence:
    """In-memory PaperPersistence with the same semantics as the SQL tables (newest-first fills,
    oldest-first equity, upsert orders and events by id, ``paper_load_run(None)`` = newest run not
    ended, ``paper_last_ended_run()`` = the newest ended run)."""

    def __init__(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- read plan


@dataclass
class ReadRequest(_Serializable):
    kind: str  # "book" | "trades"
    exchange_id: str
    # 1 pending taker fills (every leg of a group together), 2 set-entry confirmation books (all legs or
    # none), 3 resting-order tape, 4 position marks (largest liquidation value first), 5 books of exchanges
    # with resting orders (every resting_book_refresh_s), 6 candidate depth (hole candidates, bookless ideas)
    priority: int
    reason: str
    since: Optional[float] = None  # trades: tape wanted after this time (the oldest resting order's cursor)
    after: Optional[float] = None  # books for pending taker orders: earliest acceptable observed_at (decision + latency)
    group: Optional[str] = None  # requests sharing a group are fulfilled all together or not at all (set legs)


@dataclass
class ReadPlan(_Serializable):
    requests: List[ReadRequest] = field(default_factory=list)  # priority order, one per (kind, exchange)


@dataclass
class Fulfilment(_Serializable):
    """What a FulfilFn returns for one plan."""

    books: Dict[str, BookObservation] = field(default_factory=dict)
    tape: Dict[str, TapeRead] = field(default_factory=dict)
    skipped: int = 0  # requests not read (budget, or a group that did not fit)
    book_reads: int = 0
    trade_reads: int = 0  # pages


# --------------------------------------------------------------------------- pure helpers


def contract_levels(book: Optional[BookObservation], side: str, action: str) -> List[Tuple[float, float]]:
    """``depth.contract_levels`` of a BookObservation (empty for None)."""
    if book is None:
        return []
    return _depth.contract_levels(book.bids, book.asks, side, action)


walk = _depth.walk  # (levels, qty, limit, buy) -> depth.WalkResult; one implementation for sizing and fills
WalkResult = _depth.WalkResult


def headline_id(config: PaperConfig) -> str:
    """``config.headline_portfolio`` or ``f"human:{config.sizing}"``; if that id is not among the portfolios,
    the first portfolio's id."""
    raise NotImplementedError


def config_fingerprint(config: PaperConfig, code_version: str) -> str:
    """sha256 hex of the canonical JSON of ``config.to_dict()`` without ``target_hours`` and ``demo``, plus
    ``code_version`` (package version + git commit, pipeline.code_version()). A run continues only while
    this matches (§6.11)."""
    raise NotImplementedError


def liquidation_value(side: str, qty: float, book: Optional[BookObservation], quote: Optional[Quote], *,
                      last_real_book: Optional[BookObservation] = None, now: Optional[float] = None,
                      config: Optional[PaperConfig] = None) -> Tuple[float, str]:
    """(value, depth_state) of selling ``qty`` of ``side`` now (§6.7):

    * "fresh": ``book`` (real, at most mark_book_max_age_s old, best level within one tick of the touch)
      walked with depth.liquidation_proceeds; shares beyond its depth are worth 0;
    * "stale": else ``last_real_book`` at most mark_depth_max_age_s old, its sell-side levels shifted to the
      current touch (depth.shift_levels) and walked the same way;
    * "unknown": else, with a touch bid: min(qty, depth_unknown_full_shares) x touch + the rest x touch x
      (1 - depth_unknown_haircut);
    * no touch at all: (0.0, "unknown").
    Synthetic books (backtest) are never used for marks."""
    raise NotImplementedError


def mid_value(side: str, qty: float, quote: Optional[Quote]) -> Optional[float]:
    """qty x the contract's mark (analytics.mark_price of the quote, 1 - mark for NO)."""
    raise NotImplementedError


def queue_ahead_at(book: Optional[BookObservation], side: str, contract_price: float, now: float,
                   config: PaperConfig) -> Optional[float]:
    """Displayed quantity ahead of a new resting buy of ``side`` at ``contract_price`` (§6.4): the quantity at
    exactly that YES price on our side of ``book`` (YES bids for YES, YES asks for NO), or 0 when the price
    lies strictly inside the displayed range without a level there. None (unknown: only trade-throughs
    fill) when there is no book, the book is older than queue_book_max_age_s, or the price lies beyond the
    last displayed level on that side."""
    raise NotImplementedError


def maker_fills(order: PaperOrder, trades: Sequence[TradeRecord], *, min_latency_s: float,
                queue_haircut: float, now: float) -> Tuple[float, List[str], Optional[float], Optional[float]]:
    """(qty filled, trade ids used, newest print ts used, queue_ahead after) for a resting order (§6.4).
    Prints after ``created_at + min_latency_s``, ``<= now``, ``<= cancel_at`` when cancelling, not seen
    before, oldest first:

    * strictly through the YES-equivalent price -> fill min(remaining, size);
    * exactly at it AND the print's taker hit our side (taker "NO" for a resting YES bid, taker "YES" for a
      resting NO bid = YES ask) AND queue_ahead is known -> the print first reduces queue_ahead (carried to
      the next print and the next step), then queue_haircut x the rest fills;
    * anything else, or queue_ahead None -> no fill from that print."""
    raise NotImplementedError


def t_quantile(p: float, df: int) -> float:
    """Inverse Student-t CDF (stdlib only: regularized incomplete beta + bisection, |error| < 1e-6).
    t_quantile(0.95, 7) = 1.895; t_quantile(0.95, 19) = 1.729."""
    raise NotImplementedError


def cluster_t_interval(values: Sequence[float], clusters_a: Sequence[Any], clusters_b: Sequence[Any], *,
                       level: float = CI_LEVEL) -> Optional[Tuple[float, float, float, int]]:
    """(mean, low, high, G) of a two-way cluster-robust t-interval of the mean (Cameron-Gelbach-Miller):
    e_i = x_i - mean; V_k = sum over clusters of (sum of e in the cluster)^2 / n^2 for k in (a, b, a x b);
    V = V_a + V_b - V_ab (if <= 0: max(V_a, V_b)); G = min(#a, #b); SE = sqrt(V x G / (G - 1));
    half-width = t_quantile(1 - (1 - level) / 2, G - 1) x SE. None when G < 2. Deterministic."""
    raise NotImplementedError


def wilson_interval(wins: int, n: int, z: float = WILSON_Z) -> Optional[Tuple[float, float]]:
    raise NotImplementedError


@dataclass
class IdeaOutcome(_Serializable):
    """One idea a portfolio entered, as the verdict counts it."""

    idea_id: str
    kind: str
    race_key: Optional[str]  # cluster key a (else "market:<first market id>")
    entered_at: float
    direction: str  # "D" | "R" | "N"; cluster key b = (int(entered_at // TIME_BLOCK_S), direction)
    closed: bool
    pnl: float  # realised (closed) or realised + liquidation (open)
    cost: float
    synthetic: bool = False
    frozen: bool = False
    day: str = ""  # UTC date of entered_at, "YYYY-MM-DD"


def compute_verdict(portfolio_id: str, ideas: Sequence[IdeaOutcome], *, pnl_liq: float, covered_hours: float,
                    wall_hours: Optional[float], day_hours: Mapping[str, float], max_drawdown: Optional[float],
                    closed_trades: Sequence[PaperTrade], exploratory: bool, n_portfolios: int,
                    depth_unknown_share: Optional[float], swing_share: Optional[float],
                    unvalued_positions: int, unvalued_value: float, demo: bool = False) -> Verdict:
    """The level, sentence and reasons of §6.9 (exact templates there). ``ideas`` excludes nothing: the
    function drops synthetic and frozen ones itself and counts them in the result."""
    raise NotImplementedError


def downsample_equity(points: Sequence[Tuple[float, float]], max_points: int = EQUITY_POINTS_MAX) -> List[List[float]]:
    """Keep the first and last point and evenly spaced points by time in between."""
    raise NotImplementedError


@dataclass
class CoverageClock(_Serializable):
    """Covered vs wall-clock time of a run (§6.11): each step adds min(gap since the previous step,
    3 x interval) to ``covered_s``; a longer gap is recorded in ``gaps`` (at most GAPS_MAX, newest kept)."""

    interval_s: float = 30.0
    started_at: Optional[float] = None
    last_step_at: Optional[float] = None
    covered_s: float = 0.0
    day_covered_s: Dict[str, float] = field(default_factory=dict)  # UTC date -> covered seconds
    gaps: List[Dict[str, float]] = field(default_factory=list)  # [{"from", "to"}]

    def tick(self, now: float) -> Optional[float]:
        """Record a step at ``now``; returns the gap length when it exceeded 3 x interval, else None."""
        raise NotImplementedError

    @property
    def covered_hours(self) -> float:
        return self.covered_s / 3600.0

    def wall_hours(self, now: float) -> float:
        raise NotImplementedError


# --------------------------------------------------------------------------- engine


class PaperEngine:
    """Simulated portfolios side by side on one stream of signals (§6.2-6.9).

    ``policies`` maps policy names to SizingPolicy objects (default: sizing.get_policy for each
    name the portfolios use). ``persistence`` defaults to MemoryPaperPersistence. On construction
    the newest unfinished run is loaded and continued when its config fingerprint matches
    (``config_fingerprint(config, code_version)``); otherwise it is ended with reason "settings changed"
    (final snapshot written) and a run starts at the first step. ``study`` defaults to a
    study.SignalStudy (the signal-level event study, §6.14). Not thread-safe: PaperRunner serialises.
    """

    def __init__(self, config: Optional[PaperConfig] = None, *, persistence: Optional[PaperPersistence] = None,
                 policies: Optional[Mapping[str, SizingPolicy]] = None, clock: Callable[[], float] = time.time,
                 code_version: str = "", study: Any = None) -> None:
        raise NotImplementedError

    # lifecycle
    @property
    def run_id(self) -> Optional[str]:
        raise NotImplementedError

    @property
    def config(self) -> PaperConfig:
        raise NotImplementedError

    def start(self, now: float, start_capital: Optional[float] = None, capital_source: str = "") -> str:
        """Begin a new run (ending the current one with reason "reset") with every portfolio at ``start_capital``."""
        raise NotImplementedError

    def end_run(self, now: float, reason: str) -> Optional[Dict[str, Any]]:
        """End the current run: open positions are recorded as closed trades at liquidation value with
        ``exit_reason=reason`` ("reset" | "settings changed" | "completed"), the final snapshot
        ``{"at", "reason", "verdicts", "portfolios", "study"}`` is written into the state, then
        ``paper_end_run``. Returns the final snapshot (None when no run was active)."""
        raise NotImplementedError

    def reset(self, now: float, start_capital: Optional[float] = None, capital_source: str = "",
              target_hours: Optional[float] = None) -> str:
        """``end_run(now, "reset")`` then ``start``. Returns the new run id."""
        raise NotImplementedError

    # one step
    def wanted_reads(self, now: float, obs: MarketObservation, candidates: Sequence[str] = ()) -> ReadPlan:
        """What this step needs read, by priority (§6.10). The caller trims it to the budget."""
        raise NotImplementedError

    def resting_exchanges(self) -> Set[str]:
        """Exchanges where any portfolio has a resting or cancelling maker order (StrategyInputs.resting_exchanges)."""
        raise NotImplementedError

    def step(self, now: float, obs: MarketObservation, signals: Sequence[Opportunity],
             inputs: Optional[StrategyInputs] = None, *, tape_truncated: Sequence[str] = ()) -> StepReport:
        """Phases (§6.2): 0 coverage clock and gap handling, 1 settlements and frozen outcomes, 2 taker fills,
        3 maker fills, 4 marks, 5 exits, 6 entries (sized per portfolio), 7 event study, 8 equity points,
        9 persistence (and the final snapshot when covered hours first reach target_hours)."""
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
        """In-memory equity points (at most one per equity_min_spacing_s per portfolio), oldest first."""
        raise NotImplementedError

    def ideas(self, portfolio_id: str) -> List[IdeaOutcome]:
        raise NotImplementedError

    def verdicts(self, now: Optional[float] = None) -> Dict[str, Verdict]:
        raise NotImplementedError

    def events(self, kind: Optional[str] = None) -> List[SignalEvent]:
        raise NotImplementedError

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        """The ``/api/paper`` body minus the runner's ``budget`` and the tracker's extras (§10.1). Builds
        everything from memory (no persistence reads)."""
        raise NotImplementedError

    def state(self) -> Dict[str, Any]:
        """The JSON state persisted after every step (§6.11)."""
        raise NotImplementedError


# --------------------------------------------------------------------------- runner


InputsFn = Callable[[float], Tuple[StrategyInputs, MarketObservation]]
SignalFn = Callable[..., List[Opportunity]]
FulfilFn = Callable[[ReadPlan, float], Fulfilment]
CandidatesFn = Callable[[StrategyInputs], List[str]]


def run_step(engine: PaperEngine, inputs: StrategyInputs, obs: MarketObservation, fulfil: FulfilFn, *,
             signal_fn: Optional[SignalFn] = None, candidates_fn: Optional[CandidatesFn] = None,
             decided_at: Optional[Callable[[], float]] = None) -> StepReport:
    """The one step sequence shared by the live runner and the backtest (§6.2, §6.12):

    1. ``inputs.resting_exchanges = engine.resting_exchanges()``;
       ``plan = engine.wanted_reads(obs.now, obs, candidates_fn(inputs))`` (default candidates:
       strategy.hole_candidates with ``inputs.params``);
    2. ``f = fulfil(plan, obs.now)`` merged into ``obs`` (a fresh book replaces an older one; tape is
       appended, deduplicated by id) and attached to the inputs' quotes as stored books;
    3. ``signals = signal_fn(inputs, inputs.params)`` (default strategy.generate_signals);
    4. ``engine.step(now, obs, signals, inputs, tape_truncated=[e for e, t in f.tape.items() if t.truncated])``
       with ``now = decided_at()`` when given (live: the clock after the reads), else ``obs.now``; the
       read counts and ``f.skipped`` go into the report.
    """
    raise NotImplementedError


class PaperRunner:
    """One live paper step = inputs -> read plan -> reads (budgeted) -> signals -> engine.step -> publish.

    ``inputs_fn(now)`` (package E, pipeline.assemble_inputs) builds the strategy inputs and the base
    observation from what the tracker already holds, without reads. ``signal_fn`` defaults to
    strategy.generate_signals. ``limiter`` is the tracker's paper budget: an object with ``used``,
    ``limit`` and ``room()``; a read is made only while ``limiter.room() > 0`` (never blocks), at most
    ``max_book_reads_per_step`` books and ``max_trade_reads_per_step`` tape pages per step; request groups
    (set legs) are read all together or skipped together.

    Concurrency (§6.15): one ``threading.RLock`` serialises ``step``, ``reset`` and ``end_run``. After every
    step and every reset the runner builds the full ``/api/paper`` body once (verdicts, study, downsampled
    equity) and publishes it by swapping one attribute; ``summary()`` returns the published dict WITHOUT
    taking the lock (callers must not mutate it). ``extras_fn()`` (optional, called while publishing) adds
    keys such as ``fair_value``; it must not take the tracker's lock.
    """

    def __init__(self, engine: PaperEngine, reader: MarketReader, inputs_fn: InputsFn, *,
                 signal_fn: Optional[SignalFn] = None, limiter: Any = None,
                 clock: Callable[[], float] = time.time, interval: Optional[float] = None,
                 extras_fn: Optional[Callable[[], Mapping[str, Any]]] = None) -> None:
        raise NotImplementedError

    def step(self, now: Optional[float] = None) -> StepReport:
        """Never raises for data problems (they go to StepReport.errors); re-raises only shutdown."""
        raise NotImplementedError

    def summary(self) -> Dict[str, Any]:
        """The last published ``/api/paper`` body (§10.1), with ``budget``. Lock-free."""
        raise NotImplementedError

    def previous(self) -> Optional[Dict[str, Any]]:
        """The final snapshot of the newest ENDED run (``/api/paper?run=previous``), or None."""
        raise NotImplementedError

    def reset(self, now: Optional[float] = None, start_capital: Optional[float] = None,
              target_hours: Optional[float] = None, capital_source: str = "") -> str:
        raise NotImplementedError

    def end_run(self, now: Optional[float] = None, reason: str = "completed") -> Optional[Dict[str, Any]]:
        """Under the lock: ``engine.end_run`` and publish. ``paper --hours N`` calls it when done (§7.5)."""
        raise NotImplementedError

    @property
    def last_report(self) -> Optional[StepReport]:
        raise NotImplementedError
