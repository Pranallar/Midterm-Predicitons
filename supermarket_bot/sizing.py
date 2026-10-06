"""Sizing policies: how many units of an idea to buy (package B of docs/PAPER_TRADING.md §5.6, revision 2).

* :class:`ConservativePolicy`: quarter-Kelly capped at 8% of equity per idea, plus portfolio caps
  (60% gross, 12% per race, and a national-swing cap: a one-sd (3-point) national polling miss may cost
  at most 10% of equity). Units are Kelly-sized on the expected depth-walked AVERAGE fill up to the
  order's limit (``Opportunity.levels``), never on the touch alone.
* :class:`ChaserPolicy`: goal-based sizing for a player who is behind. It estimates the top-3 bar
  (:func:`estimate_bar`, from the LOWER end of its range), computes ``M = bar / own value`` and picks a
  mode with hysteresis: "near" (M <= 1.3), "chase" (1.3 < M <= 10, more than ``SWING_DAYS`` left),
  "swing" (the last ``SWING_DAYS``: at most ONE bold-to-goal bet, on a binary contract held to settlement
  (:func:`bold_exit_plan`), only when the stake the free cash, the total cap and the book can fund wins to the bar),
  "out_of_reach" (M > 10: conservative sizing, aim for Smart Score) or "unknown_bar". In every mode the
  Kelly multiplier is shrunk by edge certainty and correlated same-party ideas are sized as one factor
  bet, not several full-Kelly bets.

Every size comes with plain-English lines saying which rule set it. Sizes never exceed the free
cash. Nothing here places orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import depth
from .models import (
    POLICIES,
    POLICY_CHASER,
    POLICY_CONSERVATIVE,
    BarEstimate,
    BetShape,
    ExitPlan,
    LeaderboardSnapshot,
    Opportunity,
    SizeDecision,
    SizingContext,
    SizingPolicy,
    StrategyParams,
)

# conservative
KELLY_MULT = 0.25
IDEA_CAP_PCT = 0.08
RISKLESS_CAP_PCT = 0.08  # baskets / arbitrage sets (riskless or bounded): same 8% as the legacy arbitrage sizing
TOTAL_CAP_PCT = 0.60
RACE_CAP_PCT = 0.12
TILT_CAP_PCT = 0.10  # |national_tilt_d| x national_swing_sd_pts <= this x equity (a 3-point swing costs <= 10%)
ALL_COLLATERAL_NOTIONAL_PCT = 1.0  # with ALL collateral a set needs little cash; notional still capped

# certainty: k_eff = k x edge^2 / (edge^2 + sigma^2), sigma = Opportunity.fv_uncertainty or the kind default
DEFAULT_SIGMA = {"value": 0.025, "fade": 0.03, "carry": 0.02, "hole": 0.0, "basket": 0.0, "arbitrage": 0.0}

# chaser
NEAR_M = 1.3
OUT_OF_REACH_M = 10.0
MODE_BAND = 0.10  # hysteresis: leave a mode only when M is 10% beyond the threshold ...
MODE_MIN_HOLD_S = 6 * 3600.0  # ... and the mode has lasted at least this long (swing and unknown_bar switch at once)
SWING_DAYS = 2.0  # the swing window: election night through the Cup end
NEAR = {"kelly_mult": 0.5, "idea_cap": 0.12, "race_cap": 0.25, "total_cap": 0.70, "swing_cap": 0.30}
CHASE = {"kelly_mult": 1.0, "idea_cap": 0.25, "race_cap": 0.35, "total_cap": 0.95, "top_k": 5, "swing_cap": 0.40}
SWING = {"kelly_mult": 1.0, "idea_cap": 0.25, "race_cap": 0.35, "total_cap": 0.95, "top_k": 5, "swing_cap": None}
UNKNOWN_BAR = {"kelly_mult": 0.5, "idea_cap": 0.15, "race_cap": 0.25, "total_cap": 0.80, "swing_cap": 0.30}
UNKNOWN_DEPTH_MAX_KELLY = 0.5  # chaser: at most half-Kelly when the idea's depth is unknown (fills may reach the limit)
FACTOR_RHO = 0.5  # assumed correlation of same-party ideas through the national factor
REPLACE_MARGIN = 0.5  # top_k full: a new idea replaces the weakest held one only if its score is 50% higher
SWING_BOLD_CAP_PCT = 0.60  # the one bold-to-goal stake, at most this share of equity
CHASER_BASKET_CAP_PCT = 0.20  # chaser baskets / sets (bounded, not riskless: refunds and closeout regimes)

# bar
BAR_FLOOR_MULT = 2.0  # never assume the top-3 bar is below 2x the initial balance
BAR_CAP_MULT = 10.0
BAR_MAX_GROWTH_PER_DAY = 0.02  # LINEAR growth per day, as a share of today's value
BAR_MIN_HISTORY_S = 3 * 86400.0  # growth needs leaderboard history spanning at least 3 days
BAR_GROWTH_RANKS = (3, 4, 5, 6, 7, 8, 9, 10)  # median growth over these ranks (robust to one thin-book mark)

DIRECTIONAL_BETS = ("binary", "bracket")  # chaser concentration (top_k) and factor scaling apply to these only
SET_BETS = ("riskless", "bounded")

DEFAULT_EQUITY = 100_000.0
BOLD_CALLED_MIN = 0.85  # a bold bet needs a market that settles before the Cup end or is likely called by then
BOLD_BETS = ("binary",)  # ... and a contract held to its 1/0 result: a bracket (fade) exits at its target or stop
BOLD_HOLD_NOTE = ("Bold bet: hold to settlement (no convergence target, no stop): only a 1/0 win reaches the bar")
BOLD_HOLD_LINE = ("Hold it to settlement: no convergence target and no stop, because only a 1/0 win reaches the bar "
                  "(selling at a convergence target would bank a few cents a share on a stake many times Kelly)")

MARKS_CAVEAT = ("Leaderboard values are mid-Cup marks (cash plus positions at current prices), not settled balances; "
                "thin-market marks can be inflated.")
OUT_OF_REACH_LINE = ("The bar is more than 10x your value: the prize is out of reach in expectation, so size for a steady "
                     "record (Smart Score) instead")
SLATE_LINE = ("Correlated, same-party ideas are sized together as one bet on the national swing: concentrated, not "
              "multiplied")
CASH_LINE = "Sizes never exceed the free cash"
CONSERVATIVE_LINES = (
    "Quarter-Kelly on the expected average fill, at most 8% of equity per idea, 12% per race, 60% in total",
    "A 3-point national polling miss may cost at most 10% of equity: races share one national polling error",
)

_EPS = 1e-9


# --------------------------------------------------------------------------- small helpers


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _floor(x: float) -> int:
    """Whole units, with the legacy +1e-9 so 999.9999999999999 is 1,000."""
    if x is None or not math.isfinite(x):
        return 0
    return max(0, int(math.floor(x + 1e-9)))


def _kelly_label(mult: float) -> str:
    names = {0.25: "quarter-Kelly", 0.5: "half-Kelly", 1.0: "full Kelly"}
    for value, name in names.items():
        if abs(mult - value) < 1e-9:
            return name
    return f"{mult:.2g}x Kelly"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _px(value: Optional[float]) -> str:
    """A price with 2-4 decimals: 0.31, 0.605, 0.6125."""
    if value is None:
        return "-"
    text = f"{value:.4f}".rstrip("0")
    head, _, frac = text.partition(".")
    return f"{head}.{frac.ljust(2, '0')}"


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _sign(value: Optional[float]) -> int:
    v = _num(value)
    if v is None or abs(v) <= 1e-12:
        return 0
    return 1 if v > 0 else -1


def _params(ctx: SizingContext) -> StrategyParams:
    return ctx.params if isinstance(ctx.params, StrategyParams) else StrategyParams()


def _race_key(opp: Opportunity, ctx: SizingContext) -> str:
    if opp.race_key:
        return str(opp.race_key)
    race = ctx.races.get(str(opp.exchange_id)) if opp.exchange_id is not None and ctx.races else None
    if race is not None and getattr(race, "race_key", None):
        return str(race.race_key)
    return f"market:{opp.market_id}"


def _idea_id(opp: Opportunity) -> str:
    if opp.idea_id:
        return str(opp.idea_id)
    if opp.legs:
        ids = "+".join(str(leg.get("exchange_id")) for leg in opp.legs)
        return f"{opp.kind}:set:{ids}:{opp.side}"
    return f"{opp.kind}:x{opp.exchange_id if opp.exchange_id is not None else 'm' + str(opp.market_id)}:{opp.side}"


def _unit(opp: Opportunity) -> str:
    return "sets" if opp.unit == "sets" else "shares"


# --------------------------------------------------------------------------- Kelly mechanics


def kelly_units(p: float, gain: float, loss: float, equity: float, kelly_mult: float) -> float:
    """Units that maximise growth for a bet winning ``gain`` with ``p`` and losing ``loss`` per unit:
    ``kelly_mult * equity * (p / loss - (1 - p) / gain)`` (0 when that is not positive). For a
    contract bought at ``c`` held to 1/0 this equals ``kelly_mult * f * equity / c`` with
    ``f = (p - c) / (1 - c)``, i.e. ``strategy.size_position``."""
    vals = [_num(v) for v in (p, gain, loss, equity, kelly_mult)]
    if any(v is None for v in vals):
        return 0.0
    pp, g, lo, w, k = vals  # type: ignore[misc]
    if g <= _EPS or lo <= _EPS or w <= 0 or k <= 0:
        return 0.0
    pp = max(0.0, min(1.0, pp))
    units = k * w * (pp / lo - (1.0 - pp) / g)
    return units if units > 0 else 0.0


def bet_at_cost(bet: BetShape, cost: float) -> BetShape:
    """The same bet bought at ``cost`` per unit instead of ``bet.cost`` (``p`` fixed; conservative: the true
    EV falls more slowly than p - cost under a VWAP regime): binary gain = 1 - cost, loss = cost; bracket
    gain = (bet.cost + bet.gain) - cost, loss = cost - (bet.cost - bet.loss); riskless gain = floor - cost;
    bounded and fixed bets move their gain (and a bounded bet its tail loss) by the extra cost per unit."""
    c = float(cost)
    extra = c - bet.cost
    if bet.kind == "binary":
        gain, loss = 1.0 - c, c
    elif bet.kind == "bracket":
        gain, loss = (bet.cost + bet.gain) - c, c - (bet.cost - bet.loss)
    elif bet.kind == "riskless":
        gain = (bet.floor - c) if bet.floor is not None else bet.gain - extra
        loss = 0.0
    elif bet.kind == "bounded":
        # paying more per set lowers the expected gain one for one and deepens the refund-tail loss
        gain, loss = bet.gain - extra, bet.loss + extra
    else:  # fixed: the exit target does not move with the fill price
        gain, loss = bet.gain - extra, c
    return BetShape(kind=bet.kind, p=bet.p, gain=round(gain, 10), loss=round(max(0.0, loss), 10), cost=round(c, 10),
                    floor=bet.floor, cap_pct=bet.cap_pct, tail_prob=bet.tail_prob)


def _clean_levels(levels: Optional[Sequence[Any]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for item in levels or ():
        try:
            price, qty = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if qty > 0 and math.isfinite(price) and math.isfinite(qty):
            out.append((price, qty))
    return out


def _kelly_of(bet: BetShape, equity: float, kelly_mult: float) -> float:
    return kelly_units(bet.p, bet.gain, bet.loss, equity, kelly_mult)


def depth_kelly_units(bet: BetShape, levels: Sequence[Tuple[float, float]], equity: float, kelly_mult: float,
                      upper: Optional[float] = None) -> Tuple[int, Optional[float]]:
    """(units, average cost) = the largest whole ``u`` with ``u <= kelly_units(bet_at_cost(bet, avg(u)))``,
    ``avg(u) = depth.avg_cost(levels, u)`` and ``u <= depth.available`` of ``levels`` (and ``upper``). Binary
    search (Kelly units fall as the average cost rises). Empty ``levels``: Kelly at ``bet.cost`` (the touch),
    average None."""
    lv = _clean_levels(levels)
    if not lv:
        units = _kelly_of(bet, equity, kelly_mult)
        if upper is not None:
            units = min(units, max(0.0, float(upper)))
        return _floor(units), None
    hi_f = sum(q for _, q in lv)
    if upper is not None:
        hi_f = min(hi_f, max(0.0, float(upper)))
    # Kelly at the best level bounds every average fill from above
    hi_f = min(hi_f, _kelly_of(bet_at_cost(bet, lv[0][0]), equity, kelly_mult))
    hi = _floor(hi_f)

    def ok(u: int) -> bool:
        avg = depth.avg_cost(lv, u)
        if avg is None:
            return False
        return u <= _kelly_of(bet_at_cost(bet, avg), equity, kelly_mult) + 1e-9

    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo, (depth.avg_cost(lv, lo) if lo > 0 else None)


def certainty(edge: Optional[float], sigma: Optional[float]) -> float:
    """``edge^2 / (edge^2 + sigma^2)`` (1.0 when sigma is 0 or None; 0.0 when edge <= 0)."""
    e = _num(edge)
    if e is None or e <= 0:
        return 0.0
    s = _num(sigma)
    if s is None or s <= 0:
        return 1.0
    return e * e / (e * e + s * s)


def factor_scale(n_same: int, rho: float = FACTOR_RHO) -> float:
    """``1 / (1 + (n_same - 1) x rho)``: each of ``n_same`` same-direction ideas gets this share of its own
    Kelly size, so together they are about one Kelly bet on the national swing."""
    n = max(1, int(n_same or 1))
    return 1.0 / (1.0 + (n - 1) * max(0.0, float(rho)))


def bold_max_cost(bar: float, equity: float, cap_pct: float = SWING_BOLD_CAP_PCT) -> float:
    """The highest contract cost whose capped bold stake still reaches the bar:
    ``cap x W / (bar - W + cap x W)`` (W = 100,000, bar = 221,500, cap 60% -> 0.3306)."""
    w = float(equity)
    denom = float(bar) - w + cap_pct * w
    if w <= 0 or denom <= 0:
        return 1.0
    return min(1.0, cap_pct * w / denom)


def bold_exit_plan(opp: Opportunity) -> ExitPlan:
    """The exit plan a bold-to-goal position must use (§5.6.4, D13): held to its 1/0 settlement. No target, no
    dynamic fair-value target, no stop and no time stop; only the idea's regime exit (``exit_before_ts``: flat
    before a VWAP closeout window) is kept. The bold stake is sized so that a 1/0 WIN reaches the bar, so a
    convergence or bracket exit would turn a many-times-Kelly stake into a few cents a share."""
    old = opp.exit_plan
    before = old.exit_before_ts if old is not None else None
    note = BOLD_HOLD_NOTE
    if before is not None:
        note += "; still sold before the Cup's closeout window, as the settlement regime requires"
    return ExitPlan(kind="settle", hold_to_resolution=True, exit_before_ts=before, note=note)


def markov_ceiling(ev_multiple: float, m: float) -> float:
    """Upper bound on P(final >= m x now) for a non-negative wealth whose expected multiple is
    ``ev_multiple``: ``min(1, ev_multiple / m)``."""
    mm = _num(m)
    ev = _num(ev_multiple)
    if mm is None or mm <= 0 or ev is None:
        return 1.0
    return max(0.0, min(1.0, ev / mm))


def markov_sentence(ev_multiple: float, m: float) -> str:
    """Exact: "A strategy whose expected multiple is {ev:.2f}x reaches {m:.1f}x with probability at most
    {p:.0%}; with no edge (fair prices) the bound is 1/M = {q:.0%}." """
    p = markov_ceiling(ev_multiple, m)
    q = markov_ceiling(1.0, m)
    return (f"A strategy whose expected multiple is {ev_multiple:.2f}x reaches {m:.1f}x with probability at most "
            f"{p:.0%}; with no edge (fair prices) the bound is 1/M = {q:.0%}.")


