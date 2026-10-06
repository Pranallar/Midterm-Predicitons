#!/usr/bin/env node
/* Regression checks for the dashboard UI bugs found in QA round 1, and the Simulation view's states
 * (docs/PAPER_TRADING.md §8.4, "sim-*" checks on page.route() fixtures from sim_fixtures.cjs), and the Outside moves
 * view (docs/OUTSIDE_MOVES.md §19.6, "moves-*" checks on fixtures from moves_fixtures.cjs) (CommonJS, Playwright).
 *
 *   node tests/e2e/regressions.cjs [URL] [--only id,id]
 *
 * Without a URL it starts tests/e2e/serve_demo.py itself. Every check is named after the bug id it
 * guards (visual-3, a11y-2, robustness-1, ...) plus "contract-*" checks for the optional API fields
 * the UI renders (closed surges, news status, problems, detection, book_pending, assumptions,
 * arbitrage legs). Unusual server states are simulated with page.route(), so the checks are fast and
 * deterministic; 15-60 s timers are skipped with page.clock.fastForward(). A check fails on its own
 * assertion, on a page error, on an unhandled rejection or on a "dashboard: ... failed" render error.
 * Prints "all regression checks passed" when every check passes.
 */
'use strict';

const { launch, startServer, stopServer, check } = require('./lib.cjs');
const SF = require('./sim_fixtures.cjs');
const MF = require('./moves_fixtures.cjs');

const args = process.argv.slice(2);
let URL_ARG = null;
let ONLY = null;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--only') ONLY = new Set(String(args[++i] || '').split(',').filter(Boolean));
  else if (!URL_ARG) URL_ARG = args[i];
}

let BASE = '';
const results = [];

function log(msg) {
  process.stdout.write('[regressions] ' + msg + '\n');
}

// ------------------------------------------------------------------ page helpers

async function newPage(browser, opts) {
  const o = Object.assign({ viewport: { width: 1440, height: 900 }, colorScheme: 'light', reducedMotion: 'reduce' }, opts || {});
  const clock = o.clock;
  delete o.clock;
  const context = await browser.newContext(o);
  await context.addInitScript(() => {
    window.__rejections = [];
    window.addEventListener('unhandledrejection', (ev) => {
      window.__rejections.push(String((ev.reason && (ev.reason.stack || ev.reason.message)) || ev.reason));
    });
  });
  const page = await context.newPage();
  page.__errors = [];
  page.__status = 0;
  page.on('pageerror', (err) => page.__errors.push('page error: ' + (err.stack || err.message)));
  page.on('console', (msg) => {
    if (msg.type() === 'error' && /^dashboard: /.test(msg.text())) page.__errors.push('render error: ' + msg.text());
  });
  page.on('response', (resp) => {
    if (new URL(resp.url()).pathname === '/api/status' && resp.ok()) page.__status += 1;
  });
  if (clock) await page.clock.install();
  return page;
}

/** A fresh load of the dashboard at a hash, waiting until it is live and the history backfill is done. */
async function open(page, hash, opts) {
  await page.goto('about:blank');
  await page.goto(BASE + '/' + (hash || '#overview'));
  if (opts && opts.noWait) {
    await page.waitForFunction(() => document.getElementById('live').dataset.state !== 'loading' || /\d/.test(document.getElementById('nav-count-markets').textContent), null, { timeout: 30000 });
    return;
  }
  await page.waitForFunction(() => document.getElementById('live-label').textContent === 'Live', null, { timeout: 30000 });
  await page.waitForSelector('#startup-banner', { state: 'hidden', timeout: 120000 });
}

async function gotoView(page, view) {
  await page.click('.app-nav a[data-view="' + view + '"]');
  await page.waitForFunction((v) => location.hash === '#' + v && !document.getElementById('view-' + v).hidden, view);
}

async function openDrawer(page, eid) {
  await page.evaluate((id) => { location.hash = '#exchange/' + id; }, eid);
  await page.waitForFunction(() => document.getElementById('drawer').open);
  await page.waitForSelector('#drawer .chart svg .price-line', { timeout: 20000 });
}

/** The chart's hover target, re-read until it is stable (the chart redraws when the order book
 *  arrives and the drawer gains a scrollbar). */
async function hitBox(page) {
  await page.waitForSelector('#drawer table.ladder, #drawer .book-summary + .muted-note, #drawer #h-book ~ .muted-note', { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(300);
  const deadline = Date.now() + 10000;
  for (;;) {
    const handle = await page.$('#drawer .chart svg rect.hit');
    const box = handle && (await handle.boundingBox().catch(() => null));
    if (box) return box;
    if (Date.now() > deadline) throw new Error('the chart hover target never settled');
    await page.waitForTimeout(100);
  }
}

async function closeDrawer(page) {
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.getElementById('drawer').open);
}

/** Intercept a JSON GET and rewrite its body: fn(json, now) returns the new body (or mutates it). */
async function patch(page, pattern, fn) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    let resp;
    try {
      resp = await route.fetch();
    } catch (e) {
      return route.abort().catch(() => {});
    }
    let json;
    try {
      json = await resp.json();
    } catch (e) {
      return route.fulfill({ response: resp });
    }
    const out = fn(json, typeof json.now === 'number' ? json.now : Date.now() / 1000);
    return route.fulfill({ response: resp, json: out === undefined ? json : out });
  });
}

async function waitStatusPolls(page, n, timeout) {
  const start = page.__status;
  const deadline = Date.now() + (timeout || 20000);
  while (page.__status < start + n) {
    if (Date.now() > deadline) throw new Error('waited for ' + n + ' status polls, saw ' + (page.__status - start));
    await page.waitForTimeout(200);
  }
}

async function text(page, sel) {
  return ((await page.textContent(sel)) || '').replace(/\s+/g, ' ').trim();
}

function signed(t) {
  const m = String(t).replace(/−/g, '-').match(/[+-]?\d+\.\d+/);
  return m ? Number(m[0]) : null;
}

async function run(name, page, fn) {
  if (ONLY && !ONLY.has(name)) return;
  const t0 = Date.now();
  page.__errors.length = 0;
  try {
    await fn(page);
    const rej = await page.evaluate(() => window.__rejections || []).catch(() => []);
    if (rej.length) throw new Error('unhandled rejection: ' + rej.join(' | '));
    if (page.__errors.length) throw new Error(page.__errors.join(' | '));
    results.push({ name, ok: true });
    log('ok   ' + name + ' (' + ((Date.now() - t0) / 1000).toFixed(1) + ' s)');
  } catch (err) {
    results.push({ name, ok: false, error: err.message });
    log('FAIL ' + name + ': ' + String(err.message).split('\n')[0]);
  } finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' }).catch(() => {});
  }
}

// ------------------------------------------------------------------ fixtures

const LONG_TITLE = 'Will the Republican nominee win the special election for Ohio House District 9 after the incumbent withdrew in late September 2026?';

function legacyArb() {
  return {
    kind: 'arbitrage', exchange_id: '9016', market_id: '313', title: 'Which party will control the Senate after the midterms?', option: 'Republicans',
    side: 'no', entry_price: 0.97, target_price: null, stop_price: null, prob_win: null, edge: 0.03, expected_return: 0.0309,
    horizon_hours: null, suggested_shares: 8125, suggested_cost: 7881.25, score: 0.03, confidence: 0.7,
    rationale: ['Outcome prices sum to 1.030'], risks: ['Legs fill separately'], settles_before_cup_end: null, surge_id: null,
  };
}

function legsArb() {
  return Object.assign(legacyArb(), {
    exchange_id: null, option: null, side: null, market_id: '313',
    legs: [
      { exchange_id: '9016', market_id: '313', title: 'Which party will control the Senate after the midterms?', option: 'Republicans', side: 'yes', price: 0.705 },
      { exchange_id: '9017', market_id: '313', title: 'Which party will control the Senate after the midterms?', option: 'Democrats', side: 'no', price: 0.295 },
    ],
    entry_price: 1.0, score: 0.031,
  });
}

// ------------------------------------------------------------------ desktop checks

