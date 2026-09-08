"""Release packaging: metadata, license text, artifact contents, clean-install entrypoints.

Three tiers, cheapest first:

1. The working tree — what ``pyproject.toml`` claims, whether ``LICENSE`` really is the GPL-3.0
   text, and whether build output stays out of version control.
2. A real PEP 517 build (wheel + sdist) from a *copy* of the sources into a temporary directory,
   so the checkout is never written to and no network is touched.
3. Installing that wheel into a throwaway venv and running it from outside the source tree — the
   only way to assert what a user gets rather than what this repository happens to contain.

Every path is derived at runtime; nothing pins an interpreter location, a virtualenv name or a
developer's home directory. The heavy tiers skip themselves when their tooling is absent
(``setuptools``, a working ``venv``/``pip``) and fail when the packaging itself is wrong.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from eve_skills import __version__, alphadata

from tests.platform_contract import home_variables

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
LICENSE_PATH = REPO_ROOT / "LICENSE"

with open(PYPROJECT_PATH, "rb") as _fh:
    PYPROJECT = tomllib.load(_fh)
PROJECT = PYPROJECT["project"]

DIST_NAME = "eve-skills"
LICENSE_EXPRESSION = "GPL-3.0-only"
CONSOLE_SCRIPT = "eve-skills"
ENTRY_POINT_TARGET = "eve_skills.cli:main"

# sha256 of https://www.gnu.org/licenses/gpl-3.0.txt. The GPL may be copied verbatim or not at
# all, so one hash is a complete statement of "this is the license text".
GPLV3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"

# What a distribution has to carry, derived from the source tree rather than a hand-kept list.
SOURCE_MODULES = sorted(p.name for p in (REPO_ROOT / "eve_skills").glob("*.py"))
SDE_DATA_FILES = list(alphadata.DATA_FILES)

SCRIPTS_DIR = "Scripts" if os.name == "nt" else "bin"


def _exe(name: str) -> str:
    """The file name of a virtualenv entry point here.

    A POSIX venv installs ``bin/eve-skills`` as a script; Windows installs ``Scripts\\eve-skills.exe``,
    a real launcher binary, because the OS will not run a shebang line. Naming it is not cosmetic:
    passing an extension-less path happens to work because CreateProcess appends ``.exe`` itself, so
    a missing launcher on Windows would surface as 'command not found' for a name that does not exist
    rather than as this test failing to find what the wheel installed."""
    return name + (".exe" if os.name == "nt" else "")


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment without this process's import path or EVE configuration."""
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "PYTHONHOME"} and not k.startswith("EVE_SKILLS_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


_TMP: tempfile.TemporaryDirectory | None = None


def _workdir() -> Path:
    """Scratch directory shared by the build/install tiers, removed when the run ends."""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.TemporaryDirectory(prefix="eve-skills-release-")
        atexit.register(_TMP.cleanup)
    return Path(_TMP.name)


_ARTIFACTS: tuple[Path, Path] | None = None


def _build_one(hook: str, source: Path, out: Path, suffix: str) -> Path:
    """Run one PEP 517 hook in its own process and directory; two hooks in one process leave
    the second artifact somewhere other than the requested output directory."""
    out.mkdir(parents=True, exist_ok=True)
    code = f"import sys, setuptools.build_meta as m; m.{hook}(sys.argv[1])"
    proc = subprocess.run([sys.executable, "-c", code, str(out)], cwd=source, env=_subprocess_env(),
                          capture_output=True, text=True, timeout=900)
    produced = sorted(out.glob(f"*{suffix}"))
    if proc.returncode != 0 or len(produced) != 1:
        raise AssertionError(f"{hook} produced no single {suffix} in {out} (rc={proc.returncode}):"
                             f"\n{proc.stdout}\n{proc.stderr}")
    return produced[0]


def _build() -> tuple[Path, Path]:
    """Build wheel and sdist once per run from a copy of the sources; returns both paths."""
    global _ARTIFACTS
    if _ARTIFACTS is not None:
        return _ARTIFACTS
    if importlib.util.find_spec("setuptools") is None:
        raise unittest.SkipTest("setuptools is not installed, so no distribution can be built")

    src = _workdir() / "src"
    shutil.copytree(REPO_ROOT, src, ignore=shutil.ignore_patterns(
        "__pycache__", ".git", ".venv", "venv", "build", "dist", "*.egg-info", ".pytest_cache"))
    _ARTIFACTS = (_build_one("build_wheel", src, _workdir() / "wheel-out", ".whl"),
                  _build_one("build_sdist", src, _workdir() / "sdist-out", ".tar.gz"))
    return _ARTIFACTS


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


def _wheel_text(wheel: Path, suffix: str) -> str:
    with zipfile.ZipFile(wheel) as zf:
        return zf.read(next(n for n in zf.namelist() if n.endswith(suffix))).decode()


_VENV: Path | None = None


def _installed_venv() -> Path:
    """A venv containing nothing but the built wheel, created once per run."""
    global _VENV
    if _VENV is not None:
        return _VENV
    wheel, _ = _build()
    root = _workdir() / "venv"
    create = subprocess.run([sys.executable, "-m", "venv", str(root)], env=_subprocess_env(),
                            capture_output=True, text=True, timeout=900)
    if create.returncode != 0:
        raise unittest.SkipTest(f"cannot create a virtualenv here: {create.stderr.strip()}")
    install = subprocess.run([str(root / SCRIPTS_DIR / _exe("python")), "-m", "pip", "install",
                              "--no-index", "--no-deps", "--disable-pip-version-check", str(wheel)],
                             env=_subprocess_env(), capture_output=True, text=True, timeout=900)
    if install.returncode != 0:
        raise unittest.SkipTest(f"pip cannot install the built wheel offline: {install.stderr.strip()}")
    _VENV = root
    return root


class MetadataTests(unittest.TestCase):
    """What the built distribution will claim about itself."""

    def test_license_file_is_the_unmodified_gplv3_text(self):
        self.assertEqual(hashlib.sha256(LICENSE_PATH.read_bytes()).hexdigest(), GPLV3_SHA256,
                         "LICENSE is not the verbatim GPLv3 text from gnu.org")

    def test_declared_license_matches_the_file_and_drops_superseded_classifiers(self):
        self.assertEqual(PROJECT["license"], LICENSE_EXPRESSION)
        self.assertIn(LICENSE_PATH.name, PROJECT["license-files"])
        # PEP 639: setuptools rejects a license expression together with `License ::`
        # classifiers, so keeping both is a broken build rather than a style choice.
        self.assertEqual([c for c in PROJECT["classifiers"] if c.startswith("License ::")], [])

    def test_build_backend_is_new_enough_for_the_metadata_it_is_given(self):
        spec = " ".join(PYPROJECT["build-system"]["requires"])
        match = re.search(r"setuptools\s*(?:>=|==)\s*(\d+)", spec)
        self.assertIsNotNone(match, f"no setuptools floor declared in {spec!r}")
        self.assertGreaterEqual(int(match.group(1)), 77,
                                "SPDX license expressions and license-files need setuptools >= 77")

    def test_readme_is_declared_and_present(self):
        readme = REPO_ROOT / PROJECT["readme"]
        self.assertTrue(readme.is_file(), f"declared readme {readme.name} is missing")
        self.assertIn(f"# {DIST_NAME}", readme.read_text(encoding="utf-8"))

    def test_version_has_one_source_of_truth(self):
        # A second version literal here would ship metadata disagreeing with `eve-skills --version`.
        self.assertNotIn("version", PROJECT)
        self.assertIn("version", PROJECT["dynamic"])
        self.assertEqual(PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"],
                         "eve_skills.__version__")

    def test_runtime_stays_dependency_free_and_release_tooling_is_optional(self):
        self.assertEqual(PROJECT["dependencies"], [],
                         "eve-skills is standard library only; a runtime dependency is a decision")
        self.assertTrue(PROJECT["optional-dependencies"]["release"])

    def test_console_script_target_resolves(self):
        target = PROJECT["scripts"][CONSOLE_SCRIPT]
        self.assertEqual(target, ENTRY_POINT_TARGET)
        module_name, _, attribute = target.partition(":")
        self.assertTrue(callable(getattr(importlib.import_module(module_name), attribute)))


class ReleaseArtifactIgnoreTests(unittest.TestCase):
    """Built artifacts are rebuildable and must never reach the repository."""

    ARTIFACTS = [
        "dist/eve_skills-0.1.0-py3-none-any.whl",
        "dist/eve_skills-0.1.0.tar.gz",
        "build/lib/eve_skills/cli.py",
        "eve_skills.egg-info/PKG-INFO",
        ".eggs/setuptools_helper-1.0-py3.11.egg/README.txt",
        "wheels/eve_skills-0.1.0-py3-none-any.whl",
        "bdist_wheel/eve_skills-0.1.0.tar.gz",
    ]
    SOURCES = ["pyproject.toml", "README.md", "RELEASE.md", "LICENSE",
               "eve_skills/data/skill_catalog.json", "tests/test_packaging.py"]

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")
        if not (REPO_ROOT / ".git").exists():
            raise unittest.SkipTest("not a git checkout")

    def _ignored(self, relative: str) -> bool:
        proc = subprocess.run(["git", "check-ignore", "-q", relative], cwd=REPO_ROOT,
                              capture_output=True, timeout=60)
        return proc.returncode == 0

    def test_build_output_is_not_version_controlled(self):
        self.assertEqual([path for path in self.ARTIFACTS if not self._ignored(path)], [])

    def test_shipped_sources_are_still_tracked(self):
        # Keeps the check above from passing vacuously (e.g. after a blanket `*` rule).
        self.assertEqual([path for path in self.SOURCES if self._ignored(path)], [])


class ArtifactContentTests(unittest.TestCase):
    """Contents of the wheel and sdist built from these sources."""

    @classmethod
    def setUpClass(cls):
        cls.wheel, cls.sdist = _build()

    def test_wheel_carries_every_module_and_the_sde_documents(self):
        names = _wheel_names(self.wheel)
        for module in SOURCE_MODULES:
            self.assertIn(f"eve_skills/{module}", names)
        for document in SDE_DATA_FILES:
            self.assertIn(f"eve_skills/data/{document}", names,
                          f"{document} is not packaged - a fresh install cannot resolve skills")

    def test_wheel_metadata_matches_the_release_and_stays_dependency_free(self):
        headers: dict[str, list[str]] = {}
        for line in _wheel_text(self.wheel, ".dist-info/METADATA").splitlines():
            if not line or ":" not in line or line[:1].isspace():
                continue
            key, _, value = line.partition(":")
            headers.setdefault(key.strip(), []).append(value.strip())
        self.assertEqual(headers["Name"], [DIST_NAME])
        self.assertEqual(headers["Version"], [__version__])
        self.assertEqual(headers.get("License-Expression"), [LICENSE_EXPRESSION])
        self.assertEqual(headers.get("Summary"), [PROJECT["description"]])
        # Optional tooling shows up as `Requires-Dist: …; extra == "release"`; anything without
        # that marker is a runtime dependency, and this project deliberately has none.
        declared = headers.get("Requires-Dist", [])
        self.assertEqual([d for d in declared if "extra ==" not in d], [],
                         "the wheel declares a runtime dependency")
        self.assertTrue([d for d in declared if 'extra == "release"' in d],
                        "the optional [release] extra did not reach the metadata")

    def test_wheel_declares_the_console_entrypoint_and_ships_the_license(self):
        self.assertIn(f"{CONSOLE_SCRIPT} = {ENTRY_POINT_TARGET}",
                      _wheel_text(self.wheel, ".dist-info/entry_points.txt"))
        licenses = [n for n in _wheel_names(self.wheel)
                    if ".dist-info/licenses/" in n and n.rsplit("/", 1)[-1].upper().startswith("LICENS")]
        self.assertTrue(licenses, "no license file inside the wheel")

    def test_sdist_carries_sources_readme_license_and_data(self):
        with tarfile.open(self.sdist) as tar:
            members = {m.name.split("/", 1)[1] for m in tar.getmembers() if "/" in m.name}
        for required in ["pyproject.toml", "README.md", "LICENSE"]:
            self.assertIn(required, members)
        for module in SOURCE_MODULES:
            self.assertIn(f"eve_skills/{module}", members)
        for document in SDE_DATA_FILES:
            self.assertIn(f"eve_skills/data/{document}", members)


class InstalledEntrypointTests(unittest.TestCase):
    """The wheel as a user installs it: run from elsewhere, with an empty $XDG tree."""

    @classmethod
    def setUpClass(cls):
        cls.venv = _installed_venv()
        home = _workdir() / "home"
        # A directory with no Python files in it, so nothing can import this checkout by accident.
        cls.cwd = _workdir() / "outside"
        cls.cwd.mkdir(parents=True, exist_ok=True)
        cls.env = _subprocess_env({
            **home_variables(str(home)),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_STATE_HOME": str(home / "state"),
            # The XDG pins win on every platform, but a Windows runner also has a real APPDATA and
            # LOCALAPPDATA for the CI account; pinning them keeps an installed run out of it even if
            # resolution changes.
            "APPDATA": str(home / "roaming"),
            "LOCALAPPDATA": str(home / "local"),
        })

    def run_installed(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # The CLI's own output is UTF-8 on every platform (see cli._use_utf8_streams), so the reader
        # says so instead of trusting the console code page.
        return subprocess.run(args, cwd=self.cwd, env=self.env, capture_output=True,
                              text=True, encoding="utf-8", timeout=300)

    def test_console_script_and_module_entrypoint_report_the_same_version(self):
        script = self.run_installed([str(self.venv / SCRIPTS_DIR / _exe(CONSOLE_SCRIPT)), "--version"])
        module = self.run_installed([str(self.venv / SCRIPTS_DIR / _exe("python")), "-m", "eve_skills",
                                     "--version"])
        self.assertEqual((script.returncode, module.returncode), (0, 0),
                         f"console script: {script.stderr}\nmodule: {module.stderr}")
        self.assertEqual(script.stdout, module.stdout)
        self.assertEqual(script.stdout.strip(), f"{DIST_NAME} {__version__}")

    def test_console_script_and_module_entrypoint_share_the_help_surface(self):
        script = self.run_installed([str(self.venv / SCRIPTS_DIR / _exe(CONSOLE_SCRIPT)), "--help"])
        module = self.run_installed([str(self.venv / SCRIPTS_DIR / _exe("python")), "-m", "eve_skills",
                                     "--help"])
        self.assertEqual((script.returncode, module.returncode), (0, 0))
        self.assertEqual(script.stdout, module.stdout)
        self.assertIn("usage: eve-skills", script.stdout)

    def test_installed_copy_serves_the_bundled_sde_data(self):
        proc = self.run_installed([str(self.venv / SCRIPTS_DIR / _exe(CONSOLE_SCRIPT)), "doctor", "--json"])
        report = json.loads(proc.stdout)
        self.assertEqual(report["versions"]["package"], __version__)

        package = next(check for check in report["checks"] if check["name"] == "package")
        self.assertTrue(Path(package["module_dir"]).is_relative_to(self.venv),
                        f"ran from {package['module_dir']}, not the installed copy")

        documents = {entry["name"]: entry
                     for entry in next(c for c in report["checks"] if c["name"] == "data.alpha_caps")["files"]}
        self.assertEqual(set(documents), set(SDE_DATA_FILES))
        self.assertEqual([name for name, entry in documents.items() if not entry["present"]], [],
                         "an SDE document did not ship in the wheel")
        self.assertEqual([name for name, entry in documents.items()
                          if entry["present"] and entry["origin"] != "bundled package"], [],
                         "SDE data resolved outside the installed package")

        catalog = next(c for c in report["checks"] if c["name"] == "data.skill_catalog")
        self.assertTrue(catalog["present"], "the skill catalog is not installed")


if __name__ == "__main__":
    unittest.main()
