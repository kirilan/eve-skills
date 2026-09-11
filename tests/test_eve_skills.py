"""Unit tests: clone-state tiers, queue shapes (incl. CCP's dateless entries),
snapshots baselines, CSV contract. Pure logic - no network, no data files."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock
from eve_skills import classify, cli, esi, exports, paths, planner, snapshots, sso
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)

# Synthetic skill ids with synthetic caps: tests pin the logic, not the SDE.
CAP3 = 1003          # alpha cap 3
CAP5 = 1005          # alpha cap 5
OMEGA_ONLY = 2000    # absent from caps (catalog-known omega-only)
UNKNOWN_ID = 9999    # absent from caps AND not catalog-known

CAPS = {CAP3: 3, CAP5: 5}
NAMES = {CAP3: "Capped Skill", CAP5: "Wide Skill", OMEGA_ONLY: "Omega Only Skill"}


def skill(sid, trained, active=None, sp=1000):
    return {"skill_id": sid, "trained_skill_level": trained,
            "active_skill_level": trained if active is None else active,
            "skillpoints_in_skill": sp}


def queue_item(sid, level, start=None, finish=None, position=0):
    item = {"skill_id": sid, "finished_level": level, "queue_position": position}
    # ESI OMITS the keys entirely when training cannot be scheduled - mirror that.
    if start is not None:
        item["start_date"] = start.isoformat()
    if finish is not None:
        item["finish_date"] = finish.isoformat()
    return item


class CloneStateTests(unittest.TestCase):
    def state(self, skills, queue=(), completed=None, known=None):
        rows = classify.classify_skills(skills, CAPS, NAMES, completed, known_ids=known)
        return classify.clone_state(list(rows), list(queue), CAPS, NAMES, now=NOW, known_ids=known)

    def test_live_clamp_is_alpha(self):
        st = self.state([skill(CAP3, 5, active=3)])
        self.assertEqual((st.state, st.confidence), ("ALPHA", "high"))
        self.assertIn("trained to 5 but active at 3", st.evidence[0])

    def test_unclamped_beyond_cap_is_omega(self):
        st = self.state([skill(CAP3, 5)])
        self.assertEqual((st.state, st.confidence), ("OMEGA", "high"))

    def test_catalog_known_skill_missing_from_caps_is_omega(self):
        st = self.state([skill(OMEGA_ONLY, 2)], known={OMEGA_ONLY})
        self.assertEqual(st.state, "OMEGA")

    def test_unknown_id_makes_no_omega_claim_and_warns(self):
        st = self.state([skill(UNKNOWN_ID, 4)], known=set())
        self.assertEqual(st.state, "UNKNOWN")
        self.assertTrue(any("update-data" in w for w in st.warnings))

    def test_clamp_plus_omega_is_conflict(self):
        st = self.state([skill(CAP3, 5, active=3), skill(OMEGA_ONLY, 2)], known={OMEGA_ONLY})
        self.assertEqual((st.state, st.confidence), ("CONFLICT", "medium"))

    def test_future_queue_beyond_cap_is_likely_omega(self):
        q = queue_item(CAP3, 4, start=NOW + timedelta(hours=5), finish=NOW + timedelta(days=2))
        st = self.state([skill(CAP5, 2)], queue=[q])
        self.assertEqual((st.state, st.confidence), ("LIKELY_OMEGA", "medium"))

    def test_active_queue_beyond_cap_is_omega(self):
        q = queue_item(CAP3, 4, start=NOW - timedelta(hours=1), finish=NOW + timedelta(days=2))
        st = self.state([skill(CAP5, 2)], queue=[q])
        self.assertEqual((st.state, st.confidence), ("OMEGA", "high"))

    def test_dateless_queue_entry_never_claims_training(self):
        # Regression: CCP returns entries without start/finish dates for items it
        # will not schedule; the old code crashed with KeyError and the classifier
        # must not read such an entry as active training.
        q = queue_item(CAP3, 4)
        st = self.state([skill(CAP5, 2)], queue=[q])
        self.assertNotEqual(st.state, "OMEGA")
        self.assertTrue(any("no training schedule" in w for w in st.warnings))

    def test_pending_completion_explains_clamp(self):
        rows = classify.classify_skills([skill(CAP5, 4, active=3)], CAPS, NAMES,
                                        {CAP5: 5}, known_ids=set())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row.pending_completion)
        self.assertFalse(row.restricted)
        self.assertEqual(row.trained, 5)

    def test_everything_within_limits_is_unknown(self):
        st = self.state([skill(CAP3, 2)])
        self.assertEqual((st.state, st.confidence), ("UNKNOWN", "low"))


class QueueStatusTests(unittest.TestCase):
    def test_all_four_states(self):
        done = queue_item(CAP3, 4, finish=NOW - timedelta(minutes=1), start=NOW - timedelta(days=1))
        training = queue_item(CAP3, 4, start=NOW - timedelta(hours=1), finish=NOW + timedelta(hours=1))
        queued = queue_item(CAP3, 4, start=NOW + timedelta(hours=1), finish=NOW + timedelta(hours=3))
        blocked = queue_item(CAP3, 4)
        self.assertEqual(cli.queue_status(done, NOW), "done")
        self.assertEqual(cli.queue_status(training, NOW), "training")
        self.assertEqual(cli.queue_status(queued, NOW), "queued")
        self.assertEqual(cli.queue_status(blocked, NOW), "blocked")


class LevelSpCellTests(unittest.TestCase):
    """The `level sp` column is an SP question and must be answered from the SP fields.

    Small Hybrid Turret is the real case that exposed this: rank 1, so L4 sits at 45,255 SP and L5
    at 256,000. A queue reorder two hours ago restamped `start_date` while the level was already
    nearly trained, so the fraction of the span that has elapsed says 40% where the SP says 97%.
    """

    def item(self, **over):
        base = {"skill_id": CAP3, "finished_level": 5,
                "start_date": (NOW - timedelta(hours=2)).isoformat(),
                "finish_date": (NOW + timedelta(hours=3)).isoformat(),
                "level_start_sp": 45_255, "training_start_sp": 245_200, "level_end_sp": 256_000}
        return {**base, **over}

    def test_progress_comes_from_sp_not_from_the_restamped_span(self):
        # 245,200 + (256,000 - 245,200) * 2/5 = 249,520 SP held in the skill, which is 204,265 into
        # the 210,745 this level costs: 96.9%. The elapsed span alone would have said 40%.
        cell = cli.level_sp_cell(self.item(), NOW, "training")
        self.assertEqual("204.3K/210.7K 97%", cell)

    def test_the_sp_pair_shares_the_percentage_baseline(self):
        # Both figures are level-relative, so the ratio and the percentage cannot disagree. The
        # cumulative pair ESI publishes would print 832/1.4K here - 59% - beside "50%".
        item = self.item(finished_level=2, level_start_sp=250, training_start_sp=250,
                         level_end_sp=1_414, start_date=(NOW - timedelta(hours=4)).isoformat(),
                         finish_date=(NOW + timedelta(hours=4)).isoformat())
        self.assertEqual("582/1.2K 50%", cli.level_sp_cell(item, NOW, "training"))

    def test_a_nearly_finished_level_is_not_reported_as_barely_started(self):
        # The reported bug: an hour after a reorder, 94.9% read as 11%.
        item = self.item(start_date=(NOW - timedelta(hours=1)).isoformat(),
                         finish_date=(NOW + timedelta(hours=8)).isoformat(),
                         training_start_sp=244_000)
        self.assertIn("95%", cli.level_sp_cell(item, NOW, "training"))

    def test_finished_and_unstarted_items(self):
        self.assertEqual("210.7K/210.7K 100%", cli.level_sp_cell(self.item(), NOW, "done"))
        self.assertEqual("-", cli.level_sp_cell(self.item(), NOW, "queued"))
        self.assertEqual("-", cli.level_sp_cell(self.item(), NOW, "blocked"))

    def test_missing_sp_fields_never_invent_a_figure(self):
        bare = {k: v for k, v in self.item().items() if not k.endswith("_sp")}
        self.assertEqual("-", cli.level_sp_cell(bare, NOW, "training"))
        # A finished item is still known to be finished without any SP figure to show.
        self.assertEqual("100%", cli.level_sp_cell(bare, NOW, "done"))
        self.assertEqual("-", cli.level_sp_cell(self.item(training_start_sp=None), NOW, "training"))

    def test_a_clock_past_the_finish_stamp_is_a_full_level_not_more(self):
        item = self.item(finish_date=(NOW - timedelta(minutes=1)).isoformat())
        self.assertEqual("210.7K/210.7K 100%", cli.level_sp_cell(item, NOW, "training"))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(paths, "config_dir", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rows(self, rows):
        with open(os.path.join(self.tmp.name, "sp-history.jsonl"), "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_record_then_latest(self):
        snapshots.record(42, 12345)
        latest = snapshots.latest(42)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["total_sp"], 12345)
        self.assertIsNone(snapshots.latest(7))

    def test_baseline_is_newest_at_or_before_cutoff(self):
        now = datetime.now(timezone.utc).timestamp()
        self.write_rows([
            {"ts": now - 10 * 86400, "char_id": 1, "total_sp": 100},
            {"ts": now - 9 * 86400, "char_id": 1, "total_sp": 200},
            {"ts": now - 5 * 86400, "char_id": 1, "total_sp": 900},  # inside window: not a baseline
        ])
        base = snapshots.latest_before(1, now - 7 * 86400)
        self.assertEqual(base["total_sp"], 200)

    def test_corrupt_lines_are_skipped(self):
        path = os.path.join(self.tmp.name, "sp-history.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write('{"ts": 1}\n')  # missing keys
            fh.write(json.dumps({"ts": 5.0, "char_id": 3, "total_sp": 77}) + "\n")
        rows = snapshots.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_sp"], 77)


class CsvContractTests(unittest.TestCase):
    def test_header_and_quoting_round_trip(self):
        row = classify.SkillRow(skill_id=7, name="Skill, With Comma", trained=3, active=2,
                                sp=1234, cap=3, omega_now=False, restricted=True,
                                pending_completion=False, unknown_data=False)
        ctx = {"token": {"character_id": 55}, "public": {"name": "Test, Character"}, "rows": [row]}
        parsed = list(csv.reader(io.StringIO(cli.render_csv([ctx]))))
        self.assertEqual(parsed[0][0], "character_id")
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1][:2], ["55", "Test, Character"])
        self.assertEqual(parsed[1][3], "Skill, With Comma")
        self.assertEqual(parsed[1][8], "alpha")  # within cap despite the clamp

    def test_access_column_marks_omega_only(self):
        row = classify.SkillRow(skill_id=7, name="Omega Only", trained=2, active=2,
                                sp=500, cap=None, omega_now=True, restricted=False,
                                pending_completion=False, unknown_data=False)
        ctx = {"token": {"character_id": 1}, "public": {"name": "X"}, "rows": [row]}
        parsed = list(csv.reader(io.StringIO(cli.render_csv([ctx]))))
        self.assertEqual(parsed[1][8], "omega")


class PlannerTests(unittest.TestCase):
    def test_level_cost_is_rank_based(self):
        # The canonical ladder (rank 1): what a level is worth depends on rank alone, so
        # these numbers must never move with attributes or clone state.
        self.assertEqual([planner.cumulative_sp(level, 1) for level in range(6)],
                         [0, 250, 1_414, 8_000, 45_255, 256_000])
        self.assertEqual(planner.cumulative_sp(5, 3), 768_000)          # rank scales SP linearly
        self.assertEqual(planner.level_sp(5, 1), 256_000 - 45_255)      # one level only
        self.assertEqual(planner.levels_sp(3, 5, 1), 256_000 - 8_000)   # from L3: train L4+L5
        with self.assertRaises(ValueError):
            planner.cumulative_sp(6, 1)
        with self.assertRaises(ValueError):
            planner.level_sp(1, 0)      # no training multiplier -> nothing to price against

    def test_injector_value_tiers(self):
        self.assertEqual(planner.injector_value(4_999_999), 500_000)
        self.assertEqual(planner.injector_value(5_000_000), 400_000)
        self.assertEqual(planner.injector_value(79_999_999), 300_000)
        self.assertEqual(planner.injector_value(80_000_000), 150_000)

    def test_extraction_plan_gates_and_count(self):
        p = planner.extraction_plan(5_499_999, 6_000_000, None)
        self.assertEqual(p["count"], 0)
        self.assertIn("5,500,000", p["reason"])
        p = planner.extraction_plan(6_249_999, 7_000_000, None)
        self.assertEqual(p["count"], 2)  # floor((6.25M - 5M) / 500k)
        p = planner.extraction_plan(6_249_999, 7_000_000, 2500.0)
        self.assertAlmostEqual(p["retrain_days_each"], 500000 / 2500 / 24, places=3)

    def test_rules_staleness_warning(self):
        verified = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.assertIsNone(planner.extraction_rules_warning(verified))
        self.assertIsNotNone(planner.extraction_rules_warning(verified + timedelta(days=181)))

    def test_calibrated_rate_from_training_item(self):
        ctx = {
            "now": NOW,
            "token": {"character_id": 1},
            "queue": [{"skill_id": CAP3, "start_date": (NOW - timedelta(hours=2)).isoformat(),
                       "finish_date": (NOW + timedelta(hours=2)).isoformat(),
                       "level_start_sp": 100_000, "training_start_sp": 100_000,
                       "level_end_sp": 110_000}],
        }
        rate, source = planner.calibrated_rate(ctx)
        self.assertEqual(rate, 2500.0)
        self.assertEqual(source, "live training item")

    def test_calibrated_rate_ignores_sp_trained_before_the_queue_was_rearranged(self):
        # EVE restamps start_date on the active item whenever the queue is reordered, so the span
        # covers only the time since that edit. Half this level was already trained by then:
        # 5,000 SP over the four hours the stamps describe, not the level's whole 10,000.
        ctx = {
            "now": NOW,
            "token": {"character_id": 1},
            "queue": [{"skill_id": CAP3, "start_date": (NOW - timedelta(hours=2)).isoformat(),
                       "finish_date": (NOW + timedelta(hours=2)).isoformat(),
                       "level_start_sp": 100_000, "training_start_sp": 105_000,
                       "level_end_sp": 110_000}],
        }
        self.assertEqual((1250.0, "live training item"), planner.calibrated_rate(ctx))

    def test_calibrated_rate_skips_an_item_that_cannot_be_measured(self):
        # No training_start_sp means the span has no matching SP figure; reading level_start_sp as 0
        # would claim the whole level was trained inside it. Fall through to the history instead.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(paths, "config_dir", return_value=tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = {
            "now": NOW,
            "token": {"character_id": 1},
            "queue": [{"skill_id": CAP3, "start_date": (NOW - timedelta(hours=2)).isoformat(),
                       "finish_date": (NOW + timedelta(hours=2)).isoformat(),
                       "level_start_sp": 100_000, "level_end_sp": 110_000}],
        }
        with self.assertRaises(RuntimeError):
            planner.calibrated_rate(ctx)

    def test_calibrated_rate_needs_data(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(paths, "config_dir", return_value=tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(RuntimeError):
            planner.calibrated_rate({"now": NOW, "token": {"character_id": 1}, "queue": []})


class EsiCacheTests(unittest.TestCase):
    def setUp(self):
        from eve_skills import esi
        self.esi = esi
        self.client = esi.Esi("unittest")
        self.calls = []

    class _Resp:
        def __init__(self, body=b'{"a": 1}', status=200, headers=None):
            self._body, self.status = body, status
            self.headers = headers or {}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _patch(self, responses):
        from urllib.error import HTTPError

        def fake_urlopen(req, timeout=None):
            self.calls.append(req)
            resp = responses[len(self.calls) - 1]
            if isinstance(resp, HTTPError):
                raise resp
            return resp

        return mock.patch.object(self.esi.urllib.request, "urlopen", fake_urlopen)

    def test_second_get_is_cached_then_revalidated_with_etag(self):
        from email.message import Message
        from urllib.error import HTTPError
        h1 = Message()
        h1["Expires"] = "Tue, 30 Jun 2099 12:00:00 GMT"
        h1["ETag"] = '"abc"'
        h304_headers = Message()
        h304_headers["Expires"] = "Tue, 30 Jun 2099 12:00:00 GMT"  # ESI always sends this on 304
        h304 = HTTPError("https://esi/", 304, "Not Modified", h304_headers, None)
        h304._closer.close_called = True  # synthetic error: silence GC's implicit-cleanup warning
        with self._patch([self._Resp(headers=h1), h304, self._Resp(b'{"a": 2}')]):
            self.assertEqual(self.client.get("/x"), {"a": 1})   # fresh: network once
            self.assertEqual(self.client.get("/x"), {"a": 1})   # cache hit: no request
            self.assertEqual(len(self.calls), 1)
            key = next(iter(self.client._cache))
            self.client._cache[key]["expires"] = 0.0            # force expiry
            self.assertEqual(self.client.get("/x"), {"a": 1})   # 304 keeps cached value
            self.assertEqual(len(self.calls), 2)
            hdrs = self.calls[1]
            items = hdrs.header_items() if hasattr(hdrs, "header_items") else hdrs.headers.items()
            self.assertEqual({k.lower(): v for k, v in items}.get("if-none-match"), '"abc"')
            key = next(iter(self.client._cache))
            self.assertGreater(self.client._cache[key]["expires"], 0.0)  # expiry refreshed


class ManualLoginTests(unittest.TestCase):
    def test_cli_accepts_pasted_callback_without_starting_listener(self):
        token = {"access_token": "jwt", "refresh_token": "refresh", "expires_in": 1200}
        callback = "http://localhost:8635/callback?code=authorization-code&state=expected-state"
        claims = {"sub": "CHARACTER:EVE:9001", "name": "Remote Pilot", "scp": sso.SCOPES}
        with (
            mock.patch.object(sso, "load_config", return_value={}),
            mock.patch.object(sso, "_discover", return_value={
                "authorization_endpoint": "https://login.example/authorize",
                "token_endpoint": "https://login.example/token",
            }),
            mock.patch.object(sso.secrets, "token_bytes", return_value=b"x" * 32),
            mock.patch.object(sso.secrets, "token_urlsafe", return_value="expected-state"),
            mock.patch.object(sso.webbrowser, "open") as browser_open,
            mock.patch("builtins.input", return_value=callback),
            mock.patch.object(sso.http.server, "HTTPServer") as listener,
            mock.patch.object(sso, "_post_form", return_value=token) as post,
            mock.patch.object(sso, "decode_jwt", return_value=claims),
            mock.patch.object(sso, "_put_record"),
            mock.patch.object(sso, "save_config"),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            self.assertEqual(cli.main(["login", "--client-id", "client", "--manual", "--scopes", "all"]), 0)

        listener.assert_not_called()
        fields = post.call_args.args[1]
        self.assertEqual(fields["code"], "authorization-code")
        self.assertEqual(fields["redirect_uri"], "http://localhost:8635/callback")
        auth_query = sso.urllib.parse.parse_qs(sso.urllib.parse.urlparse(browser_open.call_args.args[0]).query)
        requested_scopes = auth_query["scope"][0].split()
        self.assertIn("esi-skills.read_skills.v1", requested_scopes)
        self.assertNotIn("esi-characters.read_attributes.v1", requested_scopes)
        self.assertEqual(len(requested_scopes), len(set(requested_scopes)))
        self.assertIn("Logged in as Remote Pilot (9001).", stdout.getvalue())


class ScopeRegistryTests(unittest.TestCase):
    def test_feature_and_all_expansion(self):
        self.assertEqual(sso.scopes_for(["standings"]), ["esi-characters.read_standings.v1"])
        self.assertEqual(sso.scopes_for(["attributes"]), ["esi-skills.read_skills.v1"])
        all_scopes = sso.scopes_for(["all"])
        self.assertIn("esi-industry.read_corporation_jobs.v1", all_scopes)
        self.assertIn("esi-clones.read_implants.v1", all_scopes)
        self.assertNotIn("esi-characters.read_attributes.v1", all_scopes)
        self.assertEqual(all_scopes, sorted(set(all_scopes)))  # deduped across overlapping features
        self.assertEqual(sso.scopes_for(["jobs", "all"]), all_scopes)
        self.assertEqual(sso.scopes_for(None), [])

    def test_unknown_feature_is_hard_error(self):
        with self.assertRaises(RuntimeError):
            sso.scopes_for(["everything"])

    def test_has_feature_requires_every_scope(self):
        partial = {"scopes": ["esi-industry.read_character_jobs.v1"]}
        full = {"scopes": sso.SCOPES + sso.scopes_for(["jobs"])}
        self.assertFalse(sso.has_feature(partial, "jobs"))
        self.assertTrue(sso.has_feature(full, "jobs"))


class RateGuardTests(unittest.TestCase):
    def _measure(self, *samples):
        """`snapshot_rate` over rows given as (hours_ago, total_sp), newest last."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "config_dir", return_value=tmp):
            base = NOW.timestamp()
            with open(os.path.join(tmp, "sp-history.jsonl"), "w", encoding="utf-8") as fh:
                for hours_ago, sp in samples:
                    fh.write(json.dumps({"ts": base - hours_ago * 3600, "char_id": 1, "total_sp": sp}) + "\n")
            ctx = {"now": NOW, "token": {"character_id": 1}, "queue": []}
            return planner.snapshot_rate(ctx)

    def _rate(self, first_sp, last_sp):
        measured = self._measure((2, first_sp), (1, last_sp))
        return None if measured is None else measured[0]

    def test_flat_history_is_none_not_zero(self):
        self.assertIsNone(self._rate(50_000_000, 50_000_000))  # ZeroDivisionError regression

    def test_extraction_dip_is_none_too(self):
        self.assertIsNone(self._rate(52_000_000, 50_000_000))

    def test_real_gain_still_measures(self):
        self.assertAlmostEqual(self._rate(50_000_000, 52_162_000), 2_162_000.0)  # 2.162M SP over the 1h gap

    def test_an_extractor_inside_the_window_does_not_net_off_the_training(self):
        # 50.0M six days ago, 49.5M three days ago (a 500k extractor ran), 50.2M now. End to end
        # that nets to 200k over 144h - 1,389 SP/hour - while the character really trained 700k in
        # the 72h since the withdrawal. Only the monotonic tail is a rate.
        rate, hours = self._measure((144, 50_000_000), (72, 49_500_000), (0, 50_200_000))
        self.assertAlmostEqual(hours, 72.0)
        self.assertAlmostEqual(rate, 700_000 / 72)

    def test_the_source_label_names_the_span_it_measured(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "config_dir", return_value=tmp):
            base = NOW.timestamp()
            with open(os.path.join(tmp, "sp-history.jsonl"), "w", encoding="utf-8") as fh:
                for hours_ago, sp in ((2, 50_000_000), (0, 50_004_000)):
                    fh.write(json.dumps({"ts": base - hours_ago * 3600, "char_id": 1, "total_sp": sp}) + "\n")
            ctx = {"now": NOW, "token": {"character_id": 1}, "queue": []}
            rate, source = planner.calibrated_rate(ctx)
            self.assertAlmostEqual(rate, 2000.0)
            # A two-hour slope must not be advertised as a week of evidence.
            self.assertEqual("local SP history (2.0h)", source)


