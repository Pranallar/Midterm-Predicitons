"""Tests for supermarket_bot/news.py: query building, relevance, providers and the searcher.

Everything is offline: providers get an ``httpx.Client`` on an ``httpx.MockTransport`` that
serves realistic Google News RSS, GDELT and NewsAPI payloads; time is a FakeClock and
sleeping is recorded, never real.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import FakeClock, Sleeper

from supermarket_bot import __version__
from supermarket_bot import news as news_mod
from supermarket_bot.models import Article
from supermarket_bot.news import (
    GDELTDoc,
    GoogleNewsRSS,
    NewsAPIOrg,
    NewsProvider,
    NewsSearcher,
    build_query,
    default_providers,
    keywords,
    relevance,
)


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


NOW = ts("2026-10-03T12:00:00Z")

# --------------------------------------------------------------------------- fixtures

RSS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<generator>NFE/5.0</generator>
<title>"Katie Hobbs" Arizona Governor when:2d - Google News</title>
<link>https://news.google.com/search?q=%22Katie+Hobbs%22+Arizona+Governor+when:2d&amp;hl=en-US&amp;gl=US&amp;ceid=US:en</link>
<language>en-US</language>
<webMaster>news-webmaster@google.com</webMaster>
<copyright>2026 Google LLC</copyright>
<lastBuildDate>Sat, 03 Oct 2026 11:58:12 GMT</lastBuildDate>
<description>Google News</description>
<item>
<title>Katie Hobbs surges ahead in new Arizona governor poll - The Arizona Republic</title>
<link>https://news.google.com/rss/articles/CBMiK2h0dHBzOi8vd3d3LmF6Y2VudHJhbC5jb20vc3Rvcnkv0gEA?oc=5</link>
<guid isPermaLink="false">CBMiK2h0dHBzOi8vd3d3LmF6Y2VudHJhbC5jb20vc3Rvcnkv0gEA</guid>
<pubDate>Sat, 03 Oct 2026 11:05:00 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMiK2h0dHBzOi8vd3d3LmF6Y2VudHJhbC5jb20vc3Rvcnkv0gEA?oc=5" target="_blank"&gt;Katie Hobbs surges ahead in new Arizona governor poll&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;The Arizona Republic&lt;/font&gt;</description>
<source url="https://www.azcentral.com">The Arizona Republic</source>
</item>
<item>
<title>Hobbs, Lake trade barbs over the border in final debate - AP News</title>
<link>https://apnews.com/article/arizona-governor-debate-2026</link>
<guid isPermaLink="false">CBMiAP</guid>
<pubDate>Fri, 02 Oct 2026 22:30:00 +0000</pubDate>
<description>Gov. Katie Hobbs and her challenger clashed over border policy on Friday.</description>
</item>
<item>
<title>Election officials across Arizona prepare for the midterms</title>
<link>https://www.example.com/az-officials</link>
<guid isPermaLink="false">CBMiEX</guid>
<pubDate>sometime last week</pubDate>
<source url="https://www.example.com">Example Times</source>
</item>
</channel>
</rss>
"""

GDELT = {
    "articles": [
        {
            "url": "https://www.azcentral.com/story/news/politics/elections/2026/10/03/katie-hobbs-poll/",
            "url_mobile": "",
            "title": "Katie Hobbs surges ahead in new Arizona governor poll",
            "seendate": "20261003T111500Z",
            "socialimage": "https://www.azcentral.com/img.jpg",
            "domain": "azcentral.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {
            "url": "https://www.foxnews.com/politics/arizona-governor-race-tightens",
            "url_mobile": "https://m.foxnews.com/politics/arizona-governor-race-tightens",
            "title": "Arizona governor race tightens as Hobbs and Lake spar",
            "seendate": "20261002T180000Z",
            "socialimage": "",
            "domain": "foxnews.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {"url": "https://example.org/untitled", "title": "", "seendate": "20261002T180000Z", "domain": "example.org"},
        {"url": "https://example.org/bad-date", "title": "Hobbs campaign memo", "seendate": "yesterday", "domain": "example.org"},
    ]
}

NEWSAPI = {
    "status": "ok",
    "totalResults": 3,
    "articles": [
        {
            "source": {"id": "reuters", "name": "Reuters"},
            "author": "Jane Reporter",
            "title": "Hobbs gains in Arizona governor race after debate",
            "description": "<p>Democratic Gov. Katie Hobbs widened her lead &amp; raised more money.</p>",
            "url": "https://www.reuters.com/world/us/hobbs-gains-2026-10-03/",
            "urlToImage": None,
            "publishedAt": "2026-10-03T10:45:00Z",
            "content": "PHOENIX (Reuters) - ...",
        },
        {
            "source": {"id": None, "name": "[Removed]"},
            "author": None,
            "title": "[Removed]",
            "description": "[Removed]",
            "url": "https://removed.com",
            "urlToImage": None,
            "publishedAt": "2026-10-03T10:00:00Z",
            "content": "[Removed]",
        },
        {
            "source": {"id": "cnn", "name": "CNN"},
            "author": None,
            "title": "Arizona races to watch",
            "description": None,
            "url": "https://www.cnn.com/2026/10/03/politics/arizona-races",
            "urlToImage": None,
            "publishedAt": "2026-10-03T09:00:00Z",
            "content": None,
        },
    ],
}


class Server:
    """A MockTransport handler that records requests and answers with a queued reply."""

    def __init__(self, *replies: Any) -> None:
        self.replies: List[Any] = list(replies)
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def params(self, i: int = -1) -> Dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(str(self.requests[i].url)).query).items()}


