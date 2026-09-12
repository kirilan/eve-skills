"""build-cost: what an install would cost, and whether buying beats it."""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from dataclasses import dataclass


from . import alphadata, esi as esi_mod, exports, industry, market, render, sso


BUILD_COST_COLUMNS = ["material", "qty", "buy/u", "build/u", "source", "cost", "surplus"]
# One row per material per product, with the job's own parameters repeated on every row so a
# spreadsheet can still slice one blueprint out of a run that priced several.
BUILD_COST_CSV_COLUMNS = ["product_id", "product_name", "blueprint_id", "activity", "runs", "me",
                          "component_me", "te", "units", "type_id", "material_name", "required",
                          "buy_unit", "buy_basis", "build_unit", "source", "forced", "cost", "surplus"]


def _percent(spec: float, flag: str) -> float:
    """A percent flag as the fraction `industry` multiplies by, converted exactly once.

    The model works in fractions because every formula it owns is a multiplication; the flag works in
    percent because nobody types "a quarter of one percent" as 0.0025. Converting here rather than
    deeper in leaves one place where a factor of 100 can go wrong, and the range check catches the
    other direction: somebody who read `--facility-tax 0.25` as "a quarter of the EIV" would otherwise
    be quoted an install fee forty times too small without a word of complaint."""
    if not 0.0 <= spec <= 100.0:
        raise RuntimeError(f"{flag} is a percentage between 0 and 100 - the NPC-station default of "
                           f"{industry.NPC_STATION_TAX * 100:g} means a quarter of one percent, "
                           f"got {spec:g}")
    return spec / 100.0


def build_scope(client: esi_mod.Esi, args) -> tuple[market.Scope, int]:
    """The one order book this run prices from, and the system whose index bills the install.

    `market` takes repeatable scopes because comparing two stations is exactly what a trader asks. A
    build cost is one shopping trip: every material comes off one book, so `--hub`/`--region` are
    single-valued here and asking for both is a contradiction rather than a wider net - there is no
    honest total to be made out of two stations' asks at once.

    The second value may not quietly default to Jita when the scope is a region. A region holds dozens
    of systems, their cost indices differ by more than the tax does, and picking one would print an
    invented fee inside somebody's total. Only a hub knows the system it stands in."""
    if args.hub and args.region:
        raise RuntimeError("--hub and --region each pick one scope; give only one of them - a build "
                           "cost is priced from a single order book")
    if args.region:
        region_id, name = market.resolve_region(client, args.region)
        scope = market.Scope(region_id, name)
    elif args.hub:
        scope = market.hub_scope(args.hub)
    else:
        scope = market.hub_scope("jita")
    if args.system:
        # Naming a system moves the install, not the shopping: ESI's order books are regional, and we
        # are not going to infer a region from a system in order to narrow them. So `--system Amarr`
        # with the default scope really does mean "buy at Jita, build in Amarr".
        return scope, market.resolve_system(client, args.system)[0]
    if scope.system_id is None:
        raise RuntimeError(f'--region has no single solar system, so there is no industry cost index '
                           f'to bill {scope.label} with - name one with --system, e.g. --system "Jita"')
    return scope, scope.system_id


def _recipe_index() -> dict[int, industry.Recipe]:
    """{product type id: Recipe} from the local SDE snapshot, read once per run.

    A missing snapshot is not a traceback and not an empty plan: `plan` answers the same situation
    with the one command that fixes it, and this command has no better advice to give."""
    try:
        document = alphadata.blueprint_materials()
    except FileNotFoundError:
        raise RuntimeError("no local blueprint data - run: eve-skills update-data") from None
    return industry.recipe_index(document)


