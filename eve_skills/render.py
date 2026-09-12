"""Plain-text rendering helpers."""

from __future__ import annotations

from datetime import datetime


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_opt(value: str | None) -> datetime | None:
    """Parse an ESI timestamp; CCP omits start/finish dates for queue items
    that cannot begin training (e.g. beyond alpha restrictions)."""
    return parse_ts(value) if value else None


def format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def format_sp(sp: int) -> str:
    if sp >= 1_000_000:
        return f"{sp / 1_000_000:.2f}M"
    if sp >= 1_000:
        return f"{sp / 1_000:.1f}K"
    return str(sp)


def isk(value: float | None) -> str:
    """ISK with thousands separators; "-" when there is no figure to show. A dash admits nobody is
    quoting that side, or that ESI priced nothing on this basis; `0.00` would claim the thing is
    worthless, and those are different statements."""
    return "-" if value is None else f"{value:,.2f}"


def csv_cell(value) -> str:
    """CSV cell for an optional value: empty when unknown, unformatted otherwise - and bools as 1/0,
    the convention every CSV in this tool already uses."""
    if value is None:
        return ""
    return str(int(value)) if isinstance(value, bool) else str(value)


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    return "\n".join(lines)
