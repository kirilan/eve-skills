"""eve-skills command line interface."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from dataclasses import dataclass, field


from . import __version__, alphadata, classify, doctor as doctor_mod, esi as esi_mod, exports, industry, market, orders, paths, planner, render, snapshots, sso, watchstate


def gather(args, client: esi_mod.Esi | None = None) -> dict:
    char_id = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    token = sso.get_access_token(char_id)
    cfg = sso.load_config()
    # One client per command/watch session: the response cache and the ESI error-limit
    # backoff only work when state survives across characters and polls.
    client = client or esi_mod.Esi(esi_mod.default_user_agent(cfg))
    char_id = token["character_id"]

    public = client.get(f"/characters/{char_id}")
    skills_doc = client.get(f"/characters/{char_id}/skills", token=token["access_token"])
    queue = client.get(f"/characters/{char_id}/skillqueue", token=token["access_token"])
    queue.sort(key=lambda q: (q.get("queue_position", 0), q.get("start_date") or "9999"))

    now = client.now()  # ESI server time; immune to a skewed system clock
    # The skills endpoint can lag behind finished training until the character logs in;
    # apply completed queue entries on top (per ESI docs).
    trained_by_id = {int(s["skill_id"]): int(s["trained_skill_level"]) for s in skills_doc.get("skills", [])}
    completed_levels: dict[int, int] = {}
    for item in queue:
        if queue_status(item, now) == "done":
            sid = int(item["skill_id"])
            level = int(item.get("finished_level") or 0)
            if level > trained_by_id.get(sid, 0):
                completed_levels[sid] = max(completed_levels.get(sid, 0), level)

    data = alphadata.load()
    caps, grade_name = classify.alpha_caps(data, public.get("bloodline_id"))
    names = esi_mod.resolve_names(client, {int(s["skill_id"]) for s in skills_doc.get("skills", [])} | {int(q["skill_id"]) for q in queue})
    known_ids = set(names)

    extra_warnings = []
    age = alphadata.data_age_days(data)
    if age is not None and age > alphadata.STALE_DAYS:
        extra_warnings.append(
            f"local alpha caps data is {age:.0f} days old (SDE build {data['grades']['build']}) - run: eve-skills update-data"
        )

    rows = classify.classify_skills(skills_doc.get("skills", []), caps, names, completed_levels, known_ids=known_ids)
    state = classify.clone_state(
        rows, [q for q in queue if queue_status(q, now) != "done"], caps, names,
        now=now, known_ids=known_ids, extra_warnings=extra_warnings,
    )

    try:
        snapshots.record(char_id, int(skills_doc.get("total_sp") or 0))
    except OSError:
        pass  # history is diagnostics; never fail the view over it

    return {
        "now": now,
        "token": token,
        "public": public,
        "skills_doc": skills_doc,
        "queue": queue,
        "rows": rows,
        "state": state,
        "caps": caps,
        "grade_name": grade_name,
        "data_build": data["grades"]["build"],
        "names": names,
    }


FILTERS = {
    # Bucket by what alpha can actually show, independent of the current clone state:
    # a downgraded omega keeps trained levels above the cap.
    "alpha": lambda r: not r.beyond_alpha,
    "omega": lambda r: r.beyond_alpha,
}


def queue_status(item: dict, now) -> str:
    """done | training | queued | blocked (CCP issued no schedule dates - item cannot train)."""
    if not item.get("start_date") or not item.get("finish_date"):
        return "blocked"
    if render.parse_ts(item["finish_date"]) <= now:
        return "done"
    if render.parse_ts(item["start_date"]) <= now:
        return "training"
    return "queued"


def level_sp_cell(item: dict, now, status: str) -> str:
    """`249.5K/256.0K 97%` - SP into the level being trained, out of what the level costs.

    ESI publishes three SP figures per queue item and they answer three different questions:
    `level_start_sp` is where the level began, `level_end_sp` is where it ends, and
    `training_start_sp` is what the character held when `start_date` was stamped. Progress through
    a level is an SP question, so it is answered from those three - never from the fraction of
    `start_date`..`finish_date` that has elapsed. EVE restamps `start_date` on the active item
    every time the queue is rearranged, so that fraction describes the current sitting at the
    keyboard rather than the level: an hour after a reorder a level 95% trained reads as 11%, under
    a column headed `level sp`.

    SP does accrue linearly across the span, which is what makes the interpolation from
    `training_start_sp` to `level_end_sp` exact and makes the restamped stamps harmless here - the
    span and `training_start_sp` are the pair that describe each other. A queued item has trained
    nothing yet and a blocked one never will, so both keep the dash the timing column explains.
    """
    level_start, level_end = item.get("level_start_sp"), item.get("level_end_sp")
    if level_start is None or level_end is None or int(level_end) <= int(level_start):
        # Nothing measurable was published. "done" is still known to be done - it finished - but a
        # figure this column cannot source is a dash, not an invented percentage.
        return "100%" if status == "done" else "-"
    level_start, level_end = int(level_start), int(level_end)
    if status == "done":
        held: float = level_end
    elif status == "training" and item.get("training_start_sp") is not None:
        start = render.parse_opt(item.get("start_date"))
        finish = render.parse_opt(item.get("finish_date"))
        if start is None or finish is None or finish <= start:
            return "-"
        at_start = int(item["training_start_sp"])
        span = (finish - start).total_seconds()
        # Clamped because ESI's stamps and its clock can disagree by a poll: a hair past the finish
        # is 100% of the level, never 101%.
        elapsed = min(max((now - start).total_seconds(), 0.0), span)
        held = at_start + (level_end - at_start) * elapsed / span
    else:
        return "-"
    return (f"{render.format_sp(int(round(held)))}/{render.format_sp(level_end)} "
            f"{(held - level_start) / (level_end - level_start):.0%}")


def render_text(ctx: dict, args) -> str:
    now = ctx["now"]
    public = ctx["public"]
    out: list[str] = []

    bloodline_race = classify.race_name(alphadata.load()["races"]["races"].get(str(public.get("bloodline_id"))))
    state = ctx["state"]
    out.append(f"{public.get('name')} (id {ctx['token']['character_id']})  {bloodline_race}  clone grade: {ctx['grade_name']}")
    out.append(f"Clone state: {state.state}  (confidence: {state.confidence})")
    for line in state.evidence[:3]:
        out.append(f"  evidence: {line}")
    for line in state.warnings:
        out.append(f"  warning: {line}")

    total = ctx["skills_doc"].get("total_sp", 0)
    unalloc = ctx["skills_doc"].get("unallocated_sp") or 0
    out.append(f"Total SP: {render.format_sp(total)}   unallocated: {render.format_sp(unalloc)}")

    queue = ctx["queue"]
    if queue and not args.trained_only:
        out.append("")
        out.append(f"TRAINING QUEUE ({len(queue)})")
        rows = []
        for item in queue:
            sid = int(item["skill_id"])
            status = queue_status(item, now)
            start = render.parse_opt(item.get("start_date"))
            finish = render.parse_opt(item.get("finish_date"))
            progress = level_sp_cell(item, now, status)
            if status == "done":
                label, when = "done", "finished (pending login)"
            elif status == "training":
                label = "TRAINING"
                when = f"{render.format_duration((finish - now).total_seconds())} left"
            elif status == "blocked":
                label, when = "BLOCKED", "no schedule - cannot train"
            else:
                label = "queued"
                when = f"starts in {render.format_duration((start - now).total_seconds())}, {render.format_duration((finish - start).total_seconds())} long"
            cap = ctx["caps"].get(sid)
            access = "alpha" if cap is not None and item.get("finished_level", 0) <= cap else "OMEGA"
            rows.append([label, ctx["names"].get(sid, f"skill {sid}"), f"L{item.get('finished_level', '?')}", progress, when, access])
        out.append(render.table(["status", "skill", "to", "level sp", "timing", "access"], rows))

    # ESI also lists never-trained prerequisite skills; the table is about what you have.
    rows_all = [r for r in ctx["rows"] if r.trained > 0 or r.pending_completion]
    alpha_n = sum(1 for r in rows_all if not r.beyond_alpha)
    omega_n = len(rows_all) - alpha_n
    filtered = [r for r in rows_all if FILTERS[args.filter](r)] if args.filter != "all" else rows_all

    if args.sort == "name":
        filtered.sort(key=lambda r: r.name.lower())
    elif args.sort == "level":
        filtered.sort(key=lambda r: (-r.trained, r.name.lower()))
    elif args.sort == "sp":
        filtered.sort(key=lambda r: -r.sp)

    out.append("")
    scope = "" if args.filter == "all" else f" (filter: {args.filter})"
    out.append(f"TRAINED SKILLS ({len(filtered)} of {len(rows_all)}; alpha-trainable: {alpha_n}, omega-restricted: {omega_n}){scope}")
    table_rows = []
    for r in filtered:
        level = f"{r.trained} (active {r.active})" if r.restricted else str(r.trained)
        if r.pending_completion:
            level += " *"
        if r.cap is None:
            access = "unknown data" if r.unknown_data else "OMEGA-only"
        else:
            access = f"alpha <= {r.cap}" + (" (exceeded)" if r.trained > r.cap else "")
        table_rows.append([r.name, level, render.format_sp(r.sp), access])
    out.append(render.table(["skill", "level", "sp", "clone access"], table_rows))
    if any(r.pending_completion for r in filtered):
        out.append("* completed training, not yet reflected by ESI (applies on next login)")

    out.append("")
    out.append(f"alpha caps source: SDE build {ctx['data_build']} (eve-skills update-data to refresh)")
    return "\n".join(out)


def render_json(ctx: dict, args) -> str:
    now = ctx["now"]
    public = ctx["public"]
    race_id = alphadata.load()["races"]["races"].get(str(public.get("bloodline_id")))
    doc = {
        "character": {
            "id": ctx["token"]["character_id"],
            "name": public.get("name"),
            "bloodline_id": public.get("bloodline_id"),
            "race_id": race_id,
            "race": classify.race_name(race_id),
            "alpha_grade": ctx["grade_name"],
        },
        "clone_state": {
            "state": ctx["state"].state,
            "confidence": ctx["state"].confidence,
            "evidence": ctx["state"].evidence,
            "warnings": ctx["state"].warnings,
        },
        "totals": {
            "total_sp": ctx["skills_doc"].get("total_sp"),
            "unallocated_sp": ctx["skills_doc"].get("unallocated_sp"),
        },
        "queue": [
            {
                "skill_id": int(q["skill_id"]),
                "name": ctx["names"].get(int(q["skill_id"])),
                "finished_level": q.get("finished_level"),
                "start_date": q.get("start_date"),
                "finish_date": q.get("finish_date"),
                "status": queue_status(q, now),
            }
            for q in ctx["queue"]
        ],
        "skills": [
            {
                "skill_id": r.skill_id,
                "name": r.name,
                "trained_level": r.trained,
                "active_level": r.active,
                "sp": r.sp,
                "alpha_cap": r.cap,
                "access": "omega" if r.beyond_alpha else "alpha",
                "restricted": r.restricted,
                "pending_completion": r.pending_completion,
                "unknown_data": r.unknown_data,
            }
            for r in ctx["rows"]
        ],
        "data_build": ctx["data_build"],
    }
    return json.dumps(doc, indent=2)


def cmd_login(args):
    scopes = (["attributes"] if args.attributes else []) + [s.strip() for s in (args.scopes or "").split(",") if s.strip()]
    record = sso.login(client_id=args.client_id, client_secret=args.client_secret, port=args.port,
                       scopes=scopes, manual=args.manual)
    print(f"Logged in as {record['character_name']} ({record['character_id']}).")
    print("Run eve-skills login again (selecting a different character) to add another.")


def cmd_logout(args):
    char_id = sso.resolve_character(args.char) if args.char else None
    sso.clear_tokens(char_id)
    print(f"Removed stored tokens for {args.char}." if char_id else "Removed stored tokens for all characters.")


def cmd_chars(args):
    records = sso.list_characters()
    if not records:
        print("no characters logged in — run: eve-skills login")
        return
    rows = []
    for r in records:
        left_min = max(int((r["expires_at"] - time.time()) / 60), 0)
        rows.append([str(r["character_id"]), r.get("character_name") or "?", f"{left_min} min", "yes" if r.get("refresh_token") else "no"])
    print(render.table(["character id", "name", "access token left", "auto-refresh"], rows))


@dataclass(frozen=True)
class FetchFailure:
    """One character whose fetch failed this cycle. str() keeps the legacy warning
    wording, so one-shot commands print exactly what they always printed."""
    character_id: int
    name: str
    error: str

    def __str__(self) -> str:
        return f"{self.name}: {self.error}"


def gather_all(args, client: esi_mod.Esi | None = None):
    """(contexts, FetchFailure list) for the selected character, or every stored one."""
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in — run: eve-skills login")
    wanted = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    ctxs, failures = [], []
    for rec in records:
        if wanted is not None and int(rec["character_id"]) != wanted:
            continue
        sub_args = argparse.Namespace(**vars(args))
        sub_args.char = str(wanted if wanted is not None else rec["character_id"])
        try:
            ctxs.append(gather(sub_args, client=client))
        except (RuntimeError, esi_mod.EsiError) as err:
            failures.append(FetchFailure(int(rec["character_id"]),
                                         rec.get("character_name") or str(rec["character_id"]), str(err)))
    return ctxs, failures


def week_line(ctx) -> str:
    """SP gained since the newest snapshot at least 7 days old; honest when history is short."""
    char_id = ctx["token"]["character_id"]
    now_ts = ctx["now"].timestamp()
    cur = int(ctx["skills_doc"].get("total_sp") or 0)
    base = snapshots.latest_before(char_id, now_ts - 7 * 86400)
    if base is None:
        first = min((r["ts"] for r in snapshots.load() if r["char_id"] == char_id), default=now_ts)
        return f"SP last 7d: no baseline yet (local history starts {(now_ts - first) / 86400:.1f} days ago)"
    days = max((now_ts - base["ts"]) / 86400, 1e-9)
    delta = cur - base["total_sp"]
    return f"SP last 7d: {delta:+,} over {days:.1f} days ({render.format_sp(int(delta / days))}/day)"


def render_csv(ctxs) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["character_id", "character_name", "skill_id", "name", "trained_level", "active_level",
                     "sp", "alpha_cap", "access", "restricted", "pending_completion", "unknown_data"])
    for ctx in ctxs:
        cid = ctx["token"]["character_id"]
        cname = ctx["public"].get("name") or ""
        for r in ctx["rows"]:
            if r.trained == 0 and not r.pending_completion:
                continue  # ESI lists never-trained prerequisites; the table view filters them - CSV must agree
            writer.writerow([cid, cname, r.skill_id, r.name, r.trained, r.active, r.sp,
                             "" if r.cap is None else r.cap, "omega" if r.beyond_alpha else "alpha",
                             int(r.restricted), int(r.pending_completion), int(r.unknown_data)])
    return buf.getvalue()


def observation_from_ctx(ctx: dict) -> watchstate.CharacterObservation:
    """The persisted slice of one fetch: queue entries and settled levels only.
    Tokens stay in the ctx and never reach watchstate."""
    cid = ctx["token"]["character_id"]
    items = tuple(
        watchstate.QueueItem(
            skill_id=int(q["skill_id"]), finished_level=int(q.get("finished_level") or 0),
            status=queue_status(q, ctx["now"]), name=ctx["names"].get(int(q["skill_id"]), f"skill {int(q['skill_id'])}"),
            finish_date=q.get("finish_date"),
        )
        for q in ctx["queue"]
    )
    return watchstate.CharacterObservation(
        character_id=cid, character_name=ctx["public"].get("name") or str(cid),
        items=items, trained_levels={r.skill_id: r.trained for r in ctx["rows"]},
    )


# What ended the order, in the words ESI is actually entitled to. `order_closed` is the honest one:
# the order left our view and the history row never arrived, so nothing here claims a reason.
ORDER_OUTCOMES = {"order_filled": "order filled",
                  "order_cancelled": "cancelled",
                  "order_closed": "closed, reason unknown to ESI"}


def order_event_text(ev: dict, names: dict[int, str]) -> str:
    """One order event as the sentence a trader wants: what moved, at what price, and how it ended."""
    data = ev.get("data") or {}
    buy = bool(data.get("is_buy"))
    verb = "bought" if buy else "sold"
    moved, total = int(data.get("filled") or 0), int(data.get("volume_total") or 0)
    item = data.get("type_name") or names.get(data.get("type_id")) or f"type {data.get('type_id')}"
    # "listed" for an order that sold nothing: the quantity then means what was put up, not moved.
    phrase = f"{verb} {moved:,} x {item}" if moved else f"{total:,} x {item} listed"
    if data.get("price") is not None:
        phrase += f" at {_isk(data['price'])} ISK"
    station = names.get(data["location_id"]) if data.get("location_id") else None
    if station:
        phrase += f" ({station})"
    if ev["kind"] == "order_expired":
        outcome = f"expired with {moved:,} of {total:,} {verb}"
    else:
        outcome = ORDER_OUTCOMES.get(ev["kind"], "closed")
    if data.get("backfill"):
        # Read out of ESI's history on the owner's first poll, never witnessed as it happened.
        return f"{ev['character_name']}: {phrase} - {outcome} [history]"
    if data.get("ts_estimated"):
        return f"{ev['character_name']}: {phrase} - {outcome} [time estimated]"
    return f"{ev['character_name']}: {phrase} - {outcome}"


def event_text(ev: dict, names: dict[int, str] | None = None) -> str:
    """Human sentence for one recorded event (watch banner and events table share it).

    `names` is optional because the two callers differ: a watch cycle resolves type and station ids
    anyway, while `events` stays strictly offline - so there an order sentence simply omits the
    station rather than printing an id nobody can read off."""
    if ev["kind"] == "queue_empty":
        return f"{ev['character_name']}: training queue is now empty"
    if str(ev["kind"]).startswith("order_"):
        return order_event_text(ev, names or {})
    return f"{ev['character_name']}: {ev['skill_name']} to L{ev['finished_level']} - finished training"


def notify_desktop(text: str) -> None:
    """Best-effort desktop ping: a missing, hanging or failing notify-send must
    never kill an overnight watch."""
    try:
        subprocess.run(["notify-send", "eve-skills", text], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


# Shipped by nothing on Windows; the watch says this once per run rather than going quiet.
NOTIFY_ABSENT_NOTICE = "desktop notifications need notify-send; events are still printed and recorded"
_notify_warned = False


def render_watch_status(ctxs, failures, last_good: dict[int, dict]) -> str:
    """Compact multi-character status table. A character whose fetch failed keeps
    its last-known row, marked stale, with the error under the table - healthy
    data is never dropped because a sibling went quiet."""
    rows = []
    for ctx in ctxs:
        now = ctx["now"]
        queue = ctx["queue"]
        statuses = [queue_status(q, now) for q in queue]
        training = next((q for q in queue if queue_status(q, now) == "training"), None)
        done_n = statuses.count("done")
        if not queue:
            queue_cell = "empty"
        else:
            queue_cell = f"{len(queue)} item" + ("" if len(queue) == 1 else "s")
            if done_n:
                queue_cell += f" ({done_n} done)"
        if training is not None:
            sid = int(training["skill_id"])
            left = render.format_duration((render.parse_ts(training["finish_date"]) - now).total_seconds())
            training_cell = f"{ctx['names'].get(sid, f'skill {sid}')} to L{training.get('finished_level', '?')}, {left} left"
        elif "blocked" in statuses:
            training_cell = "blocked (no schedule)"
        else:
            training_cell = "-"
        row = [ctx["public"].get("name") or "?", str(ctx["token"]["character_id"]), ctx["state"].state,
               queue_cell, training_cell, render.format_sp(int(ctx["skills_doc"].get("total_sp") or 0))]
        last_good[ctx["token"]["character_id"]] = {"row": row, "ts": time.time()}
        rows.append(row + ["ok"])
    now_ts = time.time()
    for f in failures:
        prev = last_good.get(f.character_id)
        if prev is not None:
            age = render.format_duration(now_ts - prev["ts"])
            rows.append(prev["row"] + [f"{age} stale"])
        else:
            rows.append([f.name, str(f.character_id), "?", "-", "-", "-", "no data yet"])
    out = [render.table(["character", "id", "clone", "queue", "training now", "sp", "fetched"], rows)]
    for f in failures:
        out.append(f"! {f.name}: {f.error}")
    return "\n".join(out)


@dataclass(frozen=True)
class WatchCycle:
    """One poll of a watch loop, whatever it was watching.

    `warnings` are this cycle's problems that do not stop the run; `observations` and
    `order_observations` are what gets claimed against persisted state; `body` is the status view to
    print under the announcements; `names` lets the sentences name types and stations; `idle` is the
    line for a cycle that could observe nothing at all."""
    title: str = "watch"
    warnings: tuple = ()
    observations: tuple = ()
    order_observations: tuple = ()
    body: str = ""
    names: dict = field(default_factory=dict)
    idle: str | None = None


@dataclass
class WatchSession:
    """Per-process memory of one watch run.

    `warned` remembers which refusals have already been printed: missing consent and a missing
    corporation role do not change between two polls, and repeating either every five minutes would
    bury the events a watcher exists to surface. `last_good` keeps the newest healthy row per
    character so a failed fetch still shows something, marked stale."""
    warned: set = field(default_factory=set)
    last_good: dict = field(default_factory=dict)


def warn_once(seen: set, key: str, text: str) -> list[str]:
    """`text`, but only the first time this session sees `key`."""
    if key in seen:
        return []
    seen.add(key)
    return [text]


CLEAR_SCREEN = "\x1b[H\x1b[2J"   # home + clear: every POSIX terminal, and a Windows console
                                  # only after virtual-terminal processing has been switched on
_vt_processing: bool | None = None  # cached verdict of _enable_vt_processing(), one attempt per run


def _enable_vt_processing() -> bool:
    """Ask a Windows console to interpret ANSI escapes instead of printing them.

    ConHost hands bytes to the screen buffer verbatim unless ENABLE_VIRTUAL_TERMINAL_PROCESSING is
    set, and older builds never grew the mode at all - there `\\x1b[H` renders as garbage. On every
    other OS ``ctypes.windll`` does not exist in the first place, and a redirected stdout has no
    console mode to set; both are honest False answers, not errors."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32          # AttributeError off Windows
        handle = kernel32.GetStdHandle(-11)         # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False                           # no console behind stdout (pipe, file)
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def watch_clear() -> str:
    """The bytes a --watch frame starts with on a tty.

    POSIX terminals - and Windows consoles that accepted virtual-terminal processing - get the real
    clear, exactly as always. One that did not gets a rule line instead of escape-code litter; the
    timestamped header under it still separates the frames."""
    global _vt_processing
    if not paths.is_windows():
        return CLEAR_SCREEN
    if _vt_processing is None:
        _vt_processing = _enable_vt_processing()
    return CLEAR_SCREEN if _vt_processing else "-" * 72 + "\n"


