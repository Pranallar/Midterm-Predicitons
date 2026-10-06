/* page.route() fixtures for the "Outside moves" view (CommonJS), built from the JSON shapes of
 * docs/OUTSIDE_MOVES.md §18 (GET /api/moves, /api/status additions, the §18.4 annotations) and the exact
 * texts of supermarket_bot/moves.py (§21). Every time is relative to `now` (epoch seconds) so the
 * relative ages ("2 min ago") are stable whatever the clock says; alert ids are fixed strings (the
 * sound and notification checks compare ids across polls).
 *
 *   const MF = require('./moves_fixtures.cjs');
 *   MF.body(now, { alerts: [MF.alertL(now)], summary: 'small' })   // a full /api/moves body
 *   MF.disabled(now)                                                // the body when the watcher is off
 *   MF.statusMoves(now, [MF.alertL(now)])                           // /api/status "moves" (badge, notifications)
 */
'use strict';

const CAVEATS = [
  'Outside prices can be wrong, thin or briefly stale: an alert is a prompt to look, not a signal to trade blindly.',
  'A gap the Cup has not closed may stay open until the race is decided; a move the Cup has not followed is not a guaranteed profit.',
  'You act by hand, minutes later: other participants and bots may move the Cup first, and your own order moves a thin Cup book.',
  "Polymarket's prices carry no quote time, so every outside quote is stamped with the time we received it; a move may have happened up to one poll (about 15 s) earlier.",
  "The Cup is read once per snapshot interval (30 s by default), so when it followed is known only to within that interval: a measured lag can be up to one interval longer than the real one, and 'followed within 1 min' is rough.",
  'Lag statistics from a few days are a small sample, and moves cluster on news days and share national swings: treat them as a hint, not proof.',
  'The bot is read-only: it never places an order. Every trade suggestion is for you to judge and place by hand.',
];
const DEMO_CAVEAT = "Demo data: the outside moves and the Cup's reactions are scripted, so these alerts and lag statistics say nothing about the real Cup.";
const TRADE_NOTE = 'Suggestion only, not a sure thing: the outside price can be wrong or thin, the Cup may never follow (the gap can last until the race is decided), and others may trade first. Nothing is traded by the bot.';
const OFF_ERROR = 'Outside-move alerts are off: start the dashboard with --fair-value auto and without --no-moves to watch Polymarket and Kalshi.';
const NO_DATA_SENTENCE = 'No outside move has been measured yet.';
const SUPPRESS_REASONS = ['thin', 'single_tick', 'disagree', 'near', 'suspect', 'stale', 'placeholder', 'match', 'no_cup'];
const SUPPRESS_LABELS = {
  thin: 'thin or wide outside book',
  single_tick: 'a single print that did not hold',
  disagree: 'the other venue did not move',
  near: 'near match (different settlement wording)',
  suspect: 'suspect match (far from the Cup price)',
  stale: 'stale or interrupted outside quotes',
  placeholder: 'placeholder or one-sided outside quote',
  match: 'match confidence too low',
  no_cup: 'no recent Cup price to compare with',
};
const STATUS_LABELS = { lagging: 'Cup lagging', already_moved: 'Cup already moved', moved_first: 'Cup moved first', reverted: 'Outside reverted' };
const THRESHOLDS = {
  windows_s: [60, 300, 900, 3600], abs_min: { 60: 0.03, 300: 0.04, 900: 0.05, 3600: 0.07 }, k_sigma: 4.0, warmup_factor: 1.5, max_spread: 0.05,
  min_liquidity_usd: 5000.0, min_top_size: 100.0, follow_fraction: 0.5, lag_track_s: 3600.0, human_delay_s: 240.0, exit_after_s: 1800.0, min_edge: 0.01,
};
// the §10.2 example: 46 outside-led moves on 21 races
const LARGE_SENTENCE = 'Over 46 outside moves on 21 races, the Cup followed within 5 minutes 41% of the time and within an hour 72%; the median lag was 3.5 min after the outside move (1.5 min after the alert). Acting 4 minutes after an alert left an average gap of +0.004 per share net of the spread (n=40); buying then and selling 30 minutes later averaged -0.002 per share (n=37). Moves cluster on news days and races share national swings, so treat this as a hint, not proof.';
const SMALL_SENTENCE = 'Only 6 outside moves on 5 races so far (2 followed by the Cup, 2 never, 1 where the Cup moved first): far too few to say whether the Cup lags. Wait for at least 20 moves on 10 races.';

