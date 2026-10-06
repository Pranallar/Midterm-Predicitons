"""Tests for supermarket_bot/demo.py: the simulated Super Market API and DemoNewsProvider.

Everything goes through the real :class:`SuperMarketClient` (or a raw ``httpx`` client on the
demo's ``MockTransport`` where the error body itself is under test); nothing touches the
network. Time is injected: ``DemoMarket(now=T0, clock=...)`` pins the demo start, so every run
sees the same prices.

* Response shapes are validated against ``docs/supermarket-openapi.json`` with a small
  validator (``$ref``, ``allOf``/``anyOf``/``oneOf``, types, ``required``, ``enum``, bounds).
* The scripted events (docs/DESIGN.md, Demo section) run through the real tracker, attribution
  and news pipeline on a fake clock that steps through the first 45 simulated minutes.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx
import pytest

import supermarket_bot.demo as demo_mod
from supermarket_bot.attribution import Attributor
from supermarket_bot.bot import resolve_context
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.demo import DEMO_BASE_URL, DEMO_SLUG, DEMO_TOURNAMENT_ID, DemoMarket, DemoNewsProvider
from supermarket_bot.errors import ApiError
from supermarket_bot.models import Article
from supermarket_bot.news import NewsProvider, NewsSearcher
from supermarket_bot.store import TrackerStore
from supermarket_bot.tracker import Tracker

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "supermarket-openapi.json"
T0 = 1_790_900_000.0  # 2026-10-02T00:13:20Z: the demo start for every test here
MIN, HOUR, DAY = 60.0, 3600.0, 86400.0
CUP_END = "2026-11-04T17:00:00.000Z"
TID = DEMO_TOURNAMENT_ID
OTHER_UUID = "00000000-0000-4000-8000-000000000000"
# 9023 belongs to the settled debate market; 9024-9034 (markets 316-326) were added for the paper trader
# (docs/PAPER_TRADING.md §7.6); 9034 (the Maine debate) settles live at T0 + 40 min.
OPEN_EXCHANGES = [str(9001 + i) for i in range(22)] + [str(9024 + i) for i in range(11)]
ALL_EXCHANGES = [str(9001 + i) for i in range(34)]
ALL_MARKETS = [str(301 + i) for i in range(26)]
PAPER_SCRIPTED = {"9033", "9034"}  # the Wyoming liquidity hole and the Maine debate's rise are scripted moves
MULTI_MARKETS = {"311", "312", "313", "314"}
PA, OH, MI = "9001", "9002", "9003"  # news surge, participant spike, live surge
BAND_BEFORE_CUP_END = {"9007", "9008"}
BAND_AFTER_CUP_END = {"9009", "9010"}


# --------------------------------------------------------------------------- helpers


class Offset:
    """Demo clock offset: ``market.now() == T0 + offset.now``."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def on_tick(price: float) -> bool:
    return abs(price / 0.005 - round(price / 0.005)) < 1e-6


def make_market(seed: int = 7, offset: Optional[Offset] = None) -> DemoMarket:
    return DemoMarket(seed=seed, now=T0, clock=offset if offset is not None else (lambda: 0.0))


def make_client(market: DemoMarket) -> SuperMarketClient:
    return market.client(reads_per_min=100_000)


# --------------------------------------------------------------------------- OpenAPI validation


class Spec:
    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    def deref(self, schema: Any) -> Any:
        for _ in range(50):
            if not (isinstance(schema, dict) and "$ref" in schema):
                return schema
            node: Any = self.data
            for part in schema["$ref"].lstrip("#/").split("/"):
                node = node[part]
            extra = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**node, **extra} if extra else node
        raise AssertionError("$ref loop")

    def response_schema(self, path: str, method: str = "get", status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        responses = self.data["paths"][path][method]["responses"]
        if status is None:
            status = "200" if "200" in responses else "201"
        if status not in responses:
            return None
        response = self.deref(responses[status])
        return response.get("content", {}).get("application/json", {}).get("schema")

    def validate(self, value: Any, schema: Any, where: str = "$", depth: int = 0) -> List[str]:
        errors: List[str] = []
        schema = self.deref(schema)
        if not isinstance(schema, dict) or depth > 40:
            return errors
        for sub in schema.get("allOf", []):
            errors += self.validate(value, sub, where, depth + 1)
        for key in ("anyOf", "oneOf"):
            if key in schema:
                options = [self.validate(value, sub, where, depth + 1) for sub in schema[key]]
                if all(options):
                    errors.append(f"{where}: matches no {key} branch ({'; '.join(o[0] for o in options)})")
        kinds = schema.get("type")
        if kinds is not None:
            kinds = list(kinds) if isinstance(kinds, list) else [kinds]
            if schema.get("nullable"):
                kinds.append("null")
            if not any(_is_type(value, k) for k in kinds):
                return errors + [f"{where}: expected {kinds}, got {type(value).__name__} {value!r:.60}"]
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} not in {schema['enum']}")
        if "const" in schema and value != schema["const"]:
            errors.append(f"{where}: {value!r} != {schema['const']!r}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{where}: {value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{where}: {value} > maximum {schema['maximum']}")
        if isinstance(value, dict):
            errors += [f"{where}: missing {name!r}" for name in schema.get("required", []) if name not in value]
            for name, sub in (schema.get("properties") or {}).items():
                if name in value:
                    errors += self.validate(value[name], sub, f"{where}.{name}", depth + 1)
        if isinstance(value, list) and "items" in schema:
            for i, item in enumerate(value[:60]):
                errors += self.validate(item, schema["items"], f"{where}[{i}]", depth + 1)
        return errors


def _is_type(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, {"string": str, "boolean": bool, "object": dict, "array": list}[kind])


