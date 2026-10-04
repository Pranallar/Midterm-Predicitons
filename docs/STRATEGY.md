# Strategy guide: Susquehanna Predictions Cup (2026 midterms)

Written 2026-10-04, day 4 of the Cup. **We have not seen your markets, prices or leaderboard**: this
sandbox can't reach thesuper.market, so market numbers below are illustrations or outside anchors.
The bot is read-only.

**Rotate your API key now** (My Profile → API Keys). The old one was shared in plain text and was
once committed to this public repo, where git history still holds it. Keep the new key only in `.env`. "All API and Bot activity shall be deemed the activity of the
participant" [RULES\*], and a leaked key lets someone dump your positions into their bids.

**Sources.** [SPEC x]: `docs/supermarket-openapi.json` at JSON path x. [RULES\*]
https://predictionscup.com/rules/, [DOCS\*] https://sig.thesuper.market/docs/tournaments-and-organizations
(search snippets only). [GH user/repo]: a participant's README. [CODE file:line]: this repo. [CALC]:
our arithmetic. [SIM n]: our simulation (invented parameters).

## 1. What actually decides the Cup

**Ranking.** The top 3 win "up to" $30,000, $5,000 and $2,500, ranked on "verified total SUSQies
balance as of the resolution of all Markets"; prizes are "subject to verification of the eligibility"
of winners [RULES\*]. Everyone started with 100,000 [DOCS\*]; 4th place pays the same as last. "All
trades must be received prior to 12:00pm Eastern Standard Time on November 4, 2026" [RULES\*] (17:00
UTC [CODE strategy.py:46]). Check `endDate` from `tournaments` against that; the spec doesn't describe
the field [SPEC components.schemas.TournamentSummary].

**Open positions at the end (the biggest unknown).**
- The rules say "all positions are locked" [RULES\*], and rankings are confirmed "once every market
  has settled" [DOCS\*]: read literally, a winning share pays 1.00 when its race resolves.
- Or the platform cashes out unresolved positions at a "proposedPrice (5h VWAP default)"
  (volume-weighted average price), but admins can override it (sources include `admin_override` and
  `missing`) and must submit prices where `needsSubmittedPrice` is set [SPEC
  paths./dmm/tournaments/{slug}/end-settlement.get]. The `resolved_outcomes` mode waits for real results
  [SPEC paths./dmm/tournaments/{slug}/end-settlement.post]. You can't see which mode applies.
- The Sponsor may "update the rules, outcomes, or resolution of a Market at any time, including after
  the resolution date or end date" [RULES\*].
- The bot assumes cash-out: it halves late-settling carries' scores [CODE strategy.py:556] ("only
  valued at market price" [CODE README.md:78; strategy.py:753, 1203]).
- **Working rule:** prefer positions that pay under both modes, and ask the organizers.

**The leaderboard is not the prize.** It ranks "SUSQies account value" [RULES\*], computed at
"tournament valuation prices" [SPEC paths./tournaments/{slug}/portfolio/pnl.get] (undocumented; we
infer cash plus positions at those prices). Today's "hundreds of thousands" may be largely *marks*
(paper values) that thin trades move, but gains can also be banked before resolution via round trips,
convergence exits and arbitrage unwinds; without trade histories you can't tell. Treat `pnl` (period
`all`) as the bar to beat, marks included.

**Smart Score** "combines PnL, ROI, win rate, and Sharpe-scaled volatility" [SPEC
paths./tournaments/{slug}/me/smart-score.get]; an admin export orders recruiting candidates "by
org-aggregate Smart Score" [SPEC paths./dmm/icims/export.get]. Weights are unpublished: a winning
all-in raises PnL and ROI, but volatility may penalise it (our inference).

## 2. Where big returns plausibly come from

