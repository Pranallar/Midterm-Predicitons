"""Start the demo dashboard on a free local port for browser tests.

Usage::

    python3 tests/e2e/serve_demo.py [--interval 2] [--port 0] [--moves-poll 5] [--no-moves]

The first line written to stdout is the dashboard URL (for example ``http://127.0.0.1:53817``).
The server runs on the simulated market (no API key, no network) with a short snapshot
interval, and stops cleanly when stdin reaches EOF (the parent process closed the pipe or
exited) or on SIGTERM / SIGINT. Its data directory is a temporary folder removed on exit.

Outside-move alerts (docs/OUTSIDE_MOVES.md §19.6) are on by default with a 5-s outside poll, so the
demo's scripted New Hampshire Senate (D) move shows a lagging alert within about 90 s of the start;
``--no-moves`` serves the demo without them.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from supermarket_bot import web  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between demo price snapshots (default 2)")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default 0: pick a free one)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true", help="log the dashboard's warnings to stderr")
    parser.add_argument("--moves-poll", type=float, default=5.0, help="seconds between outside-move polls (default 5)")
    parser.add_argument("--no-moves", action="store_true", help="serve the demo without outside-move alerts")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, stream=sys.stderr)
    data_dir = Path(tempfile.mkdtemp(prefix="supermarket-e2e-"))
    stop = threading.Event()
    runtime = None
    server = None
    serving = False
    try:
        moves_kw = {} if args.no_moves else {"moves": True, "moves_poll_s": args.moves_poll}
        runtime = web.build_demo(data_dir, interval=args.interval, out=sys.stderr, **moves_kw)
        server = web.make_server(runtime.app, args.host, args.port)
        runtime.start()
        serve = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="e2e-http", daemon=True)
        serve.start()
        serving = True

        def _stop(signum: int, frame: Any) -> None:
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):  # not in the main thread / unsupported
                pass

        def _watch_stdin() -> None:
            try:
                while sys.stdin.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            stop.set()

        threading.Thread(target=_watch_stdin, name="e2e-stdin", daemon=True).start()
        print(server.url, flush=True)
        while not stop.wait(0.5):
            pass
        return 0
    finally:
        if server is not None:
            if serving:
                server.shutdown()  # only valid once serve_forever is running (it blocks otherwise)
            server.server_close()
        if runtime is not None:
            runtime.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
