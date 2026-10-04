"""`prices`: track Jita prices of what we sell, and say whether today is a dip worth waiting out.

`sync` is the only subcommand that reads ESI for prices: history for watched types whose stored days end
before yesterday, and one Jita book snapshot each (run hourly by a timer). `signal` and `backtest`
work from the ledger alone - ESI is only asked for names, which come from the shared cache.
The rule and what it was measured to be worth are in `price_watch`.
"""

from __future__ import annotations

import contextlib
import json
import statistics
import sys
from datetime import datetime, timezone

from . import cmd_ledger, ledger_db, market, price_watch as pw, render


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(value: float | None, signed: bool = True) -> str:
    return "-" if value is None else (f"{value:+.1f}%" if signed else f"{value:.0f}%")


def cmd_sync(args) -> int:
    with contextlib.closing(ledger_db.connect()) as conn:
        result = pw.sync(conn, cmd_ledger._client(), history=not args.no_history, book=not args.no_book)
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    elif not args.quiet or result.problems:
        print(f"prices: {result.types} types, history read for {result.history_fetched} "
              f"({result.history_new_days:,} new days), {result.snapshots_new} new book snapshots")
        for line in result.problems:
            print(f"  problem: {line}", file=sys.stderr)
    return 1 if result.problems and not (result.history_fetched or result.snapshots_new) else 0


def _types(conn, args) -> dict[int, str]:
    watched = pw.watchlist(conn, _now())
    if not watched:
        raise RuntimeError("nothing watched yet: the ledger has no recent sales or jobs - "
                           "run: eve-skills ledger sync, or add a type with: eve-skills prices watch add NAME")
    return watched


def _latest_snapshot(conn, type_id: int) -> dict | None:
    rows = ledger_db.book_snapshots(conn, pw.HUB.station_id, type_id)
    return rows[-1] if rows else None


