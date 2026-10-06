"""Command-line interface: ``python -m supermarket_bot <command>``.

Run ``python -m supermarket_bot --help`` for the command list.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from . import __version__
from .books import Book, books_from_market_orderbook, select_context
from .bot import Context, MarketDataBot, market_rows, price_table, resolve_context, utc_now
from .client import SuperMarketClient
from .config import ConfigError, Settings, mask_key
from .display import fmt_num, fmt_price, sparkline, table, truncate
from .errors import ApiError, SuperMarketError, install_log_redaction
from .models import POLICIES, REGIMES

log = logging.getLogger("supermarket_bot")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supermarket_bot",
        description="Read-only market-data bot for the Super Market / Susquehanna Predictions Cup API.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _context_options(p, None)
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v for info logs, -vv for debug")
    # The context options also work after the command ("dashboard --tournament <slug>"): a copy on
    # every subcommand whose defaults are SUPPRESS, so a value given on either side is kept.
    common = argparse.ArgumentParser(add_help=False)
    _context_options(common, argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_parser_with(common))
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

    s = sub.add_parser(
        "dashboard",
        help="web dashboard: tracks every market, flags surges and high-90s, explains them and ranks ideas",
        description="Start the local web dashboard. It tracks all markets in the background and opens in your browser.",
    )
    s.add_argument("--port", type=bounded_int(0, 65535), default=8765, help="port to serve on (default 8765; 0 picks a free one)")
    s.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1, this computer only)")
    s.add_argument("--interval", type=float, default=30.0, help="seconds between price snapshots (default 30)")
    s.add_argument("--demo", action="store_true", help="run on a simulated market (no API key or internet needed)")
    s.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    s.add_argument("--llm", action="store_true", help="also ask Claude to judge surges (needs ANTHROPIC_API_KEY; costs money)")
    s.add_argument("--no-news", action="store_true", help="do not search online news for surges")
    s.add_argument("--no-paper", action="store_true", help="do not run the paper trader (the Simulation view)")
    _fair_value_options(s)
    _simulation_options(s)
    _moves_options(s)

    s = sub.add_parser(
        "paper",
        help="simulate the strategy on live read-only data (paper trading, nothing is traded)",
        description="Run the paper trader without the web server: simulated portfolios on live (or demo) read-only "
                    "data, a summary every hour and an honest verdict at the end. Nothing is ever traded.",
    )
    s.add_argument("--hours", type=positive_float, default=24.0, help="target: hours of covered data (default 24)")
    s.add_argument("--summary-every", type=positive_float, default=60.0, help="minutes between summaries (default 60)")
    s.add_argument("--interval", type=float, default=30.0, help="seconds between price snapshots (default 30)")
    s.add_argument("--reset", action="store_true", help="end the current simulation run and start a new one")
    s.add_argument("--keep-running", action="store_true",
                   help="do not end the run when --hours is reached (live: keep simulating until Ctrl-C)")
    s.add_argument("--demo", action="store_true", help="simulate on the demo market (no API key; a temporary directory)")
    s.add_argument("--fast", action="store_true", help="demo only: run on a simulated clock, as fast as the CPU allows")
    s.add_argument("--step", type=positive_float, default=30.0, help="fast mode: simulated seconds per step (default 30)")
    s.add_argument("--no-news", action="store_true", help="do not search online news for surges")
    _fair_value_options(s)
    _simulation_options(s)

    s = sub.add_parser(
        "backtest",
        help="replay the stored history through the paper trader (offline, read-only)",
        description="Replay the tracker's stored prices, books, trades and fair values through the same signals, "
                    "sizing and fill model as the paper trader. Opens the database read-only; no API key needed.",
    )
    s.add_argument("--store", help="the tracker database (default data/<tournament>/tracker.sqlite3)")
    s.add_argument("--hours", type=positive_float, default=None, help="window length in hours (default 24)")
    s.add_argument("--since", help="window start (ISO-8601, UTC)")
    s.add_argument("--until", help="window end (ISO-8601, UTC; default the newest stored price)")
    s.add_argument("--step", type=positive_float, default=300.0, help="seconds between decisions (default 300)")
    s.add_argument("--latency", type=nonnegative_float, default=60.0, help="seconds before a decision can fill (default 60)")
    s.add_argument("--spread", type=nonnegative_float, default=0.02, help="assumed spread for candle-only quotes (default 0.02)")
    s.add_argument("--touch-qty", type=positive_float, default=100.0, help="shares at the touch of a synthetic book (default 100)")
    s.add_argument("--volume-share", type=positive_float, default=0.1, help="share of candle volume a fill may take (default 0.1)")
    s.add_argument("--candle-cap", type=positive_float, default=50.0, help="shares per candle trade-through fill (default 50)")
    s.add_argument("--no-books", action="store_true", help="ignore stored order-book snapshots")
    s.add_argument("--no-fair-values", action="store_true", help="ignore recorded outside fair values")
    s.add_argument("--no-history", action="store_true", help="ignore imported outside price history")
    s.add_argument("--sweep", action="append", default=[], metavar="NAME=V1,V2",
                   help="re-run with each value of a parameter (repeatable; exploratory only)")
    s.add_argument("--latency-sweep", action="store_true", help="same as --sweep latency_s=30,120,300")
    s.add_argument("--sizing", choices=list(POLICIES), default="conservative", help="sizing policy of the headline")
    s.add_argument("--regime", choices=list(REGIMES), default=None,
                   help="Cup end-settlement assumption (default SUPERMARKET_SETTLEMENT_REGIME or unknown)")
    s.add_argument("--demo", action="store_true", help="first simulate the demo fast into a temporary directory, then replay it")
    s.add_argument("--demo-hours", type=positive_float, default=6.0, help="--demo: hours to simulate first (default 6)")

    s = sub.add_parser(
        "fairvalue",
        help="outside fair values (Polymarket, Kalshi, your file) matched to every outcome",
        description="Read Polymarket and Kalshi anonymously (GET only) and your fair_values.json, match them to every "
                    "open outcome and show the gaps. Nothing is traded.",
    )
    s.add_argument("--demo", action="store_true", help="use the demo market and its scripted outside prices (no API key)")
    s.add_argument("--fair-value", choices=["auto", "manual"], default="auto",
                   help="auto: outside venues and your file; manual: only data/<tournament>/fair_values.json")
    s.add_argument("--template", action="store_true", help="write data/<tournament>/fair_values.json if it does not exist")
    s.add_argument("--only-usable", action="store_true", help="only outcomes with a usable fair value")
    s.add_argument("--show-override", metavar="EID", help="print the fair_value_map.json snippets for one outcome")
    s.add_argument("--import-history", action="store_true",
                   help="import outside price history into the tracker database for backtests (GET only)")
    s.add_argument("--days", type=positive_float, default=7.0, help="--import-history: days to import (default 7)")

    # docs/OUTSIDE_MOVES.md §17 (package "wiring")
    s = sub.add_parser(
        "moves",
        help="watch Polymarket and Kalshi for the Cup's races and flag significant moves (alerts only)",
        description="Track the outside markets matched to every Cup outcome every 15 s (GET only, no account), flag "
                    "significant moves, compare them with the Cup at the same moment, suggest a hand trade when the "
                    "Cup lags, and measure how often and how fast the Cup follows. Nothing is traded.",
    )
    s.add_argument("--demo", action="store_true", help="use the demo market and its scripted outside venues (no API key)")
    s.add_argument("--fast", action="store_true", help="demo only: run on a simulated clock, as fast as the CPU allows")
    s.add_argument("--hours", type=positive_float, default=None,
                   help="stop after this many hours (default: until Ctrl-C; --fast: 2)")
    s.add_argument("--poll", type=float, default=15.0, help="seconds between outside polls (default 15; 5 to 120)")
    s.add_argument("--interval", type=float, default=30.0, help="seconds between Cup price snapshots (default 30)")
    s.add_argument("--book-reads", type=bounded_int(0, 30), default=4,
                   help="Cup order-book reads per minute for the suggested trade's depth (default 4; 0 = none)")
    s.add_argument("--bell", action="store_true", help="ring the terminal bell on each new lagging alert with a suggestion")
    s.add_argument("--only-lagging", action="store_true", help="print only lagging alerts and what became of them")
    s.add_argument("--summary-every", type=positive_float, default=15.0, help="minutes between lag summaries (default 15)")
    s.add_argument("--replay", action="store_true",
                   help="print the stored alerts and the lag study from the database, then exit (offline)")
    s.add_argument("--since", help="--replay: only alerts detected since (ISO-8601, UTC)")
    s.add_argument("--store", help="--replay: the tracker database (default data/<tournament>/tracker.sqlite3)")
    s.add_argument("--no-news", action="store_true", help="do not search online news for surges")
    return p


def _moves_options(s: argparse.ArgumentParser) -> None:
    """The dashboard's outside-move options (docs/OUTSIDE_MOVES.md §17.1)."""
    s.add_argument("--no-moves", action="store_true",
                   help="do not watch Polymarket and Kalshi for outside moves (the Outside moves view)")
    s.add_argument("--moves-poll", type=float, default=15.0,
                   help="seconds between outside polls for move alerts (default 15; 5 to 120)")
    s.add_argument("--moves-book-reads", type=bounded_int(0, 30), default=4,
                   help="Cup order-book reads per minute for alert trade suggestions (default 4; 0 = none)")


