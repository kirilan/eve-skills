"""End-to-end hub allocation through the fake ESI order books."""

from __future__ import annotations

import csv
import io
import json
import unittest

from eve_skills import cmd_sell_plan, market
from tests.fake_esi import (ADA, CORP_SHARED, MARKET_DOMAIN, STATION_AMARR, STATION_JITA,
                            SYSTEM_AMARR, SYSTEM_FORGE, FakeEsiEnv, _order)


class SellPlanTests(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv().start()
        self.addCleanup(self.env.stop)
        self.env.install_core()
        self.env.install_market()
        self.env.install_inventory()
        self.env.install_divisions()
        self.env.server.get(f"/characters/{ADA.character_id}/standings", token=ADA.token, doc=[])
        for hub in ("jita", "amarr"):
            info = market.HUBS[hub]
            self.env.server.get(f"/universe/stations/{info.station_id}",
                                doc={"station_id": info.station_id, "system_id": info.system_id,
                                     "owner": 1234})
        self.env.server.get(f"/universe/systems/{SYSTEM_FORGE}",
                            doc={"constellation_id": 20000020, "name": "Jita"})
        self.env.server.get("/universe/constellations/20000020",
                            doc={"region_id": market.HUBS["jita"].region_id})
        self.env.server.get("/universe/types/34", doc={"type_id": 34, "volume": .02})
        self.env.server.get(f"/latest/route/{SYSTEM_FORGE}/{SYSTEM_FORGE}",
                            doc=[SYSTEM_FORGE])
        def amarr_route(call):
            length = 8 if call.query.get("flag") == "secure" else 6
            return [SYSTEM_FORGE] + [123] * (length - 1) + [SYSTEM_AMARR]
        self.env.server.get(f"/latest/route/{SYSTEM_FORGE}/{SYSTEM_AMARR}",
                            handler=amarr_route)
        self.env.server.get(f"/markets/{MARKET_DOMAIN}/history",
                            doc=[{"type_id": 34, "date": "2026-08-10T12:00:00Z",
                                  "volume": 300, "average": 6.0}],
                            headers={"Last-Modified": "Tue, 11 Aug 2026 11:05:00 GMT"})
        self.set_daytrading(2)

    def set_daytrading(self, level):
        doc = self.env.core_docs["skills"][ADA]
        doc["skills"] = [row for row in doc["skills"]
                         if row["skill_id"] != cmd_sell_plan.DAYTRADING]
        doc["skills"].append({"skill_id": cmd_sell_plan.DAYTRADING,
                              "active_skill_level": level, "trained_skill_level": level})
        self.env.server.get(f"/characters/{ADA.character_id}/skills", token=ADA.token, doc=doc)

    def run_plan(self, *extra):
        return self.env.run(["sell-plan", "--seller", ADA.name, "--from", str(SYSTEM_FORGE), "--hub", "amarr",
                             "--item", "Tritanium=30", *extra, "--json"])

    def test_allocation_saturation_leftover_totals_and_reach(self):
        code, output, err = self.run_plan("--saturated-days", "3")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(output)
        item = doc["items"][0]
        hubs = {row["hub"]: row for row in item["hubs"]}
        self.assertEqual((hubs["amarr"]["allocated"], hubs["jita"]["allocated"]), (10, 20))
        self.assertTrue(hubs["amarr"]["saturated"])
        self.assertEqual(hubs["amarr"]["regional_volume_per_day"], 10)
        self.assertEqual((hubs["amarr"]["shortest_jumps"], hubs["amarr"]["secure_jumps"]), (6, 8))
        self.assertFalse(hubs["amarr"]["can_reprice"])
        self.assertEqual(item["gain"], round(10 * hubs["amarr"]["uplift_per_unit"], 2))
        self.assertEqual(doc["totals"]["units"], 30)
        self.assertEqual(doc["totals"]["packaged_m3"], .6)
        self.assertAlmostEqual(doc["totals"]["reference_net"], 30 * hubs["jita"]["net_per_unit"])
        self.assertEqual(doc["history_scope"], "region")
        self.set_daytrading(3)
        code, output, _ = self.run_plan()
        self.assertEqual(code, 0)
        self.assertTrue(next(row for row in json.loads(output)["routes"]
                             if row["hub"] == "amarr")["can_reprice"])
        self.assertEqual(next(row for row in json.loads(output)["items"][0]["hubs"]
                              if row["hub"] == "amarr")["allocated"], 15)

    def test_inventory_division_filter_and_csv(self):
        self.env.install_corp_inventory()
        # The fixture's only T2-Prod stack is 5,000 Tritanium; elsewhere and non-division
        # assets do not enter the selected stock.
        code, output, err = self.env.run([
            "sell-plan", "--seller", ADA.name, "--from", str(SYSTEM_FORGE), "--hub", "amarr",
            "--from-inventory", "--corp", "--division", "T2-Prod", "--type", "Tritanium",
            "--json"])
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(output)
        self.assertEqual([(item["type_id"], item["quantity"]) for item in doc["items"]],
                         [(34, 5000)])
        self.assertEqual(sum(row["allocated"] for row in doc["items"][0]["hubs"]), 5000)
        self.assertEqual(doc["totals"]["units"], 5000)
        code, output, err = self.env.run([
            "sell-plan", "--seller", ADA.name, "--from", str(SYSTEM_FORGE), "--hub", "amarr",
            "--item", "Tritanium=30", "--csv"])
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(output)))
        self.assertEqual({row["hub"] for row in rows}, {"jita", "amarr"})
        self.assertEqual(sum(int(row["allocated"]) for row in rows), 30)
        self.assertIn("history volume is regional", err)

    def test_level_five_is_region_not_jump_limited(self):
        self.set_daytrading(5)
        code, output, err = self.run_plan()
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(next(row for row in json.loads(output)["routes"]
                              if row["hub"] == "amarr")["can_reprice"])


if __name__ == "__main__":
    unittest.main()
