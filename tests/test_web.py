"""Tests for supermarket_bot/web.py: the dashboard's data layer, its HTTP server and ``run_dashboard``.

Everything is offline and deterministic:

* ``pipe`` (module scope) is the real demo pipeline from :func:`web.build_demo`, built with a
  frozen, injected clock so the simulated market, tracker, attributor, news search and app all
  agree on the time and every run sees the same prices. The tracker is driven synchronously
  (``run_once`` / ``backfill_step`` / ``analyze_pending``); no background threads.
* Edge cases use a :class:`FakeTracker` (a canned ``view()``) over an in-memory ``TrackerStore``.
* HTTP tests run a real ``DashboardServer`` on port 0 in a thread and talk to it with
  ``http.client`` / raw sockets on 127.0.0.1 only (no proxies, no external hosts).
* Static-file tests serve a temporary web root, so they do not depend on the UI files.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import http.client
import io
import json
import math
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

import httpx
import pytest

from conftest import error_body, market_page
from conftest import market as api_market
from conftest import price as api_price

import supermarket_bot.attribution as attribution_mod
import supermarket_bot.demo as demo_mod
import supermarket_bot.news as news_mod
import supermarket_bot.strategy as strategy_mod
import supermarket_bot.tracker as tracker_mod
import supermarket_bot.web as web
from supermarket_bot.bot import Context
from supermarket_bot.cli import build_parser
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import Settings
from supermarket_bot.errors import ApiError
from supermarket_bot.models import Attribution, PricePoint, StrategyReport, Surge
from supermarket_bot.ratelimit import SlidingWindowLimiter
from supermarket_bot.store import TrackerStore

T0 = 1_791_129_600.0  # 2026-10-04T16:00:00Z: demo start of the deterministic pipeline
CUP_END_TS = 1_793_811_600.0  # 2026-11-04T17:00:00Z, the Cup's end
NOW = 1_791_000_000.0  # data time for FakeTracker tests
FAKE_KEY = "ace_live_SECRET_9f8e7d6c5b4a3210"
OUTSIDE_MARK = "TOP-SECRET-OUTSIDE-THE-WEB-ROOT"

ROW_KEYS = {
    "exchange_id", "market_id", "title", "option", "settlement_date", "mark", "last", "bid", "ask", "spread",
    "change_5m", "change_1h", "change_24h", "sparkline", "high_band", "surge_id",
}
STATUS_KEYS = {
    "ok", "demo", "read_only", "now", "started_at", "interval", "tournament", "tracker", "view_error",
    "account", "cup_end", "days_left", "features", "counts", "problems", "detection", "view_updated_at",
}
COUNT_KEYS = {"outcomes", "markets", "surges", "surges_last_hour", "open_participant_surges", "high_band", "violations"}
ACCOUNT_KEYS = {"balance", "initial_balance", "my_rank", "leader_value", "leaders", "updated_at"}
SURGE_KEYS = {f.name for f in fields(Surge)} | {"title", "option"}
BAND_KEYS = {
    "exchange_id", "market_id", "side", "favorite_price", "time_in_band", "stable", "title", "option",
    "settlement_date", "settlement_ts", "settles_before_cup_end", "entry_price", "entry_is_estimate",
    "payout_per_share", "return_pct",
}
EXCHANGE_KEYS = {
    "now", "exchange", "series", "series_window_s", "surges", "trades", "book", "book_error", "book_fetched_at", "high_band",
    "book_pending", "book_stale", "market_open",
}
STRATEGY_KEYS = {f.name for f in fields(StrategyReport)} | {"now", "backtest_status", "backtest_error", "available"}
GET_ENDPOINTS = (
    "/api/health", "/api/status", "/api/markets", "/api/markets?q=arizona", "/api/surges", "/api/highband",
    "/api/exchange/9001", "/api/exchange/9002", "/api/strategy",
)

# Demo scenario ids (docs/DESIGN.md, Demo section; seed 7).
NEWS_EID, PARTICIPANT_EID = "9001", "9002"
BAND_BEFORE_CUP_END = {"9007", "9008"}  # California Governor, Wyoming Senate: settle on election night
BAND_AFTER_CUP_END = {"9009", "9010"}  # New York Governor, Libertarian House seat: settle on certification
ARIZONA = {"9011", "9012", "9013"}


# --------------------------------------------------------------------------- helpers


class DemoClock:
    """One fake time source shared by the demo market, tracker, attributor, news and app."""

    def __init__(self, start: float) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _with_defaults(factory: Callable[..., Any], **defaults: Any) -> Callable[..., Any]:
    """``factory`` with extra keyword defaults (explicit keywords from the caller still win)."""

    @functools.wraps(factory)
    def make(*args: Any, **kwargs: Any) -> Any:
        return factory(*args, **{**defaults, **kwargs})

    return make


def warm_up(tracker: Any) -> None:
    """First cycle, the full candle backfill, a second cycle (detects the scripted surges), attribution."""
    tracker.run_once()
    for _ in range(100):
        if not tracker.backfill_step(50):
            break
    tracker.run_once()
    tracker.analyze_pending(20)


def exchange_with_book(app: web.DashboardApp, eid: str) -> Optional[Dict[str, Any]]:
    """The exchange detail once its background order-book fetch has finished."""
    app.exchange(eid)  # starts the fetch (the detail itself never waits for the book)
    assert app.wait_books(10)
    return app.exchange(eid)


def dumps(value: Any) -> bytes:
    """What the server would send (raises if the payload is not JSON-safe)."""
    return web.encode_json(value)


def assert_json_safe(payload: Any) -> Any:
    body = dumps(payload)
    text = body.decode("utf-8")
    assert "NaN" not in text and "Infinity" not in text
    return json.loads(text)


def iso_epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def finite_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


@dataclass
class Resp:
    status: int
    headers: Dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HTTP:
    """Tiny ``http.client`` wrapper for a server on 127.0.0.1 (never uses proxies)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://127.0.0.1:{port}"

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None) -> Resp:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            resp = conn.getresponse()
            data = resp.read()
            return Resp(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        finally:
            conn.close()

    def get(self, path: str, headers: Optional[Mapping[str, str]] = None) -> Resp:
        return self.request("GET", path, headers=headers)

    def post(self, path: str, body: Optional[bytes] = b"{}", **headers: Optional[str]) -> Resp:
        hdrs: Dict[str, Optional[str]] = {"Content-Type": "application/json", "Origin": self.origin}
        hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
        return self.request("POST", path, body=body, headers={k: v for k, v in hdrs.items() if v is not None})


def raw_request(port: int, data: bytes, timeout: float = 10.0) -> Resp:
    """Send raw bytes (malformed requests, missing Host…) and parse the reply."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    match = re.match(r"HTTP/\d\.\d (\d{3})", lines[0]) if lines else None
    status = int(match.group(1)) if match else 0  # 0: no status line at all (an HTTP/0.9-style reply)
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return Resp(status, headers, body)


def assert_security_headers(resp: Resp) -> None:
    for name, value in web.SECURITY_HEADERS:
        assert resp.headers.get(name.lower()) == value, f"{name} missing or wrong on HTTP {resp.status}"


def assert_json_error(resp: Resp, status: int) -> Dict[str, Any]:
    assert resp.status == status, resp.body[:300]
    assert resp.headers.get("content-type") == web.JSON_TYPE
    assert_security_headers(resp)
    data = resp.json()
    assert isinstance(data.get("error"), str) and data["error"]
    return data


@contextlib.contextmanager
def serve(app: web.DashboardApp, host: str = "127.0.0.1") -> Iterator[web.DashboardServer]:
    srv = web.make_server(app, host, 0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, name="test-dashboard", daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(5)


class FakeTracker:
    """A tracker stand-in: ``view()`` returns (a copy of) a canned dict, or raises."""

    def __init__(self, view: Any = None, *, fail: Optional[BaseException] = None, queued: Any = True) -> None:
        self._view = {} if view is None else view
        self.fail = fail
        self.queued = queued
        self.requests: List[int] = []

    def view(self) -> Any:
        if self.fail is not None:
            raise self.fail
        return copy.deepcopy(self._view)

    def request_analysis(self, surge_id: int) -> bool:
        self.requests.append(surge_id)
        if isinstance(self.queued, BaseException):
            raise self.queued
        return self.queued


class NoReanalysis:
    """A tracker without ``request_analysis``."""

    def view(self) -> Dict[str, Any]:
        return {}


def make_surge(eid: str, mid: str, **kw: Any) -> Surge:
    base: Dict[str, Any] = dict(
        exchange_id=eid, market_id=mid, window="1h", window_s=3600.0, start_ts=NOW - 3000, end_ts=NOW - 60,
        start_price=0.40, end_price=0.55, change=0.15, direction="up", peak_price=0.56, detected_at=NOW - 60,
    )
    base.update(kw)
    return Surge(**base)


@pytest.fixture
def store() -> Iterator[TrackerStore]:
    """An in-memory store with two markets (one binary, one multi-outcome) and a few ticks."""
    st = TrackerStore(":memory:")
    st.upsert_markets(
        [
            api_market("m1", "Will Democrats win the Maine Senate race?", [("e1", "YES", 0.44)]),
            api_market("m2", "Who will win the Springfield mayoral race?", [("e2a", "Alice Smith", 0.6), ("e2b", "Bob Jones", 0.4)]),
        ]
    )
    for i in range(5):
        st.add_ticks(
            NOW - 600 + i * 120,
            [
                {"exchange_id": "e1", "latest_price": 0.44, "best_bid": 0.43, "best_ask": 0.45},
                {"exchange_id": "e2a", "latest_price": 0.6, "best_bid": 0.59, "best_ask": 0.61},
            ],
        )
    yield st
    st.close()


@pytest.fixture
def make_app(store: TrackerStore) -> Iterator[Callable[..., web.DashboardApp]]:
    apps: List[web.DashboardApp] = []

    def factory(tracker: Any = None, **kwargs: Any) -> web.DashboardApp:
        kwargs.setdefault("clock", lambda: NOW)
        app = web.DashboardApp(tracker if tracker is not None else FakeTracker(), kwargs.pop("store", store), **kwargs)
        apps.append(app)
        return app

    yield factory
    for app in apps:  # the strategy endpoint starts a backtest thread; let it finish before the store closes
        app.wait_backtest(10)


# --------------------------------------------------------------------------- the deterministic demo pipeline


def build_frozen_demo(data_dir: Path, t0: float) -> Tuple[web.Runtime, DemoClock]:
    """:func:`web.build_demo` with one fake clock injected into every time-aware piece."""
    clock = DemoClock(t0)
    mp = pytest.MonkeyPatch()
    try:
        # build_demo imports these at call time, so the real wiring runs with the shared fake clock.
        mp.setattr(demo_mod, "DemoMarket", _with_defaults(demo_mod.DemoMarket, now=t0, clock=clock))
        mp.setattr(tracker_mod, "Tracker", _with_defaults(tracker_mod.Tracker, clock=clock))
        mp.setattr(attribution_mod, "Attributor", _with_defaults(attribution_mod.Attributor, clock=clock))
        mp.setattr(news_mod, "NewsSearcher", _with_defaults(news_mod.NewsSearcher, clock=clock))
        mp.setattr(web, "DashboardApp", _with_defaults(web.DashboardApp, clock=clock))
        runtime = web.build_demo(data_dir, 30.0, out=io.StringIO())
    finally:
        mp.undo()
    return runtime, clock


@pytest.fixture(scope="module")
def pipe(tmp_path_factory: Any) -> Iterator[SimpleNamespace]:
    data_dir = tmp_path_factory.mktemp("dashboard-data")
    runtime, clock = build_frozen_demo(data_dir, T0)
    try:
        warm_up(runtime.tracker)
        clock.advance(30)
        runtime.tracker.run_once()
        yield SimpleNamespace(
            runtime=runtime,
            app=runtime.app,
            tracker=runtime.tracker,
            store=runtime.store,
            client=runtime.client,
            market=runtime.demo_market,
            clock=clock,
            data_dir=data_dir,
        )
    finally:
        runtime.app.wait_backtest(10)
        runtime.close()


@pytest.fixture(scope="module")
def surges_by_eid(pipe: SimpleNamespace) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for s in pipe.app.surges()["surges"]:
        out.setdefault(s["exchange_id"], s)
    return out


@pytest.fixture(scope="module")
def server(pipe: SimpleNamespace) -> Iterator[web.DashboardServer]:
    with serve(pipe.app) as srv:
        yield srv


@pytest.fixture
def client(server: web.DashboardServer) -> HTTP:
    return HTTP(server.port)


@pytest.fixture(scope="module")
def static_tree(tmp_path_factory: Any) -> SimpleNamespace:
    base = tmp_path_factory.mktemp("static")
    root = base / "web"
    (root / "sub").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Dashboard</title><p>café</p>", encoding="utf-8")
    (root / "app.js").write_text("console.log('dashboard');\n", encoding="utf-8")
    (root / "styles.css").write_text("body { color: black; }\n", encoding="utf-8")
    (root / "sub" / "page.html").write_text("<p>sub page</p>", encoding="utf-8")
    (root / ".secret.html").write_text(OUTSIDE_MARK, encoding="utf-8")  # dotfile inside the root
    (root / "notes.py").write_text(OUTSIDE_MARK, encoding="utf-8")  # unknown file type
    outside = base / "outside.html"
    outside.write_text(OUTSIDE_MARK, encoding="utf-8")
    (base / "web-backup").mkdir()
    (base / "web-backup" / "index.html").write_text(OUTSIDE_MARK, encoding="utf-8")  # sibling with a shared prefix
    symlink = False
    try:
        (root / "link.html").symlink_to(outside)
        symlink = True
    except (OSError, NotImplementedError):
        pass
    return SimpleNamespace(root=root, outside=outside, symlink=symlink)


@pytest.fixture
def static_root(static_tree: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    original = web.resolve_static
    monkeypatch.setattr(web, "resolve_static", lambda path, root=static_tree.root: original(path, root))
    return static_tree


# --------------------------------------------------------------------------- DashboardApp on the demo pipeline


def test_health(pipe: SimpleNamespace) -> None:
    assert pipe.app.health() == {"ok": True, "now": pipe.clock.now}


def test_status_shape_and_counts(pipe: SimpleNamespace) -> None:
    status = pipe.app.status()
    assert STATUS_KEYS <= set(status)
    assert status["ok"] is True and status["demo"] is True and status["read_only"] is True
    assert status["now"] == pipe.clock.now
    assert status["interval"] == 30.0
    assert status["view_error"] is None
    assert status["tournament"] == {
        "name": demo_mod.DEMO_TOURNAMENT_NAME,
        "slug": demo_mod.DEMO_SLUG,
        "currency": "SUSQies",
        "status": "active",
        "label": demo_mod.DEMO_SLUG,
    }
    assert status["features"] == {"analysis": True, "news": True, "llm": False}

    counts = status["counts"]
    assert COUNT_KEYS <= set(counts)
    assert counts["outcomes"] == 22  # 23 demo outcomes, one market already settled
    assert counts["markets"] == 14
    assert counts["surges"] == 2 and counts["surges_last_hour"] == 2
    assert counts["open_participant_surges"] == 1
    assert counts["high_band"] == len(pipe.app.highband()["bands"])
    assert counts["violations"] == 1

    tracker = status["tracker"]
    assert tracker["running"] is False  # driven synchronously in this module
    assert tracker["cycles"] == 3
    assert tracker["fatal_error"] is None and tracker["errors"] == 0 and tracker["last_error"] is None
    assert tracker["last_snapshot_at"] == T0 + 30  # the fixture's last cycle
    backfill = tracker["backfill"]
    assert (backfill["done"], backfill["total"], backfill["complete"]) == (44.0, 44.0, True)  # 22 outcomes x (1h, 5m)
    assert isinstance(tracker["queues"], dict)
    budget = tracker["read_budget"]
    assert isinstance(budget, dict) and budget

    assert status["cup_end"] == CUP_END_TS
    assert status["days_left"] == pytest.approx((CUP_END_TS - pipe.clock.now) / 86400.0)
    assert_json_safe(status)


def test_status_account_and_leaders(pipe: SimpleNamespace) -> None:
    account = pipe.app.status()["account"]
    assert ACCOUNT_KEYS <= set(account)
    tournament = pipe.client.get_tournament(demo_mod.DEMO_SLUG)
    assert account["balance"] == tournament["myBalance"]
    assert account["initial_balance"] == 100_000
    board = pipe.tracker.view()["context"]["leaderboard"]
    assert account["my_rank"] == board["my_rank"]
    assert isinstance(account["my_rank"], int)
    # The tracker stores the top 3 as {"top": [...], "leader_value": ...}; the dashboard must read them.
    assert account["leader_value"] == pytest.approx(board["leader_value"])
    assert [e["username"] for e in account["leaders"]] == [e["username"] for e in board["top"]]
    assert len(account["leaders"]) == 3
    assert account["leaders"][0]["rank"] == 1
    assert account["updated_at"] is not None


def test_markets_rows(pipe: SimpleNamespace) -> None:
    data = pipe.app.markets()
    assert data["total"] == data["count"] == 22 and data["q"] == "" and data["now"] == pipe.clock.now
    rows = data["rows"]
    assert [r["exchange_id"] for r in rows][:3] == ["9001", "9002", "9003"]
    titles = {ex.id: ex.market.title for ex in pipe.market.exchanges}
    options = {ex.id: ex.option for ex in pipe.market.exchanges}
    for row in rows:
        assert ROW_KEYS <= set(row)
        assert isinstance(row["exchange_id"], str) and isinstance(row["market_id"], str)
        assert row["title"] == titles[row["exchange_id"]]
        assert row["option"] == options[row["exchange_id"]]
        for key in ("mark", "last", "bid", "ask", "spread", "change_5m", "change_1h", "change_24h"):
            assert finite_or_none(row[key]), (row["exchange_id"], key, row[key])
        assert 0.0 <= row["mark"] <= 1.0
        assert row["bid"] < row["ask"]
        assert row["spread"] == pytest.approx(row["ask"] - row["bid"])
        spark = row["sparkline"]
        assert 2 <= len(spark) <= 96
        assert all(isinstance(p, list) and len(p) == 2 and 0.0 <= p[1] <= 1.0 for p in spark)
        assert [p[0] for p in spark] == sorted(p[0] for p in spark)
        assert spark[-1][0] <= pipe.clock.now and spark[0][0] >= pipe.clock.now - 86400 - 1
    by_id = {r["exchange_id"]: r for r in rows}
    surges = {s["exchange_id"]: s["id"] for s in pipe.app.surges()["surges"]}
    assert by_id[NEWS_EID]["surge_id"] == surges[NEWS_EID]
    assert by_id[PARTICIPANT_EID]["surge_id"] == surges[PARTICIPANT_EID]
    assert by_id["9004"]["surge_id"] is None
    assert isinstance(by_id["9007"]["high_band"], dict) and by_id["9001"]["high_band"] is None
    assert by_id[NEWS_EID]["change_1h"] >= 0.10  # the scripted +0.14 news move
    assert_json_safe(data)


def _search_text(row: Mapping[str, Any]) -> str:
    return " ".join(str(x) for x in (row["title"], row["option"], row["exchange_id"], row["market_id"]) if x).lower()


@pytest.mark.parametrize(
    "q, expected",
    [
        ("arizona", ARIZONA),
        ("ARIZONA   Governor", ARIZONA),  # case-insensitive, extra spaces, every term must match
        ("arizona senate", set()),
        ("pennsylvania", {"9001"}),
        ("9002", {"9002"}),  # exchange id
        ("311", ARIZONA),  # market id
        ("democratic nominee", {"9011"}),  # option label
        ("<script>alert(1)</script>", set()),
        ("café — \U0001f600", set()),
    ],
)
def test_markets_filter(pipe: SimpleNamespace, q: str, expected: set) -> None:
    data = pipe.app.markets(q)
    assert data["q"] == q and data["total"] == 22
    assert {r["exchange_id"] for r in data["rows"]} == expected
    assert data["count"] == len(data["rows"])


@pytest.mark.parametrize("q", [None, "", "   "])
def test_markets_blank_query_returns_everything(pipe: SimpleNamespace, q: Optional[str]) -> None:
    data = pipe.app.markets(q)
    assert data["count"] == data["total"] == 22


def test_markets_filter_matches_title_option_and_ids(pipe: SimpleNamespace) -> None:
    rows = pipe.app.markets()["rows"]
    for q in ("senate", "will democrats", "house 9", "republicans senate"):
        terms = q.split()
        expected = {r["exchange_id"] for r in rows if all(t in _search_text(r) for t in terms)}
        assert expected, q
        assert {r["exchange_id"] for r in pipe.app.markets(q)["rows"]} == expected


def test_surges_newest_first_with_attribution(pipe: SimpleNamespace, surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    data = pipe.app.surges()
    surges = data["surges"]
    assert data["count"] == len(surges) == 2
    assert {s["exchange_id"] for s in surges} == {NEWS_EID, PARTICIPANT_EID}
    keys = [(s["detected_at"], s["id"]) for s in surges]
    assert keys == sorted(keys, reverse=True)
    for s in surges:
        assert SURGE_KEYS <= set(s)
        assert s["title"] and s["option"] == "YES"
        assert s["direction"] == "up" and s["status"] == "open"
        assert s["change"] > 0 and s["peak_price"] >= s["start_price"]
        att = s["attribution"]
        assert att is not None and all(isinstance(r, str) for r in att["reasons"])
        assert 0.0 <= att["confidence"] <= 1.0 and 0.0 <= att["reversion_odds"] <= 1.0
    assert_json_safe(data)


def test_news_surge_has_headlines(surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    surge = surges_by_eid[NEWS_EID]
    assert surge["title"] == "Will Republicans keep control of the Pennsylvania Senate?"
    att = surge["attribution"]
    assert att["verdict"] == "news"
    assert att["reversion_odds"] == pytest.approx(0.2)
    articles = att["articles"]
    assert len(articles) >= 2
    for article in articles:
        assert "Pennsylvania" in article["title"]
        assert article["url"].startswith("https://example.com/news/")
        assert article["published_at"] <= surge["end_ts"]
        assert article["source"]


def test_participant_surge_has_crowd_evidence(surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    surge = surges_by_eid[PARTICIPANT_EID]
    assert surge["title"] == "Will Republicans win Ohio House District 9?"
    att = surge["attribution"]
    assert att["verdict"] == "participants"
    assert att["articles"] == []
    assert att["flow"]["n_trades"] <= 5 and att["flow"]["max_trade_size"] == 520
    assert att["book_depth"] < 500
    assert any("trader identity" in r for r in att["reasons"])
    assert att["reversion_odds"] > 0.5


def test_highband(pipe: SimpleNamespace) -> None:
    data = pipe.app.highband()
    bands = data["bands"]
    assert data["count"] == len(bands) and data["cup_end"] == CUP_END_TS
    ids = {b["exchange_id"] for b in bands}
    assert BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END <= ids
    assert 4 <= len(bands) <= 5
    favs = [b["favorite_price"] for b in bands]
    assert favs == sorted(favs, reverse=True)
    rows = {r["exchange_id"]: r for r in pipe.app.markets()["rows"]}
    for band in bands:
        assert BAND_KEYS <= set(band)
        assert band["title"] == rows[band["exchange_id"]]["title"]
        assert band["favorite_price"] >= 0.95 and band["time_in_band"] >= 0.8
        row = rows[band["exchange_id"]]
        expected_entry = row["ask"] if band["side"] == "YES" else round(1.0 - row["bid"], 6)
        assert band["entry_is_estimate"] is False
        assert band["entry_price"] == pytest.approx(expected_entry)
        assert band["payout_per_share"] == pytest.approx(1.0 - band["entry_price"])
        assert band["return_pct"] == pytest.approx((1.0 - band["entry_price"]) / band["entry_price"] * 100.0, abs=1e-3)
        assert band["settlement_ts"] == pytest.approx(iso_epoch(band["settlement_date"]))
    by_id = {b["exchange_id"]: b for b in bands}
    for eid in BAND_BEFORE_CUP_END:
        assert by_id[eid]["settles_before_cup_end"] is True
    for eid in BAND_AFTER_CUP_END:
        assert by_id[eid]["settles_before_cup_end"] is False
    assert by_id["9010"]["side"] == "NO"  # the Libertarian long shot: NO is the favourite
    assert by_id["9007"]["side"] == "YES"
    assert_json_safe(data)


def test_exchange_detail(pipe: SimpleNamespace, surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    data = exchange_with_book(pipe.app, PARTICIPANT_EID)
    assert data is not None and EXCHANGE_KEYS <= set(data)
    now = pipe.clock.now
    assert data["now"] == now and data["series_window_s"] == 7 * 86400
    row = next(r for r in pipe.app.markets()["rows"] if r["exchange_id"] == PARTICIPANT_EID)
    assert data["exchange"] == row

    series = data["series"]
    assert 10 < len(series) <= web.MAX_SERIES_POINTS
    assert [p[0] for p in series] == sorted(p[0] for p in series)
    assert all(len(p) == 3 and p[2] in ("tick", "candle") and 0.0 <= p[1] <= 1.0 for p in series)
    assert series[0][0] <= now - 6 * 86400 and series[-1][0] <= now
    assert series[-1][2] == "tick"

    assert [s["id"] for s in data["surges"]] == [surges_by_eid[PARTICIPANT_EID]["id"]]
    assert data["surges"][0]["attribution"]["verdict"] == "participants"

    trades = data["trades"]  # stored by the attribution's trade-tape read
    assert 2 <= len(trades) <= web.MAX_TRADES
    assert [t["ts"] for t in trades] == sorted((t["ts"] for t in trades), reverse=True)
    assert {(t["size"], t["side"]) for t in trades} >= {(520.0, "YES"), (300.0, "YES")}
    assert all(set(t) == {"id", "ts", "price", "size", "side"} for t in trades)

    book = data["book"]
    assert data["book_error"] is None and data["book_fetched_at"] <= now
    assert data["book_pending"] is False and data["book_stale"] is False and data["market_open"] is True
    assert set(book) >= {"bids", "asks", "best_bid", "best_ask", "spread", "mid", "sequence"}
    bids, asks = [lv["price"] for lv in book["bids"]], [lv["price"] for lv in book["asks"]]
    assert bids == sorted(bids, reverse=True) and asks == sorted(asks)
    assert 1 <= len(bids) <= web.BOOK_DEPTH and 1 <= len(asks) <= web.BOOK_DEPTH
    assert book["best_bid"] == bids[0] and book["best_ask"] == asks[0] and bids[0] < asks[0]
    assert book["mid"] == pytest.approx((bids[0] + asks[0]) / 2)
    assert all(lv["quantity"] > 0 for lv in book["bids"] + book["asks"])
    assert data["high_band"] is None
    assert_json_safe(data)


def test_exchange_detail_of_a_high_band_outcome(pipe: SimpleNamespace) -> None:
    data = pipe.app.exchange("9007")
    assert data is not None
    assert data["high_band"]["side"] == "YES" and data["high_band"]["favorite_price"] >= 0.95
    assert data["surges"] == []


def test_exchange_book_is_cached(pipe: SimpleNamespace) -> None:
    first = exchange_with_book(pipe.app, "9004")
    sent = pipe.client.requests_sent
    second = pipe.app.exchange("9004")
    assert pipe.client.requests_sent == sent  # served from the 15 s cache
    assert second["book"] == first["book"] and second["book_fetched_at"] == first["book_fetched_at"]


@pytest.mark.parametrize("eid", ["99999", "abc", "", "../etc/passwd", "9001/extra", "9001 ", "x" * 65, "%39%30%30%31", "9001\x00"])
def test_exchange_unknown_is_none(pipe: SimpleNamespace, eid: str) -> None:
    assert pipe.app.exchange(eid) is None


def test_strategy_report(pipe: SimpleNamespace, surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    pipe.app.wait_backtest(10)
    report = pipe.app.strategy()
    assert STRATEGY_KEYS <= set(report)
    assert report["available"] is True and report["now"] == pipe.clock.now
    assert report["risk_mode"] in ("protect", "balanced", "aggressive")
    assert isinstance(report["headline"], str) and report["headline"]
    assert 4 <= len(report["principles"]) <= 6
    assert "read-only" in report["disclaimer"].lower()
    opps = report["opportunities"]
    assert 1 <= len(opps) <= 50
    scores = [o["score"] for o in opps]
    assert scores == sorted(scores, reverse=True)
    for opp in opps:
        assert opp["kind"] in ("fade", "carry", "arbitrage", "watch")
        assert opp["side"] in ("yes", "no")
        assert opp["exchange_id"] is None or isinstance(opp["exchange_id"], str)
        assert isinstance(opp["suggested_shares"], int) and opp["suggested_shares"] >= 0
        assert opp["suggested_cost"] <= report["balance"] * 0.08 + 1e-6  # max position size
    fades = [o for o in opps if o["kind"] == "fade"]
    assert [(o["exchange_id"], o["side"], o["surge_id"]) for o in fades] == [
        (PARTICIPANT_EID, "no", surges_by_eid[PARTICIPANT_EID]["id"])  # fade the up-spike: buy NO
    ]
    arbitrage = {o["market_id"] for o in opps if o["kind"] == "arbitrage"}
    assert {"311", "313"} <= arbitrage  # Arizona asks sum below 1; the Senate-control violation
    bands = {b["exchange_id"]: b for b in pipe.app.highband()["bands"]}
    carries = [o for o in opps if o["kind"] == "carry"]
    assert carries and all(o["exchange_id"] in bands for o in carries)
    for opp in carries:
        assert opp["settles_before_cup_end"] == bands[opp["exchange_id"]]["settles_before_cup_end"]
    assert_json_safe(report)


def test_strategy_uses_the_trackers_leader_for_the_risk_mode(pipe: SimpleNamespace) -> None:
    report = pipe.app.strategy()
    status = pipe.app.status()
    board = pipe.tracker.view()["context"]["leaderboard"]
    assert report["leader_value"] == pytest.approx(board["leader_value"])
    assert report["my_rank"] == board["my_rank"]
    expected = strategy_mod.risk_mode(
        status["account"]["balance"], 100_000.0, board["leader_value"], board["my_rank"], report["days_left"]
    )
    assert report["risk_mode"] == expected == "aggressive"  # the demo trader is more than 10% behind the leader


def test_strategy_is_cached_and_picks_up_the_backtest(pipe: SimpleNamespace) -> None:
    pipe.app.wait_backtest(10)
    first = pipe.app.strategy()
    assert pipe.app.strategy() is first  # cached for 30 s of (frozen) time
    assert first["backtest_status"] == "ready" and first["backtest_error"] is None
    assert isinstance(first["backtest"], dict) and isinstance(first["backtest"]["n_surges"], int)


def test_analyze_queues_and_reanalyzes(pipe: SimpleNamespace, surges_by_eid: Dict[str, Dict[str, Any]]) -> None:
    sid = surges_by_eid[PARTICIPANT_EID]["id"]
    pipe.tracker.analyze_pending(10)  # start from an empty queue
    before = pipe.store.get_surge(sid).attribution.analyzed_at
    status, body = pipe.app.analyze(sid)
    assert (status, body["queued"], body["surge_id"]) == (200, True, sid)
    assert body["message"]
    assert pipe.tracker.status()["queues"]["analysis"] == 1
    status, body = pipe.app.analyze(str(sid))  # a second click while queued
    assert status == 200 and body["queued"] is True
    assert pipe.tracker.status()["queues"]["analysis"] == 1
    pipe.clock.advance(1)
    assert pipe.tracker.analyze_pending(5) == 1
    after = pipe.store.get_surge(sid).attribution
    assert after.analyzed_at == pipe.clock.now and after.analyzed_at > before
    assert after.verdict == "participants"


@pytest.mark.parametrize("sid", ["99999", 99999, "abc", "-1", " 1", "1.0", "", "1" * 13, None])
def test_analyze_unknown_surge(pipe: SimpleNamespace, sid: Any) -> None:
    status, body = pipe.app.analyze(sid)
    assert status == 404 and isinstance(body["error"], str)
    assert pipe.tracker.status()["queues"]["analysis"] == 0


# --------------------------------------------------------------------------- DashboardApp edge cases (FakeTracker)


EMPTY_VIEWS = [
    pytest.param({}, id="empty-dict"),
    pytest.param(None, id="none"),
    pytest.param([], id="not-a-mapping"),
    pytest.param({"exchanges": None, "surges": None, "high_band": None, "context": None, "status": None}, id="all-null"),
    pytest.param({"exchanges": [], "surges": [], "high_band": [], "context": {}, "status": {}}, id="before-first-cycle"),
]


@pytest.mark.parametrize("view", EMPTY_VIEWS)
def test_empty_views_are_handled(make_app: Callable[..., web.DashboardApp], view: Any) -> None:
    tracker = FakeTracker()
    tracker._view = view
    app = make_app(tracker)
    status = app.status()
    assert STATUS_KEYS <= set(status) and status["view_error"] is None
    assert all(status["counts"][k] == 0 for k in COUNT_KEYS)
    assert status["account"]["balance"] is None and status["account"]["initial_balance"] == 100_000
    assert status["account"]["leaders"] == [] and status["account"]["leader_value"] is None
    assert status["tracker"]["fatal_error"] is None and status["tracker"]["cycles"] == 0
    markets = app.markets()
    assert (markets["now"], markets["total"], markets["count"], markets["q"], markets["rows"]) == (NOW, 0, 0, "", [])
    assert app.markets("maine")["rows"] == []
    surges = app.surges()
    assert (surges["count"], surges["surges"]) == (0, [])
    assert app.highband()["bands"] == []
    report = app.strategy()
    assert report["available"] is True and report["opportunities"] == []
    for payload in (status, app.health(), report):
        assert_json_safe(payload)


def test_exchange_falls_back_to_the_store(make_app: Callable[..., web.DashboardApp]) -> None:
    app = make_app()  # empty view, no client
    data = app.exchange("e1")
    assert data is not None
    assert data["exchange"]["title"] == "Will Democrats win the Maine Senate race?"
    assert data["exchange"]["market_id"] == "m1" and data["exchange"]["option"] == "YES"
    assert [p[2] for p in data["series"]] == ["tick"] * 5
    assert data["book"] is None and data["book_error"] and data["book_fetched_at"] is None
    assert data["surges"] == [] and data["trades"] == []
    assert app.exchange("nope") is None
    assert_json_safe(data)


def test_view_errors_are_reported_and_scrubbed(make_app: Callable[..., web.DashboardApp]) -> None:
    app = make_app(FakeTracker(fail=RuntimeError(f"tracker exploded with {FAKE_KEY}")), secrets=[FAKE_KEY])
    status = app.status()
    assert status["view_error"] == "tracker exploded with ***"
    assert app.markets()["rows"] == [] and app.surges()["surges"] == []
    assert FAKE_KEY.encode() not in dumps(status)


JUNK_VIEW: Dict[str, Any] = {
    "exchanges": [
        {
            "exchange_id": 1, "market_id": 2, "title": None, "option": None, "mark": float("nan"), "last": "0.5",
            "bid": True, "ask": float("inf"), "change_1h": float("-inf"),
            "sparkline": [[1, float("nan")], [2, 0.5], "junk", {"ts": 3, "price": 0.6}, [None, 1], [4]],
            "high_band": "not-a-dict", "surge_id": True,
        },
        {"id": "e2a", "title": "Who will win the Springfield mayoral race?", "bid": 0.4, "ask": 0.5},
        {"title": "a row without an id"},
        "junk",
        None,
    ],
    "surges": [
        {
            "exchange_id": "e2a", "id": 5, "detected_at": None, "status": "open",
            "attribution": {
                "verdict": "participants",
                "articles": [
                    {"url": "javascript:alert(1)", "title": "<img src=x onerror=alert(1)>"},
                    {"url": "data:text/html,hi", "title": "data url"},
                    {"url": "https://ok.example/story", "title": "fine"},
                    {"url": "  ", "title": "blank"},
                    "junk",
                ],
                "reasons": [1, None, "a reason"],
            },
        },
        {"exchange_id": "e1", "id": 6, "detected_at": NOW - 10, "zscore": float("nan"), "attribution": "junk"},
        {"no": "exchange id"},
        None,
    ],
    "high_band": [
        {"exchange_id": "e2a", "side": "NO", "favorite_price": float("nan")},
        {"exchange_id": "e9", "side": "YES", "favorite_price": 0.97, "settlement_date": "2026-11-30T00:00:00Z"},
        {"missing": "exchange id"},
    ],
    "context": {"balance": "lots", "leaderboard": "junk", "constraints": "junk", "overround": "junk"},
    "status": {"errors": [{"message": "first"}, {"error": "second"}], "cycles": float("nan"), "backfill": {"done": 3, "pending": 2}},
}


def test_nulls_and_junk_are_normalised(make_app: Callable[..., web.DashboardApp]) -> None:
    app = make_app(FakeTracker(JUNK_VIEW))
    rows = app.markets()["rows"]
    assert [r["exchange_id"] for r in rows] == ["1", "e2a"]  # rows without an id are skipped; ids are strings
    first = rows[0]
    assert first["market_id"] == "2" and first["title"] == ""
    assert first["mark"] is None and first["last"] is None and first["bid"] is None and first["ask"] is None
    assert first["change_1h"] is None and first["spread"] is None
    assert first["sparkline"] == [[2.0, 0.5], [3.0, 0.6]]
    assert first["high_band"] is None and first["surge_id"] is None
    assert rows[1]["spread"] == pytest.approx(0.1)

    surges = app.surges()["surges"]
    assert [s["id"] for s in surges] == [6, 5]  # newest first; a missing detected_at sorts last
    junk_surge = next(s for s in surges if s["id"] == 5)
    assert junk_surge["title"] == "Who will win the Springfield mayoral race?"  # from the row
    assert [a["url"] for a in junk_surge["attribution"]["articles"]] == [None, None, "https://ok.example/story", None]
    reasons = junk_surge["attribution"]["reasons"]
    assert all(isinstance(r, str) for r in reasons) and "a reason" in reasons
    other = next(s for s in surges if s["id"] == 6)
    assert other["attribution"] is None
    assert other["title"] == "Will Democrats win the Maine Senate race?" and other["option"] == "YES"  # from the store

    bands = app.highband()["bands"]
    assert [b["exchange_id"] for b in bands] == ["e9", "e2a"]
    e9 = bands[0]
    assert e9["entry_is_estimate"] is True and e9["entry_price"] == 0.97  # no book: the mark stands in
    assert e9["settles_before_cup_end"] is False
    e2a = bands[1]
    assert e2a["entry_price"] == pytest.approx(0.6)  # NO entry = 1 - YES bid
    assert e2a["settles_before_cup_end"] is None

    status = app.status()
    assert status["tracker"]["errors"] == 2 and status["tracker"]["last_error"] == "second"
    assert status["tracker"]["cycles"] == 0
    assert status["tracker"]["backfill"] == {"done": 3.0, "total": 5.0, "failed": None, "pending": 2.0, "complete": False}
    assert status["account"]["balance"] is None
    for payload in (status, app.markets(), app.surges(), app.highband(), app.strategy()):
        assert_json_safe(payload)


@pytest.mark.parametrize("fatal", ["HTTP 401 INVALID_API_KEY: bad key (GET /markets)", {"message": "HTTP 401 INVALID_API_KEY: bad key"}])
def test_fatal_error_is_surfaced(make_app: Callable[..., web.DashboardApp], fatal: Any) -> None:
    app = make_app(FakeTracker({"status": {"fatal_error": fatal, "running": False, "cycles": 4}}))
    tracker = app.status()["tracker"]
    assert tracker["fatal_error"].startswith("HTTP 401 INVALID_API_KEY")
    assert tracker["running"] is False and tracker["cycles"] == 4


def test_fatal_error_from_a_real_tracker(make_app: Callable[..., web.DashboardApp], store: TrackerStore) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "INVALID_API_KEY", "message": "The key is not recognised"}})

    client = SuperMarketClient("ace_rejected_key_000", "https://api.invalid/api/v1", transport=httpx.MockTransport(reject))
    try:
        tracker = tracker_mod.Tracker(client, Context("t-1", "cup", "Cup"), store, backfill=False, clock=lambda: NOW)
        with pytest.raises(ApiError):
            tracker.run_once()
        app = make_app(tracker, client=client)
        status = app.status()
        assert "INVALID_API_KEY" in status["tracker"]["fatal_error"]
        assert status["view_error"] is None and status["counts"]["outcomes"] == 0
    finally:
        client.close()


def test_missing_context_uses_cup_defaults(make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERMARKET_CUP_END", raising=False)
    app = make_app(FakeTracker({"exchanges": [], "status": {}}))
    status = app.status()
    assert status["cup_end"] == CUP_END_TS
    assert status["days_left"] == pytest.approx((CUP_END_TS - NOW) / 86400.0)
    expected = {"balance": None, "initial_balance": 100_000.0, "my_rank": None, "leader_value": None, "leaders": [], "updated_at": None}
    assert {k: status["account"][k] for k in expected} == expected
    assert status["tournament"]["label"] == "public"  # no trading context


def test_tournament_end_date_overrides_the_default(make_app: Callable[..., web.DashboardApp]) -> None:
    ctx = {"tournament": {"endDate": "2026-11-01T00:00:00Z", "myBalance": 990.0, "initialBalance": 1000}}
    app = make_app(FakeTracker({"context": ctx}))
    status = app.status()
    assert status["cup_end"] == iso_epoch("2026-11-01T00:00:00Z")
    assert status["account"]["balance"] == 990.0 and status["account"]["initial_balance"] == 1000.0
    assert app.highband()["cup_end"] == status["cup_end"]


LEADERBOARD = {
    "leaderboard": [
        {"rank": 1, "profileId": "p1", "username": "alice", "pnl": 21000.0, "tradesCount": 40, "volume": 9000, "winRate": 60.0, "roi": 55.5},
        {"rank": 2, "profileId": "p2", "username": "bob", "pnl": 3000.0, "tradesCount": 12, "volume": 4000, "winRate": None, "roi": 75.0},
        {"rank": 3, "profileId": "p3", "username": None, "pnl": 2500.0, "tradesCount": 3, "volume": 800, "winRate": None, "roi": 312.5},
    ],
    "total": 120, "period": "all", "limit": 3, "offset": 0, "sort": "pnl", "myRank": 17,
}


def test_context_summary_reads_the_trackers_leaderboard() -> None:
    board = tracker_mod.Tracker._leaderboard(LEADERBOARD, 100_000.0)  # exactly what the tracker stores
    summary = web.context_summary({"leaderboard": board, "balance": 98_000.0, "initial_balance": 100_000.0})
    assert summary["my_rank"] == 17
    assert summary["leader_value"] == pytest.approx(121_000.0)
    assert [e["username"] for e in summary["leaders"]] == ["alice", "bob", None]
    assert summary["leaders"][0]["value"] == pytest.approx(121_000.0)


@pytest.mark.parametrize(
    "board, rank, leader",
    [
        (LEADERBOARD, 17, 121_000.0),  # the raw API shape
        (LEADERBOARD["leaderboard"], None, 121_000.0),  # a bare list
        ({"entries": [{"rank": 1, "username": "z", "value": 150_000.0}], "my_rank": 2}, 2, 150_000.0),
        ("junk", None, None),
        ({"leaderboard": "junk"}, None, None),
    ],
)
def test_context_summary_leaderboard_shapes(board: Any, rank: Optional[int], leader: Optional[float]) -> None:
    summary = web.context_summary({"leaderboard": board, "initial_balance": 100_000.0})
    assert summary["my_rank"] == rank
    assert summary["leader_value"] == (pytest.approx(leader) if leader is not None else None)


def test_highband_entry_and_settlement_rules(make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERMARKET_CUP_END", raising=False)
    view = {
        "exchanges": [
            {"exchange_id": "y", "title": "Yes fav", "bid": 0.96, "ask": 0.97, "settlement_date": "2026-11-04T04:59:00Z"},
            {"exchange_id": "n", "title": "No fav", "bid": 0.02, "ask": 0.03},
            {"exchange_id": "x", "title": "No book"},
            {"exchange_id": "one", "title": "Pinned at one", "bid": 0.99, "ask": 1.0},
        ],
        "high_band": [
            {"exchange_id": "y", "side": "YES", "favorite_price": 0.965},
            {"exchange_id": "n", "side": "NO", "favorite_price": 0.975, "settlement_date": "2026-11-30T23:59:00Z"},
            {"exchange_id": "x", "side": "NO", "favorite_price": 0.96},
            {"exchange_id": "one", "side": "YES", "favorite_price": 0.995},
        ],
    }
    bands = {b["exchange_id"]: b for b in make_app(FakeTracker(view)).highband()["bands"]}
    assert bands["y"]["entry_price"] == 0.97 and bands["y"]["settles_before_cup_end"] is True
    assert bands["y"]["payout_per_share"] == pytest.approx(0.03) and bands["y"]["return_pct"] == pytest.approx(3.0928, abs=1e-3)
    assert bands["n"]["entry_price"] == pytest.approx(0.98) and bands["n"]["entry_is_estimate"] is False
    assert bands["n"]["settles_before_cup_end"] is False
    assert bands["x"]["entry_is_estimate"] is True and bands["x"]["entry_price"] == 0.96
    assert bands["x"]["settlement_ts"] is None and bands["x"]["settles_before_cup_end"] is None
    assert bands["one"]["entry_price"] == 1.0
    assert bands["one"]["payout_per_share"] is None and bands["one"]["return_pct"] is None


def test_book_errors_are_short_and_scrubbed(make_app: Callable[..., web.DashboardApp]) -> None:
    class BrokenClient:
        def get_exchange_orderbook(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"connect failed for Bearer {FAKE_KEY} " + "x" * 500)

    app = make_app(client=BrokenClient(), secrets=[FAKE_KEY])
    data = exchange_with_book(app, "e1")
    assert data["book"] is None and data["book_fetched_at"] == NOW and data["book_pending"] is False
    assert FAKE_KEY not in data["book_error"] and "***" in data["book_error"]
    assert len(data["book_error"]) <= 300


def test_truncated_errors_do_not_leak_part_of_a_secret(make_app: Callable[..., web.DashboardApp]) -> None:
    class BrokenClient:
        def get_exchange_orderbook(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("x" * 280 + FAKE_KEY)

    app = make_app(client=BrokenClient(), secrets=[FAKE_KEY])
    text = web.encode_json(exchange_with_book(app, "e1"), [FAKE_KEY]).decode("utf-8")
    assert FAKE_KEY[:12] not in text


def test_analyze_edge_cases(make_app: Callable[..., web.DashboardApp], store: TrackerStore) -> None:
    sid = store.record_surge(make_surge("e1", "m1")).id
    tracker = FakeTracker()
    status, body = make_app(tracker).analyze(sid)
    assert (status, body["queued"], body["surge_id"]) == (200, True, sid) and body["message"]
    assert tracker.requests == [sid]

    status, body = make_app(FakeTracker(queued=False)).analyze(sid)
    assert status == 200 and body["queued"] is False and body["message"]

    status, body = make_app(tracker, analysis_enabled=False).analyze(sid)
    assert status == 409 and body["queued"] is False

    status, body = make_app(NoReanalysis()).analyze(sid)
    assert status == 409 and body["queued"] is False

    status, body = make_app(FakeTracker(queued=RuntimeError(f"queue broke near {FAKE_KEY}")), secrets=[FAKE_KEY]).analyze(sid)
    assert status == 500 and FAKE_KEY not in body["error"] and "***" in body["error"]

    assert make_app(tracker).analyze(sid + 100)[0] == 404


def test_tiered_series_downsampling() -> None:
    now = NOW
    points = [PricePoint(now - 7 * 86400 + i * 60, 0.5 + 0.0001 * (i % 100)) for i in range(7 * 1440 + 1)]
    out = web.tiered_series(points, now)
    assert 500 <= len(out) <= web.MAX_SERIES_POINTS
    assert [p.ts for p in out] == sorted(p.ts for p in out)
    assert out[-1].ts == points[-1].ts and out[0].ts == points[0].ts
    last_6h = [p for p in out if p.ts >= now - 6 * 3600]
    older = [p for p in out if p.ts < now - 86400]
    assert len(last_6h) >= 250 and len(older) <= 160  # denser near now
    few = [PricePoint(3.0, 0.3), PricePoint(1.0, 0.1), PricePoint(2.0, None), PricePoint(4.0, float("nan"))]
    assert [p.ts for p in web.tiered_series(few, now)] == [1.0, 3.0]  # sorted, missing prices dropped
    assert web.tiered_series([], now) == []


def test_encode_json_scrubs_secrets_and_non_finite_numbers() -> None:
    payload = {
        "msg": f"key {FAKE_KEY} leaked", "api_key": FAKE_KEY, "Authorization": f"Bearer {FAKE_KEY}",
        "nested": [{"token": "t0ken", "x-api-key": "k", "password": "p", "ok": 1, "v": float("nan"), "w": float("inf")}],
        "title": "café — \U0001f600  ",
    }
    body = web.encode_json(payload, [FAKE_KEY])
    assert FAKE_KEY.encode() not in body and b"t0ken" not in body
    data = json.loads(body.decode("utf-8"))
    assert data == {"msg": "key *** leaked", "nested": [{"ok": 1, "v": None, "w": None}], "title": "café — \U0001f600  "}


# --------------------------------------------------------------------------- static files (unit)


def test_resolve_static_serves_only_known_files_inside_the_root(static_tree: SimpleNamespace) -> None:
    root = static_tree.root
    real = root.resolve()
    assert web.resolve_static("/", root) == real / "index.html"
    assert web.resolve_static("", root) == real / "index.html"
    assert web.resolve_static("/app.js", root) == real / "app.js"
    assert web.resolve_static("/sub/page.html", root) == real / "sub" / "page.html"
    assert web.resolve_static("/%61pp.js", root) == real / "app.js"  # percent-decoded once
    for bad in (
        "/../outside.html", "/..%2foutside.html", "/%2e%2e/outside.html", "/sub/../../outside.html",
        "/sub/../index.html", "/./index.html", "/..\\outside.html", "/%5c..%5coutside.html", "\\index.html",
        "//index.html", "/index.html/", "/sub/", "/sub", "/.secret.html", "/notes.py", "/missing.css",
        "/%00index.html", "/index.html%00.js", "/%252e%252e/outside.html", "/../web-backup/index.html",
        "/" + str(static_tree.outside), "/etc/passwd", "/link.html",
    ):
        assert web.resolve_static(bad, root) is None, bad


def test_the_real_ui_files_resolve_when_present() -> None:
    if not (web.WEB_DIR / "index.html").is_file():
        pytest.skip("the browser UI (supermarket_bot/web/) is not built yet")
    assert web.resolve_static("/") == (web.WEB_DIR / "index.html").resolve()
    assert web.resolve_static("/../web.py") is None and web.resolve_static("/../__init__.py") is None


# --------------------------------------------------------------------------- HTTP: endpoints


@pytest.mark.parametrize("path", GET_ENDPOINTS)
def test_get_endpoints_return_json_with_security_headers(client: HTTP, path: str) -> None:
    resp = client.get(path)
    assert resp.status == 200, resp.body[:300]
    assert resp.headers["content-type"] == web.JSON_TYPE
    assert resp.headers["cache-control"] == "no-store"
    assert int(resp.headers["content-length"]) == len(resp.body)
    assert_security_headers(resp)
    text = resp.body.decode("utf-8")  # valid UTF-8 …
    assert "NaN" not in text and "Infinity" not in text
    assert isinstance(json.loads(text), dict)  # … and valid JSON


def test_http_payloads_match_the_app(client: HTTP, pipe: SimpleNamespace) -> None:
    assert client.get("/api/markets").json() == assert_json_safe(pipe.app.markets())
    assert client.get("/api/surges").json() == assert_json_safe(pipe.app.surges())
    assert client.get("/api/highband").json() == assert_json_safe(pipe.app.highband())
    exchange_with_book(pipe.app, "9003")  # the order book is cached, so both reads see the same one
    assert client.get("/api/exchange/9003").json() == assert_json_safe(pipe.app.exchange("9003"))
    status = client.get("/api/status").json()
    assert status["demo"] is True and status["counts"] == pipe.app.status()["counts"]
    assert status["tournament"]["name"] == "Predictions Cup — Midterm Elections (demo)"  # UTF-8 round trip


@pytest.mark.parametrize(
    "query, expected",
    [
        ("q=arizona%20governor", ARIZONA),
        ("q=Arizona+Governor", ARIZONA),
        ("q=9002", {"9002"}),
        ("q=nothing-matches-this", set()),
        ("q=%3Cscript%3E", set()),
        ("q=%ff%fe", set()),  # undecodable bytes are replaced, not an error
        ("q=arizona&q=senate", ARIZONA),  # the first q wins
        ("q=", None),
        ("other=1", None),
    ],
)
def test_http_markets_query(client: HTTP, query: str, expected: Optional[set]) -> None:
    data = client.get("/api/markets?" + query).json()
    ids = {r["exchange_id"] for r in data["rows"]}
    assert data["total"] == 22
    if expected is None:
        assert len(ids) == 22
    else:
        assert ids == expected


def test_http_markets_query_is_truncated(client: HTTP) -> None:
    data = client.get("/api/markets?q=" + "a" * 500).json()
    assert data["q"] == "a" * 200 and data["rows"] == []


@pytest.mark.parametrize(
    "path",
    ["/api/exchange/99999", "/api/exchange/abc", "/api/exchange/", "/api/exchange/9001/extra", "/api/exchange/%2e%2e",
     "/api/exchange/..%2f..%2fetc%2fpasswd", "/api/exchange/" + "9" * 70, "/api/exchange/%ed%a0%80"],
)
def test_http_exchange_unknown_is_404(client: HTTP, path: str) -> None:
    assert_json_error(client.get(path), 404)


def test_http_exchange_percent_encoded_id(client: HTTP) -> None:
    resp = client.get("/api/exchange/%39%30%30%31")
    assert resp.status == 200 and resp.json()["exchange"]["exchange_id"] == "9001"


@pytest.mark.parametrize("path", ["/api/nope", "/api/", "/api", "/api/markets/", "/api/status/extra", "/api/surges/1"])
def test_http_unknown_api_paths_are_404(client: HTTP, path: str) -> None:
    resp = client.get(path)
    assert resp.status == 404
    assert_security_headers(resp)


def test_http_head_has_headers_but_no_body(client: HTTP) -> None:
    get = client.get("/api/health")
    head = client.request("HEAD", "/api/health")
    assert head.status == 200 and head.body == b""
    assert int(head.headers["content-length"]) == len(get.body)
    assert_security_headers(head)


def test_http_keep_alive_serves_several_requests(server: web.DashboardServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        for path in ("/api/health", "/api/status", "/api/highband", "/api/health"):
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200 and json.loads(body)
    finally:
        conn.close()


def test_http_concurrent_gets(server: web.DashboardServer) -> None:
    paths = [GET_ENDPOINTS[i % len(GET_ENDPOINTS)] for i in range(20)]
    barrier = threading.Barrier(len(paths), timeout=10)

    def fetch(path: str) -> Tuple[str, int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
        try:
            barrier.wait()
            conn.request("GET", path)
            resp = conn.getresponse()
            return path, resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        results = list(pool.map(fetch, paths))
    assert [status for _, status, _ in results] == [200] * len(paths)
    for path, _, data in results:
        if path == "/api/markets":
            assert data["count"] == 22
        if path.startswith("/api/exchange/"):
            assert data["exchange"]["exchange_id"] == path.rsplit("/", 1)[1]


# --------------------------------------------------------------------------- HTTP: host and origin checks


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:{port}",
        "attacker.com:{port}",
        "127.0.0.1.nip.io:{port}",  # DNS-rebinding style names that resolve to 127.0.0.1
        "localhost.attacker.com:{port}",
        "127.0.0.1.attacker.com:{port}",
        "{port}.127.0.0.1.sslip.io:{port}",
        "127.0.0.1:{other}",
        "127.0.0.1",
        "localhost",
        "0.0.0.0:{port}",
        "127.1:{port}",
        "2130706433:{port}",
        "[::ffff:127.0.0.1]:{port}",
        "127.0.0.1:{port}@evil.example",
        "",
    ],
)
def test_bad_host_headers_get_421(client: HTTP, host: str) -> None:
    value = host.format(port=client.port, other=client.port + 1)
    for method, path in (("GET", "/api/status"), ("GET", "/"), ("GET", "/api/exchange/9001")):
        assert_json_error(client.request(method, path, headers={"Host": value}), 421)
    resp = client.post("/api/surges/1/analyze", body=None, Host=value, Origin="http://" + value)
    assert_json_error(resp, 421)
    assert_json_error(client.request("PUT", "/api/status", headers={"Host": value}), 421)


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}", "[::1]:{port}", " localhost:{port} "])
def test_local_host_headers_are_accepted(client: HTTP, host: str) -> None:
    resp = client.get("/api/health", headers={"Host": host.format(port=client.port)})
    assert resp.status == 200 and resp.json()["ok"] is True


def test_missing_host_header_gets_421(server: web.DashboardServer) -> None:
    for request in (b"GET /api/health HTTP/1.1\r\nConnection: close\r\n\r\n", b"GET /api/health HTTP/1.0\r\n\r\n"):
        assert_json_error(raw_request(server.port, request), 421)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": None},
        {"Origin": "null"},
        {"Origin": "http://evil.example"},
        {"Origin": "https://127.0.0.1:{port}"},
        {"Origin": "http://127.0.0.1:{port}/"},
        {"Origin": "http://127.0.0.1:{other}"},
        {"Origin": "http://localhost:{port}"},  # the page was loaded from 127.0.0.1, not localhost
        {"Origin": "http://127.0.0.1:{port}.evil.example"},
        {"Content-Type": None},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Content-Type": "multipart/form-data; boundary=x"},
        {"Content-Type": "application/json-patch+json"},
        {"Content-Type": "text/plain; application/json"},
    ],
)
def test_post_requires_same_origin_json(client: HTTP, pipe: SimpleNamespace, surges_by_eid: Dict[str, Any], headers: Dict[str, Optional[str]]) -> None:
    sid = surges_by_eid[PARTICIPANT_EID]["id"]
    pipe.tracker.analyze_pending(10)
    fmt = {k: (v.format(port=client.port, other=client.port + 1) if v else v) for k, v in headers.items()}
    resp = client.post(f"/api/surges/{sid}/analyze", body=None, **{k.replace("-", "_"): v for k, v in fmt.items()})
    assert_json_error(resp, 403)
    assert pipe.tracker.status()["queues"]["analysis"] == 0  # nothing was queued


def test_post_analyze_over_http(client: HTTP, pipe: SimpleNamespace, surges_by_eid: Dict[str, Any]) -> None:
    sid = surges_by_eid[NEWS_EID]["id"]
    try:
        resp = client.post(f"/api/surges/{sid}/analyze")
        assert resp.status == 200 and resp.json() == {"queued": True, "surge_id": sid, "message": "Queued for analysis."}
        assert_security_headers(resp)
        resp = client.post(f"/api/surges/{sid}/analyze", body=None, Content_Type="application/json; charset=utf-8")
        assert resp.status == 200 and resp.json()["queued"] is True
        resp = client.post(f"/api/surges/{sid}/analyze", body=b'{"reason": "manual"}', Host=f"localhost:{client.port}", Origin=f"http://localhost:{client.port}")
        assert resp.status == 200
        assert pipe.tracker.status()["queues"]["analysis"] == 1
    finally:
        pipe.tracker.analyze_pending(10)
    for bad in ("999999", "abc", "-1", "%2e%2e", "1%2f2"):
        assert_json_error(client.post(f"/api/surges/{bad}/analyze"), 404)


def test_post_body_validation(client: HTTP, server: web.DashboardServer, pipe: SimpleNamespace, surges_by_eid: Dict[str, Any]) -> None:
    sid = surges_by_eid[PARTICIPANT_EID]["id"]
    path = f"/api/surges/{sid}/analyze"
    try:
        assert_json_error(client.post(path, body=b"{not json"), 400)
        assert_json_error(client.post(path, body=b"[1, 2]"), 400)
        assert_json_error(client.post(path, body=b'"text"'), 400)
        assert_json_error(client.post(path, body=b"\xff\xfe{}"), 400)
        big = b"{" + b" " * (web.MAX_POST_BYTES - 2) + b"}"
        assert len(big) == web.MAX_POST_BYTES
        assert client.post(path, body=big).status == 200  # exactly at the limit is fine
    finally:
        pipe.tracker.analyze_pending(10)

    def raw_post(extra: str, body: bytes = b"") -> Resp:
        head = (
            f"POST {path} HTTP/1.1\r\nHost: {client.host}\r\nOrigin: {client.origin}\r\n"
            f"Content-Type: application/json\r\nConnection: close\r\n{extra}\r\n"
        )
        return raw_request(server.port, head.encode("latin-1") + body)

    assert_json_error(raw_post(f"Content-Length: {web.MAX_POST_BYTES + 1}\r\n"), 413)
    assert_json_error(raw_post("Content-Length: 1000000000\r\n"), 413)
    assert_json_error(raw_post("Content-Length: abc\r\n"), 400)
    assert_json_error(raw_post("Content-Length: -5\r\n"), 400)
    assert_json_error(raw_post("Transfer-Encoding: chunked\r\n"), 411)
    assert pipe.tracker.status()["queues"]["analysis"] == 0


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize("path", ["/api/status", "/api/surges/1/analyze", "/", "/index.html"])
def test_unsupported_methods_are_405(client: HTTP, method: str, path: str) -> None:
    resp = client.request(method, path, headers={"Content-Type": "application/json", "Origin": client.origin})
    data = assert_json_error(resp, 405)
    assert "GET" in resp.headers["allow"]
    assert data["error"]


@pytest.mark.parametrize("path, allow", [("/api/markets", "GET, HEAD"), ("/api/exchange/9001", "GET, HEAD"), ("/index.html", "GET, HEAD")])
def test_post_to_read_only_routes_is_405(client: HTTP, path: str, allow: str) -> None:
    resp = client.post(path)
    assert_json_error(resp, 405)
    assert resp.headers["allow"] == allow


def test_get_on_the_analyze_route_is_405(client: HTTP) -> None:
    resp = client.get("/api/surges/1/analyze")
    assert_json_error(resp, 405)
    assert resp.headers["allow"] == "POST"


def test_post_to_unknown_api_route_is_404(client: HTTP) -> None:
    assert_json_error(client.post("/api/nope"), 404)


@pytest.mark.parametrize("method", ["TRACE", "PROPFIND", "FOO"])
def test_exotic_methods_are_405_with_security_headers(server: web.DashboardServer, method: str) -> None:
    resp = raw_request(server.port, f"{method} /api/status HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nConnection: close\r\n\r\n".encode())
    assert resp.status == 405
    assert_security_headers(resp)


@pytest.mark.parametrize(
    "request_bytes",
    [
        pytest.param(b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nConnection: close\r\n\r\n", id="uri-too-long-414"),
        pytest.param(b"GET / HTTP/1.1\r\nX-Long: " + b"a" * 70000 + b"\r\nConnection: close\r\n\r\n", id="header-too-long-431"),
    ],
)
def test_stdlib_error_pages_carry_security_headers(server: web.DashboardServer, request_bytes: bytes) -> None:
    resp = raw_request(server.port, request_bytes)
    assert resp.status in (414, 431)
    assert_security_headers(resp)


@pytest.mark.parametrize("request_bytes", [b"GARBAGE\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"\x16\x03\x01\x02\x00\x01\r\n\r\n"])
def test_malformed_requests_are_rejected_without_serving_anything(server: web.DashboardServer, request_bytes: bytes) -> None:
    resp = raw_request(server.port, request_bytes)
    assert resp.status in (0, 400, 505)  # the stdlib answers unparseable request lines HTTP/0.9-style
    assert b"{" not in resp.body  # no API payload or static file leaks out
    assert HTTP(server.port).get("/api/health").status == 200  # and the server keeps serving


# --------------------------------------------------------------------------- HTTP: static files


def test_static_files_are_served_with_security_headers(client: HTTP, static_root: SimpleNamespace) -> None:
    index = client.get("/")
    assert index.status == 200 and index.headers["content-type"] == "text/html; charset=utf-8"
    assert index.body == (static_root.root / "index.html").read_bytes()
    assert "café" in index.body.decode("utf-8")
    assert index.headers["cache-control"] == "no-cache"
    assert_security_headers(index)
    for path, ctype in (("/index.html", "text/html; charset=utf-8"), ("/app.js?v=3", "text/javascript; charset=utf-8"),
                        ("/styles.css", "text/css; charset=utf-8"), ("/sub/page.html", "text/html; charset=utf-8")):
        resp = client.get(path)
        assert resp.status == 200 and resp.headers["content-type"] == ctype, path
        assert_security_headers(resp)
    head = client.request("HEAD", "/app.js")
    assert head.status == 200 and head.body == b""
    assert int(head.headers["content-length"]) == len((static_root.root / "app.js").read_bytes())


@pytest.mark.parametrize(
    "path",
    [
        "/../outside.html",
        "/..%2foutside.html",
        "/%2e%2e/outside.html",
        "/%2e%2e%2foutside.html",
        "/%2E%2E/%2E%2E/%2E%2E/etc/passwd",
        "/sub/../../outside.html",
        "/sub/%2e%2e/%2e%2e/outside.html",
        "/..\\outside.html",
        "/%5c..%5coutside.html",
        "/%2e%2e%5coutside.html",
        "/sub\\..\\..\\outside.html",
        "//etc/passwd",
        "/etc/passwd",
        "/%2fetc%2fpasswd",
        "/%252e%252e/outside.html",
        "/../web-backup/index.html",
        "/.secret.html",
        "/%2esecret.html",
        "/notes.py",
        "/sub/",
        "/sub",
        "/missing.js",
        "/%00index.html",
        "/index.html%00.js",
        "/link.html",
        "/OUTSIDE_ABSOLUTE",
    ],
)
def test_static_path_traversal_is_404(client: HTTP, static_root: SimpleNamespace, path: str) -> None:
    if path == "/OUTSIDE_ABSOLUTE":
        path = "/" + str(static_root.outside).lstrip("/")
        assert client.get("/" + str(static_root.outside)).status == 404
    resp = client.get(path)
    assert_json_error(resp, 404)
    assert OUTSIDE_MARK.encode() not in resp.body
    assert b"root:" not in resp.body


def test_static_root_missing_files_are_404(client: HTTP, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = web.resolve_static
    monkeypatch.setattr(web, "resolve_static", lambda path, root=tmp_path / "missing": original(path, root))
    assert_json_error(client.get("/"), 404)
    assert_json_error(client.get("/app.js"), 404)


# --------------------------------------------------------------------------- HTTP: secrets and encoding


def _leaky_view() -> Dict[str, Any]:
    """A view with the API key smuggled into every kind of field a real error or payload could carry."""
    leak = f"HTTP 500 INTERNAL: upstream echoed Authorization: Bearer {FAKE_KEY}"
    return {
        "exchanges": [
            {"exchange_id": "e1", "market_id": "m1", "title": f"Market {FAKE_KEY}", "option": "YES", "bid": 0.4, "ask": 0.45,
             "api_key": FAKE_KEY, "token": "realtime-token-abc", "high_band": {"side": "YES", "secret": FAKE_KEY}},
        ],
        "surges": [
            {"exchange_id": "e1", "id": 1, "detected_at": NOW, "title": "t", "status": "open",
             "attribution": {"verdict": "unclear", "reasons": [leak], "summary": leak,
                             "articles": [{"title": FAKE_KEY, "url": f"https://news.example/?apikey={FAKE_KEY}", "summary": leak}]}},
        ],
        "high_band": [{"exchange_id": "e1", "side": "YES", "favorite_price": 0.96, "password": FAKE_KEY}],
        "context": {
            "balance": 1000.0, "authorization": f"Bearer {FAKE_KEY}",
            "leaderboard": {"top": [{"rank": 1, "username": FAKE_KEY, "value": 2000.0}], "leader_value": 2000.0},
            "tournament": {"name": f"Cup {FAKE_KEY}", "description": leak},
        },
        "status": {"last_error": {"at": NOW, "where": "x", "error": leak}, "fatal_error": leak,
                   "recent_errors": [{"at": NOW, "error": leak}], "headers": {"Authorization": f"Bearer {FAKE_KEY}"}},
    }


def test_no_secrets_in_any_response(make_app: Callable[..., web.DashboardApp], store: TrackerStore) -> None:
    sid = store.record_surge(make_surge("e1", "m1")).id

    class LeakyClient:
        def get_exchange_orderbook(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError(f"proxy refused Bearer {FAKE_KEY}")

    tracker = FakeTracker(_leaky_view(), queued=RuntimeError(f"queue rejected {FAKE_KEY}"))
    app = make_app(tracker, client=LeakyClient(), secrets=[FAKE_KEY])
    assert "***" in exchange_with_book(app, "e1")["book_error"]  # the cached (masked) book error is served below
    with serve(app) as srv:
        http_client = HTTP(srv.port)
        responses = [http_client.get(p) for p in ("/api/health", "/api/status", "/api/markets", f"/api/markets?q={FAKE_KEY}",
                                                  "/api/surges", "/api/highband", "/api/exchange/e1", "/api/strategy",
                                                  f"/api/exchange/{FAKE_KEY}", "/nope")]
        responses.append(http_client.post(f"/api/surges/{sid}/analyze"))
        app.wait_backtest(10)
    statuses = [r.status for r in responses]
    assert statuses == [200] * 8 + [404, 404, 500]
    for resp in responses:
        raw = resp.body + json.dumps(resp.headers).encode()
        assert FAKE_KEY.encode() not in raw, resp.body[:200]
        assert b"realtime-token-abc" not in raw
        data = resp.json()

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    assert not re.search(r"(?i)api[_-]?key|token|secret|password|authorization", key), key
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)
    status = responses[1].json()
    assert "***" in status["tracker"]["fatal_error"]  # the text was there, masked
    assert status["account"]["leaders"][0]["username"] == "***"
    assert "***" in responses[3].json()["q"]  # even the echoed search query


def test_build_live_masks_the_settings_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live wiring with a fake key: the demo API stands in for the network and echoes the key in an error."""
    monkeypatch.delenv("SUPERMARKET_LLM", raising=False)
    market = demo_mod.DemoMarket(seed=7)
    inner = market.transport()
    seen_auth: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/relationships/constraints"):
            return httpx.Response(400, json={"error": {"code": "VALIDATION_ERROR", "message": f"key {FAKE_KEY} is not allowed here"}})
        return inner.handle_request(request)

    def from_settings(cls: Any, settings: Settings, **kwargs: Any) -> SuperMarketClient:
        return market.client(api_key=settings.api_key, transport=httpx.MockTransport(handler), reads_per_min=10_000)

    monkeypatch.setattr(SuperMarketClient, "from_settings", classmethod(from_settings))
    settings = Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path)
    args = build_parser().parse_args(["dashboard", "--no-news", "--interval", "30"])
    runtime = web.build_live(settings, args, out=io.StringIO())
    try:
        assert runtime.app.demo is False and runtime.app.secrets == [FAKE_KEY]
        assert runtime.app.news_enabled is False and runtime.news is None  # --no-news: no news hosts at all
        runtime.tracker.run_once()
        assert f"Bearer {FAKE_KEY}" in seen_auth  # the key really was used …
        assert any(FAKE_KEY in str(e) for e in runtime.tracker.status()["recent_errors"])  # … and leaked into an error
        with serve(runtime.app) as srv:
            http_client = HTTP(srv.port)
            bodies = [http_client.get(p).body for p in GET_ENDPOINTS]
            runtime.app.wait_backtest(10)
        for body in bodies:
            assert FAKE_KEY.encode() not in body
            assert b"Bearer" not in body
        status = json.loads(bodies[1])
        assert status["demo"] is False and status["counts"]["outcomes"] == 22
        assert "***" in status["tracker"]["last_error"]
        assert (tmp_path / demo_mod.DEMO_SLUG / "tracker.sqlite3").exists()
    finally:
        runtime.app.wait_backtest(10)
        runtime.close()


