"""What building an item costs in ISK, from live prices and the local SDE blueprint data.

Domain logic only: recipes come out of ``alphadata.blueprint_materials()``, prices come in as a
`Prices` pair of maps, and everything below is a deterministic reduction over both - with one
exception, `cost_indices`, which reads the single public ESI document that says how expensive each
system is to work in. Keeping the arithmetic out of the CLI means `build-cost` today, and anything
that later has to answer "build it or buy it", pays for these rules exactly once.

The formulas are CCP's, and every constant carries the date it was measured (see `rules_warning`):

    materials   required = max(runs, ceil(round(runs * base * (1 - ME/100) * rig, 2)))
    job fee     EIV * (system cost index + facility tax + SCC surcharge)
                where EIV = sum(base * runs * adjusted_price)     <- never reduced by ME
    job time    base_time * runs * (1 - TE/100)

``adjusted_price`` is the industry reference and the only price the job fee uses; it comes from
``/markets/prices`` and is a published figure rather than something anybody will fill (see
`market.Reference`). Material costs are a different question and are priced from real asks, with
that same published figure standing in when a type has no visible order book.

Deliberately not modelled, because each one needs data or decisions this module must not invent:
character industry skills and their time/material bonuses, structure and rig bonuses beyond the
single `material_multiplier` a caller supplies, ME/TE research itself (only its caps are known
here), the 25 % tax an alpha clone pays instead of running the job at all, and invention - a T2
blueprint is *invented*, so its cost is a random number of attempts whose decrypts and data cores
average out only over many tries. ``RULES_MEASURED`` dates what is modelled; `rules_warning` says
out loud when that date has gotten old.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import alphadata, esi as esi_mod

# The two activity names are decided by the SDE pipeline, so they are unpacked from it rather than
# written out again here: a third material-consuming activity in a future snapshot must break at
# import time instead of being silently treated as researchable (or as a recipe nobody can cost).
MANUFACTURING, REACTION = alphadata.INDUSTRY_ACTIVITIES

# Surcharge levied by a Structure Caretaker (SCC) contract, as a fraction of EIV. Measured
# 2026-09-09 against the in-game install screen; it is CCP policy, so no endpoint reports it and no
# amount of re-deriving from the SDE will produce it - which is why it is a dated constant and why
# `rules_warning` exists. A player-owned structure with its own billing replaces this number, not
# the facility tax, which is why both stay separate fields on `Facility`.
SCC_SURCHARGE = 0.04

# Facility tax charged when the job runs in an NPC station, as a fraction of EIV. Measured
# 2026-09-09. An alpha clone pays the same rate for a job it cannot start at all; that case is not
# modelled here (see the module docstring), so this is simply the tax an omega character pays in
# highsec NPC space, and a player structure caller sets it to whatever its own billing says.
NPC_STATION_TAX = 0.0025

# Research caps: a blueprint's ME stops at 10 and its TE at 20 (measured 2026-09-09 against the
# research screen). They are enforced here because a plan asked for ME 40 is not "very efficient",
# it is a typo, and reporting a cost computed from an impossible number would be a lie with decimals.
MAX_ME = 10
MAX_TE = 20

# The ME a component job runs at when the caller names none. Deliberately not the top job's default:
# the common real case is a T2 hull built from an invented copy of its own blueprint - unresearched,
# because you cannot research what you just made - fed by components that came from BPOs their owner
# has had for years and already took to the cap. Assuming the other way round, no research on the
# components, overstates a build by more than any other single assumption this command makes: it is
# applied to every material of every component job, which is where most of a build's ISK sits.
DEFAULT_COMPONENT_ME = MAX_ME

# When the constants above were last checked against the game, and how long that is believed.
RULES_MEASURED = "2026-09-09"
STALE_AFTER_DAYS = 180

# One public, unauthenticated document with a row per solar system and one index per activity:
# ~5485 systems, served in a single page with an hour-long `Expires`. The manufacturing and
# reaction activity names in it are identical to the keys of an SDE blueprint's activities, so the
# same activity string selects both the recipe and its cost index.
COST_INDEX_PATH = "/industry/systems"


def rules_warning(now: datetime | None = None) -> str | None:
    """Staleness notice when the dated CCP industry constants may no longer be accurate.

    A tax rate is not something this tool can discover: if CCP moves the SCC surcharge, every cost
    printed here stays plausible and becomes quietly wrong, so the date has to speak for itself
    rather than wait for somebody to notice a total that does not match their install window."""
    measured = datetime.strptime(RULES_MEASURED, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - measured).days
    if age > STALE_AFTER_DAYS:
        return (f"industry cost rules last measured {RULES_MEASURED} ({age} days ago) - "
                "check CCP before relying on them")
    return None


def _as_id(value) -> int | None:
    """Id as an int, or None for anything that is not one.

    The blueprint document keys everything by string and ESI has been known to serve a null where a
    number belongs; dropping the odd row beats losing every recipe in the same run."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_me(me: int) -> None:
    if not 0 <= me <= MAX_ME:
        raise RuntimeError(f"material efficiency must be 0..{MAX_ME} - a blueprint researches at "
                           f"most {MAX_ME} ME, got {me}")


