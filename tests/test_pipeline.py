"""Tests for supermarket_bot/pipeline.py (docs/PAPER_TRADING.md §7.3): input assembly from the tracker's view
and the store, the base observation, settlements, the moved ``context_summary``, ``code_version`` and the
synchronous fast-demo driver. Offline; the strategy/sizing/fair-value collaborators are faked where the test
is about the plumbing.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from supermarket_bot import __version__, pipeline, web
from supermarket_bot.demo import SIM_T0, SimClock
from supermarket_bot.models import (
    FairValue,
    HighBand,
    LeaderboardSnapshot,
    RaceRef,
    SettlementInfo,
    StrategyParams,
    Surge,
)
from supermarket_bot.store import TrackerStore

NOW = 1_791_000_000.0
CUP_END = 1_793_811_600.0  # 2026-11-04T17:00:00Z


def _market(mid: str, title: str, eids: List[str], options: List[str]) -> Dict[str, Any]:
    return {"id": mid, "title": title, "status": "open", "settlementDate": "2026-11-04T04:59:00.000Z",
            "categories": ["Election Outcome"], "isMultiOutcome": len(eids) > 1,
            "exchanges": [{"id": e, "option": o, "initialPrice": 0.5} for e, o in zip(eids, options)]}


def _row(eid: str, mid: str, title: str, bid: float, ask: float, **kw: Any) -> Dict[str, Any]:
    row = {"exchange_id": eid, "market_id": mid, "title": title, "option": "YES", "settlement_date": "2026-11-04T04:59:00.000Z",
           "mark": round((bid + ask) / 2, 6), "last": bid, "bid": bid, "ask": ask, "spread": round(ask - bid, 6),
           "change_5m": None, "change_1h": None, "change_24h": None, "sparkline": [[NOW - 60, bid]], "high_band": None,
           "surge_id": None, "updated_at": NOW - 5, "stale": False}
    row.update(kw)
    return row


CONTEXT = {
    "tournament": {"id": "t1", "slug": "cup", "endDate": "2026-11-04T17:00:00.000Z", "initialBalance": 100000},
    "balance": 90_000.0,
    "initial_balance": 100_000.0,
    "account_value": 97_500.0,
    "leaderboard": {
        "my_rank": 40, "leader_value": 285_000.0, "total": 120, "period": "all",
        "top": [{"rank": 1, "username": "a", "pnl": 185_000.0, "value": 285_000.0}],
        "entries": [{"rank": i, "username": f"u{i}", "pnl": 185_000.0 - 1000 * i, "value": 285_000.0 - 1000 * i}
                    for i in range(1, 6)],
    },
    "constraints": {"data": [{"relationshipId": "r1"}], "violationsCount": 1},
    "overround": [{"market_id": "m9", "overround": 0.97, "arbitrage": True}],
    "updated_at": NOW - 100,
}


@pytest.fixture
def store() -> TrackerStore:
    st = TrackerStore(":memory:")
    st.upsert_markets([
        _market("316", "Will the Democratic Party win the Nevada Governor?", ["9024"], ["YES"]),
        _market("317", "Will the Republican Party win the Nevada Governor?", ["9025"], ["YES"]),
        _market("326", "Will the Maine Senate candidates debate before October 7?", ["9034"], ["YES"]),
    ])
    st.put_book("9024", NOW - 20, [[0.53, 100], [0.52, 50]], [[0.54, 80]])
    yield st
    st.close()


def _view(stale: bool = False) -> Dict[str, Any]:
    return {
        "exchanges": [
            _row("9024", "316", "Will the Democratic Party win the Nevada Governor?", 0.53, 0.54),
            _row("9025", "317", "Will the Republican Party win the Nevada Governor?", 0.51, 0.52, stale=stale),
        ],  # 9034 is not on the open list (closed): it must never yield an idea
        "surges": [],
        "high_band": [dict(HighBand("9024", "316", "YES", 0.96, 0.9, 0.96, 0.95, 0.97, 21600.0, True).to_dict(),
                           title="shown in the dashboard only")],
        "context": json.loads(json.dumps(CONTEXT)),
        "status": {},
    }


class FakeFairValues:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.value = FairValue(exchange_id="9024", value=0.6, source="demo", usable=True)

    def current(self) -> Any:
        return SimpleNamespace(enabled=self.enabled, values={"9024": self.value})

    def races(self) -> Dict[str, RaceRef]:
        return {"9024": RaceRef("2026:GOVERNOR:NV", "GOVERNOR", "NV", party="D")}


@pytest.fixture
def fake_bar(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    calls: List[Any] = []

    def estimate_bar(leaderboard: Any, history: Any, initial: Any, days_left: float) -> Any:
        calls.append((leaderboard, list(history), initial, days_left))
        return SimpleNamespace(value=221_500.0)

    import supermarket_bot.sizing as sizing_mod

    monkeypatch.setattr(sizing_mod, "estimate_bar", estimate_bar)
    return calls


# --------------------------------------------------------------------------- assemble_inputs


def test_assemble_inputs_from_a_canned_view(store: TrackerStore, fake_bar: List[Any]) -> None:
    store.record_surge(Surge("9024", "316", "1h", 3600.0, NOW - 3000, NOW - 60, 0.4, 0.53, 0.13, "up", 0.54, NOW - 60))
    store.record_surge(Surge("9034", "326", "1h", 3600.0, NOW - 3000, NOW - 60, 0.8, 0.95, 0.15, "up", 0.95, NOW - 60))
    store.add_leaderboard_snapshot(LeaderboardSnapshot(at=NOW - 86400, entries=[{"rank": 3, "value": 200_000.0}]))
    params = StrategyParams()
    mids = {"9024": [(NOW - 30, 0.535), (NOW - 60, 0.53), ("bad", 1)], 9025: [(NOW - 30, 0.515)]}
    inputs = pipeline.assemble_inputs(now=NOW, view=_view(stale=True), store=store, fair_values=FakeFairValues(),
                                      regime="vwap_closeout", params=params, recent_mids=mids)
    assert inputs.now == NOW and inputs.cup_end == CUP_END
    assert set(inputs.infos) == {"9024", "9025"}  # the closed outcome 9034 is left out
    assert inputs.infos["9024"].market_title == "Will the Democratic Party win the Nevada Governor?"
    assert set(inputs.latest) == {"9024"}  # the stale row yields no quote, so no trade idea
    q = inputs.latest["9024"]
    assert (q.ts, q.bid, q.ask, q.last, q.price, q.source) == (NOW - 5, 0.53, 0.54, 0.53, 0.535, "tick")
    assert q.book is not None and q.book["at"] == NOW - 20 and q.book["bids"][0] == [0.53, 100.0]
    assert [s.exchange_id for s in inputs.surges] == ["9024"]  # surges on open outcomes only
    assert [b.exchange_id for b in inputs.bands] == ["9024"]
    assert (inputs.balance, inputs.initial_balance, inputs.account_value) == (90_000.0, 100_000.0, 97_500.0)
    assert (inputs.my_rank, inputs.leader_value) == (40, 285_000.0)
    assert inputs.constraints == {"data": [{"relationshipId": "r1"}], "violationsCount": 1}
    assert inputs.overround_rows == [{"market_id": "m9", "overround": 0.97, "arbitrage": True}]
    assert inputs.fair_values["9024"].value == 0.6
    assert inputs.races["9024"].race_key == "2026:GOVERNOR:NV"
    assert inputs.races["9025"].race_key == "2026:GOVERNOR:NV" and inputs.races["9025"].party == "R"  # parsed from the title
    assert inputs.settlement_regime == "vwap_closeout" and inputs.params is params
    assert inputs.recent_mids == {"9024": [(NOW - 60, 0.53), (NOW - 30, 0.535)], "9025": [(NOW - 30, 0.515)]}
    assert inputs.leaderboard is not None and [e["rank"] for e in inputs.leaderboard.entries] == [1, 2, 3, 4, 5]
    assert inputs.bar is not None and inputs.bar.value == 221_500.0
    board, history, initial, days_left = fake_bar[0]
    assert board.my_rank == 40 and [h.at for h in history] == [NOW - 86400] and initial == 100_000.0
    assert days_left == pytest.approx((CUP_END - NOW) / 86400.0)


def test_assemble_inputs_without_fair_values_or_context(store: TrackerStore, fake_bar: List[Any]) -> None:
    view = {"exchanges": [_row("9024", "316", "Will the Democratic Party win the Nevada Governor?", 0.53, 0.54)]}
    inputs = pipeline.assemble_inputs(now=NOW, view=view, store=store, cup_end=NOW + 86400)
    assert inputs.fair_values == {} and inputs.balance is None and inputs.leaderboard is None and inputs.bar is None
    assert inputs.cup_end == NOW + 86400 and inputs.settlement_regime == "unknown" and inputs.recent_mids == {}
    off = pipeline.assemble_inputs(now=NOW, view=view, store=store, fair_values=FakeFairValues(enabled=False))
    assert off.fair_values == {}
    assert fake_bar == []


def test_a_row_without_updated_at_is_priced_from_the_store(store: TrackerStore, fake_bar: List[Any]) -> None:
    store.add_ticks(NOW - 30, [{"exchange_id": "9024", "latest_price": 0.5, "best_bid": 0.49, "best_ask": 0.51}])
    row = _row("9024", "316", "Will the Democratic Party win the Nevada Governor?", 0.53, 0.54)
    del row["updated_at"]
    inputs = pipeline.assemble_inputs(now=NOW, view={"exchanges": [row]}, store=store)
    assert inputs.latest["9024"].ts == NOW - 30 and inputs.latest["9024"].bid == 0.49


# --------------------------------------------------------------------------- observation


def test_observation_from_the_view_and_the_store(store: TrackerStore, fake_bar: List[Any]) -> None:
    store.record_settlements([SettlementInfo("9034", "326", "YES", settled_on=NOW - 600, payout_yes=1.0, detected_at=NOW - 30)])
    view = _view()
    inputs = pipeline.assemble_inputs(now=NOW, view=view, store=store, fair_values=FakeFairValues())
    obs = pipeline.observation(now=NOW, inputs=inputs, view=view, store=store)
    assert obs.now == NOW and obs.cup_end == inputs.cup_end
    assert set(obs.quotes) == {"9024", "9025"}
    assert (obs.quotes["9025"].bid, obs.quotes["9025"].ask, obs.quotes["9025"].ts) == (0.51, 0.52, NOW - 5)
    book = obs.books["9024"]
    assert book.source == "tracker" and book.observed_at == NOW - 20 and book.bids == [(0.53, 100.0), (0.52, 50.0)]
    assert "9025" not in obs.books  # never read
    assert obs.settlements["9034"].payout_yes == 1.0  # only detected settlements exist
    assert obs.open_ids == {"9024", "9025"} and obs.trades == {}
    assert set(obs.infos) == {"9024", "9025"} and obs.fair_values["9024"].value == 0.6 and "9024" in obs.races
    only = pipeline.observation(now=NOW, inputs=inputs, view=view, store=store, settlements={}, open_ids=["9024"])
    assert only.settlements == {} and only.open_ids == {"9024"}


# --------------------------------------------------------------------------- settlements_from_market


def _settled(settled_with: Any, exchanges: List[Dict[str, Any]], multi: bool = False, status: str = "settled") -> Dict[str, Any]:
    return {"id": "326", "status": status, "settledWith": settled_with, "settledOn": "2026-10-05T14:40:00.000Z",
            "isMultiOutcome": multi, "exchanges": exchanges}


def test_settlements_from_market_yes_no_option_refund_unknown() -> None:
    yes = pipeline.settlements_from_market(_settled("YES", [{"id": "9034", "option": "YES"}]), NOW)
    assert yes == [SettlementInfo("9034", "326", "YES", settled_on=SIM_T0 + 2400, payout_yes=1.0, refund=False,
                                  detected_at=NOW)]
    no = pipeline.settlements_from_market(_settled("no", [{"id": "9034", "option": "YES"}]), NOW)
    assert no[0].payout_yes == 0.0 and no[0].settled_with == "no"
    multi = pipeline.settlements_from_market(
        _settled("Republican nominee", [{"id": "9011", "option": "Democratic nominee"},
                                        {"id": "9012", "option": "Republican nominee"},
                                        {"id": "9013", "option": "Any other candidate"}], multi=True), NOW)
    assert [(s.exchange_id, s.payout_yes) for s in multi] == [("9011", 0.0), ("9012", 1.0), ("9013", 0.0)]
    by_id = pipeline.settlements_from_market(_settled("9013", [{"id": "9011", "option": "A"}, {"id": "9013", "option": "B"}],
                                                      multi=True), NOW)
    assert [(s.exchange_id, s.payout_yes) for s in by_id] == [("9011", 0.0), ("9013", 1.0)]
    refund = pipeline.settlements_from_market(_settled("REFUND", [{"id": "9034", "option": "YES"}]), NOW)
    assert refund[0].refund is True and refund[0].payout_yes is None
    void = pipeline.settlements_from_market(_settled("Voided", [{"id": "9034"}]), NOW)
    assert void[0].refund is True and void[0].payout_yes is None  # case-insensitive refund words
    unknown = pipeline.settlements_from_market(_settled("Somebody else", [{"id": "9034", "option": "YES"}]), NOW)
    assert unknown[0].payout_yes is None and unknown[0].refund is False
    assert pipeline.settlements_from_market(_settled(None, [{"id": "9034"}], status="open"), NOW) == []
    assert pipeline.settlements_from_market({"status": "settled"}, NOW) == []
    assert pipeline.settlements_from_market("junk", NOW) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- context_summary, leaderboard


def test_context_summary_moved_here_and_reexported_by_web() -> None:
    assert web.context_summary is pipeline.context_summary
    ctx = pipeline.context_summary(CONTEXT)
    assert ctx["account_value"] == 97_500.0 and ctx["balance"] == 90_000.0 and ctx["initial_balance"] == 100_000.0
    assert ctx["my_rank"] == 40 and ctx["leader_value"] == 285_000.0
    assert [lead["username"] for lead in ctx["leaders"]] == ["a"]
    assert ctx["constraints"]["violationsCount"] == 1 and ctx["updated_at"] == NOW - 100
    empty = pipeline.context_summary(None)
    assert empty["balance"] is None and empty["account_value"] is None and empty["leaders"] == []
    assert pipeline.context_summary({"leaderboard": [{"rank": 1, "pnl": 10.0}], "initial_balance": 50.0})["leader_value"] == 60.0


def test_pipeline_does_not_import_web_at_module_level() -> None:
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "from . import web" not in source and "from .web import" not in source and "import web" not in source


def test_leaderboard_snapshot_prefers_entries() -> None:
    snap = pipeline.leaderboard_snapshot(CONTEXT, NOW)
    assert snap is not None and snap.at == NOW and snap.my_rank == 40 and snap.total == 120
    assert snap.initial_balance == 100_000.0 and len(snap.entries) == 5
    top_only = pipeline.leaderboard_snapshot({"leaderboard": {"top": [{"rank": 1, "username": "x", "pnl": 1.0, "value": 2.0}]}}, NOW)
    assert top_only is not None and top_only.entries == [{"rank": 1, "username": "x", "pnl": 1.0, "value": 2.0}]
    assert pipeline.leaderboard_snapshot({}, NOW) is None


# --------------------------------------------------------------------------- code_version


def _fake_git(root: Path, head: str, refs: Dict[str, str], packed: str = "") -> None:
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n", encoding="utf-8")
    for name, sha in refs.items():
        (git / name).write_text(sha + "\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(packed, encoding="utf-8")


def test_git_commit_from_head_ref_detached_and_packed(tmp_path: Path) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    a = tmp_path / "a"
    _fake_git(a, "ref: refs/heads/main", {"refs/heads/main": sha})
    assert pipeline._git_commit(a) == sha
    b = tmp_path / "b"
    _fake_git(b, sha, {})
    assert pipeline._git_commit(b) == sha
    c = tmp_path / "c"
    _fake_git(c, "ref: refs/heads/dev", {}, packed=f"# pack-refs with: peeled\n{sha} refs/heads/dev\n")
    assert pipeline._git_commit(c) == sha
    assert pipeline._git_commit(tmp_path / "none") is None


def test_code_version_format() -> None:
    version = pipeline.code_version()
    assert version == __version__ or (version.startswith(f"{__version__}+") and len(version.split("+", 1)[1]) == 12)
    assert pipeline.code_version() == version  # stable


# --------------------------------------------------------------------------- run_simulation


def test_run_simulation_on_a_small_fast_demo(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    runtime = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock, news=False)
    seen: List[Dict[str, Any]] = []
    try:
        with pytest.raises(ValueError):
            pipeline.run_simulation(runtime, clock, hours=0.1, step_s=0)
        final = pipeline.run_simulation(runtime, clock, hours=10 / 60, step_s=30.0, summary_every_s=300.0,
                                        on_summary=lambda s: seen.append(s))
        assert clock() == SIM_T0 + 600  # the first step at the start, the last one 10 minutes later
        run = final["run"]
        assert run["steps"] == 21 and run["end_reason"] == "completed" and run["ended_at"] == SIM_T0 + 600
        assert run["hours_run"] == pytest.approx(10 / 60, abs=1e-5)
        assert [s["run"]["last_step_at"] for s in seen] == [SIM_T0 + 300, SIM_T0 + 600]
        assert runtime.tracker.status()["cycles"] == 21
        assert runtime.tracker.paper_view("previous")["run"]["end_reason"] == "completed"
        assert runtime.store.fair_value_refreshes(SIM_T0 - 1)  # fair values refreshed every simulated minute
        assert len(runtime.store.fair_value_refreshes(SIM_T0 - 1)) == 11
    finally:
        runtime.close()


def test_run_simulation_can_leave_the_run_open(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    runtime = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock, news=False, fair_value="off")
    try:
        final = pipeline.run_simulation(runtime, clock, hours=60 / 3600, step_s=30.0, end_run=False)
        assert final["run"]["steps"] == 3 and final["run"]["end_reason"] is None and final["has_previous"] is False
        assert runtime.store.paper_load_run(None)["run_id"] == final["run"]["run_id"]
    finally:
        runtime.close()


class _RecordingTracker:
    """A tracker stand-in for run_simulation's call sequence (docs/OUTSIDE_MOVES.md §16)."""

    def __init__(self, clock: SimClock, moves: bool = True, paper: bool = True) -> None:
        self.clock = clock
        self.moves = SimpleNamespace(poll_s=15.0) if moves else None
        self.paper = paper
        self.calls: List[Any] = []

    def run_once(self) -> None:
        self.calls.append(("run", self.clock()))

    def backfill_step(self, n: int) -> int:
        self.calls.append(("backfill", self.clock()))
        return 0

    def analyze_pending(self, n: int) -> int:
        self.calls.append(("analyze", self.clock()))
        return 0

    def fair_value_step(self, now: float) -> bool:
        self.calls.append(("fv", now))
        return True

    def paper_step(self, now: float) -> None:
        self.calls.append(("paper", now))

    def moves_step(self, now: float) -> None:
        assert now == self.clock()  # the clock sits at the sub-tick the watcher is stepped at
        self.calls.append(("moves", now))

    def paper_view(self) -> Any:
        return {"run": {"steps": sum(1 for c in self.calls if c[0] == "paper")}} if self.paper else None

    def moves_view(self) -> Dict[str, Any]:
        return {"steps": sum(1 for c in self.calls if c[0] == "moves")}

    def paper_end(self, reason: str) -> None:
        self.calls.append(("end", self.clock()))


