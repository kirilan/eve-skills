"""What an item is, and where it actually sits: the type catalogue and the location resolver.

ESI hands out names a few ids at a time and nothing else. Asset rows are therefore just numbers -
a `type_id`, a `location_id`, a `location_type` - and turning them into a readable inventory needs
two things this module owns: `type_info`, which says what a type is called and which group and
category it belongs to, and `resolve_locations`, which says which station, structure, ship or
container an asset row is really in by following the chain from a fitted module up through the
cargo hold, the hull and into the hangar.

Everything here works from ESI alone - no SDE download - and caches what it learns permanently in
``$XDG_CACHE_HOME/eve-skills/types.json``, beside the ``names.json`` that `esi.resolve_names` owns.
Type, group and category data does not change, so a cold run pays one request per unseen id in
three fanned-out waves (the types, then the distinct groups they name, then the distinct categories
those groups name) and every later run is served from disk with no request at all. Groups and
categories are cached as their own sections because there are far fewer of them than types: a newly
met type in an already-known group costs exactly one request.

What is deliberately *not* cached is the personal, mutable half: custom item names and the
structures only the owner's own token can see. Both change the moment a ship is renamed or a citadel
repacked, and the asset rows this resolver needs already came from a live call, so two small
requests per run are cheaper than serving a stale label forever. Everything immutable stays
cached: a warm run makes no type, group, category, station or system request whatsoever.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import esi as esi_mod, paths, sso, storage

# Shape version of types.json. Bump it when the layout below changes: a reader that does not
# recognise the document discards it wholesale rather than mis-reading an old field as a new one.
CACHE_VERSION = 1
DOC_NAME = "types.json"
LOCK_NAME = "types.lock"

# Shown for a group or category ESI would not name. Deliberately different from the `type <id>`
# fallback: an id-derived name is only honest for the id that was actually asked about.
UNKNOWN = "unknown"

# `/universe/names` validates `ids` as int32 and answers 400 for the *whole batch* when one id
# overflows it, and 404 for the whole batch when one id is not a nameable universe id (both
# verified against live ESI on 2026-09-08). Item-derived ids - structures, ships, containers -
# are far above this bound, so they must never join a names request: one citadel would cost every
# station name in the batch.
INT32_MAX = 2 ** 31 - 1

# The assets/names endpoints take at most 1000 ids per call.
ASSET_NAMES_CHUNK = 1000

# Longest parent chain followed before it is declared malformed. Real chains are three or four
# deep (module -> hold -> hull -> station); ESI has shipped cycles in asset locations before, and
# an unbounded walk turns one of those into a hang on a ten-thousand-row inventory.
MAX_CHAIN_HOPS = 64

STRUCTURE_SCOPE = "esi-universe.read_structures.v1"
SHIP_CATEGORY = "Ship"


@dataclass(frozen=True)
class TypeInfo:
    """One item type, named and classified.

    A type ESI refused or returned malformed degrades to `type <id>` with both names `unknown`
    and no volumes; the id is left out of the cache so the next run asks again. `packaged_volume`
    holds the effective number (ESI documents it as defaulting to `volume`) so a caller doing
    freight maths never has to know which of the two fields ESI actually sent."""

    type_id: int
    name: str
    group_id: int | None
    group_name: str
    category_id: int | None
    category_name: str
    volume: float | None
    packaged_volume: float | None


@dataclass(frozen=True)
class Location:
    """One place an asset can be, or one item that acts as a place.

    `kind` is what the inventory view groups on: `station`, `structure`, `system` (loose in space),
    `container`, `ship`, or `other` for anything ESI labels as neither (`location_type: other`).
    `parent_id` is set for a container/ship to the id it sits in, so a caller renders
    `Jita - Mradd > My Freighter > Cargo` by walking `locations[loc.parent_id]`; parent links are
    acyclic by construction, so that walk always terminates. `system_id`/`system_name` are filled
    when ESI says - a `solar_system` location or a structure probe - and propagated down the chain."""

    location_id: int
    kind: str
    name: str
    system_id: int | None = None
    system_name: str | None = None
    parent_id: int | None = None


# ---------------------------------------------------------------------------
# value parsing (shared by live payloads and cached records)
# ---------------------------------------------------------------------------

def _as_int(value) -> int | None:
    """An ESI id as an int, or None when it is missing or not a number.

    bool is rejected on purpose: `True` compares equal to 1 and would silently become type 1."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)   # rows reloaded from CSV/JSON carry ids as text
    return None


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _parse_type(payload) -> dict | None:
    """A `/universe/types/{id}` body as a cache record, or None when it is not usable."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    volume = _as_float(payload.get("volume"))
    packaged = _as_float(payload.get("packaged_volume"))
    return {"name": name, "group_id": _as_int(payload.get("group_id")), "volume": volume,
            "packaged_volume": packaged if packaged is not None else volume}


def _parse_group(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return {"name": name, "category_id": _as_int(payload.get("category_id"))}


def _parse_category(payload) -> dict | None:
    """Categories carry nothing the inventory needs beyond their name, so the record *is* the name."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    return {"name": name} if isinstance(name, str) and name.strip() else None


