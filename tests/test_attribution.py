"""Tests for supermarket_bot/attribution.py: the heuristic, the Claude judge and the Attributor.

Offline: the Super Market API is the FakeAPI from conftest, Claude is a fake client object
that records the ``beta.messages.create`` kwargs, and news comes from fake searchers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import pytest

from conftest import TOURNAMENT_ID, error_body, market

from supermarket_bot import attribution as attr_mod
from supermarket_bot.attribution import (
    DEFAULT_LLM_MODEL,
    SCHEMA,
    SYSTEM_PROMPT,
    Attributor,
    LLMJudge,
    attribute,
    build_judge_prompt,
    crowd_signals,
    merge,
    news_score,
    timing_factor,
)
from supermarket_bot.bot import Context
from supermarket_bot.errors import ApiError
from supermarket_bot.models import Article, Attribution, ExchangeInfo, Surge, TradeFlow, TradeRecord


def ts(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


T0 = ts("2026-10-03T12:00:00Z")  # end of the surge window
START = T0 - 3600
NOW = T0 + 120
CTX = Context(TOURNAMENT_ID, "predictions-cup", "SIG Predictions Cup")


def make_surge(**kw: Any) -> Surge:
    base: Dict[str, Any] = dict(
        exchange_id="ex1",
        market_id="m1",
        window="1h",
        window_s=3600.0,
        start_ts=START,
        end_ts=T0,
        start_price=0.50,
        end_price=0.64,
        change=0.14,
        direction="up",
        peak_price=0.65,
        detected_at=T0,
        zscore=4.2,
    )
    base.update(kw)
    return Surge(**base)


def make_flow(**kw: Any) -> TradeFlow:
    base: Dict[str, Any] = dict(
        n_trades=20, total_size=5000.0, max_trade_size=600.0, top_trade_share=0.12, hhi=0.08, yes_share=0.6, vwap=0.58
    )
    base.update(kw)
    return TradeFlow(**base)


def make_article(title: str = "Katie Hobbs surges in Arizona governor poll", rel: float = 0.9, published: Optional[float] = START - 600, **kw: Any) -> Article:
    return Article(title=title, url=kw.pop("url", "https://news.example/" + title[:12].replace(" ", "-")), source=kw.pop("source", "AP"), published_at=published, relevance=rel, **kw)


# --------------------------------------------------------------------------- timing and news score


@pytest.mark.parametrize(
    "published, expected",
    [
        (None, 0.4),
        (START - 6 * 3600, 1.0),  # boundary: start - 6h is inside the full window
        (START - 6 * 3600 - 1, 0.6),
        (START - 24 * 3600, 0.6),
        (START - 24 * 3600 - 1, 0.0),  # older than a day: stale
        (START, 1.0),
        (T0, 1.0),
        (T0 + 1800, 1.0),  # end + 30m still counts as the cause
        (T0 + 1801, 0.3),  # later: coverage, not cause
    ],
)
def test_timing_factor(published: Optional[float], expected: float) -> None:
    assert timing_factor(published, make_surge()) == expected


def test_news_score_is_max_relevance_times_timing() -> None:
    surge = make_surge()
    articles = [
        make_article(rel=0.9, published=T0 + 7200),  # 0.27
        make_article(rel=0.5, published=START - 600),  # 0.5
        make_article(rel=0.8, published=None),  # 0.32
    ]
    assert news_score(articles, surge) == pytest.approx(0.5)
    assert news_score([], surge) == 0.0
    assert news_score([make_article(rel=1.7, published=START)], surge) == 1.0  # relevance clamped


# --------------------------------------------------------------------------- crowd signals


def test_no_crowd_signals_for_broad_flow_and_deep_book() -> None:
    assert crowd_signals(make_surge(), make_flow(), 2500.0) == []
    assert crowd_signals(make_surge(), None, None) == []


@pytest.mark.parametrize(
    "flow_kw, depth, change, needle",
    [
        ({"n_trades": 5}, 2500.0, 0.14, "Few trades: 5"),
        ({"top_trade_share": 0.5}, 2500.0, 0.14, "Concentrated flow: the largest trade was 50%"),
        ({"hhi": 0.35}, 2500.0, 0.14, "HHI 0.35"),
        ({"yes_share": 0.85}, 2500.0, 0.14, "One-sided flow: 85% of traded size was on YES"),
        ({"yes_share": 0.10}, 2500.0, 0.14, "One-sided flow: 90% of traded size was on NO"),
        ({"total_size": 999.0}, 2500.0, 0.10, "Big move on small volume: 0.10 on 999 shares"),
        ({}, 499.0, 0.14, "Thin book: 499 shares"),
    ],
)
def test_each_crowd_signal(flow_kw: Dict[str, Any], depth: float, change: float, needle: str) -> None:
    signals = crowd_signals(make_surge(change=change), make_flow(**flow_kw), depth)
    assert len(signals) == 1, signals
    assert needle in signals[0]


def test_signal_thresholds_are_strict_where_designed() -> None:
    surge = make_surge()
    assert crowd_signals(surge, make_flow(n_trades=6, top_trade_share=0.49, hhi=0.34, yes_share=0.84), 500.0) == []
    assert crowd_signals(make_surge(change=0.099), make_flow(total_size=10.0), None) == []
    assert crowd_signals(make_surge(change=-0.12), make_flow(total_size=10.0), None)  # |change| counts


def test_no_trades_counts_as_two_signals() -> None:
    signals = crowd_signals(make_surge(), TradeFlow(), 5000.0)
    # few trades (0 <= 5), big move on small volume (0 shares), and "no trades" counted twice
    assert len(signals) == 4
    assert any("No trades at all" in s for s in signals)
    assert any("second signal" in s for s in signals)


# --------------------------------------------------------------------------- heuristic verdicts


def test_attribute_news_verdict() -> None:
    surge = make_surge()
    articles = [
        make_article("Unrelated story", rel=0.1, published=START),
        make_article("Katie Hobbs surges in Arizona governor poll", rel=0.9, published=START - 600, source="AP"),
        make_article("Old background piece", rel=0.9, published=START - 3 * 86400),  # stale: dropped
    ]
    flow = make_flow(n_trades=4)  # one crowd signal
    result = attribute(surge, flow, articles, 2500.0, "Who will win the Arizona Governor race?", "Katie Hobbs", NOW)
    assert result.verdict == "news"
    assert result.confidence == pytest.approx(min(0.95, 0.5 + 0.9 / 2 - 0.05 * 1))
    assert result.reversion_odds == 0.2
    assert result.method == "heuristic"
    assert result.analyzed_at == NOW
    assert result.flow is flow and result.book_depth == 2500.0
    assert [a.title for a in result.articles] == ["Katie Hobbs surges in Arizona governor poll", "Unrelated story"]
    assert "Katie Hobbs surges" in result.summary and "(AP)" in result.summary and "10 min before" in result.summary
    text = "\n".join(result.reasons)
    assert "YES rose 0.140 (0.500 → 0.640) over the 1h window, z +4.2" in text
    assert "relevance 0.90 x timing 1.0 = news score 0.90" in text
    assert "Few trades: 4" in text
    assert "Book depth: 2,500 shares" in text


def test_attribute_participants_verdict() -> None:
    surge = make_surge(change=0.18, end_price=0.68)
    flow = make_flow(n_trades=2, total_size=900.0, max_trade_size=700.0, top_trade_share=0.78, hhi=0.65, yes_share=1.0)
    result = attribute(surge, flow, [], 250.0, "Ohio House District 9", "YES", NOW)
    # few + concentrated + one-sided + big move on small volume + thin book = 5
    assert result.verdict == "participants"
    assert result.confidence == pytest.approx(min(0.9, 0.4 + 0.12 * 5))
    assert result.reversion_odds == pytest.approx(min(0.8, 0.5 + 0.08 * 5))
    text = "\n".join(result.reasons)
    assert "No news articles matched" in text
    assert "no trader identity" in text
    assert "Thin book: 250 shares" in text
    assert "Participant-driven: +0.18 on 2 trade(s) with 5 crowd signal(s)" in result.summary


def test_attribute_participants_formula_with_two_signals() -> None:
    flow = make_flow(n_trades=3, top_trade_share=0.6)
    result = attribute(make_surge(), flow, [make_article(rel=0.2, published=START)], 5000.0, "t", None, NOW)
    assert result.verdict == "participants"
    assert result.confidence == pytest.approx(0.4 + 0.24)
    assert result.reversion_odds == pytest.approx(0.5 + 0.16)
    assert any("weak or off-topic" in r for r in result.reasons)


@pytest.mark.parametrize(
    "articles, flow, depth",
    [
        ([make_article(rel=0.4, published=START)], make_flow(n_trades=2, top_trade_share=0.9), 100.0),  # 0.3 <= news < 0.55
        ([], make_flow(n_trades=3), 5000.0),  # no news but only one crowd signal
        ([], None, None),  # nothing known at all
    ],
)
def test_attribute_unclear(articles: List[Article], flow: Optional[TradeFlow], depth: Optional[float]) -> None:
    result = attribute(make_surge(), flow, articles, depth, "t", None, NOW)
    assert result.verdict == "unclear"
    assert result.confidence == 0.35 and result.reversion_odds == 0.4
    assert result.summary.startswith("Unclear")


def test_attribute_reports_missing_evidence() -> None:
    result = attribute(make_surge(zscore=None), None, [], None, "t", None, NOW)
    text = "\n".join(result.reasons)
    assert "Trade tape unavailable" in text
    assert "Order book unavailable" in text
    assert ", z " not in result.reasons[0]


def test_news_confidence_drops_with_crowd_signals() -> None:
    flow = make_flow(n_trades=1, top_trade_share=1.0, hhi=1.0, yes_share=1.0, total_size=50.0)
    result = attribute(make_surge(), flow, [make_article(rel=0.6, published=START)], 10.0, "t", None, NOW)
    assert result.verdict == "news"  # news beats crowd signals
    assert result.confidence == pytest.approx(0.5 + 0.3 - 0.05 * 5)


# --------------------------------------------------------------------------- merge


def heuristic_result(**kw: Any) -> Attribution:
    base: Dict[str, Any] = dict(
        verdict="participants",
        confidence=0.76,
        reversion_odds=0.74,
        summary="Participant-driven: ...",
        reasons=["r1", "r2"],
        articles=[make_article("A", rel=0.2), make_article("B", rel=0.25), make_article("C", rel=0.1)],
    )
    base.update(kw)
    return Attribution(**base)


def test_merge_takes_llm_verdict_and_averages() -> None:
    h = heuristic_result()
    llm = {"verdict": "news", "confidence": 0.6, "reversion_odds": 0.3, "explanation": "Article B broke first.", "key_article_indexes": [1]}
    merged = merge(h, llm)
    assert merged.verdict == "news"
    assert merged.confidence == pytest.approx((0.76 + 0.6) / 2)
    assert merged.reversion_odds == pytest.approx((0.74 + 0.3) / 2)
    assert merged.reasons == ["Claude: Article B broke first.", "r1", "r2"]
    assert merged.method == "heuristic+llm"
    assert merged.llm == llm
    assert [a.title for a in merged.articles] == ["B", "A", "C"]  # key article first
    assert "Claude" in merged.summary and "heuristic said participants" in merged.summary
    assert h.method == "heuristic" and h.verdict == "participants"  # input untouched


def test_merge_same_verdict_keeps_summary() -> None:
    h = heuristic_result()
    merged = merge(h, {"verdict": "participants", "confidence": 0.9, "reversion_odds": 0.9, "explanation": "", "key_article_indexes": []})
    assert merged.summary == h.summary
    assert merged.reasons == h.reasons


def test_merge_without_llm_returns_heuristic() -> None:
    h = heuristic_result()
    assert merge(h, None) is h
    assert merge(h, {}) is h
    assert merge(h, {"verdict": "bogus", "confidence": 1, "reversion_odds": 1}) is h
    assert merge(h, {"verdict": "news", "confidence": "x", "reversion_odds": 1}) is h


def test_merge_uses_given_article_list_for_indexes() -> None:
    h = heuristic_result()
    seen = [make_article("X"), make_article("Y")]
    merged = merge(h, {"verdict": "news", "confidence": 0.5, "reversion_odds": 0.5, "explanation": "e", "key_article_indexes": [1, 7]}, seen)
    assert [a.title for a in merged.articles] == ["Y", "X"]


# --------------------------------------------------------------------------- LLM judge


class FakeMessages:
    def __init__(self, response: Any = None, error: Optional[BaseException] = None) -> None:
        self.response = response
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAnthropicClient:
    def __init__(self, response: Any = None, error: Optional[BaseException] = None) -> None:
        self.messages_api = FakeMessages(response, error)
        self.beta = SimpleNamespace(messages=self.messages_api)

    @property
    def calls(self) -> List[Dict[str, Any]]:
        return self.messages_api.calls


def reply(payload: Any, stop_reason: str = "end_turn", blocks: Optional[List[Any]] = None) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, content=content, model="claude-opus-5-5")


GOOD = {"verdict": "participants", "confidence": 0.8, "reversion_odds": 0.7, "explanation": "Two big YES trades into a thin book; no news.", "key_article_indexes": []}


class FakeAPIError(Exception):
    pass


class FakeConnectionError(FakeAPIError):
    pass


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stand-in for the ``anthropic`` package so tests do not depend on it being installed."""
    created: List[Any] = []

    def factory(**kwargs: Any) -> Any:
        client = FakeAnthropicClient(reply(GOOD))
        client.kwargs = kwargs  # type: ignore[attr-defined]
        created.append(client)
        return client

    sdk = SimpleNamespace(APIError=FakeAPIError, APIConnectionError=FakeConnectionError, Anthropic=factory, created=created)
    monkeypatch.setattr(attr_mod, "_import_anthropic", lambda: sdk)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SUPERMARKET_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return sdk