def _reference_loop(tracker: Any, clock: SimClock, hours: float, step_s: float) -> None:
    """The loop of run_simulation before docs/OUTSIDE_MOVES.md (no moves_every_s): the expected call sequence."""
    from supermarket_bot.fairvalue import FV_REFRESH_S

    steps = max(0, int(round(hours * 3600.0 / step_s)))
    next_fv = float(clock())
    for i in range(steps + 1):
        if i:
            clock.advance(step_s)
        tracker.run_once()
        tracker.backfill_step(8)
        tracker.analyze_pending(2)
        now = float(clock())
        if now >= next_fv - 1e-9:
            tracker.fair_value_step(now)
            while next_fv <= now + 1e-9:
                next_fv += FV_REFRESH_S
        tracker.paper_step(now)
    tracker.paper_end("completed")


def test_run_simulation_steps_the_watcher_at_the_step_and_sub_step_times() -> None:
    clock = SimClock(SIM_T0)
    tracker = _RecordingTracker(clock)
    final = pipeline.run_simulation(SimpleNamespace(tracker=tracker), clock, hours=2 / 60, step_s=30.0, moves_every_s=15.0)
    moves = [t - SIM_T0 for kind, t in tracker.calls if kind == "moves"]
    assert moves == [0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0, 105.0, 120.0]  # the first one at the demo start
    assert [t - SIM_T0 for kind, t in tracker.calls if kind == "run"] == [0.0, 30.0, 60.0, 90.0, 120.0]
    assert clock() == SIM_T0 + 120 and final == {"run": {"steps": 5}}
    # within one step: the watcher steps after the paper step, then at the sub-tick
    first = [c[0] for c in tracker.calls[:7]]
    assert first == ["run", "backfill", "analyze", "fv", "paper", "moves", "moves"]
    # a sub-tick period that does not divide the step: sub-ticks strictly before the next step only
    clock2 = SimClock(SIM_T0)
    other = _RecordingTracker(clock2)
    pipeline.run_simulation(SimpleNamespace(tracker=other), clock2, hours=90 / 3600, step_s=30.0, moves_every_s=20.0)
    assert [t - SIM_T0 for kind, t in other.calls if kind == "moves"] == [0.0, 20.0, 30.0, 50.0, 60.0, 80.0, 90.0]
    with pytest.raises(ValueError):
        pipeline.run_simulation(SimpleNamespace(tracker=other), clock2, hours=0.01, step_s=30.0, moves_every_s=0.0)