# ---------------------------------------------------------------------------
# types.json
# ---------------------------------------------------------------------------

def doc_path(cache_dir: str | None = None) -> str:
    """Where the catalogue lives: `types.json` beside `names.json`."""
    return os.path.join(cache_dir or paths.cache_dir(), DOC_NAME)


def _empty_doc() -> dict:
    return {"version": CACHE_VERSION, "types": {}, "groups": {}, "categories": {}}


def read_doc(cache_dir: str | None = None) -> dict:
    """types.json as three id->record maps; unreadable, foreign or corrupt reads as empty.

    Every record goes through the same parser that validates a live response, so a hand-edited or
    half-written file costs a refetch of the affected ids instead of an AttributeError in the
    middle of a command."""
    doc = _empty_doc()
    path = doc_path(cache_dir)
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (FileNotFoundError, ValueError):
        return doc
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return doc
    for section, parse in (("types", _parse_type), ("groups", _parse_group),
                           ("categories", _parse_category)):
        entries = raw.get(section)
        if not isinstance(entries, dict):
            continue
        for key, value in entries.items():
            ident, record = _as_int(key), parse(value)
            if ident is not None and record is not None:
                doc[section][str(ident)] = record
    return doc


def _publish(cache_dir: str | None, learned: dict[str, dict]) -> None:
    """Add this run's new records to types.json without losing anybody else's.

    The re-read happens inside the lock on purpose, exactly as in `esi.resolve_names`: another
    process may have cached other ids since this one read the file, and publishing our own view of
    the document would silently drop them. Nothing is written when nothing was learned, so a warm
    run leaves the file (and its mtime) alone."""
    if not any(learned.values()):
        return
    path = doc_path(cache_dir)
    with storage.file_lock(os.path.join(os.path.dirname(path) or ".", LOCK_NAME)):
        doc = read_doc(cache_dir)
        for section, records in learned.items():
            doc[section].update(records)
        storage.atomic_write_json(path, doc)


def _fetch(client, template: str, ids: Sequence[int], token: str | None = None) -> dict[int, object]:
    """One fanned-out batch of `/…/{id}` GETs; refused or broken ids simply come back absent.

    Failures arrive as values from `get_many` (one bad id must not sink the pool), and a value that
    is not a usable payload is dropped here rather than cached, so the next run retries it."""
    if not ids:
        return {}
    by_path = {template.format(id=i): i for i in ids}
    out: dict[int, object] = {}
    for path, payload in client.get_many(list(by_path), token=token).items():
        if not isinstance(payload, Exception):
            out[by_path[path]] = payload
    return out


# ---------------------------------------------------------------------------
# what a type is
# ---------------------------------------------------------------------------

