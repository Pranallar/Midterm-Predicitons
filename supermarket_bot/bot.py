"""Market-data bot: find the trading context, snapshot prices, and watch for moves.

Every read is pinned to one trading context. For the Susquehanna Predictions Cup
that is a tournament: its UUID ``id`` is passed as ``tournamentId`` on every
price/orderbook read so prices come from that tournament's isolated order books.
Unbound (non-organization) keys can use the public context instead.

Request cost of one snapshot: ``ceil(markets / 100)`` market-list reads plus
``ceil(exchanges / 100)`` bulk-price reads, so watching a whole tournament every
30 seconds uses only a few reads per minute out of the 100/minute budget.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, TextIO

from .books import books_from_market_orderbook
from .client import SuperMarketClient
from .display import fmt_delta, fmt_price, table, truncate
from .errors import SuperMarketError

log = logging.getLogger("supermarket_bot")

ROW_FIELDS = [
    "taken_at",
    "tournament",
    "market_id",
    "market_title",
    "market_status",
    "settlement_date",
    "exchange_id",
    "option",
    "latest_price",
    "best_bid",
    "best_ask",
    "spread",
    "mid",
]


class ContextError(SuperMarketError):
    """The trading context could not be determined automatically."""


@dataclass(frozen=True)
class Context:
    """A trading context: one tournament, or the public global context."""

    tournament_id: Optional[str]
    slug: Optional[str]
    name: str
    currency: Optional[str] = None
    status: Optional[str] = None

    @property
    def is_public(self) -> bool:
        return self.tournament_id is None

    @property
    def label(self) -> str:
        return self.slug or "public"

    @classmethod
    def public(cls) -> "Context":
        return cls(None, None, "Public global markets")

    @classmethod
    def from_tournament(cls, t: Mapping[str, Any]) -> "Context":
        return cls(
            tournament_id=str(t["id"]),
            slug=t.get("slug"),
            name=t.get("name") or t.get("slug") or str(t["id"]),
            currency=t.get("currencyName"),
            status=t.get("status"),
        )


def resolve_context(client: SuperMarketClient, slug: Optional[str] = None, public: bool = False) -> Context:
    """Pick the tournament to read.

    * ``slug`` given → that tournament (``GET /tournaments/{slug}``).
    * ``public`` → the public global context (unbound keys).
    * otherwise → the only active tournament the key can access; if there are
      several, raise :class:`ContextError` listing them so you can pass ``--tournament``.
      With no tournaments at all, fall back to the public context.
    """
    if public:
        return Context.public()
    if slug:
        return Context.from_tournament(client.get_tournament(slug))
    active = list(client.iter_tournaments(status="active"))
    if len(active) == 1:
        return Context.from_tournament(active[0])
    if len(active) > 1:
        names = ", ".join(f"{t.get('slug')} ({t.get('name')})" for t in active)
        raise ContextError(
            f"Your key can access {len(active)} active tournaments: {names}. "
            "Choose one with --tournament <slug> or SUPERMARKET_TOURNAMENT."
        )
    every = list(client.iter_tournaments(status="any"))
    if len(every) == 1:
        log.info("No active tournament; using %s (%s)", every[0].get("slug"), every[0].get("status"))
        return Context.from_tournament(every[0])
    if every:
        names = ", ".join(f"{t.get('slug')} ({t.get('status')})" for t in every)
        raise ContextError(f"No active tournament. Available: {names}. Choose one with --tournament <slug>.")
    log.info("No tournaments found for this key; using the public global context")
    return Context.public()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mid(bid: Any, ask: Any) -> Optional[float]:
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        return round((bid + ask) / 2, 6)
    return None


@dataclass
class Snapshot:
    taken_at: datetime
    context: Context
    markets: List[Dict[str, Any]]
    rows: List[Dict[str, Any]]
    missing_ids: List[str] = field(default_factory=list)

    def by_exchange(self) -> Dict[str, Dict[str, Any]]:
        return {row["exchange_id"]: row for row in self.rows}


def build_rows(
    markets: Sequence[Mapping[str, Any]],
    prices: Sequence[Mapping[str, Any]],
    context: Context,
    taken_at: datetime,
) -> List[Dict[str, Any]]:
    """Join market metadata with bulk price quotes into one flat row per exchange."""
    quotes = {str(p.get("exchangeId")): p for p in prices}
    stamp = iso(taken_at)
    rows: List[Dict[str, Any]] = []
    for market in markets:
        for ex in market.get("exchanges") or []:
            eid = str(ex.get("id"))
            quote = quotes.get(eid, {})
            latest = quote.get("latestPrice", ex.get("latestPrice"))
            bid, ask = quote.get("bestBid"), quote.get("bestAsk")
            spread = quote.get("spread")
            if spread is None and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                spread = round(ask - bid, 6)
            rows.append(
                {
                    "taken_at": stamp,
                    "tournament": context.label,
                    "market_id": str(market.get("id")),
                    "market_title": market.get("title"),
                    "market_status": market.get("status"),
                    "settlement_date": market.get("settlementDate"),
                    "exchange_id": eid,
                    "option": ex.get("option") if ex.get("option") is not None else quote.get("option"),
                    "latest_price": latest,
                    "best_bid": bid,
                    "best_ask": ask,
                    "spread": spread,
                    "mid": _mid(bid, ask),
                }
            )
    return rows


def diff_rows(
    previous: Mapping[str, Mapping[str, Any]], current: Sequence[Mapping[str, Any]], min_move: float = 0.0
) -> List[Dict[str, Any]]:
    """Rows whose last price, best bid or best ask changed since the previous snapshot."""
    changes: List[Dict[str, Any]] = []
    for row in current:
        before = previous.get(row["exchange_id"])
        if before is None:
            if previous:
                changes.append({**row, "change": "new", "delta": None})
            continue
        moved = []
        for key in ("latest_price", "best_bid", "best_ask"):
            old, new = before.get(key), row.get(key)
            if old == new:
                continue
            if isinstance(old, (int, float)) and isinstance(new, (int, float)) and abs(new - old) < min_move:
                continue
            moved.append(key)
        if moved:
            old, new = before.get("latest_price"), row.get("latest_price")
            delta = round(new - old, 6) if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None
            changes.append({**row, "change": ",".join(moved), "delta": delta})
    return changes


class SnapshotWriter:
    """Appends every snapshot row to a daily JSONL file and rewrites ``latest.csv``."""

    def __init__(self, data_dir: Path, context: Context) -> None:
        self.dir = Path(data_dir) / context.label
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, snap: Snapshot) -> Dict[str, Path]:
        day = snap.taken_at.strftime("%Y-%m-%d")
        jsonl = self.dir / f"prices-{day}.jsonl"
        with jsonl.open("a", encoding="utf-8") as fh:
            for row in snap.rows:
                fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
        latest = self.dir / "latest.csv"
        tmp = latest.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(snap.rows)
        tmp.replace(latest)
        markets = self.dir / "markets.json"
        markets.write_text(json.dumps(snap.markets, indent=1, ensure_ascii=False), encoding="utf-8")
        return {"jsonl": jsonl, "csv": latest, "markets": markets}


PRICE_COLUMNS = [
    ("market_id", "MKT"),
    ("market_title", "MARKET"),
    ("exchange_id", "EXCH"),
    ("option", "OPTION"),
    ("last", "LAST"),
    ("bid", "BID"),
    ("ask", "ASK"),
    ("spread_s", "SPREAD"),
]


def price_table(rows: Sequence[Mapping[str, Any]], with_delta: bool = False) -> str:
    view = [
        {
            **row,
            "last": fmt_price(row.get("latest_price")),
            "bid": fmt_price(row.get("best_bid")),
            "ask": fmt_price(row.get("best_ask")),
            "spread_s": fmt_price(row.get("spread")),
            "delta_s": fmt_delta(row.get("delta")),
        }
        for row in rows
    ]
    columns = list(PRICE_COLUMNS)
    if with_delta:
        columns += [("delta_s", "ΔLAST"), ("change", "CHANGED")]
    return table(view, columns, max_width={"market_title": 48, "option": 20})


class MarketDataBot:
    def __init__(
        self,
        client: SuperMarketClient,
        context: Context,
        data_dir: Optional[Path] = None,
        out: Optional[TextIO] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.context = context
        self.writer = SnapshotWriter(data_dir, context) if data_dir is not None else None
        self.out = out
        self._sleep = sleep
        self._clock = clock

    def _print(self, text: str = "") -> None:
        if self.out is not None:
            print(text, file=self.out, flush=True)

    # -------------------------------------------------------------- discovery

    def iter_markets(self, status: str = "open", search: Optional[str] = None, max_items: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        if self.context.slug:
            return self.client.iter_tournament_markets(self.context.slug, status=status, search=search, max_items=max_items)
        return self.client.iter_markets(tournament_id=self.context.tournament_id, status=status, search=search, max_items=max_items)

    def list_markets(self, status: str = "open", search: Optional[str] = None, max_items: Optional[int] = None) -> List[Dict[str, Any]]:
        return list(self.iter_markets(status=status, search=search, max_items=max_items))

    # -------------------------------------------------------------- snapshots

    def snapshot(self, markets: Optional[Sequence[Mapping[str, Any]]] = None, status: str = "open", search: Optional[str] = None) -> Snapshot:
        if markets is None:
            markets = self.list_markets(status=status, search=search)
        ids = [str(ex.get("id")) for m in markets for ex in m.get("exchanges") or [] if ex.get("id") is not None]
        prices = self.client.get_prices(ids, tournament_id=self.context.tournament_id) if ids else {"data": [], "missingIds": []}
        taken_at = utc_now()
        rows = build_rows(markets, prices["data"], self.context, taken_at)
        if prices["missingIds"]:
            log.warning("%d exchange(s) missing from the price snapshot: %s", len(prices["missingIds"]), ", ".join(prices["missingIds"][:10]))
        snap = Snapshot(taken_at, self.context, [dict(m) for m in markets], rows, list(prices["missingIds"]))
        if self.writer is not None:
            self.writer.write(snap)
        return snap

    def watch(
        self,
        interval: float = 30.0,
        iterations: Optional[int] = None,
        status: str = "open",
        search: Optional[str] = None,
        min_move: float = 0.0,
        refresh_markets_every: float = 300.0,
        on_snapshot: Optional[Callable[[Snapshot, List[Dict[str, Any]]], None]] = None,
    ) -> None:
        """Poll prices forever (or ``iterations`` times), printing what moved."""
        markets: Optional[List[Dict[str, Any]]] = None
        markets_at = float("-inf")
        previous: Dict[str, Dict[str, Any]] = {}
        count = 0
        while iterations is None or count < iterations:
            started = self._clock()
            try:
                if markets is None or started - markets_at >= refresh_markets_every:
                    markets = self.list_markets(status=status, search=search)
                    markets_at = started
                snap = self.snapshot(markets)
            except SuperMarketError as exc:
                log.error("snapshot failed: %s", exc)
                snap = None
            if snap is not None:
                changes = diff_rows(previous, snap.rows, min_move=min_move)
                stamp = snap.taken_at.strftime("%H:%M:%S")
                if not previous:
                    self._print(f"[{stamp}] {self.context.name}: {len(markets or [])} markets, {len(snap.rows)} outcomes")
                    self._print(price_table(snap.rows))
                elif changes:
                    self._print(f"[{stamp}] {len(changes)} change(s)")
                    self._print(price_table(changes, with_delta=True))
                else:
                    self._print(f"[{stamp}] no changes")
                if on_snapshot is not None:
                    on_snapshot(snap, changes)
                previous = snap.by_exchange()
            count += 1
            if iterations is not None and count >= iterations:
                break
            self._sleep(max(0.0, interval - (self._clock() - started)))

    # -------------------------------------------------------------- analysis

    def scan(self, max_markets: int = 25, status: str = "open") -> Dict[str, Any]:
        """Look for mispricings the engine itself reports.

        * ``GET /relationships/constraints?violationsOnly=true`` — ALL relationship
          violations with suggested corrective trades.
        * ``GET /markets/{id}/orderbook`` for multi-outcome markets — engine
          ``overround`` and ``hasArbitrageOpportunity`` flags.

        Read-only: nothing is traded.
        """
        constraints = self.client.get_relationship_constraints(
            violations_only=True, tournament_id=self.context.tournament_id
        )
        multi = [m for m in self.list_markets(status=status) if m.get("isMultiOutcome")]
        if len(multi) > max_markets:
            log.info("scanning %d of %d multi-outcome markets (raise --max-markets for more)", max_markets, len(multi))
        books = []
        for market in multi[:max_markets]:
            try:
                resp = self.client.get_market_orderbook(market["id"], tournament_id=self.context.tournament_id, depth=1)
            except SuperMarketError as exc:
                log.warning("orderbook for market %s failed: %s", market.get("id"), exc)
                continue
            _, ob = books_from_market_orderbook(resp, self.context.tournament_id)
            books.append(
                {
                    "market_id": str(market["id"]),
                    "market_title": market.get("title"),
                    "outcomes": len(ob.get("exchanges") or []),
                    "overround": ob.get("overround"),
                    "arbitrage": bool(ob.get("hasArbitrageOpportunity")),
                }
            )
        return {
            "violations": constraints.get("data") or [],
            "violationsCount": constraints.get("violationsCount", 0),
            "computedAt": constraints.get("computedAt"),
            "markets": books,
            "scannedMarkets": len(books),
            "multiOutcomeMarkets": len(multi),
        }


def market_rows(markets: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Summary rows for a market listing table."""
    rows = []
    for m in markets:
        exchanges = m.get("exchanges") or []
        if len(exchanges) == 1:
            outcome = fmt_price(exchanges[0].get("latestPrice"))
        else:
            outcome = f"{len(exchanges)} outcomes"
        rows.append(
            {
                "id": m.get("id"),
                "title": truncate(m.get("title"), 60),
                "status": m.get("status"),
                "closes": (m.get("settlementDate") or "")[:16].replace("T", " "),
                "last": outcome,
                "categories": ",".join(m.get("categories") or []),
            }
        )
    return rows
