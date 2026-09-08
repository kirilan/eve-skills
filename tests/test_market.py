"""Market price lookup: name resolution, book reduction, cluster folding, and the `market` command.

Runs against tests/fake_esi.py's in-process ESI, whose order books are served with real RFC 1123
`Last-Modified` headers - freshness has to be pinned against the same clock the client reads, not
against an ISO string the real endpoint never sends."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from eve_skills import esi, exports, market

from tests.fake_esi import (
    ABYSSAL_REGION, INV_TYPE_CONTAINER, INV_TYPE_SHIP, MARKET_BROKEN, MARKET_BOOK_AGE,
    MARKET_DOMAIN, MARKET_FORGE, MARKET_PLEX, MARKET_PRICES_AGE, MARKET_UNTRADED, STATION_AMARR,
    STATION_FORGE_OTHER, STATION_JITA, WORMHOLE_REGION, FakeEsiEnv, http_date,
)


class MarketTestCase(unittest.TestCase):
    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()       # /universe/names
        self.env.install_market()     # ids, region list, two books, one dead shard, history
        self.addCleanup(self.env.stop)
        self.client = esi.Esi("unittest")

    def now(self) -> float:
        return self.client.now().timestamp()


class ResolveTests(MarketTestCase):
    def test_type_name_reads_only_the_inventory_bucket(self):
        # The fixture answers "Tritanium" with a character as well; taking whichever bucket came
        # back first would price a player.
        self.assertEqual(market.resolve_type(self.client, "Tritanium"), (34, "Tritanium"))

    def test_numeric_type_id_is_not_looked_up_by_name(self):
        self.assertEqual(market.resolve_type(self.client, "34"), (34, "Tritanium"))
        self.assertEqual(self.env.server.calls_to("/universe/ids"), [])

    def test_unknown_type_names_say_that_esi_matches_exactly(self):
        with self.assertRaisesRegex(RuntimeError, r"no type named 'Tritaniom'"):
            market.resolve_type(self.client, "Tritaniom")

    def test_region_resolves_by_name_and_by_id(self):
        forge = (MARKET_FORGE, "The Forge")
        self.assertEqual(market.resolve_region(self.client, "The Forge"), forge)
        self.assertEqual(market.resolve_region(self.client, str(MARKET_FORGE)), forge)


class QuoteTests(MarketTestCase):
    def test_regional_book_reduces_both_sides(self):
        quote = market.quote(self.client, 34, market.Scope(MARKET_FORGE, "The Forge"))
        self.assertEqual((quote.min_sell, quote.max_buy), (4.98, 4.30))
        # volume_remain, not volume_total: what is left on the book is what can be traded.
        self.assertEqual((quote.sell_volume, quote.buy_volume), (1250, 920))
        # Two sells and two buys of type 34; the Pyerite and blueprint rows stay out.
        self.assertEqual((quote.sell_orders, quote.buy_orders), (2, 2))
        self.assertEqual(quote.best_sell_location, STATION_FORGE_OTHER)
        self.assertAlmostEqual(quote.spread, 0.68, places=9)
        self.assertAlmostEqual(quote.margin_pct, 15.813953488372095, places=9)

    def test_station_scope_only_counts_orders_at_that_station(self):
        hub = market.HUBS["jita"]
        scope = market.Scope(hub.region_id, "Jita 4-4 (station)", hub.system_id, hub.station_id)
        quote = market.quote(self.client, 34, scope)
        self.assertEqual((quote.min_sell, quote.max_buy), (5.05, 4.20))
        self.assertEqual((quote.sell_orders, quote.buy_orders), (1, 1))
        self.assertEqual(quote.best_sell_location, STATION_JITA)
        self.assertEqual(quote.best_buy_location, STATION_JITA)

    def test_a_side_with_no_orders_is_missing_rather_than_zero(self):
        # Type 590 has a buy order and no sell order: "somebody sells it for 0 ISK" is a lie.
        quote = market.quote(self.client, 590, market.Scope(MARKET_FORGE, "The Forge"))
        self.assertIsNone(quote.min_sell)
        self.assertEqual(quote.max_buy, 3.10)
        self.assertIsNone(quote.spread)
        self.assertIsNone(quote.margin_pct)

    def test_an_empty_book_quotes_nothing(self):
        quote = market.quote(self.client, 40520, market.Scope(MARKET_DOMAIN, "Domain"))
        self.assertIsNone(quote.min_sell)
        self.assertIsNone(quote.max_buy)
        self.assertEqual((quote.sell_volume, quote.buy_volume), (0, 0))

    def test_freshness_comes_from_the_response_not_from_the_clock(self):
        quote = market.quote(self.client, 34, market.Scope(MARKET_FORGE, "The Forge"))
        age = self.now() - quote.meta.last_modified
        self.assertGreater(age, 100)      # the fixture serves a book two minutes old
        self.assertLess(age, 240)
        line = market.freshness_line(quote.meta, self.now())
        self.assertIn("as of", line)
        self.assertIn("ago; ESI refreshes the book every 5 min", line)

    def test_a_response_without_a_stamp_says_so(self):
        self.assertIn("unknown", market.freshness_line(esi.Meta(), self.now()))


class ClusterTests(MarketTestCase):
    def test_market_regions_skips_space_that_has_no_markets(self):
        # Wormhole and abyssal regions are listed by /universe/regions but never trade; scanning
        # them would add ~44 pointless requests to every cluster scan.
        self.assertEqual(market.market_regions(self.client),
                         [(MARKET_DOMAIN, "Domain"), (MARKET_BROKEN, "Heimatar"), (MARKET_FORGE, "The Forge")])

    def test_cluster_keeps_the_region_of_each_extreme_and_counts_failures(self):
        quote = market.quote_cluster(self.client, 34, market.market_regions(self.client))
        self.assertEqual(quote.scope, "global (2/3 regions)")
        self.assertEqual((quote.regions_scanned, quote.regions_failed), (2, 1))
        self.assertEqual((quote.min_sell, quote.best_sell_region), (4.98, MARKET_FORGE))
        self.assertEqual((quote.max_buy, quote.best_buy_region), (4.55, MARKET_DOMAIN))
        self.assertEqual((quote.sell_volume, quote.buy_volume), (1300, 980))

    def test_cluster_is_as_old_as_its_oldest_region(self):
        # The Forge answers with a 2-minute book, Domain with a 15-minute one: the fold is the
        # worst of the batch, because that is when the numbers stopped agreeing with each other.
        quote = market.quote_cluster(self.client, 34, market.market_regions(self.client))
        self.assertGreater(self.now() - quote.meta.last_modified, 800)

    def test_one_dead_region_does_not_sink_the_scan(self):
        paths = [market.book_path(region, 34) for region in (MARKET_FORGE, MARKET_DOMAIN, MARKET_BROKEN)]
        fetched = self.client.get_many(paths)
        self.assertEqual(set(fetched), set(paths))
        self.assertIsInstance(fetched[paths[0]], list)
        self.assertIsInstance(fetched[paths[2]], Exception)

    def test_a_repeated_path_is_fetched_once(self):
        path = market.book_path(MARKET_FORGE, 34)
        self.client.get_many([path, path, path])
        self.assertEqual(len(self.env.server.calls_to(f"/markets/{MARKET_FORGE}/orders")), 1)


class ReferencePriceTests(MarketTestCase):
    """`/markets/prices`: CCP's published figures, for the types no order book answers for."""

    def test_reference_reads_the_row_for_a_type(self):
        ref = market.reference_price(self.client, 34)
        self.assertEqual((ref.type_id, ref.average_price, ref.adjusted_price), (34, 4.87, 4.05))

    def test_a_published_zero_is_not_reported_as_missing(self):
        # Live ESI says `adjusted_price: 0.0` for PLEX. Zero is a figure CCP publishes, not a gap,
        # so the lookup must hand it through instead of collapsing it to "no industry reference".
        ref = market.reference_price(self.client, MARKET_PLEX)
        self.assertEqual((ref.average_price, ref.adjusted_price), (4574918.36, 0.0))

    def test_an_omitted_key_is_missing_rather_than_zero(self):
        # Pyerite's row carries no `adjusted_price` at all - the other half of that distinction.
        ref = market.reference_price(self.client, 36)
        self.assertIsNone(ref.adjusted_price)
        self.assertEqual(ref.average_price, 4.20)

    def test_a_type_the_document_omits_has_no_reference(self):
        self.assertIsNone(market.reference_price(self.client, 40520))

    def test_one_request_answers_for_every_type(self):
        # The document lists every priced type: reading it per type would be one huge request each.
        table = market.price_table(self.client)
        self.assertEqual([table.reference(t).average_price for t in (34, 36, MARKET_PLEX)],
                         [4.87, 4.20, 4574918.36])
        self.assertIsNone(table.reference(40520))
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 1)

    def test_the_reference_carries_the_documents_own_age(self):
        # Its own, that is: a reference stamped with the five-minute book's age would be wrong in
        # whichever direction the reader trusted.
        ref = market.reference_price(self.client, MARKET_PLEX)
        self.assertGreater(self.now() - ref.meta.last_modified, MARKET_PRICES_AGE - 60)