def test_demo_responses_carry_no_credentials(client: HTTP) -> None:
    for path in GET_ENDPOINTS:
        body = client.get(path).body.lower()
        for needle in (b"bearer", b"authorization", b"demo_offline_key", b"anonkey", b"demo-anon-key", b"supabase"):
            assert needle not in body, (path, needle)


def test_non_ascii_text_round_trips(make_app: Callable[..., web.DashboardApp]) -> None:
    title = "Qui gagnera ? café — 日本 \U0001f5f3️   \"quotes\" <b>&amp;</b>"
    app = make_app(FakeTracker({"exchanges": [{"exchange_id": "e1", "title": title, "option": "Ño"}]}))
    with serve(app) as srv:
        resp = HTTP(srv.port).get("/api/markets")
    assert resp.status == 200 and resp.headers["content-type"] == "application/json; charset=utf-8"
    row = json.loads(resp.body.decode("utf-8"))["rows"][0]
    assert row["title"] == title and row["option"] == "Ño"


def test_lone_surrogates_do_not_break_an_endpoint(make_app: Callable[..., web.DashboardApp]) -> None:
    app = make_app(FakeTracker({"exchanges": [{"exchange_id": "e1", "title": "broken \ud83d emoji"}]}))
    with serve(app) as srv:
        resp = HTTP(srv.port).get("/api/markets")
    assert resp.status == 200
    assert json.loads(resp.body.decode("utf-8"))["rows"][0]["exchange_id"] == "e1"


