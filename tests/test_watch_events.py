"""Watch-mode determinism: transition model, durable history, events command.

The transition model runs on injected timestamps and in-memory state (no disk,
no clock); persistence runs in a throwaway $XDG_STATE_HOME; the command-level
tests drive the real CLI through tests/fake_esi.py with time.sleep patched per
cycle. Nothing here touches a real clock deadline, network, token or desktop:
notify-send is always mocked, never executed."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from eve_skills import cli, orders, watchstate
from tests.fake_esi import (ADA, CORP_SHARED, SKILL_NAV, SKILL_WIDE, VELA, FakeEsiEnv,
                            owner_order)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_800_000_000.0  # synthetic epoch base; retention is relative to the ts a test passes


def item(sid: int, level: int, status: str, finish: str | None = None,
         name: str | None = None) -> watchstate.QueueItem:
    return watchstate.QueueItem(skill_id=sid, finished_level=level, status=status,
                                name=name or f"skill {sid}", finish_date=finish)


def obs(cid: int = ADA.character_id, name: str = "Ada Vane", items=(), trained=None
        ) -> watchstate.CharacterObservation:
    return watchstate.CharacterObservation(character_id=cid, character_name=name,
                                           items=tuple(items), trained_levels=dict(trained or {}))


def stamp(epoch: float) -> str:
    """ISO-8601 UTC for an injected poll time, so expiry arithmetic stays inside the test."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def orow(order_id: int, *, closed: bool = False, **kw) -> orders.Order:
    """One ESI order row as the fetcher hands it over; `closed=True` reads it like a history row."""
    return orders.normalise(owner_order(order_id, **kw), f"char:{ADA.character_id}", ADA.name,
                            closed=closed)


def oobs(open_rows=(), history_rows=(), key: str | None = None, name: str | None = None,
         history_ok: bool = True, names=None) -> watchstate.OrderObservation:
    return watchstate.OrderObservation(key or f"char:{ADA.character_id}", name or ADA.name,
                                       tuple(open_rows), tuple(history_rows), history_ok,
                                       dict(names or {}))


class TransitionModelTests(unittest.TestCase):
    """observe() is pure: injected state and timestamps only."""

    def test_first_observation_announces_nothing(self):
        # A queue that already holds a finished-but-unlogged item must not produce news.
        poll = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), item(SKILL_WIDE, 5, "done", "F-WIDE")])]
        state, events = watchstate.observe(watchstate.empty_state(), poll, T0)
        self.assertEqual(events, [])
        entry = state["characters"][str(ADA.character_id)]
        self.assertEqual(entry["queue_len"], 2)
        self.assertIn(f"{SKILL_NAV}:2", entry["known"])   # only the training item is tracked

    def test_repeated_identical_polls_are_quiet(self):
        poll = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])]
        state, _ = watchstate.observe(watchstate.empty_state(), poll, T0)
        for tick in range(3):
            state, events = watchstate.observe(state, poll, T0 + tick)
            self.assertEqual(events, [])

    def test_completion_announced_once_with_stable_id(self):
        first = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), item(SKILL_WIDE, 5, "queued", "F-WIDE")])]
        state, _ = watchstate.observe(watchstate.empty_state(), first, T0)
        settled = [obs(items=[item(SKILL_NAV, 2, "done", "F-NAV"), item(SKILL_WIDE, 5, "queued", "F-WIDE")],
                       trained={SKILL_NAV: 1})]
        state, events = watchstate.observe(state, settled, T0 + 60)
        self.assertEqual([e.kind for e in events], ["training_finished"])
        ev = events[0]
        self.assertEqual((ev.skill_id, ev.finished_level, ev.finish_date), (SKILL_NAV, 2, "F-NAV"))
        # a restart replaying the same prior state derives the same dedup id
        restarted = {"version": 1, "characters": {"91000001": {
            "name": "Ada Vane", "queue_len": 2, "last_finish": "F-WIDE",
            "known": {f"{SKILL_NAV}:2": {"skill_name": "skill 1011", "finish_date": "F-NAV"}},
            "updated": T0}}}
        _, again = watchstate.observe(restarted, settled, T0 + 999)
        self.assertEqual([e.id for e in again], [ev.id])
        # ...and the claimed state never re-announces it
        _, events = watchstate.observe(state, settled, T0 + 120)
        self.assertEqual(events, [])

    def test_vanished_item_settled_by_trained_level(self):
        first = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), item(SKILL_WIDE, 5, "queued", "F-WIDE")])]
        state, _ = watchstate.observe(watchstate.empty_state(), first, T0)
        # CCP dropped the finished entry entirely; only the trained level proves it completed.
        gone = [obs(items=[item(SKILL_WIDE, 5, "queued", "F-WIDE")], trained={SKILL_NAV: 2})]
        state, events = watchstate.observe(state, gone, T0 + 60)
        self.assertEqual([e.kind for e in events], ["training_finished"])

    def test_unsettled_vanish_is_not_announced(self):
        first = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), item(SKILL_WIDE, 5, "queued", "F-WIDE")])]
        state, _ = watchstate.observe(watchstate.empty_state(), first, T0)
        # item gone but trained level never caught up (e.g. queue edited away): no false event
        _, events = watchstate.observe(state, [obs(items=[item(SKILL_WIDE, 5, "queued", "F-WIDE")],
                                                   trained={SKILL_NAV: 1})], T0 + 60)
        self.assertEqual(events, [])

    def test_a_vanished_item_is_announced_once_when_the_level_catches_up_later(self):
        # /skills and /skillqueue are separately cached: the queue entry can disappear a
        # poll before the trained level rises. The completion must still be announced.
        rest = item(SKILL_WIDE, 5, "queued", "F-WIDE")   # keeps this about the vanished item
        first = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), rest])]
        state, _ = watchstate.observe(watchstate.empty_state(), first, T0)
        state, events = watchstate.observe(state, [obs(items=[rest], trained={SKILL_NAV: 1})], T0 + 60)
        self.assertEqual(events, [])                                     # nothing settled yet
        state, events = watchstate.observe(state, [obs(items=[rest], trained={SKILL_NAV: 2})], T0 + 120)
        self.assertEqual([(e.kind, e.finished_level) for e in events], [("training_finished", 2)])
        _, again = watchstate.observe(state, [obs(items=[rest], trained={SKILL_NAV: 2})], T0 + 180)
        self.assertEqual(again, [])                                      # and only once

    def test_an_item_that_never_settles_is_forgotten_after_the_grace_window(self):
        rest = item(SKILL_WIDE, 5, "queued", "F-WIDE")
        first = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV"), rest])]
        state, _ = watchstate.observe(watchstate.empty_state(), first, T0)
        stale = T0 + (watchstate.SETTLE_GRACE_DAYS * 86400) + 60
        state, events = watchstate.observe(state, [obs(items=[rest], trained={SKILL_NAV: 1})], stale)
        self.assertEqual(events, [])
        self.assertEqual(state["characters"][str(ADA.character_id)]["known"], {})
        _, later = watchstate.observe(state, [obs(items=[rest], trained={SKILL_NAV: 2})], stale + 60)
        self.assertEqual(later, [])   # a cancelled item must never turn into a completion

    def test_queue_empty_fires_once_per_episode(self):
        state = watchstate.empty_state()
        fill = [obs(items=[item(SKILL_NAV, 2, "training", "F-ONE")])]
        state, events = watchstate.observe(state, fill, T0)                       # first sight: quiet
        self.assertEqual(events, [])
        state, events = watchstate.observe(state, [obs()], T0 + 60)               # emptied
        self.assertEqual([e.kind for e in events], ["queue_empty"])
        empty_id = events[0].id
        state, events = watchstate.observe(state, [obs()], T0 + 120)              # stays empty: quiet
        self.assertEqual(events, [])
        state, _ = watchstate.observe(state, [obs(items=[item(SKILL_WIDE, 5, "training", "F-TWO")])], T0 + 180)
        _, events = watchstate.observe(state, [obs()], T0 + 240)                  # next episode: new event
        self.assertEqual([e.kind for e in events], ["queue_empty"])
        self.assertNotEqual(events[0].id, empty_id)

    def test_queue_empty_fires_again_when_the_items_carried_no_finish_date(self):
        # CCP issues no schedule dates for an item it will not train, so `last_finish` stays put
        # across such an episode. Two emptyings then hashed to the same id and the second was read
        # as a duplicate: the user who asked to be told their queue emptied was told once, ever.
        blocked = [obs(items=[item(SKILL_NAV, 2, "blocked", None)])]
        state, _ = watchstate.observe(watchstate.empty_state(), blocked, T0)
        state, first = watchstate.observe(state, [obs()], T0 + 60)
        state, _ = watchstate.observe(state, blocked, T0 + 120)
        _, second = watchstate.observe(state, [obs()], T0 + 180)
        self.assertEqual(["queue_empty"], [e.kind for e in first])
        self.assertEqual(["queue_empty"], [e.kind for e in second])
        self.assertNotEqual(first[0].id, second[0].id)

    def test_state_written_before_episodes_existed_keeps_its_event_id(self):
        # An upgrade must not re-announce what the old version already announced, so an entry with
        # no `episode` field still hashes to exactly the id the old two-part formula produced.
        state, _ = watchstate.observe(watchstate.empty_state(),
                                      [obs(items=[item(SKILL_NAV, 2, "training", "F-ONE")])], T0)
        legacy = json.loads(json.dumps(state))
        legacy["characters"][str(ADA.character_id)].pop("episode")
        _, events = watchstate.observe(legacy, [obs()], T0 + 60)
        self.assertEqual([watchstate._event_id("queue_empty", ADA.character_id, "F-ONE")],
                         [e.id for e in events])

    def test_failed_fetch_keeps_prior_state_and_late_event_keeps_its_id(self):
        poll = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])]
        state, _ = watchstate.observe(watchstate.empty_state(), poll, T0)
        state, events = watchstate.observe(state, [], T0 + 60)   # character failed this cycle
        self.assertEqual(events, [])
        self.assertIn(f"{SKILL_NAV}:2", state["characters"][str(ADA.character_id)]["known"])
        # when it answers again, the completion still fires - with the id it would have had
        settled = [obs(items=[item(SKILL_NAV, 2, "done", "F-NAV")])]
        _, late = watchstate.observe(state, settled, T0 + 120)
        finished = [e for e in late if e.kind == "training_finished"]
        self.assertEqual(len(finished), 1)
        immediate_state = watchstate.observe(watchstate.empty_state(), poll, T0)[0]
        _, fresh = watchstate.observe(immediate_state, settled, T0 + 61)
        self.assertEqual([e.id for e in fresh if e.kind == "training_finished"], [finished[0].id])


