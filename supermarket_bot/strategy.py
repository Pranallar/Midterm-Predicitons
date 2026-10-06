"""Read-only trade ideas: value vs outside fair value, pair/set baskets, liquidity holes, fades of
participant surges, carry on high-90s favourites, engine-reported arbitrage.

See docs/DESIGN.md (Strategy section) and docs/PAPER_TRADING.md §5 (package B). Nothing here places
orders: :func:`generate_signals` returns UNSIZED ideas (what the paper engine and the event study
consume); :func:`build_report` sizes them with a sizing policy (``sizing.py``) for the Strategy view.

Side conventions: prices are YES-denominated; an idea always names the contract to *buy*.
Buying NO after an up-surge costs ``1 - yes_bid`` and a NO share pays 1 if NO wins.
``entry_price``, ``target_price``, ``stop_price`` and ``limit_price`` are prices of that contract.
Every depth walk uses ``depth.py`` (one implementation shared with sizing and the paper engine).
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from . import depth
from . import sizing as sizing_mod
from .analytics import (
    SURGE_WINDOWS,
    Z_THRESHOLD,
    _num,
    _Series,
    _SurgeScanner,
    parse_iso_ts,
    update_surge_status,
    window_tolerance,
)
from .models import (
    KIND_ARBITRAGE,
    KIND_BASKET,
    KIND_CARRY,
    KIND_FADE,
    KIND_HOLE,
    KIND_VALUE,
    KIND_WATCH,
    POLICY_CHASER,
    POLICY_CONSERVATIVE,
    REGIME_RESOLVED,
    REGIME_UNKNOWN,
    REGIME_VWAP,
    REGIMES,
    SURGE_OPEN,
    SURGE_REVERTED,
    VERDICT_PARTICIPANTS,
    VERDICT_UNCLEAR,
    BacktestResult,
    BetShape,
    ExchangeInfo,
    ExitPlan,
    ExposureSummary,
    FairValue,
    HighBand,
    Opportunity,
    PricePoint,
    RaceRef,
    SizeDecision,
    SizingContext,
    StrategyInputs,
    StrategyParams,
    StrategyReport,
    Surge,
    iso_ts,
)

log = logging.getLogger("supermarket_bot")

DEFAULT_INITIAL_BALANCE = 100_000.0
DEFAULT_CUP_END_ISO = "2026-11-04T17:00:00Z"  # noon ET, Nov 4 2026 (EST)
CUP_END_ENV = "SUPERMARKET_CUP_END"

KELLY_MULT = 0.25
MAX_POSITION_PCT = 0.08
FADE_HORIZON_H = 6.0
CARRY_MAX_ENTRY = 0.995  # no room left above this
MIN_BACKTEST_SURGES = 10  # use a measured backtest rate only with this many surges
THIN_BOOK_SHARES = 500.0
TOP_N = 50
CONSTRAINT_CONFIDENCE = 0.7  # engine-reported relationship violation
BOOK_ARB_CONFIDENCE = 0.6  # "potential, not guaranteed" multi-outcome book flag
BOOK_MAX_AGE_S = 900.0  # an order-book read older than this does not size an idea
BOOK_TICK = 0.005  # the price grid: a book whose best level sits more than a tick off the live quote is outdated
REVERSION_LINK_S = 6 * 3600.0  # an opposite move this soon after a surge's window may be its reversion
ASSUMED_SPREAD = 0.02  # when no live bid/ask is known (marks, candles): the spread charged on the way in and out

# States whose count usually runs past the Cup's closeout window (or uses ranked-choice runoffs).
SLOW_COUNT_STATES = frozenset({"AK", "AZ", "NV", "CA", "WA", "OR", "UT", "ME"})
RCV_STATES = frozenset({"AK", "ME"})
RCV_CALLED_PROB = 0.80  # a safe seat in a ranked-choice state can still go to a runoff
# Chamber control (House / Senate majority, state "US") is decided by the LAST seats counted, which sit in the
# slow-count states above (House control was called on Nov 16 in 2022 and Nov 13 in 2024; the Cup closes at noon
# ET the day after the election): it counts as a slow count everywhere (called_prob, riskless sets, regime exits).
CHAMBER_OFFICES = frozenset({"HOUSE_CONTROL", "SENATE_CONTROL"})
CHAMBER_STATE = "US"
CHAMBER_SAFE_CALLED_PROB = 0.80  # even a 0.95+ chamber favourite can hinge on late-counted seats
# Option labels that make a multi-outcome market exhaustive (a catch-all outcome exists).
CATCH_ALL_RE = re.compile(r"\b(any other|other|someone else|field|none of the above)\b", re.IGNORECASE)

VALUE_CONFIDENCE = {"high": 0.8, "medium": 0.6, "low": 0.4}
FLAT_FV_CONFIDENCE = 0.6  # manual and demo fair values
BASKET_CONF_RACE = 0.9
BASKET_CONF_EXHAUSTIVE = 0.95
HOLE_CONFIDENCE = 0.5
SETTLEMENT_RISK = ("A settlement date is not a payout date: autosettlement can retry, REFUND and tournament rulings "
                   "exist")

MODE_PROTECT = "protect"
MODE_BALANCED = "balanced"
MODE_AGGRESSIVE = "aggressive"
MODE_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    MODE_AGGRESSIVE: {"fade": 1.3, "arbitrage": 1.3, "carry": 0.7},
    MODE_PROTECT: {"carry": 1.3, "fade": 0.6},
    MODE_BALANCED: {},
}
_KIND_ORDER = {"arbitrage": 0, "basket": 1, "value": 2, "fade": 3, "carry": 4, "hole": 5, "watch": 6}
LEGACY_KINDS = (KIND_FADE, KIND_CARRY, KIND_ARBITRAGE)
NEW_KINDS = (KIND_VALUE, KIND_BASKET, KIND_HOLE)

_EPS = 1e-9
_ND = NormalDist()
TICK = depth.TICK

Levels = List[Tuple[float, float]]  # (cost of the contract, shares), cheapest first


# --------------------------------------------------------------------------- formatting


def _px(value: Optional[float]) -> str:
    """A price with 2-4 decimals: 0.31, 0.605, 0.6125."""
    if value is None:
        return "—"
    text = f"{value:.4f}".rstrip("0")
    head, _, frac = text.partition(".")
    if head == "-0" and not frac.strip("0"):
        head = "0"
    return f"{head}.{frac.ljust(2, '0')}"


def _signed(value: float) -> str:
    text = _px(abs(value))
    return ("-" if value < -_EPS else "+") + text


def _when(ts: Optional[float]) -> str:
    text = iso_ts(ts)
    return text[:16].replace("T", " ") + " UTC" if text else "unknown"


def _kelly_label(mult: float) -> str:
    names = {0.25: "quarter-Kelly", 0.5: "half-Kelly", 1.0: "full Kelly"}
    for value, name in names.items():
        if abs(mult - value) < 1e-9:
            return name
    return f"{mult:g}x Kelly"


def _clamp01(value: Optional[float], default: float = 0.0) -> float:
    num = _num(value)
    if num is None:
        return default
    return max(0.0, min(1.0, num))


def _title(info: Optional[ExchangeInfo], market_id: str) -> Tuple[str, Optional[str]]:
    if info is not None:
        return (info.market_title or f"Market {market_id}"), info.option
    return f"Market {market_id}", None


def _ago(now: float, at: Optional[float]) -> str:
    if at is None:
        return "at an unknown time"
    secs = max(0.0, now - at)
    return f"{secs / 60:.0f} min ago" if secs >= 90 else f"{secs:.0f} s ago"


def _source_label(source: Optional[str]) -> str:
    return {"polymarket": "Polymarket", "kalshi": "Kalshi", "blend": "Polymarket and Kalshi",
            "manual": "your fair-value file", "demo": "the demo's outside feed",
            "history": "imported outside price history"}.get(str(source or ""), str(source or "an outside source"))


# --------------------------------------------------------------------------- basics


def cup_end_ts(tournament: Optional[Mapping[str, Any]] = None) -> float:
    """Tournament ``endDate`` if present, else SUPERMARKET_CUP_END, else DEFAULT_CUP_END_ISO."""
    if tournament:
        ts = parse_iso_ts(tournament.get("endDate"))
        if ts is not None:
            return ts
    raw = os.environ.get(CUP_END_ENV, "").strip()
    if raw:
        ts = parse_iso_ts(raw)
        if ts is not None:
            return ts
        log.warning("ignoring unparseable %s=%r; using %s", CUP_END_ENV, raw, DEFAULT_CUP_END_ISO)
    ts = parse_iso_ts(DEFAULT_CUP_END_ISO)
    assert ts is not None
    return ts


def kelly_fraction(p_win: float, price: float) -> float:
    """Kelly fraction for buying a contract at ``price`` that pays 1 with probability ``p_win``.

    ``f = (p - c) / (1 - c)``, 0 when ``p <= c`` or the price is outside ``[0, 1)``.
    """
    p, c = _num(p_win), _num(price)
    if p is None or c is None or c < 0 or c >= 1 - _EPS:
        return 0.0
    p = max(0.0, min(1.0, p))
    if p <= c + _EPS:
        return 0.0
    return min(1.0, (p - c) / (1 - c))


def size_position(p_win: float, price: float, balance: float, kelly_mult: float = 0.25,
                  max_position_pct: float = 0.08, depth_limit: Optional[float] = None) -> int:
    """Shares to buy: ``floor(stake / price)`` with ``stake = min(kelly_mult * f * balance,
    max_position_pct * balance)``, then capped by ``depth_limit`` shares when known."""
    c, b = _num(price), _num(balance)
    f = kelly_fraction(p_win, price)
    if f <= 0 or c is None or c <= 0 or b is None or b <= 0:
        return 0
    stake = min(kelly_mult * f * b, max_position_pct * b)
    shares = math.floor(stake / c + 1e-9)
    lim = _num(depth_limit)
    if lim is not None:
        shares = min(shares, math.floor(max(0.0, lim) + 1e-9))
    return max(0, int(shares))


def _bracket_shares(p: float, gain: float, loss: float, entry: float, balance: Optional[float],
                    kelly_mult: float, max_position_pct: float, depth_cap: Optional[float]) -> int:
    """Kelly sizing for a target/stop trade.

    A bracket that gains ``gain`` or loses ``loss`` per share is a binary bet at the break-even
    price ``c = loss / (gain + loss)`` with ``loss`` at risk per share, so the Kelly fraction
    ``(p - c) / (1 - c)`` is the share of the balance *at risk*. For a contract held to
    settlement (``gain = 1 - entry``, ``loss = entry``) this is exactly :func:`size_position`.
    """
    b = _num(balance)
    if b is None or b <= 0 or loss <= _EPS or gain <= _EPS or entry <= 0:
        return 0
    f = kelly_fraction(p, loss / (gain + loss))
    if f <= 0:
        return 0
    limit = min(kelly_mult * f * b / loss, max_position_pct * b / entry)
    if depth_cap is not None:
        limit = min(limit, max(0.0, depth_cap))
    return max(0, int(math.floor(limit + 1e-9)))


def risk_mode(balance: Optional[float], initial: Optional[float], leader_value: Optional[float],
              my_rank: Optional[int], days_left: Optional[float], account_value: Optional[float] = None) -> str:
    """``protect`` (top 3, a week or less left), ``aggressive`` (10%+ behind the leader, or
    10 days or less left outside the top 10), else ``balanced``.

    "Behind" compares like with like: the leader's value (initial balance + P&L, positions
    included) with your ``account_value`` (cash + positions). Only when that is unknown does
    it fall back to ``balance`` (cash), which understates a player holding positions.
    """
    value = _num(account_value)
    bal = value if value is not None else _num(balance)
    leader, days = _num(leader_value), _num(days_left)
    rank = my_rank if isinstance(my_rank, int) and not isinstance(my_rank, bool) else None
    if rank is not None and rank <= 3 and days is not None and days <= 7:
        return MODE_PROTECT
    behind = bool(leader) and bal is not None and bal < 0.9 * leader  # type: ignore[operator]
    late_and_outside = days is not None and days <= 10 and (rank is None or rank > 10)
    if behind or late_and_outside:
        return MODE_AGGRESSIVE
    return MODE_BALANCED


def settles_before(settlement_date: Optional[str], cup_end: Optional[float], margin_s: float = 3600.0) -> Optional[bool]:
    """True only when the market settles at least ``margin_s`` before the Cup end (a settlementDate
    equal to the Cup end is the end-of-Cup closeout, not a resolution: False); None when unknown."""
    settle = parse_iso_ts(settlement_date) if settlement_date else None
    end = _num(cup_end)
    if settle is None or end is None:
        return None
    return settle <= end - max(0.0, float(margin_s or 0.0)) + 1e-6


def _settles_before(info_date: Optional[str], cup_end: Optional[float],
                    margin_s: float = 3600.0) -> Tuple[Optional[bool], Optional[float]]:
    """(:func:`settles_before`, the settlement time)."""
    settle = parse_iso_ts(info_date) if info_date else None
    return settles_before(info_date, cup_end, margin_s), settle


def is_chamber(race: Optional[RaceRef]) -> bool:
    """A chamber-control race (House / Senate majority): office HOUSE_CONTROL / SENATE_CONTROL or state "US"."""
    return race is not None and (race.office in CHAMBER_OFFICES or race.state == CHAMBER_STATE)


def _slow_state(state: Optional[str]) -> bool:
    """A slow count: a state in SLOW_COUNT_STATES, or chamber control (state "US"), decided by the last seats."""
    return bool(state) and (state in SLOW_COUNT_STATES or state == CHAMBER_STATE)


def _slow_race(race: Optional[RaceRef]) -> bool:
    return race is not None and (is_chamber(race) or _slow_state(race.state))


def called_prob(race: Optional[RaceRef], favourite_price: Optional[float], params: StrategyParams) -> float:
    """P(the race is called, so prices go to ~0/1, before the Cup's closeout window):
    chamber control (House / Senate majority) -> CHAMBER_SAFE_CALLED_PROB (0.80) for a favourite >= 0.95, else
    called_prob_slow (it hinges on the slow-count seats); favourite >= 0.95 -> called_prob_safe (0.80 in
    RCV_STATES); state in SLOW_COUNT_STATES -> called_prob_slow; known race elsewhere -> called_prob_fast;
    unknown race -> the mean of fast and slow."""
    fav = _num(favourite_price)
    state = race.state if race is not None else None
    safe = fav is not None and fav >= 0.95 - 1e-9
    if is_chamber(race):
        return min(CHAMBER_SAFE_CALLED_PROB, params.called_prob_safe) if safe else params.called_prob_slow
    if safe:
        return RCV_CALLED_PROB if state in RCV_STATES else params.called_prob_safe
    if race is None:
        return (params.called_prob_fast + params.called_prob_slow) / 2.0
    if state in SLOW_COUNT_STATES:
        return params.called_prob_slow
    return params.called_prob_fast


def _regime(value: Optional[str]) -> str:
    return value if value in REGIMES else REGIME_UNKNOWN


def regime_ev(q: float, entry: float, cup_mid: float, regime: str, called: float, params: StrategyParams, *,
              settles_before_end: Optional[bool] = None) -> Tuple[float, str, Dict[str, float]]:
    """Expected value per share of holding a contract bought at ``entry`` whose fair value is ``q``, under
    the settlement regime (§5.2, revision 2). ``cup_mid`` is the contract's Cup mid at the decision.

    * resolved_outcomes, or the market settles before the Cup end: ``ev = q - entry``;
    * vwap_closeout: the race is called before the closeout with probability ``called`` (payoff 1/0,
      expected ``q_called``) or is still uncalled (the closeout VWAP is ``q_uncalled - (1 - unconverged_share)
      x g`` with ``g = q - cup_mid``, the Cup mispricing that persists). With ``q_uncalled = 0.5 + (q - 0.5)
      x uncalled_shrink`` and ``q_called = clip((q - (1 - called) x q_uncalled) / called, 0, 1)`` (so the two
      branches average to q), ``ev = called x (q_called - entry) + (1 - called) x (q_uncalled - (1 -
      unconverged_share) x g - entry)``, which simplifies to ``q - entry - (1 - called) x (1 -
      unconverged_share) x g`` when q_called is not clipped. A favourite still uncalled closes near 0.5-0.6,
      so its uncalled branch is a LOSS; this is a documented heuristic, and the rationale says so;
    * unknown: the smaller of the two.

    Returns (ev, one sentence saying which rule applied, detail ``{"q_called", "q_uncalled", "called_payoff",
    "uncalled_payoff"}``; empty detail for the resolved rule)."""
    q, entry, mid = float(q), float(entry), float(cup_mid)
    resolved = q - entry
    regime = _regime(regime)
    if regime == REGIME_RESOLVED or settles_before_end is True:
        if settles_before_end is True:
            text = (f"It settles before the Cup ends, so it pays 1/0: EV = fair value {q:.3f} - cost {entry:.3f} = "
                    f"{resolved:+.4f} per share")
        else:
            text = (f"Valued at a real 1/0 resolution (the resolved-outcomes rule): EV = fair value {q:.3f} - cost "
                    f"{entry:.3f} = {resolved:+.4f} per share")
        return resolved, text, {}
    c = max(1e-9, min(1.0, float(called)))
    q_u = 0.5 + (q - 0.5) * params.uncalled_shrink
    q_c = min(1.0, max(0.0, (q - (1.0 - c) * q_u) / c))
    g = q - mid
    close_u = q_u - (1.0 - params.unconverged_share) * g
    called_payoff = q_c - entry
    uncalled_payoff = close_u - entry
    vwap = c * called_payoff + (1.0 - c) * uncalled_payoff
    detail = {"q_called": q_c, "q_uncalled": q_u, "called_payoff": called_payoff, "uncalled_payoff": uncalled_payoff}
    branches = (f"called before the closeout ({c:.0%}) it pays {q_c:.3f} ({called_payoff:+.4f}/share); still uncalled "
                f"it closes near {close_u:.3f} ({uncalled_payoff:+.4f}/share)")
    if regime == REGIME_VWAP:
        return vwap, (f"Under a 5-hour VWAP closeout (a heuristic for how an uncalled race closes): {branches}; EV "
                      f"{vwap:+.4f} per share"), detail
    ev = min(resolved, vwap)
    text = (f"The Cup's end rule is unknown, so the smaller of two values is used: {resolved:+.4f} per share at a real "
            f"1/0 resolution, {vwap:+.4f} under a 5-hour VWAP closeout (a heuristic for how an uncalled race closes: "
            f"{branches})")
    return ev, text, detail


def factor_delta(race: Optional[RaceRef], side: str, p_yes: Optional[float], params: StrategyParams) -> float:
    """SUSQies gained per share for a 1-point national swing toward Democrats (§5.6.1): with ``s =
    params.race_margin_sd_pts``, a YES share of the D leg gains ``phi(Phi^-1(p)) / s`` (``p`` = the leg's YES
    probability: usable fair value, else the Cup mid, clipped to [0.01, 0.99]); YES-R the negative of its own
    value; NO flips the sign. Parties other than D/R, no race, or ``p_yes`` None: 0.0. Chamber-control legs
    use the same formula (a heuristic)."""
    if race is None or race.party not in ("D", "R"):
        return 0.0
    p = _num(p_yes)
    sd = _num(params.race_margin_sd_pts)
    if p is None or sd is None or sd <= 0:
        return 0.0
    p = min(0.99, max(0.01, p))
    delta = _ND.pdf(_ND.inv_cdf(p)) / sd
    sign = 1.0 if race.party == "D" else -1.0
    if str(side or "").lower() == "no":
        sign = -sign
    return round(sign * delta, 6)


def growth_score(bet: Optional[BetShape], edge: Optional[float], confidence: float, entry: Optional[float]) -> float:
    """The ranking score of every trade idea (§5.1, revision 2): expected profit in percent of equity at the
    conservative capped size, times confidence: ``100 x confidence x edge x u`` with ``u`` = units per unit of
    equity = ``min(0.25 x (p / loss - (1 - p) / gain), 0.08 / cost)`` for binary / bracket / bounded bets,
    ``0.08 / cost`` for riskless sets and ``cap_pct / cost`` for fixed (hole) bets; 0 when anything is
    missing or non-positive. Ranks a 0.50 toss-up with a 5-cent edge above a 0.10 longshot with a 2-cent edge.

    A bounded set counts its expected profit, ``p x gain - tail x loss``, when that is below the floor edge
    (the uncalled and refund branches are part of what it is expected to make)."""
    e, conf = _num(edge), _num(confidence)
    if bet is None or e is None or e <= _EPS or conf is None or conf <= 0:
        return 0.0
    cost = _num(bet.cost)
    if cost is None or cost <= 0:
        cost = _num(entry)
    if cost is None or cost <= 0:
        return 0.0
    if bet.kind in ("binary", "bracket", "bounded"):
        if bet.gain <= _EPS or bet.loss <= _EPS:
            return 0.0
        kelly = sizing_mod.KELLY_MULT * (bet.p / bet.loss - (1.0 - bet.p) / bet.gain)
        cap = sizing_mod.RISKLESS_CAP_PCT if bet.kind == "bounded" else sizing_mod.IDEA_CAP_PCT
        u = min(kelly, cap / cost)
        if bet.kind == "bounded":
            e = min(e, bet.p * bet.gain - (1.0 - bet.p) * bet.loss)
    elif bet.kind == "riskless":
        u = sizing_mod.RISKLESS_CAP_PCT / cost
    elif bet.kind == "fixed":
        u = (_num(bet.cap_pct) or 0.0) / cost
    else:
        return 0.0
    if u <= 0 or e <= 0:
        return 0.0
    return round(100.0 * conf * e * u, 6)


def _eid_key(eid: Any) -> Tuple[int, int, str]:
    text = str(eid)
    return (0, int(text), text) if text.isdigit() else (1, 0, text)


def idea_id_for(kind: str, *, exchange_id: Optional[str] = None, legs: Sequence[Mapping[str, Any]] = (),
                side: Optional[str] = None, surge_id: Optional[int] = None, extra: str = "") -> str:
    """The stable idea key: ``"<kind>:x<eid>:<side>[:s<surge_id>][:<extra>]"`` for single-outcome
    ideas, ``"<kind>:set:<eid1>+<eid2>...:<sides>"`` (legs sorted by exchange id) for sets."""
    if legs:
        pairs = sorted(((str(leg.get("exchange_id")), str(leg.get("side") or "").lower()) for leg in legs),
                       key=lambda kv: (_eid_key(kv[0]), kv[1]))
        text = f"{kind}:set:{'+'.join(e for e, _ in pairs)}:{''.join('y' if s == 'yes' else 'n' for _, s in pairs)}"
    else:
        text = f"{kind}:x{exchange_id}:{str(side or 'yes').lower()}"
        if surge_id is not None:
            text += f":s{surge_id}"
    if extra:
        text += f":{extra}"
    return text


def value_limit(q: float, entry: float, cup_mid: float, target_exit: float, regime: str, called: float,
                required: float, params: StrategyParams, *, settles_before_end: Optional[bool] = None) -> Optional[float]:
    """The worst contract price a value order accepts (§5.1): the highest tick ``L`` in
    ``[entry, floor_tick(entry + 0.5 x ev(entry))]`` whose MARGINAL ev (regime_ev at L) and convergence gain
    (``target_exit - L``) are both ``>= required``; None when even ``L = entry`` fails."""
    ev0, _, _ = regime_ev(q, entry, cup_mid, regime, called, params, settles_before_end=settles_before_end)
    if ev0 <= 0:
        return None
    top = min(depth.floor_tick(entry + 0.5 * ev0), depth.floor_tick(q))
    lim = top
    while lim >= entry - 1e-9:
        ev_l, _, _ = regime_ev(q, lim, cup_mid, regime, called, params, settles_before_end=settles_before_end)
        if ev_l >= required - 1e-9 and target_exit - lim >= required - 1e-9:
            return round(lim, 3)
        lim = round(lim - TICK, 3)
    return None


# --------------------------------------------------------------------------- order-book depth


def _book_levels(contract: str, point: Optional[PricePoint], now: float,
                 live_cost: Optional[float] = None,
                 max_age_s: float = BOOK_MAX_AGE_S) -> Tuple[Optional[Levels], Optional[float]]:
    """The levels a buy of ``contract`` fills against, from the point's stored order book.

    YES buys take the YES asks (cost = price); NO buys take the YES bids (cost = 1 - price).
    Returns ``(levels, book time)``, levels None when no usable book is known: none stored,
    older than ``max_age_s``, or out of line with the live quote (levels cheaper than the
    live cost were taken since the read and are dropped; if what is left starts more than a
    tick above the live cost, the read is outdated). Built on ``depth.contract_levels``.
    """
    book = getattr(point, "book", None) if point is not None else None
    if not isinstance(book, Mapping):
        return None, None
    at = _num(book.get("at"))
    if at is None or now - at > max_age_s:
        return None, at
    bids, asks = depth.book_sides(book)
    levels: Levels = depth.contract_levels(bids, asks, "yes" if contract == "yes" else "no", "buy")
    live = _num(live_cost)
    if live is not None:
        levels = [lv for lv in levels if lv[0] >= live - _EPS]
        if levels and levels[0][0] > live + BOOK_TICK + _EPS:
            return None, at
    return (levels or None), at


def _depth_limit(value: float, entry: float) -> float:
    """The highest cost a further share (or set) may fill at: it must keep at least half of the
    edge quoted at the entry (``value - entry``), so the average fill stays near the quote."""
    return entry + 0.5 * max(0.0, value - entry)


def _walk(levels: Levels, limit: float) -> Tuple[float, Levels]:
    """Shares on offer at a cost up to ``limit`` (``depth.available``), and those levels."""
    taken: Levels = []
    for cost, qty in levels:
        if cost > limit + _EPS:
            break
        taken.append((cost, qty))
    return depth.available(levels, limit, buy=True), taken


def _avg_cost(levels: Levels, qty: float) -> Optional[float]:
    """Average cost of filling ``qty`` shares from ``levels`` (None for nothing to fill)."""
    total = sum(q for _, q in levels)
    take = min(qty, total)
    if take <= _EPS:
        return None
    return depth.avg_cost(levels, take)


def _walk_set(legs: Sequence[Levels], limit: float) -> float:
    """Full sets (one share of every leg) on offer while the next set costs at most ``limit``
    (``depth.walk_set``): each step takes the cheapest remaining level of every leg."""
    return depth.walk_set(legs, limit)


def _cut(levels: Optional[Levels], limit: float) -> Levels:
    return [(round(p, 6), q) for p, q in (levels or []) if p <= limit + _EPS]


def _two_sided(point: Optional[PricePoint]) -> Optional[Tuple[float, float]]:
    """(YES bid, YES ask) when the quote is two-sided and inside (0, 1), else None."""
    if point is None:
        return None
    bid, ask = _num(point.bid), _num(point.ask)
    if bid is None or ask is None or not 0.0 < bid < 1.0 or not 0.0 < ask < 1.0 or bid > ask + _EPS:
        return None
    return bid, ask


def _usable_fv(fv: Optional[FairValue], now: float, params: StrategyParams) -> Optional[FairValue]:
    """The fair value when it may be traded on now: usable, valued, not suspect, and fresh (90 s for
    outside values, 72 h for manual entries, 600 s for imported history in replays)."""
    if fv is None or not fv.usable or fv.suspect:
        return None
    value = _num(fv.value)
    if value is None or not 0.0 <= value <= 1.0:
        return None
    as_of = _num(fv.as_of)
    if as_of is None:
        return None
    if fv.source == "manual":
        max_age = params.manual_fv_max_age_s
    elif fv.history or fv.source == "history":
        max_age = params.history_fv_max_age_s
    else:
        max_age = params.fv_max_age_s
    if now - as_of > max_age + 1e-9:
        return None
    return fv


def _exit_before(regime: str, states: Sequence[Optional[str]], settles_before_end: Optional[bool],
                 cup_end: Optional[float], params: StrategyParams) -> Optional[float]:
    """The regime exit rule (all kinds, §5.2): flat ``closeout_buffer_s`` before the Cup end under a VWAP
    closeout, or under an unknown rule in a slow-count state (chamber control, state "US", counts as one);
    markets settling before the end never."""
    if settles_before_end is True or cup_end is None:
        return None
    regime = _regime(regime)
    if regime == REGIME_VWAP or (regime == REGIME_UNKNOWN and any(_slow_state(s) for s in states if s)):
        return round(float(cup_end) - params.closeout_buffer_s, 3)
    return None


def _mid(point: Optional[PricePoint]) -> Optional[float]:
    quote = _two_sided(point)
    if quote is not None:
        return (quote[0] + quote[1]) / 2.0
    return _num(point.price) if point is not None else None


def _horizon(settle_before: Optional[bool], settle_ts: Optional[float], now: float, cup_end: Optional[float]) -> Optional[float]:
    if settle_before is True and settle_ts is not None:
        return round(max(0.0, (settle_ts - now) / 3600.0), 2)
    if cup_end is not None:
        return round(max(0.0, (cup_end - now) / 3600.0), 2)
    return None


def _noop(_size: Optional[Dict[str, Any]]) -> None:
    return None


@dataclass
class _Built:
    """An idea plus the function that writes its rationale and risks for a given size (None: unsized)."""

    opp: Opportunity
    render: Callable[[Optional[Dict[str, Any]]], None] = _noop
    meta: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- fade


def _surge_levels(surge: Surge) -> Optional[Tuple[bool, float, float, float]]:
    """(up, signed YES move start->peak, YES target at half reversion, YES stop at +50% extension)."""
    up = surge.direction != "down"
    move = surge.peak_price - surge.start_price
    if abs(move) < _EPS or (move > 0) != up:
        return None
    target = surge.start_price + 0.5 * move
    stop = max(0.0, min(1.0, surge.peak_price + 0.5 * move))
    return up, move, target, stop


def _fade_build(surge: Surge, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                backtest: Optional[BacktestResult], cup_end: Optional[float], *, fv: Optional[FairValue],
                race: Optional[RaceRef], regime: str, params: StrategyParams, require_quote: bool) -> Optional[_Built]:
    att = surge.attribution
    if surge.status != SURGE_OPEN or att is None or att.verdict != VERDICT_PARTICIPANTS:
        return None
    lv = _surge_levels(surge)
    if lv is None:
        return None
    up, move, yes_target, yes_stop = lv
    quote = _two_sided(current)
    if require_quote and quote is None:
        return None
    bid = _num(current.bid) if current is not None else None
    ask = _num(current.ask) if current is not None else None
    mark = next((v for v in (_num(current.price) if current is not None else None,
                             _num(surge.current_price), _num(surge.end_price)) if v is not None), None)
    no_book = False
    if up:
        side = "no"
        if bid is not None:
            entry, how = 1.0 - bid, f"1 - YES bid {_px(bid)}"
        elif mark is not None:
            entry, how, no_book = 1.0 - mark, f"1 - YES mark {_px(mark)}", True
        else:
            return None
        target_c, stop_c = 1.0 - yes_target, 1.0 - yes_stop
    else:
        side = "yes"
        if ask is not None:
            entry, how = ask, "the YES ask"
        elif mark is not None:
            entry, how, no_book = mark, "the YES mark", True
        else:
            return None
        target_c, stop_c = yes_target, yes_stop
    spread_known = bid is not None and ask is not None and ask >= bid
    half = (ask - bid) / 2.0 if spread_known else ASSUMED_SPREAD / 2.0  # type: ignore[operator]
    half_target = target_c
    used = _usable_fv(fv, now, params)
    fv_c: Optional[float] = None
    fv_capped = False
    if used is not None:
        v = float(used.value)  # type: ignore[arg-type]
        if (up and v > surge.peak_price + _EPS) or (not up and v < surge.peak_price - _EPS):
            return None  # the move went toward fair value: nothing to fade
        fv_c = 1.0 - v if up else v
        if fv_c < target_c - _EPS:
            target_c, fv_capped = fv_c, True  # never aim beyond fair value
    entry, target_c, stop_c = round(entry, 6), round(target_c, 6), round(stop_c, 6)
    if not 0.0 < entry < 1.0:
        return None
    gain_mark, loss_mark = target_c - entry, entry - stop_c
    if gain_mark <= _EPS or loss_mark <= _EPS:  # already past the target, or already through the stop
        return None
    target_bid = round(target_c - half, 6)
    stop_bid = round(max(0.0, stop_c - half), 6)
    gain, loss = round(target_bid - entry, 6), round(entry - stop_bid, 6)
    if gain < params.min_fade_gain - _EPS or loss <= _EPS:
        return None
    p_att = _clamp01(att.reversion_odds)
    p, capped = p_att, False
    rate = backtest.participant_reversion_rate if backtest is not None else None
    n_part = backtest.n_participant if backtest is not None else 0
    if rate is not None and n_part >= MIN_BACKTEST_SURGES and _clamp01(rate) < p:
        p, capped = _clamp01(rate), True
    edge = p * gain - (1.0 - p) * loss
    if edge <= _EPS:
        return None
    er = edge / entry
    value = entry + edge  # p x target_bid + (1 - p) x stop_bid: a fill above it has no edge left
    limit = min(max(entry, depth.floor_tick(entry + 0.5 * edge)), value)
    limit = round(limit, 6)
    levels_all, book_at = _book_levels(side, current, now, None if no_book else entry, params.book_max_age_s)
    side_depth = _num(att.bid_depth if up else att.ask_depth)  # within 5c of the mid, when analysed
    att_depth = side_depth if side_depth is not None else _num(att.book_depth)
    cut = _cut(levels_all, limit) if levels_all is not None else []
    if levels_all is not None:
        max_units: Optional[float] = float(sum(q for _, q in cut))
    else:
        max_units = float(math.floor(max(0.0, att_depth) + 1e-9)) if att_depth is not None else None
    confidence = _clamp01(att.confidence)
    title, option = _title(info, surge.market_id)
    before, _settle = _settles_before(info.settlement_date if info is not None else None, cup_end, params.settle_margin_s)
    bet = BetShape(kind="bracket", p=round(p, 6), gain=gain, loss=loss, cost=entry)
    p_yes = float(used.value) if used is not None else (_mid(current) if current is not None else mark)  # type: ignore[arg-type]
    horizon_h = params.fade_horizon_h
    exit_plan = ExitPlan(kind="bracket", target_bid=target_bid, stop_bid=stop_bid,
                         time_stop_ts=round(now + horizon_h * 3600.0, 3),
                         exit_before_ts=_exit_before(regime, [race.state if race else None], before, cup_end, params),
                         note=(f"Sell when the {side.upper()} bid reaches {target_bid:.3f} (target) or falls to "
                               f"{stop_bid:.3f} (stop), or after {horizon_h:g} h"))
    opp = Opportunity(
        kind=KIND_FADE, exchange_id=surge.exchange_id, market_id=surge.market_id, title=title, option=option, side=side,
        entry_price=round(entry, 4), target_price=round(target_c, 4), stop_price=round(stop_c, 4), prob_win=round(p, 4),
        edge=round(edge, 6), expected_return=round(er, 6), horizon_hours=horizon_h, suggested_shares=0,
        suggested_cost=0.0, score=growth_score(bet, round(edge, 6), confidence, entry), confidence=round(confidence, 4),
        settles_before_cup_end=before, surge_id=surge.id, depth_checked=levels_all is not None, fill_price=None,
        idea_id=idea_id_for(KIND_FADE, exchange_id=surge.exchange_id, side=side, surge_id=surge.id),
        race_key=race.race_key if race is not None else None, bet=bet,
        bet_limit=sizing_mod.bet_at_cost(bet, limit), exit_plan=exit_plan, order_type="taker", limit_price=limit,
        levels=cut, max_units=max_units, fair_value=round(fv_c, 4) if fv_c is not None else None,
        fair_source=used.source if used is not None else None, settlement_regime=_regime(regime),
        fv_uncertainty=sizing_mod.DEFAULT_SIGMA["fade"], factor_delta=factor_delta(race, side, p_yes, params),
        priced_from=str(getattr(current, "source", "tick") or "tick") if current is not None else "tick",
    )
    name = side.upper()
    verb, noun = ("jumped", "jump") if up else ("dropped", "drop")
    rising = "rising" if up else "falling"
    spread_text = f"the {_px(round(2 * half, 4))} spread" if spread_known else f"an assumed {_px(ASSUMED_SPREAD)} spread"

    def render(size: Optional[Dict[str, Any]]) -> None:
        rationale = [
            f"YES {verb} {_signed(move)} in {surge.window} ({_px(surge.start_price)} -> {_px(surge.peak_price)}) "
            f"with no matching news: the evidence points to a few Cup traders ({confidence:.0%} confidence)",
        ]
        if up:
            if fv_capped:
                rationale.append(f"Buy NO at {_px(entry)} ({how}): YES may give back half its {_px(abs(move))} {noun} to "
                                 f"{_px(yes_target)}, but the outside fair value caps the target at NO {_px(target_c)} "
                                 f"({_signed(gain_mark)}/share): a fade never aims beyond fair value")
            else:
                rationale.append(f"Buy NO at {_px(entry)} ({how}): if YES gives back half its {_px(abs(move))} {noun} to "
                                 f"{_px(yes_target)}, NO is worth {_px(target_c)} ({_signed(gain_mark)}/share)")
            rationale.append(f"Stop if YES extends another half-move to {_px(yes_stop)}: NO falls to "
                             f"{_px(stop_c)} ({_signed(-loss_mark)}/share)")
        else:
            if fv_capped:
                rationale.append(f"Buy YES at {_px(entry)} ({how}): YES may win back half its {_px(abs(move))} {noun} to "
                                 f"{_px(yes_target)}, but the outside fair value caps the target at {_px(target_c)} "
                                 f"({_signed(gain_mark)}/share): a fade never aims beyond fair value")
            else:
                rationale.append(f"Buy YES at {_px(entry)} ({how}): if YES wins back half its {_px(abs(move))} {noun} to "
                                 f"{_px(half_target)}, it is worth {_px(target_c)} ({_signed(gain_mark)}/share)")
            rationale.append(f"Stop if YES falls another half-move to {_px(yes_stop)} ({_signed(-loss_mark)}/share)")
        odds = f"Win chance {p:.0%}: attribution reversion odds {p_att:.0%}"
        if capped:
            odds += (f", capped at the measured {_clamp01(rate):.0%} net-of-spread reversion rate of {n_part} "
                     "participant-driven surges")
        rationale.append(odds)
        if size is None:
            rationale.append(f"Edge {_signed(edge)}/share ({er:.1%} of cost) over {horizon_h:g} h after the exit spread; "
                             "the size comes from the sizing policy")
        else:
            rationale.append(
                f"Edge {_signed(edge)}/share ({er:.1%} of cost) over {horizon_h:g} h; size {size['shares']:,} shares "
                f"= {size['cost']:,.0f} SUSQies ({_kelly_label(size['kelly_mult'])} on the target/stop bracket, capped at "
                f"{size['cap_pct']:.0%} of balance{' and by book depth' if size.get('depth_capped') else ''})")
        rationale.append(f"Exits sell at the {name} bid: half of {spread_text} is charged on the way out, so the target "
                         f"nets {_px(target_bid)} ({_signed(gain)}/share) and the stop {_px(stop_bid)} "
                         f"({_signed(-loss)}/share)")
        if levels_all is not None:
            offered = sum(q for _, q in cut)
            line = (f"Book ({_ago(now, book_at)}): {offered:,.0f} {name} shares on offer up to {_px(round(limit, 4))}, "
                    "where each keeps at least half the edge")
            fill = size.get("fill") if size else None
            if size is not None and fill is not None and size["shares"] > 0:
                line += (f"; {size['shares']:,} fill at an average of {_px(round(fill, 4))} "
                         f"({_signed(value - fill)}/share there)")
            rationale.append(line)
        if used is not None and not fv_capped:
            rationale.append(f"Outside fair value {_px(round(fv_c or 0.0, 4))} for {name} ({_source_label(used.source)}) "
                             "lies beyond the target: it does not cap it")
        if att.reasons:
            rationale.append(f"Evidence: {att.reasons[0]}")
        first = f"If the move was informed (news not out yet), YES may keep {rising}: the stop caps the loss at {_px(loss)}/share"
        if size is not None:
            first += f", about {size['shares'] * loss:,.0f} SUSQies at this size"
        risks = [first]
        if no_book:
            risks.append(f"No live book: the entry uses the mark {_px(mark)}; real fills will be worse by at least the spread")
        if levels_all is None:
            risks.append(f"Size not checked against a recent order book on the {name} side: check the depth "
                         "before buying, a large order moves the price")
        if att_depth is not None and att_depth < THIN_BOOK_SHARES:
            where = f"on the side a {name} buy takes " if side_depth is not None else ""
            risks.append(f"Thin book: about {att_depth:,.0f} shares rest {where}within 5c of the mid, so a large order "
                         "moves the price")
        risks.append(f"Exit within {horizon_h:g} h at the target or the stop: a reversion trade, not a hold-to-settlement bet")
        opp.rationale, opp.risks = rationale, risks

    render(None)
    return _Built(opp, render, {"levels": levels_all, "cut": cut, "att_depth": att_depth, "entry": entry, "p": p,
                                "gain": gain, "loss": loss, "value": value})


def fade_opportunity(surge: Surge, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                     balance: float, backtest: Optional[BacktestResult] = None, *,
                     cup_end: Optional[float] = None, kelly_mult: float = KELLY_MULT,
                     max_position_pct: float = MAX_POSITION_PCT, fair_value: Optional[FairValue] = None,
                     race: Optional[RaceRef] = None, regime: str = REGIME_UNKNOWN,
                     params: Optional[StrategyParams] = None) -> Optional[Opportunity]:
    """Bet on reversion of an open, participant-driven surge (None when not applicable or edge <= 0).

    After an up-move buy NO at ``1 - yes_bid``; after a down-move buy YES at ``yes_ask``
    (the mark without a book). Target: half the start->peak move given back, or the outside fair value
    when that is nearer (a fade never aims beyond fair value; a move TOWARD fair value is not faded).
    Stop: a further 50% extension beyond the peak. Both are sold at the bid, so half the spread is
    charged on the way out (``exit_plan.target_bid`` / ``stop_bid``); no fade under ``min_fade_gain``.
    ``p`` is the attribution's reversion odds, capped by the backtest's measured net-of-spread reversion
    rate of participant-driven surges when it covers at least 10 of them. Sized here with the legacy
    quarter-Kelly bracket rule at ``balance`` (``build_report`` re-sizes with a sizing policy).
    """
    p_ = params or StrategyParams()
    built = _fade_build(surge, current, info, now, backtest, cup_end, fv=fair_value, race=race, regime=regime,
                        params=p_, require_quote=False)
    if built is None:
        return None
    opp, meta = built.opp, built.meta
    entry = meta["entry"]
    kelly_shares = _bracket_shares(meta["p"], meta["gain"], meta["loss"], entry, balance, kelly_mult,
                                   max_position_pct, None)
    fill: Optional[float] = None
    if meta["levels"] is not None:
        offered = sum(q for _, q in meta["cut"])
        shares = min(kelly_shares, int(math.floor(offered + 1e-9)))
        fill = _avg_cost(meta["cut"], shares) if shares > 0 else None
    else:
        att_depth = meta["att_depth"]
        shares = kelly_shares if att_depth is None else min(kelly_shares, int(math.floor(max(0.0, att_depth) + 1e-9)))
    if shares <= 0:
        return None  # the book has no shares at a price that keeps an edge
    cost = round(shares * entry, 2)
    opp.suggested_shares, opp.suggested_cost = shares, cost
    opp.fill_price = round(fill, 6) if fill is not None else None
    built.render({"shares": shares, "cost": cost, "kelly_mult": kelly_mult, "cap_pct": max_position_pct,
                  "depth_capped": shares < kelly_shares, "fill": fill})
    return opp


# --------------------------------------------------------------------------- carry


def _carry_build(band: HighBand, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                 cup_end: float, *, fv: Optional[FairValue], race: Optional[RaceRef], regime: str,
                 params: StrategyParams, require_quote: bool) -> Optional[_Built]:
    if not band.stable:
        return None
    yes = (band.side or "YES").upper() != "NO"
    name = "YES" if yes else "NO"
    side = "yes" if yes else "no"
    quote = _two_sided(current)
    if require_quote and quote is None:
        return None
    bid = _num(current.bid) if current is not None else None
    ask = _num(current.ask) if current is not None else None
    mark = _num(current.price) if current is not None else None
    fav_mark = (mark if yes else 1.0 - mark) if mark is not None else _num(band.favorite_price)
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    no_book = False
    if yes:
        if ask is not None:
            entry, how = ask, "the ask"
        else:
            entry, how, no_book = fav_mark, "the mark, no live book", True
        fav_mid = mid if mid is not None else fav_mark
    else:
        if bid is not None:
            entry, how = 1.0 - bid, f"1 - YES bid {_px(bid)}"
        else:
            entry, how, no_book = fav_mark, "the mark, no live book", True
        fav_mid = 1.0 - mid if mid is not None else fav_mark
    if entry is None or fav_mid is None:
        return None
    entry, fav_mid = round(entry, 6), round(fav_mid, 6)
    if entry <= 0 or entry >= params.carry_max_entry - _EPS:
        return None
    used = _usable_fv(fv, now, params)
    if used is not None:
        v = float(used.value)  # type: ignore[arg-type]
        p_true = v if yes else 1.0 - v
        sigma = _num(used.uncertainty)
        sigma = sizing_mod.DEFAULT_SIGMA["carry"] if sigma is None else sigma
    else:
        adjust = min(0.01, 0.2 * (1.0 - fav_mid))
        p_true = min(1.0, fav_mid + adjust)
        sigma = sizing_mod.DEFAULT_SIGMA["carry"]
    settlement = band.settlement_date or (info.settlement_date if info is not None else None)
    before, settle_ts = _settles_before(settlement, cup_end, params.settle_margin_s)
    fav_side = max(fav_mid, 1.0 - fav_mid)
    called = called_prob(race, fav_side, params)
    ev, regime_text, detail = regime_ev(p_true, entry, fav_mid, regime, called, params, settles_before_end=before)
    if ev <= _EPS:
        return None
    er = ev / entry
    if before is True:
        horizon: Optional[float] = max(0.0, (settle_ts - now) / 3600.0)  # type: ignore[operator]
    elif before is False:
        horizon = max(0.0, (cup_end - now) / 3600.0)
    else:
        horizon = None
    limit = round(min(max(entry, depth.floor_tick(entry + 0.5 * ev)), max(entry, p_true)), 6)
    levels_all, book_at = _book_levels(side, current, now, None if no_book else entry, params.book_max_age_s)
    cut = _cut(levels_all, limit) if levels_all is not None else []
    confidence = round(min(0.9, 0.4 + 0.5 * _clamp01(band.time_in_band)), 4)
    bet = BetShape(kind="binary", p=round(min(1.0, entry + ev), 6), gain=round(1.0 - entry, 6), loss=entry, cost=entry)
    title, option = _title(info, band.market_id)
    p_yes = float(used.value) if used is not None else (_mid(current) if current is not None else (  # type: ignore[arg-type]
        fav_mid if yes else 1.0 - fav_mid))
    exit_plan = ExitPlan(kind="settle", hold_to_resolution=True,
                         exit_before_ts=_exit_before(regime, [race.state if race else None], before, cup_end, params),
                         note="Hold to settlement (pays 1.00 if it wins)")
    opp = Opportunity(
        kind=KIND_CARRY, exchange_id=band.exchange_id, market_id=band.market_id, title=title, option=option, side=side,
        entry_price=round(entry, 4), target_price=1.0, stop_price=None, prob_win=round(p_true, 4), edge=round(ev, 6),
        expected_return=round(er, 6), horizon_hours=round(horizon, 2) if horizon is not None else None,
        suggested_shares=0, suggested_cost=0.0, score=growth_score(bet, round(ev, 6), confidence, entry), confidence=confidence,
        settles_before_cup_end=before, depth_checked=levels_all is not None, fill_price=None,
        idea_id=idea_id_for(KIND_CARRY, exchange_id=band.exchange_id, side=side),
        race_key=race.race_key if race is not None else None, bet=bet, bet_limit=sizing_mod.bet_at_cost(bet, limit),
        exit_plan=exit_plan, order_type="taker", limit_price=limit, levels=cut,
        max_units=float(sum(q for _, q in cut)) if levels_all is not None else None,
        fair_value=round(p_true, 4) if used is not None else None, fair_source=used.source if used is not None else None,
        settlement_regime=_regime(regime), fv_uncertainty=sigma, factor_delta=factor_delta(race, side, p_yes, params),
        called_prob=round(called, 4),
        priced_from=str(getattr(current, "source", "tick") or "tick") if current is not None else "tick",
    )
    regime_used = _regime(regime)

    def render(size: Optional[Dict[str, Any]]) -> None:
        rationale = [
            f"{name} has sat at {_px(band.low)}-{_px(band.high)} (mean {_px(band.mean)}) for "
            f"{band.time_in_band:.0%} of the last {band.lookback_s / 3600.0:g} h: a stable high-90s favourite",
            f"Buy {name} at {_px(entry)} ({how}): it pays 1.00 if {name} wins ({_signed(1.0 - entry)}/share)",
        ]
        if used is not None:
            unc = _num(used.uncertainty)
            rationale.append(f"Fair value {p_true:.3f} for {name} from {_source_label(used.source)}"
                             + (f" (uncertainty {unc:.3f})" if unc is not None else "")
                             + ": the outside price, not a rule of thumb")
        else:
            rationale.append(
                f"Fair value {p_true:.3f} = mid {fav_mid:.3f} + min(0.01, 0.2 x {1.0 - fav_mid:.3f}): a small "
                "favourite-longshot adjustment, since heavy favourites tend to be slightly underpriced (a rule of thumb, "
                "not a measured edge)")
        if size is None:
            rationale.append(f"Edge {_signed(ev)}/share ({er:.2%} of cost), slow but steady; the size comes from the "
                             "sizing policy")
        else:
            rationale.append(
                f"Edge {_signed(ev)}/share ({er:.2%} of cost), slow but steady; {size['shares']:,} shares = "
                f"{size['cost']:,.0f} SUSQies ({_kelly_label(size['kelly_mult'])}, capped at {size['cap_pct']:.0%} of "
                f"balance{' and by book depth' if size.get('depth_capped') else ''})")
        if levels_all is not None:
            offered = sum(q for _, q in cut)
            line = (f"Book ({_ago(now, book_at)}): {offered:,.0f} {name} shares on offer at prices that keep at least "
                    "half the edge")
            fill = size.get("fill") if size else None
            if size is not None and fill is not None and size["shares"] > 0:
                line += (f"; {size['shares']:,} fill at an average of {_px(round(fill, 4))} "
                         f"({_signed(entry + ev - fill)}/share there)")
            rationale.append(line)
        if before is True:
            rationale.append(f"Settles {_when(settle_ts)} (in {horizon:.0f} h), before the Cup ends: paid out at 1.00 if "
                             "it wins")
        else:
            where = (f"Settles {_when(settle_ts)}, after the Cup ends ({_when(cup_end)})" if before is False
                     else "Settlement date unknown")
            rationale.append(f"{where}: {regime_text} (called probability {called:.0%})")
        shares = size["shares"] if size is not None else None
        if shares is not None:
            risks = [f"An upset loses the whole {_px(entry)}/share: {shares:,} shares put {size['cost']:,.0f} SUSQies "  # type: ignore[index]
                     f"at risk to make {shares * (1.0 - entry):,.0f}"]
        else:
            risks = [f"An upset loses the whole {_px(entry)}/share to make {_signed(1.0 - entry)}/share"]
        if before is True:
            risks.append(SETTLEMENT_RISK)
        elif before is False:
            if regime_used == REGIME_RESOLVED:
                risks.append(f"It settles {_when(settle_ts)}, after the Cup ends on {_when(cup_end)}: under the "
                             "resolved-outcomes rule it is paid at the real result, which may take a while")
            else:
                payoff = detail.get("uncalled_payoff")
                risks.append(f"If the Cup closes out at a 5-hour VWAP, it is valued at market price at Cup end, not paid "
                             f"out: it settles {_when(settle_ts)}, after the Cup ends on {_when(cup_end)}, so the result "
                             "depends on the price then"
                             + (f" (an uncalled race closes near a coin flip: {payoff:+.3f}/share)" if payoff is not None
                                else ""))
        else:
            risks.append("Settlement date unknown: it may not pay out before the Cup ends")
        if no_book:
            risks.append(f"No live book: the entry uses the mark; check the {name} ask before buying")
        if levels_all is None:
            risks.append(f"Size not checked against a recent order book on the {name} side: at the next price level "
                         "up the edge may be gone, so check the depth before buying")
        if used is None:
            risks.append("The favourite-longshot adjustment is a rule of thumb, not a measured edge")
        opp.rationale, opp.risks = rationale, risks

    render(None)
    return _Built(opp, render, {"levels": levels_all, "cut": cut, "entry": entry, "p": entry + ev, "ev": ev})


def carry_opportunity(band: HighBand, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                      cup_end: float, balance: float, *, kelly_mult: float = KELLY_MULT,
                      max_position_pct: float = MAX_POSITION_PCT, fair_value: Optional[FairValue] = None,
                      race: Optional[RaceRef] = None, regime: str = REGIME_UNKNOWN,
                      params: Optional[StrategyParams] = None) -> Optional[Opportunity]:
    """Buy a stable high-90s favourite at its ask (None when unstable, no room, or edge <= 0).

    ``p_true`` = the usable outside fair value of the contract, else the rule of thumb ``fav_mid +
    min(0.01, 0.2 * (1 - fav_mid))``. The edge is :func:`regime_ev`: a market settling before the Cup end
    pays 1/0 (``p_true - entry``); one settling at the Cup end carries the uncalled-branch loss of a VWAP
    closeout unless the regime is ``resolved_outcomes`` (no more "score halved" rule). Sized here with the
    legacy quarter-Kelly rule at ``balance``.
    """
    p_ = params or StrategyParams()
    built = _carry_build(band, current, info, now, cup_end, fv=fair_value, race=race, regime=regime, params=p_,
                         require_quote=False)
    if built is None:
        return None
    opp, meta = built.opp, built.meta
    entry = meta["entry"]
    kelly_shares = size_position(meta["p"], entry, balance, kelly_mult, max_position_pct)
    shares = kelly_shares
    fill: Optional[float] = None
    if meta["levels"] is not None:
        offered = sum(q for _, q in meta["cut"])
        shares = min(kelly_shares, int(math.floor(offered + 1e-9)))
        fill = _avg_cost(meta["cut"], shares) if shares > 0 else None
        if shares <= 0:
            return None
    cost = round(shares * entry, 2)
    opp.suggested_shares, opp.suggested_cost = shares, cost
    opp.fill_price = round(fill, 6) if fill is not None else None
    built.render({"shares": shares, "cost": cost, "kelly_mult": kelly_mult, "cap_pct": max_position_pct,
                  "depth_capped": shares < kelly_shares, "fill": fill})
    return opp


# --------------------------------------------------------------------------- watch


def watch_opportunity(surge: Surge, info: Optional[ExchangeInfo]) -> Optional[Opportunity]:
    """An open surge whose cause is unclear: no size, wait for confirmation (None otherwise)."""
    att = surge.attribution
    if surge.status != SURGE_OPEN or att is None or att.verdict != VERDICT_UNCLEAR:
        return None
    levels = _surge_levels(surge)
    if levels is None:
        return None
    up, move, yes_target, yes_stop = levels
    mark = next((v for v in (_num(surge.current_price), _num(surge.end_price)) if v is not None), surge.end_price)
    side = "no" if up else "yes"
    entry = 1.0 - mark if up else mark
    target = 1.0 - yes_target if up else yes_target
    stop = 1.0 - yes_stop if up else yes_stop
    title, option = _title(info, surge.market_id)
    verb = "jumped" if up else "dropped"
    rationale = [
        f"YES {verb} {_signed(move)} in {surge.window} ({_px(surge.start_price)} -> {_px(surge.peak_price)}, now "
        f"{_px(mark)}); the cause is unclear ({_clamp01(att.confidence):.0%} confidence)",
        f"Wait for confirmation: fade it (buy {side.upper()} near {_px(entry)}, target {_px(target)}) only if no news "
        "appears and the move stalls; if credible news breaks, the move will probably hold",
    ]
    rationale.extend(f"Evidence: {reason}" for reason in att.reasons[:2])
    risks = [
        f"Unclear moves revert about {_clamp01(att.reversion_odds):.0%} of the time by the heuristic: not enough edge to trade yet",
    ]
    return Opportunity(
        kind=KIND_WATCH,
        exchange_id=surge.exchange_id,
        market_id=surge.market_id,
        title=title,
        option=option,
        side=side,
        entry_price=round(entry, 4),
        target_price=round(target, 4),
        stop_price=round(stop, 4),
        prob_win=round(_clamp01(att.reversion_odds), 4),
        edge=None,
        expected_return=None,
        horizon_hours=FADE_HORIZON_H,
        suggested_shares=0,
        suggested_cost=0.0,
        score=0.0,
        confidence=round(_clamp01(att.confidence), 4),
        rationale=rationale,
        risks=risks,
        surge_id=surge.id,
        idea_id=idea_id_for(KIND_WATCH, exchange_id=surge.exchange_id, side=side, surge_id=surge.id),
    )


# --------------------------------------------------------------------------- arbitrage


def _leg(trade: Mapping[str, Any]) -> Tuple[str, Optional[float]]:
    """(contract to buy, its cost) for an engine-suggested trade; Sell YES == buy NO."""
    yes_side = str(trade.get("outcomeSide") or "yes").strip().lower().startswith("y")
    sell = str(trade.get("action") or "buy").strip().lower().startswith("s")
    contract = "yes" if yes_side != sell else "no"
    price = _num(trade.get("currentPrice"))  # YES-normalised exchange price
    if price is None:
        return contract, None
    return contract, (price if contract == "yes" else 1.0 - price)


def _quote_cost(contract: str, point: Optional[PricePoint]) -> Optional[float]:
    """What buying ``contract`` costs at the live book: the YES ask, or ``1 - YES bid`` for NO."""
    if point is None:
        return None
    if contract == "yes":
        ask = _num(point.ask)
        return ask if ask is not None and 0.0 < ask < 1.0 else None
    bid = _num(point.bid)
    return round(1.0 - bid, 6) if bid is not None and 0.0 < bid < 1.0 else None


def _members(v: Mapping[str, Any]) -> List[str]:
    """Exchange ids the relationship constrains (direction members, else the observed prices)."""
    ids: List[str] = []
    direction = v.get("direction")
    if isinstance(direction, Mapping):
        for key in ("fromExchangeIds", "toExchangeIds"):
            ids.extend(str(x) for x in direction.get(key) or [] if x is not None)
    if not ids:
        ids = [str(o.get("exchangeId")) for o in v.get("observedPrices") or []
               if isinstance(o, Mapping) and o.get("exchangeId") is not None]
    return list(dict.fromkeys(ids))


def _set_payoff(kind: str, legs: Sequence[Mapping[str, Any]], members: Sequence[str]) -> Tuple[Optional[float], bool]:
    """(guaranteed settlement payoff of one set of the legs, whether it is only a lower bound).

    * NO on n members of a mutually exclusive (or complementary) group: at most one wins, so at
      least ``n - 1`` NO shares pay (exactly ``n - 1`` when the group is complementary and covered).
    * YES on every member of a complementary group: exactly one wins, so the set pays 1.
    Anything else (monotonic, implication, mixed sides): unknown, ``(None, False)``.
    """
    ids = [str(leg.get("exchange_id")) for leg in legs]
    if len(legs) < 2 or len(set(ids)) != len(ids):
        return None, False
    sides = {leg.get("side") for leg in legs}
    kind = str(kind or "").strip().lower()
    covered = bool(members) and set(members) <= set(ids)
    if sides == {"no"} and kind in ("mutually_exclusive", "complementary"):
        return float(len(legs) - 1), not (kind == "complementary" and covered)
    if sides == {"yes"} and kind == "complementary" and covered:
        return 1.0, False
    return None, False


def _legs_settle_before(exchange_ids: Sequence[Optional[str]], infos: Mapping[str, ExchangeInfo],
                        cup_end: Optional[float], margin_s: float = 3600.0) -> Tuple[Optional[bool], Optional[float]]:
    """True/False when every leg's settlement date is known (all before the Cup end?), else None.

    Also returns the latest settlement time of the legs.
    """
    if cup_end is None or not exchange_ids:
        return None, None
    latest: Optional[float] = None
    verdicts: List[bool] = []
    for eid in exchange_ids:
        info = infos.get(str(eid)) if eid is not None else None
        before, settle = _settles_before(info.settlement_date if info is not None else None, cup_end, margin_s)
        if before is None or settle is None:
            return None, None
        verdicts.append(before)
        latest = settle if latest is None else max(latest, settle)
    return all(verdicts), latest


def _settlement_risk(before: Optional[bool], settle: Optional[float], cup_end: Optional[float]) -> str:
    if before is True:
        return (f"It pays at settlement ({_when(settle)}), before the Cup ends; but a settlement date is not a payout "
                "date: autosettlement can retry, REFUND and tournament rulings exist")
    if before is False:
        return (f"Some legs settle after the Cup ends ({_when(cup_end)}): until then they are only valued at "
                "market price, not paid out, and under a VWAP closeout each leg is cashed at its own VWAP")
    return "It pays at settlement; if that is after the Cup ends it is only valued at market price then"


_PARTY_WORDS = {"D": "Democratic", "R": "Republican", "I": "Independent", "L": "Libertarian", "G": "Green"}
_OFFICE_WORDS = {"SENATE": "Senate", "GOVERNOR": "Governor"}


def _leg_name(leg: Mapping[str, Any]) -> str:
    """A leg's outcome in words: its option, or for a binary market (option "YES") the party, else the title."""
    option = str(leg.get("option") or "").strip()
    if option and option.upper() not in ("YES", "NO"):
        return option
    party = _PARTY_WORDS.get(str(leg.get("party") or ""))
    if party:
        return party
    return str(leg.get("title") or option or f"exch {leg.get('exchange_id')}")


def _legs_text(legs: Sequence[Mapping[str, Any]]) -> str:
    return " + ".join(f"{str(leg.get('side') or '').upper()} {_leg_name(leg)} @ {_px(leg.get('price'))}" for leg in legs)


def _race_words(race: Optional[RaceRef], fallback: str) -> str:
    """ "the Nevada Governor race", "AZ-01", "control of the U.S. Senate" (``fallback`` when unknown)."""
    if race is None:
        return fallback
    if race.office == "SENATE_CONTROL":
        return "control of the U.S. Senate"
    if race.office == "HOUSE_CONTROL":
        return "control of the U.S. House"
    if race.office == "HOUSE":
        return f"the {race.district or race.state} House race"
    office = _OFFICE_WORDS.get(race.office)
    if office is None:
        return fallback
    return f"the {_STATE_WORDS.get(race.state, race.state)} {office} race"


_STATE_WORDS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "the District of Columbia",
}


