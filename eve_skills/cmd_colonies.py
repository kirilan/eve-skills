"""colonies: what a character's planetary industry is actually doing right now.

`pi` plans a colony out of the local SDE and needs no login for it. It cannot answer the question
that starts any return to planetary industry - *do these colonies still exist, and did an extractor
run out while I was away* - because nothing installed locally knows what a character owns. ESI does,
on two endpoints:

  GET /characters/{id}/planets               every colony, stamped with its last recalculation
  GET /characters/{id}/planets/{planet_id}   one colony's pins, links and routes

Both are read-only in the strongest sense ESI offers. The published OpenAPI document (fetched
2026-09-13 from https://esi.evetech.net/meta/openapi.json) lists GET as the only method either path
implements, so ``esi-planets.manage_planets.v1`` gives this tool nothing it could write with - the
same reason the scope is offered as opt-in consent rather than a base scope.

One caveat shapes every number printed here, quoted verbatim from ESI's own description of the
detail endpoint:

    "Note: Planetary information is only recalculated when the colony is viewed through the client.
     Information will not update until this criteria is met."

So `last update` is the last time a human opened that colony *in the game*, not the last time
anything changed there. The report says so wherever it matters, and `--detail` never presents an
expiry countdown from a layout ESI has not recomputed in a month as if it were live.

Recipes, structure names and planet type names come from the shipped SDE document (see `pi`); the public
``/universe/schematics/{id}`` endpoint is asked only about a schematic that document has never heard of -
or when no document is installed at all - and the report says when it was.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from . import alphadata, esi as esi_mod, exports, render

FEATURE = "planets"
SCOPE = "esi-planets.manage_planets.v1"

# How old a colony's `last_update` may get before the report stops trusting what it implies.
#
# Two different clocks are in play and only one of them is measurable:
#   * Transport. Both colony routes publish a 600 s cache window - measured 2026-09-13 against
#     /meta/openapi.json (`x-cache-age` and `x-client-cache-ttl` are both 600, in the same
#     `char-industry` group as the rate limit ESI also advertises: 600 requests per 15 minutes).
#     That bounds how old the *bytes* can be, and `Esi.get` already honours it.
#   * The game. CCP recalculates a colony only when it is opened in the client (quoted in the module
#     docstring), so `last_update` can lag by months and no cache header says so.
# Seven days is the point where the second clock makes a countdown worthless: measured on the shipped
# SDE snapshot, planetary schematic cycles run 1800-3600 s, so a week-old layout is already 170-335
# cycles of drift and any "expires in" figure taken from it describes a colony nobody has looked at
# since. Seven days is also a full PI round, so the marker fires for genuinely abandoned colonies
# rather than for one visited last Sunday - the threshold is a judgement, the numbers above are not.
STALE_AFTER_DAYS = 7

COLONY_COLUMNS = ["character", "system", "planet", "type", "CCU", "pins", "last update"]
EXTRACTOR_COLUMNS = ["extractor", "product", "qty/cycle", "cycle", "heads", "expires"]
FACILITY_COLUMNS = ["facility", "schematic", "cycle", "makes"]

COLONY_CSV_COLUMNS = ["character_id", "character_name", "solar_system_id", "system_name", "planet_id",
                      "planet_type", "planet_type_name", "owner_id", "upgrade_level", "num_pins",
                      "last_update", "age_seconds", "stale"]
# With --detail a CSV row is one extractor instead of one colony (the precedent is
# `inventory --items`, which switches granularity the same way). The colony columns repeat so every
# row still says where that extractor stands; a colony with none keeps one row with those cells empty,
# because "this colony exists and has no extractors" is an answer, not a missing row.
EXTRACTOR_CSV_COLUMNS = COLONY_CSV_COLUMNS + ["pin_id", "extractor_type_id", "product_type_id",
                                              "product_name", "qty_per_cycle", "cycle_seconds", "heads",
                                              "expiry_time", "expired", "seconds_to_expiry"]


class ColonyAccess(esi_mod.EsiError):
    """ESI refused a colony endpoint: consent withdrawn since the token was minted, or the
    character no longer exists. Either way the fix is a browser login, which only the user can do."""


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Extractor:
    """One extractor pin, as its colony's layout described it.

    `cycle_time` is read off the pin rather than assumed: ESI states it per extractor, and heads or
    an upgrade can change it, so a constant here would be wrong for exactly the colonies that matter."""

    pin_id: int
    type_id: int | None = None
    product_type_id: int | None = None
    qty_per_cycle: int | None = None
    cycle_time: int | None = None
    heads: int | None = None
    expiry_time: str | None = None      # ISO UTC as ESI sent it; None = no extraction programmed


@dataclass(frozen=True)
class Facility:
    """One facility pin that is running something. Names come later, from the local document."""

    pin_id: int
    type_id: int | None = None
    schematic_id: int | None = None


@dataclass(frozen=True)
class Layout:
    """A colony's pins, bucketed the way a player thinks about them.

    A pin is an extractor when ESI attaches `extractor_details` to it and a facility when it carries
    a schematic; everything else (command centre, ECUs, storage, launchpads, product waiting to be
    hauled off) is counted, not listed - those pins have no timer worth watching here."""

    planet_id: int
    extractors: tuple[Extractor, ...] = ()
    facilities: tuple[Facility, ...] = ()
    other_type_ids: tuple[int | None, ...] = ()
    total_pins: int = 0


@dataclass(frozen=True)
class Colony:
    """One row of the colony list, plus its layout when --detail asked for one."""

    character_id: int
    character_name: str
    planet_id: int
    planet_type: str                    # ESI's own lowercase enum, kept verbatim
    solar_system_id: int | None = None
    owner_id: int | None = None
    upgrade_level: int | None = None
    num_pins: int | None = None
    last_update: str | None = None
    layout: Layout | None = None


@dataclass(frozen=True)
class CharacterColonies:
    """What the two endpoints answered for one character.

    `warnings` holds per-colony layout failures: the list is what answers "do I still have colonies",
    so losing one colony's pins must not lose the others' rows."""

    character_id: int
    character_name: str
    colonies: tuple[Colony, ...] = ()
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# fetching (read-only; ESI's own cache headers decide how often anything is re-requested)
# ---------------------------------------------------------------------------

def _num(value) -> int | None:
    """An optional id, level or count; None for absent or non-numeric, and 0 stays 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _moment(value) -> datetime | None:
    """An ESI timestamp as an aware UTC datetime; junk reads as unknown rather than raising.

    ESI stamps colonies in UTC with a `Z`, and `render.parse_ts` accepts that from 3.11 on. A naive
    value means the same instant, so it is given a UTC tzinfo instead of being compared to an aware
    clock - which would raise."""
    if not value:
        return None
    try:
        moment = render.parse_ts(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _schematic_id(pin: dict) -> int | None:
    """The schematic a pin is running, from either of the two places ESI may put it.

    `factory_details.schematic_id` is where the documented shape keeps it; the top-level
    `pin["schematic_id"]` is what rows have historically carried, and one of the two is always the
    authoritative copy, so preferring the nested one and falling back loses nothing."""
    details = pin.get("factory_details")
    if isinstance(details, dict):
        found = _num(details.get("schematic_id"))
        if found is not None:
            return found
    return _num(pin.get("schematic_id"))


def _heads(details: dict) -> int | None:
    """Attached extractor heads. The document says a list of head positions; an integer count is
    tolerated because only the number is ever printed."""
    heads = details.get("heads")
    if isinstance(heads, (list, tuple)):
        return len(heads)
    return _num(heads)


def _colony(character_id: int, character_name: str, row: dict, layout: Layout | None = None) -> Colony:
    """One colony-list row as a `Colony`. Every field the list endpoint marks required is read
    defensively anyway: a missing optional value costs one dash in a table, not the whole report."""
    return Colony(character_id=character_id, character_name=character_name,
                  planet_id=_num(row.get("planet_id")), planet_type=str(row.get("planet_type") or "unknown"),
                  solar_system_id=_num(row.get("solar_system_id")), owner_id=_num(row.get("owner_id")),
                  upgrade_level=_num(row.get("upgrade_level")), num_pins=_num(row.get("num_pins")),
                  last_update=row.get("last_update") if isinstance(row.get("last_update"), str) else None,
                  layout=layout)


def colony_layout(client: esi_mod.Esi, record: dict, planet_id: int) -> Layout:
    """One colony's pins, fetched and bucketed.

    The detail endpoint is the only place ESI lists pin ids, and it is also where `expiry_time`
    lives - which is what makes the `extractor_expired` watch event possible at all."""
    cid = int(record["character_id"])
    body = client.get(f"/characters/{cid}/planets/{planet_id}", token=record["access_token"]) or {}
    pins = body.get("pins") if isinstance(body.get("pins"), list) else []
    extractors, facilities, other = [], [], []
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        ident = _num(pin.get("pin_id"))
        if ident is None:
            continue                      # no identity means no row can be tracked across polls
        type_id = _num(pin.get("type_id"))
        details = pin.get("extractor_details")
        if isinstance(details, dict):
            extractors.append(Extractor(pin_id=ident, type_id=type_id,
                                        product_type_id=_num(details.get("product_type_id")),
                                        qty_per_cycle=_num(details.get("qty_per_cycle")),
                                        cycle_time=_num(details.get("cycle_time")),
                                        heads=_heads(details),
                                        expiry_time=pin.get("expiry_time")
                                        if isinstance(pin.get("expiry_time"), str) else None))
        elif _schematic_id(pin) is not None:
            facilities.append(Facility(ident, type_id, _schematic_id(pin)))
        else:
            other.append(type_id)
    return Layout(planet_id=planet_id, extractors=tuple(extractors), facilities=tuple(facilities),
                  other_type_ids=tuple(other), total_pins=len(pins))


def character_colonies(client: esi_mod.Esi, record: dict, detail: bool = False) -> CharacterColonies:
    """Every colony of one character; with `detail`, each colony's layout too.

    A 401/403 here means the stored consent no longer covers the endpoint - a token minted before
    `--scopes planets` was granted, or revoked in the EVE account console - so the message names the
    login that fixes it instead of surfacing ESI's prose. Layout failures are collected per colony:
    one unreadable planet must not cost the rows for the rest."""
    cid = int(record["character_id"])
    name = record.get("character_name") or str(cid)
    token = record["access_token"]
    try:
        rows = client.get(f"/characters/{cid}/planets", token=token) or []
    except esi_mod.AuthError as err:
        raise ColonyAccess(f"ESI refused the colony list ({err}) - it needs {SCOPE}; run: "
                           f"eve-skills login --scopes {FEATURE}") from None
    colonies, warnings = [], []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        layout = None
        if detail:
            planet_id = _num(row.get("planet_id"))
            try:
                layout = colony_layout(client, record, planet_id)
            except esi_mod.EsiError as err:
                warnings.append(f"{name}: the layout of planet {planet_id} could not be read ({err}); "
                                f"its row in this report has no extractors")
        colonies.append(_colony(cid, name, row, layout))
    return CharacterColonies(cid, name, tuple(colonies), tuple(warnings))


# ---------------------------------------------------------------------------
# naming: the local SDE document first, ESI only for what it cannot name
# ---------------------------------------------------------------------------

def _document() -> dict:
    """The planetary-industry snapshot, or an error naming the one command that fixes it.

    Same discipline as `pi`: nothing installed is a different failure from something installed that
    is not in this shape, and both have to reach the user as `error: ...` rather than a traceback."""
    try:
        return alphadata.planet_industry()
    except FileNotFoundError:
        raise RuntimeError("no local planetary industry data - run: eve-skills update-data") from None
    except ValueError as err:      # alphadata's own shape errors already name the fix
        raise RuntimeError(str(err)) from None


def planet_label(value: str, document: dict | None = None) -> str:
    """A planet type as a word.

    ESI's enum is already the SDE name lowercased (`temperate` against `Temperate`), so the plain
    report can capitalise it and needs no local document at all; when one is loaded its spelling wins,
    which keeps `colonies` and `pi planet-type` naming a planet identically."""
    if document:
        for name in (document.get("planet_types") or {}).values():
            if isinstance(name, str) and name.lower() == value.lower():
                return name
    return value[:1].upper() + value[1:]


def type_names(client: esi_mod.Esi, document: dict | None, ids) -> dict[int, str]:
    """Display names for pin and product type ids, from the local snapshot wherever it has one.

    The shipped document holds all 130 planetary structures and all 83 PI commodities (counted in the
    bundled build), so a colony report is readable without asking ESI anything. Whatever is left -
    a type added since the build we ship - goes to the public ``/universe/names`` endpoint once, in
    bulk; it is disk-cached and drops ids it cannot resolve, so an unknown pin shows as an id instead
    of failing the report."""
    wanted = {int(ident) for ident in ids if ident is not None}
    out: dict[int, str] = {}
    if document:
        for section in ("commodities", "structures"):
            for key, row in (document.get(section) or {}).items():
                ident = _num(key)
                if ident in wanted and isinstance(row, dict) and row.get("name"):
                    out[ident] = row["name"]
    missing = wanted - set(out)
    if missing:
        out.update(esi_mod.resolve_names(client, missing))
    return out


def schematic_view(client: esi_mod.Esi, document: dict | None, schematic_id: int | None,
                   names: dict[int, str], notices: list[str]) -> dict | None:
    """What a pin is running, as far as anything here can know.

    The local document answers name, cycle and recipe. A schematic it has never heard of (CCP added
    one after the SDE build we ship) falls back to the public ``/universe/schematics/{id}``, which
    documents only `schematic_name` and `cycle_time` - so the recipe is honestly unknown there, and a
    notice says which row came from where. Without that notice a reader cannot tell "no outputs" from
    "outputs nobody published"."""
    if schematic_id is None:
        return None
    row = (document or {}).get("schematics", {}).get(str(schematic_id))
    if isinstance(row, dict) and row.get("name"):
        out = {"schematic_id": schematic_id, "name": row["name"], "cycle_seconds": _num(row.get("cycle")),
               "source": "sde",
               "inputs": [{"type_id": _num(ident), "name": names.get(_num(ident), f"type {ident}"),
                           "quantity": qty} for ident, qty in sorted((row.get("in") or {}).items())],
               "outputs": [{"type_id": _num(ident), "name": names.get(_num(ident), f"type {ident}"),
                            "quantity": qty} for ident, qty in sorted((row.get("out") or {}).items())]}
        return out
    try:
        doc = client.get(f"/universe/schematics/{schematic_id}") or {}
    except esi_mod.EsiError as err:
        notices.append(f"schematic {schematic_id} is not in the local SDE snapshot and ESI could not "
                       f"name it either ({err})")
        return {"schematic_id": schematic_id, "name": None, "cycle_seconds": None, "source": "none",
                "inputs": [], "outputs": []}
    notices.append(f"schematic {schematic_id} is not in the local SDE snapshot (build "
                   f"{(document or {}).get('build', 'unknown')}); its name came from ESI and its recipe "
                   f"is not published there - run: eve-skills update-data")
    return {"schematic_id": schematic_id, "name": doc.get("schematic_name"),
            "cycle_seconds": _num(doc.get("cycle_time")), "source": "esi", "inputs": [], "outputs": []}


# ---------------------------------------------------------------------------
# derived figures
# ---------------------------------------------------------------------------

def age_seconds(last_update: str | None, now: datetime) -> float | None:
    """How long ago ESI says this colony was last recalculated; None when it says nothing."""
    moment = _moment(last_update)
    return None if moment is None else (now - moment).total_seconds()


def epoch_seconds(value) -> float | None:
    """An ESI stamp as epoch seconds; None when there is nothing to read.

    Public because `--watch` ranks extractors by exactly these stamps and must not bring its own
    timestamp parser - two ways to mis-read the same field is one too many."""
    moment = _moment(value)
    return None if moment is None else moment.timestamp()


def is_stale(age: float | None) -> bool:
    """Whether a colony is too old for its own numbers to be trusted (see STALE_AFTER_DAYS)."""
    return age is not None and age > STALE_AFTER_DAYS * 86400


def _age_cell(last_update: str | None, now: datetime) -> tuple[str, bool]:
    """"3d 04h ago *" and whether it deserves the star; "-" when there is no stamp to age."""
    age = age_seconds(last_update, now)
    if age is None:
        return "-", False
    return f"{render.format_duration(age)} ago" + (" *" if is_stale(age) else ""), is_stale(age)


def _stamp(value: str | None) -> str:
    """An ESI timestamp as a compact date; "-" when there is none to show."""
    moment = _moment(value)
    return "-" if moment is None else moment.strftime("%b %d %H:%M")


def _expiry_cell(expiry_time: str | None, now: datetime) -> str:
    """When an extraction ends, and how far off that is - or the plain truth that it already ended."""
    moment = _moment(expiry_time)
    if moment is None:
        return "-"
    left = (moment - now).total_seconds()
    stamp = moment.strftime("%b %d %H:%M")
    if left > 0:
        return f"{stamp} (in {render.format_duration(left)})"
    return f"{stamp} (expired {render.format_duration(-left)} ago)"


def _qty_cell(quantity) -> str:
    return "-" if quantity is None else f"{quantity:,}"


def _extractor_doc(ex: Extractor, names: dict[int, str], now: datetime) -> dict:
    """One extractor pin as data, its expiry given both ways.

    `seconds_to_expiry` is negative once the extraction has run out, which is what a spreadsheet needs
    to sort the pins that need reprogramming to the top; `expired` says the same thing in words."""
    moment = _moment(ex.expiry_time)
    return {"pin_id": ex.pin_id, "type_id": ex.type_id,
            "name": names.get(ex.type_id) if ex.type_id else None,
            "product_type_id": ex.product_type_id,
            "product_name": names.get(ex.product_type_id) if ex.product_type_id else None,
            "qty_per_cycle": ex.qty_per_cycle, "cycle_seconds": ex.cycle_time, "heads": ex.heads,
            "expiry_time": ex.expiry_time, "expired": moment is not None and moment <= now,
            "seconds_to_expiry": None if moment is None else (moment - now).total_seconds()}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def colony_doc(colony: Colony, names: dict[int, str], document: dict | None, now: datetime,
               schematics: dict[int, dict]) -> dict:
    """One colony as machine-readable data: ESI's own fields, plus every name we can attach to them.

    `layout_fetched` is explicit because the two differ: an absent extractor list after a report that
    never asked for layouts would read as "this colony has no extractors", which is a different claim
    altogether."""
    age = age_seconds(colony.last_update, now)
    doc = {"character_id": colony.character_id, "character_name": colony.character_name,
           "solar_system_id": colony.solar_system_id,
           "system_name": names.get(colony.solar_system_id) if colony.solar_system_id else None,
           "planet_id": colony.planet_id, "planet_type": colony.planet_type,
           "planet_type_name": planet_label(colony.planet_type, document),
           "owner_id": colony.owner_id, "upgrade_level": colony.upgrade_level,
           "num_pins": colony.num_pins, "last_update": colony.last_update,
           "age_seconds": None if age is None else int(age), "stale": is_stale(age),
           "layout_fetched": colony.layout is not None}
    layout = colony.layout
    if layout is None:
        return doc
    doc["pins_read"] = layout.total_pins
    doc["extractors"] = [_extractor_doc(ex, names, now) for ex in layout.extractors]
    doc["facilities"] = [{"pin_id": fac.pin_id, "type_id": fac.type_id,
                          "name": names.get(fac.type_id) if fac.type_id else None,
                          "schematic_id": fac.schematic_id,
                          "schematic": schematics.get(fac.schematic_id) if fac.schematic_id else None}
                         for fac in layout.facilities]
    counts: dict[int, int] = {}
    for ident in layout.other_type_ids:
        counts[ident] = counts.get(ident, 0) + 1
    doc["other_pins"] = [{"type_id": ident, "name": names.get(ident) if ident else None, "count": count}
                         for ident, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    return doc


def colony_row(colony: Colony, names: dict[int, str], document: dict | None, now: datetime) -> list[str]:
    """One summary-table row."""
    system = names.get(colony.solar_system_id) if colony.solar_system_id else None
    age_cell, _ = _age_cell(colony.last_update, now)
    return [colony.character_name, system or (f"system {colony.solar_system_id}"
                                              if colony.solar_system_id else "-"),
            render.csv_cell(colony.planet_id), planet_label(colony.planet_type, document),
            "-" if colony.upgrade_level is None else str(colony.upgrade_level),
            "-" if colony.num_pins is None else str(colony.num_pins), age_cell]


def _other_pins_line(layout: Layout, names: dict[int, str]) -> str | None:
    """The pins that are neither extractors nor running a schematic, counted rather than listed."""
    counts: dict[int | None, int] = {}
    for ident in layout.other_type_ids:
        counts[ident] = counts.get(ident, 0) + 1
    parts = [f"{names.get(ident) if ident is not None else 'unknown pin'} x{count}"
             for ident, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    return ("also on the layout: " + ", ".join(parts)) if parts else None


def colony_block(colony: Colony, names: dict[int, str], document: dict | None, now: datetime,
                 schematics: dict[int, dict], asked: bool = False) -> str:
    """One colony in full: header, extractors, facilities, and a count of everything else.

    `asked` says whether this run requested layouts at all, which is the only way to tell the two
    reasons a layout can be missing apart - and printing "pass --detail" after a run that did pass it,
    with the real reason sitting in a warning nobody sees, would send the reader off in the wrong
    direction."""
    system = names.get(colony.solar_system_id) if colony.solar_system_id else None
    age_cell, stale = _age_cell(colony.last_update, now)
    head = (f"{system or f'system {colony.solar_system_id}'} - "
            f"{planet_label(colony.planet_type, document)} planet {colony.planet_id} "
            f"({colony.character_name})  "
            f"CCU {'?' if colony.upgrade_level is None else colony.upgrade_level}, "
            f"{'?' if colony.num_pins is None else colony.num_pins} "
            f"{'pin' if colony.num_pins == 1 else 'pins'}, "
            f"last update {age_cell}")
    lines = [head]
    layout = colony.layout
    if layout is None:
        lines.append(_notes([
            "layout not fetched - pass --detail" if not asked else
            "this colony's pins could not be read - ESI refused the layout call, so no extractor or "
            "facility is claimed for it; the warning on stderr says why"]))
        return "\n".join(lines)
    # Soonest to run out first: the whole point of the view is finding the extractor that needs
    # reprogramming, and an unprogrammed one cannot need anything, so it sorts last.
    extractors = sorted(layout.extractors, key=lambda ex: (ex.expiry_time is None,
                                                           ex.expiry_time or "", ex.pin_id))
    if extractors:
        lines.append(render.table(EXTRACTOR_COLUMNS,
                                  [[names.get(ex.type_id) if ex.type_id else "-",
                                    names.get(ex.product_type_id) if ex.product_type_id
                                    else ("-" if ex.product_type_id is None else f"type {ex.product_type_id}"),
                                    _qty_cell(ex.qty_per_cycle),
                                    "-" if ex.cycle_time is None else render.format_duration(ex.cycle_time),
                                    "-" if ex.heads is None else str(ex.heads),
                                    _expiry_cell(ex.expiry_time, now)] for ex in extractors]))
    else:
        lines.append(_notes(["(no extractors on this colony)"]))
    running = [fac for fac in layout.facilities if fac.schematic_id is not None]
    if running:
        rows = []
        for fac in sorted(running, key=lambda f: ((schematics.get(f.schematic_id) or {}).get("name") or "",
                                                  f.pin_id)):
            view = schematics.get(fac.schematic_id) or {}
            makes = ", ".join(f"{out['name']} x{_qty_cell(out['quantity'])}" for out in view["outputs"])
            rows.append([names.get(fac.type_id) if fac.type_id else "-", view.get("name") or "-",
                         "-" if view.get("cycle_seconds") is None
                         else render.format_duration(view["cycle_seconds"]), makes or "-"])
        lines.append(render.table(FACILITY_COLUMNS, rows))
    else:
        lines.append(_notes(["(no facility is running a schematic)"]))
    other = _other_pins_line(layout, names)
    if other:
        lines.append(_notes([other]))
    if stale:
        lines.append(_notes([f"* ESI last recalculated this colony on {_stamp(colony.last_update)}; it "
                             f"only does that when the colony is opened in-game, so the pins above are "
                             f"as of then, not as of now"]))
    return "\n".join(lines)


def _notes(lines: list[str]) -> str:
    """Footnotes under a table, indented the way `pi` and `build-cost` indent their own."""
    return "\n".join(f"  {line}" for line in lines)


def collect(client: esi_mod.Esi, args, chars) -> tuple[list, list[str]]:
    """(per-character reports, warning lines). One character's refusal never hides the others'.

    `exports.targets` has already dropped the characters whose consent does not cover the scope and
    turned each into a hint line, so a refusal reaching here is ESI disagreeing with a token that
    claims the scope - worth reporting separately, because only a browser login fixes it."""
    reports, failures = [], []
    for tok, public in chars:
        name = public.get("name") or tok.get("character_name") or str(tok["character_id"])
        try:
            reports.append(character_colonies(client, tok, detail=args.detail))
        except (esi_mod.EsiError, RuntimeError) as err:
            failures.append(f"{name}: {err}")
    return reports, failures


def cmd_colonies(args):
    """Every colony of every stored character that consented - and what is running on them."""
    client, chars, hints = exports.targets(args, [(FEATURE, SCOPE)])
    reports, failures = collect(client, args, chars)
    colonies = [colony for report in reports for colony in report.colonies]
    warnings = list(failures) + [line for report in reports for line in report.warnings]
    # A refusal or an unreadable layout is a warning, never the whole command's failure: what ESI did
    # answer is still worth having. Same channel as every other command (and before the report, so a
    # piped stdout stays clean); --json carries them as a field instead of as prose.
    if not args.json:
        for line in warnings:
            print(f"warning: {line}", file=sys.stderr)
    # The summary view needs no local document: ESI names a planet type as a word already. Only --detail
    # wants one, and even there its absence degrades instead of stopping the run: every column of the
    # extractor table is something ESI states itself, so making a ~100 MB download a precondition for
    # "did my extractors run out" would cost the answer to the one question this command exists for. The
    # notice below says what came from where instead.
    notices: list[str] = []
    document = None
    if args.detail:
        try:
            document = _document()
        except RuntimeError as err:
            notices.append(f"{err}; structure and schematic names come from ESI for this run, and ESI "
                           f"publishes no recipe for a schematic")
    ids = {colony.solar_system_id for colony in colonies if colony.solar_system_id}
    recipes = (document or {}).get("schematics") or {}
    for report in reports:
        for colony in report.colonies:
            layout = colony.layout
            if layout is None:
                continue
            ids.update(ex.type_id for ex in layout.extractors)
            ids.update(ex.product_type_id for ex in layout.extractors)
            ids.update(fac.type_id for fac in layout.facilities)
            ids.update(layout.other_type_ids)
            # A facility's output is the most interesting cell of the whole table, and it lives only in
            # the recipe: without these ids `makes` reads `type 3837 x5`.
            for fac in layout.facilities:
                row = recipes.get(str(fac.schematic_id)) if fac.schematic_id is not None else None
                if isinstance(row, dict):
                    ids.update(_num(key) for key in (row.get("in") or {}))
                    ids.update(_num(key) for key in (row.get("out") or {}))
    names = type_names(client, document, ids) if ids else {}
    schematics: dict[int, dict] = {}
    for colony in colonies:
        for fac in (colony.layout.facilities if colony.layout else ()):
            if fac.schematic_id is not None and fac.schematic_id not in schematics:
                schematics[fac.schematic_id] = schematic_view(client, document, fac.schematic_id,
                                                              names, notices)

    def sort_key(colony: Colony):
        system = names.get(colony.solar_system_id) or f"system {colony.solar_system_id}"
        return (colony.character_name.lower(), system.lower(), colony.planet_id or 0)

    ordered = sorted(colonies, key=sort_key)
    now = client.now()

    if args.json:
        print(json.dumps({
            "generated": now.astimezone(timezone.utc).replace(microsecond=0).isoformat()
            .replace("+00:00", "Z"),
            "stale_after_days": STALE_AFTER_DAYS,
            "detail": bool(args.detail),
            # Prose would break a parser, so these three travel as fields: consent hints, per-character
            # or per-colony failures, and the notes that explain where a name came from.
            "hints": hints, "warnings": warnings, "notices": notices,
            "characters": [{"character_id": report.character_id, "character_name": report.character_name,
                            "colonies": [colony_doc(colony, names, document, now, schematics)
                                         for colony in sorted(report.colonies, key=sort_key)]}
                           for report in reports],
        }, indent=2))
        return
    if args.csv:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        columns = EXTRACTOR_CSV_COLUMNS if args.detail else COLONY_CSV_COLUMNS
        writer.writerow(columns)
        for colony in ordered:
            doc = colony_doc(colony, names, document, now, schematics)
            base = [render.csv_cell(doc.get(col)) for col in COLONY_CSV_COLUMNS]
            if not args.detail:
                writer.writerow(base)
                continue
            extractors = doc.get("extractors") or []
            # One row per extractor; a colony without one still gets its row. The empty cells are written
            # out rather than left off: DictReader hands back None for a field the row never had, which is
            # a different value from an empty one, and anything counting columns would call a short row a
            # corrupt export.
            for ex in extractors or [None]:
                tail = [] if ex is None else [
                    render.csv_cell(ex["pin_id"]), render.csv_cell(ex["type_id"]),
                    render.csv_cell(ex["product_type_id"]), render.csv_cell(ex["product_name"]),
                    render.csv_cell(ex["qty_per_cycle"]), render.csv_cell(ex["cycle_seconds"]),
                    render.csv_cell(ex["heads"]), render.csv_cell(ex["expiry_time"]),
                    render.csv_cell(ex["expired"]), render.csv_cell(ex["seconds_to_expiry"])]
                writer.writerow(base + tail + [""] * (len(EXTRACTOR_CSV_COLUMNS) - len(base) - len(tail)))
        sys.stdout.write(buf.getvalue())
        # The footnotes matter to whoever opens the spreadsheet; they just may not pollute the pipe.
        for line in notices:
            print(line, file=sys.stderr)
        return

    blocks = list(hints)
    if ordered:
        if args.detail:
            blocks.append("\n\n".join(colony_block(colony, names, document, now, schematics,
                                                   asked=True) for colony in ordered))
        else:
            lines = [render.table(COLONY_COLUMNS,
                                  [colony_row(colony, names, document, now) for colony in ordered])]
            owner_count = len({colony.character_id for colony in ordered})
            lines.append(f"{len(ordered)} colon{'y' if len(ordered) == 1 else 'ies'} across "
                         f"{owner_count} character{'' if owner_count == 1 else 's'} - pass --detail for "
                         f"the extractors, facilities and expiry times on each")
            if any(is_stale(age_seconds(colony.last_update, now)) for colony in ordered):
                lines += ["", _notes([
                    f"* ESI recalculates a colony only when it is opened in the game client, so a last "
                    f"update more than {STALE_AFTER_DAYS} days old means everything known about that "
                    f"colony - pin count included - is as of then, not as of now"])]
            blocks.append("\n".join(lines))
    for report in reports:
        if not report.colonies:
            blocks.append(f"{report.character_name}: no colonies")
    # Where a name came from is part of the answer, so it prints with the report rather than past it.
    for line in notices:
        blocks.append(_notes([line]))
    if blocks:
        print("\n\n".join(blocks))
