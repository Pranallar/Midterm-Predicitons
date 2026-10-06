/* page.route() fixtures for the Simulation view (CommonJS), built from docs/PAPER_TRADING.md §10:
 *
 *   paper(now, opts)      GET /api/paper (§10.1): a populated run; opts.empty (no step yet), opts.level
 *                         (the headline verdict level), opts.hasPrevious
 *   paperDisabled(now)    GET /api/paper when the dashboard runs with --no-paper
 *   previous(now)         GET /api/paper?run=previous (an ended run's final snapshot)
 *   fairvalue(now)        GET /api/fairvalue (§10.4), with a suspect match and its fix-a-match snippets
 *   backtest(now, status) GET /api/backtest (§10.3), status "ready" with a full report by default
 *   strategyIdeas(now)    value / basket / hole ideas with every optional field of §3 and §8.3
 *   strategySizing()      the strategy report's sizing block (explain() + "alternative")
 *
 * Constants follow the demo (§7.6): portfolio ids, headline human:conservative, bar 221,500, M 2.215.
 */
'use strict';

const CAVEATS = [
  'A day is a small sample, and trades on different races share one national polling factor, so they are not independent: treat any result as a hint, not proof.',
  'Value and carry ideas pay mostly when races are decided (November 3-4). A one-day test shows them at liquidation value, spread included, so it favours ideas that converge fast and understates ideas meant to be held to resolution.',
  'You will act by hand, minutes after a signal, against bots that react in seconds. The headline portfolio waits about 4 minutes before it can fill; the bot-speed portfolios (30 s) are an upper bound for manual copying, especially for baskets and liquidity holes.',
  'Simulated orders have no market impact: a real order of the same size would move the price against you and could be seen by other bots.',
  'Fills are simulated from order books read after each decision; real fills can be worse (other bots get there first, queue position) or occasionally better.',
  'Profit is shown at liquidation value: what selling every position into the bids you could see would fetch. Marks at the mid and the model\'s own valuation at fair value are higher and are shown only for reference.',
  'The Cup\'s end-of-tournament settlement rule is unknown, so positions still open at the end are valued conservatively rather than at 1.00.',
  'Portfolios are separate what-ifs that run on the same signals; they do not compete with each other for the same liquidity.',
  'Nothing was traded: the bot is read-only and every order here is simulated.',
];
const DEMO_CAVEAT = 'Demo data: the market is simulated and its mispricings are scripted (the demo\'s outside prices lead the Cup by design), so these results say nothing about real profitability.';
const TABLE_WARNING = 'The best of 9 portfolios looks better than it is by chance: judge the headline, and confirm any single kind on data recorded later.';
const FV_MODEL_LABEL = 'The model\'s own opinion of these positions (valued at the outside fair value that generated them): not evidence of profit.';
const CAN_SHOW = 'What one day can show: how often, and how fast, Cup prices move toward the outside fair value after a signal, net of the spread, over hundreds of signals.';
const CANNOT_SHOW = 'What it cannot show: whether the outside fair value is right about who wins (that pays only when races are decided, November 3-4), how prices behave on election night, or how the Cup\'s end-of-tournament settlement will work.';

const PORTFOLIOS = [
  ['human:conservative', 'You, acting by hand ~4 min late: all ideas, conservative sizing', 'conservative', null, 240],
  ['policy:conservative', 'Bot speed (30 s): all ideas, conservative sizing (quarter-Kelly, 8% cap)', 'conservative', null, 2],
  ['policy:chaser', 'Bot speed (30 s): all ideas, chaser sizing (goal-based)', 'chaser', null, 2],
  ['kind:value', 'Value vs outside fair value only', 'conservative', ['value'], 2],
  ['kind:basket', 'Pair and set baskets only', 'conservative', ['basket'], 2],
  ['kind:hole', 'Liquidity-hole resting orders only', 'conservative', ['hole'], 2],
  ['kind:fade', 'Fades of participant spikes only', 'conservative', ['fade'], 2],
  ['kind:carry', 'High-90s carry only', 'conservative', ['carry'], 2],
  ['kind:arbitrage', 'Engine-reported arbitrage only', 'conservative', ['arbitrage'], 2],
];
const PNL = [412.4, 1880.2, -2650.7, -310.5, 905.0, 64.0, 0, -12.3, 0];
const LEVELS = ['insufficient', 'promising', 'inconclusive', 'negative', 'promising', 'insufficient', 'insufficient', 'insufficient', 'insufficient'];

const SENTENCES = {
  insufficient: (p) => `Not enough evidence yet: 12 ideas entered (5 closed, 7 open) across 8 races in 6.0 h observed. A verdict needs at least 30 ideas, 20 independent groups of races and 2-hour periods, and 12 hours; P&L so far ${p} SUSQies at liquidation value.`,
  inconclusive: (p) => `Inconclusive: ${p} SUSQies at liquidation value over 14.0 h observed and 41 ideas (20 closed, 21 open), but the 90% interval for the average idea (-12.0 to +31.5 SUSQies) includes zero.`,
  promising: (p) => `Promising, not proof: ${p} SUSQies at liquidation value over 14.0 h observed and 44 ideas (25 closed, 19 open); the 90% interval for the average idea is +2.1 to +40.3 SUSQies, and the result stays positive without the best idea (+1,204). One day cannot separate an edge from one national swing: confirm it on a second day before acting.`,
  positive: (p) => `Profitable so far: ${p} SUSQies at liquidation value over 50.0 h observed on 2 days and 80 ideas; the 90% interval for the average idea is +3.0 to +21.0 SUSQies, positive on each of the last two days and without the best idea (+2,010).`,
  negative: (p) => `Losing so far: ${p} SUSQies at liquidation value over 14.0 h observed and 35 ideas (20 closed, 15 open); the 90% interval for the average idea is -40.1 to -3.2 SUSQies, entirely below zero.`,
};