def _sizing_text(shares: int, cap: int, unit: str, max_position_pct: float, offered: Optional[float],
                 fill: Optional[float], limit: Optional[float], book_at: Optional[float], now: Optional[float]) -> str:
    """How a set/arbitrage size was chosen: the balance cap, and what the books can fill at a
    cost up to ``limit`` (where a further one still keeps half the edge)."""
    if offered is None:
        return f"{shares:,} {unit} at the {max_position_pct:.0%}-of-balance cap"
    when = f" ({_ago(now, book_at)})" if now is not None else ""
    one = unit[:-1] if unit.endswith("s") else unit
    if shares <= 0:
        return f"but the order books{when} hold no full {one} at up to {_px(round(limit or 0.0, 4))}: nothing to size"
    capped = "the books' depth" if shares < cap else f"the {max_position_pct:.0%}-of-balance cap"
    avg = f", at an average of {_px(round(fill, 4))} each" if fill is not None else ""
    return (f"{shares:,} {unit} ({capped}; the books{when} offer {offered:,.0f} at up to {_px(round(limit or 0.0, 4))} "
            f"per {one}, where each keeps at least half the edge{avg})")


def _set_depth(legs: Sequence[Mapping[str, Any]], latest: Mapping[str, PricePoint], now: Optional[float],
               limit: Optional[float]) -> Tuple[Optional[float], Optional[List[Levels]], Optional[float]]:
    """(full sets on offer at a set cost up to ``limit``, each leg's levels, the oldest book
    time), or ``(None, None, None)`` when a leg has no usable recent book (sizes are then not
    depth-checked)."""
    if now is None or limit is None or not legs:
        return None, None, None
    books: List[Levels] = []
    oldest: Optional[float] = None
    for leg in legs:
        eid = leg.get("exchange_id")
        levels, at = _book_levels(str(leg.get("side") or "yes"), latest.get(str(eid)) if eid is not None else None,
                                  now, _num(leg.get("price")))
        if levels is None:
            return None, None, None
        books.append(levels)
        oldest = at if oldest is None or (at is not None and at < oldest) else oldest
    return _walk_set(books, limit), books, oldest