@pytest.fixture(scope="module")
def spec() -> Spec:
    return Spec(json.loads(SPEC_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def market() -> DemoMarket:
    """A demo frozen at T0 (its clock never moves)."""
    return make_market()


@pytest.fixture(scope="module")
def api(market: DemoMarket) -> Iterator[SuperMarketClient]:
    client = make_client(market)
    yield client
    client.close()


@pytest.fixture(scope="module")
def raw(market: DemoMarket) -> Iterator[httpx.Client]:
    """A plain httpx client on the demo transport, for looking at error bodies directly."""
    client = httpx.Client(transport=market.transport(), base_url=DEMO_BASE_URL + "/", headers={"Authorization": "Bearer demo"})
    yield client
    client.close()


# --------------------------------------------------------------------------- shapes vs the OpenAPI spec


SHAPE_CASES: List[Tuple[str, str, str, Callable[[SuperMarketClient], Any]]] = [
    ("account", "/account", "get", lambda c: c.get_account()),
    ("tournaments", "/tournaments", "get", lambda c: c.list_tournaments()),
    ("tournament", "/tournaments/{slug}", "get", lambda c: c.get_tournament(DEMO_SLUG)),
    ("tournament-markets", "/tournaments/{slug}/markets", "get", lambda c: c.list_tournament_markets(DEMO_SLUG)),
    ("tournament-market-multi", "/tournaments/{slug}/markets/{id}", "get", lambda c: c.get_tournament_market(DEMO_SLUG, "311")),
    ("tournament-market-settled", "/tournaments/{slug}/markets/{id}", "get", lambda c: c.get_tournament_market(DEMO_SLUG, "315")),
    ("markets", "/markets", "get", lambda c: c.list_markets(tournament_id=TID, status="any")),
    ("market", "/markets/{id}", "get", lambda c: c.get_market("301", tournament_id=TID)),
    ("market-settled", "/markets/{id}", "get", lambda c: c.get_market("315", tournament_id=TID)),
    ("nodes-binary", "/markets/{id}/nodes", "get", lambda c: c.get_market_nodes("301", tournament_id=TID)),
    ("nodes-multi", "/markets/{id}/nodes", "get", lambda c: c.get_market_nodes("311", tournament_id=TID)),
    ("market-orderbook", "/markets/{id}/orderbook", "get", lambda c: c.get_market_orderbook("311", tournament_id=TID)),
    ("market-orderbook-settled", "/markets/{id}/orderbook", "get", lambda c: c.get_market_orderbook("315", tournament_id=TID)),
    ("exchanges", "/exchanges", "get", lambda c: c.list_exchanges(tournament_id=TID)),
    ("prices", "/exchanges/prices", "get", lambda c: c.get_prices(OPEN_EXCHANGES + ["9023", "1"], tournament_id=TID)),
    ("price", "/exchanges/{id}/price", "get", lambda c: c.get_exchange_price("9001", tournament_id=TID)),
    ("price-settled", "/exchanges/{id}/price", "get", lambda c: c.get_exchange_price("9023", tournament_id=TID)),
    ("orderbook", "/exchanges/{id}/orderbook", "get", lambda c: c.get_exchange_orderbook("9002", depth=20, tournament_id=TID)),
    ("price-history-1h", "/exchanges/{id}/price-history", "get", lambda c: c.get_price_history("9001", tournament_id=TID, resolution="1h", limit=168)),
    ("price-history-5m", "/exchanges/{id}/price-history", "get", lambda c: c.get_price_history("9002", tournament_id=TID, resolution="5m", start=iso(T0 - DAY), limit=100)),
    ("trades", "/exchanges/{id}/trades", "get", lambda c: c.get_trades("9001", tournament_id=TID, limit=200)),
    ("constraints", "/relationships/constraints", "get", lambda c: c.get_relationship_constraints(tournament_id=TID)),
    ("violations", "/relationships/constraints", "get", lambda c: c.get_relationship_constraints(violations_only=True, tournament_id=TID)),
    ("tournament-leaderboard", "/tournaments/{slug}/leaderboard", "get", lambda c: c.get_tournament_leaderboard(DEMO_SLUG, limit=100)),
    ("leaderboards", "/leaderboards", "get", lambda c: c.get_leaderboard(tournament_slug=DEMO_SLUG, period="7d")),
    ("positions", "/tournaments/{slug}/portfolio/positions", "get", lambda c: c.get_tournament_positions(DEMO_SLUG)),
    ("pnl-week", "/tournaments/{slug}/portfolio/pnl", "get", lambda c: c.get_tournament_pnl(DEMO_SLUG, "week")),
    ("pnl-all", "/tournaments/{slug}/portfolio/pnl", "get", lambda c: c.get_tournament_pnl(DEMO_SLUG, "all")),
    ("realtime-token", "/realtime/token", "post", lambda c: c.mint_realtime_token()),
]


@pytest.mark.parametrize("path, method, call", [pytest.param(p, m, f, id=name) for name, p, m, f in SHAPE_CASES])
def test_responses_match_the_openapi_spec(spec: Spec, api: SuperMarketClient, path: str, method: str, call: Callable[[SuperMarketClient], Any]) -> None:
    body = call(api)
    schema = spec.response_schema(path, method)
    assert schema is not None
    assert spec.validate(body, schema) == []


def test_the_validator_catches_mistakes(spec: Spec, api: SuperMarketClient) -> None:
    schema = spec.response_schema("/exchanges/{id}/price")
    good = api.get_exchange_price("9001", tournament_id=TID)
    assert spec.validate(good, schema) == []
    broken = {**good, "exchangeId": 9001}
    del broken["bestBid"]
    errors = spec.validate(broken, schema)
    assert any("bestBid" in e for e in errors) and any("exchangeId" in e for e in errors)
    book = api.get_exchange_orderbook("9001", tournament_id=TID)
    assert spec.validate({**book, "bids": [{"price": "0.5", "quantity": 1}]}, spec.response_schema("/exchanges/{id}/orderbook"))


def test_tournament_and_account(api: SuperMarketClient) -> None:
    t = api.get_tournament(DEMO_SLUG)
    assert t["id"] == TID and t["slug"] == DEMO_SLUG
    assert t["name"] == "Predictions Cup — Midterm Elections (demo)"
    assert t["currencyName"] == "SUSQies" and t["initialBalance"] == 100_000 and t["status"] == "active"
    assert t["endDate"] == CUP_END and t["marketCount"] == 26
    account = api.get_account()
    assert account["balance"] == t["myBalance"]
    listed = api.list_tournaments()
    assert [x["slug"] for x in listed["data"]] == [DEMO_SLUG] and listed["pagination"]["total"] == 1
    assert api.list_tournaments(status="ended")["data"] == []
    assert len(api.list_tournaments(market_id="301")["data"]) == 1
    assert api.list_tournaments(market_id="999")["data"] == []


def test_markets_and_outcomes(api: SuperMarketClient) -> None:
    markets = api.list_markets(tournament_id=TID, status="any")["data"]
    assert sorted(m["id"] for m in markets) == ALL_MARKETS
    exchanges = [ex for m in markets for ex in m["exchanges"]]
    assert sorted(ex["id"] for ex in exchanges) == ALL_EXCHANGES
    by_id = {m["id"]: m for m in markets}
    assert by_id["301"]["title"] == "Will Republicans keep control of the Pennsylvania Senate?"
    assert by_id["302"]["title"] == "Will Republicans win Ohio House District 9?"
    arizona = by_id["311"]
    assert arizona["title"] == "Who will win the Arizona Governor race?" and arizona["isMultiOutcome"] is True
    assert [ex["option"] for ex in arizona["exchanges"]] == ["Democratic nominee", "Republican nominee", "Any other candidate"]
    assert {m["id"] for m in markets if m["isMultiOutcome"]} == MULTI_MARKETS
    for m in markets:
        assert m["categories"] == ["Election Outcome"] and m["isComposite"] is False
        assert m["contexts"][0]["tournament"]["id"] == TID
        for ex in m["exchanges"]:
            assert ex["latestPrice"] is None or (0 < ex["latestPrice"] < 1 and on_tick(ex["latestPrice"]))
    settled = by_id["315"]
    assert settled["status"] == "settled" and settled["settledWith"] == "YES" and settled["settledOn"]
    assert all(m["status"] == "open" for m in markets if m["id"] != "315")


def test_prices_and_books_are_two_sided_on_the_tick(api: SuperMarketClient) -> None:
    prices = api.get_prices(OPEN_EXCHANGES, tournament_id=TID)
    assert [p["exchangeId"] for p in prices["data"]] == OPEN_EXCHANGES and prices["missingIds"] == []
    for p in prices["data"]:
        assert 0 < p["bestBid"] < p["bestAsk"] < 1
        assert on_tick(p["bestBid"]) and on_tick(p["bestAsk"]) and on_tick(p["latestPrice"])
        assert p["spread"] == pytest.approx(p["bestAsk"] - p["bestBid"])
    book = api.get_exchange_orderbook("9001", depth=20, tournament_id=TID)
    bids, asks = [lv["price"] for lv in book["bids"]], [lv["price"] for lv in book["asks"]]
    assert bids == sorted(bids, reverse=True) and asks == sorted(asks)
    assert len(bids) == len(asks) == 20 and bids[0] < asks[0]
    assert all(lv["quantity"] >= 1 and isinstance(lv["quantity"], int) for lv in book["bids"] + book["asks"])
    assert (book["bestBid"], book["bestAsk"]) == (bids[0], asks[0])
    assert isinstance(book["asOf"]["sequence"], int) and epoch(book["asOf"]["at"]) == pytest.approx(T0, abs=0.001)
    thin = api.get_exchange_orderbook(OH, depth=20, tournament_id=TID)
    near = sum(lv["quantity"] for lv in thin["bids"] + thin["asks"] if abs(lv["price"] - (thin["bestBid"] + thin["bestAsk"]) / 2) <= 0.05)
    assert near < 500  # Ohio's thin book is part of the participant-spike story


def test_nodes(api: SuperMarketClient) -> None:
    binary = api.get_market_nodes("301", tournament_id=TID)
    assert binary["market_id"] == "301" and binary["root"]["node_type"] == "contract"
    multi = api.get_market_nodes("311", tournament_id=TID)
    assert multi["root"]["node_type"] == "operator" and multi["root"]["operator"] == "OR"
    assert [c["contract_details"]["outcome"] for c in multi["root"]["children"]] == ["Democratic nominee", "Republican nominee", "Any other candidate"]


def test_leaderboard_portfolio_and_token(api: SuperMarketClient) -> None:
    board = api.get_tournament_leaderboard(DEMO_SLUG, limit=100)
    rows = board["leaderboard"]
    assert board["total"] == len(rows) == 51  # + the three far-ahead leaders of the paper-trading demo (§7.6)
    pnls = [r["pnl"] for r in rows]
    assert pnls == sorted(pnls, reverse=True) and [r["rank"] for r in rows] == sorted(r["rank"] for r in rows)
    me = next(r for r in rows if r["username"] == "demo_trader")
    assert board["myRank"] == me["rank"]
    top3 = api.get_tournament_leaderboard(DEMO_SLUG, limit=3)
    assert [r["username"] for r in top3["leaderboard"]] == [r["username"] for r in rows[:3]]
    by_roi = api.get_leaderboard(tournament_slug=DEMO_SLUG, period="1d", sort="roi", limit=100)["leaderboard"]
    assert [r["roi"] for r in by_roi] == sorted((r["roi"] for r in by_roi), reverse=True)

    positions = api.get_tournament_positions(DEMO_SLUG)
    summary = positions["summary"]
    assert summary["totalMarketValue"] == pytest.approx(sum(p["marketValue"] for p in positions["positions"]))
    assert summary["totalCostBasis"] == pytest.approx(sum(p["costBasis"] for p in positions["positions"]))
    pnl = api.get_tournament_pnl(DEMO_SLUG, "week")
    assert pnl["totalAccountValue"] == pytest.approx(api.get_account()["balance"] + pnl["totalHoldingsValue"])
    assert pnl["periodEnd"] == demo_mod._iso(T0)
    assert api.get_tournament_pnl(DEMO_SLUG, "all")["periodPnl"] is None

    token = api.mint_realtime_token()
    assert token["token"].startswith("demo.") and epoch(token["expiresAt"]) == pytest.approx(T0 + 3 * HOUR, abs=1)


# --------------------------------------------------------------------------- pagination and filters


def _cursor_pages(fetch: Callable[[Optional[str]], Dict[str, Any]], items_key: str = "data") -> List[Dict[str, Any]]:
    pages, cursor = [], None
    for _ in range(100):
        page = fetch(cursor)
        pages.append(page)
        if not page["pagination"]["hasMore"]:
            assert page["pagination"]["nextCursor"] is None
            return pages
        cursor = page["pagination"]["nextCursor"]
        assert cursor
    raise AssertionError("pagination did not end")


def test_market_cursor_pagination(api: SuperMarketClient) -> None:
    pages = _cursor_pages(lambda cur: api.list_markets(tournament_id=TID, status="any", limit=4, cursor=cur))
    ids = [m["id"] for p in pages for m in p["data"]]
    assert len(pages) == 7 and [len(p["data"]) for p in pages] == [4, 4, 4, 4, 4, 4, 2]
    assert len(ids) == len(set(ids)) and sorted(ids) == ALL_MARKETS
    assert all(p["pagination"]["total"] == 26 and p["pagination"]["limit"] == 4 for p in pages)
    assert [m["id"] for m in api.iter_markets(tournament_id=TID, status="any")] == [m["id"] for m in api.list_markets(tournament_id=TID, status="any")["data"]]

    tm_pages = _cursor_pages(lambda cur: api.list_tournament_markets(DEMO_SLUG, limit=6, cursor=cur))
    assert sorted(m["id"] for p in tm_pages for m in p["data"]) == ALL_MARKETS
    assert "contexts" not in tm_pages[0]["data"][0]  # tournament rows are not the canonical shape

    ex_pages = _cursor_pages(lambda cur: api.list_exchanges(tournament_id=TID, limit=10, cursor=cur))
    assert [len(p["data"]) for p in ex_pages] == [10, 10, 10, 4]
    assert [e["id"] for p in ex_pages for e in p["data"]] == ALL_EXCHANGES


def test_trade_cursor_pagination(api: SuperMarketClient) -> None:
    since = iso(T0 - 6 * HOUR)
    pages = _cursor_pages(lambda cur: api.get_trades(PA, tournament_id=TID, start=since, limit=25, cursor=cur))
    trades = [t for p in pages for t in p["data"]]
    assert len(pages) >= 3
    ids = [int(t["id"]) for t in trades]
    times = [epoch(t["createdAt"]) for t in trades]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == len(ids)  # newest first, no duplicates
    assert times == sorted(times, reverse=True)
    assert T0 - 6 * HOUR <= min(times) and max(times) <= T0
    single = api.get_trades(PA, tournament_id=TID, start=since, limit=200)
    assert len(trades) < 200 and single["data"] == trades and single["pagination"]["hasMore"] is False
    assert list(api.iter_trades(PA, tournament_id=TID, start=since)) == trades
    for t in trades:
        assert t["side"] in ("YES", "NO") and t["size"] >= 1 and on_tick(t["price"])
        assert t["volume"] == pytest.approx(t["size"] * (t["price"] if t["side"] == "YES" else 1 - t["price"]))


def test_trade_window_filters(api: SuperMarketClient) -> None:
    lo, hi = T0 - 3 * HOUR, T0 - HOUR
    page = api.get_trades(PA, tournament_id=TID, start=iso(lo), end=iso(hi), limit=200)
    times = [epoch(t["createdAt"]) for t in page["data"]]
    assert times and all(lo <= ts < hi for ts in times)
    assert page["from"] == demo_mod._iso(lo) and page["to"] == demo_mod._iso(hi)
    assert api.get_trades(PA, tournament_id=TID, start=iso(T0 + HOUR), limit=10)["data"] == []  # nothing from the future


def test_price_history_window_pagination(api: SuperMarketClient) -> None:
    start = T0 - 48 * HOUR
    candles: List[Dict[str, Any]] = []
    cursor = iso(start)
    for _ in range(20):
        page = api.get_price_history(PA, tournament_id=TID, resolution="1h", start=cursor, limit=10)
        candles += page["candles"]
        if page["coverage"]["complete"]:
            break
        assert len(page["candles"]) == 10
        cursor = page["to"]  # the exclusive continuation boundary
    else:
        raise AssertionError("price history did not complete")
    whole = api.get_price_history(PA, tournament_id=TID, resolution="1h", start=iso(start), limit=1000)
    assert whole["coverage"]["complete"] is True and candles == whole["candles"]
    times = [epoch(c["time"]) for c in candles]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert all(ts % 3600 == 0 and math.floor(start / HOUR) * HOUR <= ts <= T0 for ts in times)  # `from` is floored
    for c in candles:
        assert c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"]
        assert c["low"] - 1e-9 <= c["vwap"] <= c["high"] + 1e-9 and c["volume"] > 0 and c["tradeCount"] >= 1
    newest = api.get_price_history(PA, tournament_id=TID, resolution="1h", limit=5)
    assert len(newest["candles"]) == 5 and newest["candles"] == whole["candles"][-5:]
    assert newest["from"] == newest["candles"][0]["time"]


def test_market_filters_and_sorting(api: SuperMarketClient, raw: httpx.Client) -> None:
    def ids(**kw: Any) -> List[str]:
        return [m["id"] for m in api.list_markets(tournament_id=TID, **kw)["data"]]

    assert sorted(ids(status="open")) == [m for m in ALL_MARKETS if m != "315"] and ids(status="settled") == ["315"]
    assert ids(status="resolved") == ["315"]
    senate = ids(status="any", search="SENATE")
    assert senate and all("senate" in m["title"].lower() for m in api.list_markets(tournament_id=TID, status="any", search="senate")["data"])
    assert "301" in senate and "311" not in senate
    assert set(ids(status="any", is_multi_outcome=True)) == MULTI_MARKETS
    assert set(ids(status="any", is_multi_outcome=False)) == set(ALL_MARKETS) - MULTI_MARKETS
    assert sorted(ids(status="any", ids=["301", "311", "999"])) == ["301", "311"]
    assert ids(status="any", category="sports-outcome") == [] and len(ids(status="any", category="election-outcome")) == 26
    assert ids(status="any", is_composite=True) == []
    assert ids(status="any", sort="oldest") == ALL_MARKETS and ids(status="any", sort="recent") == ALL_MARKETS[::-1]
    closing = api.list_markets(tournament_id=TID, status="any", sort="closing")["data"]
    dates = [m["settlementDate"] for m in closing]
    assert dates == sorted(dates)
    assert sorted(ids(status="any", sort="trending")) == ALL_MARKETS
    assert [e["id"] for e in api.list_exchanges(market_id="311", tournament_id=TID)["data"]] == ["9011", "9012", "9013"]
    assert [e["id"] for e in api.list_exchanges(ids=["9002", "9001", "777"], tournament_id=TID)["data"]] == ["9002", "9001"]
    prices = api.request("GET", "/exchanges/prices", params={"ids": "9001,9001,999,9002", "tournamentId": TID})
    assert [p["exchangeId"] for p in prices["data"]] == ["9001", "9002"] and prices["missingIds"] == ["999"]
    book = api.get_exchange_orderbook(PA, depth=3, tournament_id=TID)
    assert book["depth"] == 3 and len(book["bids"]) == len(book["asks"]) == 3


def test_constraint_filters(api: SuperMarketClient, market: DemoMarket) -> None:
    every = api.get_relationship_constraints(tournament_id=TID)
    assert len(every["data"]) == 3 and every["violationsCount"] == 1
    assert {r["evaluationStatus"] for r in every["data"]} == {"violated", "satisfied"}
    senate = api.get_relationship_constraints(market_id="313", tournament_id=TID)["data"]
    assert [r["evaluationStatus"] for r in senate] == ["violated"]
    house = api.get_relationship_constraints(market_id="312", tournament_id=TID)["data"]
    assert [r["evaluationStatus"] for r in house] == ["satisfied"]
    rid = market.scenario["violation_relationship_id"]
    assert [r["relationshipId"] for r in api.get_relationship_constraints(relationship_id=rid, tournament_id=TID)["data"]] == [rid]
    assert api.get_relationship_constraints(violations_only=True, min_violation=0.05, tournament_id=TID)["data"] == []
    assert epoch(every["computedAt"]) == pytest.approx(T0, abs=0.001)


# --------------------------------------------------------------------------- error envelopes


ERROR_CASES: List[Tuple[str, str, str, Dict[str, str], int, str, Optional[str]]] = [
    # (method, url path, spec path or "", params, status, code, field named in details.fieldErrors)
    ("GET", "markets", "/markets", {"tournamentId": "not-a-uuid"}, 400, "VALIDATION_ERROR", "tournamentId"),
    ("GET", "markets", "/markets", {"tournamentId": OTHER_UUID}, 404, "NOT_FOUND", None),
    ("GET", "exchanges/prices", "/exchanges/prices", {"ids": ",".join(str(9001 + i) for i in range(101))}, 400, "VALIDATION_ERROR", "ids"),
    ("GET", "exchanges/prices", "/exchanges/prices", {}, 400, "VALIDATION_ERROR", "ids"),
    ("GET", "exchanges/prices", "/exchanges/prices", {"ids": "abc"}, 400, "VALIDATION_ERROR", "ids"),
    ("GET", "exchanges/prices", "/exchanges/prices", {"ids": "9001", "tournamentId": "bad"}, 400, "VALIDATION_ERROR", "tournamentId"),
    ("GET", "exchanges/9001/price", "/exchanges/{id}/price", {"tournamentId": "123"}, 400, "VALIDATION_ERROR", "tournamentId"),
    ("GET", "exchanges/9999/price", "/exchanges/{id}/price", {}, 404, "NOT_FOUND", None),
    ("GET", "exchanges/abc/price", "/exchanges/{id}/price", {}, 400, "INVALID_ID", None),
    ("GET", "markets/999", "/markets/{id}", {}, 404, "NOT_FOUND", None),
    ("GET", "markets/abc", "/markets/{id}", {}, 400, "INVALID_ID", None),
    ("GET", "markets/301/orderbook", "/markets/{id}/orderbook", {"depth": "0"}, 400, "VALIDATION_ERROR", "depth"),
    ("GET", "tournaments/nope", "/tournaments/{slug}", {}, 404, "NOT_FOUND", None),
    ("GET", "tournaments/nope/leaderboard", "/tournaments/{slug}/leaderboard", {}, 404, "NOT_FOUND", None),
    ("GET", f"tournaments/{DEMO_SLUG}/leaderboard", "/tournaments/{slug}/leaderboard", {"period": "decade"}, 400, "VALIDATION_ERROR", "period"),
    ("GET", "leaderboards", "/leaderboards", {"sort": "luck"}, 400, "VALIDATION_ERROR", "sort"),
    ("GET", "exchanges/9001/price-history", "/exchanges/{id}/price-history", {"resolution": "2m"}, 400, "VALIDATION_ERROR", "resolution"),
    ("GET", "exchanges/9001/price-history", "/exchanges/{id}/price-history", {"limit": "0"}, 400, "VALIDATION_ERROR", "limit"),
    ("GET", "exchanges/9001/price-history", "/exchanges/{id}/price-history", {"limit": "1001"}, 400, "VALIDATION_ERROR", "limit"),
    ("GET", "exchanges/9001/price-history", "/exchanges/{id}/price-history", {"from": "yesterday"}, 400, "VALIDATION_ERROR", "from"),
    ("GET", "exchanges/9001/price-history", "/exchanges/{id}/price-history", {"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:30:00Z"}, 400, "VALIDATION_ERROR", "to"),
    ("GET", "exchanges/9001/trades", "/exchanges/{id}/trades", {"limit": "201"}, 400, "VALIDATION_ERROR", "limit"),
    ("GET", "exchanges/9001/trades", "/exchanges/{id}/trades", {"from": "2026-10-01T02:00:00Z", "to": "2026-10-01T01:00:00Z"}, 400, "VALIDATION_ERROR", "to"),
    ("GET", "exchanges/9001/trades", "/exchanges/{id}/trades", {"cursor": "not*a*cursor"}, 400, "INVALID_CURSOR", None),
    ("GET", "exchanges/9001/trades", "/exchanges/{id}/trades", {"cursor": base64.urlsafe_b64encode(b"mk:4").decode()}, 400, "INVALID_CURSOR", None),
    ("GET", "markets", "/markets", {"cursor": base64.urlsafe_b64encode(b"tr:4").decode()}, 400, "INVALID_CURSOR", None),
    ("GET", "markets", "/markets", {"limit": "101"}, 400, "VALIDATION_ERROR", "limit"),
    ("GET", "markets", "/markets", {"sort": "random"}, 400, "VALIDATION_ERROR", "sort"),
    ("GET", "exchanges/9001/orderbook", "/exchanges/{id}/orderbook", {"depth": "201"}, 400, "VALIDATION_ERROR", "depth"),
    ("GET", "exchanges", "/exchanges", {"marketId": "311", "ids": "9011"}, 400, "VALIDATION_ERROR", "marketId"),
    ("GET", "relationships/constraints", "/relationships/constraints", {"minViolation": "2"}, 400, "VALIDATION_ERROR", "minViolation"),
    ("GET", "relationships/constraints", "/relationships/constraints", {"marketId": "abc"}, 400, "VALIDATION_ERROR", "marketId"),
    ("GET", "relationships/constraints", "/relationships/constraints", {"marketId": "999"}, 404, "NOT_FOUND", None),
    ("GET", "relationships/constraints", "/relationships/constraints", {"violationsOnly": "yes"}, 400, "VALIDATION_ERROR", "violationsOnly"),
    ("GET", "nope", "", {}, 404, "NOT_FOUND", None),
    ("GET", "orders", "", {}, 404, "NOT_FOUND", None),  # read-only: no order endpoints at all
    ("POST", "orders", "", {}, 404, "NOT_FOUND", None),
    ("DELETE", "orders/1", "", {}, 404, "NOT_FOUND", None),
    ("POST", "orders/cancel-all", "", {}, 404, "NOT_FOUND", None),
    ("PUT", "markets/301", "", {}, 404, "NOT_FOUND", None),
]


@pytest.mark.parametrize(
    "method, url, spec_path, params, status, code, field",
    [pytest.param(*case, id=f"{case[0]} {case[1]} {case[4]} {case[5]} {'&'.join(case[3])}"[:90]) for case in ERROR_CASES],
)
def test_error_envelopes(spec: Spec, raw: httpx.Client, method: str, url: str, spec_path: str, params: Dict[str, str], status: int, code: str, field: Optional[str]) -> None:
    resp = raw.request(method, url, params=params)
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert err["code"] == code and isinstance(err["message"], str) and err["message"]
    assert set(err) <= {"code", "message", "details"}
    if field is not None:
        assert field in err["details"]["fieldErrors"]
    if spec_path:
        schema = spec.response_schema(spec_path, method.lower(), str(status))
        if schema is not None:
            assert spec.validate(body, schema) == []


def test_missing_api_key_is_401(market: DemoMarket) -> None:
    with httpx.Client(transport=market.transport(), base_url=DEMO_BASE_URL + "/") as plain:
        for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}):
            resp = plain.get("markets", headers=headers)
            assert resp.status_code == 401 and resp.json()["error"]["code"] == "MISSING_API_KEY"


