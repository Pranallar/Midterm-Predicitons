"""End-to-end tests for ``supermarket_bot.cli`` driven through ``main(argv, out, transport)``.

Every command runs against the in-memory ``FakeAPI`` (no network). Settings come from the
environment (``SUPERMARKET_API_KEY`` / ``SUPERMARKET_BASE_URL``) with ``--env-file`` pointed at a
non-existent file unless a test writes one. ``main()`` builds its own client, so an autouse
fixture swaps in a subclass whose ``sleep`` only records, keeping retry tests instant.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from conftest import (
    API_KEY,
    BASE_URL,
    TOURNAMENT_ID,
    TOURNAMENT_SLUG,
    book_exchange,
    error_body,
    market,
    market_orderbook,
    market_page,
    price,
    tournament,
    tournament_page,
)
from supermarket_bot import __version__, cli
from supermarket_bot.client import SuperMarketClient
from supermarket_bot.config import mask_key

ENV_VARS = (
    "SUPERMARKET_API_KEY",
    "SUPERMARKET_BASE_URL",
    "SUPERMARKET_TOURNAMENT",
    "SUPERMARKET_DATA_DIR",
    "SUPERMARKET_READS_PER_MIN",
    "SUPERMARKET_WRITES_PER_MIN",
)
T_PATH = f"/tournaments/{TOURNAMENT_SLUG}"
T_MARKETS = f"/tournaments/{TOURNAMENT_SLUG}/markets"
HEADER = "SIG Predictions Cup [predictions-cup] · SIG Coins"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPERMARKET_API_KEY", API_KEY)
    monkeypatch.setenv("SUPERMARKET_BASE_URL", BASE_URL)
    monkeypatch.chdir(tmp_path)  # no stray ./.env or ./data from the repo


@pytest.fixture(autouse=True)
def client_sleeps(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    """Make the client main() builds record sleeps instead of really sleeping."""
    sleeps: List[float] = []

    class RecordingSleepClient(SuperMarketClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("sleep", sleeps.append)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cli, "SuperMarketClient", RecordingSleepClient)
    return sleeps


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def run(fake, tmp_path: Path, data_dir: Path):
    def _run(*argv: str, env_file: Optional[Path] = None, data: Optional[Path] = None) -> Tuple[int, str]:
        out = io.StringIO()
        args = ["--env-file", str(env_file or tmp_path / "missing.env"), "--data-dir", str(data or data_dir), *argv]
        code = cli.main(args, out=out, transport=httpx.MockTransport(fake))
        return code, out.getvalue()

    return _run


@pytest.fixture
def cup(fake):
    """Explicit tournament lookup for ``--tournament predictions-cup``."""
    fake.add("GET", T_PATH, tournament())
    return fake


def _lines(text: str) -> List[str]:
    return [line.rstrip() for line in text.splitlines()]


def _account() -> Dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "username": "johndoe",
        "email": "johndoe@example.com",
        "createdAt": "2025-01-15T10:00:00.000Z",
        "avatarUrl": None,
        "bio": None,
        "balance": 1200.5,
    }


def _two_outcome_market() -> Dict[str, Any]:
    return market("26", "Who wins?", [("36", "A", 0.4), ("37", "B", 0.6)])


def _trade(tid: str, px: float, second: int, size: int = 10, side: str = "YES") -> Dict[str, Any]:
    return {"id": tid, "createdAt": f"2026-10-03T12:00:0{second}.000Z", "price": px, "size": size, "side": side, "volume": size * px}


def _trade_page(trades: List[Dict[str, Any]], cursor: Optional[str] = None) -> Dict[str, Any]:
    return {
        "exchangeId": "36",
        "marketId": "26",
        "from": "1970-01-01T00:00:00.000Z",
        "to": "2026-10-03T12:00:00.000Z",
        "data": trades,
        "pagination": {"limit": 200, "hasMore": cursor is not None, "nextCursor": cursor},
        "coverage": {"complete": True, "projectedThroughSequence": 5},
    }


def _candles(complete: bool = True) -> Dict[str, Any]:
    return {
        "exchangeId": "36",
        "marketId": "26",
        "resolution": "5m",
        "from": "2026-10-01T00:00:00.000Z",
        "to": "2026-10-01T00:10:00.000Z",
        "candles": [
            {"time": "2026-10-01T00:00:00.000Z", "open": 0.4, "high": 0.45, "low": 0.39, "close": 0.42, "vwap": 0.415, "volume": 1200, "tradeCount": 14},
            {"time": "2026-10-01T00:05:00.000Z", "open": 0.42, "high": 0.5, "low": 0.42, "close": 0.5, "vwap": 0.47, "volume": 300.5, "tradeCount": 3},
        ],
        "coverage": {"complete": complete, "projectedThroughSequence": 184233},
    }


def _leaderboard(period: str = "7d", my_rank: Optional[int] = 3) -> Dict[str, Any]:
    return {
        "leaderboard": [
            {"rank": 1, "profileId": "p1", "username": "alice", "pnl": 150.25, "tradesCount": 12, "volume": 3000, "winRate": 66.67, "roi": 5.01},
            {"rank": 2, "profileId": "p2-profile", "username": None, "pnl": -20, "tradesCount": 3, "volume": 100, "winRate": None, "roi": None},
        ],
        "total": 12,
        "period": period,
        "limit": 20,
        "offset": 0,
        "myRank": my_rank,
        "season": None,
        "boundGroups": [],
        "activeGroupId": None,
    }


# --------------------------------------------------------------------------- settings & exit codes


def test_missing_api_key_exits_2_with_message_and_no_requests(run, fake, monkeypatch, capsys):
    monkeypatch.delenv("SUPERMARKET_API_KEY")
    code, out = run("account")
    err = capsys.readouterr().err
    assert code == 2
    assert out == ""
    assert "error: No API key found" in err
    assert "SUPERMARKET_API_KEY" in err
    assert fake.calls == []


def test_invalid_reads_per_min_exits_2(run, fake, monkeypatch, capsys):
    monkeypatch.setenv("SUPERMARKET_READS_PER_MIN", "lots")
    code, _ = run("account")
    assert code == 2
    assert "SUPERMARKET_READS_PER_MIN must be an integer" in capsys.readouterr().err
    assert fake.calls == []


def test_env_file_supplies_key_base_url_and_tournament(run, fake, monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERMARKET_API_KEY")
    monkeypatch.delenv("SUPERMARKET_BASE_URL")
    env_file = tmp_path / "bot.env"
    env_file.write_text(
        "# comment\nexport SUPERMARKET_API_KEY='ace_fromfile_9876543210'\n"
        f"SUPERMARKET_BASE_URL={BASE_URL}/\nSUPERMARKET_TOURNAMENT=file-cup  # inline comment\n",
        encoding="utf-8",
    )
    fake.add("GET", "/tournaments/file-cup", tournament(slug="file-cup"))
    fake.add("GET", "/tournaments/file-cup/markets", market_page([]))
    code, out = run("markets", env_file=env_file)
    assert code == 0
    assert [c.path for c in fake.calls] == ["/tournaments/file-cup", "/tournaments/file-cup/markets"]
    assert fake.calls[0].request.url.host == "example.test"
    assert fake.calls[0].headers["authorization"] == "Bearer ace_fromfile_9876543210"
    assert "0 market(s)" in out


def test_environment_overrides_env_file(run, fake, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SUPERMARKET_API_KEY=ace_file_key_should_lose\nSUPERMARKET_TOURNAMENT=file-cup\n", encoding="utf-8")
    monkeypatch.setenv("SUPERMARKET_TOURNAMENT", TOURNAMENT_SLUG)
    fake.add("GET", T_PATH, tournament())
    fake.add("GET", T_MARKETS, market_page([]))
    code, _ = run("markets", env_file=env_file)
    assert code == 0
    assert fake.calls[0].path == T_PATH
    assert fake.calls[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_tournament_flag_beats_env_and_resolves_by_slug(run, fake, monkeypatch):
    monkeypatch.setenv("SUPERMARKET_TOURNAMENT", "env-cup")
    fake.add("GET", T_PATH, tournament())
    fake.add("GET", T_MARKETS, market_page([]))
    code, out = run("--tournament", TOURNAMENT_SLUG, "markets")
    assert code == 0
    assert [c.path for c in fake.calls] == [T_PATH, T_MARKETS]
    assert fake.calls_to("/tournaments/env-cup") == []
    assert fake.calls_to("/tournaments") == []  # no auto-discovery when a slug is given
    assert out.splitlines()[0] == HEADER


def test_env_tournament_resolves_via_get_tournament(run, fake, monkeypatch):
    monkeypatch.setenv("SUPERMARKET_TOURNAMENT", TOURNAMENT_SLUG)
    fake.add("GET", T_PATH, tournament())
    fake.add("GET", T_MARKETS, market_page([]))
    code, _ = run("markets")
    assert code == 0
    assert fake.calls[0].path == T_PATH
    assert fake.calls_to("/tournaments") == []


def test_auto_context_uses_the_only_active_tournament(run, fake):
    fake.add("GET", "/tournaments", tournament_page([tournament()]))
    fake.add("GET", T_MARKETS, market_page([]))
    code, out = run("markets")
    assert code == 0
    discovery = fake.calls_to("/tournaments")
    assert len(discovery) == 1 and discovery[0].params["status"] == "active"
    assert fake.calls_to(T_MARKETS)[0].params["status"] == "open"
    assert out.splitlines()[0] == HEADER


def test_public_flag_skips_tournament_lookups(run, fake, monkeypatch):
    monkeypatch.setenv("SUPERMARKET_TOURNAMENT", TOURNAMENT_SLUG)  # ignored with --public
    fake.add("GET", "/markets", market_page([market("5", "Global market", [("9", None, 0.3)])]))
    code, out = run("--public", "markets")
    assert code == 0
    assert [c.path for c in fake.calls] == ["/markets", "/markets"]  # public-context probe, then the listing
    assert fake.calls[0].params == {"limit": "1"}
    assert "tournamentId" not in fake.calls[1].params
    assert fake.calls[1].params["status"] == "open"
    assert out.splitlines()[0] == "Public global markets [public]"


def test_several_active_tournaments_exit_1_listing_slugs(run, fake, capsys):
    fake.add(
        "GET",
        "/tournaments",
        tournament_page([tournament(slug="cup-a", tid="a", name="Cup A"), tournament(slug="cup-b", tid="b", name="Cup B")]),
    )
    code, out = run("markets")
    err = capsys.readouterr().err
    assert code == 1
    assert "2 active tournaments" in err
    assert "cup-a (Cup A)" in err and "cup-b (Cup B)" in err
    assert "--tournament" in err
    assert fake.calls_to("/markets") == [] and out == ""


def test_no_active_tournament_but_several_others_exit_1_with_statuses(run, fake, capsys):
    def by_status(request: httpx.Request) -> httpx.Response:
        if request.url.params["status"] == "active":
            return httpx.Response(200, json=tournament_page([]))
        return httpx.Response(200, json=tournament_page([tournament(slug="old", tid="o", status="ended"), tournament(slug="next", tid="n", status="draft")]))

    fake.add("GET", "/tournaments", by_status)
    code, _ = run("markets")
    err = capsys.readouterr().err
    assert code == 1
    assert "No active tournament" in err
    assert "old (ended)" in err and "next (draft)" in err


def test_no_tournaments_at_all_falls_back_to_public(run, fake):
    fake.add("GET", "/tournaments", tournament_page([]))
    fake.add("GET", "/markets", market_page([]))
    code, out = run("markets")
    assert code == 0
    assert [c.params["status"] for c in fake.calls_to("/tournaments")] == ["active", "any"]
    assert fake.calls_to("/markets")[0].params.get("tournamentId") is None
    assert out.startswith("Public global markets [public]")


def test_api_error_exit_1_prints_message_hint_and_details(run, fake, capsys):
    details = {"required": ["read"], "missing": ["read"]}
    fake.add("GET", "/account", (403, error_body("INSUFFICIENT_SCOPES", "Missing scope", details)))
    code, out = run("account")
    err = capsys.readouterr().err
    assert code == 1
    assert out == ""
    assert "error: HTTP 403 INSUFFICIENT_SCOPES: Missing scope (GET /account)" in err
    assert "`read` scope" in err  # the hint
    assert f"details: {json.dumps(details)}" in err
    assert len(fake.calls) == 1  # 4xx is never retried


def test_api_error_without_details_has_no_details_line(run, fake, capsys):
    fake.add("GET", "/tournaments/nope", (404, error_body("NOT_FOUND", "Tournament not found")))
    code, _ = run("--tournament", "nope", "markets")
    err = capsys.readouterr().err
    assert code == 1
    assert "error: HTTP 404 NOT_FOUND: Tournament not found (GET /tournaments/nope)" in err
    assert "details:" not in err
    assert fake.calls_to("/tournaments/nope/markets") == []


def test_retryable_503_is_retried_then_succeeds(run, fake, client_sleeps):
    fake.add("GET", "/account", (503, error_body("SERVICE_UNAVAILABLE", "busy")), _account())
    fake.add("GET", "/tournaments", tournament_page([]))
    code, out = run("account")
    assert code == 0
    assert len(fake.calls_to("/account")) == 2
    assert len(client_sleeps) == 1
    assert "User:    johndoe" in out


def test_network_error_exhausts_retries_and_exits_1(run, fake, client_sleeps, capsys):
    fake.add("GET", "/account", httpx.ConnectError("connection refused"))
    code, _ = run("account")
    assert code == 1
    assert "error: GET /account" in capsys.readouterr().err
    assert len(fake.calls_to("/account")) == 5  # first try + max_retries (4)
    assert len(client_sleeps) == 4


def test_keyboard_interrupt_returns_130(run, fake):
    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    fake.add("GET", "/account", interrupt)
    code, _ = run("account")
    assert code == 130


# --------------------------------------------------------------------------- argument parsing


def test_command_is_required(run, capsys):
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2
    assert "COMMAND" in capsys.readouterr().err


def test_invalid_choice_is_rejected_by_argparse(run, fake, capsys):
    with pytest.raises(SystemExit) as exc:
        run("leaderboard", "--period", "90d")
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert fake.calls == []


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"supermarket_bot {__version__}"


def test_verbose_flags_and_logging_configuration(run, fake):
    fake.add("GET", "/tournaments", tournament_page([]))
    code, _ = run("-vv", "tournaments")
    assert code == 0
    cli.configure_logging(1)
    for noisy in ("httpx", "httpcore", "realtime", "websockets"):
        assert logging.getLogger(noisy).level == logging.WARNING


def test_r2_robustness_9_log_lines_mask_the_api_key(monkeypatch):
    """Every handler configure_logging sets up masks the key, also in other libraries' records."""
    import io

    from supermarket_bot.errors import RedactingFilter, register_secret

    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    monkeypatch.setattr(root, "handlers", [handler])
    cli.configure_logging(0)
    assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    register_secret("sk_live_QA_SECRET_9f8e7d6c5b4a")
    logging.getLogger("some.library").warning("upstream said: Bearer sk_live_QA_SECRET_9f8e7d6c5b4a")
    logging.getLogger("supermarket_bot").warning("tracker: leaderboard failed: %s", "key sk_live_QA_SECRET_9f8e7d6c5b4a")
    text = stream.getvalue()
    assert "sk_live_QA" not in text and "Bearer ***" in text and "key ***" in text
    cli.configure_logging(0)  # idempotent: one filter per handler
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1


