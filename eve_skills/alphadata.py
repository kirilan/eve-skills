"""Static Data Export snapshots: alpha clone skill caps, the full skill catalog and blueprint material lists.

A snapshot ships with the repository in ./data; `eve-skills update-data` downloads the
current SDE zip and refreshes a copy under $XDG_DATA_HOME/eve-skills, which takes
precedence when present.

The skill catalog covers *every* catalogued skill - including ones the character has
never trained - so the planner can price them: name, training time multiplier ("rank"),
primary/secondary attribute and the prerequisite skills with their required levels.

Blueprint material lists cover the two activities that actually consume goods - manufacturing and
reaction - so a build cost can be computed from what a run eats, what it yields and how long it takes.
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
DATA_FILES = ("clone_grades.json", "bloodline_races.json", "skill_catalog.json", "blueprint_materials.json")

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
    which is what `doctor` lines up against the other three files. A row is
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
        name = doc.get("name")
        row["name"] = (name.get("en") if isinstance(name, dict) else name) or ""
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


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "eve-skills/0.1 (data update)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def latest_build() -> int:
    doc = json.loads(_fetch(f"{SDE_BASE}/latest.jsonl").decode())
    return doc["buildNumber"]


def update(build: int | None = None) -> dict:
    """Download the SDE zip and refresh alpha caps, bloodline races, the skill catalog and
    blueprint material lists.

    The run holds ``update.lock``: two `update-data` processes would otherwise both pull
    ~100 MB and interleave, leaving the four files describing different builds (and
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

        fetched = json.loads(_fetch(f"{SDE_BASE}/latest.jsonl").decode()).get("releaseDate", "")
        payloads = (
            ("clone_grades.json", {"source": src, "build": build, "fetched": fetched, "grades": grades}),
            ("bloodline_races.json", {"source": src, "build": build, "fetched": fetched, "races": races}),
            ("skill_catalog.json", {"source": src, "build": build, "fetched": fetched, "skills": catalog}),
            ("blueprint_materials.json",
             {"source": src, "build": build, "fetched": fetched, "blueprints": blueprints}),
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
    }