def _recipe_for(index, type_id: int, name: str) -> industry.Recipe:
    """The blueprint that makes one requested product; refusal when none does.

    Most of what a player types here - ore, a shop-bought module, a ship nobody launches - has no
    blueprint at all. The honest answer is that the SDE knows no way to build it, said before a single
    order book is read, rather than a table of nothing bought at great expense."""
    recipe = index.get(type_id)
    if recipe is None:
        raise RuntimeError(f"no blueprint in the local SDE data makes {name} (type {type_id}) - only "
                           f"manufactured and reacted items have a build cost; for the newest "
                           f"blueprint list run: eve-skills update-data")
    return recipe


def _buildable(recipe: industry.Recipe, material_id: int, index) -> bool:
    """Whether some blueprint could make one direct material of `recipe`.

    This restates `industry._expandable`, which is private and cannot be asked, because the blanket
    `--build-all` has to know which materials it is promising: a recipe's ore is buildable by nobody,
    and forcing it would fail the whole run over a line nobody typed. A material that is the product
    itself is never expandable - building the item in order to build the item is a loop, not a plan."""
    if material_id == recipe.product_id:
        return False
    candidate = index.get(material_id)
    return candidate is not None and bool(candidate.materials)


def _forced_names(client: esi_mod.Esi, args) -> dict[int, str]:
    """`--build`/`--buy` as {type id: "build" | "buy"}, resolved before anything expensive is read.

    Being told a component's name was wrong after forty order books would be a bad afternoon."""
    force: dict[int, str] = {}
    for specs, choice in ((args.build, "build"), (args.buy, "buy")):
        for spec in specs or []:
            type_id = market.resolve_type(client, spec)[0]
            if type_id in force:
                raise RuntimeError(f"type {type_id} is named by both --build and --buy; one material "
                                   f"can be forced one way only")
            force[type_id] = choice
    return force


def _expand_force(force: dict[int, str], args, index, recipes, prices: industry.Prices) -> None:
    """The blanket flags, filled in from what is buildable and what anyone quotes.

    Done after the books are read because `--buy-all` needs to know which materials have a price:
    forcing a purchase ESI cannot price is refused by the model, and losing the whole report over a
    line nobody typed individually is a worse answer than leaving that row unpriced. `--build-all`
    skips what no blueprint makes for the same reason, but stops there - forcing a build whose own
    inputs are unpriceable genuinely cannot be honoured, so `industry.plan_build` says so in terms of
    the one type at fault instead of us guessing which half of the request to drop."""
    if args.build_all:
        for recipe in recipes:
            for material_id in recipe.materials:
                if _buildable(recipe, material_id, index):
                    force.setdefault(material_id, "build")
    if args.buy_all:
        for recipe in recipes:
            for material_id in recipe.materials:
                if prices.quote(material_id)[0] is not None:
                    force.setdefault(material_id, "buy")


@dataclass(frozen=True)
class BuildTarget:
    """One requested product, the job this run costed for it, and where that job would be installed.

    The facility travels with the target rather than with the run because the cost index is per
    activity: two products in one command can be a manufacturing job and a reaction, billed on two
    different numbers from the same `/industry/systems` page."""
    type_id: int
    name: str
    recipe: industry.Recipe
    plan: industry.BuildPlan
    facility: industry.Facility


@dataclass(frozen=True)
class BuildRun:
    """Everything one `build-cost` run decided, so all three output formats print one truth.

    Renderers get the whole run instead of a per-product slice because the notes are shared: scope,
    price basis, freshness and cost index describe every block equally, and a renderer that had to
    re-derive them would be free to disagree with the one that printed first."""
    now: float
    scope: market.Scope
    system_id: int
    facility_tax: float
    scc_surcharge: float
    material_multiplier: float
    indices_used: dict[str, float]
    index_meta: esi_mod.Meta
    figures: market.BookFigures
    reference: market.PriceTable
    names: dict[int, str]
    targets: tuple[BuildTarget, ...]
    rules: str | None
    warnings: tuple[str, ...]