def _set_fill(books: Sequence[Levels], sets: float) -> Optional[float]:
    """Average cost of ``sets`` full sets: the sum of each leg's average fill."""
    costs = [_avg_cost(levels, sets) for levels in books]
    return round(sum(costs), 6) if costs and all(c is not None for c in costs) else None  # type: ignore[misc]


def _enrich_legs(legs: List[Dict[str, Any]], latest: Mapping[str, PricePoint], races: Mapping[str, RaceRef],
                 edge: Optional[float]) -> None:
    """Add the §3 leg fields: per-leg limit (``floor_tick(price + 0.5 x edge / n)``), race, party, touch, quote time."""
    n = len(legs)
    for leg in legs:
        eid = str(leg.get("exchange_id")) if leg.get("exchange_id") is not None else None
        point = latest.get(eid) if eid is not None else None
        quote = _two_sided(point)
        race = races.get(eid) if eid is not None else None
        price = _num(leg.get("price"))
        if price is not None and edge is not None and edge > 0 and n:
            leg["limit"] = round(max(price, depth.floor_tick(price + 0.5 * edge / n)), 3)
        else:
            leg["limit"] = round(price, 6) if price is not None else None
        leg["race_key"] = race.race_key if race is not None else None
        leg["party"] = race.party if race is not None else None
        leg["yes_bid"] = quote[0] if quote is not None else None
        leg["yes_ask"] = quote[1] if quote is not None else None
        leg["quote_ts"] = _num(point.ts) if point is not None else None