function signedInt(x) {
  const r = Math.round(x);
  return (r >= 0 ? '+' : '-') + Math.abs(r).toLocaleString('en-US');
}

function verdict(pid, level, pnl, exploratory, hours) {
  const sentence = SENTENCES[level](signedInt(pnl));
  return {
    portfolio_id: pid, level, sentence: exploratory ? 'Exploratory (one of 9 portfolios, judged at the stricter 99% level): ' + sentence : sentence,
    hours_run: hours, closed_trades: 5, clusters: 6, pnl_liq: pnl, pnl_ex_best: pnl - 300, best_trade_pnl: 300, mean_trade_pnl: pnl / 12,
    ci_low: null, ci_high: null, ci_level: exploratory ? 0.989 : 0.9, win_rate: 0.6, win_rate_low: 0.23, win_rate_high: 0.88, max_drawdown: 0.004,
    thresholds: { min_hours: 12, min_ideas: 30, min_clusters: 20, positive_hours: 48, positive_days: 2 },
    reasons: ['only 6.0 h observed (needs 12)', 'only 12 ideas entered (needs 30)', 'only 6 independent groups (needs 20): 8 races, 6 time blocks',
      'Closed: 5 ideas, +210 realised; open: 7 ideas, +202 at liquidation value', 'Win rate 60% (3 of 5; closed trades only: biased toward quick winners)'],
    caveats: CAVEATS.slice(), exploratory, n_ideas: 12, open_ideas: 7, closed_pnl: 210, open_pnl_liq: 202, clusters_race: 8, clusters_time: 6, df: 5,
    return_mean: 0.01, return_ci_low: null, return_ci_high: null, days_covered: 0, wall_hours: hours + 0.4, unvalued_positions: pid === 'human:conservative' ? 1 : 0,
    unvalued_value: pid === 'human:conservative' ? 118.5 : 0, depth_unknown_share: 0.08, swing_share: 0.02, top_race_share: 0.3, synthetic_excluded: 0,
    synthetic_pnl: 0, untested: [],
  };
}

function barEstimate() {
  return {
    value: 221500, third_value: 221500, tenth_value: 107433.95, hundredth_value: null, growth_per_day: null, projected: false, method: 'third',
    explanation: ['Leaderboard values are mid-Cup marks (cash plus positions at current prices), not settled balances; thin-market marks can be inflated.',
      'Top-3 bar: 221,500 now (no growth estimate: less than 3 days of leaderboard history). Sizing uses the lower end.'],
    low: 221500, high: 236000, history_days: 0,
  };
}

function chaserSizing() {
  return {
    policy: 'chaser', label: 'Chaser: goal-based sizing to clear the top-3 bar', mode: 'chase', kept_by_hysteresis: true,
    lines: ['M = the bar / your value = 221,500 / 100,000 = 2.21',
      'A strategy whose expected multiple is 1.01x reaches 2.2x with probability at most 46%; with no edge (fair prices) the bound is 1/M = 45%.',
      'Mode: chase (1.3 < M <= 10 with more than 2 days left): full Kelly, at most 25% of equity per idea'],
    bar: barEstimate(), M: 2.215, markov_ceiling: 0.4562, ev_multiple: 1.0105,
  };
}

function conservativeSizing() {
  return {
    policy: 'conservative', label: 'Conservative: quarter-Kelly, at most 8% of equity per idea', mode: null,
    lines: ['Quarter-Kelly on the expected average fill, at most 8% of equity per idea, 12% per race, 60% in total',
      'A 3-point national polling miss may cost at most 10% of equity: races share one national polling error'],
    bar: barEstimate(), M: 2.215, markov_ceiling: 0.4515, ev_multiple: 1.0,
  };
}

function execution() {
  return {
    value: { entries: 6, filled: 4, fill_rate: 0.6667, avg_slippage: 0.0021, unfilled: 2, unfilled_pnl_now: 31.5, exits: 2, avg_exit_slippage: 0.001 },
    basket: { entries: 2, filled: 2, fill_rate: 1, avg_slippage: 0.0018, unfilled: 0, unfilled_pnl_now: null, exits: 1, avg_exit_slippage: 0.0 },
  };
}