def _esi_age(meta: esi_mod.Meta, now: float) -> str:
    """When ESI generated a document and how long ago, for payloads that are not order books.

    `market.freshness_line` is the right words for a book because it names the five-minute refresh.
    `/markets/prices` and `/industry/systems` move on their own schedules, so dating one of them with
    that cadence would stamp a number with someone else's clock."""
    if meta.last_modified is None:
        return "with no Last-Modified, so its age is unknown"
    return (f"generated {time.strftime('%H:%M:%SZ', time.gmtime(meta.last_modified))}, "
            f"{market.format_age(now - meta.last_modified)} ago")


def _build_preflight_notice(info: market.Preflight) -> str:
    """What the book fan-out is about to cost, said before it starts.

    Counts rather than a progress bar, in the register `exports` set for the same notice and at its
    measured per-book rate rather than a re-guessed one. A build costs more distinct types than an
    inventory does - every material plus every material of every component - so this is the one part
    of the wait a user cannot infer from the command they typed."""
    kinds = "" if info.types == 1 else "s"
    if not info.fetches:
        return (f"pricing {info.types} distinct type{kinds} for this build: every figure is already "
                f"in the local quote cache, so no order book is read")
    line = (f"pricing {info.types} distinct type{kinds} for this build: {info.fetches} order "
            f"book{'s' if info.fetches != 1 else ''} to read, one per type")
    if info.cached:
        line += f" ({info.cached} already priced from the last run)"
    seconds = info.fetches * exports.BOOK_SECONDS_PER_TYPE
    if seconds >= exports.NOTICE_MIN_SECONDS:
        line += f"; about {market.format_age(seconds)} at this size"
    return line


def _build_requests_note(figures: market.BookFigures) -> str:
    """What the price fan-out actually cost, so a cheap run and an expensive one look different."""
    if not figures.fetched:
        return (f"no order-book request: all {figures.cached} types came from the local quote cache, "
                f"which ESI's own expiry says is still current")
    line = f"{figures.fetched} order-book request{'s' if figures.fetched != 1 else ''} now"
    if figures.cached:
        line += f", {figures.cached} type{'s' if figures.cached != 1 else ''} from the cache"
    if figures.failed:
        line += f", {figures.failed} did not answer"
    return line


def _build_unit_charge(component: industry.Component) -> float | None:
    """What the build option would charge per unit of the material the recipe actually needs.

    Deliberately not `BuildQuote.unit`, which is per unit *produced*: a component blueprint yielding
    100 per run would print its batch price in the row beside the `buy` decision that beat it, and the
    table would read as though a cheap option had lost to an expensive one by mistake. Dividing the
    whole job's cost by the requirement is the comparison the plan itself made.

    None - a dash, never 0.00 - when there is no build option, or when that option has a material it
    cannot price: `industry` treats such a build as not chargeable and never lets it win, so any total
    printed for it would be missing an input."""
    if component.build is None or component.build.unpriced:
        return None
    return component.build.total / component.required


def _build_heading(target: BuildTarget) -> str:
    """One line saying what would be built, how much of it, and under what research."""
    plan, recipe = target.plan, target.recipe
    units = f"{plan.units:,} unit" + ("" if plan.units == 1 else "s")
    runs = f"{plan.runs} run" + ("" if plan.runs == 1 else "s")
    # A reaction has no research to speak of. Printing "ME 0 / TE 0" for one would imply the zeros were
    # choices the user made rather than a thing that does not exist.
    research = (f"at ME {plan.me} / TE {plan.te}" if recipe.researchable
                else "a reaction (no ME or TE to research)")
    # The component jobs take their own ME, and it is the level that decides most of the material
    # line - so it belongs on the heading beside the one people came to read. Only when this recipe
    # really has something to build: naming a level for a job that will not run is noise, and on a
    # fully-bought recipe it would be the number doing most of the talking for no reason at all.
    if any(component.build is not None for component in plan.components):
        research += f", components at ME {plan.component_me}"

    return (f"{target.name} (id {target.type_id}) - {units} from {runs}, {research}, "
            f"blueprint {recipe.blueprint_id}")


