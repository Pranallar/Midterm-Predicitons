"""Sizing policies: how many units of an idea to buy (package B of docs/PAPER_TRADING.md §5.6).

* :class:`ConservativePolicy`: the bot's original rule, quarter-Kelly capped at 8% of equity per
  idea, plus portfolio caps (60% gross, 12% per race, 30% net national tilt). It reproduces the
  legacy ``size_position`` / ``_bracket_shares`` / arbitrage sizes exactly when sizing one idea on
  its own, so the Strategy view's numbers do not change for the old idea kinds.
* :class:`ChaserPolicy`: goal-based sizing for a player who is behind. It estimates the top-3 bar
  (:func:`estimate_bar`), computes ``M = bar / own value`` and picks a mode: "near" (M <= 1.3),
  "chase" (1.3 < M <= 10, more than ``SWING_DAYS`` left: full Kelly concentrated on the best
  edges), "swing" (the last ``SWING_DAYS``: bold-to-goal on ideas that resolve by the Cup end),
  "out_of_reach" (M > 10: conservative sizing, aim for Smart Score) or "unknown_bar".

Every size comes with plain-English lines saying which rule set it. Sizes never exceed the free
cash. Nothing here places orders.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .models import (
    POLICY_CHASER,
    POLICY_CONSERVATIVE,
    BarEstimate,
    LeaderboardSnapshot,
    Opportunity,
    SizeDecision,
    SizingContext,
    SizingPolicy,
)

# conservative
KELLY_MULT = 0.25
IDEA_CAP_PCT = 0.08
RISKLESS_CAP_PCT = 0.08  # baskets / arbitrage sets: same 8% as the legacy arbitrage sizing
TOTAL_CAP_PCT = 0.60
RACE_CAP_PCT = 0.12
TILT_CAP_PCT = 0.30
ALL_COLLATERAL_NOTIONAL_PCT = 1.0  # with ALL collateral a set needs little cash; notional still capped

# chaser
NEAR_M = 1.3
OUT_OF_REACH_M = 10.0
SWING_DAYS = 2.0  # the swing window: election night through the Cup end
NEAR = {"kelly_mult": 0.5, "idea_cap": 0.12, "race_cap": 0.25, "total_cap": 0.70}
CHASE = {"kelly_mult": 1.0, "idea_cap": 0.25, "race_cap": 0.35, "total_cap": 0.95, "top_k": 5}
UNKNOWN_BAR = {"kelly_mult": 0.5, "idea_cap": 0.15, "race_cap": 0.25, "total_cap": 0.80}
SWING_BOLD_CAP_PCT = 0.60  # bold-to-goal stake cap per idea in the swing window
CHASER_RISKLESS_CAP_PCT = 0.50

# bar
BAR_FLOOR_MULT = 2.0  # never assume the top-3 bar is below 2x the initial balance
BAR_CAP_MULT = 10.0
BAR_MAX_GROWTH_PER_DAY = 0.05
BAR_MIN_HISTORY_S = 12 * 3600.0

DIRECTIONAL_BETS = ("binary", "bracket")  # chaser concentration (top_k) applies to these only


def kelly_units(p: float, gain: float, loss: float, equity: float, kelly_mult: float) -> float:
    """Units that maximise growth for a bet winning ``gain`` with ``p`` and losing ``loss`` per unit:
    ``kelly_mult * equity * (p / loss - (1 - p) / gain)`` (0 when that is not positive). For a
    contract bought at ``c`` held to 1/0 this equals ``kelly_mult * f * equity / c`` with
    ``f = (p - c) / (1 - c)``, i.e. ``strategy.size_position``."""
    raise NotImplementedError


def markov_ceiling(ev_multiple: float, m: float) -> float:
    """Upper bound on P(final >= m x now) for a non-negative wealth: ``min(1, ev_multiple / m)``."""
    raise NotImplementedError


def estimate_bar(leaderboard: Optional[LeaderboardSnapshot], history: Sequence[LeaderboardSnapshot],
                 initial_balance: Optional[float], days_left: float) -> BarEstimate:
    """The final top-3 balance a chaser must clear (docs/PAPER_TRADING.md §5.6.3).

    third = the 3rd-highest ``value`` (initial + pnl); growth per day from the oldest history
    snapshot at least BAR_MIN_HISTORY_S older (clipped to [0, BAR_MAX_GROWTH_PER_DAY]); projected =
    third x (1 + g) ** days_left capped at BAR_CAP_MULT x initial; value = max(projected or third,
    BAR_FLOOR_MULT x initial). Without a leaderboard: the floor, method "floor". Explanation lines
    always say that mid-Cup values are marks, not settled balances."""
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
        """"near" | "chase" | "swing" | "out_of_reach" | "unknown_bar" (see the module docstring)."""
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

