"""system: what a solar system actually is - security, region, planets, and how far from the hub.

Picking a staging system for a spread of colonies needed three throwaway ESI scripts, because no
command here answered any of the three questions at once: what a system's security status really is
(not what it displays), what kinds of planet it holds, and how many jumps each candidate sits from
Jita. The decision then turned on a boundary case that only the true security figure exposes -
measured against live ESI on 2026-09-13, **Enderailen at 0.4487847685813904 is lowsec while Kulelen
at 0.4753689467906952 is highsec** - so the report prints the true value, the rounded value the game
shows, and the class both imply, in three columns nobody has to reconcile by hand.

Where each column comes from matters, because the two sources disagree in kind:

  security, region, jumps   live ESI (`/universe/systems`, `/universe/constellations`, `/route`).
                            Security status is read live even though the SDE has it too: measured on
                            build 3494416, `mapSolarSystems.jsonl`'s `securityStatus` for Enderailen
                            is 0.448785 against ESI's 0.4487847685813904 - the same call on a class
                            boundary, made against a snapshot CCP can move between builds. Anything
                            CCP can move is read from ESI. Region needs two calls because ESI's system
                            record names a constellation and nothing above it.
  planets, planet types     the local census (`alphadata.system_planets()`, built by `update-data`).
                            Counting `mapPlanets.jsonl`'s 68,407 rows costs 2.6 s and 50.9 MB of
                            reading, so it happens once per SDE build instead of once per report.

No login and no authenticated scope: every endpoint here is public.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
from dataclasses import dataclass

from . import alphadata, esi as esi_mod, market, render, sso


# ---------------------------------------------------------------------------
# security status: the rule EVE actually applies
# ---------------------------------------------------------------------------

# CCP's own statement of both rules, in the "System Security" guide of the EVE Developer
# Documentation (read 2026-09-13): rounding "follows normal rounding rules, with one exception: if
# the security status is in the range 0.0 < x < 0.05, it is rounded to 0.1, instead of 0.0", and
# High Security is "where the security status is x >= 0.45, or the rounded security status is
# x >= 0.5", Low Security "where 0.0 < x < 0.45, or the rounded security status is 0.1 <= x <= 0.4",
# Null Security "where x <= 0.0". https://developers.eveonline.com/docs/guides/system-security/
SECURITY_GUIDE = "https://developers.eveonline.com/docs/guides/system-security/"

HIGHSEC_MINIMUM = 0.45        # on the true value, per the guide above
ROUNDING_EXCEPTION = 0.05     # below this a positive system still displays 0.1, never 0.0

# How close to a class line a system has to sit before the report says something about it. Half of
# one displayed decimal is exactly the band where the true value and the displayed one can be read
# as different classes by a human doing the rounding in their head.
BOUNDARY_BAND = 0.05

ROUTE_FLAGS = ("shortest", "secure", "insecure")

# Which flag is worth fetching *besides* the one asked for, and therefore which two get compared:
# `shortest` is what ESI computes by default and `secure` is the path a hauler can actually fly, so
# the gap between them is the whole reason to name a staging system out here. `insecure` answers a
# different question (through wormholes and nullsec alike) and has no meaningful sibling.
COMPARE_FLAG = {"shortest": "secure", "secure": "shortest"}

CLASS_LABELS = {"high": "highsec", "low": "lowsec", "null": "nullsec"}

# ESI's `/route` is a legacy-only endpoint, so it is the one path here that cannot go versionless.
# Measured live on 2026-09-13: under this tool's pinned compatibility date (2026-08-18) the versionless
# `GET /route/{origin}/{destination}` answers `404 Page not found` while `/universe/systems` and
# `/universe/constellations` answer fine on that same header, and the only compatibility date ESI still
# offers that serves it is 2020-01-01 - which is what the response reports as its own
# `x-compatibility-date` however it is asked for. `/latest` is the documented alias for those legacy
# endpoints and serves them with or without the header, so a request that drops the pin behind a path
# prefix costs nothing: no second client, no second cache, and the route fan-out can stay in the same
# batch as the constellations.
ROUTE_PATH = "/latest/route/{origin}/{destination}"


def display_security(value: float) -> float:
    """The one-decimal security status the game shows for a true status.

    Written as CCP's own snippet writes it - `Math.round(x * 10).toDouble() / 10` - rather than with
    Python's ``round``, which rounds halves to even and so would print 0.45 as 0.4 where the client
    prints 0.5. ``math.floor(x * 10 + 0.5)`` is the away-from-zero half-up that snippet performs; it
    reproduces the guide's own example of -0.45 rounding to -0.4 (which needs a floor, not truncation).

    The exception is not cosmetic: a system at 0.04 displays 0.1 in-game, and one that really is 0.0
    displays 0.0 - the difference between "someone can gank you here" and "CONCORD will answer".
    """
    if value == 0.0:      # also catches -0.0, which must not print as "-0.0"
        return 0.0
    if 0.0 < value < ROUNDING_EXCEPTION:
        return 0.1
    return math.floor(value * 10.0 + 0.5) / 10.0


def security_class(value: float) -> str:
    """`high`, `low` or `null` for a true security status, by the guide's own predicates.

    Classifying on the true value and classifying on the displayed one are the same test - x >= 0.45
    is exactly the set that displays >= 0.5 - but the true form is what is written down here because
    it needs no rounding rule to re-derive, and cannot be broken by a change to the display.
    """
    if value >= HIGHSEC_MINIMUM:
        return "high"
    if value > 0.0:
        return "low"
    return "null"


# ---------------------------------------------------------------------------
# the local census
# ---------------------------------------------------------------------------

def _census() -> dict:
    """The per-system planet census, or an error naming the one command that builds it.

    Same two failures as `pi` tells apart: nothing installed, and something installed that is not in
    this shape. Both are `RuntimeError` because `cli.main()` renders that as `error: ...` with exit 1;
    a bare `FileNotFoundError` would reach the user as a traceback."""
    try:
        document = alphadata.system_planets()
    except FileNotFoundError:
        raise RuntimeError("no local planet census - run: eve-skills update-data") from None
    except ValueError as err:      # alphadata's own shape errors already name the fix
        raise RuntimeError(str(err)) from None
    return document


def _staleness(document: dict) -> str | None:
    """A warning line when the census is older than every other SDE document here tolerates."""
    age = alphadata.stamp_age_days(document.get("fetched"))
    if age is None or age <= alphadata.STALE_DAYS:
        return None
    return (f"local planet census is {age:.0f} days old "
            f"(SDE build {document.get('build')}) - run: eve-skills update-data")


@dataclass(frozen=True)
class PlanetCount:
    """One planet type's share of one system, named out of the census itself."""

    type_id: int
    name: str
    count: int


