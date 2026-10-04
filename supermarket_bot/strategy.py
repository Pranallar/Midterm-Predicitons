"""Read-only trade ideas: fade participant surges, carry high-90s favourites, arbitrage.

See docs/DESIGN.md (Strategy section). Nothing here places orders.

Side conventions: prices are YES-denominated; an idea always names the contract to *buy*.
Buying NO after an up-surge costs ``1 - yes_bid`` and a NO share pays 1 if NO wins.
``entry_price``, ``target_price`` and ``stop_price`` are prices of that contract.
"""

from __future__ import annotations

import copy
import logging
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
    SURGE_OPEN,
    SURGE_REVERTED,
    VERDICT_PARTICIPANTS,
    VERDICT_UNCLEAR,
    BacktestResult,
    ExchangeInfo,
    HighBand,
    Opportunity,
    PricePoint,
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
MIN_BACKTEST_SURGES = 10  # blend the backtest reversion rate only with this many surges
THIN_BOOK_SHARES = 500.0
TOP_N = 50
CONSTRAINT_CONFIDENCE = 0.7  # engine-reported relationship violation
BOOK_ARB_CONFIDENCE = 0.6  # "potential, not guaranteed" multi-outcome book flag
BOOK_MAX_AGE_S = 900.0  # an order-book read older than this does not size an idea
BOOK_TICK = 0.005  # the price grid: a book whose best level sits more than a tick off the live quote is outdated
REVERSION_LINK_S = 6 * 3600.0  # an opposite move this soon after a surge's window may be its reversion

MODE_PROTECT = "protect"
MODE_BALANCED = "balanced"
MODE_AGGRESSIVE = "aggressive"
MODE_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    MODE_AGGRESSIVE: {"fade": 1.3, "arbitrage": 1.3, "carry": 0.7},
    MODE_PROTECT: {"carry": 1.3, "fade": 0.6},
    MODE_BALANCED: {},
}
_KIND_ORDER = {"arbitrage": 0, "fade": 1, "carry": 2, "watch": 3}

_EPS = 1e-9


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
    depth = _num(depth_limit)
    if depth is not None:
        shares = min(shares, math.floor(max(0.0, depth) + 1e-9))
    return max(0, int(shares))


def _bracket_shares(p: float, gain: float, loss: float, entry: float, balance: Optional[float],
                    kelly_mult: float, max_position_pct: float, depth: Optional[float]) -> int:
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
    if depth is not None:
        limit = min(limit, max(0.0, depth))
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


def _settles_before(info_date: Optional[str], cup_end: Optional[float]) -> Tuple[Optional[bool], Optional[float]]:
    settle = parse_iso_ts(info_date) if info_date else None
    if settle is None or cup_end is None:
        return None, settle
    return settle <= cup_end + 1e-6, settle


# --------------------------------------------------------------------------- opportunities


# --------------------------------------------------------------------------- order-book depth

Levels = List[Tuple[float, float]]  # (cost of the contract, shares), cheapest first


def _book_levels(contract: str, point: Optional[PricePoint], now: float,
                 live_cost: Optional[float] = None) -> Tuple[Optional[Levels], Optional[float]]:
    """The levels a buy of ``contract`` fills against, from the point's stored order book.

    YES buys take the YES asks (cost = price); NO buys take the YES bids (cost = 1 - price).
    Returns ``(levels, book time)``, levels None when no usable book is known: none stored,
    older than ``BOOK_MAX_AGE_S``, or out of line with the live quote (levels cheaper than the
    live cost were taken since the read and are dropped; if what is left starts more than a
    tick above the live cost, the read is outdated).
    """
    book = getattr(point, "book", None) if point is not None else None
    if not isinstance(book, Mapping):
        return None, None
    at = _num(book.get("at"))
    if at is None or now - at > BOOK_MAX_AGE_S:
        return None, at
    yes = contract == "yes"
    levels: Levels = []
    for raw in (book.get("asks") if yes else book.get("bids")) or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        price, qty = _num(raw[0]), _num(raw[1])
        if price is None or qty is None or qty <= 0 or not 0.0 < price < 1.0:
            continue
        levels.append((round(price if yes else 1.0 - price, 6), qty))
    levels.sort(key=lambda lv: lv[0])
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
    """Shares on offer at a cost up to ``limit`` (see :func:`_depth_limit`), and those levels."""
    taken: Levels = []
    total = 0.0
    for cost, qty in levels:
        if cost > limit + _EPS:
            break
        taken.append((cost, qty))
        total += qty
    return total, taken


def _avg_cost(levels: Levels, qty: float) -> Optional[float]:
    """Average cost of filling ``qty`` shares from ``levels`` (None for nothing to fill)."""
    if qty <= _EPS:
        return None
    left, spent = qty, 0.0
    for cost, size in levels:
        take = min(size, left)
        spent += take * cost
        left -= take
        if left <= _EPS:
            break
    filled = qty - max(0.0, left)
    return spent / filled if filled > _EPS else None


def _walk_set(legs: Sequence[Levels], limit: float) -> float:
    """Full sets (one share of every leg) on offer while the next set costs at most ``limit``
    (see :func:`_depth_limit`): each step takes the cheapest remaining level of every leg."""
    if not legs or any(not levels for levels in legs):
        return 0.0
    idx = [0] * len(legs)
    left = [levels[0][1] for levels in legs]
    sets = 0.0
    while all(i < len(levels) for i, levels in zip(idx, legs)):
        marginal = sum(levels[i][0] for i, levels in zip(idx, legs))
        if marginal > limit + _EPS:
            break
        step = min(left)
        sets += step
        for k, levels in enumerate(legs):
            left[k] -= step
            if left[k] <= _EPS:
                idx[k] += 1
                left[k] = levels[idx[k]][1] if idx[k] < len(levels) else 0.0
    return sets


