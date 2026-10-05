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
    FairValueRefresh,
    RaceRef,
    _Serializable,
)

log = logging.getLogger("supermarket_bot")

# --------------------------------------------------------------------------- constants

FV_REFRESH_S = 60.0  # the tracker's fair-value worker refreshes this often
FV_MAX_AGE_S = 90.0  # an external quote older than this is "stale" (unusable); = StrategyParams.fv_max_age_s
MANUAL_MAX_AGE_S = 72 * 3600.0  # a manual entry whose updated_at is older is "stale" (unusable)
FV_RECORD_HEARTBEAT_S = 600.0  # re-record an unchanged fair value at least this often (refresh rows prove the rest)
FV_RECORD_MIN_CHANGE = 0.005  # record when value, bid or ask moved by at least one tick (or usable/confidence changed)
REFRESH_DEADLINE_S = 30.0  # one refresh of all providers starts no new request after this
MAX_RETRY_AFTER_S = 600.0  # a venue's Retry-After is honoured up to this
SUSPECT_GAP = 0.25  # |fair value - Cup mid| above this: "suspect match", not usable until the user confirms it
# +/- of a venue's price as an estimate of the probability: fees and rounding (Polymarket ~1c, Kalshi ~1-2c)
FEE_BAND: Dict[str, float] = {"polymarket": 0.01, "kalshi": 0.02, "demo": 0.0, "manual": 0.0, "history": 0.02}
MANUAL_UNCERTAINTY = 0.02  # a manual entry without its own "uncertainty"
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
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"  # GET /prices-history only (optional history import)
KALSHI_BASE_URLS = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",  # fallback on DNS / connection errors
)
EXTERNAL_TIMEOUT_S = 8.0
EXTERNAL_READS_PER_MIN = 30  # per host, our own politeness budget (the hosts allow far more)
POLYMARKET_IDS_PER_CALL = 50  # GET /markets?id=..&id=..&limit=<n>
KALSHI_TICKERS_PER_CALL = 100  # GET /markets?tickers=a,b,c
DISCOVERY_EVERY_S = 6 * 3600.0  # unmatched races are searched for at most this often
DISCOVERY_PAGE_LIMIT = 100  # GET /events?...&limit=100&offset=k, at most 3 pages
HISTORY_POLYMARKET_FIDELITY_MIN = 5  # /prices-history fidelity (minutes) for the history import
HISTORY_KALSHI_PERIOD_MIN = 60  # /markets/candlesticks period_interval for the history import
HISTORY_MAX_DAYS = 14  # venues keep fine history for about 7-15 days
BACKOFF_S = (30.0, 60.0, 120.0, 300.0, 600.0)  # after consecutive failures of one host
MAP_FILE = "fair_value_map.json"
MANUAL_JSON = "fair_values.json"
MANUAL_CSV = "fair_values.csv"

FV_MODES = ("auto", "manual", "off")  # CLI --fair-value: external + manual | manual only | nothing

# Shown with every fair-value table (``/api/fairvalue`` "caveats", the ``fairvalue`` command); exact texts (§10.4).
FV_CAVEATS: Tuple[str, ...] = (
    "Outside prices are other markets' opinions, not the truth: they carry their own biases (longshots tend to be overpriced) and fees.",
    "Polymarket resolves on media calls or certification and Kalshi on swearing-in; the Cup's own resolution rule may differ.",
    "Chamber control and Independent legs are near matches only: their settlement wording differs, so they are shown but not traded unless you enable them.",
    "A fair value is traded only when the gap exceeds its own uncertainty (venue disagreement, spread and fees) plus the minimum edge.",
)

# Kalshi event-ticker prefixes that encode the race (the pin's ticker minus its last "-<party>" segment must
# match the target's race): {st} = USPS code, {n} = district number without padding, {nn} = zero-padded.
KALSHI_EVENT_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "SENATE": (r"^(KX)?SENATE{st}S?-",),
    "GOVERNOR": (r"^(KX)?GOVPARTY{st}-",),
    "HOUSE": (r"^HOUSE{st}{n}-", r"^KXHOUSERACE-{st}{nn}-"),
    "SENATE_CONTROL": (r"^CONTROLS-",),
    "HOUSE_CONTROL": (r"^CONTROLH-",),
}

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
STATE_NAMES: Dict[str, str] = {code: name for name, code in STATE_CODES.items()}


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
    trade_near: bool = False  # override "trade_near": true: a NEAR match may be traded (needs near_extra_edge more)
    confirmed: bool = False  # override "confirmed": true: the user checked the match (clears the suspect-gap guard)
    cup_mid: Optional[float] = None  # the Cup's YES mid at the latest refresh (suspect-gap guard)


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
    uncertainty: Optional[float] = None  # +/- as a probability; None = MANUAL_UNCERTAINTY
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
    map: Optional[Dict[str, Any]] = None  # MatchMap.status(): {"path", "exists", "overrides", "loaded_at", "errors"}