def rss_reply(body: str = RSS, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body.encode("utf-8"), headers={"Content-Type": "application/xml; charset=UTF-8"})


# --------------------------------------------------------------------------- query building


@pytest.mark.parametrize(
    "title, option, expected",
    [
        ("Will Republicans win the Pennsylvania Senate race?", None, "Republicans Pennsylvania Senate"),
        ("Will Republicans win the Pennsylvania Senate race?", "YES", "Republicans Pennsylvania Senate"),
        ("Will Republicans win the Pennsylvania Senate race?", "No", "Republicans Pennsylvania Senate"),
        ("Who will win the Arizona Governor race?", "Katie Hobbs", '"Katie Hobbs" Arizona Governor'),
        ("Will Democrats control the House after the 2026 midterms?", None, "Democrats House midterms"),
        ("Will turnout exceed 50% in Georgia?", None, "turnout Georgia"),
        ("Who will win the 2026 New York Governor race?", "Kathy Hochul (D)", '"Kathy Hochul" "New York" Governor'),
        ("Who will be the next Attorney General of Texas?", "Ken Paxton", '"Ken Paxton" "Attorney General" Texas'),
        ("WILL REPUBLICANS WIN THE PENNSYLVANIA SENATE RACE?", None, "Republicans Pennsylvania Senate"),
        ("Will Republicans Win The Pennsylvania Senate Race In November?", None, "Republicans Pennsylvania Senate"),
        ("Which party wins the PA-07 House seat?", "Republican", "Republican Pennsylvania House"),
        ("Will Hobbs, Lake or Robson win AZ Gov?", None, "Hobbs Lake Robson Arizona Governor"),
        ("Will the U.S. Senate have 51+ Republican seats on Jan. 3, 2027?", None, "Senate Republican"),
        ("Ohio House District 9: will the GOP flip it?", None, "Ohio House District GOP"),
        ("Who wins the Georgia Senate runoff?", "Raphael Warnock", '"Raphael Warnock" Georgia Senate runoff'),
        ("Will Florida's abortion amendment pass?", None, "Florida abortion amendment"),
    ],
)
def test_build_query(title: str, option: Optional[str], expected: str) -> None:
    assert build_query(title, option) == expected


def test_build_query_drops_scaffolding_years_and_numbers() -> None:
    query = build_query("Will the Democrats win more than 220 seats in the 2026 House election?")
    assert query == "Democrats House"
    for word in ("Will", "the", "win", "2026", "220", "election", "?"):
        assert word not in query.split()


def test_build_query_caps_terms_at_six() -> None:
    title = "Will Alice Smith, Bob Jones, Carol White, Dan Brown or Eve Black win the Ohio Senate primary?"
    query = build_query(title, "Frank Green")
    terms = keywords(title, "Frank Green")
    assert len(terms) == 6
    assert terms[0] == "frank green"  # the option always comes first and has top priority
    assert query.startswith('"Frank Green"')


@pytest.mark.parametrize("option", [None, "", "YES", "no", "Other", "50% or more", "over", "Tie"])
def test_non_name_options_are_ignored(option: Optional[str]) -> None:
    assert build_query("Who will win the Arizona Governor race?", option) == "Arizona Governor"


