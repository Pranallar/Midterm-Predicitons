# Super Market market-data bot (Susquehanna Predictions Cup)

A **read-only** Python bot for the [Super Market](https://www.thesuper.market) prediction-market
API (`/api/v1`) that the Susquehanna Predictions Cup runs on. It finds your tournament, then
reads markets, prices, order books, trades, candles, leaderboards and engine-reported mispricings,
either once, on a polling loop, or live over WebSocket.

It never places or cancels orders.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt                          # httpx (+ realtime for the live stream)
cp .env.example .env                                      # then put your key in .env
```

`.env` (git-ignored, never commit it):

```ini
SUPERMARKET_API_KEY=ace_...
# SUPERMARKET_TOURNAMENT=<slug>   # optional; see `tournaments` below
```

You can also `export SUPERMARKET_API_KEY=...` in your shell instead. The key needs the `read` scope.

Check that the key works:

```bash
python -m supermarket_bot account
```

This prints your profile, balance and the tournaments your key can access. If exactly one
tournament is active the bot uses it automatically. Otherwise pass `--tournament <slug>`
(or set `SUPERMARKET_TOURNAMENT`).

## Dashboard (recommended)

```bash
python -m supermarket_bot dashboard          # uses your key from .env
python -m supermarket_bot dashboard --demo   # simulated market: no key, no internet
```

It opens `http://127.0.0.1:8765` in your browser. The page is served only to your own computer.
While it runs, the bot records every outcome's price every 30 seconds into
`data/<tournament>/tracker.sqlite3`. It also loads the last 7 days of price history, so surges
show up straight away. Stop it with Ctrl-C.

| View | What it shows |
| --- | --- |
| **Overview** | Outcomes tracked, surges in the last hour, competitor-driven surges still open, high-90s count, your balance and rank, and a "Look at now" list |
| **Markets** | Every outcome: last price, bid, ask, spread, change over 5 minutes, 1 hour and 24 hours, a 24-hour sparkline, and flags. Click a row for detail |
| **Surges** | Sudden moves, each with a verdict and the evidence behind it (see below) |
| **High 90s** | Outcomes whose favourite side has sat at 0.95 or more, and whether they settle before the Cup ends |
| **Strategy** | Ranked, sized trade ideas with reasoning and risks, plus a risk mode based on your standing |
| **Market detail** | Price chart with surges marked, order book, recent trades, headlines |

How a surge gets its verdict:

1. **Spotting it.** A move counts as a surge when it is large over 5 minutes, 1 hour, 6 hours or
   24 hours, judged against how much that market usually moves.
2. **Checking the news.** The bot searches Google News and GDELT (a free global news index) for
   headlines about that market published around the time of the move.
3. **Checking the trades.** It looks at who moved the price. The price history doesn't name
   traders, but a few large trades on a thin order book with no news points to a handful of
   competitors.
4. **The verdict.** **News** means the move is probably real and likely to hold. **Participants**
   means it is likely to fall back, so it becomes a "fade" idea (a bet that the price returns).
   **Unclear** means wait for confirmation.

With `--llm` and an `ANTHROPIC_API_KEY`, Claude double-checks each verdict. This costs a little
per surge and needs Python 3.10+ and `pip install anthropic`.

The strategy ideas are:

* **Fade**: bet against competitor-driven spikes.
* **Carry**: buy near-certain favourites. These are flagged when the market settles after the Cup
  ends on Nov 4; such positions are only valued at market price, not paid out.
* **Arbitrage**: pricing errors the exchange reports itself.

Sizes are a fraction of the Kelly bet size, which is calculated from your edge, and capped at
8% of your balance.

**Nothing is traded automatically.**

Options: `--port`, `--interval` (seconds between snapshots), `--no-news`, `--no-browser`,
`--host`. Screenshots are in `docs/screenshots/`.

## Commands

| Command | What it reads |
| --- | --- |
| `account` | `GET /account`, `GET /tournaments`: verifies the key |
| `tournaments [--status active]` | tournaments you can access, with slugs and IDs |
| `markets [--status open] [--search fed]` | markets in the tournament |
| `market <id>` | one market: outcomes, live quotes, resolution logic tree |
| `snapshot [--save]` | bid/ask/last for **every** outcome via the bulk price endpoint |
| `watch [--interval 30] [--min-move 0.01]` | polls snapshots, prints what moved, saves to `data/` |
| `book <market_id> [--depth 10]` | full order-book ladder for every outcome of a market |
| `book --exchange <exchange_id>` | one exchange's book |
| `trades <exchange_id> [--limit 50]` | trade tape, newest first |
| `history <exchange_id> [--resolution 1h] [--csv out.csv]` | OHLCV candles |
| `leaderboard [--period 7d] [--sort roi]` | tournament standings and your rank |
| `scan` | ALL-relationship constraint violations + multi-outcome overround/arbitrage flags |
| `portfolio` | your positions and P&L in the tournament |
| `stream [market_id ...]` | live trades and books over WebSocket (`pip install realtime`) |

Global flags go **before** the command: `--tournament <slug>`, `--public` (unbound keys),
`--json` (raw JSON for scripting), `--data-dir <dir>`, `-v` / `-vv` (logging).

```bash
python -m supermarket_bot snapshot
python -m supermarket_bot --json snapshot > prices.json
python -m supermarket_bot watch --interval 20 --min-move 0.005
python -m supermarket_bot book 26 --depth 5
python -m supermarket_bot history 36 --resolution 5m --limit 100 --csv data/36.csv
python -m supermarket_bot stream 26 27 --duration 600
```

`pip install -e .` also installs a `supermarket-bot` command with the same subcommands.

## Output files

`watch` and `snapshot --save` write to `data/<tournament-slug>/`:

* `prices-YYYY-MM-DD.jsonl`: one line per outcome per snapshot (append-only history)
* `latest.csv`: the most recent snapshot
* `markets.json`: market metadata from the last refresh

`stream` appends every event (trades, top-of-book changes, resyncs, settlements) to
`data/<tournament-slug>/stream-YYYY-MM-DD.jsonl`.

All prices are **YES-denominated probabilities in [0, 1]**. For a NO position the cost per share is
`1 - price`.

## Using it from Python

```python
from supermarket_bot import Settings, SuperMarketClient
from supermarket_bot.bot import MarketDataBot, resolve_context

settings = Settings.load()                      # reads SUPERMARKET_API_KEY / .env
with SuperMarketClient.from_settings(settings) as client:
    ctx = resolve_context(client, slug=settings.tournament)
    bot = MarketDataBot(client, ctx)
    snap = bot.snapshot()
    for row in snap.rows:
        print(row["market_title"], row["option"], row["best_bid"], row["best_ask"])

    book = client.get_market_orderbook("26", tournament_id=ctx.tournament_id, depth=20)
    candles = client.get_price_history("36", tournament_id=ctx.tournament_id, resolution="1h")
```

Every client method maps 1:1 to a documented endpoint; see `supermarket_bot/client.py`.

## How it follows the API rules

* **Auth**: `Authorization: Bearer <key>`. The key is read from the environment or `.env` and
  only ever shown masked (e.g. `ace_ab…wxyz`).
* **Trading context**: every price, book, trade and candle read passes the tournament's UUID as
  `tournamentId`, so data comes from that tournament's isolated order books and not the public ones.
* **Rate limits**: the budget is per account (100 reads and 30 writes a minute on standard
  accounts, shared by all your keys). A client-side sliding window keeps the bot at 90/25 by
  default (`SUPERMARKET_READS_PER_MIN`, `SUPERMARKET_WRITES_PER_MIN`). One full snapshot costs
  `ceil(markets/100) + ceil(outcomes/100)` reads, so `watch` every 30 s uses only a few reads a minute.
* **Retries**: `429` waits for `Retry-After` and pauses all requests. `503` (`TX_CONFLICT`,
  `SERVICE_UNAVAILABLE`, standings updating) retries with exponential backoff and jitter. Reads
  also retry `500/502/504` and network errors. Other `4xx` errors fail immediately with the
  API's error code and a hint.
* **Pagination**: follows `pagination.nextCursor` while `hasMore` is true (offset paging for
  tournaments and leaderboards).
* **Realtime**: `stream` mints a token (`POST /realtime/token`, one write) and authorizes with
  `setAuth` before joining private `tournament:{id}:market:{id}` channels. It applies pushed books
  only when their `asOf` version is newer, tracks `delivery.revision` per topic, and re-reads the
  REST order book after every (re)subscribe, revision gap, `resyncRequired` batch, null trade
  `sequence`, token refresh, `nextExpiryAt` expiry, and periodically. After a gap or rejoin it
  backfills missed trades from the REST tape. Tokens are refreshed before their 3-hour expiry.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite is fully offline:

* The REST API is faked with `httpx.MockTransport`.
* The realtime tests run the real `realtime` client against a local fake Phoenix WebSocket server.
* The dashboard has a Playwright browser smoke test (`tests/e2e/`). It runs when Node and
  Playwright are installed; skip it with `python -m pytest -m "not e2e"`.

## Reference

`docs/supermarket-openapi.json` is the OpenAPI 3.1 spec the bot was built against.
