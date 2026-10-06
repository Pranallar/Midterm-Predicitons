"""Store schema v2 (docs/PAPER_TRADING.md §7.1, §7.8): migration, the replay-history methods, pruning, leases,
the read-only connection and the paper-persistence contract against ``paper.MemoryPaperPersistence``.

Offline, in-memory or in ``tmp_path``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest

from supermarket_bot import store as store_mod
from supermarket_bot.backtest import HistorySource
from supermarket_bot.models import (
    BookObservation,
    EquityPoint,
    FairValueRecord,
    FairValueRefresh,
    LeaderboardSnapshot,
    PaperFill,
    PaperOrder,
    PaperTrade,
    SettlementInfo,
    SignalEvent,
    Surge,
    iso_ts,
)
from supermarket_bot.paper import MemoryPaperPersistence
from supermarket_bot.store import (
    BOOK_SNAPSHOT_DEPTH,
    PAPER_RUN_RETENTION_S,
    SCHEMA_VERSION,
    TICK_RETENTION_S,
    TrackerStore,
)

T = 1_791_000_000.0
DAY = 86400.0


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(T)


@pytest.fixture
def st(clock: Clock) -> TrackerStore:
    s = TrackerStore(":memory:", clock=clock)
    yield s
    s.close()


def _market(mid: str, eids: List[str]) -> Dict[str, Any]:
    return {"id": mid, "title": f"Market {mid}", "status": "open", "settlementDate": "2026-11-04T04:59:00.000Z",
            "categories": ["Election Outcome"], "isMultiOutcome": len(eids) > 1,
            "exchanges": [{"id": e, "option": "YES", "initialPrice": 0.5} for e in eids]}


# --------------------------------------------------------------------------- schema and migration


def test_schema_v2_tables_and_version(st: TrackerStore) -> None:
    assert st.schema_version == SCHEMA_VERSION == 3  # v3 (docs/OUTSIDE_MOVES.md §14) keeps every v2 table
    tables = {r[0] for r in st._query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"fair_values", "fair_value_refreshes", "book_snapshots", "settlements", "leaderboard_snapshots", "leases",
            "paper_runs", "paper_orders", "paper_fills", "paper_trades", "paper_equity", "paper_events"} <= tables
    cols = {r[1] for r in st._query("PRAGMA table_info(trades)")}
    assert "fetched_at" in cols


def test_v1_database_migrates_to_v3_keeping_its_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript(store_mod._SCHEMA_V1 + "\nPRAGMA user_version = 1;")
    conn.execute("INSERT INTO ticks VALUES ('e1', ?, 0.5, 0.49, 0.51)", (T,))
    conn.execute("INSERT INTO trades (trade_id, exchange_id, ts, price, size, side) VALUES ('t1', 'e1', ?, 0.5, 10, 'YES')", (T,))
    conn.execute("INSERT INTO state VALUES ('k', '{\"a\": 1}')")
    conn.commit()
    conn.close()

    st = TrackerStore(path, clock=Clock(T + 100))
    try:
        assert st.schema_version == 3
        assert st.tick_count() == 1 and st.get_state("k") == {"a": 1}
        [old] = st.trades("e1", T - 1)
        assert old.trade_id == "t1" and old.fetched_at is None  # stored before v2: never replayed
        st.add_trades("e1", [{"id": "t2", "createdAt": iso_ts(T + 50), "price": 0.51, "size": 5, "side": "no"}])
        new = {t.trade_id: t for t in st.trades("e1", T - 1)}
        assert new["t2"].fetched_at == T + 100 and new["t2"].side == "NO"
        assert st.paper_load_run(None) is None and st.settlements() == {}
    finally:
        st.close()
    again = TrackerStore(path)  # re-opening a v3 database is a no-op
    try:
        assert again.schema_version == 3 and again.tick_count() == 1
    finally:
        again.close()


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        TrackerStore(path)


# --------------------------------------------------------------------------- trades, surges, candles, bounds


def test_add_trades_fetched_at_explicit_default_and_first_kept(st: TrackerStore, clock: Clock) -> None:
    rows = [{"id": "a", "createdAt": "2026-10-02T00:00:00.000Z", "price": 0.4, "size": 3, "side": "YES"},
            {"id": "b", "createdAt": iso_ts(T - 10), "price": 0.41, "size": 4, "side": "NO"}]
    assert st.add_trades("e1", rows, fetched_at=T - 5) == 2
    clock.now = T + 60
    assert st.add_trades("e1", rows + [{"id": "c", "createdAt": iso_ts(T + 30), "price": 0.42, "size": 1, "side": "YES"}]) == 1
    got = {t.trade_id: t.fetched_at for t in st.trades("e1", 0)}
    assert got == {"a": T - 5, "b": T - 5, "c": T + 60}  # a print keeps the time it was first fetched
    assert [t.trade_id for t in st.trades("e1", T - 20, T)] == ["b"]


def test_surges_until_filters_by_detection_time(st: TrackerStore) -> None:
    def surge(eid: str, detected: float) -> Surge:
        return Surge(eid, "m1", "1h", 3600.0, detected - 600, detected - 60, 0.4, 0.55, 0.15, "up", 0.56, detected)

    st.record_surge(surge("e1", T - 100))
    st.record_surge(surge("e2", T + 100))
    assert {s.exchange_id for s in st.surges()} == {"e1", "e2"}
    assert [s.exchange_id for s in st.surges(until=T)] == ["e1"]
    assert [s.exchange_id for s in st.surges(since=T - 200, until=T + 200)] == ["e2", "e1"]


def test_candles_by_bucket_start_with_close_ts(st: TrackerStore) -> None:
    candles = [{"time": f"2026-10-02T0{h}:00:00.000Z", "open": 0.4, "high": 0.5, "low": 0.3, "close": 0.45, "vwap": 0.44,
                "volume": 10, "tradeCount": 2} for h in range(3)]
    st.add_candles("e1", "1h", candles)
    start = 1_791_244_800.0 - 4 * DAY  # 2026-10-02T00:00Z
    got = st.candles("e1", "1h", start)
    assert [c["ts"] for c in got] == [start, start + 3600, start + 7200]
    assert got[0]["close_ts"] == start + 3600 and got[0]["trade_count"] == 2 and got[0]["vwap"] == 0.44
    assert [c["ts"] for c in st.candles("e1", "1h", start + 1, start + 3600)] == [start + 3600]
    assert st.candles("e1", "5m", start) == []


def test_tick_bounds(st: TrackerStore) -> None:
    assert st.tick_bounds() is None
    st.add_ticks(T, [{"exchange_id": "e1", "latest_price": 0.5}])
    st.add_ticks(T + 30, [{"exchange_id": "e1", "latest_price": 0.51}])
    assert st.tick_bounds() == (T, T + 30)


# --------------------------------------------------------------------------- fair values, books, settlements, leaderboard


def test_fair_values_upsert_and_history(st: TrackerStore) -> None:
    recs = [
        FairValueRecord(ts=T, exchange_id="e2", value=0.6, source="polymarket", bid=0.59, ask=0.61, confidence="high",
                        usable=True, agreement=None, detail={"uncertainty": 0.01}, as_of=T - 2, venues=["polymarket"]),
        FairValueRecord(ts=T, exchange_id="e1", value=0.4, source="blend", venues=["polymarket", "kalshi"]),
        FairValueRecord(ts=T + 60, exchange_id="e1", value=0.41, source="blend"),
    ]
    assert st.add_fair_values(recs) == 3
    assert st.add_fair_values([{"ts": T, "exchange_id": "e1", "value": 0.42, "source": "blend"}]) == 1  # upsert
    hist = st.fair_value_history(T - 1)
    assert [(r.ts, r.exchange_id, r.value) for r in hist] == [(T, "e1", 0.42), (T, "e2", 0.6), (T + 60, "e1", 0.41)]
    e2 = hist[1]
    assert e2.usable is True and e2.confidence == "high" and e2.detail == {"uncertainty": 0.01} and e2.as_of == T - 2
    assert e2.venues == ["polymarket"]
    assert [r.exchange_id for r in st.fair_value_history(T - 1, T, exchange_ids=["e2"])] == ["e2"]
    assert [r.ts for r in st.fair_value_history(T + 1)] == [T + 60]
    assert st.fair_value_source_stats("blend") == {"records": 2, "first": T, "last": T + 60}
    assert st.fair_value_source_stats("history")["records"] == 0


def test_fair_value_refreshes_oldest_first(st: TrackerStore) -> None:
    st.add_fair_value_refresh(FairValueRefresh(ts=T + 60, venues={"kalshi": {"status": "ok", "fetched_at": T + 59}}))
    st.add_fair_value_refresh({"ts": T, "venues": {"polymarket": {"status": "ok", "fetched_at": T - 1}}})
    got = st.fair_value_refreshes(T - 1)
    assert [r.ts for r in got] == [T, T + 60] and got[1].venues["kalshi"]["status"] == "ok"
    assert [r.ts for r in st.fair_value_refreshes(T - 1, T)] == [T]


def test_book_snapshots_from_dicts_and_observations(st: TrackerStore) -> None:
    deep = [(round(0.5 - 0.005 * i, 3), 10.0) for i in range(30)]
    n = st.add_book_snapshots([
        BookObservation("e1", T, bids=deep, asks=[(0.52, 5.0)], source="paper", sequence=7),
        {"exchange_id": "e1", "ts": T - 30, "source": "tracker", "bids": [[0.49, 3]], "asks": [[0.51, 4]], "sequence": None},
    ])
    assert n == 2
    got = st.book_snapshots("e1", T - 60)
    assert [b.observed_at for b in got] == [T - 30, T]
    assert len(got[1].bids) == BOOK_SNAPSHOT_DEPTH and got[1].bids[0] == (0.5, 10.0) and got[1].sequence == 7
    assert got[0].source == "tracker" and got[0].asks == [(0.51, 4.0)]
    assert [b.observed_at for b in st.book_snapshots("e1", T - 60, T - 1)] == [T - 30]
    assert st.book_snapshots("e2", 0) == []


def test_settlements_keep_the_first_detection(st: TrackerStore) -> None:
    st.record_settlements([SettlementInfo("e1", "m1", "YES", settled_on=T - 600, payout_yes=1.0, detected_at=T)])
    st.record_settlements([SettlementInfo("e1", "m1", "YES", settled_on=T - 600, payout_yes=1.0, detected_at=T + 600),
                           {"exchange_id": "e2", "market_id": "m2", "settled_with": "REFUND", "refund": True,
                            "detected_at": T + 600}])
    got = st.settlements()
    assert got["e1"].detected_at == T and got["e1"].payout_yes == 1.0
    assert got["e2"].refund is True and got["e2"].payout_yes is None and got["e2"].settled_with == "REFUND"


def test_leaderboard_snapshots(st: TrackerStore) -> None:
    entries = [{"rank": 1, "username": "a", "pnl": 5000.0, "value": 105_000.0}]
    st.add_leaderboard_snapshot(LeaderboardSnapshot(at=T, total=120, my_rank=17, initial_balance=100_000.0, entries=entries))
    st.add_leaderboard_snapshot({"at": T + 300, "period": "all", "entries": []})
    got = st.leaderboard_snapshots(T - 1)
    assert [s.at for s in got] == [T, T + 300]
    assert got[0].entries == entries and got[0].my_rank == 17 and got[0].total == 120 and got[0].initial_balance == 100_000.0
    assert [s.at for s in st.leaderboard_snapshots(T - 1, T)] == [T]


def test_the_store_is_a_backtest_history_source(st: TrackerStore) -> None:
    for name in [n for n in dir(HistorySource) if not n.startswith("_")]:
        assert callable(getattr(st, name)), name


# --------------------------------------------------------------------------- pruning


def test_pruning_drops_old_history_and_runs_ended_long_ago(st: TrackerStore) -> None:
    now = T + 40 * DAY
    old, recent = now - TICK_RETENTION_S - 60, now - 60
    st.add_fair_values([FairValueRecord(ts=old, exchange_id="e1", value=0.4, source="blend"),
                        FairValueRecord(ts=recent, exchange_id="e1", value=0.5, source="blend")])
    st.add_fair_value_refresh(FairValueRefresh(ts=old))
    st.add_fair_value_refresh(FairValueRefresh(ts=recent))
    st.add_book_snapshots([BookObservation("e1", old), BookObservation("e1", recent)])
    st.add_ticks(old, [{"exchange_id": "e1", "latest_price": 0.4}])
    st.add_ticks(now, [{"exchange_id": "e1", "latest_price": 0.5}])
    for rid, started, ended in (("old", now - 40 * DAY, now - PAPER_RUN_RETENTION_S - 60),
                                ("ended-recently", now - 5 * DAY, now - 4 * DAY), ("open", now - DAY, None)):
        st.paper_save_run(rid, started, {"x": 1}, {"y": 2}, started)
        st.paper_add_fills(rid, [{"fill_id": f"{rid}-f", "portfolio_id": "p", "ts": started}])
        st.paper_put_events(rid, [{"event_id": f"{rid}-e", "kind": "value", "t0": started}])
        if ended is not None:
            st.paper_end_run(rid, ended)
    st.prune(now)
    assert [r.ts for r in st.fair_value_history(0)] == [recent]
    assert [r.ts for r in st.fair_value_refreshes(0)] == [recent]
    assert [b.observed_at for b in st.book_snapshots("e1", 0)] == [recent]
    assert st.tick_bounds() == (now, now)
    assert {r["run_id"] for r in st.paper_runs()} == {"ended-recently", "open"}
    assert st.paper_fills("old") == [] and st.paper_events("old") == []
    assert len(st.paper_fills("ended-recently")) == 1


# --------------------------------------------------------------------------- leases


def test_leases_acquire_renew_block_take_over_and_release(st: TrackerStore) -> None:
    me = {"owner_id": "host:1:a", "pid": 1, "host": "host"}
    other = {"owner_id": "laptop:1234:b", "pid": 1234, "host": "my-laptop"}
    assert st.acquire_lease("tracker", me, T, ttl_s=90) is None
    assert st.acquire_lease("tracker", me, T + 30, ttl_s=90) is None  # the same owner renews
    holder = st.acquire_lease("tracker", other, T + 60, ttl_s=90)
    # another machine (or a holder whose state cannot be checked): held while its heartbeat is fresh
    assert holder == {"owner_id": "host:1:a", "pid": 1, "host": "host", "heartbeat_at": T + 30, "alive": None}
    assert st.renew_lease("tracker", "host:1:a", T + 100) is True
    assert st.renew_lease("tracker", "laptop:1234:b", T + 100) is False
    assert st.acquire_lease("tracker", other, T + 100 + 91, ttl_s=90) is None  # a stale heartbeat is taken over
    assert st.lease("tracker")["owner_id"] == "laptop:1234:b"
    assert st.renew_lease("tracker", "host:1:a", T + 200) is False  # ours was taken over
    st.release_lease("tracker", "host:1:a")  # not ours: no effect
    assert st.lease("tracker") is not None
    st.release_lease("tracker", "laptop:1234:b")
    assert st.lease("tracker") is None
    assert st.acquire_lease("tracker", me, T + 300, ttl_s=90) is None


def _dead_pid() -> int:
    """The pid of a process that has just exited (reaped: it no longer exists)."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


