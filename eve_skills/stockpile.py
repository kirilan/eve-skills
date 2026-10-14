"""Stock over time: are finished goods piling up, and are we running out of inputs?

Pure analysis, no ESI. It joins four things the dashboard already has:

* live stock per type (`ledger inventory --json`: held in hangars plus listed in open sell orders),
* the ledger replay's rates - units built and sold per day, and what jobs consumed per day (`Book.consumed`),
* the replay's end-of-day pool of every produced type (`Book.daily_stock`), which reconstructs how finished
  goods built up before any stock snapshot existed,
* stored stock snapshots (`ledger_db.stock_snapshots`), the measured history from the first one on,

plus, when known, the region's daily volume per type and the idle blueprint copies an input is blocking.

A rate is measured over the last `WINDOW_DAYS`, or since the type's first activity when it is newer than
that, so a line started last week is not diluted by days it did not exist.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

from . import ledger

WINDOW_DAYS = 14
# Finished goods: days of our own sales the stock on hand would last.
PILE_DAYS = 14          # beyond this a line is piling up (the dip rule never holds more than two weeks)
SLOW_DAYS = 7
# Inputs: days of current use the stock would cover.
LOW_DAYS = 3
WATCH_DAYS = 7
HISTORY_DAYS = 45
# An idle copy only counts as blocked when its line was built or invented this recently: an old stack of
# copies nobody runs any more is not a production line waiting for an input.
ACTIVE_LINE_DAYS = WINDOW_DAYS

# Most urgent first; the page sorts and colours by these.
GOODS_STATUS = ("not selling", "piling up", "slow", "ok", "sold out")
INPUT_STATUS = ("out", "low", "short", "watch", "ok")


def _day(stamp: str) -> date:
    return date.fromisoformat(stamp[:10])


def _window(first: str | None, today: date, days: int) -> tuple[str, float]:
    """(first day counted, days in the window) - never longer than `days`, never under one."""
    start = today - timedelta(days=days - 1)
    if first is not None and _day(first) > start:
        start = _day(first)
    return start.isoformat(), float(max(1, (today - start).days + 1))


def flows(book: ledger.Book) -> dict[int, dict]:
    """Per type: (day, qty) of what was built, sold and consumed, and when each first happened."""
    out: dict[int, dict] = {}

    def row(type_id: int) -> dict:
        return out.setdefault(type_id, {"built": [], "sold": [], "consumed": [], "first": None})

    for job in book.jobs.values():
        if (job.activity_id in (ledger.MANUFACTURING, *ledger.REACTIONS) and job.done
                and job.product_type_id is not None and job.output_qty):
            row(job.product_type_id)["built"].append(((job.end_date or job.start_date)[:10], job.output_qty))
    for e in book.entries:
        if e.kind == "revenue" and e.type_id is not None and e.scope in (ledger.SCOPE_INVENTION, ledger.SCOPE_OTHER):
            row(e.type_id)["sold"].append((e.date[:10], e.qty))
    for type_id, per_day in book.consumed.items():
        row(type_id)["consumed"].extend(per_day.items())
    for r in out.values():
        days = [d for key in ("built", "sold", "consumed") for d, _q in r[key]]
        r["first"] = min(days) if days else None
    return out


def _rate(pairs: Iterable[tuple[str, float]], start: str) -> float:
    return sum(q for d, q in pairs if d >= start)


def is_input(flow: Mapping) -> bool:
    """A type jobs consume at least as much of as we sell (R.A.M., T1 modules for T2 builds)."""
    consumed = sum(q for _d, q in flow["consumed"])
    return consumed > 0 and consumed >= sum(q for _d, q in flow["sold"])


def goods_history(book: ledger.Book, types: Iterable[int], today: date, days: int = HISTORY_DAYS) -> dict:
    """Day by day, the ledger's quantity and cost of each type, carried over quiet days."""
    wanted = set(types)
    start = today - timedelta(days=days - 1)
    known = sorted(book.daily_stock)
    current: dict[int, tuple[float, float]] = {}
    i = 0
    per_type: dict[int, list] = {t: [] for t in wanted}
    totals = []
    day = start
    while i < len(known) and known[i] < start.isoformat():
        current.update(book.daily_stock[known[i]])
        i += 1
    while day <= today:
        key = day.isoformat()
        while i < len(known) and known[i] <= key:
            current.update(book.daily_stock[known[i]])
            i += 1
        qty = cost = 0.0
        for t in wanted:
            q, c = current.get(t, (0.0, 0.0))
            per_type[t].append(round(q, 2))
            qty += q
            cost += c
        totals.append({"day": key, "qty": round(qty, 2), "cost": round(cost, 2)})
        day += timedelta(days=1)
    return {"days": [t["day"] for t in totals], "totals": totals, "per_type": per_type}