class OrderTransitionTests(unittest.TestCase):
    """observe_orders() is pure as well: injected state, injected clock, no disk.

    What is being pinned here is not the arithmetic but the honesty: which moment a row claims to
    know, and how loudly it is allowed to say so."""

    KEY = f"char:{ADA.character_id}"
    GRACE = watchstate.ORDER_SETTLE_GRACE_DAYS * 86400

    def entry(self, state):
        return state["owners"][self.KEY]

    def test_first_sight_records_the_backlog_and_announces_nothing_new(self):
        # ESI remembers ~90 days of closed orders. Announcing that backlog as news would open the
        # user's first watch on a hundred bells and teach them to ignore the next real one.
        poll = [oobs(open_rows=[orow(1, remain=40, total=100)],
                     history_rows=[orow(2, closed=True, state="filled", remain=0, total=60)])]
        state, events = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        self.assertEqual([e.kind for e in events], ["order_filled"])
        self.assertTrue(events[0].data["backfill"])
        self.assertTrue(events[0].data["ts_estimated"])
        # Recorded, not remembered in memory: the next poll - or a second watcher - claims nothing.
        state, again = watchstate.observe_orders(state, poll, T0 + 60)
        self.assertEqual(again, [])
        entry = self.entry(state)
        self.assertEqual(list(entry["settled"]), ["2"])
        self.assertIn("1", entry["open"])

    def test_a_witnessed_fill_is_dated_when_the_watcher_saw_it(self):
        state, events = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(7, remain=40, total=100)])], T0)
        self.assertEqual(events, [])                       # first sight never announces
        closed = [oobs(history_rows=[orow(7, closed=True, state="filled", remain=0, total=100)])]
        state, events = watchstate.observe_orders(state, closed, T0 + 60)
        self.assertEqual([e.kind for e in events], ["order_filled"])
        ev = events[0]
        # This watcher had the order on the book until now, so detection time is the honest answer.
        self.assertEqual((ev.ts, ev.data["backfill"], ev.data["ts_estimated"]), (T0 + 60, False, False))
        self.assertEqual((ev.data["filled"], ev.data["volume_total"]), (100, 100))
        _, again = watchstate.observe_orders(state, closed, T0 + 120)
        self.assertEqual(again, [])                        # and only ever once

    def test_the_event_id_never_depends_on_when_we_think_it_happened(self):
        # Two watchers with two clocks, one fact. A timestamp in the id would record the same closing
        # twice every time the estimate moved; the reason and the order id are all it may name.
        poll = [oobs(history_rows=[orow(9, closed=True, state="cancelled", remain=5, total=20)])]
        _, first = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        _, late = watchstate.observe_orders(watchstate.empty_state(), poll, T0 + 86400)
        self.assertEqual([e.id for e in late], [e.id for e in first])

    def test_an_expired_order_is_dated_when_it_lapsed_not_when_we_noticed(self):
        # ESI has no closed-at field, but an expired row's expiry is arithmetic on its own issued and
        # duration - a fact, not a guess, so it beats the poll time by weeks.
        issued = T0 - 45 * 86400          # a 30-day order that lapsed two weeks before this poll
        poll = [oobs(history_rows=[orow(11, closed=True, state="expired", issued=stamp(issued),
                                       duration=30, remain=5, total=20)])]
        _, events = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        ev = events[0]
        self.assertEqual(ev.kind, "order_expired")
        self.assertEqual(ev.ts, issued + 30 * 86400)
        self.assertLess(ev.ts, T0)
        self.assertEqual(ev.data["expires"], stamp(issued + 30 * 86400))
        self.assertTrue(ev.data["ts_estimated"])            # still a date we never witnessed

    def test_a_backfilled_order_cannot_be_dated_after_its_own_expiry(self):
        # Already gone when we first looked: all that is known is that it closed before our first poll
        # and could not outlive its expiry, so the honest ts is the earlier of the two.
        issued = T0 - 100 * 86400
        poll = [oobs(history_rows=[orow(12, closed=True, state="cancelled", issued=stamp(issued),
                                       duration=90, remain=5, total=10)])]
        _, events = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        self.assertEqual(events[0].ts, issued + 90 * 86400)
        self.assertTrue(events[0].data["ts_estimated"])

    def test_a_history_row_without_a_lifetime_is_dated_at_detection(self):
        # Closed rows report duration 0; adding zero days would fake a lapse at the issue date.
        poll = [oobs(history_rows=[orow(13, closed=True, state="cancelled", issued=stamp(T0 - 86400),
                                       duration=0)])]
        _, events = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        self.assertIsNone(events[0].data["expires"])
        self.assertEqual(events[0].ts, T0)

    def test_a_vanished_order_waits_for_history_then_closes_once(self):
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(21, remain=100, total=100)])], T0)
        gone = [oobs()]          # the live book answers with nothing and history has no row for it
        state, events = watchstate.observe_orders(state, gone, T0 + 60)
        self.assertEqual(events, [])                       # noticed: the wait starts here
        self.assertIn("21", self.entry(state)["pending"])
        state, events = watchstate.observe_orders(state, gone, T0 + 60 + self.GRACE)
        self.assertEqual(events, [])                       # at the boundary: still waiting
        state, events = watchstate.observe_orders(state, gone, T0 + 61 + self.GRACE)
        self.assertEqual([e.kind for e in events], ["order_closed"])
        ev = events[0]
        self.assertFalse(ev.data["backfill"])              # we watched it go: that is news
        self.assertTrue(ev.data["ts_estimated"])           # ...but not a moment we can prove
        self.assertEqual(self.entry(state)["pending"], {})
        # A history row surfacing afterwards cannot add a second verdict on the same order.
        _, later = watchstate.observe_orders(
            state, [oobs(history_rows=[orow(21, closed=True, state="filled", remain=0, total=100)])],
            T0 + 62 + self.GRACE)
        self.assertEqual(later, [])

    def test_the_reason_wins_when_history_catches_up_inside_the_grace_window(self):
        # The two documents are cached separately: the book can drop a row a poll before history says
        # why. Guessing `closed` there would tell the user less than ESI does.
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(22, remain=100, total=100)])], T0)
        state, events = watchstate.observe_orders(state, [oobs()], T0 + 60)
        self.assertEqual(events, [])
        self.assertIn("22", self.entry(state)["pending"])
        _, events = watchstate.observe_orders(
            state, [oobs(history_rows=[orow(22, closed=True, state="filled", remain=0, total=100)])],
            T0 + 120)
        self.assertEqual([e.kind for e in events], ["order_filled"])
        self.assertFalse(events[0].data["ts_estimated"])   # witnessed open: detection is honest

    def test_an_order_that_flickers_back_onto_the_book_is_not_a_closure(self):
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(23, remain=100, total=100)])], T0)
        state, events = watchstate.observe_orders(state, [oobs()], T0 + 60)
        self.assertEqual(events, [])
        back = [oobs(open_rows=[orow(23, remain=100, total=100)])]
        state, events = watchstate.observe_orders(state, back, T0 + 120)
        self.assertEqual(events, [])
        self.assertEqual(self.entry(state)["pending"], {})   # the episode is over, not deferred
        _, later = watchstate.observe_orders(state, back, T0 + self.GRACE * 2)
        self.assertEqual(later, [])

    def test_a_failed_history_call_holds_the_verdict_back(self):
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(31, remain=100, total=100)])], T0)
        state, events = watchstate.observe_orders(state, [oobs()], T0 + 60)
        self.assertEqual(events, [])                       # the wait for history starts here
        # Now past that window, but the history document did not answer this cycle: an outage is not a
        # transition, so nothing may be concluded about why the order left the book.
        state, events = watchstate.observe_orders(state, [oobs(history_ok=False)],
                                                  T0 + 60 + self.GRACE + 60)
        self.assertEqual(events, [])
        self.assertIn("31", self.entry(state)["pending"])     # owed, not forgotten
        _, events = watchstate.observe_orders(state, [oobs()], T0 + 60 + self.GRACE + 120)
        self.assertEqual([e.kind for e in events], ["order_closed"])

    def test_an_owner_that_stops_answering_is_not_a_closing_spree(self):
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(41, remain=100, total=100)])], T0)
        # A cycle about somebody else says nothing about this owner: its rows survive untouched.
        state, events = watchstate.observe_orders(state, [oobs(key="char:91000002", name="Vela Krinn")],
                                                  T0 + 10 * 86400)
        self.assertEqual(events, [])
        self.assertIn("41", self.entry(state)["open"])

    def test_each_half_of_the_state_survives_the_other_halves_poll(self):
        # One state file, two pollers: neither may rebuild away what the other recorded.
        state = watchstate.empty_state()
        state, _ = watchstate.observe(state, [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])], T0)
        state, _ = watchstate.observe_orders(state, [oobs(open_rows=[orow(51)])], T0 + 60)
        self.assertIn(str(ADA.character_id), state["characters"])
        self.assertIn(self.KEY, state["owners"])
        state, _ = watchstate.observe(state, [obs(items=[])], T0 + 120)
        self.assertIn(self.KEY, state["owners"])
        state, _ = watchstate.observe_orders(state, [], T0 + 180)
        self.assertIn(str(ADA.character_id), state["characters"])

    def test_a_state_esi_has_never_documented_is_closed_rather_than_guessed(self):
        poll = [oobs(history_rows=[orow(61, closed=True, state="liquidated")])]
        _, events = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        self.assertEqual([e.kind for e in events], ["order_closed"])

    def test_a_history_row_still_listed_as_open_is_not_a_closure(self):
        # The two documents overlap live; a row ESI calls open must never end an order.
        state, events = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(history_rows=[orow(71, closed=True, state="open")])], T0)
        self.assertEqual(events, [])
        self.assertEqual(self.entry(state)["settled"], {})     # nothing was claimed against it
        _, events = watchstate.observe_orders(
            state, [oobs(history_rows=[orow(71, closed=True, state="cancelled", remain=5, total=10)])],
            T0 + 60)
        self.assertEqual([e.kind for e in events], ["order_cancelled"])   # still claimable later

    def test_corporation_events_belong_to_the_corporation_not_the_member(self):
        corp = dict(key=f"corp:{CORP_SHARED}", name="Shared Ledger Holdings")
        state, _ = watchstate.observe_orders(
            watchstate.empty_state(), [oobs(open_rows=[orow(81, remain=100, total=100)], **corp)], T0)
        closed = [oobs(history_rows=[orow(81, closed=True, state="filled", remain=0, total=100)], **corp)]
        state, events = watchstate.observe_orders(state, closed, T0 + 60)
        self.assertEqual([e.kind for e in events], ["order_filled"])
        ev = events[0]
        # Whosever token read the book is not a fact about the order: filing it under a member would
        # let `events --char` claim a corporation's trade for one person.
        self.assertIsNone(ev.character_id)
        self.assertEqual((ev.character_name, ev.data["owner_key"]),
                         ("Shared Ledger Holdings", f"corp:{CORP_SHARED}"))
        _, again = watchstate.observe_orders(state, closed, T0 + 120)   # a colleague's token adds nothing
        self.assertEqual(again, [])

    def test_type_names_arrive_with_the_event_so_prose_needs_no_lookup(self):
        poll = [oobs(open_rows=[orow(91, remain=100, total=100)], names={34: "Tritanium"})]
        state, _ = watchstate.observe_orders(watchstate.empty_state(), poll, T0)
        state, _ = watchstate.observe_orders(state, [oobs()], T0 + 60)
        _, events = watchstate.observe_orders(state, [oobs()], T0 + 61 + self.GRACE)
        self.assertEqual((events[0].data["type_id"], events[0].data["type_name"]), (34, "Tritanium"))


