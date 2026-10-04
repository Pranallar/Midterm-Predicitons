"""Online news search for surge attribution (Google News RSS, GDELT, optional NewsAPI).

The Super Market API has no news endpoint, so a market's own title (and the outcome's
option label) is turned into a search query, sent to public news sources, and every
result is scored for relevance to the market. See docs/DESIGN.md ("News").

* :func:`build_query` / :func:`keywords` turn "Will Republicans win the Pennsylvania
  Senate race?" into ``Republicans Pennsylvania Senate``.
* :func:`relevance` scores an article's title and summary against those keywords.
* Providers (:class:`GoogleNewsRSS`, :class:`GDELTDoc`, :class:`NewsAPIOrg`) each take an
  injectable ``httpx.Client`` and **never raise**: network, HTTP and parse failures are
  logged as warnings and the search returns ``[]``.
* :class:`NewsSearcher` queries every provider (respecting each provider's minimum
  interval between requests), dedups, scores, sorts and caches the results.
"""

from __future__ import annotations

import email.utils
import html
import json
import logging
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import __version__
from .books import parse_time
from .models import Article

log = logging.getLogger("supermarket_bot")

USER_AGENT = f"supermarket-bot/{__version__} (+news lookup)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # never buffer more than 2 MB from a news source
MAX_TERMS = 6
DEFAULT_CACHE_TTL = 1200.0  # 20 minutes per (query, window)
CACHE_BUCKET_S = 3600.0  # since/until are rounded to the hour for the cache key
DEFAULT_COOLDOWN_S = 120.0  # how long a provider rests after HTTP 429

GOOGLE_NEWS_URL = "https://news.google.com/rss/search"
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
NEWSAPI_URL = "https://newsapi.org/v2/everything"

# --------------------------------------------------------------------------- vocabulary

_US_STATES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming", "District of Columbia",
)
_STATE_CANON = {s.lower(): s for s in _US_STATES}
STATE_NAMES = frozenset(_STATE_CANON)

# Postal codes that are unambiguous in an election title ("AZ Governor", "PA-07"). IN, OR,
# ME, OK and HI are left out: they are ordinary words when a title is written in capitals.
_STATE_ABBR = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "MA": "Massachusetts", "MD": "Maryland", "MI": "Michigan",
    "MN": "Minnesota", "MO": "Missouri", "MS": "Mississippi", "MT": "Montana",
    "NC": "North Carolina", "ND": "North Dakota", "NE": "Nebraska", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NV": "Nevada", "NY": "New York", "OH": "Ohio",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VA": "Virginia", "VT": "Vermont",
    "WA": "Washington", "WI": "Wisconsin", "WV": "West Virginia", "WY": "Wyoming",
}

_OFFICES = {
    "senate": "Senate",
    "senator": "Senator",
    "sen": "Senator",
    "house": "House",
    "house of representatives": "House",
    "congress": "Congress",
    "governor": "Governor",
    "gov": "Governor",
    "governorship": "Governor",
    "gubernatorial": "Governor",
    "lieutenant governor": "Lieutenant Governor",
    "lt governor": "Lieutenant Governor",
    "lt gov": "Lieutenant Governor",
    "mayor": "Mayor",
    "mayoral": "Mayor",
    "attorney general": "Attorney General",
    "secretary of state": "Secretary of State",
    "state senate": "State Senate",
    "state house": "State House",
    "supreme court": "Supreme Court",
    "assembly": "Assembly",
    "legislature": "Legislature",
    "comptroller": "Comptroller",
    "treasurer": "Treasurer",
    "speaker": "Speaker",
    "president": "President",
}

_PARTIES = {
    "republican": "Republican",
    "republicans": "Republicans",
    "gop": "GOP",
    "democrat": "Democrat",
    "democrats": "Democrats",
    "democratic": "Democratic",
    "independent": "Independent",
    "independents": "Independents",
    "libertarian": "Libertarian",
    "libertarians": "Libertarians",
}

