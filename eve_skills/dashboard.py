"""The dashboard's document: profitability from the ledger, jobs and orders from live ESI, as one JSON.

Pure assembly - no ESI, no HTTP. `cmd_dashboard` replays the ledger, runs the live reports
(`industry status --corp --json`, `orders --check --json`) and hands everything here; the page in
`data/dashboard.html` renders whatever this returns. The figures keep the ledger's semantics: total
profitability is invention lines plus other trade, disposal is left out, overhead belongs to no
product or market. Planetary industry is its own panel with its own profit (`ledger.planetary`), next
to the total and never inside it, plus the colonies' live extractor timers from `colonies --detail`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta

from . import ledger

# The preset periods the page offers; each gets its own summary, products and markets.
RANGES = (("7d", "Last 7 days", 7), ("30d", "Last 30 days", 30), ("all", "Since start", None))

_JOB_SLOTS = ("manufacturing", "science")


def range_starts(today: date, since: str) -> dict[str, str]:
    """First day of each preset, never before `since` (the operation's start)."""
    out = {}
    for key, _label, days in RANGES:
        start = since if days is None else (today - timedelta(days=days - 1)).isoformat()
        out[key] = max(start, since)
    return out


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def summary(book: ledger.Book, since: str) -> dict:
    (row,) = ledger.pnl(book, since=since) or [None]
    if row is None:
        return {"revenue": 0.0, "gross_profit": 0.0, "overhead": 0.0, "net_profit": 0.0, "margin_pct": None,
                "taxes_and_fees": 0.0, "cogs": 0.0, "units_sold": 0.0}
    total = row["total"]
    units = sum(e.qty for e in book.entries
                if e.kind == "revenue" and e.scope in (ledger.SCOPE_INVENTION, ledger.SCOPE_OTHER)
                and e.date >= since)
    return {
        "revenue": _round(total["revenue"]),
        "taxes_and_fees": _round(total["sales_tax"] + total["broker_fees"]),
        "cogs": _round(total["cogs"]),
        "gross_profit": _round(total["gross_profit"]),
        "overhead": _round(row["overhead_total"]),
        "overhead_lines": {k: _round(v) for k, v in row["overhead"].items()},
        "net_profit": _round(row["net_profit"]),
        "margin_pct": _round(total["gross_profit"] / total["revenue"] * 100) if total["revenue"] else None,
        "units_sold": units,
    }


def timeline(book: ledger.Book, since: str, today: date, by: str = "day") -> list[dict]:
    """One row per day (or ISO week) from `since` to `today`, quiet periods included as zeros, with the
    running net profit - so a chart's x-axis is real time, not just the days something sold."""
    rows = {r["period"]: r for r in ledger.pnl(book, since=since, by=by)}
    periods: list[str] = []
    day = date.fromisoformat(since)
    while day <= today:
        key = ledger.period_key(day.isoformat(), by)
        if not periods or periods[-1] != key:
            periods.append(key)
        day += timedelta(days=1)
    periods += sorted(k for k in rows if k not in periods)      # anything booked past `today`
    out, running = [], 0.0
    for key in periods:
        r = rows.get(key)
        gross = r["total"]["gross_profit"] if r else 0.0
        overhead = r["overhead_total"] if r else 0.0
        net = r["net_profit"] if r else 0.0
        running += net
        planetary = r[ledger.SCOPE_PLANETARY] if r else {}
        out.append({"period": key, "revenue": _round(r["total"]["revenue"] if r else 0.0),
                    "gross_profit": _round(gross), "overhead": _round(overhead), "net_profit": _round(net),
                    "cumulative_net": _round(running),
                    "planetary_revenue": _round(planetary.get("revenue", 0.0)),
                    "planetary_customs": _round(planetary.get("customs", 0.0) + planetary.get("setup", 0.0)),
                    "planetary_profit": _round(planetary.get("gross_profit", 0.0))})
    return out


def products(book: ledger.Book, since: str) -> list[dict]:
    rows = [r for r in ledger.products(book, since=since)
            if r["scope"] not in (ledger.SCOPE_DISPOSAL, ledger.SCOPE_PLANETARY)]
    return [{"type_id": r["type_id"], "scope": r["scope"], "built": r["built"], "sold": r["sold"],
             "unit_cost": _round(r["unit_cost"]), "net_per_unit": _round(r["net_per_unit"]),
             "revenue": _round(r["revenue"]), "profit": _round(r["profit"]),
             "margin_pct": _round(r["margin_pct"])} for r in rows if r["sold"]]


def markets(book: ledger.Book, since: str) -> list[dict]:
    return [{"location_id": r["location_id"], "sold": r["sold"], "types": r["types"],
             "revenue": _round(r["revenue"]), "taxes_and_fees": _round(r["sales_tax"] + r["broker_fees"]),
             "profit": _round(r["profit"]), "margin_pct": _round(r["margin_pct"]),
             "last_sale": r["last_sale"]} for r in ledger.markets(book, since=since)]


def planetary_range(doc: Mapping | None) -> dict:
    """One period of the planetary panel, from `cmd_ledger.planetary_document` for that period."""
    if not doc:
        return {"revenue": 0.0, "taxes_and_fees": 0.0, "customs": 0.0, "setup": 0.0, "profit": 0.0,
                "output_units": 0.0, "sold_units": 0.0, "products": [], "planets": []}
    lines = doc["lines"]
    prods = [{"type_id": r["type_id"], "name": r.get("type_name") or f"type {r['type_id']}", "output": r["built"],
              "sold": r["sold"], "net_per_unit": _round(r["net_per_unit"]), "revenue": _round(r["revenue"]),
              "profit": _round(r["profit"])} for r in doc["products"]]
    planets = [{"planet_id": r["planet_id"], "name": r.get("planet_name") or f"planet {r['planet_id']}",
                "system": r.get("system_name"), "characters": r.get("characters") or [],
                "exports": r["exports"], "export_tax": _round(r["export_tax"]), "import_tax": _round(r["import_tax"]),
                "construction": _round(r["construction"]), "last": r["last"]} for r in doc["planets"]]
    return {"revenue": _round(lines["revenue"]), "taxes_and_fees": _round(lines["sales_tax"] + lines["broker_fees"]),
            "customs": _round(lines["customs"]), "setup": _round(lines["setup"]), "cogs": _round(lines["cogs"]),
            "profit": _round(lines["gross_profit"]), "output_units": doc["output_units"],
            "sold_units": sum(r["sold"] for r in doc["products"]), "products": prods, "planets": planets}


def colonies_section(doc: Mapping | None, error: str | None = None,
                     planet_names: Mapping[int, str] | None = None) -> dict:
    """Every colony with its extractors' end times, from `colonies --detail --json`; soonest first."""
    if doc is None:
        return {"error": error or "no colony report", "colonies": []}
    names = planet_names or {}
    rows, warnings = [], list(doc.get("warnings") or []) + list(doc.get("hints") or [])
    for character in doc.get("characters") or []:
        for c in character.get("colonies") or []:
            extractors = [{"product": x.get("product_name") or (f"type {x['product_type_id']}"
                                                                if x.get("product_type_id") else "–"),
                           "heads": x.get("heads"), "expiry": x.get("expiry_time"), "expired": bool(x.get("expired"))}
                          for x in c.get("extractors") or []]
            # What leaves the colony: everything its facilities make that none of them consumes.
            schematics = [f.get("schematic") or {} for f in c.get("facilities") or []]
            used = {i.get("type_id") for sc in schematics for i in sc.get("inputs") or []}
            makes = sorted({o.get("name") or f"type {o.get('type_id')}" for sc in schematics
                            for o in sc.get("outputs") or [] if o.get("type_id") not in used})
            ends = sorted(x["expiry"] for x in extractors if x["expiry"])
            rows.append({"character": c.get("character_name"), "system": c.get("system_name"),
                         "planet_id": c.get("planet_id"), "planet": names.get(c.get("planet_id")),
                         "type": c.get("planet_type_name"), "ccu": c.get("upgrade_level"),
                         "last_update": c.get("last_update"), "stale": bool(c.get("stale")),
                         "extractors": extractors, "makes": makes, "next_expiry": ends[0] if ends else None})
    rows.sort(key=lambda r: (r["next_expiry"] is None, r["next_expiry"] or "", r["character"] or ""))
    return {"error": error, "as_of": doc.get("generated"), "warnings": warnings, "colonies": rows}


def _status_warning(issue) -> str:
    """One `industry status` warning as a sentence. Its freshness issues are data there
    ({owner_id, subject, delivered_jobs}), and printing that dict would mean nothing to a reader."""
    if isinstance(issue, Mapping) and issue.get("delivered_jobs"):
        count, subject = issue["delivered_jobs"], issue.get("subject") or "asset"
        return (f"{count} job{'' if count == 1 else 's'} delivered after ESI's {subject} snapshot: their output "
                f"is not in the {subject} figures until ESI refreshes it (within the hour)")
    return issue if isinstance(issue, str) else str(issue)


def jobs_section(status: Mapping | None, error: str | None = None) -> dict:
    """Slots per character and the job groups from `industry status --corp --json`."""
    if status is None:
        return {"error": error or "no industry status", "characters": [], "ready": [], "running": []}
    characters, ready, running = [], [], []
    # The report lists each freshness issue twice - once overall, once under its owner - so keep one.
    issues = list(status.get("warnings") or [])
    as_of = None
    for owner in status.get("owners") or []:
        issues += owner.get("warnings") or []
        as_of = ((owner.get("documents") or {}).get("jobs") or {}).get("last_modified") or as_of
        for c in owner.get("characters") or []:
            slots = c.get("slots") or {}
            characters.append({"name": c.get("character_name"),
                               **{kind: slots.get(kind) for kind in _JOB_SLOTS}})
        jobs = owner.get("jobs") or {}
        for group, target in (("ready", ready), ("running", running)):
            for j in jobs.get(group) or []:
                target.append({"who": j.get("installer_name"), "activity": j.get("activity"),
                               "product": j.get("product"), "runs": j.get("runs"), "count": j.get("count"),
                               "end_start": j.get("end_start"), "end_end": j.get("end_end")})
    running.sort(key=lambda j: j["end_start"] or "")
    warnings = list(dict.fromkeys(_status_warning(issue) for issue in issues))
    return {"error": error, "as_of": as_of, "warnings": warnings, "characters": characters,
            "ready": ready, "running": running}


def orders_section(doc: Mapping | None, error: str | None = None) -> dict:
    """Open orders with their standing in the station's book, from `orders --check --json`."""
    if doc is None:
        return {"error": error or "no orders", "orders": []}
    rows = []
    for o in doc.get("orders") or []:
        if o.get("state") not in (None, "open"):
            continue
        check = o.get("check") or {}
        rows.append({"order_id": o.get("order_id"), "owner": o.get("owner_name"),
                     "side": "buy" if o.get("is_buy") else "sell", "type_name": o.get("type_name"),
                     "location": o.get("location_name"), "region": o.get("region_name"),
                     "price": o.get("price"), "volume_total": o.get("volume_total"),
                     "volume_remain": o.get("volume_remain"), "issued": o.get("issued"),
                     "expires": o.get("expires"), "status": check.get("status"),
                     "best_other": check.get("best_other"), "units_ahead": check.get("units_ahead"),
                     "days_ahead": check.get("days_ahead"), "suggest": check.get("suggest"),
                     "book_as_of": check.get("book_as_of")})
    return {"error": error, "as_of": doc.get("generated"), "orders": rows}


def document(book: ledger.Book, header: Mapping, *, since: str, now: str, today: date,
             names: Callable[[Iterable[int]], Mapping[int, str]],
             status: Mapping | None = None, status_error: str | None = None,
             orders: Mapping | None = None, orders_error: str | None = None,
             stock: Mapping | None = None, refresh_minutes: int | None = None,
             planetary: Mapping[str, Mapping] | None = None, colonies: Mapping | None = None,
             colonies_error: str | None = None, planet_names: Mapping[int, str] | None = None) -> dict:
    """`planetary` is `cmd_ledger.planetary_document` per range key (each from that range's start)."""
    starts = range_starts(today, since)
    planetary = planetary or {}
    ranges = {}
    for key, label, _days in RANGES:
        start = starts[key]
        ranges[key] = {"label": label, "since": start, "summary": summary(book, start),
                       "products": products(book, start), "markets": markets(book, start),
                       "planetary": planetary_range(planetary.get(key))}
    newest = planetary.get("all") or next(iter(planetary.values()), None) or {}
    wanted = {r["type_id"] for rng in ranges.values() for r in rng["products"]}
    wanted |= {r["location_id"] for rng in ranges.values() for r in rng["markets"] if r["location_id"] is not None}
    stock = dict(stock or {"error": "no stock analysis", "goods": [], "materials": []})
    stock_rows = [*stock.get("goods", []), *stock.get("materials", [])]
    wanted |= {r["type_id"] for r in stock_rows if not r.get("name")}
    known = names(wanted)
    for rng in ranges.values():
        for r in rng["products"]:
            r["name"] = known.get(r["type_id"]) or f"type {r['type_id']}"
        for r in rng["markets"]:
            r["name"] = (known.get(r["location_id"]) or f"location {r['location_id']}"
                         if r["location_id"] is not None else "unknown station")
    for r in stock_rows:
        r["name"] = r.get("name") or known.get(r["type_id"]) or f"type {r['type_id']}"
    at_cost = sum(p.cost for (kind, _t), p in book.pools.items() if kind == "item" and p.qty > 0)
    wip = ledger.work_in_progress(book)
    return {
        "generated": now,
        "cutover": header.get("cutover"),
        "last_sync": header.get("last_sync"),
        "since": since,
        "refresh_minutes": refresh_minutes,
        "warnings": list(header.get("warnings") or []),
        "assumptions": list(header.get("notes") or []),
        "stock": {"at_cost": _round(at_cost), "wip_cost": _round(sum(j["cost"] for j in wip)), "wip_jobs": len(wip)},
        "stockpile": stock,
        "ranges": ranges,
        "days": timeline(book, since, today, "day"),
        "weeks": timeline(book, since, today, "week"),
        "jobs": jobs_section(status, status_error),
        "orders": orders_section(orders, orders_error),
        "planetary": {
            "tracking_since": book.planetary_since, "held_at": newest.get("held_at"),
            "held_value": _round(newest.get("held_value")),
            "held": [{"type_id": r["type_id"], "name": r.get("type_name") or f"type {r['type_id']}",
                      "units": r["units"], "unit_price": _round(r["unit_price"]), "value": _round(r["value"])}
                     for r in newest.get("held") or []],
            "colonies": colonies_section(colonies, colonies_error, planet_names),
        },
    }
