"""Shared input assembly for the Strategy view, the paper trader and headless simulations
(package E of docs/PAPER_TRADING.md §7.3).

``web.DashboardApp._build_strategy`` used to assemble the strategy's inputs itself; this module
does it once for every consumer so the live Strategy view and the paper trader see the same
inputs. Nothing here performs a read against the Super Market API: it uses what the tracker and
the store already hold.

Import rule: this module must not import ``web`` at module level (web imports pipeline).
``context_summary`` lives here now and ``web`` re-exports it under the old name.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .models import (
    LeaderboardSnapshot,
    MarketObservation,
    SettlementInfo,
    StrategyInputs,
    StrategyParams,
)

log = logging.getLogger("supermarket_bot")

SURGE_LOOKBACK_S = 2 * 86400.0
LEADERBOARD_HISTORY_S = 7 * 86400.0
RECENT_MIDS_S = 300.0  # StrategyInputs.recent_mids window


def context_summary(context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Moved verbatim from ``web.context_summary`` (web re-exports it), plus ``account_value`` (cash plus
    positions from the tracker context, codebase_map #14)."""
    raise NotImplementedError


def code_version() -> str:
    """``"<package __version__>+<12-char git commit>"`` when the package runs from a git checkout (read
    ``.git/HEAD`` and the ref file directly, no subprocess), else the package version. Part of the run
    fingerprint (§6.11): new code starts a new run."""
    raise NotImplementedError


def leaderboard_snapshot(context: Mapping[str, Any], now: float) -> Optional[LeaderboardSnapshot]:
    """The tracker context's ``leaderboard`` (``entries`` when present, else ``top``) as a snapshot."""
    raise NotImplementedError


def assemble_inputs(*, now: float, view: Mapping[str, Any], store: Any, fair_values: Any = None,
                    races: Optional[Mapping[str, Any]] = None, backtest: Any = None,
                    regime: str = "unknown", params: Optional[StrategyParams] = None,
                    cup_end: Optional[float] = None,
                    recent_mids: Optional[Mapping[str, Sequence[Any]]] = None) -> StrategyInputs:
    """StrategyInputs from the tracker's ``view()`` and the store (§7.3):

    open outcomes = the view's exchange rows; ``latest`` = the view row's bid/ask/last at its
    ``updated_at`` with ``store.book(eid)`` attached (rows flagged ``stale`` are left out of
    ``latest``, so they yield no trade ideas); surges = ``store.surges(since=now - 2 d)`` on
    open outcomes, not closed; bands from ``view["high_band"]``; balance / initial / account value /
    rank / leader value / constraints / overround from ``context_summary(view["context"])``;
    fair values from ``fair_values.current().values`` (a FairValueService, or None); races from
    ``races`` or ``fairvalue.races_for(infos)``; leaderboard + bar via ``sizing.estimate_bar`` with
    ``store.leaderboard_snapshots(now - 7 d, now)`` as history; ``recent_mids`` (the tracker's
    ``recent_mids(RECENT_MIDS_S)``, from its in-memory series cache, no SQL) as given."""
    raise NotImplementedError


def observation(*, now: float, inputs: StrategyInputs, view: Mapping[str, Any], store: Any,
                settlements: Optional[Mapping[str, SettlementInfo]] = None,
                open_ids: Optional[Sequence[str]] = None) -> MarketObservation:
    """The base MarketObservation for a paper step (no reads): quotes from the view rows (``ts`` =
    the row's ``updated_at``), books from ``store.book`` as BookObservation(source="tracker"),
    settlements from ``store.settlements()`` when not given, open ids, infos, fair values, races."""
    raise NotImplementedError


def settlements_from_market(market: Mapping[str, Any], detected_at: float) -> List[SettlementInfo]:
    """One SettlementInfo per exchange of a settled market (``GET /tournaments/{slug}/markets?status=settled``
    row): binary "YES"/"NO" -> payout 1/0; multi-outcome -> the exchange whose option (or id) equals
    ``settledWith`` pays 1, the others 0; "REFUND"/"VOID"/"CANCELLED" -> refund; anything else ->
    payout None (the engine freezes and flags it). Case-insensitive."""
    raise NotImplementedError


def run_simulation(runtime: Any, clock: Any, *, hours: float, step_s: float = 30.0,
                   summary_every_s: float = 3600.0, on_summary: Optional[Callable[[Dict[str, Any]], None]] = None,
                   backfill_reads_per_step: int = 8, analyses_per_step: int = 2,
                   end_run: bool = True) -> Dict[str, Any]:
    """Drive a demo runtime synchronously on a fake ``clock`` (demo.SimClock) for ``hours`` of
    simulated time (§7.6): each step advances the clock, then runs ``tracker.run_once()``, a few
    backfill and analysis steps, ``tracker.fair_value_step()`` (every 60 simulated s) and
    ``tracker.paper_step()``. Single-threaded and deterministic: every limiter in the runtime runs on the
    SimClock with a ``sleep`` that raises ``demo.SimClockStall`` instead of waiting (build_demo(clock=...)
    sets this up), so a budget that would need a wait fails loudly instead of depending on CPU speed.
    Calls ``on_summary(paper summary)`` every ``summary_every_s``; ends the run ("completed") when
    ``end_run``; returns the final summary."""
    raise NotImplementedError