def _require_te(te: int) -> None:
    if not 0 <= te <= MAX_TE:
        raise RuntimeError(f"time efficiency must be 0..{MAX_TE} - a blueprint researches at most "
                           f"{MAX_TE} TE, got {te}")


def _require_runs(runs: int) -> None:
    if runs < 1:
        raise RuntimeError(f"a job needs at least one run, got {runs}")


def _require_runs_within_limit(recipe: Recipe, runs: int) -> None:
    """Refuse an install the game would reject; a limit of 0 means the SDE states none."""
    if recipe.max_runs > 0 and runs > recipe.max_runs:
        raise RuntimeError(
            f"blueprint {recipe.blueprint_id} allows at most {recipe.max_runs} runs per install, "
            f"asked for {runs} - install it as {-(-runs // recipe.max_runs)} jobs instead")


def _require_multiplier(multiplier: float) -> None:
    if multiplier <= 0:
        raise RuntimeError(f"material multiplier must be a positive fraction (1.0 means no rig or "
                           f"structure bonus), got {multiplier}")


@dataclass(frozen=True)
class Recipe:
    """One blueprint's one activity, reduced to what a cost needs.

    Quantities are per run and unresearched: `materials` holds the SDE base quantities, so ME is a
    parameter of `required_quantity`, never something baked in here. That keeps one recipe usable at
    any research level instead of needing a copy per level."""

    blueprint_id: int
    activity: str                  # MANUFACTURING or REACTION
    product_id: int
    product_qty: int               # units out of one run
    time: int                      # seconds per run, unresearched
    max_runs: int                  # maxProductionLimit; 0 when the SDE states no limit
    materials: Mapping[int, int]   # material type id -> base quantity per run
    alternatives: tuple[int, ...]  # other blueprint ids making the same product, sorted, not this one

    @property
    def researchable(self) -> bool:
        """Whether ME and TE exist for this activity.

        Reactions cannot be researched - verified in the SDE, where a reaction blueprint carries the
        ``reaction`` activity and never ``research_material`` or ``research_time``. Their materials
        are still reduced by structure rigs, so `Facility.material_multiplier` applies to them even
        though ME does not."""
        return self.activity != REACTION


def _recipe(blueprint_id: int, activity: str, row: Mapping) -> Recipe | None:
    """One SDE activity row as a `Recipe`, or None when it cannot state a cost.

    A row with no materials has nothing to price and one with no product builds nothing anybody
    asked the price of; both are already dropped by `alphadata`, and re-checking here costs nothing
    on the way to a 4952-row index."""
    if not isinstance(row, Mapping):
        return None
    product = row.get("p") or ()
    materials = row.get("m") or {}
    if len(product) != 2 or not isinstance(materials, Mapping) or not materials:
        return None
    try:
        return Recipe(
            blueprint_id=blueprint_id,
            activity=str(activity),
            product_id=int(product[0]),
            product_qty=int(product[1]),
            time=int(row.get("t") or 0),
            max_runs=int(row.get("limit") or 0),
            materials={int(material): int(quantity) for material, quantity in materials.items()},
            alternatives=(),
        )
    except (TypeError, ValueError):
        return None