# Lower-case topical nouns worth searching for even though they are not proper nouns.
_TOPICS = frozenset(
    """
    turnout midterm midterms recount runoff primary primaries debate debates poll polls polling
    referendum ballot amendment proposition impeachment impeached shutdown indictment indicted
    endorsement endorses endorse filibuster redistricting gerrymander absentee resignation resign
    resigns concede concedes concession certification certify lawsuit abortion marijuana cannabis
    tariff tariffs inflation immigration gun guns
    """.split()
)

# Other recognised multi-word names that would otherwise be split.
_PROPER_PHRASES = {
    "white house": "White House",
    "electoral college": "Electoral College",
}

# Question scaffolding: dropped even when capitalised (sentence-initial or Title Case titles).
_STOPWORDS = frozenset(
    """
    will would who whom whose which what when where why how whether does do did is are was were
    be been being the a an and or nor but of in on at by for to from with without into onto after
    before during until than then more most less least over under above below between within
    exceed exceeds exceeding surpass reach reaches hit top beat beats
    win wins winning won winner winners lose loses losing lost control controls controlling
    hold holds keep keeps retain retains flip flips gain gains take takes become becomes remain
    remains get gets receive receives finish finishes end ends lead leads leading
    race races seat seats election elections electoral vote votes voting voter voters result
    results outcome outcomes market markets cup prediction predictions party parties candidate
    candidates nominee nominees reelection re-election reelected re-elected percent percentage
    point points share majority minority margin general special yes no not any all each every
    either neither other others it its this that these those there their they them he she his
    her him us usa me my we our you your ok hi oh new next last first second third
    day days week weeks month months year years night today tonight tomorrow
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec
    monday tuesday wednesday thursday friday saturday sunday
    q1 q2 q3 q4 est et pt utc am pm
    """.split()
)

# Capitalised words that are places or institutions in general rather than a specific name.
_GENERIC_PROPER = frozenset(
    "district county city state court committee congressional national federal north south east west".split()
)

_NON_NAME_OPTIONS = frozenset(
    """
    yes no y n true false other others field none tie draw neither over under above below higher
    lower up down nobody someone anyone
    """.split()
)

# term (lower-case) -> alternatives that count as a match in an article
_ALIASES = {
    "republican": ("republican", "republicans", "gop"),
    "republicans": ("republican", "republicans", "gop"),
    "gop": ("gop", "republican", "republicans"),
    "democrat": ("democrat", "democrats", "democratic", "dems"),
    "democrats": ("democrat", "democrats", "democratic", "dems"),
    "democratic": ("democrat", "democrats", "democratic", "dems"),
    "governor": ("governor", "governors", "gubernatorial", "governorship"),
    "senate": ("senate", "senator", "senators"),
    "house": ("house", "congress", "congressional"),
    "midterm": ("midterm", "midterms", "mid-term", "mid-terms"),
    "midterms": ("midterm", "midterms", "mid-term", "mid-terms"),
    "mayor": ("mayor", "mayoral"),
}

_PRIORITY = {"option": 0, "proper": 1, "state": 2, "office": 3, "party": 4, "topic": 5, "fallback": 6}

# Hyphens join words ("Ocasio-Cortez") but not numbers, so "PA-07" yields "PA" and "07".
_TOKEN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.'’]|-(?=[A-Za-z]))*")
_PAREN_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class _Token:
    text: str  # as written, minus trailing punctuation and possessive 's
    key: str  # lower-case, dots removed (for vocabulary lookups)
    start: int
    end: int


@dataclass
class _Term:
    text: str
    kind: str  # option | proper | state | office | party | topic | fallback
    pos: int


def _tokens(text: str) -> List[_Token]:
    out: List[_Token] = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group().rstrip(".-'’")
        if raw.lower().endswith(("'s", "’s")):
            raw = raw[:-2]
        if not raw:
            continue
        out.append(_Token(raw, raw.lower().replace(".", ""), m.start(), m.end()))
    return out


def _joined(text: str, a: _Token, b: _Token) -> bool:
    """True when nothing but whitespace separates two consecutive tokens."""
    return not text[a.end : b.start].strip()