function clone(x) {
  return JSON.parse(JSON.stringify(x));
}

function quote(ts, bid, ask, last) {
  return { ts, bid, ask, mid: Math.round(((bid + ask) / 2) * 1e6) / 1e6, last: last == null ? bid : last, spread: Math.round((ask - bid) * 1e6) / 1e6 };
}

function venueMove(venue, label, w, baseTs, base, after, now, extra) {
  return Object.assign({
    venue, label, window_s: w, base_ts: baseTs, base_value: base, after_ts: baseTs + w, after_value: after, now_value: after,
    move: Math.round((after - base) * 1e6) / 1e6, threshold: 0.04, sigma: 0.008, vol_known: true, ratio: 1.5, spread_base: 0.01,
    spread_now: 0.01, thin: false, stale: false, gap: false, flags: [], t_half: baseTs + 160,
  }, extra || {});
}

function lag(extra) {
  return Object.assign({
    eligible: true, outcome: 'pending', t_move: null, followed_at: null, lag_s: null, lag_after_alert_s: null, resolved_at: null, reason: '',
    capture_4m: null, capture_at: null, exit_30m: null, exit_at: null, excluded_reason: null,
  }, extra || {});
}

/** The §18.2 Example L alert (North Carolina Senate D): lagging, actionable, a trade with depth from a book read 2 s ago. */
function alertL(now, over) {
  const tBase = now - 315;
  const tMove = now - 155;
  const det = now - 120;
  const base = quote(tBase - 15, 0.505, 0.52, 0.51);
  const a = {
    alert_id: 'mv-1104-1791208860', exchange_id: '1104', market_id: '552', title: 'Will the Democratic Party win the North Carolina Senate?',
    option: 'YES', race_key: '2026:SENATE:NC', party: 'D', race_label: 'North Carolina Senate (D)', direction: 1,
    window_s: 300.0, windows: [300.0], venues: ['polymarket', 'kalshi'], confirmation: 'two venues',
    venue_moves: [
      venueMove('polymarket', 'Polymarket', 300, tBase, 0.52, 0.58, now, { threshold: 0.04, sigma: 0.008, ratio: 1.5 }),
      venueMove('kalshi', 'Kalshi', 300, tBase, 0.515, 0.565, now, { threshold: 0.044, sigma: 0.011, ratio: 1.136364, spread_base: 0.02, spread_now: 0.02 }),
    ],
    t_base: tBase, t_move: tMove, outside_before: 0.5175, outside_after: 0.5725, outside_now: 0.5725,
    move: 0.055, peak_move: 0.055, threshold: 0.044, sigma: 0.011, vol_known: true, uncertainty: 0.02,
    detected_at: det, updated_at: now - 5, grown_at: det, state: 'open', status: 'lagging', status_label: 'Cup lagging',
    reason: 'The outside price moved +5.5 pts in 5 min; the Cup has moved +0.0 pts over the same time (now 0.512).',
    cup: { base, ref: quote(tBase - 900, 0.505, 0.52, 0.51), now: quote(det - 15, 0.505, 0.52, 0.51), move_same_window: 0.0, move_lookback: 0.0, half_cross_ts: null, short_history: false },
    lag_gap: 0.055, lag: lag({ t_move: tMove }),
    closed_at: null, cup_now: quote(now - 10, 0.505, 0.52, 0.51), lag_gap_now: 0.055, level_gap_now: 0.06,
    trade: {
      side: 'yes', action: 'buy', text: 'Buy YES at 0.520', limit: 0.52, max_limit: 0.525, outside_value: 0.565, uncertainty: 0.02, cup_half_spread: 0.0075,
      edge_per_share: 0.0375, edge_after_uncertainty: 0.0175, edge_at_resolution: 0.045, shares_at_limit: 300.0, shares_to_max: 750.0,
      book_source: 'read', book_age_s: 2.0, computed_at: now, note: TRADE_NOTE,
    },
    trade_note: null, actionable: true, linked: [], flags: [], demo: false, opened_status: 'lagging', opened_trade: null, opened_trade_note: null,
  };
  return Object.assign(a, over || {});
}

