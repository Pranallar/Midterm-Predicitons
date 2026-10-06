"""The read-only rule, proved (docs/PAPER_TRADING.md §0, §7.8, D20).

1. Runtime: a fast demo simulation with the paper trader and outside fair values (the demo feed plus Polymarket and
   Kalshi providers on recording fixture transports) for 200 steps sends nothing but GETs; every Super Market client
   the builders create carries the GET-only guard; a direct POST raises ``ReadOnlyViolation``; outside requests are
   anonymous. The same with the outside-move watcher (docs/OUTSIDE_MOVES.md §0): its polls and its Cup book reads.
2. The Polymarket and Kalshi providers against fixture transports: GET only.
3. Static (AST) over every simulator module (``moves.py`` included, with no allowlist entry): no write call, no write
   verb, no ``/orders`` path, except an explicit allowlist of (file, enclosing function).

Offline.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import httpx
import pytest

import supermarket_bot.demo as demo_mod
from supermarket_bot import pipeline, web
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import Settings
from supermarket_bot.fairvalue import KalshiProvider, PolymarketProvider, build_targets, races_for
from supermarket_bot.models import ExchangeInfo
from supermarket_bot.readonly import ReadOnlyViolation, is_guarded

PACKAGE = Path(__file__).resolve().parent.parent / "supermarket_bot"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "external"
FORBIDDEN_PATHS = ("/orders", "cancel", "multi-leg", "realtime/token")
FAKE_KEY = "ace_live_READONLY_0f1e2d3c4b5a6978"


# --------------------------------------------------------------------------- recording transports


class OutsideRecorder:
    """Polymarket / Kalshi from the recorded fixtures: ``/markets`` reads answered from the fixture pools, anything
    else 404 (the providers degrade). Records every request."""

    def __init__(self) -> None:
        self.calls: List[httpx.Request] = []
        self.gamma: Dict[str, Dict[str, Any]] = {}
        self.kalshi: Dict[str, Dict[str, Any]] = {}
        for name in ("polymarket_markets.json", "polymarket_events.json", "kalshi_markets.json", "kalshi_events.json"):
            for r in json.loads((FIXTURES / name).read_text(encoding="utf-8"))["responses"]:
                body = r.get("body")
                if isinstance(body, list):
                    for m in body:
                        if isinstance(m, dict) and m.get("id") is not None and "question" in m:
                            self.gamma.setdefault(str(m["id"]), m)
                        for sub in (m.get("markets") or []) if isinstance(m, dict) else []:
                            if isinstance(sub, dict) and sub.get("id") is not None:
                                self.gamma.setdefault(str(sub["id"]), sub)
                elif isinstance(body, dict):
                    for m in body.get("markets") or []:
                        if isinstance(m, dict) and m.get("ticker"):
                            self.kalshi.setdefault(m["ticker"], m)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        u = request.url
        if "polymarket" in u.host and u.path == "/markets":
            return httpx.Response(200, json=[self.gamma[i] for i in u.params.get_list("id") if i in self.gamma])
        if "kalshi" in u.host and u.path.endswith("/markets") and "tickers" in u.params:
            tickers = u.params["tickers"].split(",")
            return httpx.Response(200, json={"markets": [self.kalshi[t] for t in tickers if t in self.kalshi], "cursor": ""})
        return httpx.Response(404, json={"error": "no fixture"})


def _assert_reads_only(requests: Sequence[httpx.Request]) -> None:
    assert requests
    for r in requests:
        assert r.method in ("GET", "HEAD"), f"{r.method} {r.url}"
        assert not any(bad in r.url.path for bad in FORBIDDEN_PATHS), str(r.url)


def _assert_anonymous(requests: Sequence[httpx.Request], secrets: Sequence[str]) -> None:
    for r in requests:
        assert "authorization" not in r.headers and "cookie" not in r.headers, str(r.url)
        for secret in secrets:
            assert secret not in str(r.url)


# --------------------------------------------------------------------------- (1) runtime


def test_a_fast_simulation_with_paper_and_fair_values_sends_only_gets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sm_calls: List[httpx.Request] = []
    original = demo_mod.DemoMarket.transport

    def recording_transport(self: Any) -> httpx.MockTransport:
        inner = original(self)

        def handler(request: httpx.Request) -> httpx.Response:
            sm_calls.append(request)
            return inner.handle_request(request)

        return httpx.MockTransport(handler)

    monkeypatch.setattr(demo_mod.DemoMarket, "transport", recording_transport)
    outside = OutsideRecorder()
    clock = demo_mod.SimClock(demo_mod.SIM_T0)

    def providers(market: Any) -> List[Any]:
        kw = dict(transport=httpx.MockTransport(outside), reads_per_min=100_000, clock=clock, sleep=lambda s: None)
        return [demo_mod.DemoFairValueProvider(market), PolymarketProvider(**kw), KalshiProvider(**kw)]

    runtime = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock, fair_value_providers=providers)
    try:
        assert is_guarded(runtime.client) and is_guarded(runtime.tracker._quick)
        pipeline.run_simulation(runtime, clock, hours=199 * 30 / 3600, step_s=30.0)  # 200 steps
        body = runtime.app.paper()
        assert body["run"]["steps"] == 200 and sum(p["fills"] for p in body["portfolios"]) > 0
        with pytest.raises(ReadOnlyViolation):
            runtime.client.request("POST", "/orders", json={"exchangeId": "9001", "side": "yes"})
        with pytest.raises(ReadOnlyViolation):
            runtime.tracker._quick.request("DELETE", "/orders/1")
        with pytest.raises(ReadOnlyViolation):
            runtime.client.mint_realtime_token()
        secrets = list(getattr(runtime.client, "_secrets", ()))
    finally:
        runtime.close()
    _assert_reads_only(sm_calls)
    paths = {r.url.path for r in sm_calls}
    assert any(p.endswith("/orderbook") for p in paths) and any(p.endswith("/trades") for p in paths)  # paper reads
    assert any(r.url.params.get("status") == "settled" for r in sm_calls)  # settlements read
    _assert_reads_only(outside.calls)
    _assert_anonymous(outside.calls, secrets)
    assert {r.url.host for r in outside.calls} >= {"gamma-api.polymarket.com"}


def test_a_fast_simulation_with_outside_moves_sends_only_gets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/OUTSIDE_MOVES.md §0 / §22: a fast demo with the outside-move watcher (its scripted venues, Cup order-book
    reads for lagging alerts) and, polled by a second watcher on the same tracker, the real Polymarket and Kalshi
    providers on the recording fixture transports: every Super Market and every outside request is an anonymous GET,
    and no outside poll ever goes through the Super Market client."""
    from supermarket_bot.moves import MoveParams, OutsideMoveWatcher
    from supermarket_bot.tracker import TrackerCupFeed

    sm_calls: List[httpx.Request] = []
    original = demo_mod.DemoMarket.transport

    def recording_transport(self: Any) -> httpx.MockTransport:
        inner = original(self)

        def handler(request: httpx.Request) -> httpx.Response:
            sm_calls.append(request)
            return inner.handle_request(request)

        return httpx.MockTransport(handler)

    monkeypatch.setattr(demo_mod.DemoMarket, "transport", recording_transport)
    outside = OutsideRecorder()
    clock = demo_mod.SimClock(demo_mod.SIM_T0)
    made: List[Any] = []

    def providers(market: Any) -> List[Any]:
        kw = dict(transport=httpx.MockTransport(outside), reads_per_min=100_000, clock=clock, sleep=lambda s: None)
        made.extend([PolymarketProvider(**kw), KalshiProvider(**kw)])
        return [demo_mod.DemoFairValueProvider(market)] + made

    runtime = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock, fair_value_providers=providers,
                             moves=True, paper=False, news=False)
    try:
        assert runtime.moves is not None and runtime.tracker.moves is runtime.moves
        assert is_guarded(runtime.client) and is_guarded(runtime.tracker._quick)
        http_watcher = OutsideMoveWatcher(runtime.fair_values, providers=made, cup=TrackerCupFeed(runtime.tracker),
                                          clock=clock, params=MoveParams(poll_s=15.0))
        before = len(sm_calls)
        for _ in range(20):
            pipeline.run_simulation(runtime, clock, hours=60 / 3600, step_s=30.0, moves_every_s=15.0, end_run=False)
            http_watcher.step()
        steps = runtime.tracker.status()["moves"]["steps"]
        assert steps >= 40 and sm_calls[before:]  # the watcher stepped (and the Cup was read) during the run
        with pytest.raises(ReadOnlyViolation):
            runtime.tracker._quick.request("POST", "/orders", json={"exchangeId": "9035"})
        secrets = list(getattr(runtime.client, "_secrets", ()))
    finally:
        runtime.close()
        for p in made:
            p.close()
    _assert_reads_only(sm_calls)
    _assert_reads_only(outside.calls)
    _assert_anonymous(outside.calls, secrets)
    # the watcher's own outside reads are the batched /markets reads of the validated matches (GET, no account)
    polls = [r for r in outside.calls if r.url.path.endswith("/markets") and ("id" in r.url.params or "tickers" in r.url.params)]
    assert polls and all(r.method == "GET" for r in polls)
    assert not any("polymarket" in r.url.host or "kalshi" in r.url.host for r in sm_calls)


