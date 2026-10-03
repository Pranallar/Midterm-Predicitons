# Tracker, surge attribution, strategy and dashboard: design

This extends the read-only bot with:

1. **Tracker**: records every open outcome's price on an interval into SQLite, backfills
   history from candles, and keeps a live analytics view.
2. **Surge detection**: sudden moves over 5m / 1h / 6h / 24h, scaled by each outcome's own
   volatility. **High-band detection** flags outcomes whose favourite side sits at 0.95 or more.
3. **Attribution**: for each surge, searches online news and examines the trade tape and
   book to judge whether the move was *news* or *participants* (other Cup traders), and how
   likely it is to revert. An optional Claude judge refines the heuristic.
4. **Strategy**: turns all of that into ranked, sized, read-only trade ideas (fade,
   carry, arbitrage) plus a tournament-aware risk posture and a fade backtest.
5. **Dashboard**: a local web UI (`python -m supermarket_bot dashboard`), plus a `--demo`
   mode that runs the whole pipeline against a simulated market so it works offline.

Nothing in this design places orders.

Shared types live in `supermarket_bot/models.py`. Conventions: timestamps are epoch
seconds (float, UTC); prices are YES-denominated in [0, 1]; buying NO at `q` is the
same as selling YES at `1 - q`; for YES best bid `b` and best ask `a`, the NO ask is
`1 - b` and the NO bid is `1 - a`.

## Module map

| Module | Role | Depends on |
| --- | --- | --- |
| `models.py` | dataclasses (done) | – |
| `store.py` | `TrackerStore`: SQLite persistence (thread-safe) | models |
| `analytics.py` | pure functions: mark price, changes, volatility, surges, high band, trade flow, downsampling | models |
| `news.py` | news providers (Google News RSS, GDELT, optional NewsAPI), query building, relevance | models |
| `attribution.py` | heuristic verdict, optional `LLMJudge` (Claude), `Attributor` orchestration | models, analytics, news, client |
| `strategy.py` | opportunities, sizing, risk mode, report, fade backtest | models, analytics |
| `tracker.py` | `Tracker`: background loop + workers, live view | client, bot, store, analytics, attribution, strategy |
| `demo.py` | simulated Super Market API (`httpx.MockTransport`) + `DemoNewsProvider` | models, news |
| `web.py` + `web/` | HTTP server + single-page UI | tracker, store, strategy |

## Rate budget (per account: 100 reads + 30 writes per minute, shared by all keys)

The client's own limiter caps the bot at `SUPERMARKET_READS_PER_MIN` (default 90). The
tracker splits that budget:

* **Snapshots** (highest priority): `ceil(markets/100) + ceil(outcomes/100)` reads per cycle.
  The market list refreshes every `market_refresh` seconds (default 300); prices every `interval` (default 30).
* **Backfill** (its own limiter, default 30 reads/min): one `price-history` read per outcome
  per resolution: `1h` x 168 (7 days), then `5m` x 288 (24 hours).
* **Analysis** (its own limiter, default 20 reads/min): per surge, one trade-tape read (up to
  200 trades since `start_ts - 1h`) and one exchange orderbook read (`depth=20`).
* **Context** (every 300 s): `GET /relationships/constraints?violationsOnly=true`,
  `GET /tournaments/{slug}` (my balance), the leaderboard top 3, and up to 10
  multi-outcome `GET /markets/{id}/orderbook` reads for overround and arbitrage flags.

News providers are separate hosts with their own politeness limits: GDELT at most 1
request per 5 s; Google News at most 1 request per 2 s. Results are cached in the store
for 20 minutes per query.

## Analytics (pure, `analytics.py`)

* **Mark price**: `mark_price(last, bid, ask, max_spread=0.10)`. If bid and ask both exist and
  `ask - bid <= max_spread`, the mid; else the last trade if present; else the mid (wide book);
  else None.
* **Value at time**: the mark of the latest point with `ts <= t`. It counts as missing if that
  point is older than `t - tolerance`, where the tolerance is `max(0.25 * window, 120 s)`.
* **Volatility**: resample marks onto a 300 s grid (forward-fill) over
  `[now - 24h - window, now - window]` (the history *before* the window under test), take the
  standard deviation of step differences. Return None with fewer than 12 steps.