def _vocab(key: str) -> Optional[Tuple[str, str]]:
    if key in STATE_NAMES:
        return "state", _STATE_CANON[key]
    if key in _OFFICES:
        return "office", _OFFICES[key]
    if key in _PARTIES:
        return "party", _PARTIES[key]
    if key in _PROPER_PHRASES:
        return "proper", _PROPER_PHRASES[key]
    if key in _TOPICS:
        return "topic", key
    return None


def _title_terms(text: str) -> List[_Term]:
    """Search terms in title order (before de-duplication and truncation)."""
    toks = _tokens(text)
    letters = [c for c in text if c.isalpha()]
    shouting = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8
    terms: List[_Term] = []
    proper: List[_Token] = []

    def flush() -> None:
        if proper:
            terms.append(_Term(" ".join(t.text for t in proper), "proper", proper[0].start))
            proper.clear()

    i = 0
    while i < len(toks):
        tok = toks[i]
        match = None
        for n in (4, 3, 2, 1):  # longest vocabulary phrase first ("New York", "Attorney General")
            if i + n > len(toks):
                continue
            span = toks[i : i + n]
            if any(not _joined(text, span[k], span[k + 1]) for k in range(n - 1)):
                continue
            found = _vocab(" ".join(t.key for t in span))
            if found:
                match = (n, found)
                break
        if match is None and not shouting and len(tok.text) == 2 and tok.text.isupper() and tok.text in _STATE_ABBR:
            match = (1, ("state", _STATE_ABBR[tok.text]))
        if match is not None:
            flush()
            n, (kind, canonical) = match
            terms.append(_Term(canonical, kind, tok.start))
            i += n
            continue
        is_name = (
            tok.text[0].isupper()
            and tok.key not in _STOPWORDS
            and not any(c.isdigit() for c in tok.text)
            and len(tok.key) > 1
        )
        if is_name:
            if proper and (len(proper) >= 4 or not _joined(text, proper[-1], tok)):
                flush()
            proper.append(tok)
        else:
            flush()
        i += 1
    flush()
    return terms


def _option_terms(option: Optional[str]) -> List[_Term]:
    """The option label as a search term when it names something (not YES/NO, not a number)."""
    if not option or not isinstance(option, str):
        return []
    label = _SPACE_RE.sub(" ", _PAREN_RE.sub(" ", option)).strip(" .,:;-")
    if not label or label.lower() in _NON_NAME_OPTIONS or any(c.isdigit() for c in label):
        return []
    words = label.split()
    if len(words) > 4:  # a sentence-like option: mine it like a title
        return [_Term(t.text, "option", -1) for t in _title_terms(label) if t.kind != "topic"][:2]
    if not any(w[:1].isupper() for w in words):
        return []
    return [_Term(label, "option", -1)]


def _select_terms(title: str, option: Optional[str]) -> List[_Term]:
    title = title or ""
    candidates = _option_terms(option) + _title_terms(title)
    seen = set()
    unique: List[_Term] = []
    for term in candidates:
        key = term.text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    if not unique:  # nothing recognisable: fall back to the longer content words
        for tok in _tokens(title):
            if tok.key in _STOPWORDS or len(tok.key) < 3 or any(c.isdigit() for c in tok.key) or tok.key in seen:
                continue
            seen.add(tok.key)
            unique.append(_Term(tok.key, "fallback", tok.start))
    chosen = sorted(unique, key=lambda t: (_PRIORITY[t.kind], t.pos))[:MAX_TERMS]
    return sorted(chosen, key=lambda t: (t.kind != "option", t.pos))


def _quote(term: str) -> str:
    return f'"{term}"' if " " in term else term


def build_query(title: str, option: Optional[str] = None) -> str:
    """A short news-search query for a market title and option label (at most 6 terms).

    Question scaffolding, years and numbers are dropped; proper nouns, US states, offices,
    parties, a few election topics ("turnout", "runoff") and a named option are kept.
    Multi-word terms are quoted: ``"Katie Hobbs" Arizona Governor``.
    """
    return " ".join(_quote(t.text) for t in _select_terms(title, option))