class UnclassifiedCountTests(unittest.TestCase):
    """A skill the local data cannot classify is not an omega claim.

    `beyond_alpha` is true for any row without an alpha cap, which includes the rows
    `classify` deliberately refused to judge because it could not resolve the skill id. Folding
    those into the omega count made one screen assert an omega-restricted skill and, two lines up,
    warn that no omega claim had been made about it.
    """

    def render(self, rows):
        ctx = {"now": NOW, "token": {"character_id": 1}, "public": {"name": "T", "bloodline_id": 1},
               "grade_name": "g", "caps": {}, "queue": [], "rows": rows, "data_build": 3494416,
               "skills_doc": {"total_sp": 1000, "unallocated_sp": 0},
               "state": SimpleNamespace(state="UNKNOWN", confidence="low", evidence=[], warnings=[]),
               "names": {}}
        args = SimpleNamespace(trained_only=False, filter="all", sort="name", week=False)
        with mock.patch.object(cli.alphadata, "load", return_value={"races": {"races": {}}}):
            return cli.render_text(ctx, args)

    def row(self, sid, cap, unknown=False):
        return classify.SkillRow(skill_id=sid, name=f"skill {sid}", trained=4, active=4, sp=1000,
                                 cap=cap, omega_now=False, restricted=False,
                                 pending_completion=False, unknown_data=unknown)

    def test_unresolvable_skills_get_their_own_bucket(self):
        out = self.render([self.row(1, 4), self.row(9999, None, unknown=True)])
        self.assertIn("alpha-trainable: 1, omega-restricted: 0, unclassified: 1", out)

    def test_a_real_omega_skill_is_still_counted_as_omega(self):
        out = self.render([self.row(1, 4), self.row(2, None)])
        self.assertIn("alpha-trainable: 1, omega-restricted: 1", out)
        self.assertNotIn("unclassified", out)


