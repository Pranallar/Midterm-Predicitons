"""Online news search for surge attribution (Google News RSS, GDELT, optional NewsAPI).

Providers never raise: network/parse failures are logged and return []. See docs/DESIGN.md.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import httpx

from .models import Article


def build_query(title: str, option: Optional[str] = None) -> str:
    raise NotImplementedError


def keywords(title: str, option: Optional[str] = None) -> List[str]:
    raise NotImplementedError


def relevance(article: Article, terms: Sequence[str], strong: Sequence[str] = ()) -> float:
    """0..1 weighted share of ``terms`` found in title/summary; ``strong`` terms weigh 2."""
    raise NotImplementedError


class NewsProvider:
    name = "base"
    min_interval_s = 2.0

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        raise NotImplementedError


class GoogleNewsRSS(NewsProvider):
    name = "google-news"

    def __init__(self, http: Optional[httpx.Client] = None) -> None:
        raise NotImplementedError


class GDELTDoc(NewsProvider):
    name = "gdelt"
    min_interval_s = 5.0

    def __init__(self, http: Optional[httpx.Client] = None) -> None:
        raise NotImplementedError


class NewsAPIOrg(NewsProvider):
    name = "newsapi"

    def __init__(self, api_key: str, http: Optional[httpx.Client] = None) -> None:
        raise NotImplementedError


def default_providers(http: Optional[httpx.Client] = None) -> List[NewsProvider]:
    """Google News + GDELT, plus NewsAPI when NEWSAPI_KEY is set."""
    raise NotImplementedError


class NewsSearcher:
    def __init__(self, providers: Sequence[NewsProvider], store: object = None, cache_ttl: float = 1200.0, clock: object = None) -> None:
        raise NotImplementedError

    def search_for_market(self, title: str, option: Optional[str], since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        raise NotImplementedError
