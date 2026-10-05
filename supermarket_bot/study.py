"""Signal-level event study (package C of docs/PAPER_TRADING.md §6.14).

A one-day portfolio verdict is almost always "not enough evidence": a handful of closed trades that
share one national factor. This module measures the signals themselves instead. Every ``value`` and
``basket`` signal (traded or not) opens an event at its first appearance in an episode; the event
records what buying at the decision-time touch would be worth, sold at the touch, after +5 min,
+30 min, +2 h and +6 h (net of the spread by construction), whether the gap closed because the CUP
moved toward the outside fair value ("converged") or because the OUTSIDE value moved toward the Cup
("reversed": our fair value was stale), and summarises them per kind and horizon with a two-way
cluster-robust interval (races x entry hour). That gives hundreds of observations a day and answers
the question the user has before acting: how fast, and how often, do these gaps really close?

It uses only the bulk quotes and fair values already in each MarketObservation: no extra reads.
The engine calls :meth:`SignalStudy.observe` once per step; the backtest gets it for free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .models import MarketObservation, Opportunity, SignalEvent

HORIZONS: Tuple[Tuple[str, float], ...] = (("5m", 300.0), ("30m", 1800.0), ("2h", 7200.0), ("6h", 21600.0))
STUDY_KINDS = ("value", "basket")
EPISODE_GAP_S = 1800.0  # a signal absent this long starts a new event when it reappears
HORIZON_SLACK_FACTOR = 2.0  # a horizon is observed from the first quote in [t0 + h, t0 + h + this x interval]
MIN_EVENTS_FOR_INTERVAL = 20
MIN_CLUSTERS_FOR_INTERVAL = 10
MAX_PENDING = 2000  # open events kept in the state (oldest dropped first, counted in "dropped")

# Shown next to the portfolio verdict (exact texts, §11).
CAN_SHOW = (
    "What one day can show: how often, and how fast, Cup prices move toward the outside fair value after a "
    "signal, net of the spread, over hundreds of signals."
)
CANNOT_SHOW = (
    "What it cannot show: whether the outside fair value is right about who wins (that pays only when races "
    "are decided, November 3-4), how prices behave on election night, or how the Cup's end-of-tournament "
    "settlement will work."
)


class SignalStudy:
    """Event study state. ``interval_s`` is the step interval (horizon slack). Deterministic."""

    def __init__(self, *, interval_s: float = 30.0, horizons: Sequence[Tuple[str, float]] = HORIZONS,
                 kinds: Sequence[str] = STUDY_KINDS) -> None:
        raise NotImplementedError

    def observe(self, now: float, obs: MarketObservation, signals: Sequence[Opportunity],
                traded_ids: Set[str]) -> List[SignalEvent]:
        """Open events for new episodes of value/basket signals at ``now`` (entry = the contract ask from
        ``obs.quotes``: YES ask, or 1 - YES bid; baskets: the sum over legs); fill every pending horizon
        whose first quote at or after t0 + h is in ``obs.quotes`` (value: contract bid - entry; NO basket:
        sum(1 - YES ask_i) - entry; YES basket: sum(YES bid_i) - entry); set ``converged`` (value: the Cup
        contract mid moved at least half the original gap toward fv0; basket: the set edge at the touch fell
        below half its entry value) and ``reversed`` (value: the outside fair value moved toward the Cup's
        t0 mid by at least half the gap); mark ``traded`` from ``traded_ids``. Returns events completed
        in this call (every horizon observed or expired)."""
        raise NotImplementedError

    def summary(self) -> Dict[str, Any]:
        """``{"kinds": {kind: {"events", "traded", "horizons": {label: {"n", "mean", "ci_low", "ci_high",
        "ci_level", "clusters", "share_positive", "share_converged", "share_reversed"}}}}, "pending",
        "dropped", "can_show": CAN_SHOW, "cannot_show": CANNOT_SHOW}``. Intervals use
        paper.cluster_t_interval with clusters (race_key, int(t0 // 3600)) at 90%; None below
        MIN_EVENTS_FOR_INTERVAL events or MIN_CLUSTERS_FOR_INTERVAL clusters."""
        raise NotImplementedError

    def completed(self) -> List[SignalEvent]:
        raise NotImplementedError

    def state(self) -> Dict[str, Any]:
        """Pending events and episode bookkeeping (completed events are persisted by the engine via
        PaperPersistence.paper_put_events and reloaded with ``load``)."""
        raise NotImplementedError

    def load(self, state: Optional[Dict[str, Any]], completed: Sequence[Dict[str, Any]] = ()) -> None:
        raise NotImplementedError
