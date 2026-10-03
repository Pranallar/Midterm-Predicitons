"""Plain-text formatting helpers (no third-party dependencies)."""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

Column = Tuple[str, str]  # (row key, header)


def fmt_price(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    return f"{value:.3f}"


def fmt_num(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.{digits}f}"


def fmt_delta(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:+.3f}"


def truncate(text: Any, width: int) -> str:
    s = "" if text is None else str(text)
    if len(s) <= width:
        return s
    if width <= 0:
        return ""
    return s[: width - 1] + "…"


def table(rows: Iterable[Mapping[str, Any]], columns: Sequence[Column], max_width: Optional[Mapping[str, int]] = None) -> str:
    """Render rows as an aligned text table. Values should already be strings or numbers."""
    widths = dict(max_width or {})
    body: List[List[str]] = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            text = "" if value is None else str(value)
            if key in widths:
                text = truncate(text, widths[key])
            cells.append(text)
        body.append(cells)
    headers = [header for _, header in columns]
    col_w = [len(h) for h in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            col_w[i] = max(col_w[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "  ".join(cell.ljust(col_w[i]) for i, cell in enumerate(cells)).rstrip()

    out = [line(headers), line(["-" * w for w in col_w])]
    out.extend(line(cells) for cells in body)
    return "\n".join(out)


def sparkline(values: Sequence[Optional[float]]) -> str:
    ticks = "▁▂▃▄▅▆▇█"
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo or 1.0
    return "".join(
        " " if not isinstance(v, (int, float)) else ticks[min(len(ticks) - 1, int((v - lo) / span * (len(ticks) - 1)))]
        for v in values
    )