def test_run_simulation_without_moves_every_s_keeps_the_old_loop() -> None:
    for moves_every_s, watcher in ((None, True), (None, False), (15.0, False)):
        clock, ref_clock = SimClock(SIM_T0), SimClock(SIM_T0)
        tracker, reference = _RecordingTracker(clock, moves=watcher), _RecordingTracker(ref_clock, moves=watcher)
        pipeline.run_simulation(SimpleNamespace(tracker=tracker), clock, hours=0.1, step_s=30.0, moves_every_s=moves_every_s)
        _reference_loop(reference, ref_clock, 0.1, 30.0)
        assert tracker.calls == reference.calls and clock() == ref_clock()
        assert not any(kind == "moves" for kind, _ in tracker.calls)


def test_run_simulation_hands_the_moves_summary_over_without_a_paper_trader() -> None:
    clock = SimClock(SIM_T0)
    tracker = _RecordingTracker(clock, paper=False)
    seen: List[Any] = []
    final = pipeline.run_simulation(SimpleNamespace(tracker=tracker), clock, hours=10 / 60, step_s=30.0,
                                    summary_every_s=300.0, on_summary=seen.append, moves_every_s=15.0, end_run=False)
    assert seen == [{"steps": 21}, {"steps": 41}]  # at the step times 5 and 10 minutes in, after their watcher step
    assert final == {"steps": 41}


