"""The dashboard: the document it assembles from a ledger replay, and the read-only web server.

The document tests reuse the synthetic industry of `test_ledger`; the server tests run the real
handler on a free local port with a canned document, so no ESI and no refresh thread are involved.
"""

from __future__ import annotations

import argparse
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from unittest import mock

from eve_skills import cmd_dashboard, dashboard, ledger

from .test_ledger import CHAR, MINERAL, PART, T1_BP, T2_BP, T2_ITEM, copy, facts, job, journal, replay, stamp, tx

HEADER = {"cutover": "2026-09-01", "last_sync": stamp(8), "warnings": [], "notes": ["an assumption"]}


def book() -> ledger.Book:
    return replay(facts(
        transactions=[tx(1, PART, 50, 10.0, buy=True), tx(8, T2_ITEM, 10, 200.0, buy=False),
                      tx(8, MINERAL, 10, 5.0, buy=False)],
        jobs=[job(1, ledger.INVENTION, blueprint_type=T1_BP, product=T2_BP, runs=1, start=stamp(2),
                  end=stamp(3), successes=1, cost=10.0),
              job(2, ledger.MANUFACTURING, blueprint_type=T2_BP, product=T2_ITEM, runs=10, start=stamp(4),
                  end=stamp(5), cost=0.0)],
        entries=[journal(1, "office_rental_fee", -100.0)],
        blueprints=[copy(1, T2_BP, 2, 4, seen=stamp(4))], opening={MINERAL: 3.0}))


STATUS = {"owners": [{
    "warnings": [],
    "documents": {"jobs": {"last_modified": stamp(9)}},
    "characters": [{"character_name": "Builder", "slots": {
        "manufacturing": {"used": 10, "ready": 2, "free": 0, "max": 10, "next_end": stamp(9, 14)},
        "science": {"used": 1, "ready": 0, "free": 9, "max": 10, "next_end": None}}}],
    "jobs": {"ready": [{"installer_name": "Builder", "activity": "manufacturing", "product": "Widget II",
                        "runs": "10 x1", "count": 2, "end_start": stamp(9), "end_end": stamp(9)}],
             "running": [{"installer_name": "Builder", "activity": "invention", "product": "Widget II Blueprint",
                          "runs": "14 x100", "count": 1, "end_start": stamp(10), "end_end": stamp(10)},
                         {"installer_name": "Builder", "activity": "manufacturing", "product": "Widget II",
                          "runs": "10 x1", "count": 8, "end_start": stamp(9, 14), "end_end": stamp(9, 15)}]},
}]}

ORDERS = {"generated": stamp(9), "orders": [
    {"order_id": 1, "owner_name": "Seller", "is_buy": False, "type_name": "Widget II", "state": "open",
     "location_name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "region_name": "The Forge",
     "price": 210.0, "volume_total": 10, "volume_remain": 4,
     "check": {"status": "undercut", "best_other": 205.0, "units_ahead": 30, "days_ahead": 0.5, "suggest": 204.0}},
    {"order_id": 2, "owner_name": "Seller", "is_buy": False, "type_name": "Old", "state": "expired",
     "price": 1.0, "volume_total": 1, "volume_remain": 1},
]}


