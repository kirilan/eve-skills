"""Price ticks, the real floor of a sell book and an order's standing (`eve_skills.pricing`).

The worked examples are the Jita pricing rounds of 2026-09-27 that set the rule. No network."""

from __future__ import annotations

import unittest

from eve_skills import pricing


def book(*rows):
    return [{"order_id": 900 + i, "price": price, "volume": volume} for i, (price, volume) in enumerate(rows)]


class TickTests(unittest.TestCase):
    def test_four_significant_digits_below(self):
        self.assertEqual(3_324_000, pricing.tick_below(3_325_000))
        self.assertEqual(13_200_000, pricing.tick_below(13_210_000))
        self.assertEqual(912.0, pricing.tick_below(912.1))

    def test_crossing_a_power_of_ten_lands_on_the_finer_grid(self):
        self.assertEqual(999_000, pricing.tick_below(1_000_000))
        self.assertEqual(1_000_000, pricing.tick_above(999_900))

    def test_never_below_one_cent(self):
        self.assertEqual(0.01, pricing.tick_below(0.01))
        self.assertEqual(0.02, pricing.tick_above(0.01))


class RealFloorTests(unittest.TestCase):
    def test_one_cheap_unit_ahead_of_a_real_book_is_ignored(self):
        # Medium Explosive SR II: 1 @ 2.214M, then 20 @ 2.926M; we list 20, 138/day.
        sells = book((2_214_000, 1), (2_926_000, 20), (2_927_000, 48))
        price, skipped = pricing.sell_price(sells, 138, 20)
        self.assertEqual(2_925_000, price)
        self.assertEqual([2_214_000], [s["price"] for s in skipped])

    def test_a_thick_cheap_block_is_the_market(self):
        # 883 Broken Drone Transceivers against our 5,209: undercut them, do not skip.
        sells = book((2_748, 400), (2_760, 483), (2_900, 1_000))
        self.assertEqual(2_747, pricing.sell_price(sells, 3_000, 5_209)[0])

    def test_a_unit_only_a_tick_under_the_next_is_undercut(self):
        sells = book((1_258_000, 1), (1_259_000, 50))
        self.assertEqual(1_257_000, pricing.sell_price(sells, 400, 50)[0])

    def test_the_sliver_is_measured_against_our_stack(self):
        # Five cheap units are thin on the market but not next to a stack of ten.
        sells = book((100.0, 5), (150.0, 100))
        self.assertEqual(99.9, pricing.sell_price(sells, 10_000, 10)[0])
        self.assertEqual(149.9, pricing.sell_price(sells, 10_000, 1_000)[0])


class StandingTests(unittest.TestCase):
    def test_own_orders_never_count_as_competition(self):
        sells = book((5.0, 10), (6.0, 10))
        doc = pricing.standing(False, 5.0, 10, sells, {900}, 100)
        self.assertEqual(("cheapest", 6.0, 0, None), (doc["status"], doc["best_other"], doc["units_ahead"],
                                                      doc["suggest"]))

    def test_undercut_sell_reports_units_and_days_ahead(self):
        sells = book((4.0, 300), (5.0, 10))
        doc = pricing.standing(False, 5.0, 10, sells, {901}, 100)
        self.assertEqual(("undercut", 300, 3.0, 3.99),
                         (doc["status"], doc["units_ahead"], doc["days_ahead"], doc["suggest"]))

    def test_only_a_sliver_ahead_leaves_the_order_alone(self):
        sells = book((3.0, 1), (5.0, 100), (5.5, 500))
        doc = pricing.standing(False, 5.0, 100, sells, {901}, None)
        self.assertEqual(("behind sliver", None, 1), (doc["status"], doc["suggest"], doc["units_ahead"]))

    def test_outbid_buy_suggests_one_tick_over(self):
        buys = book((3.2, 1000), (3.1, 500))
        doc = pricing.standing(True, 3.1, 500, buys, {901}, 50)
        self.assertEqual(("outbid", 3.21, 1000, 20.0),
                         (doc["status"], doc["suggest"], doc["units_ahead"], doc["days_ahead"]))

    def test_no_competitor_is_alone(self):
        self.assertEqual("alone", pricing.standing(True, 3.1, 5, book((3.1, 5)), {900}, 1)["status"])


if __name__ == "__main__":
    unittest.main()
