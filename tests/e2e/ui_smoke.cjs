#!/usr/bin/env node
/* Browser smoke test for the dashboard UI (CommonJS, Playwright).
 *
 *   node tests/e2e/ui_smoke.cjs [URL]
 *
 * Without a URL it starts tests/e2e/serve_demo.py itself (python3 on PATH, or $PYTHON).
 * It opens every view (including the Simulation view: the human-speed headline, the table warning,
 * the published demo portfolios and top-3 bar, a simulated fill; and the Outside moves view: the demo's
 * scripted New Hampshire Senate (D) lagging alert with its suggested hand trade, which serve_demo.py's
 * 5-s outside poll raises about 90 s after the start), sorts, filters, searches, opens and
 * closes the detail drawer (button, Esc, backdrop), toggles the theme, re-analyzes a surge, simulates an outage and a fatal
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
const VIEWS = ['overview', 'markets', 'surges', 'moves', 'high', 'strategy', 'sim'];
// Published demo constants (docs/PAPER_TRADING.md §7.6): portfolio ids in config order, headline first.
const SIM_PORTFOLIOS = ['human:conservative', 'policy:conservative', 'policy:chaser', 'kind:value', 'kind:basket', 'kind:hole', 'kind:fade',
  'kind:carry', 'kind:arbitrage'];

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
  await page.screenshot({ path: path.join(SHOTS, name + '.png'), animations: 'disabled', fullPage: !!(opts && opts.fullPage) });
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

/** The chart's hover target; re-read when the chart redraws (for example when the order book arrives
 *  and the drawer gains a scrollbar, the chart is redrawn at the new width). */
async function hitBox(page) {
  const deadline = Date.now() + 10000;
  for (;;) {
    const handle = await page.$('#drawer .chart svg rect.hit');
    const box = handle && (await handle.boundingBox().catch(() => null));
    if (box) return box;
    if (Date.now() > deadline) throw new Error('the chart hover target never settled');
    await page.waitForTimeout(100);
  }
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
  // a11y-1: the ticking update time is plain text; the live indicator speaks only when its state changes
  check((await page.getAttribute('#updated', 'aria-live')) === null, 'the ticking update time is not a live region');
  check((await page.getAttribute('#live', 'role')) === 'status', 'the live indicator is the status region');
  check(/Prices are live/.test(await page.textContent('#live-sentence')), 'the status region says "Prices are live."');
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
  const titles = await page.$$eval('#markets-body tr .row-link > span:first-child', (as) => as.map((a) => a.textContent.toLowerCase()));
  for (let i = 1; i < titles.length; i++) check(titles[i - 1].localeCompare(titles[i]) <= 0, 'titles sorted A to Z');

  // search
  // (both words must match: the outside-moves demo also has an Ohio Senate market)
  await page.fill('#market-search', 'ohio house');
  await page.waitForFunction(() => document.querySelectorAll('#markets-body tr').length === 1);
  check(/Ohio House/.test(await page.textContent('#markets-body tr')), 'search finds Ohio House District 9');
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
  const box = await hitBox(page);
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
  check((await page.textContent('#drawer button[data-action="table-toggle"]')).trim() === 'Table view', 'the toggle keeps its label');
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
  // one card by its surge id: a live surge detected meanwhile goes on top and must not take its place
  const sid = await page.getAttribute('#surge-cards .surge-card button.reanalyze', 'data-surge-id');
  const btn = '#surge-cards .surge-card button.reanalyze[data-surge-id="' + sid + '"]';
  await page.click(btn);
  await page.waitForFunction((sel) => {
    const card = document.querySelector(sel) && document.querySelector(sel).closest('.surge-card');
    const msg = card && card.querySelector('.action-msg');
    return msg && /Queued|Already queued/.test(msg.textContent);
  }, btn);
  check(/Re-analyze|Sending/.test(await page.textContent(btn)), 'Re-analyze button is still there');
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
  check(/BUY (YES|NO)( on every outcome)? @ \d\.\d{3}|Buy \d+ legs @ \d\.\d{3}/.test(await page.textContent('#strategy-body .idea .idea-action')), 'ideas say BUY YES/NO @ price');
  await page.click('#strategy-body .idea details summary');
  check(await page.evaluate(() => document.querySelector('#strategy-body .idea details').open), 'rationale expands');
  check(/Read-only/.test(await page.textContent('#strategy-body .disclaimer')), 'disclaimer shown');
}

