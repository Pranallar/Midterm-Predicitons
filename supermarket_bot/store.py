"""SQLite persistence for the tracker (ticks, candles, trades, surges, news cache, state).

Thread-safe: one connection guarded by a lock (WAL mode on file databases). Pass ``":memory:"``
for tests. See docs/DESIGN.md.

Layout (schema version 1, tracked with ``PRAGMA user_version``):

* ``markets`` / ``exchanges``: metadata from the market list (one row per market / outcome).
* ``ticks``: our own bulk-price snapshots, ``PRIMARY KEY (exchange_id, ts)``. Pruned to the
  last 10 days as new ticks arrive.
* ``candles``: backfilled ``price-history`` candles keyed by bucket *start* time.
* ``trades``: the trade tape, idempotent by trade id (per exchange, since the API has two
  id spaces: engine sequences and legacy ids).
* ``surges``: detected moves, with the attribution stored as JSON.
* ``news_cache`` / ``state``: JSON blobs keyed by string.

All timestamps are epoch seconds (UTC floats). API ISO strings are converted with
:func:`books.parse_time`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from .books import parse_time
from .models import SURGE_OPEN, Article, Attribution, ExchangeInfo, MarketInfo, PricePoint, Surge, TradeFlow, TradeRecord

log = logging.getLogger("supermarket_bot")

SCHEMA_VERSION = 1
TICK_RETENTION_S = 10 * 86400.0  # ticks older than this (relative to the newest insert) are pruned
PRUNE_EVERY = 500  # tick inserts between prunes
NEWS_RETENTION_S = 7 * 86400.0
RESOLUTION_SECONDS: Dict[str, float] = {"1m": 60.0, "5m": 300.0, "1h": 3600.0, "1d": 86400.0, "1w": 604800.0}
_MAX_RESOLUTION_S = max(RESOLUTION_SECONDS.values())

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

_MIGRATIONS: Dict[int, str] = {1: _SCHEMA_V1}

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


def _merge_surges(old: Surge, new: Surge) -> Surge:
    """Merge a fresh detection into the stored open surge for the same exchange and direction.

    Keeps the earliest start and the latest end; the larger ``|change|`` decides the window,
    its length and z-score. The peak is the extreme of both. ``detected_at`` (the first
    detection) and the attribution are kept.
    """
    merged = dataclasses.replace(old)
    if new.start_ts < old.start_ts:
        merged.start_ts, merged.start_price = new.start_ts, new.start_price
    if new.end_ts >= old.end_ts:
        merged.end_ts, merged.end_price = new.end_ts, new.end_price
    if abs(new.change) > abs(old.change):
        merged.change, merged.window, merged.window_s, merged.zscore = new.change, new.window, new.window_s, new.zscore
    if old.direction == "down":
        merged.peak_price = min(old.peak_price, new.peak_price)
    else:
        merged.peak_price = max(old.peak_price, new.peak_price)
    if new.current_price is not None:
        merged.current_price = new.current_price
    if new.reverted_fraction is not None:
        merged.reverted_fraction = new.reverted_fraction
    merged.detected_at = min(old.detected_at, new.detected_at)
    merged.status = SURGE_OPEN
    return merged


# --------------------------------------------------------------------------- store


class TrackerStore:
    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._memory = str(path) == ":memory:"
        self.path = str(path) if self._memory else str(Path(path).expanduser())
        if not self._memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0  # nesting level of _write() so inner writes join the outer transaction
        self._since_prune = PRUNE_EVERY  # prune on the first insert after opening
        self._closed = False
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=10.0,
            isolation_level=None,  # autocommit; transactions are explicit in _write()
        )
        self._conn.row_factory = sqlite3.Row
        try:
            if not self._memory:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

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
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run the block in one transaction (nested calls join the outer one)."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN")
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
            self._since_prune += len(records)
            if self._since_prune >= PRUNE_EVERY:
                self._since_prune = 0
                self._prune_ticks(conn, stamp - TICK_RETENTION_S, {r[0] for r in records})
        return len(records)

    @staticmethod
    def _prune_ticks(conn: sqlite3.Connection, cutoff: float, extra_ids: Any) -> None:
        # Per-exchange range deletes use the primary key, so no extra ts index is needed.
        ids = {r[0] for r in conn.execute("SELECT exchange_id FROM exchanges")} | set(extra_ids)
        conn.executemany("DELETE FROM ticks WHERE exchange_id = ? AND ts < ?", [(eid, cutoff) for eid in ids])

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

    def series(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[PricePoint]:
        """Merged, time-ordered points: candle closes (``source="candle"``, at candle end time)
        for the period before the first tick, then ticks. Marks via ``analytics.mark_price``.

        "The first tick" is the first tick inside ``[since, until]``, so candles also fill a
        window that starts before tracking did. Where 5m and 1h candles overlap, the finer
        5m candles win (generally: a finer resolution hides coarser closes inside its span).
        Candles with a null close are skipped.
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
            boundary = tick_rows[0]["ts"] if tick_rows else upper
            params: List[Any] = [eid, since - _MAX_RESOLUTION_S]
            sql = "SELECT resolution, ts, close FROM candles WHERE exchange_id = ? AND ts >= ?"
            if math.isfinite(boundary):
                sql += " AND ts < ?"
                params.append(boundary)
            candle_rows = self._conn.execute(sql + " ORDER BY ts", params).fetchall()

        by_res: Dict[str, List[Tuple[float, Optional[float]]]] = {}
        for row in candle_rows:
            length = RESOLUTION_SECONDS.get(row["resolution"])
            if length is None:
                continue
            close_ts = row["ts"] + length
            if close_ts < since or close_ts >= boundary or close_ts > upper:
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
        candle_points.sort(key=lambda p: p.ts)

        ticks = [
            PricePoint(ts=r["ts"], price=mark(r["last"], r["bid"], r["ask"]), last=r["last"], bid=r["bid"], ask=r["ask"], source="tick")
            for r in tick_rows
        ]
        return candle_points + ticks

    def latest(self, exchange_id: str) -> Optional[PricePoint]:
        """The newest tick, else the candle with the latest close time (None when nothing is stored)."""
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

    def tick_count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM ticks")[0][0])

    # trades -----------------------------------------------------------------
    def add_trades(self, exchange_id: str, trades: Sequence[Mapping[str, Any]]) -> int:
        """Insert trade-tape dicts (``id, createdAt, price, size, side``). Idempotent by id.

        Returns the number of trades that were new.
        """
        eid = str(exchange_id)
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
                 side.upper() if isinstance(side, str) else None)
            )
        if not records:
            return 0
        with self._write() as conn:
            before = conn.total_changes
            conn.executemany("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?)", records)
            return conn.total_changes - before

    def trades(self, exchange_id: str, since: float, until: Optional[float] = None) -> List[TradeRecord]:
        """Stored trades in ``[since, until]``, oldest first."""
        sql = "SELECT * FROM trades WHERE exchange_id = ? AND ts >= ?"
        params: List[Any] = [str(exchange_id), since]
        if until is not None:
            sql += " AND ts <= ?"
            params.append(until)
        rows = self._query(sql + " ORDER BY ts, length(trade_id), trade_id", params)
        return [
            TradeRecord(trade_id=r["trade_id"], exchange_id=r["exchange_id"], ts=r["ts"], price=r["price"], size=r["size"], side=r["side"])
            for r in rows
        ]

    # surges -----------------------------------------------------------------
    def record_surge(self, surge: Surge, merge_window_s: float = 6 * 3600) -> Surge:
        """Insert, or merge into the open surge for the same exchange+direction detected within
        ``merge_window_s`` (keep earliest start, latest end, larger |change|). Returns the stored surge with id.

        "Detected within" counts from the stored surge's first detection or its latest end,
        whichever is newer, so a move that keeps being re-detected stays one surge. The stored
        attribution is kept; the caller's ``surge`` object is not modified.
        """
        cutoff = surge.detected_at - merge_window_s
        with self._write() as conn:
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

    def surges(self, since: Optional[float] = None, status: Optional[str] = None, exchange_id: Optional[str] = None, limit: int = 200) -> List[Surge]:
        """Newest first (by detected_at).

        ``since`` keeps surges detected at or after it, or still extending (``end_ts``) at or
        after it.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if since is not None:
            clauses.append("(detected_at >= ? OR end_ts >= ?)")
            params += [since, since]
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