def test_stream_argument_parsing_only():
    args = cli.build_parser().parse_args(["-t", "cup", "stream", "26", "27", "--duration", "5", "--no-log", "--max-markets", "3"])
    assert args.command == "stream"
    assert args.tournament == "cup"
    assert args.market_ids == ["26", "27"]
    assert args.duration == 5.0
    assert args.no_log is True
    assert args.max_markets == 3
    assert args.resync_interval == 90.0
    defaults = cli.build_parser().parse_args(["stream"])
    assert defaults.market_ids == [] and defaults.duration is None and defaults.no_log is False


# --------------------------------------------------------------------------- account / tournaments


def test_account_prints_masked_key_profile_balance_and_tournament_table(run, fake, capsys):
    pending = {**tournament(slug="next-cup", tid="t2", status="draft", name="Next Cup"), "isPendingEnrolment": True, "joinedAt": None, "myBalance": None}
    outsider = {**tournament(slug="open-cup", tid="t3", name="Open Cup"), "joinedAt": None}
    fake.add("GET", "/account", _account())
    fake.add("GET", "/tournaments", tournament_page([tournament(), pending, outsider]))
    code, out = run("--tournament", TOURNAMENT_SLUG, "account")
    assert code == 0
    lines = _lines(out)
    assert lines[0] == f"Key OK ({mask_key(API_KEY)}) → {BASE_URL}"
    assert lines[1] == "User:    johndoe (johndoe@example.com)"
    assert lines[2] == "Profile: 00000000-0000-0000-0000-000000000001"
    assert lines[3] == "Balance: 1,200.50"
    header = next(line for line in lines if line.startswith("SLUG"))
    for col in ("NAME", "STATUS", "ENDS", "MY BALANCE", "ENROLLED", "TOURNAMENT ID"):
        assert col in header
    cup_row = next(line for line in lines if line.startswith(TOURNAMENT_SLUG))
    assert "SIG Predictions Cup" in cup_row and "2026-11-04" in cup_row and "950.50 SIG Coins" in cup_row
    assert cup_row.split()[-2:] == ["yes", TOURNAMENT_ID]
    assert next(line for line in lines if line.startswith("next-cup")).split()[-2:] == ["pending", "t2"]
    assert next(line for line in lines if line.startswith("open-cup")).split()[-2:] == ["no", "t3"]
    # the key is only ever shown masked, and account never resolves a tournament context
    assert API_KEY not in out and API_KEY not in capsys.readouterr().err
    assert [c.path for c in fake.calls] == ["/account", "/tournaments"]
    assert fake.calls[1].params["status"] == "any"


def test_account_json_dumps_account_and_tournaments(run, fake):
    fake.add("GET", "/account", _account())
    fake.add("GET", "/tournaments", tournament_page([tournament()]))
    code, out = run("--json", "account")
    assert code == 0
    assert json.loads(out) == {"account": _account(), "tournaments": [tournament()]}
    assert API_KEY not in out and "Key OK" not in out


def test_account_without_tournaments_or_username(run, fake):
    fake.add("GET", "/account", {**_account(), "username": None, "email": None, "balance": None})
    fake.add("GET", "/tournaments", tournament_page([]))
    code, out = run("account")
    assert code == 0
    assert "User:    — (no email)" in out
    assert "Balance: —" in out
    assert "No tournaments are visible to this key" in out and "--public" in out


def test_tournaments_status_filter_and_offset_pagination(run, fake):
    first = tournament_page([tournament(slug="old-1", tid="o1", status="ended")], has_more=True)
    second = tournament_page([tournament(slug="old-2", tid="o2", status="ended")], offset=1)
    fake.add("GET", "/tournaments", first, second)
    code, out = run("tournaments", "--status", "ended")
    assert code == 0
    calls = fake.calls_to("/tournaments")
    assert [c.params["status"] for c in calls] == ["ended", "ended"]
    assert [c.params["offset"] for c in calls] == ["0", "1"]
    assert "old-1" in out and "old-2" in out


def test_tournaments_json_defaults_to_any(run, fake):
    fake.add("GET", "/tournaments", tournament_page([tournament()]))
    code, out = run("--json", "tournaments")
    assert code == 0
    assert json.loads(out) == [tournament()]
    assert fake.calls[0].params["status"] == "any"


# --------------------------------------------------------------------------- markets / market


def test_markets_table_in_tournament_context(run, cup, fake):
    binary = market("30", "Will the Fed cut rates?", [("40", "YES", 0.4)])
    fake.add("GET", T_MARKETS, market_page([_two_outcome_market(), binary]))
    code, out = run("-t", TOURNAMENT_SLUG, "markets")
    assert code == 0
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1] == "2 market(s)"
    assert lines[2].split() == ["ID", "TITLE", "STATUS", "SETTLES", "(UTC)", "LAST", "CATEGORY"]
    multi_row = next(line for line in lines if line.startswith("26"))
    assert "Who wins?" in multi_row and "2026-11-04 04:59" in multi_row and "2 outcomes" in multi_row
    assert "Election Outcome" in multi_row
    assert "0.400" in next(line for line in lines if line.startswith("30"))
    params = fake.calls_to(T_MARKETS)[0].params
    assert params == {"limit": "100", "status": "open"}


def test_markets_status_search_and_limit(run, cup, fake):
    page = market_page([market("1", "Fed A", [("10", "YES", 0.1)]), market("2", "Fed B", [("11", "YES", 0.2)])], cursor="next")
    fake.add("GET", T_MARKETS, page, market_page([market("3", "Fed C", [("12", "YES", 0.3)])]))
    code, out = run("-t", TOURNAMENT_SLUG, "markets", "--status", "any", "--search", "Fed", "--limit", "1")
    assert code == 0
    calls = fake.calls_to(T_MARKETS)
    assert len(calls) == 1  # max_items stops before fetching page 2
    assert calls[0].params["status"] == "any" and calls[0].params["search"] == "Fed"
    assert "1 market(s)" in out and "Fed A" in out and "Fed B" not in out


def test_markets_limit_follows_cursor_across_pages(run, cup, fake):
    fake.add("GET", T_MARKETS, market_page([market("1", "Pokémon odds", [("10", "YES", 0.1)])], cursor="c2"), market_page([market("2", "Two", [("11", "YES", 0.2)]), market("3", "Three", [("12", "YES", 0.3)])]))
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "markets", "--limit", "2")
    assert code == 0
    assert [m["id"] for m in json.loads(out)] == ["1", "2"]
    assert "Pok\\u00e9mon odds" in out and json.loads(out)[0]["title"] == "Pokémon odds"  # ASCII-escaped, lossless
    calls = fake.calls_to(T_MARKETS)
    assert "cursor" not in calls[0].params and calls[1].params["cursor"] == "c2"


def _market_with_context(status: str = "open", settled_with: Optional[str] = None) -> Dict[str, Any]:
    m = _two_outcome_market()
    m["contexts"] = [
        {
            "type": "tournament",
            "tournament": {"id": TOURNAMENT_ID, "slug": TOURNAMENT_SLUG, "name": "SIG Predictions Cup", "currencyName": "SIG Coins", "isOngoingPlay": False},
            "status": status,
            "settledWith": settled_with,
            "settledOn": "2026-10-02T00:00:00.000Z" if settled_with else None,
            "exchanges": [{"id": "36", "latestPrice": 0.45}, {"id": "37", "latestPrice": 0.55}],
        }
    ]
    return m


NODES = {
    "market_id": "26",
    "root": {
        "node_id": "100",
        "node_type": "operator",
        "operator": "AND",
        "children": [
            {"node_id": "101", "node_type": "contract", "contract_id": "101", "contract_type": "Freeform", "title": "Rain in NYC", "settlement_date": "2026-11-04T04:59:00.000Z", "settled_with": None, "contract_details": None},
            {
                "node_id": "102",
                "node_type": "operator",
                "operator": "NOT",
                "children": [
                    {"node_id": "103", "node_type": "contract", "contract_id": "103", "contract_type": None, "title": None, "settlement_date": None, "settled_with": "YES", "contract_details": None}
                ],
            },
        ],
    },
    "contexts": [],
}


def test_market_detail_prices_and_resolution_tree(run, cup, fake):
    fake.add("GET", "/markets/26", _market_with_context())
    fake.add("GET", "/markets/26/nodes", NODES)
    fake.add("GET", "/exchanges/prices", {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []})
    code, out = run("-t", TOURNAMENT_SLUG, "market", "26")
    assert code == 0
    assert fake.calls_to("/markets/26")[0].params == {"tournamentId": TOURNAMENT_ID}
    assert fake.calls_to("/markets/26/nodes")[0].params == {"tournamentId": TOURNAMENT_ID}
    assert fake.calls_to("/exchanges/prices")[0].params == {"ids": "36,37", "tournamentId": TOURNAMENT_ID}
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1] == "#26 Who wins?"
    assert lines[2] == "status open · settles 2026-11-04T04:59:00.000Z · categories Election Outcome"
    assert not any(line.startswith("settled with") for line in lines)
    assert lines[3].split() == ["EXCH", "OPTION", "LAST", "BID", "ASK", "SPREAD", "INITIAL"]
    assert next(line for line in lines if line.startswith("36")).split() == ["36", "A", "0.410", "0.400", "0.420", "0.020", "0.500"]
    assert next(line for line in lines if line.startswith("37")).split() == ["37", "B", "0.600", "0.580", "0.610", "0.030", "0.500"]
    tree = lines[lines.index("Resolution logic:") + 1 :]
    assert tree == [
        "  AND",
        "    • [Freeform] Rain in NYC (settles 2026-11-04)",
        "    NOT",
        "      • [contract] 103 → YES",
    ]


def test_market_uses_context_status_and_falls_back_to_listed_price(run, cup, fake):
    fake.add("GET", "/markets/26", _market_with_context(status="settled", settled_with="A"))
    fake.add("GET", "/markets/26/nodes", {"market_id": "26", "root": None, "contexts": []})
    fake.add("GET", "/exchanges/prices", {"data": [price("36", "26", 0.99, None, None, "A")], "missingIds": ["37"]})
    code, out = run("-t", TOURNAMENT_SLUG, "market", "26")
    assert code == 0
    assert "status settled ·" in out  # tournament context wins over the top-level "open"
    assert "settled with A on 2026-10-02T00:00:00.000Z" in out
    lines = _lines(out)
    assert next(line for line in lines if line.startswith("36")).split() == ["36", "A", "0.990", "—", "—", "—", "0.500"]
    # no quote for 37: last falls back to the market's listed exchange price
    assert next(line for line in lines if line.startswith("37")).split() == ["37", "B", "0.600", "—", "—", "—", "0.500"]
    assert "Resolution logic" not in out


def test_market_json_and_no_price_read_without_exchanges(run, cup, fake):
    empty = {**market("27", "No outcomes yet", []), "exchanges": []}
    fake.add("GET", "/markets/27", empty)
    fake.add("GET", "/markets/27/nodes", NODES)
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "market", "27")
    assert code == 0
    assert json.loads(out) == {"market": empty, "nodes": NODES, "prices": {"data": [], "missingIds": []}}
    assert fake.calls_to("/exchanges/prices") == []


# --------------------------------------------------------------------------- snapshot


