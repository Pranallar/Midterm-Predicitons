"""External fair values for Super Market outcomes (read-only; package A of docs/PAPER_TRADING.md).

Sources, in precedence order for one outcome:

1. :class:`ManualFairValues`: ``data/<tournament>/fair_values.json`` (or ``.csv``) written by the
   user. Always works offline.
2. :class:`PolymarketProvider` (Gamma ``/markets``) and :class:`KalshiProvider` (``/markets``):
   public, keyless GET requests to *other* hosts, with their own small request budget, timeouts
   and backoff. They never touch the Super Market read budget and never block the tracker (they
   run in the tracker's fair-value worker thread).
3. A deterministic fake provider in demo mode (``demo.DemoFairValueProvider``, package E), which
   implements :class:`FairValueProvider`.

The race-key normaliser (:func:`parse_race`) turns a Super Market title into a
:class:`~supermarket_bot.models.RaceRef`; :func:`races_for` maps every open outcome to its race
(used for baskets even when fair values are off). Matches between Super Market outcomes and
external markets come from the pinned table (:mod:`supermarket_bot.fv_pins`), user overrides
and discovery, each with a confidence and a reason, and are persisted in
``data/<tournament>/fair_value_map.json``.

Fail closed: no match, a stale or placeholder quote, venues that disagree, or an unreachable host
all mean "no usable fair value" for that outcome, never a guessed one. Nothing here sends anything
but GET requests.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .models import (
    ExchangeInfo,
    FairValue,
    FairValueQuote,
    FairValueRecord,
    RaceRef,
    _Serializable,
)

log = logging.getLogger("supermarket_bot")

# --------------------------------------------------------------------------- constants

FV_REFRESH_S = 60.0  # the tracker's fair-value worker refreshes this often
FV_MAX_AGE_S = 300.0  # an external quote older than this is "stale" (unusable)
MANUAL_MAX_AGE_S = 72 * 3600.0  # a manual entry whose updated_at is older is "stale" (unusable)
FV_RECORD_HEARTBEAT_S = 600.0  # re-record an unchanged fair value at least this often
MAX_VENUE_SPREAD = 0.10  # wider: the venue value is its last trade ("last-trade-only") or nothing
HIGH_CONF_SPREAD = 0.04  # a "high" confidence needs one venue at least this tight
HIGH_CONF_AGREEMENT = 0.03
MAX_DISAGREEMENT = 0.05  # venues further apart: not usable for trading
RACE_SUM_BAND = (0.95, 1.05)  # per venue, the party values of one race must sum inside this
MIN_MATCH_CONFIDENCE = 0.8
CONF_PIN_EXACT = 0.95
CONF_PIN_NEAR = 0.80
CONF_DISCOVERY = 0.85
CONF_OVERRIDE = 1.0

POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
KALSHI_BASE_URLS = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",  # fallback on DNS / connection errors
)
EXTERNAL_TIMEOUT_S = 8.0
EXTERNAL_READS_PER_MIN = 30  # per host, our own politeness budget (the hosts allow far more)
POLYMARKET_IDS_PER_CALL = 50  # GET /markets?id=..&id=..&limit=<n>
KALSHI_TICKERS_PER_CALL = 100  # GET /markets?tickers=a,b,c
DISCOVERY_EVERY_S = 6 * 3600.0  # unmatched races are searched for at most this often
BACKOFF_S = (30.0, 60.0, 120.0, 300.0, 600.0)  # after consecutive failures of one host
MAP_FILE = "fair_value_map.json"
MANUAL_JSON = "fair_values.json"
MANUAL_CSV = "fair_values.csv"

FV_MODES = ("auto", "manual", "off")  # CLI --fair-value: external + manual | manual only | nothing

# Polymarket event titles that are NOT the race's winner market (sigprediction ``isWinnerEventFor``).
REJECT_EVENT_RE = re.compile(
    r"primary|margin|combo|closer|closest|nominee|turnout|debate|sweep|popular vote| and |\bvs\.?\b|seats",
    re.IGNORECASE,
)

# Super Market title grammar of the Cup (all 237 titles match; see tests/fixtures/external/supermarket_titles.txt).
CUP_TITLE_RE = re.compile(r"^Will the (Democratic|Republican|Independent|Libertarian|Green) Party win the (.+)\?$")
HOUSE_RACE_RE = re.compile(r"^([A-Z]{2})-(\d{1,2}) House race$")
# The demo's older wording ("Will Democrats win the Michigan Senate race?", "Will Republicans win
# Ohio House District 9?"); "keep control of the <State> Senate" is a state chamber and is NOT parsed.
LEGACY_TITLE_RE = re.compile(r"^Will (Democrats|Republicans|Independents) win (?:the )?(.+?)(?: race)?\?$")
LEGACY_DISTRICT_RE = re.compile(r"^(.+) House District (\d{1,2})$")

PARTY_WORDS: Dict[str, str] = {
    "democratic": "D", "democrat": "D", "democrats": "D", "democratic party": "D", "democratic nominee": "D",
    "republican": "R", "republicans": "R", "republican party": "R", "republican nominee": "R",
    "independent": "I", "independents": "I", "independent party": "I",
    "libertarian": "L", "green": "G",
    "any other candidate": "O", "other": "O", "someone else": "O", "field": "O",
}

STATE_CODES: Dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


# --------------------------------------------------------------------------- types (package A only)


@dataclass
class MatchTarget(_Serializable):
    """One Super Market outcome a provider should price."""

    exchange_id: str
    market_id: str
    title: str
    option: Optional[str] = None
    race: Optional[RaceRef] = None
    pins: Dict[str, str] = field(default_factory=dict)  # venue -> external id (pinned table or user override)
    pin_kind: str = "EXACT"  # "EXACT" | "NEAR" (fv_pins match_kind)
    disabled: bool = False  # the user switched this outcome's external fair value off


@dataclass
class VenueMatch(_Serializable):
    """A Super Market outcome matched to one external market."""

    venue: str  # "polymarket" | "kalshi" | "demo"
    exchange_id: str
    external_id: str
    label: str = ""
    kind: str = "EXACT"  # "EXACT" | "NEAR"
    confidence: float = 0.0
    reason: str = ""  # one sentence: "pinned id 630844 (participant map), validated: active, 2026, 'Chris Pappas (D)'"
    source: str = "pin"  # "pin" | "override" | "discovery" | "demo"
    validated_at: Optional[float] = None


@dataclass
class ProviderResult(_Serializable):
    venue: str
    status: str  # "ok" | "partial" | "offline" | "backoff" | "disabled" | "error"
    quotes: Dict[str, FairValueQuote] = field(default_factory=dict)  # exchange_id -> quote (before race normalisation)
    matches: Dict[str, VenueMatch] = field(default_factory=dict)
    requests: int = 0
    errors: List[str] = field(default_factory=list)  # plain sentences, no secrets, at most 5
    next_try_at: Optional[float] = None  # set while backing off


@dataclass
class ManualEntry(_Serializable):
    probability: Optional[float]  # None: a template row, ignored
    exchange_id: Optional[str] = None
    race_key: Optional[str] = None
    party: Optional[str] = None
    match: Optional[str] = None  # case-insensitive substring of title/option, or a regex when it starts with "re:"
    source: str = "manual"
    note: Optional[str] = None
    updated_at: Optional[float] = None
    line: Optional[int] = None  # 1-based line (CSV) or list index (JSON), for error messages


@dataclass
class ManualLoad(_Serializable):
    path: str
    exists: bool = False
    entries: List[ManualEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    loaded_at: Optional[float] = None
    mtime: Optional[float] = None


@dataclass
class MatchRow(_Serializable):
    """One outcome's matching state (``/api/fairvalue`` rows before Super Market quotes are added)."""

    exchange_id: str
    market_id: str
    title: str
    option: Optional[str]
    race_key: Optional[str]
    party: Optional[str]
    matches: List[VenueMatch] = field(default_factory=list)
    fair: Optional[FairValue] = None
    unmatched_reason: Optional[str] = None  # "no external market for this race (Alaska Governor)", ...


