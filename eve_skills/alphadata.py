"""Static Data Export snapshots: alpha clone skill caps, the full skill catalog and blueprint material lists.

A snapshot ships with the repository in ./data; `eve-skills update-data` downloads the
current SDE zip and refreshes a copy under $XDG_DATA_HOME/eve-skills, which takes
precedence when present.

The skill catalog covers *every* catalogued skill - including ones the character has
never trained - so the planner can price them: name, training time multiplier ("rank"),
primary/secondary attribute and the prerequisite skills with their required levels.

Blueprint material lists cover the two activities that actually consume goods - manufacturing and
reaction - so a build cost can be computed from what a run eats, what it yields and how long it takes.

Planetary industry covers the side of industry that runs on a planet rather than in a station, so a
`pi` command can answer recipe, fitting and customs questions without scraping a wiki: what each of
the 68 schematics eats and yields, which facility class and planet types run it, what every structure
costs a command center in CPU and power, how much output a command center supplies at each upgrade
level, and the customs multiplier of every commodity.
"""

from __future__ import annotations

import io
import json
import os
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths, storage

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
SDE_BASE = "https://developers.eveonline.com/static-data/tranquility"

# Every document update() publishes; diagnostics walk this list in order.
DATA_FILES = ("clone_grades.json", "bloodline_races.json", "skill_catalog.json",
              "blueprint_materials.json", "planet_industry.json", "system_planets.json",
              "market_types.json")

# Alpha caps drift out of date with each SDE release; the skills view and doctor warn here.
STALE_DAYS = 90.0

# The dogma attributes that describe a skill itself, as opposed to the bonuses it grants.
ATTR_PRIMARY = 180        # primaryAttribute: id of the character attribute driving the rate
ATTR_SECONDARY = 181      # secondaryAttribute
ATTR_RANK = 275           # skillTimeConstant == the training time multiplier ("rank")
# requiredSkillN -> requiredSkillNLevel; the SDE carries at most three prerequisites.
PREREQUISITE_ATTRS = ((182, 277), (183, 278), (184, 279))

# The only two SDE activities that consume a material list. Copying, invention and the two
# research activities spend time, skill points and data cores instead - inputs no market price
# exists for - so keeping them would put rows in the document that nothing can cost in ISK.
INDUSTRY_ACTIVITIES = ("manufacturing", "reaction")

# The dogma attributes that describe a planetary installation. Measured over all 130 PI item types
# in build 3503375, so none of these is a guess taken off a wiki:
#   49/cpuLoad and 15/powerLoad are what a structure charges the command center. The ship-fitting
#     pair 50/cpu and 30/power is carried by *none* of them (0 rows), which is why reading those
#     instead would quietly produce a document full of zeros.
#   48/cpuOutput and 11/powerOutput are what a command center supplies back. Only the 48 command
#     centers carry them; no PI structure carries both a load and an output figure.
#   1690/1691 are the head figures of an extractor control unit - the CPU/power its head module adds
#     on top of the body's own 400/2600, and present on exactly the eight ECUs.
#   1632/planetRestriction is the only attribute tying an item to a planet type (130 rows = the
#     whole PI structure set), and 709/harvesterType names the raw resource an extractor pulls.
#   1640/importTaxMultiplier and 1641/exportTaxMultiplier are a commodity's customs multipliers;
#     measured equal for every one of the 83 commodities, so either answers and import is used.
#   1638/importTax and 1639/exportTax are a structure's own tax rates: only the eight launchpads
#     carry 1638 (0.5), while 1639 reads 1.0 on a launchpad and 3.0 on every command center - so a
#     planet's tax factors have to come from the launchpad, not from "any type with exportTax".
ATTR_CPU_LOAD = 49
ATTR_POWER_LOAD = 15
ATTR_CPU_OUTPUT = 48
ATTR_POWER_OUTPUT = 11
ATTR_ECU_HEAD_CPU = 1690
ATTR_ECU_HEAD_POWER = 1691
ATTR_PLANET_RESTRICTION = 1632
ATTR_HARVESTER_TYPE = 709
ATTR_IMPORT_TAX_MULTIPLIER = 1640
ATTR_EXPORT_TAX_MULTIPLIER = 1641
ATTR_IMPORT_TAX = 1638
ATTR_EXPORT_TAX = 1639
# A command center states its own upgrade level with the same requiredSkill1/Level pair the skill
# catalog reads for prerequisites: requiredSkill1 is always 2505 (Command Center Upgrades) and
# requiredSkill1Level is the level. The eight base units carry neither, which is what makes them 0.
ATTR_REQUIRED_SKILL1_LEVEL = 277

# The item groups that make up an installation. Measured, SDE category 41 "Planetary Industry" holds
# exactly these six plus 1036 Planetary Links (one type), and a link moves goods between planets
# instead of costing command center resources - so it has no place in a fit and gets no role.
GROUP_EXTRACTORS = 1026
GROUP_COMMAND_CENTERS = 1027
GROUP_PROCESSORS = 1028
GROUP_STORAGE_FACILITIES = 1029
GROUP_SPACEPORTS = 1030                     # the launchpad: the only type carrying importTax
GROUP_EXTRACTOR_CONTROL_UNITS = 1063

# `role` values for the document, keyed by group. Processors are qualified with their facility
# class (see FACILITY_CLASSES) because that is the distinction `pi fit` has to price.
PI_ROLE_BY_GROUP = {
    GROUP_EXTRACTORS: "extractor",
    GROUP_COMMAND_CENTERS: "command_center",
    GROUP_PROCESSORS: "processor",
    GROUP_STORAGE_FACILITIES: "storage_facility",
    GROUP_SPACEPORTS: "launchpad",
    GROUP_EXTRACTOR_CONTROL_UNITS: "extractor_control_unit",
}