def _snapshot_fakes(fake, prices: Optional[List[Dict[str, Any]]] = None, missing: Optional[List[str]] = None) -> None:
    fake.add("GET", T_MARKETS, market_page([_two_outcome_market()]))
    fake.add(
        "GET",
        "/exchanges/prices",
        {"data": prices if prices is not None else [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": missing or []},
    )


def test_snapshot_table_without_save_writes_nothing(run, cup, fake, data_dir):
    _snapshot_fakes(fake, prices=[price("36", "26", 0.41, 0.40, 0.42, "A")], missing=["37"])
    code, out = run("-t", TOURNAMENT_SLUG, "snapshot", "--search", "wins")
    assert code == 0
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1].startswith("1 market(s), 2 outcome(s) at ") and lines[1].endswith(" UTC")
    assert lines[2].split() == ["MKT", "MARKET", "EXCH", "OPTION", "LAST", "BID", "ASK", "SPREAD"]
    assert next(line for line in lines if line.startswith("26") and " 36 " in line).split()[-4:] == ["0.410", "0.400", "0.420", "0.020"]
    assert "No quote for exchange(s): 37" in out
    assert "Saved to" not in out
    assert fake.calls_to(T_MARKETS)[0].params["search"] == "wins"
    assert fake.calls_to("/exchanges/prices")[0].params["tournamentId"] == TOURNAMENT_ID
    assert not data_dir.exists()


def test_snapshot_save_writes_jsonl_csv_and_markets(run, cup, fake, data_dir):
    _snapshot_fakes(fake)
    code, out = run("-t", TOURNAMENT_SLUG, "snapshot", "--save")
    assert code == 0
    folder = data_dir / TOURNAMENT_SLUG
    assert f"Saved to {folder}/" in out
    jsonl = list(folder.glob("prices-*.jsonl"))
    assert len(jsonl) == 1
    rows = [json.loads(line) for line in jsonl[0].read_text(encoding="utf-8").splitlines()]
    assert [(r["exchange_id"], r["best_bid"], r["mid"], r["tournament"]) for r in rows] == [("36", 0.40, 0.41, TOURNAMENT_SLUG), ("37", 0.58, 0.595, TOURNAMENT_SLUG)]
    with (folder / "latest.csv").open(encoding="utf-8") as fh:
        assert [r["exchange_id"] for r in csv.DictReader(fh)] == ["36", "37"]
    assert [m["id"] for m in json.loads((folder / "markets.json").read_text(encoding="utf-8"))] == ["26"]


def test_snapshot_json_with_save(run, cup, fake, data_dir):
    _snapshot_fakes(fake, missing=["99"])
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "snapshot", "--save", "--status", "any")
    assert code == 0
    payload = json.loads(out)  # nothing but JSON on stdout
    assert set(payload) == {"takenAt", "rows", "missingIds"}
    assert [r["exchange_id"] for r in payload["rows"]] == ["36", "37"]
    assert payload["missingIds"] == ["99"]
    assert fake.calls_to(T_MARKETS)[0].params["status"] == "any"
    assert len(list((data_dir / TOURNAMENT_SLUG).glob("prices-*.jsonl"))) == 1


def test_data_dir_flag_overrides_env(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERMARKET_DATA_DIR", str(tmp_path / "from-env"))
    fake.add("GET", T_PATH, tournament())
    _snapshot_fakes(fake)
    out = io.StringIO()
    code = cli.main(
        ["--env-file", str(tmp_path / "none.env"), "--data-dir", str(tmp_path / "from-flag"), "-t", TOURNAMENT_SLUG, "snapshot", "--save"],
        out=out,
        transport=httpx.MockTransport(fake),
    )
    assert code == 0
    assert (tmp_path / "from-flag" / TOURNAMENT_SLUG / "latest.csv").exists()
    assert not (tmp_path / "from-env").exists()


def test_env_data_dir_used_without_flag(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERMARKET_DATA_DIR", str(tmp_path / "from-env"))
    fake.add("GET", T_PATH, tournament())
    _snapshot_fakes(fake)
    code = cli.main(["--env-file", str(tmp_path / "none.env"), "-t", TOURNAMENT_SLUG, "snapshot", "--save"], out=io.StringIO(), transport=httpx.MockTransport(fake))
    assert code == 0
    assert (tmp_path / "from-env" / TOURNAMENT_SLUG / "latest.csv").exists()


# --------------------------------------------------------------------------- watch


def test_watch_prints_first_table_then_changes_and_saves(run, cup, fake, data_dir):
    fake.add("GET", T_MARKETS, market_page([_two_outcome_market()]))
    fake.add(
        "GET",
        "/exchanges/prices",
        {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []},
        {"data": [price("36", "26", 0.46, 0.45, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []},
    )
    code, out = run("-t", TOURNAMENT_SLUG, "watch", "--iterations", "2", "--interval", "0")
    assert code == 0
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1] == f"Polling every 0s, saving to {data_dir / TOURNAMENT_SLUG}/. Ctrl-C to stop."
    assert lines[2].startswith("[") and lines[2].endswith("] SIG Predictions Cup: 1 markets, 2 outcomes")
    change_idx = next(i for i, line in enumerate(lines) if line.endswith("] 1 change(s)"))
    assert change_idx > 3
    assert "ΔLAST" in lines[change_idx + 1] and "CHANGED" in lines[change_idx + 1]
    changed = lines[change_idx + 3]
    assert " 36 " in changed and "+0.050" in changed and changed.endswith("latest_price,best_bid")
    assert len(lines) == change_idx + 4  # only exchange 36 is reported as moved
    # the market list is read once (refresh every 300s by default), prices every snapshot
    assert len(fake.calls_to(T_MARKETS)) == 1
    assert len(fake.calls_to("/exchanges/prices")) == 2
    jsonl = next((data_dir / TOURNAMENT_SLUG).glob("prices-*.jsonl"))
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 4


def test_watch_no_changes_with_no_save(run, cup, fake, data_dir):
    _snapshot_fakes(fake)
    code, out = run("-t", TOURNAMENT_SLUG, "watch", "--iterations", "2", "--interval", "0", "--no-save")
    assert code == 0
    lines = _lines(out)
    assert lines[1] == "Polling every 0s. Ctrl-C to stop."
    assert lines[-1].endswith("] no changes")
    assert not data_dir.exists()


def test_watch_min_move_ignores_small_moves(run, cup, fake):
    fake.add("GET", T_MARKETS, market_page([_two_outcome_market()]))
    fake.add(
        "GET",
        "/exchanges/prices",
        {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []},
        {"data": [price("36", "26", 0.415, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []},
    )
    code, out = run("-t", TOURNAMENT_SLUG, "watch", "--iterations", "2", "--interval", "0", "--no-save", "--min-move", "0.01")
    assert code == 0
    assert _lines(out)[-1].endswith("] no changes")


def test_watch_refresh_markets_zero_relists_every_snapshot(run, cup, fake):
    _snapshot_fakes(fake)
    code, _ = run("-t", TOURNAMENT_SLUG, "watch", "--iterations", "3", "--interval", "0", "--refresh-markets", "0", "--no-save", "--status", "closed")
    assert code == 0
    calls = fake.calls_to(T_MARKETS)
    assert len(calls) == 3 and all(c.params["status"] == "closed" for c in calls)
    assert len(fake.calls_to("/exchanges/prices")) == 3


def test_watch_json_emits_one_record_per_snapshot(run, cup, fake):
    fake.add("GET", T_MARKETS, market_page([_two_outcome_market()]))
    fake.add(
        "GET",
        "/exchanges/prices",
        {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []},
        {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.5, 0.48, 0.52, "B")], "missingIds": []},
    )
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "watch", "--iterations", "2", "--interval", "0", "--no-save")
    assert code == 0
    records = [json.loads(line) for line in out.splitlines()]  # no header / tables mixed in
    assert len(records) == 2
    assert set(records[0]) == {"takenAt", "changes", "rows"}  # baseline once
    assert set(records[1]) == {"takenAt", "changes"}
    assert [(c["exchange_id"], c["delta"]) for c in records[1]["changes"]] == [("37", -0.1)]


def test_watch_json_first_record_carries_initial_prices(run, cup, fake):
    _snapshot_fakes(fake)
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "watch", "--iterations", "1", "--interval", "0", "--no-save")
    assert code == 0
    first = json.loads(out.splitlines()[0])
    # the table mode prints the full first snapshot; JSON consumers need that baseline too
    assert "0.58" in json.dumps(first) and '"37"' in json.dumps(first)


# --------------------------------------------------------------------------- book


def _arb_book() -> Dict[str, Any]:
    return market_orderbook(
        [
            book_exchange("36", [(0.39, 50), (0.40, 100)], [(0.41, 80)], option="A"),
            book_exchange("37", [(0.55, 10)], [(0.6, 5), (0.58, 20)], seq=None, option="B"),
        ],
        overround=0.97,
        arb=True,
    )


def test_book_market_ladder_overround_and_arbitrage_flag(run, cup, fake):
    fake.add("GET", "/markets/26/orderbook", _arb_book())
    code, out = run("-t", TOURNAMENT_SLUG, "book", "26")
    assert code == 0
    assert fake.calls_to("/markets/26/orderbook")[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "10"}
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1] == "Market 26: 2 outcome(s) · overround 0.970 · ARBITRAGE FLAGGED"
    i36 = lines.index("Exchange 36 (A) · mid 0.405 · spread 0.010 · seq 100")
    assert lines[i36 + 1].split() == ["BID", "QTY", "BID", "ASK", "ASK", "QTY"]
    assert lines[i36 + 3].split() == ["100", "0.400", "0.410", "80"]  # best bid first
    assert lines[i36 + 4].split() == ["50", "0.390"]
    i37 = lines.index("Exchange 37 (B) · mid 0.565 · spread 0.030")  # null asOf: no version suffix
    assert lines[i37 + 3].split() == ["10", "0.550", "0.580", "20"]  # best ask (lowest) first
    assert lines[i37 + 4].split() == ["0.600", "5"]


def test_book_without_arbitrage_and_empty_book(run, cup, fake):
    fake.add("GET", "/markets/26/orderbook", market_orderbook([book_exchange("36", [], [], option="YES")], overround=None, arb=False))
    code, out = run("-t", TOURNAMENT_SLUG, "book", "26")
    assert code == 0
    assert "Market 26: 1 outcome(s) · overround —" in out
    assert "ARBITRAGE" not in out
    assert "Exchange 36 (YES) · mid — · spread — · seq 100" in out
    assert "  (empty book)" in out


def test_book_reads_the_tournament_context_not_legacy_top_level(run, cup, fake):
    ours = {"exchanges": [book_exchange("36", [(0.3, 1)], [(0.35, 2)], option="A")], "overround": 1.02, "hasArbitrageOpportunity": False}
    other = {"exchanges": [book_exchange("36", [(0.9, 9)], [(0.95, 9)], option="A")], "overround": 0.5, "hasArbitrageOpportunity": True}
    resp = {
        **other,  # legacy top-level mirrors another tournament
        "contexts": [
            {"type": "tournament", "tournament": {"id": "other-tournament"}, "orderbook": other},
            {"type": "tournament", "tournament": {"id": TOURNAMENT_ID}, "orderbook": ours},
        ],
    }
    fake.add("GET", "/markets/26/orderbook", resp)
    code, out = run("-t", TOURNAMENT_SLUG, "book", "26")
    assert code == 0
    assert "overround 1.020" in out and "ARBITRAGE" not in out
    assert "0.300" in out and "0.900" not in out


def test_book_depth_is_clamped_and_limits_rows(run, cup, fake):
    deep = market_orderbook([book_exchange("36", [(0.4, 1), (0.39, 2), (0.38, 3)], [(0.41, 1), (0.42, 2)], option="A")])
    fake.add("GET", "/markets/26/orderbook", deep)
    with pytest.raises(SystemExit) as exc:  # out-of-range depth is rejected by argparse
        run("-t", TOURNAMENT_SLUG, "book", "26", "--depth", "500")
    assert exc.value.code == 2
    code, out = run("-t", TOURNAMENT_SLUG, "book", "26", "--depth", "200")
    assert code == 0
    assert fake.calls_to("/markets/26/orderbook")[-1].params["depth"] == "200"
    code, out = run("-t", TOURNAMENT_SLUG, "book", "26", "--depth", "1")
    assert code == 0
    assert fake.calls_to("/markets/26/orderbook")[-1].params["depth"] == "1"
    assert "0.390" not in out and "0.420" not in out  # only the top rung printed


def test_book_single_exchange(run, cup, fake):
    resp = {
        "exchangeId": "36",
        "marketId": "26",
        "asOf": {"sequence": 7, "at": "2026-10-03T12:00:00.000Z"},
        "depth": 5,
        "bids": [{"price": 0.4, "quantity": 100}],
        "asks": [{"price": 0.45, "quantity": 25.5}],
        "bestBid": 0.4,
        "bestAsk": 0.45,
        "spread": 0.05,
    }
    fake.add("GET", "/exchanges/36/orderbook", resp)
    code, out = run("-t", TOURNAMENT_SLUG, "book", "--exchange", "36", "--depth", "5")
    assert code == 0
    assert fake.calls_to("/exchanges/36/orderbook")[0].params == {"depth": "5", "tournamentId": TOURNAMENT_ID}
    assert fake.calls_to("/markets/26/orderbook") == []
    lines = _lines(out)
    assert lines[1] == "Exchange 36 · mid 0.425 · spread 0.050 · seq 7"
    assert lines[4].split() == ["100", "0.400", "0.450", "25.50"]
    assert "Market " not in out


def test_book_json(run, cup, fake):
    fake.add("GET", "/markets/26/orderbook", _arb_book())
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "book", "26")
    assert code == 0
    assert json.loads(out) == _arb_book()


def test_book_without_market_or_exchange_is_an_error_exit(run, cup, fake):
    with pytest.raises(SystemExit) as exc:
        run("-t", TOURNAMENT_SLUG, "book")
    assert exc.value.code not in (0, None)
    assert "MARKET_ID" in str(exc.value.code) and "--exchange" in str(exc.value.code)
    assert not any("orderbook" in c.path for c in fake.calls)


# --------------------------------------------------------------------------- trades