class DocumentTests(unittest.TestCase):
    def doc(self, **live):
        return dashboard.document(book(), HEADER, since="2026-09-01", now=stamp(9), today=date(2026, 9, 9),
                                  names=lambda ids: {T2_ITEM: "Widget II", 60003760: "Jita IV - Moon 4"},
                                  refresh_minutes=60, **live)

    def test_ranges_never_start_before_the_operation(self):
        starts = dashboard.range_starts(date(2026, 9, 9), "2026-09-05")
        self.assertEqual({"7d": "2026-09-05", "30d": "2026-09-05", "all": "2026-09-05"}, starts)
        self.assertEqual("2026-09-03", dashboard.range_starts(date(2026, 9, 9), "2026-08-01")["7d"])

    def test_figures_match_the_ledger_and_leave_disposal_out(self):
        doc = self.doc()
        whole = doc["ranges"]["all"]
        (row,) = ledger.pnl(book(), since="2026-09-01")
        self.assertAlmostEqual(row["net_profit"], whole["summary"]["net_profit"])
        self.assertEqual(10, whole["summary"]["units_sold"])
        self.assertEqual(["Widget II"], [p["name"] for p in whole["products"]])      # no MINERAL: disposal
        (market,) = whole["markets"]
        self.assertEqual("Jita IV - Moon 4", market["name"])
        self.assertAlmostEqual(whole["summary"]["gross_profit"], market["profit"])
        self.assertEqual(["an assumption"], doc["assumptions"])

    def test_the_timeline_has_every_day_and_a_running_total(self):
        days = self.doc()["days"]
        self.assertEqual(9, len(days))                       # 1st..9th, quiet days included
        self.assertEqual("2026-09-01", days[0]["period"])
        self.assertAlmostEqual(-100.0, days[0]["net_profit"])   # rent
        self.assertAlmostEqual(sum(d["net_profit"] for d in days), days[-1]["cumulative_net"])
        weeks = self.doc()["weeks"]
        self.assertEqual(["2026-W36", "2026-W37"], [w["period"] for w in weeks])

    def test_live_sections_are_reduced_and_errors_kept(self):
        doc = self.doc(status=STATUS, orders=ORDERS)
        jobs = doc["jobs"]
        self.assertIsNone(jobs["error"])
        self.assertEqual(stamp(9), jobs["as_of"])
        self.assertEqual(2, jobs["characters"][0]["manufacturing"]["ready"])
        self.assertEqual([stamp(9, 14), stamp(10)], [j["end_start"] for j in jobs["running"]])  # by end
        (only,) = doc["orders"]["orders"]                       # the expired one is dropped
        self.assertEqual(("sell", "undercut", 204.0), (only["side"], only["status"], only["suggest"]))
        failed = self.doc(status_error="ESI down", orders_error="no consent")
        self.assertEqual(("ESI down", []), (failed["jobs"]["error"], failed["jobs"]["characters"]))
        self.assertEqual("no consent", failed["orders"]["error"])

    def test_the_stock_section_is_passed_through_with_names_filled_in(self):
        stock = {"error": None, "goods": [{"type_id": T2_ITEM, "name": None, "status": "slow"}],
                 "materials": [{"type_id": PART, "name": "Part", "status": "low"}]}
        doc = self.doc(stock=stock)
        self.assertEqual("Widget II", doc["stockpile"]["goods"][0]["name"])
        self.assertEqual("Part", doc["stockpile"]["materials"][0]["name"])
        self.assertEqual("no stock analysis", self.doc()["stockpile"]["error"])

class LiveCommandTests(unittest.TestCase):
    """The live sections re-run this tool; the frozen Windows build has no interpreter to pass -m to."""

    def run_live(self, frozen):
        done = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(cmd_dashboard.sys, "executable", "/x/runner"), \
                mock.patch.object(cmd_dashboard.sys, "frozen", frozen, create=True), \
                mock.patch.object(cmd_dashboard.subprocess, "run", return_value=done) as run:
            self.assertEqual(({"ok": True}, None), cmd_dashboard._live("orders", "--check"))
        return run.call_args.args[0]

    def test_an_installed_package_runs_the_module(self):
        self.assertEqual(["/x/runner", "-m", "eve_skills", "orders", "--check", "--json"], self.run_live(False))

    def test_a_frozen_executable_runs_itself(self):
        self.assertEqual(["/x/runner", "orders", "--check", "--json"], self.run_live(True))


def _args(**over):
    base = dict(host="127.0.0.1", port=0, refresh=60, no_sync=True, no_live=True, since=None,
                orders_char=None, verbose=False)
    return argparse.Namespace(**{**base, **over})


class ServerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(cmd_dashboard, "_cache_path", return_value="/nonexistent/dashboard.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server, self.state = cmd_dashboard.make_server(_args())
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.headers.get("Content-Type"), res.read()
        except urllib.error.HTTPError as err:
            with err:
                return err.code, err.headers.get("Content-Type"), err.read()

    def test_the_page_is_served(self):
        code, kind, body = self.get("/")
        self.assertEqual((200, "text/html; charset=utf-8"), (code, kind))
        self.assertIn(b"api/dashboard", body)

    def test_the_document_is_503_until_the_first_build_then_served(self):
        code, _kind, body = self.get("/api/dashboard")
        self.assertEqual(503, code)
        self.assertTrue(json.loads(body)["building"])
        self.state.body = b'{"ok": true}'
        self.assertEqual((200, b'{"ok": true}'), self.get("/api/dashboard")[::2])

    def test_unknown_paths_are_404_and_nothing_else_is_read(self):
        for path in ("/../../etc/passwd", "/data/dashboard.html", "/api/refresh"):
            self.assertEqual(404, self.get(path)[0], path)
        self.assertEqual(404, self.get("/api/dashboard", method="POST")[0])

    def test_a_manual_refresh_works_right_after_boot(self):
        # time.monotonic() counts from boot: a fresh machine must not read "no refresh yet" as "just now"
        with mock.patch.object(cmd_dashboard.time, "monotonic", return_value=12.0):
            self.assertEqual(202, self.get("/api/refresh", method="POST")[0])

    def test_a_manual_refresh_is_rate_limited(self):
        code, _kind, body = self.get("/api/refresh", method="POST")
        self.assertEqual(202, code)
        self.assertTrue(self.state.wake.is_set())
        self.state.last_attempt = cmd_dashboard.time.monotonic()      # as the thread does when it starts
        self.assertEqual(429, self.get("/api/refresh", method="POST")[0])


if __name__ == "__main__":
    unittest.main()
