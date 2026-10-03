"""Tests for supermarket_bot.client.SuperMarketClient (HTTP plumbing, retries, pagination, endpoints).

Everything runs against the in-memory ``FakeAPI`` from conftest; sleeps are recorded by the
``sleeper`` fixture (which advances the fake clock) instead of really sleeping.
"""

from __future__ import annotations

import email.utils
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import httpx
import pytest

from conftest import API_KEY, BASE_URL, TOURNAMENT_ID, TOURNAMENT_SLUG, error_body, price
from supermarket_bot import __version__
from supermarket_bot import client as client_mod
from supermarket_bot.client import SuperMarketClient, _clean_params, _seg, parse_retry_after
from supermarket_bot.config import Settings
from supermarket_bot.errors import ApiError, NetworkError

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "supermarket-openapi.json"
EMPTY_PAGE: Dict[str, Any] = {"data": [], "pagination": {"limit": 100, "hasMore": False, "nextCursor": None}}


# --------------------------------------------------------------------------- helpers


@pytest.fixture(scope="module")
def spec() -> Dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _resolve(spec: Dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target
    return node


def _spec_query_params(spec: Dict[str, Any], template: str, method: str) -> Dict[str, Dict[str, Any]]:
    item = spec["paths"][template]
    op = item[method.lower()]
    out: Dict[str, Dict[str, Any]] = {}
    for raw in list(op.get("parameters", [])) + list(item.get("parameters", [])):
        prm = _resolve(spec, raw)
        if prm.get("in") == "query":
            out[prm["name"]] = _resolve(spec, prm.get("schema", {}))
    return out


def _check_against_schema(name: str, value: str, schema: Dict[str, Any]) -> None:
    if "enum" in schema:
        assert value in schema["enum"], f"{name}={value!r} not in documented enum {schema['enum']}"
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "integer" in types or "number" in types:
        num = float(value)
        if "integer" in types and "number" not in types:
            assert num == int(num), f"{name}={value!r} must be an integer"
        if "minimum" in schema:
            assert num >= schema["minimum"], f"{name}={value} below documented minimum {schema['minimum']}"
        if "maximum" in schema:
            assert num <= schema["maximum"], f"{name}={value} above documented maximum {schema['maximum']}"
    if "pattern" in schema:
        assert re.fullmatch(schema["pattern"].strip("^$"), value), f"{name}={value!r} violates pattern {schema['pattern']}"


@pytest.fixture
def upper_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make jittered backoff deterministic: always the top of the [ceiling/2, ceiling] range."""
    monkeypatch.setattr(client_mod.random, "uniform", lambda lo, hi: hi)


def _recorder(clock, reply: Any = None) -> Callable[[httpx.Request], httpx.Response]:
    """A reply callable that records the fake-clock time at which each request was sent."""
    times: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        times.append(clock.now)
        return httpx.Response(200, json=reply if reply is not None else {"ok": True})

    handler.times = times  # type: ignore[attr-defined]
    return handler


# --------------------------------------------------------------------------- construction, headers, URLs


def test_auth_user_agent_and_accept_headers(fake, client):
    fake.add("GET", "/account", {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    headers = fake.calls[0].headers
    assert headers["authorization"] == f"Bearer {API_KEY}"
    assert headers["user-agent"] == f"supermarket-bot/{__version__}"
    assert headers["accept"] == "application/json"
    assert "x-api-key" not in headers  # one auth mechanism only


def test_url_joined_under_api_v1(fake, client):
    fake.add("GET", "/markets/26", {"id": "26"})
    client.get_market(26)
    assert str(fake.calls[0].request.url) == "https://example.test/api/v1/markets/26"


@pytest.mark.parametrize("base", ["https://example.test/api/v1/", "https://example.test/api/v1///"])
def test_trailing_slashes_on_base_url_are_normalised(fake, sleeper, clock, base):
    fake.add("GET", "/account", {"id": "u1"})
    with SuperMarketClient(API_KEY, base, transport=httpx.MockTransport(fake), sleep=sleeper, clock=clock) as c:
        assert c.base_url == "https://example.test/api/v1"
        c.get_account()
    assert fake.calls[0].request.url.raw_path == b"/api/v1/account"


def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        SuperMarketClient("", BASE_URL)


def test_from_settings_applies_base_url_and_rate_limits(fake, sleeper, clock):
    settings = Settings(api_key=API_KEY, base_url=BASE_URL, reads_per_min=7, writes_per_min=3)
    with SuperMarketClient.from_settings(settings, transport=httpx.MockTransport(fake), sleep=sleeper, clock=clock) as c:
        assert c.base_url == BASE_URL
        assert c.read_limiter.limit == 7
        assert c.write_limiter.limit == 3


def test_context_manager_closes_http_client(fake, make_client):
    fake.add("GET", "/account", {"id": "u1"})
    with make_client() as c:
        c.get_account()
        assert not c._http.is_closed
    assert c._http.is_closed
    with pytest.raises(RuntimeError):
        c.get_account()


def test_close_is_explicit_and_idempotent(make_client):
    c = make_client()
    c.close()
    c.close()
    assert c._http.is_closed


# --------------------------------------------------------------------------- query params and path segments


def test_clean_params_rules():
    cleaned = _clean_params(
        {
            "none": None,
            "yes": True,
            "no": False,
            "ids": [36, "37", 42],
            "tuple": ("a", "b"),
            "empty_list": [],
            "empty_tuple": (),
            "zero": 0,
            "text": "x",
            "ratio": 0.05,
        }
    )
    assert cleaned == {"yes": "true", "no": "false", "ids": "36,37,42", "tuple": "a,b", "zero": 0, "text": "x", "ratio": 0.05}
    assert _clean_params(None) == {}


def test_query_params_cleaned_on_the_wire(fake, client):
    fake.add("GET", "/x", {})
    client.get("/x", gone=None, flag=True, off=False, ids=[1, 2, 3], nothing=[], zero=0, ratio=0.25)
    call = fake.calls[0]
    assert call.params == {"flag": "true", "off": "false", "ids": "1,2,3", "zero": "0", "ratio": "0.25"}
    # comma-joined lists are one query parameter, not repeated keys
    assert call.request.url.params.get_list("ids") == ["1,2,3"]


@pytest.mark.parametrize(
    "slug, raw",
    [
        ("my cup/2026", b"/api/v1/tournaments/my%20cup%2F2026"),
        ("a?b#c", b"/api/v1/tournaments/a%3Fb%23c"),
        ("  padded  ", b"/api/v1/tournaments/padded"),
        ("50%-off", b"/api/v1/tournaments/50%25-off"),
    ],
)
def test_path_segments_are_quoted(fake, client, slug, raw):
    fake.add("GET", "/tournaments/" + slug.strip(), {"id": TOURNAMENT_ID})
    client.get_tournament(slug)
    assert fake.calls[0].request.url.raw_path == raw


def test_slug_with_slash_does_not_reach_a_different_route(fake, client):
    # "/" must be encoded, so the server sees one segment rather than /tournaments/a/markets
    fake.add("GET", "/tournaments/a/markets", {"data": "wrong route"})
    fake.add("GET", "/tournaments/a%2Fmarkets", {"id": "ok"})
    client.get_tournament("a/markets")
    assert fake.calls[0].request.url.raw_path == b"/api/v1/tournaments/a%2Fmarkets"


@pytest.mark.xfail(strict=True, reason="BUG: _seg leaves '.'/'..' unencoded, so httpx dot-segment removal escapes the path")
@pytest.mark.parametrize("segment", ["..", "."])
def test_dot_segments_cannot_escape_the_path(fake, client, segment):
    # get_tournament_market("..", 26) must not silently turn into the public GET /markets/26
    fake.add("GET", "/markets/26", {"id": "26", "contexts": [{"type": "public"}]})
    fake.add("GET", "/tournaments/markets/26", {"id": "26", "contexts": [{"type": "public"}]})
    try:
        client.get_tournament_market(segment, 26)
    except ValueError:
        return  # rejecting the segment outright is also acceptable
    raw = fake.calls[0].request.url.raw_path
    assert raw.startswith(b"/api/v1/tournaments/") and raw.count(b"/") == 6, raw


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_empty_path_segment_rejected_before_any_request(fake, client, bad):
    with pytest.raises(ValueError):
        client.get_tournament(bad)
    with pytest.raises(ValueError):
        client.get_exchange_orderbook(bad)
    assert fake.calls == []


def test_seg_accepts_integer_ids():
    assert _seg(26) == "26"
    assert _seg("36") == "36"


# --------------------------------------------------------------------------- endpoints vs the OpenAPI spec

SLUG = TOURNAMENT_SLUG
TID = TOURNAMENT_ID
REL_ID = "11111111-2222-3333-4444-555555555555"
T0, T1 = "2026-10-01T00:00:00.000Z", "2026-10-02T00:00:00.000Z"

ENDPOINT_CASES = [
    # (id, call, method, spec template, concrete path, expected query params)
    ("get_account", lambda c: c.get_account(), "GET", "/account", "/account", {}),
    ("list_tournaments", lambda c: c.list_tournaments(status="active", limit=10, offset=20, market_id=26), "GET", "/tournaments", "/tournaments",
     {"status": "active", "limit": "10", "offset": "20", "marketId": "26"}),
    ("list_tournaments_defaults", lambda c: c.list_tournaments(), "GET", "/tournaments", "/tournaments", {"status": "any", "limit": "50", "offset": "0"}),
    ("iter_tournaments", lambda c: list(c.iter_tournaments(status="ended", market_id=26)), "GET", "/tournaments", "/tournaments",
     {"status": "ended", "marketId": "26", "limit": "100", "offset": "0"}),
    ("get_tournament", lambda c: c.get_tournament(SLUG), "GET", "/tournaments/{slug}", f"/tournaments/{SLUG}", {}),
    ("list_tournament_markets", lambda c: c.list_tournament_markets(SLUG, limit=5, cursor="cur", search="senate", status="open", market_group_id="g1"),
     "GET", "/tournaments/{slug}/markets", f"/tournaments/{SLUG}/markets",
     {"limit": "5", "cursor": "cur", "search": "senate", "status": "open", "marketGroupId": "g1"}),
    ("list_tournament_markets_defaults", lambda c: c.list_tournament_markets(SLUG), "GET", "/tournaments/{slug}/markets", f"/tournaments/{SLUG}/markets",
     {"limit": "100", "status": "any"}),
    ("iter_tournament_markets", lambda c: list(c.iter_tournament_markets(SLUG, status="open", search="gov")), "GET", "/tournaments/{slug}/markets",
     f"/tournaments/{SLUG}/markets", {"limit": "100", "status": "open", "search": "gov"}),
    ("get_tournament_market", lambda c: c.get_tournament_market(SLUG, 26), "GET", "/tournaments/{slug}/markets/{id}", f"/tournaments/{SLUG}/markets/26", {}),
    ("get_tournament_leaderboard", lambda c: c.get_tournament_leaderboard(SLUG, period="7d", limit=10, offset=10, group_id="g", season="2026-Q1", sort="roi"),
     "GET", "/tournaments/{slug}/leaderboard", f"/tournaments/{SLUG}/leaderboard",
     {"period": "7d", "limit": "10", "offset": "10", "groupId": "g", "season": "2026-Q1", "sort": "roi"}),
    ("get_tournament_leaderboard_defaults", lambda c: c.get_tournament_leaderboard(SLUG), "GET", "/tournaments/{slug}/leaderboard",
     f"/tournaments/{SLUG}/leaderboard", {"period": "all", "limit": "50", "offset": "0"}),
    ("get_tournament_seasons", lambda c: c.get_tournament_seasons(SLUG), "GET", "/tournaments/{slug}/seasons", f"/tournaments/{SLUG}/seasons", {}),
    ("get_tournament_positions", lambda c: c.get_tournament_positions(SLUG), "GET", "/tournaments/{slug}/portfolio/positions",
     f"/tournaments/{SLUG}/portfolio/positions", {}),
    ("get_tournament_pnl", lambda c: c.get_tournament_pnl(SLUG, period="week"), "GET", "/tournaments/{slug}/portfolio/pnl",
     f"/tournaments/{SLUG}/portfolio/pnl", {"period": "week"}),
    ("get_tournament_pnl_defaults", lambda c: c.get_tournament_pnl(SLUG), "GET", "/tournaments/{slug}/portfolio/pnl",
     f"/tournaments/{SLUG}/portfolio/pnl", {"period": "all"}),
    ("iter_tournament_fills", lambda c: list(c.iter_tournament_fills(SLUG, exchange_id=36, market_id=26)), "GET", "/tournaments/{slug}/portfolio/fills",
     f"/tournaments/{SLUG}/portfolio/fills", {"limit": "200", "exchangeId": "36", "marketId": "26"}),
    ("list_markets", lambda c: c.list_markets(limit=20, cursor="c1", tournament_id=TID, search="x", sort="trending", status="open",
                                              category="election-outcome", ids=[1, 2], is_composite=False, is_multi_outcome=True),
     "GET", "/markets", "/markets",
     {"limit": "20", "cursor": "c1", "tournamentId": TID, "search": "x", "sort": "trending", "status": "open",
      "category": "election-outcome", "ids": "1,2", "is_composite": "false", "is_multi_outcome": "true"}),
    ("list_markets_defaults", lambda c: c.list_markets(), "GET", "/markets", "/markets", {"limit": "100"}),
    ("iter_markets", lambda c: list(c.iter_markets(tournament_id=TID, status="open", is_composite=True, is_multi_outcome=False, ids=["5"])),
     "GET", "/markets", "/markets",
     {"limit": "100", "tournamentId": TID, "status": "open", "is_composite": "true", "is_multi_outcome": "false", "ids": "5"}),
    ("get_market", lambda c: c.get_market(26, tournament_id=TID), "GET", "/markets/{id}", "/markets/26", {"tournamentId": TID}),
    ("get_market_orderbook", lambda c: c.get_market_orderbook(26, tournament_id=TID, depth=200), "GET", "/markets/{id}/orderbook", "/markets/26/orderbook",
     {"tournamentId": TID, "depth": "200"}),
    ("get_market_nodes", lambda c: c.get_market_nodes(26, tournament_id=TID), "GET", "/markets/{id}/nodes", "/markets/26/nodes", {"tournamentId": TID}),
    ("list_exchanges_market", lambda c: c.list_exchanges(market_id=26, limit=100, cursor="c", tournament_id=TID), "GET", "/exchanges", "/exchanges",
     {"marketId": "26", "limit": "100", "cursor": "c", "tournamentId": TID}),
    ("list_exchanges_ids", lambda c: c.list_exchanges(ids=[36, 37]), "GET", "/exchanges", "/exchanges", {"ids": "36,37", "limit": "200"}),
    ("get_exchange_price", lambda c: c.get_exchange_price(36, tournament_id=TID), "GET", "/exchanges/{id}/price", "/exchanges/36/price", {"tournamentId": TID}),
    ("get_exchange_orderbook", lambda c: c.get_exchange_orderbook(36, depth=5, tournament_id=TID), "GET", "/exchanges/{id}/orderbook",
     "/exchanges/36/orderbook", {"depth": "5", "tournamentId": TID}),
    ("get_price_history", lambda c: c.get_price_history(36, tournament_id=TID, resolution="5m", start=T0, end=T1, limit=500), "GET",
     "/exchanges/{id}/price-history", "/exchanges/36/price-history",
     {"tournamentId": TID, "resolution": "5m", "from": T0, "to": T1, "limit": "500"}),
    ("get_price_history_defaults", lambda c: c.get_price_history(36), "GET", "/exchanges/{id}/price-history", "/exchanges/36/price-history",
     {"resolution": "1h"}),
    ("get_trades", lambda c: c.get_trades(36, tournament_id=TID, start=T0, end=T1, limit=25, cursor="c"), "GET", "/exchanges/{id}/trades",
     "/exchanges/36/trades", {"tournamentId": TID, "from": T0, "to": T1, "limit": "25", "cursor": "c"}),
    ("get_trades_defaults", lambda c: c.get_trades(36), "GET", "/exchanges/{id}/trades", "/exchanges/36/trades", {"limit": "50"}),
    ("iter_trades", lambda c: list(c.iter_trades(36, tournament_id=TID, start=T0, end=T1)), "GET", "/exchanges/{id}/trades", "/exchanges/36/trades",
     {"tournamentId": TID, "from": T0, "to": T1, "limit": "200"}),
    ("get_prices", lambda c: c.get_prices([36, 37], tournament_id=TID), "GET", "/exchanges/prices", "/exchanges/prices", {"ids": "36,37", "tournamentId": TID}),
    ("get_leaderboard", lambda c: c.get_leaderboard(tournament_slug=SLUG, group_id="g", period="quarter", sort="volume", limit=10, offset=30), "GET",
     "/leaderboards", "/leaderboards",
     {"tournamentSlug": SLUG, "groupId": "g", "period": "quarter", "sort": "volume", "limit": "10", "offset": "30"}),
    ("get_leaderboard_defaults", lambda c: c.get_leaderboard(), "GET", "/leaderboards", "/leaderboards", {"period": "all", "limit": "50", "offset": "0"}),
    ("list_relationships_market", lambda c: c.list_relationships(market_id=26, type="implication", depth=2, limit=50, cursor="c", tournament_id=TID),
     "GET", "/relationships", "/relationships",
     {"marketId": "26", "type": "implication", "depth": "2", "limit": "50", "cursor": "c", "tournamentId": TID}),
    ("list_relationships_exchange", lambda c: c.list_relationships(exchange_id=36), "GET", "/relationships", "/relationships",
     {"exchangeId": "36", "limit": "200"}),
    ("get_relationship_graph", lambda c: c.get_relationship_graph(26, depth=3, tournament_id=TID), "GET", "/relationships/graph", "/relationships/graph",
     {"marketId": "26", "depth": "3", "tournamentId": TID}),
    ("get_relationship_constraints", lambda c: c.get_relationship_constraints(market_id=26, relationship_id=REL_ID, violations_only=True,
                                                                              min_violation=0.05, tournament_id=TID),
     "GET", "/relationships/constraints", "/relationships/constraints",
     {"marketId": "26", "relationshipId": REL_ID, "violationsOnly": "true", "minViolation": "0.05", "tournamentId": TID}),
    ("get_relationship_constraints_false", lambda c: c.get_relationship_constraints(violations_only=False), "GET", "/relationships/constraints",
     "/relationships/constraints", {"violationsOnly": "false"}),
    ("get_relationship_constraints_defaults", lambda c: c.get_relationship_constraints(), "GET", "/relationships/constraints",
     "/relationships/constraints", {}),
    ("get_positions", lambda c: c.get_positions(), "GET", "/portfolio/positions", "/portfolio/positions", {}),
    ("get_pnl", lambda c: c.get_pnl("month"), "GET", "/portfolio/pnl", "/portfolio/pnl", {"period": "month"}),
    ("get_pnl_defaults", lambda c: c.get_pnl(), "GET", "/portfolio/pnl", "/portfolio/pnl", {"period": "all"}),
    ("mint_realtime_token", lambda c: c.mint_realtime_token(), "POST", "/realtime/token", "/realtime/token", {}),
]


@pytest.mark.parametrize("call, method, template, path, expected", [c[1:] for c in ENDPOINT_CASES], ids=[c[0] for c in ENDPOINT_CASES])
def test_endpoint_path_and_query_match_spec(fake, client, spec, call, method, template, path, expected):
    fake.add(method, path, EMPTY_PAGE)
    call(client)

    assert len(fake.calls) == 1
    sent = fake.calls[0]
    assert sent.method == method
    assert sent.path == path
    assert sent.params == expected

    # The concrete path is an instance of a documented operation for this method ...
    assert template in spec["paths"], f"{template} is not a documented path"
    assert method.lower() in spec["paths"][template], f"{method} {template} is not documented"
    pattern = "^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$"
    assert re.match(pattern, sent.path)
    # ... and every query parameter name/value is one the spec documents.
    documented = _spec_query_params(spec, template, method)
    for name, value in sent.params.items():
        assert name in documented, f"{name!r} is not a documented query parameter of {method} {template}: {sorted(documented)}"
        _check_against_schema(name, value, documented[name])
    if method == "POST":
        assert sent.body is None


def test_returns_decoded_json_body(fake, client):
    body = {"exchangeId": "36", "bids": [{"price": 0.4, "quantity": 10}], "asks": []}
    fake.add("GET", "/exchanges/36/orderbook", body)
    assert client.get_exchange_orderbook("36") == body


def test_lowercase_method_is_normalised(fake, client):
    fake.add("GET", "/account", {"id": "u1"})
    assert client.request("get", "/account") == {"id": "u1"}
    assert client.read_limiter.used == 1
    assert client.write_limiter.used == 0


# --------------------------------------------------------------------------- 2xx body handling


@pytest.mark.parametrize("status", [200, 204])
def test_2xx_empty_body_returns_none(fake, client, status):
    fake.add("GET", "/portfolio/positions", httpx.Response(status))
    assert client.get_positions() is None


def test_2xx_non_json_body_is_invalid_response_and_not_retried(fake, client, sleeper):
    fake.add("GET", "/account", httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.status == 200
    assert info.value.code == "INVALID_RESPONSE"
    assert info.value.method == "GET" and info.value.path == "/account"
    assert len(fake.calls) == 1
    assert sleeper.calls == []


# --------------------------------------------------------------------------- error envelope parsing


def test_error_envelope_parsed_into_api_error(fake, client):
    details = {"required": ["read"], "missing": ["read"]}
    fake.add("GET", "/markets/26", (403, error_body("INSUFFICIENT_SCOPES", "Key lacks read", details)))
    with pytest.raises(ApiError) as info:
        client.get_market(26)
    err = info.value
    assert (err.status, err.code, err.message, err.details) == (403, "INSUFFICIENT_SCOPES", "Key lacks read", details)
    assert err.retry_after is None
    assert (err.method, err.path) == ("GET", "/markets/26")
    assert "HTTP 403 INSUFFICIENT_SCOPES: Key lacks read (GET /markets/26)" in str(err)


def test_error_retry_after_header_is_parsed_even_when_not_retried(fake, client, sleeper):
    fake.add("GET", "/account", (400, error_body("VALIDATION_ERROR", "bad"), {"Retry-After": "5"}))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.retry_after == 5.0
    assert len(fake.calls) == 1 and sleeper.calls == []


def test_non_json_error_body_uses_text_and_http_code(fake, client):
    fake.add("GET", "/account", (400, "upstream proxy said no"))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.code == "HTTP_400"
    assert info.value.message == "upstream proxy said no"
    assert info.value.details == {}


def test_long_non_json_error_body_is_truncated(fake, client):
    fake.add("GET", "/account", (404, "x" * 1000))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.message == "x" * 200


def test_empty_error_body_falls_back_to_reason_phrase(fake, client):
    fake.add("GET", "/account", httpx.Response(401))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert (info.value.code, info.value.message) == ("HTTP_401", "Unauthorized")


def test_bare_string_error_body(fake, client):
    fake.add("GET", "/account", (401, {"error": "Unauthorized"}))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert (info.value.status, info.value.code, info.value.message) == (401, "HTTP_401", "Unauthorized")


def test_bare_string_error_body_with_top_level_code(fake, client):
    fake.add("GET", "/account", (401, {"error": "Key revoked", "code": "API_KEY_REVOKED"}))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert (info.value.code, info.value.message) == ("API_KEY_REVOKED", "Key revoked")
    assert info.value.hint is not None  # known auth code gets a remediation hint


def test_envelope_with_missing_fields_and_non_dict_details(fake, client):
    fake.add("GET", "/account", (400, {"error": {"code": "VALIDATION_ERROR", "details": ["not", "a", "dict"]}}))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.code == "VALIDATION_ERROR"
    assert info.value.message == "Bad Request"
    assert info.value.details == {}


def test_json_error_body_that_is_not_an_object(fake, client):
    fake.add("GET", "/account", (400, ["oops"]))
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert info.value.code == "HTTP_400"
    assert "oops" in info.value.message


# --------------------------------------------------------------------------- retry policy: 4xx


@pytest.mark.parametrize(
    "status, code",
    [
        (400, "VALIDATION_ERROR"),
        (400, "INVALID_CURSOR"),
        (401, "INVALID_API_KEY"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (409, "CONFLICT"),
        (422, "RELATIONSHIP_VIOLATION"),
    ],
)
def test_4xx_never_retried(fake, client, sleeper, status, code):
    fake.add("GET", "/account", (status, error_body(code, "no")), {"id": "never"})
    with pytest.raises(ApiError) as info:
        client.get_account()
    assert (info.value.status, info.value.code) == (status, code)
    assert len(fake.calls) == 1
    assert sleeper.calls == []
    assert client.requests_sent == 1


def test_4xx_not_retried_for_retry_unsafe_post_either(fake, client, sleeper):
    fake.add("POST", "/realtime/token", (403, error_body("FORBIDDEN", "no")), {"token": "t"})
    with pytest.raises(ApiError):
        client.mint_realtime_token()
    assert len(fake.calls) == 1 and sleeper.calls == []


# --------------------------------------------------------------------------- retry policy: 429


def test_429_with_retry_after_seconds_pauses_limiter_once(fake, client, sleeper, clock):
    handler = _recorder(clock, {"id": "u1"})
    fake.add("GET", "/account", (429, error_body("RATE_LIMITED", "slow down"), {"Retry-After": "60"}), handler)
    assert client.get_account() == {"id": "u1"}
    assert len(fake.calls) == 2
    # exactly one wait of Retry-After seconds (via the limiter pause), not a sleep plus a pause
    assert sleeper.calls == [60.0]
    assert handler.times == [1060.0]


def test_429_with_retry_after_http_date(fake, client, sleeper):
    when = email.utils.formatdate(time.time() + 45, usegmt=True)
    fake.add("GET", "/account", (429, error_body("RATE_LIMITED"), {"Retry-After": when}), {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    assert len(sleeper.calls) == 1
    assert 43.0 <= sleeper.calls[0] <= 45.0


def test_429_without_retry_after_waits_at_least_one_second(fake, client, sleeper, upper_backoff):
    fake.add("GET", "/account", (429, error_body("RATE_LIMITED")), (429, error_body("RATE_LIMITED")), {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    assert len(fake.calls) == 3
    # backoff ceilings 0.25 and 0.5 are floored to 1s for rate limiting
    assert sleeper.calls == [1.0, 1.0]


def test_429_wait_is_capped_by_max_retry_wait(fake, make_client, sleeper):
    c = make_client(max_retry_wait=30)
    fake.add("GET", "/account", (429, error_body("RATE_LIMITED"), {"Retry-After": "600"}), {"id": "u1"})
    c.get_account()
    assert sleeper.calls == [30.0]


def test_429_on_write_pauses_write_limiter(fake, client, sleeper, clock):
    handler = _recorder(clock, {"token": "t"})
    fake.add("POST", "/realtime/token", (429, error_body("RATE_LIMITED"), {"Retry-After": "7"}), handler)
    assert client.mint_realtime_token() == {"token": "t"}
    assert sleeper.calls == [7.0]
    assert handler.times == [1007.0]  # the retry went out only once the pause expired
    assert (client.write_limiter.used, client.read_limiter.used) == (2, 0)


def test_429_retried_for_plain_post_too(fake, client, sleeper):
    fake.add("POST", "/something", (429, error_body("RATE_LIMITED"), {"Retry-After": "2"}), {"ok": True})
    assert client.request("POST", "/something") == {"ok": True}
    assert len(fake.calls) == 2
    assert sleeper.calls == [2.0]


@pytest.mark.xfail(strict=True, reason="BUG: a 429 that is raised (retries exhausted) never pauses the limiter, so the next call ignores Retry-After")
def test_429_given_up_still_honours_retry_after_for_next_call(fake, make_client, sleeper, clock):
    c = make_client(max_retries=0)
    handler = _recorder(clock)
    fake.add("GET", "/account", (429, error_body("RATE_LIMITED"), {"Retry-After": "60"}), handler)
    with pytest.raises(ApiError) as info:
        c.get_account()
    assert info.value.code == "RATE_LIMITED" and info.value.retry_after == 60.0
    # The account budget is shared; the next request must not go out before Retry-After elapses.
    c.get_account()
    assert handler.times and handler.times[0] >= 1060.0


# --------------------------------------------------------------------------- retry policy: 503 / 409 / 5xx


@pytest.mark.parametrize("code", ["TX_CONFLICT", "SERVICE_UNAVAILABLE"])
def test_503_retried_with_exponential_backoff(fake, client, sleeper, upper_backoff, code):
    fake.add("GET", "/account", (503, error_body(code)), (503, error_body(code)), (503, error_body(code)), {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    assert len(fake.calls) == 4
    assert sleeper.calls == [0.25, 0.5, 1.0]
    assert client.requests_sent == 4


def test_backoff_is_jittered_within_half_to_full_ceiling(fake, make_client, sleeper):
    c = make_client(max_retries=6)
    fake.add("GET", "/account", *([(503, error_body("TX_CONFLICT"))] * 6), {"id": "u1"})
    c.get_account()
    ceilings = [min(20.0, 0.25 * 2**i) for i in range(6)]
    assert len(sleeper.calls) == 6
    for slept, ceiling in zip(sleeper.calls, ceilings):
        assert ceiling / 2 <= slept <= ceiling


def test_backoff_cap(fake, make_client, sleeper, upper_backoff):
    c = make_client(backoff_base=10, backoff_cap=15, max_retry_wait=1000)
    fake.add("GET", "/account", (503, error_body("TX_CONFLICT")), (503, error_body("TX_CONFLICT")), (503, error_body("TX_CONFLICT")), {})
    c.get_account()
    assert sleeper.calls == [10.0, 15.0, 15.0]


def test_503_with_retry_after_header_waits_at_least_that_long(fake, client, sleeper):
    fake.add("GET", "/account", (503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "3"}), {"id": "u1"})
    client.get_account()
    assert sleeper.calls == [3.0]


def test_503_standings_updating_uses_retry_after_header(fake, client, sleeper):
    body = error_body("SERVICE_UNAVAILABLE", "Standings are updating", {"reason": "STANDINGS_UPDATING", "retryAfterSeconds": 10})
    fake.add("GET", f"/tournaments/{SLUG}/leaderboard", (503, body, {"Retry-After": "10"}), {"leaderboard": [], "total": 0})
    assert client.get_tournament_leaderboard(SLUG) == {"leaderboard": [], "total": 0}
    assert sleeper.calls == [10.0]


def test_503_standings_updating_falls_back_to_details_retry_after_seconds(fake, client, sleeper):
    body = error_body("SERVICE_UNAVAILABLE", "Standings are updating", {"reason": "STANDINGS_UPDATING", "retryAfterSeconds": 12})
    fake.add("GET", "/leaderboards", (503, body), {"leaderboard": [], "total": 0})
    client.get_leaderboard()
    assert sleeper.calls == [12.0]


def test_503_wait_is_capped_by_max_retry_wait(fake, make_client, sleeper):
    c = make_client(max_retry_wait=5)
    fake.add("GET", "/account", (503, error_body("SERVICE_UNAVAILABLE"), {"Retry-After": "90"}), {})
    c.get_account()
    assert sleeper.calls == [5.0]


def test_503_retried_for_plain_post(fake, client, sleeper):
    fake.add("POST", "/something", (503, error_body("TX_CONFLICT")), {"ok": True})
    assert client.request("POST", "/something") == {"ok": True}
    assert len(fake.calls) == 2


def test_409_request_in_flight_waits_90_seconds_by_default(fake, client, sleeper):
    fake.add("POST", "/something", (409, error_body("REQUEST_IN_FLIGHT", "still running")), {"ok": True})
    assert client.request("POST", "/something", json={"idempotencyKey": "k"}) == {"ok": True}
    assert sleeper.calls == [90.0]
    assert [c.body for c in fake.calls] == [{"idempotencyKey": "k"}] * 2  # identical payload replayed


def test_409_request_in_flight_honours_retry_after(fake, client, sleeper):
    fake.add("GET", "/account", (409, error_body("REQUEST_IN_FLIGHT"), {"Retry-After": "15"}), {"id": "u1"})
    client.get_account()
    assert sleeper.calls == [15.0]


@pytest.mark.parametrize("status", [500, 502, 504])
def test_5xx_retried_for_get(fake, client, sleeper, upper_backoff, status):
    fake.add("GET", "/account", (status, error_body("INTERNAL_ERROR")), {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    assert len(fake.calls) == 2
    assert sleeper.calls == [0.25]


@pytest.mark.parametrize("status, code", [(500, "INTERNAL_ERROR"), (502, "ORDER_STATUS_UNKNOWN"), (504, "HTTP_504")])
def test_5xx_not_retried_for_plain_post(fake, client, sleeper, status, code):
    fake.add("POST", "/something", (status, error_body(code)), {"ok": True})
    with pytest.raises(ApiError) as info:
        client.request("POST", "/something", json={"x": 1})
    assert info.value.status == status
    assert len(fake.calls) == 1
    assert sleeper.calls == []


def test_mint_realtime_token_retried_on_5xx_and_503(fake, client, sleeper):
    fake.add(
        "POST",
        "/realtime/token",
        (500, error_body("INTERNAL_ERROR")),
        (502, "bad gateway"),
        (503, error_body("SERVICE_UNAVAILABLE")),
        (504, ""),
        {"token": "jwt", "expiresAt": "2026-10-03T15:00:00Z"},
    )
    assert client.mint_realtime_token()["token"] == "jwt"
    assert len(fake.calls) == 5
    assert all(c.method == "POST" for c in fake.calls)
    assert client.write_limiter.used == 5 and client.read_limiter.used == 0


def test_max_retries_exhaustion_raises_last_error(fake, make_client, sleeper):
    c = make_client(max_retries=2)
    fake.add(
        "GET",
        "/account",
        (503, error_body("TX_CONFLICT", "first")),
        (503, error_body("SERVICE_UNAVAILABLE", "second")),
        (503, error_body("TX_CONFLICT", "third")),
        {"id": "too late"},
    )
    with pytest.raises(ApiError) as info:
        c.get_account()
    assert (info.value.code, info.value.message) == ("TX_CONFLICT", "third")
    assert len(fake.calls) == 3
    assert len(sleeper.calls) == 2


def test_max_retries_zero_disables_retries(fake, make_client, sleeper):
    c = make_client(max_retries=0)
    fake.add("GET", "/account", (503, error_body("TX_CONFLICT")), {"id": "u1"})
    with pytest.raises(ApiError):
        c.get_account()
    assert len(fake.calls) == 1 and sleeper.calls == []


# --------------------------------------------------------------------------- transport errors


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset")])
def test_transport_error_retried_for_get(fake, client, sleeper, upper_backoff, exc):
    fake.add("GET", "/account", exc, exc, {"id": "u1"})
    assert client.get_account() == {"id": "u1"}
    assert len(fake.calls) == 3
    assert sleeper.calls == [0.25, 0.5]


def test_transport_error_exhaustion_raises_network_error(fake, make_client, sleeper):
    c = make_client(max_retries=3)
    fake.add("GET", "/account", httpx.ConnectError("refused"))
    with pytest.raises(NetworkError) as info:
        c.get_account()
    assert len(fake.calls) == 4
    assert len(sleeper.calls) == 3
    assert "GET /account" in str(info.value)
    assert isinstance(info.value.__cause__, httpx.ConnectError)


def test_post_network_error_not_retried(fake, client, sleeper):
    fake.add("POST", "/something", httpx.ReadTimeout("slow"), {"ok": True})
    with pytest.raises(NetworkError):
        client.request("POST", "/something", json={"x": 1})
    assert len(fake.calls) == 1 and sleeper.calls == []


def test_post_network_error_retried_when_retry_unsafe(fake, client, sleeper):
    fake.add("POST", "/realtime/token", httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), {"token": "t"})
    assert client.mint_realtime_token() == {"token": "t"}
    assert len(fake.calls) == 3
    assert len(sleeper.calls) == 2


def test_network_errors_and_http_errors_share_the_retry_budget(fake, make_client):
    c = make_client(max_retries=2)
    fake.add("GET", "/account", httpx.ConnectError("refused"), (503, error_body("TX_CONFLICT")), (503, error_body("TX_CONFLICT", "last")), {})
    with pytest.raises(ApiError) as info:
        c.get_account()
    assert info.value.message == "last"
    assert len(fake.calls) == 3


# --------------------------------------------------------------------------- rate limiters


def test_reads_and_writes_use_separate_limiters(fake, make_client, sleeper, clock):
    c = make_client(reads_per_min=2, writes_per_min=1)
    fake.add("GET", "/account", {"id": "u1"})
    fake.add("POST", "/realtime/token", {"token": "t"})
    c.get_account()
    c.get_account()
    c.mint_realtime_token()  # write budget is independent of the exhausted read budget
    assert sleeper.calls == []
    assert (c.read_limiter.used, c.write_limiter.used) == (2, 1)
    c.get_account()  # third read inside the window must wait for the window to slide
    assert sleeper.calls == [60.0]
    c.mint_realtime_token()  # write window also slid during that wait
    assert sleeper.calls == [60.0]


def test_head_counts_as_read(fake, client):
    fake.add("HEAD", "/account", httpx.Response(200))
    assert client.request("HEAD", "/account") is None
    assert (client.read_limiter.used, client.write_limiter.used) == (1, 0)


def test_every_attempt_counts_against_the_limiter(fake, client):
    fake.add("GET", "/account", (503, error_body("TX_CONFLICT")), (500, error_body("INTERNAL_ERROR")), {})
    client.get_account()
    assert client.read_limiter.used == 3
    assert client.requests_sent == 3


# --------------------------------------------------------------------------- iter_cursor


def _cursor_page(items: List[Any], cursor: Any = None, has_more: Any = None) -> Dict[str, Any]:
    return {"data": items, "pagination": {"limit": 2, "hasMore": cursor is not None if has_more is None else has_more, "nextCursor": cursor}}


def test_iter_cursor_follows_next_cursor(fake, client):
    fake.add("GET", "/markets", _cursor_page([1, 2], "c1"), _cursor_page([3, 4], "c2"), _cursor_page([5]))
    assert list(client.iter_cursor("/markets", {"limit": 2, "status": "open"})) == [1, 2, 3, 4, 5]
    params = [c.params for c in fake.calls]
    assert params == [
        {"limit": "2", "status": "open"},
        {"limit": "2", "status": "open", "cursor": "c1"},
        {"limit": "2", "status": "open", "cursor": "c2"},
    ]


def test_iter_cursor_stops_when_has_more_false_even_with_cursor(fake, client):
    fake.add("GET", "/markets", _cursor_page([1], "c1", has_more=False), _cursor_page([99]))
    assert list(client.iter_cursor("/markets")) == [1]
    assert len(fake.calls) == 1


def test_iter_cursor_stops_on_null_cursor_even_with_has_more(fake, client):
    fake.add("GET", "/markets", _cursor_page([1], None, has_more=True), _cursor_page([99]))
    assert list(client.iter_cursor("/markets")) == [1]
    assert len(fake.calls) == 1


def test_iter_cursor_stops_on_repeated_cursor(fake, client):
    fake.add("GET", "/markets", _cursor_page([1], "same"), _cursor_page([2], "same"), _cursor_page([3]))
    assert list(client.iter_cursor("/markets")) == [1, 2]
    assert len(fake.calls) == 2


def test_iter_cursor_max_items_stops_without_extra_request(fake, client):
    fake.add("GET", "/markets", _cursor_page([1, 2], "c1"), _cursor_page([3, 4], "c2"), _cursor_page([5]))
    assert list(client.iter_cursor("/markets", max_items=3)) == [1, 2, 3]
    assert len(fake.calls) == 2
    fake.calls.clear()
    fake.routes.clear()
    fake.add("GET", "/markets", _cursor_page([1, 2], "c1"), _cursor_page([3]))
    assert list(client.iter_cursor("/markets", max_items=2)) == [1, 2]
    assert len(fake.calls) == 1  # page boundary: no fetch of a page we will not use


@pytest.mark.xfail(strict=True, reason="BUG: max_items=0 yields one item (limit checked only after yielding)")
def test_iter_cursor_max_items_zero_yields_nothing(fake, client):
    fake.add("GET", "/markets", _cursor_page([1, 2], "c1"))
    assert list(client.iter_cursor("/markets", max_items=0)) == []


def test_iter_cursor_max_pages(fake, client):
    fake.add("GET", "/markets", _cursor_page([1], "c1"), _cursor_page([2], "c2"), _cursor_page([3], "c3"), _cursor_page([4]))
    assert list(client.iter_cursor("/markets", max_pages=2)) == [1, 2]
    assert len(fake.calls) == 2


def test_iter_cursor_custom_items_key_initial_cursor_and_no_param_mutation(fake, client):
    fake.add("GET", "/x", {"rows": ["a"], "pagination": {"hasMore": True, "nextCursor": "n1"}}, {"rows": ["b"], "pagination": {"hasMore": False}})
    params = {"cursor": "start", "limit": 10}
    assert list(client.iter_cursor("/x", params, items_key="rows")) == ["a", "b"]
    assert params == {"cursor": "start", "limit": 10}
    assert [c.params.get("cursor") for c in fake.calls] == ["start", "n1"]


def test_iter_cursor_tolerates_empty_body_and_missing_pagination(fake, client):
    fake.add("GET", "/a", httpx.Response(204))
    fake.add("GET", "/b", {"data": [1, 2]})
    assert list(client.iter_cursor("/a")) == []
    assert list(client.iter_cursor("/b")) == [1, 2]
    assert len(fake.calls) == 2


def test_iter_cursor_is_lazy(fake, client):
    fake.add("GET", "/markets", _cursor_page([1, 2], "c1"), _cursor_page([3]))
    gen = client.iter_cursor("/markets")
    assert fake.calls == []
    assert next(gen) == 1
    assert len(fake.calls) == 1


def test_iter_tournament_markets_follows_cursor_and_respects_max_items(fake, client):
    path = f"/tournaments/{SLUG}/markets"
    fake.add("GET", path, _cursor_page([{"id": "1"}, {"id": "2"}], "c1"), _cursor_page([{"id": "3"}]))
    assert [m["id"] for m in client.iter_tournament_markets(SLUG, max_items=5)] == ["1", "2", "3"]
    assert fake.calls[1].params["cursor"] == "c1"
    assert fake.calls[1].params["limit"] == "100"


# --------------------------------------------------------------------------- iter_offset


def test_iter_offset_has_more_style(fake, client):
    fake.add(
        "GET",
        "/tournaments",
        {"data": [1, 2], "pagination": {"limit": 2, "offset": 0, "hasMore": True, "total": 3}},
        {"data": [3], "pagination": {"limit": 2, "offset": 2, "hasMore": False, "total": 3}},
        {"data": [99], "pagination": {"hasMore": False}},
    )
    assert list(client.iter_offset("/tournaments", {"status": "any"}, page_size=2)) == [1, 2, 3]
    assert [c.params for c in fake.calls] == [
        {"status": "any", "limit": "2", "offset": "0"},
        {"status": "any", "limit": "2", "offset": "2"},
    ]


def test_iter_offset_has_more_true_then_empty_page_stops(fake, client):
    fake.add("GET", "/tournaments", {"data": [1], "pagination": {"hasMore": True}}, {"data": [], "pagination": {"hasMore": True}})
    assert list(client.iter_offset("/tournaments")) == [1]
    assert len(fake.calls) == 2


def test_iter_offset_total_style_leaderboard(fake, client):
    fake.add(
        "GET",
        "/leaderboards",
        {"leaderboard": [{"rank": 1}, {"rank": 2}], "total": 5, "limit": 2, "offset": 0},
        {"leaderboard": [{"rank": 3}, {"rank": 4}], "total": 5, "limit": 2, "offset": 2},
        {"leaderboard": [{"rank": 5}], "total": 5, "limit": 2, "offset": 4},
        {"leaderboard": [{"rank": 99}], "total": 5},
    )
    ranks = [e["rank"] for e in client.iter_offset("/leaderboards", {"period": "all"}, items_key="leaderboard", page_size=2)]
    assert ranks == [1, 2, 3, 4, 5]
    assert [c.params["offset"] for c in fake.calls] == ["0", "2", "4"]
    assert all(c.params["limit"] == "2" and c.params["period"] == "all" for c in fake.calls)


def test_iter_offset_without_total_or_has_more_reads_one_page(fake, client):
    fake.add("GET", "/x", {"data": [1, 2]}, {"data": [3]})
    assert list(client.iter_offset("/x")) == [1, 2]
    assert len(fake.calls) == 1


def test_iter_offset_empty_first_page(fake, client):
    fake.add("GET", "/x", {"data": [], "pagination": {"hasMore": True}})
    assert list(client.iter_offset("/x")) == []
    assert len(fake.calls) == 1


def test_iter_offset_max_items(fake, client):
    fake.add("GET", "/x", {"data": [1, 2, 3], "total": 9}, {"data": [4, 5, 6], "total": 9}, {"data": [7, 8, 9], "total": 9})
    assert list(client.iter_offset("/x", page_size=3, max_items=4)) == [1, 2, 3, 4]
    assert len(fake.calls) == 2


@pytest.mark.xfail(strict=True, reason="BUG: max_items=0 yields one item (limit checked only after yielding)")
def test_iter_offset_max_items_zero_yields_nothing(fake, client):
    fake.add("GET", "/x", {"data": [1, 2, 3], "total": 3})
    assert list(client.iter_offset("/x", max_items=0)) == []


def test_iter_offset_starts_from_given_offset(fake, client):
    fake.add("GET", "/x", {"data": [11, 12], "total": 12})
    params = {"offset": 10, "limit": 999}
    assert list(client.iter_offset("/x", params, page_size=50)) == [11, 12]
    assert fake.calls[0].params == {"offset": "10", "limit": "50"}
    assert params == {"offset": 10, "limit": 999}


def test_iter_tournaments_pages_by_offset(fake, client):
    fake.add(
        "GET",
        "/tournaments",
        {"data": [{"slug": "a"}] * 100, "pagination": {"limit": 100, "offset": 0, "hasMore": True, "total": 101}},
        {"data": [{"slug": "b"}], "pagination": {"limit": 100, "offset": 100, "hasMore": False, "total": 101}},
    )
    assert len(list(client.iter_tournaments())) == 101
    assert [c.params["offset"] for c in fake.calls] == ["0", "100"]


# --------------------------------------------------------------------------- get_prices (bulk, chunked)


def _prices_handler(missing_every: int = 0) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split(",")
        data = [price(i, "m" + i, 0.5, 0.49, 0.51) for i in ids if not (missing_every and int(i) % missing_every == 0)]
        missing = [int(i) for i in ids if missing_every and int(i) % missing_every == 0]
        return httpx.Response(200, json={"data": data, "missingIds": missing})

    return handler


def test_get_prices_chunks_to_100_and_preserves_order(fake, client):
    ids = list(range(1000, 1250))
    fake.add("GET", "/exchanges/prices", _prices_handler(missing_every=50))
    result = client.get_prices(ids, tournament_id=TID)

    sent = [c.params["ids"].split(",") for c in fake.calls]
    assert [len(chunk) for chunk in sent] == [100, 100, 50]
    assert [i for chunk in sent for i in chunk] == [str(i) for i in ids]
    assert all(c.params["tournamentId"] == TID for c in fake.calls)

    expected_missing = [str(i) for i in ids if i % 50 == 0]
    assert result["missingIds"] == expected_missing  # merged across chunks, stringified
    assert [p["exchangeId"] for p in result["data"]] == [str(i) for i in ids if i % 50 != 0]


@pytest.mark.parametrize("count, chunks", [(1, [1]), (100, [100]), (101, [100, 1]), (200, [100, 100])])
def test_get_prices_chunk_boundaries(fake, client, count, chunks):
    fake.add("GET", "/exchanges/prices", _prices_handler())
    client.get_prices(range(1, count + 1))
    assert [len(c.params["ids"].split(",")) for c in fake.calls] == chunks


def test_get_prices_deduplicates_keeping_first_occurrence(fake, client):
    fake.add("GET", "/exchanges/prices", _prices_handler())
    result = client.get_prices([37, "36", 36, "37", 42, 36])
    assert len(fake.calls) == 1
    assert fake.calls[0].params == {"ids": "37,36,42"}
    assert [p["exchangeId"] for p in result["data"]] == ["37", "36", "42"]


def test_get_prices_accepts_generators(fake, client):
    fake.add("GET", "/exchanges/prices", _prices_handler())
    client.get_prices(str(i) for i in (5, 6))
    assert fake.calls[0].params["ids"] == "5,6"


def test_get_prices_empty_input_makes_no_request(fake, client):
    assert client.get_prices([]) == {"data": [], "missingIds": []}
    assert fake.calls == []


def test_get_prices_tolerates_sparse_chunk_bodies(fake, client):
    fake.add("GET", "/exchanges/prices", {"data": [price("1", "m", 0.5, None, None)]}, httpx.Response(204))
    result = client.get_prices(range(1, 151))
    assert [p["exchangeId"] for p in result["data"]] == ["1"]
    assert result["missingIds"] == []


# --------------------------------------------------------------------------- argument validation


def test_list_exchanges_market_id_and_ids_mutually_exclusive(fake, client):
    with pytest.raises(ValueError, match="mutually exclusive"):
        client.list_exchanges(market_id=26, ids=[36])
    assert fake.calls == []


def test_list_exchanges_more_than_100_ids_rejected(fake, client):
    with pytest.raises(ValueError):
        client.list_exchanges(ids=[str(i) for i in range(101)])
    assert fake.calls == []
    fake.add("GET", "/exchanges", EMPTY_PAGE)
    client.list_exchanges(ids=[str(i) for i in range(100)])  # exactly 100 is fine
    assert len(fake.calls[0].params["ids"].split(",")) == 100


def test_list_exchanges_empty_ids_with_market_id_is_allowed(fake, client):
    fake.add("GET", "/exchanges", EMPTY_PAGE)
    client.list_exchanges(market_id=26, ids=[])
    assert fake.calls[0].params == {"marketId": "26", "limit": "200"}


def test_list_relationships_market_and_exchange_mutually_exclusive(fake, client):
    with pytest.raises(ValueError, match="mutually exclusive"):
        client.list_relationships(market_id=26, exchange_id=36)
    assert fake.calls == []


# --------------------------------------------------------------------------- parse_retry_after


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("60", 60.0),
        (" 5 ", 5.0),
        ("0", 0.0),
        ("1.5", 1.5),
        ("-10", 0.0),
        ("soon", None),
        ("Mon, 99 Foo 2026 25:61:00 GMT", None),
    ],
)
def test_parse_retry_after_values(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date_relative_to_now():
    stamp = 1445412480.0  # Wed, 21 Oct 2015 07:28:00 GMT
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=stamp - 30) == pytest.approx(30.0)
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=stamp + 30) == 0.0  # past dates clamp to zero


def test_parse_retry_after_http_date_defaults_to_wall_clock():
    value = email.utils.formatdate(time.time() + 120, usegmt=True)
    assert 118.0 <= parse_retry_after(value) <= 120.0
