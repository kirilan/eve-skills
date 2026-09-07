"""Order normalisation, fetching and the `orders` command.

Protects the two things that cannot be re-derived later: what an ESI order row *means* - the
terminal state has to be derived because ESI's history enum has no `filled` - and how one owner's
refusal degrades for everybody else, including the corporation case where two stored characters are
colleagues and would otherwise report one order book twice. No network."""

from __future__ import annotations

import csv
import io
import json
import unittest

from eve_skills import esi, orders, sso

from tests.fake_esi import (
    ADA, CORP_SHARED, MIRA, FakeEsiEnv, owner_order,
)


class NormalisationTests(unittest.TestCase):
    """Pure row translation: no client, so a malformed or partial ESI row is still handled."""

    def test_open_row_carries_its_book_identity_and_expiry(self):
        row = owner_order(700001, price=5.75, remain=100, total=250, issued="2026-09-01T06:30:00Z",
                          duration=30)
        order = orders.normalise(row, "char:91000001", "Ada Vane")
        self.assertEqual(order.order_id, 700001)
        self.assertEqual((order.owner_key, order.owner_name), ("char:91000001", "Ada Vane"))
        self.assertFalse(order.is_buy)
        self.assertEqual((order.type_id, order.region_id, order.location_id),
                         (34, 10000002, 60003760))
        self.assertEqual(order.price, 5.75)
        self.assertEqual((order.volume_remain, order.volume_total), (100, 250))
        self.assertEqual(order.filled, 150)          # what already left the order
        self.assertEqual(order.state, "open")        # a live book row is open by definition
        self.assertEqual(order.range, "station")
        # issued + duration days, as an ISO UTC stamp like every other timestamp this tool emits.
        self.assertEqual(order.expires, "2026-10-01T06:30:00Z")

    def test_zero_duration_row_has_no_expiry_date(self):
        # ESI reports duration 0 on rows that were never timed orders; dating their expiry at the
        # moment of issue would report every one of them as instantly lapsed.
        order = orders.normalise(owner_order(700104, duration=0), "char:91000001", "Ada Vane")
        self.assertIsNone(order.expires)

    def test_cancelled_row_stays_cancelled_whatever_it_had_sold(self):
        for remain in (0, 40):
            order = orders.normalise(owner_order(700101, state="cancelled", remain=remain,
                                                 total=100), "char:91000001", "Ada Vane", closed=True)
            self.assertEqual(order.state, "cancelled")   # the owner ended it, not the market

    def test_expired_and_fully_filled_row_is_reported_as_filled(self):
        order = orders.normalise(owner_order(700102, state="expired", remain=0, total=90),
                                 "char:91000001", "Ada Vane", closed=True)
        self.assertEqual(order.state, "filled")
        self.assertEqual(order.filled, 90)

    def test_expired_with_volume_left_is_expired_and_part_filled(self):
        order = orders.normalise(owner_order(700103, state="expired", remain=20, total=80),
                                 "char:91000001", "Ada Vane", closed=True)
        self.assertEqual(order.state, "expired")
        self.assertEqual(order.filled, 60)

    def test_keys_esi_leaves_out_entirely_become_none(self):
        row = owner_order(700002, buy=True, escrow=1550.0, min_volume=10)
        for key in ("escrow", "is_buy_order", "min_volume"):
            row.pop(key)
        order = orders.normalise(row, "char:91000001", "Ada Vane")
        self.assertFalse(order.is_buy)
        self.assertIsNone(order.escrow)
        self.assertIsNone(order.min_volume)

    def test_a_personal_row_can_be_funded_by_the_corporation(self):
        # Character endpoints flag this with `is_corporation`; corporation endpoints never send that
        # key at all, so the fetcher has to say it - a corporation row must not read as a member's.
        flagged = orders.normalise(owner_order(700050, corp_order=True), "char:91000001", "Ada Vane")
        own = orders.normalise(owner_order(700051), "char:91000001", "Ada Vane")
        corporate = orders.normalise(owner_order(700052, wallet_division=3), f"corp:{CORP_SHARED}",
                                     "Shared Ledger Holdings", corporation=True)
        self.assertTrue(flagged.is_corporation)
        self.assertFalse(own.is_corporation)
        self.assertTrue(corporate.is_corporation)


class OrdersFetchCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.env.install_orders()
        self.addCleanup(self.env.stop)
        self.client = esi.Esi(esi.default_user_agent(sso.load_config()))

    def record(self, char, without: str | None = None) -> dict:
        tok = dict(sso.get_access_token(char.character_id))
        if without:
            tok["scopes"] = [s for s in tok["scopes"] if s != without]
        return tok

    def public(self, char) -> dict:
        return self.client.get(f"/characters/{char.character_id}")


class FetchTests(OrdersFetchCase):
    def test_character_book_includes_every_history_page(self):
        book = orders.fetch_character(self.client, self.record(ADA))
        self.assertEqual(book.owner_key, "char:91000001")
        self.assertEqual(book.owner_name, "Ada Vane")
        self.assertEqual([o.order_id for o in book.open], [700001, 700002])
        # served over two pages: a fetcher that ignores X-Pages silently loses the last row.
        self.assertEqual([o.order_id for o in book.history], [700101, 700102, 700103])
        self.assertTrue(book.history_ok)

    def test_failed_history_keeps_the_live_book(self):
        self.env.server.get(f"/characters/{ADA.character_id}/orders/history", token=ADA.token,
                            error=(500, {"error": "shard unavailable"}))
        book = orders.fetch_character(self.client, self.record(ADA))
        self.assertEqual([o.order_id for o in book.open], [700001, 700002])
        self.assertEqual(book.history, ())
        self.assertFalse(book.history_ok)   # "unknown", never "everything closed"

    def test_corporation_book_is_keyed_by_the_corporation(self):
        book = orders.fetch_corporation(self.client, self.record(ADA), self.public(ADA))
        self.assertEqual(book.owner_key, f"corp:{CORP_SHARED}")
        self.assertEqual(book.owner_name, "Shared Ledger Holdings")
        self.assertEqual([o.order_id for o in book.open], [700201, 700202])
        self.assertEqual([o.order_id for o in book.history], [700301])
        # Who placed it and which wallet division holds the escrow belong to corporation rows.
        self.assertEqual(book.open[0].issued_by, MIRA.character_id)
        self.assertEqual(book.open[0].wallet_division, 2)

    def test_refusal_without_the_consent_asks_for_the_consent(self):
        self.env.server.get(f"/corporations/{CORP_SHARED}/orders", error=(403, {"error": "Forbidden"}))
        with self.assertRaises(orders.OrderAccess) as ctx:
            orders.fetch_corporation(self.client, self.record(ADA, without=orders.CORPORATION_SCOPE),
                                     self.public(ADA))
        self.assertIn("no corp-orders consent", str(ctx.exception))
        self.assertIn("login --scopes corp-orders", str(ctx.exception))

    def test_refusal_without_the_roles_consent_says_the_role_is_unverifiable(self):
        self.env.server.get(f"/corporations/{CORP_SHARED}/orders", error=(403, {"error": "Forbidden"}))
        with self.assertRaises(orders.OrderAccess) as ctx:
            orders.fetch_corporation(self.client, self.record(ADA, without=orders.ROLE_SCOPE),
                                     self.public(ADA))
        self.assertIn("role could not be verified", str(ctx.exception))

    def test_refusal_names_the_missing_in_game_role(self):
        # Full consent, and the roles endpoint says Ada holds neither of the two order roles.
        self.env.server.get(f"/corporations/{CORP_SHARED}/orders", error=(403, {"error": "Forbidden"}))
        with self.assertRaises(orders.OrderAccess) as ctx:
            orders.fetch_corporation(self.client, self.record(ADA), self.public(ADA))
        message = str(ctx.exception)
        self.assertIn("holds neither Accountant nor Trader in Shared Ledger Holdings", message)
        self.assertIn("ask a director", message)

    def test_refusal_does_not_blame_the_role_when_it_is_held(self):
        self.env.server.get(f"/corporations/{CORP_SHARED}/orders", error=(403, {"error": "Forbidden"}))
        self.env.server.get(f"/characters/{ADA.character_id}/roles", token=ADA.token,
                            doc={"roles": {"Accountant": 1, "Station_Manager": 0}, "titles": []})
        with self.assertRaises(orders.OrderAccess) as ctx:
            orders.fetch_corporation(self.client, self.record(ADA), self.public(ADA))
        # Ada holds the role, so telling her to go get it would send her to the wrong fix.
        self.assertIn("even though it holds Accountant", str(ctx.exception))


