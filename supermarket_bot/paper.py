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

import copy
import dataclasses
import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from . import depth as _depth
from .models import (
    SET_KINDS,
    TRADE_KINDS,
    BookObservation,
    EquityPoint,
    ExitPlan,
    ExposureSummary,
    MarketObservation,
    Opportunity,
    PaperConfig,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperTrade,
    PortfolioSpec,
    PortfolioSummary,
    Quote,
    SignalEvent,
    SizeDecision,
    SizingContext,
    SizingPolicy,
    StepReport,
    StrategyInputs,
    StrategyParams,
    TradeRecord,
    Verdict,
    _Serializable,
)

log = logging.getLogger("supermarket_bot")

TICK = _depth.TICK
DEFAULT_START_CAPITAL = 100_000.0
DEFAULT_CAPITAL_SOURCE = "default 100,000"  # the last-resort start capital (§6.1): no setting, no account value yet
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
RECENT_FILLS_KEPT = 1000  # in memory, newest; the store keeps every fill

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

TARGET_REACHED = "target reached"  # reason of the snapshot written when covered hours first reach target_hours
NO_DATA_GAP = "no data"  # CoverageClock gap reason: steps ran but saw no fresh quote (an outage)
# trades the END of a run creates at liquidation value (end_run): never realised, never closed ideas
END_REASONS = frozenset({"completed", "reset", "settings changed"})

_EPS = 1e-9
_PROPAGATE_NAMES = ("_Stopping", "SimClockStall", "ReadOnlyViolation", "AuthenticationError", "FatalKeyError")

# "why no trades" clauses for blocked entries (§6.13)
_BLOCK_TEXT = {
    "deferred": "waiting for fresh books of every leg (budget)",
    "stale_quote": "the quote was stale",
    "closed": "the outcome was not open",
    "held": "the idea was already held or ordered",
    "exchange": "another position or order was open on the same outcome",
    "cooldown": "it was in its re-entry cooldown",
    "max_positions": "the portfolio held its maximum number of positions",
    "sized_zero": "the sizing policy gave it no size",
    "order_cap": "the per-step order cap was reached",
    "cash": "there was not enough free cash",
    "cup_end": "the Cup has ended",
    "replace": "a held idea is being sold first",
    "no_limit": "it had no limit price",
    "sizing_error": "the sizing policy failed",
}


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


def _jsonable(value: Any) -> Any:
    """A deep, JSON-safe copy (what a SQL round trip returns)."""
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


class MemoryPaperPersistence:
    """In-memory PaperPersistence with the same semantics as the SQL tables (newest-first fills,
    oldest-first equity, upsert orders and events by id, ``paper_load_run(None)`` = newest run not
    ended, ``paper_last_ended_run()`` = the newest ended run).

    Orderings (the SQL implementation must match, §7.8 contract test): fills and trades newest first
    (by ``ts`` / ``closed_at``, ties: the later insert first); equity oldest first (by ``ts``, ties by
    portfolio id); orders oldest first (by ``created_at``, ties by insert order); events oldest first
    (by ``t0``, ties by event id); runs by ``started_at`` (ties: the later save). Every returned payload is
    a deep JSON copy."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._run_seq: Dict[str, int] = {}
        self._orders: Dict[str, Dict[str, Tuple[int, Dict[str, Any]]]] = {}
        self._fills: Dict[str, Dict[str, Tuple[int, Dict[str, Any]]]] = {}
        self._trades: Dict[str, Dict[str, Tuple[int, Dict[str, Any]]]] = {}
        self._equity: Dict[str, Dict[Tuple[str, float], Dict[str, Any]]] = {}
        self._events: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.book_rows: List[Dict[str, Any]] = []
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    # runs
    def paper_save_run(self, run_id: str, started_at: float, config: Dict[str, Any], state: Dict[str, Any],
                       updated_at: float) -> None:
        with self._lock:
            old = self._runs.get(run_id)
            self._runs[run_id] = {
                "run_id": run_id, "started_at": float(started_at), "config": _jsonable(config),
                "state": _jsonable(state), "updated_at": float(updated_at),
                "ended_at": old.get("ended_at") if old else None,
            }
            if run_id not in self._run_seq:
                self._run_seq[run_id] = self._next()

    def paper_load_run(self, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            if run_id is not None:
                rec = self._runs.get(run_id)
                return _jsonable(rec) if rec is not None else None
            open_runs = [r for r in self._runs.values() if r.get("ended_at") is None]
            if not open_runs:
                return None
            best = max(open_runs, key=lambda r: (r["started_at"], self._run_seq.get(r["run_id"], 0)))
            return _jsonable(best)

    def paper_last_ended_run(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            ended = [r for r in self._runs.values() if r.get("ended_at") is not None]
            if not ended:
                return None
            best = max(ended, key=lambda r: (r["ended_at"], r["started_at"], self._run_seq.get(r["run_id"], 0)))
            return _jsonable(best)

    def paper_end_run(self, run_id: str, ended_at: float) -> None:
        with self._lock:
            rec = self._runs.get(run_id)
            if rec is not None:
                rec["ended_at"] = float(ended_at)

    # rows
    def paper_put_orders(self, run_id: str, orders: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            table = self._orders.setdefault(run_id, {})
            for o in orders:
                oid = str(o["order_id"])
                seq = table[oid][0] if oid in table else self._next()
                table[oid] = (seq, _jsonable(o))

    def paper_add_fills(self, run_id: str, fills: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            table = self._fills.setdefault(run_id, {})
            for f in fills:
                fid = str(f["fill_id"])
                seq = table[fid][0] if fid in table else self._next()
                table[fid] = (seq, _jsonable(f))

    def paper_add_trades(self, run_id: str, trades: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            table = self._trades.setdefault(run_id, {})
            for t in trades:
                tid = str(t["trade_id"])
                seq = table[tid][0] if tid in table else self._next()
                table[tid] = (seq, _jsonable(t))

    def paper_add_equity(self, run_id: str, points: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            table = self._equity.setdefault(run_id, {})
            for p in points:
                table[(str(p["portfolio_id"]), float(p["ts"]))] = _jsonable(p)

    def paper_put_events(self, run_id: str, events: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            table = self._events.setdefault(run_id, {})
            for e in events:
                table[str(e["event_id"])] = _jsonable(e)

    def paper_fills(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [(seq, d) for seq, d in self._fills.get(run_id, {}).values()
                    if portfolio_id is None or d.get("portfolio_id") == portfolio_id]
            rows.sort(key=lambda r: (r[1].get("ts", 0.0), r[0]), reverse=True)
            out = [_jsonable(d) for _, d in rows]
            return out if limit is None else out[: int(limit)]

    def paper_trades(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [(seq, d) for seq, d in self._trades.get(run_id, {}).values()
                    if portfolio_id is None or d.get("portfolio_id") == portfolio_id]
            rows.sort(key=lambda r: (r[1].get("closed_at", 0.0), r[0]), reverse=True)
            out = [_jsonable(d) for _, d in rows]
            return out if limit is None else out[: int(limit)]

    def paper_equity(self, run_id: str, portfolio_id: Optional[str] = None, since: Optional[float] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [d for (pid, ts), d in self._equity.get(run_id, {}).items()
                    if (portfolio_id is None or pid == portfolio_id) and (since is None or ts >= since)]
            rows.sort(key=lambda d: (d["ts"], d["portfolio_id"]))
            return [_jsonable(d) for d in rows]

    def paper_orders(self, run_id: str, statuses: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        with self._lock:
            wanted = set(statuses) if statuses is not None else None
            rows = [(seq, d) for seq, d in self._orders.get(run_id, {}).values()
                    if wanted is None or d.get("status") in wanted]
            rows.sort(key=lambda r: (r[1].get("created_at", 0.0), r[0]))
            return [_jsonable(d) for _, d in rows]

    def paper_events(self, run_id: str, kind: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [d for d in self._events.get(run_id, {}).values() if kind is None or d.get("kind") == kind]
            rows.sort(key=lambda d: (d.get("t0", 0.0), d.get("event_id", "")))
            out = [_jsonable(d) for d in rows]
            return out if limit is None else out[: int(limit)]

    def add_book_snapshots(self, rows: Sequence[Any]) -> int:
        with self._lock:
            n = 0
            for r in rows:
                if isinstance(r, BookObservation):
                    r = {"exchange_id": r.exchange_id, "ts": r.observed_at, "source": r.source,
                         "bids": [list(x) for x in r.bids], "asks": [list(x) for x in r.asks], "sequence": r.sequence}
                self.book_rows.append(_jsonable(dict(r)))
                n += 1
            return n


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


def fulfil_plan(plan: ReadPlan, *, read_book: Callable[[ReadRequest], Optional[BookObservation]],
                read_tape: Callable[[ReadRequest, int], Optional[TapeRead]], max_books: int, max_pages: int,
                max_tape_pages: int = 3, room: Optional[Callable[[], int]] = None) -> Fulfilment:
    """Fulfil ``plan`` in priority order within the per-step caps (§6.10), shared by the live runner and
    the backtest adapter: at most ``max_books`` book reads and ``max_pages`` tape pages, and (live) only
    while ``room()`` > 0; a group is read only when every member fits, else every member is skipped; a
    reader returning None is a failed read (counted, no retry this step)."""
    f = Fulfilment()
    groups: Dict[str, List[ReadRequest]] = {}
    for r in plan.requests:
        if r.group:
            groups.setdefault(r.group, []).append(r)
    decided: Set[str] = set()
    done: Set[Tuple[str, str]] = set()

    def room_left() -> float:
        if room is None:
            return float("inf")
        try:
            return float(room())
        except Exception:  # a broken limiter reads nothing
            return 0.0

    def do_book(r: ReadRequest) -> None:
        f.book_reads += 1
        obs = read_book(r)
        if obs is not None:
            f.books[r.exchange_id] = obs

    def do_tape(r: ReadRequest, pages: int) -> None:
        tape = read_tape(r, pages)
        if tape is None:
            f.trade_reads += 1
            return
        f.trade_reads += max(1, int(tape.pages or 0))
        f.tape[r.exchange_id] = tape

    for r in plan.requests:
        key = (r.kind, r.exchange_id)
        if key in done:
            continue
        if r.group:
            if r.group in decided:
                continue
            decided.add(r.group)
            members = [m for m in groups[r.group] if (m.kind, m.exchange_id) not in done]
            n_books = sum(1 for m in members if m.kind == "book")
            n_pages = sum(1 for m in members if m.kind != "book")
            fits = (f.book_reads + n_books <= max_books and f.trade_reads + n_pages <= max_pages
                    and room_left() >= n_books + n_pages)
            for m in members:
                done.add((m.kind, m.exchange_id))
                if not fits:
                    f.skipped += 1
                elif m.kind == "book":
                    do_book(m)
                else:
                    do_tape(m, 1)
            continue
        done.add(key)
        if r.kind == "book":
            if f.book_reads >= max_books or room_left() < 1:
                f.skipped += 1
                continue
            do_book(r)
        else:
            pages = min(int(max_tape_pages), int(max_pages) - f.trade_reads)
            left = room_left()
            if left != float("inf"):
                pages = min(pages, int(left))
            if pages < 1:
                f.skipped += 1
                continue
            do_tape(r, pages)
    return f


# --------------------------------------------------------------------------- pure helpers


def contract_levels(book: Optional[BookObservation], side: str, action: str) -> List[Tuple[float, float]]:
    """``depth.contract_levels`` of a BookObservation (empty for None)."""
    if book is None:
        return []
    return _depth.contract_levels(book.bids, book.asks, side, action)


walk = _depth.walk  # (levels, qty, limit, buy) -> depth.WalkResult; one implementation for sizing and fills
WalkResult = _depth.WalkResult


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def _plural(n: float, word: str) -> str:
    return word if abs(n - 1) < 1e-9 else word + "s"


def _r6(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 6)


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _contract_bid(quote: Optional[Quote], side: str) -> Optional[float]:
    """The contract's touch bid (what selling it fetches): YES bid, or 1 - YES ask for NO."""
    if quote is None:
        return None
    if side == "yes":
        b = _num(quote.bid)
        return b if b is not None and b > 0 else None
    a = _num(quote.ask)
    if a is None or a >= 1.0 or a <= 0:
        return None
    return round(1.0 - a, 6)


def _contract_ask(quote: Optional[Quote], side: str) -> Optional[float]:
    """The contract's touch ask (what buying it costs): YES ask, or 1 - YES bid for NO."""
    if quote is None:
        return None
    if side == "yes":
        a = _num(quote.ask)
        return a if a is not None and 0 < a < 1 else None
    b = _num(quote.bid)
    if b is None or b <= 0 or b >= 1:
        return None
    return round(1.0 - b, 6)


def _yes_price(side: str, contract_price: float) -> float:
    return round(contract_price if side == "yes" else 1.0 - contract_price, 6)


def _book_side(side: str, action: str) -> str:
    """Which YES side of the book an order takes: buy YES / sell NO take the asks, the others the bids."""
    if action == "buy":
        return "asks" if side == "yes" else "bids"
    return "bids" if side == "yes" else "asks"


def _is_real(book: Optional[BookObservation]) -> bool:
    return book is not None and str(book.source or "") != "synthetic"


def headline_id(config: PaperConfig) -> str:
    """``config.headline_portfolio`` or ``f"human:{config.sizing}"``; if that id is not among the portfolios,
    the first portfolio's id."""
    wanted = config.headline_portfolio or f"human:{config.sizing}"
    ids = [p.portfolio_id for p in config.portfolios]
    if wanted in ids or not ids:
        return wanted
    return ids[0]


def config_fingerprint(config: PaperConfig, code_version: str) -> str:
    """sha256 hex of the canonical JSON of ``config.to_dict()`` without ``target_hours`` and ``demo``, plus
    ``code_version`` (package version + git commit, pipeline.code_version()). A run continues only while
    this matches (§6.11)."""
    data = config.to_dict()
    data.pop("target_hours", None)
    data.pop("demo", None)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256((text + "|" + str(code_version or "")).encode("utf-8")).hexdigest()


LevelsAdjust = Callable[[List[Tuple[float, float]]], List[Tuple[float, float]]]


def _shift_by(levels: Sequence[Tuple[float, float]], shift: float) -> List[Tuple[float, float]]:
    """``levels`` moved by ``shift`` (prices clipped to (0, 1)), like depth.shift_levels but by a given offset."""
    out: List[Tuple[float, float]] = []
    for p, q in levels:
        moved = round(p + shift, 6)
        if 0.0 < moved < 1.0 and q > _EPS:
            out.append((moved, q))
    return out


def liquidation_value(side: str, qty: float, book: Optional[BookObservation], quote: Optional[Quote], *,
                      last_real_book: Optional[BookObservation] = None, now: Optional[float] = None,
                      config: Optional[PaperConfig] = None,
                      adjust_levels: Optional[LevelsAdjust] = None) -> Tuple[float, str]:
    """(value, depth_state) of selling ``qty`` of ``side`` now (§6.7):

    * "fresh": ``book`` (real, at most mark_book_max_age_s old) walked with depth.liquidation_proceeds; shares
      beyond its depth are worth 0. When the quote is at least as new as the book, the book must be consistent
      with it (its best level within one tick of the touch); a book read AFTER the quote, or while the quote is
      older than stale_quote_s, defines the touch itself (an old quote never overrules a newer book, and a newer
      book with no bids is worth 0);
    * "stale": else ``last_real_book`` at most mark_depth_max_age_s old, its sell-side levels shifted to the
      current touch (depth.shift_levels) and walked the same way (unshifted when that book is newer than the
      quote);
    * "unknown": else, with a touch bid: min(qty, depth_unknown_full_shares) x touch + the rest x touch x
      (1 - depth_unknown_haircut);
    * no touch at all: (0.0, "unknown").

    ``adjust_levels`` (the engine: the portfolio's own consumed liquidity, §6.3/D35) is applied to the contract
    sell levels before they are walked, so a mark never counts bids the same portfolio already sold into. The
    consistency check and the stale shift use the RAW levels (the touch is still where the book shows it; only
    our share of it is gone). Synthetic books (backtest) are never used for marks."""
    cfg = config or PaperConfig()
    adj: LevelsAdjust = adjust_levels if adjust_levels is not None else (lambda lv: lv)
    qty = max(0.0, float(qty or 0.0))
    if qty <= _EPS:
        return 0.0, "fresh"
    touch = _contract_bid(quote, side)
    if now is None:
        stamps = [b.observed_at for b in (book, last_real_book) if b is not None]
        if quote is not None:
            stamps.append(quote.ts)
        now = max(stamps) if stamps else 0.0
    quote_ts = _num(quote.ts) if quote is not None else None
    quote_stale = quote is not None and (quote_ts is None or now - quote_ts > float(cfg.stale_quote_s) + _EPS)

    def newer_than_quote(bk: BookObservation) -> bool:
        return quote is None or quote_stale or quote_ts is None or bk.observed_at > quote_ts + _EPS

    def walk_levels(levels: List[Tuple[float, float]]) -> float:
        proceeds, _ = _depth.liquidation_proceeds(adj(list(levels)), qty)
        return proceeds

    fresh_book = book if _is_real(book) and now - book.observed_at <= cfg.mark_book_max_age_s + _EPS else None  # type: ignore[union-attr]
    if fresh_book is not None and newer_than_quote(fresh_book):
        # the book was read after the quote (or the quote stopped updating): the book itself says where the bids are
        return walk_levels(contract_levels(fresh_book, side, "sell")), "fresh"
    if quote is not None and touch is None:
        return 0.0, "unknown"  # the (current) quote shows no bid at all: no book can be called consistent with it
    if fresh_book is not None:
        levels = contract_levels(fresh_book, side, "sell")
        if levels and (touch is None or abs(levels[0][0] - touch) <= TICK + _EPS):
            return walk_levels(levels), "fresh"
    if touch is not None:
        candidates = [b for b in (book, last_real_book) if _is_real(b)]
        candidates = [b for b in candidates if now - b.observed_at <= cfg.mark_depth_max_age_s + _EPS]
        candidates.sort(key=lambda b: -b.observed_at)
        for old in candidates:
            raw = contract_levels(old, side, "sell")
            if not raw:
                continue
            # our consumption is keyed by the prices of the book we sold into: subtract it BEFORE the shift, and
            # shift by the raw book's offset (shifting the reduced best up to the touch would re-inflate it)
            shift = 0.0 if newer_than_quote(old) else touch - raw[0][0]
            if not _shift_by(raw, shift):
                continue  # every level would leave the price range
            proceeds, _ = _depth.liquidation_proceeds(_shift_by(adj(list(raw)), shift), qty)
            return proceeds, "stale"
        full = max(0.0, float(cfg.depth_unknown_full_shares))
        value = min(qty, full) * touch + max(0.0, qty - full) * touch * (1.0 - cfg.depth_unknown_haircut)
        return value, "unknown"
    return 0.0, "unknown"


def mid_value(side: str, qty: float, quote: Optional[Quote]) -> Optional[float]:
    """qty x the contract's mark (analytics.mark_price of the quote, 1 - mark for NO)."""
    if quote is None:
        return None
    from .analytics import mark_price

    mark = mark_price(quote.last, quote.bid, quote.ask)
    if mark is None:
        return None
    return float(qty) * (mark if side == "yes" else 1.0 - mark)


def queue_ahead_at(book: Optional[BookObservation], side: str, contract_price: float, now: float,
                   config: PaperConfig) -> Optional[float]:
    """Displayed quantity ahead of a new resting buy of ``side`` at ``contract_price`` (§6.4): the quantity at
    exactly that YES price on our side of ``book`` (YES bids for YES, YES asks for NO), or 0 when the price
    lies strictly inside the displayed range without a level there. None (unknown: only trade-throughs
    fill) when there is no book, the book is older than queue_book_max_age_s, or the price lies beyond the
    last displayed level on that side."""
    if not _is_real(book):
        return None
    if now - book.observed_at > config.queue_book_max_age_s + _EPS:  # type: ignore[union-attr]
        return None
    bids, asks = _depth.book_sides(book)
    p = _yes_price(side, contract_price)
    if side == "yes":
        levels = sorted(bids, key=lambda lv: -lv[0])  # best (highest) first
        better = lambda a, b: a > b + _EPS  # noqa: E731  (a is a better resting price than b)
    else:
        levels = sorted(asks, key=lambda lv: lv[0])  # best (lowest) first
        better = lambda a, b: a < b - _EPS  # noqa: E731
    if not levels:
        return None
    qty_at = sum(q for price, q in levels if abs(price - p) <= _EPS)
    if qty_at > 0:
        return float(qty_at)
    worst = levels[-1][0]
    if better(worst, p):  # beyond the last displayed level: the queue there is unknown, not empty
        return None
    return 0.0


def _candle_print_for(trade_id: str, order_id: str) -> Optional[bool]:
    """None for a real print; True/False whether a synthetic candle print (backtest) was made for this order."""
    if not str(trade_id).startswith("candle:"):
        return None
    return str(trade_id).endswith(":" + order_id)


def _eligible_prints(order: PaperOrder, trades: Sequence[TradeRecord], min_latency_s: float,
                     now: float) -> List[TradeRecord]:
    seen = set(order.seen_trade_ids)
    start = order.created_at + float(min_latency_s)
    end = now if order.cancel_at is None or order.status != "cancelling" else min(now, order.cancel_at)
    out: List[TradeRecord] = []
    for t in sorted(trades, key=lambda x: (x.ts, str(x.trade_id))):
        tid = str(t.trade_id)
        if tid in seen:
            continue
        if t.ts <= start + _EPS or t.ts > end + _EPS:
            continue
        if order.last_trade_ts is not None and t.ts < order.last_trade_ts - _EPS:
            continue
        mine = _candle_print_for(tid, order.order_id)
        if mine is False:
            continue
        out.append(t)
    return out


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
    remaining = max(0.0, float(order.qty) - float(order.filled_qty))
    queue = order.queue_ahead
    p = _yes_price(order.side, order.limit_price)
    yes = order.side == "yes"
    filled = 0.0
    used: List[str] = []
    newest: Optional[float] = None
    for t in _eligible_prints(order, trades, min_latency_s, now):
        price = _num(t.price)
        size = max(0.0, _num(t.size) or 0.0)
        if price is None or size <= 0:
            continue
        left = remaining - filled
        take = 0.0
        through = price < p - _EPS if yes else price > p + _EPS
        if through:
            take = min(left, size)
        elif abs(price - p) <= _EPS and queue is not None:
            taker = str(t.side or "").strip().upper()
            hit_us = taker == ("NO" if yes else "YES")
            if hit_us:
                if queue >= size - _EPS:
                    queue = max(0.0, queue - size)
                    rest = 0.0
                else:
                    rest = size - queue
                    queue = 0.0
                take = min(left, math.floor(queue_haircut * rest + _EPS))
        take = math.floor(max(0.0, take) + _EPS)
        if take > 0 and left > _EPS:
            filled += take
            used.append(str(t.trade_id))
            newest = t.ts if newest is None else max(newest, t.ts)
    return float(filled), used, newest, queue


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + aa / c if abs(c) > 1e-30 else 1e30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + aa / c if abs(c) > 1e-30 else 1e30
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-14:
            break
    return h


def _ibeta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1.0 - x) / b


def _t_cdf(t: float, df: float) -> float:
    x = df / (df + t * t)
    tail = 0.5 * _ibeta(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: int) -> float:
    """Inverse Student-t CDF (stdlib only: regularized incomplete beta + bisection, |error| < 1e-6).
    t_quantile(0.95, 7) = 1.895; t_quantile(0.95, 19) = 1.729."""
    if df is None or df < 1:
        raise ValueError("t_quantile needs at least one degree of freedom")
    if not 0.0 < p < 1.0:
        raise ValueError("p must lie strictly between 0 and 1")
    if abs(p - 0.5) < 1e-15:
        return 0.0
    if p < 0.5:
        return -t_quantile(1.0 - p, df)
    hi = 1.0
    while _t_cdf(hi, df) < p and hi < 1e12:
        hi *= 2.0
    lo = 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def _hashable(key: Any) -> Any:
    if isinstance(key, list):
        return tuple(_hashable(k) for k in key)
    return key


