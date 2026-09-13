"""Plan building against the skill catalog: prerequisite closure, coverage, ordering, price.

Catalogs here are synthetic - ids, ranks and prerequisites invented below - so a failure
points at planner logic rather than at whatever CCP shipped. `PackagedCatalogTests` is the
single place where the bundled SDE snapshot itself is checked.
"""

from __future__ import annotations

import unittest

from eve_skills import alphadata, planner, sso
from eve_skills.alphadata import SkillInfo

from . import fake_esi


def skill(type_id: int, name: str, rank: int = 1, pre: dict[int, int] | None = None,
          published: bool = True) -> SkillInfo:
    return SkillInfo(type_id, name, rank, "perception", "willpower", dict(pre or {}), published)


# Diamond graph: Apex needs Mid Gate IV *and* Alpha Base IV, so a shared prerequisite has
# to be planned once, at the deepest level anyone asks for.
CATALOG = {
    s.type_id: s for s in (
        skill(1, "Alpha Base"),
        skill(2, "Beta Base"),
        skill(3, "Mid Gate", rank=2, pre={1: 2, 2: 3}),
        skill(4, "Apex", rank=5, pre={3: 4, 1: 4}),
        skill(5, "Broken Chain", pre={99: 1}),   # prerequisite with no catalog row
        skill(6, "Loop A", pre={7: 1}),
        skill(7, "Loop B", pre={6: 1}),
        skill(8, "Zero Rank", rank=0),           # CCP ships such rows; they cannot be priced
    )
}


def by_name(plan) -> dict[str, planner.PlanItem]:
    return {item.name: item for item in plan.items}


class BuildPlanTests(unittest.TestCase):
    def plan(self, targets: dict[int, int], trained=None, scheduled=None):
        return planner.build_plan(targets, CATALOG, dict(trained or {}), dict(scheduled or {}))

    def test_prerequisites_are_added_once_at_the_deepest_level_needed(self):
        plan = self.plan({3: 5, 4: 5})
        items = by_name(plan)
        self.assertEqual(sorted(items), ["Alpha Base", "Apex", "Beta Base", "Mid Gate"])
        self.assertEqual(items["Alpha Base"].to_level, 4)   # Mid Gate wanted II, Apex wants IV
        self.assertEqual({i.name for i in plan.items if i.requested}, {"Mid Gate", "Apex"})
        self.assertEqual(items["Mid Gate"].required_by, ("Apex",))
        self.assertEqual(items["Alpha Base"].required_by, ("Apex", "Mid Gate"))

    def test_cost_is_rank_based_from_the_level_training_resumes(self):
        plan = self.plan({4: 5})
        self.assertEqual({name: item.sp for name, item in by_name(plan).items()}, {
            "Alpha Base": 45_255,        # rank 1, L0->L4
            "Beta Base": 8_000,          # rank 1, L0->L3
            "Mid Gate": 90_510,          # rank 2 -> exactly twice the rank-1 ladder
            "Apex": 1_280_000,           # rank 5, L0->L5
        })
        self.assertEqual(plan.total_sp, 45_255 + 8_000 + 90_510 + 1_280_000)

    def test_every_prerequisite_is_ordered_before_the_skill_needing_it(self):
        order = [item.name for item in self.plan({4: 5}).items]
        for before, after in (("Alpha Base", "Mid Gate"), ("Beta Base", "Mid Gate"),
                              ("Mid Gate", "Apex"), ("Alpha Base", "Apex")):
            self.assertLess(order.index(before), order.index(after), f"{before} must precede {after}")

    def test_trained_levels_are_not_charged_again(self):
        plan = self.plan({4: 5}, trained={1: 4, 2: 3})
        self.assertEqual([item.name for item in plan.items], ["Mid Gate", "Apex"])
        self.assertEqual({c.name: c.level for c in plan.covered}, {"Alpha Base": 4, "Beta Base": 3})
        self.assertFalse(any(c.via_queue for c in plan.covered))

    def test_a_target_that_is_already_trained_needs_no_item(self):
        plan = self.plan({1: 3}, trained={1: 5})
        self.assertEqual(plan.items, [])
        self.assertEqual(plan.total_sp, 0)
        self.assertEqual([(c.name, c.level, c.target) for c in plan.covered], [("Alpha Base", 5, 3)])

    def test_levels_the_existing_queue_reaches_are_not_charged_either(self):
        covered = self.plan({1: 4}, trained={1: 2}, scheduled={1: 4})
        self.assertEqual(covered.items, [])
        self.assertTrue(covered.covered[0].via_queue, "the note must say the queue covers it")

        partial = self.plan({1: 5}, trained={1: 2}, scheduled={1: 4})
        item, = partial.items
        self.assertEqual((item.trained_level, item.from_level, item.to_level), (2, 4, 5))
        self.assertEqual(item.sp, planner.levels_sp(4, 5, 1))

    def test_an_unschedulable_queue_entry_promises_no_coverage(self):
        queue = [{"skill_id": 1, "finished_level": 5},                       # no dates: cannot train
                 {"skill_id": 2, "finished_level": 3, "start_date": "x", "finish_date": "y"},
                 {"skill_id": 2, "finished_level": 4, "start_date": "x", "finish_date": "y"}]
        self.assertEqual(planner.scheduled_levels(queue), {2: 4})            # deepest wins

    def test_cyclic_prerequisites_are_reported_instead_of_looped_on(self):
        with self.assertRaises(planner.PlanError) as caught:
            self.plan({6: 2})
        self.assertIn("Loop A -> Loop B -> Loop A", str(caught.exception))

    def test_a_prerequisite_the_catalog_does_not_know_is_reported(self):
        with self.assertRaises(planner.PlanError) as caught:
            self.plan({5: 1})
        message = str(caught.exception)
        self.assertIn("Broken Chain", message)
        self.assertIn("99", message)
        self.assertIn("update-data", message)

    def test_a_skill_without_a_training_multiplier_cannot_be_priced(self):
        with self.assertRaises(planner.PlanError) as caught:
            self.plan({8: 1})
        self.assertIn("Zero Rank", str(caught.exception))

    def test_target_levels_outside_the_game_are_rejected(self):
        for level in (0, 6):
            with self.assertRaises(planner.PlanError) as caught:
                self.plan({1: level})
            self.assertIn("1..5", str(caught.exception))