def test_the_client_maps_demo_errors(api: SuperMarketClient) -> None:
    ids = [str(9001 + i) for i in range(101)]
    with pytest.raises(ApiError) as caught:
        api.request("GET", "/exchanges/prices", params={"ids": ids, "tournamentId": TID})
    assert (caught.value.status, caught.value.code) == (400, "VALIDATION_ERROR")
    assert "ids" in caught.value.details["fieldErrors"]
    with pytest.raises(ApiError) as caught:
        api.get_market("301", tournament_id="not-a-uuid")
    assert (caught.value.status, caught.value.code) == (400, "VALIDATION_ERROR")
    with pytest.raises(ApiError) as caught:
        api.get_exchange_price("424242", tournament_id=TID)
    assert (caught.value.status, caught.value.code) == (404, "NOT_FOUND")
    # get_prices chunks large requests to 100 ids, so a long list works through the client
    bulk = api.get_prices(ids, tournament_id=TID)
    # every open outcome is quoted; the settled debate (9023) is missing like any settled outcome (§7.6 (l))
    assert len(bulk["data"]) == 33 and len(bulk["missingIds"]) == 101 - 33 and "9023" in bulk["missingIds"]


# --------------------------------------------------------------------------- determinism and time consistency


def _snapshot(client: SuperMarketClient) -> Dict[str, Any]:
    return {
        "prices": client.get_prices(OPEN_EXCHANGES, tournament_id=TID),
        "book": client.get_exchange_orderbook(PA, depth=20, tournament_id=TID),
        "trades": client.get_trades(OH, tournament_id=TID, limit=200),
        "history_1h": client.get_price_history(PA, tournament_id=TID, resolution="1h", limit=168),
        "history_5m": client.get_price_history(MI, tournament_id=TID, resolution="5m", limit=288),
        "constraints": client.get_relationship_constraints(tournament_id=TID),
        "leaderboard": client.get_tournament_leaderboard(DEMO_SLUG, limit=100),
        "markets": client.list_markets(tournament_id=TID, status="any"),
        "arizona": client.get_market_orderbook("311", tournament_id=TID),
    }