def cmd_signal(args) -> int:
    now = _now()
    with contextlib.closing(cmd_ledger._open()) as conn:
        book, header, _recipes, _invention = cmd_ledger._state(conn)
        watched = _types(conn, args)
        stocks = pw.stock_figures(conn, book.pools, now, watched)
        fee = pw.realised_fee_pct(book.entries, now)
        advice = []
        for type_id in watched:
            days = pw.days_from_rows(ledger_db.market_history(conn, pw.HUB.region_id, type_id))
            advice.append(pw.advise(type_id, days, _latest_snapshot(conn, type_id), stocks[type_id], fee, now))
        last_prices = next((s["synced_at"] for s in ledger_db.sources(conn) if s["source"] == "prices"), None)
    names = cmd_ledger._names(cmd_ledger._client(), watched)
    if not args.all:
        advice = [a for a in advice if a.stock.qty > 0 or a.state in (pw.DIP, pw.HIGH)]
    order = {pw.DIP: 0, pw.HIGH: 1, pw.FALLING: 2, pw.NORMAL: 3, None: 4}
    advice.sort(key=lambda a: (order[a.state], names.get(a.type_id, "")))
    assumptions = [
        "stock and unit cost are the ledger's (goods made or bought, minus goods sold), not a live asset read",
        f"hold cap = {pw.HORIZON_DAYS} days of our own sales over the last {pw.SALES_RATE_DAYS} days, every hub",
        "normal = median daily average in The Forge over the last 30 days; the daily average mixes buy- and "
        "sell-order fills",
        "now = lowest Jita 4-4 ask from the newest book snapshot (under %dh old), else the newest daily average"
        % pw.SNAPSHOT_MAX_AGE_HOURS,
        "fee rate = sales tax + broker fees over revenue, last 30 days of our own sales"
        + ("" if fee is not None else " - none recorded, so net prices are unknown"),
        "measured edge of holding a dip (2026-10-04, T2 rigs/modules): median +2.3%/unit, 56% of holds gained",
    ]
    if args.json:
        rows = []
        for a in advice:
            rows.append({"type_id": a.type_id, "type": names.get(a.type_id), "state": a.state, "action": a.action,
                         "price": a.price, "price_source": a.price_source,
                         "normal": a.base.median if a.base else None, "spread": a.base.scale if a.base else None,
                         "z": a.z, "week_vs_normal_pct": (a.base.short_median / a.base.median - 1) * 100
                         if a.base else None,
                         "deviation_pct": (a.price / a.base.median - 1) * 100 if a.base and a.price else None,
                         "stock": a.stock.qty, "unit_cost": a.stock.unit_cost, "sold_per_day": a.stock.sold_per_day,
                         "hold_cap": a.hold_cap, "hold": a.hold_qty, "sell": a.sell_qty, "target": a.target,
                         "net_now": a.net_now, "past_dips": vars(a.evidence) | {"rate": a.evidence.rate},
                         "reason": a.reason})
        print(json.dumps({**header, "prices_synced": last_prices, "fee_pct": fee, "items": rows,
                          "assumptions": assumptions}, indent=2))
        return 0
    lines = cmd_ledger._print_header("prices signal (Jita 4-4)", header)
    lines.append(f"prices synced: {last_prices or 'never - run: eve-skills prices sync'}")
    table = []
    for a in advice:
        dev = (a.price / a.base.median - 1) * 100 if a.base and a.price else None
        week = (a.base.short_median / a.base.median - 1) * 100 if a.base else None
        below_cost = a.net_now is not None and a.stock.unit_cost is not None and a.net_now < a.stock.unit_cost
        table.append([names.get(a.type_id, f"type {a.type_id}"), a.state or "?",
                      cmd_ledger._compact(a.price) + ("" if a.price_source.startswith("book") else "~"),
                      cmd_ledger._compact(a.base.median if a.base else None), _pct(dev), _pct(week),
                      f"{a.stock.qty:,.0f}", "-" if a.hold_cap is None else f"{a.hold_cap:,.0f}",
                      a.action + (f" {a.hold_qty:,.0f}" if a.hold_qty else ""),
                      cmd_ledger._compact(a.net_now) + ("!" if below_cost else ""),
                      cmd_ledger._compact(a.stock.unit_cost)])
    lines.append(render.table(["type", "state", "now", "normal", "vs normal", "7d vs normal", "stock",
                               "hold cap", "action", "net now", "unit cost"], table))
    lines.append("~ newest daily average (no fresh book snapshot); ! net below the ledger's unit cost")
    for a in advice:
        if a.state in (pw.DIP, pw.FALLING, pw.HIGH):
            lines.append(f"  {names.get(a.type_id, a.type_id)}: {a.reason}")
    header["notes"] = assumptions + header["notes"]
    cmd_ledger._print_notes(lines, header)
    print("\n".join(lines))
    return 0