def cluster_t_interval(values: Sequence[float], clusters_a: Sequence[Any], clusters_b: Sequence[Any], *,
                       level: float = CI_LEVEL) -> Optional[Tuple[float, float, float, int]]:
    """(mean, low, high, G) of a two-way cluster-robust t-interval of the mean (Cameron-Gelbach-Miller):
    e_i = x_i - mean; V_k = sum over clusters of (sum of e in the cluster)^2 / n^2 for k in (a, b, a x b);
    V = V_a + V_b - V_ab (if <= 0: max(V_a, V_b)); G = min(#a, #b); SE = sqrt(V x G / (G - 1));
    half-width = t_quantile(1 - (1 - level) / 2, G - 1) x SE. None when G < 2. Deterministic."""
    n = len(values)
    if n == 0 or len(clusters_a) != n or len(clusters_b) != n:
        return None
    xs = [float(v) for v in values]
    a = [_hashable(k) for k in clusters_a]
    b = [_hashable(k) for k in clusters_b]
    mean = math.fsum(xs) / n
    e = [x - mean for x in xs]

    def v_of(keys: Sequence[Any]) -> Tuple[float, int]:
        sums: Dict[Any, float] = {}
        for ei, k in zip(e, keys):
            sums[k] = sums.get(k, 0.0) + ei
        return math.fsum(s * s for s in sums.values()) / (n * n), len(sums)

    va, ga = v_of(a)
    vb, gb = v_of(b)
    vab, _ = v_of(list(zip(a, b)))
    v = va + vb - vab
    if v <= 0:
        v = max(va, vb)
    g = min(ga, gb)
    if g < 2:
        return None
    se = math.sqrt(max(0.0, v) * g / (g - 1))
    half = t_quantile(1.0 - (1.0 - level) / 2.0, g - 1) * se
    return mean, mean - half, mean + half, g


