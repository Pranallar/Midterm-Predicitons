"""Local web dashboard: a standard-library HTTP server plus a single-page UI in ``web/``.

Run it with ``python -m supermarket_bot dashboard`` (add ``--demo`` to try it offline).
See docs/DESIGN.md (Web section).

* :class:`DashboardApp` turns the tracker's live view and the SQLite store into plain,
  JSON-safe dicts, one method per endpoint, so it can be tested without HTTP.
* :class:`DashboardServer` / :class:`DashboardHandler` serve those dicts as JSON and the
  static UI files, with local-only protections: Host-header allow-list (DNS rebinding; see
  :class:`DashboardServer`), JSON-only POSTs from an allowed Origin, a strict
  Content-Security-Policy and no secrets in any response.
* :func:`run_dashboard` wires everything together for the CLI.

Paper trading (docs/PAPER_TRADING.md §7.4): ``/api/paper`` (``?run=previous``), ``POST /api/paper/reset``,
``/api/backtest`` (run in one background thread on a separate read-only database connection) and
``/api/fairvalue``. Every Super Market client the builders create carries the GET-only guard
(:func:`readonly.enforce_get_only`).

Nothing here places orders.
"""

from __future__ import annotations

import dataclasses
import errno
import ipaddress
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from . import analytics, pipeline, strategy
from . import models as _models
from .books import Book, parse_time
from .models import SURGE_OPEN, HighBand, PricePoint, Surge
from .pipeline import context_summary  # noqa: F401  (moved to pipeline; re-exported under the old name, D58)
from .readonly import enforce_get_only

# Set by the tracker when a surge's market leaves the open-market list (closed or settled).
# Read with a default so this module also works with a models.py that predates the status.
SURGE_CLOSED: str = getattr(_models, "SURGE_CLOSED", "closed")

log = logging.getLogger("supermarket_bot")

WEB_DIR = Path(__file__).resolve().parent / "web"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
STATIC_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
JSON_TYPE = "application/json; charset=utf-8"
MAX_POST_BYTES = 16 * 1024

DAY_S = 86400.0
SERIES_WINDOW_S = 7 * DAY_S
MAX_SERIES_POINTS = 600
# The 7-day series is downsampled in tiers so the 6 h and 24 h chart ranges stay detailed:
# (span start before now, span end before now, max points). Totals 600.
SERIES_TIERS: Tuple[Tuple[float, float, int], ...] = (
    (SERIES_WINDOW_S, DAY_S, 150),
    (DAY_S, 6 * 3600.0, 150),
    (6 * 3600.0, 0.0, 300),
)
MAX_TRADES = 50
BOOK_DEPTH = 20
STRATEGY_TTL_S = 30.0
STRATEGY_ERROR_TTL_S = 5.0
BACKTEST_TTL_S = 600.0
BACKTEST_RETRY_S = 60.0
PAPER_BACKTEST_TTL_S = 1800.0  # the dashboard's replay backtest is re-run when its report is older than this
PAPER_BACKTEST_HOURS = 24.0
PAPER_RESET_MAX_CAPITAL = 10_000_000.0
PAPER_RESET_MAX_HOURS = 720.0
FAIRVALUE_TTL_S = 15.0  # /api/fairvalue is rebuilt at most this often
PAPER_OFF_ERROR = "The paper trader is off (started with --no-paper)."
NO_PREVIOUS_RUN = "No earlier simulation run has ended yet."
BOOK_TTL_S = 15.0  # a fetched order book (or its error) is reused for this long
BOOK_WAIT_S = 0.4  # a request waits at most this long for a background book fetch, then answers "pending"
BOOK_STALE_S = 120.0  # while a refresh runs, an expired book younger than this is still shown (marked stale)
MAX_BOOK_FETCHES = 4  # order-book reads in flight at once (one per exchange)
MAX_BOOKS_CACHED = 256
MAX_PROBLEM_TEXT = 300
MAX_SURGES = 100
DEFAULT_INITIAL_BALANCE = 100_000.0
DEFAULT_CUP_END_ISO = "2026-11-04T17:00:00Z"

_EXCHANGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SURGE_ID_RE = re.compile(r"^[0-9]{1,12}$")
# A syntactically plausible Host header (name, IPv4 or [IPv6], optional port), lower-cased.
_HOST_HEADER_RE = re.compile(r"^(?:[a-z0-9_](?:[a-z0-9._-]{0,252})|\[[0-9a-f:.%a-z]{2,64}\])(?::[0-9]{1,5})?$")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|passwd|authorization|bearer|cookie)")


# --------------------------------------------------------------------------- JSON helpers


def _finite(value: Any) -> Optional[float]:
    """A finite float, or None for missing / non-numeric / bool / NaN / inf values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _plain(value: Any) -> Any:
    """Dataclasses → dicts (via ``to_dict``), recursively; other values unchanged."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class _Scrubber:
    """Makes any value JSON-safe and strips secrets on the way.

    Keys that look like credentials (``api_key``, ``token``, ``secret`` …) are dropped and any
    known secret string (the API key) is masked wherever it appears in a value.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 4]

    def text(self, value: str) -> str:
        for secret in self.secrets:
            if secret in value:
                value = value.replace(secret, "***")
        return value

    def __call__(self, value: Any, depth: int = 0) -> Any:
        if depth > 40:
            return None
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            return self.text(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
        if isinstance(value, Mapping):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if _SENSITIVE_KEY_RE.search(name):
                    continue
                out[name] = self(item, depth + 1)
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self(item, depth + 1) for item in value]
        return self.text(str(value))


def encode_json(value: Any, secrets: Iterable[str] = ()) -> bytes:
    text = json.dumps(_Scrubber(secrets)(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # A lone surrogate (possible in API JSON) cannot be UTF-8 encoded; replace it, never 500.
    return text.encode("utf-8", errors="replace")


def _safe_url(url: Any) -> Optional[str]:
    """Only absolute http(s) URLs survive (no ``javascript:``, ``data:`` …)."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text or any(ch in text for ch in "\x00\r\n\t"):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return text


def _epoch(value: Any) -> Optional[float]:
    """Epoch seconds from a number or an ISO string (None otherwise)."""
    number = _finite(value)
    if number is not None:
        return number
    parsed = parse_time(value)
    return parsed.timestamp() if parsed is not None else None


