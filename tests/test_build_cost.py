"""`build-cost` end to end: the cheapest-option table, its two machine formats, what it costs in
requests, and what it refuses to answer.

Runs against tests/fake_esi.py's in-process ESI with a four-blueprint world seeded into
$XDG_DATA_HOME, so the recipes are known exactly and every ISK figure below is written out by hand
from those rows (see the build-cost universe in fake_esi.py). The arithmetic itself - material
multipliers, EIV, whole-run charging, forced substitution - is pinned in tests/test_industry.py;
what this file protects is the seams around it: that the command reads one book per type and then
none, that a published reference price stays distinguishable from a buyable ask, that an unpriceable
material is excluded rather than counted as free, and that refusals happen before anything is asked.
"""

from __future__ import annotations

import csv
import io
import json
import re
import unittest
from tests.fake_esi import (
    BUILD_ADDON, BUILD_ALLOY, BUILD_ARTICLE, BUILD_CELL, BUILD_CRYO, BUILD_GOO, BUILD_HOUSING,
    BUILD_PASTE, BUILD_PLATE, MARKET_FORGE, FakeEsiEnv,
)

# The types a one-level quote for the article needs: the product itself, its four materials, and the
# two inputs of the plate's own blueprint. `Vacuum Sealed Paste` has no order anywhere but is still
# priced from ESI's reference document, so its book is read too.
ARTICLE_TYPES = {BUILD_ARTICLE, BUILD_PLATE, BUILD_HOUSING, BUILD_CELL, BUILD_PASTE, BUILD_CRYO,
                 BUILD_GOO}


class BuildCostTestCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_build_cost()   # ids, names, books with Expires, prices, cost indices, recipes
        self.addCleanup(self.env.stop)

    def books(self) -> list[int]:
        """The type ids whose regional book was actually read, in call order."""
        path = f"/markets/{MARKET_FORGE}/orders"
        return [int(c.query["type_id"]) for c in self.env.server.calls_to(path)]

    def row(self, out: str, name: str) -> list[str]:
        """One material's table line as its six cells: qty, buy/u, build/u, source, cost, surplus.

        Split after the name because the names themselves contain spaces; this is what lets a test
        say "the ask column reads 40.00" without matching it inside another column's total."""
        lines = [line for line in out.splitlines() if line.startswith(name + " ")]
        self.assertEqual(len(lines), 1, f"expected exactly one table row for {name}: {lines}")
        return lines[0][len(name):].split()

    def json_doc(self, argv: list[str]) -> dict:
        code, out, err = self.env.run(["build-cost", *argv, "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def material(self, product: dict, type_id: int) -> dict:
        matches = [m for m in product["materials"] if m["type_id"] == type_id]
        self.assertEqual(len(matches), 1, product["materials"])
        return matches[0]


class TableTests(BuildCostTestCase):
    def test_every_material_is_priced_and_the_cheaper_option_is_charged(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget"])
        self.assertEqual(code, 0)
        self.assertIn("pricing 7 distinct types for this build: 7 order books to read, one per type", err)
        self.assertIn("Benchwork Widget (id 920001) - 1 unit from 1 run, at ME 0 / TE 0, "
                      "components at ME 10, blueprint 930001", out)

        # Bulk Casing: 10 x 7.00 bought, and no blueprint makes it - the dash means "no build
        # option", never "free".
        self.assertEqual(self.row(out, "Bulk Casing"), ["10", "7.00", "-", "buy", "70.00", "0"])
        # Batch Cell: five wanted cost 45.00 at ask, but covering them means a whole run of ten -
        # 45.00 of input at the component level's ME plus its fee, so 55.00 over the five needed.
        self.assertEqual(self.row(out, "Batch Cell"), ["5", "9.00", "11.00", "buy", "45.00", "0"])
        # Widget Plate: 40.00 asked against 28.00 built, so this is the row that builds. The ask has
        # to be the hub's own - a cheaper 20.00 sits at another station in the same region.
        self.assertEqual(self.row(out, "Widget Plate"), ["4", "40.00", "28.00", "build", "112.00", "0"])
        # Vacuum Sealed Paste: nobody orders it here, so its unit price is ESI's published reference.
        self.assertEqual(self.row(out, "Vacuum Sealed Paste"), ["2", "20.00", "-", "buy", "40.00", "0"])

        # Biggest line first: the row that dominates the bill should be the first one read.
        positions = [out.index(name) for name in ("Bulk Casing", "Batch Cell", "Widget Plate",
                                                  "Vacuum Sealed Paste")]
        self.assertEqual(positions, sorted(positions))

        self.assertIn("  material cost   267.00 ISK", out)      # 70 + 45 + 112 + 40
        self.assertIn("  EIV             260.00 ISK", out)      # base quantities, ME not applied
        self.assertIn("  total           319.00 ISK", out)
        self.assertIn("  cost per unit   319.00 ISK", out)
        self.assertIn("  job time        1h 00m", out)
        # The comparison is measured against the same scope's book, on both sides.
        self.assertIn("buy instead: cheapest ask 250.00 ISK, richest bid 90.00 ISK at Jita 4-4 (station)", out)
        self.assertIn("  buying is cheaper by 69.00 ISK for 1 unit (build 319.00 vs buy 250.00)", out)
        # A build option that yields ten while five were wanted leaves surplus, and the table says so
        # instead of letting the reader assume the run was sized to the recipe.
        self.assertIn("whole runs", out)
        self.assertIn("real surplus behind", out)

    def test_the_install_fee_is_the_printed_eiv_times_the_printed_rates(self):
        code, out, _ = self.env.run(["build-cost", "Benchwork Widget"])
        self.assertEqual(code, 0)
        # The fee line shows its own arithmetic. The point here is that the two halves agree: they
        # only do if the fee is charged on EIV (not on material cost) and with all three rates -
        # index, facility tax and SCC surcharge.
        fee = re.search(r"job cost\s+([\d,.]+) ISK  = EIV x \(([\d.]+) cost index \+ ([\d.]+) "
                        r"facility tax \+ ([\d.]+) SCC surcharge\)", out)
        self.assertIsNotNone(fee, out)
        charged, index, tax, surcharge = (float(g.replace(",", "")) for g in fee.groups())
        eiv = float(re.search(r"EIV\s+([\d,.]+) ISK", out).group(1).replace(",", ""))
        self.assertEqual(index, 0.1575)     # what /industry/systems published for this system
        self.assertEqual((tax, surcharge), (0.0025, 0.04))
        self.assertEqual(charged, round(eiv * (index + tax + surcharge), 2))

    def test_a_named_system_bills_the_install_without_moving_the_shopping(self):
        code, out, _ = self.env.run(["build-cost", "Benchwork Widget", "--system", "Amarr"])
        self.assertEqual(code, 0)
        # Amarr bills manufacturing higher, so the fee rises...
        self.assertIn("  job cost        63.05 ISK  = EIV x (0.2000 cost index + 0.0025 facility tax "
                      "+ 0.0400 SCC surcharge)", out)
        # ...and it rises on the component jobs too, which is what moves the material cost as well:
        # one system builds the whole tree.
        self.assertIn("  material cost   271.25 ISK", out)
        self.assertIn("  total           334.30 ISK", out)
        # But --system prices the install only; buying still happens where the scope says.
        self.assertIn("scope: Jita 4-4 (station)", out)
        self.assertIn("cost index: manufacturing 0.2000 in Amarr (30002187)", out)
        self.assertEqual({c.path for c in self.env.server.calls if "/orders" in c.path},
                         {f"/markets/{MARKET_FORGE}/orders"})


class MachineOutputTests(BuildCostTestCase):
    def test_json_keeps_the_losing_build_option_visible(self):
        doc = self.json_doc(["Benchwork Widget"])
        product = doc["products"][0]
        self.assertEqual(product["blueprint_id"], 930001)
        self.assertEqual((product["runs"], product["units"]), (1, 1))
        self.assertEqual((product["material_cost"], product["eiv"], product["job_cost"],
                          product["total"], product["cost_per_unit"]),
                         (267.0, 260.0, 52.0, 319.0, 319.0))

        # Batch Cell is bought, and the report still carries the build that was turned down -
        # including the five spares one run leaves - because that is the answer to "what if?".
        cell = self.material(product, BUILD_CELL)
        self.assertEqual((cell["source"], cell["cost"]), ("buy", 45.0))
        self.assertEqual({k: cell["build"][k] for k in ("blueprint_id", "runs", "units", "surplus",
                                                        "material_cost", "job_cost", "total")},
                         {"blueprint_id": 930030, "runs": 1, "units": 10, "surplus": 5,
                          "material_cost": 45.0, "job_cost": 10.0, "total": 55.0})
        # `build.unit` is per unit *produced* (55/10); the table prints cost per unit *needed* (55/5).
        self.assertEqual(cell["build"]["unit"], 5.5)
        # No blueprint makes Bulk Casing, so there is no option to report - null, not a zero build.
        self.assertIsNone(self.material(product, BUILD_HOUSING)["build"])
        # The two price bases stay distinguishable: an ask you can hit, and a published figure.
        self.assertEqual(self.material(product, BUILD_PLATE)["buy"],
                         {"unit": 40.0, "basis": "min_sell"})
        self.assertEqual(self.material(product, BUILD_PASTE)["buy"],
                         {"unit": 20.0, "basis": "esi_adjusted"})
        self.assertEqual((product["market"]["min_sell"], product["market"]["max_buy"]), (250.0, 90.0))
        # This run priced everything from the network; a parser can tell that from a cached answer.
        self.assertEqual({k: doc["figures"][k] for k in ("fetched", "cached", "failed")},
                         {"fetched": 7, "cached": 0, "failed": 0})

    def test_csv_is_one_row_per_material_with_the_notes_on_stderr(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget", "--csv"])
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(list(rows[0]), ["product_id", "product_name", "blueprint_id", "activity",
                                         "runs", "me", "component_me", "te", "units", "type_id",
                                         "material_name", "required", "buy_unit", "buy_basis",
                                         "build_unit", "source", "forced", "cost", "surplus"])
        self.assertEqual([int(r["type_id"]) for r in rows],
                         [BUILD_HOUSING, BUILD_CELL, BUILD_PLATE, BUILD_PASTE])
        by_type = {int(r["type_id"]): r for r in rows}
        self.assertEqual(float(by_type[BUILD_CELL]["build_unit"]), 11.0)   # per unit needed
        self.assertEqual(by_type[BUILD_PLATE]["source"], "build")
        self.assertEqual(float(by_type[BUILD_PLATE]["cost"]), 112.0)
        self.assertEqual(by_type[BUILD_PASTE]["buy_basis"], "esi_adjusted")
        # No build option is an empty field, not a fabricated zero; nothing here was forced.
        self.assertEqual(by_type[BUILD_HOUSING]["build_unit"], "")
        self.assertEqual(by_type[BUILD_HOUSING]["source"], "buy")
        self.assertEqual({r["forced"] for r in rows}, {"0"})
        # Everything that explains the numbers is prose, so it stays off the parsed stream.
        for note in ("scope:", "requests:", "price basis:"):
            self.assertNotIn(note, out)
            self.assertIn(note, err)

    def test_forcing_a_component_charges_whole_runs_and_reports_surplus(self):
        code, out, _ = self.env.run(["build-cost", "Benchwork Widget", "--build", "Batch Cell"])
        self.assertEqual(code, 0)
        # Five cells wanted, one run of ten: the row now buys nothing and carries the five spares.
        self.assertEqual(self.row(out, "Batch Cell"), ["5", "9.00", "11.00", "build", "55.00", "5"])
        self.assertIn("  material cost   277.00 ISK", out)
        self.assertIn("  total           329.00 ISK", out)
        # Forcing the dearer option has to show up in the buy-vs-build verdict too.
        self.assertIn("  buying is cheaper by 79.00 ISK for 1 unit (build 329.00 vs buy 250.00)", out)
        forced = self.material(self.json_doc(["Benchwork Widget", "--build", "Batch Cell"])
                               ["products"][0], BUILD_CELL)
        self.assertTrue(forced["forced"])

        # --buy-all is the other end of the lever: the plate's ask (4 x 40.00) replaces its build.
        _, bought, _ = self.env.run(["build-cost", "Benchwork Widget", "--buy-all"])
        self.assertEqual(self.row(bought, "Widget Plate"), ["4", "40.00", "28.00", "buy", "160.00", "0"])
        self.assertIn("  total           367.00 ISK", bought)

        # --build-all builds what it can and leaves the rest bought without complaining: two of the
        # four materials have no blueprint at all, which is a fact about the SDE, not a bad request.
        _, built, _ = self.env.run(["build-cost", "Benchwork Widget", "--build-all"])
        self.assertEqual(self.row(built, "Bulk Casing"), ["10", "7.00", "-", "buy", "70.00", "0"])
        self.assertEqual(self.row(built, "Widget Plate"), ["4", "40.00", "28.00", "build", "112.00", "0"])
        self.assertIn("  total           329.00 ISK", built)


class RequestTests(BuildCostTestCase):
    def test_a_cold_run_reads_one_book_per_type_and_a_warm_rerun_reads_none(self):
        code, _, err = self.env.run(["build-cost", "Benchwork Widget"])
        self.assertEqual(code, 0)
        # One book per type - not per material line, and not the whole region: the product is priced
        # too, since that is what the comparison at the bottom is measured against.
        self.assertEqual(set(self.books()), ARTICLE_TYPES)
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 1)
        self.assertEqual(len(self.env.server.calls_to("/industry/systems")), 1)

        cold = self.books()
        doc = self.json_doc(["Benchwork Widget"])
        # The books carry a live `Expires`, so the quote cache is licensed to answer the second run.
        self.assertEqual(self.books(), cold)
        self.assertEqual({k: doc["figures"][k] for k in ("fetched", "cached")},
                         {"fetched": 0, "cached": 7})
        self.assertIn("no order-book request: all 7 types came from the local quote cache",
                      "\n".join(doc["warnings"]))

    def test_two_products_share_one_fan_out(self):
        code, _, _ = self.env.run(["build-cost", "Benchwork Widget", "Salvage Sampler"])
        self.assertEqual(code, 0)
        # Bulk Casing is in both recipes; reading it twice would price one bill from two moments.
        self.assertEqual(set(self.books()), ARTICLE_TYPES | {BUILD_ADDON, BUILD_ALLOY})
        self.assertEqual(self.books().count(BUILD_HOUSING), 1)

    def test_an_unpriceable_material_is_named_and_kept_out_of_every_total(self):
        code, out, _ = self.env.run(["build-cost", "Salvage Sampler"])
        self.assertEqual(code, 0)
        # No ask and no published figure: the row is present and says so, in both cost columns.
        self.assertEqual(self.row(out, "Unquoteable Alloy"), ["6", "-", "-", "unpriced", "-", "0"])
        self.assertIn("  material cost   21.00 ISK", out)
        self.assertIn("  EIV             18.00 ISK", out)       # three casings, nothing invented
        self.assertIn("  total           24.60 ISK", out)
        # A per-unit figure would be a number with a hole in it, so it is withheld instead.
        self.assertIn("  cost per unit   -", out)
        self.assertIn("(not stated while a material has no price - see the note below)", out)
        self.assertIn("no sell order there and no published price, so excluded from every total above "
                      "rather than counted as free", out)
        self.assertIn("the install fee understates its share", out)
        # A buy price does exist for this product, so a reader might expect buy-vs-build; weighing
        # them would be arithmetic with a missing term.
        self.assertIn("with a material unpriced the two are not comparable", out)

        doc = self.json_doc(["Salvage Sampler"])
        product = doc["products"][0]
        self.assertIsNone(product["cost_per_unit"])
        self.assertEqual([u["type_id"] for u in product["unpriced"]], [BUILD_ALLOY])
        self.assertEqual([u["name"] for u in product["eiv_missing"]], ["Unquoteable Alloy"])
        # The JSON has no prose notes, but a parser summing `total` still has to learn it is short.
        self.assertIn("Salvage Sampler: 1 material(s) with no price are excluded from every total "
                      "rather than counted as free", doc["warnings"])


class RefusalTests(BuildCostTestCase):
    def test_contradictory_flags_are_refused_before_anything_is_asked(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget", "--build-all", "--buy-all"])
        self.assertEqual(code, 1)
        self.assertIn("--build-all", err)
        self.assertIn("--buy-all", err)
        # The contradiction is in the arguments, so ESI must not have been contacted at all.
        self.assertEqual(self.env.server.calls, [])

    def test_a_component_level_beyond_the_cap_is_refused_before_anything_is_asked(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget", "--component-me", "11"])
        self.assertEqual(code, 1)
        self.assertIn("--component-me", err)
        # Same rule as --me: a typo costs nothing when it is caught before the order books.
        self.assertEqual(self.env.server.calls, [])

    def test_a_type_no_blueprint_makes_is_refused_without_reading_books(self):
        code, out, err = self.env.run(["build-cost", "Common Ore"])
        self.assertEqual(code, 1)
        self.assertIn("no blueprint in the local SDE data makes Common Ore (type 920060)", err)
        # Raw materials and research outputs have no build cost; pointing at update-data is the only
        # useful advice left when an outdated snapshot really is the reason.
        self.assertIn("eve-skills update-data", err)
        self.assertEqual(self.books(), [])

    def test_a_region_scope_without_a_system_names_the_flag_it_needs(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget", "--region", "The Forge"])
        self.assertEqual(code, 1)
        # A region holds as many cost indices as it has systems; picking one would invent the fee.
        self.assertIn("--system", err)
        self.assertEqual(self.books(), [])

    def test_a_system_with_no_published_index_is_refused(self):
        code, out, err = self.env.run(["build-cost", "Benchwork Widget", "--system",
                                       "Unindexed System"])
        self.assertEqual(code, 1)
        self.assertIn("no manufacturing cost index for system 30045281", err)
        # Billing tax plus surcharge alone would print a fee that is not a fee.
        self.assertIn("just the tax and surcharge", err)
        self.assertIn("--system", err)


class ComponentMeTests(BuildCostTestCase):
    """`--component-me`: one level for the blueprint run, another for the jobs feeding it."""

    def test_the_default_component_level_is_what_makes_the_cheaper_build(self):
        # Nothing asked for a level, and the component jobs still run at the cap: that is the whole
        # difference between 319.00 and the 327.00 an unresearched component blueprint would cost.
        _, default, _ = self.env.run(["build-cost", "Benchwork Widget"])
        self.assertIn("components at ME 10", default)
        self.assertIn("  total           319.00 ISK", default)
        _, flat, _ = self.env.run(["build-cost", "Benchwork Widget", "--component-me", "0"])
        self.assertIn("components at ME 0", flat)
        self.assertIn("  total           327.00 ISK", flat)

    def test_the_two_levels_print_and_record_separately(self):
        # --me still belongs to the top blueprint alone: asking for ME 3 on the components leaves the
        # product's own `me` at 0, and moves only the plate's own materials (78 cryo instead of 72).
        doc = self.json_doc(["Benchwork Widget", "--component-me", "3"])["products"][0]
        self.assertEqual((doc["me"], doc["te"], doc["component_me"]), (0, 0, 3))
        self.assertEqual(self.material(doc, BUILD_PLATE)["build"]["material_cost"], 98.0)
        self.assertEqual(doc["total"], 325.0)

    def test_the_component_level_is_a_csv_column_beside_the_top_one(self):
        code, out, _ = self.env.run(["build-cost", "Benchwork Widget", "--component-me", "0", "--csv"])
        self.assertEqual(code, 0)
        rows = list(csv.reader(io.StringIO(out)))
        header = rows[0]
        self.assertEqual(header[header.index("me") + 1], "component_me")
        self.assertTrue(all(row[header.index("component_me")] == "0" for row in rows[1:]), rows)

    def test_a_recipe_with_nothing_to_build_says_nothing_about_components(self):
        # Both of the sampler's materials are bought - one of them at a price nobody publishes - so a
        # component level would be a number with no job behind it.
        code, out, _ = self.env.run(["build-cost", "Salvage Sampler"])
        self.assertEqual(code, 0)
        self.assertIn("Salvage Sampler (id 920002) - 1 unit from 1 run, at ME 0 / TE 0, "
                      "blueprint 930002", out)
        self.assertNotIn("components at ME", out)


if __name__ == "__main__":
    unittest.main()
