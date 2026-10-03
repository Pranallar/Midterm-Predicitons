"""Command-line interface: ``python -m supermarket_bot <command>``.

Run ``python -m supermarket_bot --help`` for the command list.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO

from . import __version__
from .books import Book, books_from_market_orderbook, select_context
from .bot import Context, MarketDataBot, market_rows, price_table, resolve_context, utc_now
from .client import SuperMarketClient
from .config import ConfigError, Settings, mask_key
from .display import fmt_num, fmt_price, sparkline, table, truncate
from .errors import ApiError, SuperMarketError

log = logging.getLogger("supermarket_bot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supermarket_bot",
        description="Read-only market-data bot for the Super Market / Susquehanna Predictions Cup API.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-t", "--tournament", help="tournament slug (default: SUPERMARKET_TOURNAMENT or the only active one)")
    p.add_argument("--public", action="store_true", help="use the public global context instead of a tournament")
    p.add_argument("--json", action="store_true", help="print raw JSON instead of tables")
    p.add_argument("--env-file", default=".env", help="file to read settings from (default: .env)")
    p.add_argument("--data-dir", help="where snapshots and stream logs are written (default: data/)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for info logs, -vv for debug")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("account", help="verify the key: profile, balance and accessible tournaments")

    s = sub.add_parser("tournaments", help="list tournaments your key can access")
    s.add_argument("--status", default="any", choices=["draft", "active", "ended", "any"])

    s = sub.add_parser("markets", help="list markets in the tournament")
    _market_filters(s)
    s.add_argument("--limit", type=positive_int, help="stop after this many markets")

    s = sub.add_parser("market", help="one market: outcomes, prices and resolution tree")
    s.add_argument("market_id")

    s = sub.add_parser("snapshot", help="price every outcome once (bulk price endpoint)")
    _market_filters(s)
    s.add_argument("--save", action="store_true", help="also append to data/<tournament>/prices-<date>.jsonl")

    s = sub.add_parser("watch", help="poll prices on an interval and print what moved")
    _market_filters(s)
    s.add_argument("--interval", type=float, default=30.0, help="seconds between snapshots (default 30)")
    s.add_argument("--iterations", type=positive_int, help="stop after N snapshots (default: run until Ctrl-C)")
    s.add_argument("--min-move", type=float, default=0.0, help="ignore price moves smaller than this")
    s.add_argument("--refresh-markets", type=float, default=300.0, help="seconds between market-list refreshes")
    s.add_argument("--no-save", action="store_true", help="do not write snapshots to the data directory")

    s = sub.add_parser("book", help="order book for a market (all outcomes) or one exchange")
    s.add_argument("market_id", nargs="?")
    s.add_argument("--exchange", help="show one exchange's book instead of a whole market")
    s.add_argument("--depth", type=bounded_int(1, 200), default=10, help="price levels per side (1-200, default 10)")

    s = sub.add_parser("trades", help="recent trades for an exchange (newest first)")
    s.add_argument("exchange_id")
    s.add_argument("--limit", type=positive_int, default=50, help="number of trades (default 50)")
    s.add_argument("--since", help="ISO-8601 start time (inclusive)")

    s = sub.add_parser("history", help="OHLCV price candles for an exchange")
    s.add_argument("exchange_id")
    s.add_argument("--resolution", default="1h", choices=["1m", "5m", "1h", "1d", "1w"])
    s.add_argument("--limit", type=bounded_int(1, 1000), default=48, help="number of candles (1-1000, default 48)")
    s.add_argument("--since", help="ISO-8601 start time; without it the newest candles are returned")
    s.add_argument("--csv", help="also write the candles to this CSV file")

    s = sub.add_parser("leaderboard", help="tournament standings")
    s.add_argument("--period", default="all", choices=["1d", "7d", "30d", "quarter", "all"])
    s.add_argument("--sort", choices=["pnl", "roi", "winRate", "volume", "trades"])
    s.add_argument("--limit", type=positive_int, default=20, help="rows to show (paged 100 at a time)")

    s = sub.add_parser("scan", help="engine-reported mispricings: ALL violations and overround")
    s.add_argument("--max-markets", type=positive_int, default=25, help="multi-outcome market books to read (1 read each)")

    s = sub.add_parser("portfolio", help="your positions and P&L in the tournament")
    s.add_argument(
        "--period",
        default="quarter",
        choices=["day", "week", "month", "quarter", "year", "all"],
        help="P&L lookback (default quarter; the API reports no period P&L for 'all')",
    )

    s = sub.add_parser("stream", help="live trades and books over WebSocket (needs `pip install realtime`)")
    s.add_argument("market_ids", nargs="*", help="markets to follow (default: every open market)")
    s.add_argument("--duration", type=float, help="stop after this many seconds")
    s.add_argument("--resync-interval", type=float, default=90.0, help="seconds between REST book resyncs per market")
    s.add_argument("--max-markets", type=positive_int, default=50, help="cap on markets when following all of them")
    s.add_argument("--no-log", action="store_true", help="do not write data/<tournament>/stream-<date>.jsonl")
    return p


def positive_int(text: str) -> int:
    return bounded_int(1, None)(text)


def bounded_int(low: int, high: Optional[int]) -> Any:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}")
        if value < low or (high is not None and value > high):
            span = f"{low}-{high}" if high is not None else f">= {low}"
            raise argparse.ArgumentTypeError(f"must be {span}, got {value}")
        return value

    return parse


def _market_filters(s: argparse.ArgumentParser) -> None:
    s.add_argument("--status", default="open", choices=["open", "closed", "settled", "any"])
    s.add_argument("--search", help="case-insensitive title search")


class App:
    def __init__(self, args: argparse.Namespace, settings: Settings, client: SuperMarketClient, out: TextIO) -> None:
        self.args = args
        self.settings = settings
        self.client = client
        self.out = out
        self._context: Optional[Context] = None

    def print(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def dump(self, data: Any) -> None:
        self.print(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    @property
    def context(self) -> Context:
        if self._context is None:
            slug = self.args.tournament or self.settings.tournament
            self._context = resolve_context(self.client, slug=slug, public=self.args.public)
            log.info("context: %s (%s)", self._context.name, self._context.tournament_id or "public")
        return self._context

    def bot(self, save: bool = False) -> MarketDataBot:
        return MarketDataBot(self.client, self.context, data_dir=self.settings.data_dir if save else None, out=self.out)

    def header(self) -> None:
        if not self.args.json:
            ctx = self.context
            cur = f" · {ctx.currency}" if ctx.currency else ""
            self.print(f"{ctx.name} [{ctx.label}]{cur}")

    # ---------------------------------------------------------------- commands

    def cmd_account(self) -> None:
        account = self.client.get_account()
        tournaments = list(self.client.iter_tournaments(status="any"))
        if self.args.json:
            return self.dump({"account": account, "tournaments": tournaments})
        self.print(f"Key OK ({mask_key(self.settings.api_key)}) → {self.settings.base_url}")
        self.print(f"User:    {account.get('username') or '—'} ({account.get('email') or 'no email'})")
        self.print(f"Profile: {account.get('id')}")
        self.print(f"Balance: {fmt_num(account.get('balance'))}")
        self.print()
        self._tournament_table(tournaments)

    def cmd_tournaments(self) -> None:
        tournaments = list(self.client.iter_tournaments(status=self.args.status))
        if self.args.json:
            return self.dump(tournaments)
        self._tournament_table(tournaments)

    def _tournament_table(self, tournaments: Sequence[Dict[str, Any]]) -> None:
        if not tournaments:
            self.print("No tournaments are visible to this key (it may be an unbound key: use --public).")
            return
        rows = [
            {
                "slug": t.get("slug"),
                "name": t.get("name"),
                "status": t.get("status"),
                "ends": (t.get("endDate") or "")[:10],
                "balance": f"{fmt_num(t.get('myBalance'))} {t.get('currencyName') or ''}".strip(),
                "joined": "pending" if t.get("isPendingEnrolment") else ("yes" if t.get("joinedAt") else "no"),
                "id": t.get("id"),
            }
            for t in tournaments
        ]
        self.print(
            table(
                rows,
                [("slug", "SLUG"), ("name", "NAME"), ("status", "STATUS"), ("ends", "ENDS"), ("balance", "MY BALANCE"), ("joined", "ENROLLED"), ("id", "TOURNAMENT ID")],
                max_width={"name": 40},
            )
        )

    def cmd_markets(self) -> None:
        markets = self.bot().list_markets(status=self.args.status, search=self.args.search, max_items=self.args.limit)
        if self.args.json:
            return self.dump(markets)
        self.header()
        self.print(f"{len(markets)} market(s)")
        self.print(
            table(
                market_rows(markets),
                [("id", "ID"), ("title", "TITLE"), ("status", "STATUS"), ("closes", "SETTLES (UTC)"), ("last", "LAST"), ("categories", "CATEGORY")],
            )
        )

    def cmd_market(self) -> None:
        ctx = self.context
        market = self.client.get_market(self.args.market_id, tournament_id=ctx.tournament_id)
        nodes = self.client.get_market_nodes(self.args.market_id, tournament_id=ctx.tournament_id)
        ids = [ex["id"] for ex in market.get("exchanges") or []]
        prices = self.client.get_prices(ids, tournament_id=ctx.tournament_id) if ids else {"data": [], "missingIds": []}
        if self.args.json:
            return self.dump({"market": market, "nodes": nodes, "prices": prices})
        view = select_context(market, ctx.tournament_id)
        self.header()
        self.print(f"#{market.get('id')} {market.get('title')}")
        self.print(f"status {view.get('status', market.get('status'))} · settles {market.get('settlementDate') or '—'}"
                   f" · categories {', '.join(market.get('categories') or []) or '—'}")
        settled = view.get("settledWith", market.get("settledWith"))
        if settled:
            self.print(f"settled with {settled} on {view.get('settledOn', market.get('settledOn'))}")
        quotes = {str(q.get("exchangeId")): q for q in prices["data"]}
        rows = []
        for ex in market.get("exchanges") or []:
            q = quotes.get(str(ex.get("id")), {})
            rows.append(
                {
                    "id": ex.get("id"),
                    "option": ex.get("option"),
                    "last": fmt_price(q.get("latestPrice", ex.get("latestPrice"))),
                    "bid": fmt_price(q.get("bestBid")),
                    "ask": fmt_price(q.get("bestAsk")),
                    "spread": fmt_price(q.get("spread")),
                    "initial": fmt_price(ex.get("initialPrice")),
                }
            )
        self.print(table(rows, [("id", "EXCH"), ("option", "OPTION"), ("last", "LAST"), ("bid", "BID"), ("ask", "ASK"), ("spread", "SPREAD"), ("initial", "INITIAL")]))
        root = nodes.get("root")
        if root:
            self.print("\nResolution logic:")
            for line in _tree_lines(root):
                self.print("  " + line)

    def cmd_snapshot(self) -> None:
        snap = self.bot(save=self.args.save).snapshot(status=self.args.status, search=self.args.search)
        if self.args.json:
            return self.dump({"takenAt": snap.taken_at.isoformat(), "rows": snap.rows, "missingIds": snap.missing_ids})
        self.header()
        self.print(f"{len(snap.markets)} market(s), {len(snap.rows)} outcome(s) at {snap.taken_at:%Y-%m-%d %H:%M:%S} UTC")
        self.print(price_table(snap.rows))
        if snap.missing_ids:
            self.print(f"\nNo quote for exchange(s): {', '.join(snap.missing_ids)}")
        if self.args.save:
            self.print(f"\nSaved to {self.settings.data_dir / self.context.label}/")

    def cmd_watch(self) -> None:
        bot = self.bot(save=not self.args.no_save)
        if not self.args.json:
            self.header()
            where = "" if self.args.no_save else f", saving to {self.settings.data_dir / self.context.label}/"
            self.print(f"Polling every {self.args.interval:g}s{where}. Ctrl-C to stop.")

        first = [True]

        def emit_json(snap: Any, changes: List[Dict[str, Any]]) -> None:
            if self.args.json:
                record: Dict[str, Any] = {"takenAt": snap.taken_at.isoformat(), "changes": changes}
                if first[0]:
                    record["rows"] = snap.rows  # full baseline once, then only what moved
                    first[0] = False
                self.print(json.dumps(record, default=str))

        if self.args.json:
            bot.out = None
        bot.watch(
            interval=self.args.interval,
            iterations=self.args.iterations,
            status=self.args.status,
            search=self.args.search,
            min_move=self.args.min_move,
            refresh_markets_every=self.args.refresh_markets,
            on_snapshot=emit_json,
        )

    def cmd_book(self) -> None:
        ctx = self.context
        depth = max(1, min(200, self.args.depth))
        if self.args.exchange:
            resp = self.client.get_exchange_orderbook(self.args.exchange, depth=depth, tournament_id=ctx.tournament_id)
            if self.args.json:
                return self.dump(resp)
            self.header()
            self._print_book(Book.from_payload(resp), depth)
            return
        if not self.args.market_id:
            raise SystemExit("book: give a MARKET_ID or --exchange EXCHANGE_ID")
        resp = self.client.get_market_orderbook(self.args.market_id, tournament_id=ctx.tournament_id, depth=depth)
        if self.args.json:
            return self.dump(resp)
        books, ob = books_from_market_orderbook(resp, ctx.tournament_id, market_id=self.args.market_id)
        self.header()
        self.print(f"Market {self.args.market_id}: {len(books)} outcome(s)"
                   f" · overround {fmt_price(ob.get('overround'))}"
                   f"{' · ARBITRAGE FLAGGED' if ob.get('hasArbitrageOpportunity') else ''}")
        for book in books:
            self.print()
            self._print_book(book, depth)

    def _print_book(self, book: Book, depth: int) -> None:
        version = f" · {book.as_of}" if book.as_of else ""
        option = f" ({book.option})" if book.option else ""
        self.print(f"Exchange {book.exchange_id}{option} · mid {fmt_price(book.mid)} · spread {fmt_price(book.spread)}{version}")
        rows = []
        for i in range(min(depth, max(len(book.bids), len(book.asks)))):
            bid = book.bids[i] if i < len(book.bids) else None
            ask = book.asks[i] if i < len(book.asks) else None
            rows.append(
                {
                    "bq": fmt_num(bid.quantity) if bid else "",
                    "bid": fmt_price(bid.price) if bid else "",
                    "ask": fmt_price(ask.price) if ask else "",
                    "aq": fmt_num(ask.quantity) if ask else "",
                }
            )
        if not rows:
            self.print("  (empty book)")
            return
        self.print(table(rows, [("bq", "BID QTY"), ("bid", "BID"), ("ask", "ASK"), ("aq", "ASK QTY")]))

    def cmd_trades(self) -> None:
        ctx = self.context
        trades = list(
            self.client.iter_trades(self.args.exchange_id, tournament_id=ctx.tournament_id, start=self.args.since, max_items=self.args.limit)
        )
        if self.args.json:
            return self.dump(trades)
        self.header()
        self.print(f"Exchange {self.args.exchange_id}: {len(trades)} trade(s), newest first (prices are YES-denominated)")
        rows = [
            {"time": (t.get("createdAt") or "")[:19].replace("T", " "), "price": fmt_price(t.get("price")), "size": fmt_num(t.get("size")), "side": t.get("side"), "volume": fmt_num(t.get("volume")), "id": t.get("id")}
            for t in trades
        ]
        self.print(table(rows, [("time", "TIME (UTC)"), ("price", "PRICE"), ("size", "SIZE"), ("side", "SIDE"), ("volume", "VOLUME"), ("id", "TRADE ID")]))

    def cmd_history(self) -> None:
        ctx = self.context
        resp = self.client.get_price_history(
            self.args.exchange_id,
            tournament_id=ctx.tournament_id,
            resolution=self.args.resolution,
            start=self.args.since,
            limit=max(1, min(1000, self.args.limit)),
        )
        candles = resp.get("candles") or []
        if self.args.csv:
            path = Path(self.args.csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["time", "open", "high", "low", "close", "vwap", "volume", "tradeCount"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(candles)
        if self.args.json:
            return self.dump(resp)
        self.header()
        complete = (resp.get("coverage") or {}).get("complete", True)
        self.print(f"Exchange {self.args.exchange_id} · {self.args.resolution} candles · {len(candles)} bucket(s) with trades"
                   f"{'' if complete else ' · (truncated: more candles exist)'}")
        if candles:
            self.print(f"close {sparkline([c.get('close') for c in candles])}")
        rows = [
            {"time": (c.get("time") or "")[:16].replace("T", " "), "o": fmt_price(c.get("open")), "h": fmt_price(c.get("high")), "l": fmt_price(c.get("low")), "c": fmt_price(c.get("close")), "vwap": fmt_price(c.get("vwap")), "vol": fmt_num(c.get("volume")), "n": c.get("tradeCount")}
            for c in candles
        ]
        self.print(table(rows, [("time", "TIME (UTC)"), ("o", "OPEN"), ("h", "HIGH"), ("l", "LOW"), ("c", "CLOSE"), ("vwap", "VWAP"), ("vol", "VOLUME"), ("n", "TRADES")]))
        if self.args.csv:
            self.print(f"\nWrote {len(candles)} candle(s) to {self.args.csv}")

    def cmd_leaderboard(self) -> None:
        ctx = self.context
        wanted = self.args.limit
        period = self.args.period

        def fetch(offset: int, limit: int) -> Dict[str, Any]:
            # Both leaderboard endpoints cap limit at 100, so page with offset.
            if ctx.slug and period != "quarter":  # the tournament endpoint has no "quarter"
                return self.client.get_tournament_leaderboard(ctx.slug, period=period, sort=self.args.sort, limit=limit, offset=offset)
            if ctx.slug:
                return self.client.get_leaderboard(tournament_slug=ctx.slug, period=period, sort=self.args.sort, limit=limit, offset=offset)
            return self.client.get_leaderboard(period=period, limit=limit, offset=offset)  # global: no sort

        want = min(100, wanted)
        resp = fetch(0, want)
        entries = list(resp.get("leaderboard") or [])
        total = resp.get("total")
        full_page = len(entries) >= want
        while full_page and len(entries) < wanted and isinstance(total, int) and len(entries) < total:
            want = min(100, wanted - len(entries))
            page = fetch(len(entries), want).get("leaderboard") or []
            entries.extend(page)
            full_page = len(page) >= want  # a short page is the last one
        resp = {**resp, "leaderboard": entries[:wanted]}
        if self.args.json:
            return self.dump(resp)
        self.header()
        my_rank = resp.get("myRank")
        self.print(f"{resp.get('total', '?')} ranked · period {resp.get('period')}" + (f" · your rank {my_rank}" if my_rank else ""))
        rows = []
        for e in resp.get("leaderboard") or []:
            rows.append(
                {
                    "rank": e.get("rank"),
                    "user": truncate(e.get("username") or e.get("profileId"), 24),
                    "pnl": fmt_num(e.get("pnl", e.get("finalTotalValue"))),
                    "roi": f"{fmt_num(e.get('roi'))}%" if e.get("roi") is not None else "—",
                    "win": f"{fmt_num(e.get('winRate'))}%" if e.get("winRate") is not None else "—",
                    "trades": e.get("tradesCount"),
                    "volume": fmt_num(e.get("volume")),
                }
            )
        self.print(table(rows, [("rank", "#"), ("user", "USER"), ("pnl", "P&L"), ("roi", "ROI"), ("win", "WIN"), ("trades", "TRADES"), ("volume", "VOLUME")]))

    def cmd_scan(self) -> None:
        result = self.bot().scan(max_markets=max(1, self.args.max_markets))
        if self.args.json:
            return self.dump(result)
        self.header()
        self.print(f"ALL relationship violations: {result['violationsCount']} (computed {result.get('computedAt') or '—'})")
        for v in result["violations"]:
            self.print(f"  • {v.get('type')} violated by {fmt_price(v.get('violationAmount'))}: {v.get('reason')}")
            rule = (v.get("constraint") or {}).get("priceRule")
            if rule:
                self.print(f"    rule: {rule}")
            for t in v.get("suggestedCorrectiveTrades") or []:
                self.print(f"    suggests {t.get('action')} {t.get('outcomeSide')} on exch {t.get('exchangeId')}"
                           f" ({t.get('marketTitle') or t.get('marketId')}, now {fmt_price(t.get('currentPrice'))}): {t.get('rationale')}")
        self.print(f"\nMulti-outcome books scanned: {result['scannedMarkets']} of {result['multiOutcomeMarkets']}")
        rows = [
            {**m, "overround": fmt_price(m.get("overround")), "arbitrage": "YES" if m.get("arbitrage") else "", "market_title": truncate(m.get("market_title"), 50)}
            for m in sorted(result["markets"], key=lambda m: (not m["arbitrage"], -(m.get("overround") or 0)))
        ]
        if rows:
            self.print(table(rows, [("market_id", "MKT"), ("market_title", "MARKET"), ("outcomes", "OUTCOMES"), ("overround", "OVERROUND"), ("arbitrage", "ARB?")]))
        self.print("\n(Read-only: nothing was traded.)")

    def cmd_portfolio(self) -> None:
        ctx = self.context
        if ctx.slug:
            positions = self.client.get_tournament_positions(ctx.slug)
            pnl = self.client.get_tournament_pnl(ctx.slug, period=self.args.period)
        else:
            positions = self.client.get_positions()
            pnl = self.client.get_pnl(period=self.args.period)
        if self.args.json:
            return self.dump({"positions": positions, "pnl": pnl})
        self.header()
        self.print(f"Account value {fmt_num(pnl.get('totalAccountValue'))} · unrealized {fmt_num(pnl.get('unrealizedPnl'))}"
                   f" · period P&L {fmt_num(pnl.get('periodPnl'))} · ROI {fmt_num(pnl.get('roi'))}% · Sharpe {fmt_num(pnl.get('sharpe'))}")
        rows = [
            {
                "market": truncate(p.get("marketTitle"), 40),
                "exch": p.get("exchangeId"),
                "option": p.get("option"),
                "qty": fmt_num(p.get("quantity")),
                "avg": fmt_price(p.get("avgCost")),
                "now": fmt_price(p.get("currentPrice")),
                "value": fmt_num(p.get("marketValue")),
                "upnl": fmt_num(p.get("unrealizedPnl")),
            }
            for p in positions.get("positions") or []
        ]
        if rows:
            self.print(table(rows, [("market", "MARKET"), ("exch", "EXCH"), ("option", "OPTION"), ("qty", "QTY"), ("avg", "AVG COST"), ("now", "PRICE"), ("value", "VALUE"), ("upnl", "UNREAL P&L")]))
        else:
            self.print("No open positions.")

    def cmd_stream(self) -> None:
        from .stream import MarketStream, describe_event, safe_resync_interval

        ctx = self.context
        market_ids = [str(m) for m in self.args.market_ids]
        if not market_ids:
            markets = self.bot().list_markets(status="open", max_items=self.args.max_markets)
            market_ids = [str(m["id"]) for m in markets]
        if not market_ids:
            raise SystemExit("stream: no open markets to follow")
        interval = safe_resync_interval(len(market_ids), self.settings.reads_per_min, self.args.resync_interval)
        if interval > self.args.resync_interval:
            log.warning("resync interval raised to %.0fs to stay inside the read budget", interval)
        log_path = None
        if not self.args.no_log:
            log_path = self.settings.data_dir / ctx.label / f"stream-{utc_now():%Y-%m-%d}.jsonl"

        def on_event(event: Any) -> None:
            if self.args.json:
                self.print(json.dumps(event.to_json(), default=str))
            elif event.kind != "status" or self.args.verbose:
                self.print(describe_event(event))

        stream = MarketStream(self.client, ctx, market_ids, on_event=on_event, resync_interval=interval, log_path=log_path)
        if not self.args.json:
            self.header()
            self.print(f"Streaming {len(market_ids)} market(s); REST resync every {interval:.0f}s. Ctrl-C to stop.")
        asyncio.run(stream.run(self.args.duration))


def _tree_lines(node: Dict[str, Any], depth: int = 0) -> List[str]:
    pad = "  " * depth
    if node.get("node_type") == "operator":
        lines = [f"{pad}{node.get('operator')}"]
        for child in node.get("children") or []:
            lines.extend(_tree_lines(child, depth + 1))
        return lines
    settled = f" → {node['settled_with']}" if node.get("settled_with") else ""
    when = f" (settles {node['settlement_date'][:10]})" if node.get("settlement_date") else ""
    return [f"{pad}• [{node.get('contract_type') or 'contract'}] {node.get('title') or node.get('contract_id')}{when}{settled}"]


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if verbosity < 2:
        for noisy in ("httpx", "httpcore", "realtime", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _safe_console() -> None:
    """Never crash on characters like "Δ" or "→" when output goes to a cp1252 pipe/file on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass


def main(argv: Optional[Sequence[str]] = None, out: Optional[TextIO] = None, transport: Any = None) -> int:
    _safe_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    out = out or sys.stdout
    try:
        overrides: Dict[str, Any] = {}
        if args.data_dir:
            overrides["data_dir"] = args.data_dir
        settings = Settings.load(env_file=Path(args.env_file) if args.env_file else None, **overrides)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = SuperMarketClient.from_settings(settings, transport=transport)
    app = App(args, settings, client, out)
    try:
        getattr(app, f"cmd_{args.command}")()
        return 0
    except KeyboardInterrupt:
        return 130
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.details:
            print(f"details: {json.dumps(exc.details, default=str)}", file=sys.stderr)
        return 1
    except SuperMarketError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
