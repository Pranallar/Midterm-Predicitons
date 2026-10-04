"""Local web dashboard: a standard-library HTTP server plus a single-page UI in ``web/``.

Run it with ``python -m supermarket_bot dashboard`` (add ``--demo`` to try it offline).
See docs/DESIGN.md (Web section).

* :class:`DashboardApp` turns the tracker's live view and the SQLite store into plain,
  JSON-safe dicts, one method per endpoint, so it can be tested without HTTP.
* :class:`DashboardServer` / :class:`DashboardHandler` serve those dicts as JSON and the
  static UI files, with local-only protections: Host-header allow-list (DNS rebinding),
  same-origin JSON-only POSTs, a strict Content-Security-Policy and no secrets in any
  response.
* :func:`run_dashboard` wires everything together for the CLI.

Nothing here places orders.
"""

from __future__ import annotations

import dataclasses
import errno
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

from . import analytics, strategy
from .books import Book, parse_time
from .models import HighBand, PricePoint, Surge

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
BOOK_TTL_S = 15.0
MAX_SURGES = 100
DEFAULT_INITIAL_BALANCE = 100_000.0
DEFAULT_CUP_END_ISO = "2026-11-04T17:00:00Z"

_EXCHANGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SURGE_ID_RE = re.compile(r"^[0-9]{1,12}$")
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
        out.extend(_downsample(chunk, budget))
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


def context_summary(raw: Any) -> Dict[str, Any]:
    """Normalise the tracker's ``view()["context"]`` (balance, leaderboard, constraints, overround)."""
    ctx = _plain(raw) if raw is not None else {}
    if not isinstance(ctx, Mapping):
        ctx = {}
    tournament = ctx.get("tournament") if isinstance(ctx.get("tournament"), Mapping) else {}
    board_raw = ctx.get("leaderboard")
    board_leader_value: Optional[float] = None
    if isinstance(board_raw, Mapping):
        # The tracker stores {"top": [...], "my_rank", "leader_value"}; the raw API uses "leaderboard".
        entries = (
            board_raw.get("top") or board_raw.get("leaderboard") or board_raw.get("entries") or board_raw.get("data") or []
        )
        board_rank = board_raw.get("myRank", board_raw.get("my_rank"))
        board_leader_value = _finite(board_raw.get("leader_value"))
    elif isinstance(board_raw, list):
        entries, board_rank = board_raw, None
    else:
        entries, board_rank = [], None
    leaders = []
    for entry in entries[:3] if isinstance(entries, list) else []:
        if isinstance(entry, Mapping):
            leaders.append(
                {
                    "rank": entry.get("rank"),
                    "username": entry.get("username") or entry.get("name"),
                    "pnl": _finite(entry.get("pnl")),
                    "value": _finite(_pick(entry, "value", "finalTotalValue", "totalValue")),
                }
            )
    balance = _finite(_pick(ctx, "balance", "my_balance", "myBalance"))
    if balance is None:
        balance = _finite(tournament.get("myBalance"))
    initial = _finite(_pick(ctx, "initial_balance", "initialBalance"))
    if initial is None:
        initial = _finite(tournament.get("initialBalance"))
    my_rank = _pick(ctx, "my_rank", "myRank")
    if my_rank is None:
        my_rank = board_rank
    my_rank = int(my_rank) if isinstance(my_rank, (int, float)) and not isinstance(my_rank, bool) else None
    leader_value = _finite(ctx.get("leader_value"))
    if leader_value is None:
        leader_value = board_leader_value
    if leader_value is None and leaders:
        top = leaders[0]
        if top["value"] is not None:
            leader_value = top["value"]
        elif top["pnl"] is not None:
            leader_value = (initial if initial is not None else DEFAULT_INITIAL_BALANCE) + top["pnl"]
    constraints = ctx.get("constraints")
    if isinstance(constraints, list):
        constraints = {"data": constraints, "violationsCount": len(constraints)}
    elif not isinstance(constraints, Mapping):
        constraints = None
    overround = _pick(ctx, "overround", "overround_rows", "markets", default=[])
    if not isinstance(overround, list):
        overround = []
    return {
        "balance": balance,
        "initial_balance": initial,
        "my_rank": my_rank,
        "leader_value": leader_value,
        "leaders": leaders,
        "constraints": constraints,
        "overround": [row for row in overround if isinstance(row, Mapping)],
        "tournament": dict(tournament),
        "updated_at": _epoch(_pick(ctx, "updated_at", "refreshed_at", "fetched_at")),
    }


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
    if total is None and done is not None and pending is not None:
        total = done + pending
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
            "last_snapshot_at": _epoch(_pick(st, "last_snapshot_at", "last_snapshot", "last_snapshot_ts", "last_cycle_at")),
            "errors": int(_finite(errors) or 0),
            "last_error": str(last_error) if last_error else None,
            "fatal_error": str(fatal) if fatal else None,
            "backfill": {
                "done": done,
                "total": total,
                "complete": bool(_pick(backfill, "complete", "finished_all", default=total is not None and done is not None and done >= total)),
            },
            "queues": dict(queues),
            "read_budget": dict(budget),
        }
    )
    return out