def _build_table_rows(run: BuildRun, target: BuildTarget) -> list[list[str]]:
    """One row per direct material: what it costs either way, and which way was charged."""
    return [[exports.name_or_id(run.names, component.type_id), f"{component.required:,}",
             render.isk(component.buy_unit), render.isk(_build_unit_charge(component)), component.source,
             render.isk(component.cost), f"{component.surplus:,}"]
            for component in target.plan.components]


def _surplus_footnote(target: BuildTarget) -> str | None:
    """Why a build can cost more than the recipe asked for, whenever that is possible in this plan.

    Named only when some component's build option would leave surplus - including the ones that lost
    to buying, because a reader comparing the two columns is exactly the reader who needs to know that
    they are not like for like. An option with a material nobody priced is not counted: it prints a
    dash rather than a figure, so there would be nothing here to explain."""
    if not any(component.build and not component.build.unpriced and component.build.surplus
               for component in target.plan.components):
        return None
    return ("  A component job runs in whole runs, so the build column charges every run needed to\n"
            "  cover the quantity above: a blueprint that yields more than the recipe wants leaves\n"
            "  real surplus behind - product you own and could sell, not waste.")


def _totals_lines(run: BuildRun, target: BuildTarget) -> list[str]:
    """The money, with the install fee's arithmetic left visible instead of baked into one number."""
    plan, facility = target.plan, target.facility
    fee = (f"= EIV x ({facility.cost_index:.4f} cost index + {facility.facility_tax:.4f} facility "
           f"tax + {facility.scc_surcharge:.4f} SCC surcharge)")
    # The model withholds `cost_per_unit` exactly when a material has no price (see the docstring on
    # `industry.BuildPlan`), so the dash and the reason for it have to be printed together; the note
    # used to hang off the branch that prints a figure, where nothing can reach it.
    per_unit = ("-  (not stated while a material has no price - see the note below)"
                if plan.cost_per_unit is None else f"{render.isk(plan.cost_per_unit)} ISK")
    return ["totals:",
            f"  material cost   {render.isk(plan.material_cost)} ISK",
            f"  EIV             {render.isk(plan.eiv)} ISK  (base quantities x ESI adjusted price; ME does "
            f"not reduce it)",
            f"  job cost        {render.isk(plan.job_cost)} ISK  {fee}",
            f"  total           {render.isk(plan.total)} ISK",
            f"  cost per unit   {per_unit}",
            f"  job time        {render.format_duration(plan.time)}"]


def _comparison_lines(run: BuildRun, target: BuildTarget) -> list[str]:
    """The same output bought instead of built, from the books this run already read.

    A build cost is only worth computing because of the decision it feeds, and the decision needs the
    alternative: 19.5 M ISK for a Hound means nothing until it is next to what one costs on the wing.
    Both sides come out of the one `BookFigures` fetched for the materials, so the comparison cannot
    be quoted from a different minute than the total."""
    plan = target.plan
    ask = run.figures.min_sell.get(target.type_id)
    bid = run.figures.max_buy.get(target.type_id)
    units = f"{plan.units:,} unit" + ("" if plan.units == 1 else "s")
    if ask is None and bid is None:
        return [f"buy instead: nobody is quoting {target.name} at {run.scope.label}, so this build "
                f"cost has nothing to be compared with - the total above stands alone"]
    lines = [f"buy instead: cheapest ask {render.isk(ask)} ISK, richest bid {render.isk(bid)} ISK at "
             f"{run.scope.label}"]
    if ask is None:
        return lines + ["  nobody is selling it there, so buying cannot be priced; the build total is "
                        "the only side of this you could act on"]
    buy = ask * plan.units
    if plan.cost_per_unit is None:
        return lines + [f"  the same {units} would cost {render.isk(buy)} ISK to buy against an incomplete "
                        f"build total - with a material unpriced the two are not comparable"]
    winner, by = (("building", buy - plan.total) if buy >= plan.total
                  else ("buying", plan.total - buy))
    return lines + [f"  {winner} is cheaper by {render.isk(by)} ISK for {units} "
                    f"(build {render.isk(plan.total)} vs buy {render.isk(buy)})"]


