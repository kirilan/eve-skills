"""``eve-skills doctor``: what a user is told, that it never writes, and that nothing secret leaks.

Every case runs in a throwaway $XDG_* tree against a fake transport with a fixed clock, so
no real configuration, token store, cache or network service can be read or touched."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
from datetime import date, timedelta
from email.message import Message
from email.utils import formatdate
from pathlib import Path
from unittest import mock

from eve_skills import alphadata, cli, doctor, sso

ADA, VELA = 91000001, 91000002
NOW = 1_800_000_000.0          # fixed clock: expiry and data age must not drift with reality
CLIENT_ID = "client-id-AAAAAAAA"
CLIENT_SECRET = "client-secret-BBBBBBBB"
ACCESS_ADA = "access-token-CCCCCCCC"
REFRESH_ADA = "refresh-token-DDDDDDDD"
JWTISH = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJDaGFyYWN0ZXI6OTAwMDAxIn0.c2lnbmF0dXJlLXZhbHVl"

SSO_URL = sso.WELL_KNOWN
ESI_STATUS = doctor.esi_mod.BASE + doctor.ESI_STATUS_PATH
ESI_COMPAT = doctor.esi_mod.BASE + doctor.ESI_COMPAT_PATH
ESI_MARKET = doctor.esi_mod.BASE + doctor.MARKET_PROBE_PATH

# The order-book probe reads headers, not the body, so a healthy answer needs both: one row is
# enough to be "a list of orders", and ESI's own numbers are the ones worth asserting against.
MARKET_ROWS = [{"order_id": 700000001, "type_id": doctor.MARKET_PROBE_TYPE_ID, "price": 10.4,
                "volume_remain": 5, "volume_total": 10, "is_buy_order": False}]
MARKET_HEADERS = {"Last-Modified": formatdate(NOW - 60, usegmt=True),
                  "X-Ratelimit-Limit": "12000/15m", "X-Ratelimit-Remaining": "11998"}


def message(headers: dict) -> Message:
    """Response headers as urllib hands them back: a case-insensitive mapping."""
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    return msg


SECRET_VALUES = (CLIENT_ID, CLIENT_SECRET, ACCESS_ADA, REFRESH_ADA, JWTISH)
FORBIDDEN_KEYS = {"access_token", "refresh_token", "client_secret", "id_token", "code", "authorization"}

def strings(node, key=None):
    """Every (containing-key, string) pair anywhere in a report, however deeply nested."""
    if isinstance(node, str):
        yield (key, node)
    elif isinstance(node, dict):
        for child_key, value in node.items():
            yield from strings(value, child_key)
    elif isinstance(node, list):
        for item in node:
            yield from strings(item, key)


def make_record(character_id: int, name: str, *, access: str = ACCESS_ADA, refresh: str | None = REFRESH_ADA,
                expires_in: float = 3600.0, scopes=None, client_id: str | None = CLIENT_ID) -> dict:
    record = {"client_id": client_id, "client_secret": None, "access_token": access, "refresh_token": refresh,
              "expires_at": NOW + expires_in, "scopes": list(sso.SCOPES) if scopes is None else list(scopes),
              "character_id": character_id, "character_name": name}
    return record


class FakeTransport:
    """URL-routed stand-in for urllib.request.urlopen that records what was asked for.

    A route value is the document, an Exception to raise, or a ``(document, headers)`` tuple -
    the tuple form being how a test supplies the cache and rate-limit headers only the order-book
    endpoint sends."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict, object]] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, {k.lower(): v for k, v in request.header_items()}, timeout))
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        payload, raw_headers = result if isinstance(result, tuple) else (result, {})

        class Response:
            status = 200

            def __init__(self):
                # A real response header object is case-insensitive; so is this one.
                self.headers = message(raw_headers)

            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.calls]


def sso_document() -> dict:
    return {"issuer": "https://login.eveonline.com",
            "authorization_endpoint": "https://login.eveonline.com/v2/oauth/authorize",
            "token_endpoint": "https://login.eveonline.com/v2/oauth/token"}


def dns_failure() -> urllib.error.URLError:
    return urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))


def http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    err = urllib.error.HTTPError(ESI_STATUS, code, "Service Unavailable", message(headers or {}), None)
    err.close()  # the wrapper owns a file handle; leaving it to the GC prints a ResourceWarning
    return err


