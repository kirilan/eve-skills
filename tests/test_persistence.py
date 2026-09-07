"""Durable-write and token-lifecycle invariants: concurrency, atomicity, permissions.

Every test runs inside a throwaway $XDG_* tree with the transports mocked, so nothing
here can read or touch a real config, cache, token or network service."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from eve_skills import alphadata, cli, esi, snapshots, sso, storage

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADA, VELA = 91000001, 91000002


def make_record(character_id: int, name: str, *, access: str | None = None,
                refresh: str | None = "refresh-1", expires_in: float = 3600.0) -> dict:
    return {
        "client_id": "test-client",
        "client_secret": None,
        "access_token": access or f"access-{character_id}",
        "refresh_token": refresh,
        "expires_at": time.time() + expires_in,
        "scopes": list(sso.SCOPES),
        "character_id": character_id,
        "character_name": name,
    }


def fake_jwt(claims: dict) -> str:
    """A structurally valid unsigned JWT; sso.decode_jwt reads the payload for real."""
    return ".".join([sso._b64url(b'{"alg":"none"}'), sso._b64url(json.dumps(claims).encode()), ""])


class XdgTestCase(unittest.TestCase):
    """Isolates XDG_CONFIG_HOME / CACHE / DATA in a TemporaryDirectory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-persist-")
        self.addCleanup(self.tmp.cleanup)
        self.config_home = os.path.join(self.tmp.name, "config")
        self.cache_home = os.path.join(self.tmp.name, "cache")
        self.data_home = os.path.join(self.tmp.name, "data")
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.config_home,
            "XDG_CACHE_HOME": self.cache_home,
            "XDG_DATA_HOME": self.data_home,
            "EVE_SKILLS_CLIENT_ID": "",   # a real env override must not leak in
            "EVE_SKILLS_SSO_PORT": "",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = os.path.join(self.config_home, "eve-skills")
        self.cache_dir = os.path.join(self.cache_home, "eve-skills")
        self.data_dir = os.path.join(self.data_home, "eve-skills")
        for directory in (self.config_dir, self.cache_dir, self.data_dir):
            os.makedirs(directory)

    # -- store helpers -------------------------------------------------------

    def tokens_path(self) -> str:
        return os.path.join(self.config_dir, "tokens.json")

    def seed_store(self, *records: dict):
        self.write_json(self.tokens_path(), {"characters": {str(r["character_id"]): r for r in records}})

    def read_store(self) -> dict:
        with open(self.tokens_path()) as fh:
            return json.load(fh)

    @staticmethod
    def write_text(path: str, text: str, mode: int | None = None) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def write_json(self, path: str, obj, mode: int | None = None) -> str:
        return self.write_text(path, json.dumps(obj), mode)

    # -- filesystem assertions ----------------------------------------------

    def mode(self, path: str) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def temp_leftovers(self, directory: str) -> list[str]:
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory) if name.endswith(".tmp"))


# ---------------------------------------------------------------------------
# storage primitives
# ---------------------------------------------------------------------------