def test_same_seed_same_market() -> None:
    first, second = make_market(), make_market()
    with make_client(first) as a, make_client(second) as b:
        snap_a, snap_b = _snapshot(a), _snapshot(b)
        assert snap_a == snap_b
        assert _snapshot(a) == snap_a  # repeat reads (warm caches) agree too


def test_different_seed_different_prices_same_story() -> None:
    with make_client(make_market(seed=7)) as a, make_client(make_market(seed=8)) as b:
        ha = {e: a.get_price_history(e, tournament_id=TID, resolution="1h", limit=168)["candles"] for e in OPEN_EXCHANGES[:6]}
        hb = {e: b.get_price_history(e, tournament_id=TID, resolution="1h", limit=168)["candles"] for e in OPEN_EXCHANGES[:6]}
        assert ha != hb
        titles = lambda c: [m["title"] for m in c.list_markets(tournament_id=TID, status="any")["data"]]  # noqa: E731
        assert titles(a) == titles(b)


def test_history_agrees_with_later_queries() -> None:
    offset = Offset()
    demo = make_market(offset=offset)
    with make_client(demo) as client:
        window = {"start": iso(T0 - 30 * HOUR), "end": iso(T0 - 2 * HOUR)}
        before_1h = client.get_price_history(OH, tournament_id=TID, resolution="1h", limit=1000, **window)["candles"]
        before_5m = client.get_price_history(PA, tournament_id=TID, resolution="5m", limit=1000, **window)["candles"]
        before_trades = client.get_trades(PA, tournament_id=TID, limit=200, **window)["data"]
        offset.now = 3 * HOUR  # three simulated hours later
        assert client.get_price_history(OH, tournament_id=TID, resolution="1h", limit=1000, **window)["candles"] == before_1h
        assert client.get_price_history(PA, tournament_id=TID, resolution="5m", limit=1000, **window)["candles"] == before_5m
        assert client.get_trades(PA, tournament_id=TID, limit=200, **window)["data"] == before_trades
        later = client.get_price_history(PA, tournament_id=TID, resolution="1h", limit=1000, start=iso(T0 - HOUR))["candles"]
        assert max(epoch(c["time"]) for c in later) > T0  # and new candles appear as time passes


def _consistency(client: SuperMarketClient, demo: DemoMarket) -> float:
    now = demo.now()
    worst = 0.0
    prices = {p["exchangeId"]: p for p in client.get_prices(OPEN_EXCHANGES, tournament_id=TID)["data"]}
    for eid in OPEN_EXCHANGES:
        snap = prices[eid]
        close_5m = client.get_price_history(eid, tournament_id=TID, resolution="5m", limit=12)["candles"][-1]
        close_1h = client.get_price_history(eid, tournament_id=TID, resolution="1h", limit=2)["candles"][-1]
        last_trade = client.get_trades(eid, tournament_id=TID, limit=1)["data"][0]
        # latestPrice is the last trade, and both candle resolutions close on it
        assert close_5m["close"] == close_1h["close"] == snap["latestPrice"] == last_trade["price"], eid
        assert epoch(last_trade["createdAt"]) <= now
        assert demo.last_trade_at(eid, now) == snap["latestPrice"]
        mid = (snap["bestBid"] + snap["bestAsk"]) / 2
        assert demo.mid_at(eid, now) == pytest.approx(mid)
        book = client.get_exchange_orderbook(eid, depth=1, tournament_id=TID)
        assert (book["bestBid"], book["bestAsk"]) == (snap["bestBid"], snap["bestAsk"])
        worst = max(worst, abs(mid - snap["latestPrice"]))
    return worst


def test_snapshots_agree_with_price_history_and_the_tape() -> None:
    offset = Offset()
    demo = make_market(offset=offset)
    with make_client(demo) as client:
        assert _consistency(client, demo) <= 0.05  # the last print sits within a few ticks of the mid
        offset.now = 10 * MIN + 7.5  # later, between two scripted trades
        assert _consistency(client, demo) <= 0.05


def test_price_history_includes_a_trade_printed_exactly_now() -> None:
    offset = Offset()
    demo = make_market(offset=offset)
    offset.now = 10 * MIN  # a scripted Ohio NO trade prints at exactly T0 + 10 min
    with make_client(demo) as client:
        latest = client.get_exchange_price(OH, tournament_id=TID)["latestPrice"]
        assert client.get_trades(OH, tournament_id=TID, limit=1)["data"][0]["price"] == latest
        assert client.get_price_history(OH, tournament_id=TID, resolution="5m", limit=3)["candles"][-1]["close"] == latest


def test_settled_market_has_no_book(api: SuperMarketClient) -> None:
    price = api.get_exchange_price("9023", tournament_id=TID)
    assert price["bestBid"] is None and price["bestAsk"] is None and price["spread"] is None
    assert price["latestPrice"] == pytest.approx(0.99, abs=0.02)  # it drifted up before settling YES
    book = api.get_exchange_orderbook("9023", tournament_id=TID)
    assert book["bids"] == [] and book["asks"] == [] and book["spread"] is None
    assert api.list_markets(tournament_id=TID, status="open", ids=["315"])["data"] == []


# --------------------------------------------------------------------------- scripted events (static checks)


def test_scenario_description(market: DemoMarket) -> None:
    sc = market.scenario
    assert sc["t0"] == T0 and sc["tournament_id"] == TID and sc["slug"] == DEMO_SLUG
    assert (sc["news_surge"]["exchange_id"], sc["participant_spike"]["exchange_id"], sc["live_surge"]["exchange_id"]) == (PA, OH, MI)
    assert sc["news_surge"]["news_at"] < sc["news_surge"]["start"] < T0
    assert sc["live_surge"]["start"] == T0 + 90
    assert sc["arbitrage_market_id"] == "311" and sc["settled_market_id"] == "315"
    bands = {b["exchange_id"]: b for b in sc["high_band"]}
    assert set(bands) == BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END
    assert {e for e, b in bands.items() if b["settles_before_cup_end"]} == BAND_BEFORE_CUP_END
    assert bands["9010"]["favorite"] == "NO"


def test_high_band_outcomes_hover_in_the_high_90s(api: SuperMarketClient) -> None:
    cup_end = epoch(CUP_END)
    markets = {m["id"]: m for m in api.list_markets(tournament_id=TID, status="open")["data"]}
    favourites = {}
    for eid in OPEN_EXCHANGES:
        candles = api.get_price_history(eid, tournament_id=TID, resolution="5m", start=iso(T0 - 6 * HOUR), limit=1000)["candles"]
        closes = [c["close"] for c in candles]
        fav = [max(p, 1 - p) for p in closes]
        if closes and min(fav) >= 0.95:
            favourites[eid] = (min(fav), max(fav), closes[-1])
    assert BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END <= set(favourites)
    assert 4 <= len(favourites) <= 6  # + the Nebraska Democratic long shot (NO ~0.97), added for the paper trader
    for eid in BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END:
        low, high, _ = favourites[eid]
        assert 0.95 <= low and high <= 0.99
    settles = {ex["id"]: epoch(m["settlementDate"]) for m in markets.values() for ex in m["exchanges"]}
    assert all(settles[e] < cup_end for e in BAND_BEFORE_CUP_END)
    assert all(settles[e] > cup_end for e in BAND_AFTER_CUP_END)
    assert favourites["9010"][2] < 0.5  # the Libertarian long shot: NO is the favourite


def test_arbitrage_flag_on_the_multi_outcome_market(api: SuperMarketClient) -> None:
    arizona = api.get_market_orderbook("311", tournament_id=TID)
    assert arizona["hasArbitrageOpportunity"] is True
    asks = [row["asks"][0]["price"] for row in arizona["exchanges"]]
    assert len(asks) == 3 and sum(asks) < 1.0 and arizona["overround"] == pytest.approx(sum(asks))
    assert arizona["contexts"][0]["orderbook"]["hasArbitrageOpportunity"] is True
    for mid in sorted(MULTI_MARKETS - {"311"}):
        assert api.get_market_orderbook(mid, tournament_id=TID)["hasArbitrageOpportunity"] is False
    single = api.get_market_orderbook("301", tournament_id=TID)
    assert single["hasArbitrageOpportunity"] is False  # binary markets never flag
    tm = api.get_tournament_market(DEMO_SLUG, "311")
    assert tm["hasArbitrageOpportunity"] is True and tm["overround"] == arizona["overround"]


def test_exactly_one_constraint_violation(api: SuperMarketClient, market: DemoMarket) -> None:
    violations = api.get_relationship_constraints(violations_only=True, tournament_id=TID)
    assert violations["violationsCount"] == 1 and len(violations["data"]) == 1
    row = violations["data"][0]
    assert row["relationshipId"] == market.scenario["violation_relationship_id"]
    assert row["evaluationStatus"] == "violated" and row["type"] == "complementary"
    assert row["violationAmount"] >= 0.01 and row["currentValue"] == pytest.approx(1.0 + row["violationAmount"])
    assert {o["marketId"] for o in row["observedPrices"]} == {"313"}
    assert [t["exchangeId"] for t in row["suggestedCorrectiveTrades"]] == ["9016", "9017"]
    assert {t["action"] for t in row["suggestedCorrectiveTrades"]} == {"sell"}  # the legs sum above 1


