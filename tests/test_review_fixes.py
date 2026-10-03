"""Regression tests for fixes made after the multi-agent review."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import httpx
import pytest

from conftest import API_KEY, BASE_URL, TOURNAMENT_SLUG, error_body, market, market_page, price, tournament

from supermarket_bot.bot import Context, MarketDataBot, SnapshotWriter, advance_reference, atomic_write, diff_rows, is_fatal
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import ConfigError, Settings
from supermarket_bot.errors import ApiError, NetworkError, RequestCancelled

MARKETS_PATH = f"/tournaments/{TOURNAMENT_SLUG}/markets"
CTX = Context.from_tournament(tournament())


def _row(eid: str = "36", latest: float = 0.5) -> dict:
    return {"exchange_id": eid, "latest_price": latest, "best_bid": None, "best_ask": None}


def test_small_steps_accumulate_until_min_move_is_reached() -> None:
    reference = {}
    reported = []
    for p in (0.53, 0.56, 0.59, 0.62):
        rows = [_row(latest=p)]
        changes = diff_rows(reference, rows, min_move=0.05)
        reported.append([round(c["delta"], 3) for c in changes if c.get("delta") is not None])
        reference = advance_reference(reference, rows, changes, min_move=0.05)
    # 0.53 -> 0.59 is the first cumulative move >= 0.05; the baseline then resets to 0.59
    assert reported == [[], [], [0.06], []]


def test_watch_stops_on_revoked_key(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, (401, error_body("API_KEY_REVOKED", "revoked")))
    bot = MarketDataBot(client, CTX, sleep=lambda s: None, clock=clock)
    with pytest.raises(ApiError) as exc:
        bot.watch(interval=1, iterations=5)
    assert exc.value.code == "API_KEY_REVOKED"
    assert len(fake.calls) == 1  # did not keep hammering the API


def test_watch_gives_up_after_three_identical_client_errors(fake, client, clock) -> None:
    fake.add("GET", MARKETS_PATH, (400, error_body("VALIDATION_ERROR", "bad")))
    bot = MarketDataBot(client, CTX, sleep=lambda s: None, clock=clock)
    with pytest.raises(ApiError):
        bot.watch(interval=1, iterations=10)
    assert len(fake.calls) == 3


def test_is_fatal_classification() -> None:
    assert is_fatal(ApiError(401, "INVALID_API_KEY", "x"))
    assert is_fatal(ApiError(403, "TERMS_NOT_ACKNOWLEDGED", "x"))
    assert is_fatal(ApiError(403, "FORBIDDEN", "Confirm the email address"))
    assert is_fatal(RequestCancelled("x"))
    assert not is_fatal(ApiError(503, "SERVICE_UNAVAILABLE", "x"))
    assert not is_fatal(ApiError(429, "RATE_LIMITED", "x"))
    assert not is_fatal(NetworkError("x"))


def test_snapshot_survives_locked_output_file(fake, client, tmp_path, monkeypatch, caplog) -> None:
    fake.add("GET", MARKETS_PATH, market_page([market("26", "Q", [("36", "YES", 0.4)])]))
    fake.add("GET", "/exchanges/prices", {"data": [price("36", "26", 0.4, 0.39, 0.41)], "missingIds": []})
    bot = MarketDataBot(client, CTX, data_dir=tmp_path)

    def locked(*a, **k):
        raise PermissionError("[WinError 32] file in use")

    monkeypatch.setattr("supermarket_bot.bot.os.replace", locked)
    snap = bot.snapshot()
    assert len(snap.rows) == 1
    assert "could not save snapshot" in caplog.text
    assert not list((tmp_path / TOURNAMENT_SLUG).glob("*.tmp"))  # temp files cleaned up


def test_atomic_write_uses_unique_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "latest.csv"
    atomic_write(target, lambda fh: fh.write("a"))
    atomic_write(target, lambda fh: fh.write("b"))
    assert target.read_text() == "b"
    assert [p.name for p in tmp_path.iterdir()] == ["latest.csv"]


def test_snapshot_writer_round_trip(tmp_path: Path) -> None:
    from supermarket_bot.bot import Snapshot, utc_now

    writer = SnapshotWriter(tmp_path, CTX)
    paths = writer.write(Snapshot(utc_now(), CTX, [{"id": "26"}], [{**_row(), "market_id": "26"}]))
    assert paths["csv"].read_text().startswith("taken_at,")
    assert paths["markets"].read_text().strip().startswith("[")


def test_cancel_interrupts_rate_limit_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=error_body("RATE_LIMITED"), headers={"Retry-After": "30"})

    client = SuperMarketClient(API_KEY, BASE_URL, transport=httpx.MockTransport(handler), max_retries=3)
    errors = []

    def worker() -> None:
        try:
            client.get_account()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=worker)
    started = time.monotonic()
    t.start()
    time.sleep(0.2)
    client.cancel()
    t.join(timeout=5)
    assert not t.is_alive()
    assert time.monotonic() - started < 5
    assert isinstance(errors[0], RequestCancelled)
    client.close()


def test_env_example_with_real_key_gets_a_specific_hint(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("SUPERMARKET_API_KEY=ace_real_looking_key_123\n")
    with pytest.raises(ConfigError, match="Your key is in .env.example"):
        Settings.load(env={}, env_file=tmp_path / ".env")


def test_env_example_placeholder_gives_plain_error(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("SUPERMARKET_API_KEY=ace_your_key_here\n")
    with pytest.raises(ConfigError) as exc:
        Settings.load(env={}, env_file=tmp_path / ".env")
    assert ".env.example, which is committed" not in str(exc.value)


def test_decoding_error_becomes_network_error(fake, make_client) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad gzip", request=request)

    fake.add("GET", "/account", broken)
    client = make_client(max_retries=1)
    with pytest.raises(NetworkError):
        client.get_account()
    assert len(fake.calls) == 2  # retried once as an idempotent read


def test_cli_survives_cp1252_output(fake, monkeypatch, tmp_path) -> None:
    from supermarket_bot import cli

    monkeypatch.setenv("SUPERMARKET_API_KEY", API_KEY)
    monkeypatch.setenv("SUPERMARKET_BASE_URL", BASE_URL)
    monkeypatch.delenv("SUPERMARKET_TOURNAMENT", raising=False)
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}", tournament())
    fake.add("GET", "/exchanges/36/price-history", {"candles": [{"time": "2026-10-01T00:00:00Z", "close": 0.4}, {"time": "2026-10-01T01:00:00Z", "close": 0.6}], "coverage": {"complete": True}})
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="replace")
    code = cli.main(["--env-file", str(tmp_path / "none"), "-t", TOURNAMENT_SLUG, "history", "36"], out=out, transport=httpx.MockTransport(fake))
    out.flush()
    assert code == 0
    assert b"Exchange 36" in raw.getvalue()


def test_env_file_with_utf8_bom_and_utf16(tmp_path: Path) -> None:
    from supermarket_bot.config import load_env_file

    bom = tmp_path / "bom.env"
    bom.write_bytes(b"\xef\xbb\xbfSUPERMARKET_API_KEY=ace_bom_key\n")
    assert load_env_file(bom) == {"SUPERMARKET_API_KEY": "ace_bom_key"}
    utf16 = tmp_path / "u16.env"
    utf16.write_bytes("SUPERMARKET_API_KEY=ace_u16_key\r\n".encode("utf-16"))
    assert load_env_file(utf16) == {"SUPERMARKET_API_KEY": "ace_u16_key"}
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"SUPERMARKET_API_KEY=\xff\xff\xfe")
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_env_file(bad)


def test_realtime_frames_hidden_below_vvv() -> None:
    import logging

    from supermarket_bot.cli import configure_logging

    configure_logging(2)
    assert logging.getLogger("realtime").getEffectiveLevel() >= logging.WARNING
    configure_logging(3)
    assert logging.getLogger("realtime").getEffectiveLevel() == logging.DEBUG
    configure_logging(0)
