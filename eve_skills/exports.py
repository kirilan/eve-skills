"""Extra per-character views beyond skills: standings, industry, assets, location/clones, implants.

Every command walks all stored characters unless --char selects one. Missing consent degrades to
a hint line (exit 0): granting a scope means a browser re-auth per character, so that stays the
user's decision - never triggered implicitly.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass, field

from . import esi as esi_mod, market, render, sso, universe

ACTIVITY = {1: "manufacturing", 2: "time efficiency research", 3: "material efficiency research",
            4: "copying", 5: "invention", 8: "reaction"}


def hint(name: str, feature: str) -> str:
    return (f"{name}: no {feature} consent - run: eve-skills login --scopes {feature}"
            f"  (pick '{name}' in the browser)")


def targets(args, features):
    """(client, [(token_record, public_doc)], hints).

    `features` is [(feature, required_scope)]; a character qualifies when its consent covers
    at least one of the listed scopes. public_doc is fetched only for qualifying characters -
    it carries the corporation id needed by the corp variants.
    """
    records = sso.list_characters()
    if not records:
        raise RuntimeError("not logged in - run: eve-skills login")
    wanted = sso.resolve_character(args.char) if getattr(args, "char", None) else None
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    chars, hints = [], []
    for rec in records:
        if wanted is not None and int(rec["character_id"]) != wanted:
            continue
        tok = sso.get_access_token(int(rec["character_id"]))
        name = tok.get("character_name") or str(tok["character_id"])
        granted = set(tok.get("scopes") or [])
        if not any(scope in granted for _, scope in features):
            msg = hint(name, features[0][0])
            hints.append(msg)
            if getattr(args, "csv", False):
                print(msg, file=sys.stderr)  # CSV stdout stays machine-readable
            continue
        public = client.get(f"/characters/{tok['character_id']}")
        chars.append((tok, public))
    return client, chars, hints


def corp_of(public: dict) -> int | None:
    corp = public.get("corporation_id")
    if not corp and isinstance(public.get("corporation"), dict):
        corp = public["corporation"].get("id")
    return int(corp) if corp else None


def name_or_id(names: dict[int, str], ident) -> str:
    if ident is None:
        return "-"
    return names.get(int(ident), f"id {ident}")


def _standing_entries(doc: list[dict], names: dict[int, str]) -> list[tuple[str, int, str, float]]:
    kind_labels = {"agent": "agent", "npc_corp": "npc corp", "faction": "faction"}
    entries = [
        (kind_labels.get(e.get("from_type"), e.get("from_type") or "unknown"),
         int(e["from_id"]), name_or_id(names, e["from_id"]), float(e.get("standing") or 0))
        for e in doc
    ]
    return sorted(entries, key=lambda entry: entry[2])


def cmd_standings(args):
    client, chars, hints = targets(args, [("standings", "esi-characters.read_standings.v1")])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n") if args.csv else None
    if writer:
        writer.writerow(["character", "kind", "from_id", "name", "standing"])
    blocks = list(hints)
    for tok, public in chars:
        doc = client.get(f"/characters/{tok['character_id']}/standings", token=tok["access_token"])
        ids = {int(entry["from_id"]) for entry in doc}
        names = esi_mod.resolve_names(client, ids) if ids else {}
        entries = _standing_entries(doc, names)
        cname = public.get("name") or str(tok["character_id"])
        if writer:
            for kind, fid, fname, standing in entries:
                writer.writerow([cname, kind, fid, fname, f"{standing:+.2f}"])
        else:
            table = render.table(["kind", "name", "standing"],
                                 [[k, n, f"{s:+.2f}"] for k, _, n, s in entries]) if entries else "(no standings recorded)"
            blocks.append(f"{cname} (id {tok['character_id']})\n{table}")
    if writer:
        sys.stdout.write(buf.getvalue())
    else:
        print("\n\n".join(blocks))


def _job_rows(jobs, now):
    """Rows with raw ids in columns 2/5; the caller resolves names."""
    ids = {int(j["output_type_id"]) for j in jobs if j.get("output_type_id")}
    ids |= {int(j["installed_in"]) for j in jobs if j.get("installed_in")}
    rows = []
    for j in sorted(jobs, key=lambda j: j.get("finish_date") or "9999"):
        finish = render.parse_opt(j.get("finish_date"))
        if j.get("status") == "active" and finish:
            time_left = f"{render.format_duration(max((finish - now).total_seconds(), 0))} left"
        elif finish:
            time_left = finish.strftime("%b %d %H:%M")
        else:
            time_left = "-"
        runs = (f"{j.get('installed_runs', '?')}/{j.get('runs', '?')}"
                if j.get("runs") or j.get("installed_runs") else "-")
        rows.append([j.get("status", "?"), ACTIVITY.get(j.get("activity"), f"activity {j.get('activity')}"),
                     int(j.get("output_type_id") or 0), runs, time_left, int(j.get("installed_in") or 0)])
    return rows, ids


def cmd_jobs(args):
    scope = "esi-industry.read_corporation_jobs.v1" if args.corp else "esi-industry.read_character_jobs.v1"
    client, chars, hints = targets(args, [("jobs", scope)])
    now = client.now()
    blocks, failures, all_rows = list(hints), [], []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        try:
            if args.corp:
                corp_id = corp_of(public)
                if not corp_id:
                    failures.append(f"{cname}: no corporation id on the public record")
                    continue
                path = f"/corporations/{corp_id}/industry/jobs"
                if args.completed:
                    path += "?include_completed=true"
                jobs = client.get_all(path, token=tok["access_token"])
            else:
                path = f"/characters/{cid}/industry/jobs"
                if args.completed:
                    path += "?include_completed=true"
                jobs = client.get(path, token=tok["access_token"])
        except esi_mod.AuthError as err:
            failures.append(f"{cname}: ESI refused ({err}) - corporation endpoints need the matching director/Account-Manager role")
            continue
        rows, ids = _job_rows(jobs, now)
        names = esi_mod.resolve_names(client, ids) if ids else {}
        for r in rows:
            r[2] = name_or_id(names, r[2] or None)
            r[5] = name_or_id(names, r[5] or None)
        all_rows.append((cname, rows))
    for line in failures:
        print(f"warning: {line}", file=sys.stderr)
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "status", "activity", "product", "runs", "time", "installed_in"])
        for cname, rows in all_rows:
            for r in rows:
                writer.writerow([cname] + r)
    else:
        for cname, rows in all_rows:
            table = render.table(["status", "activity", "product", "runs", "time", "installed in"], rows) if rows else "(no jobs)"
            blocks.append(f"{cname}\n{table}")
        print("\n\n".join(blocks))


# ---------------------------------------------------------------------------
# inventory: named, placed, valued
# ---------------------------------------------------------------------------

# The nine leading CSV columns are the historical header: their positions and meanings are fixed,
# so everything this view adds is appended behind them. `item_name` keeps meaning exactly what it
# always meant - the name of `type_id` - and a player's own label for a singleton gets its own
# column rather than quietly replacing it.
INVENTORY_CSV_COLUMNS = [
    "character", "item_id", "type_id", "item_name", "quantity", "singleton", "flag",
    "location_id", "location_name",
    "group_name", "category_name", "custom_name", "location_path", "location_kind",
    "price_basis", "price_scope", "unit_price", "value", "unpriced_types",
]
SUMMARY_COLUMNS = {
    "location": ["location", "category", "types", "units", "value"],
    "category": ["category", "location", "types", "units", "value"],
}
ITEMS_COLUMNS = ["item", "group", "category", "qty", "location", "unit price", "value"]
PATH_SEPARATOR = " > "
REFERENCE_BASIS = "esi_reference"
BOOK_BASIS = "max_buy"
REFERENCE_LABEL = ("ESI's published reference price - a figure CCP publishes about an item, "
                   "not an order anybody will fill")


def _isk(value) -> str:
    """ISK with separators, or `-`. A dash admits ESI gave no figure for this basis; `0.00` would
    claim the item is worthless, and those are different statements about somebody's holding."""
    return "-" if value is None else f"{value:,.2f}"


