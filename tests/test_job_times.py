"""Measured job times and window-fitting run counts (`eve_skills.job_times`). No network."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from eve_skills import job_times

# Helsinki in summer (EEST) as a fixed offset: the same answers as Europe/Helsinki until the last Sunday of
# October, and available on every platform - Windows has no time zone database without `tzdata`.
HELSINKI = job_times.zone("+03:00")
HAS_ZONES = job_times.zone_database_available()
# 2026-09-30 05:45 UTC = 08:45 Helsinki (EEST, UTC+3).
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
                         job_times.parse_start("20:00", HELSINKI, NOW))
        # More than an hour in the past means tomorrow.
        self.assertEqual(datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc),
                         job_times.parse_start("07:00", HELSINKI, NOW))

    def test_each_window_keeps_its_longest_fit(self):
        # 2.5 h per run from 08:45 Helsinki: 5 runs -> 21:15 today, 10 -> 09:45 tomorrow.
        fits = job_times.window_fits(NOW, 2.5, job_times.parse_windows("08-10,20-22"), HELSINKI, max_hours=30)
        self.assertEqual([10, 5], [f["runs"] for f in fits])
        self.assertEqual("2026-10-01T06:45:00Z", fits[0]["end"])

    def test_caps_by_hours_and_by_runs(self):
        windows = job_times.parse_windows("08-10,20-22")
        self.assertEqual([5], [f["runs"] for f in job_times.window_fits(NOW, 2.5, windows, HELSINKI, max_hours=20)])
        self.assertEqual([5], [f["runs"] for f in
                               job_times.window_fits(NOW, 2.5, windows, HELSINKI, max_hours=30, max_runs=8)])

    @unittest.skipUnless(HAS_ZONES, "no time zone database (Windows without tzdata)")
    def test_winter_time_moves_the_utc_window(self):
        # After the last Sunday of October Helsinki is UTC+2: 08:00 local is 06:00 UTC.
        helsinki = job_times.zone("Europe/Helsinki")
        winter = datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc)
        fits = job_times.window_fits(winter, 1.0, [(480, 600)], helsinki, max_hours=5)
        self.assertEqual([{"runs": 2, "end": "2026-11-02T07:30:00Z"}], fits)
        self.assertEqual(NOW.astimezone(helsinki).utcoffset(), NOW.astimezone(HELSINKI).utcoffset())

    def test_fixed_offsets_in_every_spelling(self):
        for spec, hours in (("+03:00", 3), ("UTC+3", 3), ("+0300", 3), ("GMT-5", -5), ("-05:30", -5.5)):
            with self.subTest(spec=spec):
                self.assertEqual(hours * 3600, job_times.zone(spec).utcoffset(None).total_seconds())
        self.assertIs(timezone.utc, job_times.zone("UTC"))
        with self.assertRaises(RuntimeError):
            job_times.zone("+15:00")

    def test_unknown_zone_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as caught:
            job_times.zone("Mars/Olympus")
        self.assertIn("+03:00", str(caught.exception))   # the offset that works everywhere is offered

    def test_missing_database_names_the_two_ways_out(self):
        from unittest import mock
        with mock.patch.object(job_times, "zone_database_available", return_value=False), \
                mock.patch.object(job_times, "ZoneInfo", side_effect=job_times.ZoneInfoNotFoundError("x")):
            with self.assertRaises(RuntimeError) as caught:
                job_times.zone("Europe/Helsinki")
        self.assertIn("tzdata", str(caught.exception))
        self.assertIn("fixed offset", str(caught.exception))


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


class FacilityTests(unittest.TestCase):
    def row(self, hours, facility):
        return {"activity": "manufacturing", "product": "Tritanium", "installer": "Ada", "status": "delivered",
                "hours_per_run": hours, "start": NOW - timedelta(days=1), "facility_id": facility,
                "installed_in": f"place {facility}"}

    def test_each_facility_gets_its_own_median(self):
        rows = [self.row(4.0, 60003760), self.row(4.2, 60003760), self.row(2.0, 1040000000000)]
        out = job_times.median_times(rows, NOW - timedelta(days=14), {1040000000000: "example-raitaru"})
        self.assertEqual([("example-raitaru", 2.0), ("place 60003760", 4.1)],
                         [(r["facility"], r["hours_per_run"]) for r in out])
        only = job_times.median_times(rows, NOW - timedelta(days=14), facility=60003760)
        self.assertEqual([4.1], [r["hours_per_run"] for r in only])


class SiteTimesTests(unittest.TestCase):
    """`jobs --times --site`: the time where the site does the activity, never scaled from a guess."""
    LABS, FACTORY, ELSEWHERE = 1_040_000_000_000, 1_040_000_000_002, 1_040_000_000_009

    def setUp(self):
        from eve_skills import operation
        self.op = operation.parse({
            "sites": {"null": {"owner": "personal", "facility": "labs", "characters": ["Ada", "Mira"],
                               "facilities": {"manufacturing": "factory"}}},
            "facilities": {
                # Labs: manufacturing time 0.70 (stated); the factory 0.406.
                "labs": {"location_id": self.LABS, "tax_pct": 1, "material": {}, "time": {"manufacturing": 0.70}},
                "factory": {"location_id": self.FACTORY, "tax_pct": 1, "material": {},
                            "time": {"manufacturing": 0.406}},
                "nowhere": {"system": "Z-EXMPL", "tax_pct": 1, "material": {}, "time": {}}}})
        self.site = self.op.site("null")

    def times(self, *specs):
        """(installer, facility_id, hours, jobs) -> median_times-shaped rows."""
        return [{"activity": "manufacturing", "product": "Widget II", "installer": who, "facility_id": fid,
                 "facility": f"facility {fid}", "hours_per_run": hours, "min_hours": hours * 0.9,
                 "max_hours": hours * 1.1, "jobs": jobs} for who, fid, hours, jobs in specs]

    def test_measured_at_home_is_used_as_is(self):
        rows, unscaled = job_times.site_times(self.times(("Ada", self.FACTORY, 0.96, 12),
                                                         ("Ada", self.LABS, 2.0, 30)), self.op, self.site)
        self.assertEqual([("factory", 0.96, None)], [(r["facility"], r["hours_per_run"], r["scaled_from"]) for r in rows])
        self.assertEqual([], unscaled)

    def test_measured_only_at_a_known_facility_is_scaled_by_the_multipliers(self):
        rows, unscaled = job_times.site_times(self.times(("Mira", self.LABS, 1.40, 5)), self.op, self.site)
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual(1.40 * 0.406 / 0.70, rows[0]["hours_per_run"])
        self.assertAlmostEqual(1.40 * 0.9 * 0.406 / 0.70, rows[0]["min_hours"])
        self.assertEqual(("factory", self.FACTORY, "labs"),
                         (rows[0]["facility"], rows[0]["facility_id"], rows[0]["scaled_from"]))
        self.assertEqual([], unscaled)

    def test_several_sources_use_the_one_with_most_jobs(self):
        self.op.facilities["other"] = type(self.op.facilities["labs"])(
            name="other", location_id=self.ELSEWHERE, tax=0.01, time={"manufacturing": 1.0})
        rows, _ = job_times.site_times(self.times(("Ada", self.LABS, 1.4, 2), ("Ada", self.ELSEWHERE, 2.0, 9)),
                                       self.op, self.site)
        self.assertEqual("other", rows[0]["scaled_from"])
        self.assertAlmostEqual(2.0 * 0.406, rows[0]["hours_per_run"])

    def test_measured_only_at_an_unknown_facility_is_never_scaled(self):
        rows, unscaled = job_times.site_times(self.times(("Ada", 60003760, 4.0, 7)), self.op, self.site)
        self.assertEqual([], rows)
        self.assertEqual([{"activity": "manufacturing", "product": "Widget II", "installer": "Ada",
                           "facility_id": 60003760, "facility": "facility 60003760", "jobs": 7}], unscaled)

    def test_a_home_facility_without_location_id_is_an_error_naming_it(self):
        from eve_skills import operation
        op = operation.parse({"sites": {"s": {"owner": "personal", "facility": "nowhere", "characters": ["Ada"]}},
                              "facilities": {"nowhere": {"system": "Z-EXMPL"}}})
        with self.assertRaises(RuntimeError) as ctx:
            job_times.site_times(self.times(("Ada", self.LABS, 1.0, 1)), op, op.site("s"))
        self.assertIn("nowhere", str(ctx.exception))

    def test_window_fits_use_the_scaled_hours(self):
        rows, _ = job_times.site_times(self.times(("Mira", self.LABS, 1.40, 5)), self.op, self.site)
        hours = rows[0]["hours_per_run"]                       # 0.812 h per run at the factory
        fits = job_times.window_fits(NOW, hours, job_times.parse_windows("20-22"), HELSINKI, max_hours=30)
        # From 08:45 local, 0.812 h runs: 14 runs end 20:12, 16 end 21:44, 17 would end 22:33.
        self.assertEqual(16, fits[0]["runs"])