**Costs.** There are "no real-money deposits, withdrawals, or fees" [SPEC
paths./dmm/tournaments/{slug}/transactions.get]. Your costs are the *spread*, *slippage* past the top of
thin books, *adverse selection* (being filled when you're wrong) and capital locked until resolution.
One participant's exits "realised losses" when "market-sold through the book" [GH nullif1ed/sigprediction].

**Arithmetic.** The chance of ending at M times your start is at most your expected multiple divided
by M [CALC: Markov's inequality]; with no edge, 4x has at most a 25% chance, so 300–400k probably needs
*concentration* (our inference). The bot bets a quarter of *Kelly*
(the growth-maximizing fraction), capped at 8% [CODE strategy.py:49-50], which never doubled an
account in our runs [SIM 2].

**Two cautions.**
- *Cap the whole book.* Fifteen quarter-Kelly stakes of 6,250 deploy 94% of 100k [CALC], and
  Cup-vs-Polymarket gaps tend to point the same way. Copy one participant's limits: reduce-only if "the worst settlement
  outcome would cost more than 30% of the account", plus a net Republican-vs-Democrat cap [GH
  grissou/prediction-market-maker].
- *Directional edges may be thin.* Another bot trades "Arbitrage only by default": "directional trades
  and sniping lost money on the newest unseen data", and 1-point entries "lost money out of sample" [GH
  nullif1ed/sigprediction]. Trade only large, verified gaps with depth.

Contributions below are rough guesses.

### 2.1 Value trades against outside prices
- **Why it might work.** A participant says the tournament is "seeded from" Polymarket and trades
  "against stale house quotes when Polymarket has been 5c+ past them" [GH grissou/prediction-market-maker].
  Gaps close when Cup traders, or the house's "managed market-maker liquidity" [SPEC
  paths./dmm/tournaments/{slug}/exchanges/{exchangeId}/liquidity], re-quote.
