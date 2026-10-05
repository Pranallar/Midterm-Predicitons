"""Thread safety of the paper trader inside the tracker (docs/PAPER_TRADING.md §6.15, §7.2, D43).

Four threads poll ``paper_view()`` / ``status()`` (and the dashboard's ``paper()`` / ``status()``) while the main
worker runs 200 fast demo steps and one reset: no exception, no deadlock (every call returns within 5 s, and the
steps keep making progress), and the stored paper rows keep unique ids. Offline (the demo's simulated API on a
SimClock).

Each reader polls about 20 times a second (a real dashboard polls every few seconds). Busy-looping readers with
no pause would only measure CPython's GIL hand-off: every SQLite call of the snapshot loop releases the GIL and
then queues behind four CPU-bound threads, which slows the steps 20-fold without any lock being held. So the
deadlock check is progress-based (no step may take longer than ``STALL_LIMIT_S``), not a wall-clock budget for all
200 steps, which would depend on how busy the machine is.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Tuple

from supermarket_bot import pipeline, web
from supermarket_bot.demo import SIM_T0, SimClock

CALL_LIMIT_S = 5.0
STALL_LIMIT_S = 60.0  # no single step (or the reset) may take this long: that would be a deadlock
POLL_PAUSE_S = 0.05
TOTAL_LIMIT_S = 900.0


def test_readers_never_block_or_break_the_paper_steps(tmp_path: Path) -> None:
    clock = SimClock(SIM_T0)
    runtime = web.build_demo(tmp_path, 30.0, out=io.StringIO(), clock=clock, news=False)
    tracker, app = runtime.tracker, runtime.app
    stop = threading.Event()
    errors: List[BaseException] = []
    slowest: List[Tuple[float, str]] = []
    calls = [0]
    reset_ids: List[str] = []
    progress = [0, time.monotonic()]  # steps done, when the last one finished
    original_step = tracker.paper_step

    def counted_step(now: Any = None) -> Any:
        result = original_step(now)
        progress[0] += 1
        progress[1] = time.monotonic()
        return result

    tracker.paper_step = counted_step  # type: ignore[method-assign]

    def reader(name: str, fns: List[Callable[[], Any]]) -> None:
        worst = (0.0, name)
        try:
            while not stop.is_set():
                for fn in fns:
                    started = time.monotonic()
                    result = fn()
                    took = time.monotonic() - started
                    worst = max(worst, (took, f"{name}:{getattr(fn, '__name__', fn)}"))
                    assert result is not None
                    calls[0] += 1
                time.sleep(POLL_PAUSE_S)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            slowest.append(worst)

    readers = [
        threading.Thread(target=reader, args=("t1", [tracker.paper_view, tracker.status]), daemon=True),
        threading.Thread(target=reader, args=("t2", [tracker.status, tracker.paper_view]), daemon=True),
        threading.Thread(target=reader, args=("t3", [app.paper, app.status]), daemon=True),
        threading.Thread(target=reader, args=("t4", [lambda: tracker.paper_view("previous") or {}, app.status]), daemon=True),
    ]

    def drive() -> None:
        try:
            pipeline.run_simulation(runtime, clock, hours=99 * 30 / 3600, step_s=30.0, end_run=False)  # 100 steps
            reset_ids.append(str(tracker.paper_reset(target_hours=24)))
            progress[1] = time.monotonic()
            clock.advance(30)
            pipeline.run_simulation(runtime, clock, hours=99 * 30 / 3600, step_s=30.0, end_run=False)  # 100 more
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=drive, name="paper-driver", daemon=True)
    try:
        for t in readers:
            t.start()
        worker.start()
        deadline = time.monotonic() + TOTAL_LIMIT_S
        while worker.is_alive() and time.monotonic() < deadline:
            started = time.monotonic()
            status = tracker.status()  # polled from the main thread while steps run
            assert time.monotonic() - started < CALL_LIMIT_S
            assert "read_budget" in status
            worker.join(0.5)
            stalled = time.monotonic() - progress[1]
            assert not worker.is_alive() or stalled < STALL_LIMIT_S, (
                f"no paper step finished for {stalled:.0f} s after {progress[0]} steps: deadlock?")
        assert not worker.is_alive(), "the paper steps did not finish: deadlock?"
        assert progress[0] == 200
    finally:
        stop.set()
        for t in readers:
            t.join(10)
    try:
        assert not errors, errors[0]
        assert all(not t.is_alive() for t in readers)
        assert calls[0] > 50
        worst = max(slowest)
        assert worst[0] < CALL_LIMIT_S, worst
        body = tracker.paper_view()
        assert body["run"]["run_id"] == reset_ids[0] and body["run"]["steps"] == 100
        previous = tracker.paper_view("previous")
        assert previous["run"]["end_reason"] == "reset" and previous["run"]["steps"] == 100
        store = runtime.store
        for run in store.paper_runs():
            fills = store.paper_fills(run["run_id"], limit=None)
            assert len({f["fill_id"] for f in fills}) == len(fills)
            orders = store.paper_orders(run["run_id"])
            assert len({o["order_id"] for o in orders}) == len(orders)
        assert sum(p["fills"] for p in body["portfolios"]) == len(store.paper_fills(reset_ids[0], limit=None))
        final = previous["run"]["final"]["portfolios"]
        assert sum(p["fills"] for p in final) == len(store.paper_fills(previous["run"]["run_id"], limit=None))
    finally:
        runtime.close()
