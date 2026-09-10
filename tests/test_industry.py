"""Build-cost arithmetic: run quantities, job fees, build-or-buy per material, cost indices.

Recipes here are synthetic - blueprint and type ids invented below - so a failure points at the cost
model rather than at whatever CCP shipped in this week's SDE. Prices are plain dicts and the cost
index client is a stub returning one canned document: nothing in this module touches the network,
and `Prices` is deliberately the seam where live data gets handed over.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from eve_skills import esi, industry


DOC = {
    # The item being costed: one buy-only part plus 22 of a part that can be built.
    "800001": {"manufacturing": {"m": {"900500": 1, "900600": 22}, "p": ["900001", 1],
                                 "t": 12000, "limit": 300}},
    # Two more blueprints make the same product, so the winner has to be decided, not stumbled on.
    "800002": {"manufacturing": {"m": {"900500": 2}, "p": ["900001", 1], "t": 9000, "limit": 1}},
    "800003": {"manufacturing": {"m": {"900500": 3}, "p": ["900001", 2], "t": 9000, "limit": 0}},
    # A buildable component: 10 per run, out of a second component.
    "800601": {"manufacturing": {"m": {"900700": 100}, "p": ["900600", 10], "t": 1800, "limit": 50}},
    # ... which is itself buildable, and must stay bought: one level deep means one level.
    "800701": {"manufacturing": {"m": {"900800": 3}, "p": ["900700", 1], "t": 600, "limit": 10}},
    # A recipe that eats its own product; expanding it would build the thing to build the thing.
    "800101": {"manufacturing": {"m": {"900101": 5, "900700": 2}, "p": ["900101", 6],
                                 "t": 600, "limit": 4}},
    # A reaction: no ME, no TE.
    "800901": {"reaction": {"m": {"900500": 500}, "p": ["900002", 1], "t": 3600, "limit": 1}},
    # A product fed by that reaction, so a component job can be asked for on a recipe that cannot
    # be researched - which is the case `component_me` must not silently apply to.
    "800004": {"manufacturing": {"m": {"900002": 1}, "p": ["900003", 1], "t": 600, "limit": 10}},
}

INDEX = industry.recipe_index(DOC)
WIDGET = INDEX[900_001]          # 1x 900500 (buy only) + 22x 900600 (buildable)
REACTION = INDEX[900_002]        # 500x 900500, unresearchable
CYCLE = INDEX[900_101]           # 5x its own product + 2x 900700
REACTION_FED = INDEX[900_003]    # 1x 900002, which only a reaction can make

ADJUSTED = {900_500: 1000.0, 900_600: 18.0, 900_700: 3.5}
# Same adjusted prices, opposite conclusion for the buildable component: 20 a unit on the market
# beats ~47.50 building it, 120 a unit does not.
CHEAP_BUY = industry.Prices(unit={900_500: 1000.0, 900_600: 20.0, 900_700: 4.0}, adjusted=ADJUSTED)
CHEAP_BUILD = industry.Prices(unit={900_500: 1000.0, 900_600: 120.0, 900_700: 4.0}, adjusted=ADJUSTED)
FACILITY = industry.Facility(cost_index=0.1718)     # Jita 4-4 manufacturing, as measured


def component(plan, type_id: int) -> industry.Component:
    return next(row for row in plan.components if row.type_id == type_id)


class RequiredQuantityTests(unittest.TestCase):
    def test_no_research_and_no_rig_is_the_base_quantity(self):
        self.assertEqual(industry.required_quantity(30, 4), 120)
        self.assertEqual(industry.required_quantity(30, 4, multiplier=1.0), 120)

    def test_a_one_per_run_material_cannot_be_researched_away(self):
        # max(runs, ...) is the floor: at ME 10 a single-run job still needs the one unit, and a
        # 50-run job still needs one per run however much research it carries.
        self.assertEqual(industry.required_quantity(1, 1, me=10), 1)
        self.assertEqual(industry.required_quantity(1, 50, me=10), 50)

    def test_the_game_rounds_to_two_decimals_before_ceiling(self):
        # 7 units x 5 runs at ME 5 in a -3.75 % structure is 32.003125. The game charges 32; a naive
        # ceil of the raw product charges 33 on every job of this shape, which is what the round()
        # is for and why it must stay inside the ceil rather than beside it.
        self.assertEqual(industry.required_quantity(7, 5, me=5, multiplier=0.9625), 32)
        self.assertEqual(math.ceil(7 * 5 * (1 - 5 / 100) * 0.9625), 33)   # the wrong answer

    def test_a_rig_bonus_composes_with_me(self):
        # 2000 raw -> 1900 at ME 5 -> 1852.5 after the rig, rounded up to a whole unit.
        self.assertEqual(industry.required_quantity(200, 10, me=5, multiplier=0.975), 1853)


class JobTimeTests(unittest.TestCase):
    def test_time_scales_with_runs_and_research(self):
        self.assertEqual(industry.job_time(WIDGET, 1), 12_000)
        self.assertEqual(industry.job_time(WIDGET, 3), 36_000)
        self.assertEqual(industry.job_time(WIDGET, 3, te=20), 28_800)

    def test_a_reaction_takes_full_time_whatever_te_is_claimed(self):
        # Reactions have no time efficiency to research, so claiming it changes nothing.
        self.assertEqual(industry.job_time(REACTION, 2, te=20), 7_200)


class EstimatedItemValueTests(unittest.TestCase):
    def test_eiv_is_base_quantities_times_adjusted_price(self):
        # 1*1000 + 22*18 per run - CCP's industry figure, never its rolling average.
        self.assertEqual(industry.estimated_item_value(WIDGET, 1, ADJUSTED), (1396.0, ()))
        self.assertEqual(industry.estimated_item_value(WIDGET, 3, ADJUSTED), (4188.0, ()))

    def test_a_material_ccp_does_not_price_is_named_not_summed_as_zero(self):
        partial = {900_600: 18.0}
        self.assertEqual(industry.estimated_item_value(WIDGET, 1, partial), (396.0, (900_500,)))


class JobCostTests(unittest.TestCase):
    def test_the_fee_is_eiv_times_index_tax_and_surcharge(self):
        self.assertAlmostEqual(industry.job_cost(1396.0, FACILITY), 299.1628, places=9)

    def test_a_structure_that_bills_differently_replaces_only_its_own_terms(self):
        facility = industry.Facility(cost_index=0.0859, facility_tax=0.0, scc_surcharge=0.0)
        self.assertAlmostEqual(industry.job_cost(1000.0, facility), 85.9, places=9)


class RulesWarningTests(unittest.TestCase):
    def test_the_dated_rules_announce_themselves_once_old(self):
        measured = datetime.strptime(industry.RULES_MEASURED, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        self.assertIsNone(industry.rules_warning(measured))
        self.assertIsNotNone(industry.rules_warning(measured + timedelta(days=industry.STALE_AFTER_DAYS + 1)))


class RecipeIndexTests(unittest.TestCase):
    def test_the_lowest_blueprint_id_wins_and_the_others_are_listed(self):
        self.assertEqual(WIDGET.blueprint_id, 800_001)
        self.assertEqual(WIDGET.alternatives, (800_002, 800_003))
        self.assertEqual(WIDGET.product_qty, 1)
        self.assertEqual(WIDGET.max_runs, 300)
        # Ids are strings in the document and ints everywhere past the index.
        self.assertEqual(dict(WIDGET.materials), {900_500: 1, 900_600: 22})

    def test_a_reaction_is_not_researchable_and_a_manufacturing_run_is(self):
        self.assertTrue(WIDGET.researchable)
        self.assertFalse(REACTION.researchable)


class PlanBuildTests(unittest.TestCase):
    def test_the_cheaper_side_wins_for_each_material(self):
        bought = industry.plan_build(WIDGET, INDEX, CHEAP_BUY, FACILITY)
        self.assertEqual([(row.type_id, row.source) for row in bought.components],
                         [(900_600, "buy"), (900_500, "buy")])
        built = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY)
        self.assertEqual([(row.type_id, row.source) for row in built.components],
                         [(900_600, "build"), (900_500, "buy")])

    def test_forcing_overrides_the_cheaper_choice_either_way(self):
        forced_build = industry.plan_build(WIDGET, INDEX, CHEAP_BUY, FACILITY, force={900_600: "build"})
        self.assertEqual(component(forced_build, 900_600).source, "build")
        self.assertTrue(component(forced_build, 900_600).forced)
        # The unforced row keeps its own verdict and says so.
        self.assertFalse(component(forced_build, 900_500).forced)

        forced_buy = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY, force={900_600: "buy"})
        self.assertEqual(component(forced_buy, 900_600).source, "buy")
        self.assertTrue(component(forced_buy, 900_600).forced)
        self.assertAlmostEqual(forced_buy.material_cost, 1000.0 + 22 * 120.0, places=9)

    def test_a_built_component_is_charged_whole_runs(self):
        plan = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY)
        build = component(plan, 900_600).build
        self.assertEqual((build.runs, build.units, build.surplus), (3, 30, 8))
        # Three runs consume 270 of the sub-component - whole runs, and at the component level's own
        # ME 10, not the top job's ME 0. Not the 220 a fractional 2.2 runs would charge.
        self.assertAlmostEqual(build.material_cost, 270 * 4.0, places=9)
        self.assertAlmostEqual(build.eiv, 300 * 3.5, places=9)      # base quantities again
        self.assertEqual(build.time, 3 * 1800)
        # And the plan charges that whole job, surplus included.
        self.assertAlmostEqual(component(plan, 900_600).cost, build.total, places=9)
        self.assertEqual(component(plan, 900_600).surplus, 8)

    def test_each_level_bills_its_own_job(self):
        # Top blueprint researched, component blueprints unresearched: the quantity the top job eats
        # follows `me`, and the materials each component job eats follow `component_me`. One plan,
        # both numbers visible at once - which is the whole point of them being two.
        plan = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY, me=10, component_me=0)
        self.assertEqual((plan.me, plan.component_me), (10, 0))
        row = component(plan, 900_600)
        self.assertEqual(row.required, 20)                 # 22 * 0.9 rounded up
        self.assertEqual((row.build.runs, row.build.surplus), (2, 0))
        self.assertAlmostEqual(row.build.material_cost, 200 * 4.0, places=9)   # 100/run at ME 0
        # The other way round: nothing taken off the top job, and the component still runs at the cap.
        flipped = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY, me=0, component_me=10)
        self.assertEqual((flipped.me, flipped.component_me), (0, 10))
        self.assertEqual(component(flipped, 900_600).required, 22)
        self.assertAlmostEqual(component(flipped, 900_600).build.material_cost, 270 * 4.0, places=9)

    def test_components_run_at_the_cap_while_the_top_job_stays_unresearched(self):
        # Nobody asked for a level: the default is what an owned BPO carries, while the blueprint
        # being run - typically an invented copy - keeps its own unresearched quantities.
        plan = industry.plan_build(WIDGET, INDEX, CHEAP_BUILD, FACILITY)
        self.assertEqual((plan.me, plan.component_me), (0, industry.DEFAULT_COMPONENT_ME))
        row = component(plan, 900_600)
        self.assertEqual(row.required, 22)                 # ME 0 up here
        self.assertAlmostEqual(row.build.material_cost, 270 * 4.0, places=9)   # ME 10 down there

    def test_a_build_whose_own_material_cannot_be_priced_falls_back_to_buying(self):
        # Neither an ask nor a published figure for 900700: the build has no chargeable total, so
        # buying the component at 120 wins even though building it would have been far cheaper.
        prices = industry.Prices(unit={900_500: 1000.0, 900_600: 120.0},
                                 adjusted={900_500: 1000.0, 900_600: 18.0})
        plan = industry.plan_build(WIDGET, INDEX, prices, FACILITY)
        row = component(plan, 900_600)
        self.assertEqual(row.source, "buy")
        self.assertEqual(row.build.unpriced, (900_700,))   # the reason it was not chargeable
        self.assertAlmostEqual(row.cost, 22 * 120.0, places=9)

    def test_a_material_nothing_can_price_is_never_counted_as_free(self):
        prices = industry.Prices(unit={900_600: 20.0, 900_700: 4.0}, adjusted={900_600: 18.0, 900_700: 3.5})
        plan = industry.plan_build(WIDGET, INDEX, prices, FACILITY)
        row = component(plan, 900_500)
        self.assertEqual((row.source, row.unit_cost, row.cost), ("unpriced", None, None))
        self.assertEqual(plan.unpriced, (900_500,))
        # The totals are what could be priced - and the per-unit figure, the number somebody would
        # quote to another person, refuses to pretend nothing is missing.
        self.assertAlmostEqual(plan.material_cost, 22 * 20.0, places=9)
        self.assertIsNone(plan.cost_per_unit)

    def test_a_type_with_no_ask_is_priced_from_ccp_and_says_so(self):
        prices = industry.Prices(unit={900_600: 20.0, 900_700: 4.0}, adjusted=ADJUSTED)
        row = component(industry.plan_build(WIDGET, INDEX, prices, FACILITY), 900_500)
        self.assertEqual((row.buy_unit, row.buy_basis, row.source), (1000.0, "esi_adjusted", "buy"))

    def test_me_never_reduces_eiv(self):
        plain = industry.plan_build(WIDGET, INDEX, CHEAP_BUY, FACILITY)
        researched = industry.plan_build(WIDGET, INDEX, CHEAP_BUY, FACILITY, me=10)
        self.assertEqual(researched.eiv, plain.eiv)
        # ... while the goods consumed really do drop: 22 -> 20 of the component, ME-immune at 1.
        self.assertEqual(component(researched, 900_600).required, 20)
        self.assertEqual(component(researched, 900_500).required, 1)

    def test_an_unresearchable_recipe_ignores_me_and_te(self):
        prices = industry.Prices(unit={900_500: 10.0}, adjusted={900_500: 9.0})
        plan = industry.plan_build(REACTION, INDEX, prices, FACILITY, me=10, te=20)
        self.assertEqual((plan.me, plan.te), (0, 0))
        self.assertEqual(component(plan, 900_500).required, 500)
        self.assertEqual(plan.time, 3600)

    def test_a_reaction_component_takes_no_me_however_high_the_component_level_is(self):
        # The one blueprint for 900002 is a reaction, and there is no ME to research on it: the cap
        # asked for the other component jobs must not reach this row, where it would report 450 of
        # 900500 against the 500 the game really charges - a cheaper build that cannot be installed.
        prices = industry.Prices(unit={900_002: 700.0, 900_500: 1.0},
                                 adjusted={900_002: 600.0, 900_500: 1.0})
        plan = industry.plan_build(REACTION_FED, INDEX, prices, FACILITY, component_me=10)
        row = component(plan, 900_002)
        self.assertEqual((row.source, row.build.runs), ("build", 1))
        self.assertAlmostEqual(row.build.material_cost, 500 * 1.0, places=9)
        # The plan still records the level it was asked to use for components; whether a given one of
        # them can take it is decided row by row, exactly as `me` records the top job's own clamp.
        self.assertEqual(plan.component_me, 10)

    def test_a_recipe_is_never_expanded_into_itself(self):
        prices = industry.Prices(unit={900_101: 30.0, 900_700: 4.0},
                                 adjusted={900_101: 28.0, 900_700: 3.5})
        row = component(industry.plan_build(CYCLE, INDEX, prices, FACILITY), 900_101)
        self.assertIsNone(row.build)          # its own blueprint is not an input to itself
        self.assertEqual((row.source, row.cost), ("buy", 150.0))


class PricingIdsTests(unittest.TestCase):
    def test_the_product_its_direct_materials_and_one_more_level(self):
        # 900700 has to be known because building 900600 would consume it; 900800 belongs to a job
        # this model never runs, so asking for its price would buy nothing.
        self.assertEqual(industry.pricing_ids(WIDGET, INDEX), {900_001, 900_500, 900_600, 900_700})

    def test_a_material_that_is_the_product_contributes_no_further_types(self):
        self.assertEqual(industry.pricing_ids(CYCLE, INDEX), {900_101, 900_700, 900_800})


class IndicesClient:
    """One canned `/industry/systems` response, and a count of how often it was asked for."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[str] = []

    def get_meta(self, path, token=None, cache=True):
        self.calls.append(path)
        return self.rows, esi.Meta(expires=1_800_000_000.0, last_modified=1_799_996_400.0)