def cmd_backtest(args) -> int:
    with contextlib.closing(cmd_ledger._open()) as conn:
        watched = _types(conn, args)
        series = {t: pw.days_from_rows(ledger_db.market_history(conn, pw.HUB.region_id, t)) for t in watched}
    names = cmd_ledger._names(cmd_ledger._client(), watched)
    rows, pooled = [], {pw.DIP: [], pw.FALLING: []}
    for type_id, days in series.items():
        if args.sold_only and watched[type_id] != "sales":
            continue
        result = pw.backtest(days, proxy=args.proxy)
        if result is None:
            continue
        episodes = pw.dip_episodes(days)
        dip, falling = pw.evidence(episodes, pw.DIP), pw.evidence(episodes, pw.FALLING)
        for e in episodes:
            if not e.censored and e.hold_return_pct is not None:
                pooled[e.state].append(e.hold_return_pct)
        rows.append({"type_id": type_id, "type": names.get(type_id), "watched_for": watched[type_id],
                     "first_day": days[0].day.isoformat(), "last_day": days[-1].day.isoformat(),
                     "dip": vars(dip), "falling": vars(falling), "simulation": vars(result) | {
                         "uplift_pct": result.uplift_pct}})
    summary = {state: {"holds": len(values), "median_pct": statistics.median(values) if values else None,
                       "mean_pct": statistics.mean(values) if values else None,
                       "won": sum(1 for v in values if v > 0)} for state, values in pooled.items()}
    if args.json:
        print(json.dumps({"proxy": args.proxy, "horizon_days": pw.HORIZON_DAYS, "types": rows,
                          "pooled": summary}, indent=2))
        return 0
    if not rows:
        print("no stored history yet - run: eve-skills prices sync")
        return 0
    rows.sort(key=lambda r: names.get(r["type_id"], ""))
    lines = [f"prices backtest: Jita, hold dips up to {pw.HORIZON_DAYS} days, price = daily {args.proxy}",
             render.table(["type", "days", "dips", "back", "med days", "med hold", "mean hold", "won",
                           "falling med", "1/day uplift"],
                          [[r["type"] or f"type {r['type_id']}", str(r["simulation"]["days"]), str(r["dip"]["dips"]),
                            str(r["dip"]["recovered"]),
                            "-" if r["dip"]["median_days"] is None else f"{r['dip']['median_days']:g}",
                            _pct(r["dip"]["median_return_pct"]), _pct(r["dip"]["mean_return_pct"]),
                            f"{r['dip']['wins']}/{r['dip']['dips']}", _pct(r["falling"]["median_return_pct"]),
                            _pct(r["simulation"]["uplift_pct"])] for r in rows])]
    for state, s in summary.items():
        if s["holds"]:
            lines.append(f"all types, {state} starts: {s['holds']} holds, median {_pct(s['median_pct'])}, "
                         f"mean {_pct(s['mean_pct'])}, {s['won']}/{s['holds']} gained")
    lines += ["", "med/mean hold: per unit held from the first dip day, sold on the first day back near normal "
                  f"or after {pw.HORIZON_DAYS} days, against selling it that first day (before fees)",
              "1/day uplift: one unit a day all year, holding through dips vs selling daily - small because "
              "few days are dips"]
    print("\n".join(lines))
    return 0


def cmd_watch(args) -> int:
    now = _now()
    with contextlib.closing(ledger_db.connect()) as conn:
        if args.watch_action in ("add", "exclude", "reset"):
            client = cmd_ledger._client()
            type_id, name = market.resolve_type(client, args.type)
            with conn:
                ledger_db.set_watchlist(conn, type_id, None if args.watch_action == "reset" else args.watch_action,
                                        pw._iso(now))
            print(f"{name} ({type_id}): {args.watch_action}")
            return 0
        watched = pw.watchlist(conn, now)
        excluded = [t for t, mode in ledger_db.watchlist_changes(conn).items() if mode == "exclude"]
        newest = {t: ledger_db.newest_history_day(conn, pw.HUB.region_id, t) for t in watched}
        snaps = {t: _latest_snapshot(conn, t) for t in watched}
    names = cmd_ledger._names(cmd_ledger._client(), set(watched) | set(excluded))
    if args.json:
        print(json.dumps({"watched": [{"type_id": t, "type": names.get(t), "why": why, "history_to": newest[t],
                                       "last_snapshot": (snaps[t] or {}).get("ts")} for t, why in watched.items()],
                          "excluded": [{"type_id": t, "type": names.get(t)} for t in excluded]}, indent=2))
        return 0
    print(render.table(["type", "why", "history to", "last book"],
                       [[names.get(t, f"type {t}"), why, newest[t] or "-", (snaps[t] or {}).get("ts") or "-"]
                        for t, why in sorted(watched.items(), key=lambda kv: names.get(kv[0], ""))]))
    if excluded:
        print("excluded: " + ", ".join(names.get(t, str(t)) for t in excluded))
    return 0


def cmd_prices(args) -> int:
    handler = {"sync": cmd_sync, "signal": cmd_signal, "backtest": cmd_backtest,
               "watch": cmd_watch}.get(args.prices_action)
    if handler is None:
        print("usage: eve-skills prices {sync,signal,backtest,watch} [--help]", file=sys.stderr)
        return 2
    return handler(args)