class FairValueProvider(Protocol):
    """An external source of probabilities. ``refresh`` must never raise and never block for longer
    than its own timeouts; failures become ``status``/``errors`` in the result."""

    name: str

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        """``deadline`` (epoch s, our clock): start no request after it. The first connect error or timeout
        ends this provider's refresh (status "offline"); no further requests until the backoff ends."""
        ...

    def close(self) -> None:
        ...


Recorder = Callable[[Sequence[FairValueRecord]], Any]  # e.g. TrackerStore.add_fair_values
RefreshRecorder = Callable[[FairValueRefresh], Any]  # e.g. TrackerStore.add_fair_value_refresh


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
    ``disabled: true`` switches the outcome off; ``trade_near`` / ``confirmed`` set those flags)."""
    raise NotImplementedError


def known_unmatched_races() -> frozenset:
    """Race keys whose every pin row has neither a Polymarket id nor a Kalshi ticker (no party-level outside
    market exists, e.g. the Alaska Governor): discovery never searches for them."""
    raise NotImplementedError


def race_named_in(text: Optional[str], race: RaceRef) -> bool:
    """True when ``text`` (a Polymarket question or ``events[0].title``) names the race: the state's full
    name and the office word ("Senate" / "Governor"), or the district as "ST-NN" / "ST-N" for House seats,
    or "Senate" / "House" for chamber control. Case-insensitive, whole words."""
    raise NotImplementedError


def kalshi_ticker_matches(ticker: str, race: RaceRef) -> bool:
    """True when the event ticker (``ticker`` minus its last "-" segment) matches KALSHI_EVENT_PATTERNS for
    the race's office, state and district."""
    raise NotImplementedError


def uncertainty_of(quotes: Sequence[FairValueQuote], agreement: Optional[float]) -> float:
    """``max(agreement or 0, tightest venue half-spread, max FEE_BAND of the venues used)``; a
    last-trade-only quote counts as half-spread 0.05."""
    raise NotImplementedError


