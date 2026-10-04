#!/usr/bin/env node
/* Browser smoke test for the dashboard UI (CommonJS, Playwright).
 *
 *   node tests/e2e/ui_smoke.cjs [URL]
 *
 * Without a URL it starts tests/e2e/serve_demo.py itself (python3 on PATH, or $PYTHON).
 * It opens every view, sorts, filters, searches, opens and closes the detail drawer (button,
 * Esc, backdrop), toggles the theme, re-analyzes a surge, simulates an outage and a fatal
 * tracker error, waits for poll cycles, and fails on any console error, page error, failed
 * request, HTTP error response or unhandled promise rejection.
 *
 * Screenshots (1440x900 viewport, plus a 390 px wide phone view) go to docs/screenshots/ or
 * $E2E_SCREENSHOT_DIR. Set E2E_EXTRA_SHOTS=<dir> to also save full-page review shots there.
 * Environment: PLAYWRIGHT_MODULE (default /opt/node-tools/node_modules/playwright),
 * CHROME_PATH (optional explicit browser binary), E2E_HEADED=1 to watch it run.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/pw-browsers')) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
}
const PW_MODULE = process.env.PLAYWRIGHT_MODULE || '/opt/node-tools/node_modules/playwright';
const { chromium } = require(PW_MODULE);

const ROOT = path.resolve(__dirname, '..', '..');
const SHOTS = path.resolve(process.env.E2E_SCREENSHOT_DIR || path.join(ROOT, 'docs', 'screenshots'));
const EXTRA = process.env.E2E_EXTRA_SHOTS ? path.resolve(process.env.E2E_EXTRA_SHOTS) : null;
const VIEWS = ['overview', 'markets', 'surges', 'high', 'strategy'];

const problems = [];
let expectErrors = false; // true only while an outage is simulated on purpose
let navigating = false; // true while the page reloads (in-flight polls are aborted by design)
let statusResponses = 0;

function log(msg) {
  process.stdout.write('[ui-smoke] ' + msg + '\n');
}

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const base = '/opt/pw-browsers';
  if (!fs.existsSync(base)) return null;
  for (const dir of fs.readdirSync(base).filter((d) => /^chromium-\d+$/.test(d)).sort().reverse()) {
    const bin = path.join(base, dir, 'chrome-linux', 'chrome');
    if (fs.existsSync(bin)) return bin;
  }
  return null;
}

async function launch() {
  const headless = !process.env.E2E_HEADED;
  const explicit = process.env.CHROME_PATH ? findChrome() : null;
  if (explicit) return chromium.launch({ headless, executablePath: explicit });
  try {
    return await chromium.launch({ headless });
  } catch (err) {
    const bin = findChrome();
    if (!bin) throw err;
    log('default browser launch failed (' + String(err.message).split('\n')[0] + '); using ' + bin);
    return chromium.launch({ headless, executablePath: bin });
  }
}

function startServer() {
  return new Promise((resolve, reject) => {
    const python = process.env.PYTHON || 'python3';
    const child = spawn(python, [path.join(__dirname, 'serve_demo.py'), '--interval', '2'], {
      cwd: ROOT,
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('serve_demo.py did not print a URL within 60 s')), 60000);
    child.on('error', (err) => { clearTimeout(timer); reject(err); });
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error('serve_demo.py exited early with code ' + code)); });
    child.stdout.on('data', (chunk) => {
      buf += chunk.toString();
      const nl = buf.indexOf('\n');
      if (nl !== -1) {
        clearTimeout(timer);
        child.removeAllListeners('exit');
        resolve({ url: buf.slice(0, nl).trim(), child });
      }
    });
  });
}

function check(cond, message) {
  if (!cond) throw new Error('check failed: ' + message);
}

function watch(page, label) {
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !expectErrors) problems.push(label + ' console error: ' + msg.text() + ' ' + JSON.stringify(msg.location()));
  });
  page.on('pageerror', (err) => problems.push(label + ' page error: ' + (err.stack || err.message)));
  page.on('requestfailed', (req) => {
    const why = (req.failure() && req.failure().errorText) || 'unknown';
    if (expectErrors) return;
    if (navigating && /ERR_ABORTED/.test(why)) return;
    problems.push(label + ' request failed: ' + req.method() + ' ' + req.url() + ' (' + why + ')');
  });
  page.on('response', (resp) => {
    if (resp.url().endsWith('/api/status')) statusResponses += 1;
    if (resp.status() >= 400 && !expectErrors) problems.push(label + ' HTTP ' + resp.status() + ' for ' + resp.request().method() + ' ' + resp.url());
  });
}

async function addRejectionTrap(context) {
  await context.addInitScript(() => {
    window.__rejections = [];
    window.addEventListener('unhandledrejection', (ev) => {
      window.__rejections.push(String(ev.reason && (ev.reason.stack || ev.reason.message) || ev.reason));
    });
  });
}

async function shot(page, name, opts) {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.waitForTimeout(150);
  await page.screenshot({ path: path.join(SHOTS, name + '.png'), animations: 'disabled' });
  if (EXTRA && !(opts && opts.noExtra)) {
    fs.mkdirSync(EXTRA, { recursive: true });
    await page.screenshot({ path: path.join(EXTRA, name + '-full.png'), fullPage: true, animations: 'disabled' });
  }
  log('saved ' + name + '.png');
}

async function waitPolls(page, n, timeout) {
  const start = statusResponses;
  const deadline = Date.now() + (timeout || 30000);
  while (statusResponses < start + n) {
    if (Date.now() > deadline) throw new Error('waited for ' + n + ' polls, saw ' + (statusResponses - start));
    await page.waitForTimeout(200);
  }
}

async function gotoView(page, view) {
  await page.click('.app-nav a[data-view="' + view + '"]');
  await page.waitForFunction((v) => location.hash === '#' + v && !document.getElementById('view-' + v).hidden, view);
}

async function waitDrawerOpen(page) {
  await page.waitForFunction(() => document.getElementById('drawer').open);
  await page.waitForSelector('#drawer .chart svg .price-line, #drawer .chart .chart-empty', { timeout: 20000 });
}

async function waitDrawerClosed(page) {
  await page.waitForFunction(() => !document.getElementById('drawer').open);
}

async function activeKey(page) {
  return page.evaluate(() => document.activeElement && document.activeElement.getAttribute('data-focus-key'));
}

// ------------------------------------------------------------------ scenarios

async function overview(page) {
  await page.waitForFunction(() => /\d/.test(document.getElementById('tile-outcomes').textContent));
  await page.waitForFunction(() => document.getElementById('live-label').textContent === 'Live', null, { timeout: 30000 });
  // surges and the high-90s band need the price-history backfill; the banner hides when it is done
  await page.waitForSelector('#startup-banner', { state: 'hidden', timeout: 90000 });
  await page.waitForFunction(() => /[1-9]/.test(document.getElementById('tile-high').textContent), null, { timeout: 30000 });
  await page.waitForSelector('#look-ideas li a', { timeout: 30000 });
  await page.waitForSelector('#look-surges li a', { timeout: 30000 });
  check(await page.isVisible('#demo-badge'), 'DEMO badge is shown');
  check(/Read-only/.test(await page.textContent('#readonly-note')), 'read-only note is shown');
  check((await page.getAttribute('#updated', 'aria-live')) === 'polite', 'the update time is the polite live region');
  const live = await page.$$eval('[aria-live]', (els) => els.map((e) => e.id));
  check(live.length === 1 && live[0] === 'updated', 'aria-live only on the update time, got ' + live.join(','));
  check(/\d/.test(await page.textContent('#tile-balance')), 'balance tile has a value');
  check(/\d/.test(await page.textContent('#budget-text')), 'read budget shows numbers');
  const title = await page.getAttribute('#updated', 'title');
  check(title && /\d{4}/.test(title), 'update time has an absolute title');
}

async function markets(page) {
  await gotoView(page, 'markets');
  await page.waitForFunction(() => document.querySelectorAll('#markets-body tr').length > 10);
  const total = await page.$$eval('#markets-body tr', (rows) => rows.length);

  // sort by the 1h change: first click sorts descending (biggest risers first)
  const th1h = '#markets-table th[data-sort="change_1h"]';
  await page.click(th1h + ' button');
  check((await page.getAttribute(th1h, 'aria-sort')) === 'descending', 'Δ1h sorts descending first');
  const values = await page.$$eval('#markets-body tr td:nth-child(8)', (tds) => tds.map((td) => td.textContent.replace('−', '-').replace(/[^0-9.+-]/g, '')));
  const nums = values.filter((v) => v !== '').map(Number);
  for (let i = 1; i < nums.length; i++) check(nums[i - 1] >= nums[i], 'Δ1h column is in descending order: ' + nums.join(','));
  const blanksAfter = values.indexOf('') === -1 || values.slice(values.indexOf('')).every((v) => v === '');
  check(blanksAfter, 'missing values sort last');
  await page.click(th1h + ' button');
  check((await page.getAttribute(th1h, 'aria-sort')) === 'ascending', 'second click flips to ascending');
  check((await page.$$eval('#markets-table th[aria-sort]', (t) => t.length)) === 1, 'only one column has aria-sort');

  // keyboard: Enter on the Market header sorts A to Z
  await page.focus('#markets-table th[data-sort="title"] button');
  await page.keyboard.press('Enter');
  check((await page.getAttribute('#markets-table th[data-sort="title"]', 'aria-sort')) === 'ascending', 'Enter on a header sorts');
  const titles = await page.$$eval('#markets-body tr .row-link', (as) => as.map((a) => a.textContent.toLowerCase()));
  for (let i = 1; i < titles.length; i++) check(titles[i - 1].localeCompare(titles[i]) <= 0, 'titles sorted A to Z');

  // search
  await page.fill('#market-search', 'ohio');
  await page.waitForFunction(() => document.querySelectorAll('#markets-body tr').length === 1);
  check(/Ohio/.test(await page.textContent('#markets-body tr')), 'search finds Ohio');
  await page.fill('#market-search', 'zzzz-no-such-market');
  await page.waitForSelector('#markets-empty:not([hidden]) button');
  await page.click('#markets-empty button');
  await page.waitForFunction((n) => document.querySelectorAll('#markets-body tr').length === n, total);
  check((await page.inputValue('#market-search')) === '', 'Show all clears the search');

  // filter chips
  await page.click('.chip[data-filter="surging"]');
  check((await page.getAttribute('.chip[data-filter="surging"]', 'aria-pressed')) === 'true', 'Surging chip pressed');
  const surging = await page.$$eval('#markets-body tr', (rows) => rows.map((r) => !!r.querySelector('.flag-surge')));
  check(surging.length >= 1 && surging.every(Boolean), 'Surging shows only surging rows');
  await page.click('.chip[data-filter="high"]');
  const high = await page.$$eval('#markets-body tr', (rows) => rows.map((r) => !!r.querySelector('.flag-high')));
  check(high.length >= 1 && high.every(Boolean), 'High 90s shows only high-band rows');
  check(/High 90s/.test(await page.textContent('#markets-body tr .flag-high')), 'flag badge has a text label');
  await page.click('.chip[data-filter="all"]');
  await page.waitForFunction((n) => document.querySelectorAll('#markets-body tr').length === n, total);

  // sparklines and signed moves
  check((await page.$$('#markets-body svg.spark path')).length > 5, 'sparklines are drawn');
  const deltaText = await page.$$eval('#markets-body .delta', (els) => els.map((e) => e.textContent));
  check(deltaText.some((t) => /^▲\+0\.\d{3}$/.test(t)), 'up moves show an arrow and a + sign: ' + deltaText.slice(0, 4).join(' '));

  // sort, filter and scroll survive polls
  await page.click('#markets-table th[data-sort="change_24h"] button');
  await page.evaluate(() => window.scrollTo(0, 260));
  const before = await page.$$eval('#markets-body tr', (rows) => rows.map((r) => r.dataset.eid).join(','));
  await waitPolls(page, 2);
  check((await page.getAttribute('#markets-table th[data-sort="change_24h"]', 'aria-sort')) === 'descending', 'sort kept across polls');
  const scrollY = await page.evaluate(() => window.scrollY);
  check(Math.abs(scrollY - 260) < 2, 'scroll position kept across polls (got ' + scrollY + ')');
  const after = await page.$$eval('#markets-body tr', (rows) => rows.map((r) => r.dataset.eid).join(','));
  check(before.split(',').length === after.split(',').length, 'row count stable across polls');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click('#markets-table th[data-sort="title"] button');
}

async function drawer(page, theme) {
  // row click (not on the link) opens the drawer
  await page.click('#markets-body tr[data-eid="9002"] td.cell-option');
  await waitDrawerOpen(page);
  check(/^#exchange\/9002$/.test(await page.evaluate(() => location.hash)), 'hash routes to #exchange/9002');
  check(/Ohio/.test(await page.textContent('#drawer-title')), 'drawer shows the market title');
  check(await page.evaluate(() => document.getElementById('drawer').contains(document.activeElement)), 'focus moved into the drawer');
  check(!(await page.isHidden('#view-markets')), 'the markets view stays underneath');

  for (const r of ['6h', '7d', '24h']) {
    await page.click('#drawer button[data-range="' + r + '"]');
    check((await page.getAttribute('#drawer button[data-range="' + r + '"]', 'aria-pressed')) === 'true', r + ' range pressed');
    await page.waitForSelector('#drawer .chart svg .price-line');
  }
  check((await page.$$('#drawer .chart svg .surge-line')).length >= 1, 'surge segment is highlighted');
  check((await page.$$('#drawer .chart svg text.tick')).length >= 4, 'axis ticks are drawn');

  // hover crosshair + tooltip
  const box = await page.locator('#drawer .chart svg rect.hit').boundingBox();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height / 2);
  await page.waitForSelector('#drawer .tooltip:not([hidden])');
  check(/\d\.\d{3}/.test(await page.textContent('#drawer .tooltip')), 'tooltip shows a 3 dp price');
  check(/%/.test(await page.textContent('#drawer .tooltip')), 'tooltip shows a percentage');
  await shot(page, 'drawer-' + theme);
  await page.mouse.move(5, 5);

  // keyboard on the chart
  await page.focus('#drawer .chart');
  await page.keyboard.press('ArrowLeft');
  await page.waitForSelector('#drawer .tooltip:not([hidden])');

  // table view toggle
  await page.click('#drawer button[data-action="table-toggle"]');
  await page.waitForSelector('#drawer .chart-table-wrap table tbody tr');
  check((await page.getAttribute('#drawer button[data-action="table-toggle"]', 'aria-pressed')) === 'true', 'table toggle pressed');
  await page.click('#drawer button[data-action="table-toggle"]');
  await page.waitForSelector('#drawer .chart svg .price-line');

  // order book ladder, trades and the latest surge
  await page.waitForSelector('#drawer table.ladder tbody tr');
  check(/\d\.\d{3}/.test(await page.textContent('#drawer .book-summary')), 'best bid / ask shown');
  await page.waitForSelector('#drawer .surge-card .verdict');
  check(/Participants|News|Unclear|Analyzing/.test(await page.textContent('#drawer .surge-card .verdict')), 'drawer shows the verdict');

  // close with the button: focus returns to the row's link
  await page.click('#drawer-close');
  await waitDrawerClosed(page);
  check((await page.evaluate(() => location.hash)) === '#markets', 'closing returns to #markets');
  check((await activeKey(page)) === 'row:9002', 'focus returned to the row (got ' + (await activeKey(page)) + ')');

  // keyboard: Enter on a row link opens, Esc closes and focus returns
  await page.focus('#markets-body tr[data-eid="9001"] a.row-link');
  await page.keyboard.press('Enter');
  await waitDrawerOpen(page);
  await page.keyboard.press('Escape');
  await waitDrawerClosed(page);
  check((await activeKey(page)) === 'row:9001', 'Esc returns focus to the link');

  // two quick Esc presses close the drawer once (no extra step back through history)
  await page.click('#markets-body tr[data-eid="9001"] a.row-link');
  await waitDrawerOpen(page);
  // two Esc (cancel) events in the same task: faster than any hashchange can land in between
  await page.evaluate(() => {
    const dlg = document.getElementById('drawer');
    dlg.dispatchEvent(new Event('cancel', { cancelable: true }));
    dlg.dispatchEvent(new Event('cancel', { cancelable: true }));
  });
  await waitDrawerClosed(page);
  await page.waitForTimeout(700);
  check((await page.evaluate(() => location.hash)) === '#markets', 'a double Esc stays on #markets, got ' + (await page.evaluate(() => location.hash)));
  check(!(await page.isHidden('#view-markets')), 'markets view still shown after a double Esc');

  // backdrop click closes
  await page.click('#markets-body tr[data-eid="9007"] a.row-link');
  await waitDrawerOpen(page);
  await page.mouse.click(30, 450);
  await waitDrawerClosed(page);
  check((await page.evaluate(() => location.hash)) === '#markets', 'backdrop click closes the drawer');
}

async function surges(page) {
  await gotoView(page, 'surges');
  // The demo scripts a news surge and a participant spike; how the spike is judged depends on timing,
  // so only the news verdict (and some finished verdict on every card) is required.
  await page.waitForFunction(() => document.querySelectorAll('#surge-cards .surge-card').length >= 1, null, { timeout: 30000 });
  await page.waitForSelector('#surge-cards .verdict-news', { timeout: 30000 });
  await page.waitForFunction(() => Array.from(document.querySelectorAll('#surge-cards .surge-card')).every((c) =>
    c.querySelector('.verdict-news, .verdict-participants, .verdict-unclear')), null, { timeout: 30000 });
  const labels = await page.$$eval('#surge-cards .surge-card .card-badges .verdict', (els) => els.map((e) => e.textContent));
  for (const l of labels) check(/^(News|Participants|Unclear)\d+% confidence$/.test(l), 'verdict badge has label + confidence: ' + l);
  const links = await page.$$eval('#surge-cards .headline a', (as) => as.map((a) => [a.target, a.rel, a.protocol]));
  check(links.length > 0, 'news headlines are listed');
  for (const l of links) check(l[0] === '_blank' && l[1] === 'noopener noreferrer' && /^https?:$/.test(l[2]), 'headline link is safe: ' + l.join(' '));
  const first = page.locator('#surge-cards .surge-card').first();
  await first.locator('button.reanalyze').click();
  await page.waitForFunction(() => {
    const msg = document.querySelector('#surge-cards .surge-card .action-msg');
    return msg && /Queued|Already queued/.test(msg.textContent);
  });
  check(/Re-analyze|Sending/.test(await first.locator('button.reanalyze').textContent()), 'Re-analyze button is still there');
}

async function high(page) {
  await gotoView(page, 'high');
  await page.waitForSelector('#high-body tr');
  const text = await page.textContent('#high-body');
  check(/Yes, pays out/.test(text) && /No — valued at market price/.test(text), 'settles-before-Cup-end shows both cases');
  check(/\/share/.test(text), 'payout per share shown');
}

async function strategy(page) {
  await gotoView(page, 'strategy');
  await page.waitForSelector('#strategy-body .risk-banner');
  await page.waitForSelector('#strategy-body .idea');
  check(/BUY (YES|NO) @ \d\.\d{3}/.test(await page.textContent('#strategy-body .idea .idea-action')), 'ideas say BUY YES/NO @ price');
  await page.click('#strategy-body .idea details summary');
  check(await page.evaluate(() => document.querySelector('#strategy-body .idea details').open), 'rationale expands');
  check(/Read-only/.test(await page.textContent('#strategy-body .disclaimer')), 'disclaimer shown');
}

async function themeAndPersistence(page) {
  await page.click('[data-theme-choice="dark"]');
  check((await page.getAttribute('html', 'data-theme')) === 'dark', 'dark theme applied');
  check((await page.evaluate(() => localStorage.getItem('supermarket-dashboard:theme'))) === 'dark', 'theme persisted');
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  check(bg === 'rgb(13, 13, 13)', 'dark page background, got ' + bg);
}

async function darkShots(page) {
  for (const v of VIEWS) {
    await gotoView(page, v);
    await settleView(page, v);
    await shot(page, v + '-dark');
  }
  await gotoView(page, 'markets');
  await page.click('#markets-body tr[data-eid="9001"] a.row-link');
  await waitDrawerOpen(page);
  await page.waitForSelector('#drawer table.ladder tbody tr');
  const box = await page.locator('#drawer .chart svg rect.hit').boundingBox();
  await page.mouse.move(box.x + box.width * 0.7, box.y + box.height / 2);
  await page.waitForSelector('#drawer .tooltip:not([hidden])');
  await shot(page, 'drawer-dark');
  await page.keyboard.press('Escape');
  await waitDrawerClosed(page);

  navigating = true;
  await page.reload();
  navigating = false;
  check((await page.getAttribute('html', 'data-theme')) === 'dark', 'theme survives a reload');
  check((await page.getAttribute('[data-theme-choice="dark"]', 'aria-pressed')) === 'true', 'Dark button pressed after reload');
  await page.click('[data-theme-choice="system"]');
  check((await page.getAttribute('html', 'data-theme')) === null, 'System removes the override');
  check((await page.evaluate(() => localStorage.getItem('supermarket-dashboard:theme'))) === null, 'System clears storage');
}

async function settleView(page, v) {
  if (v === 'overview') await page.waitForSelector('#look-ideas li a');
  if (v === 'markets') await page.waitForSelector('#markets-body tr');
  if (v === 'surges') await page.waitForSelector('#surge-cards .surge-card');
  if (v === 'high') await page.waitForSelector('#high-body tr');
  if (v === 'strategy') await page.waitForSelector('#strategy-body .idea');
}

async function deepLink(page, url) {
  navigating = true;
  await page.goto('about:blank'); // a fresh load, not a same-document hash change
  await page.goto(url + '/#exchange/9007');
  navigating = false;
  await waitDrawerOpen(page);
  check(/California/.test(await page.textContent('#drawer-title')), 'deep link opens the drawer');
  await page.click('#drawer-close');
  await waitDrawerClosed(page);
  check((await page.evaluate(() => location.hash)) === '#overview', 'closing a deep-linked drawer shows the overview');
}

async function outage(page) {
  expectErrors = true;
  const fail = (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"simulated outage"}' });
  await page.route('**/api/status', fail);
  await page.waitForSelector('#offline-banner:not([hidden])', { timeout: 20000 });
  check((await page.textContent('#live-label')) === 'Offline', 'live indicator says Offline');
  check(/Can't reach the bot/.test(await page.textContent('#offline-banner')), 'banner says it cannot reach the bot');
  await shot(page, 'offline-light', { noExtra: true });
  await page.click('#offline-dismiss');
  check(await page.isHidden('#offline-banner'), 'banner can be dismissed');
  await page.unroute('**/api/status', fail);
  await page.waitForFunction(() => document.getElementById('live-label').textContent === 'Live', null, { timeout: 45000 });
  await page.waitForTimeout(500);
  expectErrors = false;

  // a fatal tracker error (for example a revoked key) is shown prominently with a hint
  const fatal = async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    json.tracker.fatal_error = 'HTTP 401 API_KEY_REVOKED: This API key has been revoked (GET /exchanges/prices)';
    await route.fulfill({ response: resp, json });
  };
  await page.route('**/api/status', fatal);
  await page.waitForSelector('#fatal-banner:not([hidden])', { timeout: 20000 });
  check(/SUPERMARKET_API_KEY/.test(await page.textContent('#fatal-hint')), 'fatal banner explains how to fix the key');
  check((await page.textContent('#live-label')) === 'Stopped', 'live indicator says Stopped');
  await shot(page, 'fatal-light', { noExtra: true });
  await page.unroute('**/api/status', fatal);
  await page.waitForSelector('#fatal-banner[hidden]', { state: 'attached', timeout: 20000 });

  // no snapshot for longer than two intervals: the indicator says Stale
  const stale = async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    json.tracker.last_snapshot_at = json.now - 600;
    await route.fulfill({ response: resp, json });
  };
  await page.route('**/api/status', stale);
  await page.waitForFunction(() => document.getElementById('live-label').textContent === 'Stale', null, { timeout: 20000 });
  check(/10 min ago/.test(await page.textContent('#updated')), 'update time shows the stale age');
  await page.unroute('**/api/status', stale);
  await page.waitForFunction(() => document.getElementById('live-label').textContent === 'Live', null, { timeout: 20000 });
}