def recipe_index(document: Mapping) -> dict[int, Recipe]:
    """{product type id: `Recipe`} for every blueprint in a blueprint-materials document.

    The document is keyed by blueprint id and spells every id a string; this is the one place that
    reads it, so everything past here works with ints and looks a recipe up by what it makes rather
    than by what made it - which is exactly the question a cost asks ("how do I get 22 of this?").

    Five products in today's snapshot are made by more than one blueprint. The winner is the lowest
    blueprint type id, so two runs over the same document always agree: taking whichever row came
    last would let dict order out of the JSON reader decide the answer, and a cost that moves
    between runs with nothing changed is indistinguishable from a bug. The losers are not dropped -
    they are listed in `alternatives`, because "a different blueprint makes this too, possibly
    cheaper" is a fact for the user to weigh against the research levels they own, not a choice for
    an index to make silently on their behalf."""
    by_product: dict[int, list[Recipe]] = {}
    for key, activities in document.items():
        blueprint_id = _as_id(key)
        if blueprint_id is None:
            continue
        for activity, row in (activities or {}).items():
            recipe = _recipe(blueprint_id, activity, row)
            if recipe is not None:
                by_product.setdefault(recipe.product_id, []).append(recipe)

    index: dict[int, Recipe] = {}
    for product_id, recipes in by_product.items():
        # Activity is the tiebreak only so a blueprint with two activities producing one product
        # still resolves deterministically; no such row exists today.
        ordered = sorted(recipes, key=lambda recipe: (recipe.blueprint_id, recipe.activity))
        winner, *rest = ordered
        index[product_id] = replace(winner, alternatives=tuple(other.blueprint_id for other in rest))
    return index


def required_quantity(base: int, runs: int, me: int = 0, multiplier: float = 1.0) -> int:
    """Units of one material a job of `runs` installs consumes.

        required = max(runs, ceil(round(runs * base * (1 - ME/100) * multiplier, 2)))

    Measured 2026-09-09 against install screens, including the two parts people most often get
    wrong. The ``round(..., 2)`` before the ceiling is load-bearing: structure bonuses make the raw
    product a long decimal (7 units at ME 5 in a -3.75 % structure is 32.003125), and the game
    charges 32 where a naive ``ceil`` of the raw product charges 33 on every job of that shape.
    The ``max(runs, ...)`` floor is what makes a material used one-per-run immune to ME - no amount
    of research takes the last one of them away, so rounding it down would report an impossible
    recipe."""
    _require_me(me)
    _require_runs(runs)
    _require_multiplier(multiplier)
    per_job = runs * base * (1 - me / 100.0) * multiplier
    return max(runs, math.ceil(round(per_job, 2)))


def job_time(recipe: Recipe, runs: int, te: int = 0) -> int:
    """Seconds one job of `runs` installs takes: ``base_time * runs * (1 - TE/100)``.

    Measured 2026-09-09. Character industry skills and structure time bonuses are not in this
    number - they are the caller's to fold in, because no endpoint reports what a given character
    has trained. TE is ignored for an unresearchable recipe (a reaction cannot be researched, so a
    plan that says TE 20 on one still takes the full base time). Rounded rather than truncated: SDE
    times are whole seconds and TE arrives in whole percent, so the only sub-second residue worth
    deciding about is floating-point noise, which must not become a whole second either way."""
    _require_te(te)
    _require_runs(runs)
    return round(recipe.time * runs * (1 - (te if recipe.researchable else 0) / 100.0))


