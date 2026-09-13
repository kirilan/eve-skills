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
from pathlib import Path
from unittest import mock

from eve_skills import alphadata, cmd_market, esi, exports, market


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
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["figures"][market.figure_key(scope, type_id)]["expires"] = self.now() - 1.0
        with open(path, "w", encoding="utf-8") as fh:
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
                with open(market.quote_doc_path(self.cache), "w", encoding="utf-8") as fh:
                    fh.write(body)
                figures = market.book_figures(self.process(), [34], scope, cache_dir=self.cache)
                self.assertEqual((figures.fetched, figures.cached), (1, 0))
                self.assertAlmostEqual(figures.max_buy[34], 4.30, places=9)
        # Publishing over a broken document leaves a readable one behind rather than joining it.
        with open(market.quote_doc_path(self.cache), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], market.QUOTE_CACHE_VERSION)

    def test_a_figure_with_no_stated_expiry_is_never_served(self):
        """Hand-edited or half-written records without `expires` are the same case as a live response
        that omitted it: unusable, whatever the prices in them look like."""
        self.serve_books_with_expiry()
        scope = market.Scope(MARKET_FORGE, "The Forge")
        key = market.figure_key(scope, 34)
        with open(market.quote_doc_path(self.cache), "w", encoding="utf-8") as fh:
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
        with open(market.quote_doc_path(self.cache), encoding="utf-8") as fh:
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
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["figures"][market.figure_key(scope, 34)]["expires"] = self.now() - 1.0
        with open(path, "w", encoding="utf-8") as fh:
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


class MarketIndexFixture(MarketTestCase):
    """`--group`/`--category` and `--fields`, against a seeded market type index.

    The bundled index is 0.94 MB of live SDE names; quoting it would make every assertion here a bet
    that CCP does not rename a group before the next build. These four types - all served by
    tests/fake_esi.py's books, one of them deliberately empty - are the whole world instead."""

    GROUPS = {
        "Basic Commodities - Tier 1": {"id": 1042, "category_id": 43, "category": "Planetary Commodities",
                                       "types": {"34": "Tritanium", "36": "Pyerite"}},
        "Refined Commodities - Tier 2": {"id": 1034, "category_id": 43,
                                         "category": "Planetary Commodities",
                                         "types": {"590": "Caldari Ship Blueprint"}},
        "Account Status": {"id": 517, "category_id": 17, "category": "Commodity",
                           "types": {"44992": "PLEX"}},
    }
    CATEGORIES = {
        "Planetary Commodities": {"id": 43, "groups": ["Basic Commodities - Tier 1",
                                                       "Refined Commodities - Tier 2"]},
        "Commodity": {"id": 17, "groups": ["Account Status"]},
    }

    def setUp(self):
        super().setUp()
        self.seed_index()

    def seed_index(self, **overrides) -> None:
        """Write the index where `alphadata` looks before the package's own copy."""
        self.env._write_json(os.path.join(self.env.data_home, "eve-skills", "market_types.json"),
                             {"source": "synthetic", "build": 2500001, "fetched": "2026-09-01T00:00:00Z",
                              "groups": self.GROUPS, "categories": self.CATEGORIES, **overrides})

    def without_any_index(self) -> None:
        """Hide the copy bundled in the package too, so "nothing installed" is provable whatever the
        wheel ships - the same trick `tests/test_doctor.py` uses."""
        empty = os.path.join(tempfile.mkdtemp(prefix="eve-skills-no-index-"), "package-data")
        os.makedirs(empty, exist_ok=True)
        patcher = mock.patch.object(alphadata, "PACKAGE_DATA_DIR", Path(empty))
        patcher.start()
        self.addCleanup(patcher.stop)

    def books_read(self) -> list[str]:
        """Which type ids this run asked ESI for, in request order."""
        return [call.query["type_id"]
                for region in (MARKET_FORGE, MARKET_DOMAIN)
                for call in self.env.server.calls_to(f"/markets/{region}/orders")]