async function desktopChecks(browser) {
  const page = await newPage(browser);

  await run('visual-3', page, async () => {
    await open(page, '#markets');
    for (const [w, h] of [[1440, 900], [1366, 768], [1280, 800], [1024, 768]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(250);
      const m = await page.evaluate(() => {
        const wrap = document.getElementById('markets-wrap');
        const right = wrap.getBoundingClientRect().right;
        const cut = Array.from(document.querySelectorAll('#markets-body .flag, #markets-body .cell-settles span')).filter((el) => el.getBoundingClientRect().right > right + 0.5).length;
        return { over: wrap.scrollWidth - wrap.clientWidth, cut };
      });
      check(m.over <= 1, 'Markets table fits its card at ' + w + ' px (overflow ' + m.over + ' px)');
      check(m.cut === 0, 'no Flags/Settles badge is cut off at ' + w + ' px (' + m.cut + ' cut)');
    }
    await gotoView(page, 'high');
    await page.waitForSelector('#high-body tr');
    const over = await page.evaluate(() => { const w = document.getElementById('high-wrap'); return w.scrollWidth - w.clientWidth; });
    check(over <= 1, 'High 90s table fits at 1024 px (overflow ' + over + ' px)');
    await page.setViewportSize({ width: 600, height: 900 });
    await gotoView(page, 'markets');
    await page.waitForTimeout(300);
    const cue = await page.evaluate(() => ({
      right: document.getElementById('markets-frame').hasAttribute('data-more-right'),
      sticky: getComputedStyle(document.querySelector('#markets-body td')).position,
    }));
    check(cue.right, 'a narrow Markets table shows the right-edge scroll cue');
    check(cue.sticky === 'sticky', 'the Market column stays pinned while the table scrolls (' + cue.sticky + ')');
    await page.evaluate(() => { const w = document.getElementById('markets-wrap'); w.scrollLeft = w.scrollWidth; });
    await page.waitForTimeout(200);
    const after = await page.evaluate(() => ({
      left: document.getElementById('markets-frame').hasAttribute('data-more-left'),
      right: document.getElementById('markets-frame').hasAttribute('data-more-right'),
    }));
    check(after.left && !after.right, 'scrolled to the end: the cue moves to the left edge ' + JSON.stringify(after));
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await run('visual-4', page, async () => {
    let balance = 98518.75;
    await patch(page, '**/api/status', (j) => { j.account.balance = balance; });
    await open(page, '#overview');
    for (const b of [98518.75, 1234567.89]) {
      balance = b;
      await waitStatusPolls(page, 1);
      await page.waitForFunction((v) => document.getElementById('tile-balance').textContent.indexOf(v) !== -1, b > 1e6 ? '1,234,568' : '98,519');
      const bad = [];
      for (let w = 320; w <= 1440; w += 24) {
        await page.setViewportSize({ width: w, height: 900 });
        const r = await page.evaluate(() => {
          const lines = (el) => new Set(Array.from(el.getClientRects()).map((x) => Math.round(x.top))).size;
          const num = document.querySelector('#tile-balance .tile-num');
          const unit = document.querySelector('#tile-balance .tile-unit');
          const unitRange = document.createRange();
          unitRange.selectNodeContents(unit);
          const tile = document.getElementById('tile-balance');
          return { num: lines(num), unit: new Set(Array.from(unitRange.getClientRects()).map((x) => Math.round(x.top))).size, over: tile.scrollWidth - tile.clientWidth };
        });
        if (r.num !== 1 || r.unit !== 1 || r.over > 1) bad.push(w + ':' + JSON.stringify(r));
      }
      check(!bad.length, 'balance ' + b + ' never splits the number or the unit: ' + bad.slice(0, 4).join(' '));
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await run('visual-6', page, async () => {
    await open(page, '#markets');
    for (const eid of ['9001', '9002', '9005', '9010']) {
      await openDrawer(page, eid);
      await page.waitForTimeout(300);
      const r = await page.evaluate(() => {
        const facts = {};
        for (const div of document.querySelectorAll('#drawer .drawer-facts > div')) facts[div.querySelector('dt').textContent] = div.querySelector('dd').textContent;
        return { facts, summary: document.querySelector('#drawer .chart-summary').textContent, end: (document.querySelector('#drawer .chart .end-label') || {}).textContent };
      });
      const fact = r.facts['Change 24h'];
      const m = r.summary.match(/\(([^)]*)\)/);
      if (fact && /\d/.test(fact)) {
        check(m && signed(m[1]) === signed(fact), eid + ': chart summary change ' + (m && m[1]) + ' equals the Change 24h fact ' + fact);
      }
      const nums = r.summary.replace(/−/g, '-').match(/(\d\.\d{3}) → (\d\.\d{3}) \(([+-]?\d\.\d{3})\)/);
      check(nums && Math.abs(Number(nums[2]) - Number(nums[1]) - Number(nums[3])) < 0.0011, eid + ': summary start → end adds up: ' + r.summary);
      check('Mark' in r.facts && 'Last trade' in r.facts, eid + ': facts show both Last trade and Mark ' + Object.keys(r.facts).join(','));
      check(r.facts.Mark === r.end, eid + ': the chart end label (' + r.end + ') matches the Mark fact (' + r.facts.Mark + ')');
      await closeDrawer(page);
    }
  });

  await run('visual-10', page, async () => {
    await patch(page, '**/api/exchange/9001', (j) => { j.exchange.title = LONG_TITLE; });
    await open(page, '#markets');
    await page.setViewportSize({ width: 720, height: 450 }); // a 1440x900 window at 200% zoom
    await openDrawer(page, '9001');
    await page.evaluate(() => { document.getElementById('drawer-inner').scrollTop = 600; });
    await page.waitForTimeout(200);
    const zoom = await page.evaluate(() => {
      const head = document.querySelector('#drawer .drawer-head').getBoundingClientRect();
      return { covered: Math.max(0, Math.min(head.bottom, innerHeight) - Math.max(head.top, 0)) / innerHeight };
    });
    check(zoom.covered <= 0.2, 'at 200% zoom the drawer header covers at most 20% of the screen while scrolling (' + (zoom.covered * 100).toFixed(0) + '%)');
    await closeDrawer(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openDrawer(page, '9001');
    const desk = await page.evaluate(() => {
      const t = document.getElementById('drawer-title');
      const lh = parseFloat(getComputedStyle(t).lineHeight);
      return { lines: Math.round(t.getBoundingClientRect().height / lh), full: t.getAttribute('title'), text: t.textContent };
    });
    check(desk.lines <= 2, 'the drawer title is clamped to 2 lines (' + desk.lines + ')');
    check(desk.full === LONG_TITLE && desk.text === LONG_TITLE, 'the full title stays available (tooltip and accessible name)');
    await closeDrawer(page);
  });

  await run('visual-11', page, async () => {
    await open(page, '#markets');
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.click('#markets-body tr[data-eid="9001"] a.row-link'); // Playwright scrolls the row into view first
    await page.waitForFunction(() => document.getElementById('drawer').open);
    const y0 = await page.evaluate(() => window.scrollY);
    await page.mouse.move(200, 450);
    await page.mouse.wheel(0, 800);
    await page.waitForTimeout(400);
    const y = await page.evaluate(() => window.scrollY);
    check(y === y0, 'the page behind the open drawer does not scroll (scrollY ' + y0 + ' → ' + y + ')');
    await closeDrawer(page);
    const y2 = await page.evaluate(() => window.scrollY);
    check(Math.abs(y2 - y0) <= 1, 'after closing, the list is where the user left it (' + y0 + ' → ' + y2 + ')');
    check(!(await page.evaluate(() => document.documentElement.classList.contains('drawer-open'))), 'the scroll lock is released on close');
  });

  await run('functional-2', page, async () => {
    await patch(page, '**/api/surges', (j) => {
      const s = j.surges[0];
      Object.assign(s, { start_price: 0.54, end_price: 0.675, change: 0.14, peak_change: 0.195 });
    });
    await open(page, '#surges');
    await page.waitForSelector('#surge-cards .surge-card .move-main');
    const t = await text(page, '#surge-cards .surge-card .move-line');
    check(/0\.540 → 0\.675 ▲\+0\.135/.test(t), 'the arrow value is end − start: ' + t);
    check(!/\+0\.140/.test(t), 'the stale stored change is not shown: ' + t);
    check(/Peak move \+0\.195/.test(t), 'a different peak move is labelled as such: ' + t);
  });

  await run('functional-4', page, async () => {
    await patch(page, '**/api/strategy', (j) => { j.opportunities = [legacyArb(), legsArb()].concat(j.opportunities || []); });
    await open(page, '#strategy');
    await page.waitForSelector('#strategy-body .idea .legs li');
    const body = await text(page, '#strategy-body .ideas');
    check(!/BUY NO @ 0\.970/.test(body), 'a set is never shown as one "BUY NO @ <sum>" order');
    const legs = await page.$$eval('#strategy-body .legs li', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
    check(legs.some((l) => /^BUY YES Republicans @ 0\.705/.test(l)) && legs.some((l) => /^BUY NO Democrats @ 0\.295/.test(l)), 'legs are listed: ' + legs.join(' | '));
    const first = await text(page, '#strategy-body .idea:nth-child(1)');
    check(/per full set/.test(first) && /8,125 sets/.test(first), 'a legacy set says "per full set" and sizes in sets: ' + first.slice(0, 200));
    check(!/— Republicans/.test(await text(page, '#strategy-body .idea:nth-child(1) .idea-title')), 'a set does not name only its first leg');
    await gotoView(page, 'overview');
    await page.waitForSelector('#look-ideas li a');
    const look = await page.$$eval('#look-ideas li', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ')));
    check(!look.some((l) => /BUY NO @ 0\.970/.test(l)), 'Overview never shows the set as one order: ' + look[0]);
    check(/per full set/.test(look[0]) && /\/set/.test(look[0]), 'Overview labels the set and its edge per set: ' + look[0]);
    check(/^#strategy\/./.test(await page.getAttribute('#look-ideas li:first-child a', 'href')), 'a set links to its card on the strategy page, not to one leg');
  });

  await run('functional-6', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9004');
    await page.click('#drawer button[data-action="table-toggle"]');
    await page.waitForSelector('#drawer .chart-table-wrap tbody tr');
    const rows = await page.$$eval('#drawer .chart-table-wrap tbody tr', (trs) => trs.map((tr) => tr.textContent));
    let dupes = 0;
    for (let i = 1; i < rows.length; i++) if (rows[i] === rows[i - 1]) dupes += 1;
    check(rows.length > 5, 'the price table has rows (' + rows.length + ')');
    check(dupes === 0, 'no row reads the same as the one above it (' + dupes + ' of ' + rows.length + ')');
    check(/\d:\d\d:\d\d/.test(rows[0]), 'live snapshots show seconds: ' + rows[0]);
    await page.click('#drawer button[data-action="table-toggle"]');
    await closeDrawer(page);
  });

  await run('functional-7', page, async () => {
    await open(page, '#markets');
    await page.route('**/api/**', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"simulated outage"}' }));
    await page.waitForSelector('#offline-banner:not([hidden])', { timeout: 20000 });
    await page.waitForFunction(() => /Trying again in \d+ s/.test(document.getElementById('offline-countdown').textContent), null, { timeout: 15000 });
    const a = await text(page, '#offline-countdown');
    const u1 = await text(page, '#updated');
    await page.waitForTimeout(2300);
    const b = await text(page, '#offline-countdown');
    const u2 = await text(page, '#updated');
    check(a !== b, 'the retry countdown ticks while offline: "' + a + '" then "' + b + '"');
    check(u1 !== u2, 'the "Updated … ago" age keeps ticking while offline: "' + u1 + '" then "' + u2 + '"');
  });

  await run('a11y-1', page, async () => {
    await open(page, '#overview');
    const r = await page.evaluate(() => {
      const upd = document.getElementById('updated');
      return { live: upd.getAttribute('aria-live'), inRegion: !!upd.closest('[aria-live],[role=status],[role=alert]'), status: document.getElementById('live').getAttribute('role') };
    });
    check(!r.live && !r.inRegion, 'the ticking "Updated … ago" time is not a live region');
    check(r.status === 'status', 'the live indicator is the status region');
    const seen = new Set([await page.textContent('#live')]);
    for (let i = 0; i < 2; i++) {
      await waitStatusPolls(page, 1);
      seen.add(await page.textContent('#live'));
    }
    check(seen.size === 1, 'the status region says nothing new on a plain poll: ' + Array.from(seen).join(' / '));
    check(/Prices are live/.test(await page.textContent('#live-sentence')), 'the region speaks a full sentence');
  });

  await run('a11y-2', page, async () => {
    // (r2-a11y-8 changed how: the result is announced once, through the announcer, when typing pauses)
    await page.route('**/api/surges/*/analyze', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{"queued":true,"message":"Queued for analysis."}' }));
    await open(page, '#markets');
    const inLive = (sel) => page.evaluate((s) => { const el = document.querySelector(s); return !!(el && el.closest('[aria-live],[role=status],[role=alert],[role=log],output')); }, sel);
    await page.fill('#market-search', 'senate');
    await page.waitForFunction(() => /^Showing \d+ of \d+ outcomes$/.test(document.getElementById('announcer').textContent), null, { timeout: 5000 });
    await page.fill('#market-search', 'zzzz');
    await page.waitForSelector('#markets-empty:not([hidden])');
    await page.waitForFunction(() => /No markets match “zzzz”/.test(document.getElementById('announcer').textContent), null, { timeout: 5000 });
    check(!(await inLive('#markets-count')) && !(await inLive('#markets-empty')), 'the count and the empty state are not live regions of their own');
    await page.click('#markets-empty button');
    await gotoView(page, 'surges');
    await page.waitForSelector('#surge-cards button.reanalyze');
    await page.focus('#surge-cards button.reanalyze');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => /Queued/.test(document.getElementById('announcer').textContent), null, { timeout: 5000 });
    check(await inLive('#surge-cards .action-msg'), 'the Re-analyze message sits in a status region');
    check(await inLive('#live-label'), 'the Live/Offline label is inside the status region');
    check((await page.getAttribute('#offline-banner', 'role')) === 'alert', 'the offline banner is an alert');
    check((await page.getAttribute('#fatal-banner', 'role')) === 'alert', 'the fatal banner is an alert');
  });

  await run('a11y-3', page, async () => {
    for (const [w, h] of [[720, 450], [1280, 620]]) {
      await page.setViewportSize({ width: w, height: h });
      await open(page, '#markets');
      await openDrawer(page, '9002');
      await page.waitForSelector('#drawer .surge-card button.reanalyze');
      await page.focus('#drawer .surge-card button.reanalyze');
      const hidden = [];
      for (let i = 0; i < 10; i++) {
        await page.keyboard.press('Shift+Tab');
        await page.waitForTimeout(60);
        const r = await page.evaluate(() => {
          const el = document.activeElement;
          const head = document.querySelector('#drawer .drawer-head');
          if (!el || head.contains(el) || !document.getElementById('drawer').contains(el)) return null;
          const rect = el.getBoundingClientRect();
          const sticky = getComputedStyle(head).position === 'sticky';
          const top = sticky ? head.getBoundingClientRect().bottom : 0;
          return { name: (el.textContent || el.getAttribute('aria-label') || el.tagName).trim().slice(0, 30), visible: rect.top >= top - 1 && rect.bottom <= innerHeight + 1 };
        });
        if (r && !r.visible) hidden.push(r.name);
      }
      check(!hidden.length, 'no focused control hides under the sticky header at ' + w + 'x' + h + ': ' + hidden.join(', '));
      await closeDrawer(page);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  await run('a11y-6', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9002');
    await page.focus('#drawer .chart');
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(500);
    const r = await page.evaluate(() => {
      const region = document.querySelector('#drawer .chart ~ [role="status"]');
      const host = document.querySelector('#drawer .chart');
      const help = document.getElementById(host.getAttribute('aria-describedby'));
      return { text: region && region.textContent, live: region && region.getAttribute('aria-live'), help: help && help.textContent };
    });
    check(r.live === 'polite' && /^Price \d\.\d{3}, [\d.]+%, .+Point \d+ of \d+/.test(r.text || ''), 'arrow keys are read out: ' + r.text);
    check(/Table view/.test(r.help || ''), 'the chart points screen-reader users to the table too');
    await closeDrawer(page);
  });

  await run('a11y-7', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9002');
    const sel = '#drawer button[data-action="table-toggle"]';
    const before = [await text(page, sel), await page.getAttribute(sel, 'aria-pressed')];
    await page.click(sel);
    await page.waitForSelector('#drawer .chart-table-wrap table');
    const after = [await text(page, sel), await page.getAttribute(sel, 'aria-pressed')];
    check(before[0] === 'Table view' && after[0] === 'Table view', 'the toggle keeps one label: ' + before[0] + ' / ' + after[0]);
    check(before[1] === 'false' && after[1] === 'true', 'aria-pressed says whether the table shows');
    await page.click(sel);
    await closeDrawer(page);
  });

  await run('a11y-8', page, async () => {
    await open(page, '#markets');
    const fail = (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"simulated outage"}' });
    await page.route('**/api/status', fail);
    await page.waitForSelector('#offline-banner:not([hidden])', { timeout: 20000 });
    await page.focus('#offline-retry');
    await page.unroute('**/api/status', fail);
    await page.keyboard.press('Enter');
    await page.waitForSelector('#offline-banner', { state: 'hidden', timeout: 10000 });
    const id = await page.evaluate(() => document.activeElement && (document.activeElement.id || document.activeElement.tagName));
    check(id === 'main', 'focus moves to the main content when the banner goes away (got ' + id + ')');
  });

  await run('a11y-9', page, async () => {
    await open(page, '#markets');
    for (const theme of ['light', 'dark']) {
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
      const ratio = await page.evaluate(() => {
        const parse = (c) => c.match(/[\d.]+/g).map(Number);
        const input = getComputedStyle(document.getElementById('market-search'));
        const page = parse(getComputedStyle(document.body).backgroundColor);
        const b = parse(input.borderTopColor);
        const a = b.length > 3 ? b[3] : 1;
        const mix = [0, 1, 2].map((i) => b[i] * a + page[i] * (1 - a));
        const lum = (c) => { const v = c.map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
        const l1 = lum(mix), l2 = lum(page);
        return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      });
      check(ratio >= 3, 'search field boundary contrast is at least 3:1 in ' + theme + ' (' + ratio.toFixed(2) + ')');
    }
    await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));
  });

  await run('a11y-10', page, async () => {
    await open(page, '#strategy');
    await page.waitForSelector('#strategy-body .idea details summary .chev');
    const closed = await page.$eval('#strategy-body .idea details summary .chev', (el) => getComputedStyle(el).transform);
    await page.click('#strategy-body .idea details summary');
    await page.waitForTimeout(250);
    const opened = await page.$eval('#strategy-body .idea details summary .chev', (el) => getComputedStyle(el).transform);
    check(closed !== opened && opened !== 'none', 'the disclosure chevron turns when open (' + closed + ' → ' + opened + ')');
  });

  await run('a11y-12', page, async () => {
    await open(page, '#markets');
    await page.waitForSelector('#markets-body tr .row-link');
    const names = await page.$$eval('#markets-body .row-link', (as) => as.map((a) => a.textContent.trim()));
    check(new Set(names).size === names.length, 'every Markets row link has its own name (' + names.length + ' links)');
    check(names.some((n) => /How many House seats will Democrats win\? — 218-229/.test(n)), 'the link name includes the outcome');
    await gotoView(page, 'high');
    await page.waitForSelector('#high-body .row-link');
    const high = await page.$$eval('#high-body .row-link', (as) => as.map((a) => a.textContent.trim()));
    check(new Set(high).size === high.length, 'every High 90s row link has its own name');
    await gotoView(page, 'surges');
    await page.waitForSelector('#surge-cards button.reanalyze');
    const btns = await page.$$eval('#surge-cards button.reanalyze', (bs) => bs.map((b) => b.textContent.trim()));
    check(btns.length < 2 || new Set(btns).size === btns.length, 'every Re-analyze button names its surge: ' + btns.join(' | '));
  });

  await run('a11y-13', page, async () => {
    await open(page, '#markets');
    const r = await page.evaluate(() => ['markets', 'high'].map((v) => {
      const sec = document.getElementById('view-' + v);
      const secName = document.getElementById(sec.getAttribute('aria-labelledby')).textContent.trim();
      const wrap = document.getElementById(v + '-wrap');
      return [secName, wrap.getAttribute('aria-label') || (wrap.getAttribute('aria-labelledby') && document.getElementById(wrap.getAttribute('aria-labelledby')).textContent.trim())];
    }));
    for (const [a, b] of r) check(a !== b && b, 'the table region has its own name: ' + a + ' / ' + b);
  });

  await run('a11y-14', page, async () => {
    await open(page, '#strategy');
    await page.waitForSelector('#strategy-body .idea');
    const r = await page.evaluate(() => ({
      titles: Array.from(document.querySelectorAll('#strategy-body .idea-title')).map((e) => e.tagName),
      subs: Array.from(document.querySelectorAll('#strategy-body .idea details h5')).length,
      h3InIdeas: document.querySelectorAll('#strategy-body .ideas h3, #strategy-body .ideas details h4').length,
      section: document.getElementById('h-ideas').tagName,
    }));
    check(r.section === 'H3' && r.titles.length && r.titles.every((t) => t === 'H4'), 'idea titles are h4 under the h3 section: ' + r.titles.join(','));
    check(r.subs >= 2 && r.h3InIdeas === 0, 'Rationale/Risks are h5');
  });

  await run('robustness-1', page, async () => {
    await page.route('**/api/strategy', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"Internal error; see the dashboard\'s log."}' }));
    await open(page, '#overview');
    await page.waitForSelector('#view-errors-overview:not([hidden])', { timeout: 15000 });
    const start = page.__status;
    await page.waitForTimeout(11000);
    check(page.__status - start >= 2, 'status polls keep their 5 s pace (' + (page.__status - start) + ' in 11 s)');
    check((await text(page, '#live-label')) === 'Live', 'the header stays Live');
    check(await page.isHidden('#offline-banner'), 'no "Can\'t reach the bot" banner');
    check(/Could not load the strategy ideas: Internal error/.test(await text(page, '#view-errors-overview')), 'the failure shows inline in the view');
    check(/\d/.test(await text(page, '#tile-outcomes')), 'the tiles keep updating');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await page.waitForSelector('#view-errors-overview', { state: 'hidden', timeout: 15000 });
    // a slow endpoint never holds up the status poll
    await page.route('**/api/strategy', async (route) => {
      await new Promise((r) => setTimeout(r, 9000));
      route.continue().catch(() => {});
    });
    const s2 = page.__status;
    await page.waitForTimeout(10500);
    check(page.__status - s2 >= 2, 'a 9 s-slow strategy request does not stall the status poll (' + (page.__status - s2) + ' polls in 10.5 s)');
    check((await text(page, '#live-label')) === 'Live', 'still Live during the slow request');
  });

  await run('robustness-2', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9002');
    await page.route('**/api/**', (route) => route.abort('connectionrefused'));
    await page.waitForSelector('#drawer .drawer-stale:not([hidden])', { timeout: 15000 });
    const t = await text(page, '#drawer .drawer-stale');
    check(/Can’t reach the bot|Could not refresh/.test(t) && /Showing data from .*ago/.test(t), 'the drawer says its data is not updating: ' + t);
    check(await page.isVisible('#drawer .chart svg .price-line'), 'the last chart stays visible');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await page.waitForSelector('#drawer .drawer-stale', { state: 'hidden', timeout: 20000 });
    await closeDrawer(page);
  });

  await run('robustness-9', page, async () => {
    await patch(page, '**/api/status', (j) => {
      j.view_error = 'RuntimeError: view exploded';
      j.tracker.cycles = 0;
      j.tracker.last_snapshot_at = null;
    });
    await open(page, '#overview', { noWait: true });
    await page.waitForSelector('#view-error-banner:not([hidden])', { timeout: 15000 });
    check(await page.isHidden('#startup-banner'), 'no "Getting ready" banner next to the view-error banner');
    check((await text(page, '#live-label')) !== 'Starting…', 'the header does not claim the tracker is starting');
  });

  await run('robustness-10', page, async () => {
    await patch(page, '**/api/strategy', (j, now) => { j.now = Date.now() / 1000; j.generated_at = Date.now() / 1000; });
    let bump = 0;
    await patch(page, '**/api/surges', (j) => {
      bump += 1;
      for (const s of j.surges) { s.current_price = Number((0.5 + (bump % 7) / 1000).toFixed(3)); s.reverted_fraction = (bump % 9) / 10; }
    });
    await open(page, '#strategy');
    await page.waitForSelector('#strategy-body .principles li');
    const sel = await page.evaluate(() => {
      const li = document.querySelector('#strategy-body .principles li');
      const r = document.createRange();
      r.selectNodeContents(li);
      getSelection().removeAllRanges();
      getSelection().addRange(r);
      return getSelection().toString();
    });
    await waitStatusPolls(page, 2);
    await page.waitForTimeout(400);
    const still = await page.evaluate(() => getSelection().toString());
    check(still && still === sel, 'a selection in the Strategy principles survives polls');
    await page.evaluate(() => getSelection().removeAllRanges());
    await gotoView(page, 'surges');
    await page.waitForSelector('#surge-cards .surge-card .reasons li');
    const sel2 = await page.evaluate(() => {
      const li = document.querySelector('#surge-cards .surge-card .reasons li');
      const r = document.createRange();
      r.selectNodeContents(li);
      getSelection().removeAllRanges();
      getSelection().addRange(r);
      return getSelection().toString();
    });
    const nowBefore = await text(page, '#surge-cards .surge-card .move-line');
    await waitStatusPolls(page, 2);
    await page.waitForTimeout(400);
    const still2 = await page.evaluate(() => getSelection().toString());
    check(still2 && still2 === sel2, 'a selection in a surge card\'s reasons survives polls');
    const nowAfter = await text(page, '#surge-cards .surge-card .move-line');
    check(nowBefore !== nowAfter, 'the live numbers on the card still update in place');
    await page.evaluate(() => getSelection().removeAllRanges());
  });

  await run('live-api-2', page, async () => {
    let legacy = false;
    await patch(page, '**/api/status', (j, now) => {
      if (legacy) {
        delete j.problems;
        j.tracker.recent_errors = [{ at: now - 3, where: 'prices', error: 'HTTP 503 SERVICE_UNAVAILABLE: The authoritative exchange orderbooks are temporarily unavailable. (GET /exchanges/prices)' }];
      } else {
        j.problems = [
          { source: 'prices', message: 'HTTP 503 SERVICE_UNAVAILABLE: The authoritative exchange orderbooks are temporarily unavailable. (GET /exchanges/prices)', since: now - 125, last: now - 2, count: 31, severity: 'error' },
          { source: 'rate_limit', message: 'Rate limited by the API (HTTP 429): waiting 20 s', since: now - 12, last: now - 1, count: 3, severity: 'warning' },
        ];
      }
    });
    await open(page, '#overview');
    await page.waitForSelector('#problems-banner:not([hidden])', { timeout: 15000 });
    const t = await text(page, '#problems-banner');
    check(/Some data is unavailable/.test(t) && /Price snapshots: HTTP 503 SERVICE_UNAVAILABLE/.test(t) && /since/.test(t) && /31 failed attempts/.test(t), 'problems are listed with source, message and since: ' + t.slice(0, 200));
    check(/Rate limit/.test(t), 'the rate limit is listed');
    const meter = await page.evaluate(() => ({ level: document.getElementById('budget-meter').getAttribute('data-level'), text: document.getElementById('budget-text').textContent }));
    check(meter.level === 'critical' && /Rate limited|Paused/.test(meter.text), 'the budget meter shows the throttled state: ' + JSON.stringify(meter));
    const cls = await page.getAttribute('#problems-banner', 'class');
    const offline = await page.getAttribute('#offline-banner', 'class');
    check(/banner-warning/.test(cls) && /banner-neutral/.test(offline) && !/banner-critical/.test(cls), 'yellow problems banner, distinct from the grey offline and red fatal ones');
    legacy = true;
    await waitStatusPolls(page, 1);
    await page.waitForFunction(() => !/Rate limit/.test(document.getElementById('problems-banner').textContent), null, { timeout: 10000 });
    check(/Price snapshots: HTTP 503/.test(await text(page, '#problems-banner')), 'older servers: recent tracker errors still show');
  });

  await run('live-api-13', page, async () => {
    await patch(page, '**/api/markets', (j) => { for (const r of j.rows) if (r.exchange_id === '9001') r.last = null; });
    await patch(page, '**/api/exchange/9001', (j) => { j.exchange.last = null; });
    await open(page, '#markets');
    await page.waitForSelector('#markets-body tr[data-eid="9001"]');
    const cell = await page.$eval('#markets-body tr[data-eid="9001"] td:nth-child(3) span', (el) => ({ text: el.textContent, title: el.title }));
    check(/^—/.test(cell.text) && cell.title === 'No tournament trades yet', 'Last shows a dash for an outcome that never traded: ' + JSON.stringify(cell));
    await openDrawer(page, '9001');
    const facts = await page.evaluate(() => {
      const out = {};
      for (const div of document.querySelectorAll('#drawer .drawer-facts > div')) out[div.querySelector('dt').textContent] = div.querySelector('dd').textContent;
      return out;
    });
    check(/^—/.test(facts['Last trade']) && /^\d\.\d{3}$/.test(facts.Mark), 'the drawer shows "Last trade —" and the mark separately: ' + JSON.stringify(facts));
    await closeDrawer(page);
  });

  // ---- contract fields (all optional; older data renders too)
  await run('contract-closed', page, async () => {
    await patch(page, '**/api/surges', (j) => { if (j.surges[0]) j.surges[0].status = 'closed'; });
    await open(page, '#surges');
    await page.waitForSelector('#surge-cards .status-badge');
    const badge = await text(page, '#surge-cards .surge-card .status-badge');
    check(/^Market closed$/.test(badge), 'a closed market reads "Market closed": ' + badge);
  });

  await run('contract-news-status', page, async () => {
    let ns = 'unavailable';
    await patch(page, '**/api/surges', (j) => { const a = j.surges[0] && j.surges[0].attribution; if (a) { a.articles = []; a.news_status = ns; } });
    await open(page, '#surges');
    // the demo's first surge can take a while on a loaded machine: wait for a card before reading it
    await page.waitForSelector('#surge-cards .surge-card', { timeout: 60000 });
    await page.waitForFunction(() => /News search unavailable/.test((document.querySelector('#surge-cards .surge-card') || {}).textContent || ''), null, { timeout: 15000 });
    check(!/No matching headlines/.test(await text(page, '#surge-cards .surge-card')), 'an outage is not reported as "no matching headlines"');
    ns = 'disabled';
    await page.waitForFunction(() => /News search off/.test((document.querySelector('#surge-cards .surge-card') || {}).textContent || ''), null, { timeout: 15000 });
  });

  await run('contract-detection', page, async () => {
    let det = { enabled: true, waiting_for_history: 3, live_only: 0, reason: 'Waiting for price history for 3 of 22 outcome(s) before detecting surges there (at most 10 min)' };
    let empty = true;
    await patch(page, '**/api/status', (j) => { j.detection = det; });
    await patch(page, '**/api/surges', (j) => { if (empty) j.surges = []; });
    await open(page, '#surges');
    await page.waitForFunction(() => /^No surges yet\. Waiting for price history for 3 of 22 outcome\(s\) before detecting surges there \(at most 10 min\)\. Watching \d+ outcomes\.$/.test(document.getElementById('surges-empty').textContent), null, { timeout: 15000 });
    det = { enabled: false, waiting_for_history: 22, live_only: 0, reason: 'Waiting for the market list before surge detection can start' };
    await page.waitForFunction(() => /^Surge detection is off: Waiting for the market list before surge detection can start\./.test(document.getElementById('surges-empty').textContent), null, { timeout: 15000 });
    det = { enabled: true, waiting_for_history: 0, live_only: 22, reason: 'For 22 of 22 outcome(s) price history is unavailable (HTTP 503): surges are detected from live prices only' };
    empty = false;
    await page.waitForSelector('#surges-note:not([hidden])', { timeout: 15000 });
    check(/^Surge detection: For 22 of 22 outcome\(s\) price history is unavailable/.test(await text(page, '#surges-note')), 'a live-prices-only note shows above the cards');
  });

  await run('contract-book-pending', page, async () => {
    await patch(page, '**/api/exchange/9002', (j) => { j.book = null; j.book_pending = true; j.book_error = null; });
    await open(page, '#markets');
    await openDrawer(page, '9002');
    await page.waitForFunction(() => /Loading the order book…/.test(document.getElementById('drawer-body').textContent));
    check(await page.isVisible('#drawer .chart svg .price-line'), 'the chart renders while the book loads');
    check((await page.$$('#drawer .surge-card')).length === 1, 'the surge card renders while the book loads');
    await closeDrawer(page);
  });

  await run('contract-assumptions', page, async () => {
    await patch(page, '**/api/strategy', (j) => { j.assumptions = ['The leaderboard is unavailable: rank assumed to be 10'] ; });
    await open(page, '#strategy');
    await page.waitForSelector('#strategy-body .risk-banner .assumptions li');
    check(/rank assumed to be 10/.test(await text(page, '#strategy-body .risk-banner .assumptions')), 'assumptions are noted under the risk banner');
  });

  await page.context().close();
}

// ------------------------------------------------------------------ simulation view (docs/PAPER_TRADING.md §8.4)

const JSON_HEADERS = { 'content-type': 'application/json' };

/** Serve the Simulation endpoints from fixtures (§10). opts: paper(now, url) -> body or {status, body}; fairvalue,
 *  backtest likewise; resets: an array that records every POST /api/paper/reset; gets: records GET /api/paper URLs. */
async function simRoutes(page, opts) {
  opts = opts || {};
  const reply = (route, out) => {
    const res = out && out.__status ? out : { __status: 200, body: out };
    return route.fulfill({ status: res.__status, headers: JSON_HEADERS, body: JSON.stringify(res.body) });
  };
  await page.route((u) => u.pathname === '/api/paper', (route) => {
    const now = Date.now() / 1000;
    const u = new URL(route.request().url());
    if (opts.gets) opts.gets.push(u.search);
    if (u.searchParams.get('run') === 'previous') return reply(route, opts.previous ? opts.previous(now) : SF.previous(now));
    return reply(route, (opts.paper || SF.paper)(now, {}));
  });
  await page.route((u) => u.pathname === '/api/paper/reset', (route) => {
    const req = route.request();
    if (opts.resets) opts.resets.push({ method: req.method(), type: req.headers()['content-type'], body: req.postData() });
    return reply(route, { reset: true, run_id: 'run-2', previous_run_id: 'run-1',
      message: 'Started a new 24-hour simulation with 100,000 SUSQies per portfolio. The previous run\'s result is kept under Previous run.' });
  });
  await page.route((u) => u.pathname === '/api/fairvalue', (route) => reply(route, (opts.fairvalue || SF.fairvalue)(Date.now() / 1000)));
  await page.route((u) => u.pathname === '/api/backtest', (route) => reply(route, (opts.backtest || SF.backtest)(Date.now() / 1000)));
}

async function openSim(page) {
  await open(page, '#sim', { noWait: true });
  await page.waitForSelector('#sim-body > *', { timeout: 20000 });
}

function waitText(page, sel, re, timeout) {
  return page.waitForFunction((a) => {
    const el = document.querySelector(a[0]);
    return !!el && new RegExp(a[1]).test(el.textContent.replace(/\s+/g, ' '));
  }, [sel, re.source], { timeout: timeout || 15000 });
}

async function simChecks(browser) {
  const page = await newPage(browser);

  await run('sim-empty-run', page, async () => {
    await simRoutes(page, { paper: (now) => SF.paper(now, { empty: true }) });
    await openSim(page);
    await waitText(page, '#sim-body [data-panel="clock"]', /Waiting for the first simulated step \(every 30 s\)\./);
    check(/0\.0 h observed of the 24-hour test/.test(await text(page, '#sim-clock-text')), 'the clock reads 0.0 h of the 24-hour test');
    const bar = await page.evaluate(() => { const b = document.querySelector('#sim-body [role="progressbar"]'); return b && [b.getAttribute('aria-valuenow'), b.getAttribute('aria-valuemax')]; });
    check(bar && bar[0] === '0' && bar[1] === '24', 'the progress bar runs from 0 to the 24 target hours: ' + JSON.stringify(bar));
    check(!(await page.$('#sim-body [data-panel="headline"]')), 'no headline before the first step');
    check(await page.isVisible('#sim-body [data-panel="fairvalue"]'), 'the fair values still show');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await simRoutes(page, { paper: (now) => Object.assign(SF.paper(now, { empty: true }), { run: null }) });
    await openSim(page);
    await waitText(page, '#sim-body .sim-note', /^Waiting for the first simulated step \(every \d+ s\)\.$/);
  });

  await run('sim-populated', page, async () => {
    await simRoutes(page);
    await openSim(page);
    await page.waitForSelector('#sim-body [data-panel="headline"] .verdict-sentence');
    const head = await text(page, '#sim-body [data-panel="headline"]');
    check((await page.getAttribute('#sim-body [data-panel="headline"]', 'data-portfolio')) === 'human:conservative', 'the headline is the pre-registered human: portfolio');
    check(/You, acting by hand ~4 min late: all ideas, conservative sizing/.test(head), 'the headline label is shown');
    check(/\+412 SUSQies profit/.test(head) && /\+0\.41% at liquidation value/.test(head) && /\(at mid marks: \+1,234\)/.test(head), 'P&L, % and mid marks: ' + head.slice(0, 200));
    const fx = SF.paper(Date.now() / 1000, {});
    check((await text(page, '#sim-body [data-panel="headline"] .verdict-sentence')) === fx.headline.verdict.sentence, 'the verdict sentence is verbatim');
    check((await text(page, '#sim-body [data-panel="headline"] .verdict-level')) === 'Not enough evidence', 'the verdict badge has its icon and text');
    const cav = await page.$$eval('#sim-body [data-panel="headline"] .verdict-caveats li', (els) => els.map((e) => e.textContent));
    check(cav.length === 4 && cav[0] === SF.CAVEATS[0] && cav[3] === SF.CAVEATS[3], 'the four verdict caveats sit directly under the verdict');
    check(/1 position whose market closed without a ruling is left out/.test(head) && /8% of the value is marked without a recent order book/.test(head), 'unvalued and depth-unknown lines');
    check((await text(page, '#sim-clock-text')) === '6.0 h observed of the 24-hour test (1 gap; 6.4 h on the clock)', 'covered hours, gaps and clock hours: ' + (await text(page, '#sim-clock-text')));
    const clock = await text(page, '#sim-body [data-panel="clock"]');
    check(/Settlement rule assumed: unknown, valued conservatively/.test(clock) && /100,000 SUSQies \(default 100,000\)/.test(clock), 'regime and capital source: ' + clock.slice(0, 300));
    await page.click('#sim-body [data-panel="clock"] .sim-details summary');
    check(/min_value_edge = 0\.03/.test(await text(page, '#sim-body [data-panel="clock"]')) && /0\.1\.0\+bb37b8755/.test(await text(page, '#sim-body [data-panel="clock"]')), 'the run settings list the changed parameters and code version');
    // portfolios: three groups, the warning above, exploratory badges, the model column's footnote
    const groups = await page.$$eval('#sim-body .portfolios-table tbody .group-row', (els) => els.map((e) => e.textContent.trim()));
    check(groups.join('|') === 'Your headline (decided before the run)|Bot speed: an upper bound for acting by hand|By strategy (equal capital, conservative sizing, exploratory)', 'three portfolio groups: ' + groups.join('|'));
    const ids = await page.$$eval('#sim-body .portfolios-table tbody tr[data-pid]', (els) => els.map((e) => e.dataset.pid));
    check(ids.join(',') === SF.PORTFOLIOS.map((p) => p[0]).join(','), 'every portfolio in config order, headline first: ' + ids.join(','));
    check((await text(page, '#sim-body [data-panel="portfolios"] .table-warning')) === SF.TABLE_WARNING, 'the table warning is the fixed sentence');
    check(/\* The model's own opinion/.test(await text(page, '#sim-body [data-panel="portfolios"] .footnote')), 'the model valuation is labelled');
    check(/No fade signals in 6\.0 h/.test(await text(page, '#sim-body .portfolios-table tr[data-pid="kind:fade"]')), 'a portfolio without fills says why');
    // no text names a best portfolio (the fixed warnings are the only place the word may appear)
    const best = await page.evaluate(() => {
      const root = document.getElementById('sim-body').cloneNode(true);
      for (const w of root.querySelectorAll('.table-warning')) w.remove();
      const host = document.querySelector('#sim-body .sim-chart');
      return { text: /\bbest\b/i.test(root.textContent), label: /\bbest\b/i.test(host.getAttribute('aria-label') || '') };
    });
    check(!best.text && !best.label, 'nothing names or highlights a best portfolio: ' + JSON.stringify(best));
    // equity chart: one line per portfolio, the headline thicker and labelled, kinds told apart by dash patterns
    const eq = await page.evaluate(() => {
      const paths = Array.from(document.querySelectorAll('#sim-body .sim-chart path.eq-line'));
      return {
        n: paths.length, head: paths.filter((p) => p.classList.contains('eq-headline')).length,
        dashes: paths.filter((p) => p.classList.contains('eq-kind')).map((p) => p.getAttribute('stroke-dasharray')),
        label: (document.querySelector('#sim-body .sim-chart .end-label') || {}).textContent,
        aria: document.querySelector('#sim-body .sim-chart').getAttribute('aria-label'),
        legend: Array.from(document.querySelectorAll('#sim-body .eq-legend button')).map((b) => b.getAttribute('aria-pressed')),
      };
    });
    check(eq.n === 9 && eq.head === 1, 'nine equity lines, one headline: ' + JSON.stringify(eq));
    check(new Set(eq.dashes).size === eq.dashes.length && eq.dashes.every(Boolean), 'every kind line has its own dash pattern: ' + eq.dashes.join(' | '));
    check(/^You, by hand [+−]/.test(eq.label || ''), 'the headline is labelled at its end: ' + eq.label);
    check(/^Equity curves: 9 portfolios; headline \(you, by hand\): \+412 at liquidation$/.test(eq.aria), 'the chart summary names the headline only: ' + eq.aria);
    check(eq.legend.length === 9 && eq.legend.every((x) => x === 'true'), 'the legend is a row of pressed toggle buttons');
    await page.focus('#sim-body .eq-legend button[data-pid="kind:value"]');
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .sim-chart path.eq-line').length === 8);
    check((await page.getAttribute('#sim-body .eq-legend button[data-pid="kind:value"]', 'aria-pressed')) === 'false', 'a legend toggle hides its line (keyboard)');
    await page.keyboard.press('Space');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .sim-chart path.eq-line').length === 9);
    await page.focus('#sim-body .sim-chart');
    await page.keyboard.press('ArrowLeft');
    await page.waitForSelector('#sim-body .sim-chart .tooltip:not([hidden])');
    const tip = await text(page, '#sim-body .sim-chart .tooltip');
    check(/You, by hand \(headline\)/.test(tip) && /Arbitrage only/.test(tip), 'the readout lists every portfolio at that time: ' + tip.slice(0, 160));
    await page.waitForFunction(() => /Time \d+ of \d+\./.test(document.querySelector('#sim-body [data-panel="equity"] [role="status"]').textContent));
    // positions, baskets, fills and trades in plain words (the filter starts on the headline portfolio)
    check((await page.inputValue('#sim-pos-filter')) === SF.PORTFOLIOS[0][0], 'open positions start filtered to the headline: ' + (await page.inputValue('#sim-pos-filter')));
    await page.selectOption('#sim-pos-filter', '');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .positions-table tbody tr').length > 1);
    const pos = await text(page, '#sim-body [data-panel="positions"]');
    for (const w of ['depth from an older book', 'depth unknown: valued with a haircut', 'market closed, no ruling yet (not counted)', 'quote stale', 'after the Cup end', 'bold bet']) {
      check(pos.indexOf(w) !== -1, 'position flag in plain words: ' + w);
    }
    check(/Right after entry a set is worth less at liquidation than its cost: that is the spread, not a loss of the locked edge\./.test(pos), 'the basket sentence');
    check(/Floor at settlement/.test(pos) && /Now at liquidation/.test(pos) && /300\.00/.test(pos) && /279\.00/.test(pos), 'floor and liquidation side by side');
    await page.selectOption('#sim-pos-filter', 'kind:hole');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .positions-table tbody tr').length === 1);
    await page.selectOption('#sim-pos-filter', '');
    const fills = await page.$$eval('#sim-body .fills-table tbody tr .fill-text', (els) => els.map((e) => e.textContent));
    check(fills.join('|') === 'Bought 300 YES @ 0.695|Sold 300 NO @ 0.520|Settled 800 YES @ 1.000', 'fills read as orders: ' + fills.join('|'));
    const trades = await text(page, '#sim-body [data-panel="trades"]');
    check(/Converged: the gap closed/.test(trades) && /Settled/.test(trades) && /The fair value moved/.test(trades) && /2\.3 h/.test(trades), 'closed trades: exit reasons and hold times');
    const chaser = await text(page, '#sim-body [data-panel="chaser"]');
    check(/chase \(kept by hysteresis\)/.test(chaser) && /221,500 SUSQies \(range 221,500–236,000\)/.test(chaser) && /M\s*2\.2(?!\d)/.test(chaser) && /can also lose most of its capital/.test(chaser), 'chaser sizing: ' + chaser.slice(0, 260));
    const cv = await page.$$eval('#sim-body [data-panel="caveats"] li', (els) => els.map((e) => e.textContent));
    check(cv.length === 10 && cv[9] === SF.DEMO_CAVEAT, 'How to read this lists every caveat (and the demo one)');
    check(await page.isVisible('#sim-demo-badge'), 'the demo badge shows');
  });

  await run('sim-disabled', page, async () => {
    await simRoutes(page, { paper: (now) => SF.paperDisabled(now) });
    await openSim(page);
    await waitText(page, '#sim-body .sim-note', /^The paper trader is off\. Start the dashboard without --no-paper to simulate\.$/);
    check(!(await page.$('#sim-body [data-panel="headline"]')) && !(await page.$('#sim-body [data-panel="portfolios"]')), 'no simulation panels while it is off');
    check((await text(page, '#live-label')) !== 'Offline' && (await page.isHidden('#offline-banner')), 'not reported as offline');
    check(await page.isHidden('#view-errors-sim'), 'no error line for a disabled simulator');
  });

  await run('sim-endpoint-error', page, async () => {
    await simRoutes(page, { paper: () => ({ __status: 500, body: { error: 'Internal error; see the dashboard\'s log.' } }) });
    await openSim(page);
    await page.waitForSelector('#view-errors-sim:not([hidden])', { timeout: 15000 });
    check(/Could not load the simulation: Internal error/.test(await text(page, '#view-errors-sim')), 'the failure shows inline in the view: ' + (await text(page, '#view-errors-sim')));
    check(/Could not load the simulation\. Retrying every 5 s\./.test(await text(page, '#sim-body .sim-note')), 'the view says it retries');
    check((await text(page, '#live-label')) === 'Live' && (await page.isHidden('#offline-banner')), 'the header stays Live, no Offline banner');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await simRoutes(page, { backtest: () => ({ __status: 503, body: { error: 'busy' } }) });
    await page.waitForSelector('#sim-body [data-panel="headline"]', { timeout: 15000 });
  });

  await run('sim-reset-dialog', page, async () => {
    const resets = [];
    const gets = [];
    await simRoutes(page, { resets, gets });
    await openSim(page);
    await page.waitForSelector('#sim-reset');
    await page.click('#sim-reset');
    await page.waitForFunction(() => document.getElementById('sim-reset-dialog').open);
    check((await page.evaluate(() => document.activeElement.id)) === 'sim-reset-cancel', 'the dialog opens with focus on Cancel');
    check(/Every portfolio goes back to its start capital and the 24-hour clock restarts\. The current run’s result is kept under "Previous run"\. Nothing real is affected\./.test(await text(page, '#sim-reset-text')), 'the dialog explains the reset');
    await page.click('#sim-reset-cancel');
    await page.waitForFunction(() => !document.getElementById('sim-reset-dialog').open);
    check((await page.evaluate(() => document.activeElement.id)) === 'sim-reset', 'focus returns to the Reset button');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.getElementById('sim-reset-dialog').open);
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.getElementById('sim-reset-dialog').open);
    await page.waitForTimeout(300);
    check(resets.length === 0, 'Cancel and Esc send nothing (' + resets.length + ' requests)');
    const before = gets.length;
    await page.click('#sim-reset');
    await page.click('#sim-reset-confirm');
    await waitText(page, '#sim-status', /^Started a new 24-hour simulation with 100,000 SUSQies per portfolio\./);
    await page.waitForTimeout(500);
    check(resets.length === 1, 'Confirm sends exactly one request (' + resets.length + ')');
    check(resets[0].method === 'POST' && /^application\/json/.test(resets[0].type || '') && JSON.stringify(JSON.parse(resets[0].body)) === '{"confirm":true}', 'one POST with the JSON body: ' + JSON.stringify(resets[0]));
    check((await page.getAttribute('#sim-status', 'role')) === 'status', 'the result goes to a status region');
    check(gets.length > before, 'the view reloads its data after the reset');
    check(!(await page.evaluate(() => document.getElementById('sim-reset-dialog').open)), 'the dialog closed');
  });

  await run('sim-equity-table', page, async () => {
    await simRoutes(page, { paper: (now) => SF.paper(now, { points: 300 }) });
    await openSim(page);
    await page.waitForSelector('#sim-body .sim-chart path.eq-line');
    const btn = '#sim-body button[data-action="sim-table-toggle"]';
    await page.click(btn);
    await page.waitForSelector('#sim-body .eq-table tbody tr');
    const t = await page.evaluate(() => ({
      rows: document.querySelectorAll('#sim-body .eq-table tbody tr').length,
      cols: document.querySelectorAll('#sim-body .eq-table thead th').length,
      chart: document.querySelector('#sim-body .sim-chart').hidden,
    }));
    check(t.rows <= 50 && t.rows >= 40 && t.cols === 10 && t.chart, 'the table lists up to 50 evenly spaced times x 9 portfolios: ' + JSON.stringify(t));
    check((await page.getAttribute(btn, 'aria-pressed')) === 'true' && (await text(page, btn)) === 'Show as table', 'the toggle keeps one label and says it is pressed');
    await page.click(btn);
    await page.waitForSelector('#sim-body .sim-chart path.eq-line');
    check(await page.isHidden('#sim-body .sim-eq-table'), 'the chart is back');
  });

  await run('sim-verdict-badges', page, async () => {
    let level = 'insufficient';
    await simRoutes(page, { paper: (now) => SF.paper(now, { level }) });
    await openSim(page);
    const want = { insufficient: 'Not enough evidence', inconclusive: 'Inconclusive', promising: 'Promising, not proof', positive: 'Profitable so far', negative: 'Losing so far' };
    for (const lv of Object.keys(want)) {
      level = lv;
      await page.waitForFunction((w) => {
        const b = document.querySelector('#sim-body [data-panel="headline"] .verdict-level');
        return b && b.textContent.trim() === w;
      }, want[lv], { timeout: 15000 });
      const s = await text(page, '#sim-body [data-panel="headline"] .verdict-sentence');
      check(s === SF.paper(Date.now() / 1000, { level: lv }).headline.verdict.sentence, lv + ': the sentence is shown verbatim: ' + s.slice(0, 60));
      check(await page.$('#sim-body [data-panel="headline"] .verdict-level .icon'), lv + ': the badge has an icon, not only a colour');
    }
    const rows = await page.$$eval('#sim-body .portfolios-table tbody tr[data-pid] .cell-verdict', (els) => els.map((e) => e.textContent.trim()));
    check(rows[0] === 'Profitable so far' || rows[0] === 'Losing so far', 'the headline row is not exploratory: ' + rows[0]);
    check(rows.indexOf('Exploratory: promising, not proof') !== -1 && rows.indexOf('Exploratory: losing so far') !== -1 && rows.indexOf('Exploratory: inconclusive') !== -1, 'exploratory rows read "Exploratory: <level>": ' + rows.join(' | '));
    check(rows.slice(1).every((r) => /^Exploratory: /.test(r)), 'every other portfolio is exploratory');
  });

  await run('sim-study', page, async () => {
    await simRoutes(page);
    await openSim(page);
    await page.waitForSelector('#sim-body [data-panel="study"] .study-table tbody tr');
    check((await text(page, '#sim-body [data-panel="study"] .study-can')) === SF.CAN_SHOW, 'the "can show" sentence is verbatim');
    check((await text(page, '#sim-body [data-panel="study"] .study-cannot')) === SF.CANNOT_SHOW, 'the "cannot show" sentence is verbatim');
    const heads = await page.$$eval('#sim-body [data-panel="study"] .study-table thead th', (els) => els.map((e) => e.textContent.trim()));
    check(heads.join('|') === 'Kind|+5 min|+30 min|+2 h|+6 h', 'columns +5 min, +30 min, +2 h, +6 h: ' + heads.join('|'));
    const cells = await page.$$eval('#sim-body [data-panel="study"] .study-table tbody tr', (trs) => trs.map((tr) => Array.from(tr.querySelectorAll('td')).map((td) => td.textContent.replace(/\s+/g, ' ').trim())));
    check(cells[0][0] === '212 signals: +0.004 (−0.001 to +0.009) per share; converged 41%, reversed 12%', 'value +5 min: ' + cells[0][0]);
    check(cells[0][2] === '120 signals: +0.011 per share; converged 58%, reversed 18%', 'an interval only when present: ' + cells[0][2]);
    check(cells[1][0] === '9 signals: +0.012 per set; converged 33%' && cells[1][3] === 'No signals yet', 'basket per set: ' + cells[1].join(' | '));
  });

  await run('sim-testability', page, async () => {
    let status = 'ready';
    await simRoutes(page, { backtest: (now) => SF.backtest(now, status) });
    await openSim(page);
    await page.waitForSelector('#sim-body [data-panel="backtest"] .testability-table tbody tr');
    const order = await page.evaluate(() => {
      const panel = document.querySelector('#sim-body [data-panel="backtest"]');
      const tables = Array.from(panel.querySelectorAll('table'));
      const win = panel.querySelector('.bt-window');
      return { first: tables[0].classList.contains('testability-table'), before: !!(win && (tables[0].compareDocumentPosition(win) & Node.DOCUMENT_POSITION_FOLLOWING)) };
    });
    check(order.first && order.before, 'the testability table comes first in the report: ' + JSON.stringify(order));
    const rows = await page.$$eval('#sim-body .testability-table tbody tr', (trs) => trs.map((tr) => tr.dataset.kind + ':' + tr.querySelector('td').textContent.trim()));
    check(rows.join(',') === 'value:Not testable yet,basket:Partly testable,hole:Partly testable,fade:Partly testable,carry:Testable,arbitrage:Not replayable', 'kind, status word: ' + rows.join(','));
    const bt = await text(page, '#sim-body [data-panel="backtest"]');
    check(/not an independent check, so agreement between the two is not evidence/.test(await text(page, '#sim-body .bt-overlap')), 'the overlap warning is prominent');
    check(/The best of 2 settings is an optimistic estimate/.test(bt) && /latency_s=300/.test(bt), 'the sweep table with its warning');
    check(/Quotes: 62% from live snapshots, 38% from candles/.test(bt) && /Only 9 hours of data/.test(bt) && /cannot tell the sizing policies apart/.test(bt), 'coverage, assumptions and warnings');
    for (const [st, re] of [['pending', /Replaying the stored history/], ['no_data', /Less than 1 hour of prices is stored yet/], ['error', /The backtest failed: database is locked/]]) {
      status = st;
      await openSim(page); // a fresh load: the backtest is otherwise asked again only every 30 s
      await waitText(page, '#sim-body [data-panel="backtest"] .bt-status', re, 20000);
    }
  });

  await run('sim-fairvalue-suspect', page, async () => {
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
    const consoleErrors = [];
    const onConsole = (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); };
    page.on('console', onConsole);
    await simRoutes(page);
    await openSim(page);
    const row = '#sim-body .fv-table tr[data-eid="9031"]';
    await page.waitForSelector(row);
    const t = await text(page, row);
    check(/suspect match/.test(t) && /No/.test(t) && /Suspect: 0\.34 away/.test(t), 'a suspect row says so: ' + t.slice(0, 200));
    check(/near match: shown, not traded/.test(await text(page, '#sim-body .fv-table tr[data-eid="9016"]')), 'a near match is shown, not traded');
    check(/Polymarket: offline from this machine/.test(await text(page, '#sim-body [data-panel="fairvalue"]')), 'provider status in words');
    await page.click(row + ' .fix-details summary');
    const pres = await page.$$eval(row + ' .fix-details pre', (els) => els.map((e) => e.textContent));
    check(pres.length === 3 && pres[2] === '"9031": {"polymarket": "512301", "confirmed": true}', 'the disable, pin and confirm snippets: ' + pres.join(' | '));
    check(/Paste it under "overrides" in \/home\/user\/\.supermarket\/2026-midterms\/fair_value_map\.json; it applies within a minute\./.test(await text(page, row + ' .fix-details')), 'the paste instruction names the map file');
    await page.click(row + ' .fix-details .btn-copy >> nth=2');
    await waitText(page, row + ' .fix-details .snippet-block:nth-child(3) .copy-label', /^Copied$/);
    check((await page.evaluate(() => navigator.clipboard.readText())) === pres[2], 'Copy puts the snippet on the clipboard');
    // no clipboard access: the snippet is selected instead, without errors
    await page.evaluate(() => { navigator.clipboard.writeText = () => Promise.reject(new DOMException('Write permission denied.', 'NotAllowedError')); });
    await page.click(row + ' .fix-details .btn-copy >> nth=0');
    await waitText(page, row + ' .fix-details .snippet-block:nth-child(1) .copy-label', /Selected: press Ctrl\+C/);
    check((await page.evaluate(() => getSelection().toString())) === pres[0], 'without clipboard access the snippet text is selected');
    await page.evaluate(() => getSelection().removeAllRanges());
    check(!consoleErrors.length, 'no console errors: ' + consoleErrors.join(' | '));
    page.off('console', onConsole);
    // filters
    await page.check('#sim-fv-usable');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .fv-table tbody tr').length === 1);
    check(/Showing 1 of 4 outcomes/.test(await text(page, '#sim-fv-count')), 'Only usable filters the rows');
    await page.uncheck('#sim-fv-usable');
    await page.fill('#sim-fv-search', 'nebraska');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .fv-table tbody tr').length === 1);
    await page.fill('#sim-fv-search', '');
  });

  await run('sim-previous-run', page, async () => {
    const gets = [];
    let missing = false;
    await simRoutes(page, { gets, previous: (now) => (missing ? { __status: 404, body: { error: 'No earlier simulation run has ended yet.' } } : SF.previous(now)) });
    await openSim(page);
    await page.waitForSelector('#sim-previous-btn:not([hidden])');
    check(!gets.some((q) => /run=previous/.test(q)), 'the previous run is not loaded until asked for');
    await page.click('#sim-previous-btn');
    await page.waitForSelector('#sim-body [data-panel="previous"] table');
    check(gets.filter((q) => /run=previous/.test(q)).length === 1, 'opening it loads ?run=previous once');
    check((await page.getAttribute('#sim-previous-btn', 'aria-expanded')) === 'true', 'the button says it is expanded');
    const prev = await text(page, '#sim-body [data-panel="previous"]');
    check(/^Previous run\s*Ended .+\(Simulation reset\)/.test(prev) && /Promising, not proof/.test(prev), 'reason, end time and the headline verdict: ' + prev.slice(0, 200));
    check((await page.$$('#sim-body [data-panel="previous"] tbody tr')).length === 9, 'one row per portfolio');
    await page.click('#sim-previous-btn');
    await page.waitForFunction(() => !document.querySelector('#sim-body [data-panel="previous"]') || !document.querySelector('#sim-body [data-panel="previous"]').isConnected);
    missing = true;
    await page.click('#sim-previous-btn');
    await waitText(page, '#sim-body [data-panel="previous"]', /No earlier simulation run has ended yet\./);
    await page.click('#sim-previous-btn');
  });

  // ui-1: the ended run's verdict (where a reset, a "settings changed" end or a finished 24-h test leaves it, D60) carries
  // its verdict caveats directly under it (D54), the best-of-9 table warning (D4) and the demo caveat; so does the
  // backtest's portfolio table (TABLE_WARNING with n = its rows when the report has no table_warning of its own).
  await run('ui-1', page, async () => {
    let demo = true;
    await simRoutes(page, { previous: (now) => Object.assign(SF.previous(now), demo ? {} : { demo: false, caveats: SF.CAVEATS.slice() }) });
    await openSim(page);
    await page.waitForSelector('#sim-previous-btn:not([hidden])');
    await page.click('#sim-previous-btn');
    await page.waitForSelector('#sim-previous .verdict-sentence');
    const r = await page.evaluate(() => {
      const p = document.getElementById('sim-previous');
      const verdict = p.querySelector('.sim-verdict');
      const cav = p.querySelector('.verdict-caveats');
      const warn = p.querySelector('.table-warning');
      const table = p.querySelector('table');
      const after = (a, b) => !!(a && b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
      return {
        caveats: cav ? Array.from(cav.querySelectorAll('li')).map((li) => li.textContent) : [],
        underVerdict: !!cav && verdict.nextElementSibling === cav, warning: warn ? warn.textContent.trim() : null,
        warningAboveTable: after(warn, table), demo: (p.querySelector('.demo-caveat') || {}).textContent || '', text: p.textContent,
      };
    });
    check(JSON.stringify(r.caveats) === JSON.stringify(SF.CAVEATS.slice(0, 4)), 'the previous run shows headline.verdict_caveats: ' + JSON.stringify(r.caveats));
    check(r.underVerdict, 'the verdict caveats sit directly under the previous verdict');
    check(r.warning === SF.TABLE_WARNING && r.warningAboveTable, 'the table warning sits above the previous run\'s portfolio table: ' + r.warning);
    check(r.demo.trim() === SF.DEMO_CAVEAT, 'the demo caveat is shown with the previous run: ' + r.demo);
    // a real-data previous run carries no demo caveat
    demo = false;
    await page.click('#sim-previous-btn');
    await page.click('#sim-previous-btn');
    await page.waitForFunction(() => { const p = document.getElementById('sim-previous'); return p && p.isConnected && p.querySelector('.verdict-sentence') && !p.querySelector('.demo-caveat'); });
    check((await text(page, '#sim-previous')).indexOf('Demo data') === -1 && !!(await page.$('#sim-previous .table-warning')), 'no demo caveat for a real run, the table warning stays');
    await page.click('#sim-previous-btn');
    // the backtest's "Portfolios in the replay" table
    await page.waitForSelector('#sim-body [data-panel="backtest"] .bt-table');
    const bt = await page.evaluate(() => {
      const p = document.querySelector('#sim-body [data-panel="backtest"]');
      const warn = p.querySelector('.table-warning');
      const table = p.querySelector('.bt-table');
      return { warning: warn ? warn.textContent.trim() : null, above: !!(warn && (warn.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING)), rows: table.querySelectorAll('tbody tr').length };
    });
    check(bt.rows === 9 && bt.warning === SF.TABLE_WARNING && bt.above, 'the backtest portfolio table carries the table warning (n = 9 rows): ' + JSON.stringify(bt));
  });

  // ui-8: right after a reset the new run has 0 steps ("waiting"); Previous run still opens its panel, after the clock
  await run('ui-8', page, async () => {
    const gets = [];
    await simRoutes(page, { gets, paper: (now) => SF.paper(now, { empty: true }) });
    await openSim(page);
    await waitText(page, '#sim-body [data-panel="clock"]', /Waiting for the first simulated step/);
    await page.waitForSelector('#sim-previous-btn:not([hidden])');
    await page.click('#sim-previous-btn');
    await page.waitForSelector('#sim-body [data-panel="previous"] .verdict-sentence', { timeout: 15000 });
    const r = await page.evaluate(() => {
      const panels = Array.from(document.querySelectorAll('#sim-body > [data-panel]')).map((p) => p.dataset.panel);
      return { panels, expanded: document.getElementById('sim-previous-btn').getAttribute('aria-expanded'), rows: document.querySelectorAll('#sim-previous tbody tr').length };
    });
    check(r.expanded === 'true' && r.rows === 9, 'the previous run renders while the new run waits: ' + JSON.stringify(r));
    check(r.panels.indexOf('previous') === r.panels.indexOf('clock') + 1, 'the previous run sits right after the clock: ' + r.panels.join(','));
    check(gets.filter((q) => /run=previous/.test(q)).length === 1, 'loaded once');
    await page.click('#sim-previous-btn');
    await page.waitForFunction(() => !document.getElementById('sim-previous') || !document.getElementById('sim-previous').isConnected);
    check((await page.getAttribute('#sim-previous-btn', 'aria-expanded')) === 'false', 'collapsing it says so');
  });

  // ui-10: a P&L under one SUSQie keeps its sign and marker (a -27% legging loss of -0.42 is not a neutral "● 0")
  await run('ui-10', page, async () => {
    await simRoutes(page, { paper: (now) => {
      const d = SF.paper(now, {});
      const t0 = d.trades[0];
      d.trades = [
        Object.assign({}, t0, { trade_id: 'kind:basket:t9', pnl: -0.42, return_pct: -0.274, cost: 1.53, proceeds: 1.11, exit_reason: 'legging' }),
        Object.assign({}, t0, { trade_id: 'kind:basket:t8', pnl: 0.3, return_pct: 0.02 }),
        Object.assign({}, t0, { trade_id: 'kind:basket:t7', pnl: 0.001, return_pct: 0.0001 }),
        Object.assign({}, t0, { trade_id: 'kind:basket:t6', pnl: -412.4, return_pct: -0.05 }),
      ];
      d.headline.pnl_liq = -0.42;
      d.headline.pnl_liq_pct = -0.0000042;
      return d;
    } });
    await openSim(page);
    await page.waitForSelector('#sim-body .trades-table tbody tr');
    const cells = await page.$$eval('#sim-body .trades-table tbody tr td.num .pnl', (els) => els.map((e) => e.className.replace('pnl delta ', '') + ' ' + e.textContent));
    check(cells.join(' | ') === 'down ▼−0.42 | up ▲+0.30 | flat ●0 | down ▼−412', 'small P&Ls keep their sign and marker: ' + cells.join(' | '));
    const head = await text(page, '#sim-body [data-panel="headline"] .sim-pnl');
    check(head === '▼−0.42 SUSQies loss', 'a headline loss under one SUSQie reads as a loss: ' + head);
  });

  // ui-12: no "would show — now" when the missed signals cannot be valued (holes), and a period before "It retries"
  await run('ui-12', page, async () => {
    await simRoutes(page, {
      paper: (now) => {
        const d = SF.paper(now, {});
        d.portfolios[5].execution = { hole: { entries: 6, filled: 0, fill_rate: 0, avg_slippage: null, unfilled: 4, unfilled_pnl_now: null, exits: 0, avg_exit_slippage: null } };
        return d;
      },
      backtest: (now) => SF.backtest(now, 'error'),
    });
    await openSim(page);
    await page.click('#sim-body .portfolios-table tr[data-pid="kind:hole"] details summary');
    const hole = await text(page, '#sim-body .portfolios-table tr[data-pid="kind:hole"] .exec-lines');
    check(hole === 'Hole: 6 entries, 0 filled (0%); 4 signals did not fill', 'an unvalued miss is just counted: ' + hole);
    await page.click('#sim-body .portfolios-table tr[data-pid="kind:value"] details summary');
    const value = await text(page, '#sim-body .portfolios-table tr[data-pid="kind:value"] .exec-lines');
    check(/; 2 signals that did not fill would show \+32 now;/.test(value), 'a valued miss keeps its clause: ' + value);
    await waitText(page, '#sim-body [data-panel="backtest"] .bt-status', /failed/);
    const bt = await text(page, '#sim-body [data-panel="backtest"] .bt-status');
    check(bt === 'The backtest failed: database is locked. It retries in a minute.', 'the error is a sentence before "It retries": ' + bt);
  });

  // ui-13: the equity chart's y axis spans at least ±0.5% of the start capital, so +30 on 100,000 is a near-flat line
  await run('ui-13', page, async () => {
    let move = 30;
    await simRoutes(page, { paper: (now) => {
      const d = SF.paper(now, {});
      for (const [pid, pts] of Object.entries(d.equity)) d.equity[pid] = pts.map(([t], k) => [t, 100000 + (pid.indexOf('human:') === 0 ? move : move / 6) * k / (pts.length - 1)]);
      d.headline.pnl_liq = move;
      d.headline.pnl_liq_pct = move / 100000;
      d.portfolios[0].pnl_liq = move;
      return d;
    } });
    const measure = () => page.evaluate(() => {
      const path = document.querySelector('#sim-body .sim-chart path.eq-line[data-pid^="human:"]');
      const hit = document.querySelector('#sim-body .sim-chart rect.hit');
      const ticks = Array.from(document.querySelectorAll('#sim-body .sim-chart text.tick')).map((t) => Number(t.textContent.replace(/,/g, ''))).filter((v) => v > 1000);
      return { share: path.getBBox().height / Number(hit.getAttribute('height')), lo: Math.min.apply(null, ticks), hi: Math.max.apply(null, ticks),
        summary: document.querySelector('#sim-body [data-panel="equity"] .chart-summary').textContent };
    });
    await openSim(page);
    await page.waitForSelector('#sim-body .sim-chart path.eq-line');
    const small = await measure();
    check(small.share < 0.1 && small.lo <= 99500 && small.hi >= 100500, '+0.03% stays a near-flat line on a ±0.5% axis: ' + JSON.stringify(small));
    check(/\(\+0\.03% of the start capital\)/.test(small.summary), 'the summary says how big the move is: ' + small.summary);
    move = 5000; // a +5% move still uses most of the plot
    await openSim(page);
    await page.waitForSelector('#sim-body .sim-chart path.eq-line');
    const big = await measure();
    check(big.share > 0.5 && big.hi >= 105000, 'a +5% move fills the plot: ' + JSON.stringify(big));
  });

  // Integration round (round 3): what the engine now publishes is shown where the number it qualifies is.
  // accounting-8 (a frozen row shows what the totals charge), accounting-6 (naked basket shares have no floor),
  // ui-3 (untested value ideas next to the "all ideas" label), ui-7 (legging exits apart from the win rate),
  // ui-4 (a run on the default capital says so), ui-6 (the chart's end label is the headline's P&L).
  await run('round3-sim-panels', page, async () => {
    const UNTESTED = 'Value ideas were not tested: usable outside fair values on 0% of steps (outside prices off, offline or not received yet), so this result covers basket, hole, fade, carry and arbitrage ideas only.';
    await simRoutes(page, { paper: (now) => {
      const d = SF.paper(now, {});
      d.headline.untested = [UNTESTED];
      d.headline.verdict = Object.assign({}, d.headline.verdict, { untested: [UNTESTED] });
      d.portfolios[0].legging_trades = 2;
      d.portfolios[0].legging_pnl = -3.2;
      const bk = d.baskets[0];
      bk.legs[0].qty = 400; bk.legs[0].naked_qty = 100; bk.naked_qty = 100;
      return d;
    } });
    await openSim(page);
    await page.waitForSelector('#sim-body [data-panel="headline"] .verdict-sentence');
    const label = await page.$eval('#sim-body [data-panel="headline"] .sim-label', (e) => e.nextElementSibling && e.nextElementSibling.textContent.trim());
    check(label === 'Value ideas were not tested (see the verdict below), so this is not all ideas.', 'ui-3: the untested kinds sit right under the "all ideas" label: ' + label);
    const row = await text(page, '#sim-body .portfolios-table tr[data-pid="human:conservative"]');
    check(/2 legging exits −3, not counted/.test(row) && /3 of 5/.test(row), 'ui-7: legging exits listed apart from the closed-ideas win rate: ' + row.slice(-200));
    const clock = await text(page, '#sim-body [data-panel="clock"]');
    check(/Your account value was not known when this run started, so every portfolio started with the default 100,000 SUSQies/.test(clock), 'ui-4: a default-capital run says so: ' + clock.slice(0, 400));
    const endLabel = await page.$eval('#sim-body .sim-chart .end-label', (e) => e.textContent);
    check(/^You, by hand \+412$/.test(endLabel), 'ui-6: the chart end label is the headline P&L: ' + endLabel);
    await page.selectOption('#sim-pos-filter', '');
    await page.waitForFunction(() => document.querySelectorAll('#sim-body .positions-table tbody tr').length > 1);
    const frozen = await page.$$eval('#sim-body .positions-table tbody tr[data-eid="9034"] td', (tds) => tds.map((t) => t.textContent.replace(/\s+/g, ' ').trim()));
    check(frozen[4] === 'unvalued(last 640.00)' && /−688/.test(frozen[5]), 'accounting-8: the frozen row is unvalued (its last value aside) and charged at −cost: ' + JSON.stringify(frozen));
    const basket = await text(page, '#sim-body .baskets-table tbody tr');
    check(/100 naked, no floor/.test(basket) && /100 naked shares beyond the sets: no floor/.test(basket), 'accounting-6: naked basket shares are named: ' + basket);
  });

  // accounting-4: the backtest's "P&L at liquidation" includes assumed fills; the figure the verdict states is beside it
  await run('round3-backtest-assumed', page, async () => {
    await simRoutes(page, { backtest: (now) => {
      const b = SF.backtest(now);
      const v = b.report.verdicts['human:conservative'];
      b.report.verdicts['human:conservative'] = Object.assign({}, v, { synthetic_pnl: 25, pnl_liq: 387.4 });
      b.report.sweep[0] = Object.assign({}, b.report.sweep[0], { synthetic_pnl: 10, verdict_pnl: 200 });
      return b;
    } });
    await openSim(page);
    await page.waitForSelector('#sim-body .bt-table');
    const cell = await page.$eval('#sim-body .bt-table tbody tr:first-child td', (e) => e.textContent.replace(/\s+/g, ' ').trim());
    check(/▲\+412 ?\+387 without assumed fills/.test(cell), 'the replay row names its P&L without assumed fills: ' + cell);
    const others = await page.$$eval('#sim-body .bt-table tbody tr .assumed-sub', (els) => els.length);
    check(others === 1, 'only rows with assumed fills carry the note: ' + others);
    const sw = await page.$eval('#sim-body .sweep-table tbody tr:first-child td', (e) => e.textContent.replace(/\s+/g, ' ').trim());
    check(/\+200 without assumed fills/.test(sw), 'the sweep row too: ' + sw);
  });

  // live-4: a venue never asked is "not asked yet" (neutral), an offline one that never answered says so
  await run('round3-providers', page, async () => {
    await simRoutes(page, { fairvalue: (now) => {
      const f = SF.fairvalue(now);
      f.providers.push({ name: 'manual', status: 'pending', last_ok_at: null, last_error: null, requests: 0, matched: null, quoted: null, next_try_at: null });
      return f;
    } });
    await openSim(page);
    await page.waitForSelector('#sim-body .provider-list li');
    const lines = await page.$$eval('#sim-body .provider-list li', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
    check(/^Polymarket: offline from this machine · 0 matched, 0 quoted · never answered · next try/.test(lines[0]) && /unreachable from this machine/.test(lines[0]), 'an offline venue: ' + lines[0]);
    check(!/last answer/.test(lines[0]) && /Kalshi: answering .* last answer/.test(lines[1]), 'only a venue that answered shows a last answer: ' + lines[1]);
    check(/^Your fair-value file: not asked yet/.test(lines[2]) && !/never answered/.test(lines[2]), 'a pending provider: ' + lines[2]);
    const cls = await page.$eval('#sim-body .provider-list li:nth-child(3)', (e) => e.className);
    check(/provider-pending/.test(cls), 'pending has its own class: ' + cls);
  });

  await run('strategy-new-kinds', page, async () => {
    await patch(page, '**/api/strategy', (j, now) => {
      j.opportunities = SF.strategyIdeas(now).concat(j.opportunities || []);
      j.sizing = SF.strategySizing();
      j.settlement_regime = 'unknown';
    });
    await open(page, '#strategy', { noWait: true });
    await page.waitForSelector('#strategy-body .idea');
    const kinds = await page.$$eval('#strategy-body .idea .idea-head .kind', (els) => els.slice(0, 3).map((e) => e.textContent.trim()));
    check(kinds.join(',') === 'Value,Basket,Hole', 'the new kinds have badges: ' + kinds.join(','));
    const facts = await page.evaluate(() => Array.from(document.querySelectorAll('#strategy-body .idea')).slice(0, 3).map((card) => {
      const out = {};
      for (const div of card.querySelectorAll('.facts > div')) out[div.querySelector('dt').textContent] = div.querySelector('dd').textContent.replace(/\s+/g, ' ').trim();
      return out;
    }));
    const [v, b, ho] = facts;
    check(v['Fair value'] === '0.580 (Polymarket and Kalshi, ± 1.0 cents)', 'fair value with its uncertainty: ' + v['Fair value']);
    check(v.Order === 'Limit 0.535 (keeps the full required edge)', 'taker order: ' + v.Order);
    check(v['If filled at the limit'] === '+0.035/share', 'edge at the limit: ' + v['If filled at the limit']);
    check(/^Sell when the YES bid reaches 0\.565/.test(v['Exit plan'] || ''), 'exit plan');
    check(v['National swing'] === 'A 3-point national swing toward the Republicans costs 0.17 per share', 'national swing: ' + v['National swing']);
    check(/Conservative sizing: 2,400 shares/.test(v.Sizing || '') && /^Chaser sizing \(chase\): 9,100 shares/.test(v['Chaser sizing'] || ''), 'sizing and the chaser alternative');
    check(v['Per day of capital'] === '0.21%', 'per day of capital: ' + v['Per day of capital']);
    check(b['Set type'] === 'Bounded: a refund on one leg could lose 0.55 per set' && /^Limit 0\.970 per set \(legs 0\.410 \/ 0\.560\)$/.test(b.Order || ''), 'basket set type and leg limits: ' + JSON.stringify(b));
    check(/^Resting limit at 0\.695 until /.test(ho.Order || '') && !('National swing' in b), 'maker order; sets carry no swing line');
    const card = await text(page, '#strategy-body .idea:nth-child(2)');
    check(/Buy 2 legs @ 0\.950 per full set/.test(card) && /800 sets/.test(card) && /BUY NO @ 0\.400/.test(card) && !/BUY NO YES/.test(card), 'a basket is a set of legs: ' + card.slice(0, 200));
    const sz = await text(page, '#strategy-body section[aria-labelledby="h-sizing"]');
    check(/^Sizing: Conservative: quarter-Kelly/.test(sz) && /221,500/.test(sz) && /Settlement rule assumed: unknown, valued conservatively/.test(sz) && /The other policy: Chaser/.test(sz), 'the sizing panel: ' + sz.slice(0, 200));
    await gotoView(page, 'overview');
    await page.waitForSelector('#look-ideas li a');
    const hrefs = await page.$$eval('#look-ideas li a', (as) => as.map((a) => a.getAttribute('href')));
    check(/^#strategy\//.test(hrefs[1]) && hrefs[0] === '#exchange/9026', 'a basket links to its card, a value idea to its outcome: ' + hrefs.slice(0, 3).join(' '));
  });

  /** The Strategy view on the fixture ideas only (value, basket, hole) with the fixture sizing block; fn(ideas) edits them. */
  async function strategyFixture(fn) {
    await patch(page, '**/api/strategy', (j, now) => {
      const ideas = SF.strategyIdeas(now);
      if (fn) fn(ideas);
      j.opportunities = ideas;
      j.sizing = SF.strategySizing();
      j.settlement_regime = 'unknown';
    });
    await open(page, '#strategy', { noWait: true });
    await page.waitForSelector('#strategy-body .idea');
  }

  function cardFacts(sel) {
    return page.$eval(sel, (card) => {
      const out = {};
      for (const div of card.querySelectorAll('.idea-main > .facts > div')) out[div.querySelector('dt').textContent] = div.querySelector('dd').textContent.replace(/\s+/g, ' ').trim();
      return out;
    });
  }

  // ui-2: a sized taker idea states its edge and return at the expected average fill of the suggested size (what its
  // cost is priced at, and how the simulator fills), with the best-price (touch) figures only beside them
  await run('ui-2', page, async () => {
    await strategyFixture((ideas) => {
      // the Texas value idea: touch 0.540, q 0.580 (edge +0.040, 7.4%); 2,400 shares walk the book to an average 0.550
      Object.assign(ideas[0], { edge: 0.04, expected_return: 0.0741, fill_price: 0.55 });
      ideas[2].fill_price = 0.695; // a resting hole order: its fill price is its limit, nothing to restate
    });
    const v = await cardFacts('#strategy-body .idea[data-key^="value:x9026"]');
    check(v['Edge per share'] === '+0.030 (+0.040 at the best price)', 'the edge is stated at the expected average fill: ' + v['Edge per share']);
    check(v['Expected return'] === '5.5% (7.4% at the best price)', 'the return is stated at the expected average fill: ' + v['Expected return']);
    check(v['Expected average fill'] === '0.550 per share for 2,400 shares (best price 0.540)', 'the expected average fill is shown: ' + v['Expected average fill']);
    const ho = await cardFacts('#strategy-body .idea[data-key^="hole:x9033"]');
    check(!('Expected average fill' in ho) && ho['Edge per share'] === '+0.004', 'a resting order keeps its own edge: ' + JSON.stringify(ho));
    const b = await cardFacts('#strategy-body .idea[data-key^="basket:"]');
    check(b['Edge per set'] === '+0.050' && !('Expected average fill' in b), 'without a fill price the edge is unchanged: ' + b['Edge per set']);
    await gotoView(page, 'overview');
    await page.waitForSelector('#look-ideas li a[href="#exchange/9026"]');
    const side = await page.$eval('#look-ideas li a[href="#exchange/9026"] .look-side', (e) => e.textContent.replace(/\s+/g, ' ').trim());
    check(/Edge \+0\.030\/share$/.test(side), 'the Overview shows the same edge as the card: ' + side);
  });

  // ui-5: a hole's prob_win is the chance its resting order fills (hole_fill_prob), not the chance the outcome wins
  await run('ui-5', page, async () => {
    await strategyFixture();
    const ho = await cardFacts('#strategy-body .idea[data-key^="hole:x9033"]');
    check(ho['Chance it fills'] === '2%' && !('Win probability' in ho), 'the hole card says "Chance it fills": ' + JSON.stringify(ho));
    const tip = await page.$eval('#strategy-body .idea[data-key^="hole:x9033"] .facts > div:first-child dd', (e) => e.title);
    check(/not the chance the outcome wins/.test(tip), 'the tooltip says what it is not: ' + tip);
    const v = await cardFacts('#strategy-body .idea[data-key^="value:x9026"]');
    check(v['Win probability'] === '58%', 'other kinds keep "Win probability": ' + v['Win probability']);
    await gotoView(page, 'overview');
    await page.waitForSelector('#look-ideas li a[href="#exchange/9033"]');
    const side = await page.$eval('#look-ideas li a[href="#exchange/9033"] .look-side', (e) => e.textContent.replace(/\s+/g, ' ').trim());
    check(/^Fill chance 2%/.test(side) && !/Win/.test(side), 'the Overview item says "Fill chance": ' + side);
  });

  // strategy-9: a longshot value idea is priced on its shrunk fair value: the card says so next to the outside value
  await run('strategy-9', page, async () => {
    await strategyFixture((ideas) => { Object.assign(ideas[0], { fair_value: 0.13, prob_win: 0.117, entry_price: 0.035 }); });
    const v = await cardFacts('#strategy-body .idea[data-key^="value:x9026"]');
    check(/^0\.130 \(.*\); valued at 0\.117 as a longshot$/.test(v['Fair value'] || ''), 'the shrunk value is shown: ' + v['Fair value']);
    await strategyFixture();
    const plain = await cardFacts('#strategy-body .idea[data-key^="value:x9026"]');
    check(!/longshot/.test(plain['Fair value'] || ''), 'a non-longshot keeps the plain fair value: ' + plain['Fair value']);
  });

  // ui-9: the Markov ceiling is a bound ("at most"), and the Markov sentence is shown for every policy (the conservative
  // policy's own lines leave it out), once
  await run('ui-9', page, async () => {
    await strategyFixture();
    const sel = '#strategy-body section[aria-labelledby="h-sizing"]';
    const r = await page.$eval(sel, (p) => {
      const tiles = {};
      for (const d of p.querySelectorAll(':scope > .facts > div')) tiles[d.querySelector('dt').textContent] = d.querySelector('dd').textContent.trim();
      const alt = p.querySelector('.alt-sizing');
      return {
        tiles, lines: Array.from(p.querySelectorAll(':scope > .sizing-lines li')).map((li) => li.textContent),
        altLines: Array.from(alt.querySelectorAll('.sizing-lines li')).map((li) => li.textContent),
        altTile: Array.from(alt.querySelectorAll('.facts > div')).map((d) => d.querySelector('dt').textContent + ': ' + d.querySelector('dd').textContent.trim()),
      };
    });
    check(r.tiles['Chance of reaching the bar'] === 'at most 45%' && !('Chance ceiling' in r.tiles), 'the tile is worded as a bound: ' + JSON.stringify(r.tiles));
    check(r.lines[0] === 'A strategy whose expected multiple is 1.00x reaches 2.2x with probability at most 45%; with no edge (fair prices) the bound is 1/M = 45%.',
      'the conservative panel states the Markov sentence: ' + r.lines[0]);
    check(r.altLines.filter((t) => /with probability at most/.test(t)).length === 1, 'the chaser\'s own Markov sentence is not repeated: ' + r.altLines.join(' | '));
    check(r.altTile.indexOf('Chance of reaching the bar: at most 46%') !== -1, 'the chaser tile too: ' + r.altTile.join(' | '));
    // the Simulation view's chaser panel uses the same tile
    await simRoutes(page);
    await openSim(page);
    await page.waitForSelector('#sim-body [data-panel="chaser"] .facts');
    const ch = await text(page, '#sim-body [data-panel="chaser"]');
    check(/Chance of reaching the bar\s*at most 46%/.test(ch) && (ch.match(/with probability at most/g) || []).length === 1, 'the Simulation chaser panel: ' + ch.slice(0, 300));
  });

  await page.context().close();

  // phone: the Simulation view never scrolls the page sideways; wide tables scroll in their own box
  const phone = await newPage(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  await run('sim-390', phone, async () => {
    await simRoutes(phone);
    await openSim(phone);
    await phone.waitForSelector('#sim-body .portfolios-table');
    await phone.waitForTimeout(300);
    const r = await phone.evaluate(() => ({
      page: document.documentElement.scrollWidth - innerWidth,
      table: (function () { const w = document.querySelector('#sim-body .portfolios-table').closest('.table-wrap'); return w.scrollWidth > w.clientWidth; })(),
      chart: document.querySelector('#sim-body .sim-chart svg').getBoundingClientRect().width <= innerWidth,
    }));
    check(r.page <= 1 && r.table && r.chart, 'no sideways page scroll at 390 px; tables scroll in their box: ' + JSON.stringify(r));
  });
  await phone.context().close();
}

// ------------------------------------------------------------------ outside moves (docs/OUTSIDE_MOVES.md §19.6)

/** Serve /api/moves from a fixture: body(now, url) -> a body, or {__status, body}. `status` (optional) is a function
 *  returning the alerts whose actionable ones /api/status lists under "moves" (the badge, sound and notifications). */
async function movesRoutes(page, opts) {
  opts = opts || {};
  const reply = (route, out) => {
    const res = out && out.__status ? out : { __status: 200, body: out };
    return route.fulfill({ status: res.__status, headers: JSON_HEADERS, body: JSON.stringify(res.body) });
  };
  if (opts.body !== false) {
    await page.route((u) => u.pathname === '/api/moves', (route) => {
      if (opts.gets) opts.gets.push(route.request().method() + ' ' + new URL(route.request().url()).search);
      return reply(route, (opts.body || ((now) => MF.body(now)))(Date.now() / 1000, route.request().url()));
    });
  }
  if (opts.status) {
    await patch(page, '**/api/status', (j, now) => {
      const alerts = opts.status(now);
      j.moves = alerts === null ? null : MF.statusMoves(now, alerts);
    });
  }
}

/** A page that records every console error and CSP violation (the moves checks fail on either). */
async function movesPage(browser, opts) {
  const page = await newPage(browser, opts);
  page.__console = [];
  page.on('console', (msg) => { if (msg.type() === 'error') page.__console.push(msg.text()); });
  await page.context().addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
  });
  return page;
}

async function cleanPage(page, where) {
  const r = await page.evaluate(() => ({
    csp: window.__csp || [],
    styled: document.querySelectorAll('#view-moves [style], #moves-body [style]').length,
    scripts: document.querySelectorAll('script').length,
  }));
  check(!r.csp.length, where + ': no CSP violations: ' + r.csp.join(' | '));
  check(r.styled === 0, where + ': no inline style attributes in the moves view (' + r.styled + ')');
  check(r.scripts === 1, where + ': no injected scripts');
  check(!page.__console.length, where + ': no console errors: ' + page.__console.join(' | '));
}

async function openMoves(page, sel) {
  await open(page, '#moves', { noWait: true });
  await page.waitForSelector(sel || '#moves-body .move-card', { timeout: 20000 });
}

async function setMovesFilter(page, f) {
  await page.click('#moves-body button[data-moves-filter="' + f + '"]');
  await page.waitForFunction((x) => document.querySelector('#moves-body button[data-moves-filter="' + x + '"]').getAttribute('aria-pressed') === 'true', f);
}

async function cardText(page, id) {
  return page.$eval('#moves-body .move-card[data-alert-id="' + id + '"]', (el) => el.textContent.replace(/\s+/g, ' ').trim());
}

/** The card text a sighted user reads: the screen-reader-only separators left out. */
async function visibleCardText(page, id) {
  return page.$eval('#moves-body .move-card[data-alert-id="' + id + '"]', (el) => {
    const c = el.cloneNode(true);
    for (const s of c.querySelectorAll('.sr-only')) s.remove();
    return c.textContent.replace(/\s+/g, ' ').trim();
  });
}

/** An AudioContext stub that records every oscillator start (window.__starts) and every construction (window.__ctx). */
function audioStub() {
  window.__starts = [];
  window.__ctx = 0;
  window.AudioContext = class {
    constructor() { window.__ctx += 1; this.state = 'running'; this.currentTime = 1; this.destination = {}; }
    resume() { this.state = 'running'; return Promise.resolve(); }
    createOscillator() {
      return { type: '', frequency: { value: 0 }, connect() {}, start(t) { window.__starts.push({ t, f: this.frequency.value, type: this.type }); }, stop() {} };
    }
    createGain() { return { gain: { value: 0 }, connect() {} }; }
  };
}

/** A Notification stub: window.__perm is the permission, window.__answer what requestPermission() answers. */
function notificationStub(perm) {
  window.__notes = [];
  window.__asked = 0;
  window.__perm = perm;
  window.Notification = class {
    constructor(title, options) {
      this.title = title;
      this.options = options || {};
      this.onclick = null;
      window.__notes.push({ title, body: this.options.body, tag: this.options.tag });
      window.__lastNote = this;
    }
    static get permission() { return window.__perm; }
    static requestPermission(cb) {
      window.__asked += 1;
      window.__perm = window.__answer || 'granted';
      if (cb) cb(window.__perm);
      return Promise.resolve(window.__perm);
    }
    close() {}
  };
}

/** run() for the moves checks: each starts with an empty console log (cleanPage fails on any error). */
async function mrun(name, page, fn) {
  return run(name, page, async (p) => {
    p.__console.length = 0;
    await fn(p);
  });
}

async function movesChecks(browser) {
  const page = await movesPage(browser);
  const now0 = () => Date.now() / 1000;

  await mrun('moves-statuses', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    await setMovesFilter(page, 'all');
    const fx = MF.allAlerts(now0());
    const cards = await page.$$eval('#moves-body .move-card', (els) => els.map((el) => ({
      id: el.dataset.alertId,
      badge: el.querySelector('.move-status').textContent.trim(),
      icon: !!el.querySelector('.move-status svg.icon'),
      closed: !!el.querySelector('.status-closed'),
      race: el.querySelector('.move-race').textContent.trim(),
      href: el.querySelector('.move-title a') ? el.querySelector('.move-title a').getAttribute('href') : null,
    })));
    check(cards.map((c) => c.id).join(',') === fx.map((a) => a.alert_id).join(','), 'every alert is shown, in the server order: ' + cards.map((c) => c.id).join(','));
    fx.forEach((a, i) => {
      const c = cards[i];
      check(c.badge === MF.STATUS_LABELS[a.status] && c.icon, a.alert_id + ': the badge has an icon and the status text (' + c.badge + ')');
      check(c.closed === (a.state === 'closed'), a.alert_id + ': a closed alert says "closed"');
      check(c.race === a.race_label, a.alert_id + ': the race label leads the card (' + c.race + ')');
      check(c.href === '#exchange/' + a.exchange_id, a.alert_id + ': the outcome links to the drawer (' + c.href + ')');
    });
    const icons = await page.$$eval('#moves-body .move-card .move-status svg', (els) => new Set(els.map((s) => s.innerHTML)).size);
    check(icons === 4, 'the four statuses have four different icons (' + icons + ')');
    const L = fx[0];
    const t = await visibleCardText(page, L.alert_id);
    check(/Detected 2 min ago · moved in 5 min/.test(t), 'age and window: ' + t.slice(0, 200));
    check(/Outside 0\.517 → 0\.573 \(\+5\.5 pts\) · Polymarket 0\.520 → 0\.580 · Kalshi 0\.515 → 0\.565/.test(t), 'the outside line per venue: ' + t.slice(0, 300));
    check(/Cup now 0\.512 \(bid 0\.505 \/ ask 0\.520\), moved 0\.0 pts since the outside move began · lag gap 5\.5 pts/.test(t), 'the Cup line and the lag gap: ' + t);
    // (integration) the gap between the prices now, when it differs from the peak-based lag gap by half a point
    check(/lag gap 5\.5 pts · the outside price is now 6\.0 pts above the Cup/.test(t), 'the current level gap is said next to the lag gap: ' + t.slice(0, 400));
    check(!/the outside price is now/.test(await visibleCardText(page, fx[1].alert_id)), 'no level note when it equals the lag gap');
    check(t.indexOf(L.reason) !== -1, 'the reason sentence is verbatim');
    check(/Waiting for the Cup: 2 of 60 min\./.test(t), 'a pending lag counts the minutes: ' + t.slice(-200));
    const followed = await visibleCardText(page, fx[6].alert_id);
    check(/The Cup followed 4\.3 min after the outside move \(1\.8 min after this alert\)\./.test(followed), 'a followed lag line: ' + followed);
    check(/Measured: 4 min after the alert: \+0\.00[78] a share left; bought then and sold 30 min later: \+0\.005 a share/.test(followed), 'captures when known: ' + followed);
    const nf = await visibleCardText(page, fx[7].alert_id);
    check(/The Cup did not follow within 60 min; the gap is 6\.0 pts now\./.test(nf) && /Never followed within the hour/.test(nf) && /−0\.010 a share/.test(nf), 'not followed: ' + nf);
    const cens = await visibleCardText(page, fx[8].alert_id);
    check(/Not counted in the lag study: the bot was not running for 25 min while this was measured\./.test(cens), 'censored: ' + cens);
    const first = await visibleCardText(page, fx[5].alert_id);
    check(/the outside followed the Cup, not a lag/.test(first) && /The Cup moved first, about 11\.2 min earlier\./.test(first), 'Cup moved first: ' + first);
    // (integration) a move the Cup made first has no "lag gap" to show (it is not a lag)
    check(/no lag gap: the Cup moved first/.test(first) && !/lag gap \d/.test(first), 'Cup moved first shows no lag gap: ' + first.slice(0, 400));
    const rev = await visibleCardText(page, fx[4].alert_id);
    check(/came back 3\.5 of its 5\.5 pts/.test(rev) && /Never followed: the outside price came back first\./.test(rev) && /−0\.005 a share left/.test(rev), 'reverted: ' + rev);
    check(!/sure thing/i.test((await text(page, '#moves-body')).replace(/not a sure thing/gi, '')), 'nothing calls an alert a sure thing');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.body(now, { alerts: [MF.alertAlready(now, { lag_gap_now: -0.01 })] }) });
    await openMoves(page);
    check(/lag gap closed \(the Cup moved 1\.0 pts further than the outside price\)/.test(await visibleCardText(page, MF.alertAlready(0).alert_id)), 'an overshooting Cup: the gap is closed, not negative');
    // (integration) a converging move (the outside price moved to where the Cup already was) is not a lag either
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => {
      const a = MF.alertAlready(now, { lag_gap: 0.054, lag_gap_now: 0.054, level_gap_now: 0.0 });
      a.lag = Object.assign({}, a.lag, { eligible: false, outcome: 'excluded', excluded_reason: 'converging: the outside moved to the Cup\'s price' });
      return MF.body(now, { alerts: [a] });
    } });
    await openMoves(page);
    const conv = await visibleCardText(page, MF.alertAlready(0).alert_id);
    check(/no lag gap: the outside price moved to the Cup’s price/.test(conv) && !/lag gap 5\.4/.test(conv) && /the outside price is level with the Cup now/.test(conv), 'a converging move shows no lag gap: ' + conv.slice(0, 400));
    await cleanPage(page, 'moves-statuses');
  });

  await mrun('moves-trade-box', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    const fx = MF.allAlerts(now0());
    const box = await page.$eval('#moves-body .move-card[data-alert-id="' + fx[0].alert_id + '"] .trade-box', (el) => el.textContent.replace(/\s+/g, ' ').trim());
    check(/^Suggested hand trade: Buy YES at 0\.520 — 300 shares at that price \(book read (just now|\d+ s ago)\); up to 0\.525 still keeps 1 cent a share: 750 shares\./.test(box), 'limit, shares at the limit and up to the max limit: ' + box);
    check(/\+0\.037 per share net of the spread \(\+0\.018 after the outside price’s ±2\.0-cent uncertainty\) if the Cup catches up to 0\.565; \+0\.045 at resolution if the outside price is right\./.test(box), 'edges net of spread, after uncertainty and at resolution: ' + box);
    check(box.indexOf(MF.TRADE_NOTE) !== -1, 'the "suggestion only" note is verbatim');
    const no = await page.$eval('#moves-body .move-card[data-alert-id="' + fx[1].alert_id + '"] .trade-box', (el) => el.textContent.replace(/\s+/g, ' ').trim());
    check(/^Suggested hand trade: Buy NO at 0\.430 — shares at that price unknown \(no recent Cup order book\); up to 0\.450 still keeps 1 cent a share\./.test(no), 'without a book the depth is unknown: ' + no);
    check(/if the Cup’s NO price catches up to 0\.475 \(YES 0\.525\)/.test(no) && no.indexOf(MF.TRADE_NOTE) !== -1, 'a NO trade names the NO price: ' + no);
    const nt = await cardText(page, fx[2].alert_id);
    check(nt.indexOf('No trade suggested: ' + fx[2].trade_note) !== -1 && !/Suggested hand trade/.test(nt), 'a lagging card without a trade shows its trade_note: ' + nt);
    check(/one venue only/.test(nt), 'a one-venue move says so');
    const am = await cardText(page, fx[3].alert_id);
    check(!/Suggested hand trade|No trade suggested/.test(am), 'an already-moved card has no trade box');
    await setMovesFilter(page, 'all');
    const fl = await cardText(page, fx[6].alert_id);
    check(/When it opened, the suggestion was: Buy YES at 0\.520 \(\+0\.037 per share net of the spread\)\. It was a suggestion only\./.test(fl) && !/Suggested hand trade/.test(fl),
      'a closed card keeps what it suggested when it opened, not a live trade: ' + fl);
    check(!(await page.$('#moves-body .trade-stale')), 'a fresh suggestion carries no staleness warning');
    const actionable = await page.$$eval('#moves-body .move-card.is-actionable', (els) => els.map((e) => e.dataset.alertId));
    check(actionable.join(',') === [fx[0].alert_id, fx[1].alert_id].join(','), 'only the actionable alerts are marked: ' + actionable.join(','));
    await cleanPage(page, 'moves-trade-box');
  });

  await mrun('moves-trade-stale', page, async () => {
    // the watcher stopped re-pricing (a stall): the suggestion says how old it is
    await movesRoutes(page, { body: (now) => MF.body(now, { alerts: [MF.alertL(now, { trade: Object.assign(MF.alertL(now).trade, { computed_at: now - 300, book_age_s: 2 }) })] }) });
    await openMoves(page);
    const st = await text(page, '#moves-body .trade-box .trade-stale');
    check(/^Worked out 5 min ago: the Cup and the outside price may have moved since\. Check the Cup’s order book before acting\.$/.test(st), 'a stale suggestion says so: ' + st);
    await cleanPage(page, 'moves-trade-stale');
  });

  await mrun('moves-filters', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    const fx = MF.allAlerts(now0());
    const ids = () => page.$$eval('#moves-body .move-card', (els) => els.map((e) => e.dataset.alertId).join(','));
    const pressed = () => page.$$eval('#moves-body button[data-moves-filter]', (bs) => bs.map((b) => b.dataset.movesFilter + '=' + b.getAttribute('aria-pressed')).join(' '));
    check((await pressed()) === 'open=true lagging=false all=false', 'Open is the default filter: ' + (await pressed()));
    check((await ids()) === fx.filter((a) => a.state === 'open').map((a) => a.alert_id).join(','), 'Open shows the open alerts: ' + (await ids()));
    check((await text(page, '#moves-count')) === '5 open, 3 lagging', 'the count: ' + (await text(page, '#moves-count')));
    await setMovesFilter(page, 'lagging');
    check((await ids()) === [fx[0], fx[1], fx[2]].map((a) => a.alert_id).join(','), 'Lagging only: ' + (await ids()));
    check((await pressed()) === 'open=false lagging=true all=false', 'aria-pressed follows the filter');
    await setMovesFilter(page, 'all');
    check((await ids()) === fx.map((a) => a.alert_id).join(','), 'All shows every alert: ' + (await ids()));
    await waitStatusPolls(page, 1);
    await page.waitForTimeout(400);
    check((await pressed()) === 'open=false lagging=false all=true', 'the filter survives a poll');
    // keyboard: the filter buttons are reachable and keep focus across a poll
    await page.focus('#moves-body button[data-moves-filter="open"]');
    await page.keyboard.press('Enter');
    await waitStatusPolls(page, 1);
    await page.waitForTimeout(400);
    check(await page.evaluate(() => document.activeElement && document.activeElement.dataset.movesFilter === 'open'), 'focus stays on the filter after a poll');
    // only the closed alerts left: the Open filter says where they are
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.body(now, { alerts: MF.allAlerts(now).filter((a) => a.state === 'closed') }) });
    await openMoves(page, '#moves-body .move-empty:not([hidden])');
    check((await text(page, '#moves-body .move-empty')) === 'No open alerts right now. 4 closed alerts are under All.', 'empty Open filter: ' + (await text(page, '#moves-body .move-empty')));
    await cleanPage(page, 'moves-filters');
  });

  await mrun('moves-empty-disabled', page, async () => {
    await movesRoutes(page, { body: (now) => MF.empty(now) });
    await openMoves(page, '#moves-body .move-empty:not([hidden])');
    check((await text(page, '#moves-body .move-empty')) === 'No outside move yet. Alerts appear here when Polymarket or Kalshi moves by at least 3 points in a minute (more over longer windows, and more for outcomes that are usually volatile).',
      'the empty state: ' + (await text(page, '#moves-body .move-empty')));
    check((await text(page, '#moves-body .lag-sentence')) === MF.NO_DATA_SENTENCE, 'the lag study says there is no data');
    check(/Nothing was filtered out today/.test(await text(page, '#moves-body [data-panel="filtered"]')), 'nothing filtered');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.disabled(now) });
    await openMoves(page, '#moves-body .moves-note .empty');
    check((await text(page, '#moves-body')) === MF.OFF_ERROR, 'disabled: the error sentence only (' + (await text(page, '#moves-body')) + ')');
    check(!(await page.$('#moves-body [data-panel]')), 'disabled: no panels');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => Object.assign(MF.body(now, { alerts: [], suppressed: [], summary: 'empty', venuesList: [] }), { available: false, steps: 0, last_step_at: null }) });
    await openMoves(page, '#moves-body .move-empty:not([hidden])');
    check((await text(page, '#moves-body .move-empty')) === 'Waiting for the first outside check (every 15 s).', 'not stepped yet: ' + (await text(page, '#moves-body .move-empty')));
    check(/No outside venue has been polled yet\./.test(await text(page, '#moves-body [data-panel="venues"]')) && /no check yet/.test(await text(page, '#moves-body [data-panel="venues"]')), 'no venue yet');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => Object.assign(MF.body(now, { alerts: [] }), { available: false, error: 'The outside watcher crashed: KeyError.' }) });
    await openMoves(page, '#moves-body .moves-note .inline-error');
    check(/The outside watcher is not running: The outside watcher crashed: KeyError\./.test(await text(page, '#moves-body .moves-note')), 'a watcher error is shown inline');
    await cleanPage(page, 'moves-empty-disabled');
  });

  await mrun('moves-venues', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    const lines = () => page.$$eval('#moves-body .venue-list li', (els) => els.map((e) => [e.dataset.venue, e.className, !!e.querySelector('svg'), e.textContent.replace(/\s+/g, ' ').trim()]));
    let l = await lines();
    check(l.length === 2 && l.every((x) => x[2]), 'one line per venue, each with an icon');
    check(/^Polymarket: answering · 231 matched · last read \d+ s ago · 25 of 45 reads a minute$/.test(l[0][3]), 'an answering venue: ' + l[0][3]);
    check(/^Kalshi: offline from this machine — Kalshi is unreachable from this machine \(could not connect\)/.test(l[1][3]) && /venue-offline/.test(l[1][1]), 'an offline venue: ' + l[1][3]);
    const facts = await text(page, '#moves-body .venue-facts');
    check(/Watching 237 outcomes \(231 matched to an outside market\) · last check \d+ s ago/.test(facts) || /last check just now/.test(facts), 'watching counts: ' + facts);
    check(/Cup order books: 1 of 4 reads a minute/.test(facts), 'the Cup book reads: ' + facts);
    for (const kind of ['backoff', 'busy', 'pending']) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await movesRoutes(page, { body: (now) => MF.body(now, { venues: kind }) });
      await openMoves(page);
      l = await lines();
      if (kind === 'backoff') check(/^Kalshi: waiting \(HTTP 429\) until \d/.test(l[1][3]) && /429/.test(l[1][3]), 'a rate-limited venue: ' + l[1][3]);
      if (kind === 'busy') check(l[0][3] === 'Polymarket: reading (the fair-value refresh is using it)', 'a busy venue: ' + l[0][3]);
      if (kind === 'pending') check(l.every((x) => /: waiting for the first fair-value refresh to validate matches$/.test(x[3])), 'pending venues: ' + l.map((x) => x[3]).join(' | '));
    }
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.body(now, { book_reads: { used: 0, limit: 0, room: 0 }, last_step_at: now - 600 }) });
    await openMoves(page);
    const v = await text(page, '#moves-body [data-panel="venues"]');
    check(/Cup order books: off/.test(v), 'book reads off: ' + v);
    check(/The outside watcher has not run since .+ \(10 min ago\): these alerts may be out of date\./.test(v), 'a stalled watcher is flagged: ' + v);
    await cleanPage(page, 'moves-venues');
  });

  await mrun('moves-venues-fresh', page, async () => {
    // every answer has the venue read 6 s and the step 3 s before it: the shown ages follow the newest answer
    await movesRoutes(page);
    await openMoves(page);
    await waitStatusPolls(page, 3);
    await page.waitForTimeout(500);
    const ages = await page.evaluate(() => Array.from(document.querySelectorAll('#moves-body .venue-list time, #moves-body .venue-facts time'))
      .map((t) => t.textContent));
    const secs = ages.map((a) => (a === 'just now' ? 0 : /^(\d+) s ago$/.test(a) ? Number(a.match(/^(\d+)/)[1]) : 999));
    check(ages.length === 2 && secs.every((x) => x <= 13), 'the last-read and last-check ages are fresh after three polls: ' + ages.join(', '));
    await cleanPage(page, 'moves-venues-fresh');
  });

  await mrun('moves-lag-table', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    const rows = () => page.$$eval('#moves-body .lag-table tbody tr', (trs) => trs.map((tr) => [tr.querySelector('th').textContent.trim(), tr.querySelector('td').textContent.replace(/\s+/g, ' ').trim()]));
    check((await text(page, '#moves-body .lag-sentence')) === MF.SMALL_SENTENCE, 'the small-sample sentence is verbatim');
    check(/Small sample/.test(await text(page, '#moves-body [data-panel="summary"] .lag-badges')), 'a small-sample badge');
    let r = await rows();
    const labels = r.map((x) => x[0]);
    check(labels.join(' | ') === ['Followed within 1 min', 'Followed within 5 min', 'Followed within 15 min', 'Followed within 60 min',
      'Never followed (came back / did not follow)', 'Cup moved first', 'Median lag (after the outside move / after the alert)',
      'Left 4 min after the alert (per share, net of spread)', 'Bought then, sold 30 min later (per share)'].join(' | '), 'the table rows: ' + labels.join(' | '));
    check(r[1][1] === '25%(n = 1 of 4)', 'followed within 5 min: ' + r[1][1]);
    check(/^50%\(n = 2 of 4\): came back 25% \(n = 1\) \/ did not follow 25% \(n = 1\)$/.test(r[4][1]), 'never followed: ' + r[4][1]);
    check(r[5][1] === '20%(n = 1 of 5)', 'Cup moved first: ' + r[5][1]);
    check(/^4\.3 min \/ 1\.8 min/.test(r[6][1]) && /\(n = 2 followed\)/.test(r[6][1]), 'medians: ' + r[6][1]);
    check(/^\+0\.018 on average/.test(r[7][1]) && /no interval yet/.test(r[7][1]) && /\(n = 3\)/.test(r[7][1]), 'capture: ' + r[7][1]);
    check(r[8][1] === 'n/a(n = 0)', 'a null exit reads n/a with its n: ' + r[8][1]);
    check(/Excluded: 1 converging \(or without a Cup price\), 1 censored/.test(await text(page, '#moves-body .lag-excluded')), 'the excluded counts');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.body(now, { summary: 'large' }) });
    await openMoves(page);
    await page.waitForFunction((s) => document.querySelector('#moves-body .lag-sentence').textContent === s, MF.LARGE_SENTENCE);
    check(!(await page.$('#moves-body [data-panel="summary"] .small-sample')), 'no small-sample badge on a large sample');
    r = await rows();
    check(r[1][1] === '41%(n = 19 of 46)' && r[3][1] === '72%(n = 33 of 46)', 'large-sample shares: ' + r[1][1] + ' / ' + r[3][1]);
    check(/^\+0\.004 on average/.test(r[7][1]) && /90% interval −0\.001 to \+0\.009 \(over per-race means\)/.test(r[7][1]) && /\(n = 40\)/.test(r[7][1]), 'capture with its interval: ' + r[7][1]);
    check(/^−0\.002 on average/.test(r[8][1]) && /\(n = 37\)/.test(r[8][1]), 'a negative exit keeps its sign: ' + r[8][1]);
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { body: (now) => MF.body(now, { summary: 'empty' }) });
    await openMoves(page);
    await page.waitForFunction((s) => document.querySelector('#moves-body .lag-sentence').textContent === s, MF.NO_DATA_SENTENCE);
    r = await rows();
    check(r.slice(0, 6).every((x) => /^n\/a\(n = 0/.test(x[1])), 'null shares read n/a: ' + r.map((x) => x[1]).join(' | '));
    await cleanPage(page, 'moves-lag-table');
  });

  await mrun('moves-filtered', page, async () => {
    await movesRoutes(page);
    await openMoves(page);
    const det = '#moves-body #moves-filtered-details';
    check(!(await page.$eval(det, (d) => d.open)), 'the Filtered out details start closed');
    check((await text(page, det + ' > summary')) === '3 moves were filtered out today', 'its summary counts: ' + (await text(page, det + ' > summary')));
    await page.click(det + ' > summary');
    const counts = await page.$$eval(det + ' .filtered-counts li', (els) => els.map((e) => e.textContent.trim()));
    check(counts.join(' | ') === '1 thin or wide outside book | 1 a single print that did not hold | 1 the other venue did not move', 'per-reason counts in plain words: ' + counts.join(' | '));
    const items = await page.$$eval(det + ' .suppressed-list li', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
    check(items.length === 3 && /^Michigan Governor \(D\) — the other venue did not move: Polymarket \+6\.0 pts, Kalshi \+1\.0 pt: the venues disagree\. \((\d+ (s|min) ago|just now)\)$/.test(items[0]), 'the latest items: ' + items[0]);
    await waitStatusPolls(page, 1);
    await page.waitForTimeout(400);
    check(await page.$eval(det, (d) => d.open), 'it stays open across a poll');
    await cleanPage(page, 'moves-filtered');
  });

  await mrun('moves-badge', page, async () => {
    let alerts = [];
    await movesRoutes(page, { status: (now) => alerts.map((f) => f(now)) });
    alerts = [MF.alertL, MF.alertNoBook];
    await open(page, '#overview', { noWait: true });
    await page.waitForFunction(() => document.getElementById('nav-count-moves').textContent === '2');
    check((await page.getAttribute('#nav-count-moves', 'aria-hidden')) === 'true', 'the badge number is hidden from screen readers');
    check((await text(page, '#nav-moves-sr')) === ', 2 open lagging alerts', 'its sentence: ' + (await text(page, '#nav-moves-sr')));
    const name = await page.$eval('.app-nav a[data-view="moves"]', (a) => a.textContent.replace(/\s+/g, ' ').trim());
    check(/^Outside moves 2, 2 open lagging alerts$/.test(name), 'the nav link: ' + name);
    const order = await page.$$eval('.app-nav a[data-view]', (as) => as.map((a) => a.dataset.view).join(','));
    check(order === 'overview,markets,surges,moves,high,strategy,sim', 'Outside moves comes after Surges: ' + order);
    alerts = [MF.alertL];
    await page.waitForFunction(() => document.getElementById('nav-count-moves').textContent === '1');
    check((await text(page, '#nav-moves-sr')) === ', 1 open lagging alert', 'singular');
    alerts = [];
    await page.waitForFunction(() => document.getElementById('nav-count-moves').textContent === '');
    check(!(await page.isVisible('#nav-count-moves')) && (await text(page, '#nav-moves-sr')) === '', 'no badge without open lagging alerts');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await movesRoutes(page, { status: () => null });
    await open(page, '#overview', { noWait: true });
    await waitStatusPolls(page, 1);
    check((await text(page, '#nav-count-moves')) === '', 'status.moves null: no badge');
    await cleanPage(page, 'moves-badge');
  });

  await mrun('moves-inline-error', page, async () => {
    let fail = true;
    await movesRoutes(page, { body: (now) => (fail ? { __status: 500, body: { error: "Internal error; see the dashboard's log." } } : MF.body(now)) });
    await open(page, '#moves', { noWait: true });
    await page.waitForSelector('#view-errors-moves:not([hidden]) .inline-error');
    check(/Could not load the outside moves: Internal error; see the dashboard's log\. Retrying every 5 s\./.test(await text(page, '#view-errors-moves')), 'an inline error: ' + (await text(page, '#view-errors-moves')));
    check((await text(page, '#live-label')) !== 'Offline' && (await page.isHidden('#offline-banner')), 'the page is not Offline');
    check(/Could not load the outside moves\. Retrying every 5 s\./.test(await text(page, '#moves-body')), 'the view says it could not load');
    fail = false;
    await page.waitForSelector('#moves-body .move-card', { timeout: 15000 });
    await page.waitForSelector('#view-errors-moves', { state: 'hidden' });
    fail = true;
    await page.waitForSelector('#view-errors-moves:not([hidden]) .inline-error', { timeout: 15000 });
    check(/Could not refresh the outside moves/.test(await text(page, '#view-errors-moves')) && (await page.$$('#moves-body .move-card')).length > 0, 'a refresh error keeps the cards');
    page.__console.length = 0; // the 500 answers are logged by the browser on purpose
  });

  await mrun('moves-annotations', page, async () => {
    await patch(page, '**/api/strategy', (j, now) => { j.opportunities = [MF.annotatedIdea(now)].concat(j.opportunities || []); });
    await open(page, '#strategy', { noWait: true });
    await page.waitForSelector('#strategy-body .idea .move-fact-link');
    const fact = await page.$eval('#strategy-body .idea .move-fact-link', (a) => [a.getAttribute('href'), a.textContent.replace(/\s+/g, ' ').trim(),
      a.closest('div').querySelector('dt').textContent]);
    check(fact[0] === '#moves' && fact[1] === 'Cup lagging, +5.5 pts (2 min ago)' && fact[2] === 'Outside move', 'the idea fact links to #moves: ' + fact.join(' / '));
    await page.click('#strategy-body .idea .move-fact-link');
    await page.waitForFunction(() => location.hash === '#moves' && !document.getElementById('view-moves').hidden);
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await simRoutes(page, { fairvalue: (now) => {
      const f = SF.fairvalue(now);
      f.rows[0].move = { alert_id: 'mv-x', status: 'lagging', status_label: 'Cup lagging', state: 'open', direction: 1, move: 0.06, lag_gap_now: 0.06, detected_at: now - 60, actionable: true };
      for (const r of f.rows.slice(1)) r.move = null;
      return f;
    } });
    await openSim(page);
    await page.waitForSelector('#sim-body .fv-table .flag-move');
    const badges = await page.$$eval('#sim-body .fv-table .flag-move', (els) => els.map((e) => [e.textContent.trim(), e.getAttribute('href'), !!e.querySelector('svg')]));
    check(badges.length === 1 && badges[0][0] === 'moved: Cup lagging' && badges[0][1] === '#moves' && badges[0][2], 'one "moved" badge with the status label: ' + JSON.stringify(badges));
    await cleanPage(page, 'moves-annotations');
  });

  await page.context().close();

  // ---- sound (an AudioContext stub records every oscillator start)
  const sp = await movesPage(browser);
  await sp.context().addInitScript(audioStub);
  await mrun('moves-sound', sp, async () => {
    let alerts = [MF.alertL];
    await movesRoutes(sp, { status: (now) => alerts.map((f) => f(now)) });
    await openMoves(sp);
    const starts = () => sp.evaluate(() => window.__starts.length);
    check((await sp.getAttribute('#moves-sound', 'aria-pressed')) === 'false' && /Sound off/.test(await text(sp, '#moves-sound')), 'Sound is off by default');
    check((await sp.evaluate(() => window.__ctx)) === 0, 'no AudioContext before the click');
    await sp.click('#moves-sound');
    check((await sp.getAttribute('#moves-sound', 'aria-pressed')) === 'true' && /Sound on/.test(await text(sp, '#moves-sound')), 'the toggle turns on');
    check((await sp.evaluate(() => window.__ctx)) === 1, 'one AudioContext, created in the click');
    check((await sp.evaluate(() => localStorage.getItem('supermarket-dashboard:moves-sound'))) === 'on', 'the choice is saved');
    await waitStatusPolls(sp, 2);
    check((await starts()) === 0, 'an alert open before the page loaded never beeps (' + (await starts()) + ')');
    alerts = [MF.alertNoBook, MF.alertL];
    await waitStatusPolls(sp, 2);
    const s = await sp.evaluate(() => window.__starts);
    check(s.length === 2 && s.every((x) => x.f === 880 && x.type === 'sine') && Math.abs(s[1].t - s[0].t - 0.2) < 1e-6, 'one new id: two 880 Hz beeps 0.2 s apart: ' + JSON.stringify(s));
    await waitStatusPolls(sp, 1);
    check((await starts()) === 2, 'the same id does not beep again');
    await sp.click('#moves-sound');
    check((await sp.getAttribute('#moves-sound', 'aria-pressed')) === 'false', 'the toggle turns off');
    alerts = [(now) => MF.alertL(now, { alert_id: 'mv-third-1', race_label: 'Third (D)' }), MF.alertNoBook, MF.alertL];
    await waitStatusPolls(sp, 2);
    check((await starts()) === 2, 'no beep while Sound is off (' + (await starts()) + ')');
    // a saved "on": nothing plays before a gesture, and the first poll still only records
    alerts = [MF.alertL];
    await openMoves(sp);
    check((await sp.getAttribute('#moves-sound', 'aria-pressed')) === 'false', 'still off after a reload (it was turned off)');
    await sp.evaluate(() => localStorage.setItem('supermarket-dashboard:moves-sound', 'on'));
    await openMoves(sp);
    check((await sp.getAttribute('#moves-sound', 'aria-pressed')) === 'true', 'a saved "on" is restored');
    check((await sp.evaluate(() => window.__ctx)) === 0 && /starts after your first click or key press/.test(await text(sp, '#moves-alert-note')), 'it waits for a gesture: ' + (await text(sp, '#moves-alert-note')));
    await sp.click('#h-moves');
    check((await sp.evaluate(() => window.__ctx)) === 1, 'a click anywhere creates the AudioContext');
    alerts = [MF.alertNoBook, MF.alertL];
    await waitStatusPolls(sp, 2);
    check((await starts()) === 2, 'then a new id beeps (' + (await starts()) + ')');
    await cleanPage(sp, 'moves-sound');
  });
  await sp.context().close();

  const silent = await movesPage(browser);
  await silent.context().addInitScript(() => { delete window.AudioContext; delete window.webkitAudioContext; });
  await mrun('moves-sound-unsupported', silent, async () => {
    await movesRoutes(silent);
    await openMoves(silent);
    check(await silent.$eval('#moves-sound', (b) => b.disabled), 'the Sound toggle is disabled');
    check(/Sound is not supported in this browser\./.test(await text(silent, '#moves-alert-note')), 'and says why');
    await cleanPage(silent, 'moves-sound-unsupported');
  });
  await silent.context().close();

  // ---- desktop notifications (a Notification stub records every notification)
  const np = await movesPage(browser);
  await np.context().addInitScript(notificationStub, 'default');
  await mrun('moves-notifications', np, async () => {
    let alerts = [MF.alertL];
    await movesRoutes(np, { status: (now) => alerts.map((f) => f(now)) });
    await openMoves(np);
    check((await text(np, '#moves-notify')) === 'Turn on desktop notifications' && (await np.getAttribute('#moves-notify', 'aria-pressed')) === null, 'permission default: ' + (await text(np, '#moves-notify')));
    await np.click('#moves-notify');
    await np.waitForFunction(() => document.getElementById('moves-notify').getAttribute('aria-pressed') === 'true');
    check((await np.evaluate(() => window.__asked)) === 1, 'the click asks for permission once');
    check((await text(np, '#moves-notify')) === 'Desktop notifications on', 'granted: the toggle is on: ' + (await text(np, '#moves-notify')));
    check((await np.evaluate(() => localStorage.getItem('supermarket-dashboard:moves-notify'))) === 'on', 'saved');
    await waitStatusPolls(np, 1);
    check((await np.evaluate(() => window.__notes.length)) === 0, 'no notification for an alert open before the page loaded');
    alerts = [MF.alertNoBook, MF.alertL];
    await np.waitForFunction(() => window.__notes.length === 1, null, { timeout: 15000 });
    const n = await np.evaluate(() => window.__notes[0]);
    const want = MF.notificationText(MF.alertNoBook(Date.now() / 1000));
    check(n.title === 'Cup lagging: Ohio Senate (R)' && n.title === want.headline, 'the headline: ' + n.title);
    check(n.body === want.body && /Suggestion: Buy NO at 0\.430\. Not a sure thing\.$/.test(n.body), 'the body: ' + n.body);
    check(n.tag === MF.alertNoBook(Date.now() / 1000).alert_id, 'tagged with the alert id: ' + n.tag);
    await np.waitForFunction(() => document.getElementById('moves-announcer').textContent === 'Cup lagging: Ohio Senate (R).');
    await waitStatusPolls(np, 2);
    check((await np.evaluate(() => window.__notes.length)) === 1, 'one notification per new id');
    await np.evaluate(() => { location.hash = '#overview'; });
    await np.waitForFunction(() => !document.getElementById('view-overview').hidden);
    await np.evaluate(() => window.__lastNote.onclick());
    await np.waitForFunction(() => location.hash === '#moves' && !document.getElementById('view-moves').hidden);
    await np.click('#moves-notify');
    check((await text(np, '#moves-notify')) === 'Desktop notifications off' && (await np.getAttribute('#moves-notify', 'aria-pressed')) === 'false', 'the toggle turns off');
    alerts = [(now) => MF.alertL(now, { alert_id: 'mv-third-2', race_label: 'Third (D)' }), MF.alertNoBook, MF.alertL];
    await waitStatusPolls(np, 2);
    check((await np.evaluate(() => window.__notes.length)) === 1, 'none while off');
    await cleanPage(np, 'moves-notifications');
  });
  await np.context().close();

  // notifications on: the status poll goes on while the tab is hidden (the case notifications are for)
  const bp = await movesPage(browser);
  await bp.context().addInitScript(notificationStub, 'granted');
  await bp.context().addInitScript(() => { try { localStorage.setItem('supermarket-dashboard:moves-notify', 'on'); } catch (e) { /* about:blank */ } });
  await mrun('moves-background', bp, async () => {
    let alerts = [MF.alertL];
    await movesRoutes(bp, { status: (now) => alerts.map((f) => f(now)) });
    // the saved choice applies on any view: this page never opens the Outside moves view
    await open(bp, '#overview', { noWait: true });
    await waitStatusPolls(bp, 1);
    await bp.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    alerts = [MF.alertNoBook, MF.alertL];
    await waitStatusPolls(bp, 2, 20000);
    await bp.waitForFunction(() => window.__notes.length === 1, null, { timeout: 10000 });
    check((await bp.evaluate(() => window.__notes[0].title)) === 'Cup lagging: Ohio Senate (R)', 'the notification fires while the tab is hidden');
    await bp.evaluate(() => {
      delete document.hidden;
      delete document.visibilityState;
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await gotoView(bp, 'moves');
    await bp.waitForSelector('#moves-body .move-card');
    check((await text(bp, '#moves-notify')) === 'Desktop notifications on', 'the saved "on" shows on the toggle: ' + (await text(bp, '#moves-notify')));
    await cleanPage(bp, 'moves-background');
  });
  await bp.context().close();

  const dp = await movesPage(browser);
  await dp.context().addInitScript(notificationStub, 'denied');
  await mrun('moves-notifications-denied', dp, async () => {
    let alerts = [MF.alertL];
    await movesRoutes(dp, { status: (now) => alerts.map((f) => f(now)) });
    await openMoves(dp);
    check((await text(dp, '#moves-notify')) === 'Notifications are blocked in this browser’s settings' && (await dp.$eval('#moves-notify', (b) => b.disabled)), 'denied: ' + (await text(dp, '#moves-notify')));
    alerts = [MF.alertNoBook, MF.alertL];
    await waitStatusPolls(dp, 2);
    check((await dp.evaluate(() => window.__notes.length)) === 0 && (await dp.evaluate(() => window.__asked)) === 0, 'no notification and no prompt when denied');
    await cleanPage(dp, 'moves-notifications-denied');
  });
  await dp.context().close();

  const up = await movesPage(browser);
  await up.context().addInitScript(() => { delete window.Notification; });
  await mrun('moves-notifications-unsupported', up, async () => {
    await movesRoutes(up);
    await openMoves(up);
    check((await text(up, '#moves-notify')) === 'Desktop notifications: not supported in this browser' && (await up.$eval('#moves-notify', (b) => b.disabled)), 'unsupported: ' + (await text(up, '#moves-notify')));
    await cleanPage(up, 'moves-notifications-unsupported');
  });
  await up.context().close();

  // ---- 390 px, light and dark
  for (const scheme of ['light', 'dark']) {
    const pp = await movesPage(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2, colorScheme: scheme });
    await mrun('moves-phone-' + scheme, pp, async () => {
      await movesRoutes(pp, { body: (now) => MF.body(now, { summary: 'large' }) });
      for (const w of [390, 360, 320]) {
        await pp.setViewportSize({ width: w, height: 800 });
        await openMoves(pp);
        await setMovesFilter(pp, 'all');
        await pp.click('#moves-filtered-details > summary');
        await pp.waitForTimeout(200);
        const r = await pp.evaluate(() => ({
          over: document.documentElement.scrollWidth - innerWidth,
          wide: Array.from(document.querySelectorAll('#moves-body .move-card, #moves-body .trade-box, #moves-body .moves-controls button')).filter((el) => el.getBoundingClientRect().right > innerWidth + 1).length,
        }));
        check(r.over <= 1, 'no horizontal page scroll at ' + w + ' px (overflow ' + r.over + ' px)');
        check(r.wide === 0, 'cards, trade boxes and buttons fit at ' + w + ' px (' + r.wide + ' too wide)');
      }
      await cleanPage(pp, 'moves-phone-' + scheme);
    });
    await pp.context().close();
  }
}

// ------------------------------------------------------------------ phone checks (touch)

async function phoneChecks(browser) {
  const page = await newPage(browser, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });

  await run('visual-5', page, async () => {
    for (const w of [390, 360, 320]) {
      await page.setViewportSize({ width: w, height: 800 });
      await open(page, '#markets');
      await openDrawer(page, '9002');
      await page.waitForSelector('#drawer table.ladder tbody tr td.bid-px');
      const r = await page.evaluate(() => {
        const textBox = (td) => { const range = document.createRange(); range.selectNodeContents(td); return range.getBoundingClientRect(); };
        const bad = [];
        for (const tr of document.querySelectorAll('#drawer table.ladder tbody tr')) {
          const b = tr.querySelector('td.bid-px'), a = tr.querySelector('td.ask-px');
          if (!b.textContent || !a.textContent) continue;
          const bt = textBox(b), at = textBox(a), bc = b.getBoundingClientRect(), ac = a.getBoundingClientRect();
          const bar = tr.querySelector('.qty-cell.ask .bar');
          if (bt.left < bc.left - 0.5 || bt.right > bc.right + 0.5 || at.left < ac.left - 0.5 || at.right > ac.right + 0.5) bad.push('overflow ' + b.textContent + '/' + a.textContent);
          if (bt.right + 6 > at.left) bad.push('run together ' + b.textContent + a.textContent);
          if (bar && bar.getBoundingClientRect().left < at.right) bad.push('bar covers ' + a.textContent);
        }
        return bad;
      });
      check(!r.length, 'order book prices fit and stay apart at ' + w + ' px: ' + r.slice(0, 3).join(', '));
      await closeDrawer(page);
    }
    await page.setViewportSize({ width: 390, height: 844 });
  });

  await run('visual-7', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9001');
    const box = await hitBox(page);
    const x = box.x + box.width * 0.15;
    await page.touchscreen.tap(x, box.y + box.height / 2);
    await page.waitForSelector('#drawer .tooltip:not([hidden])');
    await page.waitForTimeout(150);
    const cx = await page.evaluate(() => {
      const svg = document.querySelector('#drawer .chart svg');
      const r = svg.getBoundingClientRect();
      const vb = svg.viewBox.baseVal;
      return r.left + Number(document.querySelector('#drawer .chart .crosshair').getAttribute('x1')) * (r.width / vb.width);
    });
    check(Math.abs(cx - x) < 16, 'a tap moves the crosshair to the tapped time (tap ' + x.toFixed(0) + ', crosshair ' + cx.toFixed(0) + ')');
    await closeDrawer(page);
  });

  await run('visual-10-phone', page, async () => {
    await patch(page, '**/api/exchange/9001', (j) => { j.exchange.title = LONG_TITLE; });
    await open(page, '#markets');
    await openDrawer(page, '9001');
    await page.evaluate(() => { document.getElementById('drawer-inner').scrollTop = 600; });
    await page.waitForTimeout(200);
    const share = await page.evaluate(() => document.querySelector('#drawer .drawer-head').getBoundingClientRect().height / innerHeight);
    check(share <= 0.2, 'the phone drawer header stays compact with a long title (' + (share * 100).toFixed(0) + '% of the screen)');
    await closeDrawer(page);
  });

  await run('visual-13', page, async () => {
   for (const w of [390, 360, 320]) {
    await page.setViewportSize({ width: w, height: 800 });
    await open(page, '#markets');
    await openDrawer(page, '9002');
    await page.click('#drawer button[data-action="table-toggle"]');
    await page.waitForSelector('#drawer .chart-table-wrap tbody tr');
    const r = await page.evaluate(() => {
      const wrap = document.querySelector('#drawer .chart-table-wrap');
      const ths = wrap.querySelectorAll('thead th');
      const lastTh = Array.from(ths).filter((th) => getComputedStyle(th).display !== 'none').pop();
      const td = wrap.querySelector('tbody td');
      return { over: wrap.scrollWidth - wrap.clientWidth, rowH: td.getBoundingClientRect().height, surgeVisible: lastTh.textContent === 'Surge' && lastTh.getBoundingClientRect().right <= wrap.getBoundingClientRect().right + 1 };
    });
    check(r.over <= 1, 'the price table fits the phone at ' + w + ' px (overflow ' + r.over + ' px)');
    check(r.rowH <= 40, 'rows stay one line tall at ' + w + ' px (' + r.rowH.toFixed(0) + ' px)');
    check(r.surgeVisible, 'the Surge column is visible at ' + w + ' px');
    await page.click('#drawer button[data-action="table-toggle"]');
    await closeDrawer(page);
   }
    await page.setViewportSize({ width: 390, height: 844 });
  });

  await page.context().close();
}

