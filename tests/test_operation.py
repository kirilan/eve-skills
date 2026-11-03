"""The operation file: sites, facilities (stated or derived from rigs), hubs, freight - and the
`build-cost --facility/--site` seam that reads it.

The derivations are written out by hand: a Raitaru with T1 rigs in null-sec is the 4GYV-Q case the
null-sec scan (ledger note #322) was costed with, 0.99 * (1 - 0.02 * 2.1) material and
0.85 * (1 - 0.20 * 2.1) time."""

from __future__ import annotations

import json
import os
import unittest

from eve_skills import industry, operation
from tests.fake_esi import BUILD_ARTICLE, SYSTEM_FORGE, FakeEsiEnv


NULL = {"system": "4GYV-Q", "structure": "raitaru", "security": "null", "tax_pct": 1.0,
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
               "sites": {"Kulelen": {"owner": "corp", "facility": "kulelen", "readers": ["General Fail"]},
                         "null": {"owner": "personal", "facility": "4gyv",
                                  "characters": ["Santraginus XI", "Santraginus XII"]}},
               "facilities": {"kulelen": {"location_id": 60002329, "system": "Kulelen"}, "4gyv": dict(NULL)},
               "hubs": {"4-hwwf": {"location_id": 1053970513596, "region": "Vale of the Silent"}},
               "freight": {"4-HWWF->Jita": {"per_m3": 350, "collateral_pct": 1, "min": 10_000_000}}}
        doc.update(extra)
        return doc

    def test_sites_facilities_hubs_freight(self):
        op = operation.parse(self.doc(), "/x/operation.json")
        self.assertEqual(op.site(None).name, "null")
        self.assertEqual(op.site("KULELEN").owner, "corp")
        self.assertEqual(op.site("null").characters, ("Santraginus XI", "Santraginus XII"))
        facility = op.facility("4gyv")
        self.assertEqual(facility.tax, 0.01)
        self.assertEqual(facility.unknown, ())
        self.assertIn("raitaru", facility.derived_from)
        self.assertEqual(op.facility("kulelen").material_multiplier("manufacturing"), 1.0)
        self.assertEqual(op.hubs["4-hwwf"].location_id, 1053970513596)
        route = op.route("4-hwwf", "jita")
        # 20,000 m3 and 5B collateral: 7M volume + 50M collateral (the screenshot's 57M).
        self.assertEqual(route.contract_cost(20_000, 5_000_000_000), 57_000_000)
        self.assertEqual(route.contract_cost(10, 1_000_000), 10_000_000)   # the minimum binds

    def test_a_structure_without_tax_or_rigs_says_so(self):
        doc = self.doc()
        doc["facilities"]["4gyv"] = {"system": "4GYV-Q", "structure": "raitaru", "security": "null"}
        facility = operation.parse(doc).facility("4gyv")
        self.assertEqual(set(facility.unknown), {"tax_pct", "rigs", "location_id"})

    def test_stated_multipliers_win_over_derivation(self):
        doc = self.doc()
        doc["facilities"]["4gyv"] = {"system": "4GYV-Q", "tax_pct": 0.5,
                                     "material": {"manufacturing": 0.95}, "time": {"manufacturing": 0.6}}
        facility = operation.parse(doc).facility("4gyv")
        self.assertEqual((facility.material_multiplier("manufacturing"), facility.time_multiplier("manufacturing")),
                         (0.95, 0.6))

    def test_mistakes_are_named(self):
        bad = [
            self.doc(default_site="moon"),
            self.doc(sites={"x": {"owner": "alliance", "facility": "kulelen"}}),
            self.doc(sites={"x": {"owner": "personal", "facility": "kulelen"}}),
            self.doc(sites={"x": {"owner": "corp", "facility": "nowhere"}}),
            self.doc(freight={"jita": {"per_m3": 1}}),
            self.doc(hubs={"h": {"location_id": "x"}}),
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
        self.assertEqual(op.site(None).name, "kulelen")


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