/** A lagging NO-side alert (the demo's lead_long numbers) whose depth is unknown (no recent Cup book). */
function alertNoBook(now, over) {
  const a = alertL(now);
  const tBase = now - 400;
  Object.assign(a, {
    alert_id: 'mv-9036-1791208920', exchange_id: '9036', market_id: '328', title: 'Will the Republican Party win the Ohio Senate?',
    race_key: '2026:SENATE:OH', party: 'R', race_label: 'Ohio Senate (R)', direction: -1, window_s: 60.0, windows: [60.0, 300.0],
    venues: ['demo-a', 'demo-b'],
    venue_moves: [
      venueMove('demo-a', 'Demo venue A', 60, tBase, 0.58, 0.52, now, { threshold: 0.045, sigma: null, vol_known: false, ratio: 1.33, flags: ['warm-up thresholds'] }),
      venueMove('demo-b', 'Demo venue B', 60, tBase, 0.58, 0.525, now, { threshold: 0.045, sigma: null, vol_known: false, ratio: 1.22, flags: ['warm-up thresholds'] }),
    ],
    t_base: tBase, t_move: tBase + 30, outside_before: 0.58, outside_after: 0.5225, outside_now: 0.5225, move: 0.0575, peak_move: 0.0575,
    detected_at: now - 360, grown_at: now - 360, uncertainty: 0.005,
    reason: 'The outside price moved -5.8 pts in 1 min; the Cup has moved +0.0 pts over the same time (now 0.580).',
    cup: { base: quote(tBase - 10, 0.57, 0.59), ref: quote(tBase - 900, 0.57, 0.59), now: quote(now - 370, 0.57, 0.59), move_same_window: 0.0, move_lookback: 0.0, half_cross_ts: null, short_history: true },
    cup_now: quote(now - 4, 0.57, 0.59), lag_gap: 0.0575, lag_gap_now: 0.0575, level_gap_now: 0.0575, lag: lag({ t_move: tBase + 30 }),
    trade: {
      side: 'no', action: 'buy', text: 'Buy NO at 0.430', limit: 0.43, max_limit: 0.45, outside_value: 0.475, uncertainty: 0.005, cup_half_spread: 0.01,
      edge_per_share: 0.035, edge_after_uncertainty: 0.03, edge_at_resolution: 0.045, shares_at_limit: null, shares_to_max: null,
      book_source: null, book_age_s: null, computed_at: now, note: TRADE_NOTE,
    },
    flags: ['warm-up thresholds', 'short Cup history'], demo: true,
  });
  return Object.assign(a, over || {});
}

/** Lagging but no edge after the spread and the uncertainty: no trade, a trade_note, not actionable. */
function alertNoTrade(now, over) {
  const a = alertL(now);
  Object.assign(a, {
    alert_id: 'mv-1110-1791208980', exchange_id: '1110', market_id: '555', title: 'Will the Democratic Party win the Maine Senate?',
    race_key: '2026:SENATE:ME', party: 'D', race_label: 'Maine Senate (D)', detected_at: now - 200, confirmation: 'one venue', venues: ['polymarket'],
    venue_moves: [venueMove('polymarket', 'Polymarket', 300, now - 500, 0.52, 0.58, now)],
    cup_now: quote(now - 6, 0.52, 0.535), trade: null, actionable: false, flags: ['one venue'],
    trade_note: "The Cup's ask (0.535) leaves less than 1 cent per share after the spread and the outside price's uncertainty: no trade suggested.",
  });
  return Object.assign(a, over || {});
}

/** Open, the Cup already moved: no edge. */
function alertAlready(now, over) {
  const a = alertL(now);
  Object.assign(a, {
    alert_id: 'mv-1120-1791209040', exchange_id: '1120', market_id: '560', title: 'Will the Democratic Party win the Arizona Governor?',
    race_key: '2026:GOVERNOR:AZ', party: 'D', race_label: 'Arizona Governor (D)', detected_at: now - 600, status: 'already_moved', status_label: 'Cup already moved',
    reason: 'The Cup has already moved +3.0 of the 5.5 pts (now 0.545): no edge left to chase.', trade: null, actionable: false,
    cup_now: quote(now - 3, 0.54, 0.55), lag_gap: 0.025, lag_gap_now: 0.0225, opened_status: 'already_moved',
  });
  return Object.assign(a, over || {});
}