def _build_product_notes(run: BuildRun, target: BuildTarget) -> list[str]:
    """What this product's totals left out, or left a choice about.

    `alternatives` is a note and not an error because the SDE lists several blueprints for some
    products with no way to know which one is in the hangar: the run picked one and says how many
    others existed rather than pretending there was only ever one."""
    recipe, plan = target.recipe, target.plan
    notes = []
    if recipe.alternatives:
        others = ", ".join(str(blueprint_id) for blueprint_id in recipe.alternatives)
        count = len(recipe.alternatives)
        notes.append(f"blueprint {recipe.blueprint_id} was costed; {count} other "
                     f"blueprint{'s' if count != 1 else ''} make this product too ({others}) - your "
                     f"own research levels and rig bonuses can make a different one cheaper")
    if plan.unpriced:
        names = ", ".join(exports.name_or_id(run.names, ident) for ident in plan.unpriced)
        notes.append(f"{names}: no sell order there and no published price, so excluded from every "
                     f"total above rather than counted as free")
    if plan.eiv_missing:
        names = ", ".join(exports.name_or_id(run.names, ident) for ident in plan.eiv_missing)
        notes.append(f"{names}: absent from EIV only (ESI's price document has no adjusted price for "
                     f"it), so the install fee understates its share; its material cost is still in "
                     f"the total")
    return notes


def _build_run_notes(run: BuildRun) -> list[str]:
    """What the numbers are, how old they are, and what they were assembled from.

    Printed once per run rather than under every product, because scope, price basis, freshness and
    cost index describe all of the blocks equally."""
    region = run.names.get(run.scope.region_id) or f"region {run.scope.region_id}"
    lines = [f"scope: {run.scope.label} - every material priced off one order book: {region}",
             "price basis: the cheapest standing ask there, falling back to ESI's published industry "
             "reference where no order exists; a published figure is not something you can buy at"]
    if run.material_multiplier != 1.0:
        lines.append(f"material multiplier {run.material_multiplier:g} applied to every requirement - "
                     f"an aggregate rig or structure bonus, so the quantities above are below the "
                     f"blueprint's own")
    if run.figures.answered:
        # `freshness_line` labels its own failure case, and a line that began "order books:
        # freshness:" would read as a typo rather than as ESI having answered without a date.
        age = market.freshness_line(run.figures.meta, run.now).removeprefix("freshness: ")
        lines.append(f"order books: {age}")
    else:
        lines.append("order books: none answered - every figure below came from ESI's published "
                     "reference or is missing")
    lines.append(f"ESI price document: {_esi_age(run.reference.meta, run.now)}")
    system = run.names.get(run.system_id) or f"system {run.system_id}"
    for activity, value in sorted(run.indices_used.items()):
        lines.append(f"cost index: {activity} {value:.4f} in {system} ({run.system_id}); "
                     f"/industry/systems {_esi_age(run.index_meta, run.now)}")
    if run.rules:
        lines.append(f"note: {run.rules}")
    lines.append(f"requests: {_build_requests_note(run.figures)}")
    return lines


def build_cost_text(run: BuildRun) -> str:
    """One block per product, then the notes that describe the whole run."""
    blocks = []
    for target in run.targets:
        lines = [_build_heading(target),
                 render.table(BUILD_COST_COLUMNS, _build_table_rows(run, target))]
        footnote = _surplus_footnote(target)
        if footnote:
            lines.append(footnote)
        lines += _totals_lines(run, target)
        lines += _comparison_lines(run, target)
        lines += [f"  {line}" for line in _build_product_notes(run, target)]
        blocks.append("\n".join(lines))
    blocks.append("\n".join(_build_run_notes(run)))
    return "\n\n".join(blocks)