def test_option_already_in_title_is_not_repeated() -> None:
    assert build_query("Will Katie Hobbs win the Arizona Governor race?", "Katie Hobbs") == '"Katie Hobbs" Arizona Governor'


def test_build_query_fallback_and_empty() -> None:
    assert build_query("will it rain?") == "rain"
    assert build_query("") == ""
    assert build_query("2026?") == ""
    assert keywords("") == []


def test_keywords_are_lowercase_query_terms() -> None:
    assert keywords("Who will win the Arizona Governor race?", "Katie Hobbs") == ["katie hobbs", "arizona", "governor"]
    assert keywords("Will turnout exceed 50% in Georgia?") == ["turnout", "georgia"]


def test_strong_terms_are_option_candidate_and_state() -> None:
    strong = news_mod._strong_terms("Who will win the Arizona Governor race?", "Katie Hobbs")
    assert strong == ["katie hobbs", "arizona"]
    assert news_mod._strong_terms("Will the Democratic candidate win Ohio's 9th Congressional District?") == ["ohio"]


# --------------------------------------------------------------------------- relevance


def art(title: str, summary: Optional[str] = None, **kw: Any) -> Article:
    return Article(title=title, url=kw.pop("url", "https://example.com/" + title.replace(" ", "-")), summary=summary, **kw)


def test_relevance_weights_strong_terms_double() -> None:
    terms = ["katie hobbs", "arizona", "governor"]
    strong = ["katie hobbs", "arizona"]
    assert relevance(art("Katie Hobbs leads Arizona governor race"), terms, strong) == 1.0
    assert relevance(art("Arizona weather update"), terms, strong) == pytest.approx(2 / 5)
    assert relevance(art("Governor signs a bill"), terms, strong) == pytest.approx(1 / 5)
    assert relevance(art("Governor signs a bill"), terms) == pytest.approx(1 / 3)  # no strong terms: equal weights


def test_relevance_word_boundaries_case_and_summary() -> None:
    terms = ["house", "democrats"]
    assert relevance(art("Household budgets squeezed"), terms) == 0.0  # "house" is not "household"
    assert relevance(art("DEMOCRATS rally", summary="The House vote is close"), terms) == 1.0
    assert relevance(art("Democrats' message"), ["democrats"]) == 1.0  # possessive
    assert relevance(art("A Democratic sweep"), ["democrats"]) == 1.0  # party synonym


def test_relevance_aliases_and_surnames() -> None:
    assert relevance(art("GOP gains ground in Pennsylvania"), ["republicans", "pennsylvania"]) == 1.0
    assert relevance(art("Gubernatorial debate set"), ["governor"]) == 1.0
    assert relevance(art("Midterm turnout surges"), ["midterms", "turnout"]) == 1.0
    terms, strong = ["katie hobbs", "arizona"], ["katie hobbs", "arizona"]
    assert relevance(art("Hobbs’s lead grows"), terms, strong) == pytest.approx(0.5)  # surname alone counts
    assert relevance(art("Hobbs lead grows"), ["katie hobbs"]) == 0.0  # only for strong (candidate) terms
    assert relevance(art("Ocasio Cortez speaks"), ["alexandria ocasio-cortez"], ["alexandria ocasio-cortez"]) == 1.0


def test_relevance_edge_cases() -> None:
    assert relevance(art("Anything"), []) == 0.0
    assert relevance(art("Anything"), ["", "  "]) == 0.0
    assert relevance(Article(title="", url="u", summary=None), ["arizona"]) == 0.0
    score = relevance(art("Arizona Arizona Arizona"), ["arizona", "arizona"], ["arizona"])
    assert 0.0 <= score <= 1.0 and score == 1.0  # duplicates do not push it past 1


# --------------------------------------------------------------------------- Google News RSS