def _ago(now: float, at: Optional[float]) -> str:
    if at is None:
        return "at an unknown time"
    secs = max(0.0, now - at)
    return f"{secs / 60:.0f} min ago" if secs >= 90 else f"{secs:.0f} s ago"


def _surge_levels(surge: Surge) -> Optional[Tuple[bool, float, float, float]]:
    """(up, signed YES move start->peak, YES target at half reversion, YES stop at +50% extension)."""
    up = surge.direction != "down"
    move = surge.peak_price - surge.start_price
    if abs(move) < _EPS or (move > 0) != up:
        return None
    target = surge.start_price + 0.5 * move
    stop = max(0.0, min(1.0, surge.peak_price + 0.5 * move))
    return up, move, target, stop


def fade_opportunity(surge: Surge, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                     balance: float, backtest: Optional[BacktestResult] = None, *,
                     cup_end: Optional[float] = None, kelly_mult: float = KELLY_MULT,
                     max_position_pct: float = MAX_POSITION_PCT) -> Optional[Opportunity]:
    """Bet on reversion of an open, participant-driven surge (None when not applicable or edge <= 0).

    After an up-move buy NO at ``1 - yes_bid``; after a down-move buy YES at ``yes_ask``
    (the mark without a book). Target: half the start->peak move given back. Stop: a further
    50% extension beyond the peak. ``p`` is the attribution's reversion odds, blended 50/50
    with the backtest reversion rate when the backtest has at least 10 surges.
    """
    att = surge.attribution
    if surge.status != SURGE_OPEN or att is None or att.verdict != VERDICT_PARTICIPANTS:
        return None
    levels = _surge_levels(surge)
    if levels is None:
        return None
    up, move, yes_target, yes_stop = levels
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
        target, stop = 1.0 - yes_target, 1.0 - yes_stop
    else:
        side = "yes"
        if ask is not None:
            entry, how = ask, "the YES ask"
        elif mark is not None:
            entry, how, no_book = mark, "the YES mark", True
        else:
            return None
        target, stop = yes_target, yes_stop
    entry, target, stop = round(entry, 6), round(target, 6), round(stop, 6)
    if not 0.0 < entry < 1.0:
        return None
    gain, loss = target - entry, entry - stop
    if gain <= _EPS or loss <= _EPS:  # already past the target, or already through the stop
        return None
    p_att = _clamp01(att.reversion_odds)
    p, blended = p_att, False
    rate = backtest.reversion_rate if backtest is not None else None
    if backtest is not None and rate is not None and backtest.n_surges >= MIN_BACKTEST_SURGES:
        p, blended = 0.5 * p_att + 0.5 * _clamp01(rate), True
    edge = p * gain - (1.0 - p) * loss
    if edge <= _EPS:
        return None
    er = edge / entry
    kelly_shares = _bracket_shares(p, gain, loss, entry, balance, kelly_mult, max_position_pct, None)
    # What one share is worth by these odds (target or stop): a fill above it has no edge left.
    value = p * target + (1.0 - p) * stop
    levels, book_at = _book_levels(side, current, now, None if no_book else entry)
    side_depth = _num(att.bid_depth if up else att.ask_depth)  # within 5c of the mid, when analysed
    depth = side_depth if side_depth is not None else _num(att.book_depth)
    fill: Optional[float] = None
    offered = 0.0
    limit = _depth_limit(value, entry)
    if levels is not None:
        offered, taken = _walk(levels, limit)
        shares = min(kelly_shares, int(math.floor(offered + 1e-9)))
        fill = _avg_cost(taken, shares)
        depth_capped = shares < kelly_shares
    else:
        # No recent book: the depth near the mid when the surge was analysed is only a loose cap.
        shares = kelly_shares if depth is None else min(kelly_shares, int(math.floor(max(0.0, depth) + 1e-9)))
        depth_capped = shares < kelly_shares
    if shares <= 0:
        return None  # the book has no shares at a price that keeps an edge
    cost = round(shares * entry, 2)
    confidence = _clamp01(att.confidence)
    title, option = _title(info, surge.market_id)
    before, _ = _settles_before(info.settlement_date if info is not None else None, cup_end)

    verb, noun = ("jumped", "jump") if up else ("dropped", "drop")
    rationale = [
        f"YES {verb} {_signed(move)} in {surge.window} ({_px(surge.start_price)} -> {_px(surge.peak_price)}) "
        f"with no matching news: the evidence points to a few Cup traders ({confidence:.0%} confidence)",
    ]
    if up:
        rationale.append(f"Buy NO at {_px(entry)} ({how}): if YES gives back half its {_px(abs(move))} {noun} to "
                         f"{_px(yes_target)}, NO is worth {_px(target)} ({_signed(gain)}/share)")
        rationale.append(f"Stop if YES extends another half-move to {_px(yes_stop)}: NO falls to "
                         f"{_px(stop)} ({_signed(-loss)}/share)")
    else:
        rationale.append(f"Buy YES at {_px(entry)} ({how}): if YES wins back half its {_px(abs(move))} {noun} to "
                         f"{_px(yes_target)}, it is worth {_px(target)} ({_signed(gain)}/share)")
        rationale.append(f"Stop if YES falls another half-move to {_px(yes_stop)} ({_signed(-loss)}/share)")
    odds = f"Win chance {p:.0%}: attribution reversion odds {p_att:.0%}"
    if blended:
        odds += (f", blended 50/50 with the backtest's {_clamp01(rate):.0%} reversion rate over "
                 f"{backtest.n_surges} surges")  # type: ignore[union-attr]
    rationale.append(odds)
    rationale.append(
        f"Edge {_signed(edge)}/share ({er:.1%} of cost) over {FADE_HORIZON_H:g} h; size {shares:,} shares "
        f"= {cost:,.0f} SUSQies ({_kelly_label(kelly_mult)} on the target/stop bracket, capped at "
        f"{max_position_pct:.0%} of balance{' and by book depth' if depth_capped else ''})"
    )
    if levels is not None and fill is not None:
        rationale.append(
            f"Book ({_ago(now, book_at)}): {offered:,.0f} {side.upper()} shares on offer up to {_px(round(limit, 4))}, "
            f"where each keeps at least half the edge; {shares:,} fill at an average of {_px(round(fill, 4))} "
            f"({_signed(value - fill)}/share there)"
        )
    if att.reasons:
        rationale.append(f"Evidence: {att.reasons[0]}")

    rising = "rising" if up else "falling"
    risks = [
        f"If the move was informed (news not out yet), YES may keep {rising}: the stop caps the loss at "
        f"{_px(loss)}/share, about {shares * loss:,.0f} SUSQies at this size",
    ]
    if no_book:
        risks.append(f"No live book: the entry uses the mark {_px(mark)}; real fills will be worse by at least the spread")
    if levels is None:
        risks.append(f"Size not checked against a recent order book on the {side.upper()} side: check the depth "
                     "before buying, a large order moves the price")
    if depth is not None and depth < THIN_BOOK_SHARES:
        where = f"on the side a {side.upper()} buy takes " if side_depth is not None else ""
        risks.append(f"Thin book: about {depth:,.0f} shares rest {where}within 5c of the mid, so a large order moves the price")
    risks.append(f"Exit within {FADE_HORIZON_H:g} h at the target or the stop: a reversion trade, not a hold-to-settlement bet")

    return Opportunity(
        kind="fade",
        exchange_id=surge.exchange_id,
        market_id=surge.market_id,
        title=title,
        option=option,
        side=side,
        entry_price=round(entry, 4),
        target_price=round(target, 4),
        stop_price=round(stop, 4),
        prob_win=round(p, 4),
        edge=round(edge, 6),
        expected_return=round(er, 6),
        horizon_hours=FADE_HORIZON_H,
        suggested_shares=shares,
        suggested_cost=cost,
        score=round(er * confidence, 6),
        confidence=round(confidence, 4),
        rationale=rationale,
        risks=risks,
        settles_before_cup_end=before,
        surge_id=surge.id,
        depth_checked=levels is not None,
        fill_price=round(fill, 6) if fill is not None else None,
    )


