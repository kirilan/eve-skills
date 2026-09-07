"""Watch-mode memory: queue observations, transitions, durable event history.

The watch loop fetches, sleeps, renders and notifies. This module holds the part
that must be testable without any of that: given one poll of per-character queue
observations, which transitions (training finished, queue emptied) did we just
witness? ``observe`` is pure; persistence wraps it with the storage primitives:

- ``watch-state.json`` — minimal non-secret observations per character, written
  atomically under a lock, so a restart or a second watcher never re-announces
  training that was already announced.
- ``events.jsonl`` — append-only completion history. Every event carries an id
  derived from the transition itself (character, skill, level, ESI finish date),
  so the same transition seen by two watchers — or by one watcher that crashed
  between appending and claiming state — is recorded exactly once.

Both files live in ``paths.state_dir()`` - ``$XDG_STATE_HOME/eve-skills`` (default
``~/.local/state/eve-skills``) on POSIX, ``%LOCALAPPDATA%\\eve-skills\\state`` on Windows: mutable
machine-local state — not config, not cache. Deleting them only costs re-announcement of whatever was in flight.
Readers resolve paths with ``create=False``: inspecting state never lays out
directories.

The persisted payload is character ids/names, skill ids/names/levels and ESI
timestamps.

Two documents, one discipline. Orders follow the same rules as training, because the traps have the
same shape:

- A first sight of an owner ingests ESI's ~90 days of order history as *backfilled* events: recorded
  once, never announced. That backlog is history, not something that just happened.
- One terminal event per order, ever. `settled` remembers the ids already recorded, so a re-read
  history page, a crash replay or a second watcher adds no row - and the event id, derived from the
  reason alone, refuses the duplicate at the append.
- ESI has no closed-at timestamp anywhere, so an order event's `ts` is the best time that can be
  *known*, and `data["ts_estimated"]` says when even that is only an upper bound.
- An order that left the live book but is missing from history waits out a grace period before its
  reason is declared unknown, and a cycle whose history fetch failed concludes nothing at all: a
  partial fetch must not look like a transition either.

A watch cycle is read -> diff -> append -> write, and it can be interrupted at any point: the diff is
pure, state is written only after events are claimed, and event ids are derived from the transition
itself, so a crash replay or a second watcher records the same event once.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Mapping, Sequence

from . import paths, render, storage

SCHEMA_VERSION = 1
STATE_RETENTION_DAYS = 30    # forget characters untouched this long (logged out / removed)
EVENT_RETENTION_DAYS = 365   # completions are rare; a year of history is cheap and useful
SETTLE_GRACE_DAYS = 2        # how long a vanished queue item waits for /skills to catch up
ORDER_SETTLE_GRACE_DAYS = 2  # how long an order that left the live book waits for its history row
ORDER_MEMORY_DAYS = 120      # slightly past ESI's ~90-day history: longer, and a re-read could
                             # legitimately surface an order we have already recorded

# Every kind this module can emit; `events --kind` validates against this list.
EVENT_KINDS = ("training_finished", "queue_empty",
               "order_filled", "order_expired", "order_cancelled", "order_closed")

OWNER_KINDS = ("char", "corp")             # the two halves of an owner key, e.g. "corp:98356123"
STILL_OPEN = "open"                        # a history row ESI still calls open is not a closure


# ---------------------------------------------------------------------------
# Paths (the platform state directory; never created by readers)
# ---------------------------------------------------------------------------


def state_file(create: bool = True) -> str:
    """watch-state.json (queue observations); readers pass create=False."""
    return os.path.join(paths.state_dir(create=create), "watch-state.json")


def events_file(create: bool = True) -> str:
    """events.jsonl (completion history); readers pass create=False."""
    return os.path.join(paths.state_dir(create=create), "events.jsonl")


def _commit_lock():
    """One lock over state + history: a commit must claim transitions and append
    their records as a unit, or a racing watcher cannot tell the two apart."""
    return storage.file_lock(os.path.join(paths.state_dir(), "watch-state.lock"))


# ---------------------------------------------------------------------------
# Observation / event model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueItem:
    """One queue entry as observed this poll. status is cli.queue_status()."""
    skill_id: int
    finished_level: int
    status: str                       # done | training | queued | blocked
    name: str = ""                    # display name; non-secret
    finish_date: str | None = None

    @property
    def key(self) -> str:
        return f"{self.skill_id}:{self.finished_level}"


@dataclass(frozen=True)
class CharacterObservation:
    """Everything one successful fetch says about a character's training."""
    character_id: int
    character_name: str
    items: tuple[QueueItem, ...] = ()
    trained_levels: Mapping[int, int] = field(default_factory=dict)  # skill_id -> trained (queue-adjusted)