* **Surges**: `detect_surges(points, now, exchange_id, market_id)`. Windows and minimum
  absolute moves are `5m` ≥ 0.05, `1h` ≥ 0.08, `6h` ≥ 0.10, `24h` ≥ 0.12. The current mark must
  be from a point no older than 15 minutes. `change = p_now - p_then`.
  `z = change / (sigma * sqrt(window / 300))` when sigma > 0, else None. A window qualifies if
  `|change| >= min_change` and (`z is None` or `|z| >= 3`). Return **at most one Surge per
  exchange**: the qualifying window with the largest `|z|`, or the largest `|change|` when
  z is None. Set `start_ts`/`start_price` from the `p_then` point and `end_ts`/`end_price` from
  the latest point. `peak_price` is the max (up) or min (down) mark in `[start_ts, end_ts]`.
* **Surge lifecycle**: `update_surge_status(surge, current_price, now)` sets
  `reverted_fraction = (peak - current) / (peak - start)` for up-moves (mirrored for down,
  clamped to [-1, 2]). Status is `reverted` if that fraction is ≥ 0.5, else `held` once
  `now - end_ts >= 24h`, else `open`.
* **High band**: `high_band(points, now, threshold=0.95, lookback_s=6h, min_fraction=0.8)`.
  The favourite side is YES if the current mark is ≥ 0.5, else NO, with
  `fav = p` or `1 - p`. `time_in_band` is the time-weighted share of the lookback where
  fav ≥ threshold. Report the band when the current fav ≥ threshold and
  `time_in_band >= min_fraction`. It is `stable` when `high - low <= 0.03` in the lookback.
  `hours_to_settlement` is computed from `settlement_date` when given.
* **Trade flow**: `trade_flow(trades)` computes the `TradeFlow` fields. HHI is
  `sum((size/total)^2)`. `price_impact` is `|last_price - first_price| / max(total/100, 1)`,
  with trades in time order.
* **Downsampling**: `downsample(points, max_points)` keeps the first and last points and
  picks evenly spaced points in between (for sparklines and charts).

## Attribution (`attribution.py`)

The heuristic is `attribute(surge, flow, articles, book_depth, market_title, option, now)`.

* **News score**: the maximum over articles of `relevance x timing`. Timing is 1.0 when the
  article was published in `[start_ts - 6h, end_ts + 30m]`; 0.6 when in
  `[start_ts - 24h, start_ts - 6h)`; 0.3 when after `end_ts + 30m` (coverage, not cause); 0.4
  when the date is unknown.
* **Crowd signals**, booleans counted as `crowd`:
  * few trades: `n_trades <= 5`
  * concentrated: `top_trade_share >= 0.5` or `hhi >= 0.35`
  * thin book: `book_depth < 500`
  * one-sided: `yes_share >= 0.85` or `<= 0.15`
  * big move on small volume: `|change| >= 0.10` and `total_size < 1000`
  * no trades at all in the window (`n_trades == 0`): the move came from quotes or the book
    alone (counts as 2)
* **Verdict**:
  * **news**: news score ≥ 0.55. `confidence = min(0.95, 0.5 + news/2 - 0.05 * crowd)`,
    `reversion_odds = 0.2`.
  * **participants**: news score < 0.3 and crowd ≥ 2.
    `confidence = min(0.9, 0.4 + 0.12 * crowd)`,
    `reversion_odds = min(0.8, 0.5 + 0.08 * crowd)`.
  * **unclear**: anything else. `confidence = 0.35`, `reversion_odds = 0.4`.
* `reasons` are short human-readable bullets for each signal, with numbers.
* The trade tape has no trader identity, so "participants" means the evidence points to a
  few Cup traders. Say this in the reasons.

`LLMJudge` is optional, enabled by `--llm` / `SUPERMARKET_LLM=1` and requires the `anthropic`
package plus credentials (ANTHROPIC_API_KEY or an `ant auth login` profile).

