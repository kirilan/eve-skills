"""Order alerts: what is news from one check to the next, and the Telegram channel. No network."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from eve_skills import order_alerts, telegram

UTC = timezone.utc
NOON = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)          # 12:00 in Sofia
SETTINGS = order_alerts.parse_settings({"exclude_locations": ["WY-9LL"], "remind_hours": 4,
                                        "quiet_hours": "00:00-08:00", "timezone": "Europe/Sofia"})
READ = {"char:1"}


def row(order_id=1, status="undercut", price=100.0, **over):
    base = {"order_id": order_id, "owner": "Seller", "owner_key": "char:1", "type_name": "Widget II",
            "side": "sell", "location": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "price": price,
            "volume_total": 10, "volume_remain": 10, "status": status, "best_other": 99.0,
            "units_ahead": 5, "days_ahead": 0.5, "suggest": 98.9}
    return {**base, **over}


def run(state, rows, at=NOON, closed=None, read=READ, settings=SETTINGS):
    return order_alerts.evaluate(state, rows, closed or {}, read, settings, at)


def job(job_id=7, left_min=10, at=NOON, **over):
    end = (at + timedelta(minutes=left_min)).isoformat().replace("+00:00", "Z")
    base = {"job_id": job_id, "owner_key": "char:1", "installer": "Builder", "activity": "invention",
            "product_id": 99, "product": "Widget II Blueprint", "location": "4GYV-Q - Labs",
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
        night = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)        # 03:00 in Sofia
        held, alert = run(later, [row()], night)
        self.assertTrue(alert.empty() and alert.silent)         # quiet hours: the reminder waits
        _, alert = run(held, [row()], datetime(2026, 11, 12, 6, 30, tzinfo=UTC))   # 08:30 Sofia
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
        settings = order_alerts.parse_settings({"exclude_locations": ["wy-9ll"], "exclude_characters": ["Prime"]})
        _, alert = run(order_alerts.empty_state(),
                       [row(1, location="WY-9LL VIII - Moon 3"), row(2, owner="prime"), row(3)],
                       settings=settings)
        self.assertEqual([3], [r["order_id"] for r in alert.behind])

    def test_quiet_hours_send_silently(self):
        _, alert = run(order_alerts.empty_state(), [row()], datetime(2026, 11, 11, 23, 0, tzinfo=UTC))
        self.assertTrue(alert.silent)                           # 01:00 in Sofia
        self.assertEqual(1, len(alert.behind))

    def test_settings_errors_name_the_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            order_alerts.parse_settings({"quiet_hours": "late"})
        self.assertIn("quiet_hours", str(ctx.exception))
        self.assertFalse(order_alerts.parse_settings({"quiet_hours": ""}).quiet(NOON))

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
        self.assertTrue(text.startswith("EVE: 1 still behind, 1 jobs to deliver"))
        self.assertIn("Jobs to deliver (reminder every 4 h):", text)

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

    def test_job_messages_group_one_product(self):
        jobs = [job(i, left_min=10 + i, product="Small EM Shield Reinforcer II Blueprint") for i in range(3)]
        _, alert = jrun(order_alerts.empty_state(), jobs)
        text = order_alerts.render(alert, SETTINGS, NOON)
        self.assertTrue(text.startswith("EVE jobs: 3 jobs ending"))
        self.assertIn("⚙ Job ending: Small EM Shield Reinforcer II Blueprint\n"
                      "   Builder · invention · 3 jobs · 4GYV-Q · 10 min–12 min left", text)
        settings = order_alerts.parse_settings({"job_alerts": False, "exclude_characters": ["builder"]})
        self.assertFalse(settings.job_alerts)
        _, alert = jrun(order_alerts.empty_state(), jobs, settings=settings)
        self.assertTrue(alert.empty())

    def test_the_message_uses_full_names_and_the_suggested_price(self):
        _, alert = run(order_alerts.empty_state(), [row(), row(2, status="outbid", side="buy", suggest=None)])
        text = order_alerts.render(alert, SETTINGS)
        self.assertTrue(text.startswith("EVE orders: 2 behind"))
        self.assertIn("▼ Undercut: Widget II", text)
        self.assertIn("ours 100 · best 99 · 5 ahead (0.5 d) · set 98.90", text)
        self.assertIn("▲ Outbid: Widget II", text)
        self.assertIn("Seller · sell · Jita", text)


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
