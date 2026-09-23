"""Stock allocation across trade hubs, priced from station books and regional history."""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict

from . import cmd_market, cmd_system, divisions, esi as esi_mod, exports, market, render, sso

DAYTRADING = 16595
HISTORY_DAYS = 30
CSV_COLUMNS = ("type_id", "type", "quantity", "hub", "station_id", "region_id", "region_name",
               "allocated", "list_price", "net_per_unit", "uplift_per_unit",
               "regional_volume_per_day", "days_on_sale", "capacity", "saturated",
               "shortest_jumps", "secure_jumps", "can_reprice", "gross", "net",
               "reference_net", "gain", "packaged_m3")


def _inventory(client, args, seller) -> tuple[dict[int, int], list[str]]:
    token = sso.get_access_token(seller.character_id)
    corp_id = None
    notes = []
    if args.corp:
        public = client.get(f"/characters/{seller.character_id}")
        corp_id = exports.corp_of(public)
        if corp_id is None:
            raise RuntimeError("seller has no corporation id")
        scope = "esi-assets.read_corporation_assets.v1"
    else:
        scope = "esi-assets.read_assets.v1"
    if scope not in set(token.get("scopes") or []):
        notes.append(exports.hint(seller.name, "assets"))
        return {}, notes
    path = (f"/corporations/{corp_id}/assets" if corp_id is not None
            else f"/characters/{seller.character_id}/assets")
    try:
        assets = client.get_all_meta(path, token=token["access_token"])[0]
    except esi_mod.AuthError as exc:
        notes.append(f"{seller.name}: assets unavailable ({exc})")
        return {}, notes
    names = {}
    if corp_id is not None:
        names, note = divisions.fetch(client, token, corp_id)
        if note:
            notes.append(note)
    wanted = divisions.resolve(args.division, names) if args.division is not None else None
    ids = {int(row["type_id"]) for row in assets if row.get("type_id") is not None}
    type_names = esi_mod.resolve_names(client, ids) if args.type else {}
    quantities: dict[int, int] = defaultdict(int)
    for row in assets:
        type_id = int(row["type_id"])
        if wanted is not None and divisions.number(row.get("location_flag")) != wanted:
            continue
        if args.type and args.type.casefold() not in type_names.get(type_id, "").casefold():
            continue
        quantities[type_id] += int(row.get("quantity", 1))
    notes.append("inventory quantities include matching assets at every location; --from sets route origin")
    return dict(quantities), notes


def _items(client, args, seller) -> tuple[list[tuple[int, str, int]], list[str]]:
    if args.from_inventory:
        quantities, notes = _inventory(client, args, seller)
        names = esi_mod.resolve_names(client, set(quantities)) if quantities else {}
        return ([(ident, names.get(ident, f"type {ident}"), qty)
                 for ident, qty in sorted(quantities.items(), key=lambda pair:
                                          (names.get(pair[0], "").casefold(), pair[0])) if qty > 0], notes)
    quantities: dict[int, int] = defaultdict(int)
    names = {}
    for spec in args.item or []:
        name, separator, raw = spec.rpartition("=")
        if not separator or not name.strip():
            raise RuntimeError(f"--item needs NAME=QTY: {spec!r}")
        try:
            qty = int(raw.strip())
        except ValueError:
            qty = 0
        if qty < 1:
            raise RuntimeError(f"--item quantity must be a positive integer: {spec!r}")
        ident, resolved = market.resolve_type(client, name.strip())
        quantities[ident] += qty
        names[ident] = resolved
    return sorted(((ident, names[ident], qty) for ident, qty in quantities.items()),
                  key=lambda row: (row[1].casefold(), row[0])), []


def _routes(client, origin: int, origin_region: int, hubs: list[str], level: int) -> list[dict]:
    routes = []
    for key in hubs:
        hub = market.HUBS[key]
        jumps = {}
        for flag in ("shortest", "secure"):
            path = cmd_system.ROUTE_PATH.format(origin=origin, destination=hub.system_id)
            try:
                route = client.get(f"{path}?flag={flag}")
                jumps[flag] = len(route) - 1
            except esi_mod.EsiError:
                jumps[flag] = None
        distance = jumps["shortest"]
        reachable = (origin == hub.system_id if level < 2 else
                     distance is not None and distance <= 5 * (2 ** (level - 2)) if level < 5 else
                     origin_region == hub.region_id)
        routes.append({"hub": key, "station_id": hub.station_id, "system_id": hub.system_id,
                       "region_id": hub.region_id, "shortest_jumps": distance,
                       "secure_jumps": jumps["secure"], "can_reprice": reachable})
    return routes


