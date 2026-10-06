"""SQLite persistence for the tracker (ticks, candles, trades, surges, news cache, state) and the
paper trader (docs/PAPER_TRADING.md §7.1).

Thread-safe: one connection guarded by a lock (WAL mode on file databases). Pass ``":memory:"``
for tests. See docs/DESIGN.md. :meth:`TrackerStore.open_read_only` opens a second, read-only
connection on the same file (the dashboard's backtests never touch the live connection).

Layout (schema version 3, tracked with ``PRAGMA user_version``; version 1 and 2 databases migrate in place):

* ``markets`` / ``exchanges``: metadata from the market list (one row per market / outcome).
* ``ticks``: our own bulk-price snapshots, ``PRIMARY KEY (exchange_id, ts)``. Pruned to the
  last 10 days as new ticks arrive.
* ``candles``: backfilled ``price-history`` candles keyed by bucket *start* time.
* ``trades``: the trade tape, idempotent by trade id (per exchange, since the API has two
  id spaces: engine sequences and legacy ids).
* ``surges``: detected moves, with the attribution stored as JSON.
* ``news_cache`` / ``state``: JSON blobs keyed by string.

Added in schema version 2 (replays and the paper trader):

* ``trades.fetched_at``: when a print was fetched (NULL for rows stored before v2; never replayed).
* ``fair_values`` / ``fair_value_refreshes``: recorded outside fair values (on a one-tick change plus a
  10-minute heartbeat) and one row per refresh proving which venues re-confirmed them.
* ``book_snapshots``: order books the paper trader read (depth 20), for replays and audits.
* ``settlements``: how outcomes resolved and when the tracker detected it.
* ``leaderboard_snapshots``: up to 100 leaderboard rows per context refresh (the top-3 bar).
* ``leases``: one tracker per database (a heartbeat lease, D46).
* ``paper_runs`` / ``paper_orders`` / ``paper_fills`` / ``paper_trades`` / ``paper_equity`` /
  ``paper_events``: the paper trader's runs (``paper.PaperPersistence``).

Added in schema version 3 (outside-move alerts, docs/OUTSIDE_MOVES.md §14; ``moves.MovesPersistence``):

* ``outside_quotes``: polled Polymarket / Kalshi (or demo venue) samples, stored on a one-tick change plus a
  300-s heartbeat, kept 3 days (about 35 MB a day live).
* ``move_alerts``: every outside-move alert with its lag outcome (the JSON ``data`` is ``MoveAlert.to_dict()``),
  kept 30 days.

All timestamps are epoch seconds (UTC floats). API ISO strings are converted with
:func:`books.parse_time`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, Union

from .books import parse_time
from .models import (
    SURGE_OPEN,
    Article,
    Attribution,
    BookObservation,
    ExchangeInfo,
    FairValueRecord,
    FairValueRefresh,
    LeaderboardSnapshot,
    MarketInfo,
    PricePoint,
    SettlementInfo,
    Surge,
    TradeFlow,
    TradeRecord,
)

log = logging.getLogger("supermarket_bot")

SCHEMA_VERSION = 3
TICK_RETENTION_S = 10 * 86400.0  # ticks older than this (relative to the newest insert) are pruned
TICK_GAP_S = 900.0  # a pause between ticks longer than this is a tracking gap: candles fill it
PRUNE_EVERY = 500  # tick inserts between prunes
NEWS_RETENTION_S = 7 * 86400.0
PAPER_RUN_RETENTION_S = 30 * 86400.0  # paper runs ended longer ago are deleted with their rows
# docs/OUTSIDE_MOVES.md §14 (schema v3, package "wiring"): outside-move samples and alerts
OUTSIDE_QUOTE_RETENTION_S = 3 * 86400.0  # stored outside samples (on a one-tick change + 300-s heartbeat)
MOVE_ALERT_RETENTION_S = 30 * 86400.0  # outside-move alerts and their lag outcomes
# A lease held by a LIVE process on this machine is refused whatever its heartbeat age (live-1: a cycle stuck in
# client retries is not a dead tracker), up to this age (a hung, suspended or recycled pid cannot lock a store out
# for ever).
LEASE_LIVE_HOLDER_MAX_S = 600.0
BOOK_SNAPSHOT_DEPTH = 20  # levels per side kept in book_snapshots
RESOLUTION_SECONDS: Dict[str, float] = {"1m": 60.0, "5m": 300.0, "1h": 3600.0, "1d": 86400.0, "1w": 604800.0}
_MAX_RESOLUTION_S = max(RESOLUTION_SECONDS.values())
_LENGTH_SQL = "(CASE resolution " + " ".join(f"WHEN '{r}' THEN {s:g}" for r, s in RESOLUTION_SECONDS.items()) + " ELSE 0 END)"

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    status TEXT,
    settlement_date TEXT,
    categories TEXT NOT NULL DEFAULT '[]',
    is_multi INTEGER NOT NULL DEFAULT 0,
    exchange_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS exchanges (
    exchange_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    option TEXT,
    market_title TEXT NOT NULL DEFAULT '',
    settlement_date TEXT,
    initial_price REAL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS exchanges_market ON exchanges (market_id, position);
CREATE TABLE IF NOT EXISTS ticks (
    exchange_id TEXT NOT NULL,
    ts REAL NOT NULL,
    last REAL,
    bid REAL,
    ask REAL,
    PRIMARY KEY (exchange_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS candles (
    exchange_id TEXT NOT NULL,
    resolution TEXT NOT NULL,
    ts REAL NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    vwap REAL,
    volume REAL,
    trade_count INTEGER,
    PRIMARY KEY (exchange_id, resolution, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS candles_exchange_ts ON candles (exchange_id, ts);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    ts REAL NOT NULL,
    price REAL,
    size REAL NOT NULL DEFAULT 0,
    side TEXT,
    PRIMARY KEY (exchange_id, trade_id)
);
CREATE INDEX IF NOT EXISTS trades_exchange_ts ON trades (exchange_id, ts);
CREATE TABLE IF NOT EXISTS surges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    win TEXT NOT NULL,
    window_s REAL NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    start_price REAL NOT NULL,
    end_price REAL NOT NULL,
    change REAL NOT NULL,
    direction TEXT NOT NULL,
    peak_price REAL NOT NULL,
    detected_at REAL NOT NULL,
    zscore REAL,
    status TEXT NOT NULL,
    current_price REAL,
    reverted_fraction REAL,
    attribution TEXT
);
CREATE INDEX IF NOT EXISTS surges_exchange ON surges (exchange_id, direction, status, detected_at);
CREATE INDEX IF NOT EXISTS surges_detected ON surges (detected_at);
CREATE INDEX IF NOT EXISTS surges_status ON surges (status, detected_at);
CREATE TABLE IF NOT EXISTS news_cache (
    key TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    articles TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
ALTER TABLE trades ADD COLUMN fetched_at REAL;
CREATE TABLE IF NOT EXISTS fair_values (
    exchange_id TEXT NOT NULL, ts REAL NOT NULL, value REAL, source TEXT NOT NULL,
    bid REAL, ask REAL, confidence TEXT, usable INTEGER NOT NULL DEFAULT 0, agreement REAL,
    as_of REAL, venues TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '{}', PRIMARY KEY (exchange_id, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS fair_values_ts ON fair_values (ts);
CREATE TABLE IF NOT EXISTS fair_value_refreshes (ts REAL PRIMARY KEY, venues TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS book_snapshots (
    exchange_id TEXT NOT NULL, ts REAL NOT NULL, source TEXT NOT NULL, bids TEXT NOT NULL,
    asks TEXT NOT NULL, sequence INTEGER, PRIMARY KEY (exchange_id, ts, source)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settlements (
    exchange_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, settled_with TEXT, settled_on REAL,
    payout_yes REAL, refund INTEGER NOT NULL DEFAULT 0, detected_at REAL);
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    ts REAL PRIMARY KEY, period TEXT NOT NULL, total INTEGER, my_rank INTEGER,
    initial_balance REAL, entries TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases (
    name TEXT PRIMARY KEY, owner_id TEXT NOT NULL, pid INTEGER, host TEXT,
    acquired_at REAL NOT NULL, heartbeat_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS paper_runs (
    run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, config TEXT NOT NULL, state TEXT NOT NULL,
    updated_at REAL NOT NULL, ended_at REAL);
CREATE TABLE IF NOT EXISTS paper_orders (
    run_id TEXT NOT NULL, order_id TEXT NOT NULL, portfolio_id TEXT NOT NULL, created_at REAL NOT NULL,
    status TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (run_id, order_id));
CREATE TABLE IF NOT EXISTS paper_fills (
    run_id TEXT NOT NULL, fill_id TEXT NOT NULL, portfolio_id TEXT NOT NULL, ts REAL NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (run_id, fill_id));
CREATE INDEX IF NOT EXISTS paper_fills_ts ON paper_fills (run_id, ts);
CREATE TABLE IF NOT EXISTS paper_trades (
    run_id TEXT NOT NULL, trade_id TEXT NOT NULL, portfolio_id TEXT NOT NULL, closed_at REAL NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (run_id, trade_id));
CREATE TABLE IF NOT EXISTS paper_equity (
    run_id TEXT NOT NULL, portfolio_id TEXT NOT NULL, ts REAL NOT NULL, cash REAL NOT NULL,
    reserved_cash REAL NOT NULL, liq_value REAL NOT NULL, mark_value REAL NOT NULL, fv_value REAL,
    open_positions INTEGER NOT NULL, PRIMARY KEY (run_id, portfolio_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS paper_events (
    run_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, t0 REAL NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (run_id, event_id));
"""