def _fair_value_options(s: argparse.ArgumentParser) -> None:
    s.add_argument("--fair-value", choices=["auto", "manual", "off"], default="auto",
                   help="outside fair values: auto (Polymarket, Kalshi and your file; GET only, no account), "
                        "manual (only data/<tournament>/fair_values.json) or off (default auto)")
    s.add_argument("--no-fair-value", action="store_true", help="same as --fair-value off")


def _simulation_options(s: argparse.ArgumentParser) -> None:
    s.add_argument("--regime", choices=list(REGIMES), default=None,
                   help="how the Cup settles unresolved markets at its end: unknown (default; or "
                        "SUPERMARKET_SETTLEMENT_REGIME), resolved_outcomes or vwap_closeout")
    s.add_argument("--sizing", choices=list(POLICIES), default="conservative",
                   help="sizing of the Strategy view and of the simulation's headline (you, by hand): conservative "
                        "(default) or chaser (goal-based, for catching up)")
    s.add_argument("--all-collateral", action="store_true",
                   help="assume D/R pairs share collateral (unknown; off by default)")
    s.add_argument("--paper-capital", type=capital_amount, default=None,
                   help="start capital of every simulated portfolio (default: your account value)")


def _context_options(p: argparse.ArgumentParser, default: Any) -> None:
    """Options shared by the top-level parser and every subcommand (see build_parser)."""

    def d(value: Any) -> Any:
        return value if default is None else default

    p.add_argument("-t", "--tournament", default=d(None),
                   help="tournament slug (default: SUPERMARKET_TOURNAMENT or the only active one)")
    p.add_argument("--public", action="store_true", default=d(False),
                   help="use the public global context instead of a tournament")
    p.add_argument("--json", action="store_true", default=d(False), help="print raw JSON instead of tables")
    p.add_argument("--env-file", default=d(".env"), help="file to read settings from (default: .env)")
    p.add_argument("--data-dir", default=d(None), help="where snapshots and stream logs are written (default: data/)")


def _parser_with(common: argparse.ArgumentParser) -> Any:
    """A subparser class that always inherits ``common``'s options."""

    class _Sub(argparse.ArgumentParser):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["parents"] = [common] + list(kwargs.get("parents") or [])
            super().__init__(*args, **kwargs)

    return _Sub


def positive_int(text: str) -> int:
    return bounded_int(1, None)(text)


def _float_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}")
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"expected a finite number, got {text!r}")
    return value


