"""pi: planetary industry - recipe trees, colony budgets, and what one planet type can make alone.

Planetary industry was the one production line in this tool with no data behind it: a colony's
recipes, what its buildings draw from the CPU/powergrid budget, what a command centre upgrade actually
buys, and the customs multiplier on every commodity all had to be copied out of a wiki by hand. They
are in the local SDE snapshot now (`alphadata.planet_industry()`, refreshed by `update-data`), so the
three questions that planning session actually needed can be answered from it:

  ``chain <PRODUCT>``      the recipe tree down to raw materials - priced, with value added per
                           facility-hour, and at ``--customs-rate`` the customs bill as its own column
  ``fit``                  a colony layout against its command centre budget, including how many
                           extractor heads still fit and which of CPU/powergrid runs out first
  ``planet-type <TYPE>``   one planet type's five raw materials and everything it can refine with no
                           imports, which is the question that decides where a colony goes

No login and no authenticated scope: only ``chain`` touches the network, and only for public order
books, because prices are the one thing the SDE does not carry. ``fit`` and ``planet-type`` read the
local document and send no request at all - which is why they take their names from it instead of
`/universe/names`.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from dataclasses import dataclass

from . import alphadata, esi as esi_mod, industry, market, render, sso


# An extractor control unit carries at most ten extractor heads. This is a documented game rule, not a
# dogma value: SDE build 3503375's `dogmaAttributes.jsonl` was read in full (2867 attributes) and the
# only head/extractor attributes it defines are 1644 extractorDepletionRange, 1645
# extractorDepletionRate, 1690 ecuExtractorHeadCPU and 1691 ecuExtractorHeadPower - no maximum
# anywhere. Live ESI agrees: type 2848 (the Barren ECU) publishes 1690=110 and 1691=550 and nothing
# that could be a head count, and ESI's `/universe/dogma/*` endpoints were removed (404 measured
# 2026-09-13). The cap itself is EVE University's "Planetary buildings" page (read 2026-09-13): "every
# head (up to a maximum of 10) needs an amount of Powergrid (550) and CPU".
MAX_HEADS_PER_ECU = 10

# The command centre's upgrade levels, as the game numbers them: level 0 is the bare structure.
CCU_LEVELS = (0, 1, 2, 3, 4, 5)

# Facility class -> the SDE structure role that runs that tier of schematic, and the label to print.
FACILITY_LABELS = {"basic": "basic", "advanced": "advanced", "high_tech": "high-tech"}

# What `fit` can be asked to place, in the order its table prints them: the argument's attribute name,
# the SDE structure role whose figures it draws, and the label a player would use.
FIT_ITEMS = (("launchpad", "launchpad", "launchpad"),
             ("ecu", "extractor_control_unit", "extractor control unit"),
             ("basic", "processor_basic", "basic industry facility"),
             ("advanced", "processor_advanced", "advanced industry facility"),
             ("high_tech", "processor_high_tech", "high-tech industry facility"),
             ("storage", "storage_facility", "storage facility"))

# Which of the three limits capped the head count, phrased for the sentence that reports it.
BINDING_LABELS = {"cpu": "CPU", "powergrid": "powergrid",
                  "per_ecu_cap": f"the {MAX_HEADS_PER_ECU}-heads-per-ECU cap"}

CHAIN_COLUMNS = ["commodity", "tier", "qty/unit", "facility", "cycle", "planets"]
CHAIN_CSV_COLUMNS = ["depth", "type_id", "name", "tier", "qty_per_unit", "schematic_id", "facility",
                     "cycle_seconds", "planet_type_ids", "price_per_unit", "price_basis",
                     "value_added_per_facility_hour", "customs_rate_pct", "customs_per_facility_hour"]
PLANET_CSV_COLUMNS = ["planet_type_id", "planet_type_name", "tier", "type_id", "name", "facility",
                      "cycle_seconds", "schematic_id", "inputs"]


# ---------------------------------------------------------------------------
# the local document
# ---------------------------------------------------------------------------

def _document() -> dict:
    """The planetary-industry snapshot, or an error naming the one command that fixes it.

    Two failures are worth telling apart: nothing installed yet (the message has to say what to run)
    and something installed that is not in this shape (where `alphadata` already names `update-data`,
    and re-wording its sentence would only blur which of the two went wrong). Both come back as
    `RuntimeError` because `cli.main()` formats that into `error: ...` and exit 1; a raw `ValueError`
    would reach the user as a traceback."""
    try:
        return alphadata.planet_industry()
    except FileNotFoundError:
        raise RuntimeError("no local planetary industry data - run: eve-skills update-data") from None
    except ValueError as err:      # alphadata's own shape errors already name the fix
        raise RuntimeError(str(err)) from None


def _staleness(document: dict) -> str | None:
    """A warning line when the snapshot is older than every other SDE document here tolerates."""
    age = alphadata.stamp_age_days(document.get("fetched"))
    if age is None or age <= alphadata.STALE_DAYS:
        return None
    return (f"local planetary industry data is {age:.0f} days old "
            f"(SDE build {document.get('build')}) - run: eve-skills update-data")


def _name(document: dict, type_id: int) -> str:
    """Commodity name out of the SDE itself. All 83 PI commodities are in the document, so a PI report
    never needs `/universe/names` to be readable."""
    row = document["commodities"].get(str(type_id))
    return row["name"] if isinstance(row, dict) and row.get("name") else f"type {type_id}"


def _tier(document: dict, type_id: int) -> int | None:
    row = document["commodities"].get(str(type_id))
    tier = row.get("tier") if isinstance(row, dict) else None
    return int(tier) if isinstance(tier, (int, float)) else None


def _tax(document: dict, type_id: int) -> float | None:
    """One commodity's per-unit customs value, or None when the SDE gives none.

    Never 0.0 for an absent multiplier: "pays no customs" and "this snapshot has no figure" are
    different statements, and the first would quietly inflate a margin."""
    row = document["commodities"].get(str(type_id))
    tax = row.get("tax") if isinstance(row, dict) else None
    return float(tax) if isinstance(tax, (int, float)) else None


def _number(value, what: str) -> float:
    """A required numeric field of a document row, naming the offending thing when it is absent."""
    if not isinstance(value, (int, float)):
        raise RuntimeError(f"the local planetary industry data has no usable number for {what} "
                           f"- run: eve-skills update-data")
    return float(value)


def _schematic_outputs(document: dict) -> dict[int, tuple[int, dict]]:
    """{output type id: (schematic id, row)} for every recipe in the snapshot.

    A PI schematic has exactly one output - that is what makes "which step makes this commodity" a
    lookup rather than a choice. Measured on build 3494416 all 68 schematics have one output and no
    type is produced twice; a document that stopped being true would otherwise make the tree pick a
    branch silently."""
    by_output: dict[int, tuple[int, dict]] = {}
    for key, row in document["schematics"].items():
        out = row.get("out") or {}
        if len(out) != 1:
            raise RuntimeError(f"planetary schematic {key} has {len(out)} outputs; a recipe tree needs "
                               f"one product per step - run: eve-skills update-data")
        by_output[int(next(iter(out)))] = (int(key), row)
    return by_output


def _inputs(row: dict) -> list[tuple[int, float]]:
    return [(int(type_id), float(qty)) for type_id, qty in row["in"].items()]


def _out_qty(row: dict) -> float:
    return float(next(iter(row["out"].values())))


def _cycle_hours(document: dict, key: int) -> float:
    """One cycle of a schematic in hours; a zero or missing cycle time cannot divide anything."""
    row = document["schematics"][str(key)]
    seconds = _number(row.get("cycle"), f"schematic {key} cycle time")
    if seconds <= 0:
        raise RuntimeError(f"planetary schematic {key} claims a cycle time of {seconds:g}s "
                           f"- run: eve-skills update-data")
    return seconds / 3600.0


def _facility(row: dict) -> str | None:
    facility = row.get("facility")
    return FACILITY_LABELS.get(str(facility), str(facility)) if facility else None


# ---------------------------------------------------------------------------
# formatting helpers shared by the three actions
# ---------------------------------------------------------------------------

def _qty(value: float | None) -> str:
    """A quantity with only as many decimals as it needs.

    PI quantities are whole units per cycle and fractions of them per plan, so `24,000` and `0.50` both
    have to read as exact rather than one of them carrying noise."""
    if value is None:
        return "-"
    number = float(value)
    return f"{int(number):,}" if number.is_integer() else f"{number:,.2f}"


def _plural(count: int, singular: str) -> str:
    """`1 head` / `12 heads`, so a refusal reads as a sentence instead of a count plus a stem."""
    return f"{count:,} {singular if count == 1 else singular + 's'}"


def _binding_label(binding: str) -> str:
    """The prose version of a `limits` key, so `--json` can carry the key and the table can say
    `powergrid` or `the 10-heads-per-ECU cap` - or both, when two limits tie for first place."""
    return " and ".join(BINDING_LABELS[name] for name in binding.split("+"))


def _cell(value):
    """CSV cell for a quantity that may be whole units: 24000 rather than 24000.0."""
    if value is None:
        return ""
    number = float(value)
    return int(number) if number.is_integer() else number


def _notes(lines: list[str]) -> str:
    """Footnotes under a table, indented the way `build-cost` indents its own."""
    return "\n".join(f"  {line}" for line in lines)


# ---------------------------------------------------------------------------
# pi chain
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainStep:
    """One node of a recipe tree, at the quantity this run's product needs.

    `qty` is per one unit of the requested product, so a tier-4 item wants 24,000 units of a raw
    material and the row says so; `depth` is what the text output indents by. `schematic_id` is None for
    a leaf - raw materials are extracted, not refined - and `planets` is filled only for those leaves."""

    type_id: int
    name: str
    tier: int | None
    qty: float
    depth: int
    schematic_id: int | None = None
    facility: str | None = None
    cycle: int | None = None
    planets: tuple[tuple[int, str], ...] = ()
    price: float | None = None
    basis: str | None = None
    value_per_hour: float | None = None
    customs_per_hour: float | None = None
    unpriced: tuple[str, ...] = ()
    untaxed: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChainRun:
    """Everything one `pi chain` reported, so text, JSON and CSV cannot disagree."""

    now: float
    sde_build: object
    product_id: int
    product_name: str
    steps: tuple[ChainStep, ...]
    scope: market.Scope | None = None
    figures: market.BookFigures | None = None
    customs: dict | None = None
    notes: tuple[str, ...] = ()


def _planets_yielding(document: dict, type_id: int) -> tuple[tuple[int, str], ...]:
    """Planet types whose extractors produce a raw material, as (id, name) in id order."""
    return tuple((int(pid), document["planet_types"][pid])
                 for pid, raws in sorted(document["resources"].items(), key=lambda pair: int(pair[0]))
                 if type_id in [int(raw) for raw in raws])


def _chain_steps(document: dict, root_id: int) -> list[ChainStep]:
    """The tree of steps that supply one unit of `root_id`, parent before children.

    Quantities scale through the tree by each schematic's own per-cycle ratio, so a row is what this
    product needs rather than what one facility cycle makes - 24,000 units of Carbon Compounds for one
    Broadcast Node. A document whose recipes loop is refused with the path that loops: without that
    guard a cycle would recurse until the interpreter gave up."""
    by_output = _schematic_outputs(document)
    steps: list[ChainStep] = []

    def visit(type_id: int, qty: float, depth: int, trail: tuple[int, ...]) -> None:
        if type_id in trail:
            path = " > ".join(_name(document, ident) for ident in trail + (type_id,))
            raise RuntimeError(f"the local planetary industry data describes a recipe cycle: {path} "
                               f"- run: eve-skills update-data")
        step = by_output.get(type_id)
        if step is None:
            steps.append(ChainStep(type_id=type_id, name=_name(document, type_id),
                                   tier=_tier(document, type_id), qty=qty, depth=depth,
                                   planets=_planets_yielding(document, type_id)))
            return
        key, row = step
        out_qty = _out_qty(row)
        if out_qty <= 0:
            raise RuntimeError(f"planetary schematic {key} yields {out_qty:g} per cycle, which cannot "
                               f"scale a recipe - run: eve-skills update-data")
        steps.append(ChainStep(type_id=type_id, name=_name(document, type_id),
                               tier=_tier(document, type_id), qty=qty, depth=depth, schematic_id=key,
                               facility=_facility(row), cycle=int(_number(row.get("cycle"),
                                                                          f"schematic {key} cycle time"))))
        scale = qty / out_qty
        for in_id, in_qty in sorted(_inputs(row), key=lambda pair: _name(document, pair[0])):
            visit(in_id, in_qty * scale, depth + 1, trail + (type_id,))

    visit(root_id, 1.0, 0, ())
    return steps


def _price_steps(document: dict, steps: list[ChainStep], prices: industry.Prices,
                 customs: dict | None) -> list[ChainStep]:
    """Attach a price, a value added per facility-hour and a customs bill to every step.

    Value added is `(output x price - inputs x prices) / cycle hours` for one cycle of that facility.
    The ratio is scale-invariant - doubling the quantities doubles both the margin and the hours - so
    one figure describes a single cycle and this run's throughput alike, which is why it prints once per
    step rather than once per quantity. A step with any unpriced participant gets None instead of a
    total missing an input: an input nobody quoted is not an input worth nothing."""
    factors = (customs or {}).get("factors") or {}
    priced: list[ChainStep] = []
    for step in steps:
        price, basis = prices.quote(step.type_id)
        value = None
        per_hour = None
        unpriced: tuple[str, ...] = ()
        untaxed: tuple[str, ...] = ()
        if step.schematic_id is not None:
            row = document["schematics"][str(step.schematic_id)]
            hours = _cycle_hours(document, step.schematic_id)
            out_qty = _out_qty(row)
            missing = [] if price is not None else [_name(document, step.type_id)]
            tax_missing = [] if _tax(document, step.type_id) is not None \
                else [_name(document, step.type_id)]
            cost = 0.0
            tax_in = 0.0
            for in_id, in_qty in _inputs(row):
                in_price, _ = prices.quote(in_id)
                if in_price is None:
                    missing.append(_name(document, in_id))
                else:
                    cost += in_qty * in_price
                in_tax = _tax(document, in_id)
                if in_tax is None:
                    tax_missing.append(_name(document, in_id))
                else:
                    tax_in += in_qty * in_tax
            if not missing:
                value = (out_qty * price - cost) / hours
            if customs is not None and not tax_missing:
                bill = (customs["rate_pct"] / 100.0) * (tax_in * factors["import"]
                                                        + out_qty * _tax(document, step.type_id)
                                                        * factors["export"])
                per_hour = bill / hours
            unpriced, untaxed = tuple(missing), tuple(tax_missing)
        priced.append(ChainStep(type_id=step.type_id, name=step.name, tier=step.tier, qty=step.qty,
                                depth=step.depth, schematic_id=step.schematic_id,
                                facility=step.facility, cycle=step.cycle, planets=step.planets,
                                price=price, basis=basis, value_per_hour=value,
                                customs_per_hour=per_hour, unpriced=unpriced, untaxed=untaxed))
    return priced


def _customs_options(document: dict, rate_pct: float) -> dict:
    """The customs maths this run bills with, from the snapshot's own multipliers.

    The factors are read, never hard-coded: `tax_factors` is where the document keeps the 0.5 import /
    1.0 export asymmetry, so a change upstream shows up here instead of in a constant. The *rate* has to
    come from the player - ESI only publishes a customs office's rate to the corporation that owns it -
    which is also why a rate outside 0..100 is refused rather than clamped."""
    if not 0 <= rate_pct <= 100:
        raise RuntimeError(f"--customs-rate is a percentage of the customs bill a corporation sets at "
                           f"its own office, so it has to be 0..100 (got {rate_pct:g})")
    factors = document.get("tax_factors") or {}
    parsed = {}
    for side in ("import", "export"):
        value = factors.get(side)
        if not isinstance(value, (int, float)):
            raise RuntimeError(f"the local planetary industry data has no {side} customs factor "
                               f"- run: eve-skills update-data")
        parsed[side] = float(value)
    return {"rate_pct": float(rate_pct), "factors": parsed}


def chain_text(run: ChainRun) -> str:
    headers = list(CHAIN_COLUMNS)
    if run.scope is not None:
        headers += ["price/u", "va/fac-hr"]
        if run.customs is not None:
            headers.append("customs/fac-hr")
    rows = []
    for step in run.steps:
        cells = ["  " * step.depth + step.name, "-" if step.tier is None else str(step.tier),
                 _qty(step.qty), step.facility or "-",
                 "-" if step.cycle is None else render.format_duration(step.cycle),
                 ", ".join(label for _ident, label in step.planets)]
        if run.scope is not None:
            cells += [render.isk(step.price), render.isk(step.value_per_hour)]
            if run.customs is not None:
                cells.append(render.isk(step.customs_per_hour))
        rows.append(cells)
    lines = [f"{run.product_name} (id {run.product_id}) - planetary recipe tree per 1 unit of product, "
             f"SDE build {run.sde_build}", render.table(headers, rows)]
    if run.notes:
        lines.append(_notes(list(run.notes)))
    return "\n".join(lines)


def chain_json(run: ChainRun) -> dict:
    steps = [{"type_id": step.type_id, "name": step.name, "tier": step.tier, "depth": step.depth,
              "qty_per_unit_of_product": _cell(step.qty), "schematic_id": step.schematic_id,
              "facility": step.facility, "cycle_seconds": step.cycle,
              "planets": [{"id": ident, "name": label} for ident, label in step.planets],
              "price_per_unit": step.price, "price_basis": step.basis,
              "value_added_per_facility_hour": step.value_per_hour,
              "customs_per_facility_hour": step.customs_per_hour,
              "unpriced_participants": list(step.unpriced),
              "untaxed_participants": list(step.untaxed)}
             for step in run.steps]
    pricing = None
    if run.scope is not None and run.figures is not None:
        pricing = {"scope": run.scope.label, "region_id": run.scope.region_id,
                   "location_id": run.scope.location_id,
                   "basis": "min_sell (cheapest standing ask), falling back to ESI's published "
                            "adjusted price where the scope has no order",
                   "last_modified": market.iso_utc(run.figures.meta.last_modified),
                   "age_seconds": None if run.figures.meta.last_modified is None
                                   else round(run.now - run.figures.meta.last_modified, 1),
                   "books_fetched": run.figures.fetched, "books_cached": run.figures.cached,
                   "books_failed": run.figures.failed}
    return {"generated": market.iso_utc(run.now), "sde_build": run.sde_build,
            "product": {"type_id": run.product_id, "name": run.product_name},
            "unit": 1, "steps": steps, "pricing": pricing,
            "customs": None if run.customs is None else
                       {"rate_pct": run.customs["rate_pct"],
                        "import_factor": run.customs["factors"]["import"],
                        "export_factor": run.customs["factors"]["export"]},
            "warnings": list(run.notes)}


def chain_csv(run: ChainRun) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CHAIN_CSV_COLUMNS)
    for step in run.steps:
        writer.writerow([step.depth, step.type_id, step.name, step.tier, _cell(step.qty),
                         step.schematic_id, step.facility, step.cycle,
                         ";".join(str(ident) for ident, _label in step.planets),
                         _cell(step.price), step.basis, _cell(step.value_per_hour),
                         None if run.customs is None else run.customs["rate_pct"],
                         _cell(step.customs_per_hour)])
    sys.stdout.write(buffer.getvalue())


# ---------------------------------------------------------------------------
# pi fit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FitItem:
    """One class of building in the proposed layout, and what it draws."""

    role: str
    label: str
    cpu_each: float
    power_each: float
    count: int

    @property
    def cpu_total(self) -> float:
        return self.cpu_each * self.count

    @property
    def power_total(self) -> float:
        return self.power_each * self.count


@dataclass(frozen=True)
class FitRun:
    """One `pi fit` verdict: the load, the budget, and how many heads are left over."""

    now: float
    sde_build: object
    level: int
    budget_cpu: float
    budget_power: float
    planet_count: int
    items: tuple[FitItem, ...]
    head_cpu: float
    head_power: float
    ecu_count: int
    heads: int | None
    link_allowance: tuple[float, float] | None
    max_heads: int
    limits: dict[str, int]
    binding: str
    over_budget: bool
    fits: bool
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def load_cpu(self) -> float:
        return sum(item.cpu_total for item in self.items)

    @property
    def load_power(self) -> float:
        return sum(item.power_total for item in self.items)


def _role_figures(document: dict, role: str, label: str) -> tuple[float, float]:
    """The (cpu, powergrid) one structure role draws, refusing to average a disagreement.

    Measured on SDE build 3494416: every role costs the same on all eight planet types - extractor
    200/800, basic processor 200/800, advanced 500/700, high-tech 1100/400, storage 500/700, launchpad
    3600/700, ECU body 400/2600 with heads at 110/550 - which is what lets `fit` answer without being
    told a planet type. Should CCP ever price a role per planet, this names the split instead of picking
    one silently."""
    figures: dict[tuple[float, float], list[str]] = {}
    for key, row in document["structures"].items():
        if row.get("role") != role:
            continue
        pair = (_number(row.get("cpu"), f"structure {key} CPU"),
                _number(row.get("power"), f"structure {key} powergrid"))
        figures.setdefault(pair, []).append(str(row.get("name") or key))
    if not figures:
        raise RuntimeError(f"the local planetary industry data has no structure with role '{role}' "
                           f"- run: eve-skills update-data")
    if len(figures) > 1:
        detail = "; ".join(f"{cpu:g}/{power:g} for {', '.join(sorted(names))}"
                           for (cpu, power), names in sorted(figures.items()))
        raise RuntimeError(f"the local planetary industry data gives '{label}' different fitting costs "
                           f"({detail}) and this command has no planet type to choose between them "
                           f"- run: eve-skills update-data, and if it persists the command needs a "
                           f"--planet option")
    return next(iter(figures))


def _head_figures(document: dict) -> tuple[float, float]:
    """An ECU's per-head CPU/powergrid draw - dogma attributes 1690 and 1691 in the snapshot."""
    for key, row in document["structures"].items():
        if row.get("role") != "extractor_control_unit":
            continue
        return (_number(row.get("head_cpu"), f"extractor control unit {key} head CPU"),
                _number(row.get("head_power"), f"extractor control unit {key} head powergrid"))
    raise RuntimeError("the local planetary industry data has no extractor control unit "
                       "- run: eve-skills update-data")