@dataclass
class FairValueSnapshot(_Serializable):
    at: Optional[float]
    mode: str = "auto"  # FV_MODES
    enabled: bool = True
    values: Dict[str, FairValue] = field(default_factory=dict)  # exchange_id -> combined value (usable or not)
    races: Dict[str, RaceRef] = field(default_factory=dict)
    # venue -> {"status", "last_ok_at", "last_error", "requests", "matched", "quoted", "next_try_at"}
    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    manual: Optional[Dict[str, Any]] = None  # {"path", "exists", "entries", "errors"}
    errors: List[str] = field(default_factory=list)


class FairValueProvider(Protocol):
    """An external source of probabilities. ``refresh`` must never raise and never block for longer
    than its own timeouts; failures become ``status``/``errors`` in the result."""

    name: str

    def refresh(self, targets: Sequence[MatchTarget], now: float) -> ProviderResult:
        ...

    def close(self) -> None:
        ...


Recorder = Callable[[Sequence[FairValueRecord]], Any]  # e.g. TrackerStore.add_fair_values


# --------------------------------------------------------------------------- race keys


def parse_race(title: str, option: Optional[str] = None) -> Optional[RaceRef]:
    """The race and party of one outcome from its market title (and option for multi-outcome
    markets), or None when the title is not an election race we understand.

    Grammar (docs/PAPER_TRADING.md §4.1): the Cup's ``Will the <Party> Party win the <race>?`` with
    race ``U.S. House`` / ``U.S. Senate`` (chamber control), ``<State> Senate``, ``<State> Governor``
    or ``<ST>-<NN> House race``; the demo's legacy ``Will Democrats win the <State> Senate race?`` /
    ``Will Republicans win <State> House District <n>?``; multi-outcome titles ``Which party will
    control the House|Senate ...`` and ``Who will win the <State> <Office> race?`` with the party
    taken from ``option`` (:data:`PARTY_WORDS`). District numbers are zero-padded to two digits.
    """
    raise NotImplementedError