def test_google_news_rss_parses_items() -> None:
    server = Server(rss_reply())
    provider = GoogleNewsRSS(server.client(), clock=lambda: NOW)
    since = NOW - 36 * 3600
    articles = provider.search('"Katie Hobbs" Arizona Governor', since, NOW, limit=20)

    params = server.params()
    assert str(server.requests[0].url).startswith("https://news.google.com/rss/search?")
    assert params == {"q": '"Katie Hobbs" Arizona Governor when:2d', "hl": "en-US", "gl": "US", "ceid": "US:en"}
    assert [a.title for a in articles] == [
        "Katie Hobbs surges ahead in new Arizona governor poll",
        "Hobbs, Lake trade barbs over the border in final debate",
        "Election officials across Arizona prepare for the midterms",
    ]
    first, second, third = articles
    assert first.source == "The Arizona Republic"  # from <source>, suffix stripped from the title
    assert first.url.startswith("https://news.google.com/rss/articles/")
    assert first.published_at == ts("2026-10-03T11:05:00Z")
    assert first.summary is None  # the description only repeated title + source
    assert first.provider == "google-news"
    assert second.source == "AP News"  # no <source>: taken from the " - Source" suffix
    assert second.published_at == ts("2026-10-02T22:30:00Z")
    assert second.summary == "Gov. Katie Hobbs and her challenger clashed over border policy on Friday."
    assert third.published_at is None  # unparseable pubDate
    assert third.source == "Example Times"
    assert provider.last_error is None


def test_google_news_window_and_limit() -> None:
    server = Server(rss_reply())
    provider = GoogleNewsRSS(server.client(), clock=lambda: NOW)
    # since after the second item: it is dropped; the undated one is kept
    articles = provider.search("q", ts("2026-10-03T00:00:00Z"), NOW, limit=20)
    assert [a.source for a in articles] == ["The Arizona Republic", "Example Times"]
    assert server.params()["q"] == "q when:1d"
    # until before the first item drops it
    assert [a.source for a in provider.search("q", None, ts("2026-10-03T11:00:00Z"), limit=20)] == ["AP News", "Example Times"]
    assert server.params()["q"] == "q when:7d"  # no since: a week
    assert len(provider.search("q", None, None, limit=1)) == 1
    assert provider.search("", None, None) == [] and provider.search("q", None, None, limit=0) == []


@pytest.mark.parametrize(
    "reply, reason",
    [
        (rss_reply("<rss><channel><item><title>broken"), "parse error"),
        (rss_reply("not xml at all"), "parse error"),
        (rss_reply('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel/></rss>'), "DTD"),
        (rss_reply("<html>Service unavailable</html>", status=503), "HTTP 503"),
        (httpx.ConnectError("connection refused"), "ConnectError"),
        (httpx.ReadTimeout("timed out"), "ReadTimeout"),
    ],
)
def test_google_news_failures_return_empty(reply: Any, reason: str, caplog: pytest.LogCaptureFixture) -> None:
    provider = GoogleNewsRSS(Server(reply).client(), clock=lambda: NOW)
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        assert provider.search("Arizona Governor", NOW - 3600, NOW) == []
    assert reason in (provider.last_error or "")
    assert any("google-news" in r.getMessage() for r in caplog.records)


def test_google_news_rejects_bodies_over_two_megabytes() -> None:
    big = b"<rss><channel>" + b" " * (news_mod.MAX_RESPONSE_BYTES + 10) + b"</channel></rss>"
    provider = GoogleNewsRSS(Server(httpx.Response(200, content=big)).client(), clock=lambda: NOW)
    assert provider.search("q", None, None) == []
    assert "too large" in (provider.last_error or "")

    def chunks() -> Any:  # no Content-Length: the cap applies while streaming
        for _ in range(3):
            yield b" " * (1024 * 1024)

    provider = GoogleNewsRSS(Server(lambda req: httpx.Response(200, content=chunks())).client(), clock=lambda: NOW)
    assert provider.search("q", None, None) == []
    assert "larger than" in (provider.last_error or "")


def test_rate_limited_provider_cools_down() -> None:
    clock = FakeClock(NOW)
    server = Server(httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "300"}), rss_reply())
    provider = GoogleNewsRSS(server.client(), clock=clock)
    assert provider.search("q", None, None) == []
    assert "429" in (provider.last_error or "")
    clock.now += 299
    assert provider.search("q", None, None) == []  # still resting: no request sent
    assert len(server.requests) == 1
    clock.now += 2
    assert len(provider.search("q", None, None)) == 3
    assert len(server.requests) == 2


def test_default_http_client_identifies_itself() -> None:
    provider = GoogleNewsRSS()
    try:
        assert provider.http.headers["User-Agent"] == f"supermarket-bot/{__version__} (+news lookup)"
        assert provider.http.follow_redirects is True
        assert provider.http.timeout.read == 10
    finally:
        provider.close()
    assert provider.http.is_closed  # the provider owned it


def test_injected_client_is_not_closed_by_provider() -> None:
    client = Server(rss_reply()).client()
    GoogleNewsRSS(client).close()
    assert not client.is_closed
    client.close()