def _ident(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quantity(asset: dict) -> int:
    quantity = _ident(asset.get("quantity", 1))
    return 1 if quantity is None else quantity


@dataclass(frozen=True)
class Valuation:
    """What one run priced its holdings with, and what asking for it cost.

    Two bases exist because the two questions are different questions: "what does CCP publish" is
    one document for the whole cluster and cannot be traded at, while "what would dumping this pay
    today" costs one order book per distinct type - or nothing at all while the last run's figures
    are still inside ESI's stated expiry - and answers with money somebody will hand over.
    `requests` is what this run actually sent, so a warm repeat reports 0, and `cached_figures`
    names the types that came off disk instead: "free" and "already paid for" are not the same
    reason. `unit` carries only the types this basis could price - an absent type is not worth zero,
    it is worth nothing ESI would say, and every total has to skip it out loud."""

    key: str
    label: str
    short: str
    unit: dict[int, float]
    requests: int
    requests_note: str
    alt_label: str | None = None
    alt_unit: dict[int, float] = field(default_factory=dict)
    freshness: str | None = None
    scope: dict | None = None
    failed_books: int = 0
    cached_figures: int = 0

    @property
    def scope_label(self) -> str:
        return self.scope["label"] if self.scope else ""


def _reference_valuation(client, type_ids: set[int], now: float) -> Valuation:
    """ESI's published reference for every held type, bought with exactly one request.

    `average_price` is the figure; CCP's industry `adjusted_price` only stands in when a row has no
    average. A published 0.0 is a price like any other (PLEX's adjusted really is zero live), so a
    type counts as priced whenever the document holds a number for it: absent and zero are not the
    same statement, and only one of them belongs in the unpriced footnote."""
    if not type_ids:
        return Valuation(key=REFERENCE_BASIS, label=REFERENCE_LABEL, short="ESI reference",
                         unit={}, requests=0, requests_note="no request: nothing was held")
    table = market.price_table(client)
    unit: dict[int, float] = {}
    for type_id in type_ids:
        reference = table.reference(type_id)
        if reference is None:
            continue
        price = reference.average_price
        if price is None:
            price = reference.adjusted_price
        if price is not None:
            unit[type_id] = price
    return Valuation(key=REFERENCE_BASIS, label=REFERENCE_LABEL, short="ESI reference", unit=unit,
                     requests=1, requests_note="1 request for ESI's whole price document",
                     freshness=market.reference_freshness_line(table.meta, now))


def _valuation_scope(client, spec: str) -> market.Scope:
    """Where `--value-at` reads orders from: a named hub's station, else a region by name or id.

    Resolved before any asset traffic, because paying for every page of a character's holdings only
    to be told the scope name was wrong is a bad afternoon."""
    text = str(spec).strip()
    if not text:
        raise RuntimeError("empty --value-at: name a hub "
                           f"({', '.join(market.HUBS)}), a region by exact name, or its id")
    hub = market.HUBS.get(text.lower())
    if hub is not None:
        return market.Scope(hub.region_id, hub.label, hub.system_id, hub.station_id)
    region_id, name = market.resolve_region(client, text)
    return market.Scope(region_id, name)


# A book fan-out runs at about five books a second through the transport's default 8 workers on a
# real holding, measured end to end the same day: 518 distinct types held read cold in 102 s, of
# which all but a couple was the fan-out. Earlier sampling of 60 popular types suggested nine a
# second; the whole holding is slower because its books are bigger, and pushing those same 60
# through 24 workers was slower still, so concurrency is not the lever here. The pre-flight estimate
# is quoted from that rate rather than invented per machine, and only once it crosses ten seconds -
# below that the wait needs no explaining, and a notice about nothing teaches the user to stop
# reading notices.
BOOK_SECONDS_PER_TYPE = 0.19
NOTICE_MIN_SECONDS = 10.0


def _valuation_notice(info: market.Preflight) -> str:
    """The line printed before a book fan-out starts, so a long wait is not read as a hang.

    Counts rather than a progress bar: what the silence is buying, and roughly how long it lasts.
    When the cache already holds every figure the run is instant, and saying that out loud is the
    difference between an answer and an app that appears to be doing nothing."""
    kinds = "" if info.types == 1 else "s"
    if not info.fetches:
        return (f"pricing {info.types} distinct type{kinds} held: every figure already in the local "
                f"quote cache, so no order book is read")
    line = (f"pricing {info.types} distinct type{kinds} held: {info.fetches} order "
            f"book{'s' if info.fetches != 1 else ''} to read, one per type")
    if info.cached:
        line += f" ({info.cached} already priced from the last run)"
    seconds = info.fetches * BOOK_SECONDS_PER_TYPE
    if seconds >= NOTICE_MIN_SECONDS:
        line += f"; about {market.format_age(seconds)} at this size"
    return line


def _book_requests_note(figures: market.BookFigures) -> str:
    """What the money column cost this run, with the cached half named apart from the read half."""
    read = f"{figures.fetched} order-book request{'s' if figures.fetched != 1 else ''}"
    if not figures.cached:
        return f"{read}, one per distinct type held"
    return f"{read} now; {figures.cached} types served from the local quote cache"


def _book_valuation(client, type_ids: set[int], scope: market.Scope, preflight=None) -> Valuation:
    """One regional book per distinct type the cache cannot answer, valued at the richest bid.

    `max_buy` is what dumping the holding into standing buy orders at the scope pays right now;
    `min_sell` comes out of the same rows, so the alternative (listing it yourself) is free rather
    than a second fan-out. A hub means that station's orders only - the same reading `market quote`
    gives for "the price at Jita 4-4" - because money you cannot reach is not a valuation.

    Figures still inside ESI's own stated expiry come from `market.book_figures`' cache rather than
    the wire, so looking at the same holding twice costs nothing; the freshness line still names the
    oldest `Last-Modified` of every figure folded into the total, however it arrived."""
    figures = market.book_figures(client, type_ids, scope, preflight=preflight)
    return Valuation(
        key=BOOK_BASIS,
        label=f"the richest standing buy order at {scope.label} - what dumping the holding there "
              f"pays right now",
        short=f"max buy @ {scope.label}", unit=figures.max_buy,
        requests=figures.fetched, requests_note=_book_requests_note(figures),
        alt_label=f"{scope.label}'s cheapest standing ask",
        alt_unit=figures.min_sell, failed_books=figures.failed, cached_figures=figures.cached,
        freshness=(f"freshness: {market.freshness_line(figures.meta, client.now().timestamp())}"
                   if figures.answered else None),
        scope={"region_id": scope.region_id, "label": scope.label,
               "system_id": scope.system_id, "location_id": scope.location_id})


def location_chain(places: dict, ident) -> list[universe.Location]:
    """Root-first path to `ident`: station, then ship, then container.

    Parent links are acyclic by construction (see `universe.resolve_locations`), so the walk ends;
    the visited set is only insurance for a map a caller built itself, and it costs one comparison
    per hop. A place missing from the map ends the chain with an honest `location <id>` label."""
    chain: list[universe.Location] = []
    seen: set[int] = set()
    while ident is not None and ident not in seen:
        seen.add(ident)
        place = places.get(ident)
        if place is None:
            chain.append(universe.Location(location_id=ident, kind="other",
                                           name=f"location {ident}"))
            break
        chain.append(place)
        ident = place.parent_id
    return list(reversed(chain))


def _unnamed_structure(place) -> bool:
    """True for `resolve_locations`' fallback label, which is what the consent notice counts.

    The module has no other way to say "I could not ask": a structure this token may not probe is
    labelled `structure <id>`, and that exact string is the only observable."""
    return place.kind == "structure" and place.name == f"structure {place.location_id}"


@dataclass
class InventoryOwner:
    """One owner's asset rows, their places, and the views built from both."""

    name: str
    character_id: int
    token: str
    corp_id: int | None
    assets: list[dict]
    places: dict[int, universe.Location] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)


