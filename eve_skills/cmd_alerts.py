"""`alerts`: market order alerts to a Telegram bot.

`run` is one check, made for a timer (contrib/systemd/eve-orders-alert.timer, every 20 minutes - ESI's
cache time for order books): it reads every stored character's open orders and history, places each
open order in its book exactly like `orders --check`, lets `order_alerts.evaluate` decide what is
news, and sends it. With `job_alerts` on it also reads every character's industry jobs, so a job about to
finish is announced and then kept in the reminder until it is delivered. The state is saved only after
Telegram took the message.

`setup` stores the bot token and finds the chat (the user messages the bot first); `test` sends a line.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from . import cmd_orders, esi as esi_mod, exports, market, order_alerts, orders, render, telegram

JOBS_SCOPE = "esi-industry.read_character_jobs.v1"


def _read_token(path: str | None) -> str | None:
    """The bot token from a file, or stdin for `-`: never a command-line argument, which `ps` shows."""
    if not path:
        return None
    with (contextlib.nullcontext(sys.stdin) if path == "-" else open(path, encoding="utf-8")) as fh:
        token = fh.read().strip()
    if not token:
        raise RuntimeError("the token file is empty")
    return token


def _configured(need_chat: bool = True) -> dict:
    doc = telegram.load_config()
    if not doc.get("token"):
        raise RuntimeError("no Telegram bot yet - run: eve-skills alerts setup --token-file FILE")
    if need_chat and not doc.get("chat_id"):
        raise RuntimeError("the bot has no chat yet - send it any message in Telegram, then run: "
                           "eve-skills alerts setup")
    return doc


def cmd_setup(args) -> int:
    doc = telegram.load_config()
    token = _read_token(args.token_file) or doc.get("token")
    if not token:
        raise RuntimeError("pass --token-file FILE (or - for stdin) with the token @BotFather gave you")
    name = telegram.bot_name(token)
    doc["token"] = token
    if args.chat_id is not None:
        doc["chat_id"] = args.chat_id
    else:
        chats = telegram.private_chats(token)
        if not chats:
            telegram.save_config(doc)
            print(f"token saved for @{name}; no chat yet - open @{name} in Telegram, send it any "
                  f"message (e.g. /start) and run this again", file=sys.stderr)
            return 1
        if len(chats) > 1:
            listing = ", ".join(f"{c['id']} ({c['name']})" for c in chats)
            telegram.save_config(doc)
            print(f"several chats wrote to @{name}: {listing}; pick one with --chat-id", file=sys.stderr)
            return 1
        doc["chat_id"] = chats[0]["id"]
    telegram.save_config(doc)
    telegram.send(token, doc["chat_id"], "eve-skills: order alerts will arrive in this chat.")
    print(f"@{name} will send alerts to chat {doc['chat_id']}; saved in {telegram.config_path()}")
    print(f"alert settings: {order_alerts.settings_path()}")
    order_alerts.load_settings()        # writes the defaults on first use, so there is a file to edit
    return 0


def cmd_test(args) -> int:
    doc = _configured()
    telegram.send(doc["token"], doc["chat_id"], args.text or "eve-skills: test message")
    print("sent")
    return 0


def _checked_rows(client: esi_mod.Esi, chars, settings) -> tuple[list[dict], dict[int, str], set[str], list[str]]:
    """This run's open orders with their book standing, the closed states ESI's history shows,
    the owners whose books were read, and one warning per owner that could not be.

    Excluded orders are named but never checked: their books cost ESI calls and nobody reads them."""
    owners, failures = cmd_orders.order_owners(client, SimpleNamespace(corp=False), chars)
    book = [o for owner in owners for o in owner.open]
    ids = {i for o in book for i in (o.type_id, o.region_id, o.location_id) if i}
    names = esi_mod.resolve_names(client, ids) if ids else {}
    names.update(cmd_orders.structure_names(client, book, chars))
    rows = []
    for o in book:
        rows.append({"order_id": o.order_id, "owner": o.owner_name, "owner_key": o.owner_key,
                     "type_name": exports.name_or_id(names, o.type_id), "side": "buy" if o.is_buy else "sell",
                     "location": names.get(o.location_id) or f"location {o.location_id}",
                     "price": o.price, "volume_total": o.volume_total, "volume_remain": o.volume_remain})
    wanted = [o for o, row in zip(book, rows) if not settings.excluded(row)]
    checks = cmd_orders.check_orders(client, wanted, {o.order_id for o in book})
    for row in rows:
        row.update({k: v for k, v in (checks.get(row["order_id"]) or {}).items()
                    if k in ("status", "best_other", "units_ahead", "days_ahead", "suggest", "error")})
    closed = {o.order_id: o.state for owner in owners if owner.history_ok for o in owner.history}
    # An owner without readable history cannot prove an order closed; treat it as unread.
    read = {owner.owner_key for owner in owners if owner.history_ok}
    return rows, closed, read, failures


def _jobs(client: esi_mod.Esi, chars) -> tuple[list[dict], set[str], list[str]]:
    """Every character's undelivered jobs (the character list covers the jobs it installed for its
    corporation too), the owner keys read, and a warning per character ESI refused. Names are left
    for `_name_jobs`, which only names the few that make it into a message."""
    now = client.now()
    rows, read, failures = [], set(), []
    for tok, public in chars:
        if JOBS_SCOPE not in set(tok.get("scopes") or []):
            continue
        cid = int(tok["character_id"])
        name = public.get("name") or tok.get("character_name") or str(cid)
        try:
            jobs = client.get(f"/characters/{cid}/industry/jobs", token=tok["access_token"])
        except (esi_mod.EsiError, RuntimeError) as err:
            failures.append(f"{name} jobs: {err}")
            continue
        key = orders.owner_key("char", cid)
        read.add(key)
        for job in jobs or []:
            end = render.parse_opt(job.get("end_date"))
            status = exports._job_status(job, end, now)
            if status in ("delivered", "cancelled", "reverted"):
                continue
            rows.append({"job_id": job["job_id"], "owner_key": key, "installer": name,
                         "activity": exports.ACTIVITY.get(job.get("activity_id"), "industry"),
                         "product_id": exports._job_ident(job, "product_type_id", "blueprint_type_id"),
                         "facility_id": exports._job_ident(job, *exports.FACILITY_FIELDS),
                         "runs": job.get("runs"), "end": job.get("end_date"), "status": status})
    return rows, read, failures


def _name_jobs(client: esi_mod.Esi, jobs: list[dict], chars) -> None:
    """Product and facility names for the jobs in this message."""
    if not jobs:
        return
    ids = {i for j in jobs for i in (j.get("product_id"), j.get("facility_id")) if i}
    names = esi_mod.resolve_names(client, ids)
    # structure_names wants order-like rows: the installer's own token is tried first.
    places = [SimpleNamespace(location_id=j["facility_id"], owner_key=j["owner_key"])
              for j in jobs if j.get("facility_id")]
    names.update(cmd_orders.structure_names(client, places, chars))
    for job in jobs:
        job["product"] = exports.name_or_id(names, job.get("product_id"))
        job["location"] = names.get(job.get("facility_id")) or f"facility {job.get('facility_id')}"


def cmd_run(args) -> int:
    settings = order_alerts.load_settings()
    doc = None if args.dry_run else _configured()
    features = [(orders.CHARACTER_FEATURE, orders.CHARACTER_SCOPE)]
    if settings.job_alerts:
        features.append(("jobs", JOBS_SCOPE))
    client, chars, hints = exports.targets(SimpleNamespace(char=None, csv=False), features)
    for line in hints:
        print(line, file=sys.stderr)
    order_chars = [(tok, pub) for tok, pub in chars if orders.CHARACTER_SCOPE in set(tok.get("scopes") or [])]
    rows, closed, read, failures = _checked_rows(client, order_chars, settings)
    jobs = job_owners = None
    if settings.job_alerts:
        jobs, job_owners, job_failures = _jobs(client, chars)
        failures += job_failures
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    now = datetime.fromtimestamp(client.now().timestamp(), timezone.utc)
    state, alert = order_alerts.evaluate(order_alerts.load_state(), rows, closed, read, settings, now,
                                         jobs, job_owners)
    _name_jobs(client, alert.jobs_soon + alert.jobs_waiting, chars)
    text = None if alert.empty() else order_alerts.render(alert, settings, now)
    if args.dry_run:
        print(text or "(nothing new)")
        unchecked = sorted({r["error"] for r in rows if r.get("status") == "unchecked" and r.get("error")})
        for reason in unchecked:
            print(f"unchecked: {reason}", file=sys.stderr)
        return 0
    if text:
        telegram.send(doc["token"], doc["chat_id"], text, silent=alert.silent)
    order_alerts.save_state(state)
    if not args.quiet:
        stamp = market.iso_utc(now.timestamp())
        print(f"{stamp} {len(rows)} open orders; " + (
            f"sent: {len(alert.behind)} behind, {len(alert.back_on_top)} back on top, "
            f"{len(alert.filled)} filled, {len(alert.still_behind)} reminders, "
            f"{len(alert.jobs_soon)} jobs ending, {len(alert.jobs_waiting)} jobs to deliver"
            + (" (silent)" if alert.silent else "") if text else "nothing new"))
    return 0


def cmd_alerts(args) -> int:
    action = getattr(args, "alerts_action", None)
    if action == "setup":
        return cmd_setup(args)
    if action == "test":
        return cmd_test(args)
    if action == "run":
        return cmd_run(args)
    raise RuntimeError("usage: eve-skills alerts {setup,test,run}")
