"""Simulated Super Market tournament for ``dashboard --demo`` and offline tests.

See docs/DESIGN.md (Demo section).

:class:`DemoMarket` answers the REST endpoints the bot reads through an
``httpx.MockTransport``, so the real :class:`~supermarket_bot.client.SuperMarketClient`,
:class:`~supermarket_bot.bot.MarketDataBot` and the tracker run unmodified and offline.
Response shapes follow ``docs/supermarket-openapi.json``.

Price model
-----------
Every price is a pure function of ``(seed, exchange, time)``, so any historical query
agrees with later live queries and with the trade tape:

* A stationary, mean-reverting walk: seeded value noise (smoothly interpolated random
  values on 2 d / 6 h / 45 min / 5 min lattices) plus per-minute jitter, sampled on a
  1-minute lattice and scaled by the outcome's liquidity class. Its amplitude is bounded,
  so background noise never crosses the surge thresholds.
* Multi-outcome markets normalise their outcomes to a fixed sum (0.95 for the Arizona
  market, which leaves its asks below 1: a flagged arbitrage).
* Scripted event curves: piecewise-linear offsets relative to the demo start ``t0``.

Books are centred on that fair value on the 0.005 tick, with depth decaying away from
the touch. Trades are drawn per (exchange, UTC hour) from a seeded RNG (background flow
plus scripted event trades) and print at the ask (YES taker) or the bid (NO taker).
Candles aggregate those trades and ``latestPrice`` is the last one.

Scripted events (``t0`` = demo start)
-------------------------------------
* (a) news surge: Pennsylvania Senate +0.14 from ``t0 - 50m`` on many small trades; it
  holds. :class:`DemoNewsProvider` has matching headlines published from ``t0 - 55m``.
* (b) participant spike: Ohio House District 9 +0.18 at ``t0 - 20m`` from two large YES
  trades on a thin book, no news; about 60% reverts over ``[t0, t0 + 40m]``.
* (c) four outcomes whose favourite hovers at 0.96-0.985 (one of them a NO favourite);
  two settle before the Cup end (2026-11-04 17:00 UTC), two after.
* (d) "Who will win the Arizona Governor race?": three outcomes whose asks sum below 1.
* (e) one ALL constraint violation (the Senate-control outcomes sum to 1.03).
* (f) a live participant-style surge on the Michigan Senate market at ``t0 + 90s``.

Paper-trading scenarios (docs/PAPER_TRADING.md §7.6; markets 316-326, exchanges 9024-9034, titles in the
real Cup grammar so the race matcher is exercised):

* (g) a NO basket on the Nevada Governor pair: the two YES bids sum to 1.05 over ``[t0 + 4m, t0 + 30m]``,
  and from ``t0 + 45m`` the YES asks sum to 1.00 (the set can be sold at a profit);
* (h) a value winner on the Texas Senate pair: an outside fair value of 0.58 while the Cup trades at 0.50,
  converging linearly to 0.575 over ``[t0 + 10m, t0 + 3h]``;
* (i) a value loser on the Iowa Senate pair: the outside price says 0.46 but the Cup drifts from 0.38 to
  0.33, and at ``t0 + 90m`` the outside price drops to 0.30 (it was wrong);
* (j) a 3-leg NO basket on the Nebraska Senate (D, R and Independent legs): bids sum above 1.03 over
  ``[t0 + 60m, t0 + 80m]``;
* (k) a liquidity hole on the Wyoming Governor: 12 sell prints walk the bid from 0.925 down to 0.62 within
  20 seconds at ``t0 + 20m``; the price is back at 0.93 by ``t0 + 26m``;
* (l) a market that settles live: the Maine Senate debate market rises to 0.97 and settles YES at
  ``t0 + 40m`` (it then leaves the open list and the bulk prices).

:class:`DemoFairValueProvider` serves the scripted outside prices (and, for the liquid Michigan, Georgia,
North Carolina and Maine Senate markets, an outside price that LEADS the Cup by 15 minutes, D37).
:class:`SimClock` and :func:`no_wait` make fast simulated runs deterministic (D42).

Read-only: order endpoints are not simulated (they return 404 like any unknown route).
"""

from __future__ import annotations

import base64
import bisect
import binascii
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from .books import parse_time
from .bot import Context
from .client import SuperMarketClient
from .models import Article, FairValueQuote
from .news import NewsProvider

log = logging.getLogger("supermarket_bot")

DEMO_SLUG = "predictions-cup-demo"
DEMO_TOURNAMENT_ID = "2026c0de-0000-4000-8000-00000000cafe"
DEMO_TOURNAMENT_NAME = "Predictions Cup — Midterm Elections (demo)"
DEMO_BASE_URL = "https://supermarket-demo.invalid/api/v1"
DEMO_API_KEY = "demo_offline_key"
DEMO_CURRENCY = "SUSQies"
DEMO_INITIAL_BALANCE = 100000
CUP_START = "2026-10-01T04:00:00.000Z"
CUP_END = "2026-11-04T17:00:00.000Z"
ELECTION_NIGHT = "2026-11-04T04:59:00.000Z"  # after election night, before the Cup end
LATE_SETTLEMENT = "2026-11-30T23:59:00.000Z"  # after the Cup end

TICK = 0.005
HISTORY_DAYS = 8  # trading history generated before t0 (covers the 7-day backfill)
MIN = 60.0
HOUR = 3600.0
DAY = 86400.0

_API_PREFIX = "/api/v1"
_ME_ID = "6d656d6f-0000-4000-8000-000000000001"
_CREATOR = {"id": "5e1ec7ed-0000-4000-8000-0000000000aa", "username": "Predictions Cup Markets"}
_SEQ_BASE = 7_300_000
_ID_HOUR_OFFSET = 1_000_000
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_RESOLUTIONS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400, "1w": 604800}
_MARKET_SORTS = ("recent", "trending", "closing", "oldest")
_MARKET_STATUSES = ("open", "closed", "settled", "resolved", "any")
_LEADERBOARD_SORTS = ("pnl", "roi", "winRate", "volume", "trades")
_PNL_PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365, "all": None}

# value-noise octaves: (lattice spacing in seconds, amplitude at full volatility)
_OCTAVES = ((2 * DAY, 0.030), (6 * HOUR, 0.015), (45 * MIN, 0.008), (5 * MIN, 0.004))
_JITTER = 0.002
_BLOCK_CACHE_MAX = 60000

# Fast simulated runs (``paper --demo --fast``, ``backtest --demo``, tests) start at this fixed time, so two
# runs give identical results whatever the wall clock says (D42): 2026-10-05T14:00:00Z.
SIM_T0 = 1_791_208_800.0

# --------------------------------------------------------------------------- published demo constants (D61)
# Package F hard-codes these in its e2e checks; tests/test_demo.py asserts them.
PAPER_MARKET_IDS: Tuple[str, ...] = tuple(str(316 + i) for i in range(11))  # 316-326
PAPER_EXCHANGE_IDS: Tuple[str, ...] = tuple(str(9024 + i) for i in range(11))  # 9024-9034
DEMO_TOP_MEMBERS: Tuple[Tuple[str, float], ...] = (
    ("election_whale", 185_000.0), ("polls_gambler", 142_000.0), ("longshot_lucy", 121_500.0),
)
DEMO_LEADER_VALUES: Tuple[float, ...] = (285_000.0, 242_000.0, 221_500.0)  # initial 100,000 + pnl
DEMO_BAR = 221_500.0  # the top-3 bar
DEMO_CHASER_M = 2.215  # bar / 100,000: the chaser runs in "chase" mode
DEMO_PORTFOLIO_IDS: Tuple[str, ...] = (
    "human:conservative", "policy:conservative", "policy:chaser", "kind:value", "kind:basket", "kind:hole",
    "kind:fade", "kind:carry", "kind:arbitrage",
)
DEMO_HEADLINE = "human:conservative"
FEED_LEAD_S = 15 * 60.0  # the leading outside feed is the Cup's own mid this much later (D37)
FEED_LEADING_KEYS = ("mi-senate", "ga-senate", "nc-senate", "me-senate")  # exchanges 9003-9006
DEMO_FEED_HALF_SPREAD = 0.005  # demo quotes: feed -/+ this (their uncertainty)


# --------------------------------------------------------------------------- helpers

_MASK = (1 << 64) - 1


def _mix64(x: int) -> int:
    """splitmix64 finaliser: a fast, platform-independent integer hash."""
    x = (x + 0x9E3779B97F4A7C15) & _MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK
    return x ^ (x >> 31)


def _hash(*parts: int) -> int:
    h = 0x5DEECE66D
    for part in parts:
        h = _mix64(h ^ (int(part) & _MASK))
    return h


def _unit(*parts: int) -> float:
    """Deterministic uniform number in [0, 1) for the given integer key."""
    return (_hash(*parts) >> 11) * (1.0 / 9007199254740992.0)


def _tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 3)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(*parts: int) -> str:
    text = "%016x%016x" % (_hash(*parts), _hash(*(tuple(parts) + (1,))))
    return f"{text[:8]}-{text[8:12]}-4{text[13:16]}-8{text[17:20]}-{text[20:32]}"


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _log_uniform(u: float, lo: float, hi: float) -> int:
    return max(1, int(round(math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo))))))


def _diurnal(ts: float) -> float:
    """Trading-activity multiplier: busiest mid-afternoon US Eastern, quietest overnight."""
    hour = (ts % DAY) / HOUR
    return 0.675 + 0.325 * math.cos(2 * math.pi * (hour - 18.0) / 24.0)


def _cursor(kind: str, value: int) -> str:
    return base64.urlsafe_b64encode(f"{kind}:{value}".encode()).decode()


def _read_cursor(raw: Optional[str], kind: str) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        text = base64.urlsafe_b64decode(raw.encode() + b"=" * (-len(raw) % 4)).decode()
        prefix, _, value = text.partition(":")
        if prefix != kind:
            raise ValueError(prefix)
        return int(value)
    except (ValueError, UnicodeError, binascii.Error):
        raise _Fail(400, "INVALID_CURSOR", "Invalid or expired pagination cursor") from None


class SimClockStall(RuntimeError):
    """A fast simulation tried to wait for a rate limiter (its budget is too small for the per-step demand)."""


class SimClock:
    """A thread-safe fake clock for fast, deterministic simulations (D42): a callable returning the
    simulated time, moved only by :meth:`advance` and :meth:`set`."""

    def __init__(self, start: float) -> None:
        self._now = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    @property
    def now(self) -> float:
        return self()

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now += float(seconds)
            return self._now

    def set(self, ts: float) -> None:
        with self._lock:
            self._now = float(ts)


def no_wait(seconds: float) -> None:
    """The ``sleep`` of every limiter in a fast simulation: waiting would make results depend on CPU speed,
    so any positive wait raises :class:`SimClockStall` instead."""
    if seconds > 0:
        raise SimClockStall(f"A fast simulation tried to wait {seconds:.1f} s for a rate limiter: raise its demo budget")


