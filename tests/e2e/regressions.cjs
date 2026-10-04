#!/usr/bin/env node
/* Regression checks for the dashboard UI bugs found in QA round 1 (CommonJS, Playwright).
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
    await page.waitForFunction(() => /News search unavailable/.test(document.querySelector('#surge-cards .surge-card').textContent), null, { timeout: 15000 });
    check(!/No matching headlines/.test(await text(page, '#surge-cards .surge-card')), 'an outage is not reported as "no matching headlines"');
    ns = 'disabled';
    await page.waitForFunction(() => /News search off/.test(document.querySelector('#surge-cards .surge-card').textContent), null, { timeout: 15000 });
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
