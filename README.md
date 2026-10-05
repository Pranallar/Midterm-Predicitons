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
| **Simulation** | The paper trader: simulated portfolios on the live ideas, their P&L at liquidation value, the verdict, the signal study and the replay backtest (see [Simulate before you trade](#simulate-before-you-trade)) |
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

* **Value**: the Cup price is far from the outside fair value (Polymarket, Kalshi or your own
  numbers, see [Outside fair values](#outside-fair-values)) by more than that fair value's own
  uncertainty plus a minimum edge.
* **Basket**: prices that do not add up. Buying every NO of a race whose YES bids sum above 1
  (or every YES when the asks sum below 1) locks in the difference, if every leg resolves.
* **Hole**: resting buy orders far below the touch that catch a thin book being swept.
* **Fade**: bet against competitor-driven spikes.
* **Carry**: buy near-certain favourites. These are flagged when the market settles after the Cup
  ends on Nov 4; such positions are only valued at market price, not paid out.
* **Arbitrage**: pricing errors the exchange reports itself.

Sizing is **conservative** by default: a fraction of the Kelly bet size, shrunk when the edge
is uncertain, capped at 8% of your balance per idea and at a 10% loss for a 3-point national
polling miss. `--sizing chaser` sizes toward the top-3 bar instead (bigger, more concentrated
bets when you are far behind; see the Strategy view's explanation of what that costs).

**Nothing is traded automatically.**

Options: `--port`, `--interval` (seconds between snapshots), `--no-news`, `--no-browser`,
`--host`, and for the simulation `--no-paper`, `--fair-value {auto,manual,off}` (or
`--no-fair-value`), `--sizing {conservative,chaser}`, `--regime {unknown,resolved_outcomes,vwap_closeout}`,
`--all-collateral`, `--paper-capital N`. Screenshots are in `docs/screenshots/`.

## Simulate before you trade

The dashboard runs a **paper trader** next to the tracker: it takes every idea the Strategy view
would show, places simulated orders for several portfolios, fills them only from order books it
reads *after* the decision, and values everything at **liquidation value** (what selling into the
bids you could see would fetch). Nothing is sent to Super Market: the bot is read-only and every
order and fill lives on your computer (`data/<tournament>/tracker.sqlite3`).

Let it run for a day on live data, then read the **Simulation** tab before you act by hand.

* **The headline is "you, acting by hand"** (`human:<sizing>`, your `--sizing`). It may fill only
  from a book read at least 4 minutes after the signal: the time it takes you to see the idea and
  type the order. It is fixed before the run starts, and it is the only portfolio whose verdict
  can reach every level. The other portfolios are exploratory: both sizing policies at bot speed
  (30 s, an upper bound for copying by hand) and one per idea kind. The best of nine looks better
  than it is by chance, so no portfolio is ever called "best".
* **Verdict levels**: *Not enough evidence yet* (below 12 covered hours, 30 ideas or 20
  independent groups of races and 2-hour periods), *Losing so far*, *Inconclusive*, *Promising,
  not proof*, and *Profitable so far*. One day can at most be **"promising, not proof"**: races
  share one national polling factor, so a single day cannot tell an edge from one national swing.
  *Profitable so far* needs 48 covered hours on two days, with the ideas of each of the last two
  days positive. Every idea entered counts (open ones at liquidation value), not just the quick
  winners that already closed.
* **The signal study** follows every value and basket signal, traded or not, and checks the Cup
  price 5 minutes, 30 minutes, 2 hours and 6 hours later: how often, and how fast, the gap to the
  outside fair value closes, net of the spread. That is hundreds of observations a day instead of
  a dozen trades. What it cannot show: whether the outside fair value is right about who wins
  (that pays on November 3-4), election night, or how the Cup's end settlement will work.
* **"Hours" are covered hours**: time the bot was actually watching. A laptop that sleeps
  overnight does not count those hours, and the gaps are shown.
* **`--regime`**: the Cup's end-of-tournament settlement rule is not published. `unknown` (the
  default, or `SUPERMARKET_SETTLEMENT_REGIME`) values open positions conservatively;
  `resolved_outcomes` assumes decided races pay 1/0; `vwap_closeout` assumes a closing-price
  settlement. `--all-collateral` assumes the D and R legs of a race share collateral (unconfirmed).

Headless and offline versions of the same thing:

```bash
python -m supermarket_bot paper --hours 24                    # live, no web server: a summary every hour
python -m supermarket_bot paper --demo --fast --hours 2       # scripted demo, seconds, in a temporary folder
python -m supermarket_bot backtest                            # replay the stored history through the same code
python -m supermarket_bot backtest --latency-sweep            # how much survives acting 30 s, 2 min or 5 min late
python -m supermarket_bot backtest --demo --demo-hours 2      # simulate the demo first, then replay it
python -m supermarket_bot fairvalue                           # every outcome's outside fair value and match
```

* `paper` ends its run when the target hours are covered (keeping the result: the next
  `dashboard` shows it under "Previous run"); `--keep-running` keeps it open, `--reset` starts a
  new one, Ctrl-C before the target leaves it open to continue next time. `--json` prints the
  final `/api/paper` body. Two `paper --demo --fast` runs print identical output.
* `backtest` opens the database read-only and first prints **what it can test**: value ideas need
  recorded (or imported) outside prices, baskets need same-snapshot quotes, arbitrage cannot be
  replayed. A replay over the same hours as the live run is not an independent check. Options:
  `--store`, `--hours`, `--since`/`--until`, `--step`, `--latency`, `--sweep NAME=V1,V2`, `--json`.
* The dashboard's **Reset** button (`POST /api/paper/reset`, local only) ends the current run and
  starts a new one; the ended run's result stays under "Previous run".
* The demo's mispricings are scripted (its outside prices lead the Cup by design), so a demo
  result says nothing about real profitability.

## Outside fair values

Value ideas compare each Cup outcome with outside prices for the same race. The bot reads
**Polymarket** (Gamma API) and **Kalshi** anonymously, with `GET` requests only: no account, no
key, nothing sent but the market ids. It refreshes them every minute, in its own worker with its
own small budget, so a slow or unreachable site never delays the Cup snapshots. If they are
unreachable (some networks block them), the bot says so and simply has no outside fair value.
`--fair-value manual` stops these reads; `--fair-value off` turns fair values off entirely.

* **Your own numbers**: `data/<tournament>/fair_values.json` (or `.csv`). Each entry names an
  outcome by `exchange_id`, by `race_key` + `party`, or by a `match` text, with a `probability`
  (`0.55` or `"55%"`), an optional `uncertainty` (default ±0.02) and `updated_at` (entries older
  than 72 hours are ignored). Your value replaces the outside one for that outcome.
  `fairvalue --template` writes a starter file listing every outcome.
* **Matches**: each outcome is matched to an outside market from a table of known ids, but only
  after checking it live (the right state and office, the right party leg, YES first, ending in
  2026). A fair value more than 0.25 from the Cup price is "suspect" and not traded until you
  confirm it. Chamber-control and Independent legs are near matches (different settlement
  wording): shown, not traded unless you allow them.
* **Fixing a match**: edit `data/<tournament>/fair_value_map.json` under `"overrides"`. Every
  row of the fair-value table (and `fairvalue --show-override <exchange_id>`) prints ready-made
  snippets to disable an outcome, pin other market ids, confirm a suspect match or allow a near
  match, e.g. `"1070": {"disabled": true, "note": "wrong match"}`. The bot re-reads the file at
  every refresh, so an edit applies within a minute.
* **History for backtests**: `fairvalue --import-history [--days 7]` imports outside price
  history (GET only) so the backtest can test value ideas over days before the bot was recording.
  Polymarket's history is indicative (not executable prices) and is labelled so.

## One process per account

The account allows 100 reads a minute across **all** your keys. The dashboard uses about 36-40 a
minute once it is running (up to about 80 in the first quarter of an hour, while it loads price
history), keeps 12 of the client's 90 for the price snapshots and lets the paper trader use at
most 20 (6 while the history loads). `/api/status` shows the projected rate and warns above 75.

So run **one** bot per account. Each database has a lease: a second `dashboard` or `paper` on the
same `data/<tournament>/tracker.sqlite3` refuses to start (exit code 2) and names the process that
holds it. Use `--data-dir` for a separate copy, and stop the other process before you start
another one on the same account; another script on the account has about 10 reads a minute left.

**Disk use**: about 25 MB a day of price snapshots, 10-20 MB of recorded fair values, about 30 MB
of order books and 10 MB of paper-trading records, kept for 10 days (ended simulation runs for 30
days): roughly 0.6-0.9 GB at most.

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
| `paper [--hours 24] [--demo --fast]` | the paper trader without a web server (see [Simulate before you trade](#simulate-before-you-trade)) |
| `backtest [--store PATH] [--demo]` | replays stored history through the paper trader (read-only; no API key) |
| `fairvalue [--demo] [--show-override EID]` | outside fair values and matches per outcome; `--template`, `--import-history` |

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

* **Read-only, enforced**: every Super Market client the dashboard and the `paper`, `backtest` and
  `fairvalue` commands build carries a guard that refuses any request other than `GET`/`HEAD`
  before it leaves the process, and a test checks the source for write calls. Outside sites
  (Polymarket, Kalshi) are read anonymously with `GET` only. The only exceptions are outside the
  simulator: `stream` mints its realtime token (below) and the opt-in `--llm` judge calls Claude.
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
