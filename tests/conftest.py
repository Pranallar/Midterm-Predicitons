"""Shared fixtures: an in-memory fake of the Super Market REST API.

Nothing here touches the network. ``FakeAPI`` plugs into ``httpx.MockTransport``
and answers by ``(method, path)``; every request is recorded in ``fake.calls``.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union

import httpx
import pytest

from supermarket_bot.client import SuperMarketClient

BASE_URL = "https://example.test/api/v1"
API_KEY = "ace_test_0123456789abcdef"
TOURNAMENT_ID = "550e8400-e29b-41d4-a716-446655440000"
TOURNAMENT_SLUG = "predictions-cup"

Handler = Callable[[httpx.Request], httpx.Response]
Reply = Union[Dict[str, Any], List[Any], Tuple[int, Any], Tuple[int, Any, Dict[str, str]], httpx.Response, Handler, Exception]


def error_body(code: str, message: str = "error", details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


class Call:
    def __init__(self, request: httpx.Request) -> None:
        self.request = request
        self.method = request.method
        self.path = request.url.path[len("/api/v1") :] if request.url.path.startswith("/api/v1") else request.url.path
        self.params = dict(request.url.params)
        self.headers = request.headers
        self.body = json.loads(request.content) if request.content else None

    def __repr__(self) -> str:
        return f"Call({self.method} {self.path} {self.params})"


class FakeAPI:
    """Route table of canned replies.

    ``add(method, path, *replies)`` queues replies for that route. Each reply is
    one of: a dict/list (200 JSON), ``(status, body)``, ``(status, body, headers)``,
    an ``httpx.Response``, an exception instance to raise (e.g.
    ``httpx.ConnectError``), or a callable ``(request) -> httpx.Response``. The last
    queued reply repeats once the queue is drained.
    """

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Deque[Reply]] = {}
        self.calls: List[Call] = []

    def add(self, method: str, path: str, *replies: Reply) -> "FakeAPI":
        self.routes.setdefault((method.upper(), path), deque()).extend(replies)
        return self

    def calls_to(self, path: str, method: Optional[str] = None) -> List[Call]:
        return [c for c in self.calls if c.path == path and (method is None or c.method == method.upper())]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        call = Call(request)
        self.calls.append(call)
        queue = self.routes.get((call.method, call.path))
        if not queue:
            return httpx.Response(404, json=error_body("NOT_FOUND", f"no fake route for {call.method} {call.path}"))
        reply = queue.popleft() if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply) and not isinstance(reply, (dict, list)):
            return reply(request)
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, tuple):
            status, body = reply[0], reply[1]
            headers = reply[2] if len(reply) > 2 else {}
            if isinstance(body, (dict, list)):
                return httpx.Response(status, json=body, headers=headers)
            return httpx.Response(status, text=str(body or ""), headers=headers)
        return httpx.Response(200, json=reply)


class Sleeper:
    """Records requested sleeps instead of sleeping; optionally advances a fake clock."""

    def __init__(self, clock: Optional["FakeClock"] = None) -> None:
        self.calls: List[float] = []
        self.clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.now += seconds

    @property
    def total(self) -> float:
        return sum(self.calls)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeper(clock: FakeClock) -> Sleeper:
    return Sleeper(clock)


@pytest.fixture
def make_client(fake: FakeAPI, sleeper: Sleeper, clock: FakeClock) -> Callable[..., SuperMarketClient]:
    created: List[SuperMarketClient] = []

    def factory(**kwargs: Any) -> SuperMarketClient:
        opts: Dict[str, Any] = dict(
            transport=httpx.MockTransport(fake),
            sleep=sleeper,
            clock=clock,
            reads_per_min=10_000,
            writes_per_min=10_000,
        )
        opts.update(kwargs)
        client = SuperMarketClient(API_KEY, BASE_URL, **opts)
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def client(make_client: Callable[..., SuperMarketClient]) -> SuperMarketClient:
    return make_client()


# --------------------------------------------------------------------------- sample data
# Shapes follow docs/supermarket-openapi.json.


def tournament(slug: str = TOURNAMENT_SLUG, tid: str = TOURNAMENT_ID, status: str = "active", name: str = "SIG Predictions Cup") -> Dict[str, Any]:
    return {
        "id": tid,
        "slug": slug,
        "name": name,
        "description": None,
        "status": status,
        "startDate": "2026-09-01T00:00:00.000Z",
        "endDate": "2026-11-04T00:00:00.000Z",
        "initialBalance": 1000,
        "currencyName": "SIG Coins",
        "myBalance": 950.5,
        "joinedAt": "2026-09-02T00:00:00.000Z",
        "isPendingEnrolment": False,
    }


def tournament_page(items: List[Dict[str, Any]], offset: int = 0, has_more: bool = False) -> Dict[str, Any]:
    return {"data": items, "pagination": {"limit": 100, "offset": offset, "hasMore": has_more, "total": len(items)}}


def market(mid: str, title: str, exchanges: List[Tuple[str, Optional[str], Optional[float]]], multi: Optional[bool] = None, status: str = "open") -> Dict[str, Any]:
    return {
        "id": mid,
        "title": title,
        "status": status,
        "createdAt": "2026-09-01T00:00:00.000Z",
        "settlementDate": "2026-11-04T04:59:00.000Z",
        "settledWith": None,
        "settledOn": None,
        "categories": ["Election Outcome"],
        "isComposite": False,
        "isMultiOutcome": len(exchanges) > 1 if multi is None else multi,
        "exchanges": [{"id": eid, "option": opt, "latestPrice": px, "initialPrice": 0.5} for eid, opt, px in exchanges],
        "creator": {"id": "u1", "username": None},
        "thumbnailUrl": None,
    }


def market_page(items: List[Dict[str, Any]], cursor: Optional[str] = None) -> Dict[str, Any]:
    return {"data": items, "pagination": {"total": len(items), "limit": 100, "hasMore": cursor is not None, "nextCursor": cursor}}


def price(eid: str, mid: str, latest: Optional[float], bid: Optional[float], ask: Optional[float], option: Optional[str] = "YES") -> Dict[str, Any]:
    spread = round(ask - bid, 6) if bid is not None and ask is not None else None
    return {"exchangeId": eid, "marketId": mid, "option": option, "latestPrice": latest, "bestBid": bid, "bestAsk": ask, "spread": spread}


def book_exchange(eid: str, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], seq: Optional[int] = 100, at: str = "2026-10-03T12:00:00.000+00:00", option: Optional[str] = "YES") -> Dict[str, Any]:
    return {
        "exchangeId": eid,
        "option": option,
        "asOf": {"sequence": seq, "at": at} if seq is not None else None,
        "impliedProbability": None,
        "bids": [{"price": p, "quantity": q} for p, q in bids],
        "asks": [{"price": p, "quantity": q} for p, q in asks],
    }


def market_orderbook(exchanges: List[Dict[str, Any]], overround: Optional[float] = 1.0, arb: bool = False, tid: Optional[str] = TOURNAMENT_ID) -> Dict[str, Any]:
    ob = {"exchanges": exchanges, "overround": overround, "hasArbitrageOpportunity": arb}
    ctx_tournament = None if tid is None else {"id": tid, "slug": TOURNAMENT_SLUG, "name": "SIG Predictions Cup", "currencyName": "SIG Coins", "isOngoingPlay": False}
    return {**ob, "contexts": [{"type": "tournament" if tid else "public", "tournament": ctx_tournament, "orderbook": ob}]}
