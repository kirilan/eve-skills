"""The operation file: sites, facilities (stated or derived from rigs), hubs, freight - and the
`build-cost --facility/--site` seam that reads it.

The derivations are written out by hand: a Raitaru with T1 rigs in null-sec costs
0.99 * (1 - 0.02 * 2.1) material and 0.85 * (1 - 0.20 * 2.1) time."""

from __future__ import annotations

import json
import os
import unittest

from eve_skills import industry, operation
from tests.fake_esi import BUILD_ARTICLE, SYSTEM_FORGE, FakeEsiEnv


NULL = {"system": "X-EXMPL", "structure": "raitaru", "security": "null", "tax_pct": 1.0,
        "location_id": 1_040_000_000_000,
        "rigs": {"manufacturing": {"me": "t1", "te": "t1"}, "invention": {"te": "t2"}}}


class DeriveTest(unittest.TestCase):
    def test_raitaru_t1_rigs_in_null(self):
        material, time, _ = operation.derive("raitaru", "null", NULL["rigs"])
        self.assertAlmostEqual(material["manufacturing"], 0.99 * (1 - 0.042), places=6)
        self.assertAlmostEqual(time["manufacturing"], 0.85 * (1 - 0.42), places=6)
        # A science time rig (T2: 24 % x 2.1) on top of the hull's 15 %; invention has no material bonus.
        self.assertAlmostEqual(time["invention"], 0.85 * (1 - 0.504), places=6)
        self.assertNotIn("invention", material)
        # No copying rig: the hull's role bonus only.
        self.assertAlmostEqual(time["copying"], 0.85, places=6)

    def test_npc_station_has_no_bonus(self):
        self.assertEqual(operation.derive(None, None, None)[:2], ({}, {}))

    def test_highsec_rig_is_not_multiplied(self):
        material, _time, _ = operation.derive("azbel", "high", {"manufacturing": {"me": "t2"}})
        self.assertAlmostEqual(material["manufacturing"], 0.99 * (1 - 0.024), places=6)

    def test_refuses_what_it_does_not_know(self):
        for args in (("fortizar", "null", None), ("raitaru", "deep", None),
                     ("raitaru", "null", {"cooking": {"me": "t1"}}),
                     ("raitaru", "null", {"manufacturing": {"me": "t3"}})):
            with self.subTest(args=args), self.assertRaises(operation.OperationError):
                operation.derive(*args)


