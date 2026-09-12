"""The --watch loops, and the event history they record.

One poll/announce/render/sleep skeleton (`watch_loop`) drives both watches; `skills --watch` and
`orders --watch` differ only in the cycle function they hand it. `events` reads back what they wrote."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field


from . import esi as esi_mod, exports, orders, paths, render, sso, watchstate
# One-way by design: the watch loops read the skills and orders command areas, and neither
# imports this module at module level - `skills --watch` and `orders --watch` reach back in
# from inside their handlers, where this module is fully loaded.
from . import cmd_orders, cmd_skills


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
        phrase += f" at {render.isk(data['price'])} ISK"
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
        statuses = [cmd_skills.queue_status(q, now) for q in queue]
        training = next((q for q in queue if cmd_skills.queue_status(q, now) == "training"), None)
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
    ctxs, failures = cmd_skills.gather_all(args, client=client)
    warnings = [f"skipped {f}" for f in failures]
    poll = OrderPoll() if args.no_orders else poll_order_observations(args, client, session.warned)
    warnings += poll.warnings
    names = order_names(client, poll.books)
    if args.full:
        body = ("\n" + "=" * 72 + "\n").join(cmd_skills.render_text(ctx, args) for ctx in ctxs)
    else:
        body = render_watch_status(ctxs, failures, session.last_good)
    return WatchCycle(warnings=tuple(warnings),
                      observations=tuple(cmd_skills.observation_from_ctx(c) for c in ctxs),
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
                             *[render.csv_cell((ev.get("data") or {}).get(col))
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
        totals = cmd_orders.orders_totals(items)
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
                     render.isk(totals["sell_isk"]), render.isk(totals["buy_escrow_isk"]),
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