- **Match resolution terms.** Kalshi's Senate market reportedly settles on the President pro tempore's
  party "on February 1, 2027" (snippet of https://kalshi.com/markets/controls/senate-winner/controls-2026).
  Check Cup terms with `market <id>`.
- **Example [CALC].** Cup ask 0.40, fair value 0.55: Kelly = 0.15/0.60 = 25%. A quarter is 6,250
  SUSQies (15,625 shares), +2,344 expected. Selling at a 0.53 bid banks +2,031, if the depth is there.
- **Leads, not consensus** (snippets; re-check). Democrats' Senate chance: Silver Bulletin 71% on Oct 3
  (https://politicalwire.com/2026/10/03/forecasters-favor-democrats-to-win-house-and-senate/; an older
  snippet said 57%), Kalshi 63
  (https://news.kalshi.com/p/democrats-midterm-senate-odds-jump-to-63-up-14-points-since-mid-september),
  Polymarket 65 in October (https://polymarket.com/event/which-party-will-win-the-senate-in-2026).
  Sources 6–8 points apart give you a view, not an edge, and this market may not resolve by Nov 4
  (leaving it at the end price under a cash-out).
- **Size** quarter-Kelly per race inside the book caps; a same-party slate of toss-ups is one bet [SIM 3].
- **What kills it:** wrong outside prices, faster bots, thin books, and (under a cash-out) gaps that
  never close.
- **Contribution [CALC]:** 30k at risk near 0.40 expects about +3,750 per round at 5-cent gaps,
  +11,250 at 15-cent gaps, before slippage.

### 2.2 Election night on public results
- **Why it works.** Polls close from 6pm to 1am ET [https://www.270towin.com/poll-closing-times/],
  leaving 11–18 hours of public results and AP calls before the lock. Early states reveal the national
  error; thin books lag. Possibly the biggest window: low tens of thousands if books are deep.
- **Example [SIM 9].** A D+1 race (sd 4 points) has P(D) = 0.60. Early states put Democrats ~3 points
  behind the model: shift the race −2.5 (to R+1.5), sd 3.5, and P(D) = 0.33. If the Cup still bids 0.58
  for D YES, NO costs 0.42, worth ~0.67: +2,459 on 10,000 shares costing 4,200.
- **Called races** at 0.93 pay +7.5% but risk 0.93 to make 0.07. Calls get retracted, the Sponsor can
  re-resolve, and under a cash-out, races settling after Nov 4 are valued at the end price. Our cap: 10% of your account
  per race, 30% in total.
- **Tools.** `stream <market ids>`: Realtime connections "do not count against the budget" [SPEC
  info.description]; the bot's 30-second polling [CODE tracker.py:235] is too slow. Writes are capped
  (rule 4), so batch re-quotes.
- **What kills it:** markets that may resolve on certification (check `market <id>`), misleading count
  order [https://www.nbcnews.com/politics/2024-election/red-blue-mirage-election-night-vote-counts-make-hard-tell-will-win-rcna175475],
  Alaska counting until about Nov 18
  [https://www.elections.alaska.gov/ballot-counting-process/counting-system-and-schedule/], and
  vanishing liquidity.

### 2.3 Arbitrage: NO baskets
- **Why it works.** Races reportedly come as separate "Democrat wins" and "Republican wins" markets
  not forced to sum to 1 [GH sam-bateman/sig-bot]. At most one wins, so when YES **bids** sum above 1,
  NO on both pays at least 1.00, *if* both legs fill at those prices and settle on the real outcome.
- **Example [CALC].** Bids D 0.60 + R 0.45 = 1.05. Selling YES at both bids buys NO ("sell yes q@p ≡
  buy no q@(1−p)" [SPEC paths./orders.post]) for 0.95: +5.3%. A participant saw Delaware Senate
  bids of 0.95 + 0.08 (+3.1%) on 30 Sep, before the Cup opened [GH nullif1ed/sigprediction].
- **How it breaks.** A market can settle as "`REFUND` for a refund" [SPEC paths./realtime/token.post]:
  if D refunds (about 0.40 back) and R wins, the set returns about 0.40 on 0.95. Mismatched resolution
  sources or a re-resolution also break it, so compare both legs with `market <id>`.
- **Not arbitrage.** YES **asks** summing below 1 lose if a third candidate wins (our reasoning).
  `hasArbitrageOpportunity` flags "a mutually exclusive multi-outcome market whose best prices sum below
  1 — a potential arbitrage, not a guaranteed one" [SPEC
  components.schemas.CombinedMarketOrderbook.properties.hasArbitrageOpportunity]: one market's book,
  so it misses separate D and R markets.
- **Spot and size.** Sum D/R bids from `--json snapshot` (`scan --max-markets 250` lists only engine
  violations). Send one multi-leg order ("All legs succeed or none are persisted" [SPEC
  paths./orders/multi-leg.post]); resting legs can still fill unevenly. Linked holdings may need less
  cash (an "ALL collateral advance" [SPEC components.schemas.AllExecutionEconomics]); that isn't
  directional leverage (our reading).
- **Capital lock-up.** Held to resolution, a set ties up its 0.95 until election night or later, for a
  few percent over the whole Cup. You don't have to hold it: sell both NO legs once the pair's prices
  are back in line. Round-trip profit = (YES bid-sum at entry) − (YES ask-sum at exit) [CALC]. Example:
  enter at bids 0.95 + 0.08 = 1.03 (cost 0.97); later the YES asks are 0.955 + 0.05 = 1.005, so the NO
  legs sell for 0.045 + 0.95 = 0.995: +0.025 a set, and the cash is free for the next trade.
- **Depth is the real cap.** Profit is edge × sets you can actually fill: 2,000 sets at +0.03 is +60
  SUSQies (0.06% of 100k) [CALC]. Check the depth of both books first (`book <market_id> --depth 20`).
- **Contribution:** a few hundred to a few thousand, at lower risk *if* both legs fill and resolve
  identically. Useful for idle cash; it will not close a big gap. Each set's losing leg may dent your
  Smart Score win rate.

### 2.4 Resting bids for "liquidity holes"
A *market order* fills "at best available price" [SPEC paths./orders.post], and "Other bots regularly
pull a whole side of the book" (bids 0.96 to 0.16 and back) [GH nullif1ed/sigprediction]. **Example
[CALC]:** a small resting *limit order* at 0.70 in a race worth 0.95 catches a careless 500-share
sell: +125. Set an `expirationDate` [SPEC components.schemas.OrderInput] and cancel before news, which
hits that bid first. The Sponsor may "void, reverse, or disqualify any order, trade, or position"
[RULES\*], so fills at erroneous prices may be reversed.

### 2.5 Fading participant-driven spikes (the bot's "fade")
No-news moves made by a few big trades tend to revert
[https://users.wfu.edu/strumpks/papers/ManipIHT_June2008(KS).pdf]. **Example [CALC]:** after a spike
from 0.40 to 0.60, buy NO at 0.41 with a +0.09 target and a −0.11 stop. If the target hits first 70%
of the time, the edge is +0.03 a share (break-even 55%). The bot's heuristic gives 66–80% *reversion*
odds (not target-first odds), depending on how concentrated the flow is, capped at 60% in news
outages [CODE attribution.py:295, 299]; at 66% the edge is +0.022. The API has no stop orders [SPEC
components.schemas.OrderInput], and a mechanical stop can sell into a hole: at a 0.25 loss, the edge
is −0.012. Stop on the mid with confirmation; exit with limit orders.

### 2.6 Market making and carry (steadiness, not catch-up)
*Market making* at 1 cent a side on 2,000-share clips needs 10 million shares bought and 10 million
sold for +200k, before adverse selection, and loses money when informed flow is heavy [SIM 6]. The
30-writes-a-minute account budget also caps re-quoting. *Carry* (YES at 0.97, dashboard **High 90s**)
pays +3.1% only if it wins, so it needs a true chance above 0.97. One upset costs the gain from ~32
winners, "safe" seats flip together in a wave, and under a cash-out late settlers are marked, not paid
1.00. Low variance until it isn't: cap it (say 20%). Both may help Smart Score (weights unknown); neither is likely to win.

## 3. Month plan

- **Oct 4–6.** Run Section 6, record resolution sources and settlement dates, email the organizers.
- **Oct 6–15.** Maine's Senate debates start Oct 6
  [https://www.themainewire.com/2026/10/maines-october-debate-sprint-begins-as-candidates-in-three-major-races-prepare-to-face-off/].
  Iowa: Oct 7 [https://cbs2iowa.com/news/local/iowa-pbs-sets-oct-7-us-senate-debate-between-hinson-and-turek]
  and Oct 13 [https://who13.com/on-air/seen-on-tv/who-13-to-host-iowa-u-s-senate-debate-oct-13/].
  Texas (Oct 6, 13, 14) is proposed only; Paxton hadn't accepted by Sep 29
  [https://www.wfaa.com/article/news/local/dates-times-announced-debate-paxton-talarico/287-3f31b225-e83a-4f1b-9276-e09223ae1d09;
  https://www.washingtonpost.com/politics/2026/09/29/fox-news-agrees-host-texas-senate-debate-no-word-paxton-yet/].
  CPI: Oct 14 [https://www.bls.gov/schedule/news_release/cpi.htm].
- **Oct 16–28.** Build an edge-based lead; track the 3rd-place bar weekly. FOMC: Oct 28
  [https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm].
- **Oct 29–Nov 2.** Choose a posture (Section 4) and prepare an election-night sheet (margin, reporting
  order, resolution source, count speed). The `30d` ("This Month") leaderboard resets at 00:00 UTC
  Nov 1 [SPEC paths./tournaments/{slug}/leaderboard.get].
- **Nov 3** poll closings (ET): 7pm GA, VA; 7–8pm NH; 7:30 NC, OH; 8pm ME, PA, most of MI and TX; 9pm
  AZ, IA, MN, NE, NY, WI; 10pm NV; 11pm CA; midnight HI and most of AK
  [https://ballotpedia.org/State_Poll_Opening_and_Closing_Times_(2026)].
- **Nov 4, 7am to noon ET,** would be a 5-hour cash-out window (our inference): no large thin positions.
- **After the lock,** under `resolved_outcomes`, ranks wait for Alaska (about Nov 18) and any Georgia
  runoff (Dec 1 [https://georgia.gov/events/2026-12-01/election-day-general-election-runoff]), or
  longer for recounts and certification.

## 4. Risk rules

1. Limit orders only, on the 0.005 tick from 0.005 to 0.995 [SPEC info.description]. Expire resting
   orders and cancel them before news. Size to tradable depth, and value your book at bids.
2. Count correlated races as one bet, inside the book-wide caps (Section 2).
3. Pass the Cup's `tournamentId`. `/portfolio/positions`, `/pnl`, `/history`, `/settlements` and
   `/transactions` take none and "always report that default" (the organization's default tournament,
   maybe the Cup); use `/tournaments/{slug}/portfolio/*` [SPEC info.description].
4. You get 100 reads and 30 writes per minute per account, shared by all your keys and bots; "one
   batch or multi-leg order request is one write" [SPEC info.description].
5. Losing your whole account value ends your prize chance (Smart Score effect unknown).
6. The rules mention position limits; we found no numbers [RULES\*].

**Leader versus chaser.** Let M = 3rd-place value ÷ your value, both from the leaderboard. Our
simulated 3rd-place median ran from 2x to 7.6x the start, depending on the field [SIM 11]: track the
real bar for weeks; it's provisional.
- **Leader** (top 3, late): cut variance relative to the field. Public trades carry no identities
  [SPEC components.schemas.ExchangeTrade], so infer the crowd's lean from prices, volume and
  leaderboard moves. A stylised cover [SIM 4]: at 400k, putting 178k into a 300k chaser's all-in YES at
  0.55 beats them either way. In practice you can't see the trade, it's 323,000 shares, and if NO wins
  you fall to 222k, below everyone in between. Cap any cover at 10–15%.
- **Chaser:** concentrate only late, after measuring the bar, with the smallest stake whose win clears
  it. Assume 2,000 entrants, 3% all-in on longshots (a bar near 7.4x), a +10% edge book first and a
  0.10 longshot truly worth 0.12: a 30% stake made the top 3 0.3% of the time vs 9.7% all-in [SIM 5].
  With 1,000 entrants and 0.5% lottery players (bar near 2.2x): 11.7% vs 11.8% [SIM 5, re-run]. All-in
  at 0.10 ends at zero (see rule 5) about 88% of the time; "bold-to-goal" betting ended below 0.5x in
  62% of runs [SIM 2].
- **If M is above about 10** (a judgement call): one 0.10 all-in pays about 10x, so you'd need a
  cheaper longshot or two hits. Aim for Smart Score instead.

Weigh this against "up to" prizes and a recruiter-visible Smart Score. The bot's risk modes never
change size [CODE strategy.py:1385-1387]; its "independent bets" advice is wrong for chasers [CODE
strategy.py:1196].

## 5. What NOT to do

One account per person; participation is "strictly individual, not team based"; no trading on material
non-public information; and the Sponsor may "void, reverse, or disqualify any order, trade, or
position" [RULES\*]. Organizers see "both sides' identity fields" on every trade [SPEC
paths./dmm/tournaments/{slug}/trades.get]. We never saw the full rules: treat all of these as banned.
- **Multiple accounts** or a friend's account (explicitly banned).
- **Collusion or moving value between accounts,** e.g. losing to a partner on purpose.
- **Wash trading.** Self-trade prevention blocks it within one account [SPEC paths./orders.post];
  across accounts it's multi-accounting. A similar contest bans "coordinating to simultaneously buy and
  sell the same contract to create artificial activity" [https://www.drw.com/market-madness].
- **Spoofing:** orders placed to fake demand, meant to be cancelled.
- **Pushing thin prices** to lift marks or the close. This is market manipulation. What it looks like
  [SIM 8]: against 2,000 late shares at 0.40, buying 3,000 at 0.70 lifts the VWAP to 0.58 and marks
  50,000 shares up 9,000. Organizers see identities, get `lastTradePrice` "for divergence-spotting"
  [SPEC paths./dmm/tournaments/{slug}/end-settlement.get], and can apply "retrospective settlement
  valuation corrections" [SPEC paths./dmm/tournaments/{slug}/settlement-overrides.post].

**Protecting yourself.** The Sponsor "has no duty or obligation to monitor, detect, investigate, or
prevent violations" [RULES\*]. So: don't chase one trader's spike. An order that rests unfilled or is
cancelled "changes `bookDirty` without adding a trade" [SPEC paths./realtime/token.post], so you can
see it; discount big ones that come and go (our advice). Against a push, rest limit asks well above
fair value, sized to hold to resolution (under a VWAP cash-out, a successful push marks that short
against you). Keep big thin positions out of the final 5 hours. Keep logs; report abuse
(predictionscup@sig.com, from a snippet).

## 6. Open questions, and your real data

Ask the organizers about the end mode, resolution sources and refunds, position limits, conduct
rules, valuation prices, D/R linking (`GET /relationships?tournamentId=`) and Smart Score weights.

Your data: `python -m supermarket_bot` (global flags first) with `--json tournaments --status active`
(`endDate`), `--json markets --status any`, `--json snapshot` (every price), `market <id>` (resolution
tree, settlement date), `book <market_id> --depth 20`, `leaderboard --period all --sort pnl --limit
100` (the bar), `portfolio` and `stream <market_id>`. Check Smart Score on the site (`GET
/tournaments/{slug}/me/smart-score`). Send us the `markets`, `snapshot` and `leaderboard` JSON to apply
this guide to your real markets.