class PlanWithoutItemsTests(unittest.TestCase):
    def test_a_fully_covered_plan_does_not_need_a_training_rate(self):
        # Nothing to train means nothing to price, so a character with no measurable rate (idle
        # queue, fresh install with no SP history) must still be told the target is covered rather
        # than be refused with "pass --rate" for a figure the answer never uses.
        catalog = cli.load_skill_catalog()
        skill_id, info = next((sid, row) for sid, row in catalog.items() if row.name == "Industry")
        ctx = {"now": NOW, "token": {"character_id": 1}, "public": {"name": "Tester"},
               "rows": [SimpleNamespace(skill_id=skill_id, trained=5, name=info.name)],
               "queue": [], "caps": {skill_id: 5}}
        args = SimpleNamespace(char="Tester", target=["Industry:5"], rate=None)
        with mock.patch.object(cli, "gather", return_value=ctx), \
             mock.patch.object(sso, "list_characters", return_value=[{"character_id": 1}]), \
             mock.patch.object(planner, "calibrated_rate", side_effect=AssertionError("no rate needed")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertIn(cli.cmd_plan(args), (None, 0))
        printed = out.getvalue()
        self.assertIn("0 item(s) to train", printed)
        self.assertIn("already trained to L5", printed)
        self.assertNotIn("rate:", printed)


class HistoryClockTests(unittest.TestCase):
    def test_rows_are_stamped_with_the_clock_the_caller_measures_against(self):
        # ESI server time is what every consumer compares these rows to, so a machine whose clock
        # is hours off must not write rows on its own clock: the two ends of `skills --week` would
        # then come from different clocks and the SP/day would be wrong by the skew.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "config_dir", return_value=tmp):
            esi_now = NOW.timestamp()
            with mock.patch.object(snapshots.time, "time", return_value=esi_now + 6 * 3600):
                snapshots.record(7, 1_000_000, now=esi_now)
            self.assertEqual([esi_now], [row["ts"] for row in snapshots.load()])


class CsvParityTests(unittest.TestCase):
    def test_csv_skips_never_trained_rows_like_the_table(self):
        row = lambda sid, trained, pending=False: SimpleNamespace(
            skill_id=sid, name=f"s{sid}", trained=trained, active=trained, sp=trained * 100,
            cap=5, beyond_alpha=False, restricted=False, pending_completion=pending, unknown_data=False)
        ctx = {"token": {"character_id": 7}, "public": {"name": "T"},
               "rows": [row(1, 0), row(2, 0, pending=True), row(3, 3)]}
        out = list(csv.DictReader(io.StringIO(cli.render_csv([ctx]))))
        self.assertEqual([int(r["skill_id"]) for r in out], [2, 3])  # L0 unstarted rows only live in --json


class NameCacheReuseTests(unittest.TestCase):
    def test_second_run_resolves_only_the_missing_ids(self):
        class Client:
            def __init__(self):
                self.fetched = []

            def post(self, _path, ids):
                self.fetched.append(sorted(ids))
                return [{"id": ident, "name": f"name {ident}"} for ident in ids]

        with tempfile.TemporaryDirectory() as tmp:
            first = Client()
            esi.resolve_names(first, {1}, cache_dir=tmp)
            second = Client()
            self.assertEqual(esi.resolve_names(second, {1, 2}, cache_dir=tmp), {1: "name 1", 2: "name 2"})
            with open(os.path.join(tmp, "names.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"1": "name 1", "2": "name 2"})
        self.assertEqual(first.fetched, [[1]])   # the cached id was never re-resolved


class ExportLogicTests(unittest.TestCase):
    def test_live_standings_shape_is_normalized_and_named(self):
        doc = [
            {"from_id": 2, "from_type": "npc_corp", "standing": -1.25},
            {"from_id": 1, "from_type": "agent", "standing": 3.5},
        ]
        entries = exports._standing_entries(doc, {1: "Agent One", 2: "Corp Two"})
        self.assertEqual(entries, [
            ("agent", 1, "Agent One", 3.5),
            ("npc corp", 2, "Corp Two", -1.25),
        ])

    def test_job_rows_status_runs_and_activity(self):
        jobs = [
            {"activity": 1, "status": "active", "output_type_id": 34, "installed_in": 60003760,
             "installed_runs": 2, "runs": 10, "finish_date": "2026-09-05T15:00:00Z"},
            {"activity": 8, "status": "finished", "output_type_id": 36, "installed_in": 60003760,
             "finish_date": "2026-09-01T00:00:00Z"},
            {"activity": 42, "status": "cancelled", "installed_in": 1048236548577},
        ]
        rows, ids = exports._job_rows(jobs, NOW)
        self.assertEqual(ids, {34, 36, 60003760, 1048236548577})
        # rows are chronological: finished Sep 1, then active (Sep 5), then dateless cancelled
        self.assertEqual(rows[0][:2], ["finished", "reaction"])
        self.assertEqual(rows[0][3], "-")
        self.assertEqual(rows[0][4], "Sep 01 00:00")
        self.assertEqual(rows[1][:2], ["active", "manufacturing"])
        self.assertEqual(rows[1][3], "2/10")
        self.assertTrue(rows[1][4].endswith("left"))
        self.assertIn("activity 42", rows[2][1])  # unknown CCP activity codes stay visible

    def test_corp_id_flat_and_nested(self):
        self.assertEqual(exports.corp_of({"corporation_id": 980}), 980)
        self.assertEqual(exports.corp_of({"corporation": {"id": 981}}), 981)
        self.assertIsNone(exports.corp_of({"corporation_id": 0}))

    def test_hint_names_the_feature_and_character(self):
        text = exports.hint("Ada Vane", "standings")
        self.assertIn("login --scopes standings", text)
        self.assertIn("'Ada Vane'", text)


if __name__ == "__main__":
    unittest.main()