def _budget(document: dict, level: int) -> tuple[float, float, int]:
    """(cpu, powergrid, how many planet types agree) that a command centre at this level outputs.

    The document keys the budget by planet type; measured on build 3494416 all eight agree at every
    level (0: 1675/6000, 1: 7057/9000, 2: 12136/12000, 3: 17215/15000, 4: 21315/17000,
    5: 25415/19000), so the level alone identifies it. A divergence is reported rather than averaged,
    for the reason `_role_figures` gives."""
    seen: dict[tuple[float, float], list[str]] = {}
    for pid, levels in document["command_centers"].items():
        row = levels.get(str(level)) if isinstance(levels, dict) else None
        if not isinstance(row, dict):
            continue
        name = document["planet_types"].get(str(pid), f"planet type {pid}")
        pair = (_number(row.get("cpu"), f"command centre level {level} CPU on {name}"),
                _number(row.get("power"), f"command centre level {level} powergrid on {name}"))
        seen.setdefault(pair, []).append(name)
    if not seen:
        raise RuntimeError(f"the local planetary industry data has no command centre output for upgrade "
                           f"level {level} - run: eve-skills update-data")
    if len(seen) > 1:
        detail = "; ".join(f"{cpu:g}/{power:g} on {', '.join(sorted(names))}"
                           for (cpu, power), names in sorted(seen.items()))
        raise RuntimeError(f"the local planetary industry data gives level {level} command centres "
                           f"different output ({detail}); this fit cannot pick one "
                           f"- run: eve-skills update-data")
    pair = next(iter(seen))
    return pair[0], pair[1], len(seen[pair])