def _set_book_levels(legs: Sequence[Mapping[str, Any]], latest: Mapping[str, PricePoint], now: Optional[float],
                     limit: Optional[float], params: StrategyParams) -> Tuple[Levels, Optional[float]]:
    """(``depth.set_levels`` of every leg's book cut at its leg limit, up to the set limit; max units), or
    ``([], None)`` when a leg has no usable recent book."""
    if now is None or limit is None or not legs:
        return [], None
    cuts: List[Levels] = []
    for leg in legs:
        eid = leg.get("exchange_id")
        point = latest.get(str(eid)) if eid is not None else None
        levels, _ = _book_levels(str(leg.get("side") or "yes"), point, now, _num(leg.get("price")),
                                 params.book_max_age_s)
        if levels is None:
            return [], None
        cuts.append(_cut(levels, _num(leg.get("limit")) or _num(leg.get("price")) or 0.0))
    if any(not c for c in cuts):
        return [], 0.0
    lv = depth.set_levels(cuts, limit)
    return [(round(p, 6), q) for p, q in lv], float(sum(q for _, q in lv))


def _set_bet(legs: Sequence[Mapping[str, Any]], cost: float, floor: Optional[float], edge: float, regime: str,
             races: Mapping[str, RaceRef], latest: Mapping[str, PricePoint],
             params: StrategyParams) -> Tuple[BetShape, float, str]:
    """(bet, called probability, why) for a set: riskless only under ``resolved_outcomes`` or when every
    leg's race is a fast count (never chamber control); otherwise bounded (refund tail, uncalled branch priced
    as no gain, D27)."""
    calls: List[float] = []
    fast = True
    chamber = False
    for leg in legs:
        eid = str(leg.get("exchange_id"))
        race = races.get(eid)
        mid = _mid(latest.get(eid))
        if mid is None:
            price = _num(leg.get("price"))
            mid = (price if leg.get("side") == "yes" else 1.0 - price) if price is not None else None
        fav = max(mid, 1.0 - mid) if mid is not None else None
        c = called_prob(race, fav, params)
        calls.append(c)
        chamber = chamber or is_chamber(race)
        if race is None or _slow_race(race) or c < params.called_prob_fast - 1e-9:
            fast = False
    called = min(calls) if calls else (params.called_prob_fast + params.called_prob_slow) / 2.0
    regime = _regime(regime)
    if regime == REGIME_RESOLVED or fast:
        why = ("the regime is resolved outcomes, so every leg pays 1/0" if regime == REGIME_RESOLVED
               else "every leg's race is a fast count, so it is called before any closeout")
        return BetShape(kind="riskless", p=1.0, gain=round(edge, 6), loss=0.0, cost=round(cost, 6), floor=floor), called, why
    costs = [_num(leg.get("price")) for leg in legs]
    cheapest = min(c for c in costs if c is not None) if any(c is not None for c in costs) else 0.0
    tail = params.basket_refund_prob
    states = sorted({races[str(leg.get('exchange_id'))].state for leg in legs if str(leg.get("exchange_id")) in races})
    slow = [s for s in states if s in SLOW_COUNT_STATES]
    if chamber:
        why = (f"chamber control is decided by the last seats counted (slow counts such as CA, AZ and NV), so it is "
               f"often uncalled at the closeout ({called:.0%} called before it)")
    elif slow:
        why = f"{', '.join(slow)} counts slowly (called before the closeout {called:.0%})"
    elif not all(str(leg.get("exchange_id")) in races for leg in legs):
        why = f"the race is not known, so it may not be called before the closeout ({called:.0%})"
    else:
        why = f"the Cup's end rule is unknown and the race may be uncalled at the closeout ({called:.0%} called)"
    bet = BetShape(kind="bounded", p=round(1.0 - tail, 6), gain=round(edge * called, 6),
                   loss=round(max(0.0, cost - cheapest), 6), cost=round(cost, 6), floor=floor, tail_prob=tail)
    return bet, called, why


def _basket_exit(edge: float, regime: str, states: Sequence[Optional[str]], before: Optional[bool],
                 cup_end: Optional[float], params: StrategyParams, no_basket: bool) -> ExitPlan:
    min_profit = max(params.basket_exit_min_profit, params.basket_exit_fraction * edge)
    how = " (for a NO basket: entry bid sum minus exit ask sum)" if no_basket else ""
    return ExitPlan(kind="basket", min_set_profit=round(min_profit, 6), hold_to_resolution=_regime(regime) == REGIME_RESOLVED,
                    exit_before_ts=_exit_before(regime, states, before, cup_end, params),
                    note=(f"Sell the set when selling every leg brings at least {_signed(min_profit)} per set over its "
                          f"cost{how}; otherwise hold to settlement"))


def _arbitrage_build(constraints: Optional[Mapping[str, Any]], overround_rows: Sequence[Mapping[str, Any]], *,
                     latest: Mapping[str, PricePoint], infos: Mapping[str, ExchangeInfo], cup_end: Optional[float],
                     now: Optional[float], races: Mapping[str, RaceRef], regime: str,
                     params: StrategyParams) -> List[_Built]:
    out: List[_Built] = []
    rows: List[Any] = []
    if constraints:
        rows = list(constraints.get("data") or constraints.get("violations") or [])
    for v in rows:
        if not isinstance(v, Mapping):
            continue
        amount = _num(v.get("violationAmount")) or 0.0
        if amount <= _EPS:
            continue
        trades = [t for t in (v.get("suggestedCorrectiveTrades") or []) if isinstance(t, Mapping)]
        legs: List[Dict[str, Any]] = []
        live = bool(trades)
        for t in trades:
            contract, engine_cost = _leg(t)
            eid = str(t.get("exchangeId")) if t.get("exchangeId") is not None else None
            quoted = _quote_cost(contract, latest.get(eid)) if eid is not None else None
            live = live and quoted is not None
            cost = quoted if quoted is not None else engine_cost
            info = infos.get(eid) if eid is not None else None
            legs.append({
                "exchange_id": eid,
                "market_id": str(t.get("marketId")) if t.get("marketId") is not None else (info.market_id if info else None),
                "title": t.get("marketTitle") or (info.market_title if info else None),
                "option": t.get("outcome") if t.get("outcome") is not None else (info.option if info else None),
                "side": contract,
                "price": round(cost, 4) if cost is not None else None,
            })
        costs = [leg["price"] for leg in legs]
        entry = round(sum(costs), 6) if legs and all(c is not None for c in costs) else None  # type: ignore[misc]
        multi = len(legs) > 1
        first = trades[0] if trades else {}
        observed = next((o for o in (v.get("observedPrices") or []) if isinstance(o, Mapping)), {})
        market_id = str(first.get("marketId") or observed.get("marketId") or "")
        kind = v.get("type") or "relationship"
        titles = list(dict.fromkeys(str(leg["title"]) for leg in legs if leg.get("title")))
        if multi and len(titles) > 1:
            title = " / ".join(titles[:3])
        else:
            title = first.get("marketTitle") or observed.get("marketTitle") or str(v.get("reason") or f"{kind} relationship")
        payoff, at_least = _set_payoff(str(kind), legs, _members(v)) if multi else (None, False)
        if payoff is not None and entry is not None:
            edge: Optional[float] = round(payoff - entry, 6)
        else:
            edge = round(amount, 6)
        er = edge / entry if entry and edge is not None else None
        tradable = entry is not None and edge is not None and edge > _EPS
        worth = payoff if payoff is not None else ((entry + amount) if entry is not None else None)
        legacy_limit = _depth_limit(worth, entry) if worth is not None and entry is not None else None
        before, settle = _legs_settle_before([leg["exchange_id"] for leg in legs], infos, cup_end, params.settle_margin_s)
        _enrich_legs(legs, latest, races, edge if tradable else None)
        unit = "sets" if multi else "shares"
        single = legs[0] if legs and not multi else None
        sides = {leg["side"] for leg in legs}
        states = [races[str(leg["exchange_id"])].state for leg in legs if str(leg.get("exchange_id")) in races]
        bet: Optional[BetShape] = None
        why = ""
        called: Optional[float] = None
        limit_price: Optional[float] = None
        levels: Levels = []
        max_units: Optional[float] = None
        exit_plan: Optional[ExitPlan] = None
        race_keys = {leg.get("race_key") for leg in legs}
        race_key = next(iter(race_keys)) if len(race_keys) == 1 and None not in race_keys else None
        fdelta = 0.0
        if tradable and multi:
            limit_price = round(sum(leg["limit"] for leg in legs), 6)
            levels, max_units = _set_book_levels(legs, latest, now, limit_price, params)
            bet, called, why = _set_bet(legs, entry, payoff, edge, regime, races, latest, params)  # type: ignore[arg-type]
            exit_plan = _basket_exit(edge, regime, states, before, cup_end, params,  # type: ignore[arg-type]
                                     no_basket=sides == {"no"})
        elif tradable and single is not None:
            eid = single["exchange_id"]
            limit_price = round(max(entry, depth.floor_tick(entry + 0.5 * edge)), 6)  # type: ignore[operator]
            lv_all = None
            if now is not None:  # without a decision time no stored book can be judged fresh
                lv_all, _ = _book_levels(single["side"], latest.get(str(eid)), now, entry, params.book_max_age_s)
            if lv_all is not None:
                levels = _cut(lv_all, limit_price)
                max_units = float(sum(q for _, q in levels))
            bet = BetShape(kind="binary", p=round(min(1.0, entry + edge), 6), gain=round(1.0 - entry, 6),  # type: ignore[operator]
                           loss=entry, cost=entry)  # type: ignore[arg-type]
            race = races.get(str(eid))
            exit_plan = ExitPlan(kind="settle", hold_to_resolution=True,
                                 exit_before_ts=_exit_before(regime, [race.state if race else None], before, cup_end, params),
                                 note="Hold until the prices correct or the market settles")
            fdelta = factor_delta(race, single["side"], _mid(latest.get(str(eid))), params)
        if multi:
            idea_id = idea_id_for(KIND_ARBITRAGE, legs=legs)
        elif single is not None:
            idea_id = idea_id_for(KIND_ARBITRAGE, exchange_id=single["exchange_id"], side=single["side"])
        else:
            idea_id = f"{KIND_ARBITRAGE}:market:{market_id}"
        rule = (v.get("constraint") or {}).get("priceRule") if isinstance(v.get("constraint"), Mapping) else None
        opp = Opportunity(
            kind=KIND_ARBITRAGE,
            exchange_id=single["exchange_id"] if single else None,
            market_id=market_id,
            title=str(title),
            option=single["option"] if single else None,
            side=(sides.pop() if len(sides) == 1 else legs[0]["side"]) if legs else "yes",
            entry_price=round(entry, 4) if entry is not None else None,
            target_price=round(payoff, 4) if payoff is not None and not at_least else None,
            stop_price=None,
            prob_win=None if bet is None else round(bet.p, 4),
            edge=edge,
            expected_return=round(er, 6) if er is not None else None,
            horizon_hours=None,
            suggested_shares=0,
            suggested_cost=0.0,
            score=growth_score(bet, edge, CONSTRAINT_CONFIDENCE, entry) if tradable else 0.0,
            confidence=CONSTRAINT_CONFIDENCE,
            settles_before_cup_end=before,
            legs=legs if multi else [],
            unit=unit,
            depth_checked=(max_units is not None) if tradable else None,
            fill_price=None,
            idea_id=idea_id, race_key=race_key, bet=bet,
            bet_limit=sizing_mod.bet_at_cost(bet, limit_price) if bet is not None and limit_price is not None else None,
            exit_plan=exit_plan, order_type="taker", limit_price=limit_price, levels=levels, max_units=max_units,
            settlement_regime=_regime(regime), factor_delta=fdelta if not multi else 0.0,
            called_prob=round(called, 4) if called is not None else None,
            priced_from="tick" if live else "engine",
        )
        if not multi and single is not None:
            opp.priced_from = str(getattr(latest.get(str(single["exchange_id"])), "source", "tick") or "tick") if live else "engine"

        def render(size: Optional[Dict[str, Any]], *, _v=v, _opp=opp, _legs=legs, _trades=trades, _kind=kind,
                   _amount=amount, _entry=entry, _payoff=payoff, _at_least=at_least, _edge=edge, _er=er,
                   _tradable=tradable, _live=live, _multi=multi, _unit=unit, _rule=rule, _before=before,
                   _settle=settle, _bet=bet, _why=why, _legacy_limit=legacy_limit) -> None:
            rationale = [f"Engine-reported {_kind} violation of {_px(_amount)}: "
                         f"{_v.get('reason') or 'prices break the relationship'}"]
            if _rule:
                rationale.append(f"Rule: {_rule}")
            for t, leg in zip(_trades, _legs):
                rationale.append(
                    f"{t.get('action') or 'Trade'} {t.get('outcomeSide') or ''} on {t.get('marketTitle') or t.get('marketId') or '?'}"
                    f"{' - ' + str(t.get('outcome')) if t.get('outcome') else ''} (exch {t.get('exchangeId')}, buy {leg['side'].upper()} "
                    f"at {_px(leg['price'])}): {t.get('rationale') or ''}".rstrip(": ")
                )
            if size is None:
                sizing = "the size comes from the sizing policy"
            else:
                sizing = _sizing_text(size["shares"], size["cap_shares"], _unit, size["cap_pct"], size.get("offered"),
                                      size.get("fill"), size.get("limit"), size.get("book_at"), size.get("now"))
            if _entry is not None and _payoff is not None:
                pays = f"{'at least ' if _at_least else ''}{_payoff:.2f}"
                if _tradable:
                    rationale.append(f"One set ({_legs_text(_legs)}) costs {_px(_entry)} and pays {pays} at settlement: "
                                     f"{_signed(_edge)} per set ({_er:.1%}); {sizing}")  # type: ignore[arg-type]
                else:
                    rationale.append(f"At these prices one set ({_legs_text(_legs)}) costs {_px(_entry)}, no less than the "
                                     f"{pays} it pays: the gap is gone after the spread, so there is nothing to size")
            elif _entry is not None:
                what = f"The {len(_legs)} legs ({_legs_text(_legs)}) cost {_px(_entry)} per set" if _multi else \
                    f"The trade costs {_px(_entry)} per share"
                rationale.append(f"{what} and gain about {_px(_amount)} as prices correct ({_er:.1%}); {sizing}")
            if _bet is not None and _multi:
                if _bet.kind == "riskless":
                    rationale.append(f"Riskless at a 1/0 settlement: {_why}")
                else:
                    rationale.append(f"Not riskless: {_why}; a refund or ruling on one leg, or an uncalled race cashed at "
                                     f"its VWAP under a closeout, breaks the floor, so it is sized as a bounded bet "
                                     f"({_bet.p:.0%} to gain about {_px(_bet.gain)}/set, {1 - _bet.p:.0%} to lose up to "
                                     f"{_px(_bet.loss)}/set)")
                if _opp.exit_plan is not None:
                    rationale.append(_opp.exit_plan.note)
            prices_risk = ("Leg prices are the live asks (NO = 1 - YES bid): fills can still move them"
                           if _live else
                           "Prices are last/valuation prices, not executable quotes: check the asks on every leg, the spread can eat the gap")
            risks = [prices_risk]
            if _tradable and (size is None or size.get("offered") is None) and _opp.max_units is None:
                risks.append("Size not checked against recent order books on every leg: check the depth on each one, "
                             "walking a thin book can cost more than the set pays")
            if _multi:
                risks.append("Legs fill separately: a partial fill leaves a one-sided position")
            risks.append("The gap can persist until settlement, tying up capital")
            risks.append(_settlement_risk(_before, _settle, cup_end))
            _opp.rationale, _opp.risks = rationale, risks

        render(None)
        out.append(_Built(opp, render, {"path": "constraint", "entry": entry, "edge": edge, "tradable": tradable,
                                        "live": live, "legs": legs, "legacy_limit": legacy_limit}))

    by_market: Dict[str, List[ExchangeInfo]] = {}
    for info in infos.values():
        by_market.setdefault(str(info.market_id), []).append(info)
    exhaustive_markets = {g.market_ids[0] for g in group_races(infos, races, constraints)
                          if g.source == "market" and g.exhaustive and g.market_ids}
    for row in overround_rows or ():
        if not isinstance(row, Mapping) or not (row.get("arbitrage") or row.get("hasArbitrageOpportunity")):
            continue
        over = _num(row.get("overround"))
        market_id = str(row.get("market_id") or row.get("marketId") or row.get("id") or "")
        title = str(row.get("market_title") or row.get("marketTitle") or row.get("title") or f"Market {market_id}")
        n_out = row.get("outcomes")
        members = sorted(by_market.get(market_id) or [], key=lambda m: _eid_key(m.exchange_id))
        before, settle = _legs_settle_before([m.exchange_id for m in members], infos, cup_end, params.settle_margin_s)
        legs = []
        if members and (not isinstance(n_out, int) or n_out == len(members)):
            legs = [{"exchange_id": m.exchange_id, "market_id": market_id, "title": title, "option": m.option,
                     "side": "yes", "price": _quote_cost("yes", latest.get(m.exchange_id))} for m in members]
            if any(leg["price"] is None for leg in legs):
                legs = []  # without every ask the per-leg list would not add up to the set price
        if legs:
            # Price the set from the live asks it lists, so the set price is the sum of its legs.
            entry = round(sum(leg["price"] for leg in legs), 6)
            if entry >= 1.0 - _EPS:
                continue  # the asks no longer leave a gap: the flag is from an older book read
            edge = round(1.0 - entry, 6)
        else:
            entry = over if over is not None and over > 0 else None
            edge = round(1.0 - over, 6) if over is not None and over < 1.0 - _EPS else None
        er = edge / entry if edge is not None and entry else None
        legacy_limit = _depth_limit(1.0, entry) if entry is not None else None
        bet = None
        why = ""
        called = None
        limit_price = None
        levels = []
        max_units = None
        exit_plan = None
        if legs and edge is not None and edge > _EPS:
            _enrich_legs(legs, latest, races, edge)
            limit_price = round(sum(leg["limit"] for leg in legs), 6)
            levels, max_units = _set_book_levels(legs, latest, now, limit_price, params)
            states = [races[str(leg["exchange_id"])].state for leg in legs if str(leg["exchange_id"]) in races]
            bet, called, why = _set_bet(legs, entry, 1.0, edge, regime, races, latest, params)  # type: ignore[arg-type]
            if market_id not in exhaustive_markets:
                # without a catch-all outcome or a complementary relationship the set is only a potential
                # arbitrage: if none of the listed outcomes wins, every leg loses
                bet = BetShape(kind="bounded", p=round(1.0 - params.basket_refund_prob, 6), gain=round(edge * called, 6),
                               loss=round(entry, 6), cost=round(entry, 6), floor=1.0,
                               tail_prob=params.basket_refund_prob)
                why = "the listed outcomes may not be exhaustive (no catch-all outcome or complementary relationship)"
            exit_plan = _basket_exit(edge, regime, states, before, cup_end, params, no_basket=False)
        elif legs:
            _enrich_legs(legs, latest, races, None)
        tradable_now = edge is not None and entry is not None
        opp = Opportunity(
            kind=KIND_ARBITRAGE,
            exchange_id=None,
            market_id=market_id,
            title=title,
            option=None,
            side="yes",
            entry_price=round(entry, 4) if entry is not None else None,
            target_price=1.0,
            stop_price=None,
            prob_win=None if bet is None else round(bet.p, 4),
            edge=edge,
            expected_return=round(er, 6) if er is not None else None,
            horizon_hours=None,
            suggested_shares=0,
            suggested_cost=0.0,
            score=growth_score(bet, edge, BOOK_ARB_CONFIDENCE, entry) if tradable_now else 0.0,
            confidence=BOOK_ARB_CONFIDENCE,
            settles_before_cup_end=before,
            legs=legs,
            unit="sets",
            depth_checked=(max_units is not None) if edge is not None else None,
            fill_price=None,
            idea_id=idea_id_for(KIND_ARBITRAGE, legs=legs) if legs else f"{KIND_ARBITRAGE}:market:{market_id}",
            bet=bet,
            bet_limit=sizing_mod.bet_at_cost(bet, limit_price) if bet is not None and limit_price is not None else None,
            exit_plan=exit_plan, order_type="taker", limit_price=limit_price, levels=levels, max_units=max_units,
            settlement_regime=_regime(regime), factor_delta=0.0,
            called_prob=round(called, 4) if called is not None else None,
        )

        def render(size: Optional[Dict[str, Any]], *, _opp=opp, _legs=legs, _entry=entry, _edge=edge, _er=er,
                   _over=over, _row=row, _n_out=n_out, _before=before, _settle=settle, _bet=bet, _why=why) -> None:
            rationale: List[str] = []
            if _legs:
                rationale.append(f"The engine flags a potential arbitrage, and the live asks of all {len(_legs)} outcomes "
                                 f"({_legs_text(_legs)}) sum to {_px(_entry)}, below 1.00")
            else:
                when = f" ({_ago(now, _num(_row.get('at')))})" if now is not None and _num(_row.get("at")) is not None else ""
                rationale.append(f"The engine flags a potential arbitrage: the best prices across "
                                 f"{str(_n_out) + ' ' if _n_out else 'its '}outcomes sum to {_px(_over)}, below 1.00, at "
                                 f"the last order-book read{when}; not every outcome has a live ask, so the set price is "
                                 "not re-checked")
            if _edge is not None and _entry:
                if size is None:
                    sizing = "the size comes from the sizing policy"
                else:
                    sizing = _sizing_text(size["shares"], size["cap_shares"], "sets", size["cap_pct"], size.get("offered"),
                                          size.get("fill"), size.get("limit"), size.get("book_at"), size.get("now"))
                rationale.append(f"Buying one YES share of every outcome costs {_px(_entry)} and pays 1.00 if exactly one "
                                 f"wins ({_signed(_edge)} per set, {_er:.1%}); {sizing}")
            if _bet is not None:
                if _bet.kind == "riskless":
                    rationale.append(f"Riskless at a 1/0 settlement: {_why}")
                else:
                    rationale.append(f"Not riskless: {_why}, so it is sized as a bounded bet ({_bet.p:.0%} to gain about "
                                     f"{_px(_bet.gain)}/set, {1 - _bet.p:.0%} to lose up to {_px(_bet.loss)}/set)")
                if _opp.exit_plan is not None:
                    rationale.append(_opp.exit_plan.note)
            risks = [
                "Only a potential arbitrage: it pays only if the listed outcomes are exhaustive and mutually exclusive",
                "Every leg must fill at its best ask: a partial fill leaves a one-sided position",
            ]
            if _edge is not None and _entry is not None and (size is None or size.get("offered") is None) \
                    and _opp.max_units is None:
                risks.append("Size not checked against recent order books on every outcome: check the depth on each one, "
                             "walking a thin book can cost more than the set pays")
            risks.append(_settlement_risk(_before, _settle, cup_end))
            _opp.rationale, _opp.risks = rationale, risks

        render(None)
        out.append(_Built(opp, render, {"path": "book", "entry": entry, "edge": edge, "legs": legs,
                                        "legacy_limit": legacy_limit, "over": over}))
    return out