def type_info(client, type_ids: Iterable[int], cache_dir: str | None = None) -> dict[int, TypeInfo]:
    """Every requested `type_id` -> its name, group, category and volumes. Disk-cached.

    Three fanned-out waves on a cold run - the unseen types, then the distinct groups they name,
    then the distinct categories those groups name - so a thousand rows of twenty ores cost twenty
    type requests plus one group each, not one request per row. A warm cache makes no request at
    all, and empty input touches neither disk nor network.

    Every id gets an entry: ESI's refusal to name one (an unpublished type, an id that is not a
    type at all) degrades that entry to `type <id>` / `unknown` instead of failing the batch."""
    wanted = {ident for ident in (_as_int(value) for value in type_ids) if ident is not None}
    if not wanted:
        return {}

    doc = read_doc(cache_dir)
    learned: dict[str, dict] = {"types": {}, "groups": {}, "categories": {}}

    # Wave 1: the types themselves.
    unseen = sorted(ident for ident in wanted if str(ident) not in doc["types"])
    for type_id, payload in _fetch(client, "/universe/types/{id}", unseen).items():
        record = _parse_type(payload)
        if record is not None:
            doc["types"][str(type_id)] = record
            learned["types"][str(type_id)] = record

    # Wave 2: the distinct groups of the types we now know. Cached types count too - their group may
    # have been fetched by a run that died before publishing it, and re-asking for one group is how
    # a half-known record heals without refetching the type beside it.
    group_ids = set()
    for ident in wanted:
        record = doc["types"].get(str(ident))
        if record and record["group_id"] is not None:
            group_ids.add(record["group_id"])
    unseen_groups = sorted(gid for gid in group_ids if str(gid) not in doc["groups"])
    for group_id, payload in _fetch(client, "/universe/groups/{id}", unseen_groups).items():
        record = _parse_group(payload)
        if record is not None:
            doc["groups"][str(group_id)] = record
            learned["groups"][str(group_id)] = record

    # Wave 3: the distinct categories of those groups. There are about twenty in the whole game, so
    # after the first run this wave is empty forever.
    category_ids = set()
    for gid in group_ids:
        record = doc["groups"].get(str(gid))
        if record and record["category_id"] is not None:
            category_ids.add(record["category_id"])
    unseen_categories = sorted(cid for cid in category_ids if str(cid) not in doc["categories"])
    for category_id, payload in _fetch(client, "/universe/categories/{id}", unseen_categories).items():
        record = _parse_category(payload)
        if record is not None:
            doc["categories"][str(category_id)] = record
            learned["categories"][str(category_id)] = record

    _publish(cache_dir, learned)
    return {ident: _info(ident, doc) for ident in wanted}


def _info(type_id: int, doc: dict) -> TypeInfo:
    """Assemble one entry from the merged maps; missing pieces degrade, never raise."""
    record = doc["types"].get(str(type_id))
    if record is None:
        # Nothing was learned about it, so nothing was cached either: the next run asks again.
        return TypeInfo(type_id=type_id, name=f"type {type_id}", group_id=None, group_name=UNKNOWN,
                        category_id=None, category_name=UNKNOWN, volume=None, packaged_volume=None)
    group = doc["groups"].get(str(record["group_id"])) if record["group_id"] is not None else None
    category_id = group["category_id"] if group else None
    category = doc["categories"].get(str(category_id)) if category_id is not None else None
    return TypeInfo(type_id=type_id, name=record["name"], group_id=record["group_id"],
                    group_name=group["name"] if group else UNKNOWN, category_id=category_id,
                    category_name=category["name"] if category else UNKNOWN,
                    volume=record["volume"], packaged_volume=record["packaged_volume"])


def _resolve_chains(by_item: dict[int, dict]) -> tuple[dict[int, int | None], set[int]]:
    """item_id -> the location it ultimately sits in, plus the item ids on a malformed chain.

    An asset whose `location_id` equals another row's `item_id` lives inside that item, so walking
    `location_id` from any row reaches the top-level id that is not itself an item here - a station,
    a solar system, or an `other`. Two shapes are treated as malformed and cut rather than followed:
    a chain that revisits an item (ESI has shipped cycles) and one longer than any real fitting. The
    caller drops the parent link of every item on such a chain, which is what guarantees that a
    consumer walking `Location.parent_id` always terminates.

    A parent that is simply absent from the rows is *not* malformed: its id is returned as the top,
    and gets described by the place rules below with whatever ESI can say about it."""
    top_of: dict[int, int | None] = {}
    broken: set[int] = set()
    for start in by_item:
        if start in top_of:
            continue
        path: list[int] = []
        visited: set[int] = set()
        cur = start
        top: int | None = None
        while True:
            row = by_item.get(cur)
            if row is None:
                top = cur                      # a place, or a parent these rows do not include
                break
            nxt = _as_int(row.get("location_id"))
            if nxt is None or nxt in visited or len(path) >= MAX_CHAIN_HOPS:
                broken.update(path)            # never reaches a place: cut every link on it
                break
            visited.add(cur)
            path.append(cur)
            cur = nxt
        for node in path:
            top_of[node] = top
    return top_of, broken


def _token_scopes(token: str | None) -> frozenset[str] | None:
    """The scopes inside an access token, or None when they cannot be read.

    A 403 is not free: ESI counts it against the error window that throttles every other call in
    this process, and without `esi-universe.read_structures.v1` *every* structure probe would be a
    403 (verified live: the scope is missing from every stored consent today). The list is a claim
    in the token itself, so a fight that cannot be won is skipped before it starts. An unreadable
    token yields None and we simply try - ESI's answer beats our guess."""
    if not token:
        return frozenset()
    try:
        claims = sso.decode_jwt(token)
    except (IndexError, ValueError, TypeError):
        return None
    scope = claims.get("scp")
    if isinstance(scope, str):
        return frozenset({scope})
    if isinstance(scope, list):
        return frozenset(s for s in scope if isinstance(s, str))
    return None