def _typed_docs(ids, names: dict[int, str]) -> list[dict]:
    """Unpriced types as ids with their names beside them, so a script needs no second lookup."""
    return [{"type_id": ident, "name": names.get(ident) or f"type {ident}"} for ident in ids]


def _component_doc(component: industry.Component, names: dict[int, str]) -> dict:
    """One material row, including the option that lost.

    `build.unit` is per unit *produced*, which is not what the table prints: the human column charges
    `build.total / required`, because a component blueprint that yields 100 per run has to be compared
    against the requirement, not against one of its own outputs. Both numbers are here, so a script
    can see why a build lost without having to know that."""
    build = component.build
    return {"type_id": component.type_id, "name": names.get(component.type_id) or f"type {component.type_id}",
            "required": component.required, "source": component.source,
            "forced": component.forced, "unit_cost": component.unit_cost, "cost": component.cost,
            "surplus": component.surplus,
            "buy": {"unit": component.buy_unit, "basis": component.buy_basis},
            "build": None if build is None else {
                "blueprint_id": build.recipe.blueprint_id, "runs": build.runs, "units": build.units,
                "surplus": build.surplus, "unit": build.unit, "material_cost": build.material_cost,
                "job_cost": build.job_cost, "eiv": build.eiv, "total": build.total,
                "unpriced": _typed_docs(build.unpriced, names),
                "eiv_missing": _typed_docs(build.eiv_missing, names)}}


def build_cost_json(run: BuildRun) -> dict:
    """Machine-readable output: raw numerics, ISO-Z stamps, ids and names together.

    The prose notes are not repeated here - their content is in `unpriced`, `eiv_missing`, `market`
    and the freshness fields - but `warnings` keeps the caveats that qualify the totals themselves,
    because a parser that sums `total` cannot otherwise tell an incomplete one from a finished one."""
    figures = run.figures
    return {
        "generated": market.iso_utc(run.now),
        "scope": {"label": run.scope.label, "region_id": run.scope.region_id,
                  "region_name": run.names.get(run.scope.region_id),
                  "system_id": run.scope.system_id, "location_id": run.scope.location_id},
        "job": {"system_id": run.system_id, "system": run.names.get(run.system_id),
                "activity_indices_used": dict(sorted(run.indices_used.items())),
                "facility_tax": run.facility_tax, "scc_surcharge": run.scc_surcharge,
                "material_multiplier": run.material_multiplier},
        "products": [{
            "product_id": target.type_id, "name": target.name,
            "blueprint_id": target.recipe.blueprint_id, "activity": target.recipe.activity,
            "alternatives": list(target.recipe.alternatives),
            "runs": target.plan.runs, "me": target.plan.me, "te": target.plan.te,
            "component_me": target.plan.component_me, "units": target.plan.units,
            "materials": [_component_doc(component, run.names)
                          for component in target.plan.components],
            "material_cost": target.plan.material_cost, "eiv": target.plan.eiv,
            "job_cost": target.plan.job_cost, "total": target.plan.total,
            "cost_per_unit": target.plan.cost_per_unit, "time_seconds": target.plan.time,
            "market": {"min_sell": figures.min_sell.get(target.type_id),
                       "max_buy": figures.max_buy.get(target.type_id)},
            "unpriced": _typed_docs(target.plan.unpriced, run.names),
            "eiv_missing": _typed_docs(target.plan.eiv_missing, run.names),
        } for target in run.targets],
        "figures": {"fetched": figures.fetched, "cached": figures.cached, "failed": figures.failed,
                    "last_modified": market.iso_utc(figures.meta.last_modified),
                    "expires": market.iso_utc(figures.meta.expires),
                    "age_seconds": None if figures.meta.last_modified is None
                    else round(run.now - figures.meta.last_modified, 1)},
        "warnings": list(run.warnings),
    }


