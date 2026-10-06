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
import supermarket_bot.moves as moves_mod
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
    "/api/paper", "/api/backtest", "/api/fairvalue",  # docs/PAPER_TRADING.md §7.4
    "/api/moves", "/api/moves?state=open&limit=5",  # docs/OUTSIDE_MOVES.md §18.2
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
        runtime.app.wait_paper_backtest(60)
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
    # docs/PAPER_TRADING.md §10.5: the paper trader, fair-value mode, sizing and regime were added.
    assert status["features"] == {
        "analysis": True, "news": True, "llm": False, "paper": True, "fair_value": "auto", "sizing": "conservative",
        "regime": "unknown", "moves": False,  # docs/OUTSIDE_MOVES.md §18.3: build_demo() without moves=True
    }
    assert status["moves"] is None and status["tracker"]["read_budget"]["moves"] is None

    counts = status["counts"]
    assert COUNT_KEYS <= set(counts)
    assert counts["outcomes"] == 33  # 34 demo outcomes (11 added for the paper trader), one market already settled
    assert counts["markets"] == 25
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
    assert (backfill["done"], backfill["total"], backfill["complete"]) == (66.0, 66.0, True)  # 33 outcomes x (1h, 5m)
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
    assert data["total"] == data["count"] == 33 and data["q"] == "" and data["now"] == pipe.clock.now
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
    assert data["q"] == q and data["total"] == 33
    assert {r["exchange_id"] for r in data["rows"]} == expected
    assert data["count"] == len(data["rows"])


@pytest.mark.parametrize("q", [None, "", "   "])
def test_markets_blank_query_returns_everything(pipe: SimpleNamespace, q: Optional[str]) -> None:
    data = pipe.app.markets(q)
    assert data["count"] == data["total"] == 33


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
    assert 4 <= len(bands) <= 6  # + the Nebraska Democratic long shot (NO near 0.97) added for the paper trader
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
        # docs/PAPER_TRADING.md §5: value, basket and hole ideas were added
        assert opp["kind"] in ("fade", "carry", "arbitrage", "watch", "value", "basket", "hole")
        assert opp["side"] in ("yes", "no")
        assert opp["exchange_id"] is None or isinstance(opp["exchange_id"], str)
        assert isinstance(opp["suggested_shares"], int) and opp["suggested_shares"] >= 0
        assert opp["suggested_cost"] <= report["balance"] * 0.08 + 1e-6  # max position size
    fades = [o for o in opps if o["kind"] == "fade"]
    assert [(o["exchange_id"], o["side"], o["surge_id"]) for o in fades] == [
        (PARTICIPANT_EID, "no", surges_by_eid[PARTICIPANT_EID]["id"])  # fade the up-spike: buy NO
    ]
    arbitrage = {o["market_id"] for o in opps if o["kind"] == "arbitrage"}
    sets = arbitrage | {o["market_id"] for o in opps if o["kind"] == "basket"}
    # Arizona asks sum below 1; the Senate-control violation (YES bids above 1) is now its NO basket (§5.3)
    assert "311" in arbitrage and "313" in sets
    assert report["sizing_policy"] == "conservative" and isinstance(report["sizing"], dict)
    assert report["settlement_regime"] == "unknown"
    assert report["fair_value"] is None or report["fair_value"]["mode"] == "auto"
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
    pipe.app.ensure_backtest()  # [integration] also when this test runs on its own (-k): start it if no earlier test did
    pipe.app.wait_backtest(10)
    first = pipe.app.strategy()
    assert pipe.app.strategy() is first  # cached for 30 s of (frozen) time
    assert first["backtest_status"] == "ready" and first["backtest_error"] is None
    assert isinstance(first["backtest"], dict) and isinstance(first["backtest"]["n_surges"], int)