def test_trades_cursor_pagination_limit_and_since(run, cup, fake):
    path = "/exchanges/36/trades"
    fake.add("GET", path, _trade_page([_trade("t5", 0.45, 5), _trade("t4", 0.44, 4, side="NO")], cursor="c2"), _trade_page([_trade("t3", 0.43, 3), _trade("t2", 0.42, 2)], cursor="c3"))
    code, out = run("-t", TOURNAMENT_SLUG, "trades", "36", "--limit", "3", "--since", "2026-10-01T00:00:00Z")
    assert code == 0
    calls = fake.calls_to(path)
    assert len(calls) == 2  # stops once --limit trades are collected, never reads page 3
    assert calls[0].params == {"tournamentId": TOURNAMENT_ID, "from": "2026-10-01T00:00:00Z", "limit": "200"}
    assert calls[1].params["cursor"] == "c2" and calls[1].params["from"] == "2026-10-01T00:00:00Z"
    lines = _lines(out)
    assert lines[1] == "Exchange 36: 3 trade(s), newest first (prices are YES-denominated)"
    assert lines[2].split() == ["TIME", "(UTC)", "PRICE", "SIZE", "SIDE", "VOLUME", "TRADE", "ID"]
    assert lines[4].split() == ["2026-10-03", "12:00:05", "0.450", "10", "YES", "4.50", "t5"]
    assert lines[5].split()[-3:] == ["NO", "4.40", "t4"]
    assert lines[6].split()[-1] == "t3"
    assert len(lines) == 7


def test_trades_json_default_limit(run, cup, fake):
    fake.add("GET", "/exchanges/36/trades", _trade_page([_trade("t1", 0.5, 1)]))
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "trades", "36")
    assert code == 0
    assert json.loads(out) == [_trade("t1", 0.5, 1)]
    assert "from" not in fake.calls_to("/exchanges/36/trades")[0].params


# --------------------------------------------------------------------------- history


def test_history_params_table_and_sparkline(run, cup, fake):
    path = "/exchanges/36/price-history"
    fake.add("GET", path, _candles())
    code, out = run("-t", TOURNAMENT_SLUG, "history", "36", "--resolution", "5m", "--since", "2026-10-01T00:00:00Z", "--limit", "1000")
    assert code == 0
    assert fake.calls_to(path)[0].params == {"tournamentId": TOURNAMENT_ID, "resolution": "5m", "from": "2026-10-01T00:00:00Z", "limit": "1000"}
    lines = _lines(out)
    assert lines[1] == "Exchange 36 · 5m candles · 2 bucket(s) with trades"
    assert lines[2] == "close ▁█"
    assert lines[3].split() == ["TIME", "(UTC)", "OPEN", "HIGH", "LOW", "CLOSE", "VWAP", "VOLUME", "TRADES"]
    assert lines[5].split() == ["2026-10-01", "00:00", "0.400", "0.450", "0.390", "0.420", "0.415", "1,200", "14"]
    assert lines[6].split()[-2:] == ["300.50", "3"]
    assert "Wrote" not in out


def test_history_defaults_without_since(run, cup, fake):
    path = "/exchanges/36/price-history"
    fake.add("GET", path, {**_candles(), "candles": []})
    with pytest.raises(SystemExit):
        run("-t", TOURNAMENT_SLUG, "history", "36", "--limit", "0")
    code, out = run("-t", TOURNAMENT_SLUG, "history", "36", "--limit", "1")
    assert code == 0
    assert fake.calls_to(path)[0].params == {"tournamentId": TOURNAMENT_ID, "resolution": "1h", "limit": "1"}
    assert "· 1h candles · 0 bucket(s) with trades" in out
    assert "close " not in out  # no sparkline without candles


def test_history_truncated_coverage_message(run, cup, fake):
    fake.add("GET", "/exchanges/36/price-history", _candles(complete=False))
    code, out = run("-t", TOURNAMENT_SLUG, "history", "36", "--since", "2026-01-01T00:00:00Z")
    assert code == 0
    assert "2 bucket(s) with trades · (truncated: more candles exist)" in out


def test_history_csv_written(run, cup, fake, tmp_path):
    fake.add("GET", "/exchanges/36/price-history", _candles())
    target = tmp_path / "exports" / "nested" / "36.csv"
    code, out = run("-t", TOURNAMENT_SLUG, "history", "36", "--csv", str(target))
    assert code == 0
    assert f"Wrote 2 candle(s) to {target}" in out
    with target.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert reader.fieldnames == ["time", "open", "high", "low", "close", "vwap", "volume", "tradeCount"]
    assert [(r["time"], r["close"], r["tradeCount"]) for r in rows] == [("2026-10-01T00:00:00.000Z", "0.42", "14"), ("2026-10-01T00:05:00.000Z", "0.5", "3")]


def test_history_json_still_writes_csv(run, cup, fake, tmp_path):
    fake.add("GET", "/exchanges/36/price-history", _candles())
    target = tmp_path / "h.csv"
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "history", "36", "--csv", str(target))
    assert code == 0
    assert json.loads(out) == _candles()
    assert len(target.read_text(encoding="utf-8").splitlines()) == 3


# --------------------------------------------------------------------------- leaderboard


def test_leaderboard_tournament_path(run, cup, fake):
    path = f"/tournaments/{TOURNAMENT_SLUG}/leaderboard"
    fake.add("GET", path, _leaderboard())
    code, out = run("-t", TOURNAMENT_SLUG, "leaderboard", "--period", "7d", "--sort", "roi")
    assert code == 0
    assert fake.calls_to(path)[0].params == {"period": "7d", "limit": "20", "offset": "0", "sort": "roi"}
    assert fake.calls_to("/leaderboards") == []
    lines = _lines(out)
    assert lines[1] == "12 ranked · period 7d · your rank 3"
    assert lines[2].split() == ["#", "USER", "P&L", "ROI", "WIN", "TRADES", "VOLUME"]
    assert lines[4].split() == ["1", "alice", "150.25", "5.01%", "66.67%", "12", "3,000"]
    assert lines[5].split() == ["2", "p2-profile", "-20", "—", "—", "3", "100"]


def test_leaderboard_quarter_uses_leaderboards_with_tournament_slug(run, cup, fake):
    fake.add("GET", "/leaderboards", {**_leaderboard(period="quarter"), "myRank": None})
    code, out = run("-t", TOURNAMENT_SLUG, "leaderboard", "--period", "quarter", "--sort", "pnl", "--limit", "5")
    assert code == 0
    # the tournament endpoint has no `quarter` period; /leaderboards does
    assert fake.calls_to(f"/tournaments/{TOURNAMENT_SLUG}/leaderboard") == []
    params = fake.calls_to("/leaderboards")[0].params
    assert params == {"tournamentSlug": TOURNAMENT_SLUG, "period": "quarter", "sort": "pnl", "limit": "5", "offset": "0"}
    assert "12 ranked · period quarter" in out and "your rank" not in out


def test_leaderboard_public_is_global_without_sort(run, fake):
    fake.add("GET", "/leaderboards", _leaderboard(period="all", my_rank=None))
    fake.add("GET", "/markets", {"data": [{"id": "5", "contexts": [{"type": "public", "tournament": None}]}], "pagination": {"hasMore": False, "nextCursor": None}})
    code, out = run("--public", "leaderboard", "--sort", "roi")
    assert code == 0
    assert [c.path for c in fake.calls] == ["/markets", "/leaderboards"]  # probe only, no tournament lookups
    params = fake.calls[1].params
    assert "sort" not in params and "tournamentSlug" not in params  # sort is a 400 on the global board
    assert params["period"] == "all"
    assert out.splitlines()[0] == "Public global markets [public]"


def test_leaderboard_archived_entries_show_final_total_value(run, cup, fake):
    archived = {**_leaderboard(period="all", my_rank=None), "leaderboard": [{"rank": 1, "profileId": "p9", "username": "zed", "finalTotalValue": 2500.5, "finalBalance": 2000}]}
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/leaderboard", archived)
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "leaderboard")
    assert json.loads(out) == archived
    code, out = run("-t", TOURNAMENT_SLUG, "leaderboard")
    assert code == 0
    assert _lines(out)[4].split()[:3] == ["1", "zed", "2,500.50"]


def test_leaderboard_limit_stays_within_documented_range(run, cup, fake):
    path = f"/tournaments/{TOURNAMENT_SLUG}/leaderboard"
    fake.add("GET", path, _leaderboard())
    code, _ = run("-t", TOURNAMENT_SLUG, "leaderboard", "--limit", "500")
    assert code == 0
    assert all(1 <= int(c.params["limit"]) <= 100 for c in fake.calls_to(path))


# --------------------------------------------------------------------------- scan


CONSTRAINTS = {
    "data": [
        {
            "relationshipId": "r1",
            "type": "IMPLIES",
            "violationAmount": 0.1,
            "reason": "A implies B but P(A) > P(B)",
            "suggestedCorrectiveTrades": [
                {"exchangeId": "36", "outcomeSide": "YES", "action": "buy", "rationale": "lift B", "marketId": "26", "marketTitle": "Who wins?", "outcome": "A", "currentPrice": 0.4},
                {"exchangeId": "40", "outcomeSide": "NO", "action": "sell", "rationale": "cut A", "marketId": "30", "marketTitle": None, "outcome": None, "currentPrice": None},
            ],
            "constraint": {"description": "d", "priceRule": "P(B) >= P(A)", "boundSemantics": "b", "evaluationEndpoint": "e"},
        }
    ],
    "violationsCount": 1,
    "computedAt": "2026-10-03T12:00:00.000Z",
}


def _scan_fakes(fake) -> None:
    fake.add("GET", "/relationships/constraints", CONSTRAINTS)
    fake.add(
        "GET",
        T_MARKETS,
        market_page(
            [
                market("26", "Who wins?", [("36", "A", 0.4), ("37", "B", 0.6)]),
                market("27", "Which month?", [("38", "Jan", 0.5), ("39", "Feb", 0.6)]),
                market("30", "Binary", [("40", "YES", 0.5)]),
            ]
        ),
    )
    fake.add("GET", "/markets/26/orderbook", market_orderbook([book_exchange("36", [], []), book_exchange("37", [], [])], overround=1.05, arb=False))
    fake.add("GET", "/markets/27/orderbook", market_orderbook([book_exchange("38", [], []), book_exchange("39", [], [])], overround=0.95, arb=True))


def test_scan_reports_violations_and_overround_table(run, cup, fake):
    _scan_fakes(fake)
    code, out = run("-t", TOURNAMENT_SLUG, "scan")
    assert code == 0
    assert fake.calls_to("/relationships/constraints")[0].params == {"violationsOnly": "true", "tournamentId": TOURNAMENT_ID}
    assert fake.calls_to("/markets/30/orderbook") == []  # binary markets are not scanned
    assert fake.calls_to("/markets/26/orderbook")[0].params == {"tournamentId": TOURNAMENT_ID, "depth": "1"}
    lines = _lines(out)
    assert lines[1] == "ALL relationship violations: 1 (computed 2026-10-03T12:00:00.000Z)"
    assert lines[2] == "  • IMPLIES violated by 0.100: A implies B but P(A) > P(B)"
    assert lines[3] == "    rule: P(B) >= P(A)"
    assert lines[4] == "    suggests buy YES on exch 36 (Who wins?, now 0.400): lift B"
    assert lines[5] == "    suggests sell NO on exch 40 (30, now —): cut A"
    assert "Multi-outcome books scanned: 2 of 2" in out
    table_rows = [line for line in lines if line[:2] in ("26", "27")]
    assert [r.split()[0] for r in table_rows] == ["27", "26"]  # arbitrage first
    assert table_rows[0].split()[-2:] == ["0.950", "YES"]
    assert table_rows[1].split()[-1] == "1.050"
    assert lines[-1] == "(Read-only: nothing was traded.)"
    assert all(c.method == "GET" for c in fake.calls)


def test_scan_max_markets_and_json(run, cup, fake):
    _scan_fakes(fake)
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "scan", "--max-markets", "1")
    assert code == 0
    result = json.loads(out)
    assert result["scannedMarkets"] == 1 and result["multiOutcomeMarkets"] == 2
    assert result["violationsCount"] == 1 and result["violations"] == CONSTRAINTS["data"]
    assert result["markets"] == [{"market_id": "26", "market_title": "Who wins?", "outcomes": 2, "overround": 1.05, "arbitrage": False}]
    assert fake.calls_to("/markets/27/orderbook") == []


def test_scan_without_violations_or_multi_outcome_markets(run, fake):
    fake.add("GET", "/relationships/constraints", {"data": [], "violationsCount": 0, "computedAt": None})
    fake.add("GET", "/markets", market_page([market("30", "Binary", [("40", "YES", 0.5)])]))
    code, out = run("--public", "scan")
    assert code == 0
    assert "tournamentId" not in fake.calls_to("/relationships/constraints")[0].params
    assert "ALL relationship violations: 0 (computed —)" in out
    assert "Multi-outcome books scanned: 0 of 0" in out
    assert "MKT" not in out
    assert _lines(out)[-1] == "(Read-only: nothing was traded.)"


# --------------------------------------------------------------------------- portfolio


POSITIONS = {
    "positions": [
        {
            "exchangeId": "36",
            "marketId": "26",
            "marketTitle": "Who wins?",
            "option": "A",
            "settled": False,
            "quantity": 100,
            "avgCost": 0.4,
            "currentPrice": 0.45,
            "marketValue": 45,
            "costBasis": 40,
            "unrealizedPnl": 5,
            "unrealizedPnlPct": 12.5,
            "moneyEarned": 0,
            "lots": [],
        }
    ],
    "summary": {"totalMarketValue": 45, "totalCostBasis": 40, "totalUnrealizedPnl": 5},
}
PNL = {
    "period": "week",
    "periodStart": "2026-09-26T00:00:00.000Z",
    "periodEnd": "2026-10-03T00:00:00.000Z",
    "periodPnl": 12.5,
    "unrealizedPnl": 5,
    "totalAccountValue": 1012.5,
    "totalHoldingsValue": 45,
    "totalCostBasis": 40,
    "roi": 1.25,
    "sharpe": None,
}