function portfolioSummaries(now, opts) {
  const level = (opts && opts.level) || 'insufficient';
  return PORTFOLIOS.map((p, i) => {
    const [pid, label, policy, kinds, lat] = p;
    const pnl = PNL[i];
    const headline = i === 0;
    const lv = headline ? level : LEVELS[i];
    return {
      portfolio_id: pid, label, policy, kinds, start_capital: 100000, cash: 96000 + pnl, reserved_cash: 350, positions_liq: 4000, positions_mark: 4100,
      positions_fv: i < 4 ? 4300 : null, equity_liq: 100000 + pnl, equity_mark: 100000 + pnl + 120, equity_fv: i < 4 ? 100000 + pnl + 410 : null,
      realized_pnl: pnl / 2, unrealized_pnl_liq: pnl / 2, pnl_liq: pnl, pnl_liq_pct: pnl / 100000, pnl_mark: pnl + 822, max_drawdown: 0.0041 * (i + 1),
      max_drawdown_abs: 410 * (i + 1), fills: i === 6 || i === 8 ? 0 : 14, orders_open: 3, positions_open: i === 6 || i === 8 ? 0 : 4,
      trades_closed: 5, wins: 3, losses: 2, win_rate: i === 6 || i === 8 ? null : 0.6,
      by_kind: {}, exposure: null, sizing: policy === 'chaser' ? chaserSizing() : conservativeSizing(),
      verdict: verdict(pid, lv, pnl, !headline, headline && lv === 'positive' ? 50 : 6),
      last_step_at: now - 4, headline, exploratory: !headline, latency_s: lat, unvalued: headline ? 118.5 : 0,
      depth_unknown_share: headline ? 0.08 : 0, depth_stale_share: 0.05, swing_risk: 1200, execution: i === 6 || i === 8 ? {} : execution(),
      untested: [], legging_trades: 0, legging_pnl: 0,
      no_trade_reason: i === 6 ? 'No fade signals in 6.0 h: the market offered no such setup (that is not evidence of no edge).'
        : i === 8 ? 'No value trades yet: usable outside fair values on 0% of steps (see the fair-value status).' : null,
    };
  });
}

function equity(now, n) {
  const out = {};
  const start = now - 6 * 3600;
  PORTFOLIOS.forEach((p, i) => {
    const pts = [];
    for (let k = 0; k < n; k++) {
      const t = start + (k * 6 * 3600) / (n - 1);
      const f = k / (n - 1);
      // the last point is the current equity (the server appends equity_liq, ui-6), so it matches the headline
      const wobble = k === n - 1 ? 0 : Math.sin(k / 5 + i) * 60 * (i % 3 + 1);
      pts.push([t, Math.round((100000 + PNL[i] * f + wobble * f) * 100) / 100]);
    }
    out[p[0]] = pts;
  });
  return out;
}

function exitPlan(note) {
  return { kind: 'value', target_bid: 0.565, stop_bid: null, time_stop_ts: null, time_stop_after_fill_s: null, exit_before_ts: null, hold_to_resolution: false,
    dynamic_fv_target: true, exit_buffer: 0.005, min_set_profit: null, note };
}

function position(now, pid, eid, title, option, side, qty, avg, liq, flags, extra) {
  return Object.assign({
    position_id: pid + ':' + eid, portfolio_id: pid, exchange_id: eid, market_id: String(Number(eid) - 8708), side, qty, avg_cost: avg, cost: qty * avg,
    opened_at: now - 5000, idea_id: 'value:x' + eid + ':' + side, kind: 'value', edge_per_unit: 0.03, race_key: '2026:SENATE:TX', basket_id: null,
    exit_plan: exitPlan('Sell when the YES bid reaches 0.565, or hold to resolution'), realized_pnl: 0, collateral_advance: 0, status: 'open', flags,
    first_fill_at: now - 4980, updated_at: now - 30, liq_value: liq, mark_value: liq + 6, fv_value: liq + 20, last_marked_at: now - 30, title, option,
    depth_state: 'fresh', factor_delta: 0.057, score: 0.25, bold: false, floor_per_unit: null, synthetic: false, entered_at: now - 5010,
    unrealized_liq: liq - qty * avg, exit_note: 'Sell when the YES bid reaches 0.565, or hold to resolution', age_hours: 1.4,
    unvalued: false, last_liq_value: null,
  }, extra || {});
}