def estimated_item_value(recipe: Recipe, runs: int,
                         adjusted: Mapping[int, float]) -> tuple[float, tuple[int, ...]]:
    """(EIV, type ids with no published price) for one job of `runs` installs.

        EIV = sum(base_quantity * runs * adjusted_price)

    Measured 2026-09-09. Two things this deliberately does not do: it never applies ME (the install
    screen scales the fee with the goods you actually consume, but its Estimated Item Value is the
    base material value - applying research here underprices every job), and it never substitutes
    `average_price` for `adjusted_price`, because CCP's industry reference is a different figure
    from its rolling trade average and the game uses only the first.

    The second return value exists because a type missing from ``/markets/prices`` contributes
    nothing to the sum, and silently summing it as zero would report a fee too low with no hint that
    anything was left out."""
    _require_runs(runs)
    total = 0.0
    missing: list[int] = []
    for type_id, base in sorted(recipe.materials.items()):
        price = adjusted.get(type_id)
        if price is None:
            missing.append(type_id)
            continue
        total += base * runs * price
    return total, tuple(missing)


@dataclass(frozen=True)
class Facility:
    """Where the job runs, reduced to the four numbers the install screen bills.

    `cost_index` is the system's index for this activity as a fraction (Jita 4-4 manufacturing
    measured 0.1718 on 2026-09-09), straight from `cost_indices`. The tax and surcharge defaults are
    the NPC-station and SCC policy numbers; a player-owned structure bills differently, so its
    caller overrides whichever of them actually changes rather than the whole model.
    `material_multiplier` is the aggregate rig/structure material bonus (1.0 for none) - it is a
    facility property, not a research level, which is why it lives here and applies to reactions too."""

    cost_index: float
    facility_tax: float = NPC_STATION_TAX
    material_multiplier: float = 1.0
    scc_surcharge: float = SCC_SURCHARGE


def job_cost(eiv: float, facility: Facility) -> float:
    """The ISK the installation itself costs: ``EIV * (index + tax + surcharge)``.

    Measured 2026-09-09. Nothing here depends on runs except through EIV - which is why a job of
    many runs is worth installing once rather than repeatedly."""
    return eiv * (facility.cost_index + facility.facility_tax + facility.scc_surcharge)


@dataclass(frozen=True)
class Prices:
    """The two price bases a build cost can be stated in, both keyed by type id.

    `unit` is the cheapest ask at whatever scope was read - what it costs to buy one today.
    `adjusted` is CCP's published industry reference from ``/markets/prices``, which exists for
    types no order book shows and is the number the game itself uses for EIV. A published 0.0 is a
    price (PLEX carries one), so absence means "no basis" while zero means "free"; only the first
    makes a type unpriced."""

    unit: Mapping[int, float]      # cheapest ask at the scope, per unit
    adjusted: Mapping[int, float]  # /markets/prices adjusted_price, per unit

    def quote(self, type_id: int) -> tuple[float | None, str | None]:
        """(price, basis) for one type: an ask if there is one, else CCP's figure, else nothing.

        The ask wins because it is a price someone will actually fill; ``esi_adjusted`` is a
        fallback that keeps a type with no visible book from dropping out of the cost entirely, and
        it is labelled so the caller can say "published figure, not an order" instead of presenting
        it as something to click. Never 0.0 for a missing type: an input costing nothing is a
        different and much worse statement than an input nobody would quote."""
        unit = self.unit.get(type_id)
        if unit is not None:
            return unit, "min_sell"
        adjusted = self.adjusted.get(type_id)
        if adjusted is not None:
            return adjusted, "esi_adjusted"
        return None, None