# --------------------------------------------------------------------------- the real runtime (threads, real time)


def _wait_for(predicate: Callable[[], bool], timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def _tracker_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("tracker-") and t.is_alive()]


def test_build_demo_runtime_start_and_stop(tmp_path: Path) -> None:
    """Plain build_demo (real clock) with the tracker's background threads: start, detect, stop."""
    runtime = web.build_demo(tmp_path, 1.0, out=io.StringIO())
    try:
        assert runtime.app.demo is True and runtime.app.secrets == []
        runtime.start()
        assert runtime.tracker.running

        def participant_surge_analysed() -> bool:
            # Any verdict: which one the threaded start-up reaches is timing-dependent (see the race test below).
            return any(s["exchange_id"] == PARTICIPANT_EID and s["attribution"] for s in runtime.app.surges()["surges"])

        assert _wait_for(participant_surge_analysed, timeout=12.0), runtime.app.status()["tracker"]

        def caught_up() -> bool:  # a full backfill plus a cycle after it (the loop runs every second)
            tracker = runtime.app.status()["tracker"]
            bands = {b["exchange_id"] for b in runtime.app.highband()["bands"]}
            return tracker["backfill"]["complete"] and tracker["cycles"] >= 2 and BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END <= bands

        assert _wait_for(caught_up, timeout=10.0), runtime.app.status()["tracker"]
        status = runtime.app.status()
        assert status["tracker"]["running"] is True and status["tracker"]["fatal_error"] is None
        assert status["counts"]["outcomes"] == 22
    finally:
        runtime.app.wait_backtest(10)
        runtime.close()
    assert not runtime.tracker.running
    assert _wait_for(lambda: not _tracker_threads(), timeout=5.0)


