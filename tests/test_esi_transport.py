"""Esi transport semantics against a scripted in-process urlopen: pagination and its
cap, retry policy (which statuses, which pauses, Retry-After honoring and capping),
AuthError for 401/403 without retries, error-limit backoff, and server-clock tracking.

time.sleep is patched wherever the client would pause, so nothing waits in real time."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from eve_skills import esi
from tests.fake_esi import FakeResponse, http_date, http_error


class ScriptedTransport:
    """urlopen replacement serving a fixed script; any request past the end fails loudly."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if not self.responses:
            raise AssertionError(f"unexpected extra ESI request: {req.full_url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TransportTestCase(unittest.TestCase):
    PATH = "/characters/9/skills"

    def setUp(self):
        self.client = esi.Esi("unittest")

    def serve(self, transport):
        """Activate the script; also freeze sleeps so retry/backoff math is observable."""
        sleep_patcher = mock.patch.object(esi.time, "sleep")
        sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        urlopen_patcher = mock.patch.object(esi.urllib.request, "urlopen", transport)
        urlopen_patcher.start()
        self.addCleanup(urlopen_patcher.stop)
        return sleep

    def page(self, doc, pages=None):
        headers = {"X-Pages": str(pages)} if pages is not None else None
        return FakeResponse(doc, headers)

    def error(self, status, payload="boom", **headers):
        return http_error(esi.BASE + self.PATH, status, {"error": payload}, headers)


class PaginationTests(TransportTestCase):
    def test_get_all_concatenates_pages_until_x_pages_is_exhausted(self):
        transport = ScriptedTransport(self.page([1, 2], 3), self.page([3, 4], 3), self.page([5, 6], 3))
        self.serve(transport)
        self.assertEqual(self.client.get_all("/characters/9/assets"), [1, 2, 3, 4, 5, 6])
        pages = [urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)["page"][0]
                 for r in transport.requests]
        self.assertEqual(pages, ["1", "2", "3"])

    def test_max_pages_caps_requests_even_when_server_offers_more(self):
        transport = ScriptedTransport(self.page([1, 2], 99), self.page([3, 4], 99))
        self.serve(transport)
        self.assertEqual(self.client.get_all("/characters/9/assets", max_pages=2), [1, 2, 3, 4])
        self.assertEqual(len(transport.requests), 2)

    def test_page_param_appends_to_an_existing_query(self):
        transport = ScriptedTransport(self.page([7]))
        self.serve(transport)
        out = self.client.get_all("/characters/9/industry/jobs?include_completed=true")
        self.assertEqual(out, [7])
        self.assertIn("include_completed=true&page=1", transport.requests[0].full_url)

    def test_unparsable_x_pages_is_treated_as_a_single_page(self):
        transport = ScriptedTransport(FakeResponse([1], {"X-Pages": "soon"}))
        self.serve(transport)
        self.assertEqual(self.client.get_all("/characters/9/assets"), [1])
        self.assertEqual(len(transport.requests), 1)


class RetryTests(TransportTestCase):
    def test_transient_bad_gateway_retries_then_succeeds(self):
        transport = ScriptedTransport(self.error(502, "bad gw"), self.page({"ok": True}))
        sleep = self.serve(transport)
        self.assertEqual(self.client.get(self.PATH), {"ok": True})
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleep.call_args_list, [mock.call(2)])

    def test_retry_after_header_sets_the_pause(self):
        transport = ScriptedTransport(self.error(429, "throttled", **{"Retry-After": "7"}),
                                      self.page({"ok": True}))
        sleep = self.serve(transport)
        self.assertEqual(self.client.get(self.PATH), {"ok": True})
        self.assertEqual(sleep.call_args_list, [mock.call(7)])

    def test_retry_pause_is_capped_at_thirty_seconds(self):
        transport = ScriptedTransport(self.error(503, "maintenance", **{"Retry-After": "600"}),
                                      self.page({"ok": True}))
        sleep = self.serve(transport)
        self.assertEqual(self.client.get(self.PATH), {"ok": True})
        self.assertEqual(sleep.call_args_list, [mock.call(30)])

    def test_persistent_failure_exhausts_three_attempts_then_raises(self):
        transport = ScriptedTransport(self.error(502, "bad gw"), self.error(502, "bad gw"),
                                      self.error(502, "bad gw"))
        sleep = self.serve(transport)
        with self.assertRaises(esi.EsiError) as caught:
            self.client.get(self.PATH)
        self.assertIn("HTTP 502 on /characters/9/skills", str(caught.exception))
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(4)])

    def test_client_errors_are_never_retried(self):
        transport = ScriptedTransport(self.error(400, "bad request"))
        sleep = self.serve(transport)
        with self.assertRaises(esi.EsiError) as caught:
            self.client.get(self.PATH)
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)
        sleep.assert_not_called()

    def test_network_error_is_wrapped_as_esierror(self):
        transport = ScriptedTransport(urllib.error.URLError(ConnectionRefusedError("refused")))
        self.serve(transport)
        with self.assertRaises(esi.EsiError) as caught:
            self.client.get(self.PATH)
        self.assertIn("network error", str(caught.exception))