def build_cost_csv(run: BuildRun) -> None:
    """Flat material rows on stdout; the notes go to stderr like every other CSV here.

    A name a run could not resolve is an empty cell rather than `id 34`, so that the column means one
    thing to whatever reads this afterwards."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(BUILD_COST_CSV_COLUMNS)
    for target in run.targets:
        plan = target.plan
        for component in plan.components:
            writer.writerow([target.type_id, target.name, target.recipe.blueprint_id,
                             target.recipe.activity, plan.runs, plan.me, plan.component_me, plan.te,
                             plan.units, component.type_id, run.names.get(component.type_id) or "",
                             component.required, render.csv_cell(component.buy_unit), render.csv_cell(component.buy_basis),
                             render.csv_cell(_build_unit_charge(component)), component.source,
                             render.csv_cell(component.forced), render.csv_cell(component.cost), component.surplus])
    sys.stdout.write(buf.getvalue())


def _build_notes_for_csv(run: BuildRun) -> list[str]:
    """Every prose line this run has to say, for the output format that may not print them as rows."""
    lines = list(_build_run_notes(run))
    for target in run.targets:
        lines += [f"{target.name}: {line}" for line in _build_product_notes(run, target)]
    return lines


def cmd_build_cost(args):
    """ISK cost of manufacturing an item from its blueprint, priced from live orders (public ESI)."""
    if args.build_all and args.buy_all:
        raise RuntimeError("--build-all and --buy-all push every material of the recipe in opposite "
                           "directions; pick one and override the few you mean with --build/--buy")
    facility_tax = _percent(args.facility_tax, "--facility-tax")
    # The caps `plan_build` enforces anyway, checked against its own constants before a single order
    # book is read: refusing ME 11 after twenty-six books have already cost their time is a bad
    # afternoon, and these limits belong to the flags rather than to whichever blueprint turns out to
    # be involved. A blueprint's own maximum runs per install still has to wait for the recipe.
    if args.runs < 1:
        raise RuntimeError(f"--runs is the number of installs in one job, so it has to be at least 1; "
                           f"got {args.runs}")
    if not 0 <= args.me <= industry.MAX_ME:
        raise RuntimeError(f"--me must be 0..{industry.MAX_ME} - that is as far as a blueprint can be "
                           f"researched, got {args.me}")
    if args.component_me is not None and not 0 <= args.component_me <= industry.MAX_ME:
        raise RuntimeError(f"--component-me must be 0..{industry.MAX_ME} - that is as far as a "
                           f"component blueprint can be researched, got {args.component_me}")
    if not 0 <= args.te <= industry.MAX_TE:
        raise RuntimeError(f"--te must be 0..{industry.MAX_TE} - that is as far as a blueprint can be "
                           f"researched, got {args.te}")
    client = esi_mod.Esi(esi_mod.default_user_agent(sso.load_config()))
    scope, system_id = build_scope(client, args)
    # Explicit forces are resolved before any book is read: being told a component's name was wrong
    # after forty order books would be a bad afternoon.
    force = _forced_names(client, args)
    index = _recipe_index()
    types = list(dict.fromkeys(market.resolve_type(client, spec) for spec in args.type))
    by_id = dict(types)
    recipes = [_recipe_for(index, type_id, name) for type_id, name in types]
    # The per-blueprint install limit is checked here rather than left to `plan_build`, which only
    # sees it after the prices it was handed: `--runs 2` on a one-run BPC is refusable knowledge the
    # moment the recipe is known, and paying for a fan-out first would buy nothing but the same error.
    for recipe in recipes:
        if 0 < recipe.max_runs < args.runs:
            raise RuntimeError(f"blueprint {recipe.blueprint_id} allows at most {recipe.max_runs} "
                               f"runs per install, asked for {args.runs} - install it as "
                               f"{-(-args.runs // recipe.max_runs)} jobs instead")
    # One fan-out covers the whole run: `pricing_ids` is exactly what `plan_build` will be asked to
    # price - product, direct materials, and one level below that - so no book is read for a figure no
    # total uses, and ten products cost one pass over the union rather than ten passes.
    wanted: set[int] = set()
    for recipe in recipes:
        wanted |= industry.pricing_ids(recipe, index)
    machine = bool(args.json or args.csv)

    def announce(info: market.Preflight) -> None:
        """What the fan-out is about to cost, said before it starts and never into a machine pipe."""
        if not machine:
            print(_build_preflight_notice(info), file=sys.stderr)

    figures = market.book_figures(client, sorted(wanted), scope, preflight=announce)
    # `/markets/prices` is not optional here the way it is for `market`: the install fee is charged on
    # EIV, and EIV is CCP's `adjusted_price`, which no order book carries. One request covers every
    # type in the cluster, so asking unconditionally is cheaper than arguing about who needs it.
    price_doc = market.price_table(client)
    adjusted: dict[int, float] = {}
    for type_id in wanted:
        row = price_doc.reference(type_id)
        # A published 0.0 stays a price - PLEX really is quoted at zero - and only absence means "no
        # basis", which is how `Prices.quote` comes to distinguish the two statements.
        if row is not None and row.adjusted_price is not None:
            adjusted[type_id] = row.adjusted_price
    prices = industry.Prices(unit=figures.min_sell, adjusted=adjusted)
    _expand_force(force, args, index, recipes, prices)

    indices = industry.cost_indices(client)
    # Names for everything a table or footnote can point at: the priced types, the system whose index
    # bills the job, and the scope's own region and station, which appear nowhere else.
    named = {system_id, scope.region_id, scope.location_id} | wanted
    names = esi_mod.resolve_names(client, {ident for ident in named if ident is not None})

    targets: list[BuildTarget] = []
    indices_used: dict[str, float] = {}
    for recipe in recipes:
        cost_index = indices.index(system_id, recipe.activity)
        if cost_index is None:
            raise RuntimeError(
                f"ESI publishes no {recipe.activity} cost index for system {system_id} "
                f"({names.get(system_id) or 'unnamed'}) - the install fee would come out as just the "
                f"tax and surcharge, which is not a cost. Name a system that has one with --system.")
        indices_used[recipe.activity] = cost_index
        facility = industry.Facility(cost_index=cost_index, facility_tax=facility_tax,
                                     material_multiplier=args.material_multiplier)
        plan = industry.plan_build(recipe, index, prices, facility, runs=args.runs, me=args.me,
                                   te=args.te, component_me=args.component_me, force=force)
        targets.append(BuildTarget(type_id=recipe.product_id, name=by_id[recipe.product_id],
                                   recipe=recipe, plan=plan, facility=facility))

    warnings = [_build_requests_note(figures)]
    rules = industry.rules_warning()
    if rules:
        warnings.append(rules)
    if figures.failed:
        warnings.append(f"{figures.failed} order book{'s' if figures.failed != 1 else ''} did not "
                        f"answer; their types are counted as unpriced, not as worthless")
    for target in targets:
        if target.plan.unpriced:
            warnings.append(f"{target.name}: {len(target.plan.unpriced)} material(s) with no price "
                            f"are excluded from every total rather than counted as free")
    run = BuildRun(now=client.now().timestamp(), scope=scope, system_id=system_id,
                   facility_tax=facility_tax, scc_surcharge=industry.SCC_SURCHARGE,
                   material_multiplier=args.material_multiplier, indices_used=indices_used,
                   index_meta=indices.meta, figures=figures, reference=price_doc, names=names,
                   targets=tuple(targets), rules=rules, warnings=tuple(warnings))
    if args.json:
        print(json.dumps(build_cost_json(run), indent=2))
    elif args.csv:
        build_cost_csv(run)
        for line in _build_notes_for_csv(run):   # the footnotes matter; they may not pollute a pipe
            print(line, file=sys.stderr)
    else:
        print(build_cost_text(run))
