"""Outside-move alerts: is the Cup delayed behind Polymarket and Kalshi? (docs/OUTSIDE_MOVES.md, package "core").

Read-only and informational. The watcher polls the outside venues for every MATCHED Cup outcome about every
15 s (``poll`` on the fair-value providers, GET only, their own per-host budgets: never the Super Market
budget), keeps a short per-venue price series, flags significant moves (several windows, thresholds in
probability points AND relative to the outcome's own recent outside volatility, with noise filters), compares
each move with the Cup at the same moment, classifies it ("Cup lagging", "Cup already moved", "Cup moved
first", "outside reverted"), suggests a concrete hand trade for a lagging alert (a suggestion only), and
measures whether and when the Cup followed (the lag study), so the student can test the belief that the Cup
lags with numbers, sample sizes and an honest sentence.

Nothing here places an order or sends anything but GET requests (through the providers'
``readonly.outside_client``). Every time is the injected clock's; every outside quote carries the time its
response ARRIVED (stamped on completion, docs/PAPER_TRADING.md lookahead-2); a decision at ``t`` uses only
samples fetched by ``t``.

Threading (§12 of the spec): ``OutsideMoveWatcher`` has one lock, a leaf: it is never held while calling the
FairValueService, a provider, the Cup feed (the tracker), the persistence or a listener. Readers
(``summary()``, ``status()``) read an immutable published dict and never take the lock.

The dataclasses, constants, texts and Protocols are the shared contract of docs/OUTSIDE_MOVES.md (frozen; core added
only optional fields with defaults: ``VenueMove.t_half``, ``MoveAlert.opened_status`` / ``opened_trade`` /
``opened_trade_note``). Tests: tests/test_moves.py.
"""

from __future__ import annotations

import bisect
import copy
import dataclasses
import json
import logging
import math
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from .models import BookObservation, FairValueQuote, RaceRef, _Serializable, HUMAN_LATENCY_S, iso_ts

log = logging.getLogger("supermarket_bot")

# --------------------------------------------------------------------------- constants (docs/OUTSIDE_MOVES.md §5-§10)

# ---- polling (§4)
MOVES_POLL_S = 15.0  # outside poll cadence: 4 reads a minute keep a 1-min window evaluable with 2 confirming samples
MOVES_POLL_MIN_S = 5.0  # --moves-poll bounds
MOVES_POLL_MAX_S = 120.0
MOVES_POLL_DEADLINE_S = 10.0  # one provider's poll starts no request after this (an 8-s request still finishes)
MOVES_POLL_LOCK_WAIT_S = 3.0  # a poll waits at most this long for a running fair-value refresh of the same provider
MOVES_POLL_HEADROOM = 8  # per host: a poll batch starts only while >= this + 1 of the host's minute slots are free

# ---- series and volatility (§5)
MOVES_WINDOWS_S: Tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)
# minimum move per window in probability points (0.03 = 3 points): a smaller move sits inside a typical Cup spread
# plus both venues' half-spreads and is not tradeable by hand
MOVES_ABS_MIN: Dict[float, float] = {60.0: 0.03, 300.0: 0.04, 900.0: 0.05, 3600.0: 0.07}
MOVES_K_SIGMA = 4.0  # ... and at least this many of the outcome's own recent outside sigmas over the same window
MOVES_WARMUP_FACTOR = 1.5  # sigma unknown (fewer than MOVES_VOL_MIN_PAIRS pairs): the minimum x this
MOVES_VOL_LOOKBACK_S = 6 * 3600.0  # minute bars used for sigma(W)
MOVES_VOL_MIN_PAIRS = 30  # bar pairs needed before sigma(W) is "known"
MOVES_VOL_FLOOR = 0.005  # sigma is never below one tick
MOVES_VOL_WINSOR = 0.10  # each W-change is clipped to +/- this before the RMS (one news jump does not set the scale)
MOVES_VOL_REFRESH_S = 300.0  # sigma(W) per series is recomputed at most this often (cached)
MOVES_BAR_S = 60.0  # minute bars: the last sample of each minute
MOVES_BAR_FILL_S = 360.0  # a bar is forward-filled at most this long (covers the 300-s stored heartbeat)
MOVES_RAW_KEEP_S = 75 * 60.0  # raw samples kept in memory (the 60-min window + its base tolerance + confirmation)

# ---- detection and filters (§6)
MOVES_CONFIRM_SAMPLES = 2  # a move must hold on the last 2 samples (no single-print alerts) ...
MOVES_CONFIRM_MIN_SPAN_S = 10.0  # ... that are at least this far apart
MOVES_BASE_TOLERANCE = 0.25  # the base sample lies in [now - W - max(2 * poll_s, 0.25 * W), now - W]
MOVES_STALE_POLLS = 3.0  # a venue's latest sample older than max(3 * poll_s, MOVES_STALE_MIN_S) is stale
MOVES_STALE_MIN_S = 45.0
MOVES_GAP_MIN_S = 60.0  # two consecutive samples inside a window further apart than max(3 * poll_s, 60): "gap"
MOVES_MAX_SPREAD = 0.05  # an outside spread wider than this at either end of a move: "thin"
MOVES_MIN_LIQUIDITY_USD = 5000.0  # Polymarket liquidityNum below this: "thin"
MOVES_MIN_TOP_SIZE = 100.0  # Kalshi / demo: fewer contracts at either touch: "thin"
MOVES_CONFIRM_FRACTION = 0.5  # two venues: the weaker must have moved >= this x its own threshold, the same way
MOVES_MIN_MATCH_CONFIDENCE = 0.8  # = fairvalue.MIN_MATCH_CONFIDENCE
MOVES_SUSPECT_GAP = 0.25  # = fairvalue.SUSPECT_GAP: |outside - Cup mid| above this (not confirmed): "suspect"

# ---- Cup comparison and classification (§8)
MOVES_CUP_MAX_AGE_S = 120.0  # a Cup snapshot older than this is not "the Cup at t"
MOVES_FOLLOW_FRACTION = 0.5  # the Cup "followed" when it moved at least half as far the same way ...
MOVES_FOLLOW_CONFIRM = 2  # ... on this many consecutive Cup snapshots
MOVES_CUP_LEAD_LOOKBACK_S = 900.0  # "Cup moved first" looks this far back before the outside move's window
MOVES_CUP_FIRST_MARGIN_S = 60.0  # ... and needs the Cup's half-way time at least this much earlier than the outside's
MOVES_REVERT_FRACTION = 0.5  # "outside reverted": it gave back at least half of its peak move (2 samples)
MOVES_CONVERGE_FRACTION = 0.5  # a move that leaves |outside - Cup| < this x the move went TOWARD the Cup

# ---- hand trade (§9)
MOVES_MIN_EDGE = 0.01  # a suggested limit keeps >= 1 cent per share after the Cup half-spread and the uncertainty
MOVES_BOOK_MAX_AGE_S = 120.0  # a Cup book older than this does not give "shares available"
MOVES_BOOK_DEPTH = 20
MOVES_BOOK_READS_PER_MIN = 4  # Cup order-book reads for lagging alerts (tracker budget, behind the read reserve)

# ---- lag study (§10)
MOVES_LAG_TRACK_S = 3600.0  # a lagging move the Cup has not followed within this is "not followed"
MOVES_FOLLOW_BUCKETS_S: Tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)
MOVES_HUMAN_DELAY_S = HUMAN_LATENCY_S  # 240 s: "capturable gap" is measured this long after the alert
MOVES_EXIT_AFTER_S = 1800.0  # "exit_30m": bought at the ask after MOVES_HUMAN_DELAY_S, sold at the bid this later
MOVES_MIN_SAMPLE = 20  # fewer resolved outside-led moves than this, or ...
MOVES_MIN_RACES = 10  # ... fewer distinct races: the summary says the sample is too small
MOVES_CI_MIN_RACES = 5  # a 90% interval (over per-race means) only from this many races

# ---- alert lifecycle (§7)
MOVES_CLOSE_QUIET_S = 300.0  # a resolved alert closes after this long without growth
MOVES_GROWTH_STEP = 0.005  # the peak "grew" when it rose by at least one tick
MOVES_LINK_S = 120.0  # alerts on legs of one race opened this close together are linked
MOVES_RESUME_GAP_S = 600.0  # a watcher gap longer than this censors every pending lag (restart, stall)
MOVES_SUPPRESS_REPEAT_S = 300.0  # the same (outcome, reason) suppressed again within this counts once

# ---- persistence and output (§11)
MOVES_QUOTE_RECORD_MIN_CHANGE = 0.005  # a sample is stored when bid, ask or value moved >= one tick, or ...
MOVES_QUOTE_HEARTBEAT_S = 300.0  # ... this long after the last stored one of its series
MOVES_ALERTS_FILE = "alerts.jsonl"  # data/<slug>/alerts.jsonl: one JSON object per opened / status / closed event
MOVES_MAX_ALERTS_API = 100  # alerts in summary() (open first)
MOVES_MAX_SUPPRESSED_SHOWN = 20
MOVES_MAX_ACTIONABLE_STATUS = 10  # actionable alerts listed in status() (notifications)
MOVES_HISTORY_KEEP_S = 30 * 86400.0  # alerts kept in memory and loaded at start (= store retention)

# ---- vocabularies
MOVE_STATUSES: Tuple[str, ...] = ("lagging", "already_moved", "moved_first", "reverted")
MOVE_STATUS_LABELS: Dict[str, str] = {
    "lagging": "Cup lagging",
    "already_moved": "Cup already moved",
    "moved_first": "Cup moved first",
    "reverted": "Outside reverted",
}
MOVE_STATES: Tuple[str, ...] = ("open", "closed")
LAG_OUTCOMES: Tuple[str, ...] = ("pending", "followed", "reverted", "not_followed", "cup_first", "censored", "excluded")
SUPPRESS_REASONS: Tuple[str, ...] = (
    "thin", "single_tick", "disagree", "near", "suspect", "stale", "placeholder", "match", "no_cup",
)
SUPPRESS_LABELS: Dict[str, str] = {
    "thin": "thin or wide outside book",
    "single_tick": "a single print that did not hold",
    "disagree": "the other venue did not move",
    "near": "near match (different settlement wording)",
    "suspect": "suspect match (far from the Cup price)",
    "stale": "stale or interrupted outside quotes",
    "placeholder": "placeholder or one-sided outside quote",
    "match": "match confidence too low",
    "no_cup": "no recent Cup price to compare with",
}
VENUE_LABELS: Dict[str, str] = {
    "polymarket": "Polymarket", "kalshi": "Kalshi", "demo-a": "Demo venue A", "demo-b": "Demo venue B",
}
# +/- of a venue price as a probability (fees, rounding); = fairvalue.FEE_BAND plus the demo venues
MOVES_FEE_BAND: Dict[str, float] = {"polymarket": 0.01, "kalshi": 0.02, "demo-a": 0.0, "demo-b": 0.0}
VENUE_STATUSES: Tuple[str, ...] = ("pending", "ok", "partial", "busy", "offline", "backoff", "error", "disabled")

# ---- texts (exact; §21)
MOVES_CAVEATS: Tuple[str, ...] = (
    "Outside prices can be wrong, thin or briefly stale: an alert is a prompt to look, not a signal to trade blindly.",
    "A gap the Cup has not closed may stay open until the race is decided; a move the Cup has not followed is not a "
    "guaranteed profit.",
    "You act by hand, minutes later: other participants and bots may move the Cup first, and your own order moves a "
    "thin Cup book.",
    "Polymarket's prices carry no quote time, so every outside quote is stamped with the time we received it; a move "
    "may have happened up to one poll (about 15 s) earlier.",
    "The Cup is read once per snapshot interval (30 s by default), so when it followed is known only to within that "
    "interval: a measured lag can be up to one interval longer than the real one, and 'followed within 1 min' is "
    "rough.",
    "Lag statistics from a few days are a small sample, and moves cluster on news days and share national swings: "
    "treat them as a hint, not proof.",
    "The bot is read-only: it never places an order. Every trade suggestion is for you to judge and place by hand.",
)
MOVES_DEMO_CAVEAT = ("Demo data: the outside moves and the Cup's reactions are scripted, so these alerts and lag "
                     "statistics say nothing about the real Cup.")
MOVES_TRADE_NOTE = ("Suggestion only, not a sure thing: the outside price can be wrong or thin, the Cup may never "
                    "follow (the gap can last until the race is decided), and others may trade first. Nothing is "
                    "traded by the bot.")
MOVES_OFF_ERROR = ("Outside-move alerts are off: start the dashboard with --fair-value auto and without "
                   "--no-moves to watch Polymarket and Kalshi.")
MOVES_NO_DATA_SENTENCE = "No outside move has been measured yet."
# (integration) {moves_word} / {races_word} are "move"/"moves" and "race"/"races" for n and races; {races} counts the
# races of the same resolved moves as {n} (not the pending ones), so "Only 1 outside move on 1 race" reads right.
MOVES_SMALL_SAMPLE_SENTENCE = (
    "Only {n} outside {moves_word} on {races} {races_word} so far ({followed} followed by the Cup, {never} never, "
    "{cup_first} where the Cup moved first): far too few to say whether the Cup lags. Wait for at least {min_n} moves "
    "on {min_races} races."
)
# (integration) every measured move is still pending or was cut short: nothing is resolved, so no counts to report
MOVES_PENDING_SENTENCE = (
    "No outside move has been resolved yet ({detail}): far too few to say whether the Cup lags. Wait for at least "
    "{min_n} moves on {min_races} races."
)
MOVES_SUMMARY_SENTENCE = (
    "Over {n} outside moves on {races} races, the Cup followed within 5 minutes {p5} of the time and within an hour "
    "{p60}; the median lag was {median} after the outside move ({median_alert} after the alert). Acting 4 minutes "
    "after an alert left an average gap of {capture} per share net of the spread (n={n_capture}); buying then and "
    "selling 30 minutes later averaged {exit} per share (n={n_exit}). Moves cluster on news days and races share "
    "national swings, so treat this as a hint, not proof."
)


# --------------------------------------------------------------------------- types (all JSON via to_dict)


@dataclass
class OutsideSample(_Serializable):
    """One outside venue's quote for one Cup outcome, as polled. ``ts`` is when the batch's RESPONSE ARRIVED
    (our clock; never the request start, never the venue's ``updatedAt``)."""

    exchange_id: str
    venue: str  # "polymarket" | "kalshi" | "demo-a" | "demo-b"
    ts: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    # two-sided mid when 0 < bid < ask < 1 and ask - bid <= fairvalue.MAX_VENUE_SPREAD (0.10); else None (a
    # last-trade-only, placeholder or one-sided quote is never a move sample)
    value: Optional[float] = None
    spread: Optional[float] = None
    bid_size: Optional[float] = None  # contracts at the touch (Kalshi, demo); None when the venue does not say
    ask_size: Optional[float] = None
    liquidity: Optional[float] = None  # venue-reported liquidity in USD (Polymarket liquidityNum), else None
    flags: List[str] = field(default_factory=list)  # quote flags + "thin" (is_thin)
    external_id: str = ""
    match_kind: str = "EXACT"  # "EXACT" | "NEAR"
    match_confidence: float = 0.0


@dataclass
class CupQuote(_Serializable):
    """The Cup's quote for one outcome at one tracker snapshot (``ts`` = the bulk-price read's completion)."""

    ts: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None  # (bid + ask) / 2 when two-sided, else the mark
    last: Optional[float] = None
    spread: Optional[float] = None


@dataclass
class VenueMove(_Serializable):
    """One venue's move over one window (§6.1)."""

    venue: str
    label: str
    window_s: float
    base_ts: float
    base_value: float  # median of the up-to-3 samples in the base tolerance band
    after_ts: float
    after_value: float  # the confirming sample nearest the base (conservative)
    now_value: float  # the latest sample
    move: float  # after_value - base_value (signed, YES probability)
    threshold: float
    sigma: Optional[float] = None
    vol_known: bool = False
    ratio: float = 0.0  # |move| / threshold
    spread_base: Optional[float] = None
    spread_now: Optional[float] = None
    thin: bool = False
    stale: bool = False
    gap: bool = False
    flags: List[str] = field(default_factory=list)
    # added by core (optional): this venue's outside half-way time, interpolated on its samples (§6.1); the
    # CombinedMove's t_move is the mean over the confirming venues
    t_half: Optional[float] = None


@dataclass
class CombinedMove(_Serializable):
    """The outcome-level move of one window after the venue rules (§6.3); internal, also kept on alerts."""

    exchange_id: str
    window_s: float
    direction: int  # +1 (YES up) | -1
    venues: List[str]  # venues that confirm the move
    confirmation: str  # "two venues" | "one venue"
    venue_moves: List[VenueMove]
    t_base: float  # the latest base_ts of the confirming venues
    t_move: float  # outside half-way time (interpolated on the samples, mean over the confirming venues)
    before: float  # mean base value of the confirming venues
    after: float  # mean conservative after value
    now: float  # mean latest value
    move: float  # direction x (after - before) > 0
    threshold: float  # the largest threshold among the confirming venues
    sigma: Optional[float]
    vol_known: bool
    uncertainty: float  # max(agreement, tightest half-spread, largest MOVES_FEE_BAND of the venues)


@dataclass
class CupContext(_Serializable):
    """The Cup around a move (§8): at the base, at the look-back reference, and now."""

    base: Optional[CupQuote] = None  # the latest Cup snapshot at or before t_base
    ref: Optional[CupQuote] = None  # the latest at or before t_base - MOVES_CUP_LEAD_LOOKBACK_S (or the earliest)
    now: Optional[CupQuote] = None  # the latest at or before the evaluation time (<= MOVES_CUP_MAX_AGE_S old)
    move_same_window: Optional[float] = None  # direction x (now.mid - base.mid)
    move_lookback: Optional[float] = None  # direction x (now.mid - ref.mid)
    half_cross_ts: Optional[float] = None  # first confirmed snapshot where the move from ref reached half the outside move
    short_history: bool = False  # the reference had to be the earliest snapshot (less Cup history than the look-back)


@dataclass
class HandTrade(_Serializable):
    """A concrete hand trade for a lagging alert (§9). A suggestion only."""

    side: str  # "yes" | "no" (buying NO = selling YES at 1 - price)
    action: str  # "buy"
    text: str  # "Buy YES at 0.565"
    limit: float  # the side's best ask now, on the 0.005 tick
    max_limit: float  # the highest limit that still keeps MOVES_MIN_EDGE after the half-spread and the uncertainty
    outside_value: float  # the side's conservative outside value
    uncertainty: float
    cup_half_spread: float
    edge_per_share: float  # outside_value - cup_half_spread - limit: if the Cup catches up and you sell at its bid
    edge_after_uncertainty: float  # edge_per_share - uncertainty
    edge_at_resolution: float  # outside_value - limit: if the outside price is right and you hold to the end
    shares_at_limit: Optional[float] = None  # contract shares offered at <= limit (None: no fresh book)
    shares_to_max: Optional[float] = None  # ... at <= max_limit
    book_source: Optional[str] = None  # "read" (the watcher read it) | "stored" (the tracker's book) | None
    book_age_s: Optional[float] = None
    computed_at: float = 0.0
    note: str = MOVES_TRADE_NOTE