def party_of(text: Optional[str]) -> Optional[str]:
    """``"D"``/``"R"``/``"I"``/``"L"``/``"G"``/``"O"`` for a party word or option label (:data:`PARTY_WORDS`,
    case-insensitive, also "Chris Pappas (D)" style tags), else None."""
    raise NotImplementedError


def races_for(infos: Iterable[ExchangeInfo], overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, RaceRef]:
    """exchange_id -> RaceRef for every outcome :func:`parse_race` understands. A user override
    (``fair_value_map.json`` ``overrides[eid].race_key``/``party``) wins and gets ``source="override"``."""
    raise NotImplementedError


def pins_by_race() -> Dict[Tuple[str, str], Dict[str, str]]:
    """``(race_key, party) -> {"polymarket": id, "kalshi": ticker, "label": ..., "match_kind": ...}`` from
    :data:`supermarket_bot.fv_pins.PINS` (empty ids left out)."""
    raise NotImplementedError


def build_targets(infos: Iterable[ExchangeInfo], races: Mapping[str, RaceRef],
                  overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[MatchTarget]:
    """One MatchTarget per outcome, with pins from the pinned table and user overrides
    (an override's ``polymarket``/``kalshi`` value replaces the pin; ``null`` removes it;
    ``disabled: true`` switches the outcome off)."""
    raise NotImplementedError


# --------------------------------------------------------------------------- manual file


def parse_probability(raw: Any) -> Optional[float]:
    """0..1 floats as is, "55%" -> 0.55, numbers in (1, 100] -> /100; None for empty; ValueError otherwise
    (outside [0, 1] after conversion, or not a number)."""
    raise NotImplementedError


class ManualFairValues:
    """The user's own fair values (docs/PAPER_TRADING.md §4.3). Re-read when the file's mtime changes;
    never raises (problems go to ``ManualLoad.errors``)."""

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self._clock = clock
        self._lock = threading.Lock()
        self._load: Optional[ManualLoad] = None

    @property
    def path(self) -> Path:
        """``fair_values.json`` when it exists or neither exists, else ``fair_values.csv``."""
        raise NotImplementedError

    def load(self) -> ManualLoad:
        raise NotImplementedError

    def resolve(self, targets: Sequence[MatchTarget], now: float) -> Dict[str, FairValue]:
        """exchange_id -> FairValue (source "manual", match_kind "MANUAL"). Precedence per outcome:
        exchange_id > race_key + party > match pattern; a pattern matching several outcomes is an
        error ("ambiguous") and is skipped. Usable when 0 <= p <= 1 and not older than MANUAL_MAX_AGE_S
        (entries without updated_at use the file's mtime)."""
        raise NotImplementedError

    def template(self, targets: Sequence[MatchTarget]) -> Dict[str, Any]:
        """A starter file: ``{"version": 1, "values": [{"exchange_id", "title", "race_key", "party",
        "probability": null, "source": "", "note": "", "updated_at": null}, ...]}``."""
        raise NotImplementedError


# --------------------------------------------------------------------------- external venues


def venue_value(bid: Optional[float], ask: Optional[float], last: Optional[float]) -> Tuple[Optional[float], List[str]]:
    """(probability, flags) for one venue quote: the mid when ``0 < bid < ask < 1`` and
    ``ask - bid <= MAX_VENUE_SPREAD``; else the last trade when ``0 < last < 1`` (flag
    "last-trade-only"); else None. bid 0 / ask 1 (or missing both) is a "placeholder"."""
    raise NotImplementedError


class PolymarketProvider:
    """Polymarket Gamma reads (docs/PAPER_TRADING.md §4.4). Steady state: ``GET /markets?id=..&limit=n``
    for validated ids; discovery: ``GET /events?tag_slug=midterms&active=true&closed=false&limit=500&offset=k``
    at most every DISCOVERY_EVERY_S. Parses every field variant (JSON-encoded strings, numbers as strings)."""

    name = "polymarket"

    def __init__(self, *, base_url: str = POLYMARKET_GAMMA_URL, transport: Any = None,
                 timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        raise NotImplementedError

    def refresh(self, targets: Sequence[MatchTarget], now: float) -> ProviderResult:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class KalshiProvider:
    """Kalshi v2 public reads (docs/PAPER_TRADING.md §4.5): ``GET /markets?tickers=a,b,..`` (<= 100);
    prices are 4-decimal dollar strings (``yes_bid_dollars``), sizes ``*_fp`` strings; integer-cent
    fields are gone. Falls back to the second base URL on connection errors."""

    name = "kalshi"

    def __init__(self, *, base_urls: Sequence[str] = KALSHI_BASE_URLS, transport: Any = None,
                 timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        raise NotImplementedError

    def refresh(self, targets: Sequence[MatchTarget], now: float) -> ProviderResult:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- combining


def normalise_race(quotes: Mapping[str, FairValueQuote], races: Mapping[str, RaceRef]) -> Dict[str, FairValueQuote]:
    """Per venue and race: when every quoted leg has a raw value and their sum is inside
    RACE_SUM_BAND, ``value = raw_value / sum``; otherwise the legs keep ``value = None`` and get the
    "suspect-sum" flag. A race with one quoted leg keeps ``value = raw_value``."""
    raise NotImplementedError


def blend(exchange_id: str, quotes: Sequence[FairValueQuote], now: float, *, race: Optional[RaceRef] = None,
          max_age_s: float = FV_MAX_AGE_S) -> FairValue:
    """Combine one outcome's venue quotes: drop stale (older than ``max_age_s``) and unmatched
    (match_confidence < MIN_MATCH_CONFIDENCE) ones; blend the rest in log-odds with weights
    ``1 / max(half_spread, 0.005) ** 2``; ``agreement`` = max - min; confidence "high" (a venue with
    spread <= HIGH_CONF_SPREAD and agreement <= HIGH_CONF_AGREEMENT or one venue), "medium"
    (agreement <= MAX_DISAGREEMENT), else "low" and ``usable=False``. ``source`` is the venue name,
    or "blend" for several. ``bid``/``ask`` are the tightest venue's."""
    raise NotImplementedError


def combine(manual: Mapping[str, FairValue], external: Mapping[str, FairValue]) -> Dict[str, FairValue]:
    """Manual values win over external ones for the same outcome; the external quotes are kept in
    ``sources`` and ``agreement`` is set against them."""
    raise NotImplementedError


# --------------------------------------------------------------------------- persistence of matches


class MatchMap:
    """``data/<tournament>/fair_value_map.json``: user overrides plus validated automatic matches
    (docs/PAPER_TRADING.md §4.6). Written atomically (temp file + replace)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        raise NotImplementedError

    def overrides(self) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def save_matches(self, matches: Iterable[VenueMatch], now: float) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- service


class FairValueService:
    """What the tracker owns: targets from the market list, a refresh per FV_REFRESH_S in the
    fair-value worker, the latest snapshot for readers, and recording into the store.

    ``mode`` "auto" uses ``providers`` + manual, "manual" only the manual file (no HTTP at all),
    "off" nothing (``current().enabled`` is False). ``refresh`` never raises.
    """

    def __init__(self, directory: Path, *, mode: str = "auto", providers: Sequence[FairValueProvider] = (),
                 manual: Optional[ManualFairValues] = None, recorder: Optional[Recorder] = None,
                 clock: Callable[[], float] = time.time) -> None:
        raise NotImplementedError

    def set_targets(self, infos: Sequence[ExchangeInfo]) -> None:
        """Called by the tracker after each market-list refresh (open outcomes only)."""
        raise NotImplementedError

    def refresh(self, now: Optional[float] = None) -> FairValueSnapshot:
        raise NotImplementedError

    def current(self) -> FairValueSnapshot:
        """The latest snapshot (a copy; thread-safe). Before the first refresh: empty values, races set."""
        raise NotImplementedError

    def races(self) -> Dict[str, RaceRef]:
        raise NotImplementedError

    def matches(self) -> List[MatchRow]:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        """``{"mode", "enabled", "last_refresh_at", "usable", "total", "providers": {...}, "manual": {...}}``."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def default_providers(mode: str, *, transport: Any = None) -> List[FairValueProvider]:
    """``[PolymarketProvider(), KalshiProvider()]`` for mode "auto", else []."""
    raise NotImplementedError
