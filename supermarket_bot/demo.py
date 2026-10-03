"""Simulated Super Market tournament for ``dashboard --demo`` and offline tests.

See docs/DESIGN.md (Demo section).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

import httpx

from .models import Article
from .news import NewsProvider

DEMO_SLUG = "predictions-cup-demo"


class DemoMarket:
    def __init__(self, seed: int = 7, now: Optional[float] = None, clock: Callable[[], float] = time.time) -> None:
        raise NotImplementedError

    def transport(self) -> httpx.MockTransport:
        raise NotImplementedError


class DemoNewsProvider(NewsProvider):
    name = "demo-news"
    min_interval_s = 0.0

    def __init__(self, market: DemoMarket) -> None:
        raise NotImplementedError

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        raise NotImplementedError
