"""Background market tracker: snapshots → SQLite → surge/high-band analytics → attribution.

See docs/DESIGN.md (Tracker section).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from .bot import Context
from .client import SuperMarketClient
from .store import TrackerStore


class Tracker:
    def __init__(
        self,
        client: SuperMarketClient,
        context: Context,
        store: TrackerStore,
        *,
        interval: float = 30.0,
        market_refresh: float = 300.0,
        backfill: bool = True,
        attributor: Any = None,
        analyze: bool = True,
        backfill_reads_per_min: int = 30,
        analyze_reads_per_min: int = 20,
        context_refresh: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raise NotImplementedError

    def run_once(self) -> Dict[str, Any]:
        raise NotImplementedError

    def backfill_step(self, max_reads: int = 1) -> int:
        """Backfill candles for up to ``max_reads`` exchanges (synchronous; used by the worker and tests)."""
        raise NotImplementedError

    def analyze_pending(self, max_items: int = 1) -> int:
        """Run attribution for up to ``max_items`` queued surges (synchronous)."""
        raise NotImplementedError

    def request_analysis(self, surge_id: int) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self, timeout: float = 10.0) -> None:
        raise NotImplementedError

    def view(self) -> Dict[str, Any]:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        raise NotImplementedError