def watch_loop(args, poll) -> int:
    """The shared --watch skeleton: poll, claim the transitions once, announce, render, sleep.

    `poll()` takes one turn and returns a WatchCycle with every transient problem already turned into
    a warning line - an ESI or network outage must never kill an overnight session, so nothing in it
    raises and only Ctrl-C ends it. Claiming is one locked commit over both documents, so a crash
    between appending events and writing state replays to the same ids instead of announcing twice.
    """
    global _notify_warned
    interval = max(args.watch, 1) * 60
    notify = args.notify and shutil.which("notify-send")
    if args.notify and not notify and not _notify_warned:
        # Windows ships no notify-send; silence after a flag that promised a ping is worse than one
        # honest line. The \a bells still fire, so this only explains the missing pop-up - once per run.
        _notify_warned = True
        print(f"warning: {NOTIFY_ABSENT_NOTICE}", file=sys.stderr)
    try:
        while True:
            cycle = poll()
            for line in cycle.warnings:
                print(f"warning: {line}", file=sys.stderr)
            if cycle.idle:
                print(f"warning: {cycle.idle}", file=sys.stderr)
            # Transitions are claimed against the persisted state, so alerts fire
            # once per event across polls, restarts and concurrent watchers.
            events = (watchstate.commit(cycle.observations, cycle.order_observations)
                      if (cycle.observations or cycle.order_observations) else [])
            if sys.stdout.isatty():
                print(watch_clear(), end="")
            print(f"eve-skills {cycle.title} - {time.strftime('%Y-%m-%d %H:%M:%S')} (every {args.watch}m, Ctrl-C to stop)")
            for ev in events:
                if ev.data.get("backfill"):
                    continue  # ESI's backlog is recorded, never announced: history, not news
                text = event_text(ev.to_json(), cycle.names)
                print(f"\a{text}")
                if notify:
                    notify_desktop(text)  # once per newly claimed event, never per poll
            print(cycle.body)
            time.sleep(interval)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def skills_cycle(args, client: esi_mod.Esi, session: WatchSession) -> WatchCycle:
    """One `skills --watch` turn: the training book, plus order events wherever consent allows."""
    ctxs, failures = gather_all(args, client=client)
    warnings = [f"skipped {f}" for f in failures]
    poll = OrderPoll() if args.no_orders else poll_order_observations(args, client, session.warned)
    warnings += poll.warnings
    names = order_names(client, poll.books)
    if args.full:
        body = ("\n" + "=" * 72 + "\n").join(render_text(ctx, args) for ctx in ctxs)
    else:
        body = render_watch_status(ctxs, failures, session.last_good)
    return WatchCycle(warnings=tuple(warnings),
                      observations=tuple(observation_from_ctx(c) for c in ctxs),
                      order_observations=tuple(watchstate.OrderObservation.from_book(b, names)
                                               for b in poll.books),
                      body=body, names=names,
                      # a transient ESI/network outage must not kill an overnight session
                      idle=None if ctxs else "no character data this cycle - retrying next poll")


def cmd_watch(args):
    """`skills --watch`: training transitions, plus market order events wherever consent allows."""
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    session = WatchSession()
    return watch_loop(args, lambda: skills_cycle(args, client, session))


def cmd_summary(args):
    ctxs, failures = gather_all(args)
    for line in failures:
        print(f"warning: skipped {line}", file=sys.stderr)
    if not ctxs:
        raise RuntimeError("no character data could be fetched")
    rows, total_sp = [], 0
    for ctx in ctxs:
        sp = int(ctx["skills_doc"].get("total_sp") or 0)
        total_sp += sp
        active = next((i for i in ctx["queue"] if queue_status(i, ctx["now"]) == "training"), None)
        left = render.format_duration((render.parse_ts(active["finish_date"]) - ctx["now"]).total_seconds()) if active else "-"
        rows.append([ctx["public"].get("name") or "?", str(ctx["token"]["character_id"]),
                     f"{ctx['state'].state} ({ctx['state'].confidence})", render.format_sp(sp), str(len(ctx["queue"])), left])
    rows.append(["TOTAL", "", f"{len(ctxs)} characters", render.format_sp(total_sp), "", ""])
    print(render.table(["character", "id", "clone state", "total SP", "queue items", "current item left"], rows))


# Order events keep their payload in `data`; a training row leaves these cells empty and vice
# versa. One flat table is what a spreadsheet wants, and an empty cell says "not this kind of
# event" without needing a second file.
EVENTS_CSV_COLUMNS = ["id", "ts", "time_utc", "kind", "character_id", "character_name",
                      "skill_id", "skill_name", "finished_level", "finish_date"]
EVENTS_CSV_ORDER_COLUMNS = ["order_id", "owner_key", "owner_name", "type_id", "type_name", "is_buy",
                            "price", "volume_total", "volume_remain", "filled", "region_id",
                            "location_id", "issued", "expires", "wallet_division", "issued_by",
                            "backfill", "ts_estimated"]


def matches_owner(event: dict, spec: str) -> bool:
    """`events --owner`: the exact owner key, or a case-insensitive fragment of the owner name.

    Order events are attributed to whoever owns the order, not to whoever's token read the book: a
    corporation event carries `character_id = None` on purpose, so `--char` cannot reach it and this
    is the only filter that can. Training events have no owner payload and never match."""
    data = event.get("data") or {}
    needle = spec.strip().lower()
    if str(data.get("owner_key") or "").lower() == needle:
        return True
    name = str(data.get("owner_name") or "")
    return bool(name) and needle in name.lower()


def cmd_events(args):
    """Read-only view of the watch event history (never writes, never fetches)."""
    kinds = set(args.kind or [])
    if kinds:
        unknown = sorted(kinds - set(watchstate.EVENT_KINDS))
        if unknown:
            # Fail before reading anything: a typo'd filter would otherwise look like empty history.
            raise RuntimeError(f"unknown event kind: {', '.join(unknown)} - "
                               f"expected one of: {', '.join(watchstate.EVENT_KINDS)}")
    char_id = None
    if args.char:
        # a bare numeric id also finds events for characters already logged out
        char_id = int(args.char) if args.char.isdigit() else sso.resolve_character(args.char)
    events, skipped = watchstate.load_events(char_id)
    if skipped:
        print(f"warning: skipped {skipped} unreadable event history line(s)", file=sys.stderr)
    if kinds:
        events = [ev for ev in events if ev["kind"] in kinds]
    if args.owner:
        events = [ev for ev in events if matches_owner(ev, args.owner)]
    page = list(reversed(events[-max(args.limit, 1):]))  # most recent first, bounded
    if args.json:
        print(json.dumps(page, indent=2))
        return
    if args.csv:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(EVENTS_CSV_COLUMNS + EVENTS_CSV_ORDER_COLUMNS)
        for ev in page:
            writer.writerow([ev["id"], f"{ev['ts']:.3f}",
                             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["ts"])),
                             ev["kind"], ev["character_id"], ev.get("character_name") or "",
                             "" if ev.get("skill_id") is None else ev["skill_id"],
                             ev.get("skill_name") or "",
                             "" if ev.get("finished_level") is None else ev["finished_level"],
                             ev.get("finish_date") or "",
                             *[_raw((ev.get("data") or {}).get(col))
                               for col in EVENTS_CSV_ORDER_COLUMNS]])
        sys.stdout.write(buf.getvalue())
        return
    if not page:
        print("no recorded events yet - eve-skills skills --watch records finished training and empty "
              "queues, eve-skills orders --watch records filled, expired and cancelled orders")
        return
    rows = [[time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"])),
             ev.get("character_name") or str(ev["character_id"]), event_text(ev)] for ev in page]
    print(render.table(["time", "character", "event"], rows))