class AuthTests(TransportTestCase):
    def test_401_raises_autherror_without_retrying(self):
        transport = ScriptedTransport(self.error(401, "token expired"))
        sleep = self.serve(transport)
        with self.assertRaises(esi.AuthError) as caught:
            self.client.get(self.PATH, token="t")
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertIn("token expired", str(caught.exception))
        self.assertEqual(len(transport.requests), 1)
        sleep.assert_not_called()

    def test_403_missing_scope_is_also_autherror(self):
        transport = ScriptedTransport(self.error(403, "missing scope"))
        self.serve(transport)
        with self.assertRaises(esi.AuthError):
            self.client.get(self.PATH, token="t")


class ErrorLimitTests(TransportTestCase):
    def test_low_error_budget_holds_the_next_request_back(self):
        transport = ScriptedTransport(
            FakeResponse({"a": 1}, {"X-ESI-Error-Limit-Remain": "10", "X-ESI-Error-Limit-Reset": "42"}),
            self.page({"a": 2}),
        )
        sleep = self.serve(transport)
        self.client.get(self.PATH, cache=False)
        self.assertGreater(self.client._blocked_until, esi.time.time())
        self.client.get(self.PATH, cache=False)
        self.assertEqual(len(transport.requests), 2)
        paused = sleep.call_args_list[0].args[0]
        self.assertGreater(paused, 41)   # waited out the announced window...
        self.assertLessEqual(paused, 42)  # ...without overshooting it

    def test_error_responses_also_note_the_limit(self):
        transport = ScriptedTransport(
            self.error(400, "bad request", **{"X-ESI-Error-Limit-Remain": "5", "X-ESI-Error-Limit-Reset": "30"}))
        self.serve(transport)
        with self.assertRaises(esi.EsiError):
            self.client.get(self.PATH, cache=False)
        self.assertGreater(self.client._blocked_until, esi.time.time())

    def test_healthy_headers_never_block(self):
        transport = ScriptedTransport(FakeResponse({"a": 1}, {"X-ESI-Error-Limit-Remain": "99",
                                                              "X-ESI-Error-Limit-Reset": "5"}))
        self.serve(transport)
        self.client.get(self.PATH, cache=False)
        self.assertEqual(self.client._blocked_until, 0.0)


