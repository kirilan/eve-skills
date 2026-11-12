"""`types`: id, exact name, group, category and packaged volume for names or ids (fake ESI, no network)."""

from __future__ import annotations

import csv
import io
import json
import unittest
from unittest import mock

from eve_skills import cmd_types

from tests.fake_esi import INV_TYPE_SHIP, FakeEsiEnv


class ParseLineTests(unittest.TestCase):
    def test_plain_names_tab_quantities_and_blank_lines(self):
        self.assertEqual(("Tritanium", None), cmd_types.parse_line("Tritanium\n"))
        self.assertEqual(("Tritanium", 12000), cmd_types.parse_line("Tritanium\t12,000\n"))
        self.assertIsNone(cmd_types.parse_line("   \n"))
        with self.assertRaises(RuntimeError):
            cmd_types.parse_line("Tritanium\tlots")


class TypesCommandTests(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.env.install_inventory()
        self.env.server.get("/universe/types/999999", error=(404, {"error": "Type not found"}))
        # Live ESI matches names regardless of case and answers with the name as CCP spells it.
        known = {"tritanium": 34, "pyerite": 36, "rifter": INV_TYPE_SHIP}
        spelled = {34: "Tritanium", 36: "Pyerite", INV_TYPE_SHIP: "Rifter"}
        self.env.server.post("/universe/ids", handler=lambda call: {"inventory_types": [
            {"id": known[name.casefold()], "name": spelled[known[name.casefold()]]}
            for name in call.json if name.casefold() in known]})
        self.addCleanup(self.env.stop)

    def run_types(self, *args, stdin=None):
        with mock.patch("sys.stdin", io.StringIO(stdin or "")):
            return self.env.run(["types", *args])

    def test_names_and_ids_resolve_with_group_category_and_packaged_volume(self):
        code, out, err = self.run_types("tritanium", "36", str(INV_TYPE_SHIP), "--json")
        self.assertEqual((0, ""), (code, err))
        doc = json.loads(out)
        self.assertEqual([], doc["unknown"])
        self.assertIsNone(doc["total_m3"])
        self.assertEqual([(34, "Tritanium", "Ore", "Material", 0.02), (36, "Pyerite", "Ore", "Material", 0.02),
                          (INV_TYPE_SHIP, "Rifter", "Frigate", "Ship", 780.0)],
                         [(r["type_id"], r["name"], r["group"], r["category"], r["packaged_m3"]) for r in doc["types"]])
        # One batched name lookup, never one per name.
        self.assertEqual(1, len(self.env.server.calls_to("/universe/ids")))

    def test_unknown_names_and_ids_are_reported_and_fail_the_exit_status_while_the_rest_print(self):
        code, out, _ = self.run_types("Tritanium", "Nonsenseium", "999999")
        self.assertEqual(1, code)
        self.assertIn("Tritanium", out)
        self.assertIn("not a type: Nonsenseium", out)
        self.assertIn("not a type: 999999", out)

    def test_stdin_paste_echoes_quantities_and_adds_an_m3_total(self):
        code, out, _ = self.run_types("-", "--csv", stdin="Tritanium\t1,000\n\nRifter\t2\n")
        self.assertEqual(0, code)
        rows = list(csv.DictReader(out.splitlines()))
        self.assertEqual(cmd_types.CSV_COLUMNS, list(rows[0]))
        self.assertEqual([("Tritanium", "1000", "20.0"), ("Rifter", "2", "1560.0")],
                         [(r["name"], r["quantity"], r["total_m3"]) for r in rows])
        code, out, _ = self.run_types("-", stdin="Tritanium\t1,000\nRifter\t2\n")
        self.assertIn("total: 1,580.00 m3 (packaged)", out)

    def test_csv_keeps_unknown_names_off_stdout(self):
        code, out, err = self.run_types("Nonsenseium", "Pyerite", "--csv")
        self.assertEqual(1, code)
        self.assertNotIn("Nonsenseium", out)
        self.assertIn("not a type: Nonsenseium", err)


if __name__ == "__main__":
    unittest.main()