@pytest.mark.skipif(__import__("os").name != "posix", reason="pid liveness is checked on POSIX only")
def test_live_5_a_fresh_lease_of_a_dead_process_on_this_machine_is_taken_over(st: TrackerStore) -> None:
    """live-5: after a crash or a closed terminal the lease's heartbeat is still fresh, but its pid (this host) is
    gone: the next start takes it over at once instead of refusing for 90 s."""
    import os
    import socket

    host = socket.gethostname()
    pid = _dead_pid()
    crashed = {"owner_id": f"{host}:{pid}:{T - 100:.6f}:1", "pid": pid, "host": host}
    assert st.acquire_lease("tracker", crashed, T, ttl_s=90) is None
    me = {"owner_id": f"{host}:{os.getpid()}:{T:.6f}:2", "pid": os.getpid(), "host": host}
    assert st.acquire_lease("tracker", me, T + 5, ttl_s=90) is None  # 5 s later: heartbeat fresh, pid dead
    assert st.lease("tracker")["owner_id"] == me["owner_id"]
    assert st.last_takeover is not None and st.last_takeover["pid"] == pid and st.last_takeover["alive"] is False


@pytest.mark.skipif(__import__("os").name != "posix", reason="pid liveness is checked on POSIX only")
def test_live_1_a_running_process_on_this_machine_keeps_its_lease_whatever_the_heartbeat_age(st: TrackerStore) -> None:
    """live-1: a tracker whose cycle sits in client retries is alive: a second start is refused even when the
    heartbeat is older than the TTL (up to LEASE_LIVE_HOLDER_MAX_S), with ``alive`` True."""
    import socket
    import subprocess
    import sys
    import time as _time

    host = socket.gethostname()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        holder = {"owner_id": f"{host}:{proc.pid}:{_time.time() + 5:.6f}:1", "pid": proc.pid, "host": host}
        assert st.acquire_lease("tracker", holder, T, ttl_s=90) is None
        other = {"owner_id": "x:1:2:3", "pid": 999999, "host": host}
        busy = st.acquire_lease("tracker", other, T + 206, ttl_s=90)  # 206 s without a heartbeat (TTL 90 s)
        assert busy is not None and busy["pid"] == proc.pid and busy["alive"] is True
        late = T + store_mod.LEASE_LIVE_HOLDER_MAX_S + 1  # hung for longer than that: it cannot lock the store
        assert st.acquire_lease("tracker", other, late, ttl_s=90) is None
    finally:
        proc.kill()
        proc.wait(timeout=30)