def keywords(title: str, option: Optional[str] = None) -> List[str]:
    """The terms of :func:`build_query`, lower-cased, for :func:`relevance`."""
    return [t.text.lower() for t in _select_terms(title, option)]


def _strong_terms(title: str, option: Optional[str] = None) -> List[str]:
    """Keywords that weigh double in :func:`relevance`: the option/candidate name and the state."""
    out = []
    for t in _select_terms(title, option):
        key = t.text.lower()
        if t.kind in ("option", "state") or (t.kind == "proper" and not set(key.split()) <= _GENERIC_PROPER):
            out.append(key)
    return out


# --------------------------------------------------------------------------- relevance


def _norm_text(text: str) -> str:
    text = text.casefold().replace("’", "'").replace("‘", "'")
    return _SPACE_RE.sub(" ", text)


@lru_cache(maxsize=1024)
def _term_pattern(term: str, surname: bool) -> "re.Pattern[str]":
    alternatives = list(_ALIASES.get(term, (term,)))
    if surname and " " in term and term not in STATE_NAMES and term not in _OFFICES:
        last = term.split()[-1]
        if len(last) >= 3:
            alternatives.append(last)
    parts = []
    for alt in alternatives:
        words = [re.escape(w) for w in re.split(r"[\s\-]+", alt) if w]
        if words:
            parts.append(r"[\s\-]+".join(words))
    body = "|".join(sorted(parts, key=len, reverse=True))
    return re.compile(r"(?<![a-z0-9])(?:" + body + r")(?:'s|s|es)?(?![a-z0-9])")


def relevance(article: Article, terms: Sequence[str], strong: Sequence[str] = ()) -> float:
    """0..1 weighted share of ``terms`` found in title/summary; ``strong`` terms weigh 2.

    Matching is case-insensitive on word boundaries, tolerates plurals and possessives,
    knows party/office synonyms ("GOP" for Republicans, "gubernatorial" for Governor) and
    accepts a candidate's surname alone ("Hobbs" for "Katie Hobbs").
    """
    unique: List[str] = []
    for term in terms:
        key = _norm_text(term or "").strip()
        if key and key not in unique:
            unique.append(key)
    if not unique:
        return 0.0
    strong_set = {_norm_text(s or "").strip() for s in strong}
    text = _norm_text(f"{article.title or ''} {article.summary or ''}")
    total = found = 0.0
    for key in unique:
        weight = 2.0 if key in strong_set else 1.0
        total += weight
        if _term_pattern(key, key in strong_set).search(text):
            found += weight
    return max(0.0, min(1.0, found / total))


# --------------------------------------------------------------------------- providers