class _Fail(Exception):
    """An API error to return in the ``{"error": {...}}`` envelope."""

    def __init__(self, status: int, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def response(self) -> httpx.Response:
        err: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        return httpx.Response(self.status, json={"error": err})


def _invalid(name: str, message: str) -> _Fail:
    return _Fail(400, "VALIDATION_ERROR", f"{name}: {message}", {"fieldErrors": {name: [message]}})


def _int_param(params: Mapping[str, str], name: str, default: int, lo: int, hi: Optional[int] = None) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _invalid(name, "must be an integer") from None
    if value < lo or (hi is not None and value > hi):
        raise _invalid(name, f"must be between {lo} and {hi}" if hi is not None else f"must be at least {lo}")
    return value


def _enum_param(params: Mapping[str, str], name: str, allowed: Sequence[str], default: Optional[str]) -> Optional[str]:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    if raw not in allowed:
        raise _invalid(name, "must be one of " + ", ".join(allowed))
    return raw


def _time_param(params: Mapping[str, str], name: str) -> Optional[float]:
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    parsed = parse_time(raw)
    if parsed is None:
        raise _invalid(name, "must be an ISO-8601 timestamp")
    return parsed.timestamp()


def _bool_param(params: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _enum_param(params, name, ("true", "false"), None)
    return default if raw is None else raw == "true"


# --------------------------------------------------------------------------- scenario data


@dataclass(frozen=True)
class _Liquidity:
    vol: float  # noise scale (1.0 = liquid binary near 0.5)
    spread_ticks: int  # even, so the mid sits on the tick
    touch_qty: float  # resting shares at the best level
    decay: float  # quantity decay per level away from the touch
    gap: float  # chance a non-touch level is empty
    rate: float  # background trades per hour (before the diurnal multiplier)
    size: Tuple[float, float]  # log-uniform background trade size range


_LIQUIDITY = {
    "liquid": _Liquidity(1.0, 2, 420.0, 0.88, 0.0, 14.0, (10, 150)),
    "medium": _Liquidity(0.8, 4, 180.0, 0.85, 0.1, 6.0, (5, 100)),
    "thin": _Liquidity(0.5, 6, 30.0, 0.85, 0.45, 1.5, (5, 40)),
    "band": _Liquidity(0.4, 2, 650.0, 0.9, 0.0, 3.0, (20, 300)),
    "multi": _Liquidity(0.6, 2, 220.0, 0.86, 0.05, 5.0, (10, 120)),
    "deep": _Liquidity(0.5, 2, 900.0, 0.92, 0.0, 10.0, (20, 200)),
}

# (key, title, liquidity, settlement date, [(option, base price)], options)
_MARKET_SPECS: List[Tuple[str, str, str, str, List[Tuple[str, float]], Dict[str, Any]]] = [
    ("pa-senate", "Will Republicans keep control of the Pennsylvania Senate?", "liquid", ELECTION_NIGHT,
     [("YES", 0.52)], {"office": "State Senate", "jurisdiction": "Pennsylvania", "party": "Republican"}),
    ("oh-house-09", "Will Republicans win Ohio House District 9?", "thin", ELECTION_NIGHT,
     [("YES", 0.40)], {"office": "U.S. House", "jurisdiction": "Ohio", "district": 9, "party": "Republican"}),
    ("mi-senate", "Will Democrats win the Michigan Senate race?", "liquid", ELECTION_NIGHT,
     [("YES", 0.55)], {"office": "U.S. Senate", "jurisdiction": "Michigan", "party": "Democratic"}),
    ("ga-senate", "Will Democrats win the Georgia Senate race?", "liquid", ELECTION_NIGHT,
     [("YES", 0.61)], {"office": "U.S. Senate", "jurisdiction": "Georgia", "party": "Democratic"}),
    ("nc-senate", "Will Republicans win the North Carolina Senate race?", "medium", ELECTION_NIGHT,
     [("YES", 0.47)], {"office": "U.S. Senate", "jurisdiction": "North Carolina", "party": "Republican"}),
    ("me-senate", "Will Democrats win the Maine Senate race?", "medium", ELECTION_NIGHT,
     [("YES", 0.44)], {"office": "U.S. Senate", "jurisdiction": "Maine", "party": "Democratic"}),
    ("ca-governor", "Will Democrats win the California Governor race?", "band", ELECTION_NIGHT,
     [("YES", 0.975)], {"office": "Governor", "jurisdiction": "California", "party": "Democratic"}),
    ("wy-senate", "Will Republicans win the Wyoming Senate race?", "band", ELECTION_NIGHT,
     [("YES", 0.97)], {"office": "U.S. Senate", "jurisdiction": "Wyoming", "party": "Republican"}),
    ("ny-governor", "Will Democrats win the New York Governor race?", "band", LATE_SETTLEMENT,
     [("YES", 0.965)], {"office": "Governor", "jurisdiction": "New York", "party": "Democratic",
                        "note": "Settles on state certification."}),
    ("libertarian-house", "Will a Libertarian candidate win a U.S. House seat?", "band", LATE_SETTLEMENT,
     [("YES", 0.025)], {"office": "U.S. House", "jurisdiction": "United States", "party": "Libertarian",
                        "note": "Settles on state certification."}),
    ("az-governor", "Who will win the Arizona Governor race?", "multi", ELECTION_NIGHT,
     [("Democratic nominee", 0.52), ("Republican nominee", 0.40), ("Any other candidate", 0.03)],
     {"office": "Governor", "jurisdiction": "Arizona", "target_sum": 0.95}),
    ("house-control", "Which party will control the House after the midterms?", "multi", ELECTION_NIGHT,
     [("Democrats", 0.68), ("Republicans", 0.32)], {"office": "U.S. House", "jurisdiction": "United States", "target_sum": 1.0}),
    ("senate-control", "Which party will control the Senate after the midterms?", "multi", ELECTION_NIGHT,
     [("Republicans", 0.71), ("Democrats", 0.32)],
     {"office": "U.S. Senate", "jurisdiction": "United States", "target_sum": 1.03}),
    ("house-seats", "How many House seats will Democrats win?", "multi", LATE_SETTLEMENT,
     [("Fewer than 205", 0.08), ("205-217", 0.22), ("218-229", 0.38), ("230-241", 0.24), ("242 or more", 0.08)],
     {"office": "U.S. House", "jurisdiction": "United States", "target_sum": 1.0, "vol": 0.7,
      "note": "Settles on state certification."}),
    ("ga-debate", "Will the Georgia Senate candidates hold a televised debate?", "medium", "",
     [("YES", 0.80)], {"office": "U.S. Senate", "jurisdiction": "Georgia", "settled": "YES"}),
    # ---- paper-trading scenarios (g)-(l), appended so the ids above never change (316+ / 9024+) ----
    ("nv-gov-d", "Will the Democratic Party win the Nevada Governor?", "medium", CUP_END,
     [("YES", 0.50)], {"office": "Governor", "jurisdiction": "Nevada", "party": "Democratic", "vol": 0.15}),
    ("nv-gov-r", "Will the Republican Party win the Nevada Governor?", "medium", CUP_END,
     [("YES", 0.50)], {"office": "Governor", "jurisdiction": "Nevada", "party": "Republican", "mirror": "nv-gov-d"}),
    ("tx-sen-d", "Will the Democratic Party win the Texas Senate?", "liquid", CUP_END,
     [("YES", 0.50)], {"office": "U.S. Senate", "jurisdiction": "Texas", "party": "Democratic", "vol": 0.10}),
    ("tx-sen-r", "Will the Republican Party win the Texas Senate?", "liquid", CUP_END,
     [("YES", 0.50)], {"office": "U.S. Senate", "jurisdiction": "Texas", "party": "Republican", "mirror": "tx-sen-d"}),
    ("ia-sen-d", "Will the Democratic Party win the Iowa Senate?", "medium", CUP_END,
     [("YES", 0.38)], {"office": "U.S. Senate", "jurisdiction": "Iowa", "party": "Democratic", "vol": 0.10}),
    ("ia-sen-r", "Will the Republican Party win the Iowa Senate?", "medium", CUP_END,
     [("YES", 0.62)], {"office": "U.S. Senate", "jurisdiction": "Iowa", "party": "Republican", "mirror": "ia-sen-d"}),
    ("ne-sen-d", "Will the Democratic Party win the Nebraska Senate?", "thin", CUP_END,
     [("YES", 0.03)], {"office": "U.S. Senate", "jurisdiction": "Nebraska", "party": "Democratic", "vol": 0.0}),
    ("ne-sen-r", "Will the Republican Party win the Nebraska Senate?", "medium", CUP_END,
     [("YES", 0.66)], {"office": "U.S. Senate", "jurisdiction": "Nebraska", "party": "Republican", "vol": 0.0}),
    ("ne-sen-i", "Will the Independent Party win the Nebraska Senate?", "medium", CUP_END,
     [("YES", 0.31)], {"office": "U.S. Senate", "jurisdiction": "Nebraska", "party": "Independent", "vol": 0.0}),
    ("wy-gov-r", "Will the Republican Party win the Wyoming Governor?", "deep", CUP_END,
     [("YES", 0.93)], {"office": "Governor", "jurisdiction": "Wyoming", "party": "Republican", "vol": 0.30}),
    ("me-debate", "Will the Maine Senate candidates debate before October 7?", "medium", "",
     [("YES", 0.85)], {"office": "U.S. Senate", "jurisdiction": "Maine", "vol": 0.10, "settles_live": 40 * MIN,
                       "settled": "YES"}),
]

# Members of the demo leaderboard besides the demo user (deterministic per seed).
_HANDLES = (
    "keystone_quant", "bellwether_bets", "swingstate_sam", "median_voter", "polls_and_priors",
    "turnout_tina", "brier_score", "ev_maximizer", "the_fade_desk", "carry_trade_carl",
    "precinct_pete", "margin_of_error", "overround_olga", "kelly_criterion", "split_ticket",
    "mail_ballot_mo", "late_swing", "base_rate_bea", "district_drift", "calibrated_cal",
    "the_closer", "tick_size_tom", "spread_hunter", "mean_reverter", "news_desk_nina",
    "quant_on_the_hill", "crosstabs", "likely_voter", "two_sided_tess", "fair_value_fred",
    "sigma_sally", "momentum_mike", "contrarian_cara", "no_side_nate", "yes_side_yuki",
    "vol_crusher", "arb_alex", "liquidity_lou", "settlement_sue", "electoral_ed",
    "battleground_bo", "redshift_rita", "blue_wall_ben", "toss_up_tara", "poll_skeptic",
    "q4_quinn", "last_look_lee",
)
_LEADERBOARD_FACTORS = {"1d": (-0.04, 0.09), "7d": (0.15, 0.45), "30d": (0.85, 1.0), "quarter": (1.0, 1.0), "all": (1.0, 1.0)}

# (market key, option index, YES shares, average cost, days before t0 the lot was opened)
_POSITIONS = (("ga-senate", 0, 800, 0.575, 3.2), ("ca-governor", 0, 2000, 0.962, 2.1), ("az-governor", 0, 600, 0.47, 1.4))
_REALIZED_PNL = 1184.75

# Canned news. Times are seconds relative to t0. Stories match a query that contains
# every term in ``match``; unmatched queries get the unrelated noise headlines.
_STORIES: List[Tuple[Tuple[str, ...], List[Tuple[float, str, str, str, str]]]] = [
    (("pennsylvania",), [
        (-55 * MIN, "Republicans' hold on the Pennsylvania Senate firms up as Democratic recruit quits key race",
         "Keystone Ledger (demo)", "pennsylvania-senate-democratic-recruit-quits",
         "A Democratic challenger in a top Pennsylvania Senate battleground withdrew this morning, "
         "leaving Republicans a clearer path to keep control of the chamber."),
        (-52 * MIN, "Pennsylvania Senate: surprise withdrawal leaves Republicans favored to keep majority",
         "Capitol Wire (demo)", "pennsylvania-senate-withdrawal-republicans-favored",
         "Party officials have days to name a replacement; strategists say Republicans now hold the edge "
         "in the fight for the Pennsylvania Senate."),
        (-46 * MIN, "Forecasters move the Pennsylvania Senate toward Republicans after candidate exit",
         "Demo Election Desk", "forecasters-shift-pennsylvania-senate",
         "Two forecasters shifted their Pennsylvania Senate ratings toward Republicans within an hour of "
         "the announcement."),
        (-31 * MIN, "What the withdrawal means for Republicans and the Pennsylvania Senate map",
         "Harrisburg Daily (demo)", "what-withdrawal-means-pennsylvania-senate",
         "An explainer on the seats that decide control of the Pennsylvania Senate and why Republicans "
         "now need fewer of them."),
    ]),
    (("california",), [
        (-2.5 * DAY, "Democrats keep a commanding lead in the California Governor race, polls show",
         "Golden State Report (demo)", "california-governor-poll-democrats-lead",
         "Polling averages give Democrats a lead of more than 20 points in the California Governor race."),
    ]),
    (("arizona",), [
        (-3 * DAY, "Arizona Governor race tightens slightly in new survey",
         "Desert Dispatch (demo)", "arizona-governor-race-tightens",
         "A new survey shows the Arizona Governor race within the margin of error among likely voters."),
    ]),
    (("georgia",), [
        (-30 * HOUR, "Georgia Senate race: Democrats outraise rivals in third quarter",
         "Peach State Politics (demo)", "georgia-senate-fundraising-q3",
         "Third-quarter filings show the Democratic campaign in the Georgia Senate race with a cash edge."),
    ]),
]
_NOISE_NEWS: List[Tuple[float, str, str, str, str]] = [
    (-3 * HOUR, "Early voting turnout climbs in several battleground states", "Demo Wire", "early-voting-turnout-climbs",
     "Election officials report brisk early voting, with in-person turnout ahead of the last midterm."),
    (-9 * HOUR, "Campaign finance filings show record small-dollar donations", "Demo Wire", "record-small-dollar-donations",
     "Small-dollar giving hit a record in the latest reporting period, according to new filings."),
    (-27 * HOUR, "Election officials prepare for record mail-ballot volume", "Civic Times (demo)", "record-mail-ballot-volume",
     "County clerks are adding staff and drop boxes ahead of an expected surge in mailed ballots."),
]


@dataclass
class _Trade:
    ts: float
    price: float
    size: int
    side: str
    tid: int

    def to_json(self) -> Dict[str, Any]:
        notional = self.size * (self.price if self.side == "YES" else 1.0 - self.price)
        return {
            "id": str(self.tid),
            "createdAt": _iso(self.ts),
            "price": self.price,
            "size": self.size,
            "side": self.side,
            "volume": round(notional, 6),
        }


@dataclass
class _Exchange:
    index: int
    id: str
    slot: int
    option: str
    base: float
    amp: float  # noise amplitude multiplier
    liq: _Liquidity
    market: "_Market" = field(repr=False)
    yes_bias: float = 0.0
    # The other leg of a two-party race: this outcome's unscripted fair value is 1 - the mirror's (so the
    # pair sums to 1 outside its scripted windows).
    mirror: Optional["_Exchange"] = field(default=None, repr=False)
    knot_times: List[float] = field(default_factory=list)
    knot_values: List[float] = field(default_factory=list)
    scripted: List[Tuple[float, str, int]] = field(default_factory=list)
    quiet: List[Tuple[float, float]] = field(default_factory=list)
    initial_price: float = 0.5

    def offset(self, ts: float) -> float:
        """Scripted price offset: piecewise linear, right-continuous at steps, 0 before the first knot."""
        times = self.knot_times
        if not times or ts < times[0]:
            return 0.0
        i = bisect.bisect_right(times, ts) - 1
        if i + 1 >= len(times):
            return self.knot_values[i]
        t1, t2 = times[i], times[i + 1]
        v1, v2 = self.knot_values[i], self.knot_values[i + 1]
        if t2 <= t1:
            return v2
        return v1 + (v2 - v1) * (ts - t1) / (t2 - t1)

    def set_knots(self, knots: Sequence[Tuple[float, float]]) -> None:
        self.knot_times = [k[0] for k in knots]
        self.knot_values = [k[1] for k in knots]

    def is_quiet(self, ts: float) -> bool:
        return any(lo <= ts < hi for lo, hi in self.quiet)


@dataclass
class _Market:
    index: int
    id: str
    key: str
    title: str
    settlement_date: Optional[str]
    created_at: float
    details: Dict[str, Any]
    target_sum: Optional[float] = None
    settled_at: Optional[float] = None
    settled_with: Optional[str] = None
    exchanges: List[_Exchange] = field(default_factory=list)

    @property
    def is_multi(self) -> bool:
        return len(self.exchanges) > 1

    @property
    def status(self) -> str:
        """Whether the market ever settles in this simulation (use :meth:`status_at` for a given time)."""
        return "settled" if self.settled_at is not None else "open"

    def is_settled(self, now: float) -> bool:
        return self.settled_at is not None and now >= self.settled_at

    def status_at(self, now: float) -> str:
        return "settled" if self.is_settled(now) else "open"


# --------------------------------------------------------------------------- the simulated API


class DemoMarket:
    """A deterministic, offline simulation of one Predictions Cup tournament.

    ``now`` pins the demo's time at construction (default: ``clock()``); after that the demo's
    current time advances with ``clock``: ``now() = now + clock() - clock_at_start``. So with the
    default arguments the demo runs in real time, and with an injected fake clock every request
    sees the fake time. ``start`` is the scripted start ``t0`` (default: ``now``): a demo rebuilt
    on a kept database passes the original start so its scripted events continue where they were
    (docs/PAPER_TRADING.md D47). The same ``seed`` and ``start`` give identical data.
    """

    def __init__(self, seed: int = 7, now: Optional[float] = None, clock: Callable[[], float] = time.time, *,
                 start: Optional[float] = None) -> None:
        self.seed = int(seed)
        self._clock = clock
        self._clock_start = float(clock())
        self._origin = float(now) if now is not None else self._clock_start  # the demo's time at construction
        self.t0 = float(start) if start is not None else self._origin
        self.history_start = math.floor((self.t0 - HISTORY_DAYS * DAY) / HOUR) * HOUR
        self.tournament_id = DEMO_TOURNAMENT_ID
        self.slug = DEMO_SLUG
        self.base_url = DEMO_BASE_URL
        self._lock = threading.Lock()
        self._blocks: Dict[Tuple[int, int], List[_Trade]] = {}
        self._nodes: Dict[Tuple[int, int, int], float] = {}
        self.markets: List[_Market] = []
        self.exchanges: List[_Exchange] = []
        self._by_key: Dict[str, _Market] = {}
        self._market_by_id: Dict[str, _Market] = {}
        self._exchange_by_id: Dict[str, _Exchange] = {}
        self._build_markets()
        self._script_events()
        self._relationships = self._build_relationships()
        self._members = self._build_members()
        self._stories = self._build_stories()
        self.scenario = self._describe_scenario()

    # ------------------------------------------------------------------ public helpers

    def now(self) -> float:
        """The demo's current time (epoch seconds)."""
        return self._origin + (float(self._clock()) - self._clock_start)

    def transport(self) -> httpx.MockTransport:
        """An ``httpx`` transport that answers like the Super Market API (``/api/v1`` paths)."""
        return httpx.MockTransport(self._handle)

    def client(self, api_key: str = DEMO_API_KEY, **kwargs: Any) -> SuperMarketClient:
        """A real :class:`SuperMarketClient` wired to this simulation."""
        kwargs.setdefault("transport", self.transport())
        return SuperMarketClient(api_key, kwargs.pop("base_url", DEMO_BASE_URL), **kwargs)

    def context(self) -> Context:
        """The bot :class:`Context` for the demo tournament."""
        return Context.from_tournament(self._tournament_json(self.now(), summary=True))

    def mid_at(self, exchange_id: str, ts: float) -> Optional[float]:
        """Mid of the simulated book at ``ts`` (None once the market has settled)."""
        quote = self._quote(self._exchange(str(exchange_id)), ts)
        return quote[0] if quote else None

    def last_trade_at(self, exchange_id: str, ts: float) -> Optional[float]:
        """Price of the last trade at or before ``ts`` (the API's ``latestPrice`` then)."""
        trade = self._last_trade(self._exchange(str(exchange_id)), ts)
        return trade.price if trade else None

    # ------------------------------------------------------------------ construction

    def _build_markets(self) -> None:
        j = 0
        n = len(_MARKET_SPECS)
        for i, (key, title, liq_name, settlement, options, details) in enumerate(_MARKET_SPECS):
            details = dict(details)
            market = _Market(
                index=i,
                id=str(301 + i),
                key=key,
                title=title,
                settlement_date=settlement or None,
                created_at=self.history_start - (n - i) * 2 * HOUR,
                details=details,
                target_sum=details.pop("target_sum", None),
            )
            vol_scale = details.pop("vol", 1.0)
            settled = details.pop("settled", None)
            settles_live = details.pop("settles_live", None)
            mirror_key = details.pop("mirror", None)
            if settled and settles_live is not None:
                market.settled_at = self.t0 + float(settles_live)  # settles during the run (scenario l)
                market.settled_with = settled
                market.settlement_date = _iso(market.settled_at)
            elif settled:
                market.settled_at = math.floor((self.t0 - 2 * DAY) / MIN) * MIN
                market.settled_with = settled
                market.settlement_date = _iso(market.settled_at)
            liq = _LIQUIDITY[liq_name]
            total = sum(base for _, base in options)
            for slot, (option, base) in enumerate(options):
                amp = liq.vol * vol_scale * 2.0 * math.sqrt(base * (1.0 - base))
                ex = _Exchange(index=j, id=str(9001 + j), slot=slot, option=option, base=base, amp=amp, liq=liq, market=market)
                if market.target_sum is not None:
                    ex.initial_price = _tick(market.target_sum * base / total)
                else:
                    ex.initial_price = _tick(base)
                if liq_name == "band":
                    ex.yes_bias = 0.15 if base >= 0.5 else -0.15
                if mirror_key:
                    ex.mirror = self._by_key[mirror_key].exchanges[0]
                market.exchanges.append(ex)
                self.exchanges.append(ex)
                self._exchange_by_id[ex.id] = ex
                j += 1
            self.markets.append(market)
            self._by_key[key] = market
            self._market_by_id[market.id] = market

    def _script_events(self) -> None:
        t0 = self.t0
        rng = random.Random(_hash(self.seed, 21))

        # (a) news surge: +0.14 ramp from t0-50m, carried by many small, mostly-YES trades.
        pa = self._by_key["pa-senate"].exchanges[0]
        start = t0 - 50 * MIN
        pa.set_knots([(start, 0.0), (start + 3 * MIN, 0.09), (start + 8 * MIN, 0.14)])
        for _ in range(36):
            ts = start + (rng.random() ** 1.6) * 14 * MIN
            side = "YES" if rng.random() < 0.85 else "NO"
            pa.scripted.append((ts, side, _log_uniform(rng.random(), 10, 90)))
        # small pre-move prints so there is always a reference price about an hour back
        pa.scripted += [(start - 12 * MIN, "YES", 15), (start - 27 * MIN, "NO", 20)]

        # (b) participant spike: two large YES trades at t0-20m (+0.10 then +0.18) on a
        # thin book; 60% of the move reverts over the first 40 live minutes.
        oh = self._by_key["oh-house-09"].exchanges[0]
        b1, b2 = t0 - 20 * MIN, t0 - 20 * MIN + 40
        oh.set_knots([(b1, 0.0), (b1, 0.10), (b2, 0.10), (b2, 0.18), (t0, 0.18), (t0 + 40 * MIN, 0.072)])
        oh.quiet.append((t0 - 75 * MIN, t0))
        oh.scripted += [(t0 - 68 * MIN, "NO", 12), (b1, "YES", 520), (b2, "YES", 300)]
        for minutes, size in ((4, 60), (10, 45), (17, 80), (23, 35), (30, 55), (37, 40)):
            oh.scripted.append((t0 + minutes * MIN, "NO", size))

        # (f) live participant-style surge on Michigan 90 s after start: two large YES
        # trades (+0.06, then +0.10), held 8 minutes, then 60% reverts over 30 minutes.
        mi = self._by_key["mi-senate"].exchanges[0]
        f1, f2 = t0 + 90, t0 + 112
        mi.set_knots([(f1, 0.0), (f1, 0.06), (f2, 0.06), (f2, 0.10), (f2 + 8 * MIN, 0.10), (f2 + 38 * MIN, 0.04)])
        # Like Ohio: a quiet hour before the spike so the two big trades dominate the 1h window,
        # with one small print that anchors the price an hour back.
        mi.quiet.append((f1 - 70 * MIN, f2 + 6 * MIN))
        mi.scripted += [(f1 - 65 * MIN, "NO", 10), (f1, "YES", 420), (f2, "YES", 330)]
        for minutes, size in ((10, 50), (16, 70), (23, 40), (29, 65), (36, 45)):
            mi.scripted.append((f2 + minutes * MIN, "NO", size))

        # settled market: drifts up to 0.985 before it settled YES two days ago.
        debate = self._by_key["ga-debate"]
        ex = debate.exchanges[0]
        assert debate.settled_at is not None
        ex.set_knots([(self.history_start, 0.0), (debate.settled_at - DAY, 0.06), (debate.settled_at - 2 * HOUR, 0.185)])

        self._script_paper_events(rng)

        for ex in self.exchanges:
            ex.scripted.sort()

    def _script_paper_events(self, rng: random.Random) -> None:
        """Scenarios (g)-(l) for the paper trader (docs/PAPER_TRADING.md §7.6)."""
        t0 = self.t0

        def ex_of(key: str) -> _Exchange:
            return self._by_key[key].exchanges[0]

        # (g) Nevada Governor pair: both legs +0.035 over [t0+4m, t0+30m] (YES bids sum 1.05: a NO basket),
        # then from t0+45m both -0.01 (YES asks sum 1.00: the set sells at a profit).
        for key in ("nv-gov-d", "nv-gov-r"):
            ex_of(key).set_knots([(t0 + 3 * MIN, 0.0), (t0 + 4 * MIN, 0.035), (t0 + 30 * MIN, 0.035),
                                  (t0 + 33 * MIN, 0.0), (t0 + 42 * MIN, 0.0), (t0 + 45 * MIN, -0.01)])
        # (h) Texas Senate: the Cup converges from 0.50 toward the outside 0.58, reaching 0.575 at t0+3h.
        ex_of("tx-sen-d").set_knots([(t0 + 10 * MIN, 0.0), (t0 + 3 * HOUR, 0.075)])
        ex_of("tx-sen-r").set_knots([(t0 + 10 * MIN, 0.0), (t0 + 3 * HOUR, -0.075)])
        # (i) Iowa Senate: the Cup drifts away from the (wrong) outside price, 0.38 -> 0.33 by t0+90m.
        ex_of("ia-sen-d").set_knots([(t0 + 5 * MIN, 0.0), (t0 + 90 * MIN, -0.05)])
        ex_of("ia-sen-r").set_knots([(t0 + 5 * MIN, 0.0), (t0 + 90 * MIN, 0.05)])
        # (j) Nebraska Senate triple: the Independent leg +0.075 over [t0+60m, t0+80m] (bids sum 1.035).
        ex_of("ne-sen-i").set_knots([(t0 + 59 * MIN, 0.0), (t0 + 60 * MIN, 0.075), (t0 + 80 * MIN, 0.075),
                                     (t0 + 81 * MIN, 0.0)])
        # (k) Wyoming Governor liquidity hole: the mid falls 0.93 -> 0.625 within 20 s at t0+20m (12 sell
        # prints of 100 walk the bid down to 0.62), stays there until t0+23m and is back by t0+26m.
        wy = ex_of("wy-gov-r")
        hole = t0 + 20 * MIN
        wy.set_knots([(hole, 0.0), (hole + 20.0, -0.305), (t0 + 23 * MIN, -0.305), (t0 + 26 * MIN, 0.0)])
        for i in range(12):
            wy.scripted.append((hole + i * 20.0 / 11.0, "NO", 100))
        # (l) Maine Senate debate: rises to ~0.97 over [t0+20m, t0+40m] and settles YES at t0+40m.
        ex_of("me-debate").set_knots([(t0 + 20 * MIN, 0.0), (t0 + 40 * MIN, 0.12)])

    def _build_relationships(self) -> List[Dict[str, Any]]:
        senate = self._by_key["senate-control"]
        house = self._by_key["house-control"]
        arizona = self._by_key["az-governor"]
        return [
            {"id": "5e7a7e00-0000-4000-8000-000000000001", "type": "complementary", "market": senate,
             "description": "Exactly one party controls the Senate, so the two outcomes must sum to 1."},
            {"id": "5e7a7e00-0000-4000-8000-000000000002", "type": "complementary", "market": house,
             "description": "Exactly one party controls the House, so the two outcomes must sum to 1."},
            {"id": "5e7a7e00-0000-4000-8000-000000000003", "type": "mutually_exclusive", "market": arizona,
             "description": "At most one candidate wins the Arizona Governor race, so the outcomes sum to at most 1."},
        ]

    def _build_members(self) -> List[Dict[str, Any]]:
        rng = random.Random(_hash(self.seed, 11))
        members = []
        for i, handle in enumerate(_HANDLES):
            pnl = 24000.0 * (0.86 ** i) - 2500.0 + rng.uniform(-400.0, 400.0)
            members.append({
                "profileId": _uuid(self.seed, 12, i),
                "username": handle,
                "pnl": round(pnl, 2),
                "tradesCount": rng.randint(15, 420),
                "volume": round(rng.uniform(20000.0, 400000.0), 2),
                "winRate": None if rng.random() < 0.1 else round(rng.uniform(35.0, 75.0), 1),
                "factors": {p: rng.uniform(lo, hi) for p, (lo, hi) in sorted(_LEADERBOARD_FACTORS.items())},
            })
        # Three far-ahead leaders (§7.6): their all-time values set the top-3 bar at 221,500, so a 100,000
        # portfolio is "behind" (M = 2.215) and the chaser runs in its chase mode.
        for i, (handle, pnl) in enumerate(DEMO_TOP_MEMBERS):
            members.append({
                "profileId": _uuid(self.seed, 14, i),
                "username": handle,
                "pnl": pnl,
                "tradesCount": (612, 455, 389)[i],
                "volume": (2_400_000.0, 1_900_000.0, 1_650_000.0)[i],
                "winRate": (61.5, 57.2, 49.8)[i],
                "factors": {"1d": 0.01, "7d": 0.25, "30d": 0.9, "quarter": 1.0, "all": 1.0},
            })
        return members

    def _build_stories(self) -> List[Tuple[Tuple[str, ...], List[Article]]]:
        def article(item: Tuple[float, str, str, str, str]) -> Article:
            offset, title, source, slug, summary = item
            return Article(
                title=title,
                url=f"https://example.com/news/{slug}",
                source=source,
                published_at=self.t0 + offset,
                summary=summary,
                provider=DemoNewsProvider.name,
            )

        stories = [(match, [article(a) for a in items]) for match, items in _STORIES]
        stories.append(((), [article(a) for a in _NOISE_NEWS]))
        return stories

    def _describe_scenario(self) -> Dict[str, Any]:
        def ref(key: str) -> Dict[str, Any]:
            m = self._by_key[key]
            return {"market_id": m.id, "exchange_id": m.exchanges[0].id, "title": m.title}

        cup_end = parse_time(CUP_END)
        assert cup_end is not None
        high = []
        for key in ("ca-governor", "wy-senate", "ny-governor", "libertarian-house"):
            m = self._by_key[key]
            settles = parse_time(m.settlement_date or "")
            high.append({
                **ref(key),
                "favorite": "YES" if m.exchanges[0].base >= 0.5 else "NO",
                "settlement_date": m.settlement_date,
                "settles_before_cup_end": bool(settles and settles < cup_end),
            })
        return {
            "t0": self.t0,
            "tournament_id": self.tournament_id,
            "slug": self.slug,
            "news_surge": {**ref("pa-senate"), "start": self.t0 - 50 * MIN, "change": 0.14, "news_at": self.t0 - 55 * MIN},
            "participant_spike": {**ref("oh-house-09"), "start": self.t0 - 20 * MIN, "change": 0.18,
                                  "revert_by": self.t0 + 40 * MIN, "revert_fraction": 0.6},
            "live_surge": {**ref("mi-senate"), "start": self.t0 + 90, "change": 0.10},
            "high_band": high,
            "arbitrage_market_id": self._by_key["az-governor"].id,
            "violation_relationship_id": self._relationships[0]["id"],
            "settled_market_id": self._by_key["ga-debate"].id,
            # paper-trading scenarios (g)-(l), docs/PAPER_TRADING.md §7.6
            "basket_pair": {"exchange_ids": [self._by_key[k].exchanges[0].id for k in ("nv-gov-d", "nv-gov-r")],
                            "market_ids": [self._by_key[k].id for k in ("nv-gov-d", "nv-gov-r")],
                            "start": self.t0 + 4 * MIN, "end": self.t0 + 30 * MIN, "bid_sum": 1.05,
                            "exit_from": self.t0 + 45 * MIN, "ask_sum_after": 1.00},
            "value_winner": {**ref("tx-sen-d"), "pair_exchange_id": self._by_key["tx-sen-r"].exchanges[0].id,
                             "fair_value": 0.58, "converge_start": self.t0 + 10 * MIN, "converge_end": self.t0 + 3 * HOUR,
                             "mid_from": 0.50, "mid_to": 0.575},
            "value_loser": {**ref("ia-sen-d"), "pair_exchange_id": self._by_key["ia-sen-r"].exchanges[0].id,
                            "fair_value": 0.46, "fair_value_after": 0.30, "drop_at": self.t0 + 90 * MIN,
                            "mid_from": 0.38, "mid_to": 0.33},
            "basket_triple": {"exchange_ids": [self._by_key[k].exchanges[0].id for k in ("ne-sen-d", "ne-sen-r", "ne-sen-i")],
                              "market_ids": [self._by_key[k].id for k in ("ne-sen-d", "ne-sen-r", "ne-sen-i")],
                              "start": self.t0 + 60 * MIN, "end": self.t0 + 80 * MIN, "bid_sum_min": 1.03},
            "liquidity_hole": {**ref("wy-gov-r"), "start": self.t0 + 20 * MIN, "prints": 12, "print_size": 100,
                               "low_bid": 0.62, "recovered_by": self.t0 + 26 * MIN, "fair_value": 0.94},
            "settles_live": {**ref("me-debate"), "settles_at": self.t0 + 40 * MIN, "settled_with": "YES",
                             "fair_value": 0.99},
            "leading_feed": {"exchange_ids": [self._by_key[k].exchanges[0].id
                                              for k in ("mi-senate", "ga-senate", "nc-senate", "me-senate")],
                             "lead_s": FEED_LEAD_S},
        }

    # ------------------------------------------------------------------ outside fair values (scripted)

    def fair_value_feed(self, exchange_id: str, ts: float) -> Optional[float]:
        """The demo's outside YES fair value of an outcome at ``ts`` (None: no outside market prices it).

        Pure in (seed, exchange, ts). Scripted outcomes follow §7.6; the liquid Michigan, Georgia, North
        Carolina and Maine Senate markets get an outside price that LEADS the Cup by ``FEED_LEAD_S`` (the
        Cup's own mid 15 minutes later): the hypothesis that the Cup lags outside prices, by design."""
        eid = str(exchange_id)
        ex = self._exchange_by_id.get(eid)
        if ex is None:
            return None
        key = ex.market.key
        t0 = self.t0

        def mid(at: float) -> Optional[float]:
            quote = self._quote(ex, at)
            return quote[0] if quote else None

        if key in FEED_LEADING_KEYS:
            return mid(ts + FEED_LEAD_S)
        if key in ("nv-gov-d", "nv-gov-r"):
            return 0.50
        if key == "tx-sen-d":
            return 0.58 if ts >= t0 - 2 * HOUR else mid(ts)
        if key == "tx-sen-r":
            return 0.42 if ts >= t0 - 2 * HOUR else mid(ts)
        if key in ("ia-sen-d", "ia-sen-r"):
            if ts < t0 - HOUR:
                return mid(ts)
            d = 0.46 if ts < t0 + 90 * MIN else 0.30
            return d if key == "ia-sen-d" else round(1.0 - d, 6)
        if key == "ne-sen-d":
            return 0.02
        if key == "ne-sen-r":
            return 0.67
        if key == "ne-sen-i":
            return 0.31
        if key == "wy-gov-r":
            return 0.94
        if key == "me-debate":
            return 0.99 if ts >= t0 else mid(ts)
        return None

    # ------------------------------------------------------------------ price model

    def _node(self, j: int, octave: int, k: int) -> float:
        key = (j, octave, k)
        value = self._nodes.get(key)
        if value is None:
            value = 2.0 * _unit(self.seed, 1, j, octave, k) - 1.0
            self._nodes[key] = value  # deterministic, so concurrent writers agree
        return value

    def _noise(self, j: int, minute: int) -> float:
        t = minute * MIN
        total = 0.0
        for octave, (spacing, amp) in enumerate(_OCTAVES):
            x = t / spacing
            k = math.floor(x)
            f = x - k
            f = f * f * (3.0 - 2.0 * f)
            a = self._node(j, octave, k)
            total += amp * (a + (self._node(j, octave, k + 1) - a) * f)
        return total + _JITTER * (2.0 * _unit(self.seed, 2, j, minute) - 1.0)

    def _fair_raw(self, ex: _Exchange, ts: float) -> float:
        """Unscripted, unclipped YES fair value at ``ts`` (base + noise; 1 - the mirror's for a race pair)."""
        if ex.mirror is not None:
            return 1.0 - self._fair_raw(ex.mirror, ts)
        minute = math.floor(ts / MIN)
        market = ex.market
        if market.target_sum is not None:
            weights = [max(0.004, e.base + e.amp * self._noise(e.index, minute)) for e in market.exchanges]
            return market.target_sum * weights[ex.slot] / sum(weights)
        return ex.base + ex.amp * self._noise(ex.index, minute)

    def _fair(self, ex: _Exchange, ts: float) -> float:
        """Unrounded YES fair value at ``ts``."""
        price = self._fair_raw(ex, ts) + ex.offset(ts)
        return min(0.99, max(0.01, price))

    def _quote(self, ex: _Exchange, ts: float) -> Optional[Tuple[float, float, float]]:
        """``(mid, best bid, best ask)`` at ``ts``; None once the market has settled."""
        settled = ex.market.settled_at
        if settled is not None and ts >= settled:
            return None
        mid = _tick(self._fair(ex, ts))
        half = (ex.liq.spread_ticks // 2) * TICK
        bid, ask = mid - half, mid + half
        if ask > 0.995:
            ask = 0.995
            bid = ask - 2 * half
        if bid < 0.005:
            bid = 0.005
            ask = bid + 2 * half
        bid, ask = round(bid, 3), round(ask, 3)
        return round((bid + ask) / 2, 4), bid, ask

    def _p_yes(self, ex: _Exchange, ts: float) -> float:
        slope = self._fair(ex, ts + 90) - self._fair(ex, ts - 90)
        return min(0.95, max(0.05, 0.5 + ex.yes_bias + max(-0.3, min(0.3, slope * 6.0))))

    def _active(self, ex: _Exchange, ts: float) -> bool:
        settled = ex.market.settled_at
        return ts >= self.history_start and (settled is None or ts < settled)

    def _make_block(self, ex: _Exchange, hour: int) -> List[_Trade]:
        start = hour * HOUR
        end = start + HOUR
        rows: List[Tuple[float, str, int]] = []
        settled = ex.market.settled_at
        if end > self.history_start and (settled is None or start < settled):
            rng = random.Random(_hash(self.seed, 3, ex.index, hour))
            lo, hi = ex.liq.size
            for _ in range(_poisson(rng, ex.liq.rate * _diurnal(start + HOUR / 2))):
                ts = start + rng.random() * HOUR
                u_side, u_size = rng.random(), rng.random()  # always drawn, so filtering keeps the stream stable
                if not self._active(ex, ts) or ex.is_quiet(ts):
                    continue
                side = "YES" if u_side < self._p_yes(ex, ts) else "NO"
                rows.append((ts, side, _log_uniform(u_size, lo, hi)))
            first = bisect.bisect_left(ex.scripted, (start, "", 0))
            for item in ex.scripted[first:]:
                if item[0] >= end:
                    break
                if self._active(ex, item[0]):
                    rows.append(item)
        rows.sort()
        trades = []
        for i, (ts, side, size) in enumerate(rows):
            quote = self._quote(ex, ts)
            if quote is None:
                continue
            price = quote[2] if side == "YES" else quote[1]
            trades.append(_Trade(ts, price, size, side, (hour + _ID_HOUR_OFFSET) * 1_000_000 + ex.index * 10_000 + i))
        return trades

    def _block(self, ex: _Exchange, hour: int) -> List[_Trade]:
        key = (ex.index, hour)
        with self._lock:
            cached = self._blocks.get(key)
        if cached is not None:
            return cached
        trades = self._make_block(ex, hour)
        with self._lock:
            if len(self._blocks) >= _BLOCK_CACHE_MAX:
                for old in list(self._blocks)[: _BLOCK_CACHE_MAX // 4]:
                    del self._blocks[old]
            return self._blocks.setdefault(key, trades)

    def _trades_between(self, ex: _Exchange, lo: float, hi: float, now: float) -> List[_Trade]:
        """Trades with ``lo <= ts < hi`` and ``ts <= now``, oldest first."""
        lo = max(lo, self.history_start)
        hi = min(hi, math.nextafter(now, math.inf))  # ts <= now counts as "before now"
        if hi <= lo:
            return []
        out: List[_Trade] = []
        for hour in range(math.floor(lo / HOUR), math.floor(hi / HOUR) + 1):
            out.extend(t for t in self._block(ex, hour) if lo <= t.ts < hi)
        return out

    def _last_trade(self, ex: _Exchange, ts: float) -> Optional[_Trade]:
        hour = math.floor(ts / HOUR)
        first = math.floor(self.history_start / HOUR)
        while hour >= first:
            for trade in reversed(self._block(ex, hour)):
                if trade.ts <= ts:
                    return trade
            hour -= 1
        return None

    def _book(self, ex: _Exchange, ts: float, depth: int) -> Tuple[List[Dict[str, float]], List[Dict[str, float]]]:
        quote = self._quote(ex, ts)
        if quote is None:
            return [], []
        _, bid, ask = quote
        minute = math.floor(ts / MIN)
        sides: List[List[Dict[str, float]]] = []
        for side, best, step in ((0, bid, -TICK), (1, ask, TICK)):
            levels: List[Dict[str, float]] = []
            i = 0
            while len(levels) < depth:
                price = round(best + step * i, 3)
                if price < 0.005 - 1e-9 or price > 0.995 + 1e-9:
                    break
                if i == 0 or _unit(self.seed, 4, ex.index, minute, side, i) >= ex.liq.gap:
                    jitter = 0.55 + 0.9 * _unit(self.seed, 5, ex.index, minute, side, i)
                    qty = max(1, int(round(ex.liq.touch_qty * (ex.liq.decay ** i) * jitter)))
                    levels.append({"price": price, "quantity": qty})
                i += 1
            sides.append(levels)
        return sides[0], sides[1]

    def _sequence(self, ts: float) -> int:
        return _SEQ_BASE + int(max(0.0, ts - self.history_start))

    def _as_of(self, now: float) -> Dict[str, Any]:
        return {"sequence": self._sequence(now), "at": _iso(now)}

    # ------------------------------------------------------------------ JSON builders

    def _tournament_ref(self) -> Dict[str, Any]:
        return {"id": self.tournament_id, "slug": self.slug, "name": DEMO_TOURNAMENT_NAME,
                "currencyName": DEMO_CURRENCY, "isOngoingPlay": False}

    def _context_descriptor(self) -> Dict[str, Any]:
        return {"type": "tournament", "tournament": self._tournament_ref()}

    def _cash(self) -> float:
        cost = sum(qty * avg for _, _, qty, avg, _ in _POSITIONS)
        return round(DEMO_INITIAL_BALANCE + _REALIZED_PNL - cost, 2)

    def _tournament_json(self, now: float, summary: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.tournament_id,
            "slug": self.slug,
            "name": DEMO_TOURNAMENT_NAME,
            "description": "Offline simulation of the Susquehanna Predictions Cup (midterm elections). No real trading.",
            "status": "active",
            "startDate": CUP_START,
            "endDate": CUP_END,
            "initialBalance": DEMO_INITIAL_BALANCE,
            "currencyName": DEMO_CURRENCY,
        }
        if summary:
            out.update(myBalance=self._cash(), joinedAt="2026-10-01T14:03:11.000Z", isPendingEnrolment=False)
        else:
            out.update(memberCount=412, marketCount=len(self.markets), myBalance=self._cash(), isPendingEnrolment=False)
        return out

    def _latest(self, ex: _Exchange, now: float) -> Optional[float]:
        trade = self._last_trade(ex, now)
        return trade.price if trade else None

    def _market_json(self, market: _Market, now: float, canonical: bool = True) -> Dict[str, Any]:
        latest = {ex.id: self._latest(ex, now) for ex in market.exchanges}
        settled = market.is_settled(now)
        settled_on = _iso(market.settled_at) if settled and market.settled_at is not None else None
        settled_with = market.settled_with if settled else None
        status = market.status_at(now)
        out: Dict[str, Any] = {
            "id": market.id,
            "title": market.title,
            "thumbnailUrl": None,
            "status": status,
            "createdAt": _iso(market.created_at),
            "settlementDate": market.settlement_date,
            "settledWith": settled_with,
            "settledOn": settled_on,
            "categories": ["Election Outcome"],
            "isComposite": False,
            "isMultiOutcome": market.is_multi,
            "exchanges": [
                {"id": ex.id, "option": ex.option, "latestPrice": latest[ex.id], "initialPrice": ex.initial_price}
                for ex in market.exchanges
            ],
        }
        if canonical:
            out["contexts"] = [{
                **self._context_descriptor(),
                "status": status,
                "settledWith": settled_with,
                "settledOn": settled_on,
                "exchanges": [{"id": ex.id, "latestPrice": latest[ex.id]} for ex in market.exchanges],
            }]
        out["creator"] = dict(_CREATOR)
        return out

    def _price_json(self, ex: _Exchange, now: float) -> Dict[str, Any]:
        quote = self._quote(ex, now)
        bid, ask = (quote[1], quote[2]) if quote else (None, None)
        return {
            "exchangeId": ex.id,
            "marketId": ex.market.id,
            "option": ex.option,
            "latestPrice": self._latest(ex, now),
            "bestBid": bid,
            "bestAsk": ask,
            "spread": round(ask - bid, 3) if quote else None,
        }

    def _combined_book(self, market: _Market, now: float, depth: int) -> Dict[str, Any]:
        rows = []
        implied: List[Optional[float]] = []
        for ex in market.exchanges:
            bids, asks = self._book(ex, now, depth)
            prob = asks[0]["price"] if asks else self._latest(ex, now)
            implied.append(prob)
            rows.append({"exchangeId": ex.id, "option": ex.option, "asOf": self._as_of(now),
                         "impliedProbability": prob, "bids": bids, "asks": asks})
        overround = None if any(p is None for p in implied) else round(sum(p for p in implied if p is not None), 4)
        arbitrage = bool(market.is_multi and overround is not None and overround < 1.0
                         and all(row["asks"] for row in rows))
        return {"exchanges": rows, "overround": overround, "hasArbitrageOpportunity": arbitrage}

    # ------------------------------------------------------------------ portfolio / leaderboard

    def _positions(self, now: float) -> List[Dict[str, Any]]:
        out = []
        for n, (key, slot, qty, avg, days) in enumerate(_POSITIONS):
            market = self._by_key[key]
            ex = market.exchanges[slot]
            price = self._latest(ex, now)
            if price is None:
                quote = self._quote(ex, now)
                price = quote[0] if quote else avg
            value = qty * price
            cost = qty * avg
            out.append({
                "exchangeId": ex.id,
                "marketId": market.id,
                "marketTitle": market.title,
                "option": ex.option,
                "settled": market.is_settled(now),
                "quantity": qty,
                "avgCost": avg,
                "currentPrice": price,
                "marketValue": round(value, 2),
                "costBasis": round(cost, 2),
                "unrealizedPnl": round(value - cost, 2),
                "unrealizedPnlPct": round((value - cost) / cost * 100.0, 2),
                "moneyEarned": 0,
                "lots": [{"lotId": _uuid(self.seed, 13, n), "side": "YES", "quantity": qty, "entryPrice": avg,
                          "openedAt": _iso(self.t0 - days * DAY)}],
            })
        return out

    def _account_value(self, now: float) -> float:
        return round(self._cash() + sum(p["marketValue"] for p in self._positions(now)), 2)

    def _leaderboard_rows(self, period: str, sort: str, now: float) -> List[Dict[str, Any]]:
        my_all = self._account_value(now) - DEMO_INITIAL_BALANCE
        lo, hi = _LEADERBOARD_FACTORS[period]
        my_factor = (lo + hi) / 2
        rows = []
        for m in self._members:
            factor = m["factors"][period]
            rows.append(self._leader_row(m["profileId"], m["username"], m["pnl"] * factor,
                                         max(1, int(m["tradesCount"] * min(1.0, factor + 0.1))),
                                         m["volume"] * min(1.0, abs(factor) + 0.1), m["winRate"]))
        rows.append(self._leader_row(_ME_ID, "demo_trader", my_all * my_factor, max(1, int(23 * min(1.0, my_factor + 0.1))),
                                     9850.0 * min(1.0, abs(my_factor) + 0.1), 58.3))
        key: Callable[[Dict[str, Any]], Any] = {
            "pnl": lambda r: r["pnl"], "roi": lambda r: r["roi"], "winRate": lambda r: r["winRate"],
            "volume": lambda r: r["volume"], "trades": lambda r: r["tradesCount"],
        }[sort]
        rows.sort(key=lambda r: (key(r) is None, -(key(r) or 0.0), r["username"] or ""))
        rank, previous = 0, object()
        for i, row in enumerate(rows):
            value = key(row)
            if value != previous:
                rank, previous = i + 1, value
            row["rank"] = rank
        return rows

    @staticmethod
    def _leader_row(pid: str, name: str, pnl: float, trades: int, volume: float, win: Optional[float]) -> Dict[str, Any]:
        roi = max(-500.0, min(500.0, round(pnl / volume * 100.0, 2))) if volume else 0.0
        return {"rank": 0, "profileId": pid, "username": name, "pnl": round(pnl, 2), "tradesCount": trades,
                "volume": round(volume, 2), "winRate": win, "roi": roi}

    # ------------------------------------------------------------------ request handling

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if path.startswith(_API_PREFIX):
            path = path[len(_API_PREFIX):]
        path = "/" + path.strip("/")
        try:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer ") or not auth[7:].strip():
                raise _Fail(401, "MISSING_API_KEY", "No API key was provided. Send Authorization: Bearer <key>.")
            for verb, pattern, name in _ROUTES:
                match = pattern.fullmatch(path)
                if match is not None and verb == method:
                    now = self.now()
                    body = getattr(self, name)(request.url.params, now, **match.groupdict())
                    return httpx.Response(200, json=body)
            raise _Fail(404, "NOT_FOUND", f"No route for {method} {_API_PREFIX}{path}")
        except _Fail as exc:
            return exc.response()
        except Exception:  # a bug in the simulation must not look like a network failure
            log.exception("demo API: %s %s failed", method, path)
            return _Fail(500, "INTERNAL_ERROR", "Internal server error").response()

    # lookups and shared validation

    def _check_tournament(self, params: Mapping[str, str], name: str = "tournamentId") -> None:
        tid = params.get(name)
        if tid is None or tid == "":
            return  # the key's default context is the demo tournament
        if not _UUID_RE.match(tid):
            raise _invalid(name, "must be a UUID")
        if tid != self.tournament_id:
            raise _Fail(404, "NOT_FOUND", "Tournament not found")

    def _check_slug(self, slug: str) -> None:
        if slug != self.slug:
            raise _Fail(404, "NOT_FOUND", "Tournament not found")

    def _market(self, market_id: str) -> _Market:
        if not market_id.isdigit():
            raise _Fail(400, "INVALID_ID", "Invalid market id")
        market = self._market_by_id.get(market_id)
        if market is None:
            raise _Fail(404, "NOT_FOUND", "Market not found")
        return market

    def _exchange(self, exchange_id: str) -> _Exchange:
        if not exchange_id.isdigit():
            raise _Fail(400, "INVALID_ID", "Invalid exchange id")
        ex = self._exchange_by_id.get(exchange_id)
        if ex is None:
            raise _Fail(404, "NOT_FOUND", "Exchange not found")
        return ex

    def _filter_markets(self, params: Mapping[str, str], now: float) -> List[_Market]:
        status = _enum_param(params, "status", _MARKET_STATUSES, "any")
        if status == "resolved":
            status = "settled"
        search = params.get("search")
        if search is not None and len(search) > 200:
            raise _invalid("search", "must be at most 200 characters")
        markets = [m for m in self.markets if status == "any" or m.status_at(now) == status]
        if search:
            needle = search.lower()
            markets = [m for m in markets if needle in m.title.lower()]
        return markets

    @staticmethod
    def _page(items: List[Any], params: Mapping[str, str], kind: str, default_limit: int, max_limit: int) -> Tuple[List[Any], Dict[str, Any]]:
        limit = _int_param(params, "limit", default_limit, 1, max_limit)
        offset = _read_cursor(params.get("cursor"), kind) or 0
        if offset < 0:
            raise _Fail(400, "INVALID_CURSOR", "Invalid or expired pagination cursor")
        page = items[offset: offset + limit]
        has_more = offset + limit < len(items)
        return page, {"total": len(items), "limit": limit, "hasMore": has_more,
                      "nextCursor": _cursor(kind, offset + limit) if has_more else None}

    # account & tournaments

    def _get_account(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        return {"id": _ME_ID, "username": "demo_trader", "email": "demo.trader@example.com",
                "createdAt": "2026-09-28T14:03:11.000Z", "avatarUrl": None,
                "bio": "Offline demo account", "balance": self._cash()}

    def _get_tournaments(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        status = _enum_param(params, "status", ("draft", "active", "ended", "any"), "any")
        limit = _int_param(params, "limit", 50, 1, 100)
        offset = _int_param(params, "offset", 0, 0)
        market_id = params.get("marketId")
        if market_id is not None and market_id != "" and (not market_id.isdigit() or int(market_id) <= 0):
            raise _invalid("marketId", "must be a positive integer")
        items = [self._tournament_json(now, summary=True)] if status in ("active", "any") else []
        if market_id and market_id not in self._market_by_id:
            items = []
        page = items[offset: offset + limit]
        return {"data": page, "pagination": {"limit": limit, "offset": offset,
                                             "hasMore": offset + limit < len(items), "total": len(items)}}

    def _get_tournament(self, params: Mapping[str, str], now: float, slug: str) -> Dict[str, Any]:
        self._check_slug(slug)
        return self._tournament_json(now, summary=False)

    def _get_tournament_markets(self, params: Mapping[str, str], now: float, slug: str) -> Dict[str, Any]:
        self._check_slug(slug)
        if params.get("marketGroupId"):
            raise _invalid("marketGroupId", "market group is not bound to this tournament")
        page, pagination = self._page(self._filter_markets(params, now), params, "tm", 20, 100)
        return {"data": [self._market_json(m, now, canonical=False) for m in page], "pagination": pagination}

    def _get_tournament_market(self, params: Mapping[str, str], now: float, slug: str, market_id: str) -> Dict[str, Any]:
        self._check_slug(slug)
        market = self._market(market_id)
        out = self._market_json(market, now, canonical=False)
        combined = self._combined_book(market, now, 20)
        for ex_json, book in zip(out["exchanges"], combined["exchanges"]):
            ex_json.update(asOf=book["asOf"], bids=book["bids"], asks=book["asks"],
                           bestBid=book["bids"][0]["price"] if book["bids"] else None,
                           bestAsk=book["asks"][0]["price"] if book["asks"] else None)
        out["overround"] = combined["overround"]
        out["hasArbitrageOpportunity"] = combined["hasArbitrageOpportunity"]
        return out

    def _get_tournament_leaderboard(self, params: Mapping[str, str], now: float, slug: str) -> Dict[str, Any]:
        self._check_slug(slug)
        period = _enum_param(params, "period", ("1d", "7d", "30d", "all"), "all") or "all"
        sort = _enum_param(params, "sort", _LEADERBOARD_SORTS, "pnl") or "pnl"
        limit = _int_param(params, "limit", 50, 1, 100)
        offset = _int_param(params, "offset", 0, 0)
        if params.get("groupId"):
            raise _invalid("groupId", "user group is not bound to this tournament")
        season = params.get("season")
        if season and season != "current":
            raise _invalid("season", "seasons are only available on Ongoing Play tournaments")
        rows = self._leaderboard_rows(period, sort, now)
        my_rank = next((r["rank"] for r in rows if r["profileId"] == _ME_ID), None)
        return {"leaderboard": rows[offset: offset + limit], "total": len(rows), "period": period, "limit": limit,
                "offset": offset, "sort": sort, "myRank": my_rank, "season": None, "boundGroups": [],
                "activeGroupId": None}

    def _get_leaderboards(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        slug = params.get("tournamentSlug")
        if slug:
            self._check_slug(slug)
        if params.get("groupId"):
            raise _invalid("groupId", "user group is not bound to this tournament")
        period = _enum_param(params, "period", ("1d", "7d", "30d", "quarter", "all"), "all") or "all"
        sort = _enum_param(params, "sort", _LEADERBOARD_SORTS, "pnl") or "pnl"
        limit = _int_param(params, "limit", 50, 1, 100)
        offset = _int_param(params, "offset", 0, 0)
        rows = self._leaderboard_rows(period, sort, now)
        return {"leaderboard": rows[offset: offset + limit], "total": len(rows), "period": period,
                "limit": limit, "offset": offset}

    def _get_positions(self, params: Mapping[str, str], now: float, slug: str) -> Dict[str, Any]:
        self._check_slug(slug)
        positions = self._positions(now)
        value = sum(p["marketValue"] for p in positions)
        cost = sum(p["costBasis"] for p in positions)
        return {"positions": positions, "summary": {"totalMarketValue": round(value, 2), "totalCostBasis": round(cost, 2),
                                                    "totalUnrealizedPnl": round(value - cost, 2)}}

    def _get_pnl(self, params: Mapping[str, str], now: float, slug: str) -> Dict[str, Any]:
        self._check_slug(slug)
        period = _enum_param(params, "period", tuple(_PNL_PERIOD_DAYS), "quarter") or "quarter"
        positions = self._positions(now)
        holdings = round(sum(p["marketValue"] for p in positions), 2)
        cost = round(sum(p["costBasis"] for p in positions), 2)
        total = round(self._cash() + holdings, 2)
        days = _PNL_PERIOD_DAYS[period]
        if days is None:
            period_start, period_pnl = None, None
            roi: Optional[float] = round((total - DEMO_INITIAL_BALANCE) / DEMO_INITIAL_BALANCE * 100.0, 2)
        else:
            start = now - days * DAY
            baseline = self._account_value(start) if start >= self.history_start else float(DEMO_INITIAL_BALANCE)
            period_start, period_pnl = _iso(start), round(total - baseline, 2)
            roi = round(period_pnl / baseline * 100.0, 2) if baseline else None
        return {"period": period, "periodStart": period_start, "periodEnd": _iso(now), "periodPnl": period_pnl,
                "unrealizedPnl": round(holdings - cost, 2), "totalAccountValue": total, "totalHoldingsValue": holdings,
                "totalCostBasis": cost, "roi": roi, "sharpe": 1.37}

    # markets

    def _get_markets(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        self._check_tournament(params)
        sort = _enum_param(params, "sort", _MARKET_SORTS, "recent")
        category = _enum_param(params, "category", ("sports-outcome", "election-outcome", "economic-indicator",
                                                     "financial-product", "freeform", "existing-market",
                                                     "corporate-earnings"), None)
        multi = _enum_param(params, "is_multi_outcome", ("true", "false"), None)
        composite = _enum_param(params, "is_composite", ("true", "false"), None)
        markets = self._filter_markets(params, now)
        if category is not None and category != "election-outcome":
            markets = []
        if composite == "true":
            markets = []
        if multi is not None:
            markets = [m for m in markets if m.is_multi == (multi == "true")]
        ids = params.get("ids")
        if ids:
            wanted = {part.strip() for part in ids.split(",") if part.strip()}
            markets = [m for m in markets if m.id in wanted]
        if sort == "recent":
            markets.sort(key=lambda m: -m.created_at)
        elif sort == "oldest":
            markets.sort(key=lambda m: m.created_at)
        elif sort == "closing":
            markets.sort(key=lambda m: (m.settlement_date is None, m.settlement_date or "", m.id))
        else:  # trending: most trades over the last 24 hours
            def activity(m: _Market) -> int:
                return sum(len(self._trades_between(ex, now - DAY, now, now)) for ex in m.exchanges)
            markets.sort(key=lambda m: (-activity(m), m.id))
        page, pagination = self._page(markets, params, "mk", 20, 100)
        return {"data": [self._market_json(m, now) for m in page], "pagination": pagination}

    def _get_market(self, params: Mapping[str, str], now: float, market_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        return self._market_json(self._market(market_id), now)

    def _get_market_nodes(self, params: Mapping[str, str], now: float, market_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        market = self._market(market_id)
        details = {k: v for k, v in market.details.items()}
        details["resolutionSource"] = "Associated Press race call"

        def contract(node_id: str, title: str, extra: Dict[str, Any]) -> Dict[str, Any]:
            return {"node_id": node_id, "node_type": "contract", "contract_id": node_id,
                    "contract_type": "election-outcome", "title": title,
                    "settlement_date": market.settlement_date, "settled_with": market.settled_with,
                    "contract_details": {**details, **extra}}

        if market.is_multi:
            root: Dict[str, Any] = {
                "node_id": f"{market.id}-root", "node_type": "operator", "operator": "OR",
                "children": [contract(f"{market.id}-c{ex.slot + 1}", f"{market.title} — {ex.option}", {"outcome": ex.option})
                             for ex in market.exchanges],
            }
        else:
            root = contract(f"{market.id}-c1", market.title, {})
        return {"market_id": market.id, "root": root, "contexts": [self._context_descriptor()]}

    def _get_market_orderbook(self, params: Mapping[str, str], now: float, market_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        depth = _int_param(params, "depth", 20, 1, 200)
        combined = self._combined_book(self._market(market_id), now, depth)
        return {**combined, "contexts": [{**self._context_descriptor(), "orderbook": combined}]}

    # exchanges

    def _get_exchanges(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        self._check_tournament(params)
        market_id, ids = params.get("marketId"), params.get("ids")
        if market_id and ids:
            raise _invalid("marketId", "marketId and ids are mutually exclusive")
        exchanges = list(self.exchanges)
        if market_id:
            if not market_id.isdigit():
                raise _invalid("marketId", "must be a numeric id")
            exchanges = [ex for ex in exchanges if ex.market.id == market_id]
        if ids:
            wanted = [part.strip() for part in ids.split(",") if part.strip()]
            if len(wanted) > 100:
                raise _invalid("ids", "at most 100 exchange ids")
            exchanges = [self._exchange_by_id[i] for i in wanted if i in self._exchange_by_id]
        page, pagination = self._page(exchanges, params, "ex", 50, 200)
        del pagination["total"]
        data = []
        for ex in page:
            latest = self._latest(ex, now)
            data.append({"id": ex.id, "marketId": ex.market.id, "option": ex.option, "latestPrice": latest,
                         "initialPrice": ex.initial_price,
                         "contexts": [{**self._context_descriptor(), "latestPrice": latest}]})
        return {"data": data, "pagination": pagination}

    def _get_prices(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        self._check_tournament(params)
        raw = params.get("ids")
        if not raw:
            raise _invalid("ids", "is required")
        ids = [part.strip() for part in raw.split(",") if part.strip()]
        if not ids:
            raise _invalid("ids", "is required")
        if len(ids) > 100:
            raise _invalid("ids", "at most 100 exchange ids per request")
        if not all(i.isdigit() for i in ids):
            raise _invalid("ids", "must be numeric exchange ids")
        data, missing, seen = [], [], set()
        for eid in ids:
            if eid in seen:
                continue
            seen.add(eid)
            ex = self._exchange_by_id.get(eid)
            if ex is None or ex.market.is_settled(now):
                missing.append(eid)  # unknown, or settled: no longer quoted by the bulk read
            else:
                data.append(self._price_json(ex, now))
        return {"data": data, "missingIds": missing}

    def _get_price(self, params: Mapping[str, str], now: float, exchange_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        return self._price_json(self._exchange(exchange_id), now)

    def _get_orderbook(self, params: Mapping[str, str], now: float, exchange_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        ex = self._exchange(exchange_id)
        depth = _int_param(params, "depth", 20, 1, 200)
        bids, asks = self._book(ex, now, depth)
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        return {"exchangeId": ex.id, "marketId": ex.market.id, "asOf": self._as_of(now), "depth": depth,
                "bids": bids, "asks": asks, "bestBid": best_bid, "bestAsk": best_ask,
                "spread": round(best_ask - best_bid, 3) if bids and asks else None}

    def _get_price_history(self, params: Mapping[str, str], now: float, exchange_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        ex = self._exchange(exchange_id)
        resolution = _enum_param(params, "resolution", tuple(_RESOLUTIONS), "1h") or "1h"
        res = _RESOLUTIONS[resolution]
        limit = _int_param(params, "limit", 200, 1, 1000)
        start = _time_param(params, "from")
        end = _time_param(params, "to")
        if start is not None:
            start = math.floor(start / res) * res
        if end is not None:
            end = math.floor(end / res) * res
        # Without `to` the window runs through now inclusive, like /price and /trades.
        stop = end if end is not None else math.nextafter(now, math.inf)
        if start is not None and stop <= start:
            raise _invalid("to", "the floored end must be after the floored start")
        trades = self._trades_between(ex, start if start is not None else self.history_start, stop, now)
        candles = self._aggregate(trades, res)
        complete = True
        resp_to = _iso(end) if end is not None else _iso(now)
        if start is None:
            if len(candles) > limit:
                candles = candles[-limit:]
                resp_from = candles[0]["time"]
            else:
                resp_from = _iso(math.floor(self.history_start / res) * res)
        else:
            resp_from = _iso(start)
            if len(candles) > limit:
                complete = False
                resp_to = candles[limit]["time"]  # exclusive continuation boundary
                candles = candles[:limit]
        return {"exchangeId": ex.id, "marketId": ex.market.id, "resolution": resolution, "from": resp_from,
                "to": resp_to, "candles": candles,
                "coverage": {"complete": complete, "projectedThroughSequence": self._sequence(now)}}

    @staticmethod
    def _aggregate(trades: Sequence[_Trade], res: int) -> List[Dict[str, Any]]:
        candles: List[Dict[str, Any]] = []
        bucket: Optional[int] = None
        acc: Dict[str, Any] = {}
        for trade in trades:
            b = math.floor(trade.ts / res) * res
            if b != bucket:
                if bucket is not None:
                    candles.append(DemoMarket._candle(bucket, acc))
                bucket = b
                acc = {"open": trade.price, "high": trade.price, "low": trade.price, "pv": 0.0, "vol": 0, "n": 0}
            acc["high"] = max(acc["high"], trade.price)
            acc["low"] = min(acc["low"], trade.price)
            acc["close"] = trade.price
            acc["pv"] += trade.price * trade.size
            acc["vol"] += trade.size
            acc["n"] += 1
        if bucket is not None:
            candles.append(DemoMarket._candle(bucket, acc))
        return candles

    @staticmethod
    def _candle(bucket: int, acc: Mapping[str, Any]) -> Dict[str, Any]:
        return {"time": _iso(bucket), "open": acc["open"], "high": acc["high"], "low": acc["low"],
                "close": acc["close"], "vwap": round(acc["pv"] / acc["vol"], 6), "volume": acc["vol"],
                "tradeCount": acc["n"]}

    def _get_trades(self, params: Mapping[str, str], now: float, exchange_id: str) -> Dict[str, Any]:
        self._check_tournament(params)
        ex = self._exchange(exchange_id)
        limit = _int_param(params, "limit", 50, 1, 200)
        start = _time_param(params, "from")
        end = _time_param(params, "to")
        if start is not None and end is not None and end <= start:
            raise _invalid("to", "must be after from")
        before = _read_cursor(params.get("cursor"), "tr")
        lo = max(start if start is not None else self.history_start, self.history_start)
        hi = min(end if end is not None else math.inf, now)
        picked: List[_Trade] = []
        hour = math.floor(hi / HOUR)
        while hour >= math.floor(lo / HOUR) and len(picked) <= limit:
            for trade in reversed(self._block(ex, hour)):
                if trade.ts < lo or trade.ts > now or (end is not None and trade.ts >= end):
                    continue
                if before is not None and trade.tid >= before:
                    continue
                picked.append(trade)
                if len(picked) > limit:
                    break
            hour -= 1
        has_more = len(picked) > limit
        page = picked[:limit]
        return {
            "exchangeId": ex.id,
            "marketId": ex.market.id,
            "from": _iso(lo),
            "to": _iso(end) if end is not None else _iso(now),
            "data": [t.to_json() for t in page],
            "pagination": {"limit": limit, "hasMore": has_more,
                           "nextCursor": _cursor("tr", page[-1].tid) if has_more else None},
            "coverage": {"complete": True, "projectedThroughSequence": self._sequence(now)},
        }

    # relationships

    def _get_constraints(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        self._check_tournament(params)
        market_id = params.get("marketId")
        if market_id is not None and market_id != "":
            if not re.fullmatch(r"[1-9]\d*", market_id):
                raise _invalid("marketId", "must be a positive numeric id")
            if market_id not in self._market_by_id:
                raise _Fail(404, "NOT_FOUND", "Market not found in the resolved read context")
        relationship_id = params.get("relationshipId")
        if relationship_id and not _UUID_RE.match(relationship_id):
            raise _invalid("relationshipId", "must be a UUID")
        violations_only = _bool_param(params, "violationsOnly", False)
        raw_min = params.get("minViolation")
        min_violation = 0.01
        if raw_min not in (None, "", "null"):
            try:
                min_violation = float(raw_min)
            except ValueError:
                raise _invalid("minViolation", "must be a number") from None
            if not 0.0 <= min_violation <= 1.0:
                raise _invalid("minViolation", "must be between 0 and 1")
        rows = []
        for rel in self._relationships:
            if market_id and rel["market"].id != market_id:
                continue
            if relationship_id and rel["id"] != relationship_id:
                continue
            row = self._constraint_json(rel, now)
            if violations_only and not (row["evaluationStatus"] == "violated" and row["violationAmount"] >= min_violation):
                continue
            rows.append(row)
        count = sum(1 for r in rows if r["evaluationStatus"] == "violated" and r["violationAmount"] >= min_violation)
        return {"data": rows, "violationsCount": count, "computedAt": _iso(now)}

    def _constraint_json(self, rel: Mapping[str, Any], now: float) -> Dict[str, Any]:
        market: _Market = rel["market"]
        observed = []
        total = 0.0
        for ex in market.exchanges:
            quote = self._quote(ex, now)
            price = quote[0] if quote else (self._latest(ex, now) or 0.0)
            total += price
            observed.append({"exchangeId": ex.id, "price": price, "marketId": market.id, "marketTitle": market.title,
                             "outcome": ex.option, "currentPrice": self._latest(ex, now)})
        total = round(total, 4)
        if rel["type"] == "complementary":
            violation = abs(total - 1.0)
            lower, upper, rule = 1.0, 1.0, "sum(P(outcome)) = 1"
        else:
            violation = max(0.0, total - 1.0)
            lower, upper, rule = None, 1.0, "sum(P(outcome)) <= 1"
        violation = round(violation, 4) if violation > 1e-6 else 0.0
        status = "violated" if violation > 0 else "satisfied"
        trades = []
        if status == "violated":
            action = "sell" if total > 1.0 else "buy"
            for ob in observed:
                trades.append({
                    "exchangeId": ob["exchangeId"], "outcomeSide": "YES", "action": action,
                    "rationale": (f"Outcome prices sum to {total:.3f}; {action} YES on every leg "
                                  f"(a full set settles at exactly 1)."),
                    "marketId": market.id, "marketTitle": market.title, "outcome": ob["outcome"],
                    "currentPrice": ob["currentPrice"],
                })
        ids = [ex.id for ex in market.exchanges]
        reason = (f"{market.title} outcomes sum to {total:.3f}" +
                  (f", {violation:.3f} outside the bound." if status == "violated" else ", within the bound."))
        return {
            "relationshipId": rel["id"],
            "type": rel["type"],
            "violationAmount": violation,
            "reason": reason,
            "suggestedCorrectiveTrades": trades,
            "observedPrices": observed,
            "bound": 1.0,
            "lowerBound": lower,
            "upperBound": upper,
            "currentValue": total,
            "evaluationStatus": status,
            "diagnostic": None,
            "tournamentId": self.tournament_id,
            "direction": {"kind": "symmetric", "fromExchangeIds": ids, "toExchangeIds": ids,
                          "description": "Every outcome constrains every other."},
            "constraint": {"description": rel["description"], "priceRule": rule,
                           "boundSemantics": "Bounds apply to the sum of YES prices across the outcomes.",
                           "evaluationEndpoint": "/api/v1/relationships/constraints"},
        }

    # realtime

    def _post_realtime_token(self, params: Mapping[str, str], now: float) -> Dict[str, Any]:
        token = base64.urlsafe_b64encode(f"demo:{_ME_ID}:{int(now)}".encode()).decode().rstrip("=")
        return {"token": f"demo.{token}.offline", "expiresAt": _iso(now + 3 * HOUR),
                "supabaseUrl": "https://realtime.supermarket-demo.invalid", "anonKey": "demo-anon-key",
                "channels": {"user": f"user:{_ME_ID}"}}


_SEG = r"(?P<{}>[^/]+)"
_ROUTES: List[Tuple[str, Any, str]] = [
    (verb, re.compile(pattern.format(slug=_SEG.format("slug"), market_id=_SEG.format("market_id"),
                                     exchange_id=_SEG.format("exchange_id"))), name)
    for verb, pattern, name in (
        ("GET", "/account", "_get_account"),
        ("GET", "/tournaments", "_get_tournaments"),
        ("GET", "/tournaments/{slug}", "_get_tournament"),
        ("GET", "/tournaments/{slug}/markets", "_get_tournament_markets"),
        ("GET", "/tournaments/{slug}/markets/{market_id}", "_get_tournament_market"),
        ("GET", "/tournaments/{slug}/leaderboard", "_get_tournament_leaderboard"),
        ("GET", "/tournaments/{slug}/portfolio/positions", "_get_positions"),
        ("GET", "/tournaments/{slug}/portfolio/pnl", "_get_pnl"),
        ("GET", "/leaderboards", "_get_leaderboards"),
        ("GET", "/markets", "_get_markets"),
        ("GET", "/markets/{market_id}", "_get_market"),
        ("GET", "/markets/{market_id}/nodes", "_get_market_nodes"),
        ("GET", "/markets/{market_id}/orderbook", "_get_market_orderbook"),
        ("GET", "/exchanges", "_get_exchanges"),
        ("GET", "/exchanges/prices", "_get_prices"),
        ("GET", "/exchanges/{exchange_id}/price", "_get_price"),
        ("GET", "/exchanges/{exchange_id}/orderbook", "_get_orderbook"),
        ("GET", "/exchanges/{exchange_id}/price-history", "_get_price_history"),
        ("GET", "/exchanges/{exchange_id}/trades", "_get_trades"),
        ("GET", "/relationships/constraints", "_get_constraints"),
        ("POST", "/realtime/token", "_post_realtime_token"),
    )
]


# --------------------------------------------------------------------------- outside fair values


class DemoFairValueProvider:
    """The demo's outside fair-value provider (docs/PAPER_TRADING.md §4.9): one EXACT, fully confident quote
    per scripted outcome (``DemoMarket.fair_value_feed``) with bid/ask = feed -/+ 0.005, fetched "now".
    No HTTP (``requests`` 0); deterministic in (seed, exchange, now); never raises."""

    name = "demo"

    def __init__(self, market: DemoMarket) -> None:
        self.market = market

    def refresh(self, targets: Sequence[Any], now: float, *, deadline: Optional[float] = None) -> Any:
        from .fairvalue import ProviderResult

        quotes: Dict[str, FairValueQuote] = {}
        try:
            for target in targets:
                eid = str(getattr(target, "exchange_id", target))
                value = self.market.fair_value_feed(eid, float(now))
                if value is None:
                    continue
                title = getattr(target, "title", "") or ""
                quotes[eid] = FairValueQuote(
                    venue="demo", external_id=f"demo:{eid}", label=f"Demo outside market: {title}".strip(),
                    bid=round(max(0.0, value - DEMO_FEED_HALF_SPREAD), 6), ask=round(min(1.0, value + DEMO_FEED_HALF_SPREAD), 6),
                    last=round(value, 6), fetched_at=float(now), match_kind="EXACT", match_confidence=1.0,
                )
        except Exception as exc:  # providers never raise
            log.warning("demo fair-value feed failed: %s", exc)
            return ProviderResult(venue="demo", status="error", errors=["The demo fair-value feed failed."])
        return ProviderResult(venue="demo", status="ok", quotes=quotes, requests=0)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- news


class DemoNewsProvider(NewsProvider):
    """Canned headlines for the demo: the scripted news story plus unrelated noise.

    A query matches a story when it contains every one of the story's terms (e.g.
    "pennsylvania"); queries that match nothing get a couple of unrelated political
    headlines. Articles are never "published" after the demo's current time, and the
    ``since``/``until`` window is honoured. Never touches the network and never raises.
    """

    name = "demo-news"
    min_interval_s = 0.0

    def __init__(self, market: DemoMarket) -> None:
        self.market = market
        self.queries: List[str] = []

    def search(self, query: str, since: Optional[float], until: Optional[float], limit: int = 20) -> List[Article]:
        try:
            self.queries.append(query)
            now = self.market.now()
            tokens = set(_WORD_RE.findall((query or "").lower()))
            stories = getattr(self.market, "_stories", [])
            picked: List[Article] = []
            for match, articles in stories:
                if match and tokens and all(term in tokens for term in match):
                    picked.extend(articles)
            if not picked:
                picked = [a for match, articles in stories if not match for a in articles]
            out = []
            for art in picked:
                ts = art.published_at
                if ts is not None and (ts > now or (since is not None and ts < since) or (until is not None and ts > until)):
                    continue
                out.append(Article(title=art.title, url=art.url, source=art.source, published_at=ts,
                                   summary=art.summary, provider=self.name))
            out.sort(key=lambda a: -(a.published_at or 0.0))
            return out[: max(0, int(limit))]
        except Exception as exc:  # providers never raise
            log.warning("demo news search failed: %s", exc)
            return []
