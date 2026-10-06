"""Runtime enforcement of the read-only rule (docs/PAPER_TRADING.md §0 and "Design decisions" D20).

Every HTTP client the simulator uses goes through one of these guards, so a write cannot be sent even
by mistake: the guard runs as an httpx *request event hook*, i.e. after the request is built and
before any byte leaves the process, for ``.get``, ``.post``, ``.request``, ``.send``, ``.stream`` alike.
Using a hook (not a wrapping transport) keeps httpx's proxy handling from the environment intact.

* :func:`enforce_get_only` installs the guard on the Super Market client built by the dashboard,
  ``paper`` and ``fairvalue`` commands (``web.build_live`` / ``web.build_demo``). The ``stream`` command
  keeps its own client: it must mint a realtime token (a POST) and is not part of the simulator.
* :func:`outside_client` is the only way the fair-value providers create their ``httpx.Client`` for
  Polymarket and Kalshi: GET/HEAD only, no redirects, and no ``Authorization`` / ``Cookie`` header and
  no registered secret (the Super Market API key) anywhere in the request.

The opt-in ``--llm`` attribution judge sends a POST to Anthropic through its own SDK; it is outside
this rule (it is not a trading venue and is off by default).

Implemented by the architect; package E owns bug fixes.
"""

from __future__ import annotations

from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, Callable, Iterable, Optional

import httpx

from . import __version__
from .errors import redact

READ_METHODS = frozenset({"GET", "HEAD"})
OUTSIDE_FORBIDDEN_HEADERS = ("authorization", "cookie")
_HOOK_MARK = "_supermarket_readonly_guard"


class ReadOnlyViolation(RuntimeError):
    """A request that is not a plain read was about to be sent. Always a programming error."""


def _make_guard(label: str, forbid_headers: Iterable[str], check_secrets: bool) -> Callable[[httpx.Request], None]:
    forbidden = tuple(h.lower() for h in forbid_headers)

    def guard(request: httpx.Request) -> None:
        method = request.method.upper()
        if method not in READ_METHODS:
            raise ReadOnlyViolation(
                f"Blocked {method} {request.url.path} to {label}: this bot is read-only and never sends anything but GET."
            )
        for name in forbidden:
            if name in request.headers:
                raise ReadOnlyViolation(f"Blocked a request to {label} carrying a {name} header: outside reads are anonymous.")
        if check_secrets:
            url = str(request.url)
            if redact(url) != url:
                raise ReadOnlyViolation(f"Blocked a request to {label}: its URL contains a credential.")

    setattr(guard, _HOOK_MARK, True)
    return guard


def _http_of(client: Any) -> httpx.Client:
    http = getattr(client, "_http", client)  # SuperMarketClient keeps its httpx.Client in ``_http``
    if not isinstance(http, httpx.Client):
        raise TypeError("enforce_get_only needs a SuperMarketClient or an httpx.Client")
    return http


def is_guarded(client: Any) -> bool:
    """True when a read-only guard is installed on ``client`` (a SuperMarketClient or an httpx.Client)."""
    try:
        http = _http_of(client)
    except TypeError:
        return False
    return any(getattr(h, _HOOK_MARK, False) for h in http.event_hooks.get("request", []))


def enforce_get_only(client: Any, *, label: str = "the Super Market API") -> Any:
    """Install the GET/HEAD-only guard on ``client`` (idempotent) and return it. Copies made later with
    ``copy.copy`` (``tracker.single_attempt_client``) share the same httpx.Client and so the guard."""
    http = _http_of(client)
    if not is_guarded(http):
        hooks = dict(http.event_hooks)
        hooks["request"] = list(hooks.get("request", [])) + [_make_guard(label, (), False)]
        http.event_hooks = hooks
    return client


def outside_client(*, label: str, transport: Optional[httpx.BaseTransport] = None, timeout: float = 8.0,
                   base_url: str = "") -> httpx.Client:
    """An anonymous, GET-only ``httpx.Client`` for an outside venue (Polymarket, Kalshi). ``transport`` is
    for tests (``httpx.MockTransport``); without it httpx uses its default transport and the environment's
    proxy settings."""
    # Venues (Polymarket's CDN in particular) answer with Set-Cookie. A jar whose policy accepts no
    # domain never stores them, so later requests stay anonymous instead of tripping the guard.
    no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.Client(
        base_url=base_url,
        cookies=no_cookies,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        headers={"Accept": "application/json", "User-Agent": f"supermarket-bot/{__version__} (read-only)"},
        event_hooks={"request": [_make_guard(label, OUTSIDE_FORBIDDEN_HEADERS, True)]},
    )