function paper(now, opts) {
  opts = opts || {};
  const empty = !!opts.empty;
  const ports = portfolioSummaries(now, opts);
  const hl = ports[0];
  const run = {
    run_id: 'run-' + Math.floor(now - 6 * 3600), started_at: now - 6.4 * 3600, hours_run: empty ? 0 : 6.0, wall_hours: empty ? 0 : 6.4,
    gaps: empty ? [] : [{ from: now - 3 * 3600, to: now - 3 * 3600 + 1440 }], target_hours: 24, progress: empty ? 0 : 0.25, complete: false,
    steps: empty ? 0 : 720, last_step_at: empty ? null : now - 4, last_step_seconds: empty ? null : 0.41, interval: 30, regime: 'unknown',
    all_collateral: false, sizing: 'conservative', start_capital: 100000, capital_source: 'default 100,000', fingerprint: 'abc123',
    code_version: '0.1.0+bb37b8755', settings: { sizing: 'conservative', regime: 'unknown', all_collateral: false, start_capital: 100000, params_changed: { min_value_edge: 0.03 } },
    ended_at: null, end_reason: null, final: null,
  };
  if (opts.complete) Object.assign(run, { hours_run: 24.1, wall_hours: 24.5, progress: 1, complete: true });
  const body = {
    now, enabled: true, available: true, error: null, demo: true, run, has_previous: opts.hasPrevious !== false,
    headline: empty ? null : {
      portfolio_id: hl.portfolio_id, label: hl.label, latency_s: 240, pnl_liq: hl.pnl_liq, pnl_liq_pct: hl.pnl_liq_pct, pnl_mark: hl.pnl_mark,
      equity_liq: hl.equity_liq, unvalued: 118.5, depth_unknown_share: 0.08, verdict: hl.verdict, verdict_caveats: CAVEATS.slice(0, 4),
      untested: [],
    },
    table_warning: TABLE_WARNING, model_label: FV_MODEL_LABEL, portfolios: empty ? [] : ports, equity: empty ? {} : equity(now, opts.points || 120),
    positions: empty ? [] : [
      position(now, 'human:conservative', '9026', 'Will the Democratic Party win the Texas Senate?', 'YES', 'yes', 3000, 0.535, 1665, []),
      position(now, 'human:conservative', '9028', 'Will the Democratic Party win the Iowa Senate?', 'YES', 'yes', 1500, 0.40, 496, ['depth_stale'], { depth_state: 'stale' }),
      position(now, 'policy:conservative', '9034', 'Will the Maine Senate candidates debate before October 7?', 'YES', 'yes', 800, 0.86, 0, ['closed_no_ruling'], { status: 'frozen', depth_state: 'unknown', unvalued: true, last_liq_value: 640 }),
      position(now, 'policy:chaser', '9003', 'Will Democrats win the Michigan Senate race?', 'NO', 'no', 21000, 0.31, 4200, ['depth_unknown', 'stale_quote'], { depth_state: 'unknown', bold: true }),
      position(now, 'kind:hole', '9033', 'Will the Republican Party win the Wyoming Governor?', 'YES', 'yes', 100, 0.70, 92.5, ['post_cup'], { kind: 'hole' }),
      position(now, 'kind:basket', '9024', 'Will the Democratic Party win the Nevada Governor?', 'NO', 'no', 300, 0.47, 138, [], { kind: 'basket', basket_id: 'kind:basket:b1', idea_id: 'basket:set:9024+9025:nn' }),
      position(now, 'kind:basket', '9025', 'Will the Republican Party win the Nevada Governor?', 'NO', 'no', 300, 0.48, 141, [], { kind: 'basket', basket_id: 'kind:basket:b1', idea_id: 'basket:set:9024+9025:nn' }),
    ],
    baskets: empty ? [] : [{ portfolio_id: 'kind:basket', basket_id: 'kind:basket:b1', idea_id: 'basket:set:9024+9025:nn', sets: 300, cost: 285, floor_value: 300,
      liq_value: 279, naked_qty: 0, legs_total: 2,
      legs: [{ exchange_id: '9024', side: 'no', qty: 300, liq_value: 138, naked_qty: 0 }, { exchange_id: '9025', side: 'no', qty: 300, liq_value: 141, naked_qty: 0 }] }],
    orders: [],
    fills: empty ? [] : [
      { fill_id: 'human:conservative:f3', order_id: 'human:conservative:o3', portfolio_id: 'human:conservative', exchange_id: '9026', market_id: '318', side: 'yes',
        action: 'buy', qty: 300, price: 0.695, ts: now - 600, liquidity: 'taker', purpose: 'entry', kind: 'value', idea_id: 'value:x9026:yes', book_at: now - 602,
        latency_s: 251, levels: [[0.69, 200], [0.705, 100]], trade_ids: [], reason: 'The outside fair value 0.580 is above the Cup ask 0.535', group_id: null,
        title: 'Will the Democratic Party win the Texas Senate?', option: 'YES', slippage: 0.005, synthetic: false },
      { fill_id: 'kind:basket:f2', order_id: 'kind:basket:o2', portfolio_id: 'kind:basket', exchange_id: '9025', market_id: '317', side: 'no', action: 'sell', qty: 300,
        price: 0.52, ts: now - 900, liquidity: 'taker', purpose: 'exit', kind: 'basket', idea_id: 'basket:set:9024+9025:nn', book_at: now - 905, latency_s: 3,
        levels: [[0.52, 300]], trade_ids: [], reason: 'Converged', group_id: 'kind:basket:b0', title: 'Will the Republican Party win the Nevada Governor?', option: 'YES',
        slippage: -0.002, synthetic: false },
      { fill_id: 'policy:conservative:f9', order_id: 'policy:conservative:o9', portfolio_id: 'policy:conservative', exchange_id: '9034', market_id: '326', side: 'yes',
        action: 'settle', qty: 800, price: 1.0, ts: now - 1200, liquidity: 'settlement', purpose: 'settlement', kind: 'value', idea_id: 'value:x9034:yes', book_at: null,
        latency_s: null, levels: [], trade_ids: [], reason: 'Settled YES', group_id: null, title: 'Will the Maine Senate candidates debate before October 7?', option: 'YES',
        slippage: null, synthetic: false },
    ],
    trades: empty ? [] : [
      { trade_id: 'kind:basket:t1', portfolio_id: 'kind:basket', idea_id: 'basket:set:9024+9025:nn', kind: 'basket', exchange_ids: ['9024', '9025'], opened_at: now - 9000,
        closed_at: now - 900, qty: 300, cost: 285, proceeds: 301.5, pnl: 16.5, exit_reason: 'converged', race_key: '2026:GOVERNOR:NV', title: 'Nevada Governor (NO basket)',
        legs: [], return_pct: 0.0579, hold_hours: 2.25, profit_per_capital_day: 0.62, synthetic: false, entered_at: now - 9010, direction: 'N' },
      { trade_id: 'policy:conservative:t4', portfolio_id: 'policy:conservative', idea_id: 'value:x9034:yes', kind: 'value', exchange_ids: ['9034'], opened_at: now - 8000,
        closed_at: now - 1200, qty: 800, cost: 688, proceeds: 800, pnl: 112, exit_reason: 'settled', race_key: null, title: 'Will the Maine Senate candidates debate before October 7?',
        legs: [], return_pct: 0.163, hold_hours: 1.9, profit_per_capital_day: 2.06, synthetic: false, entered_at: now - 8010, direction: 'N' },
      { trade_id: 'kind:value:t2', portfolio_id: 'kind:value', idea_id: 'value:x9028:yes', kind: 'value', exchange_ids: ['9028'], opened_at: now - 7000,
        closed_at: now - 1500, qty: 1500, cost: 600, proceeds: 465, pnl: -135, exit_reason: 'fair_value', race_key: '2026:SENATE:IA', title: 'Will the Democratic Party win the Iowa Senate?',
        legs: [], return_pct: -0.225, hold_hours: 1.5, profit_per_capital_day: -3.6, synthetic: false, entered_at: now - 7010, direction: 'D' },
    ],
    signals: { count: 212, by_kind: { value: 4, basket: 2, hole: 4 }, at: now - 4 },
    study: empty ? null : {
      kinds: {
        value: { events: 212, traded: 31, horizons: {
          '5m': { n: 212, mean: 0.004, ci_low: -0.001, ci_high: 0.009, ci_level: 0.9, clusters: 14, share_positive: 0.55, share_converged: 0.41, share_reversed: 0.12 },
          '30m': { n: 190, mean: 0.009, ci_low: 0.002, ci_high: 0.016, ci_level: 0.9, clusters: 13, share_positive: 0.6, share_converged: 0.52, share_reversed: 0.15 },
          '2h': { n: 120, mean: 0.011, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 8, share_positive: 0.62, share_converged: 0.58, share_reversed: 0.18 },
          '6h': { n: 0, mean: null, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 0, share_positive: null, share_converged: null, share_reversed: null } } },
        basket: { events: 9, traded: 9, horizons: {
          '5m': { n: 9, mean: 0.012, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 3, share_positive: 0.78, share_converged: 0.33, share_reversed: null },
          '30m': { n: 8, mean: 0.018, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 3, share_positive: 0.88, share_converged: 0.5, share_reversed: null },
          '2h': { n: 5, mean: 0.02, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 2, share_positive: 1, share_converged: 0.8, share_reversed: null },
          '6h': { n: 0, mean: null, ci_low: null, ci_high: null, ci_level: 0.9, clusters: 0, share_positive: null, share_converged: null, share_reversed: null } } },
      },
      pending: 40, dropped: 0, can_show: CAN_SHOW, cannot_show: CANNOT_SHOW,
    },
    budget: { reads_used: 14, reads_limit: 20, reads_room: 6, book_reads_last_step: 3, trade_reads_last_step: 1, reads_skipped_last_step: 0,
      max_book_reads_per_step: 8, max_trade_reads_per_step: 2, sets_deferred_last_step: 0, tape_gaps_last_step: 0 },
    fair_value: { mode: 'auto', enabled: true, usable: 6, total: 33 },
    caveats: CAVEATS.concat([DEMO_CAVEAT]),
    last_step: empty ? null : { now: now - 4, step: 720, fills: 1, orders_created: 2, orders_expired: 0, orders_cancelled: 0, settled: 0, exits_started: 0, signals: 10,
      book_reads: 3, trade_reads: 1, reads_skipped: 0, errors: [], duration_s: 0.41, sets_deferred: 0, tape_gaps: 0, gap_s: null },
  };
  return body;
}