def carry_opportunity(band: HighBand, current: Optional[PricePoint], info: Optional[ExchangeInfo], now: float,
                      cup_end: float, balance: float, *, kelly_mult: float = KELLY_MULT,
                      max_position_pct: float = MAX_POSITION_PCT) -> Optional[Opportunity]:
    """Buy a stable high-90s favourite at its ask (None when unstable, no room, or edge <= 0).

    ``p_true = fav_mid + min(0.01, 0.2 * (1 - fav_mid))``; ``edge = p_true - entry``. A position
    that settles after the Cup end is only marked then: its score is halved.
    """
    if not band.stable:
        return None
    yes = (band.side or "YES").upper() != "NO"
    name = "YES" if yes else "NO"
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
    if entry <= 0 or entry >= CARRY_MAX_ENTRY - _EPS:
        return None
    adjust = min(0.01, 0.2 * (1.0 - fav_mid))
    p_true = min(1.0, fav_mid + adjust)
    edge = p_true * (1.0 - entry) - (1.0 - p_true) * entry
    if edge <= _EPS:
        return None
    er = edge / entry
    settlement = band.settlement_date or (info.settlement_date if info is not None else None)
    before, settle_ts = _settles_before(settlement, cup_end)
    if before is True:
        horizon: Optional[float] = max(0.0, (settle_ts - now) / 3600.0)  # type: ignore[operator]
    elif before is False:
        horizon = max(0.0, (cup_end - now) / 3600.0)
    else:
        horizon = None
    kelly_shares = size_position(p_true, entry, balance, kelly_mult, max_position_pct)
    contract = "yes" if yes else "no"
    levels, book_at = _book_levels(contract, current, now, None if no_book else entry)
    fill: Optional[float] = None
    offered = 0.0
    shares = kelly_shares
    if levels is not None:
        # Only shares that keep at least half of the edge count: a carry's edge is a fraction of
        # a cent, so the next level up usually has none.
        offered, taken = _walk(levels, _depth_limit(p_true, entry))
        shares = min(kelly_shares, int(math.floor(offered + 1e-9)))
        fill = _avg_cost(taken, shares)
        if shares <= 0:
            return None
    cost = round(shares * entry, 2)
    confidence = round(min(0.9, 0.4 + 0.5 * _clamp01(band.time_in_band)), 4)
    score = er * confidence * (0.5 if before is False else 1.0)
    title, option = _title(info, band.market_id)

    rationale = [
        f"{name} has sat at {_px(band.low)}-{_px(band.high)} (mean {_px(band.mean)}) for "
        f"{band.time_in_band:.0%} of the last {band.lookback_s / 3600.0:g} h: a stable high-90s favourite",
        f"Buy {name} at {_px(entry)} ({how}): it pays 1.00 if {name} wins ({_signed(1.0 - entry)}/share)",
        f"Fair value {p_true:.3f} = mid {fav_mid:.3f} + min(0.01, 0.2 x {1.0 - fav_mid:.3f}): a small "
        "favourite-longshot adjustment, since heavy favourites tend to be slightly underpriced",
        f"Edge {_signed(edge)}/share ({er:.2%} of cost), slow but steady; {shares:,} shares = {cost:,.0f} SUSQies "
        f"({_kelly_label(kelly_mult)}, capped at {max_position_pct:.0%} of balance"
        f"{' and by book depth' if shares < kelly_shares else ''})",
    ]
    if levels is not None and fill is not None:
        rationale.append(
            f"Book ({_ago(now, book_at)}): {offered:,.0f} {name} shares on offer at prices that keep at least half "
            f"the edge; {shares:,} fill at an average of {_px(round(fill, 4))} ({_signed(p_true - fill)}/share there)"
        )
    if before is True:
        rationale.append(f"Settles {_when(settle_ts)} (in {horizon:.0f} h), before the Cup ends: paid out at 1.00 if it wins")
    elif before is False:
        rationale.append(f"Settles {_when(settle_ts)}, after the Cup ends ({_when(cup_end)}): score halved")

    risks = [
        f"An upset loses the whole {_px(entry)}/share: {shares:,} shares put {cost:,.0f} SUSQies at risk "
        f"to make {shares * (1.0 - entry):,.0f}",
    ]
    if before is False:
        risks.append(f"Valued at market price at Cup end, not paid out: it settles {_when(settle_ts)}, after the Cup "
                     f"ends on {_when(cup_end)}, so the result depends on the price then")
    elif before is None:
        risks.append("Settlement date unknown: it may not pay out before the Cup ends")
    if no_book:
        risks.append(f"No live book: the entry uses the mark; check the {name} ask before buying")
    if levels is None:
        risks.append(f"Size not checked against a recent order book on the {name} side: at the next price level "
                     "up the edge may be gone, so check the depth before buying")
    risks.append("The favourite-longshot adjustment is a rule of thumb, not a measured edge")

    return Opportunity(
        kind="carry",
        exchange_id=band.exchange_id,
        market_id=band.market_id,
        title=title,
        option=option,
        side="yes" if yes else "no",
        entry_price=round(entry, 4),
        target_price=1.0,
        stop_price=None,
        prob_win=round(p_true, 4),
        edge=round(edge, 6),
        expected_return=round(er, 6),
        horizon_hours=round(horizon, 2) if horizon is not None else None,
        suggested_shares=shares,
        suggested_cost=cost,
        score=round(score, 6),
        confidence=confidence,
        rationale=rationale,
        risks=risks,
        settles_before_cup_end=before,
        depth_checked=levels is not None,
        fill_price=round(fill, 6) if fill is not None else None,
    )


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
        kind="watch",
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
    )


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
                        cup_end: Optional[float]) -> Tuple[Optional[bool], Optional[float]]:
    """True/False when every leg's settlement date is known (all before the Cup end?), else None.

    Also returns the latest settlement time of the legs.
    """
    if cup_end is None or not exchange_ids:
        return None, None
    latest: Optional[float] = None
    verdicts: List[bool] = []
    for eid in exchange_ids:
        info = infos.get(str(eid)) if eid is not None else None
        before, settle = _settles_before(info.settlement_date if info is not None else None, cup_end)
        if before is None or settle is None:
            return None, None
        verdicts.append(before)
        latest = settle if latest is None else max(latest, settle)
    return all(verdicts), latest