def _inventory_rows(assets, places, infos, basis) -> list[dict]:
    """Every asset row named, placed and priced: the one model all three views render from.

    A singleton's own display name comes from its place entry - `resolve_locations` fills that with
    the player's label when there is one - so `Nightwatch (Rifter)` says both what she calls it and
    what it is. A plain quantity row has no such entry and keeps its type name."""
    rows = []
    for asset in assets:
        type_id = _ident(asset.get("type_id")) or 0
        item_id = _ident(asset.get("item_id")) or 0
        info = infos.get(type_id)
        type_name = info.name if info else f"type {type_id}"
        own = places.get(item_id)
        custom = own.name if own is not None and own.name != type_name else None
        chain = location_chain(places, _ident(asset.get("location_id")))
        quantity = _quantity(asset)
        unit = basis.unit.get(type_id)
        rows.append({
            "item_id": item_id, "type_id": type_id, "type_name": type_name,
            "group_name": info.group_name if info else "unknown",
            "category_name": info.category_name if info else "unknown",
            "custom_name": custom,
            "display": f"{custom} ({type_name})" if custom else type_name,
            "quantity": quantity, "singleton": bool(asset.get("is_singleton")),
            # `location_flag` is what `/assets` actually calls it; the CSV column keeps its old name.
            "flag": asset.get("location_flag") or "",
            "location_id": asset.get("location_id"),
            "chain": chain,
            "place": chain[-1] if chain else None,
            "top": chain[0] if chain else None,
            "path": PATH_SEPARATOR.join(place.name for place in chain) or "-",
            "unit_price": unit,
            "value": None if unit is None else unit * quantity,
        })
    return rows