/** Closed, the Cup moved first (lag outcome cup_first, lag_s = -670). */
function alertMovedFirst(now, over) {
  const a = alertL(now);
  const reason = 'The Cup moved +3.5 pts about 11.2 min before the outside price: the outside followed the Cup, not a lag.';
  Object.assign(a, {
    alert_id: 'mv-9039-1791209100', exchange_id: '9039', market_id: '331', title: 'Will the Democratic Party win the Minnesota Senate?',
    race_key: '2026:SENATE:MN', party: 'D', race_label: 'Minnesota Senate (D)', detected_at: now - 2300, state: 'closed', closed_at: now - 1900,
    status: 'moved_first', status_label: 'Cup moved first', reason, trade: null, actionable: false, opened_status: 'moved_first',
    lag: lag({ outcome: 'cup_first', t_move: now - 2330, lag_s: -670, resolved_at: now - 2300, reason }),
  });
  return Object.assign(a, over || {});
}

/** Open, the outside price came back before the Cup moved. */
function alertReverted(now, over) {
  const a = alertL(now);
  const reason = 'The outside price came back 3.5 of its 5.5 pts before the Cup moved.';
  Object.assign(a, {
    alert_id: 'mv-9040-1791209160', exchange_id: '9040', market_id: '332', title: 'Will the Republican Party win the Pennsylvania Governor?',
    race_key: '2026:GOVERNOR:PA', party: 'R', race_label: 'Pennsylvania Governor (R)', detected_at: now - 1200, status: 'reverted', status_label: 'Outside reverted',
    reason, trade: null, actionable: false, opened_status: 'lagging',
    lag: lag({ outcome: 'reverted', t_move: now - 1230, resolved_at: now - 960, reason, capture_4m: -0.005, capture_at: now - 960 }),
  });
  return Object.assign(a, over || {});
}

/** Closed, followed (260 s / 105 s) with both captures known (+0.0075, +0.005), and the suggestion it opened with. */
function alertFollowed(now, over) {
  const a = alertL(now);
  const det = now - 2400;
  const reason = 'The Cup moved +3.2 pts, 4.3 min after the outside move (1.8 min after the alert).';
  Object.assign(a, {
    alert_id: 'mv-9035-1791209220', exchange_id: '9035', market_id: '327', title: 'Will the Democratic Party win the New Hampshire Senate?',
    race_key: '2026:SENATE:NH', party: 'D', race_label: 'New Hampshire Senate (D)', detected_at: det, state: 'closed', closed_at: det + 405,
    status: 'already_moved', status_label: 'Cup already moved', reason, trade: null, actionable: false, opened_status: 'lagging',
    opened_trade: Object.assign(clone(alertL(now).trade), { computed_at: det }),
    lag: lag({ outcome: 'followed', t_move: det - 155, followed_at: det + 105, lag_s: 260, lag_after_alert_s: 105, resolved_at: det + 135, reason,
      capture_4m: 0.0075, capture_at: det + 240, exit_30m: 0.005, exit_at: det + 2040 }),
  });
  return Object.assign(a, over || {});
}

/** Closed, the Cup did not follow within 60 min. */
function alertNotFollowed(now, over) {
  const a = alertL(now);
  const det = now - 4000;
  const reason = 'The Cup did not follow within 60 min; the gap is 6.0 pts now.';
  Object.assign(a, {
    alert_id: 'mv-9037-1791209280', exchange_id: '9037', market_id: '329', title: 'Will the Democratic Party win the Wisconsin Governor?',
    race_key: '2026:GOVERNOR:WI', party: 'D', race_label: 'Wisconsin Governor (D)', detected_at: det, state: 'closed', closed_at: det + 3630,
    status: 'lagging', status_label: 'Cup lagging', reason, trade: null, actionable: false, opened_status: 'lagging',
    lag: lag({ outcome: 'not_followed', t_move: det - 30, resolved_at: det + 3570, reason, capture_4m: 0.0525, capture_at: det + 240, exit_30m: -0.01, exit_at: det + 2040 }),
  });
  return Object.assign(a, over || {});
}