# --------------------------------------------------------------------------- scripted events through the real pipeline


@pytest.fixture(scope="module")
def timeline() -> Iterator[SimpleNamespace]:
    """Tracker + attribution + news on the demo, stepping a fake clock from T0 to T0 + 45 min."""
    offset = Offset()
    demo = make_market(offset=offset)
    client = make_client(demo)
    context = resolve_context(client, slug=DEMO_SLUG)
    store = TrackerStore(":memory:")
    provider = DemoNewsProvider(demo)
    news = NewsSearcher([provider], store=store, clock=demo.now)
    attributor = Attributor(client, context, store, news, clock=demo.now)
    tracker = Tracker(
        client, context, store, interval=30, attributor=attributor,
        backfill_reads_per_min=10_000, analyze_reads_per_min=10_000, clock=demo.now,
    )
    try:
        first = tracker.run_once()
        backfill_reads = 0
        for _ in range(100):
            reads = tracker.backfill_step(50)
            if not reads:
                break
            backfill_reads += reads
        second = tracker.run_once()
        tracker.analyze_pending(20)
        at_t0 = {s.exchange_id: s for s in store.surges()}
        view_t0 = tracker.view()
        detections: List[Tuple[float, str, int]] = []
        while offset.now < 45 * MIN:
            offset.now += 30 if offset.now < 10 * MIN else 120
            summary = tracker.run_once()
            for sid in summary["new_surges"]:
                detections.append((offset.now, store.get_surge(sid).exchange_id, sid))
            tracker.analyze_pending(20)
        yield SimpleNamespace(
            market=demo, offset=offset, client=client, store=store, tracker=tracker, provider=provider,
            first=first, second=second, backfill_reads=backfill_reads, at_t0=at_t0, view_t0=view_t0,
            detections=detections, end={s.exchange_id: s for s in store.surges()}, view_end=tracker.view(),
        )
    finally:
        client.close()
        store.close()


def test_pipeline_start_up(timeline: SimpleNamespace) -> None:
    assert (timeline.first["markets"], timeline.first["exchanges"], timeline.first["ticks"]) == (25, 33, 33)
    assert timeline.backfill_reads == 66  # 33 outcomes x (7 days of 1h + 24 hours of 5m)
    assert len(timeline.second["new_surges"]) == 2 and timeline.second["errors"] == 0
    assert timeline.tracker.status()["errors"] == 0


def test_news_surge_on_pennsylvania_is_attributed_to_news(timeline: SimpleNamespace) -> None:
    surge = timeline.at_t0[PA]
    assert surge.direction == "up" and surge.change >= 0.10  # the scripted +0.14 ramp
    assert surge.start_ts <= T0 - 50 * MIN <= surge.end_ts
    att = surge.attribution
    assert att is not None and att.verdict == "news"
    assert att.confidence >= 0.5 and att.reversion_odds == pytest.approx(0.2)
    assert att.articles and all("Pennsylvania" in a.title for a in att.articles)
    assert all(T0 - 55 * MIN - 1 <= a.published_at <= T0 for a in att.articles)
    assert all(a.url.startswith("https://example.com/news/") and a.provider == "demo-news" for a in att.articles)
    assert att.flow is not None and att.flow.n_trades > 10  # many small trades, not a few big ones
    end = timeline.end[PA]
    assert end.id == surge.id and end.status == "open" and (end.reverted_fraction or 0.0) < 0.5  # news moves hold


def test_participant_spike_on_ohio_is_attributed_to_participants(timeline: SimpleNamespace) -> None:
    surge = timeline.at_t0[OH]
    assert surge.direction == "up" and surge.change >= 0.15  # the scripted +0.18
    att = surge.attribution
    assert att is not None and att.verdict == "participants"
    assert att.articles == []  # no news for this market
    assert att.flow.n_trades <= 5 and att.flow.max_trade_size == 520 and att.flow.yes_share == 1.0
    assert att.book_depth is not None and att.book_depth < 500
    assert att.reversion_odds > 0.5
    end = timeline.end[OH]
    assert end.id == surge.id
    assert end.status == "reverted" and end.reverted_fraction >= 0.5  # about 60% given back by T0 + 40 min


def test_live_surge_on_michigan_appears_about_90s_after_start(timeline: SimpleNamespace) -> None:
    assert MI not in timeline.at_t0
    live = [(at, sid) for at, eid, sid in timeline.detections if eid == MI]
    assert len(live) == 1
    at, sid = live[0]
    # Seen in the first snapshot that shows the jump (t0 + 90 s): the last 5m candle before the
    # tracker started holds through the trade-less buckets, so the 5m window has a reference
    # (functional-3; before that fix it took until the 5m window was covered by live ticks).
    assert 90 <= at <= 300
    surge = timeline.store.get_surge(sid)
    assert surge.direction == "up" and surge.change >= 0.05
    assert surge.detected_at == pytest.approx(T0 + at)
    assert surge.attribution is not None and surge.attribution.articles == []
    assert timeline.end[MI].status == "reverted"  # 60% of the move reverts within 40 minutes


def test_live_surge_on_michigan_is_attributed_to_participants(timeline: SimpleNamespace) -> None:
    sid = next(sid for _, eid, sid in timeline.detections if eid == MI)
    assert timeline.store.get_surge(sid).attribution.verdict == "participants"


def test_background_noise_never_trips_a_surge(timeline: SimpleNamespace) -> None:
    # Only scripted moves surge: the three originals and the paper-trading scenarios (k) the Wyoming hole
    # (T0 + 20 min) and (l) the Maine debate's rise before it settles (§7.6).
    assert set(timeline.end) == {PA, OH, MI} | PAPER_SCRIPTED
    assert [eid for _, eid, _ in timeline.detections if eid not in PAPER_SCRIPTED] == [MI]


def test_tracker_sees_the_high_band_outcomes(timeline: SimpleNamespace) -> None:
    bands = {b["exchange_id"]: b for b in timeline.view_t0["high_band"]}
    assert BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END <= set(bands) and 4 <= len(bands) <= 6  # + Nebraska D (NO ~0.97)
    cup_end = epoch(CUP_END)
    for eid in BAND_BEFORE_CUP_END | BAND_AFTER_CUP_END:
        band = bands[eid]
        assert band["stable"] is True and band["favorite_price"] >= 0.95 and band["time_in_band"] >= 0.8
        assert (epoch(band["settlement_date"]) < cup_end) == (eid in BAND_BEFORE_CUP_END)
    assert bands["9010"]["side"] == "NO" and bands["9007"]["side"] == "YES"


def test_tracker_context_has_the_arbitrage_and_violation(timeline: SimpleNamespace) -> None:
    ctx = timeline.view_t0["context"]
    overround = {row["market_id"]: row for row in ctx["overround"]}
    assert set(overround) == MULTI_MARKETS
    assert [m for m, row in overround.items() if row["arbitrage"]] == ["311"]
    assert overround["311"]["overround"] < 1.0 and overround["311"]["outcomes"] == 3
    assert ctx["constraints"]["violationsCount"] == 1
    assert ctx["balance"] == timeline.client.get_account()["balance"] and ctx["initial_balance"] == 100_000
    assert ctx["leaderboard"]["my_rank"] == timeline.client.get_tournament_leaderboard(DEMO_SLUG)["myRank"]


def test_settled_market_is_not_tracked(timeline: SimpleNamespace) -> None:
    rows = timeline.view_end["exchanges"]
    ids = {r["exchange_id"] for r in rows}
    assert len(rows) == 32 and "9023" not in ids and "9034" not in ids  # the Maine debate settled at T0 + 40 min


def test_news_queries_came_from_market_titles(timeline: SimpleNamespace) -> None:
    queries = " ".join(timeline.provider.queries).lower()
    assert "pennsylvania" in queries and "ohio" in queries and "michigan" in queries


# --------------------------------------------------------------------------- DemoNewsProvider


PA_TITLES = [
    "What the withdrawal means for Republicans and the Pennsylvania Senate map",
    "Forecasters move the Pennsylvania Senate toward Republicans after candidate exit",
    "Pennsylvania Senate: surprise withdrawal leaves Republicans favored to keep majority",
    "Republicans' hold on the Pennsylvania Senate firms up as Democratic recruit quits key race",
]


def test_news_provider_matches_the_scripted_story(market: DemoMarket) -> None:
    provider = DemoNewsProvider(market)
    assert isinstance(provider, NewsProvider) and provider.name == "demo-news"
    articles = provider.search('"Pennsylvania" Senate Republicans', None, None)
    assert [a.title for a in articles] == PA_TITLES  # newest first
    published = [a.published_at for a in articles]
    assert published == sorted(published, reverse=True)
    assert published[-1] == T0 - 55 * MIN and published[0] == T0 - 31 * MIN
    for a in articles:
        assert isinstance(a, Article) and a.provider == "demo-news" and a.source
        assert a.url.startswith("https://example.com/news/") and a.summary
    assert provider.queries == ['"Pennsylvania" Senate Republicans']


def test_news_provider_window_limit_and_copies(market: DemoMarket) -> None:
    provider = DemoNewsProvider(market)
    assert [a.published_at for a in provider.search("pennsylvania", T0 - 50 * MIN, None)] == [T0 - 31 * MIN, T0 - 46 * MIN]
    assert [a.published_at for a in provider.search("pennsylvania", None, T0 - 50 * MIN)] == [T0 - 52 * MIN, T0 - 55 * MIN]
    assert len(provider.search("pennsylvania", None, None, limit=2)) == 2
    assert provider.search("pennsylvania", None, None, limit=0) == []
    assert provider.search("pennsylvania", None, None, limit=-3) == []
    first = provider.search("pennsylvania", None, None)
    first[0].title = "mutated"
    assert provider.search("pennsylvania", None, None)[0].title == PA_TITLES[0]  # callers get copies


def test_news_provider_never_publishes_the_future() -> None:
    offset = Offset()
    provider = DemoNewsProvider(make_market(offset=offset))
    offset.now = -40 * MIN  # the demo clock runs relative to its value at construction
    assert [a.published_at for a in provider.search("pennsylvania", None, None)] == [T0 - 46 * MIN, T0 - 52 * MIN, T0 - 55 * MIN]
    offset.now = 0.0
    assert len(provider.search("pennsylvania", None, None)) == 4


def test_news_provider_noise_for_unrelated_queries(market: DemoMarket) -> None:
    provider = DemoNewsProvider(market)
    for query in ("Ohio House District 9", "", "zzz"):
        articles = provider.search(query, None, None)
        assert len(articles) == 3, query
        assert not any("Pennsylvania" in a.title for a in articles)
        assert all(a.published_at <= T0 for a in articles)
    georgia = provider.search("Georgia Senate", None, None)
    assert [a.title for a in georgia] == ["Georgia Senate race: Democrats outraise rivals in third quarter"]