# --------------------------------------------------------------------------- bar estimate


def _entry_values(snapshot: Optional[LeaderboardSnapshot], initial: float) -> List[float]:
    if snapshot is None:
        return []
    out: List[float] = []
    for entry in snapshot.entries or []:
        if not isinstance(entry, dict):
            continue
        value = _num(entry.get("value"))
        if value is None:
            pnl = _num(entry.get("pnl"))
            value = initial + pnl if pnl is not None else None
        if value is not None:
            out.append(value)
    out.sort(reverse=True)
    return out


def _nth(values: Sequence[float], n: int) -> Optional[float]:
    return values[n - 1] if len(values) >= n else None


def estimate_bar(leaderboard: Optional[LeaderboardSnapshot], history: Sequence[LeaderboardSnapshot],
                 initial_balance: Optional[float], days_left: float) -> BarEstimate:
    """The final top-3 balance a chaser must clear (docs/PAPER_TRADING.md §5.6.3, revision 2).

    third = the 3rd-highest ``value`` (initial + pnl); floor = BAR_FLOOR_MULT x initial; ``low = max(third,
    floor)`` and ``value = low`` (sizing uses the lower end). Growth (shown only): for each rank in
    BAR_GROWTH_RANKS present now and in the OLDEST history snapshot at least BAR_MIN_HISTORY_S older, the
    LINEAR daily growth ``(v_now / v_then - 1) / days``; ``growth_per_day`` = their median clipped to
    [0, BAR_MAX_GROWTH_PER_DAY]; ``high = min(third x (1 + g x days_left), BAR_CAP_MULT x initial)``. Never
    compounds. Without a leaderboard: the floor, method "floor". Explanation lines always say that mid-Cup
    values are marks, not settled balances, give the range and say the lower end is used."""
    initial = _num(initial_balance)
    if (initial is None or initial <= 0) and leaderboard is not None:
        initial = _num(leaderboard.initial_balance)
    if initial is None or initial <= 0:
        initial = DEFAULT_EQUITY
    days = max(0.0, _num(days_left) or 0.0)
    floor = BAR_FLOOR_MULT * initial
    cap = BAR_CAP_MULT * initial
    now_values = _entry_values(leaderboard, initial)
    third, tenth, hundredth = _nth(now_values, 3), _nth(now_values, 10), _nth(now_values, 100)
    if third is not None and third >= floor:
        low, method = third, "third"
    else:
        low, method = floor, "floor"

    growth: Optional[float] = None
    history_days: Optional[float] = None
    if leaderboard is not None and third is not None:
        older = [h for h in history or () if isinstance(h, LeaderboardSnapshot) and h.at < leaderboard.at - 1e-6]
        if older:
            oldest_any = min(older, key=lambda h: h.at)
            history_days = round((leaderboard.at - oldest_any.at) / 86400.0, 4)
        eligible = [h for h in older if h.at <= leaderboard.at - BAR_MIN_HISTORY_S + 1e-6]
        if eligible:
            then = min(eligible, key=lambda h: h.at)
            span_days = (leaderboard.at - then.at) / 86400.0
            then_values = _entry_values(then, initial)
            rates: List[float] = []
            for rank in BAR_GROWTH_RANKS:
                v_now, v_then = _nth(now_values, rank), _nth(then_values, rank)
                if v_now is None or v_then is None or v_then <= 0 or span_days <= 0:
                    continue
                rates.append((v_now / v_then - 1.0) / span_days)
            if rates:
                growth = max(0.0, min(BAR_MAX_GROWTH_PER_DAY, float(median(rates))))
                history_days = round(span_days, 4)
    if growth is not None and third is not None:
        high = min(third * (1.0 + growth * days), cap)
        high = max(high, low)
    else:
        high = low

    lines = [MARKS_CAVEAT]
    if growth is not None:
        lines.append(f"Top-3 bar: {_money(low)} now; at the recent pace of {growth:+.1%} a day it could reach "
                     f"{_money(high)} by the Cup end. Sizing uses the lower end.")
    else:
        why = ("no leaderboard is known" if leaderboard is None else
               "3rd place is unknown" if third is None else
               "less than 3 days of leaderboard history")
        lines.append(f"Top-3 bar: {_money(low)} now (no growth estimate: {why}). Sizing uses the lower end.")
    if leaderboard is None:
        lines.append(f"No leaderboard yet: the bar is the floor of {BAR_FLOOR_MULT:g}x the initial balance "
                     f"({_money(floor)}).")
    elif method == "third":
        lines.append(f"The bar is 3rd place's current value ({_money(third or 0.0)}), never less than "
                     f"{BAR_FLOOR_MULT:g}x the initial balance ({_money(floor)}).")
    elif third is None:
        lines.append(f"3rd place's value is unknown, so the bar is the floor of {BAR_FLOOR_MULT:g}x the initial "
                     f"balance ({_money(floor)}).")
    else:
        lines.append(f"3rd place's value ({_money(third)}) is below the floor of {BAR_FLOOR_MULT:g}x the initial "
                     f"balance, so the bar is the floor ({_money(floor)}).")
    est = BarEstimate(value=round(low, 2), third_value=third, tenth_value=tenth, hundredth_value=hundredth,
                      growth_per_day=round(growth, 6) if growth is not None else None, projected=False,
                      explanation=lines, low=round(low, 2), high=round(high, 2), history_days=history_days)
    est.method = method  # set as an attribute: the read-only AST check reserves the ``method=`` keyword for HTTP
    return est