class PackagedCatalogTests(unittest.TestCase):
    """The snapshot shipped in eve_skills/data/ has to be usable without a download."""

    def test_bundled_catalog_resolves_prerequisites_and_ranks(self):
        catalog = alphadata.skill_catalog()
        self.assertGreater(len(catalog), 500, "the bundled catalog should cover the whole skill list")
        for info in catalog.values():
            for prerequisite in info.prerequisites:
                self.assertIn(prerequisite, catalog, f"{info.name} requires unknown skill {prerequisite}")
        named = {info.name.lower(): info for info in catalog.values()}
        # Ranks come from dogma 275; these two pin that the extraction reads the right attribute.
        self.assertEqual(named["navigation"].rank, 1)
        self.assertEqual(named["amarr titan"].rank, 16)
        self.assertEqual(named["astrogeology"].prerequisites.get(named["science"].type_id), 4)


class PlanCommandTests(unittest.TestCase):
    """`eve-skills plan` end to end against fake ESI plus the synthetic catalog."""

    def setUp(self):
        self.env = fake_esi.FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.env.install_skill_catalog()
        self.addCleanup(self.env.stop)

    def plan(self, *targets: str) -> tuple[int, str, str]:
        return self.env.run(["plan", "--char", "Ada Vane", "--rate", "5000", *targets])

    def row(self, out: str, name: str) -> str:
        return next(line for line in out.splitlines() if line.startswith(name))

    def test_a_skill_the_character_has_never_trained_is_planned_with_prerequisites(self):
        code, out, err = self.plan("Unseen Skill:2")
        self.assertEqual(0, code, err)
        self.assertIn("requested", self.row(out, "Unseen Skill"))
        navigation = self.row(out, "Navigation")          # L1 trained, L2 in the queue
        self.assertIn("for Unseen Skill", navigation)
        self.assertIn("L2*", navigation, "the queue's completion is where training resumes")
        self.assertIn("existing queue drains first", out)

    def test_coverage_is_explained_rather_than_priced(self):
        code, out, err = self.plan("Wide Skill:5")
        self.assertEqual(0, code, err)
        self.assertIn("0 item(s) to train", out)
        self.assertIn("Wide Skill is already trained to L5", out)
        self.assertIn("Capped Skill is already trained to L5", out)   # its III requirement

    def test_alpha_limits_are_reported_for_targets_and_prerequisites(self):
        code, out, err = self.plan("Omega Only Skill:5")
        self.assertEqual(0, code, err)
        self.assertIn("Unstarted Skill", self.row(out, "Unstarted Skill"))
        self.assertIn("Omega Only Skill is omega-only", out)

    def test_names_are_resolved_against_the_catalog_not_the_character(self):
        code, _, err = self.plan("no such skill")
        self.assertEqual(1, code)
        self.assertIn("no skill named 'no such skill'", err)
        self.assertIn("update-data", err)

    def test_an_ambiguous_name_is_refused_with_the_candidates(self):
        code, _, err = self.plan("skill")
        self.assertEqual(1, code)
        self.assertIn("is ambiguous", err)
        self.assertIn("Unseen Skill", err)

    def test_a_non_positive_rate_override_is_refused(self):
        # 0 previously fell through to calibration and negatives walked ready dates backwards.
        for value in ("0", "-100"):
            with self.subTest(rate=value):
                code, out, err = self.env.run(["plan", "--char", "Ada Vane", "--rate", value, "Wide Skill:5"])
                self.assertEqual(1, code)
                self.assertIn("--rate must be a positive SP/hour value", err)
                self.assertEqual("", out)

    # -- where the rate comes from ---------------------------------------------
    # Ada: PER 23 INT 21 MEM 20 CHA 19 WIL 22, omega. `Unseen Skill:2` spans two attribute
    # pairs - Navigation trains on intelligence/perception = (21 + 23/2) * 60 = 1,950 and
    # Unseen Skill itself on perception/willpower = (23 + 22/2) * 60 = 2,040.

    def plan_without_rate(self, *targets: str) -> tuple[int, str, str]:
        return self.env.run(["plan", "--char", "Ada Vane", *targets])

    def test_the_attributes_price_every_pair_in_one_plan(self):
        code, out, err = self.plan_without_rate("Unseen Skill:2")
        self.assertEqual(0, code, err)
        self.assertIn("rate: 1,950 SP/hour (character attributes: intelligence/perception)", out)
        self.assertIn("rate: 2,040 SP/hour (character attributes: perception/willpower)", out)
        # The live Navigation item measures ~428 SP/hour here; the old single calibrated figure
        # applied to every row priced Unseen Skill at over three hours instead of ~41 minutes.
        self.assertNotIn("live training item", out)
        self.assertNotIn("one rate is applied", out)
        # Per-row rates are the point: Navigation's 6,586 SP ride 1,950 (3h 22m) while Unseen
        # Skill's 1,414 ride 2,040 (41m). One calibrated figure made both rows wrong.
        self.assertIn("3h 22m", self.row(out, "Navigation"))
        self.assertIn("41m", self.row(out, "Unseen Skill"))

    def test_an_alpha_clone_trains_at_half_rate(self):
        # CCP support article 203217062: an alpha trains at 0.5 * (pri + sec/2) SP/minute, omega
        # at double. Vela is ALPHA by live clamp; PER 19 INT 23 MEM 21 CHA 20 WIL 18 give
        # Navigation (intelligence/perception) (23 + 19/2) * 60 / 2 = 975 and Unseen Skill
        # (perception/willpower) (19 + 18/2) * 60 / 2 = 840.
        code, out, err = self.env.run(["plan", "--char", "Vela Krinn", "Unseen Skill:2"])
        self.assertEqual(0, code, err)
        self.assertIn("rate: 975 SP/hour (character attributes, alpha half rate:"
                      " intelligence/perception)", out)
        self.assertIn("rate: 840 SP/hour (character attributes, alpha half rate:"
                      " perception/willpower)", out)

    def test_a_rate_override_still_beats_the_attributes(self):
        code, out, err = self.plan("Unseen Skill:2")   # `plan` passes --rate 5000
        self.assertEqual(0, code, err)
        self.assertIn("rate: 5,000 SP/hour (--rate override)", out)
        self.assertNotIn("character attributes", out)
        # one hand-picked number across two pairs is an estimate again, so the caveat keeps its job
        self.assertIn("one rate is applied to every row", out)

    def test_an_old_token_without_attributes_consent_falls_back_to_calibration(self):
        # A refresh token minted before esi-skills.read_skills.v1 joined the base scopes: the
        # plan must still price itself off the live training item, silently - missing optional
        # consent is never an error.
        old = [s for s in sso.SCOPES if s != "esi-skills.read_skills.v1"]
        self.env.write_tokens([self.env.token_for(fake_esi.ADA, old), self.env.token_for(fake_esi.VELA)])
        code, out, err = self.plan_without_rate("Unseen Skill:2")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("live training item", out)
        self.assertNotIn("character attributes", out)

    def test_the_missing_consent_is_named_when_nothing_else_can_price_the_plan(self):
        # Vela with an old token: no attributes consent, empty queue, no SP history. The refusal
        # has to name the one command that can fix it.
        old = [s for s in sso.SCOPES if s != "esi-skills.read_skills.v1"]
        self.env.write_tokens([self.env.token_for(fake_esi.ADA), self.env.token_for(fake_esi.VELA, old)])
        code, out, err = self.env.run(["plan", "--char", "Vela Krinn", "Unseen Skill:2"])
        self.assertEqual(1, code)
        self.assertIn("cannot estimate training rate", err)
        self.assertIn("eve-skills login --scopes attributes", err)
        self.assertIn("'Vela Krinn'", err)

    def test_a_refused_attributes_lookup_degrades_to_calibration(self):
        # Consent present but the endpoint refuses (403, not a retryable 5xx): warn on stderr and
        # price from the live item. Optional data must never fail the plan, and the fallback must
        # not be silent - the footer says which source priced the rows.
        self.env.set_attributes(fake_esi.ADA, error=(403, {"error": "Forbidden"}))
        code, out, err = self.plan_without_rate("Unseen Skill:2")
        self.assertEqual(0, code, err)
        self.assertIn("attributes lookup failed", err)
        self.assertIn("live training item", out)
        self.assertNotIn("character attributes", out)


if __name__ == "__main__":
    unittest.main()