def arbitrage_opportunities(constraints: Optional[Mapping[str, Any]], overround_rows: Sequence[Mapping[str, Any]],
                            balance: float, *, max_position_pct: float = MAX_POSITION_PCT,
                            latest: Optional[Mapping[str, PricePoint]] = None,
                            infos: Optional[Mapping[str, ExchangeInfo]] = None,
                            cup_end: Optional[float] = None, now: Optional[float] = None,
                            races: Optional[Mapping[str, RaceRef]] = None, regime: str = REGIME_UNKNOWN,
                            params: Optional[StrategyParams] = None) -> List[Opportunity]:
    """One idea per engine-reported constraint violation and per multi-outcome book flagged
    ``hasArbitrageOpportunity``. Sized here at the per-idea cap of ``balance`` (the legacy rule; the
    policies size them in ``build_report``: riskless sets at the set cap, bounded ones by Kelly and the cap).

    ``constraints`` is the ``GET /relationships/constraints`` body (``data``) or a
    ``MarketDataBot.scan`` result (``violations``); ``overround_rows`` use ``scan``'s
    ``markets`` row shape (``market_id``, ``market_title``, ``outcomes``, ``overround``,
    ``arbitrage``) or the API's ``hasArbitrageOpportunity``.

    A violation with several corrective trades is a *set*: ``legs`` lists every contract to buy
    with its own price (the live ask, ``1 - YES bid`` for NO, from ``latest`` when known, else the
    engine's ``currentPrice``), ``entry_price`` is the cost of one set, sizes count sets
    (``unit="sets"``) and ``exchange_id`` / ``option`` are None. When the set's settlement payoff
    is known (NO on every member of a mutually exclusive group, YES on every member of a
    complementary one) the edge is ``payoff - cost``; otherwise it is the engine's violation
    amount. ``settles_before_cup_end`` is set when every leg's settlement date is in ``infos``.
    Sets carry a riskless-or-bounded ``bet`` (riskless only under ``resolved_outcomes`` or fast-count
    races) and a basket-style ``exit_plan`` (sell the set when it normalises), never "locked in".

    A flagged multi-outcome book is priced from the live YES asks of its outcomes when every
    one is quoted (set price = their sum, edge = 1 - sum), so the card's set price is the sum
    of the legs it lists; the idea is dropped once that sum reaches 1.00. Without a quote on
    every outcome it falls back to the engine's overround from the last book read, and says so.

    Sizes count only what the order books can fill (``now`` and books on the ``latest``
    points, see ``TrackerStore.latest``): sets are capped where the next full set would cost
    as much as it pays, across every leg's levels. Without a recent book on every leg the
    size is the per-idea cap and the idea is marked ``depth_checked=False``.
    """
    b = _num(balance) or 0.0
    latest = latest or {}
    infos = infos or {}
    p_ = params or StrategyParams()
    out: List[Opportunity] = []
    for built in _arbitrage_build(constraints, overround_rows, latest=latest, infos=infos, cup_end=cup_end, now=now,
                                  races=races or {}, regime=regime, params=p_):
        opp, meta = built.opp, built.meta
        entry, edge, legs = meta["entry"], meta["edge"], meta["legs"]
        limit = meta["legacy_limit"]
        if meta["path"] == "constraint":
            tradable = bool(meta["tradable"])
            cap_shares = int(math.floor(max_position_pct * b / entry + 1e-9)) if tradable and b > 0 else 0
            shares = cap_shares
            offered, books, book_at = _set_depth(legs, latest, now, limit) if tradable and meta["live"] else (None, None, None)
            fill: Optional[float] = None
            if offered is not None and books is not None:
                shares = min(cap_shares, int(math.floor(offered + 1e-9)))
                fill = _set_fill(books, shares)
            built.render({"shares": shares, "cap_shares": cap_shares, "cap_pct": max_position_pct, "offered": offered,
                          "fill": fill, "limit": limit, "book_at": book_at, "now": now})
            if tradable and offered is not None and shares <= 0:
                tradable = False  # the books hold nothing at a price that keeps the gap
            opp.suggested_shares = shares if tradable else 0
            opp.suggested_cost = round(shares * entry, 2) if entry and shares and tradable else 0.0
            if not tradable:
                opp.score = 0.0
            opp.depth_checked = (offered is not None) if tradable else None
            opp.fill_price = fill if tradable else None
        else:
            cap_shares = int(math.floor(max_position_pct * b / entry + 1e-9)) if entry and edge is not None and b > 0 else 0
            shares = cap_shares
            offered, books, book_at = _set_depth(legs, latest, now, limit) if legs and edge is not None else (None, None, None)
            fill = None
            if offered is not None and books is not None:
                shares = min(cap_shares, int(math.floor(offered + 1e-9)))
                fill = _set_fill(books, shares)
            tradable = edge is not None and entry is not None and shares > 0
            built.render({"shares": shares, "cap_shares": cap_shares, "cap_pct": max_position_pct, "offered": offered,
                          "fill": fill, "limit": limit, "book_at": book_at, "now": now})
            opp.suggested_shares = shares if tradable else 0
            opp.suggested_cost = round(shares * entry, 2) if tradable and entry else 0.0
            if not (tradable or offered is None):
                opp.score = 0.0
            opp.depth_checked = (offered is not None) if edge is not None else None
            opp.fill_price = fill if tradable else None
        out.append(opp)
    return out


# --------------------------------------------------------------------------- value


def value_opportunity(exchange_id: str, fv: FairValue, point: Optional[PricePoint], info: Optional[ExchangeInfo],
                      race: Optional[RaceRef], inputs: StrategyInputs, params: StrategyParams) -> Optional[Opportunity]:
    """Buy the side the outside fair value says is cheap, net of the spread and the exit cost, with a
    regime-aware edge that must clear ``min_value_edge`` PLUS the fair value's own uncertainty (§5.2). None
    when no side clears it, the fair value is not usable / too old / suspect / jumping, the Cup moved away
    from it since it was fetched, or the quote is one-sided."""
    now = inputs.now
    used = _usable_fv(fv, now, params)
    if used is None:
        return None
    quote = _two_sided(point)
    if quote is None or point is None:
        return None
    b, a = quote
    v = float(used.value)  # type: ignore[arg-type]
    half = (a - b) / 2.0
    m = (a + b) / 2.0
    external = used.source != "manual"
    if external:
        prev = _num(used.prev_value)
        if prev is not None and abs(v - prev) > params.fv_jump_fraction * abs(v - m) + 1e-12:
            return None  # the outside price is still moving: wait for it to settle
        as_of = _num(used.as_of)
        mids = inputs.recent_mids.get(str(exchange_id)) if inputs.recent_mids else None
        m_then: Optional[float] = None
        then_ts: Optional[float] = None
        for item in mids or ():
            try:
                ts, mid_then = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if as_of is not None and ts <= as_of + 1e-9 and (then_ts is None or ts >= then_ts):
                m_then, then_ts = mid_then, ts  # the newest Cup mid at or before the outside price was read
        if m_then is not None and abs(m - m_then) > params.cup_move_fraction * abs(v - m) + 1e-12 \
                and abs(v - m) > abs(v - m_then) + 1e-12:
            return None  # the Cup moved away from fair value since it was read: it may reflect newer news
    regime = _regime(inputs.settlement_regime)
    before, settle_ts = _settles_before(info.settlement_date if info is not None else None, inputs.cup_end,
                                        params.settle_margin_s)
    called = called_prob(race, max(v, 1.0 - v), params)
    unc = _num(used.uncertainty)
    near = used.match_kind == "NEAR"
    best: Optional[Dict[str, Any]] = None
    for side in ("yes", "no"):
        entry = a if side == "yes" else round(1.0 - b, 6)
        q_raw = v if side == "yes" else 1.0 - v
        contract_mid = m if side == "yes" else 1.0 - m
        longshot = q_raw < 0.15
        q = q_raw * (1.0 - params.longshot_shrink) if longshot else q_raw
        ev, regime_text, detail = regime_ev(q, entry, contract_mid, regime, called, params, settles_before_end=before)
        if best is None or ev > best["ev"]:
            best = {"side": side, "entry": entry, "q_raw": q_raw, "q": q, "mid": contract_mid, "ev": ev,
                    "text": regime_text, "detail": detail, "longshot": longshot}
    assert best is not None
    side, entry, q, q_raw, ev = best["side"], best["entry"], best["q"], best["q_raw"], best["ev"]
    required = (params.min_value_edge * (params.longshot_min_edge_mult if best["longshot"] else 1.0)
                + (unc or 0.0) + (params.near_extra_edge if near else 0.0))
    target_bid = depth.floor_tick(q - half - params.value_exit_buffer)
    exit_limit = round(target_bid - TICK, 6)
    convergence_gain = exit_limit - entry
    if convergence_gain < required - 1e-9 or ev < required - 1e-9:
        return None
    limit = value_limit(q, entry, best["mid"], exit_limit, regime, called, required, params, settles_before_end=before)
    if limit is None:
        return None
    resolution_gap = q - entry
    mc = _num(used.match_confidence)
    mc = 1.0 if mc is None or mc <= 0 else min(1.0, mc)
    if used.source in ("manual", "demo"):
        base_conf = FLAT_FV_CONFIDENCE
    else:
        base_conf = VALUE_CONFIDENCE.get(str(used.confidence), VALUE_CONFIDENCE["low"])
    confidence = round(base_conf * mc, 4)
    bet = BetShape(kind="binary", p=round(min(1.0, entry + ev), 6), gain=round(1.0 - entry, 6), loss=entry, cost=entry)
    levels_all, book_at = _book_levels(side, point, now, entry, params.book_max_age_s)
    cut = _cut(levels_all, limit) if levels_all is not None else []
    max_units = float(sum(q_ for _, q_ in cut)) if levels_all is not None else None
    name = side.upper()
    title, option = _title(info, info.market_id if info is not None else "")
    states = [race.state if race is not None else None]
    exit_plan = ExitPlan(kind="value", target_bid=target_bid, dynamic_fv_target=True, exit_buffer=params.value_exit_buffer,
                         hold_to_resolution=regime == REGIME_RESOLVED,
                         exit_before_ts=_exit_before(regime, states, before, inputs.cup_end, params),
                         note=(f"Sell when the {name} bid reaches {target_bid:.3f} (fair value {q:.2f} less half the "
                               f"spread and {params.value_exit_buffer:g}); otherwise hold"))
    age = max(0.0, now - (_num(used.as_of) or now))
    fv_text = (f"Outside fair value for YES: {v:.3f} from {_source_label(used.source)} ({used.confidence} confidence"
               + (f", uncertainty {unc:.3f}" if unc is not None else "") + f", {age:.0f} s old); the Cup quotes "
               f"{_px(b)} bid / {_px(a)} ask")
    if not external:
        fv_text = (f"Your fair value for YES: {v:.3f} (uncertainty {unc if unc is not None else 0.0:.3f}); the Cup quotes "
                   f"{_px(b)} bid / {_px(a)} ask")
    how = "the YES ask" if side == "yes" else f"1 - YES bid {_px(b)}"
    parts = [f"minimum {params.min_value_edge:g}"]
    if best["longshot"]:
        parts[0] += f" x{params.longshot_min_edge_mult:g} (longshot)"
    if unc:
        parts.append(f"uncertainty {unc:.3f}")
    if near:
        parts.append(f"near-match extra {params.near_extra_edge:g}")
    rationale = [
        fv_text,
        f"Buy {name} at {_px(entry)} ({how}): {resolution_gap:+.3f}/share below fair value {q:.3f} if held to a 1/0 "
        f"result; this idea needs {required:.3f} ({' + '.join(parts)})",
    ]
    if best["longshot"]:
        rationale.append(f"Longshot: fair value {q_raw:.3f} is shrunk by {params.longshot_shrink:.0%} to {q:.3f}, since "
                         "longshots tend to be overpriced (the favourite-longshot bias, Page & Clemen)")
    rationale.append(f"Exit target: sell when the {name} bid reaches {target_bid:.3f} (fair value {q:.3f} less half the "
                     f"{_px(round(a - b, 4))} spread and {params.value_exit_buffer:g}); the exit order at "
                     f"{exit_limit:.3f} banks {convergence_gain:+.3f}/share")
    rationale.append(best["text"])
    rationale.append(f"Limit {limit:.3f}: every share up to it still clears the required edge (edge at the limit "
                     f"{ev - (limit - entry):+.4f}/share)")
    if levels_all is not None:
        rationale.append(f"Book ({_ago(now, book_at)}): {max_units:,.0f} {name} shares on offer up to {limit:.3f}")
    else:
        rationale.append("No recent order book: the depth at the limit is unknown")
    risks = [
        "Outside prices can be wrong: they carry their own biases and fees, and resolve differently (Polymarket on "
        "media calls or certification, Kalshi on swearing-in)",
        "Faster bots close gaps first, and a gap that closes because the outside price moved means the fair value "
        "was stale",
        "Under a 5-hour VWAP closeout an uncalled race closes near a coin flip, so a position still open then can lose "
        "even if the fair value was right",
    ]
    if before is True:
        risks.append(SETTLEMENT_RISK)
    if near:
        risks.append("Near match: the outside market's settlement wording differs; traded only because you enabled it, "
                     "with extra edge required")
    horizon = _horizon(before, settle_ts, now, inputs.cup_end)
    p_yes = v
    fair_contract = v if side == "yes" else 1.0 - v
    return Opportunity(
        kind=KIND_VALUE, exchange_id=str(exchange_id), market_id=info.market_id if info is not None else "",
        title=title, option=option, side=side, entry_price=round(entry, 4), target_price=target_bid, stop_price=None,
        prob_win=round(q, 4), edge=round(ev, 6), expected_return=round(ev / entry, 6), horizon_hours=horizon,
        suggested_shares=0, suggested_cost=0.0, score=growth_score(bet, round(ev, 6), confidence, entry), confidence=confidence,
        rationale=rationale, risks=risks, settles_before_cup_end=before, depth_checked=levels_all is not None,
        idea_id=idea_id_for(KIND_VALUE, exchange_id=str(exchange_id), side=side),
        race_key=race.race_key if race is not None else None, bet=bet, bet_limit=sizing_mod.bet_at_cost(bet, limit),
        exit_plan=exit_plan, order_type="taker", limit_price=limit, levels=cut, max_units=max_units,
        fair_value=round(fair_contract, 4), fair_source=used.source, settlement_regime=regime,
        fv_uncertainty=unc, factor_delta=factor_delta(race, side, p_yes, params), called_prob=round(called, 4),
        priced_from=str(getattr(point, "source", "tick") or "tick"),
    )