// ------------------------------------------------------------------ forced colours (Windows High Contrast)

async function forcedColorChecks(browser) {
  const page = await newPage(browser, { forcedColors: 'active' });

  await run('visual-8', page, async () => {
    await open(page, '#markets');
    await openDrawer(page, '9001');
    await page.waitForSelector('#drawer .legend .key-line.surge');
    await page.waitForSelector('#drawer .legend .key-bid');
    const r = await page.evaluate(() => {
      const bg = (sel) => getComputedStyle(document.querySelector(sel)).backgroundColor;
      const stroke = (sel) => getComputedStyle(document.querySelector(sel)).stroke;
      return {
        price: [bg('#drawer .legend .key-line:not(.surge)'), stroke('#drawer .chart .price-line')],
        surge: [bg('#drawer .legend .key-line.surge'), stroke('#drawer .chart .surge-line')],
        bid: [bg('#drawer .legend .key-bid'), bg('#drawer .qty-cell.bid .bar')],
        ask: [bg('#drawer .legend .key-ask'), bg('#drawer .qty-cell.ask .bar')],
      };
    });
    for (const k of Object.keys(r)) check(r[k][0] === r[k][1], k + ' legend key matches its mark in forced colours: ' + r[k].join(' vs '));
    await closeDrawer(page);
  });

  await run('a11y-5', page, async () => {
    await open(page, '#markets');
    const r = await page.$$eval('.app-nav a[data-view]', (as) => as.map((a) => [a.getAttribute('aria-current') === 'page', getComputedStyle(a).borderBottomColor]));
    const current = r.filter((x) => x[0]).map((x) => x[1]);
    const others = new Set(r.filter((x) => !x[0]).map((x) => x[1]));
    check(current.length === 1 && !others.has(current[0]), 'only the current view is underlined: ' + JSON.stringify(r));
  });

  await page.context().close();
}