def test_every_builder_client_is_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERMARKET_LLM", raising=False)
    demo_rt = web.build_demo(tmp_path / "d", 30.0, out=io.StringIO(), news=False)
    try:
        assert is_guarded(demo_rt.client) and is_guarded(demo_rt.tracker._quick)
        assert is_guarded(demo_rt.app.client)
    finally:
        demo_rt.close()

    market = demo_mod.DemoMarket(seed=7)
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return market.transport().handle_request(request)

    def from_settings(cls: Any, settings: Settings, **kwargs: Any) -> SuperMarketClient:
        return market.client(api_key=settings.api_key, transport=httpx.MockTransport(handler), reads_per_min=10_000)

    monkeypatch.setattr(SuperMarketClient, "from_settings", classmethod(from_settings))
    from supermarket_bot.cli import build_parser

    args = build_parser().parse_args(["dashboard", "--no-news", "--fair-value", "manual"])
    out = io.StringIO()
    live = web.build_live(Settings(api_key=FAKE_KEY, tournament=demo_mod.DEMO_SLUG, data_dir=tmp_path / "l"), args, out=out)
    try:
        assert is_guarded(live.client) and is_guarded(live.tracker._quick)
        with pytest.raises(ReadOnlyViolation):
            live.client.request("PUT", "/orders/1")
        live.tracker.run_once()
        live.tracker.paper_step()
    finally:
        live.close()
    _assert_reads_only(seen)
    assert "Outside fair values" not in out.getvalue()  # manual mode: no outside hosts are read