class _Tally:
    """Types, units and priced ISK of one group. `value` stays None until something is priced, so
    a wholly unpriced group prints a dash instead of claiming it is worth nothing."""

    def __init__(self) -> None:
        self.types: set[int] = set()
        self.units = 0
        self.value: float | None = None

    def add(self, row: dict) -> None:
        self.types.add(row["type_id"])
        self.units += row["quantity"]
        if row["value"] is not None:
            self.value = (self.value or 0.0) + row["value"]

    def cells(self) -> dict:
        return {"types": len(self.types), "units": self.units, "value": self.value}


def _inventory_groups(rows: list[dict], by: str) -> list[dict]:
    """Outer sections with subtotals and inner rows - the same aggregation read two ways.

    `location` groups by the root of each asset's path (the station, structure or system it ends up
    in) and breaks that down by category; `category` is that table transposed. Location cells always
    carry the full nested path, so a row reads on its own whichever way round the table is turned."""
    outer: dict[tuple, dict] = {}
    for row in rows:
        if by == "category":
            outer_key = (row["category_name"].lower(), "")
            inner_key = (row["path"].lower(), "")
            head, sub = row["category_name"], row["path"]
        else:
            top = row["top"]
            outer_key = ((top.name.lower() if top else "-"), _ident(top.location_id) if top else 0)
            inner_key = (row["category_name"].lower(), "")
            head, sub = (top.name if top else "-"), row["category_name"]
        group = outer.get(outer_key)
        if group is None:
            group = outer[outer_key] = {"name": head, "tally": _Tally(), "entries": {}}
        entry = group["entries"].get(inner_key)
        if entry is None:
            entry = group["entries"][inner_key] = {"name": sub, "tally": _Tally()}
        entry["tally"].add(row)
        group["tally"].add(row)
    groups = []
    for key in sorted(outer):
        group = outer[key]
        entries = [{"name": group["entries"][k]["name"], **group["entries"][k]["tally"].cells()}
                   for k in sorted(group["entries"])]
        groups.append({"name": group["name"], "entries": entries, **group["tally"].cells()})
    return groups