RACE_T0 = 1_791_095_911.0  # a start time seen to hit the race in the threaded runtime


def test_surges_seen_before_the_backfill_finishes_are_not_misattributed(tmp_path: Path) -> None:
    runtime, clock = build_frozen_demo(tmp_path, RACE_T0)
    try:
        tracker = runtime.tracker
        tracker.run_once()
        assert tracker.backfill_step(22) == 22  # the hourly pass for every outcome; the 5-minute pass is still pending
        clock.advance(1)
        tracker.run_once()  # what the loop thread does while the backfill worker keeps going
        tracker.analyze_pending(20)
        while tracker.backfill_step(50):
            pass
        for _ in range(3):
            clock.advance(30)
            tracker.run_once()
            tracker.analyze_pending(20)
        surge = next(s for s in runtime.app.surges()["surges"] if s["exchange_id"] == PARTICIPANT_EID)
        assert surge["start_ts"] >= RACE_T0 - 2 * 3600  # the spike happened 20 minutes before the start
        assert surge["attribution"]["verdict"] == "participants"
    finally:
        runtime.app.wait_backtest(10)
        runtime.close()


def _dashboard_args(tmp_path: Path, *extra: str) -> Any:
    return build_parser().parse_args(
        ["--data-dir", str(tmp_path / "data"), "--env-file", str(tmp_path / "missing.env"), "dashboard", *extra]
    )


