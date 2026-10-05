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

import dataclasses
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .models import MarketObservation, Opportunity, Quote, SignalEvent

HORIZONS: Tuple[Tuple[str, float], ...] = (("5m", 300.0), ("30m", 1800.0), ("2h", 7200.0), ("6h", 21600.0))
STUDY_KINDS = ("value", "basket")
EPISODE_GAP_S = 1800.0  # a signal absent this long starts a new event when it reappears
HORIZON_SLACK_FACTOR = 2.0  # a horizon is observed from the first quote in [t0 + h, t0 + h + this x interval]
MIN_EVENTS_FOR_INTERVAL = 20
MIN_CLUSTERS_FOR_INTERVAL = 10
MAX_PENDING = 2000  # open events kept in the state (oldest dropped first, counted in "dropped")
MAX_COMPLETED = 50000  # completed events kept in memory (oldest dropped first, counted in "dropped")
CI_LEVEL = 0.90
STATE_VERSION = 1
_EPS = 1e-9

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
        self.interval_s = float(interval_s)
        self.horizons: Tuple[Tuple[str, float], ...] = tuple((str(label), float(h)) for label, h in horizons)
        self.kinds: Tuple[str, ...] = tuple(kinds)
        self._reset()

    def _reset(self) -> None:
        self._pending: Dict[str, SignalEvent] = {}
        self._extra: Dict[str, Dict[str, Any]] = {}
        self._completed: List[SignalEvent] = []
        self._last_seen: Dict[str, float] = {}
        self._dropped = 0

    # ------------------------------------------------------------------ opening events
    def _value_event(self, now: float, opp: Opportunity, obs: MarketObservation, traded: bool) -> Optional[SignalEvent]:
        eid = str(opp.exchange_id) if opp.exchange_id is not None else None
        if eid is None:
            return None
        q = (obs.quotes or {}).get(eid)
        side = opp.side if opp.side in ("yes", "no") else "yes"
        entry = _contract_ask(q, side)
        mid = _contract_mid(q, side)
        if entry is None or mid is None:
            return None
        fv_c = _num(opp.fair_value)
        if fv_c is None:
            fv_c = _fv_contract((obs.fair_values or {}).get(eid), side)
        if fv_c is None:
            return None
        event = SignalEvent(
            event_id=f"{opp.idea_id}@{int(now)}", idea_id=str(opp.idea_id), kind="value", t0=float(now),
            exchange_ids=[eid], sides=[side], race_key=opp.race_key, direction=_direction(opp.factor_delta),
            entry=round(entry, 6), fair_value=round(fv_c, 6), gap=round(fv_c - entry, 6), traded=traded,
        )
        self._extra[event.event_id] = {"mid0": mid, "fv0": fv_c, "side": side}
        return event

    def _basket_event(self, now: float, opp: Opportunity, obs: MarketObservation, traded: bool) -> Optional[SignalEvent]:
        legs = [(str(lg.get("exchange_id")), str(lg.get("side") or opp.side)) for lg in opp.legs or ()
                if lg.get("exchange_id") is not None]
        if len(legs) < 2:
            return None
        quotes = [(obs.quotes or {}).get(eid) for eid, _ in legs]
        if any(q is None or _num(q.bid) is None or _num(q.ask) is None for q in quotes):
            return None
        asks = [_contract_ask(q, side) for q, (_, side) in zip(quotes, legs)]
        if any(a is None for a in asks):
            return None
        entry = math.fsum(a for a in asks if a is not None)
        basket_side = "no" if all(side == "no" for _, side in legs) else "yes"
        edge0 = _set_edge(quotes, basket_side)
        event = SignalEvent(
            event_id=f"{opp.idea_id}@{int(now)}", idea_id=str(opp.idea_id), kind="basket", t0=float(now),
            exchange_ids=[eid for eid, _ in legs], sides=[side for _, side in legs], race_key=opp.race_key,
            direction="N", entry=round(entry, 6), fair_value=None, gap=round(edge0, 6) if edge0 is not None else None,
            traded=traded,
        )
        self._extra[event.event_id] = {"basket_side": basket_side, "edge0": edge0}
        return event

    # ------------------------------------------------------------------ horizons
    def _observe_horizon(self, event: SignalEvent, label: str, target: float, now: float,
                         obs: MarketObservation) -> bool:
        """Fill one horizon when the first quote at or after its time is here; True when it was decided."""
        quotes = [(obs.quotes or {}).get(eid) for eid in event.exchange_ids]
        slack = HORIZON_SLACK_FACTOR * self.interval_s
        ready = all(q is not None and target - _EPS <= float(q.ts) <= target + slack + _EPS
                    and _num(q.bid) is not None and _num(q.ask) is not None for q in quotes)
        if not ready:
            if now > target + slack + _EPS:
                event.outcomes[label] = None
                event.converged[label] = None
                if event.kind == "value":
                    event.reversed[label] = None
                return True
            return False
        extra = self._extra.get(event.event_id) or {}
        if event.kind == "value":
            side = event.sides[0]
            q = quotes[0]
            bid = _contract_bid(q, side)
            mid = _contract_mid(q, side)
            event.outcomes[label] = round(bid - event.entry, 6) if bid is not None else None
            mid0, fv0 = _num(extra.get("mid0")), _num(extra.get("fv0"))
            conv: Optional[bool] = None
            rev: Optional[bool] = None
            if mid0 is not None and fv0 is not None and abs(fv0 - mid0) > _EPS:
                g0 = fv0 - mid0
                sign = 1.0 if g0 > 0 else -1.0
                if mid is not None:
                    conv = (mid - mid0) * sign >= 0.5 * abs(g0) - _EPS
                fv_now = _fv_contract((obs.fair_values or {}).get(event.exchange_ids[0]), side)
                if fv_now is not None:
                    rev = (fv0 - fv_now) * sign >= 0.5 * abs(g0) - _EPS
            event.converged[label] = conv
            event.reversed[label] = rev
            return True
        basket_side = str(extra.get("basket_side") or "no")
        if basket_side == "no":
            exit_value = math.fsum(1.0 - float(q.ask) for q in quotes)  # type: ignore[union-attr]
        else:
            exit_value = math.fsum(float(q.bid) for q in quotes)  # type: ignore[union-attr]
        event.outcomes[label] = round(exit_value - event.entry, 6)
        edge0 = _num(extra.get("edge0"))
        edge_now = _set_edge(quotes, basket_side)
        event.converged[label] = (edge_now < 0.5 * edge0 - _EPS) if edge0 is not None and edge_now is not None else None
        return True

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
        now = float(now)
        traded = {str(i) for i in traded_ids or ()}
        seen_now: Set[str] = set()
        for opp in signals or ():
            if opp.kind not in self.kinds or not opp.idea_id:
                continue
            idea = str(opp.idea_id)
            if idea in seen_now:
                continue
            seen_now.add(idea)
            last = self._last_seen.get(idea)
            if last is not None and now - last <= EPISODE_GAP_S + _EPS:
                self._last_seen[idea] = now  # the episode goes on
                continue
            event = (self._value_event(now, opp, obs, idea in traded) if opp.kind == "value"
                     else self._basket_event(now, opp, obs, idea in traded))
            if event is None:
                continue  # not measurable now (no quote / fair value): the episode starts when it is
            self._last_seen[idea] = now
            if event.event_id in self._pending:
                continue
            self._pending[event.event_id] = event
        if len(self._pending) > MAX_PENDING:
            ordered = sorted(self._pending.values(), key=lambda e: (e.t0, e.event_id))
            for e in ordered[: len(self._pending) - MAX_PENDING]:
                self._pending.pop(e.event_id, None)
                self._extra.pop(e.event_id, None)
                self._dropped += 1
        done: List[SignalEvent] = []
        for event in sorted(self._pending.values(), key=lambda e: (e.t0, e.event_id)):
            if event.idea_id in traded:
                event.traded = True
            for label, h in self.horizons:
                if label in event.outcomes:
                    continue
                target = event.t0 + h
                if now < target - _EPS:
                    continue
                self._observe_horizon(event, label, target, now, obs)
            if all(label in event.outcomes for label, _ in self.horizons):
                event.done = True
                done.append(event)
        for event in done:
            self._pending.pop(event.event_id, None)
            self._extra.pop(event.event_id, None)
            self._completed.append(event)
        if len(self._completed) > MAX_COMPLETED:
            extra = len(self._completed) - MAX_COMPLETED
            self._completed = self._completed[extra:]
            self._dropped += extra
        cutoff = now - EPISODE_GAP_S
        if len(self._last_seen) > 4 * MAX_PENDING:
            self._last_seen = {k: v for k, v in self._last_seen.items() if v >= cutoff - _EPS}
        return done

    # ------------------------------------------------------------------ reading
    def summary(self) -> Dict[str, Any]:
        """``{"kinds": {kind: {"events", "traded", "horizons": {label: {"n", "mean", "ci_low", "ci_high",
        "ci_level", "clusters", "share_positive", "share_converged", "share_reversed"}}}}, "pending",
        "dropped", "can_show": CAN_SHOW, "cannot_show": CANNOT_SHOW}``. Intervals use
        paper.cluster_t_interval with clusters (race_key, int(t0 // 3600)) at 90%; None below
        MIN_EVENTS_FOR_INTERVAL events or MIN_CLUSTERS_FOR_INTERVAL clusters."""
        from .paper import cluster_t_interval

        events = list(self._completed) + sorted(self._pending.values(), key=lambda e: (e.t0, e.event_id))
        kinds: Dict[str, Any] = {}
        for kind in self.kinds:
            evs = [e for e in events if e.kind == kind]
            horizons: Dict[str, Any] = {}
            for label, _ in self.horizons:
                rows = [e for e in evs if e.outcomes.get(label) is not None]
                values = [float(e.outcomes[label]) for e in rows]  # type: ignore[arg-type]
                n = len(values)
                a = [e.race_key or f"idea:{e.idea_id}" for e in rows]
                b = [int(e.t0 // 3600) for e in rows]
                clusters = min(len(set(a)), len(set(b))) if n else 0
                lo = hi = None
                if n >= MIN_EVENTS_FOR_INTERVAL and clusters >= MIN_CLUSTERS_FOR_INTERVAL:
                    ci = cluster_t_interval(values, a, b, level=CI_LEVEL)
                    if ci is not None:
                        lo, hi = round(ci[1], 6), round(ci[2], 6)
                conv = [e.converged.get(label) for e in evs if e.converged.get(label) is not None]
                rev = [e.reversed.get(label) for e in evs if e.reversed.get(label) is not None]
                horizons[label] = {
                    "n": n, "mean": round(math.fsum(values) / n, 6) if n else None, "ci_low": lo, "ci_high": hi,
                    "ci_level": CI_LEVEL, "clusters": clusters,
                    "share_positive": round(sum(1 for v in values if v > _EPS) / n, 6) if n else None,
                    "share_converged": round(sum(1 for c in conv if c) / len(conv), 6) if conv else None,
                    "share_reversed": round(sum(1 for r in rev if r) / len(rev), 6) if rev else None,
                }
            kinds[kind] = {"events": len(evs), "traded": sum(1 for e in evs if e.traded), "horizons": horizons}
        return {"kinds": kinds, "pending": len(self._pending), "dropped": self._dropped, "can_show": CAN_SHOW,
                "cannot_show": CANNOT_SHOW}

    def completed(self) -> List[SignalEvent]:
        return list(self._completed)

    def pending(self) -> List[SignalEvent]:
        return sorted(self._pending.values(), key=lambda e: (e.t0, e.event_id))

    def state(self) -> Dict[str, Any]:
        """Pending events and episode bookkeeping (completed events are persisted by the engine via
        PaperPersistence.paper_put_events and reloaded with ``load``)."""
        return {
            "version": STATE_VERSION, "interval_s": self.interval_s,
            "pending": [e.to_dict() for e in self.pending()],
            "extra": {k: dict(v) for k, v in sorted(self._extra.items())},
            "last_seen": dict(sorted(self._last_seen.items())), "dropped": self._dropped,
        }

    def load(self, state: Optional[Dict[str, Any]], completed: Sequence[Dict[str, Any]] = ()) -> None:
        self._reset()
        st = dict(state or {})
        if st.get("interval_s") is not None:
            self.interval_s = float(st["interval_s"])
        for d in st.get("pending") or []:
            e = _event_from(d)
            if e is not None:
                self._pending[e.event_id] = e
        self._extra = {str(k): dict(v) for k, v in (st.get("extra") or {}).items() if str(k) in self._pending}
        self._last_seen = {str(k): float(v) for k, v in (st.get("last_seen") or {}).items()}
        self._dropped = int(st.get("dropped") or 0)
        rows = [_event_from(d) for d in completed or ()]
        self._completed = sorted([e for e in rows if e is not None], key=lambda e: (e.t0, e.event_id))


# --------------------------------------------------------------------------- helpers


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _contract_ask(q: Optional[Quote], side: str) -> Optional[float]:
    if q is None:
        return None
    if side == "yes":
        a = _num(q.ask)
        return a if a is not None and 0 < a < 1 else None
    b = _num(q.bid)
    return round(1.0 - b, 6) if b is not None and 0 < b < 1 else None


def _contract_bid(q: Optional[Quote], side: str) -> Optional[float]:
    if q is None:
        return None
    if side == "yes":
        b = _num(q.bid)
        return b if b is not None and 0 <= b < 1 else None
    a = _num(q.ask)
    return round(1.0 - a, 6) if a is not None and 0 < a <= 1 else None


def _contract_mid(q: Optional[Quote], side: str) -> Optional[float]:
    if q is None:
        return None
    b, a = _num(q.bid), _num(q.ask)
    if b is None or a is None:
        return None
    mid = (b + a) / 2.0
    return mid if side == "yes" else 1.0 - mid


def _set_edge(quotes: Sequence[Optional[Quote]], basket_side: str) -> Optional[float]:
    """The set's touch edge: NO basket sum(YES bids) - 1, YES basket 1 - sum(YES asks)."""
    if any(q is None for q in quotes):
        return None
    if basket_side == "no":
        bids = [_num(q.bid) for q in quotes]  # type: ignore[union-attr]
        if any(b is None for b in bids):
            return None
        return math.fsum(b for b in bids if b is not None) - 1.0
    asks = [_num(q.ask) for q in quotes]  # type: ignore[union-attr]
    if any(a is None for a in asks):
        return None
    return 1.0 - math.fsum(a for a in asks if a is not None)


def _fv_contract(fv: Any, side: str) -> Optional[float]:
    if fv is None:
        return None
    v = _num(getattr(fv, "value", None))
    if v is None:
        return None
    return v if side == "yes" else 1.0 - v


def _direction(delta: Any) -> str:
    d = _num(delta)
    if d is None or abs(d) <= 1e-12:
        return "N"
    return "D" if d > 0 else "R"


def _event_from(data: Mapping[str, Any]) -> Optional[SignalEvent]:
    if not data:
        return None
    names = {f.name for f in dataclasses.fields(SignalEvent)}
    try:
        e = SignalEvent(**{k: v for k, v in data.items() if k in names})
    except TypeError:
        return None
    e.outcomes = {str(k): (None if v is None else float(v)) for k, v in (e.outcomes or {}).items()}
    e.converged = {str(k): (None if v is None else bool(v)) for k, v in (e.converged or {}).items()}
    e.reversed = {str(k): (None if v is None else bool(v)) for k, v in (e.reversed or {}).items()}
    e.exchange_ids = [str(x) for x in e.exchange_ids or []]
    e.sides = [str(x) for x in e.sides or []]
    return e
