"""The operation's own geography: where jobs run, who holds what, where goods are sold and hauled.

Everything here comes from one optional file, ``<config_dir>/operation.json``. Without it every
command keeps its built-in behaviour (NPC station rates, the five trade hubs, corporation reads where a
command asks for them). With it, commands can name a *site* - a facility plus the characters that work
there and whether they share a corporation hangar or each hold their own stock - instead of repeating
ids and multipliers on every command line.

Why sites rather than one global switch: an operation in the middle of a move has two at once. A
corporation in high-sec, read through characters with roles, keeps running while other characters
already build out of their personal hangars in somebody else's structure, where no corporation hangar
is ours to read. Each command answers for one site at a time.

Facility bonuses are stored as the multipliers the install screen applies, but they can be *derived*
from what a player actually knows about a structure - its hull, its rigs, the system's security - so
nobody has to work out ``0.99 * (1 - 0.02 * 2.1)`` by hand. Values nobody has filled in stay visibly
unknown: ``operation show`` lists them, because a facility silently treated as unrigged understates a
structure's advantage and one silently treated as rigged overstates it.

The file format (percentages are percent, multipliers are fractions)::

    {
      "default_site": "null",
      "sites": {
        "highsec": {"owner": "corp", "facility": "highsec", "readers": ["Alice Builder"]},
        "null": {"owner": "personal", "facility": "example-raitaru", "hub": "x-hub",
                 "characters": ["Carol Hauler", "Dave Miner"],
                 "freight_in": "jita->x-hub", "freight_out": "x-hub->jita"}
      },
      "facilities": {
        "highsec": {"location_id": 60003760, "system": "Jita"},
        "example-raitaru": {"location_id": 1040000000000, "system": "X-EXMPL", "tax_pct": 1.0,
                         "structure": "raitaru", "security": "null",
                         "rigs": {"manufacturing": {"me": "t1", "te": "t1"}, "invention": {"te": "t1"}}}
      },
      "hubs": {"x-hub": {"location_id": 1040000000001, "region_id": 10000060,
                          "system_id": 30004759, "reader": "Carol Hauler"}},
      "freight": {"jita->x-hub": {"per_m3": 500, "collateral_pct": 0, "min": 5000000},
                  "x-hub->jita": {"per_m3": 400, "collateral_pct": 1, "min": 10000000}}
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import paths

FILE_NAME = "operation.json"

ACTIVITIES = ("manufacturing", "reaction", "invention", "copying",
              "research_material", "research_time")
SCIENCE = ("invention", "copying", "research_material", "research_time")

# Engineering complex role bonuses. Manufacturing material -1 % on every hull; job time for
# manufacturing and science -15/20/30 %. Reactions get nothing from an engineering complex.
STRUCTURES: dict[str, dict] = {
    "raitaru": {"kind": "engineering", "me": 0.01, "te": 0.15},
    "azbel": {"kind": "engineering", "me": 0.01, "te": 0.20},
    "sotiyo": {"kind": "engineering", "me": 0.01, "te": 0.30},
    "athanor": {"kind": "refinery", "me": 0.0, "te": 0.0},
    "tatara": {"kind": "refinery", "me": 0.0, "te": 0.25},
    "station": {"kind": "npc", "me": 0.0, "te": 0.0},
}
# Rig bonus per tier before the security multiplier: material and time for manufacturing and
# reaction rigs, time for science rigs (invention, copying and research share one rig family).
RIG_ME = {"t1": 0.020, "t2": 0.024}
RIG_TE = {"t1": 0.20, "t2": 0.24}
SECURITY_MULTIPLIER = {"high": 1.0, "low": 1.9, "null": 2.1, "wormhole": 2.1}
OWNERS = ("corp", "personal")


class OperationError(RuntimeError):
    """operation.json says something that cannot be used; the message names the field."""


@dataclass(frozen=True)
class Facility:
    """Where jobs are installed and what the structure takes off them, per activity.

    `tax` is the owner's facility tax as a fraction; None means nobody wrote it down (an NPC station's
    is a policy constant the caller already knows, a structure's is not). `unknown` lists the values
    that are defaults because the file is silent about them, so a report can say so."""
    name: str
    system: str | None = None
    location_id: int | None = None
    tax: float | None = None
    material: dict[str, float] = field(default_factory=dict)
    time: dict[str, float] = field(default_factory=dict)
    derived_from: str | None = None
    unknown: tuple[str, ...] = ()

    def material_multiplier(self, activity: str) -> float:
        return self.material.get(activity, 1.0)

    def time_multiplier(self, activity: str) -> float:
        return self.time.get(activity, 1.0)


@dataclass(frozen=True)
class Hub:
    """A market the five built-in hubs do not cover - usually a player structure.

    `region_id` is required because every price history and every regional fallback is per region;
    `reader` is the stored character whose token reads the structure's market first."""
    key: str
    label: str
    location_id: int
    region_id: int
    system_id: int | None
    reader: str | None


