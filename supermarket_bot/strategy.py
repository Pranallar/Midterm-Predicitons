"""Read-only trade ideas: fade participant surges, carry high-90s favourites, arbitrage.

See docs/DESIGN.md (Strategy section). Nothing here places orders.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .models import BacktestResult, ExchangeInfo, HighBand, Opportunity, PricePoint, StrategyReport, Surge

DEFAULT_INITIAL_BALANCE = 100_000.0
DEFAULT_CUP_END_ISO = "2026-11-04T17:00:00Z"  # noon ET, Nov 4 2026 (EST)


def cup_end_ts(tournament: Optional[Mapping[str, Any]] = None) -> float:
    """Tournament ``endDate`` if present, else SUPERMARKET_CUP_END, else DEFAULT_CUP_END_ISO."""
    raise NotImplementedError


def kelly_fraction(p_win: float, price: float) -> float:
    raise NotImplementedError


def size_position(p_win: float, price: float, balance: float, kelly_mult: float = 0.25,
                  max_position_pct: float = 0.08, depth_limit: Optional[float] = None) -> int:
    raise NotImplementedError


def risk_mode(balance: Optional[float], initial: Optional[float], leader_value: Optional[float],
              my_rank: Optional[int], days_left: Optional[float]) -> str:
    raise NotImplementedError


def fade_opportunity(surge: Surge, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                     balance: float, backtest: Optional[BacktestResult] = None) -> Optional[Opportunity]:
    raise NotImplementedError


def carry_opportunity(band: HighBand, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                      cup_end: float, balance: float) -> Optional[Opportunity]:
    raise NotImplementedError


def watch_opportunity(surge: Surge, info: Optional[ExchangeInfo]) -> Optional[Opportunity]:
    raise NotImplementedError


def arbitrage_opportunities(constraints: Optional[Mapping[str, Any]], overround_rows: Sequence[Mapping[str, Any]],
                            balance: float) -> List[Opportunity]:
    raise NotImplementedError


def backtest_fade(series_by_exchange: Mapping[str, Sequence[PricePoint]], horizon_s: float = 6 * 3600.0,
                  step_s: float = 300.0, market_of: Optional[Mapping[str, str]] = None) -> BacktestResult:
    raise NotImplementedError


def build_report(*, now: float, surges: Sequence[Surge], bands: Sequence[HighBand],
                 latest: Mapping[str, PricePoint], infos: Mapping[str, ExchangeInfo],
                 balance: Optional[float], initial_balance: Optional[float], leader_value: Optional[float],
                 my_rank: Optional[int], cup_end: float, constraints: Optional[Mapping[str, Any]] = None,
                 overround_rows: Sequence[Mapping[str, Any]] = (), backtest: Optional[BacktestResult] = None) -> StrategyReport:
    raise NotImplementedError