# --------------------------------------------------------------------------- GDELT


def gdelt_reply(payload: Any = GDELT, status: int = 200) -> httpx.Response:
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    return httpx.Response(status, content=body.encode() if isinstance(body, str) else body, headers={"Content-Type": "application/json"})


def test_gdelt_parses_artlist() -> None:
    server = Server(gdelt_reply())
    provider = GDELTDoc(server.client(), clock=lambda: NOW)
    articles = provider.search('"Katie Hobbs" Arizona Governor', ts("2026-10-02T00:00:00Z"), NOW, limit=25)

    assert str(server.requests[0].url).startswith("https://api.gdeltproject.org/api/v2/doc/doc?")
    assert server.params() == {
        "query": '"Katie Hobbs" Arizona Governor sourcelang:english',
        "mode": "artlist",
        "format": "json",
        "maxrecords": "25",
        "sort": "datedesc",
        "startdatetime": "20261002000000",
        "enddatetime": "20261003120000",
    }
    assert [a.title for a in articles] == [
        "Katie Hobbs surges ahead in new Arizona governor poll",
        "Arizona governor race tightens as Hobbs and Lake spar",
        "Hobbs campaign memo",
    ]
    assert articles[0].published_at == ts("2026-10-03T11:15:00Z")
    assert articles[0].source == "azcentral.com"
    assert articles[0].provider == "gdelt"
    assert articles[2].published_at is None
    assert GDELTDoc.min_interval_s == 5.0


def test_gdelt_without_window_omits_dates_and_caps_records() -> None:
    server = Server(gdelt_reply({"articles": []}))
    GDELTDoc(server.client()).search("Arizona", None, None, limit=1000)
    params = server.params()
    assert "startdatetime" not in params and "enddatetime" not in params
    assert params["maxrecords"] == "250"


@pytest.mark.parametrize(
    "reply, expected_error",
    [
        (gdelt_reply("Your search contained a phrase that was too short."), "non-JSON"),
        (gdelt_reply("<html><body><h1>Invalid query</h1></body></html>"), "non-JSON"),
        (gdelt_reply("Please limit requests to one every 5 seconds", status=429), "429"),
        (gdelt_reply("[1, 2]"), "unexpected JSON"),
        (httpx.ConnectError("dns failure"), "ConnectError"),
    ],
)
def test_gdelt_errors_return_empty(reply: httpx.Response, expected_error: str) -> None:
    provider = GDELTDoc(Server(reply).client(), clock=lambda: NOW)
    assert provider.search("Arizona Governor", NOW - 3600, NOW) == []
    assert expected_error in (provider.last_error or "")


def test_gdelt_empty_results() -> None:
    for body in ("", "{}", '{"articles": null}'):
        provider = GDELTDoc(Server(gdelt_reply(body)).client())
        assert provider.search("Arizona", None, None) == []
        assert provider.last_error is None


# --------------------------------------------------------------------------- NewsAPI


def test_newsapi_parses_and_sends_key_as_header() -> None:
    server = Server(httpx.Response(200, json=NEWSAPI))
    provider = NewsAPIOrg("secret-key-123", server.client())
    articles = provider.search('"Katie Hobbs" Arizona', ts("2026-10-02T12:00:00Z"), NOW, limit=10)

    request = server.requests[0]
    assert str(request.url).startswith("https://newsapi.org/v2/everything?")
    assert request.headers["X-Api-Key"] == "secret-key-123"
    assert "secret-key-123" not in str(request.url)
    assert server.params() == {
        "q": '"Katie Hobbs" Arizona',
        "sortBy": "publishedAt",
        "language": "en",
        "pageSize": "10",
        "from": "2026-10-02T12:00:00Z",
        "to": "2026-10-03T12:00:00Z",
    }
    assert [a.title for a in articles] == ["Hobbs gains in Arizona governor race after debate", "Arizona races to watch"]
    assert articles[0].source == "Reuters"
    assert articles[0].published_at == ts("2026-10-03T10:45:00Z")
    assert articles[0].summary == "Democratic Gov. Katie Hobbs widened her lead & raised more money."
    assert articles[0].provider == "newsapi"
    assert "secret" not in repr(provider)