class GroupExpansionTests(MarketIndexFixture):
    def test_a_group_prices_every_type_it_holds(self):
        code, out, err = self.env.run(["market", "--group", "Basic Commodities - Tier 1"])
        self.assertEqual(code, 0)
        self.assertIn("Tritanium (id 34)", out)
        self.assertIn("Pyerite (id 36)", out)
        # The index carries the ids, so no `/universe/ids` lookup is needed to know what to price.
        self.assertEqual(["34", "36"], self.books_read())
        self.assertEqual('2 types from --group "Basic Commodities - Tier 1": 2 order books to read\n', err)

    def test_a_category_walks_every_group_inside_it(self):
        code, out, err = self.env.run(["market", "--category", "Planetary Commodities"])
        self.assertEqual(code, 0)
        self.assertEqual(["34", "36", "590"], self.books_read())
        self.assertIn("Caldari Ship Blueprint (id 590)", out)
        self.assertEqual('3 types from --category "Planetary Commodities": 3 order books to read\n', err)

    def test_a_group_is_reachable_by_its_sde_id_and_in_any_case(self):
        for spec in ("1042", "basic commodities - tier 1", "  BASIC COMMODITIES - TIER 1  "):
            with self.subTest(spec=spec):
                code, _out, err = self.env.run(["market", "--group", spec])
                self.assertEqual(code, 0)
                self.assertEqual(["34", "36"], self.books_read()[-2:])   # calls accumulate per subTest
                # The notice names the index's own spelling, not whatever the caller happened to type.
                self.assertIn('from --group "Basic Commodities - Tier 1"', err)

    def test_a_group_and_a_category_share_one_deduplicated_run(self):
        """The `--group` here sits inside the `--category` that was also named; idempotence is the whole
        point of expanding through one deduplicated list."""
        code, out, err = self.env.run(["market", "--category", "Planetary Commodities",
                                       "--group", "Account Status", "--hub", "amarr"])
        self.assertEqual(code, 0)
        self.assertEqual(4, out.count("min sell  max buy"))    # one table per type, none printed twice
        self.assertEqual(1, out.count("Tritanium (id 34)"))
        self.assertIn('4 types from --group "Account Status", --category "Planetary Commodities"', err)

    def test_a_type_named_twice_is_priced_once_under_the_first_spelling(self):
        code, out, err = self.env.run(["market", "Pyerite", "--group", "Basic Commodities - Tier 1"])
        self.assertEqual(code, 0)
        self.assertEqual(1, out.count("(id 36)"))
        # The command line is asked first, so its order wins: Pyerite's block before Tritanium's.
        self.assertLess(out.index("Pyerite (id 36)"), out.index("Tritanium (id 34)"))
        self.assertTrue(err.startswith("2 types from --group"))

    def test_an_unknown_group_offers_the_names_that_contain_what_was_typed(self):
        code, _out, err = self.env.run(["market", "--group", "tier"])
        self.assertEqual(1, code)
        self.assertIn("no market group named 'tier' in the local market type index (SDE build 2500001)", err)
        self.assertIn("closest: 'Basic Commodities - Tier 1', 'Refined Commodities - Tier 2'", err)
        self.assertEqual([], self.books_read())         # a mistyped name costs no request at all

    def test_an_unknown_group_or_category_prints_every_name_when_they_fit(self):
        code, _out, err = self.env.run(["market", "--group", "Commodity"])
        self.assertEqual(1, code)
        # Three groups is a list worth reading out; the real index's 814 would get a count instead.
        self.assertIn("the groups are: Account Status, Basic Commodities - Tier 1, "
                      "Refined Commodities - Tier 2", err)
        code, _out, err = self.env.run(["market", "--category", "Modules"])
        self.assertEqual(1, code)
        self.assertIn("the categories are: Commodity, Planetary Commodities", err)

    def test_an_unknown_id_says_so(self):
        code, _out, err = self.env.run(["market", "--group", "999"])
        self.assertEqual(1, code)
        self.assertIn("no market group with id 999 in the local market type index", err)

    def test_an_empty_specifier_is_not_a_search_for_everything(self):
        # Matching "" against every name would expand to the whole index; that is a typo, not a query.
        code, _out, err = self.env.run(["market", "--group", ""])
        self.assertEqual(1, code)
        self.assertIn("empty market group specifier", err)
        self.assertEqual([], self.books_read())

    def test_no_index_installed_names_the_command_that_builds_it(self):
        os.remove(os.path.join(self.env.data_home, "eve-skills", "market_types.json"))
        self.without_any_index()      # ...and the copy bundled in the package
        code, _out, err = self.env.run(["market", "--group", "Basic Commodities - Tier 1"])
        self.assertEqual(1, code)
        self.assertIn("no local market type index - run: eve-skills update-data", err)

    def test_a_category_naming_an_unindexed_group_is_refused_not_priced_short(self):
        """Expanding to a shorter list than the game's own would look exactly like a correct run."""
        self.seed_index(categories={"Planetary Commodities": {"id": 43,
                                                              "groups": ["Basic Commodities - Tier 1",
                                                                         "Ghost Group"]}})
        code, _out, err = self.env.run(["market", "--category", "Planetary Commodities"])
        self.assertEqual(1, code)
        self.assertIn("category Planetary Commodities names group 'Ghost Group'", err)
        self.assertIn("eve-skills update-data", err)

    def test_nothing_to_price_says_what_would_have_worked(self):
        code, _out, err = self.env.run(["market", "--hub", "jita"])
        self.assertEqual(1, code)
        self.assertIn("market needs something to price", err)
        self.assertIn("--group / --category", err)

    def test_an_expanded_run_announces_itself_on_stderr_only(self):
        """Machine output must stay parseable, and the point of the line is a human watching a wait."""
        code, out, err = self.env.run(["market", "--group", "Basic Commodities - Tier 1", "--json"])
        self.assertEqual((code, 0), (code, json.loads(out)["types"][0]["scopes"] and 0))
        self.assertIn("2 types from --group", err)
        code, out, err = self.env.run(["market", "34"])
        self.assertEqual("", err)      # a typed type has already said how big the run is