def _planets(document: dict, system_id: int) -> tuple[PlanetCount, ...]:
    """A system's planets as (type id, name, count), most numerous first.

    Ordered by count rather than by type id because the question this answers is what a system is
    *mostly made of*: "Gas x5, Storm x2, Ice x1" reads as a colony plan, "Ice x1, Gas x5, Storm x2"
    does not. An absent row means the SDE has no planet for this system at all - measured on build
    3494416 that is true of 402 of its 8,490 systems (Zarzakh and the AD/AurORA abyssal systems), so
    an empty tuple is an answer, not missing data.
    """
    row = document["systems"].get(str(system_id))
    if not isinstance(row, dict):
        return ()
    names = document["planet_types"]
    planets = [PlanetCount(int(type_id), str(names.get(str(type_id)) or f"planet type {type_id}"), int(count))
               for type_id, count in row.items()]
    return tuple(sorted(planets, key=lambda planet: (-planet.count, planet.name.lower(), planet.type_id)))


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteLegs:
    """One flag's answer to "how far is it", with the failure recorded when there is no answer."""

    flag: str
    jumps: int | None
    path: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class SystemRow:
    """One solar system as the report prints it."""

    system_id: int
    name: str
    security_status: float
    constellation: str
    region_id: int | None
    region: str
    planets: tuple[PlanetCount, ...]
    census_planets: int | None      # what the census counts; None when it has no row
    esi_planets: int | None         # what ESI's `planets` array holds; None when it gave none
    routes: tuple[RouteLegs, ...] = ()

    @property
    def displayed(self) -> float:
        return display_security(self.security_status)

    @property
    def klass(self) -> str:
        return security_class(self.security_status)

    @property
    def planets_shown(self) -> int | None:
        """The count to print, preferring the census over ESI when both exist.

        Not because ESI is less authoritative about what is in orbit right now - it is - but because
        this row's type breakdown comes from the census, and a `planets` column that did not sum to
        the breakdown beside it would read as arithmetic gone wrong rather than as two sources that
        disagree. Every disagreement gets its own note; `--csv` carries both raw figures."""
        if self.census_planets is not None:
            return self.census_planets
        return self.esi_planets

    def route(self, flag: str) -> RouteLegs | None:
        return next((legs for legs in self.routes if legs.flag == flag), None)