# --------------------------------------------------------------------------- the sizing core


@dataclass
class _Rules:
    """The numbers one policy (or chaser mode) sizes with."""

    name: str
    kelly_mult: float
    idea_cap: float
    set_cap: float
    total_cap: float
    race_cap: float
    swing_cap: Optional[float]
    swing_name: str = "tilt_cap"
    top_k: Optional[int] = None
    certainty_all: bool = False  # chaser: every directional idea; conservative: value ideas only
    unknown_depth_max: Optional[float] = None
    factor: bool = False
    mode: Optional[str] = None


CONSERVATIVE_RULES = _Rules("conservative", KELLY_MULT, IDEA_CAP_PCT, RISKLESS_CAP_PCT, TOTAL_CAP_PCT, RACE_CAP_PCT,
                            TILT_CAP_PCT)


@dataclass
class _State:
    """Exposure as it stands while ``size_all`` works down the list."""

    gross: float
    by_race: Dict[str, float]
    tilt: float
    free_cash: float
    directional: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, ctx: SizingContext) -> "_State":
        exp = ctx.exposure
        return cls(gross=float(exp.gross_cost or 0.0), by_race=dict(exp.by_race or {}),
                   tilt=float(exp.national_tilt_d or 0.0), free_cash=float(ctx.free_cash),
                   directional=dict(exp.directional or {}))