def test_newsapi_error_is_logged_without_the_key(caplog: pytest.LogCaptureFixture) -> None:
    body = {"status": "error", "code": "apiKeyInvalid", "message": "Your API key is invalid or incorrect."}
    provider = NewsAPIOrg("secret-key-123", Server(httpx.Response(401, json=body)).client())
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        assert provider.search("Arizona", None, None) == []
    assert "HTTP 401" in (provider.last_error or "")
    assert all("secret-key-123" not in r.getMessage() for r in caplog.records)

    provider = NewsAPIOrg("k", Server(httpx.Response(200, json={"status": "error", "code": "rateLimited"})).client())
    assert provider.search("Arizona", None, None) == []
    assert "rateLimited" in (provider.last_error or "")


def test_newsapi_requires_key() -> None:
    with pytest.raises(ValueError):
        NewsAPIOrg("")


def test_default_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Server(rss_reply()).client()
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    assert [p.name for p in default_providers(client)] == ["google-news", "gdelt"]
    monkeypatch.setenv("NEWSAPI_KEY", "abc")
    providers = default_providers(client)
    assert [p.name for p in providers] == ["google-news", "gdelt", "newsapi"]
    assert all(p.http is client for p in providers)  # type: ignore[attr-defined]
    client.close()


# --------------------------------------------------------------------------- searcher


class FakeProvider(NewsProvider):
    def __init__(self, name: str, results: Any = (), interval: float = 0.0, error: Optional[str] = None) -> None:
        self.name = name
        self.min_interval_s = interval
        self.results = results
        self.error = error
        self.calls: List[tuple] = []

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        self.calls.append((query, since, until, limit))
        self.last_error = self.error
        if isinstance(self.results, Exception):
            raise self.results
        results = self.results(query) if callable(self.results) else self.results
        return [Article(**a.to_dict()) for a in results]


class FakeStore:
    def __init__(self) -> None:
        self.data: Dict[str, tuple] = {}
        self.reads = 0

    def cached_news(self, key: str, max_age_s: float, now: float) -> Optional[List[Article]]:
        self.reads += 1
        entry = self.data.get(key)
        if entry is None or now - entry[0] > max_age_s:
            return None
        return [Article(**a) for a in entry[1]]

    def put_news(self, key: str, articles: List[Article], now: float) -> None:
        self.data[key] = (now, [a.to_dict() for a in articles])


TITLE = "Who will win the Arizona Governor race?"
OPTION = "Katie Hobbs"


def hobbs_articles() -> List[Article]:
    return [
        Article("Arizona weather turns cooler", "https://weather.example.com/az", "Weather", NOW - 600, provider="g"),
        Article("Katie Hobbs surges in Arizona governor poll", "https://www.azcentral.com/story/hobbs/?utm_source=rss", "azcentral", NOW - 3000, provider="g"),
        Article("Hobbs leads Arizona governor race", "https://news.example.com/hobbs", "News", NOW - 7200, provider="g"),
        Article("Celebrity gossip roundup", "https://gossip.example.com/x", "Gossip", NOW - 60, provider="g"),
    ]


def test_searcher_scores_dedups_and_sorts() -> None:
    google = FakeProvider("google-news", hobbs_articles())
    gdelt = FakeProvider(
        "gdelt",
        [
            Article("Katie Hobbs surges in Arizona governor poll", "https://azcentral.com/story/hobbs", "azcentral.com", None, provider="gdelt"),
            Article("Hobbs leads Arizona Governor race!", "https://other.example.com/copy", "Copycat", NOW - 7000, summary="More detail", provider="gdelt"),
        ],
    )
    searcher = NewsSearcher([google, gdelt], clock=lambda: NOW)
    results = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW, limit=20)

    assert google.calls[0] == ('"Katie Hobbs" Arizona Governor', NOW - 86400, NOW, 20)
    assert gdelt.calls[0][0] == '"Katie Hobbs" Arizona Governor'
    titles = [a.title for a in results]
    # duplicates (same URL modulo www/utm/slash; same title modulo punctuation) collapse
    assert titles == ["Katie Hobbs surges in Arizona governor poll", "Hobbs leads Arizona governor race", "Arizona weather turns cooler"]
    assert results[0].relevance == 1.0
    assert results[1].relevance == pytest.approx(1.0)
    assert results[1].summary == "More detail"  # gap filled from the duplicate
    assert results[0].published_at == NOW - 3000
    assert results[2].relevance == pytest.approx(0.4)
    assert "Celebrity gossip roundup" not in titles  # zero relevance is dropped