def _link_allowance(spec: str) -> tuple[float, float]:
    """`--link-allowance CPU,PG` as two numbers, with the expected shape in the refusal.

    These are the player's own figures: what keeping inter-planetary links up costs depends on which
    links a colony has, and ESI publishes nothing about a planet's link setup without a login this
    command does not ask for."""
    parts = [bit.strip() for bit in str(spec).split(",")]
    if len(parts) != 2 or not all(parts):
        raise RuntimeError(f"--link-allowance wants CPU,POWERGRID as two numbers "
                           f"- e.g. --link-allowance 500,400 (got '{spec}')")
    try:
        cpu, power = (float(bit) for bit in parts)
    except ValueError:
        raise RuntimeError(f"--link-allowance wants two numbers - e.g. --link-allowance 500,400 "
                           f"(got '{spec}')") from None
    if cpu < 0 or power < 0:
        raise RuntimeError(f"--link-allowance cannot be negative (got '{spec}')")
    return cpu, power


def compute_fit(document: dict, level: int, counts: dict[str, int], heads: int | None,
                link_allowance: tuple[float, float] | None,
                extra_notes: tuple[str, ...] = ()) -> FitRun:
    """The layout's load against the command centre's budget, and the head verdict.

    Being over budget is an answer, not an error: `fits=False` with the reason printed is exactly what
    a player asking "can I add a second launchpad?" needs to see, and exiting 1 for it would make the
    command useless in a script that compares layouts. Nonsense *input* - negative counts - is refused,
    because that is not a layout anybody could fit."""
    if heads is not None and heads < 0:
        raise RuntimeError(f"--heads cannot be negative (got {heads})")
    budget_cpu, budget_power, planets = _budget(document, level)
    head_cpu, head_power = _head_figures(document)
    if head_cpu <= 0 or head_power <= 0:
        raise RuntimeError("the local planetary industry data gives an extractor head a CPU or "
                           f"powergrid draw of {head_cpu:g}/{head_power:g} - run: eve-skills update-data")

    items: list[FitItem] = []
    for attribute, role, label in FIT_ITEMS:
        count = int(counts.get(attribute, 0) or 0)
        if count < 0:
            raise RuntimeError(f"--{attribute.replace('_', '-')} cannot be negative (got {count})")
        if not count:
            continue
        cpu, power = _role_figures(document, role, label)
        items.append(FitItem(role=role, label=label, cpu_each=cpu, power_each=power, count=count))
    if link_allowance is not None:
        items.append(FitItem(role="links", label="planetary links", cpu_each=link_allowance[0],
                             power_each=link_allowance[1], count=1))

    ecu_count = int(counts.get("ecu", 0) or 0)
    # The head row only appears when a number of heads was asked for; with --heads omitted the run
    # reports what *would* fit, and charging for heads nobody chose would misstate the load.
    if heads:
        items.append(FitItem(role="extractor_head", label="extractor head", cpu_each=head_cpu,
                             power_each=head_power, count=heads))

    free_cpu = budget_cpu - sum(item.cpu_total for item in items)
    free_power = budget_power - sum(item.power_total for item in items)
    over_budget = free_cpu < 0 or free_power < 0

    # How many heads the free budget would carry if nothing else limited them. A negative budget is not
    # a negative number of heads: it means none fit, and the reason says which resource did it.
    limits = {"cpu": int(free_cpu // head_cpu) if free_cpu > 0 else 0,
              "powergrid": int(free_power // head_power) if free_power > 0 else 0,
              "per_ecu_cap": ecu_count * MAX_HEADS_PER_ECU}
    max_heads = min(limits.values())
    # Which limit capped it, spelled with the same key the `limits` dict above uses (several joined
    # when they tie), so `--json` reports something a spreadsheet can pivot on and only prose maps it.
    binding = "+".join(name for name, value in limits.items() if value == max_heads)

    reasons: list[str] = []
    if free_cpu < 0:
        reasons.append(f"CPU over budget by {render.isk(-free_cpu)}")
    if free_power < 0:
        reasons.append(f"powergrid over budget by {render.isk(-free_power)}")
    if heads is not None and ecu_count == 0:
        reasons.append(f"no extractor control unit is fitted, so {_plural(heads, 'head')} cannot attach")
    elif heads is not None and heads > limits["per_ecu_cap"]:
        reasons.append(f"more heads than the units can carry: {_plural(heads, 'head')} against "
                       f"{ecu_count} x {MAX_HEADS_PER_ECU} = {limits['per_ecu_cap']}")
    notes = list(extra_notes)
    if not items:
        notes.append("nothing fitted: name at least one of --launchpad, --ecu, --basic, --advanced, "
                     "--high-tech, --storage or --link-allowance to see a load against the budget")
    return FitRun(now=time.time(), sde_build=document.get("build"), level=level,
                  budget_cpu=budget_cpu, budget_power=budget_power, planet_count=planets,
                  items=tuple(items), head_cpu=head_cpu, head_power=head_power, ecu_count=ecu_count,
                  heads=heads, link_allowance=link_allowance, max_heads=max_heads, limits=limits,
                  binding=binding, over_budget=over_budget, fits=not reasons, reasons=tuple(reasons),
                  notes=tuple(notes))


def fit_text(run: FitRun) -> str:
    rows = [[item.label, _qty(item.cpu_each), _qty(item.power_each), str(item.count),
             _qty(item.cpu_total), _qty(item.power_total)] for item in run.items]
    rows.append(["total", "", "", str(sum(item.count for item in run.items)),
                 _qty(run.load_cpu), _qty(run.load_power)])
    rows.append([f"command centre level {run.level} budget", "", "", "",
                 _qty(run.budget_cpu), _qty(run.budget_power)])
    rows.append(["free", "", "", "", _qty(run.budget_cpu - run.load_cpu),
                 _qty(run.budget_power - run.load_power)])
    lines = [f"Planetary colony fit - command centre upgrade level {run.level}, "
             f"SDE build {run.sde_build}",
             render.table(["item", "cpu each", "pg each", "count", "cpu", "pg"], rows)]
    notes = [f"each extractor head costs {_qty(run.head_cpu)} CPU / {_qty(run.head_power)} PG "
             f"(the ECU's own head figures in the local SDE)",
             f"the free budget would carry {run.limits['cpu']} heads on CPU and "
             f"{run.limits['powergrid']} on powergrid; {run.ecu_count} extractor control "
             f"unit{'' if run.ecu_count == 1 else 's'} can attach at most {run.limits['per_ecu_cap']}"]
    if not run.fits:
        notes.append("fits: no - " + "; ".join(run.reasons))
    elif run.heads is not None:
        notes.append(f"{_plural(run.heads, 'head')} fitted at {_qty(run.head_cpu)} CPU / "
                     f"{_qty(run.head_power)} PG each")
        notes.append("fits: yes - the layout above stays inside the budget")
    elif run.ecu_count == 0:
        notes.append("fits: yes - but with no extractor control unit fitted, no head can attach")
    else:
        notes.append(f"fits: yes - room for {_qty(run.max_heads)} extractor heads; "
                     f"{_binding_label(run.binding)} binds first")
    notes.append(f"the budget and every structure cost above are the same for all {run.planet_count} "
                 f"planet types in this SDE build, so no planet type is needed to answer")
    if run.link_allowance is not None:
        notes.append(f"planetary links are charged the {_qty(run.link_allowance[0])} CPU / "
                     f"{_qty(run.link_allowance[1])} PG you asked for: what a colony's links cost "
                     f"depends on which links it has, and ESI publishes nothing about them here")
    notes.extend(run.notes)
    return "\n".join(lines + ["", _notes(notes)])


def fit_json(run: FitRun) -> dict:
    return {"generated": market.iso_utc(run.now), "sde_build": run.sde_build,
            "ccu_level": run.level,
            "budget": {"cpu": _cell(run.budget_cpu), "power": _cell(run.budget_power),
                       "planet_types_agreeing": run.planet_count},
            "items": [{"role": item.role, "label": item.label, "cpu_each": _cell(item.cpu_each),
                       "power_each": _cell(item.power_each), "count": item.count,
                       "cpu_total": _cell(item.cpu_total), "power_total": _cell(item.power_total)}
                      for item in run.items],
            "totals": {"cpu": _cell(run.load_cpu), "power": _cell(run.load_power)},
            "free": {"cpu": _cell(run.budget_cpu - run.load_cpu),
                     "power": _cell(run.budget_power - run.load_power)},
            "heads": {"requested": run.heads, "ecu_count": run.ecu_count,
                      "cost": {"cpu": _cell(run.head_cpu), "power": _cell(run.head_power)},
                      "max_that_fits": run.max_heads, "binding": run.binding, "limits": dict(run.limits),
                      "max_per_ecu": MAX_HEADS_PER_ECU},
            "link_allowance": None if run.link_allowance is None else
                              {"cpu": _cell(run.link_allowance[0]),
                               "power": _cell(run.link_allowance[1])},
            "over_budget": run.over_budget, "fits": run.fits, "reasons": list(run.reasons),
            "warnings": list(run.notes)}


# ---------------------------------------------------------------------------
# pi planet-type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanetProduct:
    """One commodity this planet type can reach on its own, with the recipe that makes it.

    Tier 1 is included here and filtered out of the text table rather than left out, so `--json` and
    `--csv` describe the whole reachable set from one source of truth."""

    type_id: int
    name: str
    tier: int | None
    facility: str | None
    cycle: int | None
    schematic_id: int | None
    inputs: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class PlanetRun:
    """What one planet type yields and refines with no imports."""

    now: float
    sde_build: object
    planet_type_id: int
    planet_type_name: str
    raw_materials: tuple[tuple[int, str, tuple[tuple[int, str], ...]], ...]
    products: tuple[PlanetProduct, ...]
    tier4_anywhere: bool
    notes: tuple[str, ...] = ()


def _resolve_planet(document: dict, spec) -> tuple[int, str]:
    """Planet type as (id, name) from its exact name or its id.

    Resolved inside the document rather than through `/universe/ids`: planet types are not item types,
    the eight names are all the SDE has, and keeping this action free of ESI is what lets it answer on a
    machine with no network."""
    text = str(spec).strip()
    choices = ", ".join(sorted(document["planet_types"].values()))
    if not text:
        raise RuntimeError(f"empty planet type specifier; choices: {choices}")
    if text.isdigit():
        ident = int(text)
        name = document["planet_types"].get(str(ident))
        if name is None:
            raise RuntimeError(f"no planet type with id {ident} in the local planetary industry data; "
                               f"choices: {choices}")
        return ident, str(name)
    for pid, name in document["planet_types"].items():
        if str(name).lower() == text.lower():
            return int(pid), str(name)
    raise RuntimeError(f"no planet type named '{text}' in the local planetary industry data; "
                       f"choices: {choices}")


def _runs_here(row: dict, planet_type_id: int) -> bool:
    """Whether a schematic's facility class exists on this planet - its own `planet_types` list."""
    return planet_type_id in [int(pid) for pid in row.get("planet_types") or []]


def _reachable(document: dict, planet_type_id: int) -> tuple[set[int], dict[int, tuple[int, dict]]]:
    """(what this planet can supply, {type id: (schematic id, row)}) by fixpoint.

    A schematic runs when the planet type appears in its own `planet_types` list - that is where the SDE
    says which facility classes the planet supports - and every input is already something the planet
    can supply, starting from what its extractors yield. Iterating to a fixpoint is the whole rule; no
    tier is special-cased, which is why "no tier-4 product on one planet" comes out as a result here
    rather than being filtered in. Schematics are visited in a fixed order so that, if the SDE ever gave
    one commodity two recipes, the attribution printed below would not move between runs."""
    reachable = {int(raw) for raw in document["resources"].get(str(planet_type_id), [])}
    made: dict[int, tuple[int, dict]] = {}
    ordered = sorted(((int(key), row) for key, row in document["schematics"].items()),
                     key=lambda pair: (pair[1].get("tier", 0), pair[0]))
    while True:
        added = False
        for key, row in ordered:
            if not _runs_here(row, planet_type_id):
                continue
            inputs = _inputs(row)
            if not inputs or not all(in_id in reachable for in_id, _qty_ in inputs):
                continue
            out_id = int(next(iter(row["out"])))
            if out_id not in reachable:
                reachable.add(out_id)
                made[out_id] = (key, row)
                added = True
        if not added:
            return reachable, made


def compute_planet(document: dict, planet_type_id: int, planet_type_name: str,
                   extra_notes: tuple[str, ...] = ()) -> PlanetRun:
    reachable, made = _reachable(document, planet_type_id)

    tier_one = [(int(key), row) for key, row in document["schematics"].items()
                if int(row.get("tier") or 0) == 1 and _runs_here(row, planet_type_id)]
    raws = []
    for raw in sorted((int(ident) for ident in document["resources"].get(str(planet_type_id), [])),
                      key=lambda ident: _name(document, ident)):
        refines = {(int(out_id), _name(document, int(out_id)))
                   for _key, row in tier_one
                   if raw in [in_id for in_id, _q in _inputs(row)] for out_id in row["out"]}
        raws.append((raw, _name(document, raw), tuple(sorted(refines, key=lambda pair: pair[1]))))

    products = []
    for type_id in sorted(reachable, key=lambda ident: (_tier(document, ident) or 0,
                                                        _name(document, ident))):
        tier = _tier(document, type_id)
        if not tier:      # tier 0 is what the extractors yield; it has no recipe to report
            continue
        key = made[type_id][0] if type_id in made else None
        row = document["schematics"].get(str(key)) if key is not None else None
        inputs = sorted((_name(document, in_id), qty) for in_id, qty in _inputs(row)) if row else []
        products.append(PlanetProduct(
            type_id=type_id, name=_name(document, type_id), tier=tier,
            facility=_facility(row) if row else None,
            cycle=int(_number(row.get("cycle"), f"schematic for {type_id} cycle time")) if row else None,
            schematic_id=key, inputs=tuple(inputs)))

    # "No planet reaches a tier-4 product alone" is checked over all eight rather than asserted: it is a
    # statement about this SDE build, and the day one becomes reachable the note has to stop printing.
    tier4_anywhere = any(_tier(document, ident) == 4
                         for pid in document["planet_types"]
                         for ident in _reachable(document, int(pid))[0])
    notes = list(extra_notes)
    if not any(product.tier == 4 for product in products):
        line = f"no tier-4 product is reachable on {planet_type_name} with no imports"
        if not tier4_anywhere:
            line += (f"; no planet type in SDE build {document.get('build')} reaches one alone, because "
                     f"every tier-4 recipe wants at least one input another planet's extractors yield")
        notes.append(line)
    return PlanetRun(now=time.time(), sde_build=document.get("build"),
                     planet_type_id=planet_type_id, planet_type_name=planet_type_name,
                     raw_materials=tuple(raws), products=tuple(products),
                     tier4_anywhere=tier4_anywhere, notes=tuple(notes))


def planet_text(run: PlanetRun) -> str:
    """Two tables: what the extractors yield and what each raw material refines into, then everything
    from tier 2 up that the planet can reach alone. Tier 1 is deliberately not repeated in the second
    table - it is already the right-hand column of the first - but `--json` and `--csv` carry it with
    its recipe, because a script reading this wants the whole reachable set in one place."""
    lines = [f"{run.planet_type_name} (planet type {run.planet_type_id}) - what it yields and refines "
             f"with no imports, SDE build {run.sde_build}",
             render.table(["raw material", "refines into (tier 1)"],
                          [[name, ", ".join(label for _ident, label in refines) or "-"]
                           for _id, name, refines in run.raw_materials]),
             "",
             render.table(["product", "tier", "facility", "cycle"],
                          [[product.name, "-" if product.tier is None else str(product.tier),
                            product.facility or "-",
                            "-" if product.cycle is None else render.format_duration(product.cycle)]
                           for product in run.products if (product.tier or 0) >= 2])]
    if run.notes:
        lines.append(_notes(list(run.notes)))
    return "\n".join(lines)


def planet_json(run: PlanetRun) -> dict:
    return {"generated": market.iso_utc(run.now), "sde_build": run.sde_build,
            "planet_type": {"id": run.planet_type_id, "name": run.planet_type_name},
            "raw_materials": [{"type_id": ident, "name": name,
                               "refines_into": [{"type_id": out_id, "name": label}
                                                for out_id, label in refines]}
                              for ident, name, refines in run.raw_materials],
            "products": [{"type_id": product.type_id, "name": product.name, "tier": product.tier,
                          "facility": product.facility, "cycle_seconds": product.cycle,
                          "schematic_id": product.schematic_id,
                          "inputs": [{"name": label, "qty": _cell(qty)}
                                     for label, qty in product.inputs]}
                         for product in run.products],
            "tier4_reachable_anywhere": run.tier4_anywhere,
            "warnings": list(run.notes)}


def planet_csv(run: PlanetRun) -> None:
    """One self-contained row per commodity this planet can supply: the extracted raw materials (tier 0,
    no schematic) and every tier-1-and-up product with its recipe. The text output splits these into two
    tables because that reads better; a script wants one row per thing either way."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PLANET_CSV_COLUMNS)
    for ident, name, _refines in run.raw_materials:
        writer.writerow([run.planet_type_id, run.planet_type_name, 0, ident, name, "extractor",
                         "", "", ""])
    for product in run.products:
        writer.writerow([run.planet_type_id, run.planet_type_name, product.tier, product.type_id,
                         product.name, product.facility, product.cycle, product.schematic_id,
                         "; ".join(f"{label} {_cell(qty)}" for label, qty in product.inputs)])
    sys.stdout.write(buffer.getvalue())


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

def _price_scope(client, args) -> market.Scope:
    """Where `chain` buys: a named hub's station (default Jita 4-4), or one whole region.

    Not `cmd_build_cost.build_scope`, which this otherwise mirrors: that one also resolves the system
    whose industry index bills an install, and planetary industry has no install fee here - customs is
    the only local charge, and its rate is a corporation's number rather than a system's. So a region
    scope needs no extra argument."""
    if args.hub and args.region:
        raise RuntimeError("--hub and --region each pick one scope; give only one of them - a recipe "
                           "tree is priced from a single order book")
    if args.region:
        region_id, name = market.resolve_region(client, args.region)
        return market.Scope(region_id, name)
    return market.hub_scope(args.hub or "jita")


def _requests_note(figures: market.BookFigures) -> str:
    """What the price fan-out cost, so a cheap run and an expensive one look different."""
    if not figures.fetched:
        return (f"no order-book request: all {figures.cached} types came from the local quote cache, "
                f"which ESI's own expiry says is still current")
    line = f"{figures.fetched} order-book request{'s' if figures.fetched != 1 else ''} now"
    if figures.cached:
        line += f", {figures.cached} type{'s' if figures.cached != 1 else ''} from the cache"
    return line


def _action_chain(args):
    """`pi chain`: the recipe tree for one product, priced at a market scope."""
    document = _document()
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    type_id, name = market.resolve_type(client, args.type)
    steps = _chain_steps(document, type_id)
    ids = sorted({step.type_id for step in steps})
    scope = _price_scope(client, args)
    customs = None if args.customs_rate is None else _customs_options(document, args.customs_rate)

    figures = market.book_figures(client, ids, scope)
    # `/markets/prices` is over a megabyte, and unlike `build-cost` this command has no bill that can
    # only be paid from it: the published price is a fallback for a type with no order in scope. So it
    # is fetched only when some participant actually lacks an ask, which for Jita's PI commodities
    # usually means the request never happens.
    adjusted: dict[int, float] = {}
    if any(figures.min_sell.get(ident) is None for ident in ids):
        reference = market.price_table(client)
        for ident in ids:
            row = reference.reference(ident)
            # A published 0.0 stays a price - PLEX really is quoted at zero - and only absence means
            # "no basis", which is what makes a missing type print a dash instead of looking free.
            if row is not None and row.adjusted_price is not None:
                adjusted[ident] = row.adjusted_price
    prices = industry.Prices(unit=figures.min_sell, adjusted=adjusted)

    steps = _price_steps(document, steps, prices, customs)
    notes = [f"prices: cheapest standing ask at {scope.label}, "
             f"{market.freshness_line(figures.meta, client.now().timestamp())}; {_requests_note(figures)}",
             "va/fac-hr is one cycle of that facility: (output x price - inputs x prices) / cycle hours. "
             "The ratio does not change with scale, so it is also the figure for the quantities above.",
             "tier 0 rows are extracted, not refined; `planets` lists every planet type whose extractors "
             "yield them, and a commodity two branches share appears once per branch at its own quantity"]
    if any(step.basis == "esi_adjusted" for step in steps):
        notes.append("a price tagged esi_adjusted is ESI's published industry reference, not an ask: "
                     "nothing was standing behind it in this scope")
    missing = sorted({label for step in steps for label in step.unpriced})
    if missing:
        notes.append(f"{', '.join(missing)}: no ask in this scope and no published price, so every "
                     f"margin that needs them prints a dash instead of counting them as free")
    if customs is not None:
        notes.append(
            f"customs/fac-hr bills the {customs['rate_pct']:g}% you gave against each commodity's "
            f"per-unit customs value in this SDE, at {customs['factors']['import']:g} for inputs and "
            f"{customs['factors']['export']:g} for the output; subtract it from va/fac-hr for the margin "
            f"after customs. The rate is yours because ESI only publishes a customs office's rate to the "
            f"corporation that owns it.")
        untaxed = sorted({label for step in steps for label in step.untaxed})
        if untaxed:
            notes.append(f"{', '.join(untaxed)}: this SDE gives no customs multiplier for them, so the "
                         f"customs column prints a dash rather than assuming zero")
    if figures.failed:
        notes.append(f"{figures.failed} order book{'s' if figures.failed != 1 else ''} did not answer; "
                     f"their types count as unpriced, not as worthless")
    stale = _staleness(document)
    if stale:
        notes.append(stale)

    run = ChainRun(now=client.now().timestamp(), sde_build=document.get("build"), product_id=type_id,
                   product_name=name, steps=tuple(steps), scope=scope, figures=figures,
                   customs=customs, notes=tuple(notes))
    if args.json:
        print(json.dumps(chain_json(run), indent=2))
    elif args.csv:
        chain_csv(run)
        for line in run.notes:      # the footnotes matter; they may not pollute a pipe
            print(line, file=sys.stderr)
    else:
        print(chain_text(run))
    return 0


def _action_fit(args):
    """`pi fit`: a colony layout against its command centre budget. No request is sent."""
    document = _document()
    if args.ccu not in CCU_LEVELS:
        raise RuntimeError(f"--ccu is a command centre's upgrade level, so it has to be one of "
                           f"{', '.join(str(level) for level in CCU_LEVELS)} (got {args.ccu})")
    counts = {attribute: getattr(args, attribute, 0) for attribute, _role, _label in FIT_ITEMS}
    link = None if args.link_allowance is None else _link_allowance(args.link_allowance)
    stale = _staleness(document)
    run = compute_fit(document, args.ccu, counts, args.heads, link,
                      extra_notes=(stale,) if stale else ())
    if args.json:
        print(json.dumps(fit_json(run), indent=2))
    else:
        print(fit_text(run))
    return 0


def _action_planet_type(args):
    """`pi planet-type`: one planet type's raw materials and everything it can refine alone."""
    document = _document()
    planet_id, planet_name = _resolve_planet(document, args.planet)
    stale = _staleness(document)
    run = compute_planet(document, planet_id, planet_name, extra_notes=(stale,) if stale else ())
    if args.json:
        print(json.dumps(planet_json(run), indent=2))
    elif args.csv:
        planet_csv(run)
        for line in run.notes:      # the footnotes matter; they may not pollute a pipe
            print(line, file=sys.stderr)
    else:
        print(planet_text(run))
    return 0


def cmd_pi(args):
    """planetary industry: recipe trees, colony budgets, per-planet reach (public ESI only, no login)."""
    action = getattr(args, "pi_action", None)
    if not action:
        raise RuntimeError("pi needs an action: `chain <PRODUCT>` for the recipe tree, `fit` for a "
                           "colony's CPU/powergrid budget, `planet-type <TYPE>` for what one planet "
                           "type can make with no imports")
    handler = {"chain": _action_chain, "fit": _action_fit, "planet-type": _action_planet_type}[action]
    return handler(args)
