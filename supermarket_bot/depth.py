"""Order-book depth arithmetic shared by the idea generators, the sizing policies and the paper engine.

One implementation, so the depth an idea is sized on (``Opportunity.max_units`` / ``levels``) and the
depth a simulated fill walks can never disagree (docs/PAPER_TRADING.md, "Design decisions" D28).
Pure functions, no I/O. Implemented by the architect; package C owns bug fixes (the signatures are
frozen like ``models.py``).

Conventions: books are YES prices ``(price, qty)``; the functions here turn them into CONTRACT levels
(the price of the contract named by ``side``) ordered best first for the given action:

* buy YES  -> the YES asks, lowest first;
* buy NO   -> the YES bids as ``1 - bid``, lowest first (highest YES bid first);
* sell YES -> the YES bids, highest first;
* sell NO  -> the YES asks as ``1 - ask``, highest first (lowest YES ask first).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

TICK = 0.005
EPS = 1e-9
MIN_PRICE = 0.005
MAX_PRICE = 0.995

Level = Tuple[float, float]  # (price, quantity)
Levels = List[Level]


def floor_tick(x: float) -> float:
    """The tick at or below ``x`` (3 decimals). Buy limits use this, so a limit never gives away more edge."""
    return round(math.floor(x / TICK + EPS) * TICK, 3)


def ceil_tick(x: float) -> float:
    """The tick at or above ``x`` (3 decimals). Sell limits use this."""
    return round(math.ceil(x / TICK - EPS) * TICK, 3)


def clamp_price(x: float) -> float:
    """``x`` clipped to the order price range [0.005, 0.995]."""
    return min(MAX_PRICE, max(MIN_PRICE, x))


def _clean(raw: Optional[Iterable[Any]]) -> Levels:
    out: Levels = []
    for item in raw or ():
        if isinstance(item, Mapping):
            price, qty = item.get("price"), item.get("quantity", item.get("qty", item.get("size")))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price, qty = item[0], item[1]
        else:
            continue
        try:
            p, q = float(price), float(qty)
        except (TypeError, ValueError):
            continue
        if p != p or q != q or q <= 0 or not 0.0 < p < 1.0:
            continue
        out.append((p, q))
    return out


def book_sides(book: Any) -> Tuple[Levels, Levels]:
    """(YES bids, YES asks) from a ``BookObservation``, a stored-book mapping ``{"bids", "asks"}`` (lists of
    ``[price, qty]`` or ``{"price", "quantity"}``) or None. Invalid levels are dropped; order is not assumed."""
    if book is None:
        return [], []
    if isinstance(book, Mapping):
        return _clean(book.get("bids")), _clean(book.get("asks"))
    return _clean(getattr(book, "bids", None)), _clean(getattr(book, "asks", None))


def contract_levels(bids: Iterable[Any], asks: Iterable[Any], side: str, action: str) -> Levels:
    """CONTRACT levels an order of ``action`` ("buy" | "sell") on ``side`` ("yes" | "no") trades against,
    best first (see the module docstring). Prices are rounded to 6 decimals; equal prices are merged."""
    yes = side == "yes"
    buy = action == "buy"
    b, a = _clean(bids), _clean(asks)
    if buy:
        raw = a if yes else [(1.0 - p, q) for p, q in b]
        raw.sort(key=lambda lv: lv[0])
    else:
        raw = b if yes else [(1.0 - p, q) for p, q in a]
        raw.sort(key=lambda lv: -lv[0])
    merged: Levels = []
    for p, q in raw:
        p = round(p, 6)
        if merged and abs(merged[-1][0] - p) <= EPS:
            merged[-1] = (merged[-1][0], merged[-1][1] + q)
        else:
            merged.append((p, q))
    return merged


@dataclass
class WalkResult:
    filled: float  # whole units
    avg_price: Optional[float]  # None when nothing filled
    levels: Levels = field(default_factory=list)  # (price, qty) taken, best first


def walk(levels: Sequence[Level], qty: float, limit: float, buy: bool) -> WalkResult:
    """Take up to ``qty`` from ``levels`` (best first) at prices no worse than ``limit`` (``<=`` for buys,
    ``>=`` for sells). The total is floored to whole units; the last level taken absorbs the rounding."""
    want = math.floor(max(0.0, qty) + EPS)
    taken: Levels = []
    left = float(want)
    for price, size in levels:
        if left <= EPS:
            break
        if (buy and price > limit + EPS) or (not buy and price < limit - EPS):
            break
        take = min(size, left)
        if take > EPS:
            taken.append((price, take))
            left -= take
    total = sum(q for _, q in taken)
    whole = math.floor(total + EPS)
    if whole <= 0:
        return WalkResult(0.0, None, [])
    excess = total - whole
    while excess > EPS and taken:  # trim the fractional remainder off the worst level taken
        p, q = taken[-1]
        if q <= excess + EPS:
            taken.pop()
            excess -= q
        else:
            taken[-1] = (p, q - excess)
            excess = 0.0
    spent = sum(p * q for p, q in taken)
    return WalkResult(float(whole), spent / whole, taken)


def available(levels: Sequence[Level], limit: float, buy: bool) -> float:
    """Units on offer at prices no worse than ``limit`` (not floored)."""
    total = 0.0
    for price, size in levels:
        if (buy and price > limit + EPS) or (not buy and price < limit - EPS):
            break
        total += size
    return total


def avg_cost(levels: Sequence[Level], qty: float) -> Optional[float]:
    """Average price of taking ``qty`` units from ``levels`` best first, ignoring any limit; None when
    ``qty`` <= 0 or the levels are empty. Units beyond the levels' total are priced at the last level
    (callers cap ``qty`` by depth first)."""
    if qty <= EPS or not levels:
        return None
    left, spent = qty, 0.0
    for price, size in levels:
        take = min(size, left)
        spent += take * price
        left -= take
        if left <= EPS:
            break
    if left > EPS:
        spent += left * levels[-1][0]
    return spent / qty


def set_levels(legs: Sequence[Sequence[Level]], limit: Optional[float] = None) -> Levels:
    """Marginal set levels for buying one unit of every leg together: each step takes the cheapest remaining
    level of every leg; the set's marginal cost is the sum of those prices. Returns ``[(set cost, sets)]``
    cheapest first, stopping before the first marginal cost above ``limit`` (when given). Empty when any
    leg has no levels. ``sum(qty)`` equals the legacy ``strategy._walk_set``."""
    if not legs or any(not lv for lv in legs):
        return []
    idx = [0] * len(legs)
    left = [lv[0][1] for lv in legs]
    out: Levels = []
    while all(i < len(lv) for i, lv in zip(idx, legs)):
        marginal = round(sum(lv[i][0] for i, lv in zip(idx, legs)), 6)
        if limit is not None and marginal > limit + EPS:
            break
        step = min(left)
        if out and abs(out[-1][0] - marginal) <= EPS:
            out[-1] = (out[-1][0], out[-1][1] + step)
        else:
            out.append((marginal, step))
        for k, lv in enumerate(legs):
            left[k] -= step
            if left[k] <= EPS:
                idx[k] += 1
                left[k] = lv[idx[k]][1] if idx[k] < len(lv) else 0.0
    return out


def walk_set(legs: Sequence[Sequence[Level]], limit: float) -> float:
    """Full sets on offer while the next set costs at most ``limit`` (not floored)."""
    return sum(q for _, q in set_levels(legs, limit))


def shift_levels(levels: Sequence[Level], new_best: float) -> Levels:
    """``levels`` moved so the best level sits at ``new_best`` (tick gaps kept, prices clipped to (0, 1)):
    marks a position with an older book's depth profile at today's touch (§6.7, "stale")."""
    if not levels:
        return []
    shift = new_best - levels[0][0]
    out: Levels = []
    for p, q in levels:
        moved = round(p + shift, 6)
        if 0.0 < moved < 1.0:
            out.append((moved, q))
    return out


def liquidation_proceeds(levels: Sequence[Level], qty: float) -> Tuple[float, float]:
    """(proceeds, units sold) of selling ``qty`` into sell-side ``levels`` (best first) with no limit;
    units beyond the levels' total fetch nothing."""
    left, cash = max(0.0, qty), 0.0
    for price, size in levels:
        if left <= EPS:
            break
        take = min(size, left)
        cash += take * price
        left -= take
    return cash, max(0.0, qty) - max(0.0, left)