def positive_float(text: str) -> float:
    value = _float_arg(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def nonnegative_float(text: str) -> float:
    value = _float_arg(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {text}")
    return value


def capital_amount(text: str) -> float:
    value = _float_arg(text)
    if not (1 <= value <= 10_000_000):
        raise argparse.ArgumentTypeError(f"must be between 1 and 10,000,000, got {text}")
    return value


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
        # ASCII-escaped so no character is lost on a cp1252 console or pipe
        self.print(json.dumps(data, indent=2, ensure_ascii=True, default=str))

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

        if not self.args.json:
            self.header()
            self.print(f"Streaming {len(market_ids)} market(s); REST resync every {interval:.0f}s. Ctrl-C to stop.")

        async def run_stream() -> None:
            # Built inside the running loop so asyncio objects bind to it (Python 3.9).
            stream = MarketStream(self.client, ctx, market_ids, on_event=on_event, resync_interval=interval, log_path=log_path)
            await stream.run(self.args.duration)

        asyncio.run(run_stream())


# --------------------------------------------------------------------------- simulation commands
# ``paper``, ``backtest`` and ``fairvalue`` (docs/PAPER_TRADING.md §7.5). ``paper --demo``, ``fairvalue --demo`` and
# every ``backtest`` need no API key. Exit codes: 0 ok, 1 API/OS errors, 2 usage/config errors and a lease
# conflict (another process tracks the same database), 130 Ctrl-C. Every client is GET-only (readonly.py).

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 130
_HORIZON_LABELS = {"5m": "+5 min", "30m": "+30 min", "2h": "+2 h", "6h": "+6 h"}


class UsageError(Exception):
    """A command-line mistake (exit 2)."""


def _say(stream: TextIO, lines: Sequence[str]) -> None:
    for line in lines:
        print(line, file=stream, flush=True)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _utc_text(ts: Any) -> str:
    value = _num(ts)
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _signed(value: Any, fmt: str = "+.3f") -> str:
    v = _num(value)
    return format(v, fmt) if v is not None else "—"


def _share(value: Any) -> str:
    v = _num(value)
    return f"{v:.0%}" if v is not None else "n/a"


def _json_safe(payload: Any) -> Any:
    """``payload`` as plain JSON (NaN/inf -> null, dataclasses -> dicts), like the dashboard sends it."""
    from .web import encode_json

    return json.loads(encode_json(payload).decode("utf-8"))


def _dump_json(out: TextIO, payload: Any) -> None:
    print(json.dumps(_json_safe(payload), indent=2, ensure_ascii=True, sort_keys=True), file=out, flush=True)


def study_lines(study: Any, horizon: str = "30m") -> List[str]:
    """One line per event-study kind at ``horizon`` (§6.14)."""
    if not isinstance(study, Mapping):
        return []
    lines: List[str] = []
    kinds = study.get("kinds") or {}
    order = [k for k in ("value", "basket") if k in kinds] + sorted(k for k in kinds if k not in ("value", "basket"))
    for kind in order:
        info = kinds.get(kind) or {}
        h = (info.get("horizons") or {}).get(horizon) or {}
        label = _HORIZON_LABELS.get(horizon, horizon)
        events = int(info.get("events") or 0)
        if _num(h.get("mean")) is None:
            lines.append(f"  Signal study ({kind}, {label}): {events} signal{'' if events == 1 else 's'}, no outcome yet")
            continue
        lo, hi = _num(h.get("ci_low")), _num(h.get("ci_high"))
        level = _num(h.get("ci_level")) or 0.9
        ci = (f"({level:.0%}: {lo:+.3f} to {hi:+.3f})" if lo is not None and hi is not None
              else "(too few signals for an interval)")
        lines.append(
            f"  Signal study ({kind}, {label}): {events} signal{'' if events == 1 else 's'}, mean {_signed(h.get('mean'))} "
            f"per share {ci}, converged {_share(h.get('share_converged'))}, reversed {_share(h.get('share_reversed'))}"
        )
    return lines


def paper_summary_lines(body: Mapping[str, Any]) -> List[str]:
    """The ``paper`` command's periodic summary (§7.5)."""
    run = body.get("run") if isinstance(body.get("run"), Mapping) else {}
    budget = body.get("budget") if isinstance(body.get("budget"), Mapping) else {}
    hours = _num(run.get("hours_run")) or 0.0
    target = _num(run.get("target_hours")) or 0.0
    wall = _num(run.get("wall_hours")) or 0.0
    lines = [
        f"[paper] {_utc_text(body.get('now'))}  {hours:.1f} h observed of {target:g} h (wall {wall:.1f} h)  "
        f"steps {int(run.get('steps') or 0)}  reads {int(budget.get('reads_used') or 0)}/{int(budget.get('reads_limit') or 0)} per min"
    ]
    if str(run.get("capital_source") or "").startswith("default"):
        # ui-4: the account was not known before something filled, so the run kept the default capital
        lines.append(f"  Start capital: the default {_num(run.get('start_capital')) or 100_000:,.0f} (your account value was not "
                     "known when this run started; reset it to start on your account value)")
    for p in body.get("portfolios") or []:
        if not isinstance(p, Mapping):
            continue
        pnl, pct = _num(p.get("pnl_liq")) or 0.0, _num(p.get("pnl_liq_pct")) or 0.0
        legging = int(p.get("legging_trades") or 0)
        lines.append(
            f"  {p.get('label') or p.get('portfolio_id')}: {pnl:+,.0f} ({pct:+.2%}) at liquidation  fills {int(p.get('fills') or 0)}  "
            f"closed {int(p.get('trades_closed') or 0)}  open {int(p.get('positions_open') or 0)}"
            # ui-7: legging exits of sets still held are not closed ideas
            + (f"  (+{legging} legging exit{'s' if legging != 1 else ''} {_num(p.get('legging_pnl')) or 0.0:+,.0f}, not closed ideas)"
               if legging > 0 else "")
        )
    head = body.get("headline") if isinstance(body.get("headline"), Mapping) else None
    verdict = head.get("verdict") if head and isinstance(head.get("verdict"), Mapping) else None
    if head and verdict:
        lines.append(f"  Verdict ({head.get('label') or head.get('portfolio_id')}): {verdict.get('sentence')}")
    lines.extend(study_lines(body.get("study")))
    return lines


def paper_final_lines(body: Mapping[str, Any]) -> List[str]:
    """The summary plus every verdict, the table warning and the verdict caveats (§7.5)."""
    lines = paper_summary_lines(body)
    run = body.get("run") if isinstance(body.get("run"), Mapping) else {}
    if run.get("end_reason"):
        lines.append(f"  Run {run.get('run_id')} ended ({run.get('end_reason')}).")
    settled = [t for t in body.get("trades") or [] if isinstance(t, Mapping) and t.get("exit_reason") == "settled"]
    if settled:
        titles: List[str] = []
        for t in settled:
            title = str(t.get("title") or ", ".join(str(e) for e in t.get("exchange_ids") or []))
            if title and title not in titles:
                titles.append(title)
        n_pf = len({t.get("portfolio_id") for t in settled})
        lines.append(f"  Settled while running: {len(settled)} position{'' if len(settled) == 1 else 's'} in "
                     f"{n_pf} portfolio{'' if n_pf == 1 else 's'} ({'; '.join(titles)}).")
    lines.append("Verdicts (one pre-registered headline; every other portfolio is exploratory):")
    for p in body.get("portfolios") or []:
        v = p.get("verdict") if isinstance(p, Mapping) and isinstance(p.get("verdict"), Mapping) else None
        if v is None:
            continue
        tag = "headline" if p.get("headline") else "exploratory"
        lines.append(f"  {p.get('label') or p.get('portfolio_id')} ({tag}): {v.get('level')}: {v.get('sentence')}")
        if p.get("no_trade_reason"):
            lines.append(f"    No trades: {p['no_trade_reason']}")
    if body.get("table_warning"):
        lines.append(str(body["table_warning"]))
    study = body.get("study") if isinstance(body.get("study"), Mapping) else None
    if study:
        for key in ("can_show", "cannot_show"):
            if study.get(key):
                lines.append(str(study[key]))
    head = body.get("headline") if isinstance(body.get("headline"), Mapping) else None
    caveats = list((head or {}).get("verdict_caveats") or [])
    if caveats:
        lines.append("Read the verdict with these in mind:")
        lines.extend(f"  - {c}" for c in caveats)
    extra = [c for c in body.get("caveats") or [] if c not in caveats]
    if extra:
        lines.append("Also:")
        lines.extend(f"  - {c}" for c in extra)
    return lines


def _settings_for(args: argparse.Namespace) -> Settings:
    overrides: Dict[str, Any] = {}
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = args.data_dir
    return Settings.load(env_file=Path(args.env_file) if args.env_file else None, **overrides)


def _plain_env(args: argparse.Namespace) -> Dict[str, str]:
    """Environment plus the --env-file (no API key needed): for commands that only read local files."""
    from .config import load_env_file

    merged: Dict[str, str] = {}
    if getattr(args, "env_file", None):
        merged.update(load_env_file(Path(args.env_file)))
    merged.update(os.environ)
    return {k: v.strip() for k, v in merged.items() if isinstance(v, str) and v.strip()}


def _guarded(fn: Callable[[], int], err: TextIO) -> int:
    """Run a simulation command and map failures to exit codes (§7.5)."""
    from .tracker import TrackerBusy

    try:
        return fn()
    except KeyboardInterrupt:
        print("\nStopped.", file=err, flush=True)
        return EXIT_INTERRUPTED
    except TrackerBusy as exc:
        print(f"error: {exc}", file=err, flush=True)
        return EXIT_USAGE
    except (ConfigError, UsageError) as exc:
        print(f"error: {exc}", file=err, flush=True)
        return EXIT_USAGE
    except ApiError as exc:
        print(f"error: {exc}", file=err, flush=True)
        if exc.details:
            print(f"details: {json.dumps(exc.details, default=str)}", file=err)
        return EXIT_ERROR
    except SuperMarketError as exc:
        print(f"error: {exc}", file=err, flush=True)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error: {exc}", file=err, flush=True)
        return EXIT_ERROR


# ------------------------------------------------------------------ paper


def cmd_paper(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    return _guarded(lambda: _paper(args, out, err), err)


def _paper(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from . import pipeline, web
    from .demo import SIM_T0, SimClock

    if args.fast and not args.demo:
        raise UsageError("--fast needs --demo (a live simulation runs in real time)")
    if not math.isfinite(args.interval) or args.interval < 1:
        raise UsageError("--interval must be at least 1 second")
    import threading

    temp_dir: Optional[str] = None
    runtime: Any = None
    # SIGTERM and SIGHUP (the terminal window was closed) stop like Ctrl-C: the tracker stops and releases its
    # lease, so starting again at once continues the run (live-5)
    previous_handlers = web._install_sigterm(threading.Event())
    try:
        if args.demo:
            if args.data_dir:
                data_dir = Path(args.data_dir)
            else:  # never the dashboard's data/demo: a running `dashboard --demo` keeps its database (D46)
                temp_dir = tempfile.mkdtemp(prefix="supermarket-paper-")
                data_dir = Path(temp_dir)
            clock = SimClock(SIM_T0) if args.fast else None
            runtime = web.build_demo(data_dir, args.interval, news=not args.no_news, out=err, clock=clock,
                                     target_hours=args.hours, **web.simulation_options(args))
        else:
            runtime = web.build_live(_settings_for(args), args, out=err)
        tracker = runtime.tracker
        if getattr(tracker, "paper_runner", None) is None:
            print(f"error: {getattr(tracker, 'paper_error', None) or 'The paper trader is not available.'}", file=err)
            return EXIT_ERROR
        if args.fast:
            return _paper_fast(runtime, args, out, err, pipeline)
        return _paper_live(runtime, args, out, err)
    finally:
        try:
            if runtime is not None:
                runtime.close()
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            web._restore_sigterm(previous_handlers)


def _paper_capital(args: argparse.Namespace) -> Optional[float]:
    value = getattr(args, "paper_capital", None)
    return float(value) if value else None


def _paper_fast(runtime: Any, args: argparse.Namespace, out: TextIO, err: TextIO, pipeline: Any) -> int:
    tracker = runtime.tracker
    clock = runtime.clock
    if not args.json:
        print(f"Fast demo simulation: {args.hours:g} simulated hours in {args.step:g}-second steps "
              "(scripted demo data; nothing is traded).", file=out, flush=True)
    if args.reset:
        tracker.paper_reset(start_capital=_paper_capital(args), target_hours=args.hours)

    def on_summary(body: Mapping[str, Any]) -> None:
        if not args.json:
            _say(out, paper_summary_lines(body))

    pipeline.run_simulation(runtime, clock, hours=args.hours, step_s=args.step, summary_every_s=args.summary_every * 60.0,
                            on_summary=on_summary, end_run=not args.keep_running)
    body = runtime.app.paper()
    if args.json:
        _dump_json(out, body)
    else:
        _say(out, paper_final_lines(body))
    return EXIT_OK


def _paper_live(runtime: Any, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Real time (live, or the demo without --fast): the tracker's threads, no web server."""
    tracker = runtime.tracker
    runtime.start()  # TrackerBusy (exit 2) when another process tracks this database
    if args.reset:
        tracker.paper_reset(start_capital=_paper_capital(args), target_hours=args.hours)
    if not args.json:
        what = "the demo market (scripted data)" if args.demo else "live read-only data"
        until = "until Ctrl-C" if args.keep_running else f"until {args.hours:g} covered hours"
        print(f"Paper trading on {what} {until}; a summary every {args.summary_every:g} min. Nothing is traded.",
              file=out, flush=True)
    every = float(args.summary_every) * 60.0
    next_summary = time.monotonic() + every
    reached = interrupted = False
    try:
        while True:
            body = tracker.paper_view() or {}
            run = body.get("run") if isinstance(body.get("run"), Mapping) else {}
            hours = _num(run.get("hours_run")) or 0.0
            reached = hours >= float(args.hours) - 1e-9
            if reached and not args.keep_running:
                break
            if not tracker.running:
                fatal = (tracker.status() or {}).get("fatal_error")
                print(f"error: the tracker stopped: {fatal or 'see the log'}", file=err, flush=True)
                return EXIT_ERROR
            if time.monotonic() >= next_summary:
                if not args.json:
                    _say(out, paper_summary_lines(body))
                next_summary += every
            time.sleep(1.0)
    except KeyboardInterrupt:
        interrupted = True
    if reached and not args.keep_running:
        tracker.paper_end("completed")
    try:
        if interrupted:
            print("\nStopping the simulation…", file=err, flush=True)
        if not (reached and not args.keep_running) and not args.json:
            print("The run stays open: the next `paper` or `dashboard` on this database continues it.", file=out,
                  flush=True)
        body = runtime.app.paper()
        if args.json:
            _dump_json(out, body)
        else:
            _say(out, paper_final_lines(body))
    except (OSError, ValueError):
        if not interrupted:
            raise
        # after SIGHUP the terminal is gone (writes fail): the run is still stopped cleanly by the caller
    return EXIT_INTERRUPTED if interrupted else EXIT_OK


# ------------------------------------------------------------------ backtest


def cmd_backtest(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    return _guarded(lambda: _backtest(args, out, err), err)


def _iso_arg(text: Optional[str], name: str) -> Optional[float]:
    from .books import parse_time

    if not text:
        return None
    parsed = parse_time(text)
    if parsed is None:
        raise UsageError(f"{name}: not an ISO-8601 time: {text!r}")
    return parsed.timestamp()


def backtest_store_path(args: argparse.Namespace) -> Path:
    """``--store``, else data/<slug>/tracker.sqlite3 (slug: --tournament, SUPERMARKET_TOURNAMENT, or the only
    data/*/tracker.sqlite3 except demo)."""
    if getattr(args, "store", None):
        path = Path(args.store).expanduser()
        if not path.is_file():
            raise UsageError(f"{path}: no such database")
        return path
    env = _plain_env(args)
    data_dir = Path(args.data_dir or env.get("SUPERMARKET_DATA_DIR") or "data")
    slug = getattr(args, "tournament", None) or env.get("SUPERMARKET_TOURNAMENT")
    if slug:
        path = data_dir / slug / "tracker.sqlite3"
        if not path.is_file():
            raise UsageError(f"{path}: no such database (run the dashboard or `paper` first, or pass --store)")
        return path
    found = sorted(p for p in data_dir.glob("*/tracker.sqlite3") if p.parent.name != "demo")
    if len(found) == 1:
        return found[0]
    if not found:
        raise UsageError(f"no tracker database under {data_dir}/ (run the dashboard or `paper` first, pass --store, "
                         "or use --demo)")
    names = ", ".join(p.parent.name for p in found)
    raise UsageError(f"several tracker databases under {data_dir}/ ({names}): pick one with --tournament or --store")


def _backtest(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from . import backtest as bt
    from . import pipeline, web
    from .demo import SIM_T0, SimClock
    from .models import BacktestConfig, PaperConfig, default_portfolios
    from .store import TrackerStore

    regime = web.regime_option(args)
    specs = list(args.sweep or []) + (["latency_s=30,120,300"] if args.latency_sweep else [])
    try:
        grid = bt.parse_sweep(specs)
    except ValueError as exc:
        raise UsageError(str(exc))
    temp_dir: Optional[str] = None
    try:
        if args.demo:
            temp_dir = tempfile.mkdtemp(prefix="supermarket-backtest-")
            clock = SimClock(SIM_T0)
            if not args.json:
                print(f"Simulating the demo market for {args.demo_hours:g} h (fast, scripted data) before the replay…",
                      file=out, flush=True)
            runtime = web.build_demo(Path(temp_dir), 30.0, news=False, out=err, clock=clock, regime=regime,
                                     sizing=args.sizing)
            try:
                pipeline.run_simulation(runtime, clock, hours=args.demo_hours, step_s=30.0, summary_every_s=0.0,
                                        end_run=True)
            finally:
                runtime.close()
            path = Path(temp_dir) / "demo" / "tracker.sqlite3"
            hours = args.hours if args.hours is not None else float(args.demo_hours)
        else:
            path = backtest_store_path(args)
            hours = args.hours if args.hours is not None else 24.0
        config = BacktestConfig(
            start=_iso_arg(args.since, "--since"), end=_iso_arg(args.until, "--until"), hours=float(hours),
            step_s=float(args.step), latency_s=float(args.latency), assumed_spread=float(args.spread),
            assumed_touch_qty=float(args.touch_qty), volume_share=float(args.volume_share),
            use_book_snapshots=not args.no_books, use_fair_values=not args.no_fair_values,
            candle_fill_cap=float(args.candle_cap), use_history_fair_values=not args.no_history,
            paper=PaperConfig(sizing=args.sizing, regime=regime, portfolios=default_portfolios(args.sizing), demo=bool(args.demo)),
            label="demo" if args.demo else "",
        )
        try:
            source = TrackerStore.open_read_only(path)  # a second, read-only connection: never writes (D52)
        except RuntimeError as exc:  # an older schema
            raise UsageError(str(exc))
        try:
            report = bt.sweep(source, config, grid) if grid else bt.run_backtest(source, config)
            # lookahead-1: the overlap with every stored paper run (for --demo: its own simulation, a paper run
            # over exactly this window), not only a live run's start
            pipeline.apply_paper_overlap(report, pipeline.paper_run_intervals(source, now=time.time()))
        except ValueError as exc:
            raise UsageError(str(exc))
        finally:
            source.close()
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    if args.json:
        _dump_json(out, report)
    else:
        _say(out, backtest_lines(report, path=None if args.demo else path))
    return EXIT_OK


def backtest_lines(report: Any, path: Optional[Path] = None) -> List[str]:
    """The backtest report as text: the testability table FIRST, then window, coverage, assumptions, one row per
    portfolio, the headline verdict, the event study, sweep rows and warnings (§7.5)."""
    data = _json_safe(report)
    lines: List[str] = ["What this replay can test (per idea kind):"]
    testability = data.get("testability") or {}
    for kind, info in testability.items():
        info = info or {}
        status = str(info.get("status") or "").replace("_", " ")
        hours = _num(info.get("hours"))
        lines.append(f"  {kind:<10} {status:<15} {f'{hours:.1f} h' if hours is not None else '':>7}  {info.get('sentence') or ''}")
    if not testability:
        lines.append("  (nothing to test: no stored history in the window)")
    window = data.get("window") or {}
    where = f" from {path}" if path is not None else ""
    lines.append(f"Window{where}: {_utc_text(window.get('start'))} to {_utc_text(window.get('end'))} "
                 f"({fmt_num(window.get('hours'))} h, {int(window.get('steps') or 0)} decision steps)")
    cov = data.get("coverage") or {}
    if cov:
        parts = []
        for key, label in (("tick_share", "tick quotes"), ("candle_share", "candle quotes"),
                           ("book_snapshot_share", "real books"), ("synthetic_book_share", "synthetic books"),
                           ("fair_value_share", "fair values"), ("tape_share", "trade tape")):
            if _num(cov.get(key)) is not None:
                parts.append(f"{label} {_share(cov.get(key))}")
        if parts:
            lines.append("Coverage: " + ", ".join(parts))
    if data.get("assumptions"):
        lines.append("Assumptions:")
        lines.extend(f"  - {a}" for a in data["assumptions"])
    rows = []
    head_id = None
    for p in data.get("portfolios") or []:
        v = p.get("verdict") or {}
        if p.get("headline"):
            head_id = p.get("portfolio_id")
        rows.append({
            "portfolio": truncate(p.get("label") or p.get("portfolio_id"), 48),
            "pnl": f"{(_num(p.get('pnl_liq')) or 0.0):+,.0f}",
            "ideas": v.get("n_ideas") if v else "",
            "win": _share(p.get("win_rate")) if p.get("win_rate") is not None else "—",
            "dd": _share(p.get("max_drawdown")),
            "level": v.get("level") or "",
        })
    if rows:
        lines.append(table(rows, [("portfolio", "PORTFOLIO"), ("pnl", "P&L AT LIQUIDATION"), ("ideas", "IDEAS"),
                                  ("win", "WIN RATE (CLOSED ONLY)"), ("dd", "DRAWDOWN"), ("level", "VERDICT")]))
    verdicts = data.get("verdicts") or {}
    if head_id and head_id in verdicts:
        lines.append(f"Headline verdict ({head_id}): {verdicts[head_id].get('sentence')}")
    lines.extend(_backtest_caveat_lines(data, verdicts.get(head_id) if head_id else None, len(rows)))
    study = study_lines(data.get("study"))
    if study:
        lines.append("Signal study:")
        lines.extend(study)
    for row in data.get("sweep") or []:
        lines.append(f"  sweep {row.get('label')}: {(_num(row.get('pnl_liq')) or 0.0):+,.0f} at liquidation, "
                     f"{int(row.get('trades_closed') or 0)} closed, verdict {row.get('verdict_level')}")
    for w in data.get("warnings") or []:
        lines.append(f"Warning: {w}")
    if data.get("stopped_early"):
        lines.append("Warning: the replay stopped early (time budget).")
    return lines


def _backtest_caveat_lines(data: Mapping[str, Any], verdict: Optional[Mapping[str, Any]], n_portfolios: int) -> List[str]:
    """lookahead-5: what ``paper`` prints with its verdict, for the backtest text too: the multi-portfolio table
    warning, the headline verdict's caveats (the ones shown next to a verdict) and, for demo data, DEMO_CAVEAT
    (the demo's outside prices lead the Cup by design, so a "value" result there is built-in look-ahead)."""
    from .paper import CAVEATS, DEMO_CAVEAT, TABLE_WARNING, VERDICT_CAVEATS

    config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
    paper_cfg = config.get("paper") if isinstance(config.get("paper"), Mapping) else {}
    own = [str(c) for c in (verdict or {}).get("caveats") or []]
    demo = bool(paper_cfg.get("demo")) or str(config.get("label") or "") == "demo" or DEMO_CAVEAT in own
    lines: List[str] = []
    if demo:
        lines.append(f"Note: {DEMO_CAVEAT}")
    if n_portfolios > 1:
        lines.append(TABLE_WARNING.format(n=n_portfolios))
    shown = [CAVEATS[i] for i in VERDICT_CAVEATS]
    caveats = [c for c in shown if c in own] if own else []
    if caveats:
        lines.append("Read the verdict with these in mind:")
        lines.extend(f"  - {c}" for c in caveats)
    return lines


# ------------------------------------------------------------------ fairvalue


# ------------------------------------------------------------------ moves (docs/OUTSIDE_MOVES.md §17)


def cmd_moves(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """``python -m supermarket_bot moves`` (docs/OUTSIDE_MOVES.md §17): the tracker headless with the outside-move
    watcher (no paper trader, no web server), one block of lines per alert event (moves.format_event_lines), a
    summary every --summary-every minutes, the final lag study; or --replay from the database. Package "wiring"."""
    return _guarded(lambda: _moves(args, out, err), err)


def _moves_poll(args: argparse.Namespace) -> float:
    from .moves import MOVES_POLL_MAX_S, MOVES_POLL_MIN_S

    poll = _num(getattr(args, "poll", None))
    if poll is None or not MOVES_POLL_MIN_S <= poll <= MOVES_POLL_MAX_S:
        raise UsageError(f"--poll must be between {MOVES_POLL_MIN_S:g} and {MOVES_POLL_MAX_S:g} seconds")
    return poll


def _moves(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    from . import pipeline, web
    from .demo import SIM_T0, SimClock

    if args.fast and not args.demo:
        raise UsageError("--fast needs --demo")
    poll = _moves_poll(args)
    if not math.isfinite(args.interval) or args.interval < 1:
        raise UsageError("--interval must be at least 1 second")
    if args.replay:
        if args.demo or args.fast:
            raise UsageError("--replay reads a stored database: leave out --demo and --fast")
        return _moves_replay(args, out)
    if args.since or args.store:
        raise UsageError("--since and --store are for --replay")
    hours = args.hours if args.hours is not None else (2.0 if args.fast else None)
    temp_dir: Optional[str] = None
    runtime: Any = None
    previous_handlers = web._install_sigterm(threading.Event())  # SIGTERM/SIGHUP stop like Ctrl-C (live-5)
    try:
        if args.demo:
            if args.data_dir:
                data_dir = Path(args.data_dir)
            else:  # never the dashboard's data/demo: a running `dashboard --demo` keeps its database (D46)
                temp_dir = tempfile.mkdtemp(prefix="supermarket-moves-")
                data_dir = Path(temp_dir)
            clock = SimClock(SIM_T0) if args.fast else None
            runtime = web.build_demo(data_dir, args.interval, news=not args.no_news, out=err, clock=clock, paper=False,
                                     fair_value="auto", moves=True, moves_poll_s=poll,
                                     moves_book_reads_per_min=args.book_reads)
        else:
            args.no_paper, args.no_moves = True, False  # the watcher, never the paper trader
            args.fair_value, args.no_fair_value = "auto", False  # the watcher polls the validated outside matches
            args.moves_poll, args.moves_book_reads = poll, args.book_reads
            runtime = web.build_live(_settings_for(args), args, out=err)
        watcher = getattr(runtime, "moves", None) or getattr(runtime.tracker, "moves", None)
        if watcher is None:
            print("error: the outside-move watcher is not available in this build.", file=err, flush=True)
            return EXIT_ERROR
        printer = _MovesPrinter(args, out, demo=bool(args.demo))
        watcher.add_listener(printer.event)
        printer.start_lines(watcher, temp_dir)
        if args.fast:
            return _moves_fast(runtime, watcher, printer, args, hours, poll, pipeline)
        return _moves_realtime(runtime, watcher, printer, args, hours, err)
    finally:
        try:
            if runtime is not None:
                runtime.close()
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            web._restore_sigterm(previous_handlers)


class _MovesPrinter:
    """Prints the ``moves`` command's events and summaries (text or one JSON object per line). Events arrive from
    the watcher's thread in real time and summaries from the main thread: one lock keeps their lines together."""

    def __init__(self, args: argparse.Namespace, out: TextIO, demo: bool) -> None:
        self.args = args
        self.out = out
        self.demo = demo
        self.json = bool(getattr(args, "json", False))
        self.bell = bool(getattr(args, "bell", False))
        self.only_lagging = bool(getattr(args, "only_lagging", False))
        self.lagging_ids: set = set()  # alerts that were lagging when they opened (--only-lagging)
        self.events = 0
        self._lock = threading.Lock()

    def _emit(self, lines: Sequence[str]) -> None:
        with self._lock:
            for line in lines:
                print(line, file=self.out, flush=True)

    def start_lines(self, watcher: Any, temp_dir: Optional[str]) -> None:
        from .moves import VENUE_LABELS

        poll = float(getattr(watcher, "poll_s", 15.0) or 15.0)
        path = getattr(watcher, "alerts_path", None)
        if self.json:
            return
        if self.demo:
            labels = ", ".join(VENUE_LABELS[v] for v in ("demo-a", "demo-b"))
            lines = [f"Watching the demo's scripted outside venues ({labels}) every {poll:g} s. Demo data: the moves are "
                     "scripted. Nothing is traded."]
            if path is not None:
                lines.append(f"Alerts are written to {path} (a temporary folder removed at exit; use --data-dir to keep it)."
                             if temp_dir is not None else f"Alerts are written to {path}.")
        else:
            matched = _moves_matched(watcher)
            what = f"for {matched} matched outcomes" if matched else "for every matched outcome"
            where = f" Alerts also go to {path}." if path is not None else ""
            lines = [f"Watching outside prices: Polymarket and Kalshi every {poll:g} s {what} (GET only, no account).{where} "
                     "Nothing is traded. Ctrl-C to stop."]
        self._emit(lines)

    def event(self, event: Any) -> None:
        from .moves import format_event_lines

        kind = getattr(event, "kind", "")
        alert = getattr(event, "alert", None) or {}
        alert_id = getattr(event, "alert_id", None) or alert.get("alert_id")
        if kind == "opened" and (alert.get("opened_status") or getattr(event, "status", alert.get("status"))) == "lagging":
            self.lagging_ids.add(alert_id)
        if self.only_lagging and alert_id not in self.lagging_ids:
            return
        self.events += 1
        if self.json:
            self._emit([json.dumps(_json_safe({"type": "event", "event": kind, "alert": alert}), ensure_ascii=True,
                                   sort_keys=True)])
            return
        if kind == "closed":
            return  # closed events are in alerts.jsonl and --json only
        self._emit(format_event_lines(event, bell=self.bell))

    def summary(self, body: Optional[Mapping[str, Any]], now: float, *, final: bool) -> None:
        from .moves import MOVES_CAVEATS, MOVES_DEMO_CAVEAT

        body = body if isinstance(body, Mapping) else {}
        lag = body.get("summary") if isinstance(body.get("summary"), Mapping) else {}
        venues = [v for v in body.get("venues") or [] if isinstance(v, Mapping)]
        caveats = list(body.get("caveats") or MOVES_CAVEATS)
        if self.demo and MOVES_DEMO_CAVEAT not in caveats:
            caveats.append(MOVES_DEMO_CAVEAT)
        if self.json:
            payload: Dict[str, Any] = {"type": "summary", "at": now, "summary": dict(lag), "venues": venues,
                                       "counts": body.get("counts")}
            if final:
                payload.update(final=True, caveats=caveats)
            self._emit([json.dumps(_json_safe(payload), ensure_ascii=True, sort_keys=True)])
            return
        lines = moves_summary_lines(body, now)
        if final:
            from .moves import lag_table_lines

            lines += lag_table_lines(lag)  # the numbers and their sample sizes behind the sentence
            lines += [f"  - {c}" for c in caveats]
        self._emit(lines)


def moves_summary_lines(body: Mapping[str, Any], now: float) -> List[str]:
    """``moves.summary_lines`` for the watcher's published summary body (or a replay's ``{"summary": LagSummary}``):
    the lag sentence, the venue line and the filtered-out counts (§17.2)."""
    from .moves import summary_lines

    lag = body.get("summary") if isinstance(body.get("summary"), Mapping) else {}
    venues = [v for v in body.get("venues") or [] if isinstance(v, Mapping)]
    return list(summary_lines({**body, **lag}, venues, now))


def _moves_matched(watcher: Any) -> Optional[int]:
    try:
        watching = (watcher.summary() or {}).get("watching") or {}
    except Exception:
        return None
    matched = watching.get("matched") if isinstance(watching, Mapping) else None
    return int(matched) if isinstance(matched, int) and matched > 0 else None


def _moves_fast(runtime: Any, watcher: Any, printer: _MovesPrinter, args: argparse.Namespace, hours: float, poll: float,
                pipeline: Any) -> int:
    clock = runtime.clock
    tracker = runtime.tracker
    interrupted = False
    end_at = float(clock()) + float(hours) * 3600.0

    def on_summary(body: Mapping[str, Any]) -> None:
        if float(clock()) >= end_at - 1e-9:
            return  # the final block (with the caveats) follows at the same time: print it once
        printer.summary(tracker.moves_view(), float(clock()), final=False)

    try:
        pipeline.run_simulation(runtime, clock, hours=hours, step_s=args.interval, summary_every_s=args.summary_every * 60.0,
                                on_summary=on_summary, moves_every_s=poll, end_run=False)
    except KeyboardInterrupt:
        interrupted = True
    printer.summary(tracker.moves_view(), float(clock()), final=True)
    return EXIT_INTERRUPTED if interrupted else EXIT_OK


def _moves_realtime(runtime: Any, watcher: Any, printer: _MovesPrinter, args: argparse.Namespace, hours: Optional[float],
                    err: TextIO) -> int:
    """Real time (live, or the demo without --fast): the tracker's threads (snapshots, fair values, the watcher)."""
    from .fairvalue import OUTSIDE_READS_PER_MIN_WITH_MOVES

    tracker = runtime.tracker
    runtime.start()  # TrackerBusy (exit 2) when another process tracks this database
    clock = getattr(tracker, "_clock", None) or time.time
    every = float(args.summary_every) * 60.0
    started = time.monotonic()
    next_summary = started + every
    deadline = started + hours * 3600.0 if hours else None
    warned = bool(args.demo)
    interrupted = False
    try:
        while deadline is None or time.monotonic() < deadline:
            if not tracker.running:
                fatal = (tracker.status() or {}).get("fatal_error")
                print(f"error: the tracker stopped: {fatal or 'see the log'}", file=err, flush=True)
                printer.summary(tracker.moves_view(), float(clock()), final=True)
                return EXIT_ERROR
            if not warned:
                warned = _moves_budget_warning(watcher, float(watcher.poll_s), OUTSIDE_READS_PER_MIN_WITH_MOVES, err,
                                               time.monotonic() - started)
            if time.monotonic() >= next_summary:
                printer.summary(tracker.moves_view(), float(clock()), final=False)
                next_summary += every
            time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
    try:
        if interrupted:
            print("\nStopping the outside-move watcher…", file=err, flush=True)
        printer.summary(tracker.moves_view(), float(clock()), final=True)
    except (OSError, ValueError):
        if not interrupted:
            raise  # after SIGHUP the terminal is gone: the watcher still stops cleanly
    return EXIT_INTERRUPTED if interrupted else EXIT_OK


def _moves_budget_warning(watcher: Any, poll: float, limit: int, err: TextIO, waited: float) -> bool:
    """The §4.3 start-up warning once the Polymarket match count is known: True when done (warned or not needed)."""
    try:
        venues = (watcher.summary() or {}).get("venues") or []
    except Exception:
        venues = []
    poly = next((v for v in venues if isinstance(v, Mapping) and v.get("venue") == "polymarket"), None)
    matched = poly.get("matched") if isinstance(poly, Mapping) else None
    if not isinstance(matched, int) or matched <= 0:
        return waited > 300.0  # nothing matched yet (or no Polymarket): stop checking after 5 minutes
    reads = math.ceil(matched / 50) * 60.0 / poll + 5
    if reads > limit - 9:  # the poll keeps 8 + 1 slots for the fair-value refresh (§4.2)
        print(f"warning: A {poll:g}-second poll needs about {reads:.0f} Polymarket reads a minute, more than the {limit} "
              "this bot allows itself: some polls will be skipped.", file=err, flush=True)
    return True


def _moves_replay(args: argparse.Namespace, out: TextIO) -> int:
    """``moves --replay``: the stored alerts and the lag study from the database (read-only, offline, no key)."""
    from .moves import MOVES_ALERTS_FILE, MOVES_DEMO_CAVEAT, MoveEvent, format_event_lines, summarise
    from .store import TrackerStore

    path = backtest_store_path(args)
    since = _iso_arg(args.since, "--since")
    try:
        source = TrackerStore.open_read_only(path)
    except RuntimeError as exc:  # an older schema
        raise UsageError(str(exc))
    try:
        alerts = source.move_alerts(since=since, limit=100_000)
    finally:
        source.close()
    if args.only_lagging:
        alerts = [a for a in alerts if _was_lagging(a)]
    times = [t for a in alerts for t in (_num(a.get("updated_at")), _num(a.get("detected_at"))) if t is not None]
    now = max(times) if times else time.time()
    lag = summarise(alerts, now).to_dict()
    demo = any(bool(a.get("demo")) for a in alerts)
    if args.json:
        for alert in alerts:
            print(json.dumps(_json_safe({"type": "alert", "alert": alert}), ensure_ascii=True, sort_keys=True), file=out)
        payload = {"type": "summary", "at": now, "summary": lag, "venues": [], "final": True, "path": str(path)}
        print(json.dumps(_json_safe(payload), ensure_ascii=True, sort_keys=True), file=out, flush=True)
        return EXIT_OK
    # The store keeps each alert's latest state (a closed alert no longer carries its trade suggestion); the
    # append-only alerts.jsonl next to the database has every alert as it was when it opened.
    as_opened = _opened_alerts_from_jsonl(path.parent / MOVES_ALERTS_FILE)
    lines = [f"Replaying {len(alerts)} stored outside-move alert(s) from {path} (read-only; nothing is traded)."]
    for alert in alerts:
        lag_info = alert.get("lag") if isinstance(alert.get("lag"), Mapping) else {}
        alert_id = str(alert.get("alert_id"))
        at_open = as_opened.get(alert_id)
        opened = MoveEvent(kind="opened", at=float(alert.get("detected_at") or 0.0), alert_id=alert_id,
                           status=str((at_open or alert).get("status") or ""),
                           lag_outcome=str(lag_info.get("outcome") or "pending"), alert=dict(at_open or alert))
        block = format_event_lines(opened)
        kept = isinstance(alert.get("opened_trade"), Mapping) or bool(alert.get("opened_trade_note"))
        if (at_open is None and not kept and _was_lagging(alert) and not isinstance(alert.get("trade"), Mapping)
                and len(block) >= 3):
            # an alert stored without its opening suggestion (opened_trade) and no alerts.jsonl to recover it from
            block[2] = ("    suggestion at the time: not kept with the closed alert in the database "
                        f"(see {MOVES_ALERTS_FILE})")
        lines += block
        outcome = lag_info.get("outcome")
        if outcome in _REPLAY_STATUS_OUTCOMES and _num(lag_info.get("resolved_at")) is not None:
            resolved = MoveEvent(kind="status", at=float(lag_info["resolved_at"]), alert_id=alert_id,
                                 status=str(alert.get("status") or ""), lag_outcome=str(outcome), alert=dict(alert))
            lines += format_event_lines(resolved)
    from .moves import MOVES_CAVEATS, lag_table_lines

    summary = moves_summary_lines({"summary": lag, "venues": []}, now)
    # the replay has neither the venues nor the suppressed moves (filtered-out moves are not stored): say so rather
    # than "nothing"
    replaced = {"    venues: ": "    venues: not read (a replay of the stored alerts)",
                "    filtered out today: ": "    filtered out: not kept in the database (a replay shows the alerts only)"}
    lines += [next((v for k, v in replaced.items() if line.startswith(k)), line) for line in summary]
    lines += lag_table_lines(lag)
    lines += [f"  - {c}" for c in list(MOVES_CAVEATS) + ([MOVES_DEMO_CAVEAT] if demo else [])]
    _say(out, lines)
    return EXIT_OK


# lag outcomes that are "status" events of their own in a live run (cup_first and excluded resolve at the opening)
_REPLAY_STATUS_OUTCOMES = ("followed", "reverted", "not_followed", "censored")


def _opened_alerts_from_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    """alert id -> the alert as it was when it opened, from an alerts.jsonl ("opened" lines); {} when the file is
    missing or unreadable (a damaged line is skipped)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                alert = row.get("alert") if isinstance(row, dict) else None
                if row.get("event") == "opened" and isinstance(alert, dict) and alert.get("alert_id"):
                    # the file is append-only across runs (a fresh demo database keeps it): the LAST opening of an
                    # id belongs to the database being replayed
                    out[str(alert["alert_id"])] = alert
    except OSError:
        return {}
    return out


def _was_lagging(alert: Mapping[str, Any]) -> bool:
    """Whether a stored alert was lagging when it opened (--only-lagging on a replay): its ``opened_status``; for an
    alert stored without one, its lag gap at detection was most of the move and it was neither a Cup-moved-first
    nor an excluded (converging) move."""
    if alert.get("opened_status"):
        return alert.get("opened_status") == "lagging"
    lag = alert.get("lag") if isinstance(alert.get("lag"), Mapping) else {}
    if alert.get("status") == "moved_first" or lag.get("outcome") in ("cup_first", "excluded"):
        return False
    if alert.get("status") == "lagging" or lag.get("capture_4m") is not None:
        return True
    move, gap = _num(alert.get("move")), _num(alert.get("lag_gap"))
    return move is not None and gap is not None and move > 0 and gap > 0.5 * move


def cmd_fairvalue(args: argparse.Namespace, out: TextIO, err: TextIO, transport: Any = None) -> int:
    return _guarded(lambda: _fairvalue(args, out, err, transport), err)


def _fairvalue(args: argparse.Namespace, out: TextIO, err: TextIO, transport: Any) -> int:
    from . import web
    from .readonly import enforce_get_only
    from .store import TrackerStore

    temp_dir: Optional[str] = None
    runtime: Any = None
    client: Any = None
    store: Any = None
    owned_fv: Any = None  # live mode: the service this command built (closed at the end)
    try:
        if args.demo:
            from .demo import SIM_T0, SimClock

            temp_dir = tempfile.mkdtemp(prefix="supermarket-fairvalue-")
            clock = SimClock(SIM_T0)
            runtime = web.build_demo(Path(temp_dir), 30.0, news=False, out=err, clock=clock, paper=False,
                                     fair_value=args.fair_value)
            runtime.tracker.run_once()
            runtime.tracker.fair_value_step(clock())
            fv = runtime.fair_values
            directory = Path(temp_dir) / "demo"
            store = runtime.store
            payload = runtime.app.fairvalue()
            now = clock()
        else:
            settings = _settings_for(args)
            client = SuperMarketClient.from_settings(settings, transport=transport)
            enforce_get_only(client)
            ctx = resolve_context(client, slug=args.tournament or settings.tournament, public=args.public)
            snap = MarketDataBot(client, ctx).snapshot(status="open")
            memory = TrackerStore(":memory:")
            try:
                memory.upsert_markets(snap.markets)
                infos = memory.exchanges()
            finally:
                memory.close()
            quotes = {r["exchange_id"]: {"bid": r.get("best_bid"), "ask": r.get("best_ask")} for r in snap.rows if not r.get("missing")}
            mids = {eid: round((q["bid"] + q["ask"]) / 2, 6) for eid, q in quotes.items()
                    if _num(q.get("bid")) is not None and _num(q.get("ask")) is not None}
            directory = Path(settings.data_dir) / ctx.label
            from .fairvalue import FairValueService, default_providers

            if args.fair_value == "auto" and not args.json:
                print(web.FV_AUTO_NOTICE, file=err, flush=True)
            fv = owned_fv = FairValueService(directory, mode=args.fair_value, providers=default_providers(args.fair_value),
                                             cup_mids=lambda: mids)
            fv.set_targets(infos)
            now = time.time()
            fv.refresh(now)
            db = directory / "tracker.sqlite3"
            if args.import_history:
                store = TrackerStore(db)
            history = None
            if db.is_file():
                try:
                    reader = store or TrackerStore.open_read_only(db)
                    try:
                        history = reader.fair_value_source_stats("history")
                    finally:
                        if reader is not store:
                            reader.close()
                except Exception as exc:  # an older database: no history stats
                    log.debug("fair-value history stats unavailable: %s", exc)
            payload = web.fairvalue_payload(fv, quotes, now, history=history)
        if args.template:
            return _fv_template(fv, directory, out)
        if args.show_override:
            return _fv_show_override(fv, payload, str(args.show_override), out, args.json)
        if args.import_history:
            result = fv.import_history(now - float(args.days) * 86400.0, now, recorder=store.add_fair_values)
            if args.json:
                _dump_json(out, result)
            else:
                _say(out, [f"Imported {int(result.get('records') or 0)} outside price-history records (indicative, GET only)."]
                     + [f"  {venue}: {int(n)}" for venue, n in sorted((result.get("by_venue") or {}).items())]
                     + [f"  error: {e}" for e in result.get("errors") or []])
            return EXIT_OK
        if args.only_usable:
            payload = dict(payload, rows=[r for r in payload.get("rows") or [] if (r.get("fair") or {}).get("usable")])
        if args.json:
            _dump_json(out, payload)
        else:
            _say(out, fairvalue_lines(payload, demo=bool(args.demo)))
        return EXIT_OK
    finally:
        if runtime is None and store is not None:
            store.close()  # live --import-history (the demo's store closes with its runtime)
        if owned_fv is not None:
            owned_fv.close()
        if runtime is not None:
            runtime.close()
        if client is not None:
            client.close()
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _fv_template(fv: Any, directory: Path, out: TextIO) -> int:
    from .fairvalue import MANUAL_JSON

    path = directory / MANUAL_JSON
    if path.exists():
        print(f"{path} already exists: edit it (set \"probability\" for the outcomes you have an opinion on).", file=out)
        return EXIT_OK
    directory.mkdir(parents=True, exist_ok=True)
    template = fv.manual.template(fv.targets())
    path.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {path} with {len(template.get('values') or [])} outcomes: set \"probability\" (0-1) where you have "
          "an opinion; the bot reads it within a minute.", file=out)
    return EXIT_OK


def _fv_show_override(fv: Any, payload: Mapping[str, Any], eid: str, out: TextIO, as_json: bool) -> int:
    row = next((r for r in payload.get("rows") or [] if str(r.get("exchange_id")) == eid), None)
    if row is None:
        raise UsageError(f"no open outcome with exchange id {eid}")
    from .fairvalue import MAP_FILE

    map_info = (fv.status() or {}).get("map") or {}
    map_path = map_info.get("path") or MAP_FILE
    if as_json:
        _dump_json(out, {"exchange_id": eid, "map_path": map_path, "snippets": row.get("snippets"),
                         "matches": row.get("matches")})
        return EXIT_OK
    lines = [f"{row.get('title')}{' — ' + str(row['option']) if row.get('option') else ''}  (exchange {eid}, "
             f"race {row.get('race_key') or 'not recognised'})"]
    for m in row.get("matches") or []:
        lines.append(f"  matched {m.get('venue')}: {m.get('external_id')} {m.get('label') or ''} "
                     f"({m.get('kind')}, confidence {fmt_num(m.get('confidence'))}): {m.get('reason') or ''}".rstrip())
    if not row.get("matches"):
        lines.append(f"  not matched: {row.get('unmatched_reason') or 'no outside market'}")
    lines.append(f"Add one of these lines inside \"overrides\" in {map_path} (the bot reloads it within a minute):")
    labels = {"disable": "switch this outcome's outside fair value off", "pin": "pin these outside markets",
              "confirm": "confirm a suspect match", "trade_near": "allow trading a near match"}
    for key, snippet in (row.get("snippets") or {}).items():
        if snippet:
            lines.append(f"  {labels.get(key, key)}:")
            lines.append(f"    {snippet}")
    _say(out, lines)
    return EXIT_OK


def fairvalue_lines(payload: Mapping[str, Any], demo: bool = False) -> List[str]:
    counts = payload.get("counts") or {}
    lines = [
        f"Outside fair values ({payload.get('mode')}{', demo feed' if demo else ''}): {int(counts.get('outcomes') or 0)} outcomes, "
        f"{int(counts.get('matched') or 0)} matched, {int(counts.get('usable') or 0)} usable, {int(counts.get('manual') or 0)} "
        f"from your file, {int(counts.get('suspect') or 0)} suspect, {int(counts.get('near') or 0)} near matches"
    ]
    for r in payload.get("rows") or []:
        title = f"{r.get('title')}{' — ' + str(r['option']) if r.get('option') else ''}"
        lines.append(f"{title}  [{r.get('race_key') or 'race not recognised'}]  (exchange {r.get('exchange_id')})")
        fair = r.get("fair") if isinstance(r.get("fair"), Mapping) else None
        sm = f"Super Market {_signed(r.get('sm_bid'), '.3f')}/{_signed(r.get('sm_ask'), '.3f')}"
        if fair and _num(fair.get("value")) is not None:
            age = _num(fair.get("age_s"))
            unc = _num(fair.get("uncertainty"))
            venues = ", ".join(sorted({str(m.get("venue")) for m in r.get("matches") or []})) or fair.get("source")
            usable = "usable" if fair.get("usable") else f"not usable: {fair.get('reason') or 'see the dashboard'}"
            lines.append(
                f"    fair {fair['value']:.3f} ({fair.get('source')}, {fair.get('confidence')}"
                f"{f', ±{unc:.3f}' if unc is not None else ''}{f', {age:.0f} s old' if age is not None else ''})  {sm}  "
                f"gap {_signed(r.get('gap'))}  venues: {venues}  [{usable}]"
            )
        else:
            lines.append(f"    no fair value: {r.get('unmatched_reason') or (fair or {}).get('reason') or 'no outside market'}  {sm}")
        if r.get("suspect"):
            lines.append("    suspect: far from the Cup price; check the match and confirm it in the map file (--show-override).")
        if r.get("near"):
            lines.append("    near match: shown, not traded unless you allow it (--show-override).")
    lines.append("Providers:")
    for p in payload.get("providers") or []:
        err_text = f": {p.get('last_error')}" if p.get("last_error") else ""
        lines.append(f"  {p.get('name')}: {p.get('status')}, {int(p.get('requests') or 0)} requests, "
                     f"{int(p.get('matched') or 0)} matched, {int(p.get('quoted') or 0)} quoted{err_text}")
    if not payload.get("providers"):
        lines.append("  none (manual mode, or fair values are off)")
    mp = payload.get("map") if isinstance(payload.get("map"), Mapping) else None
    if mp:
        lines.append(f"Map file: {mp.get('path')} ({'exists' if mp.get('exists') else 'not written yet'}, "
                     f"{int(mp.get('overrides') or 0)} overrides)")
        lines.extend(f"  error: {e}" for e in mp.get("errors") or [])
    man = payload.get("manual") if isinstance(payload.get("manual"), Mapping) else None
    if man:
        lines.append(f"Your file: {man.get('path')} ({'exists' if man.get('exists') else 'absent: --template writes one'}, "
                     f"{int(man.get('entries') or 0)} entries)")
        lines.extend(f"  error: {e}" for e in man.get("errors") or [])
    hist = payload.get("history") if isinstance(payload.get("history"), Mapping) else None
    if hist:
        lines.append(f"Imported history: {int(hist.get('records') or 0)} records, {_utc_text(hist.get('first'))} to "
                     f"{_utc_text(hist.get('last'))}")
    lines.extend(f"Note: {c}" for c in payload.get("caveats") or [])
    return lines


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
    # Mask the API key (registered by every client) in every line any handler prints, including
    # other libraries' records: an upstream error can echo the Authorization header.
    install_log_redaction()
    if verbosity < 2:
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    # realtime-py logs every frame at DEBUG, including the access token; only -vvv shows them.
    for chatty in ("realtime", "websockets"):
        logging.getLogger(chatty).setLevel(logging.DEBUG if verbosity >= 3 else logging.WARNING)


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
    if args.command == "dashboard":
        from .moves import MOVES_POLL_MAX_S, MOVES_POLL_MIN_S
        from .web import run_dashboard  # loads settings itself; --demo needs no API key

        poll = _num(getattr(args, "moves_poll", None))
        if poll is None or not MOVES_POLL_MIN_S <= poll <= MOVES_POLL_MAX_S:  # (run_dashboard checks it too)
            print(f"error: --moves-poll must be between {MOVES_POLL_MIN_S:g} and {MOVES_POLL_MAX_S:g} seconds",
                  file=sys.stderr)
            return EXIT_USAGE
        return run_dashboard(None, args, out=out)
    # The simulation commands load settings themselves: ``paper --demo``, ``fairvalue --demo`` and every
    # ``backtest`` need no API key.
    if args.command == "paper":
        return cmd_paper(args, out, sys.stderr)
    if args.command == "backtest":
        return cmd_backtest(args, out, sys.stderr)
    if args.command == "fairvalue":
        return cmd_fairvalue(args, out, sys.stderr, transport)
    if args.command == "moves":
        return cmd_moves(args, out, sys.stderr)
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
