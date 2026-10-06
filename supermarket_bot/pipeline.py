"""Shared input assembly for the Strategy view, the paper trader and headless simulations
(package E of docs/PAPER_TRADING.md §7.3).

``web.DashboardApp._build_strategy`` used to assemble the strategy's inputs itself; this module
does it once for every consumer so the live Strategy view and the paper trader see the same
inputs. Nothing here performs a read against the Super Market API: it uses what the tracker and
the store already hold.

Import rule: this module must not import ``web`` at module level (web imports pipeline).
``context_summary`` lives here now and ``web`` re-exports it under the old name.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .books import parse_time
from .models import (
    SURGE_CLOSED,
    BookObservation,
    ExchangeInfo,
    HighBand,
    LeaderboardSnapshot,
    MarketObservation,
    PricePoint,
    Quote,
    SettlementInfo,
    StrategyInputs,
    StrategyParams,
)

log = logging.getLogger("supermarket_bot")

SURGE_LOOKBACK_S = 2 * 86400.0
LEADERBOARD_HISTORY_S = 7 * 86400.0
RECENT_MIDS_S = 300.0  # StrategyInputs.recent_mids window
DEFAULT_INITIAL_BALANCE = 100_000.0
REFUND_WORDS = ("REFUND", "REFUNDED", "VOID", "VOIDED", "CANCELLED", "CANCELED")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- small helpers


def _finite(value: Any) -> Optional[float]:
    """A finite float, or None for missing / non-numeric / bool / NaN / inf values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _plain(value: Any) -> Any:
    """Dataclasses -> dicts (via ``to_dict``), recursively; other values unchanged."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = value.to_dict() if hasattr(value, "to_dict") else dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _epoch(value: Any) -> Optional[float]:
    """Epoch seconds from a number or an ISO string (None otherwise)."""
    number = _finite(value)
    if number is not None:
        return number
    parsed = parse_time(value)
    return parsed.timestamp() if parsed is not None else None


def _pick(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _known(cls: Any, data: Mapping[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


# --------------------------------------------------------------------------- context


def context_summary(raw: Any) -> Dict[str, Any]:
    """Normalise the tracker's ``view()["context"]`` (balance, leaderboard, constraints, overround).

    Moved verbatim from ``web.context_summary`` (web re-exports it), plus ``account_value`` (cash plus
    positions from the tracker context, codebase_map #14): the leaderboard's value counts positions,
    so comparing it with cash alone would call a player with money in positions "far behind"."""
    ctx = _plain(raw) if raw is not None else {}
    if not isinstance(ctx, Mapping):
        ctx = {}
    tournament = ctx.get("tournament") if isinstance(ctx.get("tournament"), Mapping) else {}
    board_raw = ctx.get("leaderboard")
    board_leader_value: Optional[float] = None
    if isinstance(board_raw, Mapping):
        # The tracker stores {"top": [...], "my_rank", "leader_value"}; the raw API uses "leaderboard".
        entries = (
            board_raw.get("top") or board_raw.get("leaderboard") or board_raw.get("entries") or board_raw.get("data") or []
        )
        board_rank = board_raw.get("myRank", board_raw.get("my_rank"))
        board_leader_value = _finite(board_raw.get("leader_value"))
    elif isinstance(board_raw, list):
        entries, board_rank = board_raw, None
    else:
        entries, board_rank = [], None
    leaders = []
    for entry in entries[:3] if isinstance(entries, list) else []:
        if isinstance(entry, Mapping):
            leaders.append(
                {
                    "rank": entry.get("rank"),
                    "username": entry.get("username") or entry.get("name"),
                    "pnl": _finite(entry.get("pnl")),
                    "value": _finite(_pick(entry, "value", "finalTotalValue", "totalValue")),
                }
            )
    balance = _finite(_pick(ctx, "balance", "my_balance", "myBalance"))
    if balance is None:
        balance = _finite(tournament.get("myBalance"))
    initial = _finite(_pick(ctx, "initial_balance", "initialBalance"))
    if initial is None:
        initial = _finite(tournament.get("initialBalance"))
    my_rank = _pick(ctx, "my_rank", "myRank")
    if my_rank is None:
        my_rank = board_rank
    my_rank = int(my_rank) if isinstance(my_rank, (int, float)) and not isinstance(my_rank, bool) else None
    leader_value = _finite(ctx.get("leader_value"))
    if leader_value is None:
        leader_value = board_leader_value
    if leader_value is None and leaders:
        top = leaders[0]
        if top["value"] is not None:
            leader_value = top["value"]
        elif top["pnl"] is not None:
            leader_value = (initial if initial is not None else DEFAULT_INITIAL_BALANCE) + top["pnl"]
    constraints = ctx.get("constraints")
    if isinstance(constraints, list):
        constraints = {"data": constraints, "violationsCount": len(constraints)}
    elif not isinstance(constraints, Mapping):
        constraints = None
    overround = _pick(ctx, "overround", "overround_rows", "markets", default=[])
    if not isinstance(overround, list):
        overround = []
    return {
        "balance": balance,
        "initial_balance": initial,
        "account_value": _finite(_pick(ctx, "account_value", "accountValue", "totalAccountValue")),
        "my_rank": my_rank,
        "leader_value": leader_value,
        "leaders": leaders,
        "constraints": constraints,
        "overround": [row for row in overround if isinstance(row, Mapping)],
        "tournament": dict(tournament),
        "updated_at": _epoch(_pick(ctx, "updated_at", "refreshed_at", "fetched_at")),
    }


def leaderboard_snapshot(context: Mapping[str, Any], now: float) -> Optional[LeaderboardSnapshot]:
    """The tracker context's ``leaderboard`` (``entries`` when present, else ``top``) as a snapshot."""
    if not isinstance(context, Mapping):
        return None
    board = context.get("leaderboard")
    if not isinstance(board, Mapping):
        return None
    raw = board.get("entries")
    if not isinstance(raw, list) or not raw:
        raw = board.get("top")
    if not isinstance(raw, list):
        raw = []
    initial = _finite(context.get("initial_balance"))
    if initial is None and isinstance(context.get("tournament"), Mapping):
        initial = _finite(context["tournament"].get("initialBalance"))
    entries: List[Dict[str, Any]] = []
    for entry in raw[:100]:
        if not isinstance(entry, Mapping):
            continue
        entries.append({
            "rank": _int(entry.get("rank")),
            "username": entry.get("username") if isinstance(entry.get("username"), str) else None,
            "pnl": _finite(entry.get("pnl")),
            "value": _finite(entry.get("value")),
        })
    rank = board.get("my_rank", board.get("myRank"))
    period = board.get("period")
    return LeaderboardSnapshot(
        at=float(now),
        period=str(period) if isinstance(period, str) and period else "all",
        total=_int(board.get("total")),
        my_rank=_int(rank),
        initial_balance=initial,
        entries=entries,
    )


# --------------------------------------------------------------------------- version


def _git_dir(root: Path) -> Optional[Path]:
    git = root / ".git"
    if git.is_file():  # a worktree or submodule: "gitdir: <path>"
        try:
            text = git.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.startswith("gitdir:"):
            return None
        target = Path(text[len("gitdir:"):].strip())
        git = target if target.is_absolute() else (root / target)
    return git if git.is_dir() else None


def _git_commit(root: Path) -> Optional[str]:
    """The checked-out commit of the git repository at ``root`` (no subprocess), or None."""
    git = _git_dir(root)
    if git is None:
        return None
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if _HEX40.match(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head[len("ref:"):].strip()
    common = git
    try:
        if (git / "commondir").is_file():
            common_text = (git / "commondir").read_text(encoding="utf-8").strip()
            common = Path(common_text) if Path(common_text).is_absolute() else (git / common_text)
    except OSError:
        pass
    for base in (git, common):
        try:
            text = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _HEX40.match(text):
            return text
    for base in (git, common):
        try:
            lines = (base / "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref and _HEX40.match(parts[0]):
                return parts[0]
    return None


def code_version() -> str:
    """``"<package __version__>+<12-char git commit>"`` when the package runs from a git checkout (read
    ``.git/HEAD`` and the ref file directly, no subprocess), else the package version. Part of the run
    fingerprint (§6.11): new code starts a new run."""
    from . import __version__

    try:
        commit = _git_commit(Path(__file__).resolve().parent.parent)
    except Exception:  # never let a broken checkout stop the bot
        commit = None
    return f"{__version__}+{commit[:12]}" if commit else str(__version__)


# --------------------------------------------------------------------------- inputs


def _store_books(store: Any, exchange_ids: Sequence[str]) -> Dict[str, Any]:
    """Stored books of ``exchange_ids``: one ``store.books(ids)`` query when the store has it (TrackerStore),
    else ``store.book(eid)`` per id. Missing or failing reads are left out."""
    ids = [str(e) for e in exchange_ids]
    if not ids:
        return {}
    batch = getattr(store, "books", None)
    if callable(batch):
        try:
            return dict(batch(ids) or {})
        except Exception as exc:
            log.debug("stored books unavailable: %s", exc)
            return {}
    out: Dict[str, Any] = {}
    for eid in ids:
        try:
            book = store.book(eid)
        except Exception:
            book = None
        if book is not None:
            out[eid] = book
    return out


def _view_rows(view: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in view.get("exchanges") or []:
        row = raw if isinstance(raw, Mapping) else _plain(raw)  # a plain row needs no deep walk (cost per step)
        if isinstance(row, Mapping) and _pick(row, "exchange_id", "id") is not None:
            out = dict(row)
            out["exchange_id"] = str(_pick(row, "exchange_id", "id"))
            rows.append(out)
    return rows


def _bands(view: Mapping[str, Any]) -> List[HighBand]:
    bands: List[HighBand] = []
    for raw in view.get("high_band") or []:
        if isinstance(raw, HighBand):
            bands.append(raw)
            continue
        band = _plain(raw)
        if isinstance(band, Mapping):
            try:
                bands.append(HighBand(**_known(HighBand, band)))
            except TypeError:
                log.debug("skipping malformed high band %r", band)
    return bands


def _cup_end(tournament: Optional[Mapping[str, Any]]) -> float:
    from . import strategy

    return float(strategy.cup_end_ts(tournament or None))


def _fair_value_map(fair_values: Any) -> Dict[str, Any]:
    if fair_values is None:
        return {}
    current = getattr(fair_values, "current", None)
    if not callable(current):
        return dict(fair_values) if isinstance(fair_values, Mapping) else {}
    try:
        snap = current()
    except Exception as exc:  # a broken fair-value service must not stop the strategy
        log.warning("fair values unavailable: %s", exc)
        return {}
    if not getattr(snap, "enabled", True):
        return {}
    return dict(getattr(snap, "values", {}) or {})


def _races(races: Optional[Mapping[str, Any]], fair_values: Any, infos: Mapping[str, ExchangeInfo]) -> Dict[str, Any]:
    if races is not None:
        return {str(k): v for k, v in races.items()}
    getter = getattr(fair_values, "races", None) if fair_values is not None else None
    if callable(getter):
        try:
            known = dict(getter() or {})
        except Exception:
            known = {}
        if known:
            out = {eid: known[eid] for eid in infos if eid in known}
            missing = [info for eid, info in infos.items() if eid not in known]
            if missing:
                out.update(_races_for(missing))
            return out
    return _races_for(list(infos.values()))


def _races_for(infos: Sequence[ExchangeInfo]) -> Dict[str, Any]:
    try:
        from .fairvalue import races_for

        return dict(races_for(infos))
    except Exception as exc:  # the race parser is optional for the strategy (baskets need it)
        log.debug("races_for failed: %s", exc)
        return {}


def _bar(leaderboard: Optional[LeaderboardSnapshot], store: Any, now: float, initial: Optional[float],
         days_left: float) -> Any:
    if leaderboard is None:
        return None
    history: List[LeaderboardSnapshot] = []
    getter = getattr(store, "leaderboard_snapshots", None)
    if callable(getter):
        try:
            history = list(getter(now - LEADERBOARD_HISTORY_S, now))
        except Exception as exc:
            log.debug("leaderboard history unavailable: %s", exc)
    try:
        from .sizing import estimate_bar

        return estimate_bar(leaderboard, history, initial, days_left)
    except Exception as exc:  # no bar: the chaser sizes in "unknown_bar" mode
        log.debug("estimate_bar failed: %s", exc)
        return None


def assemble_inputs(*, now: float, view: Mapping[str, Any], store: Any, fair_values: Any = None,
                    races: Optional[Mapping[str, Any]] = None, backtest: Any = None,
                    regime: str = "unknown", params: Optional[StrategyParams] = None,
                    cup_end: Optional[float] = None,
                    recent_mids: Optional[Mapping[str, Sequence[Any]]] = None) -> StrategyInputs:
    """StrategyInputs from the tracker's ``view()`` and the store (§7.3):

    open outcomes = the view's exchange rows; ``latest`` = the view row's bid/ask/last at its
    ``updated_at`` with ``store.book(eid)`` attached (rows flagged ``stale`` are left out of
    ``latest``, so they yield no trade ideas); surges = ``store.surges(since=now - 2 d)`` on
    open outcomes, not closed; bands from ``view["high_band"]``; balance / initial / account value /
    rank / leader value / constraints / overround from ``context_summary(view["context"])``;
    fair values from ``fair_values.current().values`` (a FairValueService, or None); races from
    ``races`` or ``fairvalue.races_for(infos)``; leaderboard + bar via ``sizing.estimate_bar`` with
    ``store.leaderboard_snapshots(now - 7 d, now)`` as history; ``recent_mids`` (the tracker's
    ``recent_mids(RECENT_MIDS_S)``, from its in-memory series cache, no SQL) as given.

    A row without ``updated_at`` (a view from an older tracker) is priced from ``store.latest(eid)``."""
    view = view if isinstance(view, Mapping) else {}
    ctx = context_summary(view.get("context"))
    rows = _view_rows(view)
    open_ids = [r["exchange_id"] for r in rows]
    open_set = set(open_ids)
    try:
        stored = {str(e.exchange_id): e for e in store.exchanges()}
    except Exception as exc:
        log.warning("stored outcomes unavailable: %s", exc)
        stored = {}
    infos: Dict[str, ExchangeInfo] = {}
    for row in rows:
        eid = row["exchange_id"]
        info = stored.get(eid)
        if info is None:
            info = ExchangeInfo(exchange_id=eid, market_id=str(row.get("market_id") or ""), option=row.get("option"),
                                market_title=str(row.get("title") or ""), settlement_date=row.get("settlement_date"))
        infos[eid] = info
    latest: Dict[str, PricePoint] = {}
    books = _store_books(store, [r["exchange_id"] for r in rows
                                 if not r.get("stale") and _finite(r.get("updated_at")) is not None])
    for row in rows:
        eid = row["exchange_id"]
        if row.get("stale"):
            continue  # not a current quote: no trade idea may be priced off it
        ts = _finite(row.get("updated_at"))
        if ts is None:
            try:
                point = store.latest(eid)
            except Exception:
                point = None
            if point is not None:
                latest[eid] = point
            continue
        book = books.get(eid)
        latest[eid] = PricePoint(ts=ts, price=_finite(row.get("mark")), last=_finite(row.get("last")),
                                 bid=_finite(row.get("bid")), ask=_finite(row.get("ask")), source="tick", book=book)
    try:
        surges = [s for s in store.surges(since=now - SURGE_LOOKBACK_S, limit=200)
                  if str(s.exchange_id) in open_set and s.status != SURGE_CLOSED]
    except Exception as exc:
        log.warning("stored surges unavailable: %s", exc)
        surges = []
    if cup_end is None:
        cup_end = _cup_end(ctx["tournament"])
    days_left = max(0.0, (cup_end - now) / 86400.0)
    initial = ctx["initial_balance"]
    board = leaderboard_snapshot(view.get("context") or {}, now)
    bar = _bar(board, store, now, initial, days_left)
    mids: Dict[str, List[Tuple[float, float]]] = {}
    for eid, pts in (recent_mids or {}).items():
        clean: List[Tuple[float, float]] = []
        for item in pts or ():
            try:
                ts, mid = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if math.isfinite(ts) and math.isfinite(mid):
                clean.append((ts, mid))
        mids[str(eid)] = sorted(clean)
    return StrategyInputs(
        now=float(now),
        cup_end=float(cup_end),
        infos=infos,
        latest=latest,
        surges=surges,
        bands=_bands(view),
        constraints=dict(ctx["constraints"]) if ctx["constraints"] is not None else None,
        overround_rows=[dict(r) for r in ctx["overround"]],
        fair_values=_fair_value_map(fair_values),
        races=_races(races, fair_values, infos),
        backtest=backtest,
        balance=ctx["balance"],
        initial_balance=initial,
        account_value=ctx["account_value"],
        leader_value=ctx["leader_value"],
        my_rank=ctx["my_rank"],
        leaderboard=board,
        bar=bar,
        settlement_regime=str(regime or "unknown"),
        params=params,
        recent_mids=mids,
    )


def _levels(raw: Any) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for level in raw or []:
        try:
            price, qty = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append((price, qty))
    return out


def observation(*, now: float, inputs: StrategyInputs, view: Mapping[str, Any], store: Any,
                settlements: Optional[Mapping[str, SettlementInfo]] = None,
                open_ids: Optional[Sequence[str]] = None) -> MarketObservation:
    """The base MarketObservation for a paper step (no reads): quotes from the view rows (``ts`` =
    the row's ``updated_at``), books from ``store.book`` as BookObservation(source="tracker"),
    settlements from ``store.settlements()`` when not given, open ids, infos, fair values, races."""
    view = view if isinstance(view, Mapping) else {}
    rows = _view_rows(view)
    quotes: Dict[str, Quote] = {}
    for row in rows:
        ts = _finite(row.get("updated_at"))
        if ts is None:
            continue
        eid = row["exchange_id"]
        quotes[eid] = Quote(exchange_id=eid, bid=_finite(row.get("bid")), ask=_finite(row.get("ask")),
                            last=_finite(row.get("last")), ts=ts)
    books: Dict[str, BookObservation] = {}
    known: Dict[str, Any] = {}
    for eid in inputs.infos:
        point = (inputs.latest or {}).get(eid)
        book = getattr(point, "book", None) if point is not None else None  # assemble_inputs read it already
        if book is not None:
            known[eid] = book
    missing = [eid for eid in inputs.infos if eid not in known]
    if missing:
        known.update(_store_books(store, missing))  # one query for the rest (stale rows, rows without a book)
    for eid in inputs.infos:
        book = known.get(eid)
        if isinstance(book, Mapping) and _finite(book.get("at")) is not None:
            books[eid] = BookObservation(exchange_id=eid, observed_at=float(book["at"]), bids=_levels(book.get("bids")),
                                         asks=_levels(book.get("asks")), source="tracker")
    if settlements is None:
        try:
            settlements = store.settlements()
        except Exception as exc:
            log.warning("stored settlements unavailable: %s", exc)
            settlements = {}
    ids: Set[str] = {str(e) for e in open_ids} if open_ids is not None else {r["exchange_id"] for r in rows}
    return MarketObservation(
        now=float(now),
        cup_end=float(inputs.cup_end),
        quotes=quotes,
        books=books,
        trades={},
        settlements={str(k): v for k, v in (settlements or {}).items()},
        open_ids=ids,
        infos=dict(inputs.infos),
        fair_values=dict(inputs.fair_values),
        races=dict(inputs.races),
    )


def settlements_from_market(market: Mapping[str, Any], detected_at: float) -> List[SettlementInfo]:
    """One SettlementInfo per exchange of a settled market (``GET /tournaments/{slug}/markets?status=settled``
    row): binary "YES"/"NO" -> payout 1/0; multi-outcome -> the exchange whose option (or id) equals
    ``settledWith`` pays 1, the others 0; "REFUND"/"VOID"/"CANCELLED" -> refund; anything else ->
    payout None (the engine freezes and flags it). Case-insensitive.

    A row that is neither marked settled nor carries ``settledWith`` is not a settlement (empty list)."""
    if not isinstance(market, Mapping) or market.get("id") is None:
        return []
    raw = market.get("settledWith")
    status = str(market.get("status") or "").strip().lower()
    if raw is None and status not in ("settled", "resolved"):
        return []
    text = str(raw).strip() if raw is not None else ""
    upper = text.upper()
    exchanges = [ex for ex in market.get("exchanges") or [] if isinstance(ex, Mapping) and ex.get("id") is not None]
    multi = market.get("isMultiOutcome")
    binary = (not multi) if isinstance(multi, bool) else len(exchanges) <= 1
    refund = upper in REFUND_WORDS
    settled_on = _epoch(market.get("settledOn"))
    winners: List[str] = []
    if text and not refund:
        for ex in exchanges:
            option = str(ex.get("option") or "").strip().upper()
            if (option and option == upper) or str(ex["id"]).strip().upper() == upper:
                winners.append(str(ex["id"]))
    out: List[SettlementInfo] = []
    for ex in exchanges:
        eid = str(ex["id"])
        payout: Optional[float] = None
        if refund:
            payout = None
        elif binary and upper in ("YES", "NO"):
            payout = 1.0 if upper == "YES" else 0.0
        elif winners:
            payout = 1.0 if eid in winners else 0.0
        out.append(SettlementInfo(exchange_id=eid, market_id=str(market["id"]), settled_with=raw if raw is None else str(raw),
                                  settled_on=settled_on, payout_yes=payout, refund=refund, detected_at=float(detected_at)))
    return out


# --------------------------------------------------------------------------- backtest vs the paper runs

# appended to the backtest's OVERLAP_WARNING when the replay filled on stored order books (lookahead-1)
OVERLAP_BOOKS_NOTE = ("The real order books it fills on there are the ones the paper run read itself, right after "
                      "its own decisions, so the replay re-uses the paper run's data rather than testing it.")
_OVERLAP_MARK = "not an independent check"  # in backtest.OVERLAP_WARNING (the dashboard matches it too)


def paper_run_intervals(source: Any, now: Optional[float] = None) -> List[Tuple[float, float]]:
    """``[(start, end)]`` of every paper run the store holds (completed, reset, "settings changed" or still open):
    an ended run ends at ``ended_at``, an open one at max(``updated_at``, ``now``) (open-ended without either).
    [] when the source keeps no paper runs."""
    rows: Any = None
    for name in ("paper_run_times", "paper_runs"):
        fn = getattr(source, name, None)
        if not callable(fn):
            continue
        try:
            rows = fn()
        except Exception as exc:
            log.debug("could not read the paper runs (%s): %s", name, exc)
            rows = None
            continue
        break
    out: List[Tuple[float, float]] = []
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        start = _finite(r.get("started_at"))
        if start is None:
            continue
        end = _finite(r.get("ended_at"))
        if end is None:
            ends = [v for v in (_finite(r.get("updated_at")), _finite(now)) if v is not None]
            end = max(ends) if ends else float("inf")
        if end > start:
            out.append((start, end))
    return out


def overlap_seconds(intervals: Sequence[Tuple[float, float]], start: float, end: float) -> float:
    """Length of the union of ``intervals`` inside ``[start, end]`` (overlapping runs are counted once)."""
    parts = sorted((max(float(a), float(start)), min(float(b), float(end))) for a, b in intervals)
    total, cur_a, cur_b = 0.0, None, None
    for a, b in parts:
        if b <= a:
            continue
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a  # type: ignore[operator]
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a  # type: ignore[operator]
    return total


def apply_paper_overlap(report: Any, intervals: Sequence[Tuple[float, float]]) -> Any:
    """lookahead-1 (§6.12.3, D26): a backtest window that a paper run also saw is not an independent check. Sets
    ``report.overlap_hours`` from EVERY stored paper run (``paper_run_intervals``: the current one and the ones
    that ended -- completed, reset, or "settings changed" after a code update), not only from the current run's
    start, and puts the backtest's OVERLAP_WARNING (with OVERLAP_BOOKS_NOTE when the replay filled on stored
    order books, which only the paper run writes) first in ``warnings``. Idempotent: an overlap warning the
    replay added itself is replaced by this one (the larger of the two overlaps is kept)."""
    from .backtest import OVERLAP_WARNING

    window = getattr(report, "window", None) or {}
    start, end = _finite(window.get("start")), _finite(window.get("end"))
    if start is None or end is None or end <= start:
        return report
    existing = _finite(getattr(report, "overlap_hours", None))
    if not intervals and existing is None:
        return report
    hours = max(overlap_seconds(intervals, start, end) / 3600.0, existing or 0.0)
    report.overlap_hours = round(hours, 6)
    warnings = [w for w in list(getattr(report, "warnings", None) or []) if _OVERLAP_MARK not in str(w)]
    if hours > 0:
        text = OVERLAP_WARNING.format(h=hours)
        coverage = getattr(report, "coverage", None) or {}
        if (_finite(coverage.get("book_snapshot_share")) or 0.0) > 0:
            text += " " + OVERLAP_BOOKS_NOTE
        warnings.insert(0, text)
    report.warnings = warnings
    return report


# --------------------------------------------------------------------------- headless driver


def run_simulation(runtime: Any, clock: Any, *, hours: float, step_s: float = 30.0,
                   summary_every_s: float = 3600.0, on_summary: Optional[Callable[[Dict[str, Any]], None]] = None,
                   backfill_reads_per_step: int = 8, analyses_per_step: int = 2,
                   end_run: bool = True, moves_every_s: Optional[float] = None) -> Dict[str, Any]:
    """Drive a demo runtime synchronously on a fake ``clock`` (demo.SimClock) for ``hours`` of
    simulated time (§7.6): each step advances the clock, then runs ``tracker.run_once()``, a few
    backfill and analysis steps, ``tracker.fair_value_step()`` (every 60 simulated s) and
    ``tracker.paper_step()``. Single-threaded and deterministic: every limiter in the runtime runs on the
    SimClock with a ``sleep`` that raises ``demo.SimClockStall`` instead of waiting (build_demo(clock=...)
    sets this up), so a budget that would need a wait fails loudly instead of depending on CPU speed.
    Calls ``on_summary(paper summary)`` every ``summary_every_s``; ends the run ("completed") when
    ``end_run``; returns the final summary.

    The first step runs at the clock's current time (the demo start), the last one ``hours`` later.

    ``moves_every_s`` (docs/OUTSIDE_MOVES.md §16, package "wiring"): when the tracker has an outside-move watcher,
    ``tracker.moves_step(t)`` also runs every ``moves_every_s`` simulated seconds: at each step time after the
    paper step, then at the sub-ticks strictly between two steps (the clock advances to each sub-tick, so one step
    still advances it by ``step_s`` in all). None (the default) keeps the loop exactly as before. Without a paper
    trader, ``on_summary`` then gets the watcher's summary (``tracker.moves_view()``) and so does the return value."""
    from .fairvalue import FV_REFRESH_S

    tracker = runtime.tracker
    if step_s <= 0:
        raise ValueError("step_s must be > 0")
    moves = moves_every_s is not None and getattr(tracker, "moves", None) is not None
    if moves_every_s is not None and not float(moves_every_s) > 0:
        raise ValueError("moves_every_s must be > 0")
    steps = max(0, int(round(float(hours) * 3600.0 / float(step_s))))
    next_fv = float(clock())
    next_summary = float(clock()) + float(summary_every_s)
    step_at = float(clock())  # the current step's time (with sub-ticks the clock sits at the last sub-tick)
    for i in range(steps + 1):
        if i:
            if moves:
                step_at += step_s
                clock.set(step_at)
            else:
                clock.advance(step_s)
        tracker.run_once()
        if backfill_reads_per_step > 0:
            tracker.backfill_step(backfill_reads_per_step)
        if analyses_per_step > 0:
            tracker.analyze_pending(analyses_per_step)
        now = float(clock())
        if now >= next_fv - 1e-9:
            tracker.fair_value_step(now)
            while next_fv <= now + 1e-9:
                next_fv += FV_REFRESH_S
        tracker.paper_step(now)
        if moves:
            tracker.moves_step(now)
        if on_summary is not None and summary_every_s > 0 and now >= next_summary - 1e-9:
            summary = tracker.paper_view()
            if summary is None and moves:
                summary = tracker.moves_view()
            if summary is not None:
                on_summary(summary)
            while next_summary <= now + 1e-9:
                next_summary += summary_every_s
        if moves and i < steps:  # the sub-ticks strictly before the next step's time
            j = 1
            while True:
                sub = step_at + j * float(moves_every_s)  # on the step grid, whatever the clock read above
                if sub >= step_at + float(step_s) - 1e-9:
                    break
                clock.set(sub)
                tracker.moves_step(sub)
                j += 1
    if end_run:
        tracker.paper_end("completed")
    final = tracker.paper_view()
    if final is None and moves:
        final = tracker.moves_view()
    return final or {}