class StorageTests(XdgTestCase):
    def test_failed_replace_leaves_the_old_file_and_no_temporary(self):
        path = os.path.join(self.config_dir, "endpoints.json")
        self.write_json(path, {"keep": True})
        with mock.patch.object(storage.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.atomic_write_json(path, {"keep": False})
        self.assertEqual(self.read_json_loose(path), {"keep": True})
        self.assertEqual(self.temp_leftovers(self.config_dir), [])

    @staticmethod
    def read_json_loose(path: str):
        with open(path) as fh:
            return json.load(fh)

    def test_lock_is_reentrant_for_the_same_thread(self):
        path = os.path.join(self.config_dir, "tokens.lock")
        with storage.file_lock(path):          # sso.refresh() holds this...
            with storage.file_lock(path):      # ...and calls load_store(), which takes it again
                self.assertEqual(self.mode(path), 0o600)

    def test_lock_never_allows_two_holders_at_once(self):
        path = os.path.join(self.config_dir, "tokens.lock")
        events: list[str] = []

        def worker(tag: str):
            with storage.file_lock(path):
                events.append(f"{tag}-in")
                time.sleep(0.05)               # long enough for an unlocked peer to overlap
                events.append(f"{tag}-out")

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, tag) for tag in ("a", "b")]
            for future in futures:             # a deadlock shows up as a timeout here
                future.result(timeout=15)
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0][0], events[1][0])  # one holder finished before the other entered

    def test_lock_serialises_two_real_processes(self):
        """flock, not just our in-process guard: separate interpreters cannot interleave."""
        child = textwrap.dedent("""
            import sys, time
            from eve_skills import snapshots
            char_id = int(sys.argv[1])
            for n in range(5):
                snapshots.record(char_id, 1000 * char_id + n)
        """)
        env = {**os.environ,
               "XDG_CONFIG_HOME": self.config_home, "XDG_CACHE_HOME": self.cache_home,
               "XDG_DATA_HOME": self.data_home, "PYTHONPATH": REPO_ROOT}
        procs = [subprocess.Popen([sys.executable, "-c", child, str(91000 + i)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
        rows = snapshots.load()
        self.assertEqual(len(rows), 15, f"lost rows: {sorted((r['char_id'], r['total_sp']) for r in rows)}")
        self.assertEqual(self.temp_leftovers(self.config_dir), [])


# ---------------------------------------------------------------------------
# token store: rotation races
# ---------------------------------------------------------------------------

class TokenRefreshTests(XdgTestCase):
    ENDPOINTS = {"token_endpoint": "https://login.example/token"}

    def test_shared_expiring_token_is_refreshed_once(self):
        self.seed_store(make_record(ADA, "Ada Vane", expires_in=10))  # inside REFRESH_LEEWAY
        posts: list[dict] = []
        posts_guard = threading.Lock()

        def fake_post(url, fields, basic_auth=None):
            with posts_guard:
                posts.append(dict(fields))
            time.sleep(0.02)  # widen the window a racy second refresher would use
            return {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 1199}

        barrier = threading.Barrier(4, timeout=5)
        with (
            mock.patch.object(sso, "_discover", return_value=self.ENDPOINTS),
            mock.patch.object(sso, "_post_form", side_effect=fake_post),
        ):
            def worker(_):
                barrier.wait()
                return sso.get_access_token(ADA)["access_token"]

            with ThreadPoolExecutor(max_workers=4) as pool:
                tokens = list(pool.map(worker, range(4)))

        self.assertEqual(len(posts), 1, f"refresh-token was POSTED {len(posts)} times")
        self.assertEqual(posts[0]["refresh_token"], "refresh-1")
        self.assertEqual(set(tokens), {"access-2"})
        stored = self.read_store()["characters"][str(ADA)]
        self.assertEqual(stored["refresh_token"], "refresh-2")  # rotation persisted
        self.assertGreater(stored["expires_at"], time.time())

    def test_two_processes_share_one_refresh_request(self):
        """The real deployment shape: a watch process and a manual command race, each in its own
        interpreter. EVE rotates the refresh token, so a second POST of it would be rejected."""
        self.seed_store(make_record(ADA, "Ada Vane", expires_in=10))
        post_log = os.path.join(self.tmp.name, "posts.log")
        child = textwrap.dedent("""
            import os, sys, time
            from eve_skills import sso

            def fake_post(url, fields, basic_auth=None):
                with open(os.environ["EVE_TEST_POST_LOG"], "a") as fh:
                    fh.write(fields["refresh_token"] + "\\n")
                time.sleep(0.3)   # hold the window a racy second refresher would need
                return {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 1199}

            sso._discover = lambda: {"token_endpoint": "https://login.example/token"}
            sso._post_form = fake_post
            print(sso.get_access_token(int(sys.argv[1]))["access_token"])
        """)
        env = {**os.environ,
               "XDG_CONFIG_HOME": self.config_home, "XDG_CACHE_HOME": self.cache_home,
               "XDG_DATA_HOME": self.data_home, "PYTHONPATH": REPO_ROOT, "EVE_TEST_POST_LOG": post_log}
        procs = [subprocess.Popen([sys.executable, "-c", child, str(ADA)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        printed = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            printed.append(out.decode().strip())

        with open(post_log) as fh:
            posted = [line.strip() for line in fh if line.strip()]
        self.assertEqual(posted, ["refresh-1"], f"the expiring refresh token was POSTED {len(posted)} times")
        self.assertEqual(printed, ["access-2"] * 3)   # losers waited and returned the winner's token
        self.assertEqual(self.read_store()["characters"][str(ADA)]["refresh_token"], "refresh-2")


    def test_each_character_rotates_independently(self):
        self.seed_store(make_record(ADA, "Ada Vane", refresh="r-ada", expires_in=10),
                        make_record(VELA, "Vela Krinn", refresh="r-vela", expires_in=10))
        posts: list[str] = []
        guard = threading.Lock()

        def fake_post(url, fields, basic_auth=None):
            token = fields["refresh_token"]
            with guard:
                posts.append(token)
            time.sleep(0.02)
            return {"access_token": f"access-{token}", "refresh_token": f"next-{token}", "expires_in": 1199}

        barrier = threading.Barrier(2, timeout=5)
        with (
            mock.patch.object(sso, "_discover", return_value=self.ENDPOINTS),
            mock.patch.object(sso, "_post_form", side_effect=fake_post),
        ):
            def worker(char_id):
                barrier.wait()
                return sso.get_access_token(char_id)["access_token"]

            with ThreadPoolExecutor(max_workers=2) as pool:
                tokens = list(pool.map(worker, (ADA, VELA)))

        self.assertEqual(sorted(posts), ["r-ada", "r-vela"])
        self.assertEqual(set(tokens), {"access-r-ada", "access-r-vela"})
        chars = self.read_store()["characters"]
        self.assertEqual(chars[str(ADA)]["refresh_token"], "next-r-ada")   # neither write clobbered the other
        self.assertEqual(chars[str(VELA)]["refresh_token"], "next-r-vela")

    def test_refresh_makes_no_request_for_a_record_another_process_rotated(self):
        stale = make_record(ADA, "Ada Vane", expires_in=10)
        rotated = make_record(ADA, "Ada Vane", access="access-2", refresh="refresh-2", expires_in=1199)
        self.seed_store(rotated)
        with mock.patch.object(sso, "_post_form") as post:
            result = sso.refresh(stale)
        post.assert_not_called()
        self.assertEqual(result["access_token"], "access-2")

    def test_failed_refresh_leaves_the_record_usable_for_a_retry(self):
        self.seed_store(make_record(ADA, "Ada Vane", expires_in=10))
        with (
            mock.patch.object(sso, "_discover", return_value=self.ENDPOINTS),
            mock.patch.object(sso, "_post_form", side_effect=RuntimeError("SSO token request failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "token request failed"):
                sso.get_access_token(ADA)
        self.assertEqual(self.read_store()["characters"][str(ADA)]["access_token"], f"access-{ADA}")
        with (
            mock.patch.object(sso, "_discover", return_value=self.ENDPOINTS),
            mock.patch.object(sso, "_post_form",
                              return_value={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 1199}),
        ):
            self.assertEqual(sso.get_access_token(ADA)["access_token"], "access-2")

    def test_refresh_of_an_unstored_character_is_explicit(self):
        self.seed_store(make_record(VELA, "Vela Krinn"))
        with self.assertRaisesRegex(RuntimeError, "no stored login for character id"):
            sso.refresh(make_record(ADA, "Ada Vane"))

    def test_expired_without_refresh_token_asks_for_login(self):
        self.seed_store(make_record(ADA, "Ada Vane", refresh=None, expires_in=-10))
        with self.assertRaisesRegex(RuntimeError, "session expired"):
            sso.get_access_token(ADA)


# ---------------------------------------------------------------------------
# token store: layout migration, selection, logout
# ---------------------------------------------------------------------------

class TokenStoreLifecycleTests(XdgTestCase):
    def test_legacy_single_character_file_is_migrated(self):
        legacy = make_record(ADA, "Ada Vane")
        self.write_json(self.tokens_path(), legacy, mode=0o644)  # pre-multi-character layout
        store = sso.load_store()
        self.assertEqual(list(store["characters"]), [str(ADA)])
        self.assertEqual(store["characters"][str(ADA)]["character_name"], "Ada Vane")
        on_disk = self.read_store()
        self.assertNotIn("access_token", on_disk)      # rewritten in the new layout
        self.assertEqual(self.mode(self.tokens_path()), 0o600)
        self.assertEqual(sso.list_characters()[0]["character_id"], ADA)

    def test_legacy_record_without_character_id_is_an_explicit_error(self):
        broken = make_record(ADA, "Ada Vane")
        broken.pop("character_id")
        self.write_json(self.tokens_path(), broken)
        with self.assertRaisesRegex(RuntimeError, "without character_id"):
            sso.load_store()

    def test_corrupt_store_reads_as_logged_out(self):
        self.write_text(self.tokens_path(), "{not json")
        self.assertEqual(sso.list_characters(), [])
        with self.assertRaisesRegex(RuntimeError, "not logged in"):
            sso.get_access_token()

    def test_concurrent_logins_keep_every_character(self):
        ids = [91000000 + n for n in range(8)]
        barrier = threading.Barrier(len(ids), timeout=5)

        def login(character_id: int):
            barrier.wait()
            sso._put_record(make_record(character_id, f"Pilot {character_id}"))

        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            list(pool.map(login, ids))
        self.assertEqual(sorted(self.read_store()["characters"]), [str(i) for i in ids])
        self.assertEqual(self.temp_leftovers(self.config_dir), [])

    def test_selection_by_id_exact_name_and_unique_substring(self):
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn"))
        self.assertEqual(sso.resolve_character(str(ADA)), ADA)
        self.assertEqual(sso.resolve_character("vela krinn"), VELA)
        self.assertEqual(sso.resolve_character("krinn"), VELA)

    def test_ambiguous_and_unknown_selections_list_the_choices(self):
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Adalia Krinn"))
        with self.assertRaisesRegex(RuntimeError, "ambiguous.*Ada Vane.*Adalia Krinn"):
            sso.resolve_character("ada")
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            sso.resolve_character("nobody")

    def test_implicit_choice_needs_a_sole_character(self):
        self.seed_store(make_record(ADA, "Ada Vane"))
        self.assertEqual(sso.get_access_token()["character_id"], ADA)
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn"))
        with self.assertRaisesRegex(RuntimeError, "multiple characters stored"):
            sso.get_access_token()

    def test_logout_one_character_keeps_the_other(self):
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn"))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["logout", "--char", "Ada Vane"]), 0)
        self.assertIn("Removed stored tokens for Ada Vane.", out.getvalue())
        self.assertEqual(list(self.read_store()["characters"]), [str(VELA)])
        self.assertEqual(self.mode(self.tokens_path()), 0o600)

    def test_logout_all_removes_the_store(self):
        self.seed_store(make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["logout"]), 0)
        self.assertFalse(os.path.exists(self.tokens_path()))
        self.assertEqual(sso.list_characters(), [])

    def test_logout_without_any_stored_tokens_is_quiet(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["logout"]), 0)

    def test_logout_of_an_unknown_character_reports_through_the_cli(self):
        self.seed_store(make_record(ADA, "Ada Vane"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["logout", "--char", "Nobody"]), 1)
        self.assertIn("unknown", err.getvalue())
        self.assertEqual(list(self.read_store()["characters"]), [str(ADA)])  # nothing removed

    def test_logout_and_refresh_concurrently_leave_a_consistent_store(self):
        ada, vela = make_record(ADA, "Ada Vane"), make_record(VELA, "Vela Krinn", expires_in=10)
        self.seed_store(ada, vela)
        barrier = threading.Barrier(2, timeout=5)

        def fake_post(url, fields, basic_auth=None):
            time.sleep(0.02)
            return {"access_token": "access-vela-2", "refresh_token": "refresh-vela-2", "expires_in": 1199}

        with (
            mock.patch.object(sso, "_discover", return_value={"token_endpoint": "https://login.example/token"}),
            mock.patch.object(sso, "_post_form", side_effect=fake_post),
        ):
            def logout():
                barrier.wait()
                sso.clear_tokens(ADA)

            def refresh_vela():
                barrier.wait()
                return sso.get_access_token(VELA)["access_token"]

            with ThreadPoolExecutor(max_workers=2) as pool:
                logout_job = pool.submit(logout)
                refresh_job = pool.submit(refresh_vela)
                logout_job.result(timeout=10)
                token = refresh_job.result(timeout=10)

        chars = self.read_store()["characters"]
        self.assertEqual(list(chars), [str(VELA)])          # logout won its character, lost nothing else
        self.assertEqual(chars[str(VELA)]["access_token"], "access-vela-2")
        self.assertEqual(token, "access-vela-2")


# ---------------------------------------------------------------------------
# manual login: callback validation and persistence
# ---------------------------------------------------------------------------

class ManualLoginLifecycleTests(XdgTestCase):
    TOKEN = {"access_token": fake_jwt({"sub": f"CHARACTER:EVE:{ADA}", "name": "Ada Vane", "scp": sso.SCOPES}),
             "refresh_token": "refresh-1", "expires_in": 1200}

    def login(self, callback_url: str, *, client_secret: str | None = None):
        """Drive `login --manual` with everything external mocked; returns (code, stdout, stderr)."""
        endpoints = {"authorization_endpoint": "https://login.example/authorize",
                     "token_endpoint": "https://login.example/token"}
        claims = {"sub": f"CHARACTER:EVE:{ADA}", "name": "Ada Vane", "scp": sso.SCOPES}
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sso, "_discover", return_value=endpoints),
            mock.patch.object(sso.secrets, "token_urlsafe", return_value="expected-state"),
            mock.patch.object(sso.webbrowser, "open"),
            mock.patch("builtins.input", return_value=callback_url),
            mock.patch.object(sso, "_post_form", return_value=self.TOKEN),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["login", "--client-id", "test-client",
                             *(["--client-secret", client_secret] if client_secret else []), "--manual"])
        return code, out.getvalue(), err.getvalue()

    def test_accepted_callback_persists_record_and_config_privately(self):
        code, out, _ = self.login("http://localhost:8635/callback?code=abc&state=expected-state",
                                  client_secret="shhh")
        self.assertEqual(code, 0)
        self.assertIn("Logged in as Ada Vane", out)
        record = self.read_store()["characters"][str(ADA)]
        self.assertEqual(record["refresh_token"], "refresh-1")
        self.assertEqual(record["scopes"], sso.SCOPES)
        self.assertGreater(record["expires_at"], time.time())
        with open(os.path.join(self.config_dir, "config.json")) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["client_id"], "test-client")
        self.assertEqual(cfg["client_secret"], "shhh")
        self.assertEqual({self.mode(self.tokens_path()), self.mode(os.path.join(self.config_dir, "config.json"))},
                         {0o600})
        self.assertEqual(self.temp_leftovers(self.config_dir), [])

    def test_foreign_callback_url_is_rejected_before_any_token_request(self):
        code, _, err = self.login("http://evil.example:8635/callback?code=abc&state=expected-state")
        self.assertEqual(code, 1)
        self.assertIn("callback URL must start with http://localhost:8635/callback", err)
        self.assertFalse(os.path.exists(self.tokens_path()))

    def test_wrong_port_callback_is_rejected(self):
        code, _, err = self.login("http://localhost:9999/callback?code=abc&state=expected-state")
        self.assertEqual(code, 1)
        self.assertIn("callback URL must start with", err)

    def test_state_mismatch_is_rejected_as_possible_csrf(self):
        code, _, err = self.login("http://localhost:8635/callback?code=abc&state=forged-state")
        self.assertEqual(code, 1)
        self.assertIn("state mismatch", err)
        self.assertFalse(os.path.exists(self.tokens_path()))

    def test_sso_error_response_is_reported_verbatim(self):
        code, _, err = self.login(
            "http://localhost:8635/callback?error=access_denied&error_description=user+said+no&state=expected-state")
        self.assertEqual(code, 1)
        self.assertIn("SSO returned an error: access_denied user said no", err)
        self.assertFalse(os.path.exists(self.tokens_path()))

    def test_second_login_adds_a_character_without_losing_the_first(self):
        self.login("http://localhost:8635/callback?code=abc&state=expected-state")
        with mock.patch.object(sso, "decode_jwt", return_value={
            "sub": f"CHARACTER:EVE:{VELA}", "name": "Vela Krinn", "scp": sso.SCOPES}):
            code, _, _ = self.login("http://localhost:8635/callback?code=abc&state=expected-state")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(sso.list_characters(), key=lambda r: r["character_name"])[0]["character_name"],
                         "Ada Vane")


# ---------------------------------------------------------------------------
# SP history
# ---------------------------------------------------------------------------

class SnapshotPersistenceTests(XdgTestCase):
    def history_path(self) -> str:
        return os.path.join(self.config_dir, "sp-history.jsonl")

    def test_concurrent_records_keep_every_row(self):
        writers = [(9100 + w, 3) for w in range(6)]
        barrier = threading.Barrier(len(writers), timeout=5)

        def worker(spec):
            char_id, rows = spec
            barrier.wait()
            for n in range(rows):
                snapshots.record(char_id, char_id * 100 + n)

        with ThreadPoolExecutor(max_workers=len(writers)) as pool:
            list(pool.map(worker, writers))

        rows = snapshots.load()
        self.assertEqual(len(rows), 18, f"lost rows: {len(rows)} of 18")
        for char_id, count in writers:
            self.assertEqual(sorted(r["total_sp"] for r in rows if r["char_id"] == char_id),
                             [char_id * 100 + n for n in range(count)])
        self.assertEqual(self.temp_leftovers(self.config_dir), [])

    def test_record_prunes_expired_rows_and_corrupt_lines(self):
        now = time.time()
        fresh = {"ts": now - 3600, "char_id": 1, "total_sp": 111}
        self.write_text(self.history_path(), "\n".join([
            json.dumps({"ts": now - (snapshots.RETENTION_DAYS + 5) * 86400, "char_id": 1, "total_sp": 1}),
            "this line is not json",
            json.dumps(fresh),
            "",
        ]))
        snapshots.record(1, 222)
        rows = snapshots.load()
        self.assertEqual([r["total_sp"] for r in rows], [111, 222])

    def test_history_survives_a_reader_writing_a_second_character(self):
        snapshots.record(ADA, 1000)
        snapshots.record(VELA, 2000)
        self.assertEqual(snapshots.latest(ADA)["total_sp"], 1000)
        self.assertEqual(snapshots.latest(VELA)["total_sp"], 2000)


# ---------------------------------------------------------------------------
# name cache
# ---------------------------------------------------------------------------

class NameCachePersistenceTests(XdgTestCase):
    def cache_path(self) -> str:
        return os.path.join(self.cache_dir, "names.json")

    class Client:
        def __init__(self, delay: float = 0.0, barrier: threading.Barrier | None = None, fail=False):
            self.delay, self.barrier, self.fail = delay, barrier, fail

        def post(self, _path, ids):
            if self.barrier:
                self.barrier.wait()      # force the resolvers to overlap before either writes
            if self.delay:
                time.sleep(self.delay)
            if self.fail:
                raise esi.EsiError("HTTP 404 on /universe/names: unresolved")
            return [{"id": ident, "name": f"name {ident}"} for ident in ids]

    def test_concurrent_resolvers_merge_into_one_cache(self):
        barrier = threading.Barrier(2, timeout=5)
        client = self.Client(barrier=barrier)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda ident: esi.resolve_names(client, {ident}, cache_dir=self.cache_dir), (1, 2)))
        self.assertEqual(results, [{1: "name 1"}, {2: "name 2"}])
        with open(self.cache_path()) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, {"1": "name 1", "2": "name 2"})   # neither writer lost the other's id
        self.assertEqual(self.temp_leftovers(self.cache_dir), [])

    def test_unresolved_ids_stay_out_of_the_cache(self):
        result = esi.resolve_names(self.Client(fail=True), {7}, cache_dir=self.cache_dir)
        self.assertEqual(result, {})
        self.assertEqual(esi._read_name_cache(self.cache_path()), {})