# --------------------------------------------------------------------------- (2) providers on fixtures


def _targets() -> List[Any]:
    infos = [ExchangeInfo(str(9100 + i), str(400 + i), "YES", title, None)
             for i, title in enumerate([
                 "Will the Democratic Party win the New Hampshire Senate?",
                 "Will the Republican Party win the New Hampshire Senate?",
                 "Will the Democratic Party win the Maine Senate?",
                 "Will the Republican Party win the Maine Senate?",
                 "Will the Democratic Party win the Nevada Governor?",
             ])]
    races = races_for(infos)
    return build_targets(infos, races)


@pytest.mark.parametrize("provider_cls", [PolymarketProvider, KalshiProvider])
def test_outside_providers_only_get(provider_cls: Any) -> None:
    outside = OutsideRecorder()
    provider = provider_cls(transport=httpx.MockTransport(outside), clock=lambda: 1_791_208_800.0, sleep=lambda s: None)
    try:
        result = provider.refresh(_targets(), 1_791_208_800.0)
        assert result is not None
    finally:
        provider.close()
    _assert_reads_only(outside.calls)
    _assert_anonymous(outside.calls, [FAKE_KEY])


# --------------------------------------------------------------------------- (3) static AST check

STATIC_FILES = ("fairvalue.py", "paper.py", "backtest.py", "study.py", "pipeline.py", "sizing.py", "strategy.py", "depth.py",
                "readonly.py", "tracker.py", "cli.py", "demo.py", "store.py", "web.py",
                "moves.py")  # docs/OUTSIDE_MOVES.md §0: the outside-move watcher, with NO allowlist entry