/** Closed, censored: the bot was not running for 25 min while it was measured. */
function alertCensored(now, over) {
  const a = alertL(now);
  const det = now - 5000;
  Object.assign(a, {
    alert_id: 'mv-1130-1791209340', exchange_id: '1130', market_id: '561', title: 'Will the Republican Party win the Georgia Senate?',
    race_key: '2026:SENATE:GA', party: 'R', race_label: 'Georgia Senate (R)', detected_at: det, state: 'closed', closed_at: det + 1600,
    reason: 'The outside price moved +5.5 pts in 5 min; the Cup has moved +0.0 pts over the same time (now 0.512).', trade: null, actionable: false,
    lag: lag({ outcome: 'censored', t_move: det - 60, resolved_at: det + 1600, reason: 'The bot was not running for 25 min while this was measured.' }),
  });
  return Object.assign(a, over || {});
}

/** Every status and lag outcome, in the server's order (open: actionable first, then other open, newest first; then closed). */
function allAlerts(now) {
  return [alertL(now), alertNoBook(now), alertNoTrade(now), alertAlready(now), alertReverted(now),
    alertMovedFirst(now), alertFollowed(now), alertNotFollowed(now), alertCensored(now)];
}

function venue(name, label, status, now, extra) {
  return Object.assign({
    venue: name, label, status, last_ok_at: now - 6, last_poll_at: now - 6, last_error: null, next_try_at: null, requests_last_poll: 5,
    budget_used: 25, budget_limit: 45, matched: 231, quoted: 229, stale: 0,
  }, extra || {});
}

/** Venue lines: ok Polymarket and offline Kalshi (default), or one of 'backoff' | 'busy' | 'pending'. */
function venues(now, kind) {
  const pm = venue('polymarket', 'Polymarket', 'ok', now);
  const offline = 'Kalshi is unreachable from this machine (could not connect): no outside quotes from Kalshi this time.';
  if (kind === 'backoff') {
    return [pm, venue('kalshi', 'Kalshi', 'backoff', now, { last_error: 'Kalshi answered HTTP 429 (too many requests): waiting before the next read.', next_try_at: now + 240, matched: 220, quoted: 0, budget_used: 14 })];
  }
  if (kind === 'busy') return [venue('polymarket', 'Polymarket', 'busy', now, { requests_last_poll: 0 }), venue('kalshi', 'Kalshi', 'ok', now, { matched: 220, budget_used: 15 })];
  if (kind === 'pending') {
    return [venue('polymarket', 'Polymarket', 'pending', now, { last_ok_at: null, matched: 0, quoted: 0, last_error: 'No validated Polymarket match yet: waiting for the first fair-value refresh.' }),
      venue('kalshi', 'Kalshi', 'pending', now, { last_ok_at: null, matched: 0, quoted: 0, last_error: 'No validated Kalshi match yet: waiting for the first fair-value refresh.' })];
  }
  return [pm, venue('kalshi', 'Kalshi', 'offline', now, { last_ok_at: null, last_error: offline, requests_last_poll: 1, budget_used: 1, matched: 220, quoted: 0 })];
}

function stats(n, mean, median, pos, ci) {
  return { n, mean, median, positive_share: pos, ci90: ci || null };
}

function summary(kind) {
  if (kind === 'large') {
    return {
      moves: 52, races: 21, resolved: 50, pending: 1, censored: 1, excluded: 3, outside_led: 46, cup_first: 4, cup_first_share: 0.08,
      followed: 33, followed_within: { 60: 0.065217, 300: 0.413043, 900: 0.608696, 3600: 0.717391 }, followed_within_n: { 60: 3, 300: 19, 900: 28, 3600: 33 },
      reverted: 8, not_followed: 5, never_followed: 13, never_followed_share: 0.282609, reverted_share: 0.173913, not_followed_share: 0.108696,
      median_lag_s: 210, median_lag_after_alert_s: 90, lag_quartiles_s: [120, 420],
      capture_4m: stats(40, 0.004, 0.003, 0.575, [-0.001, 0.009]), exit_30m: stats(37, -0.002, -0.001, 0.46, [-0.008, 0.004]),
      small_sample: false, sentence: LARGE_SENTENCE, since: null, window: 'every stored alert (up to 30 days)',
    };
  }
  if (kind === 'empty') {
    return {
      moves: 0, races: 0, resolved: 0, pending: 0, censored: 0, excluded: 0, outside_led: 0, cup_first: 0, cup_first_share: null, followed: 0,
      followed_within: { 60: null, 300: null, 900: null, 3600: null }, followed_within_n: { 60: 0, 300: 0, 900: 0, 3600: 0 },
      reverted: 0, not_followed: 0, never_followed: 0, never_followed_share: null, reverted_share: null, not_followed_share: null,
      median_lag_s: null, median_lag_after_alert_s: null, lag_quartiles_s: null,
      capture_4m: stats(0, null, null, null), exit_30m: stats(0, null, null, null),
      small_sample: true, sentence: NO_DATA_SENTENCE, since: null, window: 'every stored alert (up to 30 days)',
    };
  }
  // the small sample: shares null where nothing is known yet ("n/a")
  return {
    moves: 7, races: 5, resolved: 5, pending: 2, censored: 1, excluded: 1, outside_led: 4, cup_first: 1, cup_first_share: 0.2,
    followed: 2, followed_within: { 60: 0.0, 300: 0.25, 900: 0.5, 3600: 0.5 }, followed_within_n: { 60: 0, 300: 1, 900: 2, 3600: 2 },
    reverted: 1, not_followed: 1, never_followed: 2, never_followed_share: 0.5, reverted_share: 0.25, not_followed_share: 0.25,
    median_lag_s: 260, median_lag_after_alert_s: 105, lag_quartiles_s: [230, 290],
    capture_4m: stats(3, 0.0183, 0.0075, 0.667), exit_30m: stats(0, null, null, null),
    small_sample: true, sentence: SMALL_SENTENCE, since: null, window: 'every stored alert (up to 30 days)',
  };
}

