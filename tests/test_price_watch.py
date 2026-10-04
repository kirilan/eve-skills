"""Price tracking: the dip rule, its evidence and backtest, the book reduction, and the sync's storage.

The rule is tested on synthetic daily series - a flat price with a small wobble, then the event under
test - so each failure names a rule rather than a week of Jita. The sync runs against a stub client in
live ESI's row shapes, into a temporary ledger.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from eve_skills import esi, ledger_db, price_watch as pw

START = date(2026, 1, 1)
WOBBLE = (0.0, 1.0, -1.0, 0.5, -0.5)   # percent: a spread of about 1%, the floor


def series(prices: list[float]) -> list[pw.Day]:
    return [pw.Day(START + timedelta(days=i), p, p, p, 100) for i, p in enumerate(prices)]


def flat(n: int, level: float = 100.0) -> list[float]:
    return [level * (1 + WOBBLE[i % len(WOBBLE)] / 100) for i in range(n)]


class RuleTest(unittest.TestCase):
    def test_a_thin_history_has_no_baseline(self):
        days = series(flat(pw.MIN_ROWS - 1))
        self.assertIsNone(pw.baseline(days, days[-1].day))

    def test_the_spread_never_falls_under_its_floor(self):
        days = series([100.0] * 30)
        base = pw.baseline(days, days[-1].day)
        self.assertAlmostEqual(base.scale, 100.0 * pw.MIN_SCALE_PCT / 100)

    def test_one_low_day_is_a_dip_and_a_low_week_is_a_new_level(self):
        days = series(flat(30))
        base = pw.baseline(days, days[-1].day)
        self.assertEqual(pw.DIP, pw.classify(90.0, base)[0])
        self.assertEqual(pw.HIGH, pw.classify(110.0, base)[0])
        self.assertEqual(pw.NORMAL, pw.classify(100.5, base)[0])
        lower = series(flat(30) + [90.0] * pw.SHORT_DAYS)
        self.assertEqual(pw.FALLING, pw.classify(90.0, pw.baseline(lower, lower[-1].day))[0])

    def test_a_dip_that_comes_back_is_one_episode_with_its_recovery_and_return(self):
        days = series(flat(40) + [90.0, 91.0, 100.0] + flat(20))
        (episode,) = [e for e in pw.dip_episodes(days) if e.state == pw.DIP]
        self.assertEqual(START + timedelta(days=40), episode.start)
        self.assertEqual(2, episode.recovered_after)
        self.assertAlmostEqual(100.0 / 90.0 * 100 - 100, episode.hold_return_pct)
        ev = pw.evidence(pw.dip_episodes(days))
        self.assertEqual((1, 1, 2), (ev.dips, ev.recovered, ev.median_days))

    def test_a_dip_still_inside_its_horizon_is_not_evidence_yet(self):
        days = series(flat(40) + [90.0, 90.0])
        (episode,) = pw.dip_episodes(days)
        self.assertTrue(episode.censored)
        self.assertEqual(0, pw.evidence([episode]).dips)

    def test_holding_through_a_dip_that_recovers_beats_selling_daily(self):
        days = series(flat(40) + [90.0, 100.0] + flat(10))
        result = pw.backtest(days)
        self.assertEqual(1, result.dip_days)
        self.assertGreater(result.hold_dips, result.sell_daily)
        self.assertEqual(1, result.held_unit_days)

    def test_no_unit_is_held_past_the_horizon(self):
        days = series(flat(40) + [90.0, 91.0, 92.0, 90.5])
        result = pw.backtest(days, horizon=2)
        self.assertEqual(2, result.forced)


NOW = datetime(2026, 2, 20, 12, tzinfo=timezone.utc)


class AdviceTest(unittest.TestCase):
    def days(self, last: float) -> list[pw.Day]:
        n = (NOW.date() - START).days
        return series(flat(n - 1) + [last])

    def snapshot(self, price: float, age_hours: float = 1.0) -> dict:
        ts = (NOW - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"ts": ts, "best_sell": price}

    def test_a_dip_holds_at_most_two_weeks_of_our_sales_and_lists_the_rest(self):
        stock = pw.Stock(qty=100, unit_cost=50.0, sold_per_day=2.0)
        advice = pw.advise(1, self.days(100.0), self.snapshot(90.0), stock, 5.0, NOW)
        self.assertEqual((pw.DIP, pw.HOLD), (advice.state, advice.action))
        self.assertEqual((28, 72), (advice.hold_qty, advice.sell_qty))
        self.assertAlmostEqual(100.0, advice.target, delta=1.0)
        self.assertAlmostEqual(85.5, advice.net_now)

    def test_a_normal_or_high_price_sells_everything(self):
        stock = pw.Stock(qty=10, unit_cost=50.0, sold_per_day=1.0)
        for price, state in ((100.0, pw.NORMAL), (115.0, pw.HIGH)):
            advice = pw.advise(1, self.days(100.0), self.snapshot(price), stock, None, NOW)
            self.assertEqual((state, pw.SELL, 0, 10), (advice.state, advice.action, advice.hold_qty, advice.sell_qty))

    def test_a_stale_snapshot_gives_way_to_the_newest_daily_average(self):
        advice = pw.advise(1, self.days(100.0), self.snapshot(80.0, age_hours=pw.SNAPSHOT_MAX_AGE_HOURS + 1),
                           pw.Stock(0, None, 0.0), None, NOW)
        self.assertTrue(advice.price_source.startswith("history"))
        self.assertEqual(pw.NORMAL, advice.state)


class BookTest(unittest.TestCase):
    def test_the_book_is_reduced_to_this_station_and_marks_our_orders(self):
        rows = [
            {"order_id": 1, "location_id": 60003760, "is_buy_order": False, "price": 100.0, "volume_remain": 5},
            {"order_id": 2, "location_id": 60003760, "is_buy_order": False, "price": 100.9, "volume_remain": 7},
            {"order_id": 3, "location_id": 60003760, "is_buy_order": False, "price": 104.0, "volume_remain": 11},
            {"order_id": 4, "location_id": 60003760, "is_buy_order": False, "price": 120.0, "volume_remain": 13},
            {"order_id": 5, "location_id": 60003760, "is_buy_order": True, "price": 95.0, "volume_remain": 3},
            {"order_id": 6, "location_id": 1022734985679, "is_buy_order": False, "price": 50.0, "volume_remain": 99},
        ]
        snap = pw.reduce_book(rows, 60003760, 7, "t", "p", our_orders={3})
        self.assertEqual((100.0, 95.0), (snap["best_sell"], snap["best_buy"]))
        self.assertEqual((25, 3, 12, 12), (snap["sell_units"], snap["sell_orders"], snap["sell_units_1pct"],
                                            snap["sell_units_5pct"]))
        self.assertEqual((11, 104.0), (snap["our_sell_units"], snap["our_best_sell"]))

    def test_our_own_cheapest_ask_is_not_read_as_the_market(self):
        rows = [{"order_id": 1, "location_id": 60003760, "is_buy_order": False, "price": 90.0, "volume_remain": 5},
                {"order_id": 2, "location_id": 60003760, "is_buy_order": False, "price": 100.0, "volume_remain": 7}]
        snap = pw.reduce_book(rows, 60003760, 7, "t", "p", our_orders={1})
        self.assertEqual((100.0, 90.0, 7), (snap["best_sell"], snap["our_best_sell"], snap["sell_units"]))


class StubClient:
    """ESI as the sync reads it: history rows, and a station book with its Last-Modified."""

    def __init__(self, history: dict, books: dict):
        self.history, self.books, self.calls = history, books, []

    def now(self) -> datetime:
        return NOW

    def get(self, path: str):
        self.calls.append(path)
        return self.history[int(path.rsplit("=", 1)[1])]

    def get_meta(self, path: str):
        self.calls.append(path)
        return self.books[int(path.rsplit("=", 1)[1])], esi.Meta(last_modified=NOW.timestamp() - 120)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = ledger_db.connect(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.conn.close)

    def sale(self, tid: int, type_id: int, day: str) -> None:
        with self.conn:
            ledger_db.insert_transactions(self.conn, "corp:1:1", [
                {"transaction_id": tid, "date": day, "type_id": type_id, "quantity": 1, "unit_price": 10.0,
                 "is_buy": False}])

    def test_the_watchlist_is_what_we_sold_often_plus_manual_changes(self):
        for tid in range(3):
            self.sale(tid, 500, "2026-02-10T00:00:00Z")
        self.sale(10, 600, "2026-02-10T00:00:00Z")                 # once is not often
        for tid in range(20, 23):
            self.sale(tid, 700, "2025-10-01T00:00:00Z")            # often, but long ago
        self.assertEqual({500: "sales"}, pw.watchlist(self.conn, NOW))
        with self.conn:
            ledger_db.set_watchlist(self.conn, 600, "add", "x")
            ledger_db.set_watchlist(self.conn, 500, "exclude", "x")
        self.assertEqual({600: "manual"}, pw.watchlist(self.conn, NOW))
        with self.conn:
            ledger_db.set_watchlist(self.conn, 500, None, "x")
        self.assertEqual({500: "sales", 600: "manual"}, pw.watchlist(self.conn, NOW))

    def test_sync_stores_history_once_and_a_snapshot_per_esi_book(self):
        history = {500: [{"date": "2026-02-18", "average": 10.0, "highest": 11.0, "lowest": 9.0, "volume": 4,
                          "order_count": 2},
                         {"date": "2026-02-19", "average": 10.5, "highest": 11.0, "lowest": 9.5, "volume": 6,
                          "order_count": 3}]}
        books = {500: [{"order_id": 1, "location_id": pw.HUB.station_id, "is_buy_order": False, "price": 10.4,
                        "volume_remain": 3}]}
        client = StubClient(history, books)
        result = pw.sync(self.conn, client, types=[500])
        self.assertEqual((1, 2, 1, []), (result.history_fetched, result.history_new_days, result.snapshots_new,
                                         result.problems))
        self.assertEqual(["2026-02-18", "2026-02-19"],
                         [r["day"] for r in ledger_db.market_history(self.conn, pw.HUB.region_id, 500)])
        # History reaches yesterday, and the book is the same ESI document: nothing new is stored.
        again = pw.sync(self.conn, client, types=[500])
        self.assertEqual((0, 0), (again.history_fetched, again.snapshots_new))
        self.assertEqual(1, len(ledger_db.book_snapshots(self.conn, pw.HUB.station_id, 500)))

    def test_history_that_is_still_behind_is_not_reread_every_poll(self):
        client = StubClient({500: [{"date": "2026-02-10", "average": 10.0, "volume": 1}]}, {500: []})
        pw.sync(self.conn, client, types=[500], book=False)
        pw.sync(self.conn, client, types=[500], book=False)
        self.assertEqual(1, sum(1 for c in client.calls if "/history" in c))


if __name__ == "__main__":
    unittest.main()