def _owner_totals(rows: list[dict]) -> dict:
    """What one owner holds, with unpriced types counted rather than valued at zero.

    `units` is everything held; `priced_units` is only what the money figure covers. Collapsing the
    two would let a total claim to cover units that were deliberately left out of it."""
    priced = [row for row in rows if row["value"] is not None]
    return {"types": len({row["type_id"] for row in rows}),
            "units": sum(row["quantity"] for row in rows),
            "value": sum(row["value"] for row in priced) if priced else None,
            "priced_units": sum(row["quantity"] for row in priced),
            "priced_types": len({row["type_id"] for row in priced}),
            "unpriced_types": len({row["type_id"] for row in rows if row["value"] is None})}


def _sorted_items(rows: list[dict]) -> list[dict]:
    """Dearest first, every unpriced row behind every priced one, names breaking ties."""
    return sorted(rows, key=lambda row: (row["value"] is None, -(row["value"] or 0.0),
                                         row["display"].lower()))


def _owner_table(owner: InventoryOwner, by: str, items: bool) -> str:
    """One owner's table: the grouped view with a subtotal per section, or one row per item."""
    if items:
        table_rows = [[row["display"], row["group_name"], row["category_name"],
                       f"{row['quantity']:,}", row["path"], _isk(row["unit_price"]),
                       _isk(row["value"])] for row in _sorted_items(owner.rows)]
    else:
        table_rows = []
        for group in _inventory_groups(owner.rows, by):
            for entry in group["entries"]:
                table_rows.append([group["name"], entry["name"], f"{entry['types']:,}",
                                   f"{entry['units']:,}", _isk(entry["value"])])
            table_rows.append([f"subtotal {group['name']}", "", f"{group['types']:,}",
                               f"{group['units']:,}", _isk(group["value"])])
    return render.table(ITEMS_COLUMNS if items else SUMMARY_COLUMNS[by], table_rows)


