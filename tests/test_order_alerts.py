"""Order alerts: what is news from one check to the next, and the Telegram channel. No network."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from eve_skills import job_times, order_alerts, telegram

UTC = timezone.utc
# Europe/Helsinki needs a time zone database: Windows has none without the optional tzdata package.
HAS_ZONES = job_times.zone_database_available()
needs_zones = unittest.skipUnless(HAS_ZONES, "no time zone database (Windows without tzdata)")
NOON = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)          # 12:00 in Helsinki
# +02:00 is Helsinki in winter, which every date below falls in; a fixed offset works on every platform.
SETTINGS = order_alerts.parse_settings({"exclude_locations": ["Z-EXCL"], "remind_hours": 4,
                                        "quiet_hours": "00:00-08:00", "timezone": "+02:00"})
READ = {"char:1"}


def row(order_id=1, status="undercut", price=100.0, **over):
    base = {"order_id": order_id, "owner": "Seller", "owner_key": "char:1", "type_name": "Widget II",
            "side": "sell", "location": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "price": price,
            "volume_total": 10, "volume_remain": 10, "status": status, "best_other": 99.0,
            "orders_ahead": 2, "units_ahead": 5, "days_ahead": 0.5, "suggest": 98.9}
    return {**base, **over}


def run(state, rows, at=NOON, closed=None, read=READ, settings=SETTINGS):
    return order_alerts.evaluate(state, rows, closed or {}, read, settings, at)


def job(job_id=7, left_min=10, at=NOON, **over):
    end = (at + timedelta(minutes=left_min)).isoformat().replace("+00:00", "Z")
    base = {"job_id": job_id, "owner_key": "char:1", "installer": "Builder", "activity": "invention",
            "product_id": 99, "product": "Widget II Blueprint", "location": "X-EXMPL - Example Labs",
            "runs": 1, "end": end, "status": "active"}
    return {**base, **over}


def jrun(state, jobs, at=NOON, read=READ, settings=SETTINGS):
    return order_alerts.evaluate(state, [], {}, READ, settings, at, jobs, read)


class EvaluateTests(unittest.TestCase):
    def test_an_undercut_is_announced_once_per_price_of_ours(self):
        state, alert = run(order_alerts.empty_state(), [row()])
        self.assertEqual([1], [r["order_id"] for r in alert.behind])
        state, alert = run(state, [row()], NOON + timedelta(minutes=20))
        self.assertTrue(alert.empty())                          # same price, still behind: not news
        state, alert = run(state, [row(price=98.9)], NOON + timedelta(minutes=40))
        self.assertEqual([1], [r["order_id"] for r in alert.behind])   # repriced and behind again

    def test_still_behind_is_reminded_after_the_interval_and_not_at_night(self):
        state, _ = run(order_alerts.empty_state(), [row()])
        _, alert = run(state, [row()], NOON + timedelta(hours=3))
        self.assertTrue(alert.empty())
        later, alert = run(state, [row()], NOON + timedelta(hours=4))
        self.assertEqual([1], [r["order_id"] for r in alert.still_behind])
        _, alert = run(later, [row()], NOON + timedelta(hours=6))
        self.assertTrue(alert.empty())                          # the clock restarted at the reminder
        night = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)        # 03:00 in Helsinki
        held, alert = run(later, [row()], night)
        self.assertTrue(alert.empty() and alert.silent)         # quiet hours: the reminder waits
        _, alert = run(held, [row()], datetime(2026, 11, 12, 6, 30, tzinfo=UTC))   # 08:30 Helsinki
        self.assertEqual(1, len(alert.still_behind))

    def test_back_on_top_only_when_the_competition_moved(self):
        state, _ = run(order_alerts.empty_state(), [row(), row(2)])
        _, alert = run(state, [row(status="cheapest"), row(2, status="cheapest", price=98.9)])
        self.assertEqual([1], [r["order_id"] for r in alert.back_on_top])   # 2 was repriced by us
        state, alert = run(state, [row(status="behind sliver"), row(2)])
        self.assertTrue(alert.empty())
        _, alert = run(state, [row(status="cheapest"), row(2)])
        self.assertEqual([1], [r["order_id"] for r in alert.back_on_top])   # the sliver kept the memory

    def test_a_filled_order_is_announced_when_its_history_row_arrives(self):
        state, _ = run(order_alerts.empty_state(), [row(status="cheapest")])
        state, alert = run(state, [], NOON + timedelta(minutes=20))
        self.assertTrue(alert.empty())                          # gone, history not there yet
        state, alert = run(state, [], NOON + timedelta(minutes=40), closed={1: "filled"})
        self.assertEqual([1], [r["order_id"] for r in alert.filled])
        self.assertEqual({}, state["pending"])

    def test_cancelled_or_long_missing_orders_say_nothing(self):
        state, _ = run(order_alerts.empty_state(), [row(status="cheapest"), row(2, status="cheapest")])
        state, alert = run(state, [], closed={1: "cancelled"})
        self.assertTrue(alert.empty())
        self.assertEqual(["2"], list(state["pending"]))
        state, alert = run(state, [], NOON + timedelta(hours=7))
        self.assertTrue(alert.empty())
        self.assertEqual({}, state["pending"])

    def test_an_unread_owner_keeps_its_orders(self):
        state, _ = run(order_alerts.empty_state(), [row()])
        state, alert = run(state, [], closed={1: "filled"}, read=set())
        self.assertTrue(alert.empty())
        self.assertIn("1", state["orders"])                     # not closed, not pending
        self.assertEqual({}, state["pending"])

    def test_excluded_locations_and_characters_never_alert(self):
        settings = order_alerts.parse_settings({"exclude_locations": ["z-excl"], "exclude_characters": ["Prime"], "timezone": "+02:00"})
        _, alert = run(order_alerts.empty_state(),
                       [row(1, location="Z-EXCL VIII - Moon 3"), row(2, owner="prime"), row(3)],
                       settings=settings)
        self.assertEqual([3], [r["order_id"] for r in alert.behind])

    def test_quiet_hours_send_silently(self):
        _, alert = run(order_alerts.empty_state(), [row()], datetime(2026, 11, 11, 23, 0, tzinfo=UTC))
        self.assertTrue(alert.silent)                           # 01:00 in Helsinki
        self.assertEqual(1, len(alert.behind))

    @needs_zones
    def test_quiet_hours_follow_an_iana_zone(self):
        settings = order_alerts.parse_settings({"quiet_hours": "00:00-08:00", "timezone": "Europe/Helsinki"})
        self.assertTrue(settings.quiet(datetime(2026, 11, 11, 23, 0, tzinfo=UTC)))     # 01:00 Helsinki
        self.assertFalse(settings.quiet(NOON))
        self.assertTrue(settings.quiet(datetime(2026, 7, 1, 4, 30, tzinfo=UTC)))       # 07:30 summer time

    def test_settings_errors_name_the_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            order_alerts.parse_settings({"quiet_hours": "late", "timezone": "+02:00"})
        self.assertIn("quiet_hours", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            order_alerts.parse_settings({"timezone": "Nowhere/Atlantis"})
        self.assertIn("timezone", str(ctx.exception))
        self.assertFalse(order_alerts.parse_settings({"quiet_hours": "", "timezone": "+02:00"}).quiet(NOON))

    def test_a_job_is_announced_once_within_the_notice_then_reminded_until_delivered(self):
        state, alert = jrun(order_alerts.empty_state(), [job(left_min=45)])
        self.assertTrue(alert.empty())                          # not yet within 20 minutes
        state, alert = jrun(state, [job(left_min=45)], NOON + timedelta(minutes=30))
        self.assertEqual([7], [j["job_id"] for j in alert.jobs_soon])
        state, alert = jrun(state, [job(left_min=45)], NOON + timedelta(minutes=50))
        self.assertTrue(alert.empty())                          # finished, announced already
        state, alert = jrun(state, [job(left_min=45)], NOON + timedelta(hours=4, minutes=30))
        self.assertEqual([7], [j["job_id"] for j in alert.jobs_waiting])   # the 4 h roll-up
        state, alert = jrun(state, [], NOON + timedelta(hours=5))
        self.assertTrue(alert.empty())                          # delivered: dropped quietly
        self.assertEqual({}, state["jobs"])
        self.assertIsNone(state["last_reminder"])

    def test_jobs_join_the_order_reminder_on_one_clock(self):
        state, _ = run(order_alerts.empty_state(), [row()])
        state, alert = order_alerts.evaluate(state, [row()], {}, READ, SETTINGS, NOON + timedelta(hours=2),
                                             [job(left_min=-1, at=NOON + timedelta(hours=2))], READ)
        self.assertEqual(1, len(alert.jobs_soon))
        self.assertEqual([], alert.still_behind)
        _, alert = order_alerts.evaluate(state, [row()], {}, READ, SETTINGS, NOON + timedelta(hours=4),
                                         [job(left_min=-1, at=NOON + timedelta(hours=2))], READ)
        self.assertEqual((1, 1), (len(alert.still_behind), len(alert.jobs_waiting)))
        text = order_alerts.render(alert, SETTINGS, NOON + timedelta(hours=4))
        self.assertEqual("EVE: 1 still behind, 1 job to deliver\n\n"
                         "⚙ X-EXMPL\n   Builder: 1 invention\n\n"
                         "Seller · Jita\n▼ Widget II\n   undercut by 2 orders · 5 units ahead (0.5 d)", text)

    def test_unread_installers_and_paused_jobs_keep_their_jobs(self):
        state, _ = jrun(order_alerts.empty_state(), [job(left_min=5)])
        state, alert = jrun(state, [], read=set())
        self.assertTrue(alert.empty())
        self.assertIn("7", state["jobs"])
        state, alert = jrun(state, [job(left_min=5, status="paused")], NOON + timedelta(hours=5))
        self.assertTrue(alert.empty())
        self.assertIn("7", state["jobs"])
        state, _ = order_alerts.evaluate(state, [], {}, READ, SETTINGS, NOON)   # jobs not read: kept
        self.assertIn("7", state["jobs"])

    def test_job_messages_count_per_character_facility_and_activity(self):
        jobs = [job(i, left_min=10 + i, product="Small EM Shield Reinforcer II Blueprint") for i in range(3)]
        jobs.append(job(9, left_min=-5, activity="copying"))
        jobs.append(job(10, left_min=5, installer="Other", location="Y-EXMPL - Example Factory",
                        activity="manufacturing"))
        _, alert = jrun(order_alerts.empty_state(), jobs)
        text = order_alerts.render(alert, SETTINGS, NOON)
        self.assertEqual("EVE jobs: 5 jobs ending\n\n"
                         "⚙ X-EXMPL\n   Builder: 3 invention, 1 copying · 1 ready, rest in 12 min\n"
                         "⚙ Y-EXMPL\n   Other: 1 manufacturing · in 5 min", text)
        self.assertNotIn("Blueprint", text)                     # the products are not listed
        settings = order_alerts.parse_settings({"job_alerts": False, "exclude_characters": ["builder", "other"],
                                                 "timezone": "+02:00"})
        self.assertFalse(settings.job_alerts)
        _, alert = jrun(order_alerts.empty_state(), jobs, settings=settings)
        self.assertTrue(alert.empty())

    def test_waiting_jobs_are_labelled_only_next_to_jobs_just_ending(self):
        state, _ = jrun(order_alerts.empty_state(), [job(left_min=5)])
        later = NOON + timedelta(hours=4)
        _, alert = jrun(state, [job(left_min=5), job(8, left_min=10, at=later)], later)
        text = order_alerts.render(alert, SETTINGS, later)
        self.assertEqual("EVE jobs: 1 job ending, 1 job to deliver\n\n"
                         "⚙ X-EXMPL\n   Builder: 1 invention · in 10 min\n\n"
                         "Still to deliver:\n⚙ X-EXMPL\n   Builder: 1 invention", text)

    def test_orders_are_grouped_by_character_and_place_without_prices(self):
        rows = [row(), row(2, status="outbid", side="buy", suggest=None, orders_ahead=1, units_ahead=1, days_ahead=None),
                row(3, type_name="Gadget II", units_ahead=900, days_ahead=3.0, orders_ahead=7),
                row(4, owner="Other", owner_key="char:1", location="X-EXMPL - Example Market")]
        _, alert = run(order_alerts.empty_state(), rows)
        text = order_alerts.render(alert, SETTINGS)
        self.assertEqual("EVE orders: 4 behind\n\n"
                         "Other · X-EXMPL\n"
                         "▼ Widget II\n   undercut by 2 orders · 5 units ahead (0.5 d)\n\n"
                         "Seller · Jita\n"                           # deepest first
                         "▼ Gadget II\n   undercut by 7 orders · 900 units ahead (3.0 d)\n"
                         "▼ Widget II\n   undercut by 2 orders · 5 units ahead (0.5 d)\n"
                         "▲ Widget II\n   outbid by 1 order · 1 unit ahead", text)
        for price in ("100", "99", "98.9"):
            self.assertNotIn(price, text)

    def test_back_on_top_filled_and_the_reminder_label(self):
        alert = order_alerts.Alert(behind=[row(4, type_name="Gadget II")],
                                   back_on_top=[row(2, status="cheapest"), row(5, status="top", side="buy")],
                                   filled=[row(3, side="buy")], still_behind=[row()])
        text = order_alerts.render(alert, SETTINGS, NOON)
        self.assertEqual("EVE orders: 1 behind, 2 back on top, 1 filled, 1 still behind\n\n"
                         "Seller · Jita\n"
                         "▼ Gadget II\n   undercut by 2 orders · 5 units ahead (0.5 d)\n"
                         "✓ Widget II\n   cheapest again\n"
                         "✓ Widget II\n   top bid again\n"
                         "● Widget II\n   bought all 10\n\n"
                         "Still behind:\n"                           # only next to other order news
                         "Seller · Jita\n"
                         "▼ Widget II\n   undercut by 2 orders · 5 units ahead (0.5 d)", text)

    def test_where_is_the_system_of_a_station_or_structure(self):
        for name, system in (("Jita IV - Moon 4 - Caldari Navy Assembly Plant", "Jita"),
                             ("X-EXMPL - Example Labs", "X-EXMPL"), ("Old Man Star VI - Some Station", "Old Man Star"),
                             ("facility 123", "facility 123"), ("", "")):
            self.assertEqual(system, order_alerts._where({"location": name}))
        self.assertEqual(["⚙ Jobs", "   Builder: 1 invention"], order_alerts.job_lines([job(location=None)]))

    def test_time_left_of_a_group(self):
        ts = NOON.timestamp()
        for lefts, text in (((-5, 0), "ready"), ((12, 12), "in 12 min"), ((5, 12), "in 5–12 min"),
                            ((50, 70), "in 50 min–1 h 10 min"), ((-1, -1, 12), "2 ready, rest in 12 min")):
            self.assertEqual(text, order_alerts.time_left([job(i, left_min=m) for i, m in enumerate(lefts)], ts))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TelegramTests(unittest.TestCase):
    TOKEN = "123:SECRET"

    def opener(self, doc=None, error=None):
        calls = []

        def open_(req, timeout=None):
            calls.append((req.full_url, json.loads(req.data)))
            if error:
                raise error
            return _Response(json.dumps(doc if doc is not None else {"ok": True, "result": {}}).encode())
        return open_, calls

    def test_long_text_is_split_on_lines(self):
        text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(100))
        parts = telegram.chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= telegram.MAX_MESSAGE for p in parts))
        self.assertEqual(text, "\n".join(parts))

    def test_send_posts_each_part_with_the_silent_flag(self):
        opener, calls = self.opener()
        self.assertEqual(1, telegram.send(self.TOKEN, 42, "hello", silent=True, opener=opener))
        url, body = calls[0]
        self.assertTrue(url.endswith("/bot123:SECRET/sendMessage"))
        self.assertEqual((42, "hello", True), (body["chat_id"], body["text"], body["disable_notification"]))

    def test_errors_carry_telegrams_reason_never_the_token(self):
        refused = urllib.error.HTTPError("https://api.telegram.org/bot123:SECRET/sendMessage", 400, "Bad",
                                         {}, io.BytesIO(b'{"ok": false, "description": "chat not found"}'))
        opener, _ = self.opener(error=refused)
        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.send(self.TOKEN, 42, "x", opener=opener)
        self.assertIn("chat not found", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))
        opener, _ = self.opener(error=urllib.error.URLError("no route"))
        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.bot_name(self.TOKEN, opener=opener)
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_the_chat_comes_from_the_message_sent_to_the_bot(self):
        updates = {"ok": True, "result": [
            {"message": {"chat": {"id": 7, "type": "private", "username": "old"}}},
            {"message": {"chat": {"id": 9, "type": "private", "first_name": "Kiri"}}},
            {"message": {"chat": {"id": 7, "type": "private", "username": "old"}}}]}
        opener, _ = self.opener(updates)
        self.assertEqual([7, 9], [c["id"] for c in telegram.private_chats(self.TOKEN, opener=opener)])


if __name__ == "__main__":
    unittest.main()