class MarketCommandTests(MarketTestCase):
    def test_default_scope_is_jita_at_station_level(self):
        code, out, err = self.env.run(["market", "Tritanium"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Tritanium (id 34)", out)
        self.assertIn("Jita 4-4 (station)", out)
        self.assertIn("5.05", out)
        self.assertIn("4.20", out)
        self.assertNotIn("6.20", out)          # Domain's sell order is not part of a Jita quote
        self.assertNotIn("traded/day", out)    # history only with --history
        self.assertIn("ago; ESI refreshes the book every 5 min", out)

    def test_market_lookups_never_send_a_bearer_token(self):
        self.env.run(["market", "Tritanium", "--global"])
        self.assertEqual([c for c in self.env.server.calls if "authorization" in c.headers], [])

    def test_scopes_are_queried_by_type_never_as_a_whole_region(self):
        # An unfiltered book is the whole region (Forge: 410 pages), which no price check needs.
        self.env.run(["market", "34"])
        calls = self.env.server.calls_to(f"/markets/{MARKET_FORGE}/orders")
        self.assertEqual([c.query["type_id"] for c in calls], ["34"])

    def test_region_and_hub_scopes_appear_as_separate_rows(self):
        code, out, err = self.env.run(["market", "34", "--region", "The Forge", "--hub", "amarr"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("The Forge", out)
        self.assertIn("Amarr (station)", out)
        self.assertEqual(out.count(": as of"), 2)

    def test_the_same_region_named_twice_is_one_row(self):
        code, out, _ = self.env.run(["market", "34", "--region", "The Forge", "--region", "10000002"])
        self.assertEqual(code, 0)
        self.assertEqual(out.count(": as of"), 1)

    def test_history_reports_daily_traded_volume_for_a_regions_scope(self):
        code, out, err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("traded/day*", out)
        # The footnote is the whole point: ESI's history is daily and one day behind.
        self.assertIn("one day behind", out)

    def test_global_scan_adds_a_cluster_row_and_warns_about_regions_that_failed(self):
        code, out, err = self.env.run(["market", "34", "--global"])
        self.assertEqual(code, 0)
        self.assertIn("global (2/3 regions)", out)
        self.assertIn("warning: global (2/3 regions): 1 of 3 regions did not answer", err)
        # The best buy is in Domain while the best sell is in The Forge; both stations are named.
        self.assertIn("Amarr VIII (Oris) - Emperor Family Academy", out)
        for region in (WORMHOLE_REGION, ABYSSAL_REGION):
            self.assertEqual(self.env.server.calls_to(f"/markets/{region}/orders"), [])

    def test_json_output_carries_ids_names_and_freshness(self):
        code, out, err = self.env.run(["market", "34", "--hub", "jita", "--json"])
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["history_days"], None)
        scope = doc["types"][0]["scopes"][0]
        self.assertEqual(doc["types"][0]["type_id"], 34)
        self.assertEqual(doc["types"][0]["name"], "Tritanium")
        self.assertEqual(scope["scope"], "Jita 4-4 (station)")
        self.assertEqual((scope["region_id"], scope["region_name"]), (MARKET_FORGE, "The Forge"))
        self.assertEqual(scope["location_id"], STATION_JITA)
        self.assertEqual((scope["min_sell"], scope["max_buy"]), (5.05, 4.20))
        self.assertEqual(scope["best_sell_location_id"], STATION_JITA)
        self.assertTrue(scope["last_modified"].endswith("Z"))
        self.assertGreater(scope["age_seconds"], 100)
        self.assertLess(scope["age_seconds"], 240)
        self.assertIsNone(scope["history"])

    def test_json_history_window_is_the_newest_days(self):
        _code, out, _err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7", "--json"])
        history = json.loads(out)["types"][0]["scopes"][0]["history"]
        self.assertEqual((history["rows"], history["total_volume"]), (7, 490))
        self.assertEqual(history["volume_per_day"], 70.0)
        self.assertAlmostEqual(history["average_price"], 5.07, places=6)
        self.assertEqual(history["newest_date"], "2026-08-10T12:00:00Z")

    def test_json_history_is_null_where_the_type_never_traded(self):
        _code, out, _err = self.env.run(["market", "34", "--hub", "amarr", "--history", "7", "--json"])
        self.assertIsNone(json.loads(out)["types"][0]["scopes"][0]["history"])

    def test_csv_rows_carry_raw_numbers_and_resolved_names(self):
        code, out, err = self.env.run(["market", "34", "--hub", "amarr", "--csv"])
        self.assertEqual((code, err), (0, ""))
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertEqual(row["scope"], "Amarr (station)")
        self.assertEqual((row["region_id"], row["region_name"]), (str(MARKET_DOMAIN), "Domain"))
        self.assertEqual(row["min_sell"], "6.2")
        self.assertEqual(row["best_buy_location_name"], "Amarr VIII (Oris) - Emperor Family Academy")

    def test_several_types_share_one_run(self):
        code, out, err = self.env.run(["market", "Tritanium", "Large Skill Injector"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Tritanium (id 34)", out)
        self.assertIn("Large Skill Injector (id 40520)", out)

    def test_unknown_hub_lists_the_choices(self):
        code, _out, err = self.env.run(["market", "34", "--hub", "jita4"])
        self.assertEqual(code, 1)
        self.assertIn("unknown hub 'jita4'", err)
        self.assertIn("jita, amarr, dodixie, rens, hek", err)

    def test_history_window_must_be_positive(self):
        code, _out, err = self.env.run(["market", "34", "--history", "0"])
        self.assertEqual(code, 1)
        self.assertIn("positive number of days", err)

    def test_an_empty_book_explains_esis_coverage_and_shows_the_reference(self):
        code, out, err = self.env.run(["market", "PLEX", "--hub", "jita"])
        self.assertEqual((code, err), (0, ""))
        # A row of dashes is not an answer: the reader is told why ESI has no orders to show and
        # what it does publish instead - as a coverage limit, not as a failure.
        self.assertIn("ESI publishes order books per region only", out)
        self.assertIn("account-wide vault market", out)
        self.assertIn("no global order-book endpoint", out)
        self.assertIn("average 4,574,918.36 ISK", out)
        self.assertIn("industry adjusted 0.00 ISK", out)      # a real zero, not a dash
        self.assertIn("not a bid or an ask", out)

    def test_a_live_book_prints_no_footnote_and_never_asks_for_the_price_document(self):
        code, out, _ = self.env.run(["market", "Tritanium", "--hub", "jita"])
        self.assertEqual(code, 0)
        self.assertNotIn("reference", out)
        # /markets/prices is the whole price list: a run with orders to show must not pay for it.
        self.assertEqual(self.env.server.calls_to("/markets/prices"), [])

    def test_an_empty_hub_book_says_only_that_and_points_at_the_region(self):
        # An empty Jita book is evidence about Jita and nothing else, so the footnote must not reach
        # for a cause - nor for a figure. The wider scope is what is actually left to do, and it is
        # named in the form that can be pasted straight back.
        code, out, err = self.env.run(["market", "Nanite Repair Paste", "--hub", "jita"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("a statement about those books and nothing else", out)
        self.assertIn('--region "The Forge"', out)
        self.assertNotIn("--global", out)       # the region is the wider book this scope still has
        self.assertNotIn("vault", out)          # not PLEX's story, so not told here
        self.assertNotIn("published reference", out)
        # And nothing was fetched to fill the gap: ESI does price this type, which makes the refusal
        # to quote it a decision rather than an accident of the fixture.
        self.assertEqual(self.env.server.calls_to("/markets/prices"), [])

    def test_an_empty_region_book_points_at_the_cluster_scan(self):
        code, out, err = self.env.run(["market", "Nanite Repair Paste", "--region", "Domain"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("a statement about those regions and nothing else", out)
        self.assertIn("--global reads every market region's book", out)
        self.assertNotIn("vault", out)
        self.assertEqual(self.env.server.calls_to("/markets/prices"), [])

    def test_an_empty_cluster_scan_says_so_without_borrowing_plexs_explanation(self):
        code, out, err = self.env.run(["market", "Nanite Repair Paste", "--global"])
        self.assertEqual(code, 0)
        # The fixture's dead shard still warns, on stderr as ever.
        self.assertIn("did not answer", err)
        self.assertIn("No orders in any book this run read", out)
        self.assertIn("no wider book was left to ask", out)
        # With nothing wider to ask, ESI's own figure is worth showing - labelled as what it is.
        self.assertIn("average 118.50 ISK", out)
        self.assertIn("not a bid or an ask", out)
        self.assertNotIn("vault", out)
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 1)

    def test_an_empty_cluster_with_no_reference_row_says_that_plainly(self):
        code, out, _ = self.env.run(["market", "Large Skill Injector", "--global"])
        self.assertEqual(code, 0)
        # Only here - where no wider book exists - is "ESI has no figure either" a complete answer.
        self.assertIn("has no row for this type either", out)
        self.assertNotIn("published reference for this type", out)

    def test_a_narrow_empty_book_gets_no_figure_even_when_another_type_needed_the_document(self):
        code, out, _ = self.env.run(["market", "PLEX", "Nanite Repair Paste", "--hub", "jita"])
        self.assertEqual(code, 0)
        # PLEX is measured empty in every region and gets ESI's published figure. The module under it
        # is only known to be absent from Jita, so it gets the wider scope instead - even though the
        # document that could quote a number for it is already in hand.
        self.assertEqual(out.count("account-wide vault market"), 1)
        plex, untraded = out.split(f"Nanite Repair Paste (id {MARKET_UNTRADED})")
        self.assertIn("average 4,574,918.36 ISK", plex)
        self.assertNotIn("published reference", untraded)
        self.assertIn('--region "The Forge"', untraded)

    def test_only_the_type_with_nothing_to_show_gets_the_footnote(self):
        code, out, _ = self.env.run(["market", "Tritanium", "PLEX", "--hub", "jita"])
        self.assertEqual(code, 0)
        self.assertIn("Tritanium (id 34)", out)
        self.assertEqual(out.count("account-wide vault market"), 1)

    def test_json_carries_a_reference_for_every_type_beside_its_quote(self):
        code, out, _ = self.env.run(["market", "PLEX", "Tritanium", "--hub", "jita", "--json"])
        self.assertEqual(code, 0)
        plex, tritanium = json.loads(out)["types"]
        # Stamped as what it is, so a script cannot read a published figure as an order.
        self.assertEqual(plex["reference"]["kind"], "esi_published_reference")
        self.assertEqual((plex["reference"]["average_price"], plex["reference"]["adjusted_price"]),
                         (4574918.36, 0.0))
        self.assertTrue(plex["reference"]["last_modified"].endswith("Z"))
        self.assertGreater(plex["reference"]["age_seconds"], MARKET_PRICES_AGE - 120)
        self.assertEqual(tritanium["reference"]["adjusted_price"], 4.05)
        # And it never becomes a quote: the empty book is still reported as an empty book.
        self.assertIsNone(plex["scopes"][0]["min_sell"])
        self.assertEqual(tritanium["scopes"][0]["min_sell"], 5.05)

    def test_json_reference_is_null_when_esi_has_no_row_for_the_type(self):
        # A cluster scan really does fetch the document, so null here means ESI has no row for the
        # type rather than this run never having asked.
        _code, out, _err = self.env.run(["market", "Large Skill Injector", "--global", "--json"])
        self.assertIsNone(json.loads(out)["types"][0]["reference"])
        self.assertEqual(len(self.env.server.calls_to("/markets/prices")), 1)

    def test_json_and_csv_ask_nothing_of_the_price_document_when_books_had_orders(self):
        # The whole all-types document is the expensive part of a run; wanting machine-readable
        # output is not a reason to read it.
        _code, out, _ = self.env.run(["market", "Tritanium", "--hub", "jita", "--json"])
        self.assertIsNone(json.loads(out)["types"][0]["reference"])
        _code, out, _ = self.env.run(["market", "Tritanium", "--hub", "jita", "--csv"])
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertEqual((row["min_sell"], row["reference_average_price"]), ("5.05", ""))
        self.assertEqual(self.env.server.calls_to("/markets/prices"), [])

    def test_csv_appends_reference_columns_without_disturbing_the_header(self):
        code, out, err = self.env.run(["market", "34", "PLEX", "--hub", "amarr", "--csv"])
        self.assertEqual((code, err), (0, ""))
        rows = list(csv.DictReader(io.StringIO(out)))
        header = list(rows[0])
        self.assertEqual(header[-5], "history_newest_date")
        self.assertEqual(header[-4:], ["reference_average_price", "reference_adjusted_price",
                                       "reference_last_modified", "reference_age_seconds"])
        tritanium, plex = rows
        self.assertEqual((tritanium["min_sell"], tritanium["reference_average_price"]),
                         ("6.2", "4.87"))
        # A published 0.0 stays a value here too; an empty cell would read as "no reference".
        self.assertEqual((plex["min_sell"], plex["reference_adjusted_price"]), ("", "0.0"))


# ---------------------------------------------------------------------------
# per-scope quote cache (quotes.json)
# ---------------------------------------------------------------------------

class QuoteCacheFixture:
    """Scaffolding shared by the two quote-cache test classes.

    A plain mixin, not a base TestCase: unittest would otherwise re-run every case below in each
    subclass. `Esi` keeps responses in memory for their stated TTL, so anything measuring the disk
    cache has to ask with a client of its own - which is also what each real invocation gets."""

    BOOK_TTL = 300.0     # what ESI states for a regional book, in seconds from the response

    def process(self) -> esi.Esi:
        return esi.Esi("unittest")

    def serve_books_with_expiry(self) -> None:
        """Re-serve both fixture books with an `Expires` header, as live ESI does."""
        for region in (MARKET_FORGE, MARKET_DOMAIN):
            def handler(call, region=region):
                rows, headers = self.env._market_orders(call)
                return rows, {**headers, "Expires": http_date(self.BOOK_TTL)}
            self.env.server.get(f"/markets/{region}/orders", handler=handler)

    def book_calls(self, region: int = MARKET_FORGE) -> list:
        return self.env.server.calls_to(f"/markets/{region}/orders")


class QuoteCacheTestCase(QuoteCacheFixture, MarketTestCase):
    """The figures behind `inventory --value-at`, reused only while ESI's own expiry vouches.

    The fixture's books arrive with `Last-Modified` alone - all `tests.fake_esi` has ever sent - so
    each test that expects caching re-serves them with the `Expires` live ESI adds. Without that
    header there is no stated expiry to honour and nothing may be cached, which is asserted below as
    its own case rather than left as an accident of the fixture."""

    def setUp(self):
        super().setUp()
        self.cache = tempfile.mkdtemp(prefix="quotes-")
        self.addCleanup(shutil.rmtree, self.cache, True)

    def seed(self, scope: market.Scope, type_id: int, *, age: float,
             ttl: float = QuoteCacheFixture.BOOK_TTL) -> None:
        """Put one figure on disk as though an earlier run had learned it `age` seconds ago."""
        moment = self.now()
        market.publish_figures({market.figure_key(scope, type_id): market.CachedFigure(
            min_sell=9.0, max_buy=8.0, last_modified=moment - age, expires=moment + ttl)},
            cache_dir=self.cache)

    def expire(self, scope: market.Scope, type_id: int) -> None:
        """Rewrite one record as one ESI has already disowned - the state a warm cache reaches five
        minutes later, and the reason a second run cannot simply trust whatever it finds."""
        path = market.quote_doc_path(self.cache)
        with open(path) as fh:
            doc = json.load(fh)
        doc["figures"][market.figure_key(scope, type_id)]["expires"] = self.now() - 1.0
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def test_a_warm_run_reads_no_book_and_reports_the_same_figures(self):
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        cold = market.book_figures(self.process(), [34, 36], scope, cache_dir=self.cache)
        self.assertEqual((cold.fetched, cold.cached, cold.failed), (2, 0, 0))
        self.assertEqual(2, len(self.book_calls()))
        warm = market.book_figures(self.process(), [34, 36], scope, cache_dir=self.cache)
        self.assertEqual((warm.fetched, warm.cached), (0, 2))
        self.assertEqual(2, len(self.book_calls()))      # nothing new asked of ESI
        self.assertEqual(warm.max_buy, cold.max_buy)     # the money did not move by re-reading
        self.assertEqual(warm.min_sell, cold.min_sell)

    def test_only_the_entries_past_their_stated_expiry_are_refetched(self):
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        market.book_figures(self.process(), [34, 36], scope, cache_dir=self.cache)
        read = len(self.book_calls())
        self.expire(scope, 34)
        figures = market.book_figures(self.process(), [34, 36], scope, cache_dir=self.cache)
        self.assertEqual((figures.fetched, figures.cached), (1, 1))
        # The fan-out answers out of order, so compare the calls this run added rather than the last.
        self.assertEqual(["34"], [call.query["type_id"] for call in self.book_calls()[read:]])

    def test_a_station_figure_never_answers_a_region_question(self):
        """The key is the scope's filter, not just its type: `--value-at jita` and
        `--value-at The Forge` read different order sets and may not borrow from each other."""
        self.serve_books_with_expiry()
        hub = market.HUBS["jita"]
        station = market.Scope(hub.region_id, hub.label, hub.system_id, hub.station_id)
        region = market.Scope(MARKET_FORGE, "The Forge")
        at_station = market.book_figures(self.process(), [34], station, cache_dir=self.cache)
        self.assertAlmostEqual(at_station.max_buy[34], 4.20, places=9)
        wide = market.book_figures(self.process(), [34], region, cache_dir=self.cache)
        self.assertEqual((wide.fetched, wide.cached), (1, 0))
        self.assertAlmostEqual(wide.max_buy[34], 4.30, places=9)     # the region's richest buy
        again = market.book_figures(self.process(), [34], station, cache_dir=self.cache)
        self.assertEqual((again.fetched, again.cached), (0, 1))      # and each stayed in its lane
        self.assertAlmostEqual(again.max_buy[34], 4.20, places=9)

    def test_the_freshness_names_the_oldest_figure_whatever_its_source(self):
        """Half off disk, half off ESI: the pair is as old as its stalest input, both ways round."""
        self.serve_books_with_expiry()
        forge = market.Scope(MARKET_FORGE, "The Forge")
        self.seed(forge, 36, age=1000)         # a cached figure older than the book read beside it
        figures = market.book_figures(self.process(), [34, 36], forge, cache_dir=self.cache)
        self.assertEqual((figures.cached, figures.fetched), (1, 1))
        self.assertAlmostEqual(figures.meta.last_modified, self.now() - 1000, delta=2.0)

        domain = market.Scope(MARKET_DOMAIN, "Domain")
        self.seed(domain, 36, age=10)          # and the other way: now the book is the older input
        figures = market.book_figures(self.process(), [34, 36], domain, cache_dir=self.cache)
        self.assertEqual((figures.cached, figures.fetched), (1, 1))
        self.assertGreater(self.now() - figures.meta.last_modified,
                           MARKET_BOOK_AGE[MARKET_DOMAIN] - 60)

    def test_a_book_with_no_stated_expiry_is_used_but_never_cached(self):
        # The fixture's default books carry `Last-Modified` only. Without an `Expires` nothing bounds
        # how long the figures may still be called current, so they are used once and forgotten.
        scope = market.Scope(MARKET_FORGE, "The Forge")
        first = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        self.assertAlmostEqual(first.max_buy[34], 4.30, places=9)
        second = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        self.assertEqual((second.fetched, second.cached), (1, 0))
        self.assertEqual(2, len(self.book_calls()))
        self.assertFalse(os.path.exists(market.quote_doc_path(self.cache)))

    def test_an_empty_book_is_an_answer_and_is_cached_as_one(self):
        """`[]` is ESI saying nobody has ordered this here, as of that stamp - worth keeping, and it
        leaves the type unpriced rather than worthless exactly as a cold read does."""
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        first = market.book_figures(self.process(), [MARKET_UNTRADED], scope, cache_dir=self.cache)
        self.assertEqual((first.fetched, first.cached, first.failed), (1, 0, 0))
        self.assertEqual(first.max_buy, {})
        self.assertEqual(1, first.answered)     # it spoke; it simply had nothing to say
        second = market.book_figures(self.process(), [MARKET_UNTRADED], scope, cache_dir=self.cache)
        self.assertEqual((second.fetched, second.cached), (0, 1))
        self.assertEqual(1, len(self.book_calls()))     # the second run asked for nothing

    def test_a_book_that_did_not_answer_is_left_out_of_the_cache(self):
        """A timeout must cost a retry on the next run, not five minutes of confident silence."""
        scope = market.Scope(MARKET_BROKEN, "Placid shard")
        first = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        self.assertEqual((first.fetched, first.failed, first.cached), (1, 1, 0))
        self.assertEqual(first.max_buy, {})
        second = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        self.assertEqual((second.fetched, second.cached), (1, 0))
        self.assertFalse(os.path.exists(market.quote_doc_path(self.cache)))

    def test_a_cache_that_cannot_be_read_costs_a_refetch_and_nothing_else(self):
        scope = market.Scope(MARKET_FORGE, "The Forge")
        bodies = ["not json at all", "[]", '{"version": 999, "figures": {}}',
                  '{"version": 1, "figures": "nope"}',
                  '{"version": 1, "figures": {"k": {"min_sell": 1.0}}}']
        for body in bodies:
            with self.subTest(body=body):
                with open(market.quote_doc_path(self.cache), "w") as fh:
                    fh.write(body)
                figures = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
                self.assertEqual((figures.fetched, figures.cached), (1, 0))
                self.assertAlmostEqual(figures.max_buy[34], 4.30, places=9)
        # Publishing over a broken document leaves a readable one behind rather than joining it.
        with open(market.quote_doc_path(self.cache)) as fh:
            self.assertEqual(json.load(fh)["version"], market.QUOTE_CACHE_VERSION)

    def test_a_figure_with_no_stated_expiry_is_never_served(self):
        """Hand-edited or half-written records without `expires` are the same case as a live response
        that omitted it: unusable, whatever the prices in them look like."""
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        key = market.figure_key(scope, 34)
        with open(market.quote_doc_path(self.cache), "w") as fh:
            json.dump({"version": market.QUOTE_CACHE_VERSION,
                       "figures": {key: {"min_sell": 1.0, "max_buy": 99.0}}}, fh)
        figures = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        self.assertEqual((figures.fetched, figures.cached), (1, 0))
        self.assertAlmostEqual(figures.max_buy[34], 4.30, places=9)   # ESI's figure, not the record's

    def test_expired_records_are_dropped_rather_than_written_back(self):
        """An entry past its stated expiry can never be served again, so keeping it would only grow
        the file; a live one from another run still has to survive this one's write."""
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        self.seed(scope, 36, age=1000, ttl=-60.0)      # already expired when this run starts
        self.seed(scope, MARKET_UNTRADED, age=5)       # somebody else's live figure
        market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
        with open(market.quote_doc_path(self.cache)) as fh:
            figures = json.load(fh)["figures"]
        self.assertNotIn(market.figure_key(scope, 36), figures)
        self.assertIn(market.figure_key(scope, MARKET_UNTRADED), figures)


class QuoteCacheMergeTests(unittest.TestCase):
    """Two runs publishing to one cache file - the two characters of one account, or two shells."""

    class Books:
        """A transport that only answers books, and overlaps the two runs inside one barrier.

        The barrier sits in the fetch, which both runs reach only after reading the cache: so both
        reads are provably stale by the time either publishes, and a publisher that wrote the
        document it had read earlier would lose the other run's types here every time."""

        def __init__(self, barrier: threading.Barrier):
            self.barrier = barrier

        def get_many_meta(self, paths):
            self.barrier.wait()
            moment = time.time()
            rows = [{"price": 5.0, "is_buy_order": False}, {"price": 4.0, "is_buy_order": True}]
            meta = esi.Meta(last_modified=moment - 10, expires=moment + 300)
            return {path: (rows, meta) for path in paths}

    def test_two_valuations_publish_without_losing_each_other(self):
        cache = tempfile.mkdtemp(prefix="quotes-")
        self.addCleanup(shutil.rmtree, cache, True)
        scope = market.Scope(MARKET_FORGE, "The Forge")
        barrier = threading.Barrier(2, timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda ident: market.book_figures(self.Books(barrier), [ident], scope,
                                                            cache_dir=cache), (34, 36)))
        stored = market.read_quote_cache(cache)
        self.assertEqual(sorted(stored), sorted([market.figure_key(scope, 34),
                                                 market.figure_key(scope, 36)]))


class InventoryQuoteCacheTests(QuoteCacheFixture, MarketTestCase):
    """`inventory --value-at` end to end: what the run says before it starts, and admits after.

    These live here rather than in `test_cli_integration.py` because they are about the quote cache.
    Every `env.run` builds its own `Esi`, so nothing carried from one run to the next is in memory:
    what a second run fails to ask for is exactly what this cache saved."""

    HELD = [34, 36, 590, INV_TYPE_SHIP, INV_TYPE_CONTAINER]

    def setUp(self):
        super().setUp()
        self.env.install_inventory()      # re-installs the market routes, so wrap afterwards
        self.serve_books_with_expiry()

    def hub_scope(self, name: str = "jita") -> market.Scope:
        """The scope `--value-at <hub>` prices at, built the way `_valuation_scope` builds it."""
        hub = market.HUBS[name]
        return market.Scope(hub.region_id, hub.label, hub.system_id, hub.station_id)

    def total_line(self, out: str) -> str:
        return next(line for line in out.splitlines() if line.startswith("TOTAL"))

    def test_a_second_valuation_reads_no_book_at_all(self):
        code, cold, err = self.env.run(["inventory", "--value-at", "jita"])
        self.assertEqual(code, 0)
        self.assertIn("pricing 5 distinct types held: 5 order books to read, one per type", err)
        self.assertNotIn("at this size", err)    # five books is not a wait worth quoting a cost for
        read = len(self.book_calls())
        self.assertEqual(5, read)
        code, warm, err = self.env.run(["inventory", "--value-at", "jita"])
        self.assertEqual(code, 0)
        self.assertEqual(read, len(self.book_calls()))
        self.assertIn("every figure already in the local quote cache, so no order book is read", err)
        # Same figures, and the same stated age: a cached number is not re-dated to this run.
        self.assertEqual(self.total_line(cold), self.total_line(warm))
        self.assertIn("0 order-book requests now; 5 types served from the local quote cache", warm)
        # Asking for a different view of the same holdings is not a new scope either.
        code, items, _ = self.env.run(["inventory", "--items", "--by", "category",
                                       "--value-at", "jita"])
        self.assertEqual((code, len(self.book_calls())), (0, read))
        self.assertEqual(self.total_line(warm), self.total_line(items))

    def test_a_cached_figure_is_still_reported_at_its_own_age(self):
        """The freshness line is the honesty of the whole feature: figures reused from disk are
        printed with the stamp ESI gave them, never with the moment they happened to be read."""
        scope = self.hub_scope()
        stamp = self.now() - 3600
        market.publish_figures({market.figure_key(scope, ident): market.CachedFigure(
            min_sell=9.0, max_buy=8.0, last_modified=stamp, expires=self.now() + 300)
            for ident in self.HELD})
        code, out, _ = self.env.run(["inventory", "--value-at", "jita"])
        self.assertEqual(code, 0)
        self.assertEqual([], self.book_calls())
        self.assertIn(f"as of {time.strftime('%H:%M:%SZ', time.gmtime(stamp))}", out)
        self.assertIn("1h 00m ago", out)

    def test_a_partially_warm_run_says_which_half_it_still_has_to_read(self):
        code, _out, err = self.env.run(["inventory", "--value-at", "jita"])
        self.assertEqual((code, len(self.book_calls())), (0, 5))
        scope = self.hub_scope()
        path = market.quote_doc_path()
        with open(path) as fh:
            doc = json.load(fh)
        doc["figures"][market.figure_key(scope, 34)]["expires"] = self.now() - 1.0
        with open(path, "w") as fh:
            json.dump(doc, fh)
        code, _out, err = self.env.run(["inventory", "--value-at", "jita"])
        self.assertEqual(code, 0)
        self.assertIn("pricing 5 distinct types held: 1 order book to read, one per type", err)
        self.assertIn("(4 already priced from the last run)", err)
        self.assertEqual(6, len(self.book_calls()))

    def test_machine_output_keeps_the_notice_off_both_streams(self):
        code, out, err = self.env.run(["inventory", "--value-at", "jita", "--json"])
        self.assertEqual((code, err), (0, ""))
        cold = json.loads(out)["value_basis"]
        self.assertEqual((cold["requests"], cold["cached_figures"]), (5, 0))
        warm = json.loads(self.env.run(["inventory", "--value-at", "jita", "--json"])[1])
        self.assertEqual((warm["value_basis"]["requests"], warm["value_basis"]["cached_figures"]),
                         (0, 5))

    def test_the_reference_basis_never_goes_through_the_quote_cache(self):
        """`/markets/prices` is one document for the whole cluster, stamped on its own schedule.
        Caching its figures under an order book's five-minute expiry would misstate them."""
        code, out, _ = self.env.run(["inventory"])
        self.assertEqual(code, 0)
        self.assertIn("freshness: as of ", out)
        self.assertEqual(1, len(self.env.server.calls_to("/markets/prices")))
        self.assertEqual([], self.book_calls())
        self.assertFalse(os.path.exists(market.quote_doc_path()))

    def test_the_notice_quotes_a_duration_only_when_the_wait_is_long(self):
        small = exports._valuation_notice(market.Preflight(types=5, cached=0, fetches=5))
        self.assertNotIn("at this size", small)
        big = exports._valuation_notice(market.Preflight(types=518, cached=18, fetches=500))
        self.assertIn("pricing 518 distinct types held: 500 order books to read", big)
        self.assertIn("(18 already priced from the last run)", big)
        self.assertIn("at this size", big)      # a minute of silence earns an explanation
        one = exports._valuation_notice(market.Preflight(types=1, cached=0, fetches=1))
        self.assertIn("1 distinct type held: 1 order book to read", one)



if __name__ == "__main__":
    unittest.main()