def cmd_attributes(args):
    records = [sso.resolve_character(args.char)] if args.char else [r["character_id"] for r in sso.list_characters()]
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    blocks, missing = [], []
    for cid in records:
        rec = sso.get_access_token(cid)
        name = rec.get("character_name") or str(cid)
        if not sso.has_feature(rec, "attributes"):
            missing.append(name)
            continue
        a = client.get(f"/characters/{cid}/attributes", token=rec["access_token"])
        attrs = "  ".join(f"{k[:3].upper()} {a.get(k)}" for k in ("perception", "intelligence", "memory", "charisma", "willpower"))
        last_remap = (a.get("last_remap_date") or "")[:10] or "never"
        block = f"{name} (id {cid})\n  {attrs}\n  remaps available: {a.get('accumulated_remaps', '?')}   last remap: {last_remap}"
        if a.get("accelerator_bonus_days"):
            block += f"   accelerator days left: {a['accelerator_bonus_days']}"
        blocks.append(block)
    for name in missing:
        blocks.append(f"{name}: no skills consent - run: eve-skills login  (pick '{name}' in the browser)")
    print("\n\n".join(blocks))


@dataclass
class MarketRow:
    """One quote plus the ids and traded-volume figures that explain where it came from."""

    quote: market.Quote
    region_id: int | None = None
    location_id: int | None = None
    history: market.HistoryStats | None = None


MARKET_COLUMNS = ["scope", "min sell", "max buy", "spread", "margin %", "sell vol", "buy vol",
                  "sells", "buys", "best sell at", "best buy at"]
MARKET_CSV_COLUMNS = ["type_id", "type_name", "scope", "region_id", "region_name", "location_id",
                      "location_name", "min_sell", "max_buy", "spread", "margin_pct", "sell_volume",
                      "buy_volume", "sell_orders", "buy_orders", "best_sell_location_id",
                      "best_sell_location_name", "best_sell_region_id", "best_sell_region_name",
                      "best_buy_location_id", "best_buy_location_name", "best_buy_region_id",
                      "best_buy_region_name", "regions_scanned", "regions_failed", "last_modified",
                      "expires", "age_seconds", "history_days", "history_rows", "history_total_volume",
                      "history_volume_per_day", "history_average_price", "history_newest_date",
                      # ESI's published reference, never a quote: appended so the header above keeps
                      # its meaning for anyone whose script already reads these columns by name.
                      "reference_average_price", "reference_adjusted_price",
                      "reference_last_modified", "reference_age_seconds"]


def _hub_scope(spec: str) -> market.Scope:
    """A named trade hub, narrowed to its station rather than its whole region."""
    hub = market.HUBS.get(spec.strip().lower())
    if hub is None:
        raise RuntimeError(f"unknown hub '{spec}' - choices: {', '.join(market.HUBS)}")
    return market.Scope(hub.region_id, f"{hub.label} (station)", hub.system_id, hub.station_id)


def market_scope_list(client: esi_mod.Esi, args) -> list[market.Scope]:
    """Every station/region scope asked for; nothing at all asked means station-level Jita.

    Jita is what a trader means by "the price of Tritanium", and it is also the cheapest question
    ESI can be asked: one regional book instead of seventy. `--global` alone therefore gets no
    scope here at all - the cluster row is the answer that was asked for."""
    scopes: list[market.Scope] = []
    for spec in args.region or []:
        region_id, name = market.resolve_region(client, spec)
        scopes.append(market.Scope(region_id, name))
    for spec in args.hub or []:
        scopes.append(_hub_scope(spec))
    if not scopes and not args.global_scopes:
        scopes.append(_hub_scope("jita"))
    # The same region named twice ("The Forge" and 10000002) is one row, not two.
    return list({(s.region_id, s.system_id, s.location_id): s for s in scopes}.values())


def market_type_rows(client: esi_mod.Esi, args, type_id: int, scopes, regions) -> tuple[list[MarketRow], list[str]]:
    """Rows for one type, plus warnings about the parts of the cluster ESI did not answer for."""
    rows = [MarketRow(market.quote(client, type_id, scope), scope.region_id, scope.location_id)
            for scope in scopes]
    if regions:
        rows.append(MarketRow(market.quote_cluster(client, type_id, regions)))
    warnings = []
    for row in rows:
        if args.history and row.region_id is not None:
            # Two scopes in one region ask twice; ESI's day-long Expires makes the repeat free.
            # The cluster row has no single region, so it carries no traded volume at all.
            row.history = market.history_stats(client, row.region_id, type_id, args.history)
        if row.quote.regions_failed:
            total = row.quote.regions_scanned + row.quote.regions_failed
            warnings.append(f"{row.quote.scope}: {row.quote.regions_failed} of {total} regions did not "
                            f"answer; their orders are missing from the numbers above")
    return rows, warnings


def _isk(value: float | None) -> str:
    """ISK with thousands separators; "-" for a side nobody is quoting (0 ISK would be a lie)."""
    return "-" if value is None else f"{value:,.2f}"


def _raw(value) -> str:
    """CSV cell for an optional value: empty when unknown, unformatted otherwise - and bools as 1/0,
    the convention every CSV in this tool already uses."""
    if value is None:
        return ""
    return str(int(value)) if isinstance(value, bool) else str(value)


def _book_is_empty(rows) -> bool:
    """True when no scope asked for had a single order on either side."""
    return all(row.quote.sell_orders + row.quote.buy_orders == 0 for row in rows)


# An empty book is only evidence about the books that were read, so its footnote is chosen by what
# this run actually covered - not by the fact that nothing came back. Two of the four cases below
# have earned the right to quote ESI's published reference, and they are exactly the two where no
# wider order book exists to ask for. The others get the wider question instead of a figure, because
# an item absent from one station is absent from one station, and pricing it from there is how a
# reader ends up believing a thinly traded module has no market at all.

# Asked for stations or systems: the whole regional book is still unasked, so name it.
EMPTY_BOOK_NOTE_STATION = (
    "No orders in any requested scope, which is a statement about those books and nothing else: an",
    "item nobody stocks at one station trades freely at the next. The wider book is still unasked -",
    "try {wider}, which reads the whole regional book ESI publishes.",
)

# Asked for whole regions, but not for every region: same reasoning one level up, and only the
# cluster scan is left to ask.
EMPTY_BOOK_NOTE_REGION = (
    "No orders in any requested scope, which is a statement about those regions and nothing else:",
    "a thinly traded module can be empty here and stocked one region over. The widest question ESI",
    "can be asked is still unasked - --global reads every market region's book.",
)

# Whole-cluster coverage: nothing wider does exist, because ESI publishes books per region only and
# has no endpoint above them. That is still not a cause, so the note declines to offer one.
EMPTY_BOOK_NOTE_CLUSTER = (
    "No orders in any book this run read, including a scan of every market region: ESI publishes",
    "order books per region only and has no global endpoint, so no wider book was left to ask.",
    "Nothing here says why: it says only that no region has an order out for this type right now.",
)

# The one type whose emptiness has a measured cause rather than an inferred one: no regional
# book can show it at all, whatever scope is asked (see `market.VAULT_TRADED_TYPE_IDS` for the id,
# the date and what exactly was measured).
EMPTY_BOOK_NOTE_VAULT = (
    "No orders in any requested scope: ESI publishes order books per region only, and no regional",
    "book can show this type - it trades on the account-wide vault market instead.",
    "There is no global order-book endpoint above them, so no wider book was left to ask.",
)

EMPTY_BOOK_NOTES = {"station": EMPTY_BOOK_NOTE_STATION, "region": EMPTY_BOOK_NOTE_REGION,
                    "cluster": EMPTY_BOOK_NOTE_CLUSTER, "vault": EMPTY_BOOK_NOTE_VAULT}
# The cases with nothing wider left to ask, and therefore the only ones that show a reference figure
# - which is what `cmd_market` decides the cost of `/markets/prices` on.
REFERENCE_BOOK_CASES = ("cluster", "vault")


@dataclass(frozen=True)
class MarketCoverage:
    """What one `market` run actually asked ESI, which is all an empty book can honestly answer for.

    ESI publishes order books per region and nothing above them, so a whole-cluster scan is the
    widest question there is. Anything short of it leaves a bigger book unasked, and then the honest
    footnote names that book rather than explaining the item away."""

    scopes: tuple[market.Scope, ...] = ()
    cluster_scanned: bool = False

    def empty_book_case(self, type_id: int, rows) -> str | None:
        """Which footnote this type has earned, or None when some scope had orders.

        `cmd_market` pays for `/markets/prices` on the strength of this predicate alone, and
        `market_text` prints its wording from it, so the two cannot drift apart."""
        if not _book_is_empty(rows):
            return None
        if type_id in market.VAULT_TRADED_TYPE_IDS:
            return "vault"
        if self.cluster_scanned:
            return "cluster"
        # A scope narrowed to one system or station still has its region's whole book above it; a
        # run that read whole regions already has only the cluster scan left.
        narrowed = any(scope.system_id or scope.location_id for scope in self.scopes)
        return "station" if narrowed else "region"

    def needs_reference(self, entries) -> bool:
        """True when some requested type's footnote will quote ESI's published reference."""
        return any(self.empty_book_case(type_id, rows) in REFERENCE_BOOK_CASES
                   for type_id, _name, rows in entries)


def _wider_book_hint(scopes, names) -> str:
    """The next-wider question this run did not ask, spelled the way it has to be typed back."""
    regions = list(dict.fromkeys(scope.region_id for scope in scopes
                                 if scope.system_id or scope.location_id))
    return " or ".join(f'--region "{names.get(rid) or rid}"' for rid in regions)


def market_empty_book_notes(case: str, scopes, names) -> list[str]:
    """Lines under a type's table for a book that came back empty wherever it was read."""
    note = EMPTY_BOOK_NOTES[case]
    if case == "station":
        note = tuple(line.format(wider=_wider_book_hint(scopes, names)) for line in note)
    return [f"  {line}" for line in note]


def market_reference_notes(reference, now: float) -> list[str]:
    """ESI's published figures for a type no order book answered, labelled as what they are not."""
    if reference is None:
        return ["  ESI's price document has no row for this type either, so there is no "
                "published price to show."]
    figures = []
    if reference.average_price is not None:
        figures.append(f"average {_isk(reference.average_price)} ISK")
    if reference.adjusted_price is not None:
        figures.append(f"industry adjusted {_isk(reference.adjusted_price)} ISK")
    if not figures:
        return ["  ESI's price document lists this type without a price, so there is "
                "no published figure to show."]
    return [f"  ESI's published reference for this type: {', '.join(figures)}",
            f"  {market.reference_freshness_line(reference.meta, now)}",
            "  A published figure, not a bid or an ask: nothing can be bought or sold at it."]


def market_reference_doc(reference, now: float) -> dict | None:
    """ESI's published reference as machine-readable data, stamped as what it is.

    `kind` exists because a consumer that reads `average_price` next to `min_sell` would otherwise
    have no way to tell a figure ESI publishes from an order someone placed."""
    if reference is None:
        return None
    return {"kind": "esi_published_reference",
            "average_price": reference.average_price,
            "adjusted_price": reference.adjusted_price,
            "last_modified": market.iso_utc(reference.meta.last_modified),
            "expires": market.iso_utc(reference.meta.expires),
            "age_seconds": None if reference.meta.last_modified is None
            else round(now - reference.meta.last_modified, 1)}


def _reference_of(prices, type_id: int):
    """One type's row in ESI's price document - None when this run had no reason to read it at all."""
    return None if prices is None else prices.reference(type_id)


def market_text(client: esi_mod.Esi, type_id: int, type_name: str, rows, names, coverage,
                history_days, prices) -> str:
    columns = list(MARKET_COLUMNS) + (["traded/day*", "traded total*"] if history_days else [])
    table_rows = []
    for row in rows:
        q = row.quote
        cells = [q.scope, _isk(q.min_sell), _isk(q.max_buy), _isk(q.spread),
                 "-" if q.margin_pct is None else f"{q.margin_pct:.2f}",
                 f"{q.sell_volume:,}", f"{q.buy_volume:,}", str(q.sell_orders), str(q.buy_orders),
                 exports.name_or_id(names, q.best_sell_location),
                 exports.name_or_id(names, q.best_buy_location)]
        if history_days:
            cells += [f"{row.history.volume_per_day:,.0f}" if row.history else "-",
                      f"{row.history.total_volume:,}" if row.history else "-"]
        table_rows.append(cells)
    now = client.now().timestamp()
    lines = [f"{type_name} (id {type_id})", render.table(columns, table_rows)]
    # One freshness line per scope: scopes are fetched separately and can be minutes apart in age,
    # so a single stamp for the whole block would quietly claim they are all as old as the oldest.
    lines += [f"  {row.quote.scope}: {market.freshness_line(row.quote.meta, now)}" for row in rows]
    case = coverage.empty_book_case(type_id, rows)
    if case is not None:
        lines += market_empty_book_notes(case, coverage.scopes, names)
        if case in REFERENCE_BOOK_CASES:
            # `cmd_market` fetches `/markets/prices` for exactly these two cases and no others, so a
            # table is in hand whenever the footnote has earned one.
            lines += market_reference_notes(prices.reference(type_id), now)
    return "\n".join(lines)


def market_history_doc(row: MarketRow, names) -> dict | None:
    if row.history is None:
        return None
    h = row.history
    return {"region_id": h.region_id, "region_name": names.get(h.region_id), "days": h.days,
            "rows": h.rows, "total_volume": h.total_volume, "volume_per_day": h.volume_per_day,
            "average_price": h.average_price, "newest_date": h.newest_date}