def override_snippet(target: MatchTarget, action: str, external: Optional[Mapping[str, str]] = None) -> str:
    """Copyable JSON text for ``fair_value_map.json`` ``overrides`` (§4.6): action "disable" ->
    ``"1070": {"disabled": true, "note": "wrong match"}``; "pin" -> ``"1070": {"polymarket": "<id>",
    "kalshi": "<ticker>", "confirmed": true}`` with the current matches' ids (or ``external``); "confirm" ->
    ``{"confirmed": true}``; "trade_near" -> ``{"trade_near": true}``. Deterministic key order."""
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
    for validated ids; discovery: ``GET /events?tag_slug=midterms&active=true&closed=false&limit=100&offset=k``
    at most every DISCOVERY_EVERY_S, never for known_unmatched_races(). Parses every field variant
    (JSON-encoded strings, numbers as strings). Its httpx.Client comes from readonly.outside_client."""

    name = "polymarket"

    def __init__(self, *, base_url: str = POLYMARKET_GAMMA_URL, clob_url: str = POLYMARKET_CLOB_URL,
                 transport: Any = None, timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        raise NotImplementedError

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        raise NotImplementedError

    def history(self, targets: Sequence[MatchTarget], start: float, end: float, *,
                fidelity_min: int = HISTORY_POLYMARKET_FIDELITY_MIN) -> Tuple[List[FairValueRecord], List[str]]:
        """Optional import (§4.11): ``GET {clob}/prices-history?market=<YES token>&startTs&endTs&fidelity=<min>``
        (windows of at most 14 days) for every validated match; one FairValueRecord per point, source
        "history", ``ts`` = ``as_of`` = the point time + fidelity (the bar END), usable True, confidence
        "medium", detail {"indicative": true} (Polymarket's ``p`` is a mid/last, not executable). Returns
        (records, plain-sentence errors). GET only."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class KalshiProvider:
    """Kalshi v2 public reads (docs/PAPER_TRADING.md §4.5): ``GET /markets?tickers=a,b,..&limit=100`` (<= 100);
    prices are 4-decimal dollar strings (``yes_bid_dollars``), sizes ``*_fp`` strings; integer-cent
    fields are gone. Falls back to the second base URL on connection errors. Its httpx.Client comes from
    readonly.outside_client."""

    name = "kalshi"

    def __init__(self, *, base_urls: Sequence[str] = KALSHI_BASE_URLS, transport: Any = None,
                 timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        raise NotImplementedError

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        raise NotImplementedError

    def history(self, targets: Sequence[MatchTarget], start: float, end: float, *,
                period_min: int = HISTORY_KALSHI_PERIOD_MIN) -> Tuple[List[FairValueRecord], List[str]]:
        """Optional import (§4.11): ``GET /markets/candlesticks?market_tickers=<<=100>&start_ts&end_ts&period_interval=<min>``;
        one FairValueRecord per candle from its ``yes_bid`` / ``yes_ask`` close (``*_dollars``), ``ts`` = ``as_of``
        = ``end_period_ts`` (a bar is known only when its period ends), source "history", detail
        {"indicative": false, "bid_ask": true}. GET only."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- combining


def normalise_race(quotes: Mapping[str, FairValueQuote], races: Mapping[str, RaceRef]) -> Dict[str, FairValueQuote]:
    """Per venue and race: when every QUOTED leg (a leg with a ``raw_value`` after placeholder filtering;
    a dropped placeholder leg is not quoted) has a raw value and their sum is inside RACE_SUM_BAND,
    ``value = raw_value / sum``; otherwise the legs keep ``value = None`` and get the "suspect-sum" flag.
    A race with one quoted leg keeps ``value = raw_value``."""
    raise NotImplementedError


def renormalise_combined(values: Mapping[str, FairValue], races: Mapping[str, RaceRef]) -> Dict[str, FairValue]:
    """After ``blend`` and ``combine``: for every race whose legs ALL have a value, rescale them to sum to 1
    when the sum is inside RACE_SUM_BAND (the D and R legs may have been blended from different venue
    sets); outside the band every leg of the race becomes unusable ("legs disagree: sum 1.08")."""
    raise NotImplementedError


def blend(exchange_id: str, quotes: Sequence[FairValueQuote], now: float, *, race: Optional[RaceRef] = None,
          max_age_s: float = FV_MAX_AGE_S, cup_mid: Optional[float] = None, trade_near: bool = False,
          confirmed: bool = False) -> FairValue:
    """Combine one outcome's venue quotes: drop stale (older than ``max_age_s``) and unmatched
    (match_confidence < MIN_MATCH_CONFIDENCE) ones; blend the rest in log-odds with weights
    ``1 / max(half_spread, 0.005) ** 2``; ``agreement`` = max - min; confidence "high" (a venue with
    spread <= HIGH_CONF_SPREAD and agreement <= HIGH_CONF_AGREEMENT or one venue), "medium"
    (agreement <= MAX_DISAGREEMENT), else "low" and ``usable=False``. ``source`` is the venue name,
    or "blend" for several. ``bid``/``ask`` are the tightest venue's. ``uncertainty`` = uncertainty_of.
    A NEAR match is display-only: ``usable=False`` with reason "Near match (different settlement wording):
    shown, not traded" unless ``trade_near``. A value further than SUSPECT_GAP from ``cup_mid`` and not
    ``confirmed`` is ``suspect`` and unusable: "Suspect match: outside 0.86 vs Cup 0.42; check the match"."""
    raise NotImplementedError


def combine(manual: Mapping[str, FairValue], external: Mapping[str, FairValue]) -> Dict[str, FairValue]:
    """Manual values win over external ones for the same outcome; the external quotes are kept in
    ``sources`` and ``agreement`` is set against them."""
    raise NotImplementedError


# --------------------------------------------------------------------------- persistence of matches


class MatchMap:
    """``data/<tournament>/fair_value_map.json``: user overrides plus validated automatic matches
    (docs/PAPER_TRADING.md §4.6). Written atomically (temp file + replace). Re-read whenever the file's
    mtime changed (checked at every refresh and every set_targets), so an edit takes effect at the next
    refresh. Saving matches never loses a newer user edit (re-read first, keep ``overrides`` verbatim)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        raise NotImplementedError

    def reload_if_changed(self) -> bool:
        """Re-read when the mtime differs from the last load; True when the overrides changed."""
        raise NotImplementedError

    def overrides(self) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        """``{"path", "exists", "overrides": int, "loaded_at", "errors": [str]}`` (unknown keys, bad JSON)."""
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
                 refresh_recorder: Optional[RefreshRecorder] = None, clock: Callable[[], float] = time.time,
                 cup_mids: Optional[Callable[[], Mapping[str, float]]] = None) -> None:
        """``cup_mids()`` returns the Cup's current YES mids (the tracker passes a lock-free snapshot getter)
        for the suspect-gap guard; it must not take the tracker's lock."""
        raise NotImplementedError

    def set_targets(self, infos: Sequence[ExchangeInfo]) -> None:
        """Called by the tracker after each market-list refresh (open outcomes only), never while holding
        the tracker's lock."""
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

    def targets(self) -> List[MatchTarget]:
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        """``{"mode", "enabled", "last_refresh_at", "usable", "total", "providers": {...}, "manual": {...},
        "map": MatchMap.status()}``."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def default_providers(mode: str, *, transport: Any = None) -> List[FairValueProvider]:
    """``[PolymarketProvider(), KalshiProvider()]`` for mode "auto", else []."""
    raise NotImplementedError


def import_history(providers: Sequence[Any], targets: Sequence[MatchTarget], races: Mapping[str, RaceRef],
                   start: float, end: float, recorder: Recorder) -> Dict[str, Any]:
    """The optional, GET-only outside-history import (§4.11, CLI ``fairvalue --import-history``): for every
    provider with a ``history`` method, fetch [start, end] (clipped to HISTORY_MAX_DAYS), normalise per race
    and venue at each timestamp like ``normalise_race``, and pass the records to ``recorder``. Returns
    ``{"records": int, "by_venue": {venue: int}, "errors": [str], "start", "end"}``. Never raises."""
    raise NotImplementedError