def test_searcher_sorts_by_recency_within_equal_relevance() -> None:
    provider = FakeProvider(
        "p",
        [
            Article("Arizona Governor update", "https://a.example/1", published_at=NOW - 5000),
            Article("Arizona Governor debate", "https://a.example/2", published_at=NOW - 100),
            Article("Arizona Governor undated", "https://a.example/3", published_at=None),
        ],
    )
    results = NewsSearcher([provider], clock=lambda: NOW).search_for_market(TITLE, None, None, None)
    assert [a.url[-1] for a in results] == ["2", "1", "3"]


def test_searcher_respects_provider_intervals_without_sleeping() -> None:
    clock = FakeClock(NOW)
    sleeper = Sleeper(clock)
    fast = FakeProvider("google-news", hobbs_articles(), interval=2.0)
    slow = FakeProvider("gdelt", [], interval=5.0)
    searcher = NewsSearcher([fast, slow], clock=clock, sleep=sleeper)

    searcher.search_for_market(TITLE, OPTION, NOW - 3600, NOW)
    assert sleeper.calls == []
    searcher.search_for_market("Will Republicans win the Pennsylvania Senate race?", None, NOW - 3600, NOW)
    assert sleeper.calls == [2.0, 3.0]  # google waits 2 s; gdelt then needs 3 s more to reach 5 s
    clock.now += 60
    searcher.search_for_market("Will turnout exceed 50% in Georgia?", None, NOW - 3600, NOW)
    assert sleeper.calls == [2.0, 3.0]  # enough time passed
    assert len(fast.calls) == len(slow.calls) == 3


def test_searcher_caches_in_store() -> None:
    clock = FakeClock(NOW)
    store = FakeStore()
    provider = FakeProvider("google-news", hobbs_articles())
    searcher = NewsSearcher([provider], store=store, cache_ttl=1200, clock=clock)

    first = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW)
    assert len(store.data) == 1
    key = next(iter(store.data))
    assert '"Katie Hobbs" Arizona Governor' in key and "google-news" in key
    clock.now += 600
    # until moved by 10 minutes but stays in the same hour bucket: cache hit
    second = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW + 600)
    assert len(provider.calls) == 1
    assert [a.title for a in second] == [a.title for a in first]
    assert [a.relevance for a in second] == [a.relevance for a in first]
    clock.now += 1201
    searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW + 600)
    assert len(provider.calls) == 2  # expired


def test_searcher_caches_empty_results_but_not_outages() -> None:
    clock = FakeClock(NOW)
    quiet = FakeProvider("p", [])
    searcher = NewsSearcher([quiet], clock=clock, relax=False)
    assert searcher.search_for_market(TITLE, OPTION, None, None) == []
    assert searcher.search_for_market(TITLE, OPTION, None, None) == []
    assert len(quiet.calls) == 1  # "no news" is a valid answer and is cached

    broken = FakeProvider("p", [], error="HTTP 503")
    searcher = NewsSearcher([broken], clock=clock, relax=False)
    searcher.search_for_market(TITLE, OPTION, None, None)
    searcher.search_for_market(TITLE, OPTION, None, None)
    assert len(broken.calls) == 2  # every provider failed: nothing cached


def test_searcher_memory_cache_returns_copies() -> None:
    provider = FakeProvider("p", hobbs_articles())
    searcher = NewsSearcher([provider], clock=lambda: NOW)
    first = searcher.search_for_market(TITLE, OPTION, None, None)
    first[0].title = "mutated"
    second = searcher.search_for_market(TITLE, OPTION, None, None)
    assert second[0].title != "mutated"
    assert len(provider.calls) == 1


def test_searcher_survives_broken_store_and_provider(caplog: pytest.LogCaptureFixture) -> None:
    class BrokenStore:
        def cached_news(self, *a: Any) -> Any:
            raise NotImplementedError

        def put_news(self, *a: Any) -> None:
            raise RuntimeError("disk full")

    boom = FakeProvider("boom", RuntimeError("provider bug"))
    good = FakeProvider("good", hobbs_articles())
    searcher = NewsSearcher([boom, good], store=BrokenStore(), clock=lambda: NOW)
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        results = searcher.search_for_market(TITLE, OPTION, None, None)
    assert results and results[0].relevance == 1.0
    assert any("boom" in r.getMessage() for r in caplog.records)


def test_searcher_empty_query_makes_no_requests() -> None:
    provider = FakeProvider("p", hobbs_articles())
    searcher = NewsSearcher([provider], clock=lambda: NOW)
    assert searcher.search_for_market("", None, None, None) == []
    assert searcher.search_for_market("2026?", "YES", None, None) == []
    assert searcher.search_for_market(TITLE, OPTION, None, None, limit=0) == []
    assert provider.calls == []