def _totals_line(basis: Valuation, totals: dict) -> str:
    """One owner's grand total, with the basis inside its own label.

    A bare ISK figure is a rumour: which of the two bases produced it, how much of the holding it
    covered, and what it deliberately left out all have to travel with the number."""
    if totals["value"] is None:
        return (f"TOTAL ({basis.short}): nothing priced on this basis "
                f"({totals['types']} distinct types held)")
    line = (f"TOTAL ({basis.short}): {_isk(totals['value'])} ISK over {totals['priced_units']:,} "
            f"units of {totals['priced_types']} distinct types")
    if totals["unpriced_types"]:
        extra = "s" if totals["unpriced_types"] != 1 else ""
        line += f"; {totals['unpriced_types']} more type{extra} held, none priced"
    return line


def _valuation_notes(basis: Valuation, priced: int, unpriced: list[str],
                     alt_line: str | None) -> list[str]:
    """What the money column is, how old it is, and exactly what it left out."""
    notes = [f"value basis: {basis.label}"]
    if basis.freshness:
        notes.append(basis.freshness)
    if alt_line:
        notes.append(alt_line)
    notes.append(f"priced {priced} of {priced + len(unpriced)} distinct types held "
                 f"({basis.requests_note}).")
    if unpriced:
        notes.append(f"no price on this basis, excluded from every total above "
                     f"({len(unpriced)}): {', '.join(unpriced)}")
    if basis.failed_books:
        notes.append(f"{basis.failed_books} of those books did not answer; their types are counted "
                     f"as unpriced above, not as worthless")
    return notes


def _structure_notice(count: int) -> str:
    """The one line that turns a `structure 1048236548577` cell into something the user can fix."""
    return (f"{count} structure name{'s' if count != 1 else ''} could not be resolved: ESI answers "
            f"/universe/structures only with the structures consent - run: eve-skills login "
            f"--scopes structures  (pick the character in the browser)")


def _place_doc(place) -> dict:
    return {"id": place.location_id, "name": place.name, "kind": place.kind}


def _item_doc(row: dict) -> dict:
    """One asset with ids and names both present: a machine reader should never have to resolve
    anything afterwards, and never have to guess which of the two names it is looking at."""
    return {"item_id": row["item_id"], "type_id": row["type_id"], "name": row["display"],
            "type_name": row["type_name"], "custom_name": row["custom_name"],
            "group_name": row["group_name"], "category_name": row["category_name"],
            "quantity": row["quantity"], "singleton": row["singleton"], "flag": row["flag"] or None,
            "location_id": row["location_id"],
            "location_kind": row["place"].kind if row["place"] else None,
            "location_path": [_place_doc(place) for place in row["chain"]],
            "unit_price": row["unit_price"], "value": row["value"]}


