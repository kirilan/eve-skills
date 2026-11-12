"""restock: what a set of builds and inventions still needs, where it already is, and what to buy.

Demand comes from the local SDE recipes (manufacturing materials with ME rounding per job, invention
datacores per attempt plus one decryptor each) and from fixed extras. It is netted, in this order,
against the stock at the build facility (usable as they are), what we already own at the buying hub
(pick up, no ISK), and the hauler's personal hangars in the build station's solar system (move into
the corporation hangar). The rest is bought at the hub's lowest sell, then fitted to `--cargo` in
priority order: inventions and extras first, then lines as given, the last line cut to the units that
fit.

Two kinds of build facility. A corporation site keeps its stock in the corporation's hangars, read
through one character with the Director role. A personal site (`--site` naming one in the operation
file) has no shared hangar: every character listed for it holds their own stock in the facility, so
stock is read per character, and a line or invention assigned to a character (`...@NAME`) is supplied
from that character's own hangar first and then by **transfers** from the others - the trades to make
before installing. With a freight route configured, the haul is priced as a courier contract.

A personal site may build in one facility and invent in another (`sites.NAME.facilities`). Without
`--at`, build lines are then netted against stock at the manufacturing facility and `--invent` lines
against stock at the invention facility; what a site character holds at the other one is not bought
but listed as a move between the site's facilities.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import alphadata, esi as esi_mod, exports, freshness, industry, market, operation, price_watch as pw, render, sso, universe


@dataclass
class Demand:
    kind: str                      # "invent", "extra" or "build"
    label: str
    units: int                     # product units, attempts, or quantity
    per_unit: dict[int, float]     # material type id -> quantity per unit (build/invent), or total (extra)
    fit: int = 0
    status: str = "full"
    need: dict[int, dict] = field(default_factory=dict)
    builder: str | None = None     # the character installing it (personal sites): `...@NAME`


def _assignee(spec: str, flag: str) -> tuple[str, str | None]:
    """`NAME=QTY[:...][@CHAR]` -> (the spec without its assignee, the character or None)."""
    body, at, who = spec.rpartition("@")
    if not at or "=" not in body:
        return spec, None
    if not who.strip():
        raise RuntimeError(f"{flag}: '{spec}' ends in '@' without a character name")
    return body, who.strip()


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


def build_requirements(recipe: industry.Recipe, units: int, me: int, material_multiplier: float = 1.0,
                       job_runs: int | None = None) -> Counter:
    """Direct materials for `units` of the product, installed as jobs of `job_runs` runs (default: the
    blueprint's maximum runs).

    ME rounding is per job (`industry.required_quantity`), so a line is costed as the jobs it would
    really be installed as, not as one imaginary job of every run."""
    runs = math.ceil(units / max(recipe.product_qty, 1))
    size = job_runs or (recipe.max_runs if recipe.max_runs > 0 else runs)
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


PERSONAL_ASSETS_SCOPE = "esi-assets.read_assets.v1"


def personal_stock(client, characters, wanted: set[int], at_id: int,
                   hub_id: int | None) -> tuple[dict[str, Counter], Counter, list[str]]:
    """Each site character's own stock in the facility, plus what any of them holds at the hub.

    Returns ({character: Counter}, pickup Counter, notes). A character that is not stored, did not
    grant the assets consent, or is refused by ESI is named in the notes and counted as holding
    nothing - never silently, because their stock is then bought again."""
    by_station, pickup, notes = personal_stock_at(client, characters, wanted, (at_id,), hub_id)
    return by_station[at_id], pickup, notes


def personal_stock_at(client, characters, wanted: set[int], at_ids, hub_id: int | None
                      ) -> tuple[dict[int, dict[str, Counter]], Counter, list[str]]:
    """`personal_stock` for several facilities, reading each character's assets once.

    Returns ({station id: {character: Counter}}, pickup Counter, notes); every readable character has
    an entry (possibly empty) at every station."""
    at_ids = tuple(dict.fromkeys(at_ids))
    by_station: dict[int, dict[str, Counter]] = {at: {} for at in at_ids}
    pickup: Counter = Counter()
    notes: list[str] = []
    missing: list[str] = []
    at_hub: list[str] = []
    for spec in characters:
        try:
            cid = sso.resolve_character(spec)
            tok = sso.get_access_token(cid)
        except (RuntimeError, KeyError, OSError) as err:
            missing.append(f"{spec} ({err})")
            continue
        name = tok.get("character_name") or spec
        if PERSONAL_ASSETS_SCOPE not in (tok.get("scopes") or []):
            missing.append(f"{name} (no assets consent - run: eve-skills login --scopes assets)")
            continue
        try:
            assets, meta = client.get_all_meta(f"/characters/{cid}/assets", token=tok["access_token"])
        except esi_mod.EsiError as err:
            missing.append(f"{name} (ESI refused personal assets: {err})")
            continue
        stocks = {at: Counter() for at in at_ids}
        roots = _roots(assets)
        for asset in assets:
            type_id = int(asset["type_id"])
            if type_id not in wanted or int(asset.get("quantity", 0)) <= 0:
                continue
            root = roots.get(int(asset["location_id"]), int(asset["location_id"]))
            if root in stocks:
                stocks[root][type_id] += int(asset["quantity"])
            elif hub_id is not None and root == hub_id:
                pickup[type_id] += int(asset["quantity"])
                if name not in at_hub:
                    at_hub.append(name)
        for at in at_ids:
            by_station[at][name] = stocks[at]
        notes.append(freshness.line(f"{name}'s assets", meta))
    if missing:
        notes.append("not read, so their stock is not counted: " + "; ".join(missing))
    if at_hub:
        notes.append(f"pick-ups at the hub are held by {', '.join(at_hub)}")
    return by_station, pickup, notes


def plan_transfers(demands: list[Demand], holdings: dict[str, Counter]) -> tuple[list[dict], dict[str, Counter]]:
    """Who gives whom what, so every assigned line is supplied from its installer's own hangar.

    Each demand's on-site share is taken from its builder's stock first, then from the character
    holding the most of that material (a transfer); an unassigned demand draws from whoever holds
    the most, and no transfer is listed for it because nobody said who installs it. What the haul
    brings (pick-ups and purchases) is listed per builder as a delivery."""
    left = {who: Counter(stock) for who, stock in holdings.items()}
    by_lower = {who.lower(): who for who in left}
    moves: Counter = Counter()
    deliveries: dict[str, Counter] = {}
    for d in demands:
        builder = by_lower.get(d.builder.lower()) if d.builder else None
        for type_id, r in d.need.items():
            want = r.get("usable", 0)
            if builder is not None:
                own = min(left[builder][type_id], want)
                left[builder][type_id] -= own
                want -= own
            for giver in sorted(left, key=lambda w: (-left[w][type_id], w)):
                if want <= 0:
                    break
                if giver == builder:
                    continue
                take = min(left[giver][type_id], want)
                if take <= 0:
                    break
                left[giver][type_id] -= take
                want -= take
                if builder is not None:
                    moves[(giver, builder, type_id)] += take
            brought = r.get("pickup", 0) + r.get("buy", 0)
            if builder is not None and brought:
                deliveries.setdefault(builder, Counter())[type_id] += brought
    transfers = [{"from": giver, "to": taker, "type_id": type_id, "quantity": qty}
                 for (giver, taker, type_id), qty in sorted(moves.items()) if qty]
    return transfers, deliveries


def price_check(client, hub: market.Scope, demands: list[Demand], rows: list[dict], held: Counter,
                cover_days: float) -> dict:
    """The buy side of `price_watch` for every material this trip buys: dips get extra units scaled by
    depth, highs lose their --extra buffer. Reads each one's daily history in the hub's region and
    the hub station's asks; a document that fails leaves that material unjudged, never guessed."""
    need, buffer = Counter(), Counter()
    for d in demands:
        for t, r in d.need.items():
            if d.kind == "extra":
                buffer[t] += r["buy"]
            else:
                need[t] += r["need"]
    bought = [r for r in rows if r["buy"]]
    ids = [r["type_id"] for r in bought]
    history = client.get_many_meta([f"/markets/{hub.region_id}/history?type_id={t}" for t in ids])
    books = market._read_books(client, ids, hub)
    items, failed = [], []
    for r in bought:
        t = r["type_id"]
        answer = history.get(f"/markets/{hub.region_id}/history?type_id={t}")
        book = books.get(t)
        if not isinstance(answer, tuple) or not isinstance(book, tuple):
            failed.append(r["name"])
            continue
        asks = sorted((float(o["price"]), int(o.get("volume_remain") or 0))
                      for o in market._scope_rows(book[0], hub)
                      if not o.get("is_buy_order") and o.get("price") is not None)
        a = pw.buy_advice(pw.days_from_rows(answer[0]), asks, need[t], r["buy"], held[t], buffer[t], cover_days)
        items.append({"type_id": t, "name": r["name"], "state": a.state, "ask": a.ask,
                      "normal": a.base.median if a.base else None, "depth_pct": a.depth_pct, "z": a.z,
                      "multiple": a.multiple, "extra": a.extra, "extra_isk": a.extra_isk, "limit": a.limit,
                      "skip_buffer": a.skip_buffer, "reason": a.reason})
    notes = [f"normal = median daily average in the hub's region over the last {pw.BASELINE_DAYS} days; "
             f"a dip is an ask {pw.DIP_Z:g} spreads under it, unless the last {pw.SHORT_DAYS} days were that "
             f"low too (a new level)",
             "dip extras: the trip's need x " + ", ".join(f"{m:g} from {f:g}%" for f, m in
                                                          reversed(pw.DIP_MULTIPLES))
             + f" under normal, at most {pw.BUY_COVER_DAYS} days of use including stock we own, and only "
               "from asks still under the dip line",
             "measured on daily averages (2026-10-27, 22 inputs, a year): buying a dip saved a median 7%, "
             "more the deeper it was; the ask-side edge is smaller and not yet measured"]
    if failed:
        notes.append("no history or book for: " + ", ".join(failed) + " - not judged")
    return {"hub": hub.label, "cover_days": cover_days, "cap_days": pw.BUY_COVER_DAYS, "items": items,
            "notes": notes}


def price_check_text(check: dict, rows: list[dict]) -> str:
    flagged = [i for i in check["items"] if i["state"] in (pw.DIP, pw.HIGH, pw.FALLING)]
    calm = len(check["items"]) - len(flagged)
    if not flagged:
        lines = [f"price check ({check['hub']}): all {calm:,} bought materials at normal prices"]
    else:
        order = {pw.DIP: 0, pw.HIGH: 1, pw.FALLING: 2}
        flagged.sort(key=lambda i: (order[i["state"]], i["name"]))
        lines = [f"price check ({check['hub']} ask vs {pw.BASELINE_DAYS}-day normal; "
                 f"lines cover {check['cover_days']:g} days):",
                 render.table(["material", "state", "ask", "normal", "vs normal", "extra", "extra ISK", "why"],
                              [[i["name"], i["state"], render.isk(i["ask"]), render.isk(i["normal"]),
                                f"{-i['depth_pct']:+.1f}%", f"{i['extra']:,}" if i["extra"] else
                                (f"-{i['skip_buffer']:,}" if i["skip_buffer"] else "-"),
                                render.isk(i["extra_isk"]) if i["extra_isk"] else "-", i["reason"]]
                               for i in flagged])]
        if calm:
            lines.append(f"{calm:,} other bought materials at normal prices (or not judged)")
        change = {i["type_id"]: i["extra"] - i["skip_buffer"] for i in flagged if i["extra"] or i["skip_buffer"]}
        if change:
            extra_isk = sum(i["extra_isk"] or 0 for i in flagged)
            lines += ["", "Multibuy with the price check applied"
                      + (f" (dip extras {render.isk(extra_isk)} ISK):" if extra_isk else ":")]
            lines += [f"{r['name']} {r['buy'] + change.get(r['type_id'], 0)}" for r in rows
                      if r["buy"] + change.get(r["type_id"], 0) > 0]
    lines += [f"* {n}" for n in check["notes"]]
    return "\n".join(lines)


def cmd_restock(args):
    if args.cargo is not None and args.cargo <= 0:
        raise RuntimeError("--cargo must be a positive volume in m3")
    if not (args.line or args.invent or args.extra):
        raise RuntimeError("nothing to supply: name build lines (Product=UNITS[:ME]), --invent or --extra")
    try:
        index = industry.recipe_index(alphadata.blueprint_materials())
    except FileNotFoundError:
        raise RuntimeError("no local blueprint data - run: eve-skills update-data") from None
    op = operation.load()
    site = op.site(args.site) if (args.site or (op.configured and not args.at)) else None
    if site is not None and site.owner == "corp" and site.facilities and not args.at:
        raise RuntimeError(f"site {site.name} names a facility per activity, which is not supported for a "
                           f"corp site - pass --at with the station or structure to read")
    facility = op.facility(site.facility_for("manufacturing")) if site else None
    personal = site is not None and site.owner == "personal"
    # A personal site that invents somewhere else than it builds: two stock locations (no --at).
    invent_facility = (op.facility(site.facility_for("invention"))
                       if personal and not args.at and site.facility_for("invention") != facility.name else None)
    split = invent_facility is not None
    material_multiplier = facility.material_multiplier("manufacturing") if facility else 1.0
    notes_pre: list[str] = []
    if personal:
        client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
        chars, hints = [], []
    else:
        if site is not None and not args.char and site.readers:
            args.char = site.readers[0]
        client, chars, hints = exports.targets(args, [("assets", "esi-assets.read_corporation_assets.v1")])
        if not chars:
            raise RuntimeError("no stored character with the assets consent to read corporation hangars"
                               + (f" ({hints[0]})" if hints else ""))
    hub = market.hub_scope(args.hub)
    if args.at:
        if str(args.at).strip().isdigit() and int(args.at) > universe.INT32_MAX:
            at_id, at_name = int(args.at), f"structure {args.at}"
        else:
            at_id, at_name = market._resolve(client, args.at, "stations", "station")
    elif facility is not None:
        if facility.location_id is None:
            raise RuntimeError(f"facility {facility.name} has no location_id in the operation file, so its "
                               f"hangars cannot be found - fill it in, or pass --at")
        at_id, at_name = facility.location_id, f"{facility.name} ({facility.system or facility.location_id})"
    else:
        raise RuntimeError("name the build station with --at, or a site of the operation file with --site")
    inv_id = inv_name = None
    if split:
        if invent_facility.location_id is None:
            raise RuntimeError(f"facility {invent_facility.name} (where site {site.name} invents) has no "
                               f"location_id in the operation file - fill it in, or pass --at")
        inv_id = invent_facility.location_id
        inv_name = f"{invent_facility.name} ({invent_facility.system or inv_id})"
    labels = {at_id: (facility.label or at_name) if facility is not None else at_name}
    if split:
        labels[inv_id] = invent_facility.label or inv_name

    # ---- demand
    demands: list[Demand] = []
    invention = None
    for spec in args.invent or []:
        spec, builder = _assignee(spec, "--invent")
        name, parts = _split(spec, "--invent")
        attempts = _int(parts[0], "--invent", spec, 1)
        product_id, product = market.resolve_type(client, name)
        recipe = index.get(product_id)
        if recipe is None:
            raise RuntimeError(f"no blueprint in the local SDE data makes {product}")
        invention = invention or alphadata.blueprint_invention()
        need, dname = invention_requirements(invention, recipe.blueprint_id, attempts,
                                             parts[1] if len(parts) > 1 else None)
        demands.append(Demand("invent", f"invent {product} x{attempts}" + (f" ({dname})" if dname else "")
                              + (f" @{builder}" if builder else ""),
                              attempts, {t: q / attempts for t, q in need.items()}, builder=builder))
    for spec in args.extra or []:
        name, parts = _split(spec, "--extra")
        qty = _int(parts[0], "--extra", spec, 1)
        type_id, tname = market.resolve_type(client, name)
        demands.append(Demand("extra", f"extra {tname}", qty, {type_id: 1.0}))
    for spec in args.line or []:
        spec, builder = _assignee(spec, "line")
        name, parts = _split(spec, "line")
        units = _int(parts[0], "line", spec, 1)
        me = _int(parts[1], "line", spec) if len(parts) > 1 and parts[1] else 0
        if me > industry.MAX_ME:
            raise RuntimeError(f"ME {me} in '{spec}' is past the cap of {industry.MAX_ME}")
        job_runs = _int(parts[2], "line", spec, 1) if len(parts) > 2 else None
        product_id, product = market.resolve_type(client, name)
        recipe = index.get(product_id)
        if recipe is None:
            raise RuntimeError(f"no blueprint in the local SDE data makes {product}")
        if job_runs is None:
            # An invented copy carries more runs than the T2 blueprint's own limit; without :RUNS the
            # line is costed at the most an invented copy can hold, which is what ME rounding needs.
            try:
                invented = industry.invented_run_limit(recipe.blueprint_id, invention or alphadata.blueprint_invention())
            except (FileNotFoundError, ValueError):
                invented = 0
            if invented > recipe.max_runs > 0:
                job_runs = invented
                notes_pre.append(f"{product}: jobs of {invented} runs (an invented copy's most); "
                                 f"add :RUNS to the line for other copies, e.g. {product}={units}:{me}:4")
        need = build_requirements(recipe, units, me, material_multiplier, job_runs)
        demands.append(Demand("build", f"{product}" + (f" ME{me}" if me else "")
                              + (f" x{job_runs}r" if job_runs else "") + (f" @{builder}" if builder else ""),
                              units, {t: q / units for t, q in need.items()}, builder=builder))

    wanted = {t for d in demands for t in d.per_unit}

    # ---- stock
    usable, pickup, move = Counter(), Counter(), Counter()
    holdings: dict[str, Counter] = {}       # personal site: character -> stock in the (build) facility
    by_station: dict[int, dict[str, Counter]] = {}
    stock_at: dict[int, Counter] = {}       # split site: station -> site stock left to allocate
    systems: dict[int, int | None] = {}
    at_system = _station_system(client, at_id, systems)
    notes = list(notes_pre)
    if personal:
        stations = (at_id, inv_id) if split else (at_id,)
        by_station, picked, read_notes = personal_stock_at(client, site.characters, wanted, stations,
                                                           hub.location_id)
        holdings = by_station[at_id]
        for stock in holdings.values():
            usable.update(stock)
        if split:
            for station in stations:
                stock_at[station] = Counter()
                for stock in by_station[station].values():
                    stock_at[station].update(stock)
        pickup.update(picked)
        notes += read_notes
        known = {name.lower() for name in holdings}   # every readable character has an entry
        for d in demands:
            if d.builder and d.builder.lower() not in known:
                raise RuntimeError(f"{d.label}: {d.builder} is not a readable character of site {site.name} "
                                   f"({', '.join(holdings) or 'none'})")
    else:
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
        notes.append(freshness.line("corp assets", meta) + f" (read by {reader})")
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
    held = usable + pickup + move     # before the allocation spends them: what we own, for the price check
    if split:
        held += stock_at[inv_id]

    # ---- prices and volumes
    figures = market.book_figures(client, sorted(wanted), hub)
    infos = universe.type_info(client, wanted)

    def m3(type_id: int) -> float:
        info = infos.get(type_id)
        return float((info.packaged_volume if info and info.packaged_volume is not None else
                      (info.volume if info else 0)) or 0)

    # ---- allocation
    remaining = args.cargo if args.cargo is not None else math.inf

    def home_of(d: Demand) -> tuple[int, int]:
        """(station whose stock the demand uses, the other site facility) on a split site."""
        return (inv_id, at_id) if d.kind == "invent" else (at_id, inv_id)

    def need_for(d: Demand, units: int) -> dict[int, dict]:
        out = {}
        for tid, per in d.per_unit.items():
            want = math.ceil(per * units - 1e-9)
            if d.kind == "extra":
                out[tid] = {"need": want, "usable": 0, "pickup": 0, "move": 0, "buy": want}
                continue
            if split:
                home, other = home_of(d)
                a = min(stock_at[home][tid], want)
                b = min(pickup[tid], want - a)
                s = min(stock_at[other][tid], want - a - b)   # at the site's other facility: move it
                c = min(move[tid], want - a - b - s)
                out[tid] = {"need": want, "usable": a, "pickup": b, "move": s + c, "buy": want - a - b - s - c,
                            "site_move": s}
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
            if split and d.kind != "extra":
                home, other = home_of(d)
                stock_at[home][t] -= r["usable"]
                stock_at[other][t] -= r["site_move"]
                move[t] -= r["move"] - r["site_move"]
            else:
                usable[t] -= r["usable"]
                move[t] -= r["move"]
            pickup[t] -= r["pickup"]
        remaining -= haul(n)

    totals: dict[int, Counter] = {}
    site_moves: Counter = Counter()          # (type id, from station) -> units moved between site facilities
    for d in ordered:
        for t, r in d.need.items():
            totals.setdefault(t, Counter()).update(r)
            if r.get("site_move"):
                site_moves[(t, home_of(d)[1])] += r["site_move"]
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
    if split:
        # Transfers happen where each job is installed: builds at the factory, inventions at the lab.
        transfers, deliveries = plan_transfers([d for d in ordered if d.kind != "invent"], holdings)
        inv_transfers, inv_deliveries = plan_transfers([d for d in ordered if d.kind == "invent"],
                                                       by_station[inv_id])
        for item in transfers:
            item["at"] = labels[at_id]
        for item in inv_transfers:
            item["at"] = labels[inv_id]
        transfers += inv_transfers
        for who, items in inv_deliveries.items():
            deliveries.setdefault(who, Counter()).update(items)
        for (t, station), qty in sorted(site_moves.items()):
            left, holders = qty, []
            for who, stock in sorted(by_station[station].items(), key=lambda kv: (-kv[1][t], kv[0])):
                if left <= 0 or stock[t] <= 0:
                    break
                take = min(stock[t], left)
                holders.append(f"{who} {take:,}")
                left -= take
            notes.append(f"move between site facilities: {names.get(t) or f'type {t}'} {qty:,} at "
                         f"{labels[station]}, held by {', '.join(holders) or 'nobody readable'}")
    else:
        transfers, deliveries = (plan_transfers(ordered, holdings) if personal else ([], {}))
    for item in transfers:
        item["name"] = names.get(item["type_id"]) or f"type {item['type_id']}"
    for who, items in deliveries.items():
        deliveries[who] = {names.get(t) or f"type {t}": q for t, q in items.items()}
    freight = None
    route_name = args.freight or (site.freight_in if site is not None else None)
    if route_name:
        route = op.freight.get(route_name.lower())
        if route is None:
            raise RuntimeError(f"no freight route '{route_name}' in the operation file - routes: "
                               f"{', '.join(sorted(op.freight)) or 'none'}")
        pickup_isk = sum((figures.min_sell.get(r["type_id"]) or 0) * r["pickup"] for r in rows)
        collateral = total_isk + pickup_isk
        freight = {"route": route.route, "m3": total_m3, "collateral": collateral,
                   "cost": route.contract_cost(total_m3, collateral),
                   "minimum_binds": route.contract_cost(total_m3, collateral) == route.minimum > 0}
    check = None
    if args.price_check and args.days > 0:
        check = price_check(client, hub, ordered, rows, held, args.days)
        for r in check["items"]:
            r["extra_m3"] = r["extra"] * m3(r["type_id"])
        extra_m3 = sum(r["extra_m3"] for r in check["items"])
        if args.cargo is not None and extra_m3 > args.cargo - total_m3 + 1e-9:
            check["notes"].append(f"the dip extras ({extra_m3:,.0f} m3) do not fit the "
                                  f"{max(0.0, args.cargo - total_m3):,.0f} m3 left in the hold")
    doc = {"generated": market.iso_utc(client.now().timestamp()), "build_station": {"id": at_id, "name": at_name},
           "hub": hub.label, "cargo_m3": args.cargo, "haul_m3": total_m3, "buy_isk": total_isk,
           "lines": [{"kind": d.kind, "label": d.label, "units": d.units, "fit": d.fit, "status": d.status,
                      "haul_m3": haul(d.need)} for d in ordered],
           "materials": rows, "price_check": check, "hints": hints, "notes": notes,
           "site": site.name if site else None, "owner": site.owner if site else "corp",
           "holdings": {k: {str(t): q for t, q in v.items()} for k, v in holdings.items()},
           "transfers": transfers, "deliveries": deliveries, "freight": freight}
    if split:
        doc["stations"] = {"manufacturing": {"id": at_id, "name": at_name},
                           "invention": {"id": inv_id, "name": inv_name}}
        doc["holdings_by_station"] = {str(station): {k: {str(t): q for t, q in v.items()} for k, v in chars.items()}
                                      for station, chars in by_station.items()}
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
        lines += ["", ("at the site's other facility or in personal hangars nearby - move it:" if split else
                       "in personal hangars nearby - move into the corporation hangar:")] + \
                 [f"  {r['name']} {r['move']:,}" for r in moves]
    if transfers:
        lines += ["", "transfers before installing (trade window, in the facility):"] + \
                 [f"  {t['from']} -> {t['to']}: {t['name']} {t['quantity']:,}"
                  + (f" (at {t['at']})" if t.get("at") else "") for t in transfers]
    if deliveries:
        lines += ["", "hand the haul to the installers:"] + \
                 [f"  {who}: " + ", ".join(f"{n} {q:,}" for n, q in sorted(items.items()))
                  for who, items in sorted(deliveries.items())]
    if freight is not None:
        lines += ["", f"courier {freight['route']}: {render.isk(freight['cost'])} ISK for "
                      f"{freight['m3']:,.0f} m3 and {render.isk(freight['collateral'])} ISK collateral"
                      + (" (the contract minimum)" if freight["minimum_binds"] else "")]
    short = [d for d in ordered if d.status != "full"]
    if short:
        lines += ["", "not covered this trip: " + "; ".join(f"{d.label} ({d.units - d.fit:,})" for d in short)]
    blocks.append("\n".join(lines))
    if check is not None:
        blocks.append(price_check_text(check, rows))
    blocks.append("\n".join(f"* {n}" for n in notes + [
        "materials are direct inputs only: a component you build yourself is its own line",
        (f"on site = every site character's own hangars where the job runs (builds: {labels[at_id]}, "
         f"inventions: {labels[inv_id]}); transfers move it to the character installing each line" if split else
         "on site = every site character's own hangars in the facility; transfers move it to the "
         "character installing each line" if personal else
         "on site = the corporation's hangars at the build station")
        + "; extras are bought in full"]))
    print("\n\n".join(blocks))