def snapshot_history(rows: Iterable[Mapping], types: Iterable[int] = ()) -> dict:
    """Totals per poll by class (at cost and at market), and per type the last quantity of each day:
    held plus listed for a finished good, held for a material (inputs are used from the hangar)."""
    polls: dict[str, dict] = {}
    per_type: dict[int, dict[str, float]] = {t: {} for t in types}
    for r in rows:
        p = polls.setdefault(r["ts"], {"ts": r["ts"], "material_cost": 0.0, "material_market": 0.0,
                                       "finished_cost": 0.0, "finished_market": 0.0})
        qty = (r["held"] or 0) + (r["listed"] or 0)
        cls = "finished" if r["class"] == "finished" else "material"
        p[f"{cls}_cost"] += qty * (r["unit_cost"] or 0.0)
        p[f"{cls}_market"] += qty * (r["unit_market"] or 0.0)
        if r["type_id"] in per_type:
            per_type[r["type_id"]][r["ts"][:10]] = float(qty if cls == "finished" else r["held"] or 0)
    return {"polls": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items()}
                      for p in sorted(polls.values(), key=lambda p: p["ts"])],
            "per_type": {t: sorted(days.items()) for t, days in per_type.items()}}


def blocked_inputs(blueprints: Iterable[Mapping], lines: set[int]) -> dict[int, list[dict]]:
    """Per material: the idle copies of lines we run that cannot start one job for want of it.
    `blueprints` are `industry status` rows; `lines` the blueprint types we manufacture from."""
    out: dict[int, list[dict]] = {}
    for b in blueprints:
        if b.get("kind") != "BPC" or b.get("blueprint_type_id") not in lines:
            continue
        if b.get("can_build_jobs") or b.get("limiting_type_id") is None:
            continue
        out.setdefault(int(b["limiting_type_id"]), []).append({
            "product": b.get("product"), "copies": int(b.get("count") or 1),
            "have": b.get("have"), "per_job": b.get("per_job")})
    return out


def goods_status(on_hand: float, sold_per_day: float, built_per_day: float) -> tuple[str, float | None]:
    if on_hand <= 0:
        return "sold out", 0.0
    if sold_per_day <= 0:
        return "not selling", None
    days = on_hand / sold_per_day
    if days > PILE_DAYS:
        return "piling up", days
    if days > SLOW_DAYS:
        return "slow", days
    return "ok", days


def input_status(held: float, used_per_day: float, blocking: int) -> tuple[str, float | None]:
    cover = held / used_per_day if used_per_day > 0 else None
    if held <= 0 and (used_per_day > 0 or blocking):
        return "out", 0.0
    if cover is not None and cover < LOW_DAYS:
        return "low", cover
    if blocking:
        return "short", cover
    if cover is not None and cover < WATCH_DAYS:
        return "watch", cover
    return "ok", cover