def market_json(client: esi_mod.Esi, entries, names, history_days, prices) -> dict:
    """Machine-readable output: ids and names both, numbers unformatted, ages as real numbers."""
    now = client.now().timestamp()
    scopes = []
    for _type_id, _type_name, rows in entries:
        docs = []
        for row in rows:
            q = row.quote
            docs.append({
                "scope": q.scope,
                "region_id": row.region_id,
                "region_name": names.get(row.region_id),
                "location_id": row.location_id,
                "location_name": names.get(row.location_id),
                "min_sell": q.min_sell,
                "max_buy": q.max_buy,
                "spread": q.spread,
                "margin_pct": q.margin_pct,
                "sell_volume": q.sell_volume,
                "buy_volume": q.buy_volume,
                "sell_orders": q.sell_orders,
                "buy_orders": q.buy_orders,
                "best_sell_location_id": q.best_sell_location,
                "best_sell_location_name": names.get(q.best_sell_location),
                "best_sell_region_id": q.best_sell_region,
                "best_sell_region_name": names.get(q.best_sell_region),
                "best_buy_location_id": q.best_buy_location,
                "best_buy_location_name": names.get(q.best_buy_location),
                "best_buy_region_id": q.best_buy_region,
                "best_buy_region_name": names.get(q.best_buy_region),
                "regions_scanned": q.regions_scanned,
                "regions_failed": q.regions_failed,
                "last_modified": market.iso_utc(q.meta.last_modified),
                "expires": market.iso_utc(q.meta.expires),
                "age_seconds": None if q.meta.last_modified is None else round(now - q.meta.last_modified, 1),
                "history": market_history_doc(row, names),
            })
        scopes.append(docs)
    return {
        "generated": market.iso_utc(now),
        "history_days": history_days,
        # The reference is per type, not per scope, and the key is present for every requested type -
        # including the ones with a live book, so a script never has to guess which kind of number it
        # is holding. null means either that this run had no reason to read ESI's price document at
        # all (every book had orders, or the empty ones were narrower than a cluster scan, where the
        # answer is a wider scope rather than a figure), or that the document has no row for the type.
        "types": [{"type_id": type_id, "name": type_name, "scopes": docs,
                   "reference": market_reference_doc(_reference_of(prices, type_id), now)}
                  for (type_id, type_name, _rows), docs in zip(entries, scopes)],
    }


def market_csv(client: esi_mod.Esi, entries, names, history_days, prices):
    now = client.now().timestamp()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(MARKET_CSV_COLUMNS)
    for type_id, type_name, rows in entries:
        # Empty cells when the document was never read or has no row: both mean "ESI publishes
        # nothing for this type", which is what a reader of these columns needs either way.
        reference = _reference_of(prices, type_id)
        for row in rows:
            q = row.quote
            h = row.history
            writer.writerow([
                type_id, type_name, q.scope, _raw(row.region_id), names.get(row.region_id) or "",
                _raw(row.location_id), names.get(row.location_id) or "",
                _raw(q.min_sell), _raw(q.max_buy), _raw(q.spread), _raw(q.margin_pct),
                q.sell_volume, q.buy_volume, q.sell_orders, q.buy_orders,
                _raw(q.best_sell_location), names.get(q.best_sell_location) or "",
                _raw(q.best_sell_region), names.get(q.best_sell_region) or "",
                _raw(q.best_buy_location), names.get(q.best_buy_location) or "",
                _raw(q.best_buy_region), names.get(q.best_buy_region) or "",
                q.regions_scanned, q.regions_failed, market.iso_utc(q.meta.last_modified) or "",
                market.iso_utc(q.meta.expires) or "",
                _raw(None if q.meta.last_modified is None else round(now - q.meta.last_modified, 1)),
                _raw(history_days if h else None), _raw(h.rows if h else None),
                _raw(h.total_volume if h else None), _raw(h.volume_per_day if h else None),
                _raw(h.average_price if h else None), h.newest_date if h else "",
                # Appended, never interleaved: the columns above mean what they always meant.
                _raw(reference.average_price if reference else None),
                _raw(reference.adjusted_price if reference else None),
                market.iso_utc(reference.meta.last_modified) if reference else "",
                _raw(None if reference is None or reference.meta.last_modified is None
                     else round(now - reference.meta.last_modified, 1)),
            ])
    sys.stdout.write(buf.getvalue())


def cmd_market(args):
    """Live order-book prices for item types: public ESI, no login and no stored character."""
    if args.history is not None and args.history < 1:
        raise RuntimeError("--history needs a positive number of days")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    types = list(dict.fromkeys(market.resolve_type(client, spec) for spec in args.type))
    scopes = market_scope_list(client, args)
    regions = market.market_regions(client) if args.global_scopes else []
    # What this run covered, recorded once: it is what decides both the wording of an empty-book
    # footnote and whether `/markets/prices` is worth reading at all.
    coverage = MarketCoverage(tuple(scopes), cluster_scanned=bool(regions))
    entries, warnings = [], []
    for type_id, type_name in types:
        rows, row_warnings = market_type_rows(client, args, type_id, scopes, regions)
        entries.append((type_id, type_name, rows))
        warnings += row_warnings
    ids = {i for _tid, _name, rows in entries for row in rows
           for i in (row.region_id, row.location_id, row.quote.best_sell_location,
                     row.quote.best_buy_location, row.quote.best_sell_region,
                     row.quote.best_buy_region, row.history.region_id if row.history else None)
           if i is not None}
    names = esi_mod.resolve_names(client, ids) if ids else {}
    # `/markets/prices` is one document listing every type CCP prices - over a megabyte - so it is
    # read at most once per run, and only when a footnote is going to quote it: an empty book in a
    # case where no wider order book exists to ask about (a measured vault type, or a whole-cluster
    # scan). Every narrower empty book gets the wider scope named instead of a figure, so neither
    # `--json` nor `--csv` pays for the document on its own any more; a run whose books all had
    # orders sends no request for it in any output format.
    prices = market.price_table(client) if coverage.needs_reference(entries) else None
    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    if args.json:
        print(json.dumps(market_json(client, entries, names, args.history, prices), indent=2))
    elif args.csv:
        market_csv(client, entries, names, args.history, prices)
    else:
        blocks = [market_text(client, type_id, name, rows, names, coverage, args.history, prices)
                  for type_id, name, rows in entries]
        if args.history:
            blocks.append("* ESI traded volume is daily and one day behind, and only exists per region: "
                          "a hub row shows its region's trades, the global row shows none.")
        print("\n\n".join(blocks))


# ---------------------------------------------------------------------------
# build-cost: what an install would cost, and whether buying beats it
# ---------------------------------------------------------------------------

BUILD_COST_COLUMNS = ["material", "qty", "buy/u", "build/u", "source", "cost", "surplus"]
# One row per material per product, with the job's own parameters repeated on every row so a
# spreadsheet can still slice one blueprint out of a run that priced several.
BUILD_COST_CSV_COLUMNS = ["product_id", "product_name", "blueprint_id", "activity", "runs", "me",
                          "component_me", "te", "units", "type_id", "material_name", "required",
                          "buy_unit", "buy_basis", "build_unit", "source", "forced", "cost", "surplus"]


def _percent(spec: float, flag: str) -> float:
    """A percent flag as the fraction `industry` multiplies by, converted exactly once.

    The model works in fractions because every formula it owns is a multiplication; the flag works in
    percent because nobody types "a quarter of one percent" as 0.0025. Converting here rather than
    deeper in leaves one place where a factor of 100 can go wrong, and the range check catches the
    other direction: somebody who read `--facility-tax 0.25` as "a quarter of the EIV" would otherwise
    be quoted an install fee forty times too small without a word of complaint."""
    if not 0.0 <= spec <= 100.0:
        raise RuntimeError(f"{flag} is a percentage between 0 and 100 - the NPC-station default of "
                           f"{industry.NPC_STATION_TAX * 100:g} means a quarter of one percent, "
                           f"got {spec:g}")
    return spec / 100.0


def build_scope(client: esi_mod.Esi, args) -> tuple[market.Scope, int]:
    """The one order book this run prices from, and the system whose index bills the install.

    `market` takes repeatable scopes because comparing two stations is exactly what a trader asks. A
    build cost is one shopping trip: every material comes off one book, so `--hub`/`--region` are
    single-valued here and asking for both is a contradiction rather than a wider net - there is no
    honest total to be made out of two stations' asks at once.

    The second value may not quietly default to Jita when the scope is a region. A region holds dozens
    of systems, their cost indices differ by more than the tax does, and picking one would print an
    invented fee inside somebody's total. Only a hub knows the system it stands in."""
    if args.hub and args.region:
        raise RuntimeError("--hub and --region each pick one scope; give only one of them - a build "
                           "cost is priced from a single order book")
    if args.region:
        region_id, name = market.resolve_region(client, args.region)
        scope = market.Scope(region_id, name)
    elif args.hub:
        scope = _hub_scope(args.hub)
    else:
        scope = _hub_scope("jita")
    if args.system:
        # Naming a system moves the install, not the shopping: ESI's order books are regional, and we
        # are not going to infer a region from a system in order to narrow them. So `--system Amarr`
        # with the default scope really does mean "buy at Jita, build in Amarr".
        return scope, market.resolve_system(client, args.system)[0]
    if scope.system_id is None:
        raise RuntimeError(f'--region has no single solar system, so there is no industry cost index '
                           f'to bill {scope.label} with - name one with --system, e.g. --system "Jita"')
    return scope, scope.system_id


def _recipe_index() -> dict[int, industry.Recipe]:
    """{product type id: Recipe} from the local SDE snapshot, read once per run.

    A missing snapshot is not a traceback and not an empty plan: `plan` answers the same situation
    with the one command that fixes it, and this command has no better advice to give."""
    try:
        document = alphadata.blueprint_materials()
    except FileNotFoundError:
        raise RuntimeError("no local blueprint data - run: eve-skills update-data") from None
    return industry.recipe_index(document)


def _recipe_for(index, type_id: int, name: str) -> industry.Recipe:
    """The blueprint that makes one requested product; refusal when none does.

    Most of what a player types here - ore, a shop-bought module, a ship nobody launches - has no
    blueprint at all. The honest answer is that the SDE knows no way to build it, said before a single
    order book is read, rather than a table of nothing bought at great expense."""
    recipe = index.get(type_id)
    if recipe is None:
        raise RuntimeError(f"no blueprint in the local SDE data makes {name} (type {type_id}) - only "
                           f"manufactured and reacted items have a build cost; for the newest "
                           f"blueprint list run: eve-skills update-data")
    return recipe


def _buildable(recipe: industry.Recipe, material_id: int, index) -> bool:
    """Whether some blueprint could make one direct material of `recipe`.

    This restates `industry._expandable`, which is private and cannot be asked, because the blanket
    `--build-all` has to know which materials it is promising: a recipe's ore is buildable by nobody,
    and forcing it would fail the whole run over a line nobody typed. A material that is the product
    itself is never expandable - building the item in order to build the item is a loop, not a plan."""
    if material_id == recipe.product_id:
        return False
    candidate = index.get(material_id)
    return candidate is not None and bool(candidate.materials)


def _forced_names(client: esi_mod.Esi, args) -> dict[int, str]:
    """`--build`/`--buy` as {type id: "build" | "buy"}, resolved before anything expensive is read.

    Being told a component's name was wrong after forty order books would be a bad afternoon."""
    force: dict[int, str] = {}
    for specs, choice in ((args.build, "build"), (args.buy, "buy")):
        for spec in specs or []:
            type_id = market.resolve_type(client, spec)[0]
            if type_id in force:
                raise RuntimeError(f"type {type_id} is named by both --build and --buy; one material "
                                   f"can be forced one way only")
            force[type_id] = choice
    return force


def _expand_force(force: dict[int, str], args, index, recipes, prices: industry.Prices) -> None:
    """The blanket flags, filled in from what is buildable and what anyone quotes.

    Done after the books are read because `--buy-all` needs to know which materials have a price:
    forcing a purchase ESI cannot price is refused by the model, and losing the whole report over a
    line nobody typed individually is a worse answer than leaving that row unpriced. `--build-all`
    skips what no blueprint makes for the same reason, but stops there - forcing a build whose own
    inputs are unpriceable genuinely cannot be honoured, so `industry.plan_build` says so in terms of
    the one type at fault instead of us guessing which half of the request to drop."""
    if args.build_all:
        for recipe in recipes:
            for material_id in recipe.materials:
                if _buildable(recipe, material_id, index):
                    force.setdefault(material_id, "build")
    if args.buy_all:
        for recipe in recipes:
            for material_id in recipe.materials:
                if prices.quote(material_id)[0] is not None:
                    force.setdefault(material_id, "buy")


@dataclass(frozen=True)
class BuildTarget:
    """One requested product, the job this run costed for it, and where that job would be installed.

    The facility travels with the target rather than with the run because the cost index is per
    activity: two products in one command can be a manufacturing job and a reaction, billed on two
    different numbers from the same `/industry/systems` page."""
    type_id: int
    name: str
    recipe: industry.Recipe
    plan: industry.BuildPlan
    facility: industry.Facility


@dataclass(frozen=True)
class BuildRun:
    """Everything one `build-cost` run decided, so all three output formats print one truth.

    Renderers get the whole run instead of a per-product slice because the notes are shared: scope,
    price basis, freshness and cost index describe every block equally, and a renderer that had to
    re-derive them would be free to disagree with the one that printed first."""
    now: float
    scope: market.Scope
    system_id: int
    facility_tax: float
    scc_surcharge: float
    material_multiplier: float
    indices_used: dict[str, float]
    index_meta: esi_mod.Meta
    figures: market.BookFigures
    reference: market.PriceTable
    names: dict[int, str]
    targets: tuple[BuildTarget, ...]
    rules: str | None
    warnings: tuple[str, ...]


def _esi_age(meta: esi_mod.Meta, now: float) -> str:
    """When ESI generated a document and how long ago, for payloads that are not order books.

    `market.freshness_line` is the right words for a book because it names the five-minute refresh.
    `/markets/prices` and `/industry/systems` move on their own schedules, so dating one of them with
    that cadence would stamp a number with someone else's clock."""
    if meta.last_modified is None:
        return "with no Last-Modified, so its age is unknown"
    return (f"generated {time.strftime('%H:%M:%SZ', time.gmtime(meta.last_modified))}, "
            f"{market.format_age(now - meta.last_modified)} ago")


def _build_preflight_notice(info: market.Preflight) -> str:
    """What the book fan-out is about to cost, said before it starts.

    Counts rather than a progress bar, in the register `exports` set for the same notice and at its
    measured per-book rate rather than a re-guessed one. A build costs more distinct types than an
    inventory does - every material plus every material of every component - so this is the one part
    of the wait a user cannot infer from the command they typed."""
    kinds = "" if info.types == 1 else "s"
    if not info.fetches:
        return (f"pricing {info.types} distinct type{kinds} for this build: every figure is already "
                f"in the local quote cache, so no order book is read")
    line = (f"pricing {info.types} distinct type{kinds} for this build: {info.fetches} order "
            f"book{'s' if info.fetches != 1 else ''} to read, one per type")
    if info.cached:
        line += f" ({info.cached} already priced from the last run)"
    seconds = info.fetches * exports.BOOK_SECONDS_PER_TYPE
    if seconds >= exports.NOTICE_MIN_SECONDS:
        line += f"; about {market.format_age(seconds)} at this size"
    return line


def _build_requests_note(figures: market.BookFigures) -> str:
    """What the price fan-out actually cost, so a cheap run and an expensive one look different."""
    if not figures.fetched:
        return (f"no order-book request: all {figures.cached} types came from the local quote cache, "
                f"which ESI's own expiry says is still current")
    line = f"{figures.fetched} order-book request{'s' if figures.fetched != 1 else ''} now"
    if figures.cached:
        line += f", {figures.cached} type{'s' if figures.cached != 1 else ''} from the cache"
    if figures.failed:
        line += f", {figures.failed} did not answer"
    return line


