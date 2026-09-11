"""Command-level integration: the real cli/exports handlers driven end-to-end against
the fake ESI transport and a temporary XDG tree (see tests/fake_esi.py).

Protects the seams unit tests cannot reach: token isolation between stored characters,
current live response shapes (standings as a bare list, asset rows with `type_id`),
machine-readable output contracts, and graceful degradation when one character fails
or never consented to a scope. No network, no real config, no secrets."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from eve_skills import cli, sso

from tests.fake_esi import (
    ADA, CORP_SHARED, MARKET_BROKEN, MIRA, VELA, INV_CONTAINER_ITEM, INV_CITADEL_BLIND,
    INV_SHIP_ITEM, SKILL_CAPPED, SKILL_NAV, SKILL_OMEGA_ONLY, SKILL_UNSTARTED, SKILL_WIDE,
    FakeEsiEnv,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    """`inventory` named, placed and valued.

    The fixture (`fake_esi.install_inventory`) is built so every contrast the view has to get right
    is present at once: a type ESI prices by average, one only by its industry figure, one published
    at 0.0 and one with no price row; a named ship carrying a named container; two player structures,
    one nameable and one answering 403; and buy orders at two stations, so a hub scope can be shown
    to read only the hub's."""

    def setUp(self):
        super().setUp()
        self.env.install_inventory()

    def csv_rows(self, *argv) -> dict[str, dict]:
        code, out, _ = self.env.run(["inventory", "--csv", *argv])
        self.assertEqual(code, 0)
        return {r["item_id"]: r for r in csv.DictReader(out.splitlines())}

    def ids_asked_of_names(self) -> list[int]:
        """Every id this run posted to `/universe/names`, across all its batches."""
        return [ident for call in self.env.server.calls if call.path == "/universe/names"
                for ident in (call.json or [])]

    @staticmethod
    def table(lines: list[str]) -> tuple[str, list[str]]:
        """The header and data rows of the run's first table, located by its rule of dashes rather
        than a fixed offset - hints and per-owner headings move that offset around. The table ends
        at its blank line, so the footnotes below it are never mistaken for rows."""
        rule = next(i for i, line in enumerate(lines) if line.strip() and set(line) <= set("- "))
        body = []
        for line in lines[rule + 1:]:
            if not line.strip():
                break
            if not line.startswith("TOTAL"):
                body.append(line)
        return lines[rule - 1], body

    # -- the names this command exists for ------------------------------------

    def test_legacy_csv_columns_keep_their_order_and_meaning(self):
        """The nine columns an owner's spreadsheets already read, in the order they were published -
        `item_name` derived from `type_id`, because ESI retired the `typeID` field."""
        code, out, _ = self.env.run(["inventory", "--csv"])
        self.assertEqual(code, 0)
        header = out.splitlines()[0].split(",")
        self.assertEqual(header[:9], ["character", "item_id", "type_id", "item_name", "quantity",
                                      "singleton", "flag", "location_id", "location_name"])
        rows = {r["item_id"]: r for r in csv.DictReader(out.splitlines())}
        self.assertEqual(rows["1001"]["item_name"], "Tritanium")       # via type_id, not retired typeID
        self.assertEqual(rows["1002"]["item_name"], "Caldari Ship Blueprint")
        self.assertEqual(rows["1002"]["singleton"], "1")
        self.assertTrue(all(r["character"] == "Ada Vane" for r in rows.values()))

    def test_nothing_is_left_an_id_although_container_ids_overflow_int32(self):
        """The bug this view used to have: container and structure ids are item-sized, and ESI fails
        the *whole* `/universe/names` batch when one is posted - so mixing them in cost every name in
        the batch. The fake reproduces that 400, which makes 'every row is named' a real pin here."""
        rows = self.csv_rows()
        self.assertEqual(len(rows), 9)     # two asset pages, nothing dropped
        # Exactly one row may be a label - the structure ESI refused - and it must be that one.
        blind = f"structure {INV_CITADEL_BLIND}"
        unnamed = [r for r in rows.values() if r["location_name"] == blind]
        self.assertEqual(1, len(unnamed))
        for row in rows.values():
            self.assertNotRegex(row["item_name"], r"^type \d+$", "unresolved type name")
            self.assertTrue(row["location_name"], "row left with a bare location id")
            if row is not unnamed[0]:
                self.assertNotRegex(row["location_name"], r"^(location|structure|item) \d+$",
                                    f"{row['item_id']} fell back to a placeholder")
        # The nameable structure is named from /universe/structures, never guessed.
        self.assertEqual("Keepstar Outpost", rows["1006"]["location_name"])
        asked = self.ids_asked_of_names()
        self.assertTrue(asked, "station names never went through /universe/names")
        self.assertEqual([i for i in asked if not -2**31 <= i <= 2**31 - 1], [],
                         "an item-sized id was posted to /universe/names, which fails every name")

    def test_group_and_category_come_from_the_catalogue_not_a_guess(self):
        rows = self.csv_rows()
        self.assertEqual((rows["1001"]["group_name"], rows["1001"]["category_name"]),
                         ("Ore", "Material"))
        ship, container = rows[str(INV_SHIP_ITEM)], rows[str(INV_CONTAINER_ITEM)]
        self.assertEqual((ship["group_name"], ship["category_name"]), ("Frigate", "Ship"))
        self.assertEqual((container["group_name"], container["category_name"]),
                         ("Container", "Container"))
        # A custom name is its own column; the display name keeps both.
        self.assertEqual(ship["custom_name"], "Nightwatch")

    def test_nested_items_render_the_whole_path(self):
        """`location_id` alone cannot say which ship's cargo bay - and live ESI gives no other way."""
        rows = self.csv_rows()
        inside = rows["1003"]
        self.assertEqual(inside["location_name"], "Second Shift")
        self.assertEqual(inside["location_path"], "Jita - Mradd > Nightwatch > Second Shift")
        self.assertEqual(inside["location_kind"], "container")
        self.assertEqual(inside["flag"], "Cargo")   # `location_flag` from ESI, kept its CSV name
        code, out, _ = self.env.run(["inventory", "--items"])
        self.assertEqual(code, 0)
        self.assertIn("Jita - Mradd > Nightwatch > Second Shift", out)
        self.assertIn("Nightwatch (Rifter)", out)   # a singleton is named as its owner named it

    # -- grouping and totals --------------------------------------------------

    def test_summary_groups_by_place_with_a_subtotal_and_a_labelled_total(self):
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane (9 asset rows)", out)
        header, _ = self.table(out.splitlines())
        self.assertTrue(header.startswith("location"), header)
        self.assertEqual(2, len(self.env.server.calls_to(f"/characters/{ADA.character_id}/assets")))
        # The ship, its container and the container's contents are all at Jita, so they subtotal
        # together - 1,700 units of ore is 500 in the hangar plus 1,200 two levels down.
        self.assertIn("subtotal Jita - Mradd", out)
        self.assertIn("1,703", out)
        # The ISK covers only the priced rows, so the units in that sentence are the priced ones:
        # 2,558 held minus the blueprint's single unpriced unit.
        self.assertIn("TOTAL (ESI reference): 12,012,339.00 ISK over 2,557 units of 4 distinct "
                      "types; 1 more type held, none priced", out)
        self.assertIn("no assets consent", out)     # Vela hint, unchanged behaviour

    def test_by_category_transposes_the_same_numbers(self):
        code, by_location, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        code, out, _ = self.env.run(["inventory", "--by", "category"])
        self.assertEqual(code, 0)
        header, _ = self.table(out.splitlines())
        self.assertTrue(header.startswith("category"), header)   # swapped: the outer key leads
        self.assertIn("subtotal Material", out)
        self.assertIn("12,339.00", out)               # ore only; the ship is its own category
        self.assertIn("Jita - Mradd > Nightwatch > Second Shift", out)
        # Same holdings, same money, whichever way the table is turned.
        total = [line for line in out.splitlines() if line.startswith("TOTAL")][0]
        self.assertEqual(total, [line for line in by_location.splitlines() if line.startswith("TOTAL")][0])

    def test_items_are_ordered_by_value_and_unpriced_rows_last(self):
        code, out, _ = self.env.run(["inventory", "--items"])
        self.assertEqual(code, 0)
        _, body = self.table(out.splitlines())
        self.assertTrue(body[0].startswith("Nightwatch (Rifter)"), body[0])
        self.assertTrue(body[-1].startswith("Caldari Ship Blueprint"), body[-1])
        self.assertIn("-              -", body[-1])   # no price, not a free item
        # Descending within a type too: the ranking is by money, not by name or quantity.
        self.assertEqual(["5,844.00", "3,409.00", "2,435.00"],
                         [line.split()[-1] for line in body if line.startswith("Tritanium")])

    def test_unpriced_types_are_counted_named_and_left_out_of_every_total(self):
        """Three states have to stay distinct: priced, published at zero, and absent. Only the last
        one is missing, and saying so is the difference between a valuation and a guess."""
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn("no price on this basis, excluded from every total above (1): "
                      "Caldari Ship Blueprint", out)
        rows = self.csv_rows()
        self.assertEqual("", rows["1002"]["unit_price"])     # absent: no number at all
        self.assertEqual("0.00", f'{float(rows[str(INV_CONTAINER_ITEM)]["unit_price"]):.2f}')
        # The blueprint holds a row in the Jita group and adds nothing to its subtotal.
        _, body = self.table(out.splitlines())
        jita = [line for line in body if line.startswith("subtotal Jita - Mradd")][0]
        self.assertIn("12,008,279.00", jita)

    # -- valuation bases ------------------------------------------------------

    def test_reference_basis_is_one_request_and_says_what_it_is_not(self):
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertEqual(1, len(self.env.server.calls_to("/markets/prices")))
        self.assertIn("value basis: ESI's published reference price - a figure CCP publishes about "
                      "an item, not an order anybody will fill", out)
        self.assertIn("priced 4 of 5 distinct types held (1 request for ESI's whole price document)",
                      out)

    def test_value_at_a_hub_prices_at_that_station_only(self):
        code, out, _ = self.env.run(["inventory", "--items", "--value-at", "jita"])
        self.assertEqual(code, 0)
        # Jita's richest buy is 4.20; another station in the region pays 4.30 and must not be used.
        tritanium = [line for line in out.splitlines() if line.startswith("Tritanium")][0]
        self.assertIn("4.20", tritanium)
        self.assertNotIn("4.30", tritanium)
        self.assertIn("value basis: the richest standing buy order at Jita 4-4 - what dumping the "
                      "holding there pays right now", out)
        # Only ore and the blueprint have buy orders here, so the total covers 2,401 of 2,558 units.
        self.assertIn("TOTAL (max buy @ Jita 4-4): 10,083.10 ISK over 2,401 units of 2 distinct "
                      "types; 3 more types held, none priced", out)
        # The asks come out of the same rows, so they are free - but they reach types the buy side
        # does not, and a comparison against the TOTAL has to cover the TOTAL's own rows. Only
        # Tritanium is priced on both bases here (12,120.00); Pyerite has an ask and no bid, so it
        # belongs to neither figure and is counted out loud instead of inflating the alternative.
        self.assertIn("listing the same holdings at Jita 4-4's cheapest standing ask would raise "
                      "12,120.00 ISK over the 1 type both bases price", out)
        self.assertIn("1 further type is priced on that basis alone", out)
        self.assertIn("freshness: as of ", out)
        self.assertEqual(5, len(self.env.server.calls_to("/markets/10000002/orders")))
        self.assertEqual([], self.env.server.calls_to("/markets/prices"))

    def test_value_at_a_region_widens_past_the_station(self):
        code, out, _ = self.env.run(["inventory", "--items", "--value-at", "The Forge"])
        self.assertEqual(code, 0)
        tritanium = [line for line in out.splitlines() if line.startswith("Tritanium")][0]
        self.assertIn("4.30", tritanium)     # the region's richest buy, not Jita's
        self.assertIn("richest standing buy order at The Forge", out)

    def test_a_mistaken_value_scope_fails_before_any_asset_is_fetched(self):
        """A typo in `--value-at` must not cost a page of holdings per character first, and must say
        what to do about it instead of printing a traceback."""
        code, out, err = self.env.run(["inventory", "--value-at", "Nowhere"])
        self.assertEqual(code, 1)
        self.assertIn("no region named 'Nowhere'", err)
        self.assertEqual([], self.env.server.calls_to(f"/characters/{ADA.character_id}/assets"))
        code, _, err = self.env.run(["inventory", "--value-at", "  "])
        self.assertEqual(code, 1)
        self.assertIn("empty --value-at", err)

    def test_a_dead_shard_is_reported_as_missing_rather_than_worthless(self):
        code, out, _ = self.env.run(["inventory", "--value-at", str(MARKET_BROKEN)])
        self.assertEqual(code, 0)
        self.assertIn("nothing priced on this basis (5 distinct types held)", out)
        self.assertIn("did not answer; their types are counted as unpriced above, not as worthless",
                      out)

    # -- degradation ----------------------------------------------------------

    def test_a_structure_without_consent_is_labelled_once_and_says_how_to_fix_it(self):
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn(f"structure {INV_CITADEL_BLIND}", out)
        self.assertEqual(1, out.count("could not be resolved"),
                         "the notice repeated per row instead of once per run")
        self.assertIn("eve-skills login --scopes structures", out)

    def test_empty_inventory_says_so_without_inventing_a_value(self):
        self.env.server.get(f"/characters/{ADA.character_id}/assets", token=ADA.token, doc=[])
        code, out, err = self.env.run(["inventory", "--char", "Ada"])
        self.assertEqual(code, 0)
        self.assertIn("(inventory empty)", out)
        self.assertNotIn("value basis", out + err)     # nothing was valued; nothing to footnote
        self.assertEqual([], self.env.server.calls_to("/markets/prices"))

    def test_csv_keeps_hints_and_footnotes_off_stdout(self):
        code, out, err = self.env.run(["inventory", "--csv"])
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertNotIn("consent", line)
        self.assertIn("no assets consent", err)
        self.assertIn("value basis:", err)
        self.assertTrue(out.startswith("character,item_id,"))

    def test_one_refused_owner_warns_without_silencing_the_others(self):
        """Mira is stored with the consent and refused anyway (revoked server-side). Ada's holdings
        must still render, and the refusal must reach the user - as prose on stderr, as data in JSON."""
        self.env.write_tokens([
            self.env.token_for(ADA, sso.SCOPES + sso.scopes_for(["all"])),
            self.env.token_for(VELA),
            self.env.token_for(MIRA, sso.SCOPES + sso.scopes_for(["all"])),
        ])
        self.env.server.get(f"/characters/{MIRA.character_id}",
                            doc={"name": MIRA.name, "bloodline_id": 403,
                                 "corporation_id": CORP_SHARED})
        self.env.server.get(f"/characters/{MIRA.character_id}/assets", token=MIRA.token,
                            error=(403, {"error": "consent revoked"}))
        code, out, err = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn("Mira Solen: ESI refused", err)
        self.assertIn("Ada Vane (9 asset rows)", out)
        _, out, err = self.env.run(["inventory", "--json"])
        doc = json.loads(out)
        self.assertEqual([], [line for line in err.splitlines() if line.startswith("warning:")])
        self.assertEqual(1, len(doc["warnings"]))
        self.assertIn("Mira Solen", doc["warnings"][0])

    def test_a_row_priced_by_the_fallback_figure_says_so(self):
        # `average_price` and `adjusted_price` are different published numbers. Where a held type
        # has only the second, the total quietly mixes a trade average with CCP's industry
        # reference, so the run has to say how many rows that covers and mark them per row.
        code, out, _ = self.env.run(["inventory", "--char", "Ada"])
        self.assertEqual(code, 0)
        self.assertIn("had no published average price, so CCP's industry reference", out)
        code, csv_out, _ = self.env.run(["inventory", "--char", "Ada", "--csv"])
        rows = list(csv.DictReader(io.StringIO(csv_out)))
        bases = {row["item_name"]: row["price_basis"] for row in rows}
        self.assertEqual("esi_adjusted", bases["Rifter"])      # adjusted_price only
        self.assertEqual("esi_reference", bases["Tritanium"])  # has a published average

    def test_json_is_one_document_carrying_ids_and_names_together(self):
        code, out, err = self.env.run(["inventory", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual("", err)     # hints and footnotes are in the document, not beside it
        doc = json.loads(out)
        self.assertEqual("esi_reference", doc["value_basis"]["key"])
        self.assertEqual(["Caldari Ship Blueprint"], doc["value_basis"]["unpriced_types"])
        self.assertEqual(1, len(doc["hints"]))     # Vela, machine-readable as ever
        owner = doc["characters"][0]
        self.assertEqual(9, owner["asset_rows"])
        self.assertEqual({"types": 5, "units": 2558, "priced_units": 2557, "priced_types": 4,
                          "unpriced_types": 1},
                         {k: owner["totals"][k] for k in ("types", "units", "priced_units",
                                                          "priced_types", "unpriced_types")})
        item = [i for i in owner["items"] if i["item_id"] == 1003][0]
        self.assertEqual({"id": INV_CONTAINER_ITEM, "name": "Second Shift", "kind": "container"},
                         item["location_path"][-1])
        self.assertEqual("Jita - Mradd", item["location_path"][0]["name"])
        self.assertEqual(5844.0, item["value"])
        # Group cells are numbers plus names: a consumer never has to re-derive a label from an id.
        jita = [g for g in owner["groups"] if g["name"] == "Jita - Mradd"][0]
        self.assertEqual(12008279.0, jita["value"])
        # `unpriced_units` travels with the pair so a consumer can tell that a group's unit count
        # covers more than its ISK does - the blueprint entry holds one unit nothing priced.
        self.assertIn({"name": "Ship", "types": 1, "units": 1, "value": 12000000.0,
                       "unpriced_units": 0}, jita["entries"])
        self.assertIn({"name": "Blueprint", "types": 1, "units": 1, "value": None,
                       "unpriced_units": 1}, jita["entries"])

    def test_corporation_inventory_asks_the_corporation_endpoints(self):
        """A corporation run must ask about the corporation's items. Reusing the character endpoint
        would 404 on live ESI and cost every custom name in the batch."""
        self.env.install_corp_inventory()
        code, out, _ = self.env.run(["inventory", "--corp", "--items"])
        self.assertEqual(code, 0)
        self.assertIn("Ledger Runner (Rifter)", out)
        names = sorted({c.path for c in self.env.server.calls if c.path.endswith("assets/names")})
        self.assertEqual([f"/corporations/{CORP_SHARED}/assets/names"], names)
        self.assertEqual([f"/corporations/{CORP_SHARED}/assets"],
                         sorted({c.path for c in self.env.server.calls
                                 if c.path.endswith("/assets")}))


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


class ConsoleEncodingTests(unittest.TestCase):
    """A redirected stdout is not a console, and on Windows it encodes with the ANSI code page.

    Everything this tool writes is UTF-8 - the same bytes its files hold - so a character name
    outside cp1252 has to print anyway rather than abort the command after all its work was done.
    The condition is forced with ``PYTHONIOENCODING`` instead of skipped: cp1252 is available on
    every platform, and this is precisely what ``eve-skills events > out.txt`` is on Windows."""

    NAME = "ジェーガー・Ölvsson"     # invented; the CJK half has no byte in cp1252 at all

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-console-")
        self.addCleanup(self.tmp.cleanup)
        self.state_home = os.path.join(self.tmp.name, "state")
        state_dir = os.path.join(self.state_home, "eve-skills")
        os.makedirs(state_dir, exist_ok=True)
        row = {"id": "evt-1", "ts": 1_800_000_000.0, "kind": "training_finished",
               "character_id": ADA.character_id, "character_name": self.NAME,
               "skill_id": SKILL_NAV, "skill_name": "Navigation", "finished_level": 5}
        with open(os.path.join(state_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def run_cli(self, *args: str, io_encoding: str | None = None):
        env = {**os.environ, "PYTHONPATH": REPO_ROOT, "XDG_STATE_HOME": self.state_home,
               "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config")}
        for var in ("PYTHONUTF8", "PYTHONCOERCECLOCALE", "PYTHONIOENCODING"):
            env.pop(var, None)       # the child answers for its own default encoding
        if io_encoding:
            env["PYTHONIOENCODING"] = io_encoding
        return subprocess.run([sys.executable, "-m", "eve_skills", *args], env=env,
                              capture_output=True, timeout=120)

    def test_a_name_outside_the_console_code_page_still_prints(self):
        proc = self.run_cli("events", io_encoding="cp1252")
        self.assertEqual(0, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        # Decodes as UTF-8 and still names the character: no crash, no lossy replacement.
        self.assertIn(self.NAME, proc.stdout.decode("utf-8"))
        self.assertNotIn("\ufffd", proc.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