@dataclass(frozen=True)
class BuildQuote:
    """One candidate job that would supply a material, costed at whole runs.

    `runs` is the smallest number of installs covering what the parent job needs, so `units` is
    always ``runs * product_qty`` and `surplus` is what that leaves over. The whole cost of those
    runs is charged even when only part of the yield is wanted: a blueprint producing 10 per run
    cannot be built in quantity 22, and pricing 2.2 runs would understate every plan whose
    requirement does not land on a run boundary.

    `unpriced` names this job's own materials that no basis could price. Non-empty means the total
    below is missing an input and must never be offered as what building costs.

    `eiv_missing` is the softer gap, and the same distinction the top-level plan draws: a material
    that has an ask but no `/markets/prices` adjusted row can be bought, so the material cost is
    whole, but CCP levies the install fee on a value that has no figure for it. The fee is
    therefore charged low, and the shortfall has to be named rather than folded into a total that
    presents itself as complete."""

    recipe: Recipe
    runs: int
    units: int                 # runs * product_qty, not the quantity wanted
    surplus: int               # units - what the parent job needed
    material_cost: float       # buying this job's materials at their best basis
    eiv: float
    job_cost: float            # installation fee for this job
    total: float               # material_cost + job_cost
    unit: float                # total / units, i.e. per unit actually produced
    time: int                  # seconds this job takes
    unpriced: tuple[int, ...]
    eiv_missing: tuple[int, ...]


@dataclass(frozen=True)
class Component:
    """One row of the material table: what a direct material is and how it was obtained.

    `build` is the candidate build whenever any blueprint could make this type - it is kept even
    when buying won, because "you could build it for X instead" is the half of the answer a user
    acts on. `source` is what the plan actually charged: "buy", "build", or "unpriced" for a type no
    basis would quote, which contributes nothing to any total rather than being counted as free.
    `forced` says the user overrode the cheaper choice, so a total that disagrees with the tool's own
    preference is explained by the row that produced it."""

    type_id: int
    required: int              # units the parent job consumes, after ME and rig bonus
    buy_unit: float | None     # best basis per unit, whether or not buying won
    buy_basis: str | None      # "min_sell" | "esi_adjusted" | None
    build: BuildQuote | None   # the candidate whole-run job, if one exists
    source: str                # "buy" | "build" | "unpriced"
    unit_cost: float | None    # per unit as charged (for a build: per unit produced)
    cost: float | None         # what this row adds to the plan's material cost
    surplus: int               # units left over from building; 0 when buying
    forced: bool


@dataclass(frozen=True)
class BuildPlan:
    """The cost of `runs` installs of one blueprint, materials chosen component by component.

    `material_cost` is the sum of the components' costs (a built component at its whole-run job
    cost), `job_cost` the installation fee for this job alone, and `total` the two together - which
    is what "what does it cost me to make N of these" asks. `me`/`te` are the levels actually applied
    to the top job: an unresearchable recipe records 0 whatever was asked for, so the plan describes
    the number printed rather than the request that produced it. `component_me` is the level asked
    for the component jobs, which is a separate number by default (see `DEFAULT_COMPONENT_ME`) -
    whether any one of those jobs can use it is decided per component, since a reaction among them
    still takes none.

    `cost_per_unit` is None exactly when `unpriced` is non-empty. The totals stay the sum of what
    could be priced - the shortfall has to be addable by hand - but a per-unit figure is the number
    somebody quotes to another person, and one that quietly omits an input is not that number."""

    recipe: Recipe
    runs: int
    me: int
    te: int
    component_me: int                  # level asked for the component jobs, not for this one
    units: int                         # runs * product_qty
    components: tuple[Component, ...]  # descending required quantity, then type id
    material_cost: float
    eiv: float
    job_cost: float
    total: float
    cost_per_unit: float | None
    time: int
    unpriced: tuple[int, ...]          # types no basis could price; in no total above
    eiv_missing: tuple[int, ...]       # materials with no adjusted_price, so absent from `eiv`


def _expandable(material_id: int, recipe: Recipe, index: Mapping[int, Recipe]) -> Recipe | None:
    """The blueprint that could build one direct material, or None when it has to be bought.

    A material equal to the product being built is never expanded: an alternative blueprint for the
    item itself would otherwise have the plan build the thing in order to build it, and every number
    in the answer would describe a loop."""
    if material_id == recipe.product_id:
        return None
    candidate = index.get(material_id)
    if candidate is None or not candidate.materials:
        return None
    return candidate