def _stake_units(amount: float, levels: List[Tuple[float, float]], touch: float) -> float:
    """The most units whose expected cost (``u x avg(u)``, else ``u x touch``) fits in ``amount`` (units past
    the book are priced at its last level, as ``depth.avg_cost`` does: the depth cap is separate)."""
    if amount <= 0:
        return 0.0
    if not levels:
        return amount / touch if touch > 0 else float("inf")
    cheapest = min(p for p, _ in levels)
    lo, top = 0, _floor(amount / max(cheapest, 1e-6)) + 1
    while lo < top:
        mid = (lo + top + 1) // 2
        avg = depth.avg_cost(levels, mid) or touch
        if mid * avg <= amount + 1e-6:
            lo = mid
        else:
            top = mid - 1
    return float(lo)


def _swing_units(tilt: float, delta: float, limit: float) -> float:
    """Most units of a position with ``delta`` per unit before ``|tilt|`` passes ``limit``: ``(limit - s x tilt) /
    |delta|`` with ``s`` the sign of ``delta``. A position against an over-cap tilt is always allowed to reduce it,
    through zero, and then only up to ``limit`` on the OTHER side (not to the old ``|tilt|``: a tilt of -9,000 with a
    3,333 cap may go to +3,333, never to +9,000); a position adding to an over-cap tilt gets 0."""
    if abs(delta) <= 1e-12:
        return float("inf")
    s = 1.0 if delta > 0 else -1.0
    return max(0.0, (max(0.0, limit) - s * tilt) / abs(delta))


def _zero(opp: Opportunity, policy: str, mode: Optional[str], reason: str, capped: str) -> SizeDecision:
    return SizeDecision(idea_id=_idea_id(opp), policy=policy, units=0, stake=0.0, capped_by=[capped], mode=mode,
                        lines=[reason])


def _side_text(opp: Opportunity) -> str:
    if opp.unit == "sets" or opp.legs:
        return f"the {len(opp.legs) or 2}-leg set"
    return (opp.side or "yes").upper()


def _size_one(opp: Opportunity, ctx: SizingContext, rules: _Rules, state: _State, policy: str, *,
              n_same: Optional[int] = None, label: str = "") -> SizeDecision:
    """Size one idea against ``state`` (§5.6.1). Never mutates the state."""
    mode = rules.mode
    unit = _unit(opp)
    bet = opp.bet
    if opp.kind == "watch" or bet is None:
        return _zero(opp, policy, mode, "No size: this idea has no payoff to size (a watch item, or prices that "
                                        "could not be checked)", "zero_edge")
    w = _num(ctx.equity) or 0.0
    if w <= 0:
        return _zero(opp, policy, mode, "No size: the portfolio has no equity left", "cash")
    edge = _num(opp.edge)
    if edge is None or edge <= _EPS:
        return _zero(opp, policy, mode, "No size: no edge left at these prices", "zero_edge")
    touch = _num(bet.cost) or 0.0
    if touch <= 0:
        return _zero(opp, policy, mode, "No size: the price is unknown", "zero_edge")
    params = _params(ctx)
    levels = _clean_levels(opp.levels)
    limit = _num(opp.limit_price) or touch
    available = sum(q for _, q in levels)
    max_units = _num(opp.max_units)
    depth_cap = max_units if max_units is not None else (available if levels else float("inf"))
    floor_v = _num(bet.floor)
    all_coll = bool(ctx.all_collateral) and bet.kind == "riskless" and floor_v is not None
    cash_per = max(0.0, limit - floor_v) if all_coll else limit  # the engine reserves at the limit
    free = max(0.0, state.free_cash)
    cash_cap = free / cash_per if cash_per > 1e-12 else float("inf")
    race = _race_key(opp, ctx)
    held_race = float(state.by_race.get(race, 0.0))

    def avg_at(u: float) -> float:
        if levels and u > 0:
            return depth.avg_cost(levels, u) or touch
        return touch

    caps: Dict[str, float] = {}
    lines: List[str] = []
    cert: Optional[float] = None
    fscale: Optional[float] = None
    kelly_fraction: Optional[float] = None
    k_base = rules.kelly_mult
    directional = bet.kind in DIRECTIONAL_BETS
    delta = _num(opp.factor_delta) or 0.0
    swing_limit = None

    if directional or bet.kind == "bounded":
        k = k_base
        notes: List[str] = []
        if directional and (opp.kind == "value" or rules.certainty_all):
            sigma = _num(opp.fv_uncertainty)
            if sigma is None:
                sigma = DEFAULT_SIGMA.get(opp.kind, 0.0)
            cert = certainty(edge, sigma)
            k *= cert
            if sigma > 0:
                notes.append(f"uncertainty {_px(sigma)}: {cert:.0%} certainty")
        sign = _sign(delta)
        if directional and rules.factor and sign != 0:
            n = n_same if n_same is not None else sum(
                1 for s in (ctx.exposure.directional_sign or {}).values() if s == sign) + 1
            fscale = factor_scale(n, FACTOR_RHO)
            k *= fscale
            if n > 1:
                lean = "Democratic" if sign > 0 else "Republican"
                lines.append(f"{n} {lean}-leaning ideas share one national polling error: each gets {fscale:.0%} of its "
                             "own Kelly size, so together they are about one Kelly bet on the swing")
        if directional and rules.unknown_depth_max is not None and not levels and k > rules.unknown_depth_max + 1e-12:
            k = rules.unknown_depth_max
            lines.append(f"Depth unknown: at most {_kelly_label(k)}, because the fill may walk up to the "
                         f"{_px(limit)} limit")
        # Kelly on the expected average fill, NOT clipped to the book (the depth cap below says when the book binds):
        # units past the displayed depth are priced at its last level
        kelly_levels = levels + [(levels[-1][0], 1e12)] if levels else []
        kelly_u, _ = depth_kelly_units(bet, kelly_levels, w, k)
        caps["kelly"] = float(kelly_u)
        touch_units = _kelly_of(bet, w, k)
        kelly_fraction = round(touch_units * bet.loss / w, 6) if bet.loss > 0 else None
        what = {"value": " with a fair value of " + _px(opp.fair_value) if opp.fair_value is not None else "",
                "fade": " on the target/stop bracket", "carry": " held to settlement"}.get(opp.kind, "")
        if bet.kind == "bounded":
            lines.insert(0, f"Not riskless (a refund on one leg, or an uncalled race under a VWAP closeout, breaks the "
                            f"floor): {_kelly_label(k)} on the bounded bet allows {kelly_u:,} {unit}")
        else:
            detail = ", ".join([f"edge {edge:+.3f}"] + notes)
            lines.insert(0, f"{_cap(_kelly_label(k_base))} on {_side_text(opp)}{what} ({detail}): {kelly_u:,} {unit}")
    if directional:
        caps["idea_cap"] = _stake_units(rules.idea_cap * w, levels, touch)
        caps["race_cap"] = _stake_units(rules.race_cap * w - held_race, levels, touch)
        if rules.swing_cap is not None and abs(delta) > 1e-12:
            swing_limit = rules.swing_cap * w / max(params.national_swing_sd_pts, 1e-9)
            caps[rules.swing_name] = _swing_units(state.tilt, delta, swing_limit)
    elif bet.kind == "riskless":
        caps["basket_cap"] = _stake_units(rules.set_cap * w, levels, touch)
        lines.append(f"Riskless at a 1/0 settlement: sized at the {rules.set_cap:.0%}-of-equity cap per set idea, not "
                     "by Kelly")
        if all_coll:
            caps["notional_cap"] = _stake_units(ALL_COLLATERAL_NOTIONAL_PCT * w, levels, touch)
            lines.append(f"ALL collateral: each set needs only {_px(cash_per)} in cash (the limit {_px(limit)} minus "
                         f"its {_px(floor_v)} floor), notional capped at {ALL_COLLATERAL_NOTIONAL_PCT:.0%} of equity")
    elif bet.kind == "bounded":
        caps["basket_cap"] = _stake_units(rules.set_cap * w, levels, touch)
    elif bet.kind == "fixed":
        cap_pct = _num(bet.cap_pct) or 0.0
        caps["idea_cap"] = _stake_units(cap_pct * w, levels, touch)
        caps["race_cap"] = _stake_units(rules.race_cap * w - held_race, levels, touch)
        lines.append(f"A small resting order: at most {cap_pct:.0%} of equity"
                     + (f" and {max_units:,.0f} {unit}" if max_units is not None else "") + ", no Kelly sizing")
    else:
        return _zero(opp, policy, mode, f"No size: unknown payoff shape {bet.kind!r}", "zero_edge")
    caps["total_cap"] = _stake_units(rules.total_cap * w - state.gross, levels, touch)
    caps["cash"] = cash_cap
    if math.isfinite(depth_cap):
        caps["depth"] = depth_cap
    best = min(caps.values()) if caps else 0.0
    units = _floor(best)
    binding = [name for name, value in caps.items() if _floor(value) <= units]
    avg = avg_at(units) if units > 0 else None
    cost_per = avg if avg is not None else touch
    stake_per = max(0.0, cost_per - floor_v) if all_coll else cost_per
    stake = round(units * stake_per, 2)

    if levels and avg is not None and abs(avg - touch) > 1e-9:
        lines.append(f"Sized on the expected average fill of {_px(round(avg, 4))} across the book, not the "
                     f"{_px(touch)} touch")
    elif not levels and opp.order_type != "maker" and max_units is None:
        lines.append(f"Depth unknown: sized at the {_px(touch)} touch (a fill may walk up to the {_px(limit)} limit)")
    extra: List[str] = []
    for name in binding:
        if name == "kelly":
            if cert is not None and cert < 0.999:
                extra.append("certainty")
            if fscale is not None and fscale < 0.999:
                extra.append("factor")
            continue
        if name == "idea_cap":
            if bet.kind == "fixed":
                lines.append(f"Capped at {_num(bet.cap_pct) or 0:.0%} of equity for one resting order")
            else:
                lines.append(f"Capped at {rules.idea_cap:.0%} of equity per idea")
        elif name == "basket_cap":
            lines.append(f"Capped at {rules.set_cap:.0%} of equity per set idea")
        elif name == "notional_cap":
            lines.append(f"Capped at {ALL_COLLATERAL_NOTIONAL_PCT:.0%} of equity in notional (ALL collateral)")
        elif name == "race_cap":
            lines.append(f"Capped at {rules.race_cap:.0%} of equity per race ({race} already holds "
                         f"{_money(held_race)} SUSQies)")
        elif name in ("tilt_cap", "swing_cap"):
            after = state.tilt + units * delta
            toward = "Republicans" if after > 0 else "Democrats"
            loss = abs(after) * params.national_swing_sd_pts
            lines.append(f"A {params.national_swing_sd_pts:g}-point national swing toward the {toward} would cost this "
                         f"portfolio {_money(loss)} SUSQies: capped at {rules.swing_cap:.0%} of equity")
        elif name == "total_cap":
            lines.append(f"Capped at {rules.total_cap:.0%} of equity in total ({_money(state.gross)} SUSQies already "
                         "committed)")
        elif name == "cash":
            lines.append(f"Capped by the free cash ({_money(free)} SUSQies left after pending orders)")
        elif name == "depth":
            if opp.order_type == "maker":
                lines.append(f"At most {depth_cap:,.0f} {unit} rest in one order")
            elif levels or max_units is None or opp.kind != "fade":
                lines.append(f"The book offers {depth_cap:,.0f} {unit} up to {_px(limit)}")
            else:
                lines.append(f"About {depth_cap:,.0f} {unit} rested near the mid when the surge was analysed: capped "
                             "there (no recent order book)")
    binding.extend(extra)
    prefix = f"{label}: " if label else ""
    head = f"{prefix}{units:,} {unit} for about {_money(stake)} SUSQies" if units > 0 else f"{prefix}nothing to buy"
    lines.insert(0, head)
    if units <= 0 and not binding:
        binding = ["zero_edge"]
    return SizeDecision(idea_id=_idea_id(opp), policy=policy, units=units, stake=stake, kelly_fraction=kelly_fraction,
                        capped_by=binding, mode=mode, lines=lines, avg_cost=round(avg, 6) if levels and avg else None,
                        certainty=round(cert, 6) if cert is not None else None,
                        factor_scale=round(fscale, 6) if fscale is not None else None)