class CostIndicesTests(unittest.TestCase):
    def setUp(self):
        self.client = IndicesClient([
            {"solar_system_id": 30_000_142, "cost_indices": [
                {"activity": "manufacturing", "cost_index": 0.1718},
                {"activity": "reaction", "cost_index": 0.1665},
                {"activity": "invention", "cost_index": 0.1247}]},
            {"solar_system_id": 30_045_308,
             "cost_indices": [{"activity": "manufacturing", "cost_index": 0.0859}]},
        ])
        self.indices = industry.cost_indices(self.client)

    def test_one_request_folds_into_per_system_indices(self):
        self.assertEqual(self.client.calls, [industry.COST_INDEX_PATH])
        self.assertEqual(self.indices.index(30_000_142, "manufacturing"), 0.1718)
        self.assertEqual(self.indices.index(30_045_308, "manufacturing"), 0.0859)
        self.assertEqual(self.indices.meta.last_modified, 1_799_996_400.0)

    def test_a_system_or_activity_esi_said_nothing_about_is_missing_not_zero(self):
        self.assertIsNone(self.indices.index(30_045_308, "reaction"))
        self.assertIsNone(self.indices.index(99_999_999, "manufacturing"))


class ValidationTests(unittest.TestCase):
    def plan(self, facility=FACILITY, **kwargs):
        return industry.plan_build(WIDGET, INDEX, CHEAP_BUY, facility, **kwargs)

    def test_research_levels_beyond_the_cap_are_a_typo_not_an_efficiency(self):
        for bad in (-1, 11):
            with self.assertRaisesRegex(RuntimeError, "material efficiency"):
                self.plan(me=bad)
            with self.assertRaisesRegex(RuntimeError, "material efficiency"):
                self.plan(component_me=bad)
        for bad in (-1, 21):
            with self.assertRaisesRegex(RuntimeError, "time efficiency"):
                self.plan(te=bad)

    def test_a_job_needs_at_least_one_run(self):
        with self.assertRaisesRegex(RuntimeError, "at least one run"):
            self.plan(runs=0)
        with self.assertRaisesRegex(RuntimeError, "at least one run"):
            industry.required_quantity(1, 0)

    def test_runs_above_the_install_limit_are_refused_and_say_how_to_split(self):
        with self.assertRaisesRegex(RuntimeError, "300 runs per install"):
            self.plan(runs=301)
        # maxProductionLimit 0 means the SDE states no limit, not a limit of zero.
        unlimited = industry.Recipe(blueprint_id=800_003, activity="manufacturing", product_id=900_001,
                                    product_qty=2, time=9000, max_runs=0, materials={900_500: 3},
                                    alternatives=())
        self.assertEqual(industry.plan_build(unlimited, INDEX, CHEAP_BUY, FACILITY, runs=1000).units, 2000)

    def test_a_forced_material_has_to_say_buy_or_build(self):
        with self.assertRaisesRegex(RuntimeError, "'build' or 'buy'"):
            self.plan(force={900_600: "make"})

    def test_a_material_multiplier_of_nothing_is_not_a_bonus(self):
        for bad in (0.0, -1.0):
            with self.assertRaisesRegex(RuntimeError, "material multiplier"):
                self.plan(facility=industry.Facility(cost_index=0.1718, material_multiplier=bad))
            with self.assertRaisesRegex(RuntimeError, "material multiplier"):
                industry.required_quantity(10, 1, multiplier=bad)


if __name__ == "__main__":
    unittest.main()