class RunSizeGuardTests(MarketIndexFixture):
    """The refusal and its escape hatch, at a threshold low enough for a four-type fixture."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(cmd_market, "MAX_TYPES_PER_RUN", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_run_bigger_than_the_limit_is_refused_with_numbers(self):
        code, _out, err = self.env.run(["market", "--category", "Planetary Commodities"])
        self.assertEqual(1, code)
        self.assertIn("3 types is more than the 2 `market` prices without being asked twice", err)
        self.assertIn("3 order books to read", err)
        self.assertIn("narrow it to one --group at a time", err)
        self.assertIn("--max-types 3", err)
        # Refused before the first book: an accidental run costs one name lookup, not three reads.
        self.assertEqual([], self.books_read())

    def test_a_cluster_scan_is_counted_as_its_own_unit(self):
        """`--global` alone asks for no station scope at all, so the run is three cluster scans - one
        request each, about eighteen seconds each - and the message has to say that or "3 books in 54s"
        reads as a contradiction."""
        code, _out, err = self.env.run(["market", "--category", "Planetary Commodities", "--global"])
        self.assertEqual(1, code)
        self.assertIn("3 order books to read (one whole-cluster scan per type", err)
        # Even the region list a --global run needs is not asked for.
        self.assertEqual([], self.env.server.calls_to("/universe/regions"))

    def test_max_types_is_the_number_of_types_not_of_requests(self):
        code, out, _err = self.env.run(["market", "--category", "Planetary Commodities",
                                        "--max-types", "3"])
        self.assertEqual(0, code)
        self.assertEqual(["34", "36", "590"], self.books_read())
        self.assertEqual(3, out.count(": as of"))

    def test_max_types_still_guards_a_run_of_typed_names(self):
        code, _out, err = self.env.run(["market", "34", "36", "590", "--max-types", "2"])
        self.assertEqual(1, code)
        self.assertIn("name fewer types", err)
        self.assertEqual([], self.books_read())

    def test_max_types_must_be_positive(self):
        for value in ("0", "-1"):
            with self.subTest(value=value):
                code, _out, err = self.env.run(["market", "34", "--max-types", value])
                self.assertEqual(1, code)
                self.assertIn(f"--max-types needs a positive number of types, not {value}", err)

    def test_the_notice_explains_a_wait_it_cannot_finish_quickly(self):
        """Under the threshold the line is just a count; over it, silence would look like a hang."""
        code, _out, err = self.env.run(["market", "--group", "Basic Commodities - Tier 1", "--global"])
        self.assertEqual(0, code)
        self.assertIn("2 order books to read (one whole-cluster scan per type", err)
        self.assertIn("about 36s at this size", err)     # 2 types x the measured 18 s scan

    def test_a_scan_beside_station_scopes_counts_both(self):
        code, _out, err = self.env.run(["market", "--group", "Basic Commodities - Tier 1",
                                        "--region", "The Forge", "--global"])
        self.assertEqual(0, code)
        # 2 types x (one Forge book + one cluster scan): the phrase says which, not just how many.
        self.assertIn("4 order books to read (one per type per scope, plus a whole-cluster scan per type)",
                      err)

    def test_the_first_spelling_of_a_type_is_the_one_kept(self):
        """A typed name resolves through ESI and a group member comes from the SDE index; after a rename
        those disagree, and the caller's own spelling is the one that should print."""
        pairs = [(34, "Tritanium"), (36, "Pyerite"), (34, "TRITANIUM"), (36, "Pyerite")]
        self.assertEqual([(34, "Tritanium"), (36, "Pyerite")], cmd_market._unique_types(pairs))


