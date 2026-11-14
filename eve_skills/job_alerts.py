"""Job news on top of the per-job alerts: a summary when a finish window opens, and batching.

The per-job alerts (`order_alerts`, `job_alerts` / `job_notice_minutes`) announce each job once when it
has at most the notice left, then keep it in the reminder until it is delivered. Jobs finish one by one
over an hour or two, so on their own they make one message per run while a round lands. This module
decides, per run, two things on top (`evaluate` is pure; `cmd_alerts` reads and sends):

- *window summary*: on the first run inside each finish window (settings `jobs.windows`, else the
  operation file's `planning.finish_windows`; none: no summaries), if any job is ready or any slot is
  free - ready jobs per character, free slots per character, and the jobs still ending inside the
  window. It covers every job news of that run: the jobs it lists count as announced.
- *batch*: a job the per-job alerts would announce now is held back while another job of the same site
  ends within `jobs.batch_minutes` (default 45), unless the oldest held job of that site has waited
  `jobs.max_wait_minutes` (default 90). Then everything of the site goes out in one message.
- *quiet hours*: no job news at all (not even silent); the next window summary covers the night.

`exclude_characters` applies; `jobs.enabled: false` turns this module off (the per-job alerts stay as
they are). State (`job-alerts.json`, state directory): the held job ids with the time they were first
held, and the last window summarised. It is saved only after Telegram accepted the run's message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import job_times, order_alerts, paths, storage

STATE_NAME = "job-alerts.json"


def empty_state() -> dict:
    return {"held": {}, "last_window": None}


def state_path() -> str:
    return os.path.join(paths.state_dir(), STATE_NAME)


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return empty_state()
    return doc if isinstance(doc, dict) else empty_state()


def save_state(state: dict) -> None:
    storage.atomic_write_json(state_path(), state)


@dataclass
class JobNews:
    """What the job part of one run adds: a summary to send (or None) and the job ids to hold back."""
    summary: dict | None = None
    hold: set[int] = field(default_factory=set)


def _end(job: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(job.get("end")).replace("Z", "+00:00"))
    except ValueError:
        return None


def window_spec(settings, planning_windows: str | None) -> str | None:
    """The finish windows in force: the alert settings', else the operation file's, else none."""
    if settings.jobs_windows is not None:
        return settings.jobs_windows or None
    return planning_windows or None


def current_window(now: datetime, spec: str | None, zone_name: str) -> tuple[str, datetime] | None:
    """(key, end) of the finish window `now` falls in, in the settings' zone; None outside every window.

    The key names the day and the window (`2030-01-02 08:00-10:00`), so each window is summarised once."""
    if not spec:
        return None
    tz = job_times.zone(zone_name)
    local = now.astimezone(tz)
    minute = local.hour * 60 + local.minute
    for lo, hi in job_times.parse_windows(spec):
        if lo <= minute < hi:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            key = f"{local:%Y-%m-%d} {lo // 60:02d}:{lo % 60:02d}-{hi // 60:02d}:{hi % 60:02d}"
            return key, (midnight + timedelta(minutes=hi)).astimezone(now.tzinfo)
    return None


def summary_due(state: dict, settings, planning_windows: str | None, now: datetime) -> bool:
    """Whether this run opens a window not summarised yet (the caller then reads the slots)."""
    if not (settings.job_alerts and settings.jobs_enabled) or settings.quiet(now):
        return False
    window = current_window(now, window_spec(settings, planning_windows), settings.timezone)
    return window is not None and window[0] != state.get("last_window")


def evaluate(state: dict, jobs: list[dict], slots: list[dict] | None, settings, now: datetime, *,
             announced: set[int], planning_windows: str | None = None,
             sites: dict[str, str] | None = None) -> tuple[dict, JobNews]:
    """(new state, news) for one run.

    `jobs` are this run's undelivered jobs (`job_id`, `installer`, `activity`, `end`, `status`, ...),
    `slots` the characters' slot rows (`character_name`, `slots: {manufacturing|science: {free, ...}}`)
    or None when they were not read, `announced` the job ids the per-job alerts announced already, and
    `sites` maps a character name (case-folded) to its site; characters outside every site form one group."""
    if not (settings.job_alerts and settings.jobs_enabled):
        return state, JobNews()
    ts = now.timestamp()
    held_before = {int(k): float(v) for k, v in (state.get("held") or {}).items()}
    sites = sites or {}
    live = [j for j in jobs if j.get("status") != "paused" and not settings.excluded_character(j.get("installer"))]
    notice = settings.job_notice_minutes * 60

    def due(job: dict) -> bool:
        end = _end(job)
        return end is not None and end.timestamp() - ts <= notice

    candidates = [j for j in live if int(j["job_id"]) not in announced and due(j)]
    new_state = {"held": {}, "last_window": state.get("last_window")}

    if settings.quiet(now):
        hold = {int(j["job_id"]) for j in candidates}
        new_state["held"] = {str(i): held_before.get(i, ts) for i in hold}
        return new_state, JobNews(hold=hold)

    window = current_window(now, window_spec(settings, planning_windows), settings.timezone)
    if window is not None and window[0] != state.get("last_window"):
        new_state["last_window"] = window[0]
        key, window_end = window
        ready = [j for j in live if j.get("status") == "ready" or ((_end(j) or now) <= now)]
        ready_ids = {int(j["job_id"]) for j in ready}
        ending = sorted((j for j in live if int(j["job_id"]) not in ready_ids and (
                         (_end(j) is not None and _end(j) <= window_end) or j in candidates)),
                        key=lambda j: _end(j) or now)
        free = []
        for row in slots or []:
            name = row.get("character_name")
            if settings.excluded_character(name):
                continue
            cells = {kind: (row.get("slots") or {}).get(kind) or {} for kind in ("manufacturing", "science")}
            counts = {kind: cell.get("free") for kind, cell in cells.items() if cell.get("free")}
            if counts:
                free.append({"character": name, **counts})
        if ready or free:
            return new_state, JobNews(summary={"window": key.split(" ", 1)[1], "ready": ready, "free": free,
                                               "ending": ending, "slots_read": slots is not None})
        # nothing to say at the window's start: fall through to the batch rule

    groups: dict[str, list[dict]] = {}
    for job in candidates:
        groups.setdefault(sites.get(str(job.get("installer") or "").casefold(), ""), []).append(job)
    hold: set[int] = set()
    horizon = ts + settings.batch_minutes * 60
    for site, members in groups.items():
        ids = {int(j["job_id"]) for j in members}
        oldest = min(held_before.get(i, ts) for i in ids)
        more = any(int(j["job_id"]) not in ids and int(j["job_id"]) not in announced
                   and sites.get(str(j.get("installer") or "").casefold(), "") == site
                   and (_end(j) is not None and ts < _end(j).timestamp() <= horizon)
                   for j in live)
        if more and ts - oldest < settings.max_wait_minutes * 60:
            hold |= ids
    new_state["held"] = {str(i): held_before.get(i, ts) for i in hold}
    return new_state, JobNews(hold=hold)


def render_summary(summary: dict, settings, now: datetime) -> str:
    """The window message: ready jobs per character and facility, free slots, jobs still ending in the
    window. Job lines are `order_alerts.job_lines`: counts per activity, no products."""
    tz = job_times.zone(settings.timezone)
    free_total = sum(row.get("manufacturing", 0) + row.get("science", 0) for row in summary["free"])
    head = [f"{len(summary['ready'])} ready"] + ([f"{free_total} free slot{'' if free_total == 1 else 's'}"]
                                                 if free_total else [])
    parts = [f"EVE jobs: {summary['window']} window, " + ", ".join(head)]
    if summary["ready"]:
        parts.append("\n".join(["Ready to deliver:"] + order_alerts.job_lines(summary["ready"])))
    if summary["free"]:
        lines = ["Free slots:"]
        for row in summary["free"]:
            cells = [f"{row[kind]} {kind}" for kind in ("manufacturing", "science") if row.get(kind)]
            lines.append(f"   {row['character']}: {', '.join(cells)}")
        parts.append("\n".join(lines))
    elif not summary.get("slots_read", True):
        parts.append("Free slots: not read this run")
    if summary["ending"]:
        def clock(jobs: list[dict]) -> str:
            known = sorted(end.astimezone(tz).strftime("%H:%M") for end in map(_end, jobs) if end)
            return "?" if not known else known[0] if known[0] == known[-1] else f"{known[0]}-{known[-1]}"
        parts.append("\n".join(["Ending in the window:"] + order_alerts.job_lines(summary["ending"], clock)))
    return "\n\n".join(parts)