class OrdersCommandTests(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.env.install_orders()
        self.addCleanup(self.env.stop)

    def test_open_book_lists_every_consenting_character_and_totals_the_book(self):
        code, out, err = self.env.run(["orders"])
        self.assertEqual(code, 0)
        self.assertIn("Ada Vane", out)
        self.assertIn("Mira Solen", out)
        self.assertIn("Jita - Mradd", out)          # locations are named, not raw ids
        self.assertIn("624.90", out)                # 5.75*100 + 4.99*10 still to sell
        self.assertIn("1,550.00", out)              # Ada's buy escrow
        self.assertIn("60%", out)                   # 150 of 250 units already sold
        # Vela never consented: a hint, not a failure - and the other two still print.
        self.assertIn("Vela Krinn: no orders consent", out)
        self.assertIn("login --scopes orders", out)

    def test_one_characters_refusal_warns_without_hiding_the_others(self):
        self.env.server.get(f"/characters/{ADA.character_id}/orders", token=ADA.token,
                            error=(500, {"error": "shard unavailable"}))
        code, out, err = self.env.run(["orders"])
        self.assertEqual(code, 0)
        self.assertIn("warning: Ada Vane:", err)
        self.assertIn("Mira Solen", out)            # the surviving book still prints
        self.assertIn("49.90", out)                 # and the totals cover only what ESI answered
        self.assertNotIn("624.90", out)

    def test_corporation_book_is_fetched_once_per_corporation(self):
        code, out, _ = self.env.run(["orders", "--corp", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        # Ada and Mira are colleagues: the corp's rows are one book, not two copies of it.
        self.assertEqual(sorted(o["order_id"] for o in doc["orders"]), [700201, 700202])
        self.assertEqual([o["owner_key"] for o in doc["owners"]], [f"corp:{CORP_SHARED}"])
        self.assertEqual(doc["owner_kind"], "corporation")

    def test_corporation_view_adds_the_columns_only_corps_can_fill(self):
        code, out, _ = self.env.run(["orders", "--corp"])
        self.assertEqual(code, 0)
        self.assertIn("division", out)
        self.assertIn("issued by", out)
        self.assertNotIn("624.90", out)             # personal rows are not the corporation's book
        self.assertIn("Vela Krinn: no corp-orders consent", out)

    def test_corporation_rows_carry_the_division_and_the_issuing_member(self):
        code, out, _ = self.env.run(["orders", "--corp", "--csv"])
        self.assertEqual(code, 0)
        rows = {int(r["order_id"]): r for r in csv.DictReader(io.StringIO(out))}
        # The author of a corporation order is the member who placed it, not whoever fetched it.
        self.assertEqual(rows[700201]["issued_by_name"], "Mira Solen")
        self.assertEqual(rows[700201]["wallet_division"], "2")
        self.assertEqual(rows[700202]["issued_by_name"], "Ada Vane")
        # The book belongs to the corporation, and ESI does not say so on its own endpoints.
        self.assertEqual(rows[700201]["is_corporation"], "1")

    def test_a_corporation_funded_row_in_a_personal_book_is_marked(self):
        # ESI mixes corporation-wallet orders into a character's own book; unmarked, their ISK is
        # read as the member's and the sell book looks like personal wealth.
        self.env.server.get(f"/characters/{ADA.character_id}/orders", token=ADA.token,
                            doc=[owner_order(700050, type_id=36, price=9.99, corp_order=True)])
        code, out, _ = self.env.run(["orders"])
        self.assertEqual(code, 0)
        funded = next(line for line in out.splitlines() if "Pyerite" in line)
        self.assertIn("(corp)", funded)
        self.assertIn("* (corp) marks an order funded from a corporation wallet", out)
        hers = next(line for line in out.splitlines() if "Mira Solen" in line)
        self.assertNotIn("(corp)", hers)
        _, out, _ = self.env.run(["orders", "--csv"])
        rows = {int(r["order_id"]): r for r in csv.DictReader(io.StringIO(out))}
        self.assertEqual(rows[700050]["is_corporation"], "1")
        self.assertEqual(rows[700011]["is_corporation"], "0")

    def test_closed_book_shows_the_derived_states(self):
        code, out, _ = self.env.run(["orders", "--closed"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        sold_out = next(line for line in lines if "Pyerite" in line)
        self.assertIn("filled", sold_out)
        self.assertIn("90/90", sold_out)            # nothing left: it filled before it expired
        lapsed = next(line for line in lines if "Caldari Ship Blueprint" in line)
        self.assertIn("expired", lapsed)
        self.assertIn("60/80", lapsed)              # part-filled, then out of time
        called_off = next(line for line in lines if "Tritanium" in line)
        self.assertIn("cancelled", called_off)

    def test_json_output_keeps_ids_names_and_totals(self):
        code, out, err = self.env.run(["orders", "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)                       # stdout stays parseable...
        self.assertIn("Vela Krinn: no orders consent", err)   # ...so the hint goes to stderr
        self.assertEqual(doc["book"], "open")
        self.assertEqual(doc["owner_kind"], "character")
        self.assertEqual(doc["matched"], 3)
        rows = {o["order_id"]: o for o in doc["orders"]}
        self.assertEqual(rows[700001]["type_name"], "Tritanium")
        self.assertEqual(rows[700001]["location_name"], "Jita - Mradd")
        self.assertEqual(rows[700001]["region_name"], "The Forge")
        self.assertEqual(rows[700001]["filled"], 150)
        self.assertTrue(rows[700002]["is_buy"])
        self.assertEqual(rows[700002]["escrow"], 1550.0)
        self.assertEqual(rows[700002]["min_volume"], 10)
        totals = doc["totals"]
        self.assertEqual((totals["sell_orders"], totals["buy_orders"]), (2, 1))
        self.assertAlmostEqual(totals["sell_isk"], 624.9, places=2)
        self.assertAlmostEqual(totals["buy_escrow_isk"], 1550.0, places=2)
        self.assertEqual(totals["buy_escrow_missing"], 0)

    def test_closed_json_reports_nothing_at_stake(self):
        code, out, _ = self.env.run(["orders", "--closed", "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["book"], "closed")
        self.assertIsNone(doc["totals"])            # history has no live ISK exposure to add up
        self.assertEqual([o["state"] for o in doc["orders"]].count("filled"), 1)

    def test_side_and_type_filters_narrow_the_book(self):
        _, out, _ = self.env.run(["orders", "--buy", "--json"])
        self.assertEqual([o["order_id"] for o in json.loads(out)["orders"]], [700002])
        _, out, _ = self.env.run(["orders", "--sell", "--json"])
        self.assertEqual(sorted(o["order_id"] for o in json.loads(out)["orders"]), [700001, 700011])
        _, out, _ = self.env.run(["orders", "--buy", "--sell", "--json"])
        self.assertEqual(json.loads(out)["matched"], 3)   # both flags is the whole book, not nothing
        self.env.install_market()                   # --type by name needs /universe/ids
        _, out, _ = self.env.run(["orders", "--type", "Tritanium", "--json"])
        self.assertEqual({o["type_id"] for o in json.loads(out)["orders"]}, {34})

    def test_limit_trims_rows_but_not_the_totals(self):
        _, out, _ = self.env.run(["orders", "--limit", "1", "--json"])
        doc = json.loads(out)
        self.assertEqual([o["order_id"] for o in doc["orders"]], [700011])  # newest first
        self.assertEqual((doc["matched"], doc["shown"]), (3, 1))
        _, out, _ = self.env.run(["orders", "--limit", "1"])
        self.assertIn("hidden by --limit", out)
        self.assertIn("624.90", out)                # the portfolio figure still covers all three

    def test_limit_below_one_is_rejected(self):
        code, _, err = self.env.run(["orders", "--limit", "0"])
        self.assertEqual(code, 1)
        self.assertIn("--limit needs a positive number of rows", err)


if __name__ == "__main__":
    unittest.main()