@dataclass(frozen=True)
class Site:
    """A facility and the characters working it.

    `owner` "corp": stock and blueprints sit in the corporation's hangars, read through `readers`
    (characters whose roles open the corporation endpoints). "personal": every character in
    `characters` installs from their own hangar, and materials move between them by trade."""
    name: str
    owner: str
    facility: str
    characters: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()
    freight_in: str | None = None      # route that brings supplies from the buying hub
    freight_out: str | None = None     # route that takes finished goods to the selling hub
    hub: str | None = None             # the market next to the facility, if any


@dataclass(frozen=True)
class Freight:
    """A hauling price: per m3, a percentage of the declared collateral, and a minimum per contract."""
    route: str
    per_m3: float
    collateral_pct: float = 0.0
    minimum: float = 0.0

    def contract_cost(self, volume_m3: float, collateral: float) -> float:
        return max(self.minimum, self.per_m3 * volume_m3 + self.collateral_pct / 100.0 * collateral)

    def unit_cost(self, volume_m3: float, value: float) -> float:
        """One unit's share of a contract big enough that the minimum does not bind."""
        return self.per_m3 * volume_m3 + self.collateral_pct / 100.0 * value


@dataclass(frozen=True)
class Operation:
    path: str | None
    sites: dict[str, Site]
    facilities: dict[str, Facility]
    hubs: dict[str, Hub]
    freight: dict[str, Freight]
    default_site: str | None

    @property
    def configured(self) -> bool:
        return self.path is not None

    def site(self, name: str | None) -> Site | None:
        """The named site, else the default one, else None (no operation file: built-in behaviour)."""
        key = name or self.default_site
        if key is None:
            if name is None and len(self.sites) == 1:
                return next(iter(self.sites.values()))
            return None
        site = self.sites.get(key.lower())
        if site is None:
            raise OperationError(f"no site '{key}' in {self.path or FILE_NAME} - sites: "
                                 f"{', '.join(sorted(self.sites)) or 'none'}")
        return site

    def facility(self, name: str) -> Facility:
        found = self.facilities.get(name.lower())
        if found is None:
            raise OperationError(f"no facility '{name}' in {self.path or FILE_NAME} - facilities: "
                                 f"{', '.join(sorted(self.facilities)) or 'none'}")
        return found

    def route(self, origin: str, destination: str) -> Freight | None:
        return self.freight.get(f"{origin.lower()}->{destination.lower()}")


def file_path() -> str:
    return os.path.join(paths.config_dir(create=False), FILE_NAME)


