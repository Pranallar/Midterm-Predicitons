"""Pure price analytics: marks, changes, volatility, surges, high band, trade flow.

No I/O. All thresholds are documented in docs/DESIGN.md (Analytics section).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .models import HighBand, PricePoint, Surge, TradeFlow, TradeRecord

# window name -> (seconds, minimum absolute move)
SURGE_WINDOWS: Dict[str, Tuple[float, float]] = {
    "5m": (300.0, 0.05),
    "1h": (3600.0, 0.08),
    "6h": (21600.0, 0.10),
    "24h": (86400.0, 0.12),
}
Z_THRESHOLD = 3.0
MAX_STALENESS_S = 900.0


def mark_price(last: Optional[float], bid: Optional[float], ask: Optional[float], max_spread: float = 0.10) -> Optional[float]:
    raise NotImplementedError


def value_at(points: Sequence[PricePoint], t: float, tolerance_s: float) -> Optional[PricePoint]:
    """Latest point with ``ts <= t`` and a non-None price, if not older than ``t - tolerance_s``."""
    raise NotImplementedError


def change_over(points: Sequence[PricePoint], now: float, window_s: float) -> Optional[float]:
    raise NotImplementedError


def volatility(points: Sequence[PricePoint], end: float, lookback_s: float = 86400.0, step_s: float = 300.0) -> Optional[float]:
    """Std-dev of step changes on a forward-filled ``step_s`` grid over ``[end - lookback_s, end]``."""
    raise NotImplementedError


def detect_surges(
    points: Sequence[PricePoint],
    now: float,
    exchange_id: str,
    market_id: str,
    windows: Optional[Dict[str, Tuple[float, float]]] = None,
    z_threshold: float = Z_THRESHOLD,
) -> List[Surge]:
    """At most one Surge (the most significant qualifying window). Empty list when none."""
    raise NotImplementedError


def update_surge_status(surge: Surge, current_price: Optional[float], now: float) -> Surge:
    """Set ``current_price``, ``reverted_fraction`` and ``status`` (open/reverted/held) in place; return it."""
    raise NotImplementedError


def high_band(
    points: Sequence[PricePoint],
    now: float,
    exchange_id: str,
    market_id: str,
    threshold: float = 0.95,
    lookback_s: float = 6 * 3600.0,
    min_fraction: float = 0.8,
    settlement_date: Optional[str] = None,
) -> Optional[HighBand]:
    raise NotImplementedError


def trade_flow(trades: Sequence[TradeRecord]) -> TradeFlow:
    raise NotImplementedError


def downsample(points: Sequence[PricePoint], max_points: int) -> List[PricePoint]:
    raise NotImplementedError


def hours_until(iso_date: Optional[str], now: float) -> Optional[float]:
    """Hours from ``now`` until an ISO-8601 timestamp (None if missing/unparseable)."""
    raise NotImplementedError