class ServerClockTests(TransportTestCase):
    def test_date_header_anchors_now_to_server_time(self):
        skew = timedelta(hours=1)
        transport = ScriptedTransport(FakeResponse({"a": 1}, {
            "Date": format_datetime(datetime.now(timezone.utc) + skew, usegmt=True)}))
        self.serve(transport)
        self.client.get(self.PATH, cache=False)
        drift = (self.client.now() - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(drift, 3585)   # queue math follows the servers, not a skewed local clock
        self.assertLess(drift, 3615)

    def test_malformed_date_header_is_ignored(self):
        transport = ScriptedTransport(FakeResponse({"a": 1}, {"Date": "sometime Tuesday"}))
        self.serve(transport)
        self.assertEqual(self.client.get(self.PATH, cache=False), {"a": 1})
        self.assertEqual(self.client.server_offset, 0.0)


class FreshnessTests(TransportTestCase):
    """`get_meta` is what makes a quoted price honest: the age it reports has to be the age ESI
    said - through a cache hit and through a 304 revalidation alike."""

    PATH = "/markets/10000002/orders"

    @staticmethod
    def sent(req, name):
        return {k.lower(): v for k, v in req.header_items()}.get(name.lower())

    def test_freshness_headers_become_the_meta(self):
        transport = ScriptedTransport(FakeResponse([], {
            "Last-Modified": http_date(-120), "Expires": http_date(180), "Etag": '"v1"', "X-Pages": "3"}))
        self.serve(transport)
        value, meta = self.client.get_meta(self.PATH)
        self.assertEqual(value, [])
        age = self.client.now().timestamp() - meta.last_modified
        self.assertGreater(age, 100)     # the book ESI generated two minutes ago, not this instant
        self.assertLess(age, 200)
        self.assertEqual(meta.etag, '"v1"')
        self.assertEqual(meta.pages, 3)
        self.assertGreater(meta.expires, self.client.now().timestamp())

    def test_a_cache_hit_reports_the_age_of_the_bytes_it_handed_back(self):
        transport = ScriptedTransport(FakeResponse([{"price": 1}], {
            "Last-Modified": http_date(-120), "Expires": http_date(300)}))
        self.serve(transport)
        first, meta = self.client.get_meta(self.PATH)
        second, again = self.client.get_meta(self.PATH)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(second, first)
        self.assertEqual(again.last_modified, meta.last_modified)

    def test_a_revalidation_keeps_the_original_last_modified(self):
        transport = ScriptedTransport(
            FakeResponse([{"price": 1}], {"Last-Modified": http_date(-120), "Expires": http_date(-1),
                                          "Etag": '"v1"'}),
            self.error(304, "not modified", Expires=http_date(300)),
        )
        self.serve(transport)
        _value, meta = self.client.get_meta(self.PATH)
        value, again = self.client.get_meta(self.PATH)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.sent(transport.requests[1], "If-None-Match"), '"v1"')
        # Same bytes as before: the age of a revalidated payload is when it was generated.
        self.assertEqual(value, [{"price": 1}])
        self.assertEqual(again.last_modified, meta.last_modified)
        # The new Expires is adopted, so the next call does not touch the network again.
        self.client.get_meta(self.PATH)
        self.assertEqual(len(transport.requests), 2)

    def test_a_response_that_refuses_to_be_cached_evicts_the_stale_entry(self):
        transport = ScriptedTransport(
            FakeResponse([1], {"Expires": http_date(-1), "Etag": '"v1"'}),   # cached, already stale
            FakeResponse([2]),                                               # says "do not cache me"
            FakeResponse([3]),
        )
        self.serve(transport)
        self.client.get_meta(self.PATH)
        self.assertEqual(self.client.get_meta(self.PATH)[0], [2])
        self.assertEqual(self.client.get_meta(self.PATH)[0], [3])
        self.assertIsNone(self.sent(transport.requests[2], "If-None-Match"))

    def test_304_with_nothing_cached_is_an_error_not_an_empty_payload(self):
        transport = ScriptedTransport(self.error(304, "not modified", Expires=http_date(300)))
        self.serve(transport)
        with self.assertRaisesRegex(esi.EsiError, "with nothing cached"):
            self.client.get_meta(self.PATH)

    def test_a_304_on_an_uncached_request_is_a_clean_esi_error(self):
        # cache=False never sends If-None-Match, so this only happens if a server answers 304 on
        # its own; it must still surface as the kind of failure the CLI prints instead of raising.
        transport = ScriptedTransport(self.error(304, "not modified"))
        self.serve(transport)
        with self.assertRaisesRegex(esi.EsiError, "304"):
            self.client.get_meta(self.PATH, cache=False)


class RateLimitTests(TransportTestCase):
    """`market-order` is the one group with a token window (12000/15m) and no reset header, so a
    nearly-spent window slows us down instead of parking us until an unknown rollover."""

    def test_a_nearly_empty_window_holds_the_next_request_back(self):
        transport = ScriptedTransport(
            FakeResponse([], {"X-Ratelimit-Group": "market-order", "X-Ratelimit-Limit": "12000/15m",
                              "X-Ratelimit-Remaining": "900"}),
            FakeResponse([]),
        )
        sleep = self.serve(transport)
        self.client.get(self.PATH, cache=False)
        self.assertGreater(self.client._blocked_until, esi.time.time())
        self.client.get(self.PATH, cache=False)
        paused = sleep.call_args_list[0].args[0]
        self.assertGreater(paused, 0)
        self.assertLessEqual(paused, esi.RATELIMIT_HOLD_SECONDS)

    def test_plenty_of_budget_never_pauses(self):
        transport = ScriptedTransport(FakeResponse([], {"X-Ratelimit-Limit": "12000/15m",
                                                        "X-Ratelimit-Remaining": "11990"}))
        self.serve(transport)
        self.client.get(self.PATH, cache=False)
        self.assertEqual(self.client._blocked_until, 0.0)

    def test_an_unparsable_limit_header_is_ignored(self):
        transport = ScriptedTransport(FakeResponse([], {"X-Ratelimit-Limit": "plenty",
                                                        "X-Ratelimit-Remaining": "0"}))
        self.serve(transport)
        self.client.get(self.PATH, cache=False)
        self.assertEqual(self.client._blocked_until, 0.0)

    def test_errors_are_charged_against_the_window_too(self):
        # ESI charges 5 tokens per 4XX: a run of bad requests must throttle as well as a run of good ones.
        transport = ScriptedTransport(
            self.error(400, "bad request", **{"X-Ratelimit-Limit": "12000/15m",
                                              "X-Ratelimit-Remaining": "10"}))
        self.serve(transport)
        with self.assertRaises(esi.EsiError):
            self.client.get(self.PATH, cache=False)
        self.assertGreater(self.client._blocked_until, esi.time.time())