WRITE_ATTRS = {"post", "put", "patch", "delete", "send", "stream", "build_request", "mint_realtime_token"}
WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE"}
READ_VERBS = {"GET", "HEAD"}
# (file, enclosing function) pairs that may mention a write verb: the dashboard's own local POST routes and their
# 405 answers, the demo's simulated API (it serves requests, never sends them), and the stream command (it mints a
# realtime token with its own client and is not part of the simulator).
ALLOWLIST: Set[Tuple[str, str]] = {
    ("web.py", "do_POST"),
    ("web.py", "_api_get"),
    ("demo.py", "<module>"),
    ("cli.py", "cmd_stream"),
    ("cli.py", "run_stream"),
}


def _scan(path: Path) -> List[Tuple[str, int, str, str]]:
    """``(file, line, enclosing function, what)`` for every write-looking construct in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: List[Tuple[str, int, str, str]] = []

    def literal(node: Any) -> Optional[str]:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                if target.attr in WRITE_ATTRS:
                    hits.append((path.name, node.lineno, func, f".{target.attr}()"))
                if target.attr == "Request" and isinstance(target.value, ast.Name) and target.value.id == "httpx":
                    hits.append((path.name, node.lineno, func, "httpx.Request()"))
                if target.attr == "request":
                    first = literal(node.args[0]) if node.args else None
                    if first is None or first.upper() not in READ_VERBS:
                        hits.append((path.name, node.lineno, func, ".request() without a literal GET/HEAD"))
            for kw in node.keywords:
                if kw.arg == "method":
                    value = literal(kw.value)
                    if value is None or value.upper() not in READ_VERBS:
                        hits.append((path.name, node.lineno, func, "method= other than GET/HEAD"))
        text = literal(node)
        if text is not None:
            if text in WRITE_VERBS:
                hits.append((path.name, getattr(node, "lineno", 0), func, f"literal {text!r}"))
            elif "/orders" in text:
                hits.append((path.name, getattr(node, "lineno", 0), func, "an /orders path"))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "<module>")
    return hits


def test_static_moves_py_is_scanned_and_has_no_allowlist_entry() -> None:
    assert "moves.py" in STATIC_FILES and not any(f == "moves.py" for f, _ in ALLOWLIST)
    assert _scan(PACKAGE / "moves.py") == []


def test_static_no_write_paths_outside_the_allowlist() -> None:
    hits: List[Tuple[str, int, str, str]] = []
    for name in STATIC_FILES:
        hits.extend(_scan(PACKAGE / name))
    unexpected = [h for h in hits if (h[0], h[2]) not in ALLOWLIST]
    assert not unexpected, "write-looking code outside the allowlist:\n" + "\n".join(
        f"  {f}:{line} in {fn}: {what}" for f, line, fn, what in unexpected)
    allowed = sorted({(h[0], h[2]) for h in hits})
    assert ("demo.py", "<module>") in allowed and ("web.py", "_api_get") in allowed  # the known, allowed mentions


def test_the_static_scan_finds_a_planted_write(tmp_path: Path) -> None:
    planted = tmp_path / "bad.py"
    planted.write_text(
        "import httpx\n"
        "def f(client):\n"
        "    client.post('/x')\n"
        "    client.request('POST', '/y')\n"
        "    client.request(verb, '/z')\n"
        "    httpx.Request('GET', 'https://x')\n"
        "    client.get('/a', method='PATCH')\n"
        "    return '/orders/cancel-all', 'DELETE', 'DELETE FROM ticks'\n",
        encoding="utf-8",
    )
    found = [what for _, _, fn, what in _scan(planted)]
    assert found.count(".request() without a literal GET/HEAD") == 2
    assert ".post()" in found and "httpx.Request()" in found and "method= other than GET/HEAD" in found
    assert "an /orders path" in found and "literal 'DELETE'" in found and "literal 'POST'" in found
    assert "literal 'PATCH'" in found and len(found) == 9  # "DELETE FROM ticks" (SQL) is fine
