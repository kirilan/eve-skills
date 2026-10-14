"""Stockpile analysis: days of stock for finished goods, days of cover for inputs, blocked copies, history.

Built on `test_ledger`'s synthetic industry: PART is the manufacturing input, CORE the invention's data
core, T2_ITEM the product. Live stock is handed in as `ledger inventory --json` rows.
"""

from __future__ import annotations

import unittest
from datetime import date

from eve_skills import ledger, stockpile

from .test_ledger import CORE, PART, T1_BP, T2_BP, T2_ITEM, copy, facts, job, replay, stamp, tx

TODAY = date(2026, 9, 10)


def book(sold: int = 4) -> ledger.Book:
    """Two 10-run builds (days 4 and 6), `sold` units sold on day 8."""
    return replay(facts(
        transactions=[tx(1, PART, 200, 10.0, buy=True), tx(8, T2_ITEM, sold, 200.0, buy=False)] if sold else
                     [tx(1, PART, 200, 10.0, buy=True)],
        jobs=[job(1, ledger.INVENTION, blueprint_type=T1_BP, product=T2_BP, runs=1, start=stamp(2),
                  end=stamp(3), successes=1, cost=10.0),
              job(2, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10, start=stamp(3),
                  end=stamp(4)),
              job(3, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10, start=stamp(5),
                  end=stamp(6))],
        blueprints=[copy(1, T2_BP, 2, 4, seen=stamp(3))], opening={CORE: 1.0}), now=stamp(10))


def item(type_id, held, listed=0, cls="finished", cost=50.0):
    return {"type_id": type_id, "class": cls, "held": held, "in_sell_orders": listed, "unit_cost": cost,
            "unit_market": 100.0, "market_value": (held + listed) * 100.0, "cost_value": (held + listed) * cost,
            "type_name": f"type {type_id}"}


class StatusRuleTests(unittest.TestCase):
    def test_goods_thresholds(self):
        self.assertEqual(("sold out", 0.0), stockpile.goods_status(0, 1.0, 0))
        self.assertEqual(("not selling", None), stockpile.goods_status(5, 0.0, 1.0))
        self.assertEqual("piling up", stockpile.goods_status(30, 2.0, 0)[0])        # 15 days
        self.assertEqual("slow", stockpile.goods_status(20, 2.0, 0)[0])             # 10 days
        self.assertEqual(("ok", 3.0), stockpile.goods_status(6, 2.0, 0))

    def test_input_thresholds(self):
        self.assertEqual(("out", 0.0), stockpile.input_status(0, 5.0, 0))
        self.assertEqual("out", stockpile.input_status(0, 0.0, 2)[0])              # blocks copies, none held
        self.assertEqual("low", stockpile.input_status(10, 5.0, 0)[0])             # 2 days
        self.assertEqual("short", stockpile.input_status(100, 5.0, 1)[0])          # 20 days, but blocks a copy
        self.assertEqual("watch", stockpile.input_status(25, 5.0, 0)[0])           # 5 days
        self.assertEqual(("ok", 20.0), stockpile.input_status(100, 5.0, 0))