@pytest.mark.skipif(__import__("os").name != "posix", reason="pid liveness is checked on POSIX only")
def test_live_5_a_recycled_pid_does_not_hold_a_lease(st: TrackerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pid that now belongs to a process started after the lease's tracker was created is not that tracker."""
    import os
    import socket

    host = socket.gethostname()
    ppid = os.getppid()  # alive, and started before this test
    started = store_mod._process_started_at(ppid)
    if started is None:
        pytest.skip("no /proc start times here")
    holder = {"owner_id": f"{host}:{ppid}:{started - 3600:.6f}:1", "pid": ppid, "host": host}  # made before it started
    assert store_mod.lease_holder_alive(holder) is False
    holder["owner_id"] = f"{host}:{ppid}:{started + 10:.6f}:1"
    assert store_mod.lease_holder_alive(holder) is True
    assert store_mod.lease_holder_alive({"pid": os.getpid(), "host": host}) is None  # this very process
    assert store_mod.lease_holder_alive({"pid": ppid, "host": "some-other-machine"}) is None


def test_lookahead_1_paper_run_times_lists_every_run_without_decoding(st: TrackerStore) -> None:
    st.paper_save_run("a", T, {"big": "config"}, {"big": "state"}, T + 10)
    st.paper_end_run("a", T + 3600)
    st.paper_save_run("b", T + 7200, {}, {}, T + 7300)
    assert st.paper_run_times() == [
        {"run_id": "a", "started_at": T, "updated_at": T + 10, "ended_at": T + 3600},
        {"run_id": "b", "started_at": T + 7200, "updated_at": T + 7300, "ended_at": None},
    ]


# --------------------------------------------------------------------------- read-only connection


def test_open_read_only_reads_but_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite3"
    live = TrackerStore(path)
    try:
        live.upsert_markets([_market("m1", ["e1"])])
        live.add_ticks(T, [{"exchange_id": "e1", "latest_price": 0.5, "best_bid": 0.49, "best_ask": 0.51}])
        ro = TrackerStore.open_read_only(path)
        try:
            assert ro.read_only is True and ro.tick_bounds() == (T, T)
            assert [e.exchange_id for e in ro.exchanges()] == ["e1"]
            assert [p.price for p in ro.series("e1", T - 1)] == [0.5]
            with pytest.raises(sqlite3.OperationalError):
                ro.add_ticks(T + 30, [{"exchange_id": "e1", "latest_price": 0.6}])
            with pytest.raises(sqlite3.OperationalError):
                ro.set_state("k", 1)
            live.add_ticks(T + 60, [{"exchange_id": "e1", "latest_price": 0.55}])  # the live connection still writes
            assert ro.tick_bounds() == (T, T + 60)
        finally:
            ro.close()
    finally:
        live.close()
    assert TrackerStore(path).tick_count() == 2


def test_open_read_only_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        TrackerStore.open_read_only(tmp_path / "missing.sqlite3")
    with pytest.raises(ValueError):
        TrackerStore.open_read_only(":memory:")
    old = tmp_path / "v1.sqlite3"
    conn = sqlite3.connect(str(old))
    conn.executescript(store_mod._SCHEMA_V1 + "\nPRAGMA user_version = 1;")
    conn.close()
    with pytest.raises(RuntimeError, match="schema version 1"):
        TrackerStore.open_read_only(old)


# --------------------------------------------------------------------------- the PaperPersistence contract


def _order(oid: str, created: float, status: str = "pending", pid: str = "kind:value") -> Dict[str, Any]:
    return PaperOrder(order_id=oid, portfolio_id=pid, idea_id="value:x9001:yes", kind="value", purpose="entry",
                      order_type="taker", exchange_id="9001", market_id="301", side="yes", action="buy", qty=100.0,
                      limit_price=0.55, created_at=created, status=status).to_dict()


def _fill(fid: str, ts: float, pid: str = "kind:value") -> Dict[str, Any]:
    return PaperFill(fill_id=fid, order_id="o1", portfolio_id=pid, exchange_id="9001", market_id="301", side="yes",
                     action="buy", qty=50.0, price=0.55, ts=ts, liquidity="taker", purpose="entry", kind="value",
                     idea_id="value:x9001:yes", levels=[(0.55, 50.0)]).to_dict()


def _trade(tid: str, closed: float, pid: str = "kind:value") -> Dict[str, Any]:
    return PaperTrade(trade_id=tid, portfolio_id=pid, idea_id="i", kind="value", exchange_ids=["9001"], opened_at=closed - 600,
                      closed_at=closed, qty=50.0, cost=27.5, proceeds=30.0, pnl=2.5, exit_reason="target").to_dict()


def _equity(pid: str, ts: float, cash: float) -> Dict[str, Any]:
    return EquityPoint(portfolio_id=pid, ts=ts, cash=cash, reserved_cash=0.0, liq_value=cash + 1.5, mark_value=cash + 2.0,
                       fv_value=None if pid == "kind:hole" else cash + 3.0, open_positions=1).to_dict()


def _event(eid: str, t0: float, kind: str = "value") -> Dict[str, Any]:
    return SignalEvent(event_id=eid, idea_id=eid.split("@")[0], kind=kind, t0=t0, exchange_ids=["9001"], sides=["yes"],
                       entry=0.5, outcomes={"5m": 0.01, "30m": None}).to_dict()


def _script(p: Any) -> List[Any]:
    """The same sequence of PaperPersistence calls; returns every read's result."""
    out: List[Any] = []
    out.append(p.paper_load_run(None))
    out.append(p.paper_last_ended_run())
    p.paper_save_run("run-1", T, {"fingerprint": "a", "paper": {"sizing": "conservative"}}, {"steps": 0}, T)
    p.paper_save_run("run-1", T, {"fingerprint": "a", "paper": {"sizing": "conservative"}}, {"steps": 3}, T + 90)
    p.paper_put_orders("run-1", [_order("o1", T + 10), _order("o2", T + 10), _order("o3", T + 5, "resting", "kind:hole")])
    p.paper_put_orders("run-1", [_order("o1", T + 10, "filled")])  # upsert keeps its place
    p.paper_add_fills("run-1", [_fill("f1", T + 30), _fill("f2", T + 30), _fill("f3", T + 20, "kind:basket")])
    p.paper_add_fills("run-1", [_fill("f1", T + 30)])  # re-sent: no duplicate
    p.paper_add_trades("run-1", [_trade("t1", T + 60), _trade("t2", T + 60, "kind:hole"), _trade("t3", T + 50)])
    p.paper_add_equity("run-1", [_equity("kind:value", T, 100_000.0), _equity("kind:hole", T, 100_000.0),
                                 _equity("kind:value", T + 60, 99_990.0)])
    p.paper_add_equity("run-1", [_equity("kind:value", T + 60, 99_995.0)])  # same (portfolio, ts): replaced
    p.paper_put_events("run-1", [_event("b@2", T + 30, "basket"), _event("a@1", T + 30), _event("c@0", T)])
    p.paper_put_events("run-1", [dict(_event("a@1", T + 30), traded=True)])
    out.append(p.paper_load_run(None))
    out.append(p.paper_load_run("run-1"))
    out.append(p.paper_load_run("nope"))
    out.append(p.paper_orders("run-1"))
    out.append(p.paper_orders("run-1", statuses=["pending", "resting"]))
    out.append(p.paper_orders("run-1", statuses=[]))
    out.append(p.paper_fills("run-1"))
    out.append(p.paper_fills("run-1", portfolio_id="kind:value", limit=1))
    out.append(p.paper_fills("run-1", limit=None))
    out.append(p.paper_trades("run-1"))
    out.append(p.paper_trades("run-1", portfolio_id="kind:hole"))
    out.append(p.paper_trades("run-1", limit=2))
    out.append(p.paper_equity("run-1"))
    out.append(p.paper_equity("run-1", portfolio_id="kind:value", since=T + 1))
    out.append(p.paper_events("run-1"))
    out.append(p.paper_events("run-1", kind="value", limit=1))
    p.paper_end_run("run-1", T + 3600)
    p.paper_save_run("run-2", T + 3600, {"fingerprint": "b"}, {"steps": 0}, T + 3600)
    p.paper_save_run("run-3", T + 3600, {"fingerprint": "c"}, {"steps": 0}, T + 3600)  # same start: the later save wins
    out.append(p.paper_load_run(None))
    out.append(p.paper_last_ended_run())
    p.paper_end_run("run-3", T + 7200)
    p.paper_end_run("run-2", T + 7200)
    out.append(p.paper_last_ended_run())
    p.paper_save_run("run-3", T + 3600, {"fingerprint": "c"}, {"steps": 9}, T + 7300)  # a save keeps ended_at
    out.append(p.paper_load_run("run-3"))
    out.append(p.paper_load_run(None))
    out.append(p.paper_fills("run-2"))
    out.append(p.add_book_snapshots([BookObservation("9001", T, bids=[(0.5, 1.0)], asks=[(0.51, 2.0)])]))
    return out


def test_contract_store_and_memory_persistence_agree(st: TrackerStore) -> None:
    memory = _script(MemoryPaperPersistence())
    sql = _script(st)
    assert len(memory) == len(sql)
    for i, (a, b) in enumerate(zip(memory, sql)):
        assert a == b, f"read {i} differs: {a!r} != {b!r}"


def test_paper_runs_lists_every_run(st: TrackerStore) -> None:
    st.paper_save_run("b", T + 10, {}, {}, T + 10)
    st.paper_save_run("a", T, {}, {}, T)
    assert [r["run_id"] for r in st.paper_runs()] == ["a", "b"]
    assert set(st.paper_runs()[0]) == {"run_id", "started_at", "config", "state", "updated_at", "ended_at"}


def test_paper_rows_accept_dataclasses(st: TrackerStore) -> None:
    order = PaperOrder(order_id="o9", portfolio_id="p", idea_id="i", kind="hole", purpose="entry", order_type="maker",
                       exchange_id="9033", market_id="325", side="yes", action="buy", qty=10.0, limit_price=0.62,
                       created_at=T, status="resting")
    st.paper_put_orders("r", [order])  # type: ignore[list-item]
    assert st.paper_orders("r") == [order.to_dict()]


def test_store_clock_stamps_lease_free_defaults(clock: Clock) -> None:
    st = TrackerStore(clock=clock)
    try:
        clock.now = T + 5
        st.add_trades("e1", [{"id": "x", "createdAt": iso_ts(T), "price": 0.5, "size": 1, "side": "YES"}])
        assert st.trades("e1", 0)[0].fetched_at == T + 5
        st.add_ticks(T, [{"exchange_id": "e1", "latest_price": 0.5}])
        clock.now = T + 99
        st.prune()  # defaults to the newest tick, not the clock
        assert st.tick_count() == 1
    finally:
        st.close()
