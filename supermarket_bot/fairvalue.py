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

import csv
import dataclasses
import email.utils
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

import httpx

from . import fv_pins
from .models import (
    ExchangeInfo,
    FairValue,
    FairValueQuote,
    FairValueRecord,
    FairValueRefresh,
    RaceRef,
    _Serializable,
    iso_ts,
)
from .ratelimit import SlidingWindowLimiter
from .readonly import ReadOnlyViolation, outside_client

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

# ---- private helpers' constants (package A only)
_POLYMARKET_DISCOVERY_MAX_PAGES = 3
_KALSHI_DISCOVERY_MAX_PAGES = 5  # /events listings are long; discovery is optional and runs every 6 h at most
_KALSHI_EVENTS_PAGE_LIMIT = 200
_KALSHI_MAX_CANDLES_PER_CALL = 10_000  # the batch candlestick route returns at most this many per request
_CONTROL_OFFICES = ("SENATE_CONTROL", "HOUSE_CONTROL")
_STATE_OFFICES = ("SENATE", "GOVERNOR")
_PARTY_LETTERS = ("D", "R", "I", "L", "G", "O")
_PARTY_NAMES = {"D": "Democratic", "R": "Republican", "I": "Independent", "L": "Libertarian", "G": "Green", "O": "other"}
_VENUE_NAMES = {"polymarket": "Polymarket", "kalshi": "Kalshi", "demo": "the demo feed", "manual": "your file",
                "history": "imported history"}
_PARTY_TAG_RE = re.compile(r"\((D|R|I|L|G|DFL)\)\s*$", re.IGNORECASE)
_CONTROL_TITLE_RE = re.compile(r"^Which party will (?:control|win) (?:control of )?the (?:U\.S\. |US )?(House|Senate)\b.*\?$",
                               re.IGNORECASE)
_WHO_WINS_RE = re.compile(r"^Who will win (?:the )?(.+?)(?: race| election)?\?$", re.IGNORECASE)
_HOUSE_SEAT_RE = re.compile(r"^([A-Z]{2})-(\d{1,2})(?: House(?: race| seat)?)?$")
_RACE_KEY_RE = re.compile(r"^(\d{4}):(SENATE|GOVERNOR|HOUSE|SENATE_CONTROL|HOUSE_CONTROL):([A-Z]{2})(?:-(\d{1,2}))?$")
_DISTRICT_TOKEN_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2})-(\d{1,2})(?![0-9])")
_KALSHI_YEAR_RE = re.compile(r"-(26|2026|26NOV)$", re.IGNORECASE)
_KALSHI_DEM_RE = re.compile(r"Democratic (\(DFL\) )?party", re.IGNORECASE)
_KALSHI_REP_RE = re.compile(r"Republican party", re.IGNORECASE)
_GAMMA_PARTY_LABELS = {"democrat": "D", "republican": "R", "democratic party": "D", "republican party": "R",
                       "independent": "I"}
_STATES_BY_LENGTH = sorted(STATE_CODES, key=len, reverse=True)
_STATE_PATTERNS = {name: re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", re.IGNORECASE)
                   for name in STATE_CODES}
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)


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
    # venue -> "pin" (fv_pins) | "override" (fair_value_map.json set the id, or removed it with null). An
    # override is never replaced by discovery: if it fails validation the venue gives no value (fail closed).
    pin_sources: Dict[str, str] = field(default_factory=dict)


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
    token_id: Optional[str] = None  # Polymarket YES token (clobTokenIds[0]), for the optional history import


@dataclass
class ProviderResult(_Serializable):
    venue: str
    status: str  # "ok" | "partial" | "offline" | "backoff" | "disabled" | "error"
    quotes: Dict[str, FairValueQuote] = field(default_factory=dict)  # exchange_id -> quote (before race normalisation)
    matches: Dict[str, VenueMatch] = field(default_factory=dict)
    requests: int = 0
    errors: List[str] = field(default_factory=list)  # plain sentences, no secrets, at most 5
    next_try_at: Optional[float] = None  # set while backing off
    # exchange_id -> why a pinned / discovered market was dropped this refresh (one plain sentence)
    rejected: Dict[str, str] = field(default_factory=dict)


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


# --------------------------------------------------------------------------- small helpers


def _norm_ws(text: Any) -> str:
    return " ".join(str(text).split()) if text is not None else ""


def _num(raw: Any) -> Optional[float]:
    """A float from a number or a numeric string; None for missing, bools, NaN or anything else."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _json_list(raw: Any) -> Optional[List[Any]]:
    """Gamma's ``outcomes`` / ``outcomePrices`` / ``clobTokenIds`` are JSON-encoded strings; tolerate lists."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, list) else None
    return None


def _parse_time(raw: Any) -> Optional[float]:
    """Epoch seconds from an ISO string (any fraction length, Z or offset, or a bare date) or a number
    (milliseconds when it is implausibly large). None when it cannot be read."""
    if raw is None or isinstance(raw, bool):
        return None
    number = _num(raw)
    if number is not None:
        return number / 1000.0 if number > 1e11 else number
    if not isinstance(raw, str):
        return None
    m = _ISO_RE.match(raw.strip())
    if not m:
        return None
    year, month, day, hh, mm, ss, frac, tz = m.groups()
    try:
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0), int(ss or 0), micro, tzinfo=timezone.utc)
    except ValueError:
        return None
    ts = dt.timestamp()
    if tz and tz.upper() != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        ts -= sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    return ts