@dataclass(frozen=True)
class SystemReport:
    """Everything `system` prints: the rows, what was routed to, and the footnotes."""

    rows: tuple[SystemRow, ...]
    sde_build: object
    fetched: object
    route_to_id: int | None = None
    route_to: str | None = None
    flag: str = "shortest"
    compare_flag: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def routed(self) -> bool:
        return self.route_to is not None


def _planet_cell(planets: tuple[PlanetCount, ...]) -> str:
    """"Gas x5, Storm x2", always with the multiplier - a bare name would make "one planet" and
    "the only type here" indistinguishable in a column people scan for the mix."""
    if not planets:
        return "-"
    return ", ".join(f"{planet.name} x{planet.count}" for planet in planets)


def _jumps_cell(row: SystemRow, report: SystemReport) -> str:
    """The chosen flag's jump count, with the comparison figure when it differs.

    The reported flag's number leads; the sibling is tagged with its own name so "7 (secure 9)" reads
    as "the fast route is 7 jumps and the safe one is 9" rather than as two competing guesses."""
    chosen = row.route(report.flag)
    if chosen is None or chosen.jumps is None:
        return "-"
    cell = str(chosen.jumps)
    other = row.route(report.compare_flag) if report.compare_flag else None
    if other is not None and other.jumps is not None and other.jumps != chosen.jumps:
        cell += f" ({report.compare_flag} {other.jumps})"
    return cell


def system_text(report: SystemReport) -> str:
    headers = ["system", "security", "shown", "class"]
    if report.routed:
        headers.append(f"jumps to {report.route_to}")
    headers += ["region", "planets", "planet types"]

    rows = []
    for row in report.rows:
        cells = [row.name, f"{row.security_status:.4f}", f"{row.displayed:.1f}", CLASS_LABELS[row.klass]]
        if report.routed:
            cells.append(_jumps_cell(row, report))
        cells += [row.region or "-", "-" if row.planets_shown is None else str(row.planets_shown),
                  _planet_cell(row.planets)]
        rows.append(cells)

    notes = [f"  {line}" for line in report.notes]
    return "\n".join([render.table(headers, rows)] + ([""] + notes if notes else []))


def _boundary_note(rows: tuple[SystemRow, ...]) -> str | None:
    """Name the systems sitting within half a displayed decimal of a class line.

    This is the column people read wrong: 0.4488 looks like it belongs with 0.4754 until it is
    rounded, and then one is highsec and the other is not. Rather than lecture every report, the note
    fires only for a run that actually contains such a system - measured live on 2026-09-13, Enderailen
    (0.4488 -> 0.4, lowsec) and Kulelen (0.4754 -> 0.5, highsec) are 0.0266 apart."""
    near = [row for row in rows
            if min(abs(row.security_status - line) for line in (0.0, HIGHSEC_MINIMUM)) < BOUNDARY_BAND]
    if not near:
        return None
    listed = ", ".join(f"{row.name} {row.security_status:.4f} -> shows {row.displayed:.1f}" for row in near)
    verb = "sits" if len(near) == 1 else "sit"
    note = f"{listed} {verb} within {BOUNDARY_BAND:g} of a class line, and EVE only ever shows the rounded figure"
    if len({row.klass for row in near}) > 1:
        # Worth saying out loud when it happens in front of you: two candidates this close to each other
        # are not in the same security class, so they are not interchangeable.
        spread = max(row.security_status for row in near) - min(row.security_status for row in near)
        note += f"; these rows span {spread:.4f} of true security and are not all one class"
    return note