@pytest.mark.parametrize("args", [(None, None, None), ("pennsylvania", "soon", None), ("pennsylvania", None, None, "many"), (42, None, None)])
def test_news_provider_never_raises(market: DemoMarket, args: Tuple[Any, ...]) -> None:
    out = DemoNewsProvider(market).search(*args)
    assert isinstance(out, list)
    DemoNewsProvider(market).close()


def test_news_searcher_ranks_demo_headlines(market: DemoMarket) -> None:
    searcher = NewsSearcher([DemoNewsProvider(market)], clock=market.now)
    pa = searcher.search_for_market("Will Republicans keep control of the Pennsylvania Senate?", "YES", T0 - 6 * HOUR, T0)
    assert pa and all("Pennsylvania" in a.title for a in pa)
    assert pa[0].relevance >= 0.5
    ohio = searcher.search_for_market("Will Republicans win Ohio House District 9?", "YES", T0 - 6 * HOUR, T0)
    assert all(a.relevance < 0.5 for a in ohio)  # only unrelated noise


# --------------------------------------------------------------------------- paper-trading scenarios (g)-(l), §7.6


PAPER_TITLES = {
    "316": "Will the Democratic Party win the Nevada Governor?",
    "317": "Will the Republican Party win the Nevada Governor?",
    "318": "Will the Democratic Party win the Texas Senate?",
    "319": "Will the Republican Party win the Texas Senate?",
    "320": "Will the Democratic Party win the Iowa Senate?",
    "321": "Will the Republican Party win the Iowa Senate?",
    "322": "Will the Democratic Party win the Nebraska Senate?",
    "323": "Will the Republican Party win the Nebraska Senate?",
    "324": "Will the Independent Party win the Nebraska Senate?",
    "325": "Will the Republican Party win the Wyoming Governor?",
    "326": "Will the Maine Senate candidates debate before October 7?",
}
PAPER_RACES = {
    "9024": ("2026:GOVERNOR:NV", "D"), "9025": ("2026:GOVERNOR:NV", "R"), "9026": ("2026:SENATE:TX", "D"),
    "9027": ("2026:SENATE:TX", "R"), "9028": ("2026:SENATE:IA", "D"), "9029": ("2026:SENATE:IA", "R"),
    "9030": ("2026:SENATE:NE", "D"), "9031": ("2026:SENATE:NE", "R"), "9032": ("2026:SENATE:NE", "I"),
    "9033": ("2026:GOVERNOR:WY", "R"),
}
SCRIPTED_FEED = {  # §7.6: the demo's outside fair value of each scripted outcome at T0
    "9024": 0.50, "9025": 0.50, "9026": 0.58, "9027": 0.42, "9028": 0.46, "9029": 0.54, "9030": 0.02, "9031": 0.67,
    "9032": 0.31, "9033": 0.94, "9034": 0.99,
}
LEADING = ("9003", "9004", "9005", "9006")


@pytest.fixture
def paper_demo() -> Iterator[SimpleNamespace]:
    """A demo market whose clock the test moves (``off.now`` seconds after T0), with a client on it."""
    off = Offset()
    market = make_market(offset=off)
    client = make_client(market)
    try:
        yield SimpleNamespace(market=market, client=client, off=off)
    finally:
        client.close()


def _quotes(client: SuperMarketClient, ids: List[str]) -> Tuple[Dict[str, Tuple[float, float]], List[str]]:
    bulk = client.get_prices(ids, tournament_id=TID)
    return {r["exchangeId"]: (r["bestBid"], r["bestAsk"]) for r in bulk["data"]}, list(bulk["missingIds"])


def _at(demo: SimpleNamespace, minutes: float, ids: List[str]) -> Dict[str, Tuple[float, float]]:
    demo.off.now = minutes * MIN
    return _quotes(demo.client, ids)[0]


def test_published_paper_constants() -> None:
    from supermarket_bot.models import PaperConfig, default_portfolios
    from supermarket_bot.paper import headline_id

    assert demo_mod.PAPER_MARKET_IDS == tuple(str(316 + i) for i in range(11))
    assert demo_mod.PAPER_EXCHANGE_IDS == tuple(str(9024 + i) for i in range(11))
    assert demo_mod.DEMO_LEADER_VALUES == (285_000.0, 242_000.0, 221_500.0)
    assert demo_mod.DEMO_BAR == 221_500.0 and demo_mod.DEMO_CHASER_M == pytest.approx(2.215)
    assert [name for name, _ in demo_mod.DEMO_TOP_MEMBERS] == ["election_whale", "polls_gambler", "longshot_lucy"]
    ids = (
        "human:conservative", "policy:conservative", "policy:chaser", "kind:value", "kind:basket", "kind:hole",
        "kind:fade", "kind:carry", "kind:arbitrage",
    )
    assert demo_mod.DEMO_PORTFOLIO_IDS == ids
    assert tuple(p.portfolio_id for p in default_portfolios("conservative")) == ids
    assert headline_id(PaperConfig(sizing="conservative", portfolios=default_portfolios("conservative"))) == demo_mod.DEMO_HEADLINE
    assert demo_mod.DEMO_HEADLINE == "human:conservative"
    assert demo_mod.SIM_T0 == epoch("2026-10-05T14:00:00Z")


def test_paper_markets_use_the_real_cup_grammar(api: SuperMarketClient) -> None:
    from supermarket_bot.fairvalue import parse_race

    markets = {m["id"]: m for m in api.list_markets(tournament_id=TID, status="any")["data"]}
    for mid, title in PAPER_TITLES.items():
        assert markets[mid]["title"] == title
        assert not markets[mid]["isMultiOutcome"]
    exchanges = {ex["id"]: (m["title"], ex.get("option")) for m in markets.values() for ex in m["exchanges"]}
    assert [markets[mid]["exchanges"][0]["id"] for mid in PAPER_TITLES] == list(demo_mod.PAPER_EXCHANGE_IDS)
    for eid, (race_key, party) in PAPER_RACES.items():
        ref = parse_race(exchanges[eid][0], exchanges[eid][1])
        assert ref is not None and (ref.race_key, ref.party) == (race_key, party), eid
    assert parse_race(exchanges["9034"][0], exchanges["9034"][1]) is None  # a debate market, not a race
    assert markets["326"]["settlementDate"] == iso(T0 + 40 * MIN).replace("Z", ".000Z")
    assert all(markets[mid]["settlementDate"] == CUP_END for mid in PAPER_TITLES if mid != "326")


def test_paper_scenario_references(market: DemoMarket) -> None:
    sc = market.scenario
    assert sc["basket_pair"]["exchange_ids"] == ["9024", "9025"]
    assert sc["value_winner"]["exchange_id"] == "9026" and sc["value_winner"]["pair_exchange_id"] == "9027"
    assert sc["value_loser"]["exchange_id"] == "9028" and sc["value_loser"]["drop_at"] == T0 + 90 * MIN
    assert sc["basket_triple"]["exchange_ids"] == ["9030", "9031", "9032"]
    assert sc["liquidity_hole"]["exchange_id"] == "9033" and sc["liquidity_hole"]["start"] == T0 + 20 * MIN
    assert sc["settles_live"]["exchange_id"] == "9034" and sc["settles_live"]["settles_at"] == T0 + 40 * MIN
    assert sc["leading_feed"] == {"exchange_ids": list(LEADING), "lead_s": 900.0}


def test_scenario_g_nevada_no_basket(paper_demo: SimpleNamespace) -> None:
    pair = ["9024", "9025"]
    before = _at(paper_demo, 1, pair)
    assert sum(b for b, _ in before.values()) <= 1.0 + 1e-9  # outside the window the pair adds up
    for minutes in (5, 12, 20, 29):
        q = _at(paper_demo, minutes, pair)
        assert 1.04 - 1e-9 <= sum(b for b, _ in q.values()) <= 1.06 + 1e-9, minutes  # YES bids above 1: buy both NOs
    for minutes in (45, 60, 120):
        q = _at(paper_demo, minutes, pair)
        assert sum(a for _, a in q.values()) <= 1.01 + 1e-9, minutes  # the NO basket can be sold back at a profit


def test_scenario_h_texas_value_winner(paper_demo: SimpleNamespace) -> None:
    mids = {}
    for minutes in (10, 60, 120, 180):
        q = _at(paper_demo, minutes, ["9026", "9027"])
        mids[minutes] = (q["9026"][0] + q["9026"][1]) / 2
        assert (q["9026"][0] + q["9026"][1]) / 2 + (q["9027"][0] + q["9027"][1]) / 2 == pytest.approx(1.0, abs=0.011)
    assert mids[10] == pytest.approx(0.50, abs=0.006) and mids[180] == pytest.approx(0.575, abs=0.006)
    assert mids[10] < mids[60] < mids[120] < mids[180]  # converges toward the outside 0.58
    feed = paper_demo.market.fair_value_feed
    for ts in (T0 - 2 * HOUR + 60, T0, T0 + 3 * HOUR):
        assert feed("9026", ts) == pytest.approx(0.58) and feed("9027", ts) == pytest.approx(0.42)


def test_scenario_i_iowa_value_loser(paper_demo: SimpleNamespace) -> None:
    q5, q89 = _at(paper_demo, 5, ["9028"]), _at(paper_demo, 89, ["9028"])
    assert sum(q5["9028"]) / 2 == pytest.approx(0.38, abs=0.006)
    assert sum(q89["9028"]) / 2 == pytest.approx(0.33, abs=0.006)  # drifted away from the outside price
    feed = paper_demo.market.fair_value_feed
    assert feed("9028", T0) == pytest.approx(0.46) and feed("9028", T0 + 89 * MIN) == pytest.approx(0.46)
    assert feed("9028", T0 + 91 * MIN) == pytest.approx(0.30)  # the outside price was wrong: it drops below the bid
    assert feed("9029", T0) == pytest.approx(0.54) and feed("9029", T0 + 91 * MIN) == pytest.approx(0.70)


def test_scenario_j_nebraska_three_leg_no_basket(paper_demo: SimpleNamespace) -> None:
    triple = ["9030", "9031", "9032"]
    for minutes in (30, 59, 81):
        assert sum(b for b, _ in _at(paper_demo, minutes, triple).values()) < 1.0
    for minutes in (61, 70, 79):
        assert sum(b for b, _ in _at(paper_demo, minutes, triple).values()) >= 1.03 - 1e-9, minutes


def test_scenario_k_wyoming_liquidity_hole(paper_demo: SimpleNamespace) -> None:
    before = _at(paper_demo, 19, ["9033"])["9033"]
    assert before == pytest.approx((0.925, 0.935))
    paper_demo.off.now = 21 * MIN
    book = paper_demo.client.get_exchange_orderbook("9033", depth=5, tournament_id=TID)
    assert book["bids"][0]["price"] <= 0.65  # the hole: the best bid is far below the 0.93 mid
    paper_demo.off.now = 30 * MIN
    tape = paper_demo.client.get_trades("9033", tournament_id=TID, start=iso(T0 + 20 * MIN), end=iso(T0 + 20 * MIN + 21),
                                        limit=200)["data"]
    hole = [t for t in tape if t["side"] == "NO" and t["size"] == 100]
    assert len(hole) == 12
    prices = [t["price"] for t in sorted(hole, key=lambda t: t["createdAt"])]
    assert prices[0] == pytest.approx(0.925) and min(prices) == pytest.approx(0.62)
    assert prices == sorted(prices, reverse=True)  # they walk down the bids
    after = _at(paper_demo, 27, ["9033"])["9033"]
    assert sum(after) / 2 == pytest.approx(0.93, abs=0.011)  # recovered by T0 + 26 min


