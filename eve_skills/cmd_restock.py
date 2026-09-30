"""restock: what a set of builds and inventions still needs, where it already is, and what to buy.

Demand comes from the local SDE recipes (manufacturing materials with ME rounding per job, invention
datacores per attempt plus one decryptor each) and from fixed extras. It is netted, in this order,
against the corporation's hangars at the build station (usable as they are), what the corporation and
the hauler already own at the buying hub (pick up, no ISK), and the hauler's personal hangars in the
build station's solar system (move into the corporation hangar). The rest is bought at the hub's
lowest sell, then fitted to `--cargo` in priority order: inventions and extras first, then lines as
given, the last line cut to the units that fit.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import alphadata, esi as esi_mod, exports, freshness, industry, market, render, sso, universe


@dataclass
class Demand:
    kind: str                      # "invent", "extra" or "build"
    label: str
    units: int                     # product units, attempts, or quantity
    per_unit: dict[int, float]     # material type id -> quantity per unit (build/invent), or total (extra)
    fit: int = 0
    status: str = "full"
    need: dict[int, dict] = field(default_factory=dict)


def _split(spec: str, flag: str) -> tuple[str, list[str]]:
    name, sep, rest = spec.rpartition("=")
    if not sep or not name.strip() or not rest.strip():
        raise RuntimeError(f"{flag} expects NAME=QTY[:...], got '{spec}'")
    return name.strip(), rest.split(":")


def _int(text: str, flag: str, spec: str, minimum: int = 0) -> int:
    try:
        value = int(text)
    except ValueError:
        raise RuntimeError(f"{flag}: '{text}' in '{spec}' is not a whole number") from None
    if value < minimum:
        raise RuntimeError(f"{flag}: '{spec}' needs a value of at least {minimum}")
    return value


def build_requirements(recipe: industry.Recipe, units: int, me: int, material_multiplier: float = 1.0) -> Counter:
    """Direct materials for `units` of the product, installed as jobs of the blueprint's maximum runs.

    ME rounding is per job (`industry.required_quantity`), so a line is costed as the jobs it would
    really be installed as, not as one imaginary job of every run."""
    runs = math.ceil(units / max(recipe.product_qty, 1))
    size = recipe.max_runs if recipe.max_runs > 0 else runs
    need: Counter = Counter()
    while runs > 0:
        job = min(size, runs)
        for material, base in recipe.materials.items():
            need[material] += industry.required_quantity(base, job, me, material_multiplier)
        runs -= job
    return need


def invention_requirements(invention: dict, t2_blueprint_id: int, attempts: int,
                           decryptor: str | None) -> tuple[Counter, str | None]:
    """Datacores for `attempts` invention attempts that produce `t2_blueprint_id`, plus the decryptor."""
    source = next((row for row in invention["blueprints"].values()
                   if any(int(p[0]) == t2_blueprint_id for p in row["p"])), None)
    if source is None:
        raise RuntimeError(f"no invention recipe in the local SDE data produces blueprint {t2_blueprint_id}")
    need = Counter({int(tid): int(qty) * attempts for tid, qty in source["m"].items()})
    if decryptor and decryptor.lower() != "none":
        wanted = decryptor if decryptor.lower().endswith("decryptor") else f"{decryptor} Decryptor"
        match = next((int(k) for k, v in invention["decryptors"].items()
                      if str(v.get("name", "")).lower() == wanted.lower()), None)
        if match is None:
            names = ", ".join(sorted(v["name"] for v in invention["decryptors"].values()))
            raise RuntimeError(f"unknown decryptor '{decryptor}' - one of: {names}, or none")
        need[match] += attempts
        return need, wanted
    return need, None


def _station_system(client, station_id: int, cache: dict) -> int | None:
    if station_id not in cache:
        try:
            doc = client.get(f"/universe/stations/{station_id}") if station_id <= universe.INT32_MAX else None
        except esi_mod.EsiError:
            doc = None
        cache[station_id] = int(doc["system_id"]) if isinstance(doc, dict) and doc.get("system_id") else None
    return cache[station_id]


def _roots(assets: list[dict]) -> dict[int, int]:
    """location id -> the station or structure it ultimately sits in, walked through the assets.

    A hangar item's `location_id` is an office, a container or a ship, which are assets themselves;
    following `item_id -> location_id` until it leaves the list ends at the root place. No request:
    names are not needed to decide where stock is."""
    parent = {int(a["item_id"]): int(a["location_id"]) for a in assets if a.get("item_id") is not None}
    roots: dict[int, int] = {}
    for asset in assets:
        start = int(asset["location_id"])
        if start in roots:
            continue
        loc, seen = start, set()
        while loc in parent and loc not in seen:
            seen.add(loc)
            loc = parent[loc]
        roots[start] = loc
    return roots


def cmd_restock(args):
    if args.cargo is not None and args.cargo <= 0:
        raise RuntimeError("--cargo must be a positive volume in m3")
    if not (args.line or args.invent or args.extra):
        raise RuntimeError("nothing to supply: name build lines (Product=UNITS[:ME]), --invent or --extra")
    try:
        index = industry.recipe_index(alphadata.blueprint_materials())
    except FileNotFoundError:
        raise RuntimeError("no local blueprint data - run: eve-skills update-data") from None
    client, chars, hints = exports.targets(args, [("assets", "esi-assets.read_corporation_assets.v1")])
    if not chars:
        raise RuntimeError("no stored character with the assets consent to read corporation hangars"
                           + (f" ({hints[0]})" if hints else ""))
    hub = market.hub_scope(args.hub)
    at_id, at_name = market._resolve(client, args.at, "stations", "station")

    # ---- demand
    demands: list[Demand] = []
    invention = None
    for spec in args.invent or []:
        name, parts = _split(spec, "--invent")
        attempts = _int(parts[0], "--invent", spec, 1)
        product_id, product = market.resolve_type(client, name)
        recipe = index.get(product_id)
        if recipe is None:
            raise RuntimeError(f"no blueprint in the local SDE data makes {product}")
        invention = invention or alphadata.blueprint_invention()
        need, dname = invention_requirements(invention, recipe.blueprint_id, attempts,
                                             parts[1] if len(parts) > 1 else None)
        demands.append(Demand("invent", f"invent {product} x{attempts}" + (f" ({dname})" if dname else ""),
                              attempts, {t: q / attempts for t, q in need.items()}))
    for spec in args.extra or []:
        name, parts = _split(spec, "--extra")
        qty = _int(parts[0], "--extra", spec, 1)
        type_id, tname = market.resolve_type(client, name)
        demands.append(Demand("extra", f"extra {tname}", qty, {type_id: 1.0}))
    for spec in args.line or []:
        name, parts = _split(spec, "line")
        units = _int(parts[0], "line", spec, 1)
        me = _int(parts[1], "line", spec) if len(parts) > 1 else 0
        if me > industry.MAX_ME:
            raise RuntimeError(f"ME {me} in '{spec}' is past the cap of {industry.MAX_ME}")
        product_id, product = market.resolve_type(client, name)
        recipe = index.get(product_id)
        if recipe is None:
            raise RuntimeError(f"no blueprint in the local SDE data makes {product}")
        need = build_requirements(recipe, units, me)
        demands.append(Demand("build", f"{product}" + (f" ME{me}" if me else ""), units,
                              {t: q / units for t, q in need.items()}))

    wanted = {t for d in demands for t in d.per_unit}

    # ---- stock
    usable, pickup, move = Counter(), Counter(), Counter()
    systems: dict[int, int | None] = {}
    at_system = _station_system(client, at_id, systems)
    tok, public = chars[0]
    corp_id = exports.corp_of(public)
    reader = public.get("name") or str(tok["character_id"])
    try:
        assets, meta = client.get_all_meta(f"/corporations/{corp_id}/assets", token=tok["access_token"])
    except esi_mod.AuthError as err:
        raise RuntimeError(f"{reader}: ESI refused corporation assets ({err}) - that needs the "
                           f"Director role in the corporation") from None
    roots = _roots(assets)
    for asset in assets:
        type_id = int(asset["type_id"])
        if type_id not in wanted or int(asset.get("quantity", 0)) <= 0:
            continue
        root = roots.get(int(asset["location_id"]), int(asset["location_id"]))
        qty = int(asset["quantity"])
        if root == at_id and asset.get("location_flag") != "CorpDeliveries":
            usable[type_id] += qty
        elif root == hub.location_id:
            pickup[type_id] += qty
    notes = [freshness.line("corp assets", meta) + f" (read by {reader})"]
    if args.hauler:
        hauler_id = sso.resolve_character(args.hauler)
        htok = sso.get_access_token(hauler_id)
        hname = htok.get("character_name") or str(hauler_id)
        try:
            own = client.get_all(f"/characters/{hauler_id}/assets", token=htok["access_token"])
        except esi_mod.AuthError as err:
            raise RuntimeError(f"{hname}: ESI refused personal assets ({err}) - run: eve-skills login "
                               f"--scopes assets") from None
        hroots = _roots(own)
        for asset in own:
            type_id = int(asset["type_id"])
            if type_id not in wanted or int(asset.get("quantity", 0)) <= 0:
                continue
            root = hroots.get(int(asset["location_id"]), int(asset["location_id"]))
            qty = int(asset["quantity"])
            if root == hub.location_id:
                pickup[type_id] += qty
            elif at_system is not None and _station_system(client, root, systems) == at_system:
                move[type_id] += qty
        notes.append(f"{hname}'s personal hangars counted: at {hub.label} as pick-ups, elsewhere in "
                     f"{at_name.split(' ')[0]}'s system as moves into the corporation hangar")

    # ---- prices and volumes
    figures = market.book_figures(client, sorted(wanted), hub)
    infos = universe.type_info(client, wanted)

    def m3(type_id: int) -> float:
        info = infos.get(type_id)
        return float((info.packaged_volume if info and info.packaged_volume is not None else
                      (info.volume if info else 0)) or 0)

    # ---- allocation
    remaining = args.cargo if args.cargo is not None else math.inf

    def need_for(d: Demand, units: int) -> dict[int, dict]:
        out = {}
        for tid, per in d.per_unit.items():
            want = math.ceil(per * units - 1e-9)
            if d.kind == "extra":
                out[tid] = {"need": want, "usable": 0, "pickup": 0, "move": 0, "buy": want}
                continue
            a = min(usable[tid], want)
            b = min(pickup[tid], want - a)
            c = min(move[tid], want - a - b)
            out[tid] = {"need": want, "usable": a, "pickup": b, "move": c, "buy": want - a - b - c}
        return out

    def haul(n: dict[int, dict]) -> float:
        return sum((r["pickup"] + r["buy"]) * m3(t) for t, r in n.items())

    ordered = [d for d in demands if d.kind != "build"] + [d for d in demands if d.kind == "build"]
    for d in ordered:
        n = need_for(d, d.units)
        if haul(n) > remaining:
            lo, hi = 0, d.units - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if haul(need_for(d, mid)) <= remaining:
                    lo = mid
                else:
                    hi = mid - 1
            d.fit = lo
            d.status = "partial" if lo else "does not fit"
            n = need_for(d, lo)
        else:
            d.fit = d.units
        d.need = n
        for t, r in n.items():
            usable[t] -= r["usable"]
            pickup[t] -= r["pickup"]
            move[t] -= r["move"]
        remaining -= haul(n)

    totals: dict[int, Counter] = {}
    for d in ordered:
        for t, r in d.need.items():
            totals.setdefault(t, Counter()).update(r)
    names = esi_mod.resolve_names(client, set(totals))
    rows = []
    for t, r in totals.items():
        price = figures.min_sell.get(t)
        rows.append({"type_id": t, "name": names.get(t) or f"type {t}", "m3_per_unit": m3(t),
                     "need": r["need"], "on_site": r["usable"], "pickup": r["pickup"], "move": r["move"],
                     "buy": r["buy"], "unit_price": price,
                     "buy_isk": None if price is None else price * r["buy"],
                     "haul_m3": (r["pickup"] + r["buy"]) * m3(t)})
    rows.sort(key=lambda r: (-r["haul_m3"], r["name"]))
    unpriced = [r["name"] for r in rows if r["buy"] and r["unit_price"] is None]
    if unpriced:
        notes.append(f"no sell order at {hub.label} for: {', '.join(unpriced)} - their cost is left out")
    total_m3 = sum(r["haul_m3"] for r in rows)
    total_isk = sum(r["buy_isk"] or 0 for r in rows)
    doc = {"generated": market.iso_utc(client.now().timestamp()), "build_station": {"id": at_id, "name": at_name},
           "hub": hub.label, "cargo_m3": args.cargo, "haul_m3": total_m3, "buy_isk": total_isk,
           "lines": [{"kind": d.kind, "label": d.label, "units": d.units, "fit": d.fit, "status": d.status,
                      "haul_m3": haul(d.need)} for d in ordered],
           "materials": rows, "hints": hints, "notes": notes}
    if args.json:
        for line in hints:
            print(line, file=sys.stderr)
        print(json.dumps(doc, indent=2))
        return

    blocks = list(hints)
    blocks.append(render.table(["line", "units", "status", "haul m3"],
                               [[d.label, f"{d.fit:,}/{d.units:,}", d.status, f"{haul(d.need):,.0f}"]
                                for d in ordered]))
    blocks.append(render.table(["material", "need", "on site", "pick up", "move", "buy", "haul m3", "buy ISK"],
                               [[r["name"], f"{r['need']:,}", f"{r['on_site']:,}", f"{r['pickup']:,}",
                                 f"{r['move']:,}", f"{r['buy']:,}", f"{r['haul_m3']:,.0f}",
                                 render.isk(r["buy_isk"]) if r["buy_isk"] is not None else "-"]
                                for r in rows]))
    cargo = f" of {args.cargo:,.0f}" if args.cargo is not None else ""
    lines = [f"haul {total_m3:,.0f} m3{cargo}; buy {render.isk(total_isk)} ISK at {hub.label} lowest sell"]
    buys = [r for r in rows if r["buy"]]
    if buys:
        lines += ["", "Multibuy:"] + [f"{r['name']} {r['buy']}" for r in buys]
    picks = [r for r in rows if r["pickup"]]
    if picks:
        lines += ["", f"already ours at {hub.label} - pick up:"] + [f"  {r['name']} {r['pickup']:,}" for r in picks]
    moves = [r for r in rows if r["move"]]
    if moves:
        lines += ["", "in personal hangars nearby - move into the corporation hangar:"] + \
                 [f"  {r['name']} {r['move']:,}" for r in moves]
    short = [d for d in ordered if d.status != "full"]
    if short:
        lines += ["", "not covered this trip: " + "; ".join(f"{d.label} ({d.units - d.fit:,})" for d in short)]
    blocks.append("\n".join(lines))
    blocks.append("\n".join(f"* {n}" for n in notes + [
        "materials are direct inputs only: a component you build yourself is its own line",
        "on site = the corporation's hangars at the build station; extras are bought in full"]))
    print("\n\n".join(blocks))