def system_json(report: SystemReport) -> dict:
    """The machine-readable report. Both raw planet figures survive here even though the table prints
    one of them, because a script comparing SDE builds needs to see which source said what."""
    systems = []
    for row in report.rows:
        systems.append({
            "system_id": row.system_id,
            "name": row.name,
            "security_status": row.security_status,
            "displayed_security": row.displayed,
            "class": row.klass,
            "constellation": row.constellation or None,
            "region_id": row.region_id,
            "region": row.region or None,
            "planets": {
                "total": row.planets_shown,
                "census": row.census_planets,
                "esi": row.esi_planets,
                "types": [{"type_id": planet.type_id, "name": planet.name, "count": planet.count}
                          for planet in row.planets],
            },
            "routes": [{"flag": legs.flag, "jumps": legs.jumps,
                        "path": list(legs.path) if legs.path else None, "error": legs.error}
                       for legs in row.routes],
        })
    return {
        "sde_build": report.sde_build,
        "census_fetched": report.fetched,
        "route_to": None if not report.routed else {"system_id": report.route_to_id, "name": report.route_to,
                                                    "flag": report.flag,
                                                    "compared_flag": report.compare_flag},
        "systems": systems,
        "notes": list(report.notes),
    }


SYSTEM_CSV_COLUMNS = ["system_id", "name", "security_status", "displayed_security", "class",
                      "constellation", "region_id", "region", "planets", "census_planets",
                      "esi_planets", "planet_types"]


def system_csv(report: SystemReport) -> str:
    """One row per system. The route columns only exist on a run that asked for a route, so a script
    reading `jumps` out of a plain `system A B --csv` gets a missing column rather than an empty one -
    the two are different mistakes and only one of them is the reader's."""
    columns = list(SYSTEM_CSV_COLUMNS)
    if report.routed:
        columns += ["route_to_id", "route_to", "route_flag", "jumps", "compared_flag", "compared_jumps"]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in report.rows:
        chosen = row.route(report.flag) if report.routed else None
        other = row.route(report.compare_flag) if report.routed and report.compare_flag else None
        cells = [row.system_id, row.name, f"{row.security_status:.4f}", f"{row.displayed:.1f}",
                 row.klass, row.constellation or "", render.csv_cell(row.region_id), row.region or "",
                 render.csv_cell(row.planets_shown), render.csv_cell(row.census_planets),
                 render.csv_cell(row.esi_planets),
                 "; ".join(f"{planet.name} x{planet.count}" for planet in row.planets)]
        if report.routed:
            cells += [report.route_to_id, report.route_to, report.flag,
                      render.csv_cell(chosen.jumps if chosen else None), report.compare_flag or "",
                      render.csv_cell(other.jumps if other else None)]
        writer.writerow(cells)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# gathering it: ESI for what moves, the census for what does not
# ---------------------------------------------------------------------------

def _route_target(client: esi_mod.Esi, spec: str) -> tuple[int, str]:
    """Where `--route` points, as (system id, name).

    A hub key is accepted because every jump count in EVE is quoted against one of the five trade
    hubs, and their system ids are already pinned in `market.HUBS`; the label printed is the system's
    own name rather than the hub's station label ("Jita", not "Jita 4-4"), because what was routed to
    is a solar system. Anything else goes through the same exact-name rule as every other specifier in
    this tool."""
    hub = market.HUBS.get(str(spec).strip().lower())
    if hub is None:
        return market.resolve_system(client, spec)
    name = esi_mod.resolve_names(client, {hub.system_id}).get(hub.system_id) or hub.label
    return hub.system_id, name