class PersistenceTests(unittest.TestCase):
    """commit()/load_events() against a throwaway $XDG_STATE_HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-watch-")
        self.addCleanup(self.tmp.cleanup)
        self.state_home = os.path.join(self.tmp.name, "state")
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.state_home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_readers_never_create_directories(self):
        self.assertEqual(watchstate.load_state(), watchstate.empty_state())
        self.assertEqual(watchstate.load_events(), ([], 0))
        self.assertFalse(os.path.exists(self.state_home))
        self.assertTrue(watchstate.state_file(create=False).endswith(
            os.path.join("eve-skills", "watch-state.json")))

    def test_commit_persists_and_restart_does_not_reannounce(self):
        training = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])]
        self.assertEqual(watchstate.commit(training, now_ts=T0), [])   # initial: no events
        settled = [obs(trained={SKILL_NAV: 2})]
        events = watchstate.commit(settled, now_ts=T0 + 60)
        self.assertEqual([e.kind for e in events], ["training_finished", "queue_empty"])
        with open(watchstate.state_file(create=False), encoding="utf-8") as fh:
            state_doc = json.load(fh)
        self.assertEqual(state_doc["version"], watchstate.SCHEMA_VERSION)
        with open(watchstate.events_file(create=False), encoding="utf-8") as fh:
            self.assertEqual(len([ln for ln in fh if ln.strip()]), 2)
        # a restarted (or second) watcher replays the same poll and claims nothing
        self.assertEqual(watchstate.commit(settled, now_ts=T0 + 120), [])

    def test_crash_between_append_and_state_write_replays_without_duplicates(self):
        training = [obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])]
        watchstate.commit(training, now_ts=T0)
        # simulate a watcher that appended the events but died before claiming state:
        _, pending = watchstate.observe(watchstate.load_state(), [obs(trained={SKILL_NAV: 2})], T0 + 60)
        with open(watchstate.events_file(create=False), "a", encoding="utf-8") as fh:
            for ev in pending:
                fh.write(json.dumps(ev.to_json()) + "\n")
        replayed = watchstate.commit([obs(trained={SKILL_NAV: 2})], now_ts=T0 + 61)
        # the transition was already recorded, so the replay must announce nothing: a returned
        # event is what rings the bell and fires notify-send
        self.assertEqual(replayed, [])
        with open(watchstate.events_file(create=False), encoding="utf-8") as fh:
            ids = [json.loads(ln)["id"] for ln in fh if ln.strip()]
        self.assertEqual(sorted(ids), sorted(e.id for e in pending))         # recorded exactly once

    def test_corrupt_history_lines_are_skipped_and_counted(self):
        watchstate.commit([obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])], now_ts=T0)
        watchstate.commit([obs(trained={SKILL_NAV: 2})], now_ts=T0 + 60)
        with open(watchstate.events_file(create=False), "a", encoding="utf-8") as fh:
            fh.write("garbage, not json\n")
            fh.write(json.dumps({"kind": "training_finished"}) + "\n")   # missing required fields
        rows, skipped = watchstate.load_events()
        self.assertEqual(skipped, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(isinstance(r["ts"], float) for r in rows))

    def _spawn_children(self, script: str, argv_per_child: list[list[str]]):
        env = {**os.environ, "XDG_STATE_HOME": self.state_home, "PYTHONPATH": REPO_ROOT}
        procs = [subprocess.Popen([sys.executable, "-c", script, *argv], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for argv in argv_per_child]
        claimed = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            claimed.append(int(out.decode().strip()))
        return claimed

    def test_concurrent_watchers_claim_one_transition_once(self):
        watchstate.commit([obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])], now_ts=T0)
        child = textwrap.dedent("""
            import sys
            from eve_skills import watchstate
            settled = watchstate.CharacterObservation(91000001, "Ada Vane", items=(), trained_levels={1011: 2})
            print(len(watchstate.commit([settled], now_ts=float(sys.argv[1]))))
        """)
        claimed = self._spawn_children(child, [[str(T0 + 60)]] * 3)
        self.assertEqual(sum(claimed), 2, f"three racing watchers claimed {claimed} events")
        with open(watchstate.events_file(create=False), encoding="utf-8") as fh:
            ids = [json.loads(ln)["id"] for ln in fh if ln.strip()]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_concurrent_watchers_on_different_characters_lose_nothing(self):
        chars = [(91000001, "A", 1011, "F-1"), (91000002, "B", 1012, "F-2"), (91000003, "C", 1013, "F-3")]
        watchstate.commit([obs(cid, name, items=[item(sid, 2, "training", fin)])
                           for cid, name, sid, fin in chars], now_ts=T0)
        child = textwrap.dedent("""
            import sys
            from eve_skills import watchstate
            cid, sid = int(sys.argv[1]), int(sys.argv[2])
            settled = watchstate.CharacterObservation(cid, "x", items=(), trained_levels={sid: 2})
            print(len(watchstate.commit([settled], now_ts=float(sys.argv[3]))))
        """)
        claimed = self._spawn_children(child, [[str(cid), str(sid), str(T0 + 60)]
                                               for cid, _, sid, _ in chars])
        self.assertEqual(claimed, [2, 2, 2])   # each watcher claimed exactly its own character's pair
        rows, skipped = watchstate.load_events()
        self.assertEqual((len(rows), skipped), (6, 0))   # no rewrite clobbered another's append

    def test_one_terminal_event_per_order_survives_a_second_watcher(self):
        # Two watchers with different clocks both report the same closing: one row, whichever wins.
        watchstate.commit([], [oobs(open_rows=[orow(901, remain=5, total=5)])], now_ts=T0)
        closed = [oobs(history_rows=[orow(901, closed=True, state="filled", remain=0, total=5)])]
        first = watchstate.commit([], closed, now_ts=T0 + 60)
        again = watchstate.commit([], closed, now_ts=T0 + 3600)      # a "restart" on a later clock
        self.assertEqual([e.kind for e in first], ["order_filled"])
        self.assertEqual(again, [])
        rows, skipped = watchstate.load_events()
        self.assertEqual((len(rows), skipped), (1, 0))

    def test_a_row_written_before_orders_existed_still_loads(self):
        # Real machines already have an events.jsonl. Upgrading must not invalidate that history, so a
        # row without the new `data` key has to read back with an empty payload, not be skipped.
        legacy = {"id": "a" * 16, "ts": T0 - 60, "kind": "training_finished",
                  "character_id": ADA.character_id, "character_name": ADA.name, "skill_id": SKILL_NAV,
                  "skill_name": "Navigation", "finished_level": 2, "finish_date": "F-NAV"}
        watchstate.commit([obs(items=[item(SKILL_NAV, 2, "training", "F-NAV")])], now_ts=T0)
        with open(watchstate.events_file(create=False), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(legacy) + "\n")
        rows, skipped = watchstate.load_events()
        self.assertEqual(skipped, 0)
        old = next(r for r in rows if r["id"] == "a" * 16)
        self.assertEqual(old["data"], {})
        self.assertEqual((old["skill_name"], old["finished_level"]), ("Navigation", 2))


class WatchLoopMixin:
    """Fake-ESI setup and the per-cycle sleep hook, shared by both --watch command families."""

    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.addCleanup(self.env.stop)

    def run_watch(self, cycles: int, argv=None, hooks=()):
        """Run `cycles` polls; each callable in `hooks` fires at one sleep boundary."""
        seq = list(hooks) + [None] * (cycles - len(hooks) - 1) + [KeyboardInterrupt()]

        def fake_sleep(_seconds):
            nxt = seq.pop(0) if seq else None
            if isinstance(nxt, BaseException):
                raise nxt
            if callable(nxt):
                nxt()

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("time.sleep", side_effect=fake_sleep), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(list(argv or ["skills", "--watch", "1"]))
        return code, out.getvalue(), err.getvalue()


class WatchCliTestCase(WatchLoopMixin, unittest.TestCase):
    """`skills --watch`: training transitions announced once, plus whatever orders say."""

    def complete_navigation(self):
        """Make Ada's training item finish for real: level settled, queue emptied."""
        self.env.set_trained_level(ADA, SKILL_NAV, 2)
        self.env.set_queue(ADA, [])

    # -- dashboard -----------------------------------------------------------

    def test_first_cycle_is_quiet_and_persists_state(self):
        code, out, err = self.run_watch(1)
        self.assertEqual(code, 130)                      # Ctrl-C: clean interrupt exit
        self.assertEqual(err, "\n")                     # Ctrl-C leaves a bare newline, no warnings
        self.assertIn("eve-skills watch -", out)         # cycle is timestamped
        self.assertIn("Ada Vane", out)
        self.assertIn("Vela Krinn", out)
        self.assertNotIn("\a", out)                      # no false alerts on first sight
        self.assertIn("Navigation to L2", out)           # compact status shows the active item
        self.assertTrue(os.path.exists(os.path.join(self.env.state_home, "eve-skills", "watch-state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.env.state_home, "eve-skills", "events.jsonl")))

    def test_completion_and_queue_empty_announced_once_across_polls_and_restart(self):
        self.run_watch(1)
        self.complete_navigation()
        code, out, _ = self.run_watch(1)
        self.assertEqual(code, 130)
        self.assertIn("\aAda Vane: Navigation to L2 - finished training", out)
        self.assertIn("training queue is now empty", out)

        _, out, _ = self.run_watch(1)                    # repeated poll of the same world
        self.assertNotIn("\a", out)
        _, out, _ = self.run_watch(1)                    # "restart": new invocation, persisted state
        self.assertNotIn("\a", out)

        code, out, err = self.env.run(["events", "--json"])
        self.assertEqual((code, err), (0, ""))
        kinds = sorted(ev["kind"] for ev in json.loads(out))
        self.assertEqual(kinds, ["queue_empty", "training_finished"])

    def test_watch_never_persists_token_data(self):
        self.run_watch(1)
        self.complete_navigation()
        self.run_watch(1)
        blob = ""
        for name in ("watch-state.json", "events.jsonl"):
            with open(os.path.join(self.env.state_home, "eve-skills", name), encoding="utf-8") as fh:
                blob += fh.read()
        for secret in (ADA.token, VELA.token, "access_token", "refresh_token", "client_id"):
            self.assertNotIn(secret, blob)

    def test_compact_table_marks_stale_rows_and_keeps_healthy_data(self):
        def fail_vela():
            self.env.server.get(f"/characters/{VELA.character_id}/skills", error=(500, {"error": "esi down"}))
        code, out, err = self.run_watch(2, hooks=[fail_vela])     # cycle 1 good, cycle 2 partial outage
        self.assertEqual(code, 130)
        self.assertIn("warning: skipped Vela Krinn: HTTP 500", err)
        self.assertIn("! Vela Krinn:", out)                       # failure visible on the dashboard
        self.assertIn("stale", out)                               # her last-known row survives with its age
        self.assertNotIn("no data yet", out)                      # ...not a blanked-out placeholder
        self.assertIn("Ada Vane", out)
        self.assertIn("6.00M", out)                               # healthy character still rendered

    def test_all_characters_failing_retries_without_crashing(self):
        self.run_watch(1)
        for char in (ADA, VELA):
            self.env.server.get(f"/characters/{char.character_id}/skills", error=(500, {"error": "esi down"}))
        code, out, err = self.run_watch(1)
        self.assertEqual(code, 130)                               # an outage never kills the session
        self.assertIn("no character data this cycle - retrying next poll", err)
        self.assertIn("eve-skills watch -", out)                  # header still timestamped each cycle
        self.assertIn("Ada Vane", out)                            # last-known rows retained
        self.assertIn("Vela Krinn", out)

    def test_full_view_opt_in_keeps_legacy_rendering(self):
        code, out, _ = self.run_watch(1, argv=["skills", "--watch", "1", "--full"])
        self.assertEqual(code, 130)
        self.assertIn("=" * 72, out)
        self.assertIn("TRAINING QUEUE (2)", out)

    # -- notifications ---------------------------------------------------------

    def test_notify_once_per_event_across_polls_and_restart(self):
        calls: list[list[str]] = []
        ok = subprocess.CompletedProcess(["notify-send"], 0)
        record = lambda cmd, **kw: calls.append(cmd) or ok
        argv = ["skills", "--watch", "1", "--notify"]
        with mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run", side_effect=record):
            self.run_watch(1, argv)                               # no events -> no notifications
            self.assertEqual(calls, [])
            self.complete_navigation()
            self.run_watch(1, argv)
            self.assertEqual(len(calls), 2)                       # exactly one per new event
            self.assertTrue(any("finished training" in c[2] for c in calls))
            self.assertTrue(any("queue is now empty" in c[2] for c in calls))
            self.run_watch(1, argv)                               # repeated poll: silent
            self.assertEqual(len(calls), 2)
        later: list[list[str]] = []
        with mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run", side_effect=lambda cmd, **kw: later.append(cmd) or ok):
            self.run_watch(1, argv)                               # restart: still silent
            self.assertEqual(later, [])

    def test_notify_without_a_backend_explains_itself_once_but_bells_still_fire(self):
        # Silence after --notify promised a ping is the failure this guards: absent notify-send,
        # the loop says so once per process - not once per poll, never twice across restarts in
        # one process - runs nothing, and keeps ringing the terminal's own \a bell everywhere.
        argv = ["skills", "--watch", "1", "--notify"]
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(cli, "_notify_warned", False), \
                mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            _code, _out, first = self.run_watch(1, argv)
            self.complete_navigation()
            code, out, second = self.run_watch(1, argv)
        self.assertEqual(code, 130)
        self.assertIn("\aAda Vane: Navigation to L2 - finished training", out)
        self.assertEqual(1, (first + second).count("notify-send"))
        self.assertIn("events are still printed and recorded", first + second)

    def test_notify_failure_never_kills_watch(self):
        argv = ["skills", "--watch", "1", "--notify"]
        with mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run", side_effect=OSError("notify-send exploded")):
            self.run_watch(1, argv)
            self.complete_navigation()
            code, out, err = self.run_watch(1, argv)
        self.assertEqual(code, 130)
        self.assertIn("\aAda Vane: Navigation to L2 - finished training", out)
        self.assertNotIn("Traceback", err)


class WatchClearTests(unittest.TestCase):
    """What a --watch frame opens with on a terminal: real ANSI where it works, a rule line where
    an old Windows console would print escape-code litter instead. The loop is driven directly with
    an injected poll - empty cycles claim nothing, so no disk, ESI, clock or desktop is involved."""

    def frames(self, cycles: int, tty: bool, *patches) -> str:
        state = {"n": 0}

        def poll():
            state["n"] += 1
            if state["n"] >= cycles:
                raise KeyboardInterrupt()
            return cli.WatchCycle(title="skills", body="Ada Vane: Navigation to L2")

        class Out(io.StringIO):
            def isatty(self):
                return tty

        out, err = Out(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(mock.patch("time.sleep"))
            stack.enter_context(mock.patch("sys.stdout", out))
            stack.enter_context(mock.patch("sys.stderr", err))
            code = cli.watch_loop(SimpleNamespace(watch=1, notify=False), poll)
        self.assertEqual(130, code)
        return out.getvalue()

    def test_posix_terminal_gets_the_real_clear(self):
        # The unconditional-clear branch is the non-Windows one, so it is selected through the
        # product's own seam: on a Windows runner stdout may be a pipe with no VT mode to set.
        with mock.patch.object(cli.paths, "is_windows", return_value=False):
            text = self.frames(2, tty=True)
        self.assertIn("\x1b[H\x1b[2J", text)
        self.assertNotIn("-" * 72, text)

    def test_a_redirected_frame_opens_with_nothing_decorative(self):
        text = self.frames(2, tty=False)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("-" * 72, text)

    def test_windows_console_without_virtual_terminal_falls_back_to_a_rule_line(self):
        # Forced Windows on a host that has no ctypes.windll: enabling cannot succeed, so the
        # frame must contain no escape at all - printing one is exactly the litter being avoided.
        text = self.frames(2, True,
                           mock.patch.object(cli.paths, "is_windows", return_value=True),
                           mock.patch.object(cli, "_vt_processing", None))
        self.assertNotIn("\x1b", text)
        self.assertIn("-" * 72, text)

    def test_windows_console_with_virtual_terminal_still_clears(self):
        text = self.frames(2, True,
                           mock.patch.object(cli.paths, "is_windows", return_value=True),
                           mock.patch.object(cli, "_vt_processing", None),
                           mock.patch.object(cli, "_enable_vt_processing", return_value=True))
        self.assertIn("\x1b[H\x1b[2J", text)
        self.assertNotIn("-" * 72, text)


class OrderWatchCliTests(WatchLoopMixin, unittest.TestCase):
    """The real loops over a live order book: what gets recorded, and what gets said out loud."""

    def ada_book(self, rows=(), history=()):
        return self.env.install_watch_orders(ADA, rows, history)

    def test_first_cycle_records_esis_backlog_without_announcing_it(self):
        # ~90 days of closed orders arrive on the first poll. They belong in the history; they are not
        # news, and a watcher that rang a hundred bells once teaches the user to ignore the next one.
        self.ada_book(history=[owner_order(501, state="filled", remain=0, total=60)])
        code, out, err = self.run_watch(1)
        self.assertEqual((code, err), (130, "\n"))         # no warnings, and nothing announced
        self.assertNotIn("\a", out)
        _, out, _ = self.env.run(["events", "--json"])
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["order_filled"])
        self.assertTrue(rows[0]["data"]["backfill"])

    def test_a_fill_between_cycles_is_announced_once_across_polls_and_restart(self):
        book = self.ada_book(rows=[owner_order(502, remain=100, total=100)])
        self.run_watch(1)                                  # first sight: recorded, silent

        def sold_out():
            book.open.clear()
            book.history.append(owner_order(502, state="filled", remain=0, total=100))

        code, out, err = self.run_watch(2, hooks=[sold_out])
        self.assertEqual((code, err), (130, "\n"))
        self.assertIn("\aAda Vane: sold 100 x Tritanium at 5.50 ISK (Jita - Mradd) - order filled", out)
        _, out, _ = self.run_watch(1)                      # a further poll of the same world
        self.assertNotIn("\a", out)
        _, out, _ = self.run_watch(1)                      # and a restart from persisted state
        self.assertNotIn("\a", out)
        _, out, _ = self.env.run(["events", "--json"])
        self.assertEqual([r["kind"] for r in json.loads(out)], ["order_filled"])

    def test_a_corporation_order_is_announced_under_the_corporations_name(self):
        book = self.env.install_watch_corp_orders(CORP_SHARED, ADA.token,
                                                  rows=[owner_order(503, remain=10, total=10)])
        self.run_watch(1)

        def withdrawn():
            book.open.clear()
            book.history.append(owner_order(503, state="cancelled", remain=10, total=10))

        _, out, _ = self.run_watch(2, hooks=[withdrawn])
        self.assertIn("\aShared Ledger Holdings: 10 x Tritanium listed at 5.50 ISK (Jita - Mradd)"
                      " - cancelled", out)
        _, out, _ = self.env.run(["events", "--json"])
        row = json.loads(out)[0]
        # The member whose token read the book is not the owner of the order.
        self.assertIsNone(row["character_id"])
        self.assertEqual(row["data"]["owner_key"], f"corp:{CORP_SHARED}")

    def test_orders_watch_tabulates_the_live_book(self):
        self.ada_book(rows=[owner_order(601, price=9.5, remain=40, total=200),
                            owner_order(602, buy=True, price=4.0, remain=100, escrow=400)])
        code, out, err = self.run_watch(1, argv=["orders", "--watch", "1"])
        self.assertEqual((code, err), (130, "\n"))
        self.assertIn("eve-skills orders watch -", out)
        self.assertIn("sell book ISK", out)                # what is at stake, per owner
        self.assertIn("Ada Vane", out)
        self.assertIn("Tritanium", out)                    # orders named, not numbered
        self.assertNotIn("reported no escrow", out)        # both buy orders funded: nothing to caveat

    def test_a_history_outage_is_shown_instead_of_a_guessed_closure(self):
        book = self.ada_book(rows=[owner_order(604)])
        self.run_watch(1, argv=["orders", "--watch", "1"])

        def outage():
            book.open.clear()                              # the order is gone from the live book...
            book.history_error = (500, {"error": "history shard unavailable"})   # ...and nothing explains it

        code, out, _ = self.run_watch(2, hooks=[outage], argv=["orders", "--watch", "1"])
        self.assertEqual(code, 130)                        # an outage never ends the session
        self.assertIn("order history unreadable this cycle", out)
        self.assertNotIn("\a", out)                        # no verdict invented for it
        _, out, _ = self.env.run(["events", "--json"])
        self.assertEqual(json.loads(out), [])

    def test_a_run_with_nothing_to_poll_hints_once_and_keeps_running(self):
        # Vela was stored without the orders consent: say so once, then keep the loop alive - the
        # user may re-consent in another window and this process should be there for it.
        code, out, err = self.run_watch(3, argv=["orders", "--watch", "1", "--char", VELA.name])
        self.assertEqual(code, 130)
        self.assertEqual(err.count("eve-skills login --scopes"), 1)
        self.assertIn("(no order book could be polled this cycle)", out)

    def test_no_orders_watches_training_only(self):
        # The opt-out has to reach the fetch, not just the display: polling orders every five minutes
        # is a rate-limit budget of its own.
        self.ada_book(history=[owner_order(701, state="filled", remain=0, total=5)])
        code, out, err = self.run_watch(1, argv=["skills", "--watch", "1", "--no-orders"])
        self.assertEqual((code, err), (130, "\n"))
        _, out, _ = self.env.run(["events", "--json"])
        self.assertEqual(json.loads(out), [])              # nothing ingested at all

    def test_training_and_order_news_share_one_cycle(self):
        book = self.ada_book(rows=[owner_order(801, remain=50, total=50)])
        self.run_watch(1)

        def everything_moves():
            self.env.set_trained_level(ADA, SKILL_NAV, 2)   # Navigation finishes for real...
            self.env.set_queue(ADA, [])                     # ...and leaves the queue empty
            book.open.clear()
            book.history.append(owner_order(801, state="filled", remain=0, total=50))

        _, out, _ = self.run_watch(2, hooks=[everything_moves])
        self.assertIn("\aAda Vane: Navigation to L2 - finished training", out)
        self.assertIn("\aAda Vane: sold 50 x Tritanium at 5.50 ISK (Jita - Mradd) - order filled", out)
        _, out, _ = self.env.run(["events", "--json"])
        self.assertEqual(sorted(r["kind"] for r in json.loads(out)),
                         ["order_filled", "queue_empty", "training_finished"])


class EventsCommandTests(unittest.TestCase):
    """`events` is read-only over the history: formats, filters, limits, honesty."""

    def setUp(self):
        self.env = FakeEsiEnv()
        self.env.start()
        self.env.install_core()
        self.addCleanup(self.env.stop)

    def seed(self):
        """Two events with controlled ordering: Vela empties, then Ada finishes (queue stays non-empty)."""
        watchstate.commit([obs(items=[item(SKILL_NAV, 2, "training", "F-NAV", name="Navigation")])], now_ts=T0)
        watchstate.commit([obs(VELA.character_id, "Vela Krinn",
                               items=[item(SKILL_WIDE, 3, "training", "F-WIDE")])], now_ts=T0 + 1)
        watchstate.commit([obs(VELA.character_id, "Vela Krinn")], now_ts=T0 + 2)          # vela empties
        watchstate.commit([obs(items=[item(SKILL_WIDE, 5, "queued", "F-WIDE-2")],
                               trained={SKILL_NAV: 2})], now_ts=T0 + 3)                   # ada finishes

    def test_empty_history_is_honest_in_every_format(self):
        code, out, err = self.env.run(["events"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("no recorded events yet", out)
        code, out, _ = self.env.run(["events", "--json"])
        self.assertEqual((code, json.loads(out)), (0, []))
        code, out, _ = self.env.run(["events", "--csv"])
        rows = list(csv.reader(out.splitlines()))
        self.assertEqual(len(rows), 1)                             # header only

    def test_text_table_lists_event_sentences(self):
        self.seed()
        code, out, err = self.env.run(["events"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Ada Vane: Navigation to L2 - finished training", out)
        self.assertIn("Vela Krinn: training queue is now empty", out)

    def test_json_rows_are_machine_readable(self):
        self.seed()
        _, out, _ = self.env.run(["events", "--json"])
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["training_finished", "queue_empty"])
        first = rows[0]
        self.assertEqual((first["character_id"], first["skill_id"], first["finished_level"]),
                         (ADA.character_id, SKILL_NAV, 2))
        self.assertEqual(len({r["id"] for r in rows}), 2)
        self.assertTrue(all(r["ts"] >= T0 for r in rows))

    def test_limit_bounds_to_the_newest_first(self):
        self.seed()
        _, out, _ = self.env.run(["events", "--json", "--limit", "1"])
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["character_id"]), ("training_finished", ADA.character_id))

    def test_char_filter_by_name_and_by_bare_id(self):
        self.seed()
        _, out, _ = self.env.run(["events", "--json", "--char", "Vela"])
        rows = json.loads(out)
        self.assertEqual([r["character_id"] for r in rows], [VELA.character_id])
        # a logged-out character has no token record; the bare id still finds its events
        watchstate.commit([obs(91000003, "Ghost Runner", items=[item(SKILL_WIDE, 3, "training", "F-G")])],
                          now_ts=T0 + 4)
        watchstate.commit([obs(91000003, "Ghost Runner")], now_ts=T0 + 5)
        _, out, _ = self.env.run(["events", "--json", "--char", "91000003"])
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["queue_empty"])

    def test_corrupt_lines_warn_without_losing_the_rest(self):
        self.seed()
        with open(watchstate.events_file(create=False), "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        code, out, err = self.env.run(["events", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("skipped 1 unreadable event history line", err)
        self.assertEqual(len(json.loads(out)), 2)

    def test_csv_round_trips_every_field(self):
        self.seed()
        _, out, _ = self.env.run(["events", "--csv"])
        rows = list(csv.DictReader(out.splitlines()))
        self.assertEqual(rows[0]["kind"], "training_finished")
        self.assertEqual(rows[0]["skill_name"], "Navigation")
        self.assertEqual(rows[1]["kind"], "queue_empty")
        self.assertEqual(rows[1]["skill_id"], "")                  # queue-empty rows carry no skill fields
        self.assertRegex(rows[0]["time_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    # -- order events -------------------------------------------------------

    def seed_orders(self):
        """One fill this watcher witnessed, plus one expiry read out of history."""
        names = {34: "Tritanium"}
        watchstate.commit([], [oobs(open_rows=[orow(801, remain=30, total=30)], names=names)],
                          now_ts=T0)
        watchstate.commit([], [oobs(history_rows=[
            orow(801, closed=True, state="filled", remain=0, total=30),
            orow(802, closed=True, state="expired", remain=7, total=7)], names=names)], now_ts=T0 + 60)

    def test_order_events_read_as_sentences(self):
        self.seed_orders()
        code, out, err = self.env.run(["events"])
        self.assertEqual((code, err), (0, ""))
        # `events` never fetches, so the sentence names what the row recorded and nothing else.
        self.assertIn("Ada Vane: sold 30 x Tritanium at 5.50 ISK - order filled", out)
        self.assertIn("expired with 0 of 7 sold", out)
        self.assertIn("[history]", out)          # that one was read out of ESI's backlog, not watched
        self.assertEqual(out.count("[time estimated]"), 0)   # the fill was witnessed: no hedge needed

    def test_a_closure_esi_cannot_explain_says_so(self):
        # The order left the live book, history stayed silent, and the wait ran out. The sentence must
        # admit both gaps: ESI never says why it closed, nor exactly when.
        watchstate.commit([], [oobs(open_rows=[orow(810, remain=50, total=50)])], now_ts=T0)
        watchstate.commit([], [oobs()], now_ts=T0 + 60)      # first cycle that misses it: the wait starts
        events = watchstate.commit([], [oobs()],
                                   now_ts=T0 + 61 + watchstate.ORDER_SETTLE_GRACE_DAYS * 86400)
        self.assertEqual([e.kind for e in events], ["order_closed"])
        _, out, _ = self.env.run(["events"])
        self.assertIn("closed, reason unknown to ESI [time estimated]", out)

    def test_kind_filter_selects_order_events(self):
        self.seed()
        self.seed_orders()
        _, out, _ = self.env.run(["events", "--json", "--kind", "order_filled"])
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["order_filled"])
        self.assertEqual(rows[0]["data"]["order_id"], 801)
        _, out, _ = self.env.run(["events", "--json", "--kind", "training_finished",
                                  "--kind", "order_expired"])
        self.assertEqual(sorted(r["kind"] for r in json.loads(out)),
                         ["order_expired", "training_finished"])

    # -- owner filter -------------------------------------------------------

    def seed_corp_event(self):
        """A corporation order's cancellation - recorded with no character identity at all."""
        watchstate.commit([], [oobs(key="corp:98356123", name="Shared Ledger Holdings",
                                    history_rows=[orow(901, closed=True, state="cancelled",
                                                       remain=4, total=9)],
                                    names={34: "Tritanium"})], now_ts=T0 + 120)

    def test_owner_filter_takes_the_exact_owner_key(self):
        self.seed_orders()
        _, out, _ = self.env.run(["events", "--json", "--owner", f"char:{ADA.character_id}"])
        rows = json.loads(out)
        # both of Ada's order events, and only hers - the expiry is timed off the order's own issued
        # date, so which of the two prints first is ESI's arithmetic, not something to pin here.
        self.assertEqual({"order_expired", "order_filled"}, {r["kind"] for r in rows})
        self.assertTrue(all(r["data"]["owner_key"] == f"char:{ADA.character_id}" for r in rows))
        _, out, _ = self.env.run(["events", "--json", "--owner", "char:90000099"])
        self.assertEqual([], json.loads(out))

    def test_owner_filter_matches_a_name_fragment_case_insensitively(self):
        self.seed()
        self.seed_orders()
        self.seed_corp_event()
        _, out, _ = self.env.run(["events", "--json", "--owner", "ledger"])
        self.assertEqual(["corp:98356123"], [r["data"]["owner_key"] for r in json.loads(out)])
        # A training event carries no owner, so asking for an owner never returns one - even when the
        # fragment is Ada's own name, which is also the owner of her order events.
        _, out, _ = self.env.run(["events", "--json", "--owner", "ADA VANE"])
        self.assertEqual(sorted(r["kind"] for r in json.loads(out)),
                         ["order_expired", "order_filled"])

    def test_the_owner_filter_reaches_what_char_cannot(self):
        # Corporation events deliberately have no `character_id`: `--char` belongs to a character and
        # must not claim them, so `--owner` is the only way to see them.
        self.seed()
        self.seed_corp_event()
        _, out, _ = self.env.run(["events", "--json", "--char", "Ada"])
        self.assertEqual(["training_finished"], [r["kind"] for r in json.loads(out)])
        _, out, _ = self.env.run(["events", "--json", "--owner", "corp:98356123"])
        rows = json.loads(out)
        self.assertEqual(["order_cancelled"], [r["kind"] for r in rows])
        self.assertIsNone(rows[0]["character_id"])
        self.assertEqual("Shared Ledger Holdings", rows[0]["data"]["owner_name"])

    def test_owner_and_kind_filters_compose(self):
        self.seed()
        self.seed_orders()
        _, out, _ = self.env.run(["events", "--json", "--owner", "ada", "--kind", "order_filled"])
        rows = json.loads(out)
        self.assertEqual([r["kind"] for r in rows], ["order_filled"])
        self.assertEqual(801, rows[0]["data"]["order_id"])

    def test_unknown_kind_is_rejected_with_the_list_of_kinds(self):
        code, _, err = self.env.run(["events", "--kind", "order_melted"])
        self.assertEqual(code, 1)
        self.assertIn("unknown event kind: order_melted", err)
        for kind in watchstate.EVENT_KINDS:        # the message is how the user learns the vocabulary
            self.assertIn(kind, err)

    def test_csv_carries_the_order_payload(self):
        self.seed()            # a training row too: one header has to serve both kinds
        self.seed_orders()
        _, out, _ = self.env.run(["events", "--csv"])
        rows = {r["kind"]: r for r in csv.DictReader(out.splitlines())}
        filled = rows["order_filled"]
        self.assertEqual((filled["order_id"], filled["type_name"], filled["filled"]),
                         ("801", "Tritanium", "30"))
        self.assertEqual((filled["backfill"], filled["ts_estimated"]), ("0", "0"))
        expired = rows["order_expired"]
        self.assertEqual((expired["volume_remain"], expired["backfill"], expired["ts_estimated"]),
                         ("7", "1", "1"))
        self.assertEqual(rows["training_finished"]["order_id"], "")   # shared header, empty cells


if __name__ == "__main__":
    unittest.main()