async function simulation(page) {
  await gotoView(page, 'sim');
  await page.waitForSelector('#sim-body [data-panel="headline"] .verdict-sentence', { timeout: 60000 });
  check(/^human:/.test(await page.getAttribute('#sim-body [data-panel="headline"]', 'data-portfolio')), 'the headline is the human: portfolio');
  check(/Paper trading on live data\. Nothing is traded: every order here is simulated\./.test(await page.textContent('#view-sim .view-head')), 'the view says nothing is traded');
  check(await page.isVisible('#sim-demo-badge'), 'the demo badge is shown on the Simulation view');
  const warning = await page.textContent('#sim-body [data-panel="portfolios"] .table-warning');
  check(/^The best of 9 portfolios looks better than it is by chance: judge the headline/.test(warning.trim()), 'the table warning is present: ' + warning);
  const ids = await page.$$eval('#sim-body .portfolios-table tbody tr[data-pid]', (trs) => trs.map((tr) => tr.dataset.pid));
  check(ids.join(',') === SIM_PORTFOLIOS.join(','), 'the published portfolios, headline first: ' + ids.join(','));
  const summary = await page.evaluate(() => [document.querySelector('#sim-body .sim-chart').getAttribute('aria-label'),
    document.querySelector('#sim-body [data-panel="equity"] .chart-summary').textContent]);
  check(/^Equity curves: 9 portfolios; headline \(you, by hand\): /.test(summary[0]), 'the equity summary names the headline: ' + summary[0]);
  check(!/\bbest\b/i.test(summary.join(' ')), 'no "best" in the equity summary: ' + summary.join(' / '));
  const verdict = await page.textContent('#sim-body [data-panel="headline"] .verdict-level');
  check(/Not enough evidence|Inconclusive|Promising, not proof|Losing so far/.test(verdict), 'a one-day demo run never reads "Profitable so far": ' + verdict);
  check((await page.$$('#sim-body [data-panel="headline"] .verdict-caveats li')).length === 4, 'the verdict caveats sit under the verdict');
  // the demo scripts fills within the first minutes (2 s steps here); wait for one
  await page.waitForSelector('#sim-body .fills-table tbody tr', { timeout: 90000 });
  check(/^(Bought|Sold|Settled) \d[\d,]* (YES|NO) @ \d\.\d{3}$/.test((await page.textContent('#sim-body .fills-table tbody tr .fill-text')).trim()), 'a fill reads as an order');
  const chaser = (await page.textContent('#sim-body [data-panel="chaser"]')).replace(/\s+/g, ' ');
  check(/221,500/.test(chaser) && /M\s*2\.2(?!\d)/.test(chaser), 'the chaser sees the published top-3 bar 221,500 and M 2.2: ' + chaser.slice(0, 200));
  check(/Settlement rule assumed: unknown, valued conservatively/.test(await page.textContent('#sim-body [data-panel="clock"]')), 'the run clock names the settlement rule');
  check(await page.isVisible('#sim-body [data-panel="backtest"]') && await page.isVisible('#sim-body [data-panel="fairvalue"]'), 'backtest and fair-value panels are shown');
  check((await page.$$('#sim-body [data-panel="caveats"] li')).length >= 9, 'How to read this lists the caveats');
  // the reset dialog opens and Cancel sends nothing (the regression checks cover the POST)
  await page.click('#sim-reset');
  await page.waitForFunction(() => document.getElementById('sim-reset-dialog').open);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.getElementById('sim-reset-dialog').open);
}

/** The real demo (serve_demo.py: moves on, 5-s outside poll): the first alert is the scripted New Hampshire
 *  Senate (D) move, lagging, with a suggested hand trade (docs/OUTSIDE_MOVES.md §15.2, §19.6). It opens about
 *  90 s after the server started and stays lagging for a few minutes, so this runs first. */
