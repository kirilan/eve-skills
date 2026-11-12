"""Price tracking: the dip rule, its evidence and backtest, the book reduction, and the sync's storage.

The rule is tested on synthetic daily series - a flat price with a small wobble, then the event under
test - so each failure names a rule rather than a week of Jita. The sync runs against a stub client in
live ESI's row shapes, into a temporary ledger.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from eve_skills import cmd_prices, esi, ledger_db, market, price_watch as pw

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


class BuySideTest(unittest.TestCase):
    """The dip rule read by a buyer: extra units on a dip, scaled by depth and capped three ways."""

    def days(self) -> list[pw.Day]:
        return series(flat(40))

    def test_the_multiple_grows_with_the_depth_of_the_dip(self):
        self.assertEqual([1.5, 1.5, 2.0, 3.0, 3.0, 4.0],
                         [pw.dip_multiple(d) for d in (2.0, 4.9, 5.0, 10.0, 19.9, 35.0)])

    def test_a_dip_buys_the_trip_need_times_the_multiple_from_asks_under_the_dip_line(self):
        # 12% under normal: x3, so 2 x 30 more on top of the trip's 30, all under the dip line.
        asks = [(88.0, 1000), (99.0, 1000)]
        advice = pw.buy_advice(self.days(), asks, need=30, buy=30, held=0, buffer=0, cover_days=3)
        self.assertEqual((pw.DIP, 3.0, 60, "multiple"), (advice.state, advice.multiple, advice.extra, advice.limit))
        self.assertAlmostEqual(60 * 88.0, advice.extra_isk)

    def test_a_dip_never_takes_stock_past_two_weeks_of_use(self):
        # Use is 10 a day: 140 units cover 14 days; 100 owned plus the 30 bought leave room for 10.
        asks = [(75.0, 1000)]
        advice = pw.buy_advice(self.days(), asks, need=30, buy=30, held=100, buffer=0, cover_days=3)
        self.assertEqual((4.0, 10, "cover"), (advice.multiple, advice.extra, advice.limit))

    def test_a_thin_sliver_under_the_line_is_not_a_reason_to_buy_more(self):
        # 40 cheap units: the trip's own 30 take the first of them, so only 10 extra are a bargain.
        asks = [(88.0, 40), (100.0, 1000)]
        advice = pw.buy_advice(self.days(), asks, need=30, buy=30, held=0, buffer=0, cover_days=3)
        self.assertEqual((10, "book"), (advice.extra, advice.limit))
        self.assertAlmostEqual(10 * 88.0, advice.extra_isk)

    def test_a_high_price_drops_the_buffer_and_normal_or_falling_buy_nothing_more(self):
        high = pw.buy_advice(self.days(), [(115.0, 1000)], need=30, buy=230, held=0, buffer=200, cover_days=3)
        self.assertEqual((pw.HIGH, 0, 200), (high.state, high.extra, high.skip_buffer))
        normal = pw.buy_advice(self.days(), [(100.0, 1000)], need=30, buy=30, held=0, buffer=0, cover_days=3)
        self.assertEqual((pw.NORMAL, 0, 0), (normal.state, normal.extra, normal.skip_buffer))
        lower = series(flat(30) + [88.0] * pw.SHORT_DAYS)
        falling = pw.buy_advice(lower, [(88.0, 1000)], need=30, buy=30, held=0, buffer=0, cover_days=3)
        self.assertEqual((pw.FALLING, 0), (falling.state, falling.extra))

    def test_no_ask_or_no_baseline_is_not_judged(self):
        self.assertIsNone(pw.buy_advice(self.days(), [], 30, 30, 0, 0, 3).state)
        self.assertIsNone(pw.buy_advice(series(flat(5)), [(80.0, 10)], 30, 30, 0, 0, 3).state)


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

    def test_inputs_we_buy_often_are_watched_for_the_buy_side_unless_we_also_sell_them(self):
        def buy(tid: int, type_id: int, wallet: str = "corp:1:1") -> None:
            with self.conn:
                ledger_db.insert_transactions(self.conn, wallet, [
                    {"transaction_id": tid, "date": "2026-02-10T00:00:00Z", "type_id": type_id, "quantity": 5,
                     "unit_price": 10.0, "is_buy": True}])
        for tid in range(100, 103):
            buy(tid, 800)
            buy(tid + 10, 500)
            self.sale(tid + 20, 500, "2026-02-10T00:00:00Z")
        buy(130, 900)                                                # once is not often
        for tid in range(140, 143):
            buy(tid, 950, wallet="char:7")                           # a member's own fittings
        self.assertEqual({800: "buys", 500: "sales"}, pw.watchlist(self.conn, NOW))

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


STRUCTURE = market.Hub("example-hub", "Example Hub", 1_040_000_000_009, 10_000_099, 30_000_099, structure=True,
                       reader="Carol Hauler")


class HubSyncTest(unittest.TestCase):
    """A second hub: the operation file's structure market, read once per sync for every type."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = ledger_db.connect(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.conn.close)
        day = [{"date": "2026-02-19", "average": 10.0, "highest": 11.0, "lowest": 9.0, "volume": 4, "order_count": 2}]
        self.client = StubClient({500: day, 600: day}, {500: [{"order_id": 1, "location_id": pw.HUB.station_id,
                                                               "is_buy_order": False, "price": 10.4,
                                                               "volume_remain": 3}], 600: []})
        self.structure_rows = [
            {"order_id": 7, "type_id": 500, "location_id": STRUCTURE.station_id, "is_buy_order": False,
             "price": 12.0, "volume_remain": 5},
            {"order_id": 8, "type_id": 600, "location_id": STRUCTURE.station_id, "is_buy_order": False,
             "price": 20.0, "volume_remain": 1}]

    def test_sync_stores_history_and_books_for_both_hubs(self):
        reads = []

        def book(client, structure_id, reader):
            reads.append((structure_id, reader))
            return self.structure_rows, esi.Meta(last_modified=NOW.timestamp() - 60)

        with mock.patch.object(market, "structure_book", side_effect=book):
            result = pw.sync(self.conn, self.client, types=[500, 600], hubs=[pw.HUB, STRUCTURE])
        self.assertEqual([], result.problems)
        self.assertEqual(["jita", "example-hub"], result.hubs)
        self.assertEqual([(STRUCTURE.station_id, "Carol Hauler")], reads)          # once for every type
        for region in (pw.HUB.region_id, STRUCTURE.region_id):
            self.assertEqual(1, len(ledger_db.market_history(self.conn, region, 500)))
        self.assertEqual(12.0, ledger_db.book_snapshots(self.conn, STRUCTURE.station_id, 500)[-1]["best_sell"])
        self.assertEqual(20.0, ledger_db.book_snapshots(self.conn, STRUCTURE.station_id, 600)[-1]["best_sell"])
        self.assertEqual(10.4, ledger_db.book_snapshots(self.conn, pw.HUB.station_id, 500)[-1]["best_sell"])

    def test_a_refused_structure_read_keeps_the_jita_part(self):
        refused = RuntimeError("ESI refused the market of structure 1040000000009 to Carol Hauler")
        with mock.patch.object(market, "structure_book", side_effect=refused):
            result = pw.sync(self.conn, self.client, types=[500], hubs=[pw.HUB, STRUCTURE])
        self.assertEqual(1, len(result.problems))
        self.assertIn("book example-hub", result.problems[0])
        self.assertEqual(1, result.snapshots_new)
        self.assertEqual(1, len(ledger_db.book_snapshots(self.conn, pw.HUB.station_id, 500)))
        # the command still succeeds - a warning line, exit status 0 - even when Jita has nothing new
        with self.conn:
            ledger_db.set_watchlist(self.conn, 500, "add", "x")
        args = SimpleNamespace(hub=None, no_history=False, no_book=False, json=False, quiet=True)
        path, connect = os.path.join(self.dir.name, "ledger.sqlite3"), ledger_db.connect
        with mock.patch.object(cmd_prices, "_sync_hubs", return_value=([pw.HUB, STRUCTURE], [])), \
                mock.patch.object(cmd_prices.ledger_db, "connect", side_effect=lambda: connect(path)), \
                mock.patch.object(cmd_prices.cmd_ledger, "_client", return_value=self.client), \
                mock.patch.object(market, "structure_book", side_effect=refused), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            status = cmd_prices.cmd_sync(args)
        self.assertEqual(0, status)
        self.assertIn("problem: book example-hub", err.getvalue())

    def test_default_hubs_are_jita_and_the_site_hubs(self):
        op = SimpleNamespace(sites={"home": SimpleNamespace(hub="example-hub"), "corp": SimpleNamespace(hub=None)})
        with mock.patch("eve_skills.operation.load", return_value=op), \
                mock.patch.object(market, "configured_hubs", return_value={"example-hub": STRUCTURE}):
            hubs, problems = cmd_prices._sync_hubs(None)
            self.assertEqual((["jita", "example-hub"], []), ([h.key for h in hubs], problems))
            hubs, problems = cmd_prices._sync_hubs(["amarr", "nowhere"])
        self.assertEqual(["amarr"], [h.key for h in hubs])
        self.assertIn("unknown hub 'nowhere'", problems[0])


class ThinTest(unittest.TestCase):
    def test_another_hub_with_few_trading_days_gets_no_advice(self):
        days = series(flat(40))
        sparse = [d for i, d in enumerate(days) if i % 5 == 0]           # 8 trading days in the last 30
        stock = pw.Stock(qty=10, unit_cost=50.0, sold_per_day=1.0)
        advice = pw.advise(500, sparse, None, stock, 5.0, NOW)
        thin = cmd_prices._thin(advice, sparse, NOW, "Example Region")
        self.assertEqual((pw.THIN, "-", 0.0, 10), (thin.state, thin.action, thin.hold_qty, thin.sell_qty))
        self.assertIn("thin: ", thin.reason)
        busy = pw.advise(500, days, None, stock, 5.0, NOW)
        self.assertIs(busy, cmd_prices._thin(busy, days, NOW, "Example Region"))


if __name__ == "__main__":
    unittest.main()
