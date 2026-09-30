"""Measured job times and window-fitting run counts (`eve_skills.job_times`). No network."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from eve_skills import job_times

SOFIA = job_times.zone("Europe/Sofia")
# 2026-09-30 05:45 UTC = 08:45 Sofia (EEST, UTC+3).
NOW = datetime(2026, 9, 30, 5, 45, tzinfo=timezone.utc)


class WindowTests(unittest.TestCase):
    def test_windows_parse_hours_and_minutes(self):
        self.assertEqual([(480, 600), (1200, 1320)], job_times.parse_windows("08-10,20-22"))
        self.assertEqual([(510, 600)], job_times.parse_windows("08:30-10:00"))
        for bad in ("10-08", "8", "25-26", ""):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                job_times.parse_windows(bad)

    def test_hh_mm_start_is_the_next_local_time(self):
        self.assertEqual(datetime(2026, 9, 30, 17, 0, tzinfo=timezone.utc),
                         job_times.parse_start("20:00", SOFIA, NOW))
        # More than an hour in the past means tomorrow.
        self.assertEqual(datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc),
                         job_times.parse_start("07:00", SOFIA, NOW))

    def test_each_window_keeps_its_longest_fit(self):
        # 2.5 h per run from 08:45 Sofia: 5 runs -> 21:15 today, 10 -> 09:45 tomorrow.
        fits = job_times.window_fits(NOW, 2.5, job_times.parse_windows("08-10,20-22"), SOFIA, max_hours=30)
        self.assertEqual([10, 5], [f["runs"] for f in fits])
        self.assertEqual("2026-10-01T06:45:00Z", fits[0]["end"])

    def test_caps_by_hours_and_by_runs(self):
        windows = job_times.parse_windows("08-10,20-22")
        self.assertEqual([5], [f["runs"] for f in job_times.window_fits(NOW, 2.5, windows, SOFIA, max_hours=20)])
        self.assertEqual([5], [f["runs"] for f in
                               job_times.window_fits(NOW, 2.5, windows, SOFIA, max_hours=30, max_runs=8)])

    def test_winter_time_moves_the_utc_window(self):
        # After the last Sunday of October Sofia is UTC+2: 08:00 local is 06:00 UTC.
        winter = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)
        fits = job_times.window_fits(winter, 1.0, [(480, 600)], SOFIA, max_hours=5)
        self.assertEqual([{"runs": 2, "end": "2026-11-02T07:30:00Z"}], fits)

    def test_unknown_zone_is_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            job_times.zone("Mars/Olympus")


class MedianTests(unittest.TestCase):
    def row(self, hours, status="delivered", installer="Ada", start=NOW - timedelta(days=1)):
        return {"activity": "manufacturing", "product": "Tritanium", "installer": installer,
                "status": status, "hours_per_run": hours, "start": start}

    def test_median_per_installer_over_timed_jobs_only(self):
        rows = [self.row(2.0), self.row(3.0), self.row(10.0), self.row(2.5, installer="Mira"),
                self.row(99.0, status="cancelled"), self.row(99.0, status="paused"),
                self.row(99.0, start=NOW - timedelta(days=30))]
        out = job_times.median_times(rows, NOW - timedelta(days=14))
        self.assertEqual([("Mira", 2.5, 1), ("Ada", 3.0, 3)],
                         [(r["installer"], r["hours_per_run"], r["jobs"]) for r in out])


if __name__ == "__main__":
    unittest.main()
