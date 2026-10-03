"""Read-only HTTP client for the Super Market API (``/api/v1``).

Covers every market-data read a participant key can make: account, tournaments,
markets, exchanges (prices, books, candles, trade tape), leaderboards,
relationships (ALL) and portfolio reads, plus minting a realtime token.

Order placement is intentionally not implemented: this bot only reads data.

Behaviour that follows the API docs:

* Auth via ``Authorization: Bearer <key>``.
* Client-side sliding-window rate limits (reads and writes counted separately).
* ``429 RATE_LIMITED`` waits for ``Retry-After`` (and pauses every caller, since the
  budget is per account), ``503`` is retried with exponential backoff and jitter,
  other ``4xx`` are raised immediately. Idempotent reads also retry ``500/502/504``
  and network errors.
* Cursor pagination follows ``pagination.nextCursor`` while ``hasMore`` is true.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

import httpx

from . import __version__
from .config import DEFAULT_BASE_URL, DEFAULT_READS_PER_MIN, DEFAULT_WRITES_PER_MIN, Settings
from .errors import ApiError, NetworkError
from .ratelimit import SlidingWindowLimiter

log = logging.getLogger("supermarket_bot")

Id = Union[str, int]
JSON = Dict[str, Any]

READ_METHODS = ("GET", "HEAD")
BULK_PRICE_MAX_IDS = 100
EXCHANGE_LIST_MAX_IDS = 100


def _seg(value: Id) -> str:
    """Quote one path segment (IDs and slugs)."""
    text = str(value).strip()
    if not text:
        raise ValueError("path parameter must not be empty")
    return quote(text, safe="")


def _clean_params(params: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple, set)):
            joined = ",".join(str(v) for v in value)
            if joined:
                out[key] = joined
        else:
            out[key] = value
    return out


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    current = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - current)


class SuperMarketClient:
    """Synchronous, thread-safe client. Use as a context manager or call :meth:`close`."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 20.0,
        max_retries: int = 4,
        max_retry_wait: float = 120.0,
        backoff_base: float = 0.25,
        backoff_cap: float = 20.0,
        reads_per_min: int = DEFAULT_READS_PER_MIN,
        writes_per_min: int = DEFAULT_WRITES_PER_MIN,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._sleep = sleep
        self.read_limiter = SlidingWindowLimiter(reads_per_min, clock=clock, sleep=sleep)
        self.write_limiter = SlidingWindowLimiter(writes_per_min, clock=clock, sleep=sleep)
        self.requests_sent = 0
        self._http = httpx.Client(
            base_url=self.base_url + "/",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": f"supermarket-bot/{__version__}",
            },
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> "SuperMarketClient":
        return cls(
            settings.api_key,
            settings.base_url,
            reads_per_min=settings.reads_per_min,
            writes_per_min=settings.writes_per_min,
            **kwargs,
        )

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SuperMarketClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.backoff_cap, self.backoff_base * (2**attempt))
        return random.uniform(ceiling / 2, ceiling)

    def _retry_delay(self, err: ApiError, attempt: int, idempotent: bool) -> Optional[float]:
        """Seconds to wait before retrying ``err``, or ``None`` to raise it."""
        if attempt >= self.max_retries:
            return None
        if err.status == 429:
            delay = err.retry_after if err.retry_after is not None else max(1.0, self._backoff(attempt))
        elif err.code == "REQUEST_IN_FLIGHT":
            delay = err.retry_after if err.retry_after is not None else 90.0
        elif err.status == 503:
            hinted = err.retry_after
            if hinted is None:
                seconds = err.details.get("retryAfterSeconds") if err.details else None
                hinted = float(seconds) if isinstance(seconds, (int, float)) else None
            delay = max(hinted or 0.0, self._backoff(attempt))
        elif idempotent and err.status in (500, 502, 504):
            delay = self._backoff(attempt)
        else:
            return None
        return min(delay, self.max_retry_wait)

    @staticmethod
    def _to_error(resp: httpx.Response, method: str, path: str) -> ApiError:
        code = f"HTTP_{resp.status_code}"
        message = resp.reason_phrase or "error"
        details: Dict[str, Any] = {}
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            env = body.get("error")
            if isinstance(env, dict):
                code = str(env.get("code") or code)
                message = str(env.get("message") or message)
                if isinstance(env.get("details"), dict):
                    details = env["details"]
            elif isinstance(env, str):
                message = env
                if isinstance(body.get("code"), str):
                    code = body["code"]
        elif resp.text:
            message = resp.text.strip()[:200]
        return ApiError(
            status=resp.status_code,
            code=code,
            message=message,
            details=details,
            retry_after=parse_retry_after(resp.headers.get("Retry-After")),
            method=method,
            path=path,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        retry_unsafe: bool = False,
    ) -> Any:
        """Send one request with rate limiting and retries; return the decoded JSON body.

        ``retry_unsafe`` lets a non-GET request that is safe to repeat (such as minting
        a realtime token) also retry on network errors and 5xx responses.
        """
        method = method.upper()
        limiter = self.read_limiter if method in READ_METHODS else self.write_limiter
        idempotent = method in READ_METHODS or retry_unsafe
        query = _clean_params(params)
        attempt = 0
        while True:
            limiter.acquire()
            self.requests_sent += 1
            try:
                resp = self._http.request(method, path, params=query, json=json)
            except httpx.TransportError as exc:
                if idempotent and attempt < self.max_retries:
                    delay = self._backoff(attempt)
                    attempt += 1
                    log.warning("%s %s failed (%s); retry %d in %.1fs", method, path, exc, attempt, delay)
                    self._sleep(delay)
                    continue
                raise NetworkError(f"{method} {path}: {exc}") from exc

            if 200 <= resp.status_code < 300:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ApiError(resp.status_code, "INVALID_RESPONSE", "Response was not JSON", method=method, path=path) from exc

            err = self._to_error(resp, method, path)
            delay = self._retry_delay(err, attempt, idempotent)
            if delay is None:
                raise err
            attempt += 1
            log.warning("%s; retry %d/%d in %.1fs", err, attempt, self.max_retries, delay)
            if err.status == 429:
                limiter.pause(delay)  # the next acquire() waits it out for every caller
            else:
                self._sleep(delay)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def iter_cursor(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        items_key: str = "data",
        max_items: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> Iterator[JSON]:
        """Yield items across cursor-paginated pages (``pagination.nextCursor``)."""
        query = dict(params or {})
        seen = set()
        pages = 0
        count = 0
        while True:
            page = self.request("GET", path, params=query) or {}
            pages += 1
            for item in page.get(items_key) or []:
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            pagination = page.get("pagination") or {}
            cursor = pagination.get("nextCursor")
            if not pagination.get("hasMore") or not cursor or cursor in seen:
                return
            if max_pages is not None and pages >= max_pages:
                return
            seen.add(cursor)
            query["cursor"] = cursor

    def iter_offset(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        items_key: str = "data",
        page_size: int = 100,
        max_items: Optional[int] = None,
    ) -> Iterator[JSON]:
        """Yield items across offset-paginated pages (tournaments, leaderboards)."""
        query = dict(params or {})
        offset = int(query.pop("offset", 0) or 0)
        query["limit"] = page_size
        count = 0
        while True:
            query["offset"] = offset
            page = self.request("GET", path, params=query) or {}
            items = page.get(items_key) or []
            for item in items:
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            if not items:
                return
            offset += len(items)
            pagination = page.get("pagination")
            if isinstance(pagination, dict) and "hasMore" in pagination:
                if not pagination["hasMore"]:
                    return
            else:
                total = page.get("total")
                if not isinstance(total, int) or offset >= total:
                    return

    # ------------------------------------------------------------------ account

    def get_account(self) -> JSON:
        """``GET /account`` — profile and balance (org default tournament for org keys)."""
        return self.request("GET", "/account")

    # ------------------------------------------------------------------ tournaments

    def list_tournaments(
        self, status: str = "any", limit: int = 50, offset: int = 0, market_id: Optional[Id] = None
    ) -> JSON:
        return self.request(
            "GET", "/tournaments", params={"status": status, "limit": limit, "offset": offset, "marketId": market_id}
        )

    def iter_tournaments(self, status: str = "any", market_id: Optional[Id] = None) -> Iterator[JSON]:
        return self.iter_offset("/tournaments", {"status": status, "marketId": market_id})

    def get_tournament(self, slug: str) -> JSON:
        """``GET /tournaments/{slug}`` — the response ``id`` is the ``tournamentId`` UUID."""
        return self.request("GET", f"/tournaments/{_seg(slug)}")

    def list_tournament_markets(
        self,
        slug: str,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
        search: Optional[str] = None,
        status: str = "any",
        market_group_id: Optional[str] = None,
    ) -> JSON:
        return self.request(
            "GET",
            f"/tournaments/{_seg(slug)}/markets",
            params={"limit": limit, "cursor": cursor, "search": search, "status": status, "marketGroupId": market_group_id},
        )

    def iter_tournament_markets(
        self, slug: str, *, status: str = "any", search: Optional[str] = None, max_items: Optional[int] = None
    ) -> Iterator[JSON]:
        return self.iter_cursor(
            f"/tournaments/{_seg(slug)}/markets",
            {"limit": 100, "status": status, "search": search},
            max_items=max_items,
        )

    def get_tournament_market(self, slug: str, market_id: Id) -> JSON:
        """Market detail with tournament prices and the tournament orderbook embedded."""
        return self.request("GET", f"/tournaments/{_seg(slug)}/markets/{_seg(market_id)}")

    def get_tournament_leaderboard(
        self,
        slug: str,
        *,
        period: str = "all",
        limit: int = 50,
        offset: int = 0,
        group_id: Optional[str] = None,
        season: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> JSON:
        return self.request(
            "GET",
            f"/tournaments/{_seg(slug)}/leaderboard",
            params={"period": period, "limit": limit, "offset": offset, "groupId": group_id, "season": season, "sort": sort},
        )

    def get_tournament_seasons(self, slug: str) -> JSON:
        return self.request("GET", f"/tournaments/{_seg(slug)}/seasons")

    def get_tournament_positions(self, slug: str) -> JSON:
        return self.request("GET", f"/tournaments/{_seg(slug)}/portfolio/positions")

    def get_tournament_pnl(self, slug: str, period: str = "all") -> JSON:
        return self.request("GET", f"/tournaments/{_seg(slug)}/portfolio/pnl", params={"period": period})

    def iter_tournament_fills(
        self, slug: str, *, exchange_id: Optional[Id] = None, market_id: Optional[Id] = None, max_items: Optional[int] = None
    ) -> Iterator[JSON]:
        return self.iter_cursor(
            f"/tournaments/{_seg(slug)}/portfolio/fills",
            {"limit": 200, "exchangeId": exchange_id, "marketId": market_id},
            max_items=max_items,
        )

    # ------------------------------------------------------------------ markets

    def list_markets(
        self,
        *,
        limit: int = 100,
        cursor: Optional[str] = None,
        tournament_id: Optional[str] = None,
        search: Optional[str] = None,
        sort: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        ids: Optional[Sequence[Id]] = None,
        is_composite: Optional[bool] = None,
        is_multi_outcome: Optional[bool] = None,
    ) -> JSON:
        return self.request("GET", "/markets", params=self._market_query(
            limit, cursor, tournament_id, search, sort, status, category, ids, is_composite, is_multi_outcome
        ))

    def iter_markets(
        self,
        *,
        tournament_id: Optional[str] = None,
        search: Optional[str] = None,
        sort: Optional[str] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        ids: Optional[Sequence[Id]] = None,
        is_composite: Optional[bool] = None,
        is_multi_outcome: Optional[bool] = None,
        max_items: Optional[int] = None,
    ) -> Iterator[JSON]:
        query = self._market_query(
            100, None, tournament_id, search, sort, status, category, ids, is_composite, is_multi_outcome
        )
        return self.iter_cursor("/markets", query, max_items=max_items)

    @staticmethod
    def _market_query(
        limit: int,
        cursor: Optional[str],
        tournament_id: Optional[str],
        search: Optional[str],
        sort: Optional[str],
        status: Optional[str],
        category: Optional[str],
        ids: Optional[Sequence[Id]],
        is_composite: Optional[bool],
        is_multi_outcome: Optional[bool],
    ) -> Dict[str, Any]:
        return {
            "limit": limit,
            "cursor": cursor,
            "tournamentId": tournament_id,
            "search": search,
            "sort": sort,
            "status": status,
            "category": category,
            "ids": list(ids) if ids else None,
            "is_composite": is_composite,
            "is_multi_outcome": is_multi_outcome,
        }

    def get_market(self, market_id: Id, tournament_id: Optional[str] = None) -> JSON:
        return self.request("GET", f"/markets/{_seg(market_id)}", params={"tournamentId": tournament_id})

    def get_market_orderbook(
        self, market_id: Id, tournament_id: Optional[str] = None, depth: Optional[int] = None
    ) -> JSON:
        """YES-perspective books for every exchange in the market, plus ``overround``."""
        return self.request(
            "GET", f"/markets/{_seg(market_id)}/orderbook", params={"tournamentId": tournament_id, "depth": depth}
        )

    def get_market_nodes(self, market_id: Id, tournament_id: Optional[str] = None) -> JSON:
        return self.request("GET", f"/markets/{_seg(market_id)}/nodes", params={"tournamentId": tournament_id})

    # ------------------------------------------------------------------ exchanges

    def list_exchanges(
        self,
        *,
        market_id: Optional[Id] = None,
        ids: Optional[Sequence[Id]] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
        tournament_id: Optional[str] = None,
    ) -> JSON:
        if market_id is not None and ids:
            raise ValueError("market_id and ids are mutually exclusive")
        if ids and len(ids) > EXCHANGE_LIST_MAX_IDS:
            raise ValueError(f"at most {EXCHANGE_LIST_MAX_IDS} exchange ids per request")
        return self.request(
            "GET",
            "/exchanges",
            params={"marketId": market_id, "ids": list(ids) if ids else None, "limit": limit, "cursor": cursor, "tournamentId": tournament_id},
        )

    def get_exchange_price(self, exchange_id: Id, tournament_id: Optional[str] = None) -> JSON:
        return self.request("GET", f"/exchanges/{_seg(exchange_id)}/price", params={"tournamentId": tournament_id})

    def get_exchange_orderbook(
        self, exchange_id: Id, depth: Optional[int] = None, tournament_id: Optional[str] = None
    ) -> JSON:
        return self.request(
            "GET", f"/exchanges/{_seg(exchange_id)}/orderbook", params={"depth": depth, "tournamentId": tournament_id}
        )

    def get_price_history(
        self,
        exchange_id: Id,
        *,
        tournament_id: Optional[str] = None,
        resolution: str = "1h",
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> JSON:
        """OHLCV candles. Without ``start`` the newest ``limit`` candles are returned."""
        return self.request(
            "GET",
            f"/exchanges/{_seg(exchange_id)}/price-history",
            params={"tournamentId": tournament_id, "resolution": resolution, "from": start, "to": end, "limit": limit},
        )

    def get_trades(
        self,
        exchange_id: Id,
        *,
        tournament_id: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> JSON:
        """One page of the trade tape, newest first."""
        return self.request(
            "GET",
            f"/exchanges/{_seg(exchange_id)}/trades",
            params={"tournamentId": tournament_id, "from": start, "to": end, "limit": limit, "cursor": cursor},
        )

    def iter_trades(
        self,
        exchange_id: Id,
        *,
        tournament_id: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        max_items: Optional[int] = None,
    ) -> Iterator[JSON]:
        return self.iter_cursor(
            f"/exchanges/{_seg(exchange_id)}/trades",
            {"tournamentId": tournament_id, "from": start, "to": end, "limit": 200},
            max_items=max_items,
        )

    def get_prices(self, exchange_ids: Iterable[Id], tournament_id: Optional[str] = None) -> JSON:
        """Bulk price snapshot for any number of exchanges (chunked to 100 per request).

        Returns ``{"data": [...], "missingIds": [...]}`` in request order.
        """
        ids: List[str] = []
        seen = set()
        for raw in exchange_ids:
            text = str(raw)
            if text not in seen:
                seen.add(text)
                ids.append(text)
        data: List[JSON] = []
        missing: List[str] = []
        for start in range(0, len(ids), BULK_PRICE_MAX_IDS):
            chunk = ids[start : start + BULK_PRICE_MAX_IDS]
            page = self.request("GET", "/exchanges/prices", params={"ids": chunk, "tournamentId": tournament_id}) or {}
            data.extend(page.get("data") or [])
            missing.extend(str(m) for m in page.get("missingIds") or [])
        return {"data": data, "missingIds": missing}

    # ------------------------------------------------------------------ leaderboards

    def get_leaderboard(
        self,
        *,
        tournament_slug: Optional[str] = None,
        group_id: Optional[str] = None,
        period: str = "all",
        sort: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSON:
        return self.request(
            "GET",
            "/leaderboards",
            params={"tournamentSlug": tournament_slug, "groupId": group_id, "period": period, "sort": sort, "limit": limit, "offset": offset},
        )

    # ------------------------------------------------------------------ relationships (ALL)

    def list_relationships(
        self,
        *,
        market_id: Optional[Id] = None,
        exchange_id: Optional[Id] = None,
        type: Optional[str] = None,
        depth: Optional[int] = None,
        limit: int = 200,
        cursor: Optional[str] = None,
        tournament_id: Optional[str] = None,
    ) -> JSON:
        if market_id is not None and exchange_id is not None:
            raise ValueError("market_id and exchange_id are mutually exclusive")
        return self.request(
            "GET",
            "/relationships",
            params={"marketId": market_id, "exchangeId": exchange_id, "type": type, "depth": depth, "limit": limit, "cursor": cursor, "tournamentId": tournament_id},
        )

    def get_relationship_graph(self, market_id: Id, depth: Optional[int] = None, tournament_id: Optional[str] = None) -> JSON:
        return self.request(
            "GET", "/relationships/graph", params={"marketId": market_id, "depth": depth, "tournamentId": tournament_id}
        )

    def get_relationship_constraints(
        self,
        *,
        market_id: Optional[Id] = None,
        relationship_id: Optional[str] = None,
        violations_only: Optional[bool] = None,
        min_violation: Optional[float] = None,
        tournament_id: Optional[str] = None,
    ) -> JSON:
        """Engine-evaluated price constraints (satisfied / violated / unevaluable)."""
        return self.request(
            "GET",
            "/relationships/constraints",
            params={
                "marketId": market_id,
                "relationshipId": relationship_id,
                "violationsOnly": violations_only,
                "minViolation": min_violation,
                "tournamentId": tournament_id,
            },
        )

    # ------------------------------------------------------------------ portfolio (default context)

    def get_positions(self) -> JSON:
        return self.request("GET", "/portfolio/positions")

    def get_pnl(self, period: str = "all") -> JSON:
        return self.request("GET", "/portfolio/pnl", params={"period": period})

    # ------------------------------------------------------------------ realtime

    def mint_realtime_token(self) -> JSON:
        """``POST /realtime/token`` — counts as one *write*. Tokens last 3 hours."""
        return self.request("POST", "/realtime/token", retry_unsafe=True)
