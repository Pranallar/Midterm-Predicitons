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
 * Data flow: poll() fetches /api/status plus the active view's endpoint every 5 s (backing off
 * exponentially on errors, pausing while the tab is hidden); the detail drawer polls
 * /api/exchange/<id> every 10 s while it is open. Renders update the DOM in place so sort,
 * filter, scroll position and keyboard focus survive each poll.
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
  const VIEWS = ['overview', 'markets', 'surges', 'high', 'strategy'];
  const VIEW_TITLES = { overview: 'Overview', markets: 'Markets', surges: 'Surges', high: 'High 90s', strategy: 'Strategy' };
  const VIEW_DATA = {
    overview: ['strategy', 'surges'],
    markets: ['markets'],
    surges: ['surges'],
    high: ['high'],
    strategy: ['strategy'],
  };
  const ENDPOINTS = {
    status: '/api/status',
    markets: '/api/markets',
    surges: '/api/surges',
    high: '/api/highband',
    strategy: '/api/strategy',
  };
  const RANGES = { '6h': 6 * 3600, '24h': 24 * 3600, '7d': 7 * 24 * 3600 };
  const RANGE_LABELS = { '6h': '6 hours', '24h': '24 hours', '7d': '7 days' };
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
    data: { status: null, markets: null, surges: null, high: null, strategy: null },
    offset: 0, // server clock minus browser clock, in seconds
    failures: 0,
    failureText: '',
    nextRetryMs: 0,
    offlineDismissed: false,
    pollTimer: null,
    polling: false,
    pollAgain: false,
    sort: { key: 'title', dir: 'asc' },
    filter: 'all',
    query: '',
    openIdeas: new Set(),
    analyze: {}, // surge id -> {state, text, at}
    sigs: {},
    pendingReturnKey: null,
    drawer: {
      id: null,
      gen: 0,
      data: null,
      error: null,
      timer: null,
      inflight: false,
      again: false,
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
      closing: false,
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
  };

  function icon(name, cls) {
    const svg = svgEl('svg', { class: 'icon' + (cls ? ' ' + cls : ''), viewBox: '0 0 16 16', 'aria-hidden': 'true', focusable: 'false' });
    for (const part of ICONS[name] || ICONS.info) svg.appendChild(svgEl(part[0], part[1]));
    return svg;
  }

  // ---------------------------------------------------------------- shared visual pieces

  function priceEl(v) {
    v = num(v);
    if (v == null) return h('span', { class: 'nil', title: 'No price yet' }, DASH);
    return h('span', { class: 'tabular', title: fmtPctOf(v) + ' implied chance' }, fmtPrice(v));
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
    };
    const m = map[st] || ['question', st ? st.charAt(0).toUpperCase() + st.slice(1) : 'Unknown', ''];
    return h('span', { class: 'status-badge status-' + (map[st] ? st : 'unknown'), title: m[2] || null }, icon(m[0]), m[1] + extra);
  }

  const KINDS = {
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

  // ---------------------------------------------------------------- networking

  function httpError(message, status, body) {
    const err = new Error(message);
    err.status = status || 0;
    err.body = body || null;
    return err;
  }

  async function getJSON(path) {
    const ctrl = new AbortController();
    const timer = setTimeout(function () { ctrl.abort(); }, REQUEST_TIMEOUT_MS);
    let resp;
    try {
      resp = await fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin', signal: ctrl.signal });
    } catch (e) {
      clearTimeout(timer);
      throw httpError(e && e.name === 'AbortError' ? 'The request timed out.' : 'The dashboard server did not answer.', 0);
    }
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

  function isOutage(err) {
    const st = err && err.status;
    return !st || st >= 500;
  }

  function noteFailure(err) {
    S.failures += 1;
    let text = str(err && err.message).trim() || 'The dashboard server did not answer.';
    if (!/[.!?]$/.test(text)) text += '.';
    S.failureText = text;
    S.nextRetryMs = Math.min(MAX_BACKOFF_MS, POLL_MS * Math.pow(2, S.failures));
  }

  function noteSuccess() {
    S.failures = 0;
    S.failureText = '';
    S.offlineDismissed = false;
  }

  function schedule(ms) {
    clearTimeout(S.pollTimer);
    S.pollTimer = null;
    if (document.hidden) return;
    S.pollTimer = setTimeout(poll, ms);
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
    let outage = null;
    try {
      const names = ['status'].concat(VIEW_DATA[S.view] || []);
      const results = await Promise.allSettled(names.map(function (n) { return getJSON(ENDPOINTS[n]); }));
      results.forEach(function (res, i) {
        const name = names[i];
        if (res.status === 'fulfilled') {
          S.data[name] = res.value;
          if (name === 'status' && num(res.value.now) != null) S.offset = res.value.now - Date.now() / 1000;
        } else if (name === 'status' || isOutage(res.reason)) {
          // Any status failure counts (for example 421 when the page was opened at an address the
          // server does not accept): the banner then shows the server's own explanation.
          outage = outage || res.reason;
        }
      });
    } catch (e) {
      outage = e;
    } finally {
      S.polling = false;
    }
    if (outage) noteFailure(outage);
    else noteSuccess();
    renderAll();
    if (S.pollAgain) {
      S.pollAgain = false;
      poll();
      return;
    }
    schedule(outage ? S.nextRetryMs : POLL_MS);
  }

  async function pollDrawer() {
    const D = S.drawer;
    if (!D.id || document.hidden) return;
    if (D.inflight) {
      D.again = true;
      return;
    }
    clearTimeout(D.timer);
    D.timer = null;
    const gen = D.gen;
    const id = D.id;
    D.inflight = true;
    try {
      if (!EXCHANGE_ID_RE.test(id)) throw httpError('Unknown exchange.', 404);
      const data = await getJSON('/api/exchange/' + encodeURIComponent(id));
      if (gen === D.gen) {
        D.data = data;
        D.error = null;
      }
    } catch (e) {
      if (gen === D.gen) D.error = e;
    } finally {
      D.inflight = false;
    }
    if (gen !== D.gen) {
      if (D.id) pollDrawer();
      return;
    }
    safe('drawer', renderDrawer);
    if (D.again) {
      D.again = false;
      pollDrawer();
      return;
    }
    if (D.id && !document.hidden) D.timer = setTimeout(pollDrawer, DRAWER_POLL_MS);
  }

  // ---------------------------------------------------------------- render: shell

  function renderAll() {
    safe('header', renderHeader);
    safe('banners', renderBanners);
    safe('nav', renderNav);
    safe('view', function () { renderView(S.view); });
    refreshTimes(document);
  }

  function renderView(view) {
    if (view === 'overview') renderOverview();
    else if (view === 'markets') renderMarkets();
    else if (view === 'surges') renderSurges();
    else if (view === 'high') renderHigh();
    else if (view === 'strategy') renderStrategy();
  }

  function liveState() {
    if (S.failures > 0) return { state: 'offline', label: 'Offline', title: 'The dashboard server is not answering' };
    const st = S.data.status;
    if (!st) return { state: 'loading', label: 'Connecting…', title: 'Waiting for the dashboard server' };
    const tr = obj(st.tracker);
    const interval = num(st.interval) || num(tr.interval) || 30;
    if (tr.fatal_error) return { state: 'stopped', label: 'Stopped', title: 'The tracker stopped: see the message below' };
    const last = num(tr.last_snapshot_at);
    if (last == null) return { state: 'loading', label: 'Starting…', title: 'Waiting for the first price snapshot' };
    const age = nowS() - last;
    if (age < 2 * interval) {
      return { state: 'fresh', label: 'Live', title: 'Prices refresh every ' + fmtSpan(interval) + '; the last snapshot was ' + fmtAgo(last) };
    }
    return { state: 'stale', label: 'Stale', title: 'No new price snapshot for ' + fmtSpan(age) + ' (expected every ' + fmtSpan(interval) + ')' };
  }

  function renderHeader() {
    const st = S.data.status;
    const live = liveState();
    const liveEl = $('live');
    setAttr(liveEl, 'data-state', live.state);
    setAttr(liveEl, 'title', live.title);
    setText($('live-label'), live.label);
    if (!st) return;

    const t = obj(st.tournament);
    const name = str(t.name) || 'Super Market dashboard';
    setText($('tournament-name'), name);
    setText($('currency-note'), 'Currency: ' + currency());
    const title = VIEW_TITLES[S.view] + ' · ' + name;
    if (document.title !== title) document.title = title;
    show($('demo-badge'), !!st.demo);

    const tr = obj(st.tracker);
    const last = num(tr.last_snapshot_at);
    const upd = $('updated');
    if (last != null && dateOf(last)) {
      setText(upd, fmtAgo(last));
      setAttr(upd, 'title', 'Last price snapshot: ' + fmtAbs(last));
      setAttr(upd, 'datetime', dateOf(last).toISOString());
    } else {
      setText(upd, 'not yet');
      setAttr(upd, 'title', 'No price snapshot yet');
    }

    const rb = obj(tr.read_budget);
    const used = num(rb.used);
    const limit = num(rb.limit);
    const meter = $('budget-meter');
    if (used == null || !limit) {
      setText($('budget-text'), DASH);
      $('budget-fill').style.width = '0%';
      setAttr(meter, 'aria-valuenow', 0);
      setAttr(meter, 'aria-valuetext', 'Unknown');
      setAttr(meter, 'data-level', null);
    } else {
      const frac = clamp(used / limit, 0, 1);
      $('budget-fill').style.width = (frac * 100).toFixed(1) + '%';
      setText($('budget-text'), fmtInt(used) + ' / ' + fmtInt(limit) + ' per min');
      setAttr(meter, 'aria-valuenow', Math.round(frac * 100));
      setAttr(meter, 'aria-valuetext', fmtInt(used) + ' of ' + fmtInt(limit) + ' API reads used in the last minute');
      setAttr(meter, 'data-level', frac >= 0.95 ? 'critical' : frac >= 0.8 ? 'warning' : null);
      setAttr($('budget'), 'title', 'API reads used in the last minute (the account limit is shared by every key)');
    }
  }

  function fatalHint(message) {
    if (/API_KEY|INVALID_API_KEY|REVOKED|UNAUTHORI[SZ]ED|FORBIDDEN|\b401\b|\b403\b|api key|expired/i.test(message)) {
      return 'Your API key was rejected: it may have been revoked, expired or mistyped. Put a valid key in SUPERMARKET_API_KEY ' +
        '(in your .env file), then restart the dashboard. To explore without a key, start it with --demo.';
    }
    return 'Restart the dashboard. If this keeps happening, run it with -v to see more detail in the terminal.';
  }

  function renderBanners() {
    const st = S.data.status;
    const tr = obj(st && st.tracker);

    const fatal = str(tr.fatal_error);
    show($('fatal-banner'), !!fatal);
    if (fatal) {
      setText($('fatal-message'), fatal);
      setText($('fatal-hint'), fatalHint(fatal));
    }

    const offline = S.failures > 0 && !S.offlineDismissed;
    show($('offline-banner'), offline);
    if (offline) {
      const secs = Math.round(S.nextRetryMs / 1000);
      setText($('offline-message'), S.failureText + ' Is the dashboard still running in your terminal? Trying again in ' + secs + ' s.');
    }

    const viewError = str(st && st.view_error);
    show($('view-error-banner'), !!viewError && !fatal);
    if (viewError) setText($('view-error-message'), 'The tracker could not build its live view: ' + viewError);

    let startup = '';
    let progress = null;
    if (st && !fatal) {
      const bf = obj(tr.backfill);
      const done = num(bf.done);
      const total = num(bf.total);
      if (!trackerStarted()) {
        startup = 'Taking the first price snapshot. Prices and charts appear in a few seconds.';
      } else if (total && done != null && !bf.complete && done < total) {
        startup = 'Loading price history: ' + fmtInt(done) + ' of ' + fmtInt(total) + ' series so far. Charts and surge detection fill in as it arrives.';
        progress = clamp(done / total, 0, 1);
      }
    }
    show($('startup-banner'), !!startup);
    if (startup) setText($('startup-message'), startup);
    const meter = $('startup-meter');
    show(meter, progress != null);
    if (progress != null) {
      $('startup-fill').style.width = (progress * 100).toFixed(1) + '%';
      setAttr(meter, 'aria-valuenow', Math.round(progress * 100));
    }
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

  function setTile(id, value, sub, unit) {
    const el = $('tile-' + id);
    const key = sig([value, unit]);
    if (el.dataset.sig !== key) {
      el.dataset.sig = key;
      el.replaceChildren(document.createTextNode(value));
      if (unit) el.appendChild(h('span', { class: 'tile-unit' }, unit));
    }
    setText($('tile-' + id + '-sub'), sub || ' ');
  }

  function renderOverview() {
    const st = S.data.status;
    if (st) {
      const c = obj(st.counts);
      const acct = obj(st.account);
      setTile('outcomes', fmtInt(c.outcomes), num(c.markets) != null ? 'in ' + fmtInt(c.markets) + ' markets' : '');
      setTile('surges', fmtInt(c.surges_last_hour), num(c.surges) != null ? fmtInt(c.surges) + ' in the last 2 days' : '');
      setTile('participants', fmtInt(c.open_participant_surges),
        num(c.open_participant_surges) ? 'possible fades: see Strategy' : 'none to fade right now');
      setTile('high', fmtInt(c.high_band), 'favourite side at 0.95 or more');
      const bal = num(acct.balance);
      const parts = [];
      if (num(acct.my_rank) != null) parts.push('Rank #' + fmtInt(acct.my_rank));
      if (num(acct.leader_value) != null) parts.push('leader about ' + fmtInt(acct.leader_value));
      if (!parts.length && num(acct.initial_balance) != null) parts.push('started with ' + fmtInt(acct.initial_balance));
      setTile('balance', bal == null ? DASH : fmtInt(bal), parts.join(' · ') || 'balance not known yet', bal == null ? '' : currency());
      const tile = $('tile-balance');
      setAttr(tile, 'title', bal == null ? null : fmtMoney(bal) + ' ' + currency());

      const end = cupEnd();
      const days = num(st.days_left);
      let sub = num(c.outcomes) != null && num(c.markets) != null
        ? 'Watching ' + fmtInt(c.outcomes) + ' outcomes in ' + fmtInt(c.markets) + ' markets.'
        : 'What the bot is watching right now.';
      if (end != null) sub += ' The Cup ends ' + fmtDay(end) + (days != null ? ' (' + fmtSpan(days * 86400) + ' left).' : '.');
      setText($('overview-sub'), sub);
    }
    renderLookIdeas();
    renderLookSurges();
  }

  function ideaKey(o) {
    return [str(o.kind), hasId(o.exchange_id) ? 'x' + o.exchange_id : 'm' + str(o.market_id), str(o.side), str(o.surge_id)].join(':');
  }

  function actionText(o) {
    const side = str(o.side).toUpperCase() || '?';
    let text = 'BUY ' + side + ' @ ' + fmtPrice(o.entry_price);
    if (!hasId(o.exchange_id) && str(o.kind) === 'arbitrage') text += ' per full set';
    return text;
  }

  function outcomeLabel(title, option) {
    const t = str(title) || 'Untitled market';
    return option ? t + ' — ' + option : t;
  }

  function renderLookIdeas() {
    const list = $('look-ideas');
    const empty = $('look-ideas-empty');
    const d = S.data.strategy;
    let items = null;
    let msg = '';
    if (!d) msg = 'Loading ideas…';
    else if (d.available === false) msg = str(d.error) || 'The strategy report is not available yet.';
    else {
      items = objs(d.opportunities).slice(0, 5);
      if (!items.length) msg = 'No trade ideas right now. The bot keeps looking every few seconds.';
    }
    const key = sig([msg, items]);
    if (S.sigs.lookIdeas === key) return;
    S.sigs.lookIdeas = key;
    show(empty, !!msg);
    setText(empty, msg);
    rebuild(list, (items || []).map(function (o, i) {
      const href = hasId(o.exchange_id) ? exchangeHref(o.exchange_id) : '#strategy';
      return h('li', { class: 'look-item' },
        h('a', { href: href, 'data-focus-key': 'idea:' + ideaKey(o), 'data-eid': hasId(o.exchange_id) ? String(o.exchange_id) : null },
          h('span', { class: 'look-rank' }, h('span', { class: 'sr-only' }, 'Rank '), String(i + 1)),
          h('span', { class: 'look-main' },
            h('span', { class: 'look-title' }, outcomeLabel(o.title, o.option)),
            h('span', { class: 'look-meta' }, kindBadge(o.kind), h('strong', null, actionText(o)))),
          h('span', { class: 'look-side' },
            num(o.prob_win) != null
              ? h('span', null, 'Win ' + fmtPct0(o.prob_win))
              : h('span', null, 'Return ' + fmtPctOf(o.expected_return)),
            h('span', null, 'Edge ' + fmtSigned(o.edge) + '/share'))));
    }));
  }

  function renderLookSurges() {
    const list = $('look-surges');
    const empty = $('look-surges-empty');
    const d = S.data.surges;
    let items = null;
    let msg = '';
    if (!d) msg = 'Loading surges…';
    else {
      items = objs(d.surges).slice(0, 5);
      if (!items.length) {
        const n = outcomesWatched();
        msg = trackerStarted() || n
          ? 'No surges yet — watching ' + fmtInt(n) + ' outcomes.'
          : 'Waiting for the first price snapshot…';
      }
    }
    const key = sig([msg, items && items.map(function (s) {
      return [s.id, s.change, s.window, s.status, s.attribution && s.attribution.verdict, s.attribution && s.attribution.confidence, s.title, s.option];
    })]);
    if (S.sigs.lookSurges !== key) {
      S.sigs.lookSurges = key;
      show(empty, !!msg);
      setText(empty, msg);
      rebuild(list, (items || []).map(function (s) {
        return h('li', { class: 'look-item' },
          h('a', { href: hasId(s.exchange_id) ? exchangeHref(s.exchange_id) : '#surges', 'data-focus-key': 'surge:' + surgeKey(s),
            'data-eid': hasId(s.exchange_id) ? String(s.exchange_id) : null },
            h('span', { class: 'look-rank', 'aria-hidden': 'true' }, icon('bolt')),
            h('span', { class: 'look-main' },
              h('span', { class: 'look-title' }, outcomeLabel(s.title, s.option)),
              h('span', { class: 'look-meta' }, deltaEl(s.change), h('span', null, 'over ' + str(s.window || '?')),
                timeEl(s.detected_at, 'ago'))),
            h('span', { class: 'look-side' }, verdictBadge(s.attribution))));
      }));
    }
  }

  // ---------------------------------------------------------------- render: markets

  const ROWS = new WeakMap();

  const SORT_KEYS = {
    title: function (r) { return str(r.title).toLowerCase(); },
    option: function (r) { return str(r.option).toLowerCase(); },
    last: function (r) { return num(r.last) != null ? num(r.last) : num(r.mark); },
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
    cell('title', 'cell-title').appendChild(link);
    cells.link = link;
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
    if (after) wrap.appendChild(h('span', { class: 'cell-sub settle-after' }, icon('clock'), 'after Cup end'));
    return wrap;
  }

  function updateMarketRow(tr, r) {
    const rec = ROWS.get(tr);
    const c = rec.cells;
    setText(c.link, str(r.title) || 'Untitled market');
    setCell(rec, 'option', [r.option], function () { return document.createTextNode(str(r.option) || DASH); });
    const last = num(r.last) != null ? r.last : r.mark;
    setCell(rec, 'last', [last], function () { return priceEl(last); });
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
    if (!d) msg = 'Loading markets…';
    else if (!rows.length) msg = trackerStarted() ? 'No open markets were found in this tournament.' : 'Waiting for the first price snapshot…';
    else if (!shown.length) {
      const what = S.filter === 'surging' ? ' surging' : S.filter === 'high' ? ' high-90s' : '';
      msg = terms.length ? 'No' + what + ' markets match “' + S.query.trim() + '”.' : 'No' + what + ' markets right now.';
      action = h('button', { type: 'button', class: 'btn', onclick: clearMarketFilters }, 'Show all markets');
    }
    show(wrap, !!shown.length);
    show(empty, !!msg);
    if (msg && empty.dataset.msg !== msg) {
      empty.dataset.msg = msg;
      rebuild(empty, [document.createTextNode(msg), action]);
    }
    setText($('markets-count'), d ? 'Showing ' + fmtInt(shown.length) + ' of ' + fmtInt(rows.length) + ' outcomes' : '');
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
  }

  // ---------------------------------------------------------------- render: surges

  const CARD_SIGS = new WeakMap();

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

  function analyzeFooter(s, key) {
    if (!hasId(s.id) || !/^[0-9]{1,12}$/.test(String(s.id))) return null; // the server only knows numeric surge ids
    const st = S.analyze[s.id];
    if (st && st.state === 'done' && Date.now() - st.at > 60000) delete S.analyze[s.id];
    const cur = S.analyze[s.id];
    const pending = !!(cur && cur.state === 'pending');
    const btn = h('button', {
      type: 'button',
      class: 'btn reanalyze',
      'data-focus-key': key + ':reanalyze',
      'data-surge-id': String(s.id),
      'aria-disabled': pending ? 'true' : null,
      title: 'Search the news and the trade tape again for this move',
    }, icon('refresh'), pending ? 'Sending…' : 'Re-analyze');
    const msg = h('span', { class: 'action-msg' + (cur && cur.state === 'error' ? ' is-error' : '') });
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

  function surgeCard(s, compact) {
    s = obj(s);
    const key = surgeKey(s);
    const att = s.attribution ? obj(s.attribution) : null;
    const card = h(compact ? 'div' : 'article', { class: 'card surge-card', 'data-key': key });

    const titles = h('div', { class: 'card-titles' });
    if (compact) {
      titles.appendChild(h('h4', { class: 'card-title' }, str(s.window || '?') + ' surge, detected ', timeEl(s.detected_at, 'ago')));
    } else {
      titles.appendChild(h('h3', { class: 'card-title' }, hasId(s.exchange_id)
        ? h('a', { href: exchangeHref(s.exchange_id), 'data-focus-key': key + ':title', 'data-eid': String(s.exchange_id) }, str(s.title) || 'Untitled market')
        : str(s.title) || 'Untitled market'));
      titles.appendChild(h('p', { class: 'card-sub' }, 'Outcome: ' + (str(s.option) || DASH)));
    }
    card.appendChild(h('div', { class: 'card-head' }, titles, h('div', { class: 'card-badges' }, verdictBadge(att), statusBadge(s))));

    card.appendChild(h('div', { class: 'move-line' },
      h('span', { class: 'move-main' },
        h('span', { title: fmtPctOf(s.start_price) + ' ' + ARROW + ' ' + fmtPctOf(s.end_price) }, fmtPrice(s.start_price) + ' ' + ARROW + ' ' + fmtPrice(s.end_price)),
        ' ', deltaEl(s.change, 0)),
      h('span', { class: 'move-fact' }, 'Window ', h('strong', null, str(s.window) || DASH)),
      h('span', { class: 'move-fact', title: 'How unusual the move is versus this outcome’s own recent volatility (3 or more is unusual)' },
        'z-score ', h('strong', null, fmtZ(s.zscore))),
      h('span', { class: 'move-fact' }, 'Peak ', h('strong', null, fmtPrice(s.peak_price))),
      h('span', { class: 'move-fact' }, 'Now ', h('strong', null, fmtPrice(s.current_price))),
      compact ? null : h('span', { class: 'move-fact' }, 'Detected ', h('strong', null, timeEl(s.detected_at, 'ago')))));

    if (!att) {
      card.appendChild(h('p', { class: 'pending-note' },
        analysisEnabled() ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : icon('info'),
        analysisEnabled()
          ? 'Working out whether news or other traders caused this move. The verdict usually appears within a minute.'
          : 'Surge analysis is turned off for this dashboard.'));
    } else {
      if (att.summary) card.appendChild(h('p', { class: 'card-summary' }, str(att.summary)));
      const left = h('div', null,
        h('h4', { class: 'card-section-title' }, 'Why the bot thinks so'),
        h('ul', { class: 'reasons' }, texts(att.reasons).map(function (r) { return h('li', null, r); })));
      const right = h('div', null,
        factList([
          ['Reversion odds', fmtPct0(att.reversion_odds), 'Estimated chance the move gives back at least half'],
          ['Confidence', fmtPct0(att.confidence)],
        ]),
        h('h4', { class: 'card-section-title' }, 'Trade flow'),
        flowFacts(att.flow, num(att.book_depth)),
        h('h4', { class: 'card-section-title' }, 'Headlines'),
        objs(att.articles).length
          ? headlineList(att.articles, key)
          : h('p', { class: 'muted-note' }, newsEnabled() ? 'No matching headlines were found around the move.' : 'News search is turned off for this dashboard.'));
      card.appendChild(h('div', { class: 'card-grid' }, left, right));
    }
    const foot = analyzeFooter(s, key);
    if (foot) card.appendChild(foot);
    return card;
  }

  function renderSurges() {
    const d = S.data.surges;
    const box = $('surge-cards');
    const empty = $('surges-empty');
    const list = d ? objs(d.surges) : null;
    let msg = '';
    if (!d) msg = 'Loading surges…';
    else if (!list.length) {
      const n = outcomesWatched();
      msg = trackerStarted() || n ? 'No surges yet — watching ' + fmtInt(n) + ' outcomes.' : 'Waiting for the first price snapshot…';
    }
    show(empty, !!msg);
    setText(empty, msg);
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
      const key = surgeKey(s);
      const k = sig([s, S.analyze[s.id] || null, analysisEnabled(), newsEnabled()]);
      let el = existing.get(key);
      if (!el || CARD_SIGS.get(el) !== k) {
        const fresh = surgeCard(s, false);
        CARD_SIGS.set(fresh, k);
        if (el) {
          const active = document.activeElement;
          const fkey = active && el.contains(active) ? active.getAttribute('data-focus-key') : null;
          el.replaceWith(fresh);
          if (fkey) {
            const t = fresh.querySelector('[data-focus-key="' + CSS.escape(fkey) + '"]');
            if (t) t.focus({ preventScroll: true });
          }
        }
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
    S.analyze[id] = { state: 'pending', text: '', at: Date.now() };
    rerenderSurgeViews();
    let result;
    try {
      const resp = await fetch('/api/surges/' + encodeURIComponent(String(id)) + '/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: '{}',
        cache: 'no-store',
        credentials: 'same-origin',
      });
      let body = null;
      try {
        body = await resp.json();
      } catch (e) {
        body = null;
      }
      if (resp.ok) result = { state: 'done', text: str(body && body.message) || 'Queued for analysis.', at: Date.now() };
      else result = { state: 'error', text: str(body && body.error) || 'Could not queue the analysis (HTTP ' + resp.status + ').', at: Date.now() };
    } catch (e) {
      result = { state: 'error', text: 'Could not reach the bot to queue the analysis.', at: Date.now() };
    }
    S.analyze[id] = result;
    rerenderSurgeViews();
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

  function renderHigh() {
    const d = S.data.high;
    const tbody = $('high-body');
    const empty = $('high-empty');
    const bands = d ? objs(d.bands).filter(function (b) { return hasId(b.exchange_id); }) : [];
    let msg = '';
    if (!d) msg = 'Loading…';
    else if (!bands.length) {
      const n = outcomesWatched();
      msg = trackerStarted() || n
        ? 'Nothing is hovering in the high 90s right now — watching ' + fmtInt(n) + ' outcomes.'
        : 'Waiting for the first price snapshot…';
    }
    show(empty, !!msg);
    setText(empty, msg);
    show($('high-wrap'), bands.length > 0);
    const key = sig([bands, cupEnd()]);
    if (S.sigs.high === key) return;
    S.sigs.high = key;
    rebuild(tbody, bands.map(function (b) {
      b = obj(b);
      const eid = str(b.exchange_id);
      const side = str(b.side).toUpperCase() || 'YES';
      const lookH = num(b.lookback_s) != null ? fmtSpan(b.lookback_s) : '6 h';
      const settleTs = num(b.settlement_ts) != null ? b.settlement_ts : toTs(b.settlement_date);
      const entry = num(b.entry_price);
      return h('tr', { class: 'clickable', 'data-eid': eid },
        h('td', { class: 'cell-title' }, h('a', { class: 'row-link', href: exchangeHref(eid), 'data-focus-key': 'high:' + eid, 'data-eid': eid }, str(b.title) || 'Untitled market')),
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
        h('td', null, yesNo(b.settles_before_cup_end, 'Yes, pays out', 'No — valued at market price', 'Unknown date')),
        h('td', { class: 'num' }, num(b.payout_per_share) == null ? DASH : fmtSigned(b.payout_per_share) + '/share',
          h('span', { class: 'cell-sub' }, num(b.return_pct) == null ? '' : nf(1, 2).format(b.return_pct) + '% return')));
    }));
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

  function ideaCard(o, i) {
    o = obj(o);
    const key = ideaKey(o);
    const titleText = str(o.title) || 'Untitled market';
    const titleNode = hasId(o.exchange_id)
      ? h('a', { href: exchangeHref(o.exchange_id), 'data-focus-key': 'st:' + key + ':title', 'data-eid': String(o.exchange_id) }, titleText)
      : document.createTextNode(titleText);
    const action = h('p', { class: 'idea-action' }, h('strong', null, actionText(o)));
    if (num(o.target_price) != null) action.appendChild(document.createTextNode(' · target ' + fmtPrice(o.target_price)));
    if (num(o.stop_price) != null) action.appendChild(document.createTextNode(' · stop ' + fmtPrice(o.stop_price)));
    const shares = num(o.suggested_shares) || 0;
    const sizeText = shares > 0
      ? fmtInt(shares) + ' shares · ' + fmtMoney(o.suggested_cost) + ' ' + currency()
      : 'none yet: wait for confirmation';
    const facts = factList([
      ['Win probability', fmtPct0(o.prob_win), num(o.prob_win) == null ? 'Not estimated for this kind of idea' : null],
      ['Edge per share', num(o.edge) == null ? DASH : fmtSigned(o.edge), 'Expected profit per share after the stop'],
      ['Expected return', fmtPctOf(o.expected_return), 'Edge as a share of the entry price'],
      ['Suggested size', sizeText, null, 'wide'],
      ['Score', fmtScore(o.score), 'Expected return × confidence, adjusted for the risk mode'],
      ['Confidence', fmtPct0(o.confidence)],
      num(o.horizon_hours) != null ? ['Horizon', fmtSpan(o.horizon_hours * 3600)] : null,
      ['Settles before Cup end', o.settles_before_cup_end === true ? 'Yes' : o.settles_before_cup_end === false ? 'No' : 'Unknown'],
    ]);
    const details = h('details', { 'data-idea': key },
      h('summary', { 'data-focus-key': 'st:' + key + ':why' }, 'Why this idea, and the risks'),
      h('h4', null, 'Rationale'),
      texts(o.rationale).length
        ? h('ul', { class: 'reasons' }, texts(o.rationale).map(function (r) { return h('li', null, r); }))
        : h('p', { class: 'muted-note' }, 'No rationale given.'),
      h('h4', null, 'Risks'),
      texts(o.risks).length
        ? h('ul', { class: 'reasons' }, texts(o.risks).map(function (r) { return h('li', null, r); }))
        : h('p', { class: 'muted-note' }, 'No specific risks listed.'));
    if (S.openIdeas.has(key)) details.open = true;
    details.addEventListener('toggle', function () {
      if (details.open) S.openIdeas.add(key);
      else S.openIdeas.delete(key);
    });
    return h('li', { class: 'card idea' },
      h('span', { class: 'idea-rank' }, h('span', { class: 'sr-only' }, 'Rank '), String(i + 1)),
      h('div', { class: 'idea-main' },
        h('div', { class: 'idea-head' }, kindBadge(o.kind),
          h('h3', { class: 'idea-title' }, titleNode, o.option ? h('span', { class: 'card-sub' }, ' — ' + str(o.option)) : null)),
        action, facts, details));
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

  function renderStrategy() {
    const d = S.data.strategy;
    const root = $('strategy-body');
    const key = sig([d, S.data.status && S.data.status.tournament && S.data.status.tournament.currency]);
    if (S.sigs.strategy === key) return;
    S.sigs.strategy = key;
    if (!d) {
      rebuild(root, [emptyNote('Loading the strategy report…')]);
      return;
    }
    if (d.available === false) {
      rebuild(root, [h('div', { class: 'banner banner-warning' }, icon('warn', 'icon-lg'),
        h('div', { class: 'banner-body' }, h('h3', { class: 'banner-title' }, 'The strategy report is not available'),
          h('p', null, str(d.error) || 'Try again in a few seconds.')))]);
      return;
    }
    const mode = MODES[str(d.risk_mode)] || MODES.balanced;
    const factsItems = [
      ['Balance', num(d.balance) == null ? DASH : fmtMoney(d.balance) + ' ' + currency()],
      ['Rank', num(d.my_rank) == null ? DASH : '#' + fmtInt(d.my_rank)],
      ['Leader (est.)', num(d.leader_value) == null ? DASH : fmtInt(d.leader_value) + ' ' + currency(), 'Starting balance plus the leader’s reported profit'],
      ['Days left', num(d.days_left) == null ? DASH : nf(0, 1).format(d.days_left)],
    ];
    const risk = h('section', { class: 'risk-banner', 'data-mode': str(d.risk_mode) || 'balanced', 'aria-labelledby': 'h-risk' },
      icon(mode.icon),
      h('div', { class: 'banner-body' },
        h('h3', { class: 'risk-mode', id: 'h-risk' }, mode.label),
        h('p', { class: 'risk-explain' }, mode.text),
        factList(factsItems, 'risk-facts')));
    const opps = objs(d.opportunities);
    const ideasPanel = h('section', { class: 'panel', 'aria-labelledby': 'h-ideas' },
      h('h3', { class: 'panel-title', id: 'h-ideas' }, opps.length ? 'Ranked ideas (' + opps.length + ')' : 'Ranked ideas'),
      opps.length
        ? h('ol', { class: 'ideas' }, opps.map(ideaCard))
        : emptyNote('No trade ideas right now. Ideas appear when the bot sees a participant-driven surge, a stable high-90s favourite or prices that do not add up.'));
    const principles = h('section', { class: 'panel', 'aria-labelledby': 'h-principles' },
      h('p', { class: 'headline-text' }, str(d.headline)),
      h('h3', { class: 'panel-title', id: 'h-principles' }, 'How to play the Cup'),
      texts(d.principles).length
        ? h('ol', { class: 'principles' }, texts(d.principles).map(function (p) { return h('li', null, p); }))
        : null);
    const disclaimer = h('p', { class: 'disclaimer' }, icon('lock'), str(d.disclaimer) || 'Read-only analysis. Nothing is traded automatically.');
    rebuild(root, [risk, principles, ideasPanel, backtestPanel(d), disclaimer]);
  }

  // ---------------------------------------------------------------- drawer

  const drawerEl = function () { return $('drawer'); };

  function openDrawer(id, pushed) {
    const D = S.drawer;
    const dlg = drawerEl();
    if (D.id !== id) {
      D.id = id;
      D.gen += 1;
      D.data = null;
      D.error = null;
      D.hoverTs = null;
      D.sections = null;
      D.sigs = {};
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
      dlg.showModal();
      $('drawer-inner').scrollTop = 0;
      $('drawer-title').focus({ preventScroll: true });
    }
    pollDrawer();
  }

  function closeDrawerUI() {
    const D = S.drawer;
    D.closing = false;
    if (!D.id) return;
    D.id = null;
    D.gen += 1;
    clearTimeout(D.timer);
    D.timer = null;
    D.sections = null;
    if (D.ro) D.ro.disconnect();
    D.ro = null;
    D.chartWidth = 0;
    const dlg = drawerEl();
    if (dlg.open) dlg.close();
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

  function renderDrawer() {
    const D = S.drawer;
    if (!D.id) return;
    const d = D.data;
    const err = D.error;
    const body = $('drawer-body');
    const row = drawerRow();
    if (d) {
      setText($('drawer-title'), str(row.title) || 'Untitled market');
      const bits = ['Outcome: ' + (str(row.option) || DASH)];
      if (row.exchange_id != null) bits.push('exchange ' + str(row.exchange_id));
      if (row.market_id != null) bits.push('market ' + str(row.market_id));
      setText($('drawer-sub'), bits.join(' · '));
    } else if (err) {
      setText($('drawer-title'), err.status === 404 ? 'Outcome not found' : 'Could not load this outcome');
      setText($('drawer-sub'), 'Exchange ' + D.id);
    } else {
      setText($('drawer-title'), 'Loading…');
      setText($('drawer-sub'), 'Exchange ' + D.id);
    }

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
        facts: h('section', { class: 'panel', 'aria-label': 'Prices' }),
        chart: buildChartSection(),
        book: h('section', { class: 'panel', 'aria-labelledby': 'h-book' }),
        trades: h('section', { class: 'panel', 'aria-labelledby': 'h-trades' }),
        surge: h('section', { class: 'panel', 'aria-labelledby': 'h-dsurge' }),
      };
      D.sigs = {};
      rebuild(body, [D.sections.facts, D.sections.chart.root, D.sections.surge, D.sections.book, D.sections.trades]);
    }
    renderDrawerFacts(d);
    renderChartSection(d);
    renderDrawerSurge(d);
    renderBook(d);
    renderTrades(d);
    refreshTimes(body);
  }

  function renderDrawerFacts(d) {
    const row = obj(d.exchange);
    const band = d.high_band ? obj(d.high_band) : null;
    const key = sig([row.last, row.mark, row.bid, row.ask, row.spread, row.change_5m, row.change_1h, row.change_24h, row.settlement_date, band, cupEnd()]);
    if (S.drawer.sigs.facts === key) return;
    S.drawer.sigs.facts = key;
    const settle = toTs(row.settlement_date);
    const end = cupEnd();
    const items = [
      ['Last', priceEl(num(row.last) != null ? row.last : row.mark)],
      ['Bid', priceEl(row.bid)],
      ['Ask', priceEl(row.ask)],
      ['Spread', num(row.spread) == null ? DASH : fmtPrice(row.spread)],
      ['Change 5m', deltaEl(row.change_5m)],
      ['Change 1h', deltaEl(row.change_1h)],
      ['Change 24h', deltaEl(row.change_24h)],
      ['Settles', settle == null ? DASH : h('span', null, timeEl(settle, 'day'), end != null && settle > end ? ' (after Cup end)' : ''), null,
        end != null && settle != null && settle > end ? 'wide' : null],
    ];
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
    const toggle = h('button', {
      type: 'button', class: 'btn', 'data-action': 'table-toggle', 'aria-pressed': S.drawer.table ? 'true' : 'false',
      onclick: function () { setTableView(!S.drawer.table); },
    });
    const host = h('div', {
      class: 'chart', tabindex: '0', role: 'group',
      'aria-label': 'Price chart. Use the left and right arrow keys to read individual prices.',
    });
    const tableBox = h('div', { class: 'chart-table-wrap', tabindex: '0', role: 'region', 'aria-label': 'Price history table' });
    const legend = h('div', { class: 'legend' });
    const summary = h('p', { class: 'view-sub chart-summary' });
    const root = h('section', { class: 'panel', 'aria-labelledby': 'h-chart' },
      h('div', { class: 'section-head' }, h('h3', { id: 'h-chart' }, 'Price history'), h('div', { class: 'controls' }, rangeGroup, toggle)),
      summary, host, tableBox, legend);
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
    return { root: root, host: host, tableBox: tableBox, legend: legend, toggle: toggle, rangeGroup: rangeGroup, summary: summary, chart: null };
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
    const tkey = sig([S.drawer.table]);
    if (sec.toggle.dataset.sig !== tkey) {
      sec.toggle.dataset.sig = tkey;
      sec.toggle.replaceChildren(icon(S.drawer.table ? 'chart' : 'table'), S.drawer.table ? 'Show chart' : 'Show table');
    }
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
    const surges = objs(d.surges).filter(function (s) {
      return num(s.start_ts) != null && num(s.end_ts) != null && s.end_ts >= t0 && s.start_ts <= t1;
    });
    return { all: all, inRange: inRange, before: before, t0: t0, t1: t1, surges: surges };
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

  function timeTicks(t0, t1, width) {
    const span = t1 - t0;
    const maxTicks = Math.max(2, Math.floor(width / 96));
    const steps = [900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400, 172800];
    let step = steps[steps.length - 1];
    for (const s of steps) {
      if (span / s <= maxTicks) {
        step = s;
        break;
      }
    }
    const out = [];
    const tz = -new Date(t0 * 1000).getTimezoneOffset() * 60;
    for (let t = Math.ceil((t0 + tz) / step) * step - tz; t <= t1 && out.length < 40; t += step) {
      const dt = new Date(t * 1000);
      const midnight = dt.getHours() === 0 && dt.getMinutes() === 0;
      out.push({ t: t, label: step >= 86400 || midnight ? DT_DAY.format(dt) : DT_TIME.format(dt) });
    }
    return out;
  }

  function surgeAt(ts, surges) {
    for (const s of surges) if (ts >= s.start_ts && ts <= s.end_ts) return s;
    return null;
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
      rebuild(host, [h('div', { class: 'chart-empty' }, 'Not enough price history in the last ' + RANGE_LABELS[S.drawer.range] + ' yet. Try a longer range.')]);
      rebuild(sec.legend, []);
      setText(sec.summary, '');
      return;
    }

    const W = Math.max(280, Math.floor(host.clientWidth || 640));
    const H = 290;
    const M = { l: 48, r: 58, t: 14, b: 30 };
    const plotW = W - M.l - M.r;
    const plotH = H - M.t - M.b;
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p.price); hi = Math.max(hi, p.price); }
    const first = pts[0].price;
    const lastP = pts[pts.length - 1];
    const minV = lo, maxV = hi;
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
    for (const tk of timeTicks(R.t0, R.t1, plotW)) {
      const x = X(tk.t);
      if (x < M.l + 18 || x > M.l + plotW - 18) continue;
      xl.appendChild(svgEl('text', { class: 'tick', x: x, y: H - 8, 'text-anchor': 'middle' }, tk.label));
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
    hit.addEventListener('pointermove', function (ev) {
      const rect = svg.getBoundingClientRect();
      const scale = rect.width ? W / rect.width : 1;
      const x = (ev.clientX - rect.left) * scale;
      const t = R.t0 + ((x - M.l) / plotW) * (R.t1 - R.t0);
      chart.pointer = true;
      showPoint(chart, nearest(t));
    });
    hit.addEventListener('pointerleave', function () {
      chart.pointer = false;
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
    setText(sec.summary, 'Last ' + RANGE_LABELS[S.drawer.range] + ': ' + fmtPrice(first) + ' ' + ARROW + ' ' + fmtPrice(lastP.price) +
      ' (' + fmtSigned(lastP.price - first) + '), low ' + fmtPrice(minV) + ', high ' + fmtPrice(maxV) + '.');
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
    const lines = [
      h('div', { class: 'tooltip-value' }, h('span', { class: 'tooltip-key' + (s ? ' surge' : ''), 'aria-hidden': 'true' }), fmtPrice(p.price),
        h('span', { class: 'tooltip-sub' }, fmtPctOf(p.price))),
      h('div', { class: 'tooltip-sub' }, fmtAbs(p.ts)),
    ];
    if (s) lines.push(h('div', { class: 'tooltip-sub' }, 'During a ' + str(s.window) + ' surge (' + fmtSigned(s.change) + ')'));
    tip.replaceChildren.apply(tip, lines);
    tip.hidden = false;
    const tw = tip.offsetWidth || 160;
    const th = tip.offsetHeight || 60;
    let left = x + 14;
    if (left + tw > chart.W - 4) left = x - tw - 14;
    left = Math.max(4, left);
    let top = y - th - 12;
    if (top < 0) top = Math.min(chart.H - th - 4, y + 14);
    tip.style.left = left.toFixed(0) + 'px';
    tip.style.top = top.toFixed(0) + 'px';
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
    if (focused) showPoint(chart, chart.index >= 0 ? chart.index : chart.pts.length - 1);
    else if (!chart.pointer) hidePoint(chart);
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
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Time'), h('th', { scope: 'col', class: 'num' }, 'Price'),
        h('th', { scope: 'col', class: 'num' }, 'Implied chance'), h('th', { scope: 'col' }, 'Source'), h('th', { scope: 'col' }, 'Surge'))),
      h('tbody', null, rows.map(function (p) {
        const s = surgeAt(p.ts, R.surges);
        return h('tr', null, h('td', { title: fmtAbs(p.ts) }, fmtShort(p.ts)), h('td', { class: 'num' }, fmtPrice(p.price)),
          h('td', { class: 'num' }, fmtPctOf(p.price)), h('td', null, p.source === 'candle' ? 'History candle' : p.source === 'tick' ? 'Live snapshot' : p.source || DASH),
          h('td', null, s ? str(s.window) + ' surge' : ''));
      })));
    rebuild(sec.tableBox, [table]);
    rebuild(sec.legend, []);
  }

  // ---------------------------------------------------------------- drawer: book, trades, surge

  function renderBook(d) {
    const sec = S.drawer.sections.book;
    const book = d.book ? obj(d.book) : null;
    const key = sig([book, d.book_error, d.book_fetched_at]);
    if (S.drawer.sigs.book === key) return;
    S.drawer.sigs.book = key;
    const head = h('div', { class: 'section-head' }, h('h3', { id: 'h-book' }, 'Order book (YES)'),
      num(d.book_fetched_at) != null ? h('span', { class: 'view-sub' }, 'Fetched ', timeEl(d.book_fetched_at, 'ago')) : null);
    if (!book) {
      rebuild(sec, [head, h('p', { class: 'muted-note' }, str(d.book_error) || 'No live order book is available for this outcome.')]);
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
      const bar = h('span', { class: 'bar', 'aria-hidden': 'true' });
      const frac = maxQ ? q / maxQ : 0;
      bar.style.width = 'calc(' + frac.toFixed(4) + ' * (100% - 4.5em))';
      const n = h('span', { class: 'qty-num' }, fmtInt(q));
      return h('td', { class: 'qty' }, h('div', { class: 'qty-cell ' + side }, side === 'bid' ? [n, bar] : [bar, n]));
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
    const key = sig([latest, latest ? S.analyze[latest.id] || null : null, surges.length, analysisEnabled(), newsEnabled()]);
    if (S.drawer.sigs.surge === key) return;
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
    return { view: VIEWS.indexOf(raw) !== -1 ? raw : S.view };
  }

  function showView(view) {
    const changed = view !== S.view || !S.viewShown;
    const wasShown = S.viewShown;
    S.view = view;
    S.viewShown = true;
    for (const sec of document.querySelectorAll('main > .view')) show(sec, sec.dataset.view === view);
    safe('nav', renderNav);
    const name = str(S.data.status && S.data.status.tournament && S.data.status.tournament.name) || 'Super Market dashboard';
    document.title = VIEW_TITLES[view] + ' · ' + name;
    if (changed) {
      if (wasShown) window.scrollTo(0, 0);
      safe('view', function () { renderView(view); });
      refreshTimes(document);
      poll();
    }
  }

  function route(fromHashChange) {
    const r = parseHash();
    if (r.exchange) {
      if (!S.viewShown) showView(S.view);
      openDrawer(r.exchange, !!fromHashChange && S.viewShown);
    } else {
      closeDrawerUI();
      showView(r.view);
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
    $('offline-retry').addEventListener('click', function () { poll(); });
    $('offline-dismiss').addEventListener('click', function () {
      S.offlineDismissed = true;
      show($('offline-banner'), false);
      $('main').focus({ preventScroll: true });
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
      if (S.drawer.id) requestCloseDrawer(); // closed some other way: keep the URL in sync
    });

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
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
