from conftest import TOURNAMENT_SLUG, market, market_page, price, tournament, tournament_page

from supermarket_bot.bot import MarketDataBot, resolve_context


def test_end_to_end_snapshot(fake, client):
    fake.add("GET", "/tournaments", tournament_page([tournament()]))
    fake.add("GET", f"/tournaments/{TOURNAMENT_SLUG}/markets", market_page([market("26", "Who wins?", [("36", "A", 0.4), ("37", "B", 0.6)])]))
    fake.add("GET", "/exchanges/prices", {"data": [price("36", "26", 0.41, 0.40, 0.42, "A"), price("37", "26", 0.6, 0.58, 0.61, "B")], "missingIds": []})
    ctx = resolve_context(client)
    snap = MarketDataBot(client, ctx).snapshot()
    assert [r["exchange_id"] for r in snap.rows] == ["36", "37"]
    assert snap.rows[0]["mid"] == 0.41
    prices_call = fake.calls_to("/exchanges/prices")[0]
    assert prices_call.params == {"ids": "36,37", "tournamentId": ctx.tournament_id}
    assert fake.calls[0].headers["authorization"].startswith("Bearer ace_test")