# --------------------------------------------------------------------------- baskets


@dataclass
class RaceGroup:
    """Mutually exclusive outcomes of one race: separate binary markets sharing a race key, or the
    outcomes of one multi-outcome market."""

    key: str  # race_key, or "market:<id>"
    exchange_ids: List[str]
    exhaustive: bool  # exactly one member must win (a YES basket is riskless only then)
    source: str  # "race" | "market" | "relationship"
    market_ids: List[str] = field(default_factory=list)
    why_exhaustive: str = ""


def _complementary_sets(constraints: Optional[Mapping[str, Any]]) -> List[Set[str]]:
    out: List[Set[str]] = []
    if not constraints:
        return out
    for rel in list(constraints.get("data") or constraints.get("violations") or []):
        if not isinstance(rel, Mapping):
            continue
        kind = str(rel.get("relationshipType") or rel.get("type") or "").strip().lower()
        if kind != "complementary":
            continue
        members = set(_members(rel))
        if len(members) >= 2:
            out.append(members)
    return out


def group_races(infos: Mapping[str, ExchangeInfo], races: Mapping[str, RaceRef],
                constraints: Optional[Mapping[str, Any]] = None) -> List[RaceGroup]:
    """Every mutually exclusive group with >= 2 open members (§5.3.1), deduplicated by member set."""
    infos = infos or {}
    races = races or {}
    complementary = _complementary_sets(constraints)
    groups: List[RaceGroup] = []
    seen: Set[frozenset] = set()
    by_market: Dict[str, List[str]] = {}
    for eid, info in infos.items():
        by_market.setdefault(str(info.market_id), []).append(str(eid))
    for market_id in sorted(by_market, key=_eid_key):
        eids = sorted(set(by_market[market_id]), key=_eid_key)
        if len(eids) < 2:
            continue
        members = set(eids)
        catch_all = [e for e in eids if infos[e].option and CATCH_ALL_RE.search(str(infos[e].option))]
        rel = next((c for c in complementary if c == members), None)
        if catch_all:
            why = f"the option '{infos[catch_all[0]].option}' catches every other result"
        elif rel is not None:
            why = "an engine complementary relationship covers every outcome"
        else:
            why = ""
        groups.append(RaceGroup(key=f"market:{market_id}", exchange_ids=eids, exhaustive=bool(why), source="market",
                                market_ids=[str(market_id)], why_exhaustive=why))
        seen.add(frozenset(eids))
    by_race: Dict[str, List[str]] = {}
    for eid in infos:
        race = races.get(str(eid))
        if race is not None and race.race_key:
            by_race.setdefault(race.race_key, []).append(str(eid))
    for key in sorted(by_race):
        eids = sorted(set(by_race[key]), key=_eid_key)
        parties = [races[e].party for e in eids]
        if len(eids) < 2 or len(set(parties)) != len(parties):
            continue  # a race group needs one outcome per party
        if frozenset(eids) in seen:
            continue
        rel = next((c for c in complementary if c == set(eids)), None)
        why = "an engine complementary relationship covers every outcome" if rel is not None else ""
        market_ids = sorted({str(infos[e].market_id) for e in eids}, key=_eid_key)
        groups.append(RaceGroup(key=key, exchange_ids=eids, exhaustive=bool(why), source="race", market_ids=market_ids,
                                why_exhaustive=why))
        seen.add(frozenset(eids))
    return groups


def basket_opportunities(inputs: StrategyInputs, params: StrategyParams) -> List[Opportunity]:
    """NO baskets (YES bids sum > 1 + basket_min_edge) on every group, YES baskets (YES asks sum <
    1 - basket_min_edge) only on exhaustive groups; every leg needs a TICK quote from the same bulk snapshot
    (|dts| <= same_snapshot_s, never candles); bet "riskless" only under resolved_outcomes or when every
    leg's race is a fast count, else "bounded"; depth-limited, with an early-exit plan (§5.3)."""
    now = inputs.now
    infos = inputs.infos or {}
    races = inputs.races or {}
    latest = inputs.latest or {}
    regime = _regime(inputs.settlement_regime)
    out: List[Opportunity] = []
    for group in group_races(infos, races, inputs.constraints):
        points = [latest.get(e) for e in group.exchange_ids]
        if any(p is None for p in points):
            continue
        quotes = [_two_sided(p) for p in points]
        if any(q is None for q in quotes):
            continue
        if any(str(getattr(p, "source", "tick")) != "tick" for p in points):
            continue  # candle closes from different times can sum to anything: never a set idea
        stamps = [_num(p.ts) for p in points]  # type: ignore[union-attr]
        if any(t is None for t in stamps) or max(stamps) - min(stamps) > params.same_snapshot_s + 1e-9:  # type: ignore[type-var,operator]
            continue
        n = len(group.exchange_ids)
        bids = [q[0] for q in quotes]  # type: ignore[index]
        asks = [q[1] for q in quotes]  # type: ignore[index]
        sum_b, sum_a = sum(bids), sum(asks)
        candidates = []
        if sum_b - 1.0 >= params.basket_min_edge - 1e-9:
            candidates.append(("no", [round(1.0 - x, 6) for x in bids], float(n - 1), sum_b - 1.0))
        if group.exhaustive and 1.0 - sum_a >= params.basket_min_edge - 1e-9:
            candidates.append(("yes", [round(x, 6) for x in asks], 1.0, 1.0 - sum_a))
        for side, costs, floor, edge in candidates:
            opp = _basket_idea(group, side, costs, floor, round(edge, 6), bids, asks, points, inputs, params, regime)  # type: ignore[arg-type]
            if opp is not None:
                out.append(opp)
    return out


def _basket_idea(group: RaceGroup, side: str, costs: List[float], floor: float, edge: float, bids: List[float],
                 asks: List[float], points: Sequence[PricePoint], inputs: StrategyInputs, params: StrategyParams,
                 regime: str) -> Optional[Opportunity]:
    now = inputs.now
    infos, races, latest = inputs.infos or {}, inputs.races or {}, inputs.latest or {}
    n = len(costs)
    total = round(sum(costs), 6)
    legs: List[Dict[str, Any]] = []
    for eid, cost in zip(group.exchange_ids, costs):
        info = infos.get(eid)
        legs.append({"exchange_id": eid, "market_id": info.market_id if info else None,
                     "title": info.market_title if info else None, "option": info.option if info else None,
                     "side": side, "price": round(cost, 4)})
    _enrich_legs(legs, latest, races, edge)
    limit_price = round(sum(leg["limit"] for leg in legs), 6)
    levels, max_units = _set_book_levels(legs, latest, now, limit_price, params)
    bet, called, why = _set_bet(legs, total, floor, edge, regime, races, latest, params)
    before, latest_settle = _legs_settle_before(group.exchange_ids, infos, inputs.cup_end, params.settle_margin_s)
    states = [races[e].state for e in group.exchange_ids if e in races]
    exit_plan = _basket_exit(edge, regime, states, before, inputs.cup_end, params, no_basket=side == "no")
    settle_at = latest_settle if latest_settle is not None else inputs.cup_end
    days = max(1.0 / 24.0, (settle_at - now) / 86400.0) if settle_at is not None else 1.0 / 24.0
    ppcd = edge / (total * days) if total > 0 else None
    exhaustive_no = side == "no" and group.exhaustive
    if group.source == "market":
        confidence = BASKET_CONF_EXHAUSTIVE if group.exhaustive else BASKET_CONF_RACE
    else:
        confidence = BASKET_CONF_RACE
    titles = list(dict.fromkeys(str(leg["title"]) for leg in legs if leg.get("title")))
    title = " / ".join(titles[:3]) if titles else group.key
    if group.source == "race":
        first = next((races[e] for e in group.exchange_ids if e in races), None)
        label = _race_words(first, group.key)
    else:
        label = f"\"{titles[0]}\"" if titles else group.key
    liq = round(sum((1.0 - a) for a in asks), 4) if side == "no" else round(sum(bids), 4)
    market_id = str(legs[0].get("market_id") or (group.market_ids[0] if group.market_ids else ""))
    race_keys = {leg.get("race_key") for leg in legs}
    race_key = next(iter(race_keys)) if len(race_keys) == 1 and None not in race_keys else (
        group.key if group.source == "race" else None)
    rationale: List[str] = []
    if side == "no":
        pays = "exactly" if group.exhaustive else "at least"
        rationale.append(f"The YES bids of the {n} outcomes of {label} sum to {sum(bids):.3f}, above 1.00: buying NO on "
                         f"every leg ({_legs_text(legs)}) costs {total:.3f} per set and pays {pays} {floor:.2f} at a 1/0 "
                         "settlement")
    else:
        rationale.append(f"The YES asks of the {n} outcomes of {label} sum to {sum(asks):.3f}, below 1.00, and exactly one "
                         f"of them must win ({group.why_exhaustive}): buying YES on every leg ({_legs_text(legs)}) costs "
                         f"{total:.3f} per set and pays 1.00")
    rationale.append(f"Profit {_signed(edge)} per set ({edge * 100:.1f} SUSQies per 100 sets), "
                     + (f"{ppcd:.2%} of the capital per day until settlement ({days:.1f} days)" if ppcd is not None else ""))
    if side == "no" and not exhaustive_no:
        rationale.append(f"It pays even if a third candidate wins (then all {n} NO shares pay)")
    if bet.kind == "riskless":
        rationale.append(f"Riskless at a 1/0 settlement: {why}")
    else:
        rationale.append(f"Not riskless: {why}; a refund or ruling on one leg, or an uncalled race cashed at its VWAP "
                         f"under a closeout, breaks the floor, so it is sized as a bounded bet ({bet.p:.0%} to gain about "
                         f"{_px(bet.gain)}/set, counting the uncalled branch as no gain; {1 - bet.p:.0%} to lose up to "
                         f"{_px(bet.loss)}/set)")
    limits_text = " / ".join(f"{leg['limit']:.3f}" for leg in legs)
    rationale.append(f"Leg limits {limits_text}: each keeps half of its share of the edge, so the set limit is "
                     f"{limit_price:.3f}")
    rationale.append(exit_plan.note)
    rationale.append(f"Right after entry the set is worth less at liquidation than its cost (you would sell into the "
                     f"bids: about {liq:.2f} per set against {total:.2f} here); that is the spread, not a loss of the "
                     "locked edge")
    if max_units is not None:
        rationale.append(f"The books offer {max_units:,.0f} full sets up to {limit_price:.3f}")
    else:
        rationale.append("Not every leg has a recent order book: the depth is unknown, so a simulated entry waits for "
                         "fresh books of every leg")
    risks = [
        "Legging: one leg can fill while another moves away, leaving a one-sided position",
        "Capital stays locked until the exit or the settlement",
        "Under a VWAP closeout each leg is cashed separately at its own VWAP: the set is not locked in",
        "A tournament ruling or a REFUND on one leg can turn the set into a loss",
    ]
    if before is True:
        risks.append(SETTLEMENT_RISK)
    return Opportunity(
        kind=KIND_BASKET, exchange_id=None, market_id=market_id, title=title, option=None, side=side,
        entry_price=round(total, 4), target_price=round(floor, 4) if group.exhaustive else None, stop_price=None,
        prob_win=round(bet.p, 4), edge=round(edge, 6), expected_return=round(edge / total, 6) if total > 0 else None,
        horizon_hours=round(days * 24.0, 2), suggested_shares=0, suggested_cost=0.0,
        score=growth_score(bet, edge, confidence, total), confidence=confidence, rationale=rationale, risks=risks,
        settles_before_cup_end=before, legs=legs, unit="sets", depth_checked=max_units is not None,
        idea_id=idea_id_for(KIND_BASKET, legs=legs), race_key=race_key, bet=bet,
        bet_limit=sizing_mod.bet_at_cost(bet, limit_price), exit_plan=exit_plan, order_type="taker",
        limit_price=limit_price, levels=levels, max_units=max_units, settlement_regime=regime,
        profit_per_capital_day=round(ppcd, 6) if ppcd is not None else None, factor_delta=0.0,
        called_prob=round(called, 4), fv_uncertainty=0.0, priced_from="tick",
    )


# --------------------------------------------------------------------------- liquidity holes


def _open_surge_ids(surges: Sequence[Surge]) -> Set[str]:
    return {str(s.exchange_id) for s in surges or () if s.status == SURGE_OPEN}


def _hole_quote(eid: str, inputs: StrategyInputs, params: StrategyParams,
                surging: Set[str]) -> Optional[Tuple[str, float, float, float]]:
    """(side, YES bid, YES ask, favourite mid) when the quote rules pass (spread, mid, no open surge)."""
    if eid in surging:
        return None
    quote = _two_sided((inputs.latest or {}).get(eid))
    if quote is None:
        return None
    bid, ask = quote
    if ask - bid > params.hole_max_spread + 1e-9:
        return None
    mid = (bid + ask) / 2.0
    if mid >= params.hole_min_mid - 1e-9:
        return "yes", bid, ask, mid
    if 1.0 - mid >= params.hole_min_mid - 1e-9:
        return "no", bid, ask, 1.0 - mid
    return None


def _book_at(point: Optional[PricePoint]) -> Optional[float]:
    book = getattr(point, "book", None) if point is not None else None
    return _num(book.get("at")) if isinstance(book, Mapping) else None


def hole_candidates(inputs: StrategyInputs, params: Optional[StrategyParams] = None) -> List[str]:
    """Outcomes that would qualify for a hole order if a recent book confirmed the touch depth
    (two-sided quote, spread <= hole_max_spread, favourite mid >= hole_min_mid, no open surge) but
    have no stored book younger than book_max_age_s: the paper runner reads their books (priority 4).
    At most 2 x hole_max_exchanges, tightest spread first, then exchange id."""
    p_ = params or inputs.params or StrategyParams()
    surging = _open_surge_ids(inputs.surges)
    rows: List[Tuple[float, Tuple[int, int, str], str]] = []
    for eid in inputs.infos or {}:
        eid = str(eid)
        q = _hole_quote(eid, inputs, p_, surging)
        if q is None:
            continue
        at = _book_at((inputs.latest or {}).get(eid))
        if at is not None and inputs.now - at <= p_.book_max_age_s + 1e-9:
            continue  # a recent book exists: the idea is decided on it
        rows.append((round(q[2] - q[1], 6), _eid_key(eid), eid))
    rows.sort()
    return [eid for _, _, eid in rows[: max(0, 2 * int(p_.hole_max_exchanges))]]


def hole_opportunities(inputs: StrategyInputs, params: StrategyParams) -> List[Opportunity]:
    """Resting maker bids far below liquid favourites to catch liquidity holes (§5.4). An outcome in
    ``inputs.resting_exchanges`` keeps its idea without a fresh book while its last stored book is at most
    2 x book_max_age_s old and the bulk quote still meets the spread and mid rules."""
    now = inputs.now
    infos, latest, races = inputs.infos or {}, inputs.latest or {}, inputs.races or {}
    regime = _regime(inputs.settlement_regime)
    surging = _open_surge_ids(inputs.surges)
    resting = {str(e) for e in inputs.resting_exchanges or ()}
    ranked: List[Tuple[float, Tuple[int, int, str], Opportunity]] = []
    for eid in infos:
        eid = str(eid)
        q = _hole_quote(eid, inputs, params, surging)
        if q is None:
            continue
        side, bid, ask, fav_mid = q
        point = latest.get(eid)
        book = getattr(point, "book", None) if point is not None else None
        at = _book_at(point)
        if at is None:
            continue
        max_age = params.book_max_age_s * (2.0 if eid in resting else 1.0)
        if now - at > max_age + 1e-9:
            continue
        bids, asks = depth.book_sides(book)
        touch_side = bids if side == "yes" else asks
        if not touch_side:
            continue
        best = max(touch_side, key=lambda lv: lv[0]) if side == "yes" else min(touch_side, key=lambda lv: lv[0])
        touch_qty = sum(qq for p_, qq in touch_side if abs(p_ - best[0]) <= 1e-9)
        if touch_qty < params.hole_min_touch_qty - 1e-9:
            continue
        used = _usable_fv((inputs.fair_values or {}).get(eid), now, params)
        info = infos[eid]
        race = races.get(eid)
        if used is not None:
            v = float(used.value)  # type: ignore[arg-type]
            ref = v if side == "yes" else 1.0 - v
            ref_label = f"the outside fair value ({_source_label(used.source)})"
        else:
            ref = fav_mid
            ref_label = "the mid"
        contract_bid = bid if side == "yes" else round(1.0 - ask, 6)
        price = depth.floor_tick(ref * (1.0 - params.hole_depth_fraction))
        if price > contract_bid - params.hole_min_ticks_below * TICK + 1e-9 or price < 0.05 - 1e-9:
            continue
        price = round(depth.clamp_price(price), 3)
        target_bid = depth.floor_tick(max(price + 0.05, ref - params.hole_exit_buffer))
        gain = round(target_bid - price, 6)
        if gain <= _EPS:
            continue
        edge = round(params.hole_fill_prob * gain, 6)
        bet = BetShape(kind="fixed", p=params.hole_fill_prob, gain=gain, loss=price, cost=price,
                       cap_pct=params.hole_max_stake_pct)
        before, _settle = _settles_before(info.settlement_date, inputs.cup_end, params.settle_margin_s)
        name = side.upper()
        ticks_under = int(round((contract_bid - price) / TICK))
        hours = params.hole_time_stop_s / 3600.0
        exit_plan = ExitPlan(kind="hole", target_bid=target_bid, time_stop_after_fill_s=params.hole_time_stop_s,
                             exit_before_ts=_exit_before(regime, [race.state if race else None], before, inputs.cup_end,
                                                         params),
                             note=f"If it fills, sell when the bid recovers to {target_bid:.3f}, or after {hours:g} h")
        title, option = _title(info, info.market_id)
        spread = ask - bid
        rationale = [
            f"{name} is a liquid favourite at {fav_mid:.3f} (spread {_px(round(spread, 4))}, {touch_qty:,.0f} shares at "
            "the best bid): a resting buy far below can catch a careless market order that sweeps a thin book (a "
            "liquidity hole)",
            f"Rest a buy for {name} at {price:.3f}, {ticks_under} ticks under the {contract_bid:.3f} bid and "
            f"{params.hole_depth_fraction:.0%} under {ref_label} {ref:.3f}; it expires in "
            f"{params.hole_ttl_s / 3600.0:g} h",
            "Most such orders never fill; a resting order this far under the bid usually has an unknown queue, so it "
            "fills only when prices trade through it",
            exit_plan.note,
            "Passive liquidity provision: each order is meant to fill, and is small",
        ]
        if eid in resting and now - at > params.book_max_age_s:
            rationale.append(f"Kept alive by the resting order: its last book is {_ago(now, at)} (up to "
                             f"{2 * params.book_max_age_s / 60:.0f} min is accepted)")
        risks = [
            "Adverse selection: it fills exactly when real news hits, and the price may not come back",
            "The house can sweep its own quotes: a fill may come with the book moving away",
            "Cancel it before scheduled news; it is cancelled automatically when a surge opens on this outcome, since "
            "the idea then disappears",
        ]
        p_yes = float(used.value) if used is not None else (bid + ask) / 2.0  # type: ignore[arg-type]
        confidence = HOLE_CONFIDENCE
        opp = Opportunity(
            kind=KIND_HOLE, exchange_id=eid, market_id=info.market_id, title=title, option=option, side=side,
            entry_price=price, target_price=target_bid, stop_price=None, prob_win=params.hole_fill_prob, edge=edge,
            expected_return=round(edge / price, 6), horizon_hours=round(params.hole_ttl_s / 3600.0, 2),
            suggested_shares=0, suggested_cost=0.0, score=growth_score(bet, edge, confidence, price),
            confidence=confidence, rationale=rationale, risks=risks, settles_before_cup_end=before, depth_checked=True,
            idea_id=idea_id_for(KIND_HOLE, exchange_id=eid, side=side), race_key=race.race_key if race else None,
            bet=bet, bet_limit=sizing_mod.bet_at_cost(bet, price), exit_plan=exit_plan, order_type="maker",
            limit_price=price, expires_at=round(now + params.hole_ttl_s, 3), max_units=float(params.hole_max_shares),
            levels=[], fair_value=round(ref, 4) if used is not None else None,
            fair_source=used.source if used is not None else None, settlement_regime=regime,
            fv_uncertainty=_num(used.uncertainty) if used is not None else None,
            factor_delta=factor_delta(race, side, p_yes, params),
            priced_from=str(getattr(point, "source", "tick") or "tick"),
        )
        ranked.append((-(touch_qty / max(spread, TICK)), _eid_key(eid), opp))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [opp for _, _, opp in ranked[: max(0, int(params.hole_max_exchanges))]]