def test_searcher_relaxes_an_over_specified_query() -> None:
    def answer(query: str) -> List[Article]:
        if "runoff" in query:
            return []
        return [Article("Warnock leads Georgia Senate polls", "https://ga.example/warnock", published_at=NOW - 100)]

    provider = FakeProvider("p", answer)
    results = NewsSearcher([provider], clock=lambda: NOW).search_for_market(
        "Who wins the Georgia Senate runoff?", "Raphael Warnock", None, None
    )
    assert [c[0] for c in provider.calls] == [
        '"Raphael Warnock" Georgia Senate runoff',
        '"Raphael Warnock" Georgia Senate',
    ]
    assert len(results) == 1
    # scored against the full keyword set: warnock (2) + georgia (2) + senate (1) of 6
    assert results[0].relevance == pytest.approx(5 / 6, abs=1e-4)


def test_searcher_does_not_relax_during_an_outage() -> None:
    broken = FakeProvider("p", [], error="HTTP 503")
    NewsSearcher([broken], clock=lambda: NOW).search_for_market("Who wins the Georgia Senate runoff?", "Raphael Warnock", None, None)
    assert len(broken.calls) == 1  # a failing provider is not asked twice


def test_searcher_limit() -> None:
    many = [Article(f"Arizona Governor story {i}", f"https://a.example/{i}", published_at=NOW - i) for i in range(30)]
    results = NewsSearcher([FakeProvider("p", many)], clock=lambda: NOW).search_for_market(TITLE, None, None, None, limit=5)
    assert len(results) == 5


def test_url_normalisation() -> None:
    key = news_mod._url_key
    assert key("https://www.Example.com/story/?utm_source=rss&id=7#top") == key("http://example.com/story?id=7")
    assert key("https://m.example.com/a/amp") == key("https://example.com/a")
    assert key("https://example.com/a?id=1") != key("https://example.com/a?id=2")


# --------------------------------------------------------------------------- end to end through HTTP


def test_searcher_with_real_providers_over_mock_transport() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "news.google.com":
            return rss_reply()
        if request.url.host == "api.gdeltproject.org":
            return gdelt_reply()
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(route))
    clock = FakeClock(NOW)
    sleeper = Sleeper(clock)
    searcher = NewsSearcher([GoogleNewsRSS(http, clock=clock), GDELTDoc(http, clock=clock)], clock=clock, sleep=sleeper)
    results = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW)
    titles = [a.title for a in results]
    assert titles[0] == "Katie Hobbs surges ahead in new Arizona governor poll"
    assert titles.count("Katie Hobbs surges ahead in new Arizona governor poll") == 1  # Google + GDELT merged
    assert results[0].relevance == 1.0
    assert "Arizona governor race tightens as Hobbs and Lake spar" in titles
    http.close()


def test_searcher_with_tracker_store_cache() -> None:
    store_mod = pytest.importorskip("supermarket_bot.store")
    try:
        store = store_mod.TrackerStore(":memory:")
    except NotImplementedError:
        pytest.skip("store.py not implemented yet")
    clock = FakeClock(NOW)
    provider = FakeProvider("google-news", hobbs_articles())
    searcher = NewsSearcher([provider], store=store, clock=clock)
    first = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW)
    second = searcher.search_for_market(TITLE, OPTION, NOW - 86400, NOW)
    assert len(provider.calls) == 1
    assert [(a.title, a.url, a.published_at, a.relevance) for a in second] == [
        (a.title, a.url, a.published_at, a.relevance) for a in first
    ]
    store.close()


def test_demo_news_provider_plugs_into_searcher() -> None:
    demo = pytest.importorskip("supermarket_bot.demo")
    try:
        market = demo.DemoMarket(seed=7, now=NOW, clock=lambda: NOW)
        provider = demo.DemoNewsProvider(market)
    except NotImplementedError:
        pytest.skip("demo.py not implemented yet")
    assert isinstance(provider, NewsProvider)
    searcher = NewsSearcher([provider], clock=lambda: NOW)
    results = searcher.search_for_market("Will Republicans win the Pennsylvania Senate race?", "YES", NOW - 86400, NOW)
    assert all(isinstance(a, Article) and 0 < a.relevance <= 1 for a in results)