def analyse(book: ledger.Book, items: Iterable[Mapping], *, today: date,
            market_volume: Mapping[int, float] | None = None,
            blueprints: Iterable[Mapping] = (), snapshots: Iterable[Mapping] = (),
            window: int = WINDOW_DAYS) -> dict:
    """The stockpile section of the dashboard document. `items` are `ledger inventory --json` rows."""
    market_volume = market_volume or {}
    flow = flows(book)
    stock = {int(i["type_id"]): i for i in items}
    recent = (today - timedelta(days=ACTIVE_LINE_DAYS)).isoformat()
    lines = {j.blueprint_type_id for j in book.jobs.values()
             if j.activity_id == ledger.MANUFACTURING and j.blueprint_type_id is not None and j.start_date >= recent}
    lines |= {j.product_type_id for j in book.jobs.values()
              if j.activity_id == ledger.INVENTION and j.product_type_id is not None and j.start_date >= recent}
    blocked = blocked_inputs(blueprints, lines)
    inputs = {t for t, f in flow.items() if is_input(f)} | set(blocked)

    goods = []
    for type_id in sorted(book.produced_products - inputs):
        item = stock.get(type_id, {})
        f = flow.get(type_id) or {"built": [], "sold": [], "consumed": [], "first": None}
        start, span = _window(f["first"], today, window)
        sold, built = _rate(f["sold"], start) / span, _rate(f["built"], start) / span
        held, listed = float(item.get("held") or 0), float(item.get("in_sell_orders") or 0)
        on_hand = held + listed
        if not on_hand and not sold and not built:
            continue
        status, days = goods_status(on_hand, sold, built)
        unit_cost = item.get("unit_cost")
        goods.append({
            "type_id": type_id, "name": item.get("type_name"), "held": held, "listed": listed,
            "on_hand": on_hand, "unit_cost": unit_cost, "unit_market": item.get("unit_market"),
            "value_cost": on_hand * unit_cost if unit_cost is not None else None,
            "value_market": item.get("market_value"),
            "built_per_day": round(built, 2), "sold_per_day": round(sold, 2), "window_days": span,
            "days_of_stock": None if days is None else round(days, 1),
            "market_per_day": market_volume.get(type_id),
            "market_days": (round(on_hand / market_volume[type_id], 1) if market_volume.get(type_id) else None),
            "status": status})

    materials = []
    for type_id in sorted(inputs):
        item = stock.get(type_id, {})
        f = flow.get(type_id) or {"built": [], "sold": [], "consumed": [], "first": None}
        start, span = _window(f["first"], today, window)
        used = _rate(f["consumed"], start) / span
        blocking = blocked.get(type_id, [])
        if not used and not blocking:
            continue
        held = float(item.get("held") or 0)
        status, cover = input_status(held, used, sum(b["copies"] for b in blocking))
        materials.append({
            "type_id": type_id, "name": item.get("type_name"), "held": held,
            "unit_cost": item.get("unit_cost"), "unit_market": item.get("unit_market"),
            "value_cost": item.get("cost_value"), "used_per_day": round(used, 2), "window_days": span,
            "days_of_cover": None if cover is None else round(cover, 1),
            "last_used": max((d for d, _q in f["consumed"]), default=None),
            "blocking": blocking, "status": status})

    goods.sort(key=lambda g: (GOODS_STATUS.index(g["status"]), -(g["days_of_stock"] or 1e9), -(g["value_cost"] or 0)))
    materials.sort(key=lambda m: (INPUT_STATUS.index(m["status"]),
                                  m["days_of_cover"] if m["days_of_cover"] is not None else 1e9))
    modelled = goods_history(book, [g["type_id"] for g in goods], today)
    measured = snapshot_history(snapshots, [m["type_id"] for m in materials] + [g["type_id"] for g in goods])
    for g in goods:
        g["history"] = modelled["per_type"].get(g["type_id"], [])
        g["measured"] = measured["per_type"].get(g["type_id"], [])
    for m in materials:
        m["measured"] = measured["per_type"].get(m["type_id"], [])
    return {
        "window_days": window,
        "thresholds": {"pile_days": PILE_DAYS, "slow_days": SLOW_DAYS, "low_days": LOW_DAYS, "watch_days": WATCH_DAYS},
        "goods": goods, "materials": materials,
        "history_days": modelled["days"], "goods_totals": modelled["totals"],
        "snapshots": measured["polls"],
        "counts": {
            "piling_up": sum(1 for g in goods if g["status"] in ("piling up", "not selling")),
            "slow": sum(1 for g in goods if g["status"] == "slow"),
            "inputs_out_or_low": sum(1 for m in materials if m["status"] in ("out", "low")),
            "inputs_short": sum(1 for m in materials if m["status"] == "short"),
        },
    }


def stock_rows(items: Iterable[Mapping]) -> list[dict]:
    """`ledger inventory --json` rows in the shape `ledger_db.insert_stock_snapshot` stores."""
    return [{"type_id": i["type_id"], "class": i.get("class") or "material", "held": i.get("held") or 0,
             "listed": i.get("in_sell_orders") or 0, "unit_cost": i.get("unit_cost"),
             "unit_market": i.get("unit_market")} for i in items]

