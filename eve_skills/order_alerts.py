"""Which order changes are worth a message, decided from one check to the next.

`evaluate` is pure: it takes the previous state, this run's checked open orders (the rows
`orders --check` produces), the closed orders ESI's history now shows, and the settings, and returns
the new state and the alert. `cmd_alerts` does the ESI reads and the sending; the state is saved
only after Telegram accepted the message, so a failed send is retried by the next run.

The rules:
- *behind*: an order that is `undercut` or `outbid` is announced once per price of ours. Repricing and
  falling behind again is a new alert; staying behind at the same price is not.
- *still behind*: a reminder listing every order still behind at its announced price, at most once per
  `remind_hours`, and never during quiet hours (it waits for the morning).
- *back on top*: an announced order that is `cheapest`, `top` or `alone` again at the same price, i.e.
  the competition left; when we repriced it ourselves, that is not news.
- *filled*: an order that left the open book and shows in the history as filled. ESI caches the history
  for up to an hour, so a vanished order waits in `pending` until its history row arrives.
- *job ending*: an industry job with at most `job_notice_minutes` left (or already finished) is announced
  once. Until it is delivered it is listed in the same reminder as the orders still behind, so one clock
  paces both. Delivery is the job leaving its installer's list; a paused job is not news.
- quiet hours send silently (no sound); excluded locations and characters never alert (locations apply
  to orders only, characters to both).
Orders of an owner whose book could not be read this run are carried over unchanged, never treated
as closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone

from . import job_times, paths, storage

BEHIND = ("undercut", "outbid")
ON_TOP = ("cheapest", "top", "alone")
SETTINGS_NAME = "alerts.json"
STATE_NAME = "order-alerts.json"
# A vanished order whose history row has not appeared by then is dropped without a message.
PENDING_SECONDS = 6 * 3600

DEFAULT_SETTINGS = {
    "exclude_locations": [],       # case-insensitive substrings of the station/structure name
    "exclude_characters": [],      # character names
    "remind_hours": 4,
    "quiet_hours": "00:00-08:00",  # local time below; empty string = never quiet
    "timezone": "Europe/Sofia",
    "job_alerts": True,            # industry jobs ending soon, then in the reminder until delivered
    "job_notice_minutes": 20,
}


@dataclass(frozen=True)
class Settings:
    exclude_locations: tuple[str, ...] = ()
    exclude_characters: tuple[str, ...] = ()
    remind_hours: float = 4
    quiet_start: dtime | None = None
    quiet_end: dtime | None = None
    timezone: str = "Europe/Sofia"
    job_alerts: bool = True
    job_notice_minutes: float = 20

    def excluded_character(self, name) -> bool:
        return str(name or "").casefold() in {n.casefold() for n in self.exclude_characters}

    def excluded(self, row: dict) -> bool:
        where = str(row.get("location") or "").casefold()
        who = str(row.get("owner") or "").casefold()
        return (any(part.casefold() in where for part in self.exclude_locations if part)
                or who in {name.casefold() for name in self.exclude_characters})

    def quiet(self, now: datetime) -> bool:
        if self.quiet_start is None or self.quiet_end is None:
            return False
        local = now.astimezone(job_times.zone(self.timezone)).time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= local < self.quiet_end
        return local >= self.quiet_start or local < self.quiet_end     # wraps midnight


def _clock(text: str) -> dtime:
    hours, _, minutes = text.strip().partition(":")
    return dtime(int(hours), int(minutes or 0))


def parse_settings(doc: dict) -> Settings:
    """Settings from the `alerts.json` document; a malformed value is an error that names it."""
    merged = {**DEFAULT_SETTINGS, **(doc or {})}
    quiet = str(merged.get("quiet_hours") or "").strip()
    start = end = None
    if quiet:
        try:
            first, second = quiet.split("-")
            start, end = _clock(first), _clock(second)
        except ValueError:
            raise RuntimeError(f"alerts.json: quiet_hours {quiet!r} is not HH:MM-HH:MM") from None
    try:
        remind = float(merged.get("remind_hours") or 0)
    except (TypeError, ValueError):
        raise RuntimeError("alerts.json: remind_hours must be a number (0 = no reminders)") from None
    try:
        notice = float(merged.get("job_notice_minutes") or 0)
    except (TypeError, ValueError):
        raise RuntimeError("alerts.json: job_notice_minutes must be a number") from None
    zone_name = str(merged.get("timezone") or "UTC")
    try:
        job_times.zone(zone_name)       # an IANA name, or a fixed offset such as +02:00
    except RuntimeError as err:
        raise RuntimeError(f"alerts.json: timezone: {err}") from None
    return Settings(tuple(str(x) for x in merged.get("exclude_locations") or []),
                    tuple(str(x) for x in merged.get("exclude_characters") or []),
                    remind, start, end, zone_name,
                    bool(merged.get("job_alerts")), notice)


def settings_path() -> str:
    return os.path.join(paths.config_dir(), SETTINGS_NAME)


def load_settings() -> Settings:
    """The settings file, created with the defaults on first use so there is something to edit."""
    path = settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        doc = dict(DEFAULT_SETTINGS)
        storage.atomic_write_json(path, doc)
    except ValueError as err:
        raise RuntimeError(f"{path}: not valid JSON ({err})") from None
    return parse_settings(doc if isinstance(doc, dict) else {})


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


def empty_state() -> dict:
    return {"orders": {}, "pending": {}, "jobs": {}, "last_reminder": None}


@dataclass
class Alert:
    behind: list[dict] = field(default_factory=list)
    still_behind: list[dict] = field(default_factory=list)
    back_on_top: list[dict] = field(default_factory=list)
    filled: list[dict] = field(default_factory=list)
    jobs_soon: list[dict] = field(default_factory=list)       # newly within the notice, or finished
    jobs_waiting: list[dict] = field(default_factory=list)    # announced earlier, not delivered yet
    silent: bool = False

    def empty(self) -> bool:
        return not (self.behind or self.still_behind or self.back_on_top or self.filled
                    or self.jobs_soon or self.jobs_waiting)


SNAPSHOT_KEYS = ("owner", "owner_key", "type_name", "side", "location", "price", "volume_total",
                 "volume_remain", "status", "best_other", "units_ahead", "days_ahead", "suggest")


def _snapshot(row: dict) -> dict:
    return {key: row.get(key) for key in SNAPSHOT_KEYS}


JOB_KEYS = ("owner_key", "installer", "activity", "product_id", "runs", "end")


def _end_ts(job: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(job.get("end")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _evaluate_jobs(previous: dict[int, dict], jobs: list[dict], read: set[str], settings: Settings,
                   ts: float, alert: Alert) -> dict[int, dict]:
    """The announced jobs still undelivered; fills `alert.jobs_soon` and `alert.jobs_waiting`.

    Only announced jobs are remembered. One missing from its installer's list, when that list was
    read, has been delivered (or cancelled): it is dropped without a message."""
    kept: dict[int, dict] = {}
    seen = set()
    for job in jobs:
        job_id = int(job["job_id"])
        seen.add(job_id)
        if settings.excluded_character(job.get("installer")):
            continue
        before = previous.get(job_id)
        if job.get("status") == "paused":
            if before is not None:
                kept[job_id] = before
            continue
        end = _end_ts(job)
        entry = {key: job.get(key) for key in JOB_KEYS}
        if before is not None:
            kept[job_id] = {**entry, "announced_at": before.get("announced_at")}
            alert.jobs_waiting.append(job)
        elif end is not None and end - ts <= settings.job_notice_minutes * 60:
            kept[job_id] = {**entry, "announced_at": ts}
            alert.jobs_soon.append(job)
    for job_id, before in previous.items():
        if job_id not in seen and before.get("owner_key") not in read:
            kept[job_id] = before           # unread this run: not evidence of a delivery
    return kept


def evaluate(state: dict, rows: list[dict], closed: dict[int, str], read_owners: set[str],
             settings: Settings, now: datetime, jobs: list[dict] | None = None,
             job_owners: set[str] | None = None) -> tuple[dict, Alert]:
    """(new state, alert) for one run. `rows` are this run's open orders with their check, `closed`
    maps an order id to its history state (`filled`, `expired`, `cancelled`) and `read_owners` are the
    owner keys whose books were read: everybody else's orders are carried over untouched.

    `jobs` are this run's undelivered industry jobs (`job_id`, `owner_key`, `installer`, `activity`,
    `product_id`, `runs`, `end`, `status`) and `job_owners` the installers whose lists were read.
    `None` (job alerts off, or not read) carries the remembered jobs over untouched."""
    ts = now.timestamp()
    previous = {int(k): v for k, v in (state.get("orders") or {}).items()}
    pending = {int(k): v for k, v in (state.get("pending") or {}).items()}
    alert = Alert(silent=settings.quiet(now))
    orders: dict[int, dict] = {}
    seen = set()
    for row in rows:
        order_id = int(row["order_id"])
        seen.add(order_id)
        if settings.excluded(row):
            continue
        before = previous.get(order_id) or {}
        entry = _snapshot(row)
        announced = before.get("alerted_price")
        if row.get("status") in BEHIND:
            if announced is None or announced != row.get("price"):
                alert.behind.append(row)
                entry.update(alerted_price=row.get("price"), alerted_at=ts)
            else:
                alert.still_behind.append(row)
                entry.update(alerted_price=announced, alerted_at=before.get("alerted_at"))
        elif announced is not None:
            if row.get("status") in ON_TOP:
                if announced == row.get("price"):
                    alert.back_on_top.append(row)     # we did not move: the competition did
            else:
                # `behind sliver` or unchecked: not news either way; keep the memory of the alert
                entry.update(alerted_price=announced, alerted_at=before.get("alerted_at"))
        orders[order_id] = entry

    for order_id, before in previous.items():
        if order_id in seen:
            continue
        if before.get("owner_key") not in read_owners:
            orders[order_id] = before       # unread this run: not evidence of anything
        elif order_id not in pending:
            pending[order_id] = {**before, "since": ts}

    for order_id, entry in list(pending.items()):
        state_closed = closed.get(order_id)
        if order_id in seen:
            del pending[order_id]           # back in the open book after all
        elif state_closed is not None:
            if state_closed == "filled":
                alert.filled.append({**entry, "order_id": order_id})
            del pending[order_id]
        elif ts - float(entry.get("since") or ts) > PENDING_SECONDS:
            del pending[order_id]

    previous_jobs = {int(k): v for k, v in (state.get("jobs") or {}).items()}
    if jobs is None:
        kept_jobs = previous_jobs
    else:
        kept_jobs = _evaluate_jobs(previous_jobs, jobs, set(job_owners or ()), settings, ts, alert)

    # Reminders run on one clock: from the last reminder, or else from the oldest announcement still
    # standing. Orders still behind and jobs not yet delivered share it, so they arrive as one roll-up.
    # Nothing outstanding any more resets it, so the next undercut or job starts a fresh interval.
    last = state.get("last_reminder")
    if alert.still_behind or alert.jobs_waiting:
        anchor = last if last is not None else min(
            [float(orders[int(r["order_id"])].get("alerted_at") or ts) for r in alert.still_behind]
            + [float(kept_jobs[int(j["job_id"])].get("announced_at") or ts) for j in alert.jobs_waiting])
        due = settings.remind_hours > 0 and ts - float(anchor) >= settings.remind_hours * 3600
        if due and not alert.silent:
            last = ts
        else:
            alert.still_behind = []         # wait for the next slot, or for the end of quiet hours
            alert.jobs_waiting = []
    elif not (alert.behind or alert.jobs_soon):
        last = None
    new_state = {"orders": {str(k): v for k, v in orders.items()},
                 "pending": {str(k): v for k, v in pending.items()},
                 "jobs": {str(k): v for k, v in kept_jobs.items()},
                 "last_reminder": last}
    return new_state, alert


def _num(value) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _where(row: dict) -> str:
    """The system part of a station name - `Jita`, `4-HWWF` - which is how one says where."""
    name = str(row.get("location") or "")
    return name.split(" ")[0] if name else "?"


def _left(job: dict, ts: float) -> float | None:
    end = _end_ts(job)
    return None if end is None else end - ts


def _job_groups(jobs: list[dict], ts: float) -> list[dict]:
    """Jobs of one installer, activity, product and facility as one line: ten inventions of the same
    blueprint finishing together are one thing to collect."""
    groups: dict[tuple, dict] = {}
    for job in jobs:
        key = (job.get("installer"), job.get("activity"), job.get("product"), job.get("location"))
        group = groups.setdefault(key, {"installer": key[0], "activity": key[1], "product": key[2],
                                        "location": key[3], "count": 0, "runs": 0, "left": []})
        group["count"] += 1
        group["runs"] += int(job.get("runs") or 0)
        left = _left(job, ts)
        if left is not None:
            group["left"].append(left)
    return sorted(groups.values(), key=lambda g: (min(g["left"], default=0), str(g["installer"])))


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _group_text(group: dict) -> tuple[str, str]:
    """(what, when) for a job group: `invention · 10 jobs` and `ready` / `12 min left`."""
    count, runs = group["count"], group["runs"]
    what = f"{group['activity']} · {count} job{'s' if count != 1 else ''}"
    if runs and runs != count:
        what += f", {runs:,} runs"
    lefts = [x for x in group["left"] if x > 0]
    if not lefts:
        when = "ready"
    elif len(lefts) < len(group["left"]):
        when = f"some ready, rest in {_minutes(max(lefts))}"
    elif _minutes(min(lefts)) == _minutes(max(lefts)):
        when = f"{_minutes(max(lefts))} left"
    else:
        when = f"{_minutes(min(lefts))}–{_minutes(max(lefts))} left"
    return what, when


def render(alert: Alert, settings: Settings, now: datetime | None = None) -> str:
    """The message: one block per kind, each line the full item name (pasteable into EVE search)."""
    ts = (now or datetime.now(timezone.utc)).timestamp()
    parts = []
    head = []
    if alert.behind:
        head.append(f"{len(alert.behind)} behind")
    if alert.back_on_top:
        head.append(f"{len(alert.back_on_top)} back on top")
    if alert.filled:
        head.append(f"{len(alert.filled)} filled")
    if alert.jobs_soon:
        head.append(f"{len(alert.jobs_soon)} job{'s' if len(alert.jobs_soon) != 1 else ''} ending")
    if not head:
        if alert.still_behind:
            head.append(f"{len(alert.still_behind)} still behind")
        if alert.jobs_waiting:
            head.append(f"{len(alert.jobs_waiting)} jobs to deliver")
    order_news = alert.behind or alert.back_on_top or alert.filled or alert.still_behind
    job_news = alert.jobs_soon or alert.jobs_waiting
    title = "EVE orders" if order_news and not job_news else "EVE jobs" if job_news and not order_news else "EVE"
    parts.append(f"{title}: " + ", ".join(head))

    def behind_line(row: dict) -> str:
        verb = "Outbid" if row.get("status") == "outbid" else "Undercut"
        ahead = row.get("units_ahead") or 0
        days = row.get("days_ahead")
        ahead_text = f"{ahead:,} ahead" + (f" ({days:.1f} d)" if days is not None else "")
        return (f"{'▲' if verb == 'Outbid' else '▼'} {verb}: {row.get('type_name')}\n"
                f"   {row.get('owner')} · {row.get('side')} · {_where(row)}\n"
                f"   ours {_num(row.get('price'))} · best {_num(row.get('best_other'))} · {ahead_text}"
                + (f" · set {_num(row.get('suggest'))}" if row.get("suggest") is not None else ""))

    for row in alert.behind:
        parts.append(behind_line(row))
    for row in alert.back_on_top:
        parts.append(f"✓ Back on top: {row.get('type_name')}\n"
                     f"   {row.get('owner')} · {row.get('side')} · {_where(row)} at {_num(row.get('price'))}")
    for row in alert.filled:
        verb = "bought" if row.get("side") == "buy" else "sold"
        parts.append(f"● Filled: {row.get('type_name')}\n"
                     f"   {row.get('owner')} {verb} {row.get('volume_total') or 0:,} at "
                     f"{_num(row.get('price'))} · {_where(row)}")
    for group in _job_groups(alert.jobs_soon, ts):
        what, when = _group_text(group)
        parts.append(f"⚙ Job {'done' if when == 'ready' else 'ending'}: {group['product']}\n"
                     f"   {group['installer']} · {what} · {_where(group)} · {when}")
    if alert.jobs_waiting:
        lines = [f"Jobs to deliver (reminder every {settings.remind_hours:g} h):"]
        for group in _job_groups(alert.jobs_waiting, ts):
            what, when = _group_text(group)
            lines.append(f"   {group['product']} · {group['installer']} · {what} · {_where(group)} · {when}")
        parts.append("\n".join(lines))
    if alert.still_behind:
        lines = [f"Still behind (reminder every {settings.remind_hours:g} h):"]
        for row in alert.still_behind:
            lines.append(f"   {row.get('type_name')} · {row.get('owner')} · {_where(row)} · ours "
                         f"{_num(row.get('price'))}, best {_num(row.get('best_other'))}"
                         + (f", set {_num(row.get('suggest'))}" if row.get("suggest") is not None else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