def _build_unit_charge(component: industry.Component) -> float | None:
    """What the build option would charge per unit of the material the recipe actually needs.

    Deliberately not `BuildQuote.unit`, which is per unit *produced*: a component blueprint yielding
    100 per run would print its batch price in the row beside the `buy` decision that beat it, and the
    table would read as though a cheap option had lost to an expensive one by mistake. Dividing the
    whole job's cost by the requirement is the comparison the plan itself made.

    None - a dash, never 0.00 - when there is no build option, or when that option has a material it
    cannot price: `industry` treats such a build as not chargeable and never lets it win, so any total
    printed for it would be missing an input."""
    if component.build is None or component.build.unpriced:
        return None
    return component.build.total / component.required


def _build_heading(target: BuildTarget) -> str:
    """One line saying what would be built, how much of it, and under what research."""
    plan, recipe = target.plan, target.recipe
    units = f"{plan.units:,} unit" + ("" if plan.units == 1 else "s")
    runs = f"{plan.runs} run" + ("" if plan.runs == 1 else "s")
    # A reaction has no research to speak of. Printing "ME 0 / TE 0" for one would imply the zeros were
    # choices the user made rather than a thing that does not exist.
    research = (f"at ME {plan.me} / TE {plan.te}" if recipe.researchable
                else "a reaction (no ME or TE to research)")
    # The component jobs take their own ME, and it is the level that decides most of the material
    # line - so it belongs on the heading beside the one people came to read. Only when this recipe
    # really has something to build: naming a level for a job that will not run is noise, and on a
    # fully-bought recipe it would be the number doing most of the talking for no reason at all.
    if any(component.build is not None for component in plan.components):
        research += f", components at ME {plan.component_me}"

    return (f"{target.name} (id {target.type_id}) - {units} from {runs}, {research}, "
            f"blueprint {recipe.blueprint_id}")


def _build_table_rows(run: BuildRun, target: BuildTarget) -> list[list[str]]:
    """One row per direct material: what it costs either way, and which way was charged."""
    return [[exports.name_or_id(run.names, component.type_id), f"{component.required:,}",
             _isk(component.buy_unit), _isk(_build_unit_charge(component)), component.source,
             _isk(component.cost), f"{component.surplus:,}"]
            for component in target.plan.components]


def _surplus_footnote(target: BuildTarget) -> str | None:
    """Why a build can cost more than the recipe asked for, whenever that is possible in this plan.

    Named only when some component's build option would leave surplus - including the ones that lost
    to buying, because a reader comparing the two columns is exactly the reader who needs to know that
    they are not like for like. An option with a material nobody priced is not counted: it prints a
    dash rather than a figure, so there would be nothing here to explain."""
    if not any(component.build and not component.build.unpriced and component.build.surplus
               for component in target.plan.components):
        return None
    return ("  A component job runs in whole runs, so the build column charges every run needed to\n"
            "  cover the quantity above: a blueprint that yields more than the recipe wants leaves\n"
            "  real surplus behind - product you own and could sell, not waste.")


def _totals_lines(run: BuildRun, target: BuildTarget) -> list[str]:
    """The money, with the install fee's arithmetic left visible instead of baked into one number."""
    plan, facility = target.plan, target.facility
    fee = (f"= EIV x ({facility.cost_index:.4f} cost index + {facility.facility_tax:.4f} facility "
           f"tax + {facility.scc_surcharge:.4f} SCC surcharge)")
    # The model withholds `cost_per_unit` exactly when a material has no price (see the docstring on
    # `industry.BuildPlan`), so the dash and the reason for it have to be printed together; the note
    # used to hang off the branch that prints a figure, where nothing can reach it.
    per_unit = ("-  (not stated while a material has no price - see the note below)"
                if plan.cost_per_unit is None else f"{_isk(plan.cost_per_unit)} ISK")
    return ["totals:",
            f"  material cost   {_isk(plan.material_cost)} ISK",
            f"  EIV             {_isk(plan.eiv)} ISK  (base quantities x ESI adjusted price; ME does "
            f"not reduce it)",
            f"  job cost        {_isk(plan.job_cost)} ISK  {fee}",
            f"  total           {_isk(plan.total)} ISK",
            f"  cost per unit   {per_unit}",
            f"  job time        {render.format_duration(plan.time)}"]


def _comparison_lines(run: BuildRun, target: BuildTarget) -> list[str]:
    """The same output bought instead of built, from the books this run already read.

    A build cost is only worth computing because of the decision it feeds, and the decision needs the
    alternative: 19.5 M ISK for a Hound means nothing until it is next to what one costs on the wing.
    Both sides come out of the one `BookFigures` fetched for the materials, so the comparison cannot
    be quoted from a different minute than the total."""
    plan = target.plan
    ask = run.figures.min_sell.get(target.type_id)
    bid = run.figures.max_buy.get(target.type_id)
    units = f"{plan.units:,} unit" + ("" if plan.units == 1 else "s")
    if ask is None and bid is None:
        return [f"buy instead: nobody is quoting {target.name} at {run.scope.label}, so this build "
                f"cost has nothing to be compared with - the total above stands alone"]
    lines = [f"buy instead: cheapest ask {_isk(ask)} ISK, richest bid {_isk(bid)} ISK at "
             f"{run.scope.label}"]
    if ask is None:
        return lines + ["  nobody is selling it there, so buying cannot be priced; the build total is "
                        "the only side of this you could act on"]
    buy = ask * plan.units
    if plan.cost_per_unit is None:
        return lines + [f"  the same {units} would cost {_isk(buy)} ISK to buy against an incomplete "
                        f"build total - with a material unpriced the two are not comparable"]
    winner, by = (("building", buy - plan.total) if buy >= plan.total
                  else ("buying", plan.total - buy))
    return lines + [f"  {winner} is cheaper by {_isk(by)} ISK for {units} "
                    f"(build {_isk(plan.total)} vs buy {_isk(buy)})"]


def _build_product_notes(run: BuildRun, target: BuildTarget) -> list[str]:
    """What this product's totals left out, or left a choice about.

    `alternatives` is a note and not an error because the SDE lists several blueprints for some
    products with no way to know which one is in the hangar: the run picked one and says how many
    others existed rather than pretending there was only ever one."""
    recipe, plan = target.recipe, target.plan
    notes = []
    if recipe.alternatives:
        others = ", ".join(str(blueprint_id) for blueprint_id in recipe.alternatives)
        count = len(recipe.alternatives)
        notes.append(f"blueprint {recipe.blueprint_id} was costed; {count} other "
                     f"blueprint{'s' if count != 1 else ''} make this product too ({others}) - your "
                     f"own research levels and rig bonuses can make a different one cheaper")
    if plan.unpriced:
        names = ", ".join(exports.name_or_id(run.names, ident) for ident in plan.unpriced)
        notes.append(f"{names}: no sell order there and no published price, so excluded from every "
                     f"total above rather than counted as free")
    if plan.eiv_missing:
        names = ", ".join(exports.name_or_id(run.names, ident) for ident in plan.eiv_missing)
        notes.append(f"{names}: absent from EIV only (ESI's price document has no adjusted price for "
                     f"it), so the install fee understates its share; its material cost is still in "
                     f"the total")
    return notes


def _build_run_notes(run: BuildRun) -> list[str]:
    """What the numbers are, how old they are, and what they were assembled from.

    Printed once per run rather than under every product, because scope, price basis, freshness and
    cost index describe all of the blocks equally."""
    region = run.names.get(run.scope.region_id) or f"region {run.scope.region_id}"
    lines = [f"scope: {run.scope.label} - every material priced off one order book: {region}",
             "price basis: the cheapest standing ask there, falling back to ESI's published industry "
             "reference where no order exists; a published figure is not something you can buy at"]
    if run.material_multiplier != 1.0:
        lines.append(f"material multiplier {run.material_multiplier:g} applied to every requirement - "
                     f"an aggregate rig or structure bonus, so the quantities above are below the "
                     f"blueprint's own")
    if run.figures.answered:
        # `freshness_line` labels its own failure case, and a line that began "order books:
        # freshness:" would read as a typo rather than as ESI having answered without a date.
        age = market.freshness_line(run.figures.meta, run.now).removeprefix("freshness: ")
        lines.append(f"order books: {age}")
    else:
        lines.append("order books: none answered - every figure below came from ESI's published "
                     "reference or is missing")
    lines.append(f"ESI price document: {_esi_age(run.reference.meta, run.now)}")
    system = run.names.get(run.system_id) or f"system {run.system_id}"
    for activity, value in sorted(run.indices_used.items()):
        lines.append(f"cost index: {activity} {value:.4f} in {system} ({run.system_id}); "
                     f"/industry/systems {_esi_age(run.index_meta, run.now)}")
    if run.rules:
        lines.append(f"note: {run.rules}")
    lines.append(f"requests: {_build_requests_note(run.figures)}")
    return lines


def build_cost_text(run: BuildRun) -> str:
    """One block per product, then the notes that describe the whole run."""
    blocks = []
    for target in run.targets:
        lines = [_build_heading(target),
                 render.table(BUILD_COST_COLUMNS, _build_table_rows(run, target))]
        footnote = _surplus_footnote(target)
        if footnote:
            lines.append(footnote)
        lines += _totals_lines(run, target)
        lines += _comparison_lines(run, target)
        lines += [f"  {line}" for line in _build_product_notes(run, target)]
        blocks.append("\n".join(lines))
    blocks.append("\n".join(_build_run_notes(run)))
    return "\n\n".join(blocks)


def _typed_docs(ids, names: dict[int, str]) -> list[dict]:
    """Unpriced types as ids with their names beside them, so a script needs no second lookup."""
    return [{"type_id": ident, "name": names.get(ident) or f"type {ident}"} for ident in ids]


def _component_doc(component: industry.Component, names: dict[int, str]) -> dict:
    """One material row, including the option that lost.

    `build.unit` is per unit *produced*, which is not what the table prints: the human column charges
    `build.total / required`, because a component blueprint that yields 100 per run has to be compared
    against the requirement, not against one of its own outputs. Both numbers are here, so a script
    can see why a build lost without having to know that."""
    build = component.build
    return {"type_id": component.type_id, "name": names.get(component.type_id) or f"type {component.type_id}",
            "required": component.required, "source": component.source,
            "forced": component.forced, "unit_cost": component.unit_cost, "cost": component.cost,
            "surplus": component.surplus,
            "buy": {"unit": component.buy_unit, "basis": component.buy_basis},
            "build": None if build is None else {
                "blueprint_id": build.recipe.blueprint_id, "runs": build.runs, "units": build.units,
                "surplus": build.surplus, "unit": build.unit, "material_cost": build.material_cost,
                "job_cost": build.job_cost, "eiv": build.eiv, "total": build.total,
                "unpriced": _typed_docs(build.unpriced, names)}}


def build_cost_json(run: BuildRun) -> dict:
    """Machine-readable output: raw numerics, ISO-Z stamps, ids and names together.

    The prose notes are not repeated here - their content is in `unpriced`, `eiv_missing`, `market`
    and the freshness fields - but `warnings` keeps the caveats that qualify the totals themselves,
    because a parser that sums `total` cannot otherwise tell an incomplete one from a finished one."""
    figures = run.figures
    return {
        "generated": market.iso_utc(run.now),
        "scope": {"label": run.scope.label, "region_id": run.scope.region_id,
                  "region_name": run.names.get(run.scope.region_id),
                  "system_id": run.scope.system_id, "location_id": run.scope.location_id},
        "job": {"system_id": run.system_id, "system": run.names.get(run.system_id),
                "activity_indices_used": dict(sorted(run.indices_used.items())),
                "facility_tax": run.facility_tax, "scc_surcharge": run.scc_surcharge,
                "material_multiplier": run.material_multiplier},
        "products": [{
            "product_id": target.type_id, "name": target.name,
            "blueprint_id": target.recipe.blueprint_id, "activity": target.recipe.activity,
            "alternatives": list(target.recipe.alternatives),
            "runs": target.plan.runs, "me": target.plan.me, "te": target.plan.te,
            "component_me": target.plan.component_me, "units": target.plan.units,
            "materials": [_component_doc(component, run.names)
                          for component in target.plan.components],
            "material_cost": target.plan.material_cost, "eiv": target.plan.eiv,
            "job_cost": target.plan.job_cost, "total": target.plan.total,
            "cost_per_unit": target.plan.cost_per_unit, "time_seconds": target.plan.time,
            "market": {"min_sell": figures.min_sell.get(target.type_id),
                       "max_buy": figures.max_buy.get(target.type_id)},
            "unpriced": _typed_docs(target.plan.unpriced, run.names),
            "eiv_missing": _typed_docs(target.plan.eiv_missing, run.names),
        } for target in run.targets],
        "figures": {"fetched": figures.fetched, "cached": figures.cached, "failed": figures.failed,
                    "last_modified": market.iso_utc(figures.meta.last_modified),
                    "expires": market.iso_utc(figures.meta.expires),
                    "age_seconds": None if figures.meta.last_modified is None
                    else round(run.now - figures.meta.last_modified, 1)},
        "warnings": list(run.warnings),
    }