def _settlement_risk(before: Optional[bool], settle: Optional[float], cup_end: Optional[float]) -> str:
    if before is True:
        return f"It pays at settlement ({_when(settle)}), before the Cup ends"
    if before is False:
        return (f"Some legs settle after the Cup ends ({_when(cup_end)}): until then they are only valued at "
                "market price, not paid out")
    return "It pays at settlement; if that is after the Cup ends it is only valued at market price then"


def _legs_text(legs: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for leg in legs:
        name = leg.get("option") or leg.get("title") or f"exch {leg.get('exchange_id')}"
        parts.append(f"{str(leg.get('side') or '').upper()} {name} @ {_px(leg.get('price'))}")
    return " + ".join(parts)


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


def arbitrage_opportunities(constraints: Optional[Mapping[str, Any]], overround_rows: Sequence[Mapping[str, Any]],
                            balance: float, *, max_position_pct: float = MAX_POSITION_PCT,
                            latest: Optional[Mapping[str, PricePoint]] = None,
                            infos: Optional[Mapping[str, ExchangeInfo]] = None,
                            cup_end: Optional[float] = None, now: Optional[float] = None) -> List[Opportunity]:
    """One idea per engine-reported constraint violation and per multi-outcome book flagged
    ``hasArbitrageOpportunity``. Sized at the per-idea cap (a true arbitrage has no Kelly limit).

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
    out: List[Opportunity] = []
    rows = []
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
        cap_shares = int(math.floor(max_position_pct * b / entry + 1e-9)) if tradable and b > 0 else 0  # type: ignore[operator]
        shares = cap_shares
        # What one set is worth: its settlement payoff, else its cost plus the engine's gap.
        worth = payoff if payoff is not None else ((entry + amount) if entry is not None else None)
        limit = _depth_limit(worth, entry) if worth is not None and entry is not None else None
        offered, books, book_at = _set_depth(legs, latest, now, limit) if tradable and live else (None, None, None)
        fill: Optional[float] = None
        if offered is not None and books is not None:
            shares = min(cap_shares, int(math.floor(offered + 1e-9)))
            fill = _set_fill(books, shares)
        unit = "sets" if multi else "shares"
        before, settle = _legs_settle_before([leg["exchange_id"] for leg in legs], infos, cup_end)
        rationale = [f"Engine-reported {kind} violation of {_px(amount)}: {v.get('reason') or 'prices break the relationship'}"]
        rule = (v.get("constraint") or {}).get("priceRule") if isinstance(v.get("constraint"), Mapping) else None
        if rule:
            rationale.append(f"Rule: {rule}")
        for t, leg in zip(trades, legs):
            rationale.append(
                f"{t.get('action') or 'Trade'} {t.get('outcomeSide') or ''} on {t.get('marketTitle') or t.get('marketId') or '?'}"
                f"{' - ' + str(t.get('outcome')) if t.get('outcome') else ''} (exch {t.get('exchangeId')}, buy {leg['side'].upper()} "
                f"at {_px(leg['price'])}): {t.get('rationale') or ''}".rstrip(": ")
            )
        sizing = _sizing_text(shares, cap_shares, unit, max_position_pct, offered, fill, limit, book_at, now)
        if entry is not None and payoff is not None:
            pays = f"{'at least ' if at_least else ''}{payoff:.2f}"
            if tradable:
                rationale.append(f"One set ({_legs_text(legs)}) costs {_px(entry)} and pays {pays} at settlement: "
                                 f"{_signed(edge)} per set ({er:.1%}); {sizing}")  # type: ignore[arg-type]
            else:
                rationale.append(f"At these prices one set ({_legs_text(legs)}) costs {_px(entry)}, no less than the {pays} "
                                 "it pays: the gap is gone after the spread, so there is nothing to size")
        elif entry is not None:
            what = f"The {len(legs)} legs ({_legs_text(legs)}) cost {_px(entry)} per set" if multi else \
                f"The trade costs {_px(entry)} per share"
            rationale.append(f"{what} and gain about {_px(amount)} as prices correct ({er:.1%}); {sizing}")
        if tradable and offered is not None and shares <= 0:
            tradable = False  # the books hold nothing at a price that keeps the gap
        prices_risk = ("Leg prices are the live asks (NO = 1 - YES bid): fills can still move them"
                       if live else
                       "Prices are last/valuation prices, not executable quotes: check the asks on every leg, the spread can eat the gap")
        risks = [prices_risk]
        if tradable and offered is None:
            risks.append("Size not checked against recent order books on every leg: check the depth on each one, "
                         "walking a thin book can cost more than the set pays")
        if multi:
            risks.append("Legs fill separately: a partial fill leaves a one-sided position")
        risks.append("The gap can persist until settlement, tying up capital")
        risks.append(_settlement_risk(before, settle, cup_end))
        single = legs[0] if legs and not multi else None
        sides = {leg["side"] for leg in legs}
        out.append(Opportunity(
            kind="arbitrage",
            exchange_id=single["exchange_id"] if single else None,
            market_id=market_id,
            title=str(title),
            option=single["option"] if single else None,
            side=(sides.pop() if len(sides) == 1 else legs[0]["side"]) if legs else "yes",
            entry_price=round(entry, 4) if entry is not None else None,
            target_price=round(payoff, 4) if payoff is not None and not at_least else None,
            stop_price=None,
            prob_win=None,
            edge=edge,
            expected_return=round(er, 6) if er is not None else None,
            horizon_hours=None,
            suggested_shares=shares if tradable else 0,
            suggested_cost=round(shares * entry, 2) if entry and shares and tradable else 0.0,
            score=round((er or 0.0) * CONSTRAINT_CONFIDENCE, 6) if tradable else 0.0,
            confidence=CONSTRAINT_CONFIDENCE,
            rationale=rationale,
            risks=risks,
            settles_before_cup_end=before,
            legs=legs if multi else [],
            unit=unit,
            depth_checked=(offered is not None) if tradable else None,
            fill_price=fill if tradable else None,
        ))
    by_market: Dict[str, List[ExchangeInfo]] = {}
    for info in infos.values():
        by_market.setdefault(str(info.market_id), []).append(info)
    for row in overround_rows or ():
        if not isinstance(row, Mapping) or not (row.get("arbitrage") or row.get("hasArbitrageOpportunity")):
            continue
        over = _num(row.get("overround"))
        market_id = str(row.get("market_id") or row.get("marketId") or row.get("id") or "")
        title = str(row.get("market_title") or row.get("marketTitle") or row.get("title") or f"Market {market_id}")
        n_out = row.get("outcomes")
        members = by_market.get(market_id) or []
        before, settle = _legs_settle_before([m.exchange_id for m in members], infos, cup_end)
        legs: List[Dict[str, Any]] = []
        if members and (not isinstance(n_out, int) or n_out == len(members)):
            legs = [{"exchange_id": m.exchange_id, "market_id": market_id, "title": title, "option": m.option,
                     "side": "yes", "price": _quote_cost("yes", latest.get(m.exchange_id))} for m in members]
            if any(leg["price"] is None for leg in legs):
                legs = []  # without every ask the per-leg list would not add up to the set price
        rationale: List[str] = []
        if legs:
            # Price the set from the live asks it lists, so the set price is the sum of its legs.
            entry = round(sum(leg["price"] for leg in legs), 6)
            if entry >= 1.0 - _EPS:
                continue  # the asks no longer leave a gap: the flag is from an older book read
            edge: Optional[float] = round(1.0 - entry, 6)
            rationale.append(f"The engine flags a potential arbitrage, and the live asks of all {len(legs)} outcomes "
                             f"({_legs_text(legs)}) sum to {_px(entry)}, below 1.00")
        else:
            entry = over if over is not None and over > 0 else None
            edge = round(1.0 - over, 6) if over is not None and over < 1.0 - _EPS else None
            when = f" ({_ago(now, _num(row.get('at')))})" if now is not None and _num(row.get("at")) is not None else ""
            rationale.append(f"The engine flags a potential arbitrage: the best prices across "
                             f"{str(n_out) + ' ' if n_out else 'its '}outcomes sum to {_px(over)}, below 1.00, at the last "
                             f"order-book read{when}; not every outcome has a live ask, so the set price is not re-checked")
        er = edge / entry if edge is not None and entry else None
        cap_shares = int(math.floor(max_position_pct * b / entry + 1e-9)) if entry and edge is not None and b > 0 else 0
        shares = cap_shares
        limit = _depth_limit(1.0, entry) if entry is not None else None
        offered, books, book_at = _set_depth(legs, latest, now, limit) if legs and edge is not None else (None, None, None)
        fill: Optional[float] = None
        if offered is not None and books is not None:
            shares = min(cap_shares, int(math.floor(offered + 1e-9)))
            fill = _set_fill(books, shares)
        tradable = edge is not None and entry is not None and shares > 0
        if edge is not None and entry:
            sizing = _sizing_text(shares, cap_shares, "sets", max_position_pct, offered, fill, limit, book_at, now)
            rationale.append(f"Buying one YES share of every outcome costs {_px(entry)} and pays 1.00 if exactly one wins "
                             f"({_signed(edge)} per set, {er:.1%}); {sizing}")
        risks = [
            "Only a potential arbitrage: it pays only if the listed outcomes are exhaustive and mutually exclusive",
            "Every leg must fill at its best ask: a partial fill leaves a one-sided position",
        ]
        if tradable and offered is None:
            risks.append("Size not checked against recent order books on every outcome: check the depth on each one, "
                         "walking a thin book can cost more than the set pays")
        risks.append(_settlement_risk(before, settle, cup_end))
        out.append(Opportunity(
            kind="arbitrage",
            exchange_id=None,
            market_id=market_id,
            title=title,
            option=None,
            side="yes",
            entry_price=round(entry, 4) if entry is not None else None,
            target_price=1.0,
            stop_price=None,
            prob_win=None,
            edge=edge,
            expected_return=round(er, 6) if er is not None else None,
            horizon_hours=None,
            suggested_shares=shares if tradable else 0,
            suggested_cost=round(shares * entry, 2) if tradable and entry else 0.0,
            score=round((er or 0.0) * BOOK_ARB_CONFIDENCE, 6) if tradable or offered is None else 0.0,
            confidence=BOOK_ARB_CONFIDENCE,
            rationale=rationale,
            risks=risks,
            settles_before_cup_end=before,
            legs=legs,
            unit="sets",
            depth_checked=(offered is not None) if edge is not None else None,
            fill_price=fill if tradable else None,
        ))
    return out


# --------------------------------------------------------------------------- backtest


def backtest_fade(series_by_exchange: Mapping[str, Sequence[PricePoint]], horizon_s: float = 6 * 3600.0,
                  step_s: float = 300.0, market_of: Optional[Mapping[str, str]] = None, *,
                  windows: Optional[Dict[str, Tuple[float, float]]] = None,
                  z_threshold: float = Z_THRESHOLD) -> BacktestResult:
    """How often detected surges gave back half their move within ``horizon_s``.

    Walks each series on a ``step_s`` grid making the :func:`analytics.detect_surges` decision
    at every step (incrementally: O(n + steps) per series). A surge counts once per
    (exchange, direction) until its status is no longer open, and only if it is open at
    detection. At detection + horizon it is ``reverted`` when half or more of the start->peak
    move came back; ``fade_return`` is the YES move against the surge from the detection mark
    (per share), ``hold_return`` its negative. Detections without data at the horizon are
    skipped and counted in the notes.
    """
    windows = windows or SURGE_WINDOWS
    events: List[Tuple[str, float, bool]] = []
    pending = missing = n_series = 0
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
            fade = surge.end_price - p_h if surge.direction == "up" else p_h - surge.end_price
            outcome = update_surge_status(copy.copy(surge), p_h, t_h)
            events.append((surge.window, round(fade, 6), outcome.status == SURGE_REVERTED))

    def stats(rows: Sequence[Tuple[str, float, bool]]) -> Dict[str, Any]:
        n = len(rows)
        k = sum(1 for r in rows if r[2])
        avg = sum(r[1] for r in rows) / n if n else None
        return {
            "n": n,
            "n_reverted": k,
            "reversion_rate": round(k / n, 4) if n else None,
            "avg_fade_return": round(avg, 6) if avg is not None else None,
            "avg_hold_return": round(-avg, 6) + 0.0 if avg is not None else None,
        }

    total = stats(events)
    by_window = {}
    for name, _ in sorted(windows.items(), key=lambda kv: kv[1][0]):
        rows = [e for e in events if e[0] == name]
        if rows:
            by_window[name] = stats(rows)
    hours = horizon_s / 3600.0
    notes = [
        f"Walked {n_series} series on a {step_s / 60.0:g}-minute grid; a surge counts once per exchange and "
        "direction until it reverts or holds",
        f"Fades enter at the detection mark and exit {hours:g} h later, ignoring spread and fees: real returns are lower",
    ]
    if pending:
        notes.append(f"{pending} detection(s) too recent to evaluate (less than {hours:g} h of data after them)")
    if missing:
        notes.append(f"{missing} detection(s) had no price near the horizon")
    if total["n"] < MIN_BACKTEST_SURGES:
        notes.append(f"Only {total['n']} evaluated surge(s), fewer than {MIN_BACKTEST_SURGES}: fades use the "
                     "attribution odds alone")
    return BacktestResult(
        n_surges=total["n"],
        n_reverted=total["n_reverted"],
        reversion_rate=total["reversion_rate"],
        avg_fade_return=total["avg_fade_return"],
        avg_hold_return=total["avg_hold_return"],
        horizon_hours=round(hours, 4),
        by_window=by_window,
        notes=notes,
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
        "When you are behind, variance is your friend: take more, bigger, independent bets. When you are in "
        "the top 3 near the end, cut variance and protect the lead.",
        "Carry trades on stable high-90s favourites compound slowly: a fraction of a percent to a few percent "
        "per trade, but nearly all of them pay. They protect a lead; they are too slow to catch up.",
        "Fade only participant-driven moves: a jump made by a few large trades with no news tends to give back "
        "half, while a move backed by news usually holds. Follow news, or stay out.",
        f"Respect settlement dates versus the Cup end ({_when(cup_end)}): a position that settles later is only "
        "valued at market price when the Cup ends, not paid out at 1.00.",
        "Diversify across uncorrelated markets: races in different states move on different news, so spread "
        "risk instead of stacking bets on one party-wide swing.",
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
                       "whether you are defending a top-3 finish or far behind: ideas are ranked by expected return x "
                       "confidence.")
        else:
            out.append("Mode: balanced. You are neither defending a top-3 finish nor far behind: ideas are ranked by "
                       "expected return x confidence.")
    return out


def _describe(opp: Opportunity) -> str:
    label = opp.title + (f" ({opp.option})" if opp.option else "")
    if opp.kind == "arbitrage" and opp.legs and not (opp.target_price == 1.0 and all(leg.get("side") == "yes" for leg in opp.legs)):
        return f"buy the {len(opp.legs)}-leg set on {label} ({_legs_text(opp.legs)}) for {_px(opp.entry_price)} per set"
    if opp.kind == "arbitrage" and opp.exchange_id is None:
        return f"buy every outcome of {label} for {_px(opp.entry_price)} per set"
    at = f" at {_px(opp.entry_price)}" if opp.entry_price is not None else ""
    return f"{opp.kind} - buy {opp.side.upper()} on {label}{at}"


def _headline(mode: str, days_left: float, opps: Sequence[Opportunity]) -> str:
    counts = {kind: sum(1 for o in opps if o.kind == kind) for kind in _KIND_ORDER}
    trades = counts["fade"] + counts["carry"] + counts["arbitrage"]
    text = (f"{mode.capitalize()} mode, {days_left:.0f} days left: {trades} trade idea{'s' if trades != 1 else ''} "
            f"({counts['fade']} fade, {counts['carry']} carry, {counts['arbitrage']} arbitrage)")
    if counts["watch"]:
        text += f", {counts['watch']} surge{'s' if counts['watch'] != 1 else ''} to watch"
    text += "."
    best = next((o for o in opps if o.kind != "watch" and o.score > 0), None)
    if best is not None:
        text += f" Top idea: {_describe(best)}."
    return text


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


def _resolve_conflicts(opps: List[Opportunity], infos: Mapping[str, ExchangeInfo],
                       latest: Mapping[str, PricePoint]) -> List[Opportunity]:
    """Ideas must not contradict each other.

    * A tradable arbitrage buys one side of each leg; a carry on the other side of the same
      outcome is dropped (holding both pays exactly 1.00 for more than 1.00), and a fade or
      watch on it is flagged.
    * A carry buying NO on one outcome of a multi-outcome market is flagged when buying YES on
      every other outcome costs less (it pays the same: 1.00 unless this outcome wins).
    """
    legs: Dict[str, str] = {}
    for opp in opps:
        if opp.kind != "arbitrage" or opp.suggested_shares <= 0:
            continue
        for leg in opp.legs or ([{"exchange_id": opp.exchange_id, "side": opp.side}] if opp.exchange_id else []):
            if leg.get("exchange_id") is not None:
                legs.setdefault(str(leg["exchange_id"]), str(leg.get("side") or ""))
    by_market: Dict[str, List[str]] = {}
    for info in infos.values():
        by_market.setdefault(str(info.market_id), []).append(str(info.exchange_id))
    out: List[Opportunity] = []
    for opp in opps:
        eid = str(opp.exchange_id) if opp.exchange_id is not None else None
        arb_side = legs.get(eid) if eid is not None and opp.kind in ("carry", "fade", "watch") else None
        if arb_side and arb_side != opp.side:
            if opp.kind == "carry":
                continue
            opp.risks.insert(0, f"Conflicts with the arbitrage idea, which buys {arb_side.upper()} on this outcome: "
                                f"holding both {arb_side.upper()} and {opp.side.upper()} pays exactly 1.00 for more than 1.00")
        if opp.kind == "carry" and opp.side == "no" and eid is not None and opp.entry_price is not None:
            others = [x for x in by_market.get(str(opp.market_id), []) if x != eid]
            asks = [_quote_cost("yes", latest.get(x)) for x in others]
            if others and all(a is not None for a in asks):
                cheaper = round(sum(asks), 6)  # type: ignore[arg-type]
                if cheaper < opp.entry_price - _EPS:
                    opp.risks.insert(0, f"Cheaper for the same payoff: YES on every other outcome of this market costs "
                                        f"{_px(cheaper)} in total (vs NO at {_px(opp.entry_price)}), and also pays 1.00 "
                                        "unless this outcome wins")
        out.append(opp)
    return out


def build_report(*, now: float, surges: Sequence[Surge], bands: Sequence[HighBand],
                 latest: Mapping[str, PricePoint], infos: Mapping[str, ExchangeInfo],
                 balance: Optional[float], initial_balance: Optional[float], leader_value: Optional[float],
                 my_rank: Optional[int], cup_end: float, constraints: Optional[Mapping[str, Any]] = None,
                 overround_rows: Sequence[Mapping[str, Any]] = (), backtest: Optional[BacktestResult] = None,
                 kelly_mult: float = KELLY_MULT, max_position_pct: float = MAX_POSITION_PCT,
                 top_n: int = TOP_N, account_value: Optional[float] = None) -> StrategyReport:
    """Ranked, sized, read-only ideas plus the tournament posture.

    Uses the newest open surge per exchange (participants -> fade, unclear -> watch), every
    stable band (carry) and every arbitrage flag. Scores are ``expected_return x confidence``
    times the risk-mode multiplier; the top ``top_n`` are kept, best first. Sizing uses
    ``balance``, else ``initial_balance``, else 100,000. ``assumptions`` lists what was assumed
    for unknown inputs (balance, rank, leader value), and sized ideas say so too.

    Surges whose status is not ``open`` (reverted, held, or ``closed`` because their market
    left the open list) never produce fade or watch ideas, and neither does a surge that is
    the reversion of a recent opposite surge on the same outcome. Ideas that contradict a
    tradable arbitrage are dropped or flagged (see :func:`_resolve_conflicts`).

    ``balance`` is cash (it sizes the ideas); ``account_value`` (cash + open positions) is what
    the risk mode compares with the leader's value, falling back to cash when unknown. Sizes
    are capped by the order-book depth on the ``latest`` points when known.
    """
    latest = latest or {}
    infos = infos or {}
    initial = _num(initial_balance)
    if initial is None or initial <= 0:
        initial = DEFAULT_INITIAL_BALANCE
    bal = _num(balance)
    sizing = bal if bal is not None else initial
    days_left = max(0.0, (cup_end - now) / 86400.0)
    rank = my_rank if isinstance(my_rank, int) and not isinstance(my_rank, bool) else None
    leader = _num(leader_value)
    value = _num(account_value)
    mode = risk_mode(bal, initial, leader, rank, days_left, value)

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
    opps: List[Opportunity] = []
    for eid in sorted(newest):
        s = newest[eid]
        verdict = s.attribution.verdict if s.attribution is not None else None
        opp: Optional[Opportunity] = None
        if verdict == VERDICT_PARTICIPANTS:
            opp = fade_opportunity(s, latest.get(eid), infos.get(eid), now, sizing, backtest, cup_end=cup_end,
                                   kelly_mult=kelly_mult, max_position_pct=max_position_pct)
        elif verdict == VERDICT_UNCLEAR:
            opp = watch_opportunity(s, infos.get(eid))
        if opp is not None:
            opps.append(opp)
    for band in bands or ():
        opp = carry_opportunity(band, latest.get(band.exchange_id), infos.get(band.exchange_id), now, cup_end, sizing,
                                kelly_mult=kelly_mult, max_position_pct=max_position_pct)
        if opp is not None:
            opps.append(opp)
    opps.extend(arbitrage_opportunities(constraints, overround_rows, sizing, max_position_pct=max_position_pct,
                                        latest=latest, infos=infos, cup_end=cup_end, now=now))
    opps = _resolve_conflicts(opps, infos, latest)
    if bal is None:
        for opp in opps:
            if opp.suggested_shares > 0:
                opp.rationale.append(f"Balance unknown: this size assumes the {sizing:,.0f} starting balance")

    multipliers = MODE_MULTIPLIERS.get(mode, {})
    for opp in opps:
        opp.score = round(opp.score * multipliers.get(opp.kind, 1.0), 6)
    opps.sort(key=lambda o: (-o.score, _KIND_ORDER.get(o.kind, 9), -o.confidence, o.title, o.exchange_id or ""))
    opps = opps[: max(0, top_n)]

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
        backtest=backtest,
        assumptions=_assumptions(mode, bal, sizing, leader, rank, days_left, value),
    )