class ParseTest(unittest.TestCase):
    def doc(self, **extra):
        doc = {"default_site": "null",
               "sites": {"Highsec": {"owner": "corp", "facility": "highsec", "readers": ["Alice Builder"]},
                         "null": {"owner": "personal", "facility": "example",
                                  "characters": ["Carol Hauler", "Dave Miner"]}},
               "facilities": {"highsec": {"location_id": 60003760, "system": "Jita"}, "example": dict(NULL)},
               "hubs": {"x-hub": {"location_id": 1040000000001, "region_id": 10000060}},
               "freight": {"X-Hub->Jita": {"per_m3": 400, "collateral_pct": 1, "min": 10_000_000}}}
        doc.update(extra)
        return doc

    def test_sites_facilities_hubs_freight(self):
        op = operation.parse(self.doc(), "/x/operation.json")
        self.assertEqual(op.site(None).name, "null")
        self.assertEqual(op.site("HIGHSEC").owner, "corp")
        self.assertEqual(op.site("null").characters, ("Carol Hauler", "Dave Miner"))
        facility = op.facility("example")
        self.assertEqual(facility.tax, 0.01)
        self.assertEqual(facility.unknown, ())
        self.assertIn("raitaru", facility.derived_from)
        self.assertEqual(op.facility("highsec").material_multiplier("manufacturing"), 1.0)
        self.assertEqual(op.hubs["x-hub"].location_id, 1040000000001)
        route = op.route("x-hub", "jita")
        # 20,000 m3 and 5B collateral: 8M volume + 50M collateral.
        self.assertEqual(route.contract_cost(20_000, 5_000_000_000), 58_000_000)
        self.assertEqual(route.contract_cost(10, 1_000_000), 10_000_000)   # the minimum binds

    def test_a_structure_without_tax_or_rigs_says_so(self):
        doc = self.doc()
        doc["facilities"]["example"] = {"system": "X-EXMPL", "structure": "raitaru", "security": "null"}
        facility = operation.parse(doc).facility("example")
        self.assertEqual(set(facility.unknown), {"tax_pct", "rigs", "location_id"})

    def test_stated_multipliers_win_over_derivation(self):
        doc = self.doc()
        doc["facilities"]["example"] = {"system": "X-EXMPL", "tax_pct": 0.5,
                                     "material": {"manufacturing": 0.95}, "time": {"manufacturing": 0.6}}
        facility = operation.parse(doc).facility("example")
        self.assertEqual((facility.material_multiplier("manufacturing"), facility.time_multiplier("manufacturing")),
                         (0.95, 0.6))

    def test_mistakes_are_named(self):
        bad = [
            self.doc(default_site="moon"),
            self.doc(sites={"x": {"owner": "alliance", "facility": "highsec"}}),
            self.doc(sites={"x": {"owner": "personal", "facility": "highsec"}}),
            self.doc(sites={"x": {"owner": "corp", "facility": "nowhere"}}),
            self.doc(freight={"jita": {"per_m3": 1}}),
            self.doc(hubs={"h": {"location_id": "x"}}),
            self.doc(hubs={"h": {"location_id": 1040000000001, "region": "Delve"}}),
        ]
        for doc in bad:
            with self.subTest(doc=doc), self.assertRaises(operation.OperationError):
                operation.parse(doc)
        with self.assertRaises(operation.OperationError):
            operation.parse(self.doc()).site("moon")

    def test_no_file_means_built_in_behaviour(self):
        op = operation.load("/nonexistent/operation.json")
        self.assertFalse(op.configured)
        self.assertIsNone(op.site(None))

    def test_template_parses(self):
        op = operation.parse(operation.TEMPLATE)
        self.assertEqual(op.site(None).name, "highsec")


class InventedRunLimitTest(unittest.TestCase):
    INVENTION = {"blueprints": {"31719": {"m": {}, "p": [["31725", 1, 0.34]], "t": 1}},
                 "decryptors": {"34203": {"runs": 9, "probability": 0.6, "me": -2, "te": 2},
                                "34205": {"runs": 0, "probability": 1.1, "me": 3, "te": 6}}}

    def test_base_output_plus_the_largest_decryptor(self):
        self.assertEqual(industry.invented_run_limit(31725, self.INVENTION), 10)

    def test_nothing_invents_it(self):
        self.assertEqual(industry.invented_run_limit(999, self.INVENTION), 0)
        self.assertEqual(industry.invented_run_limit(31725, None), 0)