@dataclass(frozen=True)
class WatchEvent:
    """A transition worth announcing and remembering, with a stable dedup id."""
    id: str
    ts: float
    kind: str                         # see EVENT_KINDS
    character_id: int | None          # None for a corporation-owned event
    character_name: str               # the owner: a character, or a corporation for order events
    skill_id: int | None = None
    skill_name: str | None = None
    finished_level: int | None = None
    finish_date: str | None = None
    data: Mapping[str, object] = field(default_factory=dict)   # order payload; {} for training rows

    def to_json(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "kind": self.kind,
            "character_id": self.character_id, "character_name": self.character_name,
            "skill_id": self.skill_id, "skill_name": self.skill_name,
            "finished_level": self.finished_level, "finish_date": self.finish_date,
            "data": dict(self.data),
        }


def _event_id(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _parse_key(key: str) -> tuple[int, int] | None:
    sid, sep, level = key.partition(":")
    if not sep:
        return None
    try:
        return int(sid), int(level)
    except ValueError:
        return None


def empty_state() -> dict:
    return {"version": SCHEMA_VERSION, "characters": {}, "owners": {}}


def _characters(state: dict) -> dict[int, dict]:
    """Prior per-character entries with invalid keys/records dropped, never raised on."""
    out = {}
    for key, entry in (state.get("characters") or {}).items():
        try:
            cid = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            out[cid] = entry
    return out


def observe(state: dict, observations: Sequence[CharacterObservation], now_ts: float) -> tuple[dict, list[WatchEvent]]:
    """(new_state, events) for one poll. Pure: no disk, no clock, no side effects.

    Rules the watch loop depends on:
    - A character seen for the first time never announces anything (its queue may
      already hold finished-but-unlogged items; that is history, not news).
    - Only actively *training* entries are tracked. One disappears from training —
      either still visible as ``done`` or gone with the trained level settled —
      and it has finished. An entry that vanishes before the skills document
      catches up stays tracked for SETTLE_GRACE_DAYS, so the completion is
      announced by a later poll rather than lost.
    - A character missing from the poll (fetch failed) keeps its prior entry: a
      transient outage must not look like a completion or an empty queue.
    """
    chars = _characters(state)
    events: list[WatchEvent] = []
    touched: set[int] = set()

    for obs in observations:
        cid = obs.character_id
        touched.add(cid)
        prior = chars.get(cid) or {}
        training = {it.key: it for it in obs.items if it.status == "training"}
        done_keys = {it.key for it in obs.items if it.status == "done"}

        carried: dict[str, dict] = {}   # tracked items whose level has not settled yet
        if prior:  # first observation of this character announces nothing
            for key, entry in (prior.get("known") or {}).items():
                parsed = _parse_key(key)
                if not isinstance(entry, dict) or parsed is None:
                    continue
                sid, level = parsed
                if key in training:
                    continue  # still going
                # CCP keeps a finished item visible as done until the character logs
                # in, but it may also vanish immediately - the trained level settles
                # it either way.
                settled = key in done_keys or int(obs.trained_levels.get(sid) or 0) >= level
                if settled:
                    events.append(WatchEvent(
                        id=_event_id("training_finished", cid, sid, level, entry.get("finish_date")),
                        ts=now_ts, kind="training_finished",
                        character_id=cid, character_name=obs.character_name,
                        skill_id=sid, skill_name=entry.get("skill_name") or f"skill {sid}",
                        finished_level=level, finish_date=entry.get("finish_date"),
                    ))
                elif now_ts - float(entry.get("seen") or prior.get("updated") or now_ts) <= SETTLE_GRACE_DAYS * 86400:
                    # Queue and skills are separate ESI documents with separate caches:
                    # the item can be gone while the level is still stale. Keep waiting.
                    carried[key] = {**entry, "seen": float(entry.get("seen") or prior.get("updated") or now_ts)}
            if int(prior.get("queue_len") or 0) > 0 and len(obs.items) == 0:
                events.append(WatchEvent(
                    id=_event_id("queue_empty", cid, prior.get("last_finish")),
                    ts=now_ts, kind="queue_empty",
                    character_id=cid, character_name=obs.character_name,
                ))

        finishes = [it.finish_date for it in obs.items if it.finish_date]
        chars[cid] = {
            "name": obs.character_name,
            "queue_len": len(obs.items),
            # the newest finish date of the current queue episode; distinguishes one
            # empty-the-queue transition from the next (a refill always adds a later date)
            "last_finish": max(finishes) if finishes else prior.get("last_finish"),
            "known": {**carried, **{k: {"skill_name": it.name, "finish_date": it.finish_date, "seen": now_ts}
                                    for k, it in training.items()}},
            "updated": now_ts,
        }

    kept = {cid: entry for cid, entry in chars.items()
            if cid in touched or now_ts - float(entry.get("updated") or 0) <= STATE_RETENTION_DAYS * 86400}
    # The starting state is carried through rather than rebuilt: a poll that only watched training
    # must not drop the `owners` half an order poll wrote.
    return {**state, "version": SCHEMA_VERSION,
            "characters": {str(cid): entry for cid, entry in kept.items()}}, events


# ---------------------------------------------------------------------------
# Orders: the same guarantees over a different document pair
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderObservation:
    """Everything one successful fetch says about one owner's orders.

    `open`/`history` are `orders.Order` rows, and `type_names` maps type ids to display names
    because an ESI order row carries ids only and this module resolves nothing over the network.
    `history_ok=False` means the live book was read but the history call failed: the two are separate
    documents with separate caches and separate failures.
    """
    owner_key: str
    owner_name: str
    open: tuple = ()
    history: tuple = ()
    history_ok: bool = True
    type_names: Mapping[int, str] = field(default_factory=dict)

    @classmethod
    def from_book(cls, book, type_names: Mapping[int, str] | None = None) -> "OrderObservation":
        """Adapt the `orders.OwnerOrders` a fetch returned."""
        return cls(book.owner_key, book.owner_name, tuple(book.open), tuple(book.history),
                   book.history_ok, dict(type_names or {}))


def _int(value) -> int | None:
    """Coerce something read back out of JSON without ever raising on junk."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _owners(state: dict) -> dict[str, dict]:
    """Prior per-owner entries with invalid keys/records dropped, never raised on."""
    out = {}
    for key, entry in (state.get("owners") or {}).items():
        kind, sep, ident = str(key).partition(":")
        number = _int(ident) if sep else None
        if isinstance(entry, dict) and kind in OWNER_KINDS and number is not None:
            out[f"{kind}:{number}"] = entry     # canonical, so "corp:098" matches "corp:98"
    return out


def _order_ids(mapping) -> dict[int, dict]:
    """The order-id keys of one owner section as ints; junk keys and records are dropped."""
    out = {}
    for key, entry in (mapping or {}).items():
        ident = _int(key)
        if ident is not None and isinstance(entry, dict):
            out[ident] = entry
    return out


def _settled_ids(mapping) -> dict[int, float]:
    """order_id -> when its terminal event was recorded.

    An unparseable stamp is dropped rather than raised on: losing one can only cost a duplicate that
    the event id then refuses at the append anyway.
    """
    out = {}
    for key, ts in (mapping or {}).items():
        ident, stamp = _int(key), _float(ts)
        if ident is not None and stamp is not None:
            out[ident] = stamp
    return out


def _expiry_epoch(issued, duration) -> float | None:
    """When an order was due to lapse (`issued` + `duration` days), or None if ESI gave no lifetime.

    History rows for orders that left the market report duration 0, and adding zero days would fake
    an instant lapse - so no lifetime means no bound on when it closed, not a suspicious timestamp.
    """
    days = _int(duration) or 0
    if days <= 0 or not isinstance(issued, str):
        return None
    try:
        start = render.parse_opt(issued)
    except ValueError:
        return None
    if start is None:
        return None
    if start.tzinfo is None:      # ESI stamps are UTC; a naive value means the same instant
        start = start.replace(tzinfo=timezone.utc)
    return (start + timedelta(days=days)).timestamp()


def _iso_z(epoch: float | None) -> str | None:
    """Epoch seconds in the `...Z` form ESI uses, so a payload reads like an API row."""
    return None if epoch is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _snapshot(row, type_name: str | None, now_ts: float) -> dict:
    """What we keep about an open order: enough to describe it in prose after ESI stops listing it."""
    return {
        "is_buy": bool(row.is_buy), "type_id": row.type_id, "type_name": type_name,
        "region_id": row.region_id, "location_id": row.location_id, "price": row.price,
        "volume_total": row.volume_total, "volume_remain": row.volume_remain,
        "issued": row.issued, "duration": row.duration,
        "wallet_division": row.wallet_division, "issued_by": row.issued_by,
        "seen": now_ts,
    }


def _character_of(owner_key: str) -> int | None:
    """The character id inside an owner key, or None for a corporation - which has its own id and
    must never be filed under whoever's token happened to read it."""
    kind, _, ident = str(owner_key).partition(":")
    return _int(ident) if kind == "char" else None


def _order_payload(order_id: int, owner_key: str, owner_name: str, snap: Mapping[str, object],
                   backfill: bool, estimated: bool) -> dict:
    """The `data` of one order event: what moved, where, and how much of it we claim to know."""
    total = _int(snap.get("volume_total")) or 0
    remain = _int(snap.get("volume_remain")) or 0
    return {
        "order_id": order_id, "owner_key": owner_key, "owner_name": owner_name,
        "type_id": snap.get("type_id"), "type_name": snap.get("type_name"),
        "is_buy": bool(snap.get("is_buy")), "price": snap.get("price"),
        "volume_total": total, "volume_remain": remain, "filled": max(total - remain, 0),
        "region_id": snap.get("region_id"), "location_id": snap.get("location_id"),
        "issued": snap.get("issued"),
        "expires": _iso_z(_expiry_epoch(snap.get("issued"), snap.get("duration"))),
        "wallet_division": snap.get("wallet_division"), "issued_by": snap.get("issued_by"),
        "backfill": backfill, "ts_estimated": estimated,
    }


# ESI's own closed-state enums; anything else it may invent in the future is `order_closed`, because
# an honest unknown beats a guessed reason.
_CLOSE_KINDS = {"filled": "order_filled", "expired": "order_expired", "cancelled": "order_cancelled"}


def _order_event(kind: str, reason: str, order_id: int, ts: float, owner_key: str, owner_name: str,
                 snap: Mapping[str, object], backfill: bool, estimated: bool) -> WatchEvent:
    """One terminal order transition.

    The id names the *reason*, never a timestamp: re-estimating when it happened, or watching the
    same owner with a second token, must land on the same id - otherwise the history would grow a
    second copy of one fact every time the clock disagreed.
    """
    return WatchEvent(
        id=_event_id(kind, order_id, reason), ts=ts, kind=kind,
        character_id=_character_of(owner_key), character_name=owner_name,
        data=_order_payload(order_id, owner_key, owner_name, snap, backfill, estimated),
    )


def _history_event(reason: str, order_id: int, owner_key: str, owner_name: str,
                   snap: Mapping[str, object], now_ts: float, witnessed: bool) -> WatchEvent:
    """The event for a row ESI's history says has closed, timed as well as ESI allows.

    An `expired` order lapsed at its expiry: that date is arithmetic on ESI's own issued/duration, not
    a guess, so it is used whether or not we watched it. A filled or cancelled row carries no closing
    time at all - if this watcher had the order in its own open book, detection time is honest; if it
    was already gone when we first looked, the only thing known is that it cannot have closed after
    its own expiry, hence `min(expiry, now)` plus `ts_estimated`.
    """
    expiry = _expiry_epoch(snap.get("issued"), snap.get("duration"))
    if reason == "expired" and expiry is not None:
        ts = min(expiry, now_ts)
    elif witnessed or expiry is None:
        ts = now_ts
    else:
        ts = min(expiry, now_ts)
    return _order_event(_CLOSE_KINDS.get(reason, "order_closed"), reason, order_id, ts,
                        owner_key, owner_name, snap, backfill=not witnessed,
                        estimated=not witnessed)


def _unknown_closure(order_id: int, owner_key: str, owner_name: str, snap: Mapping[str, object],
                     now_ts: float) -> WatchEvent:
    """An order that left the live book and never showed up in history.

    ESI cannot say why - cancelled, lapsed or swept, indistinguishable once the row is missing - so
    the kind says `closed` and the reason stays unknown rather than being guessed. Its time is an
    upper bound like a backfill's, but unlike a backfill this one *is* news: we watched the order
    disappear, which is exactly what the user asked to be told about.
    """
    expiry = _expiry_epoch(snap.get("issued"), snap.get("duration"))
    ts = now_ts if expiry is None else min(expiry, now_ts)
    return _order_event("order_closed", "unknown", order_id, ts, owner_key, owner_name, snap,
                        backfill=False, estimated=True)


def observe_orders(state: dict, observations: Sequence[OrderObservation],
                   now_ts: float) -> tuple[dict, list[WatchEvent]]:
    """(new_state, events) for one order poll. Pure: no disk, no clock, no side effects.

    Rules the watch loop depends on:
    - First sight of an owner ingests its whole ESI history as backfilled events and announces
      nothing; the live book is recorded so the next poll can witness a real transition.
    - A terminal event happens once per order, ever: `settled` remembers the ids already recorded, so
      a history row surfacing in a later poll - or a second watcher - adds no row and no news.
    - An order that left the live book without a history row waits ORDER_SETTLE_GRACE_DAYS for the
      history document to catch up, then is recorded as `order_closed`. If it reappears meanwhile it
      was an ESI cache flicker and nothing is announced.
    - `history_ok=False` freezes conclusions: the open book is still refreshed and a vanished order
      still starts its grace period (that fetch succeeded, so the disappearance is real evidence),
      but nothing is declared closed on a cycle whose history call failed.
    - An owner missing from the poll keeps its prior entry: a failed fetch is not a transition.
    """
    owners = _owners(state)
    events: list[WatchEvent] = []
    touched: set[str] = set()

    for obs in observations:
        key = str(obs.owner_key)
        touched.add(key)
        prior = owners.get(key) or {}
        names = obs.type_names or {}
        settled = _settled_ids(prior.get("settled"))
        known_open = _order_ids(prior.get("open"))
        pending = _order_ids(prior.get("pending"))

        open_now: dict[int, dict] = {}
        for row in obs.open:
            ident = _int(row.order_id)
            if ident is not None:
                open_now[ident] = _snapshot(row, names.get(_int(row.type_id)), now_ts)

        # Rows ESI says have closed. "Witnessed" is what makes detection time honest: this watcher
        # had the order in its own book, so it really did see it open until now. The first poll of an
        # owner takes exactly this path - with no prior book nothing can be witnessed, so its whole
        # backlog is recorded as backfill, which the watch loop then keeps out of the announcements.
        for row in obs.history:
            ident, reason = _int(row.order_id), str(row.state or "")
            if ident is None or reason == STILL_OPEN or ident in settled:
                continue
            witnessed = ident in known_open or ident in pending
            event = _history_event(reason, ident, key, obs.owner_name,
                                   _snapshot(row, names.get(_int(row.type_id)), now_ts),
                                   now_ts, witnessed)
            events.append(event)
            settled[ident] = event.ts
            pending.pop(ident, None)
        for ident in open_now:
            pending.pop(ident, None)          # back on the book: a flicker, not a closure
        for ident, snap in known_open.items():
            if ident in open_now or ident in settled:
                continue                      # still listed, or recorded as closed just now
            prev = pending.get(ident)
            since = _float(prev.get("since")) if prev else None
            pending[ident] = {**snap, "since": since if since is not None else now_ts}
        for ident, entry in list(pending.items()):
            since = _float(entry.get("since")) or now_ts
            if obs.history_ok and now_ts - since > ORDER_SETTLE_GRACE_DAYS * 86400:
                # The open book says it is gone and the history document has had its grace period to
                # say why. Waiting longer cannot help: ESI keeps no row for an order it has dropped.
                event = _unknown_closure(ident, key, obs.owner_name, entry, now_ts)
                events.append(event)
                settled[ident] = event.ts
                pending.pop(ident)

        owners[key] = {
            "name": obs.owner_name,
            "open": {str(ident): snap for ident, snap in open_now.items()},
            "pending": {str(ident): entry for ident, entry in pending.items()},
            # ESI's history only reaches back ~90 days, so past ORDER_MEMORY_DAYS an order cannot
            # come back and be re-announced; keeping the id forever would grow state without end.
            "settled": {str(ident): ts for ident, ts in settled.items()
                        if now_ts - ts <= ORDER_MEMORY_DAYS * 86400},
            "updated": now_ts,
        }

    kept = {key: entry for key, entry in owners.items()
            if key in touched or now_ts - float(entry.get("updated") or 0) <= STATE_RETENTION_DAYS * 86400}
    # ...and the reverse of `observe`'s promise: order polling leaves `characters` alone.
    return {**state, "version": SCHEMA_VERSION,
            "owners": {key: entry for key, entry in kept.items()}}, events


# ---------------------------------------------------------------------------
# Persistence: claim + append under one lock, exactly once
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """The last committed observations; absent or unreadable files are an empty state."""
    try:
        with open(state_file(create=False)) as fh:
            doc = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(doc, dict) or not isinstance(doc.get("characters"), dict):
        return empty_state()
    return doc


def _parse_row(line: str) -> dict | None:
    try:
        row = json.loads(line)
        return {
            "id": str(row["id"]), "ts": float(row["ts"]), "kind": str(row["kind"]),
            # None for a corporation-owned event; rows written before orders existed have no
            # `data` at all, and must keep parsing with an empty payload.
            "character_id": None if row.get("character_id") is None else int(row["character_id"]),
            "character_name": row.get("character_name"),
            "skill_id": None if row.get("skill_id") is None else int(row["skill_id"]),
            "skill_name": row.get("skill_name"),
            "finished_level": None if row.get("finished_level") is None else int(row["finished_level"]),
            "finish_date": row.get("finish_date"),
            "data": row["data"] if isinstance(row.get("data"), dict) else {},
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _append_events(events: Sequence[WatchEvent], now_ts: float) -> list[WatchEvent]:
    """Merge new events into the history under the commit lock; return the ones newly written.

    Existing ids are collected first so a crash-replayed or racy duplicate is never
    written twice; the whole file lands via one atomic replace, pruned to retention.
    The returned list is what makes ``commit``'s promise true: it holds exactly the rows
    this call appended, in order. An id already on disk was claimed by an earlier cycle,
    and one repeated inside a single poll is the same transition twice - announcing
    either would ring the bell twice for one event.
    """
    path = events_file()
    cutoff = now_ts - EVENT_RETENTION_DAYS * 86400
    seen_ids: set[str] = set()
    kept: list[str] = []
    try:
        with open(path) as fh:
            for line in fh:
                row = _parse_row(line)
                if row is None or row["ts"] < cutoff or row["id"] in seen_ids:
                    continue  # corrupt, expired, or a defensive duplicate from an uncooperative writer
                seen_ids.add(row["id"])
                kept.append(json.dumps(row) + "\n")
    except FileNotFoundError:
        pass
    claimed: list[WatchEvent] = []
    for event in events:
        row = event.to_json()
        if row["id"] in seen_ids:
            continue  # already recorded (crash replay between append and state write, or another watcher won)
        seen_ids.add(row["id"])
        claimed.append(event)
        kept.append(json.dumps(row) + "\n")
    storage.atomic_write(path, "".join(kept))
    return claimed


def commit(observations: Sequence[CharacterObservation],
           order_observations: Sequence[OrderObservation] = (),
           now_ts: float | None = None) -> list[WatchEvent]:
    """Persist one poll and return only the transitions this caller newly claimed.

    The whole read-compute-append-write runs under ``watch-state.lock``: a second
    watcher sees already-claimed entries and returns nothing, and a process that
    dies mid-commit replays into the same event ids, which the append skips - and
    which this function then withholds from its caller, so the replay is silent
    rather than a second announcement of one transition.

    Both document pairs share that one lock, one state file and one event stream, so a cycle that
    saw training finish *and* an order fill cannot claim one half and lose the other to a crash in
    between. Each half preserves what the other wrote.
    """
    now_ts = time.time() if now_ts is None else float(now_ts)
    with _commit_lock():
        new_state, events = observe(load_state(), observations, now_ts)
        new_state, order_events = observe_orders(new_state, order_observations, now_ts)
        events = events + order_events
        if events:
            events = _append_events(events, now_ts)
        storage.atomic_write_json(state_file(), new_state)
    return events


def load_events(character_id: int | None = None) -> tuple[list[dict], int]:
    """(events oldest-first, unreadable line count). Ids are deduplicated defensively."""
    rows: list[dict] = []
    seen_ids: set[str] = set()
    skipped = 0
    try:
        with open(events_file(create=False)) as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = _parse_row(line)
                if row is None:
                    skipped += 1
                    continue
                if row["id"] in seen_ids:
                    continue
                seen_ids.add(row["id"])
                rows.append(row)
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: (r["ts"], r["id"]))
    if character_id is not None:
        rows = [r for r in rows if r["character_id"] == character_id]
    return rows, skipped
