"""Integration of the paper trader with the tracker, the store and the demo (docs/PAPER_TRADING.md §7.6, §7.8).

A fast demo simulation (``web.build_demo(clock=SimClock)`` + ``pipeline.run_simulation``) of 2 hours must show the
scripted scenarios as simulated trades, keep consistent books, survive a restart on the same database, start a
new run when the settings change, and be deterministic: two runs give byte-identical ``/api/paper`` bodies.
Offline: the demo's simulated API and its scripted outside prices.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List

import pytest

from supermarket_bot import pipeline, web
from supermarket_bot.demo import DEMO_HEADLINE, DEMO_PORTFOLIO_IDS, SIM_T0, SimClock

HOURS = 2.0
MIN = 60.0


def _fast_demo(data_dir: Path, clock: SimClock, **kw: Any) -> web.Runtime:
    return web.build_demo(data_dir, 30.0, out=io.StringIO(), clock=clock, **kw)


@pytest.fixture(scope="module")
def sim(tmp_path_factory: Any) -> Iterator[SimpleNamespace]:
    data_dir = tmp_path_factory.mktemp("paper-sim")
    clock = SimClock(SIM_T0)
    started = time.monotonic()
    runtime = _fast_demo(data_dir, clock)
    try:
        body = pipeline.run_simulation(runtime, clock, hours=HOURS, step_s=30.0, end_run=False)
        elapsed = time.monotonic() - started
        run_id = body["run"]["run_id"]
        yield SimpleNamespace(
            runtime=runtime, clock=clock, body=body, elapsed=elapsed, run_id=run_id, store=runtime.store,
            api=runtime.app.paper(), fills=runtime.store.paper_fills(run_id, limit=None),
            trades=runtime.store.paper_trades(run_id, limit=None),
            ports={p["portfolio_id"]: p for p in body["portfolios"]},
        )
    finally:
        runtime.close()


def _fills(sim: SimpleNamespace, pid: str, kind: str = "") -> List[Dict[str, Any]]:
    return [f for f in sim.fills if f["portfolio_id"] == pid and (not kind or f["kind"] == kind)]


def _minutes(ts: float) -> float:
    return (ts - SIM_T0) / MIN


# --------------------------------------------------------------------------- a 2-hour fast run


def test_a_two_hour_fast_run_finishes_in_under_a_minute(sim: SimpleNamespace) -> None:
    assert sim.elapsed < 60.0, f"2 simulated hours took {sim.elapsed:.1f} s"
    run = sim.body["run"]
    assert run["steps"] == 241 and run["hours_run"] == pytest.approx(HOURS, abs=1e-4) and run["gaps"] == []
    assert run["last_step_at"] == SIM_T0 + HOURS * 3600 and run["interval"] == 30.0


def test_portfolios_and_the_published_headline(sim: SimpleNamespace) -> None:
    assert [p["portfolio_id"] for p in sim.body["portfolios"]] == list(DEMO_PORTFOLIO_IDS)
    head = sim.body["headline"]
    assert head["portfolio_id"] == DEMO_HEADLINE and sim.ports[DEMO_HEADLINE]["headline"] is True
    assert head["latency_s"] == 240.0
    assert sum(1 for p in sim.body["portfolios"] if p["headline"]) == 1


def test_fills_in_the_basket_value_and_hole_portfolios_and_the_human_headline(sim: SimpleNamespace) -> None:
    for pid in ("kind:basket", "kind:value", "kind:hole", DEMO_HEADLINE):
        assert sim.ports[pid]["fills"] > 0, pid
    basket = {f["exchange_id"] for f in _fills(sim, "kind:basket") if f["purpose"] == "entry"}
    assert {"9024", "9025"} <= basket  # (g) the Nevada NO basket
    assert {"9030", "9031", "9032"} <= basket  # (j) the Nebraska 3-leg NO basket
    ne = [f for f in _fills(sim, "kind:basket") if f["exchange_id"] == "9031" and f["purpose"] == "entry"]
    assert ne and 60 <= _minutes(ne[0]["ts"]) <= 80
    value = {f["exchange_id"] for f in _fills(sim, "kind:value") if f["purpose"] == "entry"}
    assert {"9026", "9028", "9034"} <= value  # (h) Texas, (i) Iowa, (l) the Maine debate
    hole = [f for f in _fills(sim, "kind:hole") if f["exchange_id"] == "9033" and f["purpose"] == "entry"]
    assert hole and hole[0]["liquidity"] == "maker" and 20 <= _minutes(hole[0]["ts"]) <= 23  # (k) the Wyoming hole
    head = {f["exchange_id"] for f in _fills(sim, DEMO_HEADLINE) if f["purpose"] == "entry"}
    assert {"9024", "9025", "9026", "9034"} <= head  # by hand: the NV basket, the TX value idea, the ME debate


def test_a_settled_trade_and_winners_and_losers(sim: SimpleNamespace) -> None:
    settled = [t for t in sim.trades if t["exit_reason"] == "settled"]
    assert settled and all(t["exchange_ids"] == ["9034"] and t["pnl"] > 0 for t in settled)  # (l) settles YES at t0+40m
    assert all(abs(_minutes(t["closed_at"]) - 40) < 1 for t in settled)
    assert any(f["action"] == "settle" and f["exchange_id"] == "9034" for f in sim.fills)
    assert any(t["pnl"] > 0 for t in sim.trades) and any(t["pnl"] < 0 for t in sim.trades)
    winners = {(t["kind"], tuple(t["exchange_ids"])) for t in sim.trades if t["pnl"] > 0}
    assert ("hole", ("9033",)) in winners and ("basket", ("9024", "9025")) in winners
    iowa = [t for t in sim.trades if t["portfolio_id"] == "kind:value" and t["exchange_ids"][0] in ("9028", "9029")]
    assert iowa and all(t["pnl"] < 0 for t in iowa)  # (i) the outside price was wrong: sold at a loss
    assert all(88 <= _minutes(t["closed_at"]) <= 100 for t in iowa)


def test_an_open_unrealised_winner(sim: SimpleNamespace) -> None:
    texas = [p for p in sim.body["positions"] if p["portfolio_id"] == "kind:value" and p["exchange_id"] in ("9026", "9027")]
    assert texas and all(p["status"] == "open" and p["unrealized_liq"] > 0 for p in texas)  # (h) still converging


def test_insufficient_headline_verdict_with_caveats(sim: SimpleNamespace) -> None:
    head = sim.body["headline"]
    assert head["verdict"]["level"] == "insufficient"  # 2 hours: never more than "not enough evidence yet"
    assert len(head["verdict_caveats"]) == 4
    assert all(p["verdict"]["level"] in ("insufficient", "inconclusive") for p in sim.body["portfolios"])
    assert sim.body["table_warning"] and "9 portfolios" in sim.body["table_warning"]
    assert any(c.startswith("Demo data") for c in sim.body["caveats"]) and sim.body["demo"] is True


def test_event_study_records_value_and_basket_signals(sim: SimpleNamespace) -> None:
    kinds = sim.body["study"]["kinds"]
    assert kinds["value"]["events"] > 0 and kinds["basket"]["events"] > 0
    assert kinds["value"]["horizons"]["30m"]["n"] > 0
    assert sim.body["study"]["can_show"] and sim.body["study"]["cannot_show"]


def test_books_are_consistent(sim: SimpleNamespace) -> None:
    by_pid: Dict[str, float] = {}
    for pos in sim.body["positions"]:
        if pos["status"] == "open":
            by_pid[pos["portfolio_id"]] = by_pid.get(pos["portfolio_id"], 0.0) + float(pos["liq_value"] or 0.0)
    for p in sim.body["portfolios"]:
        assert p["cash"] + p["positions_liq"] == pytest.approx(p["equity_liq"], abs=0.01), p["portfolio_id"]
        assert p["equity_liq"] - p["start_capital"] == pytest.approx(p["pnl_liq"], abs=0.01)
        assert p["positions_liq"] == pytest.approx(by_pid.get(p["portfolio_id"], 0.0), abs=0.01)
        assert 0 <= p["reserved_cash"] <= p["cash"] + 1e-6
        assert p["fills"] == len(_fills(sim, p["portfolio_id"]))  # every fill stored once (PK-unique ids)
    stored = sim.store.paper_equity(sim.run_id)
    assert stored and all(abs(e["liq_value"] - e["cash"]) < 1e7 for e in stored)


def test_the_api_body_carries_the_tracker_extras(sim: SimpleNamespace) -> None:
    api = sim.api
    assert api["enabled"] is True and api["available"] is True and api["error"] is None
    # 33 open outcomes at the start; the Maine debate (9034) settled at t0 + 40 min and left the targets
    assert api["fair_value"] == {"mode": "auto", "enabled": True, "usable": api["fair_value"]["usable"], "total": 32}
    assert api["fair_value"]["usable"] > 0
    budget = api["budget"]
    assert budget["reads_limit"] == 600 and budget["max_book_reads_per_step"] == 8
    assert api["last_step"]["step"] >= 240 and api["last_step"]["duration_s"] == 0.0  # measured on the SimClock
    status = sim.runtime.tracker.status()
    assert status["paper"]["run_id"] == sim.run_id and status["paper"]["steps"] == 241
    assert status["fair_value"]["mode"] == "auto"
    assert not [p for p in status["problems"] if p["source"] in ("paper", "fair value", "read budget")]


# --------------------------------------------------------------------------- determinism


def test_two_fast_runs_give_byte_identical_paper_bodies(sim: SimpleNamespace, tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    runtime = _fast_demo(tmp_path, clock)
    try:
        pipeline.run_simulation(runtime, clock, hours=HOURS, step_s=30.0, end_run=False)
        assert web.encode_json(runtime.app.paper()) == web.encode_json(sim.api)
        # and ending it writes the final snapshot that ?run=previous serves
        runtime.tracker.paper_end("completed")
        previous = runtime.app.paper(run="previous")
        assert previous["run"]["run_id"] == sim.run_id and previous["run"]["end_reason"] == "completed"
        assert set(previous["run"]["final"]["verdicts"]) == set(DEMO_PORTFOLIO_IDS)
        assert previous["headline"]["verdict"]["level"] == "insufficient"
    finally:
        runtime.close()


# --------------------------------------------------------------------------- restart continuity


def _engine_state(runtime: web.Runtime) -> Dict[str, Any]:
    return runtime.tracker.paper_runner.engine.state()


def test_restart_continues_the_same_run(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    first = _fast_demo(tmp_path, clock)
    try:
        pipeline.run_simulation(first, clock, hours=45 / 60, step_s=30.0, end_run=False)
        before = _engine_state(first)
        fills_before = len(first.store.paper_fills(before["run_id"], limit=None))
    finally:
        first.close()
    assert before["steps"] == 91 and any(p["orders"] for p in before["portfolios"].values())
    clock.advance(30)
    second = _fast_demo(tmp_path, clock, fresh_db=False)
    try:
        assert second.demo_market.t0 == SIM_T0  # the scripted events continue where they were
        after = _engine_state(second)
        assert after["run_id"] == before["run_id"] and after["steps"] == before["steps"]
        assert after["counters"] == before["counters"]
        for pid, port in before["portfolios"].items():
            again = after["portfolios"][pid]
            assert again["cash"] == port["cash"] and again["reserved_cash"] == port["reserved_cash"], pid
            assert again["positions"] == port["positions"] and again["orders"] == port["orders"], pid
        body = pipeline.run_simulation(second, clock, hours=10 / 60, step_s=30.0, end_run=False)
        assert body["run"]["run_id"] == before["run_id"] and body["run"]["steps"] == before["steps"] + 21
        assert body["run"]["gaps"] == [] and body["has_previous"] is False
        fills = second.store.paper_fills(before["run_id"], limit=None)
        assert len({f["fill_id"] for f in fills}) == len(fills) >= fills_before
        assert sum(p["fills"] for p in body["portfolios"]) == len(fills)  # counters continued: no id was reused
    finally:
        second.close()


def test_changing_the_regime_starts_a_new_run(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    first = _fast_demo(tmp_path, clock, news=False)
    try:
        pipeline.run_simulation(first, clock, hours=5 / 60, step_s=30.0, end_run=False)
        old_run = first.tracker.paper_view()["run"]["run_id"]
    finally:
        first.close()
    clock.advance(30)
    second = _fast_demo(tmp_path, clock, news=False, fresh_db=False, regime="resolved_outcomes")
    try:
        previous = second.tracker.paper_view("previous")
        assert previous["run"]["run_id"] == old_run and previous["run"]["end_reason"] == "settings changed"
        body = pipeline.run_simulation(second, clock, hours=60 / 3600, step_s=30.0, end_run=False)
        assert body["run"]["run_id"] != old_run and body["run"]["regime"] == "resolved_outcomes"
        assert body["has_previous"] is True
    finally:
        second.close()