class DoctorTestCase(unittest.TestCase):
    """Isolated XDG tree + fixed clock; nothing on disk exists unless a test puts it there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-doctor-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")   # fake, and below the throwaway tree
        patcher = mock.patch.dict(os.environ, {
            "HOME": self.home,       # never a real home: no assertion can depend on who runs this
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config"),
            "XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
            "XDG_DATA_HOME": os.path.join(self.tmp.name, "data"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state"),
            "EVE_SKILLS_CLIENT_ID": "",   # a real env override must not leak in
            "EVE_SKILLS_SSO_PORT": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = os.path.join(self.config_home(), "eve-skills")
        self.cache_dir = os.path.join(self.cache_home(), "eve-skills")
        self.data_dir = os.path.join(self.data_home(), "eve-skills")
        self.state_dir = os.path.join(self.state_home(), "eve-skills")

    def without_bundled_data(self) -> None:
        """Hide the SDE documents shipped inside the package, so "not installed" is provable
        whatever the wheel happens to bundle."""
        empty = os.path.join(self.tmp.name, "empty-package-data")
        os.makedirs(empty, exist_ok=True)
        patcher = mock.patch.object(alphadata, "PACKAGE_DATA_DIR", Path(empty))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def config_home() -> str:
        return os.environ["XDG_CONFIG_HOME"]

    @staticmethod
    def cache_home() -> str:
        return os.environ["XDG_CACHE_HOME"]

    @staticmethod
    def data_home() -> str:
        return os.environ["XDG_DATA_HOME"]

    @staticmethod
    def state_home() -> str:
        return os.environ["XDG_STATE_HOME"]

    # -- fixtures ------------------------------------------------------------

    def write_json(self, path: str, payload, mode: int = 0o600) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(payload, fh)
        os.chmod(path, mode)
        return path

    def seed_watch_state(self, doc, mode: int = 0o600) -> str:
        """`watch-state.json` as the watchers write it; `doc` may be any text for a damaged file."""
        path = os.path.join(self.state_dir, "watch-state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        os.chmod(path, mode)
        return path

    def seed_events(self, rows, mode: int = 0o600) -> str:
        """`events.jsonl`; a row that is not a dict is written verbatim as a damaged line."""
        path = os.path.join(self.state_dir, "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for row in rows:
                fh.write(row if isinstance(row, str) else json.dumps(row))
                fh.write("\n")
        os.chmod(path, mode)
        return path

    def seed_config(self, *, client_id: str | None = CLIENT_ID, client_secret: str | None = None,
                    user_agent: str | None = "eve-skills test (tester@example.com)", mode: int = 0o600) -> str:
        cfg = {}
        if client_id is not None:
            cfg["client_id"] = client_id
        if client_secret is not None:
            cfg["client_secret"] = client_secret
        if user_agent is not None:
            cfg["user_agent"] = user_agent
        return self.write_json(os.path.join(self.config_dir, "config.json"), cfg, mode)

    def seed_store(self, *records: dict, mode: int = 0o600) -> str:
        return self.write_json(os.path.join(self.config_dir, "tokens.json"),
                               {"characters": {str(r["character_id"]): r for r in records}}, mode)

    def seed_sde(self, *, build: int = 34_944_160, age_days: int = 2, races_build: int | None = None,
                 with_catalog: bool = True) -> None:
        fetched = (date.fromtimestamp(NOW) - timedelta(days=age_days)).isoformat()
        docs = {"clone_grades.json": {"build": build, "fetched": fetched, "grades": {}},
                "bloodline_races.json": {"build": races_build if races_build else build,
                                         "fetched": fetched, "races": {}}}
        if with_catalog:
            docs["skill_catalog.json"] = {"build": build, "fetched": fetched, "skills": {}}
        for name, payload in docs.items():
            self.write_json(os.path.join(self.data_dir, name), payload, mode=0o644)

    def seed_healthy(self) -> None:
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn", access="access-vela"))
        self.seed_sde()

    def seed_endpoint_cache(self, *, age_hours: float = 0.5) -> str:
        return self.write_json(os.path.join(self.cache_dir, "endpoints.json"),
                               {"fetched_at": NOW - age_hours * 3600,
                                "authorization_endpoint": "https://login.eveonline.com/v2/oauth/authorize",
                                "token_endpoint": "https://login.eveonline.com/v2/oauth/token"})

    # -- running -------------------------------------------------------------

    def report(self, **kwargs) -> dict:
        kwargs.setdefault("now", NOW)
        with contextlib.redirect_stdout(io.StringIO()):
            return doctor.collect(**kwargs)

    def check(self, report: dict, name: str) -> dict:
        found = [c for c in report["checks"] if c["name"] == name]
        self.assertTrue(found, f"no {name} check in {[c['name'] for c in report['checks']]}")
        return found[0]

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return int(code), out.getvalue()


class OfflineReportTest(DoctorTestCase):
    def test_healthy_install_reports_clean_and_exits_zero(self):
        self.seed_healthy()
        report = self.report()
        self.assertEqual(0, report["exit_code"])
        self.assertEqual(0, report["summary"]["fail"])
        self.assertEqual(0, report["summary"]["skip"])
        self.assertEqual("ok", self.check(report, "characters")["status"])
        self.assertEqual("ok", self.check(report, "config.client_id")["status"])
        self.assertEqual("ok", self.check(report, "data.alpha_caps")["status"])
        self.assertNotIn("network.sso", [c["name"] for c in report["checks"]])
        self.assertFalse(report["network"])

    def test_fresh_install_names_the_blockers_and_exits_one(self):
        report = self.report()
        self.assertEqual(1, report["exit_code"])
        self.assertEqual("fail", self.check(report, "characters")["status"])
        self.assertIn("eve-skills login", self.check(report, "characters")["hint"])
        self.assertEqual("fail", self.check(report, "config.client_id")["status"])
        self.assertIn("developers.eveonline.com", self.check(report, "config.client_id")["hint"])

    def test_warnings_alone_still_exit_zero(self):
        self.without_bundled_data()
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"), mode=0o644)
        self.seed_sde(age_days=int(alphadata.STALE_DAYS) + 30, with_catalog=False)
        report = self.report()
        self.assertEqual(0, report["exit_code"])
        self.assertEqual("warn", self.check(report, "permissions.tokens")["status"])
        self.assertEqual("warn", self.check(report, "data.alpha_caps")["status"])
        self.assertIn("update-data", self.check(report, "data.alpha_caps")["hint"])
        self.assertEqual("warn", self.check(report, "data.skill_catalog")["status"])

    def test_corrupt_token_store_fails_without_echoing_the_file(self):
        path = self.write_json(os.path.join(self.config_dir, "tokens.json"), {})
        with open(path, "w") as fh:
            fh.write('{"characters": {"1": {"access_token": "' + ACCESS_ADA + '"')  # truncated JSON
        report = self.report()
        self.assertEqual(1, report["exit_code"])
        self.assertEqual("fail", self.check(report, "tokens.store")["status"])
        self.assertEqual("fail", self.check(report, "characters")["status"])
        self.assertNotIn(ACCESS_ADA, doctor.render_text(report))

    def test_unreadable_token_store_fails(self):
        path = self.seed_store(make_record(ADA, "Ada Vane"))
        os.chmod(path, 0o000)
        if os.geteuid() == 0:
            self.skipTest("root ignores file permissions")
        report = self.report()
        self.assertEqual("fail", self.check(report, "tokens.store")["status"])

    def test_legacy_store_warns_but_still_shows_the_character(self):
        legacy = make_record(ADA, "Ada Vane")
        self.write_json(os.path.join(self.config_dir, "tokens.json"), legacy)
        report = self.report()
        store = self.check(report, "tokens.store")
        self.assertEqual("warn", store["status"])
        self.assertIn("migrates", store["detail"])
        self.assertEqual(0, report["exit_code"])
        self.assertEqual("ok", self.check(report, f"character.{ADA}")["status"])

    def test_legacy_store_without_character_id_is_a_failure(self):
        legacy = make_record(ADA, "Ada Vane")
        del legacy["character_id"]
        self.write_json(os.path.join(self.config_dir, "tokens.json"), legacy)
        report = self.report()
        self.assertEqual("fail", self.check(report, "tokens.store")["status"])
        self.assertEqual(1, report["exit_code"])

    def test_sp_history_is_reported_against_the_pinned_clock(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.seed_sde()
        with open(os.path.join(self.config_dir, "sp-history.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": NOW - 3600, "char_id": ADA, "total_sp": 1000}) + "\n")
            fh.write("not a json row\n")
            fh.write(json.dumps({"ts": NOW - 60, "char_id": VELA, "total_sp": 2000}) + "\n")
        history = self.check(self.report(), "history.sp")
        self.assertEqual("ok", history["status"])
        self.assertEqual(2, history["rows"])         # the malformed row is skipped, not fatal
        self.assertEqual(2, history["characters"])
        self.assertEqual(0.0, history["age_days"])   # (NOW - newest) / 86400, not wall-clock time

    def test_expired_token_without_refresh_token_is_a_failure(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane", refresh=None, expires_in=-7200))
        report = self.report()
        character = self.check(report, f"character.{ADA}")
        self.assertEqual("fail", character["status"])
        self.assertIn("no refresh token", character["detail"])
        self.assertIn("login --char", character["hint"])
        self.assertEqual(1, report["exit_code"])

    def test_expired_token_with_refresh_token_is_only_a_warning(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane", expires_in=-7200))
        self.seed_sde()
        report = self.report()
        character = self.check(report, f"character.{ADA}")
        self.assertEqual("warn", character["status"])
        self.assertEqual("yes", character["auto_refresh"])
        self.assertEqual(0, report["exit_code"])

    def test_record_without_client_id_cannot_refresh(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane", client_id=None))
        self.seed_sde()
        report = self.report()
        self.assertEqual("fail", self.check(report, f"character.{ADA}")["status"])
        self.assertEqual(1, report["exit_code"])

    def test_missing_core_scope_fails_the_character(self):
        self.seed_config()
        core_without_queue = [s for s in sso.SCOPES if s != "esi-skills.read_skillqueue.v1"]
        self.seed_store(make_record(ADA, "Ada Vane", scopes=core_without_queue))
        self.seed_sde()
        character = self.check(self.report(), f"character.{ADA}")
        self.assertEqual("fail", character["status"])
        self.assertEqual(["esi-skills.read_skillqueue.v1"], character["missing_core_scopes"])

    def test_optional_consent_is_reported_but_its_absence_never_fails(self):
        self.seed_config()
        base_only = [s for s in sso.SCOPES]
        self.seed_store(make_record(ADA, "Ada Vane", scopes=base_only),
                        make_record(VELA, "Vela Krinn", access="access-vela",
                                    scopes=sso.SCOPES + sso.scopes_for(["standings", "jobs"])))
        self.seed_sde()
        report = self.report()
        self.assertEqual(0, report["exit_code"])
        self.assertEqual("base only", self.check(report, f"character.{ADA}")["consent"])
        self.assertEqual([], self.check(report, f"character.{ADA}")["granted_features"])
        vela = self.check(report, f"character.{VELA}")
        self.assertEqual(["standings", "jobs"], vela["granted_features"])
        self.assertIn("standings", vela["consent"])

    def test_the_order_consents_are_visible_per_character(self):
        # `orders` and `corp-orders` are the two newest optional features; a user debugging a
        # silent "no orders for this character" has to be able to see them next to the rest.
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane",
                                    scopes=sso.SCOPES + sso.scopes_for(["orders"])),
                        make_record(VELA, "Vela Krinn", access="access-vela"))
        self.seed_sde()
        report = self.report()
        self.assertEqual(["orders"], self.check(report, f"character.{ADA}")["granted_features"])
        self.assertIn("orders", self.check(report, f"character.{ADA}")["consent"])
        self.assertEqual([], self.check(report, f"character.{VELA}")["granted_features"])
        self.assertEqual(0, report["exit_code"])

    def test_one_unusable_character_fails_the_run_but_says_the_rest_work(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"),
                        make_record(VELA, "Vela Krinn", access="access-vela", refresh=None, expires_in=-60))
        self.seed_sde()
        report = self.report()
        # The aggregate stays a warning - other characters still work - but an unusable stored
        # login is a real problem, so the run exits nonzero.
        self.assertEqual("warn", self.check(report, "characters")["status"])
        self.assertEqual("fail", self.check(report, f"character.{VELA}")["status"])
        self.assertEqual("ok", self.check(report, f"character.{ADA}")["status"])
        self.assertEqual(1, report["exit_code"])

    def test_stale_and_mixed_sde_builds_are_warnings(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.seed_sde(age_days=400, races_build=12345)
        report = self.report()
        self.assertEqual("warn", self.check(report, "data.alpha_caps")["status"])
        consistency = self.check(report, "data.consistency")
        self.assertEqual("warn", consistency["status"])
        self.assertIn("12345", consistency["detail"])
        self.assertEqual(0, report["exit_code"])

    def test_bundled_data_is_reported_when_no_user_data_exists(self):
        self.seed_config()
        self.seed_store(make_record(ADA, "Ada Vane"))
        caps = self.check(self.report(), "data.alpha_caps")
        self.assertEqual("bundled package", caps["origin"])

    def test_loose_permissions_are_warnings_not_failures(self):
        self.seed_config(client_secret=CLIENT_SECRET, mode=0o644)
        self.seed_store(make_record(ADA, "Ada Vane"), mode=0o644)
        self.seed_sde()
        report = self.report()
        self.assertEqual("warn", self.check(report, "permissions.tokens")["status"])
        self.assertEqual("warn", self.check(report, "config.file")["status"])
        self.assertEqual(0, report["exit_code"])

    def test_corrupt_config_file_fails(self):
        path = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json")
        self.seed_store(make_record(ADA, "Ada Vane"))
        report = self.report()
        self.assertEqual("fail", self.check(report, "config.client_id")["status"])
        self.assertEqual(1, report["exit_code"])

    def test_environment_client_id_counts_as_configured(self):
        self.seed_config(client_id=None)
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.seed_sde()
        with mock.patch.dict(os.environ, {"EVE_SKILLS_CLIENT_ID": CLIENT_ID}):
            check = self.check(self.report(), "config.client_id")
        self.assertEqual("ok", check["status"])
        self.assertTrue(check["from_environment"])

    def test_callback_guidance_lists_the_registered_ports(self):
        report = self.report()
        callback = self.check(report, "callback")
        for port in sso.REDIRECT_PORTS:
            self.assertIn(f"http://localhost:{port}/callback", callback["redirect_urls"])
        self.assertIn("localhost", doctor.render_text(report))
        self.assertEqual("run: eve-skills login --manual", self.check(report, "callback.manual")["hint"])

    def test_fixed_callback_port_is_reported(self):
        with mock.patch.dict(os.environ, {"EVE_SKILLS_SSO_PORT": "9001"}):
            callback = self.check(self.report(), "callback")
        self.assertEqual("ok", callback["status"])

    def test_nonsense_callback_port_warns(self):
        with mock.patch.dict(os.environ, {"EVE_SKILLS_SSO_PORT": "not-a-port"}):
            callback = self.check(self.report(), "callback")
        self.assertEqual("warn", callback["status"])

    def test_corporation_roles_are_declared_unverifiable_offline(self):
        roles = self.check(self.report(), "roles.corporation")
        self.assertIn("offline", roles["detail"])
        # The order books need a different role than the wallet views, and the hint must say which
        # commands to run rather than leaving the user to guess that a 403 has two possible causes.
        self.assertIn("orders --corp", roles["detail"])
        self.assertIn("Accountant", roles["detail"])
        self.assertIn("eve-skills orders --corp", roles["hint"])

    def test_sp_history_is_aged_against_the_real_clock_when_no_clock_is_given(self):
        # The default `now=None` path is what the CLI uses; ageing history against it
        # must produce a real check, not a crashed diagnostic.
        self.seed_healthy()
        with open(os.path.join(self.config_dir, "sp-history.jsonl"), "w") as fh:
            fh.write(json.dumps({"ts": time.time() - 86400, "char_id": ADA, "total_sp": 5_000_000}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            report = doctor.collect()
        history = self.check(report, "history.sp")
        self.assertEqual("ok", history["status"])
        self.assertAlmostEqual(1.0, history["age_days"], places=1)
        self.assertEqual([], [c for c in report["checks"] if c["name"].startswith("diagnostics.")])


class WatchCoverageTest(DoctorTestCase):
    """What the watchers remember and what they have announced - both read without touching."""

    def setUp(self):
        super().setUp()
        self.seed_healthy()

    @staticmethod
    def queue_entry(name: str, updated: float) -> dict:
        return {"name": name, "queue_len": 2, "last_finish": None, "known": {}, "updated": updated}

    @staticmethod
    def owner_entry(name: str, updated: float, order_ids=(700000001,)) -> dict:
        return {"name": name, "open": {str(i): {"price": 10.0} for i in order_ids},
                "pending": {}, "settled": {}, "updated": updated}

    @staticmethod
    def event_row(ident: str, kind: str, ts: float, **extra) -> dict:
        return {"id": ident, "ts": ts, "kind": kind, "character_id": ADA,
                "character_name": "Ada Vane", "skill_id": None, "skill_name": None,
                "finished_level": None, "finish_date": None, "data": {}, **extra}

    def test_a_fresh_install_has_nothing_to_report_yet(self):
        report = self.report()
        state = self.check(report, "watch.state")
        events = self.check(report, "watch.events")
        self.assertEqual("ok", state["status"])
        self.assertIn("no watch state yet", state["detail"])
        self.assertEqual(self.state_dir, os.path.dirname(state["path"]))
        self.assertEqual("ok", events["status"])
        self.assertIn("no recorded events yet", events["detail"])
        self.assertEqual(0, report["summary"]["fail"])

    def test_state_counts_every_order_owner(self):
        self.seed_watch_state({
            "version": 2,
            "characters": {str(ADA): self.queue_entry("Ada Vane", NOW - 9 * 86400)},
            "owners": {f"char:{ADA}": self.owner_entry("Ada Vane", NOW - 5 * 86400, (1, 2)),
                       "corp:98356123": self.owner_entry("Shared Ledger Holdings", NOW - 2 * 86400, (3,))},
        })
        state = self.check(self.report(), "watch.state")
        self.assertEqual("ok", state["status"])
        self.assertEqual(1, state["characters"])
        self.assertEqual(2, state["order_owners"])
        self.assertEqual(1, state["character_order_owners"])
        self.assertEqual(1, state["corporation_order_owners"])
        self.assertEqual(3, state["open_orders"])
        # the newest poll of any kind ages the state - here that is the corporation's, not the queue's
        self.assertEqual(2.0, state["age_days"])
        self.assertIn("2 order owner(s)", state["detail"])
        self.assertIsNone(state.get("hint"))

    def test_a_training_only_state_names_the_command_that_fixes_it(self):
        # Nothing about this is broken - but a user who wonders why no order ever gets announced
        # needs to be pointed at the poll that starts tracking them.
        self.seed_watch_state({"version": 2, "characters": {str(ADA): self.queue_entry("Ada Vane", NOW - 60)}})
        state = self.check(self.report(), "watch.state")
        self.assertEqual("ok", state["status"])
        self.assertIn("no order owner", state["detail"])
        self.assertIn("orders --watch", state["hint"])

    def test_corrupt_watch_state_warns_without_blocking(self):
        # load_state() treats it as empty, so commands keep working; what is lost is the baseline.
        self.seed_watch_state('{"characters": {"91000001"')
        report = self.report()
        state = self.check(report, "watch.state")
        self.assertEqual("warn", state["status"])
        self.assertIn("corrupt", state["detail"])
        self.assertIn("mv '", state["hint"])
        self.assertEqual(0, report["exit_code"])

    def test_event_history_is_split_between_training_and_orders(self):
        self.seed_events([
            self.event_row("e1", "training_finished", NOW - 9 * 86400),
            self.event_row("e2", "queue_empty", NOW - 7 * 86400),
            self.event_row("e3", "order_filled", NOW - 5 * 86400,
                           data={"owner_key": f"char:{ADA}", "owner_name": "Ada Vane"}),
            self.event_row("e4", "order_expired", NOW - 4 * 86400),
            self.event_row("e5", "order_closed", NOW - 3 * 86400),
        ])
        events = self.check(self.report(), "watch.events")
        self.assertEqual("ok", events["status"])
        self.assertEqual(5, events["events"])
        self.assertEqual(2, events["training_events"])
        self.assertEqual(3, events["order_events"])
        self.assertEqual({"order_closed": 1, "order_expired": 1, "order_filled": 1,
                          "queue_empty": 1, "training_finished": 1}, events["kinds"])
        self.assertEqual(0, events["unreadable_lines"])
        self.assertEqual(3.0, events["age_days"])

    def test_damaged_event_lines_are_counted_not_swallowed(self):
        self.seed_events([self.event_row("e1", "order_filled", NOW - 10),
                          "{ not json",
                          self.event_row("e2", "training_finished", NOW - 5)])
        report = self.report()
        events = self.check(report, "watch.events")
        self.assertEqual("warn", events["status"])
        self.assertEqual(2, events["events"])
        self.assertEqual(1, events["unreadable_lines"])
        self.assertIn("1 unreadable line", events["detail"])
        self.assertEqual(0, report["exit_code"])


class NetworkReportTest(DoctorTestCase):
    def setUp(self):
        super().setUp()
        self.seed_healthy()

    def test_reachable_services_are_green(self):
        transport = FakeTransport({SSO_URL: sso_document(),
                                   ESI_STATUS: {"server_version": "34944160", "players": 20000,
                                                "start_time": "2026-09-05T11:03:19Z"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE, "2026-08-04"]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        self.assertEqual(0, report["exit_code"])
        for name in ("network.sso", "network.esi", "network.esi_compat", "network.market"):
            self.assertEqual("ok", self.check(report, name)["status"], self.check(report, name)["detail"])

    def test_probes_are_unauthenticated_and_identify_the_tool(self):
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            self.report(network=True)
        self.assertEqual([SSO_URL, ESI_STATUS, ESI_COMPAT, ESI_MARKET], transport.urls)
        for _, headers, _ in transport.calls:
            self.assertNotIn("authorization", headers)
            self.assertIn("user-agent", headers)

    def test_offline_mode_never_opens_a_socket(self):
        transport = FakeTransport({})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report()
        self.assertEqual([], transport.calls)
        self.assertNotIn("network.sso", [c["name"] for c in report["checks"]])

    def test_local_data_behind_the_live_server_build_is_a_warning(self):
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "34944161"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.esi")
        self.assertEqual("warn", check["status"])
        self.assertIn("update-data", check["hint"])
        self.assertEqual(0, report["exit_code"])

    def test_retired_compatibility_date_is_a_warning(self):
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": ["2030-01-01"]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            check = self.check(self.report(network=True), "network.esi_compat")
        self.assertEqual("warn", check["status"])
        self.assertEqual("2030-01-01", check["newest_compatibility_date"])

    def test_sso_outage_with_fresh_endpoint_cache_is_survivable(self):
        self.seed_endpoint_cache(age_hours=1)
        transport = FakeTransport({SSO_URL: dns_failure(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.sso")
        self.assertEqual("warn", check["status"])
        self.assertEqual("dns", check["category"])
        self.assertTrue(check["endpoint_cache_usable"])
        self.assertEqual(0, report["exit_code"])

    def test_sso_outage_without_cache_blocks_login(self):
        transport = FakeTransport({SSO_URL: dns_failure(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.sso")
        self.assertEqual("fail", check["status"])
        self.assertFalse(check["endpoint_cache_usable"])
        self.assertEqual(1, report["exit_code"])

    def test_stale_endpoint_cache_does_not_excuse_an_sso_outage(self):
        self.seed_endpoint_cache(age_hours=doctor.ENDPOINT_CACHE_HOURS + 5)
        transport = FakeTransport({SSO_URL: dns_failure(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            check = self.check(self.report(network=True), "network.sso")
        self.assertEqual("fail", check["status"])
        self.assertFalse(check["endpoint_cache_usable"])

    def test_esi_outage_fails_and_skips_the_compatibility_probe(self):
        # ESI itself is down, so the order book (same host) must not spend a second request on it.
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: http_error(503),
                                   ESI_COMPAT: dns_failure()})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.esi")
        self.assertEqual("fail", check["status"])
        self.assertIn("HTTP 503", check["detail"])
        self.assertEqual("skip", self.check(report, "network.esi_compat")["status"])
        self.assertEqual("skip", self.check(report, "network.market")["status"])
        self.assertNotIn(ESI_MARKET, transport.urls)
        self.assertEqual(1, report["exit_code"])

    def test_timeout_is_classified_and_bounded(self):
        transport = FakeTransport({SSO_URL: TimeoutError(), ESI_STATUS: TimeoutError(),
                                   ESI_COMPAT: TimeoutError()})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True, timeout=0.01)
        check = self.check(report, "network.sso")
        self.assertEqual("timeout", check["category"])
        for _, _, used_timeout in transport.calls:
            self.assertGreaterEqual(used_timeout, 0.5)

    def test_captive_portal_style_non_json_is_reported_as_protocol(self):
        class HtmlResponse:
            status = 200

            def read(self):
                return b"<html><title>Portal</title></html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", lambda request, timeout=None: HtmlResponse()):
            report = self.report(network=True)
        self.assertEqual("protocol", self.check(report, "network.esi")["category"])
        # A portal page instead of /status means ESI is not being reached at all, so the book probe
        # does not spend a second request that can only be intercepted the same way.
        self.assertEqual("skip", self.check(report, "network.market")["status"])

    def test_market_probe_reports_freshness_and_leftover_budget(self):
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            check = self.check(self.report(network=True), "network.market")
        self.assertEqual("ok", check["status"])
        self.assertEqual(60.0, check["age_seconds"])
        self.assertEqual(12000, check["ratelimit_budget"])
        self.assertEqual(11998, check["ratelimit_remaining"])
        self.assertIn("generated 1m 00s ago", check["detail"])
        self.assertIn("budget 11998 of 12000 left", check["detail"])
        # the probe must measure the same ESI version the tool talks to, not whichever one is newest
        self.assertEqual(doctor.esi_mod.COMPAT_DATE, transport.calls[-1][1]["x-compatibility-date"])

    def test_order_book_that_stops_being_a_list_is_reported_as_broken(self):
        # ESI answered like itself on /status but handed back an error object here: the market is
        # unreadable, which is a blocker for `market`, not a network outage.
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: ({"error": "unsupported media type"}, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.market")
        self.assertEqual("fail", check["status"])
        self.assertIn("not a list of orders", check["detail"])
        self.assertEqual(1, report["exit_code"])

    def test_order_book_without_last_modified_cannot_be_dated(self):
        headers = {"X-Ratelimit-Limit": "12000/15m", "X-Ratelimit-Remaining": "11998"}
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, headers)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.market")
        self.assertEqual("warn", check["status"])
        self.assertIsNone(check["age_seconds"])
        self.assertIn("no Last-Modified", check["detail"])
        self.assertEqual(0, report["exit_code"])

    def test_stale_order_book_is_reported_as_an_upstream_problem(self):
        headers = dict(MARKET_HEADERS, **{"Last-Modified": formatdate(NOW - 3 * 86400, usegmt=True)})
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, headers)})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.market")
        self.assertEqual("warn", check["status"])
        self.assertIn("five-minute", check["detail"])
        self.assertIn("status.eveonline.com", check["hint"])
        self.assertEqual(0, report["exit_code"])

    def test_rate_limited_order_book_warns_and_names_the_waiting_time(self):
        # 420 is ESI's own "error limited" code; either way this is a wait, not a broken install.
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: http_error(429, {"Retry-After": "30"})})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.market")
        self.assertEqual("warn", check["status"])
        self.assertIn("rate limited", check["detail"])
        self.assertIn("30s", check["detail"])
        self.assertIn("--global", check["hint"])
        self.assertEqual(0, report["exit_code"])

    def test_unreachable_order_book_fails_because_nothing_is_cached_behind_it(self):
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: dns_failure()})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        check = self.check(report, "network.market")
        self.assertEqual("fail", check["status"])
        self.assertIn("no cached fallback", check["hint"])
        self.assertEqual(1, report["exit_code"])


class SecretHygieneTest(DoctorTestCase):
    def setUp(self):
        super().setUp()
        self.seed_config(client_secret=CLIENT_SECRET)
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.seed_sde()

    def test_no_credential_value_or_key_reaches_the_report(self):
        report = self.report()
        for key, value in strings(report):
            self.assertNotIn(key, FORBIDDEN_KEYS)
            for secret in SECRET_VALUES:
                self.assertNotIn(secret, value)
        text = doctor.render_text(report)
        for secret in SECRET_VALUES:
            self.assertNotIn(secret, text)

    def test_json_output_is_stable_machine_readable(self):
        report = self.report()
        payload = json.loads(doctor.render_json(report))
        self.assertEqual(report, payload)
        self.assertEqual(["tool", "doctor_version", "generated", "read_only", "network", "versions",
                          "checks", "summary", "exit_code"], list(payload))
        self.assertTrue(payload["read_only"])
        for check in payload["checks"]:
            self.assertIn(check["status"], doctor.STATUSES)
            self.assertTrue(check["detail"])

    def test_transport_error_narrating_a_token_is_redacted(self):
        leaky = urllib.error.URLError(RuntimeError(f"server rejected Authorization: Bearer {JWTISH}"))
        transport = FakeTransport({SSO_URL: leaky, ESI_STATUS: leaky, ESI_COMPAT: leaky})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        text = doctor.render_text(report)
        self.assertNotIn(JWTISH, text)
        self.assertIn(doctor.REDACTED, text)

    def test_stored_token_value_is_redacted_from_any_check(self):
        leaky = urllib.error.URLError(RuntimeError(f"retrying with {ACCESS_ADA}"))
        transport = FakeTransport({SSO_URL: leaky, ESI_STATUS: leaky, ESI_COMPAT: leaky})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        self.assertNotIn(ACCESS_ADA, doctor.render_text(report))
        self.assertIn(doctor.REDACTED, self.check(report, "network.sso")["detail"])


class HomePathTest(DoctorTestCase):
    """A report is meant to be pasted into a bug report, so it must not name the operator's home
    directory - with `$XDG_*` unset every path falls back under it, and with it their username."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {          # unset in the way the code reads it
            "XDG_CONFIG_HOME": "", "XDG_CACHE_HOME": "",
            "XDG_DATA_HOME": "", "XDG_STATE_HOME": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # ... which is where the four fallbacks land, all of them inside $HOME
        self.config_dir = os.path.join(self.home, ".config", "eve-skills")
        self.cache_dir = os.path.join(self.home, ".cache", "eve-skills")
        self.data_dir = os.path.join(self.home, ".local", "share", "eve-skills")
        self.state_dir = os.path.join(self.home, ".local", "state", "eve-skills")

    def seed_corrupt_store(self) -> str:
        path = os.path.join(self.config_dir, "tokens.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('{"characters": {"1": ')            # truncated: the store is corrupt
        return path

    def test_the_home_directory_appears_nowhere_and_the_tilde_form_everywhere(self):
        self.seed_healthy()
        self.seed_watch_state({"version": 1, "characters": {}, "owners": {}})
        report = self.report()
        for _key, value in strings(report):
            self.assertNotIn(self.home, value)
        for rendered in (doctor.render_text(report), doctor.render_json(report)):
            self.assertNotIn(self.home, rendered)
        self.assertIn("~/.config/eve-skills", doctor.render_text(report))
        self.assertIn('"~/.config/eve-skills/tokens.json"', doctor.render_json(report))
        self.assertIn('"~/.local/state/eve-skills/watch-state.json"', doctor.render_json(report))

    def test_a_configured_location_outside_the_home_is_printed_as_it_is(self):
        outside = os.path.join(self.tmp.name, "srv", "eve-config")   # the operator's own choice
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": outside}):
            self.write_json(os.path.join(outside, "eve-skills", "config.json"), {"client_id": CLIENT_ID})
            report = self.report()
        self.assertEqual(os.path.join(outside, "eve-skills"), self.check(report, "path.config")["path"])
        self.assertEqual(os.path.join(outside, "eve-skills", "tokens.json"),
                         self.check(report, "tokens.store")["path"])
        self.assertNotIn('"~/.config/eve-skills', doctor.render_json(report))

    def test_a_directory_beside_the_home_is_not_collapsed_into_it(self):
        sibling = os.path.join(self.tmp.name, "home2", "config")     # $HOME is <tmp>/home
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": sibling}):
            report = self.report()
        self.assertEqual(os.path.join(sibling, "eve-skills"), self.check(report, "path.config")["path"])
        self.assertNotIn("~2", doctor.render_json(report))

    def test_home_itself_and_an_unresolvable_or_root_home(self):
        with mock.patch.dict(os.environ, {"HOME": "/tmp/x/home"}):
            self.assertEqual("~", doctor._display_path("/tmp/x/home"))
            self.assertEqual("~/.config/eve-skills/tokens.json",
                             doctor._display_path("/tmp/x/home/.config/eve-skills/tokens.json"))
            self.assertEqual("/tmp/x/home2/tokens.json", doctor._display_path("/tmp/x/home2/tokens.json"))
        for unusable in ("/", "", "~"):   # no home, or one that would rewrite every absolute path
            with mock.patch.dict(os.environ, {"HOME": unusable}):
                self.assertEqual("/tmp/x/home/tokens.json",
                                 doctor._display_path("/tmp/x/home/tokens.json"))
                self.assertNotIn("~", str(doctor._mask_home(["/tmp/x/home/tokens.json"])))

    def run_hint_command(self, hint: str) -> None:
        """Run the command a hint prints, exactly as printed, with $HOME set as the report saw it."""
        command = hint[hint.index("mv "):].split(", then run:")[0]
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True,
                              env={**os.environ, "HOME": self.home})
        self.assertEqual(0, done.returncode, f"{command}\n{done.stderr}")

    @unittest.skipUnless(shutil.which("bash"), "needs a POSIX shell to paste the hint into")
    def test_a_hint_can_be_pasted_into_a_shell_and_actually_runs(self):
        path = self.seed_corrupt_store()
        self.seed_watch_state("{not json")
        report = self.report()
        store_hint = self.check(report, "tokens.store")["hint"]
        self.assertIn('mv "$HOME/.config/eve-skills/tokens.json"'
                      ' "$HOME/.config/eve-skills/tokens.json.bak"', store_hint)
        self.assertIn('mv "$HOME/.local/state/eve-skills/watch-state.json"'
                      ' "$HOME/.local/state/eve-skills/watch-state.json.bak"',
                      self.check(report, "watch.state")["hint"])
        for check in report["checks"]:
            hint = check.get("hint", "")
            self.assertNotIn(self.home, hint)
            self.assertNotIn("'~", hint)      # a quoted tilde is literal: the command would fail
        self.run_hint_command(store_hint)     # not just the string - it has to work
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(f"{path}.bak"))

    @unittest.skipUnless(shutil.which("bash"), "needs a POSIX shell to paste the hint into")
    def test_a_hint_for_a_location_outside_the_home_keeps_the_runnable_absolute_form(self):
        outside = os.path.join(self.tmp.name, "srv", "eve-config")
        path = os.path.join(outside, "eve-skills", "tokens.json")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": outside}):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write('{"characters": {"1": ')
            report = self.report()
        hint = self.check(report, "tokens.store")["hint"]
        self.assertIn(f"mv '{path}' '{path}.bak'", hint)   # nothing to expand; quotes suffice
        self.run_hint_command(hint)
        self.assertTrue(os.path.exists(f"{path}.bak"))

    def test_the_shell_form_expands_home_and_leaves_anything_else_alone(self):
        with mock.patch.dict(os.environ, {"HOME": "/tmp/x/home"}):
            self.assertEqual('"$HOME"', doctor._shell_path("/tmp/x/home"))
            self.assertEqual('"$HOME/.config/eve-skills/tokens.json"',
                             doctor._shell_path("/tmp/x/home/.config/eve-skills/tokens.json"))
        self.assertEqual("'/srv/eve config/tokens.json'", doctor._shell_path("/srv/eve config/tokens.json"))

    def test_credentials_are_still_redacted_and_paths_never_are(self):
        self.seed_config(client_secret=CLIENT_SECRET)
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.seed_sde()
        leaky = urllib.error.URLError(RuntimeError(f"retrying with {ACCESS_ADA}"))
        transport = FakeTransport({SSO_URL: leaky, ESI_STATUS: leaky, ESI_COMPAT: leaky})
        with mock.patch("urllib.request.urlopen", transport):
            report = self.report(network=True)
        for rendered in (doctor.render_text(report), doctor.render_json(report)):
            for secret in SECRET_VALUES:
                self.assertNotIn(secret, rendered)
        self.assertIn(doctor.REDACTED, doctor.render_text(report))
        paths = [value for key, value in strings(report) if key == "path" and isinstance(value, str)]
        self.assertTrue([p for p in paths if p.startswith("~/")], "expected tilde-relative paths")
        self.assertEqual([], [p for p in paths if doctor.REDACTED in p])