def test_scenario_l_maine_debate_settles_live(paper_demo: SimpleNamespace) -> None:
    early = _at(paper_demo, 10, ["9034"])["9034"]
    late = _at(paper_demo, 39, ["9034"])["9034"]
    assert sum(early) / 2 == pytest.approx(0.85, abs=0.011) and sum(late) / 2 >= 0.94
    paper_demo.off.now = 41 * MIN
    quotes, missing = _quotes(paper_demo.client, ["9034", "9033"])
    assert "9034" in missing and "9034" not in quotes and "9033" in quotes
    settled = {m["id"]: m for m in paper_demo.client.list_tournament_markets(DEMO_SLUG, status="settled", limit=100)["data"]}
    assert settled["326"]["status"] == "settled" and settled["326"]["settledWith"] == "YES"
    assert epoch(settled["326"]["settledOn"]) == T0 + 40 * MIN
    open_ids = {m["id"] for m in paper_demo.client.list_tournament_markets(DEMO_SLUG, status="open", limit=100)["data"]}
    assert "326" not in open_ids and "325" in open_ids
    paper_demo.off.now = 39 * MIN
    open_ids = {m["id"] for m in paper_demo.client.list_tournament_markets(DEMO_SLUG, status="open", limit=100)["data"]}
    assert "326" in open_ids


def test_demo_fair_value_provider(paper_demo: SimpleNamespace) -> None:
    provider = demo_mod.DemoFairValueProvider(paper_demo.market)
    assert provider.name == "demo"
    targets = [str(9001 + i) for i in range(34)]
    result = provider.refresh(targets, T0)
    assert (result.venue, result.status, result.requests) == ("demo", "ok", 0)
    assert set(result.quotes) == set(SCRIPTED_FEED) | set(LEADING)  # nothing else has an outside price
    for eid, value in SCRIPTED_FEED.items():
        q = result.quotes[eid]
        assert q.last == pytest.approx(value) and q.bid == pytest.approx(value - 0.005) and q.ask == pytest.approx(value + 0.005)
        assert (q.venue, q.external_id, q.match_kind, q.match_confidence, q.fetched_at) == ("demo", f"demo:{eid}", "EXACT", 1.0, T0)
    for now in (T0, T0 + 30 * MIN, T0 + 91 * MIN):
        quotes = provider.refresh(targets, now).quotes
        for eid in LEADING:  # the outside price LEADS the Cup: it is the Cup's own mid 15 minutes later (D37)
            assert quotes[eid].last == pytest.approx(paper_demo.market.mid_at(eid, now + 900))
    again = demo_mod.DemoFairValueProvider(make_market()).refresh(targets, T0 + 91 * MIN)
    assert {k: v.to_dict() for k, v in again.quotes.items()} == {
        k: v.to_dict() for k, v in provider.refresh(targets, T0 + 91 * MIN).quotes.items()}  # deterministic
    assert provider.refresh(targets, T0 + 91 * MIN).quotes["9028"].last == pytest.approx(0.30)
    provider.close()


def test_leaderboard_bar_for_the_chaser(api: SuperMarketClient) -> None:
    from supermarket_bot.tracker import leaderboard_value

    board = api.get_tournament_leaderboard(DEMO_SLUG, limit=100)
    top = board["leaderboard"][:3]
    assert [(r["rank"], r["username"], r["pnl"]) for r in top] == [
        (1, "election_whale", 185_000.0), (2, "polls_gambler", 142_000.0), (3, "longshot_lucy", 121_500.0)]
    values = tuple(leaderboard_value(r, 100_000.0) for r in top)
    assert values == demo_mod.DEMO_LEADER_VALUES
    assert values[2] / 100_000.0 == pytest.approx(demo_mod.DEMO_CHASER_M)
    try:
        from supermarket_bot.models import LeaderboardSnapshot
        from supermarket_bot.sizing import estimate_bar

        snap = LeaderboardSnapshot(at=T0, entries=[{"rank": r["rank"], "username": r["username"], "pnl": r["pnl"],
                                                    "value": leaderboard_value(r, 100_000.0)} for r in board["leaderboard"]],
                                   initial_balance=100_000.0)
        bar = estimate_bar(snap, [], 100_000.0, 33.0)
    except NotImplementedError:  # package B not merged yet: the published constants above are what F relies on
        return
    assert bar.third_value == pytest.approx(demo_mod.DEMO_BAR) and bar.value == pytest.approx(demo_mod.DEMO_BAR)


def test_sim_clock_and_no_wait() -> None:
    import threading

    from supermarket_bot.ratelimit import SlidingWindowLimiter

    clock = demo_mod.SimClock(1000.0)
    assert clock() == 1000.0 and clock.now == 1000.0
    assert clock.advance(30) == 1030.0 and clock() == 1030.0
    clock.set(5000.0)
    assert clock() == 5000.0
    workers = [threading.Thread(target=lambda: [clock.advance(1) for _ in range(1000)]) for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)
    assert clock() == 9000.0  # advance is atomic
    demo_mod.no_wait(0.0)
    demo_mod.no_wait(-1.0)
    with pytest.raises(demo_mod.SimClockStall) as caught:
        demo_mod.no_wait(1.2)
    assert str(caught.value) == "A fast simulation tried to wait 1.2 s for a rate limiter: raise its demo budget"
    limiter = SlidingWindowLimiter(2, clock=clock, sleep=demo_mod.no_wait)
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(demo_mod.SimClockStall):
        limiter.acquire()  # a third read inside the simulated minute would have to wait: loud, deterministic
    clock.advance(61)
    limiter.acquire()


# --------------------------------------------------------------------------- outside-move scenarios (OUTSIDE_MOVES.md §15)

MOVES_TITLES = {
    "327": "Will the Democratic Party win the New Hampshire Senate?",
    "328": "Will the Republican Party win the Ohio Senate?",
    "329": "Will the Democratic Party win the Wisconsin Governor?",
    "330": "Will the Republican Party win the Georgia Governor?",
    "331": "Will the Democratic Party win the Minnesota Senate?",
    "332": "Will the Republican Party win the Pennsylvania Governor?",
    "333": "Will the Democratic Party win the Michigan Governor?",
}
MOVES_RACES = {
    "9035": ("2026:SENATE:NH", "D"), "9036": ("2026:SENATE:OH", "R"), "9037": ("2026:GOVERNOR:WI", "D"),
    "9038": ("2026:GOVERNOR:GA", "R"), "9039": ("2026:SENATE:MN", "D"), "9040": ("2026:GOVERNOR:PA", "R"),
    "9041": ("2026:GOVERNOR:MI", "D"),
}
MOVES_BASE = {"9035": 0.56, "9036": 0.58, "9037": 0.52, "9038": 0.62, "9039": 0.60, "9040": 0.40, "9041": 0.50}
MOVES_SPREAD = {"9035": 0.01, "9036": 0.02, "9037": 0.02, "9038": 0.02, "9039": 0.01, "9040": 0.02, "9041": 0.01}


def moves_market(offset: Optional[Offset] = None, seed: int = 7) -> DemoMarket:
    return DemoMarket(seed=seed, now=T0, clock=offset if offset is not None else (lambda: 0.0), outside_moves=True)


def outside_value(market: DemoMarket, eid: str, venue: str, t: float) -> Optional[float]:
    quote = market.outside_quote(eid, venue, T0 + t)
    return None if quote is None else round((quote[0] + quote[1]) / 2, 4)


def test_published_outside_move_constants() -> None:
    assert demo_mod.MOVES_DEMO_VENUES == ("demo-a", "demo-b") and demo_mod.MOVES_DEMO_CYCLE_S == 3600.0
    assert demo_mod.MOVES_DEMO_MARKET_IDS == tuple(MOVES_TITLES) and demo_mod.MOVES_DEMO_EXCHANGE_IDS == tuple(MOVES_RACES)
    assert demo_mod.MOVES_DEMO_SCENARIOS == {"lead_short": "9035", "lead_long": "9036", "never": "9037", "thin_spike": "9038",
                                             "cup_first": "9039", "reverts": "9040", "disagree": "9041"}
    assert [m[1] for m in demo_mod.MOVES_DEMO_MARKETS] == list(MOVES_TITLES.values())
    assert demo_mod.MOVES_DEMO_LEAD_SHORT_S == (180.0, 60.0, 300.0, 120.0)
    assert demo_mod.MOVES_DEMO_LEAD_LONG_S == (480.0, 720.0, 360.0, 600.0)