# --------------------------------------------------------------------------- conflicts


def _is_reversion(surge: Surge, other: Surge) -> bool:
    """``surge`` is ``other`` giving its move back: the opposite direction, starting after
    ``other`` started (within its window + 6 h of its end) and ending inside its start -> peak
    range. Fading it would bet that the debunked move comes back."""
    if other is surge or other.exchange_id != surge.exchange_id or other.direction == surge.direction:
        return False
    if surge.start_ts < other.start_ts - 1e-6:
        return False
    if surge.start_ts > other.end_ts + max(_num(other.window_s) or 0.0, 0.0) + REVERSION_LINK_S:
        return False
    if other.direction == "down":
        return other.peak_price - _EPS <= surge.end_price <= other.start_price + _EPS
    return other.start_price - _EPS <= surge.end_price <= other.peak_price + _EPS


def _leg_pairs(opp: Opportunity) -> List[Tuple[str, str]]:
    if opp.legs:
        return [(str(leg.get("exchange_id")), str(leg.get("side") or "")) for leg in opp.legs
                if leg.get("exchange_id") is not None]
    if opp.exchange_id is not None:
        return [(str(opp.exchange_id), str(opp.side))]
    return []


def _arb_path(opp: Opportunity) -> Optional[str]:
    """"constraint" (an engine-reported violation) | "book" (a flagged multi-outcome book) | None."""
    if opp.kind != KIND_ARBITRAGE or not opp.rationale:
        return None
    first = opp.rationale[0]
    if first.startswith("Engine-reported"):
        return "constraint"
    if first.startswith("The engine flags"):
        return "book"
    return None


def _tradable(opp: Opportunity) -> bool:
    return opp.kind != KIND_WATCH and (opp.bet is not None or opp.suggested_shares > 0)


def _resolve_conflicts(opps: List[Opportunity], infos: Mapping[str, ExchangeInfo],
                       latest: Mapping[str, PricePoint], races: Optional[Mapping[str, RaceRef]] = None) -> List[Opportunity]:
    """Ideas must not contradict each other (``opps`` best score first).

    * An engine-violation arbitrage buying exactly the legs and sides of a basket: only the basket
      (the same trade, and the basket kind is replayable); a YES basket identical to a flagged
      multi-outcome book's arbitrage: only the arbitrage (it was there first).
    * Two value ideas on different legs of one race that bet on different winners (YES on two legs, or
      NO on two legs): only the higher-scored one (an unbalanced partial set, D38).
    * Two value ideas on different legs of one race that bet on the SAME winner (YES on one leg and NO on
      another: in a two-way race YES-D and NO-R are one bet that D wins, and in any race both lose when the NO
      leg wins): only the higher-scored one. Kept together they were sized as two Kelly bets (2x the stated
      Kelly fraction on one result) and took two of the chaser's slots.
    * A tradable arbitrage or basket buys one side of each leg; a carry on the other side of the same
      outcome is dropped (holding both pays exactly 1.00 for more than 1.00), and a fade or
      watch on it is flagged.
    * One net position per exchange: a later idea buying the opposite contract of an earlier idea's
      leg on the same exchange is flagged.
    * A carry buying NO on one outcome of a multi-outcome market is flagged when buying YES on
      every other outcome costs less (it pays the same: 1.00 unless this outcome wins).
    """
    races = races or {}
    basket_sets = {frozenset(_leg_pairs(o)) for o in opps if o.kind == KIND_BASKET}
    book_arb_sets = {frozenset(_leg_pairs(o)) for o in opps if _arb_path(o) == "book" and o.legs}
    kept: List[Opportunity] = []
    for opp in opps:
        legs = frozenset(_leg_pairs(opp))
        if _arb_path(opp) == "constraint" and legs and legs in basket_sets:
            continue  # the basket carries the same trade
        if opp.kind == KIND_BASKET and opp.side == "yes" and legs in book_arb_sets:
            continue  # the flagged book's arbitrage is the same trade
        kept.append(opp)
    # value ideas on other legs of one race: different winners (same side, D38) or the same winner (YES on one leg,
    # NO on another): either way the better-scored idea is the race's one value bet
    out: List[Opportunity] = []
    value_bets: Dict[str, List[Opportunity]] = {}
    for opp in kept:
        if opp.kind == KIND_VALUE and opp.race_key and opp.exchange_id is not None:
            clash = next((o for o in value_bets.get(opp.race_key, []) if o.exchange_id != opp.exchange_id), None)
            if clash is not None:
                continue
            value_bets.setdefault(opp.race_key, []).append(opp)
        out.append(opp)
    # arbitrage / basket legs against carries, fades and watches
    legs_map: Dict[str, Tuple[str, str]] = {}
    for opp in out:
        if opp.kind not in (KIND_ARBITRAGE, KIND_BASKET) or not _tradable(opp):
            continue
        for eid, side in _leg_pairs(opp):
            legs_map.setdefault(eid, (side, opp.kind))
    by_market: Dict[str, List[str]] = {}
    for info in infos.values():
        by_market.setdefault(str(info.market_id), []).append(str(info.exchange_id))
    result: List[Opportunity] = []
    taken: Dict[str, Tuple[str, Opportunity]] = {}
    for opp in out:
        eid = str(opp.exchange_id) if opp.exchange_id is not None else None
        flagged = False
        hit = legs_map.get(eid) if eid is not None and opp.kind in (KIND_CARRY, KIND_FADE, KIND_WATCH) else None
        if hit and hit[0] != opp.side:
            if opp.kind == KIND_CARRY:
                continue
            opp.risks.insert(0, f"Conflicts with the {hit[1]} idea, which buys {hit[0].upper()} on this outcome: "
                                f"holding both {hit[0].upper()} and {opp.side.upper()} pays exactly 1.00 for more than 1.00")
            flagged = True
        if opp.kind != KIND_WATCH and _tradable(opp):
            for leg_eid, side in _leg_pairs(opp):
                prior = taken.get(leg_eid)
                if prior is not None and prior[0] != side and not flagged:
                    other = prior[1]
                    opp.risks.insert(0, f"Conflicts with the {other.kind} idea {other.idea_id}, which buys "
                                        f"{prior[0].upper()} on exchange {leg_eid}: one net position per exchange, so only "
                                        "one of them can be held")
                    flagged = True
            for leg_eid, side in _leg_pairs(opp):
                taken.setdefault(leg_eid, (side, opp))
        if opp.kind == KIND_CARRY and opp.side == "no" and eid is not None and opp.entry_price is not None:
            others = [x for x in by_market.get(str(opp.market_id), []) if x != eid]
            asks = [_quote_cost("yes", latest.get(x)) for x in others]
            if others and all(a is not None for a in asks):
                cheaper = round(sum(asks), 6)  # type: ignore[arg-type]
                if cheaper < opp.entry_price - _EPS:
                    opp.risks.insert(0, f"Cheaper for the same payoff: YES on every other outcome of this market costs "
                                        f"{_px(cheaper)} in total (vs NO at {_px(opp.entry_price)}), and also pays 1.00 "
                                        "unless this outcome wins")
        result.append(opp)
    return result


# --------------------------------------------------------------------------- signals


def _newest_surges(surges: Sequence[Surge]) -> Dict[str, Surge]:
    every = list(surges or ())
    newest: Dict[str, Surge] = {}
    for s in every:
        if s.status != SURGE_OPEN:
            continue
        if any(_is_reversion(s, other) for other in every):
            continue  # the move giving back an earlier surge: not a new surge to fade
        prev = newest.get(s.exchange_id)
        if prev is None or (s.detected_at, s.end_ts) > (prev.detected_at, prev.end_ts):
            newest[s.exchange_id] = s
    return newest


def _signal_key(opp: Opportunity) -> Tuple[float, str]:
    return (-(opp.score or 0.0), str(opp.idea_id or ""))


def _collect(inputs: StrategyInputs, params: StrategyParams) -> List[_Built]:
    """Every idea, unsized, deduplicated, conflict-resolved, best score first."""
    now, cup_end = inputs.now, inputs.cup_end
    regime = _regime(inputs.settlement_regime)
    latest = inputs.latest or {}
    infos = inputs.infos or {}
    races = inputs.races or {}
    fvs = inputs.fair_values or {}
    built: List[_Built] = []
    newest = _newest_surges(inputs.surges)
    for eid in sorted(newest, key=_eid_key):
        s = newest[eid]
        verdict = s.attribution.verdict if s.attribution is not None else None
        if verdict == VERDICT_PARTICIPANTS:
            b = _fade_build(s, latest.get(eid), infos.get(eid), now, inputs.backtest, cup_end, fv=fvs.get(eid),
                            race=races.get(eid), regime=regime, params=params, require_quote=True)
            if b is not None and not (b.meta["levels"] is not None and (b.opp.max_units or 0.0) < 1.0):
                built.append(b)
        elif verdict == VERDICT_UNCLEAR:
            w = watch_opportunity(s, infos.get(eid))
            if w is not None:
                race = races.get(eid)
                w.race_key = race.race_key if race is not None else None
                w.settlement_regime = regime
                built.append(_Built(w))
    for band in inputs.bands or ():
        eid = str(band.exchange_id)
        b = _carry_build(band, latest.get(eid), infos.get(eid), now, cup_end, fv=fvs.get(eid), race=races.get(eid),
                         regime=regime, params=params, require_quote=True)
        if b is not None and not (b.meta["levels"] is not None and (b.opp.max_units or 0.0) < 1.0):
            built.append(b)
    built.extend(_arbitrage_build(inputs.constraints, inputs.overround_rows or (), latest=latest, infos=infos,
                                  cup_end=cup_end, now=now, races=races, regime=regime, params=params))
    for eid in sorted(fvs, key=_eid_key):
        eid_s = str(eid)
        if eid_s not in infos or eid_s not in latest:
            continue
        opp = value_opportunity(eid_s, fvs[eid], latest.get(eid_s), infos.get(eid_s), races.get(eid_s), inputs, params)
        if opp is not None:
            built.append(_Built(opp))
    built.extend(_Built(o) for o in basket_opportunities(inputs, params))
    built.extend(_Built(o) for o in hole_opportunities(inputs, params))
    for b in built:
        b.opp.suggested_shares, b.opp.suggested_cost = 0, 0.0
        b.opp.sizing = b.opp.alt_sizing = None
        b.opp.fill_price = None
        if b.opp.idea_id is None:
            b.opp.idea_id = idea_id_for(b.opp.kind, exchange_id=b.opp.exchange_id, side=b.opp.side)
        if b.opp.settlement_regime is None:
            b.opp.settlement_regime = regime
    built.sort(key=lambda b: _signal_key(b.opp))
    unique: List[_Built] = []
    seen: Set[str] = set()
    for b in built:
        key = str(b.opp.idea_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)
    by_obj = {id(b.opp): b for b in unique}
    base_risks = {id(b.opp): len(b.opp.risks) for b in unique}
    resolved = _resolve_conflicts([b.opp for b in unique], infos, latest, races)
    out: List[_Built] = []
    for opp in resolved:
        b = by_obj[id(opp)]
        added = opp.risks[: max(0, len(opp.risks) - base_risks[id(opp)])]
        if added and b.render is not _noop:
            b.render = _keep_conflicts(b.render, opp, added)
        out.append(b)
    out.sort(key=lambda b: _signal_key(b.opp))
    return out


def _keep_conflicts(render: Callable[[Optional[Dict[str, Any]]], None], opp: Opportunity,
                    added: List[str]) -> Callable[[Optional[Dict[str, Any]]], None]:
    """A re-render keeps the conflict lines :func:`_resolve_conflicts` put first."""
    def wrapped(size: Optional[Dict[str, Any]]) -> None:
        render(size)
        opp.risks = list(added) + [r for r in opp.risks if r not in added]
    return wrapped


def generate_signals(inputs: StrategyInputs, params: Optional[StrategyParams] = None) -> List[Opportunity]:
    """Every idea of every kind for these inputs, UNSIZED (suggested_shares 0, sizing None), each with
    idea_id, race_key, bet, exit_plan, order_type, limit_price and max_units. Deterministic: sorted by
    score descending, then idea_id. This is what both the paper engine and build_report consume (§5.1)."""
    p_ = params or inputs.params or StrategyParams()
    return [b.opp for b in _collect(inputs, p_)]


# --------------------------------------------------------------------------- backtest


def _bid_ask(point: PricePoint, assumed_spread: float) -> Tuple[float, float, bool]:
    """(YES bid, YES ask, whether they were assumed): the tick's own quote, else the mark +/- half an
    assumed spread."""
    quote = _two_sided(point)
    if quote is not None:
        return quote[0], quote[1], False
    price = float(point.price)  # type: ignore[arg-type]
    half = assumed_spread / 2.0
    return max(0.0, price - half), min(1.0, price + half), True


def backtest_fade(series_by_exchange: Mapping[str, Sequence[PricePoint]], horizon_s: float = 6 * 3600.0,
                  step_s: float = 300.0, market_of: Optional[Mapping[str, str]] = None, *,
                  windows: Optional[Dict[str, Tuple[float, float]]] = None,
                  z_threshold: float = Z_THRESHOLD, net_of_spread: bool = True,
                  surges: Optional[Sequence[Surge]] = None, assumed_spread: float = ASSUMED_SPREAD) -> BacktestResult:
    """How often detected surges gave back half their move within ``horizon_s``.

    Walks each series on a ``step_s`` grid making the :func:`analytics.detect_surges` decision
    at every step (incrementally: O(n + steps) per series). A surge counts once per
    (exchange, direction) until its status is no longer open, and only if it is open at
    detection. Detections without data at the horizon are skipped and counted in the notes.

    ``net_of_spread`` (default): a fade enters at the detection-time ask (YES, after a drop) or
    ``1 - bid`` (NO, after a jump) and exits at the horizon's bid of that contract; it counts as reverted
    when that exit bid reaches the half-reversion target less half the entry spread (what the live fade's
    target order needs). Points without a quote (candles, marks) assume a ``assumed_spread`` spread, and a
    note says so. ``fade_return`` / ``hold_return`` are per share, both net of both spreads.
    ``net_of_spread=False`` restores the old mark-to-mark numbers (reverted = half the move given back at
    the mark, returns from the marks, ignoring spread).

    ``surges`` (stored surges with their attribution) let the result also report ``n_participant`` and
    ``participant_reversion_rate``: the same net measure over detections matched to a stored surge on the
    same exchange and direction, detected within the detection's window, whose verdict is "participants"
    (the rate a fade's win chance may not exceed, §5.5).
    """
    windows = windows or SURGE_WINDOWS
    events: List[Tuple[str, float, bool, float, bool]] = []  # (window, fade, reverted, hold, participant)
    pending = missing = n_series = assumed = 0
    stored: Dict[Tuple[str, str], List[Surge]] = {}
    for s in surges or ():
        if s.attribution is None:
            continue
        stored.setdefault((str(s.exchange_id), str(s.direction)), []).append(s)
    for eid in sorted(series_by_exchange):
        series = _Series(series_by_exchange[eid])
        if len(series) < 2:
            continue
        n_series += 1
        market_id = str((market_of or {}).get(eid, ""))
        scanner = _SurgeScanner(series, step_s, windows)
        last_ts = series.ts[-1]
        active: Dict[str, Surge] = {}
        for t in scanner.times():
            if active:
                i = series.idx_at(t)
                mark = series.prices[i] if i is not None else None
                for direction in list(active):
                    if update_surge_status(active[direction], mark, t).status != SURGE_OPEN:
                        del active[direction]
            found = scanner.detect(t, eid, market_id, z_threshold)
            if not found:
                continue
            surge = found[0]
            if surge.direction in active or surge.status != SURGE_OPEN:
                continue
            active[surge.direction] = surge
            t_h = t + horizon_s
            if t_h > last_ts + 1e-6:
                pending += 1
                continue
            j = series.value_idx(t_h, window_tolerance(horizon_s))
            if j is None:
                missing += 1
                continue
            p_h = series.prices[j]
            up = surge.direction == "up"
            participant = any(abs(s.detected_at - t) <= max(surge.window_s, step_s) + 1e-6
                              and s.attribution is not None and s.attribution.verdict == VERDICT_PARTICIPANTS
                              for s in stored.get((str(eid), str(surge.direction)), []))
            if not net_of_spread:
                fade = surge.end_price - p_h if up else p_h - surge.end_price
                outcome = update_surge_status(copy.copy(surge), p_h, t_h)
                events.append((surge.window, round(fade, 6), outcome.status == SURGE_REVERTED, round(-fade, 6), participant))
                continue
            i = series.idx_at(t)
            point_t = series.points[i] if i is not None else series.points[0]
            bid_t, ask_t, guess_t = _bid_ask(point_t, assumed_spread)
            bid_h, ask_h, guess_h = _bid_ask(series.points[j], assumed_spread)
            if guess_t or guess_h:
                assumed += 1
            half_t = (ask_t - bid_t) / 2.0
            yes_target = surge.start_price + 0.5 * (surge.peak_price - surge.start_price)
            if up:  # fade = buy NO at 1 - bid_t, exit at the NO bid 1 - ask_h; hold = buy YES at ask_t, exit at bid_h
                fade = bid_t - ask_h
                hold = bid_h - ask_t
                reverted = (1.0 - ask_h) >= (1.0 - yes_target) - half_t - 1e-9
            else:  # fade = buy YES at ask_t, exit at bid_h; hold = buy NO at 1 - bid_t, exit at 1 - ask_h
                fade = bid_h - ask_t
                hold = bid_t - ask_h
                reverted = bid_h >= yes_target - half_t - 1e-9
            events.append((surge.window, round(fade, 6), reverted, round(hold, 6), participant))

    def stats(rows: Sequence[Tuple[str, float, bool, float, bool]]) -> Dict[str, Any]:
        n = len(rows)
        k = sum(1 for r in rows if r[2])
        avg = sum(r[1] for r in rows) / n if n else None
        hold = sum(r[3] for r in rows) / n if n else None
        return {
            "n": n,
            "n_reverted": k,
            "reversion_rate": round(k / n, 4) if n else None,
            "avg_fade_return": round(avg, 6) if avg is not None else None,
            "avg_hold_return": round(hold, 6) + 0.0 if hold is not None else None,
        }

    total = stats(events)
    part = [e for e in events if e[4]]
    part_stats = stats(part)
    by_window = {}
    for name, _ in sorted(windows.items(), key=lambda kv: kv[1][0]):
        rows = [e for e in events if e[0] == name]
        if rows:
            by_window[name] = stats(rows)
    hours = horizon_s / 3600.0
    notes = [
        f"Walked {n_series} series on a {step_s / 60.0:g}-minute grid; a surge counts once per exchange and "
        "direction until it reverts or holds",
    ]
    if net_of_spread:
        notes.append(f"Fades enter at the detection-time ask (YES) or 1 - bid (NO) and exit {hours:g} h later at the bid, "
                     "net of the spread; a reversion counts when that exit bid reaches the half-reversion target")
        if assumed:
            notes.append(f"{assumed} evaluation(s) had no quote at entry or exit (candles or marks): an assumed "
                         f"{assumed_spread:g} spread was charged")
    else:
        notes.append(f"Fades enter at the detection mark and exit {hours:g} h later, ignoring spread and fees: real "
                     "returns are lower")
    if pending:
        notes.append(f"{pending} detection(s) too recent to evaluate (less than {hours:g} h of data after them)")
    if missing:
        notes.append(f"{missing} detection(s) had no price near the horizon")
    if total["n"] < MIN_BACKTEST_SURGES:
        notes.append(f"Only {total['n']} evaluated surge(s), fewer than {MIN_BACKTEST_SURGES}: fades use the "
                     "attribution odds alone")
    if surges is None:
        notes.append("No stored attributions were given: the participant-only reversion rate is not measured")
    elif part_stats["n"] < MIN_BACKTEST_SURGES:
        notes.append(f"Only {part_stats['n']} participant-driven surge(s) evaluated, fewer than {MIN_BACKTEST_SURGES}: "
                     "their reversion rate does not cap fade odds yet")
    return BacktestResult(
        n_surges=total["n"],
        n_reverted=total["n_reverted"],
        reversion_rate=total["reversion_rate"],
        avg_fade_return=total["avg_fade_return"],
        avg_hold_return=total["avg_hold_return"],
        horizon_hours=round(hours, 4),
        by_window=by_window,
        notes=notes,
        net_of_spread=net_of_spread,
        n_participant=part_stats["n"],
        participant_reversion_rate=part_stats["reversion_rate"],
    )


# --------------------------------------------------------------------------- report


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _unknown_inputs(balance: Optional[float], leader: Optional[float], rank: Optional[int]) -> List[str]:
    out = []
    if rank is None:
        out.append("your rank")
    if leader is None:
        out.append("the leader's value")
    if balance is None:
        out.append("your balance")
    return out