def test_portfolio_tournament_positions_and_pnl(run, cup, fake):
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions", POSITIONS)
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/portfolio/pnl", PNL)
    code, out = run("-t", TOURNAMENT_SLUG, "portfolio", "--period", "week")
    assert code == 0
    assert fake.calls_to(f"/tournaments/{TOURNAMENT_SLUG}/portfolio/pnl")[0].params == {"period": "week"}
    assert fake.calls_to("/portfolio/positions") == [] and fake.calls_to("/portfolio/pnl") == []
    lines = _lines(out)
    assert lines[0] == HEADER
    assert lines[1] == "Account value 1,012.50 · unrealized 5 · period P&L 12.50 · ROI 1.25% · Sharpe —"
    assert lines[2].split() == ["MARKET", "EXCH", "OPTION", "QTY", "AVG", "COST", "PRICE", "VALUE", "UNREAL", "P&L"]
    assert lines[4].split() == ["Who", "wins?", "36", "A", "100", "0.400", "0.450", "45", "5"]


def test_portfolio_public_uses_default_context_endpoints(run, fake):
    fake.add("GET", "/portfolio/positions", {"positions": [], "summary": {}})
    fake.add("GET", "/portfolio/pnl", {**PNL, "period": "all", "periodPnl": None})
    fake.add("GET", "/markets", {"data": [{"id": "5", "contexts": [{"type": "public", "tournament": None}]}], "pagination": {"hasMore": False, "nextCursor": None}})
    code, out = run("--public", "portfolio", "--period", "all")
    assert code == 0
    assert [c.path for c in fake.calls] == ["/markets", "/portfolio/positions", "/portfolio/pnl"]
    assert fake.calls[2].params == {"period": "all"}
    assert "period P&L —" in out
    assert _lines(out)[-1] == "No open positions."


def test_portfolio_json(run, cup, fake):
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/portfolio/positions", POSITIONS)
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/portfolio/pnl", PNL)
    code, out = run("-t", TOURNAMENT_SLUG, "--json", "portfolio")
    assert code == 0
    assert json.loads(out) == {"positions": POSITIONS, "pnl": PNL}


# --------------------------------------------------------------------------- read-only guarantee


def test_cli_commands_only_ever_issue_get_requests(run, cup, fake):
    _scan_fakes(fake)
    fake.add("GET", "/exchanges/prices", {"data": [], "missingIds": []})
    fake.add("GET", "/account", _account())
    fake.add("GET", "/tournaments", tournament_page([tournament()]))
    for argv in (("account",), ("-t", TOURNAMENT_SLUG, "snapshot"), ("-t", TOURNAMENT_SLUG, "scan"), ("-t", TOURNAMENT_SLUG, "book", "26")):
        code, _ = run(*argv)
        assert code == 0
    assert fake.calls and {c.method for c in fake.calls} == {"GET"}


# --------------------------------------------------------------------------- round-1 QA regressions


def test_live_api_12_tournament_flag_works_after_the_command(run, fake, monkeypatch, capsys):
    fake.add("GET", T_PATH, tournament())
    fake.add("GET", T_MARKETS, market_page([]))
    code, out = run("markets", "--tournament", TOURNAMENT_SLUG)
    assert code == 0 and [c.path for c in fake.calls] == [T_PATH, T_MARKETS]
    assert out.splitlines()[0] == HEADER

    parse = cli.build_parser().parse_args
    args = parse(["dashboard", "--tournament", "predictions-cup-2026", "--demo"])
    assert args.tournament == "predictions-cup-2026" and args.demo and args.command == "dashboard"
    assert parse(["-t", "a", "dashboard"]).tournament == "a"  # before the command still works
    assert parse(["-t", "a", "dashboard", "-t", "b"]).tournament == "b"  # the later value wins
    plain = parse(["dashboard"])
    assert (plain.tournament, plain.public, plain.json, plain.env_file, plain.data_dir) == (None, False, False, ".env", None)
    assert parse(["markets", "--public", "--json"]).public is True

    # the error message's advice is the form that works
    seen = {}

    def fake_dashboard(settings, args, out=None):
        seen["tournament"] = args.tournament
        return 0

    import supermarket_bot.web as web_mod

    monkeypatch.setattr(web_mod, "run_dashboard", fake_dashboard)
    assert cli.main(["dashboard", "--tournament", "predictions-cup-2026"], out=io.StringIO()) == 0
    assert seen == {"tournament": "predictions-cup-2026"}
    fake.add("GET", "/tournaments", tournament_page([tournament(slug="cup-a", tid="a"), tournament(slug="cup-b", tid="b")]))
    code, _ = run("markets")
    assert code == 1 and "dashboard --tournament <slug>" in capsys.readouterr().err


# --------------------------------------------------------------------------- simulation commands (docs/PAPER_TRADING.md §7.5)