# The SDE categories that say what kind of planetary thing a group holds. Measured on both builds used
# here: 41 "Planetary Industry" holds the seven structure groups, 42 "Planetary Resources" holds
# exactly the three raw-resource groups (fifteen types) and 43 "Planetary Commodities" exactly one
# group per manufactured tier (sixty-eight types) - nothing else lives in either of the last two.
CATEGORY_RAW_RESOURCES = 42
CATEGORY_COMMODITIES = 43

# Which manufactured tier a category-43 group is, read off the group id - never off a name, an ordinal
# or a member count: 1042 Basic (15 types), 1034 Refined (24), 1040 Specialized (21), 1041 Advanced (8).
# Measured customs multipliers are 5 for the raw resources and 400 / 7200 / 60000 / 1200000 for
# tiers 1..4.
COMMODITY_TIER_BY_GROUP = {1042: 1, 1034: 2, 1040: 3, 1041: 4}

# The three processor classes, cheapest plant first. Group 1028 carries no marker saying which is
# which - measured, the only attributes on those eighteen types are cpuLoad, powerLoad and
# planetRestriction - but their fitting cost separates them without ambiguity: 200 CPU / 800 PG for
# the eight Basic Industry Facilities, 500 / 700 for the eight Advanced ones, 1100 / 400 for the two
# High-Tech Production Plants. Ordering the distinct cpuLoad values in the group recovers the ladder.
FACILITY_CLASSES = ("basic", "advanced", "high_tech")

# The sections a readable planet_industry.json must carry, in document order.
PI_SECTIONS = ("planet_types", "resources", "commodities", "schematics", "structures",
               "command_centers", "tax_factors")

# The sections a readable market_types.json must carry: the group index and the category index that
# points into it. Both are keyed by English name, because that is what a caller types to `market`.
MARKET_TYPE_SECTIONS = ("groups", "categories")


def _read(name: str) -> dict:
    for base in (Path(paths.data_dir()), PACKAGE_DATA_DIR):
        candidate = base / name
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"{name} not found in {paths.data_dir()} or {PACKAGE_DATA_DIR}")


def load() -> dict:
    """Return {'grades': {...}, 'races': {...}, 'build': int}."""
    grades = _read("clone_grades.json")
    races = _read("bloodline_races.json")
    return {"grades": grades, "races": races}


@dataclass(frozen=True)
class SkillInfo:
    """One skill as the SDE describes it: what training costs and what must come first."""

    type_id: int
    name: str
    rank: int                      # training time multiplier; SP cost scales linearly with it
    primary: str                   # character attribute driving this skill's SP/hour
    secondary: str
    prerequisites: dict[int, int]  # required skill type_id -> required level
    published: bool = True         # False for retired or test entries CCP still ships


