"""`restock`: demand from recipes, netted against where stock already is, fitted to a hold.

Recipes and books come from the build-cost universe (`fake_esi.install_build_cost`); the corporation
keeps its hangars inside an office, as live ESI nests them. No network."""

from __future__ import annotations

import json
import os
import unittest

from eve_skills import cmd_restock, industry, sso
from tests.fake_esi import (
    ADA, BUILD_ARTICLE, BUILD_CELL, BUILD_CRYO, BUILD_GOO, BUILD_HOUSING, BUILD_PASTE, BUILD_PLATE,
    CORP_SHARED, STATION_JITA, FakeEsiEnv, iso,
)

BUILD_STATION = 60099001      # where the corporation's office and factory are
NEARBY_STATION = 60099003     # another station in the same system: the hauler's own hangar
OFFICE = 1055720303518
AUGMENTATION = 34203


class RequirementTests(unittest.TestCase):
    def recipe(self, limit):
        doc = {"930001": {"manufacturing": {"m": {"34": 7}, "p": ["920001", 1], "t": 60, "limit": limit}}}
        return industry.recipe_index(doc)[920001]

    def test_me_rounding_is_per_job_of_the_blueprint_limit(self):
        # 7 per run at ME 10: one 10-run job needs ceil(63) = 63; two 5-run jobs need 32 + 32 = 64.
        self.assertEqual(63, cmd_restock.build_requirements(self.recipe(10), 10, 10)[34])
        self.assertEqual(64, cmd_restock.build_requirements(self.recipe(5), 10, 10)[34])

    def test_invention_adds_datacores_per_attempt_and_one_decryptor_each(self):
        doc = {"blueprints": {"1": {"m": {"20410": 2, "20411": 1}, "p": [["930001", 10, 0.3]], "t": 1}},
               "decryptors": {str(AUGMENTATION): {"name": "Augmentation Decryptor"}}}
        need, name = cmd_restock.invention_requirements(doc, 930001, 14, "Augmentation")
        self.assertEqual(({20410: 28, 20411: 14, AUGMENTATION: 14}, "Augmentation Decryptor"), (dict(need), name))
        self.assertNotIn(AUGMENTATION, cmd_restock.invention_requirements(doc, 930001, 14, None)[0])
        with self.assertRaises(RuntimeError):
            cmd_restock.invention_requirements(doc, 930001, 1, "Nonsense")


class RestockCommandTests(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.env.install_core()
        self.env.install_build_cost()
        self.env.write_tokens([self.env.token_for(ADA, sso.SCOPES + sso.scopes_for(["all"]))])
        self.env._write_json(os.path.join(self.env.data_home, "eve-skills", "blueprint_invention.json"), {
            "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
            "blueprints": {"930999": {"m": {str(BUILD_GOO): 2}, "p": [["930001", 10, 0.3]], "t": 100}},
            "decryptors": {str(AUGMENTATION): {"name": "Augmentation Decryptor", "probability": 0.6,
                                               "me": -2, "te": 2, "runs": 9}}})
        corp = [
            {"item_id": OFFICE, "type_id": 27, "quantity": 1, "location_id": BUILD_STATION,
             "location_flag": "OfficeFolder", "location_type": "station", "is_singleton": True},
            {"item_id": 1, "type_id": BUILD_PLATE, "quantity": 100, "location_id": OFFICE,
             "location_flag": "CorpSAG4", "location_type": "item", "is_singleton": False},
            {"item_id": 2, "type_id": BUILD_HOUSING, "quantity": 40, "location_id": STATION_JITA,
             "location_flag": "CorpDeliveries", "location_type": "station", "is_singleton": False},
        ]
        own = [
            {"item_id": 3, "type_id": BUILD_CELL, "quantity": 20, "location_id": STATION_JITA,
             "location_flag": "Hangar", "location_type": "station", "is_singleton": False},
            {"item_id": 4, "type_id": BUILD_CELL, "quantity": 7, "location_id": NEARBY_STATION,
             "location_flag": "Hangar", "location_type": "station", "is_singleton": False},
        ]
        server = self.env.server
        server.get(f"/corporations/{CORP_SHARED}/assets", token=ADA.token, doc=corp)
        server.get(f"/characters/{ADA.character_id}/assets", token=ADA.token, doc=own)
        for station in (BUILD_STATION, NEARBY_STATION, STATION_JITA):
            server.get(f"/universe/stations/{station}", doc={"station_id": station, "system_id": 30000142})
        for type_id, volume in ((BUILD_PLATE, 1.0), (BUILD_HOUSING, 1.0), (BUILD_CELL, 2.0),
                                (BUILD_PASTE, 0.5), (BUILD_GOO, 0.1), (BUILD_CRYO, 0.01), (AUGMENTATION, 0.1)):
            server.get(f"/universe/types/{type_id}",
                       doc={"type_id": type_id, "name": f"type {type_id}", "group_id": 1,
                            "volume": volume, "packaged_volume": volume})

    def run_json(self, *argv):
        code, out, err = self.env.run(["restock", "--at", str(BUILD_STATION), "--char", ADA.name,
                                       "--hauler", ADA.name, *argv, "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_stock_is_netted_on_site_then_pickup_then_move(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20", "--invent", f"{BUILD_ARTICLE}=5:Augmentation",
                            "--extra", f"{BUILD_CRYO}=10")
        m = {r["type_id"]: r for r in doc["materials"]}
        # 20 runs: plate 80 (on site 100), housing 200, cell 100, paste 40.
        self.assertEqual((80, 80, 0), (m[BUILD_PLATE]["need"], m[BUILD_PLATE]["on_site"], m[BUILD_PLATE]["buy"]))
        self.assertEqual((40, 160), (m[BUILD_HOUSING]["pickup"], m[BUILD_HOUSING]["buy"]))
        self.assertEqual((20, 7, 73), (m[BUILD_CELL]["pickup"], m[BUILD_CELL]["move"], m[BUILD_CELL]["buy"]))
        # Invention: 2 goo per attempt and one decryptor each; the extra is bought in full.
        self.assertEqual(10, m[BUILD_GOO]["buy"])
        self.assertEqual(5, m[AUGMENTATION]["buy"])
        self.assertEqual(10, m[BUILD_CRYO]["buy"])
        # Paste has no sell order at the hub: it is bought but not priced, and said so.
        self.assertIsNone(m[BUILD_PASTE]["buy_isk"])
        self.assertTrue(any("no sell order" in n for n in doc["notes"]))
        self.assertAlmostEqual(sum(r["haul_m3"] for r in doc["materials"]), doc["haul_m3"])

    def test_cargo_cuts_the_last_line_to_what_fits(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20", "--cargo", "200")
        line = doc["lines"][0]
        self.assertEqual("partial", line["status"])
        self.assertLessEqual(doc["haul_m3"], 200)
        self.assertGreater(line["fit"], 0)
        more = self.run_json(f"{BUILD_ARTICLE}={line['fit'] + 1}")
        self.assertGreater(more["haul_m3"], 200)

    def test_text_output_has_the_multibuy_block(self):
        code, out, err = self.env.run(["restock", "--at", str(BUILD_STATION), "--char", ADA.name,
                                       f"{BUILD_ARTICLE}=20"])
        self.assertEqual(code, 0, err)
        self.assertIn("Multibuy:", out)
        self.assertIn("pick up", out)

    def test_nothing_to_supply_is_refused(self):
        code, _, err = self.env.run(["restock", "--at", str(BUILD_STATION)])
        self.assertNotEqual(0, code)
        self.assertIn("nothing to supply", err)


if __name__ == "__main__":
    unittest.main()