@pytest.fixture
def methods(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str]]:
    """Every request any MockTransport answers (the demo's simulated API and the FakeAPI): (method, path)."""
    seen: List[Tuple[str, str]] = []
    original = httpx.MockTransport.handle_request

    def recording(self: httpx.MockTransport, request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return original(self, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_request", recording)
    return seen


def assert_get_only(seen: List[Tuple[str, str]]) -> None:
    assert seen, "no request was recorded"
    for method, path in seen:
        assert method == "GET", (method, path)
        assert not any(word in path for word in ("/orders", "cancel", "multi-leg", "realtime/token")), path


def test_dashboard_simulation_options_parse() -> None:
    from supermarket_bot import web

    parse = cli.build_parser().parse_args
    args = parse(["dashboard", "--demo", "--no-paper", "--fair-value", "manual", "--regime", "vwap_closeout",
                  "--sizing", "chaser", "--all-collateral", "--paper-capital", "50000"])
    assert (args.no_paper, args.fair_value, args.regime, args.sizing, args.all_collateral, args.paper_capital) == (
        True, "manual", "vwap_closeout", "chaser", True, 50000.0)
    options = web.simulation_options(args)
    expected = {"paper": False, "fair_value": "manual", "regime": "vwap_closeout", "sizing": "chaser",
                "all_collateral": True, "paper_capital": 50000.0}
    assert {k: options.get(k) for k in expected} == expected
    # the outside-move keys (package server, §18.1) are checked by test_dashboard_moves_options_reach_the_builders
    assert set(options) - set(expected) <= {"moves", "moves_poll_s", "moves_book_reads_per_min"}
    plain = parse(["dashboard"])
    assert (plain.no_paper, plain.fair_value, plain.no_fair_value, plain.sizing, plain.all_collateral, plain.paper_capital) == (
        False, "auto", False, "conservative", False, None)
    assert web.simulation_options(parse(["dashboard", "--no-fair-value"]))["fair_value"] == "off"
    for bad in (["--fair-value", "always"], ["--regime", "guess"], ["--sizing", "yolo"], ["--paper-capital", "-5"],
                ["--paper-capital", "nan"]):
        with pytest.raises(SystemExit) as caught:
            parse(["dashboard", *bad])
        assert caught.value.code == 2, bad


def test_regime_defaults_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from supermarket_bot import web

    parse = cli.build_parser().parse_args
    assert web.regime_option(parse(["dashboard"])) == "unknown"
    monkeypatch.setenv("SUPERMARKET_SETTLEMENT_REGIME", "resolved_outcomes")
    assert web.regime_option(parse(["dashboard"])) == "resolved_outcomes"
    assert web.regime_option(parse(["dashboard", "--regime", "vwap_closeout"])) == "vwap_closeout"
    monkeypatch.setenv("SUPERMARKET_SETTLEMENT_REGIME", "nonsense")
    assert web.regime_option(parse(["paper"])) == "unknown"


def test_paper_backtest_and_fairvalue_options_parse() -> None:
    parse = cli.build_parser().parse_args
    paper = parse(["paper"])
    assert (paper.hours, paper.summary_every, paper.interval, paper.step) == (24.0, 60.0, 30.0, 30.0)
    assert not (paper.reset or paper.keep_running or paper.demo or paper.fast or paper.no_news or paper.json)
    paper = parse(["paper", "--hours", "2", "--summary-every", "15", "--interval", "10", "--reset", "--keep-running",
                   "--demo", "--fast", "--step", "60", "--no-news", "--json", "--fair-value", "off", "--regime",
                   "resolved_outcomes", "--sizing", "chaser", "--all-collateral", "--paper-capital", "2000",
                   "--data-dir", "elsewhere"])
    assert (paper.hours, paper.summary_every, paper.interval, paper.step, paper.reset, paper.keep_running) == (
        2.0, 15.0, 10.0, 60.0, True, True)
    assert (paper.demo, paper.fast, paper.no_news, paper.json, paper.data_dir) == (True, True, True, True, "elsewhere")
    assert (paper.fair_value, paper.regime, paper.sizing, paper.all_collateral, paper.paper_capital) == (
        "off", "resolved_outcomes", "chaser", True, 2000.0)
    bt = parse(["backtest"])
    assert (bt.store, bt.hours, bt.since, bt.until, bt.step, bt.latency, bt.spread) == (None, None, None, None, 300.0, 60.0, 0.02)
    assert (bt.touch_qty, bt.volume_share, bt.candle_cap, bt.sweep, bt.sizing, bt.regime, bt.demo_hours) == (
        100.0, 0.1, 50.0, [], "conservative", None, 6.0)
    bt = parse(["backtest", "--store", "x.sqlite3", "--hours", "12", "--since", "2026-10-01T00:00:00Z", "--until",
                "2026-10-02T00:00:00Z", "--step", "600", "--latency", "120", "--spread", "0.03", "--touch-qty", "50",
                "--volume-share", "0.2", "--candle-cap", "25", "--no-books", "--no-fair-values", "--no-history",
                "--sweep", "min_value_edge=0.01,0.02", "--sweep", "latency_s=30,60", "--latency-sweep", "--sizing",
                "chaser", "--regime", "vwap_closeout", "--json", "--demo", "--demo-hours", "2"])
    assert (bt.store, bt.hours, bt.step, bt.latency, bt.spread, bt.touch_qty, bt.volume_share, bt.candle_cap) == (
        "x.sqlite3", 12.0, 600.0, 120.0, 0.03, 50.0, 0.2, 25.0)
    assert bt.no_books and bt.no_fair_values and bt.no_history and bt.latency_sweep and bt.json and bt.demo
    assert bt.sweep == ["min_value_edge=0.01,0.02", "latency_s=30,60"] and bt.demo_hours == 2.0
    fv = parse(["fairvalue"])
    assert (fv.demo, fv.fair_value, fv.template, fv.only_usable, fv.show_override, fv.import_history, fv.days) == (
        False, "auto", False, False, None, False, 7.0)
    fv = parse(["fairvalue", "--demo", "--fair-value", "manual", "--template", "--only-usable", "--show-override", "9026",
                "--import-history", "--days", "3", "--json"])
    assert (fv.demo, fv.fair_value, fv.template, fv.only_usable, fv.show_override, fv.import_history, fv.days) == (
        True, "manual", True, True, "9026", True, 3.0)
    for bad in (["paper", "--hours", "0"], ["paper", "--sizing", "max"], ["backtest", "--step", "-1"],
                ["fairvalue", "--fair-value", "off"], ["backtest", "--regime", "maybe"]):
        with pytest.raises(SystemExit) as caught:
            parse(bad)
        assert caught.value.code == 2, bad


def test_paper_demo_fast_runs_in_a_temporary_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                       methods: List[Tuple[str, str]], capsys: Any) -> None:
    from supermarket_bot.paper import CAVEATS, TABLE_WARNING, VERDICT_CAVEATS

    import tempfile

    made: List[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args: Any, **kwargs: Any) -> str:
        made.append(real_mkdtemp(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    out = io.StringIO()
    code = cli.main(["paper", "--demo", "--fast", "--hours", "1", "--summary-every", "30", "--no-news"], out=out)
    text = out.getvalue()
    assert code == 0, capsys.readouterr().err
    assert len(made) == 1 and not Path(made[0]).exists()  # its own temporary directory, removed at exit
    assert not (tmp_path / "data").exists()  # never the dashboard's data/demo (D46)
    lines = _lines(text)
    assert lines[0].startswith("Fast demo simulation: 1 simulated hours in 30-second steps")
    summaries = [line for line in lines if line.startswith("[paper] ")]
    assert len(summaries) >= 2 and "0.5 h observed of 1 h" in summaries[0] and "1.0 h observed of 1 h" in summaries[1]
    assert "steps 61" in summaries[0] and "reads " in summaries[0]
    assert any(line.startswith("  You, acting by hand ~4 min late") and " at liquidation  fills " in line for line in lines)
    assert any(line.startswith("  Verdict (You, acting by hand") and "Not enough evidence yet" in line for line in lines)
    assert any(line.startswith("  Signal study (value, +30 min): ") for line in lines)
    assert re.search(r"Run run-\d+ ended \(completed\)\.", text)
    assert TABLE_WARNING.format(n=9) in text
    for i in VERDICT_CAVEATS:
        assert CAVEATS[i] in text
    assert_get_only(methods)


def test_paper_keep_running_json_and_data_dir(tmp_path: Path, capsys: Any) -> None:
    from supermarket_bot.store import TrackerStore

    data = tmp_path / "sim"
    out = io.StringIO()
    code = cli.main(["paper", "--demo", "--fast", "--hours", "0.25", "--keep-running", "--json", "--no-news",
                     "--data-dir", str(data)], out=out)
    assert code == 0, capsys.readouterr().err
    body = json.loads(out.getvalue())
    assert body["enabled"] is True and body["run"]["ended_at"] is None and body["run"]["end_reason"] is None
    # the target was reached (its final snapshot is kept, §6.2 step 9) but the run itself stays open
    assert body["run"]["complete"] is True and body["run"]["final"] is not None
    assert body["run"]["steps"] == 31 and body["has_previous"] is False
    db = data / "demo" / "tracker.sqlite3"
    assert db.is_file()  # --data-dir keeps the database
    store = TrackerStore.open_read_only(db)
    try:
        [run] = store.paper_runs()
        assert run["ended_at"] is None  # --keep-running: the run stays open for the next start
    finally:
        store.close()
    out = io.StringIO()
    assert cli.main(["paper", "--demo", "--fast", "--hours", "0.1", "--json", "--no-news", "--data-dir", str(data)], out=out) == 0
    ended = json.loads(out.getvalue())
    assert ended["has_previous"] is True  # without --keep-running the run ends ("completed") with its final snapshot
    store = TrackerStore.open_read_only(db)
    try:
        last = store.paper_last_ended_run()
        assert last is not None and last["state"]["final"]["reason"] == "completed"
    finally:
        store.close()


def test_paper_refuses_a_store_another_process_is_tracking(tmp_path: Path, capsys: Any) -> None:
    import time

    from supermarket_bot.store import TrackerStore

    data = tmp_path / "busy"
    db = data / "demo" / "tracker.sqlite3"
    db.parent.mkdir(parents=True)
    store = TrackerStore(db)
    try:
        assert store.acquire_lease("tracker", {"owner_id": "my-laptop:1234:1", "pid": 1234, "host": "my-laptop"},
                                   time.time(), ttl_s=90.0) is None
    finally:
        store.close()
    code = cli.main(["paper", "--demo", "--fast", "--hours", "0.1", "--no-news", "--data-dir", str(data)], out=io.StringIO())
    err = capsys.readouterr().err
    assert code == 2
    # another machine: its liveness cannot be checked, so the message says when the lock frees itself (live-5)
    assert (f"Another process (pid 1234 on my-laptop) is already running the tracker on {db}: stop it first. If it has "
            "already ended, the lock frees itself within ") in err
    assert ("start again then. Running two would double the API reads; the account allows 100 per minute across all "
            "keys.") in err
    assert "separate copy" not in err
    assert db.is_file()  # the other process's database was not deleted


def test_live_paper_lease_conflict_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from supermarket_bot import web
    from supermarket_bot.tracker import TrackerBusy

    closed: List[bool] = []

    class BusyRuntime:
        tracker = type("T", (), {"paper_runner": object()})()

        def start(self) -> None:
            raise TrackerBusy({"pid": 1234, "host": "my-laptop"}, "data/cup/tracker.sqlite3")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(web, "build_live", lambda settings, args, out=None: BusyRuntime())
    assert cli.main(["paper", "--hours", "1"], out=io.StringIO()) == 2
    assert ("error: Another process (pid 1234 on my-laptop) is already running the tracker on data/cup/tracker.sqlite3: "
            "stop it first.") in capsys.readouterr().err
    assert closed == [True]


def test_paper_usage_errors(capsys: Any) -> None:
    assert cli.main(["paper", "--fast", "--hours", "1"], out=io.StringIO()) == 2
    assert "--fast needs --demo" in capsys.readouterr().err


def test_backtest_demo_prints_the_testability_table_first(methods: List[Tuple[str, str]], capsys: Any) -> None:
    out = io.StringIO()
    code = cli.main(["backtest", "--demo", "--demo-hours", "1"], out=out)
    assert code == 0, capsys.readouterr().err
    lines = _lines(out.getvalue())
    assert lines[0].startswith("Simulating the demo market for 1 h")
    assert lines[1] == "What this replay can test (per idea kind):"
    kinds = [line.split()[0] for line in lines[2:8]]
    assert kinds == ["value", "basket", "hole", "fade", "carry", "arbitrage"]
    order = [next(i for i, line in enumerate(lines) if line.startswith(prefix))
             for prefix in ("What this replay can test", "Window:", "Coverage:", "Assumptions:", "PORTFOLIO", "Headline verdict")]
    assert order == sorted(order)
    assert any("not replayable" in line for line in lines[2:8])
    assert any(line.startswith("Headline verdict (human:conservative): Not enough evidence yet") for line in lines)
    assert_get_only(methods)


def test_backtest_on_a_store_json_sweep_and_errors(tmp_path: Path, capsys: Any) -> None:
    data = tmp_path / "sim"
    assert cli.main(["paper", "--demo", "--fast", "--hours", "1.2", "--json", "--no-news", "--data-dir", str(data)],
                    out=io.StringIO()) == 0
    db = data / "demo" / "tracker.sqlite3"
    out = io.StringIO()
    assert cli.main(["backtest", "--store", str(db), "--hours", "1", "--json"], out=out) == 0, capsys.readouterr().err
    report = json.loads(out.getvalue())
    assert {"testability", "window", "coverage", "assumptions", "portfolios", "verdicts", "study", "warnings"} <= set(report)
    assert report["window"]["end"] - report["window"]["start"] == pytest.approx(3600.0)
    out = io.StringIO()
    assert cli.main(["backtest", "--store", str(db), "--hours", "1", "--step", "600", "--latency-sweep", "--json"], out=out) == 0
    swept = json.loads(out.getvalue())
    assert sorted(row["label"] for row in swept["sweep"]) == ["latency_s=120", "latency_s=30", "latency_s=300"]
    assert cli.main(["backtest", "--store", str(db), "--sweep", "no_such_knob=1,2"], out=io.StringIO()) == 2
    assert "no_such_knob" in capsys.readouterr().err
    assert cli.main(["backtest", "--store", str(tmp_path / "missing.sqlite3")], out=io.StringIO()) == 2
    assert "no such database" in capsys.readouterr().err


def test_backtest_finds_the_only_store(tmp_path: Path, capsys: Any) -> None:
    from supermarket_bot.store import TrackerStore

    data = tmp_path / "data"
    assert cli.main(["backtest", "--data-dir", str(data)], out=io.StringIO()) == 2
    assert "no tracker database under" in capsys.readouterr().err
    (data / "demo").mkdir(parents=True)
    TrackerStore(data / "demo" / "tracker.sqlite3").close()  # the demo's database is never picked
    (data / "cup").mkdir()
    TrackerStore(data / "cup" / "tracker.sqlite3").close()
    out = io.StringIO()
    assert cli.main(["backtest", "--data-dir", str(data), "--json"], out=out) == 0, capsys.readouterr().err
    assert json.loads(out.getvalue())["coverage"] is not None
    (data / "other").mkdir()
    TrackerStore(data / "other" / "tracker.sqlite3").close()
    assert cli.main(["backtest", "--data-dir", str(data)], out=io.StringIO()) == 2
    assert "several tracker databases" in capsys.readouterr().err
    out = io.StringIO()
    assert cli.main(["backtest", "--data-dir", str(data), "--tournament", "cup", "--json"], out=out) == 0


def test_fairvalue_demo_lists_matches_and_override_snippets(methods: List[Tuple[str, str]], capsys: Any) -> None:
    out = io.StringIO()
    assert cli.main(["fairvalue", "--demo"], out=out) == 0, capsys.readouterr().err
    lines = _lines(out.getvalue())
    assert lines[0].startswith("Outside fair values (auto, demo feed): 33 outcomes, ")
    tx = lines.index(next(line for line in lines if line.startswith("Will the Democratic Party win the Texas Senate?")))
    assert "[2026:SENATE:TX]" in lines[tx] and "(exchange 9026)" in lines[tx]
    assert "fair 0.580 (demo, high" in lines[tx + 1] and "venues: demo" in lines[tx + 1]
    assert "Providers:" in lines and any(line.startswith("  demo: ok") for line in lines)
    assert any(line.startswith("Map file: ") for line in lines)
    out = io.StringIO()
    assert cli.main(["fairvalue", "--demo", "--show-override", "9026"], out=out) == 0
    text = out.getvalue()
    assert '"9026": {"disabled": true, "note": "wrong match"}' in text and "fair_value_map.json" in text
    assert "the bot reloads it within a minute" in text
    out = io.StringIO()
    assert cli.main(["fairvalue", "--demo", "--only-usable", "--json"], out=out) == 0
    payload = json.loads(out.getvalue())
    assert payload["rows"] and all(r["fair"]["usable"] for r in payload["rows"])
    assert cli.main(["fairvalue", "--demo", "--show-override", "424242"], out=io.StringIO()) == 2
    assert "no open outcome with exchange id 424242" in capsys.readouterr().err
    assert_get_only(methods)


def test_fairvalue_live_manual_file_and_template(run: Any, cup: Any, fake: Any, data_dir: Path) -> None:
    fake.add("GET", T_MARKETS, market_page([market("m1", "Will the Democratic Party win the Texas Senate?", [("e1", None, 0.5)])]))
    fake.add("GET", "/exchanges/prices", {"data": [price("e1", "m1", 0.5, 0.49, 0.51)], "missingIds": []})
    code, text = run("--tournament", TOURNAMENT_SLUG, "fairvalue", "--fair-value", "manual", "--template")
    assert code == 0 and "Wrote " in text and "fair_values.json with 1 outcomes" in text
    path = data_dir / TOURNAMENT_SLUG / "fair_values.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert [v["exchange_id"] for v in doc["values"]] == ["e1"] and doc["values"][0]["probability"] is None
    code, text = run("--tournament", TOURNAMENT_SLUG, "fairvalue", "--fair-value", "manual", "--template")
    assert code == 0 and "already exists" in text  # never overwritten
    doc["values"][0]["probability"] = 0.58
    doc["values"][0]["updated_at"] = "2099-01-01T00:00:00Z"
    path.write_text(json.dumps(doc), encoding="utf-8")
    code, text = run("--tournament", TOURNAMENT_SLUG, "fairvalue", "--fair-value", "manual", "--json")
    assert code == 0
    payload = json.loads(text)
    assert payload["mode"] == "manual" and payload["counts"]["manual"] == 1
    [row] = payload["rows"]
    assert row["fair"]["source"] == "manual" and row["fair"]["value"] == pytest.approx(0.58)
    assert row["sm_bid"] == 0.49 and row["gap"] == pytest.approx(0.08)
    assert all(c.method == "GET" for c in fake.calls)  # Super Market reads only; manual mode reads no outside host


def test_paper_final_lines_name_the_settlements() -> None:
    """[integration] §12: the final `paper` output shows the settlements seen during the run (from the trades)."""
    body = {
        "now": 0.0, "run": {"run_id": "run-1", "end_reason": "completed", "hours_run": 2.0, "target_hours": 2.0},
        "portfolios": [], "headline": None, "table_warning": "", "caveats": [],
        "trades": [
            {"portfolio_id": "human:conservative", "exit_reason": "settled", "title": "Will the Maine debate happen?"},
            {"portfolio_id": "kind:value", "exit_reason": "settled", "title": "Will the Maine debate happen?"},
            {"portfolio_id": "kind:value", "exit_reason": "target", "title": "Other"},
        ],
    }
    lines = cli.paper_final_lines(body)
    assert "  Settled while running: 2 positions in 2 portfolios (Will the Maine debate happen?)." in lines
    body["trades"] = [t for t in body["trades"] if t["exit_reason"] != "settled"]
    assert not any(line.startswith("  Settled while running") for line in cli.paper_final_lines(body))


def test_ui_4_and_ui_7_paper_text_names_the_default_capital_and_legging_exits() -> None:
    """[integration, round 3] The text summary says when a run kept the default capital (ui-4) and lists legging exits of
    sets still held apart from the closed ideas (ui-7); a run on the account value says nothing extra."""
    port = {"portfolio_id": "human:conservative", "label": "You", "pnl_liq": 12.0, "pnl_liq_pct": 0.00012, "fills": 4,
            "trades_closed": 1, "positions_open": 2, "legging_trades": 2, "legging_pnl": -3.2}
    body = {"now": 0.0, "run": {"hours_run": 1.0, "target_hours": 2.0, "start_capital": 100000.0,
                                "capital_source": "default 100,000"}, "portfolios": [port], "headline": None}
    lines = cli.paper_summary_lines(body)
    assert lines[1].startswith("  Start capital: the default 100,000 (your account value was not known")
    assert lines[2].endswith("closed 1  open 2  (+2 legging exits -3, not closed ideas)")
    body["run"]["capital_source"] = "account value"
    port["legging_trades"] = 0
    lines = cli.paper_summary_lines(body)
    assert len(lines) == 2 and lines[1].endswith("closed 1  open 2")


# --------------------------------------------------------------------------- integration regressions (round 3)


def test_lookahead_5_and_lookahead_1_backtest_demo_text_has_the_demo_caveat_and_the_overlap(capsys: Any) -> None:
    """lookahead-5: every demo result shown carries DEMO_CAVEAT (the demo's outside prices lead the Cup by
    design), as `paper --demo` does. lookahead-1: the demo simulation the replay runs on is itself a paper run over
    exactly that window, so the text also says the backtest is not an independent check (about 1 h here)."""
    from supermarket_bot.paper import DEMO_CAVEAT, TABLE_WARNING

    out = io.StringIO()
    assert cli.main(["backtest", "--demo", "--demo-hours", "1"], out=out) == 0, capsys.readouterr().err
    lines = _lines(out.getvalue())
    assert f"Note: {DEMO_CAVEAT}" in lines
    head = next(i for i, line in enumerate(lines) if line.startswith("Headline verdict"))
    assert lines.index(f"Note: {DEMO_CAVEAT}") == head + 1  # right under the verdict it qualifies
    assert TABLE_WARNING.format(n=9) in lines and "Read the verdict with these in mind:" in lines
    overlap = [line for line in lines if "not an independent check" in line]
    assert len(overlap) == 1 and overlap[0].startswith("Warning: 1.0 h of this window were also seen by a paper run")
    assert "the ones the paper run read itself" in overlap[0]  # its real-book fills are the paper run's own reads


def test_lookahead_1_backtest_on_a_store_counts_a_completed_and_a_reset_paper_run(tmp_path: Path, capsys: Any) -> None:
    """lookahead-1 (§6.12.3, D26): the CLI backtest takes the overlap from the store's paper runs, so a window that
    a completed (or reset) run covered carries OVERLAP_WARNING and overlap_hours ~= the covered hours."""
    import sqlite3

    data = tmp_path / "sim"
    assert cli.main(["paper", "--demo", "--fast", "--hours", "1.2", "--json", "--no-news", "--data-dir", str(data)],
                    out=io.StringIO()) == 0
    db = data / "demo" / "tracker.sqlite3"
    with sqlite3.connect(db) as con:
        [(started, ended)] = con.execute("SELECT started_at, ended_at FROM paper_runs").fetchall()
    assert ended is not None  # completed: no longer the current run
    out = io.StringIO()
    assert cli.main(["backtest", "--store", str(db), "--hours", "1", "--json"], out=out) == 0, capsys.readouterr().err
    report = json.loads(out.getvalue())
    assert report["overlap_hours"] == pytest.approx(1.0, abs=1e-6)
    assert "not an independent check" in report["warnings"][0]
    # the same window seen by two runs (one ended by a reset, then a new one) is counted once
    with sqlite3.connect(db) as con:
        con.execute("UPDATE paper_runs SET ended_at = ? WHERE ended_at IS NOT NULL", (started + 1800.0,))
        con.execute("INSERT INTO paper_runs (run_id, started_at, config, state, updated_at, ended_at) "
                    "VALUES ('run-later', ?, '{}', '{}', ?, NULL)", (started + 1200.0, started + 1.2 * 3600))
    out = io.StringIO()
    assert cli.main(["backtest", "--store", str(db), "--hours", "1", "--json"], out=out) == 0
    report = json.loads(out.getvalue())
    window = report["window"]
    expected = (window["end"] - max(window["start"], started)) / 3600.0
    assert report["overlap_hours"] == pytest.approx(expected, abs=1e-6)
    text = io.StringIO()
    assert cli.main(["backtest", "--store", str(db), "--hours", "1"], out=text) == 0
    assert sum("not an independent check" in line for line in _lines(text.getvalue())) == 1


def test_live_5_a_live_paper_run_stops_cleanly_on_sighup(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """live-5: closing the terminal of a live `paper` run (SIGHUP) stops it like Ctrl-C: the runtime is closed
    (the tracker stops and releases its lease) and the handler is restored afterwards."""
    import os
    import signal
    import threading

    from supermarket_bot import web

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGHUP"):
        pytest.skip("needs the main thread and SIGHUP")

    class Unhandled(Exception):
        pass

    def fallback(signum: int, frame: Any) -> None:
        raise Unhandled("SIGHUP reached the test's own handler")

    closed: List[bool] = []

    class Tracker:
        paper_runner = object()
        running = True

        def paper_view(self) -> Dict[str, Any]:
            return {"run": {"hours_run": 0.0, "target_hours": 1.0}, "portfolios": []}

        def status(self) -> Dict[str, Any]:
            return {}

        def paper_end(self, reason: str) -> None:
            raise AssertionError("an interrupted run stays open")

    class Runtime:
        tracker = Tracker()
        app = type("A", (), {"paper": lambda self: {"run": {"hours_run": 0.0}, "portfolios": []}})()

        def start(self) -> None:
            threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGHUP)).start()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(web, "build_live", lambda settings, args, out=None: Runtime())
    original = signal.signal(signal.SIGHUP, fallback)
    try:
        code = cli.main(["paper", "--hours", "1"], out=io.StringIO())
        assert code == 130, capsys.readouterr().err
        assert closed == [True]
        assert signal.getsignal(signal.SIGHUP) is fallback  # restored
    finally:
        signal.signal(signal.SIGHUP, original)


# --------------------------------------------------------------------------- moves (docs/OUTSIDE_MOVES.md §17)


def test_moves_and_dashboard_moves_options_parse() -> None:
    from supermarket_bot import web

    parse = cli.build_parser().parse_args
    m = parse(["moves"])
    assert (m.demo, m.fast, m.hours, m.poll, m.interval, m.book_reads, m.bell, m.only_lagging, m.summary_every) == (
        False, False, None, 15.0, 30.0, 4, False, False, 15.0)
    assert (m.replay, m.since, m.store, m.no_news, m.json, m.data_dir, m.tournament) == (
        False, None, None, False, False, None, None)
    m = parse(["moves", "--demo", "--fast", "--hours", "1.5", "--poll", "30", "--interval", "20", "--book-reads", "0",
               "--bell", "--only-lagging", "--summary-every", "5", "--no-news", "--json", "--data-dir", "x",
               "--tournament", "cup", "--env-file", "e.env"])
    assert (m.demo, m.fast, m.hours, m.poll, m.interval, m.book_reads, m.bell, m.only_lagging, m.summary_every) == (
        True, True, 1.5, 30.0, 20.0, 0, True, True, 5.0)
    assert (m.no_news, m.json, m.data_dir, m.tournament, m.env_file) == (True, True, "x", "cup", "e.env")
    r = parse(["moves", "--replay", "--since", "2026-10-01T00:00:00Z", "--store", "t.sqlite3"])
    assert (r.replay, r.since, r.store) == (True, "2026-10-01T00:00:00Z", "t.sqlite3")
    d = parse(["dashboard"])
    assert (d.no_moves, d.moves_poll, d.moves_book_reads) == (False, 15.0, 4)
    d = parse(["dashboard", "--no-moves", "--moves-poll", "30", "--moves-book-reads", "0"])
    assert (d.no_moves, d.moves_poll, d.moves_book_reads) == (True, 30.0, 0)
    assert not hasattr(parse(["paper"]), "no_moves")  # the paper parser has no moves options: never the watcher
    for bad in (["moves", "--book-reads", "31"], ["moves", "--book-reads", "-1"], ["moves", "--hours", "0"],
                ["moves", "--summary-every", "0"], ["moves", "--poll", "often"], ["dashboard", "--moves-book-reads", "31"],
                ["dashboard", "--moves-poll", "x"]):
        with pytest.raises(SystemExit) as caught:
            parse(bad)
        assert caught.value.code == 2, bad


def test_dashboard_moves_options_reach_the_builders() -> None:
    """The parsed dashboard options become build_demo / build_live keywords (web.simulation_options, package server,
    docs/OUTSIDE_MOVES.md §18.1); ``paper`` has none of them, so it never runs the watcher."""
    from supermarket_bot import web

    parse = cli.build_parser().parse_args
    options = web.simulation_options(parse(["dashboard", "--no-moves", "--moves-poll", "30", "--moves-book-reads", "0"]))
    assert (options["moves"], options["moves_poll_s"], options["moves_book_reads_per_min"]) == (False, 30.0, 0)
    on = web.simulation_options(parse(["dashboard"]))
    assert (on["moves"], on["moves_poll_s"], on["moves_book_reads_per_min"]) == (True, 15.0, 4)
    assert web.simulation_options(parse(["paper"]))["moves"] is False  # the paper command never runs the watcher


@pytest.mark.parametrize("argv, message", [
    (["moves", "--fast"], "--fast needs --demo"),
    (["moves", "--demo", "--poll", "4"], "--poll must be between 5 and 120 seconds"),
    (["moves", "--demo", "--poll", "121"], "--poll must be between 5 and 120 seconds"),
    (["moves", "--demo", "--interval", "0.5"], "--interval must be at least 1 second"),
    (["moves", "--demo", "--since", "2026-10-01T00:00:00Z"], "--since and --store are for --replay"),
    (["moves", "--replay", "--demo"], "--replay reads a stored database"),
    (["dashboard", "--demo", "--moves-poll", "4"], "--moves-poll must be between 5 and 120 seconds"),
    (["dashboard", "--demo", "--moves-poll", "500"], "--moves-poll must be between 5 and 120 seconds"),
])
def test_moves_usage_errors_exit_2(argv: List[str], message: str, capsys: Any) -> None:
    assert cli.main(argv, out=io.StringIO()) == 2
    assert message in capsys.readouterr().err


def test_moves_replay_and_live_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    assert cli.main(["moves", "--replay", "--store", str(tmp_path / "missing.sqlite3")], out=io.StringIO()) == 2
    assert "no such database" in capsys.readouterr().err
    assert cli.main(["moves", "--replay", "--data-dir", str(tmp_path / "empty")], out=io.StringIO()) == 2
    assert "no tracker database under" in capsys.readouterr().err
    monkeypatch.delenv("SUPERMARKET_API_KEY")
    assert cli.main(["moves", "--env-file", str(tmp_path / "none.env")], out=io.StringIO()) == 2  # live needs a key
    assert "SUPERMARKET_API_KEY" in capsys.readouterr().err


def test_dashboard_moves_options_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from supermarket_bot import web

    seen: List[Any] = []
    monkeypatch.setattr(web, "run_dashboard", lambda settings, args, out=None: seen.append(args) or 0)
    assert cli.main(["dashboard", "--demo", "--no-moves", "--moves-poll", "20", "--moves-book-reads", "2"], out=io.StringIO()) == 0
    assert (seen[0].no_moves, seen[0].moves_poll, seen[0].moves_book_reads) == (True, 20.0, 2)
    assert cli.main(["dashboard", "--demo"], out=io.StringIO()) == 0
    assert (seen[1].no_moves, seen[1].moves_poll, seen[1].moves_book_reads) == (False, 15.0, 4)


class _FakeWatcher:
    """Stands in for moves.OutsideMoveWatcher in the CLI plumbing tests."""

    poll_s = 15.0

    def __init__(self, alerts_path: Path) -> None:
        self.alerts_path = alerts_path
        self.listeners: List[Any] = []

    def add_listener(self, fn: Any) -> None:
        self.listeners.append(fn)

    def summary(self) -> Dict[str, Any]:
        return {"watching": {"outcomes": 237, "matched": 231, "venues": 2},
                "venues": [{"venue": "polymarket", "matched": 231}, {"venue": "kalshi", "matched": 220}],
                "summary": {"sentence": "No outside move has been measured yet."}, "counts": {}, "caveats": ["C1", "C2"]}

    def emit(self, kind: str, alert_id: str, status: str, outcome: str = "pending") -> None:
        from supermarket_bot.moves import MoveEvent

        event = MoveEvent(kind=kind, at=1_791_209_115.0, alert_id=alert_id, status=status, lag_outcome=outcome,
                          alert={"alert_id": alert_id, "status": status})
        for fn in self.listeners:
            fn(event)


class _FakeTracker:
    def __init__(self, watcher: _FakeWatcher, stop_after: int = 10_000, interrupt: bool = False) -> None:
        self.moves = watcher
        self.checks = 0
        self.stop_after = stop_after
        self.interrupt = interrupt
        self._clock = lambda: 1_791_209_400.0

    @property
    def running(self) -> bool:
        self.checks += 1
        if self.checks == 1:  # the watcher's first events arrive while the command waits
            self.moves.emit("opened", "mv-1", "lagging")
            self.moves.emit("opened", "mv-2", "moved_first", "cup_first")
            self.moves.emit("status", "mv-1", "already_moved", "followed")
            self.moves.emit("status", "mv-2", "moved_first", "cup_first")
            self.moves.emit("closed", "mv-1", "already_moved", "followed")
        if self.interrupt and self.checks >= 2:
            raise KeyboardInterrupt
        return self.checks < self.stop_after

    def status(self) -> Dict[str, Any]:
        return {"fatal_error": None}

    def moves_view(self) -> Dict[str, Any]:
        return self.moves.summary()


@pytest.fixture
def fake_moves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Dict[str, Any]:
    """``moves`` (live) against a fake runtime, with moves.format_event_lines / summary_lines stubbed: the CLI's
    own plumbing (start line, listener, filters, bell flag, JSON, exit codes), independent of the watcher."""
    import supermarket_bot.moves as moves_mod
    from supermarket_bot import web

    state: Dict[str, Any] = {"bells": [], "built": [], "closed": 0, "interrupt": False}

    def fake_lines(event: Any, *, bell: bool = False) -> List[str]:
        state["bells"].append(bell)
        return [f"[moves 14:05:15] {event.kind.upper()} {event.alert_id} {event.status}" + ("\a" if bell else "")]

    monkeypatch.setattr(moves_mod, "format_event_lines", fake_lines)
    monkeypatch.setattr(moves_mod, "summary_lines", lambda summary, venues, now: [f"[moves] summary: {summary['sentence']}",
                                                                                  f"    venues: {len(venues)}"])

    class Runtime:
        def __init__(self) -> None:
            self.moves = _FakeWatcher(tmp_path / "data" / "cup" / "alerts.jsonl")
            self.tracker = _FakeTracker(self.moves, stop_after=3, interrupt=state["interrupt"])
            self.app = None

        def start(self) -> None:
            pass

        def close(self) -> None:
            state["closed"] += 1

    def build_live(settings: Any, args: Any, out: Any = None) -> Any:
        state["built"].append(args)
        return Runtime()

    monkeypatch.setattr(web, "build_live", build_live)
    return state


def test_moves_live_plumbing_text(fake_moves: Dict[str, Any]) -> None:
    out = io.StringIO()
    assert cli.main(["moves", "--hours", "1"], out=out) == 1  # the fake tracker stops: exit 1 after the final block
    args = fake_moves["built"][0]
    assert (args.no_paper, args.no_moves, args.moves_poll, args.moves_book_reads) == (True, False, 15.0, 4)
    lines = _lines(out.getvalue())
    assert lines[0].startswith("Watching outside prices: Polymarket and Kalshi every 15 s for 231 matched outcomes "
                               "(GET only, no account). Alerts also go to ")
    assert lines[0].endswith("alerts.jsonl. Nothing is traded. Ctrl-C to stop.")
    assert lines[1:5] == ["[moves 14:05:15] OPENED mv-1 lagging", "[moves 14:05:15] OPENED mv-2 moved_first",
                          "[moves 14:05:15] STATUS mv-1 already_moved", "[moves 14:05:15] STATUS mv-2 moved_first"]
    assert "CLOSED" not in out.getvalue()  # closed events print nothing in text mode
    assert lines[5:] == ["[moves] summary: No outside move has been measured yet.", "    venues: 2", "  - C1", "  - C2"]
    assert fake_moves["bells"] == [False] * 4 and fake_moves["closed"] == 1


def test_moves_live_plumbing_bell_only_lagging_and_json(fake_moves: Dict[str, Any]) -> None:
    out = io.StringIO()
    cli.main(["moves", "--bell", "--only-lagging"], out=out)
    events = [line for line in _lines(out.getvalue()) if line.startswith("[moves 14")]
    assert events == ["[moves 14:05:15] OPENED mv-1 lagging\a", "[moves 14:05:15] STATUS mv-1 already_moved\a"]
    assert fake_moves["bells"] == [True, True]  # the bell flag goes to format_event_lines (actionable openings only)
    out = io.StringIO()
    cli.main(["moves", "--json"], out=out)
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(r["type"], r.get("event")) for r in rows] == [("event", "opened"), ("event", "opened"), ("event", "status"),
                                                            ("event", "status"), ("event", "closed"), ("summary", None)]
    assert rows[0]["alert"] == {"alert_id": "mv-1", "status": "lagging"}
    assert rows[-1]["final"] is True and rows[-1]["caveats"] == ["C1", "C2"] and len(rows[-1]["venues"]) == 2


def test_moves_live_ctrl_c_prints_the_final_block_and_exits_130(fake_moves: Dict[str, Any]) -> None:
    fake_moves["interrupt"] = True
    out = io.StringIO()
    assert cli.main(["moves"], out=out) == 130
    assert "[moves] summary: No outside move has been measured yet." in out.getvalue() and fake_moves["closed"] == 1


def test_moves_live_lease_conflict_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from supermarket_bot import web
    from supermarket_bot.tracker import TrackerBusy

    class BusyRuntime:
        def __init__(self) -> None:
            self.moves = _FakeWatcher(Path("alerts.jsonl"))
            self.tracker = _FakeTracker(self.moves)

        def start(self) -> None:
            raise TrackerBusy({"pid": 1234, "host": "my-laptop"}, "data/cup/tracker.sqlite3")

        def close(self) -> None:
            pass

    monkeypatch.setattr(web, "build_live", lambda settings, args, out=None: BusyRuntime())
    assert cli.main(["moves"], out=io.StringIO()) == 2
    assert "is already running the tracker on data/cup/tracker.sqlite3" in capsys.readouterr().err


# ---- the real demo (needs the watcher and the builders: docs/OUTSIDE_MOVES.md §15.2 outcomes)

MOVES_DEMO_HOURS = "2"


@pytest.fixture(scope="module")
def moves_demo_run(tmp_path_factory: Any) -> Dict[str, Any]:
    """``moves --demo --fast --hours 2 --bell`` once, with --data-dir kept for --replay."""
    import time

    data = tmp_path_factory.mktemp("moves") / "kept"
    out = io.StringIO()
    started = time.monotonic()
    code = _moves_main(["moves", "--demo", "--fast", "--hours", MOVES_DEMO_HOURS, "--bell", "--no-news", "--data-dir",
                        str(data)], out)
    return {"code": code, "text": out.getvalue(), "data": data, "seconds": time.monotonic() - started}


def _moves_main(argv: List[str], out: Any) -> int:
    """``cli.main`` for a real ``moves --demo`` run (docs/OUTSIDE_MOVES.md §18.1)."""
    return cli.main(argv, out=out)


def test_moves_demo_fast_two_hours_text(moves_demo_run: Dict[str, Any]) -> None:
    from supermarket_bot.moves import MOVES_CAVEATS, MOVES_DEMO_CAVEAT

    assert moves_demo_run["code"] == 0
    text = moves_demo_run["text"]
    lines = _lines(text)
    assert lines[0] == ("Watching the demo's scripted outside venues (Demo venue A, Demo venue B) every 15 s. Demo data: "
                        "the moves are scripted. Nothing is traded.")
    assert lines[1].startswith("Alerts are written to ") and lines[1].endswith("demo/alerts.jsonl.")
    heads = [line for line in lines if line.startswith("[moves ") and "] summary:" not in line]
    tags = {tag for tag in ("CUP LAGGING", "FOLLOWED", "CUP MOVED FIRST", "REVERTED", "NOT FOLLOWED") if any(tag in h for h in heads)}
    assert tags == {"CUP LAGGING", "FOLLOWED", "CUP MOVED FIRST", "REVERTED", "NOT FOLLOWED"}
    assert any("New Hampshire Senate (D)" in h and "CUP LAGGING" in h for h in heads)
    assert any(line.startswith("    suggestion (not a sure thing): Buy YES at ") for line in lines)
    assert any(line.startswith("    suggestion (not a sure thing): Buy NO at ") for line in lines)  # Ohio: the outside fell
    assert not any("Georgia Governor" in h or "Michigan Governor" in h for h in heads)  # 9038 / 9041 never alert
    summaries = [line for line in lines if "] summary: " in line]
    assert summaries and "far too few to say whether the Cup lags" in summaries[-1]
    # every 15 simulated minutes and once at the end (the final block is not repeated by the periodic one)
    assert len(summaries) == 8 and len({s.split("]")[0] for s in summaries}) == 8
    assert summaries[-1].startswith("[moves 16:00:00] summary: ")
    for caveat in list(MOVES_CAVEATS) + [MOVES_DEMO_CAVEAT]:
        assert f"  - {caveat}" in lines
    # (integration) the final block gives the lag numbers with their sample sizes, not only the sentence
    study = [line for line in lines if line.startswith("    lag study: ")]
    assert study == ["    lag study: 7 resolved outside-led moves on 4 races; 2 where the Cup moved first; not counted: "
                     "3 excluded (converging or no Cup base), 0 censored, 0 still being measured"]
    at = lines.index(study[0])
    assert lines[at + 1].startswith("      followed within 1 min 0/7 (0%), 5 min ") and "1 h 4/7 (57%)" in lines[at + 1]
    assert lines[at + 2] == "      never followed 3/7 (43%): 2 came back, 1 did not follow within 60 min"
    assert any(line.startswith("      gap left 4 min after the alert, net of the spread: mean ") and "(n=7; " in line
               for line in lines[at:at + 6])
    assert any(line.startswith("      bought 4 min after the alert, sold 30 min later: mean ") for line in lines[at:at + 6])
    # --bell: the bell only on an actionable opening (a lagging alert with a suggestion), on its first line
    belled = [line for line in lines if "\a" in line]
    assert belled and all(line.startswith("[moves ") and "CUP LAGGING" in line for line in belled)
    assert all(line.endswith("\a") for line in belled)


def test_moves_demo_fast_replay_prints_the_same_alerts_and_summary(moves_demo_run: Dict[str, Any]) -> None:
    db = moves_demo_run["data"] / "demo" / "tracker.sqlite3"
    assert db.is_file() and (moves_demo_run["data"] / "demo" / "alerts.jsonl").is_file()
    out = io.StringIO()
    assert cli.main(["moves", "--replay", "--store", str(db)], out=out) == 0
    replay = _lines(out.getvalue())
    run = _lines(moves_demo_run["text"])
    assert replay[0].startswith("Replaying 12 stored outside-move alert(s) from ")

    def races(lines: List[str]) -> List[str]:
        return sorted({line.split("  ")[1] for line in lines if line.startswith("[moves ") and "] summary:" not in line})

    assert races(replay) == races(run)
    sentence = lambda lines: [line.split("] summary: ", 1)[1] for line in lines if "] summary: " in line][-1]  # noqa: E731
    assert sentence(replay) == sentence(run)
    study = lambda lines: [line for line in lines if line.startswith("    lag study: ") or line.startswith("      ")]  # noqa: E731
    assert study(replay) and study(replay) == study(run)  # the same numbers from the database
    # suppressed moves are not stored: the replay says so instead of "nothing"
    assert "    filtered out: not kept in the database (a replay shows the alerts only)" in replay
    assert not any(line.startswith("    filtered out today: ") for line in replay)

    def tags(lines: List[str]) -> Dict[str, int]:
        heads = [line for line in lines if line.startswith("[moves ") and "] summary:" not in line]
        return {tag: sum(1 for h in heads if f"] {tag}  " in h) for tag in
                ("CUP LAGGING", "CUP MOVED FIRST", "FOLLOWED", "REVERTED", "NOT FOLLOWED", "CENSORED")}

    assert tags(replay) == tags(run)  # one opening per alert, one status line per resolved lag (no duplicates)
    assert tags(run)["CUP MOVED FIRST"] == 2 and tags(run)["FOLLOWED"] == 4 and tags(run)["NOT FOLLOWED"] == 1
    # the suggestion as it was when the alert opened (alerts.jsonl), not the closed alert's empty trade
    suggestions = lambda lines: [line for line in lines if line.startswith("    suggestion (not a sure thing): ")]  # noqa: E731
    assert suggestions(replay) == suggestions(run) and suggestions(run)
    assert not any(line.startswith("    no trade suggested") for line in replay)
    out = io.StringIO()
    assert cli.main(["moves", "--replay", "--store", str(db), "--json"], out=out) == 0
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["type"] for r in rows].count("alert") == 12 and rows[-1]["type"] == "summary"
    out = io.StringIO()
    assert cli.main(["moves", "--replay", "--store", str(db), "--only-lagging"], out=out) == 0
    only = tags(_lines(out.getvalue()))
    assert only["CUP LAGGING"] == tags(run)["CUP LAGGING"] and only["CUP MOVED FIRST"] == 0