function suppressed(now) {
  return [
    { exchange_id: '9041', title: 'Will the Democratic Party win the Michigan Governor?', race_label: 'Michigan Governor (D)', reason: 'disagree',
      reason_label: SUPPRESS_LABELS.disagree, at: now - 90, window_s: 60, venues: ['polymarket', 'kalshi'], move: 0.06, threshold: 0.04,
      detail: 'Polymarket +6.0 pts, Kalshi +1.0 pt: the venues disagree.' },
    { exchange_id: '9038', title: 'Will the Republican Party win the Georgia Governor?', race_label: 'Georgia Governor (R)', reason: 'thin',
      reason_label: SUPPRESS_LABELS.thin, at: now - 400, window_s: 60, venues: ['polymarket'], move: 0.06, threshold: 0.045,
      detail: "Polymarket's book was 8 pts wide during the move." },
    { exchange_id: '9038', title: 'Will the Republican Party win the Georgia Governor?', race_label: 'Georgia Governor (R)', reason: 'single_tick',
      reason_label: SUPPRESS_LABELS.single_tick, at: now - 700, window_s: 60, venues: ['polymarket'], move: 0.09, threshold: 0.045,
      detail: 'One print of +9.0 pts that did not hold.' },
  ];
}

function suppressedCounts(over) {
  const c = {};
  for (const r of SUPPRESS_REASONS) c[r] = 0;
  return Object.assign(c, over || {});
}

/** A full enabled /api/moves body. opts: alerts (default allAlerts), venues ('ok' | 'backoff' | 'busy' | 'pending'),
 *  summary ('small' | 'large' | 'empty'), suppressed (default the three items), demo, available, error, book_reads. */
function body(now, opts) {
  const o = opts || {};
  const alerts = o.alerts || allAlerts(now);
  const open = alerts.filter((a) => a.state === 'open');
  const lagging = open.filter((a) => a.status === 'lagging');
  const actionable = open.filter((a) => a.actionable === true);
  const supp = o.suppressed || suppressed(now);
  const counts = {};
  for (const s of supp) counts[s.reason] = (counts[s.reason] || 0) + 1;
  return {
    now, enabled: true, available: o.available !== false, error: o.error || null, demo: !!o.demo, poll_s: o.poll_s || 15.0,
    last_step_at: o.last_step_at !== undefined ? o.last_step_at : now - 3, steps: o.steps != null ? o.steps : 120,
    watching: o.watching || { outcomes: 237, matched: 231, venues: 2 },
    venues: o.venuesList || venues(now, o.venues),
    counts: { open: open.length, lagging: lagging.length, actionable: actionable.length, today: alerts.length, closed_today: alerts.length - open.length,
      suppressed_today: suppressedCounts(o.counts || counts) },
    actionable_ids: actionable.map((a) => a.alert_id),
    alerts,
    suppressed: supp,
    summary: o.summary === null ? null : summary(o.summary || 'small'),
    thresholds: clone(THRESHOLDS),
    book_reads: o.book_reads !== undefined ? o.book_reads : { used: 1, limit: 4, room: 3 },
    caveats: o.demo ? CAVEATS.concat([DEMO_CAVEAT]) : CAVEATS.slice(),
  };
}