// ------------------------------------------------------------------ timers (fake clock)

async function timerChecks(browser) {
  const page = await newPage(browser, { clock: true });

  const clickFirstReanalyze = async () => {
    await page.waitForSelector('#surge-cards .surge-card button.reanalyze');
    await page.click('#surge-cards .surge-card button.reanalyze');
  };
  const firstMsg = () => text(page, '#surge-cards .surge-card .card-foot .action-msg');

  await run('functional-8', page, async () => {
    await page.route('**/api/surges/*/analyze', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '{"queued":true,"message":"Queued for analysis."}' }));
    // the surge's data never changes (as for a reverted surge), so nothing else rebuilds the card
    await patch(page, '**/api/surges', (j) => { for (const s of j.surges) { s.current_price = 0.5; s.reverted_fraction = 0.6; s.status = 'reverted'; } });
    await open(page, '#surges');
    await clickFirstReanalyze();
    await page.waitForFunction(() => /Queued/.test(document.querySelector('#surge-cards .surge-card .card-foot .action-msg').textContent));
    await page.clock.fastForward(61000);
    await page.waitForTimeout(300);
    check(!/Queued/.test(await firstMsg()), 'the "Queued for analysis." note clears after a minute: ' + (await firstMsg()));
  });

  await run('robustness-6', page, async () => {
    let newer = false;
    await page.route('**/api/surges/*/analyze', (route) => route.abort('connectionrefused'));
    await patch(page, '**/api/surges', (j, now) => { if (newer && j.surges[0] && j.surges[0].attribution) j.surges[0].attribution.analyzed_at = now + 30; });
    await open(page, '#surges');
    await clickFirstReanalyze();
    await page.waitForFunction(() => /Could not reach the bot/.test(document.querySelector('#surge-cards .surge-card .card-foot .action-msg').textContent));
    newer = true; // a newer analysis lands: the error goes away on the next poll
    await page.waitForFunction(() => !/Could not reach/.test(document.querySelector('#surge-cards .surge-card .card-foot .action-msg').textContent), null, { timeout: 15000 });
    newer = false;
    await open(page, '#surges');
    await clickFirstReanalyze();
    await page.waitForFunction(() => /Could not reach the bot/.test(document.querySelector('#surge-cards .surge-card .card-foot .action-msg').textContent));
    await page.clock.fastForward(61000);
    await page.waitForTimeout(300);
    check(!/Could not reach/.test(await firstMsg()), 'an error note expires too: ' + (await firstMsg()));
  });

  await run('robustness-7', page, async () => {
    const held = [];
    await page.route('**/api/surges/*/analyze', (route) => { held.push(route); }); // never answered
    await open(page, '#surges');
    await clickFirstReanalyze();
    await page.waitForFunction(() => /Sending/.test(document.querySelector('#surge-cards .surge-card button.reanalyze').textContent));
    await page.clock.fastForward(16000);
    await page.waitForFunction(() => /did not answer/.test(document.querySelector('#surge-cards .surge-card .card-foot .action-msg').textContent), null, { timeout: 5000 });
    const disabled = await page.getAttribute('#surge-cards .surge-card button.reanalyze', 'aria-disabled');
    check(disabled !== 'true', 'the button can be used again after the timeout');
    for (const r of held) await r.abort().catch(() => {});
  });

  await page.context().close();
}

// ------------------------------------------------------------------ main

async function main() {
  let child = null;
  let url = URL_ARG;
  if (!url) {
    const started = await startServer(2);
    url = started.url;
    child = started.child;
  }
  BASE = url.replace(/\/+$/, '');
  log('dashboard at ' + BASE);
  const browser = await launch();
  try {
    await desktopChecks(browser);
    await simChecks(browser);
    await movesChecks(browser);
    await phoneChecks(browser);
    await forcedColorChecks(browser);
    await timerChecks(browser);
  } finally {
    await browser.close();
    stopServer(child);
  }
  const failed = results.filter((r) => !r.ok);
  for (const f of failed) process.stderr.write('[regressions] FAILED ' + f.name + ': ' + f.error + '\n');
  log(results.length + ' checks, ' + failed.length + ' failed');
  if (!results.length) {
    log('no checks ran');
    process.exitCode = 1;
  } else if (failed.length) {
    process.exitCode = 1;
  } else {
    log('all regression checks passed');
  }
}

main().catch((err) => {
  process.stderr.write('[regressions] fatal: ' + (err.stack || err.message) + '\n');
  process.exitCode = 1;
});