function paperDisabled(now) {
  return { now, enabled: false, available: false, error: 'The paper trader is off (started with --no-paper).', demo: false, run: null, has_previous: false,
    headline: null, table_warning: '', model_label: '', portfolios: [], equity: {}, positions: [], baskets: [], orders: [], fills: [], trades: [],
    signals: null, study: null, budget: null, fair_value: null, caveats: [], last_step: null };
}

function previous(now) {
  const body = paper(now, { level: 'promising' });
  const ports = body.portfolios;
  const verdicts = {};
  for (const p of ports) verdicts[p.portfolio_id] = p.verdict;
  body.run = Object.assign({}, body.run, {
    run_id: 'run-previous-1', ended_at: now - 7200, end_reason: 'reset',
    final: { at: now - 7200, reason: 'reset', verdicts, portfolios: ports, study: body.study },
  });
  body.orders = [];
  body.fills = [];
  body.equity = {};
  return body;
}

function fvRow(eid, title, option, race, party, bid, ask, fair, extra) {
  const mid = bid != null && ask != null ? (bid + ask) / 2 : null;
  const row = {
    exchange_id: eid, market_id: String(Number(eid) - 8708), title, option, race_key: race, party, sm_bid: bid, sm_ask: ask, sm_mid: mid,
    fair, matches: [], gap: fair && fair.value != null && mid != null ? Math.round((fair.value - mid) * 1000) / 1000 : null,
    edge_yes: fair && fair.value != null && ask != null ? Math.round((fair.value - ask) * 1000) / 1000 : null,
    edge_no: fair && fair.value != null && bid != null ? Math.round((bid - fair.value) * 1000) / 1000 : null,
    near: false, suspect: false,
    snippets: { disable: '"' + eid + '": {"disabled": true, "note": "wrong match"}', pin: '"' + eid + '": {"polymarket": "<Polymarket market id>", "kalshi": "<Kalshi ticker>", "confirmed": true}', confirm: null, trade_near: null },
    unmatched_reason: null,
  };
  return Object.assign(row, extra || {});
}