class AnalyseTests(unittest.TestCase):
    def analyse(self, b, items, **kw):
        return stockpile.analyse(b, items, today=TODAY, **kw)

    def test_a_line_built_faster_than_it_sells_is_piling_up(self):
        out = self.analyse(book(sold=4), [item(T2_ITEM, 10, 6), item(PART, 1000, cls="material")],
                           market_volume={T2_ITEM: 8.0})
        (line,) = out["goods"]
        # First activity on day 4, so the window is days 4..10 = 7 days: 20 built, 4 sold
        self.assertEqual((7.0, 2.86, 0.57), (line["window_days"], line["built_per_day"], line["sold_per_day"]))
        self.assertEqual(("piling up", 28.0), (line["status"], line["days_of_stock"]))
        self.assertEqual(2.0, line["market_days"])                                  # 16 on hand / 8 a day
        self.assertEqual(1, out["counts"]["piling_up"])

    def test_inputs_get_days_of_cover_from_what_jobs_used(self):
        out = self.analyse(book(), [item(PART, 20, cls="material")])
        part = next(m for m in out["materials"] if m["type_id"] == PART)
        # 2 x 49 parts over days 3..10 (8 days)
        self.assertEqual((12.25, 1.6, "low"), (part["used_per_day"], part["days_of_cover"], part["status"]))
        core = next(m for m in out["materials"] if m["type_id"] == CORE)
        self.assertEqual(("out", 0.0), (core["status"], core["held"]))                # not in stock at all
        self.assertEqual(["out", "low"], [m["status"] for m in out["materials"]])    # most urgent first

    def test_only_idle_copies_of_active_lines_block_an_input(self):
        blueprints = [
            {"kind": "BPC", "blueprint_type_id": T2_BP, "product": "Widget II", "count": 3, "can_build_jobs": 0,
             "limiting_type_id": PART, "have": 20, "per_job": 49},
            {"kind": "BPC", "blueprint_type_id": 999, "product": "Old line", "count": 5, "can_build_jobs": 0,
             "limiting_type_id": PART, "have": 20, "per_job": 49},
            {"kind": "BPO", "blueprint_type_id": T2_BP, "product": "Widget II", "count": 1, "can_build_jobs": 0,
             "limiting_type_id": PART},
        ]
        out = self.analyse(book(), [item(PART, 2000, cls="material")], blueprints=blueprints)
        part = next(m for m in out["materials"] if m["type_id"] == PART)
        self.assertEqual([("Widget II", 3)], [(b["product"], b["copies"]) for b in part["blocking"]])
        self.assertEqual("short", part["status"])

    def test_history_is_carried_over_quiet_days(self):
        out = self.analyse(book(sold=4), [item(T2_ITEM, 16)])
        days = dict(zip(out["history_days"], (t["qty"] for t in out["goods_totals"])))
        self.assertEqual((0.0, 10.0, 10.0, 20.0, 16.0, 16.0),
                         (days["2026-09-03"], days["2026-09-04"], days["2026-09-05"], days["2026-09-06"],
                          days["2026-09-08"], days["2026-09-10"]))
        self.assertEqual(16.0, out["goods"][0]["history"][-1])

    def test_snapshots_total_by_class_and_follow_each_type(self):
        rows = [{"ts": stamp(9), "type_id": T2_ITEM, "class": "finished", "held": 4, "listed": 6, "unit_cost": 50.0,
                 "unit_market": 100.0},
                {"ts": stamp(9), "type_id": PART, "class": "material", "held": 100, "listed": 0, "unit_cost": 10.0,
                 "unit_market": 12.0},
                {"ts": stamp(10), "type_id": PART, "class": "material", "held": 80, "listed": 5, "unit_cost": 10.0,
                 "unit_market": None}]
        hist = stockpile.snapshot_history(rows, [T2_ITEM, PART])
        first, second = hist["polls"]
        self.assertEqual((500.0, 1000.0, 1000.0, 1200.0),
                         (first["finished_cost"], first["finished_market"], first["material_cost"],
                          first["material_market"]))
        self.assertEqual((850.0, 0.0), (second["material_cost"], second["material_market"]))
        self.assertEqual([("2026-09-09", 10.0)], hist["per_type"][T2_ITEM])           # held + listed
        self.assertEqual([("2026-09-09", 100.0), ("2026-09-10", 80.0)], hist["per_type"][PART])  # held only

    def test_stock_rows_match_the_snapshot_table(self):
        (row,) = stockpile.stock_rows([item(T2_ITEM, 3, 2)])
        self.assertEqual({"type_id": T2_ITEM, "class": "finished", "held": 3, "listed": 2, "unit_cost": 50.0,
                          "unit_market": 100.0}, row)


if __name__ == "__main__":
    unittest.main()
