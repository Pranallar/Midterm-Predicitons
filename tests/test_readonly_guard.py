"""Tests for the runtime read-only guards (supermarket_bot/readonly.py, Design decision D20)."""

from __future__ import annotations

import copy
from typing import List

import httpx
import pytest

from supermarket_bot.client import SuperMarketClient
from supermarket_bot.errors import register_secret
from supermarket_bot.readonly import ReadOnlyViolation, enforce_get_only, is_guarded, outside_client


def _recording_transport(seen: List[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def test_super_market_client_blocks_every_write_path() -> None:
    seen: List[httpx.Request] = []
    client = SuperMarketClient(api_key="test-key-0123456789", base_url="https://sm.invalid/api/v1",
                               transport=_recording_transport(seen), max_retries=0)
    enforce_get_only(client)
    enforce_get_only(client)  # idempotent
    assert is_guarded(client)
    assert client.get("/account") == {"ok": True}
    with pytest.raises(ReadOnlyViolation):
        client.request("POST", "/orders", json={"x": 1})
    with pytest.raises(ReadOnlyViolation):
        client.mint_realtime_token()
    http = client._http
    with pytest.raises(ReadOnlyViolation):
        http.send(http.build_request("DELETE", "/orders/1"))
    with pytest.raises(ReadOnlyViolation):
        with http.stream("PUT", "/orders/1"):
            pass
    quick = copy.copy(client)  # tracker.single_attempt_client copies the client: the guard is shared
    with pytest.raises(ReadOnlyViolation):
        quick.request("PATCH", "/orders/1")
    assert [r.method for r in seen] == ["GET"]
    assert len(http.event_hooks["request"]) == 1
    client.close()


def test_outside_client_is_anonymous_and_get_only() -> None:
    seen: List[httpx.Request] = []
    register_secret("sk-live-secret-key-abcdef")
    with outside_client(label="Polymarket", transport=_recording_transport(seen), base_url="https://gamma.invalid") as c:
        assert c.get("/markets", params={"id": "1"}).status_code == 200
        with pytest.raises(ReadOnlyViolation):
            c.post("/markets")
        with pytest.raises(ReadOnlyViolation):
            c.get("/markets", headers={"Authorization": "Bearer abc"})
        with pytest.raises(ReadOnlyViolation):
            c.get("/markets", headers={"Cookie": "a=b"})
        with pytest.raises(ReadOnlyViolation):
            c.get("/markets", params={"key": "sk-live-secret-key-abcdef"})
        assert not c.follow_redirects
    assert len(seen) == 1 and "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert "read-only" in seen[0].headers["user-agent"]


def test_outside_client_never_stores_or_sends_venue_cookies():
    """Polymarket's CDN sets a cookie on the first answer; the next request must stay anonymous
    (it used to carry the cookie and trip the guard, crashing `fairvalue`)."""
    import httpx

    from supermarket_bot.readonly import outside_client

    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json=[], headers={"Set-Cookie": "__cf_bm=abc; Path=/; Domain=.polymarket.com"})

    client = outside_client(label="Polymarket", transport=httpx.MockTransport(handler),
                            base_url="https://gamma-api.polymarket.com")
    client.get("/markets")
    client.get("/markets")
    assert seen == [None, None]