function fair(eid, value, source, usable, reason, extra) {
  return Object.assign({ exchange_id: eid, value, source, confidence: 'high', usable, reason, bid: value - 0.005, ask: value + 0.005, as_of: 0, agreement: 0.01,
    sources: [], race_key: null, party: null, match_kind: 'EXACT', match_confidence: 0.95, note: null, manual_updated_at: null, uncertainty: 0.01,
    prev_value: value, prev_as_of: null, suspect: false, history: false, age_s: 42 }, extra || {});
}

function fairvalue(now) {
  const rows = [
    fvRow('9026', 'Will the Democratic Party win the Texas Senate?', 'YES', '2026:SENATE:TX', 'D', 0.53, 0.54, fair('9026', 0.58, 'blend', true, 'Polymarket and Kalshi agree within 1 cent'), {
      matches: [{ venue: 'polymarket', exchange_id: '9026', external_id: '630844', label: 'Colin Allred (D) - Texas Senate Election Winner', kind: 'EXACT', confidence: 0.95,
        reason: 'pinned id 630844 (participant map), validated: active, 2026', source: 'pin', validated_at: now - 300, token_id: null },
      { venue: 'kalshi', exchange_id: '9026', external_id: 'SENATETX-26-D', label: 'Democrats win the Texas Senate race', kind: 'EXACT', confidence: 0.9, reason: 'ticker encodes the race', source: 'pin', validated_at: now - 300, token_id: null }],
    }),
    fvRow('9031', 'Will the Republican Party win the Nebraska Senate?', 'YES', '2026:SENATE:NE', 'R', 0.64, 0.66, fair('9031', 0.31, 'polymarket', false, 'Suspect: 0.34 away from the Cup mid; confirm the match before it is traded', { suspect: true }), {
      suspect: true,
      matches: [{ venue: 'polymarket', exchange_id: '9031', external_id: '512301', label: 'Dan Osborn (I) - Nebraska Senate Election Winner', kind: 'EXACT', confidence: 0.6,
        reason: 'discovered by the race name; the party does not match', source: 'discovery', validated_at: now - 300, token_id: null }],
      snippets: { disable: '"9031": {"disabled": true, "note": "wrong match"}', pin: '"9031": {"polymarket": "<Polymarket market id>", "kalshi": "<Kalshi ticker>", "confirmed": true}',
        confirm: '"9031": {"polymarket": "512301", "confirmed": true}', trade_near: null },
    }),
    fvRow('9016', 'Which party will control the Senate after the midterms?', 'Republicans', '2026:SENATE_CONTROL:US', 'R', 0.695, 0.705, fair('9016', 0.71, 'kalshi', false, 'Near match: shown, not traded', { match_kind: 'NEAR' }), {
      near: true,
      matches: [{ venue: 'kalshi', exchange_id: '9016', external_id: 'CONTROLS-26-R', label: 'Republicans control the Senate after the 2026 election', kind: 'NEAR', confidence: 0.7, reason: 'chamber control: settlement wording differs', source: 'pin', validated_at: now - 300, token_id: null }],
      snippets: { disable: '"9016": {"disabled": true}', pin: null, confirm: null, trade_near: '"9016": {"trade_near": true}' },
    }),
    fvRow('9018', 'How many House seats will Democrats win?', 'Fewer than 205', null, null, 0.08, 0.09, null, { unmatched_reason: 'The title is not an election race the matcher understands.', snippets: { disable: '"9018": {"disabled": true}', pin: null, confirm: null, trade_near: null } }),
  ];
  return {
    now, enabled: true, mode: 'auto', last_refresh_at: now - 40,
    providers: [
      { name: 'polymarket', status: 'offline', last_ok_at: null, last_error: 'Polymarket is unreachable from this machine (name resolution failed: [Errno -3] Temporary failure in name resolution): no outside fair value from Polymarket.', requests: 0, matched: 0, quoted: 0, next_try_at: now + 240 },
      { name: 'kalshi', status: 'ok', last_ok_at: now - 40, last_error: null, requests: 3, matched: 12, quoted: 11, next_try_at: null },
    ],
    manual: { path: '/home/user/.supermarket/2026-midterms/fair_values.json', exists: true, entries: 2, errors: ['line 4: probability 1.4 is not between 0 and 1'] },
    map: { path: '/home/user/.supermarket/2026-midterms/fair_value_map.json', exists: true, overrides: 3, loaded_at: now - 50, errors: [] },
    history: null,
    counts: { outcomes: 4, matched: 3, usable: 1, manual: 0, no_external: 1, suspect: 1, near: 1 },
    rows,
    caveats: [
      'Outside prices are other markets\' opinions, not the truth: they carry their own biases (longshots tend to be overpriced) and fees.',
      'Polymarket resolves on media calls or certification and Kalshi on swearing-in; the Cup\'s own resolution rule may differ.',
      'Chamber control and Independent legs are near matches only: their settlement wording differs, so they are shown but not traded unless you enable them.',
      'A fair value is traded only when the gap exceeds its own uncertainty (venue disagreement, spread and fees) plus the minimum edge.',
    ],
  };
}