/** The exact disabled body (§18.2). */
function disabled(now) {
  return {
    now, enabled: false, available: false, error: OFF_ERROR, demo: false, poll_s: null, last_step_at: null, steps: 0,
    watching: { outcomes: 0, matched: 0, venues: 0 }, venues: [],
    counts: { open: 0, lagging: 0, actionable: 0, today: 0, closed_today: 0, suppressed_today: suppressedCounts() },
    actionable_ids: [], alerts: [], suppressed: [], summary: null, thresholds: null, book_reads: null, caveats: [],
  };
}

/** An enabled watcher with nothing to show yet (stepped, no alert, nothing filtered). */
function empty(now) {
  return body(now, { alerts: [], suppressed: [], summary: 'empty' });
}

/** notification_text() of moves.py (§19.4) for an alert. */
function notificationText(a) {
  const d = a.direction < 0 ? -1 : 1;
  const pts = (d * a.move * 100).toFixed(1);
  const win = { 60: '1 min', 300: '5 min', 900: '15 min', 3600: '1 h' }[Math.round(a.window_s)] || a.window_s + ' s';
  const cup = (a.cup_now && a.cup_now.mid != null ? a.cup_now.mid : a.cup.now.mid).toFixed(3);
  let b = 'Outside ' + (d * a.move >= 0 ? '+' : '') + pts + ' pts in ' + win + ' (' + a.outside_before.toFixed(3) + ' → ' + a.outside_after.toFixed(3) + '); Cup ' + cup + '.';
  b += a.trade && a.trade.text ? ' Suggestion: ' + a.trade.text + '. Not a sure thing.' : ' No trade suggested. Not a sure thing.';
  return { headline: (a.status_label || 'Outside move') + ': ' + a.race_label, body: b };
}

/** The /api/status "moves" object (§12.4) for a set of alerts (the actionable ones are listed, newest first). */
function statusMoves(now, alerts) {
  const list = (alerts || []).filter((a) => a.state === 'open' && a.actionable === true);
  return {
    enabled: true, poll_s: 15.0, last_step_at: now - 3, steps: 120, open: (alerts || []).filter((a) => a.state === 'open').length,
    actionable: list.length,
    actionable_alerts: list.slice(0, 10).map((a) => Object.assign({ alert_id: a.alert_id, exchange_id: a.exchange_id, detected_at: a.detected_at }, notificationText(a))),
    venues: { ok: 2, total: 2 }, last_error: null,
  };
}

/** A /api/strategy value idea with the §18.4 outside_move annotation (and one without it). */
function annotatedIdea(now) {
  return {
    kind: 'value', exchange_id: '1104', market_id: '552', title: 'Will the Democratic Party win the North Carolina Senate?', option: 'YES', side: 'yes',
    entry_price: 0.52, target_price: 0.56, stop_price: 0.49, prob_win: 0.57, edge: 0.03, expected_return: 0.0577, horizon_hours: 6,
    suggested_shares: 300, suggested_cost: 156, score: 0.012, confidence: 0.7, rationale: ['Outside fair value 0.565 vs the Cup ask 0.520.'],
    risks: ['The outside price can be wrong.'], settles_before_cup_end: false,
    outside_move: { alert_id: alertL(now).alert_id, status: 'lagging', status_label: 'Cup lagging', state: 'open', direction: 1, move: 0.055,
      lag_gap_now: 0.055, detected_at: now - 120, actionable: true },
  };
}

module.exports = {
  CAVEATS, DEMO_CAVEAT, TRADE_NOTE, OFF_ERROR, NO_DATA_SENTENCE, SUPPRESS_REASONS, SUPPRESS_LABELS, STATUS_LABELS, THRESHOLDS,
  LARGE_SENTENCE, SMALL_SENTENCE,
  alertL, alertNoBook, alertNoTrade, alertAlready, alertMovedFirst, alertReverted, alertFollowed, alertNotFollowed, alertCensored, allAlerts,
  venues, summary, suppressed, body, disabled, empty, notificationText, statusMoves, annotatedIdea, clone,
};