def _apply(state: _State, opp: Opportunity, dec: SizeDecision, ctx: SizingContext) -> None:
    """Book a decision into the running exposure (cost at the expected fill, cash reserved at the limit)."""
    if dec.units <= 0 or opp.bet is None:
        return
    touch = opp.bet.cost
    cost = dec.units * (dec.avg_cost or touch)
    state.gross += cost
    race = _race_key(opp, ctx)
    state.by_race[race] = state.by_race.get(race, 0.0) + cost
    if opp.bet.kind not in SET_BETS:
        state.tilt += dec.units * (_num(opp.factor_delta) or 0.0)
    limit = _num(opp.limit_price) or touch
    floor_v = _num(opp.bet.floor)
    if ctx.all_collateral and opp.bet.kind == "riskless" and floor_v is not None:
        state.free_cash -= dec.units * max(0.0, limit - floor_v)
    else:
        state.free_cash -= dec.units * limit
    if opp.bet.kind in DIRECTIONAL_BETS:
        state.directional[_idea_id(opp)] = float(opp.score or 0.0)


def _ordered(opps: Sequence[Opportunity]) -> List[Opportunity]:
    return sorted(opps, key=lambda o: (-(o.score or 0.0), _idea_id(o)))


def _bar_fields(ctx: SizingContext) -> Dict[str, Any]:
    bar = ctx.bar
    w = _num(ctx.equity) or 0.0
    ev = 1.0 + (_num(ctx.expected_profit) or 0.0) / w if w > 0 else None
    m = bar.value / w if bar is not None and w > 0 else None
    return {
        "bar": bar.to_dict() if bar is not None else None,
        "M": round(m, 4) if m is not None else None,
        "markov_ceiling": round(markov_ceiling(ev, m), 4) if m is not None and ev is not None else None,
        "ev_multiple": round(ev, 4) if ev is not None else None,
    }


# --------------------------------------------------------------------------- policies