def _owner(token: str | None, corporation_id: int | None,
           character_id: int | None) -> tuple[int | None, bool]:
    """(owner id, is_corporation) for the assets/names endpoint.

    The published contract passes a token and optionally a corporation id; the character case needs
    an id the caller may not have given, and the access token carries it in `sub`, whose real shape
    is `CHARACTER:EVE:<id>`. An owner that cannot be derived means no custom names at all: guessing
    would mean asking ESI about somebody else's items, and one foreign id fails the whole chunk."""
    corp = _as_int(corporation_id)
    if corp is not None:
        return corp, True
    char = _as_int(character_id)
    if char is not None:
        return char, False
    if not token:
        return None, False
    try:
        sub = str(sso.decode_jwt(token).get("sub", ""))
    except (IndexError, ValueError, TypeError):
        return None, False
    owner = _as_int(sub.rsplit(":", 1)[-1])
    return (owner, False) if owner is not None else (None, False)


def _named(name: str) -> bool:
    """Whether ESI's `name` is a name a player actually gave the item.

    `assets/names` does not answer null for an unnamed item: it answers the literal string
    ``"None"`` (verified live - 18 of one character's 23 singleton rows). Taken at face value that
    label wins over the type name, and an unnamed Retriever renders as ``None (Retriever)``. The
    string is therefore ambiguous - it is overwhelmingly ESI's placeholder, and conceivably a
    player who really named a ship "None" - and between showing every unnamed item a placeholder
    and showing one deliberate joke its type name, the type name is the honest default.
    """
    stripped = name.strip()
    return bool(stripped) and stripped != "None"


def _custom_names(client, token: str | None, owner: int | None, is_corp: bool,
                  item_ids: Sequence[int]) -> dict[int, str]:
    """Player-given names for singleton items, in the 1000-id chunks the endpoint allows.

    Only singletons are asked about: they are the only items ESI lets a player name, and they are
    also the only ids guaranteed to belong to this owner - one id that is not the owner's makes the
    whole call fail with 404 (verified live), so guessing would cost every name in the chunk. A
    refusal of any kind degrades to no custom names at all, which leaves the type name."""
    if not item_ids or owner is None or not token:
        return {}
    endpoint = f"/corporations/{owner}/assets/names" if is_corp else f"/characters/{owner}/assets/names"
    out: dict[int, str] = {}
    for start in range(0, len(item_ids), ASSET_NAMES_CHUNK):
        chunk = list(item_ids[start:start + ASSET_NAMES_CHUNK])
        try:
            rows = client.post(endpoint, chunk, token=token)
        except esi_mod.EsiError:
            continue   # no assets consent for this owner (or a chunk ESI rejected): type names it is
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                item_id, name = _as_int(row.get("item_id")), row.get("name")
                if item_id is not None and isinstance(name, str) and _named(name):
                    out[item_id] = name
    return out


def _item_kind(row: dict, info: TypeInfo | None, holds_items: bool) -> str:
    """`ship`, `container` or `other` for an item that acts as a location.

    ESI has no "this item can hold things" flag: `is_singleton` only marks uniqueness (a blueprint
    is singleton and holds nothing), and `location_flag` names the slot an item sits *in*, never
    what it contains. So the two signals that do exist are used - the item's own type category,
    which is the only thing ESI offers that says "this is a ship", and whether another asset row
    reports itself as being inside this item, which is direct proof it is a container."""
    if info is not None and info.category_name == SHIP_CATEGORY:
        return "ship"
    return "container" if holds_items else "other"