def test_fade_backtest_gets_the_stored_surges(pipe: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """[integration, B -> E] the dashboard's fade backtest passes the stored, attributed surges, so the
    participant-only reversion rate that caps a fade's win chance is measured (§5.5)."""
    seen: Dict[str, Any] = {}
    real = strategy_mod.backtest_fade

    def spy(series: Any, *args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(series, *args, **kwargs)

    monkeypatch.setattr(strategy_mod, "backtest_fade", spy)
    pipe.app._run_backtest()
    surges = seen.get("surges")
    assert surges is not None and len(surges) >= 1
    assert {s.exchange_id for s in surges} <= {e.exchange_id for e in pipe.store.exchanges()}


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
    assert data["total"] == 33
    if expected is None:
        assert len(ids) == 33
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
            assert data["count"] == 33
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
        # … and reached an error: as it is (an older client) or already masked where the error was made.
        assert any(FAKE_KEY in str(e) or "key *** is not allowed" in str(e) for e in runtime.tracker.status()["recent_errors"])
        with serve(runtime.app) as srv:
            http_client = HTTP(srv.port)
            bodies = [http_client.get(p).body for p in GET_ENDPOINTS]
            runtime.app.wait_backtest(10)
            runtime.app.wait_paper_backtest(60)
        for body in bodies:
            assert FAKE_KEY.encode() not in body
            assert b"Bearer" not in body
        status = json.loads(bodies[1])
        assert status["demo"] is False and status["counts"]["outcomes"] == 33
        assert "***" in status["tracker"]["last_error"]
        assert (tmp_path / demo_mod.DEMO_SLUG / "tracker.sqlite3").exists()
    finally:
        runtime.app.wait_backtest(10)
        runtime.app.wait_paper_backtest(60)
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
        assert status["counts"]["outcomes"] == 33
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
        assert tracker.backfill_step(33) == 33  # the hourly pass for every outcome; the 5-minute pass is still pending
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

    def capture(app: web.DashboardApp, host: str = "127.0.0.1", port: int = 8765, **kwargs: Any) -> web.DashboardServer:
        srv = real_make_server(app, host, port, **kwargs)
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


def _fixed_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """This machine is "VM" (FQDN vm.lan.example) at 192.0.2.2, whatever the test host really is."""
    monkeypatch.setattr(web, "machine_addresses", lambda ipv6=False: ["192.0.2.2"])
    monkeypatch.setattr(web.socket, "gethostname", lambda: "VM")
    monkeypatch.setattr(web.socket, "getfqdn", lambda name="": "vm.lan.example")


def test_robustness_5_a_wildcard_bind_accepts_requests_from_other_devices(
    make_app: Callable[..., web.DashboardApp], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_machine(monkeypatch)
    with serve(make_app(), host="0.0.0.0") as srv:
        port = srv.port
        assert srv.local_only is False and srv.allow_any_host is False  # r2-fixcheck-16: the check stays on
        assert srv.url == f"http://127.0.0.1:{port}"
        assert srv.network_urls == [f"http://192.0.2.2:{port}"]
        assert {f"192.0.2.2:{port}", f"vm:{port}", f"vm.local:{port}", f"vm.lan.example:{port}"} <= srv.allowed_hosts
        http = HTTP(port)
        for host in (f"127.0.0.1:{port}", f"192.0.2.2:{port}", f"VM:{port}", f"vm.local:{port}", f"vm.lan.example:{port}"):
            resp = http.get("/api/health", headers={"Host": host})
            assert resp.status == 200, (host, resp.body)
            resp = http.post("/api/surges/999/analyze", Host=host, Origin=f"http://{host.lower()}")
            assert resp.status == 404, (host, resp.body)  # past the host and origin checks; the surge just does not exist
        assert_json_error(http.post("/api/surges/999/analyze", Host=f"192.0.2.2:{port}", Origin="http://evil.example"), 403)
        for bad in ("", f"127.0.0.1:{port}@evil.example", "a b", "x" * 300, f"my-laptop.local:{port}", f"[2001:db8::5]:{port}"):
            assert_json_error(http.get("/api/health", headers={"Host": bad}), 421)
    with serve(make_app()) as srv:  # the default loopback bind still turns other names away (DNS rebinding)
        assert srv.local_only is True and srv.allow_any_host is False and srv.network_urls == []
        assert_json_error(HTTP(srv.port).get("/api/health", headers={"Host": f"192.0.2.2:{srv.port}"}), 421)
        assert_json_error(HTTP(srv.port).get("/api/health", headers={"Host": f"vm:{srv.port}"}), 421)
    assert web.is_loopback_host("127.0.0.2") and web.is_loopback_host("[::1]") and web.is_loopback_host("localhost")
    assert not web.is_loopback_host("0.0.0.0") and not web.is_loopback_host("192.168.1.5") and not web.is_loopback_host("")


def test_robustness_5_run_dashboard_prints_the_lan_url_and_an_accurate_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "machine_addresses", lambda ipv6=False: ["192.0.2.2"])
    servers: List[web.DashboardServer] = []
    real_make_server = web.make_server

    def capture(app: web.DashboardApp, host: str = "127.0.0.1", port: int = 8765, **kwargs: Any) -> web.DashboardServer:
        srv = real_make_server(app, host, port, **kwargs)
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
        assert "is off" not in warning and "could also read" not in warning  # r2-fixcheck-16: no longer true
        assert HTTP(port).get("/api/health", headers={"Host": f"192.0.2.2:{port}"}).status == 200
        assert_json_error(HTTP(port).get("/api/health", headers={"Host": f"rebind.attacker.example:{port}"}), 421)
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
    # web builds the report with strategy.report_from_inputs since docs/PAPER_TRADING.md §7.4.
    monkeypatch.setattr(strategy_mod, "report_from_inputs", lambda inputs, **kwargs: copy.deepcopy(report))
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
    # The Senate-control violation is served as a set idea: engine arbitrage, or its NO basket (§5.3).
    senate = [o for o in report["opportunities"] if o["kind"] in ("arbitrage", "basket") and o["market_id"] == "313"]
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


# --------------------------------------------------------------------------- round 2 (server)


def test_r2_fixcheck_16_a_wildcard_bind_still_blocks_dns_rebinding(
    make_app: Callable[..., web.DashboardApp], store: TrackerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--host 0.0.0.0 answers this machine's own addresses and names only; a rebinding page gets 421/403."""
    _fixed_machine(monkeypatch)
    sid = store.record_surge(make_surge("e1", "m1")).id
    tracker = FakeTracker({"context": {"balance": 98_518.75}})
    with serve(make_app(tracker), host="0.0.0.0") as srv:
        port = srv.port
        http = HTTP(port)
        evil = f"rebind.attacker.example:{port}"
        resp = assert_json_error(http.get("/api/status", headers={"Host": evil}), 421)
        assert "98518" not in json.dumps(resp)
        for path in ("/", "/api/health", "/api/markets", "/api/surges"):
            assert http.get(path, headers={"Host": evil}).status == 421, path
        # The POST a rebinding page would send (its Origin matches its own Host): refused, nothing queued.
        assert http.post(f"/api/surges/{sid}/analyze", Host=evil, Origin=f"http://{evil}").status == 421
        assert tracker.requests == []
        # This machine's own address / name, with a foreign Origin: still refused.
        for origin in (f"http://{evil}", "http://evil.example", f"http://192.0.2.2:{port + 1}", "null"):
            assert_json_error(http.post(f"/api/surges/{sid}/analyze", Host=f"192.0.2.2:{port}", Origin=origin), 403)
        assert tracker.requests == []
        ok = http.post(f"/api/surges/{sid}/analyze", Host=f"192.0.2.2:{port}", Origin=f"http://192.0.2.2:{port}")
        assert ok.status == 200 and ok.json()["queued"] is True and tracker.requests == [sid]
        assert http.get("/api/status", headers={"Host": f"vm.local:{port}"}).json()["account"]["balance"] == 98_518.75
        # The local address the connection arrived on is this machine's, on any interface.
        assert srv.host_allowed(f"198.51.100.7:{port}", "198.51.100.7")
        assert srv.host_allowed(f"198.51.100.7:{port}", "::ffff:198.51.100.7")  # a dual-stack socket
        assert srv.host_allowed(f"[2001:db8::7]:{port}", "2001:db8::7")
        assert not srv.host_allowed(f"198.51.100.7:{port}", "192.0.2.2")  # not the address it arrived on
        assert not srv.host_allowed(f"198.51.100.7:{port + 1}", "198.51.100.7")  # another port
        assert not srv.host_allowed(evil, "198.51.100.7")  # a name is never matched this way
        assert not srv.host_allowed(f"0.0.0.0:{port}", "0.0.0.0")
    # ...and through the handler: a client on another interface (198.51.100.7) uses that address.
    monkeypatch.setattr(web.DashboardHandler, "_local_address", lambda self: "198.51.100.7")
    with serve(make_app(), host="0.0.0.0") as srv:
        http = HTTP(srv.port)
        assert http.get("/api/health", headers={"Host": f"198.51.100.7:{srv.port}"}).status == 200
        assert http.get("/api/health", headers={"Host": f"203.0.113.9:{srv.port}"}).status == 421
    with serve(make_app()) as srv:  # a loopback bind never accepts a non-loopback address this way
        assert not srv.host_allowed(f"198.51.100.7:{srv.port}", "198.51.100.7")


def test_r2_fixcheck_16_allow_host_adds_names_and_allow_any_host_never_opens_posts(
    make_app: Callable[..., web.DashboardApp], store: TrackerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_machine(monkeypatch)
    sid = store.record_surge(make_surge("e1", "m1")).id
    tracker = FakeTracker({})
    srv = web.make_server(
        make_app(tracker), "127.0.0.1", 0, allow_hosts=["Dash.Example.org", "proxy.example:8443", "https://pasted.example/", "", "bad name"]
    )
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        port, http = srv.port, HTTP(srv.port)
        assert srv.extra_names == {"dash.example.org", "pasted.example"} and srv.extra_hosts == {"proxy.example:8443"}
        for host in ("dash.example.org", f"dash.example.org:{port}", "dash.example.org:9000", "proxy.example:8443", "pasted.example"):
            assert http.get("/api/health", headers={"Host": host}).status == 200, host
        for host in ("proxy.example", f"proxy.example:{port}", f"evil.dash.example.org:{port}", f"vm:{port}"):
            assert http.get("/api/health", headers={"Host": host}).status == 421, host
        # A TLS-terminating reverse proxy: https Origin for an --allow-host name only.
        assert http.post(f"/api/surges/{sid}/analyze", Host="dash.example.org", Origin="https://dash.example.org").status == 200
        assert_json_error(http.post(f"/api/surges/{sid}/analyze", Host=f"127.0.0.1:{port}", Origin=f"https://127.0.0.1:{port}"), 403)
        assert_json_error(http.post(f"/api/surges/{sid}/analyze", Host="dash.example.org", Origin="https://evil.example"), 403)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(5)
    assert tracker.requests == [sid]
    # allow_any_host (no CLI option sets it) lets reads through but never a POST from a foreign Host.
    opened = web.make_server(make_app(tracker), "0.0.0.0", 0, allow_any_host=True)
    try:
        evil = f"rebind.attacker.example:{opened.port}"
        assert opened.host_allowed(evil)
        assert not opened.origin_allowed(f"http://{evil}", evil)
        assert opened.origin_allowed(f"http://192.0.2.2:{opened.port}", f"192.0.2.2:{opened.port}")
    finally:
        opened.server_close()


def test_r2_fixcheck_16_run_dashboard_passes_allow_host_and_mentions_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "machine_addresses", lambda ipv6=False: ["192.0.2.2"])
    servers: List[web.DashboardServer] = []
    real_make_server = web.make_server

    def capture(app: web.DashboardApp, host: str = "127.0.0.1", port: int = 8765, **kwargs: Any) -> web.DashboardServer:
        srv = real_make_server(app, host, port, **kwargs)
        servers.append(srv)
        return srv

    monkeypatch.setattr(web, "make_server", capture)
    out, err = io.StringIO(), io.StringIO()
    result: Dict[str, Any] = {}
    args = _dashboard_args(tmp_path, "--demo", "--no-browser", "--port", "0", "--host", "0.0.0.0")
    args.allow_host = ["dash.example.org", "a.example,b.example", "*"]  # what a repeatable --allow-host option gives

    def run() -> None:
        result["code"] = web.run_dashboard(None, args, out=out, err=err)

    thread = threading.Thread(target=run, name="test-run-dashboard-allow-host", daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: "Dashboard running" in out.getvalue(), timeout=10.0), (out.getvalue(), err.getvalue())
        srv = servers[0]
        assert srv.extra_names == {"dash.example.org", "a.example", "b.example"} and srv.allow_any_host is False
        assert "--allow-host NAME" in err.getvalue() and "DNS-rebinding" in err.getvalue()
        assert "ignoring --allow-host '*'" in err.getvalue()  # never a wildcard that would turn the check off
        assert HTTP(srv.port).get("/api/health", headers={"Host": "b.example"}).status == 200
        assert HTTP(srv.port).get("/api/health", headers={"Host": f"rebind.attacker.example:{srv.port}"}).status == 421
    finally:
        if servers:
            servers[0].shutdown()
        thread.join(15)
    assert result.get("code") == 0
    assert web._allow_host_option(SimpleNamespace()) == []  # a CLI without the option
    assert web._allow_host_option(SimpleNamespace(allow_host="x.example")) == ["x.example"]


def test_r2_robustness_6_reanalyze_refuses_once_the_tracker_has_stopped(
    make_app: Callable[..., web.DashboardApp], make_client: Callable[..., SuperMarketClient], fake: Any, store: TrackerStore
) -> None:
    sid = store.record_surge(make_surge("e1", "m1")).id
    fake.add("GET", "/tournaments/cup/markets", (401, error_body("API_KEY_REVOKED", "This API key has been revoked.")))
    client = make_client(max_retries=0)
    tracker = tracker_mod.Tracker(client, Context("t-1", "cup", "Cup"), store, backfill=False, clock=lambda: NOW)
    tracker.attributor = object()  # anything: request_analysis only queues when there is an attributor
    with pytest.raises(ApiError):
        tracker.run_once()
    assert tracker.status()["fatal_error"]
    app = make_app(tracker, client=client, secrets=[FAKE_KEY])
    queued_before = tracker.status()["analysis"]["queued_ids"]
    status, body = app.analyze(sid)
    assert status == 409 and body["queued"] is False and body["surge_id"] == sid
    assert body["error"].startswith("The tracker has stopped (HTTP 401 API_KEY_REVOKED")
    assert body["error"].endswith("Restart the dashboard to analyse surges.")
    assert tracker.status()["analysis"]["queued_ids"] == queued_before  # nothing re-queued that would never run
    with serve(app) as srv:
        resp = HTTP(srv.port).post(f"/api/surges/{sid}/analyze")
    assert_json_error(resp, 409)
    assert "The tracker has stopped" in resp.json()["error"] and "Queued" not in resp.body.decode()
    # A tracker that was started and is no longer running (its threads ended) is stopped too.
    stopped = StatusTracker({}, {"running": False, "started_at": NOW - 60, "fatal_error": None})
    status, body = make_app(stopped).analyze(sid)
    assert status == 409 and "The tracker has stopped" in body["error"] and stopped.requests == []
    # A fatal error is masked and kept short.
    leaky = StatusTracker({}, {"running": False, "started_at": NOW, "fatal_error": f"HTTP 401 bad key {FAKE_KEY} " + "x" * 500})
    status, body = make_app(leaky, secrets=[FAKE_KEY]).analyze(sid)
    assert status == 409 and FAKE_KEY not in body["error"] and "***" in body["error"] and len(body["error"]) < 300
    # Running, or never started (driven by hand), or a tracker without status(): queued as before.
    for live in (
        StatusTracker({}, {"running": True, "started_at": NOW - 60, "fatal_error": None}),
        StatusTracker({}, {"running": False, "started_at": None, "fatal_error": None}),
        FakeTracker({}),
    ):
        status, body = make_app(live).analyze(sid)
        assert status == 200 and body["queued"] is True and live.requests == [sid]


def test_r2_live_api_11_ctrl_c_while_starting_exits_130_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from supermarket_bot import cli

    def interrupted(settings: Any, args: Any, out: Any = None, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(web, "build_live", interrupted)
    settings = Settings(api_key=FAKE_KEY, tournament="cup", data_dir=tmp_path)
    out, err = io.StringIO(), io.StringIO()
    assert web.run_dashboard(settings, _dashboard_args(tmp_path, "--no-browser", "--port", "0"), out=out, err=err) == 130
    assert "Stopped before the dashboard started." in err.getvalue() and "Traceback" not in err.getvalue()
    assert "Dashboard running" not in out.getvalue()
    # Through the CLI entry point (which returns run_dashboard's code for this command).
    monkeypatch.setenv("SUPERMARKET_API_KEY", FAKE_KEY)
    code = cli.main(["--data-dir", str(tmp_path / "data"), "--env-file", str(tmp_path / "none.env"), "dashboard", "--no-browser", "--port", "0"])
    assert code == 130
    captured = capsys.readouterr()
    assert "Stopped before the dashboard started." in captured.err and "Traceback" not in captured.err


def test_r2_live_api_11_ctrl_c_in_a_slow_api_read_closes_the_half_built_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real build_live: Ctrl-C arrives while GET /tournaments/{slug} is still waiting."""
    monkeypatch.delenv("SUPERMARKET_LLM", raising=False)
    clients: List[SuperMarketClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt  # what SIGINT does to the main thread in the middle of the read

    def from_settings(cls: Any, settings: Settings, **kwargs: Any) -> SuperMarketClient:
        made = SuperMarketClient(settings.api_key, "https://fake.invalid/api/v1", transport=httpx.MockTransport(handler), max_retries=0)
        clients.append(made)
        return made

    monkeypatch.setattr(SuperMarketClient, "from_settings", classmethod(from_settings))
    settings = Settings(api_key=FAKE_KEY, tournament="cup", data_dir=tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = web.run_dashboard(settings, _dashboard_args(tmp_path, "--no-browser", "--port", "0", "--no-news"), out=out, err=err)
    assert code == 130
    text = err.getvalue()
    assert text.startswith("Connecting to the Super Market API…")  # a wait has context
    assert "Stopped before the dashboard started." in text and "Traceback" not in text
    assert len(clients) == 1 and clients[0]._http.is_closed
    assert not (tmp_path / "cup").exists()  # no store was opened


def test_r2_live_api_11_sigterm_while_starting_also_stops_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import signal

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        pytest.skip("needs the main thread")

    class Unhandled(Exception):
        pass

    def fallback(signum: int, frame: Any) -> None:  # only reached if run_dashboard installed no handler
        raise Unhandled("SIGTERM reached the test's own handler")

    original = signal.signal(signal.SIGTERM, fallback)  # never let a regression kill the test run
    before = signal.getsignal(signal.SIGTERM)

    def slow_start(settings: Any, args: Any, out: Any = None, **kwargs: Any) -> Any:
        os.kill(os.getpid(), signal.SIGTERM)  # run_dashboard's handler turns it into KeyboardInterrupt
        time.sleep(5)
        raise AssertionError("SIGTERM did not interrupt the start-up")

    monkeypatch.setattr(web, "build_live", slow_start)
    settings = Settings(api_key=FAKE_KEY, tournament="cup", data_dir=tmp_path)
    err = io.StringIO()
    started = time.monotonic()
    try:
        assert web.run_dashboard(settings, _dashboard_args(tmp_path, "--no-browser"), out=io.StringIO(), err=err) == 130
        assert time.monotonic() - started < 4 and "Stopped before the dashboard started." in err.getvalue()
        assert signal.getsignal(signal.SIGTERM) is before  # restored
    finally:
        signal.signal(signal.SIGTERM, original)


def _flaky_tournament_api(monkeypatch: pytest.MonkeyPatch, failures: Any, status: int = 503) -> Dict[str, int]:
    """The demo API, whose GET /tournaments/{slug} fails ``failures`` times (an int, or "always")."""
    market = demo_mod.DemoMarket(seed=7)
    inner = market.transport()
    seen = {"tournament": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/tournaments/{demo_mod.DEMO_SLUG}"):
            seen["tournament"] += 1
            if failures == "always" or seen["tournament"] <= failures:
                if status == 401:
                    return httpx.Response(401, json=error_body("API_KEY_REVOKED", "This API key has been revoked."))
                return httpx.Response(
                    status, json=error_body("SERVICE_UNAVAILABLE", "Authoritative tournament balances are temporarily unavailable.")
                )
        return inner.handle_request(request)

    def from_settings(cls: Any, settings: Settings, **kwargs: Any) -> SuperMarketClient:
        return market.client(api_key=settings.api_key, transport=httpx.MockTransport(handler), reads_per_min=10_000, max_retries=0)

    monkeypatch.setattr(SuperMarketClient, "from_settings", classmethod(from_settings))
    monkeypatch.delenv("SUPERMARKET_LLM", raising=False)
    return seen


def test_r2_live_api_14_startup_rides_out_a_temporary_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _flaky_tournament_api(monkeypatch, failures=2)
    waits: List[float] = []
    settings = Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path)
    args = build_parser().parse_args(["dashboard", "--no-news"])
    out = io.StringIO()
    runtime = web.build_live(settings, args, out=out, sleep=waits.append)
    try:
        assert runtime.context.slug == demo_mod.DEMO_SLUG and runtime.context.tournament_id
        assert seen["tournament"] == 3 and waits == list(web.STARTUP_RETRY_WAITS_S[:2]) == [5.0, 10.0]
        text = out.getvalue()
        assert "The Super Market API is temporarily unavailable (HTTP 503 SERVICE_UNAVAILABLE" in text
        assert "trying again in 5 s" in text and "trying again in 10 s" in text and "Ctrl-C to stop" in text
        assert FAKE_KEY not in text
        # The resolved tournament is remembered for the next start.
        saved = runtime.store.get_state(web.CONTEXT_STATE_KEY)
        assert saved["slug"] == demo_mod.DEMO_SLUG and saved["tournament_id"] == runtime.context.tournament_id
    finally:
        runtime.close()
    assert 4.5 * 60 <= sum(web.STARTUP_RETRY_WAITS_S) <= 6 * 60  # about 5 minutes in all, not 2.6 s


def test_r2_live_api_14_a_later_start_uses_the_saved_tournament_while_the_lookup_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path)
    args = build_parser().parse_args(["dashboard", "--no-news"])
    _flaky_tournament_api(monkeypatch, failures=0)
    first = web.build_live(settings, args, out=io.StringIO(), sleep=lambda s: None)
    expected = first.context
    first.close()
    _flaky_tournament_api(monkeypatch, failures="always")
    waits: List[float] = []
    out = io.StringIO()
    runtime = web.build_live(settings, args, out=out, sleep=waits.append)
    try:
        assert waits == []  # no need to wait: the tournament id is known
        assert (runtime.context.tournament_id, runtime.context.slug, runtime.context.name) == (
            expected.tournament_id, expected.slug, expected.name
        )
        assert "using the details saved for" in out.getvalue() and "balance will show once the API answers" in out.getvalue()
        runtime.tracker.run_once()  # market data works; only the balance is missing
        status = runtime.app.status()
        assert status["counts"]["outcomes"] == 33 and status["account"]["balance"] is None
        assert status["tracker"]["fatal_error"] is None
    finally:
        runtime.app.wait_backtest(10)
        runtime.close()


def test_r2_live_api_14_gives_up_after_the_retries_and_never_retries_a_rejected_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path)
    args = build_parser().parse_args(["dashboard", "--no-news"])
    seen = _flaky_tournament_api(monkeypatch, failures="always")
    waits: List[float] = []
    out = io.StringIO()
    with pytest.raises(ApiError) as caught:
        web.build_live(settings, args, out=out, sleep=waits.append, retry_waits=(0.5, 1.0))
    assert caught.value.status == 503 and waits == [0.5, 1.0] and seen["tournament"] == 3
    assert "still unavailable after 1 min of retries" in out.getvalue()
    assert not (tmp_path / demo_mod.DEMO_SLUG).exists()  # nothing saved, no store left open
    seen = _flaky_tournament_api(monkeypatch, failures="always", status=401)
    waits.clear()
    with pytest.raises(ApiError) as caught:
        web.build_live(settings, args, out=io.StringIO(), sleep=waits.append)
    assert caught.value.status == 401 and waits == [] and seen["tournament"] == 1
    # Through run_dashboard: a clear error and exit 1, no traceback.
    _flaky_tournament_api(monkeypatch, failures="always")
    monkeypatch.setattr(web, "STARTUP_RETRY_WAITS_S", (0.0,))
    err = io.StringIO()
    assert web.run_dashboard(settings, _dashboard_args(tmp_path, "--no-browser", "--no-news"), out=io.StringIO(), err=err) == 1
    assert "error: HTTP 503 SERVICE_UNAVAILABLE" in err.getvalue() and "Traceback" not in err.getvalue()
    assert web.is_transient_error(ApiError(429, "RATE_LIMITED", "slow down"))
    assert web.is_transient_error(ApiError(502, "BAD_GATEWAY", "x"))
    assert not web.is_transient_error(ApiError(404, "NOT_FOUND", "no such tournament"))
    assert not web.is_transient_error(ApiError(403, "FORBIDDEN", "not visible"))


# --------------------------------------------------------------------------- simulation endpoints (docs/PAPER_TRADING.md §7.4, §10)

PAPER_KEYS = {
    "now", "enabled", "available", "error", "demo", "run", "has_previous", "headline", "table_warning", "model_label",
    "portfolios", "equity", "positions", "baskets", "orders", "fills", "trades", "signals", "study", "budget",
    "fair_value", "caveats", "last_step",
}
RUN_KEYS = {
    "run_id", "started_at", "hours_run", "wall_hours", "gaps", "target_hours", "progress", "complete", "steps",
    "last_step_at", "last_step_seconds", "interval", "regime", "all_collateral", "sizing", "start_capital",
    "capital_source", "fingerprint", "code_version", "settings", "ended_at", "end_reason", "final",
}
HEADLINE_KEYS = {
    "portfolio_id", "label", "latency_s", "pnl_liq", "pnl_liq_pct", "pnl_mark", "equity_liq", "unvalued",
    "depth_unknown_share", "verdict", "verdict_caveats",
}
BUDGET_KEYS = {
    "reads_used", "reads_limit", "reads_room", "book_reads_last_step", "trade_reads_last_step", "reads_skipped_last_step",
    "max_book_reads_per_step", "max_trade_reads_per_step", "sets_deferred_last_step", "tape_gaps_last_step",
}
FAIRVALUE_KEYS = {"now", "enabled", "mode", "last_refresh_at", "providers", "manual", "map", "history", "counts", "rows", "caveats"}
FV_ROW_KEYS = {
    "exchange_id", "market_id", "title", "option", "race_key", "party", "sm_bid", "sm_ask", "sm_mid", "fair", "matches",
    "gap", "edge_yes", "edge_no", "near", "suspect", "snippets", "unmatched_reason",
    "move",  # docs/OUTSIDE_MOVES.md §18.4: the outcome's open outside-move alert, or null
}
SIM_HOURS = 1.05  # past the scripted NV basket (T0+4..30 min), WY hole (20 min), ME settlement (40 min) and NE triple (60 min)


def fast_demo(data_dir: Path, **kwargs: Any) -> Tuple[web.Runtime, Any]:
    clock = demo_mod.SimClock(demo_mod.SIM_T0)
    kwargs.setdefault("news", False)
    return web.build_demo(data_dir, 30.0, out=io.StringIO(), clock=clock, **kwargs), clock


@pytest.fixture(scope="module")
def sim(tmp_path_factory: Any) -> Iterator[SimpleNamespace]:
    """A fast demo (SimClock) simulated for SIM_HOURS with the paper trader and the demo's outside prices."""
    from supermarket_bot import pipeline

    runtime, clock = fast_demo(tmp_path_factory.mktemp("sim-dashboard"))
    try:
        pipeline.run_simulation(runtime, clock, hours=SIM_HOURS, step_s=30.0, end_run=False)
        yield SimpleNamespace(runtime=runtime, app=runtime.app, tracker=runtime.tracker, store=runtime.store, clock=clock)
    finally:
        runtime.app.wait_backtest(10)
        runtime.app.wait_paper_backtest(120)
        runtime.close()


@pytest.fixture
def small(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A fast demo run for 10 steps, served over HTTP (for the reset flow, which changes the run)."""
    from supermarket_bot import pipeline

    runtime, clock = fast_demo(tmp_path)
    try:
        pipeline.run_simulation(runtime, clock, hours=9 * 30 / 3600, step_s=30.0, end_run=False)
        with serve(runtime.app) as srv:
            yield SimpleNamespace(runtime=runtime, app=runtime.app, tracker=runtime.tracker, http=HTTP(srv.port), clock=clock)
    finally:
        runtime.app.wait_backtest(10)
        runtime.app.wait_paper_backtest(120)
        runtime.close()


def test_paper_endpoint_shape(sim: SimpleNamespace) -> None:
    from supermarket_bot.paper import CAVEATS, DEMO_CAVEAT, FV_MODEL_LABEL, TABLE_WARNING, VERDICT_CAVEATS

    body = assert_json_safe(sim.app.paper())
    assert PAPER_KEYS <= set(body)
    assert body["now"] == sim.clock() and body["enabled"] is True and body["available"] is True and body["error"] is None
    assert body["demo"] is True and body["has_previous"] is False
    run = body["run"]
    assert RUN_KEYS <= set(run)
    assert run["interval"] == 30.0 and run["steps"] == int(round(SIM_HOURS * 120)) + 1
    assert run["hours_run"] == pytest.approx(SIM_HOURS, abs=0.01) and run["progress"] == pytest.approx(SIM_HOURS / 24, abs=0.001)
    assert run["complete"] is False and run["final"] is None and run["ended_at"] is None and run["gaps"] == []
    assert (run["regime"], run["sizing"], run["all_collateral"]) == ("unknown", "conservative", False)
    assert set(run["settings"]) >= {"sizing", "regime", "all_collateral", "start_capital", "params_changed"}
    assert run["capital_source"] in ("account value", "cash", "initial balance", "default 100,000", "set by you")
    headline = body["headline"]
    assert HEADLINE_KEYS <= set(headline)
    assert headline["portfolio_id"] == demo_mod.DEMO_HEADLINE and headline["latency_s"] == 240.0
    assert headline["verdict"]["level"] == "insufficient"  # one simulated hour can never be evidence
    assert headline["verdict_caveats"] == [CAVEATS[i] for i in VERDICT_CAVEATS]
    assert body["table_warning"] == TABLE_WARNING.format(n=9) and body["model_label"] == FV_MODEL_LABEL
    assert [p["portfolio_id"] for p in body["portfolios"]] == list(demo_mod.DEMO_PORTFOLIO_IDS)  # headline first
    for p in body["portfolios"]:
        assert {"sizing", "verdict", "execution", "no_trade_reason", "pnl_liq", "equity_liq"} <= set(p)
    assert set(body["equity"]) == set(demo_mod.DEMO_PORTFOLIO_IDS)
    for points in body["equity"].values():
        assert 0 < len(points) <= 300 and [pt[0] for pt in points] == sorted(pt[0] for pt in points)
    assert len(body["positions"]) <= 200 and all({"unrealized_liq", "exit_note", "age_hours"} <= set(x) for x in body["positions"])
    for key in ("orders", "fills", "trades"):
        assert len(body[key]) <= 100
    fill_ts = [f["ts"] for f in body["fills"]]
    assert fill_ts and fill_ts == sorted(fill_ts, reverse=True)  # newest first
    assert set(body["signals"]) == {"count", "by_kind", "at"} and body["signals"]["count"] > 0
    assert BUDGET_KEYS <= set(body["budget"])
    assert body["fair_value"] == {"mode": "auto", "enabled": True, "usable": body["fair_value"]["usable"],
                                  "total": body["fair_value"]["total"]}
    assert body["fair_value"]["usable"] > 0
    assert body["caveats"] == list(CAVEATS) + [DEMO_CAVEAT]
    assert isinstance(body["last_step"], dict) and isinstance(body["study"], dict)
    for b in body["baskets"]:
        assert {"portfolio_id", "basket_id", "idea_id", "sets", "cost", "floor_value", "liq_value", "legs"} <= set(b)


def test_paper_endpoint_has_fills_where_the_scripts_put_them(sim: SimpleNamespace) -> None:
    body = sim.app.paper()
    fills = {p["portfolio_id"]: p["fills"] for p in body["portfolios"]}
    for pid in (demo_mod.DEMO_HEADLINE, "kind:basket", "kind:value", "kind:hole"):
        assert fills[pid] > 0, (pid, fills)
    settled = [f for f in sim.store.paper_fills(body["run"]["run_id"], limit=None) if f["purpose"] == "settlement"]
    assert settled and {f["exchange_id"] for f in settled} == {"9034"}  # the Maine debate settled at T0 + 40 min


def test_paper_endpoint_over_http_and_the_previous_run(small: SimpleNamespace) -> None:
    http = small.http
    resp = http.get("/api/paper")
    assert resp.status == 200 and resp.headers["cache-control"] == "no-store"
    assert_security_headers(resp)
    before = resp.json()
    assert before["run"]["steps"] == 10 and before["has_previous"] is False
    missing = assert_json_error(http.get("/api/paper?run=previous"), 404)
    assert missing == {"error": "No earlier simulation run has ended yet."}
    resp = http.post("/api/paper/reset", json.dumps({"confirm": True, "start_capital": 100_000, "target_hours": 24}).encode())
    assert resp.status == 200, resp.body
    data = resp.json()
    assert data["reset"] is True and data["previous_run_id"] == before["run"]["run_id"] and data["run_id"] != data["previous_run_id"]
    assert data["message"] == ("Started a new 24-hour simulation with 100,000 SUSQies per portfolio. "
                               "The previous run's result is kept under Previous run.")
    after = http.get("/api/paper").json()
    assert after["run"]["run_id"] == data["run_id"] and after["run"]["steps"] == 0 and after["has_previous"] is True
    assert after["run"]["start_capital"] == 100_000 and after["run"]["capital_source"] == "set by you"
    previous = http.get("/api/paper?run=previous").json()
    assert PAPER_KEYS <= set(previous)
    run = previous["run"]
    assert run["run_id"] == before["run"]["run_id"] and run["end_reason"] == "reset" and run["ended_at"] is not None
    assert run["final"]["reason"] == "reset" and set(run["final"]["verdicts"]) == set(demo_mod.DEMO_PORTFOLIO_IDS)
    assert [p["portfolio_id"] for p in previous["portfolios"]] == list(demo_mod.DEMO_PORTFOLIO_IDS)
    assert previous == small.app.paper(run="previous") | {"now": previous["now"]}


def test_paper_reset_security_and_validation(small: SimpleNamespace) -> None:
    http = small.http
    run_id = http.get("/api/paper").json()["run"]["run_id"]
    ok = json.dumps({"confirm": True}).encode()
    assert_json_error(http.post("/api/paper/reset", ok, Origin="http://evil.example"), 403)
    assert_json_error(http.post("/api/paper/reset", ok, Content_Type="text/plain"), 403)
    assert_json_error(http.post("/api/paper/reset", b"[1, 2]"), 400)
    assert_json_error(http.post("/api/paper/reset", b"{not json"), 400)
    cases = [
        ({}, 'Send {"confirm": true} to reset the simulation.'),
        ({"confirm": "yes"}, 'Send {"confirm": true} to reset the simulation.'),
        ({"confirm": True, "start_capital": 0}, "start_capital must be a number between 1 and 10,000,000."),
        ({"confirm": True, "start_capital": "lots"}, "start_capital must be a number between 1 and 10,000,000."),
        ({"confirm": True, "start_capital": 20_000_000}, "start_capital must be a number between 1 and 10,000,000."),
        ({"confirm": True, "target_hours": 0.5}, "target_hours must be a number between 1 and 720."),
        ({"confirm": True, "target_hours": 1000}, "target_hours must be a number between 1 and 720."),
    ]
    for body, error in cases:
        resp = http.post("/api/paper/reset", json.dumps(body).encode())
        assert resp.status == 400 and resp.json() == {"reset": False, "error": error}, body
    assert http.post("/api/paper/reset", b"").status == 400  # an empty body is {}: no confirmation
    for method in ("GET", "HEAD"):
        resp = http.request(method, "/api/paper/reset")
        assert resp.status == 405 and resp.headers["allow"] == "POST", method
    resp = http.post("/api/paper", ok)
    assert resp.status == 405 and resp.headers["allow"] == "GET, HEAD"
    assert http.get("/api/paper").json()["run"]["run_id"] == run_id  # nothing above reset anything
    resp = http.post("/api/paper/reset", ok)
    assert resp.status == 200 and resp.json()["previous_run_id"] == run_id


def test_paper_off(tmp_path: Path) -> None:
    runtime, clock = fast_demo(tmp_path, paper=False)
    try:
        runtime.tracker.run_once()
        app = runtime.app
        body = assert_json_safe(app.paper())
        assert body == {
            "now": clock(), "enabled": False, "available": False, "error": "The paper trader is off (started with --no-paper).",
            "demo": True, "run": None, "has_previous": False, "headline": None, "table_warning": "", "model_label": "",
            "portfolios": [], "equity": {}, "positions": [], "baskets": [], "orders": [], "fills": [], "trades": [],
            "signals": None, "study": None, "budget": None, "fair_value": None, "caveats": [], "last_step": None,
        }
        assert app.paper(run="previous") is None
        assert app.paper_reset({"confirm": True}) == (409, {"reset": False, "error": "The paper trader is off (started with --no-paper)."})
        status = app.status()
        assert status["features"]["paper"] is False and status["tracker"]["read_budget"]["paper"] is None
        with serve(app) as srv:
            http = HTTP(srv.port)
            assert http.get("/api/paper").json()["enabled"] is False
            assert_json_error(http.get("/api/paper?run=previous"), 404)
            assert http.post("/api/paper/reset", b'{"confirm": true}').status == 409
    finally:
        runtime.app.wait_paper_backtest(60)
        runtime.close()


def test_status_features_and_read_budget_keys(sim: SimpleNamespace) -> None:
    status = assert_json_safe(sim.app.status())
    assert status["features"]["paper"] is True and status["features"]["fair_value"] == "auto"
    assert status["features"]["sizing"] == "conservative" and status["features"]["regime"] == "unknown"
    budget = status["tracker"]["read_budget"]
    assert set(budget["paper"]) == {"used", "limit", "room"} and budget["paper"]["limit"] == 600  # fast demo: 600/min
    assert budget["reserve"] == 12 and isinstance(budget["projected_per_min"], float)
    assert budget["warning"] is None  # the demo's simulated API is not an account with a 100/min limit
    assert status["tracker"]["paper"]["enabled"] is True and status["tracker"]["paper"]["steps"] == int(round(SIM_HOURS * 120)) + 1
    assert status["tracker"]["fair_value"]["mode"] == "auto"


def test_strategy_report_has_the_new_kinds_and_sizing(sim: SimpleNamespace) -> None:
    report = assert_json_safe(sim.app.strategy())
    kinds = {o["kind"] for o in report["opportunities"]}
    assert {"value", "basket"} <= kinds, kinds  # the TX/IA gaps to the outside price; the Nebraska NO basket
    assert report["sizing_policy"] == "conservative" and "alternative" in report["sizing"]
    assert report["settlement_regime"] == "unknown"
    assert report["fair_value"]["mode"] == "auto" and report["fair_value"]["usable"] > 0
    assert set(report["fair_value"]) >= {"mode", "usable", "total", "providers"}
    value = next(o for o in report["opportunities"] if o["kind"] == "value")
    assert value["fair_value"] is not None and value["fair_source"] == "demo"


def test_fairvalue_endpoint_shape(sim: SimpleNamespace) -> None:
    from supermarket_bot.fairvalue import FV_CAVEATS

    data = assert_json_safe(sim.app.fairvalue())
    assert set(data) == FAIRVALUE_KEYS
    assert data["enabled"] is True and data["mode"] == "auto" and data["caveats"] == list(FV_CAVEATS)
    assert [p["name"] for p in data["providers"]] == ["demo"] and data["providers"][0]["status"] == "ok"
    assert set(data["providers"][0]) == {"name", "status", "last_ok_at", "last_error", "requests", "matched", "quoted", "next_try_at"}
    assert set(data["counts"]) == {"outcomes", "matched", "usable", "manual", "no_external", "suspect", "near"}
    rows = data["rows"]
    assert len(rows) == data["counts"]["outcomes"] and data["counts"]["usable"] > 0
    for r in rows:
        assert set(r) == FV_ROW_KEYS
        assert set(r["snippets"]) == {"disable", "pin", "confirm", "trade_near"}
    gaps = [abs(r["gap"]) for r in rows if r["gap"] is not None]
    assert gaps == sorted(gaps, reverse=True)
    seen_null = False
    for r in rows:  # nulls last
        seen_null = seen_null or r["gap"] is None
        assert not (seen_null and r["gap"] is not None)
    tx = next(r for r in rows if r["exchange_id"] == "9026")
    assert tx["race_key"] == "2026:SENATE:TX" and tx["party"] == "D" and tx["fair"]["value"] == pytest.approx(0.58)
    assert tx["gap"] == pytest.approx(tx["fair"]["value"] - tx["sm_mid"]) and "age_s" in tx["fair"]
    assert tx["edge_yes"] == pytest.approx(tx["fair"]["value"] - tx["sm_ask"])
    assert {m["venue"] for m in tx["matches"]} == {"demo"}
    with serve(sim.app) as srv:
        assert HTTP(srv.port).get("/api/fairvalue").json()["rows"] == json.loads(web.encode_json(sim.app.fairvalue()))["rows"]


def test_fairvalue_off(tmp_path: Path) -> None:
    from supermarket_bot.fairvalue import FV_CAVEATS

    runtime, _ = fast_demo(tmp_path, fair_value="off", paper=False)
    try:
        data = runtime.app.fairvalue()
        assert data["enabled"] is False and data["mode"] == "off" and data["rows"] == [] and data["providers"] == []
        assert data["caveats"] == list(FV_CAVEATS)
        assert runtime.app.status()["features"]["fair_value"] == "off"
    finally:
        runtime.close()


def test_backtest_endpoint_runs_on_a_read_only_connection(sim: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    import statistics

    import supermarket_bot.backtest as backtest_mod

    sources: List[Any] = []
    real = backtest_mod.run_backtest

    def spy(source: Any, config: Any, *args: Any, **kwargs: Any) -> Any:
        sources.append((source, config))
        return real(source, config, *args, **kwargs)

    monkeypatch.setattr(backtest_mod, "run_backtest", spy)
    app, tracker, clock = sim.app, sim.tracker, sim.clock

    def cycle() -> float:
        clock.advance(30)
        started = time.perf_counter()
        tracker.run_once()
        return time.perf_counter() - started

    before = [cycle() for _ in range(6)]
    first = assert_json_safe(app.backtest())
    assert first["status"] == "pending" and first["report"] is None and first["started_at"] == clock()
    during: List[float] = []
    deadline = time.monotonic() + 120
    while app._pbt_thread is not None and app._pbt_thread.is_alive() and time.monotonic() < deadline:
        during.append(cycle())
    app.wait_paper_backtest(120)
    data = assert_json_safe(app.backtest())
    assert data["status"] == "ready", data["error"]
    assert set(data) == {"now", "status", "error", "started_at", "generated_at", "report"}
    report = data["report"]
    assert {"testability", "study", "overlap_hours", "stopped_early", "portfolios", "verdicts", "assumptions"} <= set(report)
    assert report["overlap_hours"] > 0  # the window overlaps the live run: not an independent check
    [(source, config)] = sources
    assert isinstance(source, TrackerStore) and source is not sim.store and str(source.path) == str(sim.store.path)
    assert config.hours == 24 and config.paper.sizing == "conservative" and config.live_run_started_at is not None
    with pytest.raises(Exception):
        TrackerStore.open_read_only(sim.store.path).set_state("x", 1)  # that connection refuses writes
    if during:  # the replay never holds the live store: snapshot cycles keep their pace (median within 2x)
        assert statistics.median(during) <= 2 * statistics.median(before) + 0.05, (before, during)
    assert app.backtest()["generated_at"] == data["generated_at"]  # cached: not re-run within 30 minutes


def test_backtest_endpoint_without_enough_data_or_a_backtest(small: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    import supermarket_bot.backtest as backtest_mod

    app = small.app
    app.backtest()
    app.wait_paper_backtest(60)
    data = app.backtest()
    assert data["status"] == "no_data" and data["report"] is None and isinstance(data["error"], str)

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    monkeypatch.setattr(backtest_mod, "run_backtest", missing)
    small.clock.advance(3 * 3600)  # a later retry, with enough ticks stored meanwhile
    tick = {"exchange_id": "9001", "latest_price": 0.5, "best_bid": 0.49, "best_ask": 0.51}
    small.runtime.store.add_ticks(small.clock() - 2 * 3600, [tick])
    small.runtime.store.add_ticks(small.clock(), [tick])
    app.backtest()
    app.wait_paper_backtest(60)
    assert app.backtest()["status"] == "unavailable"


# --------------------------------------------------------------------------- integration regressions (round 3)


def test_ui_11_near_match_only_when_a_near_match_exists(sim: SimpleNamespace) -> None:
    """ui-11: the chamber-control outcomes (9014-9017) have no outside match, yet their FairValue carries the race's
    NEAR kind: they must not be flagged or counted as near matches, nor offer "Turn this match off" / "Trade this
    near match" snippets for a match that does not exist (pinning one stays possible)."""
    data = sim.app.fairvalue()
    rows = {r["exchange_id"]: r for r in data["rows"]}
    for r in data["rows"]:
        fair = r["fair"] or {}
        has_near = any(m.get("kind") == "NEAR" for m in r["matches"]) or (
            fair.get("match_kind") == "NEAR" and fair.get("value") is not None)
        assert r["near"] is has_near, r["exchange_id"]
        if r["snippets"]["trade_near"] is not None:
            assert r["near"] is True
        if not r["matches"] and fair.get("value") is None:
            assert r["snippets"]["disable"] is None and r["snippets"]["trade_near"] is None, r["exchange_id"]
    assert data["counts"]["near"] == sum(1 for r in data["rows"] if r["near"])
    for eid in ("9014", "9015", "9016", "9017"):
        r = rows[eid]
        assert r["matches"] == [] and r["near"] is False and r["snippets"]["disable"] is None
        assert r["snippets"]["pin"] is not None
    matched = next(r for r in data["rows"] if r["matches"])
    assert matched["snippets"]["disable"] is not None  # a real match can still be turned off


def test_lookahead_1_dashboard_backtest_counts_a_run_that_already_ended(tmp_path: Path) -> None:
    """lookahead-1: the dashboard used only the CURRENT run's start; after a run ended (completed, reset, settings
    changed) and a new one started, a replay of the same hours said overlap 0. Every stored run counts now."""
    from supermarket_bot import pipeline

    runtime, clock = fast_demo(tmp_path)
    try:
        pipeline.run_simulation(runtime, clock, hours=1.0, step_s=30.0, end_run=True)  # "completed"
        first = runtime.store.paper_run_times()
        assert len(first) == 1 and first[0]["ended_at"] is not None
        pipeline.run_simulation(runtime, clock, hours=0.1, step_s=30.0, end_run=False)  # a new run starts
        current = runtime.tracker.paper_view()["run"]
        assert current["run_id"] != first[0]["run_id"]
        app = runtime.app
        app.backtest()
        app.wait_paper_backtest(120)
        data = app.backtest()
        assert data["status"] == "ready", data["error"]
        report = data["report"]
        window = report["window"]
        start = max(window["start"], first[0]["started_at"])
        assert report["overlap_hours"] == pytest.approx((window["end"] - start) / 3600.0, abs=1e-3)
        assert report["overlap_hours"] > 1.0  # not just the ~0.1 h of the new run
        assert any("not an independent check" in w for w in report["warnings"])
    finally:
        runtime.app.wait_paper_backtest(120)
        runtime.close()


def test_live_5_sighup_while_starting_stops_cleanly_and_restores_the_handler(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """live-5: closing the terminal (SIGHUP) stops the dashboard like Ctrl-C, so the tracker stops and releases
    its lease, instead of killing the process with the lease held."""
    import os
    import signal

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGHUP"):
        pytest.skip("needs the main thread and SIGHUP")

    class Unhandled(Exception):
        pass

    def fallback(signum: int, frame: Any) -> None:  # only reached if run_dashboard installed no handler
        raise Unhandled("SIGHUP reached the test's own handler")

    original = signal.signal(signal.SIGHUP, fallback)  # never let a regression kill the test run
    before = signal.getsignal(signal.SIGHUP)

    def slow_start(settings: Any, args: Any, out: Any = None, **kwargs: Any) -> Any:
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(5)
        raise AssertionError("SIGHUP did not interrupt the start-up")

    monkeypatch.setattr(web, "build_live", slow_start)
    settings = Settings(api_key=FAKE_KEY, tournament="cup", data_dir=tmp_path)
    err = io.StringIO()
    try:
        assert web.run_dashboard(settings, _dashboard_args(tmp_path, "--no-browser"), out=io.StringIO(), err=err) == 130
        assert "Stopped before the dashboard started." in err.getvalue()
        assert signal.getsignal(signal.SIGHUP) is before  # restored
    finally:
        signal.signal(signal.SIGHUP, original)


def test_live_5_sighup_under_nohup_is_left_alone() -> None:
    import signal

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGHUP"):
        pytest.skip("needs the main thread and SIGHUP")
    original = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        previous = web._install_sigterm(threading.Event())
        try:
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN  # `nohup ... dashboard` keeps running
            assert signal.SIGHUP not in (previous or {})
        finally:
            web._restore_sigterm(previous)
    finally:
        signal.signal(signal.SIGHUP, original)


def test_live_5_a_dashboard_closed_by_sighup_releases_its_lease(tmp_path: Path) -> None:
    """The real process: `dashboard --demo` ended by SIGHUP exits cleanly and leaves no lease behind, so starting
    again at once continues the run."""
    import os
    import signal
    import subprocess
    import sys

    if not hasattr(signal, "SIGHUP"):
        pytest.skip("needs SIGHUP")
    repo = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    proc = subprocess.Popen(
        [sys.executable, "-m", "supermarket_bot", "dashboard", "--demo", "--no-browser", "--port", "0", "--no-news",
         "--interval", "2", "--data-dir", str(data), "--fair-value", "off"],
        cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        lines: List[str] = []
        deadline = time.monotonic() + 60
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "Dashboard running" in line:
                break
        assert any("Dashboard running" in line for line in lines), "".join(lines)
        db = data / "demo" / "tracker.sqlite3"
        assert wait_for_lease(db, present=True), "the tracker never took its lease"
        proc.send_signal(signal.SIGHUP)
        rest, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, "".join(lines) + rest
        assert "Stopping the dashboard" in rest
        assert _lease_of(db) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _lease_of(db: Path) -> Any:
    ro = TrackerStore.open_read_only(db)
    try:
        return ro.lease("tracker")
    finally:
        ro.close()


def wait_for_lease(db: Path, present: bool, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.is_file():
            try:
                held = _lease_of(db) is not None
            except Exception:
                held = False
            if held is present:
                return True
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------- outside-move alerts (docs/OUTSIDE_MOVES.md §18)

MOVES_KEYS = [  # §18.2, in order; every key always present
    "now", "enabled", "available", "error", "demo", "poll_s", "last_step_at", "steps", "watching", "venues", "counts",
    "actionable_ids", "alerts", "suppressed", "summary", "thresholds", "book_reads", "caveats",
]
NO_SUPPRESSED = {"thin": 0, "single_tick": 0, "disagree": 0, "near": 0, "suspect": 0, "stale": 0, "placeholder": 0,
                 "match": 0, "no_cup": 0}
MOVES_THRESHOLDS = {
    "windows_s": [60.0, 300.0, 900.0, 3600.0], "abs_min": {"60": 0.03, "300": 0.04, "900": 0.05, "3600": 0.07},
    "k_sigma": 4.0, "warmup_factor": 1.5, "max_spread": 0.05, "min_liquidity_usd": 5000.0, "min_top_size": 100.0,
    "follow_fraction": 0.5, "lag_track_s": 3600.0, "human_delay_s": 240.0, "exit_after_s": 1800.0, "min_edge": 0.01,
}
MOVES_STATUS_KEYS = {"enabled", "poll_s", "last_step_at", "steps", "open", "actionable", "actionable_alerts", "venues",
                     "last_error"}
ANNOTATION_KEYS = {"alert_id", "status", "status_label", "state", "direction", "move", "lag_gap_now", "detected_at",
                   "actionable"}
ALERT_KEYS = {f.name for f in fields(moves_mod.MoveAlert)}
MOVES_MINUTES = 20  # §18.5: lead_short (~c+90) and lead_long (~c+195) alerted, never (~c+285) still lagging
MOVES_NO_ALERT_IDS = {"9038", "9041"}  # the thin spike and the venue disagreement must never alert (§15.2)


def disabled_moves_body(now: float, demo: bool) -> Dict[str, Any]:
    """The disabled /api/moves body, written out as §18.2 states it."""
    return {
        "now": now, "enabled": False, "available": False, "error": moves_mod.MOVES_OFF_ERROR, "demo": demo, "poll_s": None,
        "last_step_at": None, "steps": 0, "watching": {"outcomes": 0, "matched": 0, "venues": 0}, "venues": [],
        "counts": {"open": 0, "lagging": 0, "actionable": 0, "today": 0, "closed_today": 0,
                   "suppressed_today": dict(NO_SUPPRESSED)},
        "actionable_ids": [], "alerts": [], "suppressed": [], "summary": None, "thresholds": None, "book_reads": None,
        "caveats": [],
    }


def example_l_alert(**overrides: Any) -> Dict[str, Any]:
    """The §18.2 example alert (Example L, abbreviated where the server does not look)."""
    alert: Dict[str, Any] = {
        "alert_id": "mv-1104-1791208800", "exchange_id": "1104", "market_id": "552",
        "title": "Will the Democratic Party win the North Carolina Senate?", "option": "YES", "race_key": "2026:SENATE:NC",
        "party": "D", "race_label": "North Carolina Senate (D)", "direction": 1, "window_s": 300.0, "windows": [300.0],
        "venues": ["polymarket", "kalshi"], "confirmation": "two venues", "venue_moves": [], "t_base": 1791208800.0,
        "t_move": 1791208960.0, "outside_before": 0.5175, "outside_after": 0.5725, "outside_now": 0.5725, "move": 0.055,
        "peak_move": 0.055, "threshold": 0.044, "sigma": 0.011, "vol_known": True, "uncertainty": 0.02,
        "detected_at": 1791209115.0, "updated_at": 1791209115.0, "grown_at": 1791209115.0, "state": "open",
        "status": "lagging", "status_label": "Cup lagging",
        "reason": "The outside price moved +5.5 pts in 5 min; the Cup has moved +0.0 pts over the same time (now 0.512).",
        "cup": {"base": None, "ref": None, "now": None, "move_same_window": 0.0, "move_lookback": 0.0, "half_cross_ts": None,
                "short_history": False},
        "lag_gap": 0.055, "lag": {"eligible": True, "outcome": "pending"}, "closed_at": None, "cup_now": None,
        "lag_gap_now": 0.055, "level_gap_now": 0.06,
        "trade": {"side": "yes", "action": "buy", "text": "Buy YES at 0.520", "limit": 0.52, "max_limit": 0.525,
                  "outside_value": 0.565, "uncertainty": 0.02, "cup_half_spread": 0.0075, "edge_per_share": 0.0375,
                  "edge_after_uncertainty": 0.0175, "edge_at_resolution": 0.045, "shares_at_limit": 300.0,
                  "shares_to_max": 750.0, "book_source": "read", "book_age_s": 2.0, "computed_at": 1791209115.0,
                  "note": moves_mod.MOVES_TRADE_NOTE},
        "trade_note": None, "actionable": True, "linked": [], "flags": [], "demo": False,
    }
    alert.update(overrides)
    return alert


def compact_of(alert: Mapping[str, Any]) -> Dict[str, Any]:
    """What the watcher's ``open_alert_for`` publishes for an open alert."""
    return {key: alert[key] for key in ANNOTATION_KEYS}


def fake_summary(alerts: List[Dict[str, Any]], **overrides: Any) -> Dict[str, Any]:
    """A published watcher summary (§12.4: the §18.2 body without the keys web.py adds)."""
    open_ = [a for a in alerts if a["state"] == "open"]
    venues = [moves_mod.VenueStatus(venue=v, label=moves_mod.VENUE_LABELS[v], status="ok", matched=231, quoted=230,
                                    budget_used=25, budget_limit=45).to_dict() for v in ("polymarket", "kalshi")]
    summary: Dict[str, Any] = {
        "poll_s": 15.0, "last_step_at": NOW, "steps": 3, "watching": {"outcomes": 237, "matched": 231, "venues": 2},
        "venues": venues,
        "counts": {"open": len(open_), "lagging": sum(1 for a in open_ if a["status"] == "lagging"),
                   "actionable": sum(1 for a in open_ if a["actionable"]), "today": len(alerts),
                   "closed_today": len(alerts) - len(open_), "suppressed_today": dict(NO_SUPPRESSED, thin=2)},
        "actionable_ids": [a["alert_id"] for a in open_ if a["actionable"]], "alerts": alerts,
        "suppressed": [{"exchange_id": "9", "title": "t", "race_label": "r", "reason": "thin", "reason_label": "x",
                        "at": NOW - 5, "window_s": 60.0, "venues": ["kalshi"], "move": 0.05, "threshold": 0.03,
                        "detail": "Kalshi's book was 10 pts wide during the move."}],
        "summary": moves_mod.LagSummary().to_dict(), "thresholds": dict(MOVES_THRESHOLDS),
        "caveats": list(moves_mod.MOVES_CAVEATS),
    }
    summary.update(overrides)
    return summary


class FakeWatcher:
    """A watcher stand-in (docs/OUTSIDE_MOVES.md §2.2): canned published summary and status, open alerts by outcome."""

    poll_s = 15.0

    def __init__(self, summary: Optional[Dict[str, Any]] = None, *, status: Optional[Dict[str, Any]] = None,
                 demo: bool = False, fail: Optional[BaseException] = None) -> None:
        self._summary = summary if summary is not None else fake_summary([])
        self._status = status if status is not None else {
            "enabled": True, "poll_s": 15.0, "last_step_at": NOW, "steps": 3, "open": 0, "actionable": 0,
            "actionable_alerts": [], "venues": {"ok": 2, "total": 2}, "last_error": None}
        self.demo = demo
        self.fail = fail
        self.alert_calls: List[Tuple[Any, ...]] = []
        self.closed = 0

    def summary(self) -> Dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        return self._summary

    def status(self) -> Dict[str, Any]:
        return self._status

    def open_alert_for(self, exchange_id: str) -> Optional[Dict[str, Any]]:
        for alert in self._summary.get("alerts") or []:
            if alert["exchange_id"] == str(exchange_id) and alert["state"] == "open":
                return compact_of(alert)
        return None

    def alerts(self, state: Optional[str] = None, since: Optional[float] = None, limit: Optional[int] = None) -> List[Any]:
        self.alert_calls.append((state, since, limit))
        return list(self._summary.get("alerts") or [])

    def close(self) -> None:
        self.closed += 1


@pytest.fixture(scope="module")
def mv(tmp_path_factory: Any) -> Iterator[SimpleNamespace]:
    """§18.5: ``build_demo(moves=True, clock=SimClock)`` + ``run_simulation(..., moves_every_s=15)`` for 20 simulated
    minutes (the paper trader on, as in the dashboard), served over HTTP."""
    from supermarket_bot import pipeline

    data_dir = tmp_path_factory.mktemp("moves-dashboard")
    runtime, clock = fast_demo(data_dir, moves=True)
    try:
        pipeline.run_simulation(runtime, clock, hours=MOVES_MINUTES / 60.0, step_s=30.0, end_run=False, moves_every_s=15.0)
        with serve(runtime.app) as srv:
            yield SimpleNamespace(runtime=runtime, app=runtime.app, tracker=runtime.tracker, watcher=runtime.moves,
                                  clock=clock, http=HTTP(srv.port), data_dir=data_dir)
    finally:
        runtime.app.wait_backtest(10)
        runtime.app.wait_paper_backtest(120)
        runtime.close()


def test_moves_disabled_body_exactly(pipe: SimpleNamespace) -> None:
    assert pipe.app.moves is None and pipe.runtime.moves is None and pipe.tracker.moves is None
    body = assert_json_safe(pipe.app.outside_moves())
    assert body == disabled_moves_body(pipe.clock.now, True)
    assert list(body) == MOVES_KEYS
    assert pipe.app.outside_moves(state="closed", since=0.0, limit=500) == body  # filters change nothing when off
    assert web.moves_payload(None, 5.0, enabled=False, demo=False) == disabled_moves_body(5.0, False)
    # a published summary never turns a disabled body on
    assert web.moves_payload(fake_summary([example_l_alert()]), 5.0, enabled=False, demo=False) == disabled_moves_body(5.0, False)


def test_moves_disabled_over_http(client: HTTP, pipe: SimpleNamespace) -> None:
    resp = client.get("/api/moves")
    assert resp.status == 200 and resp.headers["cache-control"] == "no-store"
    assert_security_headers(resp)
    assert resp.json() == disabled_moves_body(pipe.clock.now, True)
    assert client.get("/api/moves?state=open&since=0&limit=1").json() == disabled_moves_body(pipe.clock.now, True)
    data = assert_json_error(client.get("/api/moves?state=nope"), 400)  # the query is checked even when off
    assert data == {"error": "state must be open, closed or all."}
    status = client.get("/api/status").json()
    assert status["features"]["moves"] is False and status["moves"] is None
    assert status["tracker"]["read_budget"]["moves"] is None


def test_moves_enabled_body_on_the_fast_demo(mv: SimpleNamespace) -> None:
    from supermarket_bot.moves import MOVES_CAVEATS, MOVES_DEMO_CAVEAT, MOVES_TRADE_NOTE, SUPPRESS_REASONS, VENUE_LABELS

    body = assert_json_safe(mv.app.outside_moves())
    assert list(body) == MOVES_KEYS
    assert (body["enabled"], body["available"], body["error"], body["demo"]) == (True, True, None, True)
    assert body["now"] == mv.clock() and body["last_step_at"] == mv.clock() and body["poll_s"] == 15.0
    assert body["steps"] == MOVES_MINUTES * 2 * 2 + 1  # §16: a watcher step at each 30-s step and one sub-step between
    assert body["watching"]["matched"] == 7 and body["watching"]["venues"] == 2 and body["watching"]["outcomes"] >= 7
    assert [v["venue"] for v in body["venues"]] == ["demo-a", "demo-b"]
    for v in body["venues"]:
        assert set(v) == {f.name for f in fields(moves_mod.VenueStatus)}
        assert v["label"] == VENUE_LABELS[v["venue"]] and v["status"] == "ok" and v["matched"] == 7

    alerts = body["alerts"]
    assert alerts and all(set(a) == ALERT_KEYS for a in alerts)
    eids = {a["exchange_id"] for a in alerts}
    assert {"9035", "9036"} <= eids, eids  # lead_short and lead_long
    assert not eids & MOVES_NO_ALERT_IDS, eids  # the thin spike and the one-venue move never alert
    assert all(a["demo"] is True for a in alerts)
    states = [a["state"] for a in alerts]
    assert states == sorted(states, key=lambda s: s != "open")  # open first ...
    open_ = [a for a in alerts if a["state"] == "open"]
    closed = [a for a in alerts if a["state"] == "closed"]
    assert [a["actionable"] for a in open_] == sorted((a["actionable"] for a in open_), reverse=True)  # actionable first
    assert [a["detected_at"] for a in closed] == sorted((a["detected_at"] for a in closed), reverse=True)  # newest first

    counts = body["counts"]
    assert counts["open"] == len(open_) and counts["today"] == len(alerts) and counts["closed_today"] == len(closed)
    assert counts["lagging"] == sum(1 for a in open_ if a["status"] == "lagging")
    assert list(counts["suppressed_today"]) == list(SUPPRESS_REASONS)
    for reason in ("single_tick", "thin", "disagree"):  # §15.2: the thin spike (single print, then thin) and 9041
        assert counts["suppressed_today"][reason] >= 1, counts
    assert {s["exchange_id"] for s in body["suppressed"]} >= MOVES_NO_ALERT_IDS
    assert {s["reason"] for s in body["suppressed"]} >= {"single_tick", "thin", "disagree"}

    # the actionable ids: open lagging alerts with a suggestion that keeps >= 1 cent after the spread and the doubt
    assert body["actionable_ids"] and counts["actionable"] == len(body["actionable_ids"])
    by_id = {a["alert_id"]: a for a in alerts}
    for aid in body["actionable_ids"]:
        a = by_id[aid]
        assert (a["state"], a["status"], a["actionable"]) == ("open", "lagging", True)
        trade = a["trade"]
        assert re.fullmatch(r"Buy (YES|NO) at 0\.\d{3}", trade["text"]) and trade["note"] == MOVES_TRADE_NOTE
        assert round(trade["limit"] / 0.005, 6) == round(trade["limit"] / 0.005)  # on the Cup's 0.005 tick
        assert trade["limit"] <= trade["max_limit"] + 1e-9
        assert trade["edge_after_uncertainty"] >= moves_mod.MOVES_MIN_EDGE - 1e-9
    for a in alerts:  # a live suggestion only on an open lagging alert (a closed one keeps only what it said)
        if a["trade"] is not None:
            assert (a["state"], a["status"]) == ("open", "lagging"), a["alert_id"]

    summary = body["summary"]
    assert set(summary) == {f.name for f in fields(moves_mod.LagSummary)}
    assert summary["small_sample"] is True and summary["sentence"].startswith("Only ")  # a few demo moves: says so
    assert body["thresholds"] == MOVES_THRESHOLDS
    assert set(body["book_reads"]) == {"used", "limit", "room"} and body["book_reads"]["limit"] == web.FAST_READS_PER_MIN
    assert body["caveats"] == list(MOVES_CAVEATS) + [MOVES_DEMO_CAVEAT]


def test_moves_texts_never_promise_a_sure_thing(mv: SimpleNamespace) -> None:
    text = web.encode_json(mv.app.outside_moves(limit=500)).decode("utf-8").lower()
    text += web.encode_json(mv.app.status()).decode("utf-8").lower()
    assert "sure thing" in text  # the suggestions carry the note ...
    for match in re.finditer("sure thing", text):  # ... and only ever as "not a sure thing"
        assert text[max(0, match.start() - 6):match.start()] == "not a ", text[match.start() - 80:match.end() + 20]
    for match in re.finditer("guarantee", text):
        assert text[max(0, match.start() - 6):match.start()] == "not a ", text[match.start() - 80:match.end() + 20]


def test_moves_http_matches_the_app_and_filters(mv: SimpleNamespace) -> None:
    resp = mv.http.get("/api/moves")
    assert resp.status == 200 and resp.headers["content-type"] == web.JSON_TYPE
    assert resp.headers["cache-control"] == "no-store"
    assert_security_headers(resp)
    full = resp.json()
    assert full == json.loads(web.encode_json(mv.app.outside_moves()))
    alerts = full["alerts"]
    assert len(alerts) >= 4
    ids = [a["alert_id"] for a in alerts]

    def get(query: str) -> Dict[str, Any]:
        r = mv.http.get("/api/moves?" + query)
        assert r.status == 200, r.body[:300]
        body = r.json()
        assert list(body) == MOVES_KEYS and body["counts"] == full["counts"]  # only the alert list is filtered
        return body

    opened = get("state=open")["alerts"]
    assert opened and all(a["state"] == "open" for a in opened)
    assert [a["alert_id"] for a in opened] == [a["alert_id"] for a in alerts if a["state"] == "open"]
    closed = get("state=closed")["alerts"]
    assert closed and all(a["state"] == "closed" for a in closed)
    assert [a["alert_id"] for a in closed] == [a["alert_id"] for a in alerts if a["state"] == "closed"]
    assert get("state=all") == full and get("state=ALL") == full and get("state=&since=&limit=") == full
    assert [a["alert_id"] for a in get("limit=1")["alerts"]] == ids[:1]
    assert [a["alert_id"] for a in get("limit=500")["alerts"]] == ids
    assert [a["alert_id"] for a in get("state=closed&limit=2")["alerts"]] == [a["alert_id"] for a in closed][:2]
    cut = sorted(a["detected_at"] for a in alerts)[len(alerts) // 2]
    recent = get(f"since={cut}")["alerts"]
    assert [a["alert_id"] for a in recent] == [a["alert_id"] for a in alerts if a["detected_at"] >= cut]
    assert 0 < len(recent) < len(alerts)
    assert get(f"since={mv.clock() + 1}")["alerts"] == []
    assert mv.app.outside_moves(state="open", since=cut, limit=1)["alerts"] == [
        a for a in alerts if a["state"] == "open" and a["detected_at"] >= cut][:1]
    head = mv.http.request("HEAD", "/api/moves")
    assert head.status == 200 and head.body == b"" and int(head.headers["content-length"]) > 0
    assert_security_headers(head)


@pytest.mark.parametrize("query, message", [
    ("state=bogus", web.MOVES_STATE_ERROR), ("state=opened", web.MOVES_STATE_ERROR), ("state=1", web.MOVES_STATE_ERROR),
    ("since=abc", web.MOVES_SINCE_ERROR), ("since=nan", web.MOVES_SINCE_ERROR), ("since=inf", web.MOVES_SINCE_ERROR),
    ("since=-1", web.MOVES_SINCE_ERROR), ("since=2026-10-05", web.MOVES_SINCE_ERROR),
    ("limit=0", web.MOVES_LIMIT_ERROR), ("limit=501", web.MOVES_LIMIT_ERROR), ("limit=1.5", web.MOVES_LIMIT_ERROR),
    ("limit=abc", web.MOVES_LIMIT_ERROR), ("limit=-3", web.MOVES_LIMIT_ERROR), ("limit=1e2", web.MOVES_LIMIT_ERROR),
    ("limit=99999999", web.MOVES_LIMIT_ERROR),
])
def test_moves_bad_queries_are_400(mv: SimpleNamespace, query: str, message: str) -> None:
    resp = mv.http.get(f"/api/moves?{query}")
    assert assert_json_error(resp, 400) == {"error": message}
    assert resp.headers["cache-control"] == "no-store"


def test_moves_query_parser() -> None:
    from urllib.parse import parse_qs

    def parse(text: str) -> Tuple[Optional[str], Dict[str, Any]]:
        return web.moves_query(parse_qs(text, keep_blank_values=True))

    assert parse("") == (None, {"state": None, "since": None, "limit": None})
    assert parse("state=Closed&since=1791208800.5&limit=+20") == (
        None, {"state": "closed", "since": 1791208800.5, "limit": 20})
    assert parse("state=all&limit=500&since=0") == (None, {"state": "all", "since": 0.0, "limit": 500})
    assert parse("limit=1")[1]["limit"] == 1
    assert parse("state=x")[0] == "state must be open, closed or all."
    assert parse("since=x")[0] == "since must be a number of seconds since 1970."
    assert parse("limit=x")[0] == "limit must be a whole number from 1 to 500."


def test_moves_post_and_put_are_refused_like_other_get_routes(mv: SimpleNamespace) -> None:
    resp = mv.http.post("/api/moves")
    assert_json_error(resp, 405)
    assert resp.headers["allow"] == "GET, HEAD"
    for method in ("PUT", "DELETE", "PATCH"):
        resp = mv.http.request(method, "/api/moves", headers={"Content-Type": "application/json", "Origin": mv.http.origin})
        assert_json_error(resp, 405)
        assert resp.headers["allow"] == "GET, HEAD, POST"
    assert_json_error(mv.http.post("/api/moves/mv-9035-1"), 404)
    assert_json_error(mv.http.get("/api/moves/mv-9035-1"), 404)
    assert_json_error(mv.http.post("/api/moves", Origin="http://evil.example"), 403)  # same-origin rule first


def test_moves_status_additions(mv: SimpleNamespace) -> None:
    status = assert_json_safe(mv.app.status())
    assert status["features"]["moves"] is True and status["features"]["fair_value"] == "auto"
    moves = status["moves"]
    assert set(moves) == MOVES_STATUS_KEYS and moves["enabled"] is True and moves["last_error"] is None
    assert moves == status["tracker"]["moves"]  # tracker_summary keeps the raw key too
    body = mv.app.outside_moves()
    assert moves["steps"] == body["steps"] and moves["open"] == body["counts"]["open"]
    assert moves["actionable"] == len(body["actionable_ids"]) >= 1
    assert [a["alert_id"] for a in moves["actionable_alerts"]] == body["actionable_ids"][:10]
    for note in moves["actionable_alerts"]:
        assert set(note) == {"alert_id", "exchange_id", "headline", "body", "detected_at"}
        assert note["headline"].startswith("Cup lagging: ") and note["body"].endswith("Not a sure thing.")
    assert moves["venues"] == {"ok": 2, "total": 2}
    budget = status["tracker"]["read_budget"]["moves"]
    assert set(budget) == {"used", "limit", "room"} and budget == body["book_reads"]
    assert not any(p["source"] == tracker_mod.MOVES_PROBLEM for p in status["problems"])
    with serve(mv.app) as srv:
        served = HTTP(srv.port).get("/api/status").json()
    assert served["moves"] == json.loads(web.encode_json(moves)) and served["features"]["moves"] is True


def test_moves_fairvalue_rows_and_strategy_on_the_fast_demo(mv: SimpleNamespace) -> None:
    body = mv.app.outside_moves()
    open_by_eid = {a["exchange_id"]: a for a in body["alerts"] if a["state"] == "open"}
    assert open_by_eid
    data = assert_json_safe(mv.app.fairvalue())
    rows = {r["exchange_id"]: r for r in data["rows"]}
    assert all(set(r) == FV_ROW_KEYS for r in data["rows"])
    for eid, row in rows.items():
        alert = open_by_eid.get(eid)
        if alert is None:
            assert row["move"] is None, eid
        else:
            assert row["move"] == web.move_annotation(alert) and set(row["move"]) == ANNOTATION_KEYS
    assert any(rows[eid]["move"] is not None for eid in open_by_eid if eid in rows)
    report = mv.app.strategy()
    for opp in report["opportunities"]:  # the demo's value ideas are on other outcomes (no fair value for 9035-9041)
        assert ("outside_move" in opp) == (opp.get("kind") == "value" and opp.get("exchange_id") in open_by_eid)


def test_moves_alerts_file_has_one_opened_line_per_alert(mv: SimpleNamespace) -> None:
    path = mv.data_dir / "demo" / moves_mod.MOVES_ALERTS_FILE
    assert mv.watcher.alerts_path == path
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines and {line["event"] for line in lines} <= {"opened", "status", "closed"}
    opened = [line["alert"]["alert_id"] for line in lines if line["event"] == "opened"]
    assert len(opened) == len(set(opened))
    assert set(opened) == {a["alert_id"] for a in mv.app.outside_moves(limit=500)["alerts"]}
    assert all(line["at_iso"].endswith("Z") for line in lines)


def test_moves_reads_are_lock_free(mv: SimpleNamespace) -> None:
    """§12.3: the web handlers read the watcher's published summary and status without its lock (a long step can
    hold it), so /api/moves, /api/status and the annotations never wait for a step."""
    watcher = mv.watcher
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with watcher._lock:
            held.set()
            release.wait(10)

    mv.app.fairvalue()  # built once before (both are cached on the frozen SimClock)
    mv.app.strategy()
    thread = threading.Thread(target=hold, name="test-hold-watcher-lock", daemon=True)
    thread.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert mv.app.outside_moves()["available"] is True
        assert mv.app.status()["moves"]["enabled"] is True
        mv.app.fairvalue()
        mv.app.strategy()
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        thread.join(5)


def test_moves_concurrent_requests_with_a_watcher_never_deadlock(mv: SimpleNamespace) -> None:
    """Cached /api/strategy and /api/fairvalue answers are annotated outside the app's lock (a cache hit used to
    re-enter it), so many concurrent requests with a watcher all finish."""
    calls = [mv.app.strategy, mv.app.fairvalue, mv.app.outside_moves, mv.app.status,
             lambda: mv.app.outside_moves(state="closed", limit=3)] * 6
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fn) for fn in calls]
        results = [f.result(timeout=60) for f in futures]
    assert all(isinstance(r, dict) for r in results)
    for path in ("/api/strategy", "/api/strategy", "/api/fairvalue", "/api/moves?state=open"):
        assert mv.http.get(path).status == 200


def test_moves_body_from_a_fake_watcher(make_app: Callable[..., web.DashboardApp]) -> None:
    closed = example_l_alert(alert_id="mv-7-1", exchange_id="7", state="closed", status="already_moved",
                             status_label="Cup already moved", actionable=False, trade=None, detected_at=NOW - 900.0,
                             closed_at=NOW - 300.0)
    summary = fake_summary([example_l_alert(), closed])
    watcher = FakeWatcher(summary, status={**FakeWatcher().status(), "last_error": "Kalshi was slow."})
    budget = SimpleNamespace(status=lambda: {"used": 1, "limit": 4, "room": 3})
    tracker = FakeTracker({"status": {}})
    tracker.moves_budget = budget  # type: ignore[attr-defined]
    app = make_app(tracker, moves=watcher)
    body = assert_json_safe(app.outside_moves())
    assert list(body) == MOVES_KEYS
    assert (body["now"], body["enabled"], body["available"], body["demo"]) == (NOW, True, True, False)
    assert body["error"] == "Kalshi was slow."  # the watcher's last_error
    assert body["alerts"] == summary["alerts"] and body["venues"] == summary["venues"]
    assert body["counts"] == summary["counts"] and body["actionable_ids"] == ["mv-1104-1791208800"]
    assert body["suppressed"] == summary["suppressed"] and body["summary"] == summary["summary"]
    assert body["thresholds"] == MOVES_THRESHOLDS and body["caveats"] == list(moves_mod.MOVES_CAVEATS)
    assert body["book_reads"] == {"used": 1, "limit": 4, "room": 3}
    assert watcher.alert_calls == []  # an unfiltered request reads only the published summary
    assert [a["alert_id"] for a in app.outside_moves(state="closed")["alerts"]] == ["mv-7-1"]
    assert [a["alert_id"] for a in app.outside_moves(since=NOW - 600.0)["alerts"]] == ["mv-1104-1791208800"]
    assert [a["alert_id"] for a in app.outside_moves(since=1791209115.0)["alerts"]] == ["mv-1104-1791208800"]
    assert app.outside_moves(since=1791209115.5)["alerts"] == []
    assert [a["alert_id"] for a in app.outside_moves(limit=1)["alerts"]] == ["mv-1104-1791208800"]
    assert watcher.alert_calls  # a filtered one asks for every alert in memory
    # a demo dashboard always carries the demo caveat, even if a watcher forgot it
    demo_body = make_app(FakeTracker({}), moves=FakeWatcher(summary), demo=True).outside_moves()
    assert demo_body["demo"] is True and demo_body["caveats"][-1] == moves_mod.MOVES_DEMO_CAVEAT
    status = app.status()
    assert status["features"]["moves"] is True and status["moves"]["last_error"] == "Kalshi was slow."
    assert status["tracker"]["read_budget"]["moves"] == {"used": 1, "limit": 4, "room": 3}


def test_moves_body_before_the_first_step_and_from_a_broken_watcher(make_app: Callable[..., web.DashboardApp]) -> None:
    fair_values = SimpleNamespace(mode="auto", providers=[], targets=lambda: [])
    fresh = moves_mod.OutsideMoveWatcher(fair_values, clock=lambda: NOW)
    body = assert_json_safe(make_app(FakeTracker({}), moves=fresh).outside_moves())
    assert (body["enabled"], body["available"], body["error"], body["steps"]) == (True, False, None, 0)
    assert body["alerts"] == [] and body["actionable_ids"] == [] and body["suppressed"] == [] and body["venues"] == []
    assert body["summary"] == moves_mod.LagSummary().to_dict() and body["thresholds"] == MOVES_THRESHOLDS
    assert body["counts"]["suppressed_today"] == NO_SUPPRESSED and body["book_reads"] is None
    broken = make_app(FakeTracker({}), moves=FakeWatcher(fail=RuntimeError("boom")))
    data = assert_json_safe(broken.outside_moves())
    assert (data["enabled"], data["available"], data["alerts"]) == (True, False, [])
    assert "could not be read (RuntimeError)" in data["error"] and "boom" not in data["error"]
    assert broken.strategy()["available"] in (True, False)  # the annotations never break other endpoints
    with serve(broken) as srv:
        assert HTTP(srv.port).get("/api/moves").status == 200


def test_moves_secrets_never_reach_the_body(make_app: Callable[..., web.DashboardApp]) -> None:
    alert = example_l_alert(reason=f"leaked {FAKE_KEY} in a sentence")
    watcher = FakeWatcher(fake_summary([alert]),
                          status={**FakeWatcher().status(), "last_error": f"proxy refused Bearer {FAKE_KEY}"})
    app = make_app(FakeTracker({}), moves=watcher, secrets=[FAKE_KEY])
    with serve(app) as srv:
        http_client = HTTP(srv.port)
        responses = [http_client.get(p) for p in ("/api/moves", "/api/moves?state=open", "/api/status")]
        app.wait_backtest(10)
    for resp in responses:
        assert resp.status == 200
        assert_security_headers(resp)
        assert FAKE_KEY.encode() not in resp.body
    body = responses[0].json()
    assert "***" in body["error"] and "***" in body["alerts"][0]["reason"]


def test_move_annotation() -> None:
    alert = example_l_alert()
    note = web.move_annotation(alert)
    assert note == {"alert_id": "mv-1104-1791208800", "status": "lagging", "status_label": "Cup lagging", "state": "open",
                    "direction": 1, "move": 0.055, "lag_gap_now": 0.055, "detected_at": 1791209115.0, "actionable": True}
    assert web.move_annotation(compact_of(alert)) == note  # the watcher's compact open alert gives the same
    assert web.move_annotation(None) is None and web.move_annotation({}) is None and web.move_annotation("x") is None
    down = web.move_annotation({"alert_id": "mv-2-1", "status": "reverted", "direction": -1, "move": float("nan")})
    assert down is not None and down["status_label"] == "Outside reverted" and down["direction"] == -1
    assert down["move"] is None and down["actionable"] is False and down["state"] == "open"


def test_moves_annotations_on_value_ideas_and_fair_value_rows(make_app: Callable[..., web.DashboardApp],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """§18.4 on a fake watcher: ``outside_move`` only on value ideas with an open alert (also through a leg); every
    /api/fairvalue row gains ``move`` (null without one); nothing else changes and the cached report is not mutated."""
    base = {
        "target_price": None, "stop_price": None, "prob_win": 0.6, "edge": 0.03, "expected_return": 0.05,
        "horizon_hours": None, "suggested_shares": 100, "suggested_cost": 52.0, "score": 0.5, "confidence": 0.7,
        "rationale": [], "risks": [], "side": "yes", "entry_price": 0.52, "title": "t", "option": "YES", "legs": None,
    }
    report = {
        "generated_at": NOW, "risk_mode": "balanced", "headline": "h", "assumptions": [],
        "opportunities": [
            dict(base, kind="value", exchange_id="1104", market_id="552"),  # its own open alert
            dict(base, kind="value", exchange_id="2000", market_id="9"),  # no alert
            dict(base, kind="value", exchange_id=None, market_id="10", legs=[  # an alert on one leg
                {"exchange_id": "3000", "market_id": "10", "title": "t", "option": "A", "side": "yes", "price": 0.4},
                {"exchange_id": "7", "market_id": "10", "title": "t", "option": "B", "side": "yes", "price": 0.5}]),
            dict(base, kind="carry", exchange_id="1104", market_id="552"),  # not a value idea
            dict(base, kind="value", exchange_id="7", market_id="11"),  # a CLOSED alert only
        ],
    }
    calls = {"report": 0}

    def fake_report(inputs: Any, **kwargs: Any) -> Dict[str, Any]:
        calls["report"] += 1
        return copy.deepcopy(report)

    monkeypatch.setattr(strategy_mod, "report_from_inputs", fake_report)
    lagging = example_l_alert()
    leg_alert = example_l_alert(alert_id="mv-7-5", exchange_id="7", status="moved_first", status_label="Cup moved first",
                                actionable=False, trade=None, direction=-1)
    summary = fake_summary([lagging, leg_alert])
    watcher = FakeWatcher(summary)
    app = make_app(FakeTracker({}), moves=watcher)
    data = assert_json_safe(app.strategy())
    own, plain, legs, carry, closed_only = data["opportunities"]
    assert own["outside_move"] == web.move_annotation(lagging) and set(own["outside_move"]) == ANNOTATION_KEYS
    assert "outside_move" not in plain and "outside_move" not in carry
    assert legs["outside_move"]["alert_id"] == "mv-7-5" and legs["outside_move"]["direction"] == -1
    assert closed_only["outside_move"]["alert_id"] == "mv-7-5"  # the open alert of outcome 7
    first = app.strategy()
    assert app.strategy() is first and calls["report"] == 1  # cached, annotated once per published summary
    cached = app._strategy_cache[2]  # the cached report itself is never annotated in place
    assert all("outside_move" not in o for o in cached["opportunities"])
    # the alert on 7 closes: the next published summary drops the annotation without rebuilding the report
    watcher._summary = fake_summary([lagging, dict(leg_alert, state="closed")])
    later = app.strategy()["opportunities"]
    assert calls["report"] == 1 and "outside_move" not in later[2] and "outside_move" in later[0]
    # without a watcher nothing is added
    plain_app = make_app(FakeTracker({}))
    assert all("outside_move" not in o for o in plain_app.strategy()["opportunities"])

    rows = [{"exchange_id": eid, "title": eid} for eid in ("1104", "2000", "7")]
    monkeypatch.setattr(web, "fairvalue_payload", lambda fv, quotes, now, history=None: {
        "now": now, "enabled": True, "rows": copy.deepcopy(rows)})
    fv = app.fairvalue()
    assert [r["move"]["alert_id"] if r["move"] else None for r in fv["rows"]] == ["mv-1104-1791208800", None, None]
    again = app.fairvalue()  # served from the 15-s cache: annotated again, the cache keeps no "move"
    assert again["rows"] == fv["rows"] and all("move" not in r for r in app._fv_cache[1]["rows"])
    assert all(r["move"] is None for r in plain_app.fairvalue()["rows"])


def test_build_demo_with_moves_wires_the_watcher(tmp_path: Path) -> None:
    from supermarket_bot.tracker import MOVES_READS_PER_MIN

    runtime, clock = fast_demo(tmp_path, moves=True, paper=False)
    try:
        watcher = runtime.moves
        assert watcher is not None and runtime.app.moves is watcher and runtime.tracker.moves is watcher
        assert watcher.demo is True and watcher.poll_s == moves_mod.MOVES_POLL_S
        assert watcher.alerts_path == tmp_path / "demo" / moves_mod.MOVES_ALERTS_FILE
        assert [p.name for p in watcher._pollable()] == ["demo-a", "demo-b"]
        # the scripted venues are not fair-value providers: the paper trader never sees their quotes (M12)
        assert [p.name for p in runtime.fair_values.providers] == ["demo"]
        assert runtime.demo_market.outside_moves is True
        assert runtime.tracker.moves_reads_per_min == web.FAST_READS_PER_MIN  # a SimClock run never waits
        body = runtime.app.outside_moves()  # built, not stepped yet
        assert (body["enabled"], body["available"], body["error"], body["alerts"]) == (True, False, None, [])
        assert body["summary"] == moves_mod.LagSummary().to_dict()
        assert body["caveats"] == list(moves_mod.MOVES_CAVEATS) + [moves_mod.MOVES_DEMO_CAVEAT]
    finally:
        runtime.close()
    assert runtime.tracker.moves.step().errors == ["The outside-move watcher is closed."]  # closed with the runtime

    runtime, _ = fast_demo(tmp_path / "b", moves=True, paper=False, moves_poll_s=5, moves_book_reads_per_min=0)
    try:
        assert runtime.moves.poll_s == 5.0 and runtime.tracker.moves_reads_per_min == 0  # 0 means none, even fast
    finally:
        runtime.close()
    real = web.build_demo(tmp_path / "c", 30.0, out=io.StringIO(), news=False, paper=False, moves=True)  # real time
    try:
        assert real.tracker.moves_reads_per_min == MOVES_READS_PER_MIN and real.moves.poll_s == 15.0
    finally:
        real.close()
    real = web.build_demo(tmp_path / "d", 30.0, out=io.StringIO(), news=False, paper=False, moves=True,
                          moves_book_reads_per_min=7)
    try:
        assert real.tracker.moves_reads_per_min == 7
    finally:
        real.close()
    for bad in (4.9, 121, float("nan")):
        with pytest.raises(ValueError, match="--moves-poll must be between 5 and 120 seconds"):
            fast_demo(tmp_path / "e", moves=True, moves_poll_s=bad)


@pytest.mark.parametrize("mode", ["manual", "off"])
def test_build_demo_moves_need_fair_value_auto(tmp_path: Path, mode: str) -> None:
    out = io.StringIO()
    clock = demo_mod.SimClock(demo_mod.SIM_T0)
    runtime = web.build_demo(tmp_path, 30.0, out=out, clock=clock, news=False, paper=False, fair_value=mode, moves=True)
    try:
        assert out.getvalue().splitlines().count("Outside-move alerts need --fair-value auto: off.") == 1
        assert runtime.moves is None and runtime.app.moves is None and runtime.tracker.moves is None
        assert runtime.demo_market.outside_moves is False  # exactly the demo without moves
        assert runtime.app.outside_moves() == disabled_moves_body(clock(), True)
        assert runtime.app.status()["features"]["moves"] is False
    finally:
        runtime.close()


def test_build_demo_without_moves_prints_nothing_about_them(tmp_path: Path) -> None:
    out = io.StringIO()
    runtime = web.build_demo(tmp_path, 30.0, out=out, clock=demo_mod.SimClock(demo_mod.SIM_T0), news=False, paper=False,
                             fair_value="manual")
    try:
        assert runtime.moves is None and runtime.demo_market.outside_moves is False
        assert "Outside-move" not in out.getvalue()
    finally:
        runtime.close()


def test_moves_restart_with_a_kept_database_continues_the_alerts(tmp_path: Path) -> None:
    """``fresh_db=False`` keeps the store and alerts.jsonl (§18.1): a rebuilt demo continues the same alerts and
    never writes a second "opened" line for one."""
    from supermarket_bot import pipeline

    runtime, clock = fast_demo(tmp_path, moves=True, paper=False)
    try:
        pipeline.run_simulation(runtime, clock, hours=8 / 60.0, step_s=30.0, end_run=False, moves_every_s=15.0)
        before = {a["alert_id"]: a for a in runtime.app.outside_moves(limit=500)["alerts"]}
        end = clock()
    finally:
        runtime.close()
    assert before
    clock2 = demo_mod.SimClock(end + 15.0)
    again = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock2, news=False, paper=False, moves=True,
                           fresh_db=False)
    try:
        assert again.demo_market.t0 == demo_mod.SIM_T0  # the scripted events continue (D47)
        pipeline.run_simulation(again, clock2, hours=60 / 3600.0, step_s=30.0, end_run=False, moves_every_s=15.0)
        after = {a["alert_id"]: a for a in again.app.outside_moves(limit=500)["alerts"]}
        assert set(before) <= set(after)
        for aid, alert in before.items():
            assert after[aid]["t_base"] == alert["t_base"] and after[aid]["detected_at"] == alert["detected_at"]
    finally:
        again.close()
    lines = [json.loads(line) for line in (tmp_path / "demo" / "alerts.jsonl").read_text(encoding="utf-8").splitlines()]
    opened = [line["alert"]["alert_id"] for line in lines if line["event"] == "opened"]
    assert len(opened) == len(set(opened)) and set(before) <= set(opened)


def test_simulation_options_moves_keys() -> None:
    parse = build_parser().parse_args
    on = web.simulation_options(parse(["dashboard"]))
    assert (on["moves"], on["moves_poll_s"], on["moves_book_reads_per_min"]) == (True, 15.0, 4)
    off = web.simulation_options(parse(["dashboard", "--no-moves", "--moves-poll", "30", "--moves-book-reads", "0"]))
    assert (off["moves"], off["moves_poll_s"], off["moves_book_reads_per_min"]) == (False, 30.0, 0)
    assert web.simulation_options(parse(["paper"]))["moves"] is False  # `paper` never runs the watcher
    bare = web.simulation_options(SimpleNamespace())
    assert (bare["moves"], bare["moves_poll_s"], bare["moves_book_reads_per_min"]) == (False, None, None)


@pytest.mark.parametrize("value", ["4", "121", "nan", "-15"])
def test_run_dashboard_rejects_a_bad_moves_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("built despite a bad --moves-poll")

    monkeypatch.setattr(web, "build_demo", never)
    monkeypatch.setattr(web, "build_live", never)
    err = io.StringIO()
    args = _dashboard_args(tmp_path, "--demo", "--no-browser", "--moves-poll", value)
    assert web.run_dashboard(None, args, out=io.StringIO(), err=err) == 2
    assert "--moves-poll must be between 5 and 120 seconds" in err.getvalue()


def test_run_dashboard_demo_passes_the_moves_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def capture(data_dir: Path, interval: float, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise KeyboardInterrupt  # stop right there ("Stopped before the dashboard started.")

    monkeypatch.setattr(web, "build_demo", capture)
    args = _dashboard_args(tmp_path, "--demo", "--no-browser", "--moves-poll", "20", "--moves-book-reads", "2")
    assert web.run_dashboard(None, args, out=io.StringIO(), err=io.StringIO()) == 130
    assert (seen["moves"], seen["moves_poll_s"], seen["moves_book_reads_per_min"]) == (True, 20.0, 2)
    seen.clear()
    web.run_dashboard(None, _dashboard_args(tmp_path, "--demo", "--no-browser", "--no-moves"), out=io.StringIO(),
                      err=io.StringIO())
    assert seen["moves"] is False


def test_runtime_close_closes_the_watcher_before_the_fair_values(tmp_path: Path) -> None:
    order: List[str] = []

    def thing(name: str, method: str = "close") -> Any:
        return SimpleNamespace(**{method: lambda: order.append(name)})

    runtime = web.Runtime(app=None, tracker=thing("tracker", "stop"), store=thing("store"), client=thing("client"),  # type: ignore[arg-type]
                          context=None, news=thing("news"), fair_values=thing("fair values"), moves=thing("moves"))
    runtime.close()
    assert order == ["tracker", "news", "moves", "fair values", "client", "store"]


def _live_moves_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: Any) -> Tuple[web.Runtime, str]:
    """``build_live`` over the demo API (no network: nothing is started, no outside request is made)."""
    monkeypatch.delenv("SUPERMARKET_LLM", raising=False)
    market = demo_mod.DemoMarket(seed=7)

    def from_settings(cls: Any, settings: Settings, **kwargs: Any) -> SuperMarketClient:
        return market.client(api_key=settings.api_key, transport=market.transport(), reads_per_min=10_000)

    monkeypatch.setattr(SuperMarketClient, "from_settings", classmethod(from_settings))
    settings = Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path)
    out = io.StringIO()
    return web.build_live(settings, args, out=out), out.getvalue()


def test_build_live_builds_the_watcher_with_the_dashboard_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from supermarket_bot.fairvalue import FV_CAVEATS, OUTSIDE_READS_PER_MIN_WITH_MOVES  # noqa: F401
    from supermarket_bot.tracker import MOVES_READS_PER_MIN

    args = build_parser().parse_args(["dashboard", "--no-news", "--no-paper"])
    runtime, text = _live_moves_runtime(tmp_path, monkeypatch, args)
    try:
        watcher = runtime.moves
        assert watcher is not None and runtime.app.moves is watcher and runtime.tracker.moves is watcher
        assert watcher.demo is False and watcher.poll_s == 15.0
        assert watcher.alerts_path == tmp_path / demo_mod.DEMO_SLUG / moves_mod.MOVES_ALERTS_FILE
        # the watcher polls the fair-value service's own providers: one host budget (45 a minute), one backoff
        assert [p.name for p in watcher._pollable()] == [p.name for p in runtime.fair_values.providers]
        assert [p.name for p in runtime.fair_values.providers] == ["polymarket", "kalshi"]
        assert all(p.budget()["limit"] == OUTSIDE_READS_PER_MIN_WITH_MOVES for p in runtime.fair_values.providers)
        assert runtime.tracker.moves_reads_per_min == MOVES_READS_PER_MIN
        lines = text.splitlines()
        notice = ("Outside-move alerts: Polymarket and Kalshi every 15 s (GET only, about 25 and 15 reads a minute); "
                  "use --no-moves to stop.")
        assert lines.index(notice) == lines.index(web.FV_AUTO_NOTICE) + 1
        body = runtime.app.outside_moves()
        assert (body["enabled"], body["available"], body["demo"], body["steps"]) == (True, False, False, 0)
        assert body["caveats"] == list(moves_mod.MOVES_CAVEATS)
        assert runtime.app.status()["features"]["moves"] is True
    finally:
        runtime.close()


@pytest.mark.parametrize("argv, expect", [
    (["dashboard", "--no-news", "--no-paper", "--no-moves"], "off"),
    (["dashboard", "--no-news", "--no-paper", "--fair-value", "manual"], "notice"),
    (["dashboard", "--no-news", "--no-paper", "--no-fair-value"], "notice"),
    (["dashboard", "--no-news", "--no-paper", "--moves-poll", "30", "--moves-book-reads", "0"], "on"),
])
def test_build_live_moves_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: List[str], expect: str) -> None:
    runtime, text = _live_moves_runtime(tmp_path, monkeypatch, build_parser().parse_args(argv))
    try:
        if expect == "on":
            assert runtime.moves is not None and runtime.moves.poll_s == 30.0
            assert runtime.tracker.moves_reads_per_min == 0
            assert ("Outside-move alerts: Polymarket and Kalshi every 30 s (GET only, about 15 and 9 reads a minute); "
                    "use --no-moves to stop.") in text.splitlines()
            return
        assert runtime.moves is None and runtime.app.moves is None and runtime.tracker.moves is None
        assert "Outside-move alerts: Polymarket" not in text
        assert text.splitlines().count(web.MOVES_FV_NOTICE) == (1 if expect == "notice" else 0)
        if runtime.fair_values is not None:  # without the watcher the hosts keep the 30-a-minute budget
            assert all(p.budget()["limit"] == 30 for p in runtime.fair_values.providers)
    finally:
        runtime.close()


def test_build_live_without_the_moves_option_builds_no_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§18.5: an old caller's namespace (no ``no_moves`` attribute) means off."""
    args = SimpleNamespace(interval=30.0, no_news=True, no_paper=True, fair_value="auto", tournament=None, public=False)
    runtime, text = _live_moves_runtime(tmp_path, monkeypatch, args)
    try:
        assert runtime.moves is None and runtime.tracker.moves is None and runtime.app.moves is None
        assert "Outside-move" not in text
        assert runtime.app.outside_moves()["enabled"] is False
    finally:
        runtime.close()


def test_moves_live_notice_numbers() -> None:
    assert web.moves_live_notice(15.0) == ("Outside-move alerts: Polymarket and Kalshi every 15 s (GET only, about 25 "
                                           "and 15 reads a minute); use --no-moves to stop.")
    fast = web.moves_live_notice(5.0)
    assert fast.startswith("Outside-move alerts: Polymarket and Kalshi every 5 s (GET only, about 65 and 39 reads")
    assert "some polls will be skipped" in fast
    assert "skipped" not in web.moves_live_notice(10.0)  # 35 a minute fits under 45 minus the refresh's 9