def _match_terms(text: str, terms: Sequence[str]) -> bool:
    return all(term in text for term in terms)


# --------------------------------------------------------------------------- the app


class DashboardApp:
    """The dashboard's data layer: one method per endpoint, each returning a plain dict.

    ``tracker`` needs ``view()`` (and ``request_analysis(surge_id)`` for re-analysis);
    ``store`` is a :class:`~supermarket_bot.store.TrackerStore`; ``client`` (optional) reads
    live order books; ``context`` is the trading :class:`~supermarket_bot.bot.Context`.
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
    ) -> None:
        self.tracker = tracker
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
        self._books: Dict[str, Tuple[float, Optional[Dict[str, Any]], Optional[str]]] = {}
        self._book_locks: Dict[str, threading.Lock] = {}
        self.view_error: Optional[str] = None

    # ------------------------------------------------------------------ inputs
    def _view(self) -> Dict[str, Any]:
        try:
            view = self.tracker.view()
        except Exception as exc:  # the tracker failing must not take the dashboard down
            self.view_error = self._short(exc)
            log.debug("tracker.view() failed", exc_info=True)
            view = None
        else:
            self.view_error = None
        return view if isinstance(view, Mapping) else {}

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

    def _surge_dict(self, raw: Any, rows_by_id: Mapping[str, Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        surge = _plain(raw)
        if not isinstance(surge, Mapping) or surge.get("exchange_id") is None:
            return None
        out = dict(surge)
        out["exchange_id"] = str(surge["exchange_id"])
        row = rows_by_id.get(out["exchange_id"])
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
        surges = [s for s in (self._surge_dict(item, by_id) for item in raw or []) if s is not None]
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
        budget = tracker["read_budget"]
        limiter = getattr(self.client, "read_limiter", None)
        if limiter is not None and _finite(budget.get("used")) is None:
            try:
                budget = {"used": limiter.used, "limit": limiter.limit, "window_s": getattr(limiter, "window", 60.0)}
            except Exception:
                budget = {}
            tracker["read_budget"] = budget
        bands = view.get("high_band") or []
        open_participants = 0
        for s in surges:
            att = s.get("attribution") or {}
            if s.get("status") == "open" and att.get("verdict") == "participants":
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
            "view_error": self.view_error,
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
            "features": {"analysis": self.analysis_enabled, "news": self.news_enabled, "llm": self.llm_enabled},
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
        row = next((r for r in rows if r["exchange_id"] == eid), None)
        info = self._info(eid)
        if row is None and info is None:
            return None
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
        surges = [s for s in (self._surge_dict(item, by_id) for item in stored) if s is not None]
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
        fetched_at, book, book_error = self._book(eid)
        band = row.get("high_band")
        return {
            "now": now,
            "exchange": row,
            "series": series,
            "series_window_s": SERIES_WINDOW_S,
            "surges": surges,
            "trades": trades,
            "book": book,
            "book_error": book_error,
            "book_fetched_at": fetched_at,
            "high_band": band,
        }

    def _book(self, eid: str) -> Tuple[Optional[float], Optional[Dict[str, Any]], Optional[str]]:
        if self.client is None:
            return None, None, "No live connection to the market."
        now = self.clock()
        with self._lock:
            cached = self._books.get(eid)
            if cached is not None and now - cached[0] < BOOK_TTL_S:
                return cached
            lock = self._book_locks.setdefault(eid, threading.Lock())
        if not lock.acquire(blocking=False):  # another request is already fetching this book
            return cached if cached is not None else (None, None, "Loading the order book…")
        try:
            tournament_id = getattr(self.context, "tournament_id", None)
            try:
                payload = self.client.get_exchange_orderbook(eid, depth=BOOK_DEPTH, tournament_id=tournament_id)
                result = (self.clock(), self._book_dict(payload), None)
            except Exception as exc:
                log.info("order book for %s failed: %s", eid, exc)
                result = (self.clock(), None, self._short(exc))
            with self._lock:
                self._books[eid] = result
                if len(self._books) > 256:  # keep the cache small
                    oldest = sorted(self._books.items(), key=lambda kv: kv[1][0])[: len(self._books) - 256]
                    for key, _ in oldest:
                        self._books.pop(key, None)
            return result
        finally:
            lock.release()

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
        with self._lock:
            cached = self._strategy_cache
            if cached is not None and now - cached[0] < cached[1]:
                return cached[2]
        with self._strategy_build:
            now = self.clock()
            with self._lock:
                cached = self._strategy_cache
                if cached is not None and now - cached[0] < cached[1]:
                    return cached[2]
            payload, ttl = self._build_strategy(now)
            with self._lock:
                self._strategy_cache = (now, ttl, payload)
            return payload

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
            infos = {e.exchange_id: e for e in self.store.exchanges()}
            latest: Dict[str, PricePoint] = {}
            for eid in infos:
                point = self.store.latest(eid)
                if point is not None:
                    latest[eid] = point
            surges = self.store.surges(since=now - 2 * DAY_S, limit=200)
            bands = []
            for raw in view.get("high_band") or []:
                if isinstance(raw, HighBand):
                    bands.append(raw)
                    continue
                band = _plain(raw)
                if isinstance(band, Mapping):
                    try:
                        bands.append(HighBand(**_known(HighBand, band)))
                    except TypeError:
                        log.debug("skipping malformed high band %r", band)
            report = strategy.build_report(
                now=now,
                surges=surges,
                bands=bands,
                latest=latest,
                infos=infos,
                balance=ctx["balance"],
                initial_balance=ctx["initial_balance"],
                leader_value=ctx["leader_value"],
                my_rank=ctx["my_rank"],
                cup_end=cup_end,
                constraints=ctx["constraints"],
                overround_rows=ctx["overround"],
                backtest=backtest,
            )
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
            if isinstance(opp, dict) and opp.get("exchange_id") is not None:
                opp["exchange_id"] = str(opp["exchange_id"])
        return data, STRATEGY_TTL_S

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
            result = strategy.backtest_fade(series, market_of=market_of)
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
        try:
            queued = bool(request(sid))
        except Exception as exc:
            log.warning("request_analysis(%s) failed: %s", sid, exc)
            return 500, {"queued": False, "error": f"Could not queue the analysis: {self._short(exc)}"}
        message = "Queued for analysis." if queued else "Already queued; the analysis will update shortly."
        return 200, {"queued": queued, "surge_id": sid, "message": message}


# --------------------------------------------------------------------------- HTTP


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR lets two servers share a port on Windows; only use it elsewhere.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 32

    def __init__(self, app: DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.bind_host = host
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), DashboardHandler)
        self.allowed_hosts = self._allowed_hosts()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def _allowed_hosts(self) -> frozenset:
        port = self.port
        names = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        host = self.bind_host.strip().lower()
        if host:
            names.add(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
        if port == 80:
            names |= {"127.0.0.1", "localhost", "[::1]"}
        return frozenset(names)

    @property
    def url(self) -> str:
        host = self.bind_host
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

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

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts

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
        origin = (self.headers.get("Origin") or "").strip().lower()
        expected = "http://" + (self.headers.get("Host") or "").strip().lower()
        if origin != expected:
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
        if body.strip():
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._error(400, "The request body is not valid JSON.")
            if not isinstance(data, dict):
                return self._error(400, "The request body must be a JSON object.")
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
            return self._json(200, getattr(app, name)())
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


def make_server(app: DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> DashboardServer:
    return DashboardServer(app, host, port)


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

    def start(self) -> None:
        self.tracker.start()

    def close(self) -> None:
        for step, fn in (
            ("tracker", lambda: self.tracker.stop()),
            ("news", lambda: self.news.close() if self.news is not None and hasattr(self.news, "close") else None),
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


def build_demo(
    data_dir: Path,
    interval: float = 30.0,
    *,
    llm: bool = False,
    news: bool = True,
    out: TextIO = sys.stderr,
    seed: int = 7,
) -> Runtime:
    """A dashboard over the simulated market (no API key, no network)."""
    from .attribution import Attributor
    from .bot import resolve_context
    from .client import SuperMarketClient
    from .demo import DEMO_SLUG, DemoMarket, DemoNewsProvider
    from .news import NewsSearcher
    from .store import TrackerStore
    from .tracker import Tracker

    market = DemoMarket(seed=seed)
    # The simulated API has no real budget, so the demo reads faster than a live key may.
    client = SuperMarketClient(
        api_key="demo", base_url="https://demo.invalid/api/v1", transport=market.transport(), reads_per_min=600
    )
    try:
        context = resolve_context(client, slug=DEMO_SLUG)
        db = Path(data_dir) / "demo" / "tracker.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        _remove_db(db)
        store = TrackerStore(db)
    except BaseException:
        client.close()
        raise
    searcher = NewsSearcher([DemoNewsProvider(market)], store=store) if news else None
    judge = _judge(llm, out)
    attributor = Attributor(client, context, store, searcher, judge=judge)
    tracker = Tracker(
        client,
        context,
        store,
        interval=interval,
        attributor=attributor,
        backfill_reads_per_min=240,
        analyze_reads_per_min=120,
    )
    app = DashboardApp(
        tracker, store, client, context, demo=True, interval=interval, news_enabled=news, llm_enabled=judge is not None
    )
    return Runtime(app, tracker, store, client, context, news=searcher, demo_market=market)


def build_live(settings: Any, args: Any, out: TextIO = sys.stderr) -> Runtime:
    """A dashboard over the real API (needs Settings with an API key)."""
    from .attribution import Attributor
    from .bot import resolve_context
    from .client import SuperMarketClient
    from .news import NewsSearcher, default_providers
    from .store import TrackerStore
    from .tracker import Tracker

    client = SuperMarketClient.from_settings(settings)
    try:
        slug = getattr(args, "tournament", None) or settings.tournament
        context = resolve_context(client, slug=slug, public=bool(getattr(args, "public", False)))
        store = TrackerStore(Path(settings.data_dir) / context.label / "tracker.sqlite3")
    except BaseException:
        client.close()
        raise
    no_news = bool(getattr(args, "no_news", False))
    searcher = None if no_news else NewsSearcher(default_providers(), store=store)
    llm = bool(getattr(args, "llm", False)) or os.environ.get("SUPERMARKET_LLM", "").strip().lower() in ("1", "true", "yes")
    judge = _judge(llm, out)
    attributor = Attributor(client, context, store, searcher, judge=judge)
    interval = float(getattr(args, "interval", 30.0))
    tracker = Tracker(client, context, store, interval=interval, attributor=attributor)
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
    )
    return Runtime(app, tracker, store, client, context, news=searcher)


def _bind(app: DashboardApp, host: str, port: int, out: TextIO) -> Optional[DashboardServer]:
    try:
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
    if not (0 <= port <= 65535):
        print("error: --port must be between 0 and 65535", file=err)
        return 2
    if not math.isfinite(interval) or interval < 1:
        print("error: --interval must be at least 1 second", file=err)
        return 2
    if not demo and interval < 10:
        print("warning: intervals under 10 s use a lot of the 100 reads/minute budget", file=err)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: listening on {host}; anyone who can reach this address can see the dashboard", file=err)

    try:
        if demo:
            data_dir = Path(getattr(args, "data_dir", None) or "data")
            runtime = build_demo(data_dir, interval, llm=bool(getattr(args, "llm", False)), news=not getattr(args, "no_news", False), out=err)
        else:
            if settings is None:
                env_file = getattr(args, "env_file", ".env")
                overrides = {"data_dir": args.data_dir} if getattr(args, "data_dir", None) else {}
                settings = Settings.load(env_file=Path(env_file) if env_file else None, **overrides)
            runtime = build_live(settings, args, out=err)
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

    server = _bind(runtime.app, host, port, err)
    if server is None:
        runtime.close()
        return 1
    stopping = threading.Event()

    def _sigterm(signum: int, frame: Any) -> None:
        if not stopping.is_set():
            stopping.set()
            raise KeyboardInterrupt

    previous = None
    try:
        import signal

        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, _sigterm)
    except (ImportError, ValueError, OSError, AttributeError):
        previous = None

    try:
        runtime.start()
        url = server.url
        if port == 0:
            print(f"Picked free port {server.port}.", file=out)
        print(f"Dashboard running at {url}  (Ctrl-C to stop)", file=out, flush=True)
        if runtime.app.demo:
            print("Demo mode: simulated market data, no API key used.", file=out, flush=True)
        if not getattr(args, "no_browser", False):
            threading.Thread(target=_open_browser, args=(url,), name="open-browser", daemon=True).start()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping the dashboard…", file=out, flush=True)
    finally:
        stopping.set()
        try:
            server.server_close()
        finally:
            runtime.close()
            if previous is not None:
                try:
                    import signal

                    signal.signal(signal.SIGTERM, previous)
                except (ValueError, OSError):
                    pass
    return 0


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:  # no browser available (e.g. a headless server)
        log.info("could not open a browser: %s", exc)