def _component_build(material_id: int, required: int, recipe: Recipe, index: Mapping[int, Recipe],
                     prices: Prices, facility: Facility, component_me: int,
                     te: int) -> BuildQuote | None:
    """Cost supplying `required` units of one direct material by building it, at whole runs.

    `component_me` is the level asked for component jobs, never the top job's - the two are allowed
    to differ, and a name that said "me" here would read as though they were one value again. A
    component that cannot be researched takes neither level, whatever was asked."""
    candidate = _expandable(material_id, recipe, index)
    if candidate is None:
        return None
    # Whole installs only - and deliberately not capped by the component's own maxProductionLimit:
    # fee and materials are linear in runs, so several installs cost what one big install would,
    # while the limit check on the *requested* job still stands because there the game refuses it.
    job_runs = -(-required // candidate.product_qty)
    me_job = component_me if candidate.researchable else 0
    te_job = te if candidate.researchable else 0

    material_cost = 0.0
    missing: list[int] = []
    for sub_id, base in sorted(candidate.materials.items()):
        unit, _basis = prices.quote(sub_id)
        if unit is None:
            missing.append(sub_id)
            continue
        material_cost += required_quantity(base, job_runs, me_job, facility.material_multiplier) * unit

    eiv, eiv_missing = estimated_item_value(candidate, job_runs, prices.adjusted)
    fee = job_cost(eiv, facility)
    units = job_runs * candidate.product_qty
    total = material_cost + fee
    return BuildQuote(recipe=candidate, runs=job_runs, units=units, surplus=units - required,
                      material_cost=material_cost, eiv=eiv, job_cost=fee, total=total,
                      unit=total / units, time=job_time(candidate, job_runs, te_job),
                      unpriced=tuple(sorted(missing)), eiv_missing=eiv_missing)


def _normalized_force(force: Mapping[int, str] | None) -> dict[int, str]:
    """`force` as {type id: "buy"|"build"}, refusing a choice the model cannot honour.

    Keys name materials; an entry naming a type this recipe does not consume is left unused rather
    than refused, so one list of "always build these" can be reused across several items."""
    choices: dict[int, str] = {}
    for key, value in (force or {}).items():
        choice = str(value)
        if choice not in ("buy", "build"):
            raise RuntimeError(f"choice for forced material {key} must be 'build' or 'buy', "
                               f"got '{value}'")
        type_id = _as_id(key)
        if type_id is None:
            raise RuntimeError(f"forced material '{key}' is not a type id")
        choices[type_id] = choice
    return choices


def _choose(type_id: int, required: int, buy_unit: float | None, buy_basis: str | None,
            build: BuildQuote | None, force: str | None) -> Component:
    """One component row: the cheaper of buying and building, or what the user insisted on.

    Ties go to buying - a job that costs exactly the same as walking to the market adds time, a
    slot and a risk of being interrupted, so it has no claim on a tie. A build with unpriced inputs
    is not chargeable and never wins: an input nobody would price is not an input worth nothing."""
    buy_cost = None if buy_unit is None else buy_unit * required
    chargeable = build is not None and not build.unpriced

    if force == "build":
        if not chargeable:
            raise RuntimeError(
                f"cannot build {type_id} because "
                + ("no blueprint in the local data makes it" if build is None else
                   f"its own materials cannot be priced ({', '.join(str(t) for t in build.unpriced)})")
                + " - leave that one to be bought instead of forcing it")
        return Component(type_id=type_id, required=required, buy_unit=buy_unit, buy_basis=buy_basis,
                         build=build, source="build", unit_cost=build.unit, cost=build.total,
                         surplus=build.surplus, forced=True)
    if force == "buy":
        if buy_unit is None:
            raise RuntimeError(f"cannot buy {type_id}: no sell order and no published price for it "
                               "- leave that one to be built instead of forcing it")
        return Component(type_id=type_id, required=required, buy_unit=buy_unit, buy_basis=buy_basis,
                         build=build, source="buy", unit_cost=buy_unit, cost=buy_cost, surplus=0,
                         forced=True)

    if chargeable and (buy_unit is None or build.total < buy_cost):
        return Component(type_id=type_id, required=required, buy_unit=buy_unit, buy_basis=buy_basis,
                         build=build, source="build", unit_cost=build.unit, cost=build.total,
                         surplus=build.surplus, forced=False)
    if buy_unit is not None:
        return Component(type_id=type_id, required=required, buy_unit=buy_unit, buy_basis=buy_basis,
                         build=build, source="buy", unit_cost=buy_unit, cost=buy_cost, surplus=0,
                         forced=False)
    return Component(type_id=type_id, required=required, buy_unit=None, buy_basis=None, build=build,
                     source="unpriced", unit_cost=None, cost=None, surplus=0, forced=False)


def pricing_ids(recipe: Recipe, index: Mapping[int, Recipe]) -> set[int]:
    """Every type a one-level build cost needs a price for.

    The product itself (so the answer can be compared with what the item sells for), its direct
    materials, and the materials of each direct material that could be built - because deciding
    build-or-buy for a component means knowing what that job would consume. No deeper: past one
    level the model buys, so a type two steps down never enters a total and asking ESI for it would
    spend requests on a number this plan cannot use."""
    ids = {recipe.product_id}
    for material_id in recipe.materials:
        ids.add(material_id)
        candidate = _expandable(material_id, recipe, index)
        if candidate is not None:
            ids.update(candidate.materials)
    return ids


def plan_build(recipe: Recipe, index: Mapping[int, Recipe], prices: Prices, facility: Facility, *,
               runs: int = 1, me: int = 0, te: int = 0,
               component_me: int | None = None,
               force: Mapping[int, str] | None = None) -> BuildPlan:
    """Cost `runs` installs of `recipe`, taking whichever is cheaper for each material.

    One level deep and no further. A direct material that has a blueprint is costed as its own job -
    whole runs of it, materials bought at their best basis, fee from the same facility - but that
    job's materials are never expanded again. Going deeper needs research levels, rig bonus and
    install limits for every link in the chain, none of which a caller has per component, and a
    five-deep chain of estimates is not a cost; the honest answer past the first level is buying.

    The two ME levels are not the same number by default, and that is deliberate: `me` is the level
    of the blueprint being run, while every component job runs at `component_me` (ME 10 when the
    caller names none). One value for both described a build nobody makes - a T2 hull usually comes
    from an invented copy, which cannot carry research, fed by components whose BPOs their owner has
    had long enough to take to the cap - and it did so in the pessimistic direction, since ME 0 is
    charged against every material of every component. TE stays single: it moves job time only, and
    no ISK total here adds time up, so a second knob would buy nothing.

    A recipe that cannot be researched (a reaction) takes neither ME nor TE at any level asked for,
    top level or component: reporting a 20 %-faster reaction would be describing research that does
    not exist. The levels are still range-checked even then, since a plan asked for ME 40 has a typo
    in it whatever the recipe turns out to be.

    A component with no market price and no chargeable build is reported as unpriced rather than as
    free, and `cost_per_unit` goes None while any such row exists."""
    component_me = DEFAULT_COMPONENT_ME if component_me is None else component_me
    _require_me(me)
    _require_me(component_me)
    _require_te(te)
    _require_runs(runs)
    _require_runs_within_limit(recipe, runs)
    _require_multiplier(facility.material_multiplier)
    choices = _normalized_force(force)

    me_job = me if recipe.researchable else 0
    te_job = te if recipe.researchable else 0
    units = runs * recipe.product_qty

    eiv, eiv_missing = estimated_item_value(recipe, runs, prices.adjusted)
    fee = job_cost(eiv, facility)

    components: list[Component] = []
    for type_id, base in recipe.materials.items():
        required = required_quantity(base, runs, me_job, facility.material_multiplier)
        buy_unit, buy_basis = prices.quote(type_id)
        build = _component_build(type_id, required, recipe, index, prices, facility, component_me, te)
        components.append(_choose(type_id, required, buy_unit, buy_basis, build, choices.get(type_id)))

    # Biggest consumer first: that is where a cost is won or lost, and an unpriced row should not
    # hide at the bottom of the table. Type id breaks ties so two runs print in one order.
    components.sort(key=lambda component: (-component.required, component.type_id))

    material_cost = sum(component.cost for component in components if component.cost is not None)
    unpriced = tuple(sorted(component.type_id for component in components
                            if component.source == "unpriced"))
    # A component job's own EIV gap is the parent's problem too: its install fee is part of the
    # total printed here, and a fee levied on an incomplete value makes that total low. Only the
    # builds actually charged can shift it, so a gap inside a component nobody builds is not one.
    eiv_gaps = set(eiv_missing)
    for component in components:
        if component.source == "build" and component.build is not None:
            eiv_gaps.update(component.build.eiv_missing)
    total = material_cost + fee
    return BuildPlan(
        recipe=recipe,
        runs=runs,
        me=me_job,
        te=te_job,
        component_me=component_me,
        units=units,
        components=tuple(components),
        material_cost=material_cost,
        eiv=eiv,
        job_cost=fee,
        total=total,
        cost_per_unit=None if unpriced else total / units,
        time=job_time(recipe, runs, te_job),
        unpriced=unpriced,
        eiv_missing=tuple(sorted(eiv_gaps)),
    )


@dataclass(frozen=True)
class CostIndices:
    """ESI's per-system, per-activity cost indices for the whole cluster.

    The document is one page for every system and ESI stamps it with an hour-long `Expires`, so a
    run that costs ten items pays for one request; `meta` carries that stamp for the freshness line
    rather than the moment the request went out."""

    by_system: Mapping[int, Mapping[str, float]] = field(default_factory=dict)
    meta: esi_mod.Meta = field(default_factory=esi_mod.Meta)

    def index(self, system_id: int, activity: str) -> float | None:
        """The cost index for one activity in one system; None when ESI had nothing to say.

        Absent is not 0.0. An index of zero would report an installation fee of just the tax and
        surcharge - a plausible total, wrong by whatever Jita's 0.17 contributes - so the caller has
        to be handed the chance to say "ESI lists no manufacturing index for that system" instead."""
        return self.by_system.get(system_id, {}).get(activity)


def cost_indices(client: esi_mod.Esi) -> CostIndices:
    """Read `COST_INDEX_PATH` once and fold it into {system id: {activity: index}}.

    Public and unauthenticated, one page, no pagination to walk - so this is a single `get_meta`
    rather than the paginated helper, and asking for a second page would be guessing at a behaviour
    that does not exist. Rows are kept per activity as ESI spells them (``manufacturing``,
    ``reaction``, ``invention``, ``copying``, the two research activities): only the first two name
    an SDE blueprint activity, but reading the whole row costs nothing and lets a caller report an
    index for a job type this module cannot yet cost."""
    rows, meta = client.get_meta(COST_INDEX_PATH)
    by_system: dict[int, dict[str, float]] = {}
    for row in rows or []:
        system_id = _as_id(row.get("solar_system_id"))
        if system_id is None:
            continue
        entry = by_system.setdefault(system_id, {})
        for activity in row.get("cost_indices") or []:
            name = activity.get("activity")
            index = activity.get("cost_index")
            if not isinstance(name, str) or index is None:
                continue     # a row without an activity name cannot be selected by anything
            try:
                entry[name] = float(index)
            except (TypeError, ValueError):
                continue
    return CostIndices(by_system=by_system, meta=meta)