async function moves(page) {
  await gotoView(page, 'moves');
  check(/Outside moves/.test(await page.textContent('#h-moves')), 'the Outside moves view has its heading');
  await page.waitForSelector('#moves-body .move-card', { timeout: 200000 });
  const first = await page.evaluate(() => {
    const card = document.querySelector('#moves-body .move-card');
    return {
      race: card.querySelector('.move-race').textContent.trim(),
      status: card.querySelector('.move-status').textContent.trim(),
      trade: card.querySelector('.trade-box:not(.no-trade-box)') ? card.querySelector('.trade-box').textContent.replace(/\s+/g, ' ').trim() : '',
      href: card.querySelector('.move-title a').getAttribute('href'),
    };
  });
  check(first.race === 'New Hampshire Senate (D)' && first.status === 'Cup lagging', 'the first alert is the New Hampshire Senate (D) lagging alert: ' + JSON.stringify(first));
  check(/^Suggested hand trade: Buy YES at 0\.\d{3} — /.test(first.trade) && /Suggestion only, not a sure thing/.test(first.trade), 'it has a trade box with the suggestion-only note: ' + first.trade.slice(0, 160));
  check(first.href === '#exchange/9035', 'the outcome links to its drawer: ' + first.href);
  await page.waitForFunction(() => /^[1-9]$/.test(document.getElementById('nav-count-moves').textContent), null, { timeout: 15000 });
  check(/open lagging alert/.test(await page.textContent('#nav-moves-sr')), 'the nav badge has its screen-reader sentence');
  check(await page.isVisible('#moves-demo-badge'), 'the demo badge is shown on the Outside moves view');
  const venues = await page.$$eval('#moves-body .venue-list li', (els) => els.map((e) => e.textContent.replace(/\s+/g, ' ').trim()));
  check(venues.length === 2 && venues.every((v) => /^Demo venue [AB]: answering · 7 matched/.test(v)), 'both demo venues answer: ' + venues.join(' | '));
  const sentence = (await page.textContent('#moves-body .lag-sentence')).trim();
  check(/^(Only \d+ outside moves? on \d+ races?|No outside move has been (measured|resolved) yet)/.test(sentence) && /far too few|measured yet/.test(sentence), 'a young demo says its sample is small: ' + sentence);
  check(!(await page.$('#moves-body .move-card[data-alert-id^="mv-9038-"], #moves-body .move-card[data-alert-id^="mv-9041-"]')), 'nothing alerts for the thin spike or the one-venue move');
  check((await page.getAttribute('#moves-sound', 'aria-pressed')) === 'false', 'sound is off by default');
  const caveats = await page.$$eval('#moves-body [data-panel="caveats"] li', (els) => els.map((e) => e.textContent));
  check(caveats.length >= 7 && caveats.some((c) => /^Demo data: the outside moves and the Cup's reactions are scripted/.test(c)), 'the caveats include the demo caveat');
  // the drawer opens from the card and closes back to the view
  await page.click('#moves-body .move-card .move-title a');
  await waitDrawerOpen(page);
  check(/New Hampshire Senate/.test(await page.textContent('#drawer-title')), 'the drawer shows the alert\'s outcome');
  await page.keyboard.press('Escape');
  await waitDrawerClosed(page);
  check((await page.evaluate(() => location.hash)) === '#moves', 'closing returns to #moves');
  await page.evaluate(() => { window.scrollTo(0, 0); if (document.activeElement) document.activeElement.blur(); });
}

async function themeAndPersistence(page) {
  await page.click('[data-theme-choice="dark"]');
  check((await page.getAttribute('html', 'data-theme')) === 'dark', 'dark theme applied');
  check((await page.evaluate(() => localStorage.getItem('supermarket-dashboard:theme'))) === 'dark', 'theme persisted');
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  check(bg === 'rgb(13, 13, 13)', 'dark page background, got ' + bg);
}

/** Switch views; for the Outside moves view also wait for its own fresh answer (/api/moves loads only while the
 *  view is shown, so the first render after a switch shows the data from the last visit). */
async function gotoFresh(page, v) {
  const fresh = v === 'moves' ? page.waitForResponse((r) => new URL(r.url()).pathname === '/api/moves' && r.ok(), { timeout: 20000 }) : null;
  await gotoView(page, v);
  if (fresh) {
    await fresh;
    await page.waitForTimeout(300);
  }
  await settleView(page, v);
}

async function darkShots(page) {
  for (const v of VIEWS) {
    await gotoFresh(page, v);
    await shot(page, v + '-dark', { fullPage: v === 'moves' });
  }
  await gotoView(page, 'markets');
  await page.click('#markets-body tr[data-eid="9001"] a.row-link');
  await waitDrawerOpen(page);
  await page.waitForSelector('#drawer table.ladder tbody tr');
  const box = await hitBox(page);
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
  if (v === 'moves') await page.waitForSelector('#moves-body .move-card, #moves-body .move-empty:not([hidden])');
  if (v === 'strategy') await page.waitForSelector('#strategy-body .idea');
  if (v === 'sim') {
    await page.waitForSelector('#sim-body [data-panel="headline"] .verdict-sentence', { timeout: 60000 });
    await page.waitForSelector('#sim-body .sim-chart svg, #sim-body .sim-chart .chart-empty');
  }
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
      moves: { actionable: 'x', actionable_alerts: [EVIL, null, { alert_id: EVIL, headline: EVIL, body: EVIL }] },
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
    '/api/moves': {
      now, enabled: 'yes', available: 1, error: EVIL, demo: EVIL, poll_s: 'x', last_step_at: 'x', steps: 'x', watching: EVIL,
      venues: [null, 'x', { venue: EVIL, label: EVIL, status: EVIL, last_error: EVIL, next_try_at: 'x', matched: 'x', budget_limit: 'x' },
        { venue: 'kalshi', status: 'backoff', last_error: EVIL + ' 429', next_try_at: now + 60 }],
      counts: { open: 'x', lagging: null, suppressed_today: { [EVIL]: 3, thin: 'x' } }, actionable_ids: EVIL,
      alerts: [null, 'x', { alert_id: EVIL, exchange_id: EVIL, title: EVIL, option: EVIL, race_label: EVIL, status: EVIL, status_label: EVIL, state: EVIL,
        reason: EVIL, venues: EVIL, venue_moves: [null, { venue: EVIL, base_value: 'x' }], flags: [EVIL, null], linked: EVIL, cup: EVIL, cup_now: 'x',
        lag: { outcome: 'censored', reason: EVIL, capture_at: 'x' }, trade: EVIL, trade_note: EVIL, detected_at: 'x', window_s: EVIL, direction: EVIL },
      { alert_id: 'mv-x1-1', exchange_id: 'x1', title: EVIL, status: 'lagging', state: 'open', direction: -1, move: 'x', outside_before: EVIL,
        trade: { side: EVIL, text: EVIL, limit: 'x', max_limit: null, shares_at_limit: 'x', note: EVIL, book_source: EVIL, uncertainty: EVIL },
        lag: { outcome: 'pending', t_move: 'x' }, opened_trade: { text: EVIL } },
      { alert_id: 'mv-x2-1', exchange_id: 'x2', status: 'already_moved', state: 'closed', opened_trade: { text: EVIL, edge_per_share: 'x' },
        lag: { outcome: 'followed', lag_s: 'x', lag_after_alert_s: -30, capture_at: now - 10, capture_4m: 'x', exit_at: now, exit_30m: null } }],
      suppressed: [null, { race_label: EVIL, reason: EVIL, reason_label: EVIL, detail: EVIL, at: 'x' }],
      summary: { sentence: EVIL, small_sample: 'x', followed_within: EVIL, followed_within_n: [1, 2], capture_4m: { n: 'x', mean: EVIL, ci90: [EVIL, 1] },
        exit_30m: { n: 3, mean: 0.01, ci90: ['a', 'b'] }, lag_quartiles_s: EVIL, window: EVIL, since: 'x' },
      thresholds: { abs_min: EVIL, min_edge: EVIL, lag_track_s: 'x' }, book_reads: { used: EVIL, limit: 'x' }, caveats: [EVIL, null, 5],
    },
    '/api/paper': {
      enabled: 'yes', available: 1, demo: EVIL, error: EVIL, has_previous: 'yes', table_warning: EVIL, model_label: EVIL,
      run: { run_id: EVIL, steps: 5, hours_run: 'x', target_hours: -1, gaps: 'x', started_at: 'x', capital_source: EVIL, regime: EVIL,
        code_version: EVIL, sizing: EVIL, settings: { params_changed: { [EVIL]: EVIL } }, complete: 'yes' },
      headline: { portfolio_id: EVIL, label: EVIL, pnl_liq: 'x', verdict: { level: EVIL, sentence: EVIL, reasons: [EVIL, null, 4] }, verdict_caveats: EVIL },
      portfolios: [null, 'x', { portfolio_id: EVIL, label: EVIL, verdict: 'x', execution: { [EVIL]: { entries: 'x' } }, no_trade_reason: EVIL + '. ' + EVIL },
        { portfolio_id: 'policy:chaser', policy: 'chaser', sizing: { mode: EVIL, lines: EVIL, bar: 'x', M: 'big' }, verdict: { level: 'promising', exploratory: 'no' } }],
      equity: { [EVIL]: [[1, 'x'], 'bad', null], 'policy:chaser': [[now - 600, 100000], [now - 300, 'x'], [now, 99000]] },
      positions: [null, { portfolio_id: EVIL, exchange_id: EVIL, title: EVIL, option: EVIL, flags: [EVIL, null], qty: 'x', exit_note: EVIL, side: EVIL }],
      baskets: [{ portfolio_id: EVIL, legs: [null, { exchange_id: EVIL, side: EVIL }], sets: 'x' }],
      fills: [{ action: EVIL, side: EVIL, reason: EVIL, title: EVIL, exchange_id: 'x1', qty: 'x', ts: 'x', kind: EVIL }, null],
      trades: [{ exit_reason: EVIL, exchange_ids: EVIL, title: EVIL, kind: EVIL }],
      study: { kinds: { [EVIL]: { horizons: { [EVIL]: { n: 'x' } } }, value: null }, can_show: EVIL, cannot_show: EVIL, pending: EVIL },
      caveats: [EVIL, null],
    },
    '/api/fairvalue': {
      enabled: true, mode: EVIL, last_refresh_at: 'x', providers: [{ name: EVIL, status: EVIL, last_error: EVIL }, null],
      manual: { path: EVIL, exists: true, entries: 'x', errors: [EVIL] }, map: { path: EVIL, overrides: EVIL, errors: EVIL }, counts: 'x',
      rows: [null, { exchange_id: 'x1', title: EVIL, option: EVIL, race_key: EVIL, fair: { value: 'x', source: EVIL, reason: EVIL, usable: 'yes' },
        matches: [{ venue: EVIL, external_id: EVIL, reason: EVIL, label: EVIL }, null], snippets: { disable: EVIL, pin: EVIL, confirm: 5 }, suspect: true, near: 'x' }],
      caveats: [EVIL],
    },
    '/api/backtest': {
      status: 'ready', error: EVIL, generated_at: 'x',
      report: { testability: { [EVIL]: { status: EVIL, sentence: EVIL } }, warnings: [EVIL, null], assumptions: [EVIL], coverage: { exchanges: 'x' },
        portfolios: [{ portfolio_id: EVIL, verdict: { level: EVIL } }], sweep: [{ label: EVIL, params: EVIL }], window: { start: 'x' }, study: 'x', overlap_hours: 'x' },
    },
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
  for (const v of ['markets', 'surges', 'moves', 'high', 'strategy', 'sim']) {
    await gotoView(page, v);
    await page.waitForTimeout(600);
    await safeDom(v);
  }
  await gotoView(page, 'moves');
  for (const f of ['all', 'lagging', 'open', 'all']) await page.click('#moves-body button[data-moves-filter="' + f + '"]');
  await page.click('#moves-filtered-details > summary');
  await page.waitForTimeout(300);
  check((await page.$$('#moves-body .move-card')).length === 3, 'hostile outside-move alerts with an id are listed, junk entries skipped');
  await safeDom('moves details');
  await gotoView(page, 'sim');
  await page.waitForSelector('#sim-body [data-panel="portfolios"]');
  for (const sum of await page.$$('#sim-body details > summary')) await sum.click().catch(() => {});
  await page.click('#sim-body button[data-action="sim-table-toggle"]');
  await page.waitForTimeout(300);
  await safeDom('simulation details');
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
  for (const v of ['markets', 'surges', 'moves', 'high', 'strategy', 'sim']) {
    await gotoFresh(page, v);
    const o = await page.evaluate(() => document.documentElement.scrollWidth - 390);
    check(o <= 1, v + ': no horizontal page scroll at 390 px (overflow ' + o + ' px)');
    if (v === 'moves') {
      // the alerts panel at the top of the phone screen: the controls and the newest alert card
      await page.evaluate(() => document.querySelector('#moves-body [data-panel="alerts"]').scrollIntoView());
      await shot(page, 'moves-390');
    }
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

    await moves(page);
    await shot(page, 'moves-light', { fullPage: true });
    log('outside moves ok');
    await gotoView(page, 'overview');
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
    await simulation(page);
    await page.evaluate(() => { window.scrollTo(0, 0); if (document.activeElement) document.activeElement.blur(); });
    await shot(page, 'sim-light');
    log('simulation ok');
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