def test_the_default_demo_has_no_outside_move_markets_and_is_unchanged() -> None:
    plain, moves = make_market(), moves_market()
    assert len(plain.exchanges) == 34 and len(plain.markets) == 26 and "outside_moves" not in plain.scenario
    assert len(moves.exchanges) == 41 and len(moves.markets) == 33
    assert plain.outside_quote("9035", "demo-a", T0) is None and plain.outside_quote("9001", "demo-a", T0) is None
    # every existing outcome prices, books and trades exactly as in the default demo
    for ex in plain.exchanges:
        for t in (-2 * HOUR, 0.0, 95.0, 20 * MIN, 2 * HOUR):
            assert plain._quote(ex, T0 + t) == moves._quote(moves._exchange_by_id[ex.id], T0 + t), (ex.id, t)
        assert plain._block(ex, int(T0 // HOUR)) == moves._block(moves._exchange_by_id[ex.id], int(T0 // HOUR))
    sc = dict(moves.scenario)
    assert sc.pop("outside_moves") == {"exchange_ids": demo_mod.MOVES_DEMO_SCENARIOS, "cycle_s": 3600.0,
                                       "venues": ["demo-a", "demo-b"]}
    assert sc == plain.scenario


def test_outside_move_markets_use_the_cup_grammar_one_leg_per_race() -> None:
    from supermarket_bot.fairvalue import parse_race

    market = moves_market()
    client = make_client(market)
    try:
        rows = {m["id"]: m for m in client.list_markets(tournament_id=TID, status="open")["data"]}
        for mid, title in MOVES_TITLES.items():
            assert rows[mid]["title"] == title and not rows[mid]["isMultiOutcome"]
            assert rows[mid]["settlementDate"] == CUP_END
        exchanges = {rows[mid]["exchanges"][0]["id"]: rows[mid]["title"] for mid in MOVES_TITLES}
        assert list(exchanges) == list(MOVES_RACES)
        keys = []
        for eid, title in exchanges.items():
            ref = parse_race(title)
            assert ref is not None and (ref.race_key, ref.party) == MOVES_RACES[eid]
            keys.append(ref.race_key)
        other = {parse_race(m["title"]).race_key for mid, m in rows.items() if mid not in MOVES_TITLES and parse_race(m["title"])}
        assert len(set(keys)) == 7 and not set(keys) & other  # own races: no basket, no pair
        quotes, missing = _quotes(client, list(MOVES_RACES))
        assert missing == [] and all(on_tick(b) and on_tick(a) for b, a in quotes.values())
        assert {eid: round(a - b, 3) for eid, (b, a) in quotes.items()} == MOVES_SPREAD  # liquid 0.01, medium 0.02
        for eid in MOVES_RACES:
            assert market.fair_value_feed(eid, T0) is None  # no fair value: no value ideas, no paper trade (M12)
    finally:
        client.close()


def test_outside_quotes_follow_the_script_on_both_venues() -> None:
    m = moves_market()
    # lead_short: +0.06 over [c+60, c+75] on both venues; back down in cycle 1 (the direction flips)
    assert outside_value(m, "9035", "demo-a", 59) == 0.56 and outside_value(m, "9035", "demo-a", 75) == 0.62
    assert outside_value(m, "9035", "demo-a", 67.5) == pytest.approx(0.59, abs=1e-3)
    assert outside_value(m, "9035", "demo-a", HOUR + 59) == 0.62 and outside_value(m, "9035", "demo-a", HOUR + 75) == 0.56
    assert outside_value(m, "9035", "demo-a", 2 * HOUR + 80) == 0.62
    # lead_long: down 0.06 over [c+150, c+180]
    assert outside_value(m, "9036", "demo-a", 150) == 0.58 and outside_value(m, "9036", "demo-a", 180) == 0.52
    assert outside_value(m, "9036", "demo-a", HOUR + 180) == 0.58
    # never: +0.07 at c+240..270 in even cycles, back at c+900..930 in odd cycles
    assert outside_value(m, "9037", "demo-a", 270) == 0.59 and outside_value(m, "9037", "demo-a", HOUR + 899) == 0.59
    assert outside_value(m, "9037", "demo-a", HOUR + 930) == 0.52 and outside_value(m, "9037", "demo-a", 2 * HOUR + 270) == 0.59
    # cup_first: the outside moves at c+660..690; reverts: up at c+540..570, back at c+780..840
    assert outside_value(m, "9039", "demo-a", 659) == 0.60 and outside_value(m, "9039", "demo-a", 690) == 0.66
    assert [outside_value(m, "9040", "demo-a", t) for t in (539, 570, 780, 810, 840)] == [0.40, 0.46, 0.46, 0.43, 0.40]
    assert outside_value(m, "9040", "demo-a", HOUR + 570) == 0.34  # cycle 1: down, then back
    # spikes only on venue A: the thin single print and the wide, thin book (9038); the lone mover (9041)
    a, b = m.outside_quote("9038", "demo-a", T0 + 120), m.outside_quote("9038", "demo-b", T0 + 120)
    assert a == (0.7, 0.72, 500.0, 500.0) and outside_value(m, "9038", "demo-b", 120) in (0.615, 0.62, 0.625)
    assert m.outside_quote("9038", "demo-a", T0 + 134)[:2] == (0.7, 0.72)
    assert m.outside_quote("9038", "demo-a", T0 + 135)[:2] == (0.61, 0.63)  # gone before the next 15-s poll
    assert m.outside_quote("9038", "demo-a", T0 + 500) == (0.64, 0.72, 20.0, 20.0)  # 8 pts wide, 20 at the touch
    assert m.outside_quote("9038", "demo-a", T0 + 601) == (0.61, 0.63, 500.0, 500.0)
    assert b[2:] == (500.0, 500.0) and m.outside_quote("9038", "demo-b", T0 + 500)[2:] == (500.0, 500.0)
    assert outside_value(m, "9041", "demo-a", 315) == 0.55 and outside_value(m, "9041", "demo-a", 915) == 0.50
    assert outside_value(m, "9041", "demo-b", 315) in (0.495, 0.5, 0.505)
    assert outside_value(m, "9041", "demo-a", HOUR + 315) == 0.45  # cycle 1: the other way
    # nothing for other outcomes, other venues, or before the history start; nothing scripted before t0
    assert m.outside_quote("9001", "demo-a", T0) is None and m.outside_quote("9035", "polymarket", T0) is None
    assert m.outside_quote("9035", "demo-a", m.history_start - 1) is None
    assert outside_value(m, "9035", "demo-a", -HOUR + 75) == 0.56


def test_venue_b_jitters_a_quarter_of_its_15_second_buckets() -> None:
    m = moves_market()
    diffs = []
    for i in range(400):
        a = m.outside_quote("9037", "demo-a", T0 - 2 * HOUR + 15 * i)
        b = m.outside_quote("9037", "demo-b", T0 - 2 * HOUR + 15 * i)
        assert round(a[1] - a[0], 3) == round(b[1] - b[0], 3) == 0.02  # the same spread
        diffs.append(round(b[0] - a[0], 3))
        assert all(on_tick(p) or abs(p * 1000 - round(p * 1000)) < 1e-6 for p in a[:2] + b[:2])
    assert set(diffs) <= {-0.005, 0.0, 0.005}
    share = sum(1 for d in diffs if d) / len(diffs)
    assert 0.15 < share < 0.35
    # one 15-s bucket, one jitter: two times inside the same bucket give the same quote
    bucket = math.floor((T0 + 1000) / 15.0) * 15.0
    assert m.outside_quote("9037", "demo-b", bucket) == m.outside_quote("9037", "demo-b", bucket + 14.9)


def test_cup_mids_follow_the_scripted_cup_moves() -> None:
    m = moves_market()

    def mid(eid: str, t: float) -> float:
        return m.mid_at(eid, T0 + t)

    # lead_short: the Cup follows 180 s after the outside move in cycle 0 (60 s in cycle 1), +0.045 over 120 s
    assert mid("9035", 239) == pytest.approx(mid("9035", 0), abs=0.005)
    assert round(mid("9035", 360) - mid("9035", 239), 3) == 0.045
    assert round(mid("9035", HOUR + 240) - mid("9035", HOUR + 119), 3) == -0.045
    # lead_long: down 0.045 over [c+630, c+750] in cycle 0 (L = 480), back up over [c+870, c+990] in cycle 1 (L = 720)
    assert round(mid("9036", 750) - mid("9036", 629), 3) == -0.045
    assert round(mid("9036", HOUR + 990) - mid("9036", HOUR + 869), 3) == 0.045
    # cup_first: the Cup moves at c+360..480, before the outside (c+660)
    assert round(mid("9039", 480) - mid("9039", 359), 3) == 0.045
    # the other scenarios' Cup prices only carry the (small) noise
    for eid in ("9037", "9038", "9040", "9041"):
        assert all(abs(mid(eid, t) - MOVES_BASE[eid]) <= 0.01 for t in range(0, int(2 * HOUR), 300)), eid


def test_no_new_surge_from_the_scripted_cup_moves() -> None:
    """Every scripted Cup move is 0.045 over >= 120 s and the noise is held around it: no 5-minute change reaches the
    surge detector's 0.05 and no hour its 0.08 (the Surges view stays unchanged), over 3 hours of 30-s snapshots."""
    from supermarket_bot import analytics
    from supermarket_bot.models import PricePoint

    for seed in (7, 3):
        m = moves_market(seed=seed)
        for eid in MOVES_RACES:
            ex = m._exchange_by_id[eid]
            pts = []
            for i in range(-24 * 120, 3 * 120 + 1):
                ts = T0 + 30.0 * i
                q = m._quote(ex, ts)
                pts.append(PricePoint(ts=ts, price=q[0], last=q[0], bid=q[1], ask=q[2]))
            mids = [p.price for p in pts]
            live = len(mids) - 3 * 120 - 1
            for i in range(live, len(mids)):
                assert max(abs(mids[i] - mids[j]) for j in range(i - 11, i)) <= 0.045 + 1e-9, (seed, eid, i)
                assert max(abs(mids[i] - mids[j]) for j in range(i - 121, i)) < 0.08, (seed, eid, i)
            for i in range(live, len(pts), 4):
                found = analytics.detect_surges(pts[: i + 1], pts[i].ts, eid, ex.market.id)
                assert found == [], (seed, eid, found)


def test_the_demo_outside_venue_polls_the_scripted_quotes() -> None:
    from supermarket_bot.fairvalue import MatchTarget

    m = moves_market()
    venue = demo_mod.DemoOutsideVenue(m, "demo-a")
    targets = [MatchTarget(exchange_id=eid, market_id=mid, title=MOVES_TITLES[mid])
               for mid, eid in zip(MOVES_TITLES, MOVES_RACES)] + [MatchTarget("9001", "301", "Other")]
    result = venue.poll(targets, T0 + 75.0, deadline=T0 + 85.0)
    assert (result.venue, result.status, result.requests, result.errors) == ("demo-a", "ok", 0, [])
    assert set(result.quotes) == set(MOVES_RACES)  # only the scripted outcomes
    q = result.quotes["9035"]
    assert (q.venue, q.external_id, q.label) == ("demo-a", "demo-a:9035",
                                                 "Demo venue A: Will the Democratic Party win the New Hampshire Senate?")
    assert (q.bid, q.ask, q.last, q.mid, q.spread) == (0.615, 0.625, None, 0.62, 0.01)
    assert (q.bid_size, q.ask_size, q.fetched_at, q.match_kind, q.match_confidence) == (500.0, 500.0, T0 + 75.0, "EXACT", 1.0)
    assert venue.budget() == {"used": 0, "limit": 0} and venue.close() is None
    b = demo_mod.DemoOutsideVenue(m, "demo-b").poll(targets, T0 + 75.0)
    assert b.quotes["9035"].label.startswith("Demo venue B: ") and b.quotes["9035"].external_id == "demo-b:9035"
    assert venue.poll(["9036", "9001"], T0).quotes.keys() == {"9036"}  # bare exchange ids work too
    # deterministic: a second market with the same seed and start answers the same
    again = demo_mod.DemoOutsideVenue(moves_market(), "demo-a").poll(targets, T0 + 75.0)
    assert {k: v.to_dict() for k, v in again.quotes.items()} == {k: v.to_dict() for k, v in result.quotes.items()}
    # never raises
    broken = demo_mod.DemoOutsideVenue(SimpleNamespace(outside_quote=lambda *a: 1 / 0), "demo-a")
    failed = broken.poll(targets, T0)
    assert failed.status == "error" and failed.quotes == {} and failed.errors


def test_the_outside_move_demo_is_deterministic() -> None:
    a, b = moves_market(), moves_market()
    times = [T0 + 15 * i for i in range(0, 480, 7)]
    for eid in MOVES_RACES:
        assert [a.outside_quote(eid, v, t) for v in ("demo-a", "demo-b") for t in times] == \
               [b.outside_quote(eid, v, t) for v in ("demo-a", "demo-b") for t in times]
        assert [a.mid_at(eid, t) for t in times] == [b.mid_at(eid, t) for t in times]