def judge_args(n_articles: int = 2) -> Dict[str, Any]:
    surge = make_surge(status="open", current_price=0.63, reverted_fraction=0.07)
    articles = [make_article(f"Headline {i}\nwith newline", rel=0.5 - i * 0.1, published=START - 600 * (i + 1)) for i in range(n_articles)]
    flow = make_flow(n_trades=2, total_size=900.0, price_impact=0.0156)
    heuristic = attribute(surge, flow, articles, 250.0, "Who will win the Arizona Governor race?", "Katie Hobbs", NOW)
    return dict(surge=surge, flow=flow, articles=heuristic.articles, book_depth=250.0,
                market_title="Who will win the Arizona Governor race?", option="Katie Hobbs", heuristic=heuristic)


def test_available_requires_sdk_and_credentials(fake_sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    assert LLMJudge().available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert LLMJudge().available() is True
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    assert LLMJudge().available() is True
    assert fake_sdk.created == []  # available() never builds a client or touches the network
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    assert LLMJudge(client=FakeAnthropicClient()).available() is True


def test_available_false_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attr_mod, "_import_anthropic", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    judge = LLMJudge(client=FakeAnthropicClient(reply(GOOD)))
    assert judge.available() is False
    assert judge.judge(**judge_args()) is None
    assert judge._client.calls == []


def test_model_selection(fake_sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    assert LLMJudge().model == DEFAULT_LLM_MODEL == "claude-opus-5-5"
    monkeypatch.setenv("SUPERMARKET_LLM_MODEL", "claude-sonnet-5-5")
    assert LLMJudge().model == "claude-sonnet-5-5"
    assert LLMJudge(model="claude-haiku-4-5").model == "claude-haiku-4-5"


def test_judge_request_shape(fake_sdk: SimpleNamespace) -> None:
    client = FakeAnthropicClient(reply(GOOD))
    result = LLMJudge(client=client).judge(**judge_args())
    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert set(kwargs) == {"model", "max_tokens", "betas", "fallbacks", "output_config", "system", "messages"}
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    for banned in ("thinking", "temperature", "top_p", "top_k"):
        assert banned not in kwargs
    assert kwargs["system"] == SYSTEM_PROMPT
    assert "only from the evidence" in SYSTEM_PROMPT and "no trader IDs" in SYSTEM_PROMPT and "JSON" in SYSTEM_PROMPT
    assert "untrusted" in SYSTEM_PROMPT
    (message,) = kwargs["messages"]
    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, str)
    for needle in (
        "Market: Who will win the Arizona Governor race?",
        "Outcome: Katie Hobbs",
        "up +0.140 from 0.500 to 0.640, peak 0.650 in the 1h window; z-score +4.20",
        "current price 0.630",
        "Trade flow in the window: 2 trades, 900 shares total",
        "price impact 0.0156",
        "Book depth within 0.05 of the mid: 250 shares",
        "[0] Headline 0 with newline | AP | 2026-10-03T10:50:00Z | published 10 min before the move started | 0.50",
        "[1] Headline 1 with newline",
        "Heuristic verdict: unclear (confidence 0.35, reversion odds 0.40)",
        "- Mixed evidence (news score 0.50, 3 crowd signal(s))",
    ):
        assert needle in content, needle
    assert result == {**GOOD, "model": "claude-opus-5-5"}


def test_schema_is_strict_and_constraint_free() -> None:
    assert SCHEMA["additionalProperties"] is False
    assert SCHEMA["required"] == ["verdict", "confidence", "reversion_odds", "explanation", "key_article_indexes"]
    assert set(SCHEMA["properties"]) == set(SCHEMA["required"])
    assert SCHEMA["properties"]["verdict"]["enum"] == ["news", "participants", "unclear"]
    dumped = json.dumps(SCHEMA)
    for unsupported in ("minimum", "maximum", "minLength", "maxLength", "multipleOf"):
        assert unsupported not in dumped


def test_judge_builds_client_lazily(fake_sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    judge = LLMJudge()
    assert fake_sdk.created == []
    assert judge.judge(**judge_args())["verdict"] == "participants"
    assert judge.judge(**judge_args())["verdict"] == "participants"
    assert len(fake_sdk.created) == 1  # built once, reused
    assert fake_sdk.created[0].kwargs == {"timeout": 120.0, "max_retries": 2}  # credentials come from the env
    assert len(fake_sdk.created[0].calls) == 2


def test_judge_validates_and_clamps(fake_sdk: SimpleNamespace) -> None:
    payload = {"verdict": "NEWS", "confidence": 1.7, "reversion_odds": -0.2, "explanation": "  e  ", "key_article_indexes": [0, 5, -1, 1, 1, True, 2.0, "1"]}
    result = LLMJudge(client=FakeAnthropicClient(reply(payload))).judge(**judge_args(n_articles=3))
    assert result is not None
    assert result["verdict"] == "news"
    assert result["confidence"] == 1.0 and result["reversion_odds"] == 0.0
    assert result["explanation"] == "e"
    assert result["key_article_indexes"] == [0, 1, 2]


@pytest.mark.parametrize(
    "response",
    [
        reply(GOOD, stop_reason="refusal"),
        reply("not json at all"),
        reply('{"verdict": "participants", "confid'),  # truncated
        reply({**GOOD, "verdict": "maybe"}),
        reply({**GOOD, "confidence": "high"}),
        reply({**GOOD, "reversion_odds": None}),
        reply(["participants"]),
        reply("", blocks=[SimpleNamespace(type="thinking", thinking="...")]),
        reply("", blocks=[]),
        SimpleNamespace(stop_reason="end_turn", content=None),
    ],
)
def test_judge_returns_none_for_unusable_output(fake_sdk: SimpleNamespace, response: Any, caplog: pytest.LogCaptureFixture) -> None:
    judge = LLMJudge(client=FakeAnthropicClient(response))
    with caplog.at_level(logging.INFO, logger="supermarket_bot"):
        assert judge.judge(**judge_args()) is None
    assert judge.last_error
    assert any("keeping the heuristic" in r.getMessage() for r in caplog.records)


def test_refusal_does_not_read_content(fake_sdk: SimpleNamespace) -> None:
    class Exploding:
        stop_reason = "refusal"

        @property
        def content(self) -> Any:
            raise AssertionError("content must not be read after a refusal")

    assert LLMJudge(client=FakeAnthropicClient(Exploding())).judge(**judge_args()) is None


@pytest.mark.parametrize("error", [FakeAPIError("overloaded"), FakeConnectionError("dns"), RuntimeError("bug"), TypeError("bad kwarg")])
def test_judge_swallows_errors(fake_sdk: SimpleNamespace, error: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    judge = LLMJudge(client=FakeAnthropicClient(error=error))
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        assert judge.judge(**judge_args()) is None
    assert type(error).__name__ in (judge.last_error or "")
    assert caplog.records


def test_judge_unavailable_makes_no_call(fake_sdk: SimpleNamespace) -> None:
    judge = LLMJudge()  # no credentials, no client
    assert judge.judge(**judge_args()) is None
    assert fake_sdk.created == []


def test_judge_with_real_sdk_package(monkeypatch: pytest.MonkeyPatch) -> None:
    anthropic = pytest.importorskip("anthropic")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert LLMJudge().available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    assert LLMJudge().available() is True
    # real SDK error classes are caught (no network: the fake client raises one)
    err = anthropic.APIError.__new__(anthropic.APIError)
    Exception.__init__(err, "simulated")
    judge = LLMJudge(client=FakeAnthropicClient(error=err))
    assert judge.judge(**judge_args()) is None
    assert "APIError" in (judge.last_error or "")


def test_build_judge_prompt_without_evidence() -> None:
    surge = make_surge(zscore=None)
    heuristic = attribute(surge, None, [], None, "", None, NOW)
    text = build_judge_prompt(surge, None, [], None, "", None, heuristic)
    assert "Market: (unknown title)" in text
    assert "Outcome: YES" in text
    assert "z-score n/a" in text
    assert "Trade flow in the window: unavailable" in text
    assert "Book depth within 0.05 of the mid: unavailable" in text
    assert "(none found)" in text


# --------------------------------------------------------------------------- Attributor


class FakeStore:
    def __init__(self, info: Optional[ExchangeInfo] = None) -> None:
        self.info = info
        self.added: List[tuple] = []

    def exchange(self, exchange_id: str) -> Optional[ExchangeInfo]:
        return self.info if self.info and self.info.exchange_id == exchange_id else None

    def add_trades(self, exchange_id: str, trades: Sequence[Dict[str, Any]]) -> int:
        self.added.append((exchange_id, list(trades)))
        return len(trades)


class FakeNews:
    def __init__(self, articles: Sequence[Article] = (), error: Optional[BaseException] = None) -> None:
        self.articles = list(articles)
        self.error = error
        self.calls: List[tuple] = []

    def search_for_market(self, title: str, option: Optional[str], since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        self.calls.append((title, option, since, until, limit))
        if self.error:
            raise self.error
        return [Article(**a.to_dict()) for a in self.articles]


class CountingLimiter:
    def __init__(self) -> None:
        self.count = 0

    def acquire(self) -> float:
        self.count += 1
        return 0.0


class FakeJudge:
    def __init__(self, result: Optional[Dict[str, Any]], usable: bool = True) -> None:
        self.result = result
        self.usable = usable
        self.calls: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return self.usable

    def judge(self, surge: Surge, flow: Any, articles: Sequence[Article], book_depth: Any, market_title: str, option: Any, heuristic: Attribution) -> Any:
        self.calls.append(dict(surge=surge, flow=flow, articles=list(articles), book_depth=book_depth, title=market_title, option=option, heuristic=heuristic))
        return self.result


def simple_flow(records: Sequence[TradeRecord]) -> TradeFlow:
    """A tiny stand-in for analytics.trade_flow so these tests do not depend on it."""
    sizes = [r.size for r in records]
    total = sum(sizes)
    if not records:
        return TradeFlow()
    yes = sum(r.size for r in records if r.side == "YES")
    return TradeFlow(
        n_trades=len(records),
        total_size=total,
        max_trade_size=max(sizes),
        top_trade_share=max(sizes) / total,
        hhi=sum((s / total) ** 2 for s in sizes),
        yes_share=yes / total,
    )


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trade(tid: str, t: float, size: int, price: float = 0.6, side: str = "YES") -> Dict[str, Any]:
    return {"id": tid, "createdAt": iso(t), "price": price, "size": size, "side": side, "volume": size * price}


def trades_page(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "exchangeId": "ex1",
        "marketId": "m1",
        "from": iso(START - 3600),
        "to": iso(NOW),
        "data": items,
        "pagination": {"limit": 200, "hasMore": False, "nextCursor": None},
        "coverage": {"complete": True, "projectedThroughSequence": 184233},
    }


def orderbook(bids: List[tuple], asks: List[tuple]) -> Dict[str, Any]:
    return {
        "exchangeId": "ex1",
        "marketId": "m1",
        "asOf": {"sequence": 4817, "at": "2026-10-03T12:01:00.000+00:00"},
        "depth": 20,
        "bids": [{"price": p, "quantity": q} for p, q in bids],
        "asks": [{"price": p, "quantity": q} for p, q in asks],
        "bestBid": bids[0][0] if bids else None,
        "bestAsk": asks[0][0] if asks else None,
        "spread": round(asks[0][0] - bids[0][0], 6) if bids and asks else None,
    }


SPIKE_TRADES = [
    trade("t3", T0 - 600, 500, 0.66),  # newest first, as the tape returns them
    trade("t2", T0 - 1800, 400, 0.60),
    trade("t1", START - 1200, 50, 0.50, side="NO"),  # before the window: stored, not in the flow
]
THIN_BOOK = orderbook([(0.62, 100), (0.58, 200), (0.50, 5000)], [(0.66, 150), (0.70, 300)])  # 250 within 0.05 of 0.64
INFO = ExchangeInfo("ex1", "m1", option="YES", market_title="Will Democrats win Ohio House District 9?")


def route_spike(fake: Any, trades: Any = None, book: Any = None) -> None:
    fake.add("GET", "/exchanges/ex1/trades", trades if trades is not None else trades_page(SPIKE_TRADES))
    fake.add("GET", "/exchanges/ex1/orderbook", book if book is not None else THIN_BOOK)


def test_analyze_participant_spike(fake: Any, client: Any) -> None:
    route_spike(fake)
    store, news, limiter = FakeStore(INFO), FakeNews([]), CountingLimiter()
    seen: List[List[TradeRecord]] = []

    def flow_fn(records: Sequence[TradeRecord]) -> TradeFlow:
        seen.append(list(records))
        return simple_flow(records)

    attributor = Attributor(client, CTX, store, news, limiter=limiter, clock=lambda: NOW, flow_fn=flow_fn)
    surge = make_surge(change=0.18, end_price=0.68, current_price=0.64)
    result = attributor.analyze(surge)

    (trades_call,) = fake.calls_to("/exchanges/ex1/trades")
    assert trades_call.params == {"tournamentId": TOURNAMENT_ID, "from": "2026-10-03T10:00:00Z", "limit": "200"}
    (book_call,) = fake.calls_to("/exchanges/ex1/orderbook")
    assert book_call.params == {"depth": "20", "tournamentId": TOURNAMENT_ID}
    assert limiter.count == 2
    assert store.added == [("ex1", SPIKE_TRADES)]  # the whole tape is stored
    assert [r.trade_id for r in seen[0]] == ["t2", "t3"]  # only the window, in time order
    assert news.calls == [("Will Democrats win Ohio House District 9?", "YES", START - 86400, NOW, 20)]

    assert result.verdict == "participants"
    assert result.book_depth == 250.0
    assert result.flow is not None and result.flow.n_trades == 2 and result.flow.total_size == 900
    assert result.analyzed_at == NOW
    assert result.method == "heuristic"
    # few + concentrated + one-sided + big move on small volume + thin book
    assert result.confidence == pytest.approx(0.9)
    assert result.reversion_odds == pytest.approx(0.8)
    assert any("no trader identity" in r for r in result.reasons)


def test_analyze_news_surge_with_judge(fake: Any, client: Any) -> None:
    two_sided = [trade(f"t{i}", T0 - 60 * i, 300, side="YES" if i % 2 else "NO") for i in range(1, 30)]
    route_spike(fake, trades=trades_page(two_sided),
                book=orderbook([(0.63, 2000)], [(0.65, 2000)]))
    headline = Article("Democrats surge in Ohio House District 9 poll", "https://news.example/oh9", "AP", START - 300, relevance=0.95)
    judge = FakeJudge({"verdict": "news", "confidence": 0.9, "reversion_odds": 0.1, "explanation": "AP poll story broke 5 min before.", "key_article_indexes": [0]})
    attributor = Attributor(client, CTX, FakeStore(INFO), FakeNews([headline]), judge=judge, clock=lambda: NOW, flow_fn=simple_flow)
    result = attributor.analyze(make_surge())

    assert judge.calls and judge.calls[0]["title"] == INFO.market_title
    assert judge.calls[0]["book_depth"] == 4000.0
    assert judge.calls[0]["heuristic"].verdict == "news"
    assert [a.title for a in judge.calls[0]["articles"]] == [headline.title]
    assert result.verdict == "news"
    assert result.method == "heuristic+llm"
    assert result.reasons[0] == "Claude: AP poll story broke 5 min before."
    assert judge.calls[0]["heuristic"].reasons[2].startswith("Tape: 29 trade(s)")  # no crowd signals
    heuristic_conf = min(0.95, 0.5 + 0.95 / 2)
    assert result.confidence == pytest.approx((heuristic_conf + 0.9) / 2)
    assert result.reversion_odds == pytest.approx((0.2 + 0.1) / 2)


def test_analyze_skips_unavailable_judge(fake: Any, client: Any) -> None:
    route_spike(fake)
    judge = FakeJudge({"verdict": "news", "confidence": 1, "reversion_odds": 0, "explanation": "x", "key_article_indexes": []}, usable=False)
    result = Attributor(client, CTX, FakeStore(INFO), FakeNews(), judge=judge, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert judge.calls == []
    assert result.method == "heuristic"


def test_analyze_judge_failure_keeps_heuristic(fake: Any, client: Any) -> None:
    route_spike(fake)
    result = Attributor(client, CTX, FakeStore(INFO), FakeNews(), judge=FakeJudge(None), clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert result.method == "heuristic"
    assert result.verdict == "participants"


def test_analyze_degrades_on_api_errors(fake: Any, client: Any, caplog: pytest.LogCaptureFixture) -> None:
    fake.add("GET", "/exchanges/ex1/trades", (404, error_body("NOT_FOUND", "Exchange not found")))
    fake.add("GET", "/exchanges/ex1/orderbook", (400, error_body("VALIDATION_ERROR", "bad depth")))
    attributor = Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: NOW, flow_fn=simple_flow)
    with caplog.at_level(logging.WARNING, logger="supermarket_bot"):
        result = attributor.analyze(make_surge())
    assert result.flow is None and result.book_depth is None
    assert result.verdict == "unclear"
    text = "\n".join(result.reasons)
    assert "Trade tape unavailable" in text and "Order book unavailable" in text
    assert "Trade tape read failed (HTTP 404 NOT_FOUND: Exchange not found" in text
    assert "Order book read failed (HTTP 400 VALIDATION_ERROR" in text


def test_analyze_degrades_on_network_errors(fake: Any, make_client: Any) -> None:
    import httpx

    client = make_client(max_retries=0)
    fake.add("GET", "/exchanges/ex1/trades", httpx.ConnectError("connection refused"))
    fake.add("GET", "/exchanges/ex1/orderbook", THIN_BOOK)
    result = Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert result.flow is None
    assert result.book_depth == 250.0
    assert any("NetworkError" in r for r in result.reasons)


def test_analyze_raises_fatal_auth_errors(fake: Any, client: Any) -> None:
    fake.add("GET", "/exchanges/ex1/trades", (401, error_body("INVALID_API_KEY", "bad key")))
    attributor = Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: NOW, flow_fn=simple_flow)
    with pytest.raises(ApiError) as info:
        attributor.analyze(make_surge())
    assert info.value.code == "INVALID_API_KEY"


def test_analyze_flow_failure_and_news_failure(fake: Any, client: Any) -> None:
    route_spike(fake)

    def broken_flow(records: Sequence[TradeRecord]) -> TradeFlow:
        raise NotImplementedError

    news = FakeNews(error=RuntimeError("searcher bug"))
    result = Attributor(client, CTX, FakeStore(INFO), news, clock=lambda: NOW, flow_fn=broken_flow).analyze(make_surge())
    assert result.flow is None
    assert result.book_depth == 250.0
    text = "\n".join(result.reasons)
    assert "Trade-flow analysis failed (NotImplementedError)" in text
    assert "News search failed (RuntimeError)" in text


def test_analyze_without_news_searcher(fake: Any, client: Any) -> None:
    route_spike(fake)
    result = Attributor(client, CTX, FakeStore(INFO), None, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert any("News search is disabled" in r for r in result.reasons)


def test_analyze_falls_back_to_market_read_for_metadata(fake: Any, client: Any) -> None:
    route_spike(fake)
    fake.add("GET", "/markets/m1", market("m1", "Who will win the Arizona Governor race?", [("ex0", "Kari Lake", 0.4), ("ex1", "Katie Hobbs", 0.6)]))
    news, limiter = FakeNews(), CountingLimiter()
    Attributor(client, CTX, FakeStore(None), news, limiter=limiter, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    (call,) = fake.calls_to("/markets/m1")
    assert call.params == {"tournamentId": TOURNAMENT_ID}
    assert news.calls[0][:2] == ("Who will win the Arizona Governor race?", "Katie Hobbs")
    assert limiter.count == 3


def test_analyze_empty_book_and_empty_tape(fake: Any, client: Any) -> None:
    route_spike(fake, trades=trades_page([]), book=orderbook([], []))
    result = Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert result.book_depth == 0.0
    assert result.flow is not None and result.flow.n_trades == 0
    assert result.verdict == "participants"
    assert any("No trades at all" in r for r in result.reasons)


def test_r2_functional_1_depth_per_side_and_the_book_is_kept(fake: Any, client: Any) -> None:
    """A fade buys one side of the book: the analysis keeps the depth of each side (not both
    together) and stores the levels so the strategy can size the trade by what it can fill."""
    from supermarket_bot.store import TrackerStore

    route_spike(fake)
    store = TrackerStore()
    store.upsert_markets([{"id": "m1", "title": INFO.market_title, "exchanges": [{"id": "ex1", "option": "YES"}]}])
    result = Attributor(client, CTX, store, FakeNews([]), clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    # THIN_BOOK around the 0.64 mid: bids 0.62 (100) and asks 0.66 (150) lie within 5 cents
    assert (result.book_depth, result.bid_depth, result.ask_depth) == (250.0, 100.0, 150.0)
    book = store.book("ex1")
    assert book is not None and book["at"] == NOW
    assert book["bids"] == [[0.62, 100.0], [0.58, 200.0], [0.50, 5000.0]] and book["asks"] == [[0.66, 150.0], [0.70, 300.0]]
    assert attr_mod._side_depths(THIN_BOOK, None) == (100.0, 150.0, 0.64)
    assert attr_mod._side_depths(None, 0.5) == (None, None, None)


def test_book_depth_helper() -> None:
    depth, mid = attr_mod._book_depth(THIN_BOOK, None)
    assert (depth, mid) == (250.0, 0.64)
    # one-sided book: the best price is the reference
    depth, mid = attr_mod._book_depth(orderbook([(0.40, 10), (0.36, 20), (0.30, 99)], []), 0.9)
    assert (depth, mid) == (30.0, 0.40)
    # empty book: the surge price is the reference and nothing rests near it
    assert attr_mod._book_depth(orderbook([], []), 0.5) == (0.0, 0.5)
    assert attr_mod._book_depth(None, 0.5) == (None, None)


# --------------------------------------------------------------------------- integration with the real store, analytics and news


def _real_store() -> Any:
    store_mod = pytest.importorskip("supermarket_bot.store")
    try:
        return store_mod.TrackerStore(":memory:")
    except NotImplementedError:
        pytest.skip("store.py not implemented yet")


def test_integration_with_tracker_store_and_analytics(fake: Any, client: Any) -> None:
    from supermarket_bot import analytics
    from supermarket_bot.news import NewsProvider, NewsSearcher

    try:
        analytics.trade_flow([])
    except NotImplementedError:
        pytest.skip("analytics.trade_flow not implemented yet")
    store = _real_store()
    store.upsert_markets([market("m1", "Will Democrats win Ohio House District 9?", [("ex1", None, 0.5)], multi=False)])

    class Noise(NewsProvider):
        name = "noise"
        min_interval_s = 0.0

        def __init__(self) -> None:
            self.queries: List[str] = []

        def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
            self.queries.append(query)
            return [Article("Celebrity chef opens restaurant", "https://x.example/chef", "X", NOW - 100)]

    provider = Noise()
    searcher = NewsSearcher([provider], store=store, clock=lambda: NOW)
    route_spike(fake)
    attributor = Attributor(client, CTX, store, searcher, clock=lambda: NOW)  # real analytics.trade_flow
    result = attributor.analyze(make_surge(change=0.18, end_price=0.68))

    # nothing relevant came back, so the searcher retried once with the three strongest terms
    assert provider.queries == ["Democrats Ohio House District", "Ohio House District"]
    assert [t.trade_id for t in store.trades("ex1", START - 7200)] == ["t1", "t2", "t3"]
    assert result.flow is not None and result.flow.n_trades == 2 and result.flow.total_size == 900
    assert result.flow.top_trade_share == pytest.approx(500 / 900, abs=1e-6)
    assert result.verdict == "participants"
    assert result.articles == []  # the irrelevant article scored 0 and was dropped
    stored = store.record_surge(make_surge())
    store.set_attribution(stored.id, result)
    back = store.get_surge(stored.id)
    assert back is not None and back.attribution is not None and back.attribution.verdict == "participants"
    store.close()


# --------------------------------------------------------------------------- round-1 QA regressions


class _DownProvider:
    """A news provider whose every search fails (the providers never raise; they set last_error)."""

    min_interval_s = 0.0

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        self.last_error: Optional[str] = None

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        self.last_error = self.reason
        return []

    def close(self) -> None:
        pass


class _EmptyProvider(_DownProvider):
    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        self.last_error = None
        return []


def test_live_api_8_news_outage_is_not_evidence_of_no_news(fake: Any, client: Any) -> None:
    from supermarket_bot.news import NewsSearcher

    def run(providers: List[Any]) -> Attribution:
        route_spike(fake)
        searcher = NewsSearcher(providers, clock=lambda: NOW, sleep=lambda s: None)
        return Attributor(client, CTX, FakeStore(INFO), searcher, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())

    quiet = run([_EmptyProvider("google-news", ""), _EmptyProvider("gdelt", "")])
    down = run([_DownProvider("google-news", "ConnectError: [Errno -3] name resolution failed"),
                _DownProvider("gdelt", "HTTP 503: Service Unavailable")])
    assert quiet.news_status == "ok" and down.news_status == "unavailable"
    assert quiet.verdict == "participants" and quiet.confidence == pytest.approx(0.9)
    assert "No news articles matched this market around the move" in quiet.reasons
    text = "\n".join(down.reasons)
    assert "No news articles matched" not in text
    assert "News search failed (google-news: ConnectError" in text and "gdelt: HTTP 503" in text
    # five crowd signals still carry a participants verdict, but capped and with lower odds
    assert down.verdict == "participants"
    assert down.confidence == pytest.approx(attr_mod.OUTAGE_MAX_CONFIDENCE)
    assert down.reversion_odds == pytest.approx(attr_mod.OUTAGE_MAX_REVERSION) and down.reversion_odds < quiet.reversion_odds
    assert "news search failed" in down.summary
    stored = Attribution(**{**down.to_dict(), "articles": [], "flow": None})
    assert stored.news_status == "unavailable"


def test_live_api_8_outage_needs_crowd_evidence_that_stands_alone() -> None:
    flow = make_flow(n_trades=4)  # one crowd signal (few trades)
    surge = make_surge(change=0.05, end_price=0.55)
    ok = attribute(surge, flow, [], 300.0, "t", None, NOW, news_status="ok")  # + thin book = 2 signals
    assert ok.verdict == "participants"
    down = attribute(surge, flow, [], 300.0, "t", None, NOW, news_status="unavailable", news_errors=["gdelt: HTTP 503"])
    assert down.verdict == "unclear" and down.news_status == "unavailable"
    assert "News search failed (gdelt: HTTP 503)" in down.reasons[1]
    assert "3 are needed without news" in "\n".join(down.reasons)
    disabled = attribute(surge, flow, [], 300.0, "t", None, NOW, news_status="disabled")
    assert disabled.verdict == "participants" and disabled.news_status == "disabled"


def test_live_api_8_status_for_disabled_raising_and_legacy_searchers(fake: Any, client: Any) -> None:
    route_spike(fake)
    none = Attributor(client, CTX, FakeStore(INFO), None, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert none.news_status == "disabled"
    route_spike(fake)
    legacy = Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert legacy.news_status == "ok"  # a searcher without search_market(): its empty answer is trusted
    route_spike(fake)
    broken = Attributor(client, CTX, FakeStore(INFO), FakeNews(error=RuntimeError("x")), clock=lambda: NOW,
                        flow_fn=simple_flow).analyze(make_surge())
    assert broken.news_status == "unavailable"


def test_live_api_8_llm_merge_keeps_the_outage_cap(fake: Any, client: Any) -> None:
    from supermarket_bot.news import NewsSearcher

    route_spike(fake)
    judge = FakeJudge({"verdict": "participants", "confidence": 1.0, "reversion_odds": 1.0, "explanation": "few traders",
                       "key_article_indexes": []})
    searcher = NewsSearcher([_DownProvider("gdelt", "HTTP 503")], clock=lambda: NOW, sleep=lambda s: None)
    result = Attributor(client, CTX, FakeStore(INFO), searcher, judge=judge, clock=lambda: NOW, flow_fn=simple_flow).analyze(make_surge())
    assert result.method == "heuristic+llm" and result.verdict == "participants"
    assert result.confidence <= attr_mod.OUTAGE_MAX_CONFIDENCE and result.reversion_odds <= attr_mod.OUTAGE_MAX_REVERSION


def test_functional_1_flow_reads_only_the_trades_that_made_the_move(fake: Any, client: Any) -> None:
    """A 1h surge that stayed open for hours still reads the tape around its own window."""
    late = T0 + 5 * 3600
    tape = [trade(f"late{i}", T0 + 3600 * i, 300, side="YES" if i % 2 else "NO") for i in range(1, 6)] + SPIKE_TRADES
    route_spike(fake, trades=trades_page(tape))
    seen: List[List[TradeRecord]] = []

    def flow_fn(records: Sequence[TradeRecord]) -> TradeFlow:
        seen.append(list(records))
        return simple_flow(records)

    surge = make_surge(end_ts=late, detected_at=T0)
    assert attr_mod.flow_until(surge) == pytest.approx(START + 1.25 * 3600 + attr_mod.FLOW_SLACK_S)
    Attributor(client, CTX, FakeStore(INFO), FakeNews(), clock=lambda: late, flow_fn=flow_fn).analyze(surge)
    assert [r.trade_id for r in seen[0]] == ["t2", "t3"]
    assert attr_mod.flow_until(make_surge()) == pytest.approx(T0 + attr_mod.FLOW_SLACK_S)  # a fresh surge: up to its end


def test_lookahead_3_analyze_stamps_the_completion_time(fake: Any, client: Any) -> None:
    """lookahead-3: the attribution exists only once its reads, news search and judge returned, so analyzed_at is the
    clock when analyze() returns (a replay's analyzed_at <= t rule must not see it earlier); the news window still
    ends at the start."""
    route_spike(fake)
    times = iter([NOW, NOW + 59.0])
    news = FakeNews()
    attributor = Attributor(client, CTX, FakeStore(INFO), news, clock=lambda: next(times), flow_fn=simple_flow)
    result = attributor.analyze(make_surge())
    assert result.analyzed_at == NOW + 59.0
    assert news.calls and news.calls[0][3] == NOW
