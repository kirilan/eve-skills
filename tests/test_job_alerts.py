"""Window summaries and batching of job news (`job_alerts`), on top of the per-job alerts."""
from __future__ import annotations

import dataclasses
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from eve_skills import cmd_alerts, job_alerts, order_alerts

UTC = timezone.utc
SETTINGS = order_alerts.parse_settings({"quiet_hours": "00:00-07:00", "timezone": "UTC"})
WINDOWS = "08-10,20-22"
SITES = {"alice builder": "home", "bob inventor": "home", "carol hauler": "away"}


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2030, 1, 2, hh, mm, tzinfo=UTC)


def job(job_id: int, end: datetime, installer: str = "Alice Builder", status: str = "active", **over) -> dict:
    row = {"job_id": job_id, "owner_key": f"char:{installer}", "installer": installer, "activity": "manufacturing",
           "product_id": 100 + job_id, "product": f"Hobgoblin II", "runs": 10, "end": end.isoformat(),
           "status": status}
    row.update(over)
    return row


def slots(name: str, mfg_free: int = 0, sci_free: int = 0) -> dict:
    return {"character_name": name, "slots": {"manufacturing": {"free": mfg_free, "used": 1, "max": 1 + mfg_free},
                                              "science": {"free": sci_free, "used": 0, "max": sci_free}}}


def evaluate(state, jobs, now, *, slot_rows=None, announced=(), settings=SETTINGS, windows=WINDOWS, sites=SITES):
    return job_alerts.evaluate(state, jobs, slot_rows, settings, now, announced=set(announced),
                               planning_windows=windows, sites=sites)


class WindowSummaryTests(unittest.TestCase):
    def test_sent_once_per_window(self):
        jobs = [job(1, at(7, 30)), job(2, at(9, 15)), job(3, at(13))]
        state, news = evaluate(job_alerts.empty_state(), jobs, at(8, 5), slot_rows=[slots("Alice Builder", 2)])
        self.assertIsNotNone(news.summary)
        self.assertEqual(news.summary["window"], "08:00-10:00")
        self.assertEqual([j["job_id"] for j in news.summary["ready"]], [1])
        self.assertEqual([j["job_id"] for j in news.summary["ending"]], [2])     # 13:00 is after the window
        self.assertEqual(news.summary["free"], [{"character": "Alice Builder", "manufacturing": 2}])
        self.assertEqual(news.hold, set())
        # the next run in the same window: no second summary
        _, again = evaluate(state, jobs, at(8, 25), slot_rows=[slots("Alice Builder", 2)])
        self.assertIsNone(again.summary)
        # the evening window is a new one
        _, evening = evaluate(state, jobs, at(20, 5), slot_rows=[slots("Alice Builder", 2)])
        self.assertEqual(evening.summary["window"], "20:00-22:00")

    def test_nothing_ready_and_no_free_slot_sends_no_summary(self):
        state, news = evaluate(job_alerts.empty_state(), [job(1, at(15))], at(8, 5), slot_rows=[slots("Alice Builder")])
        self.assertIsNone(news.summary)
        self.assertEqual(state["last_window"], "2030-01-02 08:00-10:00")      # the window's first run was this one

    def test_alert_settings_windows_win_and_none_means_no_summaries(self):
        settings = dataclasses.replace(SETTINGS, jobs_windows="11-12")
        _, news = evaluate(job_alerts.empty_state(), [job(1, at(7))], at(11, 10), settings=settings)
        self.assertEqual(news.summary["window"], "11:00-12:00")
        _, none = evaluate(job_alerts.empty_state(), [job(1, at(7))], at(8, 10), windows=None, sites={})
        self.assertIsNone(none.summary)

    def test_summary_due_only_for_a_new_window(self):
        self.assertTrue(job_alerts.summary_due(job_alerts.empty_state(), SETTINGS, WINDOWS, at(8, 1)))
        self.assertFalse(job_alerts.summary_due({"last_window": "2030-01-02 08:00-10:00"}, SETTINGS, WINDOWS, at(9)))
        self.assertFalse(job_alerts.summary_due(job_alerts.empty_state(), SETTINGS, WINDOWS, at(12)))
        self.assertFalse(job_alerts.summary_due(job_alerts.empty_state(), SETTINGS, WINDOWS, at(6)))   # quiet

    def test_render_counts_jobs_per_character_and_facility(self):
        here = {"location": "X-EXMPL - Example Factory"}
        summary = {"window": "08:00-10:00", "slots_read": True,
                   "ready": [job(1, at(7), **here), job(2, at(7, 10), **here)],
                   "free": [{"character": "Bob Inventor", "science": 3}],
                   "ending": [job(3, at(9), **here), job(4, at(9, 20), **here),
                              job(5, at(9, 40), installer="Bob Inventor", activity="invention",
                                  product="Hobgoblin II Blueprint", location="Z-EXMPL - Example Labs")]}
        text = job_alerts.render_summary(summary, SETTINGS, at(8))
        self.assertEqual("EVE jobs: 08:00-10:00 window, 2 ready, 3 free slots\n\n"
                         "Ready to deliver:\n⚙ X-EXMPL\n   Alice Builder: 2 manufacturing\n\n"
                         "Free slots:\n   Bob Inventor: 3 science\n\n"
                         "Ending in the window:\n⚙ X-EXMPL\n   Alice Builder: 2 manufacturing · 09:00-09:20\n"
                         "⚙ Z-EXMPL\n   Bob Inventor: 1 invention · 09:40", text)
        self.assertNotIn("Hobgoblin", text)                         # the products are not listed