def resolve_locations(client, rows: Sequence[dict], token: str | None = None,
                      corporation_id: int | None = None, character_id: int | None = None,
                      cache_dir: str | None = None) -> dict[int, Location]:
    """Where the assets in `rows` actually are, keyed by every id a caller may need to render.

    The map covers each distinct `location_id` in the rows plus each `item_id` that can act as a
    location - an item another row sits in, or a singleton (the only items ESI lets a player name).
    A row's own place is `locations[row["location_id"]]`; its container's place is one
    `parent_id` further up.

    Naming goes cheapest first: the permanently cached `/universe/names` for stations and systems,
    then `/universe/structures/{id}` with the caller's token for the item-sized ids that stayed
    unresolved (a 403/404 there is normal - it means "you may not look", not "error" - and degrades
    to a stable `structure <id>` label), then the assets/names endpoint for custom names, falling
    back to the type name when that call is refused. Ids above int32 never enter a names request:
    ESI 400s the whole batch on one overflow, which would cost every station name beside it."""
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return {}

    by_item: dict[int, dict] = {}
    location_types: dict[int, set[str]] = {}
    for row in rows:
        item_id = _as_int(row.get("item_id"))
        if item_id is not None:
            by_item.setdefault(item_id, row)
        place = _as_int(row.get("location_id"))
        if place is not None:
            kind = row.get("location_type")
            location_types.setdefault(place, set()).add(kind if isinstance(kind, str) else "")

    top_of, broken = _resolve_chains(by_item)
    # Items worth describing: those another row sits in (so a chain has to be renderable through
    # them) and singletons (so a named ship can show the name its owner gave it).
    holds_items = {ident for ident in location_types if ident in by_item}
    singletons = {ident for ident, row in by_item.items() if row.get("is_singleton")}
    described = holds_items | singletons

    # Structures: only item-sized ids that are not items we were given, and that ESI describes as a
    # station or `other` - an id these rows label `item` is a ship or a container, never a structure,
    # and probing it buys a guaranteed 404 against the error window. Without a token, or with one
    # that provably lacks the structures scope, no probe can succeed, so none is attempted and those
    # ids fall through to a stable label below.
    terminal = [ident for ident in location_types if ident not in by_item]
    candidates = sorted(ident for ident in terminal
                        if ident > INT32_MAX and "item" not in location_types[ident])
    scopes = _token_scopes(token)
    probe = candidates if candidates and token and (scopes is None or STRUCTURE_SCOPE in scopes) else []
    structures: dict[int, dict] = {}
    for ident, payload in _fetch(client, "/universe/structures/{id}", probe, token=token).items():
        if isinstance(payload, dict):
            structures[ident] = payload

    # Names for everything nameable: the small terminal ids (NPC stations, solar systems) and the
    # systems the structure probes reported. Cached permanently by esi.resolve_names.
    nameable = {ident for ident in terminal if ident <= INT32_MAX}
    for payload in structures.values():
        system = _as_int(payload.get("solar_system_id"))
        if system is not None:
            nameable.add(system)
    names = esi_mod.resolve_names(client, nameable, cache_dir=cache_dir) if nameable else {}

    owner, is_corp = _owner(token, corporation_id, character_id)
    custom = _custom_names(client, token, owner, is_corp, sorted(singletons))
    infos = type_info(client, (by_item[ident].get("type_id") for ident in described),
                      cache_dir=cache_dir) if described else {}

    places: dict[int, Location] = {}
    for ident in sorted(terminal):
        structure = structures.get(ident)
        system_id = None
        if structure is not None:
            # ESI named it to *this* token: a player structure, wherever it is docked.
            kind = "structure"
            named = structure.get("name")
            name = named if isinstance(named, str) and named.strip() else f"structure {ident}"
            system_id = _as_int(structure.get("solar_system_id"))
        elif "solar_system" in location_types[ident]:
            kind, name, system_id = "system", names.get(ident) or f"system {ident}", ident
        elif "station" in location_types[ident] and ident > INT32_MAX:
            # ESI calls it a station but the id is an item's: somebody's citadel this token may not
            # see (403), or one that no longer exists (404). The label stays stable across runs.
            kind, name = "structure", f"structure {ident}"
        elif "station" in location_types[ident]:
            kind, name = "station", names.get(ident) or f"station {ident}"
        else:
            # `other`, or a chain whose parent these rows do not include. Nothing more is knowable;
            # the label at least says which kind of id the caller is looking at.
            kind = "other"
            prefix = "item" if "item" in location_types[ident] else "location"
            name = names.get(ident) or f"{prefix} {ident}"
        places[ident] = Location(location_id=ident, kind=kind, name=name, system_id=system_id,
                                 system_name=names.get(system_id) if system_id is not None else None)

    out: dict[int, Location] = dict(places)
    for ident in sorted(described):
        row = by_item[ident]
        type_id = _as_int(row.get("type_id"))
        info = infos.get(type_id) if type_id is not None else None
        anchor = places.get(top_of.get(ident)) if ident not in broken else None
        parent = None if ident in broken else _as_int(row.get("location_id"))
        out[ident] = Location(
            location_id=ident,
            kind=_item_kind(row, info, ident in holds_items),
            name=custom.get(ident) or (info.name if info else None) or f"item {ident}",
            system_id=anchor.system_id if anchor else None,
            system_name=anchor.system_name if anchor else None,
            parent_id=parent)
    return out