def cmd_inventory(args):
    """Assets with their real names, grouped by where they are and what they are, and valued.

    Type ids go to `universe.type_info` and location ids to `universe.resolve_locations`, never to
    each other. That separation is the whole bug this command used to have: `/universe/names`
    answers 400 for an entire batch as soon as one id overflows int32, and container and structure
    ids are all bigger than that - so mixing them in lost every name in the batch, not just the
    offending row."""
    by = getattr(args, "by", None) or "location"
    items = bool(args.items)
    machine = bool(getattr(args, "json", False))
    value_at = getattr(args, "value_at", None)
    consent = ("esi-assets.read_corporation_assets.v1" if args.corp
               else "esi-assets.read_assets.v1")
    client, chars, hints = targets(args, [("assets", consent)])
    # Resolved before the first asset page: a mistyped scope should cost one lookup, not a haul.
    value_scope = _valuation_scope(client, value_at) if value_at is not None else None

    owners: list[InventoryOwner] = []
    failures: list[str] = []
    for tok, public in chars:
        cid = int(tok["character_id"])
        cname = public.get("name") or str(cid)
        corp_id = corp_of(public) if args.corp else None
        if args.corp and corp_id is None:
            failures.append(f"{cname}: no corporation id on the public record")
            continue
        path = f"/corporations/{corp_id}/assets" if corp_id else f"/characters/{cid}/assets"
        try:
            assets = client.get_all(path, token=tok["access_token"])
        except esi_mod.AuthError as err:
            # A personal refusal and a corporate one have different fixes, and pointing at the wrong
            # one sends the owner hunting for a role they already hold.
            why = ("corporation assets need the director/Account-Manager role for that corp"
                   if args.corp else "the assets consent may no longer be granted")
            failures.append(f"{cname}: ESI refused ({err}) - {why}")
            continue
        owners.append(InventoryOwner(cname, cid, tok["access_token"], corp_id, list(assets)))

    held = {ident for owner in owners for asset in owner.assets
            if (ident := _ident(asset.get("type_id"))) is not None}
    # The catalogue first, on purpose: `resolve_locations` reads the same type records to tell a
    # ship from a container, so warming them here turns its lookup into a cache hit instead of a
    # second fan-out over ids this run has already fetched.
    infos = universe.type_info(client, held) if held else {}
    for owner in owners:
        who = ({"corporation_id": owner.corp_id} if owner.corp_id
               else {"character_id": owner.character_id})
        owner.places = universe.resolve_locations(client, owner.assets, token=owner.token, **who)

    def announce(info: market.Preflight) -> None:
        """Say what the valuation is about to cost, while there is still time to say it. Machine
        output keeps prose off both streams, exactly as the failure notes below do."""
        if not machine:
            print(_valuation_notice(info), file=sys.stderr)

    now = client.now().timestamp()
    basis = (_book_valuation(client, held, value_scope, preflight=announce)
             if value_scope is not None and held else _reference_valuation(client, held, now))

    blind: set[int] = set()
    for owner in owners:
        owner.rows = _inventory_rows(owner.assets, owner.places, infos, basis)
        blind.update(place.location_id for place in owner.places.values() if _unnamed_structure(place))

    unpriced = sorted((infos[ident].name if ident in infos else f"type {ident}")
                      for ident in held if ident not in basis.unit)
    alt_total, alt_types = 0.0, set()
    for owner in owners:
        for row in owner.rows:
            unit = basis.alt_unit.get(row["type_id"])
            if unit is not None:
                alt_total += unit * row["quantity"]
                alt_types.add(row["type_id"])
    alt_line = (f"listing the same holdings at {basis.alt_label} would raise {_isk(alt_total)} ISK "
                f"over {len(alt_types)} types") if basis.alt_label and alt_types else None
    notes = (_valuation_notes(basis, len(held) - len(unpriced), unpriced, alt_line)
             + ([_structure_notice(len(blind))] if blind else [])) if held else []

    if not machine:     # --json carries these in `warnings`; prose would break a parser
        for line in failures:
            print(f"warning: {line}", file=sys.stderr)
    if machine:
        print(json.dumps({
            "generated": market.iso_utc(now),
            "owner_kind": "corporation" if args.corp else "character",
            "grouped_by": by,
            "value_basis": {"key": basis.key, "label": basis.label, "short": basis.short,
                            "scope": basis.scope, "requests": basis.requests,
                            "priced_types": len(held) - len(unpriced), "unpriced_types": unpriced,
                            "failed_books": basis.failed_books,
                            # the prose lines carry a "freshness: " label; a field already named
                            # freshness does not need it repeated inside its own value
                            "freshness": None if basis.freshness is None else
                            basis.freshness.removeprefix("freshness: "),
                            "alternative": None if basis.alt_label is None else
                            {"label": basis.alt_label, "value": alt_total if alt_types else None,
                             "types": len(alt_types)},
                            "cached_figures": basis.cached_figures},
            "hints": hints,
            "warnings": failures,
            "characters": [{"character_id": owner.character_id, "name": owner.name,
                            "asset_rows": len(owner.assets), "totals": _owner_totals(owner.rows),
                            "groups": _inventory_groups(owner.rows, by),
                            "items": [_item_doc(row) for row in _sorted_items(owner.rows)]}
                           for owner in owners],
        }, indent=2))
        return
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(INVENTORY_CSV_COLUMNS)
        for owner in owners:
            unpriced_here = _owner_totals(owner.rows)["unpriced_types"]
            for row in owner.rows:
                place = row["place"]
                writer.writerow([owner.name, row["item_id"], row["type_id"], row["type_name"],
                                 row["quantity"], int(row["singleton"]), row["flag"],
                                 row["location_id"], place.name if place else "",
                                 row["group_name"], row["category_name"], row["custom_name"] or "",
                                 row["path"], place.kind if place else "", basis.key,
                                 basis.scope_label, row["unit_price"], row["value"],
                                 unpriced_here])
        for line in notes:      # the footnotes matter; they just may not pollute a CSV pipe
            print(line, file=sys.stderr)
        return
    blocks = list(hints)
    for owner in owners:
        head = f"{owner.name} ({len(owner.assets):,} asset rows)"
        if not owner.rows:
            blocks.append(f"{head}\n(inventory empty)")
            continue
        blocks.append(f"{head}\n{_owner_table(owner, by, items)}\n"
                      f"{_totals_line(basis, _owner_totals(owner.rows))}")
    if notes:
        blocks.append("\n".join(notes))
    print("\n\n".join(blocks))