def test_run_dashboard_port_in_use_exits_cleanly(tmp_path: Path) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    out, err = io.StringIO(), io.StringIO()
    try:
        code = web.run_dashboard(None, _dashboard_args(tmp_path, "--demo", "--no-browser", "--port", str(port)), out=out, err=err)
    finally:
        blocker.close()
    assert code == 1
    message = err.getvalue()
    assert f"port {port} is already in use" in message and "--port 0" in message
    assert "Traceback" not in message and "Dashboard running" not in out.getvalue()
    assert _wait_for(lambda: not _tracker_threads(), timeout=5.0)


def test_run_dashboard_port_zero_prints_the_url_and_needs_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERMARKET_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env next to us either
    servers: List[web.DashboardServer] = []
    real_make_server = web.make_server

    def capture(app: web.DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> web.DashboardServer:
        srv = real_make_server(app, host, port)
        servers.append(srv)
        return srv

    opened: List[str] = []
    monkeypatch.setattr(web, "make_server", capture)
    monkeypatch.setattr(web.webbrowser, "open", lambda url, new=0, autoraise=True: opened.append(url) or True)
    out, err = io.StringIO(), io.StringIO()
    result: Dict[str, Any] = {}
    args = _dashboard_args(tmp_path, "--demo", "--port", "0", "--interval", "5")

    def run() -> None:
        result["code"] = web.run_dashboard(None, args, out=out, err=err)

    thread = threading.Thread(target=run, name="test-run-dashboard", daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: "Dashboard running at" in out.getvalue(), timeout=10.0), err.getvalue()
        match = re.search(r"Dashboard running at (http://127\.0\.0\.1:(\d+))", out.getvalue())
        assert match, out.getvalue()
        url, port = match.group(1), int(match.group(2))
        assert port != 0 and port == servers[0].port
        assert f"Picked free port {port}." in out.getvalue()
        assert _wait_for(lambda: "Demo mode: simulated market data, no API key used." in out.getvalue(), timeout=5.0)
        assert _wait_for(lambda: opened == [url], timeout=5.0)
        resp = HTTP(port).get("/api/status")
        assert resp.status == 200 and resp.json()["demo"] is True
        assert (tmp_path / "data" / "demo" / "tracker.sqlite3").exists()
    finally:
        if servers:
            servers[0].shutdown()
        thread.join(15)
    assert not thread.is_alive()
    assert result["code"] == 0
    assert "error" not in err.getvalue().lower()
    assert _wait_for(lambda: not _tracker_threads(), timeout=5.0)


def test_run_dashboard_without_a_key_outside_demo_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERMARKET_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    assert web.run_dashboard(None, _dashboard_args(tmp_path, "--no-browser", "--port", "0"), out=out, err=err) == 2
    assert "SUPERMARKET_API_KEY" in err.getvalue() and out.getvalue() == ""


@pytest.mark.parametrize(
    "overrides, message",
    [({"interval": 0.5}, "--interval"), ({"interval": float("nan")}, "--interval"), ({"port": 70000}, "--port"), ({"port": -1}, "--port")],
)
def test_run_dashboard_rejects_bad_arguments(tmp_path: Path, overrides: Dict[str, Any], message: str) -> None:
    args = _dashboard_args(tmp_path, "--demo", "--no-browser")
    for key, value in overrides.items():
        setattr(args, key, value)
    err = io.StringIO()
    assert web.run_dashboard(None, args, out=io.StringIO(), err=err) == 2
    assert message in err.getvalue()


# --------------------------------------------------------------------------- QA round 1 regressions (server)


class GatedBookClient:
    """An order-book read that blocks until released: a slow upstream (Retry-After, timeouts, a busy budget)."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def get_exchange_orderbook(self, exchange_id: str, depth: int = 20, tournament_id: Any = None) -> Dict[str, Any]:
        with self._lock:
            self.calls += 1
        assert self.gate.wait(30), "the test never released the order book"
        return {"exchangeId": exchange_id, "bids": [{"price": 0.43, "quantity": 120}], "asks": [{"price": 0.45, "quantity": 80}]}


def test_robustness_3_exchange_detail_never_waits_for_a_slow_order_book(
    make_app: Callable[..., web.DashboardApp], store: TrackerStore
) -> None:
    assert 0 < web.BOOK_WAIT_S <= 1.0  # the most a detail request ever waits for the book
    clock = DemoClock(NOW)
    slow = GatedBookClient()
    store.record_surge(make_surge("e1", "m1"))
    app = make_app(client=slow, clock=clock, book_wait=0.05)
    try:
        started = time.monotonic()
        data = app.exchange("e1")
        assert time.monotonic() - started < 3.0
        assert data["book"] is None and data["book_pending"] is True and data["book_fetched_at"] is None
        assert data["book_error"] == "Loading the order book…"
        assert [p[2] for p in data["series"]] == ["tick"] * 5  # the local data comes straight away
        assert len(data["surges"]) == 1
        for _ in range(3):  # polls while the read is still running share the one fetch
            assert app.exchange("e1")["book_pending"] is True
        with serve(app) as srv:
            started = time.monotonic()
            resp = HTTP(srv.port).get("/api/exchange/e1")
            assert resp.status == 200 and time.monotonic() - started < 3.0  # well inside the UI's 15 s timeout
            assert resp.json()["book_pending"] is True and resp.json()["series"]
        assert slow.calls == 1
        slow.gate.set()
        assert app.wait_books(10)
        data = app.exchange("e1")
        assert data["book_pending"] is False and data["book_error"] is None and data["book_fetched_at"] == NOW
        assert data["book"]["best_bid"] == 0.43 and data["book"]["best_ask"] == 0.45
        clock.advance(web.BOOK_TTL_S - 1)
        assert app.exchange("e1")["book"] == data["book"] and slow.calls == 1  # cached for 15 s
        clock.advance(2)
        slow.gate.clear()
        stale = app.exchange("e1")  # expired: refreshed in the background; the previous book meanwhile
        assert stale["book_pending"] is True and stale["book_stale"] is True
        assert stale["book"] == data["book"] and stale["book_fetched_at"] == NOW
    finally:
        slow.gate.set()
        assert app.wait_books(10)
    assert slow.calls == 2
    fresh = app.exchange("e1")
    assert fresh["book_pending"] is False and fresh["book_stale"] is False
    assert fresh["book_fetched_at"] == NOW + web.BOOK_TTL_S + 1


def test_robustness_5_a_wildcard_bind_accepts_requests_from_other_devices(
    make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "machine_addresses", lambda ipv6=False: ["192.0.2.2"])
    monkeypatch.setattr(web.socket, "gethostname", lambda: "VM")
    with serve(make_app(), host="0.0.0.0") as srv:
        port = srv.port
        assert srv.local_only is False and srv.allow_any_host is True
        assert srv.url == f"http://127.0.0.1:{port}"
        assert srv.network_urls == [f"http://192.0.2.2:{port}"]
        assert {f"192.0.2.2:{port}", f"vm:{port}"} <= srv.allowed_hosts
        http = HTTP(port)
        for host in (f"127.0.0.1:{port}", f"192.0.2.2:{port}", f"VM:{port}", f"my-laptop.local:{port}", f"[2001:db8::5]:{port}"):
            resp = http.get("/api/health", headers={"Host": host})
            assert resp.status == 200, (host, resp.body)
            resp = http.post("/api/surges/999/analyze", Host=host, Origin=f"http://{host.lower()}")
            assert resp.status == 404, (host, resp.body)  # past the host and origin checks; the surge just does not exist
        assert_json_error(http.post("/api/surges/999/analyze", Host=f"192.0.2.2:{port}", Origin="http://evil.example"), 403)
        for bad in ("", f"127.0.0.1:{port}@evil.example", "a b", "x" * 300):
            assert_json_error(http.get("/api/health", headers={"Host": bad}), 421)
    with serve(make_app()) as srv:  # the default loopback bind still turns other names away (DNS rebinding)
        assert srv.local_only is True and srv.allow_any_host is False and srv.network_urls == []
        assert_json_error(HTTP(srv.port).get("/api/health", headers={"Host": f"192.0.2.2:{srv.port}"}), 421)
    strict = web.make_server(make_app(), "0.0.0.0", 0, allow_any_host=False)  # a wildcard bind locked to this machine
    try:
        assert strict.host_allowed(f"192.0.2.2:{strict.port}") and strict.host_allowed(f"vm:{strict.port}")
        assert not strict.host_allowed(f"evil.example:{strict.port}")
    finally:
        strict.server_close()
    assert web.is_loopback_host("127.0.0.2") and web.is_loopback_host("[::1]") and web.is_loopback_host("localhost")
    assert not web.is_loopback_host("0.0.0.0") and not web.is_loopback_host("192.168.1.5") and not web.is_loopback_host("")


def test_robustness_5_run_dashboard_prints_the_lan_url_and_an_accurate_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "machine_addresses", lambda ipv6=False: ["192.0.2.2"])
    servers: List[web.DashboardServer] = []
    real_make_server = web.make_server

    def capture(app: web.DashboardApp, host: str = "127.0.0.1", port: int = 8765) -> web.DashboardServer:
        srv = real_make_server(app, host, port)
        servers.append(srv)
        return srv

    monkeypatch.setattr(web, "make_server", capture)
    out, err = io.StringIO(), io.StringIO()
    result: Dict[str, Any] = {}
    args = _dashboard_args(tmp_path, "--demo", "--no-browser", "--port", "0", "--host", "0.0.0.0")

    def run() -> None:
        result["code"] = web.run_dashboard(None, args, out=out, err=err)

    thread = threading.Thread(target=run, name="test-run-dashboard-lan", daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: "From other devices" in out.getvalue(), timeout=10.0), (out.getvalue(), err.getvalue())
        port = servers[0].port
        assert f"Dashboard running at http://127.0.0.1:{port}" in out.getvalue()
        assert f"From other devices on your network: http://192.0.2.2:{port}" in out.getvalue()
        warning = err.getvalue()
        assert "listening on 0.0.0.0" in warning and "DNS-rebinding" in warning and "read-only" in warning
        assert HTTP(port).get("/api/health", headers={"Host": f"192.0.2.2:{port}"}).status == 200
    finally:
        if servers:
            servers[0].shutdown()
        thread.join(15)
    assert result.get("code") == 0
    assert _wait_for(lambda: not _tracker_threads(), timeout=5.0)


class StatusTracker(FakeTracker):
    """A FakeTracker that also answers ``status()`` (which keeps working when ``view()`` fails)."""

    def __init__(self, view: Any, status: Mapping[str, Any]) -> None:
        super().__init__(view)
        self._status = dict(status)

    def status(self) -> Dict[str, Any]:
        return copy.deepcopy(self._status)


def test_robustness_9_a_failing_view_keeps_the_last_good_data(
    make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SUPERMARKET_CUP_END", raising=False)
    live = {"running": True, "cycles": 7, "last_snapshot_at": NOW - 5, "markets_updated_at": NOW - 60}
    view = {
        "exchanges": [
            {"exchange_id": "e1", "market_id": "m1", "title": "Will Democrats win the Maine Senate race?", "bid": 0.43, "ask": 0.45},
            {"exchange_id": "e2a", "market_id": "m2", "title": "Who will win the Springfield mayoral race?", "bid": 0.59, "ask": 0.61},
        ],
        "context": {
            "balance": 80_000.0, "initial_balance": 100_000.0,
            "leaderboard": {"top": [{"rank": 1, "username": "alice", "value": 130_000.0}], "my_rank": 40, "leader_value": 130_000.0},
        },
        "status": dict(live, cycles=6),
    }
    tracker = StatusTracker(view, live)
    app = make_app(tracker)
    good = app.status()
    assert good["view_error"] is None and good["counts"]["outcomes"] == 2 and good["view_updated_at"] == NOW
    tracker.fail = RuntimeError("view exploded")
    status = app.status()
    assert status["view_error"] == "view exploded"
    assert status["counts"] == good["counts"]  # the last good rows, not "0 outcomes in 0 markets"
    assert status["account"] == good["account"]  # balance, rank and leader stay
    assert status["view_updated_at"] == NOW
    assert status["tracker"]["running"] is True and status["tracker"]["cycles"] == 7  # tracker.status() is still read
    assert status["tracker"]["last_snapshot_at"] == NOW - 5  # not "Starting… / not yet"
    assert [r["exchange_id"] for r in app.markets()["rows"]] == ["e1", "e2a"]
    report = app.strategy()
    assert report["available"] is True and report["balance"] == 80_000.0 and report["my_rank"] == 40
    expected = strategy_mod.risk_mode(80_000.0, 100_000.0, 130_000.0, 40, report["days_left"])
    assert report["risk_mode"] == expected == "aggressive"  # not a flip to balanced


def test_live_api_2_current_problems_and_detection_are_exposed(make_app: Callable[..., web.DashboardApp]) -> None:
    problems = [
        {"source": "prices", "message": f"HTTP 503 SERVICE_UNAVAILABLE: busy (GET /exchanges/prices) {FAKE_KEY}",
         "since": NOW - 300, "last": NOW - 5, "count": 31, "severity": "warning"},
        {"source": "leaderboard", "message": "HTTP 403 FORBIDDEN: not visible", "since": NOW - 900, "last": NOW - 600,
         "count": 2, "severity": "error"},
        {"source": "balance", "message": "x" * 1000, "since": "2026-10-01T00:00:00Z", "last": None, "count": "junk", "severity": "LOUD"},
        "junk",
        None,
        {"no": "source or message"},
    ]
    detection = {"enabled": True, "waiting_for_history": 12, "reason": "Waiting for price history on 12 outcomes"}
    app = make_app(FakeTracker({"status": {"problems": problems, "detection": detection, "errors": 34}}), secrets=[FAKE_KEY])
    status = app.status()
    got = status["problems"]
    assert [p["source"] for p in got] == ["leaderboard", "prices", "balance"]  # errors first, then the newest
    assert status["tracker"]["problems"] == got
    assert {k: got[1][k] for k in ("since", "last", "count", "severity")} == {
        "since": NOW - 300, "last": NOW - 5, "count": 31, "severity": "warning"
    }
    assert got[2]["since"] == iso_epoch("2026-10-01T00:00:00Z") and got[2]["last"] is None
    assert got[2]["count"] == 1 and got[2]["severity"] == "warning" and len(got[2]["message"]) <= 300
    assert status["detection"] == detection and status["tracker"]["detection"] == detection
    text = web.encode_json(status, [FAKE_KEY]).decode("utf-8")
    assert FAKE_KEY not in text and "busy (GET /exchanges/prices) ***" in text
    older = make_app(FakeTracker({"status": {"errors": 3}})).status()  # a tracker that predates the fields
    assert older["problems"] == [] and older["detection"] is None


def test_live_api_2_a_rate_limit_pause_is_reported(make_app: Callable[..., web.DashboardApp]) -> None:
    limiter = SlidingWindowLimiter(90)
    limiter.acquire()
    app = make_app(FakeTracker({"status": {}}), client=SimpleNamespace(read_limiter=limiter))
    calm = app.status()
    assert calm["tracker"]["read_budget"]["paused_for"] == 0 and calm["problems"] == []
    limiter.pause(20)
    status = app.status()
    budget = status["tracker"]["read_budget"]
    assert budget["used"] == 1 and budget["limit"] == 90 and 15 <= budget["paused_for"] <= 20
    [problem] = status["problems"]
    assert problem["source"] == "rate limit" and problem["severity"] == "warning"
    assert "Rate limited" in problem["message"] and "20 s" in problem["message"]


def test_live_api_3_a_failing_market_list_is_not_reported_as_a_fresh_snapshot(
    make_app: Callable[..., web.DashboardApp], make_client: Callable[..., SuperMarketClient], fake: Any, store: TrackerStore
) -> None:
    fake.add("GET", "/tournaments/cup/markets", (503, error_body("SERVICE_UNAVAILABLE", "The market list is down")))
    client = make_client(max_retries=0)
    tracker = tracker_mod.Tracker(client, Context("t-1", "cup", "Cup"), store, backfill=False, clock=lambda: NOW)
    tracker.run_once()
    raw = tracker.status()
    assert raw["last_snapshot_at"] is None and raw["last_cycle_at"] == NOW and raw["fatal_error"] is None
    status = make_app(tracker, client=client).status()
    assert status["tracker"]["last_snapshot_at"] is None  # not the cycle time: nothing was snapshotted
    assert status["tracker"]["cycles"] == 1 and status["tracker"]["errors"] >= 1
    assert status["counts"]["outcomes"] == 0
    if raw.get("problems") is not None:  # the tracker reports its current failures (contract item 3)
        assert any("SERVICE_UNAVAILABLE" in p["message"] for p in status["problems"])
    summary = web.tracker_summary({"last_snapshot_at": None, "last_cycle_at": NOW, "cycles": 3})
    assert summary["last_snapshot_at"] is None and summary["last_cycle_at"] == NOW


@pytest.mark.parametrize(
    "backfill, complete",
    [
        ({"total": 80, "done": 78, "failed": 2, "pending": 0}, True),  # the QA case: 2 series gave up
        ({"total": 740, "done": 0, "failed": 740, "pending": 0}, True),  # price history down for good
        ({"total": 6, "done": 4, "failed": 1, "pending": 1}, False),
        ({"total": 6, "done": 6, "failed": 0, "pending": 0}, True),
        ({"done": 3, "pending": 2}, False),
        ({"total": 6, "done": 4, "failed": 2, "pending": 0, "complete": False}, False),  # an explicit flag wins
    ],
)
def test_live_api_5_backfill_counts_failed_series_as_finished(backfill: Dict[str, Any], complete: bool) -> None:
    summary = web.tracker_summary({"backfill": backfill})["backfill"]
    assert summary["complete"] is complete
    assert summary["failed"] == (float(backfill["failed"]) if "failed" in backfill else None)
    assert summary["pending"] == float(backfill["pending"])


def test_live_api_5_a_real_backfill_that_gave_up_is_complete(
    make_app: Callable[..., web.DashboardApp], make_client: Callable[..., SuperMarketClient], fake: Any, store: TrackerStore
) -> None:
    title = "Will Republicans win the Arizona Senate race?"
    fake.add("GET", "/tournaments/cup/markets", market_page([api_market("m9", title, [("e9", "YES", 0.5)])]))
    fake.add("GET", "/exchanges/prices", {"data": [api_price("e9", "m9", 0.5, 0.49, 0.51)], "missingIds": []})
    fake.add("GET", "/exchanges/e9/price-history", (404, error_body("NOT_FOUND", "no history for this exchange")))
    client = make_client(max_retries=0)
    tracker = tracker_mod.Tracker(client, Context("t-1", "cup", "Cup"), store, clock=lambda: NOW, backfill_reads_per_min=1000)
    tracker.run_once()
    while tracker.backfill_step(10):
        pass
    raw = tracker.status()["backfill"]
    assert (raw["total"], raw["done"], raw["failed"], raw["pending"]) == (2, 0, 2, 0)
    summary = make_app(tracker, client=client).status()["tracker"]["backfill"]
    assert summary == {"done": 0.0, "total": 2.0, "failed": 2.0, "pending": 0.0, "complete": True}


def _crowd_surge(eid: str, mid: str, **kw: Any) -> Surge:
    surge = make_surge(eid, mid, **kw)
    surge.attribution = Attribution(
        verdict="participants", confidence=0.8, reversion_odds=0.75, summary="Two large trades and no news", analyzed_at=NOW - 30
    )
    return surge


def test_live_api_7_surges_on_closed_markets_are_closed_and_yield_no_ideas(
    make_app: Callable[..., web.DashboardApp], store: TrackerStore
) -> None:
    # m1 (e1) closed after its surge; m2 (e2a, e2b) is still open. Both surges are "open" in the store.
    closed = store.record_surge(_crowd_surge("e1", "m1"))
    still_open = store.record_surge(_crowd_surge("e2a", "m2", start_price=0.45, end_price=0.60, peak_price=0.61, change=0.15))
    rows = [
        {"exchange_id": "e2a", "market_id": "m2", "title": "Who will win the Springfield mayoral race?", "option": "Alice Smith",
         "bid": 0.59, "ask": 0.61},
        {"exchange_id": "e2b", "market_id": "m2", "title": "Who will win the Springfield mayoral race?", "option": "Bob Jones"},
    ]
    surges = [s.to_dict() for s in (closed, still_open)]
    app = make_app(FakeTracker({"exchanges": rows, "surges": surges, "status": {"markets_updated_at": NOW - 60}}))
    by_eid = {s["exchange_id"]: s for s in app.surges()["surges"]}
    assert by_eid["e1"]["status"] == web.SURGE_CLOSED == "closed" and by_eid["e1"]["market_open"] is False
    assert by_eid["e2a"]["status"] == "open" and by_eid["e2a"]["market_open"] is True
    assert app.status()["counts"]["open_participant_surges"] == 1  # only the open market's surge
    detail = app.exchange("e1")
    assert detail is not None and detail["market_open"] is False
    assert [s["status"] for s in detail["surges"]] == ["closed"]
    assert app.exchange("e2a")["market_open"] is True
    opps = app.strategy()["opportunities"]
    assert all(o.get("exchange_id") != "e1" for o in opps)  # nothing priced off the closed market's last tick
    assert [(o["kind"], o["exchange_id"], o["surge_id"]) for o in opps if o["kind"] in ("fade", "watch")] == [
        ("fade", "e2a", still_open.id)
    ]
    # Before the open-market list ever loaded nothing is relabelled: the dashboard cannot tell yet.
    early = make_app(FakeTracker({"exchanges": [], "surges": surges, "status": {}}))
    assert {s["status"] for s in early.surges()["surges"]} == {"open"}
    assert {s["market_open"] for s in early.surges()["surges"]} == {None}
    # A view that cannot be read at all (and no earlier one) says nothing about which markets are open.
    broken = StatusTracker({}, {"markets_updated_at": NOW - 60})
    broken.fail = RuntimeError("view exploded")
    assert {s["status"] for s in make_app(broken).exchange("e1")["surges"]} == {"open"}
    # A surge the tracker already marked closed passes through as it is.
    marked = dict(surges[0], status="closed")
    passed = make_app(FakeTracker({"exchanges": rows, "surges": [marked], "status": {}})).surges()["surges"]
    assert [s["status"] for s in passed] == ["closed"]


def test_functional_4_multi_leg_arbitrage_keeps_its_legs(
    make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    senate = "Which party will control the Senate after the midterms?"
    base = {
        "target_price": None, "stop_price": None, "prob_win": 1.0, "edge": 0.03, "expected_return": 0.031, "horizon_hours": None,
        "suggested_shares": 100, "suggested_cost": 97.0, "score": 0.5, "confidence": 0.9, "rationale": [], "risks": [],
    }
    report = {
        "generated_at": NOW, "risk_mode": "balanced", "headline": "Balanced mode.",
        "assumptions": ["Balance unknown: sized on the 100,000 starting balance", None, ""],
        "opportunities": [
            dict(base, kind="arbitrage", exchange_id=None, market_id=313, title=senate, option=None, side="no", entry_price=0.97, legs=[
                {"exchange_id": 9016, "market_id": 313, "title": senate, "option": "Republicans", "side": "NO", "price": 0.295},
                {"exchange_id": 9017, "market_id": 313, "title": senate, "option": "Democrats", "side": "no", "price": float("nan")},
                "junk",
            ]),
            dict(base, kind="carry", exchange_id=9007, market_id="301", title="California Governor", option="YES", side="yes",
                 entry_price=0.97, legs=None),
        ],
    }
    monkeypatch.setattr(strategy_mod, "build_report", lambda **kwargs: copy.deepcopy(report))
    data = assert_json_safe(make_app(FakeTracker({})).strategy())
    arb, carry = data["opportunities"]
    assert arb["exchange_id"] is None and arb["market_id"] == "313"
    assert [(leg["exchange_id"], leg["market_id"], leg["option"], leg["side"], leg["price"]) for leg in arb["legs"]] == [
        ("9016", "313", "Republicans", "no", 0.295),
        ("9017", "313", "Democrats", "no", None),
    ]
    assert carry["exchange_id"] == "9007" and carry["legs"] is None
    assert data["assumptions"] == ["Balance unknown: sized on the 100,000 starting balance"]


def test_functional_4_demo_constraint_arbitrage_is_served_as_legs(client: HTTP, pipe: SimpleNamespace) -> None:
    pipe.app.wait_backtest(10)
    report = client.get("/api/strategy").json()
    senate = [o for o in report["opportunities"] if o["kind"] == "arbitrage" and o["market_id"] == "313"]
    assert senate, [o["market_id"] for o in report["opportunities"]]
    legs = senate[0]["legs"]
    assert len(legs) >= 2  # buy NO on Republicans AND on Democrats, each at its own price
    for leg in legs:
        assert isinstance(leg["exchange_id"], str) and leg["market_id"] == "313"
        assert leg["side"] in ("yes", "no") and finite_or_none(leg["price"]) and leg["price"] is not None
    assert {leg["exchange_id"] for leg in legs} >= {"9016", "9017"}
    assert all(isinstance(a, str) for a in report.get("assumptions") or [])


def test_visual_9_chart_series_spreads_over_time_not_tick_count() -> None:
    from supermarket_bot.models import PricePoint as PP

    now = 1_800_000_000.0
    # 7 days of hourly candles, then 6 hours of 5-second live ticks (4,320 points)
    pts = [PP(now - 7 * 86400 + i * 3600, 0.40, source="candle") for i in range(7 * 24 - 6)]
    pts += [PP(now - 6 * 3600 + i * 5, 0.45, source="tick") for i in range(6 * 720)]
    out = web.tiered_series(pts, now)
    assert len(out) <= web.MAX_SERIES_POINTS
    span_24h = [p for p in out if p.ts >= now - 86400]
    hours_covered = {int((p.ts - (now - 86400)) // 3600) for p in span_24h}
    assert len(hours_covered) >= 20  # the whole day is represented, not just the tick-dense last hours