def _pick(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _known(cls: Any, data: Mapping[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _downsample(points: Sequence[PricePoint], max_points: int) -> List[PricePoint]:
    try:
        return list(analytics.downsample(points, max_points))
    except Exception:  # analytics unavailable: keep first/last and evenly spaced points
        pts = list(points)
        if max_points <= 0:
            return []
        if len(pts) <= max_points:
            return pts
        if max_points == 1:
            return [pts[-1]]
        step = (len(pts) - 1) / (max_points - 1)
        return [pts[int(round(i * step))] for i in range(max_points)]


def _downsample_time(points: Sequence[PricePoint], max_points: int, start: float, end: float) -> List[PricePoint]:
    """Spread over time, so a burst of live ticks cannot crowd out the rest of a tier."""
    if len(points) <= max_points:
        return list(points)
    try:
        return list(analytics.downsample_by_time(points, max_points, start, end))
    except Exception:  # analytics unavailable or failing: fall back to index-based thinning
        return _downsample(points, max_points)


def tiered_series(points: Sequence[PricePoint], now: float) -> List[PricePoint]:
    """Downsample a 7-day series to at most :data:`MAX_SERIES_POINTS`, denser near ``now``."""
    pts = sorted((p for p in points if p is not None and _finite(p.price) is not None), key=lambda p: p.ts)
    if len(pts) <= MAX_SERIES_POINTS:
        return pts
    out: List[PricePoint] = []
    for start_before, end_before, budget in SERIES_TIERS:
        lo = now - start_before
        hi = now - end_before
        last_tier = end_before == 0.0
        chunk = [p for p in pts if lo <= p.ts < hi or (last_tier and p.ts >= hi)]
        out.extend(_downsample_time(chunk, budget, lo, max(hi, chunk[-1].ts) if chunk else hi))
    older = [p for p in pts if p.ts < now - SERIES_WINDOW_S]
    if older and len(out) < MAX_SERIES_POINTS:
        out.insert(0, older[-1])
    return out[:MAX_SERIES_POINTS] if len(out) > MAX_SERIES_POINTS else out


def _sparkline(raw: Any) -> List[List[float]]:
    out: List[List[float]] = []
    for item in raw or []:
        if isinstance(item, Mapping):
            ts, price = _finite(item.get("ts")), _finite(item.get("price"))
        elif isinstance(item, PricePoint):
            ts, price = _finite(item.ts), _finite(item.price)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            ts, price = _finite(item[0]), _finite(item[1])
        else:
            continue
        if ts is not None and price is not None:
            out.append([ts, price])
    return out


def _cup_end_ts(tournament: Optional[Mapping[str, Any]]) -> float:
    """``strategy.cup_end_ts`` when available, else the same rule implemented here."""
    try:
        value = strategy.cup_end_ts(tournament)
        number = _finite(value)
        if number is not None:
            return number
    except Exception:
        pass
    for candidate in ((tournament or {}).get("endDate"), os.environ.get("SUPERMARKET_CUP_END"), DEFAULT_CUP_END_ISO):
        ts = _epoch(candidate)
        if ts is not None:
            return ts
    return 0.0  # unreachable: the default always parses


def _clip(text: str, limit: int = MAX_PROBLEM_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def problems_summary(raw: Any) -> List[Dict[str, Any]]:
    """The tracker's CURRENT non-fatal failures, normalised; malformed entries are dropped.

    Each entry is ``{"source", "message", "since", "last", "count", "severity"}`` (severity
    ``"warning"`` or ``"error"``), errors first, then the most recent. Extra keys pass through.
    """
    out: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        entry = _plain(item)
        if not isinstance(entry, Mapping):
            continue
        source = _pick(entry, "source", "where")
        message = _pick(entry, "message", "error", "last_error")
        if not source and not message:
            continue
        count = _finite(entry.get("count"))
        severity = str(entry.get("severity") or "warning").strip().lower()
        problem = dict(entry)
        problem.update(
            {
                "source": str(source or "tracker"),
                "message": _clip(str(message or "")),
                "since": _epoch(entry.get("since")),
                "last": _epoch(entry.get("last")),
                "count": max(1, int(count)) if count is not None else 1,
                "severity": "error" if severity == "error" else "warning",
            }
        )
        out.append(problem)
    out.sort(key=lambda p: (p["severity"] != "error", -(p["last"] or p["since"] or 0.0)))
    return out


def detection_summary(raw: Any) -> Optional[Dict[str, Any]]:
    """The tracker's surge-detection state ``{"enabled", "waiting_for_history", "reason"}`` (None if not reported)."""
    data = _plain(raw)
    if not isinstance(data, Mapping):
        return None
    waiting = _finite(_pick(data, "waiting_for_history", "waiting"))
    reason = data.get("reason")
    out = dict(data)
    out.update(
        {
            "enabled": bool(data.get("enabled", True)),
            "waiting_for_history": int(waiting) if waiting is not None and waiting > 0 else 0,
            "reason": str(reason) if reason else None,
        }
    )
    return out


def tracker_summary(raw: Any) -> Dict[str, Any]:
    """Normalise the tracker's status dict into the fields the UI reads (raw keys are kept too)."""
    st = _plain(raw) if raw is not None else {}
    if not isinstance(st, Mapping):
        st = {}
    errors = _pick(st, "errors", "error_count", default=0)
    last_error = _pick(st, "last_error")
    if isinstance(errors, list):
        if last_error is None and errors:
            last_error = errors[-1]
        errors = len(errors)
    if isinstance(last_error, Mapping):
        last_error = last_error.get("message") or last_error.get("error") or json.dumps(last_error, default=str)
    backfill = _pick(st, "backfill", "backfill_progress", default={})
    if not isinstance(backfill, Mapping):
        backfill = {}
    done = _finite(_pick(backfill, "done", "completed", "finished"))
    total = _finite(_pick(backfill, "total", "planned"))
    if done is None:
        done = _finite(_pick(st, "backfill_done"))
    if total is None:
        total = _finite(_pick(st, "backfill_total"))
    pending = _finite(_pick(backfill, "pending", "remaining", "queued"))
    failed = _finite(_pick(backfill, "failed", "gave_up"))
    if total is None and done is not None and pending is not None:
        total = done + pending + (failed or 0.0)
    explicit = _pick(backfill, "complete", "finished_all")
    if explicit is not None:
        complete = bool(explicit)
    else:
        # A series that failed for good is finished too: nothing is left to load for it.
        complete = total is not None and done is not None and done + (failed or 0.0) >= total
    queues = _pick(st, "queues", "queue_lengths", default={})
    if not isinstance(queues, Mapping):
        queues = {}
    budget = _pick(st, "read_budget", "reads", "budget", default={})
    if not isinstance(budget, Mapping):
        budget = {}
    fatal = _pick(st, "fatal_error", "fatal")
    if isinstance(fatal, Mapping):
        fatal = fatal.get("message") or json.dumps(fatal, default=str)
    out = dict(st)
    out.update(
        {
            "running": bool(_pick(st, "running", "alive", default=False)),
            "cycles": int(_finite(_pick(st, "cycles", "cycle_count", "snapshots")) or 0),
            # Only a snapshot that succeeded counts: a cycle whose market list or prices failed is
            # not "Updated just now" (so no fallback to last_cycle_at).
            "last_snapshot_at": _epoch(_pick(st, "last_snapshot_at", "last_snapshot", "last_snapshot_ts")),
            "errors": int(_finite(errors) or 0),
            "last_error": str(last_error) if last_error else None,
            "fatal_error": str(fatal) if fatal else None,
            "backfill": {"done": done, "total": total, "failed": failed, "pending": pending, "complete": complete},
            "queues": dict(queues),
            "read_budget": dict(budget),
            "problems": problems_summary(st.get("problems")),
            "detection": detection_summary(st.get("detection")),
        }
    )
    return out


def _normalise_opportunity(opp: Dict[str, Any]) -> None:
    """Ids as strings; a multi-leg idea's ``legs`` as clean dicts (the UI lists them one by one)."""
    for key in ("exchange_id", "market_id"):
        if opp.get(key) is not None:
            opp[key] = str(opp[key])
    raw_legs = opp.get("legs")
    if raw_legs is None:  # a single-outcome idea
        return
    legs = []
    for raw in raw_legs if isinstance(raw_legs, (list, tuple)) else []:
        leg = _plain(raw)
        if not isinstance(leg, Mapping):
            continue
        item = dict(leg)
        for key in ("exchange_id", "market_id"):
            item[key] = str(leg[key]) if leg.get(key) is not None else None
        item["title"] = str(leg.get("title") or "")
        item["option"] = leg.get("option")
        side = str(leg.get("side") or "").strip().lower()
        item["side"] = side if side in ("yes", "no") else None
        item["price"] = _finite(leg.get("price"))
        legs.append(item)
    opp["legs"] = legs


def _limiter_pause(limiter: Any) -> float:
    """Seconds left on a rate limiter's 429 pause (0 when not paused or not knowable)."""
    if limiter is None:
        return 0.0
    until = _finite(getattr(limiter, "_paused_until", None))
    clock = getattr(limiter, "_clock", None)
    if until is None or not callable(clock):
        return 0.0
    try:
        now = _finite(clock())
    except Exception:
        return 0.0
    return max(0.0, until - now) if now is not None else 0.0


def _match_terms(text: str, terms: Sequence[str]) -> bool:
    return all(term in text for term in terms)


# --------------------------------------------------------------------------- the app


class DashboardApp:
    """The dashboard's data layer: one method per endpoint, each returning a plain dict.

    ``tracker`` needs ``view()`` (and ``request_analysis(surge_id)`` for re-analysis; ``paper_view`` /
    ``paper_reset`` for the Simulation view); ``store`` is a :class:`~supermarket_bot.store.TrackerStore`;
    ``client`` (optional) reads live order books; ``context`` is the trading
    :class:`~supermarket_bot.bot.Context`; ``fair_values`` (optional) the tracker's FairValueService;
    ``regime`` and ``sizing`` the settlement regime and sizing policy the Strategy view and the dashboard
    backtest assume.
    """

    def __init__(
        self,
        tracker: Any,
        store: Any,
        client: Any = None,
        context: Any = None,
        *,
        demo: bool = False,
        interval: float = 30.0,
        clock: Callable[[], float] = time.time,
        secrets: Iterable[str] = (),
        analysis_enabled: bool = True,
        news_enabled: bool = True,
        llm_enabled: bool = False,
        book_wait: float = BOOK_WAIT_S,
        fair_values: Any = None,
        regime: str = "unknown",
        sizing: str = "conservative",
        moves: Any = None,
    ) -> None:
        self.tracker = tracker
        self.fair_values = fair_values
        # docs/OUTSIDE_MOVES.md §18 (package "server"): the tracker's OutsideMoveWatcher (None: alerts are off)
        self.moves = moves
        self.regime = str(regime or "unknown")
        self.sizing = str(sizing or "conservative")
        self.store = store
        self.client = client
        self.context = context
        self.demo = bool(demo)
        self.interval = float(interval)
        self.clock = clock
        self.secrets = [s for s in secrets if s]
        self.analysis_enabled = analysis_enabled
        self.news_enabled = news_enabled
        self.llm_enabled = llm_enabled
        self.started_at = clock()
        self._lock = threading.Lock()
        self._strategy_build = threading.Lock()
        self._strategy_cache: Optional[Tuple[float, float, Dict[str, Any]]] = None  # (built_at, ttl, payload)
        self._backtest: Any = None
        self._backtest_at: Optional[float] = None
        self._backtest_error: Optional[str] = None
        self._backtest_thread: Optional[threading.Thread] = None
        # Order books: (fetched_at, book, error) per exchange, fetched in background threads.
        self.book_wait = max(0.0, float(book_wait))
        self._books: Dict[str, Tuple[float, Optional[Dict[str, Any]], Optional[str]]] = {}
        self._book_fetches: Dict[str, threading.Event] = {}  # exchange id -> set when its fetch ends
        self.view_error: Optional[str] = None
        self._last_view: Optional[Mapping[str, Any]] = None  # the last view() that worked
        self.view_updated_at: Optional[float] = None
        # The replay backtest (/api/backtest): one background thread at a time on a read-only connection.
        self._pbt_report: Any = None
        self._pbt_status: Optional[str] = None  # last finished run: "ready" | "error" | "unavailable" | "no_data"
        self._pbt_error: Optional[str] = None
        self._pbt_started_at: Optional[float] = None
        self._pbt_generated_at: Optional[float] = None
        self._pbt_finished_at: Optional[float] = None
        self._pbt_thread: Optional[threading.Thread] = None
        self._fv_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        # (strategy payload, the watcher's published summary it was annotated against, the annotated payload)
        self._strategy_annotated: Optional[Tuple[Dict[str, Any], Any, Dict[str, Any]]] = None

    # ------------------------------------------------------------------ inputs
    def _view(self) -> Dict[str, Any]:
        """The tracker's view; when ``view()`` raises, the last good one with a fresh ``status()``.

        The tracker keeps running when building its view fails, so the dashboard keeps showing
        the last rows, counts and context (``view_error`` says the data is not updating) rather
        than an empty "starting" state, and the tracker's live status stays current.
        """
        try:
            view = self.tracker.view()
        except Exception as exc:  # the tracker failing must not take the dashboard down
            log.debug("tracker.view() failed", exc_info=True)
            self.view_error = self._short(exc)
            with self._lock:
                last = self._last_view
            stale: Dict[str, Any] = dict(last) if last is not None else {}
            status = self._tracker_status()
            if status is not None:
                stale["status"] = status
            return stale
        self.view_error = None
        if not isinstance(view, Mapping):
            return {}
        with self._lock:
            self._last_view = view
            self.view_updated_at = self.clock()
        return dict(view)

    def _tracker_status(self) -> Optional[Mapping[str, Any]]:
        status_fn = getattr(self.tracker, "status", None)
        if not callable(status_fn):
            return None
        try:
            status = status_fn()
        except Exception:
            log.debug("tracker.status() failed", exc_info=True)
            return None
        return status if isinstance(status, Mapping) else None

    def _short(self, exc: BaseException) -> str:
        # Mask secrets BEFORE truncating, or a key cut at the boundary would leak its start.
        text = _Scrubber(self.secrets).text(str(exc) or type(exc).__name__)
        if len(text) > 300:
            text = text[:297] + "…"
        return text

    def _rows(self, view: Mapping[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for raw in view.get("exchanges") or []:
            row = _plain(raw)
            if not isinstance(row, Mapping):
                continue
            eid = _pick(row, "exchange_id", "id")
            if eid is None:
                continue
            out = dict(row)
            out["exchange_id"] = str(eid)
            if row.get("market_id") is not None:
                out["market_id"] = str(row["market_id"])
            out["title"] = str(_pick(row, "title", "market_title", default="") or "")
            out["option"] = row.get("option")
            for key in ("mark", "last", "bid", "ask", "spread", "change_5m", "change_1h", "change_24h"):
                out[key] = _finite(row.get(key))
            if out["spread"] is None and out["bid"] is not None and out["ask"] is not None:
                out["spread"] = round(out["ask"] - out["bid"], 6)
            out["sparkline"] = _sparkline(row.get("sparkline"))
            band = row.get("high_band")
            out["high_band"] = dict(band) if isinstance(band, Mapping) else None
            sid = row.get("surge_id")
            out["surge_id"] = int(sid) if isinstance(sid, (int, float)) and not isinstance(sid, bool) else None
            rows.append(out)
        return rows

    @staticmethod
    def _open_known(view: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bool:
        """Whether the view's rows are the open-market list (False before it ever loaded).

        An empty list counts only when the view really has one and the tracker has loaded the
        market list (every market closed); a view that could not be read at all says nothing.
        """
        if rows:
            return True
        status = view.get("status")
        return (
            isinstance(view.get("exchanges"), list)
            and isinstance(status, Mapping)
            and status.get("markets_updated_at") is not None
        )

    def _surge_dict(
        self, raw: Any, rows_by_id: Mapping[str, Mapping[str, Any]], open_known: bool = False
    ) -> Optional[Dict[str, Any]]:
        surge = _plain(raw)
        if not isinstance(surge, Mapping) or surge.get("exchange_id") is None:
            return None
        out = dict(surge)
        out["exchange_id"] = str(surge["exchange_id"])
        row = rows_by_id.get(out["exchange_id"])
        # A surge on a market that left the open list (closed or settled) is never "open": its
        # last price is frozen and nothing can be traded. The tracker marks these SURGE_CLOSED;
        # this also covers surges stored before it did.
        out["market_open"] = (row is not None) if open_known else None
        if open_known and row is None and out.get("status") == SURGE_OPEN:
            out["status"] = SURGE_CLOSED
        info = None
        if row is None or not out.get("title"):
            info = self._info(out["exchange_id"]) if row is None else None
        if not out.get("title"):
            out["title"] = (row or {}).get("title") or (info.market_title if info else "") or ""
        if out.get("option") is None:
            out["option"] = (row or {}).get("option") if row else (info.option if info else None)
        attribution = out.get("attribution")
        if isinstance(attribution, Mapping):
            att = dict(attribution)
            articles = []
            for article in att.get("articles") or []:
                if isinstance(article, Mapping):
                    item = dict(article)
                    item["url"] = _safe_url(article.get("url"))
                    articles.append(item)
            att["articles"] = articles
            att["reasons"] = [str(r) for r in att.get("reasons") or []]
            out["attribution"] = att
        else:
            out["attribution"] = None
        return out

    def _info(self, exchange_id: str) -> Any:
        try:
            return self.store.exchange(exchange_id)
        except Exception:
            log.debug("store.exchange(%s) failed", exchange_id, exc_info=True)
            return None

    def _surges(self, view: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        by_id = {r["exchange_id"]: r for r in rows}
        raw = view.get("surges")
        if raw is None:
            try:
                raw = self.store.surges(since=self.clock() - 2 * DAY_S, limit=MAX_SURGES)
            except Exception:
                raw = []
        open_known = self._open_known(view, rows)
        surges = [s for s in (self._surge_dict(item, by_id, open_known) for item in raw or []) if s is not None]
        surges.sort(key=lambda s: (_finite(s.get("detected_at")) or 0.0, _finite(s.get("id")) or 0.0), reverse=True)
        return surges[:MAX_SURGES]

    def _cup_end(self, ctx: Mapping[str, Any]) -> float:
        return _cup_end_ts(ctx.get("tournament") or None)

    def _tournament(self) -> Dict[str, Any]:
        ctx = self.context
        if ctx is None:
            return {"name": "Super Market", "slug": None, "currency": None, "status": None, "label": "public"}
        return {
            "name": getattr(ctx, "name", None),
            "slug": getattr(ctx, "slug", None),
            "currency": getattr(ctx, "currency", None),
            "status": getattr(ctx, "status", None),
            "label": getattr(ctx, "label", None),
        }

    def _paper_enabled(self) -> bool:
        """Whether the tracker runs a paper trader (it has ``paper_view`` and it answers a dict)."""
        view_fn = getattr(self.tracker, "paper_view", None)
        if not callable(view_fn):
            return False
        try:
            return isinstance(view_fn(), Mapping)
        except Exception:
            return False

    # ------------------------------------------------------------------ endpoints
    def health(self) -> Dict[str, Any]:
        return {"ok": True, "now": self.clock()}

    def status(self) -> Dict[str, Any]:
        now = self.clock()
        view = self._view()
        rows = self._rows(view)
        surges = self._surges(view, rows)
        ctx = context_summary(view.get("context"))
        tracker = tracker_summary(view.get("status"))
        view_error = self.view_error
        budget = tracker["read_budget"]
        limiter = getattr(self.client, "read_limiter", None)
        extras = {k: budget[k] for k in ("paper", "moves", "reserve", "projected_per_min", "warning") if k in budget}
        if limiter is not None and _finite(budget.get("used")) is None:
            try:
                budget = {"used": limiter.used, "limit": limiter.limit, "window_s": getattr(limiter, "window", 60.0)}
            except Exception:
                budget = {}
            budget.update(extras)  # the tracker's paper budget, reserve and projection (§10.5) pass through
            tracker["read_budget"] = budget
        # docs/OUTSIDE_MOVES.md §18.3: the outside-move watcher's Cup order-book budget ({"used", "limit", "room"} | null)
        moves_budget = _book_reads_dict(budget.get("moves"))
        if moves_budget is None and self.moves is not None:
            moves_budget = self._moves_book_reads()
        budget["moves"] = moves_budget
        moves_status = tracker.get("moves") if isinstance(tracker.get("moves"), Mapping) else self._moves_status()
        tracker["moves"] = moves_status
        problems = list(tracker["problems"])
        paused_for = _limiter_pause(limiter)
        if limiter is not None:
            budget["paused_for"] = round(paused_for, 1)
        if paused_for > 0 and not any(p["source"] == "rate limit" for p in problems):
            # A 429 holds every read until its Retry-After; the request is still waiting, so the
            # tracker has nothing to report yet.
            problems.append(
                {
                    "source": "rate limit",
                    "message": f"Rate limited by the API: reads are paused for {math.ceil(paused_for)} s.",
                    "since": now,
                    "last": now,
                    "count": 1,
                    "severity": "warning",
                }
            )
        tracker["problems"] = problems
        bands = view.get("high_band") or []
        open_participants = 0
        for s in surges:
            att = s.get("attribution") or {}
            if s.get("status") == SURGE_OPEN and att.get("verdict") == "participants":
                open_participants += 1
        last_hour = sum(1 for s in surges if (_finite(s.get("detected_at")) or 0.0) >= now - 3600.0)
        cup_end = self._cup_end(ctx)
        return {
            "ok": True,
            "demo": self.demo,
            "read_only": True,
            "now": now,
            "started_at": self.started_at,
            "interval": self.interval,
            "tournament": self._tournament(),
            "tracker": tracker,
            "view_error": view_error,
            "view_updated_at": self.view_updated_at,
            "problems": problems,
            "detection": tracker["detection"],
            "moves": moves_status,  # the watcher's compact status (§12.4: badge, notifications) | null
            "account": {
                "balance": ctx["balance"],
                "initial_balance": ctx["initial_balance"] if ctx["initial_balance"] is not None else DEFAULT_INITIAL_BALANCE,
                "my_rank": ctx["my_rank"],
                "leader_value": ctx["leader_value"],
                "leaders": ctx["leaders"],
                "updated_at": ctx["updated_at"],
            },
            "cup_end": cup_end,
            "days_left": max(0.0, (cup_end - now) / DAY_S),
            "features": {
                "analysis": self.analysis_enabled, "news": self.news_enabled, "llm": self.llm_enabled,
                "paper": self._paper_enabled(),
                "fair_value": (getattr(self.fair_values, "mode", None) or "off") if self.fair_values is not None else "off",
                "sizing": self.sizing, "regime": self.regime,
                "moves": self.moves is not None,  # docs/OUTSIDE_MOVES.md §18.3
            },
            "counts": {
                "outcomes": len(rows),
                "markets": len({r.get("market_id") for r in rows if r.get("market_id") is not None}),
                "surges": len(surges),
                "surges_last_hour": last_hour,
                "open_participant_surges": open_participants,
                "high_band": len(bands),
                "violations": int(_finite((ctx["constraints"] or {}).get("violationsCount")) or 0),
            },
        }

    def markets(self, q: Optional[str] = None) -> Dict[str, Any]:
        view = self._view()
        rows = self._rows(view)
        total = len(rows)
        terms = [t for t in (q or "").lower().split() if t]
        if terms:
            rows = [
                r
                for r in rows
                if _match_terms(" ".join(str(x) for x in (r.get("title"), r.get("option"), r.get("exchange_id"), r.get("market_id")) if x).lower(), terms)
            ]
        return {"now": self.clock(), "total": total, "count": len(rows), "q": q or "", "rows": rows}

    def surges(self) -> Dict[str, Any]:
        view = self._view()
        surges = self._surges(view, self._rows(view))
        return {"now": self.clock(), "count": len(surges), "surges": surges}

    def highband(self) -> Dict[str, Any]:
        now = self.clock()
        view = self._view()
        rows = {r["exchange_id"]: r for r in self._rows(view)}
        ctx = context_summary(view.get("context"))
        cup_end = self._cup_end(ctx)
        bands: List[Dict[str, Any]] = []
        for raw in view.get("high_band") or []:
            band = _plain(raw)
            if not isinstance(band, Mapping) or band.get("exchange_id") is None:
                continue
            out = dict(band)
            eid = str(band["exchange_id"])
            out["exchange_id"] = eid
            row = rows.get(eid) or {}
            info = self._info(eid) if not row else None
            out["title"] = row.get("title") or (info.market_title if info else "") or ""
            out["option"] = row.get("option") if row else (info.option if info else None)
            settlement = band.get("settlement_date") or row.get("settlement_date") or (info.settlement_date if info else None)
            out["settlement_date"] = settlement
            settle_ts = _epoch(settlement)
            out["settlement_ts"] = settle_ts
            out["settles_before_cup_end"] = None if settle_ts is None else settle_ts <= cup_end
            side = str(band.get("side") or "YES").upper()
            fav = _finite(band.get("favorite_price"))
            bid, ask = row.get("bid"), row.get("ask")
            entry: Optional[float]
            if side == "NO":
                entry = round(1.0 - bid, 6) if bid is not None else None
            else:
                entry = ask
            out["entry_is_estimate"] = entry is None
            if entry is None:
                entry = fav
            out["entry_price"] = entry
            if entry is not None and 0 < entry < 1:
                out["payout_per_share"] = round(1.0 - entry, 6)
                out["return_pct"] = round((1.0 - entry) / entry * 100.0, 4)
            else:
                out["payout_per_share"] = None
                out["return_pct"] = None
            bands.append(out)
        bands.sort(key=lambda b: (-(_finite(b.get("favorite_price")) or 0.0), b.get("title") or ""))
        return {"now": now, "cup_end": cup_end, "count": len(bands), "bands": bands}

    def exchange(self, exchange_id: str) -> Optional[Dict[str, Any]]:
        """Detail for one outcome, or None when it is unknown (the server answers 404)."""
        eid = str(exchange_id)
        if not _EXCHANGE_ID_RE.match(eid):
            return None
        now = self.clock()
        view = self._view()
        rows = self._rows(view)
        open_known = self._open_known(view, rows)
        row = next((r for r in rows if r["exchange_id"] == eid), None)
        info = self._info(eid)
        if row is None and info is None:
            return None
        market_open = (row is not None) if open_known else None
        if row is None:
            row = {
                "exchange_id": eid,
                "market_id": info.market_id,
                "title": info.market_title,
                "option": info.option,
                "settlement_date": info.settlement_date,
            }
        try:
            points = self.store.series(eid, now - SERIES_WINDOW_S)
        except Exception as exc:
            log.warning("series for %s failed: %s", eid, exc)
            points = []
        series = [[p.ts, p.price, p.source] for p in tiered_series(points, now)]
        by_id = {r["exchange_id"]: r for r in rows}
        try:
            stored = self.store.surges(since=now - SERIES_WINDOW_S, exchange_id=eid, limit=50)
        except Exception:
            stored = []
        surges = [s for s in (self._surge_dict(item, by_id, open_known) for item in stored) if s is not None]
        live = {s.get("id"): s for s in self._surges(view, rows) if s.get("exchange_id") == eid}
        surges = [live.get(s.get("id"), s) for s in surges]
        for sid, s in live.items():
            if sid not in {x.get("id") for x in surges}:
                surges.append(s)
        surges.sort(key=lambda s: _finite(s.get("detected_at")) or 0.0, reverse=True)
        try:
            records = self.store.trades(eid, now - SERIES_WINDOW_S)
        except Exception:
            records = []
        trades = [
            {"id": t.trade_id, "ts": t.ts, "price": t.price, "size": t.size, "side": t.side}
            for t in reversed(records[-MAX_TRADES:])
        ]
        band = row.get("high_band")
        out = {
            "now": now,
            "exchange": row,
            "market_open": market_open,
            "series": series,
            "series_window_s": SERIES_WINDOW_S,
            "surges": surges,
            "trades": trades,
            "high_band": band,
        }
        out.update(self._book(eid))
        return out

    def _book(self, eid: str) -> Dict[str, Any]:
        """The order-book fields of the exchange detail; never waits on the network for long.

        The series, trades and surges come from the local store, so the detail answers at once.
        A cached book (or its error) is reused for :data:`BOOK_TTL_S`. Otherwise one background
        fetch per exchange starts (requests for the same book share it) and this waits at most
        ``book_wait`` seconds for it; if it is still running the answer has ``book_pending``
        True and either ``book`` None or the previous book marked ``book_stale``, and the next
        poll picks the new book up. A slow read (rate-limit wait, Retry-After, timeouts) can
        therefore never hold the whole drawer back.
        """
        if self.client is None:
            return {
                "book": None,
                "book_error": "No live connection to the market.",
                "book_fetched_at": None,
                "book_pending": False,
                "book_stale": False,
            }
        now = self.clock()
        start = False
        with self._lock:
            cached = self._books.get(eid)
            if cached is not None and now - cached[0] < BOOK_TTL_S:
                return self._book_fields(cached, pending=False)
            done = self._book_fetches.get(eid)
            if done is None and len(self._book_fetches) < MAX_BOOK_FETCHES:
                done = threading.Event()
                self._book_fetches[eid] = done
                start = True
        if start:
            assert done is not None
            thread = threading.Thread(target=self._fetch_book, args=(eid, done), name=f"dashboard-book-{eid}", daemon=True)
            try:
                thread.start()
            except RuntimeError:  # cannot start a thread (interpreter shutting down, thread limit)
                with self._lock:
                    self._book_fetches.pop(eid, None)
                done.set()
        if done is not None and self.book_wait > 0 and done.wait(self.book_wait):
            with self._lock:
                fresh = self._books.get(eid)
            if fresh is not None and fresh is not cached:
                return self._book_fields(fresh, pending=False)
        if cached is not None and cached[1] is not None and now - cached[0] < BOOK_STALE_S:
            fields = self._book_fields(cached, pending=True)
            fields["book_stale"] = True
            return fields
        return {
            "book": None,
            "book_error": "Loading the order book…",
            "book_fetched_at": None,
            "book_pending": True,
            "book_stale": False,
        }

    @staticmethod
    def _book_fields(entry: Tuple[float, Optional[Dict[str, Any]], Optional[str]], pending: bool) -> Dict[str, Any]:
        fetched_at, book, error = entry
        return {"book": book, "book_error": error, "book_fetched_at": fetched_at, "book_pending": pending, "book_stale": False}

    def _fetch_book(self, eid: str, done: threading.Event) -> None:
        """Background thread: read one order book into the cache, then wake the waiting requests."""
        result: Optional[Tuple[float, Optional[Dict[str, Any]], Optional[str]]] = None
        try:
            tournament_id = getattr(self.context, "tournament_id", None)
            try:
                payload = self.client.get_exchange_orderbook(eid, depth=BOOK_DEPTH, tournament_id=tournament_id)
                result = (self.clock(), self._book_dict(payload), None)
            except Exception as exc:
                log.info("order book for %s failed: %s", eid, exc)
                result = (self.clock(), None, self._short(exc))
        finally:
            with self._lock:
                if result is not None:
                    self._books[eid] = result
                    if len(self._books) > MAX_BOOKS_CACHED:  # keep the cache small
                        oldest = sorted(self._books.items(), key=lambda kv: kv[1][0])[: len(self._books) - MAX_BOOKS_CACHED]
                        for key, _ in oldest:
                            self._books.pop(key, None)
                self._book_fetches.pop(eid, None)
            done.set()

    def wait_books(self, timeout: float = 10.0) -> bool:
        """Wait for the background order-book fetches in flight (tests, shutdown). True if all ended."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                pending = list(self._book_fetches.values())
            if not pending:
                return True
            for event in pending:
                if not event.wait(max(0.0, deadline - time.monotonic())):
                    return False

    @staticmethod
    def _book_dict(payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, Mapping):
            return None
        book = Book.from_payload(payload)
        bids = [{"price": lv.price, "quantity": lv.quantity} for lv in book.bids[:BOOK_DEPTH]]
        asks = [{"price": lv.price, "quantity": lv.quantity} for lv in book.asks[:BOOK_DEPTH]]
        best_bid = bids[0]["price"] if bids else _finite(payload.get("bestBid"))
        best_ask = asks[0]["price"] if asks else _finite(payload.get("bestAsk"))
        spread = round(best_ask - best_bid, 6) if best_bid is not None and best_ask is not None else _finite(payload.get("spread"))
        mid = round((best_bid + best_ask) / 2, 6) if best_bid is not None and best_ask is not None else None
        return {
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid": mid,
            "sequence": book.as_of.sequence if book.as_of is not None else None,
        }

    # ------------------------------------------------------------------ strategy
    def strategy(self) -> Dict[str, Any]:
        now = self.clock()
        # (the outside-move annotation takes self._lock itself: it always runs after the lock is released)
        with self._lock:
            cached = self._strategy_cache
        if cached is not None and now - cached[0] < cached[1]:
            return self._annotate_strategy(cached[2])
        with self._strategy_build:
            now = self.clock()
            with self._lock:
                cached = self._strategy_cache
            if cached is not None and now - cached[0] < cached[1]:
                return self._annotate_strategy(cached[2])
            payload, ttl = self._build_strategy(now)
            with self._lock:
                self._strategy_cache = (now, ttl, payload)
        return self._annotate_strategy(payload)

    def _backtest_state(self) -> Tuple[Any, str, Optional[str]]:
        with self._lock:
            result, error = self._backtest, self._backtest_error
            running = self._backtest_thread is not None and self._backtest_thread.is_alive()
        if result is not None:
            return result, "ready", None
        if running or (self._backtest_at is None and error is None):
            return None, "pending", None
        return None, "error", error

    def _build_strategy(self, now: float) -> Tuple[Dict[str, Any], float]:
        self.ensure_backtest()
        view = self._view()
        ctx = context_summary(view.get("context"))
        cup_end = self._cup_end(ctx)
        backtest, backtest_status, backtest_error = self._backtest_state()
        base: Dict[str, Any] = {
            "now": now,
            "backtest_status": backtest_status,
            "backtest_error": backtest_error,
            "cup_end": cup_end,
        }
        try:
            # Only outcomes on the current open-market list: a closed or settled market keeps its last stored
            # price and its "open" surges, and must never yield a trade idea (pipeline.assemble_inputs).
            recent = getattr(self.tracker, "recent_mids", None)
            try:
                mids = recent(pipeline.RECENT_MIDS_S) if callable(recent) else None
            except Exception:
                mids = None
            inputs = pipeline.assemble_inputs(
                now=now, view=view, store=self.store, fair_values=self.fair_values, backtest=backtest,
                regime=self.regime, cup_end=cup_end, recent_mids=mids if isinstance(mids, Mapping) else None,
            )
            report = strategy.report_from_inputs(inputs, sizing=self.sizing, fair_value_status=self._fv_status_compact())
        except NotImplementedError:
            base.update({"available": False, "error": "The strategy engine is not available yet."})
            return base, STRATEGY_ERROR_TTL_S
        except Exception as exc:
            log.warning("strategy report failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            base.update({"available": False, "error": f"Could not build the strategy report: {self._short(exc)}"})
            return base, STRATEGY_ERROR_TTL_S
        data = _plain(report)
        if not isinstance(data, dict):
            data = {}
        data.update(base)
        data["available"] = True
        if data.get("backtest") is None and backtest is not None:
            data["backtest"] = _plain(backtest)
        for opp in data.get("opportunities") or []:
            if isinstance(opp, dict):
                _normalise_opportunity(opp)
        if "assumptions" in data:
            raw = data.get("assumptions")
            data["assumptions"] = [str(a) for a in raw if a] if isinstance(raw, (list, tuple)) else []
        return data, STRATEGY_TTL_S

    def _fv_status_compact(self) -> Optional[Dict[str, Any]]:
        """``{"mode", "usable", "total", "providers"}`` for the strategy report (None without fair values)."""
        if self.fair_values is None:
            return None
        try:
            st = self.fair_values.status()
        except Exception as exc:
            log.debug("fair value status failed: %s", exc)
            return None
        return {"mode": st.get("mode"), "usable": st.get("usable"), "total": st.get("total"),
                "providers": st.get("providers") or {}}

    def ensure_backtest(self) -> Optional[threading.Thread]:
        """Start the fade backtest in the background when it is missing or older than 10 minutes."""
        now = self.clock()
        with self._lock:
            if self._backtest_thread is not None and self._backtest_thread.is_alive():
                return self._backtest_thread
            if self._backtest_at is not None:
                ttl = BACKTEST_TTL_S if self._backtest is not None else BACKTEST_RETRY_S
                if now - self._backtest_at < ttl:
                    return None
            thread = threading.Thread(target=self._run_backtest, name="dashboard-backtest", daemon=True)
            self._backtest_thread = thread
        thread.start()
        return thread

    def _run_backtest(self) -> None:
        started = self.clock()
        result: Any = None
        error: Optional[str] = None
        try:
            infos = self.store.exchanges()
            series = {e.exchange_id: self.store.series(e.exchange_id, started - SERIES_WINDOW_S) for e in infos}
            market_of = {e.exchange_id: e.market_id for e in infos}
            # Stored surges with their attribution let the result measure the participant-only reversion
            # rate that caps a fade's win chance (§5.5); without them it stays unmeasured.
            try:
                surges = self.store.surges(since=started - SERIES_WINDOW_S, limit=100_000)
            except Exception as exc:
                log.debug("stored surges for the fade backtest failed: %s", exc)
                surges = None
            result = strategy.backtest_fade(series, market_of=market_of, surges=surges)
        except NotImplementedError:
            error = "The backtest is not available yet."
        except Exception as exc:
            log.warning("fade backtest failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            error = self._short(exc)
        with self._lock:
            if result is not None:
                self._backtest = result
                self._strategy_cache = None  # the next strategy request picks the result up
            self._backtest_error = error
            self._backtest_at = started

    def wait_backtest(self, timeout: float = 10.0) -> None:
        thread = self._backtest_thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------------ actions
    def _tracker_stopped(self) -> Optional[str]:
        """Why queued analyses would never run (the tracker stopped), or None while it can still run them.

        A fatal error (a rejected key) stops every worker. A tracker that was started and is no
        longer running has stopped too; one that was never started (driven by hand, as in the
        tests) is not judged.
        """
        status = self._tracker_status()
        if status is None:
            return None
        fatal = status.get("fatal_error") or status.get("fatal")
        if isinstance(fatal, Mapping):
            fatal = fatal.get("message") or fatal.get("error")
        if fatal:
            reason = self._short(RuntimeError(str(fatal)))
            if len(reason) > 200:
                reason = reason[:197] + "…"
            return f"The tracker has stopped ({reason}). Restart the dashboard to analyse surges."
        if status.get("started_at") is not None and status.get("running") is False:
            return "The tracker has stopped, so nothing would analyse the surge. Restart the dashboard to analyse surges."
        return None

    def analyze(self, surge_id: Any) -> Tuple[int, Dict[str, Any]]:
        """Re-queue a surge for attribution. Returns (HTTP status, body)."""
        text = str(surge_id)
        if not _SURGE_ID_RE.match(text):
            return 404, {"error": "Unknown surge."}
        sid = int(text)
        try:
            exists = self.store.get_surge(sid) is not None
        except Exception:
            exists = False
        if not exists:
            return 404, {"error": f"Surge {sid} was not found."}
        if not self.analysis_enabled:
            return 409, {"queued": False, "error": "Surge analysis is turned off for this dashboard."}
        request = getattr(self.tracker, "request_analysis", None)
        if request is None:
            return 409, {"queued": False, "error": "This tracker cannot re-analyze surges."}
        stopped = self._tracker_stopped()
        if stopped is not None:
            return 409, {"queued": False, "surge_id": sid, "error": stopped}
        try:
            queued = bool(request(sid))
        except Exception as exc:
            log.warning("request_analysis(%s) failed: %s", sid, exc)
            return 500, {"queued": False, "error": f"Could not queue the analysis: {self._short(exc)}"}
        message = "Queued for analysis." if queued else "Already queued; the analysis will update shortly."
        return 200, {"queued": queued, "surge_id": sid, "message": message}

    # ------------------------------------------------------------------ simulation (docs/PAPER_TRADING.md §10)
    def _paper_disabled(self, now: float, error: str = PAPER_OFF_ERROR, enabled: bool = False) -> Dict[str, Any]:
        return {
            "now": now, "enabled": enabled, "available": False, "error": error, "demo": self.demo, "run": None,
            "has_previous": False, "headline": None, "table_warning": "", "model_label": "", "portfolios": [],
            "equity": {}, "positions": [], "baskets": [], "orders": [], "fills": [], "trades": [], "signals": None,
            "study": None, "budget": None, "fair_value": None, "caveats": [], "last_step": None,
        }

    @staticmethod
    def _paper_defaults(body: Dict[str, Any]) -> Dict[str, Any]:
        """Every §10.1 key present (the runner fills them; this only guards against a partial body)."""
        for key, default in (
            ("run", None), ("has_previous", False), ("headline", None), ("table_warning", ""), ("model_label", ""),
            ("portfolios", []), ("equity", {}), ("positions", []), ("baskets", []), ("orders", []), ("fills", []),
            ("trades", []), ("signals", None), ("study", None), ("budget", None), ("fair_value", None),
            ("caveats", []), ("last_step", None),
        ):
            if key not in body:
                body[key] = copy.deepcopy(default)
        return body

    def paper(self, run: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """``GET /api/paper`` (§10.1): the paper runner's published summary. ``run="previous"``: the newest
        ENDED run's final snapshot in the same shape, or None (the server answers 404)."""
        now = self.clock()
        view_fn = getattr(self.tracker, "paper_view", None)
        if not callable(view_fn):
            return None if run == "previous" else self._paper_disabled(now)
        try:
            body = view_fn("previous") if run == "previous" else view_fn()
        except NotImplementedError:
            return None if run == "previous" else self._paper_disabled(
                now, "The paper trader is not available in this build.", enabled=True)
        except Exception as exc:
            log.warning("paper summary failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            return None if run == "previous" else self._paper_disabled(
                now, f"The paper trader failed: {self._short(exc)}", enabled=True)
        if run == "previous":
            return self._previous_body(body, now) if isinstance(body, Mapping) else None
        if not isinstance(body, Mapping):
            reason = getattr(self.tracker, "paper_error", None)
            if isinstance(reason, str) and reason:
                return self._paper_disabled(now, reason, enabled=True)
            return self._paper_disabled(now)
        out = self._paper_defaults(dict(body))
        out["now"] = now
        out["enabled"] = True
        out.setdefault("demo", self.demo)
        out["available"] = True
        out["error"] = None
        return out

    def _previous_body(self, raw: Mapping[str, Any], now: float) -> Dict[str, Any]:
        """``?run=previous``: the runner's body for the ended run, or a bare final snapshot wrapped in §10.1."""
        body = dict(raw)
        if "portfolios" not in body and "verdicts" in body:  # a bare final snapshot {"at", "reason", ...}
            final = dict(body)
            body = {"run": {"run_id": final.get("run_id"), "final": final, "ended_at": final.get("at"),
                            "end_reason": final.get("reason")},
                    "portfolios": list(final.get("portfolios") or []), "study": final.get("study")}
        out = self._paper_defaults(body)
        run = out.get("run")
        if isinstance(run, Mapping):
            run = dict(run)
            final = run.get("final") if isinstance(run.get("final"), Mapping) else None
            if final is not None:
                run.setdefault("ended_at", final.get("at"))
                run.setdefault("end_reason", final.get("reason"))
            out["run"] = run
        out["now"] = now
        out["enabled"] = True
        out.setdefault("demo", self.demo)
        out["available"] = True
        out["error"] = None
        return out

    @staticmethod
    def _bounded_number(data: Mapping[str, Any], key: str, low: float, high: float) -> Tuple[bool, Optional[float]]:
        if key not in data or data[key] is None:
            return True, None
        value = _finite(data[key])
        if value is None or not (low <= value <= high):
            return False, None
        return True, value

    def paper_reset(self, data: Optional[Mapping[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
        """``POST /api/paper/reset`` (§10.2). Returns (HTTP status, body)."""
        data = data if isinstance(data, Mapping) else {}
        if data.get("confirm") is not True:
            return 400, {"reset": False, "error": 'Send {"confirm": true} to reset the simulation.'}
        ok, capital = self._bounded_number(data, "start_capital", 1.0, PAPER_RESET_MAX_CAPITAL)
        if not ok:
            return 400, {"reset": False, "error": "start_capital must be a number between 1 and 10,000,000."}
        ok, hours = self._bounded_number(data, "target_hours", 1.0, PAPER_RESET_MAX_HOURS)
        if not ok:
            return 400, {"reset": False, "error": "target_hours must be a number between 1 and 720."}
        reset_fn = getattr(self.tracker, "paper_reset", None)
        view_fn = getattr(self.tracker, "paper_view", None)
        if not callable(reset_fn) or not callable(view_fn):
            return 409, {"reset": False, "error": PAPER_OFF_ERROR}
        try:
            before = view_fn()
        except Exception:
            before = None
        if not isinstance(before, Mapping):
            reason = getattr(self.tracker, "paper_error", None)
            return 409, {"reset": False, "error": reason if isinstance(reason, str) and reason else PAPER_OFF_ERROR}
        previous = (before.get("run") or {}).get("run_id") if isinstance(before.get("run"), Mapping) else None
        try:
            run_id = reset_fn(start_capital=capital, target_hours=hours)
        except Exception as exc:
            log.warning("paper reset failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            return 500, {"reset": False, "error": f"Could not reset the simulation: {self._short(exc)}"}
        if not run_id:
            return 409, {"reset": False, "error": PAPER_OFF_ERROR}
        try:
            after = view_fn() or {}
        except Exception:
            after = {}
        run = after.get("run") if isinstance(after.get("run"), Mapping) else {}
        shown_hours = _finite(run.get("target_hours")) or hours or 24.0
        shown_capital = _finite(run.get("start_capital")) or capital or DEFAULT_INITIAL_BALANCE
        message = (f"Started a new {shown_hours:g}-hour simulation with {shown_capital:,.0f} SUSQies per portfolio. "
                   "The previous run's result is kept under Previous run.")
        return 200, {"reset": True, "run_id": str(run_id), "previous_run_id": previous, "message": message}

    def backtest(self) -> Dict[str, Any]:
        """``GET /api/backtest`` (§10.3): the replay backtest's status and newest report. The first request
        (and one after the report is 30 minutes old) starts it in a background thread; this never waits."""
        now = self.clock()
        self.ensure_paper_backtest()
        with self._lock:
            running = self._pbt_thread is not None and self._pbt_thread.is_alive()
            report, status, error = self._pbt_report, self._pbt_status, self._pbt_error
            started, generated = self._pbt_started_at, self._pbt_generated_at
        if running and status != "no_data":
            status, error = "pending", None
        elif status is None:
            status = "pending"
        # (a periodic re-check of a store with too little data keeps saying so: it takes milliseconds, and
        # "Replaying the stored history" would be wrong)
        return {"now": now, "status": status, "error": error, "started_at": started, "generated_at": generated,
                "report": _plain(report) if report is not None else None}

    def ensure_paper_backtest(self) -> Optional[threading.Thread]:
        """Start the replay backtest when it never ran, or its result is due (30 min; 1 min after a failure)."""
        now = self.clock()
        with self._lock:
            if self._pbt_thread is not None and self._pbt_thread.is_alive():
                return self._pbt_thread
            if self._pbt_finished_at is not None:
                ttl = PAPER_BACKTEST_TTL_S if self._pbt_status == "ready" else BACKTEST_RETRY_S
                if now - self._pbt_finished_at < ttl:
                    return None
            thread = threading.Thread(target=self._run_paper_backtest, name="dashboard-replay-backtest", daemon=True)
            self._pbt_thread = thread
            self._pbt_started_at = now
        thread.start()
        return thread

    def _live_run_started_at(self) -> Optional[float]:
        view_fn = getattr(self.tracker, "paper_view", None)
        if not callable(view_fn):
            return None
        try:
            body = view_fn()
        except Exception:
            return None
        run = body.get("run") if isinstance(body, Mapping) else None
        return _finite(run.get("started_at")) if isinstance(run, Mapping) else None

    def _run_paper_backtest(self) -> None:
        """Background thread: one replay over the last 24 h on a SEPARATE read-only connection (D52)."""
        report: Any = None
        status, error = "error", None
        try:
            from .backtest import DASHBOARD_TIME_BUDGET_S, run_backtest
            from .models import BacktestConfig, PaperConfig, default_portfolios
            from .store import TrackerStore

            path = str(getattr(self.store, "path", "") or "")
            if not path or path == ":memory:":
                status, error = "unavailable", "The backtest needs the tracker's database file."
            else:
                ro = TrackerStore.open_read_only(path)
                try:
                    bounds = ro.tick_bounds()
                    if bounds is None or bounds[1] - bounds[0] < 3600.0:
                        status, error = "no_data", "Less than 1 hour of prices is stored yet: nothing to replay."
                    else:
                        config = BacktestConfig(
                            hours=PAPER_BACKTEST_HOURS,
                            paper=PaperConfig(regime=self.regime, sizing=self.sizing,
                                              portfolios=default_portfolios(self.sizing), demo=self.demo),
                            time_budget_s=DASHBOARD_TIME_BUDGET_S,
                            live_run_started_at=self._live_run_started_at(),
                        )
                        report = run_backtest(ro, config, progress=lambda done, total: time.sleep(0))
                        # lookahead-1: every stored paper run counts (one that ended -- completed, reset,
                        # "settings changed" -- saw this data as much as the current one)
                        pipeline.apply_paper_overlap(report, pipeline.paper_run_intervals(ro, now=self.clock()))
                        status = "ready"
                finally:
                    ro.close()
        except NotImplementedError:
            status, error = "unavailable", "The backtest is not available yet."
        except Exception as exc:
            log.warning("replay backtest failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            status, error = "error", f"The backtest failed: {self._short(exc)}"
        finished = self.clock()
        with self._lock:
            if report is not None:
                self._pbt_report = report
                self._pbt_generated_at = finished
            self._pbt_status = status
            self._pbt_error = error
            self._pbt_finished_at = finished

    def wait_paper_backtest(self, timeout: float = 30.0) -> None:
        thread = self._pbt_thread
        if thread is not None:
            thread.join(timeout)

    def fairvalue(self) -> Dict[str, Any]:
        """``GET /api/fairvalue`` (§10.4): matches, fair values, Cup quotes and fix-a-match snippets per outcome."""
        now = self.clock()
        with self._lock:
            cached = self._fv_cache
        if cached is not None and now - cached[0] < FAIRVALUE_TTL_S and now >= cached[0]:
            out = dict(cached[1])
            out["now"] = now
            return self._annotate_fairvalue(out)
        payload = self._build_fairvalue(now)
        with self._lock:
            self._fv_cache = (now, payload)
        return self._annotate_fairvalue(payload)

    def outside_moves(self, state: Optional[str] = None, since: Optional[float] = None,
                      limit: Optional[int] = None) -> Dict[str, Any]:
        """``GET /api/moves`` (docs/OUTSIDE_MOVES.md §18.2): the watcher's published summary plus ``now``, ``enabled``,
        ``available``, ``error``, ``demo`` and ``book_reads``, filtered by ``state`` ("open" | "closed" | "all"),
        ``since`` (detected_at >= since) and ``limit`` (1..500, default 100). The disabled body when there is no
        watcher. Query validation (400) happens in the handler.

        Lock rule (§12.3): the published summary and status are read lock-free; only a filtered request asks the
        watcher for its in-memory alert list (``alerts()``, which takes the watcher's leaf lock briefly)."""
        from .moves import MOVES_MAX_ALERTS_API

        now = self.clock()
        watcher = self.moves
        if watcher is None:
            return moves_payload(None, now, enabled=False, demo=self.demo)
        demo = self.demo or bool(getattr(watcher, "demo", False))
        error: Optional[str] = None
        summary: Optional[Mapping[str, Any]] = None
        try:
            raw = watcher.summary()
            summary = raw if isinstance(raw, Mapping) else None
        except Exception as exc:  # a broken watcher never takes the endpoint down; it says so
            log.warning("outside-move summary failed: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            error = f"The outside-move alerts could not be read ({type(exc).__name__}): see the dashboard's log."
        status = self._moves_status()
        if error is None and status is not None and status.get("last_error"):
            error = str(status["last_error"])
        if summary is None and error is None:
            error = "The outside-move alerts could not be read: see the dashboard's log."
        alerts: Optional[List[Mapping[str, Any]]] = None
        if summary is not None and (state in ("open", "closed") or since is not None
                                    or (limit is not None and limit != MOVES_MAX_ALERTS_API)):
            alerts = self._moves_alerts(watcher, summary, state, since, limit or MOVES_MAX_ALERTS_API)
        return moves_payload(summary, now, enabled=True, demo=demo, error=error, book_reads=self._moves_book_reads(),
                             alerts=alerts)

    @staticmethod
    def _moves_alerts(watcher: Any, summary: Mapping[str, Any], state: Optional[str], since: Optional[float],
                      limit: int) -> List[Mapping[str, Any]]:
        """The alerts of a filtered /api/moves request, in the summary's order (open first: actionable, then other
        open, newest first; then closed newest first). Every alert in the watcher's memory (30 days) when it can list
        them, else the published summary's (at most 100). Filters on each alert dict's own ``state`` so the list
        always agrees with the alerts it shows."""
        listed: Any = None
        fn = getattr(watcher, "alerts", None)
        if callable(fn):
            try:
                listed = fn(None, since, None)
            except Exception as exc:
                log.debug("outside-move alerts() failed: %s", exc)
                listed = None
        if not isinstance(listed, (list, tuple)):
            listed = summary.get("alerts") or []
        out: List[Mapping[str, Any]] = []
        for alert in listed:
            if not isinstance(alert, Mapping):
                continue
            if state in ("open", "closed") and alert.get("state") != state:
                continue
            detected = _finite(alert.get("detected_at"))
            if since is not None and (detected is None or detected < since):
                continue
            out.append(alert)
            if len(out) >= limit:
                break
        return out

    def _moves_status(self) -> Optional[Dict[str, Any]]:
        """The watcher's compact published status (§12.4; lock-free), or None."""
        if self.moves is None:
            return None
        try:
            raw = self.moves.status()
        except Exception as exc:
            log.debug("outside-move status failed: %s", exc)
            return None
        return dict(raw) if isinstance(raw, Mapping) else None

    def _moves_book_reads(self) -> Optional[Dict[str, int]]:
        """``tracker.read_budget.moves`` ({"used", "limit", "room"}) without taking the tracker's lock when the tracker
        exposes its MovesBudget; else from ``tracker.status()``; None without a watcher's budget."""
        budget = getattr(self.tracker, "moves_budget", None)
        status_fn = getattr(budget, "status", None)
        if callable(status_fn):
            try:
                return _book_reads_dict(status_fn())
            except Exception as exc:
                log.debug("outside-move book budget failed: %s", exc)
        if self.moves is None:
            return None
        st = self._tracker_status()
        raw = (st or {}).get("read_budget")
        return _book_reads_dict(raw.get("moves")) if isinstance(raw, Mapping) else None

    def _open_move(self, exchange_id: Any) -> Optional[Dict[str, Any]]:
        """``move_annotation`` of the outcome's OPEN outside-move alert (the watcher's lock-free
        ``open_alert_for``), or None."""
        if self.moves is None or exchange_id is None:
            return None
        fn = getattr(self.moves, "open_alert_for", None)
        if not callable(fn):
            return None
        try:
            annotation = move_annotation(fn(str(exchange_id)))
        except Exception as exc:
            log.debug("outside-move open_alert_for(%s) failed: %s", exchange_id, exc)
            return None
        return annotation if annotation is not None and annotation["state"] == "open" else None

    def _annotate_strategy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``outside_move`` on every value idea whose outcome (or any leg's) has an open outside-move alert (§18.4);
        nothing else changes, and the cached report is never mutated. Re-annotated after each watcher step (the
        published summary is the version), so a fresh alert shows within one step, not one strategy TTL."""
        watcher = self.moves
        opps = payload.get("opportunities")
        if watcher is None or not isinstance(opps, list) or not opps:
            return payload
        try:
            version = watcher.summary()
        except Exception:
            version = None
        with self._lock:
            cached = self._strategy_annotated
        if cached is not None and version is not None and cached[0] is payload and cached[1] is version:
            return cached[2]
        changed = False
        annotated: List[Any] = []
        for opp in opps:
            if isinstance(opp, Mapping) and opp.get("kind") == "value":
                ids = [opp.get("exchange_id")] + [leg.get("exchange_id") for leg in opp.get("legs") or []
                                                  if isinstance(leg, Mapping)]
                note = next((n for n in (self._open_move(eid) for eid in ids if eid is not None) if n is not None), None)
                if note is not None:
                    opp = dict(opp)
                    opp["outside_move"] = note
                    changed = True
            annotated.append(opp)
        out = payload
        if changed:
            out = dict(payload)
            out["opportunities"] = annotated
        if version is not None:
            with self._lock:
                self._strategy_annotated = (payload, version, out)
        return out

    def _annotate_fairvalue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Every /api/fairvalue row gains ``"move"``: the outcome's open outside-move alert (§18.4) or null."""
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return payload
        out = dict(payload)
        out["rows"] = [{**row, "move": self._open_move(row.get("exchange_id"))} if isinstance(row, Mapping) else row
                       for row in rows]
        return out

    def _build_fairvalue(self, now: float) -> Dict[str, Any]:
        history = None
        stats = getattr(self.store, "fair_value_source_stats", None)
        if callable(stats):
            try:
                history = stats("history")
            except Exception:
                history = None
        quotes = {r["exchange_id"]: r for r in self._rows(self._view())} if self.fair_values is not None else {}
        return fairvalue_payload(self.fair_values, quotes, now, history=history)


def fairvalue_payload(fv: Any, quotes: Mapping[str, Mapping[str, Any]], now: float, *,
                      history: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The ``GET /api/fairvalue`` body (§10.4) for a FairValueService (or None) and the Cup's current quotes
    (exchange id -> {"bid", "ask"}); also what ``fairvalue`` prints. Rows sorted by |gap| (nulls last), title."""
    from .fairvalue import FV_CAVEATS, override_snippet

    counts = {"outcomes": 0, "matched": 0, "usable": 0, "manual": 0, "no_external": 0, "suspect": 0, "near": 0}
    base: Dict[str, Any] = {
        "now": now, "enabled": False, "mode": "off", "last_refresh_at": None, "providers": [], "manual": None,
        "map": None, "history": None, "counts": counts, "rows": [], "caveats": list(FV_CAVEATS),
    }
    if fv is None:
        return base
    try:
        st = fv.status()
    except Exception as exc:
        log.warning("fair value status failed: %s", exc)
        return base
    base.update({"enabled": bool(st.get("enabled")), "mode": st.get("mode") or "off",
                 "last_refresh_at": st.get("last_refresh_at"), "manual": st.get("manual"), "map": st.get("map")})
    base["providers"] = [
        {"name": name, **{k: (info or {}).get(k) for k in ("status", "last_ok_at", "last_error", "requests", "matched",
                                                            "quoted", "next_try_at")}}
        for name, info in sorted((st.get("providers") or {}).items())
    ]
    base["history"] = dict(history) if isinstance(history, Mapping) and history.get("records") else None
    if not base["enabled"]:
        return base
    try:
        match_rows = list(fv.matches())
        targets = {t.exchange_id: t for t in fv.targets()}
    except Exception as exc:
        log.warning("fair value matches failed: %s", exc)
        return base
    rows: List[Dict[str, Any]] = []
    for m in match_rows:
        eid = str(m.exchange_id)
        q = quotes.get(eid) or {}
        bid, ask = _finite(q.get("bid")), _finite(q.get("ask"))
        mid = round((bid + ask) / 2, 6) if bid is not None and ask is not None else None
        fair = m.fair.to_dict() if m.fair is not None else None
        value = _finite(fair.get("value")) if fair else None
        if fair is not None:
            as_of = _finite(fair.get("as_of"))
            fair["age_s"] = round(now - as_of, 3) if as_of is not None else None
        matches = [vm.to_dict() for vm in m.matches]
        # ui-11: "near match" only when a NEAR match exists (a venue match of kind NEAR, or a fair value priced
        # from one): an unmatched outcome's FairValue still carries its race's match kind
        near = any(vm.kind == "NEAR" for vm in m.matches) or bool(
            fair and fair.get("match_kind") == "NEAR" and value is not None)
        has_match = bool(m.matches) or value is not None
        suspect = bool(fair and fair.get("suspect"))
        target = targets.get(eid)
        external = {vm.venue: vm.external_id for vm in m.matches if vm.venue in ("polymarket", "kalshi")}
        snippets: Dict[str, Optional[str]] = {
            "disable": f'"{eid}": {{"disabled": true, "note": "wrong match"}}' if has_match else None,
            "pin": None, "confirm": None, "trade_near": None}
        if target is not None:
            # "Turn this match off" only for a match that exists; pinning is how an unmatched outcome gets one
            snippets["disable"] = override_snippet(target, "disable") if has_match else None
            snippets["pin"] = override_snippet(target, "pin", external or None)
            if suspect:
                snippets["confirm"] = override_snippet(target, "confirm")
            if near:
                snippets["trade_near"] = override_snippet(target, "trade_near")
        rows.append({
            "exchange_id": eid, "market_id": str(m.market_id), "title": m.title, "option": m.option,
            "race_key": m.race_key, "party": m.party, "sm_bid": bid, "sm_ask": ask, "sm_mid": mid, "fair": fair,
            "matches": matches,
            "gap": round(value - mid, 6) if value is not None and mid is not None else None,
            "edge_yes": round(value - ask, 6) if value is not None and ask is not None else None,
            "edge_no": round(bid - value, 6) if value is not None and bid is not None else None,
            "near": near, "suspect": suspect, "snippets": snippets, "unmatched_reason": m.unmatched_reason,
        })
        counts["outcomes"] += 1
        counts["matched"] += 1 if matches else 0
        counts["usable"] += 1 if fair and fair.get("usable") and value is not None else 0
        counts["manual"] += 1 if fair and fair.get("source") == "manual" else 0
        counts["no_external"] += 0 if any(vm.venue != "manual" for vm in m.matches) else 1
        counts["suspect"] += 1 if suspect else 0
        counts["near"] += 1 if near else 0
    rows.sort(key=lambda r: (r["gap"] is None, -abs(r["gap"] or 0.0), r["title"] or "", r["exchange_id"]))
    base["rows"] = rows
    return base


# --------------------------------------------------------------------------- HTTP


MOVES_STATES_QUERY = ("open", "closed", "all")  # GET /api/moves?state=
MOVES_LIMIT_MAX = 500  # GET /api/moves?limit= (1..500)
MOVES_STATE_ERROR = "state must be open, closed or all."
MOVES_SINCE_ERROR = "since must be a number of seconds since 1970."
MOVES_LIMIT_ERROR = "limit must be a whole number from 1 to 500."
MOVES_FV_NOTICE = "Outside-move alerts need --fair-value auto: off."  # docs/OUTSIDE_MOVES.md §17.1 / §18.1
MOVES_POLL_ERROR = "--moves-poll must be between 5 and 120 seconds"
_WHOLE_NUMBER_RE = re.compile(r"^\+?[0-9]{1,6}$")


def _count(value: Any) -> int:
    number = _finite(value)
    return int(number) if number is not None and number > 0 else 0


def _book_reads_dict(raw: Any) -> Optional[Dict[str, int]]:
    """``{"used", "limit", "room"}`` of the watcher's Cup order-book budget (tracker.read_budget.moves), or None."""
    data = _plain(raw)
    if not isinstance(data, Mapping):
        return None
    return {"used": _count(data.get("used")), "limit": _count(data.get("limit")), "room": _count(data.get("room"))}


def moves_query(query: Mapping[str, Sequence[str]]) -> Tuple[Optional[str], Dict[str, Any]]:
    """``(error, kwargs)`` for ``GET /api/moves?state=&since=&limit=`` (docs/OUTSIDE_MOVES.md §18.2): ``error`` is one
    of the three 400 sentences, else None and ``kwargs`` are :meth:`DashboardApp.outside_moves` keywords. A blank
    value means the default (state all, no since, limit 100)."""

    def first(name: str) -> str:
        values = query.get(name) or [""]
        return str(values[0] if values else "").strip()

    kwargs: Dict[str, Any] = {"state": None, "since": None, "limit": None}
    state = first("state").lower()
    if state:
        if state not in MOVES_STATES_QUERY:
            return MOVES_STATE_ERROR, kwargs
        kwargs["state"] = state
    since = first("since")
    if since:
        try:
            value = float(since)
        except ValueError:
            return MOVES_SINCE_ERROR, kwargs
        if not math.isfinite(value) or value < 0:
            return MOVES_SINCE_ERROR, kwargs
        kwargs["since"] = value
    limit = first("limit")
    if limit:
        if not _WHOLE_NUMBER_RE.match(limit) or not 1 <= int(limit) <= MOVES_LIMIT_MAX:
            return MOVES_LIMIT_ERROR, kwargs
        kwargs["limit"] = int(limit)
    return None, kwargs


def _empty_moves_counts() -> Dict[str, Any]:
    from .moves import SUPPRESS_REASONS

    return {"open": 0, "lagging": 0, "actionable": 0, "today": 0, "closed_today": 0,
            "suppressed_today": {reason: 0 for reason in SUPPRESS_REASONS}}


def moves_payload(summary: Optional[Mapping[str, Any]], now: float, *, enabled: bool, demo: bool,
                  error: Optional[str] = None, book_reads: Optional[Mapping[str, Any]] = None,
                  alerts: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """The exact ``/api/moves`` body of docs/OUTSIDE_MOVES.md §18.2 from the watcher's published summary (or the
    disabled body when ``enabled`` is False). ``alerts`` replaces the summary's list when the request filtered it.
    Pure; every key always present. A watcher whose summary could not be read (``summary`` None while ``enabled``)
    answers like one that has not stepped yet (``available`` False) with ``error`` saying why, never as "off"."""
    from .moves import MOVES_CAVEATS, MOVES_DEMO_CAVEAT, MOVES_MAX_SUPPRESSED_SHOWN, MOVES_OFF_ERROR, LagSummary
    from .moves import SUPPRESS_REASONS

    if not enabled:
        return {
            "now": now, "enabled": False, "available": False, "error": MOVES_OFF_ERROR, "demo": bool(demo),
            "poll_s": None, "last_step_at": None, "steps": 0, "watching": {"outcomes": 0, "matched": 0, "venues": 0},
            "venues": [], "counts": _empty_moves_counts(), "actionable_ids": [], "alerts": [], "suppressed": [],
            "summary": None, "thresholds": None, "book_reads": None, "caveats": [],
        }
    s: Mapping[str, Any] = summary if isinstance(summary, Mapping) else {}
    steps = _count(s.get("steps"))
    raw_counts = s.get("counts") if isinstance(s.get("counts"), Mapping) else {}
    raw_supp = raw_counts.get("suppressed_today") if isinstance(raw_counts.get("suppressed_today"), Mapping) else {}
    counts = {key: _count(raw_counts.get(key)) for key in ("open", "lagging", "actionable", "today", "closed_today")}
    counts["suppressed_today"] = {reason: _count(raw_supp.get(reason)) for reason in SUPPRESS_REASONS}
    watching = s.get("watching") if isinstance(s.get("watching"), Mapping) else {}
    lag = s.get("summary")
    thresholds = s.get("thresholds")
    caveats = [str(c) for c in (s.get("caveats") or MOVES_CAVEATS) if c]
    if demo and MOVES_DEMO_CAVEAT not in caveats:  # demo results always say they are scripted
        caveats.append(MOVES_DEMO_CAVEAT)
    listed = alerts if alerts is not None else s.get("alerts")
    return {
        "now": now,
        "enabled": True,
        "available": summary is not None and steps >= 1,
        "error": str(error) if error else None,
        "demo": bool(demo),
        "poll_s": _finite(s.get("poll_s")),
        "last_step_at": _finite(s.get("last_step_at")),
        "steps": steps,
        "watching": {key: _count(watching.get(key)) for key in ("outcomes", "matched", "venues")},
        "venues": [dict(v) for v in s.get("venues") or [] if isinstance(v, Mapping)],
        "counts": counts,
        "actionable_ids": [str(a) for a in s.get("actionable_ids") or [] if a is not None],
        "alerts": [a for a in listed or [] if isinstance(a, Mapping)],
        "suppressed": [x for x in s.get("suppressed") or [] if isinstance(x, Mapping)][:MOVES_MAX_SUPPRESSED_SHOWN],
        "summary": dict(lag) if isinstance(lag, Mapping) else LagSummary().to_dict(),
        "thresholds": dict(thresholds) if isinstance(thresholds, Mapping) else None,
        "book_reads": _book_reads_dict(book_reads),
        "caveats": caveats,
    }


def move_annotation(alert: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The compact ``outside_move`` / ``move`` object added to /api/strategy value ideas and /api/fairvalue rows
    (§18.4): {"alert_id", "status", "status_label", "state", "direction", "move", "lag_gap_now", "detected_at",
    "actionable"}, or None. Takes a full alert dict or the watcher's compact ``open_alert_for`` one."""
    from .moves import MOVE_STATUS_LABELS

    data = _plain(alert)
    if not isinstance(data, Mapping) or not data.get("alert_id"):
        return None
    status = str(data.get("status") or "") or None
    direction = _finite(data.get("direction"))
    return {
        "alert_id": str(data["alert_id"]),
        "status": status,
        "status_label": str(data.get("status_label") or MOVE_STATUS_LABELS.get(status or "", "") or "") or None,
        "state": str(data.get("state") or "open"),
        "direction": (1 if direction > 0 else -1) if direction else None,
        "move": _finite(data.get("move")),
        "lag_gap_now": _finite(data.get("lag_gap_now")),
        "detected_at": _finite(data.get("detected_at")),
        "actionable": bool(data.get("actionable")),
    }


def is_loopback_host(host: str) -> bool:
    """True for addresses only this machine can reach (127.0.0.0/8, ::1, localhost)."""
    name = (host or "").strip().lower().strip("[]")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def is_wildcard_host(host: str) -> bool:
    return (host or "").strip().strip("[]") in ("", "0.0.0.0", "::")


def machine_addresses(ipv6: bool = False) -> List[str]:
    """This machine's own non-loopback IP addresses (best effort; no packets are sent)."""
    found: List[str] = []

    def add(address: Any) -> None:
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError:
            return
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or (ip.version == 6 and not ipv6):
            return
        text = str(ip)
        if text not in found:
            found.append(text)

    # The interface the default route uses: connecting a UDP socket only picks a source address.
    probes: List[Tuple[int, Tuple[Any, ...]]] = [(socket.AF_INET, ("10.255.255.255", 1))]
    if ipv6:
        probes.append((socket.AF_INET6, ("fd00::1", 1, 0, 0)))
    for family, target in probes:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                add(probe.getsockname()[0])
        except OSError:
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            add(info[4][0])
    except (OSError, UnicodeError):
        pass
    return found


def _split_host(host: str) -> Tuple[str, Optional[int]]:
    """``"name:port"`` / ``"[v6]:port"`` / ``"name"`` -> (lower-case name without brackets, port or None)."""
    host = (host or "").strip().lower()
    port_text = ""
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        if rest.startswith(":"):
            port_text = rest[1:]
        elif rest:
            return "", None
    elif host.count(":") == 1:
        name, _, port_text = host.partition(":")
    elif ":" in host:  # a bare IPv6 address (not valid in a Host header, but harmless here)
        name = host
    else:
        name = host
    if port_text:
        if not port_text.isdigit() or len(port_text) > 5:
            return "", None
        return name, int(port_text)
    return name, None


def _ip_of(text: Any) -> Any:
    """An :mod:`ipaddress` object (IPv4-mapped IPv6 as IPv4), or None for a name."""
    try:
        ip = ipaddress.ip_address(str(text or "").strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def machine_names() -> List[str]:
    """This machine's own host names (best effort, lower-case): ``gethostname()``, its FQDN and ``<name>.local``."""
    found: List[str] = []

    def add(name: Any) -> None:
        text = str(name or "").strip().lower().rstrip(".")
        if text and not is_loopback_host(text) and _ip_of(text) is None and text not in found:
            found.append(text)

    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    add(name)
    short = str(name or "").strip().lower().split(".", 1)[0]
    if short:
        add(short)
        add(f"{short}.local")  # mDNS / Bonjour
    try:
        add(socket.getfqdn(name) if name else "")
    except (OSError, UnicodeError):
        pass
    return found


class DashboardServer(ThreadingHTTPServer):
    """The dashboard's HTTP server.

    Every request must carry a ``Host`` this server knows as its own (421 otherwise), which
    stops DNS-rebinding pages (a page on some domain that re-points that domain at this
    machine) from reading the dashboard or queueing analyses:

    * always the loopback names (``127.0.0.1``, ``localhost``, ``[::1]``) and the bound address;
    * on a non-loopback bind (``--host 0.0.0.0``, a LAN address) also this machine's own IP
      addresses and names (:func:`machine_addresses`, :func:`machine_names`) and the local
      address the connection actually arrived on, so other devices can open the printed URL;
    * any name passed in ``allow_hosts`` (``--allow-host``), e.g. a reverse proxy's. ``name``
      matches on any port, ``name:port`` only on that port.

    ``allow_any_host=True`` switches the check off for reads (no CLI option sets it); a POST
    still needs a known ``Host`` and that page's own ``Origin`` (:meth:`origin_allowed`).
    """

    daemon_threads = True
    # SO_REUSEADDR lets two servers share a port on Windows; only use it elsewhere.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 32

    def __init__(
        self,
        app: DashboardApp,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        allow_any_host: Optional[bool] = None,
        allow_hosts: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.bind_host = host
        self.local_only = is_loopback_host(host)
        self.allow_any_host = bool(allow_any_host)
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), DashboardHandler)
        self.lan_addresses: List[str] = machine_addresses(ipv6=":" in host) if is_wildcard_host(host) else []
        self.machine_names: List[str] = [] if self.local_only else machine_names()
        self.extra_hosts, self.extra_names = self._extra_hosts(allow_hosts)
        self.allowed_hosts = self._allowed_hosts()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @staticmethod
    def _extra_hosts(allow_hosts: Iterable[str]) -> Tuple[frozenset, frozenset]:
        """``--allow-host`` entries: (exact ``name:port`` values, names allowed on any port)."""
        exact, names = set(), set()
        for entry in allow_hosts or ():
            text = str(entry or "").strip().lower()
            if "://" in text:  # tolerate a pasted URL
                text = urlsplit(text).netloc
            text = text.rstrip("/").rstrip(".")
            if not text or not _HOST_HEADER_RE.match(text):
                continue
            name, port = _split_host(text)
            if not name:
                continue
            shown = f"[{name}]" if ":" in name else name
            if port is None:
                names.add(name)
            else:
                exact.add(f"{shown}:{port}")
        return frozenset(exact), frozenset(names)

    def _allowed_hosts(self) -> frozenset:
        port = self.port
        names = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        host = self.bind_host.strip().lower()
        if host and not is_wildcard_host(host):
            names.add(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
        for address in self.lan_addresses:  # a wildcard bind: this machine's own addresses
            names.add(f"[{address}]:{port}" if ":" in address else f"{address}:{port}")
        for name in self.machine_names:  # a non-loopback bind: this machine's own names
            names.add(f"{name}:{port}")
        if port == 80:
            names |= {name.rsplit(":", 1)[0] for name in names}
        return frozenset(names | self.extra_hosts)

    def host_allowed(self, host_header: Optional[str], local_address: Any = None, *, any_host: Optional[bool] = None) -> bool:
        """Whether ``host_header`` names this server (``local_address``: where the connection arrived)."""
        host = (host_header or "").strip().lower()
        if not host or not _HOST_HEADER_RE.match(host):
            return False
        if host in self.allowed_hosts:
            return True
        name, port = _split_host(host)
        if not name:
            return False
        if name in self.extra_names:
            return True
        if not self.local_only and local_address is not None and (port == self.port or (port is None and self.port == 80)):
            # The IP address the client connected to is this machine's by definition. A rebinding
            # page always sends its own domain name, never an IP address.
            local, asked = _ip_of(local_address), _ip_of(name)
            if local is not None and asked is not None and asked == local and not local.is_unspecified:
                return True
        return self.allow_any_host if any_host is None else bool(any_host)

    def origin_allowed(self, origin: Optional[str], host_header: Optional[str], local_address: Any = None) -> bool:
        """A POST's ``Origin``: exactly the page's own origin, on a ``Host`` this server knows.

        Origin equal to Host alone proves nothing when a rebinding page controls both, so the
        Host must also pass the allow-list (even with ``allow_any_host``). ``https://`` is
        accepted only for an ``--allow-host`` name (a TLS-terminating reverse proxy).
        """
        text = (origin or "").strip().lower()
        host = (host_header or "").strip().lower()
        if not text or not host or not self.host_allowed(host, local_address, any_host=False):
            return False
        if text == "http://" + host:
            return True
        if text == "https://" + host:
            name, _port = _split_host(host)
            return host in self.extra_hosts or name in self.extra_names
        return False

    @property
    def url(self) -> str:
        host = self.bind_host
        if is_wildcard_host(host):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def network_urls(self) -> List[str]:
        """URLs other devices can use for a wildcard bind (empty for any other bind)."""
        return [f"http://[{a}]:{self.port}" if ":" in a else f"http://{a}:{self.port}" for a in self.lan_addresses]

    def handle_error(self, request: Any, client_address: Any) -> None:  # quiet disconnects
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            log.debug("client %s disconnected: %s", client_address, exc)
            return
        log.warning("dashboard request from %s failed", client_address, exc_info=True)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "SuperMarketDashboard/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # seconds a client may idle (slow clients cannot pin threads forever)

    # ------------------------------------------------------------------ plumbing
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("dashboard %s - %s", self.address_string(), format % args)

    def _headers(self, status: int, content_type: str, length: int, extra: Sequence[Tuple[str, str]] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str, extra: Sequence[Tuple[str, str]] = ()) -> None:
        self._headers(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: Sequence[Tuple[str, str]] = ()) -> None:
        body = encode_json(payload, self.server.app.secrets)
        self._send(status, body, JSON_TYPE, (("Cache-Control", "no-store"),) + tuple(extra))

    def _error(self, status: int, message: str, extra: Sequence[Tuple[str, str]] = ()) -> None:
        self._json(status, {"error": message}, extra)

    def _local_address(self) -> Optional[str]:
        """The address this connection arrived on (this machine's, whatever the interface)."""
        try:
            return str(self.connection.getsockname()[0])
        except (OSError, AttributeError, IndexError, TypeError):
            return None

    def _host_ok(self) -> bool:
        return self.server.host_allowed(self.headers.get("Host"), self._local_address())

    def _guard(self) -> bool:
        if not self._host_ok():
            self.close_connection = True
            self._error(421, "Unrecognised Host header. Open the dashboard at the address it printed.")
            return False
        return True

    # ------------------------------------------------------------------ methods
    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        parts = urlsplit(self.path)
        path = parts.path
        try:
            if path.startswith("/api/"):
                self._api_get(path, parse_qs(parts.query, keep_blank_values=True))
            else:
                self._static(path)
        except (ConnectionError, socket.timeout):
            raise
        except Exception:
            log.warning("dashboard GET %s failed", path, exc_info=True)
            self._error(500, "Internal error; see the dashboard's log.")

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlsplit(self.path).path
        # Only a page this dashboard served (or an --allow-host name) may POST: an Origin merely
        # equal to the Host header proves nothing when a rebinding page controls both.
        if not self.server.origin_allowed(self.headers.get("Origin"), self.headers.get("Host"), self._local_address()):
            self.close_connection = True
            return self._error(403, "Cross-origin requests are not allowed.")
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            return self._error(403, "POST requests must send JSON (Content-Type: application/json).")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self._error(411, "Send the body with a Content-Length.")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self.close_connection = True
            return self._error(400, "Invalid Content-Length.")
        if length < 0:
            self.close_connection = True
            return self._error(400, "Invalid Content-Length.")
        if length > MAX_POST_BYTES:
            self.close_connection = True
            return self._error(413, "Request body too large (16 KB maximum).")
        body = self.rfile.read(length) if length else b""
        data: Dict[str, Any] = {}
        if body.strip():
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._error(400, "The request body is not valid JSON.")
            if not isinstance(data, dict):
                return self._error(400, "The request body must be a JSON object.")
        if re.fullmatch(r"/api/paper/reset", path):
            # Local only: ends this process's simulated run and starts a new one (nothing leaves the machine).
            try:
                status, payload = self.server.app.paper_reset(data)
            except Exception:
                log.warning("dashboard POST %s failed", path, exc_info=True)
                return self._error(500, "Internal error; see the dashboard's log.")
            return self._json(status, payload)
        match = re.fullmatch(r"/api/surges/([^/]+)/analyze", path)
        if match:
            try:
                status, payload = self.server.app.analyze(unquote(match.group(1)))
            except Exception:
                log.warning("dashboard POST %s failed", path, exc_info=True)
                return self._error(500, "Internal error; see the dashboard's log.")
            return self._json(status, payload)
        if path.startswith("/api/"):
            known_get = path in _GET_ROUTES or path.startswith("/api/exchange/")
            if known_get:
                return self._error(405, "Method not allowed.", (("Allow", "GET, HEAD"),))
            return self._error(404, "Not found.")
        return self._error(405, "Method not allowed.", (("Allow", "GET, HEAD"),))

    def _not_allowed(self) -> None:
        if not self._guard():
            return
        self._error(405, "Method not allowed.", (("Allow", "GET, HEAD, POST"),))

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Errors raised by the stdlib itself (unknown method, 414, 431…) as JSON with security headers."""
        extra: Tuple[Tuple[str, str], ...] = (("Cache-Control", "no-store"),)
        if code == 501:  # no do_<METHOD> handler: answer like the other unsupported methods
            code, message = 405, "Method not allowed."
            extra += (("Allow", "GET, HEAD, POST"),)
        short = self.responses.get(code, ("Error", ""))[0]
        body = json.dumps({"error": message or short}).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(code, short)
            self.send_header("Content-Type", JSON_TYPE)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in SECURITY_HEADERS + extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)
        except OSError:  # the client went away
            pass

    # ------------------------------------------------------------------ routes
    def _api_get(self, path: str, query: Mapping[str, List[str]]) -> None:
        app = self.server.app
        if path in _GET_ROUTES:
            name = _GET_ROUTES[path]
            if name == "markets":
                q = (query.get("q") or [""])[0][:200]
                return self._json(200, app.markets(q))
            if name == "paper":
                which = (query.get("run") or [""])[0].strip().lower()
                if which == "previous":
                    previous = app.paper(run="previous")
                    if previous is None:
                        return self._error(404, NO_PREVIOUS_RUN)
                    return self._json(200, previous)
                return self._json(200, app.paper())
            if name == "outside_moves":  # docs/OUTSIDE_MOVES.md §18.2
                problem, kwargs = moves_query(query)
                if problem is not None:
                    return self._error(400, problem)
                return self._json(200, app.outside_moves(**kwargs))
            return self._json(200, getattr(app, name)())
        if path == "/api/paper/reset":
            return self._error(405, "Use POST to reset the simulation.", (("Allow", "POST"),))
        if path.startswith("/api/exchange/"):
            eid = unquote(path[len("/api/exchange/") :])
            data = app.exchange(eid)
            if data is None:
                return self._error(404, "Unknown exchange.")
            return self._json(200, data)
        if re.fullmatch(r"/api/surges/[^/]+/analyze", path):
            return self._error(405, "Use POST to re-analyze a surge.", (("Allow", "POST"),))
        return self._error(404, "Not found.")

    def _static(self, path: str) -> None:
        target = resolve_static(path)
        if target is None:
            return self._error(404, "Not found.")
        try:
            body = target.read_bytes()
        except OSError:
            return self._error(404, "Not found.")
        self._send(200, body, STATIC_TYPES[target.suffix.lower()], (("Cache-Control", "no-cache"),))


_GET_ROUTES: Dict[str, str] = {
    "/api/health": "health",
    "/api/status": "status",
    "/api/markets": "markets",
    "/api/surges": "surges",
    "/api/highband": "highband",
    "/api/strategy": "strategy",
    "/api/paper": "paper",
    "/api/backtest": "backtest",
    "/api/fairvalue": "fairvalue",
    "/api/moves": "outside_moves",  # docs/OUTSIDE_MOVES.md §18.2
}


def resolve_static(url_path: str, root: Path = WEB_DIR) -> Optional[Path]:
    """Map a URL path to a file inside ``root`` (None for anything else: traversal, dotfiles, unknown types)."""
    path = unquote(url_path or "/")
    if "\x00" in path or "\\" in path:
        return None
    if path in ("", "/"):
        path = "/index.html"
    segments = path.split("/")[1:]
    if not segments or any(seg in ("", ".", "..") or seg.startswith(".") for seg in segments):
        return None
    candidate = root.joinpath(*segments)
    if candidate.suffix.lower() not in STATIC_TYPES:
        return None
    try:
        real_root = root.resolve()
        real = candidate.resolve()
        real.relative_to(real_root)
    except (OSError, ValueError, RuntimeError):
        return None
    return real if real.is_file() else None


def make_server(
    app: DashboardApp,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    allow_any_host: Optional[bool] = None,
    allow_hosts: Iterable[str] = (),
) -> DashboardServer:
    return DashboardServer(app, host, port, allow_any_host=allow_any_host, allow_hosts=allow_hosts)


# --------------------------------------------------------------------------- runtime


@dataclasses.dataclass
class Runtime:
    """Everything a running dashboard owns, so it can be shut down in one call."""

    app: DashboardApp
    tracker: Any
    store: Any
    client: Any
    context: Any
    news: Any = None
    demo_market: Any = None
    fair_values: Any = None  # the tracker's FairValueService (None with --fair-value off)
    clock: Any = None  # the SimClock of a fast demo run (None: real time)
    moves: Any = None  # the OutsideMoveWatcher (None when outside-move alerts are off; docs/OUTSIDE_MOVES.md §18.1)

    def start(self) -> None:
        self.tracker.start()

    def close(self) -> None:
        for step, fn in (
            ("tracker", lambda: self.tracker.stop()),
            ("news", lambda: self.news.close() if self.news is not None and hasattr(self.news, "close") else None),
            # the watcher before the fair values (its providers) and the store it writes to (§18.1): a step still
            # running finishes its writes, and a later step does nothing
            ("outside moves", lambda: self.moves.close() if self.moves is not None else None),
            ("fair values", lambda: self.fair_values.close() if self.fair_values is not None else None),
            ("client", lambda: self.client.close()),
            ("store", lambda: self.store.close()),
        ):
            try:
                fn()
            except Exception as exc:  # keep shutting down the rest
                log.warning("stopping %s failed: %s", step, exc)


def _remove_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def _refuse_if_tracked(db: Path, interval: float) -> None:
    """Raise ``tracker.TrackerBusy`` when another live process holds ``db``'s tracker lease, so a demo start never
    deletes a running demo's database. Like ``store.acquire_lease`` (live-5): a process on this machine is asked
    directly (running: refused whatever its heartbeat age, up to LEASE_LIVE_HOLDER_MAX_S; ended: not refused);
    otherwise its heartbeat must be younger than 3 intervals of wall time."""
    if not db.is_file():
        return
    from .store import LEASE_LIVE_HOLDER_MAX_S, TrackerStore, lease_holder_alive
    from .tracker import LEASE_NAME, TrackerBusy

    try:
        st = TrackerStore.open_read_only(db)
    except Exception:  # an older or broken file: nothing can be tracking it with this code
        return
    try:
        holder = st.lease(LEASE_NAME)
    except Exception:
        holder = None
    finally:
        st.close()
    if not holder:
        return
    beat = _finite(holder.get("heartbeat_at"))
    ttl = 3.0 * max(float(interval), 30.0)
    alive = lease_holder_alive(holder)
    if alive is False:
        return  # that process ended without shutting down (a crash, a closed terminal): nothing tracks this file
    limit = max(ttl, LEASE_LIVE_HOLDER_MAX_S) if alive else ttl
    now = time.time()
    if beat is not None and -limit < now - beat < limit:  # like store.acquire_lease (a far-future beat is a SimClock's)
        raise TrackerBusy({**holder, "alive": alive}, str(db), ttl_s=ttl, now=now)


def _judge(enabled: bool, out: TextIO) -> Any:
    if not enabled:
        return None
    from .attribution import LLMJudge

    judge = LLMJudge()
    if judge.available():
        return judge
    print(
        "warning: --llm needs the `anthropic` package and ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN); "
        "continuing with the heuristic only.",
        file=out,
        flush=True,
    )
    return None


# Read budgets of the simulated API (docs/PAPER_TRADING.md §9). Real time: the demo reads faster than a live key
# may (the simulation has no real budget). Fast mode (a demo.SimClock): above the per-step demand x steps per
# simulated minute, so no limiter ever needs to wait (a wait raises demo.SimClockStall instead, D42).
DEMO_READS_PER_MIN = 600
DEMO_BACKFILL_READS_PER_MIN = 240
DEMO_ANALYZE_READS_PER_MIN = 120
DEMO_PAPER_READS_PER_MIN = 60
FAST_READS_PER_MIN = 10_000
FAST_PAPER_READS_PER_MIN = 600
DEMO_T0_STATE_KEY = "demo.t0"  # the demo's scripted start, kept in its store (a rebuild on a kept store continues it)
FV_AUTO_NOTICE = "Outside fair values: reading Polymarket and Kalshi (GET only, no account); use --fair-value manual to stop."


def _fair_value_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    if mode not in ("auto", "manual", "off"):
        raise ValueError(f"--fair-value must be auto, manual or off, not {value!r}")
    return mode


def _paper_config(*, demo: bool, sizing: str, regime: str, all_collateral: bool, start_capital: Optional[float],
                  reads_per_min: Optional[int], interval: float, target_hours: Optional[float]) -> Any:
    """The PaperConfig the builders and the ``paper`` command use (§7.4)."""
    from .models import PaperConfig, default_portfolios

    kwargs: Dict[str, Any] = dict(
        demo=bool(demo), sizing=sizing, portfolios=default_portfolios(sizing), regime=str(regime or "unknown"),
        all_collateral=bool(all_collateral), start_capital=float(start_capital) if start_capital else None,
        interval_s=float(interval),
    )
    if reads_per_min is not None:
        kwargs["reads_per_min"] = int(reads_per_min)
    if target_hours is not None:
        kwargs["target_hours"] = float(target_hours)
    return PaperConfig(**kwargs)


def build_demo(
    data_dir: Path,
    interval: float = 30.0,
    *,
    llm: bool = False,
    news: bool = True,
    out: TextIO = sys.stderr,
    seed: int = 7,
    paper: bool = True,
    fair_value: str = "auto",
    regime: str = "unknown",
    sizing: str = "conservative",
    all_collateral: bool = False,
    paper_capital: Optional[float] = None,
    clock: Optional[Callable[[], float]] = None,
    fresh_db: bool = True,
    target_hours: Optional[float] = None,
    fair_value_providers: Optional[Callable[[Any], Sequence[Any]]] = None,
    moves: bool = False,
    moves_poll_s: Optional[float] = None,
    moves_book_reads_per_min: Optional[int] = None,
) -> Runtime:
    """A dashboard over the simulated market (no API key, no network).

    ``paper`` runs the paper trader (§7.4), ``fair_value`` "auto" uses the demo's scripted outside prices
    (``demo.DemoFairValueProvider``), "manual" only ``<data_dir>/demo/fair_values.json``, "off" none.
    ``clock`` (optional) is injected into every time-aware piece; a :class:`demo.SimClock` also switches
    to **fast mode** (D42): every limiter runs on the SimClock with budgets above the per-step demand and a
    ``sleep`` (:func:`demo.no_wait`) that raises ``SimClockStall`` instead of waiting. ``fresh_db=False``
    keeps an existing ``<data_dir>/demo/tracker.sqlite3`` (restart continuity, D47). ``fair_value_providers``
    (tests) builds the "auto" providers from the DemoMarket instead of ``[DemoFairValueProvider(market)]``.

    docs/OUTSIDE_MOVES.md §18.1 (package "server"): ``moves`` builds ``DemoMarket(outside_moves=True)``, an
    OutsideMoveWatcher over the two DemoOutsideVenue venues (needs fair value "auto"), ``moves_poll_s`` (default
    MOVES_POLL_S) and ``moves_book_reads_per_min`` (default: FAST_READS_PER_MIN in fast mode, else
    MOVES_READS_PER_MIN; an explicit 0 turns the Cup book reads off in both); False (the default) builds exactly the
    demo of before. With fair value "manual" / "off" no watcher is built and ``MOVES_FV_NOTICE`` is printed to
    ``out`` (never an exception). The watcher writes ``<data_dir>/demo/alerts.jsonl`` (append-only) and the store;
    ``fresh_db=False`` keeps both, so a rebuilt demo continues its alerts (restart continuity)."""
    from .attribution import Attributor
    from .bot import resolve_context
    from .client import SuperMarketClient
    from .demo import DEMO_SLUG, DemoFairValueProvider, DemoMarket, DemoNewsProvider, SimClock, no_wait
    from .news import NewsSearcher
    from .store import TrackerStore
    from .tracker import Tracker

    fv_mode = _fair_value_mode(fair_value)
    fast = isinstance(clock, SimClock)
    poll_s = _moves_poll_value(moves_poll_s) if moves else None
    if moves and fv_mode != "auto":
        print(MOVES_FV_NOTICE, file=out, flush=True)
        moves = False
    timed: Dict[str, Any] = {"clock": clock} if clock is not None else {}
    db = Path(data_dir) / "demo" / "tracker.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    if fresh_db:
        _refuse_if_tracked(db, interval)  # never delete the database of a running demo (D46)
        _remove_db(db)
    store: Any = TrackerStore(db, **timed)
    client: Any = None
    fair_values: Any = None
    searcher: Any = None
    watcher: Any = None
    try:
        # A kept database continues its demo: the scripted events keep their original start (D47).
        saved_t0 = None if fresh_db else _finite(store.get_state(DEMO_T0_STATE_KEY))
        # the outside-move scenarios exist only with moves (docs/OUTSIDE_MOVES.md §15.1): without them the
        # DemoMarket is exactly the one the paper-trading tests know
        market_kw: Dict[str, Any] = {"outside_moves": True} if moves else {}
        if clock is not None:
            market = DemoMarket(seed=seed, now=float(clock()), clock=clock, start=saved_t0, **market_kw)
        else:
            market = DemoMarket(seed=seed, start=saved_t0, **market_kw)
        store.set_state(DEMO_T0_STATE_KEY, market.t0)
        limiter_kw: Dict[str, Any] = {"clock": clock, "sleep": no_wait} if fast else {}
        client = SuperMarketClient(
            api_key="demo", base_url="https://demo.invalid/api/v1", transport=market.transport(),
            reads_per_min=FAST_READS_PER_MIN if fast else DEMO_READS_PER_MIN, **limiter_kw,
        )
        enforce_get_only(client)  # D20: the simulator never sends anything but GET (the tracker's copy shares it)
        context = resolve_context(client, slug=DEMO_SLUG)
        searcher = NewsSearcher([DemoNewsProvider(market)], store=store, **timed) if news else None
        judge = _judge(llm, out)
        attributor = Attributor(client, context, store, searcher, judge=judge, **timed)
        holder: List[Any] = []  # the tracker, once built (its lock-free mids feed the fair-value suspect check)
        if fv_mode != "off":
            from .fairvalue import FairValueService

            if fv_mode != "auto":
                providers: List[Any] = []
            elif fair_value_providers is not None:
                providers = list(fair_value_providers(market))
            else:
                providers = [DemoFairValueProvider(market)]
            fair_values = FairValueService(
                db.parent, mode=fv_mode, providers=providers,
                recorder=store.add_fair_values, refresh_recorder=store.add_fair_value_refresh,
                cup_mids=lambda: holder[0].cup_mids() if holder else {}, **timed,
            )
        paper_rpm = FAST_PAPER_READS_PER_MIN if fast else DEMO_PAPER_READS_PER_MIN
        engine: Any = None
        if paper:
            from .paper import PaperEngine

            engine = PaperEngine(
                _paper_config(demo=True, sizing=sizing, regime=regime, all_collateral=all_collateral,
                              start_capital=paper_capital, reads_per_min=paper_rpm, interval=interval,
                              target_hours=target_hours),
                persistence=store, code_version=pipeline.code_version(), **timed,
            )
        tracker_kw: Dict[str, Any] = dict(timed)
        if fast:
            tracker_kw.update(limiter_clock=clock, limiter_sleep=no_wait)
        if moves:  # docs/OUTSIDE_MOVES.md §18.1
            from .demo import MOVES_DEMO_VENUES, DemoOutsideVenue
            from .moves import MOVES_ALERTS_FILE, MoveParams, OutsideMoveWatcher
            from .tracker import MOVES_READS_PER_MIN

            # the scripted venues are NOT fair-value providers (M12): the paper trader never sees their quotes
            watcher = OutsideMoveWatcher(
                fair_values, providers=[DemoOutsideVenue(market, venue) for venue in MOVES_DEMO_VENUES],
                persistence=store, alerts_path=db.parent / MOVES_ALERTS_FILE, clock=clock or time.time,
                params=MoveParams(poll_s=poll_s), demo=True,
            )
            if moves_book_reads_per_min is not None and int(moves_book_reads_per_min) <= 0:
                book_rpm = 0  # --book-reads 0: no Cup order-book reads at all (the trade box says shares unknown)
            elif fast:
                book_rpm = FAST_READS_PER_MIN  # a SimClock run never waits for a limiter (D42)
            else:
                book_rpm = int(moves_book_reads_per_min) if moves_book_reads_per_min is not None else MOVES_READS_PER_MIN
            tracker_kw.update(moves=watcher, moves_reads_per_min=book_rpm)
        tracker = Tracker(
            client,
            context,
            store,
            interval=interval,
            attributor=attributor,
            backfill_reads_per_min=FAST_READS_PER_MIN if fast else DEMO_BACKFILL_READS_PER_MIN,
            analyze_reads_per_min=FAST_READS_PER_MIN if fast else DEMO_ANALYZE_READS_PER_MIN,
            fair_values=fair_values,
            paper=engine,
            paper_reads_per_min=paper_rpm,
            regime=regime,
            lease=True,
            **tracker_kw,
        )
        holder.append(tracker)
        app_kw: Dict[str, Any] = dict(timed)
        if watcher is not None:
            app_kw["moves"] = watcher
        app = DashboardApp(
            tracker, store, client, context, demo=True, interval=interval, news_enabled=news, llm_enabled=judge is not None,
            fair_values=fair_values, regime=regime, sizing=sizing, **app_kw,
        )
    except BaseException:  # including Ctrl-C while starting
        for thing in (watcher, searcher, fair_values, store, client):
            if thing is not None and hasattr(thing, "close"):
                try:
                    thing.close()
                except Exception as exc:
                    log.debug("closing %r after a failed start failed: %s", thing, exc)
        raise
    return Runtime(app, tracker, store, client, context, news=searcher, demo_market=market, fair_values=fair_values,
                   clock=clock if fast else None, moves=watcher)


def _moves_poll_value(value: Any) -> float:
    """The watcher's poll period: ``value`` (seconds, MOVES_POLL_MIN_S..MOVES_POLL_MAX_S) or MOVES_POLL_S when None.
    ``ValueError`` (with the CLI's sentence) for anything else."""
    from .moves import MOVES_POLL_MAX_S, MOVES_POLL_MIN_S, MOVES_POLL_S

    if value is None:
        return float(MOVES_POLL_S)
    poll = _finite(value)
    if poll is None or not MOVES_POLL_MIN_S <= poll <= MOVES_POLL_MAX_S:
        raise ValueError(MOVES_POLL_ERROR)
    return poll


# Startup against the real API: when the first read (the tournament) fails with a temporary
# error (5xx such as the documented 503 "balances temporarily unavailable", 429, a network
# error), wait this long before each new attempt: about 5 minutes in all, then give up.
STARTUP_RETRY_WAITS_S: Tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 60.0, 60.0)
MAX_STARTUP_WAIT_S = 120.0  # cap on a server-requested Retry-After between start-up attempts
# Store state key: the tournament resolved on the last successful start (reused if the lookup
# fails temporarily on a later start; the tracker then loads the balance once the API answers).
CONTEXT_STATE_KEY = "dashboard.context"


def is_transient_error(exc: BaseException) -> bool:
    """A failure that may go away by itself (5xx, 408/425/429, no response), not a rejected key."""
    from .bot import is_fatal
    from .errors import ApiError, NetworkError

    if is_fatal(exc):
        return False
    if isinstance(exc, NetworkError):
        return True
    return isinstance(exc, ApiError) and (exc.status in (408, 425, 429) or exc.status >= 500)


def _save_context(store: Any, context: Any) -> None:
    if not getattr(context, "tournament_id", None) or not getattr(context, "slug", None):
        return
    try:
        store.set_state(
            CONTEXT_STATE_KEY,
            {
                "tournament_id": str(context.tournament_id),
                "slug": context.slug,
                "name": context.name,
                "currency": context.currency,
                "status": context.status,
                "saved_at": time.time(),
            },
        )
    except Exception as exc:  # a cache only: never stop the start-up over it
        log.debug("could not save the dashboard context: %s", exc)


def _saved_context(store: Any, slug: str) -> Any:
    """The context saved for ``slug`` by an earlier start, or None."""
    from .bot import Context

    try:
        raw = store.get_state(CONTEXT_STATE_KEY)
    except Exception:
        return None
    if not isinstance(raw, Mapping) or str(raw.get("slug") or "").lower() != str(slug).lower():
        return None
    tid = raw.get("tournament_id")
    if not isinstance(tid, (str, int)) or isinstance(tid, bool) or not str(tid).strip():
        return None

    def text(key: str) -> Optional[str]:
        value = raw.get(key)
        return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value) else None

    return Context(
        tournament_id=str(tid).strip(),
        slug=str(raw.get("slug")),
        name=text("name") or str(raw.get("slug")),
        currency=text("currency"),
        status=text("status"),
    )


def _resolve_live_context(
    client: Any,
    slug: Optional[str],
    public: bool,
    data_dir: Path,
    out: TextIO,
    *,
    sleep: Callable[[float], None],
    waits: Sequence[float],
    secrets: Sequence[str] = (),
) -> Tuple[Any, Any]:
    """``bot.resolve_context`` that rides out a temporarily unavailable API.

    Returns ``(context, store)``; ``store`` is the already-open store when the context came
    from it (a saved context), else None. Non-temporary errors are raised at once.
    """
    from .bot import resolve_context
    from .errors import ApiError
    from .store import TrackerStore

    scrub = _Scrubber(secrets)
    attempt = 0
    looked_for_saved = False
    while True:
        try:
            return resolve_context(client, slug=slug, public=public), None
        except Exception as exc:
            if not is_transient_error(exc):
                raise
            reason = scrub.text(str(exc) or type(exc).__name__)
            if len(reason) > 240:
                reason = reason[:237] + "…"
            if slug and not public and not looked_for_saved:
                looked_for_saved = True
                db = Path(data_dir) / slug / "tracker.sqlite3"
                if db.is_file():
                    store = TrackerStore(db)
                    try:
                        saved = _saved_context(store, slug)
                    except BaseException:
                        store.close()
                        raise
                    if saved is not None:
                        print(
                            f"The Super Market API could not confirm the tournament right now ({reason}); "
                            f"using the details saved for {slug} on the last run. "
                            "Your balance will show once the API answers.",
                            file=out,
                            flush=True,
                        )
                        return saved, store
                    store.close()
            if attempt >= len(waits):
                total = sum(float(w) for w in waits)
                print(
                    f"The Super Market API is still unavailable after {math.ceil(total / 60)} min of retries; "
                    "try again later.",
                    file=out,
                    flush=True,
                )
                raise
            wait = float(waits[attempt])
            retry_after = getattr(exc, "retry_after", None) if isinstance(exc, ApiError) else None
            if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and retry_after > wait:
                wait = min(float(retry_after), MAX_STARTUP_WAIT_S)
            attempt += 1
            print(
                f"The Super Market API is temporarily unavailable ({reason}); trying again in {wait:g} s "
                f"(attempt {attempt + 1} of {len(waits) + 1}; Ctrl-C to stop).",
                file=out,
                flush=True,
            )
            sleep(wait)


def build_live(
    settings: Any,
    args: Any,
    out: TextIO = sys.stderr,
    *,
    sleep: Callable[[float], None] = time.sleep,
    retry_waits: Optional[Sequence[float]] = None,
) -> Runtime:
    """A dashboard over the real API (needs Settings with an API key)."""
    from .attribution import Attributor
    from .client import SuperMarketClient
    from .news import NewsSearcher, default_providers
    from .store import TrackerStore
    from .tracker import Tracker

    client = SuperMarketClient.from_settings(settings)
    enforce_get_only(client)  # D20: GET only, whatever any component tries (the tracker's copy shares the guard)
    store: Any = None
    searcher: Any = None
    fair_values: Any = None
    watcher: Any = None
    try:
        options = simulation_options(args)
        fv_mode, sizing, regime = options["fair_value"], options["sizing"], options["regime"]
        # docs/OUTSIDE_MOVES.md §18.1: the outside-move watcher polls the fair-value providers' validated matches,
        # so it needs fair value "auto"; asked for without it, it is off and says so once
        moves_enabled = bool(options["moves"]) and fv_mode == "auto"
        moves_poll = _moves_poll_value(options["moves_poll_s"]) if moves_enabled else None
        if options["moves"] and not moves_enabled:
            print(MOVES_FV_NOTICE, file=out, flush=True)
        slug = getattr(args, "tournament", None) or settings.tournament
        data_dir = Path(settings.data_dir)
        print("Connecting to the Super Market API…", file=out, flush=True)
        context, store = _resolve_live_context(
            client,
            slug,
            bool(getattr(args, "public", False)),
            data_dir,
            out,
            sleep=sleep,
            waits=STARTUP_RETRY_WAITS_S if retry_waits is None else tuple(retry_waits),
            secrets=[settings.api_key],
        )
        if store is None:
            store = TrackerStore(data_dir / context.label / "tracker.sqlite3")
            _save_context(store, context)
        no_news = bool(getattr(args, "no_news", False))
        searcher = None if no_news else NewsSearcher(default_providers(), store=store)
        llm = bool(getattr(args, "llm", False)) or os.environ.get("SUPERMARKET_LLM", "").strip().lower() in ("1", "true", "yes")
        judge = _judge(llm, out)
        attributor = Attributor(client, context, store, searcher, judge=judge)
        interval = float(getattr(args, "interval", 30.0))
        holder: List[Any] = []
        if fv_mode != "off":
            from .fairvalue import OUTSIDE_READS_PER_MIN_WITH_MOVES, FairValueService, default_providers

            # with the watcher, each outside host gets 45 reads a minute (poll ~20/12 + refresh 5/3, §4.3)
            provider_kw: Dict[str, Any] = {"reads_per_min": OUTSIDE_READS_PER_MIN_WITH_MOVES} if moves_enabled else {}
            fair_values = FairValueService(
                data_dir / context.label, mode=fv_mode, providers=default_providers(fv_mode, **provider_kw),
                recorder=store.add_fair_values, refresh_recorder=store.add_fair_value_refresh,
                cup_mids=lambda: holder[0].cup_mids() if holder else {},
            )
        tracker_moves: Dict[str, Any] = {}
        if moves_enabled:
            from .moves import MOVES_ALERTS_FILE, MoveParams, OutsideMoveWatcher
            from .tracker import MOVES_READS_PER_MIN

            # providers=None: the FairValueService's own Polymarket/Kalshi providers (one host budget, one backoff)
            watcher = OutsideMoveWatcher(fair_values, persistence=store,
                                         alerts_path=data_dir / context.label / MOVES_ALERTS_FILE,
                                         params=MoveParams(poll_s=moves_poll))
            book_rpm = options["moves_book_reads_per_min"]
            tracker_moves = {"moves": watcher,
                             "moves_reads_per_min": int(book_rpm) if book_rpm is not None else MOVES_READS_PER_MIN}
        engine: Any = None
        if options["paper"]:
            from .paper import PaperEngine

            engine = PaperEngine(
                _paper_config(demo=False, sizing=sizing, regime=regime, all_collateral=options["all_collateral"],
                              start_capital=options["paper_capital"], reads_per_min=None, interval=interval,
                              target_hours=getattr(args, "hours", None)),
                persistence=store, code_version=pipeline.code_version(),
            )
        tracker = Tracker(client, context, store, interval=interval, attributor=attributor, fair_values=fair_values,
                          paper=engine, paper_reads_per_min=engine.config.reads_per_min if engine is not None else 20,
                          paper_reads_per_min_during_backfill=(
                              engine.config.reads_per_min_during_backfill if engine is not None else 6),
                          regime=regime, lease=True, **tracker_moves)
        holder.append(tracker)
        if fv_mode == "auto":
            print(FV_AUTO_NOTICE, file=out, flush=True)
        if watcher is not None:
            print(moves_live_notice(float(watcher.poll_s)), file=out, flush=True)
        projected = (tracker.status().get("read_budget") or {}).get("projected_per_min")
        if projected is not None:
            print(f"Projected Super Market reads: about {float(projected):.0f} per minute before the market list is read "
                  "(the status shows the live projection; the account allows 100 per minute across all keys).",
                  file=out, flush=True)
        app = DashboardApp(
            tracker,
            store,
            client,
            context,
            demo=False,
            interval=interval,
            secrets=[settings.api_key],
            news_enabled=not no_news,
            llm_enabled=judge is not None,
            fair_values=fair_values,
            regime=regime,
            sizing=sizing,
            moves=watcher,
        )
    except BaseException:  # including Ctrl-C while connecting: release what was opened
        for thing in (watcher, searcher, fair_values, store, client):
            close = getattr(thing, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    log.debug("closing %r after a failed start failed: %s", thing, exc)
        raise
    return Runtime(app, tracker, store, client, context, news=searcher, fair_values=fair_values, moves=watcher)


# The live Cup's matched outcomes when this was written (docs/OUTSIDE_MOVES.md §4.3): 231 Polymarket ids (50 per call)
# and 220 Kalshi tickers (100 per call); the fair-value refresh adds 5 and 3 calls a minute.
MOVES_LIVE_POLYMARKET_IDS = 231
MOVES_LIVE_KALSHI_TICKERS = 220


def moves_live_notice(poll_s: float) -> str:
    """The start-up line of a live dashboard with the outside-move watcher (§18.1), with the per-host reads a minute
    that poll period implies (§4.3: about 25 and 15 at 15 s). When the poll needs more than the limiter leaves above
    the fair-value refresh's headroom (36 of 45), a second sentence says some polls will be skipped (as ``moves``)."""
    from .fairvalue import OUTSIDE_READS_PER_MIN_WITH_MOVES
    from .moves import MOVES_POLL_HEADROOM

    poll = float(poll_s)
    pm = math.ceil(MOVES_LIVE_POLYMARKET_IDS / 50) * 60.0 / poll + 5
    kalshi = math.ceil(MOVES_LIVE_KALSHI_TICKERS / 100) * 60.0 / poll + 3
    text = (f"Outside-move alerts: Polymarket and Kalshi every {poll:g} s (GET only, about {pm:.0f} and {kalshi:.0f} "
            "reads a minute); use --no-moves to stop.")
    kept = MOVES_POLL_HEADROOM + 1
    if pm > OUTSIDE_READS_PER_MIN_WITH_MOVES - kept:
        text += (f" A {poll:g}-second poll needs about {pm:.0f} Polymarket reads a minute; this bot allows itself "
                 f"{OUTSIDE_READS_PER_MIN_WITH_MOVES} a minute per host and keeps {kept} of them for the fair-value "
                 "refresh, so some polls will be skipped.")
    return text


def _bind(
    app: DashboardApp, host: str, port: int, out: TextIO, allow_hosts: Sequence[str] = ()
) -> Optional[DashboardServer]:
    try:
        if allow_hosts:
            return make_server(app, host, port, allow_hosts=allow_hosts)
        return make_server(app, host, port)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or "in use" in str(exc).lower():
            print(
                f"error: port {port} is already in use (is the dashboard already running?). "
                f"Try another one, e.g. --port {port + 1 if port else 8766}, or --port 0 to pick a free port.",
                file=out,
                flush=True,
            )
        elif exc.errno in (errno.EADDRNOTAVAIL, errno.EACCES) or isinstance(exc, socket.gaierror):
            print(f"error: cannot listen on {host}:{port}: {exc}", file=out, flush=True)
        else:
            print(f"error: could not start the dashboard on {host}:{port}: {exc}", file=out, flush=True)
        return None


def _allow_host_option(args: Any) -> List[str]:
    """``--allow-host`` values (repeatable and/or comma-separated); [] when the CLI has no such option."""
    raw = getattr(args, "allow_host", None)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    names: List[str] = []
    for item in raw:
        for part in str(item or "").split(","):
            part = part.strip()
            if part and part not in names:
                names.append(part)
    return names


STOP_SIGNALS = ("SIGTERM", "SIGHUP")  # SIGHUP: the terminal window was closed (live-5)


def _install_sigterm(stopping: threading.Event) -> Any:
    """Make SIGTERM, and SIGHUP where it exists (closing the terminal window), stop the dashboard or a ``paper``
    run like Ctrl-C (also while it is still starting), so the tracker stops and releases its lease instead of
    dying with it held. Returns the old handlers ({signal number: handler}; None off the main thread)."""

    def _stop_signal(signum: int, frame: Any) -> None:
        if not stopping.is_set():
            stopping.set()
            raise KeyboardInterrupt

    try:
        import signal

        if threading.current_thread() is not threading.main_thread():
            return None
        previous: Dict[int, Any] = {}
        for name in STOP_SIGNALS:
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                if name == "SIGHUP" and signal.getsignal(signum) == signal.SIG_IGN:
                    continue  # started under nohup: closing the terminal must not stop it
                previous[int(signum)] = signal.signal(signum, _stop_signal)
            except (ValueError, OSError):
                continue
        return previous or None
    except (ImportError, ValueError, OSError, AttributeError):
        pass
    return None


def _restore_sigterm(previous: Any) -> None:
    if not previous:
        return
    try:
        import signal

        for signum, handler in dict(previous).items():
            try:
                signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                continue
    except (ImportError, ValueError, OSError, AttributeError):
        pass


def _say_safely(text: str, stream: TextIO) -> None:
    """Print on the way out: after SIGHUP the terminal is gone and a write fails (EIO); shutting down still must."""
    try:
        print(text, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def regime_option(args: Any) -> str:
    """``--regime``, else ``SUPERMARKET_SETTLEMENT_REGIME``, else "unknown" (the Cup's end rule is unknown, §13)."""
    from .models import REGIMES

    raw = getattr(args, "regime", None) or os.environ.get("SUPERMARKET_SETTLEMENT_REGIME", "").strip() or "unknown"
    value = str(raw).strip().lower()
    return value if value in REGIMES else "unknown"


def simulation_options(args: Any) -> Dict[str, Any]:
    """The dashboard's paper / fair-value / sizing options as ``build_demo`` keywords (§7.5)."""
    fv = "off" if getattr(args, "no_fair_value", False) else _fair_value_mode(getattr(args, "fair_value", None) or "auto")
    capital = getattr(args, "paper_capital", None)
    poll = getattr(args, "moves_poll", None)
    book_reads = getattr(args, "moves_book_reads", None)
    return {
        "paper": not bool(getattr(args, "no_paper", False)),
        "fair_value": fv,
        "regime": regime_option(args),
        "sizing": str(getattr(args, "sizing", None) or "conservative"),
        "all_collateral": bool(getattr(args, "all_collateral", False)),
        "paper_capital": float(capital) if capital else None,
        # docs/OUTSIDE_MOVES.md §18.1: a missing attribute means off (the `paper` parser has none of these options,
        # so `paper` never runs the watcher; old callers with a bare namespace keep their behaviour)
        "moves": not bool(getattr(args, "no_moves", True)),
        "moves_poll_s": float(poll) if poll is not None else None,
        "moves_book_reads_per_min": int(book_reads) if book_reads is not None else None,
    }


def run_dashboard(settings: Any, args: Any, out: Optional[TextIO] = None, err: Optional[TextIO] = None) -> int:
    """``python -m supermarket_bot dashboard``: build, serve until Ctrl-C, shut down cleanly."""
    out = out or sys.stdout
    err = err or sys.stderr
    from .bot import is_fatal
    from .config import ConfigError, Settings
    from .errors import SuperMarketError

    demo = bool(getattr(args, "demo", False))
    host = str(getattr(args, "host", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(args, "port", 8765))
    interval = float(getattr(args, "interval", 30.0))
    allow_hosts = _allow_host_option(args)
    if not (0 <= port <= 65535):
        print("error: --port must be between 0 and 65535", file=err)
        return 2
    if not math.isfinite(interval) or interval < 1:
        print("error: --interval must be at least 1 second", file=err)
        return 2
    moves_poll = getattr(args, "moves_poll", None)
    if moves_poll is not None:  # docs/OUTSIDE_MOVES.md §17.1 / §18.1
        try:
            _moves_poll_value(moves_poll)
        except (TypeError, ValueError):
            print(f"error: {MOVES_POLL_ERROR}", file=err)
            return 2
    if not demo and interval < 10:
        print("warning: intervals under 10 s use a lot of the 100 reads/minute budget", file=err)
    for name in allow_hosts:
        exact, names = DashboardServer._extra_hosts([name])
        if not exact and not names:
            print(f"warning: ignoring --allow-host {name!r}: not a host name (use NAME or NAME:PORT)", file=err)
    if not is_loopback_host(host):
        other_names = (
            "; add any other name it is reached by (such as a reverse proxy's) with --allow-host NAME"
            if hasattr(args, "allow_host")
            else ""
        )
        print(
            f"warning: listening on {host}, so any device that can reach this machine can open the dashboard "
            "(there is no password). It only answers to this machine's own addresses and names, which "
            f"blocks DNS-rebinding pages{other_names}. The dashboard is read-only and never shows the API key; "
            "leave out --host (127.0.0.1) unless another device needs it.",
            file=err,
        )

    stopping = threading.Event()
    previous = _install_sigterm(stopping)
    try:
        try:
            if demo:
                data_dir = Path(getattr(args, "data_dir", None) or "data")
                runtime = build_demo(
                    data_dir, interval, llm=bool(getattr(args, "llm", False)), news=not getattr(args, "no_news", False), out=err,
                    **simulation_options(args),
                )
            else:
                if settings is None:
                    env_file = getattr(args, "env_file", ".env")
                    overrides = {"data_dir": args.data_dir} if getattr(args, "data_dir", None) else {}
                    settings = Settings.load(env_file=Path(env_file) if env_file else None, **overrides)
                runtime = build_live(settings, args, out=err)
        except KeyboardInterrupt:
            print("\nStopped before the dashboard started.", file=err, flush=True)
            return 130
        except ConfigError as exc:
            print(f"error: {exc}", file=err)
            return 2
        except SuperMarketError as exc:
            print(f"error: {exc}", file=err)
            if is_fatal(exc):
                print("The key was rejected; fix SUPERMARKET_API_KEY and try again (or use --demo).", file=err)
            return 1
        except OSError as exc:
            print(f"error: {exc}", file=err)
            return 1
        return _serve(runtime, args, host, port, allow_hosts, stopping, out, err)
    finally:
        _restore_sigterm(previous)


def _serve(
    runtime: Runtime,
    args: Any,
    host: str,
    port: int,
    allow_hosts: Sequence[str],
    stopping: threading.Event,
    out: TextIO,
    err: TextIO,
) -> int:
    """Bind, start the tracker, serve until Ctrl-C/SIGTERM, then shut everything down."""
    code = 0
    try:
        server = _bind(runtime.app, host, port, err, allow_hosts)
    except KeyboardInterrupt:
        server = None
        code = 130
    if server is None:
        try:
            runtime.close()
        except KeyboardInterrupt:
            pass
        return code or 1
    from .tracker import TrackerBusy

    try:
        try:
            runtime.start()
            url = server.url
            if port == 0:
                print(f"Picked free port {server.port}.", file=out)
            print(f"Dashboard running at {url}  (Ctrl-C to stop)", file=out, flush=True)
            if is_wildcard_host(host):
                others = server.network_urls
                if others:
                    print("From other devices on your network: " + "  ".join(others), file=out, flush=True)
                else:
                    print(
                        f"From other devices on your network: http://<this machine's IP address>:{server.port}",
                        file=out,
                        flush=True,
                    )
            if runtime.app.demo:
                print("Demo mode: simulated market data, no API key used.", file=out, flush=True)
            if not getattr(args, "no_browser", False):
                threading.Thread(target=_open_browser, args=(url,), name="open-browser", daemon=True).start()
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            _say_safely("\nStopping the dashboard…", out)
        except TrackerBusy as exc:  # another live process tracks this store (D46): refuse, exit 2
            _say_safely(f"error: {exc}", err)
            code = 2
        finally:
            stopping.set()
            try:
                server.server_close()
            finally:
                runtime.close()
    except KeyboardInterrupt:  # a second Ctrl-C while shutting down: stop waiting for the workers
        _say_safely("Stopped without waiting for the background work to finish.", out)
        return 130
    return code


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:  # no browser available (e.g. a headless server)
        log.info("could not open a browser: %s", exc)