def _listing_price(min_sell: float | None) -> float | None:
    if min_sell is None or min_sell <= 0.01:
        return None
    return round(min_sell - 0.01, 2)


def _item_plan(client, type_id: int, name: str, qty: int, hubs: list[str], reference: str,
               routes: dict[str, dict], fees: dict[int, float | None], seller,
               days: float, saturated_days: float) -> dict:
    detail = client.get(f"/universe/types/{type_id}")
    volume = detail.get("packaged_volume", detail.get("volume"))
    per_m3 = float(volume) if volume is not None else None
    rows = []
    for key in hubs:
        hub = market.HUBS[key]
        quote = market.quote(client, type_id, market.hub_scope(key))
        history = market.history_stats(client, hub.region_id, type_id, HISTORY_DAYS)
        rate = history.volume_per_day if history else None
        stock_days = quote.sell_volume / rate if rate else None
        list_price = _listing_price(quote.min_sell)
        fee = fees[hub.station_id]
        net = (market.net_price(list_price, seller.sales_tax_pct + fee)
               if fee is not None else None)
        rows.append(dict(routes[key], list_price=list_price, net_per_unit=net,
                         regional_volume_per_day=rate, days_on_sale=stock_days,
                         sell_volume=quote.sell_volume, allocated=0,
                         capacity=None, saturated=(stock_days is not None and
                                                   stock_days > saturated_days),
                         uplift_per_unit=None, broker_fee_pct=fee,
                         sales_tax_pct=seller.sales_tax_pct))
    ref = next(row for row in rows if row["hub"] == reference)
    if ref["net_per_unit"] is None:
        raise RuntimeError(f"no usable sell listing or NPC station fee at reference {reference} for {name}")
    for row in rows:
        net = row["net_per_unit"]
        row["uplift_per_unit"] = round(net - ref["net_per_unit"], 2) if net is not None else None
        rate = row["regional_volume_per_day"]
        if rate is not None:
            row["capacity"] = math.floor(rate * (1.0 if row["saturated"] else days))
    left = qty
    for row in sorted((row for row in rows if row["hub"] != reference and
                       row["uplift_per_unit"] is not None and row["uplift_per_unit"] > 0),
                      key=lambda row: (-row["uplift_per_unit"], row["hub"])):
        row["allocated"] = min(left, row["capacity"] or 0)
        left -= row["allocated"]
    ref["allocated"] = left
    gross = round(sum(row["allocated"] * (row["list_price"] or 0) for row in rows), 2)
    net = round(sum(row["allocated"] * (row["net_per_unit"] or 0) for row in rows), 2)
    baseline = round(qty * ref["net_per_unit"], 2)
    return {"type_id": type_id, "type": name, "quantity": qty,
            "packaged_m3": None if per_m3 is None else round(qty * per_m3, 6),
            "hubs": rows, "gross": gross, "net": net,
            "reference_net": baseline, "gain": round(net - baseline, 2)}