class FieldSelectionTests(MarketIndexFixture):
    """`--fields`, including the byte-for-byte defaults it must not disturb."""

    DEFAULT_CSV_HEADER = ("type_id,type_name,scope,region_id,region_name,location_id,location_name,"
                          "min_sell,max_buy,spread,margin_pct,sell_volume,buy_volume,sell_orders,"
                          "buy_orders,best_sell_location_id,best_sell_location_name,"
                          "best_sell_region_id,best_sell_region_name,best_buy_location_id,"
                          "best_buy_location_name,best_buy_region_id,best_buy_region_name,"
                          "regions_scanned,regions_failed,last_modified,expires,age_seconds,"
                          "history_days,history_rows,history_total_volume,history_volume_per_day,"
                          "history_average_price,history_newest_date,reference_average_price,"
                          "reference_adjusted_price,reference_last_modified,reference_age_seconds")

    def test_the_default_csv_header_is_the_one_scripts_already_read(self):
        """Column order is the contract here: a reader that indexes by position breaks silently."""
        code, out, _err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7",
                                        "--csv"])
        self.assertEqual(0, code)
        self.assertEqual(self.DEFAULT_CSV_HEADER, out.splitlines()[0])

    def test_the_default_text_columns_are_the_ones_this_table_has_always_had(self):
        code, out, _err = self.env.run(["market", "34", "--region", "The Forge"])
        self.assertEqual(0, code)
        header = out.splitlines()[1]
        self.assertEqual(["scope", "min sell", "max buy", "spread", "margin %", "sell vol", "buy vol",
                          "sells", "buys", "best sell at", "best buy at"],
                         [cell.strip() for cell in header.split("  ") if cell.strip()])

    def test_fields_sets_the_order_in_the_table(self):
        code, out, _err = self.env.run(["market", "34", "--region", "The Forge",
                                        "--fields", "max_buy,min_sell,scope"])
        self.assertEqual(0, code)
        header, row = out.splitlines()[1], out.splitlines()[3]
        self.assertLess(header.index("max buy"), header.index("min sell"))
        self.assertLess(header.index("min sell"), header.index("scope"))
        self.assertNotIn("sells", header)       # selected, not appended to the defaults
        self.assertLess(row.index("4.30"), row.index("4.98"))

    def test_fields_sets_the_order_in_the_csv(self):
        code, out, _err = self.env.run(["market", "34", "--hub", "amarr", "--csv",
                                        "--fields", "max_buy,type_name"])
        self.assertEqual(0, code)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(["max_buy", "type_name"], rows[0])
        self.assertEqual(["4.55", "Tritanium"], rows[1])

    def test_a_field_asked_for_twice_is_printed_twice(self):
        code, out, _err = self.env.run(["market", "34", "--csv", "--fields", "min_sell,min_sell"])
        self.assertEqual(0, code)
        self.assertEqual([["min_sell", "min_sell"], ["5.05", "5.05"]],
                         list(csv.reader(io.StringIO(out))))

    def test_field_names_surround_their_commas_with_spaces(self):
        code, out, _err = self.env.run(["market", "34", "--csv", "--fields", " scope , region_name "])
        self.assertEqual(0, code)
        self.assertEqual(["scope", "region_name"], next(csv.reader(io.StringIO(out))))

    def test_an_unknown_field_names_it_and_lists_every_valid_one(self):
        code, _out, err = self.env.run(["market", "34", "--fields", "min_price"])
        self.assertEqual(1, code)
        self.assertIn("unknown --fields name 'min_price'; valid names: type_id, type_name, scope", err)
        self.assertTrue(err.rstrip().endswith("reference_age_seconds"))
        self.assertEqual([], self.books_read())     # a typo costs no order books

    def test_a_field_list_of_nothing_is_not_a_request_for_everything(self):
        code, _out, err = self.env.run(["market", "34", "--fields", " , "])
        self.assertEqual(1, code)
        self.assertIn("--fields needs at least one column name", err)

    def test_fields_does_not_apply_to_json(self):
        """Silently ignoring it would be worse than refusing: the caller would think they had chosen."""
        code, _out, err = self.env.run(["market", "34", "--json", "--fields", "min_sell"])
        self.assertEqual(1, code)
        self.assertIn("--fields does not apply to --json", err)
        self.assertEqual([], self.books_read())

    def test_a_history_column_without_a_history_window_is_refused(self):
        """An empty column here is the misreading this flag exists to prevent, and guessing a window
        would put a number nobody asked for in the output."""
        code, _out, err = self.env.run(["market", "34", "--fields", "scope,history_volume_per_day"])
        self.assertEqual(1, code)
        self.assertIn("--fields history_volume_per_day reads ESI's daily history", err)
        self.assertIn("--history 30", err)
        self.assertEqual([], self.books_read())

    def test_a_history_column_fills_when_the_window_was_asked_for(self):
        code, out, err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7",
                                       "--csv", "--fields", "scope,history_volume_per_day"])
        self.assertEqual((code, err), (0, ""))
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual({"scope": "The Forge", "history_volume_per_day": "70.0"}, rows[0])

    def test_a_history_column_without_data_is_empty_rather_than_zero(self):
        """Amarr's region has no history rows at all; the cell must not read as "traded nothing"."""
        code, out, _err = self.env.run(["market", "34", "--hub", "amarr", "--history", "7", "--csv",
                                        "--fields", "scope,history_volume_per_day"])
        self.assertEqual(0, code)
        self.assertEqual({"scope": "Amarr (station)", "history_volume_per_day": ""},
                         next(csv.DictReader(io.StringIO(out))))

    def test_the_starred_legend_follows_the_columns_actually_printed(self):
        """A run that fetched history but prints neither starred column has nothing to explain."""
        _code, out, _err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7",
                                         "--fields", "scope,min_sell"])
        self.assertNotIn("one day behind", out)
        _code, out, _err = self.env.run(["market", "34", "--region", "The Forge", "--history", "7",
                                         "--fields", "scope,history_total_volume"])
        self.assertIn("one day behind", out)

    def test_a_selected_reference_column_is_blank_when_the_document_was_never_read(self):
        """A live book means no `/markets/prices` request, so the reference columns have no source."""
        code, out, _err = self.env.run(["market", "34", "--hub", "jita", "--csv",
                                        "--fields", "type_name,min_sell,reference_average_price"])
        self.assertEqual(0, code)
        self.assertEqual({"type_name": "Tritanium", "min_sell": "5.05", "reference_average_price": ""},
                         next(csv.DictReader(io.StringIO(out))))
        self.assertEqual([], self.env.server.calls_to("/markets/prices"))

    def test_a_selected_reference_column_survives_into_the_widest_run(self):
        code, out, _err = self.env.run(["market", "Nanite Repair Paste", "--global", "--csv",
                                        "--fields", "type_name,min_sell,reference_average_price"])
        self.assertEqual(0, code)
        row = next(csv.DictReader(io.StringIO(out)))
        self.assertEqual("118.5", row["reference_average_price"])
        self.assertEqual("", row["min_sell"])


if __name__ == "__main__":
    unittest.main()
