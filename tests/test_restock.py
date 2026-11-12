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
    CORP_SHARED, MARKET_FORGE, STATION_JITA, FakeEsiEnv, iso,
)

BUILD_STATION = 60099001      # where the corporation's office and factory are
NEARBY_STATION = 60099003     # another station in the same system: the hauler's own hangar
OFFICE = 1055720303518
AUGMENTATION = 34203

# Forge history for the price check: housing and cell both traded at 8.00 for 40 days, so housing's
# 7.00 ask is a dip 12.5% under normal (x3) and cell's 9.00 is high. Other types have no history.
HISTORY = {t: [{"date": f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}", "average": 8.0 * (1 + w),
                "highest": 8.0, "lowest": 8.0, "volume": 1000, "order_count": 10}
               for d, w in enumerate([0.0, 0.01, -0.01, 0.005, -0.005] * 8)]
           for t in (BUILD_HOUSING, BUILD_CELL)}


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
        server.get(f"/markets/{MARKET_FORGE}/history",
                   handler=lambda call: HISTORY.get(int(call.query["type_id"]), []))
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

    def test_the_price_check_buys_more_at_a_dip_and_flags_a_high(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20")
        items = {i["type_id"]: i for i in doc["price_check"]["items"]}
        housing, cell = items[BUILD_HOUSING], items[BUILD_CELL]
        # Housing: the trip needs 200 (40 picked up, 160 bought); a 12.5% dip triples the need.
        self.assertEqual(("dip", 3.0, 400, "multiple"), (housing["state"], housing["multiple"], housing["extra"],
                                                        housing["limit"]))
        self.assertAlmostEqual(400 * 7.0, housing["extra_isk"])
        self.assertAlmostEqual(400 * 1.0, housing["extra_m3"])
        self.assertEqual(("high", 0), (cell["state"], cell["extra"]))
        # Paste has no ask and plate is not bought: neither is judged as a dip.
        self.assertIsNone(items[BUILD_PASTE]["state"])
        self.assertNotIn(BUILD_PLATE, items)
        code, out, err = self.env.run(["restock", "--at", str(BUILD_STATION), "--char", ADA.name,
                                       "--hauler", ADA.name, f"{BUILD_ARTICLE}=20"])
        self.assertEqual(code, 0, err)
        self.assertIn("Multibuy with the price check applied", out)
        self.assertIn("Bulk Casing 560", out)

    def test_the_price_check_can_be_skipped(self):
        self.assertIsNone(self.run_json(f"{BUILD_ARTICLE}=20", "--no-price-check")["price_check"])

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


class PersonalSiteTests(unittest.TestCase):
    """A personal site: no corporation hangar, every character's own stock, transfers between them."""

    def setUp(self):
        from tests.fake_esi import VELA
        self.vela = VELA
        self.env = FakeEsiEnv()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.env.install_core()
        self.env.install_build_cost()
        everything = sso.SCOPES + sso.scopes_for(["all"])
        self.env.write_tokens([self.env.token_for(ADA, everything), self.env.token_for(VELA, everything)])
        server = self.env.server
        ada = [{"item_id": 11, "type_id": BUILD_PLATE, "quantity": 100, "location_id": BUILD_STATION,
                "location_flag": "Hangar", "location_type": "station", "is_singleton": False}]
        vela = [{"item_id": 12, "type_id": BUILD_HOUSING, "quantity": 50, "location_id": BUILD_STATION,
                 "location_flag": "Hangar", "location_type": "station", "is_singleton": False},
                {"item_id": 13, "type_id": BUILD_CELL, "quantity": 30, "location_id": STATION_JITA,
                 "location_flag": "Hangar", "location_type": "station", "is_singleton": False}]
        server.get(f"/characters/{ADA.character_id}/assets", token=ADA.token, doc=ada)
        server.get(f"/characters/{VELA.character_id}/assets", token=VELA.token, doc=vela)
        server.get(f"/universe/stations/{BUILD_STATION}", doc={"station_id": BUILD_STATION, "system_id": 30000142})
        for type_id, volume in ((BUILD_PLATE, 1.0), (BUILD_HOUSING, 1.0), (BUILD_CELL, 2.0), (BUILD_PASTE, 0.5)):
            server.get(f"/universe/types/{type_id}",
                       doc={"type_id": type_id, "name": f"type {type_id}", "group_id": 1,
                            "volume": volume, "packaged_volume": volume})
        with open(os.path.join(self.env.config_home, "eve-skills", "operation.json"), "w") as fh:
            json.dump({"default_site": "home",
                       "sites": {"home": {"owner": "personal", "facility": "lab", "freight_in": "jita->home",
                                          "characters": [ADA.name, VELA.name]}},
                       "facilities": {"lab": {"location_id": BUILD_STATION, "system": "Jita"}},
                       "freight": {"jita->home": {"per_m3": 100, "collateral_pct": 1, "min": 1000}}}, fh)

    def run_json(self, *argv):
        code, out, err = self.env.run(["restock", *argv, "--no-price-check", "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_stock_is_read_per_character_and_moved_to_the_installer(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20@{ADA.name}")
        self.assertEqual((doc["site"], doc["owner"]), ("home", "personal"))
        m = {r["type_id"]: r for r in doc["materials"]}
        # 20 runs: plate 80 is Ada's own; housing 200 takes Vela's 50 on site and buys 150; Vela's
        # 30 cells at the hub are picked up.
        self.assertEqual((80, 0), (m[BUILD_PLATE]["on_site"], m[BUILD_PLATE]["buy"]))
        self.assertEqual((50, 150), (m[BUILD_HOUSING]["on_site"], m[BUILD_HOUSING]["buy"]))
        self.assertEqual(30, m[BUILD_CELL]["pickup"])
        self.assertEqual([{"from": self.vela.name, "to": ADA.name, "type_id": BUILD_HOUSING,
                           "quantity": 50, "name": "Bulk Casing"}], doc["transfers"])
        self.assertIn("Bulk Casing", doc["deliveries"][ADA.name])
        freight = doc["freight"]
        self.assertEqual(freight["route"], "jita->home")
        self.assertAlmostEqual(freight["cost"], 100 * doc["haul_m3"] + 0.01 * freight["collateral"])

    def test_an_unknown_installer_is_refused(self):
        code, _out, err = self.env.run(["restock", f"{BUILD_ARTICLE}=20@Nobody", "--no-price-check"])
        self.assertEqual(code, 1)
        self.assertIn("Nobody", err)

    def test_text_lists_transfers_and_the_courier(self):
        code, out, err = self.env.run(["restock", f"{BUILD_ARTICLE}=20@{ADA.name}", "--no-price-check"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{self.vela.name} -> {ADA.name}: Bulk Casing 50", out)
        self.assertIn("courier jita->home", out)


class SplitSiteTests(unittest.TestCase):
    """A personal site that invents in one structure and builds in another (`facilities`)."""
    LAB, FACTORY = 1_040_000_000_000, 1_040_000_000_002

    def setUp(self):
        from tests.fake_esi import VELA
        self.vela = VELA
        self.env = FakeEsiEnv()
        self.env.start()
        self.addCleanup(self.env.stop)
        self.env.install_core()
        self.env.install_build_cost()
        everything = sso.SCOPES + sso.scopes_for(["all"])
        self.env.write_tokens([self.env.token_for(ADA, everything), self.env.token_for(VELA, everything)])
        self.env._write_json(os.path.join(self.env.data_home, "eve-skills", "blueprint_invention.json"), {
            "source": "synthetic", "build": 2500001, "fetched": iso(-86400),
            "blueprints": {"930999": {"m": {str(BUILD_GOO): 2}, "p": [["930001", 10, 0.3]], "t": 100}},
            "decryptors": {}})

        def item(item_id, type_id, qty, where):
            return {"item_id": item_id, "type_id": type_id, "quantity": qty, "location_id": where,
                    "location_flag": "Hangar", "location_type": "item", "is_singleton": False}
        # Ada: 100 plate at the factory, 30 goo at the factory (wrong place for invention).
        # Vela: 60 housing at the lab (wrong place for building), 4 goo at the lab.
        server = self.env.server
        server.get(f"/characters/{ADA.character_id}/assets", token=ADA.token,
                   doc=[item(11, BUILD_PLATE, 100, self.FACTORY), item(12, BUILD_GOO, 30, self.FACTORY)])
        server.get(f"/characters/{VELA.character_id}/assets", token=VELA.token,
                   doc=[item(21, BUILD_HOUSING, 60, self.LAB), item(22, BUILD_GOO, 4, self.LAB)])
        for type_id, volume in ((BUILD_PLATE, 1.0), (BUILD_HOUSING, 1.0), (BUILD_CELL, 2.0), (BUILD_PASTE, 0.5),
                                (BUILD_GOO, 0.1)):
            server.get(f"/universe/types/{type_id}",
                       doc={"type_id": type_id, "name": f"type {type_id}", "group_id": 1,
                            "volume": volume, "packaged_volume": volume})
        self.write_operation({"manufacturing": "factory"})

    def write_operation(self, overrides, owner="personal"):
        site = {"owner": owner, "facility": "lab", "facilities": overrides}
        site.update({"characters": [ADA.name, self.vela.name]} if owner == "personal" else {"readers": [ADA.name]})
        with open(os.path.join(self.env.config_home, "eve-skills", "operation.json"), "w") as fh:
            json.dump({"default_site": "null", "sites": {"null": site},
                       "facilities": {"lab": {"location_id": self.LAB, "system": "X-EXMPL", "tax_pct": 1,
                                              "label": "X-EXMPL - Lab"},
                                      "factory": {"location_id": self.FACTORY, "system": "Y-EXMPL", "tax_pct": 1,
                                                  "label": "Y-EXMPL - Factory"}}}, fh)

    def run_json(self, *argv):
        code, out, err = self.env.run(["restock", *argv, "--no-price-check", "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_a_build_line_uses_factory_stock_and_moves_what_sits_at_the_lab(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20@{ADA.name}")
        m = {r["type_id"]: r for r in doc["materials"]}
        # Plate 80: Ada's own at the factory. Housing 200: Vela's 60 at the lab are moved, 140 bought.
        self.assertEqual((80, 0, 0), (m[BUILD_PLATE]["on_site"], m[BUILD_PLATE]["move"], m[BUILD_PLATE]["buy"]))
        self.assertEqual((0, 60, 140), (m[BUILD_HOUSING]["on_site"], m[BUILD_HOUSING]["move"], m[BUILD_HOUSING]["buy"]))
        self.assertIn("move between site facilities: Bulk Casing 60 at X-EXMPL - Lab, held by "
                      f"{self.vela.name} 60", doc["notes"])
        self.assertEqual(self.FACTORY, doc["build_station"]["id"])
        self.assertEqual({"manufacturing": self.FACTORY, "invention": self.LAB},
                         {k: v["id"] for k, v in doc["stations"].items()})
        # Only what the request wants is read: no goo for a build-only request.
        self.assertEqual({str(BUILD_PLATE): 100}, doc["holdings_by_station"][str(self.FACTORY)][ADA.name])
        self.assertEqual({str(BUILD_HOUSING): 60}, doc["holdings_by_station"][str(self.LAB)][self.vela.name])
        self.assertEqual(doc["holdings"], doc["holdings_by_station"][str(self.FACTORY)])

    def test_an_invent_line_uses_lab_stock_and_moves_what_sits_at_the_factory(self):
        doc = self.run_json("--invent", f"{BUILD_ARTICLE}=10@{self.vela.name}")
        goo = {r["type_id"]: r for r in doc["materials"]}[BUILD_GOO]
        # 20 goo: Vela's 4 at the lab, 16 of Ada's 30 at the factory moved, nothing bought.
        self.assertEqual((20, 4, 16, 0), (goo["need"], goo["on_site"], goo["move"], goo["buy"]))
        self.assertTrue(any(n.startswith("move between site facilities: ") and "Y-EXMPL - Factory" in n
                            and f"{ADA.name} 16" in n for n in doc["notes"]))

    def test_transfers_are_planned_where_each_job_is_installed(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20@{self.vela.name}", "--invent", f"{BUILD_ARTICLE}=1@{ADA.name}")
        by_at = {(t["from"], t["to"], t["at"]) for t in doc["transfers"]}
        self.assertIn((ADA.name, self.vela.name, "Y-EXMPL - Factory"), by_at)    # plate for Vela's build
        self.assertIn((self.vela.name, ADA.name, "X-EXMPL - Lab"), by_at)        # goo for Ada's invention

    def test_at_reads_one_location_for_everything(self):
        doc = self.run_json(f"{BUILD_ARTICLE}=20@{ADA.name}", "--site", "null", "--at", str(self.LAB))
        self.assertNotIn("stations", doc)
        m = {r["type_id"]: r for r in doc["materials"]}
        self.assertEqual((60, 0), (m[BUILD_HOUSING]["on_site"], m[BUILD_HOUSING]["move"]))

    def test_a_corp_site_with_overrides_needs_at(self):
        self.write_operation({"manufacturing": "factory"}, owner="corp")
        code, _out, err = self.env.run(["restock", f"{BUILD_ARTICLE}=20", "--no-price-check"])
        self.assertNotEqual(0, code)
        self.assertIn("not supported for a corp site", err)
        self.assertIn("--at", err)