def derive(structure: str | None, security: str | None, rigs: dict | None) -> tuple[dict, dict, list[str]]:
    """Material and time multipliers per activity from a structure, its rigs and the security class.

    material = (1 - role ME) * (1 - rig ME * security); time = (1 - role TE) * (1 - rig TE * security).
    The engineering-complex role bonus applies to manufacturing (material and time) and to science
    (time); a refinery's to reactions. Returns (material, time, notes)."""
    notes: list[str] = []
    hull = STRUCTURES.get((structure or "station").lower())
    if hull is None:
        raise OperationError(f"unknown structure '{structure}' - one of {', '.join(STRUCTURES)}")
    sec = SECURITY_MULTIPLIER.get((security or "high").lower())
    if sec is None:
        raise OperationError(f"unknown security '{security}' - one of {', '.join(SECURITY_MULTIPLIER)}")
    material: dict[str, float] = {}
    time: dict[str, float] = {}
    rigs = rigs or {}
    for activity, rig in rigs.items():
        if activity not in ACTIVITIES:
            raise OperationError(f"rigs: unknown activity '{activity}' - one of {', '.join(ACTIVITIES)}")
        for key in rig:
            if key not in ("me", "te"):
                raise OperationError(f"rigs.{activity}: '{key}' is not me or te")
            if rig[key] not in RIG_ME:
                raise OperationError(f"rigs.{activity}.{key}: '{rig[key]}' is not t1 or t2")
    for activity in ACTIVITIES:
        rig = rigs.get(activity) or {}
        role_me = role_te = 0.0
        if hull["kind"] == "engineering" and activity == "manufacturing":
            role_me, role_te = hull["me"], hull["te"]
        elif hull["kind"] == "engineering" and activity in SCIENCE:
            role_te = hull["te"]
        elif hull["kind"] == "refinery" and activity == "reaction":
            role_te = hull["te"]
        rig_me = RIG_ME[rig["me"]] * sec if "me" in rig and activity in ("manufacturing", "reaction") else 0.0
        rig_te = RIG_TE[rig["te"]] * sec if "te" in rig else 0.0
        m = (1 - role_me) * (1 - rig_me)
        t = (1 - role_te) * (1 - rig_te)
        if m != 1.0:
            material[activity] = round(m, 6)
        if t != 1.0:
            time[activity] = round(t, 6)
    if rigs:
        notes.append("rig bonuses apply only to the product groups each rig covers; check that the "
                     "structure's rigs cover what you build")
    return material, time, notes