# ---------------------------------------------------------------------------
# SDE data update
# ---------------------------------------------------------------------------

class AlphadataUpdateTests(XdgTestCase):
    BUILD = 2500001

    def setUp(self):
        super().setUp()
        self.in_flight = 0
        self.max_in_flight = 0
        self.downloads: list[str] = []
        self.guard = threading.Lock()
        self.zip_blob = self.make_zip()

    def make_zip(self) -> bytes:
        """One skill with a prerequisite, one bare skill, and types that are not skills."""
        members = {
            "cloneGrades.jsonl": '{"_key": 1, "name": "Caldari Alpha Clone", "skills": [{"typeID": 1003, "level": 3}]}\n',
            "bloodlines.jsonl": '{"_key": 402, "raceID": 1}\n',
            "dogmaAttributes.jsonl": '{"_key": 164, "name": "Perception"}\n{"_key": 165, "name": "Intelligence"}\n',
            # typeDogma values are floats in the real SDE; 180/181 mark a skill, 275 is its rank.
            "typeDogma.jsonl": '{"_key": 1003, "dogmaAttributes": [{"attributeID": 180, "value": 164.0},'
                               ' {"attributeID": 181, "value": 165.0}, {"attributeID": 275, "value": 2.0},'
                               ' {"attributeID": 182, "value": 1002.0}, {"attributeID": 277, "value": 3.0}]}\n'
                               '{"_key": 1002, "dogmaAttributes": [{"attributeID": 180, "value": 165.0},'
                               ' {"attributeID": 181, "value": 164.0}, {"attributeID": 275, "value": 1.0}]}\n'
                               '{"_key": 900, "dogmaAttributes": [{"attributeID": 180, "value": 164.0}]}\n',
            "types.jsonl": '{"_key": 1003, "name": {"en": "Astrogeology", "de": "Astrogeologie"}, "published": true}\n'
                           '{"_key": 1002, "name": {"en": "Science"}, "published": true}\n'
                           '{"_key": 900, "name": {"en": "Reactor Control Unit"}, "published": false}\n',
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, payload in members.items():
                zf.writestr(name, payload)
        return buf.getvalue()

    def fake_fetch(self, url: str) -> bytes:
        if url.endswith("latest.jsonl"):
            return json.dumps({"buildNumber": self.BUILD, "releaseDate": "2026-09-01"}).encode()
        with self.guard:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.downloads.append(url)
        try:
            time.sleep(0.05)   # the ~100 MB download window where two runs used to collide
            return self.zip_blob
        finally:
            with self.guard:
                self.in_flight -= 1

    def test_concurrent_updates_neither_collide_nor_leave_temporaries(self):
        barrier = threading.Barrier(2, timeout=5)

        def run(_):
            barrier.wait()          # both processes arrive before either takes the lock
            return alphadata.update()

        # Patched once for the whole section: threads must not race on each other's
        # mock teardown and fall back to the real network fetch.
        with (
            mock.patch.object(alphadata, "_fetch", side_effect=self.fake_fetch),
            mock.patch("builtins.print"),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                summaries = list(pool.map(run, (0, 1)))

        self.assertEqual([s["build"] for s in summaries], [self.BUILD, self.BUILD])
        self.assertEqual(summaries[0]["grades"]["1"], {"name": "Caldari Alpha Clone", "skills": 1})
        self.assertEqual(self.max_in_flight, 1, "two update-data runs downloaded at once")
        for name in ("clone_grades.json", "bloodline_races.json", "skill_catalog.json"):
            path = os.path.join(self.data_dir, name)
            with open(path) as fh:
                self.assertEqual(json.load(fh)["build"], self.BUILD)
        self.assertEqual(self.temp_leftovers(self.data_dir), [])
        # Only types carrying both attributes are skills; rank and prerequisites survive the round trip.
        self.assertEqual(summaries[0]["catalog_skills"], 2)
        self.assertEqual(alphadata.skill_catalog(), {
            1003: alphadata.SkillInfo(1003, "Astrogeology", 2, "perception", "intelligence", {1002: 3}),
            1002: alphadata.SkillInfo(1002, "Science", 1, "intelligence", "perception", {}),
        })


if __name__ == "__main__":
    unittest.main()