async function hiddenTab(page) {
  // pretend the tab is hidden: polling must stop, then resume when it is visible again
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(500);
  const start = statusResponses;
  await page.waitForTimeout(7000);
  check(statusResponses === start, 'no polling while the tab is hidden (saw ' + (statusResponses - start) + ' polls)');
  await page.evaluate(() => {
    delete document.hidden;
    delete document.visibilityState;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await waitPolls(page, 1, 4000);
}

// ------------------------------------------------------------------ hostile / malformed API data

const EVIL = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';

function evilPayloads(now) {
  return {
    '/api/status': {
      ok: true, demo: 'yes', now, interval: 'x', read_only: true,
      tournament: { name: EVIL, currency: EVIL },
      tracker: { cycles: 2, last_snapshot_at: now - 1, read_budget: { used: 5, limit: 0 }, backfill: { done: 'a', total: null }, fatal_error: null },
      account: { balance: 'lots', my_rank: null, leader_value: 'x' },
      counts: { outcomes: 3, markets: null, surges: 'many', high_band: -1 },
      features: null, cup_end: 'never', days_left: null, view_error: null,
    },
    '/api/markets': {
      total: 'n',
      rows: [
        { exchange_id: 'x1', market_id: 7, title: EVIL, option: EVIL, last: 'abc', bid: null, ask: 1.5, spread: -1, change_5m: -0.0004,
          change_1h: 1e-9, change_24h: 0.25, sparkline: [[1, null], 'bad', [2], [now - 100, 0.4], [now, 0.5]], settlement_date: 'not a date',
          high_band: {}, surge_id: 9 },
        { exchange_id: 'x1', title: 'duplicate id' },
        { exchange_id: 42, title: null, sparkline: null },
        { exchange_id: { nested: true } }, {}, null, 7, 'str', [1, 2],
      ],
    },
    '/api/surges': {
      surges: [
        { id: 9, exchange_id: 'x1', title: EVIL, option: null, window: null, start_price: 'a', end_price: null, change: null, zscore: 'z',
          status: 'zombie', reverted_fraction: -0.5, detected_at: 'yesterday',
          attribution: { verdict: 'weird', confidence: 'high', reversion_odds: null, summary: EVIL, reasons: [EVIL, null, 5],
            articles: [{ title: EVIL, url: 'javascript:window.__xss=1', source: EVIL, published_at: 'x' },
              { title: 'ok', url: 'https://example.com/a', published_at: now - 600, relevance: 2 },
              { title: 'data url', url: 'data:text/html,hi' }, null, 'str'],
            flow: { n_trades: null, hhi: 'x' }, book_depth: 'deep', method: null, analyzed_at: null } },
        { id: null, exchange_id: 'x2', attribution: null, status: 'open' },
        { id: 'abc', exchange_id: null, attribution: 'nope' },
        null, 'junk',
      ],
    },
    '/api/highband': {
      cup_end: null,
      bands: [{ exchange_id: 'x1', title: EVIL, side: null, favorite_price: null, time_in_band: 'all', stable: 'yes', low: null, high: null,
        settles_before_cup_end: 'maybe', payout_per_share: null, return_pct: 'big', lookback_s: -5, settlement_ts: null, settlement_date: 'garbage' },
      null, { exchange_id: '' }],
    },
    '/api/strategy': {
      available: true, risk_mode: EVIL, headline: EVIL, principles: [EVIL, null], backtest_status: 'ready', disclaimer: EVIL,
      balance: 'x', my_rank: 'first', leader_value: null, days_left: 'soon',
      opportunities: [
        { kind: EVIL, exchange_id: 'x1', market_id: null, title: EVIL, option: EVIL, side: null, entry_price: 'a', target_price: null, stop_price: null,
          prob_win: 'p', edge: null, expected_return: null, horizon_hours: 'h', suggested_shares: 'many', suggested_cost: null, score: 'top',
          confidence: null, rationale: EVIL, risks: [EVIL], settles_before_cup_end: 'maybe' },
        null,
        { kind: 'arbitrage', exchange_id: null, side: 'yes', entry_price: 0.97, rationale: [null] },
      ],
      backtest: { n_surges: null, by_window: { '1h': null, '<b>w</b>': { n: 'x' } }, notes: [EVIL, null] },
    },
    '/api/exchange/x1': {
      now, series_window_s: 'x',
      exchange: { exchange_id: 'x1', title: EVIL, option: null, last: null, settlement_date: 12345 },
      series: [[now - 3000, 0.4, EVIL], [null, null], 'x', [now - 1000, 'a'], [now - 500, 0.45, 'tick'], [now - 10, 0.5]],
      surges: [null, { start_ts: 'a' }, { id: 9, start_ts: now - 2000, end_ts: now - 100, start_price: 0.4, end_price: 0.5, change: 0.1, window: '1h', attribution: null }],
      trades: [null, { ts: null, price: 'p', size: null, side: EVIL }],
      book: { bids: [null, { price: null, quantity: null }, { price: 0.4, quantity: 10 }], asks: 'x', best_bid: 'b' },
      book_error: EVIL, book_fetched_at: 'now', high_band: { side: null },
    },
    '/api/exchange/42': {},
  };
}

async function hostile(browser, url) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
  await addRejectionTrap(context);
  const page = await context.newPage();
  watch(page, 'hostile');
  page.on('dialog', (d) => { problems.push('hostile: a dialog opened: ' + d.message()); d.dismiss().catch(() => {}); });
  const now = Date.now() / 1000;
  const payloads = evilPayloads(now);
  await page.route('**/api/**', (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (req.method() === 'POST') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ queued: true, message: EVIL }) });
    const body = Object.prototype.hasOwnProperty.call(payloads, p) ? payloads[p] : {};
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  const safeDom = async (where) => {
    const r = await page.evaluate(() => ({
      imgs: document.querySelectorAll('img').length,
      scripts: document.querySelectorAll('script').length,
      xss: !!window.__xss,
      badLinks: Array.from(document.querySelectorAll('a[href]')).map((a) => a.getAttribute('href')).filter((h) => !/^(https?:\/\/|#)/.test(h)),
    }));
    check(r.imgs === 0 && r.scripts === 1 && !r.xss && r.badLinks.length === 0, where + ': hostile strings stayed text ' + JSON.stringify(r));
    if (EXTRA) await page.screenshot({ path: path.join(EXTRA, 'hostile-' + where.replace(/[^a-z]+/g, '-') + '.png'), fullPage: true });
  };
  await page.goto(url + '/#overview');
  await page.waitForFunction(() => document.getElementById('tournament-name').textContent.indexOf('<img') !== -1);
  await page.waitForTimeout(600);
  await safeDom('overview');
  for (const v of ['markets', 'surges', 'high', 'strategy']) {
    await gotoView(page, v);
    await page.waitForTimeout(600);
    await safeDom(v);
  }
  await gotoView(page, 'markets');
  check((await page.$$('#markets-body tr')).length === 2, 'rows without a usable id are skipped and duplicates collapse');
  await gotoView(page, 'surges');
  check((await page.$$('#surge-cards .headline a')).length === 1, 'only the https headline is a link');
  await page.click('#surge-cards button.reanalyze');
  await page.waitForFunction(() => /<img/.test(document.querySelector('#surge-cards .action-msg').textContent));
  await safeDom('re-analyze message');
  await gotoView(page, 'strategy');
  await page.click('#strategy-body .idea details summary');
  await safeDom('strategy details');
  await gotoView(page, 'markets');
  await page.click('#markets-body tr[data-eid="x1"] a.row-link');
  await page.waitForFunction(() => document.getElementById('drawer').open && /<img/.test(document.getElementById('drawer-title').textContent));
  await page.waitForTimeout(600);
  await safeDom('drawer');
  await page.click('#drawer button[data-action="table-toggle"]');
  await page.waitForTimeout(200);
  await safeDom('drawer table');
  await page.keyboard.press('Escape');
  await waitDrawerClosed(page);
  await page.click('#markets-body tr[data-eid="42"] a.row-link');
  await page.waitForFunction(() => document.getElementById('drawer').open && /Untitled/.test(document.getElementById('drawer-title').textContent));
  await page.keyboard.press('Escape');
  await waitDrawerClosed(page);
  await checkRejections(page, 'hostile');
  await context.close();
}

async function mobile(browser, url) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, colorScheme: 'light', reducedMotion: 'reduce', isMobile: true, hasTouch: true });
  await addRejectionTrap(context);
  const page = await context.newPage();
  watch(page, 'mobile');
  await page.goto(url + '/#overview');
  await page.waitForSelector('#look-ideas li a', { timeout: 30000 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - 390);
  check(overflow <= 1, 'no horizontal page scroll at 390 px (overflow ' + overflow + ' px)');
  await shot(page, 'mobile-390');
  for (const v of ['markets', 'surges', 'high', 'strategy']) {
    await gotoView(page, v);
    await settleView(page, v);
    const o = await page.evaluate(() => document.documentElement.scrollWidth - 390);
    check(o <= 1, v + ': no horizontal page scroll at 390 px (overflow ' + o + ' px)');
    if (EXTRA) await page.screenshot({ path: path.join(EXTRA, 'mobile-390-' + v + '.png'), fullPage: true });
  }
  await gotoView(page, 'markets'); // waits for the hashchange (a click returns before it fires)
  await page.waitForSelector('#markets-body tr');
  const scrolls = await page.$eval('#markets-wrap', (el) => el.scrollWidth > el.clientWidth);
  check(scrolls, 'the markets table scrolls horizontally inside its box');
  await page.click('#markets-body tr[data-eid="9002"] a.row-link');
  await waitDrawerOpen(page);
  const o2 = await page.evaluate(() => {
    const inner = document.getElementById('drawer-inner');
    return Math.max(inner.scrollWidth - inner.clientWidth, document.documentElement.scrollWidth - 390);
  });
  check(o2 <= 1, 'drawer has no horizontal overflow at 390 px (' + o2 + ')');
  if (EXTRA) await page.screenshot({ path: path.join(EXTRA, 'mobile-390-drawer.png') });
  await checkRejections(page, 'mobile');
  await context.close();
}

