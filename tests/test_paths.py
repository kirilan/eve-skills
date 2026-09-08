"""``eve_skills.paths``: one layout policy, both platforms, proven from a POSIX host.

The Windows branch is exercised by injecting exactly the inputs the platform provides -
the ``is_windows`` seam and the profile environment - never skipped: an OS contract this
module relies on that goes untested here goes untested everywhere until a user hits it.
No Windows machine was involved in any of these tests, and none is needed to state one."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from eve_skills import alphadata, paths, snapshots, sso, watchstate

from tests.platform_contract import home_variables

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


class WindowsLayoutTest(unittest.TestCase):
    """What the four directories resolve to when the OS is Windows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "profile")   # the stand-in for C:\Users\alice
        patcher = mock.patch.object(paths, "is_windows", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve_all(self, *, create: bool = False, **profile_vars) -> tuple[str, str, str, str]:
        """Resolve the four kinds under a fake profile; XDG_* deliberately absent.

        A Windows runner really does define ``%USERPROFILE%``, ``%APPDATA%`` and
        ``%LOCALAPPDATA%``, and ``expanduser`` there reads ``USERPROFILE`` rather than ``HOME`` -
        so "no profile variables" has to be manufactured, not hoped for, or the fallback test
        would resolve against the CI account instead of this fixture.
        """
        with mock.patch.dict(os.environ, {**home_variables(self.home), **profile_vars}):
            for var in (*XDG_VARS, "APPDATA", "LOCALAPPDATA"):
                if var not in profile_vars:
                    os.environ.pop(var, None)      # patch.dict restores them on exit
            return (paths.config_dir(create=create), paths.cache_dir(create=create),
                    paths.data_dir(create=create), paths.state_dir(create=create))

    def test_profile_variables_place_each_kind(self):
        roaming = os.path.join(self.tmp.name, "Roaming")
        local = os.path.join(self.tmp.name, "Local")
        config, cache, data, state = self.resolve_all(APPDATA=roaming, LOCALAPPDATA=local)
        # Nothing roams. The config directory holds tokens.json - live refresh tokens and an
        # optional client secret - and %APPDATA% is what domain profile sync and OneDrive
        # Known Folder Move replicate, so credentials would leave the machine with it.
        self.assertEqual(config, os.path.join(local, "eve-skills", "config"))
        self.assertEqual(cache, os.path.join(local, "eve-skills", "cache"))
        self.assertEqual(data, os.path.join(local, "eve-skills", "data"))
        self.assertEqual(state, os.path.join(local, "eve-skills", "state"))
        self.assertNotIn(roaming, config)

    def test_missing_variables_fall_back_to_the_documented_profile_folders(self):
        # Windows without %APPDATA%/%LOCALAPPDATA% (odd service contexts) still has a profile.
        config, cache, data, state = self.resolve_all()
        local = os.path.join(self.home, "AppData", "Local")
        self.assertEqual(config, os.path.join(local, "eve-skills", "config"))
        self.assertEqual(cache, os.path.join(local, "eve-skills", "cache"))
        self.assertEqual(data, os.path.join(local, "eve-skills", "data"))
        self.assertEqual(state, os.path.join(local, "eve-skills", "state"))

    def test_create_builds_the_tree_under_the_profile(self):
        local = os.path.join(self.tmp.name, "Local")
        _config, cache, _data, _state = self.resolve_all(create=True, LOCALAPPDATA=local)
        self.assertTrue(os.path.isdir(cache))


class PosixDefaultsTest(unittest.TestCase):
    """With no pins on the POSIX branch, every directory lands exactly where it always did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-paths-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, home_variables(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in (*XDG_VARS, "APPDATA", "LOCALAPPDATA"):
            os.environ.pop(var, None)
        # The XDG layout is a branch of the resolver, not a property of the machine running the
        # test: driven through the seam so it is still asserted - and still XDG - on Windows.
        branch = mock.patch.object(paths, "is_windows", return_value=False)
        branch.start()
        self.addCleanup(branch.stop)

    def test_xdg_defaults_unchanged(self):
        self.assertEqual(paths.config_dir(create=False), os.path.join(self.tmp.name, ".config", "eve-skills"))
        self.assertEqual(paths.cache_dir(create=False), os.path.join(self.tmp.name, ".cache", "eve-skills"))
        self.assertEqual(paths.data_dir(create=False), os.path.join(self.tmp.name, ".local", "share", "eve-skills"))
        self.assertEqual(paths.state_dir(create=False), os.path.join(self.tmp.name, ".local", "state", "eve-skills"))

    def test_create_false_never_touches_disk_on_either_branch(self):
        for windows in (False, True):
            with self.subTest(windows=windows), mock.patch.object(paths, "is_windows", return_value=windows):
                for resolve in (paths.config_dir, paths.cache_dir, paths.data_dir, paths.state_dir):
                    resolve(create=False)
        leftovers = [os.path.join(self.tmp.name, *parts)
                     for parts in ((".config",), (".cache",), (".local",), ("AppData",))]
        self.assertEqual([], [p for p in leftovers if os.path.exists(p)])


class XdgPrecedenceTest(unittest.TestCase):
    """An explicit pin wins on every platform - containers and fixtures own the layout,
    Windows included, and it is what makes a test like this one possible there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.pins = {var: os.path.join(self.tmp.name, var.lower()) for var in XDG_VARS}
        # A pin is taken as given: no Windows profile folders may compete with it...
        self.env = {**home_variables(os.path.join(self.tmp.name, "profile")),
                    "APPDATA": os.path.join(self.tmp.name, "roaming-never-used"),
                    "LOCALAPPDATA": os.path.join(self.tmp.name, "local-never-used"), **self.pins}

    def test_pins_win_on_either_platform(self):
        for windows in (False, True):
            with self.subTest(windows=windows), \
                    mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(paths, "is_windows", return_value=windows):
                self.assertEqual(paths.config_dir(create=False), os.path.join(self.pins["XDG_CONFIG_HOME"], "eve-skills"))
                self.assertEqual(paths.cache_dir(create=False), os.path.join(self.pins["XDG_CACHE_HOME"], "eve-skills"))
                self.assertEqual(paths.data_dir(create=False), os.path.join(self.pins["XDG_DATA_HOME"], "eve-skills"))
                self.assertEqual(paths.state_dir(create=False), os.path.join(self.pins["XDG_STATE_HOME"], "eve-skills"))

    def test_an_empty_variable_counts_as_unset(self):
        env = {**self.env, "XDG_CONFIG_HOME": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(paths, "is_windows", return_value=False):
            self.assertEqual(paths.config_dir(create=False),
                             os.path.join(env["HOME"], ".config", "eve-skills"))


class SingleResolverCutoverTest(unittest.TestCase):
    """No module resolves a location for itself any more: point the four kinds at four
    separate trees and every artefact in the package must land in its own kind's tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eve-skills-paths-")
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "etc")
        self.cache = os.path.join(self.tmp.name, "scratch")
        self.data = os.path.join(self.tmp.name, "srv-data")
        self.state = os.path.join(self.tmp.name, "var-state")
        patcher = mock.patch.dict(os.environ, {
            "HOME": os.path.join(self.tmp.name, "home"),
            "XDG_CONFIG_HOME": self.cfg, "XDG_CACHE_HOME": self.cache,
            "XDG_DATA_HOME": self.data, "XDG_STATE_HOME": self.state,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_artefact_lands_in_its_own_kind(self):
        leaf = "eve-skills"
        self.assertEqual(sso.config_file(create=False), os.path.join(self.cfg, leaf, "config.json"))
        self.assertEqual(sso.token_store_file(create=False), os.path.join(self.cfg, leaf, "tokens.json"))
        self.assertEqual(snapshots.history_file(create=False), os.path.join(self.cfg, leaf, "sp-history.jsonl"))
        self.assertEqual(sso.endpoints_cache_file(create=False), os.path.join(self.cache, leaf, "endpoints.json"))
        self.assertEqual(watchstate.state_file(create=False), os.path.join(self.state, leaf, "watch-state.json"))
        self.assertEqual(watchstate.events_file(create=False), os.path.join(self.state, leaf, "events.jsonl"))

    def test_alphadata_reads_what_lives_in_the_data_tree(self):
        user_dir = os.path.join(self.data, "eve-skills")
        os.makedirs(user_dir, exist_ok=True)
        for name in ("clone_grades.json", "bloodline_races.json"):
            with open(os.path.join(user_dir, name), "w", encoding="utf-8") as fh:
                json.dump({"build": 1, "marker": "user-tree"}, fh)
        docs = alphadata.load()
        self.assertEqual(docs["grades"]["marker"], "user-tree")
        self.assertEqual(docs["races"]["marker"], "user-tree")


if __name__ == "__main__":
    unittest.main()