class ReadOnlyTest(DoctorTestCase):
    """The whole point: running doctor must leave the install byte-identical."""

    def snapshot(self) -> dict:
        state = {}
        for home in (self.config_home(), self.cache_home(), self.data_home(), self.state_home()):
            if not os.path.isdir(home):
                state[home] = "absent"
                continue
            for root, dirs, files in os.walk(home):
                for entry in dirs + files:
                    path = os.path.join(root, entry)
                    info = os.stat(path)
                    digest = ""
                    if stat.S_ISREG(info.st_mode):
                        with open(path, "rb") as fh:
                            digest = hashlib.sha256(fh.read()).hexdigest()
                    state[path] = (stat.S_IMODE(info.st_mode), info.st_size, digest)
        return state

    def test_doctor_writes_nothing_to_a_seeded_install(self):
        self.seed_healthy()
        before = self.snapshot()
        for argv in (["doctor"], ["doctor", "--json"]):
            code, _ = self.cli(*argv)
            self.assertEqual(0, code, argv)
        self.assertEqual(before, self.snapshot())

    def test_doctor_does_not_create_the_directories_it_inspects(self):
        code, text = self.cli("doctor")
        self.assertEqual(1, code)  # nothing configured yet is a real blocker
        for home in (self.config_home(), self.cache_home(), self.data_home(), self.state_home()):
            self.assertFalse(os.path.exists(home), f"{home} must not be created by a read-only run")
        self.assertIn("does not exist yet", text)

    def test_doctor_leaves_a_legacy_store_unmigrated(self):
        path = self.write_json(os.path.join(self.config_dir, "tokens.json"), make_record(ADA, "Ada Vane"))
        before = self.snapshot()
        code, _ = self.cli("doctor")
        self.assertEqual(0, code)
        self.assertEqual(before, self.snapshot())
        with open(path) as fh:
            self.assertNotIn("characters", json.load(fh))  # still the single-record layout