def build_cost_csv(run: BuildRun) -> None:
    """Flat material rows on stdout; the notes go to stderr like every other CSV here.

    A name a run could not resolve is an empty cell rather than `id 34`, so that the column means one
    thing to whatever reads this afterwards."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(BUILD_COST_CSV_COLUMNS)
    for target in run.targets:
        plan = target.plan
        for component in plan.components:
            writer.writerow([target.type_id, target.name, target.recipe.blueprint_id,
                             target.recipe.activity, plan.runs, plan.me, plan.component_me, plan.te,
                             plan.units, component.type_id, run.names.get(component.type_id) or "",
                             component.required, _raw(component.buy_unit), _raw(component.buy_basis),
                             _raw(_build_unit_charge(component)), component.source,
                             _raw(component.forced), _raw(component.cost), component.surplus])
    sys.stdout.write(buf.getvalue())


def _build_notes_for_csv(run: BuildRun) -> list[str]:
    """Every prose line this run has to say, for the output format that may not print them as rows."""
    lines = list(_build_run_notes(run))
    for target in run.targets:
        lines += [f"{target.name}: {line}" for line in _build_product_notes(run, target)]
    return lines


def cmd_build_cost(args):
    """ISK cost of manufacturing an item from its blueprint, priced from live orders (public ESI)."""
    if args.build_all and args.buy_all:
        raise RuntimeError("--build-all and --buy-all push every material of the recipe in opposite "
                           "directions; pick one and override the few you mean with --build/--buy")
    facility_tax = _percent(args.facility_tax, "--facility-tax")
    # The caps `plan_build` enforces anyway, checked against its own constants before a single order
    # book is read: refusing ME 11 after twenty-six books have already cost their time is a bad
    # afternoon, and these limits belong to the flags rather than to whichever blueprint turns out to
    # be involved. A blueprint's own maximum runs per install still has to wait for the recipe.
    if args.runs < 1:
        raise RuntimeError(f"--runs is the number of installs in one job, so it has to be at least 1; "
                           f"got {args.runs}")
    if not 0 <= args.me <= industry.MAX_ME:
        raise RuntimeError(f"--me must be 0..{industry.MAX_ME} - that is as far as a blueprint can be "
                           f"researched, got {args.me}")
    if args.component_me is not None and not 0 <= args.component_me <= industry.MAX_ME:
        raise RuntimeError(f"--component-me must be 0..{industry.MAX_ME} - that is as far as a "
                           f"component blueprint can be researched, got {args.component_me}")
    if not 0 <= args.te <= industry.MAX_TE:
        raise RuntimeError(f"--te must be 0..{industry.MAX_TE} - that is as far as a blueprint can be "
                           f"researched, got {args.te}")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    scope, system_id = build_scope(client, args)
    # Explicit forces are resolved before any book is read: being told a component's name was wrong
    # after forty order books would be a bad afternoon.
    force = _forced_names(client, args)
    index = _recipe_index()
    types = list(dict.fromkeys(market.resolve_type(client, spec) for spec in args.type))
    by_id = dict(types)
    recipes = [_recipe_for(index, type_id, name) for type_id, name in types]
    # The per-blueprint install limit is checked here rather than left to `plan_build`, which only
    # sees it after the prices it was handed: `--runs 2` on a one-run BPC is refusable knowledge the
    # moment the recipe is known, and paying for a fan-out first would buy nothing but the same error.
    for recipe in recipes:
        if 0 < recipe.max_runs < args.runs:
            raise RuntimeError(f"blueprint {recipe.blueprint_id} allows at most {recipe.max_runs} "
                               f"runs per install, asked for {args.runs} - install it as "
                               f"{-(-args.runs // recipe.max_runs)} jobs instead")
    # One fan-out covers the whole run: `pricing_ids` is exactly what `plan_build` will be asked to
    # price - product, direct materials, and one level below that - so no book is read for a figure no
    # total uses, and ten products cost one pass over the union rather than ten passes.
    wanted: set[int] = set()
    for recipe in recipes:
        wanted |= industry.pricing_ids(recipe, index)
    machine = bool(args.json or args.csv)

    def announce(info: market.Preflight) -> None:
        """What the fan-out is about to cost, said before it starts and never into a machine pipe."""
        if not machine:
            print(_build_preflight_notice(info), file=sys.stderr)

    figures = market.book_figures(client, sorted(wanted), scope, preflight=announce)
    # `/markets/prices` is not optional here the way it is for `market`: the install fee is charged on
    # EIV, and EIV is CCP's `adjusted_price`, which no order book carries. One request covers every
    # type in the cluster, so asking unconditionally is cheaper than arguing about who needs it.
    price_doc = market.price_table(client)
    adjusted: dict[int, float] = {}
    for type_id in wanted:
        row = price_doc.reference(type_id)
        # A published 0.0 stays a price - PLEX really is quoted at zero - and only absence means "no
        # basis", which is how `Prices.quote` comes to distinguish the two statements.
        if row is not None and row.adjusted_price is not None:
            adjusted[type_id] = row.adjusted_price
    prices = industry.Prices(unit=figures.min_sell, adjusted=adjusted)
    _expand_force(force, args, index, recipes, prices)

    indices = industry.cost_indices(client)
    # Names for everything a table or footnote can point at: the priced types, the system whose index
    # bills the job, and the scope's own region and station, which appear nowhere else.
    named = {system_id, scope.region_id, scope.location_id} | wanted
    names = esi_mod.resolve_names(client, {ident for ident in named if ident is not None})

    targets: list[BuildTarget] = []
    indices_used: dict[str, float] = {}
    for recipe in recipes:
        cost_index = indices.index(system_id, recipe.activity)
        if cost_index is None:
            raise RuntimeError(
                f"ESI publishes no {recipe.activity} cost index for system {system_id} "
                f"({names.get(system_id) or 'unnamed'}) - the install fee would come out as just the "
                f"tax and surcharge, which is not a cost. Name a system that has one with --system.")
        indices_used[recipe.activity] = cost_index
        facility = industry.Facility(cost_index=cost_index, facility_tax=facility_tax,
                                     material_multiplier=args.material_multiplier)
        plan = industry.plan_build(recipe, index, prices, facility, runs=args.runs, me=args.me,
                                   te=args.te, component_me=args.component_me, force=force)
        targets.append(BuildTarget(type_id=recipe.product_id, name=by_id[recipe.product_id],
                                   recipe=recipe, plan=plan, facility=facility))

    warnings = [_build_requests_note(figures)]
    rules = industry.rules_warning()
    if rules:
        warnings.append(rules)
    if figures.failed:
        warnings.append(f"{figures.failed} order book{'s' if figures.failed != 1 else ''} did not "
                        f"answer; their types are counted as unpriced, not as worthless")
    for target in targets:
        if target.plan.unpriced:
            warnings.append(f"{target.name}: {len(target.plan.unpriced)} material(s) with no price "
                            f"are excluded from every total rather than counted as free")
    run = BuildRun(now=client.now().timestamp(), scope=scope, system_id=system_id,
                   facility_tax=facility_tax, scc_surcharge=industry.SCC_SURCHARGE,
                   material_multiplier=args.material_multiplier, indices_used=indices_used,
                   index_meta=indices.meta, figures=figures, reference=price_doc, names=names,
                   targets=tuple(targets), rules=rules, warnings=tuple(warnings))
    if args.json:
        print(json.dumps(build_cost_json(run), indent=2))
    elif args.csv:
        build_cost_csv(run)
        for line in _build_notes_for_csv(run):   # the footnotes matter; they may not pollute a pipe
            print(line, file=sys.stderr)
    else:
        print(build_cost_text(run))



ORDERS_OPEN_COLUMNS = ["owner", "type", "side", "price", "remaining/total", "filled", "station",
                       "region", "issued", "expires in", "escrow"]
# Only corporation rows can fill these, so they appear with --corp and nowhere else.
ORDERS_CORP_COLUMNS = ["division", "issued by"]
ORDERS_CLOSED_COLUMNS = ["owner", "type", "side", "price", "state", "filled/total", "station",
                         "region", "issued", "expires"]
ORDERS_CSV_COLUMNS = ["order_id", "owner_key", "owner_name", "is_buy", "is_corporation", "type_id",
                      "type_name", "region_id", "region_name", "location_id", "location_name",
                      "price", "volume_total", "volume_remain", "filled", "state", "issued",
                      "duration", "expires", "escrow", "min_volume", "range", "wallet_division",
                      "issued_by", "issued_by_name"]


def _stamp(value) -> str:
    """An ESI timestamp as a compact date; "-" when there is none to show."""
    moment = render.parse_opt(value)
    return "-" if moment is None else moment.strftime("%b %d %H:%M")


def order_doc(order, names: dict[int, str]) -> dict:
    """One order as machine-readable data: raw ids *and* their names, numbers unformatted."""
    return {
        "order_id": order.order_id,
        "owner_key": order.owner_key,
        "owner_name": order.owner_name,
        "is_buy": order.is_buy,
        "is_corporation": order.is_corporation,
        "type_id": order.type_id,
        "type_name": names.get(order.type_id),
        "region_id": order.region_id,
        "region_name": names.get(order.region_id),
        "location_id": order.location_id,
        "location_name": names.get(order.location_id),
        "price": order.price,
        "volume_total": order.volume_total,
        "volume_remain": order.volume_remain,
        "filled": order.filled,
        "state": order.state,
        "issued": order.issued,
        "duration": order.duration,
        "expires": order.expires,
        "escrow": order.escrow,
        "min_volume": order.min_volume,
        "range": order.range,
        "wallet_division": order.wallet_division,
        "issued_by": order.issued_by,
        "issued_by_name": names.get(order.issued_by) if order.issued_by else None,
    }


def orders_row(order, names: dict[int, str], now, *, closed: bool, corp: bool) -> list[str]:
    """One table row; `closed` switches to the history columns, `corp` adds the two only
    corporation rows can fill."""
    # A personal book can hold an order funded from the corporation wallet; without the marker its
    # ISK reads as the member's own. Under --corp every row is the corporation's, so it says nothing.
    side = "buy" if order.is_buy else "sell"
    row = [order.owner_name, exports.name_or_id(names, order.type_id),
           f"{side} (corp)" if order.is_corporation and not corp else side, _isk(order.price)]
    if closed:
        row += [order.state, f"{order.filled:,}/{order.volume_total:,}"]
    else:
        pct = "-" if not order.volume_total else f"{order.filled / order.volume_total * 100:.0f}%"
        row += [f"{order.volume_remain:,}/{order.volume_total:,}", pct]
    row += [exports.name_or_id(names, order.location_id), exports.name_or_id(names, order.region_id),
            _stamp(order.issued)]
    if closed:
        row.append(_stamp(order.expires))
    else:
        # An open order past its expiry is ESI lagging behind the market, not a negative countdown.
        expires = render.parse_opt(order.expires)
        if expires is None:
            left = "-"
        elif expires <= now:
            left = "due"
        else:
            left = render.format_duration((expires - now).total_seconds())
        row += [left, _isk(order.escrow)]
    if corp:
        row += [_raw(order.wallet_division), exports.name_or_id(names, order.issued_by)]
    return row


def orders_totals(items) -> dict:
    """ISK at stake in the live book: what the remaining sells would raise, and what the buys hold.

    Escrow is all ESI offers for a buy order's cost and it is optional, so `buy_escrow_missing`
    counts the buys that reported none - the total then says what it leaves out."""
    sells = [o for o in items if not o.is_buy]
    buys = [o for o in items if o.is_buy]
    return {"sell_orders": len(sells),
            "sell_isk": sum(o.price * o.volume_remain for o in sells),
            "buy_orders": len(buys),
            "buy_escrow_isk": sum(o.escrow or 0.0 for o in buys),
            "buy_escrow_missing": sum(1 for o in buys if o.escrow is None)}


def orders_totals_line(totals: dict) -> str:
    unreported = ("" if not totals["buy_escrow_missing"]
                  else f", escrow reported for {totals['buy_orders'] - totals['buy_escrow_missing']}")
    return (f"sell book {_isk(totals['sell_isk'])} ISK ({totals['sell_orders']} order(s))   "
            f"buy escrow {_isk(totals['buy_escrow_isk'])} ISK ({totals['buy_orders']} order(s){unreported})")


def order_owners(client: esi_mod.Esi, args, chars) -> tuple[list, list[str]]:
    """One OwnerOrders per owner to show, plus a warning line per owner ESI did not answer for.

    Corporation books are fetched once per corporation: stored characters can be colleagues, and a
    second token would report the identical rows a second time as if they were new."""
    owners, failures, fetched = [], [], set()
    for tok, public in chars:
        if args.corp:
            corp_id = exports.corp_of(public)
            if corp_id in fetched:
                continue
            if corp_id:
                fetched.add(corp_id)
        cname = public.get("name") or tok.get("character_name") or str(tok["character_id"])
        try:
            owners.append(orders.fetch_corporation(client, tok, public) if args.corp
                          else orders.fetch_character(client, tok))
        except (esi_mod.EsiError, RuntimeError) as err:
            # One character's refusal never hides the others' books; orders.py keeps the owner out
            # of its messages precisely so the reporter can name it in its own words.
            failures.append(f"{cname}: {err}")
    return owners, failures


def orders_side_filter(args) -> bool | None:
    """True for buys only, False for sells only, None for the whole book (both flags = no filter)."""
    if args.buy and not args.sell:
        return True
    if args.sell and not args.buy:
        return False
    return None


def cmd_orders(args):
    """The order book of every stored character that consented - or of their corporations."""
    if args.watch:
        return cmd_orders_watch(args)
    if args.limit is not None and args.limit < 1:
        raise RuntimeError("--limit needs a positive number of rows")
    feature = orders.CORPORATION_FEATURE if args.corp else orders.CHARACTER_FEATURE
    scope = orders.CORPORATION_SCOPE if args.corp else orders.CHARACTER_SCOPE
    client, chars, hints = exports.targets(args, [(feature, scope)])
    owners, failures = order_owners(client, args, chars)
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    type_id = market.resolve_type(client, args.type)[0] if args.type else None
    side = orders_side_filter(args)
    book = [o for owner in owners for o in (owner.history if args.closed else owner.open)
            if (type_id is None or o.type_id == type_id) and (side is None or o.is_buy == side)]
    # Newest first - ESI's own order is oldest-first, the opposite of what a glance at one's own
    # orders looks for. Its UTC stamps sort correctly as written.
    book.sort(key=lambda o: o.issued, reverse=True)
    shown = book[:args.limit] if args.limit else book
    ids = {i for o in shown for i in (o.type_id, o.region_id, o.location_id, o.issued_by) if i}
    names = esi_mod.resolve_names(client, ids) if ids else {}
    # --csv sends the hint lines to stderr inside targets(); --json needs the same treatment here
    # so that stdout stays parseable either way.
    if args.json:
        for line in hints:
            print(line, file=sys.stderr)
        print(json.dumps({
            "generated": market.iso_utc(client.now().timestamp()),
            "owner_kind": "corporation" if args.corp else "character",
            "book": "closed" if args.closed else "open",
            "filters": {"type_id": type_id,
                        "side": None if side is None else ("buy" if side else "sell")},
            "owners": [{"owner_key": o.owner_key, "owner_name": o.owner_name,
                        "history_ok": o.history_ok} for o in owners],
            "matched": len(book),
            "shown": len(shown),
            "orders": [order_doc(o, names) for o in shown],
            # A closed book has nothing still at stake; the derived states are the answer there.
            "totals": None if args.closed else orders_totals(book),
        }, indent=2))
        return
    if args.csv:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(ORDERS_CSV_COLUMNS)
        for order in shown:
            doc = order_doc(order, names)
            # bool as 1/0 like every other CSV in this tool.
            writer.writerow(["" if doc[c] is None else
                             (int(doc[c]) if c in ("is_buy", "is_corporation") else doc[c])
                             for c in ORDERS_CSV_COLUMNS])
        sys.stdout.write(buf.getvalue())
        return
    blocks = list(hints)
    columns = (ORDERS_CLOSED_COLUMNS if args.closed
               else ORDERS_OPEN_COLUMNS + (ORDERS_CORP_COLUMNS if args.corp else []))
    now = client.now()
    if not shown:
        blocks.append("(no closed orders in ESI's ~90-day history)" if args.closed
                      else "(no open orders)")
    else:
        rows = [orders_row(o, names, now, closed=args.closed, corp=args.corp) for o in shown]
        lines = [render.table(columns, rows)]
        if args.closed:
            lines += ["", "* derived state: ESI has no filled value, so an expired order with nothing "
                          "left is shown as filled and one with volume left as expired.",
                      "* ESI has no cancellation timestamp, so a cancelled order's expires is the date "
                      "it would have run to."]
        else:
            if len(book) > len(shown):
                lines.append(f"{len(book) - len(shown)} of {len(book)} matching order(s) hidden by "
                             f"--limit; the totals cover all of them")
            lines += ["", orders_totals_line(orders_totals(book))]
        if not args.corp and any(o.is_corporation for o in shown):
            lines += ["", "* (corp) marks an order funded from a corporation wallet; that ISK is not "
                          "the character's own."]
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))


@dataclass(frozen=True)
class OrderPoll:
    """One order poll: the books ESI answered, the warnings that stand in for the ones it did not,
    and whether any stored character could be asked at all."""
    books: tuple = ()
    warnings: tuple = ()
    eligible: bool = True


def orders_watch_hint(args) -> str:
    """What to tell a watcher when nobody can be polled - and how to fix it."""
    feature = orders.CORPORATION_FEATURE if getattr(args, "corp", False) else orders.CHARACTER_FEATURE
    return (f"no stored character has the {feature} consent - run: eve-skills login --scopes {feature}"
            " (pick the character in the browser)")


def poll_order_observations(args, client: esi_mod.Esi, warned: set) -> OrderPoll:
    """Every owner this cycle can be asked for orders, plus a warning line per owner it could not.

    Deliberately quieter than the one-shot command: missing consent is a standing fact that `orders`
    explains properly, so a watcher only says so when nothing at all can be polled. ESI's own refusals
    (no role, revoked consent) are remembered for the session - they need a browser re-auth or an
    in-game role change, not a faster poll - while a transport failure is warned about every cycle and
    retried, because the next one may well work. A corporation book is fetched once per corporation:
    stored characters can be colleagues, and a second token would report identical rows as if new.
    """
    personal = not getattr(args, "corp", False)
    wanted = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    books, warnings, asked = [], [], set()
    eligible = False
    for rec in sso.list_characters():
        cid = int(rec["character_id"])
        if wanted is not None and cid != wanted:
            continue
        tok = sso.get_access_token(cid)
        name = tok.get("character_name") or str(cid)
        if personal and sso.has_feature(tok, orders.CHARACTER_FEATURE):
            eligible = True
            try:
                books.append(orders.fetch_character(client, tok))
            except orders.OrderAccess as err:
                warnings += warn_once(warned, orders.owner_key("char", cid), f"{name}: {err}")
            except (esi_mod.EsiError, RuntimeError) as err:
                warnings.append(f"{name}: {err}")
        if not sso.has_feature(tok, orders.CORPORATION_FEATURE):
            continue
        try:
            public = client.get(f"/characters/{cid}")
        except esi_mod.EsiError as err:
            warnings.append(f"{name}: {err}")
            continue
        corp_id = exports.corp_of(public)
        if not corp_id or corp_id in asked:
            continue
        eligible = True
        try:
            books.append(orders.fetch_corporation(client, tok, public))
        except orders.OrderAccess as err:
            # Keyed by corporation *and* token: a colleague's token may hold the Accountant role this
            # one lacks, so one refusal must not silence that corporation for everybody else. The corp
            # is only marked as fetched once a book actually arrived, so the next token still gets its
            # turn - and two tokens that both work never report the same rows twice.
            warnings += warn_once(warned, f"{orders.owner_key('corp', corp_id)}@{cid}", f"{name}: {err}")
        except (esi_mod.EsiError, RuntimeError) as err:
            warnings.append(f"{name}: {err}")
        else:
            asked.add(corp_id)
    return OrderPoll(tuple(books), tuple(warnings), eligible)


def order_names(client: esi_mod.Esi, books) -> dict[int, str]:
    """Type and station names for every order in these books, resolved once per cycle.

    ESI order rows carry ids only. `resolve_names` is disk-cached and leaves out whatever it cannot
    resolve, so a private citadel still shows as an id instead of failing the whole poll. The same map
    goes to watchstate (which reads type ids out of it) and to the sentences (which add stations)."""
    ids = {int(ident) for book in books for row in (*book.open, *book.history)
           for ident in (row.type_id, row.location_id) if ident}
    return esi_mod.resolve_names(client, ids) if ids else {}


ORDERS_WATCH_COLUMNS = ["owner", "open", "sell/buy", "sell book ISK", "buy escrow ISK",
                        "least filled", "most filled", "next expiry"]


def _order_progress(order, names: dict[int, str]) -> str:
    """`45% Tritanium` - the order named, so the row says what to go and look at."""
    pct = "-" if not order.volume_total else f"{order.filled / order.volume_total * 100:.0f}%"
    return f"{pct} {exports.name_or_id(names, order.type_id)}"


def render_orders_watch_status(books, names: dict[int, str], now) -> str:
    """Per-owner dashboard for `orders --watch`: what is at stake, and which order wants attention.

    Only the live book is tabulated - closed orders are what the announcements above the table are
    for, and a screen meant to be read at a glance cannot also be a report."""
    rows, escrow_missing = [], 0
    for book in books:
        items = list(book.open)
        totals = orders_totals(items)
        sells = [o for o in items if not o.is_buy]
        buys = [o for o in items if o.is_buy]
        escrow_missing += totals["buy_escrow_missing"]
        # An open order past its expiry is ESI lagging behind the market, not a negative countdown.
        dated = sorted((o for o in items if o.expires), key=lambda o: o.expires)
        if dated:
            expires = render.parse_opt(dated[0].expires)
            left = "due" if expires <= now else f"{render.format_duration((expires - now).total_seconds())} left"
        else:
            left = "-"
        # A volume-less order counts as fully filled rather than dividing by zero.
        ranked = sorted(items, key=lambda o: (o.filled / o.volume_total) if o.volume_total else 1.0)
        rows.append([book.owner_name, str(len(items)), f"{len(sells)}/{len(buys)}",
                     _isk(totals["sell_isk"]), _isk(totals["buy_escrow_isk"]),
                     _order_progress(ranked[0], names) if ranked else "-",
                     _order_progress(ranked[-1], names) if ranked else "-", left])
    out = [render.table(ORDERS_WATCH_COLUMNS, rows)] if rows else ["(no order book could be polled this cycle)"]
    for book in books:
        if not book.history_ok:
            # The open book is still trustworthy; only the *reason* an order left it is missing, and
            # guessing at it would be worse than saying so - watchstate holds those closures back.
            out.append(f"! {book.owner_name}: order history unreadable this cycle - closures are held "
                       f"back until ESI answers that call again")
    if escrow_missing:
        out.append(f"* {escrow_missing} buy order(s) reported no escrow; the escrow total leaves them out.")
    return "\n".join(out)


def orders_watch_cycle(args, client: esi_mod.Esi, session: WatchSession) -> WatchCycle:
    """One `orders --watch` turn: poll every consenting owner and claim what changed."""
    poll = poll_order_observations(args, client, session.warned)
    names = order_names(client, poll.books)
    warnings = list(poll.warnings)
    if not poll.eligible:
        # Once per run, not once per cycle: nothing improves until a login happens, and shouting
        # about it every five minutes would bury the events this process exists to surface.
        warnings += warn_once(session.warned, "orders:no-consent", orders_watch_hint(args))
    return WatchCycle(title="orders watch", warnings=tuple(warnings),
                      order_observations=tuple(watchstate.OrderObservation.from_book(b, names)
                                               for b in poll.books),
                      body=render_orders_watch_status(poll.books, names, client.now()), names=names)


def cmd_orders_watch(args):
    """`orders --watch`: keep the live book on screen and announce orders that leave it."""
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    session = WatchSession()
    return watch_loop(args, lambda: orders_watch_cycle(args, client, session))


def parse_target(spec: str) -> tuple[str, int]:
    name, sep, lvl = spec.rpartition(":")
    if sep and lvl.isdigit() and 1 <= int(lvl) <= 5 and name:
        return name.strip(), int(lvl)
    return spec.strip(), 5


def load_skill_catalog() -> dict[int, alphadata.SkillInfo]:
    """The SDE skill catalog, with a message saying what to do when it is not installed."""
    try:
        return alphadata.skill_catalog()
    except FileNotFoundError:
        raise RuntimeError("no local skill catalog - run: eve-skills update-data") from None


def resolve_skill_id(name: str, catalog: dict[int, alphadata.SkillInfo]) -> int:
    """Resolve a target against the whole catalog, so never-trained skills work too."""
    needle = name.strip().lower()
    exact = [s for s, info in catalog.items() if info.name.lower() == needle]
    matches = exact or [s for s, info in catalog.items() if needle in info.name.lower()]
    if not matches:
        raise RuntimeError(f"no skill named '{name}' in the local skill catalog "
                           f"({len(catalog)} skills) - refresh it with: eve-skills update-data")
    if len(matches) > 1 and exact:
        matches = [s for s in exact if catalog[s].published] or exact
    if len(matches) > 1:
        shown = ", ".join(sorted(catalog[s].name for s in matches)[:6])
        raise RuntimeError(f"'{name}' is ambiguous: {shown}")
    return matches[0]


def cmd_plan(args):
    if not args.char and len(sso.list_characters()) > 1:
        raise RuntimeError("plan needs --char (one character at a time)")
    ctx = gather(args)
    catalog = load_skill_catalog()
    targets: dict[int, int] = {}
    for spec in args.target:
        name, level = parse_target(spec)
        sid = resolve_skill_id(name, catalog)
        targets[sid] = max(targets.get(sid, 0), level)  # a repeated target means its deepest level
    plan = planner.build_plan(targets, catalog, {r.skill_id: r.trained for r in ctx["rows"]},
                              planner.scheduled_levels(ctx["queue"]))
    if args.rate is not None and args.rate <= 0:
        raise RuntimeError(f"--rate must be a positive SP/hour value, not {args.rate:g}")
    rate, rate_src = (float(args.rate), "--rate override") if args.rate is not None else planner.calibrated_rate(ctx)

    # New items can only start once everything already queued has finished.
    eta = ctx["now"]
    pending = [render.parse_ts(q["finish_date"]) for q in ctx["queue"]
               if q.get("finish_date") and queue_status(q, eta) != "done"]
    backlog_end = max(pending, default=None)
    if backlog_end and backlog_end > eta:
        eta = backlog_end

    name = ctx["public"].get("name") or ctx["token"]["character_id"]
    print(f"{name}: {len(plan.items)} item(s) to train"
          + (f", {len(plan.covered)} already covered" if plan.covered else ""))
    rows, notes = [], []
    for item in plan.items:
        hours = item.sp / rate
        eta += timedelta(hours=hours)
        why = "requested" if item.requested else "for " + ", ".join(item.required_by[:2]) + (
            f" +{len(item.required_by) - 2}" if len(item.required_by) > 2 else "")
        rows.append([item.name, why, f"L{item.from_level}" + ("*" if item.from_level > item.trained_level else ""),
                     f"L{item.to_level}", render.format_sp(item.sp), render.format_duration(hours * 3600),
                     eta.strftime("%b %d %H:%M")])
        cap = ctx["caps"].get(item.skill_id)
        if cap is None:
            notes.append(f"{item.name} is omega-only (absent from the alpha list)")
        elif item.to_level > cap:
            notes.append(f"{item.name} target L{item.to_level} exceeds its alpha cap {cap} - needs an omega clone to train")
        if not catalog[item.skill_id].published:
            notes.append(f"{item.name} is no longer published by CCP - its cost may be historical")

    print(render.table(["skill", "why", "now", "target", "SP cost", "time", "ready"], rows))
    print(f"\ntotal: {render.format_sp(plan.total_sp)} SP; queue would end ~{eta.strftime('%Y-%m-%d %H:%M')} UTC")
    if backlog_end and backlog_end > ctx["now"]:
        print(f"note: the existing queue drains first (finishes ~{backlog_end.strftime('%Y-%m-%d %H:%M')} UTC);"
              " new items start after that")
    for note in notes:
        print(f"note: {note}")
    for covered in plan.covered:
        how = (f"reaches L{covered.level} in the existing queue" if covered.via_queue
               else f"is already trained to L{covered.level}")
        print(f"note: {covered.name} {how} - its L{covered.target} requirement costs nothing")
    if any(item.from_level > item.trained_level for item in plan.items):
        print("note: * = an existing queue entry raises this skill to that level before the item starts")
    pairs = sorted({(catalog[i.skill_id].primary, catalog[i.skill_id].secondary) for i in plan.items})
    print(f"rate: {rate:,.0f} SP/hour ({rate_src})")
    if len(pairs) > 1:
        listed = ", ".join(f"{primary}/{secondary}" for primary, secondary in pairs)
        print(f"note: one rate is applied to every row, but these skills are driven by {listed};"
              " rows whose attributes differ from the calibrated skill are estimates")
    print("note: costs are rank-based and exact; remaps or implant changes while training shift real time")


def cmd_extract(args):
    if not args.char and len(sso.list_characters()) > 1:
        raise RuntimeError("extract needs --char (one character at a time)")
    ctx = gather(args)
    total = int(ctx["skills_doc"].get("total_sp") or 0)
    unalloc = int(ctx["skills_doc"].get("unallocated_sp") or 0)
    allocated = total - unalloc
    try:
        rate, rate_src = planner.calibrated_rate(ctx)
    except RuntimeError:
        rate, rate_src = None, "unknown"
    p = planner.extraction_plan(allocated, total, rate)
    name = ctx["public"].get("name") or ctx["token"]["character_id"]
    print(f"{name}: {render.format_sp(allocated)} trained (allocated) SP, {render.format_sp(unalloc)} unallocated")
    if p["count"] == 0:
        print(f"cannot extract: {p['reason']}")
    else:
        print(f"can run {int(p['count'])} Skill Extractor(s) of {planner.EXTRACTION['extractor_sp']:,} SP each "
              f"(must keep {planner.EXTRACTION['floor_sp']:,} trained; queued/training SP is not extractable)")
        if p["retrain_days_each"]:
            print(f"each extractor costs about {p['retrain_days_each']:.1f} days of re-training at {rate:,.0f} SP/hour ({rate_src})")
        print(f"re-injecting here yields only {p['injector_value']:,} SP per injector at {render.format_sp(total)} total SP "
              f"(tiers: 500k below 5M, 400k to 50M, 300k to 80M, else 150k - lower-SP alts gain more)")
    warn = planner.extraction_rules_warning(ctx["now"])
    if warn:
        print("warning:", warn)
    else:
        print(f"rules per CCP ({planner.EXTRACTION['source']}), verified {planner.EXTRACTION['verified']}")


def cmd_skills(args):
    if args.watch:
        return cmd_watch(args)
    ctxs, failures = gather_all(args)
    for line in failures:
        print(f"warning: skipped {line}", file=sys.stderr)
    if not ctxs:
        raise RuntimeError("no character data could be fetched")
    if args.csv:
        sys.stdout.write(render_csv(ctxs))
        return
    if args.json:
        docs = [json.loads(render_json(ctx, args)) for ctx in ctxs]
        print(json.dumps(docs[0] if len(docs) == 1 else docs, indent=2))
        return
    blocks = [render_text(ctx, args) + (("\n" + week_line(ctx)) if args.week else "") for ctx in ctxs]
    print(("\n" + "=" * 72 + "\n").join(blocks))


def cmd_update_data(args):
    summary = alphadata.update(build=args.build)
    print(f"Updated to SDE build {summary['build']}:")
    for race, g in sorted(summary["grades"].items()):
        print(f"  {g['name']}: {g['skills']} alpha-trainable skills")
    print(f"  skill catalog: {summary['catalog_skills']} skills (name, rank, attributes, prerequisites)")
    print(f"  blueprint materials: {summary['blueprint_products']} products a manufacturing or reaction "
          f"blueprint builds (materials, run time, batch limit)")


def build_parser() -> argparse.ArgumentParser:
    """The whole command surface. Every subcommand here needs an entry in HANDLERS."""
    parser = argparse.ArgumentParser(
        prog="eve-skills",
        description="Show your EVE Online character's skills, training queue and alpha/omega access.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="authorize via EVE SSO (opens browser)")
    p_login.add_argument("--client-id", help="application client id from https://developers.eveonline.com/applications")
    p_login.add_argument("--client-secret", help="only for confidential-type app registrations")
    p_login.add_argument("--port", type=int, help="exact loopback callback port (must match the registered redirect URL; default tries 8635-8637)")
    p_login.add_argument("--manual", action="store_true", help="paste the localhost callback URL manually (for remote hosts reached over ssh)")
    p_login.add_argument("--attributes", action="store_true", help="include character attributes (already covered by the standard skills consent)")
    p_login.add_argument("--scopes", help="extra consents, comma-separated: attributes,standings,jobs,assets,location,clones,orders,corp-orders,structures,all (each re-authenticates the chosen character only)")

    p_logout = sub.add_parser("logout", help="remove stored tokens")
    p_logout.add_argument("--char", help="only this character (name or id); default removes all")

    p_skills = sub.add_parser("skills", help="show skills, queue and clone state (default)")
    p_skills.add_argument("--json", action="store_true", help="machine-readable output")
    p_skills.add_argument("--filter", choices=["all", "alpha", "omega"], default="all", help="restrict the trained-skills table")
    p_skills.add_argument("--sort", choices=["name", "level", "sp"], default="name")
    p_skills.add_argument("--trained-only", action="store_true", help="omit the training queue section")
    p_skills.add_argument("--char", help="stored character name or id (default: show every stored character)")
    p_skills.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table (all stored characters)")
    p_skills.add_argument("--week", action="store_true", help="append SP gained over the last 7 days from local history")
    p_skills.add_argument("--watch", type=int, nargs="?", const=5, metavar="MIN", help="keep refreshing every MIN minutes (default 5), announce finished training; Ctrl-C stops")
    p_skills.add_argument("--notify", action="store_true", help="with --watch: also send notify-send desktop notifications")
    p_skills.add_argument("--full", action="store_true", help="with --watch: keep the full per-character view instead of the compact status table")
    p_skills.add_argument("--no-orders", action="store_true",
                          help="with --watch: watch training only; do not poll market orders")

    sub.add_parser("chars", help="list logged-in characters")

    sub.add_parser("summary", help="one line per character (clone state, SP, queue) plus totals")

    p_events = sub.add_parser("events", help="show recorded watch events (training and market orders)")
    p_events.add_argument("--char", help="stored character name or id; a bare numeric id also matches logged-out characters")
    p_events.add_argument("--owner", metavar="VALUE",
                          help="only order events of one owner: its exact key (char:90000001, "
                               "corp:98000001) or part of its name, case-insensitive - corporation "
                               "events carry no character id, so this is how you find them")
    p_events.add_argument("--kind", action="append", metavar="K",
                          help=f"only this event kind; repeatable: {', '.join(watchstate.EVENT_KINDS)}")
    p_events.add_argument("--limit", type=int, default=50, metavar="N", help="most recent N events (default 50)")
    p_events.add_argument("--json", action="store_true", help="machine-readable output")
    p_events.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table")

    p_attrs = sub.add_parser("attributes", help="base attributes + remap status")
    p_attrs.add_argument("--char", help="stored character name or id (default: every stored character)")

    p_standings = sub.add_parser("standings", help="agent / NPC corp / faction standings (needs login --scopes standings)")
    p_standings.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_standings.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_jobs = sub.add_parser("jobs", help="industry jobs (needs login --scopes jobs)")
    p_jobs.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_jobs.add_argument("--corp", action="store_true", help="corporation industry jobs instead of personal (needs the matching director/Account-Manager role)")
    p_jobs.add_argument("--completed", action="store_true", help="include finished and cancelled jobs")
    p_jobs.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_inv = sub.add_parser("inventory", help="asset inventory with real item names, grouped by location or category and valued (needs login --scopes assets)")
    p_inv.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_inv.add_argument("--corp", action="store_true", help="corporation assets instead of personal (needs the matching director/Account-Manager role)")
    p_inv.add_argument("--by", choices=["location", "category"], default="location",
                       help="group the summary by where the items are (default) or by what they are")
    p_inv.add_argument("--items", action="store_true", help="list every item row instead of the grouped summary, most valuable first")
    p_inv.add_argument("--value-at", dest="value_at", metavar="HUB|REGION",
                       help=f"value holdings at the richest standing buy order there instead of "
                            f"ESI's published reference price; a hub reads that station only: "
                            f"{', '.join(market.HUBS)}, or a region by exact name or id")
    p_inv.add_argument("--json", action="store_true", help="machine-readable output: ids and names together, with the valuation basis")
    p_inv.add_argument("--csv", action="store_true", help="full CSV rows on stdout (always per-item); the valuation footnotes go to stderr")

    p_travel = sub.add_parser("travel", help="current location, home and jump clones with implants (needs login --scopes location / clones)")
    p_travel.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_travel.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the blocks")

    p_impl = sub.add_parser("implants", help="implants fitted in the active clone (needs login --scopes clones)")
    p_impl.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_impl.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the blocks")

    p_plan = sub.add_parser("plan", help="estimate SP and time to reach target skill levels")
    p_plan.add_argument("--char", help="stored character name or id (required when several are stored)")
    p_plan.add_argument("--rate", type=float, metavar="SPH", help="override SP/hour instead of calibrating from live training / SP history")
    p_plan.add_argument("target", nargs="+", metavar="SKILL[:LEVEL]",
                        help='e.g. "Astrogeology:5" (default target L5); missing prerequisites are added automatically')

    p_market = sub.add_parser("market", help="live order-book prices for item types (public ESI, no login)")
    p_market.add_argument("type", nargs="+", metavar="TYPE",
                          help="exact type name or numeric id, e.g. Tritanium or 34")
    p_market.add_argument("--region", action="append", metavar="NAME",
                          help='quote this region (exact name or id); repeatable: --region "The Forge"')
    p_market.add_argument("--hub", action="append", metavar="HUB",
                          help=f"quote a trade hub at station level; repeatable: {', '.join(market.HUBS)}")
    # dest is mandatory here: a bare --global would hand the handler an attribute called `global`.
    p_market.add_argument("--global", dest="global_scopes", action="store_true",
                          help="scan every region with a market and add the best prices across the cluster")
    p_market.add_argument("--history", type=int, metavar="DAYS",
                          help="also show traded volume from ESI's daily regional history (daily, one day behind)")
    p_market.add_argument("--json", action="store_true", help="machine-readable output")
    p_market.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the tables")

    p_build_cost = sub.add_parser("build-cost",
                                  help="ISK cost of manufacturing an item from its blueprint, priced "
                                       "from live ESI orders (public ESI, no login)")
    p_build_cost.add_argument("type", nargs="+", metavar="TYPE",
                              help="exact type name or numeric id of the thing to build, e.g. Hound; "
                                   "repeat for several items priced off the same order books")
    p_build_cost.add_argument("--runs", type=int, default=1, metavar="N",
                              help="installs in one job (default 1); a blueprint's own maximum runs per "
                                   "install still applies, so ask for more than it and the command says no")
    p_build_cost.add_argument("--me", type=int, default=0, metavar="N",
                              help=f"material efficiency 0..{industry.MAX_ME} (default 0) on the "
                                   f"blueprint being run only; component jobs take --component-me, and "
                                   f"a reaction ignores it entirely")
    p_build_cost.add_argument("--component-me", dest="component_me", type=int, default=None,
                              metavar="N",
                              help=f"material efficiency 0..{industry.MAX_ME} for every component job "
                                   f"(default {industry.DEFAULT_COMPONENT_ME}: component blueprints are "
                                   f"ordinarily owned BPOs researched to the cap, unlike the invented "
                                   f"copy a top blueprint often is); independent of --me, and ignored "
                                   f"by a reaction component")
    p_build_cost.add_argument("--te", type=int, default=0, metavar="N",
                              help=f"time efficiency 0..{industry.MAX_TE} (default 0); changes the job's "
                                   f"time, not its ISK")
    p_build_cost.add_argument("--hub", metavar="HUB",
                              help=f"buy every material at this trade hub's station (default jita): "
                                   f"{', '.join(market.HUBS)}")
    p_build_cost.add_argument("--region", metavar="NAME",
                              help='buy anywhere in this region (exact name or id) instead of at one '
                                   'station; needs --system too, because a region has no single cost index')
    p_build_cost.add_argument("--system", metavar="NAME",
                              help="solar system whose industry cost index bills the install (exact name "
                                   "or id); defaults to the hub's own system - it does not narrow the "
                                   "order book, so --system Amarr with the default scope means buy at "
                                   "Jita, install in Amarr")
    p_build_cost.add_argument("--build", action="append", metavar="TYPE",
                              help="force this material to be built even where buying is cheaper; repeatable")
    p_build_cost.add_argument("--buy", action="append", metavar="TYPE",
                              help="force this material to be bought even where building is cheaper; repeatable")
    p_build_cost.add_argument("--build-all", action="store_true",
                              help="build every direct material that some blueprint makes, wherever that "
                                   "leads - including surplus you did not ask for")
    p_build_cost.add_argument("--buy-all", action="store_true",
                              help="buy every quoted material instead of building it: no component jobs at all")
    p_build_cost.add_argument("--facility-tax", dest="facility_tax", type=float,
                              default=industry.NPC_STATION_TAX * 100.0, metavar="PCT",
                              help=f"installation tax as a percent of EIV (default "
                                   f"{industry.NPC_STATION_TAX * 100:g} = an NPC station; a player "
                                   f"structure bills differently and only you know its rate)")
    p_build_cost.add_argument("--material-multiplier", dest="material_multiplier", type=float, default=1.0,
                              metavar="F",
                              help="aggregate material bonus as a fraction of the blueprint's requirements "
                                   "(default 1.0 = none; 0.95 is a 5%% reduction, so quantities and cost fall)")
    p_build_cost.add_argument("--json", action="store_true",
                              help="machine-readable output, including the build option that lost")
    p_build_cost.add_argument("--csv", action="store_true",
                              help="CSV material rows on stdout; the notes go to stderr")

    p_orders = sub.add_parser("orders",
                              help="open market orders of stored characters or their corps (needs login --scopes orders)")
    p_orders.add_argument("--char", help="stored character name or id (default: every stored character)")
    p_orders.add_argument("--corp", action="store_true",
                          help="corporation orders instead of personal ones (the character needs the Accountant or Trader role in that corp)")
    p_orders.add_argument("--closed", action="store_true",
                          help="ESI's ~90-day order history with a derived state, instead of the live book")
    p_orders.add_argument("--type", help="only this exact type name or numeric id, e.g. Tritanium or 34")
    p_orders.add_argument("--buy", action="store_true", help="only buy orders")
    p_orders.add_argument("--sell", action="store_true", help="only sell orders")
    p_orders.add_argument("--limit", type=int, metavar="N",
                          help="newest N rows only; the ISK totals still cover every matching order")
    p_orders.add_argument("--json", action="store_true", help="machine-readable output")
    p_orders.add_argument("--csv", action="store_true", help="CSV rows on stdout instead of the table")
    p_orders.add_argument("--watch", type=int, nargs="?", const=5, metavar="MIN",
                          help="keep refreshing every MIN minutes (default 5), announcing filled/expired/cancelled orders; Ctrl-C stops")
    p_orders.add_argument("--notify", action="store_true",
                          help="with --watch: also send notify-send desktop notifications")

    p_extract = sub.add_parser("extract", help="Skill Extractor math for one character")
    p_extract.add_argument("--char", help="stored character name or id (required when several are stored)")

    p_doctor = sub.add_parser("doctor", help="diagnose installation, stored logins and data freshness (never writes)")
    p_doctor.add_argument("--json", action="store_true", help="machine-readable report")
    p_doctor.add_argument("--network", action="store_true",
                          help="also probe EVE SSO discovery and public ESI endpoints (unauthenticated, bounded)")
    p_doctor.add_argument("--timeout", type=float, default=doctor_mod.NET_TIMEOUT, metavar="SECONDS",
                          help=f"per-request network timeout for --network probes (default {doctor_mod.NET_TIMEOUT:g})")

    p_update = sub.add_parser("update-data", help="refresh alpha caps, the skill catalog and blueprint materials from the official SDE (~100 MB download)")
    p_update.add_argument("--build", type=int, help="specific SDE build number (default: latest)")

    return parser


HANDLERS = {"login": cmd_login, "logout": cmd_logout, "chars": cmd_chars, "summary": cmd_summary,
            "attributes": cmd_attributes, "plan": cmd_plan, "extract": cmd_extract, "update-data": cmd_update_data,
            "standings": exports.cmd_standings, "jobs": exports.cmd_jobs, "inventory": exports.cmd_inventory,
            "travel": exports.cmd_travel, "implants": exports.cmd_implants, "doctor": doctor_mod.cmd_doctor,
            "events": cmd_events, "market": cmd_market, "build-cost": cmd_build_cost,
            "orders": cmd_orders}


def _use_utf8_streams() -> None:
    """Make our own output encoding-independent, whatever the console was configured with.

    Everything this tool writes is UTF-8: the token store, the event history, ESI's JSON and the
    ``--json`` reports that round-trip it. A character name is whatever the player typed and EVE
    accounts are global, so one legitimately contains CJK or Cyrillic - and a Windows stdout that
    is not a console (piped to another program, redirected to a file, captured by CI) encodes with
    the ANSI code page, cp1252 on an English install, where those characters do not exist. The
    command then did all its work and failed on the printing: ``UnicodeEncodeError`` and exit 1 for
    a report that was fine. An interactive Windows console already speaks UTF-8 (PEP 528), so this
    changes nothing for a person at a keyboard; it only stops the redirect from corrupting the run.

    Streams with no ``reconfigure`` - a test's ``StringIO``, an already-replaced stdout - are left
    exactly as they were found.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):     # closed, detached, or not a text stream after all
            pass


def main(argv=None):
    _use_utf8_streams()      # before parsing: an argparse usage error is output too
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None or args.command == "skills":
        if args.command is None:
            args = parser.parse_args(["skills"] + (argv or []))
        try:
            return cmd_skills(args) or 0
        except (RuntimeError, esi_mod.EsiError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

    try:
        # Handlers return None for the ordinary success path; doctor reports its own exit code.
        return int(HANDLERS[args.command](args) or 0)
    except (RuntimeError, esi_mod.EsiError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