class PathTransport:
    """urlopen replacement answering by path: a fan-out runs concurrently, so a scripted
    response order would make these tests depend on which thread won."""

    def __init__(self, docs):
        self.docs = docs          # request path (with query when it has one) -> FakeResponse | Exception
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        split = urllib.parse.urlsplit(req.full_url)
        item = self.docs[f"{split.path}?{split.query}" if split.query else split.path]
        if isinstance(item, Exception):
            raise item
        return item


class FanoutTests(TransportTestCase):
    PATHS = ["/markets/1/orders", "/markets/2/orders", "/markets/3/orders"]

    def test_every_path_comes_back_with_its_payload_or_its_failure(self):
        transport = PathTransport({
            "/markets/1/orders": FakeResponse([{"price": 5}]),
            "/markets/2/orders": self.error(500, "shard down"),
            "/markets/3/orders": FakeResponse([{"price": 6}]),
        })
        self.serve(transport)
        got = self.client.get_many(self.PATHS)
        self.assertEqual(set(got), set(self.PATHS))
        self.assertEqual(got["/markets/1/orders"], [{"price": 5}])
        # One dead region is data, not an aborted scan: the other two still have prices.
        self.assertIsInstance(got["/markets/2/orders"], esi.EsiError)
        self.assertEqual(got["/markets/3/orders"], [{"price": 6}])

    def test_a_repeated_path_is_fetched_once(self):
        transport = PathTransport({"/markets/1/orders": FakeResponse([])})
        self.serve(transport)
        self.client.get_many(["/markets/1/orders"] * 3)
        self.assertEqual(len(transport.requests), 1)

    def test_no_paths_means_no_pool_and_no_request(self):
        transport = PathTransport({})
        self.serve(transport)
        self.assertEqual(self.client.get_many([]), {})
        self.assertEqual(transport.requests, [])

    def test_meta_fanout_keeps_each_response_and_folds_to_the_oldest(self):
        transport = PathTransport({
            "/markets/1/orders": FakeResponse([1], {"Last-Modified": http_date(-60), "Expires": http_date(300)}),
            "/markets/2/orders": FakeResponse([2], {"Last-Modified": http_date(-900), "Expires": http_date(60)}),
        })
        self.serve(transport)
        got = self.client.get_many_meta(["/markets/1/orders", "/markets/2/orders"])
        folded = esi.fold_meta(meta for _payload, meta in got.values())
        self.assertEqual(sorted(got), ["/markets/1/orders", "/markets/2/orders"])
        # The batch is as old as its oldest region and expires with its shortest-lived member.
        self.assertAlmostEqual(folded.last_modified, got["/markets/2/orders"][1].last_modified)
        self.assertAlmostEqual(folded.expires, got["/markets/2/orders"][1].expires)

    def test_folding_nothing_is_an_unknown_meta(self):
        folded = esi.fold_meta([])
        self.assertIsNone(folded.last_modified)
        self.assertIsNone(folded.expires)
        self.assertEqual(folded.pages, 1)

    def test_etags_only_fold_when_every_response_agrees(self):
        same = esi.fold_meta([esi.Meta(etag='"a"'), esi.Meta(etag='"a"')])
        self.assertEqual(same.etag, '"a"')
        split = esi.fold_meta([esi.Meta(etag='"a"'), esi.Meta(etag='"b"')])
        self.assertIsNone(split.etag)

    def test_paginated_fanout_concatenates_pages_per_path(self):
        transport = PathTransport({
            "/markets/1/orders?page=1": self.page([1], 2),
            "/markets/1/orders?page=2": self.page([2], 2),
            # get_all_meta always asks for page 1, even where one page is the whole answer.
            "/markets/2/orders?page=1": FakeResponse([9]),
        })
        self.serve(transport)
        got = self.client.get_many(["/markets/1/orders", "/markets/2/orders"], paginated=True)
        self.assertEqual(got["/markets/1/orders"], [1, 2])
        self.assertEqual(got["/markets/2/orders"], [9])


if __name__ == "__main__":
    unittest.main()