async function checkRejections(page, label) {
  const rej = await page.evaluate(() => window.__rejections || []);
  for (const r of rej) problems.push(label + ' unhandled rejection: ' + r);
}

async function run(url) {
  const browser = await launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light', reducedMotion: 'reduce' });
    await addRejectionTrap(context);
    const page = await context.newPage();
    watch(page, 'desktop');
    log('opening ' + url);
    await page.goto(url + '/');

    await overview(page);
    await shot(page, 'overview-light');
    log('overview ok');
    await markets(page);
    await shot(page, 'markets-light');
    log('markets ok');
    await drawer(page, 'light');
    log('drawer ok');
    await surges(page);
    await shot(page, 'surges-light');
    log('surges ok');
    await high(page);
    await shot(page, 'high-light');
    log('high ok');
    await strategy(page);
    await shot(page, 'strategy-light');
    log('strategy ok');
    await waitPolls(page, 2);
    log('two poll cycles ok');
    await themeAndPersistence(page);
    await darkShots(page);
    log('dark mode ok');
    await deepLink(page, url);
    log('deep link ok');
    await outage(page);
    log('outage, fatal and stale states ok');
    await hiddenTab(page);
    log('hidden tab pauses polling ok');
    await checkRejections(page, 'desktop');
    await context.close();
    await mobile(browser, url);
    log('mobile ok');
    await hostile(browser, url);
    log('hostile data ok');
  } catch (err) {
    await dumpFailure(browser);
    throw err;
  } finally {
    await browser.close();
  }
}