def test_run_simulation_with_moves_every_s_but_no_watcher_gives_the_same_paper_body(tmp_path: Path) -> None:
    bodies = []
    for i, every in enumerate((None, 15.0)):
        clock = SimClock(SIM_T0)
        runtime = web.build_demo(tmp_path / str(i), 30.0, out=io.StringIO(), clock=clock, news=False)
        try:
            pipeline.run_simulation(runtime, clock, hours=5 / 60, step_s=30.0, moves_every_s=every)
            bodies.append(json.dumps(runtime.app.paper(), sort_keys=True, default=str))
        finally:
            runtime.close()
    assert bodies[0] == bodies[1]


# --------------------------------------------------------------------------- backtest vs the paper runs (lookahead-1)


def test_lookahead_1_overlap_is_the_union_of_every_paper_run_inside_the_window() -> None:
    H = 3600.0
    assert pipeline.overlap_seconds([], 0.0, 10 * H) == 0.0
    # two overlapping runs count once; a run outside the window does not count; a clipped run counts its part
    runs = [(1 * H, 3 * H), (2 * H, 4 * H), (20 * H, 21 * H), (-5 * H, 0.5 * H)]
    assert pipeline.overlap_seconds(runs, 0.0, 10 * H) == pytest.approx(3.5 * H)
    assert pipeline.overlap_seconds([(0.0, float("inf"))], 2 * H, 5 * H) == pytest.approx(3 * H)