function backtest(now, status) {
  status = status || 'ready';
  if (status !== 'ready') return { now, status, error: status === 'error' ? 'The backtest failed: database is locked' : status === 'no_data' ? 'Less than 1 hour of prices is stored yet: nothing to replay.' : null, started_at: now - 30, generated_at: null, report: null };
  const p = paper(now, { level: 'inconclusive' });
  const verdicts = {};
  for (const x of p.portfolios) verdicts[x.portfolio_id] = x.verdict;
  return {
    now, status: 'ready', error: null, started_at: now - 100, generated_at: now - 60,
    report: {
      generated_at: now - 60, config: { hours: 24, step_s: 300, latency_s: 60 },
      window: { start: now - 24 * 3600, end: now - 60, hours: 24, steps: 288 },
      coverage: { exchanges: 33, decision_steps: 288, tick_share: 0.62, candle_share: 0.38, book_snapshot_share: 0.4, synthetic_book_share: 0.6,
        fair_value_share: 0, fair_value_recorded_share: 0, fair_value_imported_share: 0, tape_share: 0.2, maker_fills_tape: 1, maker_fills_candle: 2,
        set_tick_share: 0.55, guard_filtered: 1234 },
      assumptions: ['Decisions every 300 s; fills use the first order book observed at least 60 s after a decision (the headline portfolio, you acting by hand, waits 240 s)',
        'Depth before the bot started recording is synthetic (one level, a share of recent volume), so the backtest cannot tell the sizing policies apart: compare them in the live run.',
        'Engine-reported arbitrage cannot be replayed: the engine\'s constraints and overround rows are not stored historically.'],
      portfolios: p.portfolios, equity: {}, trades: [], verdicts,
      sweep: [{ params: { latency_s: 30 }, label: 'latency_s=30', pnl_liq: 210, trades_closed: 9, verdict_level: 'insufficient', max_drawdown: 0.004 },
        { params: { latency_s: 300 }, label: 'latency_s=300', pnl_liq: -40, trades_closed: 6, verdict_level: 'insufficient', max_drawdown: 0.006 }],
      warnings: ['Only 9 hours of data',
        '6.0 h of this window were also seen by a paper run (the current one, or one that ended or was reset): a backtest over the same data is not an independent check, so agreement between the two is not evidence.',
        'The best of 2 settings is an optimistic estimate (it was picked after seeing the results); prefer settings whose neighbours also do well, and confirm them on data recorded later.'],
      testability: {
        value: { status: 'not_testable', hours: 0, sentence: 'value: not testable yet (0 h of outside prices recorded or imported in this window); run `fairvalue --import-history` or let the bot record for a day' },
        basket: { status: 'partial', hours: 14, sentence: 'basket: 14.0 h of tick quotes; candle-only hours cannot price a set' },
        hole: { status: 'partial', hours: 6, sentence: 'hole: 6.0 h with stored tape or real order books; 18.0 h candle-only' },
        fade: { status: 'partial', hours: 6, sentence: 'fade: 6.0 h with stored tape or real order books; 18.0 h candle-only' },
        carry: { status: 'testable', hours: 24, sentence: 'carry: testable from quotes, but it pays at resolution: a one-day replay shows only its liquidation value' },
        arbitrage: { status: 'not_replayable', hours: 0, sentence: 'Engine-reported arbitrage cannot be replayed: the engine\'s constraints and overround rows are not stored historically.' },
      },
      study: p.study, overlap_hours: 6, stopped_early: false,
    },
  };
}

function bet(kind, p, gain, loss, cost, extra) {
  return Object.assign({ kind, p, gain, loss, cost, floor: null, cap_pct: null, tail_prob: null }, extra || {});
}

