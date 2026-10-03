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
    assert "Pokémon odds" in out  # JSON is written unescaped (ensure_ascii=False)
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