def cmd_sell_plan(args):
    if not math.isfinite(args.days) or args.days <= 0 or not math.isfinite(args.saturated_days) or args.saturated_days <= 0:
        raise RuntimeError("--days and --saturated-days must be positive finite numbers")
    if args.corp and not args.from_inventory or args.division and not args.from_inventory or args.type and not args.from_inventory:
        raise RuntimeError("--corp, --division and --type require --from-inventory")
    if args.division and not args.corp:
        raise RuntimeError("--division requires --corp")
    hubs = list(dict.fromkeys([key.strip().lower() for key in args.hub] + [args.reference.lower()]))
    for key in hubs:
        market.hub_scope(key)  # reject unknown hubs before any order books
    reference = args.reference.lower()
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    seller, hint = cmd_market.resolve_seller(client, args.seller)
    origin, origin_name = market.resolve_system(client, args.from_system)
    system = client.get(f"/universe/systems/{origin}")
    constellation = client.get(f"/universe/constellations/{system['constellation_id']}")
    region_id = int(constellation["region_id"])
    token = sso.get_access_token(seller.character_id)
    skills = client.get(f"/characters/{seller.character_id}/skills", token=token["access_token"])
    level = next((int(row.get("active_skill_level", 0)) for row in skills.get("skills", [])
                  if int(row["skill_id"]) == DAYTRADING), 0)
    routes = _routes(client, origin, region_id, hubs, level)
    region_names = esi_mod.resolve_names(client, {row["region_id"] for row in routes})
    for route in routes:
        route["region_name"] = region_names.get(route["region_id"], f"region {route['region_id']}")
    places = cmd_market.listing_places(client, seller, [market.HUBS[key].station_id for key in hubs])
    fees = {station: place.fee_pct for station, place in places.items()}
    items, notes = _items(client, args, seller)
    plans = [_item_plan(client, ident, name, qty, hubs, reference,
                        {row["hub"]: row for row in routes}, fees, seller,
                        args.days, args.saturated_days) for ident, name, qty in items]
    total_m3 = (round(sum(row["packaged_m3"] for row in plans), 6)
                if all(row["packaged_m3"] is not None for row in plans) else None)
    totals = {"units": sum(row["quantity"] for row in plans), "packaged_m3": total_m3,
              "gross": round(sum(row["gross"] for row in plans), 2),
              "net": round(sum(row["net"] for row in plans), 2),
              "reference_net": round(sum(row["reference_net"] for row in plans), 2),
              "gain": round(sum(row["gain"] for row in plans), 2)}
    rule = (f"positive net uplift first, up to {args.days:g} × regional units/day; "
            f"over {args.saturated_days:g} days already on sale caps at 1 day; remainder to {reference}")
    doc = {"seller": {"character_id": seller.character_id, "name": seller.name,
                      "sales_tax_pct": seller.sales_tax_pct, "daytrading_level": level},
           "origin": {"system_id": origin, "name": origin_name, "region_id": region_id},
           "reference": reference, "history_scope": "region", "history_days": HISTORY_DAYS,
           "allocation_rule": rule, "routes": routes, "items": plans, "totals": totals,
           "notes": ([hint] if hint else []) + notes}
    if args.json:
        print(json.dumps(doc, indent=2))
        return
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for item in plans:
            for hub in item["hubs"]:
                writer.writerow({key: hub.get(key, item.get(key, "")) for key in CSV_COLUMNS})
        for note in doc["notes"]:
            print(note, file=sys.stderr)
        print(f"totals: {json.dumps(totals, sort_keys=True)}; rule: {rule}; history volume is regional",
              file=sys.stderr)
        return
    lines = [f"Sell plan: {seller.name} from {origin_name} (Daytrading {level})",
             f"rule: {rule}", "history volume/day is regional, not station volume"] + doc["notes"]
    lines.append(render.table(["hub", "region (history)", "shortest", "secure", "reprice"],
                              [[row["hub"], row["region_name"], str(row["shortest_jumps"] or 0) if row["shortest_jumps"] is not None else "?",
                                str(row["secure_jumps"] or 0) if row["secure_jumps"] is not None else "?",
                                "yes" if row["can_reprice"] else "no"] for row in routes]))
    for item in plans:
        lines.append(f"{item['type']} ({item['type_id']}) ×{item['quantity']}")
        lines.append(render.table(["hub", "qty", "list", "net/unit", "uplift", "regional/day", "on sale (days)"],
                                  [[row["hub"], str(row["allocated"]), render.isk(row["list_price"]),
                                    render.isk(row["net_per_unit"]), render.isk(row["uplift_per_unit"]),
                                    "?" if row["regional_volume_per_day"] is None else f"{row['regional_volume_per_day']:.2f}",
                                    "?" if row["days_on_sale"] is None else f"{row['days_on_sale']:.2f}"]
                                   for row in item["hubs"]]))
    lines.append(f"Totals: {totals['units']} units; {totals['packaged_m3'] if total_m3 is not None else '?'} m³; "
                 f"gross {render.isk(totals['gross'])} ISK; net {render.isk(totals['net'])} ISK; "
                 f"all-{reference} net {render.isk(totals['reference_net'])} ISK; "
                 f"gain {render.isk(totals['gain'])} ISK")
    print("\n\n".join(lines))