def cmd_travel(args):
    client, chars, hints = targets(args, [("clones", "esi-clones.read_clones.v1"),
                                          ("location", "esi-location.read_location.v1")])
    blocks, csv_rows = list(hints), []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        granted = set(tok.get("scopes") or [])
        lines = [f"{cname} (id {cid})"]
        loc = None
        if "esi-location.read_location.v1" in granted:
            try:
                loc = client.get(f"/characters/{cid}/location", token=tok["access_token"]) or {}
            except esi_mod.AuthError as err:
                lines.append(f"  location: ESI refused ({err})")
        else:
            lines.append("  " + hint(cname, "location"))
        clones_doc = {}
        try:
            clones_doc = client.get(f"/characters/{cid}/clones", token=tok["access_token"]) or {}
        except esi_mod.AuthError as err:
            lines.append(f"  clones: ESI refused ({err})")
        ids = set()
        if loc:
            ids |= {int(v) for v in (loc.get("solar_system_id"), loc.get("station_id"), loc.get("structure_id")) if v}
        home = clones_doc.get("home_location") or {}
        if home:
            ids.add(int(home["location_id"]))
        jump_clones = clones_doc.get("jump_clones") or []
        for jc in jump_clones:
            ids.add(int(jc["location_id"]))
            ids |= {int(i) for i in (jc.get("implants") or [])}
        names = esi_mod.resolve_names(client, ids) if ids else {}
        where = name_or_id(names, loc.get("station_id") or loc.get("structure_id")) if loc else "-"
        system = name_or_id(names, loc.get("solar_system_id")) if loc else "-"
        if loc:
            lines.append(f"  current: {where}" + (f" ({system})" if where not in ("-", system) else ""))
        if home:
            lines.append(f"  home: {name_or_id(names, home['location_id'])}")
        if clones_doc.get("last_clone_jump_date"):
            lines.append(f"  last clone jump: {render.parse_ts(clones_doc['last_clone_jump_date']).strftime('%Y-%m-%d %H:%M')} UTC")
        if jump_clones:
            rows = []
            for jc in sorted(jump_clones, key=lambda j: j.get("name") or ""):
                imp = ", ".join(name_or_id(names, i) for i in (jc.get("implants") or [])) or "-"
                rows.append([jc.get("name") or "(unnamed)", name_or_id(names, jc["location_id"]), imp])
            lines.append(render.table(["jump clone", "location", "implants"], rows))
        if loc:
            csv_rows.append([cname, "current", where if where != "-" else system, "", ""])
        if home:
            csv_rows.append([cname, "home", name_or_id(names, home["location_id"]), "", ""])
        for jc in jump_clones:
            imp = ", ".join(name_or_id(names, i) for i in (jc.get("implants") or []))
            csv_rows.append([cname, "jump clone", name_or_id(names, jc["location_id"]), jc.get("name") or "(unnamed)", imp])
        blocks.append("\n".join(lines))
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "kind", "location", "clone_name", "implants"])
        writer.writerows(csv_rows)
    else:
        print("\n\n".join(blocks))


def cmd_implants(args):
    client, chars, hints = targets(args, [("clones", "esi-clones.read_implants.v1")])
    blocks, csv_rows = list(hints), []
    for tok, public in chars:
        cid = tok["character_id"]
        cname = public.get("name") or str(cid)
        doc = client.get(f"/characters/{cid}/implants", token=tok["access_token"])
        ids = doc.get("implants") if isinstance(doc, dict) else doc
        ids = [int(i) for i in (ids or [])]
        names = esi_mod.resolve_names(client, set(ids)) if ids else {}
        # one row per implant instance: the same type can be fitted in both head slots
        rows = [[names.get(i, f"id {i}")] for i in sorted(ids, key=lambda i: names.get(i, ""))]
        csv_rows.extend([cname, r[0]] for r in rows)
        table = render.table(["implant"], rows) if rows else "(no implants fitted)"
        blocks.append(f"{cname} (id {cid})\n{table}")
    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["character", "implant"])
        writer.writerows(csv_rows)
    else:
        print("\n\n".join(blocks))