def test_lookahead_1_paper_run_intervals_from_the_store(tmp_path: Path) -> None:
    st = TrackerStore(tmp_path / "t.sqlite3")
    try:
        assert pipeline.paper_run_intervals(st, now=NOW) == []
        st.paper_save_run("done", NOW - 7200, {}, {}, NOW - 7100)
        st.paper_end_run("done", NOW - 3600)  # completed / reset / settings changed: all end the same way
        st.paper_save_run("open", NOW - 1800, {}, {}, NOW - 60)
        assert pipeline.paper_run_intervals(st, now=NOW) == [(NOW - 7200, NOW - 3600), (NOW - 1800, NOW)]
        assert pipeline.paper_run_intervals(st, now=None) == [(NOW - 7200, NOW - 3600), (NOW - 1800, NOW - 60)]
        assert pipeline.paper_run_intervals(SimpleNamespace(), now=NOW) == []  # a source without paper runs
    finally:
        st.close()


def test_lookahead_1_apply_paper_overlap_sets_the_hours_and_one_warning() -> None:
    from supermarket_bot.backtest import OVERLAP_WARNING

    H = 3600.0
    report = SimpleNamespace(window={"start": 0.0, "end": 2 * H}, overlap_hours=None,
                             warnings=["Only 2.0 hours of data in this window: too little for a verdict."],
                             coverage={"book_snapshot_share": 0.4})
    pipeline.apply_paper_overlap(report, [(0.0, 2 * H)])  # a completed run over the whole window
    assert report.overlap_hours == pytest.approx(2.0)
    assert report.warnings[0] == OVERLAP_WARNING.format(h=2.0) + " " + pipeline.OVERLAP_BOOKS_NOTE
    assert len(report.warnings) == 2
    # idempotent, and replaces a smaller overlap the replay computed from the current run alone
    report.warnings.append(OVERLAP_WARNING.format(h=0.1))
    pipeline.apply_paper_overlap(report, [(0.0, 2 * H)])
    assert sum("not an independent check" in w for w in report.warnings) == 1 and report.overlap_hours == 2.0
    clean = SimpleNamespace(window={"start": 0.0, "end": H}, overlap_hours=None, warnings=[], coverage={})
    pipeline.apply_paper_overlap(clean, [(5 * H, 6 * H)])
    assert clean.overlap_hours == 0.0 and clean.warnings == []
    none = SimpleNamespace(window={"start": 0.0, "end": H}, overlap_hours=None, warnings=[], coverage={})
    pipeline.apply_paper_overlap(none, [])
    assert none.overlap_hours is None  # no paper run stored: nothing to compare with