def skill_catalog() -> dict[int, SkillInfo]:
    """{type_id: SkillInfo} for every catalogued skill, whether trained or not.

    Raises FileNotFoundError when no snapshot is installed, ValueError when the installed
    document predates this schema (it is deliberately named differently from the old
    ``skill_attrs.json``, so a stale file can never masquerade as a catalog)."""
    rows = _read("skill_catalog.json").get("skills")
    if not isinstance(rows, dict):
        raise ValueError("the local skill catalog is not in the expected format - run: eve-skills update-data")
    catalog: dict[int, SkillInfo] = {}
    for key, row in rows.items():
        try:
            catalog[int(key)] = SkillInfo(
                type_id=int(key),
                name=str(row["name"]),
                rank=int(row["rank"]),
                primary=str(row.get("pri") or ""),
                secondary=str(row.get("sec") or ""),
                prerequisites={int(k): int(v) for k, v in (row.get("pre") or {}).items()},
                published=bool(row.get("pub", True)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"skill catalog entry {key} is malformed - run: eve-skills update-data") from exc
    return catalog


def blueprint_materials() -> dict:
    """{blueprint type id: {activity: row}} exactly as the SDE describes it.

    Only the inner mapping is returned: ``source``/``build``/``fetched`` belong to the document,
    which is what `doctor` lines up against the other four files. A row is
    ``{"m": {material type id: quantity}, "p": [product type id, units], "t": seconds, "limit": int}``.

    Raises FileNotFoundError when no snapshot is installed, ValueError when the installed document
    is not in this shape - a cost computed from a half-parsed material list would be worse than no
    cost at all, so the caller gets the one action that fixes it."""
    rows = _read("blueprint_materials.json").get("blueprints")
    if not isinstance(rows, dict):
        raise ValueError("the local blueprint data is not in the expected format - run: eve-skills update-data")
    for key, activities in rows.items():
        if not isinstance(activities, dict) or not all(
            isinstance(row, dict) and isinstance(row.get("m"), dict)
            and isinstance(row.get("p"), list) and len(row["p"]) == 2
            for row in activities.values()
        ):
            raise ValueError(f"blueprint entry {key} is malformed - run: eve-skills update-data")
    return rows


def planet_industry() -> dict:
    """The planetary-industry document, envelope included.

    Unlike the loaders above this one hands back the whole document rather than its inner mapping:
    a recipe or a customs multiplier is only worth anything if `pi` can say which SDE build it came
    from, and the seven sections are addressed by name (PI_SECTIONS), so there is no single body to
    unwrap. Every key in it is a string - type ids, planet type ids, command center levels alike -
    because that is what survives a JSON round trip unchanged.

    Raises FileNotFoundError when no snapshot is installed, ValueError when the installed document is
    not in this shape: a fit or a customs bill priced off half a schematic would be worse than
    pointing at the one command that rebuilds it."""
    document = _read("planet_industry.json")
    if any(not isinstance(document.get(key), dict) for key in PI_SECTIONS):
        raise ValueError("the local planetary industry data is not in the expected format "
                         "- run: eve-skills update-data")
    for key, row in document["schematics"].items():
        if not isinstance(row, dict) or not isinstance(row.get("in"), dict) or not isinstance(row.get("out"), dict):
            raise ValueError(f"planetary industry schematic {key} is malformed - run: eve-skills update-data")
    return document


def system_planets() -> dict:
    """The per-system planet census built by `update-data`, envelope included.

    The whole document comes back rather than its inner mapping, because a count of planets is only
    worth quoting next to the SDE build it was counted from - and both halves are addressed by name
    (`planet_types` for the names, `systems` for the counts), so there is no single body to unwrap.

    Raises FileNotFoundError when no census is installed, ValueError when the installed document is not
    in this shape: printing a system as empty because its counts were misread would be worse than
    pointing at the one command that rebuilds it."""
    document = _read("system_planets.json")
    if not isinstance(document.get("planet_types"), dict) or not isinstance(document.get("systems"), dict):
        raise ValueError("the local planet census is not in the expected format - run: eve-skills update-data")
    return document


def market_types() -> dict:
    """The market-listed type index built by `update-data`, envelope included.

    Whole document again, for the same reason as the census: a group's contents are only worth quoting
    beside the build they were read from, and the two halves (`groups`, `categories`) are addressed by
    name rather than unwrapped into one body.

    Raises FileNotFoundError when no index is installed, ValueError when the installed document is not
    in this shape - `market --group` would otherwise price a silently truncated list of types and look
    exactly like a run that had been asked for by hand."""
    document = _read("market_types.json")
    if any(not isinstance(document.get(key), dict) for key in MARKET_TYPE_SECTIONS):
        raise ValueError("the local market type index is not in the expected format "
                         "- run: eve-skills update-data")
    for key, row in document["groups"].items():
        if not isinstance(row, dict) or not isinstance(row.get("types"), dict):
            raise ValueError(f"market type index group {key} is malformed - run: eve-skills update-data")
    # A category naming a group the index does not hold would expand to a shorter list than the game's
    # own item list, and `--category` would price that silently. `update-data` only ever writes names it
    # has just indexed, so a mismatch means this document did not come from it.
    for key, row in document["categories"].items():
        groups = row.get("groups") if isinstance(row, dict) else None
        if not isinstance(groups, list):
            raise ValueError(f"market type index category {key} is malformed - run: eve-skills update-data")
        missing = next((name for name in groups if name not in document["groups"]), None)
        if missing is not None:
            raise ValueError(f"market type index category {key} names group '{missing}', which the index "
                             f"has no types for - run: eve-skills update-data")
    return document


def stamp_age_days(fetched, now: float | None = None) -> float | None:
    """Age in days of an SDE ``fetched`` stamp (plain date or ISO datetime); None when unusable."""
    if not isinstance(fetched, str) or not fetched:
        return None
    try:
        dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # SDE releaseDate is a plain date; interpret as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    reference = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    return (reference - dt).total_seconds() / 86400


def data_age_days(data: dict) -> float | None:
    """Age of the local alpha-caps snapshot in days, or None when unknown."""
    return stamp_age_days(data["grades"].get("fetched"))


def _jsonl(zf: zipfile.ZipFile, name: str) -> Iterator[dict]:
    """Parsed documents of one jsonl member, streamed so a 150 MB member never lands in memory."""
    with zf.open(name) as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _english(value) -> str:
    """The English name out of an SDE row, whether the row spells ``name`` as a localized mapping or
    as a plain string; empty when it has none, which is how a nameless row gets dropped."""
    if isinstance(value, dict):
        value = value.get("en")
    return str(value).strip() if value else ""


def _transform_clone_grades(docs) -> dict:
    return {
        str(grade["_key"]): {
            "name": grade["name"],
            "caps": {str(s["typeID"]): s["level"] for s in grade["skills"]},
        }
        for grade in docs
    }


def _transform_bloodlines(docs) -> dict:
    return {str(bloodline["_key"]): bloodline["raceID"] for bloodline in docs}


def _transform_skill_catalog(dogma_docs, attribute_docs, type_docs) -> dict:
    """Skill rows keyed by type id: {"name", "rank", "pri", "sec", "pre", ["pub": false]}.

    A skill is any type carrying primaryAttribute/secondaryAttribute; its rank is the
    ``skillTimeConstant`` multiplier and its prerequisites are the requiredSkillN pair.
    Names come from types.jsonl (English) so a never-trained skill still resolves."""
    attr_names = {d["_key"]: str(d.get("name") or "").lower() for d in attribute_docs}
    skills: dict[str, dict] = {}
    for doc in dogma_docs:
        attrs = {a["attributeID"]: a.get("value") for a in doc.get("dogmaAttributes", [])}
        if ATTR_PRIMARY not in attrs or ATTR_SECONDARY not in attrs:
            continue
        pre = {}
        for skill_attr, level_attr in PREREQUISITE_ATTRS:
            if skill_attr in attrs:
                # The level attribute is always present in practice; default 1 rather than
                # silently dropping a requirement the SDE clearly states.
                pre[str(int(attrs[skill_attr]))] = int(attrs.get(level_attr) or 1)
        skills[str(doc["_key"])] = {
            "name": None,
            "rank": int(attrs.get(ATTR_RANK) or 0),
            "pri": attr_names.get(int(attrs[ATTR_PRIMARY]), ""),
            "sec": attr_names.get(int(attrs[ATTR_SECONDARY]), ""),
            "pre": pre,
        }
    for doc in type_docs:  # types.jsonl is huge; only the skill rows survive it
        row = skills.get(str(doc["_key"]))
        if row is None:
            continue
        row["name"] = _english(doc.get("name"))
        if not doc.get("published", True):
            row["pub"] = False
    return {key: row for key, row in skills.items() if row["name"]}


def _activity_row(entry: dict, limit: int) -> dict | None:
    """One activity as ``{"m", "p", "t", "limit"}``, or None when it has no cost to state.

    An activity with no materials has nothing to price, and one with no products builds nothing
    anyone asked the price of - both are dead weight in a document whose only reader wants run
    economics. A blueprint may list extra outputs it produces only occasionally next to the item it
    is really for, so the product recorded here is the certain one: costing the occasional bonus as
    though it were the yield would quietly understate the run."""
    materials = entry.get("materials") or []
    products = entry.get("products") or []
    if not materials or not products:
        return None
    product = next((p for p in products if not p.get("isProbability")), products[0])
    return {
        "m": {str(material["typeID"]): int(material["quantity"]) for material in materials},
        "p": [str(product["typeID"]), int(product["quantity"])],
        "t": int(entry.get("time") or 0),
        "limit": limit,
    }


def _transform_blueprint_materials(docs) -> dict:
    """Blueprint rows keyed by blueprint type id: {"manufacturing"|"reaction": row}.

    Only manufacturing and reaction survive, because they are the two activities that consume a
    material list. Copying spends a slot and time on a blueprint you already hold, the research
    activities burn data cores and skill points, and none of those inputs is something a market
    price can be looked up for - keeping them would fill the document with rows no cost can be
    attached to. Invention is deliberately left out for now: a T2 blueprint is *invented*, so a
    manufacturing run sits behind a random number of attempts whose decrypts and data cores only
    average out over many tries. Modelling that weighted attempt count is a feature of its own, not
    an input this document is missing - once the blueprint is in hand, its manufacturing row is
    exactly what building one costs.

    ``docs`` is the live generator over the zip member, so every row is seen once."""
    blueprints: dict[str, dict] = {}
    for doc in docs:
        limit = int(doc.get("maxProductionLimit") or 0)
        activities = {}
        for name, entry in (doc.get("activities") or {}).items():
            if name not in INDUSTRY_ACTIVITIES:
                continue
            row = _activity_row(entry, limit)
            if row is not None:
                activities[name] = row
        # A blueprint with no surviving activity cannot be built from goods at all; recording an
        # empty entry would only give the caller a key to trip over.
        if activities:
            blueprints[str(doc["_key"])] = activities
    return blueprints


def _number(value) -> float:
    """A dogma figure as a float; a type that does not carry the attribute reads as 0.0 instead of
    None so no consumer has to branch on a missing key. Measured: all 130 PI structures carry their
    load or output pair and all 83 commodities carry their multiplier, so this only fires for a row
    CCP ships without one."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _planet_name(name: str) -> str:
    """``Planet (Barren)`` -> ``Barren``.

    The SDE has no table of planet type names: the only place id 2016 is spelled out at all is an
    unpublished marker type in types.jsonl named "Planet (Barren)". Once the id is known to be a
    planet type the wrapper carries no information, so it comes off; a name without the wrapper is
    passed through untouched rather than chopped at a parenthesis."""
    stripped = (name or "").strip()
    if stripped.startswith("Planet (") and stripped.endswith(")"):
        return stripped[len("Planet ("):-1].strip()
    return stripped


def _transform_planet_industry(schematic_docs, dogma_docs, type_docs, group_docs) -> dict:
    """The planetary-industry document: planet_types, resources, commodities, schematics, structures,
    command_centers and tax_factors.

    The SDE describes a planetary installation sideways. ``planetSchematics.jsonl`` names facility
    *pin types* and input/output type ids but never a planet; the fitting cost of every structure
    lives in dogma; and only ``planetRestriction`` connects an item to a planet type at all. Joining
    those views here is what lets `pi` answer "what does this recipe eat, on which planets, in which
    class of plant, and what will customs take" out of one local read instead of a wiki page.

    Nothing in here knows how many planets, commodities or command center levels exist. The planet
    list is whatever ``planetRestriction`` values appear on planetary items; a tier comes from the
    type's group and that group's category; the facility class comes from the fitting ladder inside
    group 1028; and a command center's level is its own ``requiredSkill1Level``, with the base unit -
    which carries no level attribute at all - being level 0.

    Two measured facts decide what may be filtered out. All forty upgraded command centers are
    ``published: false`` (only the eight base units are published), and the eight "Planet (…)" marker
    types that give a planet id its name are unpublished as well - so an unpublished filter applied
    without exceptions would leave one command center level per planet and no planet names whatsoever.
    Everything else of interest is published in both builds checked here, so the exception stays narrow:
    unpublished rows survive only as a command center level, as a planet name, or when a recipe eats
    or makes them.

    Each of the four members is drained exactly once, in parameter order: the recipe rows decide which
    type ids matter, dogma then keeps the ~220 rows that either sit on a planet, carry a customs
    multiplier or appear in a recipe, and only those ids are looked for in types.jsonl. That is what
    keeps the 150 MB types member and the 27 MB typeDogma member from being materialised for a feature
    that needs a few hundred rows."""
    recipes = []
    recipe_types: set[int] = set()
    for doc in schematic_docs:
        sid, name = doc.get("_key"), _english(doc.get("name"))
        # No id to key it by or no name to show it under; measured, all 68 rows have both.
        if sid is None or not name:
            continue
        # Measured shape of a row in both builds here: ``pins`` is a flat list of facility type ids -
        # not objects - and each entry of ``types`` is ``{"_key": type id, "isInput": bool,
        # "quantity": int}``. Quantities are summed rather than assigned; measured one row per type per
        # recipe, so the summing only matters if CCP ever splits a line across two rows.
        pins, inputs, outputs = [], {}, {}
        for pin in doc.get("pins") or []:
            if pin is not None:
                pins.append(int(pin))
        for row in doc.get("types") or []:
            if row.get("_key") is None:
                continue
            tid = int(row["_key"])
            bucket = inputs if row.get("isInput") else outputs
            bucket[tid] = bucket.get(tid, 0) + int(row.get("quantity") or 0)
        recipe_types.update(pins)
        recipe_types.update(inputs)
        recipe_types.update(outputs)
        recipes.append({"id": int(sid), "name": name, "cycle": int(doc.get("cycleTime") or 0),
                        "pins": pins, "in": inputs, "out": outputs})

    # Dogma pass. A type belongs to planetary industry when it sits on a planet, carries a customs
    # multiplier, or is named by a recipe - 222 rows out of ~29k in build 3503375 - and that test needs
    # nothing from types.jsonl, which is what lets the passes run in parameter order.
    attrs: dict[int, dict] = {}
    for doc in dogma_docs:
        tid = doc.get("_key")
        if tid is None:
            continue
        values = {int(a["attributeID"]): a.get("value") for a in doc.get("dogmaAttributes") or []
                  if a.get("attributeID") is not None}
        wanted = (ATTR_PLANET_RESTRICTION in values or ATTR_IMPORT_TAX_MULTIPLIER in values
                  or ATTR_EXPORT_TAX_MULTIPLIER in values or int(tid) in recipe_types)
        if wanted:
            attrs[int(tid)] = values

    # Planet types are whatever that restriction attribute points at, never a hard-coded eight. In the
    # builds checked here that is exactly the eight colonisable types: the two other "Planet (…)"
    # marker types (Shattered, Scorched Barren) carry no PI items and so stay out.
    planet_ids = {int(values[ATTR_PLANET_RESTRICTION]) for values in attrs.values()
                  if values.get(ATTR_PLANET_RESTRICTION) is not None}

    names: dict[int, dict] = {}
    for doc in type_docs:      # types.jsonl is huge; only the ids named above survive it
        tid = doc.get("_key")
        if tid is None or int(tid) not in (set(attrs) | recipe_types | planet_ids):
            continue
        name = _english(doc.get("name"))
        if not name:
            continue            # an id with no English name has nothing to show
        names[int(tid)] = {"name": name, "group": doc.get("groupID"),
                           "published": bool(doc.get("published", True))}

    groups: dict[int, dict] = {}
    for doc in group_docs:
        gid = doc.get("_key")
        if gid is not None:
            groups[int(gid)] = {"category": doc.get("categoryID")}

    def tier_of(tid: int) -> int | None:
        """Tier of a type id, or None when it is no PI commodity at all. Both halves of the answer come
        out of the group record: category 42 (Planetary Resources) is what an extractor pulls and
        nothing manufactures, so tier 0, while category 43 (Planetary Commodities) holds one group per
        manufactured tier. Measured on both builds checked here, those two categories hold exactly the
        fifteen raw resources and the sixty-eight commodities and nothing else."""
        row = names.get(tid)
        if row is None or row["group"] is None:
            return None
        group = groups.get(int(row["group"]))
        if group is None:
            return None
        if group["category"] == CATEGORY_RAW_RESOURCES:
            return 0
        if group["category"] == CATEGORY_COMMODITIES:
            return COMMODITY_TIER_BY_GROUP.get(int(row["group"]))
        return None

    # Structures: the groups of category 41 that cost command center resources, each pinned to the
    # planet its own planetRestriction names. Planetary Links (1036) are absent from PI_ROLE_BY_GROUP -
    # they move goods between planets and draw nothing.
    items: dict[int, dict] = {}
    for tid in sorted(names):
        row = names[tid]
        group_id = int(row["group"]) if row["group"] is not None else None
        if group_id not in PI_ROLE_BY_GROUP:
            continue
        if not row["published"] and group_id != GROUP_COMMAND_CENTERS:
            # Every upgraded command center is unpublished and is a level you can actually fit; an
            # unpublished structure outside that group (measured: none in these builds) is not.
            continue
        values = attrs.get(tid) or {}
        if values.get(ATTR_PLANET_RESTRICTION) is None:
            continue            # a structure that names no planet cannot be placed on one
        items[tid] = {"name": row["name"], "group": group_id, "attrs": values,
                      "planet": int(values[ATTR_PLANET_RESTRICTION])}

    cpu_ladder = sorted({_number(i["attrs"].get(ATTR_CPU_LOAD)) for i in items.values()
                         if i["group"] == GROUP_PROCESSORS})

    def facility_class(values) -> str:
        """Which class of processor a group-1028 type is, by its fitting cost - see FACILITY_CLASSES.
        A type missing cpuLoad lands on the cheapest rung rather than failing the whole document."""
        cpu = _number(values.get(ATTR_CPU_LOAD))
        rank = cpu_ladder.index(cpu) if cpu in cpu_ladder else 0
        return FACILITY_CLASSES[min(rank, len(FACILITY_CLASSES) - 1)]

    structures: dict[str, dict] = {}
    command_centers: dict[str, dict] = {}
    harvested_by_planet: dict[str, set] = {}
    tax_factors: dict[str, float] = {}
    for tid, item in items.items():     # ascending type id, so anything read from "the first" is stable
        values = item["attrs"]
        role = PI_ROLE_BY_GROUP[item["group"]]
        if item["group"] == GROUP_PROCESSORS:
            role = f"{role}_{facility_class(values)}"   # processor_basic / _advanced / _high_tech
        entry = {"name": item["name"], "role": role, "planet_type": item["planet"]}
        if item["group"] == GROUP_COMMAND_CENTERS:
            # The command center is the one structure here that supplies rather than draws, so its row
            # carries cpuOutput/powerOutput under these keys - they are exactly what a fit check
            # compares the draw against. Measured: no PI type carries both a load and an output
            # attribute, so there is never a choice to make between the two readings.
            entry["cpu"] = _number(values.get(ATTR_CPU_OUTPUT))
            entry["power"] = _number(values.get(ATTR_POWER_OUTPUT))
        else:
            entry["cpu"] = _number(values.get(ATTR_CPU_LOAD))
            entry["power"] = _number(values.get(ATTR_POWER_LOAD))
        if item["group"] == GROUP_EXTRACTOR_CONTROL_UNITS:
            # An ECU bills its body and its head separately, and the head figures (1690/1691) exist on
            # exactly those eight types and nowhere else - so only this role carries them.
            entry["head_cpu"] = _number(values.get(ATTR_ECU_HEAD_CPU))
            entry["head_power"] = _number(values.get(ATTR_ECU_HEAD_POWER))
        structures[str(tid)] = entry

        if item["group"] == GROUP_COMMAND_CENTERS:
            # The level comes straight off the type. Measured: 6 levels for every planet type, and the
            # base unit is the one without requiredSkill1Level, which reads as level 0.
            level = str(int(_number(values.get(ATTR_REQUIRED_SKILL1_LEVEL))))
            command_centers.setdefault(str(item["planet"]), {})[level] = {
                "type_id": tid, "cpu": entry["cpu"], "power": entry["power"]}
        elif item["group"] == GROUP_EXTRACTORS:
            # What a planet offers is the join of its extractors onto harvesterType - there is no table
            # of planet inventories to read. Measured: exactly five resources per planet type.
            pulled = values.get(ATTR_HARVESTER_TYPE)
            if pulled is not None:
                harvested_by_planet.setdefault(str(item["planet"]), set()).add(int(pulled))
        elif item["group"] == GROUP_SPACEPORTS and not tax_factors:
            # The launchpad's own rates are the planet's customs factors, and measured all eight carry
            # the same pair (0.5 import / 1.0 export). A command center also carries exportTax, at 3.0,
            # which is a modifier on that structure and not the answer to "what does customs take here".
            if values.get(ATTR_IMPORT_TAX) is not None and values.get(ATTR_EXPORT_TAX) is not None:
                tax_factors = {"import": _number(values[ATTR_IMPORT_TAX]),
                               "export": _number(values[ATTR_EXPORT_TAX])}

    recipe_io = {tid for recipe in recipes for tid in list(recipe["in"]) + list(recipe["out"])}
    commodities: dict[str, dict] = {}
    for tid in sorted(names):
        tier = tier_of(tid)
        if tier is None:
            continue
        # The unpublished exception, narrowed: a commodity a recipe actually eats or makes has to stay
        # nameable and taxable even if CCP hides it; one nobody references can go. Measured: every
        # commodity in both builds here is published, so this only fires for future scratch rows.
        if not names[tid]["published"] and tid not in recipe_io:
            continue
        values = attrs.get(tid) or {}
        tax = values.get(ATTR_IMPORT_TAX_MULTIPLIER)
        if tax is None:
            tax = values.get(ATTR_EXPORT_TAX_MULTIPLIER)  # measured equal to import on all 83
        if tax is None:
            continue          # no multiplier means no customs answer to give; measured: none such
        commodities[str(tid)] = {"name": names[tid]["name"], "tier": tier, "tax": _number(tax)}

    schematics: dict[str, dict] = {}
    for recipe in recipes:
        tiers = [tier for tier in (tier_of(tid) for tid in recipe["out"]) if tier is not None]
        pins = [items[pin] for pin in recipe["pins"] if pin in items]
        # A row whose output is no commodity cannot be placed in the tier chain, and one whose pins are
        # no PI processor has no plant to name; measured, all 68 rows resolve on both counts.
        if not tiers or not pins:
            continue
        cheapest = min(pins, key=lambda item: _number(item["attrs"].get(ATTR_CPU_LOAD)))
        schematics[str(recipe["id"])] = {
            "name": recipe["name"],
            "cycle": recipe["cycle"],
            "in": {str(tid): qty for tid, qty in sorted(recipe["in"].items())},
            "out": {str(tid): qty for tid, qty in sorted(recipe["out"].items())},
            # Measured: exactly one output per row, so max() only decides what a future multi-output
            # recipe would mean - the most valuable thing it makes.
            "tier": max(tiers),
            # The weakest plant that can run it; measured, every pin of a schematic is one class.
            "facility": facility_class(cheapest["attrs"]),
            "planet_types": sorted({pin["planet"] for pin in pins}),
        }

    planet_types = {}
    for pid in sorted(planet_ids):
        row = names.get(pid)
        if row is None:
            continue          # measured: every restriction resolves to a "Planet (…)" type row
        planet_types[str(pid)] = _planet_name(row["name"])

    return {
        "planet_types": planet_types,
        "resources": {planet: sorted(pulled) for planet, pulled in sorted(harvested_by_planet.items())},
        "commodities": commodities,
        "schematics": schematics,
        "structures": structures,
        "command_centers": command_centers,
        "tax_factors": tax_factors,
    }


def _transform_system_planets(planet_docs, type_docs) -> dict:
    """The planet census: `planet_types` (id -> name) and `systems` (solar system id -> {type id: count}).

    `mapPlanets.jsonl` is 50.9 MB of mostly irrelevant detail - orbits, heightmaps, moon lists - for
    68,407 planets in 8,088 systems (measured, build 3494416). Counting it here once turns that into a
    463 KB document small enough to read on every command, and the caller hands rows one at a time so
    the member is never held in memory whole.

    Two things the shape has to survive. Ten planet types appear in the file, not the eight that
    `planet_industry.json` names: measured on build 3494416 there are also 713 Shattered (30889) and a
    single Scorched Barren (73911), which no extractor can touch but which are still planets in the
    system you are weighing - so every type present is counted, and named from types.jsonl rather than
    from `planet_industry`'s shorter list. And a per-system value is a sparse {type id: count} map
    rather than a positional vector over all ten: measured on that build the vector would cost 348 KB
    against these 463 KB, which is a trade worth making because `pi fit` never parses this file at all,
    while a vector would silently misalign the day CCP ships an eleventh planet type.

    Keys are written in ascending numeric order (the dict comprehension preserves insertion order) so
    that rebuilding the same build produces a diff of nothing rather than a shuffle."""
    census: dict[int, dict[int, int]] = {}
    for doc in planet_docs:
        system_id, type_id = doc.get("solarSystemID"), doc.get("typeID")
        # A planet with no system to count it in, or no type to count it as; measured, none such.
        if system_id is None or type_id is None:
            continue
        per_system = census.setdefault(int(system_id), {})
        per_system[int(type_id)] = per_system.get(int(type_id), 0) + 1

    listed = {type_id for per_system in census.values() for type_id in per_system}
    names: dict[int, str] = {}
    for doc in type_docs:      # types.jsonl is huge; only the ids actually counted survive it
        type_id = doc.get("_key")
        if type_id is None or int(type_id) not in listed:
            continue
        name = _english(doc.get("name"))
        if name:
            names[int(type_id)] = _planet_name(name)

    return {
        # An id with no types.jsonl row still gets counted, and gets a placeholder name rather than
        # vanishing from the census that named it.
        "planet_types": {str(t): names.get(t, f"unnamed planet type {t}") for t in sorted(listed)},
        "systems": {str(system): {str(t): per_system[t] for t in sorted(per_system)}
                    for system, per_system in sorted(census.items())},
    }


def _transform_market_types(type_docs, group_docs, category_docs) -> dict:
    """The market-listed type index: `groups` (group name -> its types) and `categories`
    (category name -> its group names).

    This is what lets `market --group "Basic Commodities - Tier 1"` price its fifteen commodities - or
    `--category "Planetary Commodities"` all sixty-eight of them - without typing their names, and it
    exists as a document of its own because none of the three members it comes from ships small:
    `types.jsonl` is 152.9 MB uncompressed and is only ever
    streamed, while `groups.jsonl` (0.81 MB) and `categories.jsonl` (0.01 MB) are the whole of the
    other two. Reduced to what a price run needs - published, market-listed types with an English name
    - it is 19,551 types in 814 groups across 32 categories, and 0.94 MB on disk (measured, build
    3494416), so a group lookup costs one JSON parse instead of a 153 MB scan.

    Two rules the shape depends on, both measured on build 3494416:

    * "market-listed" means the type row carries a `marketGroupID`. That flag is what says ESI will
      price the type at all; it is not the grouping key. Grouping by `groupID` (and only then looking
      up `groups.jsonl.categoryID`) gives 814 groups in 32 categories, with the four planetary
      commodity groups landing in "Planetary Commodities" where a player expects them. Grouping by
      `marketGroupID` instead gives 1,614 groups in 37 categories and leaves about 9,910 types under a
      category nothing can name - the market groups are a merchandising tree with holes in it.
    * A type survives when it is published, market-listed, and has both a `groupID` and an English
      name: 52,999 rows -> 27,116 published -> 19,786 market-listed -> 19,551 of both. Every one of
      those resolves to a `groups.jsonl` row (zero misses); five of them sit in three groups the SDE
      itself marks unpublished, and they stay in - the type is published and priced, which is the only
      thing this command cares about, and dropping it would make "what is in group X" answer different
      from the game's own item list.

    Group keys are English names because that is what a caller types, and a duplicate name would merge
    two groups and silently price the wrong items. Measured: none of the 814 listed groups shares an
    English name (six duplicates exist across all groups - `Asteroid Belt`, `Miscellaneous`,
    `Laboratory`, `Encounter Surveillance System`, `Services`, `Phased Asteroid` - but each belongs to
    a family with no market-listed type), and all 32 category names are unique. Rather than trust that
    forever, a name used by more than one listed group is keyed as `"Name (<id>)"`, which keeps both
    groups reachable and makes the collision visible in any listing.

    Keys are written sorted - groups and categories by name, types by id - so rebuilding the same build
    diffs as nothing rather than as a shuffle."""
    category_names = {int(doc["_key"]): _english(doc.get("name")) for doc in category_docs}
    group_rows = {int(doc["_key"]): doc for doc in group_docs if doc.get("_key") is not None}

    members: dict[int, dict[int, str]] = {}
    for doc in type_docs:      # types.jsonl is huge; only listed rows survive it
        if not doc.get("published", True) or doc.get("marketGroupID") is None:
            continue
        group_id, name = doc.get("groupID"), _english(doc.get("name"))
        if group_id is None or not name or doc.get("_key") is None:
            continue
        members.setdefault(int(group_id), {})[int(doc["_key"])] = name

    # Only groups with something to price are indexed; the SDE lists far more groups than trade.
    listed = sorted(members)
    group_key = _unique_name_keys({gid: _english(group_rows[gid].get("name")) if gid in group_rows
                                   else "" for gid in listed})
    category_of = {gid: group_rows.get(gid, {}).get("categoryID") for gid in listed}
    category_key = _unique_name_keys({int(cid): category_names.get(int(cid), "")
                                      for cid in {int(c) for c in category_of.values() if c is not None}})

    groups: dict[str, dict] = {}
    by_category: dict[int, list[str]] = {}
    for gid in listed:
        name = group_key[gid]
        cid = category_of[gid]
        groups[name] = {
            "id": gid,
            "category_id": None if cid is None else int(cid),
            # A group whose category row is missing still gets a name here, so the caller can print
            # what it found instead of a blank: measured zero such rows, so this is only the shape.
            "category": category_key.get(int(cid)) if cid is not None else None,
            "types": {str(t): members[gid][t] for t in sorted(members[gid])},
        }
        if cid is not None:
            by_category.setdefault(int(cid), []).append(name)
    categories = {category_key[cid]: {"id": cid, "groups": sorted(names)}
                  for cid, names in sorted(by_category.items(), key=lambda item: category_key[item[0]])}
    return {"groups": {name: groups[name] for name in sorted(groups)}, "categories": categories}


def _unique_name_keys(names: dict[int, str]) -> dict[int, str]:
    """{id: name} where any name used twice becomes `"Name (<id>)"`.

    Iteration order is kept, so a pair of qualified keys lands next to each other in a listing instead
    of scattered by their suffix; an id with no English name is keyed by its id alone, which is ugly but
    reachable - dropping it would make a whole group unquotable."""
    counts: dict[str, int] = {}
    for name in names.values():
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {key: (name if name and counts.get(name, 0) == 1 else (f"{name} ({key})" if name else f"id {key}"))
            for key, name in names.items()}


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "eve-skills/0.1 (data update)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def latest_build() -> int:
    doc = json.loads(_fetch(f"{SDE_BASE}/latest.jsonl").decode())
    return doc["buildNumber"]


def update(build: int | None = None) -> dict:
    """Download the SDE zip and refresh alpha caps, bloodline races, the skill catalog, blueprint
    material lists, the planetary industry document, the planet census and the market type index.

    The run holds ``update.lock``: two `update-data` processes would otherwise both pull
    ~100 MB and interleave, leaving the seven files describing different builds (and
    fighting over one fixed `.tmp` name). Each file is replaced atomically, so a reader
    never sees a half-written snapshot; the set as a whole switches build file by file."""
    dest = Path(paths.data_dir())
    with storage.file_lock(str(dest / "update.lock")):
        build = build or latest_build()
        src = f"eve-online-static-data-{build}-jsonl.zip"
        print(f"Downloading SDE build {build} (~100 MB)...")
        blob = _fetch(f"{SDE_BASE}/{src}")

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            # Each member is streamed and drained before the next one opens, so the
            # 150 MB types.jsonl never has to be held in memory.
            grades = _transform_clone_grades(_jsonl(zf, "cloneGrades.jsonl"))
            races = _transform_bloodlines(_jsonl(zf, "bloodlines.jsonl"))
            catalog = _transform_skill_catalog(
                _jsonl(zf, "typeDogma.jsonl"),
                _jsonl(zf, "dogmaAttributes.jsonl"),
                _jsonl(zf, "types.jsonl"),
            )
            blueprints = _transform_blueprint_materials(_jsonl(zf, "blueprints.jsonl"))
            # Planetary industry reads typeDogma and types a second time. That is deliberate: the two
            # transforms select different rows, and re-opening a member keeps both passes streaming -
            # measured on build 3503375, the extra read costs about two seconds and no memory.
            planet = _transform_planet_industry(
                _jsonl(zf, "planetSchematics.jsonl"),
                _jsonl(zf, "typeDogma.jsonl"),
                _jsonl(zf, "types.jsonl"),
                _jsonl(zf, "groups.jsonl"),
            )
            # The census counts mapPlanets.jsonl, a member nothing else reads, and re-reads types.jsonl
            # a third time for the two planet names `planet_industry` has no use for. Measured on build
            # 3494416 that whole count costs 2.6 s of wall clock and no resident memory, because rows of
            # a member are streamed one at a time.
            census = _transform_system_planets(
                _jsonl(zf, "mapPlanets.jsonl"),
                _jsonl(zf, "types.jsonl"),
            )
            # The market index needs types.jsonl a fourth time, plus groups and categories - two members
            # nothing else reads, 0.82 MB together. Measured on build 3494416 the whole pass costs 1.6 s of
            # wall clock, 1.57 s of it streaming the 152.85 MB member and 0.02 s for the other two; only
            # the 19,551 surviving rows are held. Re-opening a member instead of caching rows across
            # passes is what keeps the four reads of one file free of memory.
            market_index = _transform_market_types(
                _jsonl(zf, "types.jsonl"),
                _jsonl(zf, "groups.jsonl"),
                _jsonl(zf, "categories.jsonl"),
            )

        fetched = json.loads(_fetch(f"{SDE_BASE}/latest.jsonl").decode()).get("releaseDate", "")
        payloads = (
            ("clone_grades.json", {"source": src, "build": build, "fetched": fetched, "grades": grades}),
            ("bloodline_races.json", {"source": src, "build": build, "fetched": fetched, "races": races}),
            ("skill_catalog.json", {"source": src, "build": build, "fetched": fetched, "skills": catalog}),
            ("blueprint_materials.json",
             {"source": src, "build": build, "fetched": fetched, "blueprints": blueprints}),
            # The seven sections go in at the top level next to the envelope - there is no single body
            # key here, which is why planet_industry() returns the whole document.
            ("planet_industry.json", {"source": src, "build": build, "fetched": fetched, **planet}),
            # Same shape as planet_industry.json: the census sections sit next to the envelope.
            ("system_planets.json", {"source": src, "build": build, "fetched": fetched, **census}),
            # Both market halves are name-keyed and sit beside the envelope like the two above.
            ("market_types.json", {"source": src, "build": build, "fetched": fetched, **market_index}),
        )
        for name, payload in payloads:
            storage.atomic_write(str(dest / name), json.dumps(payload))

        # Distinct products rather than blueprints: several blueprints can build the same item, and
        # the number a user can act on is how many items have a material list behind them.
        products = {row["p"][0] for activities in blueprints.values() for row in activities.values()}

    return {
        "build": build,
        "grades": {race: {"name": g["name"], "skills": len(g["caps"])} for race, g in grades.items()},
        "catalog_skills": len(catalog),
        "blueprint_products": len(products),
        "pi_schematics": len(planet["schematics"]),
        "pi_planet_types": len(planet["planet_types"]),
        "pi_commodities": len(planet["commodities"]),
        # Levels, not types: what a user can act on is how many upgrade steps the document can price.
        "pi_command_center_levels": sum(len(levels) for levels in planet["command_centers"].values()),
        # Systems and planets, not the type list: what `system` can now answer is which of the 8,088
        # systems have a census at all, and how many planets it counts in total.
        "census_systems": len(census["systems"]),
        "census_planets": sum(sum(row.values()) for row in census["systems"].values()),
        "census_planet_types": len(census["planet_types"]),
        # Types, groups and categories - the three numbers that say what `market --group` and
        # `--category` can now expand, and the group count is also the number of distinct answers.
        "market_types": sum(len(row["types"]) for row in market_index["groups"].values()),
        "market_groups": len(market_index["groups"]),
        "market_categories": len(market_index["categories"]),
    }