class BatchTests(unittest.TestCase):
    def test_waits_while_another_job_of_the_site_ends_soon_and_fires_at_max_wait(self):
        first = job(1, at(12, 10))
        others = [job(n, at(12, 10) + timedelta(minutes=30 * (n - 1))) for n in range(2, 6)]  # every 30 min
        state, news = evaluate(job_alerts.empty_state(), [first, *others], at(12))
        self.assertEqual(news.hold, {1})                    # job 2 ends 12:40, within 45 min
        # an hour later jobs 2 and 3 are due too and job 4 ends 13:40: still held, the oldest waited 60 min
        state, news = evaluate(state, [first, *others], at(13))
        self.assertEqual(news.hold, {1, 2, 3})
        # at 90 minutes the oldest has waited long enough: everything of the site goes out
        state, news = evaluate(state, [first, *others], at(13, 30))
        self.assertEqual(news.hold, set())
        self.assertEqual(state["held"], {})

    def test_released_when_nothing_else_of_the_site_ends_within_the_batch(self):
        jobs = [job(1, at(12, 10)), job(2, at(14), installer="Carol Hauler")]   # another site
        _, news = evaluate(job_alerts.empty_state(), jobs, at(12))
        self.assertEqual(news.hold, set())

    def test_announced_jobs_are_not_held_or_repeated(self):
        jobs = [job(1, at(12, 10)), job(2, at(12, 30))]
        _, news = evaluate(job_alerts.empty_state(), jobs, at(12, 15), announced={1})
        self.assertEqual(news.hold, set())                  # job 2 due, nothing else of the site pending
        _, news = evaluate(job_alerts.empty_state(), jobs + [job(3, at(12, 50))], at(12, 15), announced={1})
        self.assertEqual(news.hold, {2})                    # job 1 was announced already: never held again

    def test_excluded_character_is_ignored(self):
        settings = dataclasses.replace(SETTINGS, exclude_characters=("Bob Inventor",))
        jobs = [job(1, at(12, 10)), job(2, at(12, 30), installer="Bob Inventor")]
        _, news = evaluate(job_alerts.empty_state(), jobs, at(12), settings=settings)
        self.assertEqual(news.hold, set())                  # Bob's job neither holds nor is held
        _, summary = evaluate(job_alerts.empty_state(), [job(3, at(7), installer="Bob Inventor")], at(8, 5),
                              slot_rows=[slots("Bob Inventor", 4)], settings=settings)
        self.assertIsNone(summary.summary)

    def test_without_an_operation_file_everyone_is_one_group(self):
        jobs = [job(1, at(12, 10)), job(2, at(12, 30), installer="Dave Elsewhere")]
        _, news = evaluate(job_alerts.empty_state(), jobs, at(12), sites={}, windows=None)
        self.assertEqual(news.hold, {1})