class CliSurfaceTest(DoctorTestCase):
    def test_json_flag_emits_parsable_report_and_exit_code_follows_problems(self):
        self.seed_healthy()
        code, text = self.cli("doctor", "--json")
        self.assertEqual(0, code)
        self.assertEqual(0, json.loads(text)["exit_code"])

        os.chmod(os.path.join(self.config_dir, "tokens.json"), 0o000)
        if os.geteuid() != 0:
            code, text = self.cli("doctor", "--json")
            self.assertEqual(1, code)
            self.assertEqual(1, json.loads(text)["exit_code"])

    def test_text_report_shows_the_character_matrix(self):
        self.seed_healthy()
        code, text = self.cli("doctor")
        self.assertEqual(0, code)
        self.assertIn("Characters", text)
        self.assertIn("Ada Vane", text)
        self.assertNotIn(ACCESS_ADA, text)

    def test_timeout_option_is_accepted(self):
        self.seed_healthy()
        transport = FakeTransport({SSO_URL: sso_document(), ESI_STATUS: {"server_version": "1"},
                                   ESI_COMPAT: {"compatibility_dates": [doctor.esi_mod.COMPAT_DATE]},
                                   ESI_MARKET: (MARKET_ROWS, MARKET_HEADERS)})
        with mock.patch("urllib.request.urlopen", transport):
            code, _ = self.cli("doctor", "--network", "--timeout", "3")
        self.assertEqual(0, code)
        # every probe honours --timeout, including the order book added later
        self.assertEqual([3.0] * len(transport.urls), [t for _, _, t in transport.calls])
        self.assertIn(doctor.esi_mod.BASE + doctor.MARKET_PROBE_PATH, transport.urls)


if __name__ == "__main__":
    unittest.main()