def test_moves_replay_without_alerts_jsonl_uses_the_stored_opening_suggestion(moves_demo_run: Dict[str, Any],
                                                                               tmp_path: Path) -> None:
    """Without alerts.jsonl the replay prints each alert's suggestion as it opened from the database (the alert
    keeps ``opened_trade`` after the live trade is cleared on close); only a row stored WITHOUT it says the
    suggestion was not kept, and never "no trade suggested" (it never claims there was no edge)."""
    import shutil
    import sqlite3

    src = moves_demo_run["data"] / "demo"
    for f in src.glob("tracker.sqlite3*"):
        shutil.copy(f, tmp_path / f.name)
    db = tmp_path / "tracker.sqlite3"
    out = io.StringIO()
    assert cli.main(["moves", "--replay", "--store", str(db)], out=out) == 0
    lines = _lines(out.getvalue())
    suggestions = lambda ls: [line for line in ls if line.startswith("    suggestion (not a sure thing): ")]  # noqa: E731
    assert suggestions(lines) and suggestions(lines) == suggestions(_lines(moves_demo_run["text"]))
    assert not any(line.startswith("    suggestion at the time: not kept") for line in lines)
    assert not any(line.startswith("    no trade suggested") for line in lines)
    # an alert row stored without its opening suggestion (an older build): says so instead of inventing one
    con = sqlite3.connect(str(db))
    try:
        for alert_id, data in con.execute("SELECT alert_id, data FROM move_alerts").fetchall():
            row = json.loads(data)
            for key in ("opened_trade", "opened_trade_note"):
                row.pop(key, None)
            con.execute("UPDATE move_alerts SET data = ? WHERE alert_id = ?", (json.dumps(row), alert_id))
        con.commit()
    finally:
        con.close()
    out = io.StringIO()
    assert cli.main(["moves", "--replay", "--store", str(db)], out=out) == 0
    lines = _lines(out.getvalue())
    kept = [line for line in lines if line.startswith("    suggestion at the time: not kept")]
    lagging = [line for line in lines if "] CUP LAGGING  " in line]
    assert lagging and len(kept) == len(lagging)
    assert not any(line.startswith("    no trade suggested") for line in lines)  # never claims there was no edge


def test_moves_demo_fast_json_only_lagging_and_determinism(methods: List[Tuple[str, str]]) -> None:
    outs = []
    for argv in (["--json"], ["--json"], ["--only-lagging"]):
        out = io.StringIO()
        assert _moves_main(["moves", "--demo", "--fast", "--hours", "0.25", "--no-news", *argv], out) == 0
        outs.append(out.getvalue())
    assert outs[0] == outs[1]  # deterministic (D42)
    rows = [json.loads(line) for line in outs[0].splitlines()]
    assert {r["type"] for r in rows} == {"event", "summary"} and rows[-1]["final"] is True
    opened = [r["alert"] for r in rows if r["type"] == "event" and r["event"] == "opened"]
    assert {a["exchange_id"] for a in opened} >= {"9035", "9036", "9037", "9039", "9040"}
    assert not {a["exchange_id"] for a in opened} & {"9038", "9041"}
    lagging = [line for line in _lines(outs[2]) if line.startswith("[moves ") and "] summary:" not in line]
    assert lagging and not any("CUP MOVED FIRST" in line for line in lagging)
    assert any("CUP LAGGING" in line for line in lagging)
    assert_get_only(methods)