@dataclass
class MoveLag(_Serializable):
    """Whether and when the Cup followed one outside move (§10)."""

    eligible: bool = True  # False: excluded from the lag study (converging move, no Cup data at detection)
    outcome: str = "pending"  # LAG_OUTCOMES
    t_move: Optional[float] = None  # the outside move's half-way time
    followed_at: Optional[float] = None  # first of the MOVES_FOLLOW_CONFIRM snapshots
    lag_s: Optional[float] = None  # followed_at - t_move (cup_first: the Cup's half-way time - t_move, negative)
    lag_after_alert_s: Optional[float] = None  # followed_at - detected_at
    resolved_at: Optional[float] = None
    reason: str = ""  # one sentence
    capture_4m: Optional[float] = None  # direction x (outside - Cup mid) - Cup spread, MOVES_HUMAN_DELAY_S after the alert
    capture_at: Optional[float] = None
    exit_30m: Optional[float] = None  # buy at the ask then, sell at the bid MOVES_EXIT_AFTER_S later (per share)
    exit_at: Optional[float] = None
    excluded_reason: Optional[str] = None


@dataclass
class MoveAlert(_Serializable):
    """One outside move of one Cup outcome: opened once, updated in place (debounce), closed once (§7)."""

    alert_id: str  # "mv-<exchange_id>-<int(t_base)>"
    exchange_id: str
    market_id: str
    title: str
    option: Optional[str]
    race_key: Optional[str]
    party: Optional[str]
    race_label: str  # "New Hampshire Senate (D)"
    direction: int
    window_s: float  # the shortest window that triggered first
    windows: List[float]
    venues: List[str]
    confirmation: str
    venue_moves: List[VenueMove]
    t_base: float
    t_move: float
    outside_before: float
    outside_after: float  # at detection (conservative)
    outside_now: float
    move: float  # at detection, > 0
    peak_move: float
    threshold: float
    sigma: Optional[float]
    vol_known: bool
    uncertainty: float
    detected_at: float
    updated_at: float
    grown_at: float
    state: str  # MOVE_STATES
    status: str  # MOVE_STATUSES
    status_label: str
    reason: str  # one sentence: why this status
    cup: CupContext
    lag_gap: float  # move - Cup move over the same window, at detection (points of the move not in the Cup)
    lag: MoveLag
    closed_at: Optional[float] = None
    cup_now: Optional[CupQuote] = None
    lag_gap_now: Optional[float] = None  # peak_move - direction x (Cup now - Cup at base)
    level_gap_now: Optional[float] = None  # direction x (outside_now - Cup mid now)
    trade: Optional[HandTrade] = None
    trade_note: Optional[str] = None  # why there is no trade suggestion (when status is lagging and trade is None)
    actionable: bool = False  # open, lagging and a trade suggestion exists (the nav badge and notifications)
    linked: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)  # "one venue", "warm-up thresholds", "gap", "short Cup history", ...
    demo: bool = False
    # added by core (optional): the status when the alert was opened (the lag study's captures and the CLI's
    # --only-lagging use it; the status itself changes, e.g. lagging -> already_moved after a follow)
    opened_status: str = ""
    # added by core (optional): the trade suggestion (or why there was none) when the alert opened. ``trade`` is the
    # live suggestion and is cleared when the alert closes; these keep what the "opened" event said, so a replay of
    # the stored alert prints the same suggestion (``format_event_lines`` prefers them for an "opened" event)
    opened_trade: Optional[HandTrade] = None
    opened_trade_note: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MoveAlert":
        """Rebuild an alert from its ``to_dict()`` (persistence, restart). Unknown keys are ignored."""
        raw = dict(data)
        kw = _known(cls, raw)
        kw["venue_moves"] = [_build(VenueMove, v) for v in raw.get("venue_moves") or [] if isinstance(v, Mapping)]
        cup = raw.get("cup") if isinstance(raw.get("cup"), Mapping) else {}
        ckw = _known(CupContext, cup)
        for key in ("base", "ref", "now"):
            ckw[key] = _cup_quote(cup.get(key))
        kw["cup"] = CupContext(**ckw)
        kw["lag"] = _build(MoveLag, raw.get("lag") if isinstance(raw.get("lag"), Mapping) else {})
        kw["cup_now"] = _cup_quote(raw.get("cup_now"))
        kw["trade"] = _build(HandTrade, raw["trade"]) if isinstance(raw.get("trade"), Mapping) else None
        kw["opened_trade"] = (_build(HandTrade, raw["opened_trade"]) if isinstance(raw.get("opened_trade"), Mapping)
                              else None)
        for key in ("windows", "venues", "linked", "flags"):
            kw[key] = list(raw.get(key) or [])
        kw["windows"] = [float(w) for w in kw["windows"]]
        return cls(**kw)


@dataclass
class SuppressedMove(_Serializable):
    """A would-be alert a noise filter dropped (shown for transparency, §6.2)."""

    exchange_id: str
    title: str
    race_label: str
    reason: str  # SUPPRESS_REASONS
    reason_label: str
    at: float
    window_s: float
    venues: List[str]
    move: float  # signed
    threshold: float
    detail: str = ""  # one sentence


@dataclass
class MoveEvent(_Serializable):
    """What listeners (the CLI) and alerts.jsonl receive."""

    kind: str  # "opened" | "status" | "closed"
    at: float
    alert_id: str
    status: str
    lag_outcome: str
    alert: Dict[str, Any]  # MoveAlert.to_dict()


@dataclass
class LagSummary(_Serializable):
    """The lag study over every stored alert (§10.2). Shares are of ``outside_led``; null when it is 0."""

    moves: int = 0  # lag-study (eligible) alerts, resolved or not
    races: int = 0  # distinct race keys among the RESOLVED ones (the moves the sentence counts; not pending/censored)
    resolved: int = 0
    pending: int = 0
    censored: int = 0
    excluded: int = 0  # converging moves and moves without Cup data (not in the study)
    outside_led: int = 0  # resolved and not cup_first
    cup_first: int = 0
    cup_first_share: Optional[float] = None  # of resolved
    followed: int = 0
    followed_within: Dict[str, Optional[float]] = field(default_factory=dict)  # "60" | "300" | "900" | "3600" -> share
    followed_within_n: Dict[str, int] = field(default_factory=dict)
    reverted: int = 0
    not_followed: int = 0
    never_followed: int = 0  # reverted + not_followed
    never_followed_share: Optional[float] = None
    reverted_share: Optional[float] = None
    not_followed_share: Optional[float] = None
    median_lag_s: Optional[float] = None
    median_lag_after_alert_s: Optional[float] = None
    lag_quartiles_s: Optional[List[float]] = None  # [q1, q3]
    capture_4m: Dict[str, Any] = field(default_factory=dict)  # {"n", "mean", "median", "positive_share", "ci90"}
    exit_30m: Dict[str, Any] = field(default_factory=dict)
    small_sample: bool = True
    sentence: str = MOVES_NO_DATA_SENTENCE
    since: Optional[float] = None  # the oldest alert counted
    window: str = "every stored alert (up to 30 days)"
    outside_led_races: int = 0  # (integration) distinct race keys among the outside-led moves the shares are over


@dataclass
class VenueStatus(_Serializable):
    """One venue's line in the view (§19.2)."""

    venue: str
    label: str
    status: str  # VENUE_STATUSES
    last_ok_at: Optional[float] = None  # only once the venue answered a poll
    last_poll_at: Optional[float] = None
    last_error: Optional[str] = None  # the real cause first (never the backoff text)
    next_try_at: Optional[float] = None
    requests_last_poll: int = 0
    budget_used: Optional[int] = None  # the host limiter's slots used in the last minute (poll + fair-value refresh)
    budget_limit: Optional[int] = None
    matched: int = 0  # validated matches polled
    quoted: int = 0  # usable two-sided samples in the last poll
    stale: int = 0  # matched outcomes whose latest sample is stale


@dataclass
class MoveParams(_Serializable):
    """Every tunable of the watcher (defaults = the constants above; tests override)."""

    poll_s: float = MOVES_POLL_S
    poll_deadline_s: float = MOVES_POLL_DEADLINE_S
    windows_s: Tuple[float, ...] = MOVES_WINDOWS_S
    abs_min: Dict[float, float] = field(default_factory=lambda: dict(MOVES_ABS_MIN))
    k_sigma: float = MOVES_K_SIGMA
    warmup_factor: float = MOVES_WARMUP_FACTOR
    vol_lookback_s: float = MOVES_VOL_LOOKBACK_S
    vol_min_pairs: int = MOVES_VOL_MIN_PAIRS
    vol_floor: float = MOVES_VOL_FLOOR
    vol_winsor: float = MOVES_VOL_WINSOR
    confirm_samples: int = MOVES_CONFIRM_SAMPLES
    confirm_min_span_s: float = MOVES_CONFIRM_MIN_SPAN_S
    max_spread: float = MOVES_MAX_SPREAD
    min_liquidity_usd: float = MOVES_MIN_LIQUIDITY_USD
    min_top_size: float = MOVES_MIN_TOP_SIZE
    confirm_fraction: float = MOVES_CONFIRM_FRACTION
    min_match_confidence: float = MOVES_MIN_MATCH_CONFIDENCE
    suspect_gap: float = MOVES_SUSPECT_GAP
    cup_max_age_s: float = MOVES_CUP_MAX_AGE_S
    follow_fraction: float = MOVES_FOLLOW_FRACTION
    follow_confirm: int = MOVES_FOLLOW_CONFIRM
    cup_lead_lookback_s: float = MOVES_CUP_LEAD_LOOKBACK_S
    cup_first_margin_s: float = MOVES_CUP_FIRST_MARGIN_S
    revert_fraction: float = MOVES_REVERT_FRACTION
    converge_fraction: float = MOVES_CONVERGE_FRACTION
    min_edge: float = MOVES_MIN_EDGE
    book_max_age_s: float = MOVES_BOOK_MAX_AGE_S
    lag_track_s: float = MOVES_LAG_TRACK_S
    human_delay_s: float = MOVES_HUMAN_DELAY_S
    exit_after_s: float = MOVES_EXIT_AFTER_S
    close_quiet_s: float = MOVES_CLOSE_QUIET_S
    resume_gap_s: float = MOVES_RESUME_GAP_S
    min_sample: int = MOVES_MIN_SAMPLE
    min_races: int = MOVES_MIN_RACES

    @property
    def stale_s(self) -> float:
        return max(MOVES_STALE_POLLS * self.poll_s, MOVES_STALE_MIN_S)

    @property
    def gap_s(self) -> float:
        return max(MOVES_STALE_POLLS * self.poll_s, MOVES_GAP_MIN_S)

    def base_tolerance_s(self, window_s: float) -> float:
        return max(2.0 * self.poll_s, MOVES_BASE_TOLERANCE * float(window_s))


@dataclass
class MovesStepReport(_Serializable):
    at: float  # the evaluation time (the clock after every poll returned)
    duration_s: float
    polled: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # venue -> {"status", "requests", "quotes"}
    samples: int = 0
    opened: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    closed: List[str] = field(default_factory=list)
    suppressed: int = 0
    book_reads: int = 0
    errors: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- interfaces


class CupFeed(Protocol):
    """What the watcher may know about the Cup (the tracker implements it as ``tracker.TrackerCupFeed``).
    Every method is safe to call without holding any lock and never takes the watcher's lock."""

    def quotes(self) -> Dict[str, CupQuote]:
        """exchange id -> the latest published snapshot quote of every OPEN outcome (lock-free)."""
        ...

    def history(self, seconds: float) -> Dict[str, List[CupQuote]]:
        """exchange id -> tick snapshots of the last ``seconds``, oldest first (called once, at the first step)."""
        ...

    def book(self, exchange_id: str) -> Optional[BookObservation]:
        """A fresh Cup order book (YES prices) inside the tracker's moves budget, or None when there is no room,
        the read failed, or book reads are disabled. Never waits for the budget."""
        ...

    def stored_book(self, exchange_id: str) -> Optional[BookObservation]:
        """The newest order book the tracker already stored for the outcome (no read), or None."""
        ...