# docs/OUTSIDE_MOVES.md §14: outside-move samples and alerts (moves.MovesPersistence)
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS outside_quotes (
    exchange_id TEXT NOT NULL, venue TEXT NOT NULL, ts REAL NOT NULL,
    bid REAL, ask REAL, value REAL, spread REAL, bid_size REAL, ask_size REAL, liquidity REAL,
    flags TEXT NOT NULL DEFAULT '[]', external_id TEXT NOT NULL DEFAULT '',
    match_kind TEXT NOT NULL DEFAULT 'EXACT', match_confidence REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (exchange_id, venue, ts)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS outside_quotes_ts ON outside_quotes (ts);
CREATE TABLE IF NOT EXISTS move_alerts (
    alert_id TEXT PRIMARY KEY, exchange_id TEXT NOT NULL, race_key TEXT,
    detected_at REAL NOT NULL, updated_at REAL NOT NULL, closed_at REAL,
    state TEXT NOT NULL, status TEXT NOT NULL, lag_outcome TEXT NOT NULL, lag_s REAL,
    demo INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS move_alerts_detected ON move_alerts (detected_at);
CREATE INDEX IF NOT EXISTS move_alerts_state ON move_alerts (state, detected_at);
"""

_MIGRATIONS: Dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3}
_OUTSIDE_QUOTE_COLUMNS: Tuple[str, ...] = (
    "exchange_id", "venue", "ts", "bid", "ask", "value", "spread", "bid_size", "ask_size", "liquidity", "flags",
    "external_id", "match_kind", "match_confidence",
)
_PAPER_TABLES = ("paper_orders", "paper_fills", "paper_trades", "paper_equity", "paper_events")

# Surge dataclass field -> column (only ``window`` differs: it is an SQL keyword).
_SURGE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("exchange_id", "exchange_id"),
    ("market_id", "market_id"),
    ("window", "win"),
    ("window_s", "window_s"),
    ("start_ts", "start_ts"),
    ("end_ts", "end_ts"),
    ("start_price", "start_price"),
    ("end_price", "end_price"),
    ("change", "change"),
    ("direction", "direction"),
    ("peak_price", "peak_price"),
    ("detected_at", "detected_at"),
    ("zscore", "zscore"),
    ("status", "status"),
    ("current_price", "current_price"),
    ("reverted_fraction", "reverted_fraction"),
)


# --------------------------------------------------------------------------- helpers


def _num(value: Any) -> Optional[float]:
    """A finite float, or None for missing / non-numeric / bool / NaN values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _epoch(value: Any) -> Optional[float]:
    parsed = parse_time(value)
    return parsed.timestamp() if parsed is not None else None


def fallback_mark_price(last: Optional[float], bid: Optional[float], ask: Optional[float], max_spread: float = 0.10) -> Optional[float]:
    """The DESIGN mark rule, used while ``analytics.mark_price`` is unavailable.

    Tight two-sided book → mid; else the last trade; else the (wide) mid; else None.
    """
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2, 6)
        if ask - bid <= max_spread + 1e-9:
            return mid
        return last if last is not None else mid
    return last


def _mark_function() -> Callable[[Optional[float], Optional[float], Optional[float]], Optional[float]]:
    """``analytics.mark_price`` when it works, else :func:`fallback_mark_price` (imported lazily)."""
    try:
        from . import analytics

        fn = analytics.mark_price
        fn(0.5, 0.49, 0.51)
    except Exception:  # NotImplementedError while analytics is a skeleton, or an import problem
        return fallback_mark_price
    return fn


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"), ensure_ascii=False)


def _known_fields(cls: Any, data: Mapping[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _article_from_dict(data: Mapping[str, Any]) -> Article:
    fields = _known_fields(Article, data)
    fields.setdefault("title", "")
    fields.setdefault("url", "")
    return Article(**fields)


def _flow_from_dict(data: Mapping[str, Any]) -> TradeFlow:
    return TradeFlow(**_known_fields(TradeFlow, data))


def _attribution_from_dict(data: Mapping[str, Any]) -> Attribution:
    fields = _known_fields(Attribution, data)
    fields.setdefault("verdict", "unclear")
    fields.setdefault("confidence", 0.0)
    fields.setdefault("reversion_odds", 0.0)
    fields.setdefault("summary", "")
    fields["reasons"] = [str(r) for r in fields.get("reasons") or []]
    fields["articles"] = [_article_from_dict(a) for a in fields.get("articles") or [] if isinstance(a, Mapping)]
    flow = fields.get("flow")
    fields["flow"] = _flow_from_dict(flow) if isinstance(flow, Mapping) else None
    llm = fields.get("llm")
    fields["llm"] = dict(llm) if isinstance(llm, Mapping) else None
    return Attribution(**fields)


def _load_attribution(raw: Optional[str], surge_id: Any) -> Optional[Attribution]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return _attribution_from_dict(data) if isinstance(data, Mapping) else None
    except (ValueError, TypeError) as exc:
        log.warning("stored attribution for surge %s is unreadable: %s", surge_id, exc)
        return None


def _row_to_surge(row: sqlite3.Row) -> Surge:
    values = {name: row[col] for name, col in _SURGE_COLUMNS}
    return Surge(**values, attribution=_load_attribution(row["attribution"], row["id"]), id=row["id"])


def _surge_values(surge: Surge) -> List[Any]:
    return [getattr(surge, name) for name, _ in _SURGE_COLUMNS]


_BOOK_KEY = "book:"


def _book_levels(levels: Sequence[Any], descending: bool) -> List[List[float]]:
    """``[[price, quantity], ...]`` from pairs, dicts or Level objects; positive sizes, best first."""
    out: List[List[float]] = []
    for level in levels or []:
        if isinstance(level, Mapping):
            price, qty = level.get("price"), level.get("quantity", level.get("qty"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price, qty = level[0], level[1]
        else:
            price, qty = getattr(level, "price", None), getattr(level, "quantity", None)
        p, q = _num(price), _num(qty)
        if p is None or q is None or q <= 0 or not 0.0 <= p <= 1.0:
            continue
        out.append([p, q])
    out.sort(key=lambda lv: lv[0], reverse=descending)
    return out


def _stored_book(raw: Any) -> Optional[Dict[str, Any]]:
    """A ``{"at", "bids", "asks"}`` book from its stored JSON value (None when unusable)."""
    if not isinstance(raw, Mapping) or _num(raw.get("at")) is None:
        return None
    return {
        "at": float(raw["at"]),
        "bids": _book_levels(raw.get("bids") or [], descending=True),
        "asks": _book_levels(raw.get("asks") or [], descending=False),
    }


REFRAME_SHARE = 0.5  # same rule analytics uses to pick the shortest window holding most of a move
EXTEND_MIN = 0.03  # a longer window takes over only when the move went this far past the old peak
SPAN_SHARE = 1.25  # a frame spans at most its window plus the start-point tolerance


def _beyond(up: bool, a: float, b: float) -> bool:
    """``a`` lies further than ``b`` in the move's direction (higher for an up-move)."""
    return a > b + 1e-9 if up else a < b - 1e-9


def _merge_surges(old: Surge, new: Surge) -> Surge:
    """Merge a fresh detection into the stored open surge for the same exchange and direction.

    One *frame* describes the move: ``start_ts``, ``start_price``, ``window``, ``window_s``,
    ``end_ts``, ``end_price`` and ``zscore`` always describe the same move, and ``change`` is
    always ``end_price - start_price``, so the numbers a card prints add up and match the
    attribution that analysed them.

    * The frame stays at the spike. The end (``end_ts`` / ``end_price``) only advances when a
      detection takes the move further (a new high for an up-move) within the frame's own
      window (``start_ts + 1.25 x window_s``): re-detections while the price falls back, or
      the same spike seen again through a longer window, change nothing but the peak and the
      live price. The z-score is re-measured with the change.
    * A detection replaces the frame when it describes the move better: a *shorter* window
      starting later that still holds at least half of the move (the first sighting used
      partial history), the *same* window starting later that holds all of the move up to a
      new high (a move that keeps going), or a *longer* window because the move kept going
      well past the old peak. A longer window that merely re-detects a move which already happened never drags
      the start back by a day. The adopted frame keeps the stored end when that end lies
      further in the move's direction and inside the new window (z re-measured to it).
    * The peak is the extreme of both; ``detected_at`` (the first detection), the attribution
      and the status (the tracker re-evaluates it) are kept; ``current_price`` comes from the
      newest detection and ``reverted_fraction`` is re-measured on the merged frame.
    """
    merged = dataclasses.replace(old)
    up = old.direction != "down"
    later = new.end_ts >= old.end_ts
    furthest_end = new.end_price if _beyond(up, new.end_price, old.end_price) else old.end_price
    adopt = False
    if later:
        move = round(furthest_end - old.start_price, 6)  # the stored frame, to the furthest end seen
        shorter = (
            new.window_s < old.window_s
            and new.start_ts > old.start_ts
            and abs(new.change) + 1e-9 >= REFRAME_SHARE * abs(move)
        )
        # The same window sliding along a move that keeps going: it starts later but still holds
        # all of the move up to the new high, so it describes the move at least as well.
        slides = (
            abs(new.window_s - old.window_s) < 1e-6
            and new.start_ts > old.start_ts
            and _beyond(up, new.end_price, old.end_price)
            and abs(new.change) + 1e-9 >= abs(move)
        )
        beyond = (new.peak_price - old.peak_price) if up else (old.peak_price - new.peak_price)
        extends = (
            new.window_s > old.window_s
            and beyond + 1e-9 >= EXTEND_MIN
            and abs(new.change) + 1e-9 >= abs(move)
        )
        adopt = shorter or slides or extends
    if adopt:
        merged.start_ts, merged.start_price = new.start_ts, new.start_price
        merged.window, merged.window_s, merged.zscore = new.window, new.window_s, new.zscore
        merged.end_ts, merged.end_price = new.end_ts, new.end_price
        keep_old_end = _beyond(up, old.end_price, new.end_price) and old.end_ts >= new.start_ts
        if keep_old_end:
            merged.end_ts, merged.end_price = old.end_ts, old.end_price
            if new.zscore is not None and abs(new.change) > 1e-9:
                merged.zscore = round(new.zscore * (old.end_price - new.start_price) / new.change, 4)
    elif later and _beyond(up, new.end_price, old.end_price) and \
            new.end_ts - old.start_ts <= SPAN_SHARE * old.window_s + 1e-6:
        merged.end_ts, merged.end_price = new.end_ts, new.end_price
        if old.zscore is not None and abs(old.change) > 1e-9:
            # Same frame, same volatility: z scales with the move.
            merged.zscore = round(old.zscore * (new.end_price - old.start_price) / old.change, 4)
    merged.change = round(merged.end_price - merged.start_price, 6)
    if up:
        merged.peak_price = max(old.peak_price, new.peak_price, merged.end_price)
    else:
        merged.peak_price = min(old.peak_price, new.peak_price, merged.end_price)
    if new.current_price is not None and later:
        merged.current_price = new.current_price
    current = _num(merged.current_price)
    den = (merged.peak_price - merged.start_price) if up else (merged.start_price - merged.peak_price)
    if current is not None and den > 1e-9:
        given_back = (merged.peak_price - current) if up else (current - merged.peak_price)
        merged.reverted_fraction = round(max(-1.0, min(2.0, given_back / den)), 6)
    elif new.reverted_fraction is not None and later:
        merged.reverted_fraction = new.reverted_fraction
    merged.detected_at = min(old.detected_at, new.detected_at)
    merged.status = SURGE_OPEN
    return merged


# --------------------------------------------------------------------------- store


def _process_started_at(pid: int) -> Optional[float]:
    """Wall-clock start time of process ``pid`` from Linux ``/proc`` (None elsewhere, or when unreadable)."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            stat = fh.read().decode("ascii", "replace")
        start_ticks = float(stat[stat.rindex(")") + 2:].split()[19])  # field 22 (starttime), after "(comm) "
        btime = None
        with open("/proc/stat", "rb") as fh:
            for line in fh:
                if line.startswith(b"btime "):
                    btime = float(line.split()[1])
                    break
        if btime is None:
            return None
        return btime + start_ticks / float(os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _owner_created_at(owner_id: Any) -> Optional[float]:
    """The wall-clock time a tracker's ``owner_id`` (``host:pid:time:seq``) was made, else None."""
    parts = str(owner_id or "").rsplit(":", 3)
    if len(parts) != 4:
        return None
    try:
        value = float(parts[2])
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def lease_holder_alive(holder: Mapping[str, Any]) -> Optional[bool]:
    """Whether the process holding a lease is still running (live-5): True / False for another process on THIS
    machine (``host`` is this host name), None when it cannot be told (another machine, this very process, no
    pid, or a platform without a safe check -- ``os.kill(pid, 0)`` would terminate the process on Windows).
    A pid that was recycled by a process started after the lease's tracker was created counts as ended."""
    try:
        pid = int(holder.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid() or os.name != "posix":
        return None
    try:
        if str(holder.get("host") or "") != socket.gethostname():
            return None
    except OSError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # it exists (another user's process)
    except OSError:
        return None
    created = _owner_created_at(holder.get("owner_id"))
    started = _process_started_at(pid)
    mine = _process_started_at(os.getpid())
    wall = time.time()
    # the /proc clock is trusted only when it puts this process's own start in the past
    if created is not None and started is not None and mine is not None and mine <= wall + 2.0:
        if started > created + 2.0:
            return False  # the pid now belongs to a process that started after that tracker: it ended
    return True


class TrackerStore:
    def __init__(self, path: Union[str, Path] = ":memory:", clock: Callable[[], float] = time.time, *,
                 _read_only: bool = False) -> None:
        """``clock`` stamps ``trades.fetched_at`` and lease times (the fast demo passes its SimClock)."""
        self._memory = str(path) == ":memory:"
        self.path = str(path) if self._memory else str(Path(path).expanduser())
        self.read_only = bool(_read_only)
        self._clock = clock
        if self.read_only and self._memory:
            raise ValueError("a read-only store needs a database file, not :memory:")
        if not self._memory and not self.read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0  # nesting level of _write() so inner writes join the outer transaction
        self._since_prune = PRUNE_EVERY  # prune on the first insert after opening
        self._pruned_once = False
        self._tick_ids: Set[str] = set()  # exchanges that received ticks through this store object
        self._closed = False
        self.last_takeover: Optional[Dict[str, Any]] = None  # the lease holder acquire_lease last took over
        if self.read_only:
            if not Path(self.path).is_file():
                raise FileNotFoundError(f"{self.path}: no such database")
            target = Path(self.path).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(target, uri=True, check_same_thread=False, timeout=10.0, isolation_level=None)
        else:
            self._conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                timeout=10.0,
                isolation_level=None,  # autocommit; transactions are explicit in _write()
            )
        self._conn.row_factory = sqlite3.Row
        try:
            if self.read_only:
                version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
                if version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"{self.path}: database schema version {version}, but this bot reads version {SCHEMA_VERSION}; "
                        "start the dashboard (or the paper command) once to upgrade it"
                    )
            else:
                if not self._memory:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=NORMAL")
                self._migrate()
        except BaseException:
            self._conn.close()
            raise

    @classmethod
    def open_read_only(cls, path: Union[str, Path]) -> "TrackerStore":
        """A second connection on ``path`` (``file:<path>?mode=ro``) that refuses every write: the dashboard's
        backtests read history through it, never through the live store's connection or lock (D52)."""
        return cls(path, _read_only=True)

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path}: database schema version {version} is newer than this bot supports ({SCHEMA_VERSION})"
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self._conn.executescript("BEGIN;\n" + _MIGRATIONS[target] + f"\nPRAGMA user_version = {target};\nCOMMIT;")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            log.debug("store %s migrated to schema version %d", self.path, target)

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> "TrackerStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Run the block in one transaction (nested calls join the outer one). ``immediate`` takes the
        database write lock at once (leases: no other process may read-then-write in between)."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # metadata -------------------------------------------------------------
    def upsert_markets(self, markets: Sequence[Mapping[str, Any]]) -> None:
        """Store API market dicts (``GET /tournaments/{slug}/markets`` rows) and their exchanges."""
        market_rows: List[Tuple[Any, ...]] = []
        exchange_rows: List[Tuple[Any, ...]] = []
        for market in markets:
            if not isinstance(market, Mapping) or market.get("id") is None:
                continue
            market_id = str(market["id"])
            title = market.get("title") or ""
            settlement = market.get("settlementDate")
            exchanges = [ex for ex in market.get("exchanges") or [] if isinstance(ex, Mapping) and ex.get("id") is not None]
            multi = market.get("isMultiOutcome")
            is_multi = multi if isinstance(multi, bool) else len(exchanges) > 1
            categories = [str(c) for c in market.get("categories") or []]
            exchange_ids = [str(ex["id"]) for ex in exchanges]
            market_rows.append(
                (market_id, title, market.get("status"), settlement, _dumps(categories), int(is_multi), _dumps(exchange_ids))
            )
            for position, ex in enumerate(exchanges):
                exchange_rows.append(
                    (str(ex["id"]), market_id, ex.get("option"), title, settlement, _num(ex.get("initialPrice")), position)
                )
        with self._write() as conn:
            conn.executemany("INSERT OR REPLACE INTO markets VALUES (?, ?, ?, ?, ?, ?, ?)", market_rows)
            conn.executemany("INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)", exchange_rows)

    def markets(self) -> List[MarketInfo]:
        rows = self._query("SELECT * FROM markets ORDER BY rowid")
        return [
            MarketInfo(
                market_id=r["market_id"],
                title=r["title"],
                status=r["status"],
                settlement_date=r["settlement_date"],
                categories=list(json.loads(r["categories"] or "[]")),
                is_multi=bool(r["is_multi"]),
                exchange_ids=list(json.loads(r["exchange_ids"] or "[]")),
            )
            for r in rows
        ]

    @staticmethod
    def _exchange_info(row: sqlite3.Row) -> ExchangeInfo:
        return ExchangeInfo(
            exchange_id=row["exchange_id"],
            market_id=row["market_id"],
            option=row["option"],
            market_title=row["market_title"],
            settlement_date=row["settlement_date"],
            initial_price=row["initial_price"],
        )

    def exchanges(self) -> List[ExchangeInfo]:
        rows = self._query(
            "SELECT e.* FROM exchanges e LEFT JOIN markets m ON m.market_id = e.market_id "
            "ORDER BY m.rowid, e.position, e.exchange_id"
        )
        return [self._exchange_info(r) for r in rows]

    def exchange(self, exchange_id: str) -> Optional[ExchangeInfo]:
        rows = self._query("SELECT * FROM exchanges WHERE exchange_id = ?", (str(exchange_id),))
        return self._exchange_info(rows[0]) if rows else None

    # prices -----------------------------------------------------------------
    def add_ticks(self, ts: float, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert snapshot rows (``bot.build_rows`` shape: exchange_id, latest_price, best_bid, best_ask).

        Rows without any price are skipped; a second snapshot at the same ``ts`` replaces the
        first. Returns the number of ticks written. Every ~``PRUNE_EVERY`` inserts, ticks older
        than ``ts - 10 days`` are deleted.
        """
        stamp = float(ts)
        records: List[Tuple[Any, ...]] = []
        for row in rows:
            eid = row.get("exchange_id")
            if eid is None:
                continue
            last = _num(row.get("latest_price", row.get("last")))
            bid = _num(row.get("best_bid", row.get("bid")))
            ask = _num(row.get("best_ask", row.get("ask")))
            if last is None and bid is None and ask is None:
                continue
            records.append((str(eid), stamp, last, bid, ask))
        if not records:
            return 0
        with self._write() as conn:
            conn.executemany("INSERT OR REPLACE INTO ticks VALUES (?, ?, ?, ?, ?)", records)
            self._tick_ids.update(r[0] for r in records)
            self._since_prune += len(records)
            if self._since_prune >= PRUNE_EVERY:
                self._since_prune = 0
                self._prune_ticks(conn, stamp - TICK_RETENTION_S)
        return len(records)

    def _prune_ticks(self, conn: sqlite3.Connection, cutoff: float) -> None:
        """Ticks older than ``cutoff``; with them the replay history (fair values, refresh rows and book
        snapshots, same retention) and paper runs that ended more than 30 days before the newest tick."""
        if not self._pruned_once:
            # The first prune after opening scans the whole table once (catches every old id).
            conn.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM book_snapshots WHERE ts < ?", (cutoff,))
            self._pruned_once = True
        else:
            # Afterwards, per-exchange range deletes on the primary key: no extra ts index needed.
            ids = {r[0] for r in conn.execute("SELECT exchange_id FROM exchanges")} | self._tick_ids
            conn.executemany("DELETE FROM ticks WHERE exchange_id = ? AND ts < ?", [(eid, cutoff) for eid in ids])
            conn.executemany("DELETE FROM book_snapshots WHERE exchange_id = ? AND ts < ?", [(eid, cutoff) for eid in ids])
        conn.execute("DELETE FROM fair_values WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM fair_value_refreshes WHERE ts < ?", (cutoff,))
        newest = cutoff + TICK_RETENTION_S
        self._prune_paper_runs(conn, newest - PAPER_RUN_RETENTION_S)
        # outside-move samples (3 days) and alerts (30 days), relative to the newest tick like the rest (§14)
        conn.execute("DELETE FROM outside_quotes WHERE ts < ?", (newest - OUTSIDE_QUOTE_RETENTION_S,))
        conn.execute("DELETE FROM move_alerts WHERE detected_at < ?", (newest - MOVE_ALERT_RETENTION_S,))

    @staticmethod
    def _prune_paper_runs(conn: sqlite3.Connection, ended_before: float) -> None:
        old = [r[0] for r in conn.execute(
            "SELECT run_id FROM paper_runs WHERE ended_at IS NOT NULL AND ended_at < ?", (ended_before,)
        )]
        for run_id in old:
            for table in _PAPER_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM paper_runs WHERE run_id = ?", (run_id,))

    def prune(self, now: Optional[float] = None) -> None:
        """Run the retention rules at once (``now`` defaults to the newest tick, else the store clock)."""
        if now is None:
            bounds = self.tick_bounds()
            now = bounds[1] if bounds is not None else float(self._clock())
        with self._write() as conn:
            self._prune_ticks(conn, float(now) - TICK_RETENTION_S)

    def add_candles(self, exchange_id: str, resolution: str, candles: Sequence[Mapping[str, Any]]) -> int:
        """Insert ``price-history`` candles (``time`` ISO, ``close`` …). Idempotent.

        A candle already stored for the same bucket is replaced (the newest bucket is still
        forming when it is read). Returns the number of candles written.
        """
        eid = str(exchange_id)
        records: List[Tuple[Any, ...]] = []
        for candle in candles:
            if not isinstance(candle, Mapping):
                continue
            start = _epoch(candle.get("time"))
            if start is None:
                continue
            count = candle.get("tradeCount")
            records.append(
                (
                    eid,
                    str(resolution),
                    start,
                    _num(candle.get("open")),
                    _num(candle.get("high")),
                    _num(candle.get("low")),
                    _num(candle.get("close")),
                    _num(candle.get("vwap")),
                    _num(candle.get("volume")),
                    int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else None,
                )
            )
        if not records:
            return 0
        with self._write() as conn:
            conn.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
        return len(records)

    def has_candles(self, exchange_id: str, resolution: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM candles WHERE exchange_id = ? AND resolution = ? LIMIT 1", (str(exchange_id), str(resolution))
        )
        return bool(rows)

    def series(self, exchange_id: str, since: float, until: Optional[float] = None, *, include_candles: bool = True) -> List[PricePoint]:
        """Merged, time-ordered points: ticks, with candle closes (``source="candle"``, at
        candle end time) wherever there are no ticks. Marks via ``analytics.mark_price``.

        Candles fill the period before the first tick inside ``[since, until]`` (a window that
        starts before tracking did) and every pause of more than ``TICK_GAP_S`` between two
        ticks (the dashboard was stopped, or snapshots failed, while price history covers the
        gap). Where 5m and 1h candles overlap, the finer 5m candles win (generally: a finer
        resolution hides coarser closes inside its span). Candles with a null close are
        skipped. ``include_candles=False`` returns ticks only.
        """
        eid = str(exchange_id)
        mark = _mark_function()
        upper = float("inf") if until is None else float(until)
        with self._lock:
            if until is None:
                tick_rows = self._conn.execute(
                    "SELECT ts, last, bid, ask FROM ticks WHERE exchange_id = ? AND ts >= ? ORDER BY ts", (eid, since)
                ).fetchall()
            else:
                tick_rows = self._conn.execute(
                    "SELECT ts, last, bid, ask FROM ticks WHERE exchange_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                    (eid, since, until),
                ).fetchall()
            boundary = tick_rows[0]["ts"] if tick_rows else float("inf")
            # Open intervals without ticks that candles may fill (besides the lead-in before the first tick).
            gaps: List[Tuple[float, float]] = []
            if tick_rows:
                prev = tick_rows[0]["ts"]
                for row in tick_rows[1:]:
                    if row["ts"] - prev > TICK_GAP_S:
                        gaps.append((prev, row["ts"]))
                    prev = row["ts"]
            candle_rows: List[sqlite3.Row] = []
            if include_candles and (boundary > since or gaps):
                # close time = start + length: bound it in SQL so only usable rows come back
                params: List[Any] = [eid, since - _MAX_RESOLUTION_S, since]
                sql = (
                    "SELECT resolution, ts, close FROM candles WHERE exchange_id = ? AND ts >= ? "
                    f"AND ts + {_LENGTH_SQL} >= ?"
                )
                if math.isfinite(upper):
                    sql += " AND ts <= ?"
                    params.append(upper)
                candle_rows = self._conn.execute(sql + " ORDER BY ts", params).fetchall()

        def fills(close_ts: float) -> bool:
            if close_ts < boundary:
                return True
            return any(lo < close_ts < hi for lo, hi in gaps)

        by_res: Dict[str, List[Tuple[float, Optional[float]]]] = {}
        for row in candle_rows:
            length = RESOLUTION_SECONDS.get(row["resolution"])
            if length is None:
                continue
            close_ts = row["ts"] + length
            if close_ts < since or close_ts > upper or not fills(close_ts):
                continue
            by_res.setdefault(row["resolution"], []).append((close_ts, row["close"]))

        candle_points: List[PricePoint] = []
        covered: List[Tuple[float, float]] = []  # (first bucket start, last close) of finer resolutions
        for res in sorted(by_res, key=lambda r: RESOLUTION_SECONDS[r]):
            entries = by_res[res]
            for close_ts, close in entries:
                if close is None or any(lo < close_ts <= hi for lo, hi in covered):
                    continue
                candle_points.append(PricePoint(ts=close_ts, price=mark(close, None, None), last=close, source="candle"))
            covered.append((entries[0][0] - RESOLUTION_SECONDS[res], entries[-1][0]))
        ticks = [
            PricePoint(ts=r["ts"], price=mark(r["last"], r["bid"], r["ask"]), last=r["last"], bid=r["bid"], ask=r["ask"], source="tick")
            for r in tick_rows
        ]
        if not candle_points:
            return ticks
        merged = candle_points + ticks
        merged.sort(key=lambda p: p.ts)  # stable: a candle closing at a tick's time stays first
        return merged

    def latest(self, exchange_id: str) -> Optional[PricePoint]:
        """The newest tick, else the candle with the latest close time (None when nothing is stored).

        ``book`` carries the newest stored order-book read (see :meth:`put_book`), if any.
        """
        point = self._latest_point(exchange_id)
        if point is not None:
            point.book = self.book(exchange_id)
        return point

    def _latest_point(self, exchange_id: str) -> Optional[PricePoint]:
        eid = str(exchange_id)
        mark = _mark_function()
        with self._lock:
            tick = self._conn.execute(
                "SELECT ts, last, bid, ask FROM ticks WHERE exchange_id = ? ORDER BY ts DESC LIMIT 1", (eid,)
            ).fetchone()
            if tick is not None:
                return PricePoint(
                    ts=tick["ts"], price=mark(tick["last"], tick["bid"], tick["ask"]),
                    last=tick["last"], bid=tick["bid"], ask=tick["ask"], source="tick",
                )
            candidates = []
            for res, length in RESOLUTION_SECONDS.items():
                row = self._conn.execute(
                    "SELECT ts, close FROM candles WHERE exchange_id = ? AND resolution = ? AND close IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1",
                    (eid, res),
                ).fetchone()
                if row is not None:
                    candidates.append((row["ts"] + length, -length, row["close"]))
        if not candidates:
            return None
        close_ts, _, close = max(candidates)
        return PricePoint(ts=close_ts, price=mark(close, None, None), last=close, source="candle")

    # order books ---------------------------------------------------------------
    def put_book(self, exchange_id: str, at: float, bids: Sequence[Any], asks: Sequence[Any]) -> None:
        """Keep the newest order-book read of an exchange (YES prices; replaces the previous one).

        ``bids`` / ``asks`` are levels as ``(price, quantity)`` pairs, ``{"price", "quantity"}``
        dicts or objects with those attributes; they are stored best first. The strategy sizes
        ideas by this depth (see :meth:`latest`).
        """
        record = {
            "at": float(at),
            "bids": _book_levels(bids, descending=True),
            "asks": _book_levels(asks, descending=False),
        }
        self.set_state(_BOOK_KEY + str(exchange_id), record)

    def book(self, exchange_id: str) -> Optional[Dict[str, Any]]:
        """The stored ``{"at", "bids", "asks"}`` order book of an exchange, or None."""
        return _stored_book(self.get_state(_BOOK_KEY + str(exchange_id)))

    def books(self, exchange_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        """The stored books of many exchanges in ONE query (``{exchange_id: book}`` as :meth:`book` returns
        them; exchanges without a book are absent). ``None`` = every stored book. The paper step and the
        Strategy view need every open outcome's book each step: one query instead of one per outcome keeps
        the step from queueing behind the dashboard's readers for the store lock and the GIL."""
        if exchange_ids is not None:
            wanted = {str(e) for e in exchange_ids}
            if not wanted:
                return {}
        rows = self._query("SELECT key, value FROM state WHERE key >= ? AND key < ?", (_BOOK_KEY, _BOOK_KEY[:-1] + ";"))
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            eid = str(row["key"])[len(_BOOK_KEY):]
            if exchange_ids is not None and eid not in wanted:
                continue
            try:
                raw = json.loads(row["value"])
            except ValueError:
                log.warning("state %r is unreadable; ignoring that book", row["key"])
                continue
            book = _stored_book(raw)
            if book is not None:
                out[eid] = book
        return out

    def tick_count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM ticks")[0][0])

    # trades -----------------------------------------------------------------
    def add_trades(self, exchange_id: str, trades: Sequence[Mapping[str, Any]], fetched_at: Optional[float] = None) -> int:
        """Insert trade-tape dicts (``id, createdAt, price, size, side``). Idempotent by id.

        ``fetched_at`` (default: the store clock) records when the prints were read: a replay may use a
        print at decision time t only if it was fetched by t. A print already stored keeps its first
        ``fetched_at`` (rows stored before schema v2 get this one). Returns the number of trades that were new.
        """
        eid = str(exchange_id)
        stamp = float(self._clock()) if fetched_at is None else float(fetched_at)
        records: List[Tuple[Any, ...]] = []
        for trade in trades:
            if not isinstance(trade, Mapping) or trade.get("id") is None:
                continue
            ts = _epoch(trade.get("createdAt"))
            if ts is None:
                continue
            side = trade.get("side")
            records.append(
                (str(trade["id"]), eid, ts, _num(trade.get("price")), _num(trade.get("size")) or 0.0,
                 side.upper() if isinstance(side, str) else None, stamp)
            )
        if not records:
            return 0
        with self._write() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO trades (trade_id, exchange_id, ts, price, size, side, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                records,
            )
            new = conn.total_changes - before
            conn.executemany(
                "UPDATE trades SET fetched_at = ? WHERE exchange_id = ? AND trade_id = ? AND fetched_at IS NULL",
                [(stamp, eid, r[0]) for r in records],
            )
            return new

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        """Stored trades in ``[since, until]``, oldest first (with ``fetched_at``; None for pre-v2 rows)."""
        sql = "SELECT * FROM trades WHERE exchange_id = ? AND ts >= ?"
        params: List[Any] = [str(exchange_id), since]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        rows = self._query(sql + " ORDER BY ts, length(trade_id), trade_id", params)
        return [
            TradeRecord(trade_id=r["trade_id"], exchange_id=r["exchange_id"], ts=r["ts"], price=r["price"], size=r["size"],
                        side=r["side"], fetched_at=r["fetched_at"])
            for r in rows
        ]

    # surges -----------------------------------------------------------------
    def record_surge(self, surge: Surge, merge_window_s: float = 6 * 3600, *, into: Optional[int] = None) -> Surge:
        """Insert, or merge into the open surge for the same exchange+direction detected within
        ``merge_window_s`` (see :func:`_merge_surges`: one consistent frame anchored at the
        spike, ``change == end_price - start_price``). Returns the stored surge with id.

        "Detected within" counts from the stored surge's first detection or its frame's end,
        whichever is newer. ``into`` names the open surge to merge into whatever its age (the
        tracker passes it when a detection re-sees that surge's move, e.g. a plateau seen
        through the 24h window hours later); when it is not an open surge of the same exchange
        and direction the usual rule applies. The stored attribution is kept; the caller's
        ``surge`` object is not modified.
        """
        cutoff = surge.detected_at - merge_window_s
        with self._write() as conn:
            row = None
            if into is not None:
                row = conn.execute(
                    "SELECT * FROM surges WHERE id = ? AND exchange_id = ? AND direction = ? AND status = ?",
                    (int(into), surge.exchange_id, surge.direction, SURGE_OPEN),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM surges WHERE exchange_id = ? AND direction = ? AND status = ? "
                    "AND (detected_at >= ? OR end_ts >= ?) ORDER BY detected_at DESC, id DESC LIMIT 1",
                    (surge.exchange_id, surge.direction, SURGE_OPEN, cutoff, cutoff),
                ).fetchone()
            if row is None:
                stored = dataclasses.replace(surge, id=None)
                cols = ", ".join(col for _, col in _SURGE_COLUMNS) + ", attribution"
                marks = ", ".join("?" for _ in range(len(_SURGE_COLUMNS) + 1))
                attribution = _dumps(surge.attribution) if surge.attribution is not None else None
                cur = conn.execute(f"INSERT INTO surges ({cols}) VALUES ({marks})", _surge_values(stored) + [attribution])
                stored.id = int(cur.lastrowid)
                return stored
            merged = _merge_surges(_row_to_surge(row), surge)
            self._update_surge_row(conn, merged, with_attribution=False)
            return merged

    @staticmethod
    def _update_surge_row(conn: sqlite3.Connection, surge: Surge, with_attribution: bool) -> None:
        sets = ", ".join(f"{col} = ?" for _, col in _SURGE_COLUMNS)
        values = _surge_values(surge)
        if with_attribution:
            sets += ", attribution = ?"
            values.append(_dumps(surge.attribution))
        conn.execute(f"UPDATE surges SET {sets} WHERE id = ?", values + [surge.id])

    def update_surge(self, surge: Surge) -> None:
        """Write every field of a stored surge (by ``id``).

        The attribution is only written when ``surge.attribution`` is set, so a status update
        from a copy read before the analysis finished never erases the analysis result.
        """
        if surge.id is None:
            raise ValueError("surge has no id; use record_surge() first")
        with self._write() as conn:
            self._update_surge_row(conn, surge, with_attribution=surge.attribution is not None)

    def set_attribution(self, surge_id: int, attribution: Attribution) -> None:
        with self._write() as conn:
            conn.execute("UPDATE surges SET attribution = ? WHERE id = ?", (_dumps(attribution), int(surge_id)))

    def get_surge(self, surge_id: int) -> Optional[Surge]:
        rows = self._query("SELECT * FROM surges WHERE id = ?", (int(surge_id),))
        return _row_to_surge(rows[0]) if rows else None

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None,
               limit: int = 200, until: Optional[float] = None) -> List[Surge]:
        """Newest first (by detected_at).

        ``since`` keeps surges detected at or after it, or still extending (``end_ts``) at or
        after it; ``until`` keeps surges detected at or before it (replays, §6.12.2).
        """
        clauses: List[str] = []
        params: List[Any] = []
        if since is not None:
            clauses.append("(detected_at >= ? OR end_ts >= ?)")
            params += [since, since]
        if until is not None:
            clauses.append("detected_at <= ?")
            params.append(until)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if exchange_id is not None:
            clauses.append("exchange_id = ?")
            params.append(str(exchange_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._query(f"SELECT * FROM surges{where} ORDER BY detected_at DESC, id DESC LIMIT ?", params + [int(limit)])
        return [_row_to_surge(r) for r in rows]

    # news cache / state -------------------------------------------------------
    def cached_news(self, key: str, max_age_s: float, now: float) -> Optional[List[Article]]:
        """Articles stored under ``key`` no older than ``max_age_s`` at ``now``, else None."""
        rows = self._query("SELECT fetched_at, articles FROM news_cache WHERE key = ?", (key,))
        if not rows or now - rows[0]["fetched_at"] > max_age_s:
            return None
        try:
            data = json.loads(rows[0]["articles"])
        except ValueError:
            return None
        return [_article_from_dict(a) for a in data if isinstance(a, Mapping)] if isinstance(data, list) else None

    def put_news(self, key: str, articles: Sequence[Article], now: float) -> None:
        payload = _dumps([a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in articles])
        with self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO news_cache VALUES (?, ?, ?)", (key, float(now), payload))
            conn.execute("DELETE FROM news_cache WHERE fetched_at < ?", (float(now) - NEWS_RETENTION_S,))

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM state WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            log.warning("state %r is unreadable; using the default", key)
            return default

    def set_state(self, key: str, value: Any) -> None:
        """Store any JSON-serialisable value (dataclasses are stored via ``to_dict``)."""
        payload = _dumps(value)
        with self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, payload))

    # history for replays (schema v2) -------------------------------------------
    def candles(self, exchange_id: str, resolution: str, since: float, until: Optional[float] = None) -> List[Dict[str, Any]]:
        """Stored candles whose bucket START lies in ``[since, until]``, oldest first: ``{"ts" (bucket start),
        "close_ts" (start + length), "open", "high", "low", "close", "vwap", "volume", "trade_count"}``."""
        length = RESOLUTION_SECONDS.get(str(resolution), 0.0)
        sql = "SELECT * FROM candles WHERE exchange_id = ? AND resolution = ? AND ts >= ?"
        params: List[Any] = [str(exchange_id), str(resolution), float(since)]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(float(until))
        rows = self._query(sql + " ORDER BY ts", params)
        return [
            {"ts": r["ts"], "close_ts": r["ts"] + length, "open": r["open"], "high": r["high"], "low": r["low"],
             "close": r["close"], "vwap": r["vwap"], "volume": r["volume"], "trade_count": r["trade_count"]}
            for r in rows
        ]

    def tick_bounds(self) -> Optional[Tuple[float, float]]:
        """(oldest, newest) tick time, or None without ticks."""
        row = self._query("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM ticks")[0]
        if row["lo"] is None or row["hi"] is None:
            return None
        return float(row["lo"]), float(row["hi"])

    # fair values ---------------------------------------------------------------
    def add_fair_values(self, records: Sequence[Any]) -> int:
        """Upsert :class:`FairValueRecord`s (or their dicts) by (exchange_id, ts). Returns the rows written."""
        rows: List[Tuple[Any, ...]] = []
        for raw in records or ():
            rec = raw if isinstance(raw, FairValueRecord) else _record_from_mapping(raw)
            if rec is None or rec.exchange_id is None or _num(rec.ts) is None:
                continue
            rows.append((
                str(rec.exchange_id), float(rec.ts), _num(rec.value), str(rec.source or ""), _num(rec.bid), _num(rec.ask),
                rec.confidence, int(bool(rec.usable)), _num(rec.agreement), _num(rec.as_of),
                _dumps(list(rec.venues or [])), _dumps(dict(rec.detail or {})),
            ))
        if not rows:
            return 0
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO fair_values (exchange_id, ts, value, source, bid, ask, confidence, usable, "
                "agreement, as_of, venues, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def fair_value_history(self, since: float, until: Optional[float] = None,
                           exchange_ids: Optional[Sequence[str]] = None) -> List[FairValueRecord]:
        """Recorded fair values with ``since <= ts <= until``, oldest first (ties by exchange id)."""
        base = "SELECT * FROM fair_values WHERE ts >= ?"
        params: List[Any] = [float(since)]
        if until is not None:
            base += " AND ts <= ?"
            params.append(float(until))
        rows: List[sqlite3.Row] = []
        if exchange_ids is None:
            rows = self._query(base + " ORDER BY ts, exchange_id", params)
        else:
            ids = sorted({str(e) for e in exchange_ids})
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ", ".join("?" for _ in chunk)
                rows += self._query(base + f" AND exchange_id IN ({marks})", params + chunk)
            rows.sort(key=lambda r: (r["ts"], r["exchange_id"]))
        return [
            FairValueRecord(
                ts=r["ts"], exchange_id=r["exchange_id"], value=r["value"], source=r["source"], bid=r["bid"], ask=r["ask"],
                confidence=r["confidence"] or "low", usable=bool(r["usable"]), agreement=r["agreement"],
                detail=_load_json(r["detail"], {}), as_of=r["as_of"], venues=list(_load_json(r["venues"], [])),
            )
            for r in rows
        ]

    def fair_value_source_stats(self, source: str) -> Dict[str, Any]:
        """``{"records", "first", "last"}`` of the recorded fair values with this ``source`` (e.g. "history")."""
        row = self._query("SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM fair_values WHERE source = ?",
                          (str(source),))[0]
        return {"records": int(row["n"] or 0), "first": row["lo"], "last": row["hi"]}

    def add_fair_value_refresh(self, refresh: Any) -> None:
        """Store one :class:`FairValueRefresh` (one row per refresh, keyed by its time)."""
        if isinstance(refresh, Mapping):
            ts, venues = refresh.get("ts"), refresh.get("venues")
        else:
            ts, venues = getattr(refresh, "ts", None), getattr(refresh, "venues", None)
        if _num(ts) is None:
            return
        with self._write() as conn:
            conn.execute("INSERT OR REPLACE INTO fair_value_refreshes (ts, venues) VALUES (?, ?)",
                         (float(ts), _dumps(dict(venues or {}))))

    def fair_value_refreshes(self, since: float, until: Optional[float] = None) -> List[FairValueRefresh]:
        sql = "SELECT * FROM fair_value_refreshes WHERE ts >= ?"
        params: List[Any] = [float(since)]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(float(until))
        rows = self._query(sql + " ORDER BY ts", params)
        return [FairValueRefresh(ts=r["ts"], venues=dict(_load_json(r["venues"], {}))) for r in rows]

    # outside moves (docs/OUTSIDE_MOVES.md §14; moves.MovesPersistence) -----------
    def add_outside_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert ``moves.OutsideSample.to_dict()`` rows by (exchange_id, venue, ts); returns the rows written.
        Rows without an exchange id, a venue or a finite ``ts`` are skipped."""
        records: List[Tuple[Any, ...]] = []
        for raw in rows or ():
            if not isinstance(raw, Mapping):
                continue
            eid, venue, ts = raw.get("exchange_id"), raw.get("venue"), _num(raw.get("ts"))
            if eid is None or venue is None or ts is None:
                continue
            flags = raw.get("flags")
            records.append((
                str(eid), str(venue), ts, _num(raw.get("bid")), _num(raw.get("ask")), _num(raw.get("value")),
                _num(raw.get("spread")), _num(raw.get("bid_size")), _num(raw.get("ask_size")), _num(raw.get("liquidity")),
                _dumps([str(f) for f in flags] if isinstance(flags, (list, tuple)) else []),
                str(raw.get("external_id") or ""), str(raw.get("match_kind") or "EXACT"),
                _num(raw.get("match_confidence")) or 0.0,
            ))
        if not records:
            return 0
        marks = ", ".join("?" for _ in _OUTSIDE_QUOTE_COLUMNS)
        with self._write() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO outside_quotes ({', '.join(_OUTSIDE_QUOTE_COLUMNS)}) VALUES ({marks})", records)
        return len(records)

    def outside_quotes(self, since: float, until: Optional[float] = None,
                       exchange_ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Stored samples with since <= ts <= until, oldest first (ties by exchange_id, venue), as dicts with the
        ``OutsideSample.to_dict()`` keys (``flags`` decoded)."""
        base = f"SELECT {', '.join(_OUTSIDE_QUOTE_COLUMNS)} FROM outside_quotes WHERE ts >= ?"
        params: List[Any] = [float(since)]
        if until is not None:
            base += " AND ts <= ?"
            params.append(float(until))
        rows: List[sqlite3.Row] = []
        if exchange_ids is None:
            rows = self._query(base + " ORDER BY ts, exchange_id, venue", params)
        else:
            ids = sorted({str(e) for e in exchange_ids})
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ", ".join("?" for _ in chunk)
                rows += self._query(base + f" AND exchange_id IN ({marks})", params + chunk)
            rows.sort(key=lambda r: (r["ts"], r["exchange_id"], r["venue"]))
        out: List[Dict[str, Any]] = []
        for r in rows:
            item = {name: r[name] for name in _OUTSIDE_QUOTE_COLUMNS}
            item["flags"] = [str(f) for f in _load_json(r["flags"], []) or []]
            out.append(item)
        return out

    def put_move_alerts(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert ``moves.MoveAlert.to_dict()`` rows by alert_id (indexed columns + the JSON ``data``); returns the
        rows written. ``lag_outcome`` / ``lag_s`` come from ``data["lag"]``."""
        records: List[Tuple[Any, ...]] = []
        for raw in rows or ():
            if not isinstance(raw, Mapping) or not raw.get("alert_id") or raw.get("exchange_id") is None:
                continue
            detected = _num(raw.get("detected_at"))
            if detected is None:
                continue
            updated = _num(raw.get("updated_at"))
            lag = raw.get("lag") if isinstance(raw.get("lag"), Mapping) else {}
            race_key = raw.get("race_key")
            records.append((
                str(raw["alert_id"]), str(raw["exchange_id"]), str(race_key) if race_key is not None else None,
                detected, updated if updated is not None else detected, _num(raw.get("closed_at")),
                str(raw.get("state") or "open"), str(raw.get("status") or ""), str(lag.get("outcome") or "pending"),
                _num(lag.get("lag_s")), int(bool(raw.get("demo"))), _dumps(dict(raw)),
            ))
        if not records:
            return 0
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO move_alerts (alert_id, exchange_id, race_key, detected_at, updated_at, closed_at, "
                "state, status, lag_outcome, lag_s, demo, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
        return len(records)

    def move_alerts(self, since: Optional[float] = None, until: Optional[float] = None, state: Optional[str] = None,
                    limit: int = 5000) -> List[Dict[str, Any]]:
        """Stored alert dicts with detected_at in [since, until] (and ``state``), the newest ``limit``, oldest first
        (ties by alert_id). Works on a read-only store too (``moves --replay``)."""
        sql = "SELECT data FROM move_alerts WHERE 1 = 1"
        params: List[Any] = []
        if since is not None:
            sql += " AND detected_at >= ?"
            params.append(float(since))
        if until is not None:
            sql += " AND detected_at <= ?"
            params.append(float(until))
        if state is not None:
            sql += " AND state = ?"
            params.append(str(state))
        sql += " ORDER BY detected_at DESC, alert_id DESC LIMIT ?"
        params.append(max(0, int(limit)))
        out: List[Dict[str, Any]] = []
        for r in reversed(self._query(sql, params)):
            data = _load_json(r["data"], None)
            if isinstance(data, dict):
                out.append(data)
        return out

    # book snapshots ------------------------------------------------------------
    def add_book_snapshots(self, rows: Sequence[Any]) -> int:
        """Store order-book reads: dicts ``{"exchange_id", "ts", "source", "bids", "asks", "sequence"}`` or
        :class:`BookObservation`s (``observed_at`` is the ts). YES prices, best first, at most 20 levels a side.
        Upsert by (exchange_id, ts, source); returns the rows written."""
        records: List[Tuple[Any, ...]] = []
        for raw in rows or ():
            if isinstance(raw, BookObservation):
                eid, ts, source, bids, asks, seq = (raw.exchange_id, raw.observed_at, raw.source, raw.bids, raw.asks,
                                                    raw.sequence)
            elif isinstance(raw, Mapping):
                eid = raw.get("exchange_id")
                ts = raw.get("ts", raw.get("observed_at"))
                source, bids, asks, seq = raw.get("source") or "paper", raw.get("bids"), raw.get("asks"), raw.get("sequence")
            else:
                continue
            if eid is None or _num(ts) is None:
                continue
            sequence = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) else None
            records.append((
                str(eid), float(ts), str(source),
                _dumps(_book_levels(bids or [], descending=True)[:BOOK_SNAPSHOT_DEPTH]),
                _dumps(_book_levels(asks or [], descending=False)[:BOOK_SNAPSHOT_DEPTH]),
                sequence,
            ))
        if not records:
            return 0
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO book_snapshots (exchange_id, ts, source, bids, asks, sequence) VALUES (?, ?, ?, ?, ?, ?)",
                records,
            )
        return len(records)

    def book_snapshots(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[BookObservation]:
        """Stored book reads of one exchange with ``since <= ts <= until``, oldest first."""
        sql = "SELECT * FROM book_snapshots WHERE exchange_id = ? AND ts >= ?"
        params: List[Any] = [str(exchange_id), float(since)]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(float(until))
        rows = self._query(sql + " ORDER BY ts, source", params)
        return [
            BookObservation(
                exchange_id=r["exchange_id"], observed_at=r["ts"],
                bids=[(float(p), float(q)) for p, q in _load_json(r["bids"], [])],
                asks=[(float(p), float(q)) for p, q in _load_json(r["asks"], [])],
                source=r["source"], sequence=r["sequence"],
            )
            for r in rows
        ]

    # settlements ---------------------------------------------------------------
    def record_settlements(self, rows: Sequence[Any]) -> int:
        """Upsert :class:`SettlementInfo`s by exchange id; the FIRST ``detected_at`` is kept (that is when the
        bot could first have known it). Returns the rows written."""
        records: List[Tuple[Any, ...]] = []
        for raw in rows or ():
            info = raw if isinstance(raw, SettlementInfo) else _settlement_from_mapping(raw)
            if info is None or info.exchange_id is None:
                continue
            records.append((str(info.exchange_id), str(info.market_id or ""), info.settled_with, _num(info.settled_on),
                            _num(info.payout_yes), int(bool(info.refund)), _num(info.detected_at)))
        if not records:
            return 0
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO settlements (exchange_id, market_id, settled_with, settled_on, payout_yes, refund, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(exchange_id) DO UPDATE SET market_id = excluded.market_id, "
                "settled_with = excluded.settled_with, settled_on = excluded.settled_on, payout_yes = excluded.payout_yes, "
                "refund = excluded.refund, detected_at = COALESCE(settlements.detected_at, excluded.detected_at)",
                records,
            )
        return len(records)

    def settlements(self) -> Dict[str, SettlementInfo]:
        rows = self._query("SELECT * FROM settlements ORDER BY exchange_id")
        return {
            r["exchange_id"]: SettlementInfo(
                exchange_id=r["exchange_id"], market_id=r["market_id"], settled_with=r["settled_with"],
                settled_on=r["settled_on"], payout_yes=r["payout_yes"], refund=bool(r["refund"]), detected_at=r["detected_at"],
            )
            for r in rows
        }

    # leaderboard snapshots -----------------------------------------------------
    def add_leaderboard_snapshot(self, snap: Any) -> None:
        """Store one :class:`LeaderboardSnapshot` (or its dict), keyed by its time."""
        data = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap or {})
        at = _num(data.get("at"))
        if at is None:
            return
        total, rank = data.get("total"), data.get("my_rank")
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO leaderboard_snapshots (ts, period, total, my_rank, initial_balance, entries) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (at, str(data.get("period") or "all"),
                 int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None,
                 int(rank) if isinstance(rank, (int, float)) and not isinstance(rank, bool) else None,
                 _num(data.get("initial_balance")), _dumps(list(data.get("entries") or []))),
            )

    def leaderboard_snapshots(self, since: float, until: Optional[float] = None) -> List[LeaderboardSnapshot]:
        sql = "SELECT * FROM leaderboard_snapshots WHERE ts >= ?"
        params: List[Any] = [float(since)]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(float(until))
        rows = self._query(sql + " ORDER BY ts", params)
        return [
            LeaderboardSnapshot(at=r["ts"], period=r["period"], total=r["total"], my_rank=r["my_rank"],
                                initial_balance=r["initial_balance"],
                                entries=[dict(e) for e in _load_json(r["entries"], []) if isinstance(e, Mapping)])
            for r in rows
        ]

    # leases (one tracker per database, D46) ---------------------------------------
    def acquire_lease(self, name: str, owner: Mapping[str, Any], now: float, ttl_s: float) -> Optional[Dict[str, Any]]:
        """Take (or renew) the lease ``name`` for ``owner`` (``{"owner_id", "pid", "host"}``). Returns None when it
        is ours now, else the live holder ``{"owner_id", "pid", "host", "heartbeat_at", "alive"}``.

        Who holds it (D46, live-1 / live-5): another process on THIS machine is asked directly
        (:func:`lease_holder_alive`): while it runs, the lease is refused whatever its heartbeat age (up to
        LEASE_LIVE_HOLDER_MAX_S: a hung or suspended process cannot lock the store for ever) and ``alive`` is True;
        once it has ended (a crash, a closed terminal) its lease is taken over at once. A holder on another machine
        (or this very process) holds it while its heartbeat is younger than ``ttl_s`` (``alive`` None); an older
        lease is taken over. ``last_takeover`` records the holder a successful call took over (else None)."""
        owner_id = str(owner.get("owner_id"))
        pid = owner.get("pid")
        host = owner.get("host")
        stamp = float(now)
        self.last_takeover = None
        with self._write(immediate=True) as conn:
            row = conn.execute("SELECT * FROM leases WHERE name = ?", (str(name),)).fetchone()
            if row is not None and row["owner_id"] != owner_id:
                held = {"owner_id": row["owner_id"], "pid": row["pid"], "host": row["host"],
                        "heartbeat_at": row["heartbeat_at"]}
                age = stamp - float(row["heartbeat_at"])
                alive = lease_holder_alive(held)
                if alive is True:
                    limit = max(float(ttl_s), LEASE_LIVE_HOLDER_MAX_S)
                    if -limit < age < limit:
                        return {**held, "alive": True}
                elif alive is None and -float(ttl_s) < age < float(ttl_s):
                    return {**held, "alive": None}
                self.last_takeover = {**held, "alive": alive, "age_s": age}
            if row is not None and row["owner_id"] == owner_id:
                conn.execute("UPDATE leases SET heartbeat_at = ?, pid = ?, host = ? WHERE name = ?",
                             (stamp, pid, host, str(name)))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO leases (name, owner_id, pid, host, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(name), owner_id, pid, host, stamp, stamp),
                )
        return None

    def renew_lease(self, name: str, owner_id: str, now: float) -> bool:
        """Refresh our heartbeat; False when the lease is no longer ours (taken over or released)."""
        with self._write() as conn:
            cur = conn.execute("UPDATE leases SET heartbeat_at = ? WHERE name = ? AND owner_id = ?",
                               (float(now), str(name), str(owner_id)))
            return cur.rowcount == 1

    def release_lease(self, name: str, owner_id: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM leases WHERE name = ? AND owner_id = ?", (str(name), str(owner_id)))

    def lease(self, name: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM leases WHERE name = ?", (str(name),))
        return dict(rows[0]) if rows else None

    # paper trading (paper.PaperPersistence) -------------------------------------
    def paper_save_run(self, run_id: str, started_at: float, config: Mapping[str, Any], state: Mapping[str, Any],
                       updated_at: float) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO paper_runs (run_id, started_at, config, state, updated_at, ended_at) VALUES (?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(run_id) DO UPDATE SET started_at = excluded.started_at, config = excluded.config, "
                "state = excluded.state, updated_at = excluded.updated_at",
                (str(run_id), float(started_at), _dumps(config), _dumps(state), float(updated_at)),
            )

    @staticmethod
    def _run_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {"run_id": row["run_id"], "started_at": row["started_at"], "config": _load_json(row["config"], {}),
                "state": _load_json(row["state"], {}), "updated_at": row["updated_at"], "ended_at": row["ended_at"]}

    def paper_load_run(self, run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """A run by id, or (``None``) the newest run that has not ended."""
        if run_id is None:
            rows = self._query("SELECT * FROM paper_runs WHERE ended_at IS NULL ORDER BY started_at DESC, rowid DESC LIMIT 1")
        else:
            rows = self._query("SELECT * FROM paper_runs WHERE run_id = ?", (str(run_id),))
        return self._run_dict(rows[0] if rows else None)

    def paper_last_ended_run(self) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM paper_runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC, started_at DESC, rowid DESC LIMIT 1"
        )
        return self._run_dict(rows[0] if rows else None)

    def paper_end_run(self, run_id: str, ended_at: float) -> None:
        with self._write() as conn:
            conn.execute("UPDATE paper_runs SET ended_at = ? WHERE run_id = ?", (float(ended_at), str(run_id)))

    def paper_runs(self) -> List[Dict[str, Any]]:
        """Every stored run, oldest first (``config``/``state`` decoded)."""
        rows = self._query("SELECT * FROM paper_runs ORDER BY started_at, rowid")
        return [d for d in (self._run_dict(r) for r in rows) if d is not None]

    def paper_run_times(self) -> List[Dict[str, Any]]:
        """Every stored run's ``{"run_id", "started_at", "updated_at", "ended_at"}``, oldest first, without decoding
        its config or state (what a backtest needs to tell whether its window overlaps a paper run, lookahead-1)."""
        rows = self._query("SELECT run_id, started_at, updated_at, ended_at FROM paper_runs ORDER BY started_at, rowid")
        return [{"run_id": r["run_id"], "started_at": r["started_at"], "updated_at": r["updated_at"],
                 "ended_at": r["ended_at"]} for r in rows]

    def paper_put_orders(self, run_id: str, orders: Sequence[Mapping[str, Any]]) -> None:
        rows = [(str(run_id), str(o["order_id"]), str(o.get("portfolio_id") or ""), float(o.get("created_at") or 0.0),
                 str(o.get("status") or ""), _dumps(o)) for o in _paper_dicts(orders) if o.get("order_id") is not None]
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO paper_orders (run_id, order_id, portfolio_id, created_at, status, data) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, order_id) DO UPDATE SET portfolio_id = excluded.portfolio_id, "
                "created_at = excluded.created_at, status = excluded.status, data = excluded.data",
                rows,
            )

    def paper_add_fills(self, run_id: str, fills: Sequence[Mapping[str, Any]]) -> None:
        rows = [(str(run_id), str(f["fill_id"]), str(f.get("portfolio_id") or ""), float(f.get("ts") or 0.0), _dumps(f))
                for f in _paper_dicts(fills) if f.get("fill_id") is not None]
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO paper_fills (run_id, fill_id, portfolio_id, ts, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, fill_id) DO UPDATE SET portfolio_id = excluded.portfolio_id, ts = excluded.ts, "
                "data = excluded.data",
                rows,
            )

    def paper_add_trades(self, run_id: str, trades: Sequence[Mapping[str, Any]]) -> None:
        rows = [(str(run_id), str(t["trade_id"]), str(t.get("portfolio_id") or ""), float(t.get("closed_at") or 0.0),
                 _dumps(t)) for t in _paper_dicts(trades) if t.get("trade_id") is not None]
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO paper_trades (run_id, trade_id, portfolio_id, closed_at, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, trade_id) DO UPDATE SET portfolio_id = excluded.portfolio_id, "
                "closed_at = excluded.closed_at, data = excluded.data",
                rows,
            )

    def paper_add_equity(self, run_id: str, points: Sequence[Mapping[str, Any]]) -> None:
        rows = []
        for p in _paper_dicts(points):
            if p.get("portfolio_id") is None or _num(p.get("ts")) is None:
                continue
            rows.append((str(run_id), str(p["portfolio_id"]), float(p["ts"]), _num(p.get("cash")) or 0.0,
                         _num(p.get("reserved_cash")) or 0.0, _num(p.get("liq_value")) or 0.0,
                         _num(p.get("mark_value")) or 0.0, _num(p.get("fv_value")), int(p.get("open_positions") or 0)))
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO paper_equity (run_id, portfolio_id, ts, cash, reserved_cash, liq_value, mark_value, "
                "fv_value, open_positions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def paper_put_events(self, run_id: str, events: Sequence[Mapping[str, Any]]) -> None:
        rows = [(str(run_id), str(e["event_id"]), str(e.get("kind") or ""), float(e.get("t0") or 0.0), _dumps(e))
                for e in _paper_dicts(events) if e.get("event_id") is not None]
        if not rows:
            return
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO paper_events (run_id, event_id, kind, t0, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, event_id) DO UPDATE SET kind = excluded.kind, t0 = excluded.t0, data = excluded.data",
                rows,
            )

    def paper_fills(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        """Newest first."""
        return self._paper_rows("paper_fills", run_id, portfolio_id, "ts DESC, rowid DESC", limit)

    def paper_trades(self, run_id: str, portfolio_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Newest first (by closed_at)."""
        return self._paper_rows("paper_trades", run_id, portfolio_id, "closed_at DESC, rowid DESC", limit)

    def paper_orders(self, run_id: str, statuses: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Oldest first (by created_at); ``statuses`` filters by status."""
        sql = "SELECT data FROM paper_orders WHERE run_id = ?"
        params: List[Any] = [str(run_id)]
        if statuses is not None:
            wanted = [str(s) for s in statuses]
            if not wanted:
                return []
            sql += " AND status IN (" + ", ".join("?" for _ in wanted) + ")"
            params += wanted
        return [_load_json(r["data"], {}) for r in self._query(sql + " ORDER BY created_at, rowid", params)]

    def paper_events(self, run_id: str, kind: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Oldest first (by t0, ties by event id: like paper.MemoryPaperPersistence)."""
        sql = "SELECT data FROM paper_events WHERE run_id = ?"
        params: List[Any] = [str(run_id)]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(str(kind))
        sql += " ORDER BY t0, event_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [_load_json(r["data"], {}) for r in self._query(sql, params)]

    def paper_equity(self, run_id: str, portfolio_id: Optional[str] = None, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """Oldest first: ``EquityPoint.to_dict()`` rows."""
        sql = "SELECT * FROM paper_equity WHERE run_id = ?"
        params: List[Any] = [str(run_id)]
        if portfolio_id is not None:
            sql += " AND portfolio_id = ?"
            params.append(str(portfolio_id))
        if since is not None:
            sql += " AND ts >= ?"
            params.append(float(since))
        rows = self._query(sql + " ORDER BY ts, portfolio_id", params)
        return [
            {"portfolio_id": r["portfolio_id"], "ts": r["ts"], "cash": r["cash"], "reserved_cash": r["reserved_cash"],
             "liq_value": r["liq_value"], "mark_value": r["mark_value"], "fv_value": r["fv_value"],
             "open_positions": r["open_positions"]}
            for r in rows
        ]

    def _paper_rows(self, table: str, run_id: str, portfolio_id: Optional[str], order: str,
                    limit: Optional[int]) -> List[Dict[str, Any]]:
        sql = f"SELECT data FROM {table} WHERE run_id = ?"
        params: List[Any] = [str(run_id)]
        if portfolio_id is not None:
            sql += " AND portfolio_id = ?"
            params.append(str(portfolio_id))
        sql += f" ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [_load_json(r["data"], {}) for r in self._query(sql, params)]


# --------------------------------------------------------------------------- v2 helpers


def _load_json(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if isinstance(default, dict) and not isinstance(value, dict):
        return default
    if isinstance(default, list) and not isinstance(value, list):
        return default
    return value


def _paper_dicts(items: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Paper records as plain JSON dicts (dataclasses through ``to_dict``)."""
    out: List[Dict[str, Any]] = []
    for item in items or ():
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            item = item.to_dict() if hasattr(item, "to_dict") else dataclasses.asdict(item)
        if isinstance(item, Mapping):
            out.append(json.loads(_dumps(item)))
    return out


def _record_from_mapping(raw: Any) -> Optional[FairValueRecord]:
    if not isinstance(raw, Mapping):
        return None
    try:
        fields = _known_fields(FairValueRecord, raw)
        fields.setdefault("value", None)
        fields.setdefault("source", "")
        return FairValueRecord(**fields)
    except TypeError:
        return None


def _settlement_from_mapping(raw: Any) -> Optional[SettlementInfo]:
    if not isinstance(raw, Mapping):
        return None
    try:
        fields = _known_fields(SettlementInfo, raw)
        fields.setdefault("market_id", "")
        fields.setdefault("settled_with", None)
        return SettlementInfo(**fields)
    except TypeError:
        return None
