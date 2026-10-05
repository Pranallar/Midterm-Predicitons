/* Super Market dashboard UI (read-only). Vanilla JS, no build step, no external requests.
 *
 * Safety rules this file follows everywhere:
 *   - every string that comes from the API is inserted with textContent, createTextNode or
 *     setAttribute (never innerHTML / insertAdjacentHTML / outerHTML);
 *   - external links are only created for absolute http(s) URLs, open in a new tab and carry
 *     rel="noopener noreferrer";
 *   - no eval / new Function / style attributes (the server's CSP is default-src 'self'); sizes and
 *     positions are set through the CSSOM (element.style.width), which the CSP allows.
 *
 * Data flow: poll() fetches /api/status every 5 s (backing off exponentially while it fails,
 * pausing while the tab is hidden) and starts the active view's endpoints alongside it. Each view
 * endpoint loads on its own: a slow or failing one shows an inline error in its own view and never
 * holds up the status poll or flips the page to Offline (only /api/status failures mean offline).
 * The detail drawer polls /api/exchange/<id> every 10 s while it is open. Renders update the DOM
 * in place so sort, filter, scroll position, text selection and keyboard focus survive each poll.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- theme (before first paint)

  var THEME_KEY = 'supermarket-dashboard:theme';

  function readTheme() {
    try {
      var v = window.localStorage.getItem(THEME_KEY);
      return v === 'light' || v === 'dark' ? v : 'system';
    } catch (e) {
      return 'system';
    }
  }

  function applyTheme(choice) {
    var root = document.documentElement;
    if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
  }

  applyTheme(readTheme());

  // ---------------------------------------------------------------- constants and state

  const POLL_MS = 5000;
  const DRAWER_POLL_MS = 10000;
  const MAX_BACKOFF_MS = 60000;
  const REQUEST_TIMEOUT_MS = 15000;
  const BOOK_PENDING_POLL_MS = 2000; // the server is still fetching the order book: ask again soon
  const ANALYZE_MSG_MS = 60000; // how long a Re-analyze confirmation or error stays on the card
  const VIEWS = ['overview', 'markets', 'surges', 'high', 'strategy', 'sim'];
  const VIEW_TITLES = { overview: 'Overview', markets: 'Markets', surges: 'Surges', high: 'High 90s', strategy: 'Strategy', sim: 'Simulation' };
  const VIEW_DATA = {
    overview: ['strategy', 'surges'],
    markets: ['markets'],
    surges: ['surges'],
    high: ['high'],
    strategy: ['strategy'],
    sim: ['paper', 'fairvalue', 'backtest'],
  };
  const ENDPOINTS = {
    status: '/api/status',
    markets: '/api/markets',
    surges: '/api/surges',
    high: '/api/highband',
    strategy: '/api/strategy',
    paper: '/api/paper',
    fairvalue: '/api/fairvalue',
    backtest: '/api/backtest',
  };
  // Endpoints refreshed less often than the 5 s poll (the backtest and the fair values change slowly).
  const ENDPOINT_EVERY_MS = { fairvalue: 30000, backtest: 30000 };
  const RANGES = { '6h': 6 * 3600, '24h': 24 * 3600, '7d': 7 * 24 * 3600 };
  const RANGE_LABELS = { '6h': '6 hours', '24h': '24 hours', '7d': '7 days' };
  const DATA_LABELS = {
    markets: 'the markets list', surges: 'the surges', high: 'the high-90s list', strategy: 'the strategy ideas',
    paper: 'the simulation', fairvalue: 'the fair values', backtest: 'the backtest',
  };
  const DASH = '—';
  const MINUS = '−';
  const ARROW = '→';
  const HIGH_LINE = 0.95;
  const LOW_LINE = 0.05;
  const EXCHANGE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
  const SVGNS = 'http://www.w3.org/2000/svg';

  const S = {
    view: 'overview',
    viewShown: false,
    data: { status: null, markets: null, surges: null, high: null, strategy: null, paper: null, fairvalue: null, backtest: null },
    offset: 0, // server clock minus browser clock, in seconds
    failures: 0,
    failureText: '',
    nextRetryMs: 0,
    nextRetryAt: 0,
    inflight: {}, // view endpoint name -> true while its request is out
    viewErrors: {}, // view endpoint name -> {message, status, since}
    loadedAt: {}, // view endpoint name -> server time of the last successful load
    triedAt: {}, // view endpoint name -> browser ms of the last request (slow endpoints wait ENDPOINT_EVERY_MS)
    startedAt: null, // the server's started_at: a change means the dashboard restarted
    liveState: null,
    bannerShown: {},
    offlineDismissed: false,
    pollTimer: null,
    polling: false,
    pollAgain: false,
    sort: { key: 'title', dir: 'asc' },
    filter: 'all',
    query: '',
    openIdeas: new Set(),
    analyze: {}, // surge id -> {state, text, at (browser ms), serverAt (server s)}
    analyzeTimers: {},
    sigs: {},
    strategyParts: null,
    strategyIdeas: null,
    pendingReturnKey: null,
    pendingIdea: null, // an idea key to scroll to and focus once the Strategy view has rendered it
    problemsDismissed: null, // the set of problem sources the user hid the problems banner for
    problemsOpen: false, // the problems banner's details are expanded
    announceTimer: null,
    drawer: {
      id: null,
      gen: 0,
      data: null,
      error: null,
      okAt: null, // server time of the last successful drawer load
      timer: null,
      inflight: false,
      inflightGen: null, // the drawer generation the request in flight belongs to
      ctrl: null, // its AbortController
      again: false,
      seeded: null, // the title came from a row already on the page (true) or started as "Loading…" (false)
      known: null, // {title, option} of the outcome from the page's own data, until the detail loads
      staleKind: '',
      pushed: false,
      returnFocus: null,
      returnKey: null,
      range: '24h',
      table: false,
      hoverTs: null,
      sections: null,
      sigs: {},
      chartWidth: 0,
      ro: null,
      headRo: null,
      closing: false,
      scrollY: 0,
      liveTimer: null,
    },
  };

  // ---------------------------------------------------------------- small utilities

  function $(id) {
    return document.getElementById(id);
  }

  function num(v) {
    return typeof v === 'number' && isFinite(v) ? v : null;
  }

  function str(v) {
    return v == null ? '' : String(v);
  }

  function arr(v) {
    return Array.isArray(v) ? v : [];
  }

  function obj(v) {
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  }

  /** The plain objects in a list (drops nulls, numbers, strings and nested arrays). */
  function objs(v) {
    return arr(v).filter(function (x) { return x && typeof x === 'object' && !Array.isArray(x); });
  }

  /** The non-empty texts in a list. */
  function texts(v) {
    return arr(v).filter(function (x) { return x != null && x !== ''; }).map(str);
  }

  function clamp(v, lo, hi) {
    return Math.min(hi, Math.max(lo, v));
  }

  function sig(value) {
    try {
      return JSON.stringify(value);
    } catch (e) {
      return String(Math.random());
    }
  }

  function safe(name, fn) {
    try {
      return fn();
    } catch (e) {
      // A rendering bug must never stop polling; it is reported so tests catch it.
      console.error('dashboard: ' + name + ' failed', e);
      return undefined;
    }
  }

  // ---------------------------------------------------------------- number formatting

  const NUMBER_FORMATS = {};

  function nf(minFrac, maxFrac) {
    const key = minFrac + ':' + maxFrac;
    if (!NUMBER_FORMATS[key]) {
      NUMBER_FORMATS[key] = new Intl.NumberFormat('en-US', { minimumFractionDigits: minFrac, maximumFractionDigits: maxFrac });
    }
    return NUMBER_FORMATS[key];
  }

  function fmtPrice(v) {
    v = num(v);
    return v == null ? DASH : v.toFixed(3);
  }

  function fmtPctOf(v, dp) {
    v = num(v);
    if (v == null) return DASH;
    return (v * 100).toFixed(dp == null ? 1 : dp) + '%';
  }

  function fmtPct0(v) {
    v = num(v);
    return v == null ? DASH : Math.round(v * 100) + '%';
  }

  function fmtSigned(v, dp) {
    v = num(v);
    const d = dp == null ? 3 : dp;
    if (v == null) return DASH;
    const r = Number(v.toFixed(d));
    if (r === 0) return (0).toFixed(d);
    return (r > 0 ? '+' : MINUS) + Math.abs(r).toFixed(d);
  }

  function fmtInt(v) {
    v = num(v);
    return v == null ? DASH : nf(0, 0).format(Math.round(v));
  }

  function fmtMoney(v, dp) {
    v = num(v);
    const d = dp == null ? 2 : dp;
    return v == null ? DASH : nf(d, d).format(v);
  }

  /** A whole number with a unit, or just the dash when the number is missing. */
  function fmtUnit(v, unit) {
    return num(v) == null ? DASH : fmtInt(v) + ' ' + unit;
  }

  function fmtScore(v) {
    v = num(v);
    if (v == null) return DASH;
    if (v === 0) return '0';
    const a = Math.abs(v);
    return a >= 1 ? v.toFixed(2) : Number(v.toPrecision(2)).toString();
  }

  function fmtZ(v) {
    v = num(v);
    return v == null ? DASH : fmtSigned(v, 1);
  }

  function currency() {
    const t = obj(S.data.status && S.data.status.tournament);
    return str(t.currency) || 'SUSQies';
  }

  // ---------------------------------------------------------------- time formatting

  const DT_FULL = new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  });
  const DT_DAY = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
  const DT_DAY_YEAR = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  const DT_TIME = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
  const DT_SHORT = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const DT_TIME_SECONDS = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const DT_SHORT_SECONDS = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const DT_SHORT_YEAR = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

  function nowS() {
    return Date.now() / 1000 + S.offset;
  }

  function toTs(v) {
    if (num(v) != null) return v;
    if (typeof v === 'string' && v.trim()) {
      const t = Date.parse(v);
      return isFinite(t) ? t / 1000 : null;
    }
    return null;
  }

  function dateOf(ts) {
    ts = num(ts);
    if (ts == null || Math.abs(ts) > 8e12) return null;
    const d = new Date(ts * 1000);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmtAbs(ts) {
    const d = dateOf(ts);
    return d ? DT_FULL.format(d) : '';
  }

  function fmtDay(ts) {
    const d = dateOf(ts);
    if (!d) return DASH;
    const sameYear = d.getFullYear() === new Date(nowS() * 1000).getFullYear();
    return (sameYear ? DT_DAY : DT_DAY_YEAR).format(d);
  }

  function fmtShort(ts) {
    const d = dateOf(ts);
    return d ? DT_SHORT.format(d) : DASH;
  }

  /** A compact table time: '9:20:05 AM' today (seconds tell 5 s snapshots apart), else 'Oct 3, 9:20 AM'. */
  function fmtTableTime(ts) {
    const d = dateOf(ts);
    if (!d) return DASH;
    const today = new Date(nowS() * 1000);
    const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
    return sameDay ? DT_TIME_SECONDS.format(d) : DT_SHORT.format(d);
  }

  /** A compact chart readout time: 'Oct 4, 7:25 AM' (the year only when it is not this year; seconds only when asked). */
  function fmtCompact(ts, seconds) {
    const d = dateOf(ts);
    if (!d) return DASH;
    if (d.getFullYear() !== new Date(nowS() * 1000).getFullYear()) return DT_SHORT_YEAR.format(d);
    return (seconds ? DT_SHORT_SECONDS : DT_SHORT).format(d);
  }

  /** A clock time for "the figures are from …": the time today, else the day and time. */
  function fmtClock(ts) {
    const d = dateOf(ts);
    if (!d) return DASH;
    const today = new Date(nowS() * 1000);
    const sameDay = d.getFullYear() === today.getFullYear() && d.getMonth() === today.getMonth() && d.getDate() === today.getDate();
    return sameDay ? DT_TIME.format(d) : DT_SHORT.format(d);
  }

  function fmtSpan(seconds) {
    const a = Math.abs(seconds);
    if (a < 60) return Math.max(1, Math.round(a)) + ' s';
    if (a < 3600) return Math.round(a / 60) + ' min';
    if (a < 86400) {
      const hrs = a / 3600;
      return (hrs < 10 ? Number(hrs.toFixed(1)) : Math.round(hrs)) + ' h';
    }
    const days = a / 86400;
    const v = days < 10 ? Number(days.toFixed(1)) : Math.round(days);
    return v + (v === 1 ? ' day' : ' days');
  }

  function fmtAgo(ts) {
    ts = num(ts);
    if (ts == null) return DASH;
    const d = nowS() - ts;
    if (Math.abs(d) < 5) return 'just now';
    return d > 0 ? fmtSpan(d) + ' ago' : 'in ' + fmtSpan(d);
  }

  function fmtTime(ts, fmt) {
    if (fmt === 'day') return fmtDay(ts);
    if (fmt === 'short') return fmtShort(ts);
    if (fmt === 'clock') return fmtClock(ts);
    return fmtAgo(ts);
  }

  // ---------------------------------------------------------------- DOM helpers (text only)

  function safeExternal(url) {
    if (typeof url !== 'string') return null;
    let u;
    try {
      u = new URL(url.trim());
    } catch (e) {
      return null;
    }
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  }

  function hasId(v) {
    return v != null && v !== '' && (typeof v === 'string' || typeof v === 'number');
  }

  function exchangeHref(id) {
    return '#exchange/' + encodeURIComponent(String(id));
  }

  function append(node, children) {
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false) continue;
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Create an element. props: class, text, dataset, on<event>, href (internal "#..." or http(s)), other attributes. */
  function h(tag, props) {
    const node = document.createElement(tag);
    if (props) {
      for (const key of Object.keys(props)) {
        const v = props[key];
        if (v == null || v === false) continue;
        if (key === 'class') node.className = v;
        else if (key === 'text') node.textContent = String(v);
        else if (key === 'dataset') Object.assign(node.dataset, v);
        else if (key.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(key.slice(2), v);
        else if (key === 'href') {
          const href = String(v);
          const ok = href.charAt(0) === '#' ? href : safeExternal(href);
          if (ok) node.setAttribute('href', ok);
        } else node.setAttribute(key, v === true ? '' : String(v));
      }
    }
    return append(node, Array.prototype.slice.call(arguments, 2));
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        if (attrs[key] != null) node.setAttribute(key, String(attrs[key]));
      }
    }
    return append(node, Array.prototype.slice.call(arguments, 2));
  }

  function setText(node, text) {
    if (!node) return;
    const t = String(text);
    if (node.textContent !== t) node.textContent = t;
  }

  function setAttr(node, name, value) {
    if (!node) return;
    if (value == null) {
      if (node.hasAttribute(name)) node.removeAttribute(name);
    } else if (node.getAttribute(name) !== String(value)) node.setAttribute(name, String(value));
  }

  function show(node, visible) {
    if (node && node.hidden === !!visible) node.hidden = !visible;
  }

  /** Replace a container's children, keeping keyboard focus on the element with the same data-focus-key. */
  function rebuild(container, children) {
    const active = document.activeElement;
    const key = active && container.contains(active) ? active.getAttribute('data-focus-key') : null;
    container.replaceChildren.apply(container, arr(children).flat(Infinity).filter(Boolean));
    if (key) {
      const target = container.querySelector('[data-focus-key="' + CSS.escape(key) + '"]');
      if (target) target.focus({ preventScroll: true });
    }
  }

  /** Say a short message politely to screen readers (inside the drawer while it is open: a modal
   *  dialog makes live regions outside it inert). Re-saying the same text still announces it. */
  function announce(text) {
    const region = S.drawer.id && $('drawer').open ? $('drawer-announcer') : $('announcer');
    if (!region || !text) return;
    region.textContent = '';
    setTimeout(function () { region.textContent = String(text); }, 60);
  }

  /** True while the user has text selected inside node: rebuilding it would wipe the selection. */
  function selectionInside(node) {
    const sel = window.getSelection ? window.getSelection() : null;
    if (!node || !sel || sel.isCollapsed || !sel.rangeCount) return false;
    const range = sel.getRangeAt(0);
    try {
      return range.intersectsNode(node);
    } catch (e) {
      return false;
    }
  }

  function timeEl(ts, fmt, cls) {
    ts = num(ts);
    const el = h('time', { class: cls });
    const d = dateOf(ts);
    if (!d) {
      el.textContent = DASH;
      return el;
    }
    el.dateTime = d.toISOString();
    el.title = fmtAbs(ts);
    el.dataset.ts = String(ts);
    el.dataset.fmt = fmt || 'ago';
    el.textContent = fmtTime(ts, fmt || 'ago');
    return el;
  }

  function refreshTimes(root) {
    const list = (root || document).querySelectorAll('time[data-fmt="ago"]');
    for (const el of list) {
      const ts = Number(el.dataset.ts);
      if (isFinite(ts)) setText(el, fmtAgo(ts));
    }
  }

  // ---------------------------------------------------------------- icons (constant path data)

  const ICONS = {
    lock: [['rect', { x: 3, y: 7, width: 10, height: 7, rx: 1.5 }], ['path', { d: 'M5.5 7V5a2.5 2.5 0 0 1 5 0v2' }]],
    bolt: [['path', { class: 'f', d: 'M9.5 1.5L3.5 9h4l-1 5.5 6-7.5h-4z' }]],
    peak: [['path', { d: 'M2.5 12.5h11M4 9.5l4-6 4 6z' }]],
    news: [['rect', { x: 2, y: 2.5, width: 12, height: 11, rx: 1.5 }], ['path', { d: 'M4.5 5.5h7M4.5 8h7M4.5 10.5h4' }]],
    people: [
      ['circle', { class: 'f', cx: 5.5, cy: 5, r: 2.2 }],
      ['circle', { class: 'f', cx: 11, cy: 5.5, r: 1.8 }],
      ['path', { class: 'f', d: 'M1.5 13.5c0-2.6 1.8-4.3 4-4.3s4 1.7 4 4.3zM10 13.5c0-1.5-.4-2.7-1.2-3.6.6-.4 1.3-.6 2.2-.6 1.9 0 3.5 1.5 3.5 4.2z' }],
    ],
    question: [['circle', { cx: 8, cy: 8, r: 6.5 }], ['path', { d: 'M6 6.2a2 2 0 1 1 2.8 1.8c-.6.3-.8.7-.8 1.3v.4M8 11.6v.3' }]],
    check: [['path', { d: 'M3 8.5l3.2 3L13 4.5' }]],
    checkCircle: [['circle', { cx: 8, cy: 8, r: 6.5 }], ['path', { d: 'M5 8.3l2 2 4-4.3' }]],
    cross: [['path', { d: 'M4 4l8 8M12 4l-8 8' }]],
    warn: [['path', { d: 'M8 1.5l7 13H1z' }], ['path', { d: 'M8 6v4M8 12v.3' }]],
    alert: [['circle', { cx: 8, cy: 8, r: 7 }], ['path', { d: 'M8 4.5v4.5M8 11.2v.3' }]],
    info: [['circle', { cx: 8, cy: 8, r: 7 }], ['path', { d: 'M8 7v4.5M8 4.5v.3' }]],
    clock: [['circle', { cx: 8, cy: 8, r: 6.5 }], ['path', { d: 'M8 4.5V8l2.5 1.5' }]],
    pulse: [['path', { d: 'M1.5 8.5h3l2-4 3 7 2-3h3' }]],
    undo: [['path', { d: 'M5.5 3.5l-3 3 3 3' }], ['path', { d: 'M2.5 6.5h7a4 4 0 0 1 0 8H7' }]],
    pin: [['path', { d: 'M8 14.5v-4' }], ['path', { d: 'M4.5 10.5h7L10 7V3h1V1.5H5V3h1v4z' }]],
    refresh: [['path', { d: 'M13.5 2.5v3.5H10' }], ['path', { d: 'M13.3 6A5.5 5.5 0 1 0 13.5 9.5' }]],
    external: [['path', { d: 'M9.5 2.5h4v4M13.5 2.5L8 8M11.5 9.5v3a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3' }]],
    shield: [['path', { d: 'M8 1.5l5.5 2v4c0 3.5-2.4 5.9-5.5 7-3.1-1.1-5.5-3.5-5.5-7v-4z' }], ['path', { d: 'M5.5 8l1.8 1.8L10.8 6' }]],
    scale: [['path', { d: 'M8 2v12M4.5 14h7M2.5 4.5h11M4.5 4.5l-2 5a2 2 0 0 0 4 0zM11.5 4.5l-2 5a2 2 0 0 0 4 0z' }]],
    flame: [['path', { d: 'M8 1.5c.5 2.5 4.5 4.5 4.5 8a4.5 4.5 0 0 1-9 0c0-2 1-3.2 2-4 .2 1.5.8 2.3 1.6 2.6C7 6 6.8 4 8 1.5z' }]],
    fade: [['path', { d: 'M2 4.5l4 4 2.5-2.5 5.5 5.5' }], ['path', { d: 'M10 11.5h4v-4' }]],
    carry: [['path', { d: 'M2.5 11.5l3-3 2.5 2 5.5-6' }], ['path', { d: 'M10 4.5h3.5V8' }]],
    arb: [['path', { d: 'M2.5 5.5h10.5L10.5 3M13.5 10.5H3L5.5 13' }]],
    eye: [['path', { d: 'M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z' }], ['circle', { cx: 8, cy: 8, r: 2 }]],
    table: [['rect', { x: 2, y: 3, width: 12, height: 10, rx: 1 }], ['path', { d: 'M2 6.5h12M2 9.8h12M6.5 3v10' }]],
    chart: [['path', { d: 'M2 13.5h12M3 10.5l3-3 2.5 2L13 4' }]],
    calendar: [['rect', { x: 2, y: 3, width: 12, height: 11, rx: 1.5 }], ['path', { d: 'M2 6.5h12M5.5 1.5v3M10.5 1.5v3' }]],
    chevron: [['path', { d: 'M6 3.5l4.5 4.5L6 12.5' }]],
    closed: [['circle', { cx: 8, cy: 8, r: 6.5 }], ['path', { d: 'M5 8h6' }]],
    target: [['circle', { cx: 8, cy: 8, r: 6.5 }], ['circle', { cx: 8, cy: 8, r: 3 }], ['circle', { class: 'f', cx: 8, cy: 8, r: 1 }]],
    basket: [['path', { d: 'M1.8 6.5h12.4l-1.6 7H3.4z' }], ['path', { d: 'M5 6.5l2.2-4.5M11 6.5L8.8 2M6 9v2.5M10 9v2.5' }]],
    hole: [['path', { d: 'M8 1.8v7.4M5.2 6.6L8 9.4l2.8-2.8' }], ['path', { d: 'M2.5 11v2.5h11V11' }]],
    trendDown: [['path', { d: 'M2.5 4.5l3 3 2.5-2 5.5 6' }], ['path', { d: 'M10 11.5h3.5V8' }]],
    copy: [['rect', { x: 5.5, y: 5.5, width: 8, height: 8, rx: 1.5 }], ['path', { d: 'M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2' }]],
  };

  function icon(name, cls) {
    const svg = svgEl('svg', { class: 'icon' + (cls ? ' ' + cls : ''), viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false' });
    for (const part of ICONS[name] || ICONS.info) svg.appendChild(svgEl(part[0], part[1]));
    return svg;
  }

  // ---------------------------------------------------------------- shared visual pieces

  function priceEl(v, missing) {
    v = num(v);
    if (v == null) return h('span', { class: 'nil', title: missing || 'No price yet' }, h('span', { 'aria-hidden': 'true' }, DASH), h('span', { class: 'sr-only' }, missing || 'No price yet'));
    return h('span', { class: 'tabular', title: fmtPctOf(v) + ' implied chance' }, fmtPrice(v));
  }

  /** The last tournament trade, or a dash: a bid/ask mid is never presented as a traded price. */
  function lastTradeEl(v) {
    return priceEl(v, 'No tournament trades yet');
  }

  function deltaEl(v, big) {
    v = num(v);
    if (v == null) return h('span', { class: 'nil', title: 'Not enough history for this window' }, DASH);
    const r = Number(v.toFixed(3));
    const dir = r > 0 ? 'up' : r < 0 ? 'down' : 'flat';
    const threshold = big == null ? 0.05 : big;
    const el = h('span', {
      class: 'delta ' + dir + (Math.abs(r) >= threshold ? ' big' : ''),
      title: fmtSigned(v * 100, 1) + ' percentage points',
    });
    el.appendChild(h('span', { class: 'arrow', 'aria-hidden': 'true' }, dir === 'up' ? '▲' : dir === 'down' ? '▼' : '●'));
    el.appendChild(document.createTextNode(fmtSigned(v)));
    return el;
  }

  const VERDICTS = {
    news: { key: 'news', label: 'News', icon: 'news', help: 'News: a matching headline explains the move, so it will probably hold' },
    participants: {
      key: 'participants', label: 'Participants', icon: 'people',
      help: 'Participants: a few Cup traders moved the price without news, so it often gives part of the move back',
    },
    unclear: { key: 'unclear', label: 'Unclear', icon: 'question', help: 'Unclear: the evidence is mixed or weak' },
  };

  function analysisEnabled() {
    const f = obj(S.data.status && S.data.status.features);
    return f.analysis !== false;
  }

  function newsEnabled() {
    const f = obj(S.data.status && S.data.status.features);
    return f.news !== false;
  }

  function verdictBadge(att) {
    if (!att) {
      if (!analysisEnabled()) return h('span', { class: 'verdict verdict-unclear' }, icon('question'), 'Not analysed');
      return h('span', { class: 'verdict verdict-pending', title: 'The bot is checking news and the trade tape' },
        h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Analyzing…');
    }
    const v = VERDICTS[str(att.verdict)] || VERDICTS.unclear;
    const badge = h('span', { class: 'verdict verdict-' + v.key, title: v.help }, icon(v.icon), v.label);
    const conf = num(att.confidence);
    if (conf != null) {
      badge.appendChild(h('span', { class: 'verdict-conf' }, Math.round(conf * 100) + '%', h('span', { class: 'sr-only' }, ' confidence')));
    }
    return badge;
  }

  function statusBadge(sv) {
    const st = str(sv.status);
    const rf = num(sv.reverted_fraction);
    let extra = '';
    if (rf != null) extra = rf >= 0 ? ' · ' + Math.round(rf * 100) + '% reverted' : ' · extended ' + Math.round(-rf * 100) + '%';
    const map = {
      open: ['pulse', 'Open', 'Still moving or recent: the 24 hour watch is on'],
      reverted: ['undo', 'Reverted', 'Half or more of the move has come back'],
      held: ['pin', 'Held', 'The move has held for 24 hours'],
      closed: ['closed', 'Market closed', 'The market has closed or settled: this surge can no longer be traded'],
    };
    if (st === 'closed') extra = '';
    const m = map[st] || ['question', st ? st.charAt(0).toUpperCase() + st.slice(1) : 'Unknown', ''];
    return h('span', { class: 'status-badge status-' + (map[st] ? st : 'unknown'), title: m[2] || null }, icon(m[0]), m[1] + extra);
  }

  const KINDS = {
    value: ['target', 'Value', 'The Cup price differs from the outside fair value'],
    basket: ['basket', 'Basket', 'Outcomes that cannot all win are priced above 1 (or below 1): a set that pays whatever happens at a 1/0 settlement'],
    hole: ['hole', 'Hole', 'A resting order far below a liquid favourite, to catch a liquidity hole'],
    fade: ['fade', 'Fade', 'Bet that a participant-driven move gives part of itself back'],
    carry: ['carry', 'Carry', 'Buy a stable high-90s favourite and hold it to settlement'],
    arbitrage: ['arb', 'Arbitrage', 'Prices that do not add up: a near-riskless set of trades'],
    watch: ['eye', 'Watch', 'Unclear move: wait for confirmation before trading'],
  };

  function kindBadge(kind) {
    const k = KINDS[str(kind)] || ['info', str(kind) || 'Idea', ''];
    return h('span', { class: 'kind kind-' + str(kind), title: k[2] || null }, icon(k[0]), k[1]);
  }

  function flagBadges(row) {
    const box = h('span', { class: 'flags' });
    if (row.surge_id != null) box.appendChild(h('span', { class: 'flag flag-surge', title: 'An open surge on this outcome' }, icon('bolt'), 'Surge'));
    if (row.high_band) box.appendChild(h('span', { class: 'flag flag-high', title: 'Favourite side at 0.95 or more' }, icon('peak'), 'High 90s'));
    if (!box.childNodes.length) box.appendChild(h('span', { class: 'nil' }, h('span', { 'aria-hidden': 'true' }, DASH), h('span', { class: 'sr-only' }, 'None')));
    return box;
  }

  function yesNo(value, yesText, noText, unknownText) {
    if (value === true) return h('span', { class: 'yes-no is-yes' }, icon('checkCircle'), yesText);
    if (value === false) return h('span', { class: 'yes-no is-no' }, icon('warn'), noText);
    return h('span', { class: 'yes-no is-unknown' }, icon('question'), unknownText || 'Unknown');
  }

  function factList(items, cls) {
    const dl = h('dl', { class: 'facts' + (cls ? ' ' + cls : '') });
    for (const item of items) {
      if (!item) continue;
      dl.appendChild(h('div', { class: item[3] || null }, h('dt', null, item[0]), h('dd', { title: item[2] || null }, item[1])));
    }
    return dl;
  }

  function emptyNote(text, extra) {
    return h('p', { class: 'empty' }, text, extra || null);
  }

  function cupEnd() {
    return num(S.data.status && S.data.status.cup_end);
  }

  function outcomesWatched() {
    const c = obj(S.data.status && S.data.status.counts);
    return num(c.outcomes);
  }

  function trackerStarted() {
    const t = obj(S.data.status && S.data.status.tracker);
    return (num(t.cycles) || 0) > 0;
  }

  /** Set while the tracker has stopped for good (for example a revoked key): nothing new will arrive. */
  function trackerStoppedMsg() {
    const t = obj(S.data.status && S.data.status.tracker);
    return str(t.fatal_error)
      ? 'Tracking has stopped (see the red banner above): no new prices, surges or ideas until you restart the dashboard.'
      : '';
  }

  // The data a current problem is about: these decide what the empty states and banners say.
  const PROBLEM_KINDS = {
    prices: 'prices', snapshot: 'prices', snapshots: 'prices', price_snapshot: 'prices', price_snapshots: 'prices',
    history: 'history', backfill: 'history', price_history: 'history',
    markets: 'markets', market_list: 'markets', exchanges: 'markets',
    storage: 'storage', tracker: 'tracker',
  };

  function problemKind(p) {
    return PROBLEM_KINDS[str(p && p.source).trim().toLowerCase().replace(/[\s-]+/g, '_')] || '';
  }

  function findProblem(kind) {
    for (const p of currentProblems(S.data.status)) if (problemKind(p) === kind) return p;
    return null;
  }

  /** A problem's message without the request line ("(GET /exchanges/…)"), clipped to one short sentence. */
  function shortProblem(message) {
    let t = str(message).trim().replace(/\s*\((?:GET|POST|PUT|PATCH|DELETE|HEAD)\s+[^)]*\)\s*\.?\s*$/i, '');
    if (t.length > 160) t = t.slice(0, 159).replace(/\s+\S*$/, '') + '…';
    return sentence(t, 'not available.');
  }

  /** The market-list failure while the list has never loaded (so the views have nothing to show). */
  function marketListProblem() {
    const st = S.data.status;
    if (!st) return null;
    const p = findProblem('markets');
    if (!p) return null;
    const tr = obj(st.tracker);
    return tr.markets_updated_at == null || !(outcomesWatched() > 0) ? p : null;
  }

  /** The bulk-price failure while no price snapshot has ever been taken. */
  function pricesNeverLoaded() {
    const st = S.data.status;
    if (!st) return null;
    const p = findProblem('prices');
    if (!p) return null;
    return num(obj(st.tracker).last_snapshot_at) == null || !(outcomesWatched() > 0) ? p : null;
  }

  /** Why a view has nothing to show although the tournament has markets: the tracker stopped, or
   *  the market list / prices have never loaded. Empty when none of these applies. */
  function noDataMsg() {
    const stopped = trackerStoppedMsg();
    if (stopped) return stopped;
    const m = marketListProblem();
    if (m) return 'Can’t load the market list yet: ' + shortProblem(m.message) + ' The bot keeps retrying.';
    const p = pricesNeverLoaded();
    if (p) return 'Can’t load prices yet: ' + shortProblem(p.message) + ' The bot keeps retrying.';
    return '';
  }

  /** Surge and high-90s detection run on live prices only (price history unavailable, off or
   *  still loading): {problem, live, waiting}, or null when history is there. */
  function historyLimit() {
    const st = S.data.status;
    if (!st) return null;
    const det = obj(st.detection);
    const p = findProblem('history');
    const live = num(det.live_only) || 0;
    const waiting = num(det.waiting_for_history) || 0;
    if (!p && live <= 0 && waiting <= 0) return null;
    return { problem: p, live: live, waiting: waiting };
  }

  /** "price history is unavailable (HTTP 503 …)" / "is still loading" / "is not available". */
  function historyWhy(lim) {
    if (lim.problem) return 'price history is unavailable (' + shortProblem(lim.problem.message).replace(/\.$/, '') + ')';
    if (lim.waiting > 0 && lim.live <= 0) return 'price history is still loading';
    return 'price history is not available';
  }

  /** A row's current price for "how many sit at 0.95+ or 0.05-": the mark, else the last trade, else the mid. */
  function rowPrice(r) {
    const m = num(r.mark);
    if (m != null) return m;
    const l = num(r.last);
    if (l != null) return l;
    const b = num(r.bid), a = num(r.ask);
    return b != null && a != null ? (a + b) / 2 : null;
  }

  function nearHighCount() {
    const d = S.data.markets;
    if (!d) return null;
    return objs(d.rows).filter(function (r) {
      const p = rowPrice(r);
      return p != null && (p >= HIGH_LINE - 1e-9 || p <= LOW_LINE + 1e-9);
    }).length;
  }

  // ---------------------------------------------------------------- networking

  function httpError(message, status, body) {
    const err = new Error(message);
    err.status = status || 0;
    err.body = body || null;
    return err;
  }

  /** GET JSON with a timeout; `signal` (optional) lets the caller abandon the request early. */
  async function getJSON(path, signal) {
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    const onAbort = function () { ctrl.abort(); };
    if (signal) {
      if (signal.aborted) ctrl.abort();
      else signal.addEventListener('abort', onAbort);
    }
    let resp;
    try {
      resp = await fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin', signal: ctrl.signal });
    } catch (e) {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
      if (signal && signal.aborted) throw httpError('The request was cancelled.', 0);
      throw httpError(e && e.name === 'AbortError' ? 'The request timed out.' : 'The dashboard server did not answer.', 0);
    }
    if (signal) signal.removeEventListener('abort', onAbort);
    let body = null;
    try {
      body = await resp.json();
    } catch (e) {
      body = null;
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok) throw httpError(str(body && body.error) || 'HTTP ' + resp.status, resp.status, body);
    if (body == null || typeof body !== 'object') throw httpError('The server sent an unreadable answer.', resp.status);
    return body;
  }

  function sentence(text, fallback) {
    let t = str(text).trim() || fallback || '';
    if (t && !/[.!?…]$/.test(t)) t += '.';
    return t;
  }

  function noteFailure(err) {
    S.failures += 1;
    S.failureText = sentence(err && err.message, 'The dashboard server did not answer.');
    S.nextRetryMs = Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, S.failures));
  }

  function noteSuccess() {
    if (S.failures > 0) {
      // Back from an outage: view errors recorded during it belong to the outage (the Offline banner
      // said so), not to one view. They stay hidden until that view's next load settles.
      for (const k of Object.keys(S.viewErrors)) S.viewErrors[k].afterOutage = true;
    }
    S.failures = 0;
    S.failureText = '';
    S.offlineDismissed = false;
  }

  /** Any successful answer means the server is back: while offline, re-poll the status at once
   *  instead of waiting out the backoff (which can reach a minute). */
  function serverAnswered() {
    if (!isOffline() || S.polling) return;
    // at most one early re-poll every 3 s: when only /api/status fails, every successful view load
    // would otherwise start the next status request at once (a request storm); within the 3 s the
    // retry moves up to the end of the window instead of waiting out the backoff
    const since = S.answeredPollAt ? Date.now() - S.answeredPollAt : Infinity;
    if (since < 3000) {
      if (!S.nextRetryAt || S.nextRetryAt > S.answeredPollAt + 3000) {
        schedule(3000 - since);
        S.nextRetryAt = S.answeredPollAt + 3000;
      }
      return;
    }
    S.answeredPollAt = Date.now();
    S.nextRetryAt = 0;
    clearTimeout(S.pollTimer);
    S.pollTimer = null;
    poll();
  }

  function schedule(ms) {
    clearTimeout(S.pollTimer);
    S.pollTimer = null;
    if (document.hidden) return;
    S.pollTimer = setTimeout(poll, ms);
  }

  function isOffline() {
    return S.failures > 0;
  }

  async function poll() {
    if (S.polling) {
      S.pollAgain = true;
      return;
    }
    clearTimeout(S.pollTimer);
    S.pollTimer = null;
    if (document.hidden) return;
    S.polling = true;
    // The view's own endpoints load alongside, each on its own: a slow or failing one never holds
    // up the status poll, and only a failing /api/status means the dashboard is offline.
    for (const name of viewNeeds(S.view)) loadView(name);
    let failed = null;
    try {
      const st = await getJSON(ENDPOINTS.status);
      S.data.status = st;
      if (num(st.now) != null) S.offset = st.now - Date.now() / 1000;
      noteRestart(st);
    } catch (e) {
      // Any status failure counts (for example 421 when the page was opened at an address the
      // server does not accept): the banner then shows the server's own explanation.
      failed = e;
    } finally {
      S.polling = false;
    }
    if (failed) noteFailure(failed);
    else noteSuccess();
    renderAll();
    if (S.pollAgain) {
      S.pollAgain = false;
      poll();
      return;
    }
    schedule(failed ? S.nextRetryMs : POLL_MS);
    S.nextRetryAt = failed ? Date.now() + S.nextRetryMs : 0;
    if (failed) renderOfflineCountdown();
  }

  /** The endpoints a view loads: its own, plus the markets list while the High 90s view has to
   *  explain an empty list ("N outcomes are at 0.95+ right now"). */
  function viewNeeds(view) {
    const base = VIEW_DATA[view] || [];
    if (view === 'high' && historyLimit() && !(S.data.high && objs(S.data.high.bands).length)) return base.concat(['markets']);
    return base;
  }

  /** Fetch one view endpoint; on failure keep the last good data and remember the error for that view only. */
  async function loadView(name, force) {
    if (S.inflight[name] || !ENDPOINTS[name]) return;
    const every = ENDPOINT_EVERY_MS[name];
    if (every && !force && S.triedAt[name] != null && Date.now() - S.triedAt[name] < every) return;
    S.triedAt[name] = Date.now();
    S.inflight[name] = true;
    let ok = false;
    try {
      S.data[name] = await getJSON(ENDPOINTS[name]);
      S.loadedAt[name] = nowS();
      delete S.viewErrors[name];
      ok = true;
    } catch (e) {
      const prev = S.viewErrors[name];
      S.viewErrors[name] = { message: sentence(e && e.message, 'The request failed.'), status: (e && e.status) || 0, since: prev && !prev.afterOutage ? prev.since : nowS() };
    } finally {
      S.inflight[name] = false;
    }
    if (ok) serverAnswered();
    // A failure while the status request is still out waits for it: if the whole server is down,
    // the Offline banner says so and no per-view error is shown or announced.
    if (!ok && S.polling) return;
    if (viewNeeds(S.view).indexOf(name) !== -1) {
      safe('view', function () { renderView(S.view); });
      refreshTimes(document);
    }
  }

  /** The server restarted (its started_at changed): surge ids restart at 1, so per-surge notes are dropped. */
  function noteRestart(st) {
    const started = num(st && st.started_at);
    if (started == null) return;
    if (S.startedAt != null && started !== S.startedAt) {
      for (const id of Object.keys(S.analyzeTimers)) clearTimeout(S.analyzeTimers[id]);
      S.analyze = {};
      S.analyzeTimers = {};
    }
    S.startedAt = started;
  }

  /** Abandon the drawer's request in flight (another outcome was opened, or the drawer closed):
   *  the new outcome must not wait for the old answer, and that answer is ignored. */
  function abortDrawerRequest() {
    const D = S.drawer;
    if (D.ctrl) D.ctrl.abort();
    D.ctrl = null;
    D.inflight = false;
    D.inflightGen = null;
    D.again = false;
  }

  async function pollDrawer() {
    const D = S.drawer;
    if (!D.id || document.hidden) return;
    if (D.inflight && D.inflightGen === D.gen) {
      D.again = true;
      return;
    }
    clearTimeout(D.timer);
    D.timer = null;
    const gen = D.gen;
    const id = D.id;
    const ctrl = new AbortController();
    const firstLoad = !D.data;
    D.ctrl = ctrl;
    D.inflight = true;
    D.inflightGen = gen;
    let ok = false;
    try {
      if (!EXCHANGE_ID_RE.test(id)) throw httpError('Unknown exchange.', 404);
      const data = await getJSON('/api/exchange/' + encodeURIComponent(id), ctrl.signal);
      if (gen === D.gen) {
        D.data = data;
        D.error = null;
        D.okAt = num(data.now) != null ? data.now : nowS();
        ok = true;
      }
    } catch (e) {
      if (gen === D.gen) D.error = e;
    } finally {
      if (D.inflightGen === gen) {
        D.inflight = false;
        D.inflightGen = null;
        D.ctrl = null;
      }
    }
    if (gen !== D.gen) return; // an abandoned request: the drawer now open has its own
    if (ok) serverAnswered();
    safe('drawer', renderDrawer);
    if (ok && firstLoad && D.seeded === false) {
      // The dialog opened as "Loading…": say what it shows now that the title is known.
      const row = drawerRow();
      announce(outcomeLabel(row.title, str(row.option)));
    }
    if (D.again) {
      D.again = false;
      pollDrawer();
      return;
    }
    // While the server is still fetching the order book (book_pending), ask again soon.
    const pending = !!(D.data && D.data.book_pending && !D.data.book && !D.error);
    if (D.id && !document.hidden) D.timer = setTimeout(pollDrawer, pending ? BOOK_PENDING_POLL_MS : DRAWER_POLL_MS);
  }

  // ---------------------------------------------------------------- render: shell

  function renderAll() {
    safe('header', renderHeader);
    safe('banners', renderBanners);
    safe('nav', renderNav);
    safe('view', function () { renderView(S.view); });
    // the drawer's own warning follows the tracker too (stopped / stale), not only its fetches
    if (S.drawer.id && S.drawer.sections) safe('drawer-stale', function () { renderDrawerStale(S.drawer.error); });
    refreshTimes(document);
  }

  function renderView(view) {
    safe('view-errors', function () { renderViewErrors(view); });
    if (view === 'overview') renderOverview();
    else if (view === 'markets') renderMarkets();
    else if (view === 'surges') renderSurges();
    else if (view === 'high') renderHigh();
    else if (view === 'strategy') renderStrategy();
    else if (view === 'sim') renderSim();
  }

  /** The error to show for a view endpoint (none while the whole dashboard is offline: the banner
   *  says it; nor one left over from that outage while the view's first reload is still out). */
  function viewError(name) {
    if (isOffline()) return null;
    const e = S.viewErrors[name];
    return e && !e.afterOutage ? e : null;
  }

  /** The empty-state text while a view endpoint has no data yet: loading, or the reason it failed. */
  function loadingMsg(name, text) {
    return viewError(name) ? 'Could not load ' + DATA_LABELS[name] + '. Retrying every ' + retryEvery(name) + '.' : text;
  }

  /** How often a view endpoint is asked again ("5 s"; "30 s" for the slow simulation endpoints). */
  function retryEvery(name) {
    return fmtSpan((ENDPOINT_EVERY_MS[name] || POLL_MS) / 1000);
  }

  /** Inline errors at the top of a view: one line per failing endpoint, the rest of the page is unaffected. */
  function renderViewErrors(view) {
    const box = $('view-errors-' + view);
    if (!box) return;
    const names = (VIEW_DATA[view] || []).filter(function (n) { return viewError(n); });
    const key = sig(names.map(function (n) { return [n, S.viewErrors[n].message, !!S.data[n]]; }));
    if (box.dataset.sig === key) return;
    const wasShown = !box.hidden;
    box.dataset.sig = key;
    show(box, names.length > 0);
    rebuild(box, names.map(function (n) {
      const e = S.viewErrors[n];
      const has = !!S.data[n] && S.loadedAt[n] != null;
      return h('p', { class: 'inline-error' }, icon('warn'),
        h('span', null,
          h('strong', null, (has ? 'Could not refresh ' : 'Could not load ') + DATA_LABELS[n] + ': '),
          e.message,
          has ? [' Showing what was loaded ', timeEl(S.loadedAt[n], 'ago'), '.'] : null,
          ' Retrying every ' + retryEvery(n) + '.'));
    }));
    if (names.length && !wasShown) announce('Could not refresh ' + names.map(function (n) { return DATA_LABELS[n]; }).join(' or ') + '.');
  }

  const LIVE_SENTENCES = {
    fresh: 'Prices are live.',
    stale: 'Prices are stale: no new price snapshot recently.',
    offline: 'Offline: the dashboard server is not answering.',
    stopped: 'The tracker has stopped. Prices are no longer updating.',
    loading: 'Waiting for the first price snapshot.',
  };

  function liveState() {
    if (isOffline()) return { state: 'offline', label: 'Offline', title: 'The dashboard server is not answering' };
    const st = S.data.status;
    if (!st) return { state: 'loading', label: 'Connecting…', title: 'Waiting for the dashboard server' };
    const tr = obj(st.tracker);
    const interval = num(st.interval) || num(tr.interval) || 30;
    if (tr.fatal_error) return { state: 'stopped', label: 'Stopped', title: 'The tracker stopped: see the message below' };
    const last = num(tr.last_snapshot_at);
    if (st.view_error) {
      // Snapshots may still be saved, but the views are frozen on the last data they had.
      return {
        state: 'stale', label: 'Stale', kind: 'view',
        title: 'Snapshots are still being saved, but the live view could not be built: showing the last data it had',
        sentence: 'Prices are stale: the live view could not be built, so the page shows the last data it had.',
      };
    }
    if (last == null) return { state: 'loading', label: 'Starting…', title: 'Waiting for the first price snapshot' };
    // The snapshot's age when the status was fetched: between polls the server has newer snapshots
    // we have not seen yet, so ticking this against the browser clock would flicker Live/Stale.
    const age = (num(st.now) != null ? st.now : nowS()) - last;
    if (age < 2 * interval) {
      return { state: 'fresh', label: 'Live', title: 'Prices refresh every ' + fmtSpan(interval) + '; the last snapshot was ' + fmtAgo(last) };
    }
    return { state: 'stale', label: 'Stale', age: age, title: 'No new price snapshot for ' + fmtSpan(age) + ' (expected every ' + fmtSpan(interval) + ')' };
  }

  /** The read-budget meter: usage, or a paused / rate-limited state when the API is throttling us. */
  function budgetState(st) {
    const tr = obj(st.tracker);
    const rb = obj(tr.read_budget);
    const now = nowS();
    const pausedFor = num(rb.paused_for);
    const pausedUntil = [rb.paused_until, rb.retry_at, rb.blocked_until, tr.rate_limited_until, pausedFor > 0 ? now + pausedFor : null]
      .map(num).filter(function (v) { return v != null && v > now; });
    const limited = pausedUntil.length > 0 || currentProblems(st).some(function (p) { return p.rateLimit; });
    return { used: num(rb.used), limit: num(rb.limit), limited: limited, until: pausedUntil.length ? Math.max.apply(null, pausedUntil) : null };
  }

  function renderHeader() {
    const st = S.data.status;
    const live = liveState();
    const liveEl = $('live');
    setAttr(liveEl, 'data-state', live.state);
    // The tooltip sits on the (aria-hidden) dot and label, never on the status region itself: a
    // region whose name changed on every poll ("… was 9 s ago") would be re-spoken each time.
    setAttr(liveEl, 'title', null);
    setAttr($('live-label'), 'title', live.title);
    setAttr(liveEl.querySelector('.live-dot'), 'title', live.title);
    setText($('live-label'), live.label);
    // The status region speaks only when the state changes (never on a plain poll).
    if (S.liveState !== live.state) {
      const recovered = live.state === 'fresh' && ['stale', 'offline', 'stopped'].indexOf(S.liveState) !== -1;
      const said = recovered ? 'Prices are live again.' : live.sentence || LIVE_SENTENCES[live.state] || live.label;
      // (a modal drawer makes this region inert: the drawer's own note says stopped / stale there)
      setText($('live-sentence'), said);
      S.liveState = live.state;
    }
    renderUpdated();
    if (!st) return;

    const t = obj(st.tournament);
    const name = str(t.name) || 'Super Market dashboard';
    setText($('tournament-name'), name);
    setText($('currency-note'), 'Currency: ' + currency());
    const title = VIEW_TITLES[S.view] + ' · ' + name;
    if (document.title !== title) document.title = title;
    show($('demo-badge'), !!st.demo);

    const b = budgetState(st);
    const meter = $('budget-meter');
    if (b.limited) {
      const wait = b.until != null ? Math.max(1, Math.ceil(b.until - nowS())) : null;
      const text = wait != null ? 'Paused ' + wait + ' s' : 'Rate limited';
      $('budget-fill').style.width = '100%';
      setText($('budget-text'), text);
      setAttr(meter, 'aria-valuenow', 100);
      setAttr(meter, 'aria-valuetext', 'Rate limited by the API' + (wait != null ? ': reads paused for ' + wait + ' seconds' : ''));
      setAttr(meter, 'data-level', 'critical');
      setAttr($('budget'), 'title', 'The API is rate-limiting this account, so the bot is waiting before it reads again');
    } else if (b.used == null || !b.limit) {
      setText($('budget-text'), DASH);
      $('budget-fill').style.width = '0%';
      setAttr(meter, 'aria-valuenow', 0);
      setAttr(meter, 'aria-valuetext', 'Unknown');
      setAttr(meter, 'data-level', null);
    } else {
      const frac = clamp(b.used / b.limit, 0, 1);
      $('budget-fill').style.width = (frac * 100).toFixed(1) + '%';
      setText($('budget-text'), fmtInt(b.used) + ' / ' + fmtInt(b.limit) + ' per min');
      setAttr(meter, 'aria-valuenow', Math.round(frac * 100));
      setAttr(meter, 'aria-valuetext', fmtInt(b.used) + ' of ' + fmtInt(b.limit) + ' API reads used in the last minute');
      setAttr(meter, 'data-level', frac >= 0.95 ? 'critical' : frac >= 0.8 ? 'warning' : null);
      setAttr($('budget'), 'title', 'API reads used in the last minute (the account limit is shared by every key)');
    }
  }

  /** "Updated N s ago": plain text (not a live region) that ticks every second. */
  function renderUpdated() {
    const st = S.data.status;
    const last = num(obj(st && st.tracker).last_snapshot_at);
    const upd = $('updated');
    if (last != null && dateOf(last)) {
      setText(upd, fmtAgo(last));
      setAttr(upd, 'title', 'Last price snapshot: ' + fmtAbs(last));
      setAttr(upd, 'datetime', dateOf(last).toISOString());
    } else {
      setText(upd, st ? 'not yet' : DASH);
      setAttr(upd, 'title', st ? 'No price snapshot yet' : null);
      setAttr(upd, 'datetime', null);
    }
  }

  /** What to do about a fatal tracker error. Account problems (Terms, residence, email, a ban,
   *  missing scopes) come first: a 403 for those is not the key's fault. */
  function fatalHint(message) {
    const m = str(message);
    const restart = ' Then restart the dashboard.';
    if (/TERMS_NOT_ACKNOWLEDGED|terms\s*(?:&|and)\s*conditions|accept the (?:current )?terms/i.test(m)) {
      return 'Your account has not accepted the current Terms & Conditions. Sign in to the Predictions Cup site and accept them.' + restart;
    }
    if (/RESIDENCE_UPDATE_REQUIRED|residen(?:ce|cy)|state\/territory/i.test(m)) {
      return 'Your account needs its state or territory of residence updated. Sign in to the Predictions Cup site and update it.' + restart;
    }
    if (/EMAIL_NOT_(?:CONFIRMED|VERIFIED)|confirm (?:the|your) e-?mail|e-?mail (?:address )?(?:is )?not (?:yet )?(?:confirmed|verified)/i.test(m)) {
      return 'Your account’s email address is not confirmed yet. Open the confirmation email (or request a new one on the site) and confirm it.' + restart;
    }
    if (/ACCOUNT_(?:BANNED|SUSPENDED|DISABLED)|\bbanned\b|suspended/i.test(m)) {
      return 'The Predictions Cup has suspended or banned this account, so its key cannot read the market. Contact the Cup organisers.';
    }
    if (/INSUFFICIENT_SCOPES?|missing scopes?|\bscopes?\b/i.test(m)) {
      return 'This API key lacks the read permissions (scopes) the dashboard needs. Create a key with read access on the site and put it in SUPERMARKET_API_KEY (in your .env file).' + restart;
    }
    if (/API_KEY|INVALID_API_KEY|KEY_REVOKED|REVOKED|UNAUTHORI[SZ]ED|\b401\b|api key|key (?:has )?expired|EXPIRED_KEY|KEY_EXPIRED/i.test(m)) {
      return 'Your API key was rejected: it may have been revoked, expired or mistyped. Put a valid key in SUPERMARKET_API_KEY ' +
        '(in your .env file), then restart the dashboard. To explore without a key, start it with --demo.';
    }
    if (/\b403\b|FORBIDDEN/i.test(m)) {
      return 'The API refused this account access (HTTP 403). The message above says why: fix that on the Predictions Cup site.' + restart;
    }
    return 'Restart the dashboard. If this keeps happening, run it with -v to see more detail in the terminal.';
  }

  const PROBLEM_SOURCES = {
    prices: 'Price snapshots', snapshot: 'Price snapshots', snapshots: 'Price snapshots', price_snapshot: 'Price snapshots',
    history: 'Price history', backfill: 'Price history', price_history: 'Price history',
    markets: 'Market list', market_list: 'Market list', exchanges: 'Market list',
    leaderboard: 'Leaderboard', balance: 'Your balance', tournament_balance: 'Your balance', context: 'Tournament context',
    constraints: 'Constraint checks', news: 'News search', trades: 'Trade tape', book: 'Order books', books: 'Order books',
    rate_limit: 'Rate limit', ratelimit: 'Rate limit', analysis: 'Surge analysis', attribution: 'Surge analysis', detection: 'Surge detection',
    order_books: 'Order books', storage: 'Saving data', analytics: 'Price analytics',
  };

  function problemSource(raw) {
    const key = str(raw).trim();
    const known = PROBLEM_SOURCES[key.toLowerCase().replace(/[\s-]+/g, '_')];
    if (known) return known;
    return key ? key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ') : 'Market data';
  }

  /** Non-fatal data problems: the server's "problems" list, or (older servers) its recent tracker errors. */
  function currentProblems(st) {
    if (!st) return [];
    const now = nowS();
    let list;
    if (Array.isArray(st.problems)) {
      list = objs(st.problems).map(function (p) {
        return { source: str(p.source), message: str(p.message), since: num(p.since), last: num(p.last), count: num(p.count), severity: str(p.severity) };
      });
    } else {
      // Older servers: group the tracker's recent errors by where they happened; keep the recent ones.
      const tr = obj(st.tracker);
      const window = Math.max(120, 4 * (num(st.interval) || num(tr.interval) || 30));
      const groups = {};
      for (const e of objs(tr.recent_errors)) {
        const at = num(e.at);
        const where = str(e.where) || 'tracker';
        const g = groups[where] || (groups[where] = { source: where, message: '', since: at, last: at, count: 0, severity: 'warning' });
        g.count += 1;
        g.message = str(e.error || e.message) || g.message;
        if (at != null) {
          g.since = g.since == null ? at : Math.min(g.since, at);
          g.last = g.last == null ? at : Math.max(g.last, at);
        }
      }
      list = Object.keys(groups).map(function (k) { return groups[k]; }).filter(function (g) { return g.last != null && now - g.last <= window; });
    }
    for (const p of list) {
      p.rateLimit = /rate|429|too many|throttl/i.test(p.source + ' ' + p.message) && (p.last == null || now - p.last <= 300);
    }
    list.sort(function (a, b) {
      return (b.severity === 'error') - (a.severity === 'error') || (a.since || 0) - (b.since || 0);
    });
    return list.filter(function (p) { return p.message || p.source; });
  }

  function problemItem(p) {
    // the short message (no request line); the full text stays available as a tooltip
    const full = sentence(p.message, 'not available.');
    const li = h('li', { class: 'problem' + (p.severity === 'error' ? ' is-error' : ''), title: full },
      h('strong', null, problemSource(p.source) + ': '), shortProblem(p.message));
    const meta = h('span', { class: 'problem-meta' });
    if (p.since != null && dateOf(p.since)) meta.append(' since ', timeEl(p.since, 'short'), ' (', timeEl(p.since, 'ago'), ')');
    if (p.count != null && p.count > 1) meta.append(' · ' + fmtInt(p.count) + ' failed attempts');
    if (meta.childNodes.length) li.appendChild(meta);
    return li;
  }

  /** The problems banner's first line: what the failures mean for the page. It only says the rest
   *  is up to date when every failing source is an optional one (history, leaderboard, books …). */
  function renderProblemsIntro(problems, tr) {
    const intro = $('problems-intro');
    const live = liveState();
    const listMissing = !!marketListProblem();
    const pricesDown = problems.some(function (p) { return problemKind(p) === 'prices'; }) || live.state === 'stale';
    const severe = problems.some(function (p) { return p.severity === 'error'; });
    const historyDown = problems.some(function (p) { return problemKind(p) === 'history'; }) && !obj(tr.backfill).complete;
    const last = num(tr.last_snapshot_at);
    const key = sig([listMissing, pricesDown, severe, historyDown, pricesDown ? last : null]);
    if (intro.dataset.sig === key) return;
    intro.dataset.sig = key;
    const parts = [];
    if (listMissing) parts.push('The market list has not loaded yet, so the views below have nothing to show. ');
    else if (pricesDown) {
      if (last != null && dateOf(last)) parts.push('Prices are not updating: the figures below are from ', timeEl(last, 'clock'), ' (', timeEl(last, 'ago'), '). ');
      else parts.push('No prices have loaded yet. ');
    } else if (severe) parts.push('Some figures below may be out of date. ');
    if (historyDown) parts.push('Charts fill in when price history recovers; surge detection uses live prices meanwhile. ');
    parts.push('The bot is still running and keeps retrying.');
    if (!listMissing && !pricesDown && !severe) parts.push(' Everything else on the page is up to date.');
    rebuild(intro, parts.map(function (p) { return p instanceof Node ? p : document.createTextNode(p); }));
  }

  /** Show or hide a banner; announce it the first time it appears (its text, once). */
  function showBanner(id, visible, announceText) {
    const el = $(id);
    const was = !!S.bannerShown[id];
    if (!visible && was && el.contains(document.activeElement)) {
      // the banner is going away with keyboard focus inside it (for example on "Retry now"): keep the user's place
      $('main').focus({ preventScroll: true });
    }
    show(el, visible);
    S.bannerShown[id] = !!visible;
    if (visible && !was && announceText) announce(announceText);
  }

  function renderOfflineCountdown() {
    if (!isOffline()) return;
    const left = Math.ceil((S.nextRetryAt - Date.now()) / 1000);
    setText($('offline-countdown'), S.polling || !S.nextRetryAt || left <= 0 ? 'Retrying…' : 'Trying again in ' + left + ' s.');
  }

  function renderBanners() {
    const st = S.data.status;
    const tr = obj(st && st.tracker);

    const fatal = str(tr.fatal_error);
    if (fatal) {
      setText($('fatal-message'), fatal);
      setText($('fatal-hint'), fatalHint(fatal));
    }
    showBanner('fatal-banner', !!fatal);

    const offline = isOffline() && !S.offlineDismissed;
    if (offline) {
      setText($('offline-message'), S.failureText + ' Is the dashboard still running in your terminal?');
      renderOfflineCountdown();
    }
    showBanner('offline-banner', offline);

    const viewErr = str(st && st.view_error);
    if (viewErr) setText($('view-error-message'), 'The tracker could not build its live view: ' + sentence(viewErr) + ' Showing the last data it had.');
    showBanner('view-error-banner', !!viewErr && !fatal && !isOffline(), 'Live data is temporarily unavailable.');

    const problems = fatal || isOffline() ? [] : currentProblems(st);
    const pkey = sig(problems.map(function (p) { return [p.source, p.message, p.since, p.count, p.severity]; }));
    const list = $('problems-list');
    if (list.dataset.sig !== pkey) {
      list.dataset.sig = pkey;
      const items = problems.slice(0, 6).map(problemItem);
      if (problems.length > 6) items.push(h('li', { class: 'problem' }, 'and ' + (problems.length - 6) + ' more.'));
      rebuild(list, items);
    }
    // One summary line names the sources; the details (each message, since when, how often) are
    // folded away, and the banner can be hidden until the set of failing sources changes.
    const sources = problems.map(function (p) { return problemSource(p.source); }).filter(function (v, i, a) { return a.indexOf(v) === i; });
    const skey = sig(sources);
    S.problemsKey = skey;
    if (!problems.length) S.problemsDismissed = null;
    setText($('problems-title'), 'Some data is unavailable' + (sources.length ? ': ' + sources.join(', ') : ''));
    renderProblemsIntro(problems, tr);
    const details = $('problems-details');
    setText($('problems-count'), problems.length ? '(' + problems.length + ')' : '');
    if (details.open !== S.problemsOpen) details.open = S.problemsOpen;
    showBanner('problems-banner', problems.length > 0 && S.problemsDismissed !== skey,
      'Some data is unavailable: ' + sources.join(', ') + '.');

    let startup = '';
    let progress = null;
    // A view error is not a fresh start (its banner explains), and while the market list, the
    // prices or the price history are failing the problems banner says what is going on.
    const historyDown = !!findProblem('history');
    if (st && !fatal && !viewErr && !marketListProblem() && !pricesNeverLoaded()) {
      const bf = obj(tr.backfill);
      const done = num(bf.done);
      const total = num(bf.total);
      if (!trackerStarted()) {
        startup = 'Taking the first price snapshot. Prices and charts appear in a few seconds.';
      } else if (total && done != null && !bf.complete && done < total && !historyDown) {
        startup = 'Loading price history: ' + fmtInt(done) + ' of ' + fmtInt(total) + ' series so far. Charts and surge detection fill in as it arrives.';
        progress = clamp(done / total, 0, 1);
      }
    }
    showBanner('startup-banner', !!startup);
    if (startup) setText($('startup-message'), startup);
    const meter = $('startup-meter');
    show(meter, progress != null);
    if (progress != null) {
      $('startup-fill').style.width = (progress * 100).toFixed(1) + '%';
      setAttr(meter, 'aria-valuenow', Math.round(progress * 100));
    }
  }

  /** Once a second: relative times, the retry countdown and the drawer's staleness note keep ticking. */
  function tick() {
    if (document.hidden) return;
    safe('tick', function () {
      renderUpdated();
      refreshTimes(document);
      renderOfflineCountdown();
      const live = liveState();
      if (live.state !== S.liveState) renderHeader();
    });
  }

  function renderNav() {
    for (const a of document.querySelectorAll('.app-nav a[data-view]')) {
      setAttr(a, 'aria-current', a.dataset.view === S.view ? 'page' : null);
    }
    const c = obj(S.data.status && S.data.status.counts);
    setText($('nav-count-markets'), num(c.outcomes) > 0 ? fmtInt(c.outcomes) : '');
    setText($('nav-count-surges'), num(c.surges) > 0 ? fmtInt(c.surges) : '');
    setText($('nav-count-high'), num(c.high_band) > 0 ? fmtInt(c.high_band) : '');
  }

  // ---------------------------------------------------------------- render: overview

  /** Rough width of a tile value in em of the value's font: digits ~0.6em, separators ~0.3em, the
   *  unit at its smaller size (0.47em). The value's font then shrinks to fit the tile (styles.css). */
  function tileEm(value, unit) {
    let w = 0;
    for (const ch of String(value)) w += /[0-9]/.test(ch) ? 0.6 : /[,.\s]/.test(ch) ? 0.3 : /[A-Z#]/.test(ch) ? 0.7 : 0.58;
    if (unit) {
      w += 0.47 * 0.3;
      for (const ch of String(unit)) w += 0.47 * (/[A-Z]/.test(ch) ? 0.7 : 0.56);
    }
    return Math.max(1, w * 1.08);
  }

  function setTile(id, value, sub, unit) {
    const el = $('tile-' + id);
    const key = sig([value, unit]);
    if (el.dataset.sig !== key) {
      el.dataset.sig = key;
      // Number and unit stay on one line (a wrapped unit would push every tile's value down);
      // a long value gets a smaller font instead, so it never spills out of the tile.
      const fit = h('span', { class: 'tile-fit' }, h('span', { class: 'tile-num' }, value));
      if (unit) fit.append(h('span', { class: 'tile-gap' }, ' '), h('span', { class: 'tile-unit' }, unit)); // a real space, at the unit's size
      el.replaceChildren(fit);
      el.style.setProperty('--fit', tileEm(value, unit).toFixed(2));
      el.dataset.measured = '';
    }
    if (!el.dataset.measured) {
      // measured once the tile is on screen: the value's width in em of its own font
      const fit = el.firstElementChild;
      const w = fit ? fit.getBoundingClientRect().width : 0;
      const fs = fit ? parseFloat(getComputedStyle(fit).fontSize) : 0;
      if (w > 0 && fs > 0) {
        el.style.setProperty('--fit', (w / fs * 1.03).toFixed(3));
        el.dataset.measured = '1';
      }
    }
    setText($('tile-' + id + '-sub'), sub || ' ');
  }

  /** The participants tile's second line: the fade ideas Strategy actually shows (a surge that has
   *  partly reverted can have no edge left at its stop, so not every open surge is a fade). */
  function participantsSub(open, c) {
    const d = S.data.strategy;
    let fades = num(c.fade_ideas);
    if (fades == null && d && d.available !== false) {
      fades = objs(d.opportunities).filter(function (o) { return str(o.kind) === 'fade'; }).length;
    }
    if (!open) return fades ? fades + (fades === 1 ? ' fade idea' : ' fade ideas') + ': see Strategy' : 'none to fade right now';
    if (fades == null) return 'possible fades: see Strategy';
    if (fades > 0) return fades + (fades === 1 ? ' fade idea' : ' fade ideas') + ': see Strategy';
    return 'still open, but no fade has an edge left';
  }

  function renderOverview() {
    const st = S.data.status;
    if (st) {
      const c = obj(st.counts);
      const acct = obj(st.account);
      const missing = marketListProblem() || pricesNeverLoaded();
      if (missing && !(num(c.outcomes) > 0)) setTile('outcomes', DASH, marketListProblem() ? 'the market list has not loaded yet' : 'no prices loaded yet');
      else setTile('outcomes', fmtInt(c.outcomes), num(c.markets) != null ? 'in ' + fmtInt(c.markets) + ' markets' : '');
      setTile('surges', fmtInt(c.surges_last_hour), num(c.surges) != null ? fmtInt(c.surges) + ' in the last 2 days' : '');
      setTile('participants', fmtInt(c.open_participant_surges), participantsSub(num(c.open_participant_surges) || 0, c));
      const lim = historyLimit();
      setTile('high', fmtInt(c.high_band), !(num(c.high_band) > 0) && lim
        ? 'none yet: the check needs 6 h of prices (see High 90s)'
        : 'favourite side at 0.95 or more');
      const bal = num(acct.balance);
      const parts = [];
      if (num(acct.my_rank) != null) parts.push('Rank #' + fmtInt(acct.my_rank));
      if (num(acct.leader_value) != null) parts.push('leader about ' + fmtInt(acct.leader_value));
      if (!parts.length && num(acct.initial_balance) != null) parts.push('started with ' + fmtInt(acct.initial_balance));
      setTile('balance', bal == null ? DASH : fmtInt(bal), parts.join(' · ') || 'balance not known yet', bal == null ? '' : currency());
      const tile = $('tile-balance');
      setAttr(tile, 'title', bal == null ? null : fmtMoney(bal) + ' ' + currency());

      const end = cupEnd();
      let sub = trackerStoppedMsg() ? 'Tracking has stopped: the figures below are the last ones it saw.'
        : marketListProblem() ? 'The market list can’t be loaded yet; the bot keeps retrying.'
          : num(c.outcomes) != null && num(c.markets) != null
            ? 'Watching ' + fmtInt(c.outcomes) + ' outcomes in ' + fmtInt(c.markets) + ' markets.'
            : 'What the bot is watching right now.';
      if (end != null) {
        // the time left comes from the end itself (days_left is clamped at 0 once the Cup is over)
        const left = end - nowS();
        sub += left > 0 ? ' The Cup ends ' + fmtDay(end) + ' (' + fmtSpan(left) + ' left).' : ' The Cup ended ' + fmtDay(end) + '.';
      }
      setText($('overview-sub'), sub);
    }
    renderLookIdeas();
    renderLookSurges();
  }

  function ideaKey(o) {
    return [str(o.kind), hasId(o.exchange_id) ? 'x' + o.exchange_id : 'm' + str(o.market_id), str(o.side), str(o.surge_id)].join(':');
  }

  /** An arbitrage or basket idea is a set of trades: its legs (newer servers), or "every outcome" of its market. */
  function isSet(o) {
    return str(o.kind) === 'arbitrage' || str(o.kind) === 'basket';
  }

  function ideaLegs(o) {
    return objs(o.legs).filter(function (l) { return str(l.side) || num(l.price) != null; });
  }

  function legText(l) {
    // a binary outcome's option is just "YES": "BUY NO YES" would misread, so only real option labels are named
    const opt = str(l.option);
    const named = opt && !/^(yes|no)$/i.test(opt.trim());
    return 'BUY ' + (str(l.side).toUpperCase() || '?') + (named ? ' ' + opt : '') + ' @ ' + fmtPrice(l.price);
  }

  function actionText(o) {
    const side = str(o.side).toUpperCase() || '?';
    if (isSet(o)) {
      const legs = ideaLegs(o);
      if (legs.length) return 'Buy ' + legs.length + ' legs @ ' + fmtPrice(o.entry_price) + ' per full set';
      return 'BUY ' + side + ' on every outcome @ ' + fmtPrice(o.entry_price) + ' per full set';
    }
    return 'BUY ' + side + ' @ ' + fmtPrice(o.entry_price);
  }

  /** The outcome an idea trades, or none for a set (a set never names just its first leg). */
  function ideaOption(o) {
    return isSet(o) ? '' : str(o.option);
  }

  /** A single-outcome idea opens its outcome; a set (or an idea without an outcome) opens its own
   *  card on the Strategy page (#strategy/<idea key>). */
  function ideaHref(o) {
    return !isSet(o) && hasId(o.exchange_id) ? exchangeHref(o.exchange_id) : '#strategy/' + encodeURIComponent(ideaKey(o));
  }

  function isStrategyHref(href) {
    return href.indexOf('#strategy') === 0;
  }

  function sizeUnit(o, n) {
    return isSet(o) ? (n === 1 ? 'set' : 'sets') : n === 1 ? 'share' : 'shares';
  }

  function outcomeLabel(title, option) {
    const t = str(title) || 'Untitled market';
    return option ? t + ' — ' + option : t;
  }

  /** What an Overview idea item shows: an item is rebuilt only when one of these changes. */
  function lookIdeaView(o, i) {
    const href = ideaHref(o);
    return {
      href: href, rank: i + 1, label: outcomeLabel(o.title, ideaOption(o)), kind: str(o.kind), action: actionText(o),
      side: num(o.prob_win) != null ? 'Win ' + fmtPct0(o.prob_win) : 'Return ' + fmtPctOf(o.expected_return),
      edge: 'Edge ' + fmtSigned(o.edge) + '/' + sizeUnit(o, 1),
      eid: isStrategyHref(href) ? null : String(o.exchange_id),
    };
  }

  function lookIdeaItem(v, key) {
    return h('li', { class: 'look-item', 'data-key': key },
      h('a', { href: v.href, 'data-focus-key': 'idea:' + key, 'data-eid': v.eid },
        h('span', { class: 'look-rank' }, h('span', { class: 'sr-only' }, 'Rank '), String(v.rank)),
        h('span', { class: 'look-main' },
          h('span', { class: 'look-title' }, v.label),
          h('span', { class: 'look-meta' }, kindBadge(v.kind), h('strong', null, v.action))),
        h('span', { class: 'look-side' }, h('span', null, v.side), h('span', null, v.edge))));
  }

  function renderLookIdeas() {
    const list = $('look-ideas');
    const empty = $('look-ideas-empty');
    const d = S.data.strategy;
    let items = null;
    let msg = '';
    if (!d) msg = loadingMsg('strategy', 'Loading ideas…');
    else if (d.available === false) msg = str(d.error) || 'The strategy report is not available yet.';
    else {
      items = objs(d.opportunities).slice(0, 5);
      if (!items.length) msg = noDataMsg() || 'No trade ideas right now. The bot keeps looking every few seconds.';
    }
    show(empty, !!msg);
    setText(empty, msg);
    // Keyed by idea, signed by what each item shows: the 30 s strategy refresh changes scores and
    // prices behind the scenes, and an unchanged item keeps its DOM (and any text selection in it).
    const existing = new Map();
    for (const el of list.children) existing.set(el.dataset.key, el);
    const seen = {};
    const wanted = (items || []).map(function (o, i) {
      let k = ideaKey(o);
      seen[k] = (seen[k] || 0) + 1;
      if (seen[k] > 1) k += '#' + seen[k];
      const v = lookIdeaView(o, i);
      const vkey = sig(v);
      let el = existing.get(k);
      if (!el || (CARD_SIGS.get(el) !== vkey && !selectionInside(el))) {
        const fresh = lookIdeaItem(v, k);
        CARD_SIGS.set(fresh, vkey);
        if (el) swapCard(el, fresh);
        el = fresh;
      }
      return el;
    });
    placeChildren(list, wanted);
  }

  /** Make a container's children exactly `wanted`, in order, moving as few nodes as possible and
   *  keeping keyboard focus where it was. */
  function placeChildren(box, wanted) {
    const keep = new Set(wanted);
    for (const el of Array.prototype.slice.call(box.children)) if (!keep.has(el)) el.remove();
    let same = box.children.length === wanted.length;
    for (let i = 0; same && i < wanted.length; i++) same = box.children[i] === wanted[i];
    if (!same) {
      const active = document.activeElement;
      for (const el of wanted) box.appendChild(el);
      if (active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
    }
  }

  function renderLookSurges() {
    const list = $('look-surges');
    const empty = $('look-surges-empty');
    const d = S.data.surges;
    let items = null;
    let msg = '';
    if (!d) msg = loadingMsg('surges', 'Loading surges…');
    else {
      items = objs(d.surges).slice(0, 5);
      if (!items.length) msg = noSurgesMsg();
    }
    const key = sig([msg, items && items.map(function (s) {
      const rf = num(s.reverted_fraction);
      return [s.id, moveOf(s), s.window, s.status, rf != null && s.status !== 'open' ? Math.round(rf * 100) : null,
        s.attribution && s.attribution.verdict, s.attribution && s.attribution.confidence, s.title, s.option];
    })]);
    if (S.sigs.lookSurges !== key && !selectionInside(list)) {
      S.sigs.lookSurges = key;
      show(empty, !!msg);
      setText(empty, msg);
      rebuild(list, (items || []).map(function (s) {
        // A surge that reverted, held or closed is history, not a live move: its status says so
        // (as on the Surges cards) and its verdict badge is toned down.
        const past = !!s.status && s.status !== 'open';
        return h('li', { class: 'look-item' + (past ? ' is-past' : '') },
          h('a', { href: hasId(s.exchange_id) ? exchangeHref(s.exchange_id) : '#surges', 'data-focus-key': 'surge:' + surgeKey(s),
            'data-eid': hasId(s.exchange_id) ? String(s.exchange_id) : null },
            h('span', { class: 'look-rank', 'aria-hidden': 'true' }, icon('bolt')),
            h('span', { class: 'look-main' },
              h('span', { class: 'look-title' }, outcomeLabel(s.title, s.option)),
              h('span', { class: 'look-meta' }, deltaEl(moveOf(s)), h('span', null, 'over ' + str(s.window || '?')),
                timeEl(s.detected_at, 'ago'), past ? statusBadge(s) : null)),
            h('span', { class: 'look-side' }, verdictBadge(s.attribution))));
      }));
    } else if (S.sigs.lookSurges !== key) {
      show(empty, !!msg);
      setText(empty, msg);
    }
  }

  // ---------------------------------------------------------------- render: markets

  const ROWS = new WeakMap();

  const SORT_KEYS = {
    title: function (r) { return str(r.title).toLowerCase(); },
    option: function (r) { return str(r.option).toLowerCase(); },
    last: function (r) { return num(r.last); },
    bid: function (r) { return num(r.bid); },
    ask: function (r) { return num(r.ask); },
    spread: function (r) { return num(r.spread); },
    change_5m: function (r) { return num(r.change_5m); },
    change_1h: function (r) { return num(r.change_1h); },
    change_24h: function (r) { return num(r.change_24h); },
    settles: function (r) { return toTs(r.settlement_date); },
    flags: function (r) { const v = (r.surge_id != null ? 2 : 0) + (r.high_band ? 1 : 0); return v || null; },
  };
  const TEXT_SORTS = { title: true, option: true };

  function compareRows(a, b) {
    const key = S.sort.key;
    const get = SORT_KEYS[key] || SORT_KEYS.title;
    const va = get(a);
    const vb = get(b);
    const dir = S.sort.dir === 'desc' ? -1 : 1;
    let c = 0;
    if (va == null && vb == null) c = 0;
    else if (va == null) return 1; // missing values always sort last
    else if (vb == null) return -1;
    else if (typeof va === 'string') c = va.localeCompare(vb) * dir;
    else c = (va - vb) * dir;
    if (c !== 0) return c;
    return str(a.title).localeCompare(str(b.title)) || str(a.option).localeCompare(str(b.option)) ||
      str(a.exchange_id).localeCompare(str(b.exchange_id));
  }

  function matchesQuery(r, terms) {
    if (!terms.length) return true;
    const hay = [r.title, r.option, r.exchange_id, r.market_id].map(str).join(' ').toLowerCase();
    return terms.every(function (t) { return hay.indexOf(t) !== -1; });
  }

  function matchesFilter(r) {
    if (S.filter === 'surging') return r.surge_id != null;
    if (S.filter === 'high') return !!r.high_band;
    return true;
  }

  function createMarketRow(eid) {
    const tr = h('tr', { class: 'clickable', 'data-eid': eid });
    const cells = {};
    function cell(name, cls) {
      const td = h('td', { class: cls || null });
      tr.appendChild(td);
      cells[name] = td;
      return td;
    }
    const link = h('a', { class: 'row-link', href: exchangeHref(eid), 'data-focus-key': 'row:' + eid, 'data-eid': eid });
    const linkText = h('span', { class: 'title-text' });
    // The outcome is part of the link's name (several rows share a title). On narrow screens, where
    // the table scrolls under the pinned Market column, it also shows as the pinned cell's second line.
    const linkSep = h('span', { class: 'sr-only' });
    const linkOption = h('span', { class: 'title-option' });
    link.append(linkText, linkSep, linkOption);
    cell('title', 'cell-title').appendChild(link);
    cells.link = link;
    cells.linkText = linkText;
    cells.linkOption = linkOption;
    cells.linkSep = linkSep;
    cell('option', 'cell-option');
    ['last', 'bid', 'ask', 'spread', 'change_5m', 'change_1h', 'change_24h'].forEach(function (n) { cell(n, 'num'); });
    cell('spark', 'spark-cell');
    cell('settles', 'cell-settles');
    cell('flags', 'cell-flags');
    ROWS.set(tr, { cells: cells, sigs: {} });
    return tr;
  }

  function setCell(rec, name, key, build) {
    const k = sig(key);
    if (rec.sigs[name] === k) return;
    rec.sigs[name] = k;
    rec.cells[name].replaceChildren(build());
  }

  function settlesCell(dateStr) {
    const ts = toTs(dateStr);
    if (ts == null) return h('span', { class: 'nil' }, DASH);
    const end = cupEnd();
    const after = end != null && ts > end;
    const wrap = h('span', { title: 'Settles ' + fmtAbs(ts) + (after ? ' — after the Cup ends' : '') });
    wrap.appendChild(document.createTextNode(fmtDay(ts)));
    if (after) wrap.appendChild(h('span', { class: 'cell-sub settle-after' }, icon('clock'), 'after Cup end')); // its own line
    return wrap;
  }

  function updateMarketRow(tr, r) {
    const rec = ROWS.get(tr);
    const c = rec.cells;
    setText(c.linkText, str(r.title) || 'Untitled market');
    setText(c.linkSep, str(r.option) ? ' — ' : '');
    setText(c.linkOption, str(r.option));
    setCell(rec, 'option', [r.option], function () { return document.createTextNode(str(r.option) || DASH); });
    setCell(rec, 'last', [r.last], function () { return lastTradeEl(r.last); });
    setCell(rec, 'bid', [r.bid], function () { return priceEl(r.bid); });
    setCell(rec, 'ask', [r.ask], function () { return priceEl(r.ask); });
    setCell(rec, 'spread', [r.spread], function () {
      return num(r.spread) == null ? h('span', { class: 'nil' }, DASH) : h('span', { class: 'tabular' }, fmtPrice(r.spread));
    });
    ['change_5m', 'change_1h', 'change_24h'].forEach(function (n) {
      setCell(rec, n, [r[n]], function () { return deltaEl(r[n]); });
    });
    const spark = arr(r.sparkline);
    setCell(rec, 'spark', [spark.length, spark[0], spark[spark.length - 1], spark.length > 2 ? spark[spark.length >> 1] : null],
      function () { return sparkline(spark, str(r.title)); });
    setCell(rec, 'settles', [r.settlement_date, cupEnd()], function () { return settlesCell(r.settlement_date); });
    setCell(rec, 'flags', [r.surge_id != null, !!r.high_band], function () { return flagBadges(r); });
  }

  function sparkline(raw, label) {
    const pts = arr(raw).map(function (p) { return Array.isArray(p) ? [num(p[0]), num(p[1])] : [null, null]; })
      .filter(function (p) { return p[0] != null && p[1] != null; })
      .sort(function (a, b) { return a[0] - b[0]; });
    if (pts.length < 2) return h('span', { class: 'nil', title: 'Not enough data for a 24 hour trend yet' }, DASH);
    const W = 104, H = 28, P = 3;
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }
    // A minimum span of 0.05 keeps flat markets looking flat instead of magnifying tick noise.
    const span = Math.max(hi - lo, 0.05);
    const mid = (hi + lo) / 2;
    const y0 = mid - span / 2;
    const t0 = pts[0][0];
    const t1 = pts[pts.length - 1][0];
    const X = function (t) { return P + ((t - t0) / ((t1 - t0) || 1)) * (W - 2 * P); };
    const Y = function (v) { return H - P - ((v - y0) / span) * (H - 2 * P); };
    let d = '';
    pts.forEach(function (p, i) { d += (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1); });
    const first = pts[0][1];
    const lastV = pts[pts.length - 1][1];
    const text = '24 hour trend: ' + fmtPrice(first) + ' ' + ARROW + ' ' + fmtPrice(lastV) + ', low ' + fmtPrice(lo) + ', high ' + fmtPrice(hi);
    const svg = svgEl('svg', { class: 'spark', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'img', 'aria-label': text });
    svg.appendChild(svgEl('title', null, text));
    svg.appendChild(svgEl('path', { d: d }));
    svg.appendChild(svgEl('circle', { class: 'spark-end', cx: X(t1).toFixed(1), cy: Y(lastV).toFixed(1), r: 2.5 }));
    return svg;
  }

  function renderSortHeaders() {
    for (const th of document.querySelectorAll('#markets-table th[data-sort]')) {
      const active = th.dataset.sort === S.sort.key;
      setAttr(th, 'aria-sort', active ? (S.sort.dir === 'asc' ? 'ascending' : 'descending') : null);
      setText(th.querySelector('.sort-ind'), active ? (S.sort.dir === 'asc' ? '▲' : '▼') : '');
    }
  }

  function renderMarkets() {
    renderSortHeaders();
    const d = S.data.markets;
    const tbody = $('markets-body');
    const empty = $('markets-empty');
    const wrap = $('markets-wrap');
    const seen = new Set();
    const rows = d ? objs(d.rows).filter(function (r) {
      if (!hasId(r.exchange_id)) return false;
      r.exchange_id = String(r.exchange_id);
      if (seen.has(r.exchange_id)) return false;
      seen.add(r.exchange_id);
      return true;
    }) : [];

    setText($('chip-count-all'), d ? fmtInt(rows.length) : '');
    setText($('chip-count-surging'), d ? fmtInt(rows.filter(function (r) { return r.surge_id != null; }).length) : '');
    setText($('chip-count-high'), d ? fmtInt(rows.filter(function (r) { return !!r.high_band; }).length) : '');

    const terms = S.query.toLowerCase().split(/\s+/).filter(Boolean);
    const shown = rows.filter(function (r) { return matchesFilter(r) && matchesQuery(r, terms); }).sort(compareRows);

    const existing = new Map();
    for (const tr of tbody.children) existing.set(tr.dataset.eid, tr);
    const wanted = shown.map(function (r) {
      const tr = existing.get(r.exchange_id) || createMarketRow(r.exchange_id);
      updateMarketRow(tr, r);
      return tr;
    });
    const keep = new Set(wanted);
    for (const tr of Array.prototype.slice.call(tbody.children)) if (!keep.has(tr)) tr.remove();
    const current = tbody.children;
    let same = current.length === wanted.length;
    for (let i = 0; same && i < wanted.length; i++) same = current[i] === wanted[i];
    if (!same) {
      const active = document.activeElement;
      for (const tr of wanted) tbody.appendChild(tr);
      if (active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
    }

    let msg = '';
    let action = null;
    if (!d) msg = loadingMsg('markets', 'Loading markets…');
    else if (!rows.length) {
      msg = viewUnavailable() ? 'Live data is temporarily unavailable; the markets appear again when the tracker recovers.'
        : noDataMsg() || (trackerStarted() ? 'No open markets were found in this tournament.' : 'Waiting for the first price snapshot…');
    }
    else if (!shown.length) {
      const what = S.filter === 'surging' ? ' surging' : S.filter === 'high' ? ' high-90s' : '';
      msg = terms.length ? 'No' + what + ' markets match “' + S.query.trim() + '”.' : 'No' + what + ' markets right now.';
      action = h('button', { type: 'button', class: 'btn', onclick: clearMarketFilters }, 'Show all markets');
    }
    show($('markets-frame'), !!shown.length);
    show(empty, !!msg);
    if (msg && empty.dataset.msg !== msg) {
      empty.dataset.msg = msg;
      rebuild(empty, [h('span', { class: 'empty-text' }, msg), action]);
    }
    const count = d ? 'Showing ' + fmtInt(shown.length) + ' of ' + fmtInt(rows.length) + ' outcomes' : '';
    setText($('markets-count'), count);
    // What a screen reader hears after the user searches or filters (debounced: once typing pauses).
    S.marketsSay = d && rows.length && !shown.length ? msg : count;
    updateScrollCue($('markets-frame'), wrap);
  }

  /** Announce the search / filter result once the user pauses (not on every keystroke, and once). */
  function announceMarketsSoon() {
    clearTimeout(S.announceTimer);
    S.announceTimer = setTimeout(function () {
      if (S.view === 'markets' && S.marketsSay) announce(S.marketsSay);
    }, 650);
  }

  /** Edge shadows on a horizontally scrolling table: they show which side has more columns. */
  function updateScrollCue(frame, wrap) {
    if (!frame || !wrap) return;
    const max = wrap.scrollWidth - wrap.clientWidth;
    const first = wrap.querySelector('thead th');
    if (first) frame.style.setProperty('--sticky-w', Math.round(first.getBoundingClientRect().width + 1) + 'px');
    setAttr(frame, 'data-more-left', max > 1 && wrap.scrollLeft > 1 ? '' : null);
    setAttr(frame, 'data-more-right', max > 1 && wrap.scrollLeft < max - 1 ? '' : null);
  }

  function viewUnavailable() {
    return !!str(S.data.status && S.data.status.view_error);
  }

  function clearMarketFilters() {
    S.query = '';
    $('market-search').value = '';
    setFilter('all');
    $('market-search').focus();
  }

  function setFilter(f) {
    S.filter = f;
    for (const b of document.querySelectorAll('.chip[data-filter]')) setAttr(b, 'aria-pressed', b.dataset.filter === f ? 'true' : 'false');
    safe('markets', renderMarkets);
    announceMarketsSoon();
  }

  // ---------------------------------------------------------------- render: surges

  const CARD_SIGS = new WeakMap();
  const CARD_PARTS = new WeakMap(); // card -> {volatile: key of its move line and badges, compact}

  function surgeKey(s) {
    return hasId(s.id) ? 'id' + s.id : 'x' + str(s.exchange_id) + ':' + str(s.detected_at) + ':' + str(s.window);
  }

  function flowFacts(flow, depth) {
    if (!flow) {
      return h('p', { class: 'muted-note' }, depth != null ? 'Trade tape unavailable; book depth ' + fmtInt(depth) + ' shares within 5¢ of the mid.' : 'Trade tape unavailable for this move.');
    }
    const f = obj(flow);
    return factList([
      ['Trades', fmtInt(f.n_trades)],
      ['Volume', fmtUnit(f.total_size, 'shares')],
      ['Largest trade', fmtInt(f.max_trade_size) + (num(f.max_trade_size) != null && num(f.top_trade_share) != null ? ' (' + fmtPct0(f.top_trade_share) + ')' : ''),
        'Share of all traded size in the single largest trade'],
      ['Concentration', num(f.hhi) == null ? DASH : f.hhi.toFixed(2), 'Herfindahl index of trade sizes: 1.00 means one trade did everything'],
      ['YES side', fmtPct0(f.yes_share), 'Share of traded size bought on the YES side'],
      ['VWAP', fmtPrice(f.vwap), 'Volume-weighted average trade price'],
      ['Book depth', fmtUnit(depth, 'shares'), 'Resting shares within 5¢ of the mid'],
    ]);
  }

  function headlineList(articles, key) {
    const list = h('ul', { class: 'headlines' });
    objs(articles).slice(0, 6).forEach(function (a, i) {
      const title = str(a.title) || 'Untitled article';
      const url = safeExternal(a.url);
      const titleNode = url
        ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', 'data-focus-key': key + ':h' + i },
          title, icon('external', 'external'), h('span', { class: 'sr-only' }, ' (opens in a new tab)'))
        : h('span', { class: 'headline-title' }, title);
      const meta = h('span', { class: 'headline-meta' });
      if (a.source) meta.appendChild(document.createTextNode(str(a.source)));
      if (num(a.published_at) != null) {
        if (meta.childNodes.length) meta.appendChild(document.createTextNode(' · '));
        meta.appendChild(timeEl(a.published_at, 'ago'));
      }
      if (num(a.relevance) != null) {
        if (meta.childNodes.length) meta.appendChild(document.createTextNode(' · '));
        meta.appendChild(document.createTextNode('relevance ' + fmtPct0(clamp(a.relevance, 0, 1))));
      }
      list.appendChild(h('li', { class: 'headline' }, titleNode, meta));
    });
    return list;
  }

  /** The move a card prints: end − start, so the arrow value always matches the two prices beside it. */
  function moveOf(s) {
    const a = num(s.start_price), b = num(s.end_price);
    return a != null && b != null ? Number((b - a).toFixed(6)) : num(s.change);
  }

  function noSurgesMsg() {
    if (viewUnavailable()) return 'Live data is temporarily unavailable; surges appear again when the tracker recovers.';
    const why = noDataMsg();
    if (why) return why;
    const det = obj(S.data.status && S.data.status.detection);
    const reason = str(det.reason).trim();
    const n = outcomesWatched();
    if (det.enabled === false) return 'Surge detection is off' + (reason ? ': ' + sentence(reason) : '.');
    if (det.waiting_for_history || num(det.live_only) > 0) {
      return 'No surges yet. ' + (reason ? sentence(reason) : 'Surge detection is waiting for enough price history.') + ' Watching ' + fmtInt(n) + ' outcomes.';
    }
    return trackerStarted() || n ? 'No surges yet — watching ' + fmtInt(n) + ' outcomes.' : 'Waiting for the first price snapshot…';
  }

  /** A note above the surge cards while detection is limited (waiting for history, or live prices only). */
  function detectionNote() {
    const det = obj(S.data.status && S.data.status.detection);
    const reason = str(det.reason).trim();
    if (!reason || !(det.enabled === false || det.waiting_for_history || num(det.live_only) > 0)) return '';
    return (det.enabled === false ? 'Surge detection is off: ' : 'Surge detection: ') + sentence(reason);
  }

  /** Drop a Re-analyze note once it has expired or a newer analysis has landed. */
  function pruneAnalyze(s) {
    const cur = S.analyze[s.id];
    if (!cur || cur.state === 'pending') return;
    const analyzed = num(obj(s.attribution).analyzed_at);
    if (Date.now() - cur.at > ANALYZE_MSG_MS || (analyzed != null && cur.serverAt != null && analyzed > cur.serverAt)) clearAnalyze(s.id);
  }

  function clearAnalyze(id) {
    clearTimeout(S.analyzeTimers[id]);
    delete S.analyzeTimers[id];
    delete S.analyze[id];
  }

  function setAnalyze(id, entry) {
    clearTimeout(S.analyzeTimers[id]);
    delete S.analyzeTimers[id];
    S.analyze[id] = entry;
    if (entry.state !== 'pending') {
      // expire the note on a timer: a reverted or held surge's card is otherwise never rebuilt
      S.analyzeTimers[id] = setTimeout(function () {
        if (S.analyze[id] === entry) {
          clearAnalyze(id);
          rerenderSurgeViews();
        }
      }, ANALYZE_MSG_MS);
    }
  }

  function surgeName(s) {
    return outcomeLabel(s.title, s.option);
  }

  function analyzeFooter(s, key) {
    if (!hasId(s.id) || !/^[0-9]{1,12}$/.test(String(s.id))) return null; // the server only knows numeric surge ids
    const cur = S.analyze[s.id];
    const pending = !!(cur && cur.state === 'pending');
    const btn = h('button', {
      type: 'button',
      class: 'btn reanalyze',
      'data-focus-key': key + ':reanalyze',
      'data-surge-id': String(s.id),
      'aria-disabled': pending ? 'true' : null,
      title: 'Search the news and the trade tape again for this move',
    }, icon('refresh'), pending ? 'Sending…' : 'Re-analyze', h('span', { class: 'sr-only' }, ' ' + surgeName(s)));
    const msg = h('span', { class: 'action-msg' + (cur && cur.state === 'error' ? ' is-error' : ''), role: 'status' });
    if (cur && cur.state !== 'pending') {
      msg.appendChild(icon(cur.state === 'error' ? 'alert' : 'check'));
      msg.appendChild(document.createTextNode(' ' + cur.text));
    }
    const foot = h('div', { class: 'card-foot' }, btn, msg);
    const att = s.attribution;
    if (att && num(att.analyzed_at) != null) {
      foot.appendChild(h('span', { class: 'action-msg' }, 'Analysed ', timeEl(att.analyzed_at, 'ago'),
        att.method ? ' (' + str(att.method).replace('+', ' + ') + ')' : ''));
    }
    return foot;
  }

  function surgeBadges(s, att) {
    return h('div', { class: 'card-badges' }, verdictBadge(att), statusBadge(s));
  }

  function moveLine(s, compact) {
    const move = moveOf(s);
    const peakMove = num(s.peak_change);
    return h('div', { class: 'move-line' },
      h('span', { class: 'move-main' },
        h('span', { title: fmtPctOf(s.start_price) + ' ' + ARROW + ' ' + fmtPctOf(s.end_price) }, fmtPrice(s.start_price) + ' ' + ARROW + ' ' + fmtPrice(s.end_price)),
        ' ', deltaEl(move, 0)),
      peakMove != null && move != null && Math.abs(peakMove - move) >= 0.0005
        ? h('span', { class: 'move-fact', title: 'The largest move seen since this surge was detected' }, 'Peak move ', h('strong', null, fmtSigned(peakMove)))
        : null,
      h('span', { class: 'move-fact' }, 'Window ', h('strong', null, str(s.window) || DASH)),
      h('span', { class: 'move-fact', title: 'How unusual the move is versus this outcome’s own recent volatility (3 or more is unusual)' },
        'z-score ', h('strong', null, fmtZ(s.zscore))),
      h('span', { class: 'move-fact' }, 'Peak ', h('strong', null, fmtPrice(s.peak_price))),
      h('span', { class: 'move-fact' }, s.status === 'closed' ? 'Final ' : 'Now ', h('strong', null, fmtPrice(s.current_price))),
      compact ? null : h('span', { class: 'move-fact' }, 'Detected ', h('strong', null, timeEl(s.detected_at, 'ago'))));
  }

  function headlinesNote(att) {
    const ns = str(att.news_status);
    if (ns === 'unavailable') return h('p', { class: 'muted-note' }, icon('warn'), ' News search unavailable: headlines could not be checked for this move, so the verdict leans on the trade tape.');
    if (ns === 'disabled' || !newsEnabled()) return h('p', { class: 'muted-note' }, 'News search off for this dashboard.');
    return h('p', { class: 'muted-note' }, 'No matching headlines were found around the move.');
  }

  /** The fields that change on nearly every poll (live price, revert share, the move's end): updated in place. */
  function surgeVolatileKey(s) {
    return sig([s.start_price, s.end_price, s.change, s.peak_change, s.window, s.zscore, s.peak_price, s.current_price, s.detected_at, s.status, s.reverted_fraction]);
  }

  /** The rest of a card: a change rebuilds the card (unless the user is selecting text in it). */
  function surgeStableKey(s, compact) {
    const rest = {};
    for (const k of Object.keys(s)) {
      if (['start_price', 'end_price', 'change', 'peak_change', 'zscore', 'peak_price', 'current_price', 'reverted_fraction', 'end_ts', 'start_ts', 'status'].indexOf(k) === -1) rest[k] = s[k];
    }
    return sig([rest, S.analyze[s.id] || null, analysisEnabled(), newsEnabled(), !!compact]);
  }

  function surgeCard(s, compact) {
    s = obj(s);
    const key = surgeKey(s);
    const att = s.attribution ? obj(s.attribution) : null;
    const card = h(compact ? 'div' : 'article', { class: 'card surge-card', 'data-key': key });

    const titles = h('div', { class: 'card-titles' });
    // In the drawer the card sits under the h3 "Latest surge": its title is h4, its sections h5.
    const sub = compact ? 'h5' : 'h4';
    if (compact) {
      titles.appendChild(h('h4', { class: 'card-title' }, str(s.window || '?') + ' surge, detected ', timeEl(s.detected_at, 'ago')));
    } else {
      titles.appendChild(h('h3', { class: 'card-title' }, hasId(s.exchange_id)
        ? h('a', { href: exchangeHref(s.exchange_id), 'data-focus-key': key + ':title', 'data-eid': String(s.exchange_id) }, str(s.title) || 'Untitled market',
          str(s.option) ? h('span', { class: 'sr-only' }, ' — ' + str(s.option)) : null)
        : str(s.title) || 'Untitled market'));
      titles.appendChild(h('p', { class: 'card-sub' }, 'Outcome: ' + (str(s.option) || DASH)));
    }
    card.appendChild(h('div', { class: 'card-head' }, titles, surgeBadges(s, att)));
    card.appendChild(moveLine(s, compact));
    CARD_PARTS.set(card, { volatile: surgeVolatileKey(s), compact: !!compact });

    if (!att) {
      card.appendChild(h('p', { class: 'pending-note' },
        analysisEnabled() ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : icon('info'),
        analysisEnabled()
          ? 'Working out whether news or other traders caused this move. The verdict usually appears within a minute.'
          : 'Surge analysis is turned off for this dashboard.'));
    } else {
      if (att.summary) card.appendChild(h('p', { class: 'card-summary' }, str(att.summary)));
      const left = h('div', null,
        h(sub, { class: 'card-section-title' }, 'Why the bot thinks so'),
        h('ul', { class: 'reasons' }, texts(att.reasons).map(function (r) { return h('li', null, r); })));
      const right = h('div', null,
        factList([
          ['Reversion odds', fmtPct0(att.reversion_odds), 'Estimated chance the move gives back at least half'],
          ['Confidence', fmtPct0(att.confidence)],
        ]),
        h(sub, { class: 'card-section-title' }, 'Trade flow'),
        flowFacts(att.flow, num(att.book_depth)),
        h(sub, { class: 'card-section-title' }, 'Headlines'),
        objs(att.articles).length ? headlineList(att.articles, key) : headlinesNote(att));
      card.appendChild(h('div', { class: 'card-grid' }, left, right));
    }
    const foot = analyzeFooter(s, key);
    if (foot) card.appendChild(foot);
    return card;
  }

  /** Swap only the move line and badges of a card (keeps the reasons, headlines and any selection intact). */
  function updateSurgeVolatile(card, s) {
    const parts = CARD_PARTS.get(card);
    const vkey = surgeVolatileKey(s);
    if (!parts || parts.volatile === vkey) return;
    const line = card.querySelector(':scope > .move-line');
    const badges = card.querySelector(':scope > .card-head > .card-badges');
    if (selectionInside(line) || selectionInside(badges)) return; // try again on the next poll
    if (line) line.replaceWith(moveLine(s, parts.compact));
    if (badges) badges.replaceWith(surgeBadges(s, s.attribution ? obj(s.attribution) : null));
    parts.volatile = vkey;
  }

  /** Replace a card with a fresh one, keeping keyboard focus on the same control. */
  function swapCard(el, fresh) {
    const active = document.activeElement;
    const fkey = active && el.contains(active) ? active.getAttribute('data-focus-key') : null;
    el.replaceWith(fresh);
    if (fkey) {
      const t = fresh.querySelector('[data-focus-key="' + CSS.escape(fkey) + '"]');
      if (t) t.focus({ preventScroll: true });
    }
  }

  function renderSurges() {
    const d = S.data.surges;
    const box = $('surge-cards');
    const empty = $('surges-empty');
    const list = d ? objs(d.surges) : null;
    let msg = '';
    if (!d) msg = loadingMsg('surges', 'Loading surges…');
    else if (!list.length) msg = noSurgesMsg();
    show(empty, !!msg);
    setText(empty, msg);
    const note = list && list.length ? detectionNote() : '';
    show($('surges-note'), !!note);
    setText($('surges-note'), note);
    if (d) {
      const n = list.length;
      const open = list.filter(function (s) { return s.status === 'open'; }).length;
      setText($('surges-sub'), n
        ? n + (n === 1 ? ' surge' : ' surges') + ' in the last 2 days, ' + open + ' still open. Newest first, with the bot’s verdict on what caused each one.'
        : 'Sudden price moves, newest first, with the bot’s verdict on what caused them.');
    }
    const existing = new Map();
    for (const el of box.children) existing.set(el.dataset.key, el);
    const wanted = (list || []).map(function (s) {
      s = obj(s);
      pruneAnalyze(s);
      const key = surgeKey(s);
      const k = surgeStableKey(s, false);
      let el = existing.get(key);
      if (el && CARD_SIGS.get(el) === k) {
        updateSurgeVolatile(el, s);
      } else if (el && selectionInside(el)) {
        updateSurgeVolatile(el, s); // the user is selecting text in this card: rebuild it later
      } else {
        const fresh = surgeCard(s, false);
        CARD_SIGS.set(fresh, k);
        if (el) swapCard(el, fresh);
        el = fresh;
      }
      return el;
    });
    const keep = new Set(wanted);
    for (const el of Array.prototype.slice.call(box.children)) if (!keep.has(el)) el.remove();
    let same = box.children.length === wanted.length;
    for (let i = 0; same && i < wanted.length; i++) same = box.children[i] === wanted[i];
    if (!same) {
      const active = document.activeElement;
      for (const el of wanted) box.appendChild(el);
      if (active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
    }
  }

  async function reanalyze(id) {
    if (S.analyze[id] && S.analyze[id].state === 'pending') return;
    setAnalyze(id, { state: 'pending', text: '', at: Date.now(), serverAt: nowS() });
    rerenderSurgeViews();
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    let result;
    try {
      const resp = await fetch('/api/surges/' + encodeURIComponent(String(id)) + '/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: '{}',
        cache: 'no-store',
        credentials: 'same-origin',
        signal: ctrl.signal,
      });
      let body = null;
      try {
        body = await resp.json();
      } catch (e) {
        body = null;
      }
      if (resp.ok) result = { state: 'done', text: str(body && body.message) || 'Queued for analysis.' };
      else result = { state: 'error', text: str(body && body.error) || 'Could not queue the analysis (HTTP ' + resp.status + ').' };
    } catch (e) {
      result = {
        state: 'error',
        text: e && e.name === 'AbortError' ? 'The bot did not answer; try again.' : 'Could not reach the bot to queue the analysis.',
      };
    } finally {
      clearTimeout(timer);
    }
    result.at = Date.now();
    result.serverAt = S.analyze[id] ? S.analyze[id].serverAt : nowS();
    setAnalyze(id, result);
    rerenderSurgeViews();
    announce(result.text);
    if (result.state === 'done') {
      setTimeout(function () {
        poll();
        if (S.drawer.id) pollDrawer();
      }, 2000);
    }
  }

  function rerenderSurgeViews() {
    if (S.view === 'surges') safe('surges', renderSurges);
    if (S.drawer.id) {
      S.drawer.sigs.surge = null;
      safe('drawer', renderDrawer);
    }
  }

  // ---------------------------------------------------------------- render: high 90s

  /** Why the High 90s list is empty, or a note above a short list: the check needs 6 h of prices,
   *  and without price history outcomes only join it after 6 h of live prices. */
  function highHistoryText(empty) {
    const lim = historyLimit();
    if (!lim) return '';
    const n = nearHighCount();
    const now = n == null ? '' : ' ' + fmtInt(n) + (n === 1 ? ' outcome is' : ' outcomes are') + ' at 0.95 or more (or 0.05 or less) right now.';
    const join = lim.waiting > 0 && lim.live <= 0 && !lim.problem
      ? 'outcomes join this list once it has loaded.'
      : 'outcomes join this list after 6 h of live prices.';
    const head = empty ? 'Nothing in the high 90s yet: the check needs 6 h of prices, and ' : 'The high-90s check needs 6 h of prices, and ';
    return head + historyWhy(lim) + ', so ' + join + (empty ? now : '');
  }

  /** What a High 90s row shows, rounded as displayed: a row is rebuilt only when one of these changes
   *  (the band's mean and hours-to-settlement tick on every poll but are not shown). */
  function highRowView(b) {
    const settleTs = num(b.settlement_ts) != null ? b.settlement_ts : toTs(b.settlement_date);
    return [str(b.exchange_id), str(b.title), str(b.option), str(b.side).toUpperCase(), fmtPrice(b.favorite_price), fmtPrice(b.entry_price),
      !!b.entry_is_estimate, fmtPct0(b.time_in_band), num(b.lookback_s), !!b.stable, fmtPrice(b.low), fmtPrice(b.high), settleTs,
      b.settles_before_cup_end, fmtSigned(b.payout_per_share), num(b.return_pct) == null ? null : nf(1, 2).format(b.return_pct), cupEnd()];
  }

  function highRow(b) {
    const eid = str(b.exchange_id);
    const side = str(b.side).toUpperCase() || 'YES';
    const lookH = num(b.lookback_s) != null && b.lookback_s > 0 ? fmtSpan(b.lookback_s) : '6 h';
    const settleTs = num(b.settlement_ts) != null ? b.settlement_ts : toTs(b.settlement_date);
    const entry = num(b.entry_price);
    return h('tr', { class: 'clickable', 'data-eid': eid },
      h('td', { class: 'cell-title' }, h('a', { class: 'row-link', href: exchangeHref(eid), 'data-focus-key': 'high:' + eid, 'data-eid': eid },
        h('span', { class: 'title-text' }, str(b.title) || 'Untitled market'),
        str(b.option) ? [h('span', { class: 'sr-only' }, ' — '), h('span', { class: 'title-option' }, str(b.option))] : null)),
      h('td', { class: 'cell-option' }, str(b.option) || DASH),
      h('td', null, h('span', { class: 'side-pill', title: side === 'NO' ? 'NO is the favourite: YES trades at 0.05 or less' : 'YES is the favourite' }, side)),
      h('td', { class: 'num' }, priceEl(b.favorite_price),
        h('span', { class: 'cell-sub' }, entry == null ? 'no quote' : 'buy at ' + fmtPrice(entry) + (b.entry_is_estimate ? ' (est.)' : ''))),
      h('td', { class: 'num' }, fmtPct0(b.time_in_band), h('span', { class: 'cell-sub' }, 'of the last ' + lookH)),
      h('td', null, b.stable
        ? h('span', { class: 'yes-no is-yes' }, icon('check'), 'Stable')
        : h('span', { class: 'yes-no is-unknown' }, icon('pulse'), 'Moving'),
      h('span', { class: 'cell-sub' }, fmtPrice(b.low) + '–' + fmtPrice(b.high))),
      h('td', null, settleTs == null ? DASH : timeEl(settleTs, 'day'),
        settleTs == null ? null : h('span', { class: 'cell-sub' }, timeEl(settleTs, 'ago'))),
      h('td', { class: 'cell-cupend' }, yesNo(b.settles_before_cup_end, 'Yes, pays out', 'No — valued at market price', 'Unknown date')),
      h('td', { class: 'num' }, num(b.payout_per_share) == null ? DASH : fmtSigned(b.payout_per_share) + '/share',
        h('span', { class: 'cell-sub' }, num(b.return_pct) == null ? '' : nf(1, 2).format(b.return_pct) + '% return')));
  }

  function renderHigh() {
    const d = S.data.high;
    const tbody = $('high-body');
    const empty = $('high-empty');
    const seen = new Set();
    const bands = d ? objs(d.bands).filter(function (b) {
      if (!hasId(b.exchange_id) || seen.has(String(b.exchange_id))) return false;
      seen.add(String(b.exchange_id));
      return true;
    }) : [];
    let msg = '';
    if (!d) msg = loadingMsg('high', 'Loading…');
    else if (!bands.length) {
      const n = outcomesWatched();
      msg = viewUnavailable() ? 'Live data is temporarily unavailable; the high-90s list appears again when the tracker recovers.'
        : noDataMsg() || highHistoryText(true) || (trackerStarted() || n
          ? 'Nothing is hovering in the high 90s right now — watching ' + fmtInt(n) + ' outcomes.'
          : 'Waiting for the first price snapshot…');
    }
    show(empty, !!msg);
    setText(empty, msg);
    const note = bands.length ? highHistoryText(false) : '';
    show($('high-note'), !!note);
    setText($('high-note'), note);
    show($('high-frame'), bands.length > 0);
    // Rows are keyed by outcome and signed by what they show, so a poll that changes nothing visible
    // leaves the table alone (focus, a screen reader's table position and selections survive).
    const existing = new Map();
    for (const tr of tbody.children) existing.set(tr.dataset.eid, tr);
    const wanted = bands.map(function (b) {
      const eid = str(b.exchange_id);
      const vkey = sig(highRowView(b));
      let tr = existing.get(eid);
      if (!tr || (CARD_SIGS.get(tr) !== vkey && !selectionInside(tr))) {
        const fresh = highRow(b);
        CARD_SIGS.set(fresh, vkey);
        if (tr) swapCard(tr, fresh);
        tr = fresh;
      }
      return tr;
    });
    placeChildren(tbody, wanted);
    updateScrollCue($('high-frame'), $('high-wrap'));
  }

  // ---------------------------------------------------------------- render: strategy

  const MODES = {
    protect: {
      label: 'Protect mode', icon: 'shield',
      text: 'You are in the top 3 with a week or less to go. Cut variance: favour steady carry trades and size fades down, so a late upset cannot knock you out of the prizes.',
    },
    balanced: {
      label: 'Balanced mode', icon: 'scale',
      text: 'You are neither defending a top-3 finish nor far behind. Ideas are ranked by expected return × confidence, with no extra tilt toward risk.',
    },
    aggressive: {
      label: 'Aggressive mode', icon: 'flame',
      text: 'You are well behind the leader, or time is short and you are outside the top 10. Only the top 3 win prizes, so variance helps you: fades and arbitrage get a boost and slow carry trades are dampened.',
    },
  };

  function legList(o) {
    const legs = ideaLegs(o);
    if (!legs.length) return null;
    const parent = str(o.title);
    return h('ul', { class: 'legs', 'aria-label': 'Legs of this set' }, legs.map(function (l) {
      const other = str(l.title) && str(l.title) !== parent ? str(l.title) : '';
      const text = h('span', { class: 'leg-action' }, legText(l));
      const node = hasId(l.exchange_id)
        ? h('a', { href: exchangeHref(l.exchange_id), 'data-eid': String(l.exchange_id), 'data-focus-key': 'leg:' + ideaKey(o) + ':' + l.exchange_id }, text,
          other ? h('span', { class: 'leg-market' }, ' · ' + other) : null)
        : h('span', null, text, other ? h('span', { class: 'leg-market' }, ' · ' + other) : null);
      return h('li', null, node);
    }));
  }

  const FV_SOURCE_LABELS = {
    manual: 'your fair-value file', polymarket: 'Polymarket', kalshi: 'Kalshi', blend: 'Polymarket and Kalshi', demo: 'the demo feed',
    history: 'imported history',
  };

  function fvSourceLabel(src) {
    const k = str(src);
    return FV_SOURCE_LABELS[k] || k || 'unknown source';
  }

  /** A fair value's uncertainty (a probability) in cents: "± 1.0 cents". */
  function fmtCents(v) {
    v = num(v);
    return v == null ? DASH : '± ' + (v * 100).toFixed(1) + ' cents';
  }

  /** A small amount per share with 2-3 decimals ("0.17", "0.055"). */
  function fmtSmall(v) {
    v = num(v);
    return v == null ? DASH : nf(2, 3).format(v);
  }

  /** Expected profit per unit of a bet shape (BetShape.to_dict()); null for a fixed-size passive order. */
  function betEdge(b) {
    b = obj(b);
    const p = num(b.p), gain = num(b.gain), loss = num(b.loss);
    if (b.kind === 'riskless') return gain;
    if (b.kind === 'fixed' || p == null || gain == null || loss == null) return null;
    if (b.kind === 'bounded') {
      const tail = num(b.tail_prob) != null ? b.tail_prob : 1 - p;
      return (1 - tail) * gain - tail * loss;
    }
    return p * gain - (1 - p) * loss;
  }

  const REGIME_TEXT = {
    unknown: 'Settlement rule assumed: unknown, valued conservatively',
    resolved_outcomes: 'Settlement rule assumed: unresolved markets wait for the real outcome and pay 1/0',
    vwap_closeout: 'Settlement rule assumed: unresolved markets are cashed at a ~5-hour VWAP at the Cup end',
  };

  function regimeText(regime) {
    const k = str(regime) || 'unknown';
    return REGIME_TEXT[k] || 'Settlement rule assumed: ' + k.replace(/_/g, ' ');
  }

  /** A line list as a fact value (one sentence per item). */
  function lineList(lines) {
    const t = texts(lines);
    return t.length ? h('ul', { class: 'fact-lines' }, t.map(function (x) { return h('li', null, x); })) : null;
  }

  /** The paper-trading facts of an idea (docs/PAPER_TRADING.md §8.3), each only when the server sent it. */
  function ideaPaperFacts(o) {
    const per = sizeUnit(o, 1);
    const out = [];
    const fv = num(o.fair_value);
    if (fv != null) {
      out.push(['Fair value', fmtPrice(fv) + ' (' + fvSourceLabel(o.fair_source) + (num(o.fv_uncertainty) != null ? ', ' + fmtCents(o.fv_uncertainty) : '') + ')',
        'The outside fair value of the contract bought, and its own uncertainty', 'wide']);
    }
    const limit = num(o.limit_price);
    if (limit != null) {
      let order;
      if (str(o.order_type) === 'maker') {
        order = 'Resting limit at ' + fmtPrice(limit) + (num(o.expires_at) != null ? ' until ' + fmtClock(o.expires_at) : '');
      } else if (isSet(o)) {
        const legs = ideaLegs(o).map(function (l) { return num(l.limit) != null ? fmtPrice(l.limit) : null; }).filter(Boolean);
        order = 'Limit ' + fmtPrice(limit) + ' per set' + (legs.length ? ' (legs ' + legs.join(' / ') + ')' : '');
      } else {
        order = 'Limit ' + fmtPrice(limit) + (str(o.kind) === 'value' ? ' (keeps the full required edge)' : ' (gives away at most half the edge)');
      }
      out.push(['Order', order, str(o.order_type) === 'maker' ? 'A passive order: it fills only if prices trade through it' : 'The worst price the order accepts', 'wide']);
    }
    if (o.bet_limit) {
      const b = obj(o.bet_limit);
      const e = betEdge(b);
      if (e != null) out.push(['If filled at the limit', fmtSigned(e) + '/' + per, 'The expected profit per ' + per + ' if the whole order fills at its limit']);
      else if (b.kind === 'fixed' && num(b.gain) != null) out.push(['If filled at the limit', 'up to ' + fmtSigned(b.gain) + '/' + per + ' at the target', null, 'wide']);
    }
    if (o.exit_plan && str(obj(o.exit_plan).note)) out.push(['Exit plan', str(obj(o.exit_plan).note), null, 'full']);
    const fd = num(o.factor_delta);
    if (fd != null && Math.abs(fd) >= 0.0005 && !isSet(o)) {
      out.push(['National swing', 'A 3-point national swing toward the ' + (fd > 0 ? 'Republicans' : 'Democrats') + ' costs ' + fmtSmall(3 * Math.abs(fd)) + ' per ' + per,
        'Races share one national polling error', 'full']);
    }
    if (isSet(o) && o.bet) {
      const b = obj(o.bet);
      if (b.kind === 'riskless') out.push(['Set type', 'Riskless at a 1/0 settlement', null, 'full']);
      else if (b.kind === 'bounded') out.push(['Set type', 'Bounded: a refund on one leg could lose ' + fmtSmall(b.loss) + ' per set', null, 'full']);
    }
    const sizing = o.sizing ? obj(o.sizing) : null;
    if (sizing && texts(sizing.lines).length) out.push(['Sizing', lineList(sizing.lines), null, 'full']);
    const alt = o.alt_sizing ? obj(o.alt_sizing) : null;
    if (alt) {
      const first = texts(alt.lines)[0];
      const units = num(alt.units);
      out.push([str(alt.policy) === 'conservative' ? 'Conservative sizing' : 'Chaser sizing',
        first || (units == null ? DASH : fmtInt(units) + ' ' + sizeUnit(o, units)), 'What the other sizing policy would buy', 'full']);
    }
    if (num(o.profit_per_capital_day) != null) out.push(['Per day of capital', fmtPctOf(o.profit_per_capital_day, 2), 'Expected profit per day the capital stays tied up']);
    return out;
  }

  /** The Strategy view's sizing panel: the report policy, the bar, M, the Markov bound and every line. */
  function sizingFacts(sz) {
    const bar = sz.bar ? obj(sz.bar) : null;
    const lo = bar ? num(bar.low) : null, hi = bar ? num(bar.high) : null;
    const range = lo != null && hi != null && Math.round(lo) !== Math.round(hi) ? ' (range ' + fmtInt(lo) + '–' + fmtInt(hi) + ')' : '';
    const m = num(sz.M);
    return factList([
      str(sz.mode) ? ['Mode', str(sz.mode).replace(/_/g, ' ') + (sz.kept_by_hysteresis ? ' (kept by hysteresis)' : ''), null, 'wide'] : null,
      ['Top-3 bar', bar && num(bar.value) != null ? fmtInt(bar.value) + ' ' + currency() + range : 'unknown', 'Sized from the lower end of the range', 'wide'],
      ['M', m == null ? DASH : m.toFixed(1), m == null ? null : 'The bar divided by the portfolio value (' + m.toFixed(3) + ')'],
      ['Chance ceiling', num(sz.markov_ceiling) == null ? DASH : fmtPct0(sz.markov_ceiling), 'Markov bound: the most likely a strategy with this expected multiple reaches the bar'],
    ]);
  }

  function sizingPanel(d) {
    const sz = obj(d.sizing);
    const alt = sz.alternative ? obj(sz.alternative) : null;
    const panel = h('section', { class: 'panel', 'aria-labelledby': 'h-sizing' },
      h('h3', { class: 'panel-title', id: 'h-sizing' }, 'Sizing: ' + (str(sz.label) || str(sz.policy) || 'conservative')),
      sizingFacts(sz),
      texts(sz.lines).length ? h('ul', { class: 'notes sizing-lines' }, texts(sz.lines).map(function (t) { return h('li', null, t); })) : null,
      h('p', { class: 'muted-note regime-note' }, icon('info'), sentence(regimeText(d.settlement_regime))));
    if (alt) {
      panel.appendChild(h('details', { class: 'alt-sizing' },
        h('summary', null, icon('chevron', 'chev'), 'The other policy: ' + (str(alt.label) || str(alt.policy))),
        sizingFacts(alt),
        texts(alt.lines).length ? h('ul', { class: 'notes sizing-lines' }, texts(alt.lines).map(function (t) { return h('li', null, t); })) : null));
    }
    return panel;
  }

  function ideaCard(o, i) {
    o = obj(o);
    const key = ideaKey(o);
    const titleText = str(o.title) || 'Untitled market';
    const option = ideaOption(o);
    const href = ideaHref(o);
    const titleNode = !isStrategyHref(href)
      ? h('a', { href: href, 'data-focus-key': 'st:' + key + ':title', 'data-eid': String(o.exchange_id) }, titleText,
        option ? h('span', { class: 'sr-only' }, ' — ' + option) : null)
      : document.createTextNode(titleText);
    const action = h('p', { class: 'idea-action' }, h('strong', null, actionText(o)));
    if (num(o.target_price) != null) action.appendChild(document.createTextNode(' · target ' + fmtPrice(o.target_price)));
    if (num(o.stop_price) != null) action.appendChild(document.createTextNode(' · stop ' + fmtPrice(o.stop_price)));
    const shares = num(o.suggested_shares) || 0;
    const sizeText = shares > 0
      ? fmtInt(shares) + ' ' + sizeUnit(o, shares) + ' · ' + fmtMoney(o.suggested_cost) + ' ' + currency()
      : 'none yet: wait for confirmation';
    const per = sizeUnit(o, 1);
    const facts = factList([
      ['Win probability', fmtPct0(o.prob_win), num(o.prob_win) == null ? 'Not estimated for this kind of idea' : null],
      ['Edge per ' + per, num(o.edge) == null ? DASH : fmtSigned(o.edge), 'Expected profit per ' + per + ' after the stop'],
      ['Expected return', fmtPctOf(o.expected_return), 'Edge as a share of the entry price'],
      ['Suggested size', sizeText, null, 'wide'],
      ['Score', fmtScore(o.score), 'Expected growth: the profit in % of equity at the conservative size × confidence, adjusted for the risk mode'],
      ['Confidence', fmtPct0(o.confidence)],
      num(o.horizon_hours) != null ? ['Horizon', fmtSpan(o.horizon_hours * 3600)] : null,
      ['Settles before Cup end', o.settles_before_cup_end === true ? 'Yes' : o.settles_before_cup_end === false ? 'No' : 'Unknown'],
    ].concat(ideaPaperFacts(o)));
    const details = h('details', { 'data-idea': key },
      h('summary', { 'data-focus-key': 'st:' + key + ':why' }, icon('chevron', 'chev'), 'Why this idea, and the risks',
        h('span', { class: 'sr-only' }, ' — ' + outcomeLabel(titleText, option))), // every disclosure names its idea
      h('h5', null, 'Rationale'),
      texts(o.rationale).length
        ? h('ul', { class: 'reasons' }, texts(o.rationale).map(function (r) { return h('li', null, r); }))
        : h('p', { class: 'muted-note' }, 'No rationale given.'),
      h('h5', null, 'Risks'),
      texts(o.risks).length
        ? h('ul', { class: 'reasons' }, texts(o.risks).map(function (r) { return h('li', null, r); }))
        : h('p', { class: 'muted-note' }, 'No specific risks listed.'));
    if (S.openIdeas.has(key)) details.open = true;
    details.addEventListener('toggle', function () {
      if (details.open) S.openIdeas.add(key);
      else S.openIdeas.delete(key);
    });
    return h('li', { class: 'card idea', 'data-key': key },
      h('span', { class: 'idea-rank' }, h('span', { class: 'sr-only' }, 'Rank '), String(i + 1)),
      h('div', { class: 'idea-main' },
        h('div', { class: 'idea-head' }, kindBadge(o.kind),
          // tabindex -1: a link from the Overview (#strategy/<key>) moves focus to this heading
          h('h4', { class: 'idea-title', tabindex: '-1' }, titleNode,
            option ? h('span', { class: 'card-sub', 'aria-hidden': !isStrategyHref(href) ? 'true' : null }, ' — ' + option) : null)),
        action, legList(o), facts, details));
  }

  function backtestPanel(d) {
    const panel = h('section', { class: 'panel', 'aria-labelledby': 'h-backtest' }, h('h3', { class: 'panel-title', id: 'h-backtest' }, 'Fade backtest'));
    const status = str(d.backtest_status);
    const bt = d.backtest ? obj(d.backtest) : null;
    if (!bt) {
      if (status === 'error') panel.appendChild(h('p', null, 'The backtest failed: ' + (str(d.backtest_error) || 'unknown error') + '. It retries in a minute.'));
      else panel.appendChild(h('p', { class: 'pending-note' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        'Replaying the last 7 days of prices to measure how often surges reverted. This takes a few seconds.'));
      return panel;
    }
    panel.appendChild(h('p', { class: 'view-sub' }, 'How fading past surges would have done: enter at detection, exit ' + fmtSpan((num(bt.horizon_hours) || 6) * 3600) + ' later.'));
    panel.appendChild(factList([
      ['Surges tested', fmtInt(bt.n_surges)],
      ['Reverted', fmtInt(bt.n_reverted)],
      ['Reversion rate', fmtPct0(bt.reversion_rate)],
      ['Avg fade return', num(bt.avg_fade_return) == null ? DASH : fmtSigned(bt.avg_fade_return) + '/share'],
      ['Avg hold return', num(bt.avg_hold_return) == null ? DASH : fmtSigned(bt.avg_hold_return) + '/share'],
    ]));
    const windows = Object.keys(obj(bt.by_window));
    if (windows.length) {
      const table = h('table', { class: 'mini-table' },
        h('caption', { class: 'sr-only' }, 'Backtest by surge window'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Window'), h('th', { scope: 'col' }, 'Surges'), h('th', { scope: 'col' }, 'Reverted'),
          h('th', { scope: 'col' }, 'Rate'), h('th', { scope: 'col' }, 'Avg fade return'))),
        h('tbody', null, windows.map(function (w) {
          const r = obj(bt.by_window[w]);
          return h('tr', null, h('th', { scope: 'row' }, w), h('td', null, fmtInt(r.n)), h('td', null, fmtInt(r.n_reverted)),
            h('td', null, fmtPct0(r.reversion_rate)), h('td', null, num(r.avg_fade_return) == null ? DASH : fmtSigned(r.avg_fade_return)));
        })));
      panel.appendChild(h('div', { class: 'table-scroll' }, table));
    }
    if (texts(bt.notes).length) panel.appendChild(h('ul', { class: 'notes' }, texts(bt.notes).map(function (n) { return h('li', null, n); })));
    return panel;
  }

  /** Replace one part of the strategy page only when its content changed (and the user is not selecting text in it). */
  function strategyPart(name, key, build) {
    const parts = S.strategyParts;
    const cur = parts[name];
    if (cur && cur.key === key) return cur.el;
    if (cur && selectionInside(cur.el)) return cur.el;
    const el = build();
    parts[name] = { key: key, el: el };
    return el;
  }

  /** The risk banner's explanation. The fixed copy claims a position (top 3, far behind), so when
   *  the rank or the leader is unknown it says what the mode is based on instead. A server-sent
   *  mode_reason wins. */
  function modeText(d) {
    const reason = str(d.mode_reason).trim();
    if (reason) return sentence(reason);
    const key = MODES[str(d.risk_mode)] ? str(d.risk_mode) : 'balanced';
    const rank = num(d.my_rank), leader = num(d.leader_value), bal = num(d.balance), days = num(d.days_left);
    if (key === 'aggressive') {
      const behind = leader != null && leader > 0 && bal != null && bal < 0.9 * leader;
      if (!behind && rank == null) {
        return 'Rank unknown (no leaderboard data): treated as outside the top 10' + (days != null ? ' with ' + nf(0, 0).format(days) + ' days left' : '') +
          '. Only the top 3 win prizes, so variance helps you: fades and arbitrage get a boost and slow carry trades are dampened.';
      }
      if (!behind && rank != null) {
        return 'You are ranked #' + fmtInt(rank) + ', outside the top 10' + (days != null ? ', with ' + nf(0, 0).format(days) + ' days left' : '') +
          '. Only the top 3 win prizes, so variance helps you: fades and arbitrage get a boost and slow carry trades are dampened.';
      }
      return MODES.aggressive.text;
    }
    if (key === 'balanced' && (rank == null || leader == null)) {
      const what = rank == null && leader == null ? 'Rank and the leader’s value unknown' : rank == null ? 'Rank unknown' : 'Leader’s value unknown';
      return what + ' (no leaderboard data): defaulting to balanced. Ideas are ranked by expected return × confidence, with no extra tilt toward risk.';
    }
    return MODES[key].text;
  }

  /** Days left in the Cup, or "Ended" once it is over. */
  function daysLeftText(d) {
    const end = cupEnd();
    if (end != null && end <= nowS()) return 'Ended';
    return num(d.days_left) == null ? DASH : nf(0, 1).format(d.days_left);
  }

  function riskPanel(d) {
    const mode = MODES[str(d.risk_mode)] || MODES.balanced;
    const factsItems = [
      ['Balance', num(d.balance) == null ? DASH : fmtMoney(d.balance) + ' ' + currency()],
      ['Rank', num(d.my_rank) == null ? DASH : '#' + fmtInt(d.my_rank), num(d.my_rank) == null ? 'Unknown: no leaderboard data' : null],
      ['Leader (est.)', num(d.leader_value) == null ? DASH : fmtInt(d.leader_value) + ' ' + currency(), 'Starting balance plus the leader’s reported profit'],
      ['Days left', daysLeftText(d)],
    ];
    const assumptions = texts(d.assumptions);
    return h('section', { class: 'risk-banner', 'data-mode': str(d.risk_mode) || 'balanced', 'aria-labelledby': 'h-risk' },
      icon(mode.icon),
      h('div', { class: 'banner-body' },
        h('h3', { class: 'risk-mode', id: 'h-risk' }, mode.label),
        h('p', { class: 'risk-explain' }, modeText(d)),
        factList(factsItems, 'risk-facts'),
        assumptions.length
          ? h('div', { class: 'assumptions' }, h('p', { class: 'assumptions-title' }, icon('info'), 'Based on assumptions:'),
            h('ul', null, assumptions.map(function (t) { return h('li', null, t); })))
          : null));
  }

  function ideasList(opps) {
    const list = S.strategyIdeas || (S.strategyIdeas = h('ol', { class: 'ideas' }));
    const existing = new Map();
    for (const el of list.children) existing.set(el.dataset.key, el);
    const seen = {};
    const wanted = opps.map(function (o, i) {
      let k = ideaKey(o);
      seen[k] = (seen[k] || 0) + 1;
      if (seen[k] > 1) k += '#' + seen[k];
      const cardKey = sig([o, i, currency()]);
      let el = existing.get(k);
      if (!el || (CARD_SIGS.get(el) !== cardKey && !selectionInside(el))) {
        const fresh = ideaCard(o, i);
        fresh.dataset.key = k;
        CARD_SIGS.set(fresh, cardKey);
        if (el) swapCard(el, fresh);
        el = fresh;
      }
      return el;
    });
    const keep = new Set(wanted);
    for (const el of Array.prototype.slice.call(list.children)) if (!keep.has(el)) el.remove();
    let same = list.children.length === wanted.length;
    for (let i = 0; same && i < wanted.length; i++) same = list.children[i] === wanted[i];
    if (!same) {
      const active = document.activeElement;
      for (const el of wanted) list.appendChild(el);
      if (active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
    }
    return list;
  }

  function renderStrategy() {
    const d = S.data.strategy;
    const root = $('strategy-body');
    if (!d || d.available === false) {
      const key = sig(['empty', d && d.error, !d && viewError('strategy') ? 'err' : '']);
      if (S.sigs.strategy === key) {
        focusPendingIdea();
        return;
      }
      S.sigs.strategy = key;
      S.strategyParts = {};
      S.strategyIdeas = null;
      if (!d) rebuild(root, [emptyNote(loadingMsg('strategy', 'Loading the strategy report…'))]);
      else {
        rebuild(root, [h('div', { class: 'banner banner-warning' }, icon('warn', 'icon-lg'),
          h('div', { class: 'banner-body' }, h('h3', { class: 'banner-title' }, 'The strategy report is not available'),
            h('p', null, str(d.error) || 'Try again in a few seconds.')))]);
      }
      focusPendingIdea();
      return;
    }
    S.sigs.strategy = 'report';
    if (!S.strategyParts) S.strategyParts = {};
    // Volatile fields (now, generated_at) are left out of every key: an unchanged report keeps its DOM.
    const cur = currency();
    const risk = strategyPart('risk', sig([d.risk_mode, d.mode_reason, d.balance, d.my_rank, d.leader_value, daysLeftText(d), d.assumptions, cur]),
      function () { return riskPanel(d); });
    const principles = strategyPart('principles', sig([d.headline, d.principles]), function () {
      return h('section', { class: 'panel', 'aria-labelledby': 'h-principles' },
        h('p', { class: 'headline-text' }, str(d.headline)),
        h('h3', { class: 'panel-title', id: 'h-principles' }, 'How to play the Cup'),
        texts(d.principles).length ? h('ol', { class: 'principles' }, texts(d.principles).map(function (t) { return h('li', null, t); })) : null);
    });
    const opps = objs(d.opportunities);
    const ideasPanel = strategyPart('ideasPanel', sig([opps.length > 0]), function () {
      S.strategyIdeas = null;
      return h('section', { class: 'panel', 'aria-labelledby': 'h-ideas' }, h('h3', { class: 'panel-title', id: 'h-ideas' }));
    });
    setText(ideasPanel.querySelector('#h-ideas'), opps.length ? 'Ranked ideas (' + opps.length + ')' : 'Ranked ideas');
    if (opps.length) {
      const list = ideasList(opps);
      if (list.parentNode !== ideasPanel) ideasPanel.appendChild(list);
    } else {
      let note = ideasPanel.querySelector('.empty');
      if (!note) note = ideasPanel.appendChild(emptyNote(''));
      setText(note, noDataMsg() || 'No trade ideas right now. Ideas appear when the bot sees a Cup price away from its outside fair value, prices that do not add up, a liquid favourite to rest a deep order under, a participant-driven surge or a stable high-90s favourite.');
    }
    const sizing = d.sizing && typeof d.sizing === 'object'
      ? strategyPart('sizing', sig([d.sizing, d.settlement_regime, cur]), function () { return sizingPanel(d); }) : null;
    const backtest = strategyPart('backtest', sig([d.backtest_status, d.backtest_error, d.backtest]), function () { return backtestPanel(d); });
    const disclaimer = strategyPart('disclaimer', sig([d.disclaimer]), function () {
      return h('p', { class: 'disclaimer' }, icon('lock'), str(d.disclaimer) || 'Read-only analysis. Nothing is traded automatically.');
    });
    const wanted = [risk, sizing, principles, ideasPanel, backtest, disclaimer].filter(Boolean);
    if (root.children.length !== wanted.length || !root.querySelector(':scope > .risk-banner')) {
      rebuild(root, wanted);
    } else {
      // swap only the parts that changed: moving an unchanged part would drop a text selection inside it
      wanted.forEach(function (el, i) {
        const curEl = root.children[i];
        if (curEl === el) return;
        const active = document.activeElement;
        const fkey = active && curEl.contains(active) ? active.getAttribute('data-focus-key') : null;
        curEl.replaceWith(el);
        if (fkey) {
          const t = el.querySelector('[data-focus-key="' + CSS.escape(fkey) + '"]');
          if (t) t.focus({ preventScroll: true });
        }
      });
    }
    focusPendingIdea();
  }

  /** A link to one idea (#strategy/<key>, for example an arbitrage set on the Overview) lands on
   *  that idea's card, scrolled into view with focus on its heading; an unknown key stays at the top. */
  function focusPendingIdea() {
    const key = S.pendingIdea;
    if (!key || S.view !== 'strategy' || !S.data.strategy) return; // wait for the report
    S.pendingIdea = null;
    let card = null;
    for (const el of document.querySelectorAll('#strategy-body .idea[data-key]')) {
      if (el.dataset.key === key) {
        card = el;
        break;
      }
    }
    if (!card) return; // an idea that is gone: the view's heading has focus, at the top
    const head = card.querySelector('.idea-title') || card;
    card.scrollIntoView({ block: 'start' });
    head.focus({ preventScroll: true });
    card.classList.add('is-target');
    setTimeout(function () { card.classList.remove('is-target'); }, 2500);
  }

  // ---------------------------------------------------------------- render: simulation (paper trading)
  //
  // docs/PAPER_TRADING.md §8: every number about a simulated result is at liquidation value unless it
  // says otherwise; the pre-registered headline portfolio (you, acting by hand) is the only one judged
  // in full; no text names or highlights a "best" portfolio. Panels are built once and their bodies are
  // rebuilt only when the data they show changed (focus, open disclosures and selections survive polls).

  const SIM = {
    mode: null, // the layout on screen: 'loading' | 'off' | 'failed' | 'waiting' | 'run'
    panels: {}, // name -> {el, body, key, ...}
    note: null, // the one-line state note (loading, off, failed, no run yet)
    top: null, // the headline + signal study row
    hidden: new Set(), // portfolio ids whose equity line is toggled off
    table: false, // the equity curves show as a table
    posFilter: null, // open positions: portfolio filter (null = not chosen yet: the headline; '' = every portfolio)
    fvQuery: '',
    fvUsable: false,
    fillsAll: false,
    open: new Set(), // keys of open <details>
    previousOpen: false,
    previous: null, // the ?run=previous body
    previousState: '', // '' | 'loading' | 'error' | 'ready'
    previousError: '',
    previousGen: 0,
    resetBusy: false,
    chart: null,
    chartKey: null,
    chartWidth: 0,
    ro: null,
    hoverTs: null,
    liveTimer: null,
  };

  const SIM_LEVELS = {
    insufficient: ['clock', 'Not enough evidence', 'A threshold is not met yet: 12 covered hours, 30 ideas and 20 independent groups'],
    inconclusive: ['question', 'Inconclusive', 'The interval includes zero, or a guard fired (one idea, one race, one national swing or unknown depth)'],
    promising: ['carry', 'Promising, not proof', 'The strongest verdict one day can reach: confirm it on a second day before acting'],
    positive: ['checkCircle', 'Profitable so far', 'At least 48 covered hours on two days, positive on each of the last two'],
    negative: ['trendDown', 'Losing so far', 'The interval for the average idea lies entirely below zero'],
  };

  const SIM_GROUPS = [
    ['human:', 'Your headline (decided before the run)'],
    ['policy:', 'Bot speed: an upper bound for acting by hand'],
    ['kind:', 'By strategy (equal capital, conservative sizing, exploratory)'],
  ];

  const HORIZON_LABELS = { '5m': '+5 min', '30m': '+30 min', '2h': '+2 h', '6h': '+6 h' };

  // The equity chart also starts minutes into a run: allow minute steps before the shared 15-minute ones.
  const SIM_TICK_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400, 172800];

  const EXIT_REASONS = {
    target: 'Reached its target', stop: 'Stopped out', time: 'Time limit reached', settled: 'Settled', refund: 'Refunded',
    converged: 'Converged: the gap closed', fair_value: 'The fair value moved', regime: 'Closed before the Cup end (settlement rule)',
    legging: 'Legging: the other legs did not fill', replaced: 'Replaced by a stronger idea', reset: 'Simulation reset',
    'settings changed': 'Settings changed, run ended', completed: 'Run completed',
  };

  const POSITION_FLAGS = {
    depth_stale: 'depth from an older book', depth_unknown: 'depth unknown: valued with a haircut',
    closed_no_ruling: 'market closed, no ruling yet (not counted)', stale_quote: 'quote stale', post_cup: 'after the Cup end',
    synthetic: 'filled from candles (replay)',
  };

  const PROVIDER_STATUS = {
    ok: 'answering', partial: 'partly answering', offline: 'offline from this machine', backoff: 'backing off after errors',
    disabled: 'off', error: 'failing',
  };

  const PROVIDER_NAMES = { polymarket: 'Polymarket', kalshi: 'Kalshi', manual: 'Your fair-value file', demo: 'Demo feed' };

  const TESTABILITY = {
    testable: ['checkCircle', 'Testable'], partial: ['pulse', 'Partly testable'], not_testable: ['warn', 'Not testable yet'],
    not_replayable: ['closed', 'Not replayable'],
  };

  const SNIPPETS = [
    ['disable', 'Turn this match off'], ['pin', 'Pin the right outside market'], ['confirm', 'Confirm this match'],
    ['trade_near', 'Trade this near match'],
  ];

  // Equity line styles: the headline (you, by hand) is the one emphasised line, the bot-speed portfolios
  // share the second hue, and the per-kind portfolios are recessive grey lines told apart by dash pattern.
  const EQ_DASH = {
    'policy:conservative': '', 'policy:chaser': '7 4',
    'kind:value': '6 3', 'kind:basket': '2 3', 'kind:hole': '10 3 2 3', 'kind:fade': '1.5 4', 'kind:carry': '12 4',
    'kind:arbitrage': '5 2 1.5 2',
  };

  /** A signed whole amount with thousands separators: "+1,234", "−412", "0". */
  function fmtSignedMoney(v, dp) {
    v = num(v);
    if (v == null) return DASH;
    const d = dp == null ? 0 : dp;
    const r = Number(v.toFixed(d));
    if (r === 0) return nf(d, d).format(0);
    return (r > 0 ? '+' : MINUS) + nf(d, d).format(Math.abs(r));
  }

  /** A signed percentage of a fraction: 0.0041 -> "+0.41%". */
  function fmtSignedPct(v, dp) {
    v = num(v);
    if (v == null) return DASH;
    return fmtSigned(v * 100, dp == null ? 2 : dp) + '%';
  }

  function fmtHours(v) {
    v = num(v);
    return v == null ? DASH : v.toFixed(1) + ' h';
  }

  /** "24" for 24 hours, "1.5" for 1.5 (the "24-hour test"). */
  function fmtCount(v) {
    v = num(v);
    return v == null ? DASH : nf(0, 1).format(v);
  }

  function plural(n, one, many) {
    return fmtInt(n) + ' ' + (Math.round(num(n) || 0) === 1 ? one : many || one + 's');
  }

  /** A P&L amount with an up/down marker (the marker is decoration; the sign says it in text). */
  function pnlEl(v) {
    v = num(v);
    if (v == null) return h('span', { class: 'nil' }, DASH);
    const r = Math.round(v);
    const dir = r > 0 ? 'up' : r < 0 ? 'down' : 'flat';
    return h('span', { class: 'pnl delta ' + dir, title: fmtSignedMoney(v, 2) + ' ' + currency() },
      h('span', { class: 'arrow', 'aria-hidden': 'true' }, dir === 'up' ? '▲' : dir === 'down' ? '▼' : '●'), fmtSignedMoney(v));
  }

  function levelBadge(v) {
    v = obj(v);
    const key = SIM_LEVELS[str(v.level)] ? str(v.level) : 'unknown';
    const lv = SIM_LEVELS[key] || ['question', str(v.level) || 'No verdict yet', ''];
    const label = v.exploratory === true ? 'Exploratory: ' + lv[1].charAt(0).toLowerCase() + lv[1].slice(1) : lv[1];
    return h('span', { class: 'verdict-level level-' + key + (v.exploratory === true ? ' is-exploratory' : ''), title: lv[2] || null },
      icon(lv[0]), label);
  }

  function shortPortfolio(pid, label) {
    const id = str(pid);
    if (id.indexOf('human:') === 0) return 'You, by hand (headline)';
    if (id === 'policy:conservative') return 'Bot speed: conservative';
    if (id === 'policy:chaser') return 'Bot speed: chaser';
    if (id.indexOf('kind:') === 0) {
      const k = KINDS[id.slice(5)];
      return (k ? k[1] : id.slice(5)) + ' only';
    }
    return str(label) || id || 'Portfolio';
  }

  function simPortfolios(d) {
    return objs(d && d.portfolios).filter(function (p) { return str(p.portfolio_id); });
  }

  function headlinePid(d) {
    const hl = obj(d && d.headline);
    if (str(hl.portfolio_id)) return str(hl.portfolio_id);
    const ps = simPortfolios(d);
    for (const p of ps) if (p.headline === true) return str(p.portfolio_id);
    for (const p of ps) if (str(p.portfolio_id).indexOf('human:') === 0) return str(p.portfolio_id);
    return ps.length ? str(ps[0].portfolio_id) : '';
  }

  function portfolioLabels(d) {
    const out = {};
    for (const p of simPortfolios(d)) out[str(p.portfolio_id)] = shortPortfolio(p.portfolio_id, p.label);
    return out;
  }

  function simInterval(d) {
    const run = obj(d && d.run);
    return num(run.interval) || num(S.data.status && S.data.status.interval) || 30;
  }

  /** A details disclosure whose open state survives the panel being rebuilt. */
  function simDetails(key, summary, content, cls) {
    const el = h('details', { class: 'sim-details' + (cls ? ' ' + cls : ''), 'data-key': key },
      h('summary', { 'data-focus-key': 'sd:' + key }, icon('chevron', 'chev'), summary), content);
    if (SIM.open.has(key)) el.open = true;
    el.addEventListener('toggle', function () {
      if (el.open) SIM.open.add(key);
      else SIM.open.delete(key);
    });
    return el;
  }

  /** A panel of the Simulation view, built once: a heading, optional persistent controls, a body. */
  function simPanel(name, title, controls, cls) {
    let p = SIM.panels[name];
    if (p) return p;
    const hid = 'h-sim-' + name;
    const head = h('div', { class: 'section-head' }, h('h3', { class: 'panel-title', id: hid }, title));
    if (controls && controls.length) head.appendChild(h('div', { class: 'controls' }, controls));
    const body = h('div', { class: 'sim-panel-body' });
    const el = h('section', { class: 'panel sim-panel sim-' + name + (cls ? ' ' + cls : ''), 'aria-labelledby': hid, 'data-panel': name }, head, body);
    p = { el: el, body: body, key: null, head: head };
    SIM.panels[name] = p;
    return p;
  }

  /** Rebuild a panel's body when its key changed (and the user is not selecting text in it). */
  function fillPanel(p, key, build) {
    if (p.key === key) return;
    if (p.key != null && selectionInside(p.body)) return;
    p.key = key;
    rebuild(p.body, build());
  }

  function tableWrap(label, table, cls) {
    return h('div', { class: 'table-wrap sim-table-wrap' + (cls ? ' ' + cls : ''), tabindex: '0', role: 'region', 'aria-label': label }, table);
  }

  function simNote(text, warn, title) {
    if (warn) {
      return h('div', { class: 'banner banner-warning' }, icon('warn', 'icon-lg'),
        h('div', { class: 'banner-body' }, h('h3', { class: 'banner-title' }, title || 'The simulation is not available'), h('p', null, text)));
    }
    return h('p', { class: 'empty' }, text);
  }

  function renderSim() {
    const d = S.data.paper;
    const root = $('sim-body');
    if (!root) return;
    show($('sim-demo-badge'), !!(d && d.demo === true));
    let mode;
    if (!d) mode = 'loading';
    else if (d.enabled === false) mode = 'off';
    else if (d.available === false) mode = 'failed';
    else if (!d.run || typeof d.run !== 'object' || !(num(d.run.steps) > 0)) mode = 'waiting';
    else mode = 'run';
    if (!SIM.note) SIM.note = h('div', { class: 'sim-note' });
    const noteKey = sig([mode, d && d.error, mode === 'loading' ? loadingMsg('paper', '') : '', mode === 'waiting' && !(d && d.run) ? simInterval(d) : null]);
    if (SIM.note.dataset.sig !== noteKey) {
      SIM.note.dataset.sig = noteKey;
      let note = null;
      if (mode === 'loading') note = simNote(loadingMsg('paper', 'Loading the simulation…'));
      else if (mode === 'off') note = simNote('The paper trader is off. Start the dashboard without --no-paper to simulate.');
      else if (mode === 'failed') note = simNote(str(d.error) || 'The paper trader stopped. Restart the dashboard to simulate again.', true);
      else if (mode === 'waiting' && !(d.run && typeof d.run === 'object')) note = simNote('Waiting for the first simulated step (every ' + fmtSpan(simInterval(d)) + ').');
      rebuild(SIM.note, [note]);
    }
    const wanted = [];
    if (mode === 'loading' || mode === 'off' || mode === 'failed' || (mode === 'waiting' && !(d.run && typeof d.run === 'object'))) wanted.push(SIM.note);
    if (mode === 'waiting' && d.run && typeof d.run === 'object') wanted.push(safeSim('clock', function () { return renderSimClock(d); }));
    if (mode === 'run') {
      wanted.push(safeSim('clock', function () { return renderSimClock(d); }));
      if (SIM.previousOpen) wanted.push(safeSim('previous', renderSimPrevious));
      wanted.push(safeSim('top', function () { return renderSimTop(d); }));
      wanted.push(safeSim('equity', function () { return renderSimEquity(d); }));
      wanted.push(safeSim('portfolios', function () { return renderSimPortfolios(d); }));
      wanted.push(safeSim('chaser', function () { return renderSimChaser(d); }));
      wanted.push(safeSim('positions', function () { return renderSimPositions(d); }));
      wanted.push(safeSim('fills', function () { return renderSimFills(d); }));
      wanted.push(safeSim('trades', function () { return renderSimTrades(d); }));
    }
    if (mode !== 'loading') {
      wanted.push(safeSim('backtest', renderSimBacktest));
      wanted.push(safeSim('fairvalue', renderSimFairValue));
      if (mode === 'run' || mode === 'waiting') wanted.push(safeSim('caveats', function () { return renderSimCaveats(d); }));
    }
    SIM.mode = mode;
    placeChildren(root, wanted.filter(Boolean));
  }

  function safeSim(name, fn) {
    return safe('sim-' + name, fn) || null;
  }

  // ---- run clock

  function simButtons() {
    if (SIM.buttons) return SIM.buttons;
    const reset = h('button', { type: 'button', class: 'btn', id: 'sim-reset', 'aria-haspopup': 'dialog', onclick: openResetDialog }, icon('undo'), 'Reset');
    const prev = h('button', {
      type: 'button', class: 'btn btn-toggle', id: 'sim-previous-btn', 'aria-expanded': 'false', 'aria-controls': 'sim-previous',
      onclick: togglePrevious,
    }, icon('clock'), 'Previous run');
    prev.hidden = true;
    SIM.buttons = { reset: reset, prev: prev };
    return SIM.buttons;
  }

  function clockText(run) {
    const hours = num(run.hours_run) || 0;
    const wall = num(run.wall_hours);
    const target = num(run.target_hours) || 24;
    const gaps = arr(run.gaps).length;
    const extra = [];
    if (gaps > 0) extra.push(plural(gaps, 'gap'));
    if (wall != null && Math.abs(wall - hours) >= 0.05) extra.push(wall.toFixed(1) + ' h on the clock');
    return hours.toFixed(1) + ' h observed of the ' + fmtCount(target) + '-hour test' + (extra.length ? ' (' + extra.join('; ') + ')' : '');
  }

  function renderSimClock(d) {
    const btn = simButtons();
    const p = simPanel('clock', 'Run clock', [btn.prev, btn.reset]);
    const run = obj(d.run);
    show(btn.prev, d.has_previous === true);
    setAttr(btn.prev, 'aria-expanded', SIM.previousOpen ? 'true' : 'false');
    setAttr(btn.prev, 'aria-pressed', SIM.previousOpen ? 'true' : 'false');
    if (!p.progress) {
      p.fill = h('span', { class: 'sim-progress-fill' });
      p.text = h('p', { class: 'sim-clock-text', id: 'sim-clock-text' });
      p.progress = h('div', { class: 'sim-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-labelledby': 'sim-clock-text' }, p.fill);
      p.badge = h('p', { class: 'sim-complete' });
      p.facts = h('div', { class: 'sim-clock-facts' });
      p.body.append(p.badge, p.text, p.progress, p.facts);
    }
    const hours = num(run.hours_run) || 0;
    const target = num(run.target_hours) || 24;
    const text = clockText(run);
    setText(p.text, text);
    setAttr(p.progress, 'aria-valuemax', target);
    setAttr(p.progress, 'aria-valuenow', Number(hours.toFixed(2)));
    setAttr(p.progress, 'aria-valuetext', text);
    const frac = clamp(num(run.progress) != null ? run.progress : hours / target, 0, 1);
    p.fill.style.width = (frac * 100).toFixed(1) + '%';
    const complete = run.complete === true;
    const bkey = sig([complete, target]);
    if (p.badge.dataset.sig !== bkey) {
      p.badge.dataset.sig = bkey;
      rebuild(p.badge, complete ? [h('span', { class: 'verdict-level level-complete' }, icon('checkCircle'), fmtCount(target) + '-hour test complete')] : []);
      show(p.badge, complete);
    }
    const waiting = !(num(run.steps) > 0);
    const settings = obj(run.settings);
    const key = sig([waiting, run.run_id, run.started_at, run.last_step_at, run.steps, run.start_capital, run.capital_source, run.regime, run.sizing,
      run.all_collateral, run.code_version, settings, run.last_step_seconds, simInterval(d), currency()]);
    if (p.facts.dataset.sig === key || selectionInside(p.facts)) return p.el;
    p.facts.dataset.sig = key;
    const cap = num(run.start_capital);
    const items = [
      ['Started', num(run.started_at) == null ? DASH : [timeEl(run.started_at, 'short'), ' (', timeEl(run.started_at, 'ago'), ')'], null, 'wide'],
      ['Last step', num(run.last_step_at) == null ? 'none yet' : [timeEl(run.last_step_at, 'ago'),
        num(run.last_step_seconds) != null ? ' · took ' + (run.last_step_seconds < 10 ? Math.max(0.1, run.last_step_seconds).toFixed(1) + ' s' : fmtSpan(run.last_step_seconds)) : '']],
      ['Steps', fmtInt(run.steps), 'One step every ' + fmtSpan(simInterval(d))],
      ['Start capital', cap == null ? DASH : fmtInt(cap) + ' ' + currency() + (str(run.capital_source) ? ' (' + str(run.capital_source) + ')' : ''), 'Every portfolio starts with this', 'wide'],
      ['Sizing', str(run.sizing) || DASH],
    ];
    const params = obj(settings.params_changed);
    const pnames = Object.keys(params);
    const settingsList = factList([
      ['Sizing', str(settings.sizing || run.sizing) || DASH],
      ['Settlement rule', str(settings.regime || run.regime).replace(/_/g, ' ') || DASH],
      ['All collateral', (settings.all_collateral != null ? settings.all_collateral : run.all_collateral) === true ? 'On' : 'Off', 'Whether riskless sets need only cost minus floor in cash'],
      ['Start capital', num(settings.start_capital) != null ? fmtInt(settings.start_capital) : cap == null ? DASH : fmtInt(cap)],
      ['Code version', str(run.code_version) || DASH, null, 'wide'],
      ['Changed settings', pnames.length ? pnames.map(function (n) { return n + ' = ' + str(params[n]); }).join(', ') : 'none (defaults)', null, 'full'],
    ]);
    rebuild(p.facts, [
      waiting ? h('p', { class: 'pending-note' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        'Waiting for the first simulated step (every ' + fmtSpan(simInterval(d)) + ').') : null,
      factList(items, 'sim-facts'),
      h('p', { class: 'muted-note regime-note' }, icon('info'), sentence(regimeText(run.regime))),
      simDetails('settings', 'This run’s settings', h('div', { class: 'sim-details-body' }, settingsList,
        h('p', { class: 'muted-note' }, 'A run continues after a restart only with the same settings and code; otherwise it ends ("settings changed") and a new one starts.'))),
    ]);
    return p.el;
  }

  // ---- previous run

  function togglePrevious() {
    SIM.previousOpen = !SIM.previousOpen;
    if (SIM.previousOpen) loadPrevious();
    safe('sim', renderSim);
    if (SIM.previousOpen) {
      const p = SIM.panels.previous;
      if (p) p.el.scrollIntoView({ block: 'nearest' });
    }
  }

  async function loadPrevious() {
    const gen = ++SIM.previousGen;
    SIM.previousState = 'loading';
    SIM.previousError = '';
    try {
      const body = await getJSON(ENDPOINTS.paper + '?run=previous');
      if (gen !== SIM.previousGen) return;
      SIM.previous = body;
      SIM.previousState = 'ready';
      serverAnswered();
    } catch (e) {
      if (gen !== SIM.previousGen) return;
      SIM.previous = null;
      SIM.previousState = 'error';
      SIM.previousError = sentence(e && e.message, 'Could not load the previous run.');
    }
    if (S.view === 'sim') safe('sim', renderSim);
  }

  function renderSimPrevious() {
    const p = simPanel('previous', 'Previous run');
    p.el.id = 'sim-previous';
    const prev = SIM.previous;
    fillPanel(p, sig([SIM.previousState, SIM.previousError, prev, currency()]), function () {
      if (SIM.previousState === 'loading' || !SIM.previousState) {
        return [h('p', { class: 'pending-note' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading the previous run…')];
      }
      if (SIM.previousState === 'error' || !prev) return [h('p', { class: 'muted-note' }, SIM.previousError || 'No earlier simulation run has ended yet.')];
      const run = obj(prev.run);
      const final = obj(run.final);
      const ended = num(run.ended_at) != null ? run.ended_at : num(final.at);
      const reason = str(run.end_reason || final.reason);
      const ports = objs(final.portfolios).length ? objs(final.portfolios) : simPortfolios(prev);
      const verdicts = obj(final.verdicts);
      let hid = headlinePid(prev);
      if (!hid) for (const pt of ports) if (str(pt.portfolio_id).indexOf('human:') === 0) { hid = str(pt.portfolio_id); break; }
      const hv = obj(prev.headline).verdict ? obj(obj(prev.headline).verdict) : obj(verdicts[hid] || (ports.filter(function (x) { return str(x.portfolio_id) === hid; })[0] || {}).verdict);
      const rows = ports.map(function (pt) {
        const v = obj(verdicts[str(pt.portfolio_id)] || pt.verdict);
        return h('tr', null,
          h('th', { scope: 'row' }, shortPortfolio(pt.portfolio_id, pt.label)),
          h('td', { class: 'num' }, pnlEl(pt.pnl_liq)),
          h('td', null, str(v.level) ? levelBadge(v) : DASH));
      });
      return [
        h('p', null, 'Ended ', num(ended) == null ? DASH : timeEl(ended, 'short'), reason ? ' (' + (EXIT_REASONS[reason] || reason) + ')' : '',
          str(run.run_id) ? h('span', { class: 'muted-note' }, ' · ' + str(run.run_id)) : null),
        str(hv.level) ? h('div', { class: 'sim-verdict' }, levelBadge(hv), h('p', { class: 'verdict-sentence' }, str(hv.sentence))) : null,
        rows.length ? tableWrap('Previous run portfolios', h('table', { class: 'data-table sim-table' },
          h('caption', { class: 'sr-only' }, 'Each portfolio of the previous run at its end'),
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col', class: 'num' }, 'P&L at liquidation'), h('th', { scope: 'col' }, 'Verdict'))),
          h('tbody', null, rows))) : null,
      ];
    });
    return p.el;
  }

  // ---- headline and signal study

  function renderSimTop(d) {
    if (!SIM.top) SIM.top = h('div', { class: 'sim-top' });
    const a = renderSimHeadline(d);
    const b = renderSimStudy(d.study, 'study', 'Signal study');
    placeChildren(SIM.top, [a, b].filter(Boolean));
    return SIM.top;
  }

  function renderSimHeadline(d) {
    const p = simPanel('headline', 'Headline: you, acting by hand');
    const hl = obj(d.headline);
    const v = obj(hl.verdict);
    p.el.setAttribute('data-portfolio', str(hl.portfolio_id) || headlinePid(d));
    fillPanel(p, sig([hl, currency()]), function () {
      if (!str(hl.portfolio_id) && !str(v.level)) return [h('p', { class: 'muted-note' }, 'No headline portfolio yet.')];
      const pnl = num(hl.pnl_liq);
      const r = pnl == null ? 0 : Math.round(pnl);
      const word = pnl == null ? '' : r > 0 ? 'profit' : r < 0 ? 'loss' : '(no profit or loss yet)';
      const unvalued = num(hl.unvalued) || 0;
      const nUnvalued = num(v.unvalued_positions) || 0;
      const depth = num(hl.depth_unknown_share);
      return [
        h('p', { class: 'sim-label' }, str(hl.label) || shortPortfolio(hl.portfolio_id)),
        h('p', { class: 'sim-pnl' }, pnlEl(pnl), ' ', h('span', { class: 'sim-pnl-unit' }, currency() + ' ' + word)),
        h('p', { class: 'sim-pnl-sub' }, fmtSignedPct(hl.pnl_liq_pct) + ' at liquidation value ',
          h('span', { class: 'muted-note' }, '(at mid marks: ' + fmtSignedMoney(hl.pnl_mark) + ')')),
        h('div', { class: 'sim-verdict' }, str(v.level) ? levelBadge(v) : null,
          h('p', { class: 'verdict-sentence' }, str(v.sentence) || 'No verdict yet.')),
        texts(v.reasons).length ? h('ul', { class: 'reasons verdict-reasons' }, texts(v.reasons).map(function (t) { return h('li', null, t); })) : null,
        texts(hl.verdict_caveats).length ? h('div', { class: 'verdict-caveats' },
          h('h4', { class: 'card-section-title' }, icon('info'), 'What this result cannot tell you yet'),
          h('ul', null, texts(hl.verdict_caveats).map(function (t) { return h('li', null, t); }))) : null,
        unvalued > 0 || nUnvalued > 0 ? h('p', { class: 'muted-note sim-flag-line' }, icon('warn'),
          (nUnvalued > 0 ? plural(nUnvalued, 'position') : 'Positions') + ' whose market closed without a ruling ' + (nUnvalued === 1 ? 'is' : 'are') +
          ' left out of the result (last value ' + fmtMoney(unvalued, 0) + ' ' + currency() + ').') : null,
        depth != null && depth > 0.0005 ? h('p', { class: 'muted-note sim-flag-line' }, icon('warn'),
          fmtPctOf(depth, 0) + ' of the value is marked without a recent order book (valued with a haircut).') : null,
        h('p', { class: 'muted-note' }, 'Latency: fills only from an order book read at least ' + fmtSpan(num(hl.latency_s) || 240) + ' after each signal.'),
      ];
    });
    return p.el;
  }

  function studyCell(hz, unit) {
    hz = obj(hz);
    const n = num(hz.n) || 0;
    if (!n) return h('span', { class: 'nil' }, 'No signals yet');
    const lo = num(hz.ci_low), hi = num(hz.ci_high);
    const parts = [plural(n, 'signal') + ': ' + fmtSigned(hz.mean) + (lo != null && hi != null ? ' (' + fmtSigned(lo) + ' to ' + fmtSigned(hi) + ')' : '') + ' per ' + unit];
    const extra = [];
    if (num(hz.share_converged) != null) extra.push('converged ' + fmtPct0(hz.share_converged));
    if (num(hz.share_reversed) != null) extra.push('reversed ' + fmtPct0(hz.share_reversed));
    return h('span', { title: lo != null ? Math.round((num(hz.ci_level) || 0.9) * 100) + '% interval over ' + plural(hz.clusters, 'independent group') : 'An interval needs 20 signals in 10 independent groups' },
      parts[0] + (extra.length ? '; ' + extra.join(', ') : ''));
  }

  /** The signal-level event study (study.summary()): one row per kind, one column per horizon. */
  function studyTable(st, caption) {
    st = obj(st);
    const kinds = obj(st.kinds);
    const names = ['value', 'basket'].filter(function (k) { return kinds[k]; }).concat(Object.keys(kinds).filter(function (k) { return k !== 'value' && k !== 'basket'; }));
    let horizons = [];
    for (const k of names) for (const hz of Object.keys(obj(obj(kinds[k]).horizons))) if (horizons.indexOf(hz) === -1) horizons.push(hz);
    if (!horizons.length) horizons = Object.keys(HORIZON_LABELS);
    if (!names.length) return h('p', { class: 'muted-note' }, 'No value or basket signals have been recorded yet.');
    return tableWrap(caption, h('table', { class: 'data-table sim-table study-table' },
      h('caption', { class: 'sr-only' }, caption),
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Kind'), horizons.map(function (hz) { return h('th', { scope: 'col' }, HORIZON_LABELS[hz] || hz); }))),
      h('tbody', null, names.map(function (k) {
        const kd = obj(kinds[k]);
        const unit = k === 'basket' ? 'set' : 'share';
        return h('tr', { 'data-kind': k },
          h('th', { scope: 'row' }, kindBadge(k), h('span', { class: 'cell-sub' }, plural(kd.events, 'signal') + ', ' + fmtInt(kd.traded) + ' traded')),
          horizons.map(function (hz) { return h('td', null, studyCell(obj(kd.horizons)[hz], unit)); }));
      }))));
  }

  function renderSimStudy(st, name, title) {
    const p = simPanel(name, title);
    fillPanel(p, sig([st]), function () {
      if (!st || typeof st !== 'object') return [h('p', { class: 'muted-note' }, 'The signal study starts with the first value or basket signal.')];
      const pending = num(st.pending) || 0;
      return [
        str(st.can_show) ? h('p', { class: 'study-can' }, icon('checkCircle'), str(st.can_show)) : null,
        str(st.cannot_show) ? h('p', { class: 'study-cannot' }, icon('warn'), str(st.cannot_show)) : null,
        studyTable(st, 'What each signal would show after it, net of the spread, whether or not it was traded'),
        h('p', { class: 'muted-note' }, 'Each cell: the average change per share (per set for baskets) from buying at the signal’s ask and selling into the bid then.' +
          (pending > 0 ? ' ' + plural(pending, 'signal') + ' still wait for their later horizons.' : '')),
      ];
    });
    return p.el;
  }

  // ---- equity curves

  function eqStyle(p, hid) {
    const pid = str(p.portfolio_id);
    if (pid === hid) return { group: 'headline', dash: '' };
    if (pid.indexOf('policy:') === 0) return { group: 'bot', dash: Object.prototype.hasOwnProperty.call(EQ_DASH, pid) ? EQ_DASH[pid] : '7 4' };
    return { group: 'kind', dash: EQ_DASH[pid] || '3 3' };
  }

  function eqSeries(d) {
    const eq = obj(d.equity);
    const hid = headlinePid(d);
    return simPortfolios(d).map(function (p) {
      const pid = str(p.portfolio_id);
      const pts = arr(eq[pid]).map(function (q) { return Array.isArray(q) ? [num(q[0]), num(q[1])] : [null, null]; })
        .filter(function (q) { return q[0] != null && q[1] != null; })
        .sort(function (a, b) { return a[0] - b[0]; });
      return { pid: pid, label: shortPortfolio(pid, p.label), full: str(p.label), style: eqStyle(p, hid), pts: pts, headline: pid === hid };
    });
  }

  function eqTimes(series) {
    const all = [];
    for (const s of series) for (const q of s.pts) all.push(q[0]);
    all.sort(function (a, b) { return a - b; });
    const out = [];
    for (const t of all) if (!out.length || t - out[out.length - 1] > 0.5) out.push(t);
    return out;
  }

  /** The series value at time t: its last point at or before t (null before its first point). */
  function eqValueAt(pts, t) {
    if (!pts.length || t < pts[0][0] - 0.5) return null;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (pts[mid][0] <= t + 0.5) lo = mid;
      else hi = mid - 1;
    }
    return pts[lo][1];
  }

  function eqKey(style) {
    const svg = svgEl('svg', { class: 'eq-key eq-' + style.group, viewBox: '0 0 28 8', width: 28, height: 8, 'aria-hidden': 'true', focusable: 'false' });
    svg.appendChild(svgEl('line', { x1: 1, x2: 27, y1: 4, y2: 4, 'stroke-dasharray': style.dash || null }));
    return svg;
  }

  function niceMoneyStep(span, maxTicks) {
    const raw = span / Math.max(1, maxTicks);
    const base = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
    for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * base) return m * base;
    return 10 * base;
  }

  function eqSummary(d) {
    const n = simPortfolios(d).length;
    const hl = obj(d.headline);
    return plural(n, 'portfolio') + '; headline (you, by hand): ' + fmtSignedMoney(hl.pnl_liq) + ' at liquidation';
  }

  function renderSimEquity(d) {
    let p = SIM.panels.equity;
    if (!p) {
      const toggle = h('button', {
        type: 'button', class: 'btn btn-toggle', 'data-action': 'sim-table-toggle', 'aria-pressed': 'false',
        title: 'Show the equity curves as a table', onclick: function () { SIM.table = !SIM.table; SIM.chartKey = null; safe('sim', renderSim); },
      }, icon('table'), 'Show as table');
      p = simPanel('equity', 'Equity at liquidation value', [toggle]);
      p.toggle = toggle;
      p.summary = h('p', { class: 'view-sub chart-summary' });
      p.host = h('div', { class: 'chart sim-chart', tabindex: '0', role: 'group', 'aria-describedby': 'sim-chart-help' });
      p.help = h('p', { class: 'sr-only', id: 'sim-chart-help' },
        'Left and right arrow keys step through the times and read every portfolio’s equity. Show as table lists the same values.');
      p.readout = h('p', { class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
      p.legend = h('ul', { class: 'eq-legend', 'aria-label': 'Portfolios: select one to show or hide its line' });
      p.tableBox = h('div', { class: 'sim-eq-table' });
      p.body.append(p.summary, p.host, p.help, p.readout, p.legend, p.tableBox);
      p.host.addEventListener('keydown', simChartKey);
      p.host.addEventListener('focus', function () { simChartFocus(true); });
      p.host.addEventListener('blur', function () { simChartFocus(false); });
      if (typeof ResizeObserver === 'function') {
        SIM.ro = new ResizeObserver(function () {
          const w = Math.floor(p.host.clientWidth);
          if (w && w !== SIM.chartWidth && S.data.paper && !SIM.table) {
            SIM.chartWidth = w;
            requestAnimationFrame(function () { safe('sim-chart', drawSimChart); });
          }
        });
        SIM.ro.observe(p.host);
      }
    }
    setAttr(p.toggle, 'aria-pressed', SIM.table ? 'true' : 'false');
    show(p.host, !SIM.table);
    show(p.tableBox, SIM.table);
    const series = eqSeries(d);
    const summary = eqSummary(d);
    setAttr(p.host, 'aria-label', 'Equity curves: ' + summary);
    setText(p.summary, summary + '. Profit is the liquidation value: what selling every position into the visible bids would fetch.');
    // legend: one toggle button per portfolio (the headline listed first, as in the table)
    const lkey = sig([series.map(function (s) { return [s.pid, s.label, s.style]; }), Array.from(SIM.hidden).sort()]);
    if (p.legend.dataset.sig !== lkey) {
      p.legend.dataset.sig = lkey;
      rebuild(p.legend, series.map(function (s) {
        const on = !SIM.hidden.has(s.pid);
        return h('li', null, h('button', {
          type: 'button', class: 'eq-toggle', 'aria-pressed': on ? 'true' : 'false', 'data-pid': s.pid, 'data-focus-key': 'eq:' + s.pid,
          title: s.full || null,
          onclick: function () {
            if (SIM.hidden.has(s.pid)) SIM.hidden.delete(s.pid);
            else SIM.hidden.add(s.pid);
            SIM.chartKey = null;
            safe('sim', renderSim);
          },
        }, eqKey(s.style), s.label));
      }));
    }
    const ckey = sig([series.map(function (s) { return [s.pid, s.pts.length, s.pts[0], s.pts[s.pts.length - 1], s.pts.length > 2 ? s.pts[s.pts.length >> 1] : null]; }),
      Array.from(SIM.hidden).sort(), SIM.table, obj(d.run).start_capital, obj(d.headline).pnl_liq]);
    if (SIM.chartKey !== ckey) {
      SIM.chartKey = ckey;
      if (SIM.table) renderSimEqTable(d);
      else drawSimChart();
    }
    return p.el;
  }

  function drawSimChart() {
    const p = SIM.panels.equity;
    const d = S.data.paper;
    if (!p || !d || SIM.table) return;
    const host = p.host;
    const prev = SIM.chart;
    const restore = SIM.hoverTs != null && (!!(prev && prev.pointer) || document.activeElement === host);
    const series = eqSeries(d);
    const vis = series.filter(function (s) { return !SIM.hidden.has(s.pid) && s.pts.length; });
    if (!series.some(function (s) { return s.pts.length >= 2; })) {
      SIM.chart = null;
      rebuild(host, [h('div', { class: 'chart-empty' }, h('p', null, 'The equity curves start once each portfolio has two recorded points (at most one a minute).'))]);
      return;
    }
    if (!vis.length) {
      SIM.chart = null;
      rebuild(host, [h('div', { class: 'chart-empty' }, h('p', null, 'Every line is hidden: turn one back on in the list below the chart.'))]);
      return;
    }
    const W = Math.max(260, Math.floor(host.clientWidth || 640));
    const H = 300;
    const narrow = W < 560;
    const M = { l: 62, r: narrow ? 14 : 136, t: 16, b: 30 };
    const plotW = W - M.l - M.r;
    const plotH = H - M.t - M.b;
    const start = num(obj(d.run).start_capital);
    let t0 = Infinity, t1 = -Infinity;
    for (const s of series) for (const q of s.pts) { t0 = Math.min(t0, q[0]); t1 = Math.max(t1, q[0]); }
    if (t1 - t0 < 60) t1 = t0 + 60;
    let lo = Infinity, hi = -Infinity;
    for (const s of vis) for (const q of s.pts) { lo = Math.min(lo, q[1]); hi = Math.max(hi, q[1]); }
    if (start != null) { lo = Math.min(lo, start); hi = Math.max(hi, start); }
    if (hi - lo < 20) { const m = (hi + lo) / 2; lo = m - 10; hi = m + 10; }
    const pad = (hi - lo) * 0.08;
    lo -= pad; hi += pad;
    const step = niceMoneyStep(hi - lo, narrow ? 4 : 5);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    const X = function (t) { return M.l + ((t - t0) / (t1 - t0)) * plotW; };
    const Y = function (v) { return M.t + ((hi - v) / (hi - lo)) * plotH; };
    const svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'aria-hidden': 'true', focusable: 'false' });
    svg.appendChild(svgEl('defs', null, svgEl('clipPath', { id: 'eq-clip' }, svgEl('rect', { x: M.l, y: M.t - 4, width: plotW, height: plotH + 8 }))));
    const grid = svgEl('g', { class: 'grid' });
    const labels = svgEl('g');
    for (let v = lo, i = 0; v <= hi + step / 1000 && i < 20; v += step, i++) {
      const y = Math.round(Y(v)) + 0.5;
      if (i > 0) grid.appendChild(svgEl('line', { x1: M.l, x2: M.l + plotW, y1: y, y2: y }));
      labels.appendChild(svgEl('text', { class: 'tick', x: M.l - 8, y: y + 4, 'text-anchor': 'end' }, fmtInt(v)));
    }
    svg.appendChild(grid);
    const baseY = Math.round(Y(lo)) + 0.5;
    svg.appendChild(svgEl('line', { class: 'baseline', x1: M.l, x2: M.l + plotW, y1: baseY, y2: baseY }));
    svg.appendChild(labels);
    const xl = svgEl('g');
    for (const tk of timeTicks(t0, t1, X, M.l, M.l + plotW, SIM_TICK_STEPS)) xl.appendChild(svgEl('text', { class: 'tick', x: X(tk.t), y: H - 8, 'text-anchor': 'middle' }, tk.label));
    svg.appendChild(xl);
    if (start != null) {
      const ry = Math.round(Y(start)) + 0.5;
      svg.appendChild(svgEl('line', { class: 'ref-line', x1: M.l, x2: M.l + plotW, y1: ry, y2: ry }));
      svg.appendChild(svgEl('text', { class: 'ref-label', x: M.l + 6, y: ry - 5 }, 'Start ' + fmtInt(start)));
    }
    const ordered = vis.filter(function (s) { return !s.headline; }).concat(vis.filter(function (s) { return s.headline; }));
    for (const s of ordered) {
      let dp = '';
      s.pts.forEach(function (q, i) { dp += (i ? 'L' : 'M') + X(q[0]).toFixed(1) + ' ' + Y(q[1]).toFixed(1); });
      if (s.pts.length === 1) dp += 'h0.1';
      svg.appendChild(svgEl('path', { class: 'eq-line eq-' + s.style.group, d: dp, 'stroke-dasharray': s.style.dash || null, 'clip-path': 'url(#eq-clip)', 'data-pid': s.pid }));
    }
    const head = vis.filter(function (s) { return s.headline; })[0];
    if (head) {
      const last = head.pts[head.pts.length - 1];
      const ex = X(last[0]), ey = Y(last[1]);
      svg.appendChild(svgEl('circle', { class: 'end-dot eq-headline-dot', cx: ex.toFixed(1), cy: ey.toFixed(1), r: 4 }));
      const pnl = start != null ? last[1] - start : null;
      const text = 'You, by hand' + (pnl != null ? ' ' + fmtSignedMoney(pnl) : '');
      if (narrow) svg.appendChild(svgEl('text', { class: 'end-label', x: (ex - 8).toFixed(1), y: (ey - 9).toFixed(1), 'text-anchor': 'end' }, text));
      else svg.appendChild(svgEl('text', { class: 'end-label', x: (ex + 8).toFixed(1), y: (ey + 4).toFixed(1) }, text));
    }
    const cross = svgEl('g', { class: 'cross', visibility: 'hidden' });
    const cline = svgEl('line', { class: 'crosshair', x1: 0, x2: 0, y1: M.t, y2: M.t + plotH });
    cross.appendChild(cline);
    const dots = {};
    for (const s of vis) {
      const c = svgEl('circle', { class: 'hover-dot eq-dot-' + s.style.group, cx: 0, cy: 0, r: s.headline ? 4.5 : 3.5 });
      dots[s.pid] = c;
      cross.appendChild(c);
    }
    svg.appendChild(cross);
    const hit = svgEl('rect', { class: 'hit', x: M.l, y: M.t, width: plotW, height: plotH });
    svg.appendChild(hit);
    const tip = h('div', { class: 'tooltip eq-tooltip', hidden: true });
    rebuild(host, [svg, tip]);
    const times = eqTimes(vis);
    const chart = { times: times, vis: vis, X: X, Y: Y, M: M, W: W, H: H, plotW: plotW, cross: cross, cline: cline, dots: dots, tip: tip, index: -1, pointer: false };
    SIM.chart = chart;
    function nearest(t) {
      let a = 0, b = times.length - 1;
      while (b - a > 1) {
        const mid = (a + b) >> 1;
        if (times[mid] < t) a = mid;
        else b = mid;
      }
      return Math.abs(times[a] - t) <= Math.abs(times[b] - t) ? a : b;
    }
    chart.nearest = nearest;
    function pointAt(ev) {
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? W / rect.width : 1;
      const x = (ev.clientX - rect.left) * scale;
      chart.pointer = true;
      simShowPoint(chart, nearest(t0 + ((x - M.l) / plotW) * (t1 - t0)));
    }
    hit.addEventListener('pointermove', pointAt);
    hit.addEventListener('pointerdown', function (ev) { chart.tapped = Date.now(); pointAt(ev); });
    hit.addEventListener('pointerleave', function (ev) {
      chart.pointer = false;
      if (ev.pointerType && ev.pointerType !== 'mouse') return;
      if (document.activeElement !== host) simHidePoint(chart);
    });
    if (restore && times.length) {
      chart.pointer = !!(prev && prev.pointer);
      simShowPoint(chart, nearest(SIM.hoverTs));
    }
  }

  function simShowPoint(chart, i) {
    if (!chart || i < 0 || i >= chart.times.length) return;
    chart.index = i;
    const t = chart.times[i];
    SIM.hoverTs = t;
    const x = chart.X(t);
    chart.cline.setAttribute('x1', x.toFixed(1));
    chart.cline.setAttribute('x2', x.toFixed(1));
    let yHead = null;
    const rows = [];
    for (const s of chart.vis) {
      const v = eqValueAt(s.pts, t);
      const dot = chart.dots[s.pid];
      if (v == null) {
        dot.setAttribute('visibility', 'hidden');
        continue;
      }
      const y = chart.Y(v);
      dot.setAttribute('visibility', 'visible');
      dot.setAttribute('cx', x.toFixed(1));
      dot.setAttribute('cy', y.toFixed(1));
      if (s.headline || yHead == null) yHead = y;
      rows.push(h('div', { class: 'eq-tip-row' }, eqKey(s.style), h('strong', null, fmtInt(v)), h('span', { class: 'tooltip-sub' }, s.label)));
    }
    chart.cross.setAttribute('visibility', 'visible');
    chart.tip.replaceChildren.apply(chart.tip, [h('div', { class: 'tooltip-sub', title: fmtAbs(t) }, fmtCompact(t))].concat(rows));
    chart.tip.hidden = false;
    placeTooltip(chart, chart.tip, x, yHead == null ? chart.M.t : yHead);
  }

  function simHidePoint(chart) {
    if (!chart) return;
    chart.index = -1;
    chart.cross.setAttribute('visibility', 'hidden');
    chart.tip.hidden = true;
  }

  function simChartFocus(focused) {
    const chart = SIM.chart;
    if (!chart || !chart.times.length) return;
    if (focused) {
      if (chart.tapped && Date.now() - chart.tapped < 1000 && chart.index >= 0) return;
      simShowPoint(chart, chart.index >= 0 ? chart.index : chart.times.length - 1);
    } else if (!chart.pointer) simHidePoint(chart);
  }

  function simChartKey(ev) {
    const chart = SIM.chart;
    if (!chart || !chart.times.length) return;
    const n = chart.times.length;
    let i = chart.index >= 0 ? chart.index : n - 1;
    if (ev.key === 'ArrowLeft') i -= 1;
    else if (ev.key === 'ArrowRight') i += 1;
    else if (ev.key === 'PageUp') i -= 10;
    else if (ev.key === 'PageDown') i += 10;
    else if (ev.key === 'Home') i = 0;
    else if (ev.key === 'End') i = n - 1;
    else return;
    ev.preventDefault();
    simShowPoint(chart, clamp(i, 0, n - 1));
    const t = chart.times[chart.index];
    const parts = chart.vis.map(function (s) { const v = eqValueAt(s.pts, t); return v == null ? null : s.label + ' ' + fmtInt(v); }).filter(Boolean);
    const text = fmtAbs(t) + ': ' + parts.join('; ') + '. Time ' + (chart.index + 1) + ' of ' + n + '.';
    clearTimeout(SIM.liveTimer);
    SIM.liveTimer = setTimeout(function () { const p = SIM.panels.equity; if (p) setText(p.readout, text); }, 250);
  }

  function renderSimEqTable(d) {
    const p = SIM.panels.equity;
    const series = eqSeries(d);
    const times = eqTimes(series);
    const k = Math.min(50, times.length);
    const picked = [];
    for (let i = 0; i < k; i++) {
      const t = times[k === 1 ? times.length - 1 : Math.round(i * (times.length - 1) / (k - 1))];
      if (picked[picked.length - 1] !== t) picked.push(t);
    }
    picked.reverse();
    if (!picked.length) {
      rebuild(p.tableBox, [h('p', { class: 'chart-empty' }, 'No equity points yet.')]);
      return;
    }
    const table = h('table', { class: 'data-table sim-table eq-table' },
      h('caption', { class: 'sr-only' }, 'Equity at liquidation value of every portfolio, up to 50 evenly spaced times, newest first'),
      h('thead', null, h('tr', null, h('th', { scope: 'col', class: 'col-time' }, 'Time'),
        series.map(function (s) { return h('th', { scope: 'col', class: 'num', title: s.full || null }, s.label); }))),
      h('tbody', null, picked.map(function (t) {
        return h('tr', null, h('th', { scope: 'row', class: 'col-time', title: fmtAbs(t) }, fmtTableTime(t)),
          series.map(function (s) { const v = eqValueAt(s.pts, t); return h('td', { class: 'num' }, v == null ? DASH : fmtInt(v)); }));
      })));
    rebuild(p.tableBox, [tableWrap('Equity table', table, 'pin-first')]);
  }

  // ---- portfolios table

  function execLines(ex) {
    ex = obj(ex);
    const kinds = Object.keys(ex);
    if (!kinds.length) return h('p', { class: 'muted-note' }, 'No orders yet.');
    return h('ul', { class: 'exec-lines' }, kinds.map(function (k) {
      const e = obj(ex[k]);
      const kk = KINDS[k];
      const bits = [plural(e.entries, 'entry', 'entries') + ', ' + fmtInt(e.filled) + ' filled' + (num(e.fill_rate) != null ? ' (' + fmtPct0(e.fill_rate) + ')' : '')];
      if (num(e.avg_slippage) != null) bits.push('average slippage ' + fmtSigned(e.avg_slippage) + ' per share');
      if (num(e.unfilled) > 0) bits.push(plural(e.unfilled, 'signal') + ' that did not fill would show ' + fmtSignedMoney(e.unfilled_pnl_now) + ' now');
      if (num(e.exits) > 0) bits.push(plural(e.exits, 'exit') + (num(e.avg_exit_slippage) != null ? ', exit slippage ' + fmtSigned(e.avg_exit_slippage) : ''));
      return h('li', null, h('strong', null, (kk ? kk[1] : k) + ': '), bits.join('; '));
    }));
  }

  function portfolioRow(pt, d) {
    const pid = str(pt.portfolio_id);
    const v = obj(pt.verdict);
    const start = num(pt.start_capital);
    const fv = num(pt.equity_fv);
    const closed = num(v.closed_trades) != null ? v.closed_trades : pt.trades_closed;
    const open = num(v.open_ideas) != null ? v.open_ideas : pt.positions_open;
    const wr = num(pt.win_rate);
    const exKey = 'exec:' + pid;
    // "why no fills": one sentence per kind; the first under the label, the rest in the disclosure
    const why = str(pt.no_trade_reason).split(/(?<=\.)\s+(?=[A-Z0-9])/).map(function (t) { return t.trim(); }).filter(Boolean);
    return h('tr', { 'data-pid': pid, class: pt.headline === true || pid === headlinePid(d) ? 'is-headline' : null },
      h('th', { scope: 'row', class: 'cell-title' },
        h('span', { class: 'title-text', title: str(pt.label) || null }, shortPortfolio(pid, pt.label)),
        why.length ? h('span', { class: 'cell-sub wrap no-trade' }, why[0] + (why.length > 1 ? ' (+' + plural(why.length - 1, 'more reason') + ' below)' : '')) : null,
        simDetails(exKey, 'Execution by kind', [why.length > 1 ? h('ul', { class: 'exec-lines why-lines' }, why.slice(1).map(function (t) { return h('li', null, t); })) : null,
          execLines(pt.execution)], 'exec-details')),
      h('td', { class: 'num' }, pnlEl(pt.pnl_liq)),
      h('td', { class: 'num' }, fmtSignedPct(pt.pnl_liq_pct)),
      h('td', { class: 'num' }, fmtInt(closed) + ' / ' + fmtInt(open)),
      h('td', { class: 'num' }, wr == null ? DASH : fmtPct0(wr), h('span', { class: 'cell-sub' }, fmtInt(pt.wins) + ' of ' + fmtInt(pt.trades_closed))),
      h('td', { class: 'num' }, fmtPctOf(pt.max_drawdown), h('span', { class: 'cell-sub' }, fmtMoney(pt.max_drawdown_abs, 0))),
      h('td', { class: 'num' }, fmtInt(pt.positions_open)),
      h('td', { class: 'num' }, num(pt.latency_s) == null ? DASH : fmtSpan(pt.latency_s)),
      h('td', { class: 'cell-verdict' }, str(v.level) ? levelBadge(v) : DASH),
      h('td', { class: 'num' }, fv == null || start == null ? DASH : fmtSignedMoney(fv - start)));
  }

  function renderSimPortfolios(d) {
    const p = simPanel('portfolios', 'Portfolios');
    const ports = simPortfolios(d);
    fillPanel(p, sig([ports, d.table_warning, d.model_label, headlinePid(d), currency()]), function () {
      const groups = SIM_GROUPS.map(function (g) { return { title: g[1], rows: ports.filter(function (pt) { return str(pt.portfolio_id).indexOf(g[0]) === 0; }) }; });
      const other = ports.filter(function (pt) { return !SIM_GROUPS.some(function (g) { return str(pt.portfolio_id).indexOf(g[0]) === 0; }); });
      if (other.length) groups.push({ title: 'Other portfolios', rows: other });
      const cols = 10;
      const table = h('table', { class: 'data-table sim-table portfolios-table' },
        h('caption', { class: 'sr-only' }, 'Every simulated portfolio at liquidation value'),
        h('thead', null, h('tr', null,
          h('th', { scope: 'col' }, 'Portfolio'),
          h('th', { scope: 'col', class: 'num' }, 'P&L at liquidation'),
          h('th', { scope: 'col', class: 'num' }, '%'),
          h('th', { scope: 'col', class: 'num', title: 'Ideas entered: closed / still open' }, 'Ideas (closed / open)'),
          h('th', { scope: 'col', class: 'num' }, 'Win rate', h('span', { class: 'cell-sub' }, 'closed trades only')),
          h('th', { scope: 'col', class: 'num' }, 'Max drawdown'),
          h('th', { scope: 'col', class: 'num' }, 'Open positions'),
          h('th', { scope: 'col', class: 'num' }, 'Latency'),
          h('th', { scope: 'col' }, 'Verdict'),
          h('th', { scope: 'col', class: 'num' }, 'Model’s own valuation', h('span', { 'aria-hidden': 'true' }, ' *'), h('span', { class: 'sr-only' }, ' (see the note under the table)')))),
        groups.filter(function (g) { return g.rows.length; }).map(function (g) {
          return h('tbody', null,
            h('tr', { class: 'group-row' }, h('th', { scope: 'rowgroup', colspan: String(cols) }, h('span', { class: 'group-label' }, g.title))),
            g.rows.map(function (pt) { return portfolioRow(pt, d); }));
        }));
      return [
        str(d.table_warning) ? h('p', { class: 'table-warning' }, icon('warn'), str(d.table_warning)) : null,
        ports.length ? tableWrap('Portfolios table', table, 'pin-first') : h('p', { class: 'muted-note' }, 'No portfolios yet.'),
        str(d.model_label) ? h('p', { class: 'muted-note footnote' }, '* ' + str(d.model_label)) : null,
      ];
    });
    return p.el;
  }

  // ---- chaser sizing

  function renderSimChaser(d) {
    const hid = headlinePid(d);
    const ports = simPortfolios(d).filter(function (pt) {
      return pt.sizing && (str(pt.portfolio_id) === 'policy:chaser' || (str(pt.portfolio_id) === hid && str(pt.policy) === 'chaser'));
    });
    if (!ports.length) return null;
    const p = simPanel('chaser', 'Chaser sizing');
    fillPanel(p, sig([ports.map(function (pt) { return [pt.portfolio_id, pt.label, pt.sizing]; }), currency()]), function () {
      return ports.map(function (pt) {
        const sz = obj(pt.sizing);
        return h('div', { class: 'chaser-block' },
          ports.length > 1 ? h('h4', { class: 'card-section-title' }, shortPortfolio(pt.portfolio_id, pt.label)) : null,
          sizingFacts(sz),
          texts(sz.lines).length ? h('ul', { class: 'notes sizing-lines' }, texts(sz.lines).map(function (t) { return h('li', null, t); })) : null);
      }).concat([h('p', { class: 'muted-note sim-flag-line' }, icon('warn'),
        'The chaser accepts large, correlated swings to try to reach the top 3; it can also lose most of its capital.')]);
    });
    return p.el;
  }

  // ---- open positions and baskets

  function sidePill(side) {
    const s = str(side).toUpperCase() || '?';
    return h('span', { class: 'side-pill' }, s);
  }

  function outcomeLink(eid, title, option, keyPrefix) {
    const text = outcomeLabel(title, str(option));
    if (!hasId(eid)) return document.createTextNode(text);
    return h('a', { href: exchangeHref(eid), 'data-eid': String(eid), 'data-focus-key': (keyPrefix || 'sim') + ':' + eid }, text);
  }

  function positionFlags(pos) {
    const keys = texts(pos.flags).slice();
    if (str(pos.depth_state) === 'stale' && keys.indexOf('depth_stale') === -1) keys.push('depth_stale');
    if (str(pos.depth_state) === 'unknown' && keys.indexOf('depth_unknown') === -1 && str(pos.status) !== 'frozen') keys.push('depth_unknown');
    if (pos.bold === true) keys.push('bold');
    const box = h('span', { class: 'flags' });
    for (const k of keys) box.appendChild(h('span', { class: 'flag' }, icon(k === 'bold' ? 'flame' : k === 'closed_no_ruling' ? 'warn' : 'info'), k === 'bold' ? 'bold bet' : POSITION_FLAGS[k] || k.replace(/_/g, ' ')));
    if (!box.childNodes.length) box.appendChild(h('span', { class: 'nil' }, h('span', { 'aria-hidden': 'true' }, DASH), h('span', { class: 'sr-only' }, 'None')));
    return box;
  }

  function simFilterSelect() {
    if (SIM.posSelect) return SIM.posSelect;
    const sel = h('select', { id: 'sim-pos-filter' });
    sel.addEventListener('change', function () { SIM.posFilter = sel.value; safe('sim', renderSim); });
    SIM.posSelect = sel;
    SIM.posFilterBox = h('div', { class: 'sim-filter' }, h('label', { for: 'sim-pos-filter' }, 'Portfolio'), sel);
    return sel;
  }

  function renderSimPositions(d) {
    const sel = simFilterSelect();
    const p = simPanel('positions', 'Open positions', [SIM.posFilterBox]);
    const labels = portfolioLabels(d);
    const pids = Object.keys(labels);
    const okey = sig(pids.map(function (k) { return [k, labels[k]]; }));
    if (sel.dataset.sig !== okey) {
      sel.dataset.sig = okey;
      sel.replaceChildren.apply(sel, [h('option', { value: '' }, 'All portfolios')].concat(pids.map(function (k) { return h('option', { value: k }, labels[k]); })));
    }
    if (SIM.posFilter && pids.indexOf(SIM.posFilter) === -1) SIM.posFilter = '';
    // Until the user picks one, show the headline (you, by hand): every portfolio at once is a long table.
    let filter = SIM.posFilter == null ? headlinePid(d) : SIM.posFilter;
    if (filter && pids.indexOf(filter) === -1) filter = '';
    if (sel.value !== filter) sel.value = filter;
    const all = objs(d.positions);
    const pos = all.filter(function (x) { return !filter || str(x.portfolio_id) === filter; });
    const baskets = objs(d.baskets).filter(function (x) { return !filter || str(x.portfolio_id) === filter; });
    fillPanel(p, sig([pos, baskets, labels, filter, currency()]), function () {
      const out = [];
      if (!pos.length) out.push(h('p', { class: 'muted-note' }, filter ? 'No open positions in this portfolio.' : 'No open positions.'));
      else {
        out.push(tableWrap('Open positions table', h('table', { class: 'data-table sim-table positions-table' },
          h('caption', { class: 'sr-only' }, 'Open simulated positions, valued at liquidation'),
          h('thead', null, h('tr', null,
            h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col' }, 'Outcome'), h('th', { scope: 'col' }, 'Side'),
            h('th', { scope: 'col', class: 'num' }, 'Shares'), h('th', { scope: 'col', class: 'num' }, 'Avg cost'),
            h('th', { scope: 'col', class: 'num' }, 'Liquidation value'), h('th', { scope: 'col', class: 'num' }, 'Unrealised'),
            h('th', { scope: 'col' }, 'Exit plan'), h('th', { scope: 'col' }, 'Flags'))),
          h('tbody', null, pos.map(function (x) {
            return h('tr', { 'data-pid': str(x.portfolio_id), 'data-eid': str(x.exchange_id) },
              h('th', { scope: 'row', class: 'cell-portfolio' }, labels[str(x.portfolio_id)] || str(x.portfolio_id)),
              h('td', { class: 'cell-outcome' }, outcomeLink(x.exchange_id, x.title, x.option, 'pos:' + str(x.portfolio_id)), h('span', { class: 'cell-sub' }, kindBadge(x.kind))),
              h('td', null, sidePill(x.side)),
              h('td', { class: 'num' }, fmtInt(x.qty)),
              h('td', { class: 'num' }, fmtPrice(x.avg_cost)),
              h('td', { class: 'num' }, fmtMoney(x.liq_value, 2)),
              h('td', { class: 'num' }, pnlEl(x.unrealized_liq)),
              h('td', { class: 'cell-wrap' }, str(x.exit_note) || str(obj(x.exit_plan).note) || DASH),
              h('td', null, positionFlags(x)));
          }))), 'pin-first'));
      }
      if (baskets.length) {
        const byPos = {};
        for (const x of all) byPos[str(x.portfolio_id) + ':' + str(x.exchange_id)] = x;
        out.push(h('h4', { class: 'card-section-title sim-sub-title' }, 'Baskets'),
          h('p', { class: 'muted-note' }, 'Right after entry a set is worth less at liquidation than its cost: that is the spread, not a loss of the locked edge.'),
          tableWrap('Baskets table', h('table', { class: 'data-table sim-table baskets-table' },
            h('caption', { class: 'sr-only' }, 'Open baskets: the floor they pay at a 1/0 settlement next to what selling them now would fetch'),
            h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col' }, 'Legs'), h('th', { scope: 'col', class: 'num' }, 'Sets'),
              h('th', { scope: 'col', class: 'num' }, 'Cost'), h('th', { scope: 'col', class: 'num' }, 'Floor at settlement'), h('th', { scope: 'col', class: 'num' }, 'Now at liquidation'))),
            h('tbody', null, baskets.map(function (b) {
              const pid = str(b.portfolio_id);
              return h('tr', null,
                h('th', { scope: 'row', class: 'cell-portfolio' }, labels[pid] || pid),
                h('td', null, h('ul', { class: 'legs' }, objs(b.legs).map(function (l) {
                  const ps = byPos[pid + ':' + str(l.exchange_id)] || {};
                  return h('li', null, h('span', { class: 'leg-action' }, str(l.side).toUpperCase() + ' ' + fmtInt(l.qty)), ' ',
                    outcomeLink(l.exchange_id, ps.title || ('Outcome ' + str(l.exchange_id)), ps.option, 'leg:' + pid),
                    h('span', { class: 'leg-market' }, ' · ' + fmtMoney(l.liq_value, 2) + ' now'));
                }))),
                h('td', { class: 'num' }, fmtInt(b.sets)),
                h('td', { class: 'num' }, fmtMoney(b.cost, 2)),
                h('td', { class: 'num' }, fmtMoney(b.floor_value, 2)),
                h('td', { class: 'num' }, fmtMoney(b.liq_value, 2)));
            }))), 'pin-first'));
      }
      return out;
    });
    return p.el;
  }

  // ---- recent fills and closed trades

  function fillText(f) {
    const act = str(f.action);
    const verb = act === 'sell' ? 'Sold' : act === 'settle' ? 'Settled' : 'Bought';
    return verb + ' ' + fmtInt(f.qty) + ' ' + (str(f.side).toUpperCase() || '?') + ' @ ' + fmtPrice(f.price);
  }

  function renderSimFills(d) {
    const p = simPanel('fills', 'Recent fills');
    const fills = objs(d.fills);
    const labels = portfolioLabels(d);
    fillPanel(p, sig([fills, labels, SIM.fillsAll]), function () {
      if (!fills.length) return [h('p', { class: 'muted-note' }, 'No simulated fills yet. Orders wait for an order book read after the signal (about 4 minutes for the headline).')];
      const shown = SIM.fillsAll ? fills : fills.slice(0, 20);
      const out = [tableWrap('Recent fills table', h('table', { class: 'data-table sim-table fills-table' },
        h('caption', { class: 'sr-only' }, 'Simulated fills, newest first'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Time'), h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col' }, 'Fill'),
          h('th', { scope: 'col', class: 'num', title: 'Fill price minus the price seen at the decision (positive = worse)' }, 'Slippage'),
          h('th', { scope: 'col' }, 'Kind'), h('th', { scope: 'col' }, 'Reason'))),
        h('tbody', null, shown.map(function (f) {
          const slip = num(f.slippage);
          return h('tr', null,
            h('th', { scope: 'row', class: 'col-time', title: fmtAbs(f.ts) }, fmtTableTime(f.ts)),
            h('td', { class: 'cell-portfolio' }, labels[str(f.portfolio_id)] || str(f.portfolio_id)),
            h('td', { class: 'cell-outcome' }, h('strong', { class: 'fill-text' }, fillText(f)),
              h('span', { class: 'cell-sub wrap' }, outcomeLink(f.exchange_id, f.title, f.option, 'fill:' + str(f.fill_id))),
              f.synthetic === true ? h('span', { class: 'cell-sub' }, 'from candles (assumed)') : null),
            h('td', { class: 'num' }, str(f.action) === 'settle' || slip == null ? DASH : fmtSigned(slip),
              slip == null || str(f.action) === 'settle' ? null : h('span', { class: 'cell-sub' }, Math.abs(slip) < 0.0005 ? 'at the decision price' : slip > 0 ? 'worse' : 'better')),
            h('td', null, kindBadge(f.kind)),
            h('td', { class: 'cell-wrap cell-reason' }, str(f.reason) || DASH));
        }))), 'pin-first')];
      if (fills.length > 20) {
        out.push(h('button', {
          type: 'button', class: 'btn btn-ghost', 'data-focus-key': 'fills-more', 'aria-expanded': SIM.fillsAll ? 'true' : 'false',
          onclick: function () { SIM.fillsAll = !SIM.fillsAll; safe('sim', renderSim); },
        }, SIM.fillsAll ? 'Show the newest 20' : 'Show all ' + fmtInt(fills.length) + ' fills'));
      }
      return out;
    });
    return p.el;
  }

  function renderSimTrades(d) {
    const p = simPanel('trades', 'Closed trades');
    const trades = objs(d.trades);
    const labels = portfolioLabels(d);
    fillPanel(p, sig([trades, labels]), function () {
      if (!trades.length) return [h('p', { class: 'muted-note' }, 'No closed trades yet. Value and carry ideas often stay open until their target or the settlement.')];
      return [tableWrap('Closed trades table', h('table', { class: 'data-table sim-table trades-table' },
        h('caption', { class: 'sr-only' }, 'Closed simulated trades, newest first'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Closed'), h('th', { scope: 'col' }, 'Kind'), h('th', { scope: 'col' }, 'Outcome'),
          h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col', class: 'num' }, 'P&L'), h('th', { scope: 'col' }, 'Exit reason'), h('th', { scope: 'col', class: 'num' }, 'Hold time'))),
        h('tbody', null, trades.map(function (t) {
          const eids = texts(t.exchange_ids);
          const reason = str(t.exit_reason);
          return h('tr', null,
            h('th', { scope: 'row', class: 'col-time', title: fmtAbs(t.closed_at) }, fmtTableTime(t.closed_at)),
            h('td', null, kindBadge(t.kind)),
            h('td', { class: 'cell-outcome' }, eids.length === 1 ? outcomeLink(eids[0], t.title, null, 'trade:' + str(t.trade_id)) : str(t.title) || DASH,
              eids.length > 1 ? h('span', { class: 'cell-sub' }, plural(eids.length, 'leg') + ', ' + fmtInt(t.qty) + ' sets') : null),
            h('td', { class: 'cell-portfolio' }, labels[str(t.portfolio_id)] || str(t.portfolio_id)),
            h('td', { class: 'num' }, pnlEl(t.pnl), num(t.return_pct) != null ? h('span', { class: 'cell-sub' }, fmtSignedPct(t.return_pct, 1)) : null),
            h('td', null, EXIT_REASONS[reason] || reason.replace(/_/g, ' ') || DASH),
            h('td', { class: 'num' }, num(t.hold_hours) == null ? DASH : fmtSpan(t.hold_hours * 3600)));
        }))), 'pin-first')];
    });
    return p.el;
  }

  // ---- backtest

  function coverageSentences(c) {
    c = obj(c);
    const out = [];
    if (num(c.exchanges) != null || num(c.decision_steps) != null) out.push(plural(c.exchanges, 'outcome') + ', ' + plural(c.decision_steps, 'decision step') + '.');
    if (num(c.tick_share) != null || num(c.candle_share) != null) {
      out.push('Quotes: ' + fmtPct0(c.tick_share) + ' from live snapshots, ' + fmtPct0(c.candle_share) + ' from candles (spread assumed).');
    }
    if (num(c.book_snapshot_share) != null || num(c.synthetic_book_share) != null) {
      out.push('Fills: ' + fmtPct0(c.book_snapshot_share) + ' from real order books, ' + fmtPct0(c.synthetic_book_share) + ' from synthetic books (left out of the verdict).');
    }
    if (num(c.fair_value_share) != null) {
      out.push('Outside fair values usable on ' + fmtPct0(c.fair_value_share) + ' of steps' +
        (num(c.fair_value_recorded_share) != null ? ' (' + fmtPct0(c.fair_value_recorded_share) + ' recorded, ' + fmtPct0(c.fair_value_imported_share) + ' imported)' : '') + '.');
    }
    if (num(c.maker_fills_tape) != null || num(c.maker_fills_candle) != null) {
      out.push('Resting-order fills: ' + fmtInt(c.maker_fills_tape) + ' from the stored tape, ' + fmtInt(c.maker_fills_candle) + ' from candles (assumed).');
    }
    if (num(c.tape_share) != null) out.push('Stored tape for ' + fmtPct0(c.tape_share) + ' of the resting-order checks.');
    if (num(c.set_tick_share) != null) out.push('Set ideas priced from one snapshot on ' + fmtPct0(c.set_tick_share) + ' of steps.');
    if (num(c.guard_filtered) != null) out.push('The look-ahead guard held back ' + plural(c.guard_filtered, 'stored row') + ' newer than each decision.');
    return out;
  }

  function testabilityTable(t) {
    t = obj(t);
    const order = ['value', 'basket', 'hole', 'fade', 'carry', 'arbitrage'];
    const kinds = order.filter(function (k) { return t[k]; }).concat(Object.keys(t).filter(function (k) { return order.indexOf(k) === -1; }));
    if (!kinds.length) return null;
    return tableWrap('What this replay can test', h('table', { class: 'data-table sim-table testability-table' },
      h('caption', { class: 'sr-only' }, 'What this replay can test, per kind of idea'),
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Kind'), h('th', { scope: 'col' }, 'Can it be tested?'), h('th', { scope: 'col' }, 'Why'))),
      h('tbody', null, kinds.map(function (k) {
        const row = obj(t[k]);
        const st = TESTABILITY[str(row.status)] || ['question', str(row.status).replace(/_/g, ' ') || 'Unknown'];
        return h('tr', { 'data-kind': k },
          h('th', { scope: 'row' }, kindBadge(k)),
          h('td', null, h('span', { class: 'yes-no testable-' + (TESTABILITY[str(row.status)] ? str(row.status) : 'unknown') }, icon(st[0]), st[1])),
          h('td', { class: 'cell-wrap' }, str(row.sentence) || DASH));
      }))));
  }

  function backtestReport(r, labels) {
    r = obj(r);
    const out = [];
    const testability = testabilityTable(r.testability);
    if (testability) out.push(h('h4', { class: 'card-section-title' }, 'What this replay can test'), testability);
    const w = obj(r.window);
    const warnings = texts(r.warnings);
    const overlap = warnings.filter(function (x) { return /independent check/i.test(x); });
    const sweepWarn = warnings.filter(function (x) { return /optimistic estimate/i.test(x); });
    const rest = warnings.filter(function (x) { return overlap.indexOf(x) === -1 && sweepWarn.indexOf(x) === -1; });
    if (num(w.start) != null || num(w.end) != null) {
      out.push(h('p', { class: 'bt-window' }, 'Window: ', timeEl(w.start, 'short'), ' – ', timeEl(w.end, 'short'),
        ' (' + fmtHours(w.hours) + (num(w.steps) != null ? ', ' + plural(w.steps, 'step') : '') + ')' + (r.stopped_early === true ? ' · stopped early (time budget)' : '')));
    }
    if (overlap.length || num(r.overlap_hours) > 0) {
      out.push(h('div', { class: 'banner banner-warning bt-overlap' }, icon('warn', 'icon-lg'), h('div', { class: 'banner-body' },
        h('p', null, overlap[0] || fmtHours(r.overlap_hours) + ' of this window were also seen by the live paper run: a backtest over the same data is not an independent check, so agreement between the two is not evidence.'))));
    }
    const cov = coverageSentences(r.coverage);
    if (cov.length) out.push(h('h4', { class: 'card-section-title' }, 'Coverage'), h('ul', { class: 'notes' }, cov.map(function (x) { return h('li', null, x); })));
    if (texts(r.assumptions).length) out.push(h('h4', { class: 'card-section-title' }, 'Assumptions'), h('ul', { class: 'notes' }, texts(r.assumptions).map(function (x) { return h('li', null, x); })));
    const ports = objs(r.portfolios);
    const verdicts = obj(r.verdicts);
    if (ports.length) {
      out.push(h('h4', { class: 'card-section-title' }, 'Portfolios in the replay'),
        tableWrap('Backtest portfolios', h('table', { class: 'data-table sim-table bt-table' },
          h('caption', { class: 'sr-only' }, 'Each portfolio over the replayed window, at liquidation value'),
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Portfolio'), h('th', { scope: 'col', class: 'num' }, 'P&L at liquidation'),
            h('th', { scope: 'col', class: 'num' }, 'Ideas (closed / open)'), h('th', { scope: 'col' }, 'Verdict'))),
          h('tbody', null, ports.map(function (pt) {
            const v = obj(verdicts[str(pt.portfolio_id)] || pt.verdict);
            return h('tr', null, h('th', { scope: 'row' }, labels[str(pt.portfolio_id)] || shortPortfolio(pt.portfolio_id, pt.label)),
              h('td', { class: 'num' }, pnlEl(pt.pnl_liq)),
              h('td', { class: 'num' }, fmtInt(num(v.closed_trades) != null ? v.closed_trades : pt.trades_closed) + ' / ' + fmtInt(num(v.open_ideas) != null ? v.open_ideas : pt.positions_open)),
              h('td', null, str(v.level) ? levelBadge(v) : DASH));
          }))), 'pin-first'));
    }
    if (r.study && typeof r.study === 'object') {
      out.push(h('h4', { class: 'card-section-title' }, 'Signal study over the replay'), studyTable(r.study, 'Signal study over the replayed window'));
    }
    const sweep = objs(r.sweep);
    if (sweep.length) {
      out.push(h('h4', { class: 'card-section-title' }, 'Settings sweep (headline portfolio)'));
      for (const x of sweepWarn) out.push(h('p', { class: 'table-warning' }, icon('warn'), x));
      out.push(tableWrap('Settings sweep', h('table', { class: 'data-table sim-table sweep-table' },
        h('caption', { class: 'sr-only' }, 'The headline portfolio under each combination of settings'),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Settings'), h('th', { scope: 'col', class: 'num' }, 'P&L at liquidation'),
          h('th', { scope: 'col', class: 'num' }, 'Closed trades'), h('th', { scope: 'col' }, 'Verdict'), h('th', { scope: 'col', class: 'num' }, 'Max drawdown'))),
        h('tbody', null, sweep.map(function (s) {
          return h('tr', null, h('th', { scope: 'row', class: 'cell-wrap' }, str(s.label) || sig(s.params)), h('td', { class: 'num' }, pnlEl(s.pnl_liq)),
            h('td', { class: 'num' }, fmtInt(s.trades_closed)), h('td', null, str(s.verdict_level) ? levelBadge({ level: s.verdict_level }) : DASH),
            h('td', { class: 'num' }, fmtPctOf(s.max_drawdown)));
        })))));
    }
    if (rest.length) out.push(h('h4', { class: 'card-section-title' }, 'Warnings'), h('ul', { class: 'notes bt-warnings' }, rest.map(function (x) { return h('li', null, x); })));
    return out;
  }

  function renderSimBacktest() {
    const p = simPanel('backtest', 'Backtest: the same simulator over stored history');
    const b = S.data.backtest;
    const labels = portfolioLabels(S.data.paper);
    fillPanel(p, sig([b && b.status, b && b.error, b && b.generated_at, b && b.report, labels, !b && viewError('backtest') ? 'err' : '']), function () {
      if (!b) return [h('p', { class: 'muted-note' }, loadingMsg('backtest', 'Loading the backtest…'))];
      const status = str(b.status);
      const out = [];
      if (status === 'pending') {
        out.push(h('p', { class: 'pending-note bt-status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
          h('span', null, 'Replaying the stored history through the same simulator. This takes up to two minutes; the page checks again every 30 s.')));
      } else if (status === 'ready') {
        out.push(h('p', { class: 'bt-status' }, icon('checkCircle'), h('span', null, 'Ready', num(b.generated_at) != null ? [': generated ', timeEl(b.generated_at, 'ago')] : '',
          '. A replay of the past is not a forecast.')));
      } else if (status === 'no_data') {
        out.push(h('p', { class: 'bt-status' }, icon('info'), h('span', null, str(b.error) || 'Less than 1 hour of prices is stored yet: nothing to replay.')));
      } else if (status === 'unavailable') {
        out.push(h('p', { class: 'bt-status' }, icon('info'), h('span', null, str(b.error) || 'The backtest is not available.')));
      } else {
        out.push(h('p', { class: 'bt-status' }, icon('warn'), h('span', null, (str(b.error) || 'The backtest failed.') + ' It retries in a minute.')));
      }
      if (b.report && typeof b.report === 'object') {
        if (status !== 'ready') out.push(h('p', { class: 'muted-note' }, 'The previous result:'));
        return out.concat(backtestReport(b.report, labels));
      }
      return out;
    });
    return p.el;
  }

  // ---- fair values

  function raceText(key, party) {
    const k = str(key);
    const parts = k.split(':');
    let t = k;
    if (parts.length >= 3) {
      const office = parts[1], where = parts.slice(2).join(':');
      if (office === 'SENATE_CONTROL') t = 'Senate control';
      else if (office === 'HOUSE_CONTROL') t = 'House control';
      else t = where + ' ' + office.charAt(0) + office.slice(1).toLowerCase();
    }
    return (t || DASH) + (str(party) ? ' (' + str(party) + ')' : '');
  }

  function copySnippet(btn, pre) {
    const text = pre.textContent;
    const done = function (ok) {
      if (ok) {
        setText(btn.querySelector('.copy-label'), 'Copied');
        announce('Copied to the clipboard.');
        setTimeout(function () { if (btn.isConnected) setText(btn.querySelector('.copy-label'), 'Copy'); }, 2000);
        return;
      }
      // no clipboard access (an http page, or permission denied): select the text so Ctrl+C copies it
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      announce('Copying is blocked here: the text is selected, press Ctrl+C (or ⌘C) to copy it.');
      setText(btn.querySelector('.copy-label'), 'Selected: press Ctrl+C');
    };
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
        return;
      }
    } catch (e) {
      /* fall through to selecting the text */
    }
    done(false);
  }

  function snippetBlock(key, label, text) {
    const pre = h('pre', { class: 'snippet' }, text);
    const btn = h('button', { type: 'button', class: 'btn btn-copy', 'data-focus-key': 'copy:' + key },
      icon('copy'), h('span', { class: 'copy-label' }, 'Copy'), h('span', { class: 'sr-only' }, ': ' + label));
    btn.addEventListener('click', function () { copySnippet(btn, pre); });
    return h('div', { class: 'snippet-block' }, h('div', { class: 'snippet-head' }, h('span', null, label), btn), pre);
  }

  function fvRow(r, mapPath) {
    const fair = r.fair && typeof r.fair === 'object' ? obj(r.fair) : null;
    const eid = str(r.exchange_id);
    const snippets = obj(r.snippets);
    const matches = objs(r.matches);
    const fix = SNIPPETS.filter(function (s) { return str(snippets[s[0]]); });
    const usable = fair ? fair.usable === true : false;
    return h('tr', { 'data-eid': eid, class: r.suspect === true ? 'is-suspect' : null },
      h('th', { scope: 'row', class: 'cell-title' }, outcomeLink(eid, r.title, r.option, 'fv')),
      h('td', null, r.race_key ? raceText(r.race_key, r.party) : DASH),
      h('td', { class: 'num' }, fair && num(fair.value) != null ? fmtPrice(fair.value) : DASH,
        fair && num(fair.value) != null ? h('span', { class: 'cell-sub wrap' }, [fvSourceLabel(fair.source), str(fair.confidence) ? str(fair.confidence) + ' confidence' : '',
          num(fair.uncertainty) != null ? fmtCents(fair.uncertainty) : '', num(fair.age_s) != null ? fmtSpan(fair.age_s) + ' old' : ''].filter(Boolean).join(' · ')) : null),
      h('td', { class: 'num' }, fmtPrice(r.sm_bid) + ' / ' + fmtPrice(r.sm_ask)),
      h('td', { class: 'num', title: 'Fair value minus the Cup mid (positive: the Cup is below the fair value)' }, num(r.gap) == null ? DASH : fmtSigned(r.gap),
        num(r.edge_yes) != null || num(r.edge_no) != null ? h('span', { class: 'cell-sub' }, 'YES ' + fmtSigned(r.edge_yes) + ' · NO ' + fmtSigned(r.edge_no)) : null),
      h('td', { class: 'cell-matches' },
        matches.length ? h('ul', { class: 'match-list' }, matches.map(function (m) {
          return h('li', null, h('strong', null, (PROVIDER_NAMES[str(m.venue)] || str(m.venue)) + ' ' + str(m.external_id)),
            str(m.label) ? ' ' + str(m.label) : '', str(m.kind) === 'NEAR' ? ' (near match)' : '',
            str(m.reason) ? h('span', { class: 'cell-sub wrap' }, str(m.reason)) : null);
        })) : h('span', { class: 'muted-note' }, str(r.unmatched_reason) || 'No outside market matched.'),
        fix.length ? simDetails('fix:' + eid, 'Fix this match', h('div', { class: 'sim-details-body' },
          fix.map(function (s) { return snippetBlock(eid + ':' + s[0], s[1], str(snippets[s[0]])); }),
          h('p', { class: 'muted-note' }, 'Paste it under "overrides" in ' + (mapPath || 'the match file') + '; it applies within a minute.')), 'fix-details') : null),
      h('td', { class: 'cell-usable' }, fair ? yesNo(usable, 'Yes', 'No') : yesNo(false, 'Yes', 'No'),
        fair && str(fair.reason) ? h('span', { class: 'cell-sub wrap' }, str(fair.reason)) : null,
        r.suspect === true || r.near === true ? h('span', { class: 'flags' },
          r.suspect === true ? h('span', { class: 'flag flag-suspect' }, icon('warn'), 'suspect match') : null,
          r.near === true ? h('span', { class: 'flag' }, icon('info'), 'near match: shown, not traded') : null) : null));
  }

  function fvControls() {
    if (SIM.fvControls) return SIM.fvControls;
    const search = h('input', { type: 'search', id: 'sim-fv-search', placeholder: 'e.g. Texas, Senate', autocomplete: 'off', spellcheck: 'false' });
    search.addEventListener('input', function () { SIM.fvQuery = search.value; safe('sim', renderSim); });
    const usable = h('input', { type: 'checkbox', id: 'sim-fv-usable' });
    usable.addEventListener('change', function () { SIM.fvUsable = usable.checked; safe('sim', renderSim); });
    SIM.fvControls = [
      h('div', { class: 'search sim-search' }, h('label', { for: 'sim-fv-search' }, 'Search outcomes'), search),
      h('label', { class: 'check', for: 'sim-fv-usable' }, usable, 'Only usable'),
    ];
    return SIM.fvControls;
  }

  function providerLine(pv) {
    pv = obj(pv);
    const name = PROVIDER_NAMES[str(pv.name)] || str(pv.name) || 'Provider';
    const st = str(pv.status);
    const bits = [name + ': ' + (PROVIDER_STATUS[st] || st || 'unknown')];
    const extra = [];
    if (num(pv.matched) != null) extra.push(fmtInt(pv.matched) + ' matched, ' + fmtInt(pv.quoted) + ' quoted');
    return h('li', { class: 'provider provider-' + (PROVIDER_STATUS[st] ? st : 'unknown') },
      icon(st === 'ok' ? 'checkCircle' : st === 'partial' || st === 'backoff' ? 'pulse' : st === 'disabled' ? 'closed' : 'warn'),
      h('span', null, h('strong', null, bits[0]), extra.length ? ' · ' + extra.join(', ') : '',
        num(pv.last_ok_at) != null ? [' · last answer ', timeEl(pv.last_ok_at, 'ago')] : '',
        num(pv.next_try_at) != null ? [' · next try ', timeEl(pv.next_try_at, 'ago')] : '',
        str(pv.last_error) ? h('span', { class: 'cell-sub wrap' }, shortProblem(pv.last_error)) : null));
  }

  function renderSimFairValue() {
    const p = simPanel('fairvalue', 'Outside fair values', null);
    const f = S.data.fairvalue;
    if (!p.controls) {
      p.controls = h('div', { class: 'filter-row sim-fv-filters' }, fvControls(), h('p', { class: 'result-count', id: 'sim-fv-count' }));
      p.list = h('div', { class: 'sim-fv-body' });
      p.body.append(p.controls, p.list);
      p.info = h('div', { class: 'sim-fv-info' });
      p.body.insertBefore(p.info, p.controls);
      p.foot = h('div', { class: 'sim-fv-foot' });
      p.body.appendChild(p.foot);
    }
    const rows = f ? objs(f.rows) : [];
    const terms = SIM.fvQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const shown = rows.filter(function (r) {
      if (SIM.fvUsable && !(r.fair && r.fair.usable === true)) return false;
      if (!terms.length) return true;
      const hay = [r.title, r.option, r.race_key, r.exchange_id, r.market_id].map(str).join(' ').toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) !== -1; });
    });
    show(p.controls, rows.length > 0);
    setText($('sim-fv-count'), rows.length ? 'Showing ' + fmtInt(shown.length) + ' of ' + plural(rows.length, 'outcome') : '');
    const mapPath = f && f.map ? str(obj(f.map).path) : '';
    const ikey = sig([f && f.enabled, f && f.mode, f && f.providers, f && f.manual, f && f.map, f && f.history, f && f.counts, f && f.last_refresh_at, !f && viewError('fairvalue') ? 'e' : '']);
    if (p.info.dataset.sig !== ikey && !selectionInside(p.info)) {
      p.info.dataset.sig = ikey;
      const out = [];
      if (!f) out.push(h('p', { class: 'muted-note' }, loadingMsg('fairvalue', 'Loading the fair values…')));
      else if (f.enabled === false || str(f.mode) === 'off') out.push(h('p', { class: 'muted-note' }, 'Outside fair values are off: value ideas cannot trade.'));
      else {
        const c = obj(f.counts);
        out.push(h('p', null, plural(c.outcomes, 'outcome') + ': ' + fmtInt(c.matched) + ' matched to an outside market, ' + fmtInt(c.usable) + ' usable' +
          (num(c.manual) > 0 ? ', ' + fmtInt(c.manual) + ' from your file' : '') + (num(c.suspect) > 0 ? ', ' + fmtInt(c.suspect) + ' suspect' : '') +
          (num(c.near) > 0 ? ', ' + plural(c.near, 'near match', 'near matches') : '') + '.',
          h('span', { class: 'muted-note' }, ' Mode: ' + (str(f.mode) === 'manual' ? 'your file only' : 'automatic') +
            (num(f.last_refresh_at) != null ? '; refreshed ' : ''), num(f.last_refresh_at) != null ? timeEl(f.last_refresh_at, 'ago') : '', '.')));
        if (objs(f.providers).length) out.push(h('ul', { class: 'provider-list' }, objs(f.providers).map(providerLine)));
        const man = f.manual && typeof f.manual === 'object' ? obj(f.manual) : null;
        if (man) {
          out.push(h('p', { class: 'muted-note file-line' }, 'Your fair-value file: ', h('code', null, str(man.path) || DASH),
            man.exists === true ? ' (' + plural(man.entries, 'entry', 'entries') + ')' : ' (not created yet)'));
          if (texts(man.errors).length) out.push(h('ul', { class: 'notes file-errors' }, texts(man.errors).map(function (x) { return h('li', null, x); })));
        }
        const map = f.map && typeof f.map === 'object' ? obj(f.map) : null;
        if (map) {
          out.push(h('p', { class: 'muted-note file-line' }, 'Match file: ', h('code', null, str(map.path) || DASH),
            ' (' + plural(map.overrides, 'override') + (map.exists === true ? '' : ', not created yet') + ')',
            num(map.loaded_at) != null ? [', read ', timeEl(map.loaded_at, 'ago')] : ''));
          if (texts(map.errors).length) out.push(h('ul', { class: 'notes file-errors' }, texts(map.errors).map(function (x) { return h('li', null, x); })));
        }
        const hist = f.history && typeof f.history === 'object' ? obj(f.history) : null;
        if (hist && num(hist.records) > 0) {
          out.push(h('p', { class: 'muted-note' }, 'Imported outside history: ' + plural(hist.records, 'record') + ' from ', timeEl(hist.first, 'short'), ' to ', timeEl(hist.last, 'short'), ' (replays only, indicative).'));
        }
      }
      rebuild(p.info, out);
    }
    const lkey = sig([shown, mapPath, rows.length]);
    if (p.list.dataset.sig !== lkey && !selectionInside(p.list)) {
      p.list.dataset.sig = lkey;
      if (!rows.length) rebuild(p.list, f && f.enabled !== false && str(f.mode) !== 'off' ? [h('p', { class: 'muted-note' }, 'No outcomes to match yet.')] : []);
      else if (!shown.length) rebuild(p.list, [h('p', { class: 'muted-note' }, 'No outcomes match these filters.')]);
      else {
        rebuild(p.list, [tableWrap('Fair values table', h('table', { class: 'data-table sim-table fv-table' },
          h('caption', { class: 'sr-only' }, 'Outside fair value of every outcome next to its Cup price'),
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Outcome'), h('th', { scope: 'col' }, 'Race'), h('th', { scope: 'col', class: 'num' }, 'Fair value'),
            h('th', { scope: 'col', class: 'num' }, 'Cup bid / ask'), h('th', { scope: 'col', class: 'num' }, 'Gap'), h('th', { scope: 'col' }, 'Matched venues'),
            h('th', { scope: 'col' }, 'Usable'))),
          h('tbody', null, shown.map(function (r) { return fvRow(r, mapPath); }))), 'pin-first')]);
      }
    }
    const fkey = sig([f && f.caveats]);
    if (p.foot.dataset.sig !== fkey) {
      p.foot.dataset.sig = fkey;
      rebuild(p.foot, f && texts(f.caveats).length ? [h('ul', { class: 'notes' }, texts(f.caveats).map(function (x) { return h('li', null, x); }))] : []);
    }
    return p.el;
  }

  // ---- how to read this

  function renderSimCaveats(d) {
    const p = simPanel('caveats', 'How to read this');
    fillPanel(p, sig([d.caveats]), function () {
      const c = texts(d.caveats);
      return c.length ? [h('ul', { class: 'reasons caveat-list' }, c.map(function (x) { return h('li', null, x); }))] : [h('p', { class: 'muted-note' }, 'Nothing was traded: every order here is simulated.')];
    });
    return p.el;
  }

  // ---- reset

  function openResetDialog() {
    const dlg = $('sim-reset-dialog');
    if (!dlg) return;
    const run = obj(S.data.paper && S.data.paper.run);
    const hours = num(run.target_hours) || 24;
    setText($('sim-reset-text'), 'Every portfolio goes back to its start capital and the ' + fmtCount(hours) + '-hour clock restarts. ' +
      'The current run’s result is kept under "Previous run". Nothing real is affected.');
    setAttr($('sim-reset-confirm'), 'aria-disabled', null);
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
    $('sim-reset-cancel').focus();
  }

  function closeResetDialog() {
    const dlg = $('sim-reset-dialog');
    if (dlg && dlg.open) {
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
    }
    const btn = $('sim-reset');
    if (btn && btn.isConnected) btn.focus({ preventScroll: true });
  }

  async function confirmReset() {
    if (SIM.resetBusy) return;
    SIM.resetBusy = true;
    setAttr($('sim-reset-confirm'), 'aria-disabled', 'true');
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    let text;
    let ok = false;
    try {
      const resp = await fetch('/api/paper/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ confirm: true }),
        cache: 'no-store',
        credentials: 'same-origin',
        signal: ctrl.signal,
      });
      let body = null;
      try {
        body = await resp.json();
      } catch (e) {
        body = null;
      }
      ok = resp.ok && !!(body && body.reset === true);
      text = ok ? str(body && body.message) || 'Started a new simulation.' : str(body && body.error) || 'Could not reset the simulation (HTTP ' + resp.status + ').';
    } catch (e) {
      text = e && e.name === 'AbortError' ? 'The bot did not answer; the simulation was not reset.' : 'Could not reach the bot to reset the simulation.';
    } finally {
      clearTimeout(timer);
      SIM.resetBusy = false;
    }
    closeResetDialog();
    const status = $('sim-status');
    setText(status, '');
    setText(status, text);
    status.className = 'sim-status' + (ok ? ' is-ok' : ' is-error');
    if (ok) {
      SIM.previous = null;
      SIM.previousState = '';
      SIM.chartKey = null;
      SIM.hoverTs = null;
      if (SIM.previousOpen) loadPrevious();
      loadView('paper', true);
      loadView('backtest', true);
    }
  }

  // ---------------------------------------------------------------- drawer

  const drawerEl = function () { return $('drawer'); };

  /** The title and outcome of an exchange from data the page already has (the row, band, surge or
   *  idea the user activated), so the dialog can be named before its detail loads. */
  function knownOutcome(id) {
    const sid = String(id);
    const pick = function (title, option) { return str(title) ? { title: str(title), option: str(option) } : null; };
    for (const r of objs(S.data.markets && S.data.markets.rows)) if (String(r.exchange_id) === sid) return pick(r.title, r.option);
    for (const b of objs(S.data.high && S.data.high.bands)) if (String(b.exchange_id) === sid) return pick(b.title, b.option);
    for (const s of objs(S.data.surges && S.data.surges.surges)) if (String(s.exchange_id) === sid) return pick(s.title, s.option);
    for (const o of objs(S.data.strategy && S.data.strategy.opportunities)) {
      if (!isSet(o) && String(o.exchange_id) === sid) return pick(o.title, o.option);
      for (const l of objs(o.legs)) if (String(l.exchange_id) === sid) return pick(l.title || o.title, l.option);
    }
    for (const x of objs(S.data.paper && S.data.paper.positions).concat(objs(S.data.paper && S.data.paper.fills))) {
      if (String(x.exchange_id) === sid) return pick(x.title, x.option);
    }
    for (const r of objs(S.data.fairvalue && S.data.fairvalue.rows)) if (String(r.exchange_id) === sid) return pick(r.title, r.option);
    return null;
  }

  function openDrawer(id, pushed) {
    const D = S.drawer;
    const dlg = drawerEl();
    if (D.id !== id) {
      abortDrawerRequest(); // the previous outcome's answer must not hold this one up
      D.id = id;
      D.gen += 1;
      D.data = null;
      D.error = null;
      D.hoverTs = null;
      D.sections = null;
      D.sigs = {};
      D.staleKind = '';
      D.known = knownOutcome(id);
      D.seeded = !!D.known;
      clearTimeout(D.timer);
      safe('drawer', renderDrawer);
    }
    D.pushed = !!pushed;
    D.closing = false;
    if (!dlg.open) {
      const active = document.activeElement;
      D.returnFocus = active && active !== document.body ? active : null;
      D.returnKey = S.pendingReturnKey || (active && active.getAttribute ? active.getAttribute('data-focus-key') : null);
      S.pendingReturnKey = null;
      // lock the page behind the modal: the wheel over the backdrop must not scroll the list away
      lockPageScroll(true);
      dlg.showModal();
      $('drawer-inner').scrollTop = 0;
      $('drawer-title').focus({ preventScroll: true });
      watchDrawerHead();
    }
    pollDrawer();
  }

  /** Lock the page behind the modal drawer (the wheel over the backdrop must not scroll the list
   *  away); the body is padded by the scrollbar's width so nothing behind the backdrop shifts. */
  function lockPageScroll(on) {
    const root = document.documentElement;
    const D = S.drawer;
    if (on) {
      if (root.classList.contains('drawer-open')) return;
      D.scrollY = window.scrollY;
      const bar = window.innerWidth - root.clientWidth;
      root.classList.add('drawer-open');
      if (bar > 0) document.body.style.paddingRight = bar + 'px';
    } else {
      if (!root.classList.contains('drawer-open')) return;
      root.classList.remove('drawer-open');
      document.body.style.paddingRight = '';
      if (Math.abs(window.scrollY - D.scrollY) > 1) window.scrollTo(0, D.scrollY);
    }
  }

  /** Keep --drawer-head-h equal to the sticky header's height, so focused controls scroll in below it. */
  function watchDrawerHead() {
    const D = S.drawer;
    const head = document.querySelector('#drawer .drawer-head');
    const inner = $('drawer-inner');
    const update = function () {
      const sticky = head && getComputedStyle(head).position === 'sticky';
      inner.style.setProperty('--drawer-head-h', (sticky ? Math.ceil(head.getBoundingClientRect().height) : 0) + 'px');
      safe('drawer-question', renderDrawerQuestion); // the clamp may cut the title at a new width
    };
    update();
    if (D.headRo || typeof ResizeObserver !== 'function' || !head) return;
    D.headRo = new ResizeObserver(update);
    D.headRo.observe(head);
    window.addEventListener('resize', update);
  }

  /** Scroll a newly focused control in the drawer out from under the sticky header (WCAG 2.4.11). */
  function keepFocusVisible(ev) {
    const inner = $('drawer-inner');
    const head = document.querySelector('#drawer .drawer-head');
    const el = ev.target;
    if (!inner || !head || !el || head.contains(el) || el === inner) return;
    if (getComputedStyle(head).position !== 'sticky') return;
    const r = el.getBoundingClientRect();
    const top = head.getBoundingClientRect().bottom + 8;
    const box = inner.getBoundingClientRect();
    if (r.top < top) inner.scrollTop -= top - r.top;
    else if (r.bottom > box.bottom - 8 && r.height < box.height - (top - box.top)) inner.scrollTop += r.bottom - (box.bottom - 8);
  }

  function closeDrawerUI() {
    const D = S.drawer;
    D.closing = false;
    if (!D.id) return;
    abortDrawerRequest();
    D.id = null;
    D.gen += 1;
    clearTimeout(D.timer);
    D.timer = null;
    D.sections = null;
    D.known = null;
    if (D.ro) D.ro.disconnect();
    D.ro = null;
    D.chartWidth = 0;
    const dlg = drawerEl();
    if (dlg.open) dlg.close();
    lockPageScroll(false);
    const key = D.returnKey;
    let target = null;
    if (key) target = document.querySelector('#view-' + S.view + ' [data-focus-key="' + CSS.escape(key) + '"]');
    if (!target && D.returnFocus && D.returnFocus.isConnected) target = D.returnFocus;
    if (!target) target = $('main');
    target.focus({ preventScroll: true });
    D.returnFocus = null;
    D.returnKey = null;
  }

  function requestCloseDrawer() {
    const D = S.drawer;
    if (!D.id || D.closing) return; // a second Esc must not step back through history twice
    if (D.pushed) {
      D.closing = true;
      const id = D.id;
      history.back(); // the hashchange handler closes the drawer
      setTimeout(function () {
        // the back navigation did not land on a view (for example the history was rewritten): close directly
        if (D.closing && D.id === id) {
          D.closing = false;
          history.replaceState(null, '', '#' + S.view);
          route(false);
        }
      }, 400);
    } else {
      history.replaceState(null, '', '#' + S.view);
      route(false);
    }
  }

  function drawerRow() {
    const d = S.drawer.data;
    return obj(d && d.exchange);
  }

  /** The drawer's outcome is in a market that closed or settled (the server says market_open false). */
  function drawerClosed(d) {
    return !!(d && d.market_open === false);
  }

  function renderDrawer() {
    const D = S.drawer;
    if (!D.id) return;
    const d = D.data;
    const err = D.error;
    const body = $('drawer-body');
    const row = drawerRow();
    let title = '';
    if (d) {
      title = str(row.title) || 'Untitled market';
      const bits = ['Outcome: ' + (str(row.option) || DASH)];
      if (row.exchange_id != null) bits.push('exchange ' + str(row.exchange_id));
      if (row.market_id != null) bits.push('market ' + str(row.market_id));
      setText($('drawer-sub'), bits.join(' · '));
    } else if (err) {
      title = err.status === 404 ? 'Outcome not found' : D.known ? D.known.title : 'Could not load this outcome';
      setText($('drawer-sub'), (D.known ? 'Outcome: ' + (D.known.option || DASH) + ' · exchange ' : 'Exchange ') + D.id);
    } else if (D.known) {
      // named from the row the user activated, so the dialog is not announced as "Loading…"
      title = D.known.title;
      setText($('drawer-sub'), 'Outcome: ' + (D.known.option || DASH) + ' · exchange ' + D.id);
    } else {
      title = 'Loading…';
      setText($('drawer-sub'), 'Exchange ' + D.id);
    }
    setText($('drawer-title'), title);
    setAttr($('drawer-title'), 'title', d || D.known ? title : null); // the visible title is clamped to two lines
    setText($('drawer-kicker'), drawerClosed(d) ? 'Market detail · closed' : 'Market detail');

    if (!d) {
      const msg = err
        ? (err.status === 404
          ? 'This outcome is not being tracked. It may have closed, or the link is out of date.'
          : 'Could not load the details: ' + str(err.message) + ' Retrying shortly.')
        : 'Loading prices, the order book and recent trades…';
      if (D.sigs.placeholder !== msg) {
        D.sigs.placeholder = msg;
        D.sections = null;
        rebuild(body, [emptyNote(msg)]);
      }
      return;
    }
    D.sigs.placeholder = null;
    if (!D.sections) {
      D.sections = {
        question: h('p', { class: 'drawer-question', 'aria-hidden': 'true', hidden: true }),
        closed: h('div', { class: 'drawer-closed', hidden: true }),
        stale: h('div', { class: 'drawer-stale', hidden: true }),
        facts: h('section', { class: 'panel', 'aria-label': 'Prices' }),
        chart: buildChartSection(),
        book: h('section', { class: 'panel', 'aria-labelledby': 'h-book' }),
        trades: h('section', { class: 'panel', 'aria-labelledby': 'h-trades' }),
        surge: h('section', { class: 'panel', 'aria-labelledby': 'h-dsurge' }),
      };
      D.sigs = {};
      rebuild(body, [D.sections.question, D.sections.closed, D.sections.stale, D.sections.facts, D.sections.chart.root,
        D.sections.surge, D.sections.book, D.sections.trades]);
    }
    renderDrawerQuestion();
    renderDrawerClosed(d);
    renderDrawerStale(err);
    renderDrawerFacts(d);
    renderChartSection(d);
    renderDrawerSurge(d);
    renderBook(d);
    renderTrades(d);
    refreshTimes(body);
  }

  /** The title is clamped to two lines in the sticky header; when that cuts it short, the full
   *  question is repeated at the top of the drawer (a tooltip is no help on touch screens). It is
   *  hidden from screen readers, which read the whole heading anyway. */
  function renderDrawerQuestion() {
    const sec = S.drawer.sections;
    if (!sec || !sec.question) return;
    const t = $('drawer-title');
    const cut = !!S.drawer.data && t.scrollHeight > t.clientHeight + 1;
    show(sec.question, cut);
    if (cut) setText(sec.question, t.textContent);
  }

  function renderDrawerClosed(d) {
    const box = S.drawer.sections.closed;
    const closed = drawerClosed(d);
    if (closed === !box.hidden && box.childNodes.length) return;
    show(box, closed);
    box.replaceChildren();
    if (closed) {
      box.append(icon('closed'), h('p', null, h('strong', null, 'This market has closed or settled. '),
        'The prices below are the last ones recorded, and it can no longer be traded.'));
    }
  }

  /** The drawer keeps its last good data on screen: say when that data is not live, either because
   *  its own refresh failed or because the tracker has stopped or gone stale (the header and its
   *  banners are hidden behind the modal, and inert to screen readers). */
  function renderDrawerStale(err) {
    const D = S.drawer;
    const box = D.sections && D.sections.stale;
    if (!box) return;
    const live = liveState();
    const tr = obj(S.data.status && S.data.status.tracker);
    let kind = '';
    let parts = null;
    if (err) {
      // only this drawer's own failed request claims the bot is unreachable (a refresh that just
      // worked never does, even while the header still says Offline)
      const unreachable = !err.status && isOffline();
      kind = 'fetch';
      parts = [h('strong', null, unreachable ? 'Can’t reach the bot: ' : 'Could not refresh these details: '),
        unreachable ? S.failureText || sentence(err.message) : sentence(err.message, 'The request failed.'),
        D.okAt != null ? [' Showing data from ', timeEl(D.okAt, 'ago'), '.'] : null, ' Retrying automatically.'];
    } else if (live.state === 'stopped') {
      kind = 'stopped';
      parts = [h('strong', null, 'The tracker has stopped: '), 'prices here are no longer updating. ', sentence(str(tr.fatal_error))];
    } else if (live.state === 'stale') {
      kind = 'stale';
      const last = num(tr.last_snapshot_at);
      parts = live.kind === 'view'
        ? [h('strong', null, 'Live data is temporarily unavailable: '), 'these details show the last data the tracker had.']
        : [h('strong', null, 'Prices are stale: '), last != null && dateOf(last)
          ? ['no new price snapshot since ', timeEl(last, 'clock'), ' (', timeEl(last, 'ago'), ').']
          : 'no new price snapshot recently.'];
    }
    const key = sig([kind, kind === 'fetch' ? [err.status, err.message, isOffline(), S.failureText, D.okAt] : null,
      kind === 'stopped' ? tr.fatal_error : null, kind === 'stale' ? [live.kind, tr.last_snapshot_at] : null]);
    if (D.sigs.stale === key) return;
    const was = D.staleKind;
    D.sigs.stale = key;
    D.staleKind = kind;
    show(box, !!kind);
    if (!kind) {
      box.replaceChildren();
      if (was) announce('The details are updating again.');
      return;
    }
    box.replaceChildren(icon('warn'), h('p', null, parts));
    if (was !== kind) {
      announce(kind === 'fetch' ? 'These details are not updating: showing older data.'
        : kind === 'stopped' ? LIVE_SENTENCES.stopped
          : live.sentence || LIVE_SENTENCES.stale);
    }
  }

  function renderDrawerFacts(d) {
    const row = obj(d.exchange);
    const band = d.high_band ? obj(d.high_band) : null;
    const closed = drawerClosed(d);
    // a closed market has no live quote: show the last recorded mark and trade instead of "No price yet"
    const pts = closed ? seriesPoints(d) : [];
    const lastPt = pts.length ? pts[pts.length - 1] : null;
    const lastTrade = objs(d.trades)[0] || null;
    const lastPx = num(row.last) != null ? row.last : closed && lastTrade ? num(lastTrade.price) : null;
    const key = sig([row.last, row.mark, row.bid, row.ask, row.spread, row.change_5m, row.change_1h, row.change_24h, row.settlement_date, band, cupEnd(),
      closed, lastPt && [lastPt.ts, lastPt.price], lastPx]);
    if (S.drawer.sigs.facts !== key && selectionInside(S.drawer.sections.facts)) return;
    if (S.drawer.sigs.facts === key) return;
    S.drawer.sigs.facts = key;
    const settle = toTs(row.settlement_date);
    const end = cupEnd();
    const closedPrice = function (v) { return num(v) != null ? priceEl(v) : priceEl(null, 'Market closed: no live quote'); };
    const items = closed
      ? [
        ['Last trade', num(lastPx) != null ? priceEl(lastPx) : priceEl(null, 'No trades recorded'), 'The last trade recorded before the market closed'],
        ['Last known mark', num(row.mark) != null ? priceEl(row.mark) : lastPt ? priceEl(lastPt.price) : priceEl(null, 'No price recorded'),
          lastPt ? 'The last price recorded, ' + fmtAbs(lastPt.ts) : 'The last price recorded'],
        ['Bid', closedPrice(row.bid)],
        ['Ask', closedPrice(row.ask)],
      ]
      : [
        ['Last trade', lastTradeEl(row.last), 'The last tournament trade'],
        ['Mark', priceEl(row.mark), 'The price the chart plots: the bid/ask mid when the spread is tight, else the last trade'],
        ['Bid', priceEl(row.bid)],
        ['Ask', priceEl(row.ask)],
      ];
    items.push(
      ['Spread', num(row.spread) == null ? DASH : fmtPrice(row.spread)],
      ['Change 5m', deltaEl(row.change_5m)],
      ['Change 1h', deltaEl(row.change_1h)],
      ['Change 24h', deltaEl(row.change_24h)],
      ['Settles', settle == null ? DASH : h('span', null, timeEl(settle, 'day'), end != null && settle > end ? ' (after Cup end)' : ''), null,
        end != null && settle != null && settle > end ? 'wide' : null]);
    if (band && num(band.favorite_price) != null) {
      const share = num(band.time_in_band) != null ? ', ' + fmtPct0(band.time_in_band) + ' of the last ' + fmtSpan(num(band.lookback_s) || 21600) : '';
      items.push(['High 90s', (str(band.side).toUpperCase() || 'Favourite') + ' at ' + fmtPrice(band.favorite_price) + share,
        'The favourite side has sat at 0.95 or more', 'wide']);
    }
    rebuild(S.drawer.sections.facts, [factList(items, 'drawer-facts')]);
  }

  // ---------------------------------------------------------------- drawer: price chart

  function buildChartSection() {
    const rangeGroup = h('div', { class: 'seg-group', role: 'group', 'aria-label': 'Chart range' });
    for (const r of Object.keys(RANGES)) {
      rangeGroup.appendChild(h('button', {
        type: 'button', class: 'seg', 'data-range': r, 'aria-pressed': r === S.drawer.range ? 'true' : 'false',
        title: 'Show the last ' + RANGE_LABELS[r],
        onclick: function () { setRange(r); },
      }, r));
    }
    // A toggle keeps one label; aria-pressed (and the pressed style) says whether the table is showing.
    const toggle = h('button', {
      type: 'button', class: 'btn btn-toggle', 'data-action': 'table-toggle', 'aria-pressed': S.drawer.table ? 'true' : 'false',
      title: 'Show the prices as a table instead of a chart',
      onclick: function () { setTableView(!S.drawer.table); },
    }, icon('table'), 'Table view');
    const host = h('div', {
      class: 'chart', tabindex: '0', role: 'group',
      'aria-label': 'Price chart',
      'aria-describedby': 'chart-help',
    });
    const help = h('p', { class: 'sr-only', id: 'chart-help' },
      'Left and right arrow keys step through the prices, Home and End jump to the first and last. Table view lists every price.');
    const readout = h('p', { class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
    const tableBox = h('div', { class: 'chart-table-wrap', tabindex: '0', role: 'region', 'aria-label': 'Price history table' });
    const legend = h('div', { class: 'legend' });
    const summary = h('p', { class: 'view-sub chart-summary' });
    const root = h('section', { class: 'panel', 'aria-labelledby': 'h-chart' },
      h('div', { class: 'section-head' }, h('h3', { id: 'h-chart' }, 'Price history'), h('div', { class: 'controls' }, rangeGroup, toggle)),
      summary, host, help, readout, tableBox, legend);
    host.addEventListener('keydown', chartKey);
    host.addEventListener('focus', function () { chartFocus(true); });
    host.addEventListener('blur', function () { chartFocus(false); });
    if (S.drawer.ro) S.drawer.ro.disconnect();
    S.drawer.ro = null;
    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(function () {
        const w = Math.floor(host.clientWidth);
        if (w && w !== S.drawer.chartWidth && S.drawer.data) {
          S.drawer.chartWidth = w;
          requestAnimationFrame(function () { safe('chart', drawChart); });
        }
      });
      ro.observe(host);
      S.drawer.ro = ro;
    }
    return { root: root, host: host, tableBox: tableBox, legend: legend, toggle: toggle, rangeGroup: rangeGroup, summary: summary, readout: readout, chart: null };
  }

  function setRange(r) {
    if (!RANGES[r]) return;
    S.drawer.range = r;
    S.drawer.hoverTs = null;
    if (S.drawer.data) safe('chart', function () { renderChartSection(S.drawer.data, true); });
  }

  function setTableView(on) {
    S.drawer.table = !!on;
    if (S.drawer.data) safe('chart', function () { renderChartSection(S.drawer.data, true); });
  }

  function seriesPoints(d) {
    return arr(d.series).map(function (p) {
      return Array.isArray(p) ? { ts: num(p[0]), price: num(p[1]), source: str(p[2]) } : null;
    }).filter(function (p) { return p && p.ts != null && p.price != null; }).sort(function (a, b) { return a.ts - b.ts; });
  }

  function renderChartSection(d, force) {
    const sec = S.drawer.sections && S.drawer.sections.chart;
    if (!sec) return;
    for (const b of sec.rangeGroup.querySelectorAll('button[data-range]')) setAttr(b, 'aria-pressed', b.dataset.range === S.drawer.range ? 'true' : 'false');
    setAttr(sec.toggle, 'aria-pressed', S.drawer.table ? 'true' : 'false');
    show(sec.host, !S.drawer.table);
    show(sec.tableBox, S.drawer.table);
    const series = arr(d.series);
    const key = sig([series.length, series[0], series[series.length - 1], objs(d.surges).map(function (s) { return [s.id, s.start_ts, s.end_ts]; }),
      S.drawer.range, S.drawer.table]);
    if (!force && S.drawer.sigs.chart === key) return;
    S.drawer.sigs.chart = key;
    if (S.drawer.table) renderChartTable(d);
    else drawChart();
  }

  function rangeData(d) {
    const all = seriesPoints(d);
    const now = num(d.now) != null ? d.now : nowS();
    const span = RANGES[S.drawer.range] || RANGES['24h'];
    const t0 = now - span;
    const inRange = all.filter(function (p) { return p.ts >= t0; });
    let before = null;
    for (const p of all) {
      if (p.ts < t0) before = p;
      else break;
    }
    const t1 = Math.max(now, inRange.length ? inRange[inRange.length - 1].ts : now);
    const tolerance = Math.max(3600, span * 0.1);
    const startPoint = before && t0 - before.ts <= tolerance ? before : inRange[0] || null;
    const surges = objs(d.surges).filter(function (s) {
      return num(s.start_ts) != null && num(s.end_ts) != null && s.end_ts >= t0 && s.start_ts <= t1;
    });
    return { all: all, inRange: inRange, before: before, startPoint: startPoint, t0: t0, t1: t1, surges: surges };
  }

  function niceStep(span, maxTicks) {
    const steps = [0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25, 0.5];
    for (const s of steps) if (span / s <= maxTicks) return s;
    return 0.5;
  }

  function decimalsOf(step) {
    const parts = String(step).split('.');
    return Math.max(2, parts[1] ? parts[1].length : 0);
  }

  const TICK_STEPS = [900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400, 172800];
  const TICK_EDGE = 18; // px: a centred label this close to the plot's edge would spill past it
  const TICK_GAP = 56; // px: the least room between two labels when the plot is narrow

  function tickLabel(t, step) {
    const dt = new Date(t * 1000);
    const midnight = dt.getHours() === 0 && dt.getMinutes() === 0;
    return step >= 86400 || midnight ? DT_DAY.format(dt) : DT_TIME.format(dt);
  }

  /** Time labels for the x axis, already clear of the plot's edges. A label is about 96 px apart
   *  when there is room; on a narrow plot, where that leaves one label (or none), a smaller step is
   *  tried, thinned to every k-th tick (at least TICK_GAP apart) and phased to keep the most, so
   *  the axis always has a scale (2+ labels) where any fit. */
  function timeTicks(t0, t1, X, left, right, stepsIn) {
    const STEPS = stepsIn || TICK_STEPS;
    const span = t1 - t0;
    if (!(span > 0)) return [];
    const width = right - left;
    const tz = -new Date(t0 * 1000).getTimezoneOffset() * 60;
    const inside = function (t) { const x = X(t); return x >= left + TICK_EDGE && x <= right - TICK_EDGE; };
    const grid = function (step) {
      const out = [];
      for (let t = Math.ceil((t0 + tz) / step) * step - tz; t <= t1 && out.length < 400; t += step) out.push(t);
      return out;
    };
    const maxTicks = Math.max(2, Math.floor(width / 96));
    let step = STEPS[STEPS.length - 1];
    for (const s of STEPS) {
      if (span / s <= maxTicks) {
        step = s;
        break;
      }
    }
    let best = grid(step).filter(inside).map(function (t) { return { t: t, label: tickLabel(t, step) }; });
    if (best.length >= 2) return best.slice(0, 40);
    for (let i = STEPS.indexOf(step) - 1; i >= 0; i--) {
      const s = STEPS[i];
      const pxPerStep = (s / span) * width;
      const k = Math.max(1, Math.ceil(TICK_GAP / pxPerStep));
      if (k > 48) break;
      const all = grid(s);
      for (let ph = 0; ph < k; ph++) {
        const pick = all.filter(function (t, j) { return j % k === ph; }).filter(inside);
        if (pick.length > best.length) best = pick.map(function (t) { return { t: t, label: tickLabel(t, s * k) }; });
      }
      if (best.length >= 2) break;
    }
    return best.slice(0, 40);
  }

  function surgeAt(ts, surges) {
    for (const s of surges) if (ts >= s.start_ts && ts <= s.end_ts) return s;
    return null;
  }

  /** The chart's empty state: suggest a longer range only when one exists and has enough points
   *  (as a button), else say how much history there is. */
  function chartEmpty(d, R) {
    const order = Object.keys(RANGES);
    const now = num(d.now) != null ? d.now : nowS();
    const longer = order.slice(order.indexOf(S.drawer.range) + 1).filter(function (r) {
      const t0 = now - RANGES[r];
      return R.all.filter(function (p) { return p.ts >= t0; }).length >= 2;
    })[0];
    const box = h('div', { class: 'chart-empty' });
    if (longer) {
      box.append(h('p', null, 'Not enough price history in the last ' + RANGE_LABELS[S.drawer.range] + ' to draw a line.'),
        h('button', { type: 'button', class: 'btn', onclick: function () { setRange(longer); } }, 'Show the last ' + RANGE_LABELS[longer]));
    } else if (!R.all.length) {
      box.append(h('p', null, 'No price history for this outcome yet. The chart appears after the next snapshots.'));
    } else if (R.all.length === 1) {
      const p = R.all[0];
      box.append(h('p', null, 'Only 1 price point so far (' + fmtPrice(p.price) + ' at ' + fmtClock(p.ts) + '). The chart needs at least 2; the next snapshot adds one.'));
    } else {
      box.append(h('p', null, 'Not enough price history in the last ' + RANGE_LABELS[S.drawer.range] + ' to draw a line. Table view lists the points there are.'));
    }
    return box;
  }

  function drawChart() {
    const sec = S.drawer.sections && S.drawer.sections.chart;
    const d = S.drawer.data;
    if (!sec || !d || S.drawer.table) return;
    const host = sec.host;
    const prev = sec.chart;
    // keep the crosshair where it was across a data refresh or resize (mouse resting on it, or keyboard focus)
    const restore = S.drawer.hoverTs != null && (!!(prev && prev.pointer) || document.activeElement === host);
    const R = rangeData(d);
    const pts = R.inRange;
    const legendItems = [h('span', { class: 'legend-item' }, h('span', { class: 'key-line', 'aria-hidden': 'true' }), 'Price (mark)')];

    if (pts.length < 2) {
      sec.chart = null;
      rebuild(host, [chartEmpty(d, R)]);
      rebuild(sec.legend, []);
      setText(sec.summary, '');
      return;
    }

    // The SVG is drawn 1:1 with its box (no minimum width), so viewBox units are CSS pixels.
    const W = Math.max(160, Math.floor(host.clientWidth || 640));
    const H = 290;
    const M = { l: 48, r: 58, t: 14, b: 30 };
    const plotW = W - M.l - M.r;
    const plotH = H - M.t - M.b;
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p.price); hi = Math.max(hi, p.price); }
    const first = pts[0].price;
    const lastP = pts[pts.length - 1];
    let minV = lo, maxV = hi;
    // the line starts at the point just before the range: keep it inside the y axis too
    if (R.startPoint) { lo = Math.min(lo, R.startPoint.price); hi = Math.max(hi, R.startPoint.price); }
    const pad = Math.max(0.01, (hi - lo) * 0.12);
    lo -= pad; hi += pad;
    if (hi - lo < 0.1) { const m = (hi + lo) / 2; lo = m - 0.05; hi = m + 0.05; }
    if (lo < 0) { hi -= lo; lo = 0; }
    if (hi > 1) { lo -= hi - 1; hi = 1; }
    lo = Math.max(0, lo);
    const step = niceStep(hi - lo, 5);
    lo = Math.max(0, Math.floor(lo / step + 1e-9) * step);
    hi = Math.min(1, Math.ceil(hi / step - 1e-9) * step);
    if (hi <= lo) hi = Math.min(1, lo + step);
    const dec = decimalsOf(step);
    const X = function (t) { return M.l + ((t - R.t0) / (R.t1 - R.t0)) * plotW; };
    const Y = function (v) { return M.t + ((hi - v) / (hi - lo)) * plotH; };
    const clipId = 'chart-clip-' + S.drawer.gen;

    const svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, 'aria-hidden': 'true', focusable: 'false' });
    svg.appendChild(svgEl('defs', null, svgEl('clipPath', { id: clipId }, svgEl('rect', { x: M.l, y: M.t - 4, width: plotW, height: plotH + 8 }))));

    // reference bands: favourite at 0.95+ (YES high, or YES at 0.05 or less)
    const bands = svgEl('g', { class: 'band' });
    let bandShown = false;
    if (hi > HIGH_LINE) {
      const top = Y(hi), bottom = Y(Math.max(lo, HIGH_LINE));
      bands.appendChild(svgEl('rect', { x: M.l, y: top, width: plotW, height: Math.max(0, bottom - top) }));
      if (bottom - top >= 14) bands.appendChild(svgEl('text', { class: 'band-label', x: M.l + 6, y: top + 12 }, '≥ 0.95 band'));
      bandShown = true;
    }
    if (lo < LOW_LINE) {
      const top = Y(Math.min(hi, LOW_LINE)), bottom = Y(lo);
      bands.appendChild(svgEl('rect', { x: M.l, y: top, width: plotW, height: Math.max(0, bottom - top) }));
      if (bottom - top >= 14) bands.appendChild(svgEl('text', { class: 'band-label', x: M.l + 6, y: bottom - 4 }, '≤ 0.05 band'));
      bandShown = true;
    }
    svg.appendChild(bands);

    // recessive grid + y axis labels (single axis)
    const grid = svgEl('g', { class: 'grid' });
    const labels = svgEl('g');
    for (let v = lo, i = 0; v <= hi + 1e-9 && i < 30; v += step, i++) {
      const vv = Number(v.toFixed(6));
      const y = Math.round(Y(vv)) + 0.5;
      if (i > 0) grid.appendChild(svgEl('line', { x1: M.l, x2: M.l + plotW, y1: y, y2: y }));
      labels.appendChild(svgEl('text', { class: 'tick', x: M.l - 8, y: y + 4, 'text-anchor': 'end' }, vv.toFixed(dec)));
    }
    svg.appendChild(grid);
    const baseY = Math.round(Y(lo)) + 0.5;
    svg.appendChild(svgEl('line', { class: 'baseline', x1: M.l, x2: M.l + plotW, y1: baseY, y2: baseY }));
    svg.appendChild(labels);

    // x axis labels
    const xl = svgEl('g');
    for (const tk of timeTicks(R.t0, R.t1, X, M.l, M.l + plotW)) {
      xl.appendChild(svgEl('text', { class: 'tick', x: X(tk.t), y: H - 8, 'text-anchor': 'middle' }, tk.label));
    }
    svg.appendChild(xl);

    // price line (the point before the range keeps the line continuous; the clip hides its tail)
    const linePts = (R.before ? [R.before] : []).concat(pts);
    let dPath = '';
    linePts.forEach(function (p, i) { dPath += (i ? 'L' : 'M') + X(p.ts).toFixed(1) + ' ' + Y(p.price).toFixed(1); });
    svg.appendChild(svgEl('path', { class: 'price-line', d: dPath, 'clip-path': 'url(#' + clipId + ')' }));

    // surge segments
    let surgeShown = false;
    for (const s of R.surges) {
      const seg = R.all.filter(function (p) { return p.ts >= s.start_ts && p.ts <= s.end_ts; });
      let segPts = seg.map(function (p) { return [p.ts, p.price]; });
      if (segPts.length < 2 && num(s.start_price) != null && num(s.end_price) != null) segPts = [[s.start_ts, s.start_price], [s.end_ts, s.end_price]];
      if (segPts.length < 2) continue;
      let sd = '';
      segPts.forEach(function (p, i) { sd += (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1); });
      svg.appendChild(svgEl('path', { class: 'surge-line', d: sd, 'clip-path': 'url(#' + clipId + ')' }));
      const a = segPts[0], b = segPts[segPts.length - 1];
      if (a[0] >= R.t0) svg.appendChild(svgEl('circle', { class: 'surge-dot', cx: X(a[0]).toFixed(1), cy: Y(a[1]).toFixed(1), r: 4 }));
      svg.appendChild(svgEl('circle', { class: 'surge-dot', cx: X(b[0]).toFixed(1), cy: Y(b[1]).toFixed(1), r: 4 }));
      surgeShown = true;
    }

    // end dot + direct label for the latest value
    const ex = X(lastP.ts), ey = Y(lastP.price);
    svg.appendChild(svgEl('circle', { class: 'end-dot', cx: ex.toFixed(1), cy: ey.toFixed(1), r: 4 }));
    svg.appendChild(svgEl('text', { class: 'end-label', x: (ex + 8).toFixed(1), y: (ey + 4).toFixed(1) }, fmtPrice(lastP.price)));

    // hover layer
    const cross = svgEl('g', { class: 'cross', visibility: 'hidden' });
    const cline = svgEl('line', { class: 'crosshair', x1: 0, x2: 0, y1: M.t, y2: M.t + plotH });
    const cdot = svgEl('circle', { class: 'hover-dot', cx: 0, cy: 0, r: 4.5 });
    cross.appendChild(cline);
    cross.appendChild(cdot);
    svg.appendChild(cross);
    const hit = svgEl('rect', { class: 'hit', x: M.l, y: M.t, width: plotW, height: plotH });
    svg.appendChild(hit);

    const tip = h('div', { class: 'tooltip', hidden: true });
    rebuild(host, [svg, tip]);

    const chart = { pts: pts, X: X, Y: Y, M: M, W: W, H: H, plotW: plotW, cross: cross, cline: cline, cdot: cdot, tip: tip, surges: R.surges, index: -1, pointer: false };
    sec.chart = chart;

    function nearest(t) {
      let lo2 = 0, hi2 = pts.length - 1;
      while (hi2 - lo2 > 1) {
        const mid = (lo2 + hi2) >> 1;
        if (pts[mid].ts < t) lo2 = mid;
        else hi2 = mid;
      }
      return Math.abs(pts[lo2].ts - t) <= Math.abs(pts[hi2].ts - t) ? lo2 : hi2;
    }
    chart.nearest = nearest;
    function pointAt(ev) {
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? W / rect.width : 1;
      const x = (ev.clientX - rect.left) * scale;
      const t = R.t0 + ((x - M.l) / plotW) * (R.t1 - R.t0);
      chart.pointer = true;
      showPoint(chart, nearest(t));
    }
    hit.addEventListener('pointermove', pointAt);
    // A tap fires no pointermove: read the tapped point (pointerdown comes before the focus it causes,
    // so chartFocus keeps this point instead of jumping to the latest one).
    hit.addEventListener('pointerdown', function (ev) {
      chart.tapped = Date.now();
      pointAt(ev);
    });
    hit.addEventListener('pointerleave', function (ev) {
      chart.pointer = false;
      // a finger "leaves" right after every tap (before the tap focuses the chart): keep the tapped point
      if (ev.pointerType && ev.pointerType !== 'mouse') return;
      if (document.activeElement !== host) hidePoint(chart);
    });

    if (restore) {
      chart.pointer = !!(prev && prev.pointer);
      showPoint(chart, nearest(S.drawer.hoverTs));
    }

    // legend (two marks plus the reference band) and a plain-text summary
    if (surgeShown) legendItems.push(h('span', { class: 'legend-item' }, h('span', { class: 'key-line surge', 'aria-hidden': 'true' }), 'Surge move'));
    if (bandShown) legendItems.push(h('span', { class: 'legend-item' }, h('span', { class: 'key-band', 'aria-hidden': 'true' }), 'Favourite at 0.95 or more (YES ≥ 0.95 or ≤ 0.05)'));
    rebuild(sec.legend, legendItems);
    // The summary's change is the same number as the "Change 24h" fact: the mark now minus the value
    // 24 hours ago (not the first point drawn), and it always equals end − start as printed.
    const row = obj(d.exchange);
    const ch24 = S.drawer.range === '24h' ? num(row.change_24h) : null;
    const start = ch24 != null ? lastP.price - ch24 : R.startPoint ? R.startPoint.price : first;
    const change = ch24 != null ? ch24 : lastP.price - start;
    // low and high describe the same span as the start: low <= start <= high, always
    minV = Math.min(minV, start);
    maxV = Math.max(maxV, start);
    setText(sec.summary, 'Last ' + RANGE_LABELS[S.drawer.range] + ': ' + fmtPrice(start) + ' ' + ARROW + ' ' + fmtPrice(lastP.price) +
      ' (' + fmtSigned(change) + '), low ' + fmtPrice(minV) + ', high ' + fmtPrice(maxV) + '. The line is the mark price.');
    setAttr(host, 'aria-label', 'Price chart, last ' + RANGE_LABELS[S.drawer.range] + ': ' + fmtPrice(start) + ' to ' + fmtPrice(lastP.price));
  }

  function showPoint(chart, i) {
    if (!chart || i < 0 || i >= chart.pts.length) return;
    chart.index = i;
    const p = chart.pts[i];
    S.drawer.hoverTs = p.ts;
    const x = chart.X(p.ts), y = chart.Y(p.price);
    chart.cline.setAttribute('x1', x.toFixed(1));
    chart.cline.setAttribute('x2', x.toFixed(1));
    chart.cdot.setAttribute('cx', x.toFixed(1));
    chart.cdot.setAttribute('cy', y.toFixed(1));
    chart.cross.setAttribute('visibility', 'visible');
    const s = surgeAt(p.ts, chart.surges);
    const tip = chart.tip;
    // a compact time (no year, seconds or zone) unless a neighbouring point shares the minute
    const minute = function (q) { return q ? Math.floor(q.ts / 60) : null; };
    const seconds = minute(chart.pts[i - 1]) === minute(p) || minute(chart.pts[i + 1]) === minute(p);
    const lines = [
      h('div', { class: 'tooltip-value' }, h('span', { class: 'tooltip-key' + (s ? ' surge' : ''), 'aria-hidden': 'true' }), fmtPrice(p.price),
        h('span', { class: 'tooltip-sub' }, fmtPctOf(p.price))),
      h('div', { class: 'tooltip-sub', title: fmtAbs(p.ts) }, fmtCompact(p.ts, seconds)),
    ];
    if (s) lines.push(h('div', { class: 'tooltip-sub' }, 'During a ' + str(s.window) + ' surge (' + fmtSigned(s.change) + ')'));
    tip.replaceChildren.apply(tip, lines);
    tip.hidden = false;
    placeTooltip(chart, tip, x, y);
  }

  /** Put the readout beside the crosshair (right, else left) without covering the y-axis labels;
   *  when neither side has room (a narrow phone chart), pin it to the top or bottom of the plot,
   *  away from the point, so the point being read stays visible. Positions are CSS pixels. */
  function placeTooltip(chart, tip, x, y) {
    const host = tip.parentNode;
    const scale = host && host.clientWidth && chart.W ? host.clientWidth / chart.W : 1;
    const px = x * scale, py = y * scale;
    const boxW = chart.W * scale, boxH = chart.H * scale;
    const tw = tip.offsetWidth || 150;
    const th = tip.offsetHeight || 56;
    const minLeft = chart.M.l * scale + 2;
    const maxRight = boxW - 4;
    let left, top;
    if (px + 14 + tw <= maxRight) left = px + 14;
    else if (px - 14 - tw >= minLeft) left = px - 14 - tw;
    if (left != null) {
      top = py - th - 12;
      if (top < 0) top = Math.min(boxH - th - 4, py + 14);
    } else {
      // no room on either side: centre it over the crosshair, above or below the point
      left = clamp(px - tw / 2, 4, Math.max(4, boxW - tw - 4));
      const plotTop = chart.M.t * scale, plotBottom = (chart.H - chart.M.b) * scale;
      top = py - plotTop > th + 16 ? plotTop : Math.max(py + 16, Math.min(plotBottom - th, boxH - th - 4));
      if (top < py && top + th > py - 10) top = Math.max(0, py - th - 14);
    }
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(top) + 'px';
  }

  function hidePoint(chart) {
    if (!chart) return;
    chart.index = -1;
    chart.cross.setAttribute('visibility', 'hidden');
    chart.tip.hidden = true;
  }

  function chartFocus(focused) {
    const sec = S.drawer.sections && S.drawer.sections.chart;
    const chart = sec && sec.chart;
    if (!chart) return;
    if (focused) {
      if (chart.tapped && Date.now() - chart.tapped < 1000 && chart.index >= 0) return; // focus came from a tap: keep the tapped point
      showPoint(chart, chart.index >= 0 ? chart.index : chart.pts.length - 1);
    } else if (!chart.pointer) hidePoint(chart);
  }

  /** Arrow-key reading for screen readers: the point's price, chance and time, said politely (debounced). */
  function speakPoint(chart) {
    const sec = S.drawer.sections && S.drawer.sections.chart;
    if (!sec || !chart || chart.index < 0) return;
    const p = chart.pts[chart.index];
    const s = surgeAt(p.ts, chart.surges);
    const text = 'Price ' + fmtPrice(p.price) + ', ' + fmtPctOf(p.price) + ', ' + fmtAbs(p.ts) + (s ? ', during a ' + str(s.window) + ' surge' : '') +
      '. Point ' + (chart.index + 1) + ' of ' + chart.pts.length + '.';
    clearTimeout(S.drawer.liveTimer);
    S.drawer.liveTimer = setTimeout(function () { setText(sec.readout, text); }, 250);
  }

  function chartKey(ev) {
    const sec = S.drawer.sections && S.drawer.sections.chart;
    const chart = sec && sec.chart;
    if (!chart) return;
    const n = chart.pts.length;
    let i = chart.index >= 0 ? chart.index : n - 1;
    if (ev.key === 'ArrowLeft') i -= 1;
    else if (ev.key === 'ArrowRight') i += 1;
    else if (ev.key === 'PageUp') i -= 10;
    else if (ev.key === 'PageDown') i += 10;
    else if (ev.key === 'Home') i = 0;
    else if (ev.key === 'End') i = n - 1;
    else return;
    ev.preventDefault();
    showPoint(chart, clamp(i, 0, n - 1));
    speakPoint(chart);
  }

  function renderChartTable(d) {
    const sec = S.drawer.sections.chart;
    const R = rangeData(d);
    const rows = R.inRange.slice().reverse();
    setText(sec.summary, rows.length ? fmtInt(rows.length) + ' price points in the last ' + RANGE_LABELS[S.drawer.range] + ', newest first.' : '');
    if (!rows.length) {
      rebuild(sec.tableBox, [h('p', { class: 'chart-empty' }, 'No price points in the last ' + RANGE_LABELS[S.drawer.range] + '.')]);
      return;
    }
    const table = h('table', { class: 'data-table' },
      h('caption', { class: 'sr-only' }, 'Price history for the last ' + RANGE_LABELS[S.drawer.range]),
      h('thead', null, h('tr', null, h('th', { scope: 'col', class: 'col-time' }, 'Time'), h('th', { scope: 'col', class: 'num' }, 'Price'),
        h('th', { scope: 'col', class: 'num col-chance' }, 'Chance'), h('th', { scope: 'col', class: 'col-source' }, 'Source'), h('th', { scope: 'col' }, 'Surge'))),
      h('tbody', null, rows.map(function (p) {
        const s = surgeAt(p.ts, R.surges);
        const source = p.source === 'candle' ? 'Candle' : p.source === 'tick' ? 'Snapshot' : p.source || DASH;
        return h('tr', null, h('td', { class: 'col-time', title: fmtAbs(p.ts) }, h('time', { datetime: dateOf(p.ts) ? dateOf(p.ts).toISOString() : null }, fmtTableTime(p.ts))),
          h('td', { class: 'num' }, fmtPrice(p.price)),
          h('td', { class: 'num col-chance' }, fmtPctOf(p.price)),
          h('td', { class: 'col-source', title: p.source === 'candle' ? 'From the price-history candles' : p.source === 'tick' ? 'A live price snapshot taken by the bot' : null }, source),
          h('td', null, s ? h('span', { class: 'surge-tag', title: 'During a ' + str(s.window) + ' surge' }, icon('bolt'), str(s.window), h('span', { class: 'sr-only' }, ' surge')) : ''));
      })));
    rebuild(sec.tableBox, [table]);
    rebuild(sec.legend, []);
  }

  // ---------------------------------------------------------------- drawer: book, trades, surge

  function renderBook(d) {
    const sec = S.drawer.sections.book;
    const book = d.book ? obj(d.book) : null;
    const key = sig([book, d.book_error, d.book_fetched_at, !!d.book_pending]);
    if (S.drawer.sigs.book === key) return;
    if (S.drawer.sigs.book != null && selectionInside(sec)) return;
    S.drawer.sigs.book = key;
    // "Fetched …" only labels a book that loaded; a failed read says when it was last tried.
    const tried = num(d.book_fetched_at) != null;
    const failed = !book && !d.book_pending && !!str(d.book_error) && tried;
    const head = h('div', { class: 'section-head' }, h('h3', { id: 'h-book' }, 'Order book (YES)'),
      tried && (book || failed) ? h('span', { class: 'view-sub' }, failed ? 'Last tried ' : 'Fetched ', timeEl(d.book_fetched_at, 'ago')) : null);
    if (!book && d.book_pending) {
      rebuild(sec, [head, h('p', { class: 'pending-note' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading the order book…')]);
      return;
    }
    if (!book) {
      const msg = failed
        ? h('p', { class: 'muted-note book-error' }, icon('warn'), ' ', h('strong', null, 'Could not load the order book: '), shortProblem(d.book_error), ' Trying again shortly.')
        : h('p', { class: 'muted-note' }, str(d.book_error) || 'No live order book is available for this outcome.');
      if (failed) msg.title = sentence(d.book_error);
      rebuild(sec, [head, msg]);
      return;
    }
    const bids = objs(book.bids).slice(0, 10);
    const asks = objs(book.asks).slice(0, 10);
    const levels = Math.max(bids.length, asks.length);
    let maxQ = 0;
    for (const lv of bids.concat(asks)) maxQ = Math.max(maxQ, num(lv.quantity) || 0);
    const summary = factList([
      ['Best bid', priceEl(book.best_bid)],
      ['Best ask', priceEl(book.best_ask)],
      ['Spread', num(book.spread) == null ? DASH : fmtPrice(book.spread)],
      ['Mid', priceEl(book.mid)],
    ], 'book-summary');
    if (!levels) {
      rebuild(sec, [head, summary, h('p', { class: 'muted-note' }, 'The book is empty: nobody is quoting this outcome right now.')]);
      return;
    }
    function qtyCell(lv, side) {
      const q = num(lv && lv.quantity);
      if (q == null) return h('td', { class: 'qty' });
      // The bar fills a share of its own track (whatever room the cell has), so its length stays
      // proportional to the quantity at every width; the track grows from the price column out.
      const frac = maxQ ? clamp(q / maxQ, 0, 1) : 0;
      const bar = h('span', { class: 'bar' + (frac > 0 ? ' has-qty' : ''), 'aria-hidden': 'true' });
      bar.style.width = (frac * 100).toFixed(2) + '%';
      const track = h('span', { class: 'bar-track', 'aria-hidden': 'true' }, bar);
      const n = h('span', { class: 'qty-num' }, fmtInt(q));
      return h('td', { class: 'qty' }, h('div', { class: 'qty-cell ' + side }, side === 'bid' ? [n, track] : [track, n]));
    }
    const rows = [];
    for (let i = 0; i < levels; i++) {
      const b = bids[i], a = asks[i];
      rows.push(h('tr', { class: i === 0 ? 'best' : null },
        qtyCell(b, 'bid'),
        h('td', { class: 'px bid-px', title: b ? fmtPctOf(b.price) : null }, b ? fmtPrice(b.price) : ''),
        h('td', { class: 'px ask-px', title: a ? fmtPctOf(a.price) : null }, a ? fmtPrice(a.price) : ''),
        qtyCell(a, 'ask')));
    }
    const table = h('table', { class: 'data-table ladder' },
      h('caption', { class: 'sr-only' }, 'Order book ladder: bids on the left, asks on the right, best prices in the first row'),
      h('thead', null, h('tr', null, h('th', { scope: 'col', class: 'num' }, 'Bid size'), h('th', { scope: 'col', class: 'px' }, 'Bid'),
        h('th', { scope: 'col', class: 'px' }, 'Ask'), h('th', { scope: 'col' }, 'Ask size'))),
      h('tbody', null, rows));
    const legend = h('div', { class: 'legend' },
      h('span', { class: 'legend-item' }, h('span', { class: 'key-band key-bid', 'aria-hidden': 'true' }), 'Bids: shares wanted at each price'),
      h('span', { class: 'legend-item' }, h('span', { class: 'key-band key-ask', 'aria-hidden': 'true' }), 'Asks: shares offered at each price'));
    rebuild(sec, [head, summary, h('div', { class: 'table-scroll' }, table), legend]);
  }

  function renderTrades(d) {
    const sec = S.drawer.sections.trades;
    const trades = objs(d.trades).slice(0, 15);
    const key = sig(trades);
    if (S.drawer.sigs.trades === key) return;
    S.drawer.sigs.trades = key;
    const head = h('h3', { id: 'h-trades' }, 'Recent trades');
    if (!trades.length) {
      rebuild(sec, [h('div', { class: 'section-head' }, head),
        h('p', { class: 'muted-note' }, 'No trades recorded for this outcome yet. The bot reads the trade tape when it analyses a surge.')]);
      return;
    }
    const table = h('table', { class: 'data-table' },
      h('caption', { class: 'sr-only' }, 'Recent trades, newest first'),
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'When'), h('th', { scope: 'col' }, 'Side'),
        h('th', { scope: 'col', class: 'num' }, 'Price'), h('th', { scope: 'col', class: 'num' }, 'Size'))),
      h('tbody', null, trades.map(function (t) {
        const side = str(t.side).toUpperCase();
        return h('tr', null, h('td', null, timeEl(t.ts, 'ago')), h('td', { class: side === 'NO' ? 'side-no' : 'side-yes' }, side || DASH),
          h('td', { class: 'num' }, priceEl(t.price)), h('td', { class: 'num' }, fmtInt(t.size)));
      })));
    rebuild(sec, [h('div', { class: 'section-head' }, head), h('div', { class: 'table-scroll' }, table)]);
  }

  function renderDrawerSurge(d) {
    const sec = S.drawer.sections.surge;
    const surges = objs(d.surges);
    const latest = surges[0] || null;
    if (latest) pruneAnalyze(latest);
    const key = sig([latest ? surgeStableKey(latest, true) : null, surges.length]);
    const card = sec.querySelector('.surge-card');
    if (S.drawer.sigs.surge === key || (card && S.drawer.sigs.surge != null && selectionInside(card))) {
      if (card && latest) updateSurgeVolatile(card, latest); // live price and revert share change on every refresh
      return;
    }
    S.drawer.sigs.surge = key;
    const head = h('div', { class: 'section-head' }, h('h3', { id: 'h-dsurge' }, 'Latest surge'),
      surges.length > 1 ? h('span', { class: 'view-sub' }, surges.length + ' surges in the last 7 days') : null);
    if (!latest) {
      rebuild(sec, [head, h('p', { class: 'muted-note' }, 'No surges on this outcome in the last 7 days.')]);
      return;
    }
    rebuild(sec, [head, surgeCard(latest, true)]);
  }

  // ---------------------------------------------------------------- routing

  function parseHash() {
    let raw = (location.hash || '').replace(/^#\/?/, '');
    try {
      raw = decodeURIComponent(raw);
    } catch (e) {
      /* keep the raw text */
    }
    if (raw.indexOf('exchange/') === 0) {
      const id = raw.slice('exchange/'.length).trim();
      if (id) return { exchange: id };
    }
    if (raw.indexOf('strategy/') === 0) {
      // one idea on the Strategy page (#strategy/<idea key>)
      return { view: 'strategy', idea: raw.slice('strategy/'.length) || null };
    }
    return { view: VIEWS.indexOf(raw) !== -1 ? raw : S.view };
  }

  function showView(view, idea) {
    const changed = view !== S.view || !S.viewShown;
    const wasShown = S.viewShown;
    // Was keyboard focus on something this switch hides (a link inside the old view), or nowhere?
    const active = document.activeElement;
    const oldSection = $('view-' + S.view);
    const focusLost = !active || active === document.body || (changed && oldSection && oldSection.contains(active));
    S.view = view;
    S.viewShown = true;
    for (const sec of document.querySelectorAll('main > .view')) show(sec, sec.dataset.view === view);
    safe('nav', renderNav);
    const name = str(S.data.status && S.data.status.tournament && S.data.status.tournament.name) || 'Super Market dashboard';
    document.title = VIEW_TITLES[view] + ' · ' + name;
    if (idea) S.pendingIdea = idea;
    if (changed) {
      if (wasShown) window.scrollTo(0, 0);
      // land on the new view's heading, so screen readers say where the user is (a link that
      // switched views would otherwise leave focus on <body>)
      if (wasShown && focusLost) {
        const head = $('h-' + view);
        if (head) head.focus({ preventScroll: true });
      }
      safe('view', function () { renderView(view); });
      refreshTimes(document);
      poll();
    } else if (idea) {
      safe('view', function () { renderView(view); });
    }
  }

  function route(fromHashChange) {
    const r = parseHash();
    if (r.exchange) {
      if (!S.viewShown) showView(S.view);
      openDrawer(r.exchange, !!fromHashChange && S.viewShown);
    } else {
      closeDrawerUI();
      showView(r.view, r.idea);
    }
  }

  // ---------------------------------------------------------------- wiring

  function setTheme(choice) {
    try {
      if (choice === 'light' || choice === 'dark') window.localStorage.setItem(THEME_KEY, choice);
      else window.localStorage.removeItem(THEME_KEY);
    } catch (e) {
      /* storage may be unavailable (private mode); the choice still applies to this page */
    }
    applyTheme(choice);
    syncThemeButtons();
  }

  function syncThemeButtons() {
    const current = readThemeFromDom();
    for (const b of document.querySelectorAll('[data-theme-choice]')) setAttr(b, 'aria-pressed', b.dataset.themeChoice === current ? 'true' : 'false');
  }

  function readThemeFromDom() {
    const v = document.documentElement.getAttribute('data-theme');
    return v === 'light' || v === 'dark' ? v : 'system';
  }

  /** A focused control in a sideways-scrolling table must not sit under the pinned first column
   *  (WCAG 2.4.11): scroll it out to the right of that column (or back into view on the right). */
  function keepClearOfPinned(wrap, el) {
    if (!el || el === wrap || wrap.scrollWidth <= wrap.clientWidth + 1) return;
    const cell = el.closest('th, td');
    if (!cell || cell.cellIndex === 0) return;
    const row = cell.parentNode;
    const pinned = row && row.cells && row.cells[0];
    if (!pinned || getComputedStyle(pinned).position !== 'sticky') return;
    const r = el.getBoundingClientRect();
    const left = pinned.getBoundingClientRect().right + 8;
    const right = wrap.getBoundingClientRect().right - 8;
    if (r.left < left) wrap.scrollLeft -= left - r.left;
    else if (r.right > right) wrap.scrollLeft += Math.min(r.right - right, r.left - left);
  }

  function init() {
    for (const b of document.querySelectorAll('[data-theme-choice]')) {
      b.addEventListener('click', function () { setTheme(b.dataset.themeChoice); });
    }
    syncThemeButtons();
    window.addEventListener('storage', function (ev) {
      if (ev.key === THEME_KEY) {
        applyTheme(readTheme());
        syncThemeButtons();
      }
    });

    // markets: search, filter chips, sortable headers, clickable rows
    const search = $('market-search');
    search.addEventListener('input', function () {
      S.query = search.value;
      safe('markets', renderMarkets);
      announceMarketsSoon();
    });
    for (const b of document.querySelectorAll('.chip[data-filter]')) b.addEventListener('click', function () { setFilter(b.dataset.filter); });
    for (const th of document.querySelectorAll('#markets-table th[data-sort]')) {
      th.querySelector('button').addEventListener('click', function () {
        const k = th.dataset.sort;
        if (S.sort.key === k) S.sort.dir = S.sort.dir === 'asc' ? 'desc' : 'asc';
        else S.sort = { key: k, dir: TEXT_SORTS[k] || k === 'settles' ? 'asc' : 'desc' };
        safe('markets', renderMarkets);
      });
    }
    function rowClick(ev) {
      if (ev.target.closest('a, button, input, summary')) return;
      const tr = ev.target.closest('tr[data-eid]');
      if (!tr) return;
      const link = tr.querySelector('[data-focus-key]');
      S.pendingReturnKey = link ? link.getAttribute('data-focus-key') : null;
      location.hash = exchangeHref(tr.dataset.eid);
    }
    $('markets-body').addEventListener('click', rowClick);
    $('high-body').addEventListener('click', rowClick);

    // scroll cues on the wide tables, and focus that never hides under the pinned first column
    [['markets-frame', 'markets-wrap'], ['high-frame', 'high-wrap']].forEach(function (pair) {
      const frame = $(pair[0]);
      const wrap = $(pair[1]);
      wrap.addEventListener('scroll', function () { updateScrollCue(frame, wrap); }, { passive: true });
      if (typeof ResizeObserver === 'function') new ResizeObserver(function () { updateScrollCue(frame, wrap); }).observe(wrap);
      wrap.addEventListener('focusin', function (ev) {
        keepClearOfPinned(wrap, ev.target);
        requestAnimationFrame(function () { if (document.activeElement === ev.target) keepClearOfPinned(wrap, ev.target); });
      });
    });

    // problems banner: details fold, and a dismiss that lasts until the failing sources change
    $('problems-details').addEventListener('toggle', function () { S.problemsOpen = $('problems-details').open; });
    $('problems-dismiss').addEventListener('click', function () {
      S.problemsDismissed = S.problemsKey || null;
      showBanner('problems-banner', false);
      $('main').focus({ preventScroll: true });
    });

    // surge re-analysis (delegated: cards are rebuilt on every poll)
    document.addEventListener('click', function (ev) {
      const btn = ev.target.closest('button.reanalyze');
      if (!btn) return;
      ev.preventDefault();
      if (btn.getAttribute('aria-disabled') === 'true') return;
      const id = btn.dataset.surgeId;
      if (id) reanalyze(id);
    });

    // banners
    $('offline-retry').addEventListener('click', function () {
      S.nextRetryAt = 0;
      renderOfflineCountdown();
      poll();
    });
    $('offline-dismiss').addEventListener('click', function () {
      S.offlineDismissed = true;
      show($('offline-banner'), false);
      $('main').focus({ preventScroll: true });
    });

    // simulation: the reset confirmation (Cancel sends nothing; Reset simulation sends one POST)
    const resetDlg = $('sim-reset-dialog');
    $('sim-reset-cancel').addEventListener('click', closeResetDialog);
    $('sim-reset-confirm').addEventListener('click', function () {
      if ($('sim-reset-confirm').getAttribute('aria-disabled') === 'true') return;
      confirmReset();
    });
    resetDlg.addEventListener('cancel', function (ev) {
      ev.preventDefault();
      if (!SIM.resetBusy) closeResetDialog();
    });
    let resetDown = false;
    resetDlg.addEventListener('pointerdown', function (ev) { resetDown = ev.target === resetDlg; });
    resetDlg.addEventListener('click', function (ev) {
      if (ev.target === resetDlg && resetDown && !SIM.resetBusy) closeResetDialog();
      resetDown = false;
    });

    // drawer: close button, Esc, backdrop
    const dlg = drawerEl();
    $('drawer-close').addEventListener('click', requestCloseDrawer);
    dlg.addEventListener('cancel', function (ev) {
      ev.preventDefault();
      requestCloseDrawer();
    });
    let downOnBackdrop = false;
    dlg.addEventListener('pointerdown', function (ev) { downOnBackdrop = ev.target === dlg; });
    dlg.addEventListener('click', function (ev) {
      if (ev.target === dlg && downOnBackdrop) requestCloseDrawer();
      downOnBackdrop = false;
    });
    dlg.addEventListener('close', function () {
      lockPageScroll(false);
      if (S.drawer.id) requestCloseDrawer(); // closed some other way: keep the URL in sync
    });
    $('drawer-inner').addEventListener('focusin', keepFocusVisible);

    const skip = document.querySelector('.skip-link');
    if (skip) {
      skip.addEventListener('click', function (ev) {
        ev.preventDefault();
        $('main').focus();
      });
    }

    window.addEventListener('hashchange', function () { route(true); });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        clearTimeout(S.pollTimer);
        S.pollTimer = null;
        clearTimeout(S.drawer.timer);
        S.drawer.timer = null;
      } else {
        poll();
        if (S.drawer.id) pollDrawer();
      }
    });

    route(false);
    if (!S.viewShown) showView(S.view);
    setInterval(tick, 1000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