class MovesPersistence(Protocol):
    """Implemented by ``store.TrackerStore`` (schema v3) and :class:`MemoryMovesPersistence`. Plain dicts only."""

    def add_outside_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert samples by (exchange_id, venue, ts). Row keys = OutsideSample.to_dict() keys."""
        ...

    def outside_quotes(self, since: float, until: Optional[float] = None,
                       exchange_ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Stored samples with since <= ts <= until, oldest first (ties by exchange_id, venue)."""
        ...

    def put_move_alerts(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert alerts by alert_id. Each row is ``MoveAlert.to_dict()``."""
        ...

    def move_alerts(self, since: Optional[float] = None, until: Optional[float] = None, state: Optional[str] = None,
                    limit: int = 5000) -> List[Dict[str, Any]]:
        """Stored alerts (their to_dict()), detected_at in [since, until], oldest first, at most ``limit`` (the newest)."""
        ...


class MemoryMovesPersistence:
    """In-memory :class:`MovesPersistence` with the exact semantics of the store (tests, and the contract test
    the store is checked against)."""

    def __init__(self) -> None:
        self._quotes: Dict[Tuple[str, str, float], Dict[str, Any]] = {}
        self._alerts: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add_outside_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        written = 0
        with self._lock:
            for row in rows:
                clean = _quote_row(row)
                self._quotes[(clean["exchange_id"], clean["venue"], clean["ts"])] = clean
                written += 1
        return written

    def outside_quotes(self, since: float, until: Optional[float] = None,
                       exchange_ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        wanted = {str(e) for e in exchange_ids} if exchange_ids is not None else None
        with self._lock:
            rows = [r for (eid, _venue, ts), r in self._quotes.items()
                    if ts >= float(since) and (until is None or ts <= float(until)) and (wanted is None or eid in wanted)]
            out = [copy.deepcopy(r) for r in rows]
        out.sort(key=lambda r: (r["ts"], r["exchange_id"], r["venue"]))
        return out

    def put_move_alerts(self, rows: Sequence[Mapping[str, Any]]) -> int:
        written = 0
        with self._lock:
            for row in rows:
                clean = json.loads(json.dumps(dict(row)))  # the store keeps JSON: the same types come back
                if not clean.get("alert_id"):
                    continue
                self._alerts[str(clean["alert_id"])] = clean
                written += 1
        return written

    def move_alerts(self, since: Optional[float] = None, until: Optional[float] = None, state: Optional[str] = None,
                    limit: int = 5000) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._alerts.values()
                    if (since is None or float(r.get("detected_at") or 0.0) >= float(since))
                    and (until is None or float(r.get("detected_at") or 0.0) <= float(until))
                    and (state is None or r.get("state") == state)]
            rows = [copy.deepcopy(r) for r in rows]
        rows.sort(key=lambda r: (float(r.get("detected_at") or 0.0), str(r.get("alert_id"))))
        n = max(0, int(limit))
        return rows[-n:] if n else []


Listener = Callable[[MoveEvent], Any]


# --------------------------------------------------------------------------- small helpers


_EPS = 1e-9
_TICK = 0.005  # = depth.TICK (the Cup's price tick)
_MAX_VENUE_SPREAD = 0.10  # = fairvalue.MAX_VENUE_SPREAD (a wider quote is not a price; tests check they agree)
_NAN = float("nan")
_SAMPLE_FIELDS = ("exchange_id", "venue", "ts", "bid", "ask", "value", "spread", "bid_size", "ask_size", "liquidity",
                  "flags", "external_id", "match_kind", "match_confidence")
_FILTER_ORDER: Tuple[str, ...] = ("placeholder", "match", "near", "stale", "thin", "single_tick")
_WINDOW_TEXT = {60.0: "1 min", 300.0: "5 min", 900.0: "15 min", 3600.0: "1 h"}
_RESOLVED = ("followed", "reverted", "not_followed", "cup_first")
_CONVERGING = "converging: the outside moved to the Cup's price"
_NO_CUP_BASE = "no Cup price at the start of the move"


def _num(raw: Any) -> Optional[float]:
    """A finite float, else None (bools are not numbers here)."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _r6(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6) + 0.0


def _known(cls: Any, raw: Mapping[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: copy.deepcopy(v) for k, v in raw.items() if k in names}


def _build(cls: Any, raw: Mapping[str, Any]) -> Any:
    return cls(**_known(cls, raw))


def _cup_quote(raw: Any) -> Optional[CupQuote]:
    if isinstance(raw, CupQuote):
        return raw
    if not isinstance(raw, Mapping) or _num(raw.get("ts")) is None:
        return None
    return _build(CupQuote, raw)


def _quote_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """An outside_quotes row with exactly the OutsideSample keys and the store's defaults."""
    out: Dict[str, Any] = {}
    for key in _SAMPLE_FIELDS:
        out[key] = row.get(key)
    out["exchange_id"] = str(out["exchange_id"])
    out["venue"] = str(out["venue"])
    out["ts"] = float(out["ts"])
    for key in ("bid", "ask", "value", "spread", "bid_size", "ask_size", "liquidity"):
        out[key] = _num(out[key])
    out["flags"] = [str(f) for f in (out["flags"] or [])]
    out["external_id"] = str(out["external_id"] or "")
    out["match_kind"] = str(out["match_kind"] or "EXACT")
    out["match_confidence"] = float(_num(out["match_confidence"]) or 0.0)
    return out


# ---- formatting (§17.2: every text of the feature uses these)


def fmt_pts(x: Optional[float], signed: bool = True) -> str:
    """Probability points: 0.055 -> "+5.5 pts" (signed) or "5.5 pts"; "n/a" when unknown."""
    if x is None:
        return "n/a"
    v = round(round(float(x), 6) * 100.0, 1) + 0.0
    return f"{v:+.1f} pts" if signed else f"{v:.1f} pts"


def _pts_num(x: float, signed: bool = True) -> str:
    """The number of points without the unit ("+5.5", "5.5")."""
    v = round(round(float(x), 6) * 100.0, 1) + 0.0
    return f"{v:+.1f}" if signed else f"{v:.1f}"


def fmt_price(p: Optional[float]) -> str:
    return "n/a" if p is None else f"{round(float(p), 6):.3f}"


def fmt_money(x: Optional[float]) -> str:
    """Per-share money, signed with 3 decimals ("+0.008"); "n/a" when unknown."""
    if x is None:
        return "n/a"
    return f"{round(float(x), 6) + 0.0:+.3f}"


def fmt_duration(seconds: Optional[float]) -> str:
    """"40 s" under a minute, "3.5 min" under an hour, else "1.2 h"; "n/a" when unknown."""
    if seconds is None:
        return "n/a"
    s = float(seconds)
    if abs(s) < 60.0:
        return f"{s:.0f} s"
    if abs(s) < 3600.0:
        return f"{s / 60.0:.1f} min"
    return f"{s / 3600.0:.1f} h"


def window_text(window_s: float) -> str:
    """"1 min" / "5 min" / "15 min" / "1 h" (other windows as a duration)."""
    return _WINDOW_TEXT.get(float(window_s), fmt_duration(window_s))


def fmt_share(share: Optional[float]) -> str:
    return "n/a" if share is None else f"{float(share) * 100.0:.0f}%"


def _hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%H:%M:%S")


def _detail(sentence: str) -> str:
    """A reason sentence as a CLI detail line: first letter lower-cased, final period dropped."""
    text = (sentence or "").strip()
    if text.endswith("."):
        text = text[:-1]
    return text[:1].lower() + text[1:] if text else text


def _median(values: Sequence[float]) -> Optional[float]:
    xs = sorted(float(v) for v in values)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear interpolation between order statistics (numpy's default), q in [0, 1]."""
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# --------------------------------------------------------------------------- pure helpers (§5-§10)


def race_label(race: Optional[RaceRef], title: str = "") -> str:
    """"New Hampshire Senate (D)", "AZ-01 House (R)", "U.S. Senate control (D)"; without a race the title."""
    if race is None:
        return title or "an unknown race"
    from . import fairvalue as _fv  # lazy: fairvalue imports this module

    party = race.party if race.party and race.party != "O" else "other"
    return f"{_fv._race_label(race)} ({party})"


def venue_label(venue: str) -> str:
    return VENUE_LABELS.get(venue, venue)


def sample_from_quote(exchange_id: str, quote: FairValueQuote, *, liquidity: Optional[float] = None) -> OutsideSample:
    """An :class:`OutsideSample` from a polled FairValueQuote: ``ts = quote.fetched_at`` (required; a quote
    without it is dropped by the caller), ``value`` per the OutsideSample rule, flags copied."""
    if quote.fetched_at is None:
        raise ValueError("an outside quote without fetched_at is not a sample")
    bid, ask = _num(quote.bid), _num(quote.ask)
    flags = [str(f) for f in (quote.flags or [])]
    value: Optional[float] = None
    if (bid is not None and ask is not None and 0.0 < bid < ask < 1.0 and ask - bid <= _MAX_VENUE_SPREAD + _EPS
            and "placeholder" not in flags):
        value = _r6((bid + ask) / 2.0)
    spread = _r6(ask - bid) if bid is not None and ask is not None else _num(quote.spread)
    return OutsideSample(
        exchange_id=str(exchange_id), venue=str(quote.venue), ts=float(quote.fetched_at), bid=bid, ask=ask, value=value,
        spread=spread, bid_size=_num(quote.bid_size), ask_size=_num(quote.ask_size), liquidity=_num(liquidity),
        flags=flags, external_id=str(quote.external_id or ""), match_kind=str(quote.match_kind or "EXACT"),
        match_confidence=float(_num(quote.match_confidence) or 0.0))


def _thin_parts(spread: Optional[float], liquidity: Optional[float], bid_size: Optional[float],
                ask_size: Optional[float], params: MoveParams) -> Optional[str]:
    """Why a quote is thin ("spread" | "liquidity" | "size"), or None."""
    if spread is not None and spread > params.max_spread + _EPS:
        return "spread"
    if liquidity is not None and liquidity < params.min_liquidity_usd:
        return "liquidity"
    for size in (bid_size, ask_size):
        if size is not None and size < params.min_top_size:
            return "size"
    return None


def is_thin(sample: OutsideSample, params: MoveParams) -> bool:
    """True when the spread is wider than ``max_spread``, Polymarket ``liquidity`` < ``min_liquidity_usd``, or a
    known touch size (bid_size / ask_size) < ``min_top_size``."""
    return _thin_parts(sample.spread, sample.liquidity, sample.bid_size, sample.ask_size, params) is not None


# Raw rows of a SeriesBuffer: one shared tuple per distinct quote (consecutive equal quotes share the object, so a
# quiet series costs ~16 bytes a sample: one double in the ts array and one list slot).
_ROW_BID, _ROW_ASK, _ROW_VALUE, _ROW_SPREAD, _ROW_BID_SIZE, _ROW_ASK_SIZE, _ROW_LIQ, _ROW_FLAGS, _ROW_EXT, _ROW_KIND, \
    _ROW_CONF = range(11)


def _row_of(sample: OutsideSample) -> Tuple[Any, ...]:
    return (_num(sample.bid), _num(sample.ask), _num(sample.value), _num(sample.spread), _num(sample.bid_size),
            _num(sample.ask_size), _num(sample.liquidity), tuple(sample.flags or ()), str(sample.external_id or ""),
            str(sample.match_kind or "EXACT"), float(_num(sample.match_confidence) or 0.0))


def _row_thin(row: Tuple[Any, ...], params: MoveParams) -> bool:
    return "thin" in row[_ROW_FLAGS] or _thin_parts(row[_ROW_SPREAD], row[_ROW_LIQ], row[_ROW_BID_SIZE],
                                                    row[_ROW_ASK_SIZE], params) is not None


class SeriesBuffer:
    """One (outcome, venue) series: raw samples for MOVES_RAW_KEEP_S plus minute bars for MOVES_VOL_LOOKBACK_S
    (compact: one float per minute, e.g. ``array('d')``), and a cached sigma per window (MOVES_VOL_REFRESH_S).
    Memory target: under 40 MB for 237 outcomes x 2 venues. Not thread-safe (the watcher's lock guards it)."""

    def __init__(self, exchange_id: str, venue: str, params: MoveParams) -> None:
        self.exchange_id = exchange_id
        self.venue = venue
        self.params = params
        self._ts = array("d")  # sample times, strictly increasing
        self._rows: List[Tuple[Any, ...]] = []  # one row per sample (shared when equal to the previous one)
        self._bar_start: Optional[float] = None  # the minute (epoch s, a multiple of 60) of _bars[0]
        self._bars = array("d")  # the last valued sample of each minute, forward-filled per minute_bars; NaN = None
        self._last_valued: Optional[Tuple[float, float]] = None  # (ts, value) of the newest valued sample
        self._sigma: Dict[float, Tuple[float, Optional[float]]] = {}  # window -> (computed at, sigma)
        # samples up to this time were restored from the store (§11), which keeps a sample only on a one-tick change
        # or the 300-s heartbeat: their spacing is not an interruption (the venue WAS read; nothing moved a tick)
        self.restored_until: Optional[float] = None

    def interrupted(self, a: float, b: float, gap_s: float) -> bool:
        """True when two consecutive samples at ``a`` < ``b`` are further apart than ``gap_s`` because the venue was
        not read (an outage, a restart): restored samples may be up to one heartbeat (+ gap_s) apart."""
        span = float(b) - float(a)
        if span <= gap_s + _EPS:
            return False
        restored = self.restored_until
        return not (restored is not None and b <= restored + _EPS and span <= MOVES_QUOTE_HEARTBEAT_S + gap_s + _EPS)

    def __len__(self) -> int:
        return len(self._ts)

    def add(self, sample: OutsideSample) -> bool:
        """Append (samples arrive in ts order; an older or duplicate ts is ignored, returns False)."""
        ts = _num(sample.ts)
        if ts is None or (len(self._ts) and ts <= self._ts[-1]):
            return False
        row = _row_of(sample)
        if self._rows and self._rows[-1] == row:
            row = self._rows[-1]
        self._ts.append(ts)
        self._rows.append(row)
        value = row[_ROW_VALUE]
        if value is not None:
            self._add_bar(ts, value)
        return True

    def _add_bar(self, ts: float, value: float) -> None:
        minute = math.floor(ts / MOVES_BAR_S) * MOVES_BAR_S
        if self._bar_start is None:
            self._bar_start = minute
            self._bars = array("d", [value])
        else:
            idx = int(round((minute - self._bar_start) / MOVES_BAR_S))
            n = len(self._bars)
            if idx - n > self.params.vol_lookback_s / MOVES_BAR_S + 2:  # a long silence: start the bars afresh
                self._bar_start = minute
                self._bars = array("d", [value])
            elif idx < n:
                self._bars[idx] = value  # the last sample of the minute wins
            else:
                for k in range(n, idx):
                    self._bars.append(self._fill(self._bar_start + MOVES_BAR_S * (k + 1)))
                self._bars.append(value)
        self._last_valued = (ts, value)

    def _fill(self, bar_end: float) -> float:
        last = self._last_valued
        if last is not None and bar_end - last[0] <= MOVES_BAR_FILL_S + _EPS:
            return last[1]
        return _NAN

    def bars(self, until: float) -> Tuple[Optional[float], List[Optional[float]]]:
        """(start minute, bars) with the tail forward-filled up to the last minute that ended by ``until``;
        equal to :func:`minute_bars` over this series' valued samples."""
        if self._bar_start is None:
            return None, []
        out: List[Optional[float]] = [None if b != b else b for b in self._bars]
        k = len(out)
        while self._bar_start + MOVES_BAR_S * (k + 1) <= until + _EPS:
            fill = self._fill(self._bar_start + MOVES_BAR_S * (k + 1))
            if fill != fill:
                break  # nothing to fill any more
            out.append(fill)
            k += 1
        return self._bar_start, out

    def latest(self) -> Optional[OutsideSample]:
        return self._sample(len(self._ts) - 1) if len(self._ts) else None

    def _sample(self, i: int) -> OutsideSample:
        row = self._rows[i]
        return OutsideSample(
            exchange_id=self.exchange_id, venue=self.venue, ts=self._ts[i], bid=row[_ROW_BID], ask=row[_ROW_ASK],
            value=row[_ROW_VALUE], spread=row[_ROW_SPREAD], bid_size=row[_ROW_BID_SIZE], ask_size=row[_ROW_ASK_SIZE],
            liquidity=row[_ROW_LIQ], flags=list(row[_ROW_FLAGS]), external_id=row[_ROW_EXT], match_kind=row[_ROW_KIND],
            match_confidence=row[_ROW_CONF])

    def samples(self, since: float, until: Optional[float] = None) -> List[OutsideSample]:
        lo = bisect.bisect_left(self._ts, float(since) - _EPS)
        hi = len(self._ts) if until is None else bisect.bisect_right(self._ts, float(until) + _EPS)
        return [self._sample(i) for i in range(lo, hi)]

    def points(self, since: float, until: float) -> List[Tuple[float, Optional[float]]]:
        """(ts, value) of the samples with since <= ts <= until (value None for an unpriced quote)."""
        lo = bisect.bisect_left(self._ts, float(since) - _EPS)
        hi = bisect.bisect_right(self._ts, float(until) + _EPS)
        return [(self._ts[i], self._rows[i][_ROW_VALUE]) for i in range(lo, hi)]

    def value_at(self, t: float, max_age_s: float) -> Optional[Tuple[float, float]]:
        """(ts, value) of the newest valued sample at or before ``t`` and at most ``max_age_s`` old, else None."""
        i = bisect.bisect_right(self._ts, float(t) + _EPS) - 1
        while i >= 0 and t - self._ts[i] <= max_age_s + _EPS:
            value = self._rows[i][_ROW_VALUE]
            if value is not None:
                return self._ts[i], value
            i -= 1
        return None

    def sigma(self, window_s: float, now: float) -> Optional[float]:
        """:func:`window_sigma` over this series' minute bars (cached for MOVES_VOL_REFRESH_S)."""
        key = float(window_s)
        cached = self._sigma.get(key)
        if cached is not None and 0.0 <= now - cached[0] < MOVES_VOL_REFRESH_S:
            return cached[1]
        start, bars = self.bars(now)
        value = window_sigma(bars, start, key, now, self.params) if start is not None else None
        self._sigma[key] = (float(now), value)
        return value

    def prune(self, now: float) -> None:
        cutoff = now - MOVES_RAW_KEEP_S
        drop = bisect.bisect_left(self._ts, cutoff)
        drop = min(drop, len(self._ts) - 1)  # always keep the newest sample (staleness, the next record)
        if drop > 0:
            del self._ts[:drop]
            del self._rows[:drop]
        if self._bar_start is not None:
            keep_from = now - self.params.vol_lookback_s - 2 * MOVES_BAR_S
            k = int((keep_from - self._bar_start) // MOVES_BAR_S)
            if k > 0:
                k = min(k, len(self._bars))
                del self._bars[:k]
                self._bar_start += MOVES_BAR_S * k
                if not len(self._bars):
                    self._bar_start = None if self._last_valued is None else self._bar_start


def minute_bars(samples: Sequence[OutsideSample], start: float, end: float, fill_s: float = MOVES_BAR_FILL_S) -> List[Optional[float]]:
    """One value per minute in [start, end): the last sample value of the minute, else the previous bar's value
    while the last sample is at most ``fill_s`` old, else None."""
    n = max(0, int(math.ceil((float(end) - float(start)) / MOVES_BAR_S - 1e-9)))
    vals = sorted(((float(s.ts), float(s.value)) for s in samples if s.value is not None and _num(s.ts) is not None),
                  key=lambda tv: tv[0])
    out: List[Optional[float]] = []
    j = 0
    last: Optional[Tuple[float, float]] = None
    for i in range(n):
        lo = float(start) + MOVES_BAR_S * i
        hi = lo + MOVES_BAR_S
        got: Optional[float] = None
        while j < len(vals) and vals[j][0] < hi:
            last = vals[j]
            if vals[j][0] >= lo:
                got = vals[j][1]
            j += 1
        if got is not None:
            out.append(got)
        elif last is not None and hi - last[0] <= fill_s + _EPS:
            out.append(last[1])
        else:
            out.append(None)
    return out


def window_sigma(bars: Sequence[Optional[float]], bar_start: float, window_s: float, now: float,
                 params: MoveParams) -> Optional[float]:
    """sigma(W) (§5.2): sqrt(mean((bar(t) - bar(t - W))^2)) over minute t in [now - vol_lookback_s,
    now - W - 60], both bars present, each change clipped to +/- vol_winsor; None when fewer than vol_min_pairs
    pairs; never below vol_floor."""
    lag = max(1, int(round(float(window_s) / MOVES_BAR_S)))
    lo = float(now) - params.vol_lookback_s
    hi = float(now) - float(window_s) - MOVES_BAR_S
    acc = 0.0
    n = 0
    for i in range(lag, len(bars)):
        t = float(bar_start) + MOVES_BAR_S * i
        if t < lo - _EPS or t > hi + _EPS:
            continue
        a, b = bars[i], bars[i - lag]
        if a is None or b is None:
            continue
        change = max(-params.vol_winsor, min(params.vol_winsor, float(a) - float(b)))
        acc += change * change
        n += 1
    if n < params.vol_min_pairs or n == 0:
        return None
    return max(math.sqrt(acc / n), params.vol_floor)


def threshold_for(window_s: float, sigma: Optional[float], params: MoveParams) -> float:
    """max(abs_min[W], k_sigma x sigma) when sigma is known, else abs_min[W] x warmup_factor (§5.3)."""
    w = float(window_s)
    table = {float(k): float(v) for k, v in params.abs_min.items()}
    base = table.get(w)
    if base is None:  # a window outside the table: the minimum of the nearest tabled window
        base = table[min(table, key=lambda k: (abs(k - w), k))]
    if sigma is None:
        return round(base * params.warmup_factor, 9)
    return max(base, params.k_sigma * float(sigma))


def half_cross_time(points: Sequence[Tuple[float, float]], start_value: float, direction: int, amount: float,
                    *, since: float, confirm: int = 1) -> Optional[float]:
    """The first time in ``points`` [(ts, value)] at or after ``since`` where direction x (value - start_value)
    >= amount, held on ``confirm`` consecutive points; linear interpolation between the two points around the
    crossing when ``confirm == 1`` (outside samples), the first confirming point's ts otherwise (Cup snapshots)."""
    pts = [(float(t), float(v)) for t, v in points if v is not None]
    d = 1 if direction >= 0 else -1
    need = float(amount) - _EPS

    def reached(v: float) -> bool:
        return d * (v - start_value) >= need

    prev: Optional[Tuple[float, float]] = None
    for idx, (t, v) in enumerate(pts):
        if t < since - _EPS:
            prev = (t, v)
            continue
        if reached(v):
            if confirm <= 1:
                if prev is not None and not reached(prev[1]) and v != prev[1]:
                    target = start_value + d * float(amount)
                    frac = (target - prev[1]) / (v - prev[1])
                    frac = min(1.0, max(0.0, frac))
                    return prev[0] + frac * (t - prev[0])
                return t
            run = pts[idx:idx + confirm]
            if len(run) == confirm and all(reached(rv) for _, rv in run):
                return t
        prev = (t, v)
    return None


def evaluate_window(series: SeriesBuffer, now: float, window_s: float, params: MoveParams) -> Optional[VenueMove]:
    """One venue's move over ``window_s`` at ``now`` (§6.1), or None when not evaluable (no base sample in the
    tolerance band, fewer than ``confirm_samples`` samples after the base, latest stale). Fills ``thin`` /
    ``stale`` / ``gap`` but does not decide whether to alert."""
    ts, rows = series._ts, series._rows
    end = bisect.bisect_right(ts, float(now) + _EPS)  # only samples fetched by `now` (no look-ahead)
    if end == 0:
        return None
    w = float(window_s)
    tol = params.base_tolerance_s(w)
    lo = bisect.bisect_left(ts, now - w - tol - _EPS)
    hi = min(bisect.bisect_right(ts, now - w + _EPS), end)
    base_idx: List[int] = []
    for i in range(hi - 1, lo - 1, -1):
        if rows[i][_ROW_VALUE] is not None:
            base_idx.append(i)
            if len(base_idx) >= 3:
                break
    if not base_idx:
        return None
    b = base_idx[0]
    base_ts = ts[b]
    base_value = _median([rows[i][_ROW_VALUE] for i in base_idx])
    assert base_value is not None
    last = end - 1
    if ts[last] <= base_ts:
        return None
    placeholder = rows[last][_ROW_VALUE] is None
    # the confirming samples: the newest valued sample, then earlier valued ones at least confirm_min_span_s apart
    picked: List[int] = []
    i = last
    while i > b and len(picked) < max(1, params.confirm_samples):
        if rows[i][_ROW_VALUE] is not None and (not picked or ts[picked[-1]] - ts[i] >= params.confirm_min_span_s - _EPS):
            picked.append(i)
        i -= 1
    if len(picked) < max(1, params.confirm_samples):
        return None
    latest_v = rows[picked[0]][_ROW_VALUE]
    d = 1 if latest_v - base_value >= 0 else -1
    vals = [rows[k][_ROW_VALUE] for k in picked]
    after_i = min(picked, key=lambda k: (d * (rows[k][_ROW_VALUE] - base_value), -ts[k]))
    after_value = rows[after_i][_ROW_VALUE]
    if any(d * (v - base_value) <= _EPS for v in vals):
        move = 0.0  # the confirming samples do not agree on a move away from the base: nothing confirmed
        after_value = base_value
    else:
        move = after_value - base_value
    stale = now - ts[last] > params.stale_s + _EPS
    gap = False
    for k in range(b + 1, end):
        if ts[k] - ts[k - 1] > params.gap_s + _EPS and series.interrupted(ts[k - 1], ts[k], params.gap_s):
            gap = True
            break
    # thin: any base sample behind the median, or either confirming sample (a thin print in the base band would
    # otherwise make the return to a normal book look like a move)
    thin = any(_row_thin(rows[k], params) for k in base_idx) or any(_row_thin(rows[k], params) for k in picked)
    sigma = series.sigma(w, now)
    threshold = threshold_for(w, sigma, params)
    ratio = round(abs(move) / threshold, 6) if threshold > 0 else 0.0
    t_half: Optional[float] = None
    if move != 0.0:
        pts = [(ts[k], rows[k][_ROW_VALUE]) for k in range(b, picked[0] + 1) if rows[k][_ROW_VALUE] is not None]
        t_half = half_cross_time(pts, base_value, d, 0.5 * abs(move), since=base_ts, confirm=1)
        if t_half is None:
            t_half = ts[after_i]
    flags: List[str] = []
    if placeholder:
        flags.append("placeholder")
    if sigma is None:
        flags.append("warm-up thresholds")
    return VenueMove(
        venue=series.venue, label=venue_label(series.venue), window_s=w, base_ts=base_ts, base_value=_r6(base_value),
        after_ts=ts[after_i], after_value=_r6(after_value), now_value=_r6(latest_v), move=_r6(move),
        threshold=_r6(threshold), sigma=_r6(sigma), vol_known=sigma is not None, ratio=ratio,
        spread_base=rows[b][_ROW_SPREAD], spread_now=rows[last][_ROW_SPREAD], thin=thin, stale=stale, gap=gap,
        flags=flags, t_half=_r6(t_half))


def venue_filter(vm: VenueMove) -> Optional[str]:
    """The first per-venue filter of §6.2 that drops this venue's move (placeholder, match, near, stale, thin,
    single_tick), or None when the move is usable. The watcher marks placeholder / match / near / single_tick in
    ``flags``; stale / gap / thin come from the fields."""
    flags = set(vm.flags or ())
    for reason in ("placeholder", "match", "near"):
        if reason in flags:
            return reason
    if vm.stale or vm.gap or "stale" in flags:
        return "stale"
    if vm.thin or "thin" in flags:
        return "thin"
    if "single_tick" in flags:
        return "single_tick"
    return None


def _triggers(vm: VenueMove) -> bool:
    return vm.move != 0.0 and vm.ratio >= 1.0 - 1e-6


def _sign(x: float) -> int:
    return 1 if x > 0 else -1 if x < 0 else 0


def _combine(exchange_id: str, window_s: float, moves: Mapping[str, Optional[VenueMove]], params: MoveParams
             ) -> Tuple[Optional[CombinedMove], Optional[str], List[VenueMove]]:
    """(move, None, confirming venues) | (None, reason, the venue moves behind the reason) | (None, None, [])."""
    present = [vm for vm in moves.values() if vm is not None]
    usable = [vm for vm in present if venue_filter(vm) is None]
    confirming: List[VenueMove] = []
    if len(usable) >= 2:
        dirs = {_sign(vm.move) for vm in usable}
        ratios = [vm.ratio for vm in usable]
        if (len(dirs) == 1 and 0 not in dirs and sum(ratios) / len(ratios) >= 1.0 - 1e-6
                and min(ratios) >= params.confirm_fraction - 1e-6):
            confirming = usable
        else:
            lead = [vm for vm in usable if _triggers(vm)]
            if lead:
                d = _sign(lead[0].move)
                # §6.2 "disagree": another usable venue moved the other way, not at all, or less than
                # confirm_fraction of its own threshold. Two venues that agree on the direction and each reach
                # confirm_fraction, but together stay under the bar (mean ratio < 1), are simply not a move yet:
                # no alert and nothing "filtered" (the label would wrongly say the other venue did not move).
                if any(_sign(vm.move) != d or vm.ratio < params.confirm_fraction - 1e-6 for vm in usable):
                    return None, "disagree", usable
                return None, None, []
    elif len(usable) == 1 and _triggers(usable[0]):
        confirming = usable
    if confirming:
        return _combined(exchange_id, window_s, confirming), None, confirming
    best: Optional[Tuple[int, str, VenueMove]] = None
    for pos, vm in enumerate(present):
        reason = venue_filter(vm)
        if reason is None or not (_triggers(vm) or reason == "single_tick"):
            continue
        rank = _FILTER_ORDER.index(reason)
        if best is None or rank < best[0]:
            best = (rank, reason, vm)
    if best is not None:
        return None, best[1], [best[2]]
    return None, None, []


def _combined(exchange_id: str, window_s: float, venues: Sequence[VenueMove]) -> CombinedMove:
    d = _sign(venues[0].move) or 1
    n = float(len(venues))
    before = sum(v.base_value for v in venues) / n
    after = sum(v.after_value for v in venues) / n
    now = sum(v.now_value for v in venues) / n
    top = max(venues, key=lambda v: v.threshold)
    halves = [v.t_half if v.t_half is not None else v.after_ts for v in venues]
    spreads = [v.spread_now for v in venues if v.spread_now is not None]
    agreement = (max(v.now_value for v in venues) - min(v.now_value for v in venues)) if len(venues) > 1 else 0.0
    uncertainty = max(agreement, (min(spreads) / 2.0) if spreads else 0.0,
                      max(MOVES_FEE_BAND.get(v.venue, 0.02) for v in venues))
    return CombinedMove(
        exchange_id=str(exchange_id), window_s=float(window_s), direction=d, venues=[v.venue for v in venues],
        confirmation="two venues" if len(venues) >= 2 else "one venue", venue_moves=list(venues),
        t_base=max(v.base_ts for v in venues), t_move=_r6(sum(halves) / n), before=_r6(before), after=_r6(after),
        now=_r6(now), move=_r6(d * (after - before)), threshold=top.threshold, sigma=top.sigma, vol_known=top.vol_known,
        uncertainty=_r6(uncertainty))


def combine_venues(exchange_id: str, window_s: float, moves: Mapping[str, Optional[VenueMove]],
                   params: MoveParams) -> Tuple[Optional[CombinedMove], Optional[str]]:
    """(the outcome-level move, None) when the venue rules of §6.3 trigger; (None, suppress reason) when some
    venue alone would have triggered but a filter dropped it; (None, None) when nothing moved."""
    move, reason, _who = _combine(exchange_id, window_s, moves, params)
    return move, reason


def cup_at(series: Sequence[CupQuote], t: float, max_age_s: float = MOVES_CUP_MAX_AGE_S) -> Optional[CupQuote]:
    """The latest Cup snapshot with ts <= t and ts >= t - max_age_s (snapshots oldest first), else None."""
    lo, hi = 0, len(series)
    while lo < hi:  # bisect on ts (the series is sorted by ts)
        mid = (lo + hi) // 2
        if series[mid].ts <= t + _EPS:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if i < 0:
        return None
    q = series[i]
    return q if q.ts >= t - max_age_s - _EPS else None


def classify(move: CombinedMove, cup_series: Sequence[CupQuote], now: float,
             params: MoveParams) -> Tuple[str, str, CupContext, Optional[str]]:
    """(status, reason sentence, Cup context, lag exclusion reason or None) at detection (§8.2)."""
    d = move.direction
    m = float(move.move)
    half = params.follow_fraction * m
    series = [q for q in cup_series if q.mid is not None and q.ts <= now + _EPS]
    ctx = CupContext()
    now_q = cup_at(series, now, params.cup_max_age_s)
    ctx.now = now_q
    if now_q is None:
        return ("no_cup", f"No Cup price in the last {params.cup_max_age_s / 60:.0f} minutes to compare with.", ctx, None)
    exclusion: Optional[str] = None
    base = cup_at(series, move.t_base, params.cup_max_age_s)
    if base is None:
        # no Cup snapshot at the start of the move: compare with the earliest one after it (not a lag observation)
        base = next((q for q in series if q.ts >= move.t_base - _EPS), now_q)
        exclusion = _NO_CUP_BASE
        ctx.short_history = True
    ctx.base = base
    ref = cup_at(series, move.t_base - params.cup_lead_lookback_s, params.cup_max_age_s)
    if ref is None:  # less Cup history than the look-back: the earliest snapshot at or before the base
        ref = series[0] if series and series[0].ts <= move.t_base + _EPS else base
        ctx.short_history = True
    ctx.ref = ref
    same = d * (now_q.mid - base.mid)
    lookback = d * (now_q.mid - ref.mid)
    ctx.move_same_window = _r6(same)
    ctx.move_lookback = _r6(lookback)
    window = window_text(move.window_s)
    if lookback >= half - _EPS:
        pts = [(q.ts, q.mid) for q in series if q.ts > ref.ts + _EPS]
        crossed = half_cross_time(pts, ref.mid, d, half, since=ref.ts, confirm=params.follow_confirm)
        ctx.half_cross_ts = crossed
        if crossed is not None and crossed <= move.t_move - params.cup_first_margin_s + _EPS:
            reason = (f"The Cup moved {_pts_num(now_q.mid - ref.mid)} pts about {fmt_duration(move.t_move - crossed)} "
                      "before the outside price: the outside followed the Cup, not a lag.")
            return "moved_first", reason, ctx, exclusion
    level = d * (move.now - now_q.mid)
    if level < params.converge_fraction * m - _EPS and same < half - _EPS:
        reason = f"The outside price moved to where the Cup already was ({fmt_price(now_q.mid)}): no edge."
        return "already_moved", reason, ctx, _CONVERGING
    if same >= half - _EPS:
        reason = (f"The Cup has already moved {_pts_num(now_q.mid - base.mid)} of the {_pts_num(m, False)} pts "
                  f"(now {fmt_price(now_q.mid)}): no edge left to chase.")
        return "already_moved", reason, ctx, exclusion
    reason = (f"The outside price moved {_pts_num(d * m)} pts in {window}; the Cup has moved "
              f"{_pts_num(now_q.mid - base.mid)} pts over the same time (now {fmt_price(now_q.mid)}).")
    return "lagging", reason, ctx, exclusion


def _ceil_tick(x: float) -> float:
    return round(math.ceil(x / _TICK - 1e-6) * _TICK, 3)


def hand_trade(direction: int, outside_values: Sequence[float], uncertainty: float, cup: CupQuote,
               book: Optional[BookObservation], book_source: Optional[str], now: float,
               params: MoveParams) -> Tuple[Optional[HandTrade], Optional[str]]:
    """(trade, None) or (None, why not) (§9). ``outside_values`` are the confirming venues' latest values."""
    from . import depth as _depth  # lazy: keeps this module's import light

    yes = direction >= 0
    side = "yes" if yes else "no"
    values = [float(v) for v in outside_values if v is not None]
    if not values:
        return None, "No fresh outside price right now: no trade suggested."
    v = min(values) if yes else 1.0 - max(values)
    fresh = book is not None and now - float(book.observed_at) <= params.book_max_age_s + _EPS
    bids, asks = _depth.book_sides(book) if fresh else ([], [])
    bid, ask = _num(cup.bid), _num(cup.ask)
    if fresh and float(book.observed_at) >= float(cup.ts) - _EPS and bids and asks:  # type: ignore[union-attr]
        bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)  # the book is newer than the snapshot
    if bid is None or ask is None or not 0.0 < bid < ask < 1.0:
        return None, "The Cup has no two-sided quote right now."
    h = (ask - bid) / 2.0
    u = float(uncertainty)
    limit = _ceil_tick(ask if yes else 1.0 - bid)
    max_limit = _depth.floor_tick(v - h - u - params.min_edge)
    if limit > max_limit + _EPS:
        what = "The Cup's ask" if yes else "The Cup's NO ask"
        return None, (f"{what} ({fmt_price(limit)}) leaves less than {params.min_edge * 100:.0f} cent per share after "
                      "the spread and the outside price's uncertainty: no trade suggested.")
    shares_at: Optional[float] = None
    shares_max: Optional[float] = None
    age: Optional[float] = None
    source: Optional[str] = None
    if fresh and (bids or asks):
        levels = _depth.contract_levels(bids, asks, side, "buy")
        shares_at = float(_depth.available(levels, limit, True))
        shares_max = float(_depth.available(levels, max_limit, True))
        # the book may be read just after the step's evaluation time: an age is never negative
        age = _r6(max(0.0, now - float(book.observed_at)))  # type: ignore[union-attr]
        source = book_source
    edge = v - h - limit
    return HandTrade(
        side=side, action="buy", text=f"Buy {side.upper()} at {fmt_price(limit)}", limit=limit, max_limit=max_limit,
        outside_value=_r6(v), uncertainty=_r6(u), cup_half_spread=_r6(h), edge_per_share=_r6(edge),
        edge_after_uncertainty=_r6(edge - u), edge_at_resolution=_r6(v - limit), shares_at_limit=shares_at,
        shares_to_max=shares_max, book_source=source, book_age_s=age, computed_at=float(now)), None


def _gap_now(alert: MoveAlert, cup_now: Optional[CupQuote]) -> Optional[float]:
    base = alert.cup.base
    if cup_now is None or base is None or cup_now.mid is None or base.mid is None:
        return None
    return _r6(alert.peak_move - alert.direction * (cup_now.mid - base.mid))


def update_lag(alert: MoveAlert, cup_series: Sequence[CupQuote], outside_points: Sequence[Tuple[float, float]],
               now: float, params: MoveParams) -> bool:
    """Advance ``alert.lag`` (and the captures) to ``now`` (§10.1); True when the outcome changed."""
    lag = alert.lag
    d = alert.direction
    before_outcome = lag.outcome
    series = [q for q in cup_series if q.mid is not None and q.ts <= now + _EPS]
    points = [(float(t), float(v)) for t, v in outside_points if v is not None and t <= now + _EPS]
    base = alert.cup.base
    if lag.eligible and lag.outcome == "pending" and base is not None and base.mid is not None:
        peak = float(alert.peak_move)
        need = params.follow_fraction * peak
        followed_at: Optional[float] = None
        after = [q for q in series if q.ts > alert.t_base + _EPS]
        run = max(1, params.follow_confirm)
        for k in range(len(after) - run + 1):
            if all(d * (after[k + j].mid - base.mid) >= need - _EPS for j in range(run)):
                followed_at = after[k].ts
                break
        reverted_at: Optional[float] = None
        level = (1.0 - params.revert_fraction) * peak
        later = [p for p in points if p[0] > alert.detected_at + _EPS]
        for k in range(len(later) - 1):
            if all(d * (later[k + j][1] - alert.outside_before) <= level + _EPS for j in range(2)):
                reverted_at = later[k][0]
                break
        if followed_at is not None and (reverted_at is None or followed_at <= reverted_at):
            snap = next(q for q in after if abs(q.ts - followed_at) <= _EPS)
            lag.outcome = "followed"
            lag.followed_at = followed_at
            lag.lag_s = _r6(followed_at - alert.t_move)
            lag.lag_after_alert_s = _r6(followed_at - alert.detected_at)
            lag.resolved_at = float(now)
            rel = lag.lag_after_alert_s
            when = (f"{fmt_duration(rel)} after the alert" if rel >= 0 else f"{fmt_duration(-rel)} before the alert")
            lag.reason = (f"The Cup moved {_pts_num(snap.mid - base.mid)} pts, {fmt_duration(lag.lag_s)} after the "
                          f"outside move ({when}).")
            if alert.status == "lagging":
                alert.status = "already_moved"
                alert.status_label = MOVE_STATUS_LABELS["already_moved"]
                alert.reason = lag.reason
        elif reverted_at is not None:
            latest = later[-1][1] if later else alert.outside_now
            back = peak - d * (latest - alert.outside_before)
            lag.outcome = "reverted"
            lag.resolved_at = float(now)
            lag.reason = (f"The outside price came back {_pts_num(back, False)} of its {_pts_num(peak, False)} pts "
                          "before the Cup moved.")
            alert.status = "reverted"
            alert.status_label = MOVE_STATUS_LABELS["reverted"]
            alert.reason = lag.reason
        elif now - alert.t_move >= params.lag_track_s - _EPS:
            gap = _gap_now(alert, cup_at(series, now, params.cup_max_age_s))
            if gap is None:
                gap = alert.lag_gap_now if alert.lag_gap_now is not None else alert.lag_gap
            lag.outcome = "not_followed"
            lag.resolved_at = float(now)
            lag.reason = (f"The Cup did not follow within {params.lag_track_s / 60:.0f} min; the gap is "
                          f"{_pts_num(gap, False)} pts now.")
            alert.reason = lag.reason
        else:
            newest = series[-1].ts if series else None
            if newest is None or now - newest > params.resume_gap_s + _EPS:
                lag.outcome = "censored"
                lag.resolved_at = float(now)
                lag.reason = f"No Cup price for {params.resume_gap_s / 60:.0f} min."
    # captures: alerts that were lagging when detected (§10.1), late values allowed after the close.
    # (integration) The Cup is only sampled every tracker interval (30 s live), so the latest snapshot at or before
    # t_h can be up to one interval old while the Cup is catching up: alone it would overstate the gap left (and
    # understate the entry price). Each capture therefore waits for the first snapshot after the time too (or
    # MOVES_CUP_MAX_AGE_S) and takes the LESS favourable of the two bracketing snapshots. Conservative, never better.
    if (alert.opened_status or alert.status) == "lagging":
        t_h = alert.detected_at + params.human_delay_s
        if lag.capture_at is None and now >= t_h - _EPS:
            around_h, ready = _cup_bracket(series, t_h, params.cup_max_age_s, now)
            if ready:
                lag.capture_at = t_h
                out = [v for t, v in points if t <= t_h + _EPS]
                gaps = [d * (out[-1] - q.mid) - float(q.spread) for q in around_h  # type: ignore[arg-type]
                        if out and q.mid is not None and _num(q.spread) is not None]
                if gaps and _num(around_h[0].spread) is not None:
                    lag.capture_4m = _r6(min(gaps))
        t_e = t_h + params.exit_after_s
        if lag.exit_at is None and now >= t_e - _EPS:
            around_e, ready_e = _cup_bracket(series, t_e, params.cup_max_age_s, now)
            around_h, _ready_h = _cup_bracket(series, t_h, params.cup_max_age_s, now)
            if ready_e:
                lag.exit_at = t_e
                if around_h and around_e:
                    if d >= 0:  # buy YES at the ask at t_h, sell YES at the bid at t_e (the worse of each bracket)
                        asks = [float(q.ask) for q in around_h if _num(q.ask) is not None]  # type: ignore[arg-type]
                        bids = [float(q.bid) for q in around_e if _num(q.bid) is not None]  # type: ignore[arg-type]
                        if asks and bids and _num(around_h[0].ask) is not None and _num(around_e[0].bid) is not None:
                            lag.exit_30m = _r6(min(bids) - max(asks))
                    else:  # buy NO at 1 - YES bid at t_h, sell NO at 1 - YES ask at t_e
                        bids = [float(q.bid) for q in around_h if _num(q.bid) is not None]  # type: ignore[arg-type]
                        asks = [float(q.ask) for q in around_e if _num(q.ask) is not None]  # type: ignore[arg-type]
                        if asks and bids and _num(around_h[0].bid) is not None and _num(around_e[0].ask) is not None:
                            lag.exit_30m = _r6(min(bids) - max(asks))
    return lag.outcome != before_outcome


def _cup_bracket(series: Sequence[CupQuote], t: float, max_age_s: float, now: float
                 ) -> Tuple[List[CupQuote], bool]:
    """([the latest Cup snapshot at or before ``t``, then the earliest one after it], ready). Both within
    ``max_age_s`` of ``t``; the list is empty without a snapshot at or before ``t`` (the measurement needs one, as
    §10.1). ``ready`` once a snapshot after ``t`` was seen or ``max_age_s`` passed (``series`` holds only snapshots
    up to ``now``)."""
    before = cup_at(series, t, max_age_s)
    after = next((q for q in series if t + _EPS < q.ts <= t + max_age_s + _EPS), None)
    ready = after is not None or now >= t + max_age_s - _EPS
    if before is None:
        return [], ready
    return ([before, after] if after is not None else [before]), ready


def _stats(values: Sequence[float], races: Sequence[str]) -> Dict[str, Any]:
    """{"n", "mean", "median", "positive_share", "ci90"} with the 90% t-interval over per-race means."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "positive_share": None, "ci90": None}
    mean = math.fsum(values) / n
    out: Dict[str, Any] = {"n": n, "mean": _r6(mean), "median": _r6(_median(values)),
                           "positive_share": _r6(sum(1 for v in values if v > 0) / n), "ci90": None}
    by_race: Dict[str, List[float]] = {}
    for v, r in zip(values, races):
        by_race.setdefault(r, []).append(v)
    g = len(by_race)
    if g >= MOVES_CI_MIN_RACES:
        from .paper import t_quantile  # lazy: stdlib-only inverse t (the paper verdict uses the same one)

        means = [math.fsum(vs) / len(vs) for _, vs in sorted(by_race.items())]
        centre = math.fsum(means) / g
        var = math.fsum((m - centre) ** 2 for m in means) / (g - 1)
        half = t_quantile(0.95, g - 1) * math.sqrt(var / g)
        out["ci90"] = [_r6(centre - half), _r6(centre + half)]
    return out


def summarise(alerts: Iterable[Mapping[str, Any]], now: float, params: Optional[MoveParams] = None) -> LagSummary:
    """The lag study over alert dicts (MoveAlert.to_dict()) (§10.2), including the exact sentence."""
    p = params or MoveParams()
    s = LagSummary()
    eligible: List[Mapping[str, Any]] = []
    for a in alerts:
        lag = a.get("lag") if isinstance(a.get("lag"), Mapping) else {}
        if not lag.get("eligible", True) or lag.get("outcome") == "excluded":
            s.excluded += 1
            continue
        eligible.append(a)
    s.moves = len(eligible)

    def race_of(a: Mapping[str, Any]) -> str:
        return str(a.get("race_key") or f"outcome:{a.get('exchange_id')}")

    if eligible:
        s.since = min(float(a.get("detected_at") or 0.0) for a in eligible)
    outcomes = [str((a.get("lag") or {}).get("outcome") or "pending") for a in eligible]
    s.pending = outcomes.count("pending")
    s.censored = outcomes.count("censored")
    s.resolved = sum(1 for o in outcomes if o not in ("pending", "censored"))
    s.cup_first = outcomes.count("cup_first")
    s.outside_led = s.resolved - s.cup_first
    # races are counted over the same alerts as the numbers they qualify: a pending alert on a new race must not
    # make a small resolved sample look like it covers more races (or lift it over the MOVES_MIN_RACES bar)
    s.races = len({race_of(a) for a, o in zip(eligible, outcomes) if o not in ("pending", "censored")})
    s.outside_led_races = len({race_of(a) for a, o in zip(eligible, outcomes)
                               if o not in ("pending", "censored", "cup_first")})
    s.cup_first_share = _r6(s.cup_first / s.resolved) if s.resolved else None
    followed = [a for a, o in zip(eligible, outcomes) if o == "followed"]
    s.followed = len(followed)
    lags = [float(a["lag"]["lag_s"]) for a in followed if _num(a["lag"].get("lag_s")) is not None]
    after = [float(a["lag"]["lag_after_alert_s"]) for a in followed
             if _num(a["lag"].get("lag_after_alert_s")) is not None]
    for k in MOVES_FOLLOW_BUCKETS_S:
        key = f"{k:.0f}"
        count = sum(1 for x in lags if x <= k + _EPS)
        s.followed_within_n[key] = count
        s.followed_within[key] = _r6(count / s.outside_led) if s.outside_led else None
    s.reverted = outcomes.count("reverted")
    s.not_followed = outcomes.count("not_followed")
    s.never_followed = s.reverted + s.not_followed
    if s.outside_led:
        s.never_followed_share = _r6(s.never_followed / s.outside_led)
        s.reverted_share = _r6(s.reverted / s.outside_led)
        s.not_followed_share = _r6(s.not_followed / s.outside_led)
    s.median_lag_s = _r6(_median(lags))
    s.median_lag_after_alert_s = _r6(_median(after))
    if lags:
        s.lag_quartiles_s = [_r6(_quantile(lags, 0.25)), _r6(_quantile(lags, 0.75))]  # type: ignore[list-item]
    for name in ("capture_4m", "exit_30m"):
        vals: List[float] = []
        races: List[str] = []
        for a in eligible:
            v = _num((a.get("lag") or {}).get(name))
            if v is not None:
                vals.append(v)
                races.append(race_of(a))
        setattr(s, name, _stats(vals, races))
    s.small_sample = s.outside_led < p.min_sample or s.outside_led_races < p.min_races
    if s.moves == 0:
        s.sentence = MOVES_NO_DATA_SENTENCE
    elif s.resolved == 0:
        parts = [f"{s.pending} still being measured"] if s.pending else []
        if s.censored:
            parts.append(f"{s.censored} cut short, not counted")
        s.sentence = MOVES_PENDING_SENTENCE.format(detail=", ".join(parts), min_n=p.min_sample, min_races=p.min_races)
    elif s.small_sample:
        n = s.outside_led + s.cup_first
        s.sentence = MOVES_SMALL_SAMPLE_SENTENCE.format(
            n=n, moves_word="move" if n == 1 else "moves", races=s.races,
            races_word="race" if s.races == 1 else "races", followed=s.followed, never=s.never_followed,
            cup_first=s.cup_first, min_n=p.min_sample, min_races=p.min_races)
    else:
        s.sentence = MOVES_SUMMARY_SENTENCE.format(
            n=s.outside_led, races=s.outside_led_races, p5=fmt_share(s.followed_within.get("300")),
            p60=fmt_share(s.followed_within.get("3600")), median=fmt_duration(s.median_lag_s),
            median_alert=fmt_duration(s.median_lag_after_alert_s), capture=fmt_money(s.capture_4m.get("mean")),
            n_capture=s.capture_4m.get("n", 0), exit=fmt_money(s.exit_30m.get("mean")), n_exit=s.exit_30m.get("n", 0))
    return s


def _cup_mid_of(alert: Mapping[str, Any]) -> Optional[float]:
    for key in ("cup_now",):
        q = alert.get(key)
        if isinstance(q, Mapping) and _num(q.get("mid")) is not None:
            return float(q["mid"])
    cup = alert.get("cup") if isinstance(alert.get("cup"), Mapping) else {}
    q = cup.get("now") if isinstance(cup.get("now"), Mapping) else None
    return float(q["mid"]) if q is not None and _num(q.get("mid")) is not None else None


def notification_text(alert: Mapping[str, Any]) -> Tuple[str, str]:
    """(headline, body) for a desktop notification and the status' ``actionable_alerts`` (§19.4)."""
    label = str(alert.get("status_label") or MOVE_STATUS_LABELS.get(str(alert.get("status")), "Outside move"))
    headline = f"{label}: {alert.get('race_label') or alert.get('title') or alert.get('exchange_id')}"
    d = int(alert.get("direction") or 1)
    move = _num(alert.get("move")) or 0.0
    body = (f"Outside {_pts_num(d * move)} pts in {window_text(float(alert.get('window_s') or 60.0))} "
            f"({fmt_price(_num(alert.get('outside_before')))} → {fmt_price(_num(alert.get('outside_after')))}); "
            f"Cup {fmt_price(_cup_mid_of(alert))}.")
    trade = alert.get("trade") if isinstance(alert.get("trade"), Mapping) else None
    if trade and trade.get("text"):
        body += f" Suggestion: {trade['text']}. Not a sure thing."
    else:
        body += " No trade suggested. Not a sure thing."
    return headline, body


def _title_of(alert: Mapping[str, Any]) -> str:
    title = str(alert.get("title") or alert.get("exchange_id") or "")
    option = alert.get("option")
    if option is not None and str(option).strip().upper() not in ("", "YES"):
        title = f"{title} — {option}"
    return title


_OPEN_TAGS = {"lagging": "CUP LAGGING", "already_moved": "CUP ALREADY MOVED", "moved_first": "CUP MOVED FIRST",
              "reverted": "REVERTED"}
_LAG_TAGS = {"followed": "FOLLOWED", "reverted": "REVERTED", "not_followed": "NOT FOLLOWED", "censored": "CENSORED"}


def _trade_line(alert: Mapping[str, Any], *, at_open: bool = False) -> str:
    trade = alert.get("trade") if isinstance(alert.get("trade"), Mapping) else None
    note = alert.get("trade_note")
    if at_open and (isinstance(alert.get("opened_trade"), Mapping) or alert.get("opened_trade_note")):
        trade = alert["opened_trade"] if isinstance(alert.get("opened_trade"), Mapping) else None  # as it opened
        note = alert.get("opened_trade_note")
    if not trade:
        return f"    no trade suggested: {note or 'no edge after the spread and the uncertainty.'}"
    at = _num(trade.get("shares_at_limit"))
    to_max = _num(trade.get("shares_to_max"))
    if at is None:
        depth = "shares at that price unknown (no recent Cup order book)"
    else:
        depth = (f"{at:.0f} shares at that price ({(to_max if to_max is not None else at):.0f} up to "
                 f"{fmt_price(_num(trade.get('max_limit')))})")
    return (f"    suggestion (not a sure thing): {trade.get('text')}, {depth}; "
            f"{fmt_money(_num(trade.get('edge_per_share')))}/share net of the spread, "
            f"{fmt_money(_num(trade.get('edge_after_uncertainty')))} after the outside price's uncertainty")


def format_event_lines(event: MoveEvent, *, bell: bool = False) -> List[str]:
    """The ``moves`` command's text lines for one event (§17.2), exact formats."""
    alert = event.alert if isinstance(event.alert, Mapping) else {}
    if event.kind == "closed":
        return []  # in alerts.jsonl and --json only
    lag = alert.get("lag") if isinstance(alert.get("lag"), Mapping) else {}
    if event.kind == "opened":
        status = str(alert.get("opened_status") or alert.get("status") or event.status)
        tag = _OPEN_TAGS.get(status, status.upper())
    else:
        outcome = str(lag.get("outcome") or event.lag_outcome)
        tag = _LAG_TAGS.get(outcome) or _OPEN_TAGS.get(str(alert.get("status") or event.status), "STATUS")
        status = str(alert.get("status") or event.status)
    head = f"[moves {_hhmmss(event.at)}] {tag}  {alert.get('race_label') or ''}  {_title_of(alert)}"
    if bell and event.kind == "opened" and alert.get("actionable"):
        head += "\a"
    lines = [head]
    if event.kind == "opened" and status == "lagging":
        d = int(alert.get("direction") or 1)
        venues = ", ".join(venue_label(v) for v in alert.get("venues") or [])
        cup = alert.get("cup") if isinstance(alert.get("cup"), Mapping) else {}
        now_q = cup.get("now") if isinstance(cup.get("now"), Mapping) else {}
        base_q = cup.get("base") if isinstance(cup.get("base"), Mapping) else {}
        cup_move = (float(now_q["mid"]) - float(base_q["mid"])) if (_num(now_q.get("mid")) is not None
                                                                  and _num(base_q.get("mid")) is not None) else 0.0
        lines.append(
            f"    outside {fmt_price(_num(alert.get('outside_before')))} -> {fmt_price(_num(alert.get('outside_after')))} "
            f"({_pts_num(d * float(alert.get('move') or 0.0))} pts in {window_text(float(alert.get('window_s') or 60.0))}; "
            f"{venues}); Cup {fmt_price(_num(now_q.get('mid')))} (bid {fmt_price(_num(now_q.get('bid')))} / ask "
            f"{fmt_price(_num(now_q.get('ask')))}), {_pts_num(cup_move)} pts; lag gap "
            f"{_pts_num(float(alert.get('lag_gap') or 0.0), False)} pts")
        lines.append(_trade_line(alert, at_open=True))
        return lines
    if event.kind == "opened":
        sentence = str(alert.get("reason") or "")
    else:
        outcome = str(lag.get("outcome") or "")
        sentence = str(lag.get("reason") or "") if outcome in _LAG_TAGS and lag.get("reason") else str(alert.get("reason") or "")
    if sentence:
        lines.append("    " + _detail(sentence))
    return lines


def _venue_text(v: Mapping[str, Any]) -> str:
    label = str(v.get("label") or venue_label(str(v.get("venue") or "")))
    status = str(v.get("status") or "pending")
    if status in ("ok", "partial"):
        parts = [f"{int(v.get('matched') or 0)} matched"]
        limit = v.get("budget_limit")
        if limit:
            parts.append(f"{int(v.get('budget_used') or 0)}/{int(limit)} reads a minute")
        text = f"{label} {status} ({', '.join(parts)})"
        if status == "partial" and v.get("last_error"):
            text += f": {v.get('last_error')}"
        return text
    if v.get("last_error"):
        return f"{label} {status} ({v.get('last_error')})"
    return f"{label} {status}"


def summary_lines(summary: Mapping[str, Any], venues: Sequence[Mapping[str, Any]], now: float, *,
                  final: bool = False, demo: Optional[bool] = None) -> List[str]:
    """The periodic and final summary block of the ``moves`` command (§17.2). ``summary`` is either the watcher's
    published ``summary()`` body or a ``LagSummary.to_dict()``. ``final=True`` (added by core, optional) appends
    the caveats, one per line, and MOVES_DEMO_CAVEAT for a demo (``demo`` defaults to the body's caveats)."""
    lag = summary.get("summary") if isinstance(summary.get("summary"), Mapping) else summary
    sentence = str((lag or {}).get("sentence") or MOVES_NO_DATA_SENTENCE)
    lines = [f"[moves {_hhmmss(now)}] summary: {sentence}"]
    lines.append("    venues: " + ("; ".join(_venue_text(v) for v in venues) if venues else "none configured"))
    counts = summary.get("counts") if isinstance(summary.get("counts"), Mapping) else {}
    supp = counts.get("suppressed_today") if isinstance(counts.get("suppressed_today"), Mapping) else \
        (summary.get("suppressed_today") if isinstance(summary.get("suppressed_today"), Mapping) else {})
    parts = [f"{int(supp.get(r) or 0)} {SUPPRESS_LABELS[r]}" for r in SUPPRESS_REASONS if int(supp.get(r) or 0) > 0]
    lines.append("    filtered out today: " + (", ".join(parts) if parts else "nothing"))
    if final:
        caveats = list(summary.get("caveats") or []) or list(MOVES_CAVEATS)
        if demo is True and MOVES_DEMO_CAVEAT not in caveats:
            caveats.append(MOVES_DEMO_CAVEAT)
        if demo is False:
            caveats = [c for c in caveats if c != MOVES_DEMO_CAVEAT]
        lines.extend(f"  - {c}" for c in caveats)
    return lines


def lag_table_lines(lag: Mapping[str, Any]) -> List[str]:
    """(integration) The lag study's numbers with their sample sizes, for the ``moves`` command's final block and
    ``--replay`` (the dashboard shows the same as a table): what the sentence alone hides on a small sample."""
    if not isinstance(lag, Mapping) or not int(lag.get("moves") or 0):
        return []  # nothing measured: the sentence (MOVES_NO_DATA_SENTENCE) says it all
    n = int(lag.get("outside_led") or 0)
    races = int(lag.get("outside_led_races") or 0)
    head = (f"    lag study: {n} resolved outside-led move{'' if n == 1 else 's'} on {races} race"
            f"{'' if races == 1 else 's'}; {int(lag.get('cup_first') or 0)} where the Cup moved first; not counted: "
            f"{int(lag.get('excluded') or 0)} excluded (converging or no Cup base), {int(lag.get('censored') or 0)} "
            f"censored, {int(lag.get('pending') or 0)} still being measured")
    lines = [head]
    if n <= 0:
        return lines

    def frac(k: int) -> str:
        return f"{k}/{n} ({k / n * 100:.0f}%)"

    within = lag.get("followed_within_n") if isinstance(lag.get("followed_within_n"), Mapping) else {}
    lines.append("      followed within " + ", ".join(
        f"{window_text(k)} {frac(int(within.get(f'{k:.0f}') or 0))}" for k in MOVES_FOLLOW_BUCKETS_S))
    lines.append(f"      never followed {frac(int(lag.get('never_followed') or 0))}: "
                 f"{int(lag.get('reverted') or 0)} came back, {int(lag.get('not_followed') or 0)} did not follow within "
                 "60 min")
    if _num(lag.get("median_lag_s")) is not None:
        lines.append(f"      median lag {fmt_duration(_num(lag.get('median_lag_s')))} after the outside move "
                     f"({fmt_duration(_num(lag.get('median_lag_after_alert_s')))} after the alert; "
                     f"n={int(lag.get('followed') or 0)} followed)")
    for key, what in (("capture_4m", "gap left 4 min after the alert, net of the spread"),
                      ("exit_30m", "bought 4 min after the alert, sold 30 min later")):
        st = lag.get(key) if isinstance(lag.get(key), Mapping) else {}
        k = int(st.get("n") or 0)
        if k <= 0:
            lines.append(f"      {what}: not measured yet (n=0)")
            continue
        ci = st.get("ci90")
        interval = (f"90% interval {fmt_money(_num(ci[0]))} to {fmt_money(_num(ci[1]))} over per-race means"
                    if isinstance(ci, (list, tuple)) and len(ci) == 2 else
                    f"no interval yet: fewer than {MOVES_CI_MIN_RACES} races")
        share = _num(st.get("positive_share"))
        lines.append(f"      {what}: mean {fmt_money(_num(st.get('mean')))}, median {fmt_money(_num(st.get('median')))} a "
                     f"share, positive {fmt_share(share)} (n={k}; {interval}; no price impact or queue)")
    return lines


# --------------------------------------------------------------------------- the watcher (§12)

MOVES_CUP_KEEP_S = 7200.0  # Cup snapshots kept per outcome (and read once at the first step): 2 h
_ERROR_LOG_EVERY_S = 3600.0  # an I/O error (store, alerts.jsonl) is logged at most this often per kind


@dataclass
class _Work:
    """What one step's locked computation hands to the unlocked parts (books, persistence, events)."""

    samples: int = 0
    sample_rows: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Tuple[str, str]] = field(default_factory=list)  # (kind, alert_id), in order
    opened: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    closed: List[str] = field(default_factory=list)
    suppressed: int = 0
    needs_book: List[Tuple[str, str]] = field(default_factory=list)  # (alert_id, exchange_id)


class OutsideMoveWatcher:
    """Polls the outside venues, detects and classifies moves, measures the lag, persists and publishes.

    ``fair_values``: the tracker's FairValueService (targets, map overrides; mode must be "auto").
    ``providers``: the objects whose ``poll(targets, now, deadline=)`` is called each step; default
    ``fair_values.providers`` (live: the same PolymarketProvider / KalshiProvider instances, so each host's
    limiter and backoff are shared with the fair-value refresh). The demo passes its two scripted venues.
    ``cup``: a :class:`CupFeed` (or bind it later with :meth:`bind`; the tracker does). ``persistence``: a
    :class:`MovesPersistence` (the store) or None. ``alerts_path``: the append-only alerts.jsonl or None.
    ``clock``: every time; the fast demo passes its SimClock. ``demo`` marks alerts and adds MOVES_DEMO_CAVEAT.
    """

    def __init__(self, fair_values: Any, *, providers: Optional[Sequence[Any]] = None, cup: Optional[CupFeed] = None,
                 persistence: Optional[MovesPersistence] = None, alerts_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time, params: Optional[MoveParams] = None,
                 demo: bool = False) -> None:
        self.fair_values = fair_values
        self.params = params or MoveParams()
        self.demo = bool(demo)
        self.alerts_path = Path(alerts_path) if alerts_path is not None else None
        self._providers_arg = list(providers) if providers is not None else None
        self._cup = cup
        self._persistence = persistence
        self._clock = clock
        self._lock = threading.Lock()  # the leaf lock (§12.3): pure computation only while held
        self._step_lock = threading.Lock()  # one step at a time; readers never take it
        self._listeners: List[Listener] = []
        # state (guarded by _lock)
        self._series: Dict[Tuple[str, str], SeriesBuffer] = {}
        self._cup_series: Dict[str, List[CupQuote]] = {}
        self._alerts: Dict[str, MoveAlert] = {}
        self._dicts: Dict[str, Dict[str, Any]] = {}  # alert_id -> its published to_dict() (replaced on change)
        self._open: Dict[str, str] = {}  # exchange_id -> the open alert's id
        self._last_alert: Dict[str, str] = {}  # exchange_id -> the newest alert's id (re-arm)
        self._grown_peak: Dict[str, float] = {}  # alert_id -> the peak when grown_at was last set
        self._live: Set[str] = set()  # alerts with work left (open, or late captures)
        self._dirty: Set[str] = set()
        self._suppressed: Deque[SuppressedMove] = deque(maxlen=MOVES_MAX_SUPPRESSED_SHOWN)
        self._suppress_seen: Dict[Tuple[str, str], float] = {}
        self._suppress_log: Deque[Tuple[float, str]] = deque()
        self._spikes: Dict[Tuple[str, str, float], Tuple[float, float, float, float]] = {}
        self._spike_moves: Dict[Tuple[str, str, float], float] = {}
        self._venues: Dict[str, VenueStatus] = {}
        self._venue_order: List[str] = []
        self._venue_matched: Dict[str, Set[str]] = {}
        self._last_stored: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        self._books: Dict[str, Tuple[BookObservation, str]] = {}
        self._book_tried: Dict[str, float] = {}
        self._active_ids: List[str] = []
        self._warmed = False
        self._steps = 0
        self._last_step_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._io_errors: Dict[str, str] = {}
        self._error_logged_at: Dict[str, float] = {}
        self._closed = False
        for provider in self._pollable():
            name = self._pname(provider)
            if name not in self._venues:
                self._venue_order.append(name)
                self._venues[name] = VenueStatus(venue=name, label=venue_label(name), status="pending")
        self._published_open: Dict[str, Dict[str, Any]] = {}
        self._published: Dict[str, Any] = self._empty_summary()
        self._published_status: Dict[str, Any] = {"enabled": True, "poll_s": self.params.poll_s, "last_step_at": None,
                                                  "steps": 0, "open": 0, "actionable": 0, "actionable_alerts": [],
                                                  "venues": {"ok": 0, "total": 0}, "last_error": None}

    # ------------------------------------------------------------------ small accessors

    @property
    def poll_s(self) -> float:
        return float(self.params.poll_s)

    def bind(self, cup: CupFeed) -> None:
        """Attach the Cup feed (the tracker calls this from its constructor)."""
        self._cup = cup

    def add_listener(self, fn: Listener) -> None:
        """``fn(event)`` is called after each step, outside every lock, for each opened / status / closed event
        (exceptions are logged and ignored)."""
        self._listeners.append(fn)

    @staticmethod
    def _pname(provider: Any) -> str:
        return str(getattr(provider, "name", type(provider).__name__))

    def _pollable(self) -> List[Any]:
        if self._providers_arg is not None:
            providers = self._providers_arg
        else:
            try:
                providers = list(getattr(self.fair_values, "providers", None) or [])
            except Exception:  # a broken service never breaks the watcher
                providers = []
        return [p for p in providers if callable(getattr(p, "poll", None))]

    def _thresholds(self) -> Dict[str, Any]:
        p = self.params
        return {"windows_s": [float(w) for w in p.windows_s],
                "abs_min": {f"{float(k):.0f}": float(v) for k, v in sorted(p.abs_min.items())},
                "k_sigma": float(p.k_sigma), "warmup_factor": float(p.warmup_factor), "max_spread": float(p.max_spread),
                "min_liquidity_usd": float(p.min_liquidity_usd), "min_top_size": float(p.min_top_size),
                "follow_fraction": float(p.follow_fraction), "lag_track_s": float(p.lag_track_s),
                "human_delay_s": float(p.human_delay_s), "exit_after_s": float(p.exit_after_s),
                "min_edge": float(p.min_edge)}

    def _caveats(self) -> List[str]:
        return list(MOVES_CAVEATS) + ([MOVES_DEMO_CAVEAT] if self.demo else [])

    def _empty_summary(self) -> Dict[str, Any]:
        return {"poll_s": float(self.params.poll_s), "last_step_at": None, "steps": 0,
                "watching": {"outcomes": 0, "matched": 0, "venues": 0}, "venues": [],
                "counts": {"open": 0, "lagging": 0, "actionable": 0, "today": 0, "closed_today": 0,
                           "suppressed_today": {r: 0 for r in SUPPRESS_REASONS}},
                "actionable_ids": [], "alerts": [], "suppressed": [], "summary": LagSummary().to_dict(),
                "thresholds": self._thresholds(), "caveats": self._caveats()}

    # ------------------------------------------------------------------ the step (§12.2)

    def step(self, now: Optional[float] = None) -> MovesStepReport:
        """One watch step (§12.2). Never raises except ReadOnlyViolation and BaseException (shutdown)."""
        with self._step_lock:
            if self._closed:  # shutting down: no poll, no write (the providers and the store may be closed already)
                at = float(self._clock()) if now is None else max(float(now), float(self._clock()))
                return MovesStepReport(at=at, duration_s=0.0, errors=["The outside-move watcher is closed."])
            from .readonly import ReadOnlyViolation  # lazy: the guard type only

            try:
                return self._step(now)
            except ReadOnlyViolation:
                raise
            except Exception as exc:  # a bug in one step never stops the watcher thread; it is shown, not hidden
                log.warning("outside-move step failed: %s", exc, exc_info=True)
                message = (f"The outside-move watcher failed this step ({type(exc).__name__}): alerts may be missing "
                           "until it recovers.")
                at = float(self._clock()) if now is None else max(float(now), float(self._clock()))
                with self._lock:
                    self._last_error = message
                    status = dict(self._published_status)
                    status["last_error"] = message
                    self._published_status = status
                return MovesStepReport(at=at, duration_s=0.0, errors=[message])

    def _step(self, now: Optional[float]) -> MovesStepReport:
        from .readonly import ReadOnlyViolation  # lazy: the guard type only

        t_start = float(self._clock())
        start = max(float(now), t_start) if now is not None else t_start
        errors: List[str] = []
        warm_events: List[Tuple[str, str]] = []
        if not self._warmed:
            try:
                warm_events = self._warm_up(start, errors)
            except ReadOnlyViolation:
                raise
            except Exception as exc:  # pragma: no cover - defensive: a bad stored row never stops tracking
                log.warning("outside-move warm-up failed: %s", exc, exc_info=True)
                errors.append(f"Stored outside-move data could not be loaded ({type(exc).__name__}): starting fresh.")
                self._warmed = True
        # 2. targets (no lock held)
        try:
            targets = list(self.fair_values.targets() or [])
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            log.warning("outside-move targets failed: %s", exc, exc_info=True)
            targets = []
            errors.append(f"The outside-move watcher could not read the outcomes to watch ({type(exc).__name__}).")
        active = [t for t in targets if not getattr(t, "disabled", False)]
        # 3. polls (no lock held; providers serialise themselves)
        polled: List[Tuple[str, Any, Optional[Mapping[str, Any]], float]] = []
        for provider in self._pollable():
            name = self._pname(provider)
            p_now = float(self._clock())
            try:
                res = provider.poll(active, p_now, deadline=p_now + float(self.params.poll_deadline_s))
                if res is None or not hasattr(res, "status"):
                    raise TypeError("poll returned no result")
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                log.warning("outside-move poll of %s failed: %s", name, exc, exc_info=True)
                res = _failed_result(name, f"{venue_label(name)} could not be polled ({type(exc).__name__}): no outside "
                                           "quotes from it this time.")
            done = float(self._clock())
            budget: Optional[Mapping[str, Any]] = None
            if callable(getattr(provider, "budget", None)):
                try:
                    budget = provider.budget()
                except Exception:  # the budget line is informational
                    budget = None
            polled.append((name, res, budget, done))
        # 4. the Cup (lock-free published snapshot)
        cup_quotes: Dict[str, Any] = {}
        if self._cup is not None:
            try:
                cup_quotes = dict(self._cup.quotes() or {})
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                log.warning("outside-move Cup quotes failed: %s", exc, exc_info=True)
                errors.append(f"The Cup prices could not be read for the outside-move watcher ({type(exc).__name__}).")
        # 5. the evaluation time: after every poll returned (lookahead-2)
        now_eval = float(self._clock())
        if now is not None:
            now_eval = max(float(now), now_eval)
        # 6. pure computation under the leaf lock
        with self._lock:
            work = self._compute(now_eval, targets, active, polled, cup_quotes)
            work.events[:0] = warm_events
        # 7. book reads outside the lock (at most what the Cup feed grants)
        books, book_reads = self._read_books(work.needs_book, now_eval)
        # 8. attach books, recompute trades, publish
        with self._lock:
            events, alert_rows = self._finish(now_eval, books, work)
            self._steps += 1
            self._last_step_at = now_eval
            self._last_error = errors[0] if errors else next(iter(self._io_errors.values()), None)
            self._publish(now_eval)
        # 9. persistence, alerts.jsonl and listeners outside every lock
        self._persist(work.sample_rows, alert_rows)
        self._append_jsonl(events)
        io_error = next(iter(self._io_errors.values()), None)
        if not errors and io_error != self._last_error:
            with self._lock:
                self._last_error = io_error
                status = dict(self._published_status)
                status["last_error"] = io_error
                self._published_status = status
        self._notify(events)
        polled_report = {name: {"status": str(getattr(res, "status", "error")), "requests": int(getattr(res, "requests", 0) or 0),
                                "quotes": len(getattr(res, "quotes", None) or {})} for name, res, _b, _d in polled}
        return MovesStepReport(at=now_eval, duration_s=max(0.0, float(self._clock()) - t_start), polled=polled_report,
                               samples=work.samples, opened=list(work.opened), updated=sorted(set(work.updated)),
                               closed=list(work.closed), suppressed=work.suppressed, book_reads=book_reads,
                               errors=errors + ([io_error] if io_error and io_error not in errors else []))

    # ------------------------------------------------------------------ warm-up and restart (§11)

    def _warm_up(self, start: float, errors: List[str]) -> List[Tuple[str, str]]:
        p = self.params
        alerts_raw: List[Dict[str, Any]] = []
        samples_raw: List[Dict[str, Any]] = []
        history: Mapping[str, Sequence[Any]] = {}
        if self._persistence is not None:
            try:
                alerts_raw = list(self._persistence.move_alerts(since=start - MOVES_HISTORY_KEEP_S))
            except Exception as exc:
                log.warning("stored outside-move alerts could not be read: %s", exc)
                errors.append(f"Stored outside-move alerts could not be read ({type(exc).__name__}).")
            try:
                samples_raw = list(self._persistence.outside_quotes(since=start - p.vol_lookback_s, until=start))
            except Exception as exc:
                log.warning("stored outside quotes could not be read: %s", exc)
                errors.append(f"Stored outside quotes could not be read ({type(exc).__name__}).")
        if self._cup is not None:
            try:
                history = self._cup.history(MOVES_CUP_KEEP_S) or {}
            except Exception as exc:
                log.warning("Cup history for the outside-move watcher could not be read: %s", exc)
                history = {}
        events: List[Tuple[str, str]] = []
        with self._lock:
            newest_sample: Optional[float] = None
            for row in sorted(samples_raw, key=lambda r: (float(r.get("ts") or 0.0), str(r.get("exchange_id")),
                                                           str(r.get("venue")))):
                try:
                    clean = _quote_row(row)
                    sample = _build(OutsideSample, clean)
                except Exception:
                    continue
                if sample.ts > start + _EPS:
                    continue
                key = (sample.exchange_id, sample.venue)
                series = self._series.get(key)
                if series is None:
                    series = self._series[key] = SeriesBuffer(sample.exchange_id, sample.venue, p)
                if series.add(sample):
                    self._last_stored[key] = (sample.ts, sample.bid, sample.ask, sample.value, tuple(sample.flags))
                    newest_sample = sample.ts if newest_sample is None else max(newest_sample, sample.ts)
                    series.restored_until = sample.ts
            for eid, quotes in dict(history).items():
                for q in quotes or []:
                    self._add_cup(str(eid), q, start)
            loaded: List[MoveAlert] = []
            for raw in alerts_raw:
                try:
                    loaded.append(MoveAlert.from_dict(raw))
                except Exception:
                    log.warning("a stored outside-move alert could not be read: %s", str(raw.get("alert_id"))[:80])
            loaded.sort(key=lambda a: (a.detected_at, a.alert_id))
            for alert in loaded:
                self._alerts[alert.alert_id] = alert
                self._grown_peak[alert.alert_id] = float(alert.peak_move)
                self._last_alert[alert.exchange_id] = alert.alert_id
                if alert.state == "open":
                    old = self._open.get(alert.exchange_id)
                    if old is not None and old in self._alerts and old != alert.alert_id:
                        self._close(self._alerts[old], start, events)  # never two open alerts for one outcome
                    self._open[alert.exchange_id] = alert.alert_id
                    self._live.add(alert.alert_id)
                elif self._captures_pending(alert, start):
                    self._live.add(alert.alert_id)
                self._dicts[alert.alert_id] = alert.to_dict()
            open_updates = [self._alerts[a].updated_at for a in self._open.values()]
            marks = [m for m in [newest_sample] + open_updates if m is not None]
            if self._open and marks:
                gap = start - max(marks)
                if gap > p.resume_gap_s + _EPS:
                    self._censor_open(start, f"The bot was not running for {gap / 60.0:.0f} min while this was measured.",
                                      events)
            self._warmed = True
        return events

    # ------------------------------------------------------------------ the locked computation

    def _compute(self, now: float, targets: Sequence[Any], active: Sequence[Any],
                 polled: Sequence[Tuple[str, Any, Optional[Mapping[str, Any]], float]],
                 cup_quotes: Mapping[str, Any]) -> _Work:
        p = self.params
        work = _Work()
        ids = [str(t.exchange_id) for t in active]
        id_set = set(ids)
        self._active_ids = ids
        if self._last_step_at is not None and now - self._last_step_at > p.resume_gap_s + _EPS:
            self._censor_open(now, f"The bot was not running for {(now - self._last_step_at) / 60.0:.0f} min while "
                                   "this was measured.", work.events)
        names: List[str] = []
        for name, res, budget, done in polled:
            names.append(name)
            if name not in self._venues:
                self._venue_order.append(name)
                self._venues[name] = VenueStatus(venue=name, label=venue_label(name), status="pending")
            quoted = 0
            liquidity = getattr(res, "liquidity", None) or {}
            for eid, quote in sorted((getattr(res, "quotes", None) or {}).items(), key=lambda kv: str(kv[0])):
                eid = str(eid)
                if eid not in id_set or quote is None or _num(getattr(quote, "fetched_at", None)) is None:
                    continue
                if float(quote.fetched_at) > now + _EPS:
                    continue  # no look-ahead: a quote stamped after the evaluation time is not used
                try:
                    sample = sample_from_quote(eid, quote, liquidity=liquidity.get(eid))
                except (TypeError, ValueError):
                    continue
                sample.venue = name
                if is_thin(sample, p) and "thin" not in sample.flags:
                    sample.flags.append("thin")
                key = (eid, name)
                series = self._series.get(key)
                if series is None:
                    series = self._series[key] = SeriesBuffer(eid, name, p)
                if series.add(sample):
                    work.samples += 1
                    if sample.value is not None:
                        quoted += 1
                    row = self._record_row(sample)
                    if row is not None:
                        work.sample_rows.append(row)
            self._update_venue(name, res, budget, done, quoted, id_set)
        # the Cup series of the watched outcomes only (every target, disabled ones too: their lag is still measured);
        # when the targets could not be read at all, keep every snapshot so no open alert loses its Cup data
        watched = {str(t.exchange_id) for t in targets}
        for eid, quote in cup_quotes.items():
            if not watched or str(eid) in watched:
                self._add_cup(str(eid), quote, now)
        self._prune(now, id_set)
        by_id = {str(t.exchange_id): t for t in active}
        for eid in ids:
            self._evaluate_target(by_id[eid], now, names, work)
        # an outcome that left the targets (closed, settled) censors its pending lag; a target whose outside value
        # the user switched off is still a target (its Cup follow is still measured)
        self._advance(now, {str(t.exchange_id) for t in targets}, work)
        for eid, aid in sorted(self._open.items()):
            alert = self._alerts[aid]
            if alert.status == "lagging":
                tried = self._book_tried.get(aid)
                if tried is None or now - tried >= MOVES_BOOK_MAX_AGE_S - _EPS:
                    work.needs_book.append((aid, eid))
        return work

    def _update_venue(self, name: str, res: Any, budget: Optional[Mapping[str, Any]], done: float, quoted: int,
                      ids: Set[str]) -> None:
        vs = self._venues[name]
        status = str(getattr(res, "status", "error") or "error")
        if status not in VENUE_STATUSES:
            status = "error"
        errs = [str(e) for e in (getattr(res, "errors", None) or [])]
        vs.status = status
        vs.last_poll_at = done
        vs.requests_last_poll = int(getattr(res, "requests", 0) or 0)
        vs.next_try_at = _num(getattr(res, "next_try_at", None))
        if status in ("ok", "partial"):
            quotes = getattr(res, "quotes", None) or {}
            matches = getattr(res, "matches", None) or {}
            rejected = getattr(res, "rejected", None) or {}
            if vs.requests_last_poll > 0 or quotes or matches or rejected:
                vs.last_ok_at = done
            vs.last_error = errs[0] if status == "partial" and errs else None
            matched = {str(e) for e in list(quotes) + list(matches) + list(rejected)} & ids
            self._venue_matched[name] = matched
            vs.matched = len(matched)
            vs.quoted = quoted
        elif status == "busy":
            pass  # the fair-value refresh holds the host: the previous numbers stand
        else:
            vs.last_error = errs[0] if errs else vs.last_error
            vs.quoted = 0
            if status == "pending":
                vs.matched = 0
                self._venue_matched[name] = set()
        if isinstance(budget, Mapping):
            vs.budget_used = int(_num(budget.get("used")) or 0)
            vs.budget_limit = int(_num(budget.get("limit")) or 0)

    def _record_row(self, sample: OutsideSample) -> Optional[Dict[str, Any]]:
        """§11: store a sample when its value, bid or ask moved by a tick, its flags changed, or 300 s passed."""
        key = (sample.exchange_id, sample.venue)
        cur = (sample.ts, sample.bid, sample.ask, sample.value, tuple(sample.flags))
        last = self._last_stored.get(key)
        changed = last is None or sample.ts - last[0] >= MOVES_QUOTE_HEARTBEAT_S - _EPS or last[4] != cur[4]
        if not changed:
            for a, b in zip(last[1:4], cur[1:4]):  # type: ignore[index]
                if (a is None) != (b is None) or (a is not None and abs(float(a) - float(b)) >= MOVES_QUOTE_RECORD_MIN_CHANGE - _EPS):
                    changed = True
                    break
        if not changed:
            return None
        self._last_stored[key] = cur
        return sample.to_dict()

    def _add_cup(self, eid: str, raw: Any, now: float) -> None:
        if isinstance(raw, CupQuote):
            q = CupQuote(ts=float(raw.ts), bid=_num(raw.bid), ask=_num(raw.ask), mid=_num(raw.mid), last=_num(raw.last),
                         spread=_num(raw.spread))
        elif isinstance(raw, Mapping) and _num(raw.get("ts")) is not None:
            q = CupQuote(ts=float(raw["ts"]), bid=_num(raw.get("bid")), ask=_num(raw.get("ask")), mid=_num(raw.get("mid")),
                         last=_num(raw.get("last")), spread=_num(raw.get("spread")))
        else:
            return
        if q.ts > now + _EPS:
            return  # no look-ahead
        if q.mid is None and q.bid is not None and q.ask is not None:
            q.mid = _r6((q.bid + q.ask) / 2.0)
        if q.mid is None:
            return
        if q.spread is None and q.bid is not None and q.ask is not None:
            q.spread = _r6(q.ask - q.bid)
        series = self._cup_series.setdefault(eid, [])
        if not series or q.ts > series[-1].ts + _EPS:
            series.append(q)
        elif all(abs(x.ts - q.ts) > _EPS for x in series):  # an older snapshot (history at warm-up): keep the order
            series.append(q)
            series.sort(key=lambda x: x.ts)

    def _prune(self, now: float, ids: Set[str]) -> None:
        cutoff = now - MOVES_CUP_KEEP_S
        for eid in list(self._cup_series):
            series = self._cup_series[eid]
            k = 0
            while k < len(series) - 1 and series[k].ts < cutoff:
                k += 1
            if k:
                del series[:k]
            if series and series[-1].ts < cutoff and eid not in ids:
                del self._cup_series[eid]
        for key in list(self._series):
            series = self._series[key]
            series.prune(now)
            latest = series._ts[-1] if len(series._ts) else None
            if key[0] not in ids and (latest is None or now - latest > self.params.vol_lookback_s):
                del self._series[key]
        for key in [k for k, at in self._suppress_seen.items() if now - at > MOVES_SUPPRESS_REPEAT_S]:
            del self._suppress_seen[key]
        while self._suppress_log and now - self._suppress_log[0][0] > 86400.0:
            self._suppress_log.popleft()
        for eid in [e for e, (book, _src) in self._books.items() if now - float(book.observed_at) > MOVES_BOOK_MAX_AGE_S]:
            del self._books[eid]
        old = [aid for aid, a in self._alerts.items()
               if a.state != "open" and aid not in self._live and a.detected_at < now - MOVES_HISTORY_KEEP_S]
        for aid in old:
            self._alerts.pop(aid, None)
            self._dicts.pop(aid, None)
            self._grown_peak.pop(aid, None)
            self._book_tried.pop(aid, None)

    # ------------------------------------------------------------------ detection, filters, debounce

    def _evaluate_target(self, target: Any, now: float, names: Sequence[str], work: _Work) -> None:
        p = self.params
        eid = str(target.exchange_id)
        cup_series = self._cup_series.get(eid, [])
        cup_now = cup_at(cup_series, now, p.cup_max_age_s)
        triggers: List[CombinedMove] = []
        held: List[Tuple[int, Tuple[Any, ...], Dict[str, Any]]] = []  # would-be suppressions of this step
        for w in p.windows_s:
            w = float(w)
            moves: Dict[str, Optional[VenueMove]] = {}
            for name in names:
                series = self._series.get((eid, name))
                if series is None or not len(series):
                    continue
                vm = evaluate_window(series, now, w, p)
                if vm is None:
                    self._spikes.pop((eid, name, w), None)
                    continue
                self._mark(vm, series, target, w)
                moves[name] = vm
            move, reason, who = _combine(eid, w, moves, p)
            if move is None:
                if reason and who:
                    main = max(who, key=lambda v: v.ratio)
                    held.append((_sign(main.move), (now, target, reason, w, who, moves, work), {}))
                continue
            if cup_now is None or cup_now.mid is None:
                held.append((move.direction, (now, target, "no_cup", w, move.venue_moves, moves, work),
                             {"combined": move}))
                continue
            if not getattr(target, "confirmed", False) and abs(move.now - cup_now.mid) > p.suspect_gap + _EPS:
                held.append((move.direction, (now, target, "suspect", w, move.venue_moves, moves, work),
                             {"combined": move, "cup_mid": cup_now.mid}))
                continue
            triggers.append(move)
        # a window that triggered makes the same-direction filters of the other windows moot: that move is alerted
        alerted = {m.direction for m in triggers[:1]}
        for d, args, kw in held:
            if d not in alerted:
                self._suppress(*args, **kw)
        if triggers:
            self._debounce(target, triggers, now, cup_series, work)

    def _mark(self, vm: VenueMove, series: SeriesBuffer, target: Any, w: float) -> None:
        """The per-venue filters the watcher knows and evaluate_window does not (§6.2): match confidence, NEAR
        matches, and single prints (state across steps)."""
        p = self.params
        latest = series.latest()
        if latest is None:
            return
        if latest.match_confidence < p.min_match_confidence - _EPS:
            vm.flags.append("match")
        if str(latest.match_kind).upper() == "NEAR" and not getattr(target, "trade_near", False):
            vm.flags.append("near")
        key = (series.exchange_id, series.venue, w)
        raw = float(vm.now_value) - float(vm.base_value)
        raw_ratio = abs(raw) / vm.threshold if vm.threshold > 0 else 0.0
        prev = self._spikes.get(key)
        if prev is not None and prev[0] >= latest.ts - _EPS:
            return  # no new sample since the previous step: nothing new to judge
        if (prev is not None and prev[1] >= 1.0 - 1e-6 and prev[2] < 1.0 - 1e-6
                and abs(raw) < 0.5 * vm.threshold - _EPS and "placeholder" not in vm.flags):
            vm.flags.append("single_tick")
            self._spike_moves[key] = prev[3]
        self._spikes[key] = (latest.ts, raw_ratio, vm.ratio, raw)

    def _suppress(self, now: float, target: Any, reason: str, w: float, who: Sequence[VenueMove],
                  moves: Mapping[str, Optional[VenueMove]], work: _Work, *, combined: Optional[CombinedMove] = None,
                  cup_mid: Optional[float] = None) -> None:
        eid = str(target.exchange_id)
        main = combined.venue_moves[0] if combined is not None else (max(who, key=lambda v: v.ratio) if who else None)
        if main is None:
            return
        signed = combined.direction * combined.move if combined is not None else float(main.move)
        if reason == "single_tick":
            signed = self._spike_moves.get((eid, main.venue, w), signed)
        open_id = self._open.get(eid)
        if open_id is not None and _sign(signed) == self._alerts[open_id].direction:
            return  # an alert for this move is already open: not a would-be alert
        key = (eid, reason)
        last = self._suppress_seen.get(key)
        self._suppress_seen[key] = now
        if last is not None and now - last < MOVES_SUPPRESS_REPEAT_S - _EPS:
            return  # the same filter on the same outcome counts once per episode
        detail = self._suppress_detail(reason, eid, w, now, who, combined, cup_mid)
        threshold = combined.threshold if combined is not None else main.threshold
        self._suppressed.append(SuppressedMove(
            exchange_id=eid, title=str(target.title), race_label=race_label(getattr(target, "race", None), str(target.title)),
            reason=reason, reason_label=SUPPRESS_LABELS.get(reason, reason), at=float(now), window_s=float(w),
            venues=[v.venue for v in (combined.venue_moves if combined is not None else who)], move=_r6(signed),
            threshold=_r6(threshold), detail=detail))
        self._suppress_log.append((float(now), reason))
        work.suppressed += 1

    def _suppress_detail(self, reason: str, eid: str, w: float, now: float, who: Sequence[VenueMove],
                         combined: Optional[CombinedMove], cup_mid: Optional[float]) -> str:
        p = self.params
        vm = who[0] if who else None
        label = vm.label if vm is not None else ""
        series = self._series.get((eid, vm.venue)) if vm is not None else None
        latest = series.latest() if series is not None else None
        if reason == "placeholder":
            return f"{label}'s quote turned one-sided: not a price."
        if reason == "match":
            conf = latest.match_confidence if latest is not None else 0.0
            return f"{label} match with confidence {conf:.2f}: too uncertain to alert on."
        if reason == "near":
            return "Near match (different settlement wording): not alerted."
        if reason == "stale":
            longest = 0.0
            if series is not None and vm is not None:
                pts = series.points(vm.base_ts, now)
                for (a, _va), (b, _vb) in zip(pts, pts[1:]):
                    if series.interrupted(a, b, p.gap_s):
                        longest = max(longest, b - a)
                if pts:
                    longest = max(longest, now - pts[-1][0])
            return f"{label} was not read for {fmt_duration(longest)} inside this window."
        if reason == "thin":
            rows: List[Tuple[Any, ...]] = []
            if series is not None and vm is not None:
                for t in (vm.after_ts, vm.base_ts):
                    i = bisect.bisect_left(series._ts, t - _EPS)
                    if i < len(series._ts):
                        rows.append(series._rows[i])
                if len(series._rows):
                    rows.insert(0, series._rows[-1])
            for row in rows:
                why = _thin_parts(row[_ROW_SPREAD], row[_ROW_LIQ], row[_ROW_BID_SIZE], row[_ROW_ASK_SIZE], p)
                if why == "spread":
                    return f"{label}'s book was {row[_ROW_SPREAD] * 100:.0f} pts wide during the move."
                if why == "liquidity":
                    return f"{label} had only ${row[_ROW_LIQ]:,.0f} of liquidity during the move."
                if why == "size":
                    sizes = [s for s in (row[_ROW_BID_SIZE], row[_ROW_ASK_SIZE]) if s is not None]
                    return f"{label} had only {min(sizes):.0f} contracts at the touch during the move."
            return f"{label}'s book was thin during the move."
        if reason == "single_tick":
            raw = self._spike_moves.get((eid, vm.venue, w)) if vm is not None else None
            return f"One print of {fmt_pts(raw)} that did not hold."
        if reason == "disagree":
            return ", ".join(f"{v.label} {_pts_word(v.move)}" for v in who) + ": the venues disagree."
        if reason == "suspect" and combined is not None and cup_mid is not None:
            return f"Outside {combined.now:.2f} vs Cup {cup_mid:.2f}: check the match."
        if reason == "no_cup":
            return f"No Cup price in the last {p.cup_max_age_s / 60:.0f} minutes to compare with."
        return SUPPRESS_LABELS.get(reason, reason)

    def _debounce(self, target: Any, triggers: List[CombinedMove], now: float, cup_series: Sequence[CupQuote],
                  work: _Work) -> None:
        """§7: one open alert per outcome, updated in place; an opposite trigger first checks for a reversion;
        re-arming needs a base at or after the previous alert's last growth."""
        eid = str(target.exchange_id)
        open_id = self._open.get(eid)
        alert = self._alerts.get(open_id) if open_id is not None else None
        if alert is not None:
            same = [m for m in triggers if m.direction == alert.direction]
            if same:
                self._grow(alert, same, now, work)
                return
            self._update_lag(alert, now, work)
            if alert.lag.outcome != "pending" or not alert.lag.eligible:
                # reverted (or otherwise resolved: followed, cup first, excluded): the old move is over, so close it
                # now; the opposite trigger still needs t_base >= its grown_at (the re-arm rule below)
                self._close(alert, now, work.events, work)
            else:
                return  # ignored until the open alert resolves
        group = [m for m in triggers if m.direction == triggers[0].direction]
        primary = group[0]  # the shortest window that triggered
        last_id = self._last_alert.get(eid)
        last = self._alerts.get(last_id) if last_id is not None else None
        if last is not None and primary.t_base < last.grown_at - _EPS:
            return  # re-arm: one move never gives a second alert
        self._open_alert(target, primary, group, now, cup_series, work)

    def _venue_rank(self, venue: str) -> Tuple[int, str]:
        return (self._venue_order.index(venue) if venue in self._venue_order else len(self._venue_order), venue)

    def _open_alert(self, target: Any, primary: CombinedMove, group: Sequence[CombinedMove], now: float,
                    cup_series: Sequence[CupQuote], work: _Work) -> None:
        p = self.params
        eid = str(target.exchange_id)
        status, reason, ctx, exclusion = classify(primary, cup_series, now, p)
        if status == "no_cup" or ctx.now is None:
            self._suppress(now, target, "no_cup", primary.window_s, primary.venue_moves, {}, work, combined=primary)
            return
        aid = f"mv-{eid}-{int(primary.t_base)}"
        if aid in self._alerts:
            return
        d = primary.direction
        race = getattr(target, "race", None)
        flags: List[str] = []
        if primary.confirmation == "one venue":
            flags.append("one venue")
        if any(not vm.vol_known for vm in primary.venue_moves):
            flags.append("warm-up thresholds")
        if ctx.short_history:
            flags.append("short Cup history")
        if ctx.now.spread is not None and ctx.now.spread > _MAX_VENUE_SPREAD + _EPS:
            flags.append("wide Cup book")
        lag = MoveLag(t_move=primary.t_move)
        if exclusion is not None:
            lag.eligible = False
            lag.outcome = "excluded"
            lag.excluded_reason = exclusion
            lag.resolved_at = float(now)
        elif status == "moved_first" and ctx.half_cross_ts is not None:
            lag.outcome = "cup_first"
            lag.lag_s = _r6(ctx.half_cross_ts - primary.t_move)
            lag.resolved_at = float(now)
            lag.reason = reason
        venues = sorted({v for m in group for v in m.venues}, key=self._venue_rank)
        lag_gap = _r6(primary.move - (ctx.move_same_window or 0.0))
        alert = MoveAlert(
            alert_id=aid, exchange_id=eid, market_id=str(getattr(target, "market_id", "") or ""), title=str(target.title),
            option=getattr(target, "option", None), race_key=race.race_key if race is not None else None,
            party=race.party if race is not None else None, race_label=race_label(race, str(target.title)), direction=d,
            window_s=float(primary.window_s), windows=sorted({float(m.window_s) for m in group}), venues=venues,
            confirmation=primary.confirmation, venue_moves=list(primary.venue_moves), t_base=float(primary.t_base),
            t_move=float(primary.t_move), outside_before=primary.before, outside_after=primary.after,
            outside_now=primary.now, move=primary.move, peak_move=primary.move, threshold=primary.threshold,
            sigma=primary.sigma, vol_known=primary.vol_known, uncertainty=primary.uncertainty, detected_at=float(now),
            updated_at=float(now), grown_at=float(now), state="open", status=status,
            status_label=MOVE_STATUS_LABELS[status], reason=reason, cup=ctx, lag_gap=lag_gap if lag_gap is not None else 0.0,
            lag=lag, cup_now=ctx.now, lag_gap_now=lag_gap, level_gap_now=_r6(d * (primary.now - ctx.now.mid)),
            flags=flags, demo=self.demo, opened_status=status)
        if alert.race_key:
            for other_eid, other_id in self._open.items():
                other = self._alerts.get(other_id)
                if (other is not None and other_eid != eid and other.race_key == alert.race_key
                        and abs(now - other.detected_at) <= MOVES_LINK_S + _EPS):
                    alert.linked.append(other.alert_id)
                    if aid not in other.linked:
                        other.linked.append(aid)
                        self._dirty.add(other.alert_id)
        self._alerts[aid] = alert
        self._open[eid] = aid
        self._last_alert[eid] = aid
        self._grown_peak[aid] = float(alert.peak_move)
        self._live.add(aid)
        self._dirty.add(aid)
        work.events.append(("opened", aid))
        work.opened.append(aid)

    def _grow(self, alert: MoveAlert, same: Sequence[CombinedMove], now: float, work: _Work) -> None:
        d = alert.direction
        original = self._measure_venues(alert)
        for m in same:
            if float(m.window_s) not in alert.windows:
                alert.windows.append(float(m.window_s))
            for v in m.venues:
                if v not in alert.venues:
                    alert.venues.append(v)
            after = {vm.venue: vm.after_value for vm in m.venue_moves}
            if original and all(v in after for v in original):
                # the confirmed (conservative: the confirming sample nearest the base) level of the SAME venues the
                # alert's fixed base was measured on, so a single print never sets the peak
                level = sum(after[v] for v in original) / len(original)
                peak = _r6(d * (level - alert.outside_before))
                if peak is not None and peak > alert.peak_move + _EPS:
                    alert.peak_move = peak
        alert.windows.sort()
        alert.venues.sort(key=self._venue_rank)
        base = self._grown_peak.get(alert.alert_id, float(alert.move))
        if alert.peak_move - base >= MOVES_GROWTH_STEP - _EPS:
            alert.grown_at = float(now)
            self._grown_peak[alert.alert_id] = float(alert.peak_move)
        self._dirty.add(alert.alert_id)
        work.updated.append(alert.alert_id)

    # ------------------------------------------------------------------ lag, close, censor

    def _captures_pending(self, alert: MoveAlert, now: float) -> bool:
        if (alert.opened_status or alert.status) != "lagging":
            return False
        if alert.lag.capture_at is not None and alert.lag.exit_at is not None:
            return False
        # (integration) + cup_max_age_s: a capture waits for the Cup snapshot after its time (see update_lag)
        return now <= (alert.detected_at + self.params.human_delay_s + self.params.exit_after_s
                       + self.params.cup_max_age_s + 2 * self.params.poll_s)

    @staticmethod
    def _measure_venues(alert: MoveAlert) -> List[str]:
        """The venues the alert's fixed base (``outside_before``) was measured on: its confirming venues at
        detection. ``outside_now``, the reversion points and the peak use exactly these, so a venue that joins later
        never mixes a different level into the comparison with the base (the trade still uses every venue)."""
        venues = [vm.venue for vm in alert.venue_moves]
        return venues or list(alert.venues)

    def _outside_points(self, alert: MoveAlert, now: float) -> List[Tuple[float, float]]:
        """The confirming venues' mean value at each poll since the detection (as-of each venue's freshest sample)."""
        p = self.params
        since = alert.detected_at - p.poll_s
        series = [self._series.get((alert.exchange_id, v)) for v in self._measure_venues(alert)]
        present = [s for s in series if s is not None and len(s)]
        if not present:
            return []
        ref = max(present, key=lambda s: (len(s.points(since, now)), -self._venue_rank(s.venue)[0]))
        out: List[Tuple[float, float]] = []
        for t, _v in ref.points(since, now):
            vals = [got[1] for got in (s.value_at(t, p.stale_s) for s in present) if got is not None]
            if vals:
                out.append((t, _r6(sum(vals) / len(vals))))  # type: ignore[arg-type]
        return out

    def _update_lag(self, alert: MoveAlert, now: float, work: _Work) -> None:
        before = (alert.status, alert.lag.outcome, alert.lag.capture_at, alert.lag.exit_at)
        need_points = alert.lag.outcome == "pending" or self._captures_pending(alert, now)
        points = self._outside_points(alert, now) if need_points else []
        update_lag(alert, self._cup_series.get(alert.exchange_id, []), points, now, self.params)
        after = (alert.status, alert.lag.outcome, alert.lag.capture_at, alert.lag.exit_at)
        if after != before:
            self._dirty.add(alert.alert_id)
        if after[:2] != before[:2]:
            work.events.append(("status", alert.alert_id))

    def _close(self, alert: MoveAlert, now: float, events: List[Tuple[str, str]], work: Optional[_Work] = None) -> None:
        if alert.state == "closed":
            return
        alert.state = "closed"
        alert.closed_at = float(now)
        alert.actionable = False
        alert.trade = None
        alert.trade_note = None
        if self._open.get(alert.exchange_id) == alert.alert_id:
            del self._open[alert.exchange_id]
        if not self._captures_pending(alert, now):
            self._live.discard(alert.alert_id)
        self._dirty.add(alert.alert_id)
        events.append(("closed", alert.alert_id))
        if work is not None:
            work.closed.append(alert.alert_id)

    def _censor(self, alert: MoveAlert, now: float, reason: str, events: List[Tuple[str, str]]) -> None:
        if alert.lag.outcome == "pending" and alert.lag.eligible:
            alert.lag.outcome = "censored"
            alert.lag.resolved_at = float(now)
            alert.lag.reason = reason
            self._dirty.add(alert.alert_id)
            events.append(("status", alert.alert_id))

    def _censor_open(self, now: float, reason: str, events: List[Tuple[str, str]]) -> None:
        for aid in sorted(self._open.values()):
            alert = self._alerts[aid]
            self._censor(alert, now, reason, events)
            self._close(alert, now, events)

    def _advance(self, now: float, ids: Set[str], work: _Work) -> None:
        p = self.params
        for aid in sorted(self._live, key=lambda a: (self._alerts[a].detected_at, a) if a in self._alerts else (0.0, a)):
            alert = self._alerts.get(aid)
            if alert is None:
                self._live.discard(aid)
                continue
            if alert.state == "open":
                if ids and alert.exchange_id not in ids:
                    self._censor(alert, now, "The market closed before the Cup followed.", work.events)
                    self._close(alert, now, work.events, work)
                    continue
                self._update_lag(alert, now, work)
                if alert.lag.outcome == "not_followed":
                    self._close(alert, now, work.events, work)
                elif ((alert.lag.outcome != "pending" or not alert.lag.eligible)
                      and now - alert.grown_at >= p.close_quiet_s - _EPS):
                    self._close(alert, now, work.events, work)
            else:
                if self._captures_pending(alert, now):
                    self._update_lag(alert, now, work)
                if not self._captures_pending(alert, now):
                    self._live.discard(aid)

    # ------------------------------------------------------------------ books, trades, publishing

    def _read_books(self, needs: Sequence[Tuple[str, str]], now: float
                    ) -> Tuple[Dict[str, Tuple[str, Optional[Tuple[BookObservation, str]]]], int]:
        from .readonly import ReadOnlyViolation

        out: Dict[str, Tuple[str, Optional[Tuple[BookObservation, str]]]] = {}
        reads = 0
        if self._cup is None:
            return out, 0
        for aid, eid in needs:
            got: Optional[Tuple[BookObservation, str]] = None
            try:
                stored = self._cup.stored_book(eid)
                if stored is not None and now - float(stored.observed_at) <= self.params.book_max_age_s + _EPS:
                    got = (stored, "stored")
                else:
                    book = self._cup.book(eid)
                    if book is not None:
                        got = (book, "read")
                        reads += 1
            except ReadOnlyViolation:
                raise
            except Exception as exc:  # the Cup feed reports its own problems; no book means "shares unknown"
                log.warning("outside-move order book for %s failed: %s", eid, exc)
            out[aid] = (eid, got)
        return out, reads

    def _fresh_values(self, alert: MoveAlert, now: float,
                      venues: Optional[Sequence[str]] = None) -> Dict[str, Tuple[float, Optional[float]]]:
        """venue -> (value, spread) of the alert's venues whose latest valued sample is not stale."""
        out: Dict[str, Tuple[float, Optional[float]]] = {}
        for v in (alert.venues if venues is None else venues):
            series = self._series.get((alert.exchange_id, v))
            if series is None:
                continue
            got = series.value_at(now, self.params.stale_s)
            if got is None:
                continue
            i = bisect.bisect_left(series._ts, got[0] - _EPS)
            out[v] = (got[1], series._rows[i][_ROW_SPREAD] if i < len(series._rows) else None)
        return out

    def _refresh_open(self, alert: MoveAlert, now: float) -> None:
        p = self.params
        d = alert.direction
        cup_now = cup_at(self._cup_series.get(alert.exchange_id, []), now, p.cup_max_age_s)
        alert.cup_now = cup_now
        fresh = self._fresh_values(alert, now)
        measured = self._measure_venues(alert)
        if measured and all(v in fresh for v in measured):
            alert.outside_now = _r6(sum(fresh[v][0] for v in measured) / len(measured))  # type: ignore[assignment]
        alert.lag_gap_now = _gap_now(alert, cup_now)
        alert.level_gap_now = _r6(d * (alert.outside_now - cup_now.mid)) if cup_now is not None and cup_now.mid is not None else None
        if cup_now is not None and cup_now.spread is not None and cup_now.spread > _MAX_VENUE_SPREAD + _EPS:
            if "wide Cup book" not in alert.flags:
                alert.flags.append("wide Cup book")
        trade: Optional[HandTrade] = None
        note: Optional[str] = None
        if alert.state == "open" and alert.status == "lagging":
            if cup_now is None:
                note = f"No Cup price in the last {p.cup_max_age_s / 60:.0f} minutes: no trade suggested."
            elif not fresh:
                note = "No fresh outside price right now: no trade suggested."
            else:
                values = [v for v, _s in fresh.values()]
                spreads = [s for _v, s in fresh.values() if s is not None]
                u = max(max(values) - min(values), (min(spreads) / 2.0) if spreads else 0.0,
                        max(MOVES_FEE_BAND.get(v, 0.02) for v in fresh))
                book = self._books.get(alert.exchange_id)
                if book is not None and now - float(book[0].observed_at) > p.book_max_age_s + _EPS:
                    book = None
                trade, note = hand_trade(d, values, u, cup_now, book[0] if book else None, book[1] if book else None,
                                         now, p)
        alert.trade = trade
        alert.trade_note = note if trade is None else None
        alert.actionable = alert.state == "open" and alert.status == "lagging" and trade is not None
        alert.updated_at = float(now)
        self._dirty.add(alert.alert_id)

    def _finish(self, now: float, books: Mapping[str, Tuple[str, Optional[Tuple[BookObservation, str]]]], work: _Work
                ) -> Tuple[List[MoveEvent], List[Dict[str, Any]]]:
        for aid, (eid, got) in books.items():
            self._book_tried[aid] = float(now)
            if got is not None:
                self._books[eid] = got
        for aid in sorted(self._open.values()):
            self._refresh_open(self._alerts[aid], now)
        for aid in work.opened:  # what the "opened" event says, kept after the live suggestion is cleared
            alert = self._alerts.get(aid)
            if alert is not None and alert.state == "open":
                alert.opened_trade = copy.deepcopy(alert.trade)
                alert.opened_trade_note = alert.trade_note
        rows: List[Dict[str, Any]] = []
        for aid in sorted(self._dirty):
            alert = self._alerts.get(aid)
            if alert is None:
                continue
            d = alert.to_dict()
            self._dicts[aid] = d
            rows.append(d)
        self._dirty.clear()
        events: List[MoveEvent] = []
        for kind, aid in work.events:
            alert = self._alerts.get(aid)
            if alert is None:
                continue
            d = copy.deepcopy(self._dicts.get(aid) or alert.to_dict())
            events.append(MoveEvent(kind=kind, at=float(now), alert_id=aid, status=alert.status,
                                    lag_outcome=alert.lag.outcome, alert=d))
        return events, [copy.deepcopy(r) for r in rows]

    def _ordered(self) -> List[MoveAlert]:
        """Open first (actionable, then other open, newest first), then closed newest first."""
        open_ = sorted((a for a in self._alerts.values() if a.state == "open"),
                       key=lambda a: (not a.actionable, -a.detected_at, a.alert_id))
        closed = sorted((a for a in self._alerts.values() if a.state != "open"),
                        key=lambda a: (-a.detected_at, a.alert_id))
        return open_ + closed

    def _publish(self, now: float) -> None:
        ordered = self._ordered()
        dicts = []
        for a in ordered:
            d = self._dicts.get(a.alert_id)
            if d is None:
                d = self._dicts[a.alert_id] = a.to_dict()
            dicts.append(d)
        day = now - 86400.0
        open_alerts = [a for a in ordered if a.state == "open"]
        actionable = [a for a in open_alerts if a.actionable]
        ids = set(self._active_ids)
        for vs in self._venues.values():
            matched = self._venue_matched.get(vs.venue, set())
            stale = 0
            for eid in matched:
                series = self._series.get((eid, vs.venue))
                if series is None or not len(series) or now - series._ts[-1] > self.params.stale_s + _EPS:
                    stale += 1
            vs.stale = stale
        matched_any = set().union(*self._venue_matched.values()) & ids if self._venue_matched else set()
        counts_supp = {r: 0 for r in SUPPRESS_REASONS}
        for at, reason in self._suppress_log:
            if at >= day and reason in counts_supp:
                counts_supp[reason] += 1
        summary = {
            "poll_s": float(self.params.poll_s), "last_step_at": self._last_step_at, "steps": self._steps,
            "watching": {"outcomes": len(ids), "matched": len(matched_any), "venues": len(self._venue_order)},
            "venues": [self._venues[v].to_dict() for v in self._venue_order],
            "counts": {"open": len(open_alerts), "lagging": sum(1 for a in open_alerts if a.status == "lagging"),
                       "actionable": len(actionable), "today": sum(1 for a in ordered if a.detected_at >= day),
                       "closed_today": sum(1 for a in ordered if a.closed_at is not None and a.closed_at >= day),
                       "suppressed_today": counts_supp},
            "actionable_ids": [a.alert_id for a in sorted(actionable, key=lambda a: (-a.detected_at, a.alert_id))],
            "alerts": dicts[:MOVES_MAX_ALERTS_API],
            "suppressed": [s.to_dict() for s in reversed(self._suppressed)],
            "summary": summarise(dicts, now, self.params).to_dict(),
            "thresholds": self._thresholds(),
            "caveats": self._caveats(),
        }
        notes = []
        for a in sorted(actionable, key=lambda a: (-a.detected_at, a.alert_id))[:MOVES_MAX_ACTIONABLE_STATUS]:
            headline, body = notification_text(self._dicts[a.alert_id])
            notes.append({"alert_id": a.alert_id, "exchange_id": a.exchange_id, "headline": headline, "body": body,
                          "detected_at": a.detected_at})
        ok = sum(1 for v in self._venues.values() if v.status in ("ok", "partial"))
        status = {"enabled": True, "poll_s": float(self.params.poll_s), "last_step_at": self._last_step_at,
                  "steps": self._steps, "open": len(open_alerts), "actionable": len(actionable),
                  "actionable_alerts": notes, "venues": {"ok": ok, "total": len(self._venues)},
                  "last_error": self._last_error}
        compact = {}
        for a in open_alerts:
            compact[a.exchange_id] = {"alert_id": a.alert_id, "status": a.status, "status_label": a.status_label,
                                      "state": a.state, "direction": a.direction, "move": a.move,
                                      "lag_gap_now": a.lag_gap_now, "detected_at": a.detected_at,
                                      "actionable": a.actionable}
        # replace the references (readers hold the old dicts, which are never mutated)
        self._published_open = compact
        self._published = summary
        self._published_status = status

    # ------------------------------------------------------------------ persistence, alerts.jsonl, listeners

    def _io_problem(self, key: str, message: str, exc: BaseException) -> None:
        self._io_errors[key] = message
        now = time.monotonic()
        last = self._error_logged_at.get(key)
        if last is None or now - last >= _ERROR_LOG_EVERY_S:
            self._error_logged_at[key] = now
            log.warning("%s (%s)", message, exc)

    def _persist(self, sample_rows: Sequence[Mapping[str, Any]], alert_rows: Sequence[Mapping[str, Any]]) -> None:
        if self._persistence is None:
            return
        from .readonly import ReadOnlyViolation

        try:
            if sample_rows:
                self._persistence.add_outside_quotes(sample_rows)
            if alert_rows:
                self._persistence.put_move_alerts(alert_rows)
            self._io_errors.pop("store", None)
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            self._io_problem("store", f"Outside-move data could not be saved ({type(exc).__name__}): alerts are still "
                                      "shown, but a restart will not remember them.", exc)

    def _append_jsonl(self, events: Sequence[MoveEvent]) -> None:
        if not events or self.alerts_path is None:
            return
        lines = []
        for ev in events:
            lines.append(json.dumps({"event": ev.kind, "at": ev.at, "at_iso": iso_ts(ev.at), "alert": ev.alert},
                                    ensure_ascii=False, allow_nan=False, default=str))
        try:
            self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.alerts_path, "a", encoding="utf-8") as fh:  # append only: never truncated or rewritten
                fh.write("\n".join(lines) + "\n")
                fh.flush()
            self._io_errors.pop("file", None)
        except (OSError, ValueError) as exc:
            self._io_problem("file", f"The alerts file {self.alerts_path} could not be written ({type(exc).__name__}): "
                                     "alerts are still shown here.", exc)

    def _notify(self, events: Sequence[MoveEvent]) -> None:
        for ev in events:
            for fn in list(self._listeners):
                try:
                    fn(ev)
                except Exception as exc:  # a listener never breaks the watcher
                    log.warning("an outside-move listener failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------ readers

    def summary(self) -> Dict[str, Any]:
        """The published body behind /api/moves (§18.2, minus the keys web.py adds); lock-free, a fresh copy is
        not needed (the dict is replaced, never mutated)."""
        return self._published

    def status(self) -> Dict[str, Any]:
        """The compact published status (§12.4): {"enabled", "poll_s", "last_step_at", "steps", "open",
        "actionable", "actionable_alerts", "venues": {"ok", "total"}, "last_error"}; lock-free."""
        return self._published_status

    def alerts(self, state: Optional[str] = None, since: Optional[float] = None,
               limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Alert dicts from memory (every alert of the last 30 days), ordered as in summary(); takes the lock briefly."""
        with self._lock:
            ordered = self._ordered()
            out = []
            for a in ordered:
                if state in ("open", "closed") and a.state != state:
                    continue
                if since is not None and a.detected_at < float(since) - _EPS:
                    continue
                d = self._dicts.get(a.alert_id)
                if d is None:
                    d = self._dicts[a.alert_id] = a.to_dict()
                out.append(d)
                if limit is not None and len(out) >= int(limit):
                    break
            return out

    def open_alert_for(self, exchange_id: str) -> Optional[Dict[str, Any]]:
        """The compact open alert of one outcome (§18.4 annotation) from the published summary, or None; lock-free."""
        return self._published_open.get(str(exchange_id))

    def close(self) -> None:
        """Flush the alerts file; the providers are closed by their owner (the FairValueService or the builder)."""
        self._closed = True  # alerts.jsonl is opened, appended and closed per step: nothing is left to flush
        # let a step that is running finish its writes before the owner closes the store (bounded: one poll deadline
        # plus one request); a later step() returns at once
        if self._step_lock.acquire(timeout=float(self.params.poll_deadline_s) + 10.0):
            self._step_lock.release()


def _pts_word(x: float) -> str:
    """"+6.0 pts", "+1.0 pt" (the disagreement sentence)."""
    text = fmt_pts(x)
    return text[:-1] if text.endswith(" pts") and abs(round(round(float(x), 6) * 100.0, 1)) == 1.0 else text


def _failed_result(name: str, message: str) -> Any:
    from .fairvalue import ProviderResult  # lazy: fairvalue imports this module

    return ProviderResult(venue=name, status="error", errors=[message])