def _number(raw, where: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise OperationError(f"{where} must be a number, got {raw!r}")
    value = float(raw)
    if (low is not None and value < low) or (high is not None and value > high):
        raise OperationError(f"{where} must be between {low:g} and {high:g}, got {value:g}")
    return value


def _names(raw, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise OperationError(f"{where} must be a list of character names")
    return tuple(x.strip() for x in raw)


def _facility(name: str, raw: dict) -> Facility:
    where = f"facilities.{name}"
    if not isinstance(raw, dict):
        raise OperationError(f"{where} must be an object")
    location = raw.get("location_id")
    if location is not None and (isinstance(location, bool) or not isinstance(location, int)):
        raise OperationError(f"{where}.location_id must be an integer id")
    unknown: list[str] = []
    tax = None
    if "tax_pct" in raw:
        tax = _number(raw["tax_pct"], f"{where}.tax_pct", low=0, high=100) / 100.0
    elif location is None or location > 2_147_483_647:
        unknown.append("tax_pct")
    derived_from = None
    if "material" in raw or "time" in raw:
        material = {k: _number(v, f"{where}.material.{k}", low=0.01, high=1.0)
                    for k, v in (raw.get("material") or {}).items()}
        time = {k: _number(v, f"{where}.time.{k}", low=0.01, high=1.0)
                for k, v in (raw.get("time") or {}).items()}
        for key in list(material) + list(time):
            if key not in ACTIVITIES:
                raise OperationError(f"{where}: unknown activity '{key}' - one of {', '.join(ACTIVITIES)}")
    else:
        material, time, _notes = derive(raw.get("structure"), raw.get("security"), raw.get("rigs"))
        if raw.get("structure"):
            rigs = raw.get("rigs")
            derived_from = (f"{raw['structure']} in {raw.get('security') or 'high'}-sec"
                            + (", rigs " + ", ".join(f"{a} {'/'.join(f'{k} {v}' for k, v in r.items())}"
                                                     for a, r in rigs.items()) if rigs else ", no rigs"))
            if "rigs" not in raw:
                unknown.append("rigs")
        elif location is not None and location > 2_147_483_647:
            unknown.append("structure/rigs")
    if location is None and raw.get("structure") not in (None, "station"):
        unknown.append("location_id")
    return Facility(name=name, system=raw.get("system"), location_id=location, tax=tax,
                    material=material, time=time, derived_from=derived_from, unknown=tuple(unknown))


def parse(document: dict, path: str | None = None) -> Operation:
    if not isinstance(document, dict):
        raise OperationError(f"{path or FILE_NAME} must hold a JSON object")
    facilities = {k.lower(): _facility(k.lower(), v) for k, v in (document.get("facilities") or {}).items()}
    sites: dict[str, Site] = {}
    for key, raw in (document.get("sites") or {}).items():
        where = f"sites.{key}"
        if not isinstance(raw, dict):
            raise OperationError(f"{where} must be an object")
        owner = raw.get("owner")
        if owner not in OWNERS:
            raise OperationError(f"{where}.owner must be one of {', '.join(OWNERS)}, got {owner!r}")
        facility = str(raw.get("facility") or "").lower()
        if facility not in facilities:
            raise OperationError(f"{where}.facility '{raw.get('facility')}' is not in facilities")
        characters = _names(raw.get("characters"), f"{where}.characters")
        readers = _names(raw.get("readers"), f"{where}.readers")
        if owner == "personal" and not characters:
            raise OperationError(f"{where}: a personal site lists its characters")
        sites[key.lower()] = Site(name=key.lower(), owner=owner, facility=facility,
                                  characters=characters, readers=readers,
                                  freight_in=(raw.get("freight_in") or None), freight_out=(raw.get("freight_out") or None),
                                  hub=(str(raw["hub"]).lower() if raw.get("hub") else None))
    hubs: dict[str, Hub] = {}
    for key, raw in (document.get("hubs") or {}).items():
        where = f"hubs.{key}"
        if not isinstance(raw, dict) or not isinstance(raw.get("location_id"), int):
            raise OperationError(f"{where} needs an integer location_id")
        if not isinstance(raw.get("region_id"), int):
            raise OperationError(f"{where} needs an integer region_id (Delve is 10000060)")
        system_id = raw.get("system_id")
        if system_id is not None and not isinstance(system_id, int):
            raise OperationError(f"{where}.system_id must be an integer id")
        hubs[key.lower()] = Hub(key=key.lower(), label=raw.get("label") or key, location_id=raw["location_id"],
                                region_id=raw["region_id"], system_id=system_id, reader=raw.get("reader"))
    freight: dict[str, Freight] = {}
    for key, raw in (document.get("freight") or {}).items():
        where = f"freight.{key}"
        if "->" not in key or not isinstance(raw, dict):
            raise OperationError(f"{where}: routes are named 'origin->destination' and hold an object")
        freight[key.lower()] = Freight(
            route=key.lower(), per_m3=_number(raw.get("per_m3", 0), f"{where}.per_m3", low=0),
            collateral_pct=_number(raw.get("collateral_pct", 0), f"{where}.collateral_pct", low=0, high=100),
            minimum=_number(raw.get("min", 0), f"{where}.min", low=0))
    for site in sites.values():
        for route in (site.freight_in, site.freight_out):
            if route and route.lower() not in freight:
                raise OperationError(f"sites.{site.name}: freight route '{route}' is not in freight")
    default = document.get("default_site")
    if default is not None and str(default).lower() not in sites:
        raise OperationError(f"default_site '{default}' is not in sites")
    return Operation(path=path, sites=sites, facilities=facilities, hubs=hubs, freight=freight,
                     default_site=str(default).lower() if default else None)


EMPTY = Operation(path=None, sites={}, facilities={}, hubs={}, freight={}, default_site=None)


def load(path: str | None = None) -> Operation:
    """The operation file, or EMPTY when there is none. A broken file is an error, never ignored."""
    path = path or os.environ.get("EVE_SKILLS_OPERATION") or file_path()
    try:
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
    except FileNotFoundError:
        return EMPTY
    except json.JSONDecodeError as exc:
        raise OperationError(f"{path} is not valid JSON: {exc}") from None
    return parse(document, path)


TEMPLATE = {
    "default_site": "highsec",
    "sites": {
        "highsec": {"owner": "corp", "facility": "highsec", "readers": ["Alice Builder", "Bob Inventor"]},
        "null": {"owner": "personal", "facility": "example-raitaru",
                 "characters": ["Carol Hauler", "Dave Miner", "Erin Director"],
                 "hub": "x-hub", "freight_in": "jita->x-hub", "freight_out": "x-hub->jita"},
    },
    "facilities": {
        "highsec": {"location_id": 60003760, "system": "Jita"},
        "example-raitaru": {"system": "X-EXMPL", "structure": "raitaru", "security": "null",
                         "rigs": {"manufacturing": {"me": "t1", "te": "t1"}}},
    },
    "hubs": {"x-hub": {"location_id": 1040000000001, "label": "X-EXMPL Example Market Hub",
                        "region_id": 10000060, "system_id": 30004759}},
    "freight": {"jita->x-hub": {"per_m3": 500, "collateral_pct": 0, "min": 5000000},
                "x-hub->jita": {"per_m3": 400, "collateral_pct": 1, "min": 10000000}},
}
