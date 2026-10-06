"""Integration of the outside-move watcher with the tracker, the store and the demo (docs/OUTSIDE_MOVES.md §15, §22).

A fast demo with moves (``web.build_demo(moves=True, paper=False, clock=SimClock)`` + ``pipeline.run_simulation(...,
moves_every_s=15)``) of 2 hours must produce exactly the §15.2 alerts and lag outcomes (+/-30 s on every time),
write one alerts.jsonl line per event, continue its open alerts after a short restart on the same database (and
censor them after a long one), and be deterministic: two runs give identical ``/api/moves`` bodies. Offline: the
demo's simulated API and its two scripted outside venues (no fair-value provider: the paper trader never sees
them, M12).
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from supermarket_bot import pipeline, web
from supermarket_bot.demo import MOVES_DEMO_EXCHANGE_IDS, SIM_T0, DemoOutsideVenue, SimClock
from supermarket_bot.moves import (
    MOVES_ALERTS_FILE,
    MOVES_CAVEATS,
    MOVES_DEMO_CAVEAT,
    MOVES_TRADE_NOTE,
    MoveEvent,
)

HOURS = 2.0
TOL = 30.0  # §15.2: tests allow +/-30 s on every time

# §15.2, two hours of a fast run (poll 15 s, Cup 30 s), in detection order:
# (exchange id, detected at (s after t0), status when opened, lag outcome, lag_s, resolved at (s after t0), side)
EXPECTED: List[Tuple[str, float, str, str, Optional[float], float, Optional[str]]] = [
    ("9035", 90, "lagging", "followed", 262.5, 360, "yes"),  # lead_short: the Cup follows ~4.4 min later
    ("9036", 195, "lagging", "followed", 555.0, 750, "no"),  # lead_long: Buy NO, followed ~9.3 min later
    ("9037", 285, "lagging", "not_followed", None, 3855, "yes"),  # never: not followed at c+255+3600
    ("9040", 585, "lagging", "reverted", None, 825, "yes"),  # reverts: the outside comes back first
    ("9039", 705, "moved_first", "cup_first", -225.0, 705, None),  # cup_first: Cup half-way ~c+450, outside ~c+675
    ("9040", 855, "already_moved", "excluded", None, 855, None),  # the reverts way back: converging, excluded
    ("9035", 3690, "lagging", "followed", 142.5, 3840, "no"),  # cycle 1, the other way (~2.4 min)
    ("9036", 3780, "lagging", "followed", 798.0, 4590, "yes"),  # cycle 1 (~13.3 min)
    ("9040", 4185, "lagging", "reverted", None, 4425, "no"),
    ("9039", 4305, "moved_first", "cup_first", -225.0, 4305, None),
    ("9040", 4455, "already_moved", "excluded", None, 4455, None),
    ("9037", 4545, "already_moved", "excluded", None, 4545, None),  # never's way back (converging)
]


def _build(data_dir: Path, clock: SimClock, **kw: Any) -> web.Runtime:
    return web.build_demo(data_dir, 30.0, out=io.StringIO(), news=False, clock=clock, paper=False,
                          fair_value="auto", moves=True, moves_poll_s=15.0, **kw)


def _run(runtime: web.Runtime, clock: SimClock, hours: float) -> Dict[str, Any]:
    return pipeline.run_simulation(runtime, clock, hours=hours, step_s=30.0, summary_every_s=0.0,
                                   moves_every_s=15.0, end_run=False)


def _rel(ts: Optional[float]) -> Optional[float]:
    return None if ts is None else float(ts) - SIM_T0


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def sim(tmp_path_factory: Any) -> Iterator[SimpleNamespace]:
    data_dir = tmp_path_factory.mktemp("moves-sim")
    clock = SimClock(SIM_T0)
    started = time.monotonic()
    runtime = _build(data_dir, clock)
    events: List[MoveEvent] = []
    runtime.moves.add_listener(events.append)
    try:
        _run(runtime, clock, HOURS)
        elapsed = time.monotonic() - started
        alerts = sorted(runtime.moves.alerts(), key=lambda a: (a["detected_at"], a["alert_id"]))
        yield SimpleNamespace(
            runtime=runtime, clock=clock, elapsed=elapsed, events=events, alerts=alerts,
            summary=runtime.moves.summary(), status=runtime.tracker.status(), api=runtime.app.outside_moves(),
            stored=runtime.store.move_alerts(), jsonl=_jsonl(data_dir / "demo" / MOVES_ALERTS_FILE),
            data_dir=data_dir,
        )
    finally:
        runtime.close()


# --------------------------------------------------------------------------- the 2-hour fast run (§15.2)


def test_a_two_hour_fast_run_with_moves_finishes_in_under_a_minute(sim: SimpleNamespace) -> None:
    assert sim.elapsed < 60.0, f"2 simulated hours with moves took {sim.elapsed:.1f} s"
    st = sim.status["moves"]
    assert st["enabled"] is True and st["poll_s"] == 15.0 and st["last_error"] is None
    assert st["steps"] == 481 and st["last_step_at"] == SIM_T0 + HOURS * 3600  # every 15 s from t0 to t0 + 2 h
    assert st["venues"] == {"ok": 2, "total": 2}


def test_exactly_the_scripted_alerts_and_lag_outcomes(sim: SimpleNamespace) -> None:
    assert len(sim.alerts) == len(EXPECTED) == 12
    for alert, (eid, detected, opened, outcome, lag_s, resolved, _side) in zip(sim.alerts, EXPECTED):
        where = f"{eid} detected at t0+{_rel(alert['detected_at']):.0f}"
        assert alert["exchange_id"] == eid, where
        assert _rel(alert["detected_at"]) == pytest.approx(detected, abs=TOL), where
        assert alert["opened_status"] == opened, where
        lag = alert["lag"]
        assert lag["outcome"] == outcome, where
        if lag_s is None:
            assert lag["lag_s"] is None, where
        else:
            assert lag["lag_s"] == pytest.approx(lag_s, abs=TOL), where
        assert _rel(lag["resolved_at"]) == pytest.approx(resolved, abs=TOL), where
        assert alert["state"] == "closed" and alert["demo"] is True, where
        assert alert["alert_id"] == f"mv-{eid}-{int(alert['t_base'])}"
    outcomes = [a["lag"]["outcome"] for a in sim.alerts]
    assert {o: outcomes.count(o) for o in set(outcomes)} == {
        "followed": 4, "reverted": 2, "not_followed": 1, "cup_first": 2, "excluded": 3}
    for alert in sim.alerts:
        if alert["lag"]["outcome"] == "excluded":
            assert alert["lag"]["excluded_reason"].startswith("converging") and alert["lag"]["eligible"] is False
        if alert["lag"]["outcome"] == "followed":
            assert alert["status"] == "already_moved"  # a lagging alert that followed changes status


def test_nothing_alerts_for_the_thin_spike_and_the_one_venue_move(sim: SimpleNamespace) -> None:
    alerted = {a["exchange_id"] for a in sim.alerts}
    assert not alerted & {"9038", "9041"}
    assert alerted == set(MOVES_DEMO_EXCHANGE_IDS) - {"9038", "9041"}
    suppressed = sim.summary["counts"]["suppressed_today"]
    assert suppressed["single_tick"] >= 2 and suppressed["thin"] >= 2 and suppressed["disagree"] >= 2
    reasons = {(s["exchange_id"], s["reason"]) for s in sim.summary["suppressed"]}
    assert {("9038", "single_tick"), ("9038", "thin"), ("9041", "disagree")} <= reasons


def test_lagging_alerts_carried_a_hand_trade_suggestion_when_they_opened(sim: SimpleNamespace) -> None:
    opened = {e.alert_id: e.alert for e in sim.events if e.kind == "opened"}
    assert len(opened) == 12
    for alert, (eid, _d, status, _o, _l, _r, side) in zip(sim.alerts, EXPECTED):
        at_open = opened[alert["alert_id"]]
        if status != "lagging":
            assert at_open["trade"] is None and at_open["actionable"] is False, eid
            continue
        trade = at_open["trade"]
        assert trade is not None and at_open["actionable"] is True, eid
        assert trade["side"] == side and trade["action"] == "buy", eid
        assert trade["text"] == f"Buy {side.upper()} at {trade['limit']:.3f}", eid
        assert round(trade["limit"] / 0.005) * 0.005 == pytest.approx(trade["limit"], abs=1e-9)  # on the Cup tick
        assert trade["limit"] <= trade["max_limit"] and trade["edge_after_uncertainty"] >= 0.01 - 1e-9, eid
        assert trade["note"] == MOVES_TRADE_NOTE  # "Suggestion only, not a sure thing ..."
        assert trade["shares_at_limit"] is not None and trade["book_source"] in ("read", "stored"), eid
    for alert in sim.alerts:  # closed alerts no longer suggest a trade
        assert alert["actionable"] is False


def test_the_lag_summary_is_honest_about_the_small_sample(sim: SimpleNamespace) -> None:
    lag = sim.summary["summary"]
    assert (lag["moves"], lag["excluded"], lag["resolved"], lag["pending"], lag["censored"]) == (9, 3, 9, 0, 0)
    assert (lag["outside_led"], lag["cup_first"], lag["followed"], lag["reverted"], lag["not_followed"]) == (7, 2, 4, 2, 1)
    assert lag["never_followed"] == 3 and lag["races"] == 5 and lag["small_sample"] is True
    assert lag["sentence"] == ("Only 9 outside moves on 5 races so far (4 followed by the Cup, 3 never, 2 where the Cup "
                               "moved first): far too few to say whether the Cup lags. Wait for at least 20 moves on 10 "
                               "races.")
    assert lag["capture_4m"]["n"] == 7 and lag["capture_4m"]["ci90"] is None  # fewer than 5 races: no interval
    assert list(sim.summary["caveats"]) == list(MOVES_CAVEATS) + [MOVES_DEMO_CAVEAT]


def test_every_event_is_one_alerts_jsonl_line(sim: SimpleNamespace) -> None:
    rows = sim.jsonl
    assert [(r["event"], r["alert"]["alert_id"]) for r in rows] == [(e.kind, e.alert_id) for e in sim.events]
    kinds = [r["event"] for r in rows]
    assert (kinds.count("opened"), kinds.count("status"), kinds.count("closed")) == (12, 7, 12)
    opened_ids = [r["alert"]["alert_id"] for r in rows if r["event"] == "opened"]
    assert len(set(opened_ids)) == 12  # one "opened" line per alert
    for r in rows:
        assert set(r) == {"event", "at", "at_iso", "alert"} and r["at_iso"].endswith("Z")
        assert r["at"] == r["alert"]["detected_at"] if r["event"] == "opened" else r["at"] >= r["alert"]["detected_at"]


def test_alerts_are_persisted_in_the_store_and_the_api_agrees(sim: SimpleNamespace) -> None:
    stored = {a["alert_id"]: a for a in sim.stored}
    assert set(stored) == {a["alert_id"] for a in sim.alerts}
    for alert in sim.alerts:
        assert stored[alert["alert_id"]]["lag"] == alert["lag"] and stored[alert["alert_id"]]["state"] == "closed"
    assert sim.runtime.store.outside_quotes(SIM_T0) != []  # samples persisted (on a change + the heartbeat)
    api = sim.api
    assert api["enabled"] is True and api["available"] is True and api["demo"] is True and api["error"] is None
    assert {a["alert_id"] for a in api["alerts"]} == set(stored)
    assert api["summary"]["sentence"] == sim.summary["summary"]["sentence"]
    assert api["watching"]["venues"] == 2 and api["watching"]["matched"] >= 7
    assert [v["venue"] for v in api["venues"]] == ["demo-a", "demo-b"]


def test_the_outside_venues_never_reach_the_paper_trader_or_the_book_tables(sim: SimpleNamespace) -> None:
    runtime = sim.runtime
    providers = list(getattr(runtime.fair_values, "providers", []) or [])
    assert providers and not any(isinstance(p, DemoOutsideVenue) for p in providers)  # M12
    assert runtime.tracker.paper_runner is None and runtime.tracker.paper_view() is None
    for eid in MOVES_DEMO_EXCHANGE_IDS:  # the watcher's Cup book reads are not stored (M12)
        assert not [b for b in runtime.store.book_snapshots(eid, 0.0) if b.get("source") == "moves"]


# --------------------------------------------------------------------------- determinism (D42)


def _api_body(data_dir: Path, minutes: float) -> str:
    clock = SimClock(SIM_T0)
    runtime = _build(data_dir, clock)
    try:
        _run(runtime, clock, minutes / 60.0)
        return json.dumps(runtime.app.outside_moves(state="all", limit=500), sort_keys=True)
    finally:
        runtime.close()


def test_two_runs_give_identical_api_moves_bodies(tmp_path: Path) -> None:
    first = _api_body(tmp_path / "a", 30.0)
    second = _api_body(tmp_path / "b", 30.0)
    assert first == second
    body = json.loads(first)
    assert sorted(a["exchange_id"] for a in body["alerts"]) == ["9035", "9036", "9037", "9039", "9040", "9040"]


# --------------------------------------------------------------------------- restart continuity (§11, D47)


def test_a_short_restart_continues_the_open_alerts(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    first = _build(tmp_path, clock)
    try:
        _run(first, clock, 270 / 3600.0)  # t0 .. t0 + 270 s: lead_short and lead_long are open and lagging
        before = {a["exchange_id"]: a for a in first.moves.alerts()}
    finally:
        first.close()
    assert set(before) == {"9035", "9036"} and all(a["state"] == "open" for a in before.values())
    assert all(a["lag"]["outcome"] == "pending" for a in before.values())
    clock.advance(30)  # a 30-s gap (<= 600 s): the same alerts continue
    second = _build(tmp_path, clock, fresh_db=False)
    events: List[MoveEvent] = []
    second.moves.add_listener(events.append)
    try:
        assert second.demo_market.t0 == SIM_T0  # the script continues where it was
        _run(second, clock, 900 / 3600.0)
        after = {a["alert_id"]: a for a in second.moves.alerts()}
    finally:
        second.close()
    nh = after[before["9035"]["alert_id"]]  # the same id, base and detection time
    assert nh["t_base"] == before["9035"]["t_base"] and nh["detected_at"] == before["9035"]["detected_at"]
    assert nh["lag"]["outcome"] == "followed" and nh["lag"]["lag_s"] == pytest.approx(262.5, abs=TOL)
    assert after[before["9036"]["alert_id"]]["lag"]["outcome"] == "followed"  # followed at ~t0 + 720
    assert not any(e.kind == "opened" and e.alert_id in (before["9035"]["alert_id"], before["9036"]["alert_id"])
                   for e in events)
    rows = _jsonl(tmp_path / "demo" / MOVES_ALERTS_FILE)  # appended across both runs, never rewritten
    opened = [r["alert"]["alert_id"] for r in rows if r["event"] == "opened"]
    assert len(opened) == len(set(opened)) and before["9035"]["alert_id"] in opened
    assert any(r["event"] == "status" and r["alert"]["alert_id"] == before["9035"]["alert_id"]
               and r["alert"]["lag"]["outcome"] == "followed" for r in rows)


def test_a_long_restart_censors_the_pending_lags(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    first = _build(tmp_path, clock)
    try:
        _run(first, clock, 270 / 3600.0)
        ids = {a["alert_id"] for a in first.moves.alerts()}
    finally:
        first.close()
    assert len(ids) == 2
    clock.advance(25 * 60)  # 25 minutes without the bot (> 600 s): nothing is guessed
    second = _build(tmp_path, clock, fresh_db=False)
    events: List[MoveEvent] = []
    second.moves.add_listener(events.append)
    try:
        _run(second, clock, 60 / 3600.0)
        after = {a["alert_id"]: a for a in second.moves.alerts()}
    finally:
        second.close()
    for alert_id in ids:
        lag = after[alert_id]["lag"]
        assert lag["outcome"] == "censored" and after[alert_id]["state"] == "closed", alert_id
        assert lag["reason"].startswith("The bot was not running for ") and "while this was measured" in lag["reason"]
    assert not any(e.kind == "opened" and e.alert_id in ids for e in events)
    assert {e.alert_id for e in events if e.kind == "status" and e.lag_outcome == "censored"} == ids