def collect(client: esi_mod.Esi, document: dict, specs: list[str], route_spec: str | None = None,
            flag: str = "shortest") -> SystemReport:
    """Everything the report needs, in three waves: the system records, then their constellations and
    routes together, then the region names.

    The second wave is one `get_many` because a constellation and a route depend on the system id and
    on nothing else - splitting them would serialise two independent fan-outs."""
    targets: list[tuple[int, str]] = []
    seen: set[int] = set()
    for spec in specs:
        system_id, name = market.resolve_system(client, spec)
        # A repeated system is dropped rather than printed twice: this table exists to compare
        # candidates, and a duplicate row is never the comparison somebody meant to ask for.
        if system_id not in seen:
            seen.add(system_id)
            targets.append((system_id, name))

    system_paths = {system_id: f"/universe/systems/{system_id}" for system_id, _name in targets}
    answers = client.get_many(list(system_paths.values()))
    records: dict[int, dict] = {}
    broken: list[str] = []
    for system_id, name in targets:
        payload = answers.get(system_paths[system_id])
        if isinstance(payload, dict):
            records[system_id] = payload
        else:
            # Security status has exactly one source, so a system that will not describe itself is
            # not a row with a dash in it but a report that cannot be made. Naming the id matters:
            # a numeric specifier resolves to `system <id>` and the user has to see which one.
            broken.append(f"{name}: {payload}")
    if broken:
        raise RuntimeError("ESI gave no solar system record for " + "; ".join(broken))

    hub_id = hub_name = None
    compare_flag = COMPARE_FLAG.get(flag)
    route_paths: dict[str, tuple[int, str]] = {}
    self_routes: dict[int, list[RouteLegs]] = {}
    if route_spec is not None:
        hub_id, hub_name = _route_target(client, route_spec)
        for system_id, _name in targets:
            asked = [flag] if compare_flag is None else [flag, compare_flag]
            if system_id == hub_id:
                # Routing a system to itself answers without ESI: one id in the path, zero jumps.
                self_routes[system_id] = [RouteLegs(one, 0, (system_id,)) for one in asked]
                continue
            for one in asked:
                route_paths[f"{ROUTE_PATH.format(origin=system_id, destination=hub_id)}?flag={one}"] = (system_id, one)

    constellation_ids = sorted({int(record["constellation_id"])
                                for record in records.values()
                                if isinstance(record.get("constellation_id"), int)})
    second = [f"/universe/constellations/{ident}" for ident in constellation_ids] + list(route_paths)
    answers = client.get_many(second)

    constellations: dict[int, tuple[str, int | None]] = {}
    for ident in constellation_ids:
        payload = answers.get(f"/universe/constellations/{ident}")
        if isinstance(payload, dict):
            region_id = payload.get("region_id")
            constellations[ident] = (str(payload.get("name") or f"constellation {ident}"),
                                     int(region_id) if isinstance(region_id, int) else None)

    region_ids = {region_id for _name, region_id in constellations.values() if region_id is not None}
    names = esi_mod.resolve_names(client, set(region_ids)) if region_ids else {}

    # Flag order within a system is the order the requests were queued in, which is what both the
    # comparison note and `--csv` read back.
    routes: dict[int, dict[str, RouteLegs]] = {}
    failed: list[str] = []
    for path, (system_id, asked) in route_paths.items():
        payload = answers.get(path)
        if isinstance(payload, list) and payload:
            # ESI's route is the list of systems walked, so the jumps are one fewer than its length.
            legs = RouteLegs(asked, len(payload) - 1, tuple(int(step) for step in payload))
        else:
            # A single system that would not route is a dash and a note, not a failed command: the
            # security and planets of the other candidates are still the answer somebody came for.
            legs = RouteLegs(asked, None,
                             error=str(payload if isinstance(payload, Exception) else "no route in the response"))
            failed.append(f"{dict(targets).get(system_id, system_id)} {asked}: {legs.error}")
        routes.setdefault(system_id, {})[asked] = legs
    for system_id, legs_list in self_routes.items():
        for legs in legs_list:
            routes.setdefault(system_id, {})[legs.flag] = legs

    rows: list[SystemRow] = []
    for system_id, resolved_name in targets:
        record = records[system_id]
        census_row = document["systems"].get(str(system_id))
        planets = _planets(document, system_id)
        constellation_id = record.get("constellation_id")
        constellation, region_id = (constellations[int(constellation_id)]
                                    if isinstance(constellation_id, int) and int(constellation_id) in constellations
                                    else ("", None))
        rows.append(SystemRow(
            system_id=system_id,
            name=str(record.get("name") or resolved_name),
            security_status=float(record.get("security_status") or 0.0),
            constellation=constellation,
            region_id=region_id,
            region=names.get(region_id, "") if region_id is not None else "",
            planets=planets,
            census_planets=sum(planet.count for planet in planets) if isinstance(census_row, dict) else None,
            esi_planets=len(record["planets"]) if isinstance(record.get("planets"), list) else None,
            routes=tuple(routes.get(system_id, {}).values()),
        ))

    return SystemReport(rows=tuple(rows), sde_build=document.get("build"), fetched=document.get("fetched"),
                        route_to_id=hub_id, route_to=hub_name, flag=flag, compare_flag=compare_flag,
                        notes=_notes(document, rows, hub_name, flag, compare_flag, failed))


