"""Command-level integration: the real cli/exports handlers driven end-to-end against
the fake ESI transport and a temporary XDG tree (see tests/fake_esi.py).

Protects the seams unit tests cannot reach: token isolation between stored characters,
current live response shapes (standings as a bare list, asset rows with `type_id`),
machine-readable output contracts, and graceful degradation when one character fails
or never consented to a scope. No network, no real config, no secrets."""

from __future__ import annotations

import argparse
import csv
import json
import unittest

from eve_skills import cli

from tests.fake_esi import (
    ADA, SKILL_CAPPED, SKILL_NAV, SKILL_OMEGA_ONLY, SKILL_UNSTARTED, SKILL_WIDE, VELA, FakeEsiEnv,
)


class CommandTestCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.addCleanup(self.env.stop)

    def fail_character(self, char, status=403, error="consent revoked"):
        """Make every skills fetch for `char` fail the way live ESI does."""
        self.env.server.get(f"/characters/{char.character_id}/skills", error=(status, {"error": error}))


class SkillsCommandTests(CommandTestCase):
    def test_single_character_text_view_reports_state_queue_and_pending_completion(self):
        code, out, err = self.env.run(["skills", "--char", "Ada"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Ada Vane (id 91000001)", out)
        self.assertIn("Caldari", out)
        self.assertIn("clone grade: Caldari Alpha Clone", out)
        self.assertIn("Clone state: OMEGA", out)
        self.assertIn("TRAINING QUEUE (2)", out)
        # finished-but-unlogged queue item shows as pending, not lost
        self.assertIn("* completed training, not yet reflected by ESI", out)
        self.assertNotIn("Vela", out)  # --char isolates the other stored character

    def test_json_output_is_machine_readable_and_keeps_untrained_rows(self):
        code, out, _ = self.env.run(["skills", "--char", "Ada", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)  # a single document, not a list
        self.assertEqual(doc["character"]["id"], ADA.character_id)
        self.assertEqual(doc["character"]["name"], "Ada Vane")
        self.assertEqual(doc["character"]["race"], "Caldari")
        self.assertEqual(doc["clone_state"]["state"], "OMEGA")
        self.assertEqual(doc["totals"], {"total_sp": 6000000, "unallocated_sp": 120000})
        self.assertEqual([q["status"] for q in doc["queue"]], ["done", "training"])
        self.assertEqual([q["name"] for q in doc["queue"]], ["Wide Skill", "Navigation"])
        rows = {r["skill_id"]: r for r in doc["skills"]}
        self.assertTrue(rows[SKILL_WIDE]["pending_completion"])
        self.assertEqual(rows[SKILL_WIDE]["trained_level"], 5)      # queue completion applied
        self.assertFalse(rows[SKILL_NAV]["pending_completion"])
        self.assertIsNone(rows[SKILL_OMEGA_ONLY]["alpha_cap"])
        self.assertEqual(rows[SKILL_OMEGA_ONLY]["access"], "omega")
        self.assertEqual(rows[SKILL_UNSTARTED]["trained_level"], 0)  # L0 rows live only in --json

    def test_csv_covers_every_stored_character_and_skips_zero_rows(self):
        code, out, _ = self.env.run(["skills", "--csv"])
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(out.splitlines()))
        by_char = {(r["character_id"], r["skill_id"]) for r in rows}
        self.assertEqual(by_char, {
            ("91000001", "1003"), ("91000001", "1005"), ("91000001", "2000"),
            ("91000001", "1011"), ("91000002", "1003"), ("91000002", "1005"),
        })  # the never-trained prerequisite is excluded, exactly like the table view
        self.assertNotIn("0", {r["trained_level"] for r in rows})
        vela = next(r for r in rows if (r["character_id"], r["skill_id"]) == ("91000002", "1003"))
        self.assertEqual(vela["character_name"], "Vela Krinn")  # rows stay attributed correctly
        self.assertEqual(vela["restricted"], "1")               # live clamp survives serialization
        ada_wide = next(r for r in rows if (r["character_id"], r["skill_id"]) == ("91000001", "1005"))
        self.assertEqual(ada_wide["pending_completion"], "1")

    def test_two_characters_render_separate_blocks_in_name_order(self):
        code, out, err = self.env.run(["skills"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("=" * 72, out)
        self.assertIn("Clone state: OMEGA", out)   # Ada
        self.assertIn("Clone state: ALPHA", out)   # Vela's live clamp
        self.assertLess(out.index("Ada Vane"), out.index("Vela Krinn"))

    def test_one_failing_character_degrades_to_a_warning(self):
        self.fail_character(VELA)
        code, out, err = self.env.run(["skills"])
        self.assertEqual(code, 0)  # partial data is still a usable run
        self.assertIn("warning: skipped Vela Krinn: HTTP 403: consent revoked", err)
        self.assertIn("Ada Vane", out)
        self.assertNotIn("ALPHA", out)  # the failed character contributes no rows

    def test_all_characters_failing_is_a_failing_exit(self):
        self.fail_character(ADA, status=500, error="esi down")
        self.fail_character(VELA, status=500, error="esi down")
        code, _, err = self.env.run(["skills"])
        self.assertEqual(code, 1)
        self.assertIn("no character data could be fetched", err)


class SummaryCommandTests(CommandTestCase):
    def test_totals_and_states_across_characters(self):
        code, out, err = self.env.run(["summary"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Ada Vane", out)
        self.assertIn("Vela Krinn", out)
        self.assertIn("OMEGA (high)", out)
        self.assertIn("ALPHA (high)", out)
        self.assertIn("6.90M", out)    # 6.0M + 0.9M total SP
        self.assertIn("2h 30m", out)   # Ada's active item, from ESI-relative dates
        self.assertIn("2 characters", out)

    def test_summary_survives_one_failed_character(self):
        self.fail_character(VELA)
        code, out, err = self.env.run(["summary"])
        self.assertEqual(code, 0)
        self.assertIn("warning: skipped Vela Krinn", err)
        self.assertIn("1 characters", out)
        self.assertIn("6.00M", out)    # totals only count the character that answered


class AttributesCommandTests(CommandTestCase):
    def test_base_attributes_remaps_and_accelerator(self):
        code, out, err = self.env.run(["attributes"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Ada Vane (id 91000001)", out)
        self.assertIn("PER 23", out)
        self.assertIn("remaps available: 2", out)
        self.assertIn("last remap: 2025-03-01", out)
        self.assertIn("accelerator days left: 7", out)
        self.assertIn("Vela Krinn (id 91000002)", out)
        self.assertIn("last remap: never", out)  # ESI sends null, not an empty date

    def test_character_without_consent_degrades_to_a_hint(self):
        self.env.write_tokens([
            self.env.token_for(ADA),
            self.env.token_for(VELA, scopes=["publicData"]),
        ])
        code, out, _ = self.env.run(["attributes"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane (id 91000001)", out)
        self.assertIn("Vela Krinn: no skills consent", out)
        self.assertNotIn("Vela Krinn (id", out)  # hinted, not fetched


class StandingsCommandTests(CommandTestCase):
    def test_live_list_shape_is_named_sorted_and_formatted(self):
        self.env.install_standings()
        code, out, _ = self.env.run(["standings"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane (id 91000001)", out)
        self.assertIn("Agent Six", out)
        self.assertIn("+3.50", out)
        self.assertIn("-1.25", out)
        self.assertIn("id 500001", out)  # unresolvable faction id stays visible as an id
        self.assertLess(out.index("Agent Six"), out.index("Science and Trade Institute"))
        self.assertIn("login --scopes standings", out)  # Vela's missing-consent hint
        self.assertIn("'Vela Krinn'", out)

    def test_csv_stdout_stays_machine_readable(self):
        self.env.install_standings()
        code, out, err = self.env.run(["standings", "--csv"])
        self.assertEqual(code, 0)
        self.assertIn("no standings consent", err)   # hints go to stderr...
        self.assertNotIn("consent", out)             # ...never into the CSV stream
        rows = list(csv.DictReader(out.splitlines()))
        self.assertEqual(rows[0]["kind"], "agent")
        self.assertEqual([r["standing"] for r in rows], ["+3.50", "-1.25", "+0.00"])
        self.assertEqual(rows[2]["name"], "id 500001")


class JobsCommandTests(CommandTestCase):
    def test_active_and_finished_jobs(self):
        self.env.install_jobs()
        code, out, _ = self.env.run(["jobs"])
        self.assertEqual(code, 0)
        self.assertIn("manufacturing", out)
        self.assertIn("2/10", out)      # installed_runs/runs
        self.assertIn("left", out)      # active job counted down against ESI time
        self.assertIn("reaction", out)  # finished job activity
        self.assertIn("Tritanium", out)
        self.assertIn("no jobs consent", out)  # Vela hint

    def test_csv_rows_are_chronological_and_named(self):
        self.env.install_jobs()
        code, out, err = self.env.run(["jobs", "--csv"])
        self.assertEqual(code, 0)
        self.assertIn("no jobs consent", err)
        rows = list(csv.DictReader(out.splitlines()))
        self.assertEqual([r["status"] for r in rows], ["finished", "active"])
        self.assertEqual(rows[1]["product"], "Tritanium")
        self.assertEqual(rows[1]["runs"], "2/10")
        self.assertTrue(rows[1]["time"].endswith("left"))
        self.assertEqual(rows[0]["installed_in"], "Rens - Datauri")


class InventoryCommandTests(CommandTestCase):
    def test_summary_paginates_and_groups_current_asset_rows(self):
        self.env.install_inventory()
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane (3 asset rows)", out)
        self.assertIn("Jita - Mradd", out)
        self.assertIn("501", out)              # 500 units + 1 singleton in the Jita hangar
        self.assertIn("Keepstar Outpost", out)
        self.assertEqual(len(self.env.server.calls_to(f"/characters/{ADA.character_id}/assets")), 2)
        self.assertIn("no assets consent", out)  # Vela hint

    def test_csv_resolves_the_live_type_id_field(self):
        self.env.install_inventory()
        code, out, _ = self.env.run(["inventory", "--csv"])
        self.assertEqual(code, 0)
        rows = {r["item_id"]: r for r in csv.DictReader(out.splitlines())}
        self.assertEqual(rows["1001"]["item_name"], "Tritanium")   # via type_id, not retired typeID
        self.assertEqual(rows["1002"]["singleton"], "1")
        self.assertEqual(rows["1002"]["item_name"], "Caldari Ship Blueprint")
        self.assertEqual(rows["1003"]["location_name"], "Keepstar Outpost")
        self.assertTrue(all(r["character"] == "Ada Vane" for r in rows.values()))

    def test_items_mode_lists_every_asset(self):
        self.env.install_inventory()
        code, out, _ = self.env.run(["inventory", "--items"])
        self.assertEqual(code, 0)
        self.assertIn("Tritanium", out)
        self.assertIn("Caldari Ship Blueprint", out)


class TravelCommandTests(CommandTestCase):
    def test_location_home_and_jump_clones(self):
        self.env.install_travel()
        code, out, _ = self.env.run(["travel"])
        self.assertEqual(code, 0)
        self.assertIn("current: Jita - Mradd (The Forge)", out)
        self.assertIn("home: Jita - Mradd", out)
        self.assertIn("last clone jump: 2026-08-01 10:00 UTC", out)
        self.assertIn("Rens bolt-hole", out)
        self.assertIn("Memory Augmentation", out)
        self.assertIn("no clones consent", out)  # Vela hint

    def test_csv_kinds_per_location(self):
        self.env.install_travel()
        code, out, err = self.env.run(["travel", "--csv"])
        self.assertEqual(code, 0)
        self.assertIn("no clones consent", err)
        rows = list(csv.DictReader(out.splitlines()))
        self.assertEqual([r["kind"] for r in rows], ["current", "home", "jump clone"])
        jump = rows[2]
        self.assertEqual(jump["clone_name"], "Rens bolt-hole")
        self.assertEqual(jump["implants"], "Memory Augmentation")


class ImplantsCommandTests(CommandTestCase):
    def test_one_row_per_fitted_instance(self):
        self.env.install_implants()
        code, out, _ = self.env.run(["implants"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane (id 91000001)", out)
        self.assertEqual(out.count("Memory Augmentation"), 2)  # same type in both head slots
        self.assertIn("no clones consent", out)

    def test_csv_rows(self):
        self.env.install_implants()
        code, out, err = self.env.run(["implants", "--csv"])
        self.assertEqual(code, 0)
        self.assertIn("no clones consent", err)
        rows = list(csv.reader(out.splitlines()))
        self.assertEqual(rows[0], ["character", "implant"])
        self.assertEqual(rows[1:], [["Ada Vane", "Memory Augmentation"]] * 2)


class DispatchSurfaceTests(unittest.TestCase):
    """Every dispatchable command must be reachable from the parser.

    A handler without a subparser is invisible (`invalid choice`), a subparser without a
    handler is a KeyError the moment someone runs it."""

    def parser_commands(self) -> set[str]:
        actions = [a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertEqual(len(actions), 1, "expected exactly one subcommand group")
        return set(actions[0].choices)

    def test_parser_choices_and_handlers_agree(self):
        commands = self.parser_commands()
        self.assertEqual(commands - {"skills"}, set(cli.HANDLERS))
        for command in sorted(commands):
            with self.subTest(command=command), self.assertRaises(SystemExit) as caught:
                cli.main([command, "--help"])
            self.assertEqual(caught.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