def wilson_interval(wins: int, n: int, z: float = WILSON_Z) -> Optional[Tuple[float, float]]:
    """Wilson score interval of a proportion (None without observations)."""
    if n is None or n <= 0:
        return None
    p = float(wins) / float(n)
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(max(0.0, p * (1.0 - p) / n + z2 / (4.0 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


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


def _f_pnl(x: float) -> str:
    return f"{x:+,.0f}"


def _f_ci(x: float) -> str:
    return f"{x:+,.1f}"


def _f_level(level: float) -> str:
    return f"{100.0 * level:.0f}"


def compute_verdict(portfolio_id: str, ideas: Sequence[IdeaOutcome], *, pnl_liq: float, covered_hours: float,
                    wall_hours: Optional[float], day_hours: Mapping[str, float], max_drawdown: Optional[float],
                    closed_trades: Sequence[PaperTrade], exploratory: bool, n_portfolios: int,
                    depth_unknown_share: Optional[float], swing_share: Optional[float],
                    unvalued_positions: int, unvalued_value: float, demo: bool = False,
                    untested: Sequence[str] = ()) -> Verdict:
    """The level, sentence and reasons of §6.9 (exact templates there). ``ideas`` excludes nothing: the
    function drops synthetic and frozen ones itself and counts them in the result.

    ``pnl_liq`` is the portfolio's P&L at liquidation value; the P&L of the synthetic ideas (assumed candle /
    synthetic-book fills, replays) is taken OUT of it before it is stated, tested (> 0) or used by the guards
    (pnl_ex_best, top_race_share), so assumed fills can never lift a verdict; frozen ideas stay charged in it
    (their positions count 0). The win rate is over the closed ideas counted (``closed_trades`` only names the
    legging exits of sets still held). ``untested``: sentences naming kinds the run could not test (appended)."""
    counted = [i for i in ideas if not i.synthetic and not i.frozen]
    synthetic_excluded = sum(1 for i in ideas if i.synthetic)
    synthetic_pnl = math.fsum(i.pnl for i in ideas if i.synthetic and not i.frozen)
    pnl_liq = float(pnl_liq) - synthetic_pnl  # what the counted ideas (and the frozen ones, at 0) made
    n = len(counted)
    closed = [i for i in counted if i.closed]
    c, o = len(closed), n - len(closed)
    closed_pnl = math.fsum(i.pnl for i in closed)
    open_pnl = math.fsum(i.pnl for i in counted if not i.closed)

    def race_of(i: IdeaOutcome) -> str:
        return i.race_key or f"idea:{i.idea_id}"

    def day_of(i: IdeaOutcome) -> str:
        return i.day or _utc_day(i.entered_at)

    a_keys = [race_of(i) for i in counted]
    b_keys = [(int(i.entered_at // TIME_BLOCK_S), i.direction) for i in counted]
    clusters_race, clusters_time = len(set(a_keys)), len(set(b_keys))
    k = clusters_race
    ci_level = CI_LEVEL if not exploratory else 1.0 - FAMILY_ALPHA / max(1, int(n_portfolios))
    pnls = [i.pnl for i in counted]
    ci = cluster_t_interval(pnls, a_keys, b_keys, level=ci_level) if n >= 2 else None
    with_cost = [i for i in counted if i.cost > _EPS]
    rci = None
    if len(with_cost) >= 2:
        rci = cluster_t_interval([i.pnl / i.cost for i in with_cost], [race_of(i) for i in with_cost],
                                 [(int(i.entered_at // TIME_BLOCK_S), i.direction) for i in with_cost], level=ci_level)
    g = ci[3] if ci is not None else min(clusters_race, clusters_time)
    best = max(pnls) if pnls else None
    pnl_ex_best = pnl_liq - best if best is not None else pnl_liq
    mean = math.fsum(pnls) / n if n else None
    # one notion of "closed": the closed IDEAS counted (a legging-residue exit of a set still held is a fragment of
    # an open idea, not a closed trade of its own)
    wins = sum(1 for i in closed if i.pnl > _EPS)
    wr = wins / c if c else None
    wil = wilson_interval(wins, c)
    open_keys = {(i.idea_id, round(float(i.entered_at), 3)) for i in counted if not i.closed}
    legging_open = [t for t in closed_trades if getattr(t, "exit_reason", None) == "legging"
                    and (t.idea_id, round(float(t.entered_at if t.entered_at is not None else t.opened_at), 3)) in open_keys]
    qualified = sorted(d for d, h in (day_hours or {}).items() if h >= DAY_MIN_HOURS - 1e-9)
    days_covered = len(qualified)
    race_pnl: Dict[str, float] = {}
    for i in counted:
        race_pnl[race_of(i)] = race_pnl.get(race_of(i), 0.0) + i.pnl
    top_race, top_race_share = None, None
    if race_pnl and abs(pnl_liq) > _EPS:
        top_race = max(sorted(race_pnl), key=lambda r: abs(race_pnl[r]))
        top_race_share = abs(race_pnl[top_race]) / abs(pnl_liq)

    reasons: List[str] = []
    unmet: List[str] = []
    if covered_hours < MIN_COVERED_HOURS - 1e-9:
        unmet.append(f"only {covered_hours:.1f} h observed (needs {MIN_COVERED_HOURS:.0f})")
    if n < MIN_IDEAS:
        unmet.append(f"only {n} {_plural(n, 'idea')} entered (needs {MIN_IDEAS})")
    if g < MIN_CLUSTERS:
        unmet.append(f"only {g} independent {_plural(g, 'group')} (needs {MIN_CLUSTERS}): {clusters_race} "
                     f"{_plural(clusters_race, 'race')}, {clusters_time} time {_plural(clusters_time, 'block')}")
    reasons.extend(unmet)

    lv = _f_level(ci_level)
    pnl_s, h_s = _f_pnl(pnl_liq), f"{covered_hours:.1f}"
    ideas_s = f"{n} {_plural(n, 'idea')}"
    head_counts = f"({c} closed, {o} open)"
    level = "inconclusive"
    clause = ""
    if unmet:
        level = "insufficient"
        sentence = (f"Not enough evidence yet: {n} {_plural(n, 'idea')} entered {head_counts} across {k} "
                    f"{_plural(k, 'race')} in {h_s} h observed. A verdict needs at least {MIN_IDEAS} ideas, "
                    f"{MIN_CLUSTERS} independent groups of races and 2-hour periods, and {MIN_COVERED_HOURS:.0f} hours; "
                    f"P&L so far {pnl_s} SUSQies at liquidation value.")
    else:
        assert ci is not None
        lo, hi = ci[1], ci[2]
        lo_s, hi_s = _f_ci(lo), _f_ci(hi)
        ex_s = _f_pnl(pnl_ex_best)
        if hi < 0 and pnl_liq < 0:
            level = "negative"
            sentence = (f"Losing so far: {pnl_s} SUSQies at liquidation value over {h_s} h observed and {ideas_s} "
                        f"{head_counts}; the {lv}% interval for the average idea is {lo_s} to {hi_s} SUSQies, entirely "
                        "below zero.")
        else:
            favourable = lo > 0 and rci is not None and rci[1] > 0 and pnl_liq > 0
            guards: List[Tuple[str, str]] = []
            if favourable:
                if pnl_ex_best <= 0:
                    guards.append((f"without the best idea the result is {ex_s}.",
                                   f"Without the best idea ({_f_pnl(best or 0.0)}) the result is {ex_s}: it rests on one idea"))
                if depth_unknown_share is not None and depth_unknown_share > MAX_DEPTH_UNKNOWN_SHARE:
                    pct = f"{depth_unknown_share:.0%}"
                    guards.append((f"{pct} of the value is marked without a recent order book.",
                                   f"{pct} of the value is marked without a recent order book (more than "
                                   f"{MAX_DEPTH_UNKNOWN_SHARE:.0%})"))
                if swing_share is not None and swing_share > MAX_SWING_SHARE:
                    pct = f"{swing_share:.0%}"
                    guards.append((f"a 3-point national swing would move this portfolio by {pct} of its value, so the "
                                   "result may be one swing, not an edge.",
                                   f"A 3-point national swing would move this portfolio by {pct} of its value (more than "
                                   f"{MAX_SWING_SHARE:.0%})"))
                if top_race_share is not None and top_race_share > MAX_TOP_RACE_SHARE:
                    pct = f"{top_race_share:.0%}"
                    guards.append((f"one race supplies {pct} of the P&L.",
                                   f"One race ({top_race}) supplies {pct} of the P&L (more than {MAX_TOP_RACE_SHARE:.0%})"))
            if favourable and not guards:
                last_two = qualified[-POSITIVE_MIN_DAYS:]
                day_pnl = {d: math.fsum(i.pnl for i in counted if day_of(i) == d) for d in last_two}
                days_ok = len(last_two) >= POSITIVE_MIN_DAYS and all(day_pnl[d] > 0 for d in last_two)
                if not exploratory and covered_hours >= POSITIVE_MIN_HOURS - 1e-9 and days_ok:
                    level = "positive"
                    d = days_covered
                    sentence = (f"Profitable so far: {pnl_s} SUSQies at liquidation value over {h_s} h observed on {d} "
                                f"{_plural(d, 'day')} and {ideas_s}; the {lv}% interval for the average idea is {lo_s} to "
                                f"{hi_s} SUSQies, positive on each of the last two days and without the best idea ({ex_s}).")
                else:
                    level = "promising"
                    sentence = (f"Promising, not proof: {pnl_s} SUSQies at liquidation value over {h_s} h observed and "
                                f"{ideas_s} {head_counts}; the {lv}% interval for the average idea is {lo_s} to {hi_s} "
                                f"SUSQies, and the result stays positive without the best idea ({ex_s}). One day cannot "
                                "separate an edge from one national swing: confirm it on a second day before acting.")
                    if not exploratory:
                        reasons.append(f"\"Profitable so far\" needs {POSITIVE_MIN_HOURS:.0f} covered hours on "
                                       f"{POSITIVE_MIN_DAYS} UTC days with at least {DAY_MIN_HOURS:.0f} h each, each of the "
                                       f"last two positive: {covered_hours:.1f} h on {days_covered} qualifying "
                                       f"{_plural(days_covered, 'day')} so far")
                    else:
                        reasons.append("Exploratory portfolios are capped at \"promising, not proof\": only the "
                                       "pre-registered headline can reach \"profitable so far\"")
            else:
                level = "inconclusive"
                if guards:
                    clause = guards[0][0]
                    reasons.extend(g_[1] for g_ in guards)
                elif lo <= 0 <= hi:
                    clause = f"the {lv}% interval for the average idea ({lo_s} to {hi_s} SUSQies) includes zero."
                elif lo > 0 and pnl_liq <= 0:
                    clause = "the P&L at liquidation value is not positive."
                elif lo > 0 and (rci is None or rci[1] <= 0):
                    if rci is None:
                        clause = "the return on capital per idea cannot be measured."
                    else:
                        clause = (f"the {lv}% interval for the average return on capital ({rci[1]:+.1%} to "
                                  f"{rci[2]:+.1%}) includes zero.")
                else:  # hi < 0 but the P&L at liquidation is not negative
                    clause = (f"the {lv}% interval for the average idea ({lo_s} to {hi_s} SUSQies) is below zero while "
                              "the P&L at liquidation value is not.")
                sentence = (f"Inconclusive: {pnl_s} SUSQies at liquidation value over {h_s} h observed and {ideas_s} "
                            f"{head_counts}, but " + clause)
    u = int(unvalued_positions or 0)
    if u > 0:
        sentence += (f" {u} {_plural(u, 'position')} whose market closed without a ruling {'is' if u == 1 else 'are'} "
                     f"left out (last value {unvalued_value:,.0f}).")
    untested = [str(x) for x in (untested or ()) if x]
    for line in untested:
        sentence += " " + line
    if exploratory:
        sentence = (f"Exploratory (one of {int(n_portfolios)} portfolios, judged at the stricter {lv}% level): "
                    + sentence)
    reasons.extend(untested)
    reasons.append(f"Closed: {c} {_plural(c, 'idea')}, {_f_pnl(closed_pnl)} realised; open: {o} {_plural(o, 'idea')}, "
                   f"{_f_pnl(open_pnl)} at liquidation value")
    if c and wr is not None and wil is not None:
        reasons.append(f"Win rate {wr:.0%} over {c} closed {_plural(c, 'idea')} (90% "
                       f"interval {wil[0]:.0%} to {wil[1]:.0%}): closed trades only: biased toward quick winners")
    else:
        reasons.append("No closed ideas yet, so no win rate (closed trades only: biased toward quick winners)")
    if legging_open:
        k_l = len(legging_open)
        reasons.append(f"{k_l} legging {_plural(k_l, 'exit')} of sets still held: "
                       f"{_f_pnl(math.fsum(t.pnl for t in legging_open))} (part of those open ideas, not closed ideas)")
    if synthetic_excluded:
        reasons.append(f"{synthetic_excluded} {_plural(synthetic_excluded, 'idea')} with candle or synthetic-book fills "
                       f"left out (assumed fills; their {_f_pnl(synthetic_pnl)} is not in the P&L above)")
    caveats = list(CAVEATS) + ([DEMO_CAVEAT] if demo else [])
    return Verdict(
        portfolio_id=portfolio_id, level=level, sentence=sentence, hours_run=round(covered_hours, 6), closed_trades=c,
        clusters=int(g), pnl_liq=round(pnl_liq, 6), pnl_ex_best=round(pnl_ex_best, 6), best_trade_pnl=_r6(best),
        mean_trade_pnl=_r6(mean), ci_low=_r6(ci[1]) if ci else None, ci_high=_r6(ci[2]) if ci else None,
        ci_level=round(ci_level, 6), win_rate=_r6(wr), win_rate_low=_r6(wil[0]) if wil else None,
        win_rate_high=_r6(wil[1]) if wil else None, max_drawdown=_r6(max_drawdown),
        thresholds={"min_hours": MIN_COVERED_HOURS, "min_ideas": MIN_IDEAS, "min_clusters": MIN_CLUSTERS,
                    "positive_hours": POSITIVE_MIN_HOURS, "positive_days": POSITIVE_MIN_DAYS},
        reasons=reasons, caveats=caveats, exploratory=bool(exploratory), n_ideas=n, open_ideas=o,
        closed_pnl=round(closed_pnl, 6), open_pnl_liq=round(open_pnl, 6), clusters_race=clusters_race,
        clusters_time=clusters_time, df=(ci[3] - 1) if ci else None, return_mean=_r6(rci[0]) if rci else None,
        return_ci_low=_r6(rci[1]) if rci else None, return_ci_high=_r6(rci[2]) if rci else None,
        days_covered=days_covered, wall_hours=_r6(wall_hours), unvalued_positions=u,
        unvalued_value=round(float(unvalued_value or 0.0), 6), depth_unknown_share=_r6(depth_unknown_share),
        swing_share=_r6(swing_share), top_race_share=_r6(top_race_share), synthetic_excluded=synthetic_excluded,
        synthetic_pnl=round(synthetic_pnl, 6), untested=list(untested),
    )


def downsample_equity(points: Sequence[Tuple[float, float]], max_points: int = EQUITY_POINTS_MAX) -> List[List[float]]:
    """Keep the first and last point and evenly spaced points by time in between."""
    pts = [(float(t), float(v)) for t, v in points]
    if len(pts) <= max_points:
        return [[t, v] for t, v in pts]
    if max_points < 2:
        return [[pts[-1][0], pts[-1][1]]] if max_points == 1 else []
    t0, t1 = pts[0][0], pts[-1][0]
    out: List[List[float]] = []
    idx = 0
    taken: Set[int] = set()
    span = t1 - t0
    for k in range(max_points):
        target = t0 + span * k / (max_points - 1)
        while idx < len(pts) - 1 and pts[idx][0] < target - 1e-9:
            idx += 1
        if k == max_points - 1:
            idx = len(pts) - 1
        if idx not in taken:
            taken.add(idx)
            out.append([pts[idx][0], pts[idx][1]])
    return out


@dataclass
class CoverageClock(_Serializable):
    """Covered vs wall-clock time of a run (§6.11): each step that saw the market adds min(gap since the previous
    step, 3 x interval) to ``covered_s``; a longer gap is recorded in ``gaps`` (at most GAPS_MAX, newest kept). A
    step WITHOUT data (``fresh=False``: an outage while the process keeps running) adds nothing and opens or
    extends a ``{"from", "to", "reason": "no data"}`` gap, so hours with zero observations never count as
    evidence."""

    interval_s: float = 30.0
    started_at: Optional[float] = None
    last_step_at: Optional[float] = None
    covered_s: float = 0.0
    day_covered_s: Dict[str, float] = field(default_factory=dict)  # UTC date -> covered seconds
    gaps: List[Dict[str, Any]] = field(default_factory=list)  # [{"from", "to"(, "reason")}]
    no_data_open: bool = False  # the newest gap is a "no data" stretch still going on

    def tick(self, now: float, fresh: bool = True) -> Optional[float]:
        """Record a step at ``now``; returns the gap length when it exceeded 3 x interval, else None."""
        now = float(now)
        if self.started_at is None:
            self.started_at = now
        if self.last_step_at is None:
            self.last_step_at = now  # the first step covers nothing either way
            return None
        gap = now - self.last_step_at
        if gap <= 0:
            return None
        cap = 3.0 * float(self.interval_s)
        out: Optional[float] = None
        if not fresh:
            if self.no_data_open and self.gaps and self.gaps[-1].get("reason") == NO_DATA_GAP:
                self.gaps[-1]["to"] = now
            else:
                self.gaps.append({"from": self.last_step_at, "to": now, "reason": NO_DATA_GAP})
                self.no_data_open = True
            if len(self.gaps) > GAPS_MAX:
                self.gaps = self.gaps[-GAPS_MAX:]
            self.last_step_at = now
            return gap if gap > cap + 1e-9 else None
        self.no_data_open = False
        add = min(gap, cap)
        self.covered_s += add
        self._add_days(now - add, now)
        if gap > cap + 1e-9:
            self.gaps.append({"from": self.last_step_at, "to": now})
            if len(self.gaps) > GAPS_MAX:
                self.gaps = self.gaps[-GAPS_MAX:]
            out = gap
        self.last_step_at = now
        return out

    def _add_days(self, start: float, end: float) -> None:
        t = start
        while t < end - 1e-9:
            day = _utc_day(t)
            midnight = (math.floor(t / 86400.0) + 1) * 86400.0
            seg_end = min(end, midnight)
            self.day_covered_s[day] = self.day_covered_s.get(day, 0.0) + (seg_end - t)
            t = seg_end

    @property
    def covered_hours(self) -> float:
        return self.covered_s / 3600.0

    def wall_hours(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, float(now) - self.started_at) / 3600.0

    def day_hours(self) -> Dict[str, float]:
        return {d: s / 3600.0 for d, s in sorted(self.day_covered_s.items())}

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "CoverageClock":
        d = dict(data or {})
        gaps: List[Dict[str, Any]] = []
        for g in d.get("gaps") or []:
            row: Dict[str, Any] = {"from": float(g["from"]), "to": float(g["to"])}
            if g.get("reason"):
                row["reason"] = str(g["reason"])
            gaps.append(row)
        return cls(interval_s=float(d.get("interval_s", 30.0)), started_at=_num(d.get("started_at")),
                   last_step_at=_num(d.get("last_step_at")), covered_s=float(d.get("covered_s") or 0.0),
                   day_covered_s={str(k): float(v) for k, v in (d.get("day_covered_s") or {}).items()},
                   gaps=gaps, no_data_open=bool(d.get("no_data_open", False)))


# --------------------------------------------------------------------------- (de)serialisation helpers


def _from_dict(cls: Any, data: Optional[Mapping[str, Any]]) -> Any:
    if data is None:
        return None
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _levels(raw: Any) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for item in raw or ():
        try:
            out.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return out


def _book_from(data: Optional[Mapping[str, Any]]) -> Optional[BookObservation]:
    if not data:
        return None
    return BookObservation(exchange_id=str(data.get("exchange_id")), observed_at=float(data.get("observed_at", 0.0)),
                           bids=_levels(data.get("bids")), asks=_levels(data.get("asks")),
                           source=str(data.get("source") or "paper"), sequence=data.get("sequence"))


def _position_from(data: Mapping[str, Any]) -> PaperPosition:
    pos = _from_dict(PaperPosition, data)
    if isinstance(pos.exit_plan, Mapping):
        pos.exit_plan = _from_dict(ExitPlan, pos.exit_plan)
    pos.flags = list(pos.flags or [])
    return pos


def _order_from(data: Mapping[str, Any]) -> PaperOrder:
    order = _from_dict(PaperOrder, data)
    order.seen_trade_ids = [str(x) for x in (order.seen_trade_ids or [])]
    return order


def _fill_from(data: Mapping[str, Any]) -> PaperFill:
    fill = _from_dict(PaperFill, data)
    fill.levels = _levels(fill.levels)
    return fill


def _trade_from(data: Mapping[str, Any]) -> PaperTrade:
    return _from_dict(PaperTrade, data)


def _equity_from(data: Mapping[str, Any]) -> EquityPoint:
    return _from_dict(EquityPoint, data)


def paper_config_from_dict(data: Mapping[str, Any]) -> PaperConfig:
    """The inverse of ``PaperConfig.to_dict()`` (unknown keys ignored)."""
    d = dict(data or {})
    ports = d.pop("portfolios", None)
    params = d.pop("params", None)
    cfg = _from_dict(PaperConfig, d)
    if ports is not None:
        cfg.portfolios = [_from_dict(PortfolioSpec, p) for p in ports]
    if isinstance(params, Mapping):
        cfg.params = _from_dict(StrategyParams, params)
    return cfg


def _bold_hold_plan(opp: Opportunity) -> ExitPlan:
    """The exit plan of a bold entry: held to its 1/0 settlement (sizing.bold_exit_plan; the shared ``opp`` is
    never mutated). Only the idea's regime exit (``exit_before_ts``) is kept."""
    from .sizing import bold_exit_plan

    return bold_exit_plan(opp)


def _plan_from(data: Any) -> Optional[ExitPlan]:
    if data is None:
        return None
    if isinstance(data, ExitPlan):
        return copy.copy(data)
    return _from_dict(ExitPlan, data)


def _direction(delta: Optional[float]) -> str:
    d = _num(delta)
    if d is None or abs(d) <= 1e-12:
        return "N"
    return "D" if d > 0 else "R"


# --------------------------------------------------------------------------- engine


class _Book:
    """One portfolio's mutable state (§6.8)."""

    def __init__(self, spec: PortfolioSpec, config: PaperConfig, start_capital: float) -> None:
        self.spec = spec
        self.pid = spec.portfolio_id
        self.latency = float(spec.min_latency_s if spec.min_latency_s is not None else config.min_latency_s)
        self.max_delay = float(spec.max_fill_delay_s if spec.max_fill_delay_s is not None else config.max_fill_delay_s)
        self.start_capital = float(spec.start_capital if spec.start_capital is not None else start_capital)
        self.cash = self.start_capital
        self.reserved_cash = 0.0
        self.peak_equity = self.start_capital
        self.max_drawdown = 0.0
        self.max_drawdown_abs = 0.0
        self.realized_pnl = 0.0
        self.fills = 0
        self.positions: Dict[str, PaperPosition] = {}  # exchange_id -> open or frozen position
        self.orders: Dict[str, PaperOrder] = {}  # open orders (pending / resting / cancelling)
        self.consumed: List[List[Any]] = []  # [exchange_id, book_side, yes_price, qty, at]
        self.cooldowns: Dict[str, float] = {}
        self.fill_obs: Dict[str, float] = {}
        self.mode: Dict[str, Any] = {"prev_mode": None, "mode_since": None}
        self.unfilled: List[Dict[str, Any]] = []
        self.deferred: List[str] = []
        self.diagnostics: Dict[str, Any] = {"steps": 0, "fv_steps": 0, "kinds": {}}
        self.execution: Dict[str, Dict[str, float]] = {}
        self.counters: Dict[str, int] = {"o": 0, "f": 0, "t": 0, "b": 0}
        self.entries: Dict[str, Dict[str, Any]] = {}  # entry id -> what the entry decision carried
        self.ledger: Dict[str, Dict[str, Any]] = {}  # position id -> running cost / proceeds
        self.closing: Dict[str, Dict[str, Any]] = {}  # basket id -> legs already closed
        self.exits: Dict[str, Dict[str, Any]] = {}  # basket id -> the exit in progress
        self.order_meta: Dict[str, Dict[str, Any]] = {}  # order id -> {"exit_reason", "residue", "entry", "rpu"}
        self.replace: List[str] = []  # idea ids the chaser asked to sell ("replaced")
        self.trades: List[PaperTrade] = []
        self.equity: List[EquityPoint] = []
        self.last_ctx: Optional[SizingContext] = None
        self.val: Dict[str, Any] = {}

    def free_cash(self) -> float:
        return self.cash - self.reserved_cash

    def state(self) -> Dict[str, Any]:
        return {
            "cash": self.cash, "reserved_cash": self.reserved_cash, "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown, "max_drawdown_abs": self.max_drawdown_abs,
            "realized_pnl": self.realized_pnl, "fills": self.fills, "start_capital": self.start_capital,
            "positions": [p.to_dict() for p in self.positions.values()],
            "orders": [o.to_dict() for o in self.orders.values()],
            "consumed": [list(c) for c in self.consumed], "cooldowns": dict(self.cooldowns),
            "missing": {oid: o.missing_steps for oid, o in self.orders.items() if o.order_type == "maker"},
            "fill_obs": dict(self.fill_obs), "mode": dict(self.mode), "unfilled": list(self.unfilled),
            "deferred": list(self.deferred), "diagnostics": copy.deepcopy(self.diagnostics),
            "execution": copy.deepcopy(self.execution), "entries": copy.deepcopy(self.entries),
            "ledger": copy.deepcopy(self.ledger), "closing": copy.deepcopy(self.closing),
            "exits": copy.deepcopy(self.exits), "order_meta": copy.deepcopy(self.order_meta),
            "replace": list(self.replace),
        }

    def load(self, data: Mapping[str, Any]) -> None:
        self.cash = float(data.get("cash", self.cash))
        self.reserved_cash = float(data.get("reserved_cash", 0.0))
        self.peak_equity = float(data.get("peak_equity", self.peak_equity))
        self.max_drawdown = float(data.get("max_drawdown", 0.0))
        self.max_drawdown_abs = float(data.get("max_drawdown_abs", 0.0))
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        self.fills = int(data.get("fills", 0))
        if data.get("start_capital") is not None:
            self.start_capital = float(data["start_capital"])
        self.positions = {}
        for p in data.get("positions") or []:
            pos = _position_from(p)
            self.positions[pos.exchange_id] = pos
        self.orders = {}
        for o in data.get("orders") or []:
            order = _order_from(o)
            self.orders[order.order_id] = order
        self.consumed = [list(c) for c in data.get("consumed") or []]
        self.cooldowns = {str(k): float(v) for k, v in (data.get("cooldowns") or {}).items()}
        self.fill_obs = {str(k): float(v) for k, v in (data.get("fill_obs") or {}).items()}
        self.mode = dict(data.get("mode") or {"prev_mode": None, "mode_since": None})
        self.unfilled = list(data.get("unfilled") or [])
        self.deferred = list(data.get("deferred") or [])
        self.diagnostics = dict(data.get("diagnostics") or {"steps": 0, "fv_steps": 0, "kinds": {}})
        self.execution = dict(data.get("execution") or {})
        self.entries = dict(data.get("entries") or {})
        self.ledger = dict(data.get("ledger") or {})
        self.closing = dict(data.get("closing") or {})
        self.exits = dict(data.get("exits") or {})
        self.order_meta = dict(data.get("order_meta") or {})
        self.replace = list(data.get("replace") or [])


class PaperEngine:
    """Simulated portfolios side by side on one stream of signals (§6.2-6.9).

    ``policies`` maps policy names to SizingPolicy objects (default: sizing.get_policy for each
    name the portfolios use). ``persistence`` defaults to MemoryPaperPersistence. On construction
    the newest unfinished run is loaded and continued when its config fingerprint matches
    (``config_fingerprint(config, code_version)``); otherwise it is ended with reason "settings changed"
    (final snapshot written) and a run starts at the first step. ``study`` defaults to a
    study.SignalStudy (the signal-level event study, §6.14). Not thread-safe: PaperRunner serialises.
    ``rebase_default_capital`` (default True; the backtest turns it off, its capital is fixed): a live run that
    had to start on the default 100,000 is re-based to the account value once the inputs carry one, as long as
    nothing has filled yet (ui-4, §6.1).
    """

    def __init__(self, config: Optional[PaperConfig] = None, *, persistence: Optional[PaperPersistence] = None,
                 policies: Optional[Mapping[str, SizingPolicy]] = None, clock: Callable[[], float] = time.time,
                 code_version: str = "", study: Any = None, rebase_default_capital: bool = True) -> None:
        self._config = config if config is not None else PaperConfig()
        self._rebase_capital = bool(rebase_default_capital)
        self._persistence: Any = persistence if persistence is not None else MemoryPaperPersistence()
        self._policies: Dict[str, Any] = dict(policies or {})
        self._clock = clock
        self._code_version = str(code_version or "")
        self._study_given = study is not None
        self._study = study if study is not None else self._new_study()
        self._fingerprint = config_fingerprint(self._config, self._code_version)
        self._reset_memory()
        try:
            self._has_previous = self._persistence.paper_last_ended_run() is not None
        except Exception as exc:  # a broken store never stops the engine
            log.warning("paper: could not read the previous run: %s", exc)
            self._has_previous = False
        self._load_existing()

    # ------------------------------------------------------------------ setup helpers
    def _new_study(self) -> Any:
        from .study import SignalStudy

        return SignalStudy(interval_s=self._config.interval_s)

    def _reset_memory(self) -> None:
        self._run: Optional[Dict[str, Any]] = None  # {"run_id", "started_at", "start_capital", "capital_source"}
        self._books: Dict[str, _Book] = {}
        self._coverage = CoverageClock(interval_s=self._config.interval_s)
        self._steps = 0
        self._last_step_seconds: Optional[float] = None
        self._final: Optional[Dict[str, Any]] = None
        self._last_real_books: Dict[str, BookObservation] = {}
        self._tape_read_at: Dict[str, float] = {}
        self._deferred_sets: Dict[str, List[str]] = {}
        self._basket_waiting: Dict[str, List[str]] = {}
        self._bookless: List[List[str]] = []  # top 3 sized-but-bookless ideas' exchanges (previous step)
        self._last_signals: Dict[str, Any] = {"count": 0, "by_kind": {}, "at": None, "top_bookless": []}
        self._recent_fills: List[PaperFill] = []
        self._new_fills: List[PaperFill] = []
        self._new_trades: List[PaperTrade] = []
        self._new_equity: List[EquityPoint] = []
        self._dirty_orders: Dict[str, PaperOrder] = {}
        self._last_capital: Tuple[Optional[float], str] = (None, "")
        self._now: Optional[float] = None
        self._last_obs: Optional[MarketObservation] = None
        self._step_report = StepReport(now=0.0, step=0)
        self._ending = False
        self._ended_run: Optional[Dict[str, Any]] = None
        self._final_summaries: Optional[List[PortfolioSummary]] = None  # the ended run's pre-liquidation summaries

    def _policy(self, name: str) -> Any:
        pol = self._policies.get(name)
        if pol is None:
            from . import sizing

            pol = sizing.get_policy(name)
            self._policies[name] = pol
        return pol

    def _load_existing(self) -> None:
        try:
            rec = self._persistence.paper_load_run(None)
        except Exception as exc:
            log.warning("paper: could not load the current run: %s", exc)
            rec = None
        if not rec:
            return
        stored = rec.get("config") or {}
        if stored.get("fingerprint") == self._fingerprint:
            self._restore(rec)
            return
        # settings changed: finish the old run under its own settings, then start fresh at the first step
        new_config, new_fp, new_study = self._config, self._fingerprint, self._study
        try:
            old_cfg = paper_config_from_dict(stored.get("paper") or {})
        except Exception:
            old_cfg = None
        try:
            self._config = old_cfg or new_config
            self._study = self._new_study() if not self._study_given else self._study
            self._restore(rec)
            self.end_run(self._clock(), "settings changed")
        except Exception as exc:
            log.warning("paper: could not finish the previous run cleanly: %s", exc)
            try:
                self._persistence.paper_end_run(rec["run_id"], self._clock())
                self._has_previous = True
            except Exception:
                pass
        finally:
            self._config, self._fingerprint = new_config, new_fp
            self._study = new_study if self._study_given else self._new_study()
            self._reset_memory()

    def _restore(self, rec: Mapping[str, Any]) -> None:
        state = rec.get("state") or {}
        run_id = str(rec.get("run_id") or state.get("run_id"))
        self._run = {"run_id": run_id, "started_at": float(rec.get("started_at") or state.get("started_at") or 0.0),
                     "start_capital": float(state.get("start_capital") or DEFAULT_START_CAPITAL),
                     "capital_source": str(state.get("capital_source") or "")}
        if state.get("target_hours") is not None and self._config.target_hours is None:
            self._config.target_hours = float(state["target_hours"])
        self._steps = int(state.get("steps") or 0)
        self._last_step_seconds = _num(state.get("last_step_seconds"))
        self._coverage = CoverageClock.from_dict(state.get("coverage"))
        self._coverage.interval_s = float(self._config.interval_s)
        self._final = state.get("final")
        counters = state.get("counters") or {}
        ports = state.get("portfolios") or {}
        self._books = {}
        for spec in self._config.portfolios:
            b = _Book(spec, self._config, self._run["start_capital"])
            if spec.portfolio_id in ports:
                b.load(ports[spec.portfolio_id])
            if spec.portfolio_id in counters:
                b.counters = {k: int(v) for k, v in counters[spec.portfolio_id].items()}
            self._books[b.pid] = b
        self._last_real_books = {}
        for eid, data in (state.get("last_real_books") or {}).items():
            bk = _book_from(data)
            if bk is not None:
                self._last_real_books[str(eid)] = bk
        self._tape_read_at = {str(k): float(v) for k, v in (state.get("tape_read_at") or {}).items()}
        self._deferred_sets = {str(k): list(v) for k, v in (state.get("deferred_sets") or {}).items()}
        self._basket_waiting = {str(k): list(v) for k, v in (state.get("basket_waiting") or {}).items()}
        self._bookless = [list(x) for x in (state.get("bookless") or [])]
        self._last_signals = dict(state.get("last_signals") or self._last_signals)
        self._now = _num(state.get("last_step_at"))
        try:
            trades = self._persistence.paper_trades(run_id, limit=None) or []
        except Exception:
            trades = []
        for d in reversed(trades):
            t = _trade_from(d)
            if t.portfolio_id in self._books:
                self._books[t.portfolio_id].trades.append(t)
        try:
            eq = self._persistence.paper_equity(run_id) or []
        except Exception:
            eq = []
        for d in eq:
            p = _equity_from(d)
            if p.portfolio_id in self._books:
                self._books[p.portfolio_id].equity.append(p)
        try:
            fills = self._persistence.paper_fills(run_id, limit=RECENT_FILLS_KEPT) or []
        except Exception:
            fills = []
        self._recent_fills = [_fill_from(d) for d in reversed(fills)]
        try:
            events = self._persistence.paper_events(run_id) or []
        except Exception:
            events = []
        try:
            self._study.load(state.get("study"), events)
        except Exception as exc:
            log.warning("paper: could not restore the event study: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    @property
    def run_id(self) -> Optional[str]:
        return self._run["run_id"] if self._run else None

    @property
    def config(self) -> PaperConfig:
        return self._config

    @property
    def persistence(self) -> Any:
        return self._persistence

    @property
    def study(self) -> Any:
        return self._study

    def _capital_from(self, inputs: Optional[StrategyInputs]) -> Tuple[float, str]:
        if self._config.start_capital is not None and float(self._config.start_capital) > 0:
            return float(self._config.start_capital), "set by you"
        if inputs is not None:
            for attr, label in (("account_value", "account value"), ("balance", "cash"),
                                ("initial_balance", "initial balance")):
                v = _num(getattr(inputs, attr, None))
                if v is not None and v > 0:
                    return v, label
        if self._last_capital[0] is not None:
            return float(self._last_capital[0]), self._last_capital[1]
        return DEFAULT_START_CAPITAL, DEFAULT_CAPITAL_SOURCE

    def _rebase_default_capital(self, now: float, inputs: Optional[StrategyInputs]) -> bool:
        """ui-4 (§6.1): a run that had to start on the last-resort default 100,000 (the account context was not
        known yet, e.g. the first paper step of a fresh start runs before the first balance refresh) is re-based
        to the account value as soon as the inputs carry one -- provided nothing has filled yet in any portfolio
        (no fill, position or trade: the P&L so far is exactly 0 at any capital). Orders already placed were sized
        on the default: they are withdrawn (recorded as cancelled, with the reason) and the ideas are sized again
        from the account in this same step. The stored equity points are rewritten at the new capital (no fill:
        equity was the start capital at every point). Coverage, the event study and the fair-value diagnostics
        are about the market, not the capital, and are kept. Returns whether the run was re-based."""
        run = self._run
        if (not self._rebase_capital or run is None or run.get("capital_source") != DEFAULT_CAPITAL_SOURCE
                or self._final is not None):
            return False
        capital, source = self._capital_from(inputs)
        if source == DEFAULT_CAPITAL_SOURCE or not capital or capital <= 0:
            return False
        if any(b.fills > 0 or b.positions or b.trades or b.closing for b in self._books.values()):
            return False  # something has filled: the run is what it is (the UI names its capital source)
        capital = float(capital)
        why = (f"Start capital set from your {source} ({capital:,.0f} SUSQies): this order was sized on the default "
               "100,000 and is placed again at the new size")
        run["start_capital"], run["capital_source"] = capital, source
        for spec in self._config.portfolios:
            old = self._books.get(spec.portfolio_id)
            nb = _Book(spec, self._config, capital)
            if old is not None:
                if spec.start_capital is not None:
                    continue  # this portfolio has its own capital: nothing was sized on the default
                for order in sorted(old.orders.values(), key=lambda o: o.order_id):
                    order.status, order.close_reason, order.closed_at = "cancelled", why, float(now)
                    order.reserved_cash = 0.0
                    self._dirty_orders[order.order_id] = order
                nb.counters = dict(old.counters)  # order / fill ids stay unique within the run
                nb.diagnostics = copy.deepcopy(old.diagnostics)
                nb.equity = [dataclasses.replace(p, cash=round(nb.start_capital, 6), reserved_cash=0.0,
                                                 liq_value=round(nb.start_capital, 6),
                                                 mark_value=round(nb.start_capital, 6),
                                                 fv_value=round(nb.start_capital, 6) if p.fv_value is not None else None,
                                                 open_positions=0)
                             for p in old.equity]
                self._new_equity.extend(nb.equity)
            self._books[spec.portfolio_id] = nb
        log.info("paper: run %s re-based from the default 100,000 to the %s (%.2f) before any fill",
                 run.get("run_id"), source, capital)
        return True

    def _new_run_id(self, now: float) -> str:
        base = f"run-{int(now)}"
        rid, k = base, 1
        while True:
            try:
                exists = self._persistence.paper_load_run(rid) is not None
            except Exception:
                exists = False
            if not exists:
                return rid
            k += 1
            rid = f"{base}-{k}"

    def start(self, now: float, start_capital: Optional[float] = None, capital_source: str = "") -> str:
        """Begin a new run (ending the current one with reason "reset") with every portfolio at ``start_capital``."""
        if self._run is not None:
            self.end_run(now, "reset")
        if start_capital is None:
            start_capital, src = self._capital_from(None)
            capital_source = capital_source or src
        elif not capital_source:
            capital_source = "set by you"
        self._reset_memory()
        self._study = self._study if self._study_given else self._new_study()
        if hasattr(self._study, "load"):
            try:
                self._study.load(None, ())
            except Exception:
                pass
        rid = self._new_run_id(now)
        self._run = {"run_id": rid, "started_at": float(now), "start_capital": float(start_capital),
                     "capital_source": capital_source}
        self._books = {spec.portfolio_id: _Book(spec, self._config, float(start_capital)) for spec in self._config.portfolios}
        self._coverage = CoverageClock(interval_s=float(self._config.interval_s), started_at=float(now))
        self._now = float(now)
        for b in self._books.values():
            self._equity_point(b, float(now), force=True)
        self._persist(float(now))
        return rid

    def end_run(self, now: float, reason: str) -> Optional[Dict[str, Any]]:
        """End the current run: open positions are recorded as closed trades at liquidation value with
        ``exit_reason=reason`` ("reset" | "settings changed" | "completed"), the final snapshot
        ``{"at", "reason", "verdicts", "portfolios", "study"}`` is written into the state, then
        ``paper_end_run``. Returns the final snapshot (None when no run was active)."""
        if self._run is None:
            return None
        now = float(now)
        summaries = self._summaries(now)
        snapshot = self._snapshot(now, reason, summaries)
        # what the run showed BEFORE its positions were turned into trades at liquidation value: summary(),
        # portfolios() and verdicts() serve these once the run has ended, so the end of a run never turns open
        # P&L into "realised", never adds closed trades to the win rate and never switches off the depth and
        # swing guards (their exposure is gone after the liquidation)
        frozen = copy.deepcopy(summaries)
        self._ending = True
        try:
            for b in self._books.values():
                for order in list(b.orders.values()):
                    self._close_order(b, order, "cancelled", f"Run ended ({reason})", now)
                for pos in sorted(list(b.positions.values()), key=lambda p: p.exchange_id):
                    if pos.status == "closed":
                        continue
                    value = float(pos.liq_value or 0.0) if pos.status != "frozen" else 0.0
                    qty = pos.qty
                    price = value / qty if qty > _EPS else 0.0
                    self._sell_position(b, pos, qty, price, now, reason=reason, liquidity="settlement",
                                        purpose="exit", record_fill=False)
                for order in list(b.orders.values()):
                    self._close_order(b, order, "cancelled", f"Run ended ({reason})", now)
                self._equity_point(b, now, force=True)
        finally:
            self._ending = False
        self._final = snapshot
        self._persist(now)
        try:
            self._persistence.paper_end_run(self._run["run_id"], now)
        except Exception as exc:
            log.warning("paper: could not end run %s: %s", self._run["run_id"], exc)
        self._has_previous = True
        self._ended_run = dict(self._run, ended_at=now, end_reason=reason)
        self._final_summaries = frozen
        self._run = None
        return snapshot

    def reset(self, now: float, start_capital: Optional[float] = None, capital_source: str = "",
              target_hours: Optional[float] = None) -> str:
        """``end_run(now, "reset")`` then ``start``. Returns the new run id."""
        self.end_run(now, "reset")
        if target_hours is not None:
            self._config.target_hours = float(target_hours)
            self._fingerprint = config_fingerprint(self._config, self._code_version)
        if start_capital is None:
            start_capital, src = self._capital_from(None)
            capital_source = capital_source or src
        return self.start(now, start_capital, capital_source or "set by you")

    # ------------------------------------------------------------------ read plan
    def resting_exchanges(self) -> Set[str]:
        """Exchanges where any portfolio has a resting or cancelling maker order (StrategyInputs.resting_exchanges)."""
        out: Set[str] = set()
        for b in self._books.values():
            for o in b.orders.values():
                if o.status in ("resting", "cancelling"):
                    out.add(o.exchange_id)
        return out

    def _newest_book_at(self, eid: str, obs: Optional[MarketObservation], real_only: bool = True) -> Optional[float]:
        stamps: List[float] = []
        if obs is not None:
            bk = obs.books.get(eid)
            if bk is not None and (not real_only or _is_real(bk)):
                stamps.append(bk.observed_at)
        old = self._last_real_books.get(eid)
        if old is not None:
            stamps.append(old.observed_at)
        return max(stamps) if stamps else None

    def wanted_reads(self, now: float, obs: MarketObservation, candidates: Sequence[str] = ()) -> ReadPlan:
        """What this step needs read, by priority (§6.10). The caller trims it to the budget."""
        cfg = self._config
        reqs: List[ReadRequest] = []
        index: Dict[Tuple[str, str], ReadRequest] = {}
        parent: Dict[str, str] = {}

        def find(g: str) -> str:
            while parent.get(g, g) != g:
                g = parent[g]
            return g

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        def add(kind: str, eid: str, priority: int, reason: str, *, since: Optional[float] = None,
                after: Optional[float] = None, group: Optional[str] = None) -> None:
            eid = str(eid)
            if group is not None:
                parent.setdefault(group, group)
            key = (kind, eid)
            if key in index:
                r = index[key]
                if group is not None:
                    if r.group is None:
                        r.group = group
                    else:
                        union(r.group, group)
                if after is not None and (r.after is None or after < r.after):
                    r.after = after
                if since is not None and (r.since is None or since < r.since):
                    r.since = since
                return
            r = ReadRequest(kind=kind, exchange_id=eid, priority=priority, reason=reason, since=since, after=after,
                            group=group)
            index[key] = r
            reqs.append(r)

        # 1: pending taker orders whose window opened without a usable observation (every leg of a group)
        pending: List[Tuple[float, str, PaperOrder, _Book]] = []
        for b in self._books.values():
            for o in b.orders.values():
                if o.status == "pending":
                    pending.append((o.created_at, o.order_id, o, b))
        pending.sort(key=lambda x: (x[0], x[1]))
        groups_needed: Dict[str, List[Tuple[PaperOrder, _Book]]] = {}
        for _, _, o, b in pending:
            if o.group_id:
                groups_needed.setdefault(f"{b.pid}|{o.group_id}|{o.purpose}", []).append((o, b))
        for _, _, o, b in pending:
            if now < o.created_at + b.latency - _EPS:
                continue
            if self._satisfied(o, b, obs):
                continue
            after = self._fill_after(o, b)
            group = None
            if o.group_id:
                group = f"g:{b.pid}|{o.group_id}|{o.purpose}"
                for leg, lb in groups_needed.get(f"{b.pid}|{o.group_id}|{o.purpose}", []):
                    add("book", leg.exchange_id, 1, "fill for a pending order", after=self._fill_after(leg, lb),
                        group=group)
            add("book", o.exchange_id, 1, "fill for a pending order", after=after, group=group)
        # 2: deferred set entries and basket exits waiting for books (all legs or none)
        for idea_id in sorted(self._deferred_sets):
            legs = self._deferred_sets[idea_id]
            g = f"g:set|{idea_id}"
            for eid in legs:
                add("book", eid, 2, "fresh books for every leg of a set entry", group=g)
        for basket_id in sorted(self._basket_waiting):
            g = f"g:exit|{basket_id}"
            for eid in self._basket_waiting[basket_id]:
                add("book", eid, 2, "depth for a basket exit", group=g)
        # 3: tape for resting / cancelling maker orders
        tape_rows: Dict[str, Dict[str, Any]] = {}
        for b in self._books.values():
            for o in b.orders.values():
                if o.status not in ("resting", "cancelling"):
                    continue
                row = tape_rows.setdefault(o.exchange_id, {"since": None, "cancel": False, "cancel_at": None})
                cur = o.last_trade_ts if o.last_trade_ts is not None else o.created_at
                row["since"] = cur if row["since"] is None else min(row["since"], cur)
                if o.status == "cancelling":
                    row["cancel"] = True
                    ca = o.cancel_at if o.cancel_at is not None else now
                    row["cancel_at"] = ca if row["cancel_at"] is None else min(row["cancel_at"], ca)
        urgent, regular = [], []
        for eid, row in tape_rows.items():
            last = self._tape_read_at.get(eid)
            # a cancel still on its way (cancel_at = decision + latency, in the future) is read like a resting order
            if row["cancel"] and now >= row["cancel_at"] - _EPS and (last is None or last < row["cancel_at"] - _EPS):
                urgent.append((row["cancel_at"], eid, row))
            elif last is None or now - last >= cfg.trade_poll_s - _EPS:
                regular.append((last if last is not None else -1.0, eid, row))
        for _, eid, row in sorted(urgent, key=lambda x: (x[0], x[1])):
            add("trades", eid, 3, "tape since a cancelled resting order", since=row["since"])
        for _, eid, row in sorted(regular, key=lambda x: (x[0], x[1])):
            add("trades", eid, 3, "tape for resting orders", since=row["since"])
        # 4: marks, largest liquidation value first
        marks: Dict[str, float] = {}
        for b in self._books.values():
            for pos in b.positions.values():
                if pos.status != "open":
                    continue
                marks[pos.exchange_id] = marks.get(pos.exchange_id, 0.0) + float(pos.liq_value or pos.cost or 0.0)
        for eid, value in sorted(marks.items(), key=lambda kv: (-kv[1], kv[0])):
            at = self._newest_book_at(eid, obs)
            if at is None or now - at >= cfg.mark_refresh_s - _EPS:
                add("book", eid, 4, "mark an open position")
        # 5: books for exchanges with resting orders (keeps holes and their queue estimate alive)
        for eid in sorted(self.resting_exchanges()):
            at = self._newest_book_at(eid, obs)
            if at is None or now - at >= cfg.resting_book_refresh_s - _EPS:
                add("book", eid, 5, "refresh a resting order's book")
        # 6: candidates and the previous step's sized ideas without depth
        fresh_s = 2.0 * float(cfg.interval_s)
        wanted6: List[str] = [str(c) for c in candidates or ()]
        for legs in self._bookless[:3]:
            wanted6.extend(legs)
        for eid in wanted6:
            at = self._newest_book_at(eid, obs, real_only=False)
            if at is None or now - at > fresh_s + _EPS:
                add("book", eid, 6, "depth for a candidate idea")
        for r in reqs:
            if r.group is not None:
                root = find(r.group)
                r.group = root
        # name merged groups by their members, deterministically
        members: Dict[str, List[str]] = {}
        for r in reqs:
            if r.group is not None:
                members.setdefault(r.group, []).append(r.exchange_id)
        names = {g: "g:" + "+".join(sorted(set(m))) for g, m in members.items()}
        for r in reqs:
            if r.group is not None:
                r.group = names[r.group]
        return ReadPlan(requests=reqs)

    @staticmethod
    def _fill_after(order: PaperOrder, b: _Book) -> float:
        """Earliest acceptable observed_at for the order's next fill: decision + latency, and strictly after the
        observation it already used (a partly filled exit never fills twice from one observation, §6.3)."""
        after = order.created_at + b.latency
        used = b.fill_obs.get(order.order_id)
        if used is not None:
            after = max(after, float(used) + 1e-6)
        return after

    def _satisfied(self, order: PaperOrder, b: _Book, obs: Optional[MarketObservation]) -> bool:
        if obs is None:
            return False
        bk = obs.books.get(order.exchange_id)
        if bk is None:
            return False
        lo = order.created_at + b.latency
        hi = order.created_at + b.max_delay
        used = b.fill_obs.get(order.order_id)
        if used is not None and bk.observed_at <= used + _EPS:
            return False
        return lo - _EPS <= bk.observed_at <= hi + _EPS

    # ------------------------------------------------------------------ orders and fills
    def _exec(self, b: _Book, kind: str) -> Dict[str, float]:
        return b.execution.setdefault(kind, {"entries": 0, "filled": 0, "slip_sum": 0.0, "slip_n": 0, "unfilled": 0,
                                             "exits": 0, "exit_slip_sum": 0.0, "exit_slip_n": 0})

    def _new_order(self, b: _Book, *, idea_id: str, kind: str, purpose: str, order_type: str, eid: str,
                   market_id: str, side: str, action: str, qty: float, limit: float, now: float, reason: str,
                   group_id: Optional[str] = None, expires_at: Optional[float] = None,
                   decision_touch: Optional[float] = None, planned: Optional[float] = None, title: str = "",
                   option: Optional[str] = None, reserve_per_unit: Optional[float] = None,
                   meta: Optional[Dict[str, Any]] = None, queue_ahead: Optional[float] = None) -> PaperOrder:
        b.counters["o"] = int(b.counters.get("o", 0)) + 1
        oid = f"{b.pid}:o{b.counters['o']}"
        rpu = float(limit if reserve_per_unit is None else reserve_per_unit) if action == "buy" else 0.0
        reserved = max(0.0, qty * rpu)
        order = PaperOrder(
            order_id=oid, portfolio_id=b.pid, idea_id=idea_id, kind=kind, purpose=purpose, order_type=order_type,
            exchange_id=str(eid), market_id=str(market_id or ""), side=side, action=action, qty=float(qty),
            limit_price=round(float(limit), 6), created_at=float(now), expires_at=expires_at,
            status="pending" if order_type == "taker" else "resting", reserved_cash=reserved, reason=reason,
            group_id=group_id, title=title or "", option=option, decision_touch=_r6(decision_touch),
            latency_s=b.latency, planned_price=_r6(planned), queue_ahead=queue_ahead,
        )
        b.reserved_cash += reserved
        b.orders[oid] = order
        m = dict(meta or {})
        m["rpu"] = rpu
        b.order_meta[oid] = m
        self._dirty_orders[oid] = order
        ex = self._exec(b, kind)
        if purpose == "entry":
            ex["entries"] += 1
        else:
            ex["exits"] += 1
        return order

    def _close_order(self, b: _Book, order: PaperOrder, status: str, reason: Optional[str], now: float) -> None:
        if order.order_id not in b.orders:
            return
        order.status = status
        order.close_reason = reason
        order.closed_at = float(now)
        if order.reserved_cash > 0:
            b.reserved_cash = max(0.0, b.reserved_cash - order.reserved_cash)
            order.reserved_cash = 0.0
        if b.reserved_cash < 1e-7:
            b.reserved_cash = 0.0
        b.orders.pop(order.order_id, None)
        b.fill_obs.pop(order.order_id, None)
        self._dirty_orders[order.order_id] = order
        meta = b.order_meta.get(order.order_id) or {}
        if order.purpose == "entry":
            ex = self._exec(b, order.kind)
            if order.filled_qty <= _EPS:
                ex["unfilled"] += 1
            left = order.qty - order.filled_qty
            if order.order_type == "taker" and left > _EPS:
                b.unfilled.append({"exchange_id": order.exchange_id, "side": order.side, "qty": left,
                                   "decision_touch": order.decision_touch, "created_at": order.created_at,
                                   "kind": order.kind})
                cap = max(0, int(self._config.unfilled_tracked))
                if len(b.unfilled) > cap:
                    b.unfilled = b.unfilled[-cap:] if cap else []
            entry_id = meta.get("entry")
            if entry_id:
                self._entry_resolved(b, entry_id, now)
        b.order_meta.pop(order.order_id, None)

    def _apply_buy(self, b: _Book, order: PaperOrder, qty: float, price: float, now: float, *, liquidity: str,
                   book_at: Optional[float], levels: Sequence[Tuple[float, float]], trade_ids: Sequence[str],
                   synthetic: bool, reason: str = "") -> PaperFill:
        meta = b.order_meta.get(order.order_id) or {}
        rpu = float(meta.get("rpu", order.limit_price))
        entry = b.entries.get(meta.get("entry") or "") or {}
        fill = self._record_fill(b, order, qty, price, now, liquidity=liquidity, purpose="entry", book_at=book_at,
                                 levels=levels, trade_ids=trade_ids, synthetic=synthetic, reason=reason)
        first = order.filled_qty <= _EPS
        order.avg_fill_price = ((order.avg_fill_price or 0.0) * order.filled_qty + qty * price) / (order.filled_qty + qty)
        order.filled_qty += qty
        if synthetic:
            order.synthetic = True
        release = min(order.reserved_cash, qty * rpu)
        order.reserved_cash -= release
        b.reserved_cash = max(0.0, b.reserved_cash - release)
        b.cash -= qty * price
        advance = 0.0
        if entry.get("all_collateral"):
            ratio = float(entry.get("advance_ratio") or 0.0)
            advance = qty * price * ratio
            b.cash += advance
        ex = self._exec(b, order.kind)
        if first:
            ex["filled"] += 1
        if fill.slippage is not None:
            ex["slip_sum"] += float(fill.slippage)
            ex["slip_n"] += 1
        eid = order.exchange_id
        pos = b.positions.get(eid)
        leg = (entry.get("legs") or {}).get(eid) or {}
        if pos is None or pos.status == "closed":
            pos = PaperPosition(
                position_id=f"{b.pid}:{eid}", portfolio_id=b.pid, exchange_id=eid, market_id=order.market_id,
                side=order.side, qty=0.0, avg_cost=price, cost=0.0, opened_at=float(now), idea_id=order.idea_id,
                kind=order.kind, edge_per_unit=float(entry.get("edge_per_unit") or 0.0),
                race_key=leg.get("race_key") or entry.get("race_key"), basket_id=entry.get("basket_id"),
                exit_plan=_plan_from(entry.get("exit_plan")), status="open", first_fill_at=float(now),
                updated_at=float(now), title=order.title, option=order.option,
                factor_delta=_num(leg.get("factor_delta")) if leg.get("factor_delta") is not None else None,
                score=_num(entry.get("score")), bold=bool(entry.get("bold")), floor_per_unit=_num(entry.get("floor_per_unit")),
                synthetic=bool(synthetic), entered_at=_num(entry.get("entered_at")) or order.created_at,
            )
            b.positions[eid] = pos
            b.ledger[pos.position_id] = {"bought_qty": 0.0, "bought_cost": 0.0, "proceeds": 0.0, "exit_reason": None,
                                         "residue": 0.0, "entry": meta.get("entry"), "synthetic": bool(synthetic),
                                         "fv_entry": leg.get("fv"), "market_id": order.market_id}
        led = b.ledger.setdefault(pos.position_id, {"bought_qty": 0.0, "bought_cost": 0.0, "proceeds": 0.0,
                                                    "exit_reason": None, "residue": 0.0, "entry": meta.get("entry"),
                                                    "synthetic": False, "fv_entry": None, "market_id": order.market_id})
        new_qty = pos.qty + qty
        pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / new_qty if new_qty > _EPS else price
        pos.qty = new_qty
        pos.cost = pos.qty * pos.avg_cost
        pos.collateral_advance += advance
        pos.updated_at = float(now)
        if synthetic:
            pos.synthetic = True
            led["synthetic"] = True
            if "synthetic" not in pos.flags:
                pos.flags.append("synthetic")
        led["bought_qty"] = float(led.get("bought_qty", 0.0)) + qty
        led["bought_cost"] = float(led.get("bought_cost", 0.0)) + qty * price
        return fill

    def _record_fill(self, b: _Book, order: Optional[PaperOrder], qty: float, price: float, now: float, *,
                     liquidity: str, purpose: str, book_at: Optional[float], levels: Sequence[Tuple[float, float]],
                     trade_ids: Sequence[str], synthetic: bool, reason: str = "", pos: Optional[PaperPosition] = None,
                     action: Optional[str] = None) -> PaperFill:
        b.counters["f"] = int(b.counters.get("f", 0)) + 1
        fid = f"{b.pid}:f{b.counters['f']}"
        if order is not None:
            act = action or order.action
            touch = order.decision_touch
            slip = None
            if touch is not None:
                slip = (price - touch) if act == "buy" else (touch - price)
            fill = PaperFill(
                fill_id=fid, order_id=order.order_id, portfolio_id=b.pid, exchange_id=order.exchange_id,
                market_id=order.market_id, side=order.side, action=act, qty=float(qty), price=round(float(price), 6),
                ts=float(now), liquidity=liquidity, purpose=purpose, kind=order.kind, idea_id=order.idea_id,
                book_at=book_at, latency_s=_r6(book_at - order.created_at) if book_at is not None else None,
                levels=[(round(p, 6), float(q)) for p, q in levels], trade_ids=list(trade_ids),
                reason=reason or order.reason, group_id=order.group_id, title=order.title, option=order.option,
                slippage=_r6(slip), synthetic=bool(synthetic),
            )
        else:
            assert pos is not None
            fill = PaperFill(
                fill_id=fid, order_id="", portfolio_id=b.pid, exchange_id=pos.exchange_id, market_id=pos.market_id,
                side=pos.side, action=action or "settle", qty=float(qty), price=round(float(price), 6), ts=float(now),
                liquidity=liquidity, purpose=purpose, kind=pos.kind, idea_id=pos.idea_id, reason=reason,
                group_id=pos.basket_id, title=pos.title, option=pos.option,
            )
        b.fills += 1
        ex = self._exec(b, fill.kind)
        ex["fill_count"] = int(ex.get("fill_count", 0)) + 1
        self._new_fills.append(fill)
        self._recent_fills.append(fill)
        if len(self._recent_fills) > RECENT_FILLS_KEPT:
            self._recent_fills = self._recent_fills[-RECENT_FILLS_KEPT:]
        return fill

    def _sell_position(self, b: _Book, pos: PaperPosition, qty: float, price: float, now: float, *, reason: str,
                       liquidity: str, purpose: str, order: Optional[PaperOrder] = None, book_at: Optional[float] = None,
                       levels: Sequence[Tuple[float, float]] = (), trade_ids: Sequence[str] = (),
                       synthetic: bool = False, residue: bool = False, record_fill: bool = True,
                       action: str = "sell") -> Optional[PaperFill]:
        """Sell (or settle) ``qty`` of ``pos`` at ``price`` per share; close the position at zero."""
        qty = min(float(qty), pos.qty)
        if qty <= _EPS:
            return None
        fill = None
        if record_fill:
            if order is not None:
                fill = self._record_fill(b, order, qty, price, now, liquidity=liquidity, purpose=purpose, book_at=book_at,
                                         levels=levels, trade_ids=trade_ids, synthetic=synthetic, action=action)
            else:
                fill = self._record_fill(b, None, qty, price, now, liquidity=liquidity, purpose=purpose, book_at=None,
                                         levels=(), trade_ids=(), synthetic=False, reason=reason, pos=pos, action=action)
        led = b.ledger.setdefault(pos.position_id, {"bought_qty": pos.qty, "bought_cost": pos.cost, "proceeds": 0.0,
                                                    "exit_reason": None, "residue": 0.0, "entry": None,
                                                    "synthetic": pos.synthetic, "fv_entry": None,
                                                    "market_id": pos.market_id})
        repay = pos.collateral_advance * qty / pos.qty if pos.qty > _EPS else pos.collateral_advance
        realized = qty * (price - pos.avg_cost)
        b.cash += qty * price - repay
        pos.collateral_advance = max(0.0, pos.collateral_advance - repay)
        b.realized_pnl += realized
        if synthetic:
            led["synthetic"] = True
        if residue:
            self._residue_trade(b, pos, qty, price, now, synthetic=synthetic or bool(led.get("synthetic")))
            led["bought_qty"] = max(0.0, float(led.get("bought_qty", 0.0)) - qty)
            led["bought_cost"] = max(0.0, float(led.get("bought_cost", 0.0)) - qty * pos.avg_cost)
            led["residue"] = max(0.0, float(led.get("residue", 0.0)) - qty)
        else:
            pos.realized_pnl += realized
            led["proceeds"] = float(led.get("proceeds", 0.0)) + qty * price
            prev = led.get("exit_reason")
            led["exit_reason"] = "legging" if prev == "legging" else reason
        pos.qty -= qty
        if pos.qty < 1e-7:
            pos.qty = 0.0
        pos.cost = pos.qty * pos.avg_cost
        pos.updated_at = float(now)
        if pos.qty <= _EPS:
            self._close_position(b, pos, now)
        return fill

    def _trade_common(self, b: _Book, entry: Mapping[str, Any], now: float) -> Dict[str, Any]:
        b.counters["t"] = int(b.counters.get("t", 0)) + 1
        return {"trade_id": f"{b.pid}:t{b.counters['t']}", "portfolio_id": b.pid,
                "entered_at": _num(entry.get("entered_at")), "direction": str(entry.get("direction") or "N")}

    def _emit_trade(self, b: _Book, trade: PaperTrade) -> None:
        if trade.cost > _EPS:
            trade.return_pct = _r6(trade.pnl / trade.cost)
            days = max((trade.closed_at - trade.opened_at) / 86400.0, 1.0 / 24.0)
            trade.profit_per_capital_day = _r6(trade.pnl / (trade.cost * days))
        trade.hold_hours = _r6((trade.closed_at - trade.opened_at) / 3600.0)
        b.trades.append(trade)
        self._new_trades.append(trade)

    def _residue_trade(self, b: _Book, pos: PaperPosition, qty: float, price: float, now: float, *,
                       synthetic: bool) -> None:
        led = b.ledger.get(pos.position_id) or {}
        entry = b.entries.get(led.get("entry") or "") or {}
        common = self._trade_common(b, entry, now)
        cost = qty * pos.avg_cost
        proceeds = qty * price
        trade = PaperTrade(
            trade_id=common["trade_id"], portfolio_id=b.pid, idea_id=pos.idea_id, kind=pos.kind,
            exchange_ids=[pos.exchange_id], opened_at=pos.opened_at, closed_at=float(now), qty=float(qty),
            cost=round(cost, 6), proceeds=round(proceeds, 6), pnl=round(proceeds - cost, 6), exit_reason="legging",
            race_key=pos.race_key or f"market:{pos.market_id}", title=pos.title,
            legs=[{"exchange_id": pos.exchange_id, "side": pos.side, "qty": float(qty), "avg_cost": _r6(pos.avg_cost),
                   "avg_exit": _r6(price)}],
            synthetic=bool(synthetic), entered_at=common["entered_at"] or pos.entered_at, direction="N",
        )
        self._emit_trade(b, trade)

    def _close_position(self, b: _Book, pos: PaperPosition, now: float) -> None:
        pos.status = "closed"
        b.positions.pop(pos.exchange_id, None)
        led = b.ledger.pop(pos.position_id, None) or {}
        entry_id = led.get("entry")
        entry = b.entries.get(entry_id or "") or {}
        bought = float(led.get("bought_qty", 0.0))
        cost = float(led.get("bought_cost", 0.0))
        proceeds = float(led.get("proceeds", 0.0))
        reason = led.get("exit_reason") or "closed"
        leg = {"exchange_id": pos.exchange_id, "side": pos.side, "qty": bought, "avg_cost": _r6(pos.avg_cost),
               "avg_exit": _r6(proceeds / bought) if bought > _EPS else None, "cost": cost, "proceeds": proceeds,
               "reason": reason, "synthetic": bool(led.get("synthetic")), "opened_at": pos.opened_at,
               "market_id": pos.market_id, "title": pos.title}
        if pos.basket_id:
            rec = b.closing.setdefault(pos.basket_id, {"legs": [], "idea_id": pos.idea_id, "kind": pos.kind,
                                                       "entry": entry_id, "race_key": pos.race_key})
            rec["legs"].append(leg)
            still_open = any(p.basket_id == pos.basket_id for p in b.positions.values())
            if still_open:
                return
            b.closing.pop(pos.basket_id, None)
            b.exits.pop(pos.basket_id, None)
            self._basket_waiting.pop(pos.basket_id, None)
            legs = rec["legs"]
            total_bought = sum(float(lg["qty"]) for lg in legs)
            if total_bought > _EPS:
                common = self._trade_common(b, entry, now)
                reasons = [lg["reason"] for lg in legs]
                exit_reason = "legging" if "legging" in reasons else reasons[-1]
                cost_t = sum(float(lg["cost"]) for lg in legs)
                proc_t = sum(float(lg["proceeds"]) for lg in legs)
                race = pos.race_key or entry.get("race_key") or f"market:{legs[0].get('market_id')}"
                trade = PaperTrade(
                    trade_id=common["trade_id"], portfolio_id=b.pid, idea_id=pos.idea_id, kind=pos.kind,
                    exchange_ids=[lg["exchange_id"] for lg in legs], opened_at=min(float(lg["opened_at"]) for lg in legs),
                    closed_at=float(now), qty=min(float(lg["qty"]) for lg in legs), cost=round(cost_t, 6),
                    proceeds=round(proc_t, 6), pnl=round(proc_t - cost_t, 6), exit_reason=exit_reason, race_key=race,
                    title=str(entry.get("title") or pos.title),
                    legs=[{k: lg[k] for k in ("exchange_id", "side", "qty", "avg_cost", "avg_exit")} for lg in legs],
                    synthetic=any(lg["synthetic"] for lg in legs), entered_at=common["entered_at"] or pos.entered_at,
                    direction="N",
                )
                self._emit_trade(b, trade)
            self._idea_closed(b, pos.idea_id, entry_id, now)
            return
        if bought > _EPS:
            common = self._trade_common(b, entry, now)
            trade = PaperTrade(
                trade_id=common["trade_id"], portfolio_id=b.pid, idea_id=pos.idea_id, kind=pos.kind,
                exchange_ids=[pos.exchange_id], opened_at=pos.opened_at, closed_at=float(now), qty=bought,
                cost=round(cost, 6), proceeds=round(proceeds, 6), pnl=round(proceeds - cost, 6), exit_reason=reason,
                race_key=pos.race_key or f"market:{pos.market_id}", title=pos.title,
                legs=[{k: leg[k] for k in ("exchange_id", "side", "qty", "avg_cost", "avg_exit")}],
                synthetic=bool(led.get("synthetic")), entered_at=common["entered_at"] or pos.entered_at,
                direction=common["direction"],
            )
            self._emit_trade(b, trade)
        self._idea_closed(b, pos.idea_id, entry_id, now)

    def _idea_closed(self, b: _Book, idea_id: str, entry_id: Optional[str], now: float) -> None:
        b.cooldowns[idea_id] = float(now) + float(self._config.reentry_cooldown_s)
        if entry_id and entry_id in b.entries:
            busy = any((b.order_meta.get(o.order_id) or {}).get("entry") == entry_id for o in b.orders.values())
            held = any((b.ledger.get(p.position_id) or {}).get("entry") == entry_id for p in b.positions.values())
            if not busy and not held:
                b.entries.pop(entry_id, None)
        if idea_id in b.replace:
            b.replace.remove(idea_id)

    def _entry_resolved(self, b: _Book, entry_id: str, now: float) -> None:
        """After an entry order closes: for a set whose legs all have an outcome, size the sets and send the
        legging residue to an immediate exit (§6.3)."""
        entry = b.entries.get(entry_id)
        if entry is None:
            return
        open_legs = [o for o in b.orders.values() if (b.order_meta.get(o.order_id) or {}).get("entry") == entry_id
                     and o.purpose == "entry"]
        if open_legs:
            return
        filled: Dict[str, float] = dict(entry.get("filled") or {})
        if not entry.get("set"):
            held = any((b.ledger.get(p.position_id) or {}).get("entry") == entry_id for p in b.positions.values())
            if not held:
                b.entries.pop(entry_id, None)
            return
        if entry.get("resolved") or self._ending:
            return
        entry["resolved"] = True
        legs = list((entry.get("legs") or {}).keys())
        qtys = [float(filled.get(eid, 0.0)) for eid in legs]
        sets = min(qtys) if qtys else 0.0
        entry["sets"] = sets
        for eid in legs:
            pos = b.positions.get(eid)
            if pos is None or (b.ledger.get(pos.position_id) or {}).get("entry") != entry_id:
                continue
            residue = pos.qty - sets
            if residue > _EPS:
                led = b.ledger.get(pos.position_id) or {}
                led["residue"] = float(led.get("residue", 0.0)) + residue
                self._start_residue_exit(b, pos, residue, sets, now)
        held = any((b.ledger.get(p.position_id) or {}).get("entry") == entry_id for p in b.positions.values())
        if not held:
            b.entries.pop(entry_id, None)

    def _start_residue_exit(self, b: _Book, pos: PaperPosition, qty: float, sets: float, now: float) -> None:
        if any(o.exchange_id == pos.exchange_id and o.purpose == "exit" for o in b.orders.values()):
            return
        touch = _contract_bid(self._last_obs.quotes.get(pos.exchange_id) if self._last_obs else None, pos.side)
        self._new_order(b, idea_id=pos.idea_id, kind=pos.kind, purpose="exit", order_type="taker", eid=pos.exchange_id,
                        market_id=pos.market_id, side=pos.side, action="sell", qty=qty, limit=_depth.MIN_PRICE, now=now,
                        reason=f"Legging: the other legs filled only {sets:,.0f} sets", group_id=pos.basket_id,
                        decision_touch=touch, planned=touch, title=pos.title, option=pos.option,
                        meta={"exit_reason": "legging", "residue": True, "position": pos.position_id})
        self._step_report.exits_started += 1

    def _minus_consumed(self, b: _Book, eid: str, side: str, action: str, levels: List[Tuple[float, float]],
                        now: float) -> List[Tuple[float, float]]:
        cfg = self._config
        memory = max(float(cfg.consumed_memory_s), float(cfg.reentry_cooldown_s))
        b.consumed = [c for c in b.consumed if now - float(c[4]) <= memory + _EPS]
        bside = _book_side(side, action)
        out = [[p, q] for p, q in levels]
        tol = max(0, int(cfg.consumed_match_ticks)) * TICK + _EPS
        for c in b.consumed:
            if str(c[0]) != eid or c[1] != bside:
                continue
            price = float(c[2]) if side == "yes" else round(1.0 - float(c[2]), 6)
            qty = float(c[3])
            # the same YES price first, then the nearest levels within the tolerance (on a tie the level better
            # for the order: levels are best first), spilling over while the quantity we took is not used up
            near = [(round(abs(lv[0] - price), 9), k, lv) for k, lv in enumerate(out) if abs(lv[0] - price) <= tol]
            for _, _, lv in sorted(near, key=lambda x: (x[0], x[1])):
                if qty <= _EPS:
                    break
                take = min(lv[1], qty)
                lv[1] -= take
                qty -= take
        return [(p, q) for p, q in out if q > _EPS]

    def _consume(self, b: _Book, eid: str, side: str, action: str, levels: Sequence[Tuple[float, float]],
                 now: float) -> None:
        bside = _book_side(side, action)
        for p, q in levels:
            b.consumed.append([eid, bside, _yes_price(side, p), float(q), float(now)])

    # ------------------------------------------------------------------ phases
    def _settlements(self, b: _Book, obs: MarketObservation, open_ids: Set[str], now: float) -> None:
        report = self._step_report
        settled = obs.settlements or {}
        for eid in sorted(set(settled) & {o.exchange_id for o in b.orders.values()}):
            info = settled[eid]
            if info.refund or info.payout_yes is not None:
                for order in [o for o in b.orders.values() if o.exchange_id == eid]:
                    self._close_order(b, order, "cancelled", "The outcome settled", now)
                    report.orders_cancelled += 1
        for pos in sorted(list(b.positions.values()), key=lambda p: p.exchange_id):
            info = settled.get(pos.exchange_id)
            if info is not None and (info.refund or info.payout_yes is not None):
                if info.refund:
                    price, reason = pos.avg_cost, "refund"
                else:
                    payout = float(info.payout_yes)  # type: ignore[arg-type]
                    price, reason = (payout if pos.side == "yes" else 1.0 - payout), "settled"
                self._sell_position(b, pos, pos.qty, price, now, reason=reason, liquidity="settlement",
                                    purpose="settlement", action="settle")
                report.settled += 1
                continue
            if pos.exchange_id not in open_ids:
                if pos.status != "frozen":
                    pos.status = "frozen"
                    if "closed_no_ruling" not in pos.flags:
                        pos.flags.append("closed_no_ruling")
                    for order in [o for o in b.orders.values() if o.exchange_id == pos.exchange_id]:
                        self._close_order(b, order, "cancelled", "The market closed without a ruling", now)
                        report.orders_cancelled += 1
            elif pos.status == "frozen":
                pos.status = "open"
                pos.flags = [f for f in pos.flags if f != "closed_no_ruling"]

    def _quote_ok(self, obs: MarketObservation, eid: str, now: float) -> bool:
        q = obs.quotes.get(eid)
        return q is not None and now - float(q.ts) <= float(self._config.stale_quote_s) + _EPS

    def _taker_fills(self, b: _Book, obs: MarketObservation, open_ids: Set[str], now: float) -> None:
        report = self._step_report
        for order in sorted([o for o in b.orders.values() if o.status == "pending"], key=lambda o: (o.created_at, o.order_id)):
            if order.order_id not in b.orders:
                continue
            eid = order.exchange_id
            deadline = order.created_at + b.max_delay
            book = obs.books.get(eid)
            if (book is not None and self._satisfied(order, b, obs) and eid in open_ids
                    and self._quote_ok(obs, eid, now) and eid not in (obs.settlements or {})):
                buy = order.action == "buy"
                levels = self._minus_consumed(b, eid, order.side, order.action, contract_levels(book, order.side, order.action), now)
                remaining = order.qty - order.filled_qty
                res = walk(levels, remaining, order.limit_price, buy)
                b.fill_obs[order.order_id] = float(book.observed_at)
                synthetic = not _is_real(book)
                if res.filled > 0 and res.avg_price is not None:
                    if buy:
                        self._apply_buy(b, order, res.filled, res.avg_price, now, liquidity="taker", book_at=book.observed_at,
                                        levels=res.levels, trade_ids=(), synthetic=synthetic)
                        entry_id = (b.order_meta.get(order.order_id) or {}).get("entry")
                        if entry_id and entry_id in b.entries:
                            fl = b.entries[entry_id].setdefault("filled", {})
                            fl[eid] = float(fl.get(eid, 0.0)) + res.filled
                    else:
                        meta = b.order_meta.get(order.order_id) or {}
                        pos = b.positions.get(eid)
                        if pos is not None:
                            order.avg_fill_price = ((order.avg_fill_price or 0.0) * order.filled_qty
                                                    + res.filled * res.avg_price) / (order.filled_qty + res.filled)
                            order.filled_qty += res.filled
                            if synthetic:
                                order.synthetic = True
                            fill = self._sell_position(b, pos, res.filled, res.avg_price, now,
                                                       reason=str(meta.get("exit_reason") or "exit"), liquidity="taker",
                                                       purpose="exit", order=order, book_at=book.observed_at,
                                                       levels=res.levels, synthetic=synthetic,
                                                       residue=bool(meta.get("residue")))
                            ex = self._exec(b, order.kind)
                            if fill is not None and order.planned_price is not None:
                                ex["exit_slip_sum"] += float(order.planned_price) - float(res.avg_price)
                                ex["exit_slip_n"] += 1
                    self._consume(b, eid, order.side, order.action, res.levels, now)
                    report.fills += 1
                if order.order_id in b.orders:
                    if order.filled_qty >= order.qty - _EPS:
                        self._close_order(b, order, "filled", None, now)
                    elif order.purpose == "entry":
                        self._close_order(b, order, "cancelled",
                                          f"The book had only {order.filled_qty:,.0f} of {order.qty:,.0f} shares at "
                                          f"{order.limit_price:.3f} or better", now)
                        report.orders_cancelled += 1
                    elif eid not in b.positions:
                        self._close_order(b, order, "cancelled", "Nothing left to sell", now)
            if order.order_id in b.orders and now > deadline + _EPS:
                if order.purpose == "entry":
                    why = f"No order book read within {b.max_delay:.0f} s of the decision: not filled"
                else:
                    why = (f"Expired after {b.max_delay:.0f} s with {order.filled_qty:,.0f} of {order.qty:,.0f} shares sold"
                           if order.filled_qty > _EPS else f"No order book read within {b.max_delay:.0f} s of the exit "
                                                            "decision: not sold")
                self._close_order(b, order, "expired", why, now)
                report.orders_expired += 1

    def _maker_fills(self, b: _Book, obs: MarketObservation, open_ids: Set[str], now: float, signal_ids: Set[str],
                     tape_truncated: Set[str]) -> None:
        cfg = self._config
        report = self._step_report
        for order in sorted([o for o in b.orders.values() if o.status in ("resting", "cancelling")],
                            key=lambda o: (o.created_at, o.order_id)):
            eid = order.exchange_id
            meta = b.order_meta.setdefault(order.order_id, {})
            if order.status == "resting":
                if order.idea_id in signal_ids:
                    order.missing_steps = 0
                else:
                    order.missing_steps += 1
                if order.expires_at is not None and now >= order.expires_at - _EPS:
                    # the person sets the expiry when placing the order (OrderInput.expirationDate), so the
                    # exchange removes it on time at any speed
                    order.status, order.cancel_at = "cancelling", float(order.expires_at)
                    meta["cause"] = "expired"
                elif order.missing_steps >= int(cfg.maker_cancel_after_missing_steps):
                    # a cancel takes this portfolio's latency to reach the market, exactly like the placement: a
                    # person needs ~4 minutes to cancel by hand, so the prints of a news sweep in those minutes still
                    # fill the order (prints up to cancel_at count, §6.4)
                    order.status, order.cancel_at = "cancelling", float(now) + b.latency
                    meta["cause"] = "cancelled"
                self._dirty_orders[order.order_id] = order
            tape = obs.trades.get(eid) if obs.trades else None
            if tape is not None and eid in open_ids and eid not in (obs.settlements or {}):
                eligible = _eligible_prints(order, tape, b.latency, now)
                qty, ids, newest, queue_after = maker_fills(order, tape, min_latency_s=b.latency,
                                                            queue_haircut=cfg.queue_haircut, now=now)
                if eligible:
                    seen = list(order.seen_trade_ids) + [str(t.trade_id) for t in eligible]
                    order.seen_trade_ids = seen[-SEEN_TRADE_IDS_MAX:]
                    newest_seen = max(t.ts for t in eligible)
                    order.last_trade_ts = newest_seen if order.last_trade_ts is None else max(order.last_trade_ts, newest_seen)
                order.queue_ahead = queue_after
                if eid in tape_truncated:
                    order.tape_gap = True
                if qty > 0:
                    synthetic = any(str(i).startswith("candle:") for i in ids)
                    fill_reason = "candle fill (assumed)" if synthetic else ""
                    self._apply_buy(b, order, qty, order.limit_price, now, liquidity="maker", book_at=newest, levels=(),
                                    trade_ids=ids, synthetic=synthetic, reason=fill_reason)
                    report.fills += 1
                self._dirty_orders[order.order_id] = order
            if order.order_id in b.orders and order.filled_qty >= order.qty - _EPS:
                self._close_order(b, order, "filled", None, now)
                continue
            if order.status == "cancelling" and order.order_id in b.orders:
                read_after = (tape is not None and order.cancel_at is not None
                              and float(obs.now) >= float(order.cancel_at) - _EPS)
                timed_out = order.cancel_at is not None and now >= float(order.cancel_at) + b.max_delay - _EPS
                if read_after or timed_out:
                    cause = meta.get("cause", "cancelled")
                    if cause == "expired":
                        self._close_order(b, order, "expired", "Expired unfilled" if order.filled_qty <= _EPS
                                          else "Expired partly filled", now)
                        report.orders_expired += 1
                    else:
                        self._close_order(b, order, "cancelled", meta.get("why") or "The conditions for this resting "
                                          "order no longer hold", now)
                        report.orders_cancelled += 1

    def _marks(self, b: _Book, obs: MarketObservation, now: float) -> None:
        cfg = self._config
        post_cup = now >= float(obs.cup_end) - _EPS
        for pos in b.positions.values():
            q = obs.quotes.get(pos.exchange_id)
            flags = [f for f in pos.flags if f in ("closed_no_ruling", "synthetic", "post_cup")]
            if pos.bold:
                flags.append("bold")
            if pos.status == "frozen":
                pos.flags = flags
                continue
            if post_cup:
                if "post_cup" not in flags:
                    flags.append("post_cup")
                pos.flags = flags
                if pos.liq_value is not None:
                    continue
            if q is None or now - float(q.ts) > cfg.stale_quote_s + _EPS:
                flags.append("stale_quote")
            book = obs.books.get(pos.exchange_id)

            def minus_ours(levels: List[Tuple[float, float]], p: PaperPosition = pos) -> List[Tuple[float, float]]:
                # the bids this portfolio already sold into are not there for it again (§6.3, D35): marks and
                # fills agree on depth
                return self._minus_consumed(b, p.exchange_id, p.side, "sell", levels, now)

            value, state = liquidation_value(pos.side, pos.qty, book, q, last_real_book=self._last_real_books.get(pos.exchange_id),
                                             now=now, config=cfg, adjust_levels=minus_ours)
            pos.liq_value = value
            pos.depth_state = state
            if state == "stale":
                flags.append("depth_stale")
            elif state == "unknown":
                flags.append("depth_unknown")
            pos.mark_value = mid_value(pos.side, pos.qty, q)
            fv = (obs.fair_values or {}).get(pos.exchange_id)
            fv_c = self._fv_contract(fv, pos.side, now)
            pos.fv_value = pos.qty * fv_c if fv_c is not None else None
            pos.last_marked_at = float(now)
            pos.flags = list(dict.fromkeys(flags))

    def _fv_contract(self, fv: Any, side: str, now: float) -> Optional[float]:
        if fv is None or not getattr(fv, "usable", False) or getattr(fv, "suspect", False):
            return None
        v = _num(getattr(fv, "value", None))
        if v is None:
            return None
        params = self._config.params
        src = str(getattr(fv, "source", "") or "")
        max_age = params.manual_fv_max_age_s if src == "manual" else (
            params.history_fv_max_age_s if getattr(fv, "history", False) else params.fv_max_age_s)
        as_of = _num(getattr(fv, "as_of", None))
        if as_of is not None and now - as_of > max_age + _EPS:
            return None
        return v if side == "yes" else 1.0 - v

    def _fv_for_exit(self, fv_c: float) -> float:
        """The contract fair value an exit target is built on: strategy.value_opportunity's longshot rule (a contract
        whose fair value is below 0.15 is valued at fv x (1 - longshot_shrink))."""
        params = self._config.params or StrategyParams()
        shrink = float(getattr(params, "longshot_shrink", 0.0) or 0.0)
        return fv_c * (1.0 - shrink) if fv_c < 0.15 else fv_c

    def _exits(self, b: _Book, obs: MarketObservation, open_ids: Set[str], now: float) -> None:
        if now >= float(obs.cup_end) - _EPS:
            return
        report = self._step_report
        cfg = self._config
        exiting = {o.exchange_id for o in b.orders.values() if o.purpose == "exit"}
        # residue that still needs selling (an earlier residue exit expired)
        for pos in sorted(b.positions.values(), key=lambda p: p.exchange_id):
            led = b.ledger.get(pos.position_id) or {}
            res = float(led.get("residue", 0.0))
            if res > _EPS and pos.exchange_id not in exiting and pos.status == "open" and pos.exchange_id in open_ids:
                entry = b.entries.get(led.get("entry") or "") or {}
                self._start_residue_exit(b, pos, min(res, pos.qty), float(entry.get("sets") or 0.0), now)
                exiting.add(pos.exchange_id)
        # baskets
        baskets: Dict[str, List[PaperPosition]] = {}
        for pos in b.positions.values():
            if pos.basket_id:
                baskets.setdefault(pos.basket_id, []).append(pos)
        for basket_id in sorted(baskets):
            legs = sorted(baskets[basket_id], key=lambda p: p.exchange_id)
            self._basket_exit(b, basket_id, legs, obs, open_ids, now, exiting)
        # single positions
        for pos in sorted([p for p in b.positions.values() if not p.basket_id], key=lambda p: p.exchange_id):
            if pos.status != "open" or pos.exchange_id in exiting or pos.exchange_id not in open_ids:
                continue
            plan = pos.exit_plan
            q = obs.quotes.get(pos.exchange_id)
            fresh = self._quote_ok(obs, pos.exchange_id, now)
            bid = _contract_bid(q, pos.side) if fresh else None
            rule: Optional[Tuple[str, float, Optional[float]]] = None  # (reason, limit, planned)
            if plan is not None and plan.exit_before_ts is not None and now >= plan.exit_before_ts - _EPS:
                rule = ("regime", _depth.MIN_PRICE, bid)
            elif plan is not None and bid is not None and plan.stop_bid is not None and bid <= plan.stop_bid + _EPS:
                rule = ("stop", _depth.MIN_PRICE, plan.stop_bid)
            elif plan is not None and bid is not None:
                target, why = plan.target_bid, "target"
                if plan.dynamic_fv_target:
                    fv_c = self._fv_contract((obs.fair_values or {}).get(pos.exchange_id), pos.side, now)
                    if fv_c is not None and q is not None and q.bid is not None and q.ask is not None:
                        half = (float(q.ask) - float(q.bid)) / 2.0
                        # the same contract fair value the idea used: a longshot (< 0.15) is shrunk as in
                        # strategy.value_opportunity, so the simulated exit is the one the card tells you to follow
                        target = _depth.floor_tick(self._fv_for_exit(fv_c) - half - float(plan.exit_buffer or 0.0))
                        fv_entry = _num((b.ledger.get(pos.position_id) or {}).get("fv_entry"))
                        moved = fv_entry is not None and abs(fv_c - fv_entry) >= TICK - _EPS
                        why = "fair_value" if moved else "converged"
                if target is not None and bid >= target - _EPS:
                    rule = (why, _depth.clamp_price(round(target - TICK, 6)), target)
            if rule is None and plan is not None:
                if plan.time_stop_ts is not None and now >= plan.time_stop_ts - _EPS:
                    rule = ("time", _depth.MIN_PRICE, bid)
                elif (plan.time_stop_after_fill_s is not None and pos.first_fill_at is not None
                      and now >= pos.first_fill_at + plan.time_stop_after_fill_s - _EPS):
                    rule = ("time", _depth.MIN_PRICE, bid)
            if rule is None and pos.idea_id in b.replace and bid is not None:
                rule = ("replaced", _depth.clamp_price(round(bid - TICK, 6)), bid)
            if rule is None:
                continue
            reason, limit, planned = rule
            self._cancel_entry_orders(b, pos, now)
            self._new_order(b, idea_id=pos.idea_id, kind=pos.kind, purpose="exit", order_type="taker",
                            eid=pos.exchange_id, market_id=pos.market_id, side=pos.side, action="sell", qty=pos.qty,
                            limit=limit, now=now, reason=_EXIT_TEXT.get(reason, reason), decision_touch=bid,
                            planned=planned, title=pos.title, option=pos.option,
                            meta={"exit_reason": reason, "position": pos.position_id})
            exiting.add(pos.exchange_id)
            report.exits_started += 1
            if reason == "replaced" and pos.idea_id in b.replace:
                b.replace.remove(pos.idea_id)
        b.replace = [i for i in b.replace if any(p.idea_id == i for p in b.positions.values())]

    def _cancel_entry_orders(self, b: _Book, pos: PaperPosition, now: float) -> None:
        for order in [o for o in b.orders.values() if o.purpose == "entry" and o.exchange_id == pos.exchange_id]:
            self._close_order(b, order, "cancelled", "Exit started", now)
            self._step_report.orders_cancelled += 1

    def _sell_book(self, eid: str, obs: MarketObservation, now: float) -> Optional[BookObservation]:
        cfg = self._config
        cands = [bk for bk in (obs.books.get(eid), self._last_real_books.get(eid)) if _is_real(bk)]
        cands = [bk for bk in cands if now - bk.observed_at <= cfg.mark_book_max_age_s + _EPS]
        cands.sort(key=lambda bk: -bk.observed_at)
        return cands[0] if cands else None

    def _basket_exit(self, b: _Book, basket_id: str, legs: List[PaperPosition], obs: MarketObservation,
                     open_ids: Set[str], now: float, exiting: Set[str]) -> None:
        report = self._step_report
        if any(p.status != "open" for p in legs):
            return
        pending = [o for o in b.orders.values() if o.purpose == "exit" and o.group_id == basket_id
                   and not (b.order_meta.get(o.order_id) or {}).get("residue")]
        rec = b.exits.get(basket_id)
        if rec is not None:
            start = rec.get("start") or {}
            sold_any = any(float(start.get(p.exchange_id, p.qty)) - p.qty > _EPS for p in legs) or len(legs) < len(start)
            qtys = [p.qty for p in legs]
            unmatched = len(legs) < len(start) or (max(qtys) - min(qtys) > _EPS if qtys else False)
            # a set with one leg (wholly or partly) sold is a naked position, not a basket (§6.5, D29): once the exit
            # left the legs unmatched -- or it is already a legging exit -- the rest is sold as soon as any leg has no
            # live order (it expired) or the group is past its fill delay, partly filled or not
            broken = rec.get("reason") == "legging" or (sold_any and unmatched)
            if broken:
                past = now >= float(rec.get("created_at", now)) + b.max_delay - _EPS
                stuck = past or any(not any(o.exchange_id == p.exchange_id for o in pending) for p in legs)
                if stuck:
                    for o in pending:
                        self._close_order(b, o, "cancelled", "Legging out: selling every remaining leg", now)
                        report.orders_cancelled += 1
                    self._start_basket_exit(b, basket_id, legs, "legging",
                                            {p.exchange_id: _depth.MIN_PRICE for p in legs}, obs, now, exiting)
                    return
            if pending:
                exiting.update(o.exchange_id for o in pending)
                return
            # the exit orders expired with every leg still matched (nothing sold, or the same quantity of every leg):
            # the rest is still a whole set; evaluate it again
            b.exits.pop(basket_id, None)
        elif pending:
            exiting.update(o.exchange_id for o in pending)
            return
        if any(p.exchange_id in exiting for p in legs):
            return
        self._basket_waiting.pop(basket_id, None)  # re-requested below only while the touch still says "exit"
        plan = legs[0].exit_plan
        quotes = [obs.quotes.get(p.exchange_id) for p in legs]
        fresh = all(self._quote_ok(obs, p.exchange_id, now) for p in legs) and all(p.exchange_id in open_ids for p in legs)
        if not all(p.exchange_id in open_ids for p in legs):
            return
        reason: Optional[str] = None
        limits: Dict[str, float] = {}
        if plan is not None and plan.exit_before_ts is not None and now >= plan.exit_before_ts - _EPS:
            reason = "regime"
        elif fresh and plan is not None and plan.min_set_profit is not None:
            bids = [_contract_bid(q, p.side) for q, p in zip(quotes, legs)]
            if all(x is not None for x in bids):
                sets = min(p.qty for p in legs)
                set_cost = sum(p.avg_cost for p in legs)
                touch_profit = sum(float(x) for x in bids) - set_cost  # type: ignore[arg-type]
                if touch_profit >= float(plan.min_set_profit) - _EPS:
                    books = {p.exchange_id: self._sell_book(p.exchange_id, obs, now) for p in legs}
                    missing = [eid for eid, bk in books.items() if bk is None]
                    if missing:
                        self._basket_waiting[basket_id] = [p.exchange_id for p in legs]
                    else:
                        self._basket_waiting.pop(basket_id, None)
                        proceeds = 0.0
                        for p in legs:
                            levels = contract_levels(books[p.exchange_id], p.side, "sell")
                            levels = self._minus_consumed(b, p.exchange_id, p.side, "sell", levels, now)
                            res = walk(levels, p.qty, 0.0, buy=False)
                            got, _ = _depth.liquidation_proceeds(levels, p.qty)
                            proceeds += got
                            lowest = min((lv[0] for lv in res.levels), default=None)
                            limits[p.exchange_id] = _depth.clamp_price(lowest if lowest is not None else _depth.MIN_PRICE)
                        cost_total = sum(p.qty * p.avg_cost for p in legs)
                        if proceeds - cost_total >= float(plan.min_set_profit) * sets - _EPS:
                            reason = "converged"
                        else:
                            limits = {}
        if reason is None and plan is not None:
            first = min((p.first_fill_at for p in legs if p.first_fill_at is not None), default=None)
            if plan.time_stop_ts is not None and now >= plan.time_stop_ts - _EPS:
                reason = "time"
            elif plan.time_stop_after_fill_s is not None and first is not None and now >= first + plan.time_stop_after_fill_s - _EPS:
                reason = "time"
        if reason is None and legs[0].idea_id in b.replace and fresh:
            reason = "replaced"
        if reason is None:
            return
        if reason != "converged":
            limits = {}
            for p, q in zip(legs, quotes):
                bid = _contract_bid(q, p.side) if fresh else None
                limits[p.exchange_id] = (_depth.clamp_price(round(bid - TICK, 6)) if reason == "replaced" and bid
                                         else _depth.MIN_PRICE)
        self._start_basket_exit(b, basket_id, legs, reason, limits, obs, now, exiting)

    def _start_basket_exit(self, b: _Book, basket_id: str, legs: List[PaperPosition], reason: str,
                           limits: Mapping[str, float], obs: MarketObservation, now: float, exiting: Set[str]) -> None:
        for p in legs:
            self._cancel_entry_orders(b, p, now)
            q = obs.quotes.get(p.exchange_id)
            bid = _contract_bid(q, p.side)
            planned = bid if reason != "converged" else limits.get(p.exchange_id)
            self._new_order(b, idea_id=p.idea_id, kind=p.kind, purpose="exit", order_type="taker", eid=p.exchange_id,
                            market_id=p.market_id, side=p.side, action="sell", qty=p.qty,
                            limit=float(limits.get(p.exchange_id, _depth.MIN_PRICE)), now=now,
                            reason=_EXIT_TEXT.get(reason, reason), group_id=basket_id, decision_touch=bid, planned=planned,
                            title=p.title, option=p.option, meta={"exit_reason": reason, "position": p.position_id})
            exiting.add(p.exchange_id)
        b.exits[basket_id] = {"reason": reason, "created_at": float(now), "start": {p.exchange_id: p.qty for p in legs}}
        self._basket_waiting.pop(basket_id, None)
        self._step_report.exits_started += 1

    # ------------------------------------------------------------------ entries
    def _exposure(self, b: _Book) -> ExposureSummary:
        exp = ExposureSummary()
        groups: Set[str] = set()
        for pos in b.positions.values():
            if pos.status == "closed":
                continue
            cost = float(pos.cost)
            exp.gross_cost += cost
            race = pos.race_key or f"market:{pos.market_id}"
            exp.by_race[race] = exp.by_race.get(race, 0.0) + cost
            exp.by_idea[pos.idea_id] = exp.by_idea.get(pos.idea_id, 0.0) + cost
            if pos.kind not in SET_KINDS and pos.factor_delta is not None:
                exp.national_tilt_d += pos.qty * float(pos.factor_delta)
            groups.add(pos.basket_id or pos.position_id)
            entry = b.entries.get((b.ledger.get(pos.position_id) or {}).get("entry") or "") or {}
            if entry.get("directional"):
                exp.directional[pos.idea_id] = float(pos.score or 0.0)
                exp.directional_sign[pos.idea_id] = int(math.copysign(1, pos.factor_delta)) if pos.factor_delta else 0
            if pos.bold:
                exp.bold_idea = pos.idea_id
        for o in b.orders.values():
            if o.purpose != "entry":
                continue
            entry = b.entries.get((b.order_meta.get(o.order_id) or {}).get("entry") or "") or {}
            reserved = float(o.reserved_cash)
            exp.gross_cost += reserved
            leg = (entry.get("legs") or {}).get(o.exchange_id) or {}
            race = leg.get("race_key") or entry.get("race_key") or f"market:{o.market_id}"
            exp.by_race[race] = exp.by_race.get(race, 0.0) + reserved
            exp.by_idea[o.idea_id] = exp.by_idea.get(o.idea_id, 0.0) + reserved
            delta = _num(leg.get("factor_delta"))
            if o.kind not in SET_KINDS and delta is not None:
                exp.national_tilt_d += (o.qty - o.filled_qty) * delta
            if entry.get("directional"):
                exp.directional[o.idea_id] = float(entry.get("score") or 0.0)
                exp.directional_sign[o.idea_id] = int(math.copysign(1, delta)) if delta else 0
            if entry.get("bold"):
                exp.bold_idea = o.idea_id
            groups.add(entry.get("entry_id") or o.order_id)
        exp.open_positions = len({p.basket_id or p.position_id for p in b.positions.values() if p.status != "closed"})
        exp.gross_cost = round(exp.gross_cost, 6)
        exp.national_tilt_d = round(exp.national_tilt_d, 6)
        return exp

    def _sizing_ctx(self, b: _Book, obs: MarketObservation, inputs: Optional[StrategyInputs], now: float) -> SizingContext:
        val = b.val or self._valuation(b)
        exp = self._exposure(b)
        expected = sum(p.qty * float(p.edge_per_unit or 0.0) for p in b.positions.values() if p.status == "open")
        cup_end = float(obs.cup_end)
        return SizingContext(
            now=float(now), equity=float(val["equity_liq"]), cash=float(b.cash), free_cash=max(0.0, b.free_cash()),
            start_capital=b.start_capital, cup_end=cup_end, days_left=max(0.0, (cup_end - now) / 86400.0), exposure=exp,
            bar=getattr(inputs, "bar", None) if inputs is not None else None, expected_profit=expected,
            my_rank=getattr(inputs, "my_rank", None) if inputs is not None else None, regime=self._config.regime,
            all_collateral=bool(self._config.all_collateral), races=dict(obs.races or {}),
            prev_mode=b.mode.get("prev_mode"), mode_since=_num(b.mode.get("mode_since")), params=self._config.params,
        )

    def _diag_kind(self, b: _Book, kind: str) -> Dict[str, Any]:
        kinds = b.diagnostics.setdefault("kinds", {})
        return kinds.setdefault(kind, {"signal_steps": 0, "signals": 0, "entered": 0, "blocked": {}})

    def _block(self, b: _Book, kind: str, reason: str) -> None:
        d = self._diag_kind(b, kind)
        d["blocked"][reason] = int(d["blocked"].get(reason, 0)) + 1

    def _entries(self, b: _Book, obs: MarketObservation, signals: Sequence[Opportunity], inputs: Optional[StrategyInputs],
                 open_ids: Set[str], now: float, traded: Set[str], bookless: List[Tuple[float, str, List[str]]]) -> None:
        cfg = self._config
        report = self._step_report
        kinds = set(b.spec.kinds) if b.spec.kinds is not None else set(TRADE_KINDS)
        kinds &= set(TRADE_KINDS)
        seen_kinds: Set[str] = set()
        for opp in signals:
            if opp.kind in kinds:
                d = self._diag_kind(b, opp.kind)
                d["signals"] += 1
                seen_kinds.add(opp.kind)
        for k in sorted(seen_kinds):
            self._diag_kind(b, k)["signal_steps"] += 1
        b.deferred = []
        if not signals:
            return
        cup_over = now >= float(obs.cup_end) - _EPS
        held_ideas = {p.idea_id for p in b.positions.values()} | {o.idea_id for o in b.orders.values()}
        busy_eids = set(b.positions) | {o.exchange_id for o in b.orders.values()}
        n_groups = len({p.basket_id or p.position_id for p in b.positions.values()})
        eligible: List[Opportunity] = []
        for opp in signals:
            if opp.kind not in kinds:
                continue
            idea = opp.idea_id or _fallback_idea_id(opp)
            eids = _opp_exchanges(opp)
            if cup_over:
                self._block(b, opp.kind, "cup_end")
                continue
            if opp.limit_price is None or not eids:
                self._block(b, opp.kind, "no_limit")
                continue
            if any(e not in open_ids or e in (obs.settlements or {}) for e in eids):
                self._block(b, opp.kind, "closed")
                continue
            if any(not self._quote_ok(obs, e, now) for e in eids):
                self._block(b, opp.kind, "stale_quote")
                continue
            if idea in held_ideas:
                self._block(b, opp.kind, "held")
                continue
            if b.cooldowns.get(idea, -1.0) > now + _EPS:
                self._block(b, opp.kind, "cooldown")
                continue
            if any(e in busy_eids for e in eids):
                self._block(b, opp.kind, "exchange")
                continue
            if opp.kind in SET_KINDS or opp.unit == "sets":
                fresh = all(_is_real(obs.books.get(e)) and now - obs.books[e].observed_at <= cfg.set_book_max_age_s + _EPS
                            for e in eids)
                if not fresh:
                    self._block(b, opp.kind, "deferred")
                    b.deferred.append(idea)
                    self._deferred_sets[idea] = list(eids)
                    report.sets_deferred += 1
                    continue
            if opp.idea_id is None:
                opp = dataclasses.replace(opp, idea_id=idea)
            eligible.append(opp)
        if not eligible:
            return
        if n_groups >= int(cfg.max_open_positions):
            for opp in eligible:
                self._block(b, opp.kind, "max_positions")
            return
        max_groups = int(cfg.max_open_positions)
        policy = self._policy(b.spec.policy)
        ctx = self._sizing_ctx(b, obs, inputs, now)
        b.last_ctx = ctx
        try:
            decisions: Dict[str, SizeDecision] = policy.size_all(eligible, ctx)
        except Exception as exc:
            log.warning("paper: sizing failed for %s: %s", b.pid, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            report.errors.append(f"{b.pid}: sizing failed: {exc}")
            for opp in eligible:
                self._block(b, opp.kind, "sizing_error")
            return
        mode = next((d.mode for d in decisions.values() if d is not None and d.mode), None)
        if mode and mode != b.mode.get("prev_mode"):
            b.mode = {"prev_mode": mode, "mode_since": float(now)}
        created = 0
        for opp in sorted(eligible, key=lambda o: (-float(o.score or 0.0), str(o.idea_id))):
            dec = decisions.get(str(opp.idea_id))
            if dec is None:
                self._block(b, opp.kind, "sized_zero")
                continue
            if dec.replaces:
                if dec.replaces not in b.replace and any(p.idea_id == dec.replaces for p in b.positions.values()):
                    b.replace.append(dec.replaces)
                self._block(b, opp.kind, "replace")
                continue
            units = math.floor(float(dec.units or 0) + _EPS)
            eids = _opp_exchanges(opp)
            if units >= 1 and not opp.levels:
                bookless.append((-float(opp.score or 0.0), str(opp.idea_id), list(eids)))
            is_set = opp.kind in SET_KINDS or opp.unit == "sets"
            if is_set and units >= 1:
                leg_levels = []
                for leg in opp.legs:
                    lv = contract_levels(obs.books.get(str(leg.get("exchange_id"))), str(leg.get("side")), "buy")
                    lim = _num(leg.get("limit")) or _num(leg.get("price")) or 0.0
                    leg_levels.append([x for x in lv if x[0] <= lim + _EPS])
                limit_total = float(opp.limit_price or 0.0)
                cap = _depth.walk_set(leg_levels, limit_total) if all(leg_levels) else 0.0
                cap = min(cap, min((sum(q for _, q in lv) for lv in leg_levels), default=0.0))
                units = min(units, math.floor(cap + _EPS))
            if units < 1:
                self._block(b, opp.kind, "sized_zero")
                continue
            n_orders = len(eids)
            if n_groups >= max_groups:
                self._block(b, opp.kind, "max_positions")
                continue
            if created + n_orders > int(cfg.max_new_orders_per_step):
                self._block(b, opp.kind, "order_cap")
                continue
            if any(e in busy_eids for e in eids):
                self._block(b, opp.kind, "exchange")
                continue
            entered = self._enter(b, opp, units, obs, now, bold=bool(dec.bold))
            if not entered:
                self._block(b, opp.kind, "cash")
                continue
            created += n_orders
            busy_eids.update(eids)
            traded.add(str(opp.idea_id))
            self._diag_kind(b, opp.kind)["entered"] += 1
            report.orders_created += n_orders
            n_groups += 1

    def _enter(self, b: _Book, opp: Opportunity, units: int, obs: MarketObservation, now: float, *,
               bold: bool = False) -> bool:
        cfg = self._config
        idea = str(opp.idea_id)
        is_set = opp.kind in SET_KINDS or opp.unit == "sets"
        races = obs.races or {}
        legs_meta: Dict[str, Dict[str, Any]] = {}
        fair = obs.fair_values or {}
        if is_set:
            n = max(1, len(opp.legs))
            for leg in opp.legs:
                eid = str(leg.get("exchange_id"))
                side = str(leg.get("side"))
                race = races.get(eid)
                legs_meta[eid] = {"side": side, "limit": _num(leg.get("limit")) or _num(leg.get("price")),
                                  "factor_delta": 0.0, "race_key": leg.get("race_key") or (race.race_key if race else None),
                                  "fv": self._fv_contract(fair.get(eid), side, now), "market_id": leg.get("market_id"),
                                  "title": leg.get("title"), "option": leg.get("option")}
            limit_total = float(opp.limit_price or sum(float(m["limit"] or 0.0) for m in legs_meta.values()))
            floor = _num(opp.bet.floor) if opp.bet is not None else None
            all_coll = bool(cfg.all_collateral) and opp.bet is not None and opp.bet.kind == "riskless" and floor is not None
            if all_coll:
                rpu_total = max(0.0, limit_total - float(floor))  # type: ignore[arg-type]
                need = units * rpu_total
            else:
                need = units * sum(float(m["limit"] or 0.0) for m in legs_meta.values())
            if need > b.free_cash() + 1e-6:
                units = math.floor(b.free_cash() / max(rpu_total if all_coll else limit_total, 1e-9) + _EPS)
                if units < 1:
                    return False
            edge_per_unit = float(opp.edge or 0.0) / n
            floor_per_unit = float(floor) / n if floor is not None else None
        else:
            eid = str(opp.exchange_id)
            race = races.get(eid)
            legs_meta[eid] = {"side": opp.side, "limit": float(opp.limit_price or 0.0), "factor_delta": _num(opp.factor_delta),
                              "race_key": opp.race_key or (race.race_key if race else None),
                              "fv": _num(opp.fair_value) if opp.fair_value is not None else self._fv_contract(fair.get(eid), opp.side, now),
                              "market_id": opp.market_id, "title": opp.title, "option": opp.option}
            limit = float(opp.limit_price or 0.0)
            if units * limit > b.free_cash() + 1e-6:
                units = math.floor(b.free_cash() / max(limit, 1e-9) + _EPS)
                if units < 1:
                    return False
            edge_per_unit = float(opp.edge or 0.0)
            floor_per_unit = None
            all_coll = False
        b.counters["b"] = int(b.counters.get("b", 0)) + (1 if is_set else 0)
        basket_id = f"{b.pid}:b{b.counters['b']}" if is_set else None
        directional = bool(opp.bet is not None and opp.bet.kind in ("binary", "bracket"))
        entry_id = basket_id or f"{b.pid}:e{b.counters.get('o', 0) + 1}"
        dec_reason = (opp.rationale[0] if opp.rationale else f"{opp.kind} idea")
        # a bold-to-goal stake (§5.6.4, D13) is sized so that a 1/0 WIN reaches the bar: it is held to settlement
        # (sizing.bold_exit_plan: no convergence target, no stop, only the regime exit), never sold at the idea's
        # own target for a few cents a share -- the simulator tests the bet the Strategy card tells you to place
        plan = _bold_hold_plan(opp) if bold and not is_set else opp.exit_plan
        entry = {
            "entry_id": entry_id, "idea_id": idea, "kind": opp.kind, "basket_id": basket_id, "set": is_set,
            "legs": legs_meta, "exit_plan": plan.to_dict() if plan is not None else None,
            "edge_per_unit": edge_per_unit, "race_key": opp.race_key, "score": float(opp.score or 0.0),
            "bold": bool(bold),
            "floor_per_unit": floor_per_unit, "entered_at": float(now),
            "direction": "N" if is_set else _direction(opp.factor_delta), "directional": directional,
            "title": opp.title, "filled": {}, "resolved": False, "all_collateral": all_coll,
            "advance_ratio": (min(1.0, float(floor) / limit_total) if all_coll and limit_total > 0 else 0.0)
            if is_set else 0.0,
        }
        b.entries[entry_id] = entry
        if opp.order_type == "maker" and not is_set:
            eid = str(opp.exchange_id)
            book = obs.books.get(eid)
            if not _is_real(book):
                book = self._last_real_books.get(eid)
            qa = queue_ahead_at(book, opp.side, float(opp.limit_price or 0.0), now, cfg)
            touch = _contract_ask(obs.quotes.get(eid), opp.side)
            self._new_order(b, idea_id=idea, kind=opp.kind, purpose="entry", order_type="maker", eid=eid,
                            market_id=opp.market_id, side=opp.side, action="buy", qty=units,
                            limit=float(opp.limit_price or 0.0), now=now, reason=dec_reason, expires_at=opp.expires_at,
                            decision_touch=touch, title=opp.title, option=opp.option, meta={"entry": entry_id},
                            queue_ahead=qa)
            return True
        if is_set:
            n = max(1, len(opp.legs))
            for leg in opp.legs:
                eid = str(leg.get("exchange_id"))
                m = legs_meta[eid]
                rpu = (max(0.0, float(opp.limit_price or 0.0) - float(opp.bet.floor or 0.0)) / n  # type: ignore[union-attr]
                       if all_coll else None)
                touch = _contract_ask(obs.quotes.get(eid), m["side"])
                self._new_order(b, idea_id=idea, kind=opp.kind, purpose="entry", order_type="taker", eid=eid,
                                market_id=str(leg.get("market_id") or opp.market_id), side=m["side"], action="buy",
                                qty=units, limit=float(m["limit"] or 0.0), now=now, reason=dec_reason,
                                group_id=basket_id, decision_touch=touch, title=str(leg.get("title") or opp.title),
                                option=leg.get("option"), reserve_per_unit=rpu, meta={"entry": entry_id})
            return True
        eid = str(opp.exchange_id)
        touch = _contract_ask(obs.quotes.get(eid), opp.side)
        self._new_order(b, idea_id=idea, kind=opp.kind, purpose="entry", order_type="taker", eid=eid,
                        market_id=opp.market_id, side=opp.side, action="buy", qty=units, limit=float(opp.limit_price or 0.0),
                        now=now, reason=dec_reason, decision_touch=touch, title=opp.title, option=opp.option,
                        meta={"entry": entry_id})
        return True

    # ------------------------------------------------------------------ valuation
    def _valuation(self, b: _Book) -> Dict[str, Any]:
        liq = mark = 0.0
        fv_total = 0.0
        fv_any = False
        cost_open = 0.0
        unvalued = 0.0
        unvalued_n = 0
        unknown = stale = 0.0
        advances = 0.0
        for pos in b.positions.values():
            advances += float(pos.collateral_advance or 0.0)
            if pos.status == "frozen":
                unvalued += float(pos.liq_value or 0.0)
                unvalued_n += 1
                cost_open += float(pos.cost)  # counted at 0 (D45): its whole cost is an unrealised loss for now
                continue
            lv = float(pos.liq_value) if pos.liq_value is not None else 0.0
            liq += lv
            mark += float(pos.mark_value) if pos.mark_value is not None else lv
            if pos.fv_value is not None:
                fv_total += float(pos.fv_value)
                fv_any = True
            else:
                fv_total += lv
            cost_open += float(pos.cost)
            if pos.depth_state == "unknown":
                unknown += lv
            elif pos.depth_state == "stale":
                stale += lv
        equity_liq = b.cash + liq - advances
        equity_mark = b.cash + mark - advances
        equity_fv = (b.cash + fv_total - advances) if fv_any else None
        has_pos = any(p.status == "open" for p in b.positions.values())
        out = {
            "positions_liq": liq, "positions_mark": mark, "positions_fv": fv_total if fv_any else None,
            "equity_liq": equity_liq, "equity_mark": equity_mark, "equity_fv": equity_fv,
            "unrealized_pnl_liq": liq - cost_open, "unvalued": unvalued, "unvalued_n": unvalued_n,
            "depth_unknown_share": (unknown / equity_liq if equity_liq > _EPS else None) if has_pos else 0.0,
            "depth_stale_share": (stale / equity_liq if equity_liq > _EPS else None) if has_pos else 0.0,
            "advances": advances,
        }
        b.val = out
        return out

    def _equity_point(self, b: _Book, now: float, force: bool = False) -> None:
        val = self._valuation(b)
        eq = float(val["equity_liq"])
        if eq > b.peak_equity:
            b.peak_equity = eq
        dd_abs = b.peak_equity - eq
        if dd_abs > b.max_drawdown_abs:
            b.max_drawdown_abs = dd_abs
        if b.peak_equity > _EPS and dd_abs / b.peak_equity > b.max_drawdown:
            b.max_drawdown = dd_abs / b.peak_equity
        last = b.equity[-1].ts if b.equity else None
        if force or last is None or now - last >= float(self._config.equity_min_spacing_s) - _EPS:
            point = EquityPoint(portfolio_id=b.pid, ts=float(now), cash=round(b.cash, 6),
                                reserved_cash=round(b.reserved_cash, 6), liq_value=round(eq, 6),
                                mark_value=round(float(val["equity_mark"]), 6),
                                fv_value=_r6(val["equity_fv"]),
                                open_positions=sum(1 for p in b.positions.values() if p.status != "closed"))
            if b.equity and abs(b.equity[-1].ts - now) < _EPS:
                b.equity[-1] = point
            else:
                b.equity.append(point)
            self._new_equity.append(point)

    # ------------------------------------------------------------------ the step
    def _observed(self, obs: MarketObservation, now: float) -> bool:
        """Whether this step actually saw the market: its newest bulk quote is at most max(stale_quote_s, 3 x interval)
        old. A step during an outage (the process runs, every read fails) adds nothing to the covered hours (D48:
        "observed" means observed)."""
        stamps = [_num(q.ts) for q in (obs.quotes or {}).values() if q is not None]
        stamps = [s for s in stamps if s is not None]
        if not stamps:
            return False
        window = max(float(self._config.stale_quote_s), 3.0 * float(self._config.interval_s))
        return float(now) - max(stamps) <= window + _EPS

    def observe_books(self, books: Mapping[str, BookObservation]) -> None:
        """Remember real books (the newest per exchange) for stale-depth marks and read planning."""
        for eid, bk in books.items():
            if _is_real(bk):
                old = self._last_real_books.get(str(eid))
                if old is None or bk.observed_at >= old.observed_at:
                    self._last_real_books[str(eid)] = bk

    def step(self, now: float, obs: MarketObservation, signals: Sequence[Opportunity],
             inputs: Optional[StrategyInputs] = None, *, tape_truncated: Sequence[str] = ()) -> StepReport:
        """Phases (§6.2): 0 coverage clock and gap handling, 1 settlements and frozen outcomes, 2 taker fills,
        3 maker fills, 4 marks, 5 exits, 6 entries (sized per portfolio), 7 event study, 8 equity points,
        9 persistence (and the final snapshot when covered hours first reach target_hours)."""
        t_start = self._clock()
        now = float(now)
        if inputs is not None:
            cap = self._capital_from(inputs)
            if cap[1] not in (DEFAULT_CAPITAL_SOURCE,):
                self._last_capital = cap
            self._rebase_default_capital(now, inputs)
        if self._run is None:
            capital, source = self._capital_from(inputs)
            self.start(now, capital, source)
        report = StepReport(now=now, step=self._steps + 1, signals=len(signals or ()))
        self._step_report = report
        self._last_obs = obs
        cfg = self._config
        signals = list(signals or ())
        # an EMPTY open list means no outcome is open (every position freezes); only a missing one falls back
        open_ids = set(obs.open_ids) if obs.open_ids is not None else set(obs.quotes)
        self.observe_books(obs.books or {})
        for eid in (obs.trades or {}):
            self._tape_read_at[str(eid)] = float(obs.now)
        truncated = {str(e) for e in tape_truncated}
        report.tape_gaps = len(truncated)
        # 0: coverage and downtime
        prev_step = self._coverage.last_step_at
        gap = self._coverage.tick(now, fresh=self._observed(obs, now))
        if gap is not None:
            report.gap_s = round(gap, 3)
        raw_gap = (now - prev_step) if prev_step is not None else 0.0
        if prev_step is not None and raw_gap > max(float(cfg.gap_cancel_s), 3.0 * float(cfg.interval_s)) + _EPS:
            for b in self._books.values():
                for o in b.orders.values():
                    if o.status == "resting":
                        # D48 (binding): counts as cancelled at the last step before the gap, so prints from the
                        # downtime never fill it (a cancel decided while the bot runs takes the portfolio's latency)
                        o.status, o.cancel_at = "cancelling", float(prev_step)
                        b.order_meta.setdefault(o.order_id, {})["cause"] = "cancelled"
                        b.order_meta[o.order_id]["why"] = ("The bot was not running: a resting order cannot be managed "
                                                           "through downtime")
                        self._dirty_orders[o.order_id] = o
        signal_ids = {str(s.idea_id or _fallback_idea_id(s)) for s in signals}
        traded: Set[str] = set()
        bookless: List[Tuple[float, str, List[str]]] = []
        self._deferred_sets = {}
        cup_over = now >= float(obs.cup_end) - _EPS
        has_fv = any(getattr(fv, "usable", False) and getattr(fv, "value", None) is not None
                     for fv in (obs.fair_values or {}).values())
        for spec in cfg.portfolios:
            b = self._books.get(spec.portfolio_id)
            if b is None:
                continue
            b.diagnostics["steps"] = int(b.diagnostics.get("steps", 0)) + 1
            if has_fv:
                b.diagnostics["fv_steps"] = int(b.diagnostics.get("fv_steps", 0)) + 1
            try:
                self._settlements(b, obs, open_ids, now)
                if cup_over:
                    for o in list(b.orders.values()):
                        self._close_order(b, o, "cancelled", "The Cup ended", now)
                        report.orders_cancelled += 1
                self._taker_fills(b, obs, open_ids, now)
                self._maker_fills(b, obs, open_ids, now, signal_ids, truncated)
                self._marks(b, obs, now)
                self._valuation(b)
                self._exits(b, obs, open_ids, now)
                self._entries(b, obs, signals, inputs, open_ids, now, traded, bookless)
            except Exception as exc:  # one portfolio's bug never stops the others
                if _must_propagate(exc):
                    raise
                log.warning("paper: step failed for %s: %s", spec.portfolio_id, exc, exc_info=True)
                report.errors.append(f"{spec.portfolio_id}: {exc}")
        # 7: event study (once per step)
        for b in self._books.values():
            traded.update(o.idea_id for o in b.orders.values())
            traded.update(p.idea_id for p in b.positions.values())
        completed: List[SignalEvent] = []
        try:
            completed = list(self._study.observe(now, obs, signals, traded) or [])
        except Exception as exc:
            if _must_propagate(exc):
                raise
            log.warning("paper: event study failed: %s", exc, exc_info=True)
            report.errors.append(f"event study: {exc}")
        # 8: equity
        for b in self._books.values():
            self._equity_point(b, now)
        bookless.sort()
        uniq: List[Tuple[float, str, List[str]]] = []
        for row in bookless:
            if all(row[1] != u[1] for u in uniq):
                uniq.append(row)
        bookless = uniq
        self._bookless = [legs for _, _, legs in bookless[:3]]
        by_kind: Dict[str, int] = {}
        for s in signals:
            by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
        self._last_signals = {"count": len(signals), "by_kind": by_kind, "at": now,
                              "top_bookless": [i for _, i, _ in bookless[:3]]}
        self._steps += 1
        self._now = now
        # 9: persistence
        if self._final is None and self._coverage.covered_hours >= float(cfg.target_hours) - 1e-9:
            self._final = self._snapshot(now, TARGET_REACHED)
        duration = self._clock() - t_start
        report.duration_s = round(max(0.0, duration), 6)
        self._last_step_seconds = report.duration_s
        self._persist(now, completed)
        return report

    def _persist(self, now: float, events: Sequence[SignalEvent] = ()) -> None:
        if self._run is None:
            return
        rid = self._run["run_id"]
        p = self._persistence
        try:
            if self._dirty_orders:
                p.paper_put_orders(rid, [o.to_dict() for o in self._dirty_orders.values()])
            if self._new_fills:
                p.paper_add_fills(rid, [f.to_dict() for f in self._new_fills])
            if self._new_trades:
                p.paper_add_trades(rid, [t.to_dict() for t in self._new_trades])
            if self._new_equity:
                p.paper_add_equity(rid, [e.to_dict() for e in self._new_equity])
            if events:
                p.paper_put_events(rid, [e.to_dict() for e in events])
            p.paper_save_run(rid, self._run["started_at"], self._config_payload(), self.state(), float(now))
        except Exception as exc:
            if _must_propagate(exc):
                raise
            log.warning("paper: could not persist run %s: %s", rid, exc)
            if hasattr(self, "_step_report"):
                self._step_report.errors.append(f"persistence: {exc}")
        finally:
            self._dirty_orders = {}
            self._new_fills = []
            self._new_trades = []
            self._new_equity = []

    def _config_payload(self) -> Dict[str, Any]:
        return {"fingerprint": self._fingerprint, "code_version": self._code_version, "paper": self._config.to_dict()}

    # ------------------------------------------------------------------ reading
    def _summaries(self, now: float) -> List[PortfolioSummary]:
        cfg = self._config
        head = headline_id(cfg)
        n_port = len(cfg.portfolios)
        out: List[PortfolioSummary] = []
        obs = self._last_obs
        for spec in cfg.portfolios:
            b = self._books.get(spec.portfolio_id)
            if b is None:
                continue
            val = self._valuation(b)
            ideas = self._ideas_of(b)
            untested = self._untested(b)
            verdict = self._verdict(b, val, now, spec.portfolio_id != head, n_port, ideas=ideas, untested=untested)
            # one notion of "closed" everywhere (ui-7): closed IDEAS, as the verdict counts them; legging-residue
            # exits of sets still held are fragments of open ideas, reported separately
            closed_ideas = [i for i in ideas if i.closed and not i.synthetic and not i.frozen]
            wins = sum(1 for i in closed_ideas if i.pnl > _EPS)
            losses = sum(1 for i in closed_ideas if i.pnl < -_EPS)
            legging = self._open_legging_trades(b, ideas)
            exp = self._exposure(b)
            params = cfg.params or StrategyParams()
            swing = abs(exp.national_tilt_d) * float(params.national_swing_sd_pts)
            by_kind = self._by_kind(b, ideas)
            sizing_expl = None
            try:
                ctx = b.last_ctx if b.last_ctx is not None else (self._sizing_ctx(b, obs, None, now) if obs is not None else None)
                if ctx is not None:
                    sizing_expl = self._policy(spec.policy).explain(ctx)
            except Exception as exc:
                log.debug("paper: explain failed for %s: %s", spec.portfolio_id, exc)
            pnl_liq = val["equity_liq"] - b.start_capital
            out.append(PortfolioSummary(
                portfolio_id=b.pid, label=spec.label, policy=spec.policy, kinds=list(spec.kinds) if spec.kinds else None,
                start_capital=round(b.start_capital, 6), cash=round(b.cash, 6), reserved_cash=round(b.reserved_cash, 6),
                positions_liq=round(val["positions_liq"], 6), positions_mark=round(val["positions_mark"], 6),
                positions_fv=_r6(val["positions_fv"]), equity_liq=round(val["equity_liq"], 6),
                equity_mark=round(val["equity_mark"], 6), equity_fv=_r6(val["equity_fv"]),
                realized_pnl=round(b.realized_pnl, 6), unrealized_pnl_liq=round(val["unrealized_pnl_liq"], 6),
                pnl_liq=round(pnl_liq, 6), pnl_liq_pct=round(pnl_liq / b.start_capital, 8) if b.start_capital else 0.0,
                pnl_mark=round(val["equity_mark"] - b.start_capital, 6), max_drawdown=round(b.max_drawdown, 8),
                max_drawdown_abs=round(b.max_drawdown_abs, 6), fills=b.fills, orders_open=len(b.orders),
                positions_open=sum(1 for p in b.positions.values() if p.status != "closed"),
                trades_closed=len(closed_ideas), wins=wins, losses=losses,
                win_rate=_r6(wins / len(closed_ideas)) if closed_ideas else None, by_kind=by_kind,
                exposure=exp, sizing=sizing_expl, verdict=verdict, last_step_at=self._coverage.last_step_at,
                headline=spec.portfolio_id == head, exploratory=spec.portfolio_id != head, latency_s=b.latency,
                unvalued=round(val["unvalued"], 6), depth_unknown_share=_r6(val["depth_unknown_share"]),
                depth_stale_share=_r6(val["depth_stale_share"]), swing_risk=round(swing, 6),
                execution=self._execution(b), no_trade_reason=self._no_trade_reason(b), untested=list(untested),
                legging_trades=len(legging), legging_pnl=round(math.fsum(t.pnl for t in legging), 6),
            ))
        return out

    @staticmethod
    def _idea_key(t: PaperTrade) -> Tuple[str, float]:
        entered = float(t.entered_at if t.entered_at is not None else t.opened_at)
        return (t.idea_id, round(entered, 3))

    def _open_legging_trades(self, b: _Book, ideas: Sequence[IdeaOutcome]) -> List[PaperTrade]:
        """Legging-residue trades that belong to an idea still open (a set still held)."""
        open_keys = {(i.idea_id, round(float(i.entered_at), 3)) for i in ideas if not i.closed}
        return [t for t in b.trades if t.exit_reason == "legging" and self._idea_key(t) in open_keys]

    def _untested(self, b: _Book) -> List[str]:
        """One sentence per kind this portfolio trades that the run could not test (live-3, ui-3, D62): value ideas
        need usable outside fair values; when those were missing on most steps the result covers the other kinds
        only, whether or not the portfolio has fills."""
        kinds = list(b.spec.kinds) if b.spec.kinds is not None else list(TRADE_KINDS)
        if "value" not in kinds:
            return []
        diag = b.diagnostics or {}
        steps = int(diag.get("steps", 0))
        if steps <= 0:
            return []
        share = float(diag.get("fv_steps", 0)) / steps
        if share >= 0.5:
            return []
        others = [k for k in kinds if k != "value"]
        names = (others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1]) if others else ""
        if share <= _EPS:
            covers = (f"so this result covers {names} ideas only" if others
                      else "so this portfolio could not test its only kind")
            # the run cannot tell "off or offline" from "not received yet" (the first refresh of a fresh run)
            return [f"Value ideas were not tested: usable outside fair values on 0% of steps (outside prices off, "
                    f"offline or not received yet), {covers}."]
        covers = (f"so this result mostly covers {names} ideas" if others
                  else "so this portfolio tested its only kind on part of the run")
        return [f"Value ideas were tested on only part of the run: usable outside fair values on {share:.0%} of steps, "
                f"{covers}."]

    def _by_kind(self, b: _Book, ideas: Optional[Sequence[IdeaOutcome]] = None) -> Dict[str, Dict[str, Any]]:
        """Per-kind P&L rows that add up to the portfolio's pnl_liq (trades + open positions + basket legs already
        sold while the basket's last leg is still open); closed counts and wins are over closed ideas, like the
        portfolio's."""
        out: Dict[str, Dict[str, Any]] = {}

        def row(kind: str) -> Dict[str, Any]:
            return out.setdefault(kind, {"pnl_realized": 0.0, "pnl_unrealized_liq": 0.0, "pnl_liq": 0.0, "trades_closed": 0,
                                         "wins": 0, "win_rate": None, "fills": 0, "positions_open": 0})

        for t in b.trades:
            row(t.kind)["pnl_realized"] += t.pnl
        for rec in b.closing.values():  # legs of a basket already closed, waiting for the last leg
            r = row(str(rec.get("kind") or "basket"))
            for lg in rec.get("legs") or []:
                r["pnl_realized"] += float(lg.get("proceeds") or 0.0) - float(lg.get("cost") or 0.0)
        for i in (ideas if ideas is not None else self._ideas_of(b)):
            if i.closed and not i.synthetic and not i.frozen:
                r = row(i.kind)
                r["trades_closed"] += 1
                if i.pnl > _EPS:
                    r["wins"] += 1
        for p in b.positions.values():
            r = row(p.kind)
            r["pnl_realized"] += p.realized_pnl
            if p.status == "open":
                r["pnl_unrealized_liq"] += float(p.liq_value or 0.0) - float(p.cost)
            else:
                r["pnl_unrealized_liq"] -= float(p.cost)
            r["positions_open"] += 1
        counted = {k for k, ex in b.execution.items() if "fill_count" in ex}
        for kind in counted:
            row(kind)["fills"] = int(b.execution[kind]["fill_count"])
        for f in self._recent_fills:  # runs saved before the counter existed
            if f.portfolio_id == b.pid and f.kind not in counted:
                row(f.kind)["fills"] += 1
        for r in out.values():
            r["pnl_liq"] = round(r["pnl_realized"] + r["pnl_unrealized_liq"], 6)
            r["pnl_realized"] = round(r["pnl_realized"], 6)
            r["pnl_unrealized_liq"] = round(r["pnl_unrealized_liq"], 6)
            r["win_rate"] = _r6(r["wins"] / r["trades_closed"]) if r["trades_closed"] else None
        return out

    def _execution(self, b: _Book) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        obs = self._last_obs
        for kind, ex in sorted(b.execution.items()):
            entries = int(ex.get("entries", 0))
            filled = int(ex.get("filled", 0))
            unfilled_now = 0.0
            any_unfilled = False
            for u in b.unfilled:
                if u.get("kind") != kind or u.get("decision_touch") is None or obs is None:
                    continue
                bid = _contract_bid(obs.quotes.get(str(u.get("exchange_id"))), str(u.get("side")))
                if bid is None:
                    continue
                unfilled_now += float(u.get("qty", 0.0)) * (bid - float(u["decision_touch"]))
                any_unfilled = True
            out[kind] = {
                "entries": entries, "filled": filled, "fill_rate": _r6(filled / entries) if entries else None,
                "avg_slippage": _r6(ex["slip_sum"] / ex["slip_n"]) if ex.get("slip_n") else None,
                "unfilled": int(ex.get("unfilled", 0)), "unfilled_pnl_now": _r2(unfilled_now) if any_unfilled else None,
                "exits": int(ex.get("exits", 0)),
                "avg_exit_slippage": _r6(ex["exit_slip_sum"] / ex["exit_slip_n"]) if ex.get("exit_slip_n") else None,
            }
        return out

    def _no_trade_reason(self, b: _Book) -> Optional[str]:
        if b.fills > 0:
            return None
        diag = b.diagnostics or {}
        steps = int(diag.get("steps", 0))
        if steps <= 0:
            return None
        kinds = list(b.spec.kinds) if b.spec.kinds is not None else list(TRADE_KINDS)
        hours = self._coverage.covered_hours
        parts: List[str] = []
        no_setup: List[str] = []
        fv_share = float(diag.get("fv_steps", 0)) / steps
        for kind in kinds:
            k = (diag.get("kinds") or {}).get(kind) or {}
            signals = int(k.get("signals", 0))
            entered = int(k.get("entered", 0))
            if kind == "value" and signals == 0 and fv_share < 0.5:
                parts.append(f"No value trades yet: usable outside fair values on {fv_share:.0%} of steps (see the "
                             "fair-value status).")
                continue
            if signals == 0:
                no_setup.append(kind)
                continue
            if entered == 0:
                blocked = k.get("blocked") or {}
                top = max(sorted(blocked), key=lambda r: blocked[r]) if blocked else None
                why = _BLOCK_TEXT.get(top or "", "they did not pass the entry checks")
                parts.append(f"{signals} {kind} {_plural(signals, 'signal')}, none entered: {why}.")
            else:
                parts.append(f"{entered} {kind} {_plural(entered, 'order')} placed, none filled yet (see the execution "
                             "statistics).")
        if no_setup:
            names = no_setup[0] if len(no_setup) == 1 else ", ".join(no_setup[:-1]) + " or " + no_setup[-1]
            parts.append(f"No {names} signals in {hours:.1f} h: the market offered no such setup (that is not evidence "
                         "of no edge).")
        return " ".join(parts) if parts else None

    def _verdict(self, b: _Book, val: Mapping[str, Any], now: float, exploratory: bool, n_port: int, *,
                 ideas: Optional[List[IdeaOutcome]] = None, untested: Optional[Sequence[str]] = None) -> Verdict:
        params = self._config.params or StrategyParams()
        exp = self._exposure(b)
        eq = float(val["equity_liq"])
        swing_share = abs(exp.national_tilt_d) * float(params.national_swing_sd_pts) / eq if eq > _EPS else None
        return compute_verdict(
            b.pid, ideas if ideas is not None else self._ideas_of(b), pnl_liq=eq - b.start_capital,
            covered_hours=self._coverage.covered_hours,
            wall_hours=self._coverage.wall_hours(now), day_hours=self._coverage.day_hours(),
            max_drawdown=b.max_drawdown, closed_trades=b.trades, exploratory=exploratory, n_portfolios=n_port,
            depth_unknown_share=_num(val.get("depth_unknown_share")), swing_share=swing_share,
            unvalued_positions=int(val.get("unvalued_n", 0)), unvalued_value=float(val.get("unvalued", 0.0)),
            demo=bool(self._config.demo), untested=untested if untested is not None else self._untested(b),
        )

    def _ideas_of(self, b: _Book) -> List[IdeaOutcome]:
        groups: Dict[Tuple[str, float], Dict[str, Any]] = {}
        order: List[Tuple[str, float]] = []

        def acc(key: Tuple[str, float], **kw: Any) -> Dict[str, Any]:
            if key not in groups:
                groups[key] = {"pnl": 0.0, "cost": 0.0, "closed": True, "synthetic": False, "frozen": False,
                               "kind": kw.get("kind"), "race": kw.get("race"), "direction": kw.get("direction", "N"),
                               "entered_at": key[1]}
                order.append(key)
            return groups[key]

        for t in b.trades:
            entered = float(t.entered_at if t.entered_at is not None else t.opened_at)
            g = acc((t.idea_id, round(entered, 3)), kind=t.kind, race=t.race_key, direction=t.direction)
            g["pnl"] += t.pnl
            g["cost"] += t.cost
            g["synthetic"] = g["synthetic"] or t.synthetic
            if t.exit_reason in END_REASONS:
                g["closed"] = False  # booked at liquidation value when the run ended: still an open idea
        for basket_id, rec in b.closing.items():
            entry = b.entries.get(rec.get("entry") or "") or {}
            entered = float(entry.get("entered_at") or 0.0)
            for lg in rec.get("legs") or []:
                key = (str(rec.get("idea_id")), round(entered or float(lg.get("opened_at") or 0.0), 3))
                g = acc(key, kind=rec.get("kind"), race=rec.get("race_key"), direction="N")
                g["pnl"] += float(lg["proceeds"]) - float(lg["cost"])
                g["cost"] += float(lg["cost"])
                g["closed"] = False
                g["synthetic"] = g["synthetic"] or bool(lg.get("synthetic"))
        for pos in sorted(b.positions.values(), key=lambda p: p.exchange_id):
            led = b.ledger.get(pos.position_id) or {}
            entry = b.entries.get(led.get("entry") or "") or {}
            entered = float(pos.entered_at if pos.entered_at is not None else pos.opened_at)
            direction = "N" if pos.kind in SET_KINDS else str(entry.get("direction") or _direction(pos.factor_delta))
            g = acc((pos.idea_id, round(entered, 3)), kind=pos.kind, race=pos.race_key or f"market:{pos.market_id}",
                    direction=direction)
            g["closed"] = False
            g["pnl"] += pos.realized_pnl
            g["pnl"] += (float(pos.liq_value or 0.0) - float(pos.cost)) if pos.status == "open" else -float(pos.cost)
            g["cost"] += float(led.get("bought_cost", pos.cost))
            g["synthetic"] = g["synthetic"] or pos.synthetic
            g["frozen"] = g["frozen"] or pos.status == "frozen"
        out: List[IdeaOutcome] = []
        for key in order:
            g = groups[key]
            entered = float(g["entered_at"])
            out.append(IdeaOutcome(idea_id=key[0], kind=str(g["kind"] or ""), race_key=g["race"], entered_at=entered,
                                   direction=str(g["direction"] or "N"), closed=bool(g["closed"]),
                                   pnl=round(g["pnl"], 6), cost=round(g["cost"], 6), synthetic=bool(g["synthetic"]),
                                   frozen=bool(g["frozen"]), day=_utc_day(entered)))
        return out

    def _ended(self) -> bool:
        return self._run is None and self._ended_run is not None and self._final_summaries is not None

    def portfolios(self, now: Optional[float] = None) -> List[PortfolioSummary]:
        """Every portfolio's summary; once a run has ended, the summaries it ended with (before its open positions
        were turned into trades at liquidation value)."""
        if self._ended():
            return copy.deepcopy(self._final_summaries)  # type: ignore[arg-type]
        return self._summaries(self._now_or(now))

    def _now_or(self, now: Optional[float]) -> float:
        if now is not None:
            return float(now)
        if self._now is not None:
            return self._now
        return float(self._clock())

    def positions(self, portfolio_id: Optional[str] = None) -> List[PaperPosition]:
        out: List[PaperPosition] = []
        for b in self._books.values():
            if portfolio_id is not None and b.pid != portfolio_id:
                continue
            out.extend(sorted(b.positions.values(), key=lambda p: p.exchange_id))
        return out

    def orders(self, portfolio_id: Optional[str] = None, open_only: bool = True) -> List[PaperOrder]:
        out: List[PaperOrder] = []
        for b in self._books.values():
            if portfolio_id is not None and b.pid != portfolio_id:
                continue
            out.extend(b.orders.values())
        if not open_only and self._run is not None:
            try:
                rows = self._persistence.paper_orders(self._run["run_id"])
            except Exception:
                rows = []
            known = {o.order_id for o in out}
            for d in rows:
                if d.get("order_id") in known:
                    continue
                if portfolio_id is not None and d.get("portfolio_id") != portfolio_id:
                    continue
                out.append(_order_from(d))
        out.sort(key=lambda o: (o.created_at, o.order_id))
        return out

    def fills(self, portfolio_id: Optional[str] = None, limit: int = FILLS_MAX) -> List[PaperFill]:
        rows = [f for f in reversed(self._recent_fills) if portfolio_id is None or f.portfolio_id == portfolio_id]
        return rows[:limit] if limit is not None else rows

    def trades(self, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[PaperTrade]:
        rows: List[PaperTrade] = []
        for b in self._books.values():
            if portfolio_id is not None and b.pid != portfolio_id:
                continue
            rows.extend(b.trades)
        rows.sort(key=lambda t: (t.closed_at, t.trade_id), reverse=True)
        return rows[:limit] if limit is not None else rows

    def equity(self, portfolio_id: Optional[str] = None) -> Dict[str, List[EquityPoint]]:
        """In-memory equity points (at most one per equity_min_spacing_s per portfolio), oldest first."""
        return {b.pid: list(b.equity) for b in self._books.values() if portfolio_id is None or b.pid == portfolio_id}

    def ideas(self, portfolio_id: str) -> List[IdeaOutcome]:
        b = self._books.get(portfolio_id)
        return self._ideas_of(b) if b is not None else []

    def verdicts(self, now: Optional[float] = None) -> Dict[str, Verdict]:
        if self._ended():
            return {s.portfolio_id: copy.deepcopy(s.verdict) for s in self._final_summaries or [] if s.verdict is not None}
        now = self._now_or(now)
        head = headline_id(self._config)
        n_port = len(self._config.portfolios)
        out: Dict[str, Verdict] = {}
        for spec in self._config.portfolios:
            b = self._books.get(spec.portfolio_id)
            if b is not None:
                out[b.pid] = self._verdict(b, self._valuation(b), now, b.pid != head, n_port)
        return out

    def events(self, kind: Optional[str] = None) -> List[SignalEvent]:
        out: List[SignalEvent] = []
        try:
            out.extend(self._study.completed())
        except Exception:
            pass
        pending = getattr(self._study, "pending", None)
        if callable(pending):
            try:
                out.extend(pending())
            except Exception:
                pass
        return [e for e in out if kind is None or e.kind == kind]

    def _snapshot(self, now: float, reason: str, summaries: Optional[List[PortfolioSummary]] = None) -> Dict[str, Any]:
        if summaries is None:
            summaries = self._summaries(now)
        try:
            study = self._study.summary()
        except Exception:
            study = None
        return {"at": float(now), "reason": reason,
                "verdicts": {s.portfolio_id: s.verdict.to_dict() for s in summaries if s.verdict is not None},
                "portfolios": [s.to_dict() for s in summaries], "study": study}

    def _settings(self) -> Dict[str, Any]:
        cfg = self._config
        defaults = StrategyParams().to_dict()
        current = (cfg.params or StrategyParams()).to_dict()
        changed = {k: v for k, v in current.items() if defaults.get(k) != v}
        info = self._run or self._ended_run
        return {"sizing": cfg.sizing, "regime": cfg.regime, "all_collateral": bool(cfg.all_collateral),
                "start_capital": info["start_capital"] if info else cfg.start_capital,
                "params_changed": changed}

    def _run_dict(self, now: float) -> Dict[str, Any]:
        """The ``run`` block of the summary: the active run, else the run that just ended (until the next start)."""
        cfg = self._config
        info = self._run or self._ended_run
        if info is None:
            return _empty_run(cfg, self._fingerprint, self._code_version)
        covered = self._coverage.covered_hours
        target = float(cfg.target_hours)
        wall_to = info.get("ended_at") if info.get("ended_at") is not None else now
        return {
            "run_id": info["run_id"], "started_at": info["started_at"],
            "hours_run": round(covered, 6), "wall_hours": round(self._coverage.wall_hours(wall_to), 6),
            "gaps": [dict(g) for g in self._coverage.gaps], "target_hours": target,
            "progress": round(min(1.0, covered / target), 6) if target > 0 else 1.0,
            "complete": covered >= target - 1e-9, "steps": self._steps,
            "last_step_at": self._coverage.last_step_at,
            "last_step_seconds": self._last_step_seconds, "interval": None, "regime": cfg.regime,
            "all_collateral": bool(cfg.all_collateral), "sizing": cfg.sizing,
            "start_capital": info["start_capital"], "capital_source": info["capital_source"],
            "fingerprint": self._fingerprint, "code_version": self._code_version, "settings": self._settings(),
            "ended_at": info.get("ended_at"), "end_reason": info.get("end_reason"), "final": copy.deepcopy(self._final),
        }

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        """The ``/api/paper`` body minus the runner's ``budget`` and the tracker's extras (§10.1). Builds
        everything from memory (no persistence reads)."""
        now = self._now_or(now)
        cfg = self._config
        ended = self._ended()
        if ended:
            summaries = copy.deepcopy(self._final_summaries) or []
        else:
            summaries = self._summaries(now) if (self._run or self._ended_run) else []
        head = headline_id(cfg)
        headline = None
        for s in summaries:
            if s.portfolio_id == head:
                headline = _headline_dict(s)
        live_equity = {s.portfolio_id: float(s.equity_liq) for s in summaries}
        equity: Dict[str, List[List[float]]] = {}
        for b in self._books.values():
            pts = [(p.ts, p.liq_value) for p in b.equity]
            # points are stored at most once per equity_min_spacing_s (D59); the SERVED series ends with the current
            # valuation so the chart, the equity table and the headline P&L agree on the same screen
            cur = live_equity.get(b.pid)
            if not ended and cur is not None and (not pts or now > pts[-1][0] + _EPS):
                pts.append((float(now), round(cur, 6)))
            equity[b.pid] = downsample_equity(pts)
        positions: List[Dict[str, Any]] = []
        baskets: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for b in self._books.values():
            for pos in sorted(b.positions.values(), key=lambda p: p.exchange_id):
                d = pos.to_dict()
                if pos.status == "frozen":
                    # market closed without a ruling (D45): it counts 0 in equity_liq / pnl_liq, so the row shows
                    # what the totals charge (value 0, unrealised -cost); the last mark is listed separately
                    d["last_liq_value"] = _r6(pos.liq_value)
                    d["liq_value"] = 0.0
                    d["unrealized_liq"] = _r6(-float(pos.cost))
                    d["unvalued"] = True
                else:
                    d["unrealized_liq"] = (_r6(float(pos.liq_value) - float(pos.cost)) if pos.liq_value is not None
                                           else None)
                    d["unvalued"] = False
                d["exit_note"] = pos.exit_plan.note if pos.exit_plan is not None else ""
                d["age_hours"] = round(max(0.0, now - pos.opened_at) / 3600.0, 6)
                positions.append(d)
                if pos.basket_id:
                    key = (b.pid, pos.basket_id)
                    bk = baskets.setdefault(key, {"portfolio_id": b.pid, "basket_id": pos.basket_id, "idea_id": pos.idea_id,
                                                  "sets": None, "cost": 0.0, "floor_value": 0.0, "liq_value": 0.0,
                                                  "legs": [], "naked_qty": 0.0})
                    bk["cost"] = round(bk["cost"] + float(pos.cost), 6)
                    bk["liq_value"] = round(bk["liq_value"] + float(d.get("liq_value") or 0.0), 6)
                    bk["legs"].append({"exchange_id": pos.exchange_id, "side": pos.side, "qty": pos.qty,
                                       "liq_value": _r6(d.get("liq_value"))})
            for (pid, basket_id), bk in baskets.items():
                if pid != b.pid:
                    continue
                self._finish_basket_row(b, basket_id, bk)
        orders = sorted([o for b in self._books.values() for o in b.orders.values()],
                        key=lambda o: (o.created_at, o.order_id), reverse=True)
        try:
            study = self._study.summary() if (self._run or self._ended_run) else None
        except Exception:
            study = None
        sig = self._last_signals or {}
        return {
            "now": now, "enabled": True, "available": True, "error": None, "demo": bool(cfg.demo),
            "run": self._run_dict(now),
            "has_previous": bool(self._has_previous), "headline": headline,
            "table_warning": TABLE_WARNING.format(n=len(cfg.portfolios)), "model_label": FV_MODEL_LABEL,
            "portfolios": [s.to_dict() for s in summaries], "equity": equity, "positions": positions[:POSITIONS_MAX],
            "baskets": list(baskets.values()), "orders": [o.to_dict() for o in orders[:ORDERS_MAX]],
            "fills": [f.to_dict() for f in self.fills(limit=FILLS_MAX)],
            "trades": [t.to_dict() for t in self.trades(limit=TRADES_MAX)],
            "signals": {"count": int(sig.get("count", 0)), "by_kind": dict(sig.get("by_kind") or {}), "at": sig.get("at")},
            "study": study, "budget": None, "fair_value": None,
            "caveats": list(CAVEATS) + ([DEMO_CAVEAT] if cfg.demo else []), "last_step": None,
        }

    def _basket_legs_total(self, b: _Book, basket_id: str) -> int:
        """How many legs the basket was entered with (open + already closed)."""
        open_legs = [p for p in b.positions.values() if p.basket_id == basket_id]
        n = len(open_legs) + len((b.closing.get(basket_id) or {}).get("legs") or [])
        for p in open_legs:
            entry = b.entries.get((b.ledger.get(p.position_id) or {}).get("entry") or "") or {}
            n = max(n, len(entry.get("legs") or {}))
        return n

    def _finish_basket_row(self, b: _Book, basket_id: str, bk: Dict[str, Any]) -> None:
        """``sets`` = the matched quantity over EVERY leg of the basket (a leg already sold counts 0); ``floor_value``
        = sets x the set's floor; shares beyond the matched sets are naked (no floor) and listed separately."""
        legs = bk["legs"]
        n_total = self._basket_legs_total(b, basket_id)
        qtys = [float(lg["qty"]) for lg in legs]
        sets = min(qtys) if qtys and len(legs) >= n_total else 0.0
        per_leg = max((float(p.floor_per_unit or 0.0) for p in b.positions.values() if p.basket_id == basket_id),
                      default=0.0)
        bk["sets"] = round(sets, 6)
        bk["floor_value"] = round(sets * per_leg * n_total, 6)
        naked = 0.0
        for lg in legs:
            lg["naked_qty"] = round(max(0.0, float(lg["qty"]) - sets), 6)
            naked += lg["naked_qty"]
        bk["naked_qty"] = round(naked, 6)
        bk["legs_total"] = n_total

    def previous_summary(self) -> Optional[Dict[str, Any]]:
        """The ``/api/paper?run=previous`` body: the newest ENDED run's final snapshot in the §10.1 shape (reads
        the persistence; never touches the current run)."""
        try:
            rec = self._persistence.paper_last_ended_run()
        except Exception as exc:
            log.warning("paper: could not read the previous run: %s", exc)
            return None
        if not rec:
            return None
        state = rec.get("state") or {}
        final = state.get("final") or {}
        cfg_d = ((rec.get("config") or {}).get("paper")) or {}
        try:
            cfg = paper_config_from_dict(cfg_d)
        except Exception:
            cfg = self._config
        cov = CoverageClock.from_dict(state.get("coverage"))
        ended_at = _num(rec.get("ended_at"))
        target = float(state.get("target_hours") or cfg.target_hours)
        portfolios = list(final.get("portfolios") or [])
        head = headline_id(cfg)
        headline = None
        for p in portfolios:
            if p.get("portfolio_id") == head:
                headline = {"portfolio_id": head, "label": p.get("label"), "latency_s": p.get("latency_s"),
                            "pnl_liq": p.get("pnl_liq"), "pnl_liq_pct": p.get("pnl_liq_pct"), "pnl_mark": p.get("pnl_mark"),
                            "equity_liq": p.get("equity_liq"), "unvalued": p.get("unvalued"),
                            "depth_unknown_share": p.get("depth_unknown_share"), "verdict": p.get("verdict"),
                            "verdict_caveats": [CAVEATS[i] for i in VERDICT_CAVEATS],
                            "untested": list(p.get("untested") or [])}
        rid = str(rec.get("run_id"))
        try:
            trades = self._persistence.paper_trades(rid, limit=TRADES_MAX)
            fills = self._persistence.paper_fills(rid, limit=FILLS_MAX)
            eq_rows = self._persistence.paper_equity(rid)
        except Exception:
            trades, fills, eq_rows = [], [], []
        equity: Dict[str, List[Tuple[float, float]]] = {}
        for d in eq_rows:
            equity.setdefault(str(d.get("portfolio_id")), []).append((float(d["ts"]), float(d["liq_value"])))
        covered = cov.covered_hours
        run = {
            "run_id": rid, "started_at": _num(rec.get("started_at")), "hours_run": round(covered, 6),
            "wall_hours": round(cov.wall_hours(ended_at if ended_at is not None else cov.last_step_at or 0.0), 6),
            "gaps": list(cov.gaps), "target_hours": target,
            "progress": round(min(1.0, covered / target), 6) if target > 0 else 1.0, "complete": covered >= target - 1e-9,
            "steps": int(state.get("steps") or 0), "last_step_at": _num(state.get("last_step_at")),
            "last_step_seconds": _num(state.get("last_step_seconds")), "interval": None, "regime": cfg.regime,
            "all_collateral": bool(cfg.all_collateral), "sizing": cfg.sizing,
            "start_capital": _num(state.get("start_capital")), "capital_source": str(state.get("capital_source") or ""),
            "fingerprint": (rec.get("config") or {}).get("fingerprint"),
            "code_version": (rec.get("config") or {}).get("code_version"),
            "settings": {"sizing": cfg.sizing, "regime": cfg.regime, "all_collateral": bool(cfg.all_collateral),
                         "start_capital": _num(state.get("start_capital")), "params_changed": {
                             k: v for k, v in (cfg.params or StrategyParams()).to_dict().items()
                             if StrategyParams().to_dict().get(k) != v}},
            "ended_at": ended_at, "end_reason": final.get("reason"), "final": final or None,
        }
        return {
            "now": ended_at, "enabled": True, "available": True, "error": None, "demo": bool(cfg.demo), "run": run,
            "has_previous": True, "headline": headline, "table_warning": TABLE_WARNING.format(n=len(cfg.portfolios)),
            "model_label": FV_MODEL_LABEL, "portfolios": portfolios,
            "equity": {pid: downsample_equity(pts) for pid, pts in equity.items()}, "positions": [], "baskets": [],
            "orders": [], "fills": fills, "trades": trades, "signals": None, "study": final.get("study"),
            "budget": None, "fair_value": None, "caveats": list(CAVEATS) + ([DEMO_CAVEAT] if cfg.demo else []),
            "last_step": None,
        }

    def state(self) -> Dict[str, Any]:
        """The JSON state persisted after every step (§6.11)."""
        if self._run is None:
            return {"version": STATE_VERSION, "run_id": None}
        now = self._now if self._now is not None else self._run["started_at"]
        held = {p.exchange_id for b in self._books.values() for p in b.positions.values()}
        books = {eid: bk.to_dict() for eid, bk in sorted(self._last_real_books.items())
                 if eid in held and now - bk.observed_at <= self._config.mark_depth_max_age_s + _EPS}
        try:
            study_state = self._study.state()
        except Exception:
            study_state = None
        return {
            "version": STATE_VERSION, "run_id": self._run["run_id"], "started_at": self._run["started_at"],
            "start_capital": self._run["start_capital"], "capital_source": self._run["capital_source"],
            "target_hours": float(self._config.target_hours), "steps": self._steps,
            "last_step_at": self._coverage.last_step_at, "last_step_seconds": self._last_step_seconds,
            "coverage": self._coverage.to_dict(),
            "counters": {pid: dict(b.counters) for pid, b in self._books.items()},
            "portfolios": {pid: b.state() for pid, b in self._books.items()},
            "last_real_books": books, "study": study_state, "final": copy.deepcopy(self._final),
            "last_signals": copy.deepcopy(self._last_signals),
            "tape_read_at": dict(sorted(self._tape_read_at.items())),
            "deferred_sets": {k: list(v) for k, v in sorted(self._deferred_sets.items())},
            "basket_waiting": {k: list(v) for k, v in sorted(self._basket_waiting.items())},
            "bookless": [list(x) for x in self._bookless],
        }


_EXIT_TEXT = {
    "regime": "Flat before the Cup's closeout window (settlement rule)",
    "stop": "The bid fell to the stop",
    "target": "The bid reached the target",
    "fair_value": "The bid reached the fair-value target (the fair value moved)",
    "converged": "The price converged: selling banks the edge",
    "time": "Time stop",
    "replaced": "Replaced by a better idea (chaser top-k)",
    "legging": "Legging out: selling every remaining leg",
}


def _headline_dict(s: PortfolioSummary) -> Dict[str, Any]:
    return {"portfolio_id": s.portfolio_id, "label": s.label, "latency_s": s.latency_s, "pnl_liq": s.pnl_liq,
            "pnl_liq_pct": s.pnl_liq_pct, "pnl_mark": s.pnl_mark, "equity_liq": s.equity_liq, "unvalued": s.unvalued,
            "depth_unknown_share": s.depth_unknown_share, "verdict": s.verdict.to_dict() if s.verdict else None,
            "verdict_caveats": [CAVEATS[i] for i in VERDICT_CAVEATS], "untested": list(s.untested or [])}


def _empty_run(cfg: PaperConfig, fingerprint: str, code_version: str) -> Dict[str, Any]:
    return {"run_id": None, "started_at": None, "hours_run": 0.0, "wall_hours": 0.0, "gaps": [],
            "target_hours": float(cfg.target_hours), "progress": 0.0, "complete": False, "steps": 0,
            "last_step_at": None, "last_step_seconds": None, "interval": None, "regime": cfg.regime,
            "all_collateral": bool(cfg.all_collateral), "sizing": cfg.sizing,
            "start_capital": float(cfg.start_capital) if cfg.start_capital else DEFAULT_START_CAPITAL,
            "capital_source": "", "fingerprint": fingerprint, "code_version": code_version,
            "settings": {"sizing": cfg.sizing, "regime": cfg.regime, "all_collateral": bool(cfg.all_collateral),
                         "start_capital": cfg.start_capital, "params_changed": {}},
            "ended_at": None, "end_reason": None, "final": None}


def _fallback_idea_id(opp: Opportunity) -> str:
    if opp.legs:
        legs = sorted(opp.legs, key=lambda lg: str(lg.get("exchange_id")))
        return (f"{opp.kind}:set:" + "+".join(str(lg.get("exchange_id")) for lg in legs) + ":"
                + "".join(str(lg.get("side", "y"))[:1] for lg in legs))
    return f"{opp.kind}:x{opp.exchange_id}:{opp.side}"


def _opp_exchanges(opp: Opportunity) -> List[str]:
    if opp.legs and (opp.kind in SET_KINDS or opp.unit == "sets"):
        return [str(lg.get("exchange_id")) for lg in opp.legs if lg.get("exchange_id") is not None]
    return [str(opp.exchange_id)] if opp.exchange_id is not None else []


def _must_propagate(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _PROPAGATE_NAMES:
            return True
    return False


# --------------------------------------------------------------------------- runner


InputsFn = Callable[[float], Tuple[StrategyInputs, MarketObservation]]
SignalFn = Callable[..., List[Opportunity]]
FulfilFn = Callable[[ReadPlan, float], Fulfilment]
CandidatesFn = Callable[[StrategyInputs], List[str]]


def _stored_book(book: BookObservation) -> Dict[str, Any]:
    return {"at": book.observed_at, "bids": [[p, q] for p, q in book.bids], "asks": [[p, q] for p, q in book.asks]}


def _default_candidates(inputs: StrategyInputs) -> List[str]:
    from . import strategy

    return list(strategy.hole_candidates(inputs, inputs.params))


def _default_signals(inputs: StrategyInputs, params: Optional[StrategyParams] = None) -> List[Opportunity]:
    from . import strategy

    return list(strategy.generate_signals(inputs, params))


def merge_fulfilment(obs: MarketObservation, inputs: Optional[StrategyInputs], f: Fulfilment,
                     engine: Optional[PaperEngine] = None) -> None:
    """Merge what was read into the observation (a newer book replaces an older one; tape appended, sorted,
    deduplicated by id) and attach real books to the inputs' quotes as stored books."""
    for eid, book in (f.books or {}).items():
        old = obs.books.get(eid)
        if old is not None and engine is not None and _is_real(old):
            engine.observe_books({eid: old})
        if old is None or book.observed_at >= old.observed_at:
            obs.books[eid] = book
        if inputs is not None and _is_real(book):
            point = (inputs.latest or {}).get(eid)
            if point is not None:
                point.book = _stored_book(book)
    for eid, tape in (f.tape or {}).items():
        merged: Dict[str, TradeRecord] = {str(t.trade_id): t for t in obs.trades.get(eid, [])}
        for t in tape.trades:
            merged[str(t.trade_id)] = t
        obs.trades[eid] = sorted(merged.values(), key=lambda t: (t.ts, str(t.trade_id)))


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
    errors: List[str] = []
    inputs.resting_exchanges = engine.resting_exchanges()
    cand_fn = candidates_fn or _default_candidates
    try:
        candidates = list(cand_fn(inputs) or [])
    except Exception as exc:
        if _must_propagate(exc):
            raise
        log.warning("paper: candidate selection failed: %s", exc)
        errors.append(f"candidates: {exc}")
        candidates = []
    plan = engine.wanted_reads(obs.now, obs, candidates)
    f = fulfil(plan, obs.now)
    merge_fulfilment(obs, inputs, f, engine)
    sig_fn = signal_fn or _default_signals
    try:
        signals = list(sig_fn(inputs, inputs.params) or [])
    except Exception as exc:
        if _must_propagate(exc):
            raise
        log.warning("paper: signal generation failed: %s", exc, exc_info=True)
        errors.append(f"signals: {exc}")
        signals = []
    now = decided_at() if decided_at is not None else obs.now
    truncated = [e for e, t in (f.tape or {}).items() if t.truncated]
    report = engine.step(now, obs, signals, inputs, tape_truncated=truncated)
    report.book_reads = f.book_reads
    report.trade_reads = f.trade_reads
    report.reads_skipped = f.skipped
    report.errors = errors + list(report.errors)
    return report


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
                 extras_fn: Optional[Callable[[], Mapping[str, Any]]] = None,
                 candidates_fn: Optional[CandidatesFn] = None) -> None:
        self.engine = engine
        self._reader = reader
        self._inputs_fn = inputs_fn
        self._signal_fn = signal_fn
        self._candidates_fn = candidates_fn
        self._limiter = limiter
        self._clock = clock
        self._interval = interval
        self._extras_fn = extras_fn
        self._lock = threading.RLock()
        self._last_report: Optional[StepReport] = None
        self._published: Dict[str, Any] = {}
        with self._lock:
            self._publish(None)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _room(self) -> int:
        lim = self._limiter
        if lim is None:
            return 1 << 30
        try:
            return int(lim.room())
        except Exception:
            return 0

    def _fulfil(self, plan: ReadPlan, now: float) -> Fulfilment:
        cfg = self.engine.config
        reader = self._reader

        def read_book(r: ReadRequest) -> Optional[BookObservation]:
            try:
                return reader.book(r.exchange_id, int(cfg.book_depth))
            except Exception as exc:
                if _must_propagate(exc):
                    raise
                log.warning("paper: book read failed for %s: %s", r.exchange_id, exc)
                return None

        def read_tape(r: ReadRequest, pages: int) -> Optional[TapeRead]:
            since = r.since if r.since is not None else now - float(cfg.trade_poll_s)
            try:
                return reader.trades(r.exchange_id, since, max_pages=pages)
            except Exception as exc:
                if _must_propagate(exc):
                    raise
                log.warning("paper: tape read failed for %s: %s", r.exchange_id, exc)
                return None

        return fulfil_plan(plan, read_book=read_book, read_tape=read_tape, max_books=int(cfg.max_book_reads_per_step),
                           max_pages=int(cfg.max_trade_reads_per_step), max_tape_pages=int(cfg.max_tape_pages),
                           room=self._room if self._limiter is not None else None)

    def step(self, now: Optional[float] = None) -> StepReport:
        """Never raises for data problems (they go to StepReport.errors); re-raises only shutdown."""
        with self._lock:
            clock0 = float(self._clock())
            t = float(now) if now is not None else clock0

            def decided_at() -> float:
                # the decision comes after the reads (§6.2): the step time plus the read time on the injected
                # clock (0 on a SimClock), so a book read in this step never counts as "after the decision"
                try:
                    return t + max(0.0, float(self._clock()) - clock0)
                except Exception:
                    return t

            try:
                inputs, obs = self._inputs_fn(t)
            except Exception as exc:
                if _must_propagate(exc):
                    raise
                log.warning("paper: could not assemble inputs: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                report = StepReport(now=t, step=self.engine._steps, errors=[f"inputs: {exc}"])
                self._last_report = report
                self._publish(report)
                return report
            try:
                report = run_step(self.engine, inputs, obs, self._fulfil, signal_fn=self._signal_fn,
                                  candidates_fn=self._candidates_fn, decided_at=decided_at)
            except Exception as exc:
                if _must_propagate(exc):
                    raise
                log.warning("paper: step failed: %s", exc, exc_info=True)
                report = StepReport(now=t, step=self.engine._steps, errors=[f"step: {exc}"])
            self._last_report = report
            self._publish(report)
            return report

    def _budget(self, report: Optional[StepReport]) -> Dict[str, Any]:
        cfg = self.engine.config
        lim = self._limiter
        used = limit = room = 0
        if lim is not None:
            try:
                used = int(getattr(lim, "used", 0) or 0)
                limit = int(getattr(lim, "limit", 0) or 0)
                room = int(lim.room())
            except Exception:
                pass
        else:
            limit = int(cfg.reads_per_min)
            room = limit
        return {"reads_used": used, "reads_limit": limit, "reads_room": room,
                "book_reads_last_step": report.book_reads if report else 0,
                "trade_reads_last_step": report.trade_reads if report else 0,
                "reads_skipped_last_step": report.reads_skipped if report else 0,
                "max_book_reads_per_step": int(cfg.max_book_reads_per_step),
                "max_trade_reads_per_step": int(cfg.max_trade_reads_per_step),
                "sets_deferred_last_step": report.sets_deferred if report else 0,
                "tape_gaps_last_step": report.tape_gaps if report else 0}

    def _publish(self, report: Optional[StepReport]) -> None:
        try:
            body = self.engine.summary()
        except Exception as exc:
            if _must_propagate(exc):
                raise
            log.warning("paper: summary failed: %s", exc, exc_info=True)
            body = dict(self._published) if self._published else {"now": float(self._clock()), "enabled": True,
                                                                  "available": False, "error": str(exc)}
        body["budget"] = self._budget(report)
        body["last_step"] = report.to_dict() if report is not None else None
        if body.get("run") is not None and self._interval is not None:
            body["run"]["interval"] = float(self._interval)
        if self._extras_fn is not None:
            try:
                extras = self._extras_fn() or {}
                for k, v in extras.items():
                    body[k] = v
            except Exception as exc:
                if _must_propagate(exc):
                    raise
                log.warning("paper: extras failed: %s", exc)
        self._published = body

    def summary(self) -> Dict[str, Any]:
        """The last published ``/api/paper`` body (§10.1), with ``budget``. Lock-free."""
        return self._published

    def previous(self) -> Optional[Dict[str, Any]]:
        """The final snapshot of the newest ENDED run (``/api/paper?run=previous``), or None."""
        return self.engine.previous_summary()

    def reset(self, now: Optional[float] = None, start_capital: Optional[float] = None,
              target_hours: Optional[float] = None, capital_source: str = "") -> str:
        with self._lock:
            t = float(now) if now is not None else float(self._clock())
            if start_capital is None and not capital_source:
                try:
                    inputs, _ = self._inputs_fn(t)
                    start_capital, capital_source = self.engine._capital_from(inputs)
                except Exception as exc:
                    if _must_propagate(exc):
                        raise
                    start_capital, capital_source = self.engine._capital_from(None)
            elif start_capital is not None and not capital_source:
                capital_source = "set by you"
            rid = self.engine.reset(t, start_capital, capital_source, target_hours)
            self._publish(self._last_report)
            return rid

    def end_run(self, now: Optional[float] = None, reason: str = "completed") -> Optional[Dict[str, Any]]:
        """Under the lock: ``engine.end_run`` and publish. ``paper --hours N`` calls it when done (§7.5)."""
        with self._lock:
            t = float(now) if now is not None else float(self._clock())
            snap = self.engine.end_run(t, reason)
            self._publish(self._last_report)
            return snap

    @property
    def last_report(self) -> Optional[StepReport]:
        return self._last_report