def _year_of(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip()
    if re.match(r"^\d{4}", text):
        return int(text[:4])
    ts = _parse_time(raw)
    return datetime.fromtimestamp(ts, tz=timezone.utc).year if ts is not None else None


def _fmt_p(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    text = f"{value:.3f}"
    return text[:-1] if text.endswith("0") else text


def _clip(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _venue_name(venue: str) -> str:
    return _VENUE_NAMES.get(venue, venue)


def _party_code(raw: Any) -> Optional[str]:
    """A party letter from a letter ("D") or a party word ("Democrats")."""
    if raw is None:
        return None
    text = _norm_ws(raw)
    if len(text) == 1 and text.upper() in _PARTY_LETTERS:
        return text.upper()
    return party_of(text)


def _race_from_key(key: Any, party: str, candidate: Optional[str] = None, source: str = "override") -> Optional[RaceRef]:
    if not isinstance(key, str):
        return None
    m = _RACE_KEY_RE.match(key.strip().upper())
    if not m:
        return None
    year, office, state, num = m.groups()
    if office == "HOUSE":
        if num is None or state not in STATE_NAMES:
            return None
        district = f"{state}-{int(num):02d}"
        return RaceRef(race_key=f"{year}:HOUSE:{district}", office="HOUSE", state=state, district=district, party=party,
                       candidate=candidate, source=source)
    if num is not None:
        return None
    if office in _CONTROL_OFFICES:
        if state != "US":
            return None
    elif state not in STATE_NAMES:
        return None
    return RaceRef(race_key=f"{year}:{office}:{state}", office=office, state=state, party=party, candidate=candidate,
                   source=source)


def _race_label(race: Optional[RaceRef]) -> str:
    """Short race name: "Alaska Governor", "AZ-01 House", "U.S. Senate control"."""
    if race is None:
        return "an unknown race"
    if race.office == "SENATE_CONTROL":
        return "U.S. Senate control"
    if race.office == "HOUSE_CONTROL":
        return "U.S. House control"
    if race.office == "HOUSE":
        return f"{race.district} House"
    name = STATE_NAMES.get(race.state, race.state)
    return f"{name} {'Senate' if race.office == 'SENATE' else 'Governor'}"


def _race_phrase(race: RaceRef) -> str:
    """The race as a noun phrase: "the New Hampshire Senate", "AZ-01", "control of the U.S. Senate"."""
    if race.office == "SENATE_CONTROL":
        return "control of the U.S. Senate"
    if race.office == "HOUSE_CONTROL":
        return "control of the U.S. House"
    if race.office == "HOUSE":
        return race.district or race.race_key
    return f"the {_race_label(race)}"


def _states_named(text: Optional[str]) -> List[str]:
    """USPS codes of the US states a text names (whole words, case-insensitive, longest name first so that
    "West Virginia" is not also read as "Virginia"), in order of first appearance."""
    if not text:
        return []
    work = str(text)
    found: List[Tuple[int, str]] = []
    for name in _STATES_BY_LENGTH:
        pattern = _STATE_PATTERNS[name]
        m = pattern.search(work)
        if m is None:
            continue
        found.append((m.start(), STATE_CODES[name]))
        work = pattern.sub(lambda mm: " " * len(mm.group(0)), work)
    return [code for _, code in sorted(found)]


def _districts_named(text: Optional[str]) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    for m in _DISTRICT_TOKEN_RE.finditer(str(text)):
        st, num = m.group(1), m.group(2)
        if st in STATE_NAMES:
            district = f"{st}-{int(num):02d}"
            if district not in out:
                out.append(district)
    return out


def _describe_named(text: Optional[str]) -> str:
    """What a text names, for a rejection sentence: "New Hampshire", "AZ-06", "the Senate", "no race"."""
    districts = _districts_named(text)
    if districts:
        return ", ".join(districts)
    states = [STATE_NAMES[c] for c in _states_named(text)]
    if states:
        return " and ".join(states)
    if text and re.search(r"\bSenate\b", text, re.IGNORECASE):
        return "the Senate"
    if text and re.search(r"\bHouse\b", text, re.IGNORECASE):
        return "the House"
    return "no race we recognise"


def _gamma_label_party(label: Any) -> Optional[str]:
    """The party a Polymarket ``groupItemTitle`` carries: a "(D)" / "(R)" / "(I)" tag, or exactly
    "Democrat", "Republican", "Democratic Party", "Republican Party", "Independent"."""
    text = _norm_ws(label)
    if not text:
        return None
    m = _PARTY_TAG_RE.search(text)
    if m:
        tag = m.group(1).upper()
        return "D" if tag == "DFL" else tag
    return _GAMMA_PARTY_LABELS.get(text.lower())


def _chunks(items: Sequence[Any], size: int) -> List[List[Any]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _copy_quote(q: FairValueQuote) -> FairValueQuote:
    return dataclasses.replace(q, flags=list(q.flags))


def _copy_fv(fv: FairValue) -> FairValue:
    return dataclasses.replace(fv, sources=[_copy_quote(q) for q in fv.sources])


def _copy_match(m: VenueMatch) -> VenueMatch:
    return dataclasses.replace(m)


def _copy_target(t: MatchTarget) -> MatchTarget:
    return dataclasses.replace(t, pins=dict(t.pins), pin_sources=dict(t.pin_sources))


def _eff_spread(q: FairValueQuote) -> float:
    """The spread a quote counts with: 0.10 for a last-trade-only value, else its own (unknown: 0.10)."""
    if "last-trade-only" in q.flags:
        return MAX_VENUE_SPREAD
    if q.spread is not None and q.spread >= 0:
        return float(q.spread)
    if q.bid is not None and q.ask is not None and q.ask >= q.bid:
        return float(q.ask - q.bid)
    return MAX_VENUE_SPREAD


def _retry_after_s(raw: Optional[str], now_wall: float) -> Optional[float]:
    """Seconds from a Retry-After header (delta seconds or an HTTP date), capped at MAX_RETRY_AFTER_S."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    seconds: Optional[float] = _num(text)
    if seconds is None:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - now_wall
    return _clip(float(seconds), 0.0, MAX_RETRY_AFTER_S)


# --------------------------------------------------------------------------- race keys


def _race_part(text: str) -> Optional[Tuple[str, str, Optional[str]]]:
    """(office, state, district) of a race phrase: "U.S. Senate", "New Hampshire Senate", "Georgia Governor",
    "AZ-01 House race", "Ohio House District 9"; None otherwise."""
    t = _norm_ws(text)
    if t.lower().startswith("the "):
        t = t[4:]
    if t in ("U.S. Senate", "US Senate", "United States Senate"):
        return ("SENATE_CONTROL", "US", None)
    if t in ("U.S. House", "US House", "United States House", "U.S. House of Representatives"):
        return ("HOUSE_CONTROL", "US", None)
    m = HOUSE_RACE_RE.match(t) or _HOUSE_SEAT_RE.match(t)
    if m:
        st, num = m.group(1), m.group(2)
        if st in STATE_NAMES:
            return ("HOUSE", st, f"{st}-{int(num):02d}")
        return None
    m = LEGACY_DISTRICT_RE.match(t)
    if m:
        code = STATE_CODES.get(m.group(1))
        if code:
            return ("HOUSE", code, f"{code}-{int(m.group(2)):02d}")
        return None
    for word, office in (("Senate", "SENATE"), ("Governor", "GOVERNOR")):
        suffix = " " + word
        if t.endswith(suffix):
            code = STATE_CODES.get(t[: -len(suffix)])
            if code:
                return (office, code, None)
    return None


def _candidate_of(option: Optional[str]) -> Optional[str]:
    text = _norm_ws(option)
    if not text or party_of(text) is not None and not _PARTY_TAG_RE.search(text):
        return None
    name = _PARTY_TAG_RE.sub("", text).strip()
    return name or None


def _build_ref(part: Optional[Tuple[str, str, Optional[str]]], party: Optional[str], *, source: str,
               candidate: Optional[str] = None) -> Optional[RaceRef]:
    if part is None or party is None:
        return None
    office, state, district = part
    key = f"2026:{office}:{district or state}"
    return RaceRef(race_key=key, office=office, state=state, district=district, party=party, candidate=candidate,
                   source=source)


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
    t = _norm_ws(title)
    if not t:
        return None
    m = CUP_TITLE_RE.match(t)
    if m:
        return _build_ref(_race_part(m.group(2)), party_of(m.group(1)), source="title")
    m = LEGACY_TITLE_RE.match(t)
    if m:
        return _build_ref(_race_part(m.group(2)), party_of(m.group(1)), source="title")
    m = _CONTROL_TITLE_RE.match(t)
    if m:
        office = "SENATE_CONTROL" if m.group(1).lower() == "senate" else "HOUSE_CONTROL"
        return _build_ref((office, "US", None), party_of(option), source="option", candidate=_candidate_of(option))
    m = _WHO_WINS_RE.match(t)
    if m:
        return _build_ref(_race_part(m.group(1)), party_of(option), source="option", candidate=_candidate_of(option))
    return None


def party_of(text: Optional[str]) -> Optional[str]:
    """``"D"``/``"R"``/``"I"``/``"L"``/``"G"``/``"O"`` for a party word or option label (:data:`PARTY_WORDS`,
    case-insensitive, also "Chris Pappas (D)" style tags), else None."""
    if text is None:
        return None
    t = _norm_ws(text)
    if not t:
        return None
    low = t.lower()
    if low.startswith("the "):
        low = low[4:]
    if low in PARTY_WORDS:
        return PARTY_WORDS[low]
    m = _PARTY_TAG_RE.search(t)
    if m:
        tag = m.group(1).upper()
        return "D" if tag == "DFL" else tag
    return None


def races_for(infos: Iterable[ExchangeInfo], overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, RaceRef]:
    """exchange_id -> RaceRef for every outcome :func:`parse_race` understands. A user override
    (``fair_value_map.json`` ``overrides[eid].race_key``/``party``) wins and gets ``source="override"``."""
    out: Dict[str, RaceRef] = {}
    all_overrides = overrides or {}
    for info in infos:
        eid = str(info.exchange_id)
        ref = parse_race(info.market_title, info.option)
        ov = all_overrides.get(eid)
        if isinstance(ov, Mapping) and (ov.get("race_key") or ov.get("party")):
            key = ov.get("race_key") or (ref.race_key if ref is not None else None)
            party = _party_code(ov.get("party")) if ov.get("party") else (ref.party if ref is not None else None)
            if key is not None and party is not None:
                candidate = ref.candidate if ref is not None and ref.race_key == key else None
                new = _race_from_key(key, party, candidate=candidate, source="override")
                if new is not None:
                    ref = new
        if ref is not None:
            out[eid] = ref
    return out


_PIN_INDEX: Optional[Dict[Tuple[str, str], Dict[str, str]]] = None
_PIN_LOCK = threading.Lock()


def _pin_index() -> Dict[Tuple[str, str], Dict[str, str]]:
    global _PIN_INDEX
    if _PIN_INDEX is None:
        with _PIN_LOCK:
            if _PIN_INDEX is None:
                index: Dict[Tuple[str, str], Dict[str, str]] = {}
                for row in fv_pins.PINS:
                    rec = dict(zip(fv_pins.PIN_COLUMNS, row))
                    entry: Dict[str, str] = {"match_kind": rec["match_kind"] or "EXACT"}
                    if rec["polymarket_id"]:
                        entry["polymarket"] = rec["polymarket_id"]
                        if rec["polymarket_label"]:
                            entry["label"] = rec["polymarket_label"]
                    if rec["kalshi_ticker"]:
                        entry["kalshi"] = rec["kalshi_ticker"]
                    entry["sig_exchange_id"] = rec["sig_exchange_id"]
                    entry["sig_title"] = rec["sig_title"]
                    index[(rec["race_key"], rec["party"])] = entry
                _PIN_INDEX = index
    return _PIN_INDEX


def pins_by_race() -> Dict[Tuple[str, str], Dict[str, str]]:
    """``(race_key, party) -> {"polymarket": id, "kalshi": ticker, "label": ..., "match_kind": ...}`` from
    :data:`supermarket_bot.fv_pins.PINS` (empty ids left out)."""
    return {key: dict(value) for key, value in _pin_index().items()}


def build_targets(infos: Iterable[ExchangeInfo], races: Mapping[str, RaceRef],
                  overrides: Optional[Mapping[str, Mapping[str, Any]]] = None) -> List[MatchTarget]:
    """One MatchTarget per outcome, with pins from the pinned table and user overrides
    (an override's ``polymarket``/``kalshi`` value replaces the pin; ``null`` removes it;
    ``disabled: true`` switches the outcome off; ``trade_near`` / ``confirmed`` set those flags)."""
    index = _pin_index()
    all_overrides = overrides or {}
    out: List[MatchTarget] = []
    for info in infos:
        eid = str(info.exchange_id)
        race = races.get(eid)
        pins: Dict[str, str] = {}
        sources: Dict[str, str] = {}
        kind = "EXACT"
        if race is not None:
            row = index.get((race.race_key, race.party))
            if row is not None:
                for venue in ("polymarket", "kalshi"):
                    if row.get(venue):
                        pins[venue] = row[venue]
                        sources[venue] = "pin"
                kind = row.get("match_kind") or "EXACT"
            elif race.office in _CONTROL_OFFICES or race.party == "I":
                kind = "NEAR"
        disabled = trade_near = confirmed = False
        ov = all_overrides.get(eid)
        if isinstance(ov, Mapping):
            for venue in ("polymarket", "kalshi"):
                if venue in ov:
                    value = ov[venue]
                    text = "" if value is None else str(value).strip()
                    sources[venue] = "override"
                    if text:
                        pins[venue] = text.upper() if venue == "kalshi" else text
                    else:
                        pins.pop(venue, None)
            disabled = ov.get("disabled") is True
            trade_near = ov.get("trade_near") is True
            confirmed = ov.get("confirmed") is True
        out.append(MatchTarget(
            exchange_id=eid, market_id=str(info.market_id), title=info.market_title, option=info.option, race=race,
            pins=pins, pin_kind=kind, disabled=disabled, trade_near=trade_near, confirmed=confirmed,
            pin_sources=sources,
        ))
    return out


def known_unmatched_races() -> frozenset:
    """Race keys whose every pin row has neither a Polymarket id nor a Kalshi ticker (no party-level outside
    market exists, e.g. the Alaska Governor): discovery never searches for them."""
    has_id: Dict[str, bool] = {}
    for (race_key, _party), entry in _pin_index().items():
        has_id[race_key] = has_id.get(race_key, False) or bool(entry.get("polymarket") or entry.get("kalshi"))
    return frozenset(key for key, any_id in has_id.items() if not any_id)


def race_named_in(text: Optional[str], race: RaceRef) -> bool:
    """True when ``text`` (a Polymarket question or ``events[0].title``) names the race: the state's full
    name and the office word ("Senate" / "Governor"), or the district as "ST-NN" / "ST-N" for House seats,
    or "Senate" / "House" for chamber control. Case-insensitive, whole words."""
    if not text or race is None:
        return False
    t = _norm_ws(text)
    if race.office in _STATE_OFFICES:
        if race.state not in _states_named(t):
            return False
        words = r"Senate" if race.office == "SENATE" else r"Governor|Gubernatorial"
        return re.search(rf"\b(?:{words})\b", t, re.IGNORECASE) is not None
    if race.office == "HOUSE":
        return bool(race.district) and race.district in _districts_named(t)
    if race.office in _CONTROL_OFFICES:
        word = "Senate" if race.office == "SENATE_CONTROL" else "House"
        if re.search(rf"\b{word}\b", t, re.IGNORECASE) is None:
            return False
        return not _states_named(t) and not _districts_named(t)  # a state's Senate race is not chamber control
    return False


def kalshi_ticker_matches(ticker: str, race: RaceRef) -> bool:
    """True when the event ticker (``ticker`` minus its last "-" segment) matches KALSHI_EVENT_PATTERNS for
    the race's office, state and district."""
    if not ticker or race is None:
        return False
    t = str(ticker).strip().upper()
    if "-" not in t:
        return False
    event = t.rsplit("-", 1)[0]
    patterns = KALSHI_EVENT_PATTERNS.get(race.office) or ()
    n = nn = ""
    if race.office == "HOUSE":
        if not race.district or "-" not in race.district:
            return False
        num = int(race.district.split("-", 1)[1])
        n, nn = str(num), f"{num:02d}"
    for pattern in patterns:
        regex = pattern.format(st=re.escape(race.state), n=n, nn=nn)
        if re.match(regex, event):
            return True
    return False


def uncertainty_of(quotes: Sequence[FairValueQuote], agreement: Optional[float]) -> float:
    """``max(agreement or 0, tightest venue half-spread, max FEE_BAND of the venues used)``; a
    last-trade-only quote counts as half-spread 0.05."""
    if not quotes:
        return float(agreement or 0.0)
    tightest = min(_eff_spread(q) / 2.0 for q in quotes)
    fee = max(FEE_BAND.get(q.venue, 0.02) for q in quotes)
    return max(float(agreement or 0.0), tightest, fee)


def override_snippet(target: MatchTarget, action: str, external: Optional[Mapping[str, str]] = None) -> str:
    """Copyable JSON text for ``fair_value_map.json`` ``overrides`` (§4.6): action "disable" ->
    ``"1070": {"disabled": true, "note": "wrong match"}``; "pin" -> ``"1070": {"polymarket": "<id>",
    "kalshi": "<ticker>", "confirmed": true}`` with the current matches' ids (or ``external``); "confirm" ->
    ``{"confirmed": true}``; "trade_near" -> ``{"trade_near": true}``. Deterministic key order."""
    eid = str(target.exchange_id)
    body: Dict[str, Any]
    if action == "disable":
        body = {"disabled": True, "note": "wrong match"}
    elif action == "pin":
        ids = dict(external) if external is not None else dict(target.pins)
        body = {}
        for venue in ("polymarket", "kalshi"):
            if ids.get(venue):
                body[venue] = str(ids[venue])
        if not body:
            body = {"polymarket": "<Polymarket market id>", "kalshi": "<Kalshi ticker>"}
        body["confirmed"] = True
    elif action == "confirm":
        body = {"confirmed": True}
    elif action == "trade_near":
        body = {"trade_near": True}
    else:
        raise ValueError(f"unknown override action {action!r} (use disable, pin, confirm or trade_near)")
    return f"{json.dumps(eid)}: {json.dumps(body, ensure_ascii=False)}"


# --------------------------------------------------------------------------- manual file

_MANUAL_KEYS = ("exchange_id", "race_key", "party", "match", "probability", "uncertainty", "source", "note", "updated_at")
_MANUAL_INFO_KEYS = ("title", "option", "market_id")  # template columns: shown to the user, not used
_MANUAL_TOP_KEYS = ("version", "values", "updated_at", "note", "comment")


def parse_probability(raw: Any) -> Optional[float]:
    """0..1 floats as is, "55%" -> 0.55, numbers in (1, 100] -> /100; None for empty; ValueError otherwise
    (outside [0, 1] after conversion, or not a number)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("is true/false, not a number")
    percent = False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("%"):
            percent = True
            text = text[:-1].strip()
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{raw!r} is not a number") from None
    elif isinstance(raw, (int, float)):
        value = float(raw)
    else:
        raise ValueError(f"{raw!r} is not a number")
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"{raw!r} is not a number")
    if percent:
        value = value / 100.0
    elif 1.0 < value <= 100.0:
        value = value / 100.0
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{raw!r} is outside 0..1 (or 0%..100%)")
    return value


class ManualFairValues:
    """The user's own fair values (docs/PAPER_TRADING.md §4.3). Re-read when the file's mtime changes;
    never raises (problems go to ``ManualLoad.errors``)."""

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self._clock = clock
        self._lock = threading.Lock()
        self._load: Optional[ManualLoad] = None
        self._signature: Optional[Tuple[str, int, int]] = None
        self._resolve_errors: List[str] = []

    @property
    def path(self) -> Path:
        """``fair_values.json`` when it exists or neither exists, else ``fair_values.csv``."""
        json_path = self.directory / MANUAL_JSON
        csv_path = self.directory / MANUAL_CSV
        if json_path.exists() or not csv_path.exists():
            return json_path
        return csv_path

    def load(self) -> ManualLoad:
        with self._lock:
            try:
                return self._load_locked()
            except Exception as exc:  # never raise: report
                log.warning("manual fair values could not be read: %s", exc)
                path = self.path
                self._load = ManualLoad(path=str(path), exists=path.exists(), loaded_at=self._clock(),
                                        errors=[f"{path.name} could not be read ({type(exc).__name__}): no manual values."])
                self._signature = None
                return self._load

    def _load_locked(self) -> ManualLoad:
        path = self.path
        try:
            st = path.stat()
        except FileNotFoundError:
            if self._load is None or self._load.exists or self._load.path != str(path):
                self._load = ManualLoad(path=str(path), exists=False, loaded_at=self._clock())
                self._signature = None
            return self._load
        except OSError as exc:
            self._load = ManualLoad(path=str(path), exists=True, loaded_at=self._clock(),
                                    errors=[f"{path.name} could not be read ({exc.strerror or type(exc).__name__})."])
            self._signature = None
            return self._load
        signature = (str(path), st.st_mtime_ns, st.st_size)
        if self._load is not None and self._signature == signature:
            return self._load
        load = ManualLoad(path=str(path), exists=True, loaded_at=self._clock(), mtime=st.st_mtime)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            load.errors.append(f"{path.name} could not be read ({type(exc).__name__}): no manual values.")
        else:
            if path.suffix.lower() == ".csv":
                self._parse_csv(text, load)
            else:
                self._parse_json(text, load)
        self._load = load
        self._signature = signature
        return load

    # -- parsing

    def _parse_json(self, text: str, load: ManualLoad) -> None:
        name = Path(load.path).name
        if not text.strip():
            return
        try:
            data = json.loads(text)
        except ValueError as exc:
            load.errors.append(f"{name} is not valid JSON ({exc}): no manual values until it is fixed.")
            return
        if isinstance(data, list):
            values = data
        elif isinstance(data, dict):
            unknown = [k for k in data if k not in _MANUAL_TOP_KEYS]
            if unknown:
                load.errors.append(f"{name}: unknown top-level key(s) {', '.join(repr(k) for k in unknown)} ignored.")
            values = data.get("values")
            if values is None:
                load.errors.append(f"{name} has no \"values\" list: no manual values.")
                return
            if not isinstance(values, list):
                load.errors.append(f"{name}: \"values\" must be a list: no manual values.")
                return
        else:
            load.errors.append(f"{name} must hold an object with a \"values\" list: no manual values.")
            return
        for index, raw in enumerate(values, 1):
            where = f"entry {index}"
            if not isinstance(raw, dict):
                load.errors.append(f"{where}: not an object (skipped).")
                continue
            entry = self._entry(raw, index, where, load.errors)
            if entry is not None:
                load.entries.append(entry)

    def _parse_csv(self, text: str, load: ManualLoad) -> None:
        name = Path(load.path).name
        rows = [(no, line) for no, line in enumerate(text.splitlines(), 1)
                if line.strip() and not line.lstrip().startswith("#")]
        if not rows:
            return
        header_no, header_line = rows[0]
        header = [h.strip().lower() for h in next(csv.reader([header_line]))]
        known = set(_MANUAL_KEYS) | set(_MANUAL_INFO_KEYS)
        unknown = [h for h in header if h and h not in known]
        if unknown:
            load.errors.append(f"{name} line {header_no}: unknown column(s) {', '.join(repr(h) for h in unknown)} ignored.")
        if not any(h in ("exchange_id", "race_key", "match") for h in header) or "probability" not in header:
            load.errors.append(f"{name} line {header_no}: the header needs a probability column and exchange_id, "
                               "race_key or match: no manual values.")
            return
        for no, line in rows[1:]:
            cells = next(csv.reader([line]))
            where = f"line {no}"
            if len(cells) > len(header):
                load.errors.append(f"{where}: more cells than header columns (extra cells ignored).")
            raw = {header[i]: (cells[i].strip() if i < len(cells) else "") for i in range(len(header)) if header[i]}
            raw = {k: (v if v != "" else None) for k, v in raw.items() if k in known}
            entry = self._entry(raw, no, where, load.errors)
            if entry is not None:
                load.entries.append(entry)

    @staticmethod
    def _entry(raw: Mapping[str, Any], line: int, where: str, errors: List[str]) -> Optional[ManualEntry]:
        unknown = [k for k in raw if k not in _MANUAL_KEYS and k not in _MANUAL_INFO_KEYS]
        if unknown:
            errors.append(f"{where}: unknown key(s) {', '.join(repr(k) for k in unknown)} ignored.")
        try:
            probability = parse_probability(raw.get("probability"))
        except ValueError as exc:
            errors.append(f"{where}: probability {exc} (skipped).")
            return None
        if probability is None:
            return ManualEntry(probability=None, line=line)  # a template row
        try:
            uncertainty = parse_probability(raw.get("uncertainty"))
        except ValueError as exc:
            errors.append(f"{where}: uncertainty {exc} (skipped).")
            return None
        eid = raw.get("exchange_id")
        eid_text = _norm_ws(eid) if eid is not None and not isinstance(eid, bool) else ""
        race_key = raw.get("race_key")
        race_text = _norm_ws(race_key).upper() if isinstance(race_key, str) else ""
        match = raw.get("match")
        match_text = str(match).strip() if isinstance(match, str) else ""
        party: Optional[str] = None
        if raw.get("party") is not None and _norm_ws(raw.get("party")):
            party = _party_code(raw.get("party"))
            if party is None:
                errors.append(f"{where}: party {raw.get('party')!r} is not D, R, I, L, G or O (skipped).")
                return None
        if race_text and not eid_text:
            if party is None:
                errors.append(f"{where}: race_key needs a party (skipped).")
                return None
            ref = _race_from_key(race_text, party)
            if ref is None:
                errors.append(f"{where}: race_key {race_key!r} is not like 2026:SENATE:TX or 2026:HOUSE:AZ-01 (skipped).")
                return None
            race_text = ref.race_key
        if not (eid_text or race_text or match_text):
            errors.append(f"{where}: needs exchange_id, race_key + party, or match (skipped).")
            return None
        updated_at: Optional[float] = None
        if raw.get("updated_at") not in (None, ""):
            updated_at = _parse_time(raw.get("updated_at"))
            if updated_at is None:
                errors.append(f"{where}: updated_at {raw.get('updated_at')!r} is not an ISO time or epoch seconds (skipped).")
                return None
        source = _norm_ws(raw.get("source")) or "manual"
        note = raw.get("note")
        return ManualEntry(
            probability=probability, uncertainty=uncertainty, exchange_id=eid_text or None, race_key=race_text or None,
            party=party, match=match_text or None, source=source, note=str(note) if note not in (None, "") else None,
            updated_at=updated_at, line=line,
        )

    # -- resolving

    def resolve(self, targets: Sequence[MatchTarget], now: float) -> Dict[str, FairValue]:
        """exchange_id -> FairValue (source "manual", match_kind "MANUAL"). Precedence per outcome:
        exchange_id > race_key + party > match pattern; a pattern matching several outcomes is an
        error ("ambiguous") and is skipped. Usable when 0 <= p <= 1 and not older than MANUAL_MAX_AGE_S
        (entries without updated_at use the file's mtime)."""
        load = self.load()
        errors: List[str] = []
        try:
            values = self._resolve(load, targets, now, errors)
        except Exception as exc:  # never raise
            log.warning("manual fair values could not be applied: %s", exc)
            errors.append(f"Manual values could not be applied ({type(exc).__name__}).")
            values = {}
        with self._lock:
            self._resolve_errors = errors
        return values

    def _resolve(self, load: ManualLoad, targets: Sequence[MatchTarget], now: float, errors: List[str]) -> Dict[str, FairValue]:
        by_eid = {t.exchange_id: t for t in targets}
        best: Dict[str, Tuple[int, ManualEntry, str]] = {}
        is_csv = load.path.lower().endswith(".csv")
        for entry in load.entries:
            if entry.probability is None:
                continue
            where = f"line {entry.line}" if is_csv else f"entry {entry.line}"
            selected: List[MatchTarget]
            if entry.exchange_id:
                precedence = 3
                target = by_eid.get(entry.exchange_id)
                selected = [target] if target is not None else []
                if not selected:
                    errors.append(f"{where}: exchange id {entry.exchange_id} is not an open Cup outcome (ignored).")
                    continue
            elif entry.race_key:
                precedence = 2
                selected = [t for t in targets if t.race is not None and t.race.race_key == entry.race_key
                            and t.race.party == entry.party]
                if not selected:
                    errors.append(f"{where}: no open outcome is {entry.race_key} party {entry.party} (ignored).")
                    continue
                if len(selected) > 1:
                    errors.append(f"{where}: ambiguous: {entry.race_key} {entry.party} matches {len(selected)} outcomes (skipped).")
                    continue
            else:
                precedence = 1
                matcher = self._matcher(entry.match or "")
                if matcher is None:
                    errors.append(f"{where}: match {entry.match!r} is not a valid regular expression (skipped).")
                    continue
                selected = [t for t in targets if matcher(t)]
                if entry.party:
                    selected = [t for t in selected if t.race is not None and t.race.party == entry.party]
                if len(selected) > 1:
                    errors.append(f"{where}: match {entry.match!r} is ambiguous: matches {len(selected)} outcomes (skipped).")
                    continue
                if not selected:
                    errors.append(f"{where}: match {entry.match!r} matches no open outcome (ignored).")
                    continue
            for target in selected:
                prev = best.get(target.exchange_id)
                if prev is not None and prev[0] == precedence:
                    errors.append(f"{where} overrides {prev[2]} for exchange {target.exchange_id} (the later entry wins).")
                if prev is None or precedence >= prev[0]:
                    best[target.exchange_id] = (precedence, entry, where)
        out: Dict[str, FairValue] = {}
        name = Path(load.path).name
        for eid, (_prec, entry, where) in best.items():
            target = by_eid[eid]
            p = float(entry.probability or 0.0)
            updated = entry.updated_at if entry.updated_at is not None else load.mtime
            unc = entry.uncertainty if entry.uncertainty is not None else MANUAL_UNCERTAINTY
            usable = True
            src = f", source {entry.source!r}" if entry.source and entry.source != "manual" else ""
            reason = f"Your own value in {name} ({where}{src}): {_fmt_p(p)} ± {_fmt_p(unc)}."
            if updated is not None and now - updated > MANUAL_MAX_AGE_S:
                usable = False
                reason = "manual value is older than 72 h; update updated_at"
            quote = FairValueQuote(venue="manual", external_id=f"manual:{entry.line}", label=entry.source or "manual",
                                   raw_value=p, value=p, fetched_at=updated, match_kind="MANUAL", match_confidence=1.0)
            out[eid] = FairValue(
                exchange_id=eid, value=p, source="manual", confidence="medium", usable=usable, reason=reason,
                as_of=updated, sources=[quote], race_key=target.race.race_key if target.race else None,
                party=target.race.party if target.race else None, match_kind="MANUAL", match_confidence=1.0,
                note=entry.note, manual_updated_at=updated, uncertainty=unc,
            )
        return out

    @staticmethod
    def _matcher(pattern: str) -> Optional[Callable[[MatchTarget], bool]]:
        if pattern.startswith("re:"):
            try:
                regex = re.compile(pattern[3:], re.IGNORECASE)
            except re.error:
                return None

            def test(text: str) -> bool:
                return regex.search(text) is not None
        else:
            needle = _norm_ws(pattern).lower()
            if not needle:
                return None

            def test(text: str) -> bool:
                return needle in _norm_ws(text).lower()

        def match(target: MatchTarget) -> bool:
            texts = [target.title or ""]
            option = _norm_ws(target.option)
            if option and option.upper() not in ("YES", "NO"):
                texts += [option, f"{target.title} {option}"]
            return any(test(t) for t in texts)

        return match

    def template(self, targets: Sequence[MatchTarget]) -> Dict[str, Any]:
        """A starter file: ``{"version": 1, "values": [{"exchange_id", "title", "race_key", "party",
        "probability": null, "source": "", "note": "", "updated_at": null}, ...]}``."""
        return {"version": 1, "values": [
            {"exchange_id": t.exchange_id, "title": t.title, "race_key": t.race.race_key if t.race else None,
             "party": t.race.party if t.race else None, "probability": None, "source": "", "note": "", "updated_at": None}
            for t in targets
        ]}

    def status(self) -> Dict[str, Any]:
        """``{"path", "exists", "entries", "errors"}`` of the last load (entries = rows with a probability)."""
        with self._lock:
            load = self._load
            resolve_errors = list(self._resolve_errors)
        if load is None:
            return {"path": str(self.path), "exists": self.path.exists(), "entries": 0, "errors": []}
        return {"path": load.path, "exists": load.exists,
                "entries": sum(1 for e in load.entries if e.probability is not None),
                "errors": list(load.errors) + resolve_errors}


# --------------------------------------------------------------------------- external venues


def venue_value(bid: Optional[float], ask: Optional[float], last: Optional[float]) -> Tuple[Optional[float], List[str]]:
    """(probability, flags) for one venue quote: the mid when ``0 < bid < ask < 1`` and
    ``ask - bid <= MAX_VENUE_SPREAD``; else the last trade when ``0 < last < 1`` (flag
    "last-trade-only"); else None. bid 0 / ask 1 (or missing both) is a "placeholder"."""
    if bid is not None and ask is not None and bid <= 0.0 and ask >= 1.0:
        return None, ["placeholder"]
    if bid is not None and ask is not None and 0.0 < bid < ask < 1.0 and ask - bid <= MAX_VENUE_SPREAD + 1e-9:
        return (bid + ask) / 2.0, []
    flags: List[str] = []
    if bid is not None and ask is not None and ask - bid > MAX_VENUE_SPREAD + 1e-9:
        flags.append("wide")
    if last is not None and 0.0 < last < 1.0:
        return float(last), flags + ["last-trade-only"]
    if bid is None and ask is None:
        return None, ["placeholder"]
    return None, flags


class _StopRefresh(Exception):
    """Ends a provider's refresh at the first failure (offline / error / backoff)."""

    def __init__(self, status: str, message: str, *, retry_after: Optional[float] = None, kind: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.kind = kind  # "proxy" | "connect" | "timeout" | "transport" | "http" | "data"


class _DeadlineReached(Exception):
    """The refresh deadline passed: start no further request."""


class _HttpVenue:
    """Shared plumbing of the outside providers: one anonymous GET-only client (readonly.outside_client),
    a limiter per host that is never the Super Market limiter, the deadline, first-failure stop, Retry-After
    and backoff."""

    name = "venue"
    label = "Venue"

    def __init__(self, *, transport: Any, timeout: float, reads_per_min: int, clock: Callable[[], float],
                 sleep: Callable[[float], None]) -> None:
        self._timeout = float(timeout)
        self._client = outside_client(label=self.label, transport=transport, timeout=timeout)
        self._reads_per_min = max(1, int(reads_per_min))
        self._clock = clock
        self._sleep = sleep
        self._limiters: Dict[str, SlidingWindowLimiter] = {}
        self._failures = 0
        self._next_try_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_discovery_at: Optional[float] = None
        self._matches: Dict[str, VenueMatch] = {}  # exchange_id -> the last validated match
        self._discovered: Dict[str, VenueMatch] = {}  # exchange_id -> a match found by discovery
        self._validated_at: Dict[Tuple[str, str], float] = {}
        self._failed_now: set = set()  # (exchange_id, external id) that failed validation in this refresh
        self._closed = False
        self._state_lock = threading.Lock()
        self.requests_total = 0

    # -- plumbing

    def limiter(self, host: str) -> SlidingWindowLimiter:
        with self._state_lock:
            lim = self._limiters.get(host)
            if lim is None:
                lim = SlidingWindowLimiter(self._reads_per_min, clock=self._clock, sleep=self._sleep)
                self._limiters[host] = lim
            return lim

    def _get(self, url: str, params: Sequence[Tuple[str, Any]], *, deadline: Optional[float], result: Optional[ProviderResult],
             allow_404: bool = False) -> Any:
        if deadline is not None and self._clock() >= deadline:
            raise _DeadlineReached()
        host = httpx.URL(url).host
        limiter = self.limiter(host)
        if deadline is not None and limiter.used >= limiter.limit:
            raise _DeadlineReached()  # waiting for a slot would take up to a minute: past the refresh deadline
        limiter.acquire()
        if deadline is not None and self._clock() >= deadline:  # the limiter waited past the deadline
            raise _DeadlineReached()
        if result is not None:
            result.requests += 1
        self.requests_total += 1
        label = self.label
        try:
            response = self._client.get(url, params=[(k, str(v)) for k, v in params])
        except ReadOnlyViolation:
            raise
        except httpx.ProxyError:
            raise _StopRefresh("offline", f"{label} is unreachable from this machine (connection refused by the network "
                                          f"proxy): no outside fair value from {label}.", kind="proxy") from None
        except httpx.TimeoutException:
            raise _StopRefresh("offline", f"{label} did not answer within {self._timeout:g} s: no outside fair value "
                                          f"from {label}.", kind="timeout") from None
        except httpx.ConnectError:
            raise _StopRefresh("offline", f"{label} is unreachable from this machine (could not connect): no outside "
                                          f"fair value from {label}.", kind="connect") from None
        except httpx.TransportError as exc:
            raise _StopRefresh("offline", f"{label} is unreachable from this machine ({type(exc).__name__}): no outside "
                                          f"fair value from {label}.", kind="transport") from None
        status = response.status_code
        if status == 429:
            wait = _retry_after_s(response.headers.get("Retry-After"), time.time())
            shown = wait if wait is not None else self._backoff_s(self._failures + 1)
            raise _StopRefresh("backoff", f"{label} asked us to slow down (HTTP 429): next try in {shown:.0f} s.",
                               retry_after=wait, kind="http")
        if status == 404 and allow_404:
            return None
        if status >= 500:
            raise _StopRefresh("error", f"{label} answered with a server error (HTTP {status}): no outside fair value "
                                        f"from {label} this time.", kind="http")
        if status != 200:
            raise _StopRefresh("error", f"{label} answered HTTP {status} for {httpx.URL(url).path}: no outside fair "
                                        f"value from {label} this time.", kind="http")
        try:
            return response.json()
        except ValueError:
            raise _StopRefresh("error", f"{label} sent a response that is not JSON: ignored.", kind="data") from None

    @staticmethod
    def _backoff_s(failures: int) -> float:
        return BACKOFF_S[min(max(failures, 1), len(BACKOFF_S)) - 1]

    def _begin(self, now: float) -> Optional[ProviderResult]:
        """A finished result when no request may be sent now (closed, backing off), else None."""
        if self._closed:
            return ProviderResult(venue=self.name, status="disabled", errors=[f"{self.label} provider is closed."])
        if self._next_try_at is not None and now < self._next_try_at:
            msg = f"{self.label} is backing off after an error: next try at {iso_ts(self._next_try_at)}."
            errors = [msg] + ([self._last_error] if self._last_error else [])
            return ProviderResult(venue=self.name, status="backoff", errors=errors[:5], next_try_at=self._next_try_at)
        return None

    def _finish(self, result: ProviderResult, now: float, stop: Optional[_StopRefresh], partial: bool) -> ProviderResult:
        if stop is not None:
            self._failures += 1
            wait = stop.retry_after if stop.retry_after is not None else self._backoff_s(self._failures)
            self._next_try_at = now + wait
            self._last_error = stop.message
            result.status = stop.status
            result.next_try_at = self._next_try_at
            result.errors.insert(0, stop.message)
        else:
            self._failures = 0
            self._next_try_at = None
            if partial:
                result.status = "partial"
                result.errors.insert(0, f"{self.label} refresh stopped at the {REFRESH_DEADLINE_S:g} s deadline: "
                                        "some outcomes were not read this time.")
            self._last_error = result.errors[0] if result.errors else None
        result.errors = result.errors[:5]
        return result

    def _record_match(self, eid: str, match: VenueMatch, now: float) -> VenueMatch:
        key = (eid, match.external_id)
        first = self._validated_at.get(key)
        if first is None or now - first >= DISCOVERY_EVERY_S:
            first = now
            self._validated_at[key] = now
        match.validated_at = first
        self._matches[eid] = match
        return match

    def _forget(self, eid: str) -> None:
        self._matches.pop(eid, None)

    @staticmethod
    def _prefix(source: str) -> str:
        return {"override": "Your pinned", "discovery": "Discovered"}.get(source, "Pinned")

    @staticmethod
    def _confidence(target: MatchTarget, source: str) -> float:
        if source == "override":
            return CONF_OVERRIDE
        if source == "discovery":
            return CONF_DISCOVERY
        return CONF_PIN_NEAR if target.pin_kind == "NEAR" else CONF_PIN_EXACT

    def _wanted(self, targets: Sequence[MatchTarget], venue: str) -> Tuple[Dict[str, Tuple[MatchTarget, str, str]], List[MatchTarget]]:
        """(exchange_id -> (target, external id, source) to read, targets that need discovery)."""
        wanted: Dict[str, Tuple[MatchTarget, str, str]] = {}
        need: List[MatchTarget] = []
        unmatched = known_unmatched_races()
        for t in targets:
            if t.disabled or t.race is None:
                continue
            ext = t.pins.get(venue)
            source = t.pin_sources.get(venue, "pin")
            if ext:
                wanted[t.exchange_id] = (t, ext, "override" if source == "override" else "pin")
                continue
            if source == "override":
                continue  # the user removed this venue's pin: never search for a replacement
            found = self._discovered.get(t.exchange_id)
            if found is not None:
                wanted[t.exchange_id] = (t, found.external_id, "discovery")
            elif t.race.race_key not in unmatched and self._discoverable(t):
                need.append(t)
        return wanted, need

    def _discoverable(self, target: MatchTarget) -> bool:
        return True

    def _discovery_due(self, now: float) -> bool:
        return self._last_discovery_at is None or now - self._last_discovery_at >= DISCOVERY_EVERY_S

    def close(self) -> None:
        self._closed = True
        try:
            self._client.close()
        except Exception:  # pragma: no cover - closing never matters
            pass


class PolymarketProvider(_HttpVenue):
    """Polymarket Gamma reads (docs/PAPER_TRADING.md §4.4). Steady state: ``GET /markets?id=..&limit=n``
    for validated ids; discovery: ``GET /events?tag_slug=midterms&active=true&closed=false&limit=100&offset=k``
    at most every DISCOVERY_EVERY_S, never for known_unmatched_races(). Parses every field variant
    (JSON-encoded strings, numbers as strings). Its httpx.Client comes from readonly.outside_client."""

    name = "polymarket"
    label = "Polymarket"

    def __init__(self, *, base_url: str = POLYMARKET_GAMMA_URL, clob_url: str = POLYMARKET_CLOB_URL,
                 transport: Any = None, timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(transport=transport, timeout=timeout, reads_per_min=reads_per_min, clock=clock, sleep=sleep)
        self.base_url = base_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")

    # -- refresh

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        early = self._begin(now)
        if early is not None:
            return early
        result = ProviderResult(venue=self.name, status="ok")
        stop: Optional[_StopRefresh] = None
        partial = False
        try:
            stop, partial = self._refresh(targets, now, deadline, result)
        except ReadOnlyViolation:
            raise
        except Exception as exc:  # never raise
            log.warning("Polymarket refresh failed: %s", exc, exc_info=True)
            stop = _StopRefresh("error", f"Polymarket data could not be read ({type(exc).__name__}): no outside fair "
                                         "value from Polymarket this time.", kind="data")
        return self._finish(result, now, stop, partial)

    def _refresh(self, targets: Sequence[MatchTarget], now: float, deadline: Optional[float],
                 result: ProviderResult) -> Tuple[Optional[_StopRefresh], bool]:
        self._failed_now = set()
        wanted, need = self._wanted(targets, "polymarket")
        ids: List[str] = []
        for _t, mid, _src in wanted.values():
            if mid not in ids:
                ids.append(mid)
        markets: Dict[str, Tuple[Dict[str, Any], float]] = {}
        read: set = set()
        stop: Optional[_StopRefresh] = None
        partial = False
        try:
            for chunk in _chunks(ids, POLYMARKET_IDS_PER_CALL):
                params = [("id", i) for i in chunk] + [("limit", len(chunk))]
                data = self._get(f"{self.base_url}/markets", params, deadline=deadline, result=result)
                fetched = self._clock()
                if not isinstance(data, list):
                    raise _StopRefresh("error", "Polymarket sent an unexpected response (not a list of markets): ignored.",
                                       kind="data")
                read.update(chunk)
                for m in data:
                    if isinstance(m, dict) and m.get("id") is not None:
                        markets[str(m["id"])] = (m, fetched)
        except _DeadlineReached:
            partial = True
        except _StopRefresh as exc:
            stop = exc
        for eid, (target, mid, source) in wanted.items():
            if mid not in read:
                continue
            got = markets.get(mid)
            prefix = self._prefix(source)
            if got is None:
                result.rejected[eid] = (f"{prefix} Polymarket market {mid} was not returned by Polymarket (removed or a "
                                        "wrong id): dropped.")
                self._drop(eid, target, source, need, mid)
                continue
            market, fetched = got
            ok, reason = self._validate(market, target, source)
            if not ok:
                result.rejected[eid] = reason
                self._drop(eid, target, source, need, mid)
                continue
            self._accept(eid, target, market, source, reason, fetched, now, result)
        if stop is None and not partial and need and self._discovery_due(now):
            try:
                self._discover(need, now, deadline, result)
            except _DeadlineReached:
                partial = True
            except _StopRefresh as exc:
                stop = exc
        return stop, partial

    def _drop(self, eid: str, target: MatchTarget, source: str, need: List[MatchTarget], external_id: str) -> None:
        self._forget(eid)
        self._failed_now.add((eid, str(external_id)))
        if source == "discovery":
            self._discovered.pop(eid, None)
        if source in ("pin", "discovery") and target.race is not None and target.race.race_key not in known_unmatched_races():
            need.append(target)

    def _accept(self, eid: str, target: MatchTarget, market: Mapping[str, Any], source: str, reason: str, fetched: float,
                now: float, result: ProviderResult) -> None:
        mid = str(market.get("id"))
        events = market.get("events") if isinstance(market.get("events"), list) else []
        event_title = _norm_ws(events[0].get("title")) if events and isinstance(events[0], dict) else ""
        item = _norm_ws(market.get("groupItemTitle"))
        label = f"{item} - {event_title}" if item and event_title else (item or _norm_ws(market.get("question")))
        tokens = _json_list(market.get("clobTokenIds")) or []
        match = VenueMatch(venue="polymarket", exchange_id=eid, external_id=mid, label=label, kind=target.pin_kind,
                           confidence=self._confidence(target, source), reason=reason, source=source,
                           token_id=str(tokens[0]) if tokens else None)
        self._record_match(eid, match, now)
        if source == "discovery":
            self._discovered[eid] = match
        result.matches[eid] = _copy_match(match)
        result.quotes[eid] = self._quote(market, match, fetched)

    def _quote(self, market: Mapping[str, Any], match: VenueMatch, fetched: float) -> FairValueQuote:
        bid = _num(market.get("bestBid"))
        ask = _num(market.get("bestAsk"))
        last = _num(market.get("lastTradePrice"))
        if bid is None and ask is None:
            prices = _json_list(market.get("outcomePrices"))
            if prices:
                last = _num(prices[0])
        if market.get("active") is False:
            value, flags = None, ["placeholder"]
        else:
            value, flags = venue_value(bid, ask, last)
        spread = _num(market.get("spread"))
        if spread is None and bid is not None and ask is not None:
            spread = ask - bid
        return FairValueQuote(
            venue="polymarket", external_id=match.external_id, label=match.label, bid=bid, ask=ask, last=last,
            mid=(bid + ask) / 2.0 if bid is not None and ask is not None else None, raw_value=value, value=value,
            spread=spread, fetched_at=fetched, venue_updated_at=_parse_time(market.get("updatedAt")),
            match_kind=match.kind, match_confidence=match.confidence, flags=flags,
        )

    def _validate(self, market: Mapping[str, Any], target: MatchTarget, source: str) -> Tuple[bool, str]:
        race = target.race
        mid = str(market.get("id"))
        prefix = f"{self._prefix(source)} Polymarket market {mid}"
        if race is None:
            return False, f"{prefix} cannot be checked: the outcome has no race: dropped."
        outcomes = _json_list(market.get("outcomes"))
        if outcomes is None or [str(o).strip().lower() for o in outcomes] != ["yes", "no"]:
            return False, f"{prefix} has outcomes {outcomes!r}, not [\"Yes\", \"No\"] (YES must be index 0): dropped."
        if market.get("active") is not True:
            return False, f"{prefix} is not active (a placeholder or a paused market): dropped."
        if market.get("closed") is not False:
            return False, f"{prefix} is closed: dropped."
        end = market.get("endDate") or market.get("endDateIso")
        year = _year_of(end)
        if year != 2026:
            return False, f"{prefix} ends in {year if year is not None else 'an unknown year'}, not 2026: dropped."
        events = market.get("events") if isinstance(market.get("events"), list) else []
        event_title = _norm_ws(events[0].get("title")) if events and isinstance(events[0], dict) else ""
        if event_title and REJECT_EVENT_RE.search(event_title):
            return False, f"{prefix} belongs to '{event_title}', not a winner market: dropped."
        question = _norm_ws(market.get("question"))
        if not (race_named_in(event_title, race) or race_named_in(question, race)):
            named = _describe_named(event_title or question)
            return False, f"{prefix} names {named}, not {_race_phrase(race)}: dropped."
        item = _norm_ws(market.get("groupItemTitle"))
        party = _gamma_label_party(item)
        if party != race.party:
            if party is None:
                return False, (f"{prefix} is '{item}', which carries no party tag, not the {_PARTY_NAMES.get(race.party, race.party)} "
                               "leg: dropped.")
            return False, (f"{prefix} is the {_PARTY_NAMES.get(party, party)} leg ('{item}'), not the "
                           f"{_PARTY_NAMES.get(race.party, race.party)} one: dropped.")
        origin = {"override": "your override in fair_value_map.json", "discovery": "found by discovery"}.get(
            source, "participant map")
        end_text = str(end)[:10]
        return True, (f"{self._prefix(source)} Polymarket market {mid} ({origin}), validated live: active, ends {end_text}, "
                      f"names {_race_phrase(race)}, '{item}' is the {_PARTY_NAMES.get(race.party, race.party)} leg.")

    # -- discovery

    def _event_is_race(self, event: Mapping[str, Any], race: RaceRef) -> bool:
        title = _norm_ws(event.get("title"))
        slug = str(event.get("slug") or "").strip().lower()
        if REJECT_EVENT_RE.search(title):
            return False
        if _year_of(event.get("endDate") or event.get("endDateIso")) != 2026:
            return False
        low = title.lower()
        if race.office == "SENATE":
            return low == f"{STATE_NAMES.get(race.state, '?')} senate election winner".lower()
        if race.office == "GOVERNOR":
            return low == f"{STATE_NAMES.get(race.state, '?')} governor election winner".lower()
        if race.office == "HOUSE":
            return low == f"{race.district} house election winner".lower()
        if race.office == "SENATE_CONTROL":
            return slug == "which-party-will-win-the-senate-in-2026"
        if race.office == "HOUSE_CONTROL":
            return slug == "which-party-will-win-the-house-in-2026"
        return False

    def _discover(self, need: Sequence[MatchTarget], now: float, deadline: Optional[float], result: ProviderResult) -> None:
        events: List[Dict[str, Any]] = []
        for page in range(_POLYMARKET_DISCOVERY_MAX_PAGES):
            params = [("tag_slug", "midterms"), ("active", "true"), ("closed", "false"),
                      ("limit", DISCOVERY_PAGE_LIMIT), ("offset", page * DISCOVERY_PAGE_LIMIT)]
            data = self._get(f"{self.base_url}/events", params, deadline=deadline, result=result)
            if not isinstance(data, list):
                raise _StopRefresh("error", "Polymarket sent an unexpected event list: discovery skipped.", kind="data")
            events.extend(e for e in data if isinstance(e, dict))
            if len(data) < DISCOVERY_PAGE_LIMIT:
                break
        fetched = self._clock()
        self._last_discovery_at = now
        seen: set = set()
        for target in need:
            if target.exchange_id in seen or target.exchange_id in result.matches:
                continue
            seen.add(target.exchange_id)
            race = target.race
            if race is None:
                continue
            candidates = [e for e in events if self._event_is_race(e, race)]
            if not candidates:
                result.rejected.setdefault(target.exchange_id, f"No Polymarket winner event for {_race_phrase(race)} was found "
                                                               "by discovery.")
                continue
            if len(candidates) > 1:
                result.rejected[target.exchange_id] = (f"Discovery found {len(candidates)} Polymarket events for "
                                                       f"{_race_phrase(race)}: ambiguous, not matched.")
                continue
            event = candidates[0]
            markets = [m for m in (event.get("markets") or []) if isinstance(m, dict) and m.get("active") is True
                       and [str(o).strip().lower() for o in (_json_list(m.get("outcomes")) or [])] == ["yes", "no"]
                       and _gamma_label_party(m.get("groupItemTitle")) == race.party]
            if any((target.exchange_id, str(m.get("id"))) in self._failed_now for m in markets):
                continue  # the market that just failed validation for this outcome is never re-accepted by discovery
            if len(markets) != 1:
                what = "no" if not markets else f"{len(markets)}"
                result.rejected[target.exchange_id] = (
                    f"Polymarket's '{_norm_ws(event.get('title'))}' event has {what} active market(s) tagged with the "
                    f"{_PARTY_NAMES.get(race.party, race.party)} party (candidate names only?): not matched.")
                continue
            market = dict(markets[0])
            if not isinstance(market.get("events"), list) or not market.get("events"):
                market["events"] = [{"id": event.get("id"), "title": event.get("title"), "slug": event.get("slug"),
                                     "endDate": event.get("endDate")}]
            ok, reason = self._validate(market, target, "discovery")
            if not ok:
                result.rejected[target.exchange_id] = reason
                continue
            result.rejected.pop(target.exchange_id, None)
            self._accept(target.exchange_id, target, market, "discovery", reason, fetched, now, result)

    # -- history (§4.11)

    def history(self, targets: Sequence[MatchTarget], start: float, end: float, *,
                fidelity_min: int = HISTORY_POLYMARKET_FIDELITY_MIN) -> Tuple[List[FairValueRecord], List[str]]:
        """Optional import (§4.11): ``GET {clob}/prices-history?market=<YES token>&startTs&endTs&fidelity=<min>``
        (windows of at most 14 days) for every validated match; one FairValueRecord per point, source
        "history", ``ts`` = ``as_of`` = the point time + fidelity (the bar END), usable True, confidence
        "medium", detail {"indicative": true} (Polymarket's ``p`` is a mid/last, not executable). Returns
        (records, plain-sentence errors). GET only."""
        records: List[FairValueRecord] = []
        errors: List[str] = []
        try:
            matches = self._history_matches(targets, errors)
            bar_s = float(fidelity_min) * 60.0
            windows = _history_windows(start, end)
            for target in targets:
                match = matches.get(target.exchange_id)
                if match is None or not match.token_id:
                    continue
                usable = match.kind != "NEAR" or target.trade_near
                detail_base = {"indicative": True, "venue": "polymarket", "external_id": match.external_id,
                               "match_kind": match.kind}
                for ws, we in windows:
                    params = [("market", match.token_id), ("startTs", int(ws)), ("endTs", int(we)), ("fidelity", int(fidelity_min))]
                    data = self._get(f"{self.clob_url}/prices-history", params, deadline=None, result=None, allow_404=True)
                    points = data.get("history") if isinstance(data, dict) else None
                    for point in points or []:
                        if not isinstance(point, dict):
                            continue
                        t = _num(point.get("t"))
                        p = _num(point.get("p"))
                        if t is None or p is None or not 0.0 < p < 1.0:
                            continue
                        bar_end = t + bar_s
                        if bar_end > end:
                            continue  # the bar had not ended by the end of the window
                        detail = dict(detail_base)
                        detail["raw_value"] = p
                        records.append(FairValueRecord(
                            ts=bar_end, exchange_id=target.exchange_id, value=p, source="history", confidence="medium",
                            usable=usable, detail=detail, as_of=bar_end, venues=["polymarket"]))
        except ReadOnlyViolation:
            raise
        except _StopRefresh as exc:
            errors.append(exc.message)
        except _DeadlineReached:  # pragma: no cover - history has no deadline
            pass
        except Exception as exc:  # never raise
            log.warning("Polymarket history import failed: %s", exc, exc_info=True)
            errors.append(f"Polymarket price history could not be read ({type(exc).__name__}).")
        return records, errors

    def _history_matches(self, targets: Sequence[MatchTarget], errors: List[str]) -> Dict[str, VenueMatch]:
        wanted = [t for t in targets if not t.disabled and t.race is not None]
        if wanted and not any(t.exchange_id in self._matches for t in wanted):
            res = self.refresh(wanted, self._clock())
            if res.status not in ("ok", "partial") and res.errors:
                errors.append(res.errors[0])
        return {eid: m for eid, m in self._matches.items()}


class KalshiProvider(_HttpVenue):
    """Kalshi v2 public reads (docs/PAPER_TRADING.md §4.5): ``GET /markets?tickers=a,b,..&limit=100`` (<= 100);
    prices are 4-decimal dollar strings (``yes_bid_dollars``), sizes ``*_fp`` strings; integer-cent
    fields are gone. Falls back to the second base URL on connection errors. Its httpx.Client comes from
    readonly.outside_client."""

    name = "kalshi"
    label = "Kalshi"

    def __init__(self, *, base_urls: Sequence[str] = KALSHI_BASE_URLS, transport: Any = None,
                 timeout: float = EXTERNAL_TIMEOUT_S, reads_per_min: int = EXTERNAL_READS_PER_MIN,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(transport=transport, timeout=timeout, reads_per_min=reads_per_min, clock=clock, sleep=sleep)
        self.base_urls = tuple(u.rstrip("/") for u in base_urls) or KALSHI_BASE_URLS
        self._base_index = 0
        self._fell_back = False

    @property
    def base_url(self) -> str:
        return self.base_urls[self._base_index % len(self.base_urls)]

    def _kget(self, path: str, params: Sequence[Tuple[str, Any]], *, deadline: Optional[float],
              result: Optional[ProviderResult], allow_404: bool = False) -> Any:
        try:
            return self._get(f"{self.base_url}{path}", params, deadline=deadline, result=result, allow_404=allow_404)
        except _StopRefresh as exc:
            # DNS / connection errors only (not a proxy refusal or a timeout): try the other host once per refresh
            if exc.kind != "connect" or self._fell_back or len(self.base_urls) < 2:
                raise
            self._fell_back = True
            self._base_index = (self._base_index + 1) % len(self.base_urls)
            return self._get(f"{self.base_url}{path}", params, deadline=deadline, result=result, allow_404=allow_404)

    def _discoverable(self, target: MatchTarget) -> bool:
        # Independent legs name a candidate, not a party: they are pinned by ticker only (never discovered)
        return target.race is not None and target.race.party in ("D", "R")

    # -- refresh

    def refresh(self, targets: Sequence[MatchTarget], now: float, *, deadline: Optional[float] = None) -> ProviderResult:
        early = self._begin(now)
        if early is not None:
            return early
        self._fell_back = False
        result = ProviderResult(venue=self.name, status="ok")
        stop: Optional[_StopRefresh] = None
        partial = False
        try:
            stop, partial = self._refresh(targets, now, deadline, result)
        except ReadOnlyViolation:
            raise
        except Exception as exc:  # never raise
            log.warning("Kalshi refresh failed: %s", exc, exc_info=True)
            stop = _StopRefresh("error", f"Kalshi data could not be read ({type(exc).__name__}): no outside fair value "
                                         "from Kalshi this time.", kind="data")
        return self._finish(result, now, stop, partial)

    def _refresh(self, targets: Sequence[MatchTarget], now: float, deadline: Optional[float],
                 result: ProviderResult) -> Tuple[Optional[_StopRefresh], bool]:
        self._failed_now = set()
        wanted, need = self._wanted(targets, "kalshi")
        tickers: List[str] = []
        for _t, ticker, _src in wanted.values():
            if ticker.upper() not in tickers:
                tickers.append(ticker.upper())
        markets: Dict[str, Tuple[Dict[str, Any], float]] = {}
        read: set = set()
        stop: Optional[_StopRefresh] = None
        partial = False
        try:
            for chunk in _chunks(tickers, KALSHI_TICKERS_PER_CALL):
                params = [("tickers", ",".join(chunk)), ("limit", KALSHI_TICKERS_PER_CALL)]
                data = self._kget("/markets", params, deadline=deadline, result=result)
                fetched = self._clock()
                items = data.get("markets") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise _StopRefresh("error", "Kalshi sent an unexpected response (no markets list): ignored.", kind="data")
                read.update(chunk)
                for m in items:
                    if isinstance(m, dict) and m.get("ticker"):
                        markets[str(m["ticker"]).upper()] = (m, fetched)
        except _DeadlineReached:
            partial = True
        except _StopRefresh as exc:
            stop = exc
        for eid, (target, ticker, source) in wanted.items():
            key = ticker.upper()
            if key not in read:
                continue
            prefix = self._prefix(source)
            got = markets.get(key)
            if got is None:
                result.rejected[eid] = f"{prefix} Kalshi ticker {key} was not returned by Kalshi (removed or a wrong ticker): dropped."
                self._drop(eid, target, source, need, key)
                continue
            market, fetched = got
            ok, reason = self._validate(market, target, source)
            if not ok:
                result.rejected[eid] = reason
                self._drop(eid, target, source, need, key)
                continue
            self._accept(eid, target, market, source, reason, fetched, now, result)
        if stop is None and not partial and need and self._discovery_due(now):
            try:
                self._discover(need, now, deadline, result)
            except _DeadlineReached:
                partial = True
            except _StopRefresh as exc:
                stop = exc
        return stop, partial

    def _drop(self, eid: str, target: MatchTarget, source: str, need: List[MatchTarget], external_id: str) -> None:
        self._forget(eid)
        self._failed_now.add((eid, str(external_id).upper()))
        if source == "discovery":
            self._discovered.pop(eid, None)
        if (source in ("pin", "discovery") and target.race is not None and target.race.race_key not in known_unmatched_races()
                and self._discoverable(target)):
            need.append(target)

    def _accept(self, eid: str, target: MatchTarget, market: Mapping[str, Any], source: str, reason: str, fetched: float,
                now: float, result: ProviderResult) -> None:
        ticker = str(market.get("ticker")).upper()
        who = _norm_ws(market.get("yes_sub_title")) or _norm_ws(market.get("title"))
        label = f"{ticker} - {who}" if who else ticker
        match = VenueMatch(venue="kalshi", exchange_id=eid, external_id=ticker, label=label, kind=target.pin_kind,
                           confidence=self._confidence(target, source), reason=reason, source=source)
        self._record_match(eid, match, now)
        if source == "discovery":
            self._discovered[eid] = match
        result.matches[eid] = _copy_match(match)
        result.quotes[eid] = self._quote(market, match, fetched)

    @staticmethod
    def _dollars(market: Mapping[str, Any], base: str) -> Optional[float]:
        """``<base>_dollars`` (4-decimal dollar strings); the removed integer-cent field only when the dollar one is
        missing and the value looks like dollars (a fraction <= 1, never an integer count of cents)."""
        if f"{base}_dollars" in market and market.get(f"{base}_dollars") is not None:
            return _num(market.get(f"{base}_dollars"))
        legacy = market.get(base)
        if legacy is None or isinstance(legacy, bool):
            return None
        if isinstance(legacy, int) and legacy != 0:
            return None  # integer cents: never read as dollars
        if isinstance(legacy, str) and "." not in legacy and legacy.strip() not in ("0", ""):
            return None
        value = _num(legacy)
        return value if value is not None and 0.0 <= value <= 1.0 else None

    def _quote(self, market: Mapping[str, Any], match: VenueMatch, fetched: float) -> FairValueQuote:
        bid = self._dollars(market, "yes_bid")
        ask = self._dollars(market, "yes_ask")
        last = self._dollars(market, "last_price")
        value, flags = venue_value(bid, ask, last)
        return FairValueQuote(
            venue="kalshi", external_id=match.external_id, label=match.label, bid=bid, ask=ask, last=last,
            mid=(bid + ask) / 2.0 if bid is not None and ask is not None else None, raw_value=value, value=value,
            spread=(ask - bid) if bid is not None and ask is not None else None,
            bid_size=_num(market.get("yes_bid_size_fp")), ask_size=_num(market.get("yes_ask_size_fp")),
            fetched_at=fetched, venue_updated_at=_parse_time(market.get("updated_time")),
            match_kind=match.kind, match_confidence=match.confidence, flags=flags,
        )

    def _validate(self, market: Mapping[str, Any], target: MatchTarget, source: str) -> Tuple[bool, str]:
        race = target.race
        ticker = str(market.get("ticker") or "").upper()
        prefix = f"{self._prefix(source)} Kalshi ticker {ticker}"
        if race is None:
            return False, f"{prefix} cannot be checked: the outcome has no race: dropped."
        status = str(market.get("status") or "").strip().lower()
        if status not in ("active", "open"):
            return False, f"{prefix} is {status or 'of unknown status'}, not open: dropped."
        event = str(market.get("event_ticker") or (ticker.rsplit("-", 1)[0] if "-" in ticker else ticker)).upper()
        if not _KALSHI_YEAR_RE.search(event):
            return False, f"{prefix} is not a 2026 event ({event}): dropped."
        if not kalshi_ticker_matches(ticker, race):
            return False, f"{prefix} is not {_race_phrase(race)} (its event is {event}): dropped."
        rules = _norm_ws(market.get("rules_primary"))
        if race.office in _STATE_OFFICES or race.office == "HOUSE":
            named = _states_named(rules)
            if race.state not in named:
                shown = " and ".join(STATE_NAMES[c] for c in named) or "no state"
                return False, f"{prefix} has rules that name {shown}, not {STATE_NAMES.get(race.state, race.state)}: dropped."
        dem = _KALSHI_DEM_RE.search(rules) is not None
        rep = _KALSHI_REP_RE.search(rules) is not None
        if race.party == "D":
            ok = dem and not rep
        elif race.party == "R":
            ok = rep and not dem
        elif race.party == "I":
            ok = not dem and not rep  # a named candidate; Independent legs are NEAR matches
        else:
            ok = False
        if not ok:
            said = "the Democratic party" if dem and not rep else "the Republican party" if rep and not dem else \
                "no party" if not dem and not rep else "both parties"
            return False, (f"{prefix} has rules that name {said}, not the {_PARTY_NAMES.get(race.party, race.party)} "
                           "party: dropped.")
        origin = {"override": "your override in fair_value_map.json", "discovery": "found by discovery"}.get(
            source, "participant map")
        who = _norm_ws(market.get("yes_sub_title"))
        who_text = f" ({who})" if who else ""
        return True, (f"{self._prefix(source)} Kalshi ticker {ticker} ({origin}), validated live: {status}, event {event}, "
                      f"rules name {STATE_NAMES.get(race.state, 'the chamber') if race.office not in _CONTROL_OFFICES else 'the chamber'} "
                      f"and the {_PARTY_NAMES.get(race.party, race.party)} {'candidate' if race.party == 'I' else 'party'}{who_text}.")

    # -- discovery (optional)

    def _discover(self, need: Sequence[MatchTarget], now: float, deadline: Optional[float], result: ProviderResult) -> None:
        events: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
        cursor: Optional[str] = None
        for _page in range(_KALSHI_DISCOVERY_MAX_PAGES):
            params: List[Tuple[str, Any]] = [("status", "open"), ("with_nested_markets", "true"),
                                             ("limit", _KALSHI_EVENTS_PAGE_LIMIT)]
            if cursor:
                params.append(("cursor", cursor))
            data = self._kget("/events", params, deadline=deadline, result=result)
            if not isinstance(data, dict):
                raise _StopRefresh("error", "Kalshi sent an unexpected event list: discovery skipped.", kind="data")
            evs = [e for e in (data.get("events") or []) if isinstance(e, dict)]
            top = [m for m in (data.get("markets") or []) if isinstance(m, dict)]
            for ev in evs:
                nested = [m for m in (ev.get("markets") or []) if isinstance(m, dict)]
                if not nested:  # without with_nested_markets the markets come at the top level
                    nested = [m for m in top if str(m.get("event_ticker") or "") == str(ev.get("event_ticker") or "")]
                events.append((ev, nested))
            cursor = data.get("cursor") or None
            if not cursor or not evs:
                break
        fetched = self._clock()
        self._last_discovery_at = now
        seen: set = set()
        for target in need:
            if target.exchange_id in seen or target.exchange_id in result.matches or target.race is None:
                continue
            seen.add(target.exchange_id)
            found: List[Tuple[Dict[str, Any], str]] = []
            for ev, markets in events:
                if not _KALSHI_YEAR_RE.search(str(ev.get("event_ticker") or "")):
                    continue
                for m in markets:
                    m2 = dict(m)
                    m2.setdefault("event_ticker", ev.get("event_ticker"))
                    if (target.exchange_id, str(m2.get("ticker") or "").upper()) in self._failed_now:
                        continue  # the ticker that just failed validation for this outcome is never re-accepted
                    ok, reason = self._validate(m2, target, "discovery")
                    if ok:
                        found.append((m2, reason))
            if not found:
                result.rejected.setdefault(target.exchange_id, f"No open Kalshi market for {_race_phrase(target.race)} "
                                                               "was found by discovery.")
                continue
            if len(found) > 1:
                result.rejected[target.exchange_id] = (f"Discovery found {len(found)} Kalshi markets for "
                                                       f"{_race_phrase(target.race)}: ambiguous, not matched.")
                continue
            market, reason = found[0]
            result.rejected.pop(target.exchange_id, None)
            self._accept(target.exchange_id, target, market, "discovery", reason, fetched, now, result)

    # -- history (§4.11)

    def history(self, targets: Sequence[MatchTarget], start: float, end: float, *,
                period_min: int = HISTORY_KALSHI_PERIOD_MIN) -> Tuple[List[FairValueRecord], List[str]]:
        """Optional import (§4.11): ``GET /markets/candlesticks?market_tickers=<<=100>&start_ts&end_ts&period_interval=<min>``;
        one FairValueRecord per candle from its ``yes_bid`` / ``yes_ask`` close (``*_dollars``), ``ts`` = ``as_of``
        = ``end_period_ts`` (a bar is known only when its period ends), source "history", detail
        {"indicative": false, "bid_ask": true}. GET only."""
        records: List[FairValueRecord] = []
        errors: List[str] = []
        self._fell_back = False
        try:
            wanted = [t for t in targets if not t.disabled and t.race is not None]
            if wanted and not any(t.exchange_id in self._matches for t in wanted):
                res = self.refresh(wanted, self._clock())
                if res.status not in ("ok", "partial") and res.errors:
                    errors.append(res.errors[0])
            by_ticker: Dict[str, List[Tuple[MatchTarget, VenueMatch]]] = {}
            for t in targets:
                match = self._matches.get(t.exchange_id)
                if match is not None:
                    by_ticker.setdefault(match.external_id.upper(), []).append((t, match))
            tickers = sorted(by_ticker)
            period_s = float(period_min) * 60.0
            for ws, we in _history_windows(start, end):
                n_candles = max(1, int(math.ceil((we - ws) / period_s)))
                per_call = max(1, min(KALSHI_TICKERS_PER_CALL, _KALSHI_MAX_CANDLES_PER_CALL // n_candles))
                for chunk in _chunks(tickers, per_call):
                    params = [("market_tickers", ",".join(chunk)), ("start_ts", int(ws)), ("end_ts", int(we)),
                              ("period_interval", int(period_min))]
                    data = self._kget("/markets/candlesticks", params, deadline=None, result=None, allow_404=True)
                    entries = data.get("markets") if isinstance(data, dict) else None
                    for entry in entries or []:
                        if not isinstance(entry, dict):
                            continue
                        ticker = str(entry.get("market_ticker") or entry.get("ticker") or "").upper()
                        for target, match in by_ticker.get(ticker, []):
                            records.extend(self._candle_records(target, match, entry.get("candlesticks") or [], end))
        except ReadOnlyViolation:
            raise
        except _StopRefresh as exc:
            errors.append(exc.message)
        except _DeadlineReached:  # pragma: no cover - history has no deadline
            pass
        except Exception as exc:  # never raise
            log.warning("Kalshi history import failed: %s", exc, exc_info=True)
            errors.append(f"Kalshi candlesticks could not be read ({type(exc).__name__}).")
        return records, errors

    @staticmethod
    def _close_of(ohlc: Any) -> Optional[float]:
        if not isinstance(ohlc, dict):
            return None
        if ohlc.get("close_dollars") is not None:
            return _num(ohlc.get("close_dollars"))
        legacy = ohlc.get("close")
        if isinstance(legacy, float) and 0.0 <= legacy <= 1.0:
            return legacy
        if isinstance(legacy, str) and "." in legacy:
            value = _num(legacy)
            return value if value is not None and 0.0 <= value <= 1.0 else None
        return None

    def _candle_records(self, target: MatchTarget, match: VenueMatch, candles: Sequence[Any], end: float) -> List[FairValueRecord]:
        out: List[FairValueRecord] = []
        usable = match.kind != "NEAR" or target.trade_near
        for candle in candles:
            if not isinstance(candle, dict):
                continue
            ts = _num(candle.get("end_period_ts"))
            if ts is None or ts > end:
                continue
            bid = self._close_of(candle.get("yes_bid"))
            ask = self._close_of(candle.get("yes_ask"))
            last = self._close_of(candle.get("price"))
            value, flags = venue_value(bid, ask, last)
            if value is None:
                continue
            detail: Dict[str, Any] = {"indicative": False, "bid_ask": True, "venue": "kalshi",
                                      "external_id": match.external_id, "match_kind": match.kind, "raw_value": value}
            if flags:
                detail["flags"] = flags
            out.append(FairValueRecord(ts=ts, exchange_id=target.exchange_id, value=value, source="history", bid=bid,
                                       ask=ask, confidence="medium", usable=usable, detail=detail, as_of=ts,
                                       venues=["kalshi"]))
        return out


def _history_windows(start: float, end: float) -> List[Tuple[float, float]]:
    span = HISTORY_MAX_DAYS * 86400.0
    out: List[Tuple[float, float]] = []
    ws = float(start)
    while ws < end:
        we = min(float(end), ws + span)
        out.append((ws, we))
        ws = we
    return out


# --------------------------------------------------------------------------- combining


def normalise_race(quotes: Mapping[str, FairValueQuote], races: Mapping[str, RaceRef]) -> Dict[str, FairValueQuote]:
    """Per venue and race: when every QUOTED leg (a leg with a ``raw_value`` after placeholder filtering;
    a dropped placeholder leg is not quoted) has a raw value and their sum is inside RACE_SUM_BAND,
    ``value = raw_value / sum``; otherwise the legs keep ``value = None`` and get the "suspect-sum" flag.
    A race with one quoted leg keeps ``value = raw_value``."""
    out: Dict[str, FairValueQuote] = {}
    groups: Dict[Tuple[str, str], List[str]] = {}
    for eid, quote in quotes.items():
        q = _copy_quote(quote)
        q.value = q.raw_value
        out[eid] = q
        race = races.get(eid)
        if race is not None and q.raw_value is not None:
            groups.setdefault((q.venue, race.race_key), []).append(eid)
    lo, hi = RACE_SUM_BAND
    for legs in groups.values():
        if len(legs) < 2:
            continue
        total = sum(float(out[eid].raw_value or 0.0) for eid in legs)
        if lo <= total <= hi and total > 0:
            for eid in legs:
                out[eid].value = float(out[eid].raw_value or 0.0) / total
        else:
            for eid in legs:
                out[eid].value = None
                if "suspect-sum" not in out[eid].flags:
                    out[eid].flags.append("suspect-sum")
    return out


def renormalise_combined(values: Mapping[str, FairValue], races: Mapping[str, RaceRef]) -> Dict[str, FairValue]:
    """After ``blend`` and ``combine``: for every race whose legs ALL have a value, rescale them to sum to 1
    when the sum is inside RACE_SUM_BAND (the D and R legs may have been blended from different venue
    sets); outside the band every leg of the race becomes unusable ("legs disagree: sum 1.08")."""
    out: Dict[str, FairValue] = {eid: _copy_fv(fv) for eid, fv in values.items()}
    legs_of: Dict[str, List[str]] = {}
    for eid, race in races.items():
        legs_of.setdefault(race.race_key, []).append(eid)
    lo, hi = RACE_SUM_BAND
    for race_key, legs in legs_of.items():
        if len(legs) < 2:
            continue
        if any(eid not in out or out[eid].value is None for eid in legs):
            continue  # a race with a missing leg is left as is
        total = sum(float(out[eid].value or 0.0) for eid in legs)
        manual = [eid for eid in legs if out[eid].source == "manual"]
        others = [eid for eid in legs if out[eid].source != "manual"]
        if lo <= total <= hi:
            if not others:  # every leg is the user's own: rescale them all
                scale_legs, target_sum = legs, 1.0
            else:  # the user's own numbers are kept; the outside legs fill the rest
                scale_legs = others
                target_sum = 1.0 - sum(float(out[eid].value or 0.0) for eid in manual)
            current = sum(float(out[eid].value or 0.0) for eid in scale_legs)
            if current <= 0 or target_sum <= 0:
                continue
            factor = target_sum / current
            for eid in scale_legs:
                out[eid].value = _clip(float(out[eid].value or 0.0) * factor, 0.0, 1.0)
        else:
            reason = f"legs disagree: the race's fair values sum to {total:.2f}"
            for eid in others:
                out[eid].usable = False
                out[eid].reason = reason
            if not others:
                for eid in legs:
                    out[eid].usable = False
                    out[eid].reason = reason
    return out


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
    sources: List[FairValueQuote] = []
    used: List[FairValueQuote] = []
    problems: List[str] = []
    for quote in quotes:
        q = _copy_quote(quote)
        sources.append(q)
        name = _venue_name(q.venue)
        if q.value is None:
            if "placeholder" in q.flags:
                problems.append(f"{name} has no bids or asks on this market (placeholder)")
            elif "suspect-sum" in q.flags:
                problems.append(f"{name}'s legs of this race do not add up to 1 (suspect sum)")
            else:
                problems.append(f"{name} has no usable price (spread over {MAX_VENUE_SPREAD:.2f} and no last trade)")
            continue
        if q.fetched_at is None or now - q.fetched_at > max_age_s:
            if "stale" not in q.flags:
                q.flags.append("stale")
            age = f"{now - q.fetched_at:.0f} s" if q.fetched_at is not None else "of unknown age"
            problems.append(f"{name}'s quote is {age} old (stale after {max_age_s:.0f} s)")
            continue
        if q.match_confidence < MIN_MATCH_CONFIDENCE:
            problems.append(f"{name}'s match confidence {q.match_confidence:.2f} is below {MIN_MATCH_CONFIDENCE:.2f}")
            continue
        used.append(q)
    venues = sorted({q.venue for q in (used or sources)})
    source = venues[0] if len(venues) == 1 else ("blend" if venues else "blend")
    kind = "NEAR" if any(q.match_kind == "NEAR" for q in (used or sources)) else (
        (used or sources)[0].match_kind if (used or sources) else "EXACT")
    race_key = race.race_key if race is not None else None
    party = race.party if race is not None else None
    if not used:
        reason = "No usable outside price: " + "; ".join(problems) + "." if problems else "No outside quote."
        return FairValue(exchange_id=exchange_id, value=None, source=source, confidence="low", usable=False,
                         reason=reason, sources=sources, race_key=race_key, party=party, match_kind=kind,
                         match_confidence=max((q.match_confidence for q in sources), default=0.0))
    spreads = [_eff_spread(q) for q in used]
    if len(used) == 1:
        value = float(used[0].value or 0.0)
        agreement: Optional[float] = None
    else:
        weights = [1.0 / max(s / 2.0, 0.005) ** 2 for s in spreads]
        xs = [_logit(_clip(float(q.value or 0.0), 0.01, 0.99)) for q in used]
        value = _sigmoid(sum(w * x for w, x in zip(weights, xs)) / sum(weights))
        vals = [float(q.value or 0.0) for q in used]
        agreement = max(vals) - min(vals)
    tight_index = min(range(len(used)), key=lambda i: spreads[i])
    tight = used[tight_index]
    if min(spreads) <= HIGH_CONF_SPREAD + 1e-9 and (agreement is None or agreement <= HIGH_CONF_AGREEMENT + 1e-9):
        confidence = "high"
    elif agreement is None or agreement <= MAX_DISAGREEMENT + 1e-9:
        confidence = "medium"
    else:
        confidence = "low"
    uncertainty = uncertainty_of(used, agreement)
    usable = True
    if len(used) == 1:
        q = used[0]
        reason = (f"{_venue_name(q.venue).capitalize() if q.venue == 'demo' else _venue_name(q.venue)} "
                  f"{_fmt_p(q.value)} (spread {_eff_spread(q):.3f}{', last trade only' if 'last-trade-only' in q.flags else ''})"
                  f": fair value {_fmt_p(value)} ± {uncertainty:.3f}.")
    else:
        parts = ", ".join(f"{_venue_name(q.venue)} {_fmt_p(q.value)}" for q in used)
        reason = f"Blend of {parts} (apart by {agreement * 100:.1f} cents): fair value {_fmt_p(value)} ± {uncertainty:.3f}."
    if confidence == "low":
        usable = False
        if len(used) == 2:
            a, b = used[0], used[1]
            reason = (f"{_venue_name(a.venue)} {_fmt_p(a.value)} and {_venue_name(b.venue)} {_fmt_p(b.value)} disagree by "
                      f"{(agreement or 0.0) * 100:.0f} cents: not traded on")
        else:
            parts = ", ".join(f"{_venue_name(q.venue)} {_fmt_p(q.value)}" for q in used)
            reason = f"Venues disagree by {(agreement or 0.0) * 100:.0f} cents ({parts}): not traded on"
    if kind == "NEAR":
        if trade_near:
            if confidence == "high":
                confidence = "medium"
            reason = f"Near match you chose to trade (trade_near): {reason}"
        elif usable:
            usable = False
            reason = "Near match (different settlement wording): shown, not traded"
    suspect = False
    if cup_mid is not None and not confirmed and abs(value - float(cup_mid)) > SUSPECT_GAP:
        suspect = True
        usable = False
        reason = (f"Suspect match: outside {value:.2f} vs Cup {float(cup_mid):.2f}; check the match "
                  "(or confirm it in fair_value_map.json)")
    return FairValue(
        exchange_id=exchange_id, value=value, source=source, confidence=confidence, usable=usable, reason=reason,
        bid=tight.bid, ask=tight.ask, as_of=min(float(q.fetched_at or now) for q in used), agreement=agreement,
        sources=sources, race_key=race_key, party=party, match_kind=kind,
        match_confidence=min(q.match_confidence for q in used), uncertainty=uncertainty, suspect=suspect,
    )


def combine(manual: Mapping[str, FairValue], external: Mapping[str, FairValue]) -> Dict[str, FairValue]:
    """Manual values win over external ones for the same outcome; the external quotes are kept in
    ``sources`` and ``agreement`` is set against them."""
    out: Dict[str, FairValue] = {eid: _copy_fv(fv) for eid, fv in external.items()}
    for eid, mine in manual.items():
        fv = _copy_fv(mine)
        ext = external.get(eid)
        if ext is not None:
            fv.sources = list(fv.sources) + [_copy_quote(q) for q in ext.sources]
            if ext.value is not None and fv.value is not None:
                fv.agreement = abs(float(fv.value) - float(ext.value))
            fv.race_key = fv.race_key or ext.race_key
            fv.party = fv.party or ext.party
        out[eid] = fv
    return out


# --------------------------------------------------------------------------- persistence of matches

_OVERRIDE_KEYS = ("polymarket", "kalshi", "race_key", "party", "disabled", "trade_near", "confirmed", "note")


class MatchMap:
    """``data/<tournament>/fair_value_map.json``: user overrides plus validated automatic matches
    (docs/PAPER_TRADING.md §4.6). Written atomically (temp file + replace). Re-read whenever the file's
    mtime changed (checked at every refresh and every set_targets), so an edit takes effect at the next
    refresh. Saving matches never loses a newer user edit (re-read first, keep ``overrides`` verbatim)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._doc: Dict[str, Any] = {}
        self._overrides: Dict[str, Dict[str, Any]] = {}
        self._errors: List[str] = []
        self._signature: Optional[Tuple[int, int]] = None
        self._loaded = False
        self._loaded_at: Optional[float] = None
        self._exists = False

    def _stat(self) -> Optional[Tuple[int, int]]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> Dict[str, Any]:
        with self._lock:
            self._load_locked()
            return json.loads(json.dumps(self._doc))

    def _load_locked(self) -> None:
        self._signature = self._stat()
        self._loaded = True
        self._loaded_at = time.time()
        self._exists = self._signature is not None
        self._errors = []
        if self._signature is None:
            self._doc, self._overrides = {}, {}
            return
        try:
            text = self.path.read_text(encoding="utf-8-sig")
            doc = json.loads(text) if text.strip() else {}
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._errors.append(f"{self.path.name} could not be read ({type(exc).__name__}: {exc}): treated as empty "
                                "until it is fixed.")
            self._doc, self._overrides = {}, {}
            return
        if not isinstance(doc, dict):
            self._errors.append(f"{self.path.name} must hold a JSON object: treated as empty.")
            self._doc, self._overrides = {}, {}
            return
        self._doc = doc
        self._overrides = self._clean_overrides(doc.get("overrides"))

    def _clean_overrides(self, raw: Any) -> Dict[str, Dict[str, Any]]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self._errors.append("\"overrides\" must be an object keyed by exchange id: ignored.")
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for eid, body in raw.items():
            key = str(eid).strip()
            if not isinstance(body, dict):
                self._errors.append(f"override {key}: must be an object (ignored).")
                continue
            clean: Dict[str, Any] = {}
            for name, value in body.items():
                if name not in _OVERRIDE_KEYS:
                    self._errors.append(f"override {key}: unknown key {name!r} ignored (allowed: {', '.join(_OVERRIDE_KEYS)}).")
                    continue
                if name in ("polymarket", "kalshi"):
                    if value is None or (isinstance(value, (str, int)) and not isinstance(value, bool)):
                        clean[name] = None if value is None or str(value).strip() == "" else str(value).strip()
                    else:
                        self._errors.append(f"override {key}: {name} must be an id string or null (ignored).")
                elif name in ("disabled", "trade_near", "confirmed"):
                    if isinstance(value, bool):
                        clean[name] = value
                    else:
                        self._errors.append(f"override {key}: {name} must be true or false (ignored).")
                elif name == "party":
                    code = _party_code(value) if isinstance(value, str) else None
                    if code is None:
                        self._errors.append(f"override {key}: party {value!r} is not D, R, I, L, G or O (ignored).")
                    else:
                        clean[name] = code
                elif name == "race_key":
                    ref = _race_from_key(value, "O") if isinstance(value, str) else None
                    if ref is None:
                        self._errors.append(f"override {key}: race_key {value!r} is not like 2026:SENATE:NH (ignored).")
                    else:
                        clean[name] = ref.race_key
                else:  # note
                    clean[name] = value if isinstance(value, str) else str(value)
            out[key] = clean
        return out

    def reload_if_changed(self) -> bool:
        """Re-read when the mtime differs from the last load; True when the overrides changed."""
        with self._lock:
            if self._loaded and self._stat() == self._signature:
                return False
            before = json.dumps(self._overrides, sort_keys=True)
            self._load_locked()
            return json.dumps(self._overrides, sort_keys=True) != before

    def overrides(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if not self._loaded:
                self._load_locked()
            return {k: dict(v) for k, v in self._overrides.items()}

    def status(self) -> Dict[str, Any]:
        """``{"path", "exists", "overrides": int, "loaded_at", "errors": [str]}`` (unknown keys, bad JSON)."""
        with self._lock:
            return {"path": str(self.path), "exists": self._exists if self._loaded else self.path.exists(),
                    "overrides": len(self._overrides), "loaded_at": self._loaded_at, "errors": list(self._errors)}

    def save_matches(self, matches: Iterable[VenueMatch], now: float) -> None:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for m in matches:
            row = m.to_dict()
            row.pop("exchange_id", None)
            if row.get("token_id") is None:
                row.pop("token_id", None)
            grouped.setdefault(str(m.exchange_id), []).append(row)
        with self._lock:
            doc: Dict[str, Any] = {}
            if self.path.exists():
                try:
                    text = self.path.read_text(encoding="utf-8-sig")
                    doc = json.loads(text) if text.strip() else {}
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    # never overwrite a file the user is editing (a syntax error must not cost them their edit)
                    msg = f"{self.path.name} is not valid JSON, so validated matches were not saved ({type(exc).__name__})."
                    if msg not in self._errors:
                        self._errors.append(msg)
                    return
                if not isinstance(doc, dict):
                    return
            out: Dict[str, Any] = {"version": doc.get("version", 1), "updated_at": iso_ts(now),
                                   "overrides": doc.get("overrides", {}) if doc.get("overrides") is not None else {},
                                   "matches": {eid: grouped[eid] for eid in sorted(grouped, key=_eid_sort_key)}}
            for key, value in doc.items():
                if key not in out:
                    out[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".fair_value_map.", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(out, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            # force the next reload_if_changed() to re-read: a user edit made between our read and our write
            # is then picked up (and our own write alone changes nothing: the overrides are the same)
            self._signature = None
            self._exists = True


def _eid_sort_key(eid: str) -> Tuple[int, Any]:
    return (0, int(eid)) if eid.isdigit() else (1, eid)


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
        if mode not in FV_MODES:
            raise ValueError(f"fair-value mode must be one of {', '.join(FV_MODES)}, not {mode!r}")
        self.directory = Path(directory)
        self.mode = mode
        self.enabled = mode != "off"
        self._providers: List[FairValueProvider] = list(providers) if mode == "auto" else []
        self._manual = manual if manual is not None else ManualFairValues(self.directory, clock=clock)
        self._map = MatchMap(self.directory / MAP_FILE)
        self._recorder = recorder
        self._refresh_recorder = refresh_recorder
        self._clock = clock
        self._cup_mids = cup_mids
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._infos: List[ExchangeInfo] = []
        self._races: Dict[str, RaceRef] = {}
        self._targets: List[MatchTarget] = []
        self._snapshot = FairValueSnapshot(at=None, mode=mode, enabled=self.enabled)
        self._last_refresh_at: Optional[float] = None
        self._prev: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self._recorded: Dict[str, Tuple[float, Tuple[Any, ...]]] = {}
        self._known_matches: Dict[Tuple[str, str], VenueMatch] = {}
        self._rejections: Dict[str, Dict[str, str]] = {}  # eid -> venue -> reason
        self._provider_errors: Dict[str, Optional[str]] = {}
        self._saved_signature: Optional[frozenset] = None
        self._pstatus: Dict[str, Dict[str, Any]] = {
            self._pname(p): {"status": "pending", "last_ok_at": None, "last_error": None, "requests": 0, "matched": 0,
                             "quoted": 0, "next_try_at": None}
            for p in self._providers
        }

    @staticmethod
    def _pname(provider: Any) -> str:
        return str(getattr(provider, "name", type(provider).__name__))

    @property
    def providers(self) -> List[FairValueProvider]:
        return list(self._providers)

    @property
    def map(self) -> MatchMap:
        return self._map

    @property
    def manual(self) -> ManualFairValues:
        return self._manual

    # -- targets

    def set_targets(self, infos: Sequence[ExchangeInfo]) -> None:
        """Called by the tracker after each market-list refresh (open outcomes only), never while holding
        the tracker's lock."""
        infos = list(infos)
        overrides: Dict[str, Dict[str, Any]] = {}
        if self.enabled:
            try:
                self._map.reload_if_changed()
                overrides = self._map.overrides()
            except Exception as exc:  # never let a bad map file break the tracker
                log.warning("fair_value_map.json could not be read: %s", exc)
        races = races_for(infos, overrides)
        targets = build_targets(infos, races, overrides)
        with self._lock:
            mids = {t.exchange_id: t.cup_mid for t in self._targets}
            for t in targets:
                t.cup_mid = mids.get(t.exchange_id)
            self._infos = infos
            self._races = races
            self._targets = targets
            snap = dataclasses.replace(self._snapshot, races=dict(races))
            self._snapshot = snap

    def _rebuild_locked(self) -> None:
        overrides = self._map.overrides()
        races = races_for(self._infos, overrides)
        targets = build_targets(self._infos, races, overrides)
        mids = {t.exchange_id: t.cup_mid for t in self._targets}
        for t in targets:
            t.cup_mid = mids.get(t.exchange_id)
        self._races = races
        self._targets = targets

    # -- refresh

    def refresh(self, now: Optional[float] = None) -> FairValueSnapshot:
        with self._refresh_lock:
            when = float(self._clock() if now is None else now)
            try:
                self._refresh(when)
            except ReadOnlyViolation:
                raise
            except Exception as exc:  # never raise
                log.warning("fair value refresh failed: %s", exc, exc_info=True)
                with self._lock:
                    errors = list(self._snapshot.errors) + [f"The fair-value refresh failed ({type(exc).__name__}: {exc}); "
                                                            "the previous values are kept."]
                    self._snapshot = dataclasses.replace(self._snapshot, errors=errors[-5:])
            return self.current()

    def _safe_cup_mids(self) -> Dict[str, float]:
        if self._cup_mids is None:
            return {}
        try:
            raw = self._cup_mids() or {}
        except Exception as exc:
            log.warning("cup_mids failed: %s", exc)
            return {}
        out: Dict[str, float] = {}
        for eid, mid in dict(raw).items():
            value = _num(mid)
            if value is not None:
                out[str(eid)] = value
        return out

    def _call_provider(self, provider: Any, targets: Sequence[MatchTarget], now: float) -> ProviderResult:
        name = self._pname(provider)
        try:
            res = provider.refresh(targets, now, deadline=now + REFRESH_DEADLINE_S)
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            log.warning("fair-value provider %s failed: %s", name, exc, exc_info=True)
            return ProviderResult(venue=name, status="error",
                                  errors=[f"The {name} provider failed ({type(exc).__name__}): no outside fair value from it."])
        if not isinstance(res, ProviderResult):
            return ProviderResult(venue=name, status="error", errors=[f"The {name} provider returned no result."])
        return res

    @staticmethod
    def _ensure_raw(quote: FairValueQuote) -> FairValueQuote:
        q = _copy_quote(quote)
        if q.raw_value is None and "placeholder" not in q.flags:
            if q.bid is not None or q.ask is not None or q.last is not None:
                value, flags = venue_value(q.bid, q.ask, q.last)
                q.raw_value = value
                for f in flags:
                    if f not in q.flags:
                        q.flags.append(f)
            elif q.value is not None:
                q.raw_value = q.value
        if q.mid is None and q.bid is not None and q.ask is not None:
            q.mid = (q.bid + q.ask) / 2.0
        if q.spread is None and q.bid is not None and q.ask is not None:
            q.spread = q.ask - q.bid
        return q

    def _refresh(self, now: float) -> None:
        if not self.enabled:
            with self._lock:
                self._snapshot = FairValueSnapshot(at=now, mode=self.mode, enabled=False, races=dict(self._races))
                self._last_refresh_at = now
            return
        errors: List[str] = []
        try:
            if self._map.reload_if_changed():
                with self._lock:
                    self._rebuild_locked()
        except Exception as exc:
            errors.append(f"{MAP_FILE} could not be read ({type(exc).__name__}).")
        cup = self._safe_cup_mids()  # never under our lock (it reads the tracker's published snapshot)
        with self._lock:
            targets = [_copy_target(t) for t in self._targets]
            races = dict(self._races)
        for t in targets:
            if t.exchange_id in cup:
                t.cup_mid = cup[t.exchange_id]
        ids = {t.exchange_id for t in targets}
        manual_values = {eid: fv for eid, fv in self._manual.resolve(targets, now).items() if eid in ids}
        results: List[Tuple[str, ProviderResult]] = []
        if self.mode == "auto":
            active = [t for t in targets if not t.disabled]
            for provider in self._providers:
                results.append((self._pname(provider), self._call_provider(provider, active, now)))
        per_eid: Dict[str, List[FairValueQuote]] = {}
        for _name, res in results:
            prepared = {eid: self._ensure_raw(q) for eid, q in res.quotes.items() if eid in ids}
            for eid, q in normalise_race(prepared, races).items():
                per_eid.setdefault(eid, []).append(q)
        external: Dict[str, FairValue] = {}
        for t in targets:
            quotes = per_eid.get(t.exchange_id, [])
            if t.disabled:
                external[t.exchange_id] = self._empty(t, "You switched off this outcome's outside fair value in "
                                                         f"{MAP_FILE} (disabled).")
            elif quotes:
                external[t.exchange_id] = blend(t.exchange_id, quotes, now, race=t.race, cup_mid=t.cup_mid,
                                                trade_near=t.trade_near, confirmed=t.confirmed)
            else:
                external[t.exchange_id] = self._empty(t, self._no_value_reason(t, results))
        combined = renormalise_combined(combine(manual_values, external), races)
        with self._lock:
            prev = dict(self._prev)
        new_prev: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        for eid, fv in combined.items():
            before = prev.get(eid)
            if before is not None and before[0] is not None:
                fv.prev_value, fv.prev_as_of = before
            new_prev[eid] = (fv.value, fv.as_of)
        # matches: keep the last validated ones across failed refreshes; drop only on an explicit rejection
        with self._lock:
            known = dict(self._known_matches)
            rejections = {eid: dict(v) for eid, v in self._rejections.items()}
        fresh: List[VenueMatch] = []
        for name, res in results:
            for eid, reason in res.rejected.items():
                known.pop((eid, res.venue or name), None)
                rejections.setdefault(eid, {})[res.venue or name] = reason
            for eid, match in res.matches.items():
                known[(eid, match.venue)] = _copy_match(match)
                rejections.get(eid, {}).pop(match.venue, None)
                fresh.append(match)
            if res.status in ("ok", "partial"):
                for eid, q in res.quotes.items():
                    if (eid, q.venue) not in known and eid in ids:  # e.g. the demo feed: no explicit match objects
                        known[(eid, q.venue)] = VenueMatch(
                            venue=q.venue, exchange_id=eid, external_id=q.external_id, label=q.label, kind=q.match_kind,
                            confidence=q.match_confidence, reason=f"Quoted by {_venue_name(q.venue)}.",
                            source="demo" if q.venue == "demo" else "pin", validated_at=q.fetched_at)
        for eid in [e for e in rejections if not rejections[e]]:
            rejections.pop(eid)
        known = {k: v for k, v in known.items() if k[0] in ids}
        self._update_provider_status(results, now)
        self._record(combined, now, errors)
        self._record_refresh(results, now, errors)
        self._save_matches(fresh, results, now, errors)
        manual_status = self._manual.status()
        map_status = self._map.status()
        with self._lock:
            pstatus = {k: dict(v) for k, v in self._pstatus.items()}
            self._prev = new_prev
            self._known_matches = known
            self._rejections = rejections
            self._last_refresh_at = now
            cups = {t.exchange_id: t.cup_mid for t in targets}
            for t in self._targets:
                if t.exchange_id in cups:
                    t.cup_mid = cups[t.exchange_id]
            self._snapshot = FairValueSnapshot(
                at=now, mode=self.mode, enabled=True, values=combined, races=races, providers=pstatus,
                manual=manual_status, errors=errors[:5], map=map_status)

    def _empty(self, target: MatchTarget, reason: str) -> FairValue:
        if self.mode == "manual":
            source = "manual"
        elif len(self._providers) == 1:
            name = self._pname(self._providers[0])
            source = name if name in ("polymarket", "kalshi", "demo") else "blend"
        else:
            source = "blend"
        race = target.race
        return FairValue(exchange_id=target.exchange_id, value=None, source=source, confidence="low", usable=False,
                         reason=reason, race_key=race.race_key if race else None, party=race.party if race else None,
                         match_kind=target.pin_kind)

    def _no_value_reason(self, target: MatchTarget, results: Sequence[Tuple[str, ProviderResult]]) -> str:
        race = target.race
        if self.mode == "manual":
            return (f"Manual mode: outside prices are not read; add this outcome to {MANUAL_JSON} to give it a fair "
                    "value.")
        if not self._providers:
            return "No outside price source is configured; only fair_values.json is used."
        if race is None:
            return ("The title is not an election race the matcher understands: no outside market (add it to "
                    f"{MANUAL_JSON}, or set race_key and party in {MAP_FILE}).")
        if race.race_key in known_unmatched_races():
            return f"No outside market prices this race by party ({_race_label(race)})."
        parts: List[str] = []
        for name, res in results:
            why = res.rejected.get(target.exchange_id)
            if why:
                parts.append(why)
            elif res.status in ("offline", "backoff", "error", "disabled") and res.errors:
                parts.append(res.errors[0])
            elif res.status == "partial" and target.pins.get(res.venue or name):
                parts.append(f"{_venue_name(res.venue or name)} was not read for this outcome this time (the "
                             f"{REFRESH_DEADLINE_S:g} s refresh deadline passed).")
            elif target.pins.get(res.venue or name) is None and res.venue in ("polymarket", "kalshi"):
                parts.append(f"No {_venue_name(res.venue)} market is matched to this outcome.")
        unique: List[str] = []
        for p in parts:
            if p not in unique:
                unique.append(p)
        return " ".join(unique) if unique else "No outside market is matched to this outcome."

    def _update_provider_status(self, results: Sequence[Tuple[str, ProviderResult]], now: float) -> None:
        with self._lock:
            for name, res in results:
                st = self._pstatus.setdefault(name, {"status": "pending", "last_ok_at": None, "last_error": None,
                                                     "requests": 0, "matched": 0, "quoted": 0, "next_try_at": None})
                st["status"] = res.status
                st["requests"] = int(res.requests)
                st["matched"] = len(res.matches) if res.matches else sum(
                    1 for q in res.quotes.values() if q is not None)
                st["quoted"] = sum(1 for q in res.quotes.values()
                                   if q is not None and (q.raw_value is not None or q.value is not None
                                                         or (q.bid is not None and q.ask is not None)))
                st["next_try_at"] = res.next_try_at
                if res.status in ("ok", "partial"):
                    st["last_ok_at"] = now
                if res.errors:
                    st["last_error"] = res.errors[0]
                elif res.status == "ok":
                    st["last_error"] = None

    @staticmethod
    def _used_venues(fv: FairValue) -> List[str]:
        if fv.source == "manual":
            return ["manual"]
        venues: List[str] = []
        for q in fv.sources:
            if q.value is None or "stale" in q.flags or q.match_confidence < MIN_MATCH_CONFIDENCE or q.venue == "manual":
                continue
            if q.venue not in venues:
                venues.append(q.venue)
        return sorted(venues)

    def _record(self, values: Mapping[str, FairValue], now: float, errors: List[str]) -> None:
        if self._recorder is None:
            return
        records: List[FairValueRecord] = []
        states: Dict[str, Tuple[float, Tuple[Any, ...]]] = {}
        with self._lock:
            recorded = dict(self._recorded)
        for eid in sorted(values, key=_eid_sort_key):
            fv = values[eid]
            state = (fv.value, fv.bid, fv.ask, fv.usable, fv.confidence, fv.suspect)
            last = recorded.get(eid)
            if last is not None and not _record_changed(last[1], state) and now - last[0] < FV_RECORD_HEARTBEAT_S:
                continue
            venues = self._used_venues(fv)
            records.append(FairValueRecord(
                ts=now, exchange_id=eid, value=fv.value, source=fv.source, bid=fv.bid, ask=fv.ask,
                confidence=fv.confidence, usable=fv.usable, agreement=fv.agreement,
                detail={"as_of": fv.as_of, "venues": list(venues), "match_kind": fv.match_kind,
                        "match_confidence": fv.match_confidence, "uncertainty": fv.uncertainty,
                        "prev_value": fv.prev_value, "suspect": fv.suspect},
                as_of=fv.as_of, venues=list(venues)))
            states[eid] = (now, state)
        if not records:
            return
        try:
            self._recorder(records)
        except Exception as exc:
            log.warning("fair-value recorder failed: %s", exc)
            errors.append(f"Fair values could not be recorded ({type(exc).__name__}): replays will miss this refresh.")
            return
        with self._lock:
            self._recorded.update(states)

    def _record_refresh(self, results: Sequence[Tuple[str, ProviderResult]], now: float, errors: List[str]) -> None:
        if self._refresh_recorder is None:
            return
        venues: Dict[str, Dict[str, Any]] = {}
        for name, res in results:
            times = [float(q.fetched_at) for q in res.quotes.values() if q is not None and q.fetched_at is not None]
            fetched = min(times) if times else (now if res.status == "ok" else None)
            venues[res.venue or name] = {"status": res.status, "fetched_at": fetched}
        manual = self._manual.status()
        if manual.get("exists"):
            venues["manual"] = {"status": "ok" if not manual.get("errors") else "error", "fetched_at": now}
        try:
            self._refresh_recorder(FairValueRefresh(ts=now, venues=venues))
        except Exception as exc:
            log.warning("fair-value refresh recorder failed: %s", exc)
            errors.append(f"The fair-value refresh could not be recorded ({type(exc).__name__}).")

    def _save_matches(self, fresh: Sequence[VenueMatch], results: Sequence[Tuple[str, ProviderResult]], now: float,
                      errors: List[str]) -> None:
        if not any(res.matches or res.rejected for _n, res in results):
            return
        with self._lock:
            known = [m for (eid, venue), m in self._known_matches.items()]
        current: Dict[Tuple[str, str], VenueMatch] = {(m.exchange_id, m.venue): m for m in known}
        for _name, res in results:
            for eid in res.rejected:
                current.pop((eid, res.venue), None)
        for m in fresh:
            current[(m.exchange_id, m.venue)] = m
        signature = frozenset((k[0], k[1], m.external_id, m.kind, m.source, round(m.confidence, 3))
                              for k, m in current.items())
        if signature == self._saved_signature:
            return
        try:
            self._map.save_matches(sorted(current.values(), key=lambda m: (_eid_sort_key(m.exchange_id), m.venue)), now)
            self._saved_signature = signature
        except Exception as exc:
            log.warning("fair_value_map.json could not be written: %s", exc)
            errors.append(f"{MAP_FILE} could not be written ({type(exc).__name__}).")

    # -- readers

    def current(self) -> FairValueSnapshot:
        """The latest snapshot (a copy; thread-safe). Before the first refresh: empty values, races set."""
        with self._lock:
            snap = self._snapshot
            return FairValueSnapshot(
                at=snap.at, mode=snap.mode, enabled=snap.enabled,
                values={eid: _copy_fv(fv) for eid, fv in snap.values.items()}, races=dict(snap.races),
                providers={k: dict(v) for k, v in snap.providers.items()},
                manual=json.loads(json.dumps(snap.manual)) if snap.manual is not None else None,
                errors=list(snap.errors), map=json.loads(json.dumps(snap.map)) if snap.map is not None else None)

    def races(self) -> Dict[str, RaceRef]:
        with self._lock:
            return dict(self._races)

    def targets(self) -> List[MatchTarget]:
        with self._lock:
            return [_copy_target(t) for t in self._targets]

    def matches(self) -> List[MatchRow]:
        with self._lock:
            targets = [_copy_target(t) for t in self._targets]
            known = dict(self._known_matches)
            rejections = {eid: dict(v) for eid, v in self._rejections.items()}
            values = self._snapshot.values
            fairs = {eid: _copy_fv(fv) for eid, fv in values.items()}
            refreshed = self._last_refresh_at is not None
        rows: List[MatchRow] = []
        for t in targets:
            ms = [_copy_match(m) for (eid, _venue), m in sorted(known.items()) if eid == t.exchange_id]
            reason: Optional[str] = None
            if not ms:
                reason = self._unmatched_reason(t, rejections.get(t.exchange_id, {}), refreshed)
            rows.append(MatchRow(
                exchange_id=t.exchange_id, market_id=t.market_id, title=t.title, option=t.option,
                race_key=t.race.race_key if t.race else None, party=t.race.party if t.race else None,
                matches=ms, fair=fairs.get(t.exchange_id), unmatched_reason=reason))
        return rows

    def _unmatched_reason(self, target: MatchTarget, rejected: Mapping[str, str], refreshed: bool) -> str:
        if not self.enabled:
            return "Fair values are off."
        if target.disabled:
            return f"You switched off this outcome's outside fair value in {MAP_FILE} (disabled)."
        if self.mode == "manual":
            return "Manual mode: outside markets are not read."
        if not self._providers:
            return "No outside price source is configured."
        race = target.race
        if race is None:
            return "The title is not an election race the matcher understands."
        if race.race_key in known_unmatched_races():
            return f"No outside market prices this race by party ({_race_label(race)})."
        if rejected:
            return " ".join(rejected[v] for v in sorted(rejected))
        if not refreshed:
            return "Not refreshed yet."
        with self._lock:
            errs = [st.get("last_error") for st in self._pstatus.values() if st.get("status") not in ("ok", "partial")]
        errs = [e for e in errs if e]
        if errs:
            return " ".join(errs)
        return "No outside market is matched to this outcome."

    def status(self) -> Dict[str, Any]:
        """``{"mode", "enabled", "last_refresh_at", "usable", "total", "providers": {...}, "manual": {...},
        "map": MatchMap.status()}``."""
        with self._lock:
            values = self._snapshot.values
            usable = sum(1 for fv in values.values() if fv.usable and fv.value is not None)
            total = len(self._targets)
            providers = {k: dict(v) for k, v in self._pstatus.items()}
            last = self._last_refresh_at
        out: Dict[str, Any] = {"mode": self.mode, "enabled": self.enabled, "last_refresh_at": last, "usable": usable,
                               "total": total, "providers": providers, "manual": None, "map": None}
        if self.enabled:
            out["manual"] = self._manual.status()
            out["map"] = self._map.status()
        return out

    def import_history(self, start: float, end: float, recorder: Optional[Recorder] = None) -> Dict[str, Any]:
        """Convenience for the CLI: :func:`import_history` over this service's providers, targets and races."""
        return import_history(self._providers, self.targets(), self.races(), start, end, recorder or self._recorder or (lambda _r: None))

    def close(self) -> None:
        for provider in self._providers:
            try:
                provider.close()
            except Exception:  # pragma: no cover - closing never matters
                pass


def _record_changed(old: Tuple[Any, ...], new: Tuple[Any, ...]) -> bool:
    for a, b in zip(old[:3], new[:3]):
        if (a is None) != (b is None):
            return True
        if a is not None and b is not None and abs(float(a) - float(b)) >= FV_RECORD_MIN_CHANGE - 1e-9:
            return True
    return tuple(old[3:]) != tuple(new[3:])


def default_providers(mode: str, *, transport: Any = None) -> List[FairValueProvider]:
    """``[PolymarketProvider(), KalshiProvider()]`` for mode "auto", else []."""
    if mode != "auto":
        return []
    return [PolymarketProvider(transport=transport), KalshiProvider(transport=transport)]


def _history_bucket_s(record: FairValueRecord) -> float:
    venue = (record.venues or [""])[0]
    if venue == "polymarket":
        return HISTORY_POLYMARKET_FIDELITY_MIN * 60.0
    if venue == "kalshi":
        return HISTORY_KALSHI_PERIOD_MIN * 60.0
    return 300.0


def _normalise_history(records: Sequence[FairValueRecord], races: Mapping[str, RaceRef]) -> List[FairValueRecord]:
    """Per venue, race and bar: the legs' values rescaled to sum to 1 (inside RACE_SUM_BAND), else unusable."""
    groups: Dict[Tuple[str, str, int], List[FairValueRecord]] = {}
    for rec in records:
        race = races.get(rec.exchange_id)
        if race is None or rec.value is None:
            continue
        venue = (rec.venues or [""])[0]
        groups.setdefault((venue, race.race_key, int(rec.ts // _history_bucket_s(rec))), []).append(rec)
    lo, hi = RACE_SUM_BAND
    for legs in groups.values():
        by_eid: Dict[str, FairValueRecord] = {}
        for rec in legs:
            by_eid[rec.exchange_id] = rec  # one value per leg and bar (the latest)
        if len(by_eid) < 2:
            continue
        total = sum(float(r.value or 0.0) for r in by_eid.values())
        for rec in legs:
            raw = float(rec.value or 0.0)
            rec.detail.setdefault("raw_value", raw)
            if lo <= total <= hi and total > 0:
                rec.value = raw / total
            else:
                rec.value = None
                rec.usable = False
                rec.detail["flags"] = list(rec.detail.get("flags") or []) + ["suspect-sum"]
    return list(records)


def import_history(providers: Sequence[Any], targets: Sequence[MatchTarget], races: Mapping[str, RaceRef],
                   start: float, end: float, recorder: Recorder) -> Dict[str, Any]:
    """The optional, GET-only outside-history import (§4.11, CLI ``fairvalue --import-history``): for every
    provider with a ``history`` method, fetch [start, end] (clipped to HISTORY_MAX_DAYS), normalise per race
    and venue at each timestamp like ``normalise_race``, and pass the records to ``recorder``. Returns
    ``{"records": int, "by_venue": {venue: int}, "errors": [str], "start", "end"}``. Never raises."""
    end = float(end)
    start = max(float(start), end - HISTORY_MAX_DAYS * 86400.0)
    out: Dict[str, Any] = {"records": 0, "by_venue": {}, "errors": [], "start": start, "end": end}
    if start >= end:
        out["errors"].append("The history window is empty: nothing imported.")
        return out
    live = [t for t in targets if not t.disabled]
    # process whole races together (normalisation needs every leg), in chunks that bound memory
    by_race: Dict[str, List[MatchTarget]] = {}
    loose: List[MatchTarget] = []
    for t in live:
        race = races.get(t.exchange_id) or t.race
        if race is None:
            loose.append(t)
        else:
            by_race.setdefault(race.race_key, []).append(t)
    chunks: List[List[MatchTarget]] = []
    current: List[MatchTarget] = []
    for key in sorted(by_race):
        legs = by_race[key]
        if current and len(current) + len(legs) > 50:
            chunks.append(current)
            current = []
        current.extend(legs)
    if current:
        chunks.append(current)
    for provider in providers:
        fetch = getattr(provider, "history", None)
        if fetch is None:
            continue
        venue = str(getattr(provider, "name", type(provider).__name__))
        count = 0
        for chunk in chunks:
            try:
                records, errors = fetch(chunk, start, end)
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                errors, records = [f"The {venue} history import failed ({type(exc).__name__})."], []
            for e in errors:
                if e not in out["errors"]:
                    out["errors"].append(e)
            if records:
                records = _normalise_history(records, races)
                try:
                    recorder(records)
                except Exception as exc:
                    out["errors"].append(f"Imported {venue} history could not be stored ({type(exc).__name__}).")
                    break
                count += len(records)
            if errors and not records:
                break  # offline: one plain sentence, no further requests to this venue
        out["by_venue"][venue] = count
        out["records"] += count
    return out