* Model `SUPERMARKET_LLM_MODEL`, default `claude-opus-5-5`.
* Call via `client.beta.messages.create(model=..., max_tokens=16000,
  betas=["server-side-fallback-2026-07-01"], fallbacks="default",
  output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
  system=..., messages=[...])`.
  * Do **not** send `thinking` (it can't be disabled on this model) or sampling params.
  * Check `stop_reason == "refusal"` before reading content (then return None).
  * Parse the first text block as JSON.
* Schema: `{verdict: enum[news, participants, unclear], confidence: number,
  reversion_odds: number, explanation: string, key_article_indexes: array[integer]}`,
  `additionalProperties: false`, all required.
* `merge(heuristic, llm)`: the verdict comes from the LLM; confidence and reversion_odds are
  the mean of both; prepend the explanation to the reasons; set `method = "heuristic+llm"`.
* Any SDK or API error is logged and the heuristic result stands.

`Attributor(client, context, store, news, judge=None, limiter=None)`. `analyze(surge)` does:

1. Fetch trades since `start_ts - 3600` (`client.iter_trades(..., start=iso, max_items=200)`)
   and store them.
2. Fetch the book (`client.get_exchange_orderbook(eid, depth=20, tournament_id)`) and take
   the depth within ±0.05 of the mid.
3. Search news for the market title and option, then the heuristic, then the optional LLM.
4. Return the Attribution; the caller stores it.

API errors degrade gracefully: flow or depth are left as None and the reasons say so.

## News (`news.py`)

* `build_query(title, option)` strips question scaffolding ("Will", "be", "the", "in", "by",
  "?", years such as 2026, "election", "win", etc.). It keeps proper nouns (capitalised
  tokens), US state names, offices (Senate, House, Governor, Mayor, Attorney General),
  parties, and the option label when it is a name (not YES/NO). The result is at most 6
  terms. `keywords(title, option)` returns the same terms lower-cased, for relevance.
* `relevance(article, keywords)` is the weighted share of keywords found in the title and
  summary (word-boundary, case-insensitive). The option or candidate name and the state each
  weigh 2, other terms 1. Clamp to [0, 1].
* Providers (`search(query, since, until, limit) -> List[Article]`). Each takes an injectable
  `httpx.Client` and never raises: on error it logs a warning and returns [].
  * `GoogleNewsRSS`: `https://news.google.com/rss/search?q=<query> when:<N>d&hl=en-US&gl=US&ceid=US:en`
    (RSS 2.0: `item/title`, `link`, `pubDate` (RFC 822), `source`). Strip a
    trailing " - Source" from titles.
  * `GDELTDoc`: `https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=artlist&format=json&maxrecords=<n>&sort=datedesc`
    with `startdatetime`/`enddatetime` (`YYYYMMDDHHMMSS`). JSON `articles[]` with `title`, `url`,
    `domain`, `seendate` (`YYYYMMDDTHHMMSSZ`). Add ` sourcelang:english`.
  * `NewsAPIOrg`: only when `NEWSAPI_KEY` is set
    (`https://newsapi.org/v2/everything?q=..&from=..&to=..&sortBy=publishedAt&language=en`).
* `NewsSearcher(providers, store=None, cache_ttl=1200)`. `search_for_market(title, option,
  since, until, limit=20)` queries every provider (respecting per-provider min intervals),
  dedups by normalised URL/title, scores relevance and sorts by relevance then recency. It
  caches in the store by `(query, rounded window)`.
* "News on the website": the Super Market API has no news endpoint. The market's own
  metadata (title, resolution tree) is used for the query, and the UI links to the market page.

## Strategy (`strategy.py`)

The defaults are the Cup's: 100,000 SUSQies starting balance; the tournament `endDate`
comes from the API, falling back to `SUPERMARKET_CUP_END`, default
`2026-11-04T17:00:00Z` (noon ET on Nov 4, 2026, after DST ends).

* **Fade** (a participants surge that is still `open`): bet on reversion.
  * After an up-move, buy NO at `entry = 1 - yes_bid`. After a down-move, buy YES at
    `entry = yes_ask`. Without a book, use the mark.
  * The target is a half reversion. The stop is a further 50% extension of the move beyond
    the peak.
  * `p = attribution.reversion_odds`, blended 50/50 with the backtest reversion rate when the
    backtest has at least 10 surges.
  * `edge = p * gain - (1 - p) * loss` per share. Only emit it when `edge > 0`.
  * Horizon 6 h.
* **Carry**: a `HighBand` that is `stable`. Buy the favourite at its ask.
  * `p_true = fav_mid + min(0.01, 0.2 * (1 - fav_mid))` (a small favourite-longshot
    adjustment; say so in the rationale).
  * `edge = p_true * (1 - entry) - (1 - p_true) * entry`.
  * `settles_before_cup_end` is computed from the settlement date. If False, the position is
    only marked at the Cup's end: halve the score and add the risk "valued at market price at
    Cup end, not paid out".
  * Skip when `entry >= 0.995` (no room).
* **Arbitrage**: one entry per engine-reported constraint violation (`violationAmount`, the
  suggested trades as the rationale) and per multi-outcome book with
  `hasArbitrageOpportunity`.
* **Watch**: unclear surges that are still open, with zero size and a "wait for confirmation"
  note.
* **Sizing**:
  * Kelly for buying at `c` with win probability `p`: `f = (p - c) / (1 - c)`, clamped ≥ 0.
  * Use `kelly_fraction` (default 0.25) times f, capped at `max_position_pct` (default 0.08)
    of the balance and by the visible depth at the entry, when known.
  * `shares = floor(stake / entry)`.
* **Risk mode**: `risk_mode(balance, initial, leader_value, my_rank, days_left)`.
  * **protect**: rank ≤ 3 and days_left ≤ 7.
  * **aggressive**: (leader_value and balance < 0.9 * leader_value) or
    (days_left ≤ 10 and (my_rank is None or my_rank > 10)).
  * **balanced**: otherwise.
* **Score**: `expected_return x confidence`, times a mode multiplier. Aggressive favours
  fades and arbitrage (x1.3) and dampens carry (x0.7); protect favours carry (x1.3) and
  dampens fades (x0.6).
* `build_report(...)` returns a `StrategyReport`: a headline, 4-6 principles explaining the
  tournament logic in plain language, and opportunities sorted by score (top 50).
  * The principles cover: prizes go to the top 3 of many players, so variance is your friend
    when behind; carry trades compound slowly; fade only participant-driven moves; respect
    settlement dates versus the Cup end; diversify across uncorrelated markets.
* `backtest_fade(series_by_exchange, horizon_s=6h, step_s=300)`:
  * Walk each series on the step grid and run `detect_surges` at each step.
  * Count a surge once per `(exchange, direction)` until its status is no longer open.
  * Measure the mark at detection + horizon: reverted if half or more of the move came back.
  * `fade_return` is per share (the price change against the move); `hold_return` is its
    negative.
  * Aggregate overall and by window.

## Tracker (`tracker.py`)

`Tracker(client, context, store, *, interval=30, market_refresh=300, backfill=True,
attributor=None, analyze=True, backfill_reads_per_min=30, analyze_reads_per_min=20,
context_refresh=300, clock=time.time)`.

* `run_once()` runs one cycle synchronously (used by tests and by the loop):
  1. Refresh markets if due (`MarketDataBot.list_markets(status="open")` →
     `store.upsert_markets`).
  2. Snapshot (`MarketDataBot.snapshot(markets)`) → `store.add_ticks`.
  3. For every exchange, read `store.series(eid, now - 30h)` and run `detect_surges` and
     `high_band`; merge surges into the store (`store.record_surge` keeps one open surge per
     exchange and direction, extending it); update every open surge's status. Build the
     in-memory `view`.
  4. Queue newly detected or substantially grown surges (`|change|` up by ≥ 0.03 since the
     last analysis) for attribution.
  5. Refresh context (constraints, balance, leaderboard, overround) if due.
* `start()` runs the loop in a daemon thread plus a backfill worker and an analysis worker.
  `stop()` signals them, calls `client.cancel()` and joins.
* `view()` returns a thread-safe snapshot dict for the dashboard: `exchanges` (one row per
  outcome: ids, title, option, settlement date, mark, last, bid, ask, spread,
  change_5m/1h/24h, a downsampled 24 h sparkline `[[ts, price], ...]` of at most 96 points,
  `high_band` dict or None, `surge_id` or None), `surges` (recent, newest first, with
  attribution), `high_band` list, `context` (balance, leaderboard, constraints,
  overround rows), and `status` (last snapshot time, cycles, errors, read-budget use,
  backfill progress, queue lengths).
* Errors: transient API errors are logged and counted; `bot.is_fatal` errors stop the
  tracker and surface in `status.fatal_error`.

## Demo (`demo.py`)

`DemoMarket(seed=7, now=None, clock=time.time)` simulates one tournament
("Predictions Cup — Midterm Elections (demo)", 100,000 SUSQies) with about 14 markets and 22
outcomes.

* Election-style titles, e.g. "Will Republicans win the Pennsylvania Senate race?", and one
  multi-outcome "Who will win the Arizona Governor race?" with three candidates.
* Prices follow mean-reverting random walks on the 0.005 tick, generated deterministically
  for the past 7 days and continuing live with the clock.
* Scripted events, relative to the demo start `t0`:
  * (a) **news surge**: "Pennsylvania Senate" +0.14 at `t0 - 50m` that holds. DemoNewsProvider
    returns matching headlines published at `t0 - 55m`.
  * (b) **participant spike**: "Ohio House District 9" +0.18 at `t0 - 20m` from 2 large
    trades, no news, reverting about 60% over the next 40 live minutes.
  * (c) four outcomes hovering at 0.96-0.985. Two settle before the Cup end, two after.
  * (d) a multi-outcome market whose book flags `hasArbitrageOpportunity`.
  * (e) one ALL constraint violation.
  * (f) a **live surge** about 90 s after start (participant-style), so the dashboard shows
    detection happening.
* `transport()` returns an `httpx.MockTransport` serving the endpoints the bot uses, with
  realistic shapes from the OpenAPI spec:
  * `/account`, `/tournaments`, `/tournaments/{slug}`, `/tournaments/{slug}/markets`,
    `/markets`, `/markets/{id}`, `/markets/{id}/nodes`, `/markets/{id}/orderbook`
  * `/exchanges/prices`, `/exchanges/{id}/price`, `/exchanges/{id}/orderbook`,
    `/exchanges/{id}/price-history`, `/exchanges/{id}/trades`
  * `/relationships/constraints`, `/tournaments/{slug}/leaderboard`, `/leaderboards`,
    `/tournaments/{slug}/portfolio/positions`, `/tournaments/{slug}/portfolio/pnl`
  * `POST /realtime/token`
  * Unknown routes return 404 in the error envelope.
* `DemoNewsProvider(market)` implements the provider interface with canned articles for
  scripted news events, plus unrelated noise articles.

## Web (`web.py` + `supermarket_bot/web/`)

* `python -m supermarket_bot dashboard [--port 8765] [--interval 30] [--demo] [--no-browser]
  [--llm] [--no-news] [--host 127.0.0.1]`.
* Standard library `ThreadingHTTPServer`, bound to 127.0.0.1 by default. Static files are
  served from `supermarket_bot/web/` (index.html, app.js, styles.css); there is no build step
  and no CDN.
* JSON API:
  * `GET /api/status`: context, tracker status, demo flag.
  * `GET /api/markets`: the view's exchange rows, plus `?q=` filtering.
  * `GET /api/surges`: newest first.
  * `GET /api/highband`.
  * `GET /api/exchange/<id>`: full 7 d series (ticks + candles), surges for that exchange,
    recent stored trades, and the live book (fetched on demand, cached 15 s).
  * `GET /api/strategy`: StrategyReport, cached 30 s.
  * `POST /api/surges/<id>/analyze`: re-queue for attribution.
* All article titles, market titles and other external strings are inserted as text
  (`textContent`), never as HTML. Links get `rel="noopener noreferrer"`.
* The UI polls every 5 s. Views:
  * **Overview**: stat tiles; a "what to look at now" list.
  * **Markets**: sortable and filterable table with sparklines.
  * **Surges**: cards with a verdict badge, reasons and headlines.
  * **High 90s**: settles-before-Cup-end flag.
  * **Strategy**: risk mode, principles, ranked ideas with suggested size.
  * **Market detail drawer**: price chart with surge markers and the 0.95 band, book ladder,
    trades, news.
* Light and dark mode. Charts follow the dataviz skill rules: one axis, thin marks,
  hover tooltips, recessive grid, legends, and text in text colours.