class QuietAndSwitchTests(unittest.TestCase):
    def test_nothing_in_quiet_hours_and_the_morning_summary_covers_the_night(self):
        jobs = [job(1, at(3)), job(2, at(5))]
        state, news = evaluate(job_alerts.empty_state(), jobs, at(5, 30))
        self.assertIsNone(news.summary)
        self.assertEqual(news.hold, {1, 2})
        state, news = evaluate(state, jobs, at(8, 2), slot_rows=[])
        self.assertEqual({j["job_id"] for j in news.summary["ready"]}, {1, 2})
        self.assertEqual(news.hold, set())
        self.assertEqual(state["held"], {})

    def test_enabled_false_and_job_alerts_off_change_nothing(self):
        state = {"held": {"9": 1.0}, "last_window": None}
        for settings in (dataclasses.replace(SETTINGS, jobs_enabled=False),
                         dataclasses.replace(SETTINGS, job_alerts=False)):
            new, news = evaluate(state, [job(1, at(7))], at(8, 5), settings=settings)
            self.assertIs(new, state)
            self.assertIsNone(news.summary)
            self.assertEqual(news.hold, set())

    def test_settings_parse_the_jobs_block(self):
        settings = order_alerts.parse_settings({"jobs": {"windows": "07-09", "batch_minutes": 30}})
        self.assertEqual((settings.jobs_windows, settings.batch_minutes, settings.max_wait_minutes), ("07-09", 30, 90))
        self.assertTrue(order_alerts.parse_settings({}).jobs_enabled)
        for bad in ({"jobs": {"window": "07-09"}}, {"jobs": {"windows": "9-7"}}, {"jobs": {"batch_minutes": "x"}}):
            with self.assertRaises(RuntimeError):
                order_alerts.parse_settings(bad)


class RunTests(unittest.TestCase):
    """`alerts run` with every read replaced: what it sends and which states it saves."""

    def run_alerts(self, jobs_read, now, *, dry_run=False):
        client = SimpleNamespace(now=lambda: now)
        settings = dataclasses.replace(SETTINGS, timezone="UTC")
        saved = {}
        sent = []
        with mock.patch.object(order_alerts, "load_settings", return_value=settings), \
                mock.patch.object(cmd_alerts, "_configured", return_value={"token": "t", "chat_id": 1}), \
                mock.patch.object(cmd_alerts.exports, "targets", return_value=(client, [], [])), \
                mock.patch.object(cmd_alerts, "_checked_rows", return_value=([], {}, set(), [])), \
                mock.patch.object(cmd_alerts, "_jobs", return_value=jobs_read), \
                mock.patch.object(cmd_alerts, "_name_jobs"), \
                mock.patch.object(cmd_alerts, "_operation_view", return_value=(WINDOWS, SITES)), \
                mock.patch.object(cmd_alerts, "_slot_rows", return_value=[slots("Alice Builder", 1)]), \
                mock.patch.object(order_alerts, "load_state", return_value=order_alerts.empty_state()), \
                mock.patch.object(order_alerts, "save_state", side_effect=lambda s: saved.update(orders=s)), \
                mock.patch.object(job_alerts, "load_state", return_value={"held": {"5": 1.0}, "last_window": None}), \
                mock.patch.object(job_alerts, "save_state", side_effect=lambda s: saved.update(jobs=s)), \
                mock.patch.object(cmd_alerts.telegram, "send", side_effect=lambda *a, **k: sent.append(a[2])):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                cmd_alerts.cmd_run(SimpleNamespace(dry_run=dry_run, quiet=True))
        return saved, sent, out.getvalue()

    def test_one_message_with_the_summary_and_jobs_announced(self):
        jobs = [job(1, at(7, 50)), job(2, at(8, 15))]
        saved, sent, _ = self.run_alerts((jobs, {"char:Alice Builder"}, []), at(8, 5))
        self.assertEqual(len(sent), 1)
        self.assertIn("EVE jobs: 08:00-10:00 window", sent[0])
        self.assertNotIn("jobs ending", sent[0])                    # the summary lists them instead
        self.assertEqual(set(saved["orders"]["jobs"]), {"1", "2"})  # announced: no per-job repeat later
        self.assertEqual(saved["jobs"]["last_window"], "2030-01-02 08:00-10:00")

    def test_a_failed_jobs_read_keeps_the_job_state(self):
        jobs = [job(1, at(7, 50))]
        saved, sent, _ = self.run_alerts((jobs, {"char:Alice Builder"}, ["Bob Inventor jobs: 403"]), at(8, 5))
        self.assertNotIn("jobs", saved)                             # job state untouched
        self.assertTrue(sent and "EVE jobs: 08:00-10:00 window" not in sent[0])   # per-job alert as before

    def test_dry_run_saves_nothing(self):
        saved, sent, out = self.run_alerts(([job(1, at(7, 50))], {"char:Alice Builder"}, []), at(8, 5), dry_run=True)
        self.assertEqual((saved, sent), ({}, []))
        self.assertIn("08:00-10:00 window", out)


if __name__ == "__main__":
    unittest.main()
