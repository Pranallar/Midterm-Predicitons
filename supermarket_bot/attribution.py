"""Was a surge caused by news or by other Cup participants? Heuristic + optional Claude judge.

See docs/DESIGN.md (Attribution section).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .models import Article, Attribution, Surge, TradeFlow

DEFAULT_LLM_MODEL = "claude-opus-5-5"


def news_score(articles: Sequence[Article], surge: Surge) -> float:
    raise NotImplementedError


def crowd_signals(surge: Surge, flow: Optional[TradeFlow], book_depth: Optional[float]) -> List[str]:
    """Human-readable descriptions of each crowd signal that fired (count is ``len``, ``n_trades==0`` adds two)."""
    raise NotImplementedError


def attribute(
    surge: Surge,
    flow: Optional[TradeFlow],
    articles: Sequence[Article],
    book_depth: Optional[float],
    market_title: str,
    option: Optional[str],
    now: float,
) -> Attribution:
    raise NotImplementedError


class LLMJudge:
    def __init__(self, model: Optional[str] = None, client: Any = None) -> None:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError

    def judge(self, surge: Surge, flow: Optional[TradeFlow], articles: Sequence[Article], book_depth: Optional[float],
              market_title: str, option: Optional[str], heuristic: Attribution) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


def merge(heuristic: Attribution, llm: Optional[Dict[str, Any]], articles: Sequence[Article] = ()) -> Attribution:
    raise NotImplementedError


class Attributor:
    def __init__(self, client: Any, context: Any, store: Any, news: Any = None, judge: Optional[LLMJudge] = None,
                 limiter: Any = None, clock: Any = None) -> None:
        raise NotImplementedError

    def analyze(self, surge: Surge) -> Attribution:
        raise NotImplementedError