def _notes(document: dict, rows: list[SystemRow], hub_name: str | None, flag: str,
           compare_flag: str | None, failed: list[str]) -> tuple[str, ...]:
    """The footnotes every output mode carries: what came from where, and every place the two sources
    or the two route flags disagree."""
    notes = [f"security, region and jumps are live ESI; planet counts come from the local SDE census "
             f"(build {document.get('build')})",
             f"class is EVE's own: security >= {HIGHSEC_MINIMUM} is highsec, above 0 is lowsec, "
             f"otherwise nullsec ({SECURITY_GUIDE})"]
    boundary = _boundary_note(rows)
    if boundary:
        notes.append(boundary)

    if hub_name is not None:
        line = f"jumps to {hub_name} are ESI's `/route` with flag={flag}"
        if compare_flag:
            line += (f"; flag={compare_flag} was fetched too and is shown beside it wherever the two "
                     f"differ in jump count")
        notes.append(line)
        for row in rows:
            chosen, other = row.route(flag), row.route(compare_flag) if compare_flag else None
            if chosen is None or other is None:
                continue
            if chosen.jumps is not None and other.jumps is not None and chosen.jumps != other.jumps:
                notes.append(f"{row.name}: the {compare_flag} route to {hub_name} is {other.jumps} jumps "
                             f"against {chosen.jumps} by {flag}")
            elif (chosen.path and other.path and chosen.path != other.path):
                notes.append(f"{row.name}: ESI's {compare_flag} route to {hub_name} is a different "
                             f"{other.jumps}-jump path than the {flag} one")
        if failed:
            # Named, with ESI's own reason, because "8 requests did not answer" leaves the reader to
            # work out which candidate lost its distance and whether to trust the rest of the table.
            notes.append("no route answered for " + "; ".join(failed)
                         + " - those cells print a dash rather than an assumed distance")

    mismatched = [row for row in rows if row.esi_planets is not None and row.census_planets is not None
                  and row.esi_planets != row.census_planets]
    for row in mismatched:
        notes.append(f"{row.name}: ESI counts {row.esi_planets} planets, the local census "
                     f"(SDE build {document.get('build')}) counts {row.census_planets}, so the type "
                     f"breakdown may be out of date - run: eve-skills update-data")
    uncensused = [row.name for row in rows if row.census_planets is None]
    if uncensused:
        notes.append(f"{', '.join(uncensused)} has no row in the local census; it either has no planets "
                     f"in SDE build {document.get('build')} or predates it")

    stale = _staleness(document)
    if stale:
        notes.append(stale)
    return tuple(notes)


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
def cmd_system(args):
    """system: security, region and planets for one or more solar systems, plus jumps to a hub.

    `--flag` is validated by the parser's `choices`, so nothing here re-checks it."""
    document = _census()       # refuse before spending a request on a report we cannot finish
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    report = collect(client, document, list(args.systems), args.route, args.flag)

    if args.json:
        print(json.dumps(system_json(report), indent=2))
    elif args.csv:
        sys.stdout.write(system_csv(report))
        for line in report.notes:     # the footnotes matter; they may not pollute a pipe
            print(line, file=sys.stderr)
    else:
        print(system_text(report))
    return 0