class BuildCostFacilityTest(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_build_cost()
        self.addCleanup(self.env.stop)

    def write(self, document):
        with open(os.path.join(self.env.config_home, "eve-skills", "operation.json"), "w") as fh:
            json.dump(document, fh)

    def job(self, argv):
        code, out, err = self.env.run(["build-cost", str(BUILD_ARTICLE), *argv, "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_facility_applies_tax_and_time(self):
        self.write({"facilities": {"lab": {"system": str(SYSTEM_FORGE), "tax_pct": 1.0,
                                           "time": {"manufacturing": 0.5}}},
                    "sites": {"home": {"owner": "corp", "facility": "lab"}}})
        plain = self.job([])
        for flag in (["--facility", "lab"], ["--site", "home"]):
            with self.subTest(flag=flag):
                doc = self.job(flag)
                self.assertEqual(doc["job"]["facility_tax"], 0.01)
                self.assertEqual(doc["job"]["time_multiplier"], 0.5)
                self.assertEqual(doc["job"]["facility"], "lab")
                self.assertEqual(doc["products"][0]["time_seconds"],
                                 round(plain["products"][0]["time_seconds"] * 0.5))
        # An explicit flag still wins over the facility.
        self.assertEqual(self.job(["--facility", "lab", "--facility-tax", "2"])["job"]["facility_tax"], 0.02)

    def test_facility_needs_the_file(self):
        code, _out, err = self.env.run(["build-cost", str(BUILD_ARTICLE), "--facility", "lab"])
        self.assertEqual(code, 1)
        self.assertIn("operation template", err)


if __name__ == "__main__":
    unittest.main()


class StructureMarketTest(unittest.TestCase):
    """A configured hub that is a player structure reads `/markets/structures/{id}` with a token."""

    STRUCTURE = 1_040_000_000_001

    def setUp(self):
        from tests.fake_esi import ADA, MARKET_FORGE
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.env.install_market()
        self.addCleanup(self.env.stop)
        with open(os.path.join(self.env.config_home, "eve-skills", "operation.json"), "w") as fh:
            json.dump({"hubs": {"keep": {"location_id": self.STRUCTURE, "region_id": MARKET_FORGE,
                                         "label": "The Keepstar", "reader": "Ada Vane"}}}, fh)
        self.ada = ADA

    def orders(self, rows=None, error=None):
        path = f"/markets/structures/{self.STRUCTURE}"
        if error:
            self.env.server.get(path, error=error)
        else:
            self.env.server.get(path, token=self.ada.token, doc=rows, headers={"X-Pages": "1"})

    def test_structure_book_is_read_and_split_per_type(self):
        self.orders([
            {"order_id": 1, "type_id": 34, "price": 6.0, "volume_remain": 100, "is_buy_order": False,
             "location_id": self.STRUCTURE},
            {"order_id": 2, "type_id": 34, "price": 5.5, "volume_remain": 50, "is_buy_order": False,
             "location_id": self.STRUCTURE},
            {"order_id": 3, "type_id": 34, "price": 4.0, "volume_remain": 10, "is_buy_order": True,
             "location_id": self.STRUCTURE},
            {"order_id": 4, "type_id": 35, "price": 1.0, "volume_remain": 10, "is_buy_order": False,
             "location_id": self.STRUCTURE},
        ])
        code, out, err = self.env.run(["market", "34", "--hub", "keep", "--json"])
        self.assertEqual(code, 0, err)
        scope = json.loads(out)["types"][0]["scopes"][0]
        self.assertEqual(scope["scope"], "The Keepstar (structure)")
        self.assertEqual((scope["min_sell"], scope["max_buy"]), (5.5, 4.0))

    def test_refusal_names_the_fix(self):
        self.orders(error=(403, {"error": "Forbidden"}))
        code, _out, err = self.env.run(["market", "34", "--hub", "keep"])
        self.assertEqual(code, 1)
        self.assertIn("docking access", err)

    def test_unknown_hub_lists_configured_ones(self):
        code, _out, err = self.env.run(["market", "34", "--hub", "nowhere"])
        self.assertEqual(code, 1)
        self.assertIn("keep", err)


class DashboardSitesTest(unittest.TestCase):
    def test_plan_and_merge_cover_a_split_operation(self):
        from unittest import mock
        from eve_skills import cmd_dashboard
        op = operation.parse({"sites": {
            "highsec": {"owner": "corp", "facility": "k", "readers": ["Bob Inventor"]},
            "null": {"owner": "personal", "facility": "n", "characters": ["Carol Hauler"]}},
            "facilities": {"k": {"location_id": 60003760}, "n": {"system": "X-EXMPL"}}})
        with mock.patch.object(operation, "load", return_value=op):
            plan = cmd_dashboard._status_plan()
        self.assertEqual([("industry", "status", "--corp", "--char", "Bob Inventor"), ("industry", "status")],
                         [args for args, _keep in plan])
        corp = {"owners": [{"owner_name": "Corp", "blueprints": [1]}]}
        personal = {"owners": [{"owner_name": "Carol Hauler", "blueprints": [2]},
                               {"owner_name": "Alice Builder", "blueprints": [3]}]}
        status, error = cmd_dashboard._merged_status([((corp, None), plan[0][1]), ((personal, None), plan[1][1])])
        self.assertIsNone(error)
        self.assertEqual(["Corp", "Carol Hauler"], [o["owner_name"] for o in status["owners"]])

    def test_without_a_file_it_is_the_corporation_snapshot(self):
        from unittest import mock
        from eve_skills import cmd_dashboard
        with mock.patch.object(operation, "load", return_value=operation.EMPTY):
            self.assertEqual([(("industry", "status", "--corp"), None)], cmd_dashboard._status_plan())