def _clean_text(value: Optional[str], limit: int = 400) -> Optional[str]:
    if not value:
        return None
    text = _SPACE_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", value))).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fold(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _parse_rfc822(value: Optional[str]) -> Optional[float]:
    if not value or not value.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _parse_gdelt_date(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _fmt_utc(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


def _in_window(published: Optional[float], since: Optional[float], until: Optional[float]) -> bool:
    if published is None:
        return True
    if since is not None and published < since:
        return False
    if until is not None and published > until:
        return False
    return True


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    when = _parse_rfc822(value)
    return None if when is None else max(0.0, when - time.time())


class NewsProvider:
    """A news source. ``search`` returns at most ``limit`` articles and never raises."""

    name = "base"
    min_interval_s = 2.0
    last_error: Optional[str] = None  # why the most recent search failed (None after a success)

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        raise NotImplementedError

    def close(self) -> None:
        """Release resources (no-op unless the provider owns an HTTP client)."""


class _HTTPProvider(NewsProvider):
    """Shared plumbing: an injectable ``httpx.Client``, size-capped GETs, 429 cool-down."""

    cooldown_s = DEFAULT_COOLDOWN_S

    def __init__(self, http: Optional[httpx.Client] = None, *, clock: Callable[[], float] = time.time) -> None:
        self._owns_http = http is None
        self.http = http if http is not None else default_http_client()
        self._clock = clock
        self._blocked_until = 0.0
        self.last_error = None

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        query = (query or "").strip()
        if not query or limit <= 0:
            return []
        now = self._clock()
        if now < self._blocked_until:
            self.last_error = f"rate limited for {self._blocked_until - now:.0f}s more"
            log.debug("%s: skipping search, %s", self.name, self.last_error)
            return []
        try:
            articles = self._search(query, since, until, limit)
        except Exception as exc:  # never raise: a news outage must not break attribution
            self._fail(f"unexpected {type(exc).__name__}: {exc}")
            return []
        if articles is None:
            return []
        self.last_error = None
        return articles[:limit]

    def _search(self, query: str, since: Optional[float], until: Optional[float], limit: int) -> Optional[List[Article]]:
        raise NotImplementedError

    def _fail(self, reason: str) -> None:
        self.last_error = reason
        log.warning("news provider %s failed: %s", self.name, reason)

    def _get(self, url: str, params: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None) -> Optional[Tuple[int, bytes, httpx.Headers]]:
        """GET with a 2 MB body cap. Returns ``(status, body, headers)`` or None (logged)."""
        try:
            with self.http.stream("GET", url, params=dict(params), headers=dict(headers or {})) as resp:
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    self._fail(f"response too large ({declared} bytes)")
                    return None
                chunks: List[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        self._fail(f"response larger than {MAX_RESPONSE_BYTES} bytes")
                        return None
                    chunks.append(chunk)
                status, resp_headers = resp.status_code, resp.headers
        except httpx.HTTPError as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            return None
        body = b"".join(chunks)
        if status == 429:
            wait = max(_retry_after(resp_headers.get("Retry-After")) or 0.0, self.cooldown_s)
            self._blocked_until = self._clock() + wait
            self._fail(f"HTTP 429 (rate limited); pausing {wait:.0f}s")
            return None
        if not 200 <= status < 300:
            self._fail(f"HTTP {status}: {_snippet(body)}")
            return None
        return status, body, resp_headers


def _snippet(body: bytes, limit: int = 120) -> str:
    text = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", body[:2000].decode("utf-8", "replace"))).strip()
    return text[:limit] or "(empty body)"


def default_http_client() -> httpx.Client:
    """The HTTP client news providers use when none is injected."""
    return httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


class GoogleNewsRSS(_HTTPProvider):
    """Google News search RSS (no key). At most one request every 2 s."""

    name = "google-news"
    min_interval_s = 2.0

    def __init__(self, http: Optional[httpx.Client] = None, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(http, clock=clock)

    def _search(self, query: str, since: Optional[float], until: Optional[float], limit: int) -> Optional[List[Article]]:
        days = 7
        if since is not None:
            days = int(min(30, max(1, math.ceil((self._clock() - since) / 86400.0))))
        params = {"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        got = self._get(GOOGLE_NEWS_URL, params)
        if got is None:
            return None
        body = got[1]
        if b"<!ENTITY" in body or b"<!DOCTYPE" in body[:4096].upper():
            self._fail("RSS document declares a DTD/entities; refusing to parse")
            return None
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            self._fail(f"RSS parse error: {exc}")
            return None
        articles: List[Article] = []
        for item in root.iter("item"):
            article = self._article(item)
            if article is None or not _in_window(article.published_at, since, until):
                continue
            articles.append(article)
            if len(articles) >= limit:
                break
        return articles

    def _article(self, item: ET.Element) -> Optional[Article]:
        title = _clean_text(item.findtext("title"), limit=500)
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            return None
        source_el = item.find("source")
        source = _clean_text(source_el.text if source_el is not None else None, limit=120) or ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        elif not source and " - " in title:
            head, tail = title.rsplit(" - ", 1)
            if head.strip() and 0 < len(tail) <= 60:
                title, source = head.rstrip(), tail.strip()
        summary = _clean_text(item.findtext("description"))
        if summary:
            folded = _fold(summary)
            if source and folded.endswith(_fold(source)):
                folded = folded[: -len(_fold(source))].strip()
            if not folded or folded == _fold(title):
                summary = None  # Google's description is usually just the title and source again
        return Article(
            title=title,
            url=link,
            source=source,
            published_at=_parse_rfc822(item.findtext("pubDate")),
            summary=summary,
            provider=self.name,
        )


class GDELTDoc(_HTTPProvider):
    """GDELT DOC 2.0 article search (no key). At most one request every 5 s."""

    name = "gdelt"
    min_interval_s = 5.0

    def __init__(self, http: Optional[httpx.Client] = None, *, clock: Callable[[], float] = time.time) -> None:
        super().__init__(http, clock=clock)

    def _search(self, query: str, since: Optional[float], until: Optional[float], limit: int) -> Optional[List[Article]]:
        params: Dict[str, Any] = {
            "query": f"{query} sourcelang:english",
            "mode": "artlist",
            "format": "json",
            "maxrecords": max(1, min(250, int(limit))),
            "sort": "datedesc",
        }
        if since is not None:
            params["startdatetime"] = _fmt_utc(since, "%Y%m%d%H%M%S")
        if until is not None:
            params["enddatetime"] = _fmt_utc(until, "%Y%m%d%H%M%S")
        got = self._get(GDELT_DOC_URL, params)
        if got is None:
            return None
        body = got[1]
        if not body.strip():
            return []  # GDELT answers an empty body when nothing matched
        try:
            data = json.loads(body.decode("utf-8", "replace"), strict=False)
        except ValueError:
            # Bad queries come back as 200 text/HTML ("Your search contained a phrase too short").
            self._fail(f"non-JSON response: {_snippet(body)}")
            return None
        if not isinstance(data, dict):
            self._fail("unexpected JSON shape")
            return None
        articles: List[Article] = []
        for raw in data.get("articles") or []:
            if not isinstance(raw, dict):
                continue
            title = _clean_text(raw.get("title") if isinstance(raw.get("title"), str) else None, limit=500)
            url = raw.get("url") if isinstance(raw.get("url"), str) else ""
            if not title or not url:
                continue
            published = _parse_gdelt_date(raw.get("seendate"))
            if not _in_window(published, since, until):
                continue
            domain = raw.get("domain") if isinstance(raw.get("domain"), str) else ""
            articles.append(Article(title=title, url=url.strip(), source=domain, published_at=published, provider=self.name))
            if len(articles) >= limit:
                break
        return articles


class NewsAPIOrg(_HTTPProvider):
    """newsapi.org ``/v2/everything`` (needs ``NEWSAPI_KEY``; sent as a header, never in the URL)."""

    name = "newsapi"
    min_interval_s = 2.0

    def __init__(self, api_key: str, http: Optional[httpx.Client] = None, *, clock: Callable[[], float] = time.time) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        super().__init__(http, clock=clock)
        self._api_key = api_key

    def __repr__(self) -> str:  # keep the key out of logs and tracebacks
        return "NewsAPIOrg(api_key=***)"

    def _search(self, query: str, since: Optional[float], until: Optional[float], limit: int) -> Optional[List[Article]]:
        params: Dict[str, Any] = {
            "q": query,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": max(1, min(100, int(limit))),
        }
        if since is not None:
            params["from"] = _fmt_utc(since, "%Y-%m-%dT%H:%M:%SZ")
        if until is not None:
            params["to"] = _fmt_utc(until, "%Y-%m-%dT%H:%M:%SZ")
        got = self._get(NEWSAPI_URL, params, headers={"X-Api-Key": self._api_key})
        if got is None:
            return None
        try:
            data = json.loads(got[1].decode("utf-8", "replace"))
        except ValueError:
            self._fail(f"non-JSON response: {_snippet(got[1])}")
            return None
        if not isinstance(data, dict) or data.get("status") != "ok":
            code = data.get("code") if isinstance(data, dict) else None
            self._fail(f"API error {code or 'unknown'}")
            return None
        articles: List[Article] = []
        for raw in data.get("articles") or []:
            if not isinstance(raw, dict):
                continue
            title = _clean_text(raw.get("title") if isinstance(raw.get("title"), str) else None, limit=500)
            url = raw.get("url") if isinstance(raw.get("url"), str) else ""
            if not title or not url or title == "[Removed]":
                continue
            when = parse_time(raw.get("publishedAt"))
            published = when.timestamp() if when else None
            if not _in_window(published, since, until):
                continue
            src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            desc = raw.get("description") if isinstance(raw.get("description"), str) else None
            articles.append(
                Article(
                    title=title,
                    url=url.strip(),
                    source=str(src.get("name") or ""),
                    published_at=published,
                    summary=_clean_text(desc),
                    provider=self.name,
                )
            )
            if len(articles) >= limit:
                break
        return articles


def default_providers(http: Optional[httpx.Client] = None) -> List[NewsProvider]:
    """Google News + GDELT, plus NewsAPI when NEWSAPI_KEY is set."""
    providers: List[NewsProvider] = [GoogleNewsRSS(http), GDELTDoc(http)]
    key = (os.environ.get("NEWSAPI_KEY") or "").strip()
    if key:
        providers.append(NewsAPIOrg(key, http))
    return providers


# --------------------------------------------------------------------------- searcher

_TRACKING_PARAMS = frozenset("fbclid gclid ocid cmpid smid taid mc_cid mc_eid ref ref_src".split())


def _url_key(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = parts.netloc.lower()
    for prefix in ("www.", "m.", "amp."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    path = parts.path.rstrip("/")
    if path.endswith("/amp"):
        path = path[: -len("/amp")]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit(("", host, path, urlencode(sorted(query)), ""))


def _title_key(title: str) -> str:
    return _fold(title)


def _bucket(ts: Optional[float]) -> str:
    return "-" if ts is None else str(int(ts // CACHE_BUCKET_S))


class NewsSearcher:
    """Query every provider for a market, dedup, score relevance, sort and cache.

    ``store`` is anything with ``cached_news(key, max_age_s, now)`` / ``put_news(key,
    articles, now)`` (the tracker's ``TrackerStore``); without one a small in-memory cache
    is used. ``clock``/``sleep`` are injectable so tests never really wait.
    """

    def __init__(
        self,
        providers: Sequence[NewsProvider],
        store: object = None,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        clock: object = None,
        *,
        sleep: Optional[Callable[[float], None]] = None,
        relax: bool = True,
        memory_cache_size: int = 256,
    ) -> None:
        self.providers = list(providers)
        self.store = store
        self.cache_ttl = float(cache_ttl)
        self._clock: Callable[[], float] = clock if callable(clock) else time.time  # type: ignore[assignment]
        self._sleep = sleep if sleep is not None else time.sleep
        self.relax = relax
        self._lock = threading.Lock()
        self._next_slot: Dict[int, float] = {}
        self._memory: "OrderedDict[str, Tuple[float, List[Article]]]" = OrderedDict()
        self._memory_size = memory_cache_size
        self.requests_sent = 0

    # cache -----------------------------------------------------------------
    def _cache_key(self, query: str, since: Optional[float], until: Optional[float]) -> str:
        names = "+".join(p.name for p in self.providers)
        return f"news:v1:{names}:{query}|{_bucket(since)}|{_bucket(until)}"

    def _cache_get(self, key: str, now: float) -> Optional[List[Article]]:
        if self.store is not None:
            try:
                hit = self.store.cached_news(key, self.cache_ttl, now)  # type: ignore[attr-defined]
            except Exception as exc:  # a broken cache must not stop the search
                log.debug("news cache read failed: %s", exc)
            else:
                return None if hit is None else [replace(a) for a in hit]
            return None
        with self._lock:
            entry = self._memory.get(key)
            if entry is None or now - entry[0] > self.cache_ttl:
                return None
            self._memory.move_to_end(key)
            return [replace(a) for a in entry[1]]

    def _cache_put(self, key: str, articles: Sequence[Article], now: float) -> None:
        if self.store is not None:
            try:
                self.store.put_news(key, list(articles), now)  # type: ignore[attr-defined]
            except Exception as exc:
                log.debug("news cache write failed: %s", exc)
            return
        with self._lock:
            self._memory[key] = (now, [replace(a) for a in articles])
            self._memory.move_to_end(key)
            while len(self._memory) > self._memory_size:
                self._memory.popitem(last=False)

    # politeness ------------------------------------------------------------
    def _wait_turn(self, provider: NewsProvider) -> None:
        interval = float(getattr(provider, "min_interval_s", 0.0) or 0.0)
        if interval <= 0:
            return
        with self._lock:  # reserve the next slot so concurrent callers queue up
            now = self._clock()
            slot = max(now, self._next_slot.get(id(provider), now))
            self._next_slot[id(provider)] = slot + interval
        wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def _query_all(self, query: str, since: Optional[float], until: Optional[float], limit: int) -> Tuple[List[Article], bool]:
        """Every provider's results in provider order, and whether at least one succeeded."""
        found: List[Article] = []
        any_ok = False
        for provider in self.providers:
            self._wait_turn(provider)
            self.requests_sent += 1
            try:
                results = provider.search(query, since, until, limit)
            except Exception as exc:  # providers should never raise, but be defensive
                log.warning("news provider %s raised %s: %s", getattr(provider, "name", provider), type(exc).__name__, exc)
                continue
            if getattr(provider, "last_error", None) is None:
                any_ok = True
            found.extend(a for a in results or [] if isinstance(a, Article))
        return found, any_ok

    # search ------------------------------------------------------------------
    def search_for_market(self, title: str, option: Optional[str], since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        """Relevant articles for a market, most relevant (then newest) first."""
        terms = _select_terms(title or "", option)
        if not terms or limit <= 0:
            return []
        query = " ".join(_quote(t.text) for t in terms)
        words = [t.text.lower() for t in terms]
        strong = _strong_terms(title or "", option)
        now = self._clock()
        key = self._cache_key(query, since, until)
        cached = self._cache_get(key, now)
        if cached is not None:
            return self._rank(cached, words, strong)[:limit]

        raw, any_ok = self._query_all(query, since, until, limit)
        ranked = self._rank(self._dedup(raw), words, strong)
        if not ranked and any_ok and self.relax and len(terms) > 3:
            # An over-specified query can miss real coverage: retry once with the top terms.
            top = sorted(terms, key=lambda t: (_PRIORITY[t.kind], t.pos))[:3]
            relaxed = " ".join(_quote(t.text) for t in sorted(top, key=lambda t: (t.kind != "option", t.pos)))
            log.debug("no news for %r; retrying with %r", query, relaxed)
            more, ok2 = self._query_all(relaxed, since, until, limit)
            any_ok = any_ok or ok2
            ranked = self._rank(self._dedup(more), words, strong)
        ranked = ranked[:limit]
        if any_ok:  # do not cache an outage as "no news"
            self._cache_put(key, ranked, now)
        return ranked

    @staticmethod
    def _dedup(articles: Sequence[Article]) -> List[Article]:
        out: List[Article] = []
        index: Dict[str, Article] = {}
        for art in articles:
            keys = []
            url_key = _url_key(art.url) if art.url else ""
            title_key = _title_key(art.title) if art.title else ""
            if url_key:
                keys.append("u:" + url_key)
            if title_key:
                keys.append("t:" + title_key)
            held = next((index[k] for k in keys if k in index), None)
            if held is None:
                held = replace(art)
                out.append(held)
            else:  # fill gaps from the duplicate (e.g. GDELT has the direct URL, Google the time)
                if held.published_at is None and art.published_at is not None:
                    held.published_at = art.published_at
                if not held.summary and art.summary:
                    held.summary = art.summary
                if not held.source and art.source:
                    held.source = art.source
            for k in keys:
                index.setdefault(k, held)
        return out

    @staticmethod
    def _rank(articles: Sequence[Article], words: Sequence[str], strong: Sequence[str]) -> List[Article]:
        scored: List[Article] = []
        for art in articles:
            art.relevance = round(relevance(art, words, strong), 4)
            if art.relevance > 0:
                scored.append(art)
        scored.sort(key=lambda a: (-a.relevance, -(a.published_at if a.published_at is not None else float("-inf"))))
        return scored

    def close(self) -> None:
        for provider in self.providers:
            try:
                provider.close()
            except Exception:  # pragma: no cover - best effort
                pass