class ConservativePolicy:
    name = POLICY_CONSERVATIVE
    label = "Conservative: quarter-Kelly, at most 8% of equity per idea"

    def __init__(self, *, kelly_mult: float = KELLY_MULT, idea_cap: float = IDEA_CAP_PCT,
                 riskless_cap: float = RISKLESS_CAP_PCT, total_cap: float = TOTAL_CAP_PCT,
                 race_cap: float = RACE_CAP_PCT, tilt_cap: float = TILT_CAP_PCT) -> None:
        self.kelly_mult = kelly_mult
        self.idea_cap = idea_cap
        self.riskless_cap = riskless_cap
        self.total_cap = total_cap
        self.race_cap = race_cap
        self.tilt_cap = tilt_cap

    def _rules(self) -> _Rules:
        return _Rules("conservative", self.kelly_mult, self.idea_cap, self.riskless_cap, self.total_cap, self.race_cap,
                      self.tilt_cap)

    def size(self, opp: Opportunity, ctx: SizingContext) -> SizeDecision:
        return _size_one(opp, ctx, self._rules(), _State.of(ctx), self.name, label="Conservative sizing")

    def size_all(self, opps: Sequence[Opportunity], ctx: SizingContext) -> Dict[str, SizeDecision]:
        rules = self._rules()
        state = _State.of(ctx)
        out: Dict[str, SizeDecision] = {}
        for opp in _ordered(opps):
            key = _idea_id(opp)
            if key in out:
                continue
            dec = _size_one(opp, ctx, rules, state, self.name, label="Conservative sizing")
            _apply(state, opp, dec, ctx)
            out[key] = dec
        return out

    def explain(self, ctx: SizingContext) -> Dict[str, Any]:
        lines = list(CONSERVATIVE_LINES)
        if (self.kelly_mult, self.idea_cap, self.race_cap, self.total_cap) != (KELLY_MULT, IDEA_CAP_PCT, RACE_CAP_PCT,
                                                                                TOTAL_CAP_PCT):
            lines[0] = (f"{_cap(_kelly_label(self.kelly_mult))} on the expected average fill, at most {self.idea_cap:.0%} "
                        f"of equity per idea, {self.race_cap:.0%} per race, {self.total_cap:.0%} in total")
        out: Dict[str, Any] = {"policy": self.name, "label": self.label, "mode": None, "lines": lines}
        out.update(_bar_fields(ctx))
        return out


_MODE_TABLES = {"near": NEAR, "chase": CHASE, "swing": SWING, "unknown_bar": UNKNOWN_BAR}


def _chaser_rules(mode: str) -> _Rules:
    if mode == "out_of_reach":
        rules = _Rules("conservative", KELLY_MULT, IDEA_CAP_PCT, RISKLESS_CAP_PCT, TOTAL_CAP_PCT, RACE_CAP_PCT,
                       TILT_CAP_PCT)
        rules.mode = mode
        return rules
    table = _MODE_TABLES[mode]
    return _Rules(mode, float(table["kelly_mult"]), float(table["idea_cap"]), CHASER_BASKET_CAP_PCT,
                  float(table["total_cap"]), float(table["race_cap"]),
                  None if table.get("swing_cap") is None else float(table["swing_cap"]), swing_name="swing_cap",
                  top_k=int(table["top_k"]) if table.get("top_k") else None, certainty_all=True,
                  unknown_depth_max=UNKNOWN_DEPTH_MAX_KELLY, factor=True, mode=mode)


def _mode_rules_text(mode: str) -> str:
    if mode == "out_of_reach":
        return "conservative sizing (quarter-Kelly, at most 8% of equity per idea, 12% per race, 60% in total)"
    t = _MODE_TABLES[mode]
    parts = [_kelly_label(float(t["kelly_mult"])), f"at most {float(t['idea_cap']):.0%} of equity per idea",
             f"{float(t['race_cap']):.0%} per race", f"{float(t['total_cap']):.0%} in total",
             f"{CHASER_BASKET_CAP_PCT:.0%} per set idea"]
    if t.get("swing_cap") is not None:
        parts.append(f"a 3-point national swing may cost at most {float(t['swing_cap']):.0%} of equity")
    else:
        parts.append("no swing cap")
    if t.get("top_k"):
        parts.append(f"at most {int(t['top_k'])} directional ideas at once")
    if mode == "swing":
        parts.append("plus at most one bold-to-goal bet")
    return ", ".join(parts)