async function dumpFailure(browser) {
  try {
    for (const ctx of browser.contexts()) {
      for (const page of ctx.pages()) {
        fs.mkdirSync(SHOTS, { recursive: true });
        await page.screenshot({ path: path.join(SHOTS, 'failure.png'), fullPage: true });
        const state = await page.evaluate(() => ({
          hash: location.hash,
          drawerOpen: document.getElementById('drawer').open,
          visibleView: Array.from(document.querySelectorAll('main > .view')).filter((v) => !v.hidden).map((v) => v.id),
          live: document.getElementById('live-label').textContent,
          banners: Array.from(document.querySelectorAll('.banner')).filter((b) => !b.hidden).map((b) => b.id),
          surgeCards: document.querySelectorAll('#surge-cards .surge-card').length,
          surgesEmpty: document.getElementById('surges-empty').textContent,
        }));
        process.stderr.write('[ui-smoke] page state at failure: ' + JSON.stringify(state) + '\n');
      }
    }
  } catch (e) {
    process.stderr.write('[ui-smoke] could not dump the failure state: ' + e.message + '\n');
  }
}

async function main() {
  let url = process.argv[2];
  let child = null;
  if (!url) {
    const started = await startServer();
    url = started.url;
    child = started.child;
  }
  url = url.replace(/\/+$/, '');
  let failed = false;
  try {
    await run(url);
  } catch (err) {
    failed = true;
    problems.unshift('scenario failed: ' + (err.stack || err.message));
  } finally {
    if (child) {
      child.stdin.end();
      setTimeout(() => child.kill('SIGTERM'), 3000).unref();
    }
  }
  if (problems.length) {
    for (const p of problems) process.stderr.write('[ui-smoke] PROBLEM: ' + p + '\n');
    process.exitCode = 1;
    return;
  }
  log(failed ? 'FAILED' : 'all checks passed');
  process.exitCode = failed ? 1 : 0;
}

main().catch((err) => {
  process.stderr.write('[ui-smoke] fatal: ' + (err.stack || err.message) + '\n');
  process.exitCode = 1;
});