def _assumptions(mode: str, balance: Optional[float], sizing: float, leader: Optional[float], rank: Optional[int],
                 days_left: float, account_value: Optional[float] = None) -> List[str]:
    """What the report assumed for unknown inputs (failed or missing balance / leaderboard reads)."""
    out: List[str] = []
    if balance is None:
        out.append(f"Balance unknown: sized on the {sizing:,.0f} starting balance")
    elif account_value is None and leader is not None:
        out.append("Account value unknown: risk mode compares your cash (open positions not counted) with the "
                   "leader's account value, so it may overstate how far behind you are")
    if rank is None and leader is None:
        text = f"Leaderboard unavailable: risk mode assumes {mode}"
        if mode == MODE_AGGRESSIVE:
            text += f" (rank unknown, treated as outside the top 10 with {days_left:.0f} days left)"
        out.append(text)
    elif rank is None:
        out.append("Rank unknown: risk mode treats you as outside the top 3"
                   + (" and the top 10" if mode == MODE_AGGRESSIVE and days_left <= 10 else ""))
    elif leader is None:
        out.append("Leader's value unknown: risk mode cannot tell how far behind the leader you are")
    if balance is None and leader is not None:
        out.append("Balance unknown: risk mode cannot compare your balance with the leader's")
    return out


def _principles(mode: str, balance: Optional[float], leader: Optional[float], rank: Optional[int],
                days_left: float, cup_end: float, account_value: Optional[float] = None) -> List[str]:
    out = [
        "Prizes go to the top 3 of many players, so the payoff is convex: 4th place pays the same as last. "
        "When you are behind, concentrate on your best edges, but a same-party slate is one bet on the national "
        "swing: size it as one. When you are in the top 3 near the end, cut variance and protect the lead.",
        "Carry trades on stable high-90s favourites compound slowly: a fraction of a percent to a few percent "
        "per trade, but nearly all of them pay. They protect a lead; they are too slow to catch up.",
        "Fade only participant-driven moves: a jump made by a few large trades with no news tends to give back "
        "half, while a move backed by news usually holds. Follow news, or stay out.",
        f"The Cup's end rule is unknown: prefer positions that pay under both a real resolution and a 5-hour VWAP "
        f"closeout at the Cup end ({_when(cup_end)}); a settlement date is not a payout date.",
        "Races share one national polling error: a same-party slate of toss-ups is one bet; diversify only to "
        "protect a lead.",
    ]
    if mode == MODE_PROTECT:
        out.append(f"Mode: protect. You are ranked {rank} with {days_left:.0f} days left, so favour carry "
                   "(score x1.3), damp fades (x0.6) and keep positions small.")
    elif mode == MODE_AGGRESSIVE:
        mine = account_value if account_value is not None else balance
        if leader and mine is not None and mine < 0.9 * leader:
            what = "your account value" if account_value is not None else "your cash (positions not counted)"
            why = f"{what} {mine:,.0f} is more than 10% behind the leader's {leader:,.0f}"
        else:
            where = "unranked (or your rank is unknown)" if rank is None else f"ranked {rank}"
            why = f"only {days_left:.0f} days left and you are {where}, outside the top 10"
        out.append(f"Mode: aggressive ({why}), so favour fades and arbitrage (score x1.3) over slow carry (x0.7).")
    else:
        unknown = _unknown_inputs(balance, leader, rank)
        if unknown:
            verb = "is" if len(unknown) == 1 else "are"
            out.append(f"Mode: balanced by default. {_join(unknown).capitalize()} {verb} unknown, so the bot cannot tell "
                       "whether you are defending a top-3 finish or far behind: ideas are ranked by expected growth "
                       "(profit as a share of equity at a conservative size) x confidence.")
        else:
            out.append("Mode: balanced. You are neither defending a top-3 finish nor far behind: ideas are ranked by "
                       "expected growth (profit as a share of equity at a conservative size) x confidence.")
    return out


def _describe(opp: Opportunity) -> str:
    label = opp.title + (f" ({opp.option})" if opp.option else "")
    if opp.kind == KIND_BASKET:
        return f"buy the {len(opp.legs)}-leg {opp.side.upper()} basket on {label} for {_px(opp.entry_price)} per set"
    if opp.kind == "arbitrage" and opp.legs and not (opp.target_price == 1.0 and all(leg.get("side") == "yes" for leg in opp.legs)):
        return f"buy the {len(opp.legs)}-leg set on {label} ({_legs_text(opp.legs)}) for {_px(opp.entry_price)} per set"
    if opp.kind == "arbitrage" and opp.exchange_id is None:
        return f"buy every outcome of {label} for {_px(opp.entry_price)} per set"
    if opp.kind == KIND_HOLE:
        return f"hole - rest a buy for {opp.side.upper()} on {label} at {_px(opp.limit_price)}"
    at = f" at {_px(opp.entry_price)}" if opp.entry_price is not None else ""
    return f"{opp.kind} - buy {opp.side.upper()} on {label}{at}"


def _headline(mode: str, days_left: float, opps: Sequence[Opportunity]) -> str:
    counts = {kind: sum(1 for o in opps if o.kind == kind) for kind in _KIND_ORDER}
    trades = sum(counts[k] for k in _KIND_ORDER if k != KIND_WATCH)
    parts = [f"{counts[k]} {k}" for k in NEW_KINDS if counts[k]]
    parts += [f"{counts['fade']} fade", f"{counts['carry']} carry", f"{counts['arbitrage']} arbitrage"]
    text = (f"{mode.capitalize()} mode, {days_left:.0f} days left: {trades} trade idea{'s' if trades != 1 else ''} "
            f"({', '.join(parts)})")
    if counts["watch"]:
        text += f", {counts['watch']} surge{'s' if counts['watch'] != 1 else ''} to watch"
    text += "."
    best = next((o for o in opps if o.kind != "watch" and o.score > 0), None)
    if best is not None:
        text += f" Top idea: {_describe(best)}."
    return text


def _size_info(opp: Opportunity, dec: SizeDecision, policy: Any, ctx: SizingContext, now: float) -> Dict[str, Any]:
    """What the legacy size sentences need, from a policy decision."""
    kelly, cap = sizing_mod.policy_terms(policy, ctx, opp)
    entry = opp.entry_price or 0.0
    cost = round(dec.units * (dec.avg_cost or entry), 2)
    cap_shares = int(math.floor(cap * ctx.equity / entry + 1e-9)) if entry > 0 and ctx.equity > 0 else 0
    offered = float(opp.max_units) if opp.max_units is not None and (opp.levels or opp.unit == "sets") else None
    return {"shares": dec.units, "cost": cost, "kelly_mult": kelly, "cap_pct": cap,
            "depth_capped": "depth" in dec.capped_by, "fill": dec.avg_cost, "offered": offered,
            "cap_shares": cap_shares, "limit": opp.limit_price, "book_at": None, "now": None}


def _policy_pair(sizing: str, kelly_mult: float, max_position_pct: float) -> Tuple[Any, Any]:
    name = str(sizing or POLICY_CONSERVATIVE).strip().lower()
    if name not in (POLICY_CONSERVATIVE, POLICY_CHASER):
        raise ValueError(f"unknown sizing policy {sizing!r}")
    conservative = sizing_mod.ConservativePolicy(kelly_mult=kelly_mult, idea_cap=max_position_pct,
                                                 riskless_cap=max_position_pct)
    chaser = sizing_mod.get_policy(POLICY_CHASER)
    return (conservative, chaser) if name == POLICY_CONSERVATIVE else (chaser, conservative)


JOINT_SIZING_LINE = ("The cards are sized together, best score first, the way the simulator sizes them: their sizes add "
                     "up, and following every card stays inside the caps above (a weaker idea gets less, or nothing, "
                     "when better ones use the room)")
RESORT_LINE = ("The risk mode re-ranks the cards after sizing: the sizes follow the plain growth score, the order the "
               "simulator funds them in")
BOLD_CARD_LINE = ("As the chaser's bold bet it is held to settlement: the exit target above does not apply, because only "
                  "a 1/0 win reaches the bar")
_JOINT_CAPS = frozenset({"total_cap", "race_cap", "tilt_cap", "swing_cap", "cash", "factor", "top_k"})


def _joint_lines(signals: Sequence[Opportunity], decisions: Mapping[str, SizeDecision]) -> None:
    """Tell each card it was sized after the ideas before it (in the order ``size_all`` funds them: the chaser's
    bold bet first, then best score first), and how much they take, so nobody re-adds the sizes."""
    order = sizing_mod._ordered(signals)
    bold_ids = {key for key, dec in decisions.items() if dec.bold}
    seq = [o for o in order if str(o.idea_id) in bold_ids] + [o for o in order if str(o.idea_id) not in bold_ids]
    committed = 0.0
    seen_bold = False
    others = 0
    for o in seq:
        dec = decisions.get(str(o.idea_id))
        if dec is None:
            continue
        sized = o.kind != KIND_WATCH and o.bet is not None
        if sized and committed > 0.005 and (dec.units > 0 or _JOINT_CAPS.intersection(dec.capped_by)):
            ideas = "the idea funded before it" if others == 1 else f"the {others} ideas funded before it"
            if seen_bold and others:
                before = f"the bold bet and {ideas}, which take"
            elif seen_bold:
                before = "the bold bet, which takes"
            else:
                before = f"{ideas}, which {'takes' if others == 1 else 'take'}"
            dec.lines.append(f"Sized after {before} {committed:,.0f} SUSQies: the card sizes add up")
        if dec.units > 0:
            committed += float(dec.stake or 0.0)
            seen_bold = seen_bold or dec.bold
            others += 0 if dec.bold else 1


def _hold_bold(opp: Opportunity) -> None:
    """The bold card's exit plan holds to settlement (sizing.bold_exit_plan): no target, no stop."""
    opp.exit_plan = sizing_mod.bold_exit_plan(opp)
    opp.target_price = None
    opp.stop_price = None


def _basis_lines(bal: Optional[float], value: Optional[float], held: float, resorted: bool = False) -> List[str]:
    """The sizing panel's extra lines: what "your value" is, what the bot knows of open positions, and that the cards
    are sized together (the last line, ``JOINT_SIZING_LINE``, plus ``RESORT_LINE`` when the risk mode re-ranks)."""
    out: List[str] = []
    if value is not None and value > 0:
        cash = f"; your cash ({bal:,.0f}) caps what can be bought" if bal is not None else ""
        out.append(f"Your value here is your account value ({value:,.0f}: cash plus positions at market prices), the "
                   f"same basis as the leaderboard's values and the bar{cash}")
    elif bal is not None:
        out.append(f"Your value here is your cash ({bal:,.0f}): the account value is unknown, so open positions are not "
                   "counted and M may be overstated")
    if held > 0.5:
        out.append(f"Your open positions ({held:,.0f} SUSQies at market prices) count against the total cap only: the bot "
                   "cannot see which races they are in, so the race, swing and slot caps cover only these new ideas")
    out.append(JOINT_SIZING_LINE)
    if resorted:
        out.append(RESORT_LINE)
    return out


def _with_lines(explain: Mapping[str, Any], extra: Sequence[str]) -> Dict[str, Any]:
    """``explain`` with ``extra`` lines: the basis right after the "M = ..." line (else at the end)."""
    out = dict(explain)
    lines = list(out.get("lines") or [])
    tail = (JOINT_SIZING_LINE, RESORT_LINE)
    at = next((i + 1 for i, line in enumerate(lines) if str(line).startswith("M = ")), len(lines))
    lines[at:at] = [x for x in extra if x not in tail]
    lines.extend(x for x in extra if x in tail)
    out["lines"] = lines
    return out


def _report(inputs: StrategyInputs, *, sizing: str = POLICY_CONSERVATIVE,
            fair_value_status: Optional[Mapping[str, Any]] = None, top_n: int = TOP_N,
            kelly_mult: float = KELLY_MULT, max_position_pct: float = MAX_POSITION_PCT) -> StrategyReport:
    params = inputs.params or StrategyParams()
    now, cup_end = inputs.now, inputs.cup_end
    initial = _num(inputs.initial_balance)
    if initial is None or initial <= 0:
        initial = DEFAULT_INITIAL_BALANCE
    bal = _num(inputs.balance)
    basis = bal if bal is not None else initial
    days_left = max(0.0, (cup_end - now) / 86400.0)
    rank = inputs.my_rank if isinstance(inputs.my_rank, int) and not isinstance(inputs.my_rank, bool) else None
    leader = _num(inputs.leader_value)
    value = _num(inputs.account_value)
    mode = risk_mode(bal, initial, leader, rank, days_left, value)
    regime = _regime(inputs.settlement_regime)
    policy, other = _policy_pair(sizing, kelly_mult, max_position_pct)
    bar = inputs.bar
    if bar is None and inputs.leaderboard is not None:
        bar = sizing_mod.estimate_bar(inputs.leaderboard, (), initial, days_left)
    # Like with like (the paper engine sizes on equity_liq): your value for M and Kelly is the ACCOUNT VALUE (cash plus
    # positions), the basis of the bar and the leaderboard; cash only caps what can be bought. Positions are known
    # here only as a total (account value - cash), which counts against the total cap.
    equity = value if value is not None and value > 0 else basis
    held = max(0.0, value - bal) if value is not None and bal is not None else 0.0
    exposure = ExposureSummary(gross_cost=round(held, 6))
    ctx = SizingContext(now=now, equity=max(0.0, equity), cash=max(0.0, basis), free_cash=max(0.0, basis),
                        start_capital=initial, cup_end=cup_end, days_left=days_left, exposure=exposure, bar=bar,
                        my_rank=rank, regime=regime, params=params, races=dict(inputs.races or {}))
    built = _collect(inputs, params)
    signals = [b.opp for b in built]
    # Every card is sized TOGETHER with the others (size_all, best score first), exactly as the paper engine sizes the
    # same ideas: following every card stays inside the total, race and swing caps, the slate rule, top_k and the one
    # bold bet. Sized one by one, 12 cards each "within 8%" added up to 75% of equity.
    decisions = policy.size_all(signals, ctx)
    alts = other.size_all(signals, ctx)
    _joint_lines(signals, decisions)
    opps: List[Opportunity] = []
    for b in built:
        opp = b.opp
        key = str(opp.idea_id)
        dec = decisions.get(key) or policy.size(opp, ctx)
        alt = alts.get(key) or other.size(opp, ctx)
        if dec.bold:
            _hold_bold(opp)
        opp.suggested_shares = int(dec.units)
        opp.suggested_cost = round(dec.units * (dec.avg_cost or opp.entry_price or 0.0), 2) if dec.units else 0.0
        opp.sizing = dec.to_dict()
        opp.alt_sizing = alt.to_dict()
        opp.fill_price = dec.avg_cost if dec.units > 0 else None
        if opp.kind in LEGACY_KINDS:
            b.render(_size_info(opp, dec, policy, ctx, now))
        elif opp.kind in NEW_KINDS and dec.lines:
            opp.rationale.append(dec.lines[0])
        if dec.bold:
            opp.rationale.append(BOLD_CARD_LINE)
        if bal is None and opp.suggested_shares > 0:
            opp.rationale.append(f"Balance unknown: this size assumes the {basis:,.0f} starting balance")
        opps.append(opp)

    multipliers = MODE_MULTIPLIERS.get(mode, {})
    for opp in opps:
        opp.score = round(opp.score * multipliers.get(opp.kind, 1.0), 6)
    opps.sort(key=lambda o: (-o.score, _KIND_ORDER.get(o.kind, 9), -o.confidence, o.title, o.exchange_id or "",
                             str(o.idea_id or "")))
    opps = opps[: max(0, top_n)]
    extra = _basis_lines(bal, value, held, resorted=any(abs(m - 1.0) > 1e-9 for m in multipliers.values()))
    explain = _with_lines(policy.explain(ctx), extra)
    explain["alternative"] = _with_lines(other.explain(ctx), extra)
    return StrategyReport(
        generated_at=now,
        risk_mode=mode,
        headline=_headline(mode, days_left, opps),
        balance=bal,
        initial_balance=initial,
        account_value=value,
        cup_end=cup_end,
        days_left=round(days_left, 2),
        leader_value=leader,
        my_rank=rank,
        principles=_principles(mode, bal, leader, rank, days_left, cup_end, value),
        opportunities=opps,
        backtest=inputs.backtest,
        assumptions=_assumptions(mode, bal, basis, leader, rank, days_left, value),
        sizing_policy=policy.name,
        sizing=explain,
        settlement_regime=regime,
        fair_value=dict(fair_value_status) if fair_value_status is not None else None,
    )


def build_report(*, now: float, surges: Sequence[Surge], bands: Sequence[HighBand],
                 latest: Mapping[str, PricePoint], infos: Mapping[str, ExchangeInfo],
                 balance: Optional[float], initial_balance: Optional[float], leader_value: Optional[float],
                 my_rank: Optional[int], cup_end: float, constraints: Optional[Mapping[str, Any]] = None,
                 overround_rows: Sequence[Mapping[str, Any]] = (), backtest: Optional[BacktestResult] = None,
                 kelly_mult: float = KELLY_MULT, max_position_pct: float = MAX_POSITION_PCT,
                 top_n: int = TOP_N, account_value: Optional[float] = None,
                 fair_values: Optional[Mapping[str, FairValue]] = None, races: Optional[Mapping[str, RaceRef]] = None,
                 settlement_regime: str = REGIME_UNKNOWN, leaderboard: Any = None, bar: Any = None,
                 sizing: str = POLICY_CONSERVATIVE, params: Optional[StrategyParams] = None,
                 fair_value_status: Optional[Mapping[str, Any]] = None,
                 recent_mids: Optional[Mapping[str, Sequence[Tuple[float, float]]]] = None) -> StrategyReport:
    """Ranked, sized, read-only ideas plus the tournament posture.

    Builds :class:`StrategyInputs` from its arguments and calls :func:`generate_signals` (the newest open,
    non-reversion surge per exchange -> fade or watch, every stable band -> carry, every arbitrage flag,
    value ideas from ``fair_values``, baskets from race/market groups, liquidity holes), then sizes every
    idea with the ``sizing`` policy (``"conservative"``: quarter-Kelly on the expected average fill, capped
    at ``max_position_pct`` per idea; ``"chaser"``: goal-based) JOINTLY (``size_all``, best score first, as
    the paper engine does), so the cards add up within the portfolio caps, the slate rule, top_k and the one
    bold bet. Your value (M, Kelly, caps) is ``account_value`` (cash plus positions) when known, else
    ``balance``, else ``initial_balance``, else 100,000; the cash (``balance``) caps what can be bought, and
    the positions' value (account value - cash) counts against the total cap. ``alt_sizing`` holds the other
    policy's size.
    Scores are :func:`growth_score` times the risk-mode multiplier; the top ``top_n`` are kept, best first.
    ``assumptions`` lists what was assumed for unknown inputs (balance, rank, leader value), and sized
    ideas say so too.

    Surges whose status is not ``open`` (reverted, held, or ``closed`` because their market
    left the open list) never produce fade or watch ideas, and neither does a surge that is
    the reversion of a recent opposite surge on the same outcome. Ideas that contradict a
    tradable arbitrage are dropped or flagged (see :func:`_resolve_conflicts`).

    ``balance`` is cash (it caps the sizes); ``account_value`` (cash + open positions) is the value
    the sizes, M and the risk mode use (compared like with like with the leader's value and the
    bar), falling back to cash when unknown. Sizes are capped by the order-book depth on the
    ``latest`` points when known.
    """
    inputs = StrategyInputs(
        now=now, cup_end=cup_end, infos=dict(infos or {}), latest=dict(latest or {}), surges=list(surges or ()),
        bands=list(bands or ()), constraints=dict(constraints) if constraints is not None else None,
        overround_rows=[dict(r) if isinstance(r, Mapping) else r for r in overround_rows or ()],  # type: ignore[misc]
        fair_values=dict(fair_values or {}), races=dict(races or {}), backtest=backtest, balance=balance,
        initial_balance=initial_balance, account_value=account_value, leader_value=leader_value, my_rank=my_rank,
        leaderboard=leaderboard, bar=bar, settlement_regime=_regime(settlement_regime), params=params,
        recent_mids={str(k): list(v) for k, v in (recent_mids or {}).items()},
    )
    return _report(inputs, sizing=sizing, fair_value_status=fair_value_status, top_n=top_n, kelly_mult=kelly_mult,
                   max_position_pct=max_position_pct)


def report_from_inputs(inputs: StrategyInputs, *, sizing: str = POLICY_CONSERVATIVE,
                       fair_value_status: Optional[Mapping[str, Any]] = None) -> StrategyReport:
    """build_report with every field taken from ``inputs`` (what web.DashboardApp uses, §5.7)."""
    return _report(inputs, sizing=sizing, fair_value_status=fair_value_status)