function strategyIdeas(now) {
  const base = { target_price: null, stop_price: null, horizon_hours: 24, risks: ['The outside price can be wrong'], settles_before_cup_end: false, surge_id: null,
    unit: 'shares', depth_checked: true, fill_price: null, settlement_regime: 'unknown', levels: [], called_prob: null, priced_from: 'tick', legs: [] };
  return [
    Object.assign({}, base, {
      kind: 'value', exchange_id: '9026', market_id: '318', title: 'Will the Democratic Party win the Texas Senate?', option: 'YES', side: 'yes', entry_price: 0.54,
      prob_win: 0.58, edge: 0.03, expected_return: 0.0556, suggested_shares: 2400, suggested_cost: 1284, score: 0.25, confidence: 0.7,
      rationale: ['The outside fair value 0.580 (Polymarket and Kalshi) is above the Cup ask 0.540'], idea_id: 'value:x9026:yes', race_key: '2026:SENATE:TX',
      bet: bet('binary', 0.57, 0.46, 0.54, 0.54), bet_limit: bet('binary', 0.57, 0.465, 0.535, 0.535), exit_plan: exitPlan('Sell when the YES bid reaches 0.565 (fair value less half the spread), or hold to resolution'),
      order_type: 'taker', limit_price: 0.535, expires_at: null, max_units: 2400, fair_value: 0.58, fair_source: 'blend', fv_uncertainty: 0.01, factor_delta: 0.0567,
      profit_per_capital_day: 0.0021,
      sizing: { idea_id: 'value:x9026:yes', policy: 'conservative', units: 2400, stake: 1284, kelly_fraction: 0.05, capped_by: ['certainty'], mode: null,
        lines: ['Conservative sizing: 2,400 shares for about 1,284 SUSQies', 'Quarter-Kelly on the expected average fill 0.535'], avg_cost: 0.535, certainty: 0.9, factor_scale: null, bold: false, replaces: null },
      alt_sizing: { idea_id: 'value:x9026:yes', policy: 'chaser', units: 9100, stake: 4868, kelly_fraction: 0.2, capped_by: ['swing_cap'], mode: 'chase',
        lines: ['Chaser sizing (chase): 9,100 shares for about 4,868 SUSQies'], avg_cost: 0.535, certainty: 0.9, factor_scale: 0.5, bold: false, replaces: null },
    }),
    Object.assign({}, base, {
      kind: 'basket', exchange_id: null, market_id: '316', title: 'Nevada Governor', option: null, side: 'no', entry_price: 0.95, prob_win: 1, edge: 0.05,
      expected_return: 0.0526, suggested_shares: 800, suggested_cost: 760, score: 0.3, confidence: 0.95, unit: 'sets',
      rationale: ['The YES bids of the 2 outcomes sum to 1.050, above 1.00: buying NO on every leg costs 0.950 per set and pays exactly 1.00 at a 1/0 settlement'],
      legs: [
        { exchange_id: '9024', market_id: '316', title: 'Will the Democratic Party win the Nevada Governor?', option: 'YES', side: 'no', price: 0.40, limit: 0.41, race_key: '2026:GOVERNOR:NV', party: 'D', yes_bid: 0.60, yes_ask: 0.61, quote_ts: now - 5 },
        { exchange_id: '9025', market_id: '317', title: 'Will the Republican Party win the Nevada Governor?', option: 'YES', side: 'no', price: 0.55, limit: 0.56, race_key: '2026:GOVERNOR:NV', party: 'R', yes_bid: 0.45, yes_ask: 0.46, quote_ts: now - 5 },
      ],
      idea_id: 'basket:set:9024+9025:nn', race_key: '2026:GOVERNOR:NV', bet: bet('bounded', 0.99, 0.05, 0.55, 0.95, { floor: 1, tail_prob: 0.01 }),
      bet_limit: bet('bounded', 0.99, 0.03, 0.56, 0.97, { floor: 1, tail_prob: 0.01 }),
      exit_plan: { kind: 'basket', target_bid: null, stop_bid: null, time_stop_ts: null, time_stop_after_fill_s: null, exit_before_ts: null, hold_to_resolution: false,
        dynamic_fv_target: false, exit_buffer: 0, min_set_profit: 0.025, note: 'Sell the set when selling every leg brings at least +0.025 per set over its cost; otherwise hold to settlement' },
      order_type: 'taker', limit_price: 0.97, expires_at: null, max_units: 800, fair_value: null, fair_source: null, fv_uncertainty: null, factor_delta: 0,
      profit_per_capital_day: 0.0018,
      sizing: { idea_id: 'basket:set:9024+9025:nn', policy: 'conservative', units: 800, stake: 760, kelly_fraction: null, capped_by: ['depth'], mode: null, lines: ['Conservative sizing: 800 sets for about 760 SUSQies'], avg_cost: 0.95, certainty: null, factor_scale: null, bold: false, replaces: null },
      alt_sizing: null,
    }),
    Object.assign({}, base, {
      kind: 'hole', exchange_id: '9033', market_id: '325', title: 'Will the Republican Party win the Wyoming Governor?', option: 'YES', side: 'yes', entry_price: 0.695,
      prob_win: 0.02, edge: 0.004, expected_return: 0.006, suggested_shares: 500, suggested_cost: 347.5, score: 0.003, confidence: 0.5,
      rationale: ['YES is a liquid favourite at 0.93: a resting buy far below can catch a careless market order that sweeps a thin book (a liquidity hole)'],
      idea_id: 'hole:x9033:yes', race_key: '2026:GOVERNOR:WY', bet: bet('fixed', 0.02, 0.205, 0.695, 0.695, { cap_pct: 0.01 }), bet_limit: bet('fixed', 0.02, 0.205, 0.695, 0.695, { cap_pct: 0.01 }),
      exit_plan: { kind: 'hole', target_bid: 0.9, stop_bid: null, time_stop_ts: null, time_stop_after_fill_s: 43200, exit_before_ts: null, hold_to_resolution: false,
        dynamic_fv_target: false, exit_buffer: 0, min_set_profit: null, note: 'If it fills, sell when the bid recovers to 0.900, or after 12 h' },
      order_type: 'maker', limit_price: 0.695, expires_at: now + 6 * 3600, max_units: 500, fair_value: null, fair_source: null, fv_uncertainty: null, factor_delta: -0.012,
      profit_per_capital_day: null, sizing: null, alt_sizing: null,
    }),
  ];
}

function strategySizing() {
  return Object.assign(conservativeSizing(), { alternative: chaserSizing() });
}

module.exports = { paper, paperDisabled, previous, fairvalue, backtest, strategyIdeas, strategySizing, CAVEATS, DEMO_CAVEAT, TABLE_WARNING, FV_MODEL_LABEL, CAN_SHOW, CANNOT_SHOW, PORTFOLIOS };