class ChaserPolicy:
    name = POLICY_CHASER
    label = "Chaser: goal-based sizing to clear the top-3 bar"

    # ---- mode
    @staticmethod
    def _raw_mode(m: Optional[float], days_left: float) -> str:
        if m is None:
            return "unknown_bar"
        if m <= NEAR_M:
            return "near"
        if m <= OUT_OF_REACH_M:
            return "chase" if days_left > SWING_DAYS else "swing"
        return "out_of_reach"

    def _mode(self, ctx: SizingContext) -> Tuple[str, bool, Optional[float]]:
        """(mode, kept by hysteresis, M)."""
        w = _num(ctx.equity) or 0.0
        m = ctx.bar.value / w if ctx.bar is not None and w > 0 else None
        days = max(0.0, _num(ctx.days_left) or 0.0)
        raw = self._raw_mode(m, days)
        prev = ctx.prev_mode
        if prev is None or prev == raw or m is None:
            return raw, False, m
        if raw in ("swing", "unknown_bar") or prev in ("swing", "unknown_bar") or prev not in (
                "near", "chase", "out_of_reach"):
            return raw, False, m
        held = ctx.mode_since is None or (ctx.now - ctx.mode_since) >= MODE_MIN_HOLD_S - 1e-6
        # thresholds pushed away from the current mode by MODE_BAND
        if prev == "near":
            lo, hi = NEAR_M * (1 + MODE_BAND), OUT_OF_REACH_M * (1 + MODE_BAND)
        elif prev == "chase":
            lo, hi = NEAR_M * (1 - MODE_BAND), OUT_OF_REACH_M * (1 + MODE_BAND)
        else:  # out_of_reach
            lo, hi = NEAR_M * (1 - MODE_BAND), OUT_OF_REACH_M * (1 - MODE_BAND)
        banded = "near" if m <= lo else ("out_of_reach" if m > hi else "chase")
        if banded == "chase" and days <= SWING_DAYS:
            banded = "swing"
        if banded == prev or not held:
            return prev, True, m
        return banded, False, m

    def mode(self, ctx: SizingContext) -> str:
        """"near" | "chase" | "swing" | "out_of_reach" | "unknown_bar" with hysteresis (§5.6.4): from
        ``ctx.prev_mode`` a switch across the NEAR_M / OUT_OF_REACH_M thresholds needs M beyond the
        threshold by MODE_BAND and ``now - ctx.mode_since >= MODE_MIN_HOLD_S``."""
        return self._mode(ctx)[0]

    # ---- sizing
    def size(self, opp: Opportunity, ctx: SizingContext) -> SizeDecision:
        return self._size_many([opp], ctx, single=True)[_idea_id(opp)]

    def size_all(self, opps: Sequence[Opportunity], ctx: SizingContext) -> Dict[str, SizeDecision]:
        return self._size_many(opps, ctx, single=False)

    def _size_many(self, opps: Sequence[Opportunity], ctx: SizingContext, single: bool) -> Dict[str, SizeDecision]:
        mode, kept, m = self._mode(ctx)
        rules = _chaser_rules(mode)
        state = _State.of(ctx)
        label = f"Chaser sizing ({mode.replace('_', ' ')})"
        ordered = _ordered(opps)
        out: Dict[str, SizeDecision] = {}
        if mode == "out_of_reach":
            for opp in ordered:
                key = _idea_id(opp)
                if key in out:
                    continue
                dec = _size_one(opp, ctx, rules, state, self.name, label=label)
                dec.lines.append(OUT_OF_REACH_LINE)
                _apply(state, opp, dec, ctx)
                out[key] = dec
            return out

        def tradeable_directional(o: Opportunity) -> bool:
            return (o.bet is not None and o.bet.kind in DIRECTIONAL_BETS and o.kind != "watch"
                    and (_num(o.edge) or 0.0) > _EPS)

        new_dir: List[Opportunity] = []
        seen: set = set()
        for o in ordered:
            key = _idea_id(o)
            if tradeable_directional(o) and key not in seen and key not in (ctx.exposure.directional or {}):
                new_dir.append(o)
                seen.add(key)
        held = dict(ctx.exposure.directional or {})
        slots: Optional[int] = None
        if rules.top_k is not None:
            slots = max(0, rules.top_k - len(held))
        selected = new_dir if slots is None else new_dir[:slots]
        rejected = set() if slots is None else {_idea_id(o) for o in new_dir[slots:]}
        replacement: Optional[Tuple[str, str, float, float]] = None
        if slots == 0 and new_dir and held:
            weakest_id, weakest = min(held.items(), key=lambda kv: (kv[1], kv[0]))
            best = new_dir[0]
            if (best.score or 0.0) > weakest * (1.0 + REPLACE_MARGIN) + 1e-12:
                replacement = (_idea_id(best), weakest_id, float(best.score or 0.0), float(weakest))
        held_signs: Dict[int, int] = {}
        for sign in (ctx.exposure.directional_sign or {}).values():
            s = _sign(sign)
            if s:
                held_signs[s] = held_signs.get(s, 0) + 1
        new_signs: Dict[int, int] = {}
        for o in selected:
            s = _sign(o.factor_delta)
            if s:
                new_signs[s] = new_signs.get(s, 0) + 1

        bold_id: Optional[str] = None
        bold_plan: Optional[Tuple[int, float, float, bool]] = None
        no_bold_line: Optional[str] = None
        if mode == "swing" and ctx.exposure.bold_idea is None and ctx.bar is not None:
            bold_id, bold_plan, no_bold_line = self._pick_bold(selected, ctx, state)
        if bold_id is not None and bold_plan is not None:
            # the bold bet is funded FIRST: it was qualified on the cash and room left now, and better-scored ideas
            # sized before it must not eat the stake that lets its win reach the bar
            bold_opp = next(o for o in ordered if _idea_id(o) == bold_id)
            dec = self._bold_decision(bold_opp, ctx, state, bold_plan, label)
            _apply(state, bold_opp, dec, ctx)
            state.directional[bold_id] = float(bold_opp.score or 0.0)
            out[bold_id] = dec

        for opp in ordered:
            key = _idea_id(opp)
            if key in out:
                continue
            if replacement is not None and key == replacement[0]:
                dec = SizeDecision(idea_id=key, policy=self.name, units=0, stake=0.0, capped_by=["top_k"], mode=mode,
                                   replaces=replacement[1],
                                   lines=[f"{label}: nothing to buy yet",
                                          f"Better than the weakest held idea (score {replacement[2]:.2f} vs "
                                          f"{replacement[3]:.2f}): sell that one first"])
            elif key in rejected:
                dec = SizeDecision(idea_id=key, policy=self.name, units=0, stake=0.0, capped_by=["top_k"], mode=mode,
                                   lines=[f"{label}: nothing to buy",
                                          f"The {rules.top_k} directional slots are taken by better-scored ideas "
                                          f"({len(held)} held or pending): concentrate on the best edges"])
            else:
                sign = _sign(opp.factor_delta)
                n = None
                if tradeable_directional(opp) and sign:
                    # every same-direction idea held, pending or selected in this call shares one factor
                    n = held_signs.get(sign, 0) + max(1, new_signs.get(sign, 0))
                dec =_size_one(opp, ctx, rules, state, self.name, n_same=n, label=label)
                if no_bold_line and tradeable_directional(opp):
                    dec.lines.append(no_bold_line)
            _apply(state, opp, dec, ctx)
            out[key] = dec
        # best score first, as before (the bold bet was only SIZED first)
        return {_idea_id(o): out[_idea_id(o)] for o in ordered if _idea_id(o) in out}

    @staticmethod
    def _bold_price(opp: Opportunity, units: int) -> Tuple[float, bool]:
        """(cost per unit at ``units``, priced at the limit). Known depth: the expected average fill. Unknown depth:
        the order's limit, the worst fill it accepts, so the win state is not overstated."""
        touch = opp.bet.cost if opp.bet is not None else 0.0
        levels = _clean_levels(opp.levels)
        if levels:
            return (depth.avg_cost(levels, units) if units > 0 else None) or touch, False
        limit = _num(opp.limit_price)
        if limit is not None and limit > touch + 1e-12:
            return limit, True
        return touch, False

    def _bold_goal(self, opp: Opportunity, gap: float) -> Tuple[int, float, bool]:
        """(units, cost per unit, priced at the limit) of the stake whose 1/0 win adds ``gap``: ``units =
        floor(gap / (1 - c))`` with ``c`` the cost at that size (iterated on the book's average fill)."""
        c, at_limit = self._bold_price(opp, 0)
        if c >= 1.0 - 1e-9:
            return 0, c, at_limit
        units = _floor(gap / (1.0 - c))
        for _ in range(30):
            c2, at_limit = self._bold_price(opp, units)
            if c2 >= 1.0 - 1e-9:
                return 0, c2, at_limit
            u2 = _floor(gap / (1.0 - c2))
            c = c2
            if u2 == units:
                break
            units = u2
        return units, c, at_limit

    def _bold_room(self, opp: Opportunity, ctx: SizingContext, state: _State, units: int) -> Optional[str]:
        """The first cap a bold stake of ``units`` breaks ("stake_cap", "depth", "total_cap", "cash"), else None."""
        w = _num(ctx.equity) or 0.0
        c, _ = self._bold_price(opp, units)
        touch = opp.bet.cost if opp.bet is not None else 0.0
        limit = max(_num(opp.limit_price) or touch, c)
        cost = units * c
        if cost > SWING_BOLD_CAP_PCT * w + 1e-6:
            return "stake_cap"
        max_units = _num(opp.max_units)
        if max_units is not None and units > max_units + 1e-9:
            return "depth"
        if state.gross + cost > float(SWING["total_cap"]) * w + 1e-6:
            return "total_cap"
        if units * limit > max(0.0, state.free_cash) + 1e-6:  # the engine reserves cash at the limit
            return "cash"
        return None

    def _bold_fundable(self, opp: Opportunity, ctx: SizingContext, state: _State, hi: int) -> int:
        """The most units (<= ``hi``) a bold stake can fund within every cap."""
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._bold_room(opp, ctx, state, mid) is None:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _pick_bold(self, selected: Sequence[Opportunity], ctx: SizingContext,
                   state: _State) -> Tuple[Optional[str], Optional[Tuple[int, float, float, bool]], Optional[str]]:
        """The one bold-to-goal bet (D13): the best-scored selected idea that (1) settles before the Cup end or is
        likely called by then, (2) is a binary contract held to its 1/0 result (``bold_exit_plan``), and (3) whose
        stake that reaches the bar can actually be FUNDED now: at most 60% of equity, within the free cash (reserved
        at the limit), the total cap and the book's depth, priced at the expected average fill (the limit when the
        depth is unknown). Otherwise no bold bet, and a line saying why."""
        w = _num(ctx.equity) or 0.0
        bar = ctx.bar.value if ctx.bar is not None else 0.0
        if w <= 0 or bar <= w:
            return None, None, None
        gap = bar - w
        cmax = bold_max_cost(bar, w, SWING_BOLD_CAP_PCT)
        timely = [o for o in selected if o.settles_before_cup_end is True or (_num(o.called_prob) or 0.0) >= BOLD_CALLED_MIN]
        if not timely:
            return None, None, ("No bold bet: no idea settles before the Cup end or is likely to be called by then, so "
                                "every idea is sized as in chase mode")
        candidates = [o for o in timely if o.bet is not None and o.bet.kind in BOLD_BETS]
        if not candidates:
            return None, None, ("No bold bet: the ideas that settle in time exit before settlement (a fade sells at its "
                                "target or stop), so a win could not reach the bar; every idea is sized as in chase mode")
        cheapest: Optional[float] = None
        short: Optional[Tuple[float, str, Opportunity, int]] = None  # (win state, binding cap, idea, fundable units)
        for o in candidates:
            if (_num(o.bet.cost if o.bet is not None else None) or 0.0) <= 0 or (_num(o.edge) or 0.0) <= _EPS:
                continue  # no price or no edge: nothing to size
            units, c, at_limit = self._bold_goal(o, gap)
            cheapest = c if cheapest is None else min(cheapest, c)
            if units <= 0:
                continue
            why = self._bold_room(o, ctx, state, units)
            if why is None:
                return _idea_id(o), (units, c, round(units * c, 2), at_limit), None
            if why == "stake_cap":
                continue  # too dear: the 60% stake cannot reach the bar at this price
            fund = self._bold_fundable(o, ctx, state, units)
            fc, _ = self._bold_price(o, fund)
            win_to = w + fund * (1.0 - fc)
            if short is None or win_to > short[0] + 1e-9:
                short = (win_to, why, o, fund)
        if short is not None:
            win_to, why, o, fund = short
            touch = o.bet.cost if o.bet is not None else 0.0
            limit = _num(o.limit_price) or touch
            if why == "cash":
                text = (f"the free cash ({_money(max(0.0, state.free_cash))} SUSQies, reserved at the {_px(limit)} limit) "
                        "cannot fund a stake that reaches the bar")
            elif why == "total_cap":
                text = (f"the {float(SWING['total_cap']):.0%} total cap leaves room for only "
                        f"{_money(max(0.0, float(SWING['total_cap']) * w - state.gross))} SUSQies, not a stake that "
                        "reaches the bar")
            else:
                text = (f"the book offers only {_num(o.max_units) or 0:,.0f} {_unit(o)} up to the {_px(limit)} limit, "
                        "not a stake that reaches the bar")
            return None, None, (f"No bold bet: {text} (the best candidate would win only to {_money(win_to)}, below the "
                                f"{_money(bar)} bar), so a swing would waste the variance; every idea is sized as in "
                                "chase mode")
        c = cheapest if cheapest is not None else 1.0
        win_to = w + SWING_BOLD_CAP_PCT * w * (1.0 - c) / c if c > 0 else w
        return None, None, (f"No bold bet: the cheapest qualifying contract costs {_px(round(c, 4))}; above "
                            f"{_px(round(cmax, 2))} even a {SWING_BOLD_CAP_PCT:.0%} stake cannot reach the bar (it would "
                            f"win to {_money(win_to)}), so a swing would waste the variance")

    def _bold_decision(self, opp: Opportunity, ctx: SizingContext, state: _State,
                       plan: Tuple[int, float, float, bool], label: str) -> SizeDecision:
        """The bold decision :meth:`_pick_bold` qualified: every cap was checked there, against this state."""
        units, c, stake, at_limit = plan
        w = _num(ctx.equity) or 0.0
        bar = ctx.bar.value if ctx.bar is not None else 0.0
        lines = [f"{label}: {units:,} {_unit(opp)} for about {_money(stake)} SUSQies, as the one bold bet (held to "
                 "settlement)",
                 f"Bold bet: if it wins, your value reaches the {_money(bar)} bar; it loses {_money(stake)} otherwise",
                 BOLD_HOLD_LINE]
        if at_limit:
            lines.append(f"Depth unknown: priced at the {_px(round(c, 4))} limit, the worst fill the order accepts, so a "
                         "win still reaches the bar")
        lines.append(f"One bold bet at most: two would not win together, so their win states are not joint "
                     f"(cost {_px(round(c, 4))}, at most {bold_max_cost(bar, w):.4f} to reach the bar with a "
                     f"{SWING_BOLD_CAP_PCT:.0%} stake)")
        levels = _clean_levels(opp.levels)
        return SizeDecision(idea_id=_idea_id(opp), policy=self.name, units=units, stake=stake, capped_by=["goal"],
                            mode="swing", lines=lines, avg_cost=round(c, 6) if (levels or at_limit) else None, bold=True)

    # ---- explanation
    def explain(self, ctx: SizingContext) -> Dict[str, Any]:
        mode, kept, m = self._mode(ctx)
        fields = _bar_fields(ctx)
        lines: List[str] = []
        if ctx.bar is not None:
            lines.extend(ctx.bar.explanation or [])
            w = _num(ctx.equity) or 0.0
            lines.append(f"M = the bar / your value = {_money(ctx.bar.value)} / {_money(w)} = {m:.2f}" if m is not None
                         else "M is unknown: the portfolio has no equity")
            if m is not None and fields["ev_multiple"] is not None:
                lines.append(markov_sentence(fields["ev_multiple"], m))
        else:
            lines.append("No top-3 bar is known yet (no leaderboard): the chaser sizes cautiously until it is")
        when = {"unknown_bar": "no bar is known", "near": f"M <= {NEAR_M:g}",
                "chase": f"{NEAR_M:g} < M <= {OUT_OF_REACH_M:g} with more than {SWING_DAYS:g} days left",
                "swing": f"{NEAR_M:g} < M <= {OUT_OF_REACH_M:g} in the last {SWING_DAYS:g} days",
                "out_of_reach": f"M > {OUT_OF_REACH_M:g}"}[mode]
        text = f"Mode: {mode.replace('_', ' ')} ({when}): {_mode_rules_text(mode)}"
        if kept:
            text += (f" (kept by hysteresis: leaving it needs M {MODE_BAND:.0%} past the threshold and at least "
                     f"{MODE_MIN_HOLD_S / 3600:g} h in the mode)")
        lines.append(text)
        if mode == "out_of_reach":
            lines.append(OUT_OF_REACH_LINE)
        else:
            if mode == "swing":
                lines.append("The bold bet goes only on a binary contract that settles (or is likely called) before the "
                             "Cup end, held to settlement, and only when the free cash, the total cap and the book can "
                             "fund a stake whose win reaches the bar; it is funded before the other ideas")
            lines.append("Every directional size is shrunk by how certain its edge is, and capped at half-Kelly when "
                         "the order book depth is unknown")
        lines.append(SLATE_LINE)
        lines.append(CASH_LINE)
        out: Dict[str, Any] = {"policy": self.name, "label": self.label, "mode": mode, "lines": lines}
        out.update(fields)
        out["kept_by_hysteresis"] = kept
        return out


