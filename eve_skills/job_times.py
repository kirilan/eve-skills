"""Measured industry job times and run counts that finish inside chosen local-time windows.

`jobs --times` groups past and running jobs by activity, product and installer and reports the median
hours per run (per attempt for invention, per copy for copying) - the real figure after skills,
implants, facility and blueprint TE, which the blueprint's base time is not. With `--finish-window`
it also says how many runs, installed at `--start`, end inside a window such as 08:00-10:00.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Statuses whose start/end span is a real run time. A cancelled or reverted job's end_date is the
# planned end of a job that never ran to it; a paused job's clock stopped.
TIMED_STATUSES = {"active", "ready", "delivered"}


def parse_windows(spec: str) -> list[tuple[int, int]]:
    """"08-10,20-22" -> [(480, 600), (1200, 1320)] in minutes after local midnight; HH:MM allowed."""
    def minutes(text: str) -> int:
        hh, _, mm = text.strip().partition(":")
        value = int(hh) * 60 + (int(mm) if mm else 0)
        if not 0 <= value <= 24 * 60:
            raise ValueError
        return value

    windows = []
    for part in spec.split(","):
        lo, sep, hi = part.partition("-")
        try:
            if not sep:
                raise ValueError
            window = (minutes(lo), minutes(hi))
        except ValueError:
            raise RuntimeError(f"--finish-window expects ranges like 08-10,20-22 or 08:30-10:00, got '{part}'")
        if window[0] >= window[1]:
            raise RuntimeError(f"--finish-window range '{part}' must start before it ends")
        windows.append(window)
    if not windows:
        raise RuntimeError("--finish-window needs at least one range")
    return windows


def zone(name: str | None) -> tzinfo:
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(f"unknown time zone '{name}' - use an IANA name such as Europe/Sofia")


def parse_start(spec: str | None, tz: tzinfo, now: datetime) -> datetime:
    """Install time: None = now; HH:MM = the next such local time (up to an hour back counts as today);
    anything else an ISO timestamp."""
    if not spec:
        return now
    if ":" in spec and len(spec) <= 5:
        hh, mm = spec.split(":")
        local_now = now.astimezone(tz)
        local = local_now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if local < local_now - timedelta(hours=1):
            local += timedelta(days=1)
        return local.astimezone(timezone.utc)
    moment = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    return (moment if moment.tzinfo else moment.replace(tzinfo=tz)).astimezone(timezone.utc)


def in_window(moment: datetime, windows: Sequence[tuple[int, int]], tz: tzinfo) -> bool:
    local = moment.astimezone(tz)
    minute = local.hour * 60 + local.minute
    return any(lo <= minute < hi for lo, hi in windows)


def window_fits(start: datetime, hours: float, windows: Sequence[tuple[int, int]], tz: tzinfo, *,
                max_hours: float, max_runs: int | None = None) -> list[dict]:
    """Run counts ending inside a window, longest first: the largest count per window occurrence.

    A longer job that ends in a window beats a shorter one ending at night, so each window keeps the
    most runs that still land in it; callers cap by the runs left on a blueprint with `max_runs`."""
    if hours <= 0:
        return []
    fits: dict[tuple, dict] = {}
    runs = 1
    while runs * hours <= max_hours and (max_runs is None or runs <= max_runs):
        end = start + timedelta(hours=runs * hours)
        if in_window(end, windows, tz):
            local = end.astimezone(tz)
            key = (local.date(), next(i for i, (lo, hi) in enumerate(windows)
                                      if lo <= local.hour * 60 + local.minute < hi))
            fits[key] = {"runs": runs, "end": end.isoformat().replace("+00:00", "Z")}
        runs += 1
    return sorted(fits.values(), key=lambda fit: -fit["runs"])


def median_times(rows: Iterable[dict], since: datetime) -> list[dict]:
    """Median hours per run by (activity, product, installer) over timed jobs started since `since`.

    Rows are `jobs` rows (`_job_rows` output). Invention products are the invented blueprint; the
    ' Blueprint' suffix is kept so an invention row never reads as the manufactured item."""
    groups: dict[tuple, list[float]] = {}
    for row in rows:
        start = row.get("start")
        if (row.get("status") not in TIMED_STATUSES or not row.get("hours_per_run") or start is None
                or start < since):
            continue
        key = (row["activity"], row.get("product") or f"type {row.get('product_id')}",
               row.get("installer") or f"character {row.get('installer_id')}")
        groups.setdefault(key, []).append(float(row["hours_per_run"]))
    out = [{"activity": activity, "product": product, "installer": installer,
            "hours_per_run": statistics.median(values), "min_hours": min(values),
            "max_hours": max(values), "jobs": len(values)}
           for (activity, product, installer), values in groups.items()]
    out.sort(key=lambda r: (r["activity"], r["product"].casefold(), r["hours_per_run"]))
    return out
