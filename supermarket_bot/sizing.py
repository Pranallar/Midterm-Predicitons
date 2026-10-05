"""Sizing policies: how many units of an idea to buy (package B of docs/PAPER_TRADING.md §5.6, revision 2).

* :class:`ConservativePolicy`: quarter-Kelly capped at 8% of equity per idea, plus portfolio caps
  (60% gross, 12% per race, and a national-swing cap: a one-sd (3-point) national polling miss may cost
  at most 10% of equity). Units are Kelly-sized on the expected depth-walked AVERAGE fill up to the
  order's limit (``Opportunity.levels``), never on the touch alone.
* :class:`ChaserPolicy`: goal-based sizing for a player who is behind. It estimates the top-3 bar
  (:func:`estimate_bar`, from the LOWER end of its range), computes ``M = bar / own value`` and picks a
  mode with hysteresis: "near" (M <= 1.3), "chase" (1.3 < M <= 10, more than ``SWING_DAYS`` left),
  "swing" (the last ``SWING_DAYS``: at most ONE bold-to-goal bet whose capped win state reaches the bar),
  "out_of_reach" (M > 10: conservative sizing, aim for Smart Score) or "unknown_bar". In every mode the
  Kelly multiplier is shrunk by edge certainty and correlated same-party ideas are sized as one factor
  bet, not several full-Kelly bets.

Every size comes with plain-English lines saying which rule set it. Sizes never exceed the free
cash. Nothing here places orders.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    POLICY_CHASER,
    POLICY_CONSERVATIVE,
    BarEstimate,
    BetShape,
    LeaderboardSnapshot,
    Opportunity,
    SizeDecision,
    SizingContext,
    SizingPolicy,
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


def kelly_units(p: float, gain: float, loss: float, equity: float, kelly_mult: float) -> float:
    """Units that maximise growth for a bet winning ``gain`` with ``p`` and losing ``loss`` per unit:
    ``kelly_mult * equity * (p / loss - (1 - p) / gain)`` (0 when that is not positive). For a
    contract bought at ``c`` held to 1/0 this equals ``kelly_mult * f * equity / c`` with
    ``f = (p - c) / (1 - c)``, i.e. ``strategy.size_position``."""
    raise NotImplementedError


def bet_at_cost(bet: BetShape, cost: float) -> BetShape:
    """The same bet bought at ``cost`` per unit instead of ``bet.cost`` (``p`` fixed; conservative: the true
    EV falls more slowly than p - cost under a VWAP regime): binary gain = 1 - cost, loss = cost; bracket
    gain = (bet.cost + bet.gain) - cost, loss = cost - (bet.cost - bet.loss); riskless / bounded gain =
    floor - cost (bounded keeps its tail loss)."""
    raise NotImplementedError


def depth_kelly_units(bet: BetShape, levels: Sequence[Tuple[float, float]], equity: float, kelly_mult: float,
                      upper: Optional[float] = None) -> Tuple[int, Optional[float]]:
    """(units, average cost) = the largest whole ``u`` with ``u <= kelly_units(bet_at_cost(bet, avg(u)))``,
    ``avg(u) = depth.avg_cost(levels, u)`` and ``u <= depth.available`` of ``levels`` (and ``upper``). Binary
    search (Kelly units fall as the average cost rises). Empty ``levels``: Kelly at ``bet.cost`` (the touch),
    average None."""
    raise NotImplementedError


def certainty(edge: Optional[float], sigma: Optional[float]) -> float:
    """``edge^2 / (edge^2 + sigma^2)`` (1.0 when sigma is 0 or None; 0.0 when edge <= 0)."""
    raise NotImplementedError


def factor_scale(n_same: int, rho: float = FACTOR_RHO) -> float:
    """``1 / (1 + (n_same - 1) x rho)``: each of ``n_same`` same-direction ideas gets this share of its own
    Kelly size, so together they are about one Kelly bet on the national swing."""
    raise NotImplementedError


def bold_max_cost(bar: float, equity: float, cap_pct: float = SWING_BOLD_CAP_PCT) -> float:
    """The highest contract cost whose capped bold stake still reaches the bar:
    ``cap x W / (bar - W + cap x W)`` (W = 100,000, bar = 221,500, cap 60% -> 0.3306)."""
    raise NotImplementedError


def markov_ceiling(ev_multiple: float, m: float) -> float:
    """Upper bound on P(final >= m x now) for a non-negative wealth whose expected multiple is
    ``ev_multiple``: ``min(1, ev_multiple / m)``."""
    raise NotImplementedError


def markov_sentence(ev_multiple: float, m: float) -> str:
    """Exact: "A strategy whose expected multiple is {ev:.2f}x reaches {m:.1f}x with probability at most
    {p:.0%}; with no edge (fair prices) the bound is 1/M = {q:.0%}." """
    raise NotImplementedError


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
    raise NotImplementedError


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

    def size(self, opp: Opportunity, ctx: SizingContext) -> SizeDecision:
        raise NotImplementedError

    def size_all(self, opps: Sequence[Opportunity], ctx: SizingContext) -> Dict[str, SizeDecision]:
        raise NotImplementedError

    def explain(self, ctx: SizingContext) -> Dict[str, Any]:
        raise NotImplementedError


class ChaserPolicy:
    name = POLICY_CHASER
    label = "Chaser: goal-based sizing to clear the top-3 bar"

    def mode(self, ctx: SizingContext) -> str:
        """"near" | "chase" | "swing" | "out_of_reach" | "unknown_bar" with hysteresis (§5.6.4): from
        ``ctx.prev_mode`` a switch across the NEAR_M / OUT_OF_REACH_M thresholds needs M beyond the
        threshold by MODE_BAND and ``now - ctx.mode_since >= MODE_MIN_HOLD_S``."""
        raise NotImplementedError

    def size(self, opp: Opportunity, ctx: SizingContext) -> SizeDecision:
        raise NotImplementedError

    def size_all(self, opps: Sequence[Opportunity], ctx: SizingContext) -> Dict[str, SizeDecision]:
        raise NotImplementedError

    def explain(self, ctx: SizingContext) -> Dict[str, Any]:
        raise NotImplementedError


def get_policy(name: str) -> SizingPolicy:
    """A fresh policy object by name (POLICIES); ValueError for anything else."""
    raise NotImplementedError


def explanation_lines(decision: SizeDecision) -> List[str]:
    """The decision's lines, for a rationale (identity helper kept for symmetry with the UI)."""
    return list(decision.lines)