def get_policy(name: str) -> SizingPolicy:
    """A fresh policy object by name (POLICIES); ValueError for anything else."""
    key = str(name or "").strip().lower()
    if key == POLICY_CONSERVATIVE:
        return ConservativePolicy()
    if key == POLICY_CHASER:
        return ChaserPolicy()
    raise ValueError(f"unknown sizing policy {name!r}: choose one of {', '.join(POLICIES)}")


def explanation_lines(decision: SizeDecision) -> List[str]:
    """The decision's lines, for a rationale (identity helper kept for symmetry with the UI)."""
    return list(decision.lines)


def policy_terms(policy: Any, ctx: SizingContext, opp: Optional[Opportunity] = None) -> Tuple[float, float]:
    """(Kelly multiplier, per-idea cap) the policy applies to ``opp`` now: what the legacy size sentences of
    fade / carry / arbitrage ideas quote."""
    sets = opp is not None and opp.bet is not None and opp.bet.kind in SET_BETS
    if isinstance(policy, ConservativePolicy):
        return policy.kelly_mult, (policy.riskless_cap if sets else policy.idea_cap)
    if isinstance(policy, ChaserPolicy):
        rules = _chaser_rules(policy.mode(ctx))
        return rules.kelly_mult, (rules.set_cap if sets else rules.idea_cap)
    return KELLY_MULT, IDEA_CAP_PCT
